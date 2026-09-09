# 대시보드에서 개발 도구 실행 — 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 기회 카드에서 사용자가 고른 개발 도구(Claude Code·Codex·OpenCode·pi)를 그 사이트 폴더에서 열고, 호스팅 사이트도 로컬 화면에서 같은 결과를 보게 하며, 서버의 GitHub PR 연동을 떼어 낸다.

**Architecture:** 실행은 로컬 대시보드의 `POST /api/setup/run-tool` 이 맡는다(요청문 파일 + 도구 argv + Orca/시스템 터미널). 호스팅 사이트는 로컬 `Handler` 가 `remote.api()` 로 프록시해 같은 화면을 그린다. 작업 기록은 `/api/creation` 한 라우트(로컬 `ROUTES` + 호스팅 래퍼)로 모이고 `createdb.py` 가 원격이면 그걸 부른다. 온보딩은 설정 0단계(`SEOMINER_MODE/TOOL/TERMINAL`, 정본 `doctor.py`).

**Tech Stack:** Python 3.12 stdlib(http.server, subprocess, sqlite3), FastAPI(호스팅), 바닐라 JS 뷰, 리포의 `test_*.py` 자체점검, `run_checks.py`.

**Spec:** `docs/superpowers/specs/2026-09-08-run-tool-from-dashboard-design.md` — 절 번호(§1~§5)는 이 문서를 가리킨다.

## Global Constraints

- 파이썬: `C:\Users\user\AppData\Local\Programs\Python\Python312\python.exe`, `PYTHONIOENCODING=utf-8`. PATH 의 `python` 은 스토어 스텁.
- 전체 검사 `python run_checks.py`. `skills/setup/scripts/doctor.py --selfcheck` 는 이 PC 에서 작업 전부터 실패하던 것(환경 문제)이라 그 1건만 예외. **그 외 새 실패는 만들지 않는다.**
- 파일 인코딩 UTF-8, 줄바꿈 LF. 한글에 `letter-spacing`·`uppercase`·등폭 금지(CLAUDE.md).
- 목록의 정본은 하나: `doctor.MODES/TOOLS/TERMINALS`, `dashboard.ROUTES`, 뷰 `view-def`. 화면·SKILL.md 에 사본을 두지 않는다.
- 새 이음매 검사는 **일부러 깨서 FAIL 을 확인**한 뒤 되돌린다(CLAUDE.md).
- 문구는 사용자 말로 쓴다(내부 id 를 화면에 내지 않는다). 기존 셸 도우미(`$`, `esc`, `post`, `toast`, `table`, `num`)를 쓴다.
- 커밋 꼬리: `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>` + `Claude-Session: https://claude.ai/code/session_0158t5JCke5Azv5syXcboBow`.
- 병렬 작업: Task A 와 D 는 파일이 겹치지 않는다. Task B 와 C 는 `dashboard.py`·`dashboard.html` 을 서로 다른 영역에서 만진다 — **자기 영역 밖은 건드리지 않는다**(아래 "Files" 가 경계).

---

## 파일 지도

| 파일 | A 온보딩 | B 프록시·기록 | C 실행 | D GitHub 제거 |
|---|---|---|---|---|
| `skills/setup/scripts/doctor.py` | MODES/TOOLS/TERMINALS, payload 키 | | | |
| `skills/capture/scripts/paths.py` | `site_dirs`/`set_site_dir` | | | |
| `skills/capture/scripts/dashboard.py` | `KEY_FIELDS` 확장, `/api/setup/remote`·`/api/setup/dirs`·`/api/setup/dir` | `remote_project`, Handler 프록시, `/api/projects` 합치기, `ROUTES["/api/creation"]`, `record_creation_route` | `/api/setup/run-tool`, `run_tool()`, 터미널 실행 | |
| `skills/capture/templates/views/settings.html` | 0단계 섹션 `usage`, 폴더 표 | | | |
| `skills/capture/templates/dashboard.html` | `renderSetup` 의 0단계·폴더 | `setOpp` 몸에 `project` | `SM.host.oppBtn` 로컬 기본(실행 버튼) | |
| `skills/setup/SKILL.md` | 표준 경로 1번 세 질문 | | | |
| `server/app.py` | | `/api/creation` 래퍼 | | GitHub 라우트·import 제거, `/api/settings` 키 |
| `server/assets/dash.html` | | | `SM.host.oppBtn` 안내 | 저장소 칸·`SM.host.write` 제거 |
| `server/writer.py`, `server/gh.py`, `server/settings.py`, `server/store.py` | | | | 삭제·정리 |
| `skills/create/scripts/createdb.py` | | 원격 분기 | | |
| `skills/capture/scripts/test_seams.py` | | 18번(창구 절반) | 18번(도구 절반) | |
| `skills/capture/scripts/test_dashboard.py` | 설정 payload 검사 | 프록시·creation 검사 | run-tool 검사 | |
| `skills/capture/scripts/test_render.py` | `usage` 섹션 | | 도구 없음 배지 | |
| `skills/create/scripts/test_createdb.py` | | 원격 흉내 | | |
| `README.md`, `docs/copy-guide.md` | | | | PR 문장 |

