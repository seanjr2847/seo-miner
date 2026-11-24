#!/usr/bin/env python3
"""크롤·사이트맵·사이트 단위 판정 검사 — `python test_crawl_site.py` (바깥 네트워크 0).

collect_crawl 의 자기검사는 가상 사이트 한 벌로 회차 전체(적재·비교)를 본다. 여기는 그 옆의
판정 조각들을 하나씩 본다: robots.txt 상태, 사이트맵 읽기(폴백·gzip·RSS·txt·상한), 속도 제한
물러서기, X-Robots-Tag, 소프트 404, 호스트 정규화, hreflang 대상·코드, 같은 틀 근사 중복,
템플릿 메타, 다국어 판 동등성 — 그리고 서면 안 되는 자리.
"""
import gzip
import json
import os
import sys
import tempfile
from pathlib import Path
from urllib import robotparser

sys.path.insert(0, str(Path(__file__).parent))
os.environ.setdefault("CAPTURE_HOME", tempfile.mkdtemp(prefix="seo-miner-crawl-site-test-"))

import collect_crawl as cc  # noqa: E402
import db  # noqa: E402
import scoring  # noqa: E402

H = "https://s.kr"


# ── scoring: hreflang 코드 실명단 ───────────────────────────────────────────
def test_hreflang_codes_are_real_lists():
    ok = scoring.hreflang_code_ok
    for good in ("ko", "en-GB", "zh-Hans", "zh-Hant-TW", "x-default", "EN-us", "pt-BR", "de-CH"):
        assert ok(good), good
    # 꼴은 코드 같지만 명단에 없다 — 옛 정규식은 eng·jp·en-EU·es-419 를 통과시켰다
    for bad in ("eng", "jp", "en-EU", "en-UK", "es-419", "en_US", "", "english", "ko-KR-x"):
        assert not ok(bad), bad
    # es-LA 는 명단으로는 못 거른다 — LA 는 라오스의 실제 코드다(중남미를 뜻하려던 것이라도)
    assert ok("es-LA") and ok("kr")       # kr = 카누리어(ISO 639-1). 한국어는 ko 지만 코드는 코드다
    # 명단 크기 — 한 줄이 빠지거나 오타가 끼면 여기서 걸린다(ISO 639-1 184개·ISO 3166-1 249개)
    assert len(scoring.ISO_639_1) == 184, len(scoring.ISO_639_1)
    assert len(scoring.ISO_3166_1) == 249, len(scoring.ISO_3166_1)
    assert all(len(c) == 2 and c.islower() for c in scoring.ISO_639_1)
    assert all(len(c) == 2 and c.isupper() for c in scoring.ISO_3166_1)
    # 페이지 처방(_hreflang_advice)이 같은 명단을 본다 — 두 벌이면 한쪽만 통과시킨다
    audit = {"js_shell": 0, "url": "https://a.kr/x", "canonical": "https://a.kr/x",
             "hreflang_json": json.dumps([["ko", "https://a.kr/x"], ["eng", "https://a.kr/en/x"],
                                          ["en-EU", "https://a.kr/es/x"],
                                          ["x-default", "https://a.kr/x"]])}
    got = [r["now"] for r in scoring._hreflang_advice(audit) if "코드가 아닌" in r["now"]]
    assert got and "eng" in got[0] and "en-EU" in got[0], got


# ── scoring: 템플릿 메타 ────────────────────────────────────────────────────
def test_desc_template_kind():
    k = scoring.desc_template_kind
    # 설명 = 제목(구두점·대소문자·공백 차이는 같은 것)
    assert k("Laser Toning | The Clinic", "Laser Toning | The Clinic") == "same_as_title"
    assert k("레이저 토닝 - 더클리닉", "레이저 토닝 – 더클리닉") == "same_as_title"
    # 비라틴 문자를 지우고 비교하지 않는다 — 한글 제목 둘은 서로 다르다
    assert k("기미 레이저는 3주 간격으로 다섯 번 받습니다. 비용과 회복 기간을 정리했습니다.",
             "점 제거 후 관리") is None
    # 제목 앞부분을 되풀이 — 틀이 {title}. {상투 문구} 로 찍은 것
    assert k("Laser Toning. Learn more about our services.", "Laser Toning | The Clinic",
             lang="en") == "title_repeat"
    assert k("レーザートーニング 詳しくはこちら", "レーザートーニング | クリニック", lang="ja") == "title_repeat"
    # 브랜드 꼬리가 설명에 섞임 — 제목 틀(… | 브랜드)이 설명 칸에 그대로 찍혔다
    assert k("Melasma fades over five sessions spaced three weeks apart | The Clinic",
             "Melasma treatment | The Clinic") == "brand_tail"
    # 손으로 쓴 설명 — 제목의 말을 쓰더라도 문장이면 틀이 아니다
    assert k("Five sessions, three weeks apart: what laser toning does to melasma and what it costs.",
             "Laser Toning | The Clinic", lang="en") is None
    assert k("", "제목") is None and k("설명", "") is None
    # 상투 CTA 표는 언어별 한 벌 — 표에 없는 언어는 CTA 로 판정하지 않는다(지어내지 않는다)
    assert "en" in scoring.DESC_CTA and "ko" in scoring.DESC_CTA
    assert all(isinstance(v, tuple) and v for v in scoring.DESC_CTA.values())


# ── 가짜 사이트 ─────────────────────────────────────────────────────────────
import crawl_site as cs  # noqa: E402

_N = [0]
LONG = "단어 " * 230


def page(title, *, desc="손으로 쓴 설명입니다, 이 페이지가 무엇인지 말합니다.", body="", head="", tail="",
         h1=None, lang="ko"):
    return (200, f'<html lang="{lang}"><head><title>{title}</title>'
                 f'<meta name="description" content="{desc}">{head}</head><body>'
                 f"<main><h1>{h1 or title}</h1><p>{body or title + ' ' + LONG}</p>{tail}</main>"
                 "</body></html>", "text/html")


def sitemap(urls, extra=""):
    body = "".join(u if u.startswith("<url>") else f"<url><loc>{u}</loc></url>" for u in urls)
    return (200, '<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"'
                 f"{extra}>{body}</urlset>", "application/xml")


def faker(site: dict, log: list, missing=(404, "없음", "text/html")):
    """site: {url: (status, text|bytes, ctype[, {headers, final, chain}])} 또는 url → 그것을 주는 함수."""
    def f(url):
        log.append(url)
        v = site.get(url, missing)
        if callable(v):
            v = v(url)
        status, text, ctype = v[:3]
        more = v[3] if len(v) > 3 else {}
        raw = text if isinstance(text, (bytes, bytearray)) else (text or "").encode("utf-8")
        return {"final_url": more.get("final", url), "status": status,
                "chain": more.get("chain", []), "text": "" if isinstance(text, bytes) else text,
                "bytes": len(raw), "content_type": ctype, "error": None,
                "headers": more.get("headers", {}), "content": raw}
    return f


def crawl_once(site: dict, *, host="s.kr", ptype="saas", locale="ko-KR", limit=50,
               missing=(404, "없음", "text/html")):
    """가상 사이트를 한 회차 돈다 → (run 행, {(kind,url): 이슈}, 가져온 주소, 기다린 초들, conn)."""
    _N[0] += 1
    name = f"t{_N[0]}"
    conn = db.connect()
    conn.execute("INSERT INTO projects(name, domain, locale, type) VALUES(?,?,?,?)",
                 (name, host, locale, ptype))
    conn.commit()
    log, waits = [], []
    keep = cc.fetch, cc._sleep
    cc.fetch, cc._sleep = faker(site, log, missing), waits.append
    try:
        r = cc.collect(name, conn=conn, throttle=0, limit=limit, max_depth=5)
    finally:
        cc.fetch, cc._sleep = keep
    run = conn.execute("SELECT * FROM crawl_runs ORDER BY id DESC LIMIT 1").fetchone()
    run = dict(run)
    run["_result"] = r
    run["_notes"] = conn.execute("SELECT notes FROM runs ORDER BY id DESC LIMIT 1").fetchone()["notes"]
    return run, cc._issues(conn, run["id"]), log, waits, conn


