#!/usr/bin/env python3
"""자체점검 — `python test_capture.py` (임시 폴더에서만 돈다, 진짜 Brain은 안 건드림).

가장 조용히 틀리는 것들만 본다:
  · CSV 숫자 표기 흡수(1,234 / 3,5 / 3.5%) — 로케일 따라 뜻이 뒤집힌다
  · zip에서 '검색어' 표 고르기 — 이름을 못 알아보면 페이지 CSV를 넣어버린다
  · db.py sql 이 진짜 읽기 전용인지 — WITH ... DELETE 는 문자열 검사를 통과한다
  · 기간 다른 GSC 스냅샷을 비교하지 않는지 — 28일치와 90일치를 빼면 Δ가 거짓이다
  · 같은 기회를 두 번 적재해도 목록이 안 불어나는지
  · 판정 규칙(scoring) — 임계값·남의 브랜드 제외·기회 정렬이 한 곳에서 나오는지
  · 키워드 후보가 locale 없이 쌓이지 않는지 — 쓰기 경로가 하나여야 막힌다
  · 수집이 예외로 끊겨도 실행 기록이 닫히는지
"""
import os
import sys
import tempfile
import zipfile
from pathlib import Path

HOME = Path(tempfile.mkdtemp(prefix="seo-miner-test-"))
os.environ["CAPTURE_HOME"] = str(HOME)          # import 전에 걸어야 db가 여기를 본다
sys.path.insert(0, str(Path(__file__).parent))

import collector          # noqa: E402
import dashboard          # noqa: E402
import db                 # noqa: E402
import import_gsc_csv as csvimp  # noqa: E402
import scoring            # noqa: E402

CSV = ("검색어,클릭수,노출수,CTR,게재순위\n"
       "20도 옷차림,1234,5678,3.5%,8.2\n"
       "빈 줄 아님,0,3,0%,15\n")
PAGES_CSV = "페이지,클릭수,노출수,CTR,게재순위\nhttps://e.com/a,1,2,1%,3\n"


def test_parse_num():
    assert csvimp.parse_num("1,234") == 1234        # 천단위 콤마
    assert csvimp.parse_num("3,5") == 3.5           # 소수점 콤마 (독일·프랑스 표기)
    assert csvimp.parse_num("3.5%") == 0.035        # 퍼센트는 비율로
    assert csvimp.parse_num("") == 0.0


def test_read_rows():
    rows = csvimp.read_rows(CSV.encode("utf-8"))
    assert len(rows) == 2, rows                     # 헤더는 빠진다
    assert rows[0] == ("20도 옷차림", 1234, 5678, 0.035, 8.2), rows[0]


def test_pick_csv_prefers_query_table():
    z = HOME / "export.zip"
    with zipfile.ZipFile(z, "w") as f:
        f.writestr("페이지.csv", PAGES_CSV)
        f.writestr("쿼리.csv", CSV)
    name, blob = csvimp.pick_csv(z)
    assert "쿼리" in name, name
    assert csvimp.read_rows(blob)[0][0] == "20도 옷차림"


def test_pick_csv_unknown_names_skips_urls():
    """파일 이름을 못 알아보는 언어일 때도 URL 목록(페이지 표)은 고르면 안 된다."""
    z = HOME / "export2.zip"
    with zipfile.ZipFile(z, "w") as f:
        f.writestr("aaa.csv", PAGES_CSV + "x" * 5000)   # 일부러 더 크게
        f.writestr("bbb.csv", CSV)
    _, blob = csvimp.pick_csv(z)
    assert csvimp.read_rows(blob)[0][0] == "20도 옷차림"


def _project(conn, name="t"):
    conn.execute("INSERT OR IGNORE INTO projects(name,domain) VALUES(?, 'e.com')", (name,))
    conn.commit()
    return conn.execute("SELECT * FROM projects WHERE name=?", (name,)).fetchone()


