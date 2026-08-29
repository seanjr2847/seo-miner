"""CSV 내보내기 + 사이트 한 줄 요약.

Brain(brain.db) 의 마케팅 데이터를 마케터가 엑셀로 가져갈 수 있게 잘라낸다.
여러 사이트를 한 화면에 나란히 놓을 때 필요한 한 줄 요약도 같이.
"""
from __future__ import annotations

import csv
import re
import io
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "skills" / "capture" / "scripts"))

import db  # noqa: E402
import scoring  # noqa: E402


_ALLOWED = ("keywords", "opportunities", "queries", "index")


def _csv_bytes(header: list[str], rows: list[tuple]) -> bytes:
    buf = io.StringIO()
    csv.writer(buf).writerows([header, *rows])
    return buf.getvalue().encode("utf-8-sig")


def _today() -> str:
    return date.today().isoformat()


def csv_bytes(project: str, table: str) -> tuple[bytes, str]:
    if table not in _ALLOWED:
        raise ValueError(f"unknown table {table!r} (expected one of {_ALLOWED})")
    conn = db.connect()
    try:
        pid = db.get_project(conn, project)["id"]
        if table == "keywords":
            data, name = _csv_keywords(conn, pid)
        elif table == "opportunities":
            data, name = _csv_opportunities(conn, pid)
        elif table == "queries":
            data, name = _csv_queries(conn, pid)
        else:
            data, name = _csv_index(conn, pid)
        # 사이트가 여럿이면 내려받은 파일이 서로 구분돼야 한다.
        safe = re.sub(r"[^A-Za-z0-9_-]", "", project) or "site"
        return data, f"{safe}-{name}"
    finally:
        conn.close()


def _csv_keywords(conn, pid: int) -> tuple[bytes, str]:
    latest = conn.execute(
        "SELECT MAX(snapshot_date) FROM gsc_snapshots WHERE project_id=?", (pid,)
    ).fetchone()[0]
    rows = conn.execute(
        """SELECT k.is_active, k.keyword, COALESCE(k.source,''), COALESCE(k.cluster,''),
                  COALESCE(SUM(CASE WHEN g.snapshot_date=? THEN g.impressions END), 0),
                  COALESCE(SUM(CASE WHEN g.snapshot_date=? THEN g.clicks END), 0)
             FROM keywords k
             LEFT JOIN gsc_snapshots g ON g.query=k.keyword AND g.project_id=k.project_id
            WHERE k.project_id=?
            GROUP BY k.id
            ORDER BY 5 DESC, k.keyword""",
        (latest, latest, pid)).fetchall()
    body = [("예" if r[0] else "아니오", r[1] or "", r[2], r[3],
             int(r[4] or 0), int(r[5] or 0)) for r in rows]
    return _csv_bytes(["추적", "검색어", "출처", "클러스터", "노출", "클릭"], body), \
           f"keywords-{latest or _today()}.csv"


def _csv_opportunities(conn, pid: int) -> tuple[bytes, str]:
    rows = conn.execute(
        """SELECT kind, target, ROUND(score, 1), status,
                  substr(created_at, 1, 10), reasoning
             FROM opportunities WHERE project_id=?
            ORDER BY score DESC, id DESC""", (pid,)).fetchall()
    latest = conn.execute(
        "SELECT MAX(substr(created_at,1,10)) FROM opportunities WHERE project_id=?",
        (pid,)).fetchone()[0]
    body = [(r[0] or "", r[1] or "", r[2], r[3] or "", r[4] or "", r[5] or "")
            for r in rows]
    return _csv_bytes(
        ["종류", "대상", "점수", "상태", "만든 날짜", "근거"], body
    ), f"opportunities-{latest or _today()}.csv"


def _csv_queries(conn, pid: int) -> tuple[bytes, str]:
    latest = conn.execute(
        "SELECT MAX(snapshot_date) FROM gsc_snapshots WHERE project_id=?", (pid,)
    ).fetchone()[0]
    if not latest:
        return _csv_bytes(["검색어", "노출", "클릭", "CTR", "평균 순위"], []), \
               f"queries-{_today()}.csv"
    rows = conn.execute(
        """SELECT query,
                  SUM(impressions), SUM(clicks),
                  ROUND(SUM(clicks)*1.0/NULLIF(SUM(impressions),0), 4),
                  ROUND(AVG(position), 1)
             FROM gsc_snapshots
            WHERE project_id=? AND snapshot_date=?
            GROUP BY query
            ORDER BY 2 DESC""", (pid, latest)).fetchall()
    body = [(r[0] or "", int(r[1] or 0), int(r[2] or 0),
             r[3] if r[3] is not None else "",
             r[4] if r[4] is not None else "") for r in rows]
    return _csv_bytes(["검색어", "노출", "클릭", "CTR", "평균 순위"], body), \
           f"queries-{latest}.csv"


