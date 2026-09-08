# 검색어 심사 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 기회 목록 앞에 검색어 심사 단(무관·보류·작업)을 세우고, 기회 상태의 뜻을 진행 중·완료·관찰로 또렷하게 한다.

**Architecture:** 판정은 `verdicts(project_id, key=scoring.norm(대상), verdict)` 한 표에 산다. `db.connect()` 가 SQLite 에 `norm()` 함수를 등록해 조회가 SQL 안에서 같은 정규화로 조인한다. 기회 적재(`scoring.load`)와 열린 기회 조회(`db.list_opportunities(gated=True)`)가 그 표를 보고, 화면은 `/api/triage` · `/api/verdict` 둘로 심사 화면을 그린다. 상태 값은 넷 그대로이고 `status_at` 을 더해 완료 후 관찰을 조회로 만든다.

**Tech Stack:** Python 3.12 stdlib (sqlite3, http.server), FastAPI(호스팅 래퍼), 바닐라 JS 뷰(`templates/views/*.html`), 검사는 리포의 `test_*.py` 자체점검 + `run_checks.py`.

**Spec:** `docs/superpowers/specs/2026-09-08-keyword-triage-design.md`

## Global Constraints

- 파이썬은 `C:\Users\user\AppData\Local\Programs\Python\Python312\python.exe` 로 돌린다 (`PYTHONIOENCODING=utf-8`). PATH 의 `python` 은 스토어 스텁이다.
- 판정 값은 정확히 `irrelevant | hold | work` 셋. 정본은 `db.VERDICTS`.
- 정규화는 `scoring.norm` 하나. JS 에 사본을 두지 않는다.
- 심사 대상 종류 목록은 `scoring.KEYWORD_KINDS` 하나. 통과 종류 = `ALL_KINDS - KEYWORD_KINDS`.
- 상태 값은 `db.OPP_STATUSES = ("new","acked","done","dismissed")` 그대로. 다섯 번째 상태를 만들지 않는다.
- 한글에 `letter-spacing` · `text-transform:uppercase` · 등폭을 걸지 않는다 (CLAUDE.md).
- 화면을 고치면 커밋 전에 브라우저(Orca)로 그 경로를 실제로 누른다 (CLAUDE.md).
- 검사를 추가하면 일부러 깨서 FAIL 을 확인한다 (CLAUDE.md).
- 커밋 메시지 꼬리: `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>` + `Claude-Session: https://claude.ai/code/session_0158t5JCke5Azv5syXcboBow`.

---

## 파일 지도

| 파일 | 책임 |
|---|---|
| `skills/capture/scripts/db.py` | `verdicts` 표·`VERDICTS`·`set_verdicts`·`verdict_map`·`norm` SQL 함수 등록·`status_at`·`list_opportunities(gated)`·`watch_rows` |
| `skills/capture/scripts/scoring.py` | `KEYWORD_KINDS`, `load()` 의 판정 필터, `opportunities()` 가 gated 조회 |
| `skills/capture/scripts/dashboard.py` | `triage_payload`·`set_verdict` 본체, `ROUTES` 두 줄, `VIEW_ORDER` 에 `triage`, `gather()` 에 `watch` |
| `skills/capture/templates/views/triage.html` | 새 화면 [심사] |
| `skills/capture/templates/views/overview.html` | 상태 필터 라벨, "완료 후 관찰" 섹션 |
| `skills/capture/templates/dashboard.html` | `OPP_LABEL/NEXT/SET/DONE` 라벨·전이, 펼침 패널의 작업 목록, 박제본 drop |
| `server/app.py` | `/api/triage` · `/api/verdict` 래퍼, `/api/create` 가 `acked` |
| `skills/create/scripts/createdb.py` | `done` 서브명령이 `acked` 로 (호스팅과 같은 이유) |
| `skills/capture/scripts/test_capture.py` | 판정 저장·적재 필터·gated 조회·status_at 검사 |
| `skills/capture/scripts/test_dashboard.py` | `/api/triage` 페이로드·`watch` 검사 |
| `skills/capture/scripts/test_seams.py` | 17) 판정·상태 이음매 |
| `skills/capture/scripts/test_render.py` | 심사 화면 렌더 |

---

### Task 1: `verdicts` 표와 판정 쓰기·읽기

**Files:**
- Modify: `skills/capture/scripts/db.py` (SCHEMA 끝 `creations` 뒤, `OPP_STATUSES` 근처, `connect()`)
- Modify: `skills/capture/scripts/scoring.py:119` (`KEYWORD_KINDS`)
- Test: `skills/capture/scripts/test_capture.py`

**Interfaces:**
- Produces: `db.VERDICTS = ("irrelevant", "hold", "work")`
- Produces: `db.set_verdicts(conn, project_id: int, keys: list[str], verdict: str | None) -> int` — keys 는 이미 norm 된 문자열. `None` 이면 행 삭제(미판정). 값이 셋 밖이면 `ValueError`. `irrelevant` 면 같은 키의 `keywords.is_active=0`. 갱신 행 수 반환.
- Produces: `db.verdict_map(conn, project_id) -> dict[str, str]` — key → verdict.
- Produces: `conn` 에 SQL 함수 `norm(text)` 등록 (`db.connect()` 안에서 `scoring.norm` 을 늦게 import).
- Produces: `scoring.KEYWORD_KINDS = ("striking_distance","ctr_gap","cannibalization","rank_decay","pseo_pattern","device_gap","ai_citation_gap","aio_exposure","content_gap")`.

- [ ] **Step 1: 실패하는 검사**

`test_capture.py` 끝(`if __name__` 위)에 추가:

```python
def test_verdicts_write_read_and_irrelevant_deactivates_keyword():
    conn = db.connect()
    pid = _project(conn, "vd")["id"]
    conn.execute("INSERT INTO keywords(project_id,keyword,is_active) VALUES(?,?,1)",
                 (pid, "디아더피부과 가격"))
    conn.commit()
    k = scoring.norm("디아 더 피부과 가격")           # 변형도 같은 키
    assert db.set_verdicts(conn, pid, [k], "hold") == 1
    assert db.verdict_map(conn, pid) == {k: "hold"}
    assert conn.execute("SELECT is_active FROM keywords WHERE project_id=?", (pid,)).fetchone()[0] == 1
    db.set_verdicts(conn, pid, [k], "irrelevant")
    assert conn.execute("SELECT is_active FROM keywords WHERE project_id=?", (pid,)).fetchone()[0] == 0
    assert db.set_verdicts(conn, pid, [k], None) == 1
    assert db.verdict_map(conn, pid) == {}
    try:
        db.set_verdicts(conn, pid, [k], "maybe")
        assert False, "잘못된 판정을 받았다"
    except ValueError:
        pass
    # SQL 안에서도 같은 정규화를 쓴다
    assert conn.execute("SELECT norm('디아 더 피부과 가격')").fetchone()[0] == k
    assert set(scoring.KEYWORD_KINDS) < set(scoring.ALL_KINDS)
    conn.close()
```

