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
import re
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
    "product-marketing": "사이트 포지셔닝 문서 정리",
    "seo-audit":         "색인 차단·순위 하락 진단",
    "ai-seo":            "AI가 남을 인용할 때 원인 해석",
    "content-strategy":  "키워드를 주제 묶음으로 정리",
    "site-architecture": "내부 경쟁·URL 구조 정리",
    "programmatic-seo":  "같은 틀로 페이지 여러 장 만들기",
    "schema":            "구조화 데이터 넣기",
}
OPTIONAL_SKILLS = {
    "aso": "앱 스토어 리스팅 정비 (앱이 있는 사이트만)",
}
# 위임 스킬 **전체** — 개수와 설명의 정본. 세는 쪽은 전부 여기서 센다.
# (예전엔 install_skills 가 필수 7개를 모수로 잡고 필수+선택 8개를 나열해서,
#  깨끗한 머신에서 "7개 중 8개가 설치되어 있지 않습니다"를 찍었다.)
ALL_SKILLS = {**MARKETING_SKILLS, **OPTIONAL_SKILLS}
SKILLS_REPO = "https://github.com/coreyhaines31/marketingskills"

# 버킷 라벨 — doctor 화면과 산문이 **같은 말**을 쓰게 하는 정본. 산문이 이걸
# 다시 타이핑하면 doctor 가 찍지도 않는 머리말을 찾게 만든다(실제로 그랬다:
# 문서는 [나중에 하면 좋은 것], 화면은 [선택]).
# 이 폴더 개념이라 **웹에는 뜻이 없는** must 항목. 호스팅의 사이트 선택은 레일이
# 하고, brain["picked"] 는 폴더 매칭이라 웹에서는 늘 비어 있다 — 안 빼면 사이트를
# 이미 고른 사람에게도 "고르세요"가 영영 떠 있는다.
LOCAL_ONLY_MUST = {"pick_project"}

# 할 일이 하나도 없을 때만 쓰는 요약. 위 분기 구조가 그것을 보장한다 —
# 목록과 어긋나는 요약("다 준비됐는데 GA4 가 막혔다")이 나올 길을 구조로 막는다.
READY_VERDICT = "다 준비됐습니다. 바로 쓰시면 됩니다."

BUCKET_MUST = "꼭 해야 할 일"
BUCKET_LATER = "더 켜고 싶으면"
LATER_TAG = "[선택]"

# 복구를 유발하는 채팅 문구. must 의 cmd 와 verdict 의 next_command 가 같은 것을
# 가리켜야 한다 — 두 벌이 되면 화면과 CLI 가 서로 다른 말을 시킨다.
RESCOPE_CMD = "토큰 삭제하고 GSC 로그인해줘"
BRAIN_RESET_CMD = "보관함 파일 다시 만들어줘"

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
    {"id": "ai", "name": "AI 인용 확인",
     "desc": "ChatGPT·Perplexity·Gemini가 내 글을 인용하는지 검사",
     "cost": "유료", "keys": ("OPENROUTER_API_KEY",),
     "url": "https://openrouter.ai/keys",
     "fix": "없어도 기회 목록까지는 그대로 갑니다. OpenRouter 키를 넣으면 이 단계가 "
            "켜집니다. https://openrouter.ai/keys 에서 크레딧을 조금 충전하고 키를 "
            "만드시면 됩니다.",
     # 호스팅에선 서버가 낸다. 로컬에서 키가 없으면 이 단계만 건너뛴다 —
     # 무료로 쓰는 사람도 기회 목록까지는 간다.
     "owner": "server", "blocking": False},
    {"id": "gsc", "name": "검색 실적 수집",
     "desc": "서치콘솔에서 노출·클릭·평균 순위를 그대로 수집 (필수 연결)",
     "cost": "무료", "keys": (), "url": "",
     # 콘솔 작업은 이제 없다 — 번들 클라이언트가 그 자리를 대신한다.
     # 예전 문구("무료, 5분")는 사용자에게 있지도 않은 할 일을 시키는 거짓말이 됐다.
     "fix": {
         "deps": "구글 연동 부품이 빠져 로그인 창이 안 열립니다. Claude 에게 "
                 "설치를 부탁하시면 대신 설치합니다. [설정] 화면에 버튼도 있습니다.",
         "pending": "구글 계정으로 로그인 한 번이면 켜집니다. 30초 걸리고 준비물은 "
                    "없습니다. Claude 에게 구글 로그인을 부탁하시면 브라우저 창이 "
                    "열립니다.",
         "none": "이 배포에 구글 로그인용 클라이언트 파일이 없습니다. Claude 에게 "
                 "구글 연동을 부탁하시면 파일 놓는 것부터 안내합니다. 무료입니다.",
     },
     # 구글 로그인은 양쪽 다 사용자 몫이다(서버가 대신 로그인할 수 없다).
     # 실측이 없으면 모든 판정에 재료가 없다 — 아하 모먼트를 막는다.
     "owner": "user", "blocking": True},
    # 위 fix 는 로컬(플러그인) 문구다 — 채팅과 pip 이 있는 사람에게만 뜻이 있다.
    # 호스팅에는 채팅도 셸도 없으므로 같은 3-상태를 웹 문구로 한 벌 더 갖는다
    # (아래 GSC_FIX_WEB). 두 벌이 아니라 두 청중이다 — 키는 반드시 같다.
    {"id": "rank", "name": "순위 확인",
     "desc": "검색 결과에서 몇 위인지 매일 기록",
     "cost": "유료",
     "keys": ("DATAFORSEO_LOGIN", "DATAFORSEO_PASSWORD", "SERPER_API_KEY"),
     "url": "https://dataforseo.com",
     "fix": "없어도 기회 목록까지는 그대로 갑니다. DataForSEO 아이디와 비밀번호를 "
            "넣으면 이 단계가 켜집니다. https://dataforseo.com 에 가입하면 $1 "
            "크레딧이 붙고, API 비밀번호는 계정 비밀번호와 달라 API Settings 에서 "
            "따로 봅니다. 더 싸게 쓰시려면 https://serper.dev 도 됩니다 — 무료 "
            "2,500콜에 카드가 필요 없고, 대신 구글 AI 요약은 못 봅니다.",
     "owner": "server", "blocking": False},
)

