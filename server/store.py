"""서버 저장소 — 유저·구글 토큰·테넌트 격리.

수집 엔진(skills/capture/scripts)은 CAPTURE_HOME / GSC_TOKEN_FILE 을 호출할 때마다
다시 읽는다(db.py __getattr__). 그래서 테넌트 격리는 env 를 갈아끼우는 것으로 끝난다 —
엔진 코드는 한 줄도 손대지 않는다.

Env 는 settings.py 가 소유한다 — 여기서 기본값을 정하지 않는다.
(SEOMINER_DATA, SEOMINER_SECRET_KEY)
"""
from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

from cryptography.fernet import Fernet
from fastapi import HTTPException

import settings


def data_dir() -> Path:
    return Path(settings.get("SEOMINER_DATA"))


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY,
  email TEXT UNIQUE NOT NULL,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  last_seen_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS google_tokens (
  user_id INTEGER PRIMARY KEY REFERENCES users(id),
  token_enc BLOB NOT NULL,
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS cli_tokens (
  user_id INTEGER PRIMARY KEY REFERENCES users(id),
  token_hash TEXT NOT NULL,         -- sha256 hex. 위 token_enc 들과 달리 복호화가 없다
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS sites (
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id),
  project TEXT NOT NULL,              -- capture 프로젝트 이름 (테넌트 안에서만 유일)
  gsc_property TEXT NOT NULL,         -- sc-domain:example.com
  domain TEXT NOT NULL,
  active INTEGER DEFAULT 1,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  last_run_at TEXT,                   -- NULL = 아직 한 번도 안 잼 (등록 직후)
  running_since TEXT,                 -- NULL 이 아니면 지금 수집 중
  -- repo* 셋은 떼어 낸 GitHub 연동의 잔재다. 아무도 읽고 쓰지 않지만 열은 남긴다 —
  -- 지우려면 SQLite 에서 표를 새로 만들어 옮겨야 하고, 그 마이그레이션이 이득보다
  -- 위험하다(spec 2026-09-08-run-tool-from-dashboard §4). 되살리지 마라.
  repo TEXT,
  repo_branch TEXT,
  repo_profile TEXT,
  run_every_hours REAL,               -- 이 사이트의 재측정 주기(시간). NULL = 전역 기본값, 0 = 자동 끔
  stage TEXT,                         -- 지금 도는 단계 id (run_all.STAGES 의 이름)
  stage_pct INTEGER,                  -- 그 시점의 진행률 0~100 (끝난 단계 / 전체 단계)
  run_log TEXT,                       -- 이번 런의 화면 출력 (워커 stdout+stderr). 런당 1벌
  last_ok INTEGER,                    -- 마지막 런의 결과. 1=전부 성공, 0=실패한 단계 있음, NULL=아직
  last_error TEXT,                    -- 그 실패의 사람 말 (단계명: 이유; …)
  run_groups TEXT,                    -- 지금 도는 런이 맡은 묶음 id (쉼표). run_phase() 참조
  UNIQUE(user_id, project)
);
-- 묶음별 시계와 대기열. 묶음의 정본은 run_all.GROUPS 다 — 여기는 id 만 적는다.
-- 행이 없으면 시계는 sites.last_run_at 을 쓴다(group_clocks): 묶음 시계가 생기기 전에
-- 이미 잰 사이트가 배포 직후 다섯 묶음을 한꺼번에 '밀렸다'로 보고 유료 런을 사지 않게.
-- 그 빌려 쓰기는 **한 번뿐**이다 — 묶음 하나라도 찍기 전에 전 묶음의 행을 그 값으로
-- 심는다(_seed_clocks). 안 심으면 sites.last_run_at 이 매일 런마다 새로 찍혀 행 없는
-- 묶음이 그 시계를 빌려 영영 안 밀린다.
CREATE TABLE IF NOT EXISTS site_groups (
  site_id INTEGER NOT NULL REFERENCES sites(id),
  grp TEXT NOT NULL,                  -- run_all.GROUPS 의 id
  last_run_at TEXT,                   -- 이 묶음을 마지막으로 재기 시작한 시각 (UTC)
  queued_at TEXT,                     -- 대기열에 오른 시각. NULL = 대기 없음
  PRIMARY KEY (site_id, grp)
);
"""

# 프로세스째 죽은 런에 남기는 문구. 로그(사용자가 보는 것)와 last_error(배너가 보는 것)를
# 한 벌로 둔다 — 두 벌이면 한쪽만 낡는다.
DEAD_RUN_NOTE = "\n[오류] 서버 재시작으로 수집이 중단됐습니다 — 자동으로 다시 시작합니다.\n"
DEAD_RUN_ERROR = "서버 재시작으로 수집이 중단됐습니다 — 자동으로 다시 시작합니다."
# 부분 실행(단계 몇 개만)이 죽었을 때 — 무엇을 돌던 중이었는지 남지 않으므로(run_groups='')
# 자동으로 다시 띄우지 않는다. 띄우면 인자 없는 워커 = 전체 재기(유료 단계 전부)가 된다.
DEAD_PARTIAL_NOTE = "\n[오류] 서버 재시작으로 수집이 중단됐습니다 — 필요하면 다시 실행하세요.\n"
DEAD_PARTIAL_ERROR = "서버 재시작으로 수집이 중단됐습니다 — 필요하면 다시 실행하세요."
# 도는 중(running_since)이 이만큼 지나면 죽은 런으로 본다 — due_sites 와 claim_site 가
# 같은 값을 쓴다. 살아 있는 런은 라운드마다 mark_groups 가 시각을 새로 찍는다.
DEAD_AFTER_HOURS = 3


def _migrate(conn: sqlite3.Connection) -> None:
    """CREATE TABLE IF NOT EXISTS 가 못 하는 것만 — 이미 있는 테이블의 새 컬럼."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(sites)")}
    if not cols:
        return
    for col in ("last_run_at", "running_since", "repo", "repo_branch", "repo_profile",
                "stage", "run_log", "last_error", "run_groups"):
        if col not in cols:
            conn.execute(f"ALTER TABLE sites ADD COLUMN {col} TEXT")
    # 숫자로 비교한다 — TEXT 로 두면 SQLite 가 '0' > 0 을 참으로 봐서 '끔'이 안 먹는다.
    if "run_every_hours" not in cols:
        conn.execute("ALTER TABLE sites ADD COLUMN run_every_hours REAL")
    for col in ("stage_pct", "last_ok"):
        if col not in cols:
            conn.execute(f"ALTER TABLE sites ADD COLUMN {col} INTEGER")
    conn.commit()


def _fernet() -> Fernet:
    # settings.Missing 은 RuntimeError 다 — 이 함수가 RuntimeError 를 던지던 계약 그대로.
    return Fernet(settings.get("SEOMINER_SECRET_KEY"))


def connect() -> sqlite3.Connection:
    d = data_dir()
    d.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False — FastAPI 는 sync 의존자(yield)와 sync 라우트를 스레드풀의
    # 서로 다른 스레드에서 돌린다. 의존자가 연 커넥션을 라우트가 다른 스레드에서 쓰면
    # ProgrammingError → 500 이다(동시 요청에서만 난다 — 순차면 같은 스레드를 재사용해
    # 우연히 맞는다). 커넥션은 호출마다 새로 열고 한 요청만 쓰므로 스레드를 순서대로
    # 넘겨받을 뿐 동시에 쓰이지 않는다 — 그래서 이 검사를 풀어도 안전하다.
    conn = sqlite3.connect(d / "server.db", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


# --- 유저 -------------------------------------------------------------------

def upsert_user(conn: sqlite3.Connection, email: str) -> int:
    conn.execute("INSERT INTO users(email) VALUES (?) ON CONFLICT(email) DO UPDATE "
                 "SET last_seen_at=CURRENT_TIMESTAMP", (email,))
    conn.commit()
    return conn.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()["id"]


# --- 구글 토큰 --------------------------------------------------------------

def save_token(conn: sqlite3.Connection, user_id: int, token_json: str) -> None:
    conn.execute(
        "INSERT INTO google_tokens(user_id, token_enc) VALUES (?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET token_enc=excluded.token_enc, "
        "updated_at=CURRENT_TIMESTAMP",
        (user_id, _fernet().encrypt(token_json.encode("utf-8"))))
    conn.commit()


def load_token(conn: sqlite3.Connection, user_id: int) -> str | None:
    row = conn.execute("SELECT token_enc FROM google_tokens WHERE user_id=?",
                       (user_id,)).fetchone()
    return _fernet().decrypt(row["token_enc"]).decode("utf-8") if row else None


# --- CLI 토큰 ---------------------------------------------------------------
#
# 위의 google_tokens 는 **암호화**다. 남의 API 를 다시 부르려면 원문이
# 필요하기 때문이다. 이건 성격이 다르다 — 서버가 스스로 발급한 값이라 원문을 되찾을
# 일이 영영 없고 대조만 하면 된다. 그래서 sha256 해시로 둔다(복호화할 수 없는 쪽이
# 더 안전하다). Fernet 을 여기 끌어오지 마라.

def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def issue_cli_token(conn: sqlite3.Connection, user_id: int) -> str:
    """CLI 원격 조작용 토큰을 발급하고 **원문을 여기서 딱 한 번** 돌려준다.

    유저당 1행이다(INSERT OR REPLACE) — 재발급하면 이전 토큰은 그 자리에서 무효다.
    돌려준 원문은 호출자가 화면에 한 번 보여 주고 버린다. 로그에 찍지 않는다.
    """
    token = "smt_" + secrets.token_urlsafe(32)
    conn.execute("INSERT OR REPLACE INTO cli_tokens(user_id, token_hash, created_at) "
                 "VALUES (?,?,CURRENT_TIMESTAMP)", (user_id, _token_hash(token)))
    conn.commit()
    return token


def uid_for_cli_token(conn: sqlite3.Connection, token: str) -> int | None:
    """토큰 원문 → user_id. 모르는 토큰이면 None (호출자가 401 로 옮긴다)."""
    if not token:
        return None
    row = conn.execute("SELECT user_id FROM cli_tokens WHERE token_hash=?",
                       (_token_hash(token),)).fetchone()
    return row["user_id"] if row else None


# --- 사이트 -----------------------------------------------------------------

def add_site(conn: sqlite3.Connection, user_id: int, project: str,
             gsc_property: str, domain: str) -> int:
    conn.execute(
        "INSERT INTO sites(user_id, project, gsc_property, domain) VALUES (?,?,?,?) "
        "ON CONFLICT(user_id, project) DO UPDATE SET gsc_property=excluded.gsc_property, "
        "domain=excluded.domain, active=1",
        (user_id, project, gsc_property, domain))
    conn.commit()
    return conn.execute("SELECT id FROM sites WHERE user_id=? AND project=?",
                        (user_id, project)).fetchone()["id"]


def sites(conn: sqlite3.Connection, user_id: int) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM sites WHERE user_id=? AND active=1 ORDER BY id",
                        (user_id,)).fetchall()


