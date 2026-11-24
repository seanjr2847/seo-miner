#!/usr/bin/env python3
"""회차 사이의 변화와 그 원인 가설 — 구글 업데이트 원장, 페이지 필드 비교, 클러스터 성적표.

이 파일이 못 박는 것:

  1. 원장 — 구글이 공지한 업데이트·서치콘솔 데이터 이상 구간(skill_config 한 벌)이 비교 창과
            겹칠 때만 붙고, 붙을 때는 늘 "원인 가설일 뿐"이라는 말과 출처 주소가 같이 간다.
            확인일이 30일을 넘기면 stale 이다(그 뒤 업데이트가 빠졌을 수 있다).
  2. 페이지 필드 비교 — 유효한 값끼리 바뀐 canonical·title·H1·noindex·schema 유형·상태 코드.
  3. 쓰기 전 카니벌 예방·내부 앵커 다양성·클러스터 라벨 SERP 겹침.
  4. 클러스터 성적표·묶음별 나쁨/주의 개수(점수 없음)·다음 분기에 확인할 숫자(예측 없음).
  5. 사이트 타입(publisher·agency)과 홈 신호로 추정.

CLAUDE.md 의 규율: 검사를 추가했으면 일부러 깨서 FAIL 이 나는 것까지 확인한다.
"""
from __future__ import annotations

import datetime
import json
import sqlite3
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
ROOT = Path(__file__).resolve().parents[3]

import db  # noqa: E402
import scoring  # noqa: E402
import skill_config  # noqa: E402


def _conn(ptype: str = "saas") -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    db._register_norm(conn)
    conn.execute("INSERT INTO projects(id,name,type,domain) VALUES(1,'t',?,'t.com')", (ptype,))
    return conn


def _gsc(conn, day: str, rows, period: int = 28) -> None:
    """rows: (query, page, clicks, impressions, position)"""
    conn.executemany(
        "INSERT INTO gsc_snapshots(project_id,snapshot_date,period_days,query,page,clicks,"
        "impressions,ctr,position) VALUES(1,?,?,?,?,?,?,0.0,?)",
        [(day, period, q, pg, c, i, pos) for q, pg, c, i, pos in rows])


# ── 1. 원장 ────────────────────────────────────────────────────────────────────

def test_ledger_entries_carry_source_and_verified_date():
    """항목마다 구글 주소와 확인일이 있다 — 없는 항목은 '누가 그랬다더라'다."""
    led = skill_config.CONFIG["google_ledger"]
    assert led["updates"] and led["gsc_anomalies"]
    for e in [*led["updates"], *led["gsc_anomalies"]]:
        assert e["url"].startswith(("https://status.search.google.com/",
                                    "https://support.google.com/")), e
        datetime.date.fromisoformat(e["last_verified"])
        datetime.date.fromisoformat(e["start"])
        assert e["end"] is None or e["end"] >= e["start"], e
    assert {e["kind"] for e in led["updates"]} <= set(scoring.LEDGER_KIND_LABEL), \
        "원장의 종류에 라벨이 없다"
    for e in led["gsc_anomalies"]:
        assert set(e["metrics"]) <= {"clicks", "impressions", "ctr", "position"} and e["metrics"], e


def test_ledger_overlap_only_when_window_touches():
    # 2026-05-21~06-02 코어 업데이트 — 창이 그 전에 끝나면 안 붙는다
    far = scoring.ledger_between("2026-04-10", "2026-05-20", today="2026-11-24")
    assert not [u for u in far["updates"] if u["start"] == "2026-05-21"], far
    hit = scoring.ledger_between("2026-05-01", "2026-05-21", today="2026-11-24")
    u = [u for u in hit["updates"] if u["start"] == "2026-05-21"]
    assert len(u) == 1 and u[0]["kind"] == "core" and u[0]["label"] == "코어 업데이트", hit
    assert u[0]["url"].startswith("https://status.search.google.com/incidents/")
    # 끝이 비어 있는(진행 중) 업데이트는 오늘까지로 본다
    live = scoring.ledger_between("2026-11-15", "2026-11-20", today="2026-11-24")
    assert [u for u in live["updates"] if u["end"] is None], live
    # 디스커버 업데이트는 웹 검색 실적의 가설이 아니다
    disc = scoring.ledger_between("2026-02-01", "2026-02-20", today="2026-11-24")
    assert all(u["kind"] != "discover" for u in disc["updates"]), disc


def test_ledger_stale_after_30_days():
    newest = max(e["last_verified"] for e in skill_config.CONFIG["google_ledger"]["updates"])
    d0 = datetime.date.fromisoformat(newest)
    fresh = scoring.ledger_between("2026-05-01", "2026-05-21",
                                   today=str(d0 + datetime.timedelta(days=30)))
    stale = scoring.ledger_between("2026-05-01", "2026-05-21",
                                   today=str(d0 + datetime.timedelta(days=31)))
    assert fresh["stale"] is False and stale["stale"] is True
    assert stale["verified"] == newest


def test_gsc_anomaly_only_when_windows_differ_in_exposure():
    """노출 로깅 오류(2025-05-13~2026-04-27): 두 창이 다 그 안이면 같은 오류 위의 비교다 —
    한쪽만 걸칠 때 노출·CTR·순위 변화가 계측 탓일 수 있다."""
    both_in = scoring.ledger_for_pair("2026-03-20", "2026-03-13", 28, today="2026-11-24")
    assert not both_in["anomalies"], both_in
    straddle = scoring.ledger_for_pair("2026-05-20", "2026-04-22", 28, today="2026-11-24")
    a = straddle["anomalies"]
    assert len(a) == 1 and "position" in a[0]["metrics"] and "clicks" not in a[0]["metrics"], a
    assert a[0]["url"].startswith("https://support.google.com/webmasters/answer/6211453")
    after = scoring.ledger_for_pair("2026-11-20", "2026-11-13", 28, today="2026-11-24")
    assert not after["anomalies"], after


