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


def from_progress(p: dict, name: str, domain: str) -> dict:
    gst = gsc_state()
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

    steps = [
        {"id": "register", "t": "사이트 등록",
         "gain": "측정할 도메인과 시작 검색어를 정합니다. 여기서 출발합니다.",
         "done": True, "state": domain or "", "cmd": None},
        {"id": "gsc", "t": "구글 실적 읽기",
         "gain": "서치콘솔에서 실제 노출·클릭·순위를 가져옵니다. 이 도구의 모든 판단이 "
                 "이 숫자 위에서 이뤄집니다 — 추측을 안 하려고 제일 먼저 합니다.",
         "done": gsc_done,
         "state": gsc_state_str,
         "cmd": gsc_cmd},
        {"id": "keywords", "t": "키워드 캐기",
         "gain": "자동완성으로 후보를 모아 추적할 목록을 만듭니다. 무료이고, 여기서 "
                 "늘린 만큼 다음 단계가 볼 게 많아집니다.",
         "done": p.get("keywords_found", 0) > 0,
         "state": (f"캔 것 {p['keywords_found']}개 · 추적 {p['keywords']}개"
                   if p.get("keywords_found", 0)
                   else (f"직접 적은 {p['keywords']}개뿐" if p.get("keywords", 0) else "아직 없음")),
         "cmd": f"/capture keywords {name}"},
        {"id": "ai", "t": "AI 노출 확인",
         "gain": "ChatGPT·Perplexity·Gemini가 이 주제에서 누구를 인용하는지 봅니다. "
                 "OpenRouter 키가 필요합니다.",
         "done": p.get("ai_checks", 0) > 0,
         "state": (f"답변 {p['ai_checks']}개 확인 · 질문 {p['ai_prompts']}개"
                   if p.get("ai_checks", 0)
                   else ("질문은 준비됨 · 아직 안 물어봄" if p.get("ai_prompts", 0)
                         else "물어볼 질문부터 필요")),
         "cmd": (f"/capture ai {name}" if p.get("ai_prompts", 0) else f"/capture add {name}")},
        {"id": "gaps", "t": "손댈 것 뽑기",
         "gain": "모은 숫자에서 기회를 계산합니다 — 조금만 밀면 1페이지인 검색어, 우리 "
                 "대신 인용되는 곳, 같은 틀로 여러 장 찍을 수 있는 페이지.",
         "done": p.get("opps", 0) > 0,
         "state": f"{p['opps']}건 뽑음" if p.get("opps", 0) else "아직 없음",
         "cmd": f"/capture gaps {name}"},
        {"id": "create", "t": "실제로 고치기",
         "gain": "뽑은 기회를 리포의 진짜 콘텐츠 변경으로 만듭니다. 브랜치와 PR로 "
                 "나가고, 끝나면 그 기회가 완료로 닫힙니다.",
         "done": p.get("creations", 0) > 0,
         "state": f"{p['creations']}건 고침" if p.get("creations", 0) else "아직 한 건도 안 함",
         "cmd": f"/create plan {name}"},
    ]
    here = next((i for i, s in enumerate(steps) if not s["done"]), -1)
    return {"steps": steps, "here": here}


def state(conn, project, domain: str = "") -> dict:
    """갱도 6단계 판정 — "지금 할 것"을 정하는 유일한 정본.

    conn: sqlite3.Connection
    project: projects 행 (dict 또는 sqlite3.Row) — id·name·domain 을 갖는다
    domain: 생략하면 project 의 domain 을 쓴다
    """
    dom = domain or (project["domain"] or "")
    return from_progress(progress(conn, project["id"]), project["name"], dom)


def setup_payload(d: dict = None, conn=None) -> dict:
    """대시보드 /api/doctor 응답 생성 — 정책 판단은 여기서만 한다."""
    if d is None:
        import doctor
        d = doctor.diagnose()
    projects = d.get("brain", {}).get("projects", [])
    no_project = not bool(projects)

    must = [
        m["msg"] if isinstance(m, dict) else m
        for m in d.get("must", [])
        if not (no_project and isinstance(m, dict) and m.get("id") == "first_project")
    ]
    extra = [
        f"{c['name']} — {c['desc']}. 켜려면: {c.get('fix') or '필수 설치가 끝나면 켜집니다.'}"
        for c in d.get("locked", [])
    ] + list(d.get("later", []))
    keys = d.get("keys", {})
    deps_gsc = d.get("deps_gsc", {})
    gst = gsc_state()
    gsc_mode = d.get("gsc_mode", "")
    show_deps_gsc_btn = bool(gsc_mode) and not bool(deps_gsc.get("googleapiclient"))
    show_skills_btn = bool(d.get("marketing_skills_msg"))
    show_setup = bool(d.get("must_other")) or no_project

    guide = None
    close_conn = False
    if projects:
        if conn is None:
            conn = db.connect()
            close_conn = True
        try:
            p = db.get_project(conn, projects[0])
            guide = state(conn, p)
        finally:
            if close_conn:
                conn.close()

    return {
        "verdict": d.get("verdict", ""),
        "no_project": no_project,
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


def _selfcheck() -> None:
    pr = {"gsc_days": 0, "gsc_last": "", "keywords": 2, "keywords_found": 0,
          "ai_checks": 0, "ai_prompts": 0, "opps": 0, "creations": 0}

    # 미연결 상태에서의 판정 검증
    st = from_progress(pr, "demo", "demo.com")
    assert st["here"] == 1 and st["steps"][1]["id"] == "gsc", st["here"]
    assert st["steps"][3]["cmd"] == "/capture add demo"       # 질문이 없으면 add 부터

    # GSC 연결된 상태에서의 진행 검증
    _orig_conn = db.gsc_connected
    _orig_auth = db.gsc_auth
    try:
        db.gsc_connected = lambda: True
        st = from_progress({**pr, "gsc_days": 3, "gsc_last": "2026-08-14", "keywords_found": 5,
                    "ai_prompts": 10}, "demo", "demo.com")
        assert st["here"] == 3
        assert st["steps"][3]["cmd"] == "/capture ai demo"        # 질문이 있으면 물어본다
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

    print("stage self-check ok")


if __name__ == "__main__":
    _selfcheck()
