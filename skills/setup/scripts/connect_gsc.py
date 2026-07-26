#!/usr/bin/env python3
"""사이트 하나를 Google Search Console에 연결한다 (자격증명 배치 + 승인).

왜 이 스크립트가 있나 — 2026-07-26에 실제로 연결해 보며 확인한 벽들 때문:
  * 구글은 클라이언트 보안 비밀번호를 **생성 직후 그 창에서만** 준다. 놓치면
    다시 볼 수 없고, 콘솔의 `+ Add secret`으로 새로 만드는 수밖에 없다.
  * claude-in-chrome은 크롬 다운로드를 트리거하지 못한다 — JSON 다운로드 버튼은
    자동화로 못 누른다. 그래서 **복사 버튼 → 클립보드**가 유일하게 통하는 경로다.
  * 페이지에서 http://127.0.0.1 로 보내는 우회도 크롬이 막는다(Private Network
    Access). 로컬 수신기를 띄우는 방법은 시도하지 말 것.
  * OAuth 승인 창을 기본 브라우저로 띄우면 Claude가 보는 탭 밖에서 열려 조작할 수
    없다. 그래서 여기서는 URL만 찍고(open_browser=False) 통제 가능한 탭에서 연다.

비밀번호는 클립보드 → 이 프로세스 → 파일로만 흐른다. 인자로 받지 않는다
(셸 히스토리·로그·대화 기록에 남기지 않기 위해).

Usage:
  # 콘솔에서 보안 비밀번호 복사 버튼을 누른 직후:
  python connect_gsc.py --project NAME --client-id 123-abc.apps.googleusercontent.com
  python connect_gsc.py --project NAME --auth-only   # 자격증명은 이미 있고 승인만
"""
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

CAPTURE_SCRIPTS = Path(__file__).resolve().parents[2] / "capture" / "scripts"
sys.path.insert(0, str(CAPTURE_SCRIPTS))
import db  # noqa: E402
from collect_gsc import SCOPES, creds_dir  # noqa: E402  (import 시점엔 stdlib만)

SECRET_RE = re.compile(r"GOCSPX-[A-Za-z0-9_\-]{10,}")


def clipboard_text() -> str:
    if sys.platform == "win32":
        # 한국어 윈도우 기본 콘솔은 cp949 — UTF-8로 고정해야 디코딩이 안 깨진다.
        cmd = ["powershell", "-NoProfile", "-Command",
               "[Console]::OutputEncoding=[Text.Encoding]::UTF8; Get-Clipboard -Raw"]
    elif sys.platform == "darwin":
        cmd = ["pbpaste"]
    else:
        cmd = ["xclip", "-selection", "clipboard", "-o"]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return ""
    return r.stdout.decode("utf-8", "replace")


def clear_clipboard() -> None:
    try:
        if sys.platform == "win32":
            subprocess.run(["powershell", "-NoProfile", "-Command",
                            'Set-Clipboard -Value " "'], timeout=15)
        elif sys.platform == "darwin":
            subprocess.run("pbcopy < /dev/null", shell=True, timeout=15)
        else:
            subprocess.run("xclip -selection clipboard < /dev/null",
                           shell=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        pass


def write_secrets(project: str, client_id: str, force: bool) -> Path:
    dest = creds_dir(project) / "client_secrets.json"
    if dest.exists() and not force:
        sys.exit(f"{dest} 가 이미 있습니다. 덮어쓰려면 --force.")
    m = SECRET_RE.search(clipboard_text() or "")
    if not m:
        sys.exit(
            "클립보드에서 클라이언트 보안 비밀번호를 찾지 못했습니다.\n"
            "  Google Cloud 콘솔 → 인증 플랫폼 → 클라이언트 → 해당 데스크톱 앱 →\n"
            "  '클라이언트 보안 비밀번호' 줄의 복사 아이콘을 누른 뒤 다시 실행하세요.\n"
            "  (값이 마스킹돼 보이면 `+ Add secret`으로 새로 만들면 됩니다 —\n"
            "   구글은 기존 비밀번호를 다시 보여주지 않습니다.)")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps({"installed": {
        "client_id": client_id,
        "client_secret": m.group(0),
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
        "redirect_uris": ["http://localhost"],
    }}, indent=2), encoding="utf-8")
    clear_clipboard()
    print(f"[1/2] 자격증명 저장: {dest} (비밀번호 {len(m.group(0))}자, 클립보드 비움)")
    return dest


def authorize(project: str) -> None:
    try:
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        sys.exit("구글 연동 부품이 없습니다 — 먼저 실행: "
                 "pip install google-api-python-client google-auth-oauthlib")
    token = creds_dir(project) / "gsc_token.json"
    if token.exists():
        creds = Credentials.from_authorized_user_file(str(token), SCOPES)
        if creds and creds.valid:
            print(f"[2/2] 이미 연결돼 있습니다 ({token}). "
                  f"다시 하려면 이 파일을 지우세요.")
            return
    secrets = creds_dir(project) / "client_secrets.json"
    if not secrets.exists():
        sys.exit(f"자격증명이 없습니다: {secrets} — --auth-only 없이 다시 실행하세요.")
    flow = InstalledAppFlow.from_client_secrets_file(str(secrets), SCOPES)
    print("[2/2] 아래 URL을 브라우저에서 열어 승인하세요 "
          "(Claude가 대신 열어줄 수 있습니다):", flush=True)
    creds = flow.run_local_server(port=0, open_browser=False, prompt="consent")
    token.write_text(creds.to_json(), encoding="utf-8")
    print(f"\n연결 완료 — 토큰 저장: {token}\n"
          f"이제 `python ../../capture/scripts/collect_gsc.py --project {project}` "
          "로 수집하면 됩니다.")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True, help="capture에 등록된 사이트 이름")
    ap.add_argument("--client-id", help="데스크톱 앱 클라이언트 ID")
    ap.add_argument("--auth-only", action="store_true",
                    help="자격증명은 그대로 두고 승인만 다시")
    ap.add_argument("--force", action="store_true", help="기존 자격증명 덮어쓰기")
    a = ap.parse_args()

    conn = db.connect()
    db.get_project(conn, a.project)   # 미등록이면 여기서 안내하고 멈춘다
    conn.close()

    if not a.auth_only:
        if not a.client_id:
            sys.exit("--client-id 가 필요합니다 (콘솔 클라이언트 상세의 '클라이언트 ID').")
        if not a.client_id.endswith(".apps.googleusercontent.com"):
            sys.exit(f"클라이언트 ID 형식이 아닙니다: {a.client_id}")
        write_secrets(a.project, a.client_id, a.force)
    authorize(a.project)


if __name__ == "__main__":
    main()
