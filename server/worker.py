"""수집 워커 — 유저별 env 를 갈아끼우고 기존 수집 체인(run_chain) 을 호출만 한다.

수집 엔진(skills/capture/scripts/*) 은 한 줄도 손대지 않는다. 격리는 env
(CAPTURE_HOME, GSC_TOKEN_FILE, 유료 API 키) 를 호출 시마다 다시 읽기 때문에
process-global os.environ 을 잠깐 갈아끼우는 것으로 끝난다.

CLI:
  python server/worker.py                              # demo — API 호출 없음
  python server/worker.py --all [--idle-days 30] ...   # 스케줄 대상 직렬 실행
  python server/worker.py --user <id> --project <p> .. # 단일 사이트
"""
from __future__ import annotations

import argparse
import contextlib
import io
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(ROOT / "skills" / "capture" / "scripts"))

import db                                     # noqa: E402
import exports                                # noqa: E402
import mailer                                 # noqa: E402
import run_all                                # noqa: E402
import settings                                # noqa: E402
import store                                  # noqa: E402

# 유료 키 주입은 settings.paid_keys() 가 한다 — 명명 규칙(PAID_KEYS/SERVER_PREFIX)의
# 주인이 거기고, /api/doctor 도 같은 컨텍스트를 둘러야 판정이 런과 같아진다.


def activate_from_gsc(project: str, limit: int | None = None) -> int:
    """서치콘솔에 노출된 키워드를 노출 순으로 활성화한다. 반환: 새로 켠 개수.

    로컬에서는 Claude 가 관련성을 보고 골라 준다(capture SKILL.md 의 큐레이션 단계).
    웹에는 그 자리에 사람이 없으므로 실측만 믿는다 — '구글이 이미 내 페이지를 이
    검색어에 노출시켰다'는 사실. 자동완성 후보(노출 0)는 관련성이 확인되지 않아
    그대로 후보로 둔다. 활성 키워드가 0 이면 rank 단계가 잴 대상 없이 돈다.
    """
    limit = limit or settings.count("SEOMINER_MAX_KEYWORDS")
    conn = db.connect()
    try:
        pid = db.get_project(conn, project)["id"]
        # 이미 켜 둔 것까지 세어 남은 자리만 채운다. 매번 limit 개씩 더 켜면
        # 추적 세트가 런마다 불어나고, SERP 는 키워드당 과금이라 비용이 샌다.
        active = db.count_active_keywords(conn, pid)
        room = max(0, limit - active)
        if room == 0:
            return 0
        # "노출 순으로 골라 room 개" 는 db.py 에 없다 (GSC 조인이 필요한 선택 로직이라
        # db.count_active_keywords/set_keywords_active 와는 층이 다르다) — 여기 남긴다.
        # TODO(db.py): 이 선택 로직이 다른 곳에서도 필요해지면 db.py 로 옮긴다.
        ids = [r[0] for r in conn.execute(
            "SELECT k.id FROM keywords k"
            "  JOIN gsc_snapshots g ON g.project_id=k.project_id AND g.query=k.keyword"
            " WHERE k.project_id=? AND k.is_active=0"
            " GROUP BY k.id ORDER BY SUM(g.impressions) DESC LIMIT ?",
            (pid, room)).fetchall()]
        return db.set_keywords_active(conn, pid, ids, True)
    finally:
        conn.close()


def _mail_stats(project: str) -> dict:
    """알림에 넣을 숫자. 없는 값은 넣지 않는다 — 0 으로 지어내면 거짓말이 된다.

    집계는 exports.summary() 하나로 — 최신 스냅샷 클릭/노출, 신규 기회 수·최상위
    기회, 활성 키워드 수는 화면 요약과 같은 쿼리여야 값이 어긋나지 않는다.
    """
    out: dict = {}
    try:
        s = exports.summary(project)
        if s["impressions"]:
            out["clicks"], out["impressions"] = s["clicks"] or 0, s["impressions"]
        if s["opportunities"]:
            out["opportunities"] = s["opportunities"]
            if s["top_opportunity"]:
                out["top"] = s["top_opportunity"]
        if s["keywords_active"]:
            out["keywords"] = s["keywords_active"]
    except Exception:
        pass                      # 통계를 못 모아도 메일은 보낸다
    return out


