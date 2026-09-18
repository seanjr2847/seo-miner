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

# ── 머지 반영: gh 가 머지를 말하면 기록은 merged, 기회는 done(완료 시각 = 머지 시각) ──
# 진짜 gh 를 부르지 않는다 — createdb._gh 를 갈아끼운다.
import contextlib  # noqa: E402

conn = createdb.connect()
m_opp = {}
for t, st in (("머지된 기회", "acked"), ("안 머지된 기회", "acked"), ("뺀 기회", "dismissed"),
              ("저절로 풀린 기회", "resolved")):
    m_opp[t] = conn.execute(
        "INSERT INTO opportunities(project_id, kind, target, score, status) "
        "VALUES(?, 'ctr_gap', ?, 1, ?) RETURNING id", (pid, t, st)).fetchone()[0]
conn.commit()
m_cre = {t: db.record_creation(conn, pid, "x.md", opportunity_id=m_opp[t],
                               branch=f"capture/ctr_gap-{i}")
         for i, t in enumerate(m_opp)}
conn.close()
MERGED = {"capture/ctr_gap-0": 41, "capture/ctr_gap-2": 42, "capture/ctr_gap-3": 43}
gh_calls = []


def _fake_gh(args, cwd):
    gh_calls.append((args, cwd))
    br = args[args.index("--head") + 1]
    rows = ([{"number": MERGED[br], "mergedAt": "2026-09-05T03:04:05Z"}]
            if br in MERGED else [])
    return subprocess.CompletedProcess(["gh", *args], 0, json.dumps(rows), "")


def _quiet(fn, *a, **kw):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        r = fn(*a, **kw)
    return r, buf.getvalue()


_orig_gh = createdb._gh
try:
    createdb._gh = _fake_gh
    res, _ = _quiet(createdb.sync_merged, "t", str(repo_dir))
    assert res["checked"] == 4 and res["merged"] == 3 and res["closed"] == 2, res
    assert gh_calls[0][1] == str(repo_dir), "gh 를 사이트 리포에서 부르지 않았다"
    conn = createdb.connect()
    st = {t: tuple(conn.execute("SELECT status, status_at FROM opportunities WHERE id=?",
                                (m_opp[t],)).fetchone()) for t in m_opp}
    mg = {t: conn.execute("SELECT merged FROM creations WHERE id=?", (m_cre[t],)).fetchone()[0]
          for t in m_cre}
    conn.close()
    assert st["머지된 기회"] == ("done", "2026-09-05 03:04:05"), st      # 완료 시각 = 머지 시각
    assert st["저절로 풀린 기회"][0] == "done", "머지가 있는데 저절로 풀림으로 남았다"
    assert st["안 머지된 기회"][0] == "acked", st
    assert st["뺀 기회"][0] == "dismissed", "사람이 뺀 기회를 머지로 되살렸다"
    assert mg == {"머지된 기회": 1, "안 머지된 기회": 0, "뺀 기회": 1, "저절로 풀린 기회": 1}, mg
    # 완료 후 관찰(우리 효과)은 done 만 본다 — 머지로 닫힌 것이 여기 잡혀야 한다
    conn = createdb.connect()
    wt = {w["target"] for w in db.watch_rows(conn, pid)}
    conn.close()
    assert "머지된 기회" in wt, wt

    # 거는 자리: /create plan 첫머리의 `createdb.py sync {P} --repo .` 가 머지 반영까지 한다
    conn = createdb.connect()
    c_late = db.record_creation(conn, pid, "y.md", opportunity_id=m_opp["안 머지된 기회"],
                                branch="capture/ctr_gap-2")        # MERGED 에 있는 브랜치
    conn.close()
    n_calls = len(gh_calls)
    _quiet(createdb.sync, "t", str(repo_dir))
    assert len(gh_calls) > n_calls, "sync 가 머지 반영을 부르지 않는다"
    conn = createdb.connect()
    assert conn.execute("SELECT merged FROM creations WHERE id=?", (c_late,)).fetchone()[0] == 1
    assert conn.execute("SELECT status FROM opportunities WHERE id=?",
                        (m_opp["안 머지된 기회"],)).fetchone()[0] == "done"
    conn.close()

    # gh 가 없거나 로그인이 안 됐으면 한 줄 알리고 건너뛴다 — 실패가 아니고 아무것도 안 바꾼다
    def _no_gh(args, cwd):
        raise FileNotFoundError("gh")

    def _no_login(args, cwd):
        return subprocess.CompletedProcess(["gh"], 4, "", "To get started with GitHub CLI, "
                                           "please run:  gh auth login\n")
    for fake in (_no_gh, _no_login):
        createdb._gh = fake
        res, out = _quiet(createdb.sync_merged, "t", str(repo_dir))
        assert res["skipped"] and res["merged"] == 0 and res["checked"] == 1, res
        assert out.count("\n") == 1 and "건너뜀" in out, out
    conn = createdb.connect()
    assert conn.execute("SELECT merged FROM creations WHERE id=?",
                        (m_cre["안 머지된 기회"],)).fetchone()[0] == 0, "gh 없이 머지로 적었다"
    conn.close()
finally:
    createdb._gh = _orig_gh

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

    # 원격 머지 반영: 기록은 서버에 있다 — 머지면 기회를 /api/opp 로 done 으로 옮긴다.
    # 화면 목록에 없는 기회(상태 모름)는 덮지 않는다.
    remote.api = lambda method, path, **kw: (calls.append((method, path, kw)) or
        {"opps": [{"id": 7, "kind": "ctr_gap", "target": "k", "status": "acked"}],
         "creations": [{"id": 3, "branch": "capture/ctr_gap-k", "merged": 0, "opportunity_id": 7},
                       {"id": 4, "branch": "capture/ctr_gap-z", "merged": 0, "opportunity_id": 99},
                       {"id": 5, "branch": "", "merged": 0, "opportunity_id": 7}]})
    createdb._gh = lambda args, cwd: subprocess.CompletedProcess(
        ["gh"], 0, json.dumps([{"number": 5, "mergedAt": "2026-09-05T00:00:00Z"}]), "")
    calls.clear()
    res, _ = _quiet(createdb.sync_merged, "web", None)
    posts = [c for c in calls if c[0] == "POST"]
    assert res["checked"] == 2 and res["closed"] == 1, res
    # 기록의 merged 도 서버에서 켠다(/api/creation/merged) — 안 켜면 그 기록이 다음
    # sync 의 목록에 그대로 남아 같은 PR 을 gh 에 영영 다시 묻고, 화면의 「고친 것」은
    # '병합 전'에 굳는다. 기회(/api/opp)까지 둘 다 부르는지 순서대로 본다.
    assert [(p[1], p[2]["json"]) for p in posts] == [
        ("/api/creation/merged", {"project": "web", "id": 3}),
        ("/api/opp", {"project": "web", "id": 7, "status": "done"}),
        ("/api/creation/merged", {"project": "web", "id": 4}),
    ], posts
finally:
    remote.owns, remote.api = _orig_owns, _orig_api
    createdb._gh = _orig_gh

print(f"ok — pick·claim·done·list·merged·sync 정상, 원격 분기 확인 ({HOME})")

