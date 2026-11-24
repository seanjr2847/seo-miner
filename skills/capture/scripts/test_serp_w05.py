#!/usr/bin/env python3
"""SERP 응답에서 버리던 것 쓰기 + 비교 재료 거르기 + 유료 선택 수집 — `python test_serp_w05.py`

임시 폴더에서만 돈다(진짜 Brain 은 안 건드림). 네트워크 호출은 없다 — 응답은 전부 고정
응답(fixture)이고, 칸 이름은 공식 문서에서 옮겼다(serp_adapter·serp_extras 주석의 URL).

  · serp_adapter._parse_serp: 지도 칸 항목 · 강조 스니펫 형식 · 광고 문구, Serper places → local_pack
  · db: 옛 Brain 에 새 칸이 붙는가(ADD COLUMN), NULL 과 빈 값을 안 섞는가
  · serp_kinds.peer_skip 한 벌을 상위 글 열기·요청문·SERP 겹침 셋이 같이 쓰는가
  · brief: 페이지 종류 한 줄 · 스니펫 형식 · 지도 칸 표 · 지역 주석 · 덧붙여 잰 것
  · serp_extras.run: 키 없음·꺼짐·기능 없는 검색어·이미 받은 것·계정 오류에서 돈을 안 쓰는가
  · expand_keywords.rival_expand: 구글이 돌려준 것만 후보가 되는가
"""
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

HOME = Path(tempfile.mkdtemp(prefix="seo-miner-w05-test-"))
os.environ["CAPTURE_HOME"] = str(HOME)
os.environ["GSC_CONFIG_DIR"] = str(HOME / "no-legacy")
sys.path.insert(0, str(Path(__file__).parent))

import brief              # noqa: E402
import collect_serp       # noqa: E402
import db                 # noqa: E402
import expand_keywords    # noqa: E402
import run_verdict        # noqa: E402
import scoring            # noqa: E402
import serp_adapter       # noqa: E402
import serp_extras        # noqa: E402
import serp_kinds         # noqa: E402

# DataForSEO organic advanced 의 항목 꼴(문서의 칸 이름 그대로).
DFS_ITEMS = [
    {"type": "paid", "rank_group": 1, "title": "울쎄라 가격 이벤트", "domain": "ad.example",
     "url": "https://ad.example/lp", "extra": {"description": "지금 예약하면 할인"}},
    {"type": "featured_snippet", "rank_group": 1, "domain": "www.snip.example",
     "url": "https://www.snip.example/a", "title": "T", "featured_title": "T",
     "description": "1. 세안\n2. 도포\n3. 진정", "table": None},
    {"type": "local_pack", "rank_group": 1, "title": "가나 피부과", "domain": None, "url": None,
     "phone": "02-000-0000", "is_paid": False,
     "rating": {"rating_type": "Max5", "value": 4.8, "votes_count": 312, "rating_max": None}},
    {"type": "local_pack", "rank_group": 2, "title": "우리 클리닉 강남점", "domain": "www.e.com",
     "url": "https://www.e.com/gangnam", "is_paid": False, "rating": None},
    {"type": "organic", "rank_group": 1, "url": "https://ko.wikipedia.org/wiki/x", "title": "위키"},
    {"type": "organic", "rank_group": 2, "url": "https://rival.example/guide", "title": "가이드"},
    {"type": "images", "rank_group": 1},
]


def _project(conn, name, domain="e.com", locale="ko-KR"):
    conn.execute("INSERT OR IGNORE INTO projects(name, domain, locale) VALUES(?,?,?)",
                 (name, domain, locale))
    conn.commit()
    return conn.execute("SELECT * FROM projects WHERE name=?", (name,)).fetchone()


def _kw(conn, pid, keyword, locale="ko-KR"):
    conn.execute("INSERT OR IGNORE INTO keywords(project_id, keyword, locale, is_active)"
                 " VALUES(?,?,?,1)", (pid, keyword, locale))
    conn.commit()
    return conn.execute("SELECT id, keyword, locale FROM keywords WHERE project_id=? AND keyword=?",
                        (pid, keyword)).fetchone()


# ── 파서 ──────────────────────────────────────────────────────────────────────

def test_parse_serp_keeps_local_pack_snippet_form_and_ad_copy():
    got = serp_adapter._parse_serp(DFS_ITEMS, 10)
    lp = got["local_pack"]
    assert [x["pos"] for x in lp] == [1, 2] and lp[0]["title"] == "가나 피부과", lp
    assert lp[0]["rating"] == 4.8 and lp[0]["reviews"] == 312 and lp[0]["domain"] is None, lp[0]
    assert lp[1]["domain"] == "e.com" and lp[1]["rating"] is None, lp[1]
    assert got["snippet"] == {"form": "list", "domain": "snip.example",
                              "url": "https://www.snip.example/a"}, got["snippet"]
    assert got["ads"] == [{"title": "울쎄라 가격 이벤트", "description": "지금 예약하면 할인",
                           "domain": "ad.example"}], got["ads"]
    # 기능 이름은 예전대로 남는다(요청문의 형식 표가 그 이름을 본다)
    assert {"local_pack", "featured_snippet", "paid", "images"} <= set(got["serp_features"])
    # 없던 조회는 빈 값이다 — 지도 칸이 없는데 항목이 생기지 않는다
    none = serp_adapter._parse_serp([DFS_ITEMS[4]], 10)
    assert none["local_pack"] == [] and none["snippet"] is None and none["ads"] == []