# 구글 연결이 잠겼을 때 **웹 사용자**에게 할 말. 키는 위 gsc fix 와 같은 3-상태다
# (_selfcheck 가 대조한다). 웹에는 채팅도 파일 시스템도 없으므로 명령·경로를
# 넣지 않는다 — 넣으면 할 수 없는 일을 시키는 안내가 된다.
GSC_FIX_WEB = {
    "deps": "서버에 구글 연동 부품이 빠졌습니다. 잠시 뒤 다시 시도해 주세요.",
    "pending": "구글 계정을 연결하면 켜집니다. 로그인 한 번이면 끝나고 준비물은 "
               "없습니다.",
    "none": "서버에 구글 로그인 설정이 없습니다. 잠시 뒤 다시 시도해 주세요.",
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


def _drop_local_only(must: list, hosted: bool) -> list:
    """호스팅에서 뜻이 없는 항목을 뺀다. 판정은 항목의 id 하나다.

    조건문 안에 숨기지 않고 여기로 뺀 이유: 이 머신의 상태가 어떻든 selfcheck 가
    가짜 목록을 먹여 실제로 걸러지는지 볼 수 있어야 한다(대부분의 머신에서는
    그 갈래가 아예 안 만들어져서, 조건문에 숨기면 검사가 아무것도 안 본다).
    """
    return [m for m in must
            if not (hosted and isinstance(m, dict) and m.get("id") in LOCAL_ONLY_MUST)]


def probe_keys() -> list[str]:
    """유료 키가 **실제로 살아 있나** — 무료 호출 한 번씩. 사람이 읽을 한 줄씩 반환.

    "키가 env 에 있다"와 "그 키로 살 수 있다"는 다른 말이다. 잔액 0 인 계정이
    키를 다 갖고 있어서 doctor 는 "다 준비됐습니다"라고 했고, 자동 런은 402 를
    100번 맞았다(8/30). 판정 함수의 정본은 수집기 쪽 하나다 — 여기서 다시 안 쓴다.

    이 파일은 pip 이전에도 도는 자리라 requests 를 쓰는 모듈은 늦게 읽는다.
    """
    out: list[str] = []
    try:
        import collect_ai
        import serp_adapter
    except Exception:
        return out          # 부품이 없으면 이 줄은 생략 — 진단 자체를 막지 않는다
    if serp_adapter.has_dataforseo():
        try:
            out.append(f"DataForSEO 잔액 ${serp_adapter.dataforseo_balance():.2f}")
        except Exception as e:
            out.append(f"DataForSEO 잔액 확인 실패: {e}")
    if serp_adapter.has_openrouter():
        out.append(collect_ai.openrouter_ok()[1])
    return out


def diagnose(project: str = "", *, probe: bool = False) -> dict:
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
            fix = (GSC_FIX_WEB if hosted else fix)[gsc_fix_state]
        readiness.append({**c, "keys": list(c["keys"]),
                          "on": is_on[c["id"]], "fix": fix,
                          # 로컬은 준비물이 전부 사용자 몫이다 — 명부값은 호스팅용.
                          "owner": c["owner"] if hosted else "user"})

    # 여기서부터 사용자에게 그대로 찍히는 문장이다. 두 축으로 갈린다.
    #
    #   청중  로컬(채팅·셸이 있다)과 호스팅(둘 다 없다)은 **할 수 있는 행동이
    #         다르므로** 문장도 갈린다. 판정은 hosted 하나다.
    #   자리  msg 는 결론 한 줄이다 — 그것만 읽어도 뜻이 서야 한다(최대 두 문장).
    #         나머지 사정은 detail 에 둔다: 화면이 <details>[자세히] 로 접고, 없으면
    #         None 이라 접을 것도 안 만든다. 5문장짜리 배너가 띠와 "다음에 누를 것"을
    #         두고 경합하던 자리다. 행동(cmd)은 detail 안에 넣지 않는다 — 접힌 곳에
    #         행동을 숨기면 누를 것이 안 보인다.
    #   자리  go 는 "누를 곳"이다 — {"label": 버튼 글자, "href": 갈 주소} 아니면 None.
    #         로컬은 cmd(복사 칩)가 행동을 갖고 있어 대개 None 이고, 호스팅은 칠
    #         곳이 없어 여기가 유일한 행동이다. **셸이 id 별 행동표를 갖지 않게**
    #         서버가 목적지까지 준다 — 그 표는 배포가 갈리면 반드시 낡는다.
    #         누를 곳이 정말 없는 항목(서버 쪽 문제)은 None 이고, 그때는 msg 가
    #         "잠시 뒤 다시 시도해 주세요"로 왜 없는지 말한다. _selfcheck 가 그
    #         둘 중 하나는 있는지 본다.
    #         msg·detail 둘 다 명령·파일 경로·따옴표 친 채팅 문구를 **넣지
    #         않는다**. 그대로 복사해 쓰는 것 한 줄은 cmd 에만 둔다(로컬 전용,
    #         호스팅은 항상 None). 안내 화면 단계의 cmd 와 같은 뜻이고 같은 칩으로
    #         그려진다. 화면이 아직 칩을 안 그려도 msg 는 혼자 읽힌다 — 그래서
    #         "아래" 같은 지시어를 쓰지 않는다.
    must = []
    # 이 폴더 개념이라 **웹에는 뜻이 없는** 항목. 호스팅의 사이트 선택은 레일이
    # 하고, brain["picked"] 는 폴더 매칭이라 웹에서는 늘 비어 있다 — 그대로 두면
    # 사이트를 이미 고른 사람에게도 "고르세요"가 영영 떠 있는다.
    if not core_ok:
        must.append({
            "id": "core_deps",
            "msg": "서버에 기본 부품이 빠졌습니다. 잠시 뒤 다시 시도해 주세요."
                   if hosted else
                   "기본 부품을 설치하면 기능 대부분이 켜집니다. 1분 걸립니다.",
            "detail": None if hosted else
                      "Claude 에게 설치를 부탁하시면 대신 실행합니다.",
            "go": None,                       # 서버가 고칠 일 / 로컬은 cmd 가 행동
            "cmd": None if hosted else CORE_PIP,
        })
    if not brain_ok:
        must.append({
            "id": "brain_broken",
            "msg": "보관함 파일이 손상돼 자료를 못 읽습니다. 잠시 뒤 다시 시도해 "
                   "주세요." if hosted else
                   "보관함 파일이 손상돼 자료를 못 읽습니다. 새로 만들면 지금까지 "
                   "모은 자료는 사라집니다.",
            "detail": None if hosted else
                      "그 파일을 옆으로 치우면 다음 실행 때 새로 만듭니다. Claude "
                      "에게 부탁하시면 치우는 것부터 대신 합니다.",
            "go": None,
            "cmd": None if hosted else BRAIN_RESET_CMD,
        })
    if core_ok and brain_ok and brain["projects"] and not brain["picked"]:
        must.append({
            "id": "pick_project",
            # 웹에는 "이 폴더"가 없다 — 사이트는 화면 위에서 고르는 것이다.
            # 요약(verdict)이 이미 상황을 말했으므로 여기서는 할 일만 적는다.
            "msg": ("등록된 사이트: " + ", ".join(brain["projects"]) +
                    ". 이 폴더의 사이트를 한 번 정해 두면 다음부터 자동으로 붙습니다."),
            "detail": "정해 두면 폴더 경로가 함께 기록됩니다.",
            "go": None,
            "cmd": "/create profile <이름>",
        })
    if core_ok and brain_ok and not brain["projects"]:
        # 보관함이 비었는데 호스팅에도 안 붙었다 — 웹을 먼저 쓰던 사람이면 등록을
        # 다시 시키는 게 아니라 연결이 답이다. 한 번만 묻고, 아니면 로컬 등록으로 간다.
        if hosted:
            msg, detail, cmd = ("사이트를 하나 등록하면 수집이 시작됩니다.",
                                "도메인과 검색어를 넣으면 첫 수집이 바로 돕니다.",
                                None)
            go = {"label": "사이트 고르기", "href": "/"}
        elif linked["linked"]:
            msg, detail, cmd = ("사이트를 하나 등록하면 수집이 시작됩니다.",
                                "Claude 에게 사이트 등록을 부탁하시면 물어보면서 "
                                "만들어 드립니다.", "/capture add <이름>")
            go = None
        else:
            msg, detail, cmd = (
                "웹에서 이미 쓰고 계셨다면 등록을 다시 하실 필요가 없습니다.",
                "웹 [설정]의 [명령어로 연결하기]가 주는 한 줄을 그대로 붙이시면 "
                "등록해 둔 사이트가 여기서도 같은 명령으로 돌아갑니다. 처음이시면 "
                "Claude 에게 사이트 등록을 부탁하시면 물어보면서 만들어 드립니다.",
                "/capture add <이름>")
            go = None
        must.append({"id": "first_project", "msg": msg, "detail": detail,
                     "go": go, "cmd": cmd})
    # GSC 연결은 필수다 — 실측(클릭·노출) 없이는 이 도구의 판정 전부가 재료가 없다.
    # (CSV 내보내기 임시 경로는 2026-08-18 정책으로 삭제 — 연결이 유일한 실적 경로.)
    if core_ok and brain_ok and gsc_pending:
        # 남은 게 로그인뿐인데 "연결하세요"라고만 하면, 사용자는 이미 끝난 콘솔 작업을
        # 다시 찾으러 간다. 다음 걸음은 **로그인을 유발하는 행동** 하나여야 한다.
        must.append({
            "id": "gsc_login",
            "msg": ("구글 계정을 연결하면 실적이 자동으로 쌓입니다. 로그인 한 번이면 "
                    "끝납니다." if hosted else
                    "구글 로그인 한 번이면 실적이 자동으로 쌓입니다. 계정당 1회, "
                    "30초 걸립니다."),
            "detail": (("속성마다 권한을 주는 단계는 없습니다." if hosted else
                        "준비물은 없고 속성마다 권한을 주는 단계도 없습니다. Claude "
                        "에게 구글 로그인을 부탁하시면 브라우저 창이 열립니다.")
                       + (" 로그인 창에 \"확인되지 않은 앱\" 경고가 한 번 뜨는데 "
                          "정상입니다. [고급] 다음 [이동]을 누르시면 됩니다."
                          if gsc_bundled else "")),
            "go": {"label": "구글 연결", "href": "/auth/login"} if hosted else None,
            "cmd": None if hosted else "GSC 로그인해줘",
        })
    if core_ok and brain_ok and not gsc_mode:
        must.append({
            "id": "gsc_client",
            "msg": "서버에 구글 로그인 설정이 없습니다. 잠시 뒤 다시 시도해 주세요."
                   if hosted else
                   "이 배포에 구글 로그인용 클라이언트 파일이 없어 로그인을 시작할 "
                   "수 없습니다.",
            "detail": None if hosted else
                      "Claude 에게 구글 연동을 부탁하시면 파일 놓는 것부터 "
                      "안내합니다. 내 클라이언트를 쓰는 길도 함께 알려 드립니다.",
            "go": None,
            "cmd": None if hosted else "GSC 연동해줘",
        })
    # analytics.readonly 가 나중에 추가된 스코프라, 그 전에 로그인해 둔 토큰은
    # GA4 를 못 읽는다 — 조용히 403 으로 막히기 전에 여기서 먼저 말한다.
    if core_ok and brain_ok and gsc_missing:
        must.append({
            "id": "gsc_rescope",
            "msg": "저장된 로그인에 GA4 읽기 권한이 없습니다. 검색 실적 수집은 "
                   "그대로 됩니다.",
            "detail": ("구글 계정을 다시 연결하면 새 권한까지 함께 동의합니다."
                       if hosted else
                       "저장된 로그인을 지우고 다시 로그인해야 새 권한까지 "
                       "동의합니다. 안 지우면 기존 로그인을 그대로 재사용해 동의 "
                       "창이 안 뜹니다. Claude 에게 부탁하시면 지우는 것부터 대신 "
                       "합니다."),
            "go": {"label": "구글 다시 연결", "href": "/auth/login"} if hosted else None,
            "cmd": None if hosted else RESCOPE_CMD,
        })

    must = _drop_local_only(must, hosted)

    later = []
    if brain["no_prompts"]:
        who = ", ".join(brain["no_prompts"])
        later.append(f"AI 인용 확인에 쓸 질문이 없는 사이트: {who}. 질문이 없으면 그 "
                     "단계가 바로 멈춥니다. 다른 기능은 지금도 다 됩니다. "
                     + ("[AI 인용] 화면에서 질문을 추가하면 켜집니다." if hosted else
                        "Claude 에게 질문 만들기를 부탁하시면 사이트에 맞는 질문 "
                        "10~30개를 만들어 드립니다. 1분, 무료입니다. 명령은 [안내] "
                        "화면의 AI 단계에 있습니다."))
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
            f"마케팅 스킬 {len(missing_marketing)}개를 깔면 글의 각도와 진단이 "
            "깊어집니다. 없어도 측정·수집은 그대로 됩니다. Claude 에게 마케팅 스킬 "
            "설치를 부탁하시면 대신 설치합니다. 무료입니다. "
            "끝나면 Claude Code 를 한 번 재시작해야 화면에 뜹니다. "
            f"빠진 것: {', '.join(missing_marketing)}. "
            f"직접 하시려면 {SKILLS_REPO}"
        )
        # [꼭 해야 할 일]이 아니다 — 이 메시지 자신이 "없어도 측정·수집은 그대로"라고
        # 말한다. must 로 두면 첫 세션이 **Claude Code 재시작**으로 끊긴다 (온보딩에서
        # 제일 비싼 이탈 지점인데, 정작 측정에는 필요 없는 항목이다). 권하는 자리는
        # 첫 리포트가 나온 뒤다 — 그때는 "각도를 더 깊게"가 실제 이득이라 재시작 값을 한다.
        later.append(marketing_skills_msg)
    elif not hosted and not marketing_optional.get("aso", True):
        marketing_skills_msg = (
            "앱 스토어 리스팅을 손보시려면 aso 스킬이 필요합니다. 앱이 있는 "
            "사이트만 해당합니다. Claude 에게 마케팅 스킬 설치를 부탁하시면 대신 "
            "설치합니다. 무료입니다. 끝나면 Claude Code 를 한 번 재시작해야 "
            f"화면에 뜹니다. 직접 하시려면 {SKILLS_REPO}"
        )
        later.append(marketing_skills_msg)

    # 한 줄 요약 + 다음 한 걸음 하나 — 읽는 사람이 이 두 줄만 봐도 되게.
    if not core_ok:
        verdict = ("서버에 기본 부품이 빠졌습니다." if hosted else
                   "기본 부품만 설치하면 대부분 켜집니다.")
        next_cmd = "pip install requests pyyaml"
    elif not brain_ok:
        verdict = "보관함 파일이 손상돼 자료를 못 읽습니다."
        next_cmd = None
    elif not brain["projects"]:
        verdict = "설치는 끝났습니다. 첫 사이트만 등록하면 시작합니다."
        next_cmd = "/capture add <원하는이름>"
    elif gsc_pending:
        verdict = ("구글 계정 연결 한 번만 남았습니다. 그래야 실적이 쌓입니다."
                   if hosted else
                   "구글 로그인 한 번만 남았습니다. 그래야 실적이 쌓입니다.")
        next_cmd = "GSC 로그인해줘"
    elif not gsc_mode:
        verdict = ("서버에 구글 로그인 설정이 없습니다." if hosted else
                   "구글 로그인용 클라이언트 파일이 이 배포에 빠져 있습니다.")
        next_cmd = "GSC 연동해줘"
    elif not hosted and brain["projects"] and not brain["picked"]:
        # 사이트가 여럿인데 이 폴더가 어느 것인지 모른다. 아무거나 집어서 보여주면
        # 사용자는 그게 이 폴더의 사이트인 줄 안다 — 조용히 틀리느니 물어본다.
        # (웹은 여기 안 온다: 폴더가 없으니 고르는 것은 레일 몫이다.)
        verdict = (f"등록된 사이트 {len(brain['projects'])}개 중 이 폴더가 어느 "
                   "것인지 모릅니다.")
        next_cmd = None
    elif gsc_missing:
        # GA4 를 아직 안 붙인 사람에게도 이 말은 사실이다 — 막힌 것이 GA4 하나뿐이고
        # 검색 실적은 그대로 돈다. 전에는 GA4 를 붙인 사람에게만 이 분기를 태웠는데,
        # 그러면 나머지 사람은 목록에 할 일이 남은 채 "다 준비됐습니다"를 읽었다.
        verdict = ("검색 실적은 그대로 됩니다. GA4 만 구글 계정을 다시 연결하면 "
                   "됩니다." if hosted else
                   "검색 실적은 그대로 됩니다. GA4 만 로그인을 다시 하면 됩니다.")
        next_cmd = "구글 계정 다시 연결해줘" if hosted else RESCOPE_CMD
    elif not must:
        verdict = READY_VERDICT
        next_cmd = f"/capture run {brain['picked']}" if brain["picked"] else None
    else:
        # 여기 오면 분기를 빠뜨린 것이다. 목록에 할 일이 있는데 요약할 말이 없다 —
        # 조용히 "다 준비됐습니다"라고 하느니 남은 일이 있다고 말한다. 이 구조 때문에
        # READY_VERDICT 는 must 가 빈 경우에만 나올 수 있다.
        verdict = "아직 할 일이 남았습니다."
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
            # 키가 살아 있나 (probe=True 일 때만 — 네트워크를 탄다)
            "key_status": probe_keys() if probe else [],
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
                tag = "연결됨 (서비스 계정, 무인 수집)"
            elif conn:
                tag = "연결됨 (내 구글 계정 로그인)"
                if d.get("gsc_missing_scopes"):
                    tag += ". GA4 읽기 권한이 없어 재로그인이 필요합니다"
            elif mode:
                # 새로 생긴 상태다. 예전엔 인증 파일이 있으면 곧 연결됨이었는데,
                # 번들 클라이언트가 항상 존재하면서 그 등식이 깨졌다.
                tag = "로그인 대기. 로그인 한 번이면 끝납니다 (\"GSC 로그인해줘\")"
            elif d["gsc_legacy"].get(name):
                tag = ("예전 방식 토큰만 있어 이제 안 쓰입니다. 다시 연결하세요 "
                       "(connect_gsc.py)")
            else:
                tag = "인증 없음. 클라이언트 파일부터 놓아야 합니다 (\"GSC 연동해줘\")"
            # 어느 사이트를 보고 있는지 조용히 정하지 않는다 — 화면에 이유까지 적는다.
            if name == d["brain"].get("picked"):
                mark = ("  ← 이 폴더의 사이트" if d["brain"].get("repo_match") == name
                        else "  ← 등록된 사이트가 이것 하나")
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
                  + ("로그인은 이미 끝났고 보관 중입니다." if conn else
                     "첫 조회 때 브라우저 로그인 창이 한 번 열립니다."))
            if d.get("gsc_bundled") and not conn:
                print("    * 로그인 화면에 \"확인되지 않은 앱\" 경고가 한 번 뜨는데 "
                      "정상입니다. [고급] 다음 [이동]을 누르시면 됩니다.")

    if caps_on:
        print(f"\n지금 되는 것: {' · '.join(caps_on)}")
    else:
        print(f"\n지금 되는 것: 아직 없습니다. 아래 [{BUCKET_MUST}]만 하면 켜집니다.")
    if d.get("key_status"):
        # "키가 있다"가 아니라 "그 키로 살 수 있다" — 잔액 0 은 여기서 드러난다.
        print("유료 키 상태: " + " · ".join(d["key_status"]))
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
            # 화면은 detail 을 [자세히] 로 접지만 터미널에는 접을 것이 없다 —
            # 한 칸 들여 그대로 찍는다. 접혀서 사라지는 문장을 만들지 않는다.
            det = s.get("detail") if isinstance(s, dict) else None
            if det:
                print(f"     {det}")

    # 선택 항목은 한 통에 담고 부정 문구는 머리에서 한 번만 말한다. 전에는
    # "아직 안 켠 것"과 "나중에 하면 좋은 것" 두 통이었고 같은 항목이 양쪽에 들어가,
    # 부담을 줄이려던 문구가 오히려 화면 절반을 "못 하는 것" 목록으로 만들었다.
    if d["locked"] or d["later"]:
        print(f"\n{BUCKET_LATER} (전부 선택입니다. 안 하셔도 위 기능은 그대로입니다)")
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


