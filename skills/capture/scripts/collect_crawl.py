#!/usr/bin/env python3
"""사이트 크롤 — 전수를 봐야만 나오는 것 (외부 호출 0, 내 사이트만).

page_audits(collect_page)는 기회가 걸린 페이지 20개를 깊게 본다. 이쪽은 반대다:
사이트맵·내부 링크를 따라 넓게 돌아 깨진 내부 링크·리다이렉트 사슬·고아 페이지처럼
**한 장만 보면 절대 안 나오는 것**을 잡는다. 회차(crawl_runs)로 남기는 이유는
하나다 — 지난번 대비 새로 깨진 것.

한 장을 읽는 법(제목·설명·H1·본문 길이·스키마·alt)은 collect_page._Page 가 이미
갖고 있다. 여기서는 그것을 상속해 링크(앵커·nofollow)만 얹는다 — HTML 파서를
두 벌 만들지 않는다.

이슈는 크롤이 끝난 뒤 **SQL 로 뽑아 crawl_issues 에 저장한다**. 화면에서 다시
계산하지 않는 이유는 하나다 — 저장돼 있어야 회차 비교(compare)가 된다.

Usage:
  python collect_crawl.py --project NAME [--limit N] [--max-depth N] [--dry-run]
  python collect_crawl.py                                  # self-check
"""
from __future__ import annotations

import argparse
import json
import re
import socket
import ssl
import sys
import time
import uuid
import xml.etree.ElementTree as ET
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib import robotparser
from urllib.parse import urljoin, urlsplit, urlunsplit

sys.path.insert(0, str(Path(__file__).parent))

import collect_page  # noqa: E402
import collector  # noqa: E402
import crawl_site  # noqa: E402
import db  # noqa: E402
import fanout  # noqa: E402
import http_adapter  # noqa: E402
import scoring  # noqa: E402
import serp_adapter  # noqa: E402

# 자기 신원을 밝힌다 — 내 사이트를 여는 것이라 위장할 이유가 없고, 로그에서
# 이 요청이 무엇인지 알아볼 수 있어야 한다(robots 규칙을 쓸 때도 그렇다).
UA_NAME = "seo-miner"
UA = {"User-Agent": "seo-miner/crawl (+https://github.com/seanjr2847/seo-miner)"}

# ── 임계값 한 자리 ───────────────────────────────────────────────────────────
THIN_WORDS = 200          # 이보다 짧으면 thin_content
MAX_HOPS = 2              # 리다이렉트 홉이 이 수 이상이면 redirect_chain
MAX_SITEMAPS = 20         # sitemap index 에서 따라갈 하위 사이트맵 수
MAX_SITEMAP_URLS = 5000   # 사이트맵에서 읽어 들일 URL 상한 (limit 이 다시 자른다)
ANCHOR_MAX = 200          # 앵커 텍스트 저장 길이
# 속도 제한(429·430·503 — crawl_site.RATE_STATUS)에 걸린 장은 물러섰다가 다시 묻는다.
RETRY_MAX = 2             # 다시 묻는 횟수
RETRY_WAIT = 2.0          # 첫 대기(초). 다음은 두 배. Retry-After 가 있으면 그 값(상한까지)
RETRY_WAIT_MAX = 30.0
RATE_GIVE_UP = 10         # 연달아 이만큼 속도 제한이면 더 기다리지 않는다(사이트가 통째로 막는 중이다)
CRAWL_DELAY_MAX = 10.0    # robots.txt Crawl-delay 를 따르는 상한(초) — 그보다 크면 이 값으로 돈다
ROBOTS_RETRY_WAIT = 2.0   # robots.txt 가 5xx 면 한 번 더 묻기 전에 쉬는 시간
_sleep = time.sleep       # 검사가 갈아끼운다 — 물러서기를 실제로 기다리지 않게

# 갈래 이름·왜 문제인지의 정본. 화면(site.html 의 CR_KIND)과 요청문(brief) 둘 다
# 이것을 쓴다 — 한국어 라벨이 화면 JS 안에만 있던 동안, 요청문은 같은 갈래를
# 'dup_title' 이라는 영어 kind 로 사람에게 내보냈다.
ISSUE_KIND = {
    "http_error": ["열리지 않는 페이지",
                   "그 URL 이 4xx·5xx 를 돌려줍니다. 살리거나 지우고, 가리키던 링크를 고치세요."],
    "broken_internal": ["깨진 내부 링크",
                        "내 페이지가 내 페이지를 잘못 가리킵니다. 링크만 고치면 되는, 가장 싼 자리입니다."],
    "redirect_chain": ["리다이렉트 사슬",
                       "두 번 이상 넘어갑니다. 처음 링크를 최종 URL 로 바로 바꾸세요."],
    "orphan": ["고아 페이지",
               "사이트맵에는 있는데 어디서도 링크하지 않습니다. 링크가 없으면 크롤러도 사람도 못 옵니다."],
    "dup_title": ["제목 중복",
                  "여러 페이지가 같은 제목을 씁니다. 어느 쪽을 띄울지 구글이 못 고릅니다."],
    "dup_description": ["설명 중복", "여러 페이지가 같은 meta description 을 씁니다."],
    "missing_title": ["제목 없음",
                      "title 이 비어 있습니다. 검색 결과에 나갈 한 줄이 없다는 뜻입니다."],
    "missing_description": ["설명 없음",
                            "meta description 이 없어 구글이 본문에서 임의로 잘라 씁니다."],
    "missing_h1": ["H1 없음", "본문 제목이 없습니다. 무엇에 관한 페이지인지 첫 신호가 빠집니다."],
    "thin_content": ["본문이 얇음", "본문이 너무 짧습니다. 답이 될 만큼 쓰였는지 보세요."],
    "noindex": ["색인 제외 표시", "meta robots 가 noindex 입니다. 의도한 것이면 그대로 두세요."],
    "canonical_mismatch": ["대표 URL 엇갈림",
                           "canonical 이 자기 자신도, 크롤한 어느 URL 도 아닙니다."],
    "img_no_alt": ["alt 없는 이미지",
                   "이미지 설명이 비었습니다. 접근성이 먼저고, 이미지 검색은 덤입니다."],
    "hreflang_no_return": ["hreflang 짝이 안 맞음",
                           "이쪽은 저쪽을 가리키는데 저쪽은 이쪽을 안 가리킵니다. "
                           "구글은 그 짝을 통째로 버립니다."],
    # ── 다국어 (intl_issues) — 여러 언어 판이 있는 사이트에서만 선다 ──
    "intl_untranslated": ["번역 안 된 글자",
                          "다른 언어 페이지에 사이트 기본 언어 글자가 남았습니다. 그 언어로 읽는 "
                          "사람에게는 안 읽히고, 구글은 페이지 언어를 헷갈립니다."],
    "intl_lang_leak": ["다른 언어로 새는 링크",
                       "이 페이지가 다른 언어 페이지로 링크합니다. 누른 사람은 못 읽는 페이지에 "
                       "닿습니다. 같은 언어 판이 있으면 그쪽으로 바꾸세요."],
    "intl_missing_translation": ["빠진 번역",
                                 "다른 언어 판은 있는데 몇 언어 판이 없습니다. 그 언어로 찾는 "
                                 "사람에게는 이 내용이 없는 셈입니다."],
    "intl_no_switch_link": ["언어 전환 링크 없음",
                            "hreflang 으로 다른 언어 판을 알렸는데 그리로 가는 <a href> 링크가 "
                            "없습니다. 버튼·스크립트로만 바꾸면 구글이 못 따라갑니다."],
    "intl_xdefault": ["x-default 가 기본 언어 판",
                      "사이트가 지원하지 않는 언어의 사용자에게 기본 언어 판이 뜹니다. 영어 판이 "
                      "있으면 x-default 는 영어 판이 낫습니다."],
    "intl_desc_repeat": ["설명 문구 되풀이",
                         "meta description 이 같은 구절을 연달아 되풀이합니다. 틀에 제목이 두 번 "
                         "끼인 흔적이고, 검색 결과에 그대로 보입니다."],
    "intl_schema_lang": ["구조화 데이터 언어 불일치",
                         "다른 언어 페이지의 업체 이름·주소(구조화 데이터)가 사이트 기본 언어로 "
                         "적혀 있습니다. 그 언어 검색의 업체 정보에 그대로 쓰입니다."],
    # ── 다국어 판 사이 동등성 (crawl_site.intl_more) ──
    "intl_same_body": ["번역 안 된 본문",
                       "다른 언어 판의 본문이 기본 언어 판과 같습니다. 구글은 둘을 "
                       "중복으로 접고, 그 언어로 찾는 사람은 못 읽습니다."],
    "intl_no_inlanguage": ["inLanguage 없음",
                           "글 마크업에 어느 언어인지가 없습니다. 언어 판이 여럿이면 판마다 적어 "
                           "두는 편이 안전합니다."],
    "intl_parity": ["언어 판 사이 분량 차이",
                    "다른 언어 판이 기본 판보다 구간(H2)·이미지가 절반 이하입니다. 옮기다 빠진 "
                    "구간이 있는지 보세요."],
    "intl_stale": ["원문보다 오래된 번역",
                   "원문을 고친 뒤 번역 판은 그대로입니다. 바뀐 내용이 그 언어에는 안 전해졌습니다."],
    "intl_legal_missing": ["로케일 법적 고지 없음",
                           "그 언어권에서 사업자 사이트에 요구되는 고지 페이지가 안 보입니다."],
    "intl_format": ["원문 순서 그대로인 날짜",
                    "그 언어가 쓰지 않는 순서(월/일/년)의 숫자 날짜가 남았습니다. 읽는 사람이 "
                    "날과 달을 바꿔 읽습니다."],
    # ── 사이트 단위 (crawl_site) — robots·사이트맵 파일·호스트·탐침·여러 장을 맞댄 것 ──
    "robots_5xx": ["robots.txt 서버 오류",
                   "robots.txt 가 5xx 를 돌려줍니다. 규칙을 지키는 크롤러는 이 상태에서 사이트 "
                   "전체를 멈춥니다(구글은 처음 12시간 크롤을 멈추고 30일까지 마지막 사본을 씁니다)."],
    "robots_unknown": ["robots.txt 판정 불가",
                       "robots.txt 를 받지 못했습니다(429·응답 없음). 막힌 것인지 열린 것인지 이번 "
                       "회차로는 말할 수 없습니다."],
    "sitemap_unreadable": ["읽히지 않는 사이트맵",
                           "사이트맵 파일이 열리지 않거나 형식이 깨졌습니다. 그 안의 주소는 "
                           "검색엔진에도 안 전해집니다."],
    "sitemap_hygiene": ["사이트맵 규격 밖",
                        "사이트맵에 규격 밖의 것이 있습니다(한도 초과·다른 호스트·추적 파라미터·"
                        "겹친 주소)."],
    "sitemap_lastmod": ["믿기 어려운 lastmod",
                        "구글은 lastmod 가 일관되게 정확할 때만 씁니다. 틀린 값이 섞이면 전부 안 "
                        "믿습니다."],
    "sitemap_bad_url": ["사이트맵에 넣으면 안 되는 주소",
                        "사이트맵은 색인해 달라는 목록입니다. 리다이렉트·오류·noindex·canonical 이 "
                        "다른 주소는 빼세요."],
    "not_in_sitemap": ["사이트맵에 빠진 페이지",
                       "색인될 수 있는 페이지인데 사이트맵에 없습니다. 일부러 뺀 것이 아니면 넣으세요."],
    "sitemap_news": ["뉴스 사이트맵 규칙 밖",
                     "뉴스 사이트맵은 한 파일에 1,000개까지, 최근 2일 기사만 싣습니다."],
    "rate_limited": ["속도 제한으로 못 본 페이지",
                     "서버가 429·503 으로 답해 이번 회차에 못 봤습니다. 그 페이지의 결함이 아니라 "
                     "판정하지 않았습니다."],
    "soft_404": ["없는 주소에 200",
                 "없는 주소를 열어도 200 이 옵니다(소프트 404). 지운 페이지가 색인에서 안 빠지고 "
                 "깨진 링크도 안 드러납니다."],
    "host_http": ["http 주소가 그대로",
                  "http 판이 https 로 영구 리다이렉트되지 않습니다. 같은 페이지가 두 주소로 "
                  "색인될 수 있습니다."],
    "host_dup": ["www 와 비www 가 둘 다 열림",
                 "www 가 붙은 주소와 안 붙은 주소가 둘 다 열립니다. 한쪽으로 301 하세요."],
    "cert_expiring": ["인증서 만료 임박",
                      "TLS 인증서가 곧 만료됩니다. 만료되면 브라우저와 크롤러가 사이트를 못 엽니다."],
    "url_hygiene": ["주소 꼴",
                    "색인될 주소에 파라미터·대문자·밑줄·이중 슬래시가 있습니다. 급하지 않습니다 — "
                    "새 주소부터 맞추세요."],
    "hreflang_bad_target": ["hreflang 대상이 잘못됨",
                            "hreflang 이 리다이렉트·오류·noindex·canonical 이 다른 주소를 "
                            "가리킵니다. 그 짝은 버려집니다."],
    "hreflang_mixed": ["hreflang 주소 꼴이 섞임",
                       "한 묶음 안에서 http/https 나 끝 슬래시가 엇갈립니다. 되받는 주소와 글자가 "
                       "달라 짝이 안 맞습니다."],
    "hreflang_bad_code": ["hreflang 값이 코드가 아님",
                          "언어는 ISO 639-1, 지역은 ISO 3166-1 Alpha 2 입니다. 명단에 없는 값은 "
                          "통째로 무시됩니다."],
    "template_thin": ["같은 틀의 거의 같은 본문",
                      "같은 주소 꼴의 페이지들이 이름만 바꾼 같은 글입니다. 구글은 이런 묶음을 "
                      "도어웨이로 봅니다."],
    "meta_template": ["틀에서 나온 설명",
                      "meta description 이 손으로 쓴 문장이 아니라 제목을 되풀이한 틀입니다. 검색 "
                      "결과에서 제목과 같은 말이 두 번 보입니다."],
    "trust_missing": ["신뢰 페이지가 안 보임",
                      "연락처·개인정보처리방침·약관·후기 중 크롤이 못 찾은 것이 있습니다. 있다면 "
                      "꼬리말에서 링크하세요."],
    "locator_uncrawlable": ["매장 찾기를 못 읽음",
                            "매장 찾기·지점 목록 페이지를 크롤러가 읽지 못합니다. 지점 페이지로 가는 "
                            "길이 여기뿐이면 지점이 색인에서 빠집니다."],
}

SEVERITY = {
    "http_error": "bad", "broken_internal": "bad", "redirect_chain": "warn",
    "orphan": "warn", "dup_title": "warn", "dup_description": "warn",
    "missing_title": "bad", "missing_description": "warn", "missing_h1": "warn",
    "thin_content": "warn", "noindex": "warn", "canonical_mismatch": "warn",
    "img_no_alt": "info", "hreflang_no_return": "warn",
    "intl_untranslated": "warn", "intl_lang_leak": "warn",
    "intl_missing_translation": "info", "intl_no_switch_link": "warn",
    "intl_xdefault": "warn", "intl_desc_repeat": "warn", "intl_schema_lang": "info",
    "intl_same_body": "warn", "intl_no_inlanguage": "info", "intl_parity": "info",
    "intl_stale": "info", "intl_legal_missing": "info", "intl_format": "info",
    # 사이트 단위 — 'bad' 는 기회 목록(scoring.crawl_gaps)으로 올라간다. robots.txt 5xx 하나뿐이다:
    # 나머지는 한 줄이 사이트 전체를 말하거나(틀) 급하지 않아 [사이트 점검] 표에서 본다.
    "robots_5xx": "bad", "robots_unknown": "info",
    "sitemap_unreadable": "warn", "sitemap_hygiene": "warn", "sitemap_lastmod": "info",
    "sitemap_bad_url": "warn", "not_in_sitemap": "info", "sitemap_news": "warn",
    "rate_limited": "info", "soft_404": "warn",
    "host_http": "warn", "host_dup": "warn", "cert_expiring": "warn", "url_hygiene": "info",
    "hreflang_bad_target": "warn", "hreflang_mixed": "warn", "hreflang_bad_code": "warn",
    "template_thin": "warn", "meta_template": "warn",
    "trust_missing": "info", "locator_uncrawlable": "warn",
}

# 다국어 갈래 — 전부 사이트 여러 장을 맞대야 나온다(요청문 brief.SITE_ONLY_ISSUES 가 이것을 가리킨다).
INTL_KINDS = tuple(k for k in ISSUE_KIND if k.startswith("intl_"))


# ── URL ─────────────────────────────────────────────────────────────────────
def normalize(url: str, base: str | None = None) -> str | None:
    """같은 곳을 가리키는 글자를 한 벌로. 프래그먼트·기본 포트·빈 경로를 정리한다.

    http/https 가 아니면 None — mailto:·tel:·javascript: 는 크롤 대상이 아니다.
    """
    u = (url or "").strip()
    if not u:
        return None
    if base:
        u = urljoin(base, u)
    p = urlsplit(u)
    if p.scheme.lower() not in ("http", "https"):
        return None
    netloc = p.netloc.lower()
    for scheme, port in (("http", ":80"), ("https", ":443")):
        if p.scheme.lower() == scheme and netloc.endswith(port):
            netloc = netloc[: -len(port)]
    return urlunsplit((p.scheme.lower(), netloc, p.path or "/", p.query, ""))