def base_site(host="s.kr", robots="User-agent: *\n", **pages):
    """홈 + 넘겨받은 장들. 홈이 전부로 링크한다. robots.txt 에 Sitemap: 줄은 없다."""
    H0 = f"https://{host}"
    site = {f"{H0}/robots.txt": (200, robots, "text/plain")}
    tail = "".join(f'<a href="{p}">{p}</a>' for p in pages)
    site[f"{H0}/"] = page("홈", tail=tail)
    for p, v in pages.items():
        site[f"{H0}{p}"] = v
    return site


# ── 1. robots.txt 상태 ──────────────────────────────────────────────────────
def test_robots_state_is_recorded_not_flattened_to_allow():
    assert [cs.robots_state(s) for s in (200, 404, 403, 410, 500, 503, 429, None)] == \
        ["ok", "none", "none", "none", "blocked", "blocked", "unknown", "unknown"]
    R = f"{H}/robots.txt"
    # 5xx — 준수 크롤러는 사이트 전체를 멈춘다. 다시 물어도 같을 때만 그렇게 적는다
    site = base_site()
    site[R] = (503, "", "text/plain")
    run, issues, log, waits, _ = crawl_once(site)
    assert run["robots_state"] == "blocked" and run["robots_status"] == 503, run
    assert ("robots_5xx", R) in issues and issues[("robots_5xx", R)]["severity"] == "bad", sorted(issues)
    assert log.count(R) == 2 and cc.ROBOTS_RETRY_WAIT in waits, (log, waits)
    assert run["pages"] >= 1, "robots.txt 가 5xx 여도 크롤은 돈다(내 사이트를 보는 일이다)"
    assert "robots_error=1" in run["_notes"], run["_notes"]
    # 잠깐의 5xx — 다시 물었더니 200 이면 오류로 적지 않는다
    n = [0]

    def flaky(url):
        n[0] += 1
        return (503, "", "text/plain") if n[0] == 1 else (200, "User-agent: *\n", "text/plain")
    site[R] = flaky
    run, issues, *_ = crawl_once(site)
    assert run["robots_state"] == "ok" and not any(k.startswith("robots_") for k, _ in issues), run
    # 429 — 판정 불가. 4xx — 규칙 없음(이슈 아님)
    site[R] = (429, "", "text/plain")
    run, issues, *_ = crawl_once(site)
    assert run["robots_state"] == "unknown" and ("robots_unknown", R) in issues, (run, sorted(issues))
    assert issues[("robots_unknown", R)]["severity"] == "info"
    site[R] = (404, "", "text/html")
    run, issues, *_ = crawl_once(site)
    assert run["robots_state"] == "none" and not any(k.startswith("robots_") for k, _ in issues)
    # 없는 주소를 홈으로 돌려보내는 사이트 — 200 HTML 은 robots.txt 가 아니다
    site[R] = (200, "<!doctype html><title>홈</title>", "text/html")
    run, issues, *_ = crawl_once(site)
    assert run["robots_state"] == "none" and not run["robots_txt"], run


# ── 2. 사이트맵 읽기 ────────────────────────────────────────────────────────
def _resp(text, status=200, ctype="application/xml"):
    raw = text if isinstance(text, bytes) else text.encode("utf-8")
    return {"status": status, "text": "" if isinstance(text, bytes) else text, "content": raw,
            "bytes": len(raw), "content_type": ctype}


def test_sitemap_formats_and_failures_are_read_or_recorded():
    xml = sitemap([f"{H}/a", "<url><loc>" + f"{H}/b" + "</loc><lastmod>2026-11-01</lastmod>"
                   '<xhtml:link rel="alternate" hreflang="en" href="' + f"{H}/en/b" + '"/></url>'],
                  ' xmlns:xhtml="http://www.w3.org/1999/xhtml"')[1]
    f = cs.parse_sitemap("u", _resp(xml))
    assert f["format"] == "urlset" and [e["url"] for e in f["entries"]] == [f"{H}/a", f"{H}/b"], f
    assert f["entries"][1]["lastmod"] == "2026-11-01"
    assert f["entries"][1]["alts"] == [("en", f"{H}/en/b")], f["entries"][1]
    # gzip — 매직 1f8b 로 알아보고 푼다(.xml.gz 는 글자로 읽으면 깨진다)
    gz = cs.parse_sitemap("u", _resp(gzip.compress(xml.encode()), ctype="application/gzip"))
    assert gz["gz"] and gz["format"] == "urlset" and len(gz["entries"]) == 2, gz
    bad = cs.parse_sitemap("u", _resp(b"\x1f\x8b" + b"not gzip at all", ctype="application/gzip"))
    assert bad["format"] is None and "gzip" in bad["error"], bad
    # RSS · Atom · 주소만 적은 txt
    rss = cs.parse_sitemap("u", _resp(f"<rss><channel><item><link>{H}/r1</link></item>"
                                      f"<item><link>{H}/r2</link></item></channel></rss>"))
    assert rss["format"] == "rss" and len(rss["entries"]) == 2, rss
    atom = cs.parse_sitemap("u", _resp('<feed xmlns="http://www.w3.org/2005/Atom"><entry>'
                                       f'<link href="{H}/at"/><updated>2026-10-01T00:00:00Z</updated>'
                                       "</entry></feed>"))
    assert atom["format"] == "atom" and atom["entries"][0]["url"] == f"{H}/at", atom
    txt = cs.parse_sitemap("u", _resp(f"{H}/t1\n{H}/t2\n", ctype="text/plain"))
    assert txt["format"] == "txt" and len(txt["entries"]) == 2, txt
    # 못 읽은 것은 왜인지 남긴다 — 조용히 빈 목록이면 근거가 사라진 것을 아무도 모른다
    for body, why in (("<!doctype html><html>홈</html>", "HTML"), ("<urlset><url>", "XML"),
                      ('<!DOCTYPE x [<!ENTITY a "b">]><urlset/>', "DOCTYPE"), ("", "빈"),
                      ("그냥 글자", "주소 목록")):
        got = cs.parse_sitemap("u", _resp(body))
        assert got["format"] is None and why in got["error"], (body, got)
    assert "HTTP 500" in cs.parse_sitemap("u", _resp("", status=500))["error"]
    # 색인 — 한 겹 따라가고, 상한 밖 하위 파일은 잘렸다고 적는다. 파일 사이에 겹친 주소도 센다
    idx = "<sitemapindex>" + "".join(f"<sitemap><loc>{H}/sm{i}.xml</loc></sitemap>"
                                     for i in range(5)) + "</sitemapindex>"
    files = {f"{H}/index.xml": _resp(idx)}
    for i in range(5):
        files[f"{H}/sm{i}.xml"] = _resp(sitemap([f"{H}/p{i}", f"{H}/shared"])[1])
    got = cs.read_sitemaps([f"{H}/index.xml"], lambda u: files[u], max_files=3)
    assert got["capped"] and len(got["entries"]) == 4 and got["files"][0]["skipped"] == 2, got
    assert set(got["dups"]) == {f"{H}/shared"} and len(got["dups"][f"{H}/shared"]) == 3, got["dups"]
    assert all(e["source"].startswith(f"{H}/sm") for e in got["entries"])
    full = cs.read_sitemaps([f"{H}/index.xml"], lambda u: files[u], max_urls=2)
    assert full["capped"] and len(full["entries"]) == 2, full


