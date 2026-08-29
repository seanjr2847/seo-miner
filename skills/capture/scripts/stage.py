#!/usr/bin/env python3
"""갱도 6단계 판정 및 진행 상태 — "지금 할 것"의 단일 정본.

이전에는 이 판단이 scoring.py, dashboard.py, doctor.py, dashboard.html 네 파일에
흩어져 있었고 제자리 덮어쓰기와 브라우저 전역변수로 화해시켰다.
이제 판정은 이 모듈 한 곳에서만 나고, 화면과 doctor 는 그 결과를 배달만 한다.

self-check: python stage.py
"""
import json
import os
import re
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
        "gain": "서치콘솔에서 실제 노출·클릭·순위를 가져옵니다. 이 도구의 모든 판단이 "
                "이 숫자 위에서 이뤄집니다 — 추측을 안 하려고 제일 먼저 합니다."},
    "index": {"t": "색인 생성 검사", "run": "색인 다시 검사"},
    "keywords": {"t": "키워드 캐기", "run": "키워드 다시 발굴",
        "gain": "자동완성으로 후보를 모아 추적할 목록을 만듭니다. 무료이고, 여기서 "
                "늘린 만큼 다음 단계가 볼 게 많아집니다."},
    "metrics": {"t": "키워드 지표", "run": "지표 다시 조회",
        "gain": "발굴한 키워드의 월간 검색량·경쟁 난이도·CPC 를 조회합니다. 검색량이 "
                "있어야 아직 순위가 없는 검색어의 가치를 판단할 수 있습니다. "
                "DataForSEO 유료 호출입니다."},
    "rank": {"t": "순위 추적", "run": "순위 다시 확인"},
    "crawl": {"t": "사이트 크롤", "run": "사이트 다시 크롤",
        "gain": "사이트를 따라 돌며 깨진 내부 링크·리다이렉트 사슬·고아 페이지·중복 "
                "제목을 찾습니다. 한 페이지만 봐서는 나오지 않는 항목입니다. 비용이 없습니다."},
    "ai": {"t": "AI 노출 확인", "run": "AI 인용 다시 확인",
        "gain": "ChatGPT·Perplexity·Gemini가 이 주제에서 누구를 인용하는지 봅니다. "
                "OpenRouter 키가 필요합니다."},
    "competitors": {"t": "경쟁사 역키워드", "run": "경쟁사 다시 수집",
        "gain": "경쟁 도메인이 이미 순위를 잡은 검색어를 수집해 추적 후보로 올립니다. "
                "DataForSEO Labs 유료 호출입니다."},
    "backlinks": {"t": "백링크 프로필", "run": "백링크 다시 수집",
        "gain": "참조 도메인·앵커 텍스트·개별 링크를 수집하고, 경쟁사는 링크를 받는데 "
                "우리는 못 받는 도메인을 찾습니다. DataForSEO 유료 호출입니다."},
    "gaps": {"t": "손댈 것 뽑기", "run": "기회 다시 분석",
        "gain": "모은 숫자에서 기회를 계산합니다 — 조금만 밀면 1페이지인 검색어, 우리 "
                "대신 인용되는 곳, 같은 틀로 여러 장 찍을 수 있는 페이지."},
    "pages": {"t": "내 페이지 점검", "run": "페이지 다시 점검",
        "gain": "기회에 걸린 내 페이지를 직접 열어 제목·설명·H1·본문 길이·구조화 데이터를 "
                "확인합니다. 비용이 없습니다."},
    "report": {"t": "보고서 생성"},
    "create": {"t": "실제로 고치기",
        "gain": "뽑은 기회를 리포의 진짜 콘텐츠 변경으로 만듭니다. 브랜치와 PR로 "
                "나가고, 끝나면 그 기회가 완료로 닫힙니다."},
}


