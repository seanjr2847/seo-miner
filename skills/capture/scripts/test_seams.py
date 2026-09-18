#!/usr/bin/env python3
"""화면 쪽 이음매 점검 — 브라우저를 안 띄우고 확인할 수 있는 만큼만.

호스팅판(server/assets/dash.html)은 원본 화면 뒤에 얹히는 애드온이라, 원본이
말없이 바뀌면 조용히 멈춘다. 예전에는 그 이음매가 렌더된 한국어였다 —
버튼 라벨의 정규식으로 단계 id 를, onclick 문자열의 정규식으로 기회 id 를
되찾았다. 지금은 data- 속성과 id 키 조회다. 여기서 그 계약을 지킨다.

리포 밖(플러그인 설치본)에는 server/ 가 없다 — 그때는 각 검사가 조용히 건너뛴다.

원래 stage.py 의 _check_seams() 한 함수 안에 13개 블록(주석 번호 #1~#13)으로
있던 것을 검사 하나 = 함수 하나로 옮겼다. #7(.acts 클릭 위임)은 이후 없앴다 —
그 버튼(SM.host.oppBtn)이 렌더 시점에 자기 onclick 을 직접 다는 구조가 되면서,
가드 조건(overview.html 에 그 마크업이 있는지)이 원래도 늘 거짓이라 몸통이 한
번도 안 돌던 죽은 검사였다(마크업은 dashboard.html 의 oppActs() 가 만든다). 번호는
비워 둔 채 남겨 뒀다 — 나머지 함수 이름을 옮기면 그 자체가 또 하나의 diff 가 된다.
CLAUDE.md 의 규율: 검사를 추가했으면 일부러 깨서 FAIL 이 나는 것까지 확인한다 —
안 그러면 통과하는 검사가 아니라 아무것도 안 보는 검사가 된다.

self-check: python test_seams.py
"""
import json
import re
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
ROOT = Path(__file__).resolve().parents[3]
SETUP_SCRIPTS = Path(__file__).resolve().parents[2] / "setup" / "scripts"
sys.path.insert(0, str(ROOT))   # server.app — 라우트 표의 정본을 import 로 읽는다

import db     # noqa: E402
import stage  # noqa: E402


def _server():
    """호스팅 서버 모듈(server/app.py) 자체 — 라우트 표도 carry 형식도 여기 산다.

    예전에는 이 파일의 **원문**을 정규식으로 긁었다(`@app.get("...")`,
    `CARRY_FIELDS = (...)`). 그러면 표 모양이 조금만 바뀌어도 — 데코레이터를 감싸거나
    router 로 옮기거나 튜플을 여러 줄로 펴거나 — 검사가 조용히 0개를 읽는다. 그래서
    "하나도 못 읽었다" 가드를 따로 세워야 했는데, 그 가드가 곧 정본을 안 보고 있다는
    자백이었다. 모듈을 그대로 import 하면 서버가 실제로 등록·선언한 것을 본다.

    리포 밖(server/ 없음)이거나 fastapi 가 없는 곳에서는 None — 부르는 쪽이 조용히
    건너뛴다. _load() 와 같은 규칙이다.
    """
    if not (ROOT / "server" / "app.py").exists():
        return None
    try:
        import server.app as srv
    except ImportError:
        return None
    return srv


def _server_paths():
    """호스팅 서버가 **실제로 등록한** 경로 집합 — 라우트 표의 정본.

    FastAPI 문서 경로(/docs·/openapi.json 등)가 함께 오지만 부르는 쪽은 전부
    "이 경로가 있는가"만 물으므로 상관없다.
    """
    srv = _server()
    return None if srv is None else {r.path for r in srv.app.routes if hasattr(r, "path")}


def _load():
    """공용 파일 읽기 — 13개 검사가 우려먹는 원본 셸·뷰·애드온.

    리포 밖(플러그인 설치본)에는 server/ 가 없다 — 그때는 None 을 돌려주고
    부르는 쪽이 조용히 건너뛴다.
    """
    dash_f = ROOT / "server" / "assets" / "dash.html"
    views = ROOT / "skills" / "capture" / "templates" / "views"
    shell_f = ROOT / "skills" / "capture" / "templates" / "dashboard.html"
    if not (dash_f.exists() and views.is_dir() and shell_f.exists()):
        return None
    dash = dash_f.read_text("utf-8")
    shell = shell_f.read_text("utf-8")
    tpl = shell + "".join(p.read_text("utf-8") for p in sorted(views.glob("*.html")))
    return dict(
        dash=dash, shell=shell, tpl=tpl, views=views,
        app_f=ROOT / "server" / "app.py",
        local_f=SCRIPTS / "dashboard.py",
        sc_f=SCRIPTS / "scoring.py",
    )


def _view_defs(views):
    """뷰마다 있는 view-def 선언을 읽는다 — 화면 목록의 정본."""
    defs = {}
    for p in sorted(views.glob("*.html")):
        m = re.search(r'class="view-def">\s*(\{.*?\})\s*</script>', p.read_text("utf-8"), re.S)
        assert m, f"{p.name} 에 view-def 선언이 없다"
        j = json.loads(m.group(1))
        defs[j["id"]] = j
    assert defs, "원본 뷰 선언을 하나도 못 읽었다"
    return defs


def _stage_ids(defs):
    return {s for v in defs.values() for s in v["stages"]}


def _chip_stage_ids(defs):
    """화면이 **칩으로 내놓는** 단계 — stages ∪ head.

    셸(dashboard.html)이 그렇게 읽는다: `head: (v.head || v.stages || [])`.
    head 를 안 적은 뷰는 stages 전부가 머리 칩이 되고, 적은 뷰는 그 부분집합만
    머리에 서지만 stages 쪽은 여전히 띠·빈 상태의 명령으로 나간다. 둘 중 하나만
    보면 칩이 새 나간다.
    """
    return {s for v in defs.values() for s in (list(v["stages"]) + list(v.get("head") or []))}


def _gather(name, *, ga4=False):
    """빈 Brain 하나로 gather() 를 한 번 돌려 페이로드를 받는다 — 화면·요청문이
    실제로 받는 것과 같은 자료다. 소스를 긁는 대신 이걸 본다.

    ga4=True 면 GA4 스냅샷도 심는다: gather() 의 GA4 다섯 키(ga4_funnel 등)는 연결된
    사이트에만 조건부로 실려서, 안 심으면 그 키를 읽는 화면이 전부 여기서만 걸린다.
    """
    import contextlib
    import io as _io
    import sqlite3 as _sq

    import dashboard
    c = _sq.connect(":memory:")
    c.row_factory = _sq.Row
    c.executescript(db.SCHEMA)
    c.execute("INSERT INTO projects(id,name,type,domain) VALUES(1,?,'saas','x.com')", (name,))
    if ga4:
        c.execute("INSERT INTO gsc_snapshots(project_id,snapshot_date,period_days,query,"
                  "clicks,impressions,ctr,position) VALUES(1,'2026-01-01',28,?,1,1,1.0,1.0)",
                  (name,))
        c.execute("INSERT INTO ga4_snapshots(project_id,snapshot_date,period_days,"
                  "landing_page,sessions,sessions_all,key_events)"
                  " VALUES(1,'2026-01-01',28,'/',1,1,0)")
    null = _io.StringIO()   # yaml 없는 프로젝트라 경고가 뜬다 — 검사 출력에 섞지 않는다
    with contextlib.redirect_stdout(null), contextlib.redirect_stderr(null):
        out = dashboard.gather(c, db.get_project(c, name))
    c.close()
    return out


def test_seam_01_view_ids_from_payload():
    """1) id 는 페이로드에서 온다 — 렌더된 글자에서 되짚지 않는다."""
    ctx = _load()
    if ctx is None:
        return
    views, shell, dash = ctx["views"], ctx["shell"], ctx["dash"]
    assert 'data-opp="${o.id}"' in (views / "overview.html").read_text("utf-8"), \
        "oppRow() 가 기회 id 를 data-opp 로 안 내보낸다"
    for what, pat in (("단계 칸", r'class="stp \$\{cls\}"[^>]*data-stage='),
                      ("실행 칩", r'<button class="cmd"[^>]*data-stage='),
                      ("배너 이름", r"<b data-stage=")):
        assert re.search(pat, shell), f"renderGuide() 의 {what}에 data-stage 가 없다"
    for gone in ("setOpp(", r"\/capture\s+"):
        assert gone not in dash, f"dash.html 에 정규식 고고학이 남아 있다: {gone}"


def test_seam_02_view_list_single_source():
    """2) 화면 목록의 정본은 원본 뷰의 view-def 다 — dash.html 은 그걸 읽고(매니페스트),
    자기가 정적으로 갖는 섹션은 templates/sections/*.html 의 section-def 로
    선언한다(dashboard.py._assemble 이 조립 시점에 끼운다). 예전에는 이 마크업이
    dash.html 안에서 createElement/innerHTML 로 런타임에 지어졌고, HOST_SEC 라는
    표가 어디에 붙는지를 따로 말했다 — 그 표와 dash.html 이 실제로 만드는 것이
    어긋날 수 있었다. 지금은 section-def 자체가 자리와 소속을 말하므로 어긋날
    길이 없다: 검사는 선언이 가리키는 자리가 실제로 있는지만 본다.
    """
    ctx = _load()
    if ctx is None:
        return
    views, shell, dash, tpl = ctx["views"], ctx["shell"], ctx["dash"], ctx["tpl"]
    defs = _view_defs(views)
    assert "window.__VIEWS__" in shell, \
        "원본 셸이 매니페스트를 안 읽는다 — 목록이 또 두 벌이다"
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
    assert not re.search(r"--sans\s*:", dash), \
        "dash.html 이 본문 서체를 덮는다 — 서체는 원본(dashboard.html) 한 곳이다"

    # 원본 레일을 힘으로 덮지 않는다 — !important 는 "두 시스템이 싸우는 중"의 표식이다.
    # 주석은 근거가 못 된다(왜 걷어냈는지 적어 둔 자리가 검사에 걸리면 안 된다).
    bare = re.sub(r"/\*[\s\S]*?\*/", "", dash)
    bare = re.sub(r"^\s*//.*$", "", bare, flags=re.M)
    assert "!important" not in bare, "dash.html 이 원본 규칙을 !important 로 덮는다"

    # HOST_SEC/HOST_VIEWS 표는 구조적으로 없어졌다 — section-def 가 그 자리를
    # 대신한다. 되살아나면 사본이 두 벌이 된 것이다.
    for gone in ("var HOST_SEC", "var HOST_VIEWS"):
        assert gone not in dash, \
            f"{gone} 가 되살아났다 — templates/sections/*.html 의 section-def 로 옮겨라"

    import dashboard
    secs = dashboard.section_defs()
    assert secs, "호스팅 섹션 선언(templates/sections/*.html)을 하나도 못 읽었다"
    have = set(re.findall(r'id="([\w-]+)"', tpl))
    sec_ids = {s["id"] for s in secs}
    for s in secs:
        assert s.get("view") in defs, f"section-def {s['id']} 가 없는 화면을 가리킨다: {s.get('view')}"
        # after 는 원본 뷰의 섹션이거나 같은 화면의 다른 섹션 id 여도 된다(sm-dim ← sm-perf).
        assert s.get("after") in defs[s["view"]]["sections"] or s.get("after") in sec_ids, \
            f"{s['id']} 를 붙일 자리가 {s['view']} 에 없다: {s.get('after')}"
        assert s["id"] not in have, \
            f"{s['id']} 가 원본 뷰에 이미 있다 — 매니페스트가 소유할 것이다"
        assert f'"{s["id"]}"' in dash, \
            f"section-def 가 선언하는데 dash.html 이 쓰지 않는다(참조가 없다): {s['id']}"

    for v in defs.values():             # 원본 선언이 담는 요소는 원본에 있어야 한다
        for i in v["sections"]:
            assert i in have, f'{v["id"]} 의 view-def 가 없는 요소 id 를 담는다: {i}'


def test_seam_03_stage_label_table_single_source():
    """3) 단계 용어표는 한 벌이다 — dash.html 은 자기 사본을 갖지 않고 조립이 실어
    보내는 window.__STAGES__(=STAGE_LABELS)를 읽는다.
    """
    ctx = _load()
    if ctx is None:
        return
    dash, views = ctx["dash"], ctx["views"]
    defs = _view_defs(views)
    stage_ids = _stage_ids(defs)
    assert "var STAGE = {" not in dash, \
        "dash.html 에 단계 용어표 사본이 되살아났다 — stage.STAGE_LABELS 가 정본이다"
    assert "window.__STAGES__" in dash, \
        "dash.html 이 조립이 실어 보낸 단계 용어표(window.__STAGES__)를 안 읽는다"
    entries = stage.STAGE_LABELS
    ours = {s["id"] for s in stage.from_progress(stage._DEMO, "demo", "demo.com")["steps"]}
    assert ours <= set(entries), f"용어표에 없는 안내 단계: {sorted(ours - set(entries))}"
    assert stage_ids <= set(entries), f"용어표에 없는 실행 단계: {sorted(stage_ids - set(entries))}"
    for s in sorted(stage_ids):
        assert entries[s].get("run"), f"화면에서 돌리는 단계인데 run 라벨이 없다: {s}"
    for s in sorted(entries):
        assert entries[s].get("t"), f"단계 이름이 없다: {s}"


def test_seam_04_site_list_hash_link():
    """4) 사이트 목록 → 대시보드의 이음매는 URL 의 hash 하나다. 대시보드는 그것만
    읽고(loadProjects), 비어 있으면 <select> 기본값인 첫 옵션이 잡힌다 —
    무엇을 눌러도 맨 처음 등록한 사이트가 열린다. 양쪽 끝을 함께 못 박는다.
    """
    ctx = _load()
    if ctx is None:
        return
    shell, app_f = ctx["shell"], ctx["app_f"]
    if app_f.exists():
        # 줄 전체가 <a> 이던 시절의 literal(`<li><a href="/d#`)로는 못 본다 — 줄 안에
        # 상태 배지와 "대시보드 열기" 버튼이 따로 서면서 링크가 <li> 안쪽으로 들어갔다.
        # 못 박는 것은 그때나 지금이나 하나다: 그 링크가 hash 를 싣는가.
        assert re.search(r'<li class=[^>]*>.*?href="/d#', app_f.read_text("utf-8"), re.S), \
            "사이트 목록 링크가 hash 없이 /d 로만 간다 — 무엇을 눌러도 첫 사이트가 열린다"
        assert "location.hash.slice(1)" in shell, \
            "대시보드가 hash 로 사이트를 고르지 않는다 — 링크가 실어 보낸 이름이 버려진다"


def test_seam_05_api_calls_exist_on_servers():
    """5) 화면이 부르는 API 는 그 화면이 뜨는 **모든** 서버에 있어야 한다.
    경로 오타 하나면 fetch 가 조용히 404 로 죽고 화면에는 "불러오지 못했습니다"
    만 남는다 — 화면 파일도 서버 파일도 따로 보면 멀쩡하다. 원본 화면은 로컬과
    호스팅 양쪽에서 뜨므로 둘 다 검사한다(/api/data?date= 를 한쪽에만 넣는 실수).
    /api/setup/* 만 면제한다: 호스팅은 설정 화면을 통째로 숨긴다(dash.html).
    양쪽 서버 모두 소스가 아니라 **등록된 경로 집합**을 본다 — 호스팅은
    app.routes(_server_paths), 로컬은 dashboard.ROUTES(+LOCAL_ONLY_PATHS)다.
    """
    import dashboard
    ctx = _load()
    if ctx is None:
        return
    views, shell, dash = ctx["views"], ctx["shell"], ctx["dash"]
    app_routes = _server_paths()

    def api_calls(src):
        # 끝따옴표를 요구하지 않는다 — "/api/data?project=" + name 형태가 흔하다.
        # 숫자를 받는다 — 안 받으면 /api/ga4/... 를 /api/ga 로 잘라 읽어서,
        # 서버에 라우트를 제대로 만들어 놔도 이 검사가 영영 어긋난다.
        return set(re.findall(r'"(/api/[a-z][a-z0-9/-]*)', src))

    for who, src, servers in (
            ("원본 화면", shell + "".join(p.read_text("utf-8")
                                        for p in sorted(views.glob("*.html"))),
             ("로컬", "호스팅")),
            ("dash.html", dash, ("호스팅",))):
        for call in sorted(api_calls(src)):
            for where in servers:
                if where == "로컬":
                    assert call in dashboard.LOCAL_PATHS, \
                        f"{who} 가 부르는데 로컬 서버에 없다: {call}"
                elif app_routes is not None and not call.startswith("/api/setup/"):
                    assert call in app_routes, \
                        f"{who} 가 부르는데 호스팅 서버에 없다: {call}"   # setup 은 면제