def test_sitemap_found_at_common_path_when_robots_has_no_sitemap_line():
    xml = sitemap(["<url><loc>" + f"{H}/a" + "</loc><lastmod>2026-11-01</lastmod></url>", f"{H}/"])[1]
    site = base_site(**{"/a": page("에이")})
    site[f"{H}/wp-sitemap.xml"] = (200, gzip.compress(xml.encode()), "application/gzip")
    run, issues, log, _, conn = crawl_once(site)
    assert run["seed"] == "sitemap", run
    # 앞의 흔한 경로를 순서대로 두드린 끝에 찾았다
    assert [u.rsplit("/", 1)[1] for u in log if u.endswith(".xml")] == \
        [p.strip("/") for p in cs.SITEMAP_FALLBACKS], log
    rows = {r["url"]: dict(r) for r in conn.execute(
        "SELECT url, lastmod, source FROM sitemap_urls WHERE run_id=?", (run["id"],))}
    assert rows[f"{H}/a"]["lastmod"] == "2026-11-01" and rows[f"{H}/a"]["source"].endswith("wp-sitemap.xml")
    facts = json.loads(run["facts_json"])
    assert facts["sitemap"]["fallback"] is True and facts["sitemap"]["files"][0]["gz"], facts["sitemap"]
    assert any("흔한 경로" in ln["v"] for ln in facts["lines"] if ln["k"] == "사이트맵"), facts["lines"]
    # Sitemap: 줄이 가리킨 파일이 깨졌으면 그것을 이슈로 남기고 흔한 경로를 본다
    site[f"{H}/robots.txt"] = (200, f"User-agent: *\nSitemap: {H}/broken.xml\n", "text/plain")
    site[f"{H}/broken.xml"] = (200, "<urlset><url>", "application/xml")
    run, issues, *_ = crawl_once(site)
    assert run["seed"] == "sitemap" and ("sitemap_unreadable", f"{H}/broken.xml") in issues, sorted(issues)
    # 어디에도 없으면 홈에서 돈다 — 그리고 사실 줄이 그렇게 말한다
    run, issues, *_ = crawl_once(base_site(**{"/a": page("에이")}))
    assert run["seed"] == "home" and run["discovery_capped"] == 0, run
    assert any("찾지 못했습니다" in ln["v"] for ln in json.loads(run["facts_json"])["lines"])


def test_sitemap_over_cap_is_sampled_by_url_shape_and_marked():
    assert cs.url_template(f"{H}/blog/my-post") == "/blog/*"
    assert cs.url_template(f"{H}/en/blog/x") == "/en/blog/*" and cs.url_template(f"{H}/about") == "/*"
    assert cs.url_template(f"{H}/") == "/" and cs.url_template(f"{H}/city/seoul/gangnam") == "/city/*/*"
    urls = [f"/blog/{i}" for i in range(20)] + ["/docs/a", "/docs/b", "/pricing"]
    got = cs.stratify(urls, 6)
    assert got == ["/blog/0", "/docs/a", "/pricing", "/blog/1", "/docs/b", "/blog/2"], got
    assert cs.stratify(urls) != urls and sorted(cs.stratify(urls)) == sorted(urls)
    pages = {p: page(p) for p in urls}
    site = base_site(robots=f"User-agent: *\nSitemap: {H}/sitemap.xml\n")
    for p, v in pages.items():
        site[f"{H}{p}"] = v
    site[f"{H}/sitemap.xml"] = sitemap([f"{H}{p}" for p in urls])
    keep = cc.MAX_SITEMAP_URLS
    cc.MAX_SITEMAP_URLS = 6
    try:
        run, issues, log, _, _ = crawl_once(site, limit=1)
    finally:
        cc.MAX_SITEMAP_URLS = keep
    assert run["discovery_capped"] == 1 and "sitemap_capped=1" in run["_notes"], run
    assert {f"{H}/docs/a", f"{H}/docs/b", f"{H}/pricing"} <= set(log), \
        f"상한에서 앞쪽(/blog/*)만 잘라 갔다: {log}"
    # 다 못 읽었으면 '사이트맵에 없다'를 말하지 않는다
    assert not any(k == "not_in_sitemap" for k, _ in issues), sorted(issues)
    assert any("전수가 아닙니다" in ln["v"] for ln in json.loads(run["facts_json"])["lines"])


def test_bfs_batch_is_interleaved_by_url_shape():
    """홈이 글 30개를 먼저 걸고 그 뒤에 문서·가격을 건다 — 상한이 작아도 다른 꼴이 표본에 든다."""
    order = [f"/blog/{i}" for i in range(30)] + ["/docs/x", "/pricing"]
    site = base_site(**{p: page(p) for p in order})
    run, _, log, _, _ = crawl_once(site, limit=6)
    got = [u for u in log if u in {f"{H}{p}" for p in order}]
    assert f"{H}/docs/x" in got and f"{H}/pricing" in got, got


# ── 3. 속도 제한 · Crawl-delay ──────────────────────────────────────────────
def test_rate_limited_pages_back_off_and_are_not_errors():
    n = [0]

    def slow(url):
        n[0] += 1
        if n[0] <= 2:
            return (429, "", "text/html", {"headers": {"retry-after": "7"}})
        return page("느림")
    site = base_site(**{"/slow": slow, "/never": (503, "", "text/html"), "/shop": (430, "", "text/html"),
                        "/gone": (404, "", "text/html")})
    run, issues, log, waits, conn = crawl_once(site)
    assert waits.count(7) == 2, f"Retry-After 를 안 따랐다: {waits}"
    assert log.count(f"{H}/slow") == 3 and log.count(f"{H}/never") == 1 + cc.RETRY_MAX, log
    st = {r["url"]: r["status"] for r in conn.execute(
        "SELECT url, status FROM crawl_pages WHERE run_id=?", (run["id"],))}
    assert st[f"{H}/slow"] == 200 and st[f"{H}/never"] == 503, st
    for u in ("/never", "/shop", "/slow"):
        assert ("http_error", f"{H}{u}") not in issues and ("broken_internal", f"{H}{u}") not in issues, \
            f"속도 제한을 깨진 페이지로 적었다: {u}"
    assert ("http_error", f"{H}/gone") in issues and ("broken_internal", f"{H}/gone") in issues
    rl = issues[("rate_limited", f"{H}/")]
    assert rl["severity"] == "info" and "2장" in rl["detail"], rl
    assert "rate_limited=2" in run["_notes"], run["_notes"]
    # 물러서기는 두 배씩, 기본 대기부터
    assert cc.RETRY_WAIT in waits and cc.RETRY_WAIT * 2 in waits, waits
    # 사이트가 통째로 속도 제한이면 막힌 것으로 친다 — 이슈를 안 적는다
    run, issues, _, waits, _ = crawl_once({}, missing=(429, "", "text/html"))
    assert run["_result"].skipped and not issues, (run["_result"], issues)


def test_crawl_delay_is_honoured_one_at_a_time():
    seen = {}
    keep = cc.crawl

    def spy(*a, **kw):
        seen.update(throttle=kw.get("throttle"), workers=kw.get("workers"))
        return keep(*a, **kw)
    cc.crawl = spy
    try:
        run, *_ = crawl_once(base_site(robots="User-agent: *\nCrawl-delay: 3\n"))
        assert seen == {"throttle": 3.0, "workers": 1}, seen
        assert "crawl_delay=3" in run["_notes"]
        assert any(ln["k"] == "Crawl-delay" and "3초" in ln["v"]
                   for ln in json.loads(run["facts_json"])["lines"])
        crawl_once(base_site(robots="User-agent: *\nCrawl-delay: 60\n"))
        assert seen == {"throttle": cc.CRAWL_DELAY_MAX, "workers": 1}, seen
        crawl_once(base_site())
        assert seen == {"throttle": 0.0, "workers": None}, seen
    finally:
        cc.crawl = keep


