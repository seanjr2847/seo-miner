#!/usr/bin/env python3
"""전체 수집 체인 러너 (run_all.py) — /capture run 한 줄 실행.

수집기와 스코어링·리포트를 정해진 순서로 호출하고, 각 단계가 돌려준
StageResult 를 그대로 호출자에게 넘긴다.

실행 순서 및 이유:
  1. gsc         : 다른 모든 판정의 기본 재료. 실패 시 뒤가 빈손이므로 체인 중단.
  2. ga4         : 클릭 뒤(세션·전환·이탈) — GSC 의 page 축과 잇는다. 속성 미연결이면 건너뜀.
  3. index       : 색인은 순위 이전의 문제. GSC 최신 스냅샷 상위 페이지 대상.
  4. keywords    : 자동완성 키워드 발굴 (expand_keywords.py --mode all).
  5. rank        : 유료 SERP 순위 스냅샷. 키 없으면 건너뜀.
  6. ai          : 유료 AI 인용 체크. 키 없으면 건너뜀.
  7. competitors : 유료 DataForSEO Labs 역키워드. 키 없으면 건너뜀.
  8. gaps        : scoring.py load <project> (수집 결과를 읽어 기회 데이터 적재).
  9. pages       : 내 페이지 HTML 감사 (기회에 걸린 URL 부터, 비용 0).
  10. report     : dashboard.py --export --project <project> (리포트 HTML 박제).

설계 원칙:
  - 표(STAGES)에는 디스패치가 한 종류뿐이다. 모든 단계가
    fn(project, *, dry_run, **opts) -> StageResult 다. scoring.load / dashboard.export 는
    직접 호출이다 — 나머지 열 단계와 마찬가지로 in-process 로 돈다. 둘 다 로컬
    sqlite/파일 I/O 뿐이라(외부 네트워크·os.environ 변경 없음) 격리할 이유가 없었고,
    scoring 은 이미 collect_* 절반이 물고 와 있었다.
  - --dry-run 은 각 단계에 위임하여 비용 고지.
  - 하나의 단계가 실패해도 체인은 계속 진행 (단, gsc 실패 시 즉시 중단).
  - run_chain 은 판정도 요약도 하지 않는다 — [(단계 이름, StageResult)] 를 돌려주고,
    종료코드(chain_rc)·총비용(chain_cost)·요약표(print_summary)는 그 결과에서 나온다.
    행 수·비용·산출물 경로가 정수 exit code 로 접혀 버려지던 자리가 이것이다.
"""
import argparse
import inspect
import sys
from collections import namedtuple
from pathlib import Path

# db import를 통해 CAPTURE_HOME/env 자동 로딩 및 콘솔 UTF-8 설정 적용
sys.path.insert(0, str(Path(__file__).resolve().parent))
import collect_ai          # noqa: E402
import collect_backlinks   # noqa: E402
import collect_crawl       # noqa: E402
import collect_ga4         # noqa: E402
import collect_gap         # noqa: E402
import collect_gsc         # noqa: E402
import collect_index       # noqa: E402
import collect_metrics     # noqa: E402
import collect_page        # noqa: E402
import collect_serp        # noqa: E402
import collector           # noqa: E402
import dashboard           # noqa: E402
import db                  # noqa: E402
import expand_keywords     # noqa: E402
import remote              # noqa: E402
import scoring             # noqa: E402
import serp_adapter        # noqa: E402

StageResult = collector.StageResult

ABORT_REASON = "GSC 수집 실패로 체인 중단됨"

SEPARATOR = "=" * 62
SUB_SEPARATOR = "-" * 62


def load_opportunities(project: str, *, dry_run: bool = False, **_opts) -> StageResult:
    """gaps 단계 — scoring.load. 외부 호출 0건이라 --dry-run 플래그가 없다."""
    if dry_run:
        reason = "외부 호출 0건 — 실제 실행 시 기회를 적재합니다 (돌 예정)"
        print(f"[gaps] {reason}")
        return StageResult(ok=True, skipped=True, reason=reason)
    scoring.load(project)
    return StageResult(ok=True)


