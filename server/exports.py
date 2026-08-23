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
        keywords_active = int(conn.execute(
            "SELECT COUNT(*) FROM keywords WHERE project_id=? AND is_active=1",
            (pid,)).fetchone()[0])
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

        conn.close()
    print("exports: ok")


if __name__ == "__main__":
    demo()
