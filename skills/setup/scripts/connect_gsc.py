#!/usr/bin/env python3
"""GSC 연결 — 받은 구글 인증 파일을 제자리에 설치한다 (stdlib 전용).

두 방식을 다 받는다. 어느 쪽을 받았는지는 파일을 열어 보고 판별하므로 사용자가
고를 필요가 없다:

  * **OAuth 클라이언트(기본)** — 내 구글 계정으로 로그인해서 쓴다. 로그인 한 번이면
    내가 소유한 속성이 전부 붙는다. 속성마다 사용자를 추가하는 단계가 없다.
  * 서비스 계정 키(무인 수집용) — 만료가 없어 자동 실행에 좋지만, 속성마다
    그 이메일을 '사용자 및 권한'에 추가해야 한다.

설치된 파일은
  * gsc MCP 서버(플러그인 .mcp.json)  — Claude가 서치콘솔을 실시간 조회
  * capture/scripts/collect_gsc.py    — Brain으로 벌크 수집
가 같이 쓴다. 인증은 한 벌이다 (판정 규칙은 db.gsc_auth()가 정본).

브라우저 자동화가 크롬 다운로드를 트리거하지 못하므로(2026-07-26 실측) 다운로드
버튼만 사용자가 직접 누르고, 이 스크립트가 다운로드 폴더에서 회수한다.

Usage:
  python connect_gsc.py               # 다운로드 폴더에서 최신 인증 파일을 찾아 설치
  python connect_gsc.py --from PATH   # 파일 경로를 직접 지정
  python connect_gsc.py --status      # 지금 걸린 인증과 다음 할 일 확인
"""
import argparse
import json
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
    return db.gsc_oauth_client() if k == "oauth" else db.gsc_key()


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
        print("\n남은 일 — 없습니다. 처음 조회할 때 브라우저가 한 번 열리니 "
              "구글 계정으로 로그인만 하시면 됩니다.")
        print("  (그때 받은 토큰을 즉석 조회와 벌크 수집이 같이 씁니다. "
              "속성마다 권한을 주는 단계는 없습니다 — 이미 내 계정 소유입니다.)")
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


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="src",
                    help="인증 JSON 경로 (생략하면 다운로드 폴더에서 찾음)")
    ap.add_argument("--status", action="store_true", help="지금 걸린 인증 확인만")
    ap.add_argument("--force", action="store_true", help="기존 파일 덮어쓰기")
    a = ap.parse_args()

    if a.status:
        mode = db.gsc_auth()
        if mode:
            finish(mode)
            return
        sys.exit(f"아직 연결되지 않았습니다.\n"
                 f"  기본(OAuth): {db.gsc_oauth_client()}\n"
                 f"  무인 수집용: {db.gsc_key()}\n"
                 "  둘 중 하나를 구글 클라우드에서 받은 뒤 이 스크립트를 다시 "
                 "실행하세요 (capture/references/setup.md 4-B).")

    if a.src:
        src = Path(a.src)
        k = kind(src) if src.exists() else ""
        if not k:
            sys.exit(f"{src} 는 OAuth 클라이언트도 서비스 계정 키도 아닙니다.")
    else:
        src, k = find_downloaded()
        if not src:
            sys.exit("다운로드 폴더에서 구글 인증 파일(JSON)을 찾지 못했습니다.\n"
                     "  기본(OAuth): 구글 클라우드 → API 및 서비스 → 사용자 인증 정보 →\n"
                     "    'OAuth 클라이언트 ID 만들기' → 데스크톱 앱 → JSON 다운로드\n"
                     "  또는 --from <경로> 로 직접 지정하세요.")

    dest = dest_for(k)
    if dest.exists() and not a.force:
        sys.exit(f"{dest} 가 이미 있습니다. 바꾸려면 --force.")
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dest))
    print(f"[설치] {src.name} → {dest} (다운로드 폴더에서는 제거됨)")
    finish(k)


if __name__ == "__main__":
    main()