def test_seam_06_runnable_stages_known_to_server():
    """6) 화면에서 돌릴 수 있는 단계는 서버가 전부 받아야 한다. dash.html 의 용어표에
    run 라벨이 있으면 그 버튼이 /api/run 으로 그 id 를 보낸다 — 서버가 단계
    목록 사본을 들고 있으면 새 단계는 화면에만 생기고 눌렀을 때 "실행할 수
    없는 단계입니다: crawl" 로 튕긴다(실제로 crawl·metrics·backlinks 가 그랬다).
    """
    ctx = _load()
    if ctx is None:
        return
    app_f = ctx["app_f"]
    import run_all
    entries = stage.STAGE_LABELS
    runnable = {s for s, v in entries.items() if v.get("run")}
    assert runnable <= set(run_all.VALID_STAGE_NAMES), \
        f"화면은 돌리자는데 엔진 단계표에 없다: {sorted(runnable - set(run_all.VALID_STAGE_NAMES))}"
    if app_f.exists():
        assert "run_all.VALID_STAGE_NAMES" in app_f.read_text("utf-8"), \
            "app.py 가 단계 목록 사본을 들고 있다 — 화면에만 있는 단계가 400 으로 튕긴다"


def test_seam_08_opportunity_kind_labels_match():
    """8) 기회 종류의 라벨·처방(what/acts/deliver)은 이제 scoring.py 의 KINDS
    명부가 정하고 dashboard.py 의 gather() 가 label·play 로 실어 보낸다 —
    화면은 그리기만 한다(window.KIND_LABEL·PLAY 는 없앴다). 그쪽 정합성은
    scoring.py 자체 self-check(set(_KIND_SPECS) == set(ALL_KINDS),
    all(k.play for k in KINDS))가 지킨다 — 여기서 다시 볼 게 없다.

    화면에 남은 유일한 kind 사본은 isDefensive() 의 배열 폴백이다
    (o.is_defensive 가 없는 옛 박제본에서만 쓰인다) — scoring.DEFENSIVE_KINDS
    와 어긋나면 옛 박제본에서 방어 기회가 덜 잡히거나(2종만 알던 시절처럼)
    엉뚱한 게 방어로 뜬다.
    """
    ctx = _load()
    if ctx is None:
        return
    shell, sc_f = ctx["shell"], ctx["sc_f"]
    if sc_f.exists():
        import scoring
        df = re.search(
            r"const isDefensive = o => o && \(o\.is_defensive\s*\n?\s*\?\?\s*\[(.*?)\]",
            shell, re.S)
        assert df, "셸의 isDefensive() 폴백 목록을 못 찾았다"
        js_defensive = set(re.findall(r'"(\w+)"', df.group(1)))
        assert js_defensive == set(scoring.DEFENSIVE_KINDS), \
            f"isDefensive() 폴백이 scoring.DEFENSIVE_KINDS 와 어긋났다: " \
            f"{js_defensive ^ set(scoring.DEFENSIVE_KINDS)}"

        # 색인 실패 갈래(bucket)도 한 벌이다 — 만드는 쪽(scoring.INDEX_BUCKETS)과
        # site.html 의 ST_IX(갈래별 라벨·심각도·처방)가 어긋나면 새 갈래가 화면에
        # 원문 그대로 뜨거나 처방 없이 걸린다.
        st_f = ROOT / "skills" / "capture" / "templates" / "views" / "site.html"
        ix = re.search(r"const ST_IX = \{(.*?)\n\};", st_f.read_text("utf-8"), re.S)
        assert ix, "site.html 의 ST_IX 를 못 찾았다"
        ix_buckets = set(re.findall(r"^  (\w+):", ix.group(1), re.M))
        assert ix_buckets == set(scoring.INDEX_BUCKETS), \
            f"site.html 의 ST_IX 가 scoring.INDEX_BUCKETS 와 어긋났다: " \
            f"{ix_buckets ^ set(scoring.INDEX_BUCKETS)}"


def test_seam_09_view_commands_match_declared_stages():
    """9) 화면이 말하는 명령과 그 화면이 선언한 단계는 같은 것을 가리켜야 한다.
    view-def 의 stages 는 "이 단계들이 이 화면을 채운다"는 선언이고, 호스팅은
    그걸 읽어 화면 머리에 실행 버튼을 단다. [키워드] 는 "키워드 발굴·경쟁사
    수집"이라 선언해 놓고 실제로는 GSC 스냅샷만 읽었다 — 버튼은 떴는데 눌러도
    화면이 안 채워졌다. 화면 자신이 빈 상태에서 부르는 명령이 정답을 알고 있다.
    (add·run 은 단계가 아니다: 질문 추가와 전 단계 일괄 실행.)
    """
    ctx = _load()
    if ctx is None:
        return
    views = ctx["views"]
    defs = _view_defs(views)
    NON_STAGE = {"add", "run"}
    # 다른 화면으로 넘기는 손잡이는 여기 적어 둔다 — 적지 않으면 검사에 걸린다.
    CROSS = {("competitors", "keywords"),     # 갭 검색어는 승인 대기 후보로 들어간다
             # [주제별] 축은 keywords 단계가 아니라 그 단계의 **큐레이션**(Claude 가
             # cluster 를 붙이는 일)이 채운다 — 화면에는 그걸 붙일 자리가 없다.
             ("analysis", "keywords")}
    for p in sorted(views.glob("*.html")):
        vid = p.stem
        if vid not in defs:
            continue
        declared = set(defs[vid]["stages"])
        # 화면이 단계를 부르는 세 모양: 명령 칩 글자, 셸의 act("x", …),
        # 호스팅 실행 버튼 data-run="x". 글자만 보면 뒤의 둘이 그냥 새 나간다.
        src = p.read_text("utf-8")
        called = (set(re.findall(r"/capture ([a-z]+)", src))
                  | set(re.findall(r"""act\(\s*["']([a-z]+)["']""", src))
                  | set(re.findall(r'data-run="([a-z0-9]+)"', src)))
        for cmd in sorted(called):
            if cmd in NON_STAGE or (vid, cmd) in CROSS:
                continue
            assert cmd in declared, (
                f"[{vid}] 화면이 /capture {cmd} 를 부르는데 view-def 의 stages 에 없다 "
                f"— 선언은 {sorted(declared)}. 그 단계가 이 화면을 채우면 stages 에 넣고, "
                f"다른 화면으로 넘기는 손잡이면 CROSS 에 적어라")


def test_seam_09a_section_buttons_match_declared_stages():
    """9a) 호스팅 전용 섹션이 자기 마크업에 다는 실행 버튼도 같은 계약을 탄다.

    9번은 views/*.html 만 훑는다. 호스팅 섹션은 그 바깥이라 그쪽 끝이 비어
    있었다 — [키워드] 사고("키워드 발굴이라 선언해 놓고 GSC 만 읽었다")가
    호스팅 섹션에서 다시 나도 안 잡혔다. 귀속을 추측할 필요는 없다: 그 파일의
    section-def 가 이미 어느 화면 것인지 말한다.
    """
    ctx = _load()
    if ctx is None:
        return
    defs = _view_defs(ctx["views"])
    secs = ROOT / "skills" / "capture" / "templates" / "sections"
    for sp in sorted(secs.glob("*.html")):
        src = sp.read_text("utf-8")
        m = re.search(r'class="section-def">\s*(\{.*?\})\s*</script>', src, re.S)
        assert m, f"{sp.name} 에 section-def 선언이 없다"
        j = json.loads(m.group(1))
        declared = set(defs[j["view"]]["stages"])
        for st in sorted(set(re.findall(r'data-run="([a-z0-9]+)"', src))):
            assert st in declared, (
                f'[{j["id"]}] 섹션이 data-run="{st}" 버튼을 다는데 그 섹션이 속한 '
                f'[{j["view"]}] 화면의 stages 에 없다 — 선언은 {sorted(declared)}')


def test_seam_09b_addon_buttons_match_declared_stages():
    """9b) 애드온(dash.html)이 섹션 안에 심는 실행 버튼도 마찬가지다.

    이쪽은 마크업이 렌더 함수 안의 문자열이라 선언이 딸려 오지 않는다. 대신
    dash.html 은 섹션 하나당 렌더 함수 하나라, 버튼을 **바로 앞에 나온 섹션
    id** 로 귀속시키면 흔들리지 않는다. 귀속이 안 되면 조용히 넘기지 않고
    거기서 실패시킨다 — 어느 화면 것인지 못 정하는 버튼은 검사할 수가 없다.
    """
    ctx = _load()
    if ctx is None:
        return
    import dashboard
    defs = _view_defs(ctx["views"])
    secs = dashboard.section_defs()
    # 주석은 근거가 못 된다 — data-run 을 설명하는 주석줄이 걸리면 안 된다.
    # 위치를 세는 스캔이라 길이를 지키며 지운다.
    bare = re.sub(r"/\*[\s\S]*?\*/",
                  lambda m: re.sub(r"\S", " ", m.group(0)), ctx["dash"])
    bare = re.sub(r"^([ 	]*)//.*$",
                  lambda m: m.group(1) + " " * (len(m.group(0)) - len(m.group(1))),
                  bare, flags=re.M)
    assert len(bare) == len(ctx["dash"]), "주석을 지우며 길이가 틀어졌다 — 위치가 어긋난다"
    at = sorted((m.start(), s["id"]) for s in secs
                for m in re.finditer(f'"{re.escape(s["id"])}"', bare))
    view_of = {s["id"]: s["view"] for s in secs}
    for m in re.finditer(r'data-run="([a-z0-9]+)"', bare):
        if m.group(1) in {"add", "run"}:
            continue          # 레일 바닥의 일괄 실행은 어느 화면 것도 아니다
        owner = next((sid for pos, sid in reversed(at) if pos < m.start()), None)
        assert owner, (
            f'dash.html 의 data-run="{m.group(1)}" 앞에 섹션 id 가 없다 — 어느 '
            "화면 버튼인지 못 정한다. 그 섹션 안에서 만들어라")
        vid = view_of[owner]
        declared = set(defs[vid]["stages"])
        assert m.group(1) in declared, (
            f'[{owner}] 섹션이 data-run="{m.group(1)}" 버튼을 다는데 그 섹션이 속한 '
            f"[{vid}] 화면의 stages 에 없다 — 선언은 {sorted(declared)}")


def test_seam_09c_run_button_delegation():
    """9c) 실행 버튼은 표식(data-run)으로 받는다 — 자리(.next/.empty)로 받으면
    새 자리마다 조용히 죽는다(펼침 패널의 버튼이 실제로 그랬다). 대신 자기
    onclick 을 가진 둘은 반드시 빼야 한다. 안 빼면 한 번 누른 게 두 번 돈다:
      · .sm-refresh   — runButtons() 가 자기 onclick 을 건다
      · 인라인 onclick — act() 의 로컬 갈래가 data-run 과 onclick 을 같이 낸다
    """
    ctx = _load()
    if ctx is None:
        return
    dash = ctx["dash"]
    assert 'closest("[data-run]")' in dash, (
        "dash.html 이 data-run 을 표식으로 안 받는다 — 자리로 고르면 새 자리마다 "
        "버튼이 조용히 죽는다")
    for guard, why in (('classList.contains("sm-refresh")', "머리줄 버튼"),
                       ('hasAttribute("onclick")', "인라인 onclick 버튼")):
        assert guard in dash, (
            f"dash.html 의 data-run 위임이 {why}을 안 뺀다 — 한 번 누른 게 두 번 돈다")


def test_seam_10_gather_payload_keys_match():
    """10) 화면이 읽는 페이로드 키는 gather() 가 실제로 싣는 것이어야 한다.
    이 이음매는 양쪽 다 멀쩡해 보인다: 뷰는 정상적인 자바스크립트고 gather 는
    정상적인 dict 다. 어긋나면 조용히 undefined 가 흘러 화면에 "—" 나 빈 표가
    뜰 뿐, 콘솔에도 검사에도 아무것도 안 남는다. 뷰 렌더러의 인자 이름은
    관례가 아니라 계약이다(VIEW(id, function (d) {...})) — 그래서 d.* 로 센다.
    """
    ctx = _load()
    if ctx is None:
        return
    views = ctx["views"]
    served = set(_gather("_seam", ga4=True))
    read = set()
    for p in sorted(views.glob("*.html")):
        read |= {m.group(1) for m in re.finditer(r"\bd\.([a-zA-Z_]\w*)",
                                                p.read_text("utf-8"))}
    assert read <= served, (
        f"화면이 읽는데 gather() 가 안 싣는 페이로드 키: {sorted(read - served)}")


def test_seam_11_carry_fields_match():
    """11) 로컬이 실어 보내는 사이트 설정(carry)과 호스팅이 꺼내 쓰는 이름은 한 벌이다.
    이 이음매도 양쪽 다 멀쩡해 보인다: 로컬은 정상적인 링크를 만들고 호스팅은
    정상적인 dict 를 읽는다. 이름이 하나 어긋나면 carry_read 가 그것을 걸러
    버려서(정본은 PREFILL_KEYS) **씨앗 키워드만 조용히 빈 채로** 등록된다 —
    화면에도 로그에도 아무것도 안 남는다. 그래서 이름을 여기서 대조한다.
    """
    ctx = _load()
    if ctx is None:
        return
    import dashboard
    srv = _server()
    if srv is not None:
        app_src = ctx["app_f"].read_text("utf-8")
        assert "dashboard.carry_read" in app_src, \
            "호스팅이 carry 를 직접 푼다 — 형식의 정본은 dashboard 의 carry_pack/carry_read 다"
        used = set(srv.CARRY_FIELDS)      # 서버가 선언한 것 그대로 — 소스를 안 긁는다
        used |= set(re.findall(r"CARRY\.(\w+)",
                               (ROOT / "server" / "app.html").read_text("utf-8")))
        used.add("gsc_property")          # 어느 속성에 얹을지 — 아래에서 쓰는지 본다
        assert used <= set(dashboard.PREFILL_KEYS), \
            f"호스팅이 carry 에서 꺼내는데 로컬이 싣지 않는 이름: " \
            f"{sorted(used - set(dashboard.PREFILL_KEYS))}"
        assert 'carry.get("gsc_property"' in app_src, \
            "호스팅이 carry 가 가리키는 속성을 안 본다 — 남의 사이트에 씨앗이 얹힌다"

        # 단계 용어표는 한 벌이다 — 등록 화면(app.html)도 사본을 갖지 않는다.
        # 대시보드에 대해 위 3) 이 지키는 것과 같은 계약이다.
        app_html = (ROOT / "server" / "app.html").read_text("utf-8")
        assert "window.__STAGES__" in app_html, \
            "app.html 이 서버가 실어 보낸 단계 용어표를 안 읽는다"
        assert "__STAGES__=stage.STAGE_LABELS" in app_src, \
            "app.py 가 등록 화면에 단계 용어표를 안 싣는다 — 화면이 사본을 갖게 된다"


def test_seam_12_remote_client_api_calls_exist():
    """12) 원격 클라이언트가 부르는 /api/* 는 호스팅 서버에 전부 있어야 한다.
    5) 와 같은 종류의 이음매인데 부르는 쪽만 다르다 — 화면 대신 로컬 CLI 다.
    받는 쪽은 app.routes(정본)를 읽고, 부르는 쪽은 목록을 손으로 적지 않고 소스에서
    훑는다(적는 순간 그게 곧 두 번째 사본이다).
    경로가 하나 어긋나면 클라이언트도 서버도 따로 보면 멀쩡한데, 사용자는
    "원격 서버 오류 404" 한 줄만 보고 무엇이 없는지 영영 모른다.
    부르는 쪽은 remote.py 와 그 api()/fetch() 를 쓰는 진입점들(db sql,
    dashboard --export, doctor)이다 — 그것도 소스에서 훑는다.
    """
    ctx = _load()
    if ctx is None:
        return
    app_routes = _server_paths()
    if app_routes is not None:
        remote_call = re.compile(r'(?:remote\.)?\b(?:api|fetch)\('
                                 r'\s*(?:"(?:GET|POST)"\s*,\s*)?"(/api/[a-z0-9/_-]+)"')
        for p in sorted(SCRIPTS.glob("*.py")) + sorted(SETUP_SCRIPTS.glob("*.py")):
            for call in sorted(set(remote_call.findall(p.read_text("utf-8")))):
                assert call in app_routes, \
                    f"{p.name} 가 원격으로 부르는데 호스팅 서버에 없다: {call}"


