#!/usr/bin/env python3
"""seo-miner doctor — environment diagnosis (stdlib only, runs before any pip).

Checks deps, CAPTURE_HOME/Brain, API keys, GSC 연결(3-상태: 연결됨 / 로그인 대기 /
인증 없음 — 판정은 db.gsc_connected), then prints a verdict-first
summary: 한 줄 요약 → 다음 한 걸음 → 사이트 → 기능(꺼진 것만 켜는 법 포함).

Usage: python doctor.py [--json | --web]
  --web: 텍스트 대신 로컬 대시보드를 브라우저로 띄운다 (진단 배너 + 데이터 함께)
Exit code: 0 = core usable, 1 = core setup incomplete.
"""
import importlib.util
import json
import os
import sqlite3
import sys
from pathlib import Path

# 경로·env 로딩·콘솔 인코딩은 db가 유일한 주인이다. db의 모듈 레벨은 stdlib만
# 쓰고(yaml은 함수 안에서 늦게 import) DB에 연결하지도 않으므로, pip 이전에 도는
# doctor가 import해도 안전하다 — 진단하러 왔다가 Brain을 만들어 버리는 일도 없다.
CAPTURE_SCRIPTS = Path(__file__).resolve().parents[2] / "capture" / "scripts"
sys.path.insert(0, str(CAPTURE_SCRIPTS))
import db  # noqa: E402
import stage  # noqa: E402


MARKETING_SKILLS = {
    "product-marketing": "사이트 포지셔닝 문서 (setup 온보딩)",
    "seo-audit":         "기술·온페이지 진단 (색인 차단·커버리지·순위 하락)",
    "ai-seo":            "AI 인용 갭 해석 (ChatGPT·Perplexity가 남을 인용할 때)",
    "content-strategy":  "키워드를 토픽 클러스터로 (keywords 큐레이션 직후)",
    "site-architecture": "카니벌라이제이션·URL 구조",
    "programmatic-seo":  "pSEO 페이지 대량 생성 (create plan)",
    "schema":            "구조화 데이터 (create run)",
}
OPTIONAL_SKILLS = {
    "aso": "앱 스토어 리스팅 최적화 (앱이 있는 사이트만)",
}


def find_skill(name: str) -> bool:
    # $CLAUDE_SKILLS_DIR 가 걸려 있으면 거기가 유일한 탐색 기준이다 (테스트 격리 및
    # 사용자 지정 경로). 없을 때만 기본 홈/플러그인 캐시/현재 작업 디렉토리를 순서대로 본다.
    env_dir = os.environ.get("CLAUDE_SKILLS_DIR")
    if env_dir:
        return (Path(env_dir) / name / "SKILL.md").exists()

    # 1. ~/.claude/skills/
    if (Path.home() / ".claude" / "skills" / name / "SKILL.md").exists():
        return True

    # 2. ~/.claude/plugins/cache/*/*/*/skills/ (플러그인이 동봉한 경우)
    cache = Path.home() / ".claude" / "plugins" / "cache"
    if cache.exists():
        try:
            for s_dir in cache.glob("*/*/*/skills"):
                if (s_dir / name / "SKILL.md").exists():
                    return True
        except Exception:
            pass

    # 3. <현재 작업 디렉토리>/.claude/skills/
    if (Path.cwd() / ".claude" / "skills" / name / "SKILL.md").exists():
        return True

    return False


def has(mod: str) -> bool:
    return importlib.util.find_spec(mod) is not None