def _snap(conn, pid, date_, days, query, pos, clicks):
    conn.execute("""INSERT INTO gsc_snapshots(project_id,snapshot_date,period_days,
                      query,page,clicks,impressions,ctr,position)
                    VALUES(?,?,?,?,NULL,?,100,0.1,?)""",
                 (pid, date_, days, query, clicks, pos))
    conn.commit()


def test_sql_is_really_read_only():
    conn = db.connect()
    p = _project(conn, "ro")
    conn.execute("INSERT INTO opportunities(project_id,kind,target) VALUES(?,'k','t')",
                 (p["id"],))
    conn.commit()
    conn.close()
    # 문자열 검사는 통과하는 문장 — 커넥션이 읽기 전용이어야 막힌다.
    q = "WITH x AS (SELECT 1) DELETE FROM opportunities"
    assert q.strip().lower().startswith(("select", "with"))   # 옛 가드는 뚫린다
    try:
        db.run_sql(q)
    except SystemExit as e:      # run_sql이 sqlite 예외를 잡아 안내 문구로 종료한다
        assert "조회 전용" in str(e), e
    except Exception as e:       # 안내 문구를 못 붙인 경우라도 DB는 막아야 한다
        assert "readonly" in str(e).lower(), e
    else:
        raise AssertionError("읽기 전용 커넥션이 쓰기를 막지 못했다")
    conn = db.connect()
    assert conn.execute("SELECT COUNT(*) FROM opportunities WHERE project_id=?",
                        (p["id"],)).fetchone()[0] == 1      # 삭제되지 않았다
    conn.close()


def test_gather_refuses_mixed_periods():
    conn = db.connect()
    p = _project(conn, "period")
    _snap(conn, p["id"], "2026-01-01", 90, "kw", 12.0, 5)
    _snap(conn, p["id"], "2026-02-01", 28, "kw", 8.0, 9)
    d = dashboard.gather(conn, p)
    assert d["gsc_date"] == "2026-02-01" and d["gsc_period"] == 28
    assert d["gsc_prev"] is None, d["gsc_prev"]       # 90일치와는 짝짓지 않는다
    assert d["period_mismatch"] is True
    assert d["ups"] == [] and d["downs"] == []

    _snap(conn, p["id"], "2026-01-15", 28, "kw", 11.0, 4)
    d = dashboard.gather(conn, p)
    assert d["gsc_prev"] == "2026-01-15", d["gsc_prev"]   # 같은 28일치를 찾아간다
    assert d["period_mismatch"] is False
    assert d["ups"] and d["ups"][0]["dpos"] == 3.0        # 11.0 -> 8.0
    conn.close()


def test_opportunity_upsert_does_not_duplicate():
    conn = db.connect()
    p = _project(conn, "opp")
    sql = """INSERT INTO opportunities(project_id,kind,target,score,reasoning)
             VALUES(?,?,?,?,?)
             ON CONFLICT(project_id,kind,target) DO UPDATE SET
               score=excluded.score, reasoning=excluded.reasoning"""
    conn.execute(sql, (p["id"], "striking_distance", "kw", 50, "첫 런"))
    conn.execute("UPDATE opportunities SET status='done' WHERE project_id=?", (p["id"],))
    conn.execute(sql, (p["id"], "striking_distance", "kw", 80, "두 번째 런"))
    conn.commit()
    rows = conn.execute("SELECT score, reasoning, status FROM opportunities "
                        "WHERE project_id=?", (p["id"],)).fetchall()
    assert len(rows) == 1, rows                       # 같은 기회가 두 줄이 되지 않는다
    assert rows[0]["score"] == 80 and rows[0]["reasoning"] == "두 번째 런"
    assert rows[0]["status"] == "done"                # 손댄 상태는 살아남는다
    conn.close()


def test_scoring_rules():
    """판정 규칙 module 자체점검 — 임계값·남의 브랜드 제외·기회 정렬."""
    scoring._selfcheck()


def test_collector_settings():
    """설정 우선순위(CLI > 프로젝트 yaml > config.yaml > 리터럴) 자체점검."""
    collector._selfcheck()


