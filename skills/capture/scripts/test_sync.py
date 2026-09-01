#!/usr/bin/env python3
"""동기화 모듈 (sync.py) 단위 테스트 — 웹 ↔ 로컬 설정 및 실적 데이터 패키징/복원."""
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

import db
import sync

# 임시 디렉터리로 CAPTURE_HOME 격리
TMP = tempfile.TemporaryDirectory(prefix="seo-miner-sync-test-")
HOME = Path(TMP.name)
os.environ["CAPTURE_HOME"] = str(HOME)
db.init_db()

conn = db.connect()

# 1. 원본 프로젝트 세팅
pdir = HOME / "projects"
pdir.mkdir(parents=True, exist_ok=True)
yaml_text = (
    "name: syncdemo\n"
    "type: saas\n"
    "domain: syncdemo.com\n"
    "locale: ko-KR\n"
    "brand_aliases: [동기화데모, SyncDemo]\n"
    "seed_keywords: [동기화1, 동기화2]\n"
    "competitors_manual: [comp1.com]\n"
)
(pdir / "syncdemo.yaml").write_text(yaml_text, "utf-8")
db.sync_project(str(pdir / "syncdemo.yaml"))

pid = conn.execute("SELECT id FROM projects WHERE name='syncdemo'").fetchone()[0]

# 샘플 데이터 삽입
conn.execute(
    "INSERT INTO keywords(project_id, keyword, cluster, intent, volume, source, is_active) "
    "VALUES(?, '캔키워드', '클러스터A', 'info', 1200, 'autocomplete', 1)", (pid,))
conn.execute(
    "INSERT INTO gsc_snapshots(project_id, snapshot_date, period_days, query, page, clicks, impressions, ctr, position) "
    "VALUES(?, '2026-09-01', 28, '검색어1', 'https://syncdemo.com/p1', 10, 200, 0.05, 3.2)", (pid,))
conn.execute(
    "INSERT INTO gsc_daily(project_id, date, clicks, impressions, ctr, position) "
    "VALUES(?, '2026-08-30', 5, 100, 0.05, 3.0)", (pid,))
conn.execute(
    "INSERT INTO opportunities(project_id, kind, target, score, status, reasoning) "
    "VALUES(?, 'striking_distance', '검색어1', 85.0, 'new', '순위 상승 기회')", (pid,))
conn.commit()

# 2. 내보내기 (Export) 검증
pkg = sync.export_package(conn, "syncdemo")
assert pkg["schema"] == 1
assert pkg["type"] == "seo-miner-sync-package"
assert pkg["project"] == "syncdemo"
assert "SyncDemo" in pkg["yaml_raw"]
assert len(pkg["data"]["keywords"]) == 3  # 씨앗 2개 + 캔키워드 1개
assert len(pkg["data"]["gsc_snapshots"]) == 1
assert len(pkg["data"]["gsc_daily"]) == 1
assert len(pkg["data"]["opportunities"]) == 1

# 3. 새로운 격리 환경으로 가져오기 (Import) 검증
TMP2 = tempfile.TemporaryDirectory(prefix="seo-miner-sync-dest-")
HOME2 = Path(TMP2.name)
os.environ["CAPTURE_HOME"] = str(HOME2)
db.init_db()

conn2 = db.connect()
res = sync.import_package(conn2, pkg)
assert res["ok"] is True, res
assert res["project"] == "syncdemo"

# 복원된 프로젝트 확인
p2 = db.get_project(conn2, "syncdemo")
assert p2["domain"] == "syncdemo.com"
pid2 = p2["id"]

# 복원된 YAML 파일 확인
assert (HOME2 / "projects" / "syncdemo.yaml").exists()

# 복원된 데이터 확인
kw_rows = conn2.execute("SELECT keyword, is_active FROM keywords WHERE project_id=? ORDER BY keyword", (pid2,)).fetchall()
assert len(kw_rows) == 3
gsc_rows = conn2.execute("SELECT query, clicks, impressions FROM gsc_snapshots WHERE project_id=?", (pid2,)).fetchall()
assert len(gsc_rows) == 1 and gsc_rows[0][0] == "검색어1" and gsc_rows[0][1] == 10
opp_rows = conn2.execute("SELECT kind, target, score FROM opportunities WHERE project_id=?", (pid2,)).fetchall()
assert len(opp_rows) == 1 and opp_rows[0][1] == "검색어1"

# 4. 멱등성 (동일 패키지 재가져오기 시 중복 없음 검증)
res_re = sync.import_package(conn2, pkg)
assert res_re["ok"] is True
assert conn2.execute("SELECT COUNT(*) FROM keywords WHERE project_id=?", (pid2,)).fetchone()[0] == 3
assert conn2.execute("SELECT COUNT(*) FROM gsc_snapshots WHERE project_id=?", (pid2,)).fetchone()[0] == 1
assert conn2.execute("SELECT COUNT(*) FROM opportunities WHERE project_id=?", (pid2,)).fetchone()[0] == 1

conn.close()
conn2.close()
TMP.cleanup()
TMP2.cleanup()
print("ok — sync package export / import / merge all verified.")
