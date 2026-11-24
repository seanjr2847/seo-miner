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
                 "대시보드 [설정]의 이 사이트 설정에서 넣습니다.")
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


def rows_sum(rows: list) -> dict:
    """**돌려받은 행만** 더한 값 — 사이트 합계가 아니다(그건 true_totals).

    행은 --limit 에서 잘리고(클릭 순 상위 N), 검색어·페이지로 묶으면 API 가 일부 데이터를
    떨군다. 그래서 이 값은 늘 진짜 합계보다 작거나 같다 — 예전에는 이것을 `totals` 라는
    이름으로 내보내 "지난 28일 클릭 합계"로 읽혔다.

    ctr·position 은 합이 아니라 노출 가중 평균이다. 단순 평균을 내면 노출 3짜리
    키워드가 노출 3만짜리와 같은 무게를 가져 값이 통째로 거짓이 된다.
    """
    clicks = sum(r["clicks"] or 0 for r in rows)
    imp = sum(r["impressions"] or 0 for r in rows)
    pos = (sum((r["position"] or 0) * (r["impressions"] or 0) for r in rows) / imp
           if imp else None)
    return {"clicks": clicks, "impressions": imp,
            "ctr": (clicks / imp) if imp else None, "position": pos}


def true_totals(service, prop: str, start: str, end: str, dims: list,
                filters: list, state: str) -> dict:
    """같은 창·같은 필터의 **진짜 합계** — 차원 없이 한 번 더 묻는다.

    차원을 안 주면 API 가 전부를 한 행으로 합쳐 준다("If no dimensions are specified, all
    values will be combined into a single row"), 필터는 묶지 않는 차원에도 걸 수 있다("You
    can filter by any dimension listed here, even if you are not grouping by that dimension")
    — https://developers.google.com/webmaster-tools/v1/searchanalytics/query

    집계 기준은 행과 맞춘다. 페이지로 묶거나 거르면 행은 페이지 기준(byPage)으로 세어진다
    ("If you group or filter by page, you cannot aggregate by property" — 같은 문서). 그때
    합계만 속성 기준으로 받으면 행과 합계가 서로 다른 것을 센다. 그래서 행에 page 차원이
    있으면 합계도 byPage 로 청한다(차원 없는 byPage 는 공식 가이드의 '정확한 합계' 틀이다
    — https://developers.google.com/webmaster-tools/v1/how-tos/all-your-data).
    응답의 실제 기준은 `aggregation` 에 그대로 싣는다.
    """
    body = {"startDate": start, "endDate": end, "dataState": state}
    if filters:
        body["dimensionFilterGroups"] = filters
    if "page" in dims:
        body["aggregationType"] = "byPage"
    resp = service.searchanalytics().query(siteUrl=prop, body=body).execute()
    r = (resp.get("rows") or [{}])[0]
    return {"clicks": r.get("clicks") or 0, "impressions": r.get("impressions") or 0,
            "ctr": r.get("ctr"), "position": r.get("position"),
            "aggregation": resp.get("responseAggregationType")}


def _top_rows(rows: list, limit: int) -> dict:
    """행 합(rows_sum)에 '몇 행을 더한 것인지'를 붙인다 — 읽는 쪽이 합계로 착각하지 않게."""
    return {**rows_sum(rows), "rows": len(rows), "limit": min(limit, MAX_ROWS)}


def cmd_properties(a) -> dict:
    """내 계정이 볼 수 있는 속성 전부 (MCP 의 list_properties 자리)."""
    resp = get_service().sites().list().execute()
    return {"properties": [{"property": s.get("siteUrl"),
                            "permission": s.get("permissionLevel")}
                           for s in resp.get("siteEntry", [])]}


