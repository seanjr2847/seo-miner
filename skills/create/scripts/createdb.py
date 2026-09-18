#!/usr/bin/env python3
"""create skill <-> shared Brain bridge.

Opens the SAME brain.db as the capture skill ($CAPTURE_HOME, default ~/.capture)
through capture의 db 모듈. Read/claim opportunities, record what was written
where, and close the loop so the next capture run measures it.

Standalone-safe: if brain.db doesn't exist, commands explain and exit cleanly
(create then runs in manual-brief mode without Brain).

호스팅에 등록한 사이트(`remote.owns`)는 Brain 이 서버에 있다. 그때는 같은 명령이
로컬 sqlite 대신 서버 창구(`/api/data`·`/api/opp`·`/api/creation[/merged]`)를 쓴다 — 그래서
요청문 꼬리의 기록 명령 한 줄은 로컬·호스팅 구분 없이 똑같다.

CLI:
  python createdb.py pick   <project> [--kinds a,b] [--limit 10]
  python createdb.py claim  <project> <id> [<id> ...]
  python createdb.py done   <project> <id> --path PATH [--branch BR] [--note N]
  python createdb.py list   <project>
  python createdb.py merged <creation_id> [--project P]
  python createdb.py sync   <project> --repo PATH   # [opp #id] 커밋 기록 + PR 머지 반영
"""
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "capture" / "scripts"))
import db  # noqa: E402
import remote  # noqa: E402  (호스팅 사이트면 Brain 대신 서버 창구를 쓴다)

# creations 테이블·경로 규칙·콘솔 인코딩은 전부 db가 갖는다. 여기서 따로 CREATE TABLE을
# 하던 시절엔 create가 capture의 Brain 안에 제 테이블을 심는 꼴이라, dashboard가 그
# 테이블을 셀 때마다 try/except로 없을 가능성을 감싸야 했다.

# SKILL.md가 정한 브랜치 이름 규칙. 어긋나도 막지는 않는다 — 작업 중간에
# 브랜치 이름 하나로 사용자를 세우는 건 손해가 더 크다.
BRANCH_RE = re.compile(r"^capture/\w+-\S+$")


def _remote(project: str) -> bool:
    """이 사이트는 호스팅이 갖고 있나 — 판정은 remote.owns 하나다(대시보드와 같다)."""
    return bool(project) and remote.owns(project)


def _payload(project: str) -> dict:
    """호스팅 사이트의 화면 페이로드 — 대시보드가 보는 것과 같은 /api/data."""
    return remote.api("GET", "/api/data", params={"project": project}) or {}


def _warn_branch(branch: str | None) -> None:
    if branch and not BRANCH_RE.match(branch):
        print(f"경고: 브랜치 '{branch}' 가 규칙과 다릅니다 — 기대 형식 "
              "capture/{kind}-{slug} (기록은 그대로 진행합니다)", file=sys.stderr)


def _record_remote(project: str, opp_id: int | None, path: str,
                   branch: str | None, note: str | None) -> None:
    """호스팅 기록 창구 — 로컬 _mark_done 과 같은 일을 서버가 한다
    (dashboard.record_creation_route 가 양쪽의 본체다)."""
    remote.api("POST", "/api/creation",
               json={"project": project, "opportunity_id": opp_id, "path": path,
                     "branch": branch, "note": note})


def connect():
    if not db.DB_PATH.exists():
        sys.exit(f"brain.db not found at {db.DB_PATH} — capture 미설치 상태. "
                 "create는 수동 브리프 모드로 진행 가능(SKILL.md 참조).")
    return db.connect()


def pick(project: str, kinds: str | None, limit: int) -> None:
    kinds_list = [k.strip() for k in kinds.split(",") if k.strip()] if kinds else None
    if _remote(project):
        rows = [o for o in (_payload(project).get("opps") or [])
                if o.get("status") in ("new", "acked")
                and (not kinds_list or o.get("kind") in kinds_list)]
        print(json.dumps(rows[:limit], ensure_ascii=False, indent=2))
        return
    conn = connect()
    pid = db.get_project(conn, project)["id"]
    rows = [dict(r) for r in db.open_opportunities(conn, pid, kinds=kinds_list, limit=limit)]
    print(json.dumps(rows, ensure_ascii=False, indent=2))
    conn.close()


def claim(project: str, ids: list[int]) -> None:
    if _remote(project):
        for i in ids:
            remote.api("POST", "/api/opp",
                       json={"project": project, "id": int(i), "status": "acked"})
        print(f"claimed {len(ids)} opportunities (status=acked)")
        return
    conn = connect()
    pid = db.get_project(conn, project)["id"]
    for i in ids:
        db.set_opportunity_status(conn, i, "acked", project_id=pid)
    print(f"claimed {len(ids)} opportunities (status=acked)")
    conn.close()