# ── 4. X-Robots-Tag · 소프트 404 ────────────────────────────────────────────
def test_x_robots_header_noindex_counts_as_noindex():
    x = cs.x_robots_effective
    assert x("noindex") == "noindex" and x("NoIndex, nofollow") == "noindex, nofollow"
    assert x("none") == "noindex, nofollow"
    assert x("otherbot: noindex, nofollow") is None, "다른 봇에게만 건 규칙이다"
    assert x("googlebot: noindex") == "noindex"
    assert x("max-snippet: 0, googlebot: nofollow, otherbot: noindex") == "max-snippet: 0, nofollow"
    assert x("unavailable_after: 25 Jun 2010 15:00:00 PST") == "unavailable_after: 25 jun 2010 15:00:00 pst"
    assert x("") is None and x(None) is None
    hd = lambda v: {"headers": {"x-robots-tag": v}}
    site = base_site(**{"/h": (*page("헤더"), hd("noindex")),
                        "/o": (*page("남의 봇"), hd("otherbot: noindex")),
                        "/m": page("메타", head='<meta name="robots" content="noindex">'),
                        "/f.pdf": (200, "%PDF", "application/pdf", hd("googlebot: noindex"))})
    run, issues, _, _, conn = crawl_once(site)
    assert "X-Robots-Tag" in issues[("noindex", f"{H}/h")]["detail"], issues.get(("noindex", f"{H}/h"))
    assert "meta robots" in issues[("noindex", f"{H}/m")]["detail"]
    assert ("noindex", f"{H}/f.pdf") in issues, "HTML 이 아닌 파일의 헤더 noindex 를 못 봤다"
    assert ("noindex", f"{H}/o") not in issues, "다른 봇에게만 건 noindex 를 구글 것으로 적었다"
    got = conn.execute("SELECT x_robots FROM crawl_pages WHERE run_id=? AND url=?",
                       (run["id"], f"{H}/h")).fetchone()["x_robots"]
    assert got == "noindex", got


def test_soft_404_probe_runs_once():
    run, issues, log, _, _ = crawl_once(base_site(), missing=(200, "<html><title>홈</title></html>",
                                                             "text/html"))
    probes = [u for u in log if "seo-miner-probe-" in u]
    assert len(probes) == 1, probes
    assert ("soft_404", f"{H}/") in issues and "HTTP 200" in issues[("soft_404", f"{H}/")]["detail"]
    run, issues, *_ = crawl_once(base_site())
    assert ("soft_404", f"{H}/") not in issues
    assert any(ln["k"] == "없는 주소" and "없다고 답합니다" in ln["v"]
               for ln in json.loads(run["facts_json"])["lines"])
    # 못 받은 것(5xx·429·403)은 판정하지 않는다
    for st in (500, 429, 403, None):
        assert cs.probe_soft404(f"{H}/", lambda u: {"status": st}, "x")["soft"] is None, st


# ── 5. 호스트 정규화 ────────────────────────────────────────────────────────
def test_host_checks():
    def run(http, alt, cert=None, home="https://s.kr/"):
        table = {"http://s.kr/": http, "https://www.s.kr/": alt, "https://s.kr/": alt}
        f = lambda u: {"status": None} if table.get(u) is None else dict(table[u])
        return cs.host_checks(home, f, cert_days=(lambda h: cert) if cert is not None else None)
    ok_http = {"status": 200, "final_url": "https://s.kr/", "chain": [("http://s.kr/", 301)]}
    ok_alt = {"status": 200, "final_url": "https://s.kr/", "chain": [("https://www.s.kr/", 301)]}
    facts, issues = run(ok_http, ok_alt, cert=60)
    assert issues == [] and facts == {"http": "redirect", "alt_host": "redirect", "cert_days": 60}, (facts, issues)
    # http 가 그대로 열린다 / 302 로 넘긴다
    _, issues = run({"status": 200, "final_url": "http://s.kr/", "chain": []}, ok_alt)
    assert [k for k, *_ in issues] == ["host_http"] and "그대로 열립니다" in issues[0][2], issues
    _, issues = run({**ok_http, "chain": [("http://s.kr/", 302)]}, ok_alt)
    assert [k for k, *_ in issues] == ["host_http"] and "302" in issues[0][2], issues
    # www 와 비www 가 둘 다 200 (넘어가지 않는다)
    _, issues = run(ok_http, {"status": 200, "final_url": "https://www.s.kr/", "chain": []})
    assert [k for k, *_ in issues] == ["host_dup"], issues
    # www 가 아예 없다(응답 없음) — 문제가 아니다
    facts, issues = run(ok_http, None)
    assert issues == [] and facts["alt_host"] is None, (facts, issues)
    # www 사이트는 비www 쪽을 본다
    facts, issues = run(ok_http, {"status": 200, "final_url": "https://s.kr/", "chain": []},
                        home="https://www.s.kr/")
    assert [k for k, *_ in issues] == ["host_dup"], issues
    # 인증서 — 임박·만료만. 못 쟀으면(None) 말하지 않는다
    assert [k for k, *_ in run(ok_http, ok_alt, cert=cs.CERT_WARN_DAYS)[1]] == ["cert_expiring"]
    assert "만료됐습니다" in run(ok_http, ok_alt, cert=-1)[1][0][2]
    assert run(ok_http, ok_alt, cert=cs.CERT_WARN_DAYS + 1)[1] == []
    # 검사·가짜 전송에서는 인증서를 재러 바깥으로 나가지 않는다
    keep = cc.fetch
    cc.fetch = lambda u: {}
    try:
        assert cc._cert_fn() is None
    finally:
        cc.fetch = keep


# ── 6. 사이트맵 ↔ 크롤 · lastmod · 위생 ─────────────────────────────────────
def _pg(url, **kw):
    return {"url": url, "status": 200, "redirect_to": None, "title": "T", "words": 300,
            "robots": None, "x_robots": None, "canonical": None, "description": "설명", **kw}


def test_sitemap_vs_crawl():
    pages = [_pg(f"{H}/ok"), _pg(f"{H}/gone", status=404, title=None, words=None),
             _pg(f"{H}/moved", status=301, redirect_to=f"{H}/ok", title=None, words=None),
             _pg(f"{H}/hidden", robots="noindex,follow"), _pg(f"{H}/hdr", x_robots="noindex"),
             _pg(f"{H}/canon", canonical=f"{H}/ok"), _pg(f"{H}/busy", status=503, title=None, words=None),
             _pg(f"{H}/wall", status=403, title=None, words=None),
             _pg(f"{H}/extra"), _pg(f"{H}/extra-noindex", robots="noindex"),
             _pg(f"{H}/f.pdf", title=None, words=None)]
    sm = [p["url"] for p in pages if "extra" not in p["url"] and not p["url"].endswith(".pdf")]
    got = {(k, u): d for k, u, d in cs.sitemap_vs_crawl(sm, pages)}
    bad = {u.rsplit("/", 1)[1] for k, u in got if k == "sitemap_bad_url"}
    assert bad == {"gone", "moved", "hidden", "hdr", "canon"}, bad
    assert "리다이렉트" in got[("sitemap_bad_url", f"{H}/moved")] and "404" in got[("sitemap_bad_url", f"{H}/gone")]
    assert {u for k, u in got if k == "not_in_sitemap"} == {f"{H}/extra"}, got
    # 상한에서 잘린 사이트맵이면 '없다'를 말하지 않는다. 사이트맵이 없으면 아무것도 말하지 않는다
    assert not any(k == "not_in_sitemap" for k, *_ in cs.sitemap_vs_crawl(sm, pages, capped=True))
    assert cs.sitemap_vs_crawl([], pages) == []


def test_lastmod_quality():
    from datetime import date
    today = date(2026, 11, 24)
    ent = lambda u, lm: {"url": u, "norm": u, "lastmod": lm}
    same = [ent(f"{H}/p{i}", "2026-11-20") for i in range(12)]
    got = cs.lastmod_issue(same, [], f"{H}/", today=today)
    assert got and got[0][0] == "sitemap_lastmod" and "12개 중 12개" in got[0][2], got
    varied = [ent(f"{H}/p{i}", f"2026-10-{i + 1:02d}") for i in range(12)]
    assert cs.lastmod_issue(varied, [], f"{H}/", today=today) == []
    assert cs.lastmod_issue([ent(f"{H}/a", None)], [], f"{H}/", today=today) == []
    # W3C 형식 — 되는 꼴과 안 되는 꼴
    for good in ("2026", "2026-11", "2026-11-01", "2026-11-01T10:00+09:00", "2026-11-01T10:00:00Z",
                 "2026-11-01T10:00:00.5+09:00"):
        assert cs.lastmod_issue([ent(f"{H}/a", good)], [], f"{H}/", today=today) == [], good
    for bad in ("2026/11/01", "11-01-2026", "2026-11-01 10:00:00", "어제", "2026-13-40"):
        got = cs.lastmod_issue([ent(f"{H}/a", bad)], [], f"{H}/", today=today)
        assert got and "형식" in got[0][2], bad
    assert "앞날" in cs.lastmod_issue([ent(f"{H}/a", "2027-01-01")], [], f"{H}/", today=today)[0][2]
    # 페이지가 말하는 수정일과 어긋남 — 하루 차이는 시간대다
    pages = [{"url": f"{H}/a", "modified": "2026-09-01"}, {"url": f"{H}/b", "modified": "2026-11-02"}]
    got = cs.lastmod_issue([ent(f"{H}/a", "2026-11-01"), ent(f"{H}/b", "2026-11-01")], pages,
                           f"{H}/", today=today)
    assert got and "다른 값 1개(둘 다 있는 2개 중)" in got[0][2] and "/a" in got[0][2], got