def test_rank_decay_and_click_shift_carry_ledger():
    conn = _conn()
    # 창: 수집일-3 에서 끝나고 28일. 05-13 창 = 04-13~05-10, 06-03 창 = 05-04~05-31 → 5월 코어와 겹침
    _gsc(conn, "2026-05-13", [("a", "https://t.com/a", 10, 200, 4.0)])
    _gsc(conn, "2026-06-03", [("a", "https://t.com/a", 4, 200, 9.0)])
    rows = scoring.rank_decay(conn, 1)
    assert rows and rows[0]["query"] == "a"
    led = rows[0]["ledger"]
    assert [u["start"] for u in led["updates"]] == ["2026-05-21"], led
    assert led["caveat"] == scoring.LEDGER_CAVEAT
    sh = scoring.click_shift(conn, 1, "2026-06-03", "2026-05-13", 28)
    assert sh["ledger"]["updates"] and sh["ledger"]["from"] == "2026-04-13" \
        and sh["ledger"]["to"] == "2026-05-31", sh["ledger"]
    # 겹치는 것이 없으면 행에 키를 안 단다(빈 가설을 싣지 않는다)
    c2 = _conn()
    _gsc(c2, "2026-07-29", [("a", None, 10, 200, 4.0)])
    _gsc(c2, "2026-08-05", [("a", None, 4, 200, 9.0)])
    r2 = scoring.rank_decay(c2, 1)
    assert r2 and "ledger" not in r2[0], r2
    assert scoring.click_shift(c2, 1, "2026-08-05", "2026-07-29", 28)["ledger"] is None


def test_ledger_lines_say_hypothesis_and_cite_url():
    led = scoring.ledger_for_pair("2026-06-03", "2026-05-13", 28, today="2026-11-24")
    L = scoring.ledger_lines(led)
    txt = "\n".join(L)
    assert "원인 가설" in txt and "https://status.search.google.com/incidents/" in txt, txt
    assert "2026-05-21" in txt
    assert scoring.ledger_lines(None) == [] and scoring.ledger_lines(
        {"updates": [], "anomalies": [], "stale": False}) == []
    stale = scoring.ledger_for_pair("2026-06-03", "2026-05-13", 28, today="2027-06-01")
    assert any("확인" in s and "지났" in s for s in scoring.ledger_lines(stale)), \
        scoring.ledger_lines(stale)


# ── 2. 회차 사이 필드 값 비교 ──────────────────────────────────────────────────

def _obs(**kw):
    base = {"date": "2026-08-01", "src": "audit", "status": 200, "title": "T", "h1": ["H"],
            "canonical": "https://t.com/a", "robots": "", "schema": ["Article"]}
    return {**base, **kw}


def test_page_field_changes_detects_each_field():
    a = _obs()
    kinds = lambda b: [c["kind"] for c in scoring.page_field_changes(a, b)]
    assert kinds(_obs()) == []
    assert kinds(_obs(canonical="https://t.com/b")) == ["canonical_changed"]
    # 스킴·www·끝 슬래시만 다른 canonical 은 같은 주소다
    assert kinds(_obs(canonical="http://www.t.com/a/")) == []
    assert kinds(_obs(robots="noindex, follow")) == ["noindex_added"]
    assert [c["kind"] for c in scoring.page_field_changes(_obs(robots="noindex"), _obs())] \
        == ["noindex_removed"]
    assert kinds(_obs(title="T2")) == ["title_changed"]
    assert kinds(_obs(title="  T ")) == [], "공백만 다른 제목을 바뀌었다고 한다"
    assert kinds(_obs(h1=["H", "other"])) == ["h1_changed"]
    assert kinds(_obs(schema=["WebPage"])) == ["schema_lost", "schema_gained"]
    lost = scoring.page_field_changes(a, _obs(schema=[]))
    assert lost[0]["kind"] == "schema_lost" and lost[0]["before"] == "Article", lost
    # 2xx → 4xx/5xx 는 상태만 말한다 — 못 연 페이지의 빈 제목을 '제목이 지워졌다'고 하지 않는다
    broke = scoring.page_field_changes(a, _obs(status=404, title=None, h1=[], schema=[], canonical=None))
    assert [c["kind"] for c in broke] == ["status_broke"] and broke[0]["sev"] == "bad", broke
    assert kinds(_obs(status=None, title=None, h1=[], schema=[])) == [], \
        "요청 실패(상태 모름)를 변화로 센다"
    for c in scoring.page_field_changes(a, _obs(canonical="https://t.com/b", robots="noindex")):
        assert c["kind"] in scoring.FIELD_CHANGE_LABEL and c["sev"] in ("bad", "warn", "info"), c


def _crawl_run(conn, rid, day, pages, issues=()):
    conn.execute("INSERT INTO crawl_runs(id,project_id,started_at,finished_at,seed,pages) "
                 "VALUES(?,?,?,?,?,?)", (rid, 1, day + "T00:00:00Z", day + "T00:10:00Z", "home",
                                         len(pages)))
    for u, f in pages.items():
        f = {"status": 200, "title": "T", "h1": "H", "canonical": u, "robots": None,
             "schema_types": "Article", **f}
        conn.execute("INSERT INTO crawl_pages(run_id,url,status,title,h1,canonical,robots,"
                     "schema_types) VALUES(?,?,?,?,?,?,?,?)",
                     (rid, u, f["status"], f["title"], f["h1"], f["canonical"], f["robots"],
                      f["schema_types"]))
    for k, u in issues:
        conn.execute("INSERT INTO crawl_issues(run_id,kind,severity,url) VALUES(?,?,'warn',?)",
                     (rid, k, u))


def test_crawl_compare_reports_changed_fields():
    import dashboard
    conn = _conn()
    _crawl_run(conn, 1, "2026-08-01", {"https://t.com/a": {}, "https://t.com/b": {},
                                       "https://t.com/c": {}, "https://t.com/gone": {}})
    _crawl_run(conn, 2, "2026-08-08", {
        "https://t.com/a": {"canonical": "https://t.com/other"},     # 유효한 값끼리 바뀜
        "https://t.com/b": {"status": 500, "title": None, "h1": None, "schema_types": None},
        "https://t.com/c": {},                                        # 그대로
        "https://t.com/new": {}})                                     # 직전 회차에 없던 쪽
    cmp_ = dashboard.crawl_compare(conn, 1, 2)
    assert {"new", "fixed", "changed"} <= set(cmp_), cmp_
    by = {r["url"]: [c["kind"] for c in r["changes"]] for r in cmp_["changed"]}
    assert by == {"https://t.com/a": ["canonical_changed"], "https://t.com/b": ["status_broke"]}, by
    assert cmp_["changed"][0]["url"] == "https://t.com/b", "심한 것(나쁨)이 먼저 와야 한다"
    assert dashboard.crawl_compare(conn, 1, 1)["changed"] == [], "첫 회차는 비교할 것이 없다"


def _audit(conn, day, url, **f):
    f = {"status": 200, "title": "T", "h1_json": '["H"]', "canonical": url, "robots": "",
         "schema_json": '["Article"]', **f}
    conn.execute("INSERT INTO page_audits(project_id,checked_date,url,status,title,h1_json,"
                 "canonical,robots,schema_json) VALUES(1,?,?,?,?,?,?,?,?)",
                 (day, url, f["status"], f["title"], f["h1_json"], f["canonical"], f["robots"],
                  f["schema_json"]))