def _home_url(domain: str) -> str | None:
    """projects.domain('clinic.kr' 또는 'https://clinic.kr/') → 홈 URL."""
    d = (domain or "").strip()
    if not d:
        return None
    return normalize(d if "://" in d else "https://" + d)


# ── HTML ────────────────────────────────────────────────────────────────────
# role 로만 틀임을 밝히는 요소 — <div role="navigation"> 처럼 태그 이름으로는 안 보인다.
_CHROME_ROLES = {"navigation", "banner", "contentinfo"}


class _CrawlPage(collect_page._Page):
    """collect_page._Page + 링크(목적지·앵커·nofollow·틀 안인가). 나머지는 부모 것 그대로.

    '틀 안'(메뉴·머리말·꼬리말·곁가지)을 링크마다 적는다. 안 적으면 공통 메뉴가 모든 글에서
    거는 링크와 본문이 문맥으로 거는 링크가 한 표에 섞여 — 요청문이 "이 페이지로 링크를 안
    건 글"을 영영 못 골랐다(고치기 요청문 57장 중 후보 표 0장)."""

    def __init__(self, base: str):
        super().__init__(base)
        self.links: list[tuple[str, str, bool, bool]] = []
        self.alts: list[str] = []             # 비지 않은 img alt — 다국어 검사가 글자 체계를 본다
        self._a: list | None = None
        self._roles: list[int] = []           # role=navigation 등이 열린 스택 깊이
        self._main: list[str] = []            # 틀(메뉴·머리말·꼬리말) 밖 글자 — 본문 지문의 재료
        self.links_main = 0                   # 틀 밖 링크 수
        self.maps = 0                         # 지도 임베드(iframe) 수
        self.webmcp = False                   # WebMCP 표식(form toolname · modelContext)

    def handle_starttag(self, tag, attrs):
        super().handle_starttag(tag, attrs)
        a0 = {k.lower(): (v or "") for k, v in attrs}
        if a0.get("role", "").strip().lower() in _CHROME_ROLES:
            self._roles.append(len(self._stack))
        if tag == "img" and a0.get("alt", "").strip() and len(self.alts) < 300:
            self.alts.append(a0["alt"].strip())
        if tag == "iframe" and crawl_site.MAP_EMBED.search(a0.get("src", "")):
            self.maps += 1
        elif tag == "form" and "toolname" in a0:
            self.webmcp = True
        if tag != "a":
            return
        self._close_a()                       # <a/> 처럼 안 닫힌 것이 다음 앵커로 새지 않게
        a = {k.lower(): (v or "") for k, v in attrs}
        href = a.get("href", "").strip()
        if not href or href.startswith("#"):
            return
        to = normalize(href, self.base)
        if to:
            self._a = [to, [], "nofollow" in a.get("rel", "").lower(),
                       self._in_chrome() or bool(self._roles)]

    def handle_data(self, data):
        super().handle_data(data)
        if self._a:
            self._a[1].append(data)
        if not (set(self._stack) & collect_page.SKIP_TEXT):
            if not self._roles and not self._in_chrome():
                self._main.append(data)
        elif "modelContext" in data and self._stack and self._stack[-1] == "script":
            self.webmcp = True

    def handle_endtag(self, tag):
        super().handle_endtag(tag)
        self._roles = [d for d in self._roles if d <= len(self._stack)]
        if tag == "a":
            self._close_a()

    def _close_a(self) -> None:
        if not self._a:
            return
        to, buf, nofollow, chrome = self._a
        self.links.append((to, " ".join("".join(buf).split())[:ANCHOR_MAX], nofollow, chrome))
        self.links_main += int(not chrome)
        self._a = None


def parse_page(url: str, html: str) -> dict:
    """HTML 한 장 → crawl_pages 컬럼 + links. 네트워크를 타지 않는다."""
    p = _CrawlPage(url)
    try:
        p.feed(html[: collect_page.MAX_HTML])
    except Exception as e:                    # 망가진 마크업에도 여기까지 읽은 것은 남긴다
        print(f"  ! {url}: 파싱 도중 중단 ({e})", file=sys.stderr)
    p._close_a()
    text = " ".join(p._text)
    # 틀 밖 글자 — 시맨틱 태그 없이 짠 페이지는 틀을 못 가르니 전체 글자가 그대로 본문이다
    main = " ".join(p._main) if any(x.strip() for x in p._main) else text
    return {"title": p.title, "description": p.meta_description,
            "h1": p.h1[0] if p.h1 else None,
            "canonical": p.canonical, "robots": p.robots, "words": p.words,
            "schema_types": ",".join(sorted(set(p.schema))) or None,
            "images_no_alt": p.images_no_alt, "links": p.links,
            # 상호 참조(A→B 면 B→A)는 **여러 장을 같이 봐야** 안다. 페이지 감사는
            # 한 장씩이라 자기 참조·코드·x-default 까지만 본다 — 전수 크롤만이
            # "저쪽이 이쪽을 도로 안 가리킨다" 를 말할 수 있다.
            "hreflang": p.hreflang,
            # 다국어 검사(intl_issues)의 재료 — 저장하지 않고 메모리에서만 판정한다(판정 결과는
            # crawl_issues 가 정본). 본문 전체를 들고 다니면 5000장에서 수백 MB 라, 여기서
            # 글자 체계별 발췌·글자 수로 줄여 둔다. 어느 언어가 '기본'인지는 사이트 전체를
            # 봐야 알기 때문에 체계를 가리지 않고 라틴 밖의 것을 다 적는다.
            "lang": p.html_lang,
            "scripts": {"title": _script_bits(p.title or ""),
                        "description": _script_bits(p.meta_description or ""),
                        "text": _script_bits(text, long=True),
                        "alt": _script_bits(" | ".join(p.alts))},
            "letters": sum(1 for ch in text if ch.isalpha()),
            "ld_org": _ld_org(p.ld_blocks),
            # 여러 장을 맞대는 판정(crawl_site)의 재료. body_hash·modified·h2s·images 는 표에
            # 남고(회차 비교), 지문(sketch)은 메모리에서만 쓴다.
            "body_hash": crawl_site.body_hash(main), "sketch": crawl_site.body_sketch(main),
            "modified": p.modified, "h2s": len(p.h2), "images": p.images,
            "maps": p.maps, "webmcp": p.webmcp, "links_main": p.links_main,
            "ld_inlang": crawl_site.ld_inlanguage(p.ld_blocks),
            "num_dates": crawl_site.num_date_style(main)}


# ── 다국어 재료 (한 장) ──────────────────────────────────────────────────────
# "기본 언어 글자가 남았나"는 글자의 **체계**로만 가른다 — 외부 호출도 사전도 없다.
# 라틴은 표에 없다: 기본 언어가 라틴 문자면 브랜드명·고유명사가 어느 언어 페이지에나
# 정상으로 들어가 있어 남은 글자와 못 가른다. 그때 '번역 안 된 글자'는 건너뛴다.
# (체계 이름, 글자 범위, 음절 문자인가 — 음절 문자는 한 덩어리 글자 수로, 자모 문자는 낱말 수로 센다)
_SCRIPTS = (
    ("hangul", "\uac00-\ud7a3", True),
    ("kana", "\u3040-\u30ff", True),
    ("han", "\u4e00-\u9fff", True),
    ("thai", "\u0e00-\u0e7f", True),
    ("arabic", "\u0600-\u06ff", False),
    ("cyrillic", "\u0400-\u04ff", False),
    ("hebrew", "\u0590-\u05ff", False),
    ("devanagari", "\u0900-\u097f", False),
    ("greek", "\u0370-\u03ff", False),
)
# 언어 → 그 언어를 가르는 체계. 일본어는 가나로 가른다 — 한자는 중국어와 같이 쓴다.
_LANG_SCRIPT = {"ko": "hangul", "ja": "kana", "zh": "han", "th": "thai",
                "ar": "arabic", "fa": "arabic", "ur": "arabic",
                "ru": "cyrillic", "uk": "cyrillic", "bg": "cyrillic", "sr": "cyrillic",
                "kk": "cyrillic", "mn": "cyrillic", "be": "cyrillic",
                "he": "hebrew", "hi": "devanagari", "mr": "devanagari", "ne": "devanagari",
                "el": "greek"}
# 괄호 속 짧은 글은 풀이다 — "기미 (gimi)", "News（메디소비자뉴스）", "島山大路（도산대로）".
# theotherskin 의 외국어 판은 한국어 고유명사를 일부러 괄호에 병기했다. 번역을 안 한 것이 아니다.
_GLOSS = re.compile(r"[(\uff08][^()\uff08\uff09]{0,40}[)\uff09]")
_PHRASE = {name: (re.compile(rf"(?:\d+\s*)?[{rng}]+(?:[\s\d.,·:/\-]+[{rng}]+)*"),
                  re.compile(rf"[{rng}]+"))
           for name, rng, _ in _SCRIPTS}
INTL_EXCERPTS = 3          # 체계·자리마다 남기는 발췌 수
INTL_EXCERPT_LEN = 40
# 언어 선택 메뉴에 그 언어 글자로 적히는 언어 이름(원어 이름). 라틴 문자 이름은 필요 없다 —
# '번역 안 된 글자'는 라틴 밖 체계만 센다. 다른 정본은 없다(brief.lang_label·serp_adapter.LOCALES
# 는 한국어로 부르는 이름이다). 국기 아이콘 전환의 alt="한국어" 와 나란히 놓인
# "简体中文 繁體中文" 이 외국어 판마다 '번역 안 된 글자'로 섰다(검토 2026-10-16).
LANG_ENDONYMS = (
    "한국어", "한국말", "조선어",
    "日本語", "にほんご",
    "中文", "简体中文", "繁體中文", "繁体中文", "简体", "繁體", "繁体", "汉语", "漢語",
    "华语", "華語", "普通话", "國語", "粵語", "粤语",
    "ไทย", "ภาษาไทย",
    "العربية", "عربي", "فارسی", "اردو",
    "русский", "українська", "български", "српски", "қазақша", "монгол", "беларуская",
    "עברית", "हिन्दी", "हिंदी", "मराठी", "नेपाली", "ελληνικά",
)
_ENDONYMS_LONGEST = sorted({n.casefold() for n in LANG_ENDONYMS}, key=len, reverse=True)


def _only_lang_names(phrase: str, rng: str) -> bool:
    """덩어리의 그 체계 글자가 전부 언어 이름인가 — "한국어", "简体中文 繁體中文"."""
    rest = phrase.casefold()
    for name in _ENDONYMS_LONGEST:
        rest = rest.replace(name, " ")
    return not re.search(f"[{rng}]", rest)


def _script_bits(text: str, *, long: bool = False) -> dict:
    """라틴 밖 글자 체계별 {"n": 글자 수, "ex": [발췌]}. 있는 체계만 담는다.

    long=True(본문)는 덩어리가 커야 발췌로 친다 — 음절 문자 6자 또는 두 낱말 이상.
    제목·설명·alt 는 짧아서 두 글자(자모 문자는 세 글자)만 넘으면 친다. 언어 이름뿐인
    덩어리(LANG_ENDONYMS — 언어 선택 단추·국기 alt)는 자리를 가리지 않고 뺀다.
    """
    out: dict = {}
    if not text:
        return out
    bare = _GLOSS.sub(" ", text)
    for name, rng, syllabic in _SCRIPTS:
        phrase, word = _PHRASE[name]
        n = len(re.findall(f"[{rng}]", text))
        if not n:
            continue
        ex: list[str] = []
        for m in phrase.finditer(bare):
            words = word.findall(m.group())
            size = sum(len(w) for w in words)
            if long:
                hit = len(words) >= 2 or size >= (6 if syllabic else 12)
            else:
                hit = size >= (2 if syllabic else 3)
            got = " ".join(m.group().split())[:INTL_EXCERPT_LEN]
            if hit and got not in ex and not _only_lang_names(m.group(), rng):
                ex.append(got)
                if len(ex) >= INTL_EXCERPTS:
                    break
        out[name] = {"n": n, "ex": ex}
    return out


# 업체 이름·주소를 싣는 타입 — collect_page 는 ld+json 을 JSON 으로 통째로 읽어 둔다
# (_Page.ld_blocks). 그래서 범위를 줄일 이유가 없다. 다만 JSON 으로 안 읽히는 블록은
# ld_blocks 에 없다(schema_broken 으로 개수만 센다) — 깨진 블록 속 이름은 못 본다.
_ORG_TYPES = {"Organization", "LocalBusiness", "MedicalClinic", "MedicalBusiness",
              "MedicalOrganization", "Hospital", "Physician", "Dentist", "Pharmacy",
              "Corporation", "Store", "Restaurant", "HealthAndBeautyBusiness", "BeautySalon",
              "DaySpa", "ProfessionalService", "EducationalOrganization", "Hotel", "LodgingBusiness"}
_ADDR_PARTS = ("streetAddress", "addressLocality", "addressRegion")


def _ld_org(blocks) -> list[list[str]]:
    """ld+json 의 업체(Organization 갈래) name·address. [[타입, 칸, 값], ...] 최대 6개.

    alternateName 은 보지 않는다 — 다른 언어 이름을 거기 두는 것이 정석이다."""
    out: list[list[str]] = []

    def walk(x, depth=0):
        if depth > 8 or len(out) >= 6:
            return
        if isinstance(x, list):
            for v in x:
                walk(v, depth + 1)
            return
        if not isinstance(x, dict):
            return
        t = x.get("@type")
        types = [t] if isinstance(t, str) else [s for s in (t or []) if isinstance(s, str)]
        org = next((s for s in types if s in _ORG_TYPES
                    or s.endswith(("Business", "Organization", "Clinic"))), None)
        if org:
            if isinstance(x.get("name"), str) and x["name"].strip():
                out.append([org, "name", x["name"].strip()[:80]])
            a = x.get("address")
            if isinstance(a, dict):
                a = " ".join(str(a[k]) for k in _ADDR_PARTS if isinstance(a.get(k), str))
            if isinstance(a, str) and a.strip():
                out.append([org, "address", a.strip()[:80]])
        for k, v in x.items():
            if k != "address" and isinstance(v, (dict, list)):
                walk(v, depth + 1)

    walk(list(blocks or []))
    return out[:6]


# ── 가져오기 ────────────────────────────────────────────────────────────────
def fetch(url: str) -> dict:
    """URL 한 장. 리다이렉트는 따라가되 홉을 그대로 돌려준다.

    받기는 http_adapter 한 곳(연결 재사용·한 런 안의 원문 공유)이고, 여기서는 그 응답에서
    크롤의 모양(원문+홉)만 만든다. self-check 는 이 함수를 통째로 갈아끼워 가상 사이트를
    만든다 — 그래서 반환 모양(키 이름)이 계약이다.
    """
    r = fetch_full(url)
    return {k: r[k] for k in _FETCH_KEYS}


_FETCH_KEYS = ("final_url", "status", "chain", "text", "bytes", "content_type", "error")


def fetch_full(url: str, *, accept: str | None = None) -> dict:
    """fetch 와 같은 한 장 + headers(이름 소문자)·content(바이트)·ms(걸린 시간).

    헤더는 X-Robots-Tag·Link(hreflang)·Retry-After 를, 바이트는 gzip 사이트맵을, 시간은 응답
    지연을 보는 재료다. accept 를 주면 그 Accept 헤더로 **새로** 받는다 — 같은 주소의 HTML 응답을
    공유받으면 Markdown 협상을 물을 수 없다.
    """
    t0 = time.monotonic()
    try:
        if accept:
            r = http_adapter.get(url, headers={**UA, "Accept": accept},
                                 timeout=serp_adapter.TIMEOUTS["page"], fresh=True)
        else:
            r = http_adapter.get(url, headers=UA, timeout=serp_adapter.TIMEOUTS["page"])
    except Exception as e:
        return {"final_url": url, "status": None, "chain": [], "text": "",
                "bytes": 0, "content_type": "", "error": f"{type(e).__name__}: {e}"[:200],
                "headers": {}, "content": b"", "ms": None}
    ms = int((time.monotonic() - t0) * 1000)
    return {"final_url": r.final_url, "status": r.status, "chain": list(r.chain),
            "text": r.text, "bytes": r.bytes,
            "content_type": r.content_type, "error": None,
            "headers": dict(getattr(r, "headers", None) or {}),
            # 0ms 는 받은 것이 아니라 한 런 안에서 공유받은 것이다 — 지연으로 세지 않는다
            "content": getattr(r, "content", b""), "ms": ms or None}


_REAL_FETCH = fetch


def _get(url: str, accept: str | None = None) -> dict:
    """이 모듈이 주소를 받는 한 자리. fetch 가 진짜면 헤더까지(fetch_full), 검사가 갈아끼웠으면
    그 가짜를 부른다(가짜가 dict 에 headers·content 를 넣어 주면 그대로 쓴다). 가짜는 Accept 를
    못 가르므로 accept 를 준 물음에는 '모름'(status None)을 돌려준다."""
    f = globals()["fetch"]
    if f is _REAL_FETCH:
        return fetch_full(url, accept=accept)
    if accept:
        return {"final_url": url, "status": None, "chain": [], "text": "", "bytes": 0,
                "content_type": "", "error": "accept 탐침은 가짜 fetch 로 못 묻는다"}
    return f(url)


def _retry_after(r: dict) -> float | None:
    """Retry-After 헤더의 초. 날짜 꼴(HTTP-date)은 읽지 않는다 — 없는 것으로 치고 기본 대기를 쓴다."""
    v = str((r.get("headers") or {}).get("retry-after") or "").strip()
    return float(v) if v.isdigit() else None


