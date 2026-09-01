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
CREATE TABLE IF NOT EXISTS github_tokens (
  user_id INTEGER PRIMARY KEY REFERENCES users(id),
  token_enc BLOB NOT NULL,
  login TEXT,
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
  repo TEXT,                          -- owner/name — /create 가 PR 을 낼 곳
  repo_branch TEXT,                   -- 기본 브랜치
  repo_profile TEXT,                  -- 리포 관례(JSON) — 발견 결과 캐시
  run_every_hours REAL,               -- 이 사이트의 재측정 주기(시간). NULL = 전역 기본값, 0 = 자동 끔
  stage TEXT,                         -- 지금 도는 단계 id (run_all.STAGES 의 이름)
  stage_pct INTEGER,                  -- 그 시점의 진행률 0~100 (끝난 단계 / 전체 단계)
  run_log TEXT,                       -- 이번 런의 화면 출력 (워커 stdout). 런당 1벌
  UNIQUE(user_id, project)
);
"""


def _migrate(conn: sqlite3.Connection) -> None:
    """CREATE TABLE IF NOT EXISTS 가 못 하는 것만 — 이미 있는 테이블의 새 컬럼."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(sites)")}
    if not cols:
        return
    for col in ("last_run_at", "running_since", "repo", "repo_branch", "repo_profile",
                "stage", "run_log"):
        if col not in cols:
            conn.execute(f"ALTER TABLE sites ADD COLUMN {col} TEXT")
    # 숫자로 비교한다 — TEXT 로 두면 SQLite 가 '0' > 0 을 참으로 봐서 '끔'이 안 먹는다.
    if "run_every_hours" not in cols:
        conn.execute("ALTER TABLE sites ADD COLUMN run_every_hours REAL")
    if "stage_pct" not in cols:
        conn.execute("ALTER TABLE sites ADD COLUMN stage_pct INTEGER")
    conn.commit()


def _fernet() -> Fernet:
    # settings.Missing 은 RuntimeError 다 — 이 함수가 RuntimeError 를 던지던 계약 그대로.
    return Fernet(settings.get("SEOMINER_SECRET_KEY"))


def connect() -> sqlite3.Connection:
    d = data_dir()
    d.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(d / "server.db")
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
# 위의 google_tokens / github_tokens 는 **암호화**다. 남의 API 를 다시 부르려면 원문이
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


def due_sites(conn: sqlite3.Connection, idle_days: int = 30,
              every_hours: float = 168.0) -> list[sqlite3.Row]:
    """지금 잴 사이트 — 휴면 계정과 '아직 주기가 안 된 사이트'를 뺀다.

    주기는 반드시 사이트별로 본다. 전역 스탬프 하나로 판정하면 먼저 등록한 사이트가
    도장을 찍어 버려서, 방금 가입한 사람의 첫 측정이 다음 주기까지 통째로 밀린다 —
    그 사람은 빈 대시보드만 보고 떠난다.

    last_run_at 이 NULL 이면(= 등록 직후) 즉시 대상이다.

    주기는 행마다 다르다: sites.run_every_hours 가 있으면 그 값, 없으면(NULL) 인자로
    받은 전역 기본값이다. 0 = '자동 재측정 끔'. 아직 한 번도 안 잰 사이트와 수동
    요청(request_run 이 last_run_at 을 NULL 로 만든다)까지 막으면 등록도 '지금 재기'도
    죽으므로, 0 은 last_run_at IS NULL 만 통과시킨다.

    전역 0 과 사이트별 0 은 뜻이 다르다. 사이트별 0 은 그 사이트만 끄지만, **전역 0 은
    배포 전체의 비상 브레이크라 사이트별 값을 이긴다** — SERP·AI 는 실행당 과금이라
    운영자가 비용을 멈추려고 0 을 넣었는데 주기를 지정해 둔 사이트가 계속 돌면
    브레이크가 아니다. 설정 설명("0 이면 자동 수집을 끈다")도 그 뜻이다.
    """
    # 전역이 꺼져 있으면 주기 판정 자체를 건너뛴다 — 첫 측정과 수동 요청만 남는다.
    period = ("s.last_run_at IS NULL" if every_hours <= 0 else
              "(s.last_run_at IS NULL OR (COALESCE(s.run_every_hours, :every) > 0 AND "
              " s.last_run_at <= datetime('now', '-' || COALESCE(s.run_every_hours, :every) || ' hours')))")
    return conn.execute(
        "SELECT s.*, u.email FROM sites s JOIN users u ON u.id = s.user_id "
        "WHERE s.active=1 AND u.last_seen_at > datetime('now', :idle) "
        f"  AND {period} "
        "  AND (s.running_since IS NULL OR s.running_since <= datetime('now','-3 hours')) "
        "ORDER BY s.last_run_at IS NOT NULL, s.id",   # 첫 측정 대기자를 먼저
        {"idle": f"-{idle_days} days", "every": every_hours}).fetchall()


