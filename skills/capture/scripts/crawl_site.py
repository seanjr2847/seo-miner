#!/usr/bin/env python3
"""크롤의 사이트 단위 판정 — 사이트맵 파일·호스트·탐침, 그리고 여러 장을 맞대야 나오는 것.

collect_crawl 이 부른다. 여기는 **네트워크를 직접 타지 않는다**: 받기는 전부 넘겨받은
`fetch(url) -> dict`(collect_crawl.fetch 와 같은 모양)로 하고, 판정은 받은 것만으로 한다.
그래서 검사(test_crawl_site.py)가 가짜 fetch 하나로 전부 돈다.

갈래 이름표·심각도의 정본은 collect_crawl.ISSUE_KIND·SEVERITY 다 — 여기서는 kind 이름과
세부(detail)만 만든다. 돌려주는 줄의 꼴은 (kind, url, detail) 또는 심각도를 낮춰 적을 때
(kind, url, detail, severity).

외부 사실(구글 문서·RFC)은 2026-11-24 에 공식 문서로 확인했고 쓰는 자리에 출처를 단다.
"""
from __future__ import annotations

import gzip
import hashlib
import re
import unicodedata
import xml.etree.ElementTree as ET
import zlib
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from urllib.parse import parse_qsl, urljoin, urlsplit

import scoring

# ── 상태 코드 ───────────────────────────────────────────────────────────────
# 속도 제한·잠깐 못 받음. 구글은 5xx·429 를 "서버가 버겁다"로 읽고 크롤을 늦춘다 — 그 주소가
# 깨졌다는 뜻이 아니다. 430 은 Shopify 가 봇에게 주는 속도 제한이다.
# https://developers.google.com/search/docs/crawling-indexing/http-network-errors
RATE_STATUS = frozenset({429, 430, 503})


def robots_state(status) -> str:
    """robots.txt 를 받은 결과 → ok | none | blocked | unknown.

    구글: "4xx(429 제외)는 robots.txt 가 없는 것으로 본다", 5xx 는 처음 12시간 크롤을 멈추고
    30일까지 마지막으로 받은 사본을 쓴다, 429 는 서버 오류로 친다.
    https://developers.google.com/crawling/docs/robots-txt/robots-txt-spec
    우리 요청이 받은 429·응답 없음은 '구글이 무엇을 받는지'의 증거가 아니라 unknown 으로 둔다
    (우리 요청만 속도 제한에 걸렸을 수 있다). 5xx 는 서버가 낸 답이라 blocked 다.
    """
    if status is None or status == 429:
        return "unknown"
    if 200 <= status < 300:
        return "ok"
    if status >= 500:
        return "blocked"
    if status >= 400:
        return "none"
    return "unknown"


# ── URL 꼴 ──────────────────────────────────────────────────────────────────
_LANG_SEG = re.compile(r"[a-z]{2}(?:[-_][a-zA-Z]{2,4})?")


def url_template(url: str) -> str:
    """주소의 꼴 — '/blog/my-post' → '/blog/*', '/en/blog/x' → '/en/blog/*', '/about' → '/*'.

    앞쪽 경로 한 칸(언어 경로가 앞에 있으면 두 칸)은 글자 그대로, 나머지는 '*'. 마지막 칸은 늘
    '*' 다 — 같은 틀이 찍은 페이지들을 한 묶음으로 본다(표본 층화·같은 틀 근사 중복)."""
    segs = [s for s in urlsplit(url or "").path.split("/") if s]
    if not segs:
        return "/"
    keep = 2 if len(segs) > 2 and _LANG_SEG.fullmatch(segs[0]) else 1
    keep = min(keep, len(segs) - 1)
    return "/" + "/".join(segs[:keep] + ["*"] * (len(segs) - keep))


def stratify(urls, cap: int | None = None, key=url_template) -> list[str]:
    """주소를 꼴별로 돌아가며 뽑는다(꼴이 처음 나온 순서, 꼴 안에서는 원래 순서).

    상한에서 자를 때 글 목록 하나(/blog/* 4천 개)가 표본을 다 먹지 않게 한다. cap 이 None 이면
    순서만 바꾼다."""
    groups: dict[str, list[str]] = {}
    for u in urls:
        groups.setdefault(key(u), []).append(u)
    out: list[str] = []
    lists = list(groups.values())
    i = 0
    while lists and (cap is None or len(out) < cap):
        alive = []
        for g in lists:
            if cap is not None and len(out) >= cap:
                break
            if i < len(g):
                out.append(g[i])
                alive.append(g)
        lists = [g for g in alive if i + 1 < len(g)]
        i += 1
    return out


# ── 사이트맵 ────────────────────────────────────────────────────────────────
# robots.txt 에 Sitemap: 줄이 없을 때 두드려 보는 자리 — 줄이 없다고 사이트맵이 없는 것은 아니다.
SITEMAP_FALLBACKS = ("/sitemap.xml", "/sitemap_index.xml", "/sitemap-index.xml", "/wp-sitemap.xml")
# 한 파일의 한도 — sitemaps.org: "no more than 50,000 URLs and … no larger than 50MB
# (52,428,800 bytes)" (압축을 푼 크기), <loc> 는 "less than 2,048 characters".
# https://www.sitemaps.org/protocol.html
SITEMAP_MAX_URLS = 50_000
SITEMAP_MAX_BYTES = 52_428_800
LOC_MAX = 2048
MAX_SITEMAP_FILES = 20        # 색인에서 따라갈 하위 사이트맵 수
READ_CAP = 50_000             # 읽어 들여 메모리에 두는 항목 상한
# 뉴스 사이트맵 — "a sitemap may have up to 1,000 news:news tags", "articles that were created
# in the last two days". https://developers.google.com/search/docs/crawling-indexing/sitemaps/news-sitemap
NEWS_MAX = 1000
NEWS_DAYS = 2
# 색인될 주소에 있으면 안 되는 파라미터 — 세션·추적. 값이 방문마다 달라 같은 페이지가 여러 주소가 된다.
_TOKEN_PARAMS = re.compile(r"^(?:phpsessid|jsessionid|sid|sessionid|session_id|token|auth|"
                           r"utm_[a-z]+|gclid|fbclid|msclkid|yclid)$", re.I)
# W3C Datetime(https://www.w3.org/TR/NOTE-datetime): 년 · 년-월 · 년-월-일 · 시각+시간대
_W3C = re.compile(r"\d{4}(?:-\d{2}(?:-\d{2}(?:T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?"
                  r"(?:Z|[+-]\d{2}:\d{2}))?)?)?")
LASTMOD_MIN = 10              # 이만큼은 있어야 '전부 같은 날'을 말한다
LASTMOD_SAME = 0.9            # 한 날짜가 이 비율 이상이면 빌드할 때 찍은 값이다
LASTMOD_SLACK = 1             # 페이지 수정일과 이 날수까지는 같은 것으로 본다(시간대)


def _local(tag) -> str:
    return str(tag).rsplit("}", 1)[-1].lower() if isinstance(tag, str) else ""


def gunzip(blob: bytes, limit: int = SITEMAP_MAX_BYTES) -> tuple[bytes | None, str | None]:
    """gzip 을 limit 까지만 푼다. (바이트, 오류) — 넘치면 자르고 오류에 적는다."""
    try:
        d = zlib.decompressobj(16 + zlib.MAX_WBITS)
        out = d.decompress(blob, limit + 1)
    except zlib.error as e:
        return None, f"gzip 을 못 풀었습니다 ({e})"[:120]
    if len(out) > limit:
        return out[:limit], f"압축을 풀면 {limit // 1_048_576}MB 를 넘습니다"
    return out, None


