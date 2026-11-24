#!/usr/bin/env python3
"""Bing Webmaster — 색인·크롤 상태, 검색어 성과, 인바운드 링크 (무료, API 키).

Copilot 은 Bing 검색 서비스로 질의를 보낸다. 구글만 재면 그쪽 절반이 안 보인다.
단계가 아니라 **백링크 단계의 하위 걸음**이다(collect_backlinks.collect 가 부른다) —
단계표·묶음·화면 목록을 늘리지 않는다. 키가 없으면 통째로 건너뛰고 사유를 돌려준다.

API (전부 무료, GET, JSON):
  https://ssl.bing.com/webmaster/api.svc/json/METHOD?apikey=KEY&siteUrl=…
  응답은 {"d": …} 로 감싸 오고, 오류는 400 + {"ErrorCode": n, "Message": "…"} 다.
  옛 SOAP·POX 는 2026-08-31 에 닫혔다. 이 JSON 주소와 API 키는 그대로 지원된다:
    "You are not impacted if your integration already uses
     https://ssl.bing.com/webmaster/api.svc/json/..."
    https://www.bing.com/webmasters/help/soap-pox-api-retirement-s0appox01
    https://learn.microsoft.com/en-us/bingwebmaster/getting-access (키 발급)
  메서드·필드 (learn.microsoft.com/en-us/dotnet/api/microsoft.bing.webmaster.api.interfaces.iwebmasterapi.*):
    GetUserSites                 → [{Url, IsVerified, …}]
    GetCrawlStats(siteUrl)       → [{Date, CrawledPages, InIndex, InLinks, CrawlErrors, Code2xx,
                                     Code301, Code302, Code4xx, Code5xx, BlockedByRobotsTxt, …}]
    GetCrawlIssues(siteUrl)      → [{Url, HttpCode, Issues, InLinks}]
    GetQueryStats(siteUrl)       → [{Query, Date, Impressions, Clicks, AvgImpressionPosition, …}]
    GetLinkCounts(siteUrl, page) → {Links: [{Url, Count}], TotalPages}
    GetUrlLinks(siteUrl, link, page) → {Details: [{Url, AnchorText}], TotalPages}
  날짜는 "/Date(1316156400000-0700)/" 꼴이다(밀리초). 읽기 메서드의 호출 한도는 문서에 숫자가
  없다 — 그래서 링크 상세는 상한(LINK_*)을 두고, 다 못 읽었으면 그렇다고 적는다.

키는 **사용자 단위**다(한 사람에 하나, 그 사람이 인증한 사이트 전부). 그래서 서버의 키로 남의
사이트를 읽을 수 없다 — 등록·인증 안 된 사이트는 사유를 적고 건너뛴다.

Usage:
  python collect_bing.py            # self-check (네트워크 안 탐)
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).parent))

API = "https://ssl.bing.com/webmaster/api.svc/json"
KEY_ENV = "BING_WEBMASTER_API_KEY"
KEY_URL = "https://www.bing.com/webmasters"
TIMEOUT = 30
LINK_COUNT_PAGES = 5        # GetLinkCounts 를 몇 쪽까지 읽나 (우리 페이지 목록)
LINK_TARGETS = 20           # 링크 상세(GetUrlLinks)를 볼 우리 페이지 수 — 링크 많은 순
LINK_DETAIL_PAGES = 2       # 페이지당 상세 몇 쪽
QUERY_TOP = 500             # 검색어는 노출 많은 순으로 여기까지만 적는다

_DATE = re.compile(r"/Date\((-?\d+)(?:[+-]\d{4})?\)/")


def has_key() -> bool:
    return bool(os.environ.get(KEY_ENV))


class BingError(RuntimeError):
    pass


def call(method: str, params: dict | None = None):
    """Bing Webmaster JSON 메서드 한 번 → "d" 안의 값. 오류는 BingError(사람이 읽을 문장)."""
    import requests
    r = requests.get(f"{API}/{method}", params={"apikey": os.environ.get(KEY_ENV, ""), **(params or {})},
                     headers={"Content-Type": "application/json; charset=utf-8"}, timeout=TIMEOUT)
    try:
        body = r.json()
    except ValueError:
        raise BingError(f"Bing {method}: 응답 {r.status_code}, JSON 이 아닙니다") from None
    if r.status_code >= 400 or (isinstance(body, dict) and "ErrorCode" in body):
        msg = (body or {}).get("Message") if isinstance(body, dict) else None
        raise BingError(f"Bing {method}: 응답 {r.status_code} {msg or ''}".strip())
    return body.get("d") if isinstance(body, dict) else body


def day(v) -> str | None:
    """"/Date(1316156400000-0700)/" → 'YYYY-MM-DD'(UTC). 못 읽으면 None."""
    m = _DATE.search(str(v or ""))
    if not m:
        return None
    try:
        return datetime.fromtimestamp(int(m.group(1)) / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    except (OverflowError, OSError, ValueError):
        return None


def _int(v) -> int | None:
    try:
        return None if v is None or isinstance(v, bool) else int(v)
    except (TypeError, ValueError):
        return None


def _rows(v) -> list[dict]:
    return [x for x in (v or []) if isinstance(x, dict)] if isinstance(v, list) else []


def pick_site(sites, domain: str) -> str | None:
    """인증된 사이트 중 이 도메인의 것 — 정확히 같은 호스트가 먼저, 없으면 www 만 다른 것."""
    import scoring
    want = scoring.host_of(domain)
    ok = [str(s.get("Url") or "") for s in _rows(sites) if s.get("IsVerified")]
    same = [u for u in ok if scoring.host_of(u) == want]
    # https 가 먼저 — 같은 호스트를 두 스킴으로 다 등록한 경우
    same.sort(key=lambda u: (not u.lower().startswith("https://"), len(u)))
    return same[0] if same else None


def query_rows(stats) -> list[dict]:
    """GetQueryStats(검색어×주) → 검색어별 합. 평균 순위는 노출 가중이다(노출 0 이면 None)."""
    agg: dict[str, dict] = {}
    for r in _rows(stats):
        q = str(r.get("Query") or "").strip()
        if not q:
            continue
        imp, clk = _int(r.get("Impressions")) or 0, _int(r.get("Clicks")) or 0
        pos, d = r.get("AvgImpressionPosition"), day(r.get("Date"))
        a = agg.setdefault(q, {"query": q, "impressions": 0, "clicks": 0, "_w": 0.0, "_n": 0,
                               "date_from": d, "date_to": d})
        a["impressions"] += imp
        a["clicks"] += clk
        try:
            # Bing 은 순위가 없을 때 -1 을 준다 — 평균에 넣지 않는다.
            if pos is not None and float(pos) > 0 and imp > 0:
                a["_w"] += float(pos) * imp
                a["_n"] += imp
        except (TypeError, ValueError):
            pass
        if d:
            a["date_from"] = min(a["date_from"] or d, d)
            a["date_to"] = max(a["date_to"] or d, d)
    out = []
    for a in agg.values():
        a["avg_position"] = round(a.pop("_w") / a["_n"], 1) if a["_n"] else None
        a.pop("_n")
        out.append(a)
    out.sort(key=lambda a: (-a["impressions"], -a["clicks"], a["query"]))
    return out[:QUERY_TOP]


def _put_crawl(conn, pid: int, stats) -> int:
    rows = []
    for r in _rows(stats):
        d = day(r.get("Date"))
        if d:
            rows.append((pid, d, *(_int(r.get(k)) for k in (
                "CrawledPages", "InIndex", "InLinks", "CrawlErrors", "Code2xx", "Code301",
                "Code302", "Code4xx", "Code5xx", "BlockedByRobotsTxt"))))
    conn.executemany(
        "INSERT INTO bing_crawl_stats(project_id, date, crawled_pages, in_index, in_links,"
        " crawl_errors, code_2xx, code_301, code_302, code_4xx, code_5xx, blocked_robots)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(project_id, date) DO UPDATE SET crawled_pages=excluded.crawled_pages,"
        " in_index=excluded.in_index, in_links=excluded.in_links, crawl_errors=excluded.crawl_errors,"
        " code_2xx=excluded.code_2xx, code_301=excluded.code_301, code_302=excluded.code_302,"
        " code_4xx=excluded.code_4xx, code_5xx=excluded.code_5xx,"
        " blocked_robots=excluded.blocked_robots", rows)
    return len(rows)


def _put_issues(conn, pid: int, today: str, issues) -> int:
    rows = [(pid, today, str(r["Url"]), _int(r.get("HttpCode")), _int(r.get("Issues")),
             _int(r.get("InLinks"))) for r in _rows(issues) if r.get("Url")]
    conn.execute("DELETE FROM bing_crawl_issues WHERE project_id=? AND checked_date=?", (pid, today))
    conn.executemany(
        "INSERT INTO bing_crawl_issues(project_id, checked_date, url, http_code, issues, in_links)"
        " VALUES (?,?,?,?,?,?) ON CONFLICT(project_id, checked_date, url) DO UPDATE SET"
        " http_code=excluded.http_code, issues=excluded.issues, in_links=excluded.in_links", rows)
    return len(rows)


def _put_queries(conn, pid: int, today: str, stats) -> int:
    rows = [(pid, today, a["query"], a["impressions"], a["clicks"], a["avg_position"],
             a["date_from"], a["date_to"]) for a in query_rows(stats)]
    conn.execute("DELETE FROM bing_query_stats WHERE project_id=? AND checked_date=?", (pid, today))
    conn.executemany(
        "INSERT INTO bing_query_stats(project_id, checked_date, query, impressions, clicks,"
        " avg_position, date_from, date_to) VALUES (?,?,?,?,?,?,?,?)", rows)
    return len(rows)


def inbound_links(site: str, call_fn) -> dict:
    """인바운드 링크 → {links: [(url_from, url_to, anchor)], total, complete, notes}.

    GetLinkCounts 가 우리 페이지별 링크 수를, GetUrlLinks 가 그 페이지로 오는 링크를 준다.
    둘 다 쪽 단위라 상한까지만 읽는다. total(링크 수 합)은 **목록을 끝까지 읽었을 때만** 숫자다 —
    일부만 읽은 합을 전체처럼 적으면 "백링크 340개"가 사실은 "읽은 데까지 340개"가 된다.
    """
    pages, total_pages, notes = [], None, []
    for page in range(LINK_COUNT_PAGES):
        got = call_fn("GetLinkCounts", {"siteUrl": site, "page": page}) or {}
        part = _rows(got.get("Links"))
        pages += [(str(r["Url"]), _int(r.get("Count")) or 0) for r in part if r.get("Url")]
        total_pages = _int(got.get("TotalPages"))
        if not part or (total_pages is not None and page + 1 >= total_pages):
            break
    counts_done = total_pages is not None and total_pages <= LINK_COUNT_PAGES
    pages.sort(key=lambda p: -p[1])
    links, details_done = [], True
    for url, _n in pages[:LINK_TARGETS]:
        for page in range(LINK_DETAIL_PAGES):
            # 문자열 인자는 JSON 문자열로 보낸다(문서의 표본이 url=%22…%22 꼴이다).
            got = call_fn("GetUrlLinks", {"siteUrl": site, "link": json.dumps(url), "page": page}) or {}
            part = _rows(got.get("Details"))
            links += [(str(r["Url"]), url, (r.get("AnchorText") or None)) for r in part if r.get("Url")]
            tp = _int(got.get("TotalPages"))
            if not part or (tp is not None and page + 1 >= tp):
                break
        else:
            details_done = False
    if len(pages) > LINK_TARGETS:
        details_done = False
    if not counts_done:
        notes.append(f"링크 받는 페이지 목록을 {LINK_COUNT_PAGES}쪽까지만 읽었습니다")
    if not details_done:
        notes.append(f"링크 상세는 링크 많은 페이지 {LINK_TARGETS}개, 페이지당 {LINK_DETAIL_PAGES}쪽까지입니다")
    return {"links": links, "pages": pages,
            "total": sum(n for _, n in pages) if counts_done else None,
            "complete": counts_done and details_done, "notes": notes}


def put_links(conn, pid: int, today: str, got: dict) -> dict:
    """Bing 인바운드 링크를 백링크 표들에 source='bing' 으로 적는다 → {표: 줄 수}.

    Bing 은 지수·dofollow·응답 코드를 안 준다 — 그 칸은 NULL(모름)이다. 0 으로 채우면
    "순위 반영 0개"·"지수 0"이 사실처럼 나간다. 참조 도메인 수는 **상세를 끝까지 읽었을 때만**
    요약에 적는다(got["complete"]). 아니면 NULL — 화면이 '데이터 부족'으로 말한다.
    """
    def host(u):
        try:
            return (urlparse(u).netloc or "").lower() or None
        except ValueError:
            return None
    seen, rows = set(), []
    for u_from, u_to, anchor in got["links"]:
        if (u_from, u_to) in seen:
            continue
        seen.add((u_from, u_to))
        rows.append((pid, today, u_from, u_to, host(u_from), anchor))
    conn.executemany(
        "INSERT INTO backlinks(project_id, checked_date, url_from, url_to, domain_from, anchor,"
        " is_broken, to_status, source) VALUES (?,?,?,?,?,?,0,0,'bing')"
        " ON CONFLICT(project_id, checked_date, url_from, url_to) DO UPDATE SET"
        " domain_from=excluded.domain_from, anchor=excluded.anchor, source=excluded.source", rows)
    doms: dict[str, int] = {}
    for r in rows:
        if r[4]:
            doms[r[4]] = doms.get(r[4], 0) + 1
    conn.executemany(
        "INSERT INTO referring_domains(project_id, checked_date, domain, backlinks, source)"
        " VALUES (?,?,?,?,'bing') ON CONFLICT(project_id, checked_date, domain) DO UPDATE SET"
        " backlinks=excluded.backlinks, source=excluded.source",
        [(pid, today, d, n) for d, n in doms.items()])
    anchors: dict[str, list] = {}
    for r in rows:
        a = (r[5] or "").strip()
        if a:
            e = anchors.setdefault(a, [0, set()])
            e[0] += 1
            e[1].add(r[4])
    conn.executemany(
        "INSERT INTO backlink_anchors(project_id, checked_date, anchor, backlinks, referring_domains)"
        " VALUES (?,?,?,?,?) ON CONFLICT(project_id, checked_date, anchor) DO UPDATE SET"
        " backlinks=excluded.backlinks, referring_domains=excluded.referring_domains",
        [(pid, today, a, n, len(ds)) for a, (n, ds) in anchors.items()])
    conn.executemany(
        "INSERT INTO backlink_pages(project_id, checked_date, url, backlinks) VALUES (?,?,?,?)"
        " ON CONFLICT(project_id, checked_date, url) DO UPDATE SET backlinks=excluded.backlinks",
        [(pid, today, u, n) for u, n in got["pages"]])
    conn.execute(
        "INSERT INTO backlink_summary(project_id, checked_date, backlinks, referring_domains, source)"
        " VALUES (?,?,?,?,'bing') ON CONFLICT(project_id, checked_date) DO UPDATE SET"
        " backlinks=excluded.backlinks, referring_domains=excluded.referring_domains,"
        " source=excluded.source",
        (pid, today, got["total"], len(doms) if got["complete"] else None))
    return {"backlinks": len(rows), "referring_domains": len(doms), "anchors": len(anchors),
            "pages": len(got["pages"])}


def collect_into(conn, pid: int, domain: str, today: str, *, links: bool = False,
                 call_fn=None) -> tuple[dict, list[str], int]:
    """Bing 하위 걸음 → ({표: 줄 수}, 사유·메모, 호출 수). 예외를 올리지 않는다 —
    백링크 단계의 곁가지라 여기서 죽으면 안 된다. 못 한 일은 메모에 사람 말로 남는다.

    links=True 는 DataForSEO 가 없을 때다 — 인바운드 링크를 백링크 표에 source='bing' 으로 적는다.
    """
    if not has_key() and call_fn is None:
        return {}, [f"Bing 웹마스터 생략: {KEY_ENV} 가 없습니다. 무료 키 발급: {KEY_URL} "
                    "(Settings → API Access)"], 0
    calls = 0
    raw = call_fn or call

    def c(method, params=None):
        nonlocal calls
        calls += 1
        return raw(method, params)

    rows: dict[str, int] = {}
    notes: list[str] = []
    try:
        site = pick_site(c("GetUserSites"), domain)
    except Exception as e:               # noqa: BLE001
        return {}, [f"Bing 웹마스터를 읽지 못했습니다: {e}"], calls
    if not site:
        return {}, [f"Bing 웹마스터 생략: 이 키의 계정에 {domain} 이 인증된 사이트로 없습니다. "
                    f"{KEY_URL} 에서 사이트를 추가·인증하면 다음 수집부터 읽습니다"], calls
    steps = [("bing_crawl", "GetCrawlStats", lambda v: _put_crawl(conn, pid, v)),
             ("bing_issues", "GetCrawlIssues", lambda v: _put_issues(conn, pid, today, v)),
             ("bing_queries", "GetQueryStats", lambda v: _put_queries(conn, pid, today, v))]
    for name, method, put in steps:
        try:
            rows[name] = put(c(method, {"siteUrl": site}))
        except Exception as e:           # noqa: BLE001 — 한 축이 죽어도 나머지는 간다
            notes.append(f"Bing {method} 실패: {e}")
    if links:
        try:
            got = inbound_links(site, c)
            for k, v in put_links(conn, pid, today, got).items():
                rows[f"bing_{k}"] = v
            notes += [f"Bing 인바운드 링크: {n}" for n in got["notes"]]
        except Exception as e:           # noqa: BLE001
            notes.append(f"Bing 인바운드 링크 실패: {e}")
    return rows, notes, calls


def latest(conn, pid: int) -> dict:
    """화면용 조회 — 수집한 적이 없으면 빈 값(없는 걸 지어내지 않는다)."""
    q = lambda sql, args: [dict(r) for r in conn.execute(sql, args).fetchall()]
    crawl = q("SELECT * FROM bing_crawl_stats WHERE project_id=? ORDER BY date DESC LIMIT 60", (pid,))[::-1]
    qd = conn.execute("SELECT MAX(checked_date) FROM bing_query_stats WHERE project_id=?", (pid,)).fetchone()[0]
    idate = conn.execute("SELECT MAX(checked_date) FROM bing_crawl_issues WHERE project_id=?", (pid,)).fetchone()[0]
    return {
        "crawl": crawl,
        "queries": q("SELECT query, impressions, clicks, avg_position, date_from, date_to"
                     " FROM bing_query_stats WHERE project_id=? AND checked_date=?"
                     " ORDER BY impressions DESC, clicks DESC, query LIMIT 100", (pid, qd)) if qd else [],
        "queries_date": qd,
        "issues": q("SELECT url, http_code, issues, in_links FROM bing_crawl_issues"
                    " WHERE project_id=? AND checked_date=? ORDER BY in_links DESC, url LIMIT 100",
                    (pid, idate)) if idate else [],
        "issues_date": idate,
    }


# ── 자기검사 ─────────────────────────────────────────────────────────────────

def fake_call(sites=None, *, fail=()):
    """문서의 응답 꼴 그대로인 가짜. 테스트(test_link_axis)도 이것을 쓴다."""
    ms = lambda y, m, d: int(datetime(y, m, d, tzinfo=timezone.utc).timestamp() * 1000)
    D = lambda y, m, d: f"/Date({ms(y, m, d)}-0700)/"
    log: list = []

    def call_fn(method, params=None):
        log.append((method, dict(params or {})))
        if method in fail:
            raise BingError(f"Bing {method}: 응답 400 InvalidApiKey")
        if method == "GetUserSites":
            return sites if sites is not None else [
                {"__type": "Site:#Microsoft.Bing.Webmaster.Api", "Url": "http://bt.com/", "IsVerified": True},
                {"Url": "https://bt.com/", "IsVerified": True},
                {"Url": "https://other.com/", "IsVerified": True},
                {"Url": "https://www.bt.com/", "IsVerified": False}]
        if method == "GetCrawlStats":
            return [{"Date": D(2026, 11, 1), "CrawledPages": 120, "InIndex": 95, "InLinks": 40,
                     "CrawlErrors": 3, "Code2xx": 110, "Code301": 4, "Code302": 1, "Code4xx": 5,
                     "Code5xx": 0, "BlockedByRobotsTxt": 2, "AllOtherCodes": 0},
                    {"Date": D(2026, 11, 2), "CrawledPages": 130, "InIndex": 97},
                    {"Date": "broken", "CrawledPages": 1}]
        if method == "GetCrawlIssues":
            return [{"Url": "https://bt.com/gone", "HttpCode": 404, "Issues": 32, "InLinks": 7}]
        if method == "GetQueryStats":
            return [{"Query": "bt 후기", "Date": D(2026, 10, 19), "Impressions": 100, "Clicks": 10,
                     "AvgImpressionPosition": 4, "AvgClickPosition": 3},
                    {"Query": "bt 후기", "Date": D(2026, 10, 26), "Impressions": 300, "Clicks": 5,
                     "AvgImpressionPosition": 8, "AvgClickPosition": -1},
                    {"Query": "노출만", "Date": D(2026, 10, 26), "Impressions": 0, "Clicks": 0,
                     "AvgImpressionPosition": -1}]
        if method == "GetLinkCounts":
            return {"Links": [{"Url": "https://bt.com/a", "Count": 3}, {"Url": "https://bt.com/", "Count": 9}],
                    "TotalPages": 1} if params["page"] == 0 else {"Links": [], "TotalPages": 1}
        if method == "GetUrlLinks":
            assert params["link"].startswith('"'), "link 는 JSON 문자열로 보낸다"
            if json.loads(params["link"]) == "https://bt.com/":
                return {"Details": [{"Url": "https://n.com/p", "AnchorText": "bt"},
                                    {"Url": "https://n.com/q", "AnchorText": "bt"},
                                    {"Url": "https://m.org/x", "AnchorText": ""}], "TotalPages": 1}
            return {"Details": [{"Url": "https://n.com/p", "AnchorText": "여기"}], "TotalPages": 1}
        raise AssertionError(f"모르는 메서드: {method}")
    call_fn.log = log
    return call_fn


def _selfcheck() -> None:
    import tempfile
    os.environ["CAPTURE_HOME"] = str(Path(tempfile.mkdtemp(prefix="seo-miner-bing-selftest-")))
    os.environ.pop(KEY_ENV, None)
    import db
    conn = db.connect()
    conn.execute("INSERT INTO projects(name, domain, locale) VALUES('bt','bt.com','en-US')")
    pid = db.get_project(conn, "bt")["id"]
    T = "2026-11-24"

    assert day("/Date(1316156400000-0700)/") == "2011-09-16" and day("\\/Date(0)\\/".replace("\\", "")) == "1970-01-01"
    assert day("x") is None and day(None) is None

    # 키가 없으면 부르지 않고 사유만 — 사유에 무엇을 하면 되는지가 있다.
    rows, notes, calls = collect_into(conn, pid, "bt.com", T)
    assert rows == {} and calls == 0 and KEY_ENV in notes[0] and KEY_URL in notes[0], notes

    # 인증 안 된 사이트·남의 사이트는 고르지 않는다. https 가 먼저다.
    f = fake_call()
    assert pick_site(f("GetUserSites"), "www.bt.com") == "https://bt.com/"
    assert pick_site([{"Url": "https://www.bt.com/", "IsVerified": False}], "bt.com") is None
    rows, notes, _ = collect_into(conn, pid, "bt.com", T, call_fn=fake_call(sites=[]))
    assert rows == {} and "인증된 사이트로 없습니다" in notes[0], notes

    # 색인·크롤·검색어 — DataForSEO 가 있을 때의 길(links=False): 백링크 표는 안 건드린다.
    f = fake_call()
    rows, notes, calls = collect_into(conn, pid, "bt.com", T, call_fn=f)
    assert rows == {"bing_crawl": 2, "bing_issues": 1, "bing_queries": 2} and not notes, (rows, notes)
    assert calls == 4 and not any(m.startswith("GetLink") or m == "GetUrlLinks" for m, _ in f.log)
    assert conn.execute("SELECT COUNT(*) FROM backlinks").fetchone()[0] == 0
    got = latest(conn, pid)
    assert [r["date"] for r in got["crawl"]] == ["2026-11-01", "2026-11-02"], got["crawl"]
    assert got["crawl"][0]["in_index"] == 95 and got["crawl"][1]["crawl_errors"] is None
    q = {r["query"]: r for r in got["queries"]}
    # 노출 가중 평균: (4*100 + 8*300) / 400 = 7.0. 노출 0 이면 순위는 없다(0 위가 아니다).
    assert (q["bt 후기"]["impressions"], q["bt 후기"]["clicks"], q["bt 후기"]["avg_position"]) == (400, 15, 7.0)
    assert q["노출만"]["avg_position"] is None
    assert (q["bt 후기"]["date_from"], q["bt 후기"]["date_to"]) == ("2026-10-19", "2026-10-26")
    assert got["issues"][0]["http_code"] == 404

    # 한 메서드가 죽어도 나머지는 적힌다.
    rows, notes, _ = collect_into(conn, pid, "bt.com", T, call_fn=fake_call(fail=("GetCrawlIssues",)))
    assert "bing_crawl" in rows and "bing_queries" in rows and "GetCrawlIssues 실패" in notes[0], (rows, notes)

    # 인바운드 링크(폴백) — source='bing', 모르는 칸은 NULL, 같은 링크는 한 번.
    rows, notes, _ = collect_into(conn, pid, "bt.com", T, links=True, call_fn=fake_call())
    assert rows["bing_backlinks"] == 4 and rows["bing_referring_domains"] == 2, rows
    bl = [dict(r) for r in conn.execute("SELECT * FROM backlinks ORDER BY url_from, url_to")]
    assert {r["source"] for r in bl} == {"bing"} and {r["dofollow"] for r in bl} == {None}
    assert {r["rank"] for r in bl} == {None} and {r["is_broken"] for r in bl} == {0}
    s = dict(conn.execute("SELECT * FROM backlink_summary").fetchone())
    assert (s["source"], s["backlinks"], s["referring_domains"], s["rank"]) == ("bing", 12, 2, None), s
    an = {r["anchor"]: r["backlinks"] for r in conn.execute("SELECT * FROM backlink_anchors")}
    assert an == {"bt": 2, "여기": 1}, an              # 빈 앵커는 앵커 표에 안 넣는다

    # 목록을 끝까지 못 읽었으면 합계는 숫자가 아니다(NULL) — 읽은 데까지의 합을 전체처럼 적지 않는다.
    base = fake_call()

    def capped(method, params=None):
        got = base(method, params)
        if method == "GetLinkCounts":
            return {**got, "TotalPages": 99, "Links": [{"Url": "https://bt.com/a", "Count": 3}]}
        return got
    conn.execute("DELETE FROM backlink_summary")
    rows, notes, _ = collect_into(conn, pid, "bt.com", T, links=True, call_fn=capped)
    s = dict(conn.execute("SELECT * FROM backlink_summary").fetchone())
    assert s["backlinks"] is None and s["referring_domains"] is None, s
    assert any("쪽까지만" in n for n in notes), notes
    conn.close()
    print("collect_bing self-check ok")


if __name__ == "__main__":
    _selfcheck()
