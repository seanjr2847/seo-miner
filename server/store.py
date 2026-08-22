"""서버 저장소 — 유저·구글 토큰·테넌트 격리.

수집 엔진(skills/capture/scripts)은 CAPTURE_HOME / GSC_TOKEN_FILE 을 호출할 때마다
다시 읽는다(db.py __getattr__). 그래서 테넌트 격리는 env 를 갈아끼우는 것으로 끝난다 —
엔진 코드는 한 줄도 손대지 않는다.

Env:
  SEOMINER_DATA        서버 데이터 루트 (기본 ~/.seominer)
  SEOMINER_SECRET_KEY  Fernet 키 — 구글 refresh token 암호화용 (필수)
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from cryptography.fernet import Fernet


def data_dir() -> Path:
    return Path(os.environ.get("SEOMINER_DATA", Path.home() / ".seominer"))


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
CREATE TABLE IF NOT EXISTS sites (
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id),
  project TEXT NOT NULL,              -- capture 프로젝트 이름 (테넌트 안에서만 유일)
  gsc_property TEXT NOT NULL,         -- sc-domain:example.com
  domain TEXT NOT NULL,
  active INTEGER DEFAULT 1,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(user_id, project)
);
"""


def _fernet() -> Fernet:
    key = os.environ.get("SEOMINER_SECRET_KEY")
    if not key:
        raise RuntimeError(
            "SEOMINER_SECRET_KEY 가 없습니다 — "
            "python -c \"from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())\" "
            "로 만들어 env 에 넣으세요")
    return Fernet(key)


def connect() -> sqlite3.Connection:
    d = data_dir()
    d.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(d / "server.db")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
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


def due_sites(conn: sqlite3.Connection, idle_days: int = 30) -> list[sqlite3.Row]:
    """스케줄 대상 — 휴면 계정(last_seen_at 이 idle_days 초과)은 뺀다.

    B안은 유저가 안 봐도 매주 API 비용이 나간다. 이 한 줄이 휴면 원가를 없앤다.
    """
    return conn.execute(
        "SELECT s.*, u.email FROM sites s JOIN users u ON u.id = s.user_id "
        "WHERE s.active=1 AND u.last_seen_at > datetime('now', ?) ORDER BY s.id",
        (f"-{idle_days} days",)).fetchall()


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
        os.environ["SEOMINER_SECRET_KEY"] = Fernet.generate_key().decode()
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

        add_site(conn, uid, "myproj", "sc-domain:example.com", "example.com")
        assert len(sites(conn, uid)) == 1
        assert len(due_sites(conn)) == 1
        conn.execute("UPDATE users SET last_seen_at=datetime('now','-60 days')")
        assert due_sites(conn) == [], "휴면 계정이 스케줄에서 안 빠졌다"
        conn.close()                          # 윈도우는 열린 파일을 지우지 못한다
        print("store: ok")


if __name__ == "__main__":
    demo()
