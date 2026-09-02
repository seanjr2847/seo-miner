#!/usr/bin/env python3
"""수집기 공통 서두 + 스테이지 러너 — 인자·설정 병합·프로젝트 열기·실행 루프.

수집기 다섯 개가 각자 갖고 있던 argparse 블록·부팅·설정 읽기를 여기로 모았다.
그 전에는 --throttle 기본값만 0.5/0.3/0.5 세 벌이었고 config.yaml 의
defaults·serp 섹션은 읽는 코드가 아예 없었다(장식).

우선순위:  CLI > 프로젝트 yaml > config.yaml defaults > 코드 리터럴

서두만 공유하던 시절에는 실행 루프를 수집기 여섯 개가 각자 다시 썼다 —
dry-run 조기반환 / 오늘-중복 스킵 / db.run 기록 / 항목별 try-except /
conn.commit() / sleep(throttle) / conn.close() / StageResult 조립.
그 껍데기가 이제 Stage 한 벌이다 (아래 stage()).

self-check:  python collector.py
"""
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import db      # noqa: E402
import remote  # noqa: E402

CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"
_cache: dict | None = None


@dataclass
class StageResult:
    """수집기 한 단계의 결과를 run_chain 으로 들고 오기 위한 단일 형태.

    in-process 호출로 바꾸면서 각 단계의 결과를 정수 exit code 하나로
    줄이던 자리에 들어갔다. sys.exit 로는 "건너뜀"과 "진짜 실패"가 구분이
    안 됐기 때문에 skipped / reason 으로 그 자리를 채운다.

    필드:
      ok       True 면 정상 완료 (rows·cost·artifact 가 의미를 가짐)
      skipped  True 면 의도적으로 아무것도 안 한 것 (유료 키 없음, --dry-run 등)
      reason   사용자에게 보일 한국어 문구 — 건너뜀이나 실패의 이유
      rows     이 단계가 Brain 에 넣은 행 수 (모르면 0)
      cost     이 단계가 쓴 돈 (모르면 0.0)
      artifact 만든 파일 경로 (없으면 빈 문자열)
    """
    ok: bool
    skipped: bool = False
    reason: str = ""
    rows: int = 0
    cost: float = 0.0
    artifact: str = ""


def config() -> dict:
    """스킬 레벨 config.yaml. 없거나 깨졌으면 빈 dict — 수집을 막지는 않는다."""
    global _cache
    if _cache is None:
        try:
            import yaml  # lazy: pyyaml 없이도 import는 되게
            _cache = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
        except Exception as e:
            print(f"[경고] config.yaml 을 읽지 못했습니다 ({e}) — 코드 기본값으로 진행합니다.",
                  file=sys.stderr)
            _cache = {}
    return _cache


class _Setting:
    def __init__(self, dest: str, key: str, fallback, type_fn, help_text: str | None):
        self.dest = dest
        self.key = key
        self.fallback = fallback
        self.type_fn = type_fn
        self.help = help_text


def add_setting(ap, flag: str, *, key: str, fallback, type=int, help: str | None = None) -> None:
    """argparse에 default=None으로 등록하고 설정 메타데이터(dest, key, fallback, type)를 ap에 기록한다.

    ap 마다 지역이다 — 모듈 전역에 쌓지 않는다. 프로세스 하나가 수집기 열 개를 돌리며
    _parser() 를 열 번 부르던 시절엔 이걸 전역 dict 에 key 로 쌓아서, 수집기 A 의
    --limit(dest="limit")이 수집기 B 의 --limit(dest 는 같고 key 는 다른)까지 같이
    덮어썼다. ap 지역으로 내리면 그 섞임이 구조적으로 안 생긴다.
    """
    dest = flag.lstrip("-").replace("-", "_")
    spec = _Setting(dest=dest, key=key, fallback=fallback, type_fn=type, help_text=help)
    if not hasattr(ap, "_collector_settings"):
        ap._collector_settings = []
    ap._collector_settings.append(spec)

    kwargs = {"type": type, "default": None}
    if help is not None:
        kwargs["help"] = help
    ap.add_argument(flag, **kwargs)