def test_sitemap_file_hygiene():
    long_url = f"{H}/" + "a" * cs.LOC_MAX
    disc = {"files": [{"url": f"{H}/sm.xml", "format": "urlset", "n": 4, "bytes": 100, "error": None},
                      {"url": f"{H}/big.xml", "format": "urlset", "n": cs.SITEMAP_MAX_URLS + 1,
                       "bytes": cs.SITEMAP_MAX_BYTES + 1, "error": None},
                      {"url": f"{H}/x.xml", "format": None, "error": "HTTP 404"},
                      {"url": f"{H}/fine.xml", "format": "urlset", "n": 1, "bytes": 10, "error": None}],
            "entries": [{"url": "https://other.com/x", "source": f"{H}/sm.xml"},
                        {"url": f"{H}/a?utm_source=x", "source": f"{H}/sm.xml"},
                        {"url": f"{H}/b?PHPSESSID=1", "source": f"{H}/sm.xml"},
                        {"url": long_url, "source": f"{H}/sm.xml"},
                        {"url": f"{H}/c?page=2", "source": f"{H}/fine.xml"},
                        {"url": "https://www.s.kr/www-is-mine", "source": f"{H}/fine.xml"}],
            "dups": {f"{H}/dup": [f"{H}/sm.xml", f"{H}/big.xml"]}}
    got = {(k, u): d for k, u, d in cs.sitemap_file_issues(disc, f"{H}/")}
    d = got[("sitemap_hygiene", f"{H}/sm.xml")]
    assert "다른 호스트의 주소 1개" in d and "파라미터가 붙은 주소 2개" in d and "2,048자" in d, d
    big = got[("sitemap_hygiene", f"{H}/big.xml")]
    assert "50,000" in big and "50MB" in big, big
    assert got[("sitemap_unreadable", f"{H}/x.xml")] == "HTTP 404"
    assert "겹쳐 실린 주소 1개" in got[("sitemap_hygiene", f"{H}/")]
    assert ("sitemap_hygiene", f"{H}/fine.xml") not in got, "쪽 번호 파라미터·www 는 문제가 아니다"


def test_news_sitemap_rules_only_for_publishers():
    from datetime import date
    today = date(2026, 11, 24)
    old = ('<url><loc>{0}/n1</loc><news:news><news:publication_date>2026-11-01</news:publication_date>'
           '</news:news></url><url><loc>{0}/n2</loc><news:news><news:publication_date>2026-11-23'
           '</news:publication_date></news:news></url>').format(H)
    xml = ('<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" '
           'xmlns:news="http://www.google.com/schemas/sitemap-news/0.9">' + old + "</urlset>")
    disc = cs.read_sitemaps([f"{H}/news.xml"], lambda u: _resp(xml))
    assert disc["files"][0]["news"] == 2, disc["files"]
    got = cs.news_issue(disc, f"{H}/", today=today)
    assert len(got) == 1 and "오래된 기사 1개" in got[0][2] and "/n1" in got[0][2], got
    disc["files"][0]["news"] = cs.NEWS_MAX + 1
    assert "1,000" in cs.news_issue(disc, f"{H}/", today=today)[0][2]
    # 사이트 종류가 publisher 일 때만 선다 — 다른 사이트에 뉴스 규칙은 군말이다
    site = base_site(robots=f"User-agent: *\nSitemap: {H}/news.xml\n",
                     **{"/n1": page("기사1"), "/n2": page("기사2")})
    site[f"{H}/news.xml"] = (200, xml, "application/xml")
    _, issues, *_ = crawl_once(site, ptype="saas")
    assert not any(k == "sitemap_news" for k, _ in issues), sorted(issues)
    _, issues, *_ = crawl_once(site, ptype="publisher")
    assert ("sitemap_news", f"{H}/news.xml") in issues, sorted(issues)


def test_url_hygiene():
    pages = [_pg(f"{H}/ok-page"), _pg(f"{H}/Has/Upper"), _pg(f"{H}/under_score"),
             _pg(f"{H}/a//b"), _pg(f"{H}/q?id=3"), _pg(f"{H}/" + "x" * cs.URL_LONG),
             _pg(f"{H}/Gone_404", status=404, title=None, words=None),
             _pg(f"{H}/canon_elsewhere", canonical=f"{H}/ok-page")]
    got = {u.rsplit("/", 1)[1][:12]: d for k, u, d in cs.url_hygiene(pages) if k == "url_hygiene"}
    assert set(got) == {"Upper", "under_score", "b", "q?id=3", "x" * 12}, set(got)
    assert "대문자" in got["Upper"] and "밑줄" in got["under_score"] and "이중 슬래시" in got["b"]
    assert "쿼리" in got["q?id=3"]


def test_canonical_target_is_opened_once_more():
    pages = [_pg(f"{H}/a", canonical=f"{H}/moved"), _pg(f"{H}/b", canonical=f"{H}/gone"),
             _pg(f"{H}/c", canonical=f"{H}/d"), _pg(f"{H}/d", canonical=f"{H}/c"),
             _pg(f"{H}/e", canonical=f"{H}/f"), _pg(f"{H}/f", canonical=f"{H}/g"), _pg(f"{H}/g"),
             _pg(f"{H}/ok", canonical=f"{H}/g"), _pg(f"{H}/self", canonical=f"{H}/self"),
             _pg(f"{H}/far", canonical=f"{H}/unseen-404"), _pg(f"{H}/far2", canonical=f"{H}/unseen-ok"),
             _pg(f"{H}/out", canonical="https://other.com/x"), _pg(f"{H}/busy", canonical=f"{H}/limited"),
             _pg(f"{H}/moved", status=301, redirect_to=f"{H}/a2", title=None, words=None),
             _pg(f"{H}/gone", status=410, title=None, words=None),
             _pg(f"{H}/limited", status=429, title=None, words=None)]
    asked = []

    def fetch(u):
        asked.append(u)
        return {"status": 404 if "404" in u else 200, "final_url": u, "chain": []}
    got = {u.rsplit("/", 1)[1]: d for k, u, d in cs.canonical_targets(pages, fetch, home=f"{H}/")
           if k == "canonical_bad_target"}
    assert set(got) == {"a", "b", "c", "d", "e", "far"}, sorted(got)
    assert "리다이렉트" in got["a"] and "410" in got["b"] and "고리" in got["c"] and "또 다른" in got["e"]
    assert "HTTP 404" in got["far"]
    # 크롤이 안 본 내 주소만, 한 번씩 연다 — 남의 호스트는 두드리지 않는다
    assert sorted(asked) == [f"{H}/unseen-404", f"{H}/unseen-ok"], asked
    assert cs.canonical_targets(pages, fetch, home=f"{H}/", max_fetch=0) != [] and len(asked) == 2
    # 크롤이 실제로 적는다
    site = base_site(**{"/a": page("에이", head=f'<link rel="canonical" href="{H}/nowhere">')})
    _, issues, log, *_ = crawl_once(site)
    assert ("canonical_bad_target", f"{H}/a") in issues and log.count(f"{H}/nowhere") == 1, sorted(issues)