---

### Task A: 온보딩 0단계 — 쓰는 방식·도구·터미널, 로컬 폴더, 호스팅 연결 칸

**Files:**
- Modify: `skills/setup/scripts/doctor.py` (표 셋 + `setup_payload` 키)
- Modify: `skills/capture/scripts/paths.py` (`site_dirs`, `set_site_dir`)
- Modify: `skills/capture/scripts/dashboard.py` — **`KEY_FIELDS`, `save_keys`, `LOCAL_ONLY_PATHS`, `do_GET`/`do_POST` 의 `/api/setup/*` 분기만** (`ROUTES`·`Handler` 의 다른 부분·`run_tool` 은 B·C 영역)
- Modify: `skills/capture/templates/views/settings.html` (0단계 섹션, 폴더 표, 호스팅 연결 칸, 사이트 등록 폼의 폴더 칸)
- Modify: `skills/capture/templates/dashboard.html` — **`renderSetup()` 안과 그 근처의 설정 화면 렌더만**
- Modify: `skills/setup/SKILL.md` (표준 경로 1번)
- Test: `skills/capture/scripts/test_dashboard.py`, `skills/capture/scripts/test_render.py`, `skills/setup/scripts/doctor.py --selfcheck` 의 기존 단언 유지

**Interfaces:**
- Produces: `doctor.MODES`, `doctor.TOOLS`, `doctor.TERMINALS` — 스펙 §1 의 튜플 그대로. `doctor.tool_of(tool_id) -> tuple | None`.
- Produces: `doctor.setup_payload(...)` 응답에 `mode`, `tool`, `terminal`(각 str|None), `tools: [{id, label, installed}]`, `orca_ok: bool`. `orca_ok` 는 `shutil.which("orca")` 가 있고 `orca status --json` 이 0 으로 끝나며 출력에 `"ok": true` 가 있을 때만. 5초 타임아웃, 실패는 False.
- Produces: `paths.site_dirs() -> dict[str, str]`, `paths.set_site_dir(name: str, path: str | None) -> dict` (저장 뒤 전체 반환). 파일 `paths.home()/"dirs.json"`.
- Produces: `dashboard.KEY_FIELDS` 에 `SEOMINER_MODE`, `SEOMINER_TOOL`, `SEOMINER_TERMINAL` 추가(값 검증: `MODES/TOOLS/TERMINALS` 의 id 만, 아니면 400 `{"error": "…"}`).
- Produces: `GET /api/setup/dirs -> {dirs: {...}, worktrees: [path...]}` (worktrees 는 `orca worktree ps --json` 의 `path` 들, orca 없거나 실패면 `[]`), `POST /api/setup/dir {project, path} -> {ok, dirs}` (path 빈 문자열이면 삭제, 폴더가 아니면 400), `POST /api/setup/remote {line} -> {ok, url, projects}` (`line` 에서 `https?://\S+` 와 그 다음 토큰을 뽑아 `remote.link(url, token)`; 못 뽑으면 400). `LOCAL_ONLY_PATHS` 에 셋을 더한다.
- Consumes: 없음(독립).

- [ ] **Step 1: doctor 표와 payload — 실패하는 검사**

`test_dashboard.py` 끝에:

```python
def test_setup_payload_carries_usage_choices():
    import doctor
    ids = [t[0] for t in doctor.TOOLS]
    assert ids == ["claude", "codex", "opencode", "pi"]
    assert [m[0] for m in doctor.MODES] == ["hosted", "local"]
    assert [t[0] for t in doctor.TERMINALS] == ["orca", "system"]
    assert doctor.tool_of("codex")[1] == "Codex" and doctor.tool_of("nope") is None
    os.environ["SEOMINER_TOOL"] = "codex"; os.environ["SEOMINER_MODE"] = "local"
    try:
        p = dashboard.setup_state("")
    finally:
        os.environ.pop("SEOMINER_TOOL"); os.environ.pop("SEOMINER_MODE")
    assert p["tool"] == "codex" and p["mode"] == "local" and p["terminal"] in ("orca", "system")
    assert {t["id"] for t in p["tools"]} == set(ids) and all("installed" in t for t in p["tools"])
    assert isinstance(p["orca_ok"], bool)
```

실행: `python test_dashboard.py` → `AttributeError: TOOLS`.

- [ ] **Step 2: 구현** — `doctor.py` 에 표 셋과 `tool_of`; `diagnose()`/`setup_payload` 가 `os.environ`(= `~/.capture/env` 를 `db.load_env` 로 읽은 뒤)에서 세 값을 읽어 싣는다. `terminal` 이 비면 `"orca" if orca_ok else "system"`. CLI `render()` 맨 아래 "호스팅 연결:" 줄 다음에 `쓰는 방식: {라벨 or 아직 안 고름} · 도구: {라벨(설치됨/없음)} · 터미널: {라벨}` 한 줄. `orca status` 호출은 `subprocess.run([..], capture_output=True, timeout=5)` 로, 예외는 전부 False.