def fetch_patiently(url: str, *, retries: int = RETRY_MAX) -> dict:
    """한 장을 받되 속도 제한(429·430·503)이면 물러섰다가 다시 묻는다.

    그 응답은 "이 주소가 깨졌다"가 아니라 "지금은 그만 두드려라"다. 그대로 적으면 일부만 속도
    제한에 걸린 날 없는 http_error 가 서고, 그게 "크롤에서 걸림" 기회가 된다."""
    r = _get(url)
    wait = RETRY_WAIT
    for _ in range(retries):
        if r.get("status") not in crawl_site.RATE_STATUS:
            break
        _sleep(min(_retry_after(r) or wait, RETRY_WAIT_MAX))
        wait *= 2
        r = _get(url)
    return r


def _sitemap_urls(url: str, *, follow: bool = True) -> list[str]:
    """사이트맵 한 장의 <loc>. sitemapindex 면 한 겹만 더 따라간다."""
    r = fetch(url)
    if r.get("status") != 200 or not r.get("text"):
        return []
    try:
        root = ET.fromstring(r["text"].strip())
    except ET.ParseError:
        return []
    locs = [(e.text or "").strip() for e in root.iter()
            if e.tag == "loc" or e.tag.endswith("}loc")]
    if not root.tag.endswith("sitemapindex"):
        return locs
    if not follow:
        return []
    out: list[str] = []
    for sm in locs[:MAX_SITEMAPS]:
        out += _sitemap_urls(sm, follow=False)
    return out


def discover_seeds(home: str) -> tuple[list[str], str, robotparser.RobotFileParser, str]:
    """(시드 URL, seed 종류, robots 판정기, robots.txt 원문).

    robots.txt 의 Sitemap: 줄이 정본이다 — 직접 파싱하지 않고 robotparser 가
    읽은 것을 쓴다. 사이트맵이 없거나 비면 홈에서 BFS 로 시작한다.

    원문까지 돌려주는 이유는 색인 막힘 요청문 때문이다: "구글이 이 주소를 색인
    안 했다"는 응답 옆에 **어느 Disallow 줄에 걸리는지**가 없으면, AI 가 원인
    후보를 짐작으로 세운다. 원문은 크롤 회차에 그대로 저장한다.
    """
    d = discover(home)
    return d["seeds"], d["seed"], d["rp"], d["robots_txt"]


def discover(home: str) -> dict:
    """robots.txt 와 사이트맵을 읽어 시드를 정한다 — discover_seeds 의 재료 전부.

    {"seeds","seed": sitemap|home,"rp","robots_txt","robots_status","robots_state"(crawl_site.
     robots_state),"sitemaps": crawl_site.read_sitemaps 결과|None,"entries": 사이트맵 항목(정규화한
     주소 norm 을 단 것),"fallback": 흔한 경로에서 찾았나,"capped": 상한에서 잘랐나,"listed": 읽은 주소 수}

    달라진 것 셋. ① robots.txt 가 200 이 아닐 때 '없다'(4xx)·'서버 오류'(5xx)·'모른다'(429·
    응답 없음)를 가른다 — 예전에는 셋 다 "전부 허용"이었다. 크롤은 어느 쪽이든 돈다(내 사이트를
    보는 일이라 멈추면 아무것도 안 남는다). 무엇이었는지는 회차에 적는다. ② Sitemap: 줄이 없으면
    흔한 경로(crawl_site.SITEMAP_FALLBACKS)를 두드린다 — 줄이 없다고 사이트맵이 없는 것이 아니고,
    못 찾으면 orphan·in_sitemap 근거가 통째로 사라진다. ③ 상한을 넘으면 앞에서 자르지 않고 주소
    꼴별로 돌아가며 뽑는다(crawl_site.stratify).
    """
    rp = robotparser.RobotFileParser()
    robots_url = urljoin(home, "/robots.txt")
    rp.set_url(robots_url)
    r = _get(robots_url)
    state = crawl_site.robots_state(r.get("status"))
    if state == "blocked":
        # 잠깐의 오류를 '사이트 전체 차단'으로 적지 않게 한 번 더 묻는다
        _sleep(ROBOTS_RETRY_WAIT)
        again = _get(robots_url)
        if crawl_site.robots_state(again.get("status")) != "blocked":
            r, state = again, crawl_site.robots_state(again.get("status"))
    raw_txt = r["text"] if state == "ok" and r.get("text") else ""
    if raw_txt.lstrip()[:200].lower().startswith(("<!doctype", "<html")):
        raw_txt, state = "", "none"       # 없는 주소를 홈으로 돌려보내는 사이트 — robots.txt 가 아니다
    # 못 읽으면 전부 허용 — robots.txt 가 없는 사이트가 흔하다. parse() 가
    # last_checked 를 세우므로 빈 목록이어도 can_fetch 가 True 를 돌려준다.
    rp.parse(raw_txt.splitlines() if raw_txt else [])

    listed = list(rp.site_maps() or [])
    sm = crawl_site.read_sitemaps(listed, _get, max_files=MAX_SITEMAPS) if listed else None
    fallback = False
    if not (sm and sm["entries"]):
        for path in crawl_site.SITEMAP_FALLBACKS:
            cand = urljoin(home, path)
            if cand in listed:
                continue
            one = crawl_site.read_sitemaps([cand], _get, max_files=MAX_SITEMAPS)
            if one["entries"]:
                if sm:                   # Sitemap: 줄이 가리킨 (못 읽은) 파일도 기록에 남긴다
                    one["files"] = sm["files"] + one["files"]
                sm, fallback = one, True
                break
    host = scoring.host_of(home)
    entries, seen = [], set()
    for e in (sm or {}).get("entries") or []:
        u = normalize(e["url"])
        if u and u not in seen:
            seen.add(u)
            entries.append({**e, "norm": u})
    # 다른 호스트의 주소는 시드로 안 쓴다(사이트맵 위생이 따로 말한다) — 남의 사이트를 두드리지 않는다
    urls = [e["norm"] for e in entries if scoring.owns(scoring.host_of(e["norm"]), host)]
    capped = bool(sm and sm["capped"])
    seeds = urls
    if len(urls) > MAX_SITEMAP_URLS:
        seeds, capped = crawl_site.stratify(urls, MAX_SITEMAP_URLS), True
    out = {"rp": rp, "robots_txt": raw_txt, "robots_status": r.get("status"),
           "robots_state": state, "sitemaps": sm, "entries": entries, "fallback": fallback,
           "capped": capped, "listed": len(urls)}
    if seeds:
        return {**out, "seeds": seeds, "seed": "sitemap"}
    return {**out, "seeds": [normalize(home)], "seed": "home"}


LLMS_HEAD = 400      # 앞부분만 남긴다 — 무엇을 소개하는 파일인지 알아볼 만큼


def probe_llms_txt(home: str) -> dict:
    """/llms.txt 한 번. {"found": 1|0|None, "bytes", "head"}.

    세 상태를 가른다 — found=None 은 "모른다"(연결 실패·5xx·401/403: 막혔거나 서버가
    아팠을 뿐 파일이 없다는 증거가 아니다), 0 은 "봤고 없다"(404/410, 또는 200 인데
    HTML 이 온 소프트 404 — 없는 주소를 홈으로 돌려보내는 사이트가 흔하다), 1 은 있다.

    구글은 이 파일을 쓰지 않는다. 그래서 기회 종류로 만들지 않고, 요청문과 봇 근거에
    "있다/없다" 한 줄로만 싣는다 — 없는 것을 인용 공백의 원인으로 부르면 오진이다.
    """
    r = fetch(urljoin(home, "/llms.txt"))
    st = r.get("status")
    if st in (404, 410):
        return {"found": 0, "bytes": None, "head": None}
    if st != 200:
        return {"found": None, "bytes": None, "head": None}
    text = r.get("text") or ""
    head = text.lstrip()[:200].lower()
    if "html" in (r.get("content_type") or "").lower() or head.startswith(("<!doctype", "<html")):
        return {"found": 0, "bytes": None, "head": None}
    return {"found": 1, "bytes": r.get("bytes") or len(text.encode()),
            "head": text.strip()[:LLMS_HEAD] or None}


# ── 크롤 ────────────────────────────────────────────────────────────────────
def crawl(seeds, home: str, *, limit: int, max_depth: int,
          rp=None, throttle: float = 0.0,
          workers: int | None = None, scope=None) -> tuple[list[dict], list[dict], list[list[str]]]:
    """BFS. (crawl_pages 행, crawl_links 행, 리다이렉트 사슬) 을 돌려준다.

    같은 호스트만 따라간다 — 외부 링크는 crawl_links 에 is_internal=0 으로
    기록만 하고 **가져오지 않는다**. robots.txt 의 Disallow 도 여기서 존중한다.

    가져오기는 동시에(workers 개, 기본 fanout.LIMITS["own_site"]), 처리는 순차다:
    큐 **앞쪽**에서 이번에 가져올 차례인 URL 을 workers 개까지 미리 띄워 두고, 처리는
    지금처럼 큐 순서대로 한 장씩 한다. 큐는 뒤에만 붙으므로 앞쪽 URL 은 순차 BFS 가
    어차피 다음에 가져올 바로 그것들이다 — 그래서 결과(행·순서·어느 URL 을 가져오나)
    가 순차일 때와 같다. 미리 띄우는 개수도 남은 limit 을 안 넘겨, 순차보다 한 장도
    더 가져오지 않는다. throttle 은 일꾼마다 제 요청 뒤에 쉰다 — 한 호스트에 동시
    workers 개를 넘지 않는 것이 예의의 상한이다. 이 함수는 conn 을 안 만진다(적재는
    끝나고 save 가 한 번에 한다).
    """
    host = scoring.host_of(home)
    # 분석 범위 경로(gucci.com/kr/ko/)가 있으면 그 아래만 따라간다 — 다른 나라 경로로
    # 나가는 링크도 내부 링크로 기록은 하되 가져오지는 않는다.
    seeds = [u for u in seeds if u and scoring.in_scope(u, scope)]
    q = deque((u, 0) for u in seeds if u)
    seen = {u for u in seeds if u}
    pages: list[dict] = []
    links: list[dict] = []
    chains: list[list[str]] = []
    parsed: set[str] = set()
    fetched = 0

    def allowed(u: str) -> bool:
        return rp is None or rp.can_fetch(UA_NAME, u)

    streak = {"n": 0}                         # 연달아 속도 제한에 걸린 장 수

    def get(u: str) -> dict:
        try:
            r = fetch_patiently(u, retries=0 if streak["n"] >= RATE_GIVE_UP else RETRY_MAX)
            streak["n"] = streak["n"] + 1 if r.get("status") in crawl_site.RATE_STATUS else 0
            return r
        finally:
            if throttle:
                time.sleep(throttle)

    width = max(1, workers or fanout.LIMITS["own_site"])
    pool = ThreadPoolExecutor(max_workers=width, thread_name_prefix="seo-miner-crawl")
    pending: dict = {}
    try:
        while q and fetched < limit:
            # 큐 앞쪽에서 가져올 차례인 것을 width 개(남은 limit 안)까지 띄워 둔다
            ahead = 0
            for u, _ in q:
                if ahead >= min(width, limit - fetched):
                    break
                if not allowed(u):
                    continue
                if u not in pending:
                    pending[u] = pool.submit(get, u)
                ahead += 1
            url, depth = q.popleft()
            if not allowed(url):
                continue
            r = pending.pop(url).result()
            fetched += 1
            _visit(r, url, depth, host=host, max_depth=max_depth, q=q, seen=seen,
                   pages=pages, links=links, chains=chains, parsed=parsed, scope=scope)
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
    return pages, links, chains


def _visit(r: dict, url: str, depth: int, *, host, max_depth, q, seen, pages, links,
           chains, parsed, scope=None) -> None:
    """가져온 한 장을 BFS 상태에 반영한다 — 순서대로 한 번에 하나씩만 불린다."""
    final = normalize(r.get("final_url") or url) or url

    # 리다이렉트 홉을 그대로 남긴다 — 사슬 판정이 SQL 로 되게.
    hop_urls = [normalize(u) or u for u, _ in (r.get("chain") or [])] + [final]
    for i, (_, hop_status) in enumerate(r.get("chain") or []):
        pages.append(_row(hop_urls[i], depth, status=hop_status,
                          redirect_to=hop_urls[i + 1]))
        seen.add(hop_urls[i])
    if len(hop_urls) > MAX_HOPS:
        chains.append(hop_urls)
    seen.add(final)
    if final in parsed:
        return          # 이미 본 곳으로 리다이렉트됐다 — 홉만 남기고 두 번 세지 않는다
    parsed.add(final)

    row = _row(final, depth, status=r.get("status"), nbytes=r.get("bytes") or 0)
    hdr = r.get("headers") or {}
    # X-Robots-Tag 는 HTML 이 아닌 파일(PDF)에도 붙는다 — 그래서 본문을 읽기 전에 적는다
    row["x_robots"] = crawl_site.x_robots_effective(hdr.get("x-robots-tag"))
    row["fetch_ms"] = r.get("ms")
    row["hsts"] = ("strict-transport-security" in hdr) if hdr else None
    by_header = crawl_site.link_header_hreflang(hdr.get("link"), final)
    ctype = (r.get("content_type") or "").lower()
    if r.get("status") == 200 and (not ctype or "html" in ctype):
        got = parse_page(final, r.get("text") or "")
        out = got.pop("links")
        row.update(got)
        row["links_out"] = len(out)
        fresh = []
        for to, anchor, nofollow, chrome in out:
            internal = scoring.owns(scoring.host_of(to), host)
            links.append({"url_from": final, "url_to": to, "anchor": anchor,
                          "is_internal": int(internal), "nofollow": int(nofollow),
                          "in_chrome": int(chrome)})
            if (internal and to not in seen and depth + 1 <= max_depth
                    and scoring.in_scope(to, scope)):
                seen.add(to)
                fresh.append(to)
        # 이 장이 새로 건 주소를 꼴별로 돌아가며 줄 세운다 — 글 목록 하나(/blog/* 500개)가 남은
        # 상한을 다 먹지 않게. 큐 앞쪽은 안 건드리므로 미리 띄우기와 순차 BFS 가 같은 결과다.
        q.extend((to, depth + 1) for to in crawl_site.stratify(fresh))
    if by_header:
        have = {(c, h) for c, h in row.get("hreflang") or []}
        row["hreflang"] = list(row.get("hreflang") or []) + [x for x in by_header
                                                             if (x[0], x[1]) not in have]
        row["hreflang_src"] = {(c, h): "header" for c, h in by_header if (c, h) not in have}
    pages.append(row)


def _row(url: str, depth: int, *, status=None, redirect_to=None, nbytes=0) -> dict:
    return {"url": url, "status": status, "redirect_to": redirect_to, "depth": depth,
            "title": None, "description": None, "h1": None, "canonical": None,
            "robots": None, "words": None, "schema_types": None,
            "links_in": 0, "links_out": 0, "images_no_alt": 0, "bytes": nbytes,
            "x_robots": None, "fetch_ms": None, "tpl": crawl_site.url_template(url),
            "body_hash": None, "modified": None, "h2s": None, "images": None, "lang": None}


_PAGE_COLS = ("url", "status", "redirect_to", "depth", "title", "description", "h1",
              "canonical", "robots", "words", "schema_types", "links_in", "links_out",
              "images_no_alt", "bytes",
              "x_robots", "fetch_ms", "tpl", "body_hash", "modified", "h2s", "images", "lang")


def save(conn, run_id: int, pages, links) -> None:
    """페이지·링크 적재 후 links_in 을 SQL 로 집계한다 — 크롤 중에는 모른다."""
    conn.executemany(
        f"INSERT OR REPLACE INTO crawl_pages(run_id,{','.join(_PAGE_COLS)}) "
        f"VALUES({','.join(['?'] * (len(_PAGE_COLS) + 1))})",
        [(run_id, *[p.get(c) for c in _PAGE_COLS]) for p in pages])
    conn.executemany(
        "INSERT INTO crawl_hreflang(run_id,url,code,href,source) VALUES(?,?,?,?,?)",
        [(run_id, p["url"], str(c)[:35], normalize(str(h), p["url"]) or str(h)[:500],
          (p.get("hreflang_src") or {}).get((c, h), "html"))
         for p in pages for c, h in (p.get("hreflang") or [])[:40]])
    conn.executemany(
        "INSERT INTO crawl_links(run_id,url_from,url_to,anchor,is_internal,nofollow,in_chrome) "
        "VALUES(?,?,?,?,?,?,?)",
        [(run_id, x["url_from"], x["url_to"], x["anchor"], x["is_internal"], x["nofollow"],
          x.get("in_chrome")) for x in links])
    conn.execute(
        "UPDATE crawl_pages SET links_in = (SELECT COUNT(*) FROM crawl_links l "
        " WHERE l.run_id = crawl_pages.run_id AND l.is_internal = 1"
        "   AND l.url_to = crawl_pages.url AND l.url_from <> crawl_pages.url) "
        "WHERE run_id = ?", (run_id,))


