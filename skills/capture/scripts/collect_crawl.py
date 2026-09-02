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
import sys
import time
import xml.etree.ElementTree as ET
from collections import deque
from pathlib import Path
from urllib import robotparser
from urllib.parse import urljoin, urlsplit, urlunsplit

sys.path.insert(0, str(Path(__file__).parent))

import collect_page  # noqa: E402
import collector  # noqa: E402
import db  # noqa: E402
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

SEVERITY = {
    "http_error": "bad", "broken_internal": "bad", "redirect_chain": "warn",
    "orphan": "warn", "dup_title": "warn", "dup_description": "warn",
    "missing_title": "bad", "missing_description": "warn", "missing_h1": "warn",
    "thin_content": "warn", "noindex": "warn", "canonical_mismatch": "warn",
    "img_no_alt": "info",
}


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
class _CrawlPage(collect_page._Page):
    """collect_page._Page + 링크(목적지·앵커·nofollow). 나머지는 부모 것 그대로."""

    def __init__(self, base: str):
        super().__init__(base)
        self.links: list[tuple[str, str, bool]] = []
        self._a: list | None = None

    def handle_starttag(self, tag, attrs):
        super().handle_starttag(tag, attrs)
        if tag != "a":
            return
        self._close_a()                       # <a/> 처럼 안 닫힌 것이 다음 앵커로 새지 않게
        a = {k.lower(): (v or "") for k, v in attrs}
        href = a.get("href", "").strip()
        if not href or href.startswith("#"):
            return
        to = normalize(href, self.base)
        if to:
            self._a = [to, [], "nofollow" in a.get("rel", "").lower()]

    def handle_data(self, data):
        super().handle_data(data)
        if self._a:
            self._a[1].append(data)

    def handle_endtag(self, tag):
        super().handle_endtag(tag)
        if tag == "a":
            self._close_a()

    def _close_a(self) -> None:
        if not self._a:
            return
        to, buf, nofollow = self._a
        self.links.append((to, " ".join("".join(buf).split())[:ANCHOR_MAX], nofollow))
        self._a = None


def parse_page(url: str, html: str) -> dict:
    """HTML 한 장 → crawl_pages 컬럼 + links. 네트워크를 타지 않는다."""
    p = _CrawlPage(url)
    try:
        p.feed(html[: collect_page.MAX_HTML])
    except Exception as e:                    # 망가진 마크업에도 여기까지 읽은 것은 남긴다
        print(f"  ! {url}: 파싱 도중 중단 ({e})", file=sys.stderr)
    p._close_a()
    return {"title": p.title, "description": p.meta_description,
            "h1": p.h1[0] if p.h1 else None,
            "canonical": p.canonical, "robots": p.robots, "words": p.words,
            "schema_types": ",".join(sorted(set(p.schema))) or None,
            "images_no_alt": p.images_no_alt, "links": p.links}


# ── 가져오기 ────────────────────────────────────────────────────────────────
def fetch(url: str) -> dict:
    """URL 한 장. 리다이렉트는 따라가되 홉을 그대로 돌려준다.

    self-check 는 이 함수를 통째로 갈아끼워 가상 사이트를 만든다 — 그래서
    반환 모양(키 이름)이 계약이다.
    """
    import requests
    try:
        r = requests.get(url, headers=UA, timeout=serp_adapter.TIMEOUTS["page"],
                         allow_redirects=True)
    except Exception as e:
        return {"final_url": url, "status": None, "chain": [], "text": "",
                "bytes": 0, "content_type": "", "error": f"{type(e).__name__}: {e}"[:200]}
    return {"final_url": r.url, "status": r.status_code,
            "chain": [(h.url, h.status_code) for h in r.history],
            "text": r.text, "bytes": len(r.content),
            "content_type": (r.headers.get("content-type") or ""), "error": None}


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


