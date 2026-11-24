#!/usr/bin/env python3
"""남의 페이지 한 장을 무료 GET 으로 열어 "우리에게 링크했나"를 본다.

링크 없는 언급(backlink_mention)·경쟁사만 받는 링크(backlink_prospect)는 닫히는 근거가 여태
**다음 유료 백링크 수집**(30일 주기)뿐이었다. 부탁해서 링크가 걸렸어도 한 달을 기다렸다.
언급한 페이지의 주소는 이미 갖고 있다 — 그 한 장을 그냥 열어 보면 된다.

답은 넷이고, 넷을 뭉치지 않는다:

  linked    우리 도메인으로 가는 <a href> 가 있고 순위로 넘어간다
  nofollow  링크는 있는데 rel(nofollow·ugc·sponsored) 이거나 페이지가 meta robots nofollow 다.
            **어느 쪽인지는 따로 적는다**(rel_nofollow · meta_nofollow) — rel 은 그 링크 하나의
            일이고 meta 는 그 페이지 전체의 일이라 부탁할 말이 다르다
  no_link   페이지를 읽었고 본문이 있는데 우리 링크가 없다
  unknown   **확인 불가** — 403·429(크롤러 차단)·그 밖의 오류 응답·연결 실패·HTML 아님·
            JS 껍데기(정적 HTML 에 본문이 없다). '링크 없음'으로 적으면 이미 링크한 곳에
            또 부탁하게 되고, '링크 있음'으로 적으면 안 된 일을 닫는다

받기는 http_adapter.get 한 곳이다(주소 검사·연결 재사용이 거기 있다). 막힘의 정본은
collect_page.BLOCK_STATUS, JS 껍데기의 정본은 collect_page.audit_html 의 js_shell 이다 —
여기서 문턱을 다시 적지 않는다.

Usage:
  python link_check.py            # self-check (네트워크 안 탐)
"""
from __future__ import annotations

import sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin

sys.path.insert(0, str(Path(__file__).parent))

STATES = ("linked", "nofollow", "no_link", "unknown")
# rel 값 중 "순위로 넘기지 말라"는 뜻인 것. 구글 문서의 세 값 그대로다:
# https://developers.google.com/search/docs/crawling-indexing/qualify-outbound-links
REL_NOFOLLOW = frozenset({"nofollow", "ugc", "sponsored"})
UA = {"User-Agent": "seo-miner/link-check (+https://github.com/seanjr2847/seo-miner)"}
MAX_HTML = 2_000_000
TIMEOUT = 10        # 남의 페이지 한 장. 수십 장을 여는 자리라 내 페이지 점검(20초)보다 짧게 끊는다


class _Links(HTMLParser):
    """<a href> 와 meta robots 만 줍는다."""

    def __init__(self, base: str):
        super().__init__(convert_charrefs=True)
        self.base = base
        self.links: list[tuple[str, frozenset]] = []    # (절대 주소, rel 낱말들)
        self.meta_nofollow = False

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "a" and a.get("href"):
            try:
                href = urljoin(self.base, a["href"].strip())
            except ValueError:
                return
            self.links.append((href, frozenset(a.get("rel", "").lower().split())))
        elif tag == "meta" and a.get("name", "").lower() in ("robots", "googlebot"):
            words = {w.strip() for w in a.get("content", "").lower().split(",")}
            if words & {"nofollow", "none"}:
                self.meta_nofollow = True


def scan(html: str, base: str, own: str) -> dict:
    """HTML 한 장 → 우리 도메인으로 가는 링크 수(순위 반영 / rel 로 막힌 것)와 meta nofollow."""
    import scoring
    p = _Links(base)
    try:
        p.feed((html or "")[:MAX_HTML])
    except Exception:                    # noqa: BLE001 — 망가진 마크업도 읽은 데까지는 쓴다
        pass
    ours = [(h, rel) for h, rel in p.links if scoring.owns(h, own)]
    blocked = [x for x in ours if x[1] & REL_NOFOLLOW]
    return {"follow": len(ours) - len(blocked), "rel_blocked": len(blocked),
            "meta_nofollow": p.meta_nofollow}


def judge(status, content_type: str, html: str, url: str, own: str) -> dict:
    """응답 한 장 → {state, why, status, rel_nofollow, meta_nofollow}. 네트워크를 안 탄다."""
    import collect_page
    out = {"state": "unknown", "why": "", "status": status,
           "rel_nofollow": None, "meta_nofollow": None}
    if status is None:
        return {**out, "why": "응답이 없습니다(연결 실패·시간 초과)"}
    if status in collect_page.BLOCK_STATUS:
        return {**out, "why": f"응답 {status}: 그 사이트가 크롤러를 막았습니다"}
    if status >= 400:
        return {**out, "why": f"응답 {status}: 페이지를 열 수 없습니다"}
    if "html" not in (content_type or "").lower():
        return {**out, "why": f"HTML 이 아닙니다({content_type or '종류 없음'})"}
    s = scan(html, url, own)
    out["meta_nofollow"] = int(s["meta_nofollow"])
    if s["follow"] or s["rel_blocked"]:
        out["rel_nofollow"] = int(not s["follow"])
        if s["follow"] and not s["meta_nofollow"]:
            return {**out, "state": "linked", "why": "우리에게 가는 링크가 있습니다"}
        how = [w for w, on in (("링크의 rel(nofollow·ugc·sponsored)", not s["follow"]),
                               ("페이지의 meta robots nofollow", s["meta_nofollow"])) if on]
        return {**out, "state": "nofollow",
                "why": "링크는 있지만 순위로 넘어가지 않게 표시돼 있습니다: " + " · ".join(how)}
    # 링크가 안 보인다 — 본문이 정적 HTML 에 없으면(JS 가 그린다) '없다'고 말할 수 없다.
    if collect_page.audit_html(url, html, status).get("js_shell"):
        return {**out, "why": "자바스크립트가 본문을 그리는 페이지라 링크를 볼 수 없습니다"}
    return {**out, "state": "no_link", "why": "페이지를 읽었지만 우리에게 가는 링크가 없습니다"}