- [ ] **Step 3: 검사 통과 확인** — `python test_dashboard.py`, `python skills/setup/scripts/doctor.py --selfcheck` 가 **작업 전과 같은 자리에서만** 실패하는지(`setup_payload 가 go 를 떨어뜨렸다` 단언). 다른 단언이 새로 깨지면 고친다.

- [ ] **Step 4: 폴더·연결 API — 실패하는 검사**

```python
def test_setup_dirs_and_remote_line():
    import paths
    home = Path(os.environ["CAPTURE_HOME"])
    assert paths.site_dirs() == {}
    d = tempfile.mkdtemp(prefix="seo-miner-dir-")
    assert paths.set_site_dir("mysite", d) == {"mysite": d}
    assert (home / "dirs.json").exists()
    assert paths.set_site_dir("mysite", None) == {}
    r = dashboard.setup_dir({"project": "mysite", "path": str(home / "없는폴더")})
    assert r["ok"] is False
    r = dashboard.setup_dir({"project": "mysite", "path": d})
    assert r["ok"] and r["dirs"] == {"mysite": d}
    got = dashboard.setup_dirs()
    assert got["dirs"] == {"mysite": d} and isinstance(got["worktrees"], list)
    called = {}
    import remote
    orig = remote.link
    remote.link = lambda url, token: called.update(url=url, token=token)
    try:
        r = dashboard.setup_remote({"line": 'python "C:/x/remote.py" connect https://h.example/ abc123'})
        assert r["ok"] and called == {"url": "https://h.example/", "token": "abc123"}, (r, called)
        assert dashboard.setup_remote({"line": "아무 말"})["ok"] is False
    finally:
        remote.link = orig
```

실행 → `AttributeError: site_dirs`.

- [ ] **Step 5: 구현** — `paths.site_dirs/set_site_dir`(JSON, 원자적 쓰기: 임시 파일 뒤 `replace`), `dashboard.setup_dirs()/setup_dir(body)/setup_remote(body)` 와 Handler 분기(`/api/setup/dirs` GET, `/api/setup/dir`·`/api/setup/remote` POST — 실패는 `{"ok": False, "error": …}` 를 400 으로). `setup_remote` 는 `remote.link` 가 예외를 내면 그 문구를 error 로. `LOCAL_ONLY_PATHS` 갱신. `save_keys` 의 값 검증(세 새 키는 표의 id 만 허용).

- [ ] **Step 6: 검사 통과 확인** — `python test_dashboard.py`, `python test_seams.py`(5번이 새 경로를 `LOCAL_PATHS` 에서 찾는다).

- [ ] **Step 7: 화면** — `settings.html`:
  - view-def `sections` 에 `"usage"` 를 `"setup"` 앞에 더한다. 0단계 마크업(`<div class="step" id="usage">`): 라디오 셋(쓰는 방식 / 도구 / 터미널). 항목 라벨은 렌더 시 `d.tools`·서버 payload 에서 채운다(HTML 에 도구 이름을 박지 않는다 — `renderSetup` 이 `d.tools` 로 그린다). 도구마다 `설치됨`/`없음` 배지(`.badge.ok`/`.badge.warn`). 라디오를 바꾸면 곧바로 `post("/api/setup/keys", {SEOMINER_MODE: …})` 로 저장하고 토스트.
  - 호스팅 연결 칸(0단계 안, `mode==="hosted"` 일 때만 보임): 한 줄 입력 + [연결] 버튼 → `post("/api/setup/remote", {line})` → 성공 시 "연결됨: url · 사이트 n개" 와 `loadProjects()` 재호출. 실패는 `.msg` 에.
  - `mode==="hosted"` 면 2·3·4단계 `.step` 을 `hidden`, `local` 이면 보인다. 미선택이면 전부 보인다.
  - "사이트별 로컬 폴더" 표(0단계 아래, `id="dirs"`): `GET /api/setup/dirs` 로 그리고, 사이트 목록은 `$("proj")` 의 option 들(로컬+원격). 입력 칸에 `<datalist>` 로 worktrees. [저장] → `post("/api/setup/dir", …)`.
  - 사이트 등록 폼에 "로컬 폴더" 입력(선택). `create_project` 성공 뒤 값이 있으면 `/api/setup/dir` 로 저장(폼 JS 에서 두 번째 POST).
  - `dashboard.html` 의 `renderSetup(d)` 에 위 렌더를 붙인다(`d.mode/d.tool/d.terminal/d.tools/d.orca_ok`).

