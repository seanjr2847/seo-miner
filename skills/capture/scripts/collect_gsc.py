#!/usr/bin/env python3
"""Sync Google Search Console data into the Brain (free, real measurements).

Known holes (read reports accordingly, see references/scoring.md):
  * ~2-3 day data delay; low-volume longtail queries are privacy-filtered out
  * position is an AVERAGE across impressions, not a pinpoint rank

Auth: OAuth installed-app flow.
  client secrets: $GSC_CLIENT_SECRETS or $CAPTURE_HOME/client_secrets.json
  cached token:   $CAPTURE_HOME/gsc_token.json
Setup walkthrough: references/setup.md

Usage:
  python collect_gsc.py --project NAME [--days 28] [--row-limit 25000] [--dry-run]
"""
import argparse
import json
import os
import shutil
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import db  # noqa: E402

SCOPES = ["https://www.googleapis.com/auth/webmasters.readonly"]


def resolve_secrets() -> str:
    """client_secrets.json 위치를 정한다. 사용자가 파일을 직접 옮기지 않아도 되게,
    다운로드 폴더에 있는 구글이 준 파일을 찾아 제자리에 넣어준다."""
    env = os.environ.get("GSC_CLIENT_SECRETS")
    if env:
        return env
    dest = db.CAPTURE_HOME / "client_secrets.json"
    if dest.exists():
        return str(dest)
    dl = Path(os.environ.get("DOWNLOADS_DIR", Path.home() / "Downloads"))
    for c in sorted(dl.glob("client_secret*.json"),
                    key=lambda p: -p.stat().st_mtime)[:10]:
        try:
            kind = set(json.loads(c.read_text(encoding="utf-8")))
        except (ValueError, OSError):
            continue
        if "installed" in kind:                  # 데스크톱 앱 = 우리가 원하는 유형
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(c, dest)
            print(f"[자동] 다운로드 폴더의 {c.name} 을 {dest} 로 옮겼습니다.")
            return str(dest)
        if "web" in kind:
            print(f"[주의] {c.name} 은 '웹 애플리케이션' 유형입니다 — "
                  "Search Console 연동에는 '데스크톱 앱' 클라이언트가 필요합니다.")
    return str(dest)


def get_service():
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError:
        sys.exit("구글 연동 부품이 없습니다 — 먼저 실행: "
                 "pip install google-api-python-client google-auth-oauthlib")

    token_path = db.CAPTURE_HOME / "gsc_token.json"
    secrets = resolve_secrets()
    creds = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    if not creds or not creds.valid:
        refreshed = False
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                refreshed = True
            except Exception as e:  # 만료·취소된 토큰이면 조용히 재인증으로 넘어간다
                print(f"[안내] 저장된 구글 인증이 만료됐습니다({type(e).__name__}) — "
                      "브라우저를 열어 다시 승인받습니다.")
                token_path.unlink(missing_ok=True)
        if not refreshed:
            if not Path(secrets).exists():
                sys.exit(
                    f"구글 인증 파일이 없습니다: {secrets}\n"
                    "  Google Cloud 콘솔 → Google 인증 플랫폼 → 클라이언트 → "
                    "'클라이언트 만들기' → 유형 '데스크톱 앱' → JSON 다운로드.\n"
                    "  다운로드 폴더에 두면 다음 실행 때 알아서 가져옵니다 "
                    "(자세히: references/setup.md 4-B).")
            flow = InstalledAppFlow.from_client_secrets_file(secrets, SCOPES)
            creds = flow.run_local_server(port=0)
        token_path.write_text(creds.to_json())
    return build("searchconsole", "v1", credentials=creds, cache_discovery=False)


def preview(conn, project_id: int, snap: str) -> None:
    """조금만 밀면 1페이지 갈 키워드(평균 4~20위) 미리보기. CSV 경로에서도 재사용."""
    print("saved snapshot %s. striking-distance preview "
          "(pos 4~20, impressions desc):" % snap)
    for r in conn.execute(
        """SELECT query, ROUND(AVG(position),1) pos, SUM(impressions) imp, SUM(clicks) clk
             FROM gsc_snapshots WHERE project_id=? AND snapshot_date=?
            GROUP BY query HAVING pos BETWEEN 4 AND 20
            ORDER BY imp DESC LIMIT 10""", (project_id, snap)):
        print(f"  {r['pos']:>5}  imp={r['imp']:<6} clk={r['clk']:<4} {r['query']}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    ap.add_argument("--days", type=int, default=28)
    ap.add_argument("--row-limit", type=int, default=25000)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    conn = db.connect()
    p = db.get_project(conn, a.project)
    prop = p["gsc_property"]
    if not prop:
        sys.exit("project yaml has no gsc_property "
                 "(e.g. 'sc-domain:example.com' or 'https://example.com/'). "
                 "Add it, then: python db.py sync-project <yaml>")

    end = date.today() - timedelta(days=3)     # GSC delay buffer
    start = end - timedelta(days=a.days)
    print(f"[gsc] {prop}  window {start} ~ {end} (period={a.days}d) "
          f"rowLimit={a.row_limit}")
    if a.dry_run:
        return

    service = get_service()
    body = {"startDate": str(start), "endDate": str(end),
            "dimensions": ["query", "page"], "rowLimit": a.row_limit,
            "dataState": "final"}
    resp = service.searchanalytics().query(siteUrl=prop, body=body).execute()
    rows = resp.get("rows", [])
    print(f"[gsc] fetched {len(rows)} query x page rows")

    run_id = db.start_run(conn, p["id"], "gsc")
    snap = str(date.today())
    conn.execute(  # idempotent per day: re-run replaces today's snapshot
        "DELETE FROM gsc_snapshots WHERE project_id=? AND snapshot_date=?",
        (p["id"], snap))
    for r in rows:
        q, page = r["keys"][0], r["keys"][1]
        conn.execute(
            """INSERT INTO gsc_snapshots(project_id, snapshot_date, period_days,
                 query, page, clicks, impressions, ctr, position)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (p["id"], snap, a.days, q, page,
             int(r.get("clicks", 0)), int(r.get("impressions", 0)),
             round(float(r.get("ctr", 0)), 4), round(float(r.get("position", 0)), 1)))
    conn.commit()
    db.finish_run(conn, run_id, api_calls=1, notes=f"rows={len(rows)} window={start}~{end}")
    preview(conn, p["id"], snap)
    conn.close()


if __name__ == "__main__":
    main()