def settings(args, cfg: dict | None, specs: list) -> dict:
    """specs(이 수집기가 add_setting 으로 등록한 것들, 보통 ap._collector_settings)를
    우선순위대로 해석한다:
    CLI (None 아니면, 0도 유효값) > 프로젝트 yaml > config.yaml defaults > fallback.
    limits.* 키는 프로젝트 yaml의 limits 아래에서 읽는다.
    """
    out = {}
    gcfg_defaults = (config().get("defaults") or {})

    for spec in specs:
        val = None

        # 1. CLI (0도 유효값)
        if args is not None:
            if hasattr(args, spec.dest):
                cli_v = getattr(args, spec.dest)
                if cli_v is not None:
                    val = spec.type_fn(cli_v) if spec.type_fn else cli_v
            elif isinstance(args, dict):
                if spec.dest in args and args[spec.dest] is not None:
                    val = spec.type_fn(args[spec.dest]) if spec.type_fn else args[spec.dest]
                elif spec.key in args and args[spec.key] is not None:
                    val = spec.type_fn(args[spec.key]) if spec.type_fn else args[spec.key]

        # 2. 프로젝트 yaml
        if val is None and cfg and isinstance(cfg, dict):
            if spec.key.startswith("limits."):
                subkey = spec.key.split(".", 1)[1]
                limits_sec = cfg.get("limits")
                if isinstance(limits_sec, dict) and limits_sec.get(subkey) is not None:
                    raw = limits_sec[subkey]
                    val = spec.type_fn(raw) if spec.type_fn else raw
            else:
                if cfg.get(spec.key) is not None:
                    raw = cfg[spec.key]
                    val = spec.type_fn(raw) if spec.type_fn else raw

        # 3. config.yaml defaults
        if val is None:
            if spec.key in gcfg_defaults and gcfg_defaults[spec.key] is not None:
                raw = gcfg_defaults[spec.key]
                val = spec.type_fn(raw) if spec.type_fn else raw

        # 4. Fallback
        if val is None:
            raw = spec.fallback
            val = spec.type_fn(raw) if (spec.type_fn and raw is not None) else raw

        out[spec.key] = val
        out[spec.dest] = val

    return out


def add_common(ap, *, dry_run: bool = True) -> None:
    """모든 수집기가 갖는 인자. --project 는 사이트 이름, --dry-run 은 비용 고지용."""
    ap.add_argument("--project", required=True)
    if dry_run:
        ap.add_argument("--dry-run", action="store_true",
                        help="실제 호출·저장 없이 무엇을 할지만 보여준다")


def project_cfg(name: str) -> dict:
    """프로젝트 yaml — 없거나 pyyaml이 없으면 경고하고 빈 dict.

    yaml은 기본값·브랜드 별칭 같은 부가 정보라 없다고 수집을 막으면 안 된다.
    부가 설정 파일 때문에 수집 자체가 죽으면 안 된다. 대신 조용히 넘어가지는 않는다 —
    별칭이 빠지면 브랜드 판정이 달라지므로 왜 달라졌는지 말해야 한다.
    """
    try:
        return db.load_project_yaml(name)
    except (db.ProjectConfigNotFound, Exception) as e:   # load_project_yaml은 없으면 ProjectConfigNotFound
        print(f"[경고] '{name}' 프로젝트 설정(yaml)을 읽지 못했습니다 ({e or '없음'}) — "
              "기본값으로 진행합니다. 브랜드 별칭·한도 설정은 적용되지 않습니다.",
              file=sys.stderr)
        return {}


def today_clause(col: str) -> str:
    """'그 값이 오늘인가' — 저장된 시각이 UTC 든 localtime 이든 오늘로 친다.

    ? 자리 두 개를 만든다. 값은 Stage.seen_today 가 채운다 —
    "오늘"을 만드는 자리를 수집기마다 두지 않으려는 것이다.
    """
    return f"(date({col}) = ? OR date({col}, 'localtime') = ?)"