def _csv_index(conn, pid: int) -> tuple[bytes, str]:
    latest = conn.execute(
        "SELECT MAX(checked_date) FROM gsc_index_status WHERE project_id=?", (pid,)
    ).fetchone()[0]
    if not latest:
        return _csv_bytes(["URL", "판정", "커버리지 상태"], []), \
               f"index-{_today()}.csv"
    rows = conn.execute(
        """SELECT url, verdict, coverage_state
             FROM gsc_index_status
            WHERE project_id=? AND checked_date=?
            ORDER BY url""", (pid, latest)).fetchall()
    body = [(r[0] or "", r[1] or "", r[2] or "") for r in rows]
    return _csv_bytes(["URL", "판정", "커버리지 상태"], body), \
           f"index-{latest}.csv"


def summary(project: str) -> dict:
    conn = db.connect()
    try:
        p = db.get_project(conn, project)
        pid = p["id"]
        latest = conn.execute(
            """SELECT snapshot_date, COALESCE(SUM(clicks),0), COALESCE(SUM(impressions),0)
                 FROM gsc_snapshots WHERE project_id=?
                GROUP BY snapshot_date
                ORDER BY snapshot_date DESC LIMIT 1""", (pid,)).fetchone()
        if latest:
            last_run, clicks, impressions = latest[0], int(latest[1]), int(latest[2])
        else:
            last_run, clicks, impressions = None, None, None
        prev = conn.execute(
            """SELECT COALESCE(SUM(clicks),0)
                 FROM gsc_snapshots WHERE project_id=?
                GROUP BY snapshot_date
                ORDER BY snapshot_date DESC LIMIT 1 OFFSET 1""", (pid,)).fetchone()
        delta_clicks = (clicks - int(prev[0])) if (last_run and prev) else None
        keywords_active = db.count_active_keywords(conn, pid)
        opp_count = int(conn.execute(
            "SELECT COUNT(*) FROM opportunities WHERE project_id=? AND status='new'",
            (pid,)).fetchone()[0])
        top = conn.execute(
            """SELECT target FROM opportunities
                WHERE project_id=? AND status='new'
                ORDER BY score DESC LIMIT 1""", (pid,)).fetchone()
        return {
            "project": p["name"],
            "domain": p["domain"],
            "clicks": clicks,
            "impressions": impressions,
            "keywords_active": keywords_active,
            "opportunities": opp_count,
            "top_opportunity": top[0] if top else None,
            "last_run": last_run,
            "delta_clicks": delta_clicks,
        }
    finally:
        conn.close()


def _rate(clicks: int, imps: int) -> float | None:
    return round(clicks / imps, 4) if imps else None


def _pos(weighted: float | None, imps: int) -> float | None:
    """노출 가중 평균 게재순위. 단순 평균을 쓰면 노출 1회짜리 꼬리가 값을 끌어내려
    화면의 숫자와 서치콘솔의 숫자가 어긋난다 — 서치콘솔도 노출 기준으로 낸다."""
    return round(weighted / imps, 1) if (weighted is not None and imps) else None


_DIM_SQL = """SELECT {col}, COALESCE(SUM(clicks),0), COALESCE(SUM(impressions),0),
                     SUM(position*impressions)
                FROM gsc_snapshots WHERE project_id=? AND snapshot_date=? AND {col} IS NOT NULL
               GROUP BY {col} ORDER BY 2 DESC, 3 DESC LIMIT ?"""


