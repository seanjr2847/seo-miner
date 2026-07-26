#!/usr/bin/env python3
"""Brain: SQLite layer for the capture skill.

State lives OUTSIDE the skill folder so skill updates never touch data.
  CAPTURE_HOME (default ~/.capture)
    ├── brain.db
    ├── projects/*.yaml
    ├── reports/{project}/{date}.html
    └── gsc_token.json / client_secrets.json

CLI:
  python db.py init
  python db.py sync-project <path/to/project.yaml>
  python db.py stats <project>
  python db.py sql "SELECT ..."        # read-only, for Brain queries
"""
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

CAPTURE_HOME = Path(os.environ.get("CAPTURE_HOME", Path.home() / ".capture"))
DB_PATH = Path(os.environ.get("CAPTURE_DB", CAPTURE_HOME / "brain.db"))

# 한국어 Windows 콘솔(cp949)에서 '—'·'✓' 출력이 UnicodeEncodeError로 죽는 것 방지.
# db는 모든 스크립트가 import하므로 여기 한 번이면 전부 커버된다(doctor는 예외 — 자체 처리).
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):  # 파이프로 감싼 경우 등
        pass

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
  id INTEGER PRIMARY KEY,
  name TEXT UNIQUE NOT NULL,
  type TEXT NOT NULL DEFAULT 'saas',          -- game|local_clinic|saas|directory
  domain TEXT NOT NULL,
  locale TEXT DEFAULT 'ko-KR',
  gsc_property TEXT,                          -- e.g. sc-domain:example.com
  config_path TEXT,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS keywords (
  id INTEGER PRIMARY KEY,
  project_id INTEGER NOT NULL REFERENCES projects(id),
  keyword TEXT NOT NULL,
  cluster TEXT,
  intent TEXT,                                -- info|commercial|transactional|navigational
  volume INTEGER,                             -- NULL = unknown (free pipeline)
  difficulty REAL,
  source TEXT DEFAULT 'seed',                 -- seed|autocomplete|gsc|competitor|claude
  is_active INTEGER DEFAULT 0,                -- 1 = tracked set (curated by Claude+user)
  added_at TEXT DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(project_id, keyword)
);
CREATE TABLE IF NOT EXISTS rank_snapshots (   -- reserved for SERP adapter (v2)
  id INTEGER PRIMARY KEY,
  keyword_id INTEGER NOT NULL REFERENCES keywords(id),
  checked_at TEXT DEFAULT CURRENT_TIMESTAMP,
  position INTEGER,
  url TEXT,
  serp_features_json TEXT,
  aio_present INTEGER,
  aio_cited INTEGER
);
CREATE TABLE IF NOT EXISTS gsc_snapshots (
  id INTEGER PRIMARY KEY,
  project_id INTEGER NOT NULL REFERENCES projects(id),
  snapshot_date TEXT NOT NULL,                -- YYYY-MM-DD (collection date)
  period_days INTEGER NOT NULL,
  query TEXT NOT NULL,
  page TEXT,
  clicks INTEGER,
  impressions INTEGER,
  ctr REAL,
  position REAL
);
CREATE INDEX IF NOT EXISTS idx_gsc_proj_date ON gsc_snapshots(project_id, snapshot_date);
CREATE TABLE IF NOT EXISTS ai_prompts (
  id INTEGER PRIMARY KEY,
  project_id INTEGER NOT NULL REFERENCES projects(id),
  prompt TEXT NOT NULL,
  category TEXT DEFAULT 'general',            -- 추천|비교|문제해결|브랜드|general
  is_active INTEGER DEFAULT 1,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(project_id, prompt)
);
CREATE TABLE IF NOT EXISTS ai_checks (
  id INTEGER PRIMARY KEY,
  prompt_id INTEGER NOT NULL REFERENCES ai_prompts(id),
  run_id INTEGER REFERENCES runs(id),
  engine TEXT NOT NULL,                       -- chatgpt|perplexity|gemini|claude
  sample_idx INTEGER DEFAULT 0,               -- answers are non-deterministic: sample #
  checked_at TEXT DEFAULT CURRENT_TIMESTAMP,
  mentioned INTEGER DEFAULT 0,                -- brand alias appears in answer text
  cited INTEGER DEFAULT 0,                    -- own domain appears in citations
  cited_domains_json TEXT,                    -- who got cited instead/alongside
  answer_excerpt TEXT                         -- short excerpt only (verification aid)
);
CREATE INDEX IF NOT EXISTS idx_ai_checks_run ON ai_checks(run_id);
CREATE TABLE IF NOT EXISTS competitors (
  id INTEGER PRIMARY KEY,
  project_id INTEGER NOT NULL REFERENCES projects(id),
  domain TEXT NOT NULL,
  source TEXT DEFAULT 'manual',               -- manual|auto_ai|auto_serp
  added_at TEXT DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(project_id, domain)
);
CREATE TABLE IF NOT EXISTS opportunities (
  id INTEGER PRIMARY KEY,
  project_id INTEGER NOT NULL REFERENCES projects(id),
  run_id INTEGER REFERENCES runs(id),
  kind TEXT NOT NULL,   -- striking_distance|ai_citation_gap|rank_decay|content_gap|coverage|pseo_pattern|aio_exposure
  target TEXT NOT NULL,                       -- keyword / prompt / page
  score REAL,
  reasoning TEXT,                             -- Claude-written, grounded in Brain data
  status TEXT DEFAULT 'new',                  -- new|acked|done|dismissed
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY,
  project_id INTEGER NOT NULL REFERENCES projects(id),
  kind TEXT NOT NULL,                         -- gsc|ai|keywords|analysis|report|full
  started_at TEXT DEFAULT CURRENT_TIMESTAMP,
  finished_at TEXT,
  api_calls INTEGER DEFAULT 0,
  cost_estimate_usd REAL DEFAULT 0,
  notes TEXT
);
"""


def connect() -> sqlite3.Connection:
    CAPTURE_HOME.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # ponytail: 스키마가 전부 IF NOT EXISTS라 매 연결마다 보장해도 공짜 —
    # 덕분에 사용자가 'db.py init'을 먼저 칠 일이 없다.
    conn.executescript(SCHEMA)
    return conn


def init_db() -> None:
    conn = connect()
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()
    print(f"Brain ready: {DB_PATH}")


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def get_project(conn: sqlite3.Connection, name: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM projects WHERE name=?", (name,)).fetchone()
    if not row:
        sys.exit(f"project '{name}' not found. Run: python db.py sync-project <yaml>")
    return row


def load_project_yaml(name_or_path: str) -> dict:
    import yaml  # lazy
    p = Path(name_or_path)
    if not p.exists():
        p = CAPTURE_HOME / "projects" / f"{name_or_path}.yaml"
    if not p.exists():
        sys.exit(f"project yaml not found: {name_or_path}")
    with open(p, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    cfg["_path"] = str(p)
    return cfg


def sync_project(yaml_path: str) -> None:
    cfg = load_project_yaml(yaml_path)
    conn = connect()
    conn.execute(
        """INSERT INTO projects(name,type,domain,locale,gsc_property,config_path)
           VALUES(?,?,?,?,?,?)
           ON CONFLICT(name) DO UPDATE SET type=excluded.type, domain=excluded.domain,
             locale=excluded.locale, gsc_property=excluded.gsc_property,
             config_path=excluded.config_path""",
        (cfg["name"], cfg.get("type", "saas"), cfg["domain"], cfg.get("locale", "ko-KR"),
         cfg.get("gsc_property"), cfg["_path"]),
    )
    pid = conn.execute("SELECT id FROM projects WHERE name=?", (cfg["name"],)).fetchone()[0]
    for kw in cfg.get("seed_keywords", []) or []:
        conn.execute(
            """INSERT OR IGNORE INTO keywords(project_id,keyword,source,is_active)
               VALUES(?,?, 'seed', 1)""", (pid, kw.strip()))
    for dom in cfg.get("competitors_manual", []) or []:
        conn.execute(
            "INSERT OR IGNORE INTO competitors(project_id,domain,source) VALUES(?,?, 'manual')",
            (pid, dom.strip().lower()))
    conn.commit()
    n_kw = conn.execute("SELECT COUNT(*) FROM keywords WHERE project_id=?", (pid,)).fetchone()[0]
    print(f"synced project '{cfg['name']}' (id={pid}, keywords={n_kw})")
    conn.close()


def start_run(conn: sqlite3.Connection, project_id: int, kind: str) -> int:
    cur = conn.execute("INSERT INTO runs(project_id,kind,started_at) VALUES(?,?,?)",
                       (project_id, kind, now()))
    conn.commit()
    return cur.lastrowid


def finish_run(conn, run_id: int, api_calls: int = 0, cost: float = 0.0, notes: str = "") -> None:
    conn.execute(
        "UPDATE runs SET finished_at=?, api_calls=?, cost_estimate_usd=?, notes=? WHERE id=?",
        (now(), api_calls, round(cost, 4), notes, run_id))
    conn.commit()


def stats(project: str) -> None:
    conn = connect()
    p = get_project(conn, project)
    q = lambda s, a=(): conn.execute(s, a).fetchone()[0]  # noqa: E731
    print(json.dumps({
        "project": p["name"], "type": p["type"], "domain": p["domain"],
        "keywords_active": q("SELECT COUNT(*) FROM keywords WHERE project_id=? AND is_active=1", (p["id"],)),
        "keywords_candidates": q("SELECT COUNT(*) FROM keywords WHERE project_id=? AND is_active=0", (p["id"],)),
        "ai_prompts_active": q("SELECT COUNT(*) FROM ai_prompts WHERE project_id=? AND is_active=1", (p["id"],)),
        "gsc_snapshot_dates": [r[0] for r in conn.execute(
            "SELECT DISTINCT snapshot_date FROM gsc_snapshots WHERE project_id=? ORDER BY 1 DESC LIMIT 5",
            (p["id"],))],
        "ai_runs": q("SELECT COUNT(*) FROM runs WHERE project_id=? AND kind='ai'", (p["id"],)),
        "opportunities_new": q("SELECT COUNT(*) FROM opportunities WHERE project_id=? AND status='new'", (p["id"],)),
    }, ensure_ascii=False, indent=2))
    conn.close()


def run_sql(query: str) -> None:
    """Read-only ad-hoc queries. This is how Claude consults the Brain."""
    if not query.strip().lower().startswith(("select", "with")):
        sys.exit("read-only: only SELECT/WITH queries allowed here")
    conn = connect()
    rows = conn.execute(query).fetchall()
    print(json.dumps([dict(r) for r in rows], ensure_ascii=False, indent=2, default=str))
    conn.close()


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        sys.exit(__doc__)
    cmd = args[0]
    if cmd == "init":
        init_db()
    elif cmd == "sync-project" and len(args) > 1:
        sync_project(args[1])
    elif cmd == "stats" and len(args) > 1:
        stats(args[1])
    elif cmd == "sql" and len(args) > 1:
        run_sql(args[1])
    else:
        sys.exit(__doc__)
