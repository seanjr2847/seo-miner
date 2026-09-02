#!/usr/bin/env python3
"""seo-miner doctor — environment diagnosis (stdlib only, runs before any pip).

Checks deps, CAPTURE_HOME/Brain, API keys, GSC 연결(3-상태: 연결됨 / 로그인 대기 /
인증 없음 — 판정은 db.gsc_connected), then prints a verdict-first
summary: 한 줄 요약 → 다음 한 걸음 → 사이트 → 기능(꺼진 것만 켜는 법 포함).

화면·skip 사유·--json 은 전부 모듈 수준 **준비 상태 명부**(CAPABILITIES)를 렌더한
결과다 — 문구·발급 URL·버킷 라벨의 정본이 여기 한 벌이고, 산문 문서는 이 표를
가리키기만 한다.

Usage: python doctor.py [--json | --web | --selfcheck] [--project NAME]
  --project: 그 사이트가 원격(호스팅)이면 서버가 진단하고 여기서는 그 결과만 그린다
  --web: 텍스트 대신 로컬 대시보드를 브라우저로 띄운다 (진단 배너 + 데이터 함께)
  --selfcheck: 명부가 데이터로 남아 있는지 assert 로 확인 (개수 산술 포함)
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
import collect_gsc  # noqa: E402
import db  # noqa: E402
import remote  # noqa: E402
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
# 위임 스킬 **전체** — 개수와 설명의 정본. 세는 쪽은 전부 여기서 센다.
# (예전엔 install_skills 가 필수 7개를 모수로 잡고 필수+선택 8개를 나열해서,
#  깨끗한 머신에서 "7개 중 8개가 설치되어 있지 않습니다"를 찍었다.)
ALL_SKILLS = {**MARKETING_SKILLS, **OPTIONAL_SKILLS}
SKILLS_REPO = "https://github.com/coreyhaines31/marketingskills"

# 버킷 라벨 — doctor 화면과 산문이 **같은 말**을 쓰게 하는 정본. 산문이 이걸
# 다시 타이핑하면 doctor 가 찍지도 않는 머리말을 찾게 만든다(실제로 그랬다:
# 문서는 [나중에 하면 좋은 것], 화면은 [선택]).
BUCKET_MUST = "꼭 해야 할 일"
BUCKET_LATER = "더 켜고 싶으면"
LATER_TAG = "[선택]"

CORE_PIP = "pip install requests pyyaml"
GSC_PIP = "pip install google-api-python-client google-auth-oauthlib"

# 이 런의 준비물을 **누가 대는가**. 값의 주인은 server/settings.py(HOSTED_ENV) 이고
# 표식은 settings.paid_keys() 안에서만 서 있다 — 플러그인 설치본에는 server/ 가 없어
# import 를 못 하므로 이름만 여기 적는다. 서 있으면 호스팅(서버가 유료 키·설치를
# 이미 끝냈다), 없으면 로컬(전부 사용자 몫).
HOSTED_ENV = "SEOMINER_HOSTED"

# ── 준비 상태 명부 ───────────────────────────────────────────────────────────
# "무엇이 무엇을 여는가"의 정본. 항목마다:
#   id / name / desc — 화면에 찍는 이름과 한 줄 설명
#   cost             — "무료" | "유료"
#   keys             — 이 기능을 켜는 환경변수 이름들 (없으면 빈 튜플)
#   url              — 키 발급처 (키가 없는 항목은 "")
#   fix              — 잠겼을 때 할 말. None 이면 핵심 설치만 끝나면 켜진다.
#                      dict 이면 상태별 문구(구글 연결의 3-상태: deps/pending/none).
#   owner            — 호스팅에서 이걸 대는 쪽: "user" | "server". 로컬은 전부 user 다
#                      (diagnose 가 HOSTED_ENV 를 보고 뒤집는다). server 인 항목은
#                      사용자 할 일 목록(extra/locked)에서 빠진다 — 서버가 이미 냈다.
#   blocking         — 없으면 아하 모먼트(기회 목록 = gaps)에 **못 가는가**.
#                      False 면 그 단계만 건너뛰면 된다. 이 축이 없어서 키 없는
#                      로컬 사용자의 "지금 할 것"이 AI 단계에 영영 붙어 있었다.
# 산문 파일(README·SKILL.md·references)은 이 표를 **가리키기만** 하고 개수·URL·
# 문구를 다시 적지 않는다 — 두 벌이 되면 한쪽만 낡는다(이 저장소가 반복해서 겪었다).
CAPABILITIES = (
    {"id": "keywords", "name": "키워드 찾기",
     "desc": "검색창 자동완성으로 후보 수집 (무료)",
     "cost": "무료", "keys": (), "url": "", "fix": None,
     "owner": "server", "blocking": True},
    {"id": "brain", "name": "보관함",
     "desc": "수집한 자료를 내 컴퓨터에 저장 (없으면 자동 생성)",
     "cost": "무료", "keys": (), "url": "", "fix": None,
     "owner": "server", "blocking": True},
    {"id": "create", "name": "글 만들기",
     "desc": "모은 키워드로 콘텐츠 초안 작성",
     # 글쓰기는 기회 목록 뒤의 단계다 — 이게 막혀도 아하 모먼트까지는 간다.
     "cost": "무료", "keys": (), "url": "", "fix": None,
     "owner": "server", "blocking": False},
    {"id": "ai", "name": "AI 노출 확인",
     "desc": "ChatGPT·Perplexity·Gemini가 내 글을 인용하는지 검사",
     "cost": "유료", "keys": ("OPENROUTER_API_KEY",),
     "url": "https://openrouter.ai/keys",
     "fix": "AI 노출 확인만 켜는 데 OpenRouter 키가 필요합니다 "
            "(유료, 약 5분 · 없어도 기회 목록까지는 갑니다) — 발급: "
            "https://openrouter.ai/keys · 크레딧 소액 충전 후 키 생성.",
     # 호스팅에선 서버가 낸다. 로컬에서 키가 없으면 이 단계만 건너뛴다 —
     # 무료로 쓰는 사람도 기회 목록까지는 간다.
     "owner": "server", "blocking": False},
    {"id": "gsc", "name": "구글 실적 읽기",
     "desc": "서치콘솔 자동 수집으로 진짜 순위·클릭 확인 (필수 연결)",
     "cost": "무료", "keys": (), "url": "",
     # 콘솔 작업은 이제 없다 — 번들 클라이언트가 그 자리를 대신한다.
     # 예전 문구("무료, 5분")는 사용자에게 있지도 않은 할 일을 시키는 거짓말이 됐다.
     "fix": {
         "deps": f"구글 연동 부품이 없습니다 — `{GSC_PIP}` (앞은 조회용, 뒤는 "
                 "로그인 창을 여는 부품). 채팅에 \"설치해줘\" 하시면 제가 대신 "
                 "실행합니다.",
         "pending": "구글 계정으로 로그인 한 번만 하면 됩니다 (무료, 30초, 준비물 "
                    "없음) — 채팅에 \"GSC 로그인해줘\" 하시면 브라우저 창이 한 번 "
                    "열립니다.",
         "none": "구글 인증 수단이 없습니다 (번들이 빠진 배포) — 채팅에 "
                 "\"GSC 연동해줘\" 하시면 클라이언트 파일 놓는 것부터 제가 "
                 "안내합니다 (무료).",
     },
     # 구글 로그인은 양쪽 다 사용자 몫이다(서버가 대신 로그인할 수 없다).
     # 실측이 없으면 모든 판정에 재료가 없다 — 아하 모먼트를 막는다.
     "owner": "user", "blocking": True},
    {"id": "rank", "name": "순위 추적",
     "desc": "검색결과 몇 등인지 매일 기록",
     "cost": "유료",
     "keys": ("DATAFORSEO_LOGIN", "DATAFORSEO_PASSWORD", "SERPER_API_KEY"),
     "url": "https://dataforseo.com",
     "fix": "순위 추적만 켜는 데 유료 키가 필요합니다 (없어도 기회 목록까지는 갑니다) "
            "— DataForSEO 권장(AI오버뷰·경쟁사 역키워드까지 "
            "측정): https://dataforseo.com (가입 시 $1 무료 크레딧, API 비밀번호는 "
            "계정 비번과 다름 — API Settings 에서 확인). 대체재 Serper: "
            "https://serper.dev (무료 2,500콜, 카드 불필요, AI오버뷰 없음).",
     "owner": "server", "blocking": False},
)


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


def gsc_missing_scopes() -> list[str]:
    """저장된 OAuth 토큰이 갖고 있던 스코프가 지금 collect_gsc.SCOPES 에 못 미치면
    그 차집합 — GA4용 analytics.readonly 가 나중에 추가돼서, 그 전에 로그인해 둔
    토큰은 안 갖고 있을 수 있다. 서비스 계정은 호출마다 SCOPES 를 새로 요청하므로
    (저장된 동의가 없다) 해당 없음 — 빈 리스트.
    """
    if db.gsc_auth() != "oauth":
        return []
    token = db.gsc_token_file()
    if token is None:
        return []
    try:
        saved = set(json.loads(token.read_text(encoding="utf-8")).get("scopes") or [])
    except (OSError, ValueError):
        return []                      # 손상된 토큰은 다른 경로(로그인 안내)가 잡는다
    return [s for s in collect_gsc.SCOPES if s not in saved]


def diagnose(project: str = "") -> dict:
    db.load_env()   # 대시보드가 방금 저장한 키를 같은 프로세스에서도 집어 올린다
    # 호스팅이면 유료 키·pip 설치·구글 클라이언트 등록이 유저 몫이 아니다(서버가 댄다).
    # 표식은 settings.paid_keys() 가 세우고, 그 컨텍스트 안에서는 서버 키가 엔진 이름으로
    # 실제로 보인다 — 그래서 아래 keys 판정도 "런이 보는 env" 그대로가 된다.
    hosted = bool(os.environ.get(HOSTED_ENV))
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
                "SELECT name FROM projects ORDER BY name")] if brain["tables"] else []
            # AI에 물어볼 질문이 비어 있으면 collect_ai가 그 자리에서 멈춘다.
            # 대시보드 폼으로 만든 사이트가 여기 걸린다(프롬프트 초안은 채팅 몫).
            brain["no_prompts"] = [r[0] for r in conn.execute(
                """SELECT p.name FROM projects p
                     LEFT JOIN ai_prompts a ON a.project_id=p.id AND a.is_active=1
                    GROUP BY p.id HAVING COUNT(a.id)=0""")] if brain["tables"] else []
            conn.close()
        except Exception as e:
            brain["error"] = str(e)
    # 호스팅 연결 — 웹을 먼저 쓰던 사람이 플러그인을 깔면 여기가 비어 있다. 자동으로는
    # 못 붙는다(플러그인은 사용자가 누구인지 모른다 — 신원은 웹이 발급한 토큰뿐이다) — 웹
    # [설정]의 "명령어로 연결하기" 한 줄이 유일한 경로다. 그래서 첫 진단이 그걸 묻는다.
    rc = remote.config()
    linked = {"linked": bool(rc), "url": (rc or {}).get("url", ""),
              "projects": list((rc or {}).get("projects") or [])}
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
    # 토큰은 있는데(연결됨) 그 토큰이 지금 SCOPES 를 다 못 덮을 수 있다 — analytics.readonly
    # 가 나중에 추가돼서, 그 전에 로그인해 둔 사람은 GA4 수집이 403 으로 막힌다.
    gsc_missing = gsc_missing_scopes() if gsc_conn else []
    # 스코프가 모자라도 GA4 를 아직 안 붙인 사람에게는 막힌 일이 없다 —
    # 검색 실적 수집은 그대로 돈다. 속성을 이미 연결한 프로젝트가 있을 때만
    # 전체 판정(verdict)까지 올린다. 아니면 할 일 목록에만 남긴다.
    ga4_linked = False
    if gsc_missing:
        try:
            c = db.connect()
            try:
                ga4_linked = bool(c.execute(
                    "SELECT 1 FROM projects WHERE ga4_property IS NOT NULL "
                    "AND ga4_property<>'' LIMIT 1").fetchone())
            finally:
                c.close()
        except Exception:
            ga4_linked = False        # Brain 이 없거나 옛 스키마면 판단 자체가 불가
    # Brain 은 컴퓨터 전역이라 "지금 이 폴더가 어느 사이트냐"를 따로 정해야 한다.
    # 예전에는 projects[0](먼저 등록한 것)을 집어서, 사이트와 무관한 다른 리포에서
    # /setup 을 돌려도 늘 같은 사이트를 띄웠다 (사용자 신고).
    brain["repo_match"] = db.repo_project()
    # 호출자가 사이트를 명시했으면 그것이 폴더 추론을 이긴다 — stage.setup_payload 와
    # 같은 규칙이다(화면이 고른 것 > 이 폴더의 리포 > 하나뿐이면 그것 > 못 고름).
    # 원격 CLI 는 `--project` 로 사이트를 대므로 여기서 다시 추측하면 안 된다.
    brain["picked"] = (project if project in brain["projects"]
                       else stage.pick_project(brain["projects"]))
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

    # 명부(CAPABILITIES)를 지금 상태로 렌더한다 — 여기 있는 건 "켜졌나"의 판정뿐이고,
    # 이름·설명·비용·발급처·고치는 문구는 전부 모듈 수준 명부가 갖는다.
    is_on = {
        "keywords": core_ok,
        "brain": core_ok and brain_ok,
        "create": core_ok,
        "ai": core_ok and keys["openrouter"],
        "gsc": core_ok and all(deps_gsc.values()) and gsc_linked,
        "rank": core_ok and (keys["dataforseo"] or keys["serper"]),
    }
    gsc_fix_state = ("deps" if gsc_mode and not all(deps_gsc.values())
                     else "pending" if gsc_pending else "none")
    readiness = []
    for c in CAPABILITIES:
        fix = c["fix"]
        if isinstance(fix, dict):
            fix = fix[gsc_fix_state]
        readiness.append({**c, "keys": list(c["keys"]),
                          "on": is_on[c["id"]], "fix": fix,
                          # 로컬은 준비물이 전부 사용자 몫이다 — 명부값은 호스팅용.
                          "owner": c["owner"] if hosted else "user"})

    must = []
    if not core_ok:
        must.append({
            "id": "core_deps",
            "msg": f"기본 부품 설치 — `{CORE_PIP}` (1분). "
                   "이것만 하면 기능 대부분이 켜집니다. 채팅에 \"설치해줘\" 하시면 "
                   "제가 대신 실행합니다."
        })
    if not brain_ok:
        must.append({
            "id": "brain_broken",
            "msg": f"보관함 파일이 손상됐습니다 — {DB} 를 다른 이름으로 옮기면 "
                   "다음 실행 때 새로 만들어집니다 (지금까지 모은 자료는 사라짐)"
        })
    if core_ok and brain_ok and brain["projects"] and not brain["picked"]:
        must.append({
            "id": "pick_project",
            "msg": ("이 폴더가 어느 사이트인지 모르겠습니다 — 등록된 사이트: "
                    + ", ".join(brain["projects"]) +
                    ". 채팅에 이름을 말씀해 주시면 그 사이트로 진행합니다. "
                    "이 폴더가 아직 등록 안 된 새 사이트면 `/capture add <원하는이름>`. "
                    "매번 안 묻게 하려면 이 폴더에서 `/create profile <이름>` 을 한 번 "
                    "돌리세요 — 리포 경로가 기록돼 다음부터 이 폴더에서 자동으로 붙습니다.")
        })
    if core_ok and brain_ok and not brain["projects"]:
        # 보관함이 비었고 호스팅도 안 붙었다 — 웹을 먼저 쓰던 사람이면 등록을 다시
        # 시키는 게 아니라 연결이 답이다. 한 번만 묻고, 아니면 로컬 등록으로 간다.
        if linked["linked"]:
            msg = ("첫 사이트 등록 — 채팅에 `/capture add <원하는이름>` 이라고 "
                   "하시면 제가 물어보면서 만들어 드립니다.")
        else:
            msg = ("웹(호스팅)에서 이미 쓰고 계셨나요? 그러면 웹 [설정] > "
                   "'명령어로 연결하기' 에서 나오는 한 줄을 채팅에 그대로 붙여 주세요 — "
                   "등록해 둔 사이트가 여기서도 같은 명령으로 돌아갑니다. "
                   "아니면 첫 사이트 등록 — 채팅에 `/capture add <원하는이름>` 이라고 "
                   "하시면 제가 물어보면서 만들어 드립니다.")
        must.append({"id": "first_project", "msg": msg})
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
    # analytics.readonly 가 나중에 추가된 스코프라, 그 전에 로그인해 둔 토큰은
    # GA4 를 못 읽는다 — 조용히 403 으로 막히기 전에 여기서 먼저 말한다.
    if core_ok and brain_ok and gsc_missing:
        must.append({
            "id": "gsc_rescope",
            "msg": "구글 재로그인 필요 (무료) — 저장된 로그인에 GA4 읽기 권한이 없어 "
                   "GA4 수집이 403 으로 막힙니다 (검색 실적 수집은 그대로 됩니다). "
                   + (f"기존 토큰이 유효하면 \"GSC 로그인해줘\"가 그걸 그대로 재사용해 "
                      f"재동의 창이 안 뜹니다 — 먼저 토큰을 지우세요: {db.gsc_token()} "
                      "그다음 채팅에 \"GSC 로그인해줘\"." if not hosted else
                      "대시보드에서 구글 계정을 다시 연결해 주세요 — 재로그인하면 "
                      "새 권한까지 다시 동의합니다.")
        })

    later = []
    if brain["no_prompts"]:
        who = ", ".join(brain["no_prompts"])
        later.append(f"AI에 물어볼 질문 만들기 ({who}) — 지금 이 사이트로 AI 노출 확인을 "
                     f"돌리면 \"질문이 없다\"며 멈춥니다. 채팅에 `/capture add {who.split(', ')[0]}` "
                     "이라고 하시면 사이트에 맞는 질문 10~30개를 만들어 드립니다 "
                     "(1분, 무료). 다른 기능은 지금도 다 됩니다.")
    # 유료 키 안내를 여기 다시 적지 않는다 — 명부/locked 가 이미 같은 두 항목을
    # (발급 주소까지 붙여서) 말한다. 두 벌이 되면 화면이 "못 하는 것" 목록으로
    # 채워지고, 실제로 한쪽만 낡았다(여기 있던 사본은 링크로 바꾸기 전의 "setup.md
    # 5절"을 계속 가리키고 있었다).
    # 구글 연동 부품(google-auth-oauthlib 등)도 여기 적지 않는다 — 명부의
    # "구글 실적 읽기" 잠금 사유가 부품 누락을 먼저 보고 그 pip 명령을 말한다.
    # 로그인 창을 여는 게 그 부품이라, 없으면 "로그인해줘"가 그 자리에서 막힌다.

    # 마케팅 스킬 항목은 show_setup 판정(대시보드가 패널을 펼치는 여부)에서 빼기 위해
    # 메시지를 변수로 보관한다 — 그래야 doctor가 결론을 찍고, 대시보드는 그걸 읽기만
    # 한다.
    # 호스팅에는 아예 해당 없다 — 스킬은 Claude Code 에 까는 것이고, 웹 사용자에겐
    # 깔 곳도 재시작할 것도 없다. 키 유무로 안 갈리는 항목이라 hosted 로만 가른다.
    marketing_skills_msg = None
    missing_marketing = [] if hosted else [k for k, ok in marketing_skills.items() if not ok]
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
            f"직접 하시려면: {SKILLS_REPO}"
        )
        # [꼭 해야 할 일]이 아니다 — 이 메시지 자신이 "없어도 측정·수집은 그대로"라고
        # 말한다. must 로 두면 첫 세션이 **Claude Code 재시작**으로 끊긴다 (온보딩에서
        # 제일 비싼 이탈 지점인데, 정작 측정에는 필요 없는 항목이다). 권하는 자리는
        # 첫 리포트가 나온 뒤다 — 그때는 "각도를 더 깊게"가 실제 이득이라 재시작 값을 한다.
        later.append(marketing_skills_msg)
    elif not hosted and not marketing_optional.get("aso", True):
        marketing_skills_msg = (
            "aso (앱 스토어 리스팅 최적화) — 앱이 있는 사이트만 필요합니다. "
            "채팅에 \"마케팅 스킬 설치해줘\" 하시면 제가 대신 설치합니다 (무료). "
            "끝나면 Claude Code 재시작이 한 번 필요합니다. "
            f"직접 하시려면: {SKILLS_REPO}"
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
    elif gsc_missing and ga4_linked:
        verdict = "구글 재로그인이 한 번 더 필요합니다 — GA4 를 연결해 두셨는데 저장된 토큰에 그 권한이 없습니다."
        next_cmd = "구글 계정 다시 연결해줘" if hosted else "토큰 삭제하고 GSC 로그인해줘"
    elif brain["picked"]:
        verdict = "다 준비됐습니다 — 바로 쓰시면 됩니다."
        next_cmd = f"/capture run {brain['picked']}"
    else:
        # 사이트가 여럿인데 이 폴더가 어느 것인지 모른다. 아무거나 집어서 보여주면
        # 사용자는 그게 이 폴더의 사이트인 줄 안다 — 조용히 틀리느니 물어본다.
        verdict = (f"등록된 사이트가 {len(brain['projects'])}개인데 이 폴더가 어느 "
                   "것인지 모르겠습니다 — 이름을 하나 말씀해 주세요.")
        next_cmd = None

    steps = ([m["msg"] if isinstance(m, dict) else m for m in must]
             + [f"{LATER_TAG} {s}" for s in later])
    # 마케팅 스킬은 이제 must 에 안 들어간다(위 [선택] 강등). must_other 키는 대시보드
    # show_setup 판정이 읽으므로 이름만 남겨 둔다 — 지금은 must 와 같은 값이다.
    must_other = list(must)
    return {"deps_core": deps_core, "deps_gsc": deps_gsc, "brain": brain,
            "remote": linked,   # 호스팅 연결 — remote.config() 를 그대로 렌더한 것
            "keys": keys, "gsc_sites": gsc_sites, "gsc_legacy": gsc_legacy,
            "gsc_mode": gsc_mode,   # "oauth" | "service_account" | "" (db.gsc_auth)
            # gsc_mode는 남긴다 — 대시보드가 이 JSON을 먹으므로 기존 키를 없애면 화면이 깨진다.
            "gsc_connected": gsc_conn,    # 3-상태의 정본: 연결됨 / (mode만 있으면)로그인 대기 / 없음
            "gsc_bundled": gsc_bundled,   # 번들 클라이언트로 로그인하는 중인가
            "gsc_missing_scopes": gsc_missing,   # 연결된 토큰에 없는 SCOPES (보통 GA4)
            "capabilities": {f"{c['name']} — {c['desc']}": c["on"] for c in readiness},
            # owner/blocking 도 같이 실어 보낸다 — 화면(setup_payload)이 "이건 누구
            # 할 일인가"를 문구에서 되짚지 않고 축으로 판단한다.
            "locked": [{"name": c["name"], "desc": c["desc"], "fix": c["fix"],
                        "cost": c["cost"], "keys": c["keys"], "url": c["url"],
                        "owner": c["owner"], "blocking": c["blocking"]}
                       for c in readiness if not c["on"]],
            "readiness": readiness,   # 명부를 지금 상태로 렌더한 것 (정본은 CAPABILITIES)
            "buckets": {"must": BUCKET_MUST, "later": BUCKET_LATER,
                        "later_tag": LATER_TAG},
            "skills_all": ALL_SKILLS, "skills_repo": SKILLS_REPO,
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
                if d.get("gsc_missing_scopes"):
                    tag += " — GA4 읽기 권한 없음, 재로그인 필요"
            elif mode:
                # 새로 생긴 상태다. 예전엔 인증 파일이 있으면 곧 연결됨이었는데,
                # 번들 클라이언트가 항상 존재하면서 그 등식이 깨졌다.
                tag = "로그인 대기 — 구글 로그인 한 번이면 끝납니다 (\"GSC 로그인해줘\")"
            elif d["gsc_legacy"].get(name):
                tag = ("예전 방식 토큰만 있음 — 이제 안 씁니다. 다시 연결하세요 "
                       "(connect_gsc.py)")
            else:
                tag = "인증 없음 — 클라이언트 파일부터 놓아야 합니다 (\"GSC 연동해줘\")"
            # 어느 사이트를 보고 있는지 조용히 정하지 않는다 — 화면에 이유까지 적는다.
            if name == d["brain"].get("picked"):
                mark = ("  ← 이 폴더의 사이트" if d["brain"].get("repo_match") == name
                        else "  ← 등록된 사이트가 이것뿐")
            else:
                mark = ""
            print(f"  · {name} — 구글 자동 수집: {tag}{mark}")
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
    # 모수는 위임 스킬 **전체**(필수+선택)다 — 산문도 install_skills 도 같은 8을 센다.
    m_skills = {**d.get("marketing_skills", {}), **d.get("marketing_optional", {})}
    if m_skills:
        installed = sum(1 for v in m_skills.values() if v)
        total = len(m_skills)
        missing = [k for k, v in m_skills.items() if not v]
        if missing:
            print(f"마케팅 스킬: {installed}/{total} (누락: {', '.join(missing)})")
        else:
            print(f"마케팅 스킬: {installed}/{total} (모두 설치됨)")

    if d["must"]:
        print(f"\n[{BUCKET_MUST}]")
        for i, s in enumerate(d["must"], 1):
            msg = s["msg"] if isinstance(s, dict) else s
            print(f"  {i}. {msg}")

    # 선택 항목은 한 통에 담고 부정 문구는 머리에서 한 번만 말한다. 전에는
    # "아직 안 켠 것"과 "나중에 하면 좋은 것" 두 통이었고 같은 항목이 양쪽에 들어가,
    # 부담을 줄이려던 문구가 오히려 화면 절반을 "못 하는 것" 목록으로 만들었다.
    if d["locked"] or d["later"]:
        print(f"\n{BUCKET_LATER} (전부 선택 — 안 하셔도 위 기능은 그대로입니다)")
        for c in d["locked"]:
            print(f"  · {c['name']} — {c['desc']}")
            print(f"    {c['fix'] or f'위 [{BUCKET_MUST}]만 끝내면 자동으로 켜집니다.'}")
        for s2 in d["later"]:
            print(f"  · {s2}")

    print(f"\n자료 폴더: {d['brain']['home']}")
    r = d.get("remote") or {}
    if r.get("linked"):
        print(f"호스팅 연결: {r['url']} (웹에 등록된 사이트 {len(r['projects'])}개)")
    else:
        print("호스팅 연결: 없음 — 이 컴퓨터의 보관함만 씁니다")


def _selfcheck() -> None:
    """명부가 **데이터**로 남아 있는지 지키는 자체점검 (assert 기반, 의존성 없음).

    깨지면 누군가 산문을 코드로 되돌린 것이다 — 화면 문구를 함수 안에 다시 적었거나,
    개수를 명부 밖에서 세었거나(그래서 "7개 중 8개"가 찍혔다).
    """
    ids = [c["id"] for c in CAPABILITIES]
    assert len(ids) == len(set(ids)), ids
    for c in CAPABILITIES:
        for f in ("id", "name", "desc", "cost", "keys", "url", "fix"):
            assert f in c, (c.get("id"), f)
        assert c["cost"] in ("무료", "유료"), c["id"]
        # 유료면 키 이름과 발급처를 명부가 갖는다 — 산문이 URL을 다시 적지 않게.
        assert bool(c["keys"]) == (c["cost"] == "유료"), c["id"]
        assert bool(c["keys"]) == c["url"].startswith("http"), c["id"]
        if isinstance(c["fix"], dict):
            assert set(c["fix"]) == {"deps", "pending", "none"}, c["id"]
        # 두 축은 값이 정해져 있다 — 문자열 오타 하나면 판정이 조용히 뒤집힌다.
        assert c["owner"] in ("user", "server"), c["id"]
        assert isinstance(c["blocking"], bool), c["id"]
        if c["cost"] == "유료":
            # 호스팅에선 서버가 낸다(그래서 사용자 할 일 목록에서 빠진다) — 그리고
            # 유료는 무엇도 막지 않는다. 로컬에서 무료로 쓰는 사람도 아하 모먼트까지 간다.
            assert c["owner"] == "server", c["id"]
            assert not c["blocking"], c["id"]
    # 구글 로그인은 서버가 대신해 줄 수 없고, 실측 없이는 판정에 재료가 없다.
    gsc = next(c for c in CAPABILITIES if c["id"] == "gsc")
    assert gsc["owner"] == "user" and gsc["blocking"], gsc

    d = diagnose()
    assert len(d["readiness"]) == len(CAPABILITIES)
    assert set(d["capabilities"]) == {f"{c['name']} — {c['desc']}" for c in CAPABILITIES}
    for r in d["readiness"]:
        assert isinstance(r["fix"], (str, type(None))), r["id"]   # 3-상태가 풀렸나
        assert r["on"] or {"name": r["name"], "desc": r["desc"], "fix": r["fix"],
                           "cost": r["cost"], "keys": r["keys"], "url": r["url"],
                           "owner": r["owner"],
                           "blocking": r["blocking"]} in d["locked"], r["id"]
    assert d["buckets"] == {"must": BUCKET_MUST, "later": BUCKET_LATER,
                            "later_tag": LATER_TAG}
    assert all(s.startswith(LATER_TAG) for s in d["next_steps"][len(d["must"]):])
    # 로컬(표식 없음)에서는 준비물이 전부 사용자 몫이다.
    assert all(r["owner"] == "user" for r in d["readiness"]), "로컬인데 서버 몫이 있다"

    # 호스팅 판정 — 표식이 서면 명부의 owner 를 그대로 쓰고, 서버 몫은 사용자 할 일
    # 목록(extra)에서 빠진다. 결함 1: 키를 낼 필요 없는 사람에게 키 발급을 시켰다.
    _saved = os.environ.get(HOSTED_ENV)
    try:
        os.environ[HOSTED_ENV] = "1"
        h = diagnose()
        assert {r["id"]: r["owner"] for r in h["readiness"]} == \
            {c["id"]: c["owner"] for c in CAPABILITIES}
        assert h["marketing_skills_msg"] is None, "호스팅인데 스킬 설치를 시킨다"
        # Brain 을 건드리지 않으려고 사이트 목록을 비운 채 화면 payload 만 만든다.
        payload = stage.setup_payload({**h, "brain": {**h["brain"], "projects": []}})
        for c in h["locked"]:
            if c["owner"] == "server":
                assert not any(e.startswith(c["name"]) for e in payload["extra"]), c["name"]
    finally:
        os.environ.pop(HOSTED_ENV, None) if _saved is None \
            else os.environ.__setitem__(HOSTED_ENV, _saved)

    # 호스팅 연결 판정 — 빈 보관함 + 연결 없음이면 첫 항목이 웹 연결을 묻고,
    # 연결돼 있으면 묻지 않는다. 진짜 home 을 안 건드리려고 임시 CAPTURE_HOME 을 쓴다.
    import tempfile
    _keep = {k: os.environ.get(k) for k in ("CAPTURE_HOME", "CAPTURE_DB")}
    with tempfile.TemporaryDirectory() as tmp:
        try:
            os.environ["CAPTURE_HOME"] = tmp
            os.environ.pop("CAPTURE_DB", None)
            e = diagnose()
            assert e["remote"] == {"linked": False, "url": "", "projects": []}, e["remote"]
            first = next((m for m in e["must"] if m["id"] == "first_project"), None)
            if first:   # core 부품이 없으면 이 항목 자체가 안 뜬다 — 그건 여기 관심사가 아니다
                assert "명령어로 연결하기" in first["msg"], first["msg"]
            remote._save({"url": "https://h.example", "token": "smt_x",
                          "projects": ["a", "b"]})
            e = diagnose()
            assert e["remote"] == {"linked": True, "url": "https://h.example",
                                   "projects": ["a", "b"]}, e["remote"]
            first = next((m for m in e["must"] if m["id"] == "first_project"), None)
            if first:
                assert "명령어로 연결하기" not in first["msg"], first["msg"]
        finally:
            for k, v in _keep.items():
                os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)

    # 개수 산술 — install_skills 의 모수와 명부가 같아야 한다.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import install_skills
    assert dict(ALL_SKILLS) == {**MARKETING_SKILLS, **OPTIONAL_SKILLS}
    assert len(ALL_SKILLS) == len(MARKETING_SKILLS) + len(OPTIONAL_SKILLS)
    missing = install_skills.get_missing_skills()
    assert set(missing) <= set(ALL_SKILLS), missing
    assert len(missing) <= install_skills.total_skills(), missing
    print(f"selfcheck ok — 기능 {len(CAPABILITIES)}개 · 위임 스킬 "
          f"{len(ALL_SKILLS)}개(필수 {len(MARKETING_SKILLS)} + 선택 "
          f"{len(OPTIONAL_SKILLS)}) · 버킷 [{BUCKET_MUST}] / {BUCKET_LATER}")


def _arg(flag: str) -> str:
    """`--flag VALUE` 하나를 꺼낸다 — doctor 는 argparse 를 안 쓴다(플래그가 몇 개뿐)."""
    a = sys.argv
    return a[a.index(flag) + 1] if flag in a[:-1] else ""


def main() -> None:
    if "--selfcheck" in sys.argv:
        _selfcheck()
        return
    project = _arg("--project")
    # --web 은 화면을 띄우는 명령이라 여기서 가로채지 않는다 — 텍스트 진단만 넘긴다.
    if project and "--web" not in sys.argv and remote.owns(project):
        # 서버가 자기 env(유료 키 포함) 안에서 진단한다 — 로컬 키 유무로 거짓
        # 판정하지 않으려는 것. 렌더러는 아래 render() 한 벌뿐이다.
        d = remote.api("GET", "/api/doctor", params={"project": project})
        if "capabilities" not in d:
            sys.exit("원격 /api/doctor 가 진단 페이로드(doctor.diagnose)가 아닌 것을 "
                     "돌려줬습니다 — 서버 라우트를 확인하세요.")
        if "--json" in sys.argv:
            print(json.dumps(d, ensure_ascii=False, indent=2))
        else:
            render(d)
        sys.exit(0 if d.get("core_ok") and d.get("brain_ok") else 1)
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