def _groups():
    """묶음 표의 정본 — run_all.GROUPS. 늦게 읽는다: capture 스크립트 경로는 부르는 쪽
    (app.py/worker.py)이 세우고, 이 모듈은 그 전에도 import 된다. 혼자 불리는 자리
    (scheduler.py 의 demo 처럼 경로를 안 세운 쪽)를 위해 없으면 여기서 세운다."""
    import sys
    scripts = str(Path(__file__).resolve().parent.parent / "skills" / "capture" / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    import run_all
    return run_all


def _utc(ts: str | None):
    """저장된 시각(CURRENT_TIMESTAMP 꼴이든 ISO 'T…Z' 꼴이든) → naive UTC datetime."""
    from datetime import datetime
    if not ts:
        return None
    return datetime.strptime(str(ts)[:19].replace("T", " "), "%Y-%m-%d %H:%M:%S")


def group_period(row, gid: str, every_hours: float) -> float | None:
    """이 사이트에서 이 묶음의 자동 주기(시간). None = 시계 없는 묶음, 0 = 자동 끔.

    run_every_hours(사이트 설정, 없으면 전역)는 **주간 묶음 주기**다 — 묶음 표에서
    every_hours 가 WEEKLY_HOURS 인 묶음(검색 성과·AI 노출·사이트 건강)이 이걸 따르고,
    매일 런(24h)과 경쟁·링크(720h)는 제 주기 그대로다. 0 은 예전 뜻 그대로 **자동 재기
    전부 끔**이다. 전역 0 은 비상 브레이크라 사이트별 값을 이긴다(due_sites 주석).
    """
    ra = _groups()
    base = ra.GROUP_BY_ID[gid]["every_hours"]
    if base is None:
        return None
    if every_hours <= 0:
        return 0.0
    w = row["run_every_hours"]
    w = float(w) if w is not None else float(every_hours)
    if w <= 0:
        return 0.0
    return w if base == ra.WEEKLY_HOURS else float(base)


def _seed_clocks(conn: sqlite3.Connection, site_id: int) -> None:
    """행이 없는 묶음에 sites.last_run_at 을 **한 번** 심는다 — 커밋은 부르는 쪽이 한다.

    mark_groups 가 sites.last_run_at 을 매 런 새로 찍기 **전에** 불러야 한다. 안 그러면
    행 없는 묶음(배포 전에 잰 옛 사이트, 또는 첫 런이 묶음 하나였던 새 사이트)이 매일
    런이 찍은 시계를 빌려 영영 안 밀린다 — 순위·AI·크롤·백링크가 다시는 스스로 안 돈다.
    새 사이트(last_run_at NULL)면 NULL 이 심긴다 = 한 번도 안 잰 묶음.
    """
    for g in _groups().RUNNABLE_GROUPS:
        conn.execute("INSERT OR IGNORE INTO site_groups(site_id, grp, last_run_at) "
                     "SELECT id, ?, last_run_at FROM sites WHERE id=?", (g, site_id))


def group_clocks(conn: sqlite3.Connection, site_row) -> dict[str, str | None]:
    """{묶음: 마지막으로 재기 시작한 시각}. 행이 없는 묶음은 sites.last_run_at 을 쓴다 —
    묶음 시계가 생기기 전에 잰 사이트(표 주석). 첫 묶음 런이 그 값을 행으로 심으므로
    (_seed_clocks) 빌려 쓰는 값은 배포 전 마지막 전체 런의 시각 그대로다."""
    rows = {r["grp"]: r["last_run_at"] for r in conn.execute(
        "SELECT grp, last_run_at FROM site_groups WHERE site_id=?", (site_row["id"],))}
    return {g: (rows[g] if g in rows else site_row["last_run_at"])
            for g in _groups().RUNNABLE_GROUPS}


def due_groups(conn: sqlite3.Connection, site_row, every_hours: float = 168.0) -> list[str]:
    """이 사이트에서 주기를 넘긴 묶음들(GROUPS 순서).

    한 번도 안 잰 사이트(시계가 전부 비었다)는 자동이 꺼져 있어도 전부다 — 등록 직후의
    첫 측정은 끄는 대상이 아니다(예전 0 의 뜻 그대로). 그 밖에 자동이 꺼졌으면(0) 없다.
    """
    from datetime import datetime, timezone
    clocks = group_clocks(conn, site_row)
    if all(v is None for v in clocks.values()):
        return list(clocks)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    out = []
    for g, last in clocks.items():
        per = group_period(site_row, g, every_hours)
        if not per:
            continue
        # 나이(시간)로 비교한다 — now - timedelta(hours=per) 는 주기가 아주 크면(1e9) 넘친다.
        if last is None or (now - _utc(last)).total_seconds() / 3600 >= per:
            out.append(g)
    return out


def due_sites(conn: sqlite3.Connection, idle_days: int = 30,
              every_hours: float = 168.0) -> list[sqlite3.Row]:
    """지금 잴 사이트 — 휴면 계정·도는 중인 사이트를 빼고, 주기를 넘긴 묶음이 있거나
    대기열에 묶음이 걸린 사이트.

    주기는 반드시 사이트별로 본다. 전역 스탬프 하나로 판정하면 먼저 등록한 사이트가
    도장을 찍어 버려서, 방금 가입한 사람의 첫 측정이 다음 주기까지 통째로 밀린다 —
    그 사람은 빈 대시보드만 보고 떠난다. 이제는 묶음마다 본다(due_groups): 매일 런은
    하루, 주간 묶음은 run_every_hours, 경쟁·링크는 한 달.

    0 = '자동 재측정 끔'. 아직 한 번도 안 잰 사이트와 대기열(사람이 누른 것)까지 막으면
    등록도 '지금 재기'도 죽으므로, 0 이어도 그 둘은 통과한다.

    전역 0 과 사이트별 0 은 뜻이 다르다. 사이트별 0 은 그 사이트만 끄지만, **전역 0 은
    배포 전체의 비상 브레이크라 사이트별 값을 이긴다** — SERP·AI 는 실행당 과금이라
    운영자가 비용을 멈추려고 0 을 넣었는데 주기를 지정해 둔 사이트가 계속 돌면
    브레이크가 아니다. 설정 설명("0 이면 자동 수집을 끈다")도 그 뜻이다.

    도는 중(running_since)이 3시간을 넘으면 죽은 런으로 보고 다시 잡는다.
    """
    rows = conn.execute(
        "SELECT s.*, u.email FROM sites s JOIN users u ON u.id = s.user_id "
        "WHERE s.active=1 AND u.last_seen_at > datetime('now', :idle) "
        "  AND (s.running_since IS NULL OR s.running_since <= datetime('now', :dead)) "
        "ORDER BY s.last_run_at IS NOT NULL, s.id",   # 첫 측정 대기자를 먼저
        {"idle": f"-{idle_days} days", "dead": f"-{DEAD_AFTER_HOURS} hours"}).fetchall()
    return [r for r in rows if queued(conn, r["id"]) or due_groups(conn, r, every_hours)]


# --- 묶음 대기열 -----------------------------------------------------------
#
# 도는 중에 다른 묶음을 누르면 대기열에 오르고, 지금 런이 끝나면 워커가 이어서 돈다
# (worker.run_site 의 라운드). 아직 워커가 대기열을 가져가기 전(pending, 몇 초)에 연달아
# 누른 것은 한 런으로 합쳐진다 — 워커가 뜨자마자 가져가지 않고 MERGE_SECONDS 를 기다린다.
#
#   idle     running_since IS NULL
#   pending  running_since 가 있고 run_groups IS NULL — 눌렀고 워커가 아직 안 가져감
#   running  run_groups 가 있다 (부분 실행은 빈 문자열 '')

def run_phase(row) -> str:
    """'idle' | 'pending' | 'running' — 위 표. 판정 자리는 여기 하나다."""
    if not row["running_since"]:
        return "idle"
    return "pending" if row["run_groups"] is None else "running"


def queue_groups(conn: sqlite3.Connection, site_id: int, groups) -> None:
    """묶음을 대기열에 올린다. 이미 올라 있으면 그대로(먼저 누른 시각을 지킨다).
    행을 새로 만들기 전에 시계를 심는다 — 대기열 행이 시계 없는(NULL) 행으로 먼저 서면
    옛 사이트의 그 묶음이 '한 번도 안 잰 것'이 된다."""
    _seed_clocks(conn, site_id)
    for g in groups:
        conn.execute(
            "INSERT INTO site_groups(site_id, grp, queued_at) VALUES (?,?,CURRENT_TIMESTAMP) "
            "ON CONFLICT(site_id, grp) DO UPDATE SET "
            "queued_at=COALESCE(site_groups.queued_at, CURRENT_TIMESTAMP)", (site_id, g))
    conn.commit()


def queued(conn: sqlite3.Connection, site_id: int) -> list[str]:
    q = {r["grp"] for r in conn.execute(
        "SELECT grp FROM site_groups WHERE site_id=? AND queued_at IS NOT NULL", (site_id,))}
    return [g for g in _groups().RUNNABLE_GROUPS if g in q]


def claim_queue(conn: sqlite3.Connection, site_id: int) -> list[str]:
    """대기열을 비우며 가져간다 — 가져간 쪽이 그 런의 주인이 된다(phase → running).

    읽기와 비우기를 한 트랜잭션에 묶는다(BEGIN IMMEDIATE): 두 워커가 같은 대기열을
    읽으면 같은 묶음을 두 번 산다.
    """
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        got = queued(conn, site_id)
        if got:
            conn.execute("UPDATE site_groups SET queued_at=NULL WHERE site_id=?", (site_id,))
            conn.execute("UPDATE sites SET run_groups=?, "
                         "running_since=COALESCE(running_since, CURRENT_TIMESTAMP) WHERE id=?",
                         (",".join(got), site_id))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return got


def claim_site(conn: sqlite3.Connection, site_id: int) -> sqlite3.Row | None:
    """스윕(run_all_due)이 사이트를 **원자적으로** 잡는다 — 반환: 새로 읽은 행, 못 잡으면 None.

    due_sites 는 스윕을 시작할 때 한 번 읽은 목록이다. 앞 사이트를 몇 분 도는 사이에
    사람이 이 사이트의 버튼을 눌러 워커(--queue)가 떴거나 부분 실행(mark_busy)이 돌기
    시작했으면, 그 목록만 믿고 돌면 한 사이트를 두 워커가 같이 돈다 — 유료 단계가 겹치고,
    먼저 끝난 쪽의 mark_done 이 남이 도는 런을 '끝남'으로 지운다.

    잡는 조건은 due_sites 와 같다: 안 돌거나, 도는 중이 DEAD_AFTER_HOURS 를 넘긴 죽은 런.
    죽은 런을 넘겨받으면 (1) 그 런이 맡았던 묶음을 대기열로 되돌리고(reclaim_dead_runs 와
    같은 이유 — 시계는 시작할 때 찍혔다), (2) running_since 를 지금으로 새로 찍는다 —
    옛 시각을 물려받으면 새 런이 처음부터 '죽은 런'으로 보여 또 넘겨받힌다.
    run_groups 는 '' — 도는 중(running)이다. 맡은 묶음은 곧 mark_groups 가 적는다.
    """
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT *, (running_since IS NOT NULL AND running_since > datetime('now', ?)) AS live "
            "FROM sites WHERE id=?", (f"-{DEAD_AFTER_HOURS} hours", site_id)).fetchone()
        if row is None or row["live"]:
            conn.commit()
            return None
        if row["running_since"]:
            _seed_clocks(conn, site_id)
            for g in [x for x in (row["run_groups"] or "").split(",") if x]:
                conn.execute(
                    "INSERT INTO site_groups(site_id, grp, queued_at) VALUES (?,?,CURRENT_TIMESTAMP) "
                    "ON CONFLICT(site_id, grp) DO UPDATE SET "
                    "queued_at=COALESCE(site_groups.queued_at, CURRENT_TIMESTAMP)", (site_id, g))
        conn.execute("UPDATE sites SET running_since=CURRENT_TIMESTAMP, run_groups='', "
                     "stage=NULL, stage_pct=0 WHERE id=?", (site_id,))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return conn.execute("SELECT * FROM sites WHERE id=?", (site_id,)).fetchone()


