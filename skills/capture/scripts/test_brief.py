#!/usr/bin/env python3
"""요청문(brief.py) 검사 — `python test_brief.py` (임시 폴더에서만 돈다).

보는 것: 종류마다 요청문이 **정말 다른 일을 시키는지**. 옛 요청문은 틀 한 벌이라
연락문에 "지금 이 페이지 상태"가 붙고 새 글 브리프에 "이 페이지에 있는 내용에서만"이
붙었다 — 그 실수를 종류별로 못 박는다.
  · 종류 14개 전부가 꼴(SHAPE) 하나로 가고, 빈 페이로드로도 안 죽는다
  · 연락문에는 페이지 상태가 없고, 정리(내부 경쟁)에는 페이지 표가 있고, 새 글에는
    "페이지 없음"과 붙여 넣기 칸이 있다
  · 근거 표가 실제 행에서 나온다 — 경쟁 도메인 순위, 챗봇 답변 발췌, 모바일 vs 데스크톱
  · 감사의 H2 목록이 요청문에 실린다 (없으면 AI 가 이미 있는 H2 를 또 제안한다)
  · 언어·길이 기준이 사이트 로케일을 따른다 — 영어 사이트에 "30자 이내"가 안 나간다
  · gather() 가 기회마다 brief 를 싣고 꼴 꼬리 한 벌을 같이 보낸다
"""
import os
import sys
import tempfile
from pathlib import Path

HOME = Path(tempfile.mkdtemp(prefix="seo-miner-brief-"))
os.environ["CAPTURE_HOME"] = str(HOME)
sys.path.insert(0, str(Path(__file__).parent))

import brief      # noqa: E402
import scoring    # noqa: E402

URL = "https://me.example/a"
URL2 = "https://me.example/b"


def _opp(kind, target, **kw):
    o = {"kind": kind, "target": target, "label": scoring.kind_label(kind),
         "reasoning": "근거 문장", "play": scoring.kind_play(kind, **kw)}
    o["gap_kind"] = kw.get("gap_kind")
    return o


def _audit(**over):
    # 새 감사 행의 꼴 그대로 — js_shell 이 있어야 "이 행이 모바일·언어 칸을 읽고
    # 왔다"가 된다(scoring._has_render_fields). 옛 행을 흉내 내려면 그 키들을 뺀다.
    a = {"url": URL, "checked_date": "2026-08-20", "title": "제목", "meta_description": "설명",
         "h1_json": '["제목"]', "h2_json": '["첫째", "둘째", "셋째"]', "words": 120,
         "schema_json": "[]", "canonical": None, "robots": None, "internal_links": 1,
         "external_links": 2, "images": 3, "images_no_alt": 2, "error": None,
         "status": 200, "js_shell": 0, "viewport": "width=device-width, initial-scale=1",
         "html_lang": "ko", "hreflang_json": "[]", "published": None, "modified": None}
    a.update(over)
    a["advice"] = scoring.page_advice(a, ["검색어"], domain="me.example")
    return a


def _pages(*urls):
    return [{"page": u, "impressions": 100 - i * 30, "clicks": 5 - i, "ctr": 5.0,
             "position": 8.0 + i} for i, u in enumerate(urls)]


def test_every_kind_builds_with_empty_payload():
    for k in scoring.ALL_KINDS:
        b = brief.build(_opp(k, "http://x/y" if k in ("index_blocked", "crawl_issue",
                                                        "backlink_broken") else "대상"), {})
        assert b["shape"] in brief.SHAPES, k
        assert b["body"].startswith(brief.INTRO_BY_KIND.get(k) or brief.SHAPES[b["shape"]]["intro"]), k
        assert "## 만들어 줄 것" in b["body"], k
        assert "## 이 상황에서 할 일" in b["body"], k
        assert "## 근거" not in b["body"], f"{k}: 근거가 없는데 근거 섹션을 만들었다"


def test_outreach_has_no_page_state():
    ctx = {"bl_intersect": [{"domain": "big.example", "rank": 810, "hits": 3,
                             "targets": "r1,r2,r3"}]}
    t = brief.text(_opp("backlink_prospect", "big.example"), ctx, "ko-KR")
    assert t.startswith(brief.SHAPES["outreach"]["intro"])
    assert "연락할 도메인: big.example" in t
    assert "지금 이 페이지 상태" not in t          # 남의 도메인은 고칠 페이지가 아니다
    assert "고칠 페이지" not in t and "검색어:" not in t
    assert "경쟁사 3곳: r1, r2, r3" in t
    assert "링크 부탁드립니다" in t and "120단어" in t
    assert "길이 기준" not in t                     # title 길이는 연락문과 무관하다


