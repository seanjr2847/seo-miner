"""백링크 — capture 단계로 내려간 뒤 남은 얇은 위임층.

수집·조회의 정본은 이제 `skills/capture/scripts/collect_backlinks.py` 다. 여태
이 파일이 호스팅에서만 요약·참조 도메인을 받아 왔고 로컬에는 백링크 축이 아예
없었다 — 두 배포가 같은 경로를 쓰도록 단계로 내렸다.

여기 남는 것은 **이름뿐**이다 (v1.34.0 모듈 재배치와 같은 관례): app.py 는
latest()/available() 을, worker.py 는 SCHEMA/available()/collect() 를 부른다.
그 호출부를 고치지 않으려고 re-export 로 살려 둔다.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "skills" / "capture" / "scripts"))

import collect_backlinks                         # noqa: E402
import serp_adapter                              # noqa: E402

# db.SCHEMA 가 표를 만든다. 이 이름은 worker.py 가 아직 executescript 하므로
# 하위 호환으로만 남긴다 — 빈 스크립트는 아무것도 안 한다.
SCHEMA = ""

# 대시보드 조회는 그대로 위임 — 반환 모양의 정본은 collect_backlinks.latest.
latest = collect_backlinks.latest


class BacklinksError(RuntimeError):
    pass


def available() -> bool:
    return serp_adapter.has_dataforseo()


def collect(project: str, *, limit: int = 200) -> dict:
    """단계를 한 번 돌리고 worker.py 가 찍는 모양으로 접어 돌려준다.

    max_age=0 — "언제 다시 살지"는 호스팅에서 worker._backlinks_due 가 정한다.
    단계 자체의 신선도 기본값(7일)을 여기에 겹치면 주기가 두 벌이 된다.
    """
    r = collect_backlinks.collect(project, backlink_limit=limit, max_age=0)
    if not r.ok:
        raise BacklinksError(r.reason or "백링크 수집에 실패했습니다. 잠시 후 다시 시도해 주세요.")
    got = latest(project, top=max(limit, 1))
    return {"summary": got["summary"] or {}, "domains": len(got["domains"]),
            "cost": round(r.cost, 4)}


def demo() -> None:
    """공개 이름이 그대로 살아 있는지 — 호출부(app.py·worker.py)가 쓰는 것만 본다."""
    import inspect
    import os
    import tempfile

    assert SCHEMA == "" and issubclass(BacklinksError, RuntimeError)
    assert callable(available) and callable(latest) and callable(collect)
    assert latest is collect_backlinks.latest
    assert list(inspect.signature(collect).parameters) == ["project", "limit"]
    assert list(inspect.signature(latest).parameters) == ["project", "top"]

    os.environ["CAPTURE_HOME"] = str(tempfile.mkdtemp(prefix="seo-miner-server-bl-"))
    os.environ["DATAFORSEO_LOGIN"] = "login"
    os.environ["DATAFORSEO_PASSWORD"] = "pw"
    import db                                     # noqa: E402 (경로를 먼저 세운다)
    conn = db.connect()
    conn.execute("INSERT INTO projects(name, domain) VALUES('p','bt.com')")
    pid = db.get_project(conn, "p")["id"]
    conn.executemany("INSERT INTO competitors(project_id, domain, source) VALUES(?,?,'m')",
                     [(pid, "r1.com"), (pid, "r2.com")])
    conn.commit()
    conn.close()

    orig = collect_backlinks.collect
    try:
        collect_backlinks.collect = lambda project, **kw: orig(
            project, post=collect_backlinks._fake_post([]), **kw)
        r = collect("p", limit=3)
        assert r["summary"]["backlinks"] == 340, r
        assert r["domains"] == 2 and r["cost"] > 0, r
    finally:
        collect_backlinks.collect = orig
    print("backlinks: ok")


if __name__ == "__main__":
    demo()