def test_keyword_candidates_always_carry_locale():
    """자동완성으로 캔 후보가 locale 없이 쌓이면 프로젝트 로케일로 다시 조회돼
    한국어 키워드가 전부 '순위 없음'이 된다 — 실제로 났던 버그다.
    쓰기 경로가 하나라서 호출부가 빼먹을 수 없어야 한다."""
    conn = db.connect()
    p = _project(conn, "loc")
    n = db.add_keyword_candidates(conn, p["id"], [
        ("한국어 키워드", "ko-KR", "autocomplete"),
        ("english keyword", "en-US", "autocomplete"),
        ("  ", "ko-KR", "autocomplete"),          # 빈 문자열은 세지 않는다
    ])
    assert n == 2, n
    rows = dict(conn.execute(
        "SELECT keyword, locale FROM keywords WHERE project_id=?", (p["id"],)).fetchall())
    assert rows["한국어 키워드"] == "ko-KR", rows
    assert rows["english keyword"] == "en-US", rows
    # 같은 후보를 또 넣어도 늘지 않는다
    assert db.add_keyword_candidates(conn, p["id"], [("한국어 키워드", "ko-KR", "serp")]) == 0
    conn.close()


def test_run_is_closed_even_on_crash():
    """수집 도중 예외가 나도 runs.finished_at 이 채워져야 한다.
    try/finally 없이 손으로 finish_run 하던 시절엔 '수집 이력'이 거짓말을 했다."""
    conn = db.connect()
    p = _project(conn, "crash")
    try:
        with db.run(conn, p["id"], "ai") as r:
            r.api_calls = 3
            raise RuntimeError("수집 중 폭발")
    except RuntimeError:
        pass
    row = conn.execute("SELECT finished_at, api_calls, notes FROM runs "
                       "WHERE project_id=? ORDER BY id DESC LIMIT 1", (p["id"],)).fetchone()
    assert row["finished_at"], row
    assert row["api_calls"] == 3, row
    assert "중단" in (row["notes"] or ""), row

    with db.run(conn, p["id"], "gsc") as r:
        r.notes = "정상"
    row = conn.execute("SELECT finished_at, notes FROM runs "
                       "WHERE project_id=? ORDER BY id DESC LIMIT 1", (p["id"],)).fetchone()
    assert row["finished_at"] and row["notes"] == "정상", row
    conn.close()


def test_gsc_snapshot_write_is_idempotent_per_day():
    """같은 날 다시 수집하면 덮어쓴다 — 자동 수집과 CSV 가져오기가 같은 함수를 쓴다."""
    conn = db.connect()
    p = _project(conn, "snap")
    db.write_gsc_snapshot(conn, p["id"], "2026-03-01", 28,
                          [("kw", "https://e.com/a", 1, 10, 0.1, 8.0)])
    db.write_gsc_snapshot(conn, p["id"], "2026-03-01", 28,
                          [("kw", None, 2, 20, 0.1, 7.0), ("kw2", None, 0, 5, 0.0, 30.0)])
    rows = conn.execute("SELECT query, page, clicks FROM gsc_snapshots "
                        "WHERE project_id=? ORDER BY query", (p["id"],)).fetchall()
    assert len(rows) == 2, rows
    assert rows[0]["page"] is None and rows[0]["clicks"] == 2, dict(rows[0])
    conn.close()


def test_creations_table_ships_with_schema():
    """create 스킬이 capture의 Brain 안에 제 테이블을 만들던 탓에
    대시보드가 방어적 try/except를 달고 있었다. 이제 스키마에 있다."""
    conn = db.connect()
    p = _project(conn, "creat")
    db.record_creation(conn, p["id"], "src/page.md", kind="striking_distance",
                       branch="capture/striking_distance-page")
    assert conn.execute("SELECT COUNT(*) FROM creations WHERE project_id=?",
                        (p["id"],)).fetchone()[0] == 1
    conn.close()


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)} passed  ({HOME})")