def test_page_diff_between_picks_same_source_pair():
    conn = _conn()
    u = "https://t.com/a"
    _audit(conn, "2026-04-01", u)
    _audit(conn, "2026-05-25", u, title="T2", schema_json="[]")
    _audit(conn, "2026-07-01", u, title="T3")            # 구간 뒤의 관측은 안 쓴다
    series = db.page_observations(conn, 1, [u, "https://t.com/none"])
    assert set(series) == {scoring.url_key(u)}, series
    d = scoring.page_diff_between(series[scoring.url_key(u)], "2026-04-13", "2026-05-31")
    assert (d["src"], d["from"], d["to"]) == ("audit", "2026-04-01", "2026-05-25"), d
    assert [c["kind"] for c in d["changes"]] == ["title_changed", "schema_lost"], d
    # 구간 안에 관측이 하나뿐이면 비교하지 않는다(None) — 없는 변화를 '없음'이라고 말하지 않는다
    assert scoring.page_diff_between(series[scoring.url_key(u)], "2026-02-01", "2026-03-01") is None
    assert scoring.page_diff_between({"audit": [], "crawl": []}, "2026-04-13", "2026-05-31") is None


def test_decay_brief_carries_changes_and_ledger():
    """순위 하락 요청문 — '떨어진 기간 그 페이지에서 바뀐 것' 표와 원장 줄. 본문은 dashboard 가
    실은 decay_context 한 벌에서 온다(요청문이 DB 를 다시 읽지 않는다)."""
    import brief
    ctx = {"decay_context": {"q1": {
        "prev": "2026-05-13", "at": "2026-06-03", "page": "https://t.com/a",
        "ledger": scoring.ledger_for_pair("2026-06-03", "2026-05-13", 28, today="2026-11-24"),
        "diff": {"src": "audit", "from": "2026-04-01", "to": "2026-05-25",
                 "changes": scoring.page_field_changes(_obs(), _obs(title="T2"))}}}}
    L = brief._decay_context_lines({"kind": "rank_decay", "target": "q1"}, ctx, [])
    txt = "\n".join(L)
    assert "떨어진 기간 그 페이지에서 바뀐 것" in txt and "T2" in txt and "2026-05-25" in txt, txt
    assert "원인 가설" in txt and "코어 업데이트" in txt, txt
    # 관측 짝이 없으면 '바뀐 것이 없다'가 아니라 '비교할 관측이 없다'
    ctx["decay_context"]["q1"]["diff"] = None
    none_txt = "\n".join(brief._decay_context_lines({"kind": "rank_decay", "target": "q1"}, ctx, []))
    assert "바뀐 것이 없" not in none_txt and "확인 필요" in none_txt, none_txt
    # 짝은 있는데 차이가 없으면 그렇게 말한다(관측 날짜와 함께)
    ctx["decay_context"]["q1"]["diff"] = {"src": "audit", "from": "2026-04-01",
                                          "to": "2026-05-25", "changes": []}
    same = "\n".join(brief._decay_context_lines({"kind": "rank_decay", "target": "q1"}, ctx, []))
    assert "2026-04-01" in same and "그대로" in same, same
    assert brief._decay_context_lines({"kind": "rank_decay", "target": "none"}, ctx, []) == []
    # _ev_decay 가 실제로 그 줄을 싣는다
    ev = "\n".join(brief._ev_decay({"kind": "rank_decay", "target": "q1"}, {**ctx, "downs": []}, []))
    assert "떨어진 기간 그 페이지에서 바뀐 것" in ev, ev


def test_decay_context_built_from_brain():
    import dashboard
    conn = _conn()
    u = "https://t.com/a"
    _gsc(conn, "2026-05-13", [("q1", u, 10, 200, 4.0)])
    _gsc(conn, "2026-06-03", [("q1", u, 4, 200, 9.0)])
    _audit(conn, "2026-04-01", u)
    _audit(conn, "2026-05-25", u, canonical="https://t.com/zzz")
    conn.execute("INSERT INTO runs(id,project_id,kind,started_at,finished_at) "
                 "VALUES(7,1,'analyze','2026-06-03T01:00:00Z','2026-06-03T01:01:00Z')")
    conn.execute("INSERT INTO opportunities(id,project_id,run_id,kind,target,score) "
                 "VALUES(5,1,7,'rank_decay','q1',50)")
    got = dashboard._decay_context(conn, 1, [{"id": 5, "kind": "rank_decay", "target": "q1"}],
                                   {"q1": u})
    c = got["q1"]
    assert (c["prev"], c["at"], c["page"]) == ("2026-05-13", "2026-06-03", u), c
    assert [x["kind"] for x in c["diff"]["changes"]] == ["canonical_changed"], c
    assert [x["start"] for x in c["ledger"]["updates"]] == ["2026-05-21"], c


def test_watch_rows_page_diff():
    """완료 후 관찰 — 완료 전 마지막 관측과 완료 뒤 최신 관측 사이에 그 페이지가 바뀌었나."""
    conn = _conn()
    u = "https://t.com/a"
    _gsc(conn, "2026-08-01", [("q1", u, 3, 300, 8.0)])
    _gsc(conn, "2026-08-20", [("q1", u, 5, 300, 6.0)])
    _audit(conn, "2026-08-02", u)
    _audit(conn, "2026-08-18", u, title="새 제목")
    conn.execute("INSERT INTO opportunities(id,project_id,kind,target,status,status_at) "
                 "VALUES(1,1,'ctr_gap','q1','done','2026-08-10T00:00:00Z')")
    conn.execute("INSERT INTO opportunities(id,project_id,kind,target,status,status_at) "
                 "VALUES(2,1,'ctr_gap','nopage','done','2026-08-10T00:00:00Z')")
    rows = {r["target"]: r for r in db.watch_rows(conn, 1)}
    pd = rows["q1"]["page_diff"]
    assert pd["page"] == u and (pd["from"], pd["to"]) == ("2026-08-02", "2026-08-18"), pd
    assert [c["kind"] for c in pd["changes"]] == ["title_changed"], pd
    assert rows["nopage"]["page_diff"] is None
    # 완료 뒤에 다시 읽은 적이 없으면 None — '안 바뀜'과 다르다
    c2 = _conn()
    _gsc(c2, "2026-08-01", [("q1", u, 3, 300, 8.0)])
    _audit(c2, "2026-08-02", u)
    c2.execute("INSERT INTO opportunities(id,project_id,kind,target,status,status_at) "
               "VALUES(1,1,'ctr_gap','q1','done','2026-08-10T00:00:00Z')")
    assert db.watch_rows(c2, 1)[0]["page_diff"] is None