def _email_of(conn, site) -> str | None:
    """due_sites() 는 email 을 JOIN 해 주지만 sites() 는 아니다 — 웹에서 실행한
    단일 사이트 경로(--user --project)가 여기서 터졌다."""
    try:
        return site["email"]
    except (IndexError, KeyError):
        r = conn.execute("SELECT email FROM users WHERE id=?",
                         (site["user_id"],)).fetchone()
        return r["email"] if r else None


def _latest_report(project: str) -> bytes | None:
    """report 단계가 방금 만든 보고서. tenant 안에서만 부른다."""
    try:
        files = sorted((db.CAPTURE_HOME / "reports" / project).glob("*.html"))
        return files[-1].read_bytes() if files else None
    except Exception:
        return None


def backlinks_plan(skip: str | None, every_days: float) -> tuple[str | None, dict]:
    """백링크 주기를 체인이 알아듣는 모양으로 — (skip, opts).

    여태 이 판정이 체인 **밖**에 따로 있었다(_backlinks_due). 백링크가 수집 단계가
    되면서 신선도 판정이 두 벌이 됐고, 그건 한 런에 두 번 사는 길이다. 유료 호출이라
    그게 곧 돈이다. 0 은 신선도로 표현할 수 없으므로(0일보다 오래됐나 = 늘 참) 단계를
    통째로 건너뛴다.
    """
    if every_days <= 0:
        return ",".join(x for x in [skip, "backlinks"] if x), {}
    return skip, {"backlinks": {"max_age": every_days}}


class _Tee(io.StringIO):
    """잡으면서 흘린다 — 런 내레이션을 DB 로 가져간다고 운영자가 보던 콘솔 로그
    (Railway) 를 뺏으면 안 된다. redirect_stdout 이 이걸 받는다."""

    def __init__(self, out):
        super().__init__()
        self._out = out

    def write(self, s):
        try:
            self._out.write(s)
            self._out.flush()
        except Exception:
            pass                      # 콘솔이 죽었다고 수집을 죽이지 않는다
        return super().write(s)


