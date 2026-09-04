#!/usr/bin/env python3
"""원격 모드 — 호스팅 사이트를 로컬 슬래시 명령으로 그대로 조작한다.

**엔진을 이식하지 않는다.** 서버 워커가 이미 같은 `run_all.run_chain()` +
`run_all.print_summary()` 를 돌린다. 로컬이 하는 일은 런을 쏘고, 서버가 뱉은
자기 내레이션을 받아 **그대로 print** 하는 것뿐이다. 여기서 요약표를 다시 만들면
이 리포가 금지한 "문구 두 벌"이 된다 — 그래서 이 파일에는 표를 그리는 코드가 없다.

판정은 이름 하나다: `--project` 이름이 `remote.json` 의 `projects` 캐시에 있으면
원격, 없으면 로컬. 같은 이름이 로컬 brain.db 에도 있으면 원격이 이긴다 —
사용자가 웹으로 옮긴 뒤 로컬 잔존물이 남은 경우가 흔하기 때문이다. 다만 조용히
이기지는 않는다(stderr 한 줄).

런이 끝나면 서버 보관함을 **로컬 brain.db 에도** 사본으로 남긴다(`pull`). 로컬
셋팅은 로컬에만 쌓이고, 호스팅 셋팅은 서버와 로컬 둘 다에 쌓인다 — 호스팅을
쓴다고 자기 컴퓨터에 아무것도 안 남으면 곤란하다. 사본 갱신이 실패해도 런은
성공이다(측정은 이미 끝났다).

**설정 파일이 없으면 이 모듈은 존재하지 않는 것처럼 조용하다.** 기존 로컬
사용자에게 새 출력이 한 줄도 늘면 안 된다 — `dispatch()` 는 즉시 False 를 주고
로컬 경로는 한 톨도 안 바뀐다.

토큰은 stdout·stderr·예외 메시지 어디에도 안 찍는다. `status` 도 url·사이트
목록·연결 시각만 말한다.

`requests` 는 **함수 안에서 늦게 import** 한다 — doctor.py 가 pip 이전에 이
모듈을 import 하기 때문이다(doctor 는 stdlib 만으로 떠야 한다).

Usage:
  python remote.py connect <url> <token>   # 검증 + 사이트 목록 캐시 + 저장
  python remote.py disconnect
  python remote.py status
  python remote.py sync                    # 사이트 목록 캐시 갱신
  python remote.py pull [project]          # 서버 보관함을 로컬 brain.db 에 사본으로
  python remote.py push <local> [remote]   # 로컬 사이트의 측정치를 서버 사이트에 **더한다**
  python remote.py                         # 인자 없으면 자체점검
"""
import json
import os
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import paths  # noqa: E402

POLL = 1.5          # 폴링 간격(초)
START_WAIT = 90.0   # 서버가 런을 실제로 시작하기까지 기다리는 한도(초)
TIMEOUT = 120       # HTTP 타임아웃(초) — /api/report 가 제일 크다

_warned: set[str] = set()   # 이름 충돌 경고는 프로세스당 사이트별 한 번만


# ── 설정 ────────────────────────────────────────────────────────────────────

def _file() -> Path:
    """remote.json 자리. paths.home() 을 쓴다 — CAPTURE_HOME 을 매번 다시 읽는다."""
    return paths.home() / "remote.json"


