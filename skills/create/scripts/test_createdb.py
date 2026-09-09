#!/usr/bin/env python3
"""create 스킬 Brain 브리지 자체점검 — `python test_createdb.py` (임시 폴더에서만 돈다).

pick → claim → done → list 한 바퀴가 실제로 도는지, 그리고 루프 클로즈(기회가
done으로 닫히는지)를 본다. 이 경로가 깨지면 capture가 다음 런에서 "이미 처리한
기회"를 계속 새 기회로 들고 온다.
"""
import io
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HOME = Path(tempfile.mkdtemp(prefix="seo-miner-create-test-"))
os.environ["CAPTURE_HOME"] = str(HOME)
HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.resolve().parents[1] / "capture" / "scripts"))
import createdb  # noqa: E402
import db  # noqa: E402  (capture 쪽 스키마로 Brain을 만든다)


def run(*args) -> str:
    """CLI로 돌린다 — 콘솔 인코딩까지 같이 검증하려면 import 호출로는 부족하다."""
    p = subprocess.run([sys.executable, str(HERE / "createdb.py"), *args],
                       capture_output=True, encoding="utf-8", errors="replace",
                       env={**os.environ, "PYTHONIOENCODING": "cp949"})
    assert p.returncode == 0, f"{args} 실패:\n{p.stdout}\n{p.stderr}"
    return p.stdout


conn = db.connect()
conn.execute("INSERT INTO projects(name, type, domain) VALUES('t','saas','t.com')")
pid = conn.execute("SELECT id FROM projects WHERE name='t'").fetchone()[0]
conn.execute(
    """INSERT INTO opportunities(project_id, kind, target, score, reasoning, status)
       VALUES(?,?,?,?,?,'new')""",
    (pid, "striking_distance", "ai 티어표", 84.0,
     "평균 5.7위·노출 42·클릭 0 — 제목 미정렬"))   # em dash: cp949에서 죽던 그 문자
conn.commit()
import scoring  # noqa: E402
db.set_verdicts(conn, pid, [scoring.norm("ai 티어표")], "work")   # pick 은 심사를 통과한 것만 본다
oid = conn.execute("SELECT id FROM opportunities").fetchone()[0]
conn.close()

rows = json.loads(run("pick", "t"))
assert len(rows) == 1 and rows[0]["target"] == "ai 티어표", rows

rows_filtered = json.loads(run("pick", "t", "--kinds", "striking_distance"))
assert len(rows_filtered) == 1 and rows_filtered[0]["target"] == "ai 티어표", rows_filtered

rows_empty = json.loads(run("pick", "t", "--kinds", "ctr_gap,cannibalization"))
assert len(rows_empty) == 0, rows_empty

run("claim", "t", str(oid))
conn = createdb.connect()
assert conn.execute("SELECT status FROM opportunities WHERE id=?",
                    (oid,)).fetchone()[0] == "acked"
conn.close()

run("done", "t", str(oid), "--path", "src/app/[locale]/page.tsx",
    "--branch", "capture/striking-ko-title", "--note", "메타 정렬")
conn = createdb.connect()
# done 은 기록만 남기고 상태는 진행 중이다 — 완료는 대시보드에서 관찰 뒤 사람이 누른다
assert conn.execute("SELECT status FROM opportunities WHERE id=?",
                    (oid,)).fetchone()[0] == "acked", "기록 뒤 상태가 진행 중이 아니다"
c = conn.execute("SELECT opportunity_id, file_path, branch, merged FROM creations"
                 ).fetchone()
assert tuple(c) == (oid, "src/app/[locale]/page.tsx", "capture/striking-ko-title", 0), c
cid = conn.execute("SELECT id FROM creations").fetchone()[0]
conn.close()

assert "src/app/[locale]/page.tsx" in run("list", "t")
run("merged", str(cid))
conn = createdb.connect()
assert conn.execute("SELECT merged FROM creations WHERE id=?", (cid,)).fetchone()[0] == 1
conn.close()