def test_snippet_form_is_table_only_when_the_response_carries_a_table():
    f = serp_kinds.snippet_form
    assert f({"description": "x", "table": {"table_header": ["제품", "값"],
                                            "table_content": [["a", "1"]]}}) == "table"
    assert f({"description": "한 문단짜리 직답.", "table": None}) == "paragraph"
    assert f({"description": "a\nb", "table": None}) == "paragraph"      # 두 줄은 목록이 아니다
    assert f({"description": None}) is None                              # 못 가르면 모름
    assert set(brief.SNIPPET_FORM_HINT) == set(serp_kinds.SNIPPET_FORMS)


def test_serper_places_become_local_pack():
    class R:
        status_code = 200
        text = ""

        def raise_for_status(self):
            pass

        def json(self):
            return {"organic": [{"position": 1, "link": "https://a.example/x", "title": "A"}],
                    "places": [{"title": "가나 치과", "rating": 4.5, "ratingCount": 20,
                                "website": "https://www.e.com/"}]}

    orig_post, orig_key = serp_adapter.requests.post, os.environ.get("SERPER_API_KEY")
    serp_adapter.requests.post = lambda *a, **k: R()
    os.environ["SERPER_API_KEY"] = "k"
    try:
        got = serp_adapter.fetch("serper", "치과", "ko-KR")
        bare = None
        R.json = lambda self: {"organic": []}
        bare = serp_adapter.fetch("serper", "치과", "ko-KR")
    finally:
        serp_adapter.requests.post = orig_post
        if orig_key is None:
            os.environ.pop("SERPER_API_KEY", None)
        else:
            os.environ["SERPER_API_KEY"] = orig_key
    assert "local_pack" in got["serp_features"], got["serp_features"]
    assert got["local_pack"] == [{"pos": 1, "title": "가나 치과", "domain": "e.com",
                                  "url": "https://www.e.com/", "rating": 4.5, "reviews": 20,
                                  "is_paid": False}], got["local_pack"]
    assert "local_pack" not in bare["serp_features"] and bare["local_pack"] == []


# ── DB ────────────────────────────────────────────────────────────────────────

def test_old_brain_gets_the_new_columns_and_keeps_null_apart_from_empty():
    # 새 칸이 없던 Brain — 연결만 해도 칸이 붙는다(ADD COLUMN, 옛 행은 NULL)
    home = Path(tempfile.mkdtemp(prefix="seo-miner-w05-old-"))
    raw = sqlite3.connect(home / "brain.db")
    raw.executescript("""
        CREATE TABLE rank_snapshots (id INTEGER PRIMARY KEY, keyword_id INTEGER NOT NULL,
          checked_at TEXT, position INTEGER, url TEXT, serp_features_json TEXT,
          aio_present INTEGER, aio_cited INTEGER);
        CREATE TABLE serp_outlines (id INTEGER PRIMARY KEY, url TEXT NOT NULL UNIQUE,
          checked_at TEXT, status INTEGER, title TEXT, h2_json TEXT, words INTEGER);
        INSERT INTO serp_outlines(url, status, h2_json) VALUES('https://old.example/a', 200, '[]');
    """)
    raw.commit()
    raw.close()
    conn = db.connect(home)
    cols = lambda t: {r["name"] for r in conn.execute(f"PRAGMA table_info({t})")}  # noqa: E731
    assert {"local_pack_json", "snippet_form", "ads_json"} <= cols("rank_snapshots")
    assert "schema_json" in cols("serp_outlines")
    assert {"project_id", "keyword_id", "kind", "data_json"} <= cols("serp_extras")
    assert db.serp_outlines(conn, ["https://old.example/a"])["https://old.example/a"]["schema"] is None
    conn.close()

    conn = db.connect()
    p = _project(conn, "w05_db")
    k = _kw(conn, p["id"], "지도 검색어")
    db.write_serp_outline(conn, "https://x.example/a", status=200, title="t", h2=["가"], words=10,
                          schema=[])
    db.write_serp_outline(conn, "https://x.example/b", status=200, title="t", h2=["가"], words=10,
                          schema=["Product", "BreadcrumbList"])
    db.write_serp_outline(conn, "https://x.example/c", status=403, title=None, h2=[], words=None)
    got = db.serp_outlines(conn, [f"https://x.example/{c}" for c in "abc"])
    assert got["https://x.example/a"]["schema"] == []            # 봤는데 없었다
    assert got["https://x.example/b"]["schema"] == ["Product", "BreadcrumbList"]
    assert got["https://x.example/c"]["schema"] is None          # 못 봤다 — [] 로 접지 않는다

    db.write_rank_snapshot(conn, k["id"], 3, "https://e.com/a", ["local_pack"])
    db.write_rank_extras(conn, k["id"], None, local_pack={"items": [], "own_pos": None, "own_by": None},
                         snippet_form=None, ads=[])
    row = conn.execute("SELECT local_pack_json, snippet_form, ads_json FROM rank_snapshots"
                       " WHERE keyword_id=?", (k["id"],)).fetchone()
    assert tuple(row) == (None, None, None), tuple(row)          # 빈 값은 NULL — '0곳'이 아니다
    db.write_rank_extras(conn, k["id"], None, local_pack={
        "items": [{"pos": 1, "title": "가"}], "own_pos": 1, "own_by": "name"},
        snippet_form="table", ads=[{"title": "a"}])
    row = conn.execute("SELECT local_pack_json, snippet_form, ads_json, position FROM rank_snapshots"
                       " WHERE keyword_id=?", (k["id"],)).fetchone()
    assert json.loads(row["local_pack_json"])["own_pos"] == 1 and row["snippet_form"] == "table"
    assert row["position"] == 3                                   # 순위 행은 그대로다
    conn.close()