# ── 이슈 도출 ───────────────────────────────────────────────────────────────
# 속도 제한(429·430·503)은 그 주소의 결함이 아니다 — 다시 물어도 같았던 장은 판정에서 뺀다.
_RATE_SQL = "(" + ",".join(str(c) for c in sorted(crawl_site.RATE_STATUS)) + ")"
# (kind, SQL) — SQL 은 (url, detail) 두 칸을 돌려준다. severity 는 SEVERITY 표.
_ISSUE_SQL = (
    ("http_error",
     "SELECT url, 'HTTP ' || COALESCE(status, '응답 없음') FROM crawl_pages"
     " WHERE run_id=:run AND (status IS NULL OR (status >= 400 AND status NOT IN " + _RATE_SQL + "))"),
    ("broken_internal",
     "SELECT l.url_to, 'HTTP ' || COALESCE(p.status, '응답 없음') || ' · 이 곳을 가리키는 내부 링크 '"
     "       || COUNT(*) || '개 (예: ' || MIN(l.url_from) || ')'"
     " FROM crawl_links l JOIN crawl_pages p ON p.run_id = l.run_id AND p.url = l.url_to"
     " WHERE l.run_id=:run AND l.is_internal = 1"
     "   AND (p.status IS NULL OR (p.status >= 400 AND p.status NOT IN " + _RATE_SQL + "))"
     " GROUP BY l.url_to"),
    ("orphan",
     "SELECT p.url, '사이트맵에 있으나 내부 링크 0개' FROM crawl_pages p"
     " JOIN crawl_runs r ON r.id = p.run_id"
     " WHERE p.run_id=:run AND r.seed = 'sitemap' AND p.depth = 0 AND p.links_in = 0"
     "   AND p.status BETWEEN 200 AND 299 AND p.url <> :home"),
    ("dup_title",
     "SELECT p.url, '같은 제목을 쓰는 페이지 ' || ("
     "   SELECT COUNT(*) FROM crawl_pages q WHERE q.run_id = p.run_id AND q.title = p.title)"
     "   || '개: ' || p.title"
     " FROM crawl_pages p WHERE p.run_id=:run AND p.title IS NOT NULL AND TRIM(p.title) <> ''"
     "   AND (SELECT COUNT(*) FROM crawl_pages q"
     "        WHERE q.run_id = p.run_id AND q.title = p.title) > 1"),
    ("dup_description",
     "SELECT p.url, '같은 설명을 쓰는 페이지 ' || ("
     "   SELECT COUNT(*) FROM crawl_pages q"
     "   WHERE q.run_id = p.run_id AND q.description = p.description) || '개'"
     " FROM crawl_pages p WHERE p.run_id=:run AND p.description IS NOT NULL"
     "   AND TRIM(p.description) <> ''"
     "   AND (SELECT COUNT(*) FROM crawl_pages q"
     "        WHERE q.run_id = p.run_id AND q.description = p.description) > 1"),
    ("missing_title",
     "SELECT url, 'title 이 없습니다' FROM crawl_pages"
     " WHERE run_id=:run AND status BETWEEN 200 AND 299"
     "   AND (title IS NULL OR TRIM(title) = '')"),
    ("missing_description",
     "SELECT url, 'meta description 이 없습니다' FROM crawl_pages"
     " WHERE run_id=:run AND status BETWEEN 200 AND 299"
     "   AND (description IS NULL OR TRIM(description) = '')"),
    ("missing_h1",
     "SELECT url, 'H1 이 없습니다' FROM crawl_pages"
     " WHERE run_id=:run AND status BETWEEN 200 AND 299 AND (h1 IS NULL OR TRIM(h1) = '')"),
    ("thin_content",
     "SELECT url, '본문 ' || words || '단어 (기준 ' || :thin || ')' FROM crawl_pages"
     " WHERE run_id=:run AND status BETWEEN 200 AND 299 AND words IS NOT NULL"
     "   AND words < :thin"),
    # meta robots 와 X-Robots-Tag 헤더는 같은 규칙의 두 자리다 — 헤더로만 건 noindex 는
    # HTML 을 아무리 봐도 안 나온다(x_robots 는 모든 크롤러·구글에 걸리는 규칙만 담는다).
    ("noindex",
     "SELECT url, CASE WHEN LOWER(COALESCE(robots, '')) LIKE '%noindex%'"
     "   THEN 'meta robots: ' || robots ELSE 'X-Robots-Tag 헤더: ' || x_robots END"
     " FROM crawl_pages"
     " WHERE run_id=:run AND (LOWER(COALESCE(robots, '')) LIKE '%noindex%'"
     "                        OR COALESCE(x_robots, '') LIKE '%noindex%')"),
    ("canonical_mismatch",
     "SELECT p.url, 'canonical → ' || p.canonical || ' (크롤한 URL 이 아닙니다)'"
     " FROM crawl_pages p WHERE p.run_id=:run AND p.canonical IS NOT NULL"
     "   AND TRIM(p.canonical) <> '' AND p.canonical <> p.url"
     "   AND NOT EXISTS (SELECT 1 FROM crawl_pages q"
     "                   WHERE q.run_id = p.run_id AND q.url = p.canonical)"),
    # 상호 참조 — A 가 B 를 가리키면 B 도 A 를 가리켜야 한다. 아니면 구글은 그
    # **짝을 통째로 버린다**. 한 장만 봐서는 못 잡는 것이라 여기가 유일한 자리다.
    # 상대가 우리가 크롤한 페이지일 때만 본다 — 남의 도메인은 우리가 못 고친다.
    ("hreflang_no_return",
     "SELECT h.url, '이 페이지가 ' || h.href || ' (' || h.code || ') 를 가리키는데 "
     "그쪽은 이 주소를 안 가리킵니다' FROM crawl_hreflang h"
     " JOIN crawl_pages p ON p.run_id = h.run_id AND p.url = h.href"
     " WHERE h.run_id=:run AND h.href <> h.url"
     "   AND NOT EXISTS (SELECT 1 FROM crawl_hreflang b"
     "                   WHERE b.run_id = h.run_id AND b.url = h.href AND b.href = h.url)"
     " GROUP BY h.url, h.href"),
    ("img_no_alt",
     "SELECT url, 'alt 없는 이미지 ' || images_no_alt || '개' FROM crawl_pages"
     " WHERE run_id=:run AND images_no_alt > 0"),
)


def derive_issues(conn, run_id: int, *, home: str, chains=(), extra=()) -> int:
    """크롤 결과를 훑어 crawl_issues 에 저장한다. 저장이 핵심이다 — 회차 비교의 재료다.

    리다이렉트 사슬만 SQL 이 아니다: 홉 수는 응답 이력이라 크롤이 끝나면
    되살릴 수 없다. 다국어(extra = intl_issues 의 (kind, url, detail))도 메모리의
    페이지에서 판정한다 — 본문 글자를 표에 두지 않기 때문이다. 나머지는 전부
    적재된 표에서 뽑는다.
    """
    rows = []
    p = {"run": run_id, "home": home, "thin": THIN_WORDS}
    for kind, sql in _ISSUE_SQL:
        for url, detail in conn.execute(sql, p).fetchall():
            rows.append((run_id, kind, SEVERITY[kind], url, detail))
    for hops in chains:
        rows.append((run_id, "redirect_chain", SEVERITY["redirect_chain"], hops[0],
                     f"홉 {len(hops) - 1}개: " + " → ".join(hops)))
    for kind, url, detail, *sev in extra:    # 넷째 칸은 심각도를 낮춰 적을 때만(비율로 가르는 갈래)
        rows.append((run_id, kind, sev[0] if sev else SEVERITY[kind], url, detail))
    # (kind, url) 이 이슈의 정체성이다(회차 비교) — 같은 짝이 두 번 서지 않게
    seen: set = set()
    rows = [r for r in rows if (r[1], r[3]) not in seen and not seen.add((r[1], r[3]))]
    conn.executemany(
        "INSERT INTO crawl_issues(run_id, kind, severity, url, detail) VALUES(?,?,?,?,?)",
        rows)
    return len(rows)


# ── 다국어 ──────────────────────────────────────────────────────────────────
# 판정의 기준은 손으로 점검한 theotherskin.com(한국어 + en·ja·zh-hans·ar·es, 2026-10-14)이다:
# 외국어 판 상담 버튼이 한국어 /medical-consultation/ 으로 갔고, 블로그·공지는 ar·es 판이
# 없었고(한국어 77 · 스페인어 56), 언어 전환은 버튼뿐이라 <a> 가 없었고, x-default 가 한국어
# 판이었고, 스페인어 설명이 "Cicatriz papular Cicatriz papular …" 였고, 모든 외국어 판의
# MedicalClinic name 이 '디아더피부과' 였다. 한 장씩 보는 페이지 감사로는 하나도 안 나온다.
_ASSET = re.compile(r"^/(?:_next|wp-content|wp-includes|static|assets)/|"
                    r"\.(?:jpe?g|png|gif|webp|avif|svg|ico|pdf|css|js|mp4|zip|xml|txt|woff2?)$",
                    re.I)
_REPEAT = re.compile(r"(?<!\S)(\S.{2,58}?\S)\s+\1(?!\S)")
# 틀이 제목을 끼우는 자리는 설명의 맨 앞이다. 뒤쪽의 되풀이는 대개 표다 — theotherskin 의
# 진료 시간표 공지("AM Open Open Open Closed …", "午前 通常診療 通常診療 …")가 그랬다.
REPEAT_HEAD = 20


def _lang_key(code) -> str | None:
    """'en-US' → 'en', 'zh-Hans-CN' → 'zh-hans', 'x-default' → None.

    지역(en-us)은 버리고 문자 체계(zh-hans/zh-hant)는 남긴다 — 영국 영어와 미국 영어는 서로
    '다른 언어로 새는' 사이가 아니지만, 간체와 번체는 읽는 사람이 다르다."""
    c = str(code or "").strip().lower().replace("_", "-")
    parts = c.split("-")
    if not re.fullmatch(r"[a-z]{2,3}", parts[0]):
        return None
    return "-".join([parts[0]] + [s for s in parts[1:2] if len(s) == 4])


def _prim(lang: str) -> str:
    return (lang or "").split("-")[0]


def _uses(lang: str) -> set[str]:
    """그 언어 글에 정상으로 들어가는 체계 — 일본어 판의 한자는 중국어가 남은 것이 아니다."""
    s = {_LANG_SCRIPT[_prim(lang)]} if _prim(lang) in _LANG_SCRIPT else set()
    return s | ({"han"} if _prim(lang) == "ja" else set())


def _path(u: str) -> str:
    p = urlsplit(u)
    return (p.path or "/") + (f"?{p.query}" if p.query else "")


def intl_issues(pages, links, *, home: str, locales=(), site_type=None,
                sm_lastmod=None) -> list[tuple[str, str, str]]:
    """다국어 사이트의 언어 이음매 — (kind, url, detail). 외부 호출 0, 크롤이 받은 HTML 로만.

    **다국어 사이트에서만 선다**: 크롤한 페이지의 hreflang 이 두 언어 이상을 말하거나 사이트
    시장(site_locales)이 두 언어 이상일 때. 그리고 크롤한 페이지가 실제로 두 언어 이상일 때.
    html lang 만 보고 열면 lang="en-US" 를 그대로 둔 한국어 워드프레스가 통째로 '번역 안 된
    영어 페이지'가 된다.

    페이지 언어: hreflang 자기 참조 코드 → html lang → 주소 첫 경로(/en/). 사이트 기본 언어:
    사이트 첫 시장의 언어(페이지가 있으면) → 홈의 언어 → 가장 많은 언어.
    """
    import brief                                  # 언어 이름표(lang_label)의 정본

    ok = [p for p in pages if p.get("scripts") is not None
          and isinstance(p.get("status"), int) and 200 <= p["status"] < 300]
    hl_codes = {k for p in ok for c, _ in (p.get("hreflang") or []) if (k := _lang_key(c))}
    loc_langs = [k for k in (_lang_key(x) for x in locales or ()) if k]
    if len(hl_codes) < 2 and len(set(loc_langs)) < 2:
        return []
    known = hl_codes | set(loc_langs) | {k for p in ok if (k := _lang_key(p.get("lang")))}

    def path_lang(u: str) -> str | None:
        seg = urlsplit(u).path.strip("/").split("/")[0]
        k = _lang_key(seg) if seg else None
        return k if k in known and len(seg) <= 7 else None

    by_url = {p["url"]: p for p in ok}
    lang: dict[str, str] = {}
    src: dict[str, str] = {}
    alts: dict[str, dict[str, str]] = {}          # url → {언어: 대체 주소} (자기 자신 포함)
    xdef: dict[str, str] = {}
    for p in ok:
        me = p["url"]
        alts[me] = {}
        for c, h in p.get("hreflang") or []:
            to = normalize(str(h), me) or str(h)
            if str(c).strip().lower() == "x-default":
                xdef.setdefault(me, to)
            elif (k := _lang_key(c)):
                alts[me].setdefault(k, to)
        mine = next((k for k, to in alts[me].items() if to == me), None)
        if mine:
            lang[me], src[me] = mine, "hreflang"
        elif _lang_key(p.get("lang")):
            lang[me], src[me] = _lang_key(p.get("lang")), "html"
        elif path_lang(me):
            lang[me], src[me] = path_lang(me), "path"
    by_lang: dict[str, list[str]] = defaultdict(list)
    for u, l in lang.items():
        by_lang[l].append(u)
    if len(by_lang) < 2:
        return []                                 # 크롤한 페이지가 한 언어뿐 — 견줄 판이 없다

    # 홈 — 다국어 사이트는 '/' 가 '/ko/' 로 넘어가는 일이 흔하다. 그 '/' 행은 리다이렉트 홉이라
    # 본문·hreflang 이 없다. 그대로 두면 언어 홈이 '/' 하나뿐이 되어, 모든 페이지의 언어 메뉴
    # (/ko/·/en/)가 '새는 링크'로, 전환 링크가 있는 페이지가 '전환 링크 없음'으로 섰다
    # (검토 2026-10-16 재현). 그래서 redirect_to 사슬을 끝까지 따라간 주소를 홈으로 본다.
    site_home = normalize(home) if home else None
    nxt = {p["url"]: p["redirect_to"] for p in pages if p.get("redirect_to")}
    h, hops = site_home, 0
    while h in nxt and hops < 10:
        h, hops = normalize(nxt[h]) or nxt[h], hops + 1

    def lang_root(u: str) -> str | None:
        """'/en/'·'/en' 처럼 언어 경로 하나뿐인 주소면 그 언어 — 그 언어의 홈이다.
        홈이 hreflang 을 안 달았어도 언어 메뉴가 가는 곳을 언어 홈으로 알아보게 한다."""
        sp = urlsplit(u)
        segs = [s for s in sp.path.split("/") if s]
        return path_lang(u) if len(segs) == 1 and not sp.query else None

    def pick_default() -> str:
        for loc in loc_langs[:1]:
            if loc in by_lang:
                return loc
            same = [l for l in by_lang if _prim(l) == _prim(loc)]
            if same:
                return max(same, key=lambda l: len(by_lang[l]))
        if h in lang:
            return lang[h]
        return max(by_lang, key=lambda l: (len(by_lang[l]), l))

    D = pick_default()
    S = _LANG_SCRIPT.get(_prim(D))

    def nm(l: str) -> str:
        return brief.lang_label(l)

    def rep(urls) -> str:                         # 한 언어(묶음)를 대표하는 주소 — 가장 얕은 것
        return min(urls, key=lambda u: (len(_path(u)), u))

    counts = " · ".join(f"{nm(l)} {len(by_lang[l])}"
                        for l in sorted(by_lang, key=lambda l: (l != D, -len(by_lang[l]), l)))
    out: list[tuple[str, str, str]] = []

    # 1) 번역 안 된 글자 — 기본 언어가 라틴이면 건너뛴다(브랜드명과 못 가른다).
    if S:
        for u in sorted(lang):
            l = lang[u]
            if l == D or S in _uses(l):
                continue
            sc = by_url[u]["scripts"]
            # html lang 만이 근거인데 본문 대부분이 기본 언어면, 번역이 빠진 게 아니라 lang
            # 속성이 틀린 것이다(그건 다른 문제다). hreflang·주소가 말한 언어는 믿는다.
            if src[u] == "html" and (sc.get("text", {}).get(S, {}).get("n", 0)
                                     > 0.5 * max(by_url[u].get("letters") or 0, 1)):
                continue
            parts = []
            for field, label in (("title", "제목"), ("description", "설명"),
                                 ("text", "본문"), ("alt", "이미지 alt")):
                ex = ((sc.get(field) or {}).get(S) or {}).get("ex") or []
                if ex:
                    parts.append(f"{label} " + " · ".join(f"「{e}」" for e in ex))
            if parts:
                out.append(("intl_untranslated", u,
                            f"{nm(l)} 페이지에 {nm(D)}가 남았습니다 — " + " / ".join(parts)))

    # 2) 다른 언어로 새는 내부 링크 — 언어 전환(그 페이지의 hreflang 대체 주소, 각 언어의
    #    홈)과 자산(/_next·/wp-content·파일)은 뺀다. 같은 언어 판이 있으면 그 주소를 같이 준다.
    homes = set((alts.get(h) or {}).values()) | ({h} if h else set())
    went: dict[str, dict[str, str]] = defaultdict(dict)
    for x in links:
        if not x.get("is_internal"):
            continue
        a, b = x["url_from"], x["url_to"]
        la = lang.get(a)
        lb = lang.get(b) or (path_lang(b) if b not in by_url else None)
        if not la or not lb or la == lb or b == a:
            continue
        if (b in alts.get(a, {}).values() or b in homes or lang_root(b)
                or _ASSET.search(urlsplit(b).path)):
            continue
        went[a].setdefault(b, (x.get("anchor") or "").strip())
    for a in sorted(went):
        la, ex = lang[a], []
        for b, anchor in sorted(went[a].items())[:3]:
            fix = (alts.get(b) or {}).get(la)
            ex.append((f"「{anchor[:30]}」 " if anchor else "")
                      + f"→ {_path(b)} ({nm(lang.get(b) or path_lang(b))})"
                      + (f", {nm(la)} 판은 {_path(fix)}" if fix and fix != b else ""))
        n = len(went[a])
        out.append(("intl_lang_leak", a,
                    f"{nm(la)} 페이지에서 다른 언어 페이지로 가는 링크 {n}개: " + " / ".join(ex)
                    + (f" 외 {n - 3}개" if n > 3 else "")))

    # 3) 빠진 번역 — hreflang 으로 묶인 판들(묶음이 없는 페이지는 언어 경로를 뗀 주소가 같은
    #    것끼리). 묶음마다 한 줄, 주소는 기본 언어 판(없으면 가장 얕은 판).
    parent = {u: u for u in lang}

    def find(u):
        while parent[u] != u:
            parent[u] = parent[parent[u]]
            u = parent[u]
        return u

    def union(a, b):
        if a in parent and b in parent:
            parent[find(a)] = find(b)

    bare: dict[str, list[str]] = defaultdict(list)
    for u in lang:
        others = [to for to in alts[u].values() if to != u]
        for to in others:
            union(u, to)
        if not others:
            pth = urlsplit(u).path
            seg = pth.strip("/").split("/")[0]
            base = pth[len(seg) + 1:] if seg and path_lang(u) else pth
            bare[base or "/"].append(u)
    for us in bare.values():
        for u in us[1:]:
            union(us[0], u)
    groups: dict[str, list[str]] = defaultdict(list)
    for u in lang:
        groups[find(u)].append(u)
    every = set(by_lang)
    # hreflang 으로 알린 판은 있는 판으로 친다 — 이번 크롤이 안 받았을 뿐일 수 있다(상한·깊이,
    # 사이트맵 밖). 크롤한 판끼리만 맞대면 선언도 번역도 멀쩡한데 '빠진 번역'이 섰다(검토
    # 2026-10-16 재현). 받아 보니 4xx·5xx·응답 없음이었던 판만 없는 판이다.
    status = {p["url"]: p.get("status") for p in pages}

    def declared(u: str) -> set[str]:
        return {k for k, to in alts[u].items() if k in every
                and not (to in status and not (isinstance(status[to], int) and status[to] < 400))}

    for members in groups.values():
        have = {lang[u] for u in members}.union(*(declared(u) for u in members))
        miss = every - have
        if not miss:
            continue
        mine = [u for u in members if lang[u] == D]
        order = lambda ls: sorted(ls, key=lambda l: (l != D, -len(by_lang[l]), l))
        out.append(("intl_missing_translation", rep(mine or members),
                    f"없는 판: {' · '.join(nm(l) for l in order(miss))}"
                    f" (있는 판: {' · '.join(nm(l) for l in order(have))}). "
                    f"언어별 페이지 수: {counts}"))

    # 4) 언어 전환 링크 없음 — 언어마다 한 줄(틀의 문제라 페이지마다 세우면 수백 줄이 같은 말을 한다).
    linked: dict[str, set[str]] = defaultdict(set)
    for x in links:
        linked[x["url_from"]].add(x["url_to"])
    for l in sorted(by_lang):
        # 다른 언어의 홈으로 가는 링크도 전환으로 친다(전환 메뉴가 각 언어 첫 화면으로 가는 사이트).
        # 제 언어의 홈(로고 링크)은 전환이 아니다 — 그걸 치면 모든 페이지가 통과한다.
        away = {x for x in homes if lang.get(x) != l}
        has_alt = [u for u in by_lang[l] if any(to != u for to in alts[u].values())]
        bad = [u for u in has_alt
               if not (linked[u] & ({to for to in alts[u].values() if to != u} | away))
               and not any((r := lang_root(t)) and r != l for t in linked[u])]
        if bad:
            ex = ", ".join(_path(u) for u in sorted(bad, key=lambda u: (len(_path(u)), u))[:3])
            out.append(("intl_no_switch_link", rep(bad),
                        f"{nm(l)} 페이지 {len(has_alt)}장 중 {len(bad)}장에 다른 언어 판으로 가는 "
                        f"<a href> 가 없습니다(hreflang 으로는 알림). 예: {ex}"))

    # 5) x-default 가 기본 언어(영어가 아닌) 판 — 영어 판이 있을 때만. 사이트 단위 한 줄.
    if _prim(D) != "en" and any(_prim(l) == "en" for l in by_lang):
        hit = sorted((u, to) for u, to in xdef.items() if lang.get(to) == D)
        if hit:
            # 예는 다른 언어 판에서 고른다 — "/ → /" 는 아무것도 안 보여 준다
            u, to = min(hit, key=lambda t: (lang.get(t[0]) == D, len(_path(t[0])), t))
            out.append(("intl_xdefault", site_home or rep(by_url),
                        f"x-default 가 {nm(D)} 판을 가리키는 페이지 {len(hit)}장 "
                        f"(예: {_path(u)} → {_path(to)}). 영어 판이 있습니다"))

    # 6) 설명 틀 문구 — "Marcas de acné Marcas de acné …" 처럼 같은 구절이 연달아 두 번.
    for u in sorted(lang):
        d = (by_url[u].get("description") or "").strip()
        m = _REPEAT.search(d)
        if m and m.start() <= REPEAT_HEAD and any(ch.isalpha() for ch in m.group(1)):
            out.append(("intl_desc_repeat", u,
                        f"연달아 두 번 나오는 구절 「{m.group(1)[:40]}」 — 설명: {d[:90]}"
                        + ("…" if len(d) > 90 else "")))

    # 7) 구조화 데이터 언어 불일치 — 업체 이름·주소. 틀이라 언어마다 한 줄.
    if S:
        for l in sorted(by_lang):
            if l == D or S in _uses(l):
                continue
            bad, seen_v = [], []
            for u in by_lang[l]:
                hits = [(t, f, v) for t, f, v in by_url[u].get("ld_org") or []
                        if _script_bits(v).get(S, {}).get("ex")]
                if hits:
                    bad.append(u)
                    for hv in hits:
                        if hv not in seen_v:
                            seen_v.append(hv)
            if bad:
                out.append(("intl_schema_lang", rep(bad),
                            f"{nm(l)} 페이지 {len(by_lang[l])}장 중 {len(bad)}장: "
                            + " · ".join(f"{t} {f} 「{v}」" for t, f, v in seen_v[:3])))
    # 8) 판 사이 동등성 — 같은 본문·분량·신선도·inLanguage·법적 고지·날짜 표기(crawl_site.intl_more).
    #    언어를 가르는 일은 위에서 끝났다 — 그 결과를 그대로 넘긴다.
    out += crawl_site.intl_more({"by_url": by_url, "lang": lang, "by_lang": by_lang,
                                 "groups": list(groups.values()), "D": D, "nm": nm, "rep": rep,
                                 "links": links, "site_type": site_type,
                                 "sm_lastmod": sm_lastmod or {}})
    # (kind, url) 이 이슈의 정체성이다(회차 비교) — 같은 짝이 두 번 서지 않게.
    seen: set = set()
    return [r for r in out if (r[0], r[1]) not in seen and not seen.add((r[0], r[1]))]


