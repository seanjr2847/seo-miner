#!/usr/bin/env python3
"""GSC 연결 — 인증을 제자리에 설치하고, 지금 상태를 말한다 (stdlib 전용).

**기본 경로에는 이 스크립트가 필요 없다.** 플러그인이 OAuth 클라이언트를 동봉하므로
(skills/setup/oauth_client.json) 사용자는 콘솔에 갈 일이 없고, 수집을 한 번 돌릴 때
열리는 브라우저에서 **구글 로그인 한 번**만 하면 된다.

이 스크립트가 필요한 경우는 셋뿐이다:

  * 자기 OAuth 클라이언트를 쓰고 싶을 때 — `--client-id/--client-secret` 로 조립하거나
    콘솔에서 받은 JSON 을 `--from` 으로 설치한다(번들과 달리 '확인되지 않은 앱' 경고와
    사용자 100명 상한이 없다).
  * 서비스 계정 키(무인 수집용) — 만료가 없어 자동 실행에 좋지만, 속성마다 그 이메일을
    '사용자 및 권한'에 추가해야 한다.
  * `--status` — 지금 연결됐는지 확인.

설치된 파일은
  * gsc MCP 서버(플러그인 .mcp.json)  — Claude가 서치콘솔을 실시간 조회
  * capture/scripts/collect_gsc.py    — Brain으로 벌크 수집
가 같이 쓴다. 인증은 한 벌이다 (판정 규칙은 db.gsc_auth()/db.gsc_connected()가 정본).

브라우저 자동화가 크롬 다운로드를 트리거하지 못하므로(2026-07-26 실측) 다운로드
버튼만 사용자가 직접 누르고, 이 스크립트가 다운로드 폴더에서 회수한다.

Usage:
  python connect_gsc.py --status      # 연결됨 / 로그인 대기 / 인증 없음
  python connect_gsc.py               # 다운로드 폴더에서 최신 인증 파일을 찾아 설치
  python connect_gsc.py --from PATH   # 파일 경로를 직접 지정
  python connect_gsc.py --client-id ID --client-secret SECRET   # 콘솔 화면의 두 문자열로 조립
"""
import argparse
import json
import os
import shutil
import sys
from pathlib import Path

# 파일이 어디 놓이고 다운로드 폴더가 어디인지는 db가 답한다 — 여기서 다시 계산하면
# 대시보드·collect_gsc와 어긋난 자리에 깐다. 콘솔 인코딩도 db가 맞춘다.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "capture" / "scripts"))
import db  # noqa: E402