# ── 비교 재료 거르기 — 한 벌을 세 곳이 쓴다 ───────────────────────────────────

def test_peer_skip_reads_the_config_lists_and_domain_shapes():
    skip = serp_kinds.peer_skip
    assert skip("https://ko.wikipedia.org/wiki/x") == "platform"
    assert skip("https://www.zhihu.com/question/1") == "platform"        # 로케일 확장
    assert skip("https://detail.chiebukuro.yahoo.co.jp/qa") == "platform"
    assert skip("https://www.nytimes.com/2026/a.html") == "press"
    assert skip("https://www.nhs.gov.uk/conditions") == "public"
    assert skip("https://www.amazon.de/dp/1") == "market"
    assert skip("https://www.semrush.com/blog/x") == "seo_tool"
    assert skip("https://rival.example/tag/seo/") == "archive"
    assert skip("https://rival.example/author/kim/") == "archive"
    assert skip("https://rival.example/guide") is None
    assert skip("https://rival.example/tagged-posts") is None
    assert skip("", None) is None
    # 우리와 같은 종류의 사이트는 겨룰 상대다 — 대학에게 대학, 언론에게 언론, 마켓에게 마켓
    assert skip("https://www.mit.edu/x", own="snu.ac.kr") is None
    assert skip("https://www.nytimes.com/a", own="www.wsj.com") is None
    assert skip("https://www.amazon.de/dp/1", own="coupang.com") is None
    assert skip("https://www.nytimes.com/a", own="snu.ac.kr") == "press"     # 다른 종류는 뺀다
    assert skip("https://rival.example/tag/x/", own="snu.ac.kr") == "archive"
    assert set(serp_kinds.PEER_SKIP_LABEL) == {"platform", "press", "public", "market",
                                               "seo_tool", "archive"}
    assert serp_kinds.skip_summary(["press", "platform", "platform", None]) == \
        "제3자 플랫폼 2 · 뉴스·언론 1"


def test_outline_targets_skip_non_peers_and_take_the_next_three():
    rows = [{"url": "https://ko.wikipedia.org/wiki/x", "domain": "ko.wikipedia.org"},
            {"url": "https://r1.example/a", "domain": "r1.example"},
            {"url": "https://www.youtube.com/watch?v=1", "domain": "youtube.com"},
            {"url": "https://r3.example/a", "domain": "r3.example"},
            {"url": "https://r5.example/a", "domain": "r5.example"},
            {"url": "https://r6.example/a", "domain": "r6.example"}]
    # 예전엔 앞 세 자리(위키·r1·유튜브)를 열었다 — 견줄 글은 r1 하나뿐이었다
    assert collect_serp.outline_targets(rows) == [
        "https://r1.example/a", "https://r3.example/a", "https://r5.example/a"]
    assert len(rows) > db.SERP_KEEP                       # r6 은 요청문 상위 표 밖이라 안 연다
    mixed = [{"url": "https://e.com/mine", "domain": "e.com", "is_own": True},
             {"url": "https://r2.example/tag/x/", "domain": "r2.example"},
             {"url": None, "domain": "r4.example"},
             {"url": "https://www.mit.edu/a", "domain": "mit.edu"}]
    assert collect_serp.outline_targets(mixed) == []
    assert collect_serp.outline_targets(mixed, "snu.ac.kr") == ["https://www.mit.edu/a"]


def test_serp_overlap_ignores_pages_that_stand_on_every_serp():
    conn = db.connect()
    p = _project(conn, "w05_overlap")
    shared = ["https://ko.wikipedia.org/wiki/x", "https://www.youtube.com/watch?v=1",
              "https://www.reddit.com/r/a/1"]
    for kw, own in (("가 검색어", "https://ra.example/1"), ("나 검색어", "https://rb.example/1")):
        k = _kw(conn, p["id"], kw)
        db.write_serp_results(conn, k["id"], [
            {"position": n, "url": u, "title": "t", "domain": scoring.host_of(u)}
            for n, u in enumerate([*shared, own], 1)])
    facts = scoring._surface_facts(conn, p["id"], ["가 검색어", "나 검색어"])
    a, b = facts["가 검색어"]["serp"], facts["나 검색어"]["serp"]
    assert a == {scoring.url_key("https://ra.example/1")}, a
    assert len(a & b) == 0, "위키백과·유튜브·레딧 셋이 겹쳤다고 같은 지면으로 묶인다"
    assert scoring.SERP_MIN_SHARED <= len(shared)      # 안 걸렀으면 묶였을 수다
    conn.close()


# ── 요청문 ────────────────────────────────────────────────────────────────────