def run_site(conn, site, *, dry_run: bool = False, skip: str | None = None,
             only: str | None = None, opts: dict[str, dict] | None = None) -> dict:
    """사이트 1건 처리. tenant() 안에서 유료 키를 주입하고 run_chain 을 호출.

    only 를 주면 그 단계만 돈다 — 웹에서 '구글 실적만 다시 읽기' 같은 부분 실행에 쓴다.
    opts 는 `--opt STAGE.KEY=VALUE` 로 들어온 단계별 노브다(원격 CLI 가 쓴다).

    체인이 뱉는 내레이션은 그대로 잡아 sites.run_log 에 둔다 — 원격 CLI 는 자기가
    요약표를 다시 만들지 않고 이걸 받아 그대로 print 한다(문구는 한 벌이다).
    ponytail: 런당 1벌만 보관. 이력이 필요해지면 runs 테이블로.
    """
    user_id = site["user_id"]
    project = site["project"]
    # 시작 전에 찍는다. 끝나고 찍으면 (1) 등록 직후 트리거와 60초 스케줄러 틱이 겹쳐
    # 같은 사이트를 두 번 수집하고, (2) 실패한 사이트가 매 틱마다 재시도해 비용이 샌다.
    if not dry_run:
        store.mark_run(conn, site["id"])
        store.save_run_log(conn, site["id"], "")     # 지난 런의 로그를 남기지 않는다

    buf = _Tee(sys.stdout)

    # 화면이 폴링으로 읽는 값. 단계가 끝난 만큼만 센다 — 3단계를 시작한 시점의
    # 진행률은 2/8 이지, 3/8 이 아니다.
    # 로그도 여기서 흘려 보낸다 — 끝나고 한 번만 쓰면 몇 분짜리 런 내내 화면이 빈다.
    def on_stage(idx, total, name):
        if not dry_run:
            store.mark_stage(conn, site["id"], name, round((idx - 1) * 100 / total))
            store.save_run_log(conn, site["id"], buf.getvalue())

    # 백링크는 하루 단위로 안 움직인다 — 자체 주기(기본 30일)로만 잰다.
    skip, chain_opts = backlinks_plan(skip, settings.num("SEOMINER_BACKLINKS_EVERY_DAYS"))
    # 요청이 준 노브가 이긴다. 단계 단위가 아니라 키 단위로 합친다 — 단계째로 덮으면
    # 위에서 정한 백링크 주기가 사라져 한 런에 두 번 산다.
    for name, kv in (opts or {}).items():
        chain_opts.setdefault(name, {}).update(kv)

    try:
        with store.tenant(conn, user_id), settings.paid_keys():
            with contextlib.redirect_stdout(buf):
                results = run_all.run_chain(project, dry_run=dry_run, skip=skip, only=only,
                                            opts=chain_opts, on_stage=on_stage)
                run_all.print_summary(project, results, dry_run=dry_run)
            rc = run_all.chain_rc(results)

            # 이번 런에서 모은 GSC 실적으로 다음 런의 순위 측정 대상을 정한다.
            # 부수 작업이다 — 여기서 터져도 이미 끝난 수집을 실패로 만들지 않는다.
            n = 0
            try:
                n = 0 if dry_run else activate_from_gsc(project)
                if n:
                    print(f"[{project}] 키워드 {n}개 활성화 (서치콘솔 노출 기준)")
            except Exception as e:
                print(f"[{project}] 키워드 활성화 건너뜀: {e}")

            # 메일 재료는 반드시 tenant 안에서 모은다 — 밖에서는 db.CAPTURE_HOME 이
            # 서버 기본 경로를 가리켜 남의(혹은 빈) Brain 을 읽는다.
            stats, rep, to = None, None, _email_of(conn, site)
            if not dry_run and rc == 0 and mailer.available() and to:
                stats = _mail_stats(project)
                rep = _latest_report(project)

        # 발송은 네트워크 작업이라 tenant 밖에서 한다.
        # 메일이 안 갔다고 수집을 실패로 만들지 않는다.
        if stats is not None:
            try:
                mailer.run_done(to, project, stats, report=rep)
            except Exception as e:
                print(f"[{project}] 알림 메일 건너뜀: {e}")
        return {"user_id": user_id, "project": project, "rc": rc, "ok": rc == 0,
                "activated": n}
    except Exception as e:
        # 로그에도 남긴다 — 원격 CLI 는 이 텍스트가 전부라, 여기 없으면 사용자에게는
        # 런이 조용히 끊긴 것으로 보인다.
        buf.write(f"\n[오류] 수집이 중단됐습니다: {e}\n")
        return {"user_id": user_id, "project": project, "ok": False, "error": str(e)}
    finally:
        if not dry_run:
            store.save_run_log(conn, site["id"], buf.getvalue())
            store.mark_done(conn, site["id"])     # 마지막에 끈다 — 로그를 먼저 굳힌다


def run_all_due(conn, *, idle_days: int = 30, every_hours: float = 168.0,
                dry_run: bool = False, skip: str | None = None) -> list[dict]:
    """store.due_sites() 를 직렬로 돌린다 — tenant() 가 process-global env 를
    갈아끼우므로 병렬은 안전하지 않다(스레드/프로세스 모두)."""
    results: list[dict] = []
    for site in store.due_sites(conn, idle_days=idle_days, every_hours=every_hours):
        print(f"[{site['user_id']}/{site['project']}] 시작")
        r = run_site(conn, site, dry_run=dry_run, skip=skip)
        results.append(r)
        print(f"[{site['user_id']}/{site['project']}] {'ok' if r.get('ok') else '실패'}")
    return results