# ── 사이트 단위 점검 ────────────────────────────────────────────────────────
# 판정은 crawl_site 가 한다(네트워크 없이). 여기서는 재료를 모아 넘기고, 탐침 요청을 보내고,
# 화면이 그릴 '사이트 사실' 줄을 만든다.
def cert_days_left(host: str, *, timeout: float = 5.0) -> int | None:
    """TLS 인증서 만료까지 남은 날(지났으면 음수). 못 재면 None — 0 으로 접지 않는다."""
    name = host.split(":")[0]
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((name, 443), timeout=timeout) as raw, \
                ctx.wrap_socket(raw, server_hostname=name) as tls:
            not_after = tls.getpeercert()["notAfter"]
        return int((ssl.cert_time_to_seconds(not_after) - time.time()) // 86400)
    except Exception:
        return None


def _cert_fn():
    """인증서는 진짜 네트워크일 때만 잰다 — fetch·전송을 갈아끼운 검사가 바깥으로 나가지 않게."""
    real = globals()["fetch"] is _REAL_FETCH and http_adapter._transport is http_adapter._real
    return cert_days_left if real else None


def merge_hreflang(pages, entries) -> None:
    """페이지의 hreflang 주소를 정규화하고, 사이트맵 <xhtml:link> 로 알린 것을 그 페이지에 보탠다.

    hreflang 을 알리는 길은 셋이다(<link>·HTTP Link 헤더·사이트맵). 한 길만 보면 사이트맵으로만
    알린 사이트는 "hreflang 없음"이 되고, 상호 참조·다국어 검사가 통째로 안 선다. 어느 길로 온
    것인지는 hreflang_src 에 남는다(crawl_hreflang.source)."""
    by = {p["url"]: p for p in pages}
    for p in pages:
        if not p.get("hreflang"):
            continue
        src = p.get("hreflang_src") or {}
        new, new_src = [], {}
        for c, h in p["hreflang"]:
            n = normalize(str(h), p["url"]) or str(h)
            new.append([c, n])
            if (c, h) in src:
                new_src[(c, n)] = src[(c, h)]
        p["hreflang"], p["hreflang_src"] = new, new_src
    for e in entries or ():
        p = by.get(e.get("norm"))
        if not p or not e.get("alts"):
            continue
        have = {(c, h) for c, h in p.get("hreflang") or []}
        for c, h in e["alts"]:
            n = normalize(str(h), e["norm"]) or str(h)
            if (c, n) not in have:
                have.add((c, n))
                p["hreflang"] = list(p.get("hreflang") or []) + [[c, n]]
                p.setdefault("hreflang_src", {})
                p["hreflang_src"] = {**(p.get("hreflang_src") or {}), (c, n): "sitemap"}


def site_checks(pages, links, *, disc: dict, home: str, rp=None, site_type=None, scope=None,
                today=None, crawl_delay=None) -> tuple[dict, list]:
    """사이트 단위로 한 번 보는 것 전부 — (사실, 이슈 줄). 탐침 요청이 여기서 나간다.

    이슈는 crawl_issues 로(회차 비교가 된다), 사실은 crawl_runs.facts_json 으로(화면의 '사이트 사실'
    줄). 에이전트용 발견 파일·Markdown 협상·RSL 은 사실에만 적는다 — 기회가 아니다."""
    rows: list = []
    robots_url = urljoin(home, "/robots.txt")
    st = disc.get("robots_status")
    if disc.get("robots_state") == "blocked":
        rows.append(("robots_5xx", robots_url,
                     f"robots.txt 가 HTTP {st} 를 돌려줍니다(다시 물어도 같았습니다)"))
    elif disc.get("robots_state") == "unknown":
        rows.append(("robots_unknown", robots_url,
                     "robots.txt: " + (f"HTTP {st}" if st else "응답 없음")))
    sm = disc.get("sitemaps")
    entries = [e for e in disc.get("entries") or [] if scoring.in_scope(e["norm"], scope)]
    if sm:
        rows += crawl_site.sitemap_file_issues(sm, home)
        rows += crawl_site.lastmod_issue(entries, pages, home, today=today)
        if site_type == "publisher":      # 뉴스 발행 사이트에만 — 다른 사이트에 뉴스 규칙은 군말이다
            rows += crawl_site.news_issue(sm, home, today=today)
        rows += crawl_site.sitemap_vs_crawl([e["norm"] for e in entries], pages,
                                            capped=bool(disc.get("capped")))
    limited = [p["url"] for p in pages
               if p.get("status") in crawl_site.RATE_STATUS and not p.get("redirect_to")]
    if limited:
        rows.append(("rate_limited", home,
                     f"{len(limited)}장이 속도 제한(429·503)으로 답했습니다 — 다시 물어도 같았습니다. "
                     "예: " + ", ".join(_path(u) for u in limited[:3])))
    rows += crawl_site.url_hygiene(pages)
    rows += crawl_site.hreflang_issues(pages)
    rows += crawl_site.template_thin(pages)
    rows += crawl_site.meta_template_issue(pages, home)
    trust, t_rows = crawl_site.trust_pages(pages, links, home)
    rows += t_rows
    rows += crawl_site.locator_issues(
        pages, links, thin=THIN_WORDS,
        allowed=(lambda u: rp.can_fetch(UA_NAME, u)) if rp is not None else None)

    # ── 탐침 — 요청이 나간다(크롤이 끝난 뒤, 한 번씩) ──
    soft = crawl_site.probe_soft404(home, _get, uuid.uuid4().hex[:12])
    if soft["soft"] == 1:
        went = normalize(soft.get("final") or "") or ""
        rows.append(("soft_404", home,
                     f"없는 주소 {_path(soft['url'])} 에 HTTP {soft['status']} 가 옵니다"
                     + (f" ({_path(went)} 로 넘어갑니다)" if went and went != soft["url"] else "")))
    host, h_rows = crawl_site.host_checks(home, _get, cert_days=_cert_fn())
    rows += h_rows
    wk = crawl_site.probe_well_known(home, _get)
    sample = next((p["url"] for p in pages if p.get("status") == 200 and p.get("depth")
                   and p.get("title") is not None and not urlsplit(p["url"]).query), None)
    md = crawl_site.probe_markdown(home, _get, sample)

    by = {p["url"]: p for p in pages}
    h, hops = normalize(home), 0
    while by.get(h, {}).get("redirect_to") and hops < 10:
        h, hops = by[h]["redirect_to"], hops + 1
    ms = [p["fetch_ms"] for p in pages if p.get("fetch_ms") and p.get("status") == 200
          and p.get("title") is not None]
    facts = {
        "robots": {"status": st, "state": disc.get("robots_state"), "crawl_delay": crawl_delay,
                   "rsl": crawl_site.rsl_license(disc.get("robots_txt") or "")},
        "sitemap": {"files": (sm or {}).get("files") or [], "listed": disc.get("listed") or 0,
                    "seeds": len(disc["seeds"]) if disc.get("seed") == "sitemap" else 0,
                    "fallback": bool(disc.get("fallback")), "capped": bool(disc.get("capped"))},
        "host": {**host, "hsts": by.get(h, {}).get("hsts")},
        "soft404": soft, "rate_limited": len(limited), "trust": trust,
        "latency": {"p50": crawl_site.percentile(ms, 0.5), "p95": crawl_site.percentile(ms, 0.95),
                    "n": len(ms)},
        "maps": sum(1 for p in pages if p.get("maps")),
        "webmcp": sum(1 for p in pages if p.get("webmcp")),
        "well_known": wk, "markdown": md,
    }
    facts["lines"] = fact_lines(facts)
    return facts, rows


def _sec(ms) -> str:
    return f"{ms / 1000:.1f}초"


def fact_lines(f: dict) -> list[dict]:
    """사실 → 화면이 그대로 그리는 줄 [{"g","k","v"}]. 문구의 정본은 여기다(화면은 그리기만 한다).

    g="site" 는 크롤이 사이트에 대해 한 번 본 것, g="agent" 는 에이전트용 발견 파일류다 —
    있나 없나만 적는 정보고 없다고 결함이 아니다. '확인 못 함'(None)과 '없음'(0)은 다른 말이다."""
    L: list[dict] = []

    def add(g, k, v):
        if v:
            L.append({"g": g, "k": k, "v": v})

    rb = f.get("robots") or {}
    st = rb.get("status")
    add("site", "robots.txt", {
        "ok": "읽었습니다",
        "none": f"없습니다(HTTP {st}) — 규칙 없음, 전부 허용으로 봅니다",
        "blocked": f"서버 오류(HTTP {st}) — 규칙을 지키는 크롤러는 이 상태에서 사이트 전체를 멈춥니다",
        "unknown": ("판정 불가(" + (f"HTTP {st}" if st else "응답 없음") + ") — 막혔는지 열렸는지 모릅니다"),
    }.get(rb.get("state") or "", ""))
    if rb.get("crawl_delay"):
        d = float(rb["crawl_delay"])
        add("site", "Crawl-delay",
            f"{d:g}초 — 그 간격으로 한 줄씩 돌았습니다" if d <= CRAWL_DELAY_MAX
            else f"{d:g}초 — 상한 {CRAWL_DELAY_MAX:g}초 간격으로 돌았습니다")
    sm = f.get("sitemap") or {}
    files = sm.get("files") or []
    if sm.get("seeds"):
        good = [x for x in files if x.get("format")]
        how = ("흔한 경로에서 찾았습니다(robots.txt 에 Sitemap: 줄이 없습니다)" if sm.get("fallback")
               else "robots.txt 의 Sitemap: 줄")
        v = f"파일 {len(good)}개 · 주소 {sm.get('listed') or 0:,}개 — {how}"
        if sm.get("capped"):
            v += (f". 상한에서 잘랐습니다: {sm['seeds']:,}개를 주소 꼴별로 골라 돌았습니다"
                  "(전수가 아닙니다)")
        add("site", "사이트맵", v)
    else:
        add("site", "사이트맵", "찾지 못했습니다 — 홈에서 내부 링크를 따라 돌았습니다"
            + (f"(읽히지 않은 파일 {len(files)}개)" if files else ""))
    lat = f.get("latency") or {}
    if lat.get("p50"):
        add("site", "응답 지연", f"중앙값 {_sec(lat['p50'])} · 느린 5% {_sec(lat['p95'])} 이상 "
                                f"(HTML {lat.get('n') or 0:,}장)")
    host = f.get("host") or {}
    http = host.get("http")
    add("site", "http → https",
        "301 로 넘어갑니다" if http == "redirect"
        else "그대로 열립니다(https 로 안 넘어감)" if http == "open"
        else f"넘어가지만 영구 리다이렉트가 아닙니다(HTTP {http.split('_')[1]})"
        if (http or "").startswith("redirect_") else "")
    add("site", "www · 비www", {"dup": "둘 다 200 으로 열립니다", "redirect": "한쪽으로 넘어갑니다"}
        .get(host.get("alt_host") or "", ""))
    if host.get("hsts") is not None:
        add("site", "HSTS", "헤더가 있습니다" if host["hsts"] else "헤더가 없습니다")
    if host.get("cert_days") is not None:
        add("site", "TLS 인증서", f"만료까지 {host['cert_days']}일" if host["cert_days"] >= 0
            else "만료됐습니다")
    soft = f.get("soft404") or {}
    if soft.get("soft") is not None:
        add("site", "없는 주소", f"HTTP {soft.get('status')} — "
            + ("200 을 줍니다(소프트 404)" if soft["soft"] else "없다고 답합니다"))
    if f.get("rate_limited"):
        add("site", "속도 제한", f"{f['rate_limited']:,}장을 못 봤습니다(429·503)")
    trust = f.get("trust") or {}
    if trust:
        add("site", "신뢰 페이지", " · ".join(
            f"{crawl_site.TRUST_PAGES[k][0]} {_path(u) if u else '못 찾음'}"
            for k, u in trust.items() if k in crawl_site.TRUST_PAGES))
    add("site", "지도 임베드", f"{f['maps']:,}장" if f.get("maps") else "")
    # ── 에이전트용 — 있나 없나만 ──
    wk = f.get("well_known") or []
    if wk:
        have = [x["name"] for x in wk if x.get("found") == 1]
        unk = sum(1 for x in wk if x.get("found") is None)
        no = sum(1 for x in wk if x.get("found") == 0)
        add("agent", "/.well-known 발견 파일",
            (("있음: " + " · ".join(have) + " / ") if have else "")
            + f"없음 {no}종" + (f" · 확인 못 함 {unk}종" if unk else ""))
    add("agent", "WebMCP 표식", f"{f['webmcp']:,}장" if f.get("webmcp") else "없음")
    md = f.get("markdown") or {}
    tri = lambda v, yes, no: "확인 못 함" if v is None else (yes if v else no)
    add("agent", "Markdown 협상",
        "Accept: text/markdown → " + tri(md.get("accept"), "markdown 으로 답합니다", "HTML 로 답합니다")
        + " · .md 형제 → " + tri(md.get("sibling"), "있음", "없음"))
    add("agent", "RSL 라이선스",
        f"robots.txt 의 License: 줄 — {rb['rsl']}" if rb.get("rsl") else "robots.txt 에 License: 줄 없음")
    return L


# ── 회차 비교 ───────────────────────────────────────────────────────────────
def _issues(conn, run_id: int) -> dict:
    return {(r["kind"], r["url"]): dict(r) for r in conn.execute(
        "SELECT kind, severity, url, detail FROM crawl_issues WHERE run_id=?",
        (run_id,)).fetchall()}


def compare(conn, project_id: int, run_id: int | None = None) -> dict:
    """직전 회차 대비 신규/해결된 이슈. 이 단계의 진짜 효용이 여기 있다.

    Returns:
        {"new": [...], "fixed": [...], "prev_run_id": int|None}
        비교할 직전 회차가 없으면 세 값 모두 비어 있다 — 첫 바퀴는 기준선이다.
    """
    if run_id is None:
        cur = conn.execute("SELECT id FROM crawl_runs WHERE project_id=?"
                           " ORDER BY id DESC LIMIT 1", (project_id,)).fetchone()
        run_id = cur["id"] if cur else None
    if run_id is None:
        return {"new": [], "fixed": [], "prev_run_id": None}
    prev = conn.execute("SELECT id FROM crawl_runs WHERE project_id=? AND id<?"
                        " ORDER BY id DESC LIMIT 1", (project_id, run_id)).fetchone()
    if prev is None:
        return {"new": [], "fixed": [], "prev_run_id": None}
    now, before = _issues(conn, run_id), _issues(conn, prev["id"])
    return {"new": [v for k, v in now.items() if k not in before],
            "fixed": [v for k, v in before.items() if k not in now],
            "prev_run_id": prev["id"]}


# ── 단계 ────────────────────────────────────────────────────────────────────
def collect(project: str, *, dry_run: bool = False, limit: int | None = None,
            max_depth: int | None = None, throttle: float | None = None,
            conn=None, **_opts) -> collector.StageResult:
    """사이트를 넓게 돌아 crawl_pages·crawl_links·crawl_issues 를 남긴다.

    Args:
        project: 사이트 이름
        dry_run: True 면 어디서 시드를 얻고 몇 개를 돌 예정인지만 찍는다 (fetch 0건)
        limit: 가져올 URL 상한 (설정 키 crawl_urls). 시드가 사이트맵이면 사이트맵의 주소는
            전부 가져오고 이만큼을 더 본다(상한 = 사이트맵 수 + 설정값). 0이면 끔
        max_depth: 홈에서 몇 단계까지 따라갈지 (설정 키 crawl_depth)
        throttle: 요청 간격(초) — 내 서버를 두드리는 속도
        conn: 이미 열린 Brain 연결 — 주면 그것을 쓰고 닫지 않는다
    """
    ap = _parser()
    with collector.stage(project, conn=conn, dry_run=dry_run) as st:
        conn, p = st.conn, st.project
        s = st.settings(ap, argparse.Namespace(limit=limit, max_depth=max_depth,
                                           throttle=throttle))
        limit, depth = s["crawl_urls"], s["crawl_depth"]
        if limit <= 0:
            print("[crawl] crawl_urls=0 — 사이트 크롤을 끄셨습니다.")
            return st.noop(rows=0)

        home = _home_url(p["domain"])
        if not home:
            return st.skip("사이트 주소(domain)가 없습니다. 프로젝트 yaml 의 domain 을 채우세요.")

        print(f"[crawl] {home} · 사이트맵 전부 + 링크로 {limit}개까지 · "
              f"깊이 {depth} · 비용 없음 (간격 {st.throttle}초)")
        if st.dry_run:
            print("  시드: robots.txt 의 Sitemap: 줄 → 사이트맵 URL, "
                  "없으면 홈에서 내부 링크 BFS")
            print(f"  같은 호스트만 따라갑니다 (외부 링크는 기록만) · 최대 {limit}회 조회 예정"
                  " — 사이트맵이 있으면 사이트맵 수만큼 더")
            return st.noop(rows=0)

        disc = discover(home)
        seeds, seed, rp, robots_txt = disc["seeds"], disc["seed"], disc["rp"], disc["robots_txt"]
        scope = scoring.scopes_of([sc for _, sc in db.site_markets(conn, p["id"])])
        if scope:
            # 사이트맵은 도메인 전체다 — 범위 안 주소만 시드로. 하나도 없으면 범위들의 첫 화면에서.
            seeds = [u for u in seeds if scoring.in_scope(u, scope)]
            if not seeds:
                seeds, seed = [normalize(urljoin(home, x)) for x in scope], "home"
            print(f"  분석 범위 {', '.join(scope)} 아래만 따라갑니다")
        print(f"  시드 {len(seeds)}개 ({seed})")
        # 사이트맵은 사이트가 스스로 내놓은 "색인해 달라는 주소" 전부다 — 거기서 잘라 내면
        # 전수 크롤이 아니다. theotherskin 은 사이트맵 414개인데 기본 상한 300개에서 잘려 ar·es
        # 판과 공지 일부가 회차마다 통째로 빠졌다. 사이트맵 주소는 전부 가져오고, 설정한 상한만큼
        # 여유를 더 준다(사이트맵 수 + 설정값). 상한을 사이트맵 수에 딱 맞추면 예산이 사이트맵
        # 주소에서 다 써져, 사이트맵에 없고 링크로만 걸린 주소(깨진 링크의 대상·hreflang 으로만
        # 알린 언어 판)를 하나도 못 가져왔다(검토 2026-10-16 — 사용자가 여유를 두기로 정함).
        # 사이트맵을 읽어 들이는 상한 MAX_SITEMAP_URLS 는 seeds 가 이미 지켰다.
        if seed == "sitemap":
            print(f"  사이트맵 주소 {len(seeds)}개는 전부 가져오고, 링크로만 걸린 주소를 {limit}개까지 "
                  f"더 봅니다 (이번 상한 {len(seeds) + limit}개)")
            limit = len(seeds) + limit

        disc["seeds"], disc["seed"] = seeds, seed
        # robots.txt 의 Crawl-delay 는 사이트 주인이 정한 간격이다 — 그 간격으로, 한 줄씩 돈다.
        # (구글은 이 줄을 안 읽지만 우리는 신원을 밝히고 도는 크롤러라 지킨다.)
        try:
            delay = rp.crawl_delay(UA_NAME)
        except Exception:
            delay = None
        pace, workers = st.throttle, None
        if delay:
            pace, workers = max(st.throttle, min(float(delay), CRAWL_DELAY_MAX)), 1
            print(f"  robots.txt Crawl-delay {delay}초 — {pace:g}초 간격으로 한 줄씩 돕니다")
        if disc["capped"]:
            print(f"  사이트맵이 상한을 넘습니다 — 읽은 {disc['listed']}개 중 주소 꼴별로 골라 돕니다")

        run_id = conn.execute(
            "INSERT INTO crawl_runs(project_id, seed, started_at, robots_txt, robots_status,"
            " robots_state, discovery_capped) VALUES(?,?,?,?,?,?,?)",
            (p["id"], seed, db.now(), robots_txt or None, disc["robots_status"],
             disc["robots_state"], int(bool(disc["capped"])))).lastrowid
        # 사이트맵이 시드였으면 그 목록 자체가 사실이다 — "이 주소가 사이트맵에
        # 있나" 는 색인 막힘 요청문이 매번 묻는 것인데 지금까지 아무 데도 안 남았다.
        # 시드로 고른 것만이 아니라 읽은 목록 전부(범위 안·내 호스트)를 lastmod·출처 파일과 함께.
        if seed == "sitemap":
            own = scoring.host_of(home)
            db.write_sitemap_entries(conn, run_id, [
                {"url": e["norm"], "lastmod": e.get("lastmod"), "source": e.get("source")}
                for e in disc["entries"] if scoring.in_scope(e["norm"], scope)
                and scoring.owns(scoring.host_of(e["norm"]), own)])
        with st.record("crawl") as r:
            pages, links, chains = crawl(seeds, home, limit=limit, max_depth=depth,
                                         rp=rp, throttle=pace, workers=workers, scope=scope)
            # 모든 주소가 막혔으면(응답 없음·403·429) 적지 않는다 — 적으면 막힌 홈이
            # http_error 이슈 → "크롤에서 걸림" 기회로 선다. 없는 결함이다.
            if pages and all(collect_page.blocked(x["status"])
                             or x["status"] in crawl_site.RATE_STATUS for x in pages):
                conn.execute("UPDATE crawl_runs SET finished_at=?, pages=0, issues=0 WHERE id=?",
                             (db.now(), run_id))
                conn.commit()
                r.notes = f"seed={seed} blocked={len(pages)}"
                return st.skip(collect_page.blocked_note(
                    home.split("/")[2], len(pages)))
            merge_hreflang(pages, disc["entries"])
            save(conn, run_id, pages, links)
            # robots.txt 를 받은 김에 /llms.txt 도 한 번 — 크롤 뒤에 받는 이유는 순서다:
            # 시드가 홈일 때 robots.txt 다음 요청은 홈이어야 한다(자체점검이 못 박는다).
            db.write_llms_txt(conn, run_id, probe_llms_txt(home))
            site_type = p["type"] if "type" in p.keys() else None
            intl = intl_issues(pages, links, home=home,
                               locales=db.site_locales(conn, p["id"]), site_type=site_type,
                               sm_lastmod={e["norm"]: e.get("lastmod") for e in disc["entries"]})
            facts, site_rows = site_checks(pages, links, disc=disc, home=home, rp=rp,
                                           site_type=site_type, scope=scope, crawl_delay=delay)
            n_issues = derive_issues(conn, run_id, home=home, chains=chains,
                                     extra=[*intl, *site_rows])
            fetched = sum(1 for x in pages if x["redirect_to"] is None)
            conn.execute(
                "UPDATE crawl_runs SET finished_at=?, pages=?, issues=?, latency_p50_ms=?,"
                " latency_p95_ms=?, facts_json=? WHERE id=?",
                (db.now(), fetched, n_issues, facts["latency"]["p50"], facts["latency"]["p95"],
                 json.dumps(facts, ensure_ascii=False), run_id))
            conn.commit()
            r.api_calls = fetched
            r.notes = (f"seed={seed} pages={fetched} links={len(links)} issues={n_issues}"
                       + (" robots_error=1" if disc["robots_state"] == "blocked" else "")
                       + (" robots_unknown=1" if disc["robots_state"] == "unknown" else "")
                       + (" sitemap_capped=1" if disc["capped"] else "")
                       + (f" rate_limited={facts['rate_limited']}" if facts["rate_limited"] else "")
                       + (f" crawl_delay={delay}" if delay else ""))

        diff = compare(conn, p["id"], run_id)
        print(f"saved {fetched} pages · {len(links)} links · {n_issues} issues")
        if diff["prev_run_id"]:
            print(f"  직전 회차(run {diff['prev_run_id']}) 대비 "
                  f"신규 {len(diff['new'])}건 · 해결 {len(diff['fixed'])}건")
        return st.done(rows=fetched)


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__)
    collector.add_common(ap)
    collector.add_setting(ap, "--limit", key="crawl_urls", fallback=300, type=int,
                          help="가져올 URL 상한. 사이트맵이 있으면 사이트맵 전부에 이만큼을 "
                               "더 본다(링크로만 걸린 주소). 0이면 끔")
    collector.add_setting(ap, "--max-depth", key="crawl_depth", fallback=5, type=int,
                          help="홈에서 몇 단계까지 따라갈지")
    collector.add_setting(ap, "--throttle", key="throttle", fallback=0.5, type=float,
                          help="요청 간격(초) — 내 서버를 두드리는 속도")
    return ap