def kind(path: Path) -> str:
    """이 JSON이 무엇인가 — "oauth" | "service_account" | "" (둘 다 아님)."""
    try:
        d = json.loads(Path(path).read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return ""
    if d.get("type") == "service_account" and d.get("client_email"):
        return "service_account"
    # OAuth 클라이언트 시크릿은 installed(데스크톱) 또는 web 아래에 client_id를 둔다.
    for k in ("installed", "web"):
        if isinstance(d.get(k), dict) and d[k].get("client_id"):
            return "oauth"
    return ""


def sa_email(path: Path) -> str:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8")).get("client_email", "")
    except (ValueError, OSError):
        return ""


def dest_for(k: str) -> Path:
    """설치할 자리. **db.gsc_oauth_client() 를 그대로 쓰면 안 된다** — 그 함수는
    "지금 쓰이는 것"을 답하므로 사용자 파일이 없으면 **번들 경로**를 가리킨다.
    거기에 설치하면 플러그인 안쪽(업데이트하면 날아가는 자리)에 깔린다.
    사용자가 놓는 것은 언제나 ~/.capture — 단, 환경변수로 자리를 지정했으면 그쪽.
    """
    if k != "oauth":
        return db.gsc_key()
    env = os.environ.get("GSC_OAUTH_CLIENT_SECRETS_FILE")
    return Path(env) if env else db.CAPTURE_HOME / "gsc_oauth_client.json"


def assemble(client_id: str, client_secret: str) -> dict:
    """콘솔 화면의 두 문자열만으로 클라이언트 JSON 을 조립한다 (다운로드 없이).

    형태의 정본은 번들(skills/setup/oauth_client.json)이다 — 있으면 그걸 본떠서
    엔드포인트가 바뀌어도 고칠 곳이 한 군데로 남는다. 번들이 빠진 배포에서도
    이 경로는 살아야 하므로(그때가 오히려 자기 클라이언트가 필요한 상황이다)
    구글 데스크톱 앱 기본값을 폴백으로 둔다.
    """
    body = {
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
        "redirect_uris": ["http://localhost"],
    }
    bundled = db.gsc_oauth_bundled()
    if bundled.exists():
        try:
            body = dict(json.loads(bundled.read_text(encoding="utf-8"))["installed"])
        except (ValueError, OSError, KeyError, TypeError):
            pass
    body["client_id"] = client_id
    body["client_secret"] = client_secret
    return {"installed": body}


def find_downloaded():
    """다운로드 폴더에서 최신 인증 파일 하나 — (경로, 종류). 없으면 (None, "").

    OAuth가 기본이라 같은 시각대에 둘 다 있으면 OAuth를 고른다.
    """
    found = []
    for c in sorted(db.downloads_dir().glob("*.json"),
                    key=lambda p: -p.stat().st_mtime)[:15]:
        k = kind(c)
        if k:
            found.append((c, k))
    for c, k in found:
        if k == "oauth":
            return c, k
    return found[0] if found else (None, "")


def gsc_properties() -> list:
    """등록된 사이트들의 (이름, gsc_property) — Brain에게 묻는다.

    예전엔 projects/*.yaml을 줄 단위로 훑었는데, 대시보드 폼으로 등록해 yaml이 없는
    사이트는 그 목록에 영영 안 나왔다. "내 사이트가 뭐냐"의 답은 doctor와 같은 곳에서.
    """
    if not db.DB_PATH.exists():   # 조회 하나 때문에 빈 Brain을 만들지는 않는다
        return []
    try:
        conn = db.connect_ro()
        rows = conn.execute(
            "SELECT name, gsc_property FROM projects ORDER BY name").fetchall()
        conn.close()
    except Exception:   # Brain이 아직 없거나 손상 — 설치는 이미 끝났으니 일반 안내로
        return []
    return [(r["name"], r["gsc_property"]) for r in rows if r["gsc_property"]]


def finish(k: str) -> None:
    if k == "oauth":
        print(f"\n설치됨: {db.gsc_oauth_client()}  (방식: 내 구글 계정으로 로그인)")
        print("\n남은 일 — 구글 로그인 한 번. 처음 조회할 때 브라우저가 열립니다.")
        print("  (그때 받은 토큰을 즉석 조회와 벌크 수집이 같이 씁니다. "
              "속성마다 권한을 주는 단계는 없습니다 — 이미 내 계정 소유입니다.)")
        print("  로그인이 끝났는지는 connect_gsc.py --status 가 답합니다 "
              "— 파일이 있다고 연결된 게 아닙니다.")
    else:
        email = sa_email(db.gsc_key())
        print(f"\n설치됨: {db.gsc_key()}  (방식: 서비스 계정 — 무인 수집용)")
        print(f"서비스 계정 이메일: {email}")
        print("\n남은 일 — Search Console에서 이 이메일에 읽기 권한 주기 (속성마다 1번):")
        props = gsc_properties()
        if props:
            for name, prop in props:
                print(f"  - {name}: https://search.google.com/search-console/users"
                      f"?resource_id={prop}")
        else:
            print("  - https://search.google.com/search-console → 속성 선택 → 설정 → "
                  "사용자 및 권한")
        print("    → '사용자 추가' → 위 이메일 → 권한 '제한된 사용자'(읽기는 이걸로 충분)")
    print("\n그 다음:")
    print("  - 새 Claude 세션부터 gsc MCP 툴(get_search_analytics 등)로 즉석 조회 가능")
    print("  - 벌크 수집: python ../../capture/scripts/collect_gsc.py --project <사이트>")


def status() -> None:
    """3-상태 — 연결됨 / 로그인 대기 / 인증 없음.

    **"파일이 있다"로 말하던 예전 판정은 틀렸다.** 번들 클라이언트는 설치만 하면
    항상 존재하므로, 그걸로 판정하면 아직 한 번도 로그인하지 않은 사람에게까지
    "연결됨"이라고 말하게 된다 — 이 저장소가 MCP `Connected` 표시에 이미 당한
    거짓말과 같은 것이다. 진짜 판정은 db.gsc_connected() (= 토큰이 있느냐).
    """
    mode = db.gsc_auth()
    if not mode:
        sys.exit("[인증 없음] OAuth 클라이언트가 없습니다 — 번들이 빠진 배포입니다.\n"
                 f"  기대한 자리: {db.gsc_oauth_bundled()}\n"
                 "  해결(하나 고르세요):\n"
                 "    - 자기 클라이언트 조립: connect_gsc.py --client-id ID "
                 "--client-secret SECRET\n"
                 "      (구글 클라우드 → API 및 서비스 → 사용자 인증 정보 → "
                 "OAuth 클라이언트 ID → 데스크톱 앱)\n"
                 "    - 받은 JSON 설치: connect_gsc.py --from <경로>\n"
                 f"    - 무인 수집이면 서비스 계정 키를 {db.gsc_key()} 에")

    if mode == "service_account":
        # 서비스 계정은 로그인이 없어 키 파일이 곧 연결이다. 남은 건 속성별 권한 —
        # finish()가 그 목록(속성별 URL)을 이미 만든다.
        print("[연결됨] 방식: 서비스 계정 (무인 수집)")
        finish("service_account")
        return

    client = db.gsc_oauth_client()
    bundled = client == db.gsc_oauth_bundled()
    origin = ("플러그인 동봉 — 콘솔 작업 0" if bundled else "직접 설치한 내 클라이언트")
    if db.gsc_connected():
        print("[연결됨] 구글 로그인이 끝났습니다 — 수집 가능합니다.")
        print(f"  클라이언트: {client}  ({origin})")
        print(f"  로그인 토큰: {db.gsc_token()}")
        print("\n  - 즉석 조회: gsc MCP 툴(get_search_analytics 등)")
        print("  - 벌크 수집: python ../../capture/scripts/collect_gsc.py "
              "--project <사이트>")
        return

    print("[로그인 대기] 구글 로그인 한 번만 하면 됩니다. 콘솔 작업은 없습니다.")
    print(f"  클라이언트: {client}  ({origin})")
    print(f"  아직 없는 것: 로그인 토큰 ({db.gsc_token()})")
    print("\n다음 단계: 아무 수집이나 한 번 돌리면 브라우저 로그인이 열립니다.")
    print("  python ../../capture/scripts/collect_gsc.py --project <사이트>")
    if bundled:
        print("  (동의 화면에 '확인되지 않은 앱' 경고가 한 번 뜹니다 — 고급 → 이동.")
        print("   경고 없이 쓰려면 자기 클라이언트를: --client-id ID "
              "--client-secret SECRET)")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="src",
                    help="인증 JSON 경로 (생략하면 다운로드 폴더에서 찾음)")
    ap.add_argument("--status", action="store_true",
                    help="연결됨 / 로그인 대기 / 인증 없음")
    ap.add_argument("--client-id", help="자기 OAuth 클라이언트 ID (--client-secret 과 한 쌍)")
    ap.add_argument("--client-secret", help="자기 OAuth 클라이언트 시크릿 (출력하지 않음)")
    ap.add_argument("--force", action="store_true", help="기존 파일 덮어쓰기")
    a = ap.parse_args()

    if a.status:
        status()
        return

    if a.client_id or a.client_secret:
        # JSON 다운로드 없이 콘솔 화면의 두 문자열만으로 자기 클라이언트를 쓰는 길.
        if not (a.client_id and a.client_secret):
            sys.exit(f"{'--client-secret' if a.client_id else '--client-id'} 가 "
                     "빠졌습니다 — 둘 다 있어야 클라이언트를 조립할 수 있습니다.")
        dest = dest_for("oauth")
        if dest.exists() and not a.force:
            sys.exit(f"{dest} 가 이미 있습니다. 바꾸려면 --force.")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(json.dumps(assemble(a.client_id, a.client_secret), indent=2),
                        encoding="utf-8")
        # 시크릿은 절대 찍지 않는다. client_id 도 확인용으로 앞부분만.
        print(f"[조립] 내 OAuth 클라이언트 → {dest}")
        print(f"  client_id: {a.client_id[:12]}…  (client_secret 은 출력하지 않습니다)")
        finish("oauth")
        return

    if a.src:
        src = Path(a.src)
        k = kind(src) if src.exists() else ""
        if not k:
            sys.exit(f"{src} 는 OAuth 클라이언트도 서비스 계정 키도 아닙니다.")
    else:
        src, k = find_downloaded()
        if not src:
            sys.exit("다운로드 폴더에서 구글 인증 파일(JSON)을 찾지 못했습니다.\n"
                     "  기본 경로에는 이 스크립트가 필요 없습니다 — 플러그인이 OAuth\n"
                     "  클라이언트를 동봉하므로 수집을 한 번 돌릴 때 브라우저에서\n"
                     "  구글 로그인만 하면 됩니다 (지금 상태: --status).\n"
                     "  자기 클라이언트를 쓰려면:\n"
                     "    - --client-id ID --client-secret SECRET  (콘솔 화면의 두 문자열)\n"
                     "    - 또는 콘솔에서 받은 JSON 을 --from <경로> 로 지정")

    dest = dest_for(k)
    if dest.exists() and not a.force:
        sys.exit(f"{dest} 가 이미 있습니다. 바꾸려면 --force.")
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dest))
    print(f"[설치] {src.name} → {dest} (다운로드 폴더에서는 제거됨)")
    finish(k)


if __name__ == "__main__":
    main()