def test_hold_carries_page_diff():
    """관찰 중 — 마지막 작업일 앞·뒤로 그 페이지의 값이 바뀌었나(고친 게 배포됐나)."""
    import dashboard
    conn = _conn()
    u = "https://t.com/a"
    _gsc(conn, "2026-08-01", [("q1", u, 3, 300, 8.0)])
    _audit(conn, "2026-08-02", u)
    _audit(conn, "2026-08-12", u, title="고친 제목")
    conn.execute("INSERT INTO opportunities(id,project_id,kind,target,status,score) "
                 "VALUES(1,1,'ctr_gap','q1','new',50)")
    conn.execute("INSERT INTO creations(project_id,opportunity_id,kind,file_path,created_at) "
                 "VALUES(1,1,'edit','app/a.tsx','2026-08-10T00:00:00Z')")
    hold = dashboard._axis_hold(conn, 1, None, {"opps": [], "opps_total": 0},
                                {"query_pages": {"q1": [{"page": u, "impressions": 300}]}},
                                today="2026-08-15")
    h = hold["holds"][0]
    assert h["page"] == u and h["page_diff"]["to"] == "2026-08-12", h
    assert [c["kind"] for c in h["page_diff"]["changes"]] == ["title_changed"], h
    # 같은 날의 관측은 '앞'이 아니다 — 고친 값이 기준이 되면 영영 '그대로'다
    assert db.page_diff_since({"audit": [_obs(date="2026-08-10"), _obs(date="2026-08-12")],
                               "crawl": []}, u, "2026-08-10") is None


# ── 3. 쓰기 전 카니벌 예방 · 내부 앵커 다양성 · 클러스터 라벨 검증 ──────────────

def _f(cluster=None, serp=(), page=None):
    return {"cluster": cluster, "serp": frozenset(serp), "page": page and scoring.url_key(page),
            "page_url": page, "page_src": "gsc" if page else None, "volume": 0}


def test_prewrite_overlap_points_to_existing_page():
    """안 뜨는 키워드의 검색결과가 이미 우리 페이지가 걸린 검색어와 겹치면 새 글이 아니라 보강이다.
    문턱은 지면 묶기와 같은 SERP_MIN_SHARED 한 벌이다."""
    S = ["u1", "u2", "u3", "u4", "u5"]
    facts = {"have": _f("c", S, "https://t.com/guide"),
             "near": _f("c", ["u1", "u2", "u3", "x", "y"]),       # 3개 겹침 → 보강
             "far": _f("c", ["u1", "u2", "p", "q", "r"]),         # 2개 → 새 글
             "blind": _f("c")}                                    # 검색결과를 안 쟀다 → 모름
    got = scoring.prewrite_overlap(facts, ["near", "far", "blind"])
    assert set(got) == {"near"}, got
    assert got["near"] == {"page": "https://t.com/guide", "via": "have",
                           "shared": 3, "of": 5}, got
    assert scoring.SERP_MIN_SHARED == 3


def test_coverage_marks_reinforce_keywords():
    conn = _conn()
    _gsc(conn, "2026-08-01", [("have", "https://t.com/guide", 5, 100, 4.0)])
    for i, (kw, urls) in enumerate([("have", "abcde"), ("near", "abcxy"), ("far", "pqrst")], 1):
        conn.execute("INSERT INTO keywords(id,project_id,keyword,cluster,is_active) "
                     "VALUES(?,1,?,'주제',1)", (i, kw))
        conn.executemany("INSERT INTO serp_results(keyword_id,checked_at,position,url) "
                         "VALUES(?,'2026-08-01',?,?)",
                         [(i, n, f"https://o.com/{c}") for n, c in enumerate(urls, 1)])
    cov = scoring.coverage(conn, 1)
    assert sorted(k["keyword"] for k in cov["keywords"]) == ["far", "near"], cov
    by = {k["keyword"]: k.get("reinforce") for k in cov["keywords"]}
    assert by["near"]["page"] == "https://t.com/guide" and by["far"] is None, by
    assert cov["by_cluster"] == {"주제": 2}, "개수는 그대로다 — 기회를 조용히 지우지 않는다"
    import brief
    L = brief._coverage_reinforce_lines([{"keyword": "near", "reinforce": by["near"]},
                                         {"keyword": "far", "reinforce": None}])
    txt = "\n".join(L)
    assert "새 글" in txt and "https://t.com/guide" in txt and "near" in txt and "far" not in txt, txt
    assert brief._coverage_reinforce_lines([{"keyword": "far"}]) == []


def test_anchor_diversity():
    pages = [{"url": "https://t.com/", "depth": 0, "status": 200, "robots": None},
             {"url": "https://t.com/a", "depth": 1, "status": 200, "robots": None},
             {"url": "https://t.com/b", "depth": 1, "status": 200, "robots": None},
             {"url": "https://t.com/c", "depth": 2, "status": 200, "robots": "noindex"},
             {"url": "https://t.com/d", "depth": 2, "status": 404, "robots": None}]
    L = lambda f, t, a, ch=0: {"from": f"https://t.com/{f}", "to": f"https://t.com/{t}",
                               "anchor": a, "chrome": ch}
    links = ([L(f"s{i}", "a", "여기") for i in range(3)]             # 5개 중 3개가 같은 앵커(60%)
             + [L("s3", "a", "가이드"), L("s4", "a", "자세히")]
             + [L(f"n{i}", "a", "메뉴", 1) for i in range(9)]         # 틀(메뉴) 링크는 몫에서 뺀다
             + [L("s0", "b", "x"), L("s1", "b", "y")])               # b: 들어오는 글 2곳
    got = scoring.anchor_diversity(links, pages)
    assert [(r["url"], r["anchor"], r["n"], r["of"]) for r in got["same_anchor"]] \
        == [("https://t.com/a", "여기", 3, 5)], got
    few = {r["url"]: r["n"] for r in got["few_inlinks"]}
    assert few == {"https://t.com/b": 2}, f"홈·noindex·오류 페이지는 빼고 센다: {few}"
    assert got["body_known"] is True
    # 표본이 작으면(본문 링크 4개) 몫을 말하지 않는다
    small = scoring.anchor_diversity([L(f"s{i}", "a", "여기") for i in range(4)], pages)
    assert small["same_anchor"] == [], small
    # 틀 여부를 모르는 옛 크롤 — 메뉴 앵커가 섞여 쏠림을 말할 수 없다
    old = scoring.anchor_diversity([L(f"s{i}", "a", "여기", None) for i in range(9)], pages)
    assert old["body_known"] is False and old["same_anchor"] == [], old


