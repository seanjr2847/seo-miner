#!/usr/bin/env python3
"""seo-miner doctor — environment diagnosis (stdlib only, runs before any pip).

Checks deps, CAPTURE_HOME/Brain, API keys, GSC service-account key (shared by
the bundled gsc MCP server and collect_gsc.py), then derives which capabilities
are unlocked and prints ordered next steps.

Usage: python doctor.py [--json]
Exit code: 0 = core usable, 1 = core setup incomplete.
"""
import importlib.util
import json
import os
import shutil
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
    "gsc_service_account": "구글 서치콘솔 열쇠(서비스 계정 — 전 사이트 공용 1개)",
    "node_npx": "node/npx (Claude가 서치콘솔을 즉석 조회하는 gsc MCP용)",
}


def has(mod: str) -> bool:
    return importlib.util.find_spec(mod) is not None


def diagnose() -> dict:
    deps_core = {m: has(m) for m in ("requests", "jinja2", "yaml")}
    deps_gsc = {m: has(m) for m in ("googleapiclient",)}
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
    # 구글 연결 표준: 서비스 계정 키 1개(전 사이트 공용, gsc MCP 서버와 공유).
    # 구버전 사이트별 OAuth 토큰이 남은 사이트는 그걸로도 돈다(하위호환).
    sa_key = Path(os.environ.get("GOOGLE_APPLICATION_CREDENTIALS",
                                 str(CAPTURE_HOME / "gsc_service_account.json")))
    gsc_legacy = {name: (CAPTURE_HOME / "creds" / name / "gsc_token.json").exists()
                  for name in brain["projects"]}
    gsc_sites = {name: sa_key.exists() or gsc_legacy[name]
                 for name in brain["projects"]}
    keys = {
        "openrouter": bool(os.environ.get("OPENROUTER_API_KEY")),
        "dataforseo": bool(os.environ.get("DATAFORSEO_LOGIN")
                           and os.environ.get("DATAFORSEO_PASSWORD")),
        "serper": bool(os.environ.get("SERPER_API_KEY")),
        "gsc_service_account": sa_key.exists(),
        "node_npx": bool(shutil.which("npx")),
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
        "구글 자동 연동 — 열쇠 1개면 모든 사이트 (Claude 즉석 조회 + 자동 수집)":
            core_ok and all(deps_gsc.values())
            and (keys["gsc_service_account"] or any(gsc_legacy.values())),
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
    gsc_linked = keys["gsc_service_account"] or any(gsc_legacy.values())
    if brain["projects"] and not gsc_linked:
        steps.append("[선택] 구글 실적 자동 수집 — 서비스 계정 키 1개 만들면 모든 "
                     "사이트에 적용 (5분, 무료): setup 스킬 'GSC 연결(서비스 계정)' "
                     "(setup.md 4-B). "
                     "→ 급하면 '내보내기 → CSV' 경로가 설정 없이 바로 됩니다")
    elif gsc_linked and not all(deps_gsc.values()):
        steps.append("[선택] 구글 자동 수집용 부품 설치 — "
                     "`pip install google-api-python-client`")
    if keys["gsc_service_account"] and not keys["node_npx"]:
        steps.append("[선택] node를 설치하면 수집 없이도 Claude가 서치콘솔을 바로 "
                     "조회합니다(gsc MCP) — nodejs.org")
    if not (keys["dataforseo"] or keys["serper"]):
        steps.append("[선택] 순위 추적용 유료 키 — DataForSEO 또는 Serper. "
                     "검색 순위를 매일 기록하고 싶을 때만: setup.md 7절")
    return {"deps_core": deps_core, "deps_gsc": deps_gsc, "brain": brain,
            "keys": keys, "gsc_sites": gsc_sites, "gsc_legacy": gsc_legacy,
            "capabilities": caps, "next_steps": steps,
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
            print("\n[아직 안 켠 기능 — 없어도 나머지는 다 돌아갑니다]")
            for k in locked:
                print(f"  ✗ {k}")
        print("\n[연결 상태]")
        for k, v in d["keys"].items():
            print(f"  - {KEY_NAMES.get(k, k)}: {'연결됨' if v else '아직 없음'}")
        if d["brain"].get("error"):
            print(f"\n[주의] 보관함 파일을 여는 데 실패했습니다: {d['brain']['error']}")
        if d["gsc_sites"]:
            print("\n[사이트별 구글 연결]")
            for name, ok in d["gsc_sites"].items():
                if d["keys"]["gsc_service_account"]:
                    tag = "연결됨(공용 열쇠)"
                elif d["gsc_legacy"].get(name):
                    tag = "연결됨(구버전 개별 토큰)"
                else:
                    tag = "아직 (CSV로는 지금도 가능)"
                print(f"  - {name}: {tag}")
            if d["keys"]["gsc_service_account"]:
                print("    (공용 열쇠는 Search Console '사용자 및 권한'에 이메일을 "
                      "추가한 속성만 읽습니다 — 확인: connect_gsc.py --status)")
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