- [ ] **Step 2: 실패 확인** — `python test_capture.py` → `AttributeError: module 'db' has no attribute 'set_verdicts'`

- [ ] **Step 3: 구현**

`db.py` SCHEMA 의 `creations` 표 뒤에:

```sql
CREATE TABLE IF NOT EXISTS verdicts (         -- 검색어 심사 (spec 2026-09-08 keyword-triage)
  project_id INTEGER NOT NULL REFERENCES projects(id),
  key        TEXT NOT NULL,                   -- scoring.norm(대상). 변형은 여기서 같은 값이 된다
  verdict    TEXT NOT NULL,                   -- irrelevant | hold | work
  decided_at TEXT DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(project_id, key)
);
```

`OPP_STATUSES` 옆:

```python
VERDICTS = ("irrelevant", "hold", "work")
```

`connect()` 가 연결을 만든 직후(`row_factory` 세운 뒤):

```python
    import scoring   # 늦게 — scoring 은 db 를 함수 안에서만 부른다(순환 없음)
    conn.create_function("norm", 1, scoring.norm)
```

함수 둘(`set_opportunity_status` 뒤):

```python
def set_verdicts(conn, project_id: int, keys: list[str], verdict: str | None) -> int:
    """검색어 심사 저장. keys 는 이미 scoring.norm 을 거친 것(서버가 만든 key).
    None 이면 미판정으로 되돌린다. irrelevant 는 같은 키의 키워드 추적을 끈다 —
    hold 는 우리 검색어라 측정을 계속한다."""
    if verdict is not None and verdict not in VERDICTS:
        raise ValueError(f"verdict must be one of {VERDICTS} or None, got {verdict!r}")
    keys = [str(k) for k in keys if str(k)]
    if not keys:
        return 0
    n = 0
    for k in keys:
        if verdict is None:
            n += conn.execute("DELETE FROM verdicts WHERE project_id=? AND key=?",
                              (int(project_id), k)).rowcount
        else:
            conn.execute("""INSERT INTO verdicts(project_id,key,verdict) VALUES(?,?,?)
                            ON CONFLICT(project_id,key) DO UPDATE SET
                              verdict=excluded.verdict, decided_at=CURRENT_TIMESTAMP""",
                         (int(project_id), k, verdict))
            n += 1
            if verdict == "irrelevant":
                conn.execute("UPDATE keywords SET is_active=0 WHERE project_id=? AND norm(keyword)=?",
                             (int(project_id), k))
    conn.commit()
    return n


def verdict_map(conn, project_id: int) -> dict[str, str]:
    return {r[0]: r[1] for r in conn.execute(
        "SELECT key, verdict FROM verdicts WHERE project_id=?", (int(project_id),))}
```

`scoring.py` `ALL_KINDS` 뒤:

```python
# 심사(검색어 판정)를 거치는 종류 — 대상이 검색어·질문문인 것. 나머지(coverage 의
# cluster:, index_blocked·crawl_issue 의 URL, backlink_* 의 도메인)는 판정 없이
# 기회 목록에 바로 선다. 화면·조회는 이 한 벌을 가리킨다.
KEYWORD_KINDS = ("striking_distance", "ctr_gap", "cannibalization", "rank_decay",
                 "pseo_pattern", "device_gap", "ai_citation_gap", "aio_exposure",
                 "content_gap")
```

- [ ] **Step 4: 통과 확인** — `python test_capture.py` PASS. `python scoring.py` 자체점검도 돌려 `_selfcheck` 가 여전히 통과하는지 본다.

- [ ] **Step 5: 커밋** — `feat(db): 검색어 심사 표 verdicts — norm SQL 함수·KEYWORD_KINDS`

---

### Task 2: 판정이 적재와 조회를 거른다

**Files:**
- Modify: `skills/capture/scripts/db.py:1122` (`list_opportunities`), `:1165` (`open_opportunities`)
- Modify: `skills/capture/scripts/scoring.py:2222` (`load`), `:2265` (`opportunities`)
- Test: `skills/capture/scripts/test_capture.py`

**Interfaces:**
- Produces: `db.list_opportunities(..., gated: bool = False)` — `gated=True` 면 `kind NOT IN KEYWORD_KINDS OR EXISTS(verdicts work)` 조건을 건다.
- Produces: `db.open_opportunities` 와 `scoring.opportunities` 는 `gated=True` 로 부른다.
- Produces: `scoring.load` 가 `irrelevant`·`hold` 키의 검색어 종류 행을 upsert 전에 뺀다.

- [ ] **Step 1: 실패하는 검사**

```python
def test_verdict_gates_load_and_open_list():
    conn = db.connect()
    pid = _project(conn, "vg")["id"]
    rows = [{"kind": "striking_distance", "target": "jenni ai 후기", "score": 30},
            {"kind": "pseo_pattern", "target": "jenni  ai 후기", "score": 20},   # 변형
            {"kind": "striking_distance", "target": "ecrett", "score": 25},
            {"kind": "index_blocked", "target": "https://e.com/x", "score": 10}]  # 통과 종류
    db.set_verdicts(conn, pid, [scoring.norm("jenni ai 후기")], "hold")
    db.set_verdicts(conn, pid, [scoring.norm("ecrett")], "work")
    kept = scoring.gate_rows(conn, pid, rows)
    assert {r["target"] for r in kept} == {"ecrett", "https://e.com/x"}, kept
    db.upsert_opportunities(conn, pid, None, rows)          # 직접 넣어도 조회가 거른다
    got = {r["target"] for r in db.open_opportunities(conn, pid, limit=50)}
    assert got == {"ecrett", "https://e.com/x"}, got
    allrows = {r["target"] for r in db.list_opportunities(conn, pid, order="screen", limit=50)}
    assert len(allrows) == 4, "gated=False 기본은 그대로 전부다"
    db.set_verdicts(conn, pid, [scoring.norm("ecrett")], None)   # 미판정 = 안 보인다
    assert "ecrett" not in {r["target"] for r in db.open_opportunities(conn, pid, limit=50)}
    conn.close()
```

- [ ] **Step 2: 실패 확인** — `AttributeError: ... 'gate_rows'`

- [ ] **Step 3: 구현**

`db.list_opportunities` 시그니처에 `gated: bool = False` 추가, `kinds` 필터 뒤에:

```python
    if gated:
        import scoring   # 심사 대상 종류의 정본
        ph = ",".join("?" * len(scoring.KEYWORD_KINDS))
        q += (f" AND (kind NOT IN ({ph}) OR EXISTS (SELECT 1 FROM verdicts v"
              f" WHERE v.project_id=opportunities.project_id AND v.key=norm(opportunities.target)"
              f" AND v.verdict='work'))")
        args += list(scoring.KEYWORD_KINDS)
```

`open_opportunities` 는 `gated=True` 를 넘긴다. `scoring.opportunities` 도 `gated=True`.

`scoring.py` `load()` 위에:

```python
def gate_rows(conn, project_id: int, rows: list[dict]) -> list[dict]:
    """심사에서 무관·보류로 판정된 검색어의 기회는 적재하지 않는다. 검색어가 아닌
    종류(KEYWORD_KINDS 밖)는 그대로 통과한다. 미판정은 적재한다 — 심사 화면이
    그 행을 보고 판정한다."""
    import db
    vm = db.verdict_map(conn, project_id)
    return [r for r in rows if r["kind"] not in KEYWORD_KINDS
            or vm.get(norm(str(r["target"]))) not in ("irrelevant", "hold")]
```

`load()` 에서 `with db.run(...)` 직전에 `rows = gate_rows(conn, pid, rows)`.

- [ ] **Step 4: 전체 검사** — `python test_capture.py`, `python test_dashboard.py`, `python scoring.py`. 기존 검사 중 `scoring.opportunities`/`open_opportunities` 로 결과를 세는 것(`test_list_opportunities_orders_are_distinct`, `test_axis_opps_*`, `test_load_covers_every_kind`)이 빈 목록으로 깨지면 그 픽스처에 `db.set_verdicts(conn, pid, [scoring.norm(t) for t in targets], "work")` 를 넣는다. 검사가 왜 깨졌는지 한 줄 주석으로 남긴다.

- [ ] **Step 5: 커밋** — `feat(scoring): 판정이 기회 적재와 열린 기회 조회를 거른다`

---

### Task 3: `status_at` 과 완료 후 관찰 조회

**Files:**
- Modify: `skills/capture/scripts/db.py` (`_migrate`, `set_opportunity_status`, 새 `watch_rows`)
- Test: `skills/capture/scripts/test_capture.py`

**Interfaces:**
- Produces: `opportunities.status_at TEXT` (마이그레이션 + SCHEMA 양쪽).
- Produces: `set_opportunity_status` 가 `status_at=CURRENT_TIMESTAMP` 를 같이 쓴다.
- Produces: `db.watch_rows(conn, project_id) -> list[dict]` — `status='done'` 이고 검색어 종류인 기회마다 `{id, kind, target, done_at, before: {clicks, position} | None, after: {...} | None, runs_since: int, stalled: bool}`. `before` 는 `status_at` 이전 마지막 GSC 스냅샷(같은 period 의 최신 것과 같은 period)에서, `after` 는 최신 스냅샷에서 그 검색어 행. `runs_since` 는 `status_at` 뒤의 서로 다른 `snapshot_date` 수. `stalled = runs_since >= 2 and (after is None or before is None or after["position"] >= before["position"])`.

- [ ] **Step 1: 실패하는 검사**

```python
def test_status_at_and_watch_rows():
    conn = db.connect()
    pid = _project(conn, "wt")["id"]
    db.upsert_opportunities(conn, pid, None, [{"kind": "striking_distance", "target": "q1", "score": 30}])
    oid = conn.execute("SELECT id FROM opportunities WHERE project_id=?", (pid,)).fetchone()[0]
    _snap(conn, pid, "2026-01-01", 28, "q1", 12.0, 3)
    db.set_opportunity_status(conn, oid, "done")
    conn.execute("UPDATE opportunities SET status_at='2026-01-02 00:00:00' WHERE id=?", (oid,))
    _snap(conn, pid, "2026-01-10", 28, "q1", 12.0, 3)
    _snap(conn, pid, "2026-01-20", 28, "q1", 13.0, 2)
    conn.commit()
    [w] = db.watch_rows(conn, pid)
    assert w["id"] == oid and w["done_at"].startswith("2026-01-02")
    assert w["before"]["position"] == 12.0 and w["after"]["position"] == 13.0
    assert w["runs_since"] == 2 and w["stalled"] is True
    conn.execute("UPDATE gsc_snapshots SET position=8.0 WHERE snapshot_date='2026-01-20'")
    conn.commit()
    assert db.watch_rows(conn, pid)[0]["stalled"] is False
    conn.close()
```

- [ ] **Step 2: 실패 확인** — `no such column: status_at` 또는 `watch_rows` 없음.

- [ ] **Step 3: 구현**

SCHEMA 의 `opportunities` 에 `status_at TEXT,` (created_at 앞). `_migrate` 끝에:

```python
    opp_cols = {r["name"] for r in conn.execute("PRAGMA table_info(opportunities)")}
    if "status_at" not in opp_cols:      # 완료 후 관찰의 기준 시각 (spec keyword-triage §3)
        conn.execute("ALTER TABLE opportunities ADD COLUMN status_at TEXT")
        conn.commit()
```

`set_opportunity_status` 의 두 UPDATE 를 `SET status=?, status_at=CURRENT_TIMESTAMP` 로.

```python
def watch_rows(conn, project_id: int) -> list[dict]:
    """완료된 검색어 기회의 전·후 — 상태를 늘리지 않고 조회로 관찰을 만든다."""
    import scoring
    ph = ",".join("?" * len(scoring.KEYWORD_KINDS))
    opps = conn.execute(
        f"SELECT id, kind, target, status_at FROM opportunities WHERE project_id=? AND status='done'"
        f" AND kind IN ({ph}) AND status_at IS NOT NULL ORDER BY status_at DESC LIMIT 100",
        (int(project_id), *scoring.KEYWORD_KINDS)).fetchall()
    if not opps:
        return []
    latest = conn.execute("SELECT snapshot_date, period_days FROM gsc_snapshots WHERE project_id=?"
                          " ORDER BY snapshot_date DESC LIMIT 1", (int(project_id),)).fetchone()
    if not latest:
        return [dict(id=o["id"], kind=o["kind"], target=o["target"], done_at=o["status_at"],
                     before=None, after=None, runs_since=0, stalled=False) for o in opps]
    per = latest["period_days"]

    def at(date_op, date_val, target):
        r = conn.execute(
            f"""SELECT SUM(clicks) c, AVG(position) p FROM gsc_snapshots
                 WHERE project_id=? AND period_days=? AND query=? AND snapshot_date {date_op} ?
                   AND snapshot_date=(SELECT MAX(snapshot_date) FROM gsc_snapshots
                        WHERE project_id=? AND period_days=? AND query=? AND snapshot_date {date_op} ?)""",
            (int(project_id), per, target, date_val, int(project_id), per, target, date_val)).fetchone()
        return {"clicks": int(r["c"] or 0), "position": round(float(r["p"]), 1)} if r and r["p"] is not None else None

    out = []
    for o in opps:
        day = o["status_at"][:10]
        before = at("<=", day, o["target"])
        after = at(">", day, o["target"])
        runs = conn.execute("SELECT COUNT(DISTINCT snapshot_date) FROM gsc_snapshots WHERE project_id=?"
                            " AND period_days=? AND snapshot_date>?", (int(project_id), per, day)).fetchone()[0]
        stalled = runs >= 2 and (after is None or before is None or after["position"] >= before["position"])
        out.append(dict(id=o["id"], kind=o["kind"], target=o["target"], done_at=o["status_at"],
                        before=before, after=after, runs_since=int(runs), stalled=bool(stalled)))
    return out
```

