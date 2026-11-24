#!/usr/bin/env python3
"""페이지 기계 점검·구조화 데이터 값 검사 — 수집기가 센 것(collect_page)과 그 판정(scoring).

네트워크를 타지 않는다: HTML 글자를 collect_page.audit_html 에 넣고, 그 행을 그대로
scoring.page_advice·page_info 에 넣는다(수집 → 판정이 한 줄로 이어지는지까지 본다).
"""
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import collect_page  # noqa: E402
import db  # noqa: E402
import page_lang  # noqa: E402
import schema_values  # noqa: E402
import scoring  # noqa: E402

U = "https://me.example/guide/milia"


def _audit(body: str, *, head: str = "", lang: str = "en", url: str = U, headers=None) -> dict:
    html = (f'<html lang="{lang}"><head><title>Milia removal cost and recovery guide</title>'
            f'<meta name="description" content="How much milia removal costs, how long recovery '
            f'takes, and when to see a dermatologist instead."><meta name="viewport" '
            f'content="width=device-width, initial-scale=1">{head}</head><body>{body}</body></html>')
    return collect_page.audit_html(url, html, 200, headers)


def _sig(a: dict) -> dict:
    return json.loads(a["signals_json"])


def _advice(a: dict, **kw) -> list[dict]:
    return scoring.page_advice(a, [], domain="me.example", **kw)


def _tags(a: dict, **kw) -> list[str]:
    return [x["tag"] for x in _advice(a, **kw)]


def _said(a: dict, tag: str, **kw) -> str:
    return " | ".join(x["now"] for x in _advice(a, **kw) if x["tag"] == tag)


LONG = "word " * 90
MID = "word " * 20


# ── 링크·혼합 콘텐츠·제목 단계·메타·채움 글·숨긴 글 ───────────────────────────
def test_links_mixed_headings_meta_are_counted_then_judged():
    a = _audit(f"""
      <nav><h4>Menu</h4><a href="/x"><svg></svg></a></nav>
      <main><h1>Milia</h1><h2>Cost</h2><h4>Skipped</h4><h3>Fine</h3><h2>Again</h2><h3>Ok</h3>
      <p>{MID}</p>
      <a href="http://me.example/old">an http link to my own page</a>
      <a href="https://me.example/a"></a>
      <a href="https://me.example/icon"><img src="https://me.example/i.png" alt="Pricing table"></a>
      <a href="/b" aria-label="Open the booking form"><i></i></a>
      <a href="/c">click here</a><a href="/d">Read more »</a><a href="/e">here</a>
      <a href="/f">Milia removal price list</a>
      <img src="http://cdn.example/a.png" alt="Milia before and after">
      <script src="http://cdn.example/a.js"></script>
      <link rel="stylesheet" href="http://cdn.example/a.css">
      <p>Lorem ipsum dolor sit amet, consectetur adipiscing elit sed do.</p></main>""",
               head='<meta http-equiv="refresh" content="0; url=/new"><meta charset="euc-kr">',
               headers={"Content-Type": "text/html; charset=UTF-8"})
    s = _sig(a)
    assert s["h"]["skips"] == [["H2", "H4", 1]], s["h"]              # 메뉴의 H4 는 본문 제목이 아니다
    assert (s["h"]["h3"], s["h"]["h4"]) == (2, 1), s["h"]
    assert s["links"]["http"] == 1 and s["links"]["mixed"] == 3, s["links"]
    # 글자도 이름도 없는 링크만 — 그림의 alt·aria-label 이 있으면 이름이 있는 링크다
    assert s["links"]["empty"] == 2, s["links"]                      # 메뉴의 svg 링크 + 빈 링크
    assert s["links"]["generic"] == 3, s["links"]
    assert s["meta"]["refresh"].startswith("0;") and s["meta"]["charset"] == "euc-kr"
    assert s["meta"]["charset_http"] == "utf-8" and s["text"]["lorem"] == 1, s
    tags = _tags(a)
    for t in ("혼합 콘텐츠", "메타 태그", "채움 글", "제목 구조", "링크 품질"):
        assert t in tags, (t, tags)
    assert "H2→H4 1곳" in _said(a, "제목 구조")
    said = _said(a, "링크 품질")
    assert "http:// 로 거는 링크 1개" in said and "없는 링크 2개" in said and "3개" in said, said
    assert "euc-kr" in _said(a, "메타 태그") and "meta refresh" in _said(a, "메타 태그")
    # 멀쩡한 페이지에는 아무것도 안 선다
    clean = _audit(f"<main><h1>Milia</h1><h2>Cost</h2><h3>Detail</h3><p>{MID}</p>"
                   '<a href="/f">Milia removal price list</a><a href="/g">more</a></main>',
                   head='<meta charset="utf-8">', headers={"Content-Type": "text/html; charset=utf-8"})
    new = {"혼합 콘텐츠", "메타 태그", "채움 글", "제목 구조", "링크 품질", "숨긴 텍스트"}
    assert not new & set(_tags(clean)), _tags(clean)
    assert _sig(clean)["links"]["generic"] == 1, "일반 앵커 한두 개는 말하지 않는다(GENERIC_ANCHOR_MIN)"


