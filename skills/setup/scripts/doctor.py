#!/usr/bin/env python3
"""seo-miner doctor — environment diagnosis (stdlib only, runs before any pip).

Checks deps, CAPTURE_HOME/Brain, API keys, GSC files, then derives which
capabilities are unlocked and prints ordered next steps.

Usage: python doctor.py [--json]
Exit code: 0 = core usable, 1 = core setup incomplete.
"""
import importlib.util
import json
import os
import sqlite3
import sys
from pathlib import Path

CAPTURE_HOME = Path(os.environ.get("CAPTURE_HOME", Path.home() / ".capture"))
DB = Path(os.environ.get("CAPTURE_DB", CAPTURE_HOME / "brain.db"))

KEY_NAMES = {
    "openrouter": "OpenRouter 키",
    "dataforseo": "DataForSEO 계정",
    "serper": "Serper 키",
    "gsc_client_secrets": "구글 인증 파일",
    "gsc_token_cached": "구글 로그인 기록",
}


def has(mod: str) -> bool:
    return importlib.util.find_spec(mod) is not None


def diagnose() -> dict:
    deps_core = {m: has(m) for m in ("requests", "jinja2", "yaml")}
    deps_gsc = {m: has(m) for m in ("googleapiclient", "google_auth_oauthlib")}
    brain = {"home": str(CAPTURE_HOME), "home_exists": CAPTURE_HOME.exists(),
             "db_exists": DB.exists(), "tables": 0, "projects": []}
    if DB.exists():
        try:
            conn = sqlite3.connect(DB)
            brain["tables"] = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
            brain["projects"] = [r[0] for r in conn.execute(
                "SELECT name FROM projects")] if brain["tables"] else []
            conn.close()
        except Exception as e:
            brain["error"] = str(e)
    secrets = os.environ.get("GSC_CLIENT_SECRETS",
                             str(CAPTURE_HOME / "client_secrets.json"))
    keys = {
        "openrouter": bool(os.environ.get("OPENROUTER_API_KEY")),
        "dataforseo": bool(os.environ.get("DATAFORSEO_LOGIN")
                           and os.environ.get("DATAFORSEO_PASSWORD")),
        "serper": bool(os.environ.get("SERPER_API_KEY")),
        "gsc_client_secrets": Path(secrets).exists(),
        "gsc_token_cached": (CAPTURE_HOME / "gsc_token.json").exists(),
    }
    core_ok = all(deps_core.values())
    brain_ok = brain["db_exists"] and not brain.get("error")
    # ponytail: 라벨 자체를 사람 말로 저장 — 별도 표시용 매핑 테이블 안 만듦
    caps = {
        "키워드 찾기 — 검색창 자동완성으로 후보 수집 (돈 안 듦)": core_ok,
        "보관함 — 수집한 자료를 내 컴퓨터에 저장해 둡니다": core_ok and brain_ok,
        "글 만들기 — 모은 키워드로 콘텐츠 초안 작성": core_ok,
        "AI 노출 확인 — ChatGPT 같은 AI가 내 글을 인용하는지 검사":
            core_ok and keys["openrouter"],
        "구글 실제 성과 — 서치콘솔의 진짜 순위·클릭수 가져오기":
            core_ok and all(deps_gsc.values()) and keys["gsc_client_secrets"],
        "순위 추적 — 검색결과 몇 등인지 기록 (없어도 됩니다)":
            core_ok and (keys["dataforseo"] or keys["serper"]),
    }
    steps = []
    if not core_ok:
        steps.append("기본 부품 설치 — 터미널에 `pip install requests jinja2 pyyaml` "
                     "입력 (1분). 이게 있어야 나머지가 돕니다.")
    if not brain_ok:
        steps.append("보관함 만들기 — `python skills/capture/scripts/db.py init` "
                     "(몇 초, 제가 대신 실행해 드릴 수 있어요)")
    if not keys["openrouter"]:
        steps.append("AI 노출 확인 켜기 — OpenRouter에서 키를 발급받아 "
                     "OPENROUTER_API_KEY 환경변수에 저장 (약 5분). "
                     "발급 방법: capture/references/setup.md 5절")
    if not all(deps_gsc.values()):
        steps.append("구글 연동 부품 설치 — "
                     "`pip install google-api-python-client google-auth-oauthlib`")
    if not keys["gsc_client_secrets"]:
        steps.append(f"구글 서치콘솔 연결 — 구글에서 인증 파일(client_secrets.json)을 "
                     f"내려받아 {secrets} 위치에 두기 (약 10분). "
                     "따라 할 순서: setup.md 4절")
    if not (keys["dataforseo"] or keys["serper"]):
        steps.append("[안 해도 됩니다] 순위 추적용 유료 키 — DataForSEO 또는 Serper. "
                     "순위를 직접 매기고 싶을 때만: setup.md 7절")
    if brain_ok and not brain["projects"]:
        steps.append("첫 사이트 등록 — 채팅에 `/capture add <원하는이름>` 이라고 "
                     "입력하면 됩니다")
    return {"deps_core": deps_core, "deps_gsc": deps_gsc, "brain": brain,
            "keys": keys, "capabilities": caps, "next_steps": steps,
            "core_ok": core_ok, "brain_ok": brain_ok}


def main() -> None:
    d = diagnose()
    if "--json" in sys.argv:
        print(json.dumps(d, ensure_ascii=False, indent=2))
    else:
        print("seo-miner 상태 점검")
        print(f"자료가 저장되는 폴더: {d['brain']['home']}\n")
        ready = [k for k, v in d["capabilities"].items() if v]
        locked = [k for k, v in d["capabilities"].items() if not v]
        print("[지금 쓸 수 있는 기능]")
        for k in ready:
            print(f"  ✓ {k}")
        if not ready:
            print("  아직 없습니다 — 아래 1번부터 하시면 바로 켜집니다.")
        if locked:
            print("\n[아직 못 쓰는 기능 — 아래 순서대로 하면 켜집니다]")
            for k in locked:
                print(f"  ✗ {k}")
        print("\n[연결 상태]")
        for k, v in d["keys"].items():
            print(f"  - {KEY_NAMES.get(k, k)}: {'연결됨' if v else '아직 없음'}")
        if d["brain"].get("error"):
            print(f"\n[주의] 보관함 파일을 여는 데 실패했습니다: {d['brain']['error']}")
        if d["brain"]["projects"]:
            print("\n등록된 사이트:", ", ".join(d["brain"]["projects"]))
        if d["next_steps"]:
            print("\n[다음에 할 일 — 위에서부터 하나씩]")
            for i, s in enumerate(d["next_steps"], 1):
                print(f"  {i}. {s}")
        else:
            print("\n준비 끝났습니다 — `/capture run <사이트이름>` 으로 첫 수집을 "
                  "돌려보세요.")
    sys.exit(0 if d["core_ok"] and d["brain_ok"] else 1)


if __name__ == "__main__":
    try:
        main()
    except BrokenPipeError:  # head 등 파이프 잘림 방어
        sys.exit(0)
