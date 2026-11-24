#!/usr/bin/env python3
"""2MB 넘는 HTML 을 잘라 읽었으면 그 사실이 행에 남는다 — `python test_html_truncated.py` (네트워크 0).

collect_page.MAX_HTML 을 넘는 문서는 앞부분만 파싱한다. 예전에는 그 사실이 어디에도 안 남아,
잘린 뒤쪽에 있던 H2·구조화 데이터·링크가 "없음"으로 읽혔다. 이제 page_audits.html_truncated 가
1 이다(0 = 전부 읽음, NULL = 이 칸이 생기기 전의 행 — 안 봤다).
"""
import contextlib
import io
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import collect_page  # noqa: E402
import db  # noqa: E402

HEAD = "<html><head><title>긴 문서</title></head><body><h1>제목</h1><h2>앞쪽 소제목</h2>"


def test_audit_marks_truncated_html():
    small = collect_page.audit_html("https://x.com/a", HEAD + "<p>본문</p></body></html>")
    assert small["html_truncated"] == 0, small["html_truncated"]

    pad = "<p>" + "가" * (collect_page.MAX_HTML + 10) + "</p>"
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        big = collect_page.audit_html("https://x.com/big", HEAD + pad + "<h2>뒤쪽 소제목</h2></body></html>")
    assert big["html_truncated"] == 1, big["html_truncated"]
    assert "뒤쪽 소제목" not in big["h2_json"] and "앞쪽 소제목" in big["h2_json"], big["h2_json"]
    assert "https://x.com/big" in err.getvalue() and "앞부분만" in err.getvalue(), err.getvalue()

    # 경계: 정확히 MAX_HTML 글자는 잘린 게 아니다
    exact = HEAD + "x" * (collect_page.MAX_HTML - len(HEAD))
    assert len(exact) == collect_page.MAX_HTML
    assert collect_page.audit_html("https://x.com/e", exact)["html_truncated"] == 0


def test_column_is_stored_and_old_databases_get_it():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    db._migrate(conn)
    pid = conn.execute("INSERT INTO projects(name, domain) VALUES('t', 'x.com')").lastrowid
    db.write_page_audits(conn, pid, "2026-11-24", [
        {"url": "https://x.com/big", "status": 200, "html_truncated": 1},
        {"url": "https://x.com/a", "status": 200, "html_truncated": 0},
        {"url": "https://x.com/old", "status": 200}])
    got = {r["url"]: r["html_truncated"] for r in conn.execute("SELECT url, html_truncated FROM page_audits")}
    assert got == {"https://x.com/big": 1, "https://x.com/a": 0, "https://x.com/old": None}, got

    # 이 칸이 없던 보관함 — 마이그레이션이 ADD COLUMN 으로 붙이고, 옛 행은 NULL(안 봤다)로 남는다
    old = sqlite3.connect(":memory:")
    old.row_factory = sqlite3.Row
    old.executescript(db.SCHEMA)
    old.execute("ALTER TABLE page_audits DROP COLUMN html_truncated")
    old.execute("INSERT INTO projects(name, domain) VALUES('t', 'x.com')")
    old.execute("INSERT INTO page_audits(project_id, checked_date, url, status) VALUES(1, '2026-01-01', 'u', 200)")
    db._migrate(old)
    assert old.execute("SELECT html_truncated FROM page_audits").fetchone()[0] is None


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)} passed")
