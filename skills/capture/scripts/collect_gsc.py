#!/usr/bin/env python3
"""Sync Google Search Console data into the Brain (free, real measurements).

Known holes (read reports accordingly, see references/scoring.md):
  * ~2-3 day data delay; low-volume longtail queries are privacy-filtered out
  * position is an AVERAGE across impressions, not a pinpoint rank

Auth: gsc MCP 서버와 인증을 공유한다 — 어느 쪽으로 붙였든 열쇠는 한 벌이다.
  1) 서비스 계정 키(권장, 무인 수집): $CAPTURE_HOME/gsc_service_account.json
     설치: python ../../setup/scripts/connect_gsc.py  (setup.md 4-B)
  2) 키가 없으면 MCP 서버(mcp-search-console)의 인증을 빌린다 — OAuth로 붙여
     뒀다면 그 토큰을 그대로 쓰므로 서비스 계정을 따로 만들 필요가 없다.
Setup walkthrough: references/setup.md

Usage:
  python collect_gsc.py --project NAME [--days 28] [--row-limit 25000]
                        [--breakdown device,country] [--dry-run]
"""
import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import collector  # noqa: E402
import db  # noqa: E402
import scoring  # noqa: E402

SCOPES = ["https://www.googleapis.com/auth/webmasters.readonly"]
PAGE = 25000   # Search Analytics API가 요청 한 번에 주는 최대 행 수 (그 이상은 startRow)


def _service_via_mcp():
    """gsc MCP 서버(mcp-search-console)의 인증을 그대로 빌려 쓴다.

    서버가 파이썬 패키지라 JSON-RPC를 왕복할 필요가 없다 — 인증 해석기만 부르면
    같은 종류의 서비스 객체가 나온다. 덕분에 **OAuth로 붙여 둔 사람은 열쇠를 따로
    안 만들어도 된다**: 브라우저 로그인 한 번으로 만든 토큰을 MCP와 이 수집기가
    같이 쓴다 (토큰은 서버가 user_config_dir 에 캐시한다).

    설정은 import 시점에 읽히므로 환경변수를 먼저 걸어야 한다 — 런처(gsc_mcp.mjs)와
    같은 규칙이라 어느 쪽으로 들어와도 같은 열쇠를 본다.
    """
    import os
    # 있는 파일만 가리킨다 — 없는 경로를 걸면 서버가 인증을 시도하기도 전에
    # "그 파일이 없다"로 fail-fast 한다 (gsc_mcp.mjs와 같은 규칙).
    if db.gsc_oauth_client().exists():
        os.environ.setdefault("GSC_OAUTH_CLIENT_SECRETS_FILE", str(db.gsc_oauth_client()))
    else:
        os.environ.setdefault("GSC_SKIP_OAUTH", "true")
    if db.gsc_key().exists():
        os.environ.setdefault("GSC_CREDENTIALS_PATH", str(db.gsc_key()))
    try:
        import gsc_server
    except ImportError:
        return None
    try:
        return gsc_server.get_gsc_service()
    except Exception:
        return None       # 인증 못 하면 아래의 안내문이 이유를 말한다


def get_service(project: str):
    try:
        from googleapiclient.discovery import build
    except ImportError:
        sys.exit("구글 연동 부품이 없습니다 — 먼저 실행: "
                 "pip install google-api-python-client")

    mode = db.gsc_auth()

    # 서비스 계정만 걸려 있으면 직결한다 — 읽기 전용 스코프에 무인 실행이고,
    # 무거운 MCP 서버 모듈을 import 하지 않아도 된다.
    if mode == "service_account":
        from google.oauth2 import service_account
        creds = service_account.Credentials.from_service_account_file(
            str(db.gsc_key()), scopes=SCOPES)
        return build("searchconsole", "v1", credentials=creds,
                     cache_discovery=False)

    # 기본(OAuth)은 MCP 서버의 인증에 얹힌다 — 토큰을 둘이 공유한다.
    # 토큰이 아직 없으면 여기서 브라우저 로그인이 한 번 열린다.
    if mode == "oauth":
        svc = _service_via_mcp()
        if svc is not None:
            return svc

    sys.exit("구글 서치콘솔 인증이 없습니다 — 로그인 한 번이면 끝납니다.\n"
             f"  · 기본(OAuth): {db.gsc_oauth_client()} 에 클라이언트 파일이 없습니다 —\n"
             "    python ../../setup/scripts/connect_gsc.py  (setup.md 4-B)\n"
             "  · 무인 수집(서비스 계정)을 쓰신다면 같은 스크립트가 그쪽 키도 받습니다.")