def parse_sitemap(url: str, r: dict) -> dict:
    """받은 응답 한 장 → 사이트맵 파일 하나.

    {"url","status","format": urlset|index|rss|atom|txt|None,"gz","bytes","error",
     "entries": [{"url","lastmod","alts": [(코드, 주소)],"news_date"}], "children": [주소],
     "news": news:news 태그 수}. 못 읽으면 format=None 이고 error 에 왜인지 적는다 — 조용히
    빈 목록을 돌려주면 orphan·in_sitemap 근거가 통째로 사라진 것을 아무도 모른다.
    """
    out = {"url": url, "status": r.get("status"), "format": None, "gz": False,
           "bytes": r.get("bytes") or 0, "error": None, "entries": [], "children": [], "news": 0}
    if r.get("status") != 200:
        out["error"] = f"HTTP {r.get('status') or '응답 없음'}"
        return out
    blob = r.get("content")
    text = r.get("text") or ""
    if isinstance(blob, (bytes, bytearray)) and bytes(blob[:2]) == b"\x1f\x8b":
        out["gz"] = True
        raw, err = gunzip(bytes(blob))
        if raw is None:
            out["error"] = err
            return out
        out["bytes"] = len(raw)
        text = raw.decode("utf-8", errors="replace")
        if err:
            out["error"] = err
    text = text.lstrip("﻿ \t\r\n")
    if not text:
        out["error"] = "빈 파일"
        return out
    if not text.startswith("<"):
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        if lines and all(re.match(r"https?://\S+$", ln) for ln in lines[:50]):
            out["format"] = "txt"
            out["entries"] = [{"url": ln, "lastmod": None, "alts": [], "news_date": None}
                              for ln in lines if re.match(r"https?://\S+$", ln)]
        else:
            out["error"] = "XML 도 주소 목록도 아닙니다"
        return out
    head = text[:400].lower()
    if head.startswith(("<!doctype html", "<html")):
        out["error"] = "사이트맵 자리에 HTML 이 옵니다"
        return out
    if "<!doctype" in head or "<!entity" in head:
        out["error"] = "DOCTYPE 이 든 XML 은 읽지 않습니다"
        return out
    try:
        root = ET.fromstring(text.strip())
    except (ET.ParseError, ValueError) as e:
        out["error"] = f"XML 로 안 읽힙니다 ({e})"[:140]
        return out
    kind = _local(root.tag)

    def child_text(el, name):
        for c in el:
            if _local(c.tag) == name:
                return (c.text or "").strip() or None
        return None

    if kind == "sitemapindex":
        out["format"] = "index"
        out["children"] = [t for e in root if _local(e.tag) == "sitemap"
                           and (t := child_text(e, "loc"))]
    elif kind == "urlset":
        out["format"] = "urlset"
        for e in root:
            if _local(e.tag) != "url":
                continue
            loc = child_text(e, "loc")
            if not loc:
                continue
            alts, news_date = [], None
            for c in e:
                ln = _local(c.tag)
                # <xhtml:link rel="alternate" hreflang="de" href="…"/> — 사이트맵으로 알리는 hreflang
                if ln == "link" and c.get("hreflang") and "alternate" in (c.get("rel") or "").lower():
                    alts.append((c.get("hreflang").strip(), (c.get("href") or "").strip()))
                elif ln == "news":
                    out["news"] += 1
                    news_date = next(((x.text or "").strip() for x in c.iter()
                                      if _local(x.tag) == "publication_date"), None)
            out["entries"].append({"url": loc, "lastmod": child_text(e, "lastmod"),
                                   "alts": alts, "news_date": news_date})
    elif kind == "rss":
        out["format"] = "rss"
        for item in root.iter():
            if _local(item.tag) == "item" and (loc := child_text(item, "link")):
                out["entries"].append({"url": loc, "lastmod": None, "alts": [], "news_date": None})
    elif kind == "feed":
        out["format"] = "atom"
        for entry in root:
            if _local(entry.tag) != "entry":
                continue
            href = next((c.get("href") for c in entry if _local(c.tag) == "link" and c.get("href")
                         and (c.get("rel") or "alternate") == "alternate"), None)
            if href:
                out["entries"].append({"url": href.strip(), "lastmod": child_text(entry, "updated"),
                                       "alts": [], "news_date": None})
    else:
        out["error"] = f"모르는 형식 <{kind}>"
    return out


def read_sitemaps(urls, fetch, *, max_files: int = MAX_SITEMAP_FILES,
                  max_urls: int = READ_CAP) -> dict:
    """사이트맵들을 읽어 한 벌로. 색인이면 한 겹만 따라간다(색인 안의 색인은 규격 밖이다).

    {"files": [파일 요약(entries 는 개수 n 으로)], "entries": [{url,lastmod,source,alts,news_date}],
     "capped": 상한에서 잘렸나, "dups": {주소: [실은 파일…]}} — 주소는 받은 글자 그대로다
    (정규화는 부르는 쪽이 한다).
    """
    files: list[dict] = []
    entries: list[dict] = []
    owner: dict[str, str] = {}
    dups: dict[str, list[str]] = {}
    capped = False
    seen_files: set[str] = set()

    def take(f: dict) -> None:
        nonlocal capped
        for e in f["entries"]:
            u = e["url"]
            if u in owner:
                if owner[u] != f["url"]:
                    dups.setdefault(u, [owner[u]]).append(f["url"])
                continue
            if len(entries) >= max_urls:
                capped = True
                continue
            owner[u] = f["url"]
            entries.append({**e, "source": f["url"]})

    def summary(f: dict, **more) -> dict:
        return {**{k: f[k] for k in ("url", "status", "format", "gz", "bytes", "error", "news")},
                "n": len(f["entries"]) + len(f["children"]), **more}

    for u in urls:
        if u in seen_files:
            continue
        seen_files.add(u)
        f = parse_sitemap(u, fetch(u))
        if f["format"] != "index":
            take(f)
            files.append(summary(f))
            continue
        kids = [k for k in f["children"] if k not in seen_files]
        files.append(summary(f, skipped=max(0, len(kids) - max_files)))
        if len(kids) > max_files:
            capped = True
        for k in kids[:max_files]:
            seen_files.add(k)
            c = parse_sitemap(k, fetch(k))
            if c["format"] == "index":
                c["error"] = "색인 안에 색인이 있습니다(규격 밖 — 따라가지 않습니다)"
                c["children"] = []
            take(c)
            files.append(summary(c, parent=u))
    return {"files": files, "entries": entries, "capped": capped, "dups": dups}


def sitemap_file_issues(disc: dict, home: str) -> list[tuple]:
    """사이트맵 파일 자체의 문제 — 못 읽음(sitemap_unreadable)과 위생(sitemap_hygiene)."""
    out: list[tuple] = []
    host = scoring.host_of(home)
    by_file: dict[str, list[dict]] = defaultdict(list)
    for e in disc.get("entries") or []:
        by_file[e["source"]].append(e)
    for f in disc.get("files") or []:
        if f.get("format") is None:
            out.append(("sitemap_unreadable", f["url"], f.get("error") or "읽지 못했습니다"))
            continue
        notes = []
        if f.get("error"):
            notes.append(f["error"])
        if f.get("skipped"):
            notes.append(f"하위 사이트맵 {f['skipped']}개는 읽는 상한 밖이라 안 읽었습니다")
        if (f.get("n") or 0) > SITEMAP_MAX_URLS:
            notes.append(f"주소 {f['n']:,}개 — 한 파일 한도 {SITEMAP_MAX_URLS:,}개를 넘습니다")
        if (f.get("bytes") or 0) > SITEMAP_MAX_BYTES:
            notes.append(f"{f['bytes'] // 1_048_576}MB — 한 파일 한도 50MB 를 넘습니다")
        mine = by_file.get(f["url"]) or []
        long_ = [e["url"] for e in mine if len(e["url"]) >= LOC_MAX]
        if long_:
            notes.append(f"<loc> 가 {LOC_MAX:,}자 이상인 주소 {len(long_)}개")
        other = [e["url"] for e in mine
                 if (h := scoring.host_of(e["url"])) and not scoring.owns(h, host)]
        if other:
            notes.append(f"다른 호스트의 주소 {len(other)}개 (예: {other[0][:120]})")
        tok = [e["url"] for e in mine
               if any(_TOKEN_PARAMS.match(k) for k, _ in parse_qsl(urlsplit(e["url"]).query,
                                                                  keep_blank_values=True))]
        if tok:
            notes.append(f"세션·추적 파라미터가 붙은 주소 {len(tok)}개 (예: {tok[0][:120]})")
        if notes:
            out.append(("sitemap_hygiene", f["url"], " · ".join(notes)))
    dups = disc.get("dups") or {}
    if dups:
        u, where = sorted(dups.items())[0]
        out.append(("sitemap_hygiene", home,
                    f"여러 사이트맵 파일에 겹쳐 실린 주소 {len(dups)}개 (예: {u[:120]} — "
                    f"{len(set(where))}개 파일)"))
    return out