# sync 검증: 임시 git repo에 commit 만들고 sync 돌리기
repo_dir = Path(tempfile.mkdtemp(prefix="seo-miner-git-test-"))
conn = createdb.connect()
conn.execute(
    """INSERT INTO opportunities(project_id, kind, target, score, reasoning, status)
       VALUES(?,?,?,?,?,'new')""",
    (pid, "striking_distance", "새 기회", 90.0, "테스트 기회"))
conn.commit()
oid2 = conn.execute("SELECT id FROM opportunities WHERE target='새 기회'").fetchone()[0]
conn.close()

subprocess.run(["git", "init", str(repo_dir)], check=True, capture_output=True)
subprocess.run(
    ["git", "-C", str(repo_dir), "-c", "user.email=t@t", "-c", "user.name=t",
     "commit", "--allow-empty", "-m", f"capture(fix): x [opp #{oid2}]"],
    check=True, capture_output=True)

out = run("sync", "t", "--repo", str(repo_dir))
assert "1건 기록" in out, out

conn = createdb.connect()
assert conn.execute("SELECT status FROM opportunities WHERE id=?",
                    (oid2,)).fetchone()[0] == "acked", "sync 가 기회를 진행 중으로 안 옮겼다"
c2 = conn.execute("SELECT opportunity_id, note FROM creations WHERE opportunity_id=?",
                  (oid2,)).fetchone()
assert c2 is not None and "손으로 이미 실행:" in (c2["note"] or ""), c2
conn.close()

# ── 원격 사이트: Brain 대신 서버 창구를 쓴다 ──────────────────────────────
# run() 은 서브프로세스라 monkeypatch 가 안 닿는다 — 여기만 createdb 를 직접 부른다.
import contextlib  # noqa: E402
import remote  # noqa: E402  (capture/scripts 는 이미 sys.path 에 있다)

calls = []
_orig_owns, _orig_api = remote.owns, remote.api
remote.owns = lambda p: p == "web"
remote.api = lambda method, path, **kw: (calls.append((method, path, kw)) or
    {"opps": [{"id": 7, "kind": "ctr_gap", "target": "k", "status": "new", "score": 1},
              {"id": 8, "kind": "ctr_gap", "target": "닫힌 것", "status": "done", "score": 9}],
     "creations": [{"id": 3, "file_path": "a.md"}],
     "creation_id": 3, "status": "acked", "updated": 1})


def _out(fn, *args, **kw) -> str:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn(*args, **kw)
    return buf.getvalue()


try:
    o = _out(createdb.pick, "web", None, 10)
    assert '"id": 7' in o and '"id": 8' not in o, o   # 열린 것만 (new|acked)
    assert calls[-1][0] == "GET" and calls[-1][1] == "/api/data", calls[-1]
    assert calls[-1][2]["params"]["project"] == "web", calls[-1]

    _out(createdb.claim, "web", [7])
    assert calls[-1][1] == "/api/opp" and calls[-1][2]["json"]["status"] == "acked", calls[-1]
    assert calls[-1][2]["json"]["project"] == "web", calls[-1]

    _out(createdb.done, "web", 7, "a.md", "capture/ctr_gap-k", None)
    assert calls[-1][1] == "/api/creation", calls[-1]
    assert calls[-1][2]["json"]["opportunity_id"] == 7, calls[-1]
    assert calls[-1][2]["json"]["path"] == "a.md", calls[-1]

    o = _out(createdb.list_creations, "web")
    assert '"file_path": "a.md"' in o, o
    assert calls[-1][1] == "/api/data", calls[-1]

    try:
        createdb.merged(3, "web")
        raise AssertionError("원격 머지 표시가 그냥 통과했다")
    except SystemExit:
        pass
finally:
    remote.owns, remote.api = _orig_owns, _orig_api

print(f"ok — pick·claim·done·list·merged·sync 정상, 원격 분기 확인 ({HOME})")

