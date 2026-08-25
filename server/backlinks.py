"""백링크 수집 — DataForSEO Backlinks API.

백링크는 코드가 아니라 인프라다(자체 크롤러로 링크 그래프를 만드는 일). 사서 쓴다.
SERP 에 이미 쓰는 DataForSEO 자격증명을 그대로 재사용한다 — 새 계정을 두지 않는다.

가격(2026-08 기준): 요청당 $0.024 + 행당 $0.000036. 1,000행이 $0.05 라서 자주 재도
싸지만, 백링크는 하루 단위로 움직이지 않는다 — 기본 주기를 길게 둔다.

Brain(capture 의 SQLite)에 쓴다. 테이블 정의는 여기 없다 — Brain 스키마의 소유자는
db.py 하나이고(backlink_summary·referring_domains 는 db.SCHEMA 에 있다), 이 파일은
db.connect() 가 보장해 준 테이블에 쓰기만 한다.
"""
from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "skills" / "capture" / "scripts"))

import db                                       # noqa: E402
import serp_adapter                             # noqa: E402

API = "https://api.dataforseo.com/v3/backlinks"
TIMEOUT = 120

# db.SCHEMA 가 테이블을 만든다. 이 이름은 worker.py 가 아직 executescript 하므로
# 하위 호환으로만 남긴다 — 빈 스크립트는 아무것도 안 한다.
SCHEMA = ""


class BacklinksError(RuntimeError):
    pass


def available() -> bool:
    return serp_adapter.has_dataforseo()


def _post(path: str, body: list) -> tuple[list, float]:
    """반환: (items, 실청구액). 응답의 cost 를 그대로 읽는다 — 추정하지 않는다."""
    if not available():
        raise BacklinksError("백링크 데이터 수집이 아직 연결되지 않았습니다 (DATAFORSEO_LOGIN/PASSWORD 미설정)")
    r = requests.post(f"{API}{path}", timeout=TIMEOUT, json=body,
                      auth=(os.environ["DATAFORSEO_LOGIN"],
                            os.environ["DATAFORSEO_PASSWORD"]))
    if r.status_code >= 400:
        raise BacklinksError(f"백링크 수집에 실패했습니다. 잠시 후 다시 시도해 주세요 ({r.status_code}: {r.text[:200]})")
    d = r.json()
    task = (d.get("tasks") or [{}])[0]
    if task.get("status_code") != 20000:
        raise BacklinksError(f"백링크 수집에 실패했습니다. 잠시 후 다시 시도해 주세요 ({task.get('status_message')})")
    result = task.get("result") or []
    return result, float(d.get("cost") or 0)


def collect(project: str, *, limit: int = 200) -> dict:
    """요약 + 참조 도메인 상위 N. 반환: {summary, domains, cost}."""
    conn = db.connect()
    try:
        p = db.get_project(conn, project)
        target = p["domain"]
        today = date.today().isoformat()
        cost = 0.0

        res, c = _post("/summary/live", [{"target": target, "internal_list_limit": 1}])
        cost += c
        s = (res[0] if res else {}) or {}
        info = s.get("info") or {}
        conn.execute(
            "INSERT INTO backlink_summary(project_id, checked_date, rank, backlinks,"
            " referring_domains, referring_main_domains, broken_backlinks, dofollow, nofollow)"
            " VALUES (?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(project_id, checked_date) DO UPDATE SET"
            " rank=excluded.rank, backlinks=excluded.backlinks,"
            " referring_domains=excluded.referring_domains,"
            " referring_main_domains=excluded.referring_main_domains,"
            " broken_backlinks=excluded.broken_backlinks,"
            " dofollow=excluded.dofollow, nofollow=excluded.nofollow",
            (p["id"], today, s.get("rank"), s.get("backlinks"),
             s.get("referring_domains"), s.get("referring_main_domains"),
             s.get("broken_backlinks"),
             (info.get("referring_links_attributes") or {}).get("dofollow"),
             (info.get("referring_links_attributes") or {}).get("nofollow")))

        res, c = _post("/referring_domains/live",
                       [{"target": target, "limit": limit,
                         "order_by": ["rank,desc"]}])
        cost += c
        items = ((res[0] if res else {}) or {}).get("items") or []
        conn.executemany(
            "INSERT INTO referring_domains(project_id, checked_date, domain, rank,"
            " backlinks, dofollow, first_seen, lost_date) VALUES (?,?,?,?,?,?,?,?)"
            " ON CONFLICT(project_id, checked_date, domain) DO UPDATE SET"
            " rank=excluded.rank, backlinks=excluded.backlinks, dofollow=excluded.dofollow",
            [(p["id"], today, it.get("domain"), it.get("rank"), it.get("backlinks"),
              it.get("dofollow"), it.get("first_seen"), it.get("lost_date"))
             for it in items if it.get("domain")])
        conn.commit()
        return {"summary": s, "domains": len(items), "cost": round(cost, 4)}
    finally:
        conn.close()