- [ ] **Step 8: 렌더 검사** — `test_render.py` 의 `view_sections()` 가 `usage` 섹션 id 를 자동으로 요구한다. `python test_render.py` 통과. `python skills/capture/scripts/dashboard.py --selfcheck` 통과(최상위 이름 충돌 없음).

- [ ] **Step 9: SKILL.md** — `skills/setup/SKILL.md` 표준 경로 1번에 "쓰는 방식·도구·터미널 세 가지를 doctor 의 표에서 읽어 묻고 `~/.capture/env` 에 저장한다(`SEOMINER_MODE/TOOL/TERMINAL`). 호스팅이면 '명령어로 연결하기' 한 줄을 받아 `remote.py connect`, 사이트 등록은 건너뛴다" 를 적는다. 선택지 사본을 적지 않는다.

- [ ] **Step 10: 전체 검사·커밋** — `python run_checks.py`(doctor selfcheck 1건 제외 전부 PASS). 커밋 `feat(setup): 온보딩 0단계 — 쓰는 방식·도구·터미널, 사이트별 로컬 폴더, 호스팅 연결 칸`.

---

### Task B: 프록시와 기록 창구 — 호스팅 사이트를 로컬 화면에, `/api/creation`, `createdb.py` 원격

**Files:**
- Modify: `skills/capture/scripts/dashboard.py` — **`main()` 의 원격 리다이렉트 제거, `remote_project()`, `Handler.do_GET/do_POST` 의 `ROUTES` 디스패치 부분(프록시), `list_projects()`, `ROUTES` 에 `/api/creation`, `record_creation_route()`** (`/api/setup/*` 분기와 `run_tool` 은 A·C 영역)
- Modify: `skills/capture/templates/dashboard.html` — **`setOpp()` 한 함수만** (몸에 `project: $("proj").value`)
- Modify: `server/app.py` — `/api/creation` 래퍼 추가만
- Modify: `skills/create/scripts/createdb.py` (원격 분기)
- Test: `skills/capture/scripts/test_dashboard.py`, `skills/create/scripts/test_createdb.py`, `skills/capture/scripts/test_seams.py`(18번의 창구 절반)

**Interfaces:**
- Produces: `dashboard.remote_project(project: str) -> bool`.
- Produces: 로컬 Handler 가 `ROUTES` 경로에서 `remote_project(project)` 이면 `remote.api(method, path, params=query, json=body)` 결과를 그대로 JSON 으로 낸다(GET 은 `params`, POST 는 `json`). `remote.api` 가 예외를 내면 `{"error": str(e)}` 502.
- Produces: `dashboard.list_projects()` 가 로컬 + `remote.config()["projects"]` 합집합, 정렬.
- Produces: `ROUTES[("POST", "/api/creation")]` = `record_creation_route(body)` → `{"creation_id": int, "status": "acked"}`. body `{project, opportunity_id, path, branch?, note?}`. 그 사이트의 기회가 아니면 `db.ProjectNotFound` 대신 `LookupError("기회를 찾을 수 없습니다")` → Handler/호스팅이 404.
- Produces: 호스팅 `POST /api/creation` (app.py, `/api/opp` 래퍼와 같은 꼴 + `LookupError` → 404).
- Produces: `createdb.py` 의 `pick/claim/done/sync/list` 가 `remote.owns(project)` 이면 서버 API 를 쓴다(§3). `remote` 는 `skills/capture/scripts` 경로에서 import.
- Consumes: 없음(A·C 와 독립). 셸 `setOpp` 의 `project` 는 C 의 실행 버튼도 같은 함수로 쓴다.

- [ ] **Step 1: 프록시 — 실패하는 검사**

```python
def test_local_handler_proxies_remote_sites():
    import remote
    calls = []
    orig_owns, orig_api = remote.owns, remote.api
    remote.owns = lambda p: p == "webonly"
    remote.api = lambda method, path, **kw: calls.append((method, path, kw)) or {"proxied": True}
    try:
        assert dashboard.remote_project("webonly") and not dashboard.remote_project("t")
        # Handler 를 실제로 띄워 부른다 — 프록시는 디스패치 자리에 있다
        srv = _serve()   # test_render.serve 와 같은 꼴: ThreadingHTTPServer(("127.0.0.1", 0), dashboard.Handler)
        try:
            import urllib.request, json as _j
            u = f"http://127.0.0.1:{srv.server_address[1]}/api/data?project=webonly&date=2026-01-01"
            assert _j.loads(urllib.request.urlopen(u).read()) == {"proxied": True}
            assert calls[-1][0] == "GET" and calls[-1][1] == "/api/data" and calls[-1][2]["params"]["project"] == "webonly"
            req = urllib.request.Request(f"http://127.0.0.1:{srv.server_address[1]}/api/opp",
                data=_j.dumps({"project": "webonly", "id": 1, "status": "acked"}).encode(),
                headers={"Content-Type": "application/json", "X-Token": dashboard.TOKEN}, method="POST")
            assert _j.loads(urllib.request.urlopen(req).read()) == {"proxied": True}
            assert calls[-1][0] == "POST" and calls[-1][2]["json"]["id"] == 1
            names = _j.loads(urllib.request.urlopen(f"http://127.0.0.1:{srv.server_address[1]}/api/projects").read())
            assert "webonly" not in names  # config() 를 안 흉내 냈으니 로컬만
        finally:
            srv.shutdown()
    finally:
        remote.owns, remote.api = orig_owns, orig_api
```