def test_seam_13_collectors_use_collector_cli():
    """13) 수집기는 전부 collector.cli(=remote.dispatch 를 통과하는 진입점)를 써야
    한다. 이 줄을 빠뜨린 수집기는 원격 사이트인데도 **조용히 로컬 brain.db 에**
    쓴다 — 화면에도 로그에도 아무것도 안 남고, 사용자는 웹에서 그 단계만
    영영 안 채워지는 걸 본다. 넘기는 이름까지 본다: 파일명과 단계명이 다른
    것들이 있어서(collect_serp→rank, collect_gap→competitors) 짝이 어긋나면
    `/capture rank` 를 쳤는데 서버에서 다른 단계가 돈다. 명부는 run_all.STAGES
    (모듈이 딸린 것) 하나다.
    """
    ctx = _load()
    if ctx is None:
        return
    import run_all
    modular = {s.name: s.module for s in run_all.STAGES if s.module}
    for name, mod in sorted(modular.items()):
        f = Path(mod.__file__)
        got = set(re.findall(r"collector\.cli\(\s*[\"'](\w+)[\"']",
                             f.read_text("utf-8")))
        assert got,             f"{f.name} 이 collector.cli 를 안 부른다 — 원격 사이트인데 로컬 "             f"보관함에 조용히 쓴다: /capture {name}"
        assert got == {name},             f"{f.name} 이 collector.cli 에 넘기는 단계 이름이 자기 단계와 다르다: "             f"{sorted(got)} != {name} — 서버에서 엉뚱한 단계가 돈다"


def test_seam_14_locale_list_single_source():
    """14) 언어-지역 목록은 한 벌이다(serp_adapter.LOCALES). 고르는 자리가 셋이다 —
    로컬 설정 폼(settings.html 의 <select>, 조립이 채운다), 호스팅 등록 화면
    (app.html 이 window.__LOCALES__ 를 읽는다), 호스팅 설정(dash.html 이 /api/settings
    의 locales 를 읽는다). 셋 중 하나라도 사본을 들면 새 언어를 한 곳에만 더하게
    되고, 고를 수 있는데 미국 SERP 로 떨어지는 항목이 생긴다 — 그래서 목록의 모든
    키가 LOCATION_MAP 에 닿는지까지 본다.

    조립본 매니페스트에는 이 목록이 안 실린다. 예전에는 실렸고 여기서 그걸 못
    박았는데, 조립본 안에서 window.__LOCALES__ 를 읽는 자리는 **없었다** — 설정 폼은
    <option> 으로 이미 채워져 오고, 호스팅 설정은 /api/settings 로 받는다. 등록 화면
    (app.html)의 window.__LOCALES__ 는 server/app.py 가 따로 싣는 다른 경로다.
    """
    import dashboard
    import serp_adapter
    codes = [c for c, _ in serp_adapter.LOCALES]
    assert codes and codes[0] == "ko-KR", "기본값(ko-KR)이 첫 항목이어야 폼 기본 선택이 맞다"
    for c in codes:
        assert not serp_adapter.warn_unmapped(c), f"{c} 는 고를 수 있는데 SERP 매핑이 없다"
    settings = (SCRIPTS.parent / "templates" / "views" / "settings.html").read_text("utf-8")
    assert '<select id="p-locale"><!--LOCALE_OPTIONS--></select>' in settings, \
        "설정 폼의 언어-지역이 조립이 채우는 <select> 가 아니다 — 사본이거나 자유 입력이다"
    html = dashboard._assemble("local").decode("utf-8")
    for c in codes:
        assert f'<option value="{c}">' in html, f"조립본 설정 폼에 {c} 가 없다"
    assert "window.__LOCALES__" not in html, \
        "조립본에 읽는 곳 없는 언어-지역 페이로드가 되살아났다 — 실으면 읽는 자리를 만들어라"
    ctx = _load()
    if ctx is None:
        return
    assert "window.__LOCALES__" in (ROOT / "server" / "app.html").read_text("utf-8"), \
        "app.html 이 서버가 실어 보낸 언어-지역 목록을 안 읽는다"
    app_src = ctx["app_f"].read_text("utf-8")
    assert "__LOCALES__=serp_adapter.LOCALES" in app_src, \
        "app.py 가 app.html 에 언어-지역 목록을 안 싣는다"
    assert "serp_adapter.LOCALES" in app_src.split("def api_settings(")[1].split("@app.")[0], \
        "/api/settings 가 언어-지역 목록을 안 준다 — dash.html 의 선택지가 빈다"
    assert "SET_H.locales" in ctx["dash"] and "[data-lang]" in ctx["dash"], \
        "dash.html 이 /api/settings 의 locales 로 선택지를 안 그린다"
    # 칸은 자기 이름으로 집는다 — ".ga4 select" / "[data-lang]". 예전에 저장소 칸이
    # ".repo select" 로 **첫 번째** .repo 를 집어서, 언어 칸이 그 앞에 서면 저장소
    # 저장이 언어 select 를 읽었다(실제로 그렇게 됐다). 저장소 칸은 없어졌고, 그
    # 함정을 다시 파지 않도록 자리로 고르는 셀렉터가 없는 것을 못 박는다.
    sm = (SCRIPTS.parent / "templates" / "sections" / "sm-set.html").read_text("utf-8")
    assert 'class="repo lang' in sm and 'class="repo ga4' in sm, \
        "sm-set.html 의 언어·GA4 칸 이름이 바뀌었다 — dash.html 이 그 이름으로 집는다"
    assert '".repo select"' not in ctx["dash"] and "'.repo select'" not in ctx["dash"], \
        "dash.html 이 칸을 자리(.repo 의 첫째)로 고른다 — 칸 순서가 바뀌면 남의 값을 읽는다"


def test_seam_15_dataforseo_calls_go_through_pacer():
    """15) DataForSEO 로 나가는 요청은 전부 `_dfs_call` 을 지나야 한다.

    Live 엔드포인트는 계정당 분당 12회다. 간격을 지키는 자리는 `_dfs_call` 하나뿐이라,
    새 축이 `requests.post` 를 직접 쓰면 그 경로만 조용히 10배로 던지고 429 를 맞는다
    (그게 ADR 0002 의 `errors=100` 이었다). 자체점검 안의 문자열은 제외한다.
    """
    src = (SCRIPTS / "serp_adapter.py").read_text("utf-8")
    body = src.split("def _selfcheck(")[0]      # 검사 코드의 URL 문자열은 호출이 아니다
    lines = body.splitlines()
    for i, line in enumerate(lines):
        if "https://api.dataforseo.com" not in line:
            continue
        near = "\n".join(lines[max(0, i - 3):i + 1])
        assert "_dfs_call(" in near, \
            f"serp_adapter.py:{i + 1} 의 DataForSEO 호출이 _dfs_call 을 안 지난다 — " \
            f"이 경로만 분당 12회 한도를 안 지킨다:\n{near}"
    # 다른 파일이 DataForSEO 를 직접 부르면 어댑터를 지나지 않은 것이다
    for f in sorted(SCRIPTS.glob("*.py")) + sorted((ROOT / "server").glob("*.py")):
        if f.name in ("serp_adapter.py",) or f.name.startswith("test_"):
            continue
        assert "api.dataforseo.com" not in f.read_text("utf-8"), \
            f"{f.name} 이 DataForSEO 를 직접 부른다 — serp_adapter 를 지나야 간격이 지켜진다"


def test_seam_16_brief_shapes_single_source():
    """16) 요청문의 꼴은 brief.py 가 정본이다(이름·개수는 brief.SHAPE_NAMES — 여기 안 적는다).
    화면은 기회마다 실려 온 o.brief 를 그리고, 기회로 안 올라온 행(뷰의 폴백)만
    askBlock 에 shape 이름을 직접 넘긴다 — 그 이름이 정본에 없으면 머리말도 꼬리도
    빈 요청문이 조용히 나간다. 그리고 옛 틀(규칙 문장·진단별 산출물 사본)이 셸에
    남아 있으면 두 벌이 된다.
    """
    ctx = _load()
    if ctx is None:
        return
    import brief
    shell, tpl = ctx["shell"], ctx["tpl"]
    # 폴백이 넘기는 꼴 이름 전부 — 삼항 안의 것까지 — 이 정본 안에 있다
    used = set()
    for m in re.finditer(r"askBlock\(\{(.*?)\}\)", tpl, re.S):
        body = m.group(1)
        if "brief:" in body:
            continue
        sh = re.search(r"shape:\s*([^,\n]+)", body)
        assert sh, f"askBlock 폴백이 shape 를 안 넘긴다:\n{body[:160]}"
        used |= set(re.findall(r'"(\w+)"', sh.group(1)))
    assert used, "폴백 askBlock 호출을 하나도 못 찾았다 — 정규식이 틀렸거나 호출이 사라졌다"
    assert used <= set(brief.SHAPE_NAMES), \
        f"화면이 넘기는 꼴 이름이 brief.SHAPE_NAMES 에 없다: {used - set(brief.SHAPE_NAMES)}"
    # 셸이 꼬리·머리말을 페이로드(d.brief)에서 받는다 — 옛 틀의 사본이 남아 있지 않다
    assert "window.BRIEF = d.brief" in shell, "셸이 d.brief 를 window.BRIEF 로 안 받는다"
    for stale in ("const DELIVER = {", "확인되지 않은 수치", "위에 없는 것까지 알아서 손대지",
                  "소제목을 답니다", "새로 씁니다.", "수집본에 없습니다."):
        assert stale not in shell, f"옛 요청문 틀이 셸에 남아 있다: {stale!r}"
    # 꼬리가 "임시 폴더에 쓰고 열어라"를 시키면 화면 안내도 파일을 쓸 수 있는 곳을
    # 가리켜야 한다 — 웹 챗은 사용자의 임시 폴더에 못 쓴다. 양쪽 끝이 어긋나면 어느
    # 파일도 혼자서는 안 이상하고, 붙여 넣은 사람만 빈손이 된다. 한쪽만 보면 꼬리를
    # 되돌렸을 때 검사가 조용히 사라지므로 양방향으로 맞춘다.
    wants_file = "%TEMP%" in brief.tails("ko-KR")["fix_page"]
    guides_to_code = "Claude Code 에 붙여 넣습니다" in shell
    assert wants_file == guides_to_code, (
        "꼬리는 파일을 쓰라는데 화면이 딴 데를 가리킨다" if wants_file else
        "화면은 Claude Code 를 가리키는데 꼬리는 파일을 안 시킨다")
    assert not (wants_file and "ChatGPT 에 붙여 넣습니다" in shell),         "꼬리는 파일을 쓰라는데 화면은 ChatGPT 에 붙여 넣으라고 안내한다"
    # 기회 패널의 '고칠 페이지'는 요청문이 고른 페이지(o.brief.page = brief.page_of)다.
    # 화면이 query_pages 로 따로 고르면 순위 밖 지면을 요청문은 "고쳐라", 화면은
    # "걸린 페이지 없음"이라고 한다(써마지에서 실제로 그랬다).
    m = re.search(r"function oppDetail\(o(?:, \w+)?\) \{(.*?)\n\}", shell, re.S)
    assert m, "셸의 oppDetail 을 못 찾았다"
    code = re.sub(r"//[^\n]*", "", m.group(1))     # 주석 속 낱말로 통과하지 않게
    assert "o.brief.page" in code, "oppDetail 이 고칠 페이지를 o.brief.page 에서 안 받는다"
    # 고르지 못했을 때의 후보도 요청문과 같은 목록(o.brief.candidates)을 그린다
    assert "o.brief.candidates" in code, \
        "oppDetail 이 후보 지면을 안 그린다 — 요청문은 후보 N개, 화면은 '걸린 페이지 없음'"
    assert "page" in brief.build({"kind": "aio_exposure", "target": "x"}, {}), \
        "brief.build 가 page 를 안 싣는다 — 화면이 고칠 페이지를 못 받는다"
    # 폴백이 쓰는 키가 shapes_payload 에 다 있다 — 키 하나가 빠지면 undefined 가 글에 박힌다
    keys = set(re.findall(r"\bB\.(\w+)", shell)) | set(re.findall(r"window\.BRIEF\.(\w+)", shell))
    have = set(brief.shapes_payload("ko-KR"))
    assert keys <= have, f"화면이 읽는 BRIEF 키가 페이로드에 없다: {keys - have}"


def test_seam_17_verdict_and_status_single_source():
    """17) 심사·상태 이음매.
    - 판정 값은 db.VERDICTS 한 벌: 화면(triage.html)의 TR_VERDICT 키와 같다.
    - 정규화는 서버의 scoring.norm 하나: 뷰·셸 JS 에 norm 사본(낱자 정규식)이 없고,
      화면은 서버가 준 key 를 그대로 돌려보낸다.
    - 상태 값은 db.OPP_STATUSES 한 벌: 셸의 OPP_SET·OPP_DONE(사람이 누르는 것) 키는
      그것과, OPP_LABEL·OPP_NEXT(그리는 것) 키는 거기에 db.OPP_RESOLVED 를 더한 것과
      양방향으로 같다. resolved 는 서버가 닫는 값이라 누르는 표엔 없고, 그리는 표에
      없으면 화면에 영문 "resolved" 가 날것으로 뜬다.
    - 심사 대상 종류는 scoring.KEYWORD_KINDS ⊂ ALL_KINDS.
    """
    ctx = _load()
    if ctx is None:
        return
    import scoring
    shell, views = ctx["shell"], ctx["views"]
    tr = (views / "triage.html").read_text("utf-8")
    m = re.search(r"const TR_VERDICT = \{(.*?)\}", tr, re.S)
    assert m, "triage.html 의 TR_VERDICT 를 못 찾았다"
    assert set(re.findall(r"(\w+):", m.group(1))) == set(db.VERDICTS), \
        "화면의 판정 값이 db.VERDICTS 와 다르다"
    for src, who in ((tr, "triage.html"), (shell, "dashboard.html")):
        assert "0-9a-z가-힣" not in src, f"{who} 에 norm 사본이 있다 — 정규화는 scoring.norm 하나다"
    drawn = set(db.OPP_STATUSES) | {db.OPP_RESOLVED}
    for name, want in (("OPP_LABEL", drawn), ("OPP_NEXT", drawn),
                       ("OPP_SET", set(db.OPP_STATUSES)), ("OPP_DONE", set(db.OPP_STATUSES))):
        mm = re.search(name + r" = \{(.*?)\};", shell, re.S)
        assert mm, f"셸의 {name} 을 못 찾았다"
        keys = set(re.findall(r"(\w+):", mm.group(1)))
        assert keys == want, f"{name} 의 키가 어긋났다: {sorted(keys ^ want)}"
    assert set(scoring.KEYWORD_KINDS) < set(scoring.ALL_KINDS)


def test_seam_18_run_tool_and_creation_single_source():
    """18) 실행·기록 이음매 — 개발 도구 실행과 작업 기록은 양쪽 끝이 있다.

    [기록 창구]
    기록 창구(`/api/creation`)는 로컬 `dashboard.ROUTES` 와 호스팅 `app.routes` 둘 다에
    있어야 한다. 요청문 꼬리의 기록 명령은 로컬·호스팅 구분 없이 같은 한 줄이라,
    `createdb.py` 가 `remote.owns` 로 갈라 그 창구를 부르지 않으면 호스팅 사이트의
    기록이 이 PC 의 빈 Brain 으로 떨어진다(아무 오류 없이).

    [도구 실행]
    - 도구 목록의 정본은 doctor.TOOLS 한 벌이다: 실행하는 쪽(dashboard.py)도 고르는
      쪽(settings.html)도 말하는 쪽(dashboard.html)도 id·라벨을 직접 적지 않는다.
      적는 순간 표가 두 벌이 되고, 도구가 하나 늘 때 한쪽만 늘어 "고를 수는 있는데
      눌러도 안 열리는" 도구가 생긴다.
    - 화면이 부르는 /api/setup/run-tool 이 로컬 서버에 실제로 있다(LOCAL_PATHS).
      이 경로는 로컬 전용이다 — 호스팅에 두면 서버가 사용자 PC 에서 도구를 띄우는
      척하게 된다.
    - 호스팅 기회 카드는 그래서 안내다: "이 PC 에서 열기" 가 거기 있어야 하고,
      떼어 낸 GitHub 글쓰기(SM.host.write)를 다시 부르지 않는다.
    """
    import dashboard
    ctx = _load()
    if ctx is None:
        return
    assert ("POST", "/api/creation") in dashboard.ROUTES, \
        "로컬 ROUTES 에 /api/creation 이 없다 — 기록 창구는 이 표가 정본이다"
    app_routes = _server_paths()
    assert app_routes is None or "/api/creation" in app_routes, \
        "호스팅 서버에 /api/creation 이 없다 — 웹 사이트의 기록이 갈 곳이 없다"
    create_f = ROOT / "skills" / "create" / "scripts" / "createdb.py"
    if create_f.exists():
        src = create_f.read_text("utf-8")
        assert "remote.owns" in src, \
            "createdb.py 가 원격 판정을 안 한다 — 호스팅 사이트도 로컬 Brain 을 쓴다"
        assert '"/api/creation"' in src, \
            "createdb.py 가 기록 창구를 안 부른다 — done 이 서버에 안 남는다"

    ctx = _load()
    if ctx is None:
        return
    import dashboard
    sys.path.insert(0, str(SETUP_SCRIPTS))
    import doctor
    shell, dash, local_f = ctx["shell"], ctx["dash"], ctx["local_f"]
    local_src = local_f.read_text("utf-8")
    settings = (ctx["views"] / "settings.html").read_text("utf-8")

    # ── 도구 표는 한 벌 ──
    assert "doctor.tool_of" in local_src or "doctor.TOOLS" in local_src, \
        "dashboard.py 가 도구 표를 doctor 에서 안 읽는다 — 사본을 만들었을 것이다"
    ids = "|".join(re.escape(t[0]) for t in doctor.TOOLS)
    lit = re.compile(r"""["'](?:""" + ids + r""")["']""")
    for who, src in (("dashboard.py", local_src), ("settings.html", settings),
                     ("dashboard.html", shell)):
        hit = lit.search(src)
        assert not hit, \
            f"{who} 에 도구 id 가 직접 적혀 있다({hit.group(0)}) — 정본은 doctor.TOOLS 다"
    # 라벨도 마찬가지다: 고르는 칸은 빈 자리로 서고 payload(tools[].label)가 채운다.
    # 여기 <label>·<option> 을 손으로 적어 두면 그게 두 번째 표가 된다.
    for box in ("u-mode", "u-tool", "u-terminal"):
        m = re.search(r'id="' + box + r'"[^>]*>(.*?)</div>', settings, re.S)
        assert m and not m.group(1).strip(), \
            f"settings.html 의 {box} 가 선택지를 직접 적고 있다 — 정본은 doctor 의 표다"

    # ── 실행 경로는 로컬 전용이고 화면이 그걸 부른다 ──
    assert "/api/setup/run-tool" in dashboard.LOCAL_PATHS, \
        "실행 경로가 로컬 서버에 없다 — 화면 버튼이 404 로 죽는다"
    assert "/api/setup/run-tool" in shell, "셸이 실행 경로를 안 부른다"
    assert "/api/setup/run-tool" not in dash, \
        "호스팅 화면이 실행 경로를 부른다 — 브라우저는 이 PC 의 프로세스를 못 띄운다"

    # ── 호스팅 기회 카드는 안내다 ── (서명은 셸의 로컬 기본과 같다: 기회 한 건 o — 29번)
    m = re.search(r"oppBtn\(o\) \{(.*?)\n    \},", dash, re.S)
    assert m, "dash.html 의 SM.host.oppBtn(o) 을 못 찾았다"
    assert "이 PC 에서 열기" in m.group(1), "호스팅 기회 카드에 로컬 실행 안내가 없다"
    assert "SM.host.write" not in m.group(1) and "/api/create" not in m.group(1), \
        "호스팅 기회 카드가 떼어 낸 글쓰기 경로를 아직 부른다"
    # --- 도구 절반은 Task C 가 이어 쓴다 ---