- [ ] **Step 4: 통과 확인** — `python test_capture.py`
- [ ] **Step 5: 커밋** — `feat(db): 기회 status_at 과 완료 후 관찰 조회 watch_rows`

---

### Task 4: `/api/triage` · `/api/verdict` 본체와 페이로드

**Files:**
- Modify: `skills/capture/scripts/dashboard.py` (`ROUTES`, `gather()` 의 `d`, 새 함수 둘)
- Modify: `server/app.py` (`/api/data` 옆에 래퍼 둘)
- Test: `skills/capture/scripts/test_dashboard.py`

**Interfaces:**
- Produces: `dashboard.triage_payload(project: str) -> dict` = `{"rows": [...], "counts": {"none": n, "irrelevant": n, "hold": n, "work": n}}`. row = `{key, label, variants, kinds: [kind...], labels: [한국어...], score, clicks, impressions, brand: bool, verdict: str|None}`. `label` 은 변형 중 점수가 가장 높은 원문. 정렬은 score 내림차순.
- Produces: `dashboard.set_verdict(body) -> {"updated": n}` — body `{project, keys, verdict}`; `verdict` 가 `None`/빈 문자열이면 되돌리기. `ValueError` 는 그대로 올린다(로컬 Handler 는 400 으로, 호스팅 래퍼는 HTTPException 400).
- Produces: `ROUTES[("GET","/api/triage")]`, `ROUTES[("POST","/api/verdict")]`.
- Produces: `gather()` 의 `d["watch"] = db.watch_rows(conn, pid)`, `d["keyword_kinds"] = list(scoring.KEYWORD_KINDS)`.

- [ ] **Step 1: 실패하는 검사** (`test_dashboard.py` 끝)

```python
def test_triage_payload_groups_variants_and_counts():
    conn, pid = _brain("tri")
    db.upsert_opportunities(conn, pid, None, [
        {"kind": "striking_distance", "target": "디아더피부과 가격", "score": 39.1},
        {"kind": "cannibalization", "target": "디아 더 피부과 가격", "score": 20.0},
        {"kind": "aio_exposure", "target": "레이저 토닝 후기", "score": 35.2},
        {"kind": "index_blocked", "target": "https://tri.example/x", "score": 10}])   # 심사 안 거침
    conn.execute("INSERT INTO gsc_snapshots(project_id,snapshot_date,period_days,query,clicks,impressions,ctr,position)"
                 " VALUES(?,?,28,'디아더피부과 가격',12,340,0.03,9.0)", (pid, D))
    conn.execute("UPDATE projects SET brand_aliases_json=? WHERE id=?", ('["디아더"]', pid)) \
        if "brand_aliases_json" in {r[1] for r in conn.execute("PRAGMA table_info(projects)")} else None
    conn.commit()
    conn.close()
    t = dashboard.triage_payload("tri")
    assert t["counts"] == {"none": 2, "irrelevant": 0, "hold": 0, "work": 0}, t["counts"]
    [a, b] = t["rows"]
    assert a["label"] == "디아더피부과 가격" and a["variants"] == 2
    assert set(a["kinds"]) == {"striking_distance", "cannibalization"} and a["score"] == 39.1
    assert a["clicks"] == 12 and a["impressions"] == 340
    assert b["verdict"] is None and b["clicks"] == 0
    assert dashboard.set_verdict({"project": "tri", "keys": [a["key"]], "verdict": "hold"}) == {"updated": 1}
    t = dashboard.triage_payload("tri")
    assert t["counts"]["hold"] == 1 and [r for r in t["rows"] if r["key"] == a["key"]][0]["verdict"] == "hold"
    try:
        dashboard.set_verdict({"project": "tri", "keys": [a["key"]], "verdict": "nope"})
        assert False
    except ValueError:
        pass
    assert ("GET", "/api/triage") in dashboard.ROUTES and ("POST", "/api/verdict") in dashboard.ROUTES
    conn, pid = db.connect(), db.get_project(db.connect(), "tri")["id"]
    d = dashboard.gather(conn, db.get_project(conn, "tri"))
    assert "watch" in d and d["keyword_kinds"] == list(scoring.KEYWORD_KINDS)
    conn.close()
```

브랜드 힌트는 프로젝트 yaml(`collector` 설정) 의 `name`/`brand_aliases` 에서 온다 — yaml 없는 검사 프로젝트에서는 `brand=False` 이므로 위 검사에서는 단언하지 않는다. `brand_aliases_json` 줄은 지운다(그 열은 없다).

- [ ] **Step 2: 실패 확인** — `AttributeError: 'triage_payload'`

- [ ] **Step 3: 구현** (`dashboard.py`, `set_opp_status` 뒤)

```python
def _brand_keys(conn, p) -> set[str]:
    """브랜드 힌트용 — 프로젝트 이름·별칭을 norm 한 것. yaml 이 없으면 이름만."""
    cfg = {}
    if p["config_path"]:
        try:
            cfg = db.load_project_yaml(p["config_path"])
        except (db.ProjectConfigNotFound, ImportError):
            pass
    return {scoring.norm(a) for a in scoring.aliases_of({**cfg, "name": cfg.get("name") or p["name"]}) if scoring.norm(a)}


def triage_payload(project: str) -> dict:
    """GET /api/triage — 열린 기회를 정규화한 검색어로 묶는다. 행 하나가 판정 단위다."""
    conn = db.connect()
    try:
        p = db.get_project(conn, project)
        pid = p["id"]
        vm = db.verdict_map(conn, pid)
        ph = ",".join("?" * len(scoring.KEYWORD_KINDS))
        rows = conn.execute(
            f"""SELECT norm(target) key, target, kind, score FROM opportunities
                 WHERE project_id=? AND status IN ('new','acked') AND kind IN ({ph})
                 ORDER BY score DESC, id""", (pid, *scoring.KEYWORD_KINDS)).fetchall()
        latest = conn.execute("SELECT MAX(snapshot_date) FROM gsc_snapshots WHERE project_id=?",
                              (pid,)).fetchone()[0]
        perf = {}
        if latest:
            for r in conn.execute("SELECT norm(query) k, SUM(clicks) c, SUM(impressions) i FROM gsc_snapshots"
                                  " WHERE project_id=? AND snapshot_date=? GROUP BY 1", (pid, latest)):
                perf[r["k"]] = (int(r["c"] or 0), int(r["i"] or 0))
        brands = _brand_keys(conn, p)
        groups: dict[str, dict] = {}
        for r in rows:
            g = groups.get(r["key"])
            if not g:
                c, i = perf.get(r["key"], (0, 0))
                g = groups[r["key"]] = {"key": r["key"], "label": r["target"], "variants": set(),
                                        "kinds": [], "score": r["score"], "clicks": c, "impressions": i,
                                        "brand": any(b and b in r["key"] for b in brands),
                                        "verdict": vm.get(r["key"])}
            g["variants"].add(r["target"])
            if r["kind"] not in g["kinds"]:
                g["kinds"].append(r["kind"])
        out = []
        for g in groups.values():
            g["variants"] = len(g["variants"])
            g["labels"] = [scoring.kind_label(k) for k in g["kinds"]]
            g["score"] = round(g["score"], 1) if g["score"] is not None else None
            out.append(g)
        out.sort(key=lambda g: -(g["score"] or 0))
        counts = {"none": 0, "irrelevant": 0, "hold": 0, "work": 0}
        for g in out:
            counts[g["verdict"] or "none"] += 1
        return {"rows": out, "counts": counts}
    finally:
        conn.close()


def set_verdict(body: dict) -> dict:
    """POST /api/verdict 본체 — 로컬·호스팅이 같은 함수를 부른다. 값 검증은 db.set_verdicts."""
    conn = db.connect()
    try:
        pid = db.get_project(conn, str(body.get("project") or ""))["id"]
        v = body.get("verdict") or None
        return {"updated": db.set_verdicts(conn, pid, list(body.get("keys") or []), v)}
    finally:
        conn.close()
```

