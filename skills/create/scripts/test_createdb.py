#!/usr/bin/env python3
"""create 스킬 Brain 브리지 자체점검 — `python test_createdb.py` (임시 폴더에서만 돈다).

pick → claim → done → list 한 바퀴가 실제로 도는지, 그리고 루프 클로즈(기회가
done으로 닫히는지)를 본다. 이 경로가 깨지면 capture가 다음 런에서 "이미 처리한
기회"를 계속 새 기회로 들고 온다.
"""
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
oid = conn.execute("SELECT id FROM opportunities").fetchone()[0]
conn.close()

rows = json.loads(run("pick", "t"))
assert len(rows) == 1 and rows[0]["target"] == "ai 티어표", rows

run("claim", "t", str(oid))
conn = createdb.connect()
assert conn.execute("SELECT status FROM opportunities WHERE id=?",
                    (oid,)).fetchone()[0] == "acked"
conn.close()

run("done", "t", str(oid), "--path", "src/app/[locale]/page.tsx",
    "--branch", "capture/striking-ko-title", "--note", "메타 정렬")
conn = createdb.connect()
assert conn.execute("SELECT status FROM opportunities WHERE id=?",
                    (oid,)).fetchone()[0] == "done", "루프가 안 닫혔다"
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

print(f"ok — pick·claim·done·list·merged 정상, 루프 닫힘 확인 ({HOME})")