def mark_run(conn: sqlite3.Connection, site_id: int) -> None:
    """수집 시작 표시. 성공·실패 무관하게 찍는다 — 실패한 사이트가 매 틱마다
    재시도하면 비용이 샌다."""
    conn.execute("UPDATE sites SET last_run_at=CURRENT_TIMESTAMP, "
                 "running_since=CURRENT_TIMESTAMP, stage=NULL, stage_pct=0 WHERE id=?",
                 (site_id,))
    conn.commit()


def mark_stage(conn: sqlite3.Connection, site_id: int, stage: str, pct: int) -> None:
    """지금 도는 단계와 진행률. 화면이 '분석 중…' 대신 몇 %인지 말할 수 있게 하는 값이다."""
    conn.execute("UPDATE sites SET stage=?, stage_pct=? WHERE id=?",
                 (stage, max(0, min(100, int(pct))), site_id))
    conn.commit()


def mark_done(conn: sqlite3.Connection, site_id: int) -> None:
    conn.execute("UPDATE sites SET running_since=NULL, stage=NULL, stage_pct=NULL "
                 "WHERE id=?", (site_id,))
    conn.commit()


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


def save_github(conn: sqlite3.Connection, user_id: int, token: str, login: str) -> None:
    conn.execute(
        "INSERT INTO github_tokens(user_id, token_enc, login) VALUES (?,?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET token_enc=excluded.token_enc, "
        "login=excluded.login, updated_at=CURRENT_TIMESTAMP",
        (user_id, _fernet().encrypt(token.encode("utf-8")), login))
    conn.commit()


def github(conn: sqlite3.Connection, user_id: int) -> tuple[str, str] | None:
    """(token, login) 또는 None."""
    row = conn.execute("SELECT token_enc, login FROM github_tokens WHERE user_id=?",
                       (user_id,)).fetchone()
    return (_fernet().decrypt(row["token_enc"]).decode("utf-8"), row["login"]) if row else None


def set_repo(conn: sqlite3.Connection, user_id: int, project: str,
             repo: str, branch: str) -> None:
    conn.execute("UPDATE sites SET repo=?, repo_branch=?, repo_profile=NULL "
                 "WHERE user_id=? AND project=?", (repo, branch, user_id, project))
    conn.commit()


def set_profile(conn: sqlite3.Connection, user_id: int, project: str, profile: str) -> None:
    conn.execute("UPDATE sites SET repo_profile=? WHERE user_id=? AND project=?",
                 (profile, user_id, project))
    conn.commit()


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
    """'지금 다시 재기' — 주기를 무시하고 다음 대상으로 만든다. 이미 도는 중이면 무시."""
    cur = conn.execute(
        "UPDATE sites SET last_run_at=NULL WHERE user_id=? AND project=? "
        "AND active=1 AND running_since IS NULL", (user_id, project))
    conn.commit()
    return cur.rowcount > 0


# --- 테넌트 -----------------------------------------------------------------

def home(user_id: int) -> Path:
    return data_dir() / "users" / str(user_id)


@contextmanager
def tenant(conn: sqlite3.Connection, user_id: int):
    """유저 한 명의 env 로 갈아끼운다. 엔진은 이 env 만 보고 돈다.

    나갈 때 토큰 파일을 회수해 DB 에 되쓴다 — collect_gsc 가 갱신한 access token 이
    거기 담기므로(collect_gsc.py:74), 안 거두면 매 실행마다 refresh 왕복을 한다.

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

    saved = {k: os.environ.get(k) for k in ("CAPTURE_HOME", "GSC_TOKEN_FILE")}
    os.environ["CAPTURE_HOME"] = str(h)
    os.environ["GSC_TOKEN_FILE"] = str(tok)
    try:
        yield h
    finally:
        if tok.exists():
            save_token(conn, user_id, tok.read_text("utf-8"))
        tok.unlink(missing_ok=True)           # 평문 토큰을 디스크에 남기지 않는다
        for k, v in saved.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)


@contextmanager
def session(uid: int, project: str | None = None, *, own: bool = True,
            isolate: bool = False, paid: bool = False):
    """라우트 하나가 필요로 하는 걸 한 번에: conn 열기 → (project 를 주면) 소유
    확인 → (isolate 면) tenant 두르기 → (paid 면) 유료 키까지 → 끝나면 정리.

    conn = store.connect(); try: ... finally: conn.close() 와 _own() 과
    with store.tenant() 가 라우트마다 손으로 반복되던 것을 한 곳으로 모은다.

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
            with tenant(conn, uid):
                if paid:
                    with settings.paid_keys():
                        yield conn
                else:
                    yield conn
        else:
            yield conn
    finally:
        conn.close()