def _query(service, prop: str, body: dict) -> dict:
    """Search Analytics 호출 한 번 + 403 안내.

    호출이 한 곳(query×page)뿐일 땐 try 블록 안에 안내문을 그냥 적어 뒀는데,
    일별·분해 호출이 붙으면서 그 두 개만 원인 없는 HttpError 로 죽게 됐다.
    권한 문제는 어느 호출에서 터지든 원인이 같으니 안내도 한 곳에 모은다.
    """
    from googleapiclient.errors import HttpError
    try:
        return service.searchanalytics().query(siteUrl=prop, body=body).execute()
    except HttpError as e:
        if getattr(e.resp, "status", None) == 403 and db.gsc_key().exists():
            email = json.loads(db.gsc_key().read_text(encoding="utf-8")) \
                .get("client_email", "?")
            sys.exit(f"권한 거부: {prop}\n"
                     "  Search Console → 설정 → 사용자 및 권한에 서비스 계정 "
                     f"이메일을 추가했는지 확인하세요: {email}\n"
                     "  (권한 '제한된 사용자'면 충분합니다)")
        raise


def main() -> None:
    ap = argparse.ArgumentParser()
    collector.add_common(ap)
    collector.add_setting(ap, "--days", key="gsc_days", fallback=28, type=int)
    # 콤마 구분 문자열이다 — add_setting 의 type_fn 이 스칼라만 다루므로 리스트를 못 받는다.
    collector.add_setting(ap, "--breakdown", key="gsc_breakdown", fallback="device",
                          type=str,
                          help="분해 수집 차원 (콤마 구분: device,country / 빈 문자열이면 안 함)")
    ap.add_argument("--row-limit", type=int, default=100000,
                    help="가져올 최대 행 수 (25,000행씩 나눠 받는다)")
    a = ap.parse_args()

    conn, p, cfg = collector.open_project(a.project)
    s = collector.settings(a, cfg)
    days = s["gsc_days"]
    dims = [d.strip().lower() for d in (s["gsc_breakdown"] or "").split(",") if d.strip()]
    prop = p["gsc_property"]
    if not prop:
        sys.exit("project yaml has no gsc_property "
                 "(e.g. 'sc-domain:example.com' or 'https://example.com/'). "
                 "Add it, then: python db.py sync-project <yaml>")

    end = date.today() - timedelta(days=3)     # GSC delay buffer
    start = end - timedelta(days=days)
    print(f"[gsc] {prop}  window {start} ~ {end} (period={days}d) "
          f"rowLimit={a.row_limit}")
    # 비용 고지 — dry-run 이 말한 호출 수와 실제 호출 수가 어긋나면 안 된다.
    # query×page 만 "최대"다: 마지막 장이 덜 차면 거기서 멈추므로 실제로는 더 적을 수 있다.
    qp_max = max(1, -(-a.row_limit // PAGE))
    print(f"[gsc] API 호출 계획: query×page 최대 {qp_max}회 + 일별 1회 + "
          f"분해 {len(dims)}회({', '.join(dims) or '없음'}) = 최대 {qp_max + 1 + len(dims)}회")
    if a.dry_run:
        conn.close()
        return

    service = get_service(a.project)
    rows: list = []
    calls = 0
    # API는 한 번에 최대 PAGE행만 준다 — 그 이상은 startRow로 이어받아야 한다.
    # 예전엔 한 번만 불러서, 큰 사이트가 정확히 25,000행에서 조용히 잘렸다.
    while len(rows) < a.row_limit:
        want = min(PAGE, a.row_limit - len(rows))
        resp = _query(service, prop, {
            "startDate": str(start), "endDate": str(end),
            "dimensions": ["query", "page"], "rowLimit": want,
            "startRow": len(rows), "dataState": "final"})
        batch = resp.get("rows", [])
        rows += batch
        calls += 1
        if len(batch) < want:          # 마지막 장 — 더 없다
            break
        print(f"[gsc] … {len(rows)} rows")
    print(f"[gsc] fetched {len(rows)} query x page rows")
    if len(rows) >= a.row_limit:
        print(f"[주의] --row-limit({a.row_limit})에 걸려 여기서 멈췄습니다 — "
              "더 필요하면 값을 올리세요. 지금 스냅샷은 전체가 아닙니다.")

    qp_calls = calls

    # 여기서부터는 **부수 호출**이다 — 실패해도 본체 스냅샷을 죽이지 못하게 한다.
    # 예전엔 본체 루프가 끝나면 곧바로 저장이라 이 문제가 없었는데, 뒤에 호출이
    # 둘 붙으면서 그중 하나만 실패해도 이미 받아 둔 query×page 를 통째로 잃게 됐다.
    # 실패한 축은 **쓰지 않고 건너뛴다** — 빈 값으로 쓰면 delete-then-insert 가
    # 지난번 수집분까지 지운다.
    def _optional(label: str, body: dict):
        nonlocal calls
        from googleapiclient.errors import HttpError
        try:
            resp = _query(service, prop, body)
        except (HttpError, SystemExit) as e:
            # SystemExit 도 받는다 — _query 의 403 안내가 sys.exit 로 끝나기 때문이다.
            # 본체가 이미 성공한 뒤라면 권한 안내 하나로 수확을 버릴 이유가 없다.
            print(f"[경고] {label} 수집을 건너뜁니다 ({e}) — "
                  "본체 스냅샷은 그대로 저장됩니다.", file=sys.stderr)
            return None
        calls += 1
        return resp.get("rows", [])

    # 창(window) 안의 날짜별 추이. query×page 요청에 date 를 끼우면 같은 조합이
    # 날짜 수만큼 쪼개져 행이 28배로 터지고 rowLimit 벽에 훨씬 먼저 닿는다 —
    # 그래서 dimensions=["date"] 단독으로 따로 부른다. 응답이 창 길이(28행 남짓)라
    # 호출 1회로 끝나고 비용이 사실상 없다. 그래서 옵션이 아니라 항상 한다.
    drows = _optional("일별 추이", {
        "startDate": str(start), "endDate": str(end),
        "dimensions": ["date"], "rowLimit": days + 10, "dataState": "final"})
    if drows is not None:
        print(f"[gsc] fetched {len(drows)} daily rows")

    # device/country 분해. 응답 행의 keys 순서는 요청한 dimensions 순서와 같다 →
    # keys[0]=dim_value, keys[1]=query.
    # startRow 페이지네이션은 하지 않는다 — 분해는 "모바일이 어디서 밀리나" 같은
    # 상위 분포만 보면 되는데, 페이지를 넘기기 시작하면 호출 수가 차원 수만큼
    # 곱해져서 정작 본체(query×page)보다 비싸진다.
    bd: list[tuple[str, list]] = []
    for dim in dims:
        brows = _optional(f"{dim} 분해", {
            "startDate": str(start), "endDate": str(end),
            "dimensions": [dim, "query"], "rowLimit": PAGE, "dataState": "final"})
        if brows is None:
            continue
        bd.append((dim, brows))
        print(f"[gsc] fetched {len(brows)} {dim} x query rows")
        if len(brows) >= PAGE:
            # 페이지를 안 넘기기로 한 대가다 — 잘렸으면 잘렸다고 말한다.
            print(f"[주의] {dim} 분해가 {PAGE:,}행에서 잘렸습니다 — "
                  "노출 상위 분포만 담겼습니다.")

    snap = str(date.today())
    with db.run(conn, p["id"], "gsc") as r:
        db.write_gsc_snapshot(conn, p["id"], snap, days,
                              ((x["keys"][0], x["keys"][1], x.get("clicks"),
                                x.get("impressions"), x.get("ctr"), x.get("position"))
                               for x in rows))
        if drows is not None:
            db.write_gsc_daily(conn, p["id"],
                               ((x["keys"][0], x.get("clicks"), x.get("impressions"),
                                 x.get("ctr"), x.get("position"))
                                for x in drows))
        for dim, brows in bd:
            db.write_gsc_breakdown(conn, p["id"], snap, days, dim,
                                   ((x["keys"][0], x["keys"][1], x.get("clicks"),
                                     x.get("impressions"), x.get("ctr"), x.get("position"))
                                    for x in brows))
        r.api_calls = calls
        # notes 의 pages 는 query×page 페이지 수를 뜻했다 — 이제 호출이 세 종류라
        # calls 와 다르다. 둘을 따로 적는다. 건너뛴 축은 'skip' 으로 남긴다 —
        # 나중에 "그날 왜 추이가 비었나"를 여기서 답할 수 있어야 한다.
        r.notes = (f"rows={len(rows)} pages={qp_calls} "
                   f"daily={'skip' if drows is None else len(drows)} "
                   + "".join(f"{d}={len(br)} " for d, br in bd)
                   + "".join(f"{d}=skip " for d in dims if d not in dict(bd))
                   + f"calls={calls} window={start}~{end}")

    print(f"saved snapshot {snap}. striking-distance preview "
          "(pos 4~20, impressions desc):")
    for s in scoring.striking(conn, p["id"], snap, limit=10,
                              brands=scoring.foreign_brands(conn, p["id"], cfg)):
        print(f"  {s['pos']:>5}  imp={s['imp']:<6} clk={s['clk']:<4} "
              f"gap={s['gap']:<5} {s['query']}")
    conn.close()


if __name__ == "__main__":
    main()