class Stage:
    """수집기 한 단계의 실행 문맥 — conn 수명·db.run 기록·항목별 오류 집계를 소유한다.

    수집기가 러너에 주는 것은 "항목마다 무엇을 가져와 무엇을 쓸지"(each 의 fn)
    뿐이다. 언제 멈추고·얼마를 쉬고·오류를 어떻게 세고·무엇을 돌려줄지는 러너가
    갖는다.

    conn.close() 가 파일마다 3~7번 흩어져 있던 것이 이 클래스를 만든 이유다 —
    조기반환 분기를 하나 더 만들 때마다 닫기를 빠뜨릴 자리가 하나씩 늘었다.
    이제 닫는 자리는 __exit__ 하나다.
    """

    def __init__(self, conn, project, cfg, *, dry_run: bool = False, own: bool = True):
        self.conn = conn
        self.project = project
        self.cfg = cfg
        self.dry_run = dry_run
        self.throttle = 0.0     # settings() 가 물고 온다
        self.errors = 0
        self._own = own

    @property
    def pid(self) -> int:
        return self.project["id"]

    def __enter__(self) -> "Stage":
        return self

    def __exit__(self, *exc) -> bool:
        if self._own:
            self.conn.close()
        return False            # 예외는 그대로 올린다

    def settings(self, ap, args) -> dict:
        """이 단계의 설정 해석. ap 는 이 수집기의 _parser() 가 만든 것 — 거기 등록된
        설정만 푼다(다른 수집기 것과 안 섞인다). throttle 은 러너가 바로 물고 간다 —
        수집기마다 같은 값을 자기 지역변수로 다시 옮기던 자리다."""
        s = settings(args, self.cfg, getattr(ap, "_collector_settings", []))
        if s.get("throttle") is not None:
            self.throttle = float(s["throttle"])
        return s

    def record(self, kind: str):
        """runs 한 줄 — db.run 위임. 예외가 나도 finished_at 이 남는다."""
        return db.run(self.conn, self.pid, kind)

    @property
    def today(self) -> str:
        return datetime.now().strftime("%Y-%m-%d")

    def seen_today(self, sql: str, params: tuple = (), *, force: bool = False) -> set:
        """오늘 이미 본 것들의 키 집합. force 면 빈 집합 — 전부 다시 본다.

        무엇이 '봤다'인지는 단계마다 다르다(순위는 rank_snapshots.checked_at,
        AI 는 runs.started_at). 그래서 SQL 은 수집기가 주되, 오늘을 만드는 자리와
        --force 의 뜻은 여기 한 곳이다. sql 끝에는 today_clause() 를 붙이고,
        그 ? 두 자리는 여기서 채운다.
        """
        if force:
            return set()
        rows = self.conn.execute(sql, (*params, self.today, self.today)).fetchall()
        return {(tuple(r) if len(r) > 1 else r[0]) for r in rows}

    @staticmethod
    def skip_note(n: int) -> str:
        """요약 줄에 붙는 '몇 개 건너뛰었다' — 안 건너뛰었으면 빈 문자열."""
        return f" (skipped {n} 오늘 이미 확인)" if n else ""

    def each(self, items, fn, *, label=None) -> int:
        """항목마다 fn(item). 그 항목에서 난 예외는 여기서 잡아 세고 다음으로 간다.

        열 개 중 하나가 죽었다고 아홉을 잃지 않는다. 수집기 셋(serp/gap/ai)이
        글자 그대로 같은 블록을 갖고 있던 자리다 — errors += 1 + stderr 한 줄 /
        conn.commit() / sleep(throttle).

        label(item) 은 오류 줄에 찍을 이름. 반환값은 예외 없이 끝난 항목 수.

        항목마다 commit 한다 — 도중에 죽어도 거기까지는 남는다. conn 을 빌려
        준 호출자가 자기 트랜잭션을 열어 둔 채로 부르면 그것도 같이 커밋된다.
        """
        done = 0
        for item in items:
            try:
                fn(item)
                done += 1
            except Exception as e:
                self.errors += 1
                print(f"  ! {label(item) if label else item}: {e}", file=sys.stderr)
            self.conn.commit()
            time.sleep(self.throttle)
        return done

    # ── StageResult 조립 — 세 가지 끝맺음 ────────────────────────────
    def skip(self, reason: str) -> StageResult:
        """사유 있는 비종료 (키 없음·구성 누락 등)."""
        return StageResult(ok=False, skipped=True, reason=reason)

    def noop(self, **kw) -> StageResult:
        """의도적으로 아무것도 안 함 (--dry-run, 할 일 0건)."""
        return StageResult(ok=True, skipped=True, **kw)

    def done(self, **kw) -> StageResult:
        """정상 완료."""
        return StageResult(ok=True, skipped=False, **kw)


def stage(name: str, *, conn=None, dry_run: bool = False) -> Stage:
    """수집기 한 단계를 연다 — with 에 넣으면 conn 수명이 러너 것이 된다.

    yaml은 등록할 때 기록해 둔 config_path 로 찾는다. 이름만으로 찾으면
    표준 폴더(~/.capture/projects) 밖에 둔 설정을 조용히 놓친다.

    conn 을 주면 그것을 쓰고 닫지 않는다 — 빌린 것은 안 닫는다. 안 주면 여기서
    열고 __exit__ 에서 닫는다(지금까지의 동작 그대로). 테스트가 globals() 를
    갈아끼우던 자리가 이 인자다.
    """
    own = conn is None
    if own:
        conn = db.connect()
    try:
        p = db.get_project(conn, name)
    except BaseException:
        if own:
            conn.close()        # 프로젝트가 없어도 방금 연 conn 은 닫고 나간다
        raise
    return Stage(conn, p, project_cfg(p["config_path"] or name),
                 dry_run=dry_run, own=own)