`_serve()` 헬퍼는 파일 상단에 만든다(`threading.Thread(target=srv.serve_forever, daemon=True)`). 실행 → `AttributeError: remote_project`.

- [ ] **Step 2: 구현** — `remote_project`, Handler 디스패치에서 `project = query.get("project") or (body or {}).get("project")` 를 먼저 구해 원격이면 프록시. `list_projects()` 합치기. `main()` 의 `if a.project and remote.owns(a.project): … return` 블록 제거(리포트 `--export` 의 원격 분기는 그대로 둔다). `setOpp` 가 `project` 를 싣는다.

- [ ] **Step 3: 통과 확인** — `python test_dashboard.py`.

- [ ] **Step 4: `/api/creation` — 실패하는 검사**

```python
def test_creation_route_records_and_marks_acked():
    conn, pid = _brain("cr")
    db.upsert_opportunities(conn, pid, None, [{"kind": "striking_distance", "target": "q", "score": 10}])
    oid = conn.execute("SELECT id FROM opportunities WHERE project_id=?", (pid,)).fetchone()[0]
    conn.close()
    r = dashboard.ROUTES[("POST", "/api/creation")]("", {}, {"project": "cr", "opportunity_id": oid,
                                                           "path": "content/a.md", "branch": "capture/x-a", "note": "n"})
    assert r["status"] == "acked" and r["creation_id"]
    conn = db.connect()
    assert conn.execute("SELECT status FROM opportunities WHERE id=?", (oid,)).fetchone()[0] == "acked"
    assert conn.execute("SELECT file_path FROM creations WHERE opportunity_id=?", (oid,)).fetchone()[0] == "content/a.md"
    conn.close()
    try:
        dashboard.ROUTES[("POST", "/api/creation")]("", {}, {"project": "cr", "opportunity_id": 999999, "path": "x"})
        assert False
    except LookupError:
        pass
```

- [ ] **Step 5: 구현** — `record_creation_route(body)`: `db.get_project` → `db.get_opportunity(conn, oid, project_id=pid)` 없으면 `LookupError` → `db.record_creation(...)` + `db.set_opportunity_status(conn, oid, "acked", project_id=pid)`. Handler POST 의 `except` 에 `LookupError` → 404 추가. `app.py` 에 래퍼(`store.session(uid, project, isolate=True)`, `LookupError` → 404, `ValueError` → 400).

- [ ] **Step 6: 통과 확인** — `python test_dashboard.py`, `python server/app.py`(demo: `/api/creation` 이 로그인 없이 401 인 단언을 demo 의 401 목록에 더한다), `python test_seams.py`.

- [ ] **Step 7: createdb 원격 — 실패하는 검사** (`test_createdb.py` 끝, 기존 흐름 뒤):

```python
# ── 원격 사이트: Brain 대신 서버 창구를 쓴다 ──
import remote  # noqa: E402  (capture/scripts 는 이미 sys.path 에 있다)
calls = []
remote.owns = lambda p: p == "web"
remote.api = lambda method, path, **kw: (calls.append((method, path, kw)) or
    {"opps": [{"id": 7, "kind": "ctr_gap", "target": "k", "status": "new", "score": 1}],
     "creations": [], "creation_id": 3, "status": "acked", "updated": 1})
out = run("pick", "web"); assert '"id": 7' in out and calls[-1][1] == "/api/data", out
run("claim", "web", "7"); assert calls[-1][1] == "/api/opp" and calls[-1][2]["json"]["status"] == "acked"
run("done", "web", "7", "--path", "a.md", "--branch", "capture/ctr_gap-k")
assert calls[-1][1] == "/api/creation" and calls[-1][2]["json"]["opportunity_id"] == 7, calls[-1]
```

`run()` 은 서브프로세스라 monkeypatch 가 안 닿는다 — 이 블록은 `createdb` 를 **import 해서 직접** 부른다(`createdb.pick("web", None, 10)` 등, stdout 은 `contextlib.redirect_stdout` 으로 받는다). 실행 → 원격 분기가 없어 `ProjectNotFound`.

- [ ] **Step 8: 구현** — `createdb.py` 에 `_remote(project)` 판정과 각 서브명령의 원격 분기(§3 표). `merged` 는 원격이면 `sys.exit("웹에 등록한 사이트의 머지 표시는 대시보드에서 합니다")`.