def test_generic_anchor_uses_the_page_language_table():
    body = '<main><h1>h</h1><a href="/a">자세히 보기</a><a href="/b">여기</a><a href="/c">더보기</a></main>'
    assert _sig(_audit(body, lang="ko"))["links"]["generic"] == 3
    assert _sig(_audit(body, lang="en"))["links"]["generic"] == 0, "영어 표로 한국어 앵커를 센다"
    # 표에 없는 언어는 '못 셌다'(None) — 0 이 아니고, 진단도 안 선다
    sv = _audit(body.replace("자세히 보기", "click here"), lang="sv")
    assert _sig(sv)["links"]["generic"] is None and "링크 품질" not in _tags(sv)


def test_hidden_text_is_counted_apart_from_collapsed_accordion():
    spam = "keyword " * 150
    a = _audit(f"""<main><h1>h</h1><p>{MID}</p>
      <div style="display: none">{spam}</div><span hidden>{MID}</span>
      <input type="hidden" hidden><p>{MID}</p>
      <details><summary>Question one here</summary><p>{MID}</p></details>
      <details open><summary>Open one</summary><p>{MID}</p></details></main>
      <nav><ul style="display:none"><li>{spam}</li></ul></nav>""")
    t = _sig(a)["text"]
    assert t["hidden_words"] == 170, t                 # 메뉴 안의 숨긴 목록은 안 센다(펼침 메뉴)
    assert t["fold_words"] == 20, t                    # 닫힌 <details> 의 답만 — summary 는 보인다
    assert "170단어" in _said(a, "숨긴 텍스트")
    # <input hidden> 은 닫는 태그가 없다 — 뒤따르는 본문을 숨긴 글로 만들지 않는다
    assert t["main_words"] == 1 + 20 + 20 + 3 + 20 + 2 + 20, t
    few = _audit(f'<main><h1>h</h1><p>{MID}</p><div hidden>{MID}</div></main>')
    assert "숨긴 텍스트" not in _tags(few), "말풍선 하나 크기의 숨긴 글을 지적한다"
    assert any("접힌 아코디언" in x for x in scoring.page_info(a)), scoring.page_info(a)


def test_main_content_excludes_chrome_and_flags_thin_body():
    chrome = "menu " * 400
    a = _audit(f"<nav>{chrome}</nav><main><h1>Milia</h1><p>{'word ' * 60}</p></main>"
               f"<footer>{chrome}</footer>")
    assert a["words"] > scoring.THIN_WORDS and _sig(a)["text"]["main_words"] == 61, _sig(a)["text"]
    assert "본문은 약 61단어" in _said(a, "본문"), _said(a, "본문")
    # <main> 이 없으면 틀 밖 전체가 본문이다
    b = _audit(f"<nav>{chrome}</nav><div><h1>Milia</h1><p>{'word ' * 60}</p></div>")
    assert _sig(b)["text"]["main_words"] == 61
    # 띄어쓰기로 낱말을 못 세는 언어는 낱말 수로 판정하지 않는다
    ja = _audit(f"<nav>{chrome}</nav><main><h1>稗粒腫</h1><p>{'これは本文です。' * 80}</p></main>", lang="ja")
    assert "본문" not in _tags(ja), _advice(ja)