def main() -> None:
    """인자가 없으면 자기검사 — run_checks.py 가 이 관례로 진입점을 찾는다."""
    if len(sys.argv) == 1:
        return _selfcheck()
    collector.cli("crawl")


# ── 자기검사 ────────────────────────────────────────────────────────────────
_SITEMAP = """<?xml version="1.0"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://site.kr/</loc></url>
  <url><loc>https://site.kr/a</loc></url>
  <url><loc>https://site.kr/b</loc></url>
  <url><loc>https://site.kr/orphan</loc></url>
</urlset>"""

_LONG = "단어 " * (THIN_WORDS + 10)


def _html(title, *, desc="설명입니다", h1="머리말", body="", head="", tail="") -> str:
    return (f"<html><head><title>{title}</title>"
            f'<meta name="description" content="{desc}">{head}</head>'
            f"<body><h1>{h1}</h1><p>{body or _LONG}</p>{tail}</body></html>")


def _site(*, sitemap: bool = True) -> dict:
    """가상 사이트 한 벌. fetch 를 이것으로 갈아끼운다."""
    robots = "User-agent: *\nDisallow: /private\n"
    if sitemap:
        robots += "Sitemap: https://site.kr/sitemap.xml\n"
    home_links = ('<a href="/a">에이</a><a href="/b">비</a><a href="/a#top">에이 또</a>'
                  '<a href="/dead">죽은 링크</a><a href="https://ext.com/x">바깥</a>'
                  '<a href="/private">막힌 곳</a><a href="/old">옛 주소</a>')
    return {
        "https://site.kr/robots.txt": (200, robots, "text/plain"),
        # 사이트맵이 없는 사이트는 파일도 없다 — Sitemap: 줄만 지우면 흔한 경로 폴백이 찾아낸다
        **({"https://site.kr/sitemap.xml": (200, _SITEMAP, "application/xml")} if sitemap else {}),
        "https://site.kr/": (200, _html("홈", tail=home_links), "text/html"),
        # /a 는 /b 를 hreflang 짝으로 선언하는데 /b 는 되받지 않는다 — 한 장만
        # 봐서는 /a 가 멀쩡해 보인다. 전수 크롤만 잡을 수 있는 종류다.
        "https://site.kr/a": (200, _html("같은 제목", h1="에이",
                                         head='<link rel="canonical" href="/a">'
                                              '<link rel="alternate" hreflang="ko" href="/a">'
                                              '<link rel="alternate" hreflang="en" href="/b">',
                                         tail='<a href="/b">비로</a>'), "text/html"),
        "https://site.kr/b": (200, _html("같은 제목", h1="", desc="", body="짧다",
                                         tail="<img src='x.png'>"), "text/html"),
        "https://site.kr/orphan": (200, _html("고아", h1="고아"), "text/html"),
        "https://site.kr/dead": (404, "없음", "text/html"),
        "https://site.kr/private": (200, _html("막힘"), "text/html"),
        "https://ext.com/x": (200, _html("바깥"), "text/html"),
    }


