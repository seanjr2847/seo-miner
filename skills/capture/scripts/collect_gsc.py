#!/usr/bin/env python3
"""Sync Google Search Console data into the Brain (free, real measurements).

Known holes (read reports accordingly, see references/scoring.md):
  * ~2-3 day data delay; low-volume longtail queries are privacy-filtered out
  * position is an AVERAGE across impressions, not a pinpoint rank

Auth: 서비스 계정 키만 쓴다 (전 사이트 공용 1개 — gsc MCP 서버와 같은 키):
  $GOOGLE_APPLICATION_CREDENTIALS > $CAPTURE_HOME/gsc_service_account.json
  설치: python ../../setup/scripts/connect_gsc.py  (setup.md 4-B)
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


def get_service(project: str):
    try:
        from googleapiclient.discovery import build
    except ImportError:
        sys.exit("구글 연동 부품이 없습니다 — 먼저 실행: "
                 "pip install google-api-python-client")

    key = db.gsc_key()
    if not key.exists():
        sys.exit(f"서비스 계정 키가 없습니다: {key}\n"
                 "  연결(5분, 전 사이트 공용): "
                 "python ../../setup/scripts/connect_gsc.py  (setup.md 4-B)")
    from google.oauth2 import service_account
    creds = service_account.Credentials.from_service_account_file(
        str(key), scopes=SCOPES)
    return build("searchconsole", "v1", credentials=creds,
                 cache_discovery=False)


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