def mark_pending(conn: sqlite3.Connection, site_id: int) -> bool:
    """눌렀다 — 워커를 띄우기 전에 '도는 중'으로 잡는다. 이미 돌거나 대기 중이면 False.

    여기서 잡아야 몇 초 안에 또 누른 것이 워커를 하나 더 띄우지 않고 대기열에 합쳐진다.
    스케줄러(due_sites)도 도는 중인 사이트는 안 잡는다.
    """
    cur = conn.execute("UPDATE sites SET running_since=CURRENT_TIMESTAMP, run_groups=NULL, "
                       "stage=NULL, stage_pct=NULL WHERE id=? AND running_since IS NULL",
                       (site_id,))
    conn.commit()
    return cur.rowcount > 0


def mark_groups(conn: sqlite3.Connection, site_id: int, groups) -> None:
    """묶음 런 시작 — 도는 중을 켜고 **그 묶음들의 시계만** 찍는다.

    시작할 때 찍는다(끝나고가 아니라): 실패한 묶음이 매 틱마다 재시도하면 비용이 샌다
    (mark_run 과 같은 이유). 런이 프로세스째 죽으면 run_groups 가 남아 있어 회수가 그
    묶음들을 대기열로 되돌린다(reclaim_dead_runs) — 시계를 찍었다고 안 잰 채로 넘어가지
    않는다. sites.last_run_at 도 같이 찍는다: '마지막으로 뭔가 잰 때'라 화면과 첫 측정
    판정이 읽는다.

    running_since 도 **새로** 찍는다 — 이 함수를 부르는 것은 그 런의 주인(워커의 start)
    뿐이고, 라운드마다 불린다. 옛 시각을 물려받으면(COALESCE) 라운드가 이어진 런이나
    죽은 런을 넘겨받은 런이 DEAD_AFTER_HOURS 를 넘겨 '죽은 런'으로 보이고, 스윕이 같은
    사이트를 또 잡는다.
    """
    groups = list(groups)
    _seed_clocks(conn, site_id)          # sites.last_run_at 을 새로 찍기 전에
    for g in groups:
        conn.execute(
            "INSERT INTO site_groups(site_id, grp, last_run_at) VALUES (?,?,CURRENT_TIMESTAMP) "
            "ON CONFLICT(site_id, grp) DO UPDATE SET last_run_at=CURRENT_TIMESTAMP",
            (site_id, g))
    conn.execute("UPDATE sites SET last_run_at=CURRENT_TIMESTAMP, "
                 "running_since=CURRENT_TIMESTAMP, "
                 "run_groups=?, stage=NULL, stage_pct=0 WHERE id=?",
                 (",".join(groups), site_id))
    conn.commit()


def clear_pending(conn: sqlite3.Connection, site_id: int) -> None:
    """가져갈 대기열이 없는데 pending 으로 남은 자리를 푼다. 남이 이미 가져가 도는 중이면
    (running) 건드리지 않는다 — 그 런의 표시를 끄면 화면이 거짓말한다."""
    conn.execute("UPDATE sites SET running_since=NULL, stage=NULL, stage_pct=NULL "
                 "WHERE id=? AND running_since IS NOT NULL AND run_groups IS NULL", (site_id,))
    conn.commit()


def group_status(conn: sqlite3.Connection, site_row, every_hours: float) -> dict:
    """/api/run/status 의 "groups" — {묶음: {running, queued, last_run_at, due}}.
    시계 없는 묶음(관리)은 싣지 않는다.

    대기 중(pending — 눌렀고 워커가 합치는 몇 초를 기다린다)에 올라 있는 묶음은 **도는
    중**이다: 앞에 도는 런이 없으니 곧 선다. 그걸 queued 로 내면 화면이 방금 시작한
    묶음을 "지금 도는 재기가 끝나면 잽니다"라고 말한다 — 기다릴 런이 없는데. 로컬
    (dashboard._LocalRuns.status)도 같은 규칙이다. queued 는 running 인 런 뒤에 선 것만.
    """
    clocks = group_clocks(conn, site_row)
    due = set(due_groups(conn, site_row, every_hours))
    q = set(queued(conn, site_row["id"]))
    phase = run_phase(site_row)
    if phase == "pending":
        busy, q = q, set()
    elif phase == "running":
        busy = set((site_row["run_groups"] or "").split(","))
    else:
        busy = set()
    return {g: {"running": g in busy, "queued": g in q,
                "last_run_at": clocks[g], "due": g in due}
            for g in clocks}


def mark_run(conn: sqlite3.Connection, site_id: int) -> None:
    """수집 시작 표시(전체 재기). 성공·실패 무관하게 찍는다 — 실패한 사이트가 매 틱마다
    재시도하면 비용이 샌다. 묶음 시계까지 전부 찍는다 — 전체 재기는 모든 묶음을 잰다."""
    mark_groups(conn, site_id, _groups().RUNNABLE_GROUPS)


