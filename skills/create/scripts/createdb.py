#!/usr/bin/env python3
"""create skill <-> shared Brain bridge.

Opens the SAME brain.db as the capture skill ($CAPTURE_HOME, default ~/.capture)
and adds one table of its own (creations). Read/claim opportunities, record
what was written where, and close the loop so the next capture run measures it.

Standalone-safe: if brain.db doesn't exist, commands explain and exit cleanly
(create then runs in manual-brief mode without Brain).

CLI:
  python createdb.py pick   <project> [--kinds a,b] [--limit 10]
  python createdb.py claim  <project> <id> [<id> ...]
  python createdb.py done   <project> <id> --path PATH [--branch BR] [--note N]
  python createdb.py list   <project>
  python createdb.py merged <creation_id>
"""
import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

CAPTURE_HOME = Path(os.environ.get("CAPTURE_HOME", Path.home() / ".capture"))
DB_PATH = Path(os.environ.get("CAPTURE_DB", CAPTURE_HOME / "brain.db"))

# 한국어 Windows 콘솔(cp949)에서 기회 reasoning의 '—'가 UnicodeEncodeError로 죽는다.
# capture의 db.py는 이미 하는 처리인데, create는 그걸 import하지 않아 빠져 있었다
# (첫 실사용 `pick`에서 바로 크래시).
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):   # 파이프로 감싼 경우 등
        pass

CREATIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS creations (
  id INTEGER PRIMARY KEY,
  project_id INTEGER NOT NULL,
  opportunity_id INTEGER,                -- NULL for manual-brief work
  kind TEXT,
  file_path TEXT NOT NULL,
  branch TEXT,
  note TEXT,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  merged INTEGER DEFAULT 0
);
"""


def connect() -> sqlite3.Connection:
    if not DB_PATH.exists():
        sys.exit(f"brain.db not found at {DB_PATH} — capture 미설치 상태. "
                 "create는 수동 브리프 모드로 진행 가능(SKILL.md 참조).")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(CREATIONS_SCHEMA)
    return conn


def pid_of(conn, project: str) -> int:
    row = conn.execute("SELECT id FROM projects WHERE name=?", (project,)).fetchone()
    if not row:
        sys.exit(f"project '{project}' not in Brain")
    return row["id"]


def pick(project: str, kinds: str | None, limit: int) -> None:
    conn = connect()
    pid = pid_of(conn, project)
    q = ("SELECT id, kind, target, score, reasoning, status, created_at "
         "FROM opportunities WHERE project_id=? AND status IN ('new','acked')")
    args: list = [pid]
    if kinds:
        ks = [k.strip() for k in kinds.split(",")]
        q += f" AND kind IN ({','.join('?' * len(ks))})"
        args += ks
    q += " ORDER BY status='acked' DESC, score DESC LIMIT ?"
    args.append(limit)
    rows = [dict(r) for r in conn.execute(q, args)]
    print(json.dumps(rows, ensure_ascii=False, indent=2))
    conn.close()


def claim(project: str, ids: list[int]) -> None:
    conn = connect()
    pid = pid_of(conn, project)
    for i in ids:
        conn.execute("UPDATE opportunities SET status='acked' "
                     "WHERE id=? AND project_id=?", (i, pid))
    conn.commit()
    print(f"claimed {len(ids)} opportunities (status=acked)")
    conn.close()


def done(project: str, opp_id: int | None, path: str,
         branch: str | None, note: str | None) -> None:
    conn = connect()
    pid = pid_of(conn, project)
    kind = None
    if opp_id:
        row = conn.execute("SELECT kind FROM opportunities WHERE id=? AND project_id=?",
                           (opp_id, pid)).fetchone()
        kind = row["kind"] if row else None
        conn.execute("UPDATE opportunities SET status='done' WHERE id=? AND project_id=?",
                     (opp_id, pid))
    conn.execute(
        """INSERT INTO creations(project_id, opportunity_id, kind, file_path,
                                 branch, note, created_at)
           VALUES(?,?,?,?,?,?,?)""",
        (pid, opp_id, kind, path, branch, note,
         datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")))
    conn.commit()
    print(f"recorded: opp#{opp_id} -> {path} ({branch or 'no-branch'})")
    conn.close()


def list_creations(project: str) -> None:
    conn = connect()
    pid = pid_of(conn, project)
    rows = [dict(r) for r in conn.execute(
        """SELECT id, opportunity_id, kind, file_path, branch, merged, created_at
             FROM creations WHERE project_id=? ORDER BY id DESC LIMIT 30""", (pid,))]
    print(json.dumps(rows, ensure_ascii=False, indent=2))
    conn.close()


def merged(creation_id: int) -> None:
    conn = connect()
    conn.execute("UPDATE creations SET merged=1 WHERE id=?", (creation_id,))
    conn.commit()
    print(f"creation#{creation_id} marked merged")
    conn.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p1 = sub.add_parser("pick"); p1.add_argument("project")
    p1.add_argument("--kinds"); p1.add_argument("--limit", type=int, default=10)
    p2 = sub.add_parser("claim"); p2.add_argument("project")
    p2.add_argument("ids", nargs="+", type=int)
    p3 = sub.add_parser("done"); p3.add_argument("project")
    p3.add_argument("id", type=int, nargs="?", default=None)
    p3.add_argument("--path", required=True); p3.add_argument("--branch")
    p3.add_argument("--note")
    p4 = sub.add_parser("list"); p4.add_argument("project")
    p5 = sub.add_parser("merged"); p5.add_argument("creation_id", type=int)
    a = ap.parse_args()
    if a.cmd == "pick":
        pick(a.project, a.kinds, a.limit)
    elif a.cmd == "claim":
        claim(a.project, a.ids)
    elif a.cmd == "done":
        done(a.project, a.id, a.path, a.branch, a.note)
    elif a.cmd == "list":
        list_creations(a.project)
    elif a.cmd == "merged":
        merged(a.creation_id)
