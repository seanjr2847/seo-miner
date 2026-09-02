#!/usr/bin/env python3
"""화면 쪽 이음매 점검 — 브라우저를 안 띄우고 확인할 수 있는 만큼만.

호스팅판(server/assets/dash.html)은 원본 화면 뒤에 얹히는 애드온이라, 원본이
말없이 바뀌면 조용히 멈춘다. 예전에는 그 이음매가 렌더된 한국어였다 —
버튼 라벨의 정규식으로 단계 id 를, onclick 문자열의 정규식으로 기회 id 를
되찾았다. 지금은 data- 속성과 id 키 조회다. 여기서 그 계약을 지킨다.

리포 밖(플러그인 설치본)에는 server/ 가 없다 — 그때는 각 검사가 조용히 건너뛴다.

원래 stage.py 의 _check_seams() 한 함수 안에 13개 블록(주석 번호 #1~#13)으로
있던 것을 검사 하나 = 함수 하나로 옮겼다. CLAUDE.md 의 규율: 검사를 추가했으면
일부러 깨서 FAIL 이 나는 것까지 확인한다 — 안 그러면 통과하는 검사가 아니라
아무것도 안 보는 검사가 된다.

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

import db     # noqa: E402
import stage  # noqa: E402


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
        assert '<li><a href="/d#' in app_f.read_text("utf-8"), \
            "사이트 목록 링크가 hash 없이 /d 로만 간다 — 무엇을 눌러도 첫 사이트가 열린다"
        assert "location.hash.slice(1)" in shell, \
            "대시보드가 hash 로 사이트를 고르지 않는다 — 링크가 실어 보낸 이름이 버려진다"


def test_seam_05_api_calls_exist_on_servers():
    """5) 화면이 부르는 API 는 그 화면이 뜨는 **모든** 서버에 있어야 한다.
    경로 오타 하나면 fetch 가 조용히 404 로 죽고 화면에는 "불러오지 못했습니다"
    만 남는다 — 화면 파일도 서버 파일도 따로 보면 멀쩡하다. 원본 화면은 로컬과
    호스팅 양쪽에서 뜨므로 둘 다 검사한다(/api/data?date= 를 한쪽에만 넣는 실수).
    /api/setup/* 만 면제한다: 호스팅은 설정 화면을 통째로 숨긴다(dash.html).
    로컬 쪽은 dashboard.ROUTES(+LOCAL_ONLY_PATHS)가 정본이라 소스 정규식 대신
    그 경로 집합을 본다 — Handler 가 실제로 등록하는 것과 같은 자료다.
    """
    import dashboard
    ctx = _load()
    if ctx is None:
        return
    views, shell, dash = ctx["views"], ctx["shell"], ctx["dash"]
    app_f, local_f = ctx["app_f"], ctx["local_f"]
    if app_f.exists() and local_f.exists():
        app_src = app_f.read_text("utf-8")
        app_routes = set(re.findall(r'@app\.(?:get|post)\("([^"]+)"', app_src))
        assert app_routes, "server/app.py 의 라우트를 하나도 못 읽었다 — 표 모양이 바뀌었다"

        def api_calls(src):
            # 끝따옴표를 요구하지 않는다 — "/api/data?project=" + name 형태가 흔하다.
            # 숫자를 받는다 — 안 받으면 /api/ga4/... 를 /api/ga 로 잘라 읽어서,
            # 서버에 라우트를 제대로 만들어 놔도 이 검사가 영영 어긋난다.
            return set(re.findall(r'"(/api/[a-z][a-z0-9/-]*)', src))

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
                        assert call in app_routes, \
                            f"{who} 가 부르는데 호스팅 서버에 없다: {call}"
                    else:
                        assert call in dashboard.LOCAL_PATHS, \
                            f"{who} 가 부르는데 로컬 서버에 없다: {call}"


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


def test_seam_07_acts_click_propagation():
    """7) 원본은 기회 카드의 .acts 에서 클릭 전파를 멈춘다 — 카드가 같이 펼쳐지지
    않게 하려는 것이고, 원본 버튼들은 인라인 onclick 이라 영향이 없다.
    애드온이 그 안에 심는 버튼은 사정이 다르다: document 위임으로 잡으면
    이벤트가 영영 안 닿아 **버튼은 떠 있는데 눌러도 아무 일이 없다**.
    실제로 [콘텐츠 작성]이 그렇게 죽어 있었고, 렌더 검사도 그건 못 잡는다
    (DOM 에는 멀쩡히 있다). 그래서 여기서 계약으로 못 박는다.
    """
    ctx = _load()
    if ctx is None:
        return
    views, dash = ctx["views"], ctx["dash"]
    ov = (views / "overview.html").read_text("utf-8")
    if 'class="acts" onclick="event.stopPropagation()"' in ov:
        assert not re.search(r'document\.addEventListener\(\s*"click"[\s\S]{0,500}?data-write',
                             dash), \
            ".acts 안 버튼을 document 위임으로 잡는다 — 전파가 멈춰 클릭이 안 닿는다"
        assert re.search(r'data-write[\s\S]{0,400}?\.onclick\s*=', dash), \
            "애드온이 .acts 안 버튼에 자기 핸들러를 안 단다 — 눌러도 아무 일이 없다"


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
        for cmd in sorted(set(re.findall(r"/capture ([a-z]+)", p.read_text("utf-8")))):
            if cmd in NON_STAGE or (vid, cmd) in CROSS:
                continue
            assert cmd in declared, (
                f"[{vid}] 화면이 /capture {cmd} 를 부르는데 view-def 의 stages 에 없다 "
                f"— 선언은 {sorted(declared)}. 그 단계가 이 화면을 채우면 stages 에 넣고, "
                f"다른 화면으로 넘기는 손잡이면 CROSS 에 적어라")


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
    import dashboard
    import sqlite3 as _sq, io, contextlib
    _c = _sq.connect(":memory:")
    _c.row_factory = _sq.Row
    _c.executescript(db.SCHEMA)
    _c.execute("INSERT INTO projects(id,name,type,domain) VALUES(1,'_seam','saas','x.com')")
    # GA4 다섯 키(ga4_funnel 등, dashboard.py gather())는 GA4 연결(ga4_snapshots 존재)일
    # 때만 조건부로 실린다 — 안 심으면 그 키를 읽는 화면이 전부 이 검사에서만 걸린다.
    _c.execute("INSERT INTO gsc_snapshots(project_id,snapshot_date,period_days,query,clicks,impressions,ctr,position)"
               " VALUES(1,'2026-01-01',28,'_seam',1,1,1.0,1.0)")
    _c.execute("INSERT INTO ga4_snapshots(project_id,snapshot_date,period_days,landing_page,sessions,sessions_all,key_events)"
               " VALUES(1,'2026-01-01',28,'/',1,1,0)")
    _null = io.StringIO()   # yaml 없는 프로젝트라 경고가 뜬다 — 검사 출력에 섞지 않는다
    with contextlib.redirect_stdout(_null), contextlib.redirect_stderr(_null):
        served = set(dashboard.gather(_c, db.get_project(_c, "_seam")))
    _c.close()
    read = set()
    for p in sorted(views.glob("*.html")):
        read |= {m.group(1) for m in re.finditer(r"\bd\.([a-zA-Z_]\w*)",
                                                p.read_text("utf-8"))}
    assert read <= served, \
        f"화면이 읽는데 gather() 가 안 싣는 페이로드 키: {sorted(read - served)}"


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
    app_f = ctx["app_f"]
    import dashboard
    if app_f.exists():
        app_src = app_f.read_text("utf-8")
        assert "dashboard.carry_read" in app_src, \
            "호스팅이 carry 를 직접 푼다 — 형식의 정본은 dashboard 의 carry_pack/carry_read 다"
        m = re.search(r"CARRY_FIELDS = \(([^)]*)\)", app_src)
        assert m, "app.py 의 CARRY_FIELDS 를 못 찾았다 — 표 모양이 바뀌었다"
        used = set(re.findall(r'"(\w+)"', m.group(1)))
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
    양쪽 다 소스에서 뽑는다(목록을 손으로 적으면 그게 곧 두 번째 사본이다).
    경로가 하나 어긋나면 클라이언트도 서버도 따로 보면 멀쩡한데, 사용자는
    "원격 서버 오류 404" 한 줄만 보고 무엇이 없는지 영영 모른다.
    부르는 쪽은 remote.py 와 그 api()/fetch() 를 쓰는 진입점들(db sql,
    dashboard --export, doctor)이다 — 그것도 소스에서 훑는다.
    """
    ctx = _load()
    if ctx is None:
        return
    app_f = ctx["app_f"]
    if app_f.exists():
        app_routes = set(re.findall(r'@app\.(?:get|post)\("([^"]+)"',
                                    app_f.read_text("utf-8")))
        assert app_routes, "server/app.py 의 라우트를 하나도 못 읽었다 — 표 모양이 바뀌었다"
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


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)} passed")