def config() -> dict | None:
    """remote.json 을 읽는다. 없거나·깨졌거나·url/token 이 비면 None(= 전부 로컬).

    캐시하지 않는다: 같은 프로세스 안에서 connect 직후 owns() 를 물어보는 경우와
    store.tenant() 가 CAPTURE_HOME 을 갈아끼우는 경우가 둘 다 있다.
    """
    try:
        d = json.loads(_file().read_text("utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(d, dict) or not d.get("url") or not d.get("token"):
        return None
    return d


def owns(project: str) -> bool:
    """이 사이트가 원격인가 — 이름이 projects 캐시에 있으면 그렇다."""
    c = config()
    return bool(c and project and project in (c.get("projects") or []))


def _save(c: dict) -> None:
    f = _file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(c, ensure_ascii=False, indent=2), "utf-8")
    try:                       # 토큰 파일이다 — 남이 못 읽게. Windows 에선 무의미하지만 해롭지도 않다.
        os.chmod(f, 0o600)
    except OSError:
        pass


# ── HTTP ────────────────────────────────────────────────────────────────────

def _request(method: str, url: str, **kw):
    """requests 한 겹 — 자체점검이 이 이름을 갈아끼운다(진짜 네트워크 안 탄다)."""
    import requests      # 늦은 import: doctor 가 pip 이전에 이 모듈을 읽는다
    return requests.request(method, url, timeout=TIMEOUT, **kw)


def _detail(r) -> str:
    """FastAPI 의 {"detail": ...} 를 꺼낸다. 아니면 본문 앞머리."""
    try:
        d = r.json()
        if isinstance(d, dict) and d.get("detail"):
            return str(d["detail"])
    except ValueError:
        pass
    return (r.text or "").strip()[:300]


def _raw(method: str, path: str, *, cfg: dict | None = None, **kw):
    """Bearer 를 붙여 부르고 응답 객체를 준다. 상태코드 해석은 여기 한 곳이다."""
    import requests      # 늦은 import: doctor 가 pip 이전에 이 모듈을 읽는다
    c = cfg or config()
    if not c:
        sys.exit("원격 연결이 없습니다 — python remote.py connect <url> <token>")
    headers = dict(kw.pop("headers", None) or {})
    headers["Authorization"] = f"Bearer {c['token']}"
    try:
        r = _request(method, c["url"].rstrip("/") + path, headers=headers, **kw)
    except requests.RequestException as e:
        # 예외 문자열에 토큰이 실릴 일은 없다(헤더는 안 찍힌다) — 그래도 종류만 말한다.
        sys.exit(f"원격 서버에 닿지 못했습니다 ({type(e).__name__}) — 주소를 확인하세요: {c['url']}")
    if r.status_code == 401:
        sys.exit("원격 토큰이 만료됐거나 무효입니다 — 웹 [설정] > '명령어로 연결하기'에서 "
                 "새 토큰을 받아 python remote.py connect <url> <새토큰> 을 다시 실행하세요.")
    if r.status_code >= 400:
        # 400 은 대개 "모르는 opt" 다 — 사유를 그대로 옮긴다. 조용히 무시하면
        # --device mobile 을 준 사용자가 데스크톱 결과를 모바일로 읽는다.
        sys.exit(f"원격 서버 오류 {r.status_code}: {_detail(r)}")
    return r


def api(method: str, path: str, **kw):
    """JSON 을 돌려주는 라우트용. /api/projects 처럼 list 를 주는 곳도 있다."""
    return _raw(method, path, **kw).json()


def fetch(path: str, **kw) -> bytes:
    """JSON 이 아닌 본문(리포트 HTML)용. api() 와 같은 인증·오류 해석을 쓴다."""
    return _raw("GET", path, **kw).content


# ── 런 ──────────────────────────────────────────────────────────────────────

def run(project: str, stages, opts: dict) -> int:
    """POST /api/run → 로그를 흘려받아 print → 끝나면 로컬 사본을 갱신한다.

    **pull 실패가 런을 실패로 만들지 않는다** — 측정은 이미 성공했고 결과는 서버에
    있다. 사유만 한 줄 찍고 종료코드는 런의 것을 그대로 준다.
    """
    rc = _stream(project, stages, opts)
    if rc == 0:
        try:
            pull(project)
        except (Exception, SystemExit) as e:   # _raw 는 거절을 SystemExit 로 낸다
            print(f"[알림] 로컬 사본을 갱신하지 못했습니다 ({e}) — 측정 결과는 "
                  "서버에 있습니다. 나중에 python remote.py pull 로 다시 받으세요.",
                  file=sys.stderr)
    return rc


def _stream(project: str, stages, opts: dict) -> int:
    """POST /api/run → /api/run/log 폴링 → 받은 텍스트를 그대로 print.

    서버가 이미 print_summary 까지 찍어 놓은 것을 흘려받는 것이라, 여기서는
    무엇도 다시 조립하지 않는다.

    런이 실제로 시작한 표시(`running` 이거나 새 텍스트)를 볼 때까지는 조용히
    기다린다 — 워커가 뜨기까지 몇 초 걸리는데, 그 사이 "안 돌고 텍스트도 없음"을
    끝난 것으로 읽으면 런 전체를 놓친 채 0 으로 끝난다.
    """
    api("POST", "/api/run",
        json={"project": project, "stages": ",".join(stages), "opts": opts})

    since, started, deadline = 0, False, time.time() + START_WAIT
    while True:
        d = api("GET", "/api/run/log", params={"project": project, "since": since})
        running = bool(d.get("running"))
        text = d.get("text") or ""
        if not started:
            if not running and not text:
                if time.time() > deadline:
                    print(f"서버가 '{project}' 런을 시작하지 않았습니다 — 웹 화면에서 "
                          "상태를 확인하세요.", file=sys.stderr)
                    return 1
                time.sleep(POLL)
                continue
            started = True
        if text:
            print(text, end="", flush=True)
            since = int(d.get("next", since + len(text)))
        elif not running:
            return 0
        time.sleep(POLL)


# ── 로컬 사본 (pull) ────────────────────────────────────────────────────────
#
# 로컬 셋팅은 로컬에만 쌓인다. 호스팅 셋팅은 **서버와 로컬 둘 다**에 쌓인다 —
# 호스팅을 쓴다고 자기 컴퓨터에 아무것도 안 남으면 /capture ask 도 대시보드도
# 서버가 살아 있을 때만 되는 것이 된다.
#
# 표 목록을 여기 손으로 적지 않는다. 스키마가 스스로 답한다: PRAGMA
# foreign_key_list 로 부모를 얻고 위상 정렬해 부모부터 옮긴다. 새 표가 생겨도
# 이 파일을 안 고치는 것이 이 설계의 요점이다 — 목록을 두 벌 만들지 않는다.


def _tables(con, schema: str) -> set[str]:
    return {r[0] for r in con.execute(
        f"SELECT name FROM {schema}.sqlite_master "
        "WHERE type='table' AND name NOT LIKE 'sqlite_%'")}


def _cols(con, schema: str, table: str) -> list[str]:
    return [r["name"] for r in con.execute(f'PRAGMA {schema}.table_info("{table}")')]


def _parents(con, table: str, tabs: set[str]) -> dict[str, str]:
    """{FK 컬럼: 부모 표}. 선언된 FK 가 정본이고, 이름은 그 다음이다.

    `creations.opportunity_id` 처럼 **FK 를 선언하지 않은 부모 참조**가 하나 있다.
    그냥 두면 서버 id 가 로컬 brain 에 그대로 실려 엉뚱한 기회를 가리킨다(대시보드가
    이 컬럼으로 조인한다). 그래서 `<단수>_id` → `<복수>` 표가 실재하면 부모로 친다.
    """
    p = {r["from"]: r["table"]
         for r in con.execute(f'PRAGMA foreign_key_list("{table}")')}
    for c in _cols(con, "main", table):
        if c == "id" or c in p or not c.endswith("_id"):
            continue
        stem = c[:-3]
        guess = stem[:-1] + "ies" if stem.endswith("y") else stem + "s"
        if guess in tabs:
            p[c] = guess
    return {c: t for c, t in p.items() if t in tabs and t != table}


def _plan(con) -> tuple[list[str], dict[str, dict[str, str]]]:
    """(부모부터 오는 표 순서, 표→부모맵). 사이트에 안 딸린 표는 아예 빠진다.

    projects 에서 출발해 FK 로 닿는 표만 모은다(칸 위상 정렬) — 모든 부모가 이미
    범위 안에 든 표만 다음 줄에 들어가므로, 나오는 순서가 곧 부모 먼저다.
    """
    tabs = _tables(con, "main")
    par = {t: _parents(con, t, tabs) for t in tabs}
    order, scope = ["projects"], {"projects"}
    while True:
        nxt = [t for t in sorted(tabs - scope)
               if par[t] and set(par[t].values()) <= scope]
        if not nxt:
            return order, par
        order += nxt
        scope |= set(nxt)


def _pred(parents: dict[str, str], src: str, col: str) -> str:
    """"이 행이 그 사이트 것인가" — 부모 하나는 범위 안이고, 범위 밖 부모는 없다.

    범위 밖 부모가 있는 행을 들이면 그 FK 를 갈아 끼울 짝이 없다(= 남의 사이트를
    가리키는 행이 생긴다). 그래서 OR 로 소속을 보고 AND 로 온전함을 본다.
    """
    hit = {c: f'"{c}" IN (SELECT {col} FROM {src} WHERE tbl=\'{p}\')'
           for c, p in parents.items()}
    return ("(" + " OR ".join(hit.values()) + ") AND ("
            + " AND ".join(f'("{c}" IS NULL OR {e})' for c, e in hit.items()) + ")")


def merge(remote_file, project: str) -> dict[str, int]:
    """원격 brain 에서 **이 사이트 행만** 로컬 brain.db 로 옮긴다. 표→옮긴 행수.

    id 는 로컬이 새로 매기고 FK 는 idmap 으로 갈아 끼운다 — 서버 id 를 그대로
    실으면 이미 다른 사이트가 쓰고 있는 번호와 겹쳐, 순위 스냅샷이 남의 키워드에
    붙는다. 컬럼은 양쪽 table_info 의 **교집합**만 쓴다(서버 스키마가 앞설 수 있다).

    전부 한 트랜잭션이다. 중간에 실패하면 롤백 — 반쯤 병합된 brain 을 남기지 않는다.
    """
    import db      # 늦은 import: doctor 가 pip 이전에 이 모듈을 읽는다

    db.connect().close()                 # 로컬 스키마·마이그레이션 보장
    # ATTACH 에 file: URI 를 쓰려면 **본 연결도** URI 로 열려 있어야 한다(SQLITE_OPEN_URI).
    con = sqlite3.connect(db.db_path().resolve().as_uri(), uri=True)
    con.row_factory = sqlite3.Row
    con.isolation_level = None           # BEGIN/COMMIT/ROLLBACK 을 손으로 잡는다
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("ATTACH DATABASE ? AS rem",
                (Path(remote_file).resolve().as_uri() + "?mode=ro",))
    try:
        order, par = _plan(con)
        order = [t for t in order if t in _tables(con, "rem")]
        assert order[:1] == ["projects"], "원격에 projects 표가 없다"
        con.execute("CREATE TEMP TABLE _map(tbl TEXT, rid INT, lid INT)")
        con.execute("CREATE TEMP TABLE _old(tbl TEXT, id INT)")
        con.execute("BEGIN")

        row = con.execute("SELECT id FROM rem.projects WHERE name=?", (project,)).fetchone()
        if row is None:
            raise LookupError(f"원격 보관함에 '{project}' 가 없습니다")
        rpid = row["id"]
        row = con.execute("SELECT id FROM main.projects WHERE name=?", (project,)).fetchone()
        if row is not None:
            # 있으면 로컬 행을 그대로 둔다: config_path 는 로컬 경로라 서버 값으로
            # 덮으면 그 사이트 로컬 명령이 통째로 깨진다.
            lpid = row["id"]
        else:
            cols = [c for c in _cols(con, "main", "projects")
                    if c != "id" and c in _cols(con, "rem", "projects")]
            q = ",".join(f'"{c}"' for c in cols)
            src = con.execute(f"SELECT {q} FROM rem.projects WHERE id=?", (rpid,)).fetchone()
            lpid = con.execute(f"INSERT INTO main.projects ({q}) "
                               f'VALUES ({",".join("?" * len(cols))})', tuple(src)).lastrowid

        # 1) 로컬에서 지울 범위를 부모부터 훑고, **자식부터** 지운다
        #    (부모를 먼저 지우면 아직 남은 자식이 FK 로 막는다)
        con.execute("INSERT INTO _old VALUES('projects', ?)", (lpid,))
        for t in order[1:]:
            con.execute(f'INSERT INTO _old SELECT \'{t}\', id FROM main."{t}" '
                        f'WHERE {_pred(par[t], "_old", "id")}')
        for t in reversed(order[1:]):
            con.execute(f'DELETE FROM main."{t}" '
                        f"WHERE id IN (SELECT id FROM _old WHERE tbl='{t}')")

        # 2) 부모부터 넣으면서 idmap 을 채운다
        idmap: dict[tuple[str, int], int] = {("projects", rpid): lpid}
        con.execute("INSERT INTO _map VALUES('projects', ?, ?)", (rpid, lpid))
        is_parent = {p for t in order for p in par[t].values()}
        counts: dict[str, int] = {}
        for t in order[1:]:
            shared = [c for c in _cols(con, "main", t)
                      if c != "id" and c in _cols(con, "rem", t)]
            pcols = {c: p for c, p in par[t].items() if c in shared}
            if not shared or not pcols:
                continue                 # 소속을 판정할 부모 컬럼이 원격에 없다
            q = ",".join(f'"{c}"' for c in shared)
            rows = con.execute(f'SELECT id,{q} FROM rem."{t}" '
                               f'WHERE {_pred(pcols, "_map", "rid")}')
            ins = con.cursor()
            n = 0
            for r in rows:
                vals = [idmap[(par[t][c], r[c])] if c in par[t] and r[c] is not None
                        else r[c] for c in shared]
                ins.execute(f'INSERT INTO main."{t}" ({q}) '
                            f'VALUES ({",".join("?" * len(shared))})', vals)
                if t in is_parent:
                    idmap[(t, r["id"])] = ins.lastrowid
                    con.execute("INSERT INTO _map VALUES(?,?,?)", (t, r["id"], ins.lastrowid))
                n += 1
            counts[t] = n
        con.execute("COMMIT")
        return counts
    except BaseException:
        con.execute("ROLLBACK")
        raise
    finally:
        con.close()


def _uniques(con, table: str) -> list[list[str]]:
    """표의 UNIQUE 제약 컬럼 묶음들 — 이미 있는 행을 id 로 되찾는 열쇠."""
    out = []
    for ix in con.execute(f'PRAGMA main.index_list("{table}")'):
        if ix["unique"]:
            out.append([r["name"] for r in con.execute(f'PRAGMA main.index_info("{ix["name"]}")')])
    return out


def graft(src_file, src_project: str, dst_project: str) -> dict[str, int]:
    """src brain 의 src_project 행을 main 의 dst_project 에 **더한다** — merge 의 역방향.

    merge 는 "서버 사본을 받는다" 라 목적지를 비우고 채우지만, 이쪽은 목적지가 이미
    자기 측정치(GSC 등)를 가진 살아 있는 사이트라 **지우지 않는다**. 규칙 셋:
    - projects 행은 만들지 않는다. dst 가 없으면 LookupError.
    - UNIQUE 로 이미 있는 행(같은 키워드·같은 질문)은 새로 안 넣고 기존 id 로 잇는다 —
      그 밑의 자식(순위 스냅샷)은 기존 키워드 밑에 붙는다.
    - UNIQUE 가 없는 표는 **같은 값의 행이 이미 있으면 건너뛴다**. 그래서 두 번 밀어도
      스냅샷이 두 배가 되지 않는다(멱등).
    id 재배치·FK 갈아끼우기·한 트랜잭션은 merge 와 같다.
    """
    import db      # 늦은 import: doctor 가 pip 이전에 이 모듈을 읽는다

    db.connect().close()
    con = sqlite3.connect(db.db_path().resolve().as_uri(), uri=True)
    con.row_factory = sqlite3.Row
    con.isolation_level = None
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("ATTACH DATABASE ? AS rem",
                (Path(src_file).resolve().as_uri() + "?mode=ro",))
    try:
        order, par = _plan(con)
        order = [t for t in order if t in _tables(con, "rem")]
        assert order[:1] == ["projects"], "원본에 projects 표가 없다"
        con.execute("CREATE TEMP TABLE _map(tbl TEXT, rid INT, lid INT)")
        con.execute("BEGIN")

        row = con.execute("SELECT id FROM rem.projects WHERE name=?", (src_project,)).fetchone()
        if row is None:
            raise LookupError(f"원본 보관함에 '{src_project}' 가 없습니다")
        rpid = row["id"]
        row = con.execute("SELECT id FROM main.projects WHERE name=?", (dst_project,)).fetchone()
        if row is None:
            raise LookupError(f"목적지 보관함에 '{dst_project}' 가 없습니다 — 먼저 등록하세요")
        lpid = row["id"]

        idmap: dict[tuple[str, int], int] = {("projects", rpid): lpid}
        con.execute("INSERT INTO _map VALUES('projects', ?, ?)", (rpid, lpid))
        is_parent = {p for t in order for p in par[t].values()}
        counts: dict[str, int] = {}
        for t in order[1:]:
            shared = [c for c in _cols(con, "main", t)
                      if c != "id" and c in _cols(con, "rem", t)]
            pcols = {c: p for c, p in par[t].items() if c in shared}
            if not shared or not pcols:
                continue
            q = ",".join(f'"{c}"' for c in shared)
            uniq = [u for u in _uniques(con, t) if set(u) <= set(shared)]
            rows = con.execute(f'SELECT id,{q} FROM rem."{t}" '
                               f'WHERE {_pred(pcols, "_map", "rid")}')
            ins = con.cursor()
            n = 0
            for r in rows:
                vals = [idmap[(par[t][c], r[c])] if c in par[t] and r[c] is not None
                        else r[c] for c in shared]
                lid = None
                for u in uniq:                       # 같은 키(키워드·질문)면 기존 행
                    hit = con.execute(
                        f'SELECT id FROM main."{t}" WHERE '
                        + " AND ".join(f'"{c}" IS ?' for c in u),
                        [vals[shared.index(c)] for c in u]).fetchone()
                    if hit:
                        lid = hit["id"]
                        break
                if lid is None:                      # 값 전체가 같은 행이 있으면 그것
                    hit = con.execute(
                        f'SELECT id FROM main."{t}" WHERE '
                        + " AND ".join(f'"{c}" IS ?' for c in shared), vals).fetchone()
                    if hit:
                        lid = hit["id"]
                if lid is None:
                    try:
                        ins.execute(f'INSERT INTO main."{t}" ({q}) '
                                    f'VALUES ({",".join("?" * len(shared))})', vals)
                    except sqlite3.IntegrityError:
                        # 식(expression) UNIQUE — rank_snapshots 의 (keyword_id, date(checked_at))
                        # 처럼 index_info 로 컬럼을 못 읽는 열쇠. 같은 날 스냅샷이 이미 있는
                        # 것이니 건너뛴다. 부모 표라면 자식을 이을 id 가 없어 그대로 던진다.
                        if t in is_parent:
                            raise
                        continue
                    lid = ins.lastrowid
                    n += 1
                if t in is_parent:
                    idmap[(t, r["id"])] = lid
                    con.execute("INSERT INTO _map VALUES(?,?,?)", (t, r["id"], lid))
            counts[t] = n
        con.execute("COMMIT")
        return counts
    except BaseException:
        con.execute("ROLLBACK")
        raise
    finally:
        con.close()


def push(project: str, remote_project: str | None = None) -> dict[str, int]:
    """로컬 brain 의 project 행을 서버의 remote_project(기본 같은 이름)에 더한다.

    파일은 backup() 으로 떠서 보낸다 — 열려 있는 brain.db 를 그대로 읽으면 찢어진다
    (/api/brain 이 내려줄 때와 같은 이유). 서버가 graft 를 돌리고 표→행수를 돌려준다.
    """
    import db
    dst = remote_project or project
    fd, tmp = tempfile.mkstemp(prefix="seo-miner-push-", suffix=".db")
    os.close(fd)
    try:
        s = sqlite3.connect(db.db_path().resolve().as_uri() + "?mode=ro", uri=True)
        d = sqlite3.connect(tmp)
        try:
            s.backup(d)
        finally:
            d.close()
            s.close()
        r = api("POST", "/api/brain/import", params={"project": dst, "src": project},
                data=Path(tmp).read_bytes(),
                headers={"Content-Type": "application/octet-stream"})
    finally:
        Path(tmp).unlink(missing_ok=True)
    counts = r.get("counts") or {}
    added = ", ".join(f"{t} +{n}" for t, n in counts.items() if n) or "새로 더한 행 없음(이미 있음)"
    print(f"서버 사이트 '{dst}' 에 더함 ({project} → {dst}): {added}")
    return counts


def _download() -> Path:
    """원격 brain 을 임시 파일로 받는다.

    ponytail: 통째로 메모리에 올렸다 쓴다. brain 이 수백 MB 가 되면 stream=True 로.
    """
    fd, tmp = tempfile.mkstemp(prefix="seo-miner-brain-", suffix=".db")
    os.close(fd)
    Path(tmp).write_bytes(fetch("/api/brain"))
    return Path(tmp)


def _pull_line(project: str, counts: dict[str, int]) -> str:
    p, h = str(paths.db_file()), str(Path.home())
    if p.startswith(h):
        p = "~" + p[len(h):]
    snaps = sum(n for t, n in counts.items() if t.endswith("_snapshots"))
    return (f"로컬 사본 갱신: {p} ({project} — "
            f"키워드 {counts.get('keywords', 0):,} · 스냅샷 {snaps:,})")


def pull(project: str, src: Path | None = None) -> dict[str, int]:
    """GET /api/brain → 임시 파일 → 이 사이트 행만 로컬 brain.db 에 병합."""
    own = src is None
    if own:
        src = _download()
    try:
        counts = merge(src, project)
    finally:
        if own:
            src.unlink(missing_ok=True)
    print(_pull_line(project, counts))
    return counts


# ── dispatch ────────────────────────────────────────────────────────────────

def _shadow_warn(project: str) -> None:
    """로컬 brain.db 에 같은 이름이 남아 있으면 한 줄 경고. 원격이 이긴다.

    db.connect() 를 부르지 않는다 — 진단하러 왔다가 Brain 을 만들거나 마이그레이션을
    돌리면 안 된다. 읽기 전용으로 열어 이름만 본다.
    """
    if project in _warned:
        return
    _warned.add(project)
    f = paths.db_file()
    if not f.exists():
        return
    try:
        con = sqlite3.connect(f"file:{f}?mode=ro", uri=True)
        try:
            hit = con.execute("SELECT 1 FROM projects WHERE name=?", (project,)).fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return
    if hit:
        print(f"[알림] '{project}' 는 로컬 보관함에도 있지만 원격(호스팅) 사이트로 "
              "실행합니다 — 로컬 잔존물은 건드리지 않습니다.", file=sys.stderr)


def _defaults(stage: str) -> dict | None:
    """그 단계 수집기 파서의 dest→기본값. 정본은 run_all.STAGES(stage.knobs) 다.

    gaps·report 는 자체 모듈이 아니라 run_all 의 함수라 노브가 없다 — 애초에
    opts 도 없다.
    """
    import run_all      # 늦은 import — run_all 이 수집기를, 수집기가 이 모듈을 읽는다
    stg = run_all.STAGE_BY_NAME.get(stage)
    if stg is None or not stg.knobs:
        return None
    return {dest: default for dest, (_typ, default) in stg.knobs.items()}


def opts_of(args, stage: str) -> dict:
    """argparse 네임스페이스에서 **기본값이 아닌 값만** `{stage}.{key}` 로 뽑는다.

    기본값과 같은 것은 안 보낸다: 서버는 자기 config.yaml·프로젝트 yaml 로 그
    값을 스스로 정하는데(collector.settings 의 우선순위), 로컬이 파서 fallback 을
    실어 보내면 서버 쪽 설정을 조용히 덮어쓴다.

    project·dry_run 은 opts 가 아니다(각각 URL 과 §4 의 dry-run 규칙이 가져간다).
    """
    defaults = _defaults(stage)
    if not defaults:
        return {}
    out = {}
    for k, v in vars(args).items():
        if k in ("project", "dry_run") or k not in defaults:
            continue
        if v is None or v == defaults[k]:
            continue
        out[f"{stage}.{k}"] = v
    return out


def _chain_plan(args) -> tuple[list, dict]:
    """run_all 용 — 전체 런이지만 --only/--skip/--opt 는 살려서 보낸다.

    비워 보내면 서버가 주기 판정까지 갱신하는 '정규 런'이 된다(store.request_run).
    --only 를 준 사용자가 전체 런 비용을 무는 것보다 그 단계만 도는 게 맞다.
    """
    import run_all
    only = [s for s in (getattr(args, "only", None) or "").split(",") if s.strip()]
    skip = {s.strip() for s in (getattr(args, "skip", None) or "").split(",") if s.strip()}
    stages = only or ([s for s in run_all.VALID_STAGE_NAMES if s not in skip] if skip else [])
    opts = {f"{st}.{k}": v
            for st, kv in run_all.parse_opts(getattr(args, "opt", None)).items()
            for k, v in kv.items()}
    return stages, opts


def dispatch(args, stage: str | None) -> bool:
    """수집기 main() 이 부르는 한 줄. 원격이면 실행하고 True, 아니면 False.

    stage=None 은 run_all(전체 체인)이다.

    거짓을 주는 경우 아무것도 안 찍는다 — 로컬 사용자에게는 이 모듈이 없는 것과
    같아야 한다.
    """
    project = getattr(args, "project", None)
    if not project or not owns(project):
        return False
    _shadow_warn(project)

    if getattr(args, "dry_run", False):
        # 조용히 로컬로 흘리면 안 된다: 사용자는 "계획만 봤다"고 믿고 다음 명령을 친다.
        print("원격 사이트는 서버가 비용을 냅니다 — dry-run 이 없습니다.")
        return True

    stages, opts = _chain_plan(args) if stage is None else ([stage], opts_of(args, stage))
    rc = run(project, stages, opts)
    if rc:
        sys.exit(rc)
    return True


# ── connect / disconnect / status / sync ────────────────────────────────────

def link(url: str, token: str) -> None:
    """검증(= /api/projects 가 200) + 사이트 목록 캐시 + remote.json 저장."""
    url = url.rstrip("/")
    projects = api("GET", "/api/projects", cfg={"url": url, "token": token})
    _save({"url": url, "token": token, "projects": list(projects),
           "linked_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")})
    print(f"연결됨: {url}")
    print(f"원격 사이트 {len(projects)}개: {', '.join(projects) or '(없음)'}")