def cmd_search(a) -> dict:
    prop, (start, end) = resolve_property(a), window(a)
    dims = [d.strip() for d in a.dim.split(",") if d.strip()]
    svc, filters = get_service(), parse_filters(a.filter)
    rows = search(svc, prop, start, end, dims, a.limit, filters, a.state)
    # totals = 창 전체의 진짜 합계(차원 없는 질의). top_rows_sum = 아래 rows 만 더한 값.
    return {"property": prop, "start": start, "end": end, "dimensions": dims,
            "data_state": a.state,
            "totals": true_totals(svc, prop, start, end, dims, filters, a.state),
            "top_rows_sum": _top_rows(rows, a.limit), "rows": rows}


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
    # 두 창의 totals 는 각각 진짜 합계다 — "지난달보다 클릭이 얼마" 는 이 둘로 말한다.
    # 상위 N행의 합(top_rows_sum)끼리 빼면 순위권 밖으로 밀린 검색어의 클릭이 통째로 빠진다.
    return {"property": prop, "dimensions": dims, "data_state": a.state,
            "current": {"start": cur_start, "end": cur_end,
                        "totals": true_totals(svc, prop, cur_start, cur_end, dims,
                                              filters, a.state),
                        "top_rows_sum": _top_rows(cur, a.limit)},
            "previous": {"start": str(prev_start), "end": str(prev_end),
                         "totals": true_totals(svc, prop, str(prev_start), str(prev_end),
                                               dims, filters, a.state),
                         "top_rows_sum": _top_rows(prev, a.limit)},
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

    t = rows_sum([{"clicks": 1, "impressions": 10, "ctr": 0.1, "position": 30.0},
                  {"clicks": 9, "impressions": 90, "ctr": 0.1, "position": 10.0}])
    assert t["clicks"] == 10 and t["impressions"] == 100
    assert abs(t["position"] - 12.0) < 1e-9, t["position"]   # 단순평균 20 이면 거짓
    assert abs(t["ctr"] - 0.1) < 1e-9
    _selfcheck_totals()


def _selfcheck_totals() -> None:
    """totals 는 창 전체의 진짜 합계(차원 없는 질의), 상위 N행의 합은 top_rows_sum 이다.

    가짜 서비스: 검색어 5개(클릭 50·40·30·20·10 = 150) + 검색어로 안 잡히는 클릭 50 = 200.
    --limit 2 면 행은 2개(합 90)인데, 예전에는 그 90 이 `totals` 로 나갔다."""
    calls = []
    ALL = [("a", 50), ("b", 40), ("c", 30), ("d", 20), ("e", 10)]

    class Q:
        def __init__(self, body):
            self.body = body

        def execute(self):
            b = self.body
            calls.append(b)
            if not b.get("dimensions"):
                return {"rows": [{"clicks": 200, "impressions": 4000, "ctr": 0.05, "position": 8.5}],
                        "responseAggregationType": b.get("aggregationType") or "byProperty"}
            return {"rows": [{"keys": [k] * len(b["dimensions"]), "clicks": c,
                              "impressions": c * 10, "ctr": 0.1, "position": 5.0}
                             for k, c in ALL[: b["rowLimit"]]]}

    class Svc:
        def searchanalytics(self):
            return self

        def query(self, siteUrl, body):
            return Q(body)

    class A:
        property, project = "sc-domain:x.com", None
        start, end, days = "2026-01-01", "2026-01-28", 28
        dim, limit, filter, state = "query", 2, ["page:contains:/blog/"], "all"

    g = globals()
    real = g["get_service"]
    g["get_service"] = lambda: Svc()
    try:
        out = cmd_search(A())
        assert out["totals"]["clicks"] == 200 and out["totals"]["impressions"] == 4000, out["totals"]
        assert out["top_rows_sum"]["clicks"] == 90 and out["top_rows_sum"]["rows"] == 2, out["top_rows_sum"]
        assert out["top_rows_sum"]["limit"] == 2
        assert out["totals"]["aggregation"] == "byProperty", out["totals"]
        tot = [b for b in calls if not b.get("dimensions")]
        assert len(tot) == 1 and "rowLimit" not in tot[0] and "aggregationType" not in tot[0], tot
        # 합계 질의도 같은 창·같은 필터·같은 확정 상태여야 한다 — 아니면 다른 것을 센다
        assert tot[0]["dimensionFilterGroups"] == parse_filters(A.filter) and tot[0]["dataState"] == "all"
        assert (tot[0]["startDate"], tot[0]["endDate"]) == ("2026-01-01", "2026-01-28")

        calls.clear()
        A.dim = "query,page"                       # 페이지로 묶으면 합계도 페이지 기준으로
        out = cmd_search(A())
        tot = [b for b in calls if not b.get("dimensions")]
        assert tot[0].get("aggregationType") == "byPage" and out["totals"]["aggregation"] == "byPage", tot

        calls.clear()
        A.dim = "query"
        cmp_ = cmd_compare(A())
        for side, (st, en) in (("current", ("2026-01-01", "2026-01-28")),
                               ("previous", ("2025-12-04", "2025-12-31"))):
            assert cmp_[side]["totals"]["clicks"] == 200, cmp_[side]
            assert cmp_[side]["top_rows_sum"]["clicks"] == 90, cmp_[side]
            assert any(not b.get("dimensions") and (b["startDate"], b["endDate"]) == (st, en)
                       for b in calls), (side, calls)
    finally:
        g["get_service"] = real
    print("gsc_query selfcheck ok")


if __name__ == "__main__":
    if len(sys.argv) == 1:
        _selfcheck()
    else:
        main()