def check(url: str, own: str, *, timeout=None) -> dict:
    """URL 한 장을 열어 판정한다. 어떤 실패도 예외로 올리지 않는다 — 전부 unknown 이다."""
    import http_adapter
    try:
        r = http_adapter.get(url, headers=UA, timeout=timeout or TIMEOUT)
    except Exception as e:               # noqa: BLE001 — 못 연 것이 곧 답(확인 불가)이다
        return {**judge(None, "", "", url, own), "why": f"열지 못했습니다({type(e).__name__})"}
    html = r.text if "html" in (r.content_type or "").lower() and r.status < 400 else ""
    return judge(r.status, r.content_type, html, url, own)


def _selfcheck() -> None:
    own = "bt.com"
    body = "<p>" + "본문 낱말 " * 200 + "</p>"
    page = lambda inner, head="": f"<html><head>{head}</head><body>{body}{inner}</body></html>"
    j = lambda st, html, ct="text/html; charset=utf-8": judge(st, ct, html, "https://n.com/a", own)

    assert j(200, page('<a href="https://www.bt.com/x">bt</a>'))["state"] == "linked"
    assert j(200, page('<a href="//sub.bt.com/">bt</a>'))["state"] == "linked", "하위 도메인·스킴 없는 주소"
    assert j(200, page('<a href="https://notbt.com/">x</a>'))["state"] == "no_link", "꼬리만 같은 남의 도메인"

    # rel 과 meta 는 따로 적는다 — 둘은 다른 일이다.
    r = j(200, page('<a rel="NoFollow noopener" href="https://bt.com/">bt</a>'))
    assert (r["state"], r["rel_nofollow"], r["meta_nofollow"]) == ("nofollow", 1, 0), r
    r = j(200, page('<a href="https://bt.com/">bt</a>', '<meta name="robots" content="index, nofollow">'))
    assert (r["state"], r["rel_nofollow"], r["meta_nofollow"]) == ("nofollow", 0, 1), r
    for rel in ("ugc", "sponsored"):
        assert j(200, page(f'<a rel="{rel}" href="https://bt.com/">bt</a>'))["state"] == "nofollow", rel
    # 순위 반영 링크가 하나라도 있으면 rel 로 막힌 다른 링크가 있어도 linked 다.
    r = j(200, page('<a rel="nofollow" href="https://bt.com/a">a</a><a href="https://bt.com/b">b</a>'))
    assert (r["state"], r["rel_nofollow"]) == ("linked", 0), r

    # 확인 불가 — 403·429·그 밖의 오류·연결 실패·HTML 아님·JS 껍데기. '링크 없음'이 아니다.
    for st in (403, 429, 404, 500, None):
        assert j(st, "")["state"] == "unknown", st
    assert "크롤러" in j(403, "")["why"] and "크롤러" not in j(404, "")["why"]
    assert j(200, "%PDF", "application/pdf")["state"] == "unknown"
    shell = "<html><body><div id='root'></div>" + "<script src='a.js'></script>" * 6 + "</body></html>"
    assert j(200, shell)["state"] == "unknown", "JS 껍데기를 '링크 없음'으로 읽었다"
    # 껍데기여도 링크가 정적 HTML 에 보이면 있는 것이다.
    assert j(200, shell.replace("</body>", '<a href="https://bt.com/">x</a></body>'))["state"] == "linked"

    # check() 는 전송 실패를 삼켜 unknown 으로 돌려준다.
    import http_adapter

    def boom(url, headers, timeout):
        raise ConnectionError("x")
    with http_adapter.use(boom):
        assert check("https://n.com/a", own)["state"] == "unknown"
    with http_adapter.use(lambda url, headers, timeout: http_adapter.Fetched(
            url, url, 200, (), "text/html", text=page('<a href="https://bt.com/">bt</a>'))):
        assert check("https://n.com/a", own)["state"] == "linked"
    with http_adapter.use(lambda url, headers, timeout: http_adapter.Fetched(
            url, url, 429, (), "text/html", text="slow down")):
        assert check("https://n.com/a", own)["state"] == "unknown"
    print("link_check self-check ok")


if __name__ == "__main__":
    _selfcheck()