def _day(s) -> date | None:
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", str(s or ""))
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def lastmod_issue(entries, pages, home: str, *, today: date | None = None) -> list[tuple]:
    """사이트맵 lastmod 의 품질 — 사이트 단위 한 줄.

    구글은 lastmod 를 "일관되고 검증 가능하게 정확할 때" 쓴다고 적는다 — 틀린 값을 주면 통째로
    안 믿는다. https://developers.google.com/search/docs/crawling-indexing/sitemaps/build-sitemap
    셋을 본다: W3C 형식이 아닌 값, 전부 같은 날(빌드 시각을 찍은 것), 페이지가 말하는
    수정일(crawl_pages.modified)과 어긋나는 값.
    """
    today = today or date.today()
    have = [e for e in entries if e.get("lastmod")]
    if not have:
        return []
    notes = []
    bad = [e for e in have if not _W3C.fullmatch(e["lastmod"].strip()) or (
        "-" in e["lastmod"] and len(e["lastmod"]) >= 10 and _day(e["lastmod"]) is None)]
    if bad:
        notes.append(f"W3C 날짜 형식이 아닌 값 {len(bad)}개 (예: 「{bad[0]['lastmod'][:40]}」)")
    days = [d for e in have if (d := _day(e["lastmod"]))]
    future = [d for d in days if d > today + timedelta(days=1)]
    if future:
        notes.append(f"앞날의 날짜 {len(future)}개 (예: {max(future).isoformat()})")
    if len(days) >= LASTMOD_MIN:
        top, n = Counter(days).most_common(1)[0]
        if n / len(days) >= LASTMOD_SAME:
            notes.append(f"lastmod {len(days)}개 중 {n}개({n * 100 // len(days)}%)가 같은 날"
                         f"({top.isoformat()})입니다. 고친 날이 아니라 빌드한 날을 찍은 값으로 보입니다")
    mod = {p["url"]: _day(p.get("modified")) for p in pages if p.get("modified")}
    off = []
    for e in have:
        a, b = _day(e["lastmod"]), mod.get(e.get("norm") or e["url"])
        if a and b and abs((a - b).days) > LASTMOD_SLACK:
            off.append((e.get("norm") or e["url"], a, b))
    if off:
        u, a, b = off[0]
        both = sum(1 for e in have if _day(e["lastmod"]) and mod.get(e.get("norm") or e["url"]))
        notes.append(f"페이지가 말하는 수정일과 다른 값 {len(off)}개(둘 다 있는 {both}개 중). "
                     f"예: {urlsplit(u).path or '/'} — lastmod {a.isoformat()}, 페이지 {b.isoformat()}")
    return [("sitemap_lastmod", home, " · ".join(notes))] if notes else []


def news_issue(disc: dict, home: str, *, today: date | None = None) -> list[tuple]:
    """뉴스 사이트맵 규칙 — 한 파일에 news:news 1,000개까지, 최근 2일 기사만. 뉴스 발행 사이트용."""
    today = today or date.today()
    out = []
    old_by: dict[str, list] = defaultdict(list)
    for e in disc.get("entries") or []:
        d = _day(e.get("news_date"))
        if d and (today - d).days > NEWS_DAYS:
            old_by[e["source"]].append((e["url"], d))
    for f in disc.get("files") or []:
        notes = []
        if (f.get("news") or 0) > NEWS_MAX:
            notes.append(f"news:news 태그 {f['news']:,}개 — 한 파일 한도 {NEWS_MAX:,}개를 넘습니다")
        old = old_by.get(f["url"]) or []
        if old:
            u, d = old[0]
            notes.append(f"{NEWS_DAYS}일보다 오래된 기사 {len(old)}개 (예: {urlsplit(u).path} — "
                         f"{d.isoformat()})")
        if notes:
            out.append(("sitemap_news", f["url"], " · ".join(notes)))
    return out


# ── 헤더 ────────────────────────────────────────────────────────────────────
# 값이 쌍점을 갖는 규칙 — 봇 이름('googlebot: noindex')과 헷갈리지 않게.
_ROBOTS_VALUED = ("max-snippet", "max-image-preview", "max-video-preview", "unavailable_after")
# 색인 판정에 걸리는 봇 — 이름 없이 준 규칙(모든 크롤러)과 구글 것만 센다.
_ROBOTS_FOR = ("googlebot", "google")


def x_robots_effective(value) -> str | None:
    """X-Robots-Tag 헤더에서 모든 크롤러·구글에 걸리는 규칙만 — 소문자, 쉼표로 이어서.

    "Rules specified without a user agent are valid for all crawlers", 봇 이름을 앞에 둘 수
    있다('X-Robots-Tag: otherbot: noindex, nofollow').
    https://developers.google.com/search/docs/crawling-indexing/robots-meta-tag
    requests 는 같은 이름의 헤더 여러 줄을 ', ' 로 이어 준다 — 줄 경계는 봇 이름이 다시 나오는
    자리로 본다. none 은 noindex, nofollow 다.
    """
    if not value:
        return None
    ua, rules = None, []
    for tok in str(value).split(","):
        t = tok.strip().lower()
        if not t:
            continue
        m = re.match(r"([a-z0-9_\-]+)\s*:\s*(.*)$", t)
        if m and m.group(1) not in _ROBOTS_VALUED:
            ua, t = m.group(1), m.group(2).strip()
            if not t:
                continue
        if ua is None or ua in _ROBOTS_FOR:
            rules += ["noindex", "nofollow"] if t == "none" else [t]
    return ", ".join(dict.fromkeys(rules)) or None


_LINK_PART = re.compile(r"<([^>]+)>((?:\s*;\s*[^;,<]+)*)")


def link_header_hreflang(value, base: str) -> list[list[str]]:
    """HTTP Link 헤더의 hreflang — [[코드, 주소], …].

    'Link: <https://example.com/file.pdf>; rel="alternate"; hreflang="en", <…>; …' — PDF 처럼
    <head> 가 없는 파일이 언어 판을 알리는 길이다.
    https://developers.google.com/search/docs/specialty/international/localized-versions
    """
    out = []
    for href, params in _LINK_PART.findall(str(value or "")):
        kv = {k.strip().lower(): v.strip().strip('"\'')
              for k, _, v in (p.partition("=") for p in params.split(";") if "=" in p)}
        if "alternate" in kv.get("rel", "").lower() and kv.get("hreflang"):
            out.append([kv["hreflang"], urljoin(base, href.strip())])
    return out


