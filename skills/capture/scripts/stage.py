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
         # 목록에서 빼지 않는다 — 순서는 그대로 두고 "지금 할 것"만 넘어간다.
         "skip": ai_skip,
         "state": ("건너뜀 — OpenRouter 연동하면 켜집니다" if ai_skip else
                   (f"답변 {p['ai_checks']}개 확인 · 질문 {p['ai_prompts']}개"
                    if p.get("ai_checks", 0)
                    else ("질문은 준비됨 · 아직 안 물어봄" if p.get("ai_prompts", 0)
                          else "물어볼 질문부터 필요"))),
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
    #    자기가 런타임에 만드는 것만 덧붙인다. 그 "덧붙인다"가 참인지가 이 검사다:
    #    덧붙인 id 는 dash.html 안에서 만들어져야 하고, 원본에 이미 있으면 안 된다
    #    (있으면 매니페스트가 소유할 것을 사본으로 들고 있는 것이다).
    defs = {}
    for p in sorted(views.glob("*.html")):
        d = re.search(r'class="view-def">\s*(\{.*?\})\s*</script>', p.read_text("utf-8"), re.S)
        assert d, f"{p.name} 에 view-def 선언이 없다"
        j = json.loads(d.group(1))
        defs[j["id"]] = j
    assert defs, "원본 뷰 선언을 하나도 못 읽었다"
    assert "window.__VIEWS__" in dash, "dash.html 이 매니페스트를 안 읽는다 — 목록이 또 두 벌이다"
    assert "var VIEWS = [" not in dash, "dash.html 에 화면 목록 사본이 되살아났다"

    hs = re.search(r"var HOST_SEC = \[(.*?)\n  \];", dash, re.S)
    hv = re.search(r"var HOST_VIEW = \[(.*?)\];", dash, re.S)
    assert hs and hv, "dash.html 의 호스팅 전용 목록(HOST_SEC/HOST_VIEW)을 못 찾았다"
    hv_m = re.match(r'\s*"(\w+)",\s*"[^"]*",\s*\[([^\]]*)\],\s*\[([^\]]*)\],\s*"(\w+)"',
                    hv.group(1))
    assert hv_m, "HOST_VIEW 의 모양이 바뀌었다"
    rows = re.findall(r'\["([\w-]+)",\s*"([\w-]+)",\s*"([\w-]+)"\]', hs.group(1))
    assert len(rows) >= 4, f"HOST_SEC 행을 {len(rows)}개밖에 못 읽었다 — 표 모양이 바뀌었다"

    body = dash.replace(hs.group(0), "").replace(hv.group(0), "")   # 표 자신은 근거가 못 된다
    host_ids = [sec for _, sec, _ in rows] + re.findall(r'"([\w-]+)"', hv_m.group(2))
    for i in host_ids:
        assert f'"{i}"' in body, f"덧붙인다고 선언했는데 dash.html 이 만들지 않는다: {i}"
        assert f'id="{i}"' not in tpl, f"원본 뷰에 이미 있다 — 매니페스트가 소유할 것이다: {i}"
    # 끼워 넣는 자리 — 못 찾으면 dash.html 이 말없이 맨 끝에 붙여 섹션 순서가 바뀐다.
    # 앞줄이 넣은 것 뒤에도 붙일 수 있으므로(sm-dim ← sm-perf) 표 순서대로 쌓아 본다.
    merged = {k: list(v["sections"]) for k, v in defs.items()}
    for vid, sec, anchor in rows:
        assert vid in defs, f"HOST_SEC 가 없는 화면에 붙는다: {vid}"
        assert anchor in merged[vid], f"{sec} 를 붙일 자리가 {vid} 에 없다: {anchor}"
        merged[vid].insert(merged[vid].index(anchor) + 1, sec)
    assert hv_m.group(4) in defs, f"HOST_VIEW 를 넣을 자리가 없다: {hv_m.group(4)}"
    assert hv_m.group(1) not in defs, f"원본 뷰가 있는데 호스팅 화면으로 또 만든다: {hv_m.group(1)}"

    have = set(re.findall(r'id="([\w-]+)"', tpl))
    for d in defs.values():             # 원본 선언이 담는 요소는 원본에 있어야 한다
        for i in d["sections"]:
            assert i in have, f'{d["id"]} 의 view-def 가 없는 요소 id 를 담는다: {i}'
    stage_ids = {s for d in defs.values() for s in d["stages"]}
    stage_ids |= set(re.findall(r'"(\w+)"', hv_m.group(3)))

    # 3) 단계 용어표는 한 벌이다 — 우리가 내보내는 id 를 전부 알아야 한다.
    g = re.search(r"var STAGE = \{(.*?)\n  \};", dash, re.S)
    assert g, "dash.html 의 STAGE 용어표를 못 찾았다"
    entries = dict(re.findall(r"^\s{4}(\w+):\s*\{(.*?)\}", g.group(1), re.S | re.M))
    ours = {s["id"] for s in from_progress(_DEMO, "demo", "demo.com")["steps"]}
    assert ours <= set(entries), f"용어표에 없는 안내 단계: {sorted(ours - set(entries))}"
    assert stage_ids <= set(entries), f"용어표에 없는 실행 단계: {sorted(stage_ids - set(entries))}"
    for s in sorted(stage_ids):
        assert "run:" in entries[s], f"화면에서 돌리는 단계인데 run 라벨이 없다: {s}"
    for s in sorted(entries):
        assert "t:" in entries[s], f"단계 이름이 없다: {s}"


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
