#!/usr/bin/env python3
"""GSC 연결 — 서비스 계정 키 1개를 설치한다 (전 사이트 공용, stdlib 전용).

예전 방식(사이트별 OAuth 데스크톱 클라이언트)은 동의 화면 게시·비밀번호 복사·
7일 만료 같은 벽이 많아 서비스 계정으로 교체했다. 키 하나를
  * gsc MCP 서버(플러그인 .mcp.json)  — Claude가 서치콘솔을 실시간 조회
  * capture/scripts/collect_gsc.py    — Brain으로 벌크 수집
가 같이 쓴다. 이미 OAuth로 붙여둔 사이트의 토큰은 collect_gsc.py가 계속 인식한다.

브라우저 자동화가 크롬 다운로드를 트리거하지 못하므로(2026-07-26 실측) 키 JSON의
다운로드 버튼만 사용자가 직접 누르고, 이 스크립트가 다운로드 폴더에서 회수한다.

Usage:
  python connect_gsc.py               # 다운로드 폴더에서 최신 서비스 계정 키를 찾아 설치
  python connect_gsc.py --from PATH   # 키 파일 경로를 직접 지정
  python connect_gsc.py --status      # 설치된 키의 서비스 계정 이메일·다음 할 일 확인
"""
import argparse
import json
import shutil
import sys
from pathlib import Path

# 키가 어디 놓이고 다운로드 폴더가 어디인지는 db가 답한다 — 여기서 다시 계산하면
# 대시보드·collect_gsc와 어긋난 자리에 키를 깐다. 콘솔 인코딩도 db가 맞춘다.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "capture" / "scripts"))
import db  # noqa: E402

DEST = db.gsc_key()


def sa_email(path: Path) -> str:
    """서비스 계정 키면 client_email, 아니면 빈 문자열."""
    try:
        d = json.loads(Path(path).read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return ""
    return d.get("client_email", "") if d.get("type") == "service_account" else ""


def find_downloaded():
    for c in sorted(db.downloads_dir().glob("*.json"),
                    key=lambda p: -p.stat().st_mtime)[:15]:
        if sa_email(c):
            return c
    return None


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
    except Exception:   # Brain이 아직 없거나 손상 — 키 설치는 이미 끝났으니 일반 안내로
        return []
    return [(r["name"], r["gsc_property"]) for r in rows if r["gsc_property"]]


def finish(email: str) -> None:
    print(f"\n서비스 계정 이메일: {email}")
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
                    help="키 JSON 경로 (생략하면 다운로드 폴더에서 찾음)")
    ap.add_argument("--status", action="store_true", help="설치된 키 확인만")
    ap.add_argument("--force", action="store_true", help="기존 키 덮어쓰기")
    a = ap.parse_args()

    if a.status:
        email = sa_email(DEST) if DEST.exists() else ""
        if email:
            print(f"설치돼 있습니다: {DEST}")
            finish(email)
            return
        sys.exit(f"설치된 키가 없습니다: {DEST}\n"
                 "  구글 클라우드에서 서비스 계정 키(JSON)를 받은 뒤 이 스크립트를 "
                 "다시 실행하세요 (capture/references/setup.md 4-B).")

    if DEST.exists() and not a.force:
        sys.exit(f"{DEST} 가 이미 있습니다 "
                 f"(이메일: {sa_email(DEST) or '판독 불가'}). 바꾸려면 --force.")

    src = Path(a.src) if a.src else find_downloaded()
    if not src or not src.exists():
        sys.exit("다운로드 폴더에서 서비스 계정 키(JSON)를 찾지 못했습니다.\n"
                 "  구글 클라우드 → IAM → 서비스 계정 → 키 → '키 추가' → JSON 으로 "
                 "받은 뒤 다시 실행하거나, --from <경로> 로 직접 지정하세요.")
    email = sa_email(src)
    if not email:
        sys.exit(f"{src} 는 서비스 계정 키가 아닙니다 (type=service_account 필요).")
    DEST.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(DEST))
    print(f"[설치] {src.name} → {DEST} (다운로드 폴더에서는 제거됨)")
    finish(email)


if __name__ == "__main__":
    main()