def latest(project: str, top: int = 25) -> dict:
    """대시보드용 조회. 수집한 적이 없으면 빈 값을 돌려준다(없는 걸 지어내지 않는다)."""
    conn = db.connect()
    try:
        p = db.get_project(conn, project)
        s = conn.execute(
            "SELECT * FROM backlink_summary WHERE project_id=? ORDER BY checked_date DESC"
            " LIMIT 1", (p["id"],)).fetchone()
        if not s:
            return {"summary": None, "domains": [], "history": []}
        doms = conn.execute(
            "SELECT domain, rank, backlinks, dofollow, first_seen FROM referring_domains"
            " WHERE project_id=? AND checked_date=? ORDER BY rank DESC LIMIT ?",
            (p["id"], s["checked_date"], top)).fetchall()
        hist = conn.execute(
            "SELECT checked_date, backlinks, referring_domains FROM backlink_summary"
            " WHERE project_id=? ORDER BY checked_date DESC LIMIT 12", (p["id"],)).fetchall()
        return {"summary": dict(s), "domains": [dict(d) for d in doms],
                "history": [dict(h) for h in hist][::-1]}
    finally:
        conn.close()


def demo() -> None:
    import tempfile
    g = globals()
    orig = g["_post"]
    with tempfile.TemporaryDirectory() as d:
        os.environ["CAPTURE_HOME"] = d
        conn = db.connect()
        conn.execute("INSERT INTO projects(name, type, domain) VALUES (?,?,?)",
                     ("p", "saas", "example.com"))
        conn.commit(); conn.close()

        calls = []

        def fake(path, body):
            calls.append((path, body[0]))
            if path.endswith("/summary/live"):
                return [{"rank": 120, "backlinks": 340, "referring_domains": 55,
                         "referring_main_domains": 51, "broken_backlinks": 3,
                         "info": {"referring_links_attributes":
                                  {"dofollow": 300, "nofollow": 40}}}], 0.024
            return [{"items": [{"domain": f"d{i}.com", "rank": 90 - i, "backlinks": 5,
                                "dofollow": 4, "first_seen": "2026-01-01"}
                               for i in range(3)]}], 0.03

        g["_post"] = fake
        r = collect("p", limit=3)
        assert r["summary"]["backlinks"] == 340, r
        assert r["domains"] == 3, r
        assert abs(r["cost"] - 0.054) < 1e-6, r["cost"]
        assert calls[0][1]["target"] == "example.com", calls[0]

        got = latest("p")
        assert got["summary"]["referring_domains"] == 55, got["summary"]
        assert len(got["domains"]) == 3 and got["domains"][0]["domain"] == "d0.com", got
        assert got["history"] and got["history"][-1]["backlinks"] == 340

        collect("p", limit=3)                 # 같은 날 두 번 — 행이 불어나면 안 된다
        again = latest("p")
        assert len(again["domains"]) == 3, f"중복 적재: {len(again['domains'])}"

        os.environ.pop("CAPTURE_HOME")
        g["_post"] = orig
        print("backlinks: ok")


if __name__ == "__main__":
    demo()