def export_report(project: str, *, dry_run: bool = False, **_opts) -> StageResult:
    """report 단계 — dashboard.export. 산출물 경로 규칙의 정본은 dashboard.export() 고,
    여기서는 그게 돌려주는 Path 를 그대로 옮긴다(파싱 없음)."""
    if dry_run:
        reason = "외부 호출 0건 — 실제 실행 시 대시보드 리포트 HTML을 내보냅니다 (돌 예정)"
        print(f"[report] {reason}")
        return StageResult(ok=True, skipped=True, reason=reason)
    out = dashboard.export(project)
    return StageResult(ok=True, artifact=str(out))


# 단계 정의. 표에는 디스패치가 한 종류뿐 — fn(project, *, dry_run, **opts) -> StageResult.
# 단계 순서·정의의 정본은 여기 하나다. 어떤 키가 있어야 유료 단계가 도는지는
# check_paid_keys 가 답한다 — 그 판정을 여기에도 적어 두면 두 벌이 되고, 한쪽만
# 고쳐지는 사고가 난다(이 저장소가 반복해서 겪은 것).
#
# module·knobs 는 collector.cli(원격 위임·인자 전달)와 remote.opts_of/app._stage_opts
# (원격 --opt 검증)가 함께 보는 자리다 — "이 단계가 무슨 모듈이고 어떤 노브를
# 받나"를 여기 말고 또 어딘가(STAGE_MODULES 사본, inspect.signature 재조회)에
# 적으면 둘 중 하나만 고쳐지는 사고가 난다.
Stage = namedtuple("Stage", ["name", "desc", "fn", "is_paid", "module", "knobs"],
                    defaults=(None, {}))


def _knobs(mod) -> dict:
    """mod._parser() 가 노출하는 CLI 플래그를 dest -> (type, default) 로 편다.

    collect() 의 명시 키워드 인자가 아닌 dest(conn·post·fetch 같은 테스트 주입
    전용, 또는 이름이 어긋난 것)는 걸러낸다 — --opt STAGE.KEY 도, 원격 opts_of 도
    거기까지는 안 닿게. mod 가 없으면(gaps·report — 자체 모듈이 아니라 이 파일의
    함수라 노브가 없다) 빈 dict.
    """
    if mod is None:
        return {}
    params = inspect.signature(mod.collect).parameters
    out = {}
    for act in mod._parser()._actions:
        dest = act.dest
        if dest in ("help", "project", "dry_run"):
            continue
        if dest not in params or params[dest].kind == inspect.Parameter.VAR_KEYWORD:
            continue
        out[dest] = (act.type, act.default)
    return out


def _stage(name, desc, fn, is_paid, module=None) -> Stage:
    return Stage(name, desc, fn, is_paid, module, _knobs(module))


STAGES = (
    _stage("gsc",         "GSC 실적 적재 (합계·일별·디바이스 분해)", collect_gsc.collect,     False, collect_gsc),
    _stage("ga4",         "GA4 실적 적재 (세션·전환·이탈, 랜딩페이지별)", collect_ga4.collect, False, collect_ga4),
    _stage("index",       "URL 색인 상태 수집",                      collect_index.collect,   False, collect_index),
    _stage("keywords",    "자동완성 키워드 발굴",                    expand_keywords.collect, False, expand_keywords),
    # 볼륨은 발굴 뒤·기회 적재 앞이어야 한다 — 점수가 볼륨을 재료로 쓴다.
    _stage("metrics",     "키워드 지표 (검색량·난이도·CPC)",         collect_metrics.collect, True, collect_metrics),
    _stage("rank",        "순위 스냅샷 수집",                        collect_serp.collect,    True, collect_serp),
    _stage("crawl",       "사이트 크롤 (깨진 링크·리다이렉트·고아)", collect_crawl.collect,   False, collect_crawl),
    _stage("ai",          "AI 인용 체크",                            collect_ai.collect,      True, collect_ai),
    _stage("competitors", "경쟁사 탐지·역키워드·트래픽 몫 (Labs)",   collect_gap.collect,     True, collect_gap),
    _stage("backlinks",   "백링크 프로필·앵커·링크 교집합",          collect_backlinks.collect, True, collect_backlinks),
    _stage("gaps",        "기회 적재 (외부 호출 0)",                 load_opportunities,      False),
    _stage("pages",       "내 페이지 감사 (제목·설명·본문·스키마)",  collect_page.collect,    False, collect_page),
    _stage("report",      "리포트 HTML 박제",                        export_report,           False),
)

