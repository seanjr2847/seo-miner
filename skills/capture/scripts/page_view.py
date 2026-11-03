#!/usr/bin/env python3
"""[페이지별] 화면의 본체 — 글 하나(와 그 언어판)에 걸린 것을 한자리에 모은다.

다른 화면은 사이트 전체를 주제(클릭 변화·순위·AI 인용·사이트 점검)로 쪼갠다. 이 화면은
축이 거꾸로다: 글 하나를 고르면 그 글의 언어판 주소들(/kr/ko/x · /us/en/x …)이 함께 서고,
언어마다 노출·클릭·CTR·평균 순위·글 수정일·날짜별 추이, 그 주소로 들어온 검색어, 그
주소를 인용한 AI 답이 나온다. 숫자는 새로 짓지 않는다 — 이미 모은 표를 읽기만 한다.

주소 하나 = 열쇠(norm) 하나. 서치콘솔은 같은 페이지를 http·https, www 유무, 끝 '/', 광고 꼬리표
(?utm_…)로 갈라 보고한다 — 갈린 채로 두면 한 글이 언어판 일곱 개로 서고 숫자가 흩어진다. 그래서
열쇠로 접고, 숫자는 갈린 주소를 다 더한다. 화면에 보이는 주소는 그중 https·꼬리표 없는 것.

언어판 묶기(groups):
  1. 페이지 점검(page_audits)이 읽은 hreflang — 글이 스스로 말한 언어판. 가장 정확하다.
  2. 시장 경로 — 사이트의 시장(db.site_markets)이 서로 다른 경로(/kr/ko/ · /us/en/)를 가지면,
     그 경로를 뗀 나머지가 같은 주소를 같은 글로 본다. 경로 없이 도메인 전체를 맡은 시장이
     있으면(theotherskin 의 ko-KR) 어느 경로에도 안 드는 주소가 그 시장 것이다. 점검은 고칠
     대상 위주로 일부 주소만 보기 때문에, hreflang 을 모르는 주소는 이 규칙으로 묶는다.
  어느 쪽도 아니면 주소 하나가 글 하나다.

언어 — 경로가 맞는 시장의 로케일. 경로가 안 맞으면 hreflang 이 그 주소에 붙인 코드(사이트 설정에
없는 /ja/ · /zh-hans/), 그것도 없으면 도메인 전체 시장의 로케일.

본체 둘 — 두 배포가 같은 함수를 부른다(dashboard.ROUTES 의 /api/pages · /api/page):
  page_index(conn, pid)       글 목록(고르는 칸) — 클릭 순
  page_detail(conn, pid, url) 그 주소가 든 글 하나의 전부
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import parse_qsl, urlencode

sys.path.insert(0, str(Path(__file__).parent))
import brief  # noqa: E402  (언어 이름 한 벌 — lang_label)
import db  # noqa: E402
import scoring  # noqa: E402
import serp_adapter  # noqa: E402

# 언어판 표·목록의 합계 창 — [클릭 변화]·스냅샷과 같은 28일.
WINDOW_DAYS = 28
# 추이 그래프가 싣는 날 수 — 처음 수집이 16개월을 되받아도 화면은 최근 이만큼만 그린다.
TREND_DAYS = 180
# 글 목록 상한 — 고르는 칸이다. 더 아래는 찾기로 닿는다(서버가 상한까지 싣는다).
INDEX_MAX = 1000
QUERIES_MAX = 200
# 주소를 접을 때 버리는 꼬리표 — 같은 페이지를 광고·공유 경로별로 가른 것일 뿐이다.
# 화면(views/page.html 의 PG_norm)이 같은 규칙을 쓴다 — test_page_view 가 둘을 대조한다.
TRACKING = ("utm_", "gclid", "fbclid", "msclkid", "srsltid", "_gl", "yclid", "mc_")


def norm(url: str) -> str:
    """주소의 열쇠 — 스킴 무시·호스트 소문자·www 무시·조각(#) 제거·끝 '/' 무시·꼬리표 제거.
    화면에 보이는 값은 원래 주소 그대로 둔다(이 꼴은 묶고 찾는 데만 쓴다)."""
    u = str(url or "").strip().split("#")[0]
    u, _, qs = u.partition("?")
    keep = [(k, v) for k, v in parse_qsl(qs, keep_blank_values=True)
            if not k.lower().startswith(TRACKING)]
    tail = ("?" + urlencode(keep)) if keep else ""
    if "://" not in u:
        return (u.rstrip("/") or "/") + tail
    rest = u.split("://", 1)[1]
    host, _, path = rest.partition("/")
    host = host.lower()
    host = host[4:] if host.startswith("www.") else host
    path = "/" + path
    if len(path) > 1:
        path = path.rstrip("/") or "/"
    return f"{host}{path}{tail}"


def _display(raws: list[str]) -> str:
    """갈린 주소들 중 화면에 보일 하나 — https · 꼬리표 없는 것 · 짧은 것 순."""
    return sorted(raws, key=lambda u: (not u.lower().startswith("https://"), "?" in u, len(u), u))[0]


def market_of(url: str, markets: list[tuple[str, str]]) -> tuple[str | None, str]:
    """(경로가 맞는 시장의 로케일 | None, 시장 경로를 뗀 나머지). 범위가 가장 긴(가장 구체적인)
    시장을 고른다. 맞는 시장이 없으면 (None, 경로 그대로) — 언어는 부르는 쪽이 정한다(머리말)."""
    path = scoring.path_of(url)
    best = None
    for loc, sc in markets:
        s = scoring.scope_of(sc)
        if s and scoring.in_scope(url, s) and (best is None or len(s) > len(best[1])):
            best = (loc, s)
    if best is None:
        return None, path
    return best[0], path[len(best[1].rstrip("/")):] or "/"


def _hreflang(a) -> list[tuple[str, str]]:
    try:
        return [(str(c), str(u)) for c, u in json.loads(a["hreflang_json"] or "[]")]
    except (TypeError, ValueError, KeyError, IndexError):
        return []


class _Site:
    """한 사이트의 주소 장부 — 열쇠마다 갈린 주소들·보일 주소·점검·언어, 그리고 언어판 묶음."""

    def __init__(self, conn: sqlite3.Connection, pid: int):
        self.conn, self.pid = conn, pid
        self.own = conn.execute("SELECT domain FROM projects WHERE id=?", (pid,)).fetchone()["domain"]
        self.markets = db.site_markets(conn, pid)
        self.raw: dict[str, list[str]] = {}
        snap = conn.execute("SELECT MAX(snapshot_date) FROM gsc_snapshots WHERE project_id=?",
                            (pid,)).fetchone()[0]
        rows = [r[0] for r in conn.execute(
            "SELECT DISTINCT page FROM gsc_page_daily WHERE project_id=? AND date >= ?",
            (pid, str(date.today() - timedelta(days=TREND_DAYS))))]
        if snap:
            rows += [r[0] for r in conn.execute(
                "SELECT DISTINCT page FROM gsc_snapshots WHERE project_id=? AND snapshot_date=?"
                " AND page IS NOT NULL", (pid, snap))]
        self.audits: dict[str, sqlite3.Row] = {}
        self.code: dict[str, str] = {}             # 열쇠 → hreflang 이 붙인 언어 코드
        alts: list[tuple[str, str]] = []
        for a in db.latest_page_audits(conn, pid):
            k = norm(a["url"])
            self.audits.setdefault(k, a)
            rows.append(a["url"])
            for code, u in _hreflang(a):
                if not scoring.owns(u, self.own) or code.lower() == "x-default":
                    continue
                self.code.setdefault(norm(u), code)
                rows.append(u)
                alts.append((k, norm(u)))
        for u in rows:
            lst = self.raw.setdefault(norm(u), [])
            if u not in lst:
                lst.append(u)
        self.show = {k: _display(v) for k, v in self.raw.items()}
        self.groups = self._group(alts)
        self.group_of = {k: g for g in self.groups for k in g}

    def _whole(self) -> str | None:
        """경로 없이 도메인 전체를 맡은 시장의 로케일(없으면 None)."""
        return next((loc for loc, sc in self.markets if not scoring.scope_of(sc)), None)

    def _code(self, k: str) -> str | None:
        """그 주소의 hreflang 코드. 없으면 첫 경로 칸이 언어 이름인 형제의 코드 — 옛 주소
        (/ja/acne-scars/papular_scar/)는 어느 hreflang 에도 없지만 /ja/ 아래다. 첫 칸이 그 코드의
        언어(ja · zh-hans → zh)일 때만 경로 칸을 언어로 친다(/acne-scar/ 같은 칸은 언어가 아니다)."""
        if k in self.code:
            return self.code[k]
        if not hasattr(self, "_seg"):
            self._seg = {}
            for u, c in self.code.items():
                seg = scoring.path_of("x://" + u).strip("/").split("/")[0].lower()
                if seg and seg.split("-")[0] == serp_adapter.lang_of(c):
                    self._seg.setdefault(seg, c)
        return self._seg.get(scoring.path_of(self.show.get(k, "")).strip("/").split("/")[0].lower())

    def _foreign(self, k: str) -> bool:
        """시장 경로에 안 드는데 hreflang 이 도메인 전체 시장과 **다른 언어**라고 말한 주소(/ja/)."""
        code, whole = self._code(k), self._whole()
        return bool(code) and serp_adapter.lang_of(code) != serp_adapter.lang_of(whole or "")

    def locale(self, k: str) -> str:
        loc, _ = market_of(self.show[k], self.markets)
        if loc:
            return loc
        if self._foreign(k):
            return self._code(k)
        return self._whole() or (self.markets[0][0] if self.markets else db.DEFAULT_LOCALE)

    def _group(self, alts) -> list[list[str]]:
        parent = {k: k for k in self.raw}

        def find(k):
            while parent[k] != k:
                parent[k] = parent[parent[k]]
                k = parent[k]
            return k

        def join(a, b):
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[max(ra, rb)] = min(ra, rb)

        for a, b in alts:                             # 1. hreflang
            join(a, b)
        # 2. 시장 경로 — 시장이 둘 이상이고 그중 경로를 가진 시장이 있을 때만 뜻이 있다.
        #    경로 없는 시장(도메인 전체)이 있으면 어느 경로에도 안 드는 주소가 그 시장 것이다.
        scs = [scoring.scope_of(sc) for _, sc in self.markets]
        if len(self.markets) >= 2 and any(scs):
            whole = "" in scs
            by_rest: dict[str, str] = {}
            for k in self.raw:
                loc, rest = market_of(self.show[k], self.markets)
                if loc is None and not whole:
                    continue
                if loc is None and self._foreign(k):
                    continue      # 시장에 없는 언어판(/ja/)은 경로가 아니라 hreflang 으로만 묶는다
                key = norm("x://h" + rest)
                if key in by_rest:
                    join(by_rest[key], k)
                else:
                    by_rest[key] = k
        out: dict[str, list[str]] = {}
        for k in parent:
            out.setdefault(find(k), []).append(k)
        return list(out.values())

    def raws(self, keys) -> list[str]:
        return [u for k in keys for u in self.raw.get(k, [k])]

    def label(self, keys: list[str]) -> str:
        for k in keys:
            a = self.audits.get(k)
            if a and (a["title"] or "").strip():
                return a["title"].strip()
        return scoring.path_of(self.show.get(keys[0], keys[0]))


def groups(conn: sqlite3.Connection, pid: int) -> list[list[str]]:
    """언어판 묶음 — [[보일 주소, …], …]. 묶는 규칙은 머리말."""
    site = _Site(conn, pid)
    return [[site.show[k] for k in g] for g in site.groups]


def _lang_labels(locs: list[str]) -> dict[str, str]:
    """로케일 → 짧은 언어 이름('한국어'·'중국어(간체)'). 같은 이름이 둘이면(en-US·en-GB) 그 둘만
    지역까지 붙인 이름으로."""
    short = {loc: brief.lang_label(loc) for loc in locs}
    dup = {v for v in short.values() if list(short.values()).count(v) > 1}
    return {loc: (serp_adapter.locale_label(loc) if short[loc] in dup else short[loc]) for loc in locs}


def _totals(site: _Site, keys: list[str]) -> tuple[dict[str, dict], dict]:
    """열쇠 → 최근 창의 노출·클릭·CTR·평균 순위. 일별이 있으면 그 마지막 28일, 없으면
    (일별을 받기 전) 최근 스냅샷의 검색어 합. 평균 순위는 노출 가중이다. 갈린 주소는 더한다."""
    conn, pid = site.conn, site.pid
    raws = site.raws(keys)
    if not raws:
        return {}, {}
    marks = ",".join("?" * len(raws))
    last = conn.execute("SELECT MAX(date) FROM gsc_page_daily WHERE project_id=?",
                        (pid,)).fetchone()[0]
    if last:
        since = str(date.fromisoformat(last) - timedelta(days=WINDOW_DAYS - 1))
        rows = conn.execute(
            f"""SELECT page, SUM(clicks) c, SUM(impressions) i, SUM(position*impressions) pw
                  FROM gsc_page_daily WHERE project_id=? AND date BETWEEN ? AND ?
                   AND page IN ({marks}) GROUP BY page""", (pid, since, last, *raws)).fetchall()
        win = {"from": since, "to": last, "source": "daily"}
    else:
        snap = conn.execute("SELECT MAX(snapshot_date) FROM gsc_snapshots WHERE project_id=?",
                            (pid,)).fetchone()[0]
        rows = conn.execute(
            f"""SELECT page, SUM(clicks) c, SUM(impressions) i, SUM(position*impressions) pw
                  FROM gsc_snapshots WHERE project_id=? AND snapshot_date=?
                   AND page IN ({marks}) GROUP BY page""", (pid, snap, *raws)).fetchall()
        win = {"from": None, "to": snap, "source": "snapshot"}
    acc: dict[str, list] = {}
    for r in rows:
        a = acc.setdefault(norm(r["page"]), [0, 0, 0.0])
        a[0] += int(r["c"] or 0)
        a[1] += int(r["i"] or 0)
        a[2] += float(r["pw"] or 0)
    out = {k: {"clicks": c, "impressions": i, "ctr": round(c / i, 4) if i else None,
               "position": round(pw / i, 1) if i else None} for k, (c, i, pw) in acc.items()}
    return out, win


def page_index(conn: sqlite3.Connection, pid: int) -> dict:
    """GET /api/pages 본체 — 고르는 칸의 글 목록(클릭 순)."""
    site = _Site(conn, pid)
    tot, win = _totals(site, list(site.raw))
    locs = sorted({site.locale(k) for k in site.raw})
    names = _lang_labels(locs)
    pages = []
    for g in site.groups:
        g = sorted(g, key=lambda k: (-(tot.get(k) or {}).get("clicks", 0), k))
        pages.append({
            "key": site.show[g[0]], "label": site.label(g),
            "urls": [{"url": site.show[k], "locale": site.locale(k)} for k in g],
            "clicks": sum((tot.get(k) or {}).get("clicks", 0) for k in g),
            "impressions": sum((tot.get(k) or {}).get("impressions", 0) for k in g)})
    pages.sort(key=lambda p: (-p["clicks"], -p["impressions"], p["key"]))
    return {"pages": pages[:INDEX_MAX], "total": len(pages), "window": win,
            "markets": [{"locale": loc, "label": serp_adapter.locale_label(loc)}
                        for loc, _ in site.markets],
            "lang_labels": names}


def _daily(site: _Site, keys: list[str]) -> dict:
    """보일 주소 → [[날짜, 클릭, 노출, CTR, 순위], …] 최근 TREND_DAYS 일, 날짜 오름차순.
    갈린 주소는 그날끼리 더한다(CTR·순위는 다시 잰다 — 순위는 노출 가중)."""
    conn, pid = site.conn, site.pid
    last = conn.execute("SELECT MAX(date) FROM gsc_page_daily WHERE project_id=?",
                        (pid,)).fetchone()[0]
    raws = site.raws(keys)
    if not last or not raws:
        return {"to": last, "series": {}}
    since = str(date.fromisoformat(last) - timedelta(days=TREND_DAYS - 1))
    marks = ",".join("?" * len(raws))
    acc: dict[str, dict[str, list]] = {}
    for r in conn.execute(
            f"""SELECT page, date, clicks, impressions, position FROM gsc_page_daily
                 WHERE project_id=? AND date BETWEEN ? AND ? AND page IN ({marks})""",
            (pid, since, last, *raws)):
        a = acc.setdefault(site.show[norm(r["page"])], {}).setdefault(r["date"], [0, 0, 0.0])
        a[0] += int(r["clicks"] or 0)
        a[1] += int(r["impressions"] or 0)
        a[2] += float(r["position"] or 0) * int(r["impressions"] or 0)
    series = {u: [[d, c, i, round(c / i, 4) if i else None, round(pw / i, 1) if i else None]
                  for d, (c, i, pw) in sorted(by.items())] for u, by in acc.items()}
    return {"from": since, "to": last, "series": series}


def _queries(site: _Site, keys: list[str]) -> list[dict]:
    conn, pid = site.conn, site.pid
    snap = conn.execute("SELECT MAX(snapshot_date) FROM gsc_snapshots WHERE project_id=?",
                        (pid,)).fetchone()[0]
    raws = site.raws(keys)
    if not snap or not raws:
        return []
    marks = ",".join("?" * len(raws))
    acc: dict[tuple, list] = {}
    for r in conn.execute(
            f"""SELECT page, query, clicks, impressions, position FROM gsc_snapshots
                 WHERE project_id=? AND snapshot_date=? AND page IN ({marks})""",
            (pid, snap, *raws)):
        a = acc.setdefault((site.show[norm(r["page"])], r["query"]), [0, 0, 0.0])
        a[0] += int(r["clicks"] or 0)
        a[1] += int(r["impressions"] or 0)
        a[2] += float(r["position"] or 0) * int(r["impressions"] or 0)
    out = [{"url": u, "query": q, "clicks": c, "impressions": i,
            "ctr": round(c / i, 4) if i else None, "position": round(pw / i, 1) if i else None}
           for (u, q), (c, i, pw) in acc.items()]
    out.sort(key=lambda r: (-r["clicks"], -r["impressions"], r["query"]))
    return out[:QUERIES_MAX]


def _ai(site: _Site, keys: list[str]) -> dict:
    """가장 최근 ai 런에서 이 주소들을 인용한 답 — 질문·엔진마다 한 줄.
    같은 답에 함께 인용된 다른 곳(우리 도메인 밖)도 싣는다 — 누구와 나란히 섰나."""
    conn, pid = site.conn, site.pid
    run = scoring.latest_ai_run(conn, pid)
    out = {"run_at": None, "rows": []}
    if not run:
        return out
    out["run_at"] = run["finished_at"] or run["started_at"]
    want = set(keys)
    acc: dict[tuple, dict] = {}
    for c in conn.execute(
            """SELECT q.prompt, q.locale, c.engine, c.citations_json
                 FROM ai_checks c JOIN ai_prompts q ON q.id=c.prompt_id
                WHERE c.run_id=? AND c.citations_json IS NOT NULL
                ORDER BY q.prompt, c.engine, c.sample_idx""", (run["id"],)):
        try:
            items = json.loads(c["citations_json"]) or []
        except (TypeError, ValueError):
            continue
        hit = [site.show.get(norm(it.get("url") or ""), it.get("url")) for it in items
               if norm(it.get("url") or "") in want]
        row = acc.setdefault((c["prompt"], c["engine"]), {
            "prompt": c["prompt"], "locale": c["locale"], "engine": c["engine"],
            "samples": 0, "hits": 0, "urls": [], "others": []})
        row["samples"] += 1
        if not hit:
            continue
        row["hits"] += 1
        for u in hit:
            if u not in row["urls"]:
                row["urls"].append(u)
        for it in items:
            d = scoring.host_of(it.get("url") or "") or it.get("domain") or ""
            if d and not scoring.owns(d, site.own) and d not in row["others"]:
                row["others"].append(d)
    out["rows"] = sorted((r for r in acc.values() if r["hits"]),
                         key=lambda r: (-r["hits"], r["prompt"], r["engine"]))
    return out


def page_detail(conn: sqlite3.Connection, pid: int, url: str) -> dict:
    """GET /api/page 본체 — 그 주소가 든 글 하나의 전부. 모르는 주소면 그 주소 하나로 선다
    (다른 화면이 아직 수집에 안 잡힌 주소로 데려와도 빈 화면이 아니라 "아직 없다"를 말한다)."""
    site = _Site(conn, pid)
    n = norm(url)
    if n not in site.raw:
        site.raw[n], site.show[n] = [url], url
    group = site.group_of.get(n, [n])
    tot, win = _totals(site, group)
    group = sorted(group, key=lambda k: (-(tot.get(k) or {}).get("clicks", 0), k))
    names = _lang_labels(sorted({site.locale(k) for k in group}))
    versions = []
    for k in group:
        loc, a, t = site.locale(k), site.audits.get(k), tot.get(k) or {}
        versions.append({
            "url": site.show[k], "variants": site.raw[k], "locale": loc,
            "locale_label": names[loc],
            "clicks": t.get("clicks", 0), "impressions": t.get("impressions", 0),
            "ctr": t.get("ctr"), "position": t.get("position"),
            "title": (a["title"] if a else None) or None,
            "modified": a["modified"] if a else None, "published": a["published"] if a else None,
            "audited": a["checked_date"] if a else None})
    return {"key": site.show[group[0]], "label": site.label(group), "asked": url,
            "versions": versions, "window": win,
            "daily": _daily(site, group), "queries": _queries(site, group),
            "ai": _ai(site, group)}