def test_consolidate_lists_every_page_and_skips_advice():
    audits = {URL: _audit(), URL2: _audit(url=URL2, title="다른 제목", h1_json='["다른"]')}
    ctx = {"query_pages": {"검색어": _pages(URL, URL2)}, "page_audits": audits}
    t = brief.text(_opp("cannibalization", "검색어"), ctx, "ko-KR")
    assert t.startswith(brief.SHAPES["consolidate"]["intro"])
    assert "정본 후보 페이지: " + URL in t
    assert f"| {URL} | 100 | 5 |" in t and f"| {URL2} | 70 | 4 |" in t
    assert "정본 후보입니다" in t
    assert "| " + URL2 + " | 다른 제목 | 다른 |" in t     # 페이지별 제목 표
    assert "## 진단" not in t                        # 정리는 페이지 안을 안 고친다
    assert "리다이렉트 사슬" in t


def test_new_content_says_no_page_and_offers_slot():
    ctx = {"kw_gap": [{"keyword": "없는 검색어", "domain": "rival.example", "position": 2,
                       "our_position": None, "volume": 2400, "kind": "missing"}]}
    t = brief.text(_opp("content_gap", "없는 검색어", gap_kind="missing"), ctx, "ko-KR")
    assert t.startswith(brief.SHAPES["new_content"]["intro"])
    assert "- 페이지: 없음" in t
    assert "| rival.example | 2위 | 없음 | 2,400 | missing |" in t
    assert "## 있으면 붙여 넣을 것" in t and "[여기에 붙여 넣기]" in t
    assert "지금 이 페이지 상태" not in t
    assert "본문 전체를 쓰지 않습니다" in t
    # 같은 종류라도 '밀린다'(weak)는 있는 페이지를 고친다
    ctx2 = {"query_pages": {"밀린 검색어": _pages(URL)}, "page_audits": {URL: _audit()},
            "kw_gap": [{"keyword": "밀린 검색어", "domain": "rival.example", "position": 3,
                        "our_position": 14, "volume": 900, "kind": "weak"}]}
    t2 = brief.text(_opp("content_gap", "밀린 검색어", gap_kind="weak"), ctx2, "ko-KR")
    assert t2.startswith(brief.SHAPES["fix_page"]["intro"])
    assert "| rival.example | 3위 | 14위 |" in t2
    assert "지금 이 페이지 상태 (2026-08-20 직접 확인)" in t2


def test_fix_page_carries_h2_list_and_advice():
    ctx = {"query_pages": {"검색어": _pages(URL)}, "page_audits": {URL: _audit()},
           "striking": [{"query": "검색어", "pos": 12.4, "imp": 1204, "clk": 8, "gap": 2.4,
                         "band": "page2"}], "gsc_date": "2026-08-25", "gsc_period": 28}
    t = brief.text(_opp("striking_distance", "검색어", band="page2"), ctx, "ko-KR")
    assert "H2 (3개): 첫째 / 둘째 / 셋째" in t
    assert "내부 링크 1개 · 외부 링크 2개 · 이미지 3개 (alt 없음 2개)" in t
    assert "## 진단 — 고쳐야 할 것" in t and "[title]" in t
    assert "구글 실적 2026-08-25, 최근 28일 평균: 평균 12.4위 · 노출 1,204 · 클릭 8 · 1페이지까지 2.4칸" in t
    assert "기간 평균 게재순위" in t
    assert "'바꾼 것' 표: 진단 항목 | 전 | 후" in t
    assert "title 30자 이내, meta description 80자 이내" in t


def test_fix_page_without_known_page_leaves_url_slot():
    t = brief.text(_opp("ctr_gap", "검색어"), {}, "ko-KR")
    assert "고칠 페이지를 직접 적어 주세요: [URL]" in t
    assert "## 지금 이 페이지 상태" not in t  # 모르는 페이지의 상태를 지어내지 않는다


def test_ai_gap_quotes_rival_answer_and_switches_shape_by_page():
    row = {"prompt": "무슨 도구가 좋아?", "checks": 6, "cited": 0, "mentioned": 1,
           "engines": "chatgpt,perplexity", "miss_domains": '["rival.example"]',
           "miss_answer": "첫 줄\n둘째 줄"}
    t = brief.text(_opp("ai_citation_gap", "무슨 도구가 좋아?"), {"ai_by_prompt": [row]}, "ko-KR")
    assert t.startswith(brief.SHAPES["new_content"]["intro"])     # 걸린 페이지 없음 → 새 글
    assert "질문 (챗봇에 실제로 물은 문장): 무슨 도구가 좋아?" in t
    assert "AI chatgpt,perplexity · 답변 6건 중 인용 0건, 이름만 1건" in t
    assert "대신 인용된 곳: rival.example" in t
    assert "  > 첫 줄\n  > 둘째 줄" in t
    ctx = {"ai_by_prompt": [row], "query_pages": {"무슨 도구가 좋아?": _pages(URL)}}
    t2 = brief.text(_opp("ai_citation_gap", "무슨 도구가 좋아?"), ctx, "ko-KR")
    assert t2.startswith(brief.SHAPES["fix_page"]["intro"])       # 걸린 페이지 있음 → 고친다