def mark_busy(conn: sqlite3.Connection, site_id: int) -> None:
    """부분 실행(단계 몇 개만) 시작 표시 — 도는 중만 켜고 주기 시계(last_run_at)는 안 건드린다.

    예전엔 부분 실행도 mark_run 을 불러 last_run_at 을 찍었다. 그러면 gsc 만 다시 읽어도
    "전체 재측정을 방금 했다"가 되어 주 1회 전체 런이 매번 뒤로 밀렸다 — theotherskin 은
    9/11·9/15·9/18 부분 실행만 돌고 순위 조회·페이지 감사가 3주 동안 한 번도 안 돌았다
    (요청문 237장이 경쟁 상위 글 없이 나갔다). 바로 위 주석은 내내 반대를 말하고 있었다.
    묶음 시계도 안 찍는다. run_groups 는 빈 문자열 — 대기 중이 아니라 도는 중이다."""
    conn.execute("UPDATE sites SET running_since=CURRENT_TIMESTAMP, stage=NULL, stage_pct=0, "
                 "run_groups='' WHERE id=?", (site_id,))
    conn.commit()


def mark_stage(conn: sqlite3.Connection, site_id: int, stage: str, pct: int) -> None:
    """지금 도는 단계와 진행률. 화면이 '분석 중…' 대신 몇 %인지 말할 수 있게 하는 값이다.
    묶음 런은 단계 여럿이 동시에 돈다 — stage 에 쉼표로 이어 싣는다(status 가 편다)."""
    conn.execute("UPDATE sites SET stage=?, stage_pct=? WHERE id=?",
                 (stage, max(0, min(100, int(pct))), site_id))
    conn.commit()


def mark_done(conn: sqlite3.Connection, site_id: int, *,
              ok: bool | None = None, error: str = "") -> None:
    """수집 종료 표시. ok 를 주면 결과까지 같이 굳힌다.

    결과를 여기 붙이는 이유: 런이 끝나는 자리는 이 한 곳뿐이다(워커의 finally, 그리고
    죽은 런을 회수하는 reclaim_dead_runs). 따로 두면 한쪽 경로가 결과를 안 남겨서
    화면이 "마지막 수집 성공"이라고 거짓말한다. ok=None 은 '결과는 건드리지 마라' —
    옛 호출부(진행률만 끄는 자리)가 마지막 런의 성패를 지우면 안 된다.
    """
    if ok is None:
        conn.execute("UPDATE sites SET running_since=NULL, stage=NULL, stage_pct=NULL, "
                     "run_groups=NULL WHERE id=?", (site_id,))
    else:
        conn.execute("UPDATE sites SET running_since=NULL, stage=NULL, stage_pct=NULL, "
                     "run_groups=NULL, last_ok=?, last_error=? WHERE id=?",
                     (1 if ok else 0, (error or "")[:2000] or None, site_id))
    conn.commit()


def reclaim_dead_runs(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """프로세스째 죽은 런을 회수한다 — 반환: 회수한 사이트 행들.

    워커의 finally 는 프로세스가 살아 있을 때만 돈다. Railway 는 push 마다 컨테이너를
    갈아치우므로 수집 도중 SIGKILL 이 정상 경로다. 그러면 running_since 가 남아
    (1) 화면이 3시간 동안 "분석 중 38%" 로 고정되고(due_sites 의 -3시간 회수 규칙),
    (2) request_run 이 running_since IS NULL 을 요구해 수동 재실행까지 막히고,
    (3) 사용자에게는 아무 말도 안 간다. 서버가 뜨는 자리에서 그 셋을 한 번에 푼다.

    재개는 호출자(app.lifespan)가 한다 — 저장소는 무엇이 죽었는지만 말한다.

    사이트 brain 의 끝나지 않은 runs 행도 여기서 닫는다(close_orphan_runs). 서버 행만
    풀고 brain 을 두면 "수집 이력"이 영영 "도는 중"이고, 호스팅 런 검사
    (test_remote 의 체인 검사)가 finished_at IS NULL 을 실패로 본다 — 09-02 순위
    수집 #49 가 그렇게 남았다.

    죽은 묶음 런이 맡았던 묶음(run_groups)은 대기열로 되돌린다 — 그 묶음들의 시계는
    시작할 때 이미 찍혔으므로(mark_groups), 되돌리지 않으면 안 잰 채로 한 주를 넘긴다.
    워커가 아직 안 가져간 대기열(pending)은 site_groups 에 그대로 남아 있다.
    """
    rows = conn.execute("SELECT * FROM sites WHERE running_since IS NOT NULL").fetchall()
    for r in rows:
        lost = [g for g in (r["run_groups"] or "").split(",") if g]
        if lost:
            queue_groups(conn, r["id"], lost)
        # 다시 띄울 것(대기열)이 있을 때만 "자동으로 다시 시작합니다"라고 말한다 — 부분
        # 실행은 무엇을 돌던지 안 남아 다시 안 띄운다(app.resume_dead_runs).
        again = bool(queued(conn, r["id"]))
        conn.execute("UPDATE sites SET run_log = COALESCE(run_log,'') || ? WHERE id=?",
                     (DEAD_RUN_NOTE if again else DEAD_PARTIAL_NOTE, r["id"]))
        mark_done(conn, r["id"], ok=False,
                  error=DEAD_RUN_ERROR if again else DEAD_PARTIAL_ERROR)
    for c in close_orphan_runs(conn):
        print(f"[reclaim] 끝나지 않은 런 {c['closed']}건 닫음: "
              f"{c['user_id']}/{c['project']}", flush=True)
    return rows


# 워커가 죽어 끝나지 않은 채 남은 brain runs 행에 붙이는 표식. db.run() 이 예외로
# 끝난 런에 붙이는 "중단: …" 과 같은 꼴이다 — 화면과 검사가 같은 말로 읽는다.
ORPHAN_RUN_NOTE = "중단: 워커가 죽었다(서버가 회수)"


def close_orphan_runs(conn: sqlite3.Connection) -> list[dict]:
    """서버가 '안 돈다'고 판정한 사이트의 끝나지 않은 brain runs 행을 닫는다.

    반환: [{user_id, project, closed}] — 한 건이라도 닫은 사이트만.

    **도는 런은 절대 닫지 않는다.** 판정은 두 겹이다:
    (1) 서버 DB 의 sites.running_since IS NULL 인 사이트만 본다. brain 에 runs 행을
        쓰는 것은 워커뿐이고 워커는 체인을 시작하기 **전에** mark_run 으로
        running_since 를 켠다 — 그래서 NULL 이면 그 사이트에 살아 있는 작성자가 없다.
        (라우트는 runs 행을 쓰지 않는다.)
    (2) 사이트 목록을 읽기 **전에** 찍은 시각(cutoff)보다 먼저 시작한 행만 닫는다.
        목록을 읽은 직후 워커가 떠서 mark_run → start_run 을 했다면 그 행은 cutoff
        이후에 시작했으므로 안 걸린다. (1) 과 (2) 사이의 틈을 막는 것이 이 줄이다.

    회수된 사이트(방금 reclaim)뿐 아니라 오래전에 남은 고아(#49)도 같은 규칙으로
    한 번에 정리된다 — 둘 다 "안 도는 사이트의 끝나지 않은 행"이다. 재실행에
    안전하다(이미 닫힌 행은 다시 안 걸린다).

    brain 을 새로 만들지 않는다 — 파일이 없거나 표가 없으면 그 사이트는 건너뛴다.
    """
    import paths    # 지연 import — capture 스크립트 경로는 호출자(app.py/worker.py)가 세워 뒀다
    from datetime import datetime, timezone
    # db.now() 와 같은 꼴(UTC, 초 단위). 비교는 julianday 로 한다 — 옛 행이
    # CURRENT_TIMESTAMP 꼴('YYYY-MM-DD HH:MM:SS')이어도 문자열 비교처럼 틀리지 않는다.
    cutoff = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    idle = conn.execute("SELECT user_id, project FROM sites "
                        "WHERE running_since IS NULL ORDER BY id").fetchall()
    out: list[dict] = []
    for s in idle:
        f = paths.db_file(home(s["user_id"]))
        if not f.exists():
            continue
        b = sqlite3.connect(f)
        try:
            cur = b.execute(
                "UPDATE runs SET finished_at=?, notes=CASE WHEN COALESCE(notes,'')='' THEN ? "
                "  ELSE notes || ' | ' || ? END "
                "WHERE finished_at IS NULL AND julianday(started_at) < julianday(?) "
                "  AND project_id IN (SELECT id FROM projects WHERE name=?)",
                (cutoff, ORPHAN_RUN_NOTE, ORPHAN_RUN_NOTE, cutoff, s["project"]))
            b.commit()
            if cur.rowcount > 0:
                out.append({"user_id": s["user_id"], "project": s["project"],
                            "closed": cur.rowcount})
        except sqlite3.OperationalError:       # runs/projects 표가 아직 없는 brain
            pass
        finally:
            b.close()
    return out


def save_run_log(conn: sqlite3.Connection, site_id: int, text: str) -> None:
    """이번 런의 워커 stdout. 로컬이 폴링으로 흘려받는 그 텍스트다.

    ponytail: 런당 1벌만 보관. 이력이 필요해지면 runs 테이블로.
    """
    conn.execute("UPDATE sites SET run_log=? WHERE id=?", (text, site_id))
    conn.commit()


def load_run_log(conn: sqlite3.Connection, user_id: int, project: str) -> str:
    """user_id 로 범위를 좁혀 읽는다 — project 이름만으로 남의 런 로그가 열리면 안 된다."""
    row = site(conn, user_id, project)
    return (row["run_log"] or "") if row else ""


def site(conn: sqlite3.Connection, user_id: int, project: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM sites WHERE user_id=? AND project=? AND active=1",
                        (user_id, project)).fetchone()


def every_hours(conn: sqlite3.Connection, user_id: int, project: str) -> float:
    """이 사이트에 실제로 적용되는 재측정 주기 — 값이 없으면 전역 기본값.
    due_sites 의 COALESCE 와 같은 규칙이다. 두 곳이 다른 답을 하면 화면이 거짓말한다."""
    row = site(conn, user_id, project)
    v = row["run_every_hours"] if row else None
    return float(v) if v is not None else settings.num("SEOMINER_RUN_EVERY_HOURS")


def set_every_hours(conn: sqlite3.Connection, user_id: int, project: str,
                    hours: float) -> bool:
    """사이트별 재측정 주기를 정한다. 0 이면 자동만 끈다(첫 측정·수동 실행은 그대로).
    범위를 user_id 로 좁힌다 — project 이름만으로 남의 사이트를 고칠 수 있으면 안 된다."""
    cur = conn.execute("UPDATE sites SET run_every_hours=? WHERE user_id=? AND project=?",
                       (float(hours), user_id, project))
    conn.commit()
    return cur.rowcount > 0


def request_run(conn: sqlite3.Connection, user_id: int, project: str) -> bool:
    """'지금 다시 재기'(전체) — 모든 묶음을 대기열에 올린다. 이미 도는 중이면 무시.

    예전엔 last_run_at 을 NULL 로 지워 '첫 측정'인 척 다음 스윕에 걸리게 했다. 묶음 시계가
    생긴 뒤로는 그 흉내가 안 먹는다(묶음 시계는 그대로다) — 누른 것은 대기열이 정본이다.
    """
    row = site(conn, user_id, project)
    if not row or row["running_since"]:
        return False
    queue_groups(conn, row["id"], _groups().RUNNABLE_GROUPS)
    return True


# --- 테넌트 -----------------------------------------------------------------

def home(user_id: int) -> Path:
    return data_dir() / "users" / str(user_id)


class Tenant:
    """유저 한 명의 home 에 묶인 손잡이 — session(isolate=True) 이 이걸 내준다.

    .conn 은 서버 DB(store.* 가 쓰는 것, tenant() 를 부른 쪽 것 그대로),
    .home 은 그 유저의 CAPTURE_HOME, .brain() 은 **그 home 의** brain.db 연결이다.
    라우트가 db.connect() 를 손으로 열던 자리를 t.brain() 으로 바꾸면, isolate 플래그를
    빠뜨리거나 db.connect() 를 깜빡해도 엉뚱한(서버 기본) brain 을 열 수 없다 — home
    이 이 객체에 이미 못 박혀 있기 때문이다.
    """

    def __init__(self, uid: int, home: Path, conn: sqlite3.Connection):
        self.uid = uid
        self.home = home
        self.conn = conn

    def brain(self) -> sqlite3.Connection:
        """이 유저의 brain.db 연결. env(CAPTURE_HOME) 를 안 봐도 항상 이 home 을 연다."""
        import db  # 지연 import — capture 스크립트 경로는 호출자(app.py/worker.py)가 이미 세워 뒀다
        return db.connect(home=self.home)

    @contextmanager
    def activate(self, token_file: Path | None = None):
        """엔진(subprocess 포함)이 보는 CAPTURE_HOME/GSC_TOKEN_FILE 을 이 home 으로
        갈아끼운다. worker.run_site 처럼 env 만 보는 코드를 위한 것 — t.brain() 을
        쓰는 라우트는 이게 필요 없다."""
        keys = ("CAPTURE_HOME",) if token_file is None else ("CAPTURE_HOME", "GSC_TOKEN_FILE")
        saved = {k: os.environ.get(k) for k in keys}
        os.environ["CAPTURE_HOME"] = str(self.home)
        if token_file is not None:
            os.environ["GSC_TOKEN_FILE"] = str(token_file)
        try:
            yield self
        finally:
            for k, v in saved.items():
                os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)