def _fake_fetch(site: dict, log: list, redirects: dict | None = None):
    redirects = redirects or {"https://site.kr/old": ["https://site.kr/mid", "https://site.kr/a"]}

    def f(url: str) -> dict:
        log.append(url)
        chain = []
        cur = url
        while cur in redirects:
            hops = redirects[cur]
            for h in hops[:-1]:
                chain.append((cur, 301))
                cur = h
            chain.append((cur, 301))
            cur = hops[-1]
        status, text, ctype = site.get(cur, (404, "없음", "text/html"))
        return {"final_url": cur, "status": status, "chain": chain, "text": text,
                "bytes": len(text.encode()), "content_type": ctype, "error": None}
    return f


def _crawled(log) -> list[str]:
    """가져온 것 중 크롤한 장만 — robots.txt·사이트맵·사이트 단위 탐침(site_checks)은 뺀다."""
    return [u for u in log if not (u.endswith((".txt", ".xml", ".md")) or "/.well-known/" in u
                                   or "seo-miner-probe-" in u or u.startswith("http://")
                                   or "//www." in u)]


def _selfcheck() -> None:
    import os
    import tempfile

    # 0. 계약 — 러너가 부르는 모양
    import inspect
    sig = inspect.signature(collect)
    assert "dry_run" in sig.parameters and "conn" in sig.parameters

    # 정규화: 프래그먼트·기본 포트·빈 경로가 한 벌로 접힌다
    assert normalize("https://s.kr/a#top") == "https://s.kr/a"
    assert normalize("https://S.KR:443/") == "https://s.kr/"
    assert normalize("/a?q=1#x", "https://s.kr/dir/") == "https://s.kr/a?q=1"
    assert normalize("mailto:a@b.kr") is None and normalize("") is None

    os.environ["CAPTURE_HOME"] = str(Path(tempfile.mkdtemp(prefix="seo-miner-crawl-selftest-")))
    boot = db.connect()
    boot.execute("INSERT INTO projects(name, domain, locale) VALUES('cw','site.kr','ko-KR')")
    boot.commit()
    boot.close()

    orig_fetch = globals()["fetch"]
    conn = db.connect()
    pid = conn.execute("SELECT id FROM projects WHERE name='cw'").fetchone()["id"]

    def run(**kw):
        """가상 사이트로 한 회차. (StageResult, 가져온 URL 목록)."""
        log: list[str] = []
        globals()["fetch"] = _fake_fetch(kw.pop("site", None) or _site(), log,
                                         kw.pop("redirects", None))
        try:
            r = collect("cw", conn=conn, throttle=0, limit=kw.pop("limit", 50),
                        max_depth=kw.pop("max_depth", 5), **kw)
        finally:
            globals()["fetch"] = orig_fetch
        return r, log

    try:
        # 9. crawl_urls=0 이면 아무것도 안 한다 (fetch 0건)
        r, log = run(limit=0)
        assert r.ok and r.skipped and log == [], (r, log)
        assert conn.execute("SELECT COUNT(*) c FROM crawl_runs").fetchone()["c"] == 0

        # 7(dry_run). 계획만 찍고 robots.txt 도 안 본다
        r, log = run(dry_run=True)
        assert r.ok and r.skipped and log == [], (r, log)

        # ── 1회차 ────────────────────────────────────────────────
        r, log = run()
        assert r.ok and not r.skipped, r
        run1 = conn.execute("SELECT * FROM crawl_runs ORDER BY id DESC LIMIT 1").fetchone()

        # 1. 사이트맵 시드
        assert run1["seed"] == "sitemap", dict(run1)

        # 2. 외부 호스트는 가져오지 않는다 — 기록만 한다
        assert "https://ext.com/x" not in log, log
        ext = conn.execute("SELECT is_internal FROM crawl_links WHERE run_id=? AND url_to=?",
                           (run1["id"], "https://ext.com/x")).fetchone()
        assert ext and ext["is_internal"] == 0, ext

        # robots Disallow 존중
        assert "https://site.kr/private" not in log, log

        # 3. 같은 URL 두 번 안 가져온다 (/a 는 사이트맵·홈·#top 세 경로로 걸린다)
        assert log.count("https://site.kr/a") == 1, log
        assert len(log) == len(set(log)), log

        pages = {p["url"]: p for p in conn.execute(
            "SELECT * FROM crawl_pages WHERE run_id=?", (run1["id"],)).fetchall()}
        issues = _issues(conn, run1["id"])
        kinds = {k for k, _ in issues}

        # 7. links_in 집계 — /b 는 홈과 /a 가 가리킨다
        assert pages["https://site.kr/b"]["links_in"] == 2, dict(pages["https://site.kr/b"])
        assert pages["https://site.kr/orphan"]["links_in"] == 0

        # 4. 깨진 내부 링크
        assert ("broken_internal", "https://site.kr/dead") in issues, sorted(issues)
        assert ("http_error", "https://site.kr/dead") in issues

        # 5. 중복 title — /a 와 /b 둘 다
        assert ("dup_title", "https://site.kr/a") in issues, sorted(issues)
        assert ("dup_title", "https://site.kr/b") in issues

        # 6. 고아 페이지
        assert ("orphan", "https://site.kr/orphan") in issues, sorted(issues)
        assert ("orphan", "https://site.kr/b") not in issues

        # 나머지 종류가 실제로 나온다
        assert ("missing_h1", "https://site.kr/b") in issues
        assert ("missing_description", "https://site.kr/b") in issues
        assert ("thin_content", "https://site.kr/b") in issues
        assert ("img_no_alt", "https://site.kr/b") in issues
        # hreflang 상호 참조 — /a 가 /b 를 가리키는데 /b 는 안 되받는다.
        # 자기 자신(ko → /a)은 짝이 맞으므로 안 걸려야 한다.
        assert ("hreflang_no_return", "https://site.kr/a") in issues, sorted(issues)
        _hl = issues[("hreflang_no_return", "https://site.kr/a")]["detail"]
        assert "https://site.kr/b" in _hl and "(en)" in _hl, _hl
        assert conn.execute(
            "SELECT COUNT(*) FROM crawl_hreflang WHERE run_id=?",
            (run1["id"],)).fetchone()[0] == 2, "선언 자체가 저장이 안 된다"
        assert ("redirect_chain", "https://site.kr/old") in issues, sorted(issues)
        assert "홉 2개" in issues[("redirect_chain", "https://site.kr/old")]["detail"]
        # /a 의 canonical 은 자기 자신이다 — 이건 이슈가 아니다
        assert ("canonical_mismatch", "https://site.kr/a") not in issues
        assert kinds <= set(SEVERITY), kinds
        # 이름표와 심각도는 같은 갈래 목록을 봐야 한다 — 한쪽에만 있는 갈래는
        # 화면·요청문에서 영어 kind 로 새어 나간다
        assert set(ISSUE_KIND) == set(SEVERITY), (
            set(ISSUE_KIND) ^ set(SEVERITY))
        assert all(i["severity"] in ("bad", "warn", "info") for i in issues.values())
        assert run1["issues"] == len(issues) and run1["finished_at"], dict(run1)

        # 8. 첫 회차는 기준선 — 비교 대상이 없다
        assert compare(conn, pid) == {"new": [], "fixed": [], "prev_run_id": None}

        # ── 2회차: /dead 가 살아나고, /orphan 에 noindex 가 붙는다 ────
        site2 = _site()
        site2["https://site.kr/dead"] = (200, _html("살아남"), "text/html")
        site2["https://site.kr/llms.txt"] = (200, "# 사이트\n> 소개 한 줄\n", "text/plain")
        site2["https://site.kr/orphan"] = (
            200, _html("고아", h1="고아", head='<meta name="robots" content="noindex">'),
            "text/html")
        r, log = run(site=site2)
        run2 = conn.execute("SELECT id FROM crawl_runs ORDER BY id DESC LIMIT 1").fetchone()["id"]
        # llms.txt — 1회차는 404(봤고 없음), 2회차는 있음. 회차에 그대로 남는다
        l1, l2 = (conn.execute("SELECT llms_txt_found f, llms_txt_bytes b, llms_txt_head h"
                               " FROM crawl_runs WHERE id=?", (i,)).fetchone()
                  for i in (run1["id"], run2))
        assert (l1["f"], l1["b"], l1["h"]) == (0, None, None), dict(l1)
        assert l2["f"] == 1 and l2["b"] > 0 and l2["h"].startswith("# 사이트"), dict(l2)
        diff = compare(conn, pid)
        assert diff["prev_run_id"] == run1["id"], diff
        new = {(i["kind"], i["url"]) for i in diff["new"]}
        fixed = {(i["kind"], i["url"]) for i in diff["fixed"]}
        assert ("noindex", "https://site.kr/orphan") in new, sorted(new)
        assert ("broken_internal", "https://site.kr/dead") in fixed, sorted(fixed)
        assert ("http_error", "https://site.kr/dead") in fixed, sorted(fixed)
        assert ("dup_title", "https://site.kr/a") not in new | fixed, (new, fixed)
        assert compare(conn, pid, run_id=run1["id"])["prev_run_id"] is None

        # 1(b). 사이트맵이 없으면 홈 BFS 로 시드가 바뀐다
        r, log = run(site=_site(sitemap=False))
        seed = conn.execute("SELECT seed FROM crawl_runs WHERE id=?", (run2 + 1,)).fetchone()
        assert seed["seed"] == "home", dict(seed)
        # robots.txt(와 사이트맵 찾기) 다음 첫 장이 홈
        assert log[0].endswith("/robots.txt") and _crawled(log)[0] == "https://site.kr/", log
        # Sitemap: 줄은 없는데 파일은 있다 — 흔한 경로에서 찾아 시드로 쓴다(없다고 접지 않는다)
        hidden = _site(sitemap=False)
        hidden["https://site.kr/sitemap_index.xml"] = (200, _SITEMAP, "application/xml")
        _r, log_h = run(site=hidden)
        seed_h = conn.execute("SELECT seed FROM crawl_runs ORDER BY id DESC LIMIT 1").fetchone()
        assert seed_h["seed"] == "sitemap" and "https://site.kr/orphan" in log_h, (dict(seed_h), log_h)
        assert "https://site.kr/orphan" not in log, log  # 링크가 없으니 못 닿는다

        # 깊이 상한 — 0이면 시드만 본다
        r, log = run(site=_site(sitemap=False), max_depth=0)
        assert [u for u in log if u.endswith("/a")] == [], log

        # 사이트가 이 서버를 막는다(gucci.com — 데이터센터 IP 에 응답 없음). 막힌 홈을
        # http_error 로 적으면 없는 결함이 "크롤에서 걸림" 기회로 선다 — 건너뛰고 안 적는다.
        before = conn.execute("SELECT COUNT(*) c FROM crawl_issues").fetchone()["c"]
        globals()["fetch"] = lambda url: {"final_url": url, "status": None, "chain": [],
                                          "text": "", "bytes": 0, "content_type": "",
                                          "error": "ReadTimeout: Read timed out."}
        try:
            r = collect("cw", conn=conn, throttle=0, limit=50, max_depth=5)
        finally:
            globals()["fetch"] = orig_fetch
        assert r.skipped and not r.failed and "응답하지 않습니다" in (r.reason or ""), r
        assert conn.execute("SELECT COUNT(*) c FROM crawl_issues").fetchone()["c"] == before, \
            "막힌 사이트의 홈을 크롤 이슈로 적었다"

        # 사이트맵이 시드면 사이트맵 전부 + 설정값만큼 더 — theotherskin 은 사이트맵 414개인데
        # 상한 300개에서 잘렸고, 상한을 사이트맵 수에 딱 맞추자 사이트맵 밖에서 링크로만 걸린
        # 주소(깨진 링크의 대상)를 하나도 못 봤다. 사이트맵 4개 · 상한 2개 → 사이트맵 4개를 다
        # 가져오고(시드가 먼저 큐에 선다), 남은 여유로 링크로만 걸린 /dead 까지 본다.
        r, log = run(limit=2)
        sm = {"https://site.kr/", "https://site.kr/a", "https://site.kr/b", "https://site.kr/orphan"}
        pages = _crawled(log)
        assert sm <= set(log), f"사이트맵을 상한에서 잘랐다: 빠짐 {sm - set(log)}"
        assert "https://site.kr/dead" in log, f"사이트맵 밖 링크 대상을 여유 안에서 안 봤다: {log}"
        assert len(pages) <= len(sm) + 2, f"사이트맵 수 + 설정값을 넘겨 가져왔다: {pages}"
        # 사이트맵이 없으면(홈 BFS) 상한은 설정값 그대로다
        r, log = run(site=_site(sitemap=False), limit=2)
        assert len(_crawled(log)) == 2, log

        _intl_selfcheck(conn, orig_fetch)
    finally:
        globals()["fetch"] = orig_fetch
        conn.close()

    # llms.txt 세 상태 — 있음(1)·봤고 없음(0)·모름(None). 뭉치면 못 받은 것을 "없다" 로 말한다
    def probe(status, text="", ctype="text/plain", error=None):
        globals()["fetch"] = lambda url: {"final_url": url, "status": status, "chain": [],
                                          "text": text, "bytes": len(text.encode()),
                                          "content_type": ctype, "error": error}
        try:
            return probe_llms_txt("https://s.kr/")
        finally:
            globals()["fetch"] = orig_fetch
    assert probe(200, "# 이름\n" + "가" * 500)["found"] == 1
    assert len(probe(200, "# 이름\n" + "가" * 500)["head"]) == LLMS_HEAD
    assert probe(404) == {"found": 0, "bytes": None, "head": None}
    assert probe(410)["found"] == 0
    # 없는 주소를 홈으로 돌려보내는 사이트 — 200 이지만 llms.txt 가 아니다
    assert probe(200, "<!doctype html><title>홈</title>", "text/html")["found"] == 0
    assert probe(200, "<html>홈</html>", "")["found"] == 0
    for st in (None, 403, 500, 503):      # 연결 실패·거부·서버 오류는 파일 유무의 증거가 아니다
        assert probe(st, error="x" if st is None else None)["found"] is None, st

    # 파서: 앵커·nofollow·외부/내부 판정 재료
    p = parse_page("https://s.kr/x", '<html><head><title>T</title></head><body>'
                   '<a href="/y" rel="nofollow">와이</a><a href="#top">건너뜀</a>'
                   '<a href="https://o.com/z">지</a></body></html>')
    assert [x[:3] for x in p["links"]] == [("https://s.kr/y", "와이", True), ("https://o.com/z", "지", False)], p["links"]
    # 틀(메뉴·꼬리말·role=navigation) 안의 링크와 본문 링크를 가른다 — 공통 메뉴가 모든 글에서
    # 거는 링크를 본문 링크로 세면 "링크를 안 건 글"을 영영 못 고른다.
    p = parse_page("https://s.kr/x", '<body><nav><a href="/m">메뉴</a></nav>'
                   '<div role="navigation"><a href="/r">롤</a></div>'
                   '<main><p>본문 <a href="/b">본문링크</a></p></main>'
                   '<footer><a href="/f">꼬리</a></footer></body>')
    assert {to.rsplit("/", 1)[-1]: chrome for to, _, _, chrome in p["links"]} == \
        {"m": True, "r": True, "b": False, "f": True}, p["links"]

    _concurrency_check(orig_fetch)
    print("collect_crawl self-check ok")


_LD_CLINIC = ('<script type="application/ld+json">{"@context":"https://schema.org",'
              '"@type":"MedicalClinic","name":"디아더피부과","alternateName":"디아더",'
              '"address":{"@type":"PostalAddress","streetAddress":"도산대로 307"}}</script>')


def _ml(lang, title, *, desc="", hl=(), body="", head="", tail="") -> str:
    """다국어 가상 페이지 한 장. hl = [(코드, 주소), ...]."""
    tags = "".join(f'<link rel="alternate" hreflang="{c}" href="{h}">' for c, h in hl)
    la = f' lang="{lang}"' if lang else ""
    return (f'<html{la}><head><title>{title}</title><meta name="description" content="{desc}">'
            f"{tags}{head}</head><body><h1>{title}</h1><p>{body}</p>{tail}</body></html>")


def _hl(ko, en, es=None):
    return [("ko", ko), ("en", en)] + ([("es", es)] if es else []) + [("x-default", ko)]