def test_technical_kinds_carry_google_verdict_and_device_numbers():
    ctx = {"index_issues": [{"url": "http://x/y", "bucket": "robots_blocked", "verdict": "FAIL",
                             "coverage_state": "Blocked by robots.txt", "detail": "robots 가 막음"}],
           "page_audits": {"http://x/y": _audit(url="http://x/y", robots="noindex")}}
    t = brief.text(_opp("index_blocked", "http://x/y"), ctx, "ko-KR")
    assert t.startswith(brief.SHAPES["technical"]["intro"])
    assert "구글 응답(URL 검사): Blocked by robots.txt · FAIL · 갈래: robots_blocked" in t
    assert "세부: robots 가 막음" in t
    assert "meta robots: noindex" in t
    assert "점검 표: 항목 | 확인하는 방법" in t
    d = {"query": "검색어", "mobile_pos": 18.0, "desktop_pos": 6.0, "dpos": 12.0,
         "mobile_imp": 900, "mobile_ctr": 0.4, "desktop_ctr": 3.1}
    t2 = brief.text(_opp("device_gap", "검색어"), {"device_gap": [d]}, "ko-KR")
    assert t2.startswith(brief.INTRO_BY_KIND["device_gap"])
    assert "모바일 18.0위 vs 데스크톱 6.0위 (12.0칸 차이) · 모바일 노출 900" in t2


def test_coverage_and_pseo_bring_sibling_keywords():
    ctx = {"cluster_keywords": {"가격": [{"keyword": "도구 가격", "volume": 320},
                                        {"keyword": "도구 요금제", "volume": None}]}}
    t = brief.text(_opp("coverage", "cluster:가격"), ctx, "ko-KR")
    assert "주제 (추적 키워드 묶음): 가격" in t
    assert "| 도구 가격 | 320 |" in t and "| 도구 요금제 | — |" in t
    qp = {"서울 세무사 추천": _pages(URL), "부산 세무사 추천": _pages(URL2),
          "전혀 다른 것": _pages(URL2)}
    t2 = brief.text(_opp("pseo_pattern", "서울 세무사 추천"), {"query_pages": qp}, "ko-KR")
    assert t2.startswith(brief.INTRO_BY_KIND["pseo_pattern"])
    assert "| 부산 세무사 추천 |" in t2 and "전혀 다른 것" not in t2


def test_backlink_and_crawl_rows_become_tables():
    ctx = {"bl_links": [{"url_from": "https://ref.example/post", "url_to": "http://x/gone",
                         "anchor": "앵커", "rank": 700, "dofollow": 1, "is_broken": 1},
                        {"url_from": "https://other/1", "url_to": "http://x/alive",
                         "anchor": "", "rank": 1, "dofollow": 1, "is_broken": 0}]}
    t = brief.text(_opp("backlink_broken", "http://x/gone"), ctx, "ko-KR")
    assert t.startswith(brief.INTRO_BY_KIND["backlink_broken"])
    assert "| https://ref.example/post | 앵커 | 700 | 예 |" in t and "other/1" not in t
    assert "결정 표" in t                          # 정리 꼴
    ctx2 = {"crawl": {"issues": [{"url": "http://x/404", "kind": "broken_internal",
                                  "severity": "bad", "detail": "404"}]}}
    t2 = brief.text(_opp("crawl_issue", "http://x/404"), ctx2, "ko-KR")
    assert "| broken_internal | bad | 404 |" in t2
    # 크롤 이슈의 주소는 '/path' 로도 온다 — 그걸 "아직 모르는 페이지"라 부르면 안 된다
    ctx3 = {"crawl": {"issues": [{"url": "/gone", "kind": "http_error", "severity": "bad",
                                  "detail": "404"}]}}
    t3 = brief.text(_opp("crawl_issue", "/gone"), ctx3, "ko-KR")
    assert "- 주소: /gone" in t3 and "아직 모릅니다" not in t3 and "[URL]" not in t3


def test_locale_sets_language_and_length_limits():
    ko, en, ja = (brief.tails(loc) for loc in ("ko-KR", "en-US", "ja-JP"))
    assert "한국어로 씁니다" in ko["fix_page"] and "title 30자 이내" in ko["fix_page"]
    assert "영어로 씁니다" in en["fix_page"] and "title 60자 이내, meta description 160자" in en["fix_page"]
    assert "일본어로 씁니다" in ja["new_content"] and "title 30자 이내" in ja["new_content"]
    for name in brief.SHAPE_NAMES:
        assert "## 답의 형식" in en[name] and "## 규칙" in en[name]
        assert ("길이 기준" in en[name]) == brief.SHAPES[name]["limits"], name
    # 처방 문구가 로케일을 박아 두면 영어 사이트에 "한글 30자"가 나간다 — 정본 쪽도 본다
    for k in scoring.KINDS:
        for v in (k.play.values() if "acts" not in k.play else [k.play]):
            for s in v.get("deliver", []):
                assert "한글" not in s, f"{k.name} 의 처방이 로케일을 박아 뒀다: {s}"


