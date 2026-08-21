#!/usr/bin/env python3
"""GSC 즉석 조회 — Brain에 적재하지 않고 서치콘솔 원본을 바로 읽는다.

예전에는 이 자리를 번들 gsc MCP 서버(mcp-search-console)가 맡았다. 그 서버를
걷어내면서 역할만 여기로 옮겨 왔다 — 인증도, 조회도 seo-miner 안에서 끝난다.
열쇠는 여전히 한 벌이다: `collect_gsc.get_service()` 를 그대로 빌린다.

**수집기(collect_gsc.py)와 역할이 다르다.**
  · 이쪽(즉석 조회): dataState=all — 미확정 포함, 창 끝이 오늘. GSC 대시보드와 같은 값.
    "어제 클릭 몇이야" 같은 지금 이 순간의 질문에 답하는 자리.
  · 저쪽(벌크 수집): dataState=final + 3일 버퍼. 스냅샷끼리 Δ 를 비교하려면 나중에
    값이 바뀌는 날짜가 섞이면 안 된다.
그래서 **두 경로의 숫자는 원래 다르다** — 빼서 증감을 말하면 안 된다(SKILL.md 철칙 1).

출력은 전부 JSON(stdout). 읽는 쪽은 Claude다.

Usage:
  python gsc_query.py properties
  python gsc_query.py search  --project NAME [--days 28] [--dim query,page]
                              [--limit 25] [--filter page:contains:/blog/] [--state all]
  python gsc_query.py search  --property sc-domain:example.com --start 2026-08-01 --end 2026-08-14
  python gsc_query.py compare --project NAME [--days 28] [--dim query] [--limit 25]
  python gsc_query.py inspect --project NAME URL [URL ...]
  python gsc_query.py sitemaps --project NAME
"""
import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import db  # noqa: E402
from collect_gsc import get_service  # noqa: E402

MAX_ROWS = 25000       # Search Analytics 가 한 번에 주는 최대 행 수


def resolve_property(a) -> str:
    """--property 를 그대로 쓰거나, --project 이름을 Brain 에서 속성으로 옮긴다.

    이름으로 부르는 쪽이 사람에게 자연스럽고, 속성 문자열을 외우게 하지 않는다.
    """
    if a.property:
        return a.property
    if not a.project:
        sys.exit("--project 나 --property 중 하나는 있어야 합니다.")
    if not db.DB_PATH.exists():
        sys.exit(f"보관함이 아직 없습니다({db.DB_PATH}) — --property 로 직접 지정하세요.")
    conn = db.connect_ro()
    row = conn.execute("SELECT gsc_property FROM projects WHERE name=?",
                       (a.project,)).fetchone()
    conn.close()
    if not row:
        sys.exit(f"등록되지 않은 사이트입니다: {a.project}")
    if not row["gsc_property"]:
        sys.exit(f"{a.project} 에 gsc_property 가 비어 있습니다 — "
                 "projects yaml 에 넣고 `python db.py sync-project <yaml>`.")
    return row["gsc_property"]


def window(a) -> tuple[str, str]:
    """조회 창. --start/--end 가 있으면 그것, 없으면 오늘까지 --days 일.

    수집기와 달리 3일 버퍼를 빼지 않는다 — 즉석 조회는 최신 미확정치를 보여주는 게
    목적이다(모듈 독스트링의 역할 구분).
    """
    if a.start and a.end:
        return a.start, a.end
    if a.start or a.end:
        sys.exit("--start 와 --end 는 짝으로 주세요 (또는 --days 만).")
    end = date.today()
    return str(end - timedelta(days=a.days)), str(end)


def parse_filters(specs: list) -> list:
    """`dimension:operator:expression` → API dimensionFilterGroups.

    operator 생략(`page:/blog/`)이면 contains — 즉석 조회에서 제일 흔한 쓰임이다.
    """
    ops = {"contains", "equals", "notContains", "notEquals",
           "includingRegex", "excludingRegex"}
    out = []
    for spec in specs or []:
        parts = spec.split(":", 2)
        if len(parts) == 3 and parts[1] in ops:
            dim, op, expr = parts
        elif len(parts) >= 2:
            dim, op, expr = parts[0], "contains", spec.split(":", 1)[1]
        else:
            sys.exit(f"--filter 형식이 아닙니다: {spec}  (예: page:contains:/blog/)")
        out.append({"dimension": dim, "operator": op, "expression": expr})
    return [{"filters": out}] if out else []


