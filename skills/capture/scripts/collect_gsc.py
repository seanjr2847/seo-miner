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
import os
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import db  # noqa: E402

SCOPES = ["https://www.googleapis.com/auth/webmasters.readonly"]


def get_service():
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError:
        sys.exit("missing deps: pip install google-api-python-client google-auth-oauthlib")

    token_path = db.CAPTURE_HOME / "gsc_token.json"
    secrets = os.environ.get("GSC_CLIENT_SECRETS",
                             str(db.CAPTURE_HOME / "client_secrets.json"))
    creds = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not Path(secrets).exists():
                sys.exit(f"client secrets not found: {secrets} (see references/setup.md)")
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