def test_cluster_serp_check():
    S = ["u1", "u2", "u3", "u4", "u5"]
    facts = {"a1": _f("A", S), "a2": _f("A", ["u1", "u2", "u3", "v", "w"]),
             "a3": _f("A", ["m1", "m2", "m3", "m4", "m5"]),          # A 인데 아무와도 안 겹침
             "b1": _f("B", ["u1", "u2", "u3", "u4", "z"]),           # B 인데 a1 과 4개 겹침
             "b2": _f("B", ["k1", "k2", "k3", "k4", "k5"]),          # B 가 둘뿐 — 따로라고 안 한다
             "none": _f("A")}
    got = scoring.cluster_serp_check(facts)
    assert [r["keyword"] for r in got["apart"]] == ["a3"], got
    tg = {(r["a"], r["b"]): r["shared"] for r in got["together"]}
    assert tg == {("a1", "b1"): 4, ("a2", "b1"): 3}, tg
    assert got["measured"] == 5


# ── 4. 클러스터 성적표 · 묶음별 개수 · 다음 분기에 확인할 숫자 ─────────────────

def test_cluster_report():
    P, A, B = "https://t.com/pillar", "https://t.com/a", "https://t.com/b"
    facts = {"k1": _f("주제", page=P), "k2": _f("주제", page=P), "k3": _f("주제", page=A),
             "k4": _f("주제", page=B), "k5": _f("주제"), "x": _f("다른", page=A), "u": _f(None, page=A)}
    k = scoring.url_key
    links = {(k(A), k(P)), (k(P), k(A)), (k(P), k(B))}
    rep = {r["cluster"]: r for r in scoring.cluster_report(facts, links)}
    assert set(rep) == {"주제", "다른"}, "클러스터 없는 키워드는 성적표에 안 선다"
    r = rep["주제"]
    assert (r["keywords"], r["with_page"], len(r["pages"])) == (5, 4, 3), r
    assert r["pillar"] == P, r
    assert r["pillar_links"] == {"have": 1, "of": 2} and r["to_pillar_missing"] == [B], r
    assert r["cross_links"] == {"have": 3, "of": 6}, r
    assert sorted(map(tuple, r["edges"])) == sorted([(A, P), (P, A), (P, B)]), r
    one = rep["다른"]
    assert one["pillar_links"] == {"have": 0, "of": 0} and one["cross_links"]["of"] == 0, one
    # 크롤이 없으면 링크 칸은 모름(None)이다 — 0 이 아니다
    blind = {r["cluster"]: r for r in scoring.cluster_report(facts, None)}["주제"]
    assert blind["pillar_links"] is None and blind["cross_links"] is None and blind["edges"] == []


def test_evidence_gate():
    """근거 요인이 4/7 에 못 미치면 개수 대신 '근거 부족' — 점수는 어디에도 없다."""
    assert scoring.evidence_enough(4, 7) and scoring.evidence_enough(2, 3)
    assert not scoring.evidence_enough(3, 7) and not scoring.evidence_enough(1, 2)
    assert not scoring.evidence_enough(0, 0)


def test_group_tallies_count_bad_warn_with_delta_and_gate():
    import dashboard
    conn = _conn()
    # 사이트 건강: 크롤 두 회차 — 나쁨 1→2, 주의 2→1
    for rid, day, sev in ((1, "2026-08-01", ["bad", "warn", "warn", "info"]),
                          (2, "2026-08-08", ["bad", "bad", "warn"])):
        conn.execute("INSERT INTO crawl_runs(id,project_id,started_at,finished_at) VALUES(?,1,?,?)",
                     (rid, day + "T00:00:00Z", day + "T00:09:00Z"))
        conn.executemany("INSERT INTO crawl_issues(run_id,kind,severity,url) VALUES(?,?,?,?)",
                         [(rid, "x", s, f"u{i}") for i, s in enumerate(sev)])
    for k in ("index", "crawl", "vitals"):
        conn.execute("INSERT INTO runs(project_id,kind,started_at,finished_at) "
                     "VALUES(1,?,'2026-08-08T00:00:00Z','2026-08-08T00:10:00Z')", (k,))
    # 검색 성과: 단계 5개 중 gsc 하나만 쟀다 → 근거 부족
    conn.execute("INSERT INTO runs(project_id,kind,started_at,finished_at) "
                 "VALUES(1,'gsc','2026-08-08T00:00:00Z','2026-08-08T00:10:00Z')")
    conn.execute("INSERT INTO opportunities(project_id,kind,target,status,created_at) "
                 "VALUES(1,'rank_decay','q','new','2026-08-08T01:00:00Z')")
    for kind, t in (("rank_decay", "q"), ("striking_distance", "q2")):   # 심사를 통과한 것만 센다
        conn.execute("INSERT INTO verdicts(project_id,key,verdict) VALUES(1,?,'work')",
                     (scoring.verdict_key(kind, t),))
    import datetime as _dt
    now = _dt.datetime(2026, 8, 9, tzinfo=_dt.timezone.utc)
    groups = {g["id"]: g for g in dashboard._axis_groups(conn, 1, now=now)["groups"]}
    site = groups["site"]["tally"]
    assert site["enough"] is True and (site["bad"], site["warn"]) == (2, 1), site
    assert (site["d_bad"], site["d_warn"]) == (1, -1) and site["basis"], site
    search = groups["search"]["tally"]
    assert search["enough"] is False and (search["measured"], search["of"]) == (1, 5), search
    assert "bad" not in search, "근거가 모자란데 개수를 싣는다"
    assert groups["admin"]["tally"] is None, "잴 단계가 없는 묶음에는 개수가 없다"
    for g in groups.values():
        assert "score" not in (g["tally"] or {}), "합성 점수를 만들지 않는다"
    # 열린 기회로 세는 묶음 — 방어형(이미 잃는 중)이 나쁨, 그 밖이 주의. 증감은 지난 회차 뒤의 변화.
    for k in ("ga4", "keywords", "metrics", "rank"):
        conn.execute("INSERT INTO runs(project_id,kind,started_at,finished_at) "
                     "VALUES(1,?,'2026-08-08T00:00:00Z','2026-08-08T00:10:00Z')", (k,))
    conn.execute("INSERT INTO opportunities(project_id,kind,target,status,created_at) "
                 "VALUES(1,'striking_distance','q2','new','2026-08-01T01:00:00Z')")
    s2 = {g["id"]: g for g in dashboard._axis_groups(conn, 1, now=now)["groups"]}["search"]["tally"]
    assert s2["enough"] and (s2["bad"], s2["warn"]) == (1, 1) and (s2["d_bad"], s2["d_warn"]) == (1, 0), s2