def open_project(name: str):
    """(conn, project_row, project_cfg) — 러너 이전의 호출 형태. 닫기는 호출자 몫.

    새 코드는 stage() 를 쓴다.
    """
    st = stage(name)
    return st.conn, st.project, st.cfg


def cli(stage_name: str) -> None:
    """수집기 main() 을 대체하는 한 줄 — collect_*.py 열 개가 각자 다시 쓰던
    parser 생성 → 원격 위임 → collect() 호출 → 종료코드 변환을 여기 하나로 모았다.

    단계 이름 -> 모듈 -> parser 의 정본은 run_all.STAGES 하나다(순환 임포트를 피하려고
    여기서 늦게 읽는다). 파싱된 인자는 project·dry_run 만 이름을 갈라 나머지는 그대로
    collect(**kwargs) 에 넘긴다 — run_all 의 `--opt STAGE.KEY=VALUE` 통로와 같다:
    argparse 의 dest 이름이 곧 collect() 의 키워드 인자 이름이어야 한다(어긋나면 여기서
    바로 TypeError 로 죽는다).
    """
    import run_all   # 늦은 import — run_all 이 수집기 열 개를, 수집기가 이 모듈을 읽는다

    st = run_all.STAGE_BY_NAME.get(stage_name)
    if st is None or st.module is None:
        raise RuntimeError(f"'{stage_name}' 은 run_all.STAGES 에 모듈이 딸린 단계가 아니다")

    ap = st.module._parser()
    args = ap.parse_args()
    if remote.dispatch(args, stage_name):   # 원격 사이트면 서버가 돈다
        return

    kwargs = {k: v for k, v in vars(args).items() if k not in ("project", "dry_run")}
    try:
        r = st.module.collect(args.project, dry_run=args.dry_run, **kwargs)
    except db.ProjectNotFound as e:
        sys.exit(str(e))
    if not r.ok and r.reason:
        sys.exit(r.reason)


def _selfcheck() -> None:
    import argparse
    ap = argparse.ArgumentParser()
    add_setting(ap, "--depth", key="serp_depth", fallback=10, type=int)
    add_setting(ap, "--throttle", key="throttle", fallback=0.7, type=float)
    add_setting(ap, "--max-keywords", key="limits.max_keywords", fallback=99, type=int)
    add_setting(ap, "--custom", key="custom_key", fallback=42, type=int)

    specs = ap._collector_settings

    # 1. CLI 최우선 + 0도 유효값 (fallback을 이긴다)
    a = ap.parse_args(["--depth", "3", "--max-keywords", "0", "--throttle", "0"])
    s = settings(a, {"serp_depth": 9, "limits": {"max_keywords": 50}, "throttle": 1.0}, specs)
    assert s["serp_depth"] == 3
    assert s["limits.max_keywords"] == 0, "CLI 0이 프로젝트 yaml/fallback을 이겨야 한다"
    assert s["max_keywords"] == 0
    assert s["throttle"] == 0.0, "CLI 0.0이 이겨야 한다"

    # 2. 프로젝트 yaml
    a_none = ap.parse_args([])
    s_yaml = settings(a_none, {"serp_depth": 9, "limits": {"max_keywords": 5}}, specs)
    assert s_yaml["serp_depth"] == 9
    assert s_yaml["limits.max_keywords"] == 5

    # 3. config.yaml defaults
    s_cfg = settings(a_none, None, specs)
    assert s_cfg["throttle"] in (0.5, 0.7)
    assert s_cfg["serp_depth"] == 10

    # 4. 코드 fallback
    s_fb = settings(a_none, {}, specs)
    assert s_fb["custom_key"] == 42
    assert s_fb["limits.max_keywords"] == 99

    # 5. 격리 — 다른 수집기가 같은 dest, 다른 key로 등록해도 안 섞인다.
    #    (run_all 이 한 프로세스에서 수집기 여러 개를 부를 때 --limit 이 dest="limit"로
    #    6번 등록되던 자리 — 예전엔 전역 registry 라 한쪽 CLI 값이 나머지 다섯 키를
    #    같이 덮어썼다. 지금 코드로 이 assert 만 빼고 돌리면 재현된다.)
    ap_a, ap_b = argparse.ArgumentParser(), argparse.ArgumentParser()
    add_setting(ap_a, "--limit", key="crawl_urls", fallback=300, type=int)
    add_setting(ap_b, "--limit", key="limits.backlink_limit", fallback=200, type=int)
    s_a = settings(ap_a.parse_args(["--limit", "5"]), None, ap_a._collector_settings)
    assert s_a == {"crawl_urls": 5, "limit": 5}, s_a
    assert "limits.backlink_limit" not in s_a, "다른 수집기의 키가 섞였다"

    _runner_check()
    print("collector self-check ok")