def test_seam_19_brief_context_keys_come_from_gather():
    """19) 요청문이 읽는 페이로드 키는 gather() 가 실제로 싣는 것이어야 한다.

    이 이음매도 양쪽 다 멀쩡해 보인다: brief.py 는 정상적인 dict 조회고 gather 는
    정상적인 dict 다. 어긋나면 그 근거 블록이 **조용히 사라진다** — 표가 없는
    요청문은 여전히 문법에 맞는 요청문이라, 검사에도 화면에도 아무것도 안 남고
    AI 만 근거 없이 답하게 된다.

    실제로 이 리포는 정반대 방향으로 같은 값을 치렀다: 검색결과 상위·중복 제목·
    들어오는 내부 링크는 **수집본에 내내 있었는데** 요청문이 그 키를 안 읽어서,
    요청문이 사람에게 "상위 페이지 제목을 붙여 넣으세요" 라고 시켰다.
    """
    ctx = _load()
    if ctx is None:
        return
    src = (SCRIPTS / "brief.py").read_text("utf-8")
    read = (set(re.findall(r'ctx\.get\("(\w+)"', src))
            | set(re.findall(r'ctx\["(\w+)"\]', src)))
    # attach() 가 자기가 심는 키(brief)는 gather 가 아니라 여기서 난다
    read -= {"brief"}
    assert read, "brief.py 에서 ctx 조회를 하나도 못 찾았다 — 정규식이 틀렸다"

    served = set(_gather("_seam19"))
    assert read <= served, (
        f"요청문이 읽는데 gather() 가 안 싣는 키: {sorted(read - served)}")

    # 크롤 갈래 이름표는 한 벌이다 — 화면 JS 안에 사본을 두면 요청문(서버가
    # 만든다)이 그것을 못 읽어 같은 갈래를 영어 kind 로 사람에게 내보낸다.
    import collect_crawl
    site = (ctx["views"] / "site.html").read_text("utf-8")
    assert "CR_KIND = d.crawl_kinds" in site, (
        "site.html 이 갈래 이름표를 페이로드에서 안 받는다")
    assert "열리지 않는 페이지" not in site, (
        "site.html 에 갈래 이름표 사본이 남아 있다")
    assert set(collect_crawl.ISSUE_KIND) == set(collect_crawl.SEVERITY), (
        set(collect_crawl.ISSUE_KIND) ^ set(collect_crawl.SEVERITY))

    # 근거를 만드는 쪽(수집)과 말하는 쪽(요청문)이 같은 표를 본다 — 이 넷은
    # 값을 치르고 배운 자리라 이름으로 못 박는다.
    # serp_fanout·aio_gap_ranks: 함께 묻는 질문과 AI 요약이 대신 인용한 곳 — 수집기가
    # 받아 놓고 버리던 것을 남기게 된 자리다.
    for key in ("serp_top", "crawl_inlinks", "site_probe", "vitals",
                "serp_fanout", "aio_gap_ranks"):
        assert key in served, f"gather() 가 {key} 를 안 싣는다"
        assert key in src, f"요청문이 {key} 를 안 읽는다 — 수집만 하고 안 쓰는 표가 된다"


def test_seam_20_speed_thresholds_single_source():
    """20) 속도 기준(LCP·INP·CLS)은 scoring 한 벌이다.

    화면이 숫자를 직접 적으면, 구글이 기준을 옮길 때 판정과 화면이 다른 말을 한다
    — 표는 빨갛게 칠하는데 요청문은 "기준 안입니다" 라고 하는 식이다. 화면은
    gather() 가 rules 로 실어 보낸 값만 읽는다.
    """
    ctx = _load()
    if ctx is None:
        return
    import scoring
    site = (ctx["views"] / "site.html").read_text("utf-8")
    assert "R.lcp_good_ms" in site and "R.inp_good_ms" in site and "R.cls_good" in site, (
        "site.html 이 속도 기준을 페이로드에서 안 받는다")
    # 속도 섹션 안에 기준 숫자 리터럴이 없어야 한다
    i = site.index("속도 (Core Web Vitals)")
    j = site.index("let ST_AUDITS", i)
    for lit in ("2500", "2.5", "200ms 이내", "0.1 이내"):
        assert lit not in site[i:j], f"속도 기준 숫자가 화면에 박혀 있다: {lit}"
    # gather() 가 실제로 실어 보내는 값을 본다 — 소스에 그렇게 적혀 있는지가 아니라.
    rules = _gather("_seam20").get("rules") or {}
    for key, const in (("lcp_good_ms", "LCP_GOOD_MS"), ("inp_good_ms", "INP_GOOD_MS"),
                       ("cls_good", "CLS_GOOD")):
        assert rules.get(key) == getattr(scoring, const), (
            f"gather() 의 rules 가 {key} 를 scoring.{const} 로 안 싣는다: {rules.get(key)!r}")
    assert (scoring.LCP_GOOD_MS, scoring.INP_GOOD_MS, scoring.CLS_GOOD) == (2500, 200, 0.1), (
        "구글이 공개한 기준값과 다르다 — 바꿀 이유가 있으면 여기 주석에 적는다")


def test_seam_21_prompt_categories_single_source():
    """21) AI 질문 갈래(추천·비교·문제해결·브랜드·general)는 gen_prompts 한 벌이다.

    세 벌이었고 이미 어긋나 있었다: 화면(dash.html)의 <select> 만 "general" 을
    선택지에 갖고 있었고, 만드는 쪽(gen_prompts.CATEGORIES)은 그 값을 몰랐으며,
    db.py 의 SQL 주석은 세 번째 사본이었다. 사용자는 고를 수 있는데 그 갈래로는
    아무것도 안 만들어지는 값을 보고 있었던 셈이다 — 어느 파일도 혼자서는 안
    이상하고, 화면에도 로그에도 아무것도 안 남는다.

    지금은 조립(dashboard.py._assemble)이 window.__AIQ_CATS__ 로 실어 보내고 화면은
    그걸 그린다 — 단계 용어표(3)·화면 목록(2)과 같은 방식이다. 양방향으로 본다:
    화면이 고를 수 있는 값은 전부 정본이 아는 값이고, 정본의 값은 전부 화면에 뜬다.
    """
    ctx = _load()
    if ctx is None:
        return
    import dashboard
    import gen_prompts
    dash = ctx["dash"]

    # 정본 안에서 앞뒤가 맞는다 — 기본값은 선택지 안에 있고, 만드는 넷은 그 부분집합이다.
    assert gen_prompts.DEFAULT_CATEGORY in gen_prompts.CATEGORY_CHOICES
    assert set(gen_prompts.CATEGORIES) < set(gen_prompts.CATEGORY_CHOICES), \
        "만드는 갈래가 고를 수 있는 갈래의 부분집합이 아니다"

    # 화면은 사본을 안 갖고 페이로드를 읽는다
    assert "window.__AIQ_CATS__" in dash, \
        "dash.html 이 조립이 실어 보낸 질문 갈래표를 안 읽는다"
    assert "var AIQ_CATS = [" not in dash, \
        "dash.html 에 질문 갈래 사본이 되살아났다 — gen_prompts 가 정본이다"

    # 조립이 싣는 것이 정본 그대로다(호스팅 애드온이 뜨는 조립본에서 확인한다)
    html = dashboard._assemble("hosted").decode("utf-8")
    m = re.search(r"window\.__AIQ_CATS__=(\[[^;]*\]);", html)
    assert m, "조립본 매니페스트에 질문 갈래표가 없다"
    assert json.loads(m.group(1)) == list(gen_prompts.CATEGORY_CHOICES), \
        f"조립이 싣는 갈래가 정본과 다르다: {m.group(1)}"

    # db 주석은 사본을 다시 만들지 않는다 — 표는 파이썬 한 곳에만 적힌다
    db_src = (SCRIPTS / "db.py").read_text("utf-8")
    assert "|".join(gen_prompts.CATEGORIES) not in db_src, \
        "db.py 주석에 갈래 사본이 되살아났다 — gen_prompts.CATEGORY_CHOICES 를 가리켜라"

    # 받는 쪽(호스팅 서버)도 같은 표를 본다. 화면이 고를 수 있는 값을 서버가 모르면
    # 저장이 조용히 기본값으로 접힌다 — 고른 사람에게는 아무 말도 안 나간다.
    srv = _server()
    if srv is not None:
        app_src = ctx["app_f"].read_text("utf-8")
        assert "gen_prompts.CATEGORIES" in app_src, \
            "호스팅 서버가 갈래표를 손으로 적는다 — 정본은 gen_prompts 다"
        assert f'"{gen_prompts.DEFAULT_CATEGORY}"' in app_src, \
            "서버가 화면 선택지의 기본 갈래를 모른다 — 고를 수는 있는데 저장이 접힌다"


def test_seam_22_document_escaping_single_source():
    """22) 문서에 값을 박을 때의 이스케이프는 htmlsafe 한 벌이다.

    같은 관용구가 다섯 자리에 손으로 적혀 있었고(`json.dumps(...)` + `.replace("</",
    "<\\/")`), 그중 하나(`<option>` 을 짓는 자리)는 이스케이프가 **아예 없었다**.
    한 자리만 빼먹으면 값에 든 `</script>` 가 거기서 태그를 닫고 그 뒤가 통째로
    마크업이 된다 — 화면은 반쪽만 서고 콘솔에도 검사에도 아무것도 안 남는다.

    소스에 그 관용구가 되살아났는지(사본)와, 조립본이 실제로 적대적인 값을 막는지
    (동작) 둘 다 본다.
    """
    import htmlsafe
    assert "</" not in htmlsafe.js({"a": "</script><script>evil()"})
    assert htmlsafe.attr('"><b>') == "&quot;&gt;&lt;b&gt;"

    # 관용구를 다시 손으로 적으면 그게 곧 여섯 번째 사본이다. 정본(htmlsafe.py)과
    # 이 검사 자신만 그 글자를 갖는다 — 나머지는 전부 htmlsafe.js 를 부른다.
    mine = {"htmlsafe.py", Path(__file__).name}
    for f in sorted(SCRIPTS.glob("*.py")) + sorted((ROOT / "server").glob("*.py")):
        if f.name in mine:
            continue
        assert '.replace("</"' not in f.read_text("utf-8"), \
            f"{f.name} 에 손으로 적은 스크립트 이스케이프가 되살아났다 — 정본은 htmlsafe.js 다"

    # <option> 을 짓는 자리가 값을 그대로 박지 않는다. 출처(LOCALES)를 잠깐 적대적인
    # 것으로 바꿔 조립을 한 번 돌린다 — 지금 출처가 고정 상수라 이 길로만 확인된다.
    import dashboard
    import serp_adapter
    orig = serp_adapter.LOCALES
    try:
        serp_adapter.LOCALES = list(orig) + [('x"><script>evil()</script>', "가</script>나")]
        html = dashboard._assemble("local").decode("utf-8")
    finally:
        serp_adapter.LOCALES = orig
    assert "<script>evil()" not in html and "가</script>나" not in html, \
        "조립이 <option> 값을 이스케이프 없이 박는다 — 목록 출처가 사람 손을 타면 터진다"
    assert "&lt;script&gt;evil()" in html, "적대적인 값이 아예 안 실렸다 — 검사가 헛돈다"


def test_seam_23_opportunity_groups_single_source():
    """23) 기회 묶음 이음매 — 묶는 쪽(scoring.group_opportunities)과 그리는 쪽(셸·개요).
    - 묶은 이유(via)는 scoring.GROUP_VIA 한 벌이다: 셸의 GROUP_WHY 키가 양방향으로 같다.
      서버가 새 열쇠를 만들고 화면이 모르면 펼침 패널이 GROUP_WHY[via] 에서 터진다.
    - 열린 기회는 scoring.OPEN_STATUSES 한 벌이다: 개요의 [아직 안 함](ST_GROUP.open)이
      같은 값이다. 둘 다 "이 상태면"으로 거른다 — done·resolved 는 여기 없다.
    - 개요는 서버가 접은 줄(d.opp_groups)을 그린다 — 화면이 다시 묶지 않는다.
    """
    ctx = _load()
    if ctx is None:
        return
    import scoring
    shell, views = ctx["shell"], ctx["views"]
    m = re.search(r"const GROUP_WHY = \{(.*?)\n\};", shell, re.S)
    assert m, "셸의 GROUP_WHY 를 못 찾았다"
    keys = set(re.findall(r"^\s*(\w+):", m.group(1), re.M))
    assert keys == set(scoring.GROUP_VIA), \
        f"GROUP_WHY 의 키가 scoring.GROUP_VIA 와 어긋났다: {keys ^ set(scoring.GROUP_VIA)}"
    ov = (views / "overview.html").read_text("utf-8")
    mm = re.search(r"const ST_GROUP = \{open:\[(.*?)\]", ov)
    assert mm, "overview.html 의 ST_GROUP.open 을 못 찾았다"
    assert tuple(re.findall(r'"(\w+)"', mm.group(1))) == scoring.OPEN_STATUSES, \
        "개요의 [아직 안 함] 이 scoring.OPEN_STATUSES 와 다르다"
    assert not {"done", "dismissed", "resolved"} & set(scoring.OPEN_STATUSES)
    assert "d.opp_groups" in ov, "개요가 서버가 접은 줄(d.opp_groups)을 안 읽는다"


