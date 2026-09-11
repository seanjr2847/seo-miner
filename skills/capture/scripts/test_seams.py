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
    """16) 요청문의 꼴(고치기·새 글·주소 정리·기술 점검·연락)은 brief.py 가 정본이다.
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
                  "소제목을 답니다"):
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

    # ── 호스팅 기회 카드는 안내다 ──
    m = re.search(r"oppBtn\(id\) \{(.*?)\n    \},", dash, re.S)
    assert m, "dash.html 의 SM.host.oppBtn 을 못 찾았다"
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


def test_seam_25_rank_aio_fields_and_play_come_from_server():
    """25) [순위] 화면의 AI 요약 칸·폴백 처방은 서버가 행에 실은 것 그대로다.

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


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)} passed")