def discover_seeds(home: str) -> tuple[list[str], str, robotparser.RobotFileParser]:
    """(시드 URL, seed 종류, robots 판정기).

    robots.txt 의 Sitemap: 줄이 정본이다 — 직접 파싱하지 않고 robotparser 가
    읽은 것을 쓴다. 사이트맵이 없거나 비면 홈에서 BFS 로 시작한다.
    """
    rp = robotparser.RobotFileParser()
    rp.set_url(urljoin(home, "/robots.txt"))
    r = fetch(urljoin(home, "/robots.txt"))
    # 못 읽으면 전부 허용 — robots.txt 가 없는 사이트가 흔하다. parse() 가
    # last_checked 를 세우므로 빈 목록이어도 can_fetch 가 True 를 돌려준다.
    rp.parse(r["text"].splitlines() if r.get("status") == 200 and r.get("text") else [])

    seen, urls = set(), []
    for sm in (rp.site_maps() or []):
        for raw in _sitemap_urls(sm):
            u = normalize(raw)
            if u and u not in seen:
                seen.add(u)
                urls.append(u)
            if len(urls) >= MAX_SITEMAP_URLS:
                return urls, "sitemap", rp
    if urls:
        return urls, "sitemap", rp
    return [normalize(home)], "home", rp


# ── 크롤 ────────────────────────────────────────────────────────────────────
def crawl(seeds, home: str, *, limit: int, max_depth: int,
          rp=None, throttle: float = 0.0) -> tuple[list[dict], list[dict], list[list[str]]]:
    """BFS. (crawl_pages 행, crawl_links 행, 리다이렉트 사슬) 을 돌려준다.

    같은 호스트만 따라간다 — 외부 링크는 crawl_links 에 is_internal=0 으로
    기록만 하고 **가져오지 않는다**. robots.txt 의 Disallow 도 여기서 존중한다.
    """
    host = scoring.host_of(home)
    q = deque((u, 0) for u in seeds if u)
    seen = {u for u in seeds if u}
    pages: list[dict] = []
    links: list[dict] = []
    chains: list[list[str]] = []
    parsed: set[str] = set()
    fetched = 0

    while q and fetched < limit:
        url, depth = q.popleft()
        if rp is not None and not rp.can_fetch(UA_NAME, url):
            continue
        r = fetch(url)
        fetched += 1
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
            continue        # 이미 본 곳으로 리다이렉트됐다 — 홉만 남기고 두 번 세지 않는다
        parsed.add(final)

        row = _row(final, depth, status=r.get("status"), nbytes=r.get("bytes") or 0)
        ctype = (r.get("content_type") or "").lower()
        if r.get("status") == 200 and (not ctype or "html" in ctype):
            got = parse_page(final, r.get("text") or "")
            out = got.pop("links")
            row.update(got)
            row["links_out"] = len(out)
            for to, anchor, nofollow in out:
                internal = scoring.owns(scoring.host_of(to), host)
                links.append({"url_from": final, "url_to": to, "anchor": anchor,
                              "is_internal": int(internal), "nofollow": int(nofollow)})
                if internal and to not in seen and depth + 1 <= max_depth:
                    seen.add(to)
                    q.append((to, depth + 1))
        pages.append(row)
        if throttle:
            time.sleep(throttle)
    return pages, links, chains


def _row(url: str, depth: int, *, status=None, redirect_to=None, nbytes=0) -> dict:
    return {"url": url, "status": status, "redirect_to": redirect_to, "depth": depth,
            "title": None, "description": None, "h1": None, "canonical": None,
            "robots": None, "words": None, "schema_types": None,
            "links_in": 0, "links_out": 0, "images_no_alt": 0, "bytes": nbytes}


_PAGE_COLS = ("url", "status", "redirect_to", "depth", "title", "description", "h1",
              "canonical", "robots", "words", "schema_types", "links_in", "links_out",
              "images_no_alt", "bytes")