def _mark_done(conn, pid: int, opp_id: int | None, path: str,
               branch: str | None, note: str | None) -> None:
    _warn_branch(branch)
    kind = None
    if opp_id:
        row = db.get_opportunity(conn, opp_id, project_id=pid)
        kind = row["kind"] if row else None
        # 기록은 남기되 상태는 진행 중(acked)이다 — 글 하나로 묶음 기회가 닫히면 안 되고,
        # 완료는 대시보드의 완료 후 관찰을 보고 사람이 누른다(호스팅 /api/creation 도 같다).
        db.set_opportunity_status(conn, opp_id, "acked", project_id=pid)
    db.record_creation(conn, pid, path, opportunity_id=opp_id, kind=kind,
                       branch=branch, note=note)


def done(project: str, opp_id: int | None, path: str,
         branch: str | None, note: str | None) -> None:
    if _remote(project):
        _warn_branch(branch)
        _record_remote(project, opp_id, path, branch, note)
        print(f"recorded: opp#{opp_id} -> {path} ({branch or 'no-branch'})")
        return
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

    # 커밋에서 뽑은 (기회 번호, sha) — 여기까지는 로컬·호스팅이 같다(git 은 이 PC 에 있다).
    hits = []
    for line in p.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        sha, _, _ = line.partition("\t")
        for x in re.findall(r"\[opp #(\d+)\]", line):
            hits.append((int(x), sha))

    closed = 0
    untouched = 0
    if _remote(project):
        status = {int(o["id"]): o.get("status")
                  for o in (_payload(project).get("opps") or []) if o.get("id") is not None}
        for opp_id, sha in hits:
            if status.get(opp_id) in ("new", "acked"):
                _record_remote(project, opp_id, "", None, f"손으로 이미 실행: {sha[:8]}")
                closed += 1
            else:
                untouched += 1
    else:
        conn = connect()
        pid = db.get_project(conn, project)["id"]
        for opp_id, sha in hits:
            row = db.get_opportunity(conn, opp_id, project_id=pid)
            if row and row["status"] in ("new", "acked"):
                _mark_done(conn, pid, opp_id, "", branch=None,
                           note=f"손으로 이미 실행: {sha[:8]}")
                closed += 1
            else:
                untouched += 1
        conn.close()
    print(f"sync 완료: {closed}건 기록(진행 중), {untouched}건 건드리지 않음")
    sync_merged(project, repo)


# ── 머지 반영 ──────────────────────────────────────────────────────────────
# 작업 기록(creations)의 merged 는 `createdb.py merged <id>` 를 손으로 쳐야만 1 이 됐다.
# SKILL.md 가 권하기만 해서 아무도 안 쳤고, PR 이 머지된 작업이 '진행 중'에 머물러
# 완료 후 관찰(db.watch_rows — done 만 본다)에 한 건도 안 잡혔다. 머지는 사람이 한
# 일이고(발행 게이트), 그 사실은 gh 가 안다 — 여기서 그걸 읽어 기록과 기회를 닫는다.

def _gh(args: list[str], cwd: str | None) -> subprocess.CompletedProcess:
    """gh 한 번. 검사가 이 함수를 갈아끼운다 — 진짜 gh·네트워크를 부르지 않게."""
    return subprocess.run(["gh", *args], cwd=cwd, capture_output=True,
                          encoding="utf-8", errors="replace")


class _GhUnavailable(Exception):
    """gh 가 없거나 로그인이 안 됐다 — 실패가 아니라 건너뛸 사유다."""


def _merged_pr(branch: str, cwd: str | None) -> dict | None:
    """그 브랜치의 머지된 PR {number, mergedAt}. 없으면 None."""
    try:
        p = _gh(["pr", "list", "--head", branch, "--state", "merged",
                 "--json", "number,mergedAt", "--limit", "1"], cwd)
    except FileNotFoundError:
        raise _GhUnavailable("gh 가 설치돼 있지 않습니다")
    if p.returncode != 0:
        first = ((p.stderr or p.stdout or "").strip().splitlines() or ["gh 실행 오류"])[0]
        raise _GhUnavailable(first[:160])
    try:
        rows = json.loads(p.stdout or "[]")
    except ValueError:
        raise _GhUnavailable("gh 출력이 JSON 이 아닙니다")
    return rows[0] if rows else None