# ── robots — 헤더·봇 이름 meta·스니펫 지시 ────────────────────────────────────
def test_x_robots_header_and_snippet_directives():
    body = f"<main><h1>h</h1><p>{MID}</p></main>"
    hdr = _audit(body, headers={"X-Robots-Tag": "noindex, nofollow"})
    assert scoring.robots_directives(hdr)["noindex"] == "X-Robots-Tag 헤더"
    said = _said(hdr, "robots")
    assert "X-Robots-Tag 헤더: noindex, nofollow" in said, said
    assert [x["level"] for x in _advice(hdr) if x["tag"] == "robots"] == ["bad"]
    # 다른 봇에게만 건 noindex 는 구글 검색 얘기가 아니다. googlebot 것은 그렇다
    other = _audit(body, headers={"X-Robots-Tag": "otherbot: noindex"})
    assert "robots" not in _tags(other), _advice(other)
    gb = _audit(body, headers={"X-Robots-Tag": "googlebot: noindex, otherbot: nofollow"})
    assert scoring.robots_directives(gb)["noindex"] == "X-Robots-Tag 헤더"
    meta_bot = _audit(body, head='<meta name="googlebot" content="noindex">')
    assert "meta googlebot: noindex" in _said(meta_bot, "robots")
    # meta robots 의 noindex 는 예전 줄이 말한다 — 같은 말을 두 번 하지 않는다
    both = _audit(body, head='<meta name="robots" content="noindex">',
                  headers={"X-Robots-Tag": "noindex"})
    assert len([x for x in _advice(both) if x["tag"] == "robots"]) == 1, _advice(both)
    # 스니펫을 없애는 지시 — nosnippet 과 max-snippet:0 은 같은 말이다
    for head in ('<meta name="robots" content="nosnippet">',
                 '<meta name="robots" content="index, max-snippet:0">',
                 '<meta name="googlebot" content="max-snippet: 0">'):
        a = _audit(body, head=head)
        assert "설명 글이 안 뜹니다" in _said(a, "robots"), (head, _advice(a))
        assert [x["level"] for x in _advice(a) if x["tag"] == "robots"] == ["warn"]
    ok = _audit(body, head='<meta name="robots" content="max-snippet:-1, max-image-preview:large">')
    assert "robots" not in _tags(ok), _advice(ok)
    # unavailable_after 의 날짜에는 ':' 이 있다 — 봇 이름으로 읽지 않는다
    assert scoring._robots_tokens("unavailable_after: 2027-01-01T10:00:00Z, noindex") \
        == [("", "unavailable_after:2027-01-01t10:00:00z"), ("", "noindex")]
    # data-nosnippet — span·div·section 에서만 통한다
    dn = _audit(f'<main><h1>h</h1><p data-nosnippet>{MID}</p><span data-nosnippet>x</span>'
                f'<section data-nosnippet><p>{MID}</p></section></main>')
    assert _sig(dn)["snippet"] == {"data_nosnippet": 2, "data_nosnippet_invalid": 1}
    assert "아닌 태그에 단 곳 1개" in _said(dn, "robots")
    assert any("data-nosnippet 으로" in x and "2곳" in x for x in scoring.page_info(dn))


# ── canonical 대상을 한 번 더 열기 ────────────────────────────────────────────
def test_canonical_target_is_reopened_and_judged():
    def row(url, canonical):
        a = _audit("<main><h1>h</h1></main>", head=f'<link rel="canonical" href="{canonical}">', url=url)
        return a

    pages = {"https://me.example/gone": {"url": "https://me.example/gone", "status": 404,
                                         "error": "HTTP 404"},
             "https://me.example/moved": {**row("https://me.example/moved", "https://me.example/new"),
                                          "final_url": "https://me.example/new"},
             "https://me.example/chain": row("https://me.example/chain", "https://me.example/end")}
    opened = []

    def get(u):
        opened.append(u)
        return pages[u]

    rows = [row("https://me.example/a", "https://me.example/gone"),
            row("https://me.example/b", "https://me.example/moved"),
            row("https://me.example/c", "https://me.example/chain"),
            row("https://me.example/d", "https://me.example/e"),        # 서로 가리킨다 — 이번에 본 행
            row("https://me.example/e", "https://me.example/d"),
            row("https://me.example/self", "https://me.example/self"),  # 자기 자신 — 안 연다
            row("https://me.example/x", "https://other.example/x")]     # 남의 도메인 — 안 연다
    assert collect_page.check_canonicals(rows, "me.example", get) == 3
    assert opened == ["https://me.example/gone", "https://me.example/moved", "https://me.example/chain"], \
        f"이미 본 주소·자기 자신·남의 도메인까지 연다: {opened}"
    said = {r["url"].rsplit("/", 1)[1]: [(x["level"], x["now"]) for x in scoring.page_advice(
        r, [], domain="me.example") if x["tag"] == "canonical"] for r in rows}
    assert said["a"][-1][0] == "bad" and "HTTP 404" in said["a"][-1][1], said["a"]
    assert said["b"][-1][0] == "warn" and "리다이렉트" in said["b"][-1][1], said["b"]
    assert "또 다른 주소(https://me.example/end)" in said["c"][-1][1], said["c"]
    assert said["d"][-1][0] == "bad" and "고리" in said["d"][-1][1], said["d"]
    assert said["self"] == [], said["self"]
    assert "canonical_check_json" not in rows[-1] and "canonical_check_json" not in rows[-2]
    # 못 연 것(막힘)은 말하지 않는다 — 모르는 것이다
    blocked = [row("https://me.example/a", "https://me.example/b")]
    collect_page.check_canonicals(blocked, "me.example", lambda u: {"url": u, "status": 403,
                                                                     "error": "HTTP 403"})
    assert len([x for x in scoring.page_advice(blocked[0], [], domain="me.example")
                if x["tag"] == "canonical"]) == 1, "막힌 대상을 없는 페이지로 읽는다"
    # 상한 — 대상이 많아도 CANON_MAX 까지만 연다
    many = [row(f"https://me.example/p{i}", f"https://me.example/t{i}") for i in range(30)]
    n = collect_page.check_canonicals(many, "me.example", lambda u: {"url": u, "status": 200})
    assert n == collect_page.CANON_MAX, n