`ROUTES` 에:

```python
    ("GET", "/api/triage"):
        lambda project, query, body: triage_payload(project),
    ("POST", "/api/verdict"):
        lambda project, query, body: set_verdict(body),
```

로컬 `Handler.do_POST` 가 `ValueError` 를 400 으로 돌리는지 확인한다 — 안 하면 `except ValueError as e: return self._json({"error": str(e)}, 400)` 를 `/api/opp` 와 같은 자리에 넣는다.

`gather()` 의 `d` 에 `"watch": db.watch_rows(conn, pid), "keyword_kinds": list(scoring.KEYWORD_KINDS),`.

`server/app.py` `/api/data` 뒤:

```python
@app.get("/api/triage")
def api_triage(project: str, request: Request):
    """검색어 심사 — 본체는 dashboard.ROUTES 것(로컬과 같은 함수)."""
    uid = _require_uid(request)
    try:
        with store.session(uid, project, isolate=True):
            return dashboard.ROUTES[("GET", "/api/triage")](project, {}, None)
    except db.ProjectNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.post("/api/verdict")
async def api_verdict(request: Request):
    uid = _require_uid(request)
    body = await request.json()
    project = str(body.get("project") or "")
    try:
        with store.session(uid, project, isolate=True):
            return dashboard.ROUTES[("POST", "/api/verdict")](project, {}, body)
    except ValueError:
        raise HTTPException(status_code=400, detail="알아볼 수 없는 판정값입니다. 새로고침한 뒤 다시 시도하세요.")
    except db.ProjectNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
```

- [ ] **Step 4: 통과 확인** — `python test_dashboard.py`, `python test_seams.py`(5번이 새 경로를 양쪽에서 찾는다), `python server/app.py`(demo).
- [ ] **Step 5: 커밋** — `feat(dashboard): /api/triage · /api/verdict — 심사 페이로드와 판정 저장`

---

### Task 5: 화면 [심사]

**Files:**
- Create: `skills/capture/templates/views/triage.html`
- Modify: `skills/capture/scripts/dashboard.py:45` (`VIEW_ORDER` 맨 앞에 `"triage"`)
- Modify: `skills/capture/templates/dashboard.html:2993` (박제본 `SM.drop("triage")`), 아이콘표에 `triage` 항목
- Test: `skills/capture/scripts/test_render.py` (픽스처 + MUSTS)

**Interfaces:**
- Consumes: `GET /api/triage?project=`, `POST /api/verdict` (Task 4), `d.keyword_kinds`.
- Produces: 전역 이름은 전부 `TR_` 접두 (조립본 최상위 이름 충돌 검사).

- [ ] **Step 1: 렌더 검사 픽스처** — `test_render.py` `_axes()` 끝에 심사용 기회를 심고 MUSTS 에 표식을 더한다:

```python
TRIAGE_KW = "심사검색어Z9"          # 심사 화면의 첫 줄에만 나오는 검색어
# _axes() 안:
    conn.execute("INSERT INTO opportunities(project_id,kind,target,score,status)"
                 " VALUES(?,?,?,?, 'new')", (pid, "striking_distance", TRIAGE_KW, 77))
    conn.execute("INSERT INTO opportunities(project_id,kind,target,score,status)"
                 " VALUES(?,?,?,?, 'new')", (pid, "aio_exposure", TRIAGE_KW + " ", 40))   # 변형
# MUSTS 에:
    (r'<tr class="trrow"[^>]*data-key="[^"]+"[^>]*>(?:(?!</tr>).)*' + re.escape(TRIAGE_KW) + r'(?:(?!</tr>).)*\(\+1\)',
     "심사 화면이 검색어를 한 줄로 묶어 안 그렸다(변형 +1)"),
    (r'id="tr-counts"[^>]*>[^<]*미판정 <b>1</b>', "심사 화면 상단 카운트가 안 나왔다"),
```

`python test_render.py` → FAIL (뷰가 없다).

- [ ] **Step 2: 뷰 파일** — `views/triage.html`:

```html
<!-- 검색어 심사 — 기회를 보기 전에 검색어를 거른다. 정본: docs/superpowers/specs/2026-09-08-keyword-triage-design.md -->
<script type="application/json" class="view-def">
{"id": "triage", "title": "심사",
 "sub": "측정이 물어온 검색어가 우리 것인지 먼저 가립니다. 작업으로 보낸 것만 개요의 기회가 됩니다.",
 "sections": ["triage"], "stages": ["gaps"]}
</script>

<style>
/* 심사 표 — 행 하나가 판정 단위(정규화한 검색어)다. 체크 열은 좁게, 검색어 열이 넓게. */
#triage .trtable td:first-child, #triage .trtable th:first-child { width:28px; padding-right:6px; }
#triage .trrow.cur td { background:var(--wash); }
#triage .trrow:hover td { background:var(--bar); }
#triage .trkw { font-weight:500; }
#triage .trvar { color:var(--slate); font-size:12px; margin-left:6px; }
#triage .trkinds { display:flex; flex-wrap:wrap; gap:4px; }
#triage .trkinds .kind { font:var(--micro); }
#triage .trbrand { color:var(--copper); border-color:var(--copper); }
/* 일괄 판정 바 — 고른 것이 있을 때만 선다. 판정 버튼 셋은 셸의 .go 와 같은 높이. */
#triage .trbar { display:flex; align-items:center; gap:10px; flex-wrap:wrap; margin:0 0 12px;
  min-height:var(--btn-h); }
#triage .trbar .n { color:var(--slate); font-size:13px; }
#triage .trbar kbd { font:400 11px/1 var(--mono); color:var(--slate); border:1px solid var(--rule);
  border-radius:3px; padding:2px 4px; margin-left:4px; }
#triage .trcounts { color:var(--slate); font-size:13px; }
#triage .trcounts b { color:var(--ink); font-weight:600; }
</style>

<section id="triage-sec">
  <div class="sh">
    <p class="eyebrow">검색어 심사</p>
    <h2>미판정 검색어</h2>
    <p class="sub" id="tr-counts"></p>
    <div class="tools" id="tr-tools"></div>
  </div>
  <div id="triage"></div>
</section>

<script>
"use strict";
/* 상태: 서버가 준 행(TR_ROWS), 보는 판정(TR_FILTER: ""=미판정 | irrelevant | hold | work),
   종류 거르개, 찾는 말, 고른 키, 키보드 커서. */
let TR_ROWS = [], TR_FILTER = "", TR_KIND = "", TR_Q = "", TR_SEL = new Set(), TR_CUR = 0, TR_PROJECT = "";
const TR_VERDICT = {irrelevant: "무관", hold: "보류", work: "작업"};
const TR_FILTER_OPT = [["", "미판정"], ["work", "작업"], ["hold", "보류"], ["irrelevant", "무관"]];
const TR_DONE = {irrelevant: "무관으로 뺐습니다.", hold: "보류했습니다.", work: "작업으로 보냈습니다.",
                 "": "미판정으로 되돌렸습니다."};

VIEW("triage", function (d) {
  TR_PROJECT = (d.project && d.project.name) || "";
  TR_SEL = new Set(); TR_CUR = 0;
  TR_fetch();
});

async function TR_fetch() {
  if (READONLY || !TR_PROJECT) return;
  const res = await fetch("/api/triage?project=" + encodeURIComponent(TR_PROJECT));
  if (!res.ok) { $("triage").innerHTML = `<div class="empty err"><b>심사 목록을 불러오지 못했습니다</b></div>`; return; }
  const t = await res.json();
  TR_ROWS = t.rows || [];
  TR_counts(t.counts || {});
  TR_tools();
  TR_draw();
}

function TR_counts(c) {
  $("tr-counts").innerHTML = `<span class="trcounts">미판정 <b>${c.none || 0}</b> · 무관 <b>${c.irrelevant || 0}</b>` +
    ` · 보류 <b>${c.hold || 0}</b> · 작업 <b>${c.work || 0}</b></span>`;
}

function TR_filtered() {
  const q = TR_Q.trim().toLowerCase();
  return TR_ROWS.filter(r => (r.verdict || "") === TR_FILTER)
    .filter(r => !TR_KIND || r.kinds.indexOf(TR_KIND) >= 0)
    .filter(r => !q || r.label.toLowerCase().includes(q));
}

function TR_tools() {
  const kinds = {};
  for (const r of TR_ROWS.filter(r => (r.verdict || "") === TR_FILTER)) for (const k of r.kinds) kinds[k] = (kinds[k] || 0) + 1;
  $("tr-tools").innerHTML =
    `<select onchange="TR_setFilter(this.value)" aria-label="판정으로 거르기">${TR_FILTER_OPT.map(([v, l]) =>
        `<option value="${v}"${v === TR_FILTER ? " selected" : ""}>${l}</option>`).join("")}</select>
     <select onchange="TR_setKind(this.value)" aria-label="종류로 거르기"><option value="">모든 종류</option>${Object.keys(kinds).map(k =>
        `<option value="${esc(k)}"${k === TR_KIND ? " selected" : ""}>${esc((window.KIND_LABELS || {})[k] || k)} (${kinds[k]})</option>`).join("")}</select>
     <input type="search" value="${esc(TR_Q)}" oninput="TR_find(this.value)" placeholder="검색어로 찾기" aria-label="검색어로 찾기">`;
}
function TR_setFilter(v) { TR_FILTER = v; TR_KIND = ""; TR_SEL = new Set(); TR_CUR = 0; TR_tools(); TR_draw(); }
function TR_setKind(v) { TR_KIND = v; TR_CUR = 0; TR_draw(); }
function TR_find(v) { TR_Q = v; TR_CUR = 0; TR_draw(); }

function TR_row(r, i) {
  const on = TR_SEL.has(r.key);
  return `<tr class="trrow${i === TR_CUR ? " cur" : ""}" data-key="${esc(r.key)}" data-i="${i}">
    <td><input type="checkbox"${on ? " checked" : ""} onchange="TR_toggle('${esc(r.key)}', this.checked)" aria-label="${esc(r.label)} 고르기"></td>
    <td><span class="trkw">${esc(r.label)}</span>${r.variants > 1 ? `<span class="trvar" title="띄어쓰기·대소문자만 다른 변형 ${r.variants - 1}개를 한 줄로 묶었습니다">(+${r.variants - 1})</span>` : ""}
      ${r.brand ? `<span class="st trbrand" title="사이트 이름·브랜드 표기가 들어 있는 검색어입니다">브랜드</span>` : ""}</td>
    <td><div class="trkinds">${r.kinds.map((k, j) => `<span class="kind" title="${esc(k)}">${esc(r.labels[j] || k)}</span>`).join("")}</div></td>
    <td class="num">${r.score == null ? "—" : Math.round(r.score)}</td>
    <td class="num">${num(r.clicks)} / ${num(r.impressions)}</td>
    <td>${TR_FILTER ? `<button class="go sm sec" onclick="TR_set(['${esc(r.key)}'], '')">미판정으로 되돌리기</button>` : ""}</td>
  </tr>`;
}

function TR_draw() {
  const list = TR_filtered();
  if (!TR_ROWS.length)
    return $("triage").innerHTML = `<div class="empty"><b>심사할 검색어가 없습니다</b>
      <p>기회 분석을 돌리면 측정이 물어온 검색어가 여기로 옵니다.</p></div>`;
  if (!list.length)
    return $("triage").innerHTML = TR_FILTER || TR_KIND || TR_Q.trim()
      ? `<div class="empty"><b>거른 조건에 맞는 검색어가 없습니다</b></div>`
      : `<div class="empty ok"><b>미판정 검색어가 없습니다</b>
         <p>전부 가렸습니다. 작업으로 보낸 것은 ${window.go ? go("overview", "[개요]") : "[개요]"}의 기회에 있습니다.</p></div>`;
  if (TR_CUR >= list.length) TR_CUR = list.length - 1;
  const n = TR_SEL.size;
  const bar = READONLY ? "" : `<div class="trbar">
      <label><input type="checkbox" onchange="TR_all(this.checked)"${n && n === list.length ? " checked" : ""} aria-label="전부 고르기"> 전부</label>
      <span class="n">${n ? `선택 ${n}건 →` : `줄을 고르거나 <kbd>j</kbd><kbd>k</kbd>로 옮겨 <kbd>1</kbd><kbd>2</kbd><kbd>3</kbd>`}</span>
      <button class="go sm sec" ${n ? "" : "disabled"} onclick="TR_setSel('irrelevant')">무관<kbd>1</kbd></button>
      <button class="go sm sec" ${n ? "" : "disabled"} onclick="TR_setSel('hold')">보류<kbd>2</kbd></button>
      <button class="go sm" ${n ? "" : "disabled"} onclick="TR_setSel('work')">작업<kbd>3</kbd></button>
    </div>`;
  $("triage").innerHTML = bar + table(
    ["", "검색어?띄어쓰기만 다른 변형은 한 줄로 묶습니다", "종류?이 검색어에 걸린 진단",
     ">점수?걸린 기회 중 가장 높은 점수", ">클릭 / 노출?최근 수집분의 서치콘솔 실적", ""],
    list.map(TR_row), "");
  $("triage").querySelector("table") && $("triage").querySelector("table").classList.add("trtable");
}

function TR_toggle(key, on) { on ? TR_SEL.add(key) : TR_SEL.delete(key); TR_draw(); }
function TR_all(on) { TR_SEL = new Set(on ? TR_filtered().map(r => r.key) : []); TR_draw(); }
function TR_setSel(v) { if (TR_SEL.size) TR_set([...TR_SEL], v); }

/* 판정 저장. 실패하면 행을 그대로 두고 말한다(거짓 성공을 그리지 않는다). */
async function TR_set(keys, verdict) {
  const [ok, r] = await post("/api/verdict", {project: TR_PROJECT, keys, verdict: verdict || null});
  if (!ok) return toast((r && (r.error || r.detail)) || "판정을 저장하지 못했습니다. [새로고침] 뒤 다시 하면 됩니다.", "bad");
  toast(TR_DONE[verdict || ""] + ` (${keys.length}건)`, "ok");
  TR_SEL = new Set();
  await TR_fetch();
  // 작업으로 보낸 것은 기회 목록에 들어간다 — 개요가 다음 적재 때 같이 본다.
  if (verdict === "work" || verdict === "") load();
}

/* 키보드 — 이 화면이 보일 때, 입력칸 밖에서만. j/k 이동, 스페이스 고르기, 1/2/3 판정. */
document.addEventListener("keydown", e => {
  if (!window.SM || SM.cur !== "triage" || READONLY) return;
  if (/^(INPUT|SELECT|TEXTAREA)$/.test(e.target.tagName)) return;
  const list = TR_filtered();
  if (!list.length) return;
  const cur = list[TR_CUR];
  if (e.key === "j") { TR_CUR = Math.min(list.length - 1, TR_CUR + 1); TR_draw(); }
  else if (e.key === "k") { TR_CUR = Math.max(0, TR_CUR - 1); TR_draw(); }
  else if (e.key === " ") { e.preventDefault(); TR_toggle(cur.key, !TR_SEL.has(cur.key)); }
  else if (e.key === "1" || e.key === "2" || e.key === "3") {
    const v = {1: "irrelevant", 2: "hold", 3: "work"}[e.key];
    TR_set(TR_SEL.size ? [...TR_SEL] : [cur.key], v);
  } else return;
  const row = $("triage").querySelector(`.trrow[data-i="${TR_CUR}"]`);
  if (row) row.scrollIntoView({block: "nearest"});
});
</script>
```

