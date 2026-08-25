"""서버 저장소 — 유저·구글 토큰·테넌트 격리.

수집 엔진(skills/capture/scripts)은 CAPTURE_HOME / GSC_TOKEN_FILE 을 호출할 때마다
다시 읽는다(db.py __getattr__). 그래서 테넌트 격리는 env 를 갈아끼우는 것으로 끝난다 —
엔진 코드는 한 줄도 손대지 않는다.

Env 는 settings.py 가 소유한다 — 여기서 기본값을 정하지 않는다.
(SEOMINER_DATA, SEOMINER_SECRET_KEY)
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from cryptography.fernet import Fernet

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
  UNIQUE(user_id, project)
);
"""


def _migrate(conn: sqlite3.Connection) -> None:
    """CREATE TABLE IF NOT EXISTS 가 못 하는 것만 — 이미 있는 테이블의 새 컬럼."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(sites)")}
    if not cols:
        return
    for col in ("last_run_at", "running_since", "repo", "repo_branch", "repo_profile"):
        if col not in cols:
            conn.execute(f"ALTER TABLE sites ADD COLUMN {col} TEXT")
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
    """
    # 0 = '자동 재측정 끔'. 아직 한 번도 안 잰 사이트와 수동 요청(request_run 이
    # last_run_at 을 NULL 로 만든다)까지 막으면 등록도 '지금 재기'도 죽는다.
    if every_hours <= 0:
        every_hours = 1e9
    return conn.execute(
        "SELECT s.*, u.email FROM sites s JOIN users u ON u.id = s.user_id "
        "WHERE s.active=1 AND u.last_seen_at > datetime('now', ?) "
        "  AND (s.last_run_at IS NULL OR s.last_run_at <= datetime('now', ?)) "
        "  AND (s.running_since IS NULL OR s.running_since <= datetime('now','-3 hours')) "
        "ORDER BY s.last_run_at IS NOT NULL, s.id",   # 첫 측정 대기자를 먼저
        (f"-{idle_days} days", f"-{every_hours} hours")).fetchall()


def mark_run(conn: sqlite3.Connection, site_id: int) -> None:
    """수집 시작 표시. 성공·실패 무관하게 찍는다 — 실패한 사이트가 매 틱마다
    재시도하면 비용이 샌다."""
    conn.execute("UPDATE sites SET last_run_at=CURRENT_TIMESTAMP, "
                 "running_since=CURRENT_TIMESTAMP WHERE id=?", (site_id,))
    conn.commit()


def mark_done(conn: sqlite3.Connection, site_id: int) -> None:
    conn.execute("UPDATE sites SET running_since=NULL WHERE id=?", (site_id,))
    conn.commit()


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
    """
    h = home(user_id)
    h.mkdir(parents=True, exist_ok=True)
    tok = h / "gsc_token.json"

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
            tok.unlink()                      # 평문 토큰을 디스크에 남기지 않는다
        for k, v in saved.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)


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
        assert not (home(uid) / "gsc_token.json").exists(), "평문 토큰 파일이 남았다"

        sid = add_site(conn, uid, "myproj", "sc-domain:example.com", "example.com")
        assert len(sites(conn, uid)) == 1
        assert len(due_sites(conn)) == 1, "등록 직후에는 즉시 대상이어야 한다"

        mark_run(conn, sid)
        assert due_sites(conn) == [], "방금 쟀는데 또 잰다"
        assert conn.execute("SELECT running_since FROM sites WHERE id=?",
                            (sid,)).fetchone()[0], "수집 중 표시가 안 켜졌다"
        mark_done(conn, sid)
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

        conn.execute("UPDATE users SET last_seen_at=datetime('now','-60 days')")
        assert due_sites(conn) == [], "휴면 계정이 스케줄에서 안 빠졌다"
        save_github(conn, uid, "ghp_secret", "octocat")
        assert github(conn, uid) == ("ghp_secret", "octocat")
        raw = conn.execute("SELECT token_enc FROM github_tokens").fetchone()["token_enc"]
        assert b"ghp_secret" not in raw, "GitHub 토큰이 평문으로 저장됐다"
        set_repo(conn, uid, "myproj", "octocat/site", "main")
        assert site(conn, uid, "myproj")["repo"] == "octocat/site"

        conn.close()                          # 윈도우는 열린 파일을 지우지 못한다
        print("store: ok")


if __name__ == "__main__":
    demo()