def unlink() -> None:
    f = _file()
    f.unlink(missing_ok=True)
    print("원격 연결을 끊었습니다 — 이제 전부 로컬 보관함으로 돕니다.")


def sync() -> None:
    """사이트 목록 캐시만 갱신. 웹에서 사이트를 추가한 뒤 이걸 부른다."""
    c = config()
    if not c:
        sys.exit("원격 연결이 없습니다 — python remote.py connect <url> <token>")
    c["projects"] = list(api("GET", "/api/projects"))
    _save(c)
    print(f"원격 사이트 {len(c['projects'])}개: {', '.join(c['projects']) or '(없음)'}")


def status() -> None:
    """url·사이트 목록·연결 시각만. 토큰은 안 찍는다."""
    c = config()
    if not c:
        print("원격: 연결 안 됨 (전부 로컬 보관함)")
        return
    ps = c.get("projects") or []
    print(f"원격: {c['url']}")
    print(f"사이트 {len(ps)}개: {', '.join(ps) or '(없음)'}")
    print(f"연결 시각: {c.get('linked_at') or '(모름)'}")


# ── 자체점검 ────────────────────────────────────────────────────────────────

class _Resp:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code, self._p, self.text = status, payload, text

    def json(self):
        if self._p is None:
            raise ValueError("no json")
        return self._p

    @property
    def content(self):
        return self.text.encode("utf-8")


