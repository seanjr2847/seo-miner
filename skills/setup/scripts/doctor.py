#!/usr/bin/env python3
"""seo-miner doctor — environment diagnosis (stdlib only, runs before any pip).

Checks deps, CAPTURE_HOME/Brain, API keys, GSC service-account key (shared by
the bundled gsc MCP server and collect_gsc.py), then prints a verdict-first
summary: 한 줄 요약 → 다음 한 걸음 → 사이트 → 기능(꺼진 것만 켜는 법 포함).

Usage: python doctor.py [--json | --web]
  --web: 텍스트 대신 로컬 대시보드를 브라우저로 띄운다 (진단 배너 + 데이터 함께)
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


def has(mod: str) -> bool:
    return importlib.util.find_spec(mod) is not None


def load_env(path: Path = CAPTURE_HOME / "env") -> None:
    """~/.capture/env 의 KEY=VALUE를 환경변수로 (대시보드 설정 화면이 쓰는 파일).
    db.py에 같은 함수가 있지만 doctor는 pip 이전에 도는 stdlib 전용이라 import를 못 건다."""
    for line in (path.read_text("utf-8").splitlines() if path.exists() else []):
        k, sep, v = line.partition("=")
        if sep and k.strip() and not k.lstrip().startswith("#"):
            os.environ.setdefault(k.strip(), v.strip())


def diagnose() -> dict:
    load_env()
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
    gsc_linked = keys["gsc_service_account"] or any(gsc_legacy.values())

    # (이름, 한 줄 설명, 켜짐?, 켜는 법 — None이면 핵심 설치만 끝나면 켜진다)
    caps = [
        ("키워드 찾기", "검색창 자동완성으로 후보 수집 (무료)", core_ok, None),
        ("보관함", "수집한 자료를 내 컴퓨터에 저장 (없으면 자동 생성)",
         core_ok and brain_ok, None),
        ("글 만들기", "모은 키워드로 콘텐츠 초안 작성", core_ok, None),
        ("AI 노출 확인", "ChatGPT 같은 AI가 내 글을 인용하는지 검사 "
         "(브라우저 방식은 키 없이 무료)", core_ok, None),
        ("구글 실적 읽기", "서치콘솔에서 받은 CSV로 진짜 순위·클릭 확인",
         core_ok, None),
        ("구글 자동 연동", "CSV 내보내기 없이 자동 수집 + Claude가 즉석 조회",
         core_ok and all(deps_gsc.values()) and gsc_linked,
         "서비스 계정 키 1개면 모든 사이트가 붙습니다 (무료, 5분) — 채팅에 "
         "\"GSC 연동해줘\" 하시면 제가 대신 눌러 드립니다. "
         "급하면 CSV 내보내기가 설정 없이 지금도 됩니다."),
        ("순위 추적", "검색결과 몇 등인지 매일 기록",
         core_ok and (keys["dataforseo"] or keys["serper"]),
         "유료 키가 필요합니다 (DataForSEO 또는 Serper): setup.md 7절. "
         "순위를 매일 기록하고 싶을 때만 켜세요 — 안 켜도 GSC 실측으로 충분합니다."),
    ]

    must = []
    if not core_ok:
        must.append("기본 부품 설치 — `pip install requests jinja2 pyyaml` (1분). "
                    "이것만 하면 기능 대부분이 켜집니다. 채팅에 \"설치해줘\" 하시면 "
                    "제가 대신 실행합니다.")
    if not brain_ok:
        must.append(f"보관함 파일이 손상됐습니다 — {DB} 를 다른 이름으로 옮기면 "
                    "다음 실행 때 새로 만들어집니다 (지금까지 모은 자료는 사라짐)")
    if core_ok and brain_ok and not brain["projects"]:
        must.append("첫 사이트 등록 — 채팅에 `/capture add <원하는이름>` 이라고 "
                    "하시면 제가 물어보면서 만들어 드립니다. 여기까지가 필수 끝.")

    later = []
    if not keys["openrouter"]:
        later.append("AI 노출 확인 자동화 — OpenRouter 키 (유료, 약 5분): "
                     "setup.md 5절. 무료로는 `/seo-miner:browse`가 지금도 됩니다.")
    if gsc_linked and not all(deps_gsc.values()):
        later.append("구글 자동 수집용 부품 — `pip install google-api-python-client`")
    if keys["gsc_service_account"] and not keys["node_npx"]:
        later.append("node 설치 — 수집 없이도 Claude가 서치콘솔을 바로 조회합니다"
                     "(gsc MCP): nodejs.org")

    # 한 줄 요약 + 다음 한 걸음 하나 — 읽는 사람이 이 두 줄만 봐도 되게.
    if not core_ok:
        verdict = "설치가 조금 남았습니다 — 아래 [꼭 해야 할 일] 1번이면 대부분 켜집니다."
        next_cmd = "pip install requests jinja2 pyyaml"
    elif not brain_ok:
        verdict = "보관함 파일에 문제가 있습니다 — 아래 [꼭 해야 할 일]을 봐 주세요."
        next_cmd = None
    elif not brain["projects"]:
        verdict = "설치는 끝났습니다 — 첫 사이트만 등록하면 바로 시작입니다."
        next_cmd = "/capture add <원하는이름>"
    else:
        verdict = "다 준비됐습니다 — 바로 쓰시면 됩니다."
        next_cmd = f"/capture run {brain['projects'][0]}"

    steps = must + [f"[선택] {s}" for s in later]
    return {"deps_core": deps_core, "deps_gsc": deps_gsc, "brain": brain,
            "keys": keys, "gsc_sites": gsc_sites, "gsc_legacy": gsc_legacy,
            "capabilities": {f"{n} — {d}": on for n, d, on, _ in caps},
            "locked": [{"name": n, "desc": d, "fix": fix}
                       for n, d, on, fix in caps if not on],
            "verdict": verdict, "next_command": next_cmd, "next_steps": steps,
            "must": must, "later": later,
            "core_ok": core_ok, "brain_ok": brain_ok}


def render(d: dict) -> None:
    caps_on = [k.split(" — ")[0] for k, v in d["capabilities"].items() if v]
    print("seo-miner 점검 결과")
    print(f"  {d['verdict']}")
    if d["next_command"]:
        print(f"  다음 한 걸음: {d['next_command']}")
    if d["brain"].get("error"):
        print(f"\n[주의] 보관함 파일을 여는 데 실패했습니다: {d['brain']['error']}")

    if d["gsc_sites"]:
        print(f"\n내 사이트 ({len(d['gsc_sites'])}개)")
        for name in d["gsc_sites"]:
            if d["keys"]["gsc_service_account"]:
                tag = "연결됨 (공용 열쇠)"
            elif d["gsc_legacy"].get(name):
                tag = "연결됨 (예전 방식 — 그대로 써도 됩니다)"
            else:
                tag = "아직 (CSV 내보내기로는 지금도 가능)"
            print(f"  · {name} — 구글 자동 수집: {tag}")
        if d["keys"]["gsc_service_account"]:
            print("    * 공용 열쇠는 Search Console '사용자 및 권한'에 이메일을 "
                  "추가한 사이트만 읽습니다 (확인: connect_gsc.py --status)")

    if caps_on:
        print(f"\n지금 되는 것: {' · '.join(caps_on)}")
    else:
        print("\n지금 되는 것: 아직 없습니다 — 아래 [꼭 해야 할 일]만 하면 켜집니다.")
    if d["locked"]:
        print("아직 안 켠 것 (없어도 위 기능은 다 돌아갑니다)")
        for c in d["locked"]:
            print(f"  · {c['name']} — {c['desc']}")
            print(f"    켜려면: {c['fix'] or '아래 [꼭 해야 할 일]만 끝내면 자동으로 켜집니다.'}")

    if d["must"]:
        print("\n[꼭 해야 할 일]")
        for i, s in enumerate(d["must"], 1):
            print(f"  {i}. {s}")
    if d["later"]:
        print("\n나중에 하면 좋은 것 (급하지 않습니다)")
        for s in d["later"]:
            print(f"  · {s}")

    print(f"\n자료 폴더: {d['brain']['home']}")


def main() -> None:
    d = diagnose()
    if "--web" in sys.argv:
        # 대시보드(capture 스킬)가 /api/doctor로 이 진단을 배너로 보여준다.
        import subprocess
        dash = Path(__file__).resolve().parents[2] / "capture" / "scripts" / "dashboard.py"
        # 첫 줄(주소)만 받아서 대신 찍고 나는 빠진다 — 서버 출력을 물고 있으면
        # 이 명령이 안 끝난 것처럼 보인다(파이프로 감쌌을 때 특히).
        proc = subprocess.Popen([sys.executable, "-u", str(dash), "--open"],
                                stdout=subprocess.PIPE, encoding="utf-8",
                                errors="replace")
        line = (proc.stdout.readline() or "").strip()
        proc.stdout.close()
        print("대시보드를 띄웠습니다 — 상단 배너가 점검 결과이고, 그 아래 [설정]에서 "
              "부품 설치·사이트 등록·API 키 저장을 바로 하실 수 있습니다.")
        print(line or f"(안 열리면: python {dash} --open)")
    elif "--json" in sys.argv:
        print(json.dumps(d, ensure_ascii=False, indent=2))
    else:
        render(d)
    sys.exit(0 if d["core_ok"] and d["brain_ok"] else 1)


if __name__ == "__main__":
    try:
        main()
    except BrokenPipeError:  # head 등 파이프 잘림 방어
        sys.exit(0)