- [ ] **Step 9: 이음매 18(창구 절반)** — `test_seams.py` 에 `test_seam_18_run_tool_and_creation_single_source()` 를 만들고 이 절반만 넣는다: `("POST", "/api/creation") in dashboard.ROUTES`, `app.py` 소스에 `@app.post("/api/creation")`, `createdb.py` 소스에 `"/api/creation"` 과 `remote.owns`. (도구 절반은 Task C 가 같은 함수에 덧붙인다 — 함수 이름을 바꾸지 않는다.) 일부러 깨기: `createdb.py` 의 경로 문자열을 잠깐 바꿔 FAIL 확인 뒤 되돌린다.

- [ ] **Step 10: 전체 검사·커밋** — `python run_checks.py`. 커밋 `feat(remote): 호스팅 사이트를 로컬 화면에 프록시, /api/creation 기록 창구, createdb 원격 분기`.

---

### Task C: 실행 — `run-tool`, 기회 카드 버튼, 호스팅 안내

**Files:**
- Modify: `skills/capture/scripts/dashboard.py` — **새 함수 `run_tool(body)`·`_tool_argv`·`_open_terminal`·`_work_dir` 와 `do_POST` 의 `/api/setup/run-tool` 분기, `LOCAL_ONLY_PATHS` 에 그 경로 추가만**. (파일 맨 아래 `main()` 위에 새 절로 둔다. `ROUTES`·Handler 디스패치·`/api/setup/keys|dir|dirs|remote` 는 A·B 영역 — 건드리지 않는다.)
- Modify: `skills/capture/templates/dashboard.html` — **`SM.host` 객체(로컬 기본)에 `oppBtn(id)` 추가와 그 버튼이 부르는 `runTool(id)` 함수만**. `oppActs` 는 이미 `SM.host.oppBtn` 을 부른다(1876행 근처) — 손대지 않는다.
- Modify: `server/assets/dash.html` — **`SM.host.oppBtn` 본문만** (§3 안내). `SM.host.write` 와 저장소 칸 제거는 Task D 가 한다 — 여기서는 `oppBtn` 이 더 이상 `write` 를 부르지 않게만 한다.
- Test: `skills/capture/scripts/test_dashboard.py`, `skills/capture/scripts/test_render.py`, `skills/capture/scripts/test_seams.py`(18번의 도구 절반)

**Interfaces:**
- Consumes: `doctor.TOOLS`, `doctor.tool_of` (Task A 가 만든다. **A 가 아직 없으면** `doctor` 에 같은 이름·같은 값으로 임시로 두지 말고 — 이 Task 는 A 뒤에 돈다. 병렬 2차 단계에서 A 는 이미 main 에 있다.) `paths.site_dirs()` (A). `remote_project`/프록시(B): 원격 사이트의 요청문은 `remote.api("GET", "/api/data", params={"project": p})["opps"]` 로 받고, 상태 변경은 `remote.api("POST", "/api/opp", json={...})` 로 보낸다.
- Produces: `dashboard.run_tool(body: dict) -> dict` — 스펙 §2-3 의 응답. `body["dry_run"]` 이면 터미널·상태 변경 없이 `{ok, argv, cwd, file, terminal}`.
- Produces: `POST /api/setup/run-tool` (X-Token). 실패 `{ok:false, error}` 400.
- Produces: 셸 `SM.host.oppBtn(id)` 로컬 기본: 도구가 골라져 있고 설치돼 있으면 `<button class="go sm" onclick="runTool(${id})">${도구 라벨} 로 열기</button>`, 아니면 `<span class="badge warn">도구 없음</span><button class="go sm ghost" onclick="SM.touched=true;SM.show('settings')">설정에서 고르기</button>`. 도구 상태는 `window.SETUP`(= `/api/doctor` 응답, `loadDoctor` 가 세운다 — 없으면 `renderSetup` 이 받는 `d` 를 `window.SETUP = d` 로 남기는 한 줄을 `loadDoctor` 에 더한다)의 `tool`·`tools` 에서 읽는다.
- Produces: 셸 `runTool(id)`: `post("/api/setup/run-tool", {project: $("proj").value, id})` → 성공 토스트 `"<터미널>에서 열었습니다."` (+ fallback 이면 `"Orca 워크트리가 아니라 시스템 터미널로 열었습니다."`) 뒤 `load()`; 실패는 error 토스트.
- Produces: `dash.html` `SM.host.oppBtn(id)` = 안내 배지 + `/capture dash <proj()>` 복사 칩(기존 `SM.host.copy` 를 쓴다).

- [ ] **Step 1: run-tool — 실패하는 검사**