def _selfcheck() -> None:
    """가짜 HTTP 로 판정·opts·폴백·dry-run·이름 충돌을 못 박는다. 네트워크 0회."""
    import argparse
    import contextlib
    import io
    import tempfile

    saved_home = os.environ.get("CAPTURE_HOME")
    saved_req = _request
    d = Path(tempfile.mkdtemp(prefix="seo-miner-remote-selftest-"))
    os.environ["CAPTURE_HOME"] = str(d)
    globals()["_warned"] = set()

    try:
        # ── 1. 설정이 없으면 원격은 존재하지 않는 것처럼 조용하다
        assert config() is None, "빈 CAPTURE_HOME 인데 설정을 찾았다"
        assert owns("mysite") is False
        a = argparse.Namespace(project="mysite", dry_run=False, depth=None)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            assert dispatch(a, "rank") is False, "로컬인데 원격이 삼켰다"
        assert out.getvalue() == "", f"로컬 경로에 출력이 늘었다: {out.getvalue()!r}"

        # url/token 이 비면 있어도 없는 것으로 친다
        _save({"url": "", "token": "", "projects": ["mysite"]})
        assert config() is None and owns("mysite") is False, "빈 url/token 이 통과했다"

        # ── 2. 원격 판정 — 캐시에 있는 이름만
        _save({"url": "https://h.example/", "token": "smt_secret",
               "projects": ["mysite"], "linked_at": "2026-09-01T00:00:00Z"})
        assert owns("mysite") is True and owns("othersite") is False

        # ── 3. dry-run 은 원격을 안 태우고 한 줄만 (조용히 로컬로 흘리면 안 된다)
        calls: list = []
        globals()["_request"] = lambda m, u, **kw: (_ for _ in ()).throw(
            AssertionError("dry-run 이 원격을 태웠다"))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            assert dispatch(argparse.Namespace(project="mysite", dry_run=True), "rank") is True
        assert "dry-run" in out.getvalue(), out.getvalue()

        # ── 4. opts 추출 — 기본값과 같은 것은 안 보낸다
        import collect_serp
        args = collect_serp._parser().parse_args(["--project", "mysite", "--device", "mobile"])
        got = opts_of(args, "rank")
        assert got == {"rank.device": "mobile"}, got
        args = collect_serp._parser().parse_args(
            ["--project", "mysite", "--depth", "20", "--force"])
        assert opts_of(args, "rank") == {"rank.depth": 20, "rank.force": True}, \
            opts_of(args, "rank")
        # 명시적으로 기본값을 쳐도 안 나간다 (서버 쪽 설정을 덮어쓰지 않기 위해)
        import expand_keywords
        args = expand_keywords._parser().parse_args(
            ["--project", "mysite", "--mode", "all", "--per-seed-cap", "60"])
        assert opts_of(args, "keywords") == {}, opts_of(args, "keywords")
        args = expand_keywords._parser().parse_args(["--project", "mysite", "--mode", "gsc"])
        assert opts_of(args, "keywords") == {"keywords.mode": "gsc"}, opts_of(args, "keywords")
        assert opts_of(argparse.Namespace(project="x", dry_run=False), "gaps") == {}, \
            "모듈이 없는 단계는 opts 가 없어야 한다"

        # ── 5. 런 — POST 한 번, 폴링으로 받은 텍스트를 '그대로' 찍는다
        log = [{"text": "", "next": 0, "running": False},          # 아직 시작 전
               {"text": "[gsc] ok\n", "next": 9, "running": True},
               {"text": "요약표\n", "next": 16, "running": True},
               {"text": "", "next": 16, "running": False}]

        def fake(method, url, **kw):
            calls.append((method, url, kw))
            assert kw["headers"]["Authorization"] == "Bearer smt_secret"
            if url.endswith("/api/run"):
                return _Resp(200, {"ok": True, "started": True})
            if "/api/run/log" in url:
                return _Resp(200, log.pop(0))
            if url.endswith("/api/brain"):     # 사본 갱신이 실패해도 런은 성공이다
                return _Resp(404, {"detail": "서버에 아직 보관함이 없습니다"})
            raise AssertionError(url)

        globals()["_request"] = fake
        globals()["POLL"] = 0.0
        out, errs = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(errs):
            assert dispatch(collect_serp._parser().parse_args(
                ["--project", "mysite", "--device", "mobile"]), "rank") is True
        assert out.getvalue() == "[gsc] ok\n요약표\n", repr(out.getvalue())
        assert "사본" in errs.getvalue(), errs.getvalue()
        body = calls[0][2]["json"]
        assert body == {"project": "mysite", "stages": "rank",
                        "opts": {"rank.device": "mobile"}}, body
        assert calls[0][1] == "https://h.example/api/run", calls[0][1]   # 끝 슬래시 정리
        # POST, log(시작 전), log(첫 텍스트) 다음이 오프셋을 태운 폴링이다
        assert calls[3][2]["params"] == {"project": "mysite", "since": 9}, calls[3][2]

        # ── 5b. 첫 폴링 전에 끝난 짧은 런도 놓치지 않는다 (running 은 이미 거짓)
        log[:] = [{"text": "짧은 런\n", "next": 7, "running": False},
                  {"text": "", "next": 7, "running": False}]   # 꼬리 확인 한 번
        out, errs = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(errs):
            assert run("mysite", ["gsc"], {}) == 0, "사본 갱신 실패가 런을 실패로 만들었다"
        assert out.getvalue() == "짧은 런\n", repr(out.getvalue())

        # ── 6. 이름 충돌: 원격이 이기고 stderr 에 한 줄 경고
        con = sqlite3.connect(d / "brain.db")
        con.execute("CREATE TABLE projects (id INTEGER PRIMARY KEY, name TEXT)")
        con.execute("INSERT INTO projects(name) VALUES('mysite')")
        con.commit()
        con.close()
        globals()["_warned"] = set()
        log[:] = [{"text": "x\n", "next": 2, "running": True},
                  {"text": "", "next": 2, "running": False}]
        outs, errs = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(outs), contextlib.redirect_stderr(errs):
            assert dispatch(argparse.Namespace(project="mysite", dry_run=False), "gaps") is True
        assert "원격" in errs.getvalue() and "mysite" in errs.getvalue(), errs.getvalue()
        assert "smt_secret" not in errs.getvalue() + outs.getvalue(), "토큰이 샜다"

        # ── 7. 400(모르는 opt)은 사유 그대로 + 비정상 종료
        globals()["_request"] = lambda m, u, **kw: _Resp(
            400, {"detail": "모르는 옵션입니다: rank.nope"})
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                run("mysite", ["rank"], {"rank.nope": 1})
        except SystemExit as e:
            assert "rank.nope" in str(e), e
        else:
            raise AssertionError("400 인데 안 죽었다")

        # 401 은 재연결 안내로, 토큰은 안 싣고
        globals()["_request"] = lambda m, u, **kw: _Resp(401, {"detail": "로그인이 필요합니다"})
        try:
            api("GET", "/api/projects")
        except SystemExit as e:
            assert "connect" in str(e) and "smt_secret" not in str(e), e
        else:
            raise AssertionError("401 인데 안 죽었다")

        # ── 8. status 는 토큰을 안 찍는다
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            status()
        assert "smt_secret" not in out.getvalue(), out.getvalue()
        assert "https://h.example" in out.getvalue() and "mysite" in out.getvalue()

        # ── 9. connect/disconnect 왕복
        globals()["_request"] = lambda m, u, **kw: _Resp(200, ["a", "b"])
        with contextlib.redirect_stdout(io.StringIO()):
            link("https://h.example/", "smt_new")
        assert config()["projects"] == ["a", "b"] and owns("a")
        with contextlib.redirect_stdout(io.StringIO()):
            unlink()
        assert config() is None and owns("a") is False, "끊었는데 원격이 남았다"

        # ── 10. run_all(전체 체인) — --only 는 살리고 --opt 는 편다
        import run_all
        ns = argparse.Namespace(project="mysite", dry_run=False, skip=None,
                                only="gsc,ai", opt=["rank.device=mobile"])
        assert _chain_plan(ns) == (["gsc", "ai"], {"rank.device": "mobile"}), _chain_plan(ns)
        ns = argparse.Namespace(project="mysite", dry_run=False, skip=None, only=None, opt=None)
        assert _chain_plan(ns) == ([], {}), "전체 런은 stages 를 비워 보낸다"
        ns = argparse.Namespace(project="mysite", dry_run=False, skip="ai", only=None, opt=None)
        st, _ = _chain_plan(ns)
        assert "ai" not in st and set(st) < set(run_all.VALID_STAGE_NAMES) and st, st

        # ── 11. 로컬 사본 병합 — **id 가 겹치는 상황**이 이 코드의 존재 이유다.
        # 진짜 sqlite 파일 두 개(원격 흉내 + 로컬)로 돌린다. 로컬에는 다른 사이트가
        # 먼저 있어 원격의 1,2,3 번을 이미 쓰고 있다 — 갈아 끼우지 않으면 순위
        # 스냅샷이 남의 키워드에 붙는다.
        import db as _db
        os.environ["CAPTURE_HOME"] = str(d / "merge")   # §6 의 가짜 brain 과 섞지 않는다

        def build(path, schema=None):
            c = sqlite3.connect(path)
            c.executescript(schema or _db.SCHEMA)
            c.row_factory = sqlite3.Row
            return c

        rem = d / "rem.db"
        rc_ = build(rem)
        rc_.executescript("""
            ALTER TABLE keywords ADD COLUMN future_col TEXT;   -- 서버 스키마가 앞선 경우
            INSERT INTO projects(id,name,domain) VALUES(1,'mysite','my.com'),(2,'x','x.com');
            INSERT INTO keywords(id,project_id,keyword,future_col) VALUES
              (1,1,'kw-a','앞선칸'),(2,1,'kw-b',NULL),(3,1,'kw-c',NULL),(4,2,'남의키워드',NULL);
            INSERT INTO runs(id,project_id,kind) VALUES(1,1,'full'),(2,2,'full');
            INSERT INTO rank_snapshots(id,keyword_id,position) VALUES(1,1,3),(2,3,7),(3,4,9);
            INSERT INTO gsc_snapshots(id,project_id,snapshot_date,period_days,query)
              VALUES(1,1,'2026-09-01',28,'q1'),(2,1,'2026-09-01',28,'q2');
            INSERT INTO ai_prompts(id,project_id,prompt) VALUES(1,1,'추천해줘');
            INSERT INTO ai_checks(id,prompt_id,run_id,engine) VALUES(1,1,1,'chatgpt');
            INSERT INTO opportunities(id,project_id,run_id,kind,target)
              VALUES(1,1,1,'content_gap','kw-a'),
            -- 남의 사이트 기회인데 우리 런을 가리킨다: 소속(OR)만 보면 딸려 온다
                    (2,2,1,'coverage','남의기회');
            INSERT INTO creations(id,project_id,opportunity_id,file_path)
              VALUES(1,1,1,'posts/a.md');
        """)
        rc_.commit()
        rc_.close()

        lc = _db.connect()
        lc.executescript("""
            INSERT INTO projects(name,domain) VALUES('other','o.com');       -- id 1
            INSERT INTO keywords(project_id,keyword) VALUES
              (1,'o1'),(1,'o2'),(1,'o3'),(1,'o4'),(1,'o5');                  -- id 1..5
            INSERT INTO runs(project_id,kind) VALUES(1,'full');
            INSERT INTO rank_snapshots(keyword_id,position) VALUES(1,11);
            INSERT INTO opportunities(project_id,run_id,kind,target) VALUES(1,1,'coverage','o1');
            INSERT INTO creations(project_id,opportunity_id,file_path) VALUES(1,1,'o.md');
            INSERT INTO projects(name,domain) VALUES('mysite','my.com');      -- id 2
            INSERT INTO keywords(project_id,keyword) VALUES(2,'낡은키워드');   -- 지워질 잔존물
        """)
        lc.commit()

        def dump(c):
            return {t: [tuple(r) for r in c.execute(f'SELECT * FROM "{t}" ORDER BY id')]
                    for t in sorted(_tables(c, "main"))}

        def other_rows(c):
            return [tuple(r) for q in (
                "SELECT * FROM keywords WHERE project_id=1",
                "SELECT * FROM runs WHERE project_id=1",
                "SELECT * FROM opportunities WHERE project_id=1",
                "SELECT * FROM creations WHERE project_id=1",
                "SELECT s.* FROM rank_snapshots s JOIN keywords k ON k.id=s.keyword_id"
                " WHERE k.project_id=1") for r in c.execute(q)]

        before_other = other_rows(lc)

        # 위상 정렬이 뽑은 순서 — 부모가 항상 자식보다 앞이다
        _order, _par = _plan(lc)
        assert _order[0] == "projects"
        for _t in _order:
            for _p in _par[_t].values():
                assert _order.index(_p) < _order.index(_t), f"{_t} 가 부모 {_p} 보다 앞이다"
        assert "opportunities" in _par["creations"].values(), \
            "FK 를 선언 안 한 creations.opportunity_id 를 못 잡았다"

        counts = merge(rem, "mysite")
        assert counts["keywords"] == 3 and counts["gsc_snapshots"] == 2, counts
        lc.close()

        lc = _db.connect()

        kw = {r["keyword"]: r["id"] for r in lc.execute("SELECT id,keyword FROM keywords")}
        assert "남의키워드" not in kw, "원격의 다른 사이트 행이 따라왔다"
        assert not lc.execute("SELECT 1 FROM opportunities WHERE target='남의기회'").fetchone(), \
            "부모 하나가 우리 것이면 딸려 온다 — 범위 밖 부모(project_id)를 안 봤다"
        assert "낡은키워드" not in kw, "로컬 잔존물이 안 지워졌다"
        # 원격 id 1,2,3 은 로컬에서 other 가 이미 쓰고 있다 → 새 번호를 받아야 한다
        assert min(kw[k] for k in ("kw-a", "kw-b", "kw-c")) > 5, kw
        # 순위 스냅샷이 남의 키워드가 아니라 제 키워드에 붙었다 (= FK 를 갈아 끼웠다)
        got = {r["keyword"]: r["position"] for r in lc.execute(
            "SELECT k.keyword, s.position FROM rank_snapshots s"
            " JOIN keywords k ON k.id=s.keyword_id"
            " JOIN projects p ON p.id=k.project_id WHERE p.name='mysite'")}
        assert got == {"kw-a": 3, "kw-c": 7}, got
        # FK 를 선언 안 한 creations.opportunity_id 도 제 기회를 가리킨다
        assert lc.execute(
            "SELECT count(*) FROM creations c JOIN opportunities o ON o.id=c.opportunity_id"
            " JOIN projects p ON p.id=c.project_id WHERE p.name='mysite'").fetchone()[0] == 1, \
            "creations 가 없는(혹은 남의) 기회를 가리킨다"
        # 부모가 둘인 표(ai_checks: prompt_id·run_id)도 양쪽 다 제 것으로
        assert lc.execute(
            "SELECT count(*) FROM ai_checks c JOIN ai_prompts a ON a.id=c.prompt_id"
            " JOIN runs r ON r.id=c.run_id WHERE a.project_id=r.project_id").fetchone()[0] == 1, \
            "ai_checks 의 부모 둘이 어긋났다"
        # 로컬의 다른 사이트는 한 줄도 안 건드린다
        assert other_rows(lc) == before_other, "남의 사이트 행이 바뀌었다"

        # 멱등 — 두 번 병합해도 같은 결과
        snap = dump(lc)
        merge(rem, "mysite")
        lc.close()
        lc = _db.connect()
        assert dump(lc) == snap, "두 번 병합했더니 달라졌다"

        # 실패하면 롤백 — 반쯤 병합된 brain 을 남기지 않는다.
        # 원격에만 NOT NULL 이 없는 판을 만들어 로컬 제약을 일부러 어긴다.
        bad = d / "bad.db"
        bc = build(bad, _db.SCHEMA.replace("keyword TEXT NOT NULL", "keyword TEXT", 1))
        bc.executescript("""
            INSERT INTO projects(id,name,domain) VALUES(1,'mysite','my.com');
            INSERT INTO keywords(id,project_id,keyword) VALUES(1,1,NULL);""")
        bc.commit()
        bc.close()
        try:
            merge(bad, "mysite")
        except sqlite3.IntegrityError:
            pass
        else:
            raise AssertionError("NOT NULL 을 어겼는데 병합이 통과했다")
        lc.close()
        lc = _db.connect()
        assert dump(lc) == snap, "실패했는데 반쯤 병합된 brain 이 남았다"

        try:
            merge(rem, "없는사이트")
        except LookupError:
            pass
        else:
            raise AssertionError("원격에 없는 사이트인데 병합이 통과했다")
        assert dump(lc) == snap, "없는 사이트 병합이 로컬을 건드렸다"
        lc.close()

        # ── 11-b. graft(push 의 서버쪽) — 있는 사이트에 **더하고**, 두 번 해도 안 는다.
        lc = _db.connect()
        snap_g = dump(lc)
        g = graft(rem, "mysite", "mysite")
        assert not any(g.values()), f"같은 내용을 다시 얹었는데 행이 늘었다: {g}"
        assert dump(lc) == snap_g, "멱등 graft 가 brain 을 바꿨다"
        rem2 = d / "rem2.db"                 # 원본 픽스처(rem)는 아래 재병합 검사가 다시 쓴다
        rem2.write_bytes(rem.read_bytes())
        rc_ = sqlite3.connect(rem2)
        rc_.executescript("""
            INSERT INTO keywords(id,project_id,keyword) VALUES(5,1,'kw-new');
            INSERT INTO rank_snapshots(id,keyword_id,position,checked_at)
              VALUES(4,5,2,'2020-01-02T00:00:00Z'),(5,1,4,'2020-01-01T00:00:00Z');
        """)
        rc_.commit(); rc_.close()
        g = graft(rem2, "mysite", "mysite")
        assert g["keywords"] == 1 and g["rank_snapshots"] == 2, g
        kid = lc.execute("SELECT id FROM keywords WHERE keyword='kw-a'").fetchone()[0]
        assert lc.execute("SELECT COUNT(*) FROM rank_snapshots WHERE keyword_id=?", (kid,)).fetchone()[0] == 2, \
            "새 스냅샷이 기존 키워드(UNIQUE 로 되찾은 id) 밑에 안 붙었다"
        assert other_rows(lc) == before_other, "graft 가 남의 사이트를 건드렸다"
        try:
            graft(rem, "mysite", "없는사이트")
        except LookupError:
            pass
        else:
            raise AssertionError("없는 목적지인데 graft 가 통과했다")
        lc.close()
    finally:
        globals()["_request"] = saved_req
        globals()["POLL"] = 1.5
        if saved_home is None:
            os.environ.pop("CAPTURE_HOME", None)
        else:
            os.environ["CAPTURE_HOME"] = saved_home

    print("remote self-check ok")


def main() -> None:
    args = sys.argv[1:]
    if not args:
        _selfcheck()
        return
    cmd = args[0]
    if cmd == "connect" and len(args) == 3:
        link(args[1], args[2])
    elif cmd == "disconnect":
        unlink()
    elif cmd == "status":
        status()
    elif cmd == "sync":
        sync()
    elif cmd == "push" and len(args) in (2, 3):
        push(args[1], args[2] if len(args) == 3 else None)
    elif cmd == "pull" and len(args) == 2:
        pull(args[1])
    elif cmd == "pull":
        # 이름을 안 주면 캐시에 있는 사이트 전부. 내려받기는 **한 번만** 한다 —
        # 같은 파일을 사이트 수만큼 받을 이유가 없다.
        src = _download()
        try:
            for p in (config() or {}).get("projects") or []:
                pull(p, src)
        finally:
            src.unlink(missing_ok=True)
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