# ── 설명 템플릿 ──────────────────────────────────────────────────────────────
def test_desc_template_kind_is_unicode_aware():
    k = scoring.desc_template_kind
    assert k("Milia Removal Guide | Acme Clinic", "Milia Removal Guide | Acme Clinic") == "same_as_title"
    assert k("milia removal guide - acme clinic.", "Milia Removal Guide | Acme Clinic") == "same_as_title"
    assert k("Milia Removal Guide | Acme Clinic. Learn about costs and recovery.",
             "Milia Removal Guide | Acme Clinic") == "starts_with_title"
    # 한글·가나·키릴도 글자다 — 지우고 견주면 아무것도 못 잡는다
    assert k("비립종 제거 비용과 회복 기간 | 우리피부과", "비립종 제거 비용과 회복 기간 - 우리피부과") \
        == "same_as_title"
    assert k("稗粒腫の除去費用と回復期間｜アクメ皮膚科 詳しくはこちら", "稗粒腫の除去費用と回復期間｜アクメ皮膚科") \
        == "starts_with_title"
    assert k("Удаление милиумов: цена и восстановление", "Удаление милиумов: цена и восстановление") \
        == "same_as_title"
    # 주제 이름으로 시작하는 손으로 쓴 설명은 템플릿이 아니다(브랜드 꼬리까지 옮겨야 기계다)
    assert k("Milia removal guide covering cost, methods and recovery time.",
             "Milia Removal Guide | Acme Clinic") is None
    assert k("Home", "Home") is None, "짧은 title 의 우연한 일치"
    assert k("", "Milia Removal Guide") is None
    assert set(scoring.DESC_TEMPLATE_KINDS) == {"same_as_title", "starts_with_title"}
    # 진단으로 선다 — 길이 지적보다 앞선다(같은 설명에 지적은 하나)
    t = "Milia removal cost and recovery guide"
    a = _audit("<main><h1>h</h1></main>")
    a["meta_description"] = t + " | Acme. " + t
    a["title"] = t + " | Acme"
    said = [x["now"] for x in _advice(a) if x["tag"] == "meta description"]
    assert len(said) == 1 and scoring.DESC_TEMPLATE_KINDS["starts_with_title"] in said[0], said


# ── 리치 결과가 끝난 유형·사이트 타입별 권장 ──────────────────────────────────
def test_no_markup_advice_names_types_for_the_site_and_never_retired_ones():
    page = _audit(f"<main><h1>h</h1><p>{MID}</p></main>")
    for site, want in scoring.SITE_SCHEMA_TYPES.items():
        fix = next(x["fix"] for x in _advice(page, site_type=site) if x["tag"] == "구조화 데이터")
        assert all(t in fix for t in want), (site, fix)
        assert not [t for t in scoring.RETIRED_TYPES if t in fix], (site, fix)
    saas = next(x["fix"] for x in _advice(page, site_type="saas") if x["tag"] == "구조화 데이터")
    assert "LocalBusiness" not in saas, f"SaaS 페이지에 LocalBusiness 를 권한다: {saas}"
    # 글(발행일·저자가 있는 페이지)은 어느 사이트든 Article
    post = _audit(f"<main><h1>h</h1><p>{MID}</p></main>",
                  head='<meta property="article:published_time" content="2026-03-02">')
    fix = next(x["fix"] for x in _advice(post, site_type="commerce") if x["tag"] == "구조화 데이터")
    assert "Article" in fix and "Product" not in fix, fix
    # 타입을 모르면 후보를 주되 끝난 유형은 없다
    fix = next(x["fix"] for x in _advice(page) if x["tag"] == "구조화 데이터")
    assert all(t in fix for t in scoring.SCHEMA_FALLBACK) and "FAQPage" not in fix, fix


def test_retired_markup_is_info_not_a_fix():
    a = _audit('<main><h1>h</h1></main>', head='<script type="application/ld+json">'
               '{"@context":"https://schema.org","@type":"FAQPage","mainEntity":[]}</script>')
    assert "구조화 데이터" not in _tags(a), f"끝난 유형의 빈칸을 고치라고 한다: {_advice(a)}"
    info = scoring.page_info(a)
    assert any("FAQPage 마크업" in x and "2026-05-07" in x and "리치 결과가 없습니다" in x for x in info), info
    assert not any("FAQPage" in x for x in scoring.page_info(_audit("<main><h1>h</h1></main>")))


# ── 구조화 데이터 값 검사 → 진단 ──────────────────────────────────────────────
def _ld(obj) -> str:
    return '<script type="application/ld+json">' + json.dumps(obj, ensure_ascii=False) + "</script>"