def _ctx(**kw):
    rows = [{"position": 1, "url": "https://ko.wikipedia.org/wiki/x", "title": "위키",
             "domain": "ko.wikipedia.org", "is_own": 0},
            {"position": 2, "url": "https://r1.example/a", "title": "가이드", "domain": "r1.example",
             "is_own": 0},
            {"position": 3, "url": "https://shop.example/p", "title": "상품", "domain": "shop.example",
             "is_own": 0}]
    out = lambda schema: {"h2": ["가격", "관리"], "words": 900, "tables": 1, "lists": 2,  # noqa: E731
                          "images": 3, "videos": 0, "schema": schema, "title": "t"}
    ctx = {"serp_top": {"검색어": rows},
           "serp_outlines": {"https://ko.wikipedia.org/wiki/x": out(["Article"]),
                             "https://r1.example/a": out(["BlogPosting", "BreadcrumbList"]),
                             "https://shop.example/p": out(["Product"])},
           "rank_by_kw": {"검색어": {"keyword": "검색어", "pos": 7, "features": []}},
           "project": {"locale": "ko-KR"}}
    ctx.update(kw)
    return ctx


def test_brief_keeps_non_peers_in_the_table_but_out_of_the_comparison():
    text = "\n".join(brief._serp_top({"target": "검색어"}, _ctx()))
    assert "| 1위 | 위키 |" in text, text                       # 표에는 그대로 선다
    assert "이 중 1자리(제3자 플랫폼 1)는" in text, text
    head = text.index(brief.SERP_OUTLINE_HEAD)
    assert "1위 위키" not in text[head:], "위키백과의 H2 를 비교 재료로 실었다"
    assert "- 2위 가이드" in text[head:] and "- 3위 상품" in text[head:]
    # 형식 표에도 1위(위키)가 없다 — 견줄 상대만 센다
    fmt = text[text.index("상위 글"):]
    assert "| 2위 | 900 |" in fmt and "| 1위 | 900 |" not in fmt, fmt


def test_brief_says_what_kind_of_page_ranks_from_one_classifier():
    text = "\n".join(brief._serp_top({"target": "검색어"}, _ctx()))
    assert "- 상위 글의 페이지 종류(그 글이 단 마크업 기준): 상품 1곳 · 글 1곳." in text, text
    # 마크업을 안 본 행뿐이면(이 칸 이전) 아무 말도 안 한다
    ctx = _ctx()
    for o in ctx["serp_outlines"].values():
        o["schema"] = None
    assert "페이지 종류" not in "\n".join(brief._serp_top({"target": "검색어"}, ctx))
    # 종류를 말해 주는 유형이 없으면(BreadcrumbList 뿐) 역시 말하지 않는다
    for o in ctx["serp_outlines"].values():
        o["schema"] = ["BreadcrumbList"]
    assert "페이지 종류" not in "\n".join(brief._serp_top({"target": "검색어"}, ctx))
    assert brief._page_kind_line([{"schema": ["Product"]}]) and \
        serp_kinds.page_kind(["Product"]) == "product"


def test_brief_feature_section_names_the_snippet_form_the_map_pack_and_the_region():
    row = {"keyword": "검색어", "features": ["featured_snippet", "local_pack"],
           "snippet_form": "table",
           "local_pack": {"items": [{"pos": 1, "title": "가나 피부과", "domain": None,
                                     "rating": 4.8, "reviews": 312, "is_paid": False},
                                    {"pos": 2, "title": "우리 클리닉", "domain": "e.com",
                                     "rating": None, "reviews": None, "is_paid": True}],
                          "own_pos": 2, "own_by": "domain"}}
    ctx = _ctx(rank_by_kw={"검색어": row}, kw_locales={"검색어": "de-DE"})
    text = "\n".join(brief._serp_feature_lines({"target": "검색어"}, ctx))
    assert brief.SNIPPET_FORM_HINT["table"] in text, text
    assert brief.SERP_FORMAT_HINT["featured_snippet"] not in text, "형식을 갈랐는데 뭉뚱그린 줄이 섰다"
    assert "| 1번째 | 가나 피부과 | - | 4.8 (리뷰 312) |" in text, text
    assert "| 2번째 (광고) | 우리 클리닉 | e.com | - |" in text, text
    assert "우리 업체는 이 지도 칸의 2번째입니다(사이트 도메인으로 확인)" in text
    assert "유럽경제지역(EEA)" in text, text
    assert text.index("## 지도 칸에 선 업체") < text.index(brief.NAP_HEAD)

    # 형식을 못 가른 조회는 예전 줄 그대로, 한국 조회에는 지역 주석이 없다
    row2 = {"keyword": "검색어", "features": ["featured_snippet", "local_pack"],
            "local_pack": {"items": [{"pos": 1, "title": "가나", "domain": None}],
                           "own_pos": None, "own_by": None}}
    t2 = "\n".join(brief._serp_feature_lines({"target": "검색어"}, _ctx(rank_by_kw={"검색어": row2})))
    assert brief.SERP_FORMAT_HINT["featured_snippet"] in t2 and "EEA" not in t2, t2
    assert "지도 칸에서 찾지 못했습니다" in t2 and "프로필이 없다는 뜻은 아닙니다" in t2
    # 지도 칸 항목이 없는 옛 행 — 표를 지어내지 않는다
    row3 = {"keyword": "검색어", "features": ["local_pack"]}
    t3 = "\n".join(brief._serp_feature_lines({"target": "검색어"}, _ctx(rank_by_kw={"검색어": row3})))
    assert "지도 칸에 선 업체" not in t3 and brief.NAP_HEAD in t3
    # 기능 목록이 비었는데 지역만 다른 조회 — 주석만 선다
    row4 = {"keyword": "검색어", "features": ["related_searches"]}
    t4 = "\n".join(brief._serp_feature_lines(
        {"target": "검색어"}, _ctx(rank_by_kw={"검색어": row4}, kw_locales={"검색어": "tr-TR"})))
    assert t4.startswith("## 이 지역의 검색결과 구성") and "튀르키예" in t4, t4