def save(conn, run_id: int, pages, links) -> None:
    """페이지·링크 적재 후 links_in 을 SQL 로 집계한다 — 크롤 중에는 모른다."""
    conn.executemany(
        f"INSERT OR REPLACE INTO crawl_pages(run_id,{','.join(_PAGE_COLS)}) "
        f"VALUES({','.join(['?'] * (len(_PAGE_COLS) + 1))})",
        [(run_id, *[p.get(c) for c in _PAGE_COLS]) for p in pages])
    conn.executemany(
        "INSERT INTO crawl_links(run_id,url_from,url_to,anchor,is_internal,nofollow) "
        "VALUES(?,?,?,?,?,?)",
        [(run_id, x["url_from"], x["url_to"], x["anchor"], x["is_internal"], x["nofollow"])
         for x in links])
    conn.execute(
        "UPDATE crawl_pages SET links_in = (SELECT COUNT(*) FROM crawl_links l "
        " WHERE l.run_id = crawl_pages.run_id AND l.is_internal = 1"
        "   AND l.url_to = crawl_pages.url AND l.url_from <> crawl_pages.url) "
        "WHERE run_id = ?", (run_id,))


# ── 이슈 도출 ───────────────────────────────────────────────────────────────
# (kind, SQL) — SQL 은 (url, detail) 두 칸을 돌려준다. severity 는 SEVERITY 표.
_ISSUE_SQL = (
    ("http_error",
     "SELECT url, 'HTTP ' || COALESCE(status, '응답 없음') FROM crawl_pages"
     " WHERE run_id=:run AND (status IS NULL OR status >= 400)"),
    ("broken_internal",
     "SELECT l.url_to, 'HTTP ' || COALESCE(p.status, '응답 없음') || ' · 이 곳을 가리키는 내부 링크 '"
     "       || COUNT(*) || '개 (예: ' || MIN(l.url_from) || ')'"
     " FROM crawl_links l JOIN crawl_pages p ON p.run_id = l.run_id AND p.url = l.url_to"
     " WHERE l.run_id=:run AND l.is_internal = 1 AND (p.status IS NULL OR p.status >= 400)"
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
    ("noindex",
     "SELECT url, 'meta robots: ' || robots FROM crawl_pages"
     " WHERE run_id=:run AND LOWER(COALESCE(robots, '')) LIKE '%noindex%'"),
    ("canonical_mismatch",
     "SELECT p.url, 'canonical → ' || p.canonical || ' (크롤한 URL 이 아닙니다)'"
     " FROM crawl_pages p WHERE p.run_id=:run AND p.canonical IS NOT NULL"
     "   AND TRIM(p.canonical) <> '' AND p.canonical <> p.url"
     "   AND NOT EXISTS (SELECT 1 FROM crawl_pages q"
     "                   WHERE q.run_id = p.run_id AND q.url = p.canonical)"),
    ("img_no_alt",
     "SELECT url, 'alt 없는 이미지 ' || images_no_alt || '개' FROM crawl_pages"
     " WHERE run_id=:run AND images_no_alt > 0"),
)


def derive_issues(conn, run_id: int, *, home: str, chains=()) -> int:
    """크롤 결과를 훑어 crawl_issues 에 저장한다. 저장이 핵심이다 — 회차 비교의 재료다.

    리다이렉트 사슬만 SQL 이 아니다: 홉 수는 응답 이력이라 크롤이 끝나면
    되살릴 수 없다. 나머지는 전부 적재된 표에서 뽑는다.
    """
    rows = []
    p = {"run": run_id, "home": home, "thin": THIN_WORDS}
    for kind, sql in _ISSUE_SQL:
        for url, detail in conn.execute(sql, p).fetchall():
            rows.append((run_id, kind, SEVERITY[kind], url, detail))
    for hops in chains:
        rows.append((run_id, "redirect_chain", SEVERITY["redirect_chain"], hops[0],
                     f"홉 {len(hops) - 1}개: " + " → ".join(hops)))
    conn.executemany(
        "INSERT INTO crawl_issues(run_id, kind, severity, url, detail) VALUES(?,?,?,?,?)",
        rows)
    return len(rows)


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
        limit: 가져올 URL 상한 (설정 키 crawl_urls). 0이면 끔
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
            return st.skip("사이트 주소(domain)가 없습니다 — 프로젝트 yaml 의 domain 을 채우세요.")

        print(f"[crawl] {home} · 최대 {limit}개 · 깊이 {depth} · 비용 없음 "
              f"(간격 {st.throttle}초)")
        if st.dry_run:
            print("  시드: robots.txt 의 Sitemap: 줄 → 사이트맵 URL, "
                  "없으면 홈에서 내부 링크 BFS")
            print(f"  같은 호스트만 따라갑니다 (외부 링크는 기록만) · 최대 {limit}회 조회 예정")
            return st.noop(rows=0)

        seeds, seed, rp = discover_seeds(home)
        print(f"  시드 {len(seeds)}개 ({seed})")

        run_id = conn.execute(
            "INSERT INTO crawl_runs(project_id, seed, started_at) VALUES(?,?,?)",
            (p["id"], seed, db.now())).lastrowid
        with st.record("crawl") as r:
            pages, links, chains = crawl(seeds, home, limit=limit, max_depth=depth,
                                         rp=rp, throttle=st.throttle)
            save(conn, run_id, pages, links)
            n_issues = derive_issues(conn, run_id, home=home, chains=chains)
            fetched = sum(1 for x in pages if x["redirect_to"] is None)
            conn.execute("UPDATE crawl_runs SET finished_at=?, pages=?, issues=? WHERE id=?",
                         (db.now(), fetched, n_issues, run_id))
            conn.commit()
            r.api_calls = fetched
            r.notes = f"seed={seed} pages={fetched} links={len(links)} issues={n_issues}"

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
                          help="가져올 URL 상한. 0이면 끔")
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
        "https://site.kr/sitemap.xml": (200, _SITEMAP, "application/xml"),
        "https://site.kr/": (200, _html("홈", tail=home_links), "text/html"),
        "https://site.kr/a": (200, _html("같은 제목", h1="에이",
                                         head='<link rel="canonical" href="/a">',
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
        assert ("redirect_chain", "https://site.kr/old") in issues, sorted(issues)
        assert "홉 2개" in issues[("redirect_chain", "https://site.kr/old")]["detail"]
        # /a 의 canonical 은 자기 자신이다 — 이건 이슈가 아니다
        assert ("canonical_mismatch", "https://site.kr/a") not in issues
        assert kinds <= set(SEVERITY), kinds
        assert all(i["severity"] in ("bad", "warn", "info") for i in issues.values())
        assert run1["issues"] == len(issues) and run1["finished_at"], dict(run1)

        # 8. 첫 회차는 기준선 — 비교 대상이 없다
        assert compare(conn, pid) == {"new": [], "fixed": [], "prev_run_id": None}

        # ── 2회차: /dead 가 살아나고, /orphan 에 noindex 가 붙는다 ────
        site2 = _site()
        site2["https://site.kr/dead"] = (200, _html("살아남"), "text/html")
        site2["https://site.kr/orphan"] = (
            200, _html("고아", h1="고아", head='<meta name="robots" content="noindex">'),
            "text/html")
        r, log = run(site=site2)
        run2 = conn.execute("SELECT id FROM crawl_runs ORDER BY id DESC LIMIT 1").fetchone()["id"]
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
        assert log[1] == "https://site.kr/", log        # robots.txt 다음이 홈
        assert "https://site.kr/orphan" not in log, log  # 링크가 없으니 못 닿는다

        # 깊이 상한 — 0이면 시드만 본다
        r, log = run(site=_site(sitemap=False), max_depth=0)
        assert [u for u in log if u.endswith("/a")] == [], log
    finally:
        globals()["fetch"] = orig_fetch
        conn.close()

    # 파서: 앵커·nofollow·외부/내부 판정 재료
    p = parse_page("https://s.kr/x", '<html><head><title>T</title></head><body>'
                   '<a href="/y" rel="nofollow">와이</a><a href="#top">건너뜀</a>'
                   '<a href="https://o.com/z">지</a></body></html>')
    assert p["links"] == [("https://s.kr/y", "와이", True), ("https://o.com/z", "지", False)], p["links"]
    print("collect_crawl self-check ok")


if __name__ == "__main__":
    main()