VALID_STAGE_NAMES = tuple(s.name for s in STAGES)
STAGE_BY_NAME = {s.name: s for s in STAGES}


def check_paid_keys(stage_name: str) -> tuple[bool, str]:
    """유료 수집 단계의 환경변수 키 존재 여부를 확인합니다.

    Returns:
        (키_보유_여부, 미보유시_건너뜀_사유_문구)
    """
    if stage_name == "rank":
        if not (serp_adapter.has_serper() or serp_adapter.has_dataforseo()):
            return False, ("SERPER_API_KEY 또는 DATAFORSEO_LOGIN/DATAFORSEO_PASSWORD 가 없어 건너뜀 — "
                          "있으면 순위 스냅샷을 캘 수 있습니다. 발급: https://dataforseo.com "
                          "(권장) 또는 https://serper.dev")
    elif stage_name == "ai":
        if not serp_adapter.has_openrouter():
            return False, ("OPENROUTER_API_KEY 가 없어 건너뜀 — 있으면 AI 인용 갭을 캘 수 있습니다. "
                          "발급: https://openrouter.ai/keys")
    elif stage_name in ("metrics", "backlinks"):
        if not serp_adapter.has_dataforseo():
            what = ("검색량·난이도를 캘 수 있습니다" if stage_name == "metrics"
                    else "백링크 프로필과 링크 교집합을 캘 수 있습니다")
            return False, (f"DATAFORSEO_LOGIN/DATAFORSEO_PASSWORD 가 없어 건너뜀 — 있으면 {what}. "
                          "발급: https://dataforseo.com")
    elif stage_name == "competitors":
        if not serp_adapter.has_dataforseo():
            return False, ("DATAFORSEO_LOGIN/DATAFORSEO_PASSWORD 가 없어 건너뜀 — 있으면 경쟁사가 "
                          "잡는데 나는 부재인 키워드를 캘 수 있습니다. 발급: https://dataforseo.com")
    return True, ""


def _stage_names(raw: str | None, label: str, valid: tuple[str, ...]) -> set[str]:
    """쉼표 구분 문자열 하나를 단계 이름 집합으로. 이름이 틀리면 ValueError."""
    names = {x.strip() for x in (raw or "").split(",") if x.strip()}
    invalid = names - set(valid)
    if invalid:
        raise ValueError(
            f"유효하지 않은 {label} 단계 이름: {', '.join(sorted(invalid))} — "
            f"가능한 단계: {', '.join(valid)}"
        )
    return names


def _coerce(v: str, type_fn=None):
    """--opt 값의 타입 변환. type_fn 이 있으면 그걸 신뢰한다(stage.knobs 가 정본 —
    파서가 --depth 를 int 로 등록했으면 그대로 int() 다. bool("false")==True 인
    파이썬 함정이 있어 bool 은 따로 다룬다). 모르는 단계·키(방금 추가돼 아직 표에
    없는 것 등)라 type_fn 이 없으면 예전처럼 이름으로 추정한다."""
    s = v.strip()
    if type_fn is bool:
        return s.lower() == "true"
    if type_fn is not None:
        return type_fn(s)
    low = s.lower()
    if low in ("true", "false"):
        return low == "true"
    for cast in (int, float):
        try:
            return cast(s)
        except ValueError:
            pass
    return s