def test_region_table_covers_the_three_regions_and_nothing_else():
    assert [r for r, _, _ in serp_kinds.REGION_UNIT_NOTE] == ["eea", "tr", "za"]
    assert len(serp_kinds.EEA) == 30
    assert serp_kinds.region_note("fr-FR") and serp_kinds.region_note("no-NO")
    assert serp_kinds.region_note("en-GB") is None and serp_kinds.region_note("en-US") is None
    assert serp_kinds.region_note("de-CH") is None      # 스위스는 EEA 가 아니다
    # 조회 로케일이 실제로 있는 나라만 주석이 붙는다 — 로케일 표와 어긋나면 죽은 줄이다
    codes = {loc.partition("-")[2] for loc, _label in serp_adapter.LOCALES}
    assert codes & serp_kinds.EEA, "LOCALES 에 EEA 나라가 하나도 없다"


def test_brief_extras_lines_only_say_what_was_measured():
    assert brief._extras_lines({"target": "검색어"}, _ctx()) == []
    ex = {"bing_rank": {"검색어": {"checked_at": "2026-11-20T01:00:00Z",
                                 "data": {"position": 4, "url": "https://e.com/a", "top": []}}},
          "ai_mode": {"검색어": {"checked_at": "2026-11-20", "data": {
              "present": 1, "domains": ["r1.example", "e.com"], "cited": 1}}},
          "images": {"검색어": {"checked_at": "2026-11-20", "data": {
              "top": [{"pos": 1, "domain": "r1.example", "alt": "가"},
                      {"pos": 2, "domain": "r2.example", "alt": ""}], "own": [], "alt_filled": 1}}},
          "youtube": {"검색어": {"checked_at": "2026-11-20", "data": {"videos": [
              {"pos": 1, "title": "영상 제목", "channel": "채널", "views": 12345, "shorts": False}]}}},
          "trends": {"검색어": {"checked_at": "2026-11-20", "data": {
              "months": {f"{m:02d}": (90.0 if m == 12 else 30.0 if m != 7 else 10.0)
                         for m in range(1, 13)}, "peak": "12", "low": "07", "points": 60}}},
          "intent": {"검색어": {"checked_at": "2026-11-20", "data": {
              "label": "commercial", "probability": 0.82, "secondary": [], "ours": "info",
              "agree": False}}}}
    text = "\n".join(brief._extras_lines({"target": "검색어"}, _ctx(serp_extras=ex)))
    assert text.startswith("## 덧붙여 잰 것"), text
    assert "Bing 순위(2026-11-20): 4위" in text
    assert f"{brief.AI_MODE_NAME}(2026-11-20, 구글 AI 요약과 다른 인용 면입니다)" in text
    assert "우리 페이지도 인용됐습니다" in text
    assert "설명(alt)이 적힌 것 1/2장" in text and "우리 이미지는 상위에 없습니다" in text
    assert "1. 영상 제목 (채널, 조회 12,345회)" in text
    assert "가장 높은 달 12월(90), 가장 낮은 달 7월(10)" in text
    assert "우리 낱말 규칙은 '정보', DataForSEO 는 '비교·조사'(확률 0.82)" in text
    # 의도가 맞았으면 줄이 없다 — 맞은 것을 보고하느라 요청문을 늘리지 않는다
    ex["intent"]["검색어"]["data"]["agree"] = True
    assert "검색 의도 대조" not in "\n".join(brief._extras_lines({"target": "검색어"}, _ctx(serp_extras=ex)))
    assert brief.AI_MODE_NAME != brief._FEATURE_NAME["ai_overview"]


# ── 의도 ──────────────────────────────────────────────────────────────────────

def test_ad_copy_only_raises_intent_and_only_with_intent_words():
    c = scoring.classify_intent
    assert c("울쎄라") == "info"
    assert c("울쎄라", [{"title": "울쎄라 가격", "description": "지금 예약"}]) == "transactional"
    assert c("울쎄라", [{"title": "울쎄라 후기 비교", "description": ""}]) == "commercial"
    assert c("울쎄라", [{"title": "울쎄라란", "description": "원리 설명"}]) == "info"   # 광고가 섰다고 안 올린다
    assert c("울쎄라 가격", [{"title": "울쎄라 후기"}]) == "transactional"              # 내리지 않는다
    assert c("울쎄라", []) == "info" and c("울쎄라", None) == "info"

    conn = db.connect()
    p = _project(conn, "w05_intent")
    k = _kw(conn, p["id"], "리프팅")
    db.write_rank_snapshot(conn, k["id"], 3, None, ["paid"])
    db.write_rank_extras(conn, k["id"], None, ads=[{"title": "리프팅 가격", "description": ""}])
    assert scoring._latest_ads(conn, p["id"])[k["id"]][0]["title"] == "리프팅 가격"
    scoring._backfill_intents(conn, p["id"])
    assert conn.execute("SELECT intent FROM keywords WHERE id=?", (k["id"],)).fetchone()[0] \
        == "transactional"
    conn.close()


# ── 유료 선택 수집 ────────────────────────────────────────────────────────────