# ── 7. hreflang ─────────────────────────────────────────────────────────────
def test_hreflang_targets_sets_and_codes():
    pages = [
        _pg(f"{H}/a", hreflang=[["ko", f"{H}/a"], ["en", f"{H}/en/moved"], ["ja", f"{H}/ja/gone"],
                                ["de", f"{H}/de/hidden"], ["fr", f"{H}/fr/canon"],
                                ["es", f"{H}/es/busy"], ["it", "https://other.com/it"]]),
        _pg(f"{H}/en/moved", status=301, redirect_to=f"{H}/en/a", title=None, words=None),
        _pg(f"{H}/ja/gone", status=404, title=None, words=None),
        _pg(f"{H}/de/hidden", robots="noindex"), _pg(f"{H}/fr/canon", canonical=f"{H}/fr/x"),
        _pg(f"{H}/es/busy", status=429, title=None, words=None),
        # 묶음 안 http/https 섞임 · 끝 슬래시 엇갈림 · 코드 아닌 값
        _pg(f"{H}/m1", hreflang=[["ko", f"{H}/m1"], ["en", "http://s.kr/en/m1"]]),
        _pg(f"{H}/m2", hreflang=[["ko", f"{H}/m2/"], ["en", f"{H}/en/m2"], ["x-default", f"{H}/"]]),
        _pg(f"{H}/c1", hreflang=[["ko", f"{H}/c1"], ["eng", f"{H}/en/c1"], ["en-EU", f"{H}/eu/c1"]]),
        # 멀쩡한 묶음 — 홈('/')과 파일 꼴(.html)은 끝 슬래시 판정에서 뺀다
        _pg(f"{H}/fine", hreflang=[["ko", f"{H}/"], ["en", f"{H}/en/fine.html"], ["ja", f"{H}/ja/fine"]]),
    ]
    got = {(k, u): d for k, u, d in cs.hreflang_issues(pages)}
    t = got[("hreflang_bad_target", f"{H}/a")]
    assert "리다이렉트" in t and "외 3개" in t, t           # moved·gone·hidden·canon — busy(429)·남의 호스트는 뺀다
    assert not any(k == "hreflang_bad_target" and u != f"{H}/a" for k, u in got), got
    mixed = [d for (k, _), d in got.items() if k == "hreflang_mixed"]
    assert len(mixed) == 1 and "2장" in mixed[0] and "http 와 https" in mixed[0] and "끝 슬래시" in mixed[0], mixed
    code = [d for (k, _), d in got.items() if k == "hreflang_bad_code"]
    assert len(code) == 1 and "eng (1장)" in code[0] and "en-EU" in code[0], code


def test_hreflang_from_link_header_and_sitemap():
    assert cs.link_header_hreflang(
        '<https://e.com/f.pdf>; rel="alternate"; hreflang="en", '
        '</de/f.pdf>; rel="alternate"; hreflang="de-CH", <https://e.com/x>; rel="canonical"',
        "https://e.com/f.pdf") == [["en", "https://e.com/f.pdf"], ["de-CH", "https://e.com/de/f.pdf"]]
    assert cs.link_header_hreflang(None, "https://e.com/") == []
    # 사이트맵 xhtml:link 로만 알린 사이트: /a ↔ /en/a 는 서로 가리키고, /b → /en/b 는 되받지 않는다
    def u(loc, *alts):
        return "<url><loc>" + loc + "</loc>" + "".join(
            f'<xhtml:link rel="alternate" hreflang="{c}" href="{h}"/>' for c, h in alts) + "</url>"
    sm = sitemap([u(f"{H}/a", ("ko", f"{H}/a"), ("en", f"{H}/en/a")),
                  u(f"{H}/en/a", ("ko", f"{H}/a"), ("en", f"{H}/en/a")),
                  u(f"{H}/b", ("ko", f"{H}/b"), ("en", f"{H}/en/b")), f"{H}/en/b", f"{H}/"],
                 ' xmlns:xhtml="http://www.w3.org/1999/xhtml"')
    site = base_site(robots=f"User-agent: *\nSitemap: {H}/sitemap.xml\n",
                     **{"/a": page("에이"), "/en/a": page("A", lang="en"), "/b": page("비"),
                        "/en/b": page("B", lang="en"),
                        "/f.pdf": (200, "%PDF", "application/pdf",
                                   {"headers": {"link": f'<{H}/en/f.pdf>; rel="alternate"; hreflang="en"'}})})
    site[f"{H}/sitemap.xml"] = sm
    run, issues, _, _, conn = crawl_once(site)
    src = {(r["url"], r["code"]): r["source"] for r in conn.execute(
        "SELECT url, code, source FROM crawl_hreflang WHERE run_id=?", (run["id"],))}
    assert src[(f"{H}/a", "en")] == "sitemap" and src[(f"{H}/f.pdf", "en")] == "header", src
    assert ("hreflang_no_return", f"{H}/b") in issues, "사이트맵으로 알린 hreflang 의 짝을 안 봤다"
    assert ("hreflang_no_return", f"{H}/a") not in issues


# ── 8. 같은 틀 근사 중복 · 템플릿 메타 ──────────────────────────────────────
def test_template_thin_near_duplicates_within_a_url_shape():
    # 지역명만 바꿔 끼운 60낱말짜리 페이지 — 지역명이 다섯 번(8%) 나온다
    blurb = ("{0}에서 믿을 수 있는 배관 수리를 찾으신다면 저희가 답입니다. 누수 탐지부터 배관 교체, 하수구 "
             "막힘과 보일러 배관까지 {0} 전 지역에 당일 방문합니다. 지금 전화로 무료 견적을 받아 보세요. "
             "경력 이십 년의 {0} 기사가 직접 가서 원인을 찾고, 고친 자리는 일 년 동안 다시 봐 드립니다. "
             "주말과 공휴일에도 같은 값으로 갑니다. {0} 고객 후기도 아래에서 확인하세요.")
    cities = ["서울", "부산", "대구", "인천"]
    pages = {f"/city/{i}": page(f"{c} 배관 수리", body=blurb.format(c)) for i, c in enumerate(cities)}
    posts = {f"/blog/{i}": page(f"글 {i}", body=" ".join(f"낱말{i}x{j}" for j in range(200)))
             for i in range(4)}
    run, issues, *_ = crawl_once(base_site(**pages, **posts))
    thin = {u: v["detail"] for (k, u), v in issues.items() if k == "template_thin"}
    assert list(thin) == [f"{H}/city/0"], thin
    assert "/city/* 꼴 4장 중 4장" in thin[f"{H}/city/0"], thin
    # 지문 — 띄어쓰기 없는 글도 조각이 나온다. 다른 글은 안 겹친다
    ja = "東京で信頼できる水道修理をお探しなら当社にお任せください。漏水調査から配管交換まで即日対応します。" * 3
    a, b = cs.body_sketch("東京" + ja), cs.body_sketch("大阪" + ja)
    assert a and cs.sketch_sim(a, b) >= cs.NEAR_DUP, cs.sketch_sim(a, b)
    assert cs.sketch_sim(cs.body_sketch(" ".join(f"w{i}" for i in range(300))),
                         cs.body_sketch(" ".join(f"v{i}" for i in range(300)))) == 0.0
    assert cs.body_sketch("짧은 글") == () and cs.body_hash("짧은 글") is None
    assert cs.body_hash("같은  본문, " * 40) == cs.body_hash("같은 본문 " * 40)
    # canonical 로 접은 중복·noindex 는 세지 않는다
    sk = cs.body_sketch(blurb.format("서울"))
    rows = [_pg(f"{H}/c/{i}", sketch=sk, tpl="/c/*", canonical=f"{H}/c/0" if i else None)
            for i in range(4)]
    assert cs.template_thin(rows) == []