def test_kpi_watch_states_now_and_direction_without_forecast():
    import dashboard
    conn = _conn()
    _gsc(conn, "2026-07-01", [("q", "https://t.com/a", 10, 1000, 9.0)])
    _gsc(conn, "2026-08-01", [("q", "https://t.com/a", 14, 900, 7.0)])
    k = dashboard._kpi_watch(conn, 1, None, today="2026-08-02")
    rows = {r["id"]: r for r in k["rows"]}
    assert set(rows) >= {"clicks", "impressions", "ctr", "position"}, rows
    assert (rows["clicks"]["now"], rows["clicks"]["prev"], rows["clicks"]["dir"]) == (14, 10, "up")
    assert rows["impressions"]["dir"] == "down" and rows["impressions"]["good"] is False
    # 순위는 숫자가 작아질수록 좋다 — 방향은 '좋아짐'으로 읽힌다
    assert rows["position"]["dir"] == "down" and rows["position"]["good"] is True, rows["position"]
    assert k["check_on"] == "2026-10-31", k
    assert k["from"] == "2026-07-01" and k["to"] == "2026-08-01"
    blob = json.dumps(k, ensure_ascii=False)
    for word in ("예상", "전망", "목표", "forecast", "target", "expected"):
        assert word not in blob, f"예측을 싣는다: {word}"
    # 맞댈 직전 구간이 없으면 방향을 말하지 않는다
    c2 = _conn()
    _gsc(c2, "2026-08-01", [("q", None, 14, 900, 7.0)])
    r2 = {r["id"]: r for r in dashboard._kpi_watch(c2, 1, None, today="2026-08-02")["rows"]}
    assert r2["clicks"]["now"] == 14 and r2["clicks"]["prev"] is None and r2["clicks"]["dir"] is None


def test_structure_axis_reads_brain():
    """클러스터 성적표·내부 링크 지도·앵커 점검·라벨 검증을 Brain 에서 한 벌로 조립한다."""
    import dashboard
    conn = _conn()
    P, A = "https://t.com/pillar", "https://t.com/a"
    _gsc(conn, "2026-08-01", [("k1", P, 5, 100, 4.0), ("k2", A, 5, 100, 4.0),
                              ("k3", P, 5, 100, 4.0)])
    for i, kw in enumerate(("k1", "k2", "k3", "k4"), 1):
        conn.execute("INSERT INTO keywords(id,project_id,keyword,cluster,is_active) "
                     "VALUES(?,1,?,'주제',1)", (i, kw))
    _crawl_run(conn, 3, "2026-08-02", {"https://t.com/": {}, P: {}, A: {}})
    conn.execute("INSERT INTO crawl_links(run_id,url_from,url_to,anchor,is_internal,in_chrome) "
                 "VALUES(3,?,?,'필러',1,0)", (A, P + "/"))          # 끝 슬래시가 달라도 같은 주소
    crawl = {"run": {"id": 3}}
    s = dashboard._axis_structure(conn, 1, crawl)
    r = s["cluster_report"]["rows"][0]
    assert r["cluster"] == "주제" and r["keywords"] == 4 and r["with_page"] == 3, r
    assert r["pillar"] == P, r
    assert r["pillar_links"] == {"have": 1, "of": 1} and r["edges"] == [[A, P]], r
    assert s["cluster_report"]["crawled"] is True
    assert {x["url"] for x in s["link_health"]["few_inlinks"]} == {P, A}, s["link_health"]
    assert s["cluster_check"]["measured"] == 0
    blind = dashboard._axis_structure(conn, 1, {})
    assert blind["cluster_report"]["crawled"] is False and blind["link_health"] is None


# ── 5. 사이트 타입 ─────────────────────────────────────────────────────────────

def test_site_types_publisher_agency_registered():
    import dashboard
    ids = dashboard.PROJECT_TYPE_IDS
    assert "publisher" in ids and "agency" in ids, ids
    for t in ("publisher", "agency"):
        w = scoring.WEIGHTS[t]
        assert abs(sum(w.values()) - 1.0) < 1e-9, w
    # 새 숫자를 짓지 않았다 — 기존 프리셋 하나와 같은 계수다
    old = [scoring.WEIGHTS[t] for t in ("game", "local_business", "saas", "directory", "commerce")]
    assert scoring.WEIGHTS["publisher"] in old and scoring.WEIGHTS["agency"] in old


def test_guess_site_type_from_home_signals():
    g = scoring.guess_site_type
    assert g({"schema_types": ["Organization", "Product", "Offer"]})["type"] == "commerce"
    assert g({"schema_types": ["Dentist"]})["type"] == "local_business"
    assert g({"schema_types": ["NewsMediaOrganization", "WebSite"]})["type"] == "publisher"
    assert g({"schema_types": ["SoftwareApplication"]})["type"] == "saas"
    assert g({"schema_types": ["VideoGame"]})["type"] == "game"
    # 글 유형이 사이트 대부분이면 매체다(홈 한 장이 아니라 크롤 전체의 비율)
    pub = g({"schema_types": ["WebSite"], "page_types": [["Article"]] * 8 + [["WebPage"]] * 2})
    assert pub["type"] == "publisher" and pub["evidence"], pub
    few = g({"schema_types": ["WebSite"], "page_types": [["Article"]] * 2 + [["WebPage"]] * 8})
    assert few is None, few
    # 근거가 없으면 추정하지 않는다 — saas 로 떨어뜨리지 않는다
    assert g({"schema_types": ["WebSite", "Organization"]}) is None
    assert g({}) is None
    # 서로 다른 종류의 신호가 같이 있으면 고르지 않는다
    assert g({"schema_types": ["Product", "SoftwareApplication"]}) is None
    for case in ({"schema_types": ["Product"]}, {"schema_types": ["Dentist"]}):
        import dashboard
        assert g(case)["type"] in dashboard.PROJECT_TYPE_IDS


# ── 화면(브라우저) ─────────────────────────────────────────────────────────────
# 로컬: 메뉴 버튼과 [전체] 탭을 실제로 눌러 그 화면에 들어가서, 새로 그린 것이 **보이는지** 잰다.
# 호스팅: test_render.serve_hosted 의 진짜 FastAPI 앱이 같은 칸을 실어 보내 그리는지 본다.