def test_schema_value_issues_reach_advice_and_merchant_rules_only_on_commerce():
    product = {"@context": "https://schema.org", "@type": "Product", "name": "Milia Cream",
               "image": "/img/cream.png", "description": "d", "brand": "b", "sku": "s",
               "offers": {"@type": "Offer", "price": "$29.99"}}
    a = _audit("<main><h1>h</h1></main>", head=_ld(product), url="https://me.example/p/cream")
    codes = {i["code"] for i in json.loads(a["schema_issues_json"])}
    assert {"price_not_number", "no_currency", "relative_url", "merchant_no_id",
            "merchant_no_shipping"} <= codes, codes
    shop = _said(a, "구조화 데이터", site_type="commerce")
    blog = _said(a, "구조화 데이터", site_type="saas")
    assert "숫자가 아닙니다" in shop and "priceCurrency 가 없습니다" in shop, shop
    assert "식별자" in shop and "shippingDetails" in shop, shop
    assert "숫자가 아닙니다" in blog and "식별자" not in blog and "shippingDetails" not in blog, blog
    assert "전체 주소" in blog, blog
    # 값은 칸에도 문장에도 안 실린다
    assert "29.99" not in a["schema_issues_json"] and "Milia Cream" not in shop
    # 다 맞는 마크업은 '[]' — 봤고 문제 없음
    ok = _audit("<main><h1>h</h1></main>", head=_ld({"@context": "https://schema.org",
                "@type": "BreadcrumbList", "itemListElement": [{"@type": "ListItem", "position": 1,
                                                                 "name": "Home", "item": "https://me.example/"}]}))
    assert ok["schema_issues_json"] == "[]" and "구조화 데이터" not in _tags(ok)


def test_new_rules_itemlist_and_software_app():
    a = _audit("<main><h1>h</h1></main>", head=_ld({"@context": "https://schema.org", "@graph": [
        {"@type": "ItemList"}, {"@type": "MobileApplication", "name": "App"}]}),
        url="https://me.example/app")
    gaps = {g["type"]: g for g in json.loads(a["schema_gaps_json"])}
    assert gaps["ItemList"]["need"] == ["itemListElement"], gaps
    assert gaps["SoftwareApplication"]["as"] == "MobileApplication", gaps
    assert gaps["SoftwareApplication"]["need"] == ["offers", "aggregateRating|review"], gaps
    assert "FAQPage" not in scoring.SCHEMA_RULES


def test_self_review_and_local_business_values():
    home = "https://me.example/"
    lb = {"@context": "https://schema.org", "@type": "LocalBusiness", "name": "Acme", "url": home,
          "address": "1 Main St", "telephone": "+1 555 010 0000",
          "aggregateRating": {"@type": "AggregateRating", "ratingValue": 4.9, "reviewCount": 31}}
    html = ('<main><h1>h</h1></main><script type="application/ld+json">'
            + json.dumps(lb).replace('"url"', '"geo":{"latitude":37.56,"longitude":126.97801},"url"')
            + "</script>")
    a = collect_page.audit_html(home, f"<html lang='en'><body>{html}</body></html>")
    said = " | ".join(x["now"] for x in scoring.page_advice(a, [], domain="me.example",
                                                            site_type="local_business"))
    assert "자기 자신에 대한 별점·리뷰" in said and "geo.latitude 가 소수 5자리 미만" in said, said
    assert "geo.longitude" not in said, "5자리로 적은 좌표까지 잡는다"
    # 남의 업체를 모아 평가하는 사이트(directory)에는 자기 리뷰 경고를 안 댄다
    assert "자기 자신" not in " | ".join(x["now"] for x in scoring.page_advice(
        a, [], domain="me.example", site_type="directory"))
    # 일반형·쓰지 말라는 유형은 고칠 것이 아니라 알아 둘 것
    info = scoring.page_info(a, site_type="local_business")
    assert any("일반형" in x for x in info), info
    assert not any("일반형" in x["now"] for x in scoring.page_advice(a, [], domain="me.example"))


# ── Microdata·RDFa ───────────────────────────────────────────────────────────
def test_microdata_and_rdfa_count_as_existing_markup():
    micro = _audit('<main><h1>h</h1><div itemscope itemtype="https://schema.org/Product">'
                   '<span itemprop="name">x</span></div></main>')
    assert json.loads(micro["schema_json"]) == ["Product"], micro["schema_json"]
    assert "구조화 데이터" not in _tags(micro), f"Microdata 가 있는데 없다고 한다: {_advice(micro)}"
    assert any("Microdata 로 단 구조화 데이터: Product" in x for x in scoring.page_info(micro))
    rdfa = _audit('<main vocab="https://schema.org/"><h1>h</h1><div typeof="Organization">'
                  '<span property="name">x</span></div><p typeof="schema:Person">y</p></main>',
                  url="https://me.example/")
    assert json.loads(rdfa["schema_json"]) == ["Organization", "Person"], rdfa["schema_json"]
    assert _sig(rdfa)["markup"]["rdfa"] == ["Organization", "Person"]
    # 홈에 Microdata 로 주인을 단 사이트 — '주인 없음'이라고 하지 않는다
    assert not any("사이트 주인을 밝히는" in x["now"] for x in _advice(rdfa)), _advice(rdfa)
    # 남의 어휘(data-vocabulary 등)는 schema.org 유형이 아니다
    other = _audit('<div itemscope itemtype="http://data-vocabulary.org/Breadcrumb"></div>')
    assert json.loads(other["schema_json"]) == [], other["schema_json"]