def test_tails_ask_for_a_self_contained_html_report():
    """답의 형식은 **자립형 HTML 한 장**이다 — 어디에 쓰고 어떻게 열고 무엇을 알려
    주는지가 꼴마다 다 있어야 한다. 한 자리라도 빠지면 AI 는 채팅 본문에 마크다운을
    늘어놓고 끝낸다(그게 옛 꼬리가 시키던 것이다).

    Mermaid 는 **그래프로 그릴 관계가 있는 꼴에만** 실린다. 한 벌로 실으면 목차도
    리다이렉트 지도도 없는 꼴(고치기·연락)이 억지 다이어그램을 그리고, 쓰지도 않을
    라이브러리를 CDN 에서 받는다.
    """
    t = brief.tails("ko-KR")
    for name in brief.SHAPE_NAMES:
        x = t[name]
        assert "%TEMP%" in x and f"seo-{name}-" in x and ".html" in x,             f"{name}: 꼬리가 파일을 어디에 무슨 이름으로 쓰는지 안 말한다"
        assert "절대경로" in x, f"{name}: 파일을 쓰고 경로를 안 알려 주면 사용자가 못 연다"
        assert "cdn.tailwindcss.com" in x, f"{name}: Tailwind CDN 을 안 짚는다"
        # 자간·등폭을 한글에 걸면 "아 직 안 딴 기 회"처럼 낱자가 흩어져 스캔이 안 된다.
        # 이 리포가 화면·랜딩·사이트 목록에서 세 번 저지른 실수라 꼴마다 못 박는다.
        assert "letter-spacing" in x and "등폭" in x,             f"{name}: 자간·등폭을 라틴에만 걸라는 줄이 없다"
        assert ("mermaid" in x.lower()) == bool(brief.SHAPES[name]["graph"]),             f"{name}: Mermaid 가 graph 유무와 어긋난다"
    assert brief.SHAPES["consolidate"]["graph"] and brief.SHAPES["technical"]["graph"]
    assert not brief.SHAPES["fix_page"]["graph"] and not brief.SHAPES["outreach"]["graph"]


def test_form_holds_deliverables_not_markdown_shape():
    """산출물 계약(무엇을 만드나)은 SHAPES[*]["form"], 그리는 법(어디에 쓰나·무엇으로
    그리나)은 HTML_FORM 한 벌이다. form 에 마크다운 형식 문장이 남으면 "`## 소제목`을
    답니다"와 "카드 하나로 그립니다"가 같은 요청문에 나란히 실려 두 벌이 된다.
    """
    for name, s in brief.SHAPES.items():
        joined = " ".join(s["form"])
        for stale in ("소제목", "코드 블록"):
            assert stale not in joined, f"{name} 의 form 에 옛 마크다운 형식 문장이 남았다: {stale!r}"
    # 그래도 산출물 계약 자체는 살아 있어야 한다 — 형식을 빼다 내용까지 비우면 안 된다
    assert "글자 수" in " ".join(brief.SHAPES["fix_page"]["form"])
    assert "301" in " ".join(brief.SHAPES["consolidate"]["form"])


def test_payload_shapes_are_one_set():
    p = brief.shapes_payload("ko-KR")
    assert set(p["tails"]) == set(p["intro"]) == set(p["slot"]) == set(p["labels"]) \
        == set(brief.SHAPE_NAMES)
    assert set(p["page_state"]) == {"fix_page", "technical", "consolidate"}
    assert p["by_tag"] == brief.DELIVER_BY_TAG and p["lang"] == "한국어"
    # 진단 tag 마다 산출물이 있어야 폴백 요청문이 빈손이 안 된다
    tags = {"title", "meta description", "H1", "본문", "구조화 데이터", "robots", "canonical",
            "이미지", "내부 링크", "가져오기"}
    assert tags <= set(brief.DELIVER_BY_TAG)


def test_gather_attaches_brief_to_every_opportunity():
    import db
    import dashboard
    import test_render
    test_render.fixture(HOME)
    conn = db.connect()
    try:
        p = db.get_project(conn, test_render.SITES[1])
        d = dashboard.gather(conn, p)
    finally:
        conn.close()
    assert d["opps"], "픽스처에 기회가 없다"
    for o in d["opps"]:
        b = o["brief"]
        assert b["shape"] in brief.SHAPES and b["body"].startswith(
            brief.INTRO_BY_KIND.get(o["kind"]) or brief.SHAPES[b["shape"]]["intro"]), o["kind"]
        assert "## 만들어 줄 것" in b["body"]
    sd = next(o for o in d["opps"] if o["kind"] == "striking_distance")
    assert sd["band"] == "page1" and sd["brief"]["shape"] == "fix_page"
    assert f"| https://{test_render.SITES[1]}.example/a | 120 |" in sd["brief"]["body"]
    assert set(d["brief"]["tails"]) == set(brief.SHAPE_NAMES)
    assert d["brief"]["locale"] == "ko-KR"


