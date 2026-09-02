#!/usr/bin/env python3
"""갱도 6단계 판정 및 진행 상태 — "지금 할 것"의 단일 정본.

이전에는 이 판단이 scoring.py, dashboard.py, doctor.py, dashboard.html 네 파일에
흩어져 있었고 제자리 덮어쓰기와 브라우저 전역변수로 화해시켰다.
이제 판정은 이 모듈 한 곳에서만 나고, 화면과 doctor 는 그 결과를 배달만 한다.

self-check: python stage.py
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
SETUP_SCRIPTS = Path(__file__).resolve().parents[2] / "setup" / "scripts"
sys.path.insert(0, str(SETUP_SCRIPTS))
import db


def progress(conn, pid: int) -> dict:
    """안내 화면이 "지금 어디까지 팠는지"를 말하려면 단계별 실적이 필요하다.
    각 값은 그 단계를 했다는 증거 — 0이면 아직 안 한 것."""
    def one(sql: str, args=()) -> int:
        return conn.execute(sql, args).fetchone()[0] or 0

    return {
        "gsc_days": one("SELECT COUNT(DISTINCT snapshot_date) FROM gsc_snapshots "
                        "WHERE project_id=?", (pid,)),
        "gsc_last": (conn.execute("SELECT MAX(snapshot_date) FROM gsc_snapshots "
                                  "WHERE project_id=?", (pid,)).fetchone()[0] or ""),
        "keywords": one("SELECT COUNT(*) FROM keywords WHERE project_id=? AND is_active=1",
                        (pid,)),
        # 씨앗은 등록할 때 손으로 넣은 것이다 — 발굴을 돌렸는지는 씨앗이 아닌 것으로 센다.
        "keywords_found": one("SELECT COUNT(*) FROM keywords WHERE project_id=? "
                              "AND is_active=1 AND source!='seed'", (pid,)),
        "ai_checks": one("""SELECT COUNT(*) FROM ai_checks c JOIN ai_prompts p
                              ON p.id=c.prompt_id WHERE p.project_id=?""", (pid,)),
        "ai_prompts": one("SELECT COUNT(*) FROM ai_prompts WHERE project_id=? "
                          "AND is_active=1", (pid,)),
        "opps": one("SELECT COUNT(*) FROM opportunities WHERE project_id=?", (pid,)),
        "creations": one("SELECT COUNT(*) FROM creations WHERE project_id=?", (pid,)),
    }


def gsc_state() -> str:
    """구글 연결 3-상태: "connected" | "pending" | "none".

    정본은 db.gsc_connected()(토큰/키 실존 여부)다.
    인증 수단(OAuth 클라이언트 / 서비스 계정 파일)이 있지만 토큰이 없으면 pending,
    인증 수단조차 없으면 none.
    """
    if db.gsc_connected():
        return "connected"
    if db.gsc_auth():
        return "pending"
    return "none"


def pick_project(projects, cwd=None) -> str | None:
    """지금 이 세션이 말하는 사이트 하나. 못 고르면 None — 아무거나 집지 않는다.

    Brain 이 전역이라 `projects[0]`(=먼저 등록한 것)을 집던 코드는, 사이트와 아무
    상관 없는 다른 리포에서 `/setup` 을 돌려도 늘 같은 사이트를 띄웠다. 조용히
    틀린 답을 주느니 못 고르겠다고 말하는 게 낫다 — 부르는 쪽이 사용자에게 묻는다.

    순서: 이 폴더의 리포 매치 > 사이트가 하나뿐이면 그것 > 못 고름(None).
    """
    if not projects:
        return None
    match = db.repo_project(cwd)
    if match and match in projects:
        return match
    if len(projects) == 1:
        return projects[0]
    return None


def skippable(step_id: str) -> bool:
    """이 단계를 지금 환경에서 못 하는데, 아하 모먼트(gaps)를 막지도 않는가.

    판정 근거는 doctor 의 준비 상태 명부다 — blocking=False 이고 이 기능을 여는 키가
    하나도 없으면 여기서 멈출 이유가 없다. 예전에는 키 없는 사용자의 "지금 할 것"이
    AI 단계에 영영 붙어서, 기회 목록(이 제품의 아하 모먼트)까지 못 갔다.
    doctor 는 모듈 로드 때 stage 를 import 하므로 여기서는 늦게 import 한다.
    """
    import doctor
    c = next((c for c in doctor.CAPABILITIES if c["id"] == step_id), None)
    return bool(c) and not c["blocking"] and bool(c["keys"]) and \
        not any(os.environ.get(k) for k in c["keys"])


# 단계 이름표 — 로컬 안내([안내] 화면)와 호스팅 애드온(배너·화면 머리의 '다시
# 돌리기' 버튼)이 같이 쓰는 유일한 정본이다. 예전에는 여기와 server/assets/dash.html
# 에 각자 있었고 문구가 이미 갈라져 있었다(gsc.gain: "측정할 도메인과…" vs
# "분석할 도메인과…", gsc.t: "구글 실적 읽기" vs "검색 실적 수집").
#   t     이 단계의 이름
#   gain  안내(6단계) 설명 한 줄 — 안내에 나오는 단계만 갖는다
#   run   화면 머리의 '다시 돌리기' 버튼 라벨 — 제목과 겹치지 않게 다르게 부른다.
#         view-def 의 stages 에 실제로 등장하는 단계만 갖는다.
# 조립(dashboard.py._assemble)이 이 표를 window.__STAGES__ 로 실어 보낸다 —
# dash.html 은 자기 사본을 갖지 않는다.
STAGE_LABELS: dict[str, dict] = {
    "register": {"t": "사이트 등록",
        "gain": "측정할 도메인과 시작 검색어를 정합니다. 여기서 출발합니다."},
    "gsc": {"t": "구글 실적 읽기", "run": "실적 다시 수집",
        "gain": "서치콘솔에서 실제 노출·클릭·순위를 가져옵니다. 모든 판단이 이 숫자 "
                "위에서 이뤄집니다. 추측을 안 하려고 제일 먼저 합니다."},
    "ga4": {"t": "GA4 실적 읽기", "run": "GA4 다시 수집"},
    "index": {"t": "색인 상태 확인", "run": "색인 다시 확인"},
    "keywords": {"t": "키워드 찾기", "run": "키워드 다시 찾기",
        "gain": "검색창 자동완성으로 후보를 모아 추적 목록을 만듭니다. 무료입니다. "
                "여기서 늘린 만큼 다음 단계가 볼 게 많아집니다."},
    "metrics": {"t": "키워드 지표", "run": "지표 다시 조회",
        "gain": "찾은 키워드의 월간 검색량·경쟁 난이도·CPC 를 조회합니다. 검색량이 "
                "있어야 아직 순위가 없는 검색어의 가치를 판단할 수 있습니다. "
                "DataForSEO 유료 호출입니다."},
    "rank": {"t": "순위 추적", "run": "순위 다시 확인"},
    "crawl": {"t": "사이트 크롤", "run": "사이트 다시 크롤",
        "gain": "사이트를 따라 돌며 깨진 내부 링크·리다이렉트 사슬·고아 페이지·중복 "
                "제목을 찾습니다. 한 페이지만 봐서는 안 나오는 것들입니다. 무료입니다."},
    "ai": {"t": "AI 노출 확인", "run": "AI 인용 다시 확인",
        "gain": "ChatGPT·Perplexity·Gemini가 이 주제에서 누구를 인용하는지 봅니다. "
                "OpenRouter 키가 필요합니다."},
    "competitors": {"t": "경쟁사 역키워드", "run": "경쟁사 다시 수집",
        "gain": "경쟁 도메인이 이미 순위를 잡은 검색어를 모아 추적 후보로 올립니다. "
                "DataForSEO Labs 유료 호출입니다."},
    "backlinks": {"t": "백링크 프로필", "run": "백링크 다시 수집",
        "gain": "참조 도메인·앵커 텍스트·개별 링크를 수집합니다. 경쟁사는 링크를 받는데 "
                "우리는 못 받는 도메인도 함께 찾습니다. DataForSEO 유료 호출입니다."},
    "gaps": {"t": "손댈 것 뽑기", "run": "기회 다시 분석",
        "gain": "모은 숫자에서 기회를 계산합니다. 조금만 밀면 1페이지인 검색어, 우리 "
                "대신 인용되는 곳, 같은 틀로 여러 장 찍을 페이지를 찾습니다."},
    "pages": {"t": "내 페이지 점검", "run": "페이지 다시 점검",
        "gain": "기회에 걸린 내 페이지를 직접 열어 제목·설명·H1·본문 길이·구조화 데이터를 "
                "확인합니다. 무료입니다."},
    "report": {"t": "보고서 생성"},
    "create": {"t": "실제로 고치기",
        "gain": "뽑은 기회를 저장소의 진짜 콘텐츠 변경으로 만듭니다. 브랜치와 PR 로 "
                "나가고, 끝나면 그 기회가 완료로 닫힙니다."},
}


# 유료 키 언급은 **로컬 전용**이다 — 호스팅은 서버가 키를 댄다(doctor 명부의
# owner="server"). 같은 문장이 웹으로 나가면 넣을 곳도 없는 키를 넣으라고 시키는
# 셈이고, 실제로 [AI 인용] 화면은 같은 자리에서 "키는 서버가 댑니다"라고 말한다.
# 표를 두 벌 만들지 않는다: **갈리는 문장만** 여기 적고 stage_labels() 가 덮는다.
GAIN_WEB = {
    "ai": "ChatGPT·Perplexity·Gemini가 이 주제에서 누구를 인용하는지 봅니다. "
          "키는 서버가 댑니다.",
    "metrics": "찾은 키워드의 월간 검색량·경쟁 난이도·CPC 를 조회합니다. 검색량이 "
               "있어야 아직 순위가 없는 검색어의 가치를 판단할 수 있습니다. "
               "조회 비용은 서버가 냅니다.",
    "competitors": "경쟁 도메인이 이미 순위를 잡은 검색어를 모아 추적 후보로 "
                   "올립니다. 조회 비용은 서버가 냅니다.",
    "backlinks": "참조 도메인·앵커 텍스트·개별 링크를 수집합니다. 경쟁사는 링크를 "
                 "받는데 우리는 못 받는 도메인도 함께 찾습니다. 조회 비용은 서버가 "
                 "냅니다.",
}


def _hosted() -> bool:
    """이 런이 호스팅인가. 표식의 주인은 server/settings.py 고 이름은 doctor 가 갖는다
    — 여기서 세 번째 사본을 만들지 않는다(doctor 는 stage 를 import 하므로 늦게 건다).
    """
    import doctor
    return bool(os.environ.get(doctor.HOSTED_ENV))


def stage_labels(variant: str = "local") -> dict:
    """화면에 실어 보낼 단계 용어표. 호스팅은 유료 키 문장만 갈아 끼운다.

    조립(dashboard._assemble)이 변종을 알고 있으므로 그쪽이 골라 부른다. 나머지
    항목은 STAGE_LABELS 그대로다 — 갈리는 문장 말고는 사본이 없다.
    """
    if variant != "hosted":
        return STAGE_LABELS
    return {k: ({**v, "gain": GAIN_WEB[k]} if k in GAIN_WEB else v)
            for k, v in STAGE_LABELS.items()}


def _runnable(step_id: str, cmd: str | None) -> bool:
    """이 칩의 명령이 정말 이 단계를 돌리는가. id 는 "이 칩이 속한 단계"일 뿐이라
    그걸로는 못 판단한다 — 재료가 없으면 다른 명령을 내려보낸다(질문 0건이면 ai
    단계에 `/capture add`, GSC 미연결이면 `GSC 로그인해줘`). 그 칩을 실행 버튼으로
    바꾸는 층(호스팅판)이 이 값을 본다 — 화면은 더 이상 명령 문자열을 되짚지 않는다.
    """
    return bool(cmd) and cmd.split()[:2] == ["/capture", step_id]


def from_progress(p: dict, name: str, domain: str) -> dict:
    # 사이트가 아직 없어도 안내는 그린다 — 첫 사용자가 정확히 이 상태이고, 여태
    # 그 사람에게만 6단계가 통째로 비어 있었다("아래 [지금 할 것] 하나만 하시면
    # 됩니다" 밑에 아무것도 없는 화면). 이름 자리는 고쳐 쓸 자리표로 둔다.
    name = name or "사이트이름"
    gst = gsc_state()
    ai_skip = skippable("ai")
    if gst == "connected":
        gsc_done = p.get("gsc_days", 0) > 0
        gsc_state_str = (f"{p['gsc_days']}번 읽음 · 최근 {p['gsc_last']}"
                         if p.get("gsc_days", 0) else "아직 안 읽음")
        gsc_cmd = f"/capture gsc {name}"
    elif gst == "pending":
        gsc_done = False
        gsc_state_str = "구글 로그인 대기"
        gsc_cmd = "GSC 로그인해줘"
    else:  # "none"
        gsc_done = False
        gsc_state_str = "구글 인증 없음"
        gsc_cmd = "GSC 연동해줘"

    # 안내 단계의 설명도 같은 규칙을 탄다 — 호스팅에서는 dash.html 의 restage() 가
    # 실어 보낸 표로 다시 덮지만, 페이로드 자체가 맞아야 박제본·API 응답도 맞는다.
    L = stage_labels("hosted" if _hosted() else "local")
    cmd_register = None if domain else "/capture add"
    cmd_keywords = f"/capture keywords {name}"
    cmd_ai = f"/capture ai {name}" if p.get("ai_prompts", 0) else f"/capture add {name}"
    cmd_gaps = f"/capture gaps {name}"
    cmd_create = f"/create plan {name}"
    steps = [
        {"id": "register", "t": L["register"]["t"], "gain": L["register"]["gain"],
         "done": bool(domain), "state": domain or "아직 없음",
         "cmd": cmd_register, "runnable": _runnable("register", cmd_register)},
        {"id": "gsc", "t": L["gsc"]["t"], "gain": L["gsc"]["gain"],
         "done": gsc_done,
         "state": gsc_state_str,
         "cmd": gsc_cmd, "runnable": _runnable("gsc", gsc_cmd)},
        {"id": "keywords", "t": L["keywords"]["t"], "gain": L["keywords"]["gain"],
         "done": p.get("keywords_found", 0) > 0,
         "state": (f"찾은 것 {p['keywords_found']}개 · 추적 {p['keywords']}개"
                   if p.get("keywords_found", 0)
                   else (f"직접 적은 {p['keywords']}개뿐" if p.get("keywords", 0) else "아직 없음")),
         "cmd": cmd_keywords, "runnable": _runnable("keywords", cmd_keywords)},
        {"id": "ai", "t": L["ai"]["t"], "gain": L["ai"]["gain"],
         "done": p.get("ai_checks", 0) > 0,
         # 목록에서 빼지 않는다 — 순서는 그대로 두고 "지금 할 것"만 넘어간다.
         "skip": ai_skip,
         "state": ("건너뜀 · OpenRouter 를 연동하면 켜집니다" if ai_skip else
                   (f"답변 {p['ai_checks']}개 확인 · 질문 {p['ai_prompts']}개"
                    if p.get("ai_checks", 0)
                    else ("질문은 준비됨 · 아직 안 물어봄" if p.get("ai_prompts", 0)
                          else "물어볼 질문부터 필요"))),
         "cmd": cmd_ai, "runnable": _runnable("ai", cmd_ai)},
        {"id": "gaps", "t": L["gaps"]["t"], "gain": L["gaps"]["gain"],
         "done": p.get("opps", 0) > 0,
         "state": f"{p['opps']}건 뽑음" if p.get("opps", 0) else "아직 없음",
         "cmd": cmd_gaps, "runnable": _runnable("gaps", cmd_gaps)},
        {"id": "create", "t": L["create"]["t"], "gain": L["create"]["gain"],
         "done": p.get("creations", 0) > 0,
         "state": f"{p['creations']}건 고침" if p.get("creations", 0) else "아직 한 건도 안 함",
         "cmd": cmd_create, "runnable": _runnable("create", cmd_create)},
    ]
    # 못 하는 단계 앞에서 안내를 멈추지 않는다 — 건너뛸 수 있는 것은 건너뛴다.
    here = next((i for i, s in enumerate(steps)
                 if not s["done"] and not s.get("skip")), -1)
    return {"steps": steps, "here": here}


def state(conn, project, domain: str = "") -> dict:
    """갱도 6단계 판정 — "지금 할 것"을 정하는 유일한 정본.

    conn: sqlite3.Connection
    project: projects 행 (dict 또는 sqlite3.Row) — id·name·domain 을 갖는다
    domain: 생략하면 project 의 domain 을 쓴다
    """
    dom = domain or (project["domain"] or "")
    return from_progress(progress(conn, project["id"]), project["name"], dom)


def setup_payload(d: dict = None, conn=None, project: str = "") -> dict:
    """대시보드 /api/doctor 응답 생성 — 정책 판단은 여기서만 한다.

    project: 화면이 지금 보고 있는 사이트. 이게 있으면 그게 답이다 — 화면은
    사용자가 직접 고른 것이라 폴더 추론보다 확실하다. 없을 때만 pick_project 로
    폴더에서 추론하고, 그것도 못 하면 안내(guide)를 비운다.
    """
    if d is None:
        import doctor
        d = doctor.diagnose()
    projects = d.get("brain", {}).get("projects", [])
    no_project = not bool(projects)

    # 산문(msg)과 복사할 것(cmd)을 갈라서 그대로 넘긴다 — 예전에는 여기서 문자열
    # 하나로 눌러 붙였고, 그래서 명령·파일 경로가 산문 안에 섞여 호스팅 배너까지
    # 따라갔다. 화면은 msg 를 그리고 cmd 가 있으면 칩으로 단다(cmd 는 로컬 전용).
    must = [
        {"msg": m["msg"], "cmd": m.get("cmd")} if isinstance(m, dict) else {"msg": m}
        for m in d.get("must", [])
        if not (no_project and isinstance(m, dict) and m.get("id") == "first_project")
    ]
    # owner 가 server 인 것은 뺀다 — 호스팅에서는 서버가 이미 낸 준비물이라,
    # 여기 남겨 두면 낼 필요가 없는 사람에게 키 발급 목록을 보여 주게 된다.
    # (로컬은 doctor 가 전부 user 로 렌더하므로 지금까지와 똑같다.)
    # 이름은 라벨이라 쌍점으로 붙이고, 그 뒤는 문장이라 마침표로 끊는다 — 예전에는
    # 줄표 하나가 라벨 구분과 문장 접속을 겸해서 어디까지가 이름인지 흐렸다.
    # "켜려면:" 은 뗐다: 잠금 사유가 이미 "…를 넣으면 켜집니다"로 끝난다.
    extra = [
        f"{c['name']}: {c['desc']}. {c.get('fix') or '필수 설치가 끝나면 켜집니다.'}"
        for c in d.get("locked", []) if c.get("owner", "user") != "server"
    ] + list(d.get("later", []))
    keys = d.get("keys", {})
    deps_gsc = d.get("deps_gsc", {})
    gst = gsc_state()
    gsc_mode = d.get("gsc_mode", "")
    show_deps_gsc_btn = bool(gsc_mode) and not bool(deps_gsc.get("googleapiclient"))
    show_skills_btn = bool(d.get("marketing_skills_msg"))
    show_setup = bool(d.get("must_other")) or no_project

    # 사이트가 하나도 없으면 빈 진행으로 그린다 — 안내가 통째로 사라지는 대신
    # 1번(사이트 등록)이 "지금 할 것"으로 선다. 첫 사용자가 정확히 이 상태다.
    # **여럿이라 못 고르는 것과는 다르다**: 그때는 사이트가 이미 있으므로 여기서
    # 빈 안내를 그리면 "사이트를 등록하세요"라고 틀린 말을 하게 된다 — 안 그린다.
    guide = from_progress({}, "", "") if no_project else None
    close_conn = False
    picked = project if project in projects else pick_project(projects)
    if picked:
        if conn is None:
            conn = db.connect()
            close_conn = True
        try:
            p = db.get_project(conn, picked)
            guide = state(conn, p)
        finally:
            if close_conn:
                conn.close()

    return {
        "verdict": d.get("verdict", ""),
        "no_project": no_project,
        "project": picked,          # 안내가 말하는 사이트 (못 고르면 None)
        "projects": projects,       # 여러 개일 때 화면이 고르게 하려고
        "must": must,
        "extra": extra,
        "core_ok": bool(d.get("core_ok")),
        "gsc_ok": gst == "connected",
        "gsc_state": gst,                           # "connected" | "pending" | "none"
        "gsc_mode": gsc_mode,                       # "oauth" | "service_account" | ""
        "gsc_bundled": bool(d.get("gsc_bundled")),  # 번들 클라이언트 → 동의 화면 경고 예고
        "nkeys": sum(1 for k in ("openrouter", "serper", "dataforseo") if keys.get(k)),
        "show_deps_gsc_btn": show_deps_gsc_btn,
        "show_skills_btn": show_skills_btn,         # 빠진 마케팅 스킬이 있을 때만 True
        "show_setup": show_setup,
        "guide": guide,
    }


_DEMO = {"gsc_days": 0, "gsc_last": "", "keywords": 2, "keywords_found": 0,
         "ai_checks": 0, "ai_prompts": 0, "opps": 0, "creations": 0}


def _selfcheck() -> None:
    pr = _DEMO

    # 미연결 상태에서의 판정 검증
    st = from_progress(pr, "demo", "demo.com")
    assert st["here"] == 1 and st["steps"][1]["id"] == "gsc", st["here"]
    assert st["steps"][3]["cmd"] == "/capture add demo"       # 질문이 없으면 add 부터
    assert not st["steps"][3]["runnable"], "add 는 ai 단계를 안 돈다 — runnable 이면 안 된다"
    assert st["steps"][2]["runnable"], "키워드 단계는 /capture keywords 그대로라 늘 runnable"
    assert not st["steps"][0]["runnable"], "register 의 명령은 /capture add 라 runnable 이면 안 된다"

    # GSC 연결된 상태에서의 진행 검증
    _orig_conn = db.gsc_connected
    _orig_auth = db.gsc_auth
    _orig_key = os.environ.get("OPENROUTER_API_KEY")
    try:
        db.gsc_connected = lambda: True
        os.environ["OPENROUTER_API_KEY"] = "k"     # 키가 있으면 AI 가 다음 걸음이다
        st = from_progress({**pr, "gsc_days": 3, "gsc_last": "2026-08-14", "keywords_found": 5,
                    "ai_prompts": 10}, "demo", "demo.com")
        assert st["here"] == 3
        assert st["steps"][3]["cmd"] == "/capture ai demo"        # 질문이 있으면 물어본다
        assert not st["steps"][3]["skip"]
        assert st["steps"][3]["runnable"], "질문이 있으면 ai 단계 명령이 그대로라 runnable"

        # 키가 없으면 AI 단계는 이 환경에서 못 한다 — 목록엔 남기되 "지금 할 것"은
        # 아하 모먼트(gaps)로 넘어가야 한다. 예전엔 여기 영영 붙어 있었다.
        os.environ.pop("OPENROUTER_API_KEY", None)
        st = from_progress({**pr, "gsc_days": 3, "gsc_last": "2026-08-14",
                            "keywords_found": 5, "ai_prompts": 10}, "demo", "demo.com")
        assert st["steps"][3]["skip"] and "건너뜀" in st["steps"][3]["state"]
        assert st["here"] == 4 and st["steps"][4]["id"] == "gaps", st["here"]
        assert len(st["steps"]) == 6, "단계를 목록에서 빼 버렸다"
        os.environ["OPENROUTER_API_KEY"] = "k"
        st = from_progress({**pr, "gsc_days": 1, "keywords_found": 1, "ai_checks": 1,
                    "ai_prompts": 1, "opps": 1, "creations": 1}, "demo", "demo.com")
        assert st["here"] == -1                                    # 한 바퀴 다 돎

        # gsc_state 3분기 검증
        assert gsc_state() == "connected"

        db.gsc_connected = lambda: False
        db.gsc_auth = lambda: "oauth"
        assert gsc_state() == "pending"

        db.gsc_auth = lambda: ""
        assert gsc_state() == "none"
    finally:
        db.gsc_connected = _orig_conn
        db.gsc_auth = _orig_auth
        os.environ.pop("OPENROUTER_API_KEY", None) if _orig_key is None \
            else os.environ.__setitem__("OPENROUTER_API_KEY", _orig_key)

    print("stage self-check ok")


if __name__ == "__main__":
    _selfcheck()