def test_seam_24_ai_health_fields_come_from_scoring():
    """24) [AI 인용] 화면의 "측정 안 됨·오래됨·구버전·끊긴 확인"은 서버가 센 것 그대로다.

    화면(views/ai.html 의 AI_health)은 d.ai_health 의 하위 칸(h.unmeasured,
    lr.state …)을 읽는다. 최상위 키는 10번이 보지만 그 안의 칸은 아무도 안 본다 —
    scoring.ai_health 가 칸 이름을 바꾸면 화면은 undefined 를 "0개"로 읽어 아무 줄도
    안 그리고, 끊긴 확인이 다시 조용해진다. 그래서 칸 이름과 상태 값을 실물과 대조한다.
    """
    ctx = _load()
    if ctx is None:
        return
    import sqlite3 as _sq

    import collector
    import scoring
    src = (ctx["views"] / "ai.html").read_text("utf-8")
    assert "function AI_health(" in src, "ai.html 에 AI_health 가 없다 — 이 검사가 헛돈다"
    body = src[src.index("function AI_health("):]
    body = body[:body.index("\n}\n")]
    read_h = set(re.findall(r"\bh\.([a-zA-Z_]\w*)", body))
    read_lr = set(re.findall(r"\blr\.([a-zA-Z_]\w*)", body))
    assert read_h and read_lr, "AI_health 가 읽는 칸을 하나도 못 찾았다 — 검사가 헛돈다"
    assert "d.ai_health" in src, "화면이 페이로드의 ai_health 를 안 읽는다"

    # 실물 — 끊긴 회차 하나를 둔 Brain 에서 scoring.ai_health 를 돌린다
    c = _sq.connect(":memory:")
    c.row_factory = _sq.Row
    c.executescript(db.SCHEMA)
    c.execute("INSERT INTO projects(id,name,type,domain) VALUES(1,'_seam','saas','x.com')")
    c.execute("INSERT INTO ai_prompts(project_id,prompt) VALUES(1,'질문 하나')")
    try:
        with db.run(c, 1, "ai"):
            raise collector.Fatal("402")
    except collector.Fatal:
        pass
    h = scoring.ai_health(c, 1)
    c.close()
    assert read_h <= set(h), f"화면이 읽는데 ai_health 에 없는 칸: {sorted(read_h - set(h))}"
    assert read_lr <= set(h["last_run"]), \
        f"화면이 읽는데 last_run 에 없는 칸: {sorted(read_lr - set(h['last_run']))}"
    # 상태 값도 한 벌 — 화면이 견주는 글자는 scoring 이 실제로 내는 값이어야 한다
    made = {scoring._ai_run_state(r) for r in (
        {"finished_at": None, "notes": None},
        {"finished_at": "t", "notes": f"x {scoring.AI_RUN_ABORTED} y"},
        {"finished_at": "t", "notes": "errors=0"})}
    said = set(re.findall(r'lr\.state\s*[!=]==\s*"(\w+)"', body))
    assert said and said <= made, f"화면이 모르는 런 상태를 견준다: {sorted(said - made)}"
    assert h["last_run"]["state"] == "aborted", h["last_run"]


# 차트 라이브러리 — npm 이 준 chart.js 4.5.1 의 dist/chart.umd.min.js 그대로의 해시.
# 판을 올릴 때는 npm 무결성(dist.integrity, sha512)을 대조한 뒤 이 값을 같이 고친다.
CHARTJS_SHA256 = "48444a82d4edcb5bec0f1965faacdde18d9c17db3063d042abada2f705c9f54a"


def test_seam_25_chart_library_single_source():
    """25) 차트 라이브러리는 한 벌이다 — templates/vendor/chart.umd.min.js 하나를 조립이 한 번 박는다.
    - 벤더 파일은 npm 이 준 그대로다(sha256). 누가 손대면 여기서 걸린다.
    - 조립본마다(local·hosted·frozen) 라이브러리가 정확히 한 번 들어간다.
    - new Chart( 는 셸의 chMake 한 곳뿐이다. 뷰·애드온이 차트를 따로 세우면 부수는 쪽
      (관찰자)과 다크 전환이 그 차트를 모른다 — 화면을 다시 그릴 때마다 옛 차트가 샌다.
    """
    import hashlib
    import dashboard
    got = hashlib.sha256(dashboard.VENDOR_JS.read_bytes()).hexdigest()
    assert got == CHARTJS_SHA256, (
        f"vendor/chart.umd.min.js 가 npm 이 준 파일과 다르다: {got[:16]} — "
        "줄끝이 바뀌었으면 .gitattributes 의 vendor -text 를 확인하라")
    for v in ("local", "hosted", "frozen"):
        n = dashboard._assemble(v).decode("utf-8").count("Chart.js v4.5.1")
        assert n == 1, f"{v} 조립본에 라이브러리가 {n}번 들어갔다"
    ctx = _load()
    if ctx is None:
        return

    def code(s):
        return re.sub(r"/\*[\s\S]*?\*/|//[^\n]*", "", s)   # 주석이 "new Chart(" 를 말해도 세지 않는다
    assert code(ctx["shell"]).count("new Chart(") == 1, "셸에서 차트를 세우는 자리가 chMake 하나가 아니다"
    for p in sorted(ctx["views"].glob("*.html")):
        assert "new Chart(" not in code(p.read_text("utf-8")), \
            f"{p.name} 가 차트를 직접 세운다 — window.ch* 헬퍼를 거쳐라"
    assert "new Chart(" not in code(ctx["dash"]), "dash.html 이 차트를 직접 세운다"


def test_seam_26_rank_aio_fields_and_play_come_from_server():
    """26) [순위] 화면의 AI 요약 칸·폴백 처방은 서버가 행에 실은 것 그대로다.

    화면(views/rank.html)은 행의 r.aio_domains(대신 인용된 곳)·r.aio_band(처방 갈래)를
    읽고, 처방 문구는 d.aio_play[갈래] 로 찾는다. 10번은 최상위 키(d.*)만 본다 — 행 칸
    이름이 어긋나거나 갈래 이름이 d.aio_play 의 열쇠와 다르면 화면은 undefined 를 받아
    인용처도 처방도 조용히 안 그린다. 그리고 예전처럼 화면이 AI 요약 처방을 따로 적으면
    (옛 "H2 + 직답") 기회 패널과 두 벌이 된다 — 그 가지에는 한국어 문구가 없어야 한다.
    """
    ctx = _load()
    if ctx is None:
        return
    import sqlite3 as _sq

    import dashboard
    import scoring
    src = (ctx["views"] / "rank.html").read_text("utf-8")
    read_r = set(re.findall(r"\br\.(aio_\w+)", src))
    assert {"aio_domains", "aio_band"} <= read_r, \
        f"rank.html 이 AI 요약 칸을 안 읽는다 — 이 검사가 헛돈다: {sorted(read_r)}"
    assert "RK_AIO_PLAY[r.aio_band]" in src and "d.aio_play" in src, \
        "rank.html 이 서버 처방(d.aio_play)을 갈래로 찾지 않는다"

    # 실물 — AI 요약에 빠진 검색어 둘(1페이지 안·순위 없음)을 둔 Brain
    c = _sq.connect(":memory:")
    c.row_factory = _sq.Row
    c.executescript(db.SCHEMA)
    c.execute("INSERT INTO projects(id,name,type,domain) VALUES(1,'_seam','saas','x.com')")
    for i, pos in ((1, 4), (2, None)):
        c.execute("INSERT INTO keywords(id,project_id,keyword,is_active) VALUES(?,1,?,1)",
                  (i, f"kw{i}"))
        db.write_rank_snapshot(c, i, pos, None, aio_present=1, aio_cited=0,
                               aio_domains=["rival.example"])
    rk = dashboard._axis_rank(c, 1)
    c.close()
    row = rk["ranks"][0]
    assert read_r <= set(row), f"화면이 읽는데 순위 행에 없는 칸: {sorted(read_r - set(row))}"
    bands = {r["aio_band"] for r in rk["ranks"]}
    assert bands == set(scoring.AIO_BANDS), bands
    assert bands <= set(rk["aio_play"]), \
        f"행의 갈래가 d.aio_play 의 열쇠에 없다: {sorted(bands - set(rk['aio_play']))}"

    # 화면의 AI 요약 가지는 서버 문구를 붙이기만 한다 — 자기 문구(한국어 글자)가 없다
    body = src[src.index("function RK_playParts("):]
    body = body[body.index("if (r.aio === 1 && !r.aio_cited)"):body.index("return [what")]
    lits = [s for s in re.findall(r'"([^"]*)"|`([^`]*)`', body) for s in s
            if re.search(r"[가-힣]", s)]
    assert not lits, f"rank.html 이 AI 요약 처방을 따로 적는다(두 벌): {lits}"



def test_seam_27_ai_rivals_single_count():
    """27) "대신 인용된 곳"·엔진별 수·발췌는 scoring.ai_tally 한 벌이다.

    두 벌이었다: 화면·요청문이 읽는 질문 행(dashboard._axis_ai)은
    `MAX(CASE WHEN cited=0 THEN cited_domains_json END)` 로 표본 **하나**(사전순으로 가장
    큰 JSON — 사실상 무작위)를 골랐고, 기회(scoring.ai_gaps)는 전 표본을 셌다. 요청문은
    앞쪽을 읽었다. 어느 쪽도 혼자서는 멀쩡한 SQL 이었다.

    세 끝을 본다 — 만드는 쪽(gather 가 싣는 두 행이 ai_tally 와 같다), 말하는 쪽(요청문이
    그 수를 그대로 쓴다·다시 세지 않는다), 그리는 쪽(화면이 읽는 칸이 행에 있다·옛 칸을
    안 읽는다). 표본은 MAX 로 고르면 틀리는 꼴로 깐다: 빠진 답 셋 중 사전순 최대 JSON 이
    가장 드문 도메인 하나뿐인 답이다.
    """
    ctx = _load()
    if ctx is None:
        return
    import contextlib
    import io as _io
    import sqlite3 as _sq

    import brief
    import dashboard
    import scoring
    c = _sq.connect(":memory:")
    c.row_factory = _sq.Row
    c.executescript(db.SCHEMA)
    c.execute("INSERT INTO projects(id,name,type,domain) VALUES(1,'_seam25','saas','x.com')")
    c.execute("INSERT INTO ai_prompts(id,project_id,prompt,category) VALUES(1,1,'도구 추천','추천')")
    with db.run(c, 1, "ai") as r:
        for i, (eng, doms, ans) in enumerate((
                ("chatgpt", ["reddit.com", "a.com"], "가 먼저 받은 답"),
                ("chatgpt", ["reddit.com"], "나 둘째 답"),
                ("perplexity", ["zzz.com"], "하 셋째 답"))):
            c.execute("INSERT INTO ai_checks(prompt_id,run_id,engine,sample_idx,mentioned,cited,"
                      "cited_domains_json,answer_excerpt,recommended) VALUES(1,?,?,?,0,0,?,?,0)",
                      (r.id, eng, i, json.dumps(doms), ans))
    c.execute("INSERT INTO opportunities(project_id,kind,target,score,status) "
              "VALUES(1,'ai_citation_gap','도구 추천',50,'new')")
    db.set_verdicts(c, 1, [scoring.norm("도구 추천")], "work")
    null = _io.StringIO()
    with contextlib.redirect_stdout(null), contextlib.redirect_stderr(null):
        d = dashboard.gather(c, db.get_project(c, "_seam25"))
    want = scoring.ai_tally(c, r.id)[1]
    c.close()

    # ── 만드는 쪽: 두 행 다 ai_tally 그대로 ──
    row = d["ai_by_prompt"][0]
    gap = (d.get("ai_gap_rows") or [None])[0]
    assert gap, "gather() 가 기회를 세운 행(ai_gap_rows)을 안 싣는다"
    for who, got in (("ai_by_prompt", row), ("ai_gap_rows", gap)):
        for k in ("rivals", "misses", "excerpts", "by_engine", "recommended", "lean"):
            assert got.get(k) == want[k], f"{who}.{k} 가 scoring.ai_tally 와 다르다: {got.get(k)!r}"
    assert want["rivals"][0] == {"domain": "reddit.com", "n": 2, "third_party": True}, want
    assert "miss_domains" not in row and "miss_answer" not in row, "옛 표본 칸이 되살아났다"

    # ── 말하는 쪽: 요청문이 그 수를 그대로 말하고, 다시 세지 않는다 ──
    o = next(x for x in d["opps"] if x["kind"] == "ai_citation_gap")
    body = o["brief"]["body"]
    for x in want["rivals"]:
        assert f"| {x['domain']} | {x['n']}/{want['misses']} |" in body, \
            f"요청문의 대신 인용된 곳이 집계와 다르다:\n{body}"
    assert "가 먼저 받은 답" in body and "나 둘째 답" not in body, "발췌가 결정적이지 않다"
    src = (SCRIPTS / "brief.py").read_text("utf-8")
    ev = src[src.index("def _ev_ai("):src.index("def _ev_aio(")]
    assert "cited_domains" not in ev and "json.loads" not in ev and "miss_" not in ev, \
        "요청문이 대신 인용된 곳을 다시 센다 — 정본은 scoring.ai_tally 다"

    # ── 그리는 쪽: 질문 표가 읽는 칸이 행에 있고, 옛 칸을 안 읽는다 ──
    view = (ctx["views"] / "ai.html").read_text("utf-8")
    i = view.index("let AI_ROWS")
    j = view.index("/* ── 검색 × AI 교차", i)
    part = view[i:j]
    assert "miss_domains" not in view and "miss_answer" not in view, \
        "화면이 옛 표본 칸(miss_*)을 읽는다 — 서버는 더 안 싣는다"
    read = set(re.findall(r"\br\.(\w+)", part))
    assert {"rivals", "excerpts"} <= read, f"화면이 새 칸을 안 읽는다: {sorted(read)}"
    assert read <= set(row), f"화면이 읽는데 질문 행에 없는 칸: {sorted(read - set(row))}"
    eng = next(iter(row["by_engine"].values()))
    assert set(re.findall(r"\bs\.(\w+)", part)) <= set(eng) | set(row), "엔진 몫에 없는 칸을 읽는다"
    riv = set(re.findall(r"\bx\.(\w+)", part))
    assert riv and riv <= set(want["rivals"][0]), f"대신 인용된 곳 칸이 어긋났다: {sorted(riv)}"



def test_seam_28_ai_visits_fields_and_names():
    """28) "AI 에서 온 방문" — 서버가 접은 칸을 화면이 읽고, 요청문이 그 자리를 이름으로 부른다.

    이음매가 둘이다. (가) 화면(views/ai.html 의 AI_visits)은 d.ai_referrals 의 하위 칸
    (r.source·p.sources·m.hosts …)을 읽는다. 최상위 키는 10번이 보지만 안의 칸은 아무도
    안 본다 — dashboard._ai_referrals 가 칸 이름을 바꾸면 화면은 undefined 를 0 으로
    그린다. (나) 요청문(brief._ai_visits)은 "[AI 인용] 화면의 'AI 에서 온 방문'" 처럼 화면·
    섹션 이름을 적는다. 정본은 뷰 쪽(view-def title, 섹션 h2)이라 대조한다.
    """
    ctx = _load()
    if ctx is None:
        return
    import brief
    import dashboard

    src = (ctx["views"] / "ai.html").read_text("utf-8")
    assert "function AI_visits(" in src, "ai.html 에 AI_visits 가 없다 — 이 검사가 헛돈다"
    body = src[src.index("function AI_visits("):]
    body = body[:body.index("\n}\n")]
    read = {v: set(re.findall(rf"\b{v}\.([a-zA-Z_]\w*)", body)) for v in ("r", "p", "m")}
    assert all(read.values()), f"AI_visits 가 읽는 칸을 못 찾았다 — 검사가 헛돈다: {read}"

    # 실물 — 행 하나를 둔 Brain 에서 dashboard._ai_referrals 를 돌린다
    import sqlite3 as _sq
    c = _sq.connect(":memory:")
    c.row_factory = _sq.Row
    c.executescript(db.SCHEMA)
    c.execute("INSERT INTO projects(id,name,type,domain) VALUES(1,'_seam','saas','x.com')")
    db.write_ga4_ai_referrals(c, 1, "2026-01-01", 28, ["chatgpt.com"],
                              [("chatgpt.com", "/a", 3, 1)])
    got = dashboard._ai_referrals(c, 1)
    c.close()
    made = {"r": set(got["ai_referrals"][0]), "p": set(got["ai_referral_pages"][0]),
            "m": set(got["ai_referral_meta"])}
    for v, names in read.items():
        assert names <= made[v], f"화면이 {v}. 로 읽는데 서버가 안 싣는 칸: {sorted(names - made[v])}"

    # (나) 요청문이 부르는 이름 — 화면 제목과 섹션 h2
    defs = _view_defs(ctx["views"])
    for vid, title in brief.SCREEN_TITLES.items():
        assert vid in defs, f"요청문이 없는 화면을 가리킨다: {vid}"
        assert defs[vid]["title"] == title, \
            f"요청문은 [{title}] 라는데 화면 제목은 {defs[vid]['title']!r}"
    sid, stitle = brief.AI_VISITS_SECTION
    assert sid in defs["ai"]["sections"], f"요청문이 가리키는 섹션 {sid} 가 ai view-def 에 없다"
    h2 = re.search(rf'<section id="{sid}">.*?<h2>(.*?)</h2>', src, re.S)
    assert h2 and h2.group(1).strip() == stitle, \
        f"요청문은 '{stitle}' 라는데 섹션 제목은 {h2 and h2.group(1)!r}"