def demo() -> None:
    import tempfile
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
        with tenant(conn, uid) as h:
            assert os.environ["CAPTURE_HOME"] == str(h)
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
        assert request_run(conn, uid, "myproj"), "지금 재기 요청이 안 먹는다"
        assert len(due_sites(conn)) == 1, "요청했는데 대상이 아니다"
        mark_run(conn, sid)
        assert not request_run(conn, uid, "myproj"), "도는 중인데 또 요청이 먹는다"
        mark_done(conn, sid)
        # 시간을 직접 민다 — every_hours 를 아주 작게 주는 방식은 CURRENT_TIMESTAMP 가
        # 초 단위라 같은 초 안에서만 우연히 통과한다.
        conn.execute("UPDATE sites SET last_run_at=datetime('now','-200 hours') WHERE id=?",
                     (sid,))
        conn.commit()
        assert len(due_sites(conn)) == 1, "주기가 지났는데 안 잰다"
        conn.execute("UPDATE sites SET running_since=CURRENT_TIMESTAMP WHERE id=?", (sid,))
        conn.commit()
        assert due_sites(conn) == [], "수집 중인데 또 잡는다"
        conn.execute("UPDATE sites SET running_since=datetime('now','-5 hours') WHERE id=?",
                     (sid,))
        conn.commit()
        assert len(due_sites(conn)) == 1, "죽은 실행(3시간 초과)이 영영 안 풀린다"
        mark_done(conn, sid)
        assert due_sites(conn, every_hours=0) == [], "0 인데 자동 재측정이 안 꺼진다"
        assert request_run(conn, uid, "myproj") and             len(due_sites(conn, every_hours=0)) == 1, "0 이면 수동 재측정까지 막힌다"
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
        conn.execute("UPDATE sites SET last_run_at=datetime('now','-10 hours')")
        conn.commit()
        assert due_sites(conn, every_hours=1e9) == [], "전역 주기가 아직인데 잰다"
        assert set_every_hours(conn, uid, "myproj", 6), "사이트 주기를 못 바꾼다"
        assert every_hours(conn, uid, "myproj") == 6.0
        assert [r["project"] for r in due_sites(conn, every_hours=1e9)] == ["myproj"], \
            "사이트별 값이 전역을 못 이긴다"
        assert every_hours(conn, uid2, "fresh") == 168.0, "NULL 인데 전역 기본값이 아니다"
        assert [r["project"] for r in due_sites(conn, every_hours=1)] == ["myproj", "fresh"], \
            "NULL 인 사이트가 전역 주기를 안 쓴다"
        # 0 = 자동만 끔. 첫 측정(last_run_at IS NULL)과 수동 요청은 그대로 통과한다.
        set_every_hours(conn, uid, "myproj", 0)
        assert [r["project"] for r in due_sites(conn, every_hours=1)] == ["fresh"], \
            "사이트 주기가 0 인데 자동 재측정이 안 꺼진다"
        assert request_run(conn, uid, "myproj")
        assert "myproj" in [r["project"] for r in due_sites(conn, every_hours=1)], \
            "0 이면 첫 측정·수동 재측정까지 막힌다"
        # 전역 0 은 비상 브레이크다 — 주기를 지정해 둔 사이트까지 멈춘다. 이걸 안 잡으면
        # 운영자가 비용을 멈추려고 0 을 넣어도 설정된 사이트가 계속 유료 수집을 돈다.
        set_every_hours(conn, uid, "myproj", 6)
        conn.execute("UPDATE sites SET last_run_at = datetime('now','-999 hours')")
        conn.commit()
        assert [r["project"] for r in due_sites(conn, every_hours=1)] == ["myproj", "fresh"], \
            "주기가 한참 지났는데 안 잰다"
        assert due_sites(conn, every_hours=0) == [], \
            "전역 0 인데 사이트별 주기가 있는 사이트가 계속 돈다 — 브레이크가 안 듣는다"
        set_every_hours(conn, uid, "myproj", 0)      # 아래 단언이 보는 상태로 되돌린다
        # 남의 사이트는 못 고친다 — project 이름만 알면 되는 게 아니다.
        assert not set_every_hours(conn, uid2, "myproj", 24), "남의 사이트 주기를 고쳤다"
        assert every_hours(conn, uid, "myproj") == 0.0, "남이 내 주기를 바꿨다"
        set_every_hours(conn, uid, "myproj", 168)

        conn.execute("UPDATE users SET last_seen_at=datetime('now','-60 days')")
        assert due_sites(conn) == [], "휴면 계정이 스케줄에서 안 빠졌다"
        save_github(conn, uid, "ghp_secret", "octocat")
        assert github(conn, uid) == ("ghp_secret", "octocat")
        raw = conn.execute("SELECT token_enc FROM github_tokens").fetchone()["token_enc"]
        assert b"ghp_secret" not in raw, "GitHub 토큰이 평문으로 저장됐다"
        set_repo(conn, uid, "myproj", "octocat/site", "main")
        assert site(conn, uid, "myproj")["repo"] == "octocat/site"

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