def search(service, prop: str, start: str, end: str, dims: list, limit: int,
           filters: list, state: str) -> list:
    """Search Analytics 한 번. 행은 keys 를 차원 이름으로 풀어서 돌려준다."""
    body = {"startDate": start, "endDate": end, "dimensions": dims,
            "rowLimit": min(limit, MAX_ROWS), "dataState": state}
    if filters:
        body["dimensionFilterGroups"] = filters
    resp = service.searchanalytics().query(siteUrl=prop, body=body).execute()
    rows = []
    for r in resp.get("rows", []):
        row = dict(zip(dims, r.get("keys", [])))
        row.update({"clicks": r.get("clicks"), "impressions": r.get("impressions"),
                    "ctr": r.get("ctr"), "position": r.get("position")})
        rows.append(row)
    return rows


def totals(rows: list) -> dict:
    """행 합계 — 비교(compare)가 두 창을 한 줄로 요약할 때 쓴다.

    ctr·position 은 합이 아니라 노출 가중 평균이다. 단순 평균을 내면 노출 3짜리
    키워드가 노출 3만짜리와 같은 무게를 가져 값이 통째로 거짓이 된다.
    """
    clicks = sum(r["clicks"] or 0 for r in rows)
    imp = sum(r["impressions"] or 0 for r in rows)
    pos = (sum((r["position"] or 0) * (r["impressions"] or 0) for r in rows) / imp
           if imp else None)
    return {"clicks": clicks, "impressions": imp,
            "ctr": (clicks / imp) if imp else None, "position": pos}


def cmd_properties(a) -> dict:
    """내 계정이 볼 수 있는 속성 전부 (MCP 의 list_properties 자리)."""
    resp = get_service().sites().list().execute()
    return {"properties": [{"property": s.get("siteUrl"),
                            "permission": s.get("permissionLevel")}
                           for s in resp.get("siteEntry", [])]}


def cmd_search(a) -> dict:
    prop, (start, end) = resolve_property(a), window(a)
    dims = [d.strip() for d in a.dim.split(",") if d.strip()]
    rows = search(get_service(), prop, start, end, dims, a.limit,
                  parse_filters(a.filter), a.state)
    return {"property": prop, "start": start, "end": end, "dimensions": dims,
            "data_state": a.state, "totals": totals(rows), "rows": rows}


def cmd_compare(a) -> dict:
    """같은 길이의 직전 창과 비교 (MCP 의 compare_search_periods 자리).

    창을 맞붙여 자른다 — 직전 창의 끝은 현재 창 시작 하루 전이다. 겹치면 같은
    날짜가 양쪽에 들어가 Δ 가 그만큼 부풀어 오른다.
    """
    prop = resolve_property(a)
    cur_start, cur_end = window(a)
    span = (date.fromisoformat(cur_end) - date.fromisoformat(cur_start)).days
    prev_end = date.fromisoformat(cur_start) - timedelta(days=1)
    prev_start = prev_end - timedelta(days=span)
    dims = [d.strip() for d in a.dim.split(",") if d.strip()]
    svc, filters = get_service(), parse_filters(a.filter)

    cur = search(svc, prop, cur_start, cur_end, dims, a.limit, filters, a.state)
    prev = search(svc, prop, str(prev_start), str(prev_end), dims, a.limit,
                  filters, a.state)
    key = lambda r: tuple(r[d] for d in dims)       # noqa: E731
    before = {key(r): r for r in prev}

    changes = []
    for r in cur:
        b = before.get(key(r))
        changes.append({
            **{d: r[d] for d in dims},
            "clicks": r["clicks"], "clicks_prev": (b or {}).get("clicks", 0),
            "clicks_delta": (r["clicks"] or 0) - ((b or {}).get("clicks") or 0),
            "impressions": r["impressions"],
            "impressions_prev": (b or {}).get("impressions", 0),
            "position": r["position"], "position_prev": (b or {}).get("position"),
            # 순위는 작을수록 좋다 — 개선을 양수로 보이게 이전 − 현재로 뺀다.
            "position_delta": (round((b["position"] or 0) - (r["position"] or 0), 2)
                               if b and b.get("position") and r["position"] else None),
            "new": b is None})
    changes.sort(key=lambda c: -abs(c["clicks_delta"]))
    return {"property": prop, "dimensions": dims, "data_state": a.state,
            "current": {"start": cur_start, "end": cur_end, "totals": totals(cur)},
            "previous": {"start": str(prev_start), "end": str(prev_end),
                         "totals": totals(prev)},
            "changes": changes}