def from_progress(p: dict, name: str, domain: str) -> dict:
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

    L = STAGE_LABELS
    steps = [
        {"id": "register", "t": L["register"]["t"], "gain": L["register"]["gain"],
         "done": True, "state": domain or "", "cmd": None},
        {"id": "gsc", "t": L["gsc"]["t"], "gain": L["gsc"]["gain"],
         "done": gsc_done,
         "state": gsc_state_str,
         "cmd": gsc_cmd},
        {"id": "keywords", "t": L["keywords"]["t"], "gain": L["keywords"]["gain"],
         "done": p.get("keywords_found", 0) > 0,
         "state": (f"캔 것 {p['keywords_found']}개 · 추적 {p['keywords']}개"
                   if p.get("keywords_found", 0)
                   else (f"직접 적은 {p['keywords']}개뿐" if p.get("keywords", 0) else "아직 없음")),
         "cmd": f"/capture keywords {name}"},
        {"id": "ai", "t": L["ai"]["t"], "gain": L["ai"]["gain"],
         "done": p.get("ai_checks", 0) > 0,
         # 목록에서 빼지 않는다 — 순서는 그대로 두고 "지금 할 것"만 넘어간다.
         "skip": ai_skip,
         "state": ("건너뜀 — OpenRouter 연동하면 켜집니다" if ai_skip else
                   (f"답변 {p['ai_checks']}개 확인 · 질문 {p['ai_prompts']}개"
                    if p.get("ai_checks", 0)
                    else ("질문은 준비됨 · 아직 안 물어봄" if p.get("ai_prompts", 0)
                          else "물어볼 질문부터 필요"))),
         "cmd": (f"/capture ai {name}" if p.get("ai_prompts", 0) else f"/capture add {name}")},
        {"id": "gaps", "t": L["gaps"]["t"], "gain": L["gaps"]["gain"],
         "done": p.get("opps", 0) > 0,
         "state": f"{p['opps']}건 뽑음" if p.get("opps", 0) else "아직 없음",
         "cmd": f"/capture gaps {name}"},
        {"id": "create", "t": L["create"]["t"], "gain": L["create"]["gain"],
         "done": p.get("creations", 0) > 0,
         "state": f"{p['creations']}건 고침" if p.get("creations", 0) else "아직 한 건도 안 함",
         "cmd": f"/create plan {name}"},
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

    must = [
        m["msg"] if isinstance(m, dict) else m
        for m in d.get("must", [])
        if not (no_project and isinstance(m, dict) and m.get("id") == "first_project")
    ]
    # owner 가 server 인 것은 뺀다 — 호스팅에서는 서버가 이미 낸 준비물이라,
    # 여기 남겨 두면 낼 필요가 없는 사람에게 키 발급 목록을 보여 주게 된다.
    # (로컬은 doctor 가 전부 user 로 렌더하므로 지금까지와 똑같다.)
    extra = [
        f"{c['name']} — {c['desc']}. 켜려면: {c.get('fix') or '필수 설치가 끝나면 켜집니다.'}"
        for c in d.get("locked", []) if c.get("owner", "user") != "server"
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


def _check_seams() -> None:
    """화면 쪽 이음매 점검 — 브라우저를 안 띄우고 확인할 수 있는 만큼만.

    호스팅판(server/assets/dash.html)은 원본 화면 뒤에 얹히는 애드온이라, 원본이
    말없이 바뀌면 조용히 멈춘다. 예전에는 그 이음매가 렌더된 한국어였다 —
    버튼 라벨의 정규식으로 단계 id 를, onclick 문자열의 정규식으로 기회 id 를
    되찾았다. 지금은 data- 속성과 id 키 조회다. 여기서 그 계약을 지킨다.

    리포 밖(플러그인 설치본)에는 server/ 가 없다 — 그때는 조용히 건너뛴다.
    """
    root = Path(__file__).resolve().parents[3]
    dash_f = root / "server" / "assets" / "dash.html"
    views = root / "skills" / "capture" / "templates" / "views"
    shell_f = root / "skills" / "capture" / "templates" / "dashboard.html"
    if not (dash_f.exists() and views.is_dir() and shell_f.exists()):
        return
    dash = dash_f.read_text("utf-8")
    shell = shell_f.read_text("utf-8")
    tpl = shell + "".join(p.read_text("utf-8") for p in sorted(views.glob("*.html")))

    # 1) id 는 페이로드에서 온다 — 렌더된 글자에서 되짚지 않는다.
    assert 'data-opp="${o.id}"' in (views / "overview.html").read_text("utf-8"), \
        "oppRow() 가 기회 id 를 data-opp 로 안 내보낸다"
    for what, pat in (("단계 칸", r'class="stp \$\{cls\}"[^>]*data-stage='),
                      ("실행 칩", r'<button class="cmd"[^>]*data-stage='),
                      ("배너 이름", r"<b data-stage=")):
        assert re.search(pat, shell), f"renderGuide() 의 {what}에 data-stage 가 없다"
    for gone in ("setOpp(", r"\/capture\s+"):
        assert gone not in dash, f"dash.html 에 정규식 고고학이 남아 있다: {gone}"

    # 2) 화면 목록의 정본은 원본 뷰의 view-def 다 — dash.html 은 그걸 읽고(매니페스트),
    #    자기가 정적으로 갖는 섹션은 templates/sections/*.html 의 section-def 로
    #    선언한다(dashboard.py._assemble 이 조립 시점에 끼운다). 예전에는 이 마크업이
    #    dash.html 안에서 createElement/innerHTML 로 런타임에 지어졌고, HOST_SEC 라는
    #    표가 어디에 붙는지를 따로 말했다 — 그 표와 dash.html 이 실제로 만드는 것이
    #    어긋날 수 있었다. 지금은 section-def 자체가 자리와 소속을 말하므로 어긋날
    #    길이 없다: 검사는 선언이 가리키는 자리가 실제로 있는지만 본다.
    defs = {}
    for p in sorted(views.glob("*.html")):
        d = re.search(r'class="view-def">\s*(\{.*?\})\s*</script>', p.read_text("utf-8"), re.S)
        assert d, f"{p.name} 에 view-def 선언이 없다"
        j = json.loads(d.group(1))
        defs[j["id"]] = j
    assert defs, "원본 뷰 선언을 하나도 못 읽었다"
    assert "window.__VIEWS__" in shell,         "원본 셸이 매니페스트를 안 읽는다 — 목록이 또 두 벌이다"
    assert "var VIEWS = [" not in dash, "dash.html 에 화면 목록 사본이 되살아났다"
    # 셸(레일·화면 상자·전환)은 원본 하나뿐이다. 애드온이 그걸 다시 구현하면 같은
    # 조립본이 배포마다 다른 몸으로 선다 — 섹션 순서가 실제로 갈라졌던 자리다.
    for gone, why in (("function place(", "배치"), ("function show(", "전환"),
                      ('nv.id = "sm-nav"', "메뉴")):
        assert gone not in dash, f"dash.html 이 셸의 {why}를 다시 구현한다: {gone}"
    assert "SM.sync(" in dash, "dash.html 이 셸에 덧붙이지 않는다: SM.sync("
    # 본문 서체는 한 벌이다. 애드온이 --sans 를 덮으면 같은 조립본이 배포마다 다른
    # 글자로 서고, 그러면 자간·줄바꿈·표 폭이 전부 달라진다(그걸 한 번 겪고 걷어냈다).
    # 등폭(--mono)은 예외다: 원본이 윈도우 기준이라 호스팅이 갈아끼운다.
    assert not re.search(r"--sans\s*:", dash),         "dash.html 이 본문 서체를 덮는다 — 서체는 원본(dashboard.html) 한 곳이다"

    # 원본 레일을 힘으로 덮지 않는다 — !important 는 "두 시스템이 싸우는 중"의 표식이다.
    # 주석은 근거가 못 된다(왜 걷어냈는지 적어 둔 자리가 검사에 걸리면 안 된다).
    bare = re.sub(r"/\*[\s\S]*?\*/", "", dash)
    bare = re.sub(r"^\s*//.*$", "", bare, flags=re.M)
    assert "!important" not in bare, "dash.html 이 원본 규칙을 !important 로 덮는다"

    # HOST_SEC/HOST_VIEWS 표는 구조적으로 없어졌다 — section-def 가 그 자리를
    # 대신한다. 되살아나면 사본이 두 벌이 된 것이다.
    for gone in ("var HOST_SEC", "var HOST_VIEWS"):
        assert gone not in dash,             f"{gone} 가 되살아났다 — templates/sections/*.html 의 section-def 로 옮겨라"

    import dashboard
    secs = dashboard.section_defs()
    assert secs, "호스팅 섹션 선언(templates/sections/*.html)을 하나도 못 읽었다"
    have = set(re.findall(r'id="([\w-]+)"', tpl))
    sec_ids = {s["id"] for s in secs}
    for s in secs:
        assert s.get("view") in defs, f"section-def {s['id']} 가 없는 화면을 가리킨다: {s.get('view')}"
        # after 는 원본 뷰의 섹션이거나 같은 화면의 다른 섹션 id 여도 된다(sm-dim ← sm-perf).
        assert s.get("after") in defs[s["view"]]["sections"] or s.get("after") in sec_ids,             f"{s['id']} 를 붙일 자리가 {s['view']} 에 없다: {s.get('after')}"
        assert s["id"] not in have,             f"{s['id']} 가 원본 뷰에 이미 있다 — 매니페스트가 소유할 것이다"
        assert f'"{s["id"]}"' in dash,             f"section-def 가 선언하는데 dash.html 이 쓰지 않는다(참조가 없다): {s['id']}"

    for d in defs.values():             # 원본 선언이 담는 요소는 원본에 있어야 한다
        for i in d["sections"]:
            assert i in have, f'{d["id"]} 의 view-def 가 없는 요소 id 를 담는다: {i}'
    stage_ids = {s for d in defs.values() for s in d["stages"]}

    # 3) 단계 용어표는 한 벌이다 — dash.html 은 자기 사본을 갖지 않고 조립이 실어
    #    보내는 window.__STAGES__(=STAGE_LABELS)를 읽는다.
    assert "var STAGE = {" not in dash,         "dash.html 에 단계 용어표 사본이 되살아났다 — stage.STAGE_LABELS 가 정본이다"
    assert "window.__STAGES__" in dash,         "dash.html 이 조립이 실어 보낸 단계 용어표(window.__STAGES__)를 안 읽는다"
    entries = STAGE_LABELS
    ours = {s["id"] for s in from_progress(_DEMO, "demo", "demo.com")["steps"]}
    assert ours <= set(entries), f"용어표에 없는 안내 단계: {sorted(ours - set(entries))}"
    assert stage_ids <= set(entries), f"용어표에 없는 실행 단계: {sorted(stage_ids - set(entries))}"
    for s in sorted(stage_ids):
        assert entries[s].get("run"), f"화면에서 돌리는 단계인데 run 라벨이 없다: {s}"
    for s in sorted(entries):
        assert entries[s].get("t"), f"단계 이름이 없다: {s}"

    # 4) 사이트 목록 → 대시보드의 이음매는 URL 의 hash 하나다. 대시보드는 그것만
    #    읽고(loadProjects), 비어 있으면 <select> 기본값인 첫 옵션이 잡힌다 —
    #    무엇을 눌러도 맨 처음 등록한 사이트가 열린다. 양쪽 끝을 함께 못 박는다.
    app_f = root / "server" / "app.py"
    if app_f.exists():
        assert '<li><a href="/d#' in app_f.read_text("utf-8"),             "사이트 목록 링크가 hash 없이 /d 로만 간다 — 무엇을 눌러도 첫 사이트가 열린다"
        assert "location.hash.slice(1)" in shell,             "대시보드가 hash 로 사이트를 고르지 않는다 — 링크가 실어 보낸 이름이 버려진다"

    # 5) 화면이 부르는 API 는 그 화면이 뜨는 **모든** 서버에 있어야 한다.
    #    경로 오타 하나면 fetch 가 조용히 404 로 죽고 화면에는 "불러오지 못했습니다"
    #    만 남는다 — 화면 파일도 서버 파일도 따로 보면 멀쩡하다. 원본 화면은 로컬과
    #    호스팅 양쪽에서 뜨므로 둘 다 검사한다(/api/data?date= 를 한쪽에만 넣는 실수).
    #    /api/setup/* 만 면제한다: 호스팅은 설정 화면을 통째로 숨긴다(dash.html).
    app_f = root / "server" / "app.py"
    local_f = root / "skills" / "capture" / "scripts" / "dashboard.py"
    if app_f.exists() and local_f.exists():
        app_src, local_src = app_f.read_text("utf-8"), local_f.read_text("utf-8")
        app_routes = set(re.findall(r'@app\.(?:get|post)\("([^"]+)"', app_src))
        assert app_routes, "server/app.py 의 라우트를 하나도 못 읽었다 — 표 모양이 바뀌었다"

        def api_calls(src):
            # 끝따옴표를 요구하지 않는다 — "/api/data?project=" + name 형태가 흔하다
            return set(re.findall(r'"(/api/[a-z][a-z/-]*)', src))

        for who, src, servers in (
                ("원본 화면", shell + "".join(p.read_text("utf-8")
                                            for p in sorted(views.glob("*.html"))),
                 (("로컬", None), ("호스팅", None))),
                ("dash.html", dash, (("호스팅", None),))):
            for call in sorted(api_calls(src)):
                for where, _ in servers:
                    if where == "호스팅":
                        if call.startswith("/api/setup/"):
                            continue          # 호스팅은 설정 화면 자체가 없다
                        assert call in app_routes,                             f"{who} 가 부르는데 호스팅 서버에 없다: {call}"
                    else:
                        assert f'"{call}"' in local_src,                             f"{who} 가 부르는데 로컬 서버에 없다: {call}"

    # 6) 화면에서 돌릴 수 있는 단계는 서버가 전부 받아야 한다. dash.html 의 용어표에
    #    run 라벨이 있으면 그 버튼이 /api/run 으로 그 id 를 보낸다 — 서버가 단계
    #    목록 사본을 들고 있으면 새 단계는 화면에만 생기고 눌렀을 때 "실행할 수
    #    없는 단계입니다: crawl" 로 튕긴다(실제로 crawl·metrics·backlinks 가 그랬다).
    import run_all
    runnable = {s for s, v in entries.items() if v.get("run")}
    assert runnable <= set(run_all.VALID_STAGE_NAMES),         f"화면은 돌리자는데 엔진 단계표에 없다: {sorted(runnable - set(run_all.VALID_STAGE_NAMES))}"
    if app_f.exists():
        assert "run_all.VALID_STAGE_NAMES" in app_f.read_text("utf-8"),             "app.py 가 단계 목록 사본을 들고 있다 — 화면에만 있는 단계가 400 으로 튕긴다"

    # 7) 원본은 기회 카드의 .acts 에서 클릭 전파를 멈춘다 — 카드가 같이 펼쳐지지
    #    않게 하려는 것이고, 원본 버튼들은 인라인 onclick 이라 영향이 없다.
    #    애드온이 그 안에 심는 버튼은 사정이 다르다: document 위임으로 잡으면
    #    이벤트가 영영 안 닿아 **버튼은 떠 있는데 눌러도 아무 일이 없다**.
    #    실제로 [콘텐츠 작성]이 그렇게 죽어 있었고, 렌더 검사도 그건 못 잡는다
    #    (DOM 에는 멀쩡히 있다). 그래서 여기서 계약으로 못 박는다.
    ov = (views / "overview.html").read_text("utf-8")
    if 'class="acts" onclick="event.stopPropagation()"' in ov:
        assert not re.search(r'document\.addEventListener\(\s*"click"[\s\S]{0,500}?data-write',
                             dash),             ".acts 안 버튼을 document 위임으로 잡는다 — 전파가 멈춰 클릭이 안 닿는다"
        assert re.search(r'data-write[\s\S]{0,400}?\.onclick\s*=', dash),             "애드온이 .acts 안 버튼에 자기 핸들러를 안 단다 — 눌러도 아무 일이 없다"


    # 8) 기회 종류는 한 벌이다 — 만드는 쪽(scoring.ALL_KINDS)과 말하는 쪽(셸의
    #    KIND_LABEL·PLAY)이 어긋나면 화면에 영문 kind 원문이 그대로 뜨거나
    #    (라벨 누락) 펼쳐도 할 일이 안 나온다(플레이북 누락). 실제로 그 반대가
    #    오래 있었다: 라벨과 플레이북은 있는데 **만드는 쪽이 없어서**
    #    ai_citation_gap·content_gap·aio_exposure 가 한 건도 안 생겼다.
    #    정본은 scoring.ALL_KINDS 다 — 여기서 양쪽 끝을 함께 검사한다.
    sc_f = root / "skills" / "capture" / "scripts" / "scoring.py"
    if sc_f.exists():
        m = re.search(r"ALL_KINDS = \((.*?)\)", sc_f.read_text("utf-8"), re.S)
        assert m, "scoring.ALL_KINDS 를 못 찾았다 — 기회 종류의 정본이 사라졌다"
        kinds = set(re.findall(r'"(\w+)"', m.group(1)))
        assert len(kinds) >= 8, f"ALL_KINDS 를 {len(kinds)}개밖에 못 읽었다 — 표 모양이 바뀌었다"

        lb = re.search(r"window\.KIND_LABEL = \{(.*?)\n?\};", shell, re.S)
        assert lb, "셸의 KIND_LABEL 을 못 찾았다"
        labels = set(re.findall(r"(\w+)\s*:", lb.group(1)))
        assert kinds <= labels, f"한국어 라벨이 없는 기회 종류: {sorted(kinds - labels)}"

        pl = re.search(r"window\.PLAY = \{(.*?)\n\};", shell, re.S)
        assert pl, "셸의 PLAY 플레이북을 못 찾았다"
        plays = set(re.findall(r"^  (\w+):", pl.group(1), re.M))
        assert kinds <= plays, f"할 일(PLAY)이 없는 기회 종류: {sorted(kinds - plays)}"

        # 반대 방향도 본다 — 라벨·플레이북만 있고 만드는 쪽이 없으면 그 화면은
        # 영원히 빈손이다. 이게 바로 ai_citation_gap 이 죽어 있던 모양이다.
        assert plays <= kinds, f"플레이북은 있는데 만드는 쪽이 없다: {sorted(plays - kinds)}"


    # 9) 화면이 말하는 명령과 그 화면이 선언한 단계는 같은 것을 가리켜야 한다.
    #    view-def 의 stages 는 "이 단계들이 이 화면을 채운다"는 선언이고, 호스팅은
    #    그걸 읽어 화면 머리에 실행 버튼을 단다. [키워드] 는 "키워드 발굴·경쟁사
    #    수집"이라 선언해 놓고 실제로는 GSC 스냅샷만 읽었다 — 버튼은 떴는데 눌러도
    #    화면이 안 채워졌다. 화면 자신이 빈 상태에서 부르는 명령이 정답을 알고 있다.
    #    (add·run 은 단계가 아니다: 질문 추가와 전 단계 일괄 실행.)
    NON_STAGE = {"add", "run"}
    #    다른 화면으로 넘기는 손잡이는 여기 적어 둔다 — 적지 않으면 검사에 걸린다.
    CROSS = {("competitors", "keywords")}     # 갭 검색어는 승인 대기 후보로 들어간다
    for p in sorted(views.glob("*.html")):
        vid = p.stem
        if vid not in defs:
            continue
        declared = set(defs[vid]["stages"])
        for cmd in sorted(set(re.findall(r"/capture ([a-z]+)", p.read_text("utf-8")))):
            if cmd in NON_STAGE or (vid, cmd) in CROSS:
                continue
            assert cmd in declared, (
                f"[{vid}] 화면이 /capture {cmd} 를 부르는데 view-def 의 stages 에 없다 "
                f"— 선언은 {sorted(declared)}. 그 단계가 이 화면을 채우면 stages 에 넣고, "
                f"다른 화면으로 넘기는 손잡이면 CROSS 에 적어라")


_DEMO = {"gsc_days": 0, "gsc_last": "", "keywords": 2, "keywords_found": 0,
         "ai_checks": 0, "ai_prompts": 0, "opps": 0, "creations": 0}


def _selfcheck() -> None:
    pr = _DEMO

    # 미연결 상태에서의 판정 검증
    st = from_progress(pr, "demo", "demo.com")
    assert st["here"] == 1 and st["steps"][1]["id"] == "gsc", st["here"]
    assert st["steps"][3]["cmd"] == "/capture add demo"       # 질문이 없으면 add 부터

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

    _check_seams()
    print("stage self-check ok")


if __name__ == "__main__":
    _selfcheck()
