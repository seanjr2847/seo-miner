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
import os
import sys
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(ROOT / "skills" / "capture" / "scripts"))

import run_all                                # noqa: E402
import store                                  # noqa: E402


PAID_KEYS = (
    "SERPER_API_KEY",
    "DATAFORSEO_LOGIN",
    "DATAFORSEO_PASSWORD",
    "OPENROUTER_API_KEY",
)


@contextmanager
def _paid_keys():
    """서버 env(SEOMINER_<NAME>) 를 테넌트 안에서 <NAME> 으로 노출, 끝나면 복원.

    SEOMINER_<NAME> 가 없으면 그 키를 pop — 외부 env 에 남아있던 잔여값을 엔진이
    그대로 쓰면 비용 누수가 된다(엔진은 키가 보이면 그냥 쓴다).
    """
    saved = {k: os.environ.get(k) for k in PAID_KEYS}
    for k in PAID_KEYS:
        v = os.environ.get(f"SEOMINER_{k}")
        if v:
            os.environ[k] = v
        else:
            os.environ.pop(k, None)
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def run_site(conn, site, *, dry_run: bool = False, skip: str | None = None) -> dict:
    """사이트 1건 처리. tenant() 안에서 유료 키를 주입하고 run_chain 을 호출."""
    user_id = site["user_id"]
    project = site["project"]
    try:
        with store.tenant(conn, user_id), _paid_keys():
            rc = run_all.run_chain(project, dry_run=dry_run, skip=skip)
        return {"user_id": user_id, "project": project, "rc": rc, "ok": rc == 0}
    except Exception as e:
        return {"user_id": user_id, "project": project, "ok": False, "error": str(e)}


def run_all_due(conn, *, idle_days: int = 30, dry_run: bool = False,
                skip: str | None = None) -> list[dict]:
    """store.due_sites() 를 직렬로 돌린다 — tenant() 가 process-global env 를
    갈아끼우므로 병렬은 안전하지 않다(스레드/프로세스 모두)."""
    results: list[dict] = []
    for site in store.due_sites(conn, idle_days=idle_days):
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
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--skip", help="건너뛸 단계 (쉼표 구분, 예: rank,ai)")
    args = ap.parse_args()

    if args.user is not None and not args.project:
        ap.error("--user 는 --project 와 함께 써야 합니다")

    conn = store.connect()
    try:
        if args.all:
            results = run_all_due(conn, idle_days=args.idle_days,
                                  dry_run=args.dry_run, skip=args.skip)
        else:
            sites = store.sites(conn, args.user)
            site = next((s for s in sites if s["project"] == args.project), None)
            if site is None:
                print(f"프로젝트 '{args.project}' 없음 (user={args.user})", file=sys.stderr)
                return 1
            results = [run_site(conn, site, dry_run=args.dry_run, skip=args.skip)]
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

        called: list[dict] = []

        def fake_chain(project, *, dry_run=False, skip=None, only=None):
            called.append({
                "project": project,
                "capture_home": os.environ["CAPTURE_HOME"],
                "openrouter": os.environ.get("OPENROUTER_API_KEY"),
            })
            return 0

        run_all.run_chain = fake_chain

        results = run_all_due(conn)

        assert len(called) == 1, f"run_chain 호출 {len(called)}번"
        assert called[0]["capture_home"] == str(store.home(uid)), "CAPTURE_HOME 불일치"
        assert called[0]["openrouter"] == "test-key", "OPENROUTER_API_KEY 주입 실패"
        assert len(results) == 1 and results[0]["ok"] is True, f"결과 이상: {results}"

        assert "CAPTURE_HOME" not in os.environ, "CAPTURE_HOME 복원 실패"
        assert "OPENROUTER_API_KEY" not in os.environ, "OPENROUTER_API_KEY 복원 실패"

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