def test_seam_29_ai_screen_gap_count_is_server_judgement():
    """29) [AI 인용] 화면이 "인용이 드문 질문"을 세는 기준은 서버 판정(d.ai_gap_rows —
    scoring.ai_is_gap) 한 벌이다.

    두 벌이었다: 기회는 인용률로 섰는데(6번 중 1번도 공백), 화면 머리 띠는 "인용도
    언급도 없는 질문"만 세어서, 그런 질문만 남으면 기회 목록에 "챗봇 인용 드묾"이 여럿
    떠 있는 채로 "확인한 질문 전부에서 인용되고 있습니다"라고 말했다. 어느 쪽도
    혼자서는 멀쩡한 코드였다.
    """
    ctx = _load()
    if ctx is None:
        return
    src = (ctx["views"] / "ai.html").read_text("utf-8")
    assert "d.ai_gap_rows" in src, "ai.html 이 서버의 공백 판정을 안 읽는다"
    # 띠의 수 = 서버 판정으로 거른 수. 화면이 인용 수로 다시 세지 않는다.
    import re as _re
    m = _re.search(r"const missN = byPrompt\.length\s*\?(.*?);", src, _re.S)
    assert m and "AI_GAPS.has(r.prompt)" in m.group(1), "띠의 공백 수를 화면이 다시 센다"
    # 거르기·칩 수가 같은 판정(AI_stIs)을 쓴다 — 목록과 칩이 다른 말을 하지 않게
    assert "AI_stIs(r, AI_ST)" in src and "AI_stIs(r, s)" in src, "거르기와 칩이 다른 판정을 쓴다"
    # "질문 열기"가 그 판정으로 거른다
    assert 'AI_ST = AI_GAPS ? "gap"' in src, "질문 열기가 옛 눈금(안 잡힘)으로 거른다"
    # 서버 쪽 — gather 가 그 판정 목록을 싣는다
    import dashboard
    assert '"ai_gap_rows": ai_gap_rows' in (SCRIPTS / "dashboard.py").read_text("utf-8")



def test_seam_30_form_controls_have_names():
    """30) 화면의 입력칸은 전부 이름이 있다 — 라벨(for·감싸기)이나 aria-label 로.

    [설정]의 라벨 열일곱이 칸과 안 이어져 있었다(<label>이름</label> 뒤에 칸만 따로).
    눈으로는 멀쩡해 보여서 아무도 몰랐다 — 화면낭독기는 칸 이름을 못 읽고, 라벨을
    눌러도 칸으로 안 간다. 새 칸이 이름 없이 들어오면 여기서 걸린다. 저장·연결 결과를
    적는 메시지 칸(.msg)은 비동기로 바뀌므로 role="status" 로 알린다.
    """
    import glob as _glob
    tdir = ROOT / "skills" / "capture" / "templates"
    files = [tdir / "dashboard.html", *sorted((tdir / "views").glob("*.html")),
             *sorted((tdir / "sections").glob("*.html"))]
    addon = ROOT / "server" / "assets" / "dash.html"
    if addon.exists():
        files.append(addon)
    bad, msgs = [], []
    for f in files:
        src = f.read_text("utf-8")
        # 주석 속 "<select>" 같은 글자는 칸이 아니다
        body = re.sub(r"/\*.*?\*/|<!--.*?-->", "", src, flags=re.S)
        body = re.sub(r"(?m)^\s*//.*$", "", body)
        for m in re.finditer(r"<(input|select|textarea)\b([^<>]*?)>", body, re.S):
            a = m.group(2)
            if re.search(r'type="(hidden|submit|button)"', a):
                continue
            idm = re.search(r'\bid="([^"]+)"', a)
            named = ("aria-label" in a or "aria-labelledby" in a
                     or (idm and re.search(r'<label[^>]*for="%s"' % re.escape(idm.group(1)), body)))
            pre = body[max(0, m.start() - 300):m.start()]
            if not (named or pre.rfind("<label") > pre.rfind("</label>")):
                bad.append(f"{f.name}: <{m.group(1)}{' '.join(a.split())[:60]}>")
        for m in re.finditer(r"<span class=\"msg\"[^>]*>|'<span class=\"msg\"[^']*'", body):
            if "role=" not in m.group(0):
                msgs.append(f"{f.name}: {m.group(0)[:60]}")
    assert not bad, "이름 없는 입력칸:\n  " + "\n  ".join(bad)
    assert not msgs, "role 없는 메시지 칸(비동기 결과를 못 알린다):\n  " + "\n  ".join(msgs)



def test_seam_31_skip_link_leaves_the_hash_alone():
    """31) 본문으로 건너뛰기는 URL hash 를 안 건드린다.

    이 앱은 hash 를 사이트 이름으로 읽는다(5번 — 사이트 목록 링크가 hash 를 싣는다).
    흔한 건너뛰기 링크 모양(href="#content")을 그대로 쓰면 누르는 순간 "content 라는
    사이트"를 열려 한다 — 5번과 같은 이음매를 반대쪽에서 깨는 셈이다. 그래서 포커스만
    옮긴다. 착지점(main)은 포커스를 받을 수 있어야 한다(tabindex="-1").
    """
    ctx = _load()
    if ctx is None:
        return
    shell = ctx["shell"]
    m = re.search(r'<a class="skiplink"([^>]*)>', shell)
    assert m, "건너뛰기 링크가 없다"
    attrs = m.group(1)
    assert not re.search(r'href="#', attrs), "건너뛰기 링크가 hash 를 바꾼다: " + attrs
    assert "preventDefault" in attrs and "focus()" in attrs, attrs
    assert '<main tabindex="-1">' in shell, "건너뛰기 착지점(main)이 포커스를 못 받는다"
    # 첫 탭 순서 — <body> 바로 다음이다(레일보다 앞)
    body = shell[shell.index("<body>"):]
    assert body.index('class="skiplink"') < body.index("<header>"), "건너뛰기가 레일 뒤에 있다"



def test_seam_32_screen_files_are_text():
    """32) 화면 파일에는 NUL 글자가 없다 — git 이 텍스트로 봐야 한다.

    셸(dashboard.html)의 JS 한 줄에 NUL 이 그대로 박혀 있었다(names.join 의 구분자).
    그 한 글자 때문에 git 이 파일 전체를 바이너리로 보고 diff 도 줄끝 변환(autocrlf)도
    껐다. 그래서 윈도에서 고쳐 쓴 셸이 CRLF 째 커밋됐고, 다음 병합에서 파일 **전체가**
    충돌로 잡혔다. 구분자가 필요하면 이스케이프(\\u0000)로 쓴다 — 뜻은 같다.
    벤더 파일은 받은 그대로 둔다(25번이 해시로 지킨다).
    """
    tdir = ROOT / "skills" / "capture" / "templates"
    files = [f for f in tdir.rglob("*.html") if "vendor" not in f.parts]
    addon = ROOT / "server" / "assets"
    if addon.is_dir():
        files += list(addon.glob("*.html")) + list(addon.glob("*.js"))
    bad = [str(f.relative_to(ROOT)) for f in files if b"\x00" in f.read_bytes()]
    assert not bad, f"NUL 이 든 화면 파일(git 이 바이너리로 본다): {bad}"


def test_seam_33_competitor_label_and_reader_single_source():
    """33) 경쟁사 표의 쓰는 쪽과 읽는 쪽.

    2026-09 호스팅: 순위 수집이 검색결과 플랫폼을 'auto_serp' 로 넣었고(291개), 갭 분석은
    그 표를 id 순 앞 5개로, 백링크 교집합은 id 순 20개로 **각자** 읽었다. 백링크 쪽만
    플랫폼을 빼도록 고친 날에도 갭 분석은 계속 m.blog.naver.com 의 키워드를 샀다.

    (가) 'auto_serp' 는 은퇴한 표시이고 db.retire_auto_serp 가 "이 표시가 있으면 걷는다"
        로 한 번만 돈다. 누가 이 표시로 다시 쓰면 매 연결마다 경쟁사가 지워진다 — db.py
        밖에서 이 글자가 나오면 안 된다.
    (나) 돈을 쓰는 두 수집기는 표를 scoring.rivals 로만 읽는다(자체점검 픽스처는 뺀다).
    """
    assert callable(getattr(db, "retire_auto_serp", None)), "은퇴 정리가 없다 — 이 검사가 헛돈다"
    hits = []
    for f in [*SCRIPTS.glob("*.py"), *(ROOT / "server").glob("*.py")]:
        if f.name == "db.py" or f.name.startswith("test_"):
            continue
        if "auto_serp" in f.read_text("utf-8"):
            hits.append(f.name)
    assert not hits, f"은퇴한 경쟁사 표시 'auto_serp' 를 쓰는 곳: {hits} — auto_rank·auto_labs 를 쓴다"

    for name in ("collect_gap.py", "collect_backlinks.py"):
        src = (SCRIPTS / name).read_text("utf-8")
        body = src[:src.index("def _selfcheck(")]
        assert "scoring.rivals(" in body, f"{name} 가 경쟁사를 scoring.rivals 로 안 읽는다"
        raw = re.findall(r"FROM competitors\b[^\"]*", body)
        assert not raw, f"{name} 가 경쟁사 표를 직접 읽는다: {raw} — scoring.rivals 한 벌이다"


def test_seam_34_run_tool_writes_whole_brief_and_acks_the_group():
    """34) 실행 버튼의 두 이음매 — 파일에 쓰는 요청문과 '작업 시작'이 먹는 범위.

    (가) 화면의 복사 버튼(briefText)은 o.brief.body 뒤에 d.brief.tails[shape] 를 잇는다 —
    답의 형식·규칙이 그 꼬리에 있다. run_tool 이 body 만 파일로 쓰면 어느 쪽도 틀린
    데가 없는데 도구는 형식·규칙 없이 시작한다(실제로 그랬다). 파일의 요청문은
    brief.text() 가 내는 글과 같아야 한다.
    (나) [개요]의 묶인 줄은 상태 버튼이 묶인 id 전부에 먹는데(oppIdsTo→setOpps) 열기가
    대표 하나만 바꾸면 새로고침 뒤 한 줄이 둘로 갈라진다. 셸의 SM.host.oppBtn 은 기회
    한 건(o)을 받아 runTool 에 ids 를 싣고, runTool 은 그걸 본문에 싣고, run_tool 은
    그걸 받는다. 호스팅 안내(dash.html)도 같은 서명이다(18번이 본다).
    """
    ctx = _load()
    if ctx is None:
        return
    import inspect
    import os
    import shutil
    import tempfile

    import brief
    import dashboard
    import remote
    import scoring
    sys.path.insert(0, str(SETUP_SCRIPTS))
    import doctor
    shell = ctx["shell"]

    # (나) 화면 → 서버: 기회 한 건을 받고, ids 를 싣고, 서버가 그 키를 읽는다
    assert re.search(r"\n  oppBtn\(o\) \{", shell), "셸의 SM.host.oppBtn 이 기회 한 건(o)을 안 받는다"
    assert "SM.host.oppBtn(o)" in shell, "oppActs 가 SM.host.oppBtn 에 기회가 아니라 다른 것을 넘긴다"
    assert "o.group.ids" in shell[shell.index("oppBtn(o) {"):shell.index("oppBtn(o) {") + 900], \
        "SM.host.oppBtn 이 묶인 줄의 id 를 안 싣는다 — 대표 하나만 작업 시작이 된다"
    m = re.search(r"async function runTool\(id, ids\) \{(.*?)\n\}", shell, re.S)
    assert m and re.search(r"\bids\b", m.group(1)), "runTool 이 ids 를 본문에 안 싣는다"
    src = inspect.getsource(dashboard.run_tool)
    assert 'body.get("ids")' in src, "run_tool 이 ids 를 안 읽는다 — 화면이 보내도 버려진다"

    # (가) 파일 = 본문 + 꼬리. 소스로 먼저: 꼬리(tails)를 안 읽으면 그것부터 틀렸다
    assert '"tails"' in src, "run_tool 이 d.brief.tails 를 안 읽는다 — 파일에 답의 형식·규칙이 없다"

    # 실물 — 페이로드 하나에 기회 한 건을 세우고 dry_run 으로 쓴 파일을 읽는다
    d = _gather("_seam29")
    o = {"id": 1, "kind": "striking_distance", "target": "_seam29", "score": 1.0,
         "status": "new", "reasoning": "r", "band": "page2", "gap_kind": None,
         "play": scoring.kind_play("striking_distance", band="page2")}
    o["brief"] = brief.build(o, d)
    d["opps"], d["brief"] = [o], brief.shapes_payload("ko-KR")
    want = brief.text(o, d, "ko-KR")
    assert "## 답의 형식" in want and "## 규칙" in want, "꼬리의 제목이 바뀌었다 — 검사가 헛돈다"

    home = tempfile.mkdtemp(prefix="seo-miner-seam29-")
    saved_env = {k: os.environ.get(k) for k in ("CAPTURE_HOME", doctor.TOOL_ENV, doctor.TERMINAL_ENV)}
    orig = (shutil.which, remote.owns, dashboard.payload)
    try:
        os.environ["CAPTURE_HOME"] = home
        os.environ[doctor.TOOL_ENV] = doctor.TOOLS[0][0]
        os.environ[doctor.TERMINAL_ENV] = "system"
        shutil.which = lambda c: "/bin/" + c
        remote.owns = lambda project: False
        dashboard.payload = lambda project, at=None: d
        r = dashboard.run_tool({"project": "_seam29", "id": 1, "ids": [1, 999], "dry_run": True})
        assert r["ok"], r
        assert r["ids"] == [1], f"남의 id 가 살아남았다: {r['ids']}"
        md = Path(r["file"]).read_text("utf-8")
        assert md.startswith(want), \
            "파일의 요청문이 brief.text() 와 다르다 — 화면 복사와 도구가 다른 글을 받는다"
        assert "## 답의 형식" in md and "## 규칙" in md, "파일에 답의 형식·규칙 꼬리가 없다"
    finally:
        shutil.which, remote.owns, dashboard.payload = orig
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(home, ignore_errors=True)


# "검색어를 title 에 그대로 박아라"는 지시의 꼴들. 처방·진단·산출물 어디에도 남으면 안 된다 —
# 한 페이지에 검색어 여럿이 걸리면 그 말은 검색어마다 다른 title 을 시키는 말이 된다
# (theotherskin 의 한 지면에 16건이 걸려 있었다). 판정은 scoring.page_advice 가 낱말로
# 대조하고, 처방은 "묶음의 주 의도"를 말한다.
VERBATIM_TITLE_PHRASES = ("검색어를 앞에", "앞쪽에 검색어", "검색어를 그대로 넣", "그대로 넣으세요",
                          "이 검색어로 시작하게", "앞쪽으로 올립", "60자 안에 검색어를")


def test_seam_35_no_prescription_asks_to_paste_the_query_into_title():
    """35) title·H1 처방은 한 목소리다 — 어디서도 검색어를 글자 그대로 박으라고 하지 않는다.

    진단(scoring.page_advice)·처방(scoring._KIND_SPECS 의 play)·산출물(brief.DELIVER_BY_TAG)·
    기회로 안 올라온 행의 폴백(views/rank.html·keywords.html)이 title 을 말하는 네 자리다.
    한쪽만 "낱말로 대조"로 바뀌면 같은 페이지의 요청문 안에서 진단은 "이미 맞다"고 하고
    처방은 "앞에 박아라"고 한다. 여기서 네 자리를 한 번에 본다.
    """
    ctx = _load()
    if ctx is None:
        return
    import brief
    import scoring
    texts = {"brief.DELIVER_BY_TAG": " ".join(brief.DELIVER_BY_TAG.values())}
    for k in scoring.ALL_KINDS:
        spec = scoring._KIND_BY_NAME[k].play
        plays = spec.values() if isinstance(spec, dict) and "what" not in spec else [spec]
        for p in plays:
            texts[f"play:{k}"] = texts.get(f"play:{k}", "") + " ".join(
                [p.get("what") or "", *(p.get("acts") or []), *(p.get("deliver") or [])])
    for v in ("rank.html", "keywords.html"):
        texts[f"views/{v}"] = (ctx["views"] / v).read_text("utf-8")
    # 진단 — 실물 감사 한 장으로 낸 문장까지 본다(page_advice 는 문구를 f-string 으로 만든다)
    adv = scoring.page_advice({"url": "https://x.com/a", "title": "전혀 다른 제목", "h1_json": '["딴 말"]',
                               "meta_description": "설명", "words": 500, "js_shell": 0},
                              ["밀리아 제거 비용"], domain="x.com")
    texts["scoring.page_advice"] = " ".join(f"{a['now']} {a['fix']}" for a in adv)
    bad = [(where, ph) for where, t in texts.items() for ph in VERBATIM_TITLE_PHRASES if ph in t]
    assert not bad, f"검색어를 글자 그대로 박으라는 처방이 남아 있다: {bad}"
    assert any("title" in a["tag"] for a in adv), "진단 픽스처가 title 을 안 잡는다 — 검사가 헛돈다"


def test_seam_36_trend_charts_share_one_threshold():
    """36) 추이선을 몇 회부터 그리는지는 한 벌이다.

    rank.html 은 "2점을 잇는 선은 추세가 아니다"라며 3회를 문턱으로 삼는데,
    ai.html 은 2회부터 그리고 있었다 — 그래서 AI 인용 화면이 0~100% 고정축
    바닥에 평평한 선 하나를 200px 로 그리고 있었다(두 확인일 모두 18.3%).
    한쪽만 고치면 다시 갈린다.
    """
    ctx = _load()
    if ctx is None:
        return
    views = ctx["views"]
    rank = (views / "rank.html").read_text("utf-8")
    ai = (views / "ai.html").read_text("utf-8")
    assert "확인일이 3회 쌓이면" in rank, "rank.html 이 문턱을 더는 말하지 않는다"
    assert "확인일이 3회 쌓이면" in ai, "ai.html 이 rank 와 다른 문턱을 말한다"
    m = re.search(r"if \(at\.length >= (\d+)\) \$\(\"ai-trend\"\)", ai)
    assert m, "ai.html 의 추이 문턱 분기를 못 찾았다"
    assert m.group(1) == "3", f"ai.html 이 {m.group(1)}회부터 그린다 — rank 는 3회다"