def test_meta_template_site_ratio():
    def site_of(n_tpl, n_ok):
        pages = [_pg(f"{H}/t{i}", title=f"주제 {i}번 안내 | 브랜드", description=f"주제 {i}번 안내. 자세히 보기",
                     lang="ko") for i in range(n_tpl)]
        pages += [_pg(f"{H}/o{i}", title=f"다른 주제 {i} | 브랜드",
                      description=f"{i}번 글은 실제로 무엇을 다루는지 문장으로 풀어 쓴 설명입니다.", lang="ko")
                  for i in range(n_ok)]
        return cs.meta_template_issue(pages, f"{H}/")
    assert site_of(2, 3) == [], "두 장은 틀이 아니다"
    assert site_of(3, 40) == [], "10% 가 안 되면 사이트 단위로 말하지 않는다"
    got = site_of(3, 17)
    assert got and got[0][0] == "meta_template" and got[0][3] == "info" and "20장 중 3장(15%)" in got[0][2], got
    got = site_of(8, 12)
    assert got[0][3] == "warn" and scoring.DESC_TEMPLATE_KINDS["title_repeat"] in got[0][2], got
    # 크롤이 그 심각도 그대로 적는다 — 비율이 낮으면 한 단계 가볍게
    pages = {f"/t{i}": page(f"주제 {i}번 안내 | 브랜드", desc=f"주제 {i}번 안내 | 브랜드") for i in range(3)}
    pages.update({f"/o{i}": page(f"글 {i}") for i in range(17)})
    _, issues, *_ = crawl_once(base_site(**pages))
    assert issues[("meta_template", f"{H}/")]["severity"] == "info", issues.get(("meta_template", f"{H}/"))
    assert scoring.DESC_TEMPLATE_KINDS["same_as_title"] in issues[("meta_template", f"{H}/")]["detail"]


# ── 9. 신뢰 페이지 · 매장 찾기 · 지도 ───────────────────────────────────────
def test_trust_pages_and_locator():
    links = [{"url_from": f"{H}/", "url_to": f"{H}/contact", "anchor": "Contact", "is_internal": 1},
             {"url_from": f"{H}/", "url_to": f"{H}/p/77", "anchor": "개인정보처리방침", "is_internal": 1},
             {"url_from": f"{H}/", "url_to": "https://other.com/terms", "anchor": "Terms", "is_internal": 0},
             {"url_from": f"{H}/", "url_to": f"{H}/tostadas", "anchor": "메뉴", "is_internal": 1}]
    found, issues = cs.trust_pages([_pg(f"{H}/")], links, f"{H}/")
    assert found["contact"] == f"{H}/contact" and found["privacy"] == f"{H}/p/77", found
    assert found["terms"] is None and found["reviews"] is None, found     # 남의 사이트 약관·'tos'tadas 는 아니다
    assert len(issues) == 1 and issues[0][0] == "trust_missing", issues
    assert "이용약관" in issues[0][2] and "후기" in issues[0][2] and "연락처" not in issues[0][2]
    # 다른 언어의 말로도 찾는다 — 표는 언어를 가리지 않는 한 벌이다
    for path, anchor, kind in (("/kontakt", "", "contact"), ("/x", "お問い合わせ", "contact"),
                               ("/datenschutz", "", "privacy"), ("/x", "利用規約", "terms"),
                               ("/x", "Opiniones de clientes", "reviews"), ("/agb", "", "terms")):
        f, _ = cs.trust_pages([], [{"url_from": f"{H}/", "url_to": f"{H}{path}", "anchor": anchor,
                                    "is_internal": 1}], f"{H}/")
        assert f[kind], (path, anchor, kind)
    # 매장 찾기 — 스크립트로만 그린 목록·robots 차단·noindex. 지점 링크가 HTML 에 있으면 멀쩡하다
    lk = lambda to, a="": {"url_from": f"{H}/", "url_to": to, "anchor": a, "is_internal": 1}
    pages = [_pg(f"{H}/store-locator", words=12, links_main=0, maps=1),
             _pg(f"{H}/locations", words=40, links_main=25),
             _pg(f"{H}/find", robots="noindex", words=500, links_main=9)]
    got = {u: d for k, u, d in cs.locator_issues(
        pages, [lk(f"{H}/store-locator"), lk(f"{H}/locations"), lk(f"{H}/find", "매장 찾기"),
                lk(f"{H}/stores", "Stores"), lk(f"{H}/blog/x", "글")],
        allowed=lambda u: not u.endswith("/stores"))}
    assert set(got) == {f"{H}/store-locator", f"{H}/find", f"{H}/stores"}, got
    assert "스크립트로만" in got[f"{H}/store-locator"] and "지도 임베드" in got[f"{H}/store-locator"]
    assert "robots.txt" in got[f"{H}/stores"] and "noindex" in got[f"{H}/find"]
    # 크롤이 지도 임베드를 세어 사실 줄에 싣는다
    emb = '<iframe src="https://www.google.com/maps/embed?pb=1"></iframe>'
    run, issues, *_ = crawl_once(base_site(**{"/visit": page("오시는 길", tail=emb),
                                               "/store-locator": page("매장", body="지도")}))
    facts = json.loads(run["facts_json"])
    assert facts["maps"] == 1 and any(ln["k"] == "지도 임베드" for ln in facts["lines"]), facts["lines"]
    assert ("locator_uncrawlable", f"{H}/store-locator") in issues, sorted(issues)


# ── 10. 다국어 판 사이 동등성 ───────────────────────────────────────────────
ART = ('<script type="application/ld+json">{"@context":"https://schema.org","@type":"Article",'
       '"headline":"x"%s}</script>')


def _ml_pair(path, ko_kw, en_kw, de=True):
    """한국어 기본 + 영어(+독일어) 한 글의 판들. hreflang 으로 서로 묶는다."""
    M = "https://m.kr"
    hl = [("ko", path), ("en", "/en" + path)] + ([("de", "/de" + path)] if de else [])
    out = {M + path: (200, cc._ml("ko", "글", desc="설명", hl=hl, **ko_kw), "text/html"),
           M + "/en" + path: (200, cc._ml("en", "Post", desc="desc", hl=hl, **en_kw), "text/html")}
    if de:
        out[M + "/de" + path] = (200, cc._ml("de", "Beitrag", desc="text", hl=hl,
                                             body="wort " * 220), "text/html")
    return out