```python
def test_run_tool_builds_command_and_writes_brief():
    import doctor, paths
    conn, pid = _brain("rt")
    db.upsert_opportunities(conn, pid, None, [{"kind": "striking_distance", "target": "q1", "score": 30}])
    db.set_verdicts(conn, pid, [scoring.norm("q1")], "work")
    conn.execute("INSERT INTO gsc_snapshots(project_id,snapshot_date,period_days,query,clicks,impressions,ctr,position)"
                 " VALUES(?,?,28,'q1',1,10,0.1,9.0)", (pid, D)); conn.commit()
    oid = conn.execute("SELECT id FROM opportunities WHERE project_id=?", (pid,)).fetchone()[0]
    conn.close()
    os.environ.pop("SEOMINER_TOOL", None)
    r = dashboard.run_tool({"project": "rt", "id": oid, "dry_run": True})
    assert r["ok"] is False and "도구" in r["error"]
    os.environ["SEOMINER_TOOL"] = "codex"
    try:
        import shutil
        orig = shutil.which
        shutil.which = lambda c: "/bin/codex" if c == "codex" else None
        try:
            r = dashboard.run_tool({"project": "rt", "id": oid, "dry_run": True})
        finally:
            shutil.which = orig
        assert r["ok"], r
        assert r["argv"][0] == "codex" and r["file"].endswith(f"opp-{oid}.md") and r["file"] in r["argv"][-1]
        assert Path(r["cwd"]) == paths.home() / "work" / "rt"          # 폴더 없는 사이트
        body = Path(r["file"]).read_text("utf-8")
        assert "createdb.py" in body and f"done rt {oid}" in body
        d = tempfile.mkdtemp(prefix="seo-miner-rt-")
        paths.set_site_dir("rt", d)
        shutil.which = lambda c: "/bin/codex"
        try:
            r = dashboard.run_tool({"project": "rt", "id": oid, "dry_run": True})
        finally:
            shutil.which = orig
        assert Path(r["cwd"]) == Path(d)
        assert conn_status("rt", oid) == "new"                             # dry_run 은 상태를 안 바꾼다
    finally:
        os.environ.pop("SEOMINER_TOOL", None)
```

`conn_status` 는 파일 상단에 작은 헬퍼로. 실행 → `AttributeError: run_tool`.

- [ ] **Step 2: 구현** — §2-3 그대로. 요청문은 `payload(project)["opps"]` 에서 `id` 로 찾아 `o["brief"]["body"]`(없으면 `o["reasoning"]`). 원격이면 `remote.api("GET", "/api/data", params={"project": project})`. 파일은 `paths.home()/"work"/project/f"opp-{id}.md"`. 꼬리: `\n\n---\n끝나면 이 명령으로 기록해 주세요(바꾼 파일·브랜치를 채워서):\npython "<createdb.py 절대경로>" done <project> <id> --path <바꾼 파일> --branch <브랜치>\n`. argv 는 `doctor.tool_of(tool)[3]` 의 `{prompt}` 자리를 채운다. `_open_terminal(argv, cwd, terminal, title)` — orca 면 `subprocess.run(["orca", "terminal", "create", "--worktree", f"path:{cwd}", "--title", title, "--command", <argv 를 `subprocess.list2cmdline`(Windows)/`shlex.join`(그 외)>, "--focus", "--json"], capture_output=True, timeout=15)` 가 0 이고 출력에 `"ok": true` 면 성공, 아니면 system 으로 물러나며 `fallback="system"`. system 은 OS 별 §2-3. 성공하면 상태 acked(로컬 `db.set_opportunity_status`, 원격 `/api/opp` 프록시).

- [ ] **Step 3: 통과 확인** — `python test_dashboard.py`.

- [ ] **Step 4: 셸 버튼과 호스팅 안내** — Interfaces 대로. `dash.html` 의 `oppBtn` 은 GitHub 분기 없이 안내만.

- [ ] **Step 5: 렌더 검사** — `test_render.py` MUSTS 에 `(r'<span class="badge warn">도구 없음</span>', "도구를 안 고른 상태의 기회 카드에 도구 없음 배지가 없다")` 를 로컬 대시보드 대상에 더한다(픽스처는 `SEOMINER_TOOL` 미설정). 호스팅 조립본 MUSTS(`HOSTED_MUSTS`)에 `(r'이 PC 에서 열기', "호스팅 기회 카드에 로컬 실행 안내가 없다")`. `python test_render.py` 통과. `python run_checks.py --check-dashboard-js` 통과.

- [ ] **Step 6: 이음매 18(도구 절반)** — `test_seam_18_run_tool_and_creation_single_source` 에 덧붙인다: `dashboard.py` 소스가 `doctor.tool_of` 또는 `doctor.TOOLS` 를 쓰고 도구 id 문자열(`"claude"` 등)을 직접 적지 않는다(정규식으로 `"(claude|codex|opencode|pi)"` 리터럴이 `dashboard.py`·`settings.html`·`dashboard.html` 에 없어야 한다 — 라벨은 payload 에서 온다); `/api/setup/run-tool` 이 `dashboard.LOCAL_PATHS` 에 있고 셸이 그 경로를 부른다; `dash.html` 에 `이 PC 에서 열기` 가 있고 `SM.host.write` 호출이 `oppBtn` 안에 없다. 일부러 깨서 FAIL 확인 뒤 되돌린다. (Task B 가 같은 함수를 먼저 만들었으면 그 안에 이어 쓴다. 없으면 만든다 — 이름은 정확히 이것.)