def test_seam_37_site_folder_ledger_single_source():
    """37) "사이트 ↔ 폴더"는 장부 한 벌(paths.site_dirs = dirs.json)이다.

    설정 화면의 사이트별 로컬 폴더는 dirs.json 에 쓰는데, "이 폴더가 어느 사이트냐"
    (repo_project)는 `/create profile` 이 남기는 repo.yaml 의 repo_path 를 따로 읽었다.
    그래서 설정에 폴더를 적어 둔 사이트의 리포에서도 doctor 가 "어느 사이트인지
    모릅니다 — /create profile 하세요"를 띄웠다. 쓰는 쪽(설정 화면)과 읽는 쪽(판정)이
    같은 장부를 보는지, 실제로 적고 읽어서 본다.
    """
    import inspect
    import os
    import tempfile
    import dashboard
    import doctor
    import paths
    saved = os.environ.get("CAPTURE_HOME")
    with tempfile.TemporaryDirectory() as d:
        os.environ["CAPTURE_HOME"] = str(Path(d) / "home")
        try:
            repo = Path(d) / "repo"
            (repo / "src").mkdir(parents=True)
            # 쓰는 쪽: 설정 화면의 폴더 표가 부르는 그 함수
            assert dashboard.setup_dir({"project": "alpha", "path": str(repo)})["ok"]
            # 읽는 쪽: 폴더 → 사이트 판정, 사이트 → 폴더(열기 버튼)
            assert stage.pick_project(["alpha", "beta"], cwd=repo / "src") == "alpha", \
                "설정에 적은 폴더를 사이트 판정이 안 읽는다 — 장부가 두 벌이다"
            assert dashboard._work_dir("alpha") == repo, "열기 버튼이 다른 장부를 본다"
        finally:
            os.environ.pop("CAPTURE_HOME", None) if saved is None \
                else os.environ.__setitem__("CAPTURE_HOME", saved)
    # 판정 코드가 옛 장부(repo.yaml 의 repo_path)를 다시 읽기 시작하면 두 벌이다
    src = inspect.getsource(paths.repo_project)
    assert "repo.yaml" not in src and "repo_path" not in src, \
        "repo_project 가 repo.yaml 을 다시 읽는다 — 장부는 dirs.json 한 벌이다"
    # 못 고를 때 시키는 일도 같은 장부로 간다 — 리포 분석(/create profile)이 아니다
    assert "/create profile" not in doctor.PICK_DIR_CMD, doctor.PICK_DIR_CMD
    assert "/create profile" not in inspect.getsource(doctor), \
        "doctor 가 폴더를 붙이는 길로 /create profile 을 안내한다"


def test_seam_38_overview_kind_links_point_at_real_sections():
    """38) [개요] 기회 줄의 "자세히 보기" 링크는 실제로 있는 화면·섹션을 가리킨다.

    개요는 여러 화면을 모아 보는 곳인데, 줄마다 원래 자리로 가는 길이 없어서 사용자가
    "개요의 키워드를 어느 화면에서 보냐"를 따로 물어야 했다. 링크 표의 정본은
    scoring.KINDS 의 see 한 벌이고, 페이로드(kind_views)가 그걸 그대로 싣고, 개요는
    그 표만 읽는다. 뷰 파일에서 섹션 id 가 바뀌거나 화면이 없어지면 링크가 조용히
    화면 맨 위로 떨어진다 — 여기서 잡는다.
    """
    ctx = _load()
    if ctx is None:
        return
    import scoring
    defs = _view_defs(ctx["views"])
    seen = 0
    for k in scoring.KINDS:
        if not k.see:
            continue
        view, sec = k.see
        assert view in defs, f"{k.name}: see 가 없는 화면 {view!r} 을 가리킨다 — 있는 것: {sorted(defs)}"
        body = (ctx["views"] / f"{view}.html").read_text("utf-8")
        assert re.search(r'\bid="' + re.escape(sec) + '"', body), \
            f"{k.name}: {view}.html 에 섹션 id {sec!r} 가 없다 — 링크가 화면 맨 위로 떨어진다"
        seen += 1
    assert seen >= 5, f"see 가 달린 종류가 {seen}개뿐이다 — 검사가 헛돈다"
    d = _gather("seam38")
    assert d["kind_views"] == {k.name: list(k.see) for k in scoring.KINDS if k.see}, \
        "페이로드 kind_views 가 scoring.KINDS 의 see 와 다르다"
    ov = (ctx["views"] / "overview.html").read_text("utf-8")
    assert "window.KIND_VIEWS" in ov, "개요가 서버의 kind_views 표를 안 읽는다"
    # 개요가 화면 id 를 글자로 옮겨 적으면 두 벌이다 — SM.show 는 표에서 온 값으로만 부른다.
    assert not re.search(r"OV_goSee\('[a-z]", ov), "개요가 링크 화면 id 를 손으로 적었다"


def test_seam_39_intent_split_groups_the_page_the_same_way_the_brief_does():
    """39) "이 페이지에 걸린 검색어"를 묶는 규칙은 한 벌이다.

    판정(scoring.intent_split)은 DB 에서, 요청문의 표(brief._page_queries)는 페이로드에서
    같은 물음에 답한다 — "이 검색어의 노출 1등 페이지가 이 페이지인가". 한쪽만 바뀌면
    어느 파일도 혼자서는 안 이상하다: 판정은 "해결 30" 이라 말하는데 요청문 표에는
    그 검색어가 한 줄도 없는 요청문이 나간다.

    그리고 가르기(split_page)와 고치기(fix_page)는 같은 페이지에 정반대를 시킨다.
    "검색어가 몇이든 title 한 벌이 전부를 맡는다"(RULE_ONE_SET)가 가르기 꼬리에 실리면
    요청문 하나가 나누라고 하면서 나누지 말라고 한다.
    """
    import sqlite3 as _sq

    import brief
    import scoring
    c = _sq.connect(":memory:")
    c.row_factory = _sq.Row
    c.executescript(db.SCHEMA)
    c.execute("INSERT INTO projects(id,name,type,domain) VALUES(1,'seam39','saas','e.com')")
    rows = [("syringoma vs milia", "/p/a", 118), ("milia vs syringoma", "/p/a", 39),
            ("milia removal seoul", "/p/a", 25), ("syringoma removal", "/p/a", 5),
            # 같은 검색어가 딴 페이지에도 걸리지만 노출이 적다 — 양쪽 다 /p/a 몫으로 세야 한다
            ("milia removal seoul", "/p/b", 2), ("딴 페이지 검색어", "/p/b", 80)]
    for q, pg, imp in rows:
        c.execute("INSERT INTO gsc_snapshots(project_id,snapshot_date,period_days,query,page,"
                  "clicks,impressions,ctr,position) VALUES(1,'2026-01-01',28,?,?,0,?,0.0,5.0)",
                  (q, pg, imp))
    c.commit()
    out = scoring.intent_split(c, 1)
    assert [r["page"] for r in out] == ["/p/a"], out

    # 요청문이 보는 쪽 — gather 가 싣는 것과 같은 모양의 query_pages
    ctx = {"query_pages": scoring.pages_by_query(c, 1, [q for q, *_ in rows])}
    judged = {q["query"] for q in out[0]["secondary_queries"] + out[0]["primary_queries"]}
    in_brief = {r["query"] for r in brief._page_queries("/p/a", ctx)}
    assert judged == in_brief, (
        f"판정과 요청문이 같은 페이지에 다른 검색어를 묶는다: {judged ^ in_brief}")
    # 의도 이름도 한 벌 — brief 는 scoring 의 것을 다시 내보내기만 한다
    assert brief.query_intent is scoring.query_intent, "의도 분류가 두 벌이다"
    assert brief.INTENT_DEFAULT is scoring.INTENT_DEFAULT

    # 정반대의 처방이 한 요청문에 같이 실리지 않는다
    tails = brief.tails("ko-KR")
    assert brief.RULE_ONE_SET in tails["fix_page"], "고치기에서 '한 벌이 맡는다'가 사라졌다"
    assert brief.RULE_ONE_SET not in tails["split_page"],         "가르기 꼬리에 '한 벌이 전부를 맡는다'가 실렸다 — 나누라면서 나누지 말라고 한다"
    # 가르기는 종류 한 벌짜리 꼴이다 — 딴 종류가 몰래 여기로 오면 대상이 주소가 아닐 수 있다
    split_kinds = {k for k in scoring.ALL_KINDS
                   if brief.shape_of(k, has_page=True) == "split_page"}
    assert split_kinds == {"intent_split"}, split_kinds
    assert split_kinds <= brief.URL_KINDS,         f"가르기 꼴인데 대상이 주소가 아니다 — brief.page_of 가 엉뚱한 페이지를 고른다: {split_kinds - brief.URL_KINDS}"


def test_seam_40_remote_site_opens_the_same_local_dashboard():
    """40) 원격 사이트의 대시보드를 어떻게 띄우는지, 스킬과 코드가 같은 말을 한다.

    이 이음매는 값을 두 번 치렀다. 한 번은 코드에서 — 호스팅 사이트라고 브라우저를
    호스팅 주소로 보냈고, 로컬에만 있는 [설정]·개발 도구 실행 버튼이 통째로 사라졌다.
    한 번은 문서에서 — 고친 뒤 그 사실이 dashboard.py 주석에만 남고 SKILL.md 에는
    안 들어가서, 스킬을 읽은 쪽이 "원격은 띄우는 법이 다른가" 하고 소스를 뒤졌다.
    (SKILL.md 의 원격 절은 "달라지는 것만" 을 세는데, dash 가 그 목록에 없다는 것을
    읽는 쪽은 확언으로 못 읽는다 — 부재는 말이 아니다.)

    그래서 양쪽 끝을 같이 본다: 스킬의 dash 절이 원격을 말하는가, 그리고 로컬
    대시보드가 실제로 원격 사이트의 /api/* 를 서버로 넘기는가.
    """
    ctx = _load()
    if ctx is None:
        return
    skill = (ROOT / "skills" / "capture" / "SKILL.md").read_text("utf-8")
    assert "### /capture dash" in skill, "스킬에서 dash 절을 못 찾았다 — 검사가 헛돈다"
    sec = skill.split("### /capture dash")[1].split(chr(10) + "### ")[0]
    assert "원격" in sec,         "스킬의 dash 절이 원격 사이트를 한 마디도 안 한다 — 읽는 쪽이 소스를 뒤지러 간다"
    assert "remote_project" in sec,         "dash 절이 원격을 말하면서 어디서 갈리는지(remote_project)를 안 가리킨다"
    # 원격 절도 그 자리를 가리킨다 — 사본을 두라는 게 아니라 길을 내라는 것
    rem = skill.split("**원격(호스팅) 사이트**")[1].split(chr(10) + "#")[0]
    assert "/capture dash" in rem,         "원격 절이 dash 를 한 마디도 안 한다 — '달라지는 것만' 목록의 부재를 확언으로 읽게 된다"

    # 코드 쪽 — 스킬이 한 말이 실제로 참인가
    src = ctx["local_f"].read_text("utf-8")
    assert re.search(r"def remote_project\(", src), "dashboard.py 에 remote_project 가 없다"
    assert "remote.owns(project)" in src,         "remote_project 가 remote.owns 로 안 가른다 — 판정이 두 벌이 된다"
    # 읽기(GET)와 쓰기(POST) 둘 다 넘어가야 한다 — 하나만 넘기면 화면이 반만 원격을 보고,
    # 상태를 눌러도 로컬 brain 에 쓴다(그 사이트는 로컬에 없다). 분기가 둘이라 하나만
    # 보면 나머지 하나를 없애도 검사가 통과한다 — 실제로 그렇게 헛돌았다.
    proxied = set(re.findall(r'self\._proxy\("(GET|POST)"', src))
    assert proxied == {"GET", "POST"}, \
        f"원격 사이트의 /api/* 를 다 안 넘긴다({sorted(proxied)}) — 스킬이 거짓말을 한다"
    for guard in (r"u\.path not in NEVER_PROXY and remote_project\(project\)",
                  r"path not in NEVER_PROXY and remote_project\(str\(body"):
        assert re.search(guard, src), f"프록시 분기가 remote_project 로 안 갈린다: {guard}"


def test_seam_41_applied_work_shows_on_the_collapsed_row():
    """41) "열어만 놓음"과 "적용까지 함"이 접힌 목록에서 갈린다.

    열기(run_tool)는 창이 뜨면 묶인 기회 전부를 '작업 시작'으로 찍는다. 도구가 실제로
    파일을 고치고 createdb done 을 부르면 creations 에 기록이 남지만 상태는 그대로
    '작업 시작'이다(완료는 완료 후 관찰을 보고 사람이 누른다 — 그건 맞는 결정이다).
    그래서 접힌 줄에서는 열어만 놓은 기회와 적용까지 끝낸 기회가 **똑같이** 보였다.
    기록은 페이로드(d.creations)에 내내 있었는데 펼쳐야만 보였다.

    배지는 셸이 갖는다(SM 한 벌 — 뷰가 사본을 안 만든다), 개수는 페이로드에서 오고,
    묶인 줄은 묶인 id 전부의 기록을 센다 — 상태 버튼·열기와 같은 범위여야 한 줄이
    새로고침 뒤 둘로 갈라지지 않는다.
    """
    ctx = _load()
    if ctx is None:
        return
    shell, ov = ctx["shell"], (ctx["views"] / "overview.html").read_text("utf-8")
    assert "window.madeBadge" in shell, "셸에 madeBadge 가 없다 — 뷰마다 사본을 만들게 된다"
    assert "window.madeFor" in shell, "기록을 세는 규칙(madeFor)이 셸에 없다"
    body = shell[shell.index("window.madeFor"):shell.index("window.kindTip")]
    assert "window.CREATIONS" in body, "배지가 기록을 페이로드에서 안 받는다"
    assert "group" in body, "배지가 묶인 줄의 id 를 안 센다 — 대표 하나의 기록만 보인다"
    assert "merged" in body, "배지가 머지된 것과 PR 만 열린 것을 안 가른다"
    assert "window.madeBadge(o)" in ov, "개요의 접힌 줄이 배지를 안 그린다"
    # 뷰가 제 손으로 세면 두 벌이다
    assert "CREATIONS" not in ov, "개요가 기록을 직접 센다 — 셸의 배지와 두 벌이 된다"
    # 페이로드가 실제로 그 키를 싣는다(이름만 맞고 비어 있으면 배지가 영영 안 뜬다)
    assert "creations" in _gather("seam41"), "gather() 가 creations 를 안 싣는다"