W09_PROBE = """<script>addEventListener("load", function () {
  var want = (location.search.match(/[?&]w09=([a-z]+)/) || [])[1] || "";
  var n = 0, t = setInterval(function () {
    if (!document.getElementById("an-ledger") || !document.querySelector("#holds .hd-pd")) {
      if (++n > 70) clearInterval(t); return; }
    clearInterval(t);
    var out = document.createElement("div"); out.id = "__w09__"; out.hidden = true;
    document.body.appendChild(out);
    var res = {};
    var go = function (v, tab) {
      var b = document.querySelector('#nav button[data-v="' + v + '"]');
      if (!b) return false;
      b.click();
      if (tab) [].slice.call(document.querySelectorAll('#view-' + v + ' .vtabs [role="tab"]'))
        .filter(function (x) { return x.textContent.trim().indexOf(tab) === 0; })
        .forEach(function (x) { x.click(); });
      return true;
    };
    var vis = function (sel) { var e = document.querySelector(sel);
      return !!(e && e.getClientRects().length && e.getBoundingClientRect().height > 0); };
    var open = function (sel) { var e = document.querySelector(sel); if (e) e.open = true; return !!e; };
    var steps = {
      analysis: function () { res.nav_analysis = go("analysis", "전체");
        open("#an-struct-map"); open("#an-links-few");
        res.ledger = vis("#an-ledger"); res.struct = vis("#an-struct-tb"); res.links = vis("#an-links");
        return "#an-ledger"; },
      struct: function () { go("analysis", "전체"); open("#an-struct-map"); open("#an-links-few");
        return "#an-struct"; },
      site: function () { res.nav_site = go("site", "전체"); open("#cr-changed");
        res.changed = vis("#cr-changed li li"); return "#cr-changed"; },
      hold: function () { res.nav_hold = go("hold"); res.hold = vis("#holds .hd-pd"); return "#holds .hd-pd"; },
      overview: function () { res.nav_overview = go("overview"); open("#ov-pulse");
        res.pulse = vis("#ov-pulse-body table"); res.watch = vis('#watch [data-pd]'); return "#ov-pulse"; }
    };
    try {
      if (want) steps[want]();
      else ["analysis", "site", "hold", "overview"].forEach(function (k) { steps[k](); });
    } catch (err) { res.err = String(err && err.stack || err); }
    out.textContent = JSON.stringify(res);
  }, 100);
});</script>"""


def _seed_screens(home: Path) -> None:
    """test_render.fixture 가 만든 Brain 에 이 파일이 볼 것을 더 심는다(SITES[1])."""
    import os
    import test_render as tr
    os.environ["CAPTURE_HOME"] = str(home)
    site = tr.SITES[1]
    A, B, HOME = (f"https://{site}.example/a", f"https://{site}.example/b",
                  f"https://{site}.example/")
    today = datetime.date.today()
    conn = db.connect()
    try:
        pid = conn.execute("SELECT id FROM projects WHERE name=?", (site,)).fetchone()[0]
        r1, r2 = [r[0] for r in conn.execute(
            "SELECT id FROM crawl_runs WHERE project_id=? ORDER BY id", (pid,))][:2]
        for rid, title in ((r1, "옛 제목"), (r2, "새 제목")):
            conn.executemany(
                "INSERT OR REPLACE INTO crawl_pages(run_id,url,status,depth,title,h1,canonical,"
                "schema_types) VALUES(?,?,200,?,?,?,?,?)",
                [(rid, HOME, 0, "홈", "홈", HOME, "Product,WebSite"),
                 (rid, A, 1, title, "H", A, "Article"), (rid, B, 1, "비", "H", B, "Article")])
        conn.execute("INSERT INTO crawl_links(run_id,url_from,url_to,anchor,is_internal,in_chrome)"
                     " VALUES(?,?,?,'자세히',1,0)", (r2, B, A))
        for kw in (f"{site} 검색어", f"{site} 곁 검색어"):
            conn.execute("INSERT OR IGNORE INTO keywords(project_id,keyword,is_active) VALUES(?,?,1)",
                         (pid, kw))
            conn.execute("UPDATE keywords SET cluster='피부 관리', is_active=1 WHERE project_id=?"
                         " AND keyword=?", (pid, kw))
        db.write_page_audits(conn, pid, "2026-05-10", [
            {"url": A, "status": 200, "title": "옛 제목", "h1_json": "[]", "schema_json": "[]"}])
        db.write_page_audits(conn, pid, str(today - datetime.timedelta(days=10)), [
            {"url": A, "status": 200, "title": "관찰 전 제목", "h1_json": "[]", "schema_json": "[]"}])
        db.write_page_audits(conn, pid, str(today - datetime.timedelta(days=1)), [
            {"url": A, "status": 200, "title": "관찰 뒤 제목", "h1_json": "[]", "schema_json": "[]"}])
        # 완료 후 관찰 — 완료(5/20) 앞·뒤 감사가 다르다
        conn.execute("INSERT INTO opportunities(project_id,kind,target,score,status,status_at)"
                     " VALUES(?,'ctr_gap',?,40,'done','2026-05-20T00:00:00Z')",
                     (pid, f"{site} 곁 검색어"))
        # 관찰 중 — 이틀 전에 그 페이지를 고쳤다
        oid = conn.execute("SELECT id FROM opportunities WHERE project_id=? AND kind="
                           "'striking_distance' AND status='new' ORDER BY id LIMIT 1",
                           (pid,)).fetchone()[0]
        conn.execute("INSERT INTO creations(project_id,opportunity_id,kind,file_path,created_at)"
                     " VALUES(?,?,'edit','app/a.tsx',?)",
                     (pid, oid, f"{today - datetime.timedelta(days=2)}T00:00:00Z"))
        conn.commit()
    finally:
        conn.close()