def diagnose() -> dict:
    db.load_env()   # 대시보드가 방금 저장한 키를 같은 프로세스에서도 집어 올린다
    CAPTURE_HOME, DB = db.CAPTURE_HOME, db.DB_PATH
    marketing_skills = {name: find_skill(name) for name in MARKETING_SKILLS}
    marketing_optional = {name: find_skill(name) for name in OPTIONAL_SKILLS}
    deps_core = {m: has(m) for m in ("requests", "yaml")}
    # google_auth_oauthlib 은 조회가 아니라 **로그인 창**을 여는 부품이다 —
    # 이게 없으면 OAuth(기본) 사용자는 첫 수집에서 그 자리에 멈춘다.
    deps_gsc = {m: has(m) for m in ("googleapiclient", "google_auth_oauthlib")}
    brain = {"home": str(CAPTURE_HOME), "home_exists": CAPTURE_HOME.exists(),
             "db_exists": DB.exists(), "tables": 0, "projects": [],
             "no_prompts": []}
    if DB.exists():
        try:
            conn = sqlite3.connect(DB)
            brain["tables"] = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
            brain["projects"] = [r[0] for r in conn.execute(
                "SELECT name FROM projects")] if brain["tables"] else []
            # AI에 물어볼 질문이 비어 있으면 collect_ai가 그 자리에서 멈춘다.
            # 대시보드 폼으로 만든 사이트가 여기 걸린다(프롬프트 초안은 채팅 몫).
            brain["no_prompts"] = [r[0] for r in conn.execute(
                """SELECT p.name FROM projects p
                     LEFT JOIN ai_prompts a ON a.project_id=p.id AND a.is_active=1
                    GROUP BY p.id HAVING COUNT(a.id)=0""")] if brain["tables"] else []
            conn.close()
        except Exception as e:
            brain["error"] = str(e)
    # 구글 연결은 이제 **2단**이다: 인증 수단이 있나(gsc_auth) ≠ 실제로 붙었나(gsc_connected).
    # 플러그인이 OAuth 클라이언트를 동봉하면서 gsc_auth()는 **아무것도 안 한 사람에게도**
    # "oauth"를 답한다 — 파일 유무로 판정하면 doctor가 설치 직후 전원에게 "연결됨"이라고
    # 거짓말하고, 사용자는 수집이 실패할 때까지 그걸 믿는다. 진짜 판정은 토큰이다.
    gsc_mode = db.gsc_auth()          # "oauth" | "service_account" | "" — 정본은 db
    gst = stage.gsc_state()
    gsc_conn = (gst == "connected")   # 토큰(또는 서비스 계정 키)이 실제로 있나
    gsc_pending = (gst == "pending")  # ← 새로 생긴 "로그인 대기"
    # 번들 클라이언트를 쓰는 중인가 — 동의 화면 경고("확인되지 않은 앱")와 100명 상한이
    # 붙는 갈래라, 사용자에게 미리 말해 줘야 로그인 도중에 멈추지 않는다.
    gsc_bundled = gsc_mode == "oauth" and db.gsc_oauth_client() == db.gsc_oauth_bundled()
    gsc_legacy = {name: (db.creds_dir(name) / "gsc_token.json").exists()
                  for name in brain["projects"]}
    gsc_sites = {name: gsc_conn for name in brain["projects"]}
    keys = {
        "openrouter": bool(os.environ.get("OPENROUTER_API_KEY")),
        "dataforseo": bool(os.environ.get("DATAFORSEO_LOGIN")
                           and os.environ.get("DATAFORSEO_PASSWORD")),
        "serper": bool(os.environ.get("SERPER_API_KEY")),
        # 이름은 옛날 그대로지만(대시보드가 이 키를 먹는다) 뜻은 "구글이 실제로
        # 붙었나"다 — 인증 파일이 놓였나가 아니다.
        "gsc_service_account": gsc_conn,
    }
    core_ok = all(deps_core.values())
    # 보관함은 첫 실행 때 자동 생성된다(db.connect) — 파일이 없는 건 문제가 아니고,
    # 있는데 안 열리는 것만 문제다.
    brain_ok = not brain.get("error")
    gsc_linked = gsc_conn

    # (이름, 한 줄 설명, 켜짐?, 켜는 법 — None이면 핵심 설치만 끝나면 켜진다)
    caps = [
        ("키워드 찾기", "검색창 자동완성으로 후보 수집 (무료)", core_ok, None),
        ("보관함", "수집한 자료를 내 컴퓨터에 저장 (없으면 자동 생성)",
         core_ok and brain_ok, None),
        ("글 만들기", "모은 키워드로 콘텐츠 초안 작성", core_ok, None),
        ("AI 노출 확인", "ChatGPT·Perplexity·Gemini가 내 글을 인용하는지 검사",
         core_ok and bool(os.environ.get("OPENROUTER_API_KEY")),
         "OpenRouter 키가 필요합니다 (유료, 약 5분): setup.md 5절."),
        ("구글 실적 읽기", "서치콘솔 자동 수집으로 진짜 순위·클릭 확인 (필수 연결)",
         core_ok and all(deps_gsc.values()) and gsc_linked,
         # 콘솔 작업은 이제 없다 — 번들 클라이언트가 그 자리를 대신한다.
         # 예전 문구("무료, 5분")는 사용자에게 있지도 않은 할 일을 시키는 거짓말이 됐다.
         ("구글 계정으로 로그인 한 번만 하면 됩니다 (무료, 30초, 준비물 없음) — "
          "채팅에 \"GSC 로그인해줘\" 하시면 브라우저 창이 한 번 열립니다."
          if gsc_pending else
          "구글 인증 수단이 없습니다 (번들이 빠진 배포) — 채팅에 \"GSC 연동해줘\" "
          "하시면 클라이언트 파일 놓는 것부터 제가 안내합니다 (무료).")),
        ("순위 추적", "검색결과 몇 등인지 매일 기록",
         core_ok and (keys["dataforseo"] or keys["serper"]),
         "유료 키가 필요합니다: DataForSEO 권장(AI오버뷰·경쟁사 역키워드까지 측정), "
         "Serper는 대체재(더 싸고 단순 — 선택): setup.md 7절."),
    ]

    must = []
    if not core_ok:
        must.append({
            "id": "core_deps",
            "msg": "기본 부품 설치 — `pip install requests pyyaml` (1분). "
                   "이것만 하면 기능 대부분이 켜집니다. 채팅에 \"설치해줘\" 하시면 "
                   "제가 대신 실행합니다."
        })
    if not brain_ok:
        must.append({
            "id": "brain_broken",
            "msg": f"보관함 파일이 손상됐습니다 — {DB} 를 다른 이름으로 옮기면 "
                   "다음 실행 때 새로 만들어집니다 (지금까지 모은 자료는 사라짐)"
        })
    if core_ok and brain_ok and not brain["projects"]:
        must.append({
            "id": "first_project",
            "msg": "첫 사이트 등록 — 채팅에 `/capture add <원하는이름>` 이라고 "
                   "하시면 제가 물어보면서 만들어 드립니다."
        })
    # GSC 연결은 필수다 — 실측(클릭·노출) 없이는 이 도구의 판정 전부가 재료가 없다.
    # (CSV 내보내기 임시 경로는 2026-08-18 정책으로 삭제 — 연결이 유일한 실적 경로.)
    if core_ok and brain_ok and gsc_pending:
        # 남은 게 로그인뿐인데 "연결하세요"라고만 하면, 사용자는 이미 끝난 콘솔 작업을
        # 다시 찾으러 간다. 다음 걸음은 **로그인을 유발하는 행동** 하나여야 한다.
        must.append({
            "id": "gsc_login",
            "msg": "구글 로그인 한 번 (무료, 30초, 계정당 1회) — 자동 수집과 "
                   "Claude 즉석 조회(/capture ask)의 재료입니다. 준비물은 없고 "
                   "구글 계정으로 로그인만 하면 됩니다 (속성마다 권한 주는 단계 없음). "
                   "채팅에 \"GSC 로그인해줘\" 하시면 브라우저 창이 한 번 열립니다."
                   + (" 이때 \"확인되지 않은 앱\" 경고가 뜨면 [고급] → [이동]을 "
                      "누르시면 됩니다 — 정상입니다." if gsc_bundled else "")
        })
    if core_ok and brain_ok and not gsc_mode:
        must.append({
            "id": "gsc_client",
            "msg": "구글 인증 수단 놓기 (무료) — 이 배포에는 로그인에 쓸 OAuth "
                   "클라이언트 파일이 빠져 있습니다. 채팅에 \"GSC 연동해줘\" 하시면 "
                   "파일을 놓는 것부터 제가 안내합니다 (내 클라이언트를 쓰시려면 "
                   "connect_gsc.py --client-id ... --client-secret ...)."
        })

    later = []
    if brain["no_prompts"]:
        who = ", ".join(brain["no_prompts"])
        later.append(f"AI에 물어볼 질문 만들기 ({who}) — 지금 이 사이트로 AI 노출 확인을 "
                     f"돌리면 \"질문이 없다\"며 멈춥니다. 채팅에 `/capture add {who.split(', ')[0]}` "
                     "이라고 하시면 사이트에 맞는 질문 10~30개를 만들어 드립니다 "
                     "(1분, 무료). 다른 기능은 지금도 다 됩니다.")
    if not keys["openrouter"]:
        later.append("AI 노출 확인 — OpenRouter 키 (유료, 약 5분): setup.md 5절. "
                     "이 키가 없어도 나머지 기능은 다 돌아갑니다.")
    if not (keys["dataforseo"] or keys["serper"]):
        later.append("순위 추적 키 — DataForSEO 권장(AI오버뷰·역키워드 포함), "
                     "Serper는 대체재(선택): setup.md 7절.")
    # 조건이 gsc_linked(=연결됨)면 로그인 전에는 안 뜬다. 그런데 이건 **로그인 전에**
    # 갖춰 둬야 하는 것이다 — 로그인 창을 여는 게 google-auth-oauthlib 이라,
    # 없으면 "로그인해줘"가 그 자리에서 막힌다. 그래서 gsc_mode 기준으로 본다.
    if gsc_mode and not all(deps_gsc.values()):
        later.append("구글 연동 부품 — `pip install google-api-python-client "
                     "google-auth-oauthlib` (앞은 조회용, 뒤는 로그인 창을 여는 부품)")

    # 마케팅 스킬 항목은 show_setup 판정(대시보드가 패널을 펼치는 여부)에서 빼기 위해
    # 메시지를 변수로 보관한다 — 그래야 doctor가 결론을 찍고, 대시보드는 그걸 읽기만
    # 한다.
    marketing_skills_msg = None
    missing_marketing = [k for k, ok in marketing_skills.items() if not ok]
    if missing_marketing:
        # 이름만 한 줄로 나열한다. 설명까지 괄호로 끼우면 한 문장이 화면을 넘겨서
        # 정작 "무엇을 하면 되는지"(저장소 주소)가 눈에 안 들어온다 — 설명은
        # render 의 마케팅 스킬 줄과 SKILL.md 위임 표가 이미 갖고 있다.
        # **핵심은 "채팅에 마케팅 스킬 설치해줘 하시면 제가 대신 설치합니다"다** —
        # 사용자에게 저장소 안내를 떠넘기지 않고, 같은 자리에서 설치를 끝낸다.
        marketing_skills_msg = (
            f"마케팅 스킬 {len(missing_marketing)}개 "
            f"({', '.join(missing_marketing)}) 설치 — 글의 각도와 진단을 맡는 전문 팩입니다. "
            "채팅에 \"마케팅 스킬 설치해줘\" 하시면 제가 대신 설치합니다 (무료). "
            "끝나면 Claude Code 재시작이 한 번 필요합니다 — 그 전엔 화면에 안 뜹니다. "
            "없어도 측정·수집은 그대로 됩니다. "
            "직접 하시려면: https://github.com/coreyhaines31/marketingskills"
        )
        must.append({
            "id": "marketing_skills",
            "msg": marketing_skills_msg
        })
    elif not marketing_optional.get("aso", True):
        marketing_skills_msg = (
            "aso (앱 스토어 리스팅 최적화) — 앱이 있는 사이트만 필요합니다. "
            "채팅에 \"마케팅 스킬 설치해줘\" 하시면 제가 대신 설치합니다 (무료). "
            "끝나면 Claude Code 재시작이 한 번 필요합니다. "
            "직접 하시려면: https://github.com/coreyhaines31/marketingskills"
        )
        later.append(marketing_skills_msg)

    # 한 줄 요약 + 다음 한 걸음 하나 — 읽는 사람이 이 두 줄만 봐도 되게.
    if not core_ok:
        verdict = "설치가 조금 남았습니다 — 아래 [꼭 해야 할 일] 1번이면 대부분 켜집니다."
        next_cmd = "pip install requests pyyaml"
    elif not brain_ok:
        verdict = "보관함 파일에 문제가 있습니다 — 아래 [꼭 해야 할 일]을 봐 주세요."
        next_cmd = None
    elif not brain["projects"]:
        verdict = "설치는 끝났습니다 — 첫 사이트만 등록하면 바로 시작입니다."
        next_cmd = "/capture add <원하는이름>"
    elif gsc_pending:
        verdict = "구글 로그인 한 번만 남았습니다 — 그래야 실측이 자동으로 쌓입니다."
        next_cmd = "GSC 로그인해줘"
    elif not gsc_mode:
        verdict = ("구글 인증 수단이 없습니다 — 로그인에 쓸 클라이언트 파일이 "
                   "이 배포에 빠져 있습니다.")
        next_cmd = "GSC 연동해줘"
    else:
        verdict = "다 준비됐습니다 — 바로 쓰시면 됩니다."
        next_cmd = f"/capture run {brain['projects'][0]}"

    steps = [m["msg"] if isinstance(m, dict) else m for m in must] + [f"[선택] {s}" for s in later]
    # 마케팅 스킬 누락은 "패널을 항상 펼칠 만큼 급한 일"이 아니다 — 다른 must 가
    # 있어 켜졌다가 끝나면 다시 접힌다. 그래서 마케팅 스킬을 뺀 사본을 별도 키로
    # 둔다 — 대시보드 show_setup 판정이 이걸 본다.
    must_other = [m for m in must if (m.get("id") if isinstance(m, dict) else m) != "marketing_skills"]
    return {"deps_core": deps_core, "deps_gsc": deps_gsc, "brain": brain,
            "keys": keys, "gsc_sites": gsc_sites, "gsc_legacy": gsc_legacy,
            "gsc_mode": gsc_mode,   # "oauth" | "service_account" | "" (db.gsc_auth)
            # gsc_mode는 남긴다 — 대시보드가 이 JSON을 먹으므로 기존 키를 없애면 화면이 깨진다.
            "gsc_connected": gsc_conn,    # 3-상태의 정본: 연결됨 / (mode만 있으면)로그인 대기 / 없음
            "gsc_bundled": gsc_bundled,   # 번들 클라이언트로 로그인하는 중인가
            "capabilities": {f"{n} — {d}": on for n, d, on, _ in caps},
            "locked": [{"name": n, "desc": d, "fix": fix}
                       for n, d, on, fix in caps if not on],
            "verdict": verdict, "next_command": next_cmd, "next_steps": steps,
            "must": must, "later": later,
            "must_other": must_other,             # 마케팅 스킬 항목이 빠진 must (show_setup 용)
            "marketing_skills_msg": marketing_skills_msg,   # 결론으로 만든 메시지(없으면 None)
            "marketing_skills": marketing_skills,
            "marketing_optional": marketing_optional,
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
        mode = d.get("gsc_mode", "")
        conn = d.get("gsc_connected", False)
        for name in d["gsc_sites"]:
            if conn and mode == "service_account":
                tag = "연결됨 (서비스 계정 — 무인 수집)"
            elif conn:
                tag = "연결됨 (내 구글 계정 로그인)"
            elif mode:
                # 새로 생긴 상태다. 예전엔 인증 파일이 있으면 곧 연결됨이었는데,
                # 번들 클라이언트가 항상 존재하면서 그 등식이 깨졌다.
                tag = "로그인 대기 — 구글 로그인 한 번이면 끝납니다 (\"GSC 로그인해줘\")"
            elif d["gsc_legacy"].get(name):
                tag = ("예전 방식 토큰만 있음 — 이제 안 씁니다. 다시 연결하세요 "
                       "(connect_gsc.py)")
            else:
                tag = "인증 없음 — 클라이언트 파일부터 놓아야 합니다 (\"GSC 연동해줘\")"
            print(f"  · {name} — 구글 자동 수집: {tag}")
        # 속성별 권한 부여는 서비스 계정에만 있는 절차다 — OAuth 사용자에게 이걸
        # 안내하면 있지도 않은 할 일을 시키는 셈이 된다.
        if mode == "service_account":
            print("    * 서비스 계정은 Search Console '사용자 및 권한'에 이메일을 "
                  "추가한 사이트만 읽습니다 (확인: connect_gsc.py --status)")
        elif mode == "oauth":
            print("    * 내 계정이 소유한 속성은 따로 권한을 줄 필요가 없습니다. "
                  + ("로그인은 이미 끝났습니다 (토큰 보관 중)." if conn else
                     "첫 조회 때 브라우저 로그인 창이 한 번 열립니다."))
            if d.get("gsc_bundled") and not conn:
                print("    * 로그인 화면에 \"확인되지 않은 앱\" 경고가 한 번 뜹니다 — "
                      "[고급] → [이동]을 누르시면 됩니다 (플러그인 동봉 클라이언트).")

    if caps_on:
        print(f"\n지금 되는 것: {' · '.join(caps_on)}")
    else:
        print("\n지금 되는 것: 아직 없습니다 — 아래 [꼭 해야 할 일]만 하면 켜집니다.")
    m_skills = d.get("marketing_skills", {})
    if m_skills:
        installed = sum(1 for v in m_skills.values() if v)
        total = len(m_skills)
        missing = [k for k, v in m_skills.items() if not v]
        if missing:
            print(f"마케팅 스킬: {installed}/{total} (누락: {', '.join(missing)})")
        else:
            print(f"마케팅 스킬: {installed}/{total} (모두 설치됨)")
    if d["locked"]:
        print("아직 안 켠 것 (없어도 위 기능은 다 돌아갑니다)")
        for c in d["locked"]:
            print(f"  · {c['name']} — {c['desc']}")
            print(f"    켜려면: {c['fix'] or '아래 [꼭 해야 할 일]만 끝내면 자동으로 켜집니다.'}")

    if d["must"]:
        print("\n[꼭 해야 할 일]")
        for i, s in enumerate(d["must"], 1):
            msg = s["msg"] if isinstance(s, dict) else s
            print(f"  {i}. {msg}")
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
        dash = CAPTURE_SCRIPTS / "dashboard.py"
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