`num()` 이 셸에 있는지 확인한다(overview 가 쓴다). `table()` 의 빈 문자열 empty 는 목록이 비어서 부를 일이 없다.

- [ ] **Step 3: 조립·박제본** — `VIEW_ORDER = ["triage", "overview", ...]`. 셸 아이콘표(`competitors:` 등이 있는 객체)에 `triage: \`<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M3 4.5h10M3 8h7M3 11.5h4"/><path d="M11 10.5l1.5 1.5L15 9"/></svg>\``. 박제본 분기(`SM.drop("settings")` 옆)에 `SM.drop("triage");   // 서버가 없으면 판정을 저장할 수 없다`. `test_render.py` 의 `REPORT_DROPPED = ("settings", "guide", "triage")`.

- [ ] **Step 4: 검사** — `python dashboard.py --selfcheck`, `python run_checks.py --check-dashboard-js` 대신 `python run_checks.py dashboard`, `python test_render.py`, `python test_seams.py`. 실패하면 고친다.

- [ ] **Step 5: 브라우저** — 로컬 대시보드를 다시 띄우고(포트 8765, 새 토큰) Orca 탭에서 [심사] → 줄 고르기 → [보류] → 카운트 변화 → 필터 "보류" → [미판정으로 되돌리기] → [작업] → [개요] 기회 목록에 그 검색어가 뜨는지. 키보드 `j`/`3` 도 한 번. 스크린샷을 남긴다.

- [ ] **Step 6: 커밋** — `feat(dashboard): [심사] 화면 — 검색어를 무관·보류·작업으로 일괄 판정`

---

### Task 6: 기회 상태 라벨·전이·작업 목록·완료 후 관찰

**Files:**
- Modify: `skills/capture/templates/dashboard.html:1802` (`OPP_LABEL`), `:1863-1866` (`OPP_NEXT`/`OPP_SET`), `:2217` (`OPP_DONE`), `oppDetail` 끝(작업 목록), `render()` 에 `window.CREATIONS`
- Modify: `skills/capture/templates/views/overview.html:197` (`ST_OPT`), view-def `sections` 에 `"watch"`, 새 섹션 마크업·렌더
- Modify: `server/app.py:520` (`done` → `acked`)
- Modify: `skills/create/scripts/createdb.py:72` (`done` → `acked`), `skills/create/scripts/test_createdb.py:64,99`, `skills/create/SKILL.md` 의 그 문장
- Test: `skills/capture/scripts/test_seams.py` (17번)

**Interfaces:**
- Consumes: `d.watch` (Task 4), `d.creations`(있음).
- Produces: 셸 전역 `window.OPP_LABEL = {new:"할 일", acked:"진행 중", done:"완료", dismissed:"이 기회만 뺌"}`, `OPP_NEXT = {new:["acked","작업 시작"], acked:["done","완료 표시"], done:["new","다시 열기"], dismissed:["new","다시 열기"]}`, `OPP_SET = {acked:"작업 시작", done:"완료 표시", dismissed:"이 기회만 빼기", new:"다시 열기"}`, `OPP_DONE = {acked:"진행 중으로 옮겼습니다.", done:"완료로 옮겼습니다.", dismissed:"이 기회만 뺐습니다.", new:"다시 열었습니다."}`.

- [ ] **Step 1: 이음매 검사 17** (`test_seams.py`, 16 뒤)