@contextmanager
def tenant(conn: sqlite3.Connection, user_id: int):
    """유저 한 명의 env 로 갈아끼우고, 그 home 에 묶인 Tenant 를 내준다.

    엔진(run_chain 같은 subprocess 포함)은 env 만 보고 돈다 — 그 스왑은
    Tenant.activate() 가 한다. 나갈 때 토큰 파일을 회수해 DB 에 되쓴다 —
    collect_gsc 가 갱신한 access token 이 거기 담기므로(collect_gsc.py:74),
    안 거두면 매 실행마다 refresh 왕복을 한다.

    ponytail: os.environ 은 프로세스 전역이라 동시 진입에 안전하지 않다. 워커가
    직렬로 돌기 때문에 지금은 문제없다. 병렬이 필요해지면 유저별 subprocess 로 바꾼다.

    토큰 파일 이름은 **컨텍스트마다 다르다**. 한 이름(gsc_token.json)을 공유하던
    시절엔 워커가 GSC 를 수집하는 몇 분 사이에 들어온 화면 요청이 같은 파일을 쓰고,
    나갈 때 그것을 지웠다 — 아직 도는 워커가 토큰을 잃거나, 뒤늦게 지우려던 쪽이
    FileNotFoundError 로 500 을 냈다(/api/data). 프로세스도 스레드도 자기 파일만
    만들고 자기 파일만 지운다.
    """
    h = home(user_id)
    h.mkdir(parents=True, exist_ok=True)
    tok = h / f"gsc_token.{os.getpid()}.{threading.get_ident()}.json"
    # 옛 고정 이름으로 남은 평문 토큰이 있으면 여기서 치운다(죽은 런의 잔해).
    (h / "gsc_token.json").unlink(missing_ok=True)

    stored = load_token(conn, user_id)
    if stored:
        tok.write_text(stored, "utf-8")

    t = Tenant(user_id, h, conn)
    try:
        with t.activate(tok):
            yield t
    finally:
        if tok.exists():
            save_token(conn, user_id, tok.read_text("utf-8"))
        tok.unlink(missing_ok=True)           # 평문 토큰을 디스크에 남기지 않는다


_TENANT_LOCK = threading.Lock()


@contextmanager
def session(uid: int, project: str | None = None, *, own: bool = True,
            isolate: bool = False, paid: bool = False):
    """라우트 하나가 필요로 하는 걸 한 번에: conn 열기 → (project 를 주면) 소유
    확인 → (isolate 면) tenant 두르기 → (paid 면) 유료 키까지 → 끝나면 정리.

    conn = store.connect(); try: ... finally: conn.close() 와 _own() 과
    with store.tenant() 가 라우트마다 손으로 반복되던 것을 한 곳으로 모은다.

    isolate=False 면 `as` 로 받는 건 그냥 서버 conn 이다. isolate=True 면 Tenant
    (t.home, t.brain(), t.conn=서버 conn) 다 — 라우트는 db.connect() 를 손으로
    열지 않고 t.brain() 으로 그 유저의 brain 만 연다.

    project 를 주면 소유 확인이 **기본값**이다(own=True) — 빠뜨림이 기본이면 안
    된다. own=False 는 아직 소유가 성립하지 않는 자리에만 쓴다(예: 새 사이트 등록).
    """
    conn = connect()
    try:
        if project is not None and own and not any(
                r["project"] == project for r in sites(conn, uid)):
            raise HTTPException(
                status_code=404, detail="찾을 수 없는 사이트입니다. 사이트 목록에서 다시 선택해 주세요.")
        if isolate:
            # ponytail: 프로세스 전역 락 — 테넌트 요청을 한 줄로 세운다. 화면은 /api/data·
            # /api/triage·/api/doctor 를 동시에 부르는데, tenant() 가 갈아끼우는 env
            # (GSC_TOKEN_FILE)는 전역이라 겹치면 먼저 나간 요청이 남의 토큰 파일을 지워
            # 호스팅 안내가 "구글 로그인 대기"로 틀렸다(병렬 12건 중 3건). threading.Lock
            # 인 이유: FastAPI 는 yield 의존자의 들어가기·나가기를 다른 스레드에서 돌릴 수
            # 있다(RLock 은 거기서 못 푼다). 느려지면 유저별 subprocess 로(tenant() 주석).
            _TENANT_LOCK.acquire()
            try:
                with tenant(conn, uid) as t:
                    if paid:
                        with settings.paid_keys():
                            yield t
                    else:
                        yield t
            finally:
                _TENANT_LOCK.release()
        else:
            yield conn
    finally:
        conn.close()


