#!/usr/bin/env python3
"""자체점검 — `python test_link_axis.py` (임시 폴더에서만 돈다, 진짜 Brain·네트워크는 안 건드림).

백링크 축의 새 이음매: **적은 것이 닫히고·실리고·말해지는가.**

  · 무료 GET 확인(backlink_checks) → 해소 규칙(scoring._resolve_mention·_resolve_prospect)
  · 받아 둔 줄 → 백링크 축 페이로드(dashboard._axis_backlinks 의 bl_*)
  · 페이로드 → 요청문(brief 의 backlink_mention 근거)

모듈 하나 안의 규칙(분류·문턱·응답 꼴)은 각 모듈의 _selfcheck 가 본다(link_check·link_profile·
collect_bing·cc_graph·collect_backlinks). 화면과의 이음매는 test_seams.py 의 210~215 다.
"""
import os
import sys
import tempfile
from pathlib import Path

HOME = Path(tempfile.mkdtemp(prefix="seo-miner-linkaxis-test-"))
os.environ["CAPTURE_HOME"] = str(HOME)
sys.path.insert(0, str(Path(__file__).parent))

import brief       # noqa: E402
import dashboard   # noqa: E402
import db          # noqa: E402
import link_check  # noqa: E402
import link_profile  # noqa: E402
import scoring     # noqa: E402

D = "2026-11-20"
SINCE = "2026-11-21 09:00:00"       # 기회가 선(또는 다시 열린) 시각
AFTER, BEFORE = "2026-11-21 15:30:00", "2026-11-21 08:00:00"


def _brain(name):
    conn = db.connect()
    conn.execute("INSERT INTO projects(name, domain, locale, type) VALUES(?,?,?,'saas')",
                 (name, f"{name}.example", "en-US"))
    conn.commit()
    return conn, db.get_project(conn, name)["id"]


def _check(conn, pid, kind, domain, url, state, at=AFTER, rel=None, meta=None, status=200, why="w"):
    conn.execute(
        "INSERT INTO backlink_checks(project_id, kind, domain, url, checked_at, state, status,"
        " rel_nofollow, meta_nofollow, why) VALUES (?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(project_id, kind, url) DO UPDATE SET checked_at=excluded.checked_at,"
        " state=excluded.state, rel_nofollow=excluded.rel_nofollow,"
        " meta_nofollow=excluded.meta_nofollow, status=excluded.status, why=excluded.why",
        (pid, kind, domain, url, at, state, status, rel, meta, why))
    conn.commit()


def test_free_check_closes_mention_the_same_day():
    """언급한 페이지를 직접 열어 우리 링크를 봤으면 **그날** 닫힌다 — 다음 유료 수집을 안 기다린다.
    하루 단위 판정(_after_day)이면 같은 날 확인은 영영 근거가 못 된다."""
    conn, pid = _brain("m1")
    U = "https://www.news.example/a"
    assert scoring._resolve_mention(conn, pid, "www.news.example", SINCE, {}) is None
    _check(conn, pid, "mention", "www.news.example", U, "linked")
    why = scoring._resolve_mention(conn, pid, "www.news.example", SINCE, {})
    assert why and why.tag == "better" and U in why and "직접 열어 확인" in why, why
    # www 유무가 달라도 같은 도메인이다(기회의 대상과 언급 줄의 표기가 어긋난다).
    assert scoring._resolve_mention(conn, pid, "news.example", SINCE, {})
    # 기회가 서기 **전**의 확인은 근거가 아니다 — 그때 봤는데도 기회가 섰다는 뜻이다.
    _check(conn, pid, "mention", "www.news.example", U, "linked", at=BEFORE)
    assert scoring._resolve_mention(conn, pid, "www.news.example", SINCE, {}) is None
    conn.close()


