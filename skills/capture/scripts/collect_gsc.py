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
  python collect_gsc.py --project NAME [--days 28] [--row-limit 25000] [--dry-run]
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


def main() -> None:
    ap = argparse.ArgumentParser()
    collector.add_common(ap)
    collector.add_setting(ap, "--days", key="gsc_days", fallback=28, type=int)
    ap.add_argument("--row-limit", type=int, default=100000,
                    help="가져올 최대 행 수 (25,000행씩 나눠 받는다)")
    a = ap.parse_args()

    conn, p, cfg = collector.open_project(a.project)
    s = collector.settings(a, cfg)
    days = s["gsc_days"]
    prop = p["gsc_property"]
    if not prop:
        sys.exit("project yaml has no gsc_property "
                 "(e.g. 'sc-domain:example.com' or 'https://example.com/'). "
                 "Add it, then: python db.py sync-project <yaml>")

    end = date.today() - timedelta(days=3)     # GSC delay buffer
    start = end - timedelta(days=days)
    print(f"[gsc] {prop}  window {start} ~ {end} (period={days}d) "
          f"rowLimit={a.row_limit}")
    if a.dry_run:
        conn.close()
        return

    service = get_service(a.project)
    from googleapiclient.errors import HttpError
    rows: list = []
    calls = 0
    try:
        # API는 한 번에 최대 PAGE행만 준다 — 그 이상은 startRow로 이어받아야 한다.
        # 예전엔 한 번만 불러서, 큰 사이트가 정확히 25,000행에서 조용히 잘렸다.
        while len(rows) < a.row_limit:
            want = min(PAGE, a.row_limit - len(rows))
            resp = service.searchanalytics().query(siteUrl=prop, body={
                "startDate": str(start), "endDate": str(end),
                "dimensions": ["query", "page"], "rowLimit": want,
                "startRow": len(rows), "dataState": "final"}).execute()
            batch = resp.get("rows", [])
            rows += batch
            calls += 1
            if len(batch) < want:          # 마지막 장 — 더 없다
                break
            print(f"[gsc] … {len(rows)} rows")
    except HttpError as e:
        if getattr(e.resp, "status", None) == 403 and db.gsc_key().exists():
            email = json.loads(db.gsc_key().read_text(encoding="utf-8")) \
                .get("client_email", "?")
            sys.exit(f"권한 거부: {prop}\n"
                     "  Search Console → 설정 → 사용자 및 권한에 서비스 계정 "
                     f"이메일을 추가했는지 확인하세요: {email}\n"
                     "  (권한 '제한된 사용자'면 충분합니다)")
        raise
    print(f"[gsc] fetched {len(rows)} query x page rows")
    if len(rows) >= a.row_limit:
        print(f"[주의] --row-limit({a.row_limit})에 걸려 여기서 멈췄습니다 — "
              "더 필요하면 값을 올리세요. 지금 스냅샷은 전체가 아닙니다.")

    snap = str(date.today())
    with db.run(conn, p["id"], "gsc") as r:
        db.write_gsc_snapshot(conn, p["id"], snap, days,
                              ((x["keys"][0], x["keys"][1], x.get("clicks"),
                                x.get("impressions"), x.get("ctr"), x.get("position"))
                               for x in rows))
        r.api_calls = calls
        r.notes = f"rows={len(rows)} pages={calls} window={start}~{end}"

    print(f"saved snapshot {snap}. striking-distance preview "
          "(pos 4~20, impressions desc):")
    for s in scoring.striking(conn, p["id"], snap, limit=10,
                              brands=scoring.foreign_brands(conn, p["id"], cfg)):
        print(f"  {s['pos']:>5}  imp={s['imp']:<6} clk={s['clk']:<4} "
              f"gap={s['gap']:<5} {s['query']}")
    conn.close()


if __name__ == "__main__":
    main()