# ── 이미지 alt ───────────────────────────────────────────────────────────────
def test_alt_excludes_decorative_and_counts_meaningless():
    a = _audit("""<main><h1>h</h1>
      <img src="a.png"><img src="b.png" aria-hidden="true"><img src="c.png" role="presentation">
      <img src="d.png" alt="IMG_1234"><img src="e.png" alt="image"><img src="f.png" alt="hero.jpg">
      <img src="g.png" alt="Milia around the eyes before treatment"></main>""")
    assert (a["images"], a["images_no_alt"]) == (7, 1), (a["images"], a["images_no_alt"])
    assert _sig(a)["img"] == {"decorative": 2, "junk_alt": 3}, _sig(a)["img"]
    said = _said(a, "이미지")
    assert "alt 없는 이미지 1개" in said and "설명하지 않는 이미지 3개" in said, said
    ko = _audit('<main><h1>h</h1><img src="a.png" alt="이미지"><img src="b.png" alt="사진"></main>', lang="ko")
    assert _sig(ko)["img"]["junk_alt"] == 2


# ── 발행일 ───────────────────────────────────────────────────────────────────
def test_published_date_sources():
    for head in ('<meta name="date" content="2026-03-02">',
                 '<meta name="DC.date.issued" content="2026-03-02T10:00:00Z">',
                 '<meta itemprop="datePublished" content="2026-03-02">',
                 '<meta name="parsely-pub-date" content="2026-03-02T00:00:00Z">'):
        assert _audit("<main><h1>h</h1></main>", head=head)["published"] == "2026-03-02", head
    mod = _audit('<main><h1>h</h1><time itemprop="dateModified" datetime="2026-04-01">Apr 1</time></main>',
                 head='<meta name="last-modified" content="2026-05-01">')
    assert mod["modified"] == "2026-05-01"
    # 주소의 날짜는 발행일 칸에 안 넣는다 — 고친 글을 낡았다고 하게 된다. 정보 줄로만
    u = _audit("<main><h1>h</h1></main>", url="https://me.example/2019/05/02/milia-guide/")
    assert u["published"] is None and _sig(u)["url_date"] == "2019-05-02"
    assert "갱신" not in _tags(u), "주소 날짜로 낡았다고 한다"
    assert any("주소에 날짜가 있습니다(2019-05-02)" in x for x in scoring.page_info(u))
    assert _sig(_audit("<main><h1>h</h1></main>"))["url_date"] is None


# ── 본문 전화·주소 ↔ 마크업 ───────────────────────────────────────────────────
def test_nap_markup_is_compared_with_the_page():
    home = "https://me.example/"

    def page(tel_markup, street, body):
        org = {"@context": "https://schema.org", "@type": "Dentist", "name": "Acme Dental",
               "url": home, "telephone": tel_markup, "logo": "https://me.example/l.png",
               "address": {"@type": "PostalAddress", "streetAddress": street, "postalCode": "04524"}}
        return collect_page.audit_html(home, f"<html lang='ko'><head>{_ld(org)}</head><body>"
                                             f"<main><h1>Acme</h1></main>{body}</body></html>")

    same = page("+82 2-123-4567", "세종대로 110",
                '<footer><a href="tel:02-123-4567">전화</a><address>서울 중구 세종대로 110, 04524</address></footer>')
    n = _sig(same)["nap"]
    assert n["tel_match"] is True and n["addr_match"] is True and n["addr_tag"] is True, n
    assert not any("telephone" in x["now"] or "주소(" in x["now"] for x in _advice(same)), _advice(same)
    diff = page("+82 2-123-4567", "세종대로 110",
                '<footer><a href="tel:02-999-0000">전화</a><p>서울 중구 을지로 30</p></footer>')
    n = _sig(diff)["nap"]
    assert n["tel_match"] is False and n["addr_match"] is False, n
    said = " | ".join(x["now"] for x in _advice(diff) if x["tag"] == "엔티티")
    assert "어느 것과도 다른 번호" in said and "화면에서 찾지 못했습니다" in said, said
    # 본문에 전화 링크가 없으면 못 견준 것이다(다르다고 하지 않는다)
    none = page("+82 2-123-4567", "세종대로 110", "<footer><address>서울 중구 세종대로 110 (04524)</address></footer>")
    assert _sig(none)["nap"]["tel_match"] is None and "다른 번호" not in str(_advice(none))
    # 요청문의 NAP 재료(nap_state)에 본문 대조가 실린다. 값(번호·주소)은 어디에도 안 남는다
    st = scoring.nap_state(diff)
    assert st["body"]["tel_match"] is False and st["rule"] == "LocalBusiness", st
    assert "123" not in diff["signals_json"] and "세종대로" not in diff["signals_json"]
    # 주인 마크업에 전화·주소가 아예 없으면 견줄 것이 없다
    assert _sig(_audit("<main><h1>h</h1><a href='tel:021234567'>t</a></main>"))["nap"] is None
    # 같은 번호 판정 — 국가번호·맨 앞 0 차이는 같은 번호
    assert collect_page.tel_same("+82 2-123-4567", "02-123-4567")
    assert collect_page.tel_same("0082 10 1234 5678", "010-1234-5678")
    assert not collect_page.tel_same("02-123-4567", "02-123-4568")
    assert not collect_page.tel_same("4567", "02-123-4567"), "끝자리 몇 개로 같다고 한다"