def test_absence_and_unknown_never_close():
    """'링크 없음'·'확인 불가'로는 닫지 않고, 종류가 다른 확인(prospect)도 남의 근거가 아니다."""
    conn, pid = _brain("m2")
    for i, state in enumerate(("no_link", "unknown")):
        _check(conn, pid, "mention", "n.example", f"https://n.example/{i}", state, status=403)
    assert scoring._resolve_mention(conn, pid, "n.example", SINCE, {}) is None
    _check(conn, pid, "prospect", "n.example", "https://n.example/", "linked")
    assert scoring._resolve_mention(conn, pid, "n.example", SINCE, {}) is None, "prospect 확인으로 mention 을 닫았다"
    assert scoring._resolve_prospect(conn, pid, "n.example", SINCE, {})
    conn.close()


def test_nofollow_closes_but_says_which_kind():
    """rel nofollow 와 meta nofollow 는 다른 일이다 — 닫는 사유가 어느 쪽인지 말한다."""
    conn, pid = _brain("m3")
    _check(conn, pid, "mention", "r.example", "https://r.example/p", "nofollow", rel=1, meta=0)
    why = scoring._resolve_mention(conn, pid, "r.example", SINCE, {})
    assert why and "rel nofollow" in why and "meta robots" not in why, why
    _check(conn, pid, "mention", "r.example", "https://r.example/p", "nofollow", rel=0, meta=1)
    why = scoring._resolve_mention(conn, pid, "r.example", SINCE, {})
    assert why and "meta robots nofollow" in why and "rel nofollow" not in why, why
    # 같은 도메인에 순위 반영 링크를 본 줄이 있으면 그쪽이 사유다(단서가 붙지 않는다).
    _check(conn, pid, "mention", "r.example", "https://r.example/q", "linked")
    why = scoring._resolve_mention(conn, pid, "r.example", SINCE, {})
    assert "/q" in why and "순위로 넘어가지" not in why, why
    conn.close()


def test_paid_evidence_still_closes_without_any_check():
    """무료 확인이 없어도 예전 근거(유료 수집의 we_have·linked)는 그대로 닫는다."""
    conn, pid = _brain("m4")
    conn.execute("INSERT INTO link_intersect(project_id, checked_date, domain, hits, we_have)"
                 " VALUES (?,?,?,?,1)", (pid, "2026-11-23", "p.example", 2))
    conn.execute("INSERT INTO brand_mentions(project_id, checked_date, url, domain, linked)"
                 " VALUES (?,?,?,?,1)", (pid, "2026-11-23", "https://q.example/a", "q.example"))
    conn.commit()
    assert "백링크 2026-11-23 기준" in scoring._resolve_prospect(conn, pid, "p.example", SINCE, {})
    assert "백링크 2026-11-23 기준" in scoring._resolve_mention(conn, pid, "q.example", SINCE, {})
    conn.close()