```python
def test_seam_17_verdict_and_status_single_source():
    """17) 심사·상태 이음매.
    - 판정 값은 db.VERDICTS 한 벌: 화면(triage.html)의 TR_VERDICT 키와 같다.
    - 정규화는 서버의 scoring.norm 하나: 뷰·셸 JS 에 norm 사본(정규식으로 낱자를
      지우는 식)이 없고, 화면은 서버가 준 key 를 그대로 돌려보낸다.
    - 상태 값은 db.OPP_STATUSES 한 벌: 셸의 OPP_LABEL·OPP_NEXT·OPP_SET 키가 양방향으로 같다.
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
    assert set(re.findall(r"(\w+):", m.group(1))) == set(db.VERDICTS), "판정 값이 db.VERDICTS 와 다르다"
    for src, who in ((tr, "triage.html"), (shell, "dashboard.html")):
        assert "0-9a-z가-힣" not in src, f"{who} 에 norm 사본이 있다 — 정규화는 scoring.norm 하나다"
    for name in ("OPP_LABEL", "OPP_NEXT", "OPP_SET"):
        mm = re.search(name + r" = \{(.*?)\};", shell, re.S)
        assert mm, f"셸의 {name} 을 못 찾았다"
        keys = set(re.findall(r"(\w+):", mm.group(1)))
        assert keys == set(db.OPP_STATUSES), f"{name} 의 키가 db.OPP_STATUSES 와 어긋났다: {keys ^ set(db.OPP_STATUSES)}"
    assert set(scoring.KEYWORD_KINDS) < set(scoring.ALL_KINDS)
    assert "삭제" not in re.search(r"OPP_SET = \{(.*?)\};", shell, re.S).group(1)
```

돌려서 통과하는지 본다(`triage.html` 이 있으니 통과할 것). 그 다음 **일부러 깨기**: `OPP_LABEL` 에 `watching:"관찰"` 을 잠깐 넣고 FAIL 을 보고 되돌린다.

- [ ] **Step 2: 셸 라벨·전이** — 위 Interfaces 값으로 바꾼다. `OPP_NEXT` 의 주석(“접힌 줄에는 다음 하나만”)은 유지. `oppRest` 는 그대로(dismissed 가 펼침 패널로 내려간다 — 이미 그 구조다).

- [ ] **Step 3: 작업 목록** — `render()` 에서 `window.KIND_LABELS = ...` 옆에 `window.CREATIONS = d.creations || [];`. `oppDetail` 의 `return` 직전에:

```js
  const made = (window.CREATIONS || []).filter(c => c.opportunity_id === o.id);
  const works = made.length ? `<div class="detw"><b>이 기회로 만든 것 ${made.length}건</b>
    <ul class="detl">${made.map(c => `<li><span class="mono">${esc(c.file_path)}</span>` +
      `${c.branch ? ` · ${esc(c.branch)}` : ""} · ${c.merged ? "머지됨" : "PR 열림"}` +
      `${c.note && /^https?:/.test(c.note) ? ` · <a href="${esc(c.note)}" target="_blank" rel="noopener">보기</a>` : ""}</li>`).join("")}</ul></div>` : "";
```

그리고 `${ask}${oppRest(o)}` 앞에 `${works}`. `.detl` 스타일이 없으면 `.det .detl { margin:6px 0 0 18px; padding:0; font-size:13px; }` 를 셸 CSS 의 `.detw` 옆에 둔다.

- [ ] **Step 4: 개요 필터·관찰 섹션** — `ST_OPT = [["", "전체"], ["open", "아직 안 함"], ["new", "할 일"], ["acked", "진행 중"], ["closed", "완료·뺀 것"]]`. view-def `"sections": ["docban", "band", "opps", "watch", "daily"]`. 기회 섹션 뒤에:

```html
<section id="watch-sec">
  <div class="sh">
    <p class="eyebrow">완료 후 관찰</p>
    <h2>고친 뒤 달라졌나</h2>
    <p class="sub">완료로 옮긴 검색어의 그때와 지금입니다. 두 번 재도 안 오르면 다시 엽니다.</p>
  </div>
  <div id="watch"></div>
</section>
```

overview 스크립트에:

```js
function OV_watch(d) {
  const rows = d.watch || [];
  if (!rows.length) return $("watch").innerHTML =
    `<div class="empty"><b>완료한 검색어가 아직 없습니다</b><p>기회를 완료로 옮기면 여기서 전·후를 봅니다.</p></div>`;
  const cell = m => m ? `${m.position}위 · 클릭 ${num(m.clicks)}` : "—";
  $("watch").innerHTML = table(
    ["검색어", "완료일", "그때?완료 직전 수집분", "지금?최근 수집분", ">그 뒤 측정", ""],
    rows.map(w => `<tr${w.stalled ? ' class="stalled"' : ""}><td>${esc(w.target)}</td>
      <td class="mono">${esc((w.done_at || "").slice(0, 10))}</td><td>${cell(w.before)}</td><td>${cell(w.after)}</td>
      <td class="num">${w.runs_since}회</td>
      <td>${w.stalled && !READONLY ? `<button class="go sm sec" onclick="setOpp(${w.id},'new')">다시 열기</button>` : ""}</td></tr>`), "");
}
```

`VIEW("overview", ...)` 렌더 함수 안에서 `OV_watch(d)` 를 부른다. `.mono` 클래스가 셸에 있는지 확인(없으면 `font-family:var(--mono)` 인 기존 클래스 이름으로).

- [ ] **Step 5: PR 뒤 자동 완료 → 진행 중** — `server/app.py:520` `"done"` → `"acked"` + 주석 “완료는 관찰까지 보고 사람이 누른다(spec keyword-triage §3)”. `createdb.py` `_mark_done` 의 `"done"` → `"acked"`, 함수 이름은 그대로 두되 docstring 에 이유. `test_createdb.py:64,99` 의 `== "done"` → `== "acked"` 와 문구. `skills/create/SKILL.md` 에서 `done` 으로 닫힌다고 말하는 문장을 “진행 중(acked)으로 두고 완료는 대시보드에서 관찰 뒤 누른다”로. `README.md`/`docs/copy-guide.md` 에 "확인 표시"·"목록에서 빼기" 문구가 있으면 같이 고친다(grep).

- [ ] **Step 6: 검사** — `python run_checks.py` 전부. `python skills/create/scripts/test_createdb.py`.

- [ ] **Step 7: 브라우저** — Orca 로 [개요] → 기회 펼침 → 접힌 줄 버튼이 "작업 시작"인지 → 누르고 배지 "진행 중" → 펼침 패널 안 "이 기회만 빼기" → "완료 표시" → "완료 후 관찰" 표에 뜨는지(픽스처 사이트 기준).

- [ ] **Step 8: 커밋** — `feat(opps): 상태 라벨·전이 정리 — 작업 시작/진행 중/다시 열기, 완료 후 관찰, 작업 목록; PR 뒤 자동 완료를 진행 중으로`

---

### Task 7: 마무리 — 버전·문서·전체 검사

**Files:**
- Modify: `.claude-plugin/plugin.json` (minor 범프), `README.md` 명령표 옆 한 줄(심사 설명), `skills/capture/SKILL.md` 의 대시보드 절에 심사 한 문단.

- [ ] **Step 1:** `python run_checks.py` 전부 PASS 인지. `python run_checks.py remote` 는 원격 미연결이면 SKIP.
- [ ] **Step 2:** 문서 한 줄씩. 개수·목록은 정본을 가리키기만 한다(`scoring.KEYWORD_KINDS`, `db.VERDICTS`).
- [ ] **Step 3:** 커밋 `chore(release): v1.78.0 — 검색어 심사`. push 는 사용자가 정한다(CLAUDE.md 0번 다리: 호스팅 런 확인 뒤).