# ── 본문 지문 ───────────────────────────────────────────────────────────────
# 띄어쓰기 없는 글(가나·한자·태국 문자)은 글자 하나가 낱말 노릇을 한다 — \w+ 로 자르면 한 문장이
# 통째로 한 낱말이 되어 조각이 안 나온다.
_NOSPACE = "぀-ヿ㐀-鿿฀-๿"
_TOK = re.compile(rf"[{_NOSPACE}]|[^\W_{_NOSPACE}]+")
# 잇닿은 낱말 몇 개를 한 조각으로. 둘이다: 지역명 하나가 바뀌면 그 낱말이 낀 조각이 SHINGLE 개
# 달라진다 — 60낱말짜리 도어웨이에 지역명이 다섯 번 나와도(8%) NEAR_DUP 을 넘기려면 둘이어야 한다.
SHINGLE = 2
SKETCH = 64                   # 지문 크기(가장 작은 해시 몇 개)
SKETCH_MIN = 30               # 낱말이 이보다 적으면 지문을 안 만든다(견줄 만큼이 아니다)
NEAR_DUP = 0.7                # 조각이 이만큼 겹치면 '거의 같은 본문'
TEMPLATE_MIN = 3              # 한 묶음에 이만큼은 있어야 틀 문제다
TEMPLATE_SAMPLE = 200         # 묶음마다 맞대 보는 장 수 상한(쌍 비교라 제곱으로 는다)


def body_tokens(text: str) -> list[str]:
    return _TOK.findall(unicodedata.normalize("NFKC", text or "").casefold())


def body_hash(text: str) -> str | None:
    """본문 글자의 해시 — 낱말만(공백·구두점·대소문자 차이는 같은 본문). 낱말이 없으면 None."""
    toks = body_tokens(text)
    if len(toks) < SKETCH_MIN:
        return None
    return hashlib.sha1(" ".join(toks).encode("utf-8")).hexdigest()[:16]


def body_sketch(text: str) -> tuple[int, ...]:
    """본문의 지문 — 낱말 SHINGLE 개짜리 조각 해시 중 가장 작은 SKETCH 개(bottom-k).

    crc32 를 쓴다: 파이썬 hash() 는 프로세스마다 소금이 달라 회차 사이에 못 견준다."""
    toks = body_tokens(text)
    if len(toks) < SKETCH_MIN:
        return ()
    hs = {zlib.crc32(" ".join(toks[i:i + SHINGLE]).encode("utf-8"))
          for i in range(len(toks) - SHINGLE + 1)}
    return tuple(sorted(hs)[:SKETCH])


def sketch_sim(a, b) -> float:
    """두 지문의 겹침(자카드 추정) 0~1."""
    if not a or not b:
        return 0.0
    sa, sb = set(a), set(b)
    u = sorted(sa | sb)[:min(SKETCH, len(sa | sb))]
    return sum(1 for x in u if x in sa and x in sb) / len(u)


def _indexable(p: dict) -> bool:
    """색인될 만한 장인가 — 200 HTML, noindex 아님, canonical 이 없거나 자기 자신."""
    if p.get("status") != 200 or p.get("redirect_to") or p.get("title") is None and p.get("words") is None:
        return False
    if "noindex" in (p.get("robots") or "").lower() or "noindex" in (p.get("x_robots") or ""):
        return False
    c = (p.get("canonical") or "").strip()
    return not c or c == p["url"]


def _paths(urls, n: int = 3) -> str:
    return ", ".join(_path(u) for u in list(urls)[:n])


def _path(u: str) -> str:
    sp = urlsplit(u)
    return (sp.path or "/") + (f"?{sp.query}" if sp.query else "")


