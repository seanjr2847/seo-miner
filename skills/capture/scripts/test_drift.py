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
    if failed:
        sys.exit(1)
    print("drift self-check ok")