def test_static_html_caveat_when_page_is_a_js_shell():
    """정적 HTML 로 가져온 값을 사실처럼 말하지 않는다.

    Yoast·RankMath·AIOSEO 는 ld+json 을 자바스크립트로 넣는다. 그때 "구조화 데이터
    없음" 을 그대로 실으면, 있는 것을 또 만들라고 시킨다 — 이 리포가 가진 감사는
    requests 한 번이라 정확히 그 함정 위에 있다.
    """
    plain = brief.build(_opp("ctr_gap", "검색어"),
                        {"query_pages": {"검색어": _pages(URL)},
                         "page_audits": {URL: _audit(js_shell=0)}})["body"]
    assert "구조화 데이터: (없음) — 정적 HTML 기준" in plain, plain
    assert "자바스크립트로 그리는 것으로 보입니다" not in plain

    spa = brief.build(_opp("ctr_gap", "검색어"),
                      {"query_pages": {"검색어": _pages(URL)},
                       "page_audits": {URL: _audit(js_shell=1, words=20)}})["body"]
    assert "자바스크립트로 그리는 것으로 보입니다" in spa, spa
    # 판정(scoring.page_advice)도 같이 바뀐다 — "없다" 가 아니라 "확인부터 하라"
    assert "[구조화 데이터] 지금: 정적 HTML 에는 ld+json 이 없습니다" in spa, spa
    assert "리치 결과 테스트" in spa, "확인할 방법을 안 알려준다"

    # 옛 감사 행(칸이 통째로 NULL)에는 없는 문제를 만들지 않는다
    old_row = _audit()
    for k in ("js_shell", "viewport", "html_lang", "hreflang_json", "published", "modified"):
        old_row.pop(k, None)
    old_row["advice"] = scoring.page_advice(old_row, ["검색어"], domain="me.example")
    body = brief.build(_opp("ctr_gap", "검색어"),
                       {"query_pages": {"검색어": _pages(URL)},
                        "page_audits": {URL: old_row}})["body"]
    assert "뷰포트" not in body and "정적 HTML 기준" not in body, body


def test_page_state_carries_status_viewport_language_and_dates():
    """"지금 값 → 고칠 값" 을 시키려면 지금 값이 있어야 한다."""
    a = _audit(status=200, viewport="width=device-width, initial-scale=1",
               html_lang="ko", published="2024-03-02", modified="2024-05-01",
               hreflang_json='[["ko", "https://me.example/a"], ["en", "https://me.example/en/a"]]')
    body = brief.build(_opp("index_blocked", URL), {"page_audits": {URL: a}})["body"]
    assert "- HTTP 상태: 200" in body, body
    assert "뷰포트: width=device-width" in body, body
    assert "html lang: ko · hreflang ko, en" in body, body
    # 우리가 점검한 날과 글이 쓰인 날은 다른 사실이다
    assert "글의 날짜: 발행 2024-03-02 · 수정 2024-05-01" in body, body


def test_site_wide_facts_reach_the_page_brief():
    """한 장만 봐서는 모르는 것 — 제목 중복, 들어오는 내부 링크, robots·사이트맵."""
    ctx = {"query_pages": {"검색어": _pages(URL)},
           "page_audits": {URL: _audit()},
           "crawl": {"run": {"id": 1},
                     "issues": [{"kind": "dup_title", "url": URL,
                                 "detail": "같은 제목을 쓰는 페이지 3개: 제목"},
                                {"kind": "thin_content", "url": URL, "detail": "본문 80단어"},
                                {"kind": "dup_title", "url": URL2, "detail": "남의 행"}]},
           "crawl_inlinks": {URL: [{"from": URL2, "anchor": "여기"}]},
           "site_probe": {URL: {"robots": "Disallow: /a", "in_sitemap": False}}}
    ctx["crawl_kinds"] = {"dup_title": ["제목 중복", "왜 문제인지"]}
    body = brief.build(_opp("ctr_gap", "검색어"), ctx)["body"]
    assert "같은 제목을 쓰는 페이지 3개" in body, body
    # 갈래 이름은 화면과 같은 한국어여야 한다 — 정본은 collect_crawl.ISSUE_KIND
    assert "| 제목 중복 |" in body and "dup_title |" not in body, body
    # 페이지 한 장 진단이 이미 말하는 것을 두 번 싣지 않는다
    assert "thin_content" not in body, body
    assert "남의 행" not in body, "다른 주소의 크롤 이슈가 새어 들어온다"
    assert "들어오는** 내부 링크 1개" in body and "| 여기 |" in body, body
    # 같은 요청문 안에 두 방향의 링크 수가 있다 — 방향을 안 적으면 한 값의 두 표현처럼 읽힌다
    # 진단 문장이 아니라 상태 줄에서 본다 — 진단에도 같은 말이 있어 그것만 보면
    # 늘 참인 검사가 된다
    assert "- 내보내는 내부 링크 1개 · 외부 링크 2개" in body, body
    assert "`Disallow: /a`" in body, body
    assert "사이트맵에 이 주소가 없습니다" in body, body

    # 크롤이 안 본 주소를 "고아" 라고 부르지 않는다 — None 과 [] 는 다르다
    unseen = brief.build(_opp("ctr_gap", "검색어"),
                         {**ctx, "crawl_inlinks": {}})["body"]
    assert "고아 페이지" not in unseen, unseen
    orphan = brief.build(_opp("ctr_gap", "검색어"),
                         {**ctx, "crawl_inlinks": {URL: []}})["body"]
    assert "고아 페이지" in orphan, orphan