def cmd_inspect(a) -> dict:
    """URL 색인 상태 즉석 확인 (MCP 의 inspect_url_enhanced/batch_url_inspection 자리).

    **저장하지 않는다.** 반복해서 볼 것·다음 런과 대조할 것은 collect_index.py 다
    (그쪽은 gsc_index_status 에 적재돼 index_blocked 기회로 올라온다).
    URL 당 정확히 1콜이고 속성당 하루 2,000회 쿼터를 나눠 쓴다.
    """
    prop, svc = resolve_property(a), get_service()
    out = []
    for url in a.urls:
        try:
            res = svc.urlInspection().index().inspect(body={
                "inspectionUrl": url, "siteUrl": prop}).execute()
        except Exception as e:      # 한 URL 이 죽어도 나머지는 본다
            out.append({"url": url, "error": str(e)})
            continue
        idx = (res.get("inspectionResult") or {}).get("indexStatusResult") or {}
        out.append({"url": url, "verdict": idx.get("verdict"),
                    "coverage_state": idx.get("coverageState"),
                    "robots_txt_state": idx.get("robotsTxtState"),
                    "page_fetch_state": idx.get("pageFetchState"),
                    "indexing_state": idx.get("indexingState"),
                    "google_canonical": idx.get("googleCanonical"),
                    "user_canonical": idx.get("userCanonical"),
                    "last_crawled": idx.get("lastCrawlTime")})
    return {"property": prop, "stored": False, "results": out}


def cmd_sitemaps(a) -> dict:
    prop = resolve_property(a)
    resp = get_service().sitemaps().list(siteUrl=prop).execute()
    return {"property": prop, "sitemaps": [
        {"path": s.get("path"), "last_submitted": s.get("lastSubmitted"),
         "last_downloaded": s.get("lastDownloaded"), "is_pending": s.get("isPending"),
         "errors": s.get("errors"), "warnings": s.get("warnings"),
         "contents": s.get("contents")}
        for s in resp.get("sitemap", [])]}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    def target(p, window_opts=True):
        p.add_argument("--project", help="등록된 사이트 이름 (Brain 에서 속성을 찾는다)")
        p.add_argument("--property", help="속성 문자열 직접 지정 "
                                          "(sc-domain:example.com 또는 https://example.com/)")
        if window_opts:
            p.add_argument("--days", type=int, default=28, help="오늘까지 며칠 (기본 28)")
            p.add_argument("--start", help="시작일 YYYY-MM-DD (--end 와 짝)")
            p.add_argument("--end", help="종료일 YYYY-MM-DD")
            p.add_argument("--dim", default="query",
                           help="차원 콤마 구분: query,page,date,device,country (기본 query)")
            p.add_argument("--limit", type=int, default=25, help="최대 행 수 (기본 25)")
            p.add_argument("--filter", action="append",
                           help="dimension:operator:expression (예: page:contains:/blog/). "
                                "여러 번 줄 수 있음")
            # 즉석 조회의 기본은 all 이다 — 이 선택이 곧 이 스크립트의 존재 이유다
            # (모듈 독스트링의 역할 구분). final 로 바꾸면 수집기와 같은 숫자가 된다.
            p.add_argument("--state", default="all", choices=("all", "final"),
                           help="데이터 확정 상태 (기본 all — 미확정 포함)")
        return p

    sub.add_parser("properties", help="볼 수 있는 속성 목록")
    target(sub.add_parser("search", help="검색 실적 즉석 조회"))
    target(sub.add_parser("compare", help="직전 같은 길이 창과 비교"))
    ins = target(sub.add_parser("inspect", help="URL 색인 상태 즉석 확인 (저장 안 함)"),
                 window_opts=False)
    ins.add_argument("urls", nargs="+")
    target(sub.add_parser("sitemaps", help="사이트맵 목록·상태"), window_opts=False)

    a = ap.parse_args()
    out = {"properties": cmd_properties, "search": cmd_search, "compare": cmd_compare,
           "inspect": cmd_inspect, "sitemaps": cmd_sitemaps}[a.cmd](a)
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))


def _selfcheck() -> None:
    """네트워크 없이 확인되는 것 — 창 계산·필터 파싱·합계 가중평균."""
    class A:
        start = end = None
        days = 7
    assert window(A()) == (str(date.today() - timedelta(days=7)), str(date.today()))
    A.start, A.end = "2026-01-01", "2026-01-31"
    assert window(A()) == ("2026-01-01", "2026-01-31")

    assert parse_filters(["page:contains:/blog/"]) == \
        [{"filters": [{"dimension": "page", "operator": "contains",
                       "expression": "/blog/"}]}]
    # operator 생략 — 콜론이 든 값(URL)이 잘려 나가면 안 된다
    assert parse_filters(["page:https://x.com/a"])[0]["filters"][0]["expression"] \
        == "https://x.com/a"

    t = totals([{"clicks": 1, "impressions": 10, "ctr": 0.1, "position": 30.0},
                {"clicks": 9, "impressions": 90, "ctr": 0.1, "position": 10.0}])
    assert t["clicks"] == 10 and t["impressions"] == 100
    assert abs(t["position"] - 12.0) < 1e-9, t["position"]   # 단순평균 20 이면 거짓
    assert abs(t["ctr"] - 0.1) < 1e-9
    print("gsc_query selfcheck ok")


if __name__ == "__main__":
    if len(sys.argv) == 1:
        _selfcheck()
    else:
        main()