class _Dfs:
    """serp_adapter.post_dataforseo 대역 — 부른 경로를 적고 경로마다 고정 응답을 준다."""

    def __init__(self, fatal_on=None):
        self.calls, self.fatal_on = [], fatal_on

    def __call__(self, path, body, timeout=None):
        self.calls.append((path, body))
        if self.fatal_on and self.fatal_on in path:
            raise serp_adapter.fatal("DataForSEO", 402)
        if "search_intent" in path:
            return [{"items": [{"keyword": kw, "keyword_intent": {"label": "commercial",
                                                                  "probability": 0.7},
                                "secondary_keyword_intents": None}
                               for kw in body[0]["keywords"]]}], 0.011
        if "/images/" in path:
            return [{"items": [
                {"type": "images_search", "rank_group": 1, "title": "t", "alt": "설명",
                 "url": "https://www.e.com/a", "source_url": "https://cdn.e.com/a.jpg"},
                {"type": "images_search", "rank_group": 2, "title": "t", "alt": "",
                 "url": "https://r1.example/b", "source_url": "https://r1.example/b.jpg"}]}], 0.002
        if "/youtube/" in path:
            return [{"items": [{"type": "youtube_channel", "rank_group": 1},
                               {"type": "youtube_video", "rank_group": 1, "title": "영상",
                                "url": "https://www.youtube.com/watch?v=1", "channel_name": "채널",
                                "views_count": 10, "duration_time_seconds": 61,
                                "is_shorts": False}]}], 0.002
        if "/bing/" in path:
            return [{"items": [
                {"type": "organic", "rank_group": 1, "url": "https://r1.example/a", "title": "R"},
                {"type": "organic", "rank_group": 2, "url": "https://www.e.com/us/x", "title": "밖"},
                {"type": "organic", "rank_group": 3, "url": "https://www.e.com/kr/x", "title": "안"}]}], 0.002
        if "/ai_mode/" in path:
            return [{"items": [{"type": "ai_overview", "items": [{
                "type": "ai_overview_element",
                "references": [{"type": "ai_overview_reference", "domain": "r1.example",
                                "url": "https://r1.example/a"}]}]}]}], 0.004
        if "google_trends" in path:
            data = [{"date_from": f"20{y}-{m:02d}-01", "date_to": "x", "missing_data": False,
                     "values": [80 if m == 3 else 20]} for y in (24, 25) for m in range(1, 13)]
            data.append({"date_from": "2026-03-01", "missing_data": True, "values": [0]})
            return [{"items": [{"type": "google_trends_graph", "data": data}]}], 0.009
        if "llm_mentions" in path:
            return [{"items": [
                {"domain": "reddit.com", "total": {"mentions": 40, "ai_search_volume": 900}},
                {"key": "www.e.com", "total": [{"key": "google", "mentions": 3, "ai_search_volume": 10},
                                               {"key": "chat_gpt", "mentions": 2, "ai_search_volume": 5}]},
                {"domain": "r1.example", "total": {"mentions": 12, "ai_search_volume": None}}]}], 0.101
        raise AssertionError(path)


def _with_dfs(fake, fn, *, key=True):
    orig = serp_adapter.post_dataforseo
    saved = {k: os.environ.get(k) for k in ("DATAFORSEO_LOGIN", "DATAFORSEO_PASSWORD")}
    serp_adapter.post_dataforseo = fake
    for k in saved:
        if key:
            os.environ[k] = "x"
        else:
            os.environ.pop(k, None)
    try:
        return fn()
    finally:
        serp_adapter.post_dataforseo = orig
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_extras_spend_nothing_without_a_key_or_when_off():
    conn = db.connect()
    p = _project(conn, "w05_ex_off")
    kws = [_kw(conn, p["id"], "가")]
    fake = _Dfs()
    got = _with_dfs(fake, lambda: serp_extras.run(conn, p, kws, setting="intent",
                                                  default_locale="ko-KR"), key=False)
    assert got["note"] == "extras=skipped(DataForSEO 키 없음)" and got["cost"] == 0 and not fake.calls
    got = _with_dfs(fake, lambda: serp_extras.run(conn, p, kws, setting="none",
                                                  default_locale="ko-KR"))
    assert got["note"] == "extras=off" and not fake.calls
    # 기본으로는 아무것도 안 돈다 — 전부 유료라 설정을 안 건드린 기존 사이트의 지출이 몰래
    # 늘지 않게, 켜는 것은 사람이다(사이트 설정 serp_extras 또는 --extras)
    assert serp_extras.wanted(None) == [] and not any(v["default"] for v in serp_extras.KINDS.values())
    # 사이트마다 켜고 끌 수 있다 — 설정 키가 정본 목록에 있어야 저장된다(없으면 ValueError)
    db.set_project_settings(conn, p["id"], {"serp_extras": "bing_rank", "ai_llm_mentions": 1})
    cfg = db.project_cfg(conn, p["name"])
    assert serp_extras.wanted(cfg["serp_extras"]) == ["bing_rank"] and cfg["ai_llm_mentions"] == 1
    assert not any(serp_extras.KINDS[k]["default"] for k in ("bing_rank", "trends", "ai_mode"))
    conn.close()