# ── 추출성 — H2 구간별 첫 문단 ────────────────────────────────────────────────
def test_section_leads_and_long_paragraphs_feed_extract_advice():
    a = _audit(f"""<main><h1>Milia</h1><p>{MID}</p>
      <h2>What does it cost?</h2><p>{LONG}</p><p>{MID}</p>
      <h2>How long is recovery?</h2><p>{MID}</p>
      <h2>Is it painful?</h2><ul><li>no paragraph here</li></ul>
      <h2>When to see a doctor?</h2><p>{LONG}</p></main>""")
    t = _sig(a)["text"]
    assert t["sec_leads"] == [90, 20, 0, 90], t
    assert (t["paras"], t["long_paras"]) == (5, 2), t
    for kind, tag in (("ai_citation_gap", "추출성"), ("aio_exposure", "읽기 구조")):
        said = " | ".join(x["now"] for x in scoring.extract_advice(a, kind) if x["tag"] == tag)
        assert "H2 구간 4개 중 2개가" in said and "문단 5개 중 그런 문단 2개" in said, (kind, said)
    ok = _audit(f"<main><h1>M</h1><p>{MID}</p><h2>What?</h2><p>{MID}</p><h2>Why?</h2><p>{MID}</p></main>")
    assert not any("H2 구간" in x["now"] for x in scoring.extract_advice(ok, "ai_citation_gap"))
    # 옛 행(칸 없음)은 예전 그대로 — 없는 구간을 지어내지 않는다
    old = {k: v for k, v in a.items() if k != "signals_json"}
    assert not any("H2 구간" in x["now"] for x in scoring.extract_advice(old, "ai_citation_gap"))


# ── 알아 둘 것(정보 줄) — 신뢰 신호·공유 카드·가독성 ──────────────────────────
def test_page_info_counts_signals_without_scoring():
    en = ("The cat sat on the mat. It was a sunny day and the bird sang. " * 12)
    a = _audit(f"""<header><a href="/about-us">About</a></header>
      <main><article><h1>Milia</h1><p>By <a rel="author" href="/team/kim">Dr. Kim</a>
      <time datetime="2026-03-02">Mar 2</time></p><p>{en}</p></article></main>
      <footer><a href="/contact">Contact</a></footer>""",
               head='<meta property="og:title" content="t"><meta property="og:image" content="https://me.example/i.png">'
                    '<meta name="author" content="Dr. Kim">')
    s = _sig(a)
    assert s["eeat"] == {"author": True, "author_link": True, "author_profile": False,
                         "date_shown": True, "modified": False, "about_link": True,
                         "contact_link": True}, s["eeat"]
    assert set(s["eeat"]) == {k for k, _ in scoring.EEAT_SIGNALS}, "수집기와 판정의 신호 이름이 어긋났다"
    assert s["read"]["formula"] == "Flesch Reading Ease" and 7 < s["read"]["wps"] < 8, s["read"]
    info = " \n".join(scoring.page_info(a))
    assert "있음 og:title, og:image" in info and "없음 og:description, twitter:card" in info, info
    assert "있음 저자 표기, 저자 페이지 링크" in info and "없음 저자 마크업의 소개 주소" in info, info
    assert "Flesch Reading Ease" in info and f"문장당 {s['read']['wps']}낱말" in info, info
    assert "점" not in info.replace("점수 아님", ""), f"신뢰 신호를 점수로 말한다: {info}"
    # 정보 줄은 진단(고칠 것)에 섞이지 않는다
    assert not any("og:" in x["now"] or "Flesch" in x["now"] for x in _advice(a)), _advice(a)
    # 공식이 없는 언어는 문장 길이만
    ko = _audit(f"<main><h1>h</h1><p>{'이 글은 비립종을 설명합니다. 원인과 치료를 다룹니다. ' * 20}</p></main>", lang="ko")
    assert _sig(ko)["read"] and "formula" not in _sig(ko)["read"], _sig(ko)["read"]
    assert _sig(_audit(f"<main><p>{'これは本文です。' * 80}</p></main>", lang="ja"))["read"] is None