def parse_opts(items: list[str] | None) -> dict[str, dict]:
    """`--opt rank.device=mobile` 형태를 {단계: {kwarg: 값}} 으로 편다.

    수집기들이 이미 노출한 노브(--provider --depth --device --ids --force --limit
    --breakdown --row-limit --mode)가 체인으로도 도달하게 하는 통로다. 값의 타입은
    STAGE_BY_NAME[stage].knobs 가 안다 — 없으면(단계 이름 자체가 틀린 경우 등)
    이름으로 추정한다.
    """
    out: dict[str, dict] = {}
    for item in items or []:
        target, sep, value = (item or "").partition("=")
        if not sep:
            raise ValueError(f"--opt 형식이 아닙니다 (STAGE.KEY=VALUE): {item}")
        stage_name, dot, key = target.partition(".")
        if not dot or not key.strip() or not stage_name.strip():
            raise ValueError(f"--opt 형식이 아닙니다 (STAGE.KEY=VALUE): {item}")
        bag = out.setdefault(stage_name.strip(), {})
        k = key.strip().replace("-", "_")
        stg = STAGE_BY_NAME.get(stage_name.strip())
        type_fn = stg.knobs.get(k, (None, None))[0] if stg else None
        coerced = _coerce(value, type_fn)
        # 같은 키가 두 번 이상 오면 리스트로 모은다 — argparse 의 append 인자
        # (competitors.domain)가 통로를 지나 리스트로 도착해야 한다. 덮어쓰면
        # 도메인 세 개를 준 사용자가 마지막 하나만 잰 결과를 받는다.
        if k in bag:
            cur = bag[k]
            bag[k] = (cur if isinstance(cur, list) else [cur]) + [coerced]
        else:
            bag[k] = coerced
    return out


def _run_stage(stage, project: str, *, dry_run: bool, skip_set: set[str],
               only_set: set[str], opts: dict) -> StageResult:
    """단계 하나 — 건너뛸 이유를 먼저 보고, 아니면 fn 을 부른다."""
    if stage.name in skip_set:
        reason = "--skip 옵션으로 건너뜀"
        print(f"[{stage.name}] {reason}")
        return StageResult(ok=True, skipped=True, reason=reason)

    if only_set and stage.name not in only_set:
        reason = "--only 대상이 아니므로 건너뜀"
        print(f"[{stage.name}] {reason}")
        return StageResult(ok=True, skipped=True, reason=reason)

    if stage.is_paid:
        has_keys, paid_skip_msg = check_paid_keys(stage.name)
        if not has_keys:
            print(f"[{stage.name}] {paid_skip_msg}")
            return StageResult(ok=True, skipped=True, reason=paid_skip_msg)

    try:
        return stage.fn(project=project, dry_run=dry_run, **opts)
    except Exception as e:
        return StageResult(ok=False, reason=f"예외 발생 ({e})")


def run_chain(
    project: str,
    *,
    dry_run: bool = False,
    skip: str | None = None,
    only: str | None = None,
    opts: dict[str, dict] | None = None,
    stages: tuple = STAGES,
    on_stage=None,
) -> list[tuple[str, StageResult]]:
    """전체 수집 체인을 순서대로 실행하고 각 단계의 결과를 그대로 돌려줍니다.

    Args:
        project: 프로젝트 이름
        dry_run: 실제 실행 대신 호출 계획 및 비용 확인 모드 여부
        skip: 건너뛸 단계 이름 (쉼표 구분 문자열 하나)
        only: 실행할 단계 이름 (쉼표 구분 문자열 하나)
        opts: {단계 이름: {kwarg: 값}} — 그 단계의 collect() 에 그대로 넘어간다
        stages: 실행할 단계 표. 테스트가 가짜 표를 주입하는 자리다.
        on_stage: fn(idx, total, 단계 이름) — 단계 시작마다 불린다. 진행률 보고용이라
            여기서 난 예외는 삼킨다(보고가 수집을 죽이면 안 된다).

    Returns:
        [(단계 이름, StageResult), ...] — stages 순서 그대로, 단계당 한 건.
        종료코드는 chain_rc, 총비용은 chain_cost, 요약표는 print_summary 가 낸다.

    Raises:
        ValueError: skip/only 동시 지정, 잘못된 단계 이름, 잘못된 --opt 대상.
    """
    if skip and only:
        raise ValueError("--skip 과 --only 옵션은 동시에 사용할 수 없습니다.")

    valid = tuple(s.name for s in stages)
    skip_set = _stage_names(skip, "skip", valid)
    only_set = _stage_names(only, "only", valid)

    opts = opts or {}
    unknown = set(opts) - set(valid)
    if unknown:
        raise ValueError(
            f"유효하지 않은 --opt 대상 단계: {', '.join(sorted(unknown))} — "
            f"가능한 단계: {', '.join(valid)}"
        )

    results: list[tuple[str, StageResult]] = []
    total_stages = len(stages)
    gsc_aborted = False

    print(f"\n{SEPARATOR}")
    print(f"체인 러너 시작: 프로젝트 '{project}' (dry_run={dry_run})")
    print(f"{SEPARATOR}")

    for idx, stage in enumerate(stages, start=1):
        # gsc 가 실패했을 때 나머지 단계는 실행하지 않고 중단 상태로 기록
        if gsc_aborted:
            results.append((stage.name, StageResult(ok=True, skipped=True, reason=ABORT_REASON)))
            continue

        if on_stage:
            try:
                on_stage(idx, total_stages, stage.name)
            except Exception:
                pass

        print(f"\n[{idx}/{total_stages}] {stage.name} — {stage.desc}")
        print(SUB_SEPARATOR)

        r = _run_stage(stage, project, dry_run=dry_run, skip_set=skip_set,
                       only_set=only_set, opts=opts.get(stage.name) or {})
        results.append((stage.name, r))

        if not r.ok:
            print(f"\n[오류] 단계 '{stage.name}' 실행 실패 ({r.reason or '실패'})", file=sys.stderr)
            if stage.name == "gsc":
                print(
                    "\n[중단] GSC 실적 수집이 실패하여 체인을 중단합니다. "
                    "나머지 모든 단계가 GSC 데이터를 기본 재료로 사용하므로 진행할 수 없습니다.",
                    file=sys.stderr,
                )
                gsc_aborted = True

    return results