def demo() -> None:
    import sys
    import tempfile
    # close_orphan_runs 가 paths 를 늦게 부른다 — app.py/worker.py 가 세우는 경로를 여기서도.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "skills" / "capture" / "scripts"))
    with tempfile.TemporaryDirectory() as d:
        os.environ["SEOMINER_DATA"] = d
        # 필수 설정이 없으면 조용히 굴러가지 않고 RuntimeError 다 (settings.Missing).
        os.environ.pop("SEOMINER_SECRET_KEY", None)
        try:
            _fernet()
            raise AssertionError("암호화 키 없이 통과했다")
        except RuntimeError as e:
            assert "SEOMINER_SECRET_KEY" in str(e), e
        os.environ["SEOMINER_SECRET_KEY"] = Fernet.generate_key().decode()
        assert data_dir() == Path(d), "설정을 호출 시점에 안 읽는다"
        conn = connect()

        uid = upsert_user(conn, "a@example.com")
        assert upsert_user(conn, "a@example.com") == uid, "같은 이메일은 같은 유저"

        save_token(conn, uid, '{"refresh_token":"secret"}')
        assert load_token(conn, uid) == '{"refresh_token":"secret"}'
        raw = conn.execute("SELECT token_enc FROM google_tokens").fetchone()["token_enc"]
        assert b"secret" not in raw, "토큰이 평문으로 저장됐다"

        before = os.environ.get("CAPTURE_HOME")
        with tenant(conn, uid) as t0:
            assert isinstance(t0, Tenant) and t0.uid == uid and t0.conn is conn
            assert t0.home == home(uid)
            assert os.environ["CAPTURE_HOME"] == str(t0.home)
            assert Path(os.environ["GSC_TOKEN_FILE"]).read_text("utf-8") == '{"refresh_token":"secret"}'
            Path(os.environ["GSC_TOKEN_FILE"]).write_text('{"refresh_token":"new"}', "utf-8")
        assert os.environ.get("CAPTURE_HOME") == before, "env 가 복원되지 않았다"
        assert load_token(conn, uid) == '{"refresh_token":"new"}', "갱신 토큰을 회수하지 못했다"
        assert not list(home(uid).glob("gsc_token*.json")), "평문 토큰 파일이 남았다"

        # 겹쳐 도는 두 컨텍스트 — 워커가 GSC 를 수집하는 몇 분 사이에 화면 요청이
        # 들어오던 자리다. 나중 것이 나가면서 앞선 것의 토큰 파일을 지우면 안 된다.
        def other() -> None:
            c2 = connect()
            try:
                with tenant(c2, uid):
                    pass
            finally:
                c2.close()

        with tenant(conn, uid):
            mine = Path(os.environ["GSC_TOKEN_FILE"])
            t = threading.Thread(target=other)
            t.start()
            t.join()
            assert mine.exists(), "다른 컨텍스트가 내 토큰 파일을 지웠다"
        assert not list(home(uid).glob("gsc_token*.json")), "평문 토큰 파일이 남았다"

        sid = add_site(conn, uid, "myproj", "sc-domain:example.com", "example.com")
        assert len(sites(conn, uid)) == 1
        assert len(due_sites(conn)) == 1, "등록 직후에는 즉시 대상이어야 한다"
        import run_all
        ALL = list(run_all.RUNNABLE_GROUPS)
        assert due_groups(conn, site(conn, uid, "myproj")) == ALL, "첫 측정은 전체 재기다"
        assert due_groups(conn, site(conn, uid, "myproj"), 0) == ALL,             "자동을 끈(0) 배포에서 등록 직후 첫 측정까지 막혔다"

        def age(site_id, hours, groups=None):
            """묶음 시계를 직접 민다 — every_hours 를 아주 작게 주는 방식은
            CURRENT_TIMESTAMP 가 초 단위라 같은 초 안에서만 우연히 통과한다."""
            for g in (groups or ALL):
                conn.execute("UPDATE site_groups SET last_run_at=datetime('now', ?) "
                             "WHERE site_id=? AND grp=?", (f"-{hours} hours", site_id, g))
            conn.commit()

        def due_of(project, u=None, every=168.0):
            return due_groups(conn, site(conn, u or uid, project), every)

        mark_run(conn, sid)
        assert due_sites(conn) == [], "방금 쟀는데 또 잰다"
        assert conn.execute("SELECT running_since FROM sites WHERE id=?",
                            (sid,)).fetchone()[0], "수집 중 표시가 안 켜졌다"
        mark_stage(conn, sid, "keywords", 25)
        r = conn.execute("SELECT stage, stage_pct FROM sites WHERE id=?", (sid,)).fetchone()
        assert (r["stage"], r["stage_pct"]) == ("keywords", 25), tuple(r)
        mark_done(conn, sid)
        assert conn.execute("SELECT stage_pct FROM sites WHERE id=?",
                            (sid,)).fetchone()[0] is None, "끝났는데 진행률이 남았다"
        assert not conn.execute("SELECT running_since FROM sites WHERE id=?",
                                (sid,)).fetchone()[0], "수집 중 표시가 안 꺼졌다"
        assert due_of("myproj") == [], "방금 전체를 쟀는데 밀린 묶음이 있다"

        # ── 묶음별 주기 — 매일 런(할 일)은 하루, 주간 묶음은 run_every_hours, 경쟁·링크는 한 달
        age(sid, 25)
        assert due_of("myproj") == ["todo"], f"하루 지났으면 매일 런만: {due_of('myproj')}"
        age(sid, 169)
        assert due_of("myproj") == ["todo", "search", "ai", "site"], due_of("myproj")
        age(sid, 721)
        assert due_of("myproj") == ALL, "한 달이 지났는데 경쟁·링크가 안 밀렸다"
        # 한 묶음을 재면 그 묶음 시계만 찍힌다 — 다른 묶음의 밀림은 그대로
        mark_groups(conn, sid, ["compete"])
        mark_done(conn, sid)
        assert "compete" not in due_of("myproj") and "search" in due_of("myproj"), \
            f"한 묶음을 쟀는데 다른 묶음 시계까지 찍혔다: {due_of('myproj')}"
        # 사이트 주기(주간 묶음 주기)를 바꾸면 주간 묶음만 따라간다
        age(sid, 30)
        set_every_hours(conn, uid, "myproj", 24)
        assert due_of("myproj") == ["todo", "search", "ai", "site"], due_of("myproj")
        set_every_hours(conn, uid, "myproj", 168)
        assert due_of("myproj") == ["todo"], due_of("myproj")
        # 0 = 자동 재기 전부 끔 — 매일 런까지
        set_every_hours(conn, uid, "myproj", 0)
        age(sid, 999)
        assert due_of("myproj") == [], f"0 인데 자동이 돈다: {due_of('myproj')}"
        assert due_of("myproj", every=0) == [], "전역 0(브레이크)인데 돈다"
        conn.execute("UPDATE sites SET run_every_hours=NULL WHERE id=?", (sid,))
        conn.commit()
        assert due_of("myproj") == ALL
        assert due_of("myproj", every=0) == [], "전역 0 이 비상 브레이크가 아니다"

        # 묶음 시계가 생기기 전에 잰 사이트 — 행이 없으면 sites.last_run_at 이 시계다.
        # 이걸 안 하면 배포 직후 모든 사이트가 다섯 묶음을 한꺼번에 '밀렸다'로 본다.
        conn.execute("DELETE FROM site_groups WHERE site_id=?", (sid,))
        conn.execute("UPDATE sites SET last_run_at=datetime('now','-2 hours') WHERE id=?", (sid,))
        conn.commit()
        assert due_of("myproj") == [], f"옛 사이트가 배포 직후 전부 밀렸다: {due_of('myproj')}"
        # 빌려 쓰기는 한 번뿐이다 — 매일 런(할 일)이 sites.last_run_at 을 새로 찍어도 다른
        # 묶음은 배포 전 시계로 늙어 가야 한다. 안 그러면 순위·AI·크롤이 영영 안 밀린다.
        conn.execute("UPDATE sites SET last_run_at=datetime('now','-100 hours') WHERE id=?",
                     (sid,))
        conn.commit()
        assert due_of("myproj") == ["todo"], due_of("myproj")
        mark_groups(conn, sid, ["todo"])
        mark_done(conn, sid)
        conn.execute("UPDATE site_groups SET last_run_at=datetime(last_run_at, '-70 hours') "
                     "WHERE site_id=?", (sid,))
        conn.commit()
        assert due_of("myproj") == ["todo", "search", "ai", "site"], \
            f"옛 사이트가 매일 런의 시계를 빌려 주간 묶음이 영영 안 밀린다: {due_of('myproj')}"
        # 새 사이트의 첫 런이 묶음 하나(누른 것)여도 나머지는 '한 번도 안 잰 것'이다
        uid_n = upsert_user(conn, "n@example.com")
        sid_n = add_site(conn, uid_n, "newbie", "sc-domain:n.com", "n.com")
        queue_groups(conn, sid_n, ["ai"])
        mark_groups(conn, sid_n, ["ai"])
        mark_done(conn, sid_n)
        assert due_of("newbie", uid_n) == [g for g in ALL if g != "ai"], \
            f"첫 런이 묶음 하나였더니 나머지 묶음이 그 시계를 빌렸다: {due_of('newbie', uid_n)}"
        conn.execute("UPDATE sites SET active=0 WHERE id=?", (sid_n,))
        conn.commit()
        mark_run(conn, sid)
        mark_done(conn, sid)

        # ── 대기열 — 누른 것은 주기와 상관없이 잡히고, 도는 중이면 쌓인다
        assert request_run(conn, uid, "myproj"), "지금 재기 요청이 안 먹는다"
        assert len(due_sites(conn)) == 1, "요청했는데 대상이 아니다"
        assert len(due_sites(conn, every_hours=0)) == 1, "0 이면 수동 재측정까지 막힌다"
        assert claim_queue(conn, sid) == ALL, "대기열을 못 가져간다"
        assert claim_queue(conn, sid) == [], "같은 대기열을 두 번 가져간다 — 같은 묶음을 두 번 산다"
        assert run_phase(site(conn, uid, "myproj")) == "running"
        assert not request_run(conn, uid, "myproj"), "도는 중인데 또 요청이 먹는다"
        mark_done(conn, sid)
        # pending → 몇 초 안에 누른 것은 한 런으로 합쳐진다
        assert mark_pending(conn, sid) and not mark_pending(conn, sid), "두 번 잡혔다"
        assert run_phase(site(conn, uid, "myproj")) == "pending"
        queue_groups(conn, sid, ["ai"])
        queue_groups(conn, sid, ["search", "ai"])
        assert queued(conn, sid) == ["search", "ai"], queued(conn, sid)
        # 대기 중에 올라 있는 묶음은 '도는 중'이다 — 앞에 기다릴 런이 없다(로컬도 같다)
        st = group_status(conn, site(conn, uid, "myproj"), 168.0)
        assert st["search"]["running"] and not st["search"]["queued"], \
            f"방금 누른 묶음이 없는 런 뒤에서 기다리는 것으로 보인다: {st['search']}"
        assert not st["compete"]["running"], st["compete"]
        assert due_sites(conn) == [], "대기 중(pending)인 사이트를 스윕이 또 잡는다"
        assert claim_queue(conn, sid) == ["search", "ai"], "합쳐진 대기열이 아니다"
        clear_pending(conn, sid)
        assert run_phase(site(conn, uid, "myproj")) == "running", \
            "남이 가져가 도는 런의 표시를 껐다"
        # 상태 — 화면이 읽는 모양
        mark_groups(conn, sid, ["search", "todo"])
        queue_groups(conn, sid, ["compete"])
        st = group_status(conn, site(conn, uid, "myproj"), 168.0)
        assert set(st) == set(ALL), st
        assert all(set(v) == {"running", "queued", "last_run_at", "due"} for v in st.values()), st
        assert st["search"]["running"] and not st["ai"]["running"], st
        assert st["compete"]["queued"] and not st["search"]["queued"], st
        assert st["search"]["last_run_at"] and st["search"]["due"] is False, st
        mark_done(conn, sid)
        claim_queue(conn, sid)
        mark_done(conn, sid)
        clear_pending(conn, sid)
        assert run_phase(site(conn, uid, "myproj")) == "idle"

        # ── 스윕이 사이트를 잡는 자리(claim_site) — 목록을 읽은 뒤 남이 잡았으면 못 잡는다
        got = claim_site(conn, sid)
        assert got is not None and run_phase(got) == "running", got and dict(got)
        assert claim_site(conn, sid) is None, "도는 사이트를 스윕이 또 잡았다 — 두 워커가 같이 돈다"
        mark_done(conn, sid)
        mark_busy(conn, sid)                       # 부분 실행이 도는 중
        assert claim_site(conn, sid) is None, "부분 실행이 도는 사이트를 스윕이 잡았다"
        mark_done(conn, sid)
        assert mark_pending(conn, sid)              # 버튼이 눌려 워커가 뜨는 중
        assert claim_site(conn, sid) is None, "대기 중인 사이트를 스윕이 잡았다"
        mark_done(conn, sid)
        # 죽은 런(3시간 초과)은 넘겨받는다 — 맡았던 묶음을 대기열로 되돌리고, 시각을 새로 찍는다
        mark_groups(conn, sid, ["search"])
        conn.execute("UPDATE sites SET running_since=datetime('now','-5 hours') WHERE id=?",
                     (sid,))
        conn.commit()
        got = claim_site(conn, sid)
        assert got is not None, "죽은 런을 못 넘겨받는다"
        assert queued(conn, sid) == ["search"], \
            f"죽은 런이 맡았던 묶음을 버렸다 — 시계는 찍혔으니 한 주를 안 잰 채 넘긴다: {queued(conn, sid)}"
        fresh = "SELECT running_since > datetime('now','-1 minutes') FROM sites WHERE id=?"
        assert conn.execute(fresh, (sid,)).fetchone()[0], \
            "죽은 런을 넘겨받고도 옛 시각을 물려받았다 — 새 런이 처음부터 죽은 런으로 보인다"
        # 라운드가 이어지면 시각을 새로 찍는다(COALESCE 로 물려받으면 긴 런이 죽은 런이 된다)
        conn.execute("UPDATE sites SET running_since=datetime('now','-5 hours') WHERE id=?",
                     (sid,))
        conn.commit()
        mark_groups(conn, sid, claim_queue(conn, sid))
        assert conn.execute(fresh, (sid,)).fetchone()[0], "다음 라운드가 첫 라운드의 시각을 물려받았다"
        mark_done(conn, sid)

        # 부분 실행은 주기 시계를 안 건드린다 — 찍으면 전체 런이 매번 밀린다(3주 동안 그랬다)
        age(sid, 200)
        before = group_clocks(conn, site(conn, uid, "myproj"))
        mark_busy(conn, sid)
        assert group_clocks(conn, site(conn, uid, "myproj")) == before, "부분 실행이 주기 시계를 찍었다"
        assert run_phase(site(conn, uid, "myproj")) == "running", "부분 실행이 '대기 중'으로 보인다"
        assert due_sites(conn) == [], "부분 실행이 도는 중인데 전체 런을 또 잡는다"
        mark_done(conn, sid)
        assert len(due_sites(conn)) == 1, "부분 실행 뒤 밀린 전체 런이 안 잡힌다"
        conn.execute("UPDATE sites SET running_since=CURRENT_TIMESTAMP WHERE id=?", (sid,))
        conn.commit()
        assert due_sites(conn) == [], "수집 중인데 또 잡는다"
        conn.execute("UPDATE sites SET running_since=datetime('now','-5 hours') WHERE id=?",
                     (sid,))
        conn.commit()
        assert len(due_sites(conn)) == 1, "죽은 실행(3시간 초과)이 영영 안 풀린다"
        mark_done(conn, sid)
        assert due_sites(conn, every_hours=0) == [], "0 인데 자동 재측정이 안 꺼진다"
        mark_run(conn, sid); mark_done(conn, sid)

        # 사이트별 판정이어야 한다 — 남이 방금 쟀다고 내 첫 측정이 밀리면 안 된다.
        mark_run(conn, sid); mark_done(conn, sid)      # myproj 를 '방금 잰' 상태로
        uid2 = upsert_user(conn, "b@example.com")
        add_site(conn, uid2, "fresh", "sc-domain:b.com", "b.com")
        due = due_sites(conn)
        assert [r["project"] for r in due] == ["fresh"], f"신규 사이트가 안 잡힌다: {due}"

        # 주기는 행마다 본다 — 사이트별 값이 전역을 이기고, 없으면 전역을 쓴다.
        os.environ.pop("SEOMINER_RUN_EVERY_HOURS", None)
        sid2 = site(conn, uid2, "fresh")["id"]
        mark_run(conn, sid2); mark_done(conn, sid2)
        # 매일 런(24h)은 전역·사이트 값과 상관없이 돈다 — 아래는 주간 묶음만 본다.
        age(sid, 10)
        age(sid2, 10)
        weekly = lambda every: [r["project"] for r in due_sites(conn, every_hours=every)]  # noqa: E731
        assert weekly(1e9) == [], "전역 주기가 아직인데 잰다"
        assert set_every_hours(conn, uid, "myproj", 6), "사이트 주기를 못 바꾼다"
        assert every_hours(conn, uid, "myproj") == 6.0
        assert weekly(1e9) == ["myproj"], "사이트별 값이 전역을 못 이긴다"
        assert every_hours(conn, uid2, "fresh") == 168.0, "NULL 인데 전역 기본값이 아니다"
        assert weekly(1) == ["myproj", "fresh"], "NULL 인 사이트가 전역 주기를 안 쓴다"
        # 0 = 자동만 끔. 대기열(수동 요청)은 그대로 통과한다.
        set_every_hours(conn, uid, "myproj", 0)
        assert weekly(1) == ["fresh"], "사이트 주기가 0 인데 자동 재측정이 안 꺼진다"
        assert request_run(conn, uid, "myproj")
        assert "myproj" in weekly(1), "0 이면 수동 재측정까지 막힌다"
        claim_queue(conn, sid)
        mark_done(conn, sid)
        # 전역 0 은 비상 브레이크다 — 주기를 지정해 둔 사이트까지 멈춘다. 이걸 안 잡으면
        # 운영자가 비용을 멈추려고 0 을 넣어도 설정된 사이트가 계속 유료 수집을 돈다.
        set_every_hours(conn, uid, "myproj", 6)
        age(sid, 999)
        age(sid2, 999)
        assert weekly(1) == ["myproj", "fresh"], "주기가 한참 지났는데 안 잰다"
        assert weekly(0) == [], "전역 0 인데 사이트별 주기가 있는 사이트가 계속 돈다 — 브레이크가 안 듣는다"
        set_every_hours(conn, uid, "myproj", 0)      # 아래 단언이 보는 상태로 되돌린다
        # 남의 사이트는 못 고친다 — project 이름만 알면 되는 게 아니다.
        assert not set_every_hours(conn, uid2, "myproj", 24), "남의 사이트 주기를 고쳤다"
        assert every_hours(conn, uid, "myproj") == 0.0, "남이 내 주기를 바꿨다"
        set_every_hours(conn, uid, "myproj", 168)

        conn.execute("UPDATE users SET last_seen_at=datetime('now','-60 days')")
        assert due_sites(conn) == [], "휴면 계정이 스케줄에서 안 빠졌다"

        # CLI 토큰 — 해시만 남고, 재발급하면 앞 토큰이 그 자리에서 죽는다.
        t1 = issue_cli_token(conn, uid)
        assert t1.startswith("smt_") and len(t1) > 20, t1
        assert uid_for_cli_token(conn, t1) == uid, "발급한 토큰으로 유저를 못 찾는다"
        assert uid_for_cli_token(conn, "smt_틀린값") is None, "아무 토큰이나 통과한다"
        assert uid_for_cli_token(conn, "") is None, "빈 토큰이 통과한다"
        assert t1 not in conn.execute(
            "SELECT token_hash FROM cli_tokens").fetchone()["token_hash"], "토큰이 평문으로 저장됐다"
        t2 = issue_cli_token(conn, uid)
        assert t2 != t1 and uid_for_cli_token(conn, t2) == uid
        assert uid_for_cli_token(conn, t1) is None, "재발급했는데 이전 토큰이 살아 있다"
        assert conn.execute("SELECT COUNT(*) FROM cli_tokens").fetchone()[0] == 1, \
            "유저당 1행이 아니다"

        # 런 로그 — 런당 1벌, 남의 것은 안 보인다.
        assert load_run_log(conn, uid, "myproj") == "", "처음부터 로그가 있다"
        save_run_log(conn, sid, "[1/13] gsc\n")
        assert load_run_log(conn, uid, "myproj") == "[1/13] gsc\n"
        save_run_log(conn, sid, "[1/13] gsc\n[2/13] ga4\n")   # 덮어쓴다(누적 아님)
        assert load_run_log(conn, uid, "myproj").count("gsc") == 1, "로그가 이어붙었다"
        assert load_run_log(conn, uid2, "myproj") == "", "남의 런 로그가 열린다"
        assert load_run_log(conn, uid, "없는사이트") == ""

        # 런 결과 — 화면 배너와 실패 메일이 읽는 값. ok=None 은 안 건드린다.
        mark_done(conn, sid, ok=False, error="rank: DataForSEO 잔액 없음(402)")
        r = site(conn, uid, "myproj")
        assert (r["last_ok"], r["last_error"]) == (0, "rank: DataForSEO 잔액 없음(402)"), tuple(r)
        mark_done(conn, sid)
        assert site(conn, uid, "myproj")["last_ok"] == 0, "ok=None 인데 지난 런의 결과를 지웠다"
        mark_done(conn, sid, ok=True)
        r = site(conn, uid, "myproj")
        assert (r["last_ok"], r["last_error"]) == (1, None), tuple(r)

        # 끝나지 않은 brain runs 행 — 서버가 '안 돈다'고 본 사이트 것만 닫는다.
        # brain 은 유저 하나에 사이트 여럿이다(projects.name 으로 가른다).
        busy = add_site(conn, uid, "busy", "sc-domain:busy.com", "busy.com")
        mark_run(conn, busy)                     # 지금 도는 사이트
        bf = home(uid) / "brain.db"
        bc = sqlite3.connect(bf)
        bc.executescript("""
            CREATE TABLE projects (id INTEGER PRIMARY KEY, name TEXT UNIQUE);
            CREATE TABLE runs (id INTEGER PRIMARY KEY, project_id INTEGER, kind TEXT,
              started_at TEXT, finished_at TEXT, notes TEXT);
            INSERT INTO projects VALUES (1,'myproj'),(2,'busy');
            -- #49: 오래전에 워커가 죽어 남은 고아 (사이트는 이미 안 돈다)
            INSERT INTO runs VALUES (49,1,'rank','2026-09-02T03:00:00Z',NULL,'');
            -- 옛 CURRENT_TIMESTAMP 꼴의 고아도 같은 규칙으로
            INSERT INTO runs VALUES (50,1,'gsc','2026-09-01 03:00:00',NULL,'n=3');
            -- 도는 사이트의 런: 서버가 running 이라 말한다 → 절대 안 닫힌다
            INSERT INTO runs VALUES (60,2,'rank','2026-09-02T03:00:00Z',NULL,'');
            -- 목록을 읽은 뒤 막 시작한 런(cutoff 이후) → 안 닫힌다
            INSERT INTO runs VALUES (70,1,'gsc','2999-01-01T00:00:00Z',NULL,'');
            -- 이미 끝난 런 → 손대지 않는다
            INSERT INTO runs VALUES (80,1,'gsc','2026-09-02T03:00:00Z','2026-09-02T03:05:00Z','ok');
        """)
        bc.commit()

        def run_row(rid):
            return bc.execute("SELECT finished_at, notes FROM runs WHERE id=?", (rid,)).fetchone()

        got = close_orphan_runs(conn)
        assert run_row(60)[0] is None, "도는 사이트의 런을 닫았다 — 살아 있는 워커의 이력을 끊었다"
        assert run_row(70)[0] is None, "목록을 읽은 뒤 시작한 런까지 닫았다(cutoff 가 안 듣는다)"
        assert got == [{"user_id": uid, "project": "myproj", "closed": 2}], got
        assert run_row(49)[0] and ORPHAN_RUN_NOTE in run_row(49)[1], run_row(49)
        assert run_row(50)[0] and run_row(50)[1] == f"n=3 | {ORPHAN_RUN_NOTE}", run_row(50)
        assert run_row(80) == ("2026-09-02T03:05:00Z", "ok"), "이미 끝난 런을 고쳤다"
        assert close_orphan_runs(conn) == [], "이미 닫은 고아를 또 닫는다"
        mark_done(conn, busy, ok=True)           # 이제 안 돈다 → 그 런은 고아다
        assert close_orphan_runs(conn) == [{"user_id": uid, "project": "busy", "closed": 1}]
        conn.execute("UPDATE sites SET active=0 WHERE id=?", (busy,))
        conn.commit()

        # 죽은 런 회수 — 컨테이너가 교체되면 워커의 finally 는 안 돈다.
        mark_run(conn, sid)
        save_run_log(conn, sid, "[1/13] gsc\n")
        import time
        from datetime import datetime, timezone
        bc.execute("INSERT INTO runs VALUES (90,1,'rank',?,NULL,'')",
                   (datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),))
        bc.commit()
        time.sleep(1.1)                        # cutoff 는 초 단위다 — 같은 초면 '막 시작한 런'이다
        got = reclaim_dead_runs(conn)
        assert run_row(90)[0] and ORPHAN_RUN_NOTE in run_row(90)[1], \
            f"회수했는데 brain 의 런이 끝나지 않은 채 남았다: {run_row(90)}"
        assert run_row(70)[0] is None, "회수가 cutoff 이후의 런까지 닫았다"
        bc.close()
        assert [r["id"] for r in got] == [sid], f"죽은 런을 못 찾았다: {[dict(r) for r in got]}"
        r = site(conn, uid, "myproj")
        assert not r["running_since"], "회수했는데 '수집 중' 표시가 남았다"
        assert r["last_ok"] == 0 and "서버 재시작" in (r["last_error"] or ""), tuple(r)
        assert load_run_log(conn, uid, "myproj").startswith("[1/13] gsc\n"), \
            "회수가 지금까지의 로그를 날렸다"
        assert "서버 재시작" in load_run_log(conn, uid, "myproj"), \
            "런이 왜 끊겼는지 로그에 안 남았다"
        # 죽은 런이 맡았던 묶음은 대기열로 돌아간다 — 시계는 시작할 때 찍혔으니, 안
        # 되돌리면 안 잰 채로 다음 주기까지 넘어간다.
        assert queued(conn, sid) == ALL, f"죽은 런의 묶음이 대기열로 안 돌아왔다: {queued(conn, sid)}"
        assert reclaim_dead_runs(conn) == [], "도는 런이 없는데 또 회수한다"
        claim_queue(conn, sid)
        mark_done(conn, sid, ok=True)
        # 죽은 부분 실행 — 다시 띄울 것이 없으니 "자동으로 다시 시작합니다"라고 하면 거짓말이다
        mark_busy(conn, sid)
        assert [r["id"] for r in reclaim_dead_runs(conn)] == [sid]
        r = site(conn, uid, "myproj")
        assert queued(conn, sid) == [] and r["last_error"] == DEAD_PARTIAL_ERROR, \
            (queued(conn, sid), r["last_error"])
        mark_done(conn, sid, ok=True)

        # session() — 라우트가 conn 열기·소유 확인·tenant·유료 키를 한 번에 쓰는 자리.
        with session(uid, "myproj") as c:
            assert c is not None
        # project 를 주면 소유 확인이 기본값이다 — 없는 사이트도, 남의 사이트도 404.
        try:
            with session(uid, "없는사이트"):
                raise AssertionError("없는 사이트가 세션을 열었다")
        except HTTPException as e:
            assert e.status_code == 404, e.status_code
        try:
            with session(uid2, "myproj"):        # uid2 는 myproj 의 주인이 아니다
                raise AssertionError("소유 확인 기본값이 빠졌다 — 남의 사이트가 열렸다")
        except HTTPException as e:
            assert e.status_code == 404, e.status_code
        with session(uid, "myproj", own=False):  # 새 사이트 등록처럼 아직 소유가 없는 자리
            pass
        with session(uid, "myproj", isolate=True) as c:
            assert os.environ["CAPTURE_HOME"] == str(home(uid)), "tenant 가 안 둘러졌다"
        assert "CAPTURE_HOME" not in os.environ, "session 을 나간 뒤 env 가 안 돌아왔다"
        os.environ.pop("OPENROUTER_API_KEY", None)
        os.environ["SEOMINER_OPENROUTER_API_KEY"] = "srv-key"
        with session(uid, "myproj", isolate=True, paid=True):
            assert os.environ["OPENROUTER_API_KEY"] == "srv-key", "paid=True 인데 유료 키가 안 섰다"
        assert "OPENROUTER_API_KEY" not in os.environ, "유료 키가 안 걷혔다"
        os.environ.pop("SEOMINER_OPENROUTER_API_KEY", None)

        conn.close()                          # 윈도우는 열린 파일을 지우지 못한다
        print("store: ok")


if __name__ == "__main__":
    demo()