# ── 안 본 것 ≠ 없는 것, 칸이 Brain 을 거쳐 판정까지 ───────────────────────────
def test_old_rows_and_db_roundtrip():
    a = _audit(f"<main><h1>h</h1><h2>a</h2><h4>b</h4><p>{MID}</p></main>",
               head=_ld({"@context": "https://schema.org", "@type": "Product", "name": "p",
                         "offers": {"@type": "Offer", "price": "x"}}))
    new_cols = ("signals_json", "schema_issues_json", "schema_hash", "canonical_check_json")
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    conn.execute("INSERT INTO projects(id,name,type,domain) VALUES(1,'p','commerce','me.example')")
    a["canonical_check_json"] = json.dumps({"url": "https://me.example/z", "status": 404})
    db.write_page_audits(conn, 1, "2026-11-01", [a])
    row = dict(db.latest_page_audits(conn, 1)[0])
    for c in new_cols:
        assert row[c] == a[c] and row[c] is not None, c
    assert len(row["schema_hash"]) == 16
    tags = [x["tag"] for x in scoring.page_advice(row, [], domain="me.example", site_type="commerce")]
    assert "제목 구조" in tags and "구조화 데이터" in tags and "canonical" in tags, tags
    # 옛 행 — 새 칸이 통째로 NULL. '0개'가 아니라 '안 봤다'라 새 진단은 하나도 안 선다
    old = {k: v for k, v in a.items() if k not in new_cols}
    db.write_page_audits(conn, 1, "2026-10-01", [{**old, "url": "https://me.example/old"}])
    orow = dict(conn.execute("SELECT * FROM page_audits WHERE url LIKE '%/old'").fetchone())
    assert all(orow[c] is None for c in new_cols), orow
    otags = {x["tag"] for x in scoring.page_advice(orow, [], domain="me.example", site_type="commerce")}
    assert not otags & {"제목 구조", "링크 품질", "혼합 콘텐츠", "메타 태그", "숨긴 텍스트"}, otags
    assert scoring.schema_issues(orow) is None and scoring.signal_advice(orow) == []
    # 막혀서 대신 읽은 행도 같다(머리를 못 봤다)
    alt = collect_page.audit_from_parsed("https://me.example/b", {"title": "t", "h1": "h"})
    assert "signals_json" not in alt and scoring.page_info(alt) == []
    # 칸이 생기기 전의 Brain — _migrate 가 칸을 보탠다
    old_db = sqlite3.connect(":memory:")
    old_db.row_factory = sqlite3.Row
    old_db.execute("CREATE TABLE page_audits (id INTEGER PRIMARY KEY, project_id INTEGER,"
                   " checked_date TEXT, url TEXT, js_shell INTEGER)")
    old_db.executescript(db.SCHEMA)
    db._migrate(old_db)
    cols = {r["name"] for r in old_db.execute("PRAGMA table_info(page_audits)")}
    assert set(new_cols) <= cols, set(new_cols) - cols
    # 내용 지문 — 값 하나가 바뀌면 바뀌고, @type 목록(schema_json)은 그대로다
    b = _audit("<main><h1>h</h1></main>", head=_ld({"@context": "https://schema.org", "@type": "Product",
                                                    "name": "p", "offers": {"@type": "Offer", "price": "y"}}))
    assert b["schema_hash"] != a["schema_hash"] and b["schema_json"] == a["schema_json"]
    assert _audit("<main><h1>h</h1></main>")["schema_hash"] == ""


def test_js_shell_keeps_static_facts_but_not_render_dependent_counts():
    shell = collect_page.audit_html(U, '<html lang="en"><head><meta http-equiv="refresh" content="5">'
                                    '</head><body><div id="root"><h2>a</h2><h4>b</h4>'
                                    '<a href="/x"></a></div><script src="http://c.example/a.js"></script>'
                                    "</body></html>")
    assert shell["js_shell"] == 1
    tags = [x["tag"] for x in scoring.signal_advice(shell)]
    assert tags == ["혼합 콘텐츠", "메타 태그"], tags
    fix = next(x["fix"] for x in _advice(shell) if x["tag"] == "구조화 데이터")
    assert "단정하지 않습니다" in fix and "렌더 후에도 없는지" in fix, fix


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)} passed")