- [ ] **Step 7: 전체 검사·커밋** — `python run_checks.py`. 커밋 `feat(dashboard): 기회 카드에서 개발 도구로 열기 — run-tool, Orca/시스템 터미널, 호스팅 안내`.

---

### Task D: GitHub 연동 제거

**Files:**
- Modify: `server/app.py` (라우트·import·`_create_content`·`/api/settings` 키·demo 단언)
- Delete: `server/writer.py`, `server/gh.py`
- Modify: `server/settings.py` (`GITHUB_*` 셋과 그 자체점검), `server/store.py` (`set_repo`·`set_profile`·`github`·`set_github` 류와 자체점검 — 함수 이름은 grep 으로 확인)
- Modify: `server/assets/dash.html` (저장소 칸 `repoRender/repoPick/repoSave`, `firstTodo` 의 repo 분기, `SM.host.write`, `SET_H.repo*`/`github_*` 참조 전부)
- Modify: `README.md`, `docs/copy-guide.md`, `skills/**/SKILL.md` 에서 "PR 자동 생성"·"콘텐츠 작성 버튼" 문장
- Test: `server/app.py demo()`, `server/store.py`·`server/settings.py` 자체점검, `test_seams.py`

**Interfaces:**
- Produces: 호스팅에 `/auth/github*`·`/api/repos`·`/api/repo`·`/api/create` 가 없다. `/api/settings` 응답에 `repo`·`repo_branch`·`github_connected`·`github_enabled` 가 없다.
- Produces: `dash.html` 에 `github`·`/api/create`·`/api/repo` 문자열이 없다. `SM.host.oppBtn` 은 **비워 두지 말고** Task C 가 채울 때까지 `return ""` 로 둔다(Task C 가 같은 함수를 채운다 — 충돌을 피하려고 이 Task 는 `oppBtn` 의 몸을 `return "";` 한 줄로만 바꾼다).
- Consumes: 없음.

- [ ] **Step 1: 지울 것 지도** — `grep -n "github\|repo\b\|writer\|gh\." server/app.py server/store.py server/settings.py server/assets/dash.html` 로 전부 적는다. `sites.repo*` 열과 `store` 의 열 마이그레이션 코드는 남긴다.

- [ ] **Step 2: demo 단언 먼저** — `server/app.py demo()` 에 `assert c.get("/api/repos").status_code == 404` 와 `assert c.post("/api/create", json={}).status_code == 404`, `assert "github_enabled" not in c.get("/api/settings?project=p1").json()` 를 넣고 `python server/app.py` → FAIL 확인.

- [ ] **Step 3: 제거** — 위 파일들. `import writer`, `gh` 사용, `_create_content`, 라우트 넷, `/api/settings` 키. `store` 함수와 자체점검 줄(612~613행 근처). `settings.py` 의 `GITHUB_*` Setting 셋과 그 단언. `dash.html` 저장소 칸 전부와 `firstTodo` 의 repo 분기(`ga4` 분기만 남긴다), `SM.host.write`, `oppBtn` 은 `return "";`. `writer.py`·`gh.py` 는 `git rm`.

- [ ] **Step 4: 통과 확인** — `python server/app.py`, `python server/store.py`, `python server/settings.py`, `python skills/capture/scripts/test_seams.py`(5·12 번), `python run_checks.py --check-dashboard-js`, `python skills/capture/scripts/test_render.py`(호스팅 조립본 MUSTS 에 저장소 문구가 있으면 그 줄을 지운다).

- [ ] **Step 5: 문서** — README·copy-guide·SKILL 의 PR 자동 생성 문장을 "이 PC 의 개발 도구로 연다"로. `railway.json` 의 `watchPatterns` 는 그대로(경로 삭제는 재배포를 일으켜도 된다).

- [ ] **Step 6: 전체 검사·커밋** — `python run_checks.py`. 커밋 `refactor(server): GitHub PR 연동 제거 — 실행은 PC 의 개발 도구가 맡는다`.

---

## 실행 순서

1. **1차 병렬**: Task A 와 Task D (겹치는 파일 없음). 각자 main 에서 딴 브랜치·워크트리.
2. main 에 합치고 `python run_checks.py`.
3. **2차 병렬**: Task B 와 Task C (`dashboard.py`·`dashboard.html`·`dash.html` 을 서로 다른 영역에서). 각자 새 main 에서 딴 브랜치·워크트리.
4. main 에 합치고(같은 파일의 다른 hunk 는 git 이 합친다 — 충돌 나면 사람이 본다) `python run_checks.py`.
5. 브라우저 확인(§5 마지막 항목), 버전 범프, push.