def test_serp_top_replaces_the_paste_by_hand_step():
    """상위 페이지 제목은 우리가 이미 사 온 것이다 — 사람에게 붙여 넣으라고 안 시킨다."""
    ctx = {"query_pages": {"검색어": _pages(URL)},
           "serp_top": {"검색어": [
               {"position": 1, "url": "https://rival.example/x", "title": "경쟁 글", "is_own": 0},
               {"position": 4, "url": URL, "title": "내 글", "is_own": 1}]}}
    body = brief.build(_opp("striking_distance", "검색어"), ctx)["body"]
    assert "## 지금 이 검색어의 검색결과 상위" in body, body
    assert "| 1위 | 경쟁 글 | https://rival.example/x |" in body, body
    assert "| 4위 (내 페이지) | 내 글 |" in body, body
    # 상위와 비교할 일이 아닌 꼴(주소 정리)에는 안 붙는다
    assert "검색결과 상위" not in brief.build(_opp("cannibalization", "검색어"), ctx)["body"]
    # 수집이 안 됐으면 사람이 붙여 넣는 칸이 그대로 남는다
    assert "## 지금 이 검색어의 검색결과 상위" not in brief.build(
        _opp("striking_distance", "검색어"), {"query_pages": {"검색어": _pages(URL)}})["body"]


def _vitals(**over):
    r = {"strategy": "mobile", "error": None, "origin_fallback": 0, "field_verdict": "SLOW",
         "field_lcp_ms": 4200, "field_inp_ms": 310, "field_cls": 0.24, "field_ttfb_ms": 900,
         "lab_score": 42, "lab_lcp_ms": 4310, "lab_cls": 0.24, "lab_tbt_ms": 640}
    r.update(over)
    return r


def test_device_gap_gets_real_speed_numbers():
    """기기 격차 요청문은 "위 근거에 없는 것은 짐작하지 않습니다" 라고 못 박는다.
    그 근거가 순위 차이 한 줄뿐이면 답이 나올 수 없다."""
    ctx = {"query_pages": {"검색어": _pages(URL)},
           "device_gap": [{"query": "검색어", "mobile_pos": 12.0, "desktop_pos": 4.0,
                           "dpos": 8.0, "mobile_imp": 900, "mobile_ctr": 0.4,
                           "desktop_ctr": 3.1}],
           "vitals_date": "2026-09-09",
           "vitals": {URL: {"mobile": _vitals(),
                            "desktop": _vitals(strategy="desktop", field_lcp_ms=1800,
                                               field_inp_ms=90, field_cls=0.02,
                                               lab_score=93, lab_lcp_ms=1900,
                                               lab_cls=0.02, lab_tbt_ms=40)}}}
    body = brief.text(_opp("device_gap", "검색어"), ctx, "ko-KR")
    assert "기기별 속도 (2026-09-09" in body, body
    assert "| LCP (현장) | 4.2초 | 1.8초 | 2.5초 |" in body, body
    assert "| INP (현장) | 310ms | 90ms | 200ms |" in body, body
    # 페이지 상태에도 같은 값이 실리고, 판정은 scoring 한 곳에서 온다
    assert "모바일 현장(이 페이지의 실제 사용자 28일치)" in body, body
    assert "[속도] 지금: 모바일 LCP 4.2초" in body, body
    # 속도를 안 쟀으면 아무 줄도 안 만든다 — 없는 것을 있는 척하지 않는다
    assert "기기별 속도" not in brief.text(
        _opp("device_gap", "검색어"), {k: v for k, v in ctx.items()
                                       if k not in ("vitals", "vitals_date")}, "ko-KR")


def test_origin_fallback_is_not_called_this_pages_speed():
    """사이트 전체 값을 이 페이지의 값이라고 말하면, 멀쩡한 페이지에 없는 문제를 만든다."""
    ctx = {"query_pages": {"검색어": _pages(URL)},
           "vitals_date": "2026-09-09",
           "vitals": {URL: {"mobile": _vitals(origin_fallback=1)}}}
    body = brief.build(_opp("ctr_gap", "검색어"), ctx)["body"]
    assert "모바일 현장(사이트 전체 값)" in body, body
    assert "사이트 전체(이 페이지만의 실제 사용자 값은 표본이 모자랍니다)" in body, body