SCREEN_MUSTS = [
    (r'id="an-ledger"(?:(?!</div>).)*코어 업데이트(?:(?!</div>).)*구글 공지',
     "[클릭 변화]에 원장 띠가 없다 — 비교 기간이 구글 업데이트와 겹치는데 안 그렸다"),
    (r'id="an-ledger"(?:(?!</div>).)*서치콘솔 데이터 이상', "원장 띠에 서치콘솔 데이터 이상 구간이 없다"),
    (r'id="an-ledger"(?:(?!</div>).)*원인 가설', "원장 띠가 '원인 가설일 뿐'이라는 말 없이 섰다"),
    (r'값이 바뀐 것 <b>1</b>쪽', "[사이트 점검] 크롤 비교에 '값이 바뀐 것' 개수가 없다"),
    (r'id="cr-changed"(?:(?!</details>).)*title 변경: 옛 제목 → 새 제목',
     "크롤 비교가 바뀐 값(title)의 앞·뒤를 안 보여 준다"),
    (r'class="hd-pd" data-pd="changed"(?:(?!</div>).)*title 변경: 관찰 전 제목 → 관찰 뒤 제목',
     "[관찰 중] 페이지에 '고친 뒤 바뀐 값'이 없다"),
    (r'id="watch"(?:(?!</section>).)*data-pd="changed"[^>]*>페이지 <span class="nw">바뀐 값 1가지',
     "[개요] 고친 뒤 달라졌나 줄에 페이지 변화가 없다"),
    (r'id="an-struct-tb"(?:(?!</table>).)*data-cl="피부 관리"(?:(?!</table>).)*/a</a>',
     "[클릭 변화] 주제별 페이지와 내부 링크 표에 주제 줄·대표 페이지가 없다"),
    (r'id="an-links"(?:(?!</div>).)*들어오는 내부 링크가 3곳 미만인 페이지', "내부 링크 점검이 없다"),
    (r'id="ov-pulse"(?![^>]*\shidden)', "[개요] 묶음별 상태 띠가 숨어 있다"),
    (r'data-pulse="site"', "묶음별 상태에 사이트 건강 줄이 없다"),
    (r'data-kpi="clicks"(?:(?!</tr>).)*<td class="n">\d', "다음 분기에 확인할 숫자에 클릭의 지금 값이 없다"),
    (r'id="ov-pulse-body"(?:(?!</details>).)*예측이 아닙니다', "KPI 표가 예측이 아니라는 말 없이 섰다"),
    (r'id="ov-health-list"(?:(?!</ul>).)*서치콘솔 데이터 이상 구간', "데이터 점검에 이상 띠(gsc_anomaly)가 없다"),
    (r'id="ov-health-list"(?:(?!</ul>).)*쇼핑몰 · 브랜드', "데이터 점검에 사이트 종류 추정이 없다"),
    # 새로 그린 칸 안에서만 본다 — 페이지 전체를 보면 다른 검사의 수집기 글자까지 걸린다
    *[(rf'!id="{i}"(?:(?!</{end}>).)*>[^<]*\b(undefined|NaN|null)\b',
       f"새로 그린 칸(#{i})에 undefined·NaN·null 이 찍혔다")
      for i, end in (("an-ledger", "div"), ("an-struct", "section"), ("ov-pulse-body", "details"),
                     ("cr-changed", "details"), ("watch", "section"))],
    (r'data-kpi="position"(?:(?!</tr>).)*(올랐음|밀렸음|그대로)',
     "평균 순위의 방향을 '늘었음·줄었음'으로 말한다 — 순위는 숫자가 줄어야 오른 것이다"),
]
LOCAL_ONLY_MUSTS = [
    (r'<option value="publisher">publisher · 매체 · 블로그</option>', "설정의 사이트 종류에 publisher 가 없다"),
    (r'<option value="agency">agency · 에이전시 · 전문 서비스</option>', "설정의 사이트 종류에 agency 가 없다"),
]
# 로컬에서 실제로 눌러 들어간 뒤 보여야 하는 것 (W09_PROBE 의 결과 키)
CLICKED = ("nav_analysis", "ledger", "struct", "links", "nav_site", "changed", "nav_hold", "hold",
           "nav_overview", "pulse", "watch")


def run(shots: Path | None = None) -> None:
    import gc
    import html as htmlmod
    import os
    import re
    import shutil
    import tempfile
    import test_render as tr
    browser = tr.find_browser()
    if not browser:
        print("크롬/엣지가 없어 건너뜁니다")
        return
    home = Path(tempfile.mkdtemp(prefix="seo-miner-drift-"))
    import doctor
    for k in (doctor.MODE_ENV, doctor.TOOL_ENV, doctor.TERMINAL_ENV):
        os.environ.pop(k, None)
    real_fixture = tr.fixture

    def fixture(h: Path) -> None:       # serve_hosted 가 부르는 픽스처에도 같은 것을 심는다
        real_fixture(h)
        _seed_screens(h)

    tr.fixture = fixture
    try:
        fixture(home)
        import dashboard
        shell = dashboard.HTML.replace(b"</head>", tr.PROBE.encode("utf-8") + b"</head>", 1)
        local = shell.replace(b"</body>", W09_PROBE.encode("utf-8") + b"</body>", 1)
        with tr._stdlib(local) as site:
            url = f"{site.base}/d#{tr.SITES[1]}"
            page = tr.dom(browser, url, home / "chrome-profile")
            tr.check("로컬", page, SCREEN_MUSTS + LOCAL_ONLY_MUSTS, site.failures())
            m = re.search(r'id="__w09__"[^>]*>(.*?)</div>', page, re.S)
            assert m and m.group(1).strip(), "눌러 보는 스크립트가 결과를 못 남겼다 — 화면이 안 그려졌다"
            got = json.loads(htmlmod.unescape(m.group(1)))
            assert not got.get("err"), f"눌러 보는 스크립트가 터졌다: {got['err']}"
            off = [k for k in CLICKED if got.get(k) is not True]
            assert not off, f"메뉴·탭을 눌러 들어갔는데 안 보이는 것: {off} ({got})"
            if shots:
                shots.mkdir(parents=True, exist_ok=True)
                for v in ("analysis", "struct", "site", "hold", "overview"):
                    import subprocess
                    try:
                        subprocess.run(
                            [browser, "--headless=new", "--disable-gpu", "--no-sandbox",
                             f"--user-data-dir={home / 'chrome-profile'}",
                             "--virtual-time-budget=9000", "--window-size=1280,3400",
                             f"--screenshot={shots / (v + '.png')}",
                             f"{site.base}/d?w09={v}#{tr.SITES[1]}"],
                            capture_output=True, timeout=120)
                    except subprocess.TimeoutExpired:   # 눈으로 볼 사본일 뿐 — 검사는 위에서 끝났다
                        print(f"  (스크린샷 {v} 시간 초과)")
        print("  로컬(메뉴·탭을 눌러 들어감): ok")
        if (ROOT / "server" / "assets" / "dash.html").exists():
            with tr.serve_hosted(home / "hosted") as site:
                page = tr.dom(browser, f"{site.base}/d#{tr.SITES[1]}", home / "chrome-profile")
                site.after()
                tr.check("호스팅", page, SCREEN_MUSTS, site.failures())
            print("  호스팅(FastAPI): ok")
    finally:
        tr.fixture = real_fixture
        gc.collect()
        shutil.rmtree(home, ignore_errors=True)


def _static() -> list:
    return [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]


if __name__ == "__main__":
    failed = 0
    for fn in _static():
        try:
            fn()
            print(f"  ok  {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL  {fn.__name__}: {e!r}")
        except Exception as e:      # 아직 없는 함수(AttributeError)도 실패로 센다
            failed += 1
            print(f"FAIL  {fn.__name__}: {type(e).__name__}: {e}")
    if "--static" not in sys.argv:
        try:
            shot = [a.split("=", 1)[1] for a in sys.argv if a.startswith("--shots=")]
            run(Path(shot[0]) if shot else None)
        except AssertionError as e:
            failed += 1
            print(f"FAIL  화면: {e}")
    if failed:
        sys.exit(1)
    print("drift self-check ok")