def perf(project: str, top: int = 25, days: int = 90) -> dict:
    """서치콘솔 성과 — 4대 지표(클릭·노출·CTR·게재순위)와 검색어·페이지·기기 분해.

    화면이 쓰던 것은 클릭·노출 둘뿐이었다. CTR 과 게재순위는 gsc_snapshots 에 이미
    들어 있는데 아무도 읽지 않았다 — 서치콘솔을 보던 사람에게는 지표 절반이 빈 화면이다.
    """
    conn = db.connect()
    try:
        p = db.get_project(conn, project)
        pid = p["id"]
        # 짝짓기는 scoring.snapshot_pair() 하나로 — 날짜만 보고 직전을 고르면
        # period_days 가 다른 스냅샷끼리 빼져서 Δ가 거짓이 된다 (scoring.md 4-3b).
        cur, prev_date, period, period_mismatch = scoring.snapshot_pair(conn, pid)
        if not cur:
            return {"snapshot": None, "totals": None, "prev": None,
                    "period_mismatch": False,
                    "daily": [], "queries": [], "pages": [], "devices": []}

        def totals(snap: str) -> dict:
            c, i, pw, q = conn.execute(
                """SELECT COALESCE(SUM(clicks),0), COALESCE(SUM(impressions),0),
                          SUM(position*impressions), COUNT(DISTINCT query)
                     FROM gsc_snapshots WHERE project_id=? AND snapshot_date=?""",
                (pid, snap)).fetchone()
            c, i = int(c), int(i)
            return {"date": snap, "clicks": c, "impressions": i,
                    "ctr": _rate(c, i), "position": _pos(pw, i), "queries": int(q)}

        def dim(col: str) -> list[dict]:
            return [{"key": k, "clicks": int(c), "impressions": int(i),
                     "ctr": _rate(int(c), int(i)), "position": _pos(pw, int(i))}
                    for k, c, i, pw in conn.execute(
                        _DIM_SQL.format(col=col), (pid, cur, top))]

        # 기기 분해는 별도 테이블이고 수집 주기가 본체와 어긋날 수 있다 — 자기 최신을 쓴다.
        bsnap = conn.execute(
            "SELECT MAX(snapshot_date) FROM gsc_breakdown WHERE project_id=? AND dim='device'",
            (pid,)).fetchone()[0]
        devices = [{"key": k, "clicks": int(c), "impressions": int(i),
                    "ctr": _rate(int(c), int(i)), "position": _pos(pw, int(i))}
                   for k, c, i, pw in conn.execute(
                       """SELECT dim_value, COALESCE(SUM(clicks),0), COALESCE(SUM(impressions),0),
                                 SUM(position*impressions)
                            FROM gsc_breakdown
                           WHERE project_id=? AND dim='device' AND snapshot_date=?
                           GROUP BY dim_value ORDER BY 3 DESC""",
                       (pid, bsnap))] if bsnap else []

        daily = [{"d": d, "clicks": int(c or 0), "impressions": int(i or 0),
                  "ctr": round(ct, 4) if ct is not None else None,
                  "position": round(po, 1) if po is not None else None}
                 for d, c, i, ct, po in conn.execute(
                     "SELECT date, clicks, impressions, ctr, position FROM gsc_daily"
                     " WHERE project_id=? ORDER BY date DESC LIMIT ?", (pid, days))][::-1]

        return {"snapshot": cur, "totals": totals(cur),
                "prev": totals(prev_date) if prev_date else None,
                "period_mismatch": period_mismatch,
                "daily": daily, "queries": dim("query"), "pages": dim("page"),
                "devices": devices}
    finally:
        conn.close()