def chain_rc(results: list[tuple[str, StageResult]]) -> int:
    """0: 모든 단계 성공(건너뜀 포함) / 1: 하나 이상 실패."""
    return 1 if any(not r.ok for _, r in results) else 0


def chain_cost(results: list[tuple[str, StageResult]]) -> float:
    """이번 바퀴에 쓴 돈 합계. 각 단계가 StageResult.cost 로 실청구액을 올려 준다."""
    return round(sum(r.cost for _, r in results), 4)


def _label(r: StageResult, dry_run: bool) -> str:
    if not r.ok:
        return "실패"
    if r.reason == ABORT_REASON:
        return "미실행"
    if r.skipped:
        return "건너뜀"
    return "돌 예정" if dry_run else "완료"


def print_summary(project: str, results: list[tuple[str, StageResult]], *,
                  dry_run: bool = False) -> None:
    """결과 리스트를 사람이 읽는 요약표와 다음 작업 안내로 편다."""
    aborted = any(r.reason == ABORT_REASON for _, r in results)

    print(f"\n{SEPARATOR}")
    print(f"체인 실행 결과 요약 ({project})")
    print(SUB_SEPARATOR)
    for name, r in results:
        detail = []
        if r.rows:
            detail.append(f"{r.rows:,}행")
        if r.cost:
            detail.append(f"${r.cost:.4f}")
        if r.reason:
            detail.append(r.reason)
        tail = f" ({' · '.join(detail)})" if detail else ""
        print(f"  {name:<12} | {_label(r, dry_run)}{tail}")
    print(SEPARATOR)

    cost = chain_cost(results)
    if cost:
        print(f"\n이번 바퀴 비용: ${cost:.4f}")

    print("\n다음 작업:")
    if aborted:
        # 여기서 끝나면 사용자는 빈손이다. 인증 없이 도는 유일한 수집으로 안내해
        # 첫 수확이라도 남긴다 — 로그인 실패가 곧 "아무것도 못 봄"이 되지 않게.
        print('  구글 로그인 먼저: 채팅에 "GSC 로그인해줘"')
        print(f"  로그인 없이 지금 되는 것: /capture keywords {project} "
              "(자동완성 — 인증·키 안 씀)\n")
        return

    report = next((r.artifact for name, r in results if name == "report" and r.artifact), "")
    if report:
        print(f"  리포트 파일: {report}")
    print(f"  대시보드 실행: /capture dash {project}")
    top = _top_opportunity(project)
    if top:
        # 리포트 경로만 주고 끝내면 "그래서 뭘 고치나"로 안 이어진다 — 이 도구의
        # 유일한 실제 성과는 고치기(create)이고, 그 문턱을 여기서 한 줄로 낮춘다.
        print(f"  손댈 것 1순위: [{top['kind']}] {top['target']}")
        print(f"  고치러 가기: /create plan {project}")
    # 2회차 수집부터 Δ가 나온다. 그 2회차를 부르는 것이 아무데도 없었다.
    print(f"  다음 바퀴: 1~2주 뒤 같은 명령 한 줄 — /capture run {project} "
          "(구글이 최근 3일 수치를 나중에 채우고, 순위도 주 단위로 움직인다)\n")