def _runner_check() -> None:
    """러너를 실제로 돌린다 — 가짜 수집기 하나로 dry-run 단락 / 오류 집계 /
    conn 닫힘 / 오늘-중복 스킵 / StageResult 모양을 확인한다.
    임시 CAPTURE_HOME 에서만 돈다 (진짜 Brain 안 건드림)."""
    import contextlib
    import io
    import os
    import sqlite3
    import tempfile

    os.environ["CAPTURE_HOME"] = str(
        Path(tempfile.mkdtemp(prefix="seo-miner-collector-selftest-")))
    boot = db.connect()
    boot.execute("INSERT INTO projects(name, domain, locale) VALUES('rt','rt.com','ko-KR')")
    boot.commit()
    boot.close()

    # 1. dry-run 단락 + conn 이 닫힌다.
    with stage("rt", dry_run=True) as st:
        assert st.dry_run and st.project["name"] == "rt"
        res = st.noop(cost=1.25)
        leaked = st.conn
    assert (res.ok, res.skipped, res.cost, res.rows) == (True, True, 1.25, 0)
    try:
        leaked.execute("SELECT 1")
        raise AssertionError("러너가 conn 을 닫지 않았다")
    except sqlite3.ProgrammingError:
        pass

    # 2. 항목 하나가 죽어도 나머지는 간다 — 오류는 세고 stderr 에 남긴다.
    err = io.StringIO()
    with stage("rt") as st:
        seen: list[str] = []

        def one(kw: str) -> None:
            if kw == "b":
                raise RuntimeError("터짐")
            seen.append(kw)
            st.conn.execute(
                "INSERT INTO keywords(project_id, keyword, locale, source) VALUES(?,?,?,?)",
                (st.pid, kw, "ko-KR", "seed"))

        with st.record("rank") as r:
            with contextlib.redirect_stderr(err):
                n = st.each(["a", "b", "c"], one, label=lambda kw: f"kw={kw}")
            r.api_calls = n
        res = st.done(rows=n)
        run_row = st.conn.execute(
            "SELECT api_calls, finished_at FROM runs WHERE project_id=?", (st.pid,)).fetchone()
        assert run_row["api_calls"] == 2 and run_row["finished_at"], dict(run_row)
    assert (n, st.errors, seen) == (2, 1, ["a", "c"]), (n, st.errors, seen)
    assert "kw=b: 터짐" in err.getvalue(), err.getvalue()
    assert (res.ok, res.skipped, res.rows) == (True, False, 2)

    # 3. 빌린 conn 은 러너가 닫지 않는다 (테스트·호출자가 재사용하는 자리).
    borrowed = db.connect()
    with stage("rt", conn=borrowed) as st:
        assert st.conn is borrowed
        kid = st.conn.execute("SELECT id FROM keywords WHERE keyword='a'").fetchone()["id"]
        db.write_rank_snapshot(st.conn, kid, 3, "https://rt.com/a", checked_at=db.now())
        st.conn.commit()

        # 4. 오늘-중복 스킵 한 벌 — force 는 그것을 끈다.
        sql = f"SELECT keyword_id FROM rank_snapshots WHERE {today_clause('checked_at')}"
        assert st.seen_today(sql) == {kid}
        assert st.seen_today(sql, force=True) == set(), "force 면 오늘-중복 스킵을 끈다"
        assert Stage.skip_note(2) == " (skipped 2 오늘 이미 확인)"
        assert Stage.skip_note(0) == ""
    borrowed.execute("SELECT 1")     # 안 닫혔다 — 닫혔으면 여기서 터진다
    borrowed.close()

    # 5. 사유 있는 비종료.
    with stage("rt") as st:
        res = st.skip("사유")
    assert (res.ok, res.skipped, res.reason) == (False, True, "사유")

    print("collector runner self-check ok")


if __name__ == "__main__":
    _selfcheck()