def test_intl_parity_between_language_versions():
    M = "https://m.kr"
    h2 = lambda n: "".join(f"<h2>구간 {i}</h2><img src='{i}.png' alt='그림'>" for i in range(n))
    same = " ".join(f"untranslated{i}" for i in range(200))    # 제목(H1)만 옮기고 본문은 그대로
    site = {}
    # /same: 영어 판 본문이 한국어 판과 글자까지 같다. /thin: 영어 판에 구간·그림이 절반 이하
    site.update(_ml_pair("/same", {"body": same}, {"body": same}))
    site.update(_ml_pair("/thin", {"body": "본문 " * 220, "tail": h2(6)},
                         {"body": "word " * 220, "tail": h2(2)}))
    # /old: 원문은 11월에 고쳤고 영어 판은 6월 그대로. /fresh: 사흘 차이는 늦은 것이 아니다
    mod = lambda d: f'<meta property="article:modified_time" content="{d}T00:00:00+09:00">'
    site.update(_ml_pair("/old", {"body": "본문 " * 220, "head": mod("2026-11-10") + ART % ',"inLanguage":"ko"'},
                         {"body": "word " * 220 + " due 11/25/2026 and 12/31/2026",
                          "head": mod("2026-06-01") + ART % ""}))
    site.update(_ml_pair("/fresh", {"body": "본문 " * 220, "head": mod("2026-11-10")},
                         {"body": "wort " * 220, "head": mod("2026-11-07")}))
    site[M + "/"] = (200, cc._ml("ko", "홈", desc="홈", hl=[("ko", "/"), ("en", "/en/"), ("de", "/de/")],
                                 body="본문 " * 220,
                                 tail="".join(f'<a href="{p}">x</a>' for p in ("/same", "/thin", "/old", "/fresh"))),
                     "text/html")
    site[M + "/de/"] = (200, cc._ml("de", "Start", desc="s", hl=[("ko", "/"), ("en", "/en/"), ("de", "/de/")],
                                    body="wort " * 200 + " am 11/25/2026 und 12/31/2026",
                                    tail='<a href="/de/kontakt">Kontakt</a>'), "text/html")
    got = {(k, u): d for k, u, d in cc._intl_of(site) if k in cs.INTL_MORE_KINDS}
    kinds = {k for k, _ in got}
    assert kinds == {"intl_same_body", "intl_parity", "intl_stale", "intl_legal_missing", "intl_format",
                     "intl_no_inlanguage"}, kinds
    by = lambda kind: next(d for (k, _), d in got.items() if k == kind)
    assert "/en/same" in by("intl_same_body") and "1장" in by("intl_same_body")
    assert "H2 2개(기본 판 6개)" in got[("intl_parity", M + "/en/thin")], got
    assert "90일 이상 1장" in by("intl_stale") and "/en/old" in by("intl_stale"), by("intl_stale")
    assert "/fresh" not in by("intl_stale")
    # 독일어 판이 있는데 Impressum 이 없다. 영어 판에는 요구가 없다
    legal = [(u, d) for (k, u), d in got.items() if k == "intl_legal_missing"]
    assert len(legal) == 1 and "Impressum" in legal[0][1] and "/de/" in legal[0][0], legal
    # 날짜 — 독일어(일.월.년) 판의 월/일/년. 영어 판은 어느 나라 영어인지 몰라 판정하지 않는다
    fmt = [(u, d) for (k, u), d in got.items() if k == "intl_format"]
    assert len(fmt) == 1 and "/de/" in fmt[0][0] and "11/25/2026" in fmt[0][1], fmt
    # inLanguage — 글 마크업이 있는 2장 중 1장에 없다(절반 이상일 때 선다)
    assert "2장 중 1장" in by("intl_no_inlanguage")
    # Impressum 링크가 있으면 안 선다
    site[M + "/de/"] = (200, cc._ml("de", "Start", desc="s", hl=[("ko", "/"), ("en", "/en/"), ("de", "/de/")],
                                    body="wort " * 200, tail='<a href="/de/impressum">Impressum</a>'), "text/html")
    again = {k for k, _, _ in cc._intl_of(site)}
    assert "intl_legal_missing" not in again and "intl_format" not in again, again
    # 한 언어 사이트에서는 하나도 안 선다(문이 안 열린다)
    one = {M + "/": (200, cc._ml("de", "Start", desc="s", body="wort " * 200 + " 11/25/2026 12/31/2026"),
                     "text/html")}
    assert cc._intl_of(one, locales=("de-DE",)) == []
    # 판정 조각
    assert cs.num_date_style("3/14/2026, 14/3/2026, 3/4/2026") == (1, 1, "3/14/2026")
    assert cs.ld_inlanguage([{"@type": "MedicalClinic", "name": "x"}]) is None
    assert cs.ld_inlanguage([{"@graph": [{"@type": "WebPage", "inLanguage": "ko"}]}]) is True
    assert cs.ld_inlanguage([{"@type": ["Article"]}]) is False


# ── 11. 사이트 사실 줄 (정보 — 기회가 아니다) ───────────────────────────────
def test_site_facts_lines_record_presence_only():
    wk = "/.well-known/agent-card.json"
    site = base_site(robots=f"User-agent: *\nLicense: {H}/license.xml\n",
                     **{"/a": page("에이", tail='<form toolname="search"><input name="q"></form>'),
                        "/contact": page("문의")})
    site[f"{H}{wk}"] = (200, '{"name":"agent"}', "application/json")
    site[f"{H}/a.md"] = (200, "# 에이", "text/markdown")
    run, issues, log, _, _ = crawl_once(site)
    facts = json.loads(run["facts_json"])
    lines = {ln["k"]: ln for ln in facts["lines"]}
    assert all(set(ln) == {"g", "k", "v"} and ln["v"] for ln in facts["lines"]), facts["lines"]
    # 발견 파일은 표(WELL_KNOWN) 전부를 한 번씩 묻고, 있는 것만 이름으로 적는다
    assert [u for u in log if "/.well-known/" in u] == [f"{H}{p}" for p, _, _ in cs.WELL_KNOWN], log
    w = lines["/.well-known 발견 파일"]
    assert w["g"] == "agent" and "A2A 에이전트 카드" in w["v"] and f"없음 {len(cs.WELL_KNOWN) - 1}종" in w["v"], w
    assert lines["WebMCP 표식"]["v"] == "1장" and lines["WebMCP 표식"]["g"] == "agent"
    assert f"{H}/license.xml" in lines["RSL 라이선스"]["v"]
    assert ".md 형제 → 있음" in lines["Markdown 협상"]["v"] and "확인 못 함" in lines["Markdown 협상"]["v"]
    assert lines["robots.txt"]["v"] == "읽었습니다" and lines["robots.txt"]["g"] == "site"
    assert "연락처 /contact" in lines["신뢰 페이지"]["v"]
    # 이것들은 이슈로 서지 않는다 — 없는 것이 결함이 아니다
    agentish = ("well", "webmcp", "markdown", "rsl", "agent")
    assert not any(any(a in k for a in agentish) for k, _ in issues), sorted(issues)
    # 없는 사이트도 줄은 '없음'으로 선다(안 본 것과 없는 것을 가른다)
    run, *_ = crawl_once(base_site())
    lines = {ln["k"]: ln["v"] for ln in json.loads(run["facts_json"])["lines"]}
    assert lines["WebMCP 표식"] == "없음" and "License: 줄 없음" in lines["RSL 라이선스"], lines
    assert lines["/.well-known 발견 파일"] == f"없음 {len(cs.WELL_KNOWN)}종", lines
    # Markdown 협상 — 진짜 응답으로
    md = cs.probe_markdown(f"{H}/", lambda u, a: {"status": 200, "content_type":
                                                  "text/markdown" if a else "text/html", "text": "# x"},
                           f"{H}/a")
    assert md == {"accept": 1, "sibling": 0}, md
    # 응답 지연 — 가까운 순위법
    assert cs.percentile([100, 200, 300, 400], 0.5) == 200 and cs.percentile([100, 200, 300, 400], 0.95) == 400
    assert cs.percentile([], 0.5) is None
    f = cc.fact_lines({"latency": {"p50": 420, "p95": 1800, "n": 37}})
    assert any(ln["k"] == "응답 지연" and "0.4초" in ln["v"] and "1.8초" in ln["v"] for ln in f), f


# ── 12. 이름표 한 벌 ────────────────────────────────────────────────────────
def test_every_kind_crawl_site_emits_has_a_label_and_severity():
    import re
    src = Path(cs.__file__).read_text("utf-8")
    emitted = set(re.findall(r'\(\s*"([a-z0-9_]+)",\s*(?:[a-zA-Z_\[\]\'"{}.0-9()]+|f\{H\})', src)) \
        & {k for k in re.findall(r'"([a-z]+_[a-z0-9_]+)"', src)}
    emitted = {k for k in emitted if re.search(rf'\.append\(\(\s*"{k}"|return \[\(\s*"{k}"', src)}
    assert len(emitted) >= 18, sorted(emitted)
    assert emitted <= set(cc.ISSUE_KIND) and emitted <= set(cc.SEVERITY), \
        sorted(emitted - set(cc.ISSUE_KIND))
    assert set(cs.INTL_MORE_KINDS) <= set(cc.INTL_KINDS) and set(cs.INTL_MORE_KINDS) <= emitted
    # 기회로 올라가는 것(bad)은 robots.txt 서버 오류 하나뿐이다 — 틀·참고 갈래가 기회 목록을 메우지 않게
    new = emitted | {"robots_5xx", "robots_unknown", "rate_limited", "soft_404"}
    assert {k for k in new if cc.SEVERITY[k] == "bad"} == {"robots_5xx"}
    assert set(scoring.DESC_TEMPLATE_KINDS) == {"same_as_title", "brand_tail", "title_repeat"}


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)} passed")