def main() -> int:
    ap = argparse.ArgumentParser(description="seo-miner 수집 워커")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--all", action="store_true", help="스케줄 대상 전부")
    g.add_argument("--user", type=int, help="특정 유저 ID (--project 필수)")
    ap.add_argument("--project", help="--user 와 함께 — 사이트 프로젝트 이름")
    ap.add_argument("--idle-days", type=int, default=30)
    ap.add_argument("--every-hours", type=float,
                    default=settings.num("SEOMINER_RUN_EVERY_HOURS"),
                    help="사이트별 재측정 주기. 0 이면 자동 수집을 끈다")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--skip", help="건너뛸 단계 (쉼표 구분, 예: rank,ai)")
    ap.add_argument("--only", help="이 단계만 (쉼표 구분, 예: gsc)")
    # 형식·타입 추정의 정본은 run_all.parse_opts 다 — 여기서 다시 파싱하지 않는다.
    ap.add_argument("--opt", action="append", metavar="STAGE.KEY=VALUE",
                    help="단계별 옵션 (반복 지정). 예: --opt rank.device=mobile")
    args = ap.parse_args()

    if args.user is not None and not args.project:
        ap.error("--user 는 --project 와 함께 써야 합니다")
    if args.opt and args.all:
        # 조용히 무시하면 옵션을 줬다고 믿는 쪽이 틀린 결과를 맞는 결과로 읽는다.
        ap.error("--opt 는 --user/--project 단일 실행에만 쓸 수 있습니다")

    conn = store.connect()
    try:
        if args.all:
            results = run_all_due(conn, idle_days=args.idle_days,
                                  every_hours=args.every_hours,
                                  dry_run=args.dry_run, skip=args.skip)
        else:
            sites = store.sites(conn, args.user)
            site = next((s for s in sites if s["project"] == args.project), None)
            if site is None:
                print(f"프로젝트 '{args.project}' 없음 (user={args.user})", file=sys.stderr)
                return 1
            results = [run_site(conn, site, dry_run=args.dry_run, skip=args.skip,
                                only=args.only, opts=run_all.parse_opts(args.opt))]
    finally:
        conn.close()

    ok = sum(1 for r in results if r.get("ok"))
    fail = len(results) - ok
    print(f"완료 {ok} / 실패 {fail}")
    return 1 if fail else 0