def test_seam_42_project_types_are_one_list():
    """42) 사이트 종류는 id 도 라벨도 한 벌이다 — 정본은 dashboard.PROJECT_TYPES.

    id 는 다섯 곳에 흩어져 산다: 받는 쪽 검증(dashboard.PROJECT_TYPE_IDS), 점수
    계수(scoring.WEIGHTS), 온보딩 few-shot(_presets.yaml), 그리고 화면 둘 — 로컬
    settings.html 과 호스팅 app.html. 한 곳만 고치면 나머지가 조용히 모른 척한다:
    화면에만 넣은 종류는 서버가 "종류는 …중 하나"로 거부하고, WEIGHTS 에만 빠진
    종류는 거부당하는 대신 saas 계수로 **조용히** 떨어진다(scoring.score 의 폴백
    `WEIGHTS.get(t) or WEIGHTS["saas"]`) — w_fit 0.45 짜리 프리셋이 0.15 로 바뀌어도
    화면 어디에도 안 나온다. local_clinic→local_business 리네임이 딱 그 자리였다.

    라벨도 한 벌이다. 예전엔 두 벌이었고(app.html "SaaS / 웹 서비스" ↔ settings.html
    "saas — 서비스·앱") 같은 값에 두 이름이었다. 이제 둘 다 PROJECT_TYPES 에서 받는다 —
    settings.html 은 조립이 채우는 <!--TYPE_OPTIONS--> 로, app.html 은 server/app.py 가
    싣는 window.__TYPES__ 로(언어-지역이 LOCALES 를 받는 것과 같은 길이다).
    **표기 규칙만 화면마다 다르다**: 설정 폼은 `id — 라벨`(사용자가 그 id 를
    ~/.capture/projects/*.yaml 에 직접 적는다), 등록 화면은 라벨만(id 를 쓸 일이 없다).
    그래서 아래는 두 화면에서 **라벨 문구**가 같은지를 본다 — id 접두는 벗겨 내고 본다.
    """
    import dashboard
    import scoring
    canon = set(dashboard.PROJECT_TYPE_IDS)
    assert len(canon) == len(dashboard.PROJECT_TYPE_IDS), "PROJECT_TYPES 에 같은 종류가 두 번"
    labels = dict(dashboard.PROJECT_TYPES)
    assert set(labels) == canon and all(labels.values()), "PROJECT_TYPES 에 라벨 없는 종류가 있다"

    assert set(scoring.WEIGHTS) == canon, (
        f"점수 계수와 종류 목록이 어긋난다 — 모자란 쪽은 saas 계수로 조용히 떨어진다: "
        f"{set(scoring.WEIGHTS) ^ canon}")

    presets = ROOT / "skills" / "capture" / "projects" / "_presets.yaml"
    keys = set(re.findall(r"^([a-z_]+):", presets.read_text("utf-8"), re.M))
    assert keys == canon, f"온보딩 프리셋과 종류 목록이 어긋난다: {keys ^ canon}"

    # 로컬 설정 폼 — 템플릿은 사본을 안 갖고, 조립본에서 `id — 라벨` 로 채워져 나온다
    sett = ROOT / "skills" / "capture" / "templates" / "views" / "settings.html"
    assert '<select id="p-type"><!--TYPE_OPTIONS--></select>' in sett.read_text("utf-8"), \
        "설정 폼의 종류가 조립이 채우는 <select> 가 아니다 — 사본이거나 자유 입력이다"
    html = dashboard._assemble("local").decode("utf-8")
    form = html.split('<select id="p-type">')[1].split("</select>")[0]
    opts = dict(re.findall(r'<option value="([a-z_]+)">([^<]+)</option>', form))
    assert set(opts) == canon, f"설정 화면 고르개와 종류 목록이 어긋난다: {set(opts) ^ canon}"
    for i, t in opts.items():
        assert t == f"{i} — {labels[i]}", f"설정 화면 라벨이 정본과 다르다: {t!r}"

    # 호스팅 화면 — 리포 밖(플러그인 설치본)에는 server/ 가 없다
    app_html = ROOT / "server" / "app.html"
    if app_html.exists():
        src = app_html.read_text("utf-8")
        assert "window.__TYPES__" in src, \
            "app.html 이 서버가 실어 보낸 종류 목록을 안 읽는다 — 사본이 되살아났다"
        # 폴백 한 줄(서버가 안 실었을 때)까지 정본이어야 한다 — 거기 옛 문구가 남으면
        # 그 화면만 조용히 두 벌로 돌아간다.
        m = re.search(r"window\.__TYPES__ \|\| (\[\[.*?\]\]);", src, re.S)
        assert m, "app.html 의 TYPES 폴백을 못 찾았다 — 꼴이 바뀌었으면 이 검사도 옮긴다"
        for i, t in re.findall(r'\["([a-z_]+)", "([^"]+)"\]', m.group(1)):
            assert i in canon and t == labels[i], f"호스팅 화면 폴백 라벨이 정본과 다르다: {t!r}"
        app_src = (ROOT / "server" / "app.py").read_text("utf-8")
        assert "__TYPES__=dashboard.PROJECT_TYPES" in app_src, \
            "app.py 가 app.html 에 종류 목록을 안 싣는다 — 화면 고르개가 빈다"

    # 옛 이름이 어디에도 안 남았다 — 남으면 그 자리만 saas 계수로 떨어진다
    for p in (presets, sett, app_html):
        if p.exists():
            assert "local_clinic" not in p.read_text("utf-8"), f"{p.name} 에 옛 종류 이름이 남았다"


def test_seam_42_readme_lists_every_command_the_skills_have():
    """42) README 의 명령 표가 스킬에 실제로 있는 명령과 같은 한 벌이어야 한다.

    양쪽 다 혼자서는 멀쩡하다: 스킬에는 절이 있고 README 에는 표가 있다. 어긋나면
    **있는 기능이 없는 것이 된다** — `/capture pages` 가 표에 없어서, 요청문의 '진단'
    절이 비어 나가는데도 사용자가 그걸 채우는 명령을 찾을 길이 없었다(pages·vitals·
    gap 셋이 그렇게 빠져 있었다). 반대로 README 에만 있는 명령은 쳐도 안 도는 명령이다.

    정본은 스킬의 `### /명령` 절이다. README 는 그것을 가리키기만 한다.
    """
    readme = (ROOT / "README.md").read_text("utf-8")
    have = set()
    for f in sorted((ROOT / "skills").glob("*/SKILL.md")):
        body = f.read_text("utf-8")
        have |= {m.group(1) for m in re.finditer(
            r"^### (/(?:capture|create|setup) [a-z]+)", body, re.M)}
        # setup 스킬은 명령마다 절을 두지 않고 본문에서 백틱으로 부른다 — 그 자리가 정본이다.
        have |= set(re.findall(r"`(/setup [a-z]+)`", body))
    assert len(have) >= 15, f"스킬에서 명령을 {len(have)}개밖에 못 찾았다 — 정규식이 틀렸다"
    listed = set(re.findall(r"\| `(/(?:capture|create|setup) [a-z]+)`", readme))
    assert not (have - listed), f"스킬에 있는데 README 표에 없는 명령: {sorted(have - listed)}"
    assert not (listed - have), f"README 표에만 있는 명령: {sorted(listed - have)}"

    # 그림도 실제 명령만 가리킨다 — 그림이 틀린 명령을 치게 만들면 표보다 나쁘다.
    # 첫 블록만 보면 나머지 그림이 마음대로 틀릴 수 있다(실제로 그렇게 헛돌았다).
    blocks = re.findall(r"```mermaid\n(.*?)```", readme, re.S)
    assert len(blocks) >= 3, f"README 의 시작하기 그림이 {len(blocks)}개뿐이다"
    import run_all
    stages = {s.name if hasattr(s, "name") else str(s) for s in run_all.STAGES}
    seen = set()
    for i, b in enumerate(blocks, 1):
        drawn = set(re.findall(r"(/(?:capture|create|setup) [a-z]+)", b))
        assert not (drawn - have), f"그림 {i} 이 스킬에 없는 명령을 가리킨다: {sorted(drawn - have)}"
        seen |= drawn
        # 그림이 단계 이름을 적으면 run_all.STAGES 의 사본이 하나 더 는다. 단계 목록은
        # 아래 명령 표 한 곳에만 적고, 그림은 명령 이름으로만 말한다.
        # (report·index·rank 처럼 명령 이름과 겹치는 낱말은 `/capture ` 뒤에 붙은 것만 빼고 센다)
        bare = re.sub(r"/(?:capture|create|setup) [a-z]+", " ", b)
        drawn_stages = {s for s in stages if re.search(rf"\b{re.escape(s)}\b", bare)}
        assert not drawn_stages, \
            f"그림 {i} 이 단계 이름을 적었다 — 정본은 run_all.STAGES 다: {sorted(drawn_stages)}"
    assert seen, "그림에서 명령을 하나도 못 찾았다 — 정규식이 틀렸다"

    # 단계 이름을 적는 자리는 README 에 딱 하나(`/capture run` 설명)여야 하고,
    # 그 한 벌이 run_all.STAGES 와 같아야 한다. 사본이 낡으면 없는 단계를 안내한다.
    row = re.search(r"\| `/capture run` \|([^|]*)\|", readme)
    assert row, "README 명령 표에서 /capture run 줄을 못 찾았다"
    # 화살표로 이은 한 덩어리만 본다 — 같은 칸의 산문에도 단계 이름이 섞여 있다
    chain = re.search(r"`([^`]*→[^`]*)`", row.group(1))
    assert chain, "/capture run 줄에서 단계 사슬(a → b → …)을 못 찾았다"
    order = [s.name if hasattr(s, "name") else str(s) for s in run_all.STAGES]
    named = [x.strip() for x in chain.group(1).split("→")]
    assert named == order, (
        f"README 의 단계 목록이 run_all.STAGES 와 다르다\n  README: {named}\n  정본  : {order}")


def test_seam_43_chip_stages_have_skill_commands():
    """43) 화면이 칩으로 내놓는 단계는 스킬에 **그 이름의 명령 절**이 있어야 한다.

    칩의 근거는 뷰의 view-def(stages·head)이고 글자는 단계 이름 그대로다
    (`/capture ${st}` — dashboard.html 의 viewChip·act). 그래서 화면 쪽만 보면
    늘 멀쩡하다. 어긋난 건 스킬과의 사이였다:

    [경쟁 분석]의 「경쟁사 찾기」 칩은 `/capture competitors` 를 복사해 줬는데
    SKILL.md 가 정의한 명령은 `/capture gap`(단수)뿐이었다. 붙여 넣으면 안 먹고,
    "오타인가" 하고 `s` 를 붙이면 `/capture gaps` 가 돈다 — 그건 기회를 세우는
    **다른 단계**라 경쟁사 수집은 하나도 안 됐는데 성공한 것처럼 보였다. 조용히
    틀린 답이 나오는 쪽이라 화면 검사로도, 단계 검사로도 안 잡혔다.
    `crawl`·`backlinks` 는 아예 절이 없었다 — 칩은 있는데 그 명령의 설명이
    리포 어디에도 없었다는 뜻이다.

    고치는 방향은 정해져 있다: **명령 이름을 단계 이름에 맞춘다.** 화면에 특례
    표(칩 이름 → 명령 이름)를 두면 그게 곧 세 번째 사본이 된다.

    정본은 양쪽 다 하나씩이다 — 칩 쪽은 view-def, 명령 쪽은 SKILL.md 의 `### /명령`
    절(seam 42 가 README 표를 그 절에 맞춘다). 단계 이름 목록을 여기 옮겨 적지
    않는 이유도 같다.
    """
    ctx = _load()
    if ctx is None:
        return
    defs = _view_defs(ctx["views"])
    chips = _chip_stage_ids(defs)
    assert chips, "화면이 칩으로 내놓는 단계를 하나도 못 읽었다 — view-def 를 잘못 읽고 있다"

    skill_f = ROOT / "skills" / "capture" / "SKILL.md"
    have = set(re.findall(r"^### /capture ([a-z0-9]+)", skill_f.read_text("utf-8"), re.M))
    # 절의 꼴이 바뀌어 하나도 못 읽으면 아래 단언은 "전부 없다"로 요란하게 터진다 —
    # 조용히 통과하지는 않는다. 그때 원인을 단계 탓으로 오해하지 않게 먼저 말해 둔다.
    assert len(have) >= 10, \
        f"SKILL.md 에서 명령 절을 {len(have)}개밖에 못 찾았다 — 정규식이 틀렸다"

    # 어느 화면이 그 칩을 내놓는지까지 말해 준다 — 이름만으로는 고칠 자리를 못 찾는다.
    missing = {}
    for vid, v in sorted(defs.items()):
        for st in sorted(set(list(v["stages"]) + list(v.get("head") or []))):
            if st not in have:
                missing.setdefault(st, []).append(vid)
    assert not missing, (
        "화면이 칩으로 내놓는데 SKILL.md 에 그 명령 절(`### /capture <단계>`)이 없다 "
        "— 사용자가 복사해 붙여도 안 돈다: "
        + " · ".join(f"/capture {st} ([{']·['.join(v)}] 화면)"
                     for st, v in sorted(missing.items()))
        + f". 스킬에 있는 명령: {sorted(have)}. 절 이름을 단계 이름에 맞춰 쓰고"
          " (README 표는 seam 42 가 같이 본다), 화면에 특례 표를 만들지 마라")

# 새 글 꼴의 산출물이 "이미 있는 페이지"를 가리키면 그 요청문은 없는 것을 고치라고 시킨다.
# 아래 두 검사가 그 이음매(꼴 ↔ 처방, 꼴 ↔ 형식)를 양쪽에서 잡는다.
PAGE_PRESUPPOSING = (
    "우리 페이지", "이 페이지로", "이 페이지를 고쳐", "지금 들어오는 링크", "지금 앵커",
    "안 바꾸는 게 답이면", "이 글에 빠진", "우리 글에는 없는", "우리 글에 없는",
)


def _new_content_kinds() -> list[tuple[str, str | None, str | None]]:
    """(kind, gap_kind, band) — 걸린 페이지가 없을 때 '새 글'로 가는 조합 전부.

    정본은 brief.KIND_SHAPE 다. 여기에 목록을 손으로 적으면 그게 사본이 되고, 종류가
    늘 때 이 검사만 옛 목록을 본다.
    """
    import brief
    import scoring
    out = []
    for kind in scoring.ALL_KINDS:
        for gk in (None, "missing", "weak", "own", "sites", "third_party"):
            for band in ((None,) + scoring.AIO_BANDS if kind == "aio_exposure" else (None,)):
                if brief.shape_of(kind, gap_kind=gk, has_page=False,
                                  band=band) == "new_content":
                    out.append((kind, gk, band))
    return out


def test_seam_44_new_content_prescription_never_points_at_a_page():
    """44) 꼴이 '새 글'이면 그 종류의 산출물은 **있는 페이지를 가리키지 않는다**.

    band 와 꼴은 다른 물음이다: band(page1/beyond)는 우리 **순위**를 말하고, 꼴
    (fix_page/new_content)은 손댈 **지면의 유무**를 말한다. aio_exposure 의 beyond
    처방은 band 로만 갈려서, 순위에 걸린 페이지가 없는 요청문에도 "우리 페이지 | 상위
    2~3개 | 차이" 표와 "안 바꾸는 게 답이면"과 "지금 들어오는 링크가 없는 글에서"가
    그대로 나갔다 — 네 산출물 중 셋이 없는 페이지를 가리켰다(theotherskin
    `do papular scars go away`, opp-361). 이제 그 갈래는 deliver_new 가 받는다.

    검사는 **brief 가 실제로 고른 산출물**을 본다(scoring 의 dict 를 직접 읽지 않는다)
    — 고르는 자리가 brief.build 라서, 거기서 안 고르면 scoring 만 고쳐도 소용없다.
    """
    import brief
    import scoring
    combos = _new_content_kinds()
    assert combos, "새 글로 가는 종류가 하나도 없다 — KIND_SHAPE 를 잘못 읽었다"
    for kind, gk, band in combos:
        o = {"kind": kind, "target": "검색어", "gap_kind": gk, "band": band,
             "label": scoring.kind_label(kind), "reasoning": "근거",
             "play": scoring.kind_play(kind, band=band, gap_kind=gk)}
        body = brief.build(o, {}, "ko-KR")["body"]
        want = body.split("## 만들어 줄 것")[1].split("\n## ")[0]
        bad = [w for w in PAGE_PRESUPPOSING if w in want]
        assert not bad, (
            f"{kind}/{gk}/{band}: 새 글 요청문의 산출물이 있는 페이지를 가리킨다 {bad}\n{want}")


def test_seam_45_new_content_form_names_no_artifact_of_its_own():
    """45) 새 글 꼴의 **형식**은 산출물 이름을 새로 부르지 않는다 — 정본은 '만들어 줄 것'이다.

    형식(SHAPES[...]['form'])에 "(직답 블록·구조화 데이터 등)"이라고 예가 박혀 있었다.
    그건 사본이었고, 게다가 **틀린 사본**이었다: 구글 AI 요약 처방은 바로 그 둘을 하지
    말라고 말한다(_AIO_PLAY 의 주석 — 순위가 먼저다). 그래서 한 요청문이 같은 산출물을
    금지하면서 목차에 배정하라고 시켰고, 그 이름은 그 요청문의 '만들어 줄 것'에 있지도
    않았다. 이름을 부르는 곳은 처방 한 곳이다.
    """
    import brief
    import scoring
    # 처방이 하지 말라고 못 박은 것들 — 형식이 이 이름을 부르면 두 말이 된다
    forbidden = ("직답 블록", "구조화 데이터", "JSON-LD", "FAQ 스키마")
    form = " ".join(brief.SHAPES["new_content"]["form"])
    bad = [w for w in forbidden if w in form]
    assert not bad, f"새 글 형식이 산출물 이름을 스스로 부른다(정본은 '만들어 줄 것'): {bad}"

    # 렌더까지 — AI 요약 요청문 안에서 '금지'와 '만들라'가 같이 서지 않는다
    o = {"kind": "aio_exposure", "target": "검색어", "band": "beyond",
         "label": scoring.kind_label("aio_exposure"), "reasoning": "근거",
         "play": scoring.kind_play("aio_exposure", band="beyond")}
    body = brief.build(o, {}, "ko-KR")["body"] + "\n" + brief.tails("ko-KR")["new_content"]
    assert "구조화 데이터로 요약에 끼어드는 길은 없습니다" in body, \
        "AI 요약 처방의 금지 문장이 사라졌다 — 이 검사가 볼 것이 없어졌다"
    for w in forbidden:
        assert w not in body.split("## 만들어 줄 것")[1], \
            f"금지한 산출물({w})을 같은 요청문이 만들라고 시킨다"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)} passed")
