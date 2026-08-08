#!/usr/bin/env python3
"""Sync Google Search Console data into the Brain (free, real measurements).

Known holes (read reports accordingly, see references/scoring.md):
  * ~2-3 day data delay; low-volume longtail queries are privacy-filtered out
  * position is an AVERAGE across impressions, not a pinpoint rank

Auth: 서비스 계정 키가 표준이다 (전 사이트 공용 1개 — gsc MCP 서버와 같은 키):
  $GOOGLE_APPLICATION_CREDENTIALS > $CAPTURE_HOME/gsc_service_account.json
  설치: python ../../setup/scripts/connect_gsc.py  (setup.md 4-B)
키가 없으면 구버전 사이트별 OAuth(creds/{project}/client_secrets.json + gsc_token.json)
로 동작한다 — 이미 붙여둔 사이트용 하위호환이고, 새 연결은 전부 서비스 계정으로 한다.
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


def creds_dir(project: str) -> Path:
    return db.CAPTURE_HOME / "creds" / project


def sa_key() -> Path:
    return Path(os.environ.get("GOOGLE_APPLICATION_CREDENTIALS",
                               str(db.CAPTURE_HOME / "gsc_service_account.json")))


def client_id_of(path: Path) -> str:
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return ""
    return (cfg.get("installed") or cfg.get("web") or {}).get("client_id", "")


def client_owner(path: Path, exclude: str) -> str:
    """이 클라이언트를 이미 쓰고 있는 다른 사이트 이름 (없으면 빈 문자열)."""
    cid = client_id_of(path)
    if not cid:
        return ""
    for other in sorted((db.CAPTURE_HOME / "creds").glob("*/client_secrets.json")):
        if other.parent.name != exclude and client_id_of(other) == cid:
            return other.parent.name
    return ""


def resolve_secrets(project: str) -> str:
    """이 사이트가 쓸 client_secrets.json 을 고른다.

    사이트별 자격증명이 기본이다 — creds/{project}/ 아래에 각자 두므로 한 사이트의
    구글 클라이언트가 다른 사이트의 Search Console을 대신 열지 않는다.
    공용 파일을 두면 그걸 쓰지만, 그건 명시적으로 선택했을 때뿐이다.
    """
    env = os.environ.get("GSC_CLIENT_SECRETS")
    if env:
        return env
    own = creds_dir(project) / "client_secrets.json"
    if own.exists():
        return str(own)
    shared = db.CAPTURE_HOME / "client_secrets.json"
    if shared.exists():
        print(f"[안내] 사이트 전용 자격증명이 없어 공용 {shared.name} 을 씁니다. "
              f"분리하려면 이 사이트용 JSON을 {own} 에 두세요.")
        return str(shared)
    # 다운로드 폴더에서 방금 받은 걸 이 사이트 전용 자리에 넣어준다.
    dl = Path(os.environ.get("DOWNLOADS_DIR", Path.home() / "Downloads"))
    for c in sorted(dl.glob("client_secret*.json"),
                    key=lambda p: -p.stat().st_mtime)[:10]:
        try:
            kind = set(json.loads(c.read_text(encoding="utf-8")))
        except (ValueError, OSError):
            continue
        if "installed" in kind:                  # 데스크톱 앱 = 우리가 원하는 유형
            used_by = client_owner(c, exclude=project)
            if used_by:
                # 같은 클라이언트를 몰래 복제하면 사이트 분리가 무의미해진다.
                print(f"[주의] {c.name} 은 이미 '{used_by}' 사이트가 쓰는 "
                      f"클라이언트입니다. {project} 용으로는 두 가지 중 하나:\n"
                      f"  · 이 사이트 전용 데스크톱 클라이언트를 새로 만들어 받기 "
                      "(권장 — 사이트별 분리 유지)\n"
                      f"  · 일부러 공용으로 쓰려면 그 JSON을 "
                      f"{db.CAPTURE_HOME / 'client_secrets.json'} 에 두기")
                continue
            own.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(c, own)
            print(f"[자동] 다운로드 폴더의 {c.name} 을 {own} 로 옮겼습니다 "
                  f"({project} 전용).")
            return str(own)
        if "web" in kind:
            print(f"[주의] {c.name} 은 '웹 애플리케이션' 유형입니다 — "
                  "Search Console 연동에는 '데스크톱 앱' 클라이언트가 필요합니다.")
    return str(own)


def get_service(project: str):
    try:
        from googleapiclient.discovery import build
    except ImportError:
        sys.exit("구글 연동 부품이 없습니다 — 먼저 실행: "
                 "pip install google-api-python-client")

    key = sa_key()
    if key.exists():
        from google.oauth2 import service_account
        creds = service_account.Credentials.from_service_account_file(
            str(key), scopes=SCOPES)
        return build("searchconsole", "v1", credentials=creds,
                     cache_discovery=False)

    # ---- 이하 구버전 OAuth 경로 (이미 붙여둔 사이트의 토큰 재사용 전용) ----
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        sys.exit(f"서비스 계정 키가 없습니다: {key}\n"
                 "  연결(5분, 전 사이트 공용): "
                 "python ../../setup/scripts/connect_gsc.py  (setup.md 4-B)")

    secrets = resolve_secrets(project)
    token_path = creds_dir(project) / "gsc_token.json"
    legacy = db.CAPTURE_HOME / "gsc_token.json"
    if not token_path.exists() and legacy.exists() \
            and secrets == str(db.CAPTURE_HOME / "client_secrets.json"):
        # 예전 공용 토큰은 공용 자격증명을 계속 쓸 때만 재사용 (불필요한 재인증 방지)
        token_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(legacy, token_path)
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
                    "구글 연결이 없습니다. 지금 표준은 서비스 계정 방식입니다 — \n"
                    "  키 1개로 모든 사이트: python ../../setup/scripts/connect_gsc.py "
                    "(setup.md 4-B, 5분)")
            print(f"[인증] {project} 전용 구글 승인 창을 엽니다 — "
                  "이 사이트의 Search Console 속성만 읽습니다.")
            flow = InstalledAppFlow.from_client_secrets_file(secrets, SCOPES)
            creds = flow.run_local_server(port=0)
        token_path.parent.mkdir(parents=True, exist_ok=True)
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

    service = get_service(a.project)
    body = {"startDate": str(start), "endDate": str(end),
            "dimensions": ["query", "page"], "rowLimit": a.row_limit,
            "dataState": "final"}
    from googleapiclient.errors import HttpError
    try:
        resp = service.searchanalytics().query(siteUrl=prop, body=body).execute()
    except HttpError as e:
        if getattr(e.resp, "status", None) == 403 and sa_key().exists():
            email = json.loads(sa_key().read_text(encoding="utf-8")) \
                .get("client_email", "?")
            sys.exit(f"권한 거부: {prop}\n"
                     "  Search Console → 설정 → 사용자 및 권한에 서비스 계정 "
                     f"이메일을 추가했는지 확인하세요: {email}\n"
                     "  (권한 '제한된 사용자'면 충분합니다)")
        raise
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