def demo() -> None:
    """API 호출 없이 워커 동작을 검증한다."""
    import tempfile
    from cryptography.fernet import Fernet

    for k in ("CAPTURE_HOME", "GSC_TOKEN_FILE", "OPENROUTER_API_KEY",
              "SERPER_API_KEY", "DATAFORSEO_LOGIN", "DATAFORSEO_PASSWORD",
              "SEOMINER_OPENROUTER_API_KEY"):
        os.environ.pop(k, None)

    with tempfile.TemporaryDirectory() as d:
        os.environ["SEOMINER_DATA"] = d
        os.environ["SEOMINER_SECRET_KEY"] = Fernet.generate_key().decode()
        conn = store.connect()

        uid = store.upsert_user(conn, "demo@example.com")
        store.add_site(conn, uid, "demo-proj", "sc-domain:demo.com", "demo.com")
        os.environ["SEOMINER_OPENROUTER_API_KEY"] = "test-key"
        # 서버 env 에 짝이 없는 잔여 키는 엔진에 보이면 안 된다 — 보이면 그냥 쓰고 돈이 샌다.
        os.environ["SERPER_API_KEY"] = "잔여값"

        called: list[dict] = []

        def fake_chain(project, *, dry_run=False, skip=None, only=None, opts=None,
                       on_stage=None):
            print("[가짜체인] 내레이션 한 줄")     # run_log 에 잡혀야 한다
            if on_stage:
                on_stage(3, 8, "keywords")  # 화면이 읽는 진행률이 실제로 찍히는지 본다
            called.append({
                # 단계 콜백이 지금까지의 텍스트를 이미 흘려 보냈는지 — 끝나고 한 번만
                # 쓰면 몇 분짜리 런 내내 원격 화면이 비어 있다.
                "log_midrun": conn.execute("SELECT run_log FROM sites").fetchone()[0],
                "project": project,
                "skip": skip,
                "opts": opts,
                "capture_home": os.environ["CAPTURE_HOME"],
                "openrouter": os.environ.get("OPENROUTER_API_KEY"),
                "serper": os.environ.get("SERPER_API_KEY"),
                "progress": conn.execute(
                    "SELECT stage, stage_pct FROM sites").fetchone()[:2],
            })
            return []                       # run_chain 은 [(단계, StageResult)] 를 돌려준다

        run_all.run_chain = fake_chain

        results = run_all_due(conn)

        assert len(called) == 1, f"run_chain 호출 {len(called)}번"
        assert called[0]["capture_home"] == str(store.home(uid)), "CAPTURE_HOME 불일치"
        assert called[0]["openrouter"] == "test-key", "OPENROUTER_API_KEY 주입 실패"
        assert called[0]["serper"] is None, "짝 없는 잔여 키가 엔진에 노출됐다"
        assert called[0]["progress"] == ("keywords", 25), called[0]["progress"]
        assert "내레이션" in (called[0]["log_midrun"] or ""), \
            "단계 중간에 런 로그가 흘러가지 않는다 — 원격은 런이 끝날 때까지 빈 화면이다"
        assert "내레이션" in conn.execute("SELECT run_log FROM sites").fetchone()[0], \
            "런이 끝났는데 로그가 안 남았다"
        # 백링크 주기는 체인 **안**에서 정해져야 한다. 여기 밖에 또 두면 한 런에 두 번
        # 사게 된다 — 유료 호출이라 그게 곧 돈이다.
        assert (called[0]["opts"] or {}).get("backlinks", {}).get("max_age") == 30.0,             f"백링크 주기가 단계 옵션으로 안 갔다: {called[0]['opts']}"
        assert "backlinks" not in (called[0]["skip"] or ""), "주기가 켜져 있는데 단계를 건너뛴다"
        # 0 = 끔. 신선도로는 못 끄므로(0일보다 오래됐나 = 늘 참) 단계를 건너뛰어야 한다.
        assert backlinks_plan(None, 0) == ("backlinks", {}), backlinks_plan(None, 0)
        assert backlinks_plan("rank", 0) == ("rank,backlinks", {}), backlinks_plan("rank", 0)
        assert backlinks_plan("rank", 7) == ("rank", {"backlinks": {"max_age": 7}})
        assert conn.execute("SELECT stage_pct FROM sites").fetchone()[0] is None, \
            "끝났는데 진행률이 남았다"
        assert len(results) == 1 and results[0]["ok"] is True, f"결과 이상: {results}"

        assert "CAPTURE_HOME" not in os.environ, "CAPTURE_HOME 복원 실패"
        assert "OPENROUTER_API_KEY" not in os.environ, "OPENROUTER_API_KEY 복원 실패"
        assert os.environ.pop("SERPER_API_KEY") == "잔여값", "바깥 env 를 복원하지 않았다"

        # 활성 키워드 상한 — 런마다 limit 개씩 더 켜면 추적 세트가 불어나 SERP 비용이 샌다.
        with store.tenant(conn, uid):
            c = db.connect()
            c.execute("INSERT INTO projects(name, type, domain) VALUES (?,?,?)",
                      ("demo-proj", "saas", "demo.com"))
            c.commit()
            pid = db.get_project(c, "demo-proj")["id"]
            c.executemany(
                "INSERT INTO keywords(project_id, keyword, source, is_active) VALUES (?,?,?,0)",
                [(pid, f"kw{i}", "gsc") for i in range(8)])
            c.executemany(
                "INSERT INTO gsc_snapshots(project_id, snapshot_date, period_days, query, "
                "impressions) VALUES (?,'2026-01-01',28,?,?)",
                [(pid, f"kw{i}", 100 - i) for i in range(8)])
            c.commit(); c.close()
            assert activate_from_gsc("demo-proj", limit=5) == 5, "상한만큼 안 켜진다"
            assert activate_from_gsc("demo-proj", limit=5) == 0, "상한을 넘겨 또 켠다"
            c = db.connect()
            n = c.execute("SELECT COUNT(*) FROM keywords WHERE project_id=? AND is_active=1",
                          (pid,)).fetchone()[0]
            c.close()
            assert n == 5, f"활성 키워드가 {n}개 — 상한 5를 넘었다"

        # 메일 재료는 tenant 안에서 모아야 한다 — 밖에서 모으면 db.CAPTURE_HOME 이
        # 서버 기본 경로를 가리켜 빈 Brain 을 읽고 보고서 파일도 못 찾는다.
        with store.tenant(conn, uid):
            c = db.connect()
            pid2 = db.get_project(c, "demo-proj")["id"]
            c.execute("INSERT INTO opportunities(project_id, kind, target, score, status)"
                      " VALUES (?,?,?,?,'new')", (pid2, "striking_distance", "kw0", 90))
            c.commit(); c.close()
            rp = db.CAPTURE_HOME / "reports" / "demo-proj"
            rp.mkdir(parents=True, exist_ok=True)
            (rp / "2026-01-01.html").write_bytes(b"<html>report</html>")
            assert _latest_report("demo-proj") == b"<html>report</html>", "보고서를 못 읽는다"
            assert _mail_stats("demo-proj").get("opportunities") == 1, "통계를 못 읽는다"
        assert _latest_report("demo-proj") is None,             "tenant 밖인데 보고서를 찾았다 — 남의 경로를 보고 있다"

        # sites() 에는 email 컬럼이 없다 — 웹의 단일 사이트 실행이 여기서 터졌다.
        assert _email_of(conn, store.sites(conn, uid)[0]) == "demo@example.com",             "sites() 행에서 이메일을 못 얻는다"

        # 돌고 나면 사이트에 도장이 찍혀서 다음 틱에 또 돌지 않아야 한다.
        assert run_all_due(conn) == [], "방금 쟀는데 또 잰다"
        assert len(called) == 1, "중복 실행됐다"

        # --opt 로 들어온 노브(원격 CLI 의 `--device mobile`)가 체인까지 간다.
        # 단계째로 덮으면 안 된다 — 위에서 정한 백링크 주기가 사라져 한 런에 두 번 산다.
        run_site(conn, store.sites(conn, uid)[0], opts={"rank": {"device": "mobile"}})
        assert called[-1]["opts"]["rank"] == {"device": "mobile"}, called[-1]["opts"]
        assert called[-1]["opts"]["backlinks"]["max_age"] == 30.0, \
            f"요청 옵션이 백링크 주기를 덮어썼다: {called[-1]['opts']}"
        assert conn.execute("SELECT run_log FROM sites").fetchone()[0].count("내레이션") == 1, \
            "런 로그가 지난 런에 이어붙었다 — 런당 1벌이어야 한다"

        conn.execute("UPDATE users SET last_seen_at=datetime('now','-60 days')")
        conn.commit()
        before = len(called)
        results = run_all_due(conn, idle_days=30)
        assert results == [], f"휴면 유저인데 결과 {results}"
        assert len(called) == before, "run_chain 이 추가로 호출됨"

        conn.close()                            # 윈도우: 열린 파일이 있으면 임시 디렉토리 삭제 실패
        print("worker: ok")


if __name__ == "__main__":
    if len(sys.argv) == 1:
        demo()
    else:
        sys.exit(main())