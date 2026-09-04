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
    a = {"url": URL, "checked_date": "2026-08-20", "title": "제목", "meta_description": "설명",
         "h1_json": '["제목"]', "h2_json": '["첫째", "둘째", "셋째"]', "words": 120,
         "schema_json": "[]", "canonical": None, "robots": None, "internal_links": 1,
         "external_links": 2, "images": 3, "images_no_alt": 2, "error": None}
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
    assert "## 바꾼 것" in t
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


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)} passed")