def _ml_site() -> dict:
    """theotherskin(2026-10-14 손 점검)을 줄인 다국어 가상 사이트 — 한국어가 기본, en·es.

    갈래마다 서야 할 자리 하나와 **서면 안 되는 자리** 하나씩을 같이 둔다(괄호 속 한국어 풀이,
    언어 선택 단추의 '한국어', 언어 홈으로 가는 전환 링크, 일부러 따로 둔 alternateName)."""
    H = "https://m.kr"
    en_body = ("Treatments in Seoul. Address: 서울특별시 강남구 도산대로 307. "
               "Melasma is called 기미 (gimi) in Korea. News（메디소비자뉴스）covered us. " + "word " * 220)
    pages = {
        "/": _ml("ko", "홈", desc="강남 피부과", hl=_hl("/", "/en/", "/es/"), body="본문 " * 220,
                 tail='<a href="/svc">시술</a><a href="/consult">상담</a><a href="/en/">English</a>'),
        # 언어 선택 단추의 "한국어"(글자·국기 alt)는 번역을 안 한 것이 아니다. 로고 alt 의 한국어는
        # 남은 것이다
        "/en/": _ml("en", "Home", desc="Clinic in Seoul", hl=_hl("/", "/en/", "/es/"),
                    body="word " * 220 + " 한국어",
                    tail='<img src="l.png" alt="더채움 로고"><a href="/en/svc">Service</a>'
                         '<a href="/"><img src="kr.png" alt="한국어">한국어</a>'),
        "/es/": _ml("es", "Inicio", desc="Marcas de acné Marcas de acné en Seúl",
                    hl=_hl("/", "/en/", "/es/"), body="palabra " * 220,
                    tail='<a href="/en/">English</a>'),
        "/svc": _ml("ko", "시술", desc="시술 안내", hl=_hl("/svc", "/en/svc"), body="본문 " * 220,
                    head=_LD_CLINIC),
        # html lang 이 틀렸다(ko) — hreflang 자기 참조(en)가 먼저다
        "/en/svc": _ml("ko", "Service", desc="시술 안내 페이지", hl=_hl("/svc", "/en/svc"),
                       body=en_body, head=_LD_CLINIC,
                       tail='<a href="/consult">Book now</a><a href="/svc">KO</a>'
                            '<a href="/en/">Home</a>'),
        "/consult": _ml("ko", "상담", desc="상담 예약", hl=_hl("/consult", "/en/consult"),
                        body="본문 " * 220),
        # 설명 뒤쪽의 되풀이는 표다(진료 시간표) — 틀 문구가 아니다
        "/en/consult": _ml("en", "Consultation",
                           desc="Book a visit. Hours Mon Tue Wed AM Open Open Open Closed",
                           hl=_hl("/consult", "/en/consult"), body="word " * 220),
        # lang 도 hreflang 도 없다 — 주소 첫 경로(/es/)로 언어를 정한다
        "/es/about": _ml(None, "Sobre", desc="Sobre nosotros", body="palabra " * 220),
    }
    sm = "".join(f"<url><loc>{H}{p}</loc></url>" for p in pages)
    site = {f"{H}{p}": (200, html, "text/html") for p, html in pages.items()}
    site[f"{H}/robots.txt"] = (200, f"User-agent: *\nSitemap: {H}/sitemap.xml\n", "text/plain")
    site[f"{H}/sitemap.xml"] = (200, '<?xml version="1.0"?><urlset xmlns='
                                '"http://www.sitemaps.org/schemas/sitemap/0.9">' + sm + "</urlset>",
                                "application/xml")
    return site


def _intl_of(site: dict, locales=("ko-KR",), home="https://m.kr/") -> list:
    """가상 사이트 → intl_issues. 네트워크·DB 없이 parse_page 만 거친다.
    3xx 는 (상태, 넘어갈 주소, _), 4xx·5xx 는 본문 없이 행만 — 크롤(_visit)이 남기는 모양 그대로."""
    pages, links = [], []
    for url, (status, html, _) in site.items():
        if 300 <= status < 400:
            pages.append({"url": url, "status": status, "redirect_to": html})
        elif status != 200:
            pages.append({"url": url, "status": status})
        elif not url.endswith((".txt", ".xml")):
            got = parse_page(url, html)
            for to, anchor, _nf, _ch in got.pop("links"):
                links.append({"url_from": url, "url_to": to, "anchor": anchor,
                              "is_internal": int(scoring.owns(scoring.host_of(to),
                                                              scoring.host_of(home)))})
            pages.append({**got, "url": url, "status": status})
    return intl_issues(pages, links, home=home, locales=locales)


def _intl_selfcheck(conn, orig_fetch) -> None:
    """다국어 검사 — 다국어 가상 사이트에서는 갈래마다 서고, 한 언어 사이트에서는 하나도 안 선다."""
    H = "https://m.kr"
    conn.execute("INSERT INTO projects(name, domain, locale) VALUES('mw','m.kr','ko-KR')")
    conn.commit()
    log: list[str] = []
    globals()["fetch"] = _fake_fetch(_ml_site(), log)
    try:
        r = collect("mw", conn=conn, throttle=0, limit=50, max_depth=5)
    finally:
        globals()["fetch"] = orig_fetch
    assert r.ok and not r.skipped, r
    rid = conn.execute("SELECT id FROM crawl_runs ORDER BY id DESC LIMIT 1").fetchone()["id"]
    got = _issues(conn, rid)
    intl = {k: v for k, v in got.items() if k[0] in INTL_KINDS}
    want = {("intl_untranslated", f"{H}/en/"), ("intl_untranslated", f"{H}/en/svc"),
            ("intl_lang_leak", f"{H}/en/svc"),
            ("intl_missing_translation", f"{H}/svc"), ("intl_missing_translation", f"{H}/consult"),
            ("intl_missing_translation", f"{H}/es/about"),
            ("intl_no_switch_link", f"{H}/svc"), ("intl_no_switch_link", f"{H}/en/consult"),
            ("intl_xdefault", f"{H}/"), ("intl_desc_repeat", f"{H}/es/"),
            ("intl_schema_lang", f"{H}/en/svc")}
    assert set(intl) == want, (f"더 섬 {sorted(set(intl) - want)} · 안 섬 {sorted(want - set(intl))}")
    # 판 사이 동등성 갈래(crawl_site.INTL_MORE_KINDS)는 이 사이트에서 안 선다 — test_crawl_site 가 본다
    assert {k for k, _ in intl} == set(INTL_KINDS) - set(crawl_site.INTL_MORE_KINDS), \
        "다국어 갈래 중 가상 사이트에서 안 서는 것이 있다"
    det = {k: v["detail"] for k, v in intl.items()}
    assert all(v["severity"] == SEVERITY[k[0]] for k, v in intl.items())
    # 번역 안 된 글자: 남은 것(alt·설명·주소)은 서고, 풀이(괄호)·언어 단추 '한국어'·두 글자 '기미'는 안 선다
    u1, u2 = det[("intl_untranslated", f"{H}/en/")], det[("intl_untranslated", f"{H}/en/svc")]
    assert "더채움" in u1 and "「한국어」" not in u1, u1
    assert "시술 안내 페이지" in u2 and "도산대로" in u2, u2
    assert "메디소비자뉴스" not in u2 and "「기미" not in u2, u2
    # 새는 링크: 같은 언어 판이 있으면 그 주소를 같이 준다. 전환(/svc·/en/·/)은 새는 것이 아니다
    lk = det[("intl_lang_leak", f"{H}/en/svc")]
    assert "/consult" in lk and "/en/consult" in lk and "링크 1개" in lk, lk
    assert ("intl_lang_leak", f"{H}/en/") not in intl and ("intl_lang_leak", f"{H}/es/") not in intl
    # 빠진 번역: 언어별 페이지 수를 함께 말한다
    mt = det[("intl_missing_translation", f"{H}/svc")]
    assert mt.startswith("없는 판: 스페인어") and "한국어 3" in mt and "스페인어 2" in mt, mt
    assert "한국어 · 영어" in det[("intl_missing_translation", f"{H}/es/about")]
    assert "3장 중 1장" in det[("intl_no_switch_link", f"{H}/en/consult")]
    assert "Marcas de acné" in det[("intl_desc_repeat", f"{H}/es/")]
    assert "(예: /en/ → /)" in det[("intl_xdefault", f"{H}/")], det[("intl_xdefault", f"{H}/")]
    sc = det[("intl_schema_lang", f"{H}/en/svc")]
    assert "디아더피부과" in sc and "도산대로" in sc and "「디아더」" not in sc, sc   # alternateName 은 안 본다

    # ── 안 서야 하는 사이트 ──
    def ko_page(path, **kw):
        return (200, _ml(kw.pop("lang", "ko"), "시술", desc="시술 안내 시술 안내 페이지",
                         body="본문 " * 220, head=_LD_CLINIC, **kw), "text/html")
    # 한 언어(한국어) 사이트 — hreflang 이 자기 자신·x-default 뿐. 설명 되풀이·한국어 구조화 데이터가
    # 있어도 다국어 갈래로 세우지 않는다
    one = {f"{H}/": ko_page("/", hl=[("ko", "/"), ("x-default", "/")], tail='<a href="/a">a</a>'),
           f"{H}/a": ko_page("/a", hl=[("ko", "/a"), ("x-default", "/a")])}
    assert _intl_of(one) == [], _intl_of(one)
    # html lang="en-US" 를 그대로 둔 한국어 워드프레스 — 시장이 하나면 문이 안 열리고, 둘이어도
    # 크롤한 페이지가 한 언어뿐이면 견줄 판이 없다. 열리면 사이트 전체가 '번역 안 된 영어 페이지'가 된다
    wp = {f"{H}/": ko_page("/", lang="en-US", tail='<a href="/a">a</a>'),
          f"{H}/a": ko_page("/a", lang="en-US")}
    assert _intl_of(wp) == [] and _intl_of(wp, locales=("ko-KR", "en-US")) == []
    # hreflang 도 없고 시장도 하나인데 lang 이 다른 페이지가 섞였다 — 다국어 사이트가 아니다
    mix = {f"{H}/": ko_page("/", tail='<a href="/a">a</a>'), f"{H}/a": ko_page("/a", lang="en")}
    assert _intl_of(mix) == [], _intl_of(mix)
    # 기본 언어가 라틴(영어)이면 '번역 안 된 글자'는 건너뛴다 — 브랜드명과 못 가른다
    lat = {f"{H}/": (200, _ml("en", "Home", desc="Clinic", hl=[("en", "/"), ("es", "/es/")],
                              body="The Other Clinic " * 80, tail='<a href="/es/">ES</a>'), "text/html"),
           f"{H}/es/": (200, _ml("es", "Inicio", desc="The Other Clinic en Seúl",
                                 hl=[("en", "/"), ("es", "/es/")], body="The Other Clinic " * 80,
                                 tail='<a href="/">EN</a>'), "text/html")}
    assert _intl_of(lat, locales=("en-US",)) == [], _intl_of(lat, locales=("en-US",))

    # '/' 가 '/ko/' 로 넘어가고, 모든 페이지에 전역 언어 메뉴(/ko/·/en/, 국기 alt 「한국어」)가 있다.
    # 홈을 '/' 로만 보면 메뉴가 '새는 링크'로, 메뉴가 있는 페이지가 '전환 링크 없음'으로 선다.
    # 홈이 hreflang 을 안 단 경우(언어 경로만으로 언어 홈을 알아봐야 한다)도 같이 본다.
    # /ko/b 의 영어 판은 선언만 됐고 크롤하지 않았다 — 빠진 번역이 아니다. /ko/c 의 영어 판은
    # 받아 보니 404 — 그건 빠진 번역이다.
    def rd_site(home_hl: bool) -> dict:
        menu = '<a href="/ko/"><img src="kr.png" alt="한국어">한국어</a><a href="/en/">English</a>'

        def pg(lang, path, pair):
            hl = [("ko", f"/ko/{pair}"), ("en", f"/en/{pair}")] if (pair or home_hl) else []
            return (200, _ml(lang, "시술" if lang == "ko" else "Service", desc=f"{lang} {path}",
                             hl=hl, body=("본문 " if lang == "ko" else "word ") * 220, tail=menu),
                    "text/html")
        return {f"{H}/": (301, f"{H}/ko/", ""),
                f"{H}/ko/": pg("ko", "/ko/", ""), f"{H}/en/": pg("en", "/en/", ""),
                f"{H}/ko/a": pg("ko", "/ko/a", "a"), f"{H}/en/a": pg("en", "/en/a", "a"),
                f"{H}/ko/b": pg("ko", "/ko/b", "b"),
                f"{H}/ko/c": pg("ko", "/ko/c", "c"), f"{H}/en/c": (404, "", "text/html")}
    for home_hl in (True, False):
        got = {(k, u) for k, u, _ in _intl_of(rd_site(home_hl))}
        assert got == {("intl_missing_translation", f"{H}/ko/c")}, (home_hl, sorted(got))
    # 중국어(간체) 기본 사이트의 영어 판 — 나란히 놓인 언어 이름 「简体中文 繁體中文」은 메뉴다.
    # 제목에 남은 중국어는 그대로 선다(빼는 것은 언어 이름뿐이다)
    zh_hl = [("zh-Hans", "/"), ("en", "/en/")]
    zh = {f"{H}/": (200, _ml("zh-Hans", "首页", desc="首尔皮肤科", hl=zh_hl, body="正文 " * 220,
                             tail='<a href="/en/">English</a>'), "text/html"),
          f"{H}/en/": (200, _ml("en", "Home 服务介绍", desc="Clinic", hl=zh_hl, body="word " * 220,
                                tail='<ul><li>简体中文</li><li>繁體中文</li></ul>'
                                     '<a href="/">中文</a>'), "text/html")}
    zr = [(k, u, d) for k, u, d in _intl_of(zh, locales=("zh-CN",)) if k == "intl_untranslated"]
    assert len(zr) == 1 and "服务介绍" in zr[0][2] and "简体中文" not in zr[0][2], zr


def _concurrency_check(orig_fetch) -> None:
    """동시 가져오기가 순차 BFS 와 **같은 결과**를 내나 — 행·링크·사슬·가져온 URL 집합.

    가짜 사이트는 페이지마다 응답 시간이 다르다(앞 번호일수록 느리다). 끝난 순서대로
    처리하면 큐 순서가 바뀌어 depth·행 순서가 달라지고, 미리 띄우는 개수가 limit 을
    넘으면 순차보다 더 가져온다 — 둘 다 여기서 걸린다.
    """
    import threading

    n = 14
    site = {"https://site.kr/": (200, _html("홈", body="".join(
        f'<a href="/p{i}">{i}</a>' for i in range(n)) + '<a href="/old">옛</a>'), "text/html")}
    for i in range(n):
        site[f"https://site.kr/p{i}"] = (200, _html(f"p{i}", body=(
            f'<a href="/p{(i * 5 + 3) % n}">x</a><a href="/q{i}">q</a>'
            '<a href="/private/z">비밀</a>')), "text/html")
        site[f"https://site.kr/q{i}"] = (200, _html(f"q{i}"), "text/html")
    site["https://site.kr/a"] = (200, _html("a"), "text/html")
    rp = robotparser.RobotFileParser()
    rp.parse(["User-agent: *", "Disallow: /private"])

    lock = threading.Lock()
    live = {"now": 0, "peak": 0}
    queued: list[str] = []

    def run(workers: int, limit: int):
        log: list[str] = []
        sent: list[str] = []
        base = _fake_fetch(site, log)

        def slow(url: str) -> dict:
            with lock:
                sent.append(url)      # 요청이 **떠난** 순간 — 끝나기 전에 취소돼도 보낸 것이다
                live["now"] += 1
                live["peak"] = max(live["peak"], live["now"])
            tail = url.rstrip("/").rsplit("/", 1)[-1]
            num = int(tail[1:]) if tail[1:].isdigit() else 0
            time.sleep(0.002 * (n - num))
            with lock:
                live["now"] -= 1
            return base(url)

        class Spy(ThreadPoolExecutor):
            """띄운 것을 센다 — 끝의 cancel_futures 가 안 떠난 것을 치워 주면 보낸 목록만으로는
            미리 띄우기가 limit 을 넘는 것을 못 본다(타이밍에 따라 가려진다)."""
            def submit(self, fn, *a, **kw):
                with lock:
                    queued.append(a[0])
                return super().submit(fn, *a, **kw)

        queued.clear()
        globals()["fetch"], globals()["ThreadPoolExecutor"] = slow, Spy
        try:
            got = crawl(["https://site.kr/"], "https://site.kr/", limit=limit, max_depth=3,
                        rp=rp, workers=workers)
        finally:
            globals()["fetch"], globals()["ThreadPoolExecutor"] = orig_fetch, Spy.__bases__[0]
        return got, sent

    for limit in (9, 40):
        live["peak"] = 0
        (p1, l1, c1), log1 = run(1, limit)
        assert live["peak"] == 1
        live["peak"] = 0
        (p4, l4, c4), log4 = run(4, limit)
        assert sorted(queued) == sorted(log1),             f"순차보다 더 띄웠다 (limit={limit}): +{set(queued) - set(log1)}"
        assert (p4, l4, c4) == (p1, l1, c1), f"동시 크롤이 순차와 다른 결과를 냈다 (limit={limit})"
        assert sorted(log4) == sorted(log1),             f"가져온 URL 이 순차와 다르다 (limit={limit}): +{set(log4) - set(log1)} -{set(log1) - set(log4)}"
        assert not any("/private" in u for u in log4), "robots Disallow 를 미리 띄웠다"
        assert 1 < live["peak"] <= 4, f"동시 상한: peak={live['peak']}"
    assert c1, "리다이렉트 사슬 재료가 검사에 없다"

    # 분석 범위 — /kr/ko/ 아래만 가져오고, 다른 나라 경로는 내부 링크로 기록만 한다
    pagesx = {"https://g.com/kr/ko/": '<a href="/kr/ko/a">a</a><a href="/us/en/b">b</a>',
              "https://g.com/kr/ko/a": '<a href="/kr/ko/">h</a>'}
    got_urls: list[str] = []

    def fake(u):
        got_urls.append(u)
        return {"status": 200, "final_url": u, "content_type": "text/html",
                "text": f"<html><body>{pagesx.get(u, '')}</body></html>", "bytes": 10}
    globals()["fetch"] = fake
    try:
        _, lx, _ = crawl(["https://g.com/", "https://g.com/kr/ko/"], "https://g.com/",
                         limit=20, max_depth=3, workers=1, scope="/kr/ko/")
    finally:
        globals()["fetch"] = orig_fetch
    assert sorted(got_urls) == ["https://g.com/kr/ko/", "https://g.com/kr/ko/a"], got_urls
    assert any(x["url_to"].endswith("/us/en/b") and x["is_internal"] for x in lx), "범위 밖 링크 기록이 사라졌다"


if __name__ == "__main__":
    main()