def demo() -> None:
    import os
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        os.environ["CAPTURE_HOME"] = d
        conn = db.connect()
        conn.execute(
            "INSERT INTO projects(name, type, domain) VALUES (?,?,?)",
            ("demo", "saas", "demo.example.com"))
        pid = conn.execute(
            "SELECT id FROM projects WHERE name=?", ("demo",)).fetchone()[0]
        conn.executemany(
            """INSERT INTO keywords(project_id, keyword, source, cluster, is_active)
               VALUES (?,?,?,?,?)""",
            [(pid, "a,b", "seed", "c1", 1),
             (pid, "hello", "seed", "c2", 0),
             (pid, "world", "autocomplete", "c3", 0)])
        conn.executemany(
            """INSERT INTO gsc_snapshots(project_id, snapshot_date, period_days,
                 query, page, clicks, impressions, ctr, position)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            [(pid, "2026-01-01", 28, "a,b", "/x", 5, 100, 0.05, 4.0),
             (pid, "2026-01-01", 28, "hello", "/y", 3, 50, 0.06, 5.5),
             (pid, "2026-01-01", 28, "world", "/z", 1, 30, 0.03, 7.0),
             (pid, "2026-01-29", 28, "a,b", "/x", 8, 150, 0.0533, 3.5),
             (pid, "2026-01-29", 28, "hello", "/y", 4, 80, 0.05, 5.0)])
        conn.executemany(
            """INSERT INTO opportunities(project_id, kind, target, score, reasoning, status, created_at)
               VALUES (?,?,?,?,?,?,?)""",
            [(pid, "striking_distance", "a,b", 92.0, "4위 — 1만 노출", "new", "2026-01-30T00:00:00Z"),
             (pid, "ctr_gap", "hello", 70.5, "CTR이 낮음", "new", "2026-01-29T00:00:00Z"),
             (pid, "rank_decay", "world", 80.0, "순위 하락", "acked", "2026-01-15T00:00:00Z")])
        conn.executemany(
            """INSERT INTO gsc_index_status(project_id, checked_date, url, verdict, coverage_state)
               VALUES (?,?,?,?,?)""",
            [(pid, "2026-02-01", "/page-a", "PASS", "Indexed"),
             (pid, "2026-02-01", "/page-b", "FAIL", "Excluded")])
        conn.commit()

        for t in _ALLOWED:
            data, fname = csv_bytes("demo", t)
            first = data.split(b"\n", 1)[0].decode("utf-8-sig")
            assert any(ord(c) >= 0xAC00 for c in first), f"{t} 헤더 한국어 아님: {first}"
            assert data.startswith(b"\xef\xbb\xbf"), f"{t} BOM 누락"
            print(f"  {t}: {fname}")

        data, _ = csv_bytes("demo", "keywords")
        rows = list(csv.reader(io.StringIO(data.decode("utf-8-sig"))))
        assert len(rows) == 4, f"헤더+3행 기대, 실제 {len(rows)}: {rows}"
        assert "a,b" in [r[1] for r in rows[1:]], "a,b 가 한 셀로"

        try:
            csv_bytes("demo", "nope")
            raise AssertionError("ValueError 기대")
        except ValueError:
            pass

        conn2 = db.connect()
        conn2.execute(
            "INSERT INTO projects(name, type, domain) VALUES (?,?,?)",
            ("empty", "saas", "e.example.com"))
        conn2.commit(); conn2.close()
        for t in _ALLOWED:
            data, _ = csv_bytes("empty", t)
            rows = list(csv.reader(io.StringIO(data.decode("utf-8-sig"))))
            assert len(rows) == 1, f"{t} 빈: 헤더만 기대, 실제 {len(rows)}: {rows}"

        s = summary("demo")
        assert s["clicks"] == 12, s
        assert s["impressions"] == 230, s
        assert s["keywords_active"] == 1, s
        assert s["opportunities"] == 2, s
        assert s["top_opportunity"] == "a,b", s
        assert s["last_run"] == "2026-01-29", s
        assert s["delta_clicks"] == 3, s

        conn2 = db.connect()
        pid2 = conn2.execute(
            "SELECT id FROM projects WHERE name=?", ("empty",)).fetchone()[0]
        conn2.execute(
            """INSERT INTO gsc_snapshots(project_id, snapshot_date, period_days,
                 query, page, clicks, impressions, ctr, position)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (pid2, "2026-02-01", 28, "q", "/p", 2, 10, 0.2, 5.0))
        conn2.commit(); conn2.close()
        assert summary("empty")["delta_clicks"] is None

        try:
            summary("nope")
            raise AssertionError("ProjectNotFound 기대")
        except db.ProjectNotFound:
            pass

        # 성과 집계 — 게재순위는 노출 가중이라 단순 평균과 다른 값이 나와야 한다.
        conn3 = db.connect()
        pid3 = db.get_project(conn3, "empty")["id"]
        conn3.execute(
            """INSERT INTO gsc_snapshots(project_id, snapshot_date, period_days,
                 query, page, clicks, impressions, ctr, position)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (pid3, "2026-02-01", 28, "q2", "/p2", 0, 90, 0.0, 30.0))
        conn3.commit(); conn3.close()
        pf = perf("empty")
        t = pf["totals"]
        assert t["clicks"] == 2 and t["impressions"] == 100, t
        assert t["ctr"] == 0.02, t                    # 2/100
        # 단순 평균이면 17.5 — 노출 가중이면 (5*10 + 30*90)/100 = 27.5
        assert t["position"] == 27.5, t["position"]
        assert [q["key"] for q in pf["queries"]] == ["q", "q2"], pf["queries"]
        assert pf["pages"][0]["key"] == "/p", pf["pages"]
        assert pf["devices"] == [] and pf["daily"] == [], "없는 축이 빈 목록이 아니다"
        assert pf["prev"] is None and pf["period_mismatch"] is False,             "스냅샷이 하나뿐인데 mismatch 로 잘못 판정"

        # period_mismatch — 일부러 기간이 다른 스냅샷 두 개를 넣는다. 고치기 전
        # 코드(날짜만 보고 직전을 고름)는 이걸 조용히 삼켜 prev 를 내준다.
        conn4 = db.connect()
        conn4.execute(
            "INSERT INTO projects(name, type, domain) VALUES (?,?,?)",
            ("mismatch", "saas", "m.example.com"))
        pid4 = conn4.execute(
            "SELECT id FROM projects WHERE name=?", ("mismatch",)).fetchone()[0]
        conn4.executemany(
            """INSERT INTO gsc_snapshots(project_id, snapshot_date, period_days,
                 query, page, clicks, impressions, ctr, position)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            [(pid4, "2026-02-01", 90, "q", "/p", 3, 60, 0.05, 6.0),
             (pid4, "2026-01-01", 28, "q", "/p", 1, 20, 0.05, 8.0)])
        conn4.commit(); conn4.close()
        pf2 = perf("mismatch")
        assert pf2["snapshot"] == "2026-02-01", pf2["snapshot"]
        assert pf2["prev"] is None, "기간이 다른 스냅샷을 짝으로 골랐다"
        assert pf2["period_mismatch"] is True, "period_mismatch 를 안 알렸다"

        conn.close()
    print("exports: ok")


if __name__ == "__main__":
    demo()