def test_extras_opted_in_run_only_fetches_what_the_serp_showed_and_not_twice():
    conn = db.connect()
    p = _project(conn, "w05_ex_run")
    a, b = _kw(conn, p["id"], "이미지 검색어"), _kw(conn, p["id"], "글 검색어")
    # 기본값(설정을 안 건드린 사이트)은 유료 선택 수집을 하나도 안 돈다 — 켜는 것은 사람이다
    idle = _Dfs()
    none = _with_dfs(idle, lambda: serp_extras.run(conn, p, [a, b], default_locale="ko-KR",
        features_of={a["id"]: ["images", "video"]}))
    assert not idle.calls and none["cost"] == 0, none
    assert serp_extras.wanted(None) == [], serp_extras.DEFAULT
    fake = _Dfs()
    run = lambda: serp_extras.run(                                     # noqa: E731
        conn, p, [a, b], default_locale="ko-KR", setting="intent,images,youtube",
        features_of={a["id"]: ["images", "video"], b["id"]: ["people_also_ask"]})
    got = _with_dfs(fake, run)
    paths = [c[0] for c in fake.calls]
    assert sum("search_intent" in x for x in paths) == 1, paths       # 두 검색어를 한 건에
    assert sum("/images/" in x for x in paths) == 1 and sum("/youtube/" in x for x in paths) == 1
    assert fake.calls[0][1][0]["keywords"] == ["이미지 검색어", "글 검색어"]
    assert got["note"] == "extras=intent:2,images:1,youtube:1", got["note"]
    assert abs(got["cost"] - 0.015) < 1e-9 and got["calls"] == 3, got
    # 순위 단계의 판정 규칙(run_verdict)이 이 조각을 실패로 읽지 않는다
    assert run_verdict.judge({"notes": f"errors=0 {got['note']} extras_errors=2",
                              "finished_at": "t"})["state"] == "ok"

    ex = serp_extras.payload(conn, p["id"])
    img = ex["images"]["이미지 검색어"]["data"]
    assert img["own"] == [1] and img["alt_filled"] == 1 and img["top"][1]["domain"] == "r1.example"
    assert ex["youtube"]["이미지 검색어"]["data"]["videos"][0]["channel"] == "채널"
    it = ex["intent"]["글 검색어"]["data"]
    assert it == {"label": "commercial", "probability": 0.7, "secondary": [], "ours": "info",
                  "agree": False}, it
    assert "글 검색어" not in ex["images"]                            # 이미지 칸이 없던 검색어

    # 다시 돌면 아무것도 안 받는다(fresh_days 안)
    fake2 = _Dfs()
    again = _with_dfs(fake2, run)
    assert not fake2.calls and again["cost"] == 0 and again["note"] == "extras=none", again
    conn.close()


def test_extras_opt_in_kinds_parse_and_a_fatal_stops_the_rest():
    conn = db.connect()
    p = _project(conn, "w05_ex_opt")
    a = _kw(conn, p["id"], "가 검색어")
    fake = _Dfs()
    got = _with_dfs(fake, lambda: serp_extras.run(
        conn, p, [a], setting="bing_rank,trends,ai_mode", default_locale="ko-KR",
        scopes={"ko-KR": "/kr/"}))
    assert got["note"] == "extras=bing_rank:1,trends:1,ai_mode:1", got["note"]
    ex = serp_extras.payload(conn, p["id"])
    bing = ex["bing_rank"]["가 검색어"]["data"]
    assert bing["position"] == 3 and bing["url"].endswith("/kr/x"), bing   # 분석 범위 밖(/us/)은 내 순위가 아니다
    tr = ex["trends"]["가 검색어"]["data"]
    assert tr["peak"] == "03" and tr["points"] == 24 and tr["months"]["03"] == 80.0, tr
    assert ex["ai_mode"]["가 검색어"]["data"] == {"present": 1, "domains": ["r1.example"], "cited": 0}
    # 보낸 본문: 로케일이 실린다
    body = next(b for path, b in fake.calls if "/bing/" in path)[0]
    assert body["keyword"] == "가 검색어" and body["language_code"] == "ko", body

    # 잔액 없음(402)은 남은 종류를 전부 멈춘다 — 같은 오류를 검색어 수만큼 맞지 않는다
    b = _kw(conn, p["id"], "나 검색어")
    fake2 = _Dfs(fatal_on="/bing/")
    got = _with_dfs(fake2, lambda: serp_extras.run(
        conn, p, [b], setting="bing_rank,trends,ai_mode", default_locale="ko-KR"))
    assert [c[0] for c in fake2.calls] == ["/serp/bing/organic/live/advanced"], fake2.calls
    assert "stopped=bing_rank:" in got["note"] and got["cost"] == 0, got
    conn.close()


def test_extras_parsers_keep_unknown_apart_from_none():
    assert serp_extras.parse_ai_mode([{"items": [{"type": "ai_overview", "items": []}]}], "e.com") \
        == {"present": 1, "domains": None, "cited": None}       # 답은 있는데 인용을 못 뽑았다
    assert serp_extras.parse_trends([{"items": []}]) is None
    assert serp_extras.parse_bing([{"items": []}], "e.com") == {"position": None, "url": None, "top": []}
    assert serp_extras.parse_intent([{"items": [{"keyword": "x", "keyword_intent": None}]}], {}) == {}
    assert set(serp_extras.AXIS_NAME) == {"info", "navigational", "commercial", "transactional"}
    assert set(serp_extras._INTENT_AXIS.values()) == set(serp_extras.AXIS_NAME)