def test_technical_findings_do_not_pad_a_content_todo_list():
    """열세 개를 늘어놓고 세 개만 시키는 글이 되지 않게.

    모바일·언어·hreflang·속도는 이 페이지의 진짜 문제지만 "있는 페이지 고치기" 의
    일이 아니다. 같은 번호 목록에 섞으면, 요청문이 시키지도 않을 것을 할 일처럼
    적어 두고 규칙으로는 "요청하지 않은 것은 손대지 않습니다" 라고 말하게 된다.
    """
    a = _audit(viewport=None, html_lang=None)
    a["advice"] = scoring.page_advice(a, ["검색어"], domain="me.example")
    ctx = {"query_pages": {"검색어": _pages(URL)}, "page_audits": {URL: a},
           "vitals_date": "2026-09-09", "vitals": {URL: {"mobile": _vitals()}}}

    fix = brief.build(_opp("ctr_gap", "검색어"), ctx)["body"]
    todo = fix.split("## 진단 — 고쳐야 할 것")[1].split("##")[0]
    assert "[title]" in todo and "[모바일]" not in todo and "[속도]" not in todo, todo
    aside = fix.split("## 이 페이지에서 같이 눈에 띈 것")[1].split("##")[0]
    assert "[모바일]" in aside and "[속도]" in aside, aside
    assert "이번 요청문에서는 손대지 않습니다" in aside, aside

    # 기술 점검 요청문에서는 안 가른다 — 거기서는 그게 본업이다
    tech = brief.build(_opp("index_blocked", URL), {**ctx, "page_audits": {URL: a}})["body"]
    assert "같이 눈에 띈 것" not in tech, tech
    assert "[모바일] 지금:" in tech, tech


def test_serp_table_replaces_the_paste_ask_instead_of_doubling_it():
    """상위 제목을 위에서 줘 놓고 아래에서 또 "제목을 붙여 넣으세요" 라고 하지 않는다."""
    base = {"query_pages": {"검색어": _pages(URL)}}
    plain = brief.build(_opp("striking_distance", "검색어"), base)["body"]
    assert brief.SHAPES["fix_page"]["slot"] in plain, plain

    withtop = brief.build(_opp("striking_distance", "검색어"), {**base, "serp_top": {"검색어": [
        {"position": 1, "url": "https://rival.example/x", "title": "경쟁 글", "is_own": 0}]}})["body"]
    assert brief.SHAPES["fix_page"]["slot"] not in withtop, withtop
    assert "제목은 이미 위에 있습니다" in withtop, withtop


def test_ai_bot_block_is_its_own_request_and_warns_the_citation_brief():
    """막힌 크롤러는 콘텐츠 문제가 아니다.

    ClaudeBot 이 robots.txt 로 막혀 있으면 그 엔진에서는 무엇을 써도 인용되지
    않는다. 그 상태에서 인용 공백 요청문이 "이 내용을 채우세요" 라고만 하면 두
    기회가 서로 모순되는 말을 한다.
    """
    ctx = {"ai_bots": [{"bot": "ClaudeBot", "rule": "Disallow: /"},
                       {"bot": "GPTBot", "rule": None}]}
    b = brief.build(_opp("ai_bot_blocked", "ClaudeBot"), ctx)
    assert b["shape"] == "technical", b["shape"]
    body = b["body"]
    assert "막힌 AI 크롤러 (robots.txt 의 User-agent): ClaudeBot" in body, body
    # 막힌 것만 주면 "이것만 열면 되나" 로 읽힌다 — 허용도 같은 표에 놓는다
    assert "| ClaudeBot | 차단 | Disallow: / |" in body, body
    assert "| GPTBot | 허용 |" in body, body
    assert "여는 것이 늘 정답은 아닙니다" in body, "의도적 차단을 되돌리라고 시킨다"
    # 글을 고치라고 하지 않는다
    assert "막힌 채로는 고쳐도 안 읽힙니다" in body, body

    # 인용 공백 요청문이 같은 사실을 먼저 말한다
    gap = brief.build(_opp("ai_citation_gap", "질문"), {
        **ctx, "ai_by_prompt": [{"prompt": "질문", "engines": "chatgpt", "checks": 4,
                                 "cited": 0, "mentioned": 1}]})["body"]
    assert "robots.txt 가 ClaudeBot 를 막고 있습니다" in gap, gap
    # 안 막혀 있으면 그 줄이 없다 — 없는 문제를 만들지 않는다
    clean = brief.build(_opp("ai_citation_gap", "질문"), {
        "ai_bots": [{"bot": "GPTBot", "rule": None}],
        "ai_by_prompt": [{"prompt": "질문", "engines": "chatgpt", "checks": 4,
                          "cited": 0, "mentioned": 1}]})["body"]
    assert "먼저 볼 것" not in clean, clean


def test_aio_brief_follows_rank_and_never_asks_for_faq_markup():
    """구글 AI 요약은 AI 전용 마크업이 필요 없고 출처를 순위 시스템에서 고른다.

    예전 처방은 순위와 무관하게 "직답 블록 + Article·FAQ 구조화 데이터"였다 — 40위인
    검색어에 필요한 건 직답 블록이 아니라 순위다. 1페이지 안/밖으로 처방이 갈리고,
    어느 쪽도 FAQ 구조화 데이터를 산출물로 시키지 않는다.
    """
    ctx = {"query_pages": {"검색어": _pages(URL)}}
    inside = brief.build(_opp("aio_exposure", "검색어", band="page1"), ctx)["body"]
    outside = brief.build(_opp("aio_exposure", "검색어", band="beyond"), ctx)["body"]
    assert "이미 1페이지 안인데" in inside and "E-E-A-T" in inside, inside
    assert "순위가 먼저입니다" in outside and "1페이지에 들기 위해" in outside, outside
    assert "순위가 먼저입니다" not in inside and "E-E-A-T" not in outside
    for body in (inside, outside):
        want = body.split("## 만들어 줄 것")[1].split("##")[0]
        assert "FAQ" not in want and "구조화 데이터" not in want, want
        assert "직답 블록" not in want, want
        # 조각내기·AI 전용 마크업을 하지 말라고 말한다 — 안 하던 걸 시키지 않는 것만으론
        # 모자라다(직답 블록을 만들라던 요청문이 이미 나가 있다)
        assert "AI 전용" in body, body
    for b in scoring.AIO_BANDS:
        p = scoring.kind_play("aio_exposure", band=b)
        assert not any("FAQ" in x for x in p["acts"] + p["deliver"]), (b, p)