def _seed_profile(conn, pid, *, links=100, source=None):
    conn.execute(
        "INSERT INTO backlink_summary(project_id, checked_date, rank, backlinks, referring_domains,"
        " dofollow, nofollow, spam_score, tld_json, country_json, source) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (pid, D, 300, links, 3, 80, 20, 12, '{"com": 70, "io": 30}', '{"US": 60, "": 40}', source))
    conn.executemany(
        "INSERT INTO referring_domains(project_id, checked_date, domain, rank, backlinks, spam_score)"
        " VALUES (?,?,?,?,?,?)",
        [(pid, D, "big.example", 500, 60, 5), (pid, D, "mid.example", 200, 30, 45),
         (pid, D, "spam.example", 10, 10, 90)])
    conn.executemany(
        "INSERT INTO backlink_anchors(project_id, checked_date, anchor, backlinks) VALUES (?,?,?,?)",
        [(pid, D, "blue widget", 55), (pid, D, "P1", 30), (pid, D, "click here", 10), (pid, D, "", 5)])
    conn.commit()


def test_axis_carries_profile_and_flags_exact_match_skew():
    conn, pid = _brain("p1")
    _seed_profile(conn, pid)
    conn.execute("INSERT INTO keywords(project_id, keyword, is_active) VALUES (?,?,1)", (pid, "Blue Widget"))
    conn.execute("INSERT INTO keywords(project_id, keyword, is_active) VALUES (?,?,0)", (pid, "click here"))
    conn.commit()
    d = dashboard._axis_backlinks(conn, pid)
    mix = d["bl_anchor_mix"]
    by = {t["type"]: t for t in mix["types"]}
    # 사이트 이름(p1)은 브랜드, 추적 검색어와 같은 앵커는 정확 일치, 꺼 둔 키워드는 정확 일치가 아니다.
    assert set(by) == {"brand", "exact", "generic", "empty"}, sorted(by)
    assert (by["exact"]["share"], by["empty"]["backlinks"]) == (0.55, 5), by
    assert mix["warn"] and mix["exact_share"] == 0.55 and mix["warn_at"] == link_profile.ANCHOR_EXACT_WARN
    c = d["bl_dist"]["concentration"]
    assert (c["domain"], c["share"], c["tier"]) == ("big.example", 0.6, 0.25), c
    assert [t["key"] for t in d["bl_dist"]["tld"]] == ["com", "io"]
    assert [b["n"] for b in d["bl_spam"]["bands"]] == [1, 1, 1] and d["bl_spam"]["score"] == 12
    assert d["bl_source"]["id"] == "dataforseo" and d["bl_source"]["full"] is True, d["bl_source"]
    assert d["bl_insufficient"] == link_profile.INSUFFICIENT
    assert d["bl_graph"] is None and d["bl_bing"]["crawl"] == [] and d["bl_checks"] == []
    conn.close()


def test_axis_gives_none_not_numbers_when_evidence_is_thin():
    """근거가 모자라면 비율 칸이 None 이다 — 화면이 찍을 숫자가 페이로드에 아예 없다."""
    conn, pid = _brain("p2")
    d = dashboard._axis_backlinks(conn, pid)           # 수집 전
    assert d["bl_source"] is None and d["bl_spam"] is None
    assert d["bl_anchor_mix"]["types"] == [] and d["bl_anchor_mix"]["exact_share"] is None
    assert d["bl_dist"]["concentration"]["share"] is None and d["bl_velocity"]["flag"] is None
    assert d["bl_pages"]["zero"] is None and d["bl_reciprocal"] == {"crawled": False, "rows": []}
    _seed_profile(conn, pid, links=9, source="bing")   # 링크 9개, Bing 폴백
    conn.execute("DELETE FROM backlink_anchors")
    conn.execute("INSERT INTO backlink_anchors(project_id, checked_date, anchor, backlinks)"
                 " VALUES (?,?,?,?)", (pid, D, "blue widget", 9))
    conn.commit()
    d = dashboard._axis_backlinks(conn, pid)
    mix = d["bl_anchor_mix"]
    assert not mix["enough"] and not mix["warn"] and all(t["share"] is None for t in mix["types"]), mix
    assert d["bl_dist"]["concentration"]["share"] is None and d["bl_dist"]["concentration"]["why"]
    assert d["bl_source"]["id"] == "bing" and d["bl_source"]["full"] is False
    conn.close()


def test_reciprocal_zero_pages_and_velocity_reach_the_payload():
    conn, pid = _brain("p3")
    _seed_profile(conn, pid)
    run = conn.execute("INSERT INTO crawl_runs(project_id, finished_at, pages) VALUES (?, '2026-11-19', 3)",
                       (pid,)).lastrowid
    conn.executemany("INSERT INTO crawl_pages(run_id, url, status, depth, links_in) VALUES (?,?,?,?,?)",
                     [(run, "https://p3.example/", 200, 0, 9), (run, "https://p3.example/guide", 200, 1, 4),
                      (run, "https://p3.example/lonely/", 200, 2, 1), (run, "https://p3.example/gone", 404, 2, 1)])
    conn.executemany("INSERT INTO crawl_links(run_id, url_from, url_to, is_internal) VALUES (?,?,?,?)",
                     [(run, "https://p3.example/", "https://www.big.example/x", 0),
                      (run, "https://p3.example/", "https://big.example/y", 0),
                      (run, "https://p3.example/", "https://other.example/", 0),
                      (run, "https://p3.example/", "https://p3.example/guide", 1)])
    conn.executemany("INSERT INTO backlink_pages(project_id, checked_date, url, backlinks, referring_domains)"
                     " VALUES (?,?,?,?,?)", [(pid, D, "https://p3.example/guide/", 40, 22),
                                             (pid, D, "https://p3.example/", 9, 3)])
    conn.executemany(
        "INSERT INTO backlink_timeseries(project_id, month, referring_domains, new_referring_domains,"
        " lost_referring_domains) VALUES (?,?,?,?,?)",
        [(pid, f"2026-{m:02d}-28", 50 + m, n, 1) for m, n in ((6, 4), (7, 5), (8, 3), (9, 4), (10, 40))])
    conn.commit()
    d = dashboard._axis_backlinks(conn, pid)
    rc = d["bl_reciprocal"]
    assert rc["crawled"] and [(r["domain"], r["out_links"]) for r in rc["rows"]] == [("big.example", 2)], rc
    pg = d["bl_pages"]
    assert [r["url"] for r in pg["magnets"]] == ["https://p3.example/guide/", "https://p3.example/"]
    # 끝 '/' 만 다른 주소는 같은 페이지다. 404 는 페이지가 아니다.
    assert pg["zero_n"] == 1 and pg["zero"][0]["url"] == "https://p3.example/lonely/", pg
    v = d["bl_velocity"]
    assert (v["flag"], v["new"], v["usual"]) == ("spike", 40, 4.0), v
    conn.close()


def test_brief_never_says_no_link_for_a_page_it_could_not_open():
    """요청문 — 열지 못한 페이지(403)를 '링크가 없었다'고 쓰지 않는다. 본 것은 본 대로 쓴다."""
    o = {"id": 1, "kind": "backlink_mention", "target": "www.news.example", "score": 40.0,
         "reasoning": "", "status": "new"}
    row = {"domain": "www.news.example", "url": "https://www.news.example/a", "alias": "Acme",
           "title": "t", "snippet": "Acme said", "rank": 700, "linked": 0}
    chk = lambda state, why: {"kind": "mention", "domain": "www.news.example",
                              "url": "https://www.news.example/a", "checked_at": AFTER,
                              "state": state, "why": why}
    blocked = brief.text(o, {"bl_mentions": [row], "bl_checks": [
        chk("unknown", "응답 403: 그 사이트가 크롤러를 막았습니다")]}, "ko-KR")
    assert "직접 열어 확인하지 못했습니다" in blocked and "응답 403" in blocked, blocked[:900]
    assert "링크가 없다는 뜻이 아닙니다" in blocked and "링크가 없었습니다" not in blocked
    seen = brief.text(o, {"bl_mentions": [row], "bl_checks": [chk("no_link", "x")]}, "ko-KR")
    assert "직접 열어 봤고 우리에게 가는 링크가 없었습니다" in seen
    done = brief.text(o, {"bl_mentions": [row], "bl_checks": [chk("linked", "x")]}, "ko-KR")
    assert "이미 우리에게 가는 링크가 있습니다" in done and "부탁할 일이 아닙니다" in done
    # 다른 도메인·다른 종류의 확인은 섞이지 않고, 확인이 없으면 예전 그대로다.
    other = brief.text(o, {"bl_mentions": [row], "bl_checks": [
        {**chk("unknown", "y"), "domain": "else.example"}, {**chk("unknown", "y"), "kind": "prospect"}]}, "ko-KR")
    assert "직접 열어" not in other and other == brief.text(o, {"bl_mentions": [row]}, "ko-KR")
    # 요청문이 아는 상태는 link_check.STATES 전부다(모르는 상태는 '확인 못 함'으로 떨어진다).
    for s in link_check.STATES:
        assert "직접 열어" in brief.text(o, {"bl_mentions": [row], "bl_checks": [chk(s, "z")]}, "ko-KR"), s


if __name__ == "__main__":
    import shutil
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    try:
        for t in tests:
            t()
            print(f"  ok  {t.__name__}")
        print(f"\n{len(tests)} passed  ({HOME})")
    finally:
        shutil.rmtree(HOME, ignore_errors=True)
