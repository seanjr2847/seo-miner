#!/usr/bin/env python3
"""create skill <-> shared Brain bridge.

Opens the SAME brain.db as the capture skill ($CAPTURE_HOME, default ~/.capture)
through capture의 db 모듈. Read/claim opportunities, record what was written
where, and close the loop so the next capture run measures it.

Standalone-safe: if brain.db doesn't exist, commands explain and exit cleanly
(create then runs in manual-brief mode without Brain).

CLI:
  python createdb.py pick   <project> [--kinds a,b] [--limit 10]
  python createdb.py claim  <project> <id> [<id> ...]
  python createdb.py done   <project> <id> --path PATH [--branch BR] [--note N]
  python createdb.py list   <project>
  python createdb.py merged <creation_id>
  python createdb.py sync   <project> --repo PATH
"""
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "capture" / "scripts"))
import db  # noqa: E402

# creations 테이블·경로 규칙·콘솔 인코딩은 전부 db가 갖는다. 여기서 따로 CREATE TABLE을
# 하던 시절엔 create가 capture의 Brain 안에 제 테이블을 심는 꼴이라, dashboard가 그
# 테이블을 셀 때마다 try/except로 없을 가능성을 감싸야 했다.

# SKILL.md가 정한 브랜치 이름 규칙. 어긋나도 막지는 않는다 — 작업 중간에
# 브랜치 이름 하나로 사용자를 세우는 건 손해가 더 크다.
BRANCH_RE = re.compile(r"^capture/\w+-\S+$")


def connect():
    if not db.DB_PATH.exists():
        sys.exit(f"brain.db not found at {db.DB_PATH} — capture 미설치 상태. "
                 "create는 수동 브리프 모드로 진행 가능(SKILL.md 참조).")
    return db.connect()


def pick(project: str, kinds: str | None, limit: int) -> None:
    conn = connect()
    pid = db.get_project(conn, project)["id"]
    rows = [dict(r) for r in db.open_opportunities(conn, pid, kinds=kinds, limit=limit)]
    print(json.dumps(rows, ensure_ascii=False, indent=2))
    conn.close()


def claim(project: str, ids: list[int]) -> None:
    conn = connect()
    pid = db.get_project(conn, project)["id"]
    for i in ids:
        db.set_opportunity_status(conn, i, "acked", project_id=pid)
    print(f"claimed {len(ids)} opportunities (status=acked)")
    conn.close()


def _mark_done(conn, pid: int, opp_id: int | None, path: str,
               branch: str | None, note: str | None) -> None:
    if branch and not BRANCH_RE.match(branch):
        print(f"경고: 브랜치 '{branch}' 가 규칙과 다릅니다 — 기대 형식 "
              "capture/{kind}-{slug} (기록은 그대로 진행합니다)", file=sys.stderr)
    kind = None
    if opp_id:
        row = conn.execute("SELECT kind FROM opportunities WHERE id=? AND project_id=?",
                           (opp_id, pid)).fetchone()
        kind = row["kind"] if row else None
        db.set_opportunity_status(conn, opp_id, "done", project_id=pid)
    db.record_creation(conn, pid, path, opportunity_id=opp_id, kind=kind,
                       branch=branch, note=note)


def done(project: str, opp_id: int | None, path: str,
         branch: str | None, note: str | None) -> None:
    conn = connect()
    pid = db.get_project(conn, project)["id"]
    _mark_done(conn, pid, opp_id, path, branch, note)
    print(f"recorded: opp#{opp_id} -> {path} ({branch or 'no-branch'})")
    conn.close()


def sync(project: str, repo: str) -> None:
    try:
        p = subprocess.run(
            ["git", "-C", str(repo), "log", r"--grep=\[opp\ #", r"--format=%H%x09%s"],
            capture_output=True, encoding="utf-8", errors="replace",
        )
    except FileNotFoundError:
        sys.exit("git 실행 파일을 찾을 수 없습니다.")
    if p.returncode != 0:
        err = p.stderr.strip() or p.stdout.strip() or "git 저장소가 아니거나 실행 오류"
        sys.exit(f"git log 실행 실패 ({repo}): {err}")

    conn = connect()
    pid = db.get_project(conn, project)["id"]
    closed = 0
    untouched = 0

    for line in p.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        sha, _, _ = line.partition("\t")
        ids = [int(x) for x in re.findall(r"\[opp #(\d+)\]", line)]
        for opp_id in ids:
            row = conn.execute(
                "SELECT id, status FROM opportunities WHERE id=? AND project_id=?",
                (opp_id, pid)
            ).fetchone()
            if row and row["status"] in ("new", "acked"):
                _mark_done(conn, pid, opp_id, "", branch=None,
                           note=f"손으로 이미 실행: {sha[:8]}")
                closed += 1
            else:
                untouched += 1

    conn.close()
    print(f"sync 완료: {closed}건 닫음, {untouched}건 건드리지 않음")


def list_creations(project: str) -> None:
    conn = connect()
    pid = db.get_project(conn, project)["id"]
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
    p6 = sub.add_parser("sync"); p6.add_argument("project")
    p6.add_argument("--repo", required=True)
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
    elif a.cmd == "sync":
        sync(a.project, a.repo)
