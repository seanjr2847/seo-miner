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

for _s in (sys.stdout, sys.stderr):  # 한국어 Windows 콘솔(cp949)에서 ✓/— 출력 보호
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

KEY_NAMES = {
    "openrouter": "OpenRouter 키",
    "dataforseo": "DataForSEO 계정",
    "serper": "Serper 키",
    "gsc_client_secrets": "구글 인증 파일(모든 사이트 공용)",
    "gsc_token_cached": "구글 로그인 기록(구버전 공용)",
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
    # 구글 연결은 사이트별로 따로 잡힌다 (creds/{사이트}/).
    gsc_sites = {name: (CAPTURE_HOME / "creds" / name / "client_secrets.json").exists()
                 for name in brain["projects"]}
    keys = {
        "openrouter": bool(os.environ.get("OPENROUTER_API_KEY")),
        "dataforseo": bool(os.environ.get("DATAFORSEO_LOGIN")
                           and os.environ.get("DATAFORSEO_PASSWORD")),
        "serper": bool(os.environ.get("SERPER_API_KEY")),
        "gsc_client_secrets": Path(secrets).exists(),
        "gsc_token_cached": (CAPTURE_HOME / "gsc_token.json").exists(),
    }
    core_ok = all(deps_core.values())
    # 보관함은 첫 실행 때 자동 생성된다(db.connect) — 파일이 없는 건 문제가 아니고,
    # 있는데 안 열리는 것만 문제다.
    brain_ok = not brain.get("error")
    # ponytail: 라벨 자체를 사람 말로 저장 — 별도 표시용 매핑 테이블 안 만듦
    caps = {
        "키워드 찾기 — 검색창 자동완성으로 후보 수집 (돈 안 듦)": core_ok,
        "보관함 — 수집한 자료를 내 컴퓨터에 저장 (없으면 자동으로 만듭니다)":
            core_ok and brain_ok,
        "글 만들기 — 모은 키워드로 콘텐츠 초안 작성": core_ok,
        "AI 노출 확인 — ChatGPT 같은 AI가 내 글을 인용하는지 검사 "
        "(브라우저로 하면 키 없이도 됩니다)": core_ok,
        "구글 실제 성과 — 서치콘솔에서 받은 CSV로 진짜 순위·클릭 읽기": core_ok,
        "구글 자동 연동 — 매번 내보내기 안 하고 알아서 가져오기 (사이트마다 따로)":
            core_ok and all(deps_gsc.values())
            and (any(gsc_sites.values()) or keys["gsc_client_secrets"]),
        "순위 추적 — 검색결과 몇 등인지 기록 (없어도 됩니다)":
            core_ok and (keys["dataforseo"] or keys["serper"]),
    }
    steps = []
    if not core_ok:
        steps.append("기본 부품 설치 — 터미널에 `pip install requests jinja2 pyyaml` "
                     "입력 (1분). 이게 있어야 나머지가 돕니다.")
    if not brain_ok:
        steps.append(f"보관함 파일이 손상됐습니다 — {DB} 를 다른 이름으로 옮기면 "
                     "다음 실행 때 새로 만들어집니다 (지금까지 모은 자료는 사라짐)")
    if brain_ok and not brain["projects"]:
        steps.append("첫 사이트 등록 — 채팅에 `/capture add <원하는이름>` 이라고 "
                     "입력하면 제가 물어보면서 만들어 드립니다. 여기까지가 필수 끝.")
    # 아래는 전부 선택 — '해야 할 일'처럼 보이지 않게 [선택] 접두사를 붙인다.
    if not keys["openrouter"]:
        steps.append("[선택] AI 노출 확인을 자동으로 돌리려면 OpenRouter 키 "
                     "(유료, 약 5분): capture/references/setup.md 5절. "
                     "→ 돈 안 들이려면 `/seo-miner:browse`로 브라우저에서 직접 확인하면 됩니다")
    unlinked = [n for n, ok in gsc_sites.items() if not ok]
    if unlinked and not keys["gsc_client_secrets"]:
        steps.append("[선택] 구글 실적 자동 수집이 아직 안 붙은 사이트: "
                     f"{', '.join(unlinked)} — 사이트마다 자기 데스크톱 클라이언트를 "
                     "쓰므로 하나씩 붙입니다 (사이트당 3~5분): setup.md 4-B. "
                     "→ 급하면 '내보내기 → CSV' 경로가 설정 없이 바로 됩니다")
    elif not all(deps_gsc.values()):
        steps.append("[선택] 구글 자동 연동용 부품 설치 — "
                     "`pip install google-api-python-client google-auth-oauthlib`")
    if not (keys["dataforseo"] or keys["serper"]):
        steps.append("[선택] 순위 추적용 유료 키 — DataForSEO 또는 Serper. "
                     "검색 순위를 매일 기록하고 싶을 때만: setup.md 7절")
    return {"deps_core": deps_core, "deps_gsc": deps_gsc, "brain": brain,
            "keys": keys, "gsc_sites": gsc_sites, "capabilities": caps,
            "next_steps": steps, "core_ok": core_ok, "brain_ok": brain_ok}


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
            print("\n[아직 안 켠 기능 — 없어도 나머지는 다 돌아갑니다]")
            for k in locked:
                print(f"  ✗ {k}")
        print("\n[연결 상태]")
        for k, v in d["keys"].items():
            print(f"  - {KEY_NAMES.get(k, k)}: {'연결됨' if v else '아직 없음'}")
        if d["brain"].get("error"):
            print(f"\n[주의] 보관함 파일을 여는 데 실패했습니다: {d['brain']['error']}")
        if d["gsc_sites"]:
            print("\n[사이트별 구글 연결] — 사이트마다 자기 클라이언트를 씁니다")
            for name, ok in d["gsc_sites"].items():
                print(f"  - {name}: {'연결됨' if ok else '아직 (CSV로는 지금도 가능)'}")
        must = [s for s in d["next_steps"] if not s.startswith("[선택]")]
        opt = [s for s in d["next_steps"] if s.startswith("[선택]")]
        if must:
            print("\n[해야 할 일]")
            for i, s in enumerate(must, 1):
                print(f"  {i}. {s}")
        else:
            print("\n[해야 할 일] 없습니다 — `/capture run <사이트이름>` 으로 "
                  "바로 돌리시면 됩니다.")
        if opt:
            print("\n[나중에 해도 되는 것 — 급하지 않습니다]")
            for s in opt:
                print(f"  · {s[len('[선택] '):]}")
    sys.exit(0 if d["core_ok"] and d["brain_ok"] else 1)


if __name__ == "__main__":
    try:
        main()
    except BrokenPipeError:  # head 등 파이프 잘림 방어
        sys.exit(0)