def sync_merged(project: str, repo: str | None) -> dict:
    """머지를 아직 모르는 작업 기록마다 PR 머지를 확인해, 머지됐으면 기록을 merged 로,
    그 기회를 done 으로 닫는다(status_at = 머지 시각). gh 가 없거나 로그인이 안
    됐으면 한 줄 알리고 건너뛴다 — 실패로 치지 않는다(sync 의 나머지는 이미 끝났다).

    호스팅 사이트는 기록이 서버에 있다 — 기회 상태는 /api/opp 로, 기록의 merged 는
    /api/creation/merged 로 닫는다(로컬이 db.mark_creation_merged 를 부르는 자리다).
    기록을 닫아야 다음 sync 의 목록(merged=0 인 것)에서 빠진다 — 안 그러면 같은 PR 을
    gh 에 영영 다시 묻고, 화면의 「고친 것」은 계속 '병합 전'으로 남는다.
    """
    out = {"checked": 0, "merged": 0, "closed": 0, "skipped": ""}
    cwd = str(repo) if repo else None
    remote_site = _remote(project)
    if remote_site:
        d = _payload(project)
        status = {int(o["id"]): o.get("status")
                  for o in (d.get("opps") or []) if o.get("id") is not None}
        todo = [(c.get("id"), c.get("opportunity_id"), c.get("branch"))
                for c in (d.get("creations") or [])
                if not c.get("merged") and (c.get("branch") or "").strip()]
        conn = pid = None
    else:
        conn = connect()
        pid = db.get_project(conn, project)["id"]
        todo = [(r["id"], r["opportunity_id"], r["branch"])
                for r in db.unmerged_creations(conn, pid)]
    unknown = 0
    try:
        for cid, oid, branch in todo:
            out["checked"] += 1
            pr = _merged_pr(branch.strip(), cwd)
            if not pr:
                continue
            out["merged"] += 1
            reason = f"PR #{pr.get('number')} 머지됨 ({branch.strip()})"
            if remote_site:
                if cid is not None:   # 번호가 있어야 닫는다 — 없으면 기회만 닫고 넘어간다
                    remote.api("POST", "/api/creation/merged",
                               json={"project": project, "id": int(cid)})
                if not oid:
                    continue
                if status.get(int(oid)) in ("new", "acked", db.OPP_RESOLVED):
                    remote.api("POST", "/api/opp",
                               json={"project": project, "id": int(oid), "status": "done"})
                    status[int(oid)] = "done"
                    out["closed"] += 1
                elif int(oid) not in status:
                    unknown += 1     # 화면 목록에 없는 기회 — 상태를 모르면 덮지 않는다
            else:
                db.mark_creation_merged(conn, cid)
                if oid:
                    out["closed"] += db.close_opportunity_by_work(
                        conn, oid, pid, pr.get("mergedAt"), reason)
    except _GhUnavailable as e:
        out["skipped"] = str(e)
        print(f"머지 확인 건너뜀: {e} — `gh auth login` 뒤 다시 sync 하면 반영됩니다")
        return out
    finally:
        if conn is not None:
            conn.close()
    tail = f", 상태를 몰라 건너뜀 {unknown}건" if unknown else ""
    print(f"머지 확인: {out['checked']}건 중 {out['merged']}건 머지됨 → "
          f"기회 {out['closed']}건 완료{tail}")
    return out


def list_creations(project: str) -> None:
    if _remote(project):
        rows = list(_payload(project).get("creations") or [])[:30]
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return
    conn = connect()
    pid = db.get_project(conn, project)["id"]
    rows = [dict(r) for r in db.list_creations(conn, pid, limit=30)]
    print(json.dumps(rows, ensure_ascii=False, indent=2))
    conn.close()


def merged(creation_id: int, project: str | None = None) -> None:
    # 머지 표시는 로컬 Brain 만 한다 — 호스팅 사이트의 기록은 웹 화면이 갖고 있다.
    if project and _remote(project):
        sys.exit("웹에 등록한 사이트의 머지 표시는 대시보드에서 합니다")
    conn = connect()
    db.mark_creation_merged(conn, creation_id)
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
    p5.add_argument("--project")
    p6 = sub.add_parser("sync"); p6.add_argument("project")
    p6.add_argument("--repo", required=True)
    a = ap.parse_args()
    try:
        if a.cmd == "pick":
            pick(a.project, a.kinds, a.limit)
        elif a.cmd == "claim":
            claim(a.project, a.ids)
        elif a.cmd == "done":
            done(a.project, a.id, a.path, a.branch, a.note)
        elif a.cmd == "list":
            list_creations(a.project)
        elif a.cmd == "merged":
            merged(a.creation_id, a.project)
        elif a.cmd == "sync":
            sync(a.project, a.repo)
    except db.ProjectNotFound as e:
        sys.exit(str(e))