def _top_opportunity(project: str):
    """요약에 붙일 기회 1건. 실패해도 요약을 깨뜨리지 않는다 — 장식이지 결과가 아니다."""
    try:
        conn = db.connect()
        try:
            p = db.get_project(conn, project)
            row = conn.execute(
                "SELECT kind, target FROM opportunities "
                " WHERE project_id=? AND status='new' "
                " ORDER BY score DESC, id DESC LIMIT 1", (p["id"],)).fetchone()
            return {"kind": row["kind"], "target": row["target"]} if row else None
        finally:
            conn.close()
    except Exception:
        return None


def _selfcheck() -> None:
    """--opt STAGE.KEY=VALUE 통로(및 collector.cli 가 쓰는 같은 통로)가 실제로
    도는지 못 박는다.

    이 통로는 수집기가 이미 CLI 로 노출한 노브(--limit --depth --device ...)를
    체인으로도, collector.cli 로도 쓰게 하려는 것이다. 그런데 CLI 플래그 이름과
    collect() 의 키워드 인자 이름이 어긋나면 --opt rank.depth=20 같은 흔한 사용이
    TypeError 로 죽는다(collect_index 의 --limit vs 예전 index_urls 가 그 사례).

    각 단계의 파서를 실제로 파싱해(기본값만, --project·--dry-run 만 채워서) 그
    결과를 collect() 에 그대로 흘린다 — collector.cli 가 하는 것과 같은 일이다.
    ProjectNotFound 는 "인자는 받아들여졌다"는 뜻이라 통과로 친다 — TypeError 만
    이 검사가 잡으려는 것이다.
    """
    import os
    import tempfile

    os.environ["CAPTURE_HOME"] = str(Path(tempfile.mkdtemp(prefix="seo-miner-runall-selftest-")))
    for stg in STAGES:
        if stg.module is None:
            continue
        args = stg.module._parser().parse_args(["--project", "__selfcheck__", "--dry-run"])
        kwargs = {k: v for k, v in vars(args).items() if k not in ("project", "dry_run")}
        try:
            stg.module.collect(project=args.project, dry_run=True, **kwargs)
        except db.ProjectNotFound:
            pass    # 인자는 받아들여졌다 — 여기서 잡으려는 건 TypeError 뿐
        except TypeError as e:
            raise AssertionError(f"[{stg.name}] 파서 기본값으로 collect() 를 못 부른다: {e}") from e

    print("run_all self-check ok")


def main() -> None:
    if len(sys.argv) == 1:
        _selfcheck()
        return
    ap = argparse.ArgumentParser(
        description="전체 수집 체인 실행 — "
                    "gsc → ga4 → index → keywords → rank → ai → competitors → gaps → pages → report"
    )
    ap.add_argument("--project", required=True, help="프로젝트 이름")
    ap.add_argument("--dry-run", action="store_true", help="실제 실행 없이 호출 계획 및 비용만 확인")
    ap.add_argument("--skip", help="건너뛸 단계 (쉼표 구분, 예: index,ai)")
    ap.add_argument("--only", help="실행할 단계 (쉼표 구분, 예: gsc,gaps)")
    ap.add_argument("--opt", action="append", metavar="STAGE.KEY=VALUE",
                    help="스테이지별 옵션 (반복 지정). 예: --only rank --opt rank.device=mobile")
    args = ap.parse_args()

    # 원격 사이트면 서버가 같은 run_chain 을 돌고, 그 내레이션을 그대로 받아 찍는다.
    if remote.dispatch(args, None):
        return

    try:
        results = run_chain(
            args.project,
            dry_run=args.dry_run,
            skip=args.skip,
            only=args.only,
            opts=parse_opts(args.opt),
        )
    except ValueError as e:
        print(f"[오류] {e}", file=sys.stderr)
        sys.exit(1)

    print_summary(args.project, results, dry_run=args.dry_run)
    sys.exit(chain_rc(results))


if __name__ == "__main__":
    main()