def _must_prose() -> list[str]:
    """diagnose() 의 must 항목이 msg 자리에 적어 둔 산문 조각 전부.

    실행만으로는 갈래를 다 못 본다 — 설치가 멀쩡한 머신에서는 core_deps 문장이
    만들어지지도 않아서, 거기 명령을 도로 적어 넣어도 검사가 아무것도 못 본다.
    검사하려는 것은 "그 자리에 무엇을 적었나"이므로 소스를 ast 로 읽는다.
    later.append 는 dict 가 아니라 문자열이라 여기 안 걸린다(그쪽은 cmd 자리가
    없어 명령을 아예 안 쓴다).
    """
    import ast
    out: list[str] = []
    for node in ast.walk(ast.parse(Path(__file__).read_text(encoding="utf-8"))):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "append" and node.args
                and isinstance(node.args[0], ast.Dict)):
            continue
        for k, v in zip(node.args[0].keys, node.args[0].values):
            if isinstance(k, ast.Constant) and k.value == "msg":
                out += [n.value for n in ast.walk(v)
                        if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    assert out, "must 항목의 msg 를 하나도 못 읽었다 — 표 모양이 바뀌었다"
    return out


def _must_entries() -> list[dict]:
    """must.append 로 만드는 항목마다 (id · 있는 자리 · go 가 None 인가 · msg 글자).

    실행만으로는 갈래를 다 못 본다 — 이 머신에서 서버 쪽 문제(core_deps 등)는
    아예 안 만들어져서, 거기에 엉뚱한 목적지를 적어 넣어도 검사가 아무것도 못
    본다(실제로 그랬다). _must_prose 와 같은 이유로 소스를 ast 로 읽는다.

    id 가 리터럴이 아닌 항목은 건너뛴다 — 지금은 전부 리터럴이다.
    """
    import ast
    out: list[dict] = []
    for node in ast.walk(ast.parse(Path(__file__).read_text(encoding="utf-8"))):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "append" and node.args
                and isinstance(node.args[0], ast.Dict)):
            continue
        d = node.args[0]
        item = {"keys": set(), "id": None, "go_is_none": None, "msg": ""}
        for k, v in zip(d.keys, d.values):
            if not (isinstance(k, ast.Constant) and isinstance(k.value, str)):
                continue
            item["keys"].add(k.value)
            if k.value == "id" and isinstance(v, ast.Constant):
                item["id"] = v.value
            if k.value == "go":
                item["go_is_none"] = isinstance(v, ast.Constant) and v.value is None
            if k.value == "msg":
                item["msg"] = " ".join(
                    n.value for n in ast.walk(v)
                    if isinstance(n, ast.Constant) and isinstance(n.value, str))
        if item["id"]:
            out.append(item)
    assert out, "must 항목을 하나도 못 읽었다 — 표 모양이 바뀌었다"
    return out


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
            # 웹 문구는 같은 3-상태를 덮어야 한다 — 한 상태만 빠지면 그 상태의
            # 웹 사용자에게 채팅·pip 안내가 그대로 나간다(KeyError 도 안 난다).
            assert set(GSC_FIX_WEB) == set(c["fix"]), sorted(GSC_FIX_WEB)
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
    # 배너 한 줄 계약 — msg 는 결론이고 detail 은 나머지다. 이 자리가 5문장짜리
    # 산문으로 돌아가면 화면의 [자세히] 가 할 일이 없어지고, 배너가 다시 "다음에
    # 누를 것"을 띠와 두고 경합한다. 소스만 봐서는 갈래가 다 안 만들어지므로
    # _must_prose 가 아니라 실제 항목을 잰다.
    for m in d["must"]:
        assert set(m) >= {"id", "msg", "detail", "go", "cmd"}, m
        assert m["msg"].count(".") <= 2, f"배너 msg 가 결론 한 줄이 아니다: {m}"
        assert isinstance(m["detail"], (str, type(None))), m
        assert m["detail"] is None or m["detail"].strip(), \
            f"detail 이 빈 문자열이다 — 없으면 None 이어야 접을 것을 안 만든다: {m}"
        if m["go"] is not None:
            assert set(m["go"]) == {"label", "href"}, m
            assert m["go"]["href"].startswith("/"), m      # 이 앱 안에서 가는 곳만
            assert len(m["go"]["label"]) <= 10, m          # 버튼 사전 길이
        # 누를 것이 하나도 없으면 왜 없는지 문장이 말해야 한다. 이게 없으면 화면은
        # "무엇을 하라"는 상자를 세워 놓고 누를 것을 안 준다 — 셸이 아무 데로나
        # 보내는 버튼을 지어내게 되는 자리다(실제로 [설정 열기]가 그렇게 붙었다).
        assert m["go"] or m["cmd"] or "잠시 뒤" in m["msg"], \
            f"누를 곳도 없고 왜 없는지도 안 말한다: {m}"

    # 위 루프는 **이 머신에서 실제로 뜬 항목**만 본다. 서버 쪽 문제처럼 여기서
    # 안 만들어지는 갈래는 소스로 본다 — 안 그러면 통과하는 검사가 아니라
    # 아무것도 안 보는 검사가 된다.
    _server_side = {"core_deps", "brain_broken", "gsc_client"}
    _seen = set()
    for e in _must_entries():
        _seen.add(e["id"])
        assert {"id", "msg", "detail", "go", "cmd"} <= e["keys"], e
        if e["id"] in _server_side:
            assert e["go_is_none"], \
                f"서버가 고칠 문제인데 사용자를 어딘가로 보낸다: {e['id']}"
            assert "잠시 뒤" in e["msg"], \
                f"누를 곳이 없는데 왜 없는지를 안 말한다: {e['id']}"
    assert _server_side <= _seen, f"서버 쪽 항목이 사라졌다: {_server_side - _seen}"
    assert all(s.startswith(LATER_TAG) for s in d["next_steps"][len(d["must"]):])
    # 로컬(표식 없음)에서는 준비물이 전부 사용자 몫이다.
    assert all(r["owner"] == "user" for r in d["readiness"]), "로컬인데 서버 몫이 있다"
    # 요약은 목록과 어긋나면 안 된다 — 할 일이 남았는데 "다 준비됐습니다"는 거짓이다.
    # 분기 순서를 바꾸다 보면 조용히 깨지는 자리라 못 박는다.
    assert not (d["must"] and d["verdict"] == READY_VERDICT), (d["verdict"], d["must"])
    # 로컬도 산문 본문에는 명령·경로를 안 넣는다 — 복사할 것은 cmd 한 자리에만 있다.
    # 이 규칙이 풀리면 같은 문장이 호스팅 배너로 새는 길이 다시 열린다. 이 머신의
    # 상태가 어떻든 모든 갈래를 보려고 must 를 강제로 한 바퀴 돌린다.
    for text in _must_prose():
        for banned in ("채팅", "/capture ", "/create ", "pip install", "`"):
            assert banned not in text, f"must 산문에 {banned!r} 이 있다: {text}"
    # 칩은 사용자가 그대로 쓰는 것이다 — 파일 경로를 실으면 경로를 손으로 다루라는
    # 뜻이 되고, 머신마다 다른 임시 경로가 화면에 그대로 뜬다. 복구를 부르는 문구를
    # 싣는다(그건 Claude 가 경로를 찾아 처리한다). pip 한 줄만 예외다: 그건 경로가
    # 아니라 명령이고, 사용자가 셸에 그대로 칠 수 있는 유일한 것이다.
    for m in d["must"]:
        cmd = m.get("cmd")
        if not cmd or cmd == CORE_PIP:
            continue
        assert "\\" not in cmd and not re.match(r"^[A-Za-z]:", cmd),             f"must 의 cmd 가 파일 경로다: {m}"
        assert str(db.CAPTURE_HOME) not in cmd, f"must 의 cmd 에 보관함 경로가 있다: {m}"

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
        # 웹 사용자에게는 채팅도 셸도 파일 시스템도 없다. 이 문장들이 그대로
        # 호스팅 배너에 찍히므로, 명령·경로가 섞이면 할 수 없는 일을 시키는
        # 안내가 된다 — 그 자리를 여기서 못 박는다 (로컬 갈래는 그대로 둔다).
        # detail 도 함께 본다 — 접힌 곳이라고 새도 되는 것이 아니다. 오히려
        # 눈에 덜 띄어 로컬 문구가 오래 남는다.
        for text in [h["verdict"],
                     *(m["msg"] for m in h["must"]),
                     *(m["detail"] for m in h["must"] if m.get("detail")),
                     *payload["extra"]]:
            for banned in ("채팅", "/capture ", "/create ", "pip install",
                           "connect_gsc", "`", "\\"):
                assert banned not in text, f"호스팅 문구에 {banned!r} 이 있다: {text}"
        # 이 폴더 개념인 항목은 호스팅에 안 나간다. 이 머신에서는 그 갈래가 아예
        # 안 만들어질 수 있으므로 거르는 함수에 가짜 목록을 직접 먹여 본다 —
        # 그래야 통과하는 검사가 아니라 실제로 무언가를 보는 검사가 된다.
        assert not (h["must"] and h["verdict"] == READY_VERDICT), (h["verdict"], h["must"])
        assert not any(m["id"] in LOCAL_ONLY_MUST for m in h["must"]), h["must"]
        # 명부를 통째로 비우면 위 줄은 아무것도 안 보게 된다 — 아는 항목을 이름으로
        # 못 박고, 거르는 함수에 고정된 가짜 목록을 먹여 실제 동작까지 본다.
        assert "pick_project" in LOCAL_ONLY_MUST, "폴더 개념 항목이 명부에서 빠졌다"
        _fake = [{"id": "pick_project", "msg": "x"}, {"id": "gsc_login", "msg": "y"}]
        assert [m["id"] for m in _drop_local_only(_fake, True)] == ["gsc_login"]
        assert _drop_local_only(_fake, False) == _fake
        # 복사 칩은 로컬 전용이다 — 호스팅에 실리면 화면이 못 쓸 칩을 그린다.
        assert all(m.get("cmd") is None for m in h["must"]), h["must"]
        assert all(m.get("cmd") is None for m in payload["must"]), payload["must"]
        # 호스팅에는 칠 곳이 없으므로 go 가 유일한 행동이다. 서버가 고칠 문제
        # (core_deps·brain_broken·gsc_client)는 사용자를 보낼 곳이 없다 — 거기에
        # 목적지를 지어내면 아무것도 못 하는 화면으로 보내는 막다른 길이 된다.
        _server_side = {"core_deps", "brain_broken", "gsc_client"}
        for m in h["must"]:
            if m["id"] in _server_side:
                assert m["go"] is None, f"서버 몫인데 사용자를 어딘가로 보낸다: {m}"
                assert "잠시 뒤" in m["msg"], m
        # setup_payload 는 id 를 안 싣는다(화면이 안 쓴다) — 대신 go 가 그대로
        # 실려 오는지 본다. 떨어뜨리면 호스팅 배너에서 유일한 행동이 사라진다.
        assert all("go" in m for m in payload["must"]), payload["must"]
        _pgo = [m["go"] for m in payload["must"]]
        _hgo = [m["go"] for m in h["must"] if m["go"]]
        assert all(g in _pgo for g in _hgo), \
            f"setup_payload 가 go 를 떨어뜨렸다: {_pgo}"
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
                whole = first["msg"] + " " + (first.get("detail") or "")
                assert "명령어로 연결하기" in whole, first
            remote._save({"url": "https://h.example", "token": "smt_x",
                          "projects": ["a", "b"]})
            e = diagnose()
            assert e["remote"] == {"linked": True, "url": "https://h.example",
                                   "projects": ["a", "b"]}, e["remote"]
            first = next((m for m in e["must"] if m["id"] == "first_project"), None)
            if first:
                whole = first["msg"] + " " + (first.get("detail") or "")
                assert "명령어로 연결하기" not in whole, first
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
    # 잔액·키 확인은 네트워크를 탄다 — 화면(--web)과 기계용(--json)은 즉답이
    # 생명이라 텍스트 진단에서만 묻는다 (둘 다 무료 호출이지만 공짜는 아니다: 시간).
    d = diagnose(probe=not {"--web", "--json"} & set(sys.argv))
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
        print("대시보드를 띄웠습니다. 위쪽 배너가 점검 결과입니다. [설정] 화면에서 "
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