def template_thin(pages) -> list[tuple]:
    """같은 주소 꼴(/city/*) 안에서 본문이 거의 같은 페이지 묶음 — 지역명만 바꿔 끼운 도어웨이.

    묶음마다 한 줄. 주소는 묶음에서 가장 짧은 것. 메뉴·꼬리말은 지문에서 빠진다(parse_page 가
    틀 밖 글자로만 만든다) — 시맨틱 태그 없이 짠 사이트는 틀을 못 갈라 전체 글자로 견준다."""
    groups: dict[str, list[dict]] = defaultdict(list)
    for p in pages:
        if p.get("sketch") and _indexable(p):
            groups[p.get("tpl") or url_template(p["url"])].append(p)
    out = []
    for tpl, ps in sorted(groups.items()):
        if len(ps) < TEMPLATE_MIN or tpl == "/":
            continue
        ps = sorted(ps, key=lambda p: p["url"])[:TEMPLATE_SAMPLE]
        parent = list(range(len(ps)))

        def find(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i
        for i in range(len(ps)):
            for j in range(i + 1, len(ps)):
                if sketch_sim(ps[i]["sketch"], ps[j]["sketch"]) >= NEAR_DUP:
                    parent[find(i)] = find(j)
        cl: dict[int, list[str]] = defaultdict(list)
        for i, p in enumerate(ps):
            cl[find(i)].append(p["url"])
        for urls in cl.values():
            if len(urls) < TEMPLATE_MIN:
                continue
            urls.sort(key=lambda u: (len(u), u))
            out.append(("template_thin", urls[0],
                        f"{tpl} 꼴 {len(ps)}장 중 {len(urls)}장의 본문이 서로 "
                        f"{int(NEAR_DUP * 100)}% 이상 같습니다 (예: {_paths(urls)})"))
    return out


# ── 템플릿 메타 ─────────────────────────────────────────────────────────────
META_TPL_MIN = 3              # 이만큼은 걸려야 틀이다 — 한두 장은 손으로 그렇게 쓴 것일 수 있다
META_TPL_RATIO = 0.10         # 설명이 있는 장 중 이 비율 이상이면 사이트 단위로 말한다
META_TPL_WIDE = 0.30          # 이 비율 이상이면 틀이 기본값이다 — 한 단계 무겁게


def meta_template_issue(pages, home: str) -> list[tuple]:
    """설명 칸이 틀에서 나온 페이지의 비율 — 사이트 단위 한 줄. 판정 규칙은
    scoring.desc_template_kind 한 벌이다(페이지 처방과 같은 함수)."""
    have = [p for p in pages if _indexable(p) and (p.get("description") or "").strip()]
    hits: dict[str, list[dict]] = defaultdict(list)
    for p in have:
        k = scoring.desc_template_kind(p["description"], p.get("title") or "",
                                       lang=p.get("lang") or "")
        if k:
            hits[k].append(p)
    n = sum(len(v) for v in hits.values())
    if n < META_TPL_MIN or n / len(have) < META_TPL_RATIO:
        return []
    ratio = n / len(have)
    parts = " · ".join(f"{scoring.DESC_TEMPLATE_KINDS[k]} {len(v)}"
                       for k, v in sorted(hits.items(), key=lambda kv: -len(kv[1])))
    ex = max(hits.values(), key=len)[0]
    detail = (f"설명이 있는 {len(have)}장 중 {n}장({int(ratio * 100)}%)의 설명이 틀에서 나왔습니다 — "
              f"{parts}. 예: {_path(ex['url'])} 「{ex['description'][:70]}」")
    return [("meta_template", home, detail, "warn" if ratio >= META_TPL_WIDE else "info")]


# ── URL 위생 ────────────────────────────────────────────────────────────────
URL_LONG = 200                # 이보다 긴 주소 — 한도가 아니라 '손으로 못 옮기는 길이'의 기준이다


def url_hygiene(pages) -> list[tuple]:
    """색인될 주소의 꼴 — 쿼리 파라미터·대문자·밑줄·이중 슬래시·긴 주소. 주소마다 한 줄(참고).

    구글은 주소를 단순하게, 낱말은 밑줄 대신 붙임표로 잇기를 권한다.
    https://developers.google.com/search/docs/crawling-indexing/url-structure"""
    out = []
    for p in pages:
        if not _indexable(p):
            continue
        sp = urlsplit(p["url"])
        notes = []
        if sp.query:
            notes.append("쿼리 파라미터가 붙은 주소가 색인 대상입니다")
        if sp.path != sp.path.lower():
            notes.append("경로에 대문자")
        if "_" in sp.path:
            notes.append("경로에 밑줄(낱말은 붙임표로 잇습니다)")
        if "//" in sp.path:
            notes.append("이중 슬래시")
        if len(p["url"]) > URL_LONG:
            notes.append(f"{len(p['url'])}자")
        if notes:
            out.append(("url_hygiene", p["url"], " · ".join(notes)))
    return out


# ── 사이트맵 ↔ 크롤 ─────────────────────────────────────────────────────────
def sitemap_vs_crawl(sitemap_urls, pages, *, capped: bool = False) -> list[tuple]:
    """사이트맵에 넣으면 안 되는 주소(sitemap_bad_url)와 색인 가능한데 빠진 주소(not_in_sitemap).

    사이트맵은 "색인해 달라는 주소"다 — 리다이렉트·4xx·noindex·canonical 이 다른 주소가 있으면
    구글이 목록 전체를 덜 믿는다. 속도 제한(429·503)·봇 차단(403)은 그 주소의 결함이 아니라
    세지 않는다. 빠진 주소는 사이트맵을 다 읽었을 때만 말한다(capped 면 모른다).
    """
    sm = set(sitemap_urls)
    if not sm:
        return []
    by = {p["url"]: p for p in pages}
    out = []
    for u in sorted(sm):
        p = by.get(u)
        if not p:
            continue
        st = p.get("status")
        why = None
        if p.get("redirect_to"):
            why = f"리다이렉트됩니다 → {p['redirect_to']}"
        elif isinstance(st, int) and st >= 400 and st not in RATE_STATUS and st != 403:
            why = f"HTTP {st}"
        elif "noindex" in (p.get("robots") or "").lower() or "noindex" in (p.get("x_robots") or ""):
            why = "noindex 입니다"
        elif (p.get("canonical") or "").strip() and p["canonical"] != u:
            why = f"canonical 이 다른 주소입니다 → {p['canonical']}"
        if why:
            out.append(("sitemap_bad_url", u, why))
    if not capped:
        for p in pages:
            if p["url"] not in sm and _indexable(p) and p.get("title") is not None:
                out.append(("not_in_sitemap", p["url"],
                            "색인될 수 있는 페이지인데 사이트맵에 없습니다"))
    return out


# ── hreflang ────────────────────────────────────────────────────────────────
def hreflang_issues(pages) -> list[tuple]:
    """hreflang 대상·묶음·코드 — 대상은 주소마다, 묶음·코드는 사이트 한 줄(틀의 문제다).

    대상(hreflang_bad_target): 가리킨 주소가 리다이렉트·4xx·noindex·canonical 이 다른 주소.
    구글은 대체 주소를 "완전한 주소"로, 서로 가리키게 달라고 한다 — 리다이렉트되는 주소는
    되받는 짝이 없다. 묶음(hreflang_mixed): 한 묶음 안에 http/https 가 섞이거나 끝 슬래시가
    엇갈린다. 코드(hreflang_bad_code): scoring.hreflang_code_ok 가 아닌 값.
    """
    by = {p["url"]: p for p in pages}
    out = []
    mixed, codes = [], defaultdict(list)
    for p in pages:
        hl = p.get("hreflang") or []
        if not hl:
            continue
        bad = []
        for code, href in hl:
            if not scoring.hreflang_code_ok(code):
                codes[str(code)].append(p["url"])
            t = by.get(href)
            if not t or href == p["url"]:
                continue
            st = t.get("status")
            if t.get("redirect_to"):
                bad.append(f"{href} ({code}) 는 리다이렉트됩니다 → {t['redirect_to']}")
            elif isinstance(st, int) and st >= 400 and st not in RATE_STATUS and st != 403:
                bad.append(f"{href} ({code}) 는 HTTP {st}")
            elif "noindex" in (t.get("robots") or "").lower() or "noindex" in (t.get("x_robots") or ""):
                bad.append(f"{href} ({code}) 는 noindex 입니다")
            elif (t.get("canonical") or "").strip() and t["canonical"] != href:
                bad.append(f"{href} ({code}) 의 canonical 은 {t['canonical']} 입니다")
        if bad:
            out.append(("hreflang_bad_target", p["url"],
                        bad[0] + (f" 외 {len(bad) - 1}개" if len(bad) > 1 else "")))
        hrefs = [urlsplit(h) for _, h in hl]
        notes = []
        if len({s.scheme for s in hrefs if s.scheme}) > 1:
            notes.append("http 와 https 가 섞였습니다")
        # 끝 슬래시 — 홈('/')과 파일 꼴(.html)은 견주지 않는다
        deep = [s.path for s in hrefs if s.path not in ("", "/")
                and "." not in s.path.rsplit("/", 1)[-1]]
        if len({pth.endswith("/") for pth in deep}) > 1:
            notes.append("끝 슬래시가 있는 주소와 없는 주소가 섞였습니다")
        if notes:
            mixed.append((p["url"], notes))
    if mixed:
        u, notes = min(mixed, key=lambda t: (len(t[0]), t[0]))
        kinds = sorted({n for _, ns in mixed for n in ns})
        out.append(("hreflang_mixed", u,
                    f"hreflang 묶음 {len(mixed)}장: " + " · ".join(kinds) + f" (예: {_path(u)})"))
    if codes:
        u = min((us[0] for us in codes.values()), key=lambda x: (len(x), x))
        out.append(("hreflang_bad_code", u,
                    "코드가 아닌 값: " + ", ".join(f"{c} ({len(set(us))}장)"
                                                  for c, us in sorted(codes.items())[:6])))
    return out


# ── 신뢰 페이지 · 매장 찾기 ─────────────────────────────────────────────────
# 주소·앵커에 이 말이 있으면 그 페이지다. 언어를 가리지 않고 한 벌 — 사이트 언어를 몰라도 된다.
# (라벨, 주소·앵커에 나오는 말들 — 소문자)
TRUST_PAGES = {
    "contact": ("연락처", ("contact", "kontakt", "contacto", "contatti", "contato", "문의",
                           "연락처", "오시는", "お問い合わせ", "お問合せ", "联系", "聯絡", "聯繫",
                           "связаться", "контакт", "iletisim", "iletişim", "اتصل")),
    "privacy": ("개인정보처리방침", ("privacy", "datenschutz", "privacidad", "privacidade",
                                    "confidentialite", "confidentialité", "개인정보",
                                    "プライバシー", "個人情報", "隐私", "隱私", "конфиденциальн",
                                    "gizlilik", "الخصوصية")),
    "terms": ("이용약관", ("terms", "conditions", "tos", "agb", "nutzungsbedingungen", "terminos",
                           "términos", "condiciones", "cgu", "cgv", "termos", "이용약관", "약관",
                           "利用規約", "规约", "條款", "条款", "условия", "kosullar", "koşullar",
                           "الشروط")),
    "reviews": ("후기", ("review", "testimonial", "bewertung", "erfahrungen", "opiniones",
                         "resenas", "reseñas", "avis", "recensioni", "depoimentos", "avaliacoes",
                         "후기", "리뷰", "レビュー", "口コミ", "お客様の声", "评价", "評價", "отзыв",
                         "yorum")),
}
# 매장 찾기·지점 목록 페이지의 주소·앵커에 나오는 말
LOCATOR_WORDS = ("store-locator", "storelocator", "find-a-store", "find-store", "stores", "locations",
                 "our-locations", "branches", "매장찾기", "매장안내", "매장 찾기", "지점안내",
                 "지점 안내", "店舗", "店舗一覧", "门店", "門市", "分店", "filialen", "filialfinder",
                 "standorte", "tiendas", "sucursales", "magasins", "nos-magasins", "negozi",
                 "punti-vendita", "lojas")
# 지도 임베드 — iframe src 의 호스트·경로
MAP_EMBED = re.compile(r"google\.[a-z.]+/maps|maps\.google\.|maps\.googleapis\.com|map\.naver\.com|"
                       r"map\.kakao\.com|openstreetmap\.org|bing\.com/maps|yandex\.[a-z]+/map|"
                       r"map\.baidu\.com|amap\.com|maps\.apple\.com", re.I)


def _word_hit(words, *texts) -> bool:
    blob = " ".join(unicodedata.normalize("NFKC", t or "").casefold() for t in texts)
    for w in words:
        if not w.isascii():
            if w in blob:
                return True
        # 짧은 말(tos·agb)은 낱말 통째로, 긴 말은 앞머리로(review → reviews)
        elif re.search(rf"(?<![a-z0-9]){re.escape(w)}" + ("(?![a-z0-9])" if len(w) <= 4 else ""),
                       blob):
            return True
    return False


def trust_pages(pages, links, home: str) -> tuple[dict, list[tuple]]:
    """신뢰 페이지(연락처·개인정보처리방침·약관·후기)가 있나 — ({종류: 주소|None}, 이슈).

    크롤한 주소와 내부 링크의 목적지·앵커로 본다(꼬리말 링크는 상한 밖 페이지도 드러낸다).
    없다는 것은 '크롤이 그리로 가는 링크를 못 봤다'는 뜻이다 — 그래서 참고다."""
    cand: list[tuple[str, str]] = [(p["url"], p.get("title") or "") for p in pages
                                   if p.get("status") == 200 and not p.get("redirect_to")]
    cand += [(x["url_to"], x.get("anchor") or "") for x in links if x.get("is_internal")]
    found: dict[str, str | None] = {}
    for kind, (_label, words) in TRUST_PAGES.items():
        found[kind] = next((u for u, text in cand
                            if _word_hit(words, urlsplit(u).path.replace("-", " ").replace("_", " "),
                                         text)), None)
    miss = [TRUST_PAGES[k][0] for k, u in found.items() if not u]
    if not miss or not cand:
        return found, []
    return found, [("trust_missing", home,
                    "크롤이 못 찾은 페이지: " + " · ".join(miss)
                    + ". 주소나 링크 글자에 그 뜻의 말이 있는 내부 페이지가 없습니다")]


def locator_issues(pages, links, *, allowed=None, thin: int = 200) -> list[tuple]:
    """매장 찾기·지점 목록 페이지를 크롤러가 읽을 수 있나 — 페이지마다 한 줄.

    지점 목록을 스크립트로만 그리면 HTML 에는 지도 틀뿐이라 지점 페이지로 가는 링크가 없다.
    robots 로 막혔거나 noindex 여도 같은 결과다. allowed(url) 는 robots 판정."""
    by = {p["url"]: p for p in pages}
    seen, out = set(), []
    cand = [(x["url_to"], x.get("anchor") or "") for x in links if x.get("is_internal")]
    cand += [(p["url"], "") for p in pages]
    for u, anchor in cand:
        if u in seen:
            continue
        slug = urlsplit(u).path.rstrip("/").rsplit("/", 1)[-1]
        if not _word_hit(LOCATOR_WORDS, slug, slug.replace("-", " "), anchor):
            continue
        seen.add(u)
        p = by.get(u)
        why = None
        if allowed is not None and not allowed(u):
            why = "robots.txt 가 이 주소를 막습니다"
        elif not p:
            continue
        elif p.get("redirect_to"):
            continue
        elif isinstance(p.get("status"), int) and p["status"] >= 400 and p["status"] not in RATE_STATUS \
                and p["status"] != 403:
            why = f"HTTP {p['status']}"
        elif "noindex" in (p.get("robots") or "").lower() or "noindex" in (p.get("x_robots") or ""):
            why = "noindex 입니다"
        elif p.get("status") == 200 and (p.get("words") or 0) < thin and (p.get("links_main") or 0) == 0:
            why = (f"HTML 에 본문 {p.get('words') or 0}단어, 본문 링크 0개 — 지점 목록을 스크립트로만 "
                   "그리는 것으로 보입니다" + (" (지도 임베드는 있습니다)" if p.get("maps") else ""))
        if why:
            out.append(("locator_uncrawlable", u, why))
    return out


# ── 탐침 (fetch 를 넘겨받는다) ──────────────────────────────────────────────
def exists(r: dict) -> int | None:
    """파일이 있나 — 1 있음 · 0 봤고 없음 · None 모름. collect_crawl.probe_llms_txt 와 같은 세 상태:
    404/410 과 '200 인데 HTML'(없는 주소를 홈으로 돌려보내는 사이트)은 없음, 나머지 실패는 모름."""
    st = r.get("status")
    if st in (404, 410):
        return 0
    if st != 200:
        return None
    head = (r.get("text") or "").lstrip()[:200].lower()
    if "html" in (r.get("content_type") or "").lower() or head.startswith(("<!doctype", "<html")):
        return 0
    return 1


# 에이전트용 발견 파일 — **있나 없나만** 적는다. 전부 초안이거나 검색엔진이 쓴다는 근거가 없는
# 표준이라 기회로 만들지 않는다(없는 것을 결함으로 부르면 오진이다). 경로는 1차 출처에서 확인했다.
# (경로, 이름, 출처)
WELL_KNOWN = (
    ("/.well-known/api-catalog", "API 카탈로그(RFC 9727)",
     "https://www.rfc-editor.org/rfc/rfc9727.html"),
    ("/.well-known/oauth-protected-resource", "OAuth 보호 자원 메타데이터(RFC 9728)",
     "https://www.rfc-editor.org/rfc/rfc9728.html"),
    ("/.well-known/oauth-authorization-server", "OAuth 인가 서버 메타데이터(RFC 8414)",
     "https://www.rfc-editor.org/rfc/rfc8414.html"),
    ("/.well-known/agent-card.json", "A2A 에이전트 카드",
     "https://a2a-protocol.org/latest/specification/"),
    ("/.well-known/ucp", "UCP 사업자 프로필", "https://ucp.dev/specification/overview/"),
    # ARD 는 v0.91 제안 단계고 경로가 바뀌었다 — 지금은 ard.json, 앞선 판이 ai-catalog.json.
    ("/.well-known/ard.json", "ARD 카탈로그(ard.json)", "https://agenticresourcediscovery.org/spec"),
    ("/.well-known/ai-catalog.json", "ARD 옛 경로(ai-catalog.json)",
     "https://agenticresourcediscovery.org/spec"),
)


def probe_well_known(home: str, fetch) -> list[dict]:
    """WELL_KNOWN 을 한 번씩 — [{"path","name","found": 1|0|None}]."""
    return [{"path": path, "name": name, "found": exists(fetch(urljoin(home, path)))}
            for path, name, _src in WELL_KNOWN]


def probe_soft404(home: str, fetch, token: str) -> dict:
    """없는 주소를 한 번 두드린다 — {"url","status","soft": 1|0|None}.

    없는 주소에 200 을 주면(오류 화면을 200 으로 주거나 홈으로 돌려보내면) 구글이 '소프트 404'
    로 적고, 지운 페이지가 색인에서 안 빠진다. 4xx 면 정상(0), 못 받았으면 모름(None)."""
    url = urljoin(home, f"/seo-miner-probe-{token}")
    r = fetch(url)
    st = r.get("status")
    soft = None if st is None or st in RATE_STATUS or st == 403 or st >= 500 else \
        (1 if 200 <= st < 300 else 0)
    return {"url": url, "status": st, "final": r.get("final_url"), "soft": soft}


def probe_markdown(home: str, fetch_accept, sample: str | None = None) -> dict:
    """Markdown 협상 — 'Accept: text/markdown' 에 markdown 으로 답하나, '.md' 형제가 있나.
    {"accept": 1|0|None, "sibling": 1|0|None}. 어느 엔진이 이 헤더를 보낸다는 근거는 없다 — 기록만."""
    r = fetch_accept(home, "text/markdown")
    st = r.get("status")
    accept = None if st != 200 else int("markdown" in (r.get("content_type") or "").lower())
    sibling = None
    if sample and "." not in urlsplit(sample).path.rsplit("/", 1)[-1]:
        sibling = exists(fetch_accept(sample.rstrip("/") + ".md", None))
    return {"accept": accept, "sibling": sibling}


CERT_WARN_DAYS = 14           # 자동 갱신은 보통 30일 전에 돈다 — 이만큼 남았으면 갱신이 실패한 것이다


def host_checks(home: str, fetch, *, cert_days=None) -> tuple[dict, list[tuple]]:
    """호스트 정규화 — http→https, www/비www, 인증서. (사실, 이슈).

    http 판이 그대로 열리거나 www·비www 가 둘 다 200 이면 같은 사이트가 두 주소로 색인된다.
    보안 헤더 등급은 보지 않는다(순위 신호가 아니다) — HTTPS 강제와 HSTS 유무만."""
    sp = urlsplit(home)
    host = sp.netloc
    facts: dict = {"http": None, "alt_host": None, "cert_days": None}
    issues: list[tuple] = []
    if sp.scheme == "https":
        r = fetch(f"http://{host}/")
        st, final = r.get("status"), (r.get("final_url") or "")
        if st is not None and st not in RATE_STATUS:
            first = (r.get("chain") or [(None, None)])[0][1]
            if not final.startswith("https://"):
                if st < 400:
                    facts["http"] = "open"
                    issues.append(("host_http", f"http://{host}/",
                                   f"http 주소가 https 로 넘어가지 않고 그대로 열립니다(HTTP {st})"))
            elif first not in (301, 308):
                facts["http"] = f"redirect_{first}"
                issues.append(("host_http", f"http://{host}/",
                               f"http → https 가 영구 리다이렉트(301)가 아니라 HTTP {first} 입니다"))
            else:
                facts["http"] = "redirect"
    alt = host[4:] if host.startswith("www.") else "www." + host
    r = fetch(f"{sp.scheme}://{alt}/")
    st = r.get("status")
    if st is not None and st not in RATE_STATUS:
        landed = urlsplit(r.get("final_url") or "").netloc.lower()
        stayed = landed == alt and not (r.get("chain") or [])
        if st == 200 and stayed:
            facts["alt_host"] = "dup"
            issues.append(("host_dup", f"{sp.scheme}://{alt}/",
                           f"{alt} 과 {host} 가 둘 다 200 으로 열립니다. 한쪽을 다른 쪽으로 301 하세요"))
        else:
            facts["alt_host"] = "redirect" if landed == host else "other"
    if cert_days is not None and sp.scheme == "https":
        d = cert_days(host)
        facts["cert_days"] = d
        if d is not None and d <= CERT_WARN_DAYS:
            issues.append(("cert_expiring", home,
                           "TLS 인증서가 만료됐습니다" if d < 0
                           else f"TLS 인증서가 {d}일 뒤 만료됩니다"))
    return facts, issues


def percentile(values, q: float) -> int | None:
    """가까운 순위법 — values 의 q(0~1) 분위. 비면 None."""
    vs = sorted(v for v in values if isinstance(v, (int, float)))
    if not vs:
        return None
    import math
    return int(vs[max(0, math.ceil(q * len(vs)) - 1)])


# ── 다국어 — collect_crawl.intl_issues 가 언어를 가른 뒤에 부른다 ───────────
# 이 모듈이 더하는 다국어 갈래(이름표·심각도는 collect_crawl 이 갖는다).
INTL_MORE_KINDS = ("intl_same_body", "intl_no_inlanguage", "intl_parity", "intl_stale",
                   "intl_legal_missing", "intl_format")
SAME_BODY = 0.9               # 다른 언어 판인데 본문 조각이 이만큼 겹치면 번역을 안 한 것이다(제목만 옮긴 판 포함)
PARITY_MIN = 4                # 기본 판에 이만큼은 있어야 견준다
PARITY_RATIO = 0.5            # 다른 판이 기본 판의 이 비율 이하면 빠진 것
STALE_DAYS = (90, 30, 7)      # 원문이 고쳐진 뒤 번역이 이만큼 늦다 — 큰 것부터
# 로케일별 법적 고지 — 그 언어 판이 있으면 있어야 하는 페이지. (라벨, 찾는 말, 사이트 종류 제한, 출처)
LEGAL_PAGES = {
    # 독일 DDG §5 "Allgemeine Informationspflichten": 디지털 서비스 제공자는 정보를 "leicht
    # erkennbar und unmittelbar erreichbar … ständig verfügbar" 하게 둔다(오스트리아 ECG·스위스 UWG
    # 도 같은 이름의 페이지를 쓴다). https://www.gesetze-im-internet.de/ddg/__5.html
    "de": ("Impressum", ("impressum",), None, "https://www.gesetze-im-internet.de/ddg/__5.html"),
    # 프랑스 LCEN(loi n° 2004-575) — 사업자 사이트는 "doit afficher plusieurs mentions obligatoires".
    # https://entreprendre.service-public.gouv.fr/vosdroits/F37351
    "fr": ("Mentions légales", ("mentions legales", "mentions légales", "mentions-legales"), None,
           "https://entreprendre.service-public.gouv.fr/vosdroits/F37351"),
    # 일본 特定商取引法 — 통신판매(인터넷 판매 포함) 광고에 사업자 이름·주소·전화를 적는다. 파는 사이트만.
    # https://www.no-trouble.caa.go.jp/what/mailorder/
    "ja": ("特定商取引法に基づく表記", ("特定商取引", "tokushoho", "tokutei", "specified-commercial",
                                      "legal-notice"), ("commerce",),
           "https://www.no-trouble.caa.go.jp/what/mailorder/"),
}
# 숫자 날짜 — 월/일/년(미국식)과 일/월/년이 갈리는 꼴만 본다: 13 이상인 칸이 어느 쪽인지 말해 준다.
_NUM_DATE = re.compile(r"(?<![\d/.])(\d{1,2})/(\d{1,2})/(\d{4})(?![\d/])")
# 숫자 날짜를 일/월/년(또는 년-월-일)으로 쓰는 언어 — 이 언어 판에 월/일/년이 있으면 원문(미국식)
# 표기가 그대로 남은 것이다. CLDR 의 짧은 날짜 꼴(https://cldr.unicode.org/)에서 d/M/y·dd.MM.y·y-MM-dd 인 언어.
DMY_LANGS = frozenset("de fr es it pt nl pl ru uk tr sv da nb fi cs sk el he ar id vi th hi ro hu "
                      "bg hr sr sl ko ja zh".split())
FORMAT_MIN = 2                # 한 장에 이만큼은 있어야 표기 문제다


def num_date_style(text: str) -> tuple[int, int, str | None]:
    """본문의 숫자 날짜 중 (월/일/년으로만 읽히는 수, 일/월/년으로만 읽히는 수, 월/일/년 예)."""
    mdy = dmy = 0
    ex = None
    for a, b, _y in _NUM_DATE.findall(text or ""):
        a, b = int(a), int(b)
        if a <= 12 < b <= 31:
            mdy += 1
            ex = ex or f"{a}/{b}/{_y}"
        elif b <= 12 < a <= 31:
            dmy += 1
    return mdy, dmy, ex


def intl_more(ctx: dict) -> list[tuple]:
    """다국어 판 사이의 동등성 — collect_crawl.intl_issues 가 만든 재료(ctx)로.

    ctx: by_url{url: page}, lang{url: 언어}, by_lang{언어: [url]}, groups[[같은 글의 판들]],
         D(기본 언어), nm(언어 → 이름), rep(urls → 대표 주소), links, site_type, sm_lastmod{url: lastmod}.
    전부 틀의 문제라 언어마다 한 줄로 낸다.
    """
    by_url, lang, by_lang = ctx["by_url"], ctx["lang"], ctx["by_lang"]
    D, nm, rep = ctx["D"], ctx["nm"], ctx["rep"]
    prim = lambda l: (l or "").split("-")[0]
    out: list[tuple] = []
    same: dict[str, list] = defaultdict(list)
    par: dict[str, list] = defaultdict(list)
    stale: dict[str, list] = defaultdict(list)
    sm = ctx.get("sm_lastmod") or {}

    def moddate(u):
        return _day(by_url[u].get("modified")) or _day(sm.get(u))

    for members in ctx["groups"]:
        base = next((u for u in members if lang[u] == D), None)
        if not base:
            continue
        b = by_url[base]
        for u in members:
            l = lang[u]
            if u == base or prim(l) == prim(D):
                continue
            p = by_url[u]
            # 1) 본문이 같다 — 다른 언어 판인데 글자가 (거의) 똑같으면 번역을 안 한 것이다
            if (b.get("body_hash") and b["body_hash"] == p.get("body_hash")) or                     sketch_sim(b.get("sketch"), p.get("sketch")) >= SAME_BODY:
                same[l].append((u, base))
            # 2) H2·이미지 수 — 기본 판의 절반 이하면 구간·그림이 빠졌다
            for field, label in (("h2s", "H2"), ("images", "이미지")):
                x, y = b.get(field) or 0, p.get(field) or 0
                if x >= PARITY_MIN and y <= x * PARITY_RATIO:
                    par[l].append((u, f"{label} {y}개(기본 판 {x}개)"))
            # 3) 번역 신선도 — 원문이 고쳐진 뒤에 번역은 그대로
            d0, d1 = moddate(base), moddate(u)
            if d0 and d1 and (d0 - d1).days >= STALE_DAYS[-1]:
                stale[l].append((u, (d0 - d1).days, d0, d1))
    for l, hits in sorted(same.items()):
        us = [u for u, _ in hits]
        out.append(("intl_same_body", rep(us),
                    f"{nm(l)} 페이지 {len(by_lang[l])}장 중 {len(us)}장의 본문이 {nm(D)} 판과 "
                    f"같습니다 (예: {_paths(sorted(us, key=lambda u: (len(u), u)))})"))
    for l, hits in sorted(par.items()):
        us = list(dict.fromkeys(u for u, _ in hits))
        u0, why = min(hits, key=lambda t: (len(t[0]), t[0]))
        out.append(("intl_parity", rep(us),
                    f"{nm(l)} 페이지 {len(by_lang[l])}장 중 {len(us)}장이 {nm(D)} 판보다 구간·그림이 "
                    f"절반 이하입니다 (예: {_path(u0)} — {why})"))
    for l, hits in sorted(stale.items()):
        us = [h[0] for h in hits]
        buckets = []
        left = list(hits)
        for days in STALE_DAYS:
            n = sum(1 for h in left if h[1] >= days)
            left = [h for h in left if h[1] < days]
            if n:
                buckets.append(f"{days}일 이상 {n}장")
        u0, gap, d0, d1 = max(hits, key=lambda h: h[1])
        out.append(("intl_stale", rep(us),
                    f"{nm(l)} 페이지 {len(by_lang[l])}장 중 {len(us)}장이 {nm(D)} 원문이 고쳐진 뒤 "
                    f"그대로입니다 — {' · '.join(buckets)} (예: {_path(u0)} — 원문 {d0.isoformat()}, "
                    f"이 판 {d1.isoformat()})"))

    # 4) inLanguage — 글(CreativeWork 갈래) 마크업이 있는데 어느 언어인지 안 적었다. 사이트 한 줄.
    marked = [u for u in lang if by_url[u].get("ld_inlang") is not None]
    no = [u for u in marked if by_url[u]["ld_inlang"] is False]
    if no and len(no) * 2 >= len(marked):
        out.append(("intl_no_inlanguage", rep(no),
                    f"글 마크업(WebPage·Article 갈래)이 있는 {len(marked)}장 중 {len(no)}장에 "
                    f"inLanguage 가 없습니다 (예: {_paths(sorted(no, key=lambda u: (len(u), u)))})"))

    # 5) 법적 고지 — 그 언어 판이 있는데 그 로케일이 요구하는 페이지가 안 보인다
    links = ctx.get("links") or ()
    for l in sorted(by_lang):
        rule = LEGAL_PAGES.get(prim(l))
        if not rule or (rule[2] and ctx.get("site_type") not in rule[2]):
            continue
        label, words, _only, _src = rule
        mine = set(by_lang[l])
        texts = [(u, by_url[u].get("title") or "") for u in mine]
        texts += [(x["url_to"], x.get("anchor") or "") for x in links if x["url_from"] in mine]
        if not any(_word_hit(words, urlsplit(u).path, t) for u, t in texts):
            out.append(("intl_legal_missing", rep(mine),
                        f"{nm(l)} 판 {len(mine)}장 어디에도 「{label}」 페이지나 그리로 가는 링크가 "
                        "없습니다. 그 로케일에서 사업자 사이트에 요구되는 고지입니다"))

    # 6) 날짜 표기 — 일/월/년을 쓰는 언어 판에 월/일/년 숫자 날짜가 남았다
    for l in sorted(by_lang):
        if prim(l) not in DMY_LANGS or prim(l) == prim(D):     # 기본 판의 표기는 원문 그 자체다
            continue
        hits = [(u, by_url[u]["num_dates"]) for u in by_lang[l]
                if (by_url[u].get("num_dates") or (0, 0, None))[0] >= FORMAT_MIN]
        if hits:
            u0, (n, _d, ex) = min(hits, key=lambda t: (len(t[0]), t[0]))
            out.append(("intl_format", rep([u for u, _ in hits]),
                        f"{nm(l)} 페이지 {len(by_lang[l])}장 중 {len(hits)}장에 월/일/년 숫자 날짜가 "
                        f"있습니다 (예: {_path(u0)} 「{ex}」 외 {n - 1}개). 이 언어는 일·월·년 순서로 씁니다"))
    return out


# 글(CreativeWork 갈래) 타입 — inLanguage 는 이 갈래의 속성이다(Organization·LocalBusiness 에는 없다).
_WORK_TYPES = frozenset({"WebPage", "WebSite", "Article", "BlogPosting", "NewsArticle",
                         "TechArticle", "CreativeWork", "FAQPage", "AboutPage", "ContactPage",
                         "CollectionPage", "ItemPage", "ProfilePage", "QAPage", "HowTo", "Recipe",
                         "Course", "Book", "VideoObject", "MedicalWebPage", "Report"})


def ld_inlanguage(blocks) -> bool | None:
    """ld+json 에 글 타입이 있으면 그중 inLanguage 를 가진 것이 있나. 글 타입이 없으면 None."""
    works = has = 0
    stack = [blocks]
    depth = 0
    while stack and depth < 5000:
        depth += 1
        x = stack.pop()
        if isinstance(x, list):
            stack.extend(x)
        elif isinstance(x, dict):
            t = x.get("@type")
            types = [t] if isinstance(t, str) else [s for s in (t or []) if isinstance(s, str)]
            if any(s in _WORK_TYPES or s.endswith(("Page", "Article", "Posting")) for s in types):
                works += 1
                has += int(bool(x.get("inLanguage")))
            stack.extend(v for v in x.values() if isinstance(v, (dict, list)))
    return None if not works else has > 0


# RSL(Really Simple Licensing) — 사이트는 robots.txt 의 'License: <절대 주소>' 줄로 라이선스
# 파일을 알린다(정해진 파일 이름은 없다 — license.xml 은 예시다). https://rslstandard.org/rsl
_RSL_LINE = re.compile(r"(?im)^\s*License\s*:\s*(\S+)\s*$")
# Content-Signal(search·ai-input·ai-train) 같은 다른 줄은 여기서 보지 않는다(AI 봇 판정 쪽 일이다).


def rsl_license(robots_txt: str) -> str | None:
    """robots.txt 의 License: 줄이 가리키는 주소 — 없으면 None."""
    m = _RSL_LINE.search(robots_txt or "")
    return m.group(1) if m else None