def test_aio_brief_names_who_google_cited_instead():
    """챗봇 인용 공백 요청문은 "누가 대신 인용됐나"를 말하는데 구글 AI 요약은 못 했다 —
    수집기가 인용 도메인을 0/1 로 접고 버렸기 때문이다. 이제 그 목록을 싣는다."""
    row = {"keyword": "검색어", "pos": None, "url": None, "features": ["ai_overview"],
           "aio": 1, "aio_cited": 0, "aio_domains": ["rival.example", "wiki.example"]}
    # 화면용 ranks(30개로 잘림)에 없어도 aio_gap_ranks 에서 찾는다
    body = brief.build(_opp("aio_exposure", "검색어"),
                       {"ranks": [], "aio_gap_ranks": {"검색어": row}})["body"]
    assert "구글 AI 요약이 대신 인용한 곳: rival.example, wiki.example" in body, body
    assert "순위 없음" in body, body
    # 옛 조회(목록을 안 남기던 때)는 아무 말도 안 한다 — "없다"고 지어내지 않는다
    old = brief.build(_opp("aio_exposure", "검색어"),
                      {"aio_gap_ranks": {"검색어": {**row, "aio_domains": None}}})["body"]
    assert "대신 인용한 곳" not in old and "뽑지 못했습니다" not in old, old
    # 요약은 떴는데 인용을 못 뽑았다([])는 그렇다고 말한다
    empty = brief.build(_opp("aio_exposure", "검색어"),
                        {"aio_gap_ranks": {"검색어": {**row, "aio_domains": []}}})["body"]
    assert "뽑지 못했습니다" in empty and "대신 인용한 곳:" not in empty, empty


def test_fanout_questions_reach_fix_new_and_aio_briefs():
    """AI 는 사용자가 친 질문 하나가 아니라 관련 질문 묶음으로 찾는다 — 그 묶음이 구글이
    같이 보여 준 질문·연관 검색어다. 수집기는 내내 받아 왔는데 키워드 후보로만 넣고
    어느 검색어에서 나왔는지를 버렸다."""
    fan = {"검색어": [{"kind": "paa", "text": "비용은 얼마인가요"},
                      {"kind": "paa", "text": "부작용이 있나요"},
                      {"kind": "related", "text": "검색어 후기"}]}
    head = "## 함께 답해야 할 질문 (구글이 같이 보여 준 것)"
    fix = brief.build(_opp("striking_distance", "검색어"),
                      {"query_pages": {"검색어": _pages(URL)}, "serp_fanout": fan})["body"]
    assert head in fix and "  - 비용은 얼마인가요" in fix, fix
    assert "- 연관 검색어: 검색어 후기" in fix, fix
    new = brief.build(_opp("content_gap", "검색어", gap_kind="missing"), {"serp_fanout": fan})
    assert new["shape"] == "new_content" and head in new["body"], new["body"]
    aio = brief.build(_opp("aio_exposure", "검색어"), {"serp_fanout": fan})["body"]
    assert head in aio and "부작용이 있나요" in aio, aio
    # 상위와 견줄 일이 아닌 꼴에는 안 붙는다
    assert head not in brief.build(_opp("cannibalization", "검색어"), {"serp_fanout": fan})["body"]
    # 수집이 안 됐으면(키 없음·빈 목록) 블록이 없다 — 없는 것을 있는 척하지 않는다
    for ctx in ({}, {"serp_fanout": {}}, {"serp_fanout": {"검색어": []}},
                {"serp_fanout": {"다른 검색어": fan["검색어"]}}):
        assert head not in brief.build(_opp("striking_distance", "검색어"), ctx)["body"], ctx


def test_trust_signals_are_asked_for_but_never_invented():
    """E-E-A-T — 저자·출처·갱신일은 요구하되, 이름·자격은 지어내지 않게 못 박는다."""
    tails = brief.tails("ko-KR")
    for shape in ("fix_page", "new_content"):
        # 답이 HTML 카드가 된 뒤로 소제목 틀은 form 이 갖지 않는다 — 요구 자체만 본다
        assert "신뢰 신호" in tails[shape], shape
        assert "[저자]" in tails[shape], shape
        assert "지어내지 않습니다" in tails[shape], shape
    # 고칠 페이지가 없는 일(연락문)에는 안 붙는다
    assert "신뢰 신호" not in tails["outreach"]


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)} passed")