def test_llm_mentions_is_site_level_once_a_month():
    conn = db.connect()
    p = _project(conn, "w05_llm")
    fake = _Dfs()
    data, cost, note = _with_dfs(fake, lambda: serp_extras.llm_mentions(
        conn, p, ["가", "나", "가"], "en-US"))
    assert note == "llm_mentions=3" and cost == 0.101
    assert fake.calls[0][1][0]["target"] == [{"keyword": "가", "search_filter": "include"},
                                             {"keyword": "나", "search_filter": "include"}]
    assert [x["domain"] for x in data["top"]] == ["reddit.com", "r1.example", "e.com"], data
    assert data["top"][0]["third_party"] and data["top"][2]["own"] and data["top"][2]["mentions"] == 5
    assert data["own_rank"] == 3
    assert serp_extras.payload(conn, p["id"])["llm_mentions"]["data"]["targets"] == ["가", "나"]
    fake2 = _Dfs()
    _d, cost2, note2 = _with_dfs(fake2, lambda: serp_extras.llm_mentions(conn, p, ["가"], "en-US"))
    assert note2 == "llm_mentions=fresh" and cost2 == 0 and not fake2.calls
    _d, _c, note3 = _with_dfs(_Dfs(), lambda: serp_extras.llm_mentions(
        conn, _project(conn, "w05_llm2"), ["가"], "en-US"), key=False)
    assert note3 == "llm_mentions=skipped(DataForSEO 키 없음)"
    conn.close()


# ── 수집기 끝에서 끝까지 ──────────────────────────────────────────────────────

def test_rank_stage_writes_the_new_columns_and_leaves_extras_off_for_serper():
    conn = db.connect()
    p = _project(conn, "w05_e2e")
    db.set_project_settings(conn, p["id"], {"brand_aliases": ["우리 클리닉"]})
    k = _kw(conn, p["id"], "강남 피부과")
    conn.close()
    res = serp_adapter._parse_serp(DFS_ITEMS, 10)
    res["local_pack"][1].update(domain=None, url=None)      # 사이트를 안 건 프로필 — 이름으로 찾는다
    res["cost"] = 0.001
    opened = []
    import collect_page
    orig_fetch, orig_page = serp_adapter.fetch, collect_page.fetch
    serp_adapter.fetch = lambda *a, **kw: dict(res)

    def page(u, timeout=None):
        opened.append(u)
        return {"url": u, "status": 200, "title": "T", "h2_json": '["가"]', "words": 10,
                "schema_json": '["Article"]'}
    collect_page.fetch = page
    fake = _Dfs()
    try:
        out = _with_dfs(fake, lambda: collect_serp.collect("w05_e2e", provider="serper", throttle=0))
    finally:
        serp_adapter.fetch, collect_page.fetch = orig_fetch, orig_page
    assert out.ok, out
    assert not fake.calls, "serper 로 잰 런에서 DataForSEO 선택 수집이 돌았다"
    assert opened == ["https://rival.example/guide"], opened   # 위키백과는 안 연다
    conn = db.connect()
    row = conn.execute("SELECT local_pack_json, snippet_form, ads_json FROM rank_snapshots"
                       " WHERE keyword_id=?", (k["id"],)).fetchone()
    lp = json.loads(row["local_pack_json"])
    assert lp["own_pos"] == 2 and lp["own_by"] == "name" and len(lp["items"]) == 2, lp
    assert row["snippet_form"] == "list" and json.loads(row["ads_json"])[0]["domain"] == "ad.example"
    assert db.serp_outlines(conn, opened)[opened[0]]["schema"] == ["Article"]
    notes = conn.execute("SELECT notes FROM runs WHERE project_id=? ORDER BY id DESC LIMIT 1",
                         (p["id"],)).fetchone()[0]
    assert "extras=off" in notes, notes
    conn.close()


# ── 경쟁사 이름 씨앗 ──────────────────────────────────────────────────────────

def test_rival_seeds_only_keep_what_google_returned():
    assert expand_keywords.rival_seeds(["www.ahrefs.com", "blog.ahrefs.com", "ab.io"], "en") == [
        ("ahrefs", "ahrefs alternatives"), ("ahrefs", "ahrefs vs")]
    assert expand_keywords.rival_seeds(["ahrefs.com"], "sw") == []       # 표에 없는 언어는 안 묻는다
    assert len(expand_keywords.rival_seeds([f"rival{i}.com" for i in range(9)], "ko")) \
        == expand_keywords.RIVAL_MAX * 2

    conn = db.connect()
    p = _project(conn, "w05_rival", locale="en-US")
    db.add_competitors(conn, p["id"], ["ahrefs.com", "reddit.com"], "manual")
    asked = []

    def fake_suggest(q, hl, gl):
        asked.append((q, hl, gl))
        return ["ahrefs alternatives", "ahrefs alternatives free", "best seo tools",
                "Ahrefs vs Semrush"] if "alternatives" in q else ["ahrefs vs", "ahrefs vs e.com"]

    orig = expand_keywords.suggest
    expand_keywords.suggest = fake_suggest
    try:
        dry = expand_keywords.rival_expand(conn, p, "en-US", "en", "us", 0, True)
        assert dry == [] and not asked                                    # dry-run 은 안 묻는다
        got = expand_keywords.rival_expand(conn, p, "en-US", "en", "us", 0, False)
    finally:
        expand_keywords.suggest = orig
    assert [q for q, _, _ in asked] == ["ahrefs alternatives", "ahrefs vs"], asked   # 플랫폼(reddit)은 경쟁사가 아니다
    assert got == [("ahrefs alternatives free", "en-US", "rival"),
                   ("Ahrefs vs Semrush", "en-US", "rival"),
                   ("ahrefs vs e.com", "en-US", "rival")], got
    conn.close()


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)} passed  ({HOME})")
