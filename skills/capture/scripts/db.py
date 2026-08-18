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
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

CAPTURE_HOME = Path(os.environ.get("CAPTURE_HOME", Path.home() / ".capture"))
DB_PATH = Path(os.environ.get("CAPTURE_DB", CAPTURE_HOME / "brain.db"))

# 한국어 Windows 콘솔(cp949)에서 '—'·'✓' 출력이 UnicodeEncodeError로 죽는 것 방지.
# db는 모든 스크립트가 import하므로 여기 한 번이면 전부 커버된다 — doctor·connect_gsc·
# createdb 도 각자 갖고 있던 사본을 지우고 이 import에 기댄다.
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):  # 파이프로 감싼 경우 등
        pass


def load_env(path: Path = CAPTURE_HOME / "env") -> None:
    """~/.capture/env 의 KEY=VALUE를 환경변수로 — 대시보드 설정 화면이 여기 쓴다.
    셸에 이미 export한 값이 우선(setdefault). # ponytail: dotenv 패키지 대신 4줄."""
    for line in (path.read_text("utf-8").splitlines() if path.exists() else []):
        k, sep, v = line.partition("=")
        if sep and k.strip() and not k.lstrip().startswith("#"):
            os.environ.setdefault(k.strip(), v.strip())


load_env()


# ── 자료가 어디 사는가 ─────────────────────────────────────────────
# 경로 규칙은 여기서만 답한다. 예전에는 db·doctor·connect_gsc·createdb·collect_gsc가
# 각자 계산해서 CAPTURE_HOME 5벌·GSC 키 4벌이 돌아다녔고 다운로드 폴더는 이미 어긋나 있었다.

def gsc_key() -> Path:
    """구글 서비스 계정 키 (전 사이트 공용)."""
    return Path(os.environ.get("GOOGLE_APPLICATION_CREDENTIALS",
                               CAPTURE_HOME / "gsc_service_account.json"))


def creds_dir(project: str) -> Path:
    """레거시 사이트별 OAuth 자격증명 폴더 — 서비스 계정 키로 대체됐다."""
    return CAPTURE_HOME / "creds" / project


def downloads_dir() -> Path:
    """브라우저 다운로드 폴더 — GSC 서비스 계정 키(connect_gsc)를 여기서 찾는다."""
    return Path(os.environ.get("DOWNLOADS_DIR", Path.home() / "Downloads")).expanduser()

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
  locale TEXT,                                -- NULL = fall back to projects.locale
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
CREATE INDEX IF NOT EXISTS idx_rank_kw_date ON rank_snapshots(keyword_id, checked_at);
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
  answer_excerpt TEXT                         -- 답변 전문 (상한 8000자, record_ai_check)
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
  kind TEXT NOT NULL,   -- striking_distance|ai_citation_gap|rank_decay|content_gap|coverage|pseo_pattern|aio_exposure|ctr_gap|cannibalization
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
CREATE TABLE IF NOT EXISTS creations (      -- /create 가 실제로 고친 것 (루프 클로즈)
  id INTEGER PRIMARY KEY,
  project_id INTEGER NOT NULL REFERENCES projects(id),
  opportunity_id INTEGER,                     -- NULL = 수동 브리프 작업
  kind TEXT,
  file_path TEXT NOT NULL,
  branch TEXT,
  note TEXT,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  merged INTEGER DEFAULT 0
);
"""


def _migrate(conn: sqlite3.Connection) -> None:
    """IF NOT EXISTS가 못 하는 것만. 각 단계는 스스로 "이미 했나"를 보고 건너뛴다 —
    한 단계가 끝났다고 함수 전체를 return하면 그 뒤 단계가 영영 안 돈다.

    SCHEMA는 매 연결마다 재실행되지만 CREATE TABLE IF NOT EXISTS는 이미 존재하는
    테이블을 건드리지 않으므로, 컬럼을 새로 추가하면 기존 Brain에는 반영되지 않는다.
    """
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(keywords)")}
    if "locale" not in cols:
        conn.execute("ALTER TABLE keywords ADD COLUMN locale TEXT")
        # 한글이 든 키워드는 프로젝트 로케일이 무엇이든 en-US로 조회하면 안 된다.
        # aitierlist 실사용에서 프로젝트 로케일(en-US)이 한국어 키워드에도 적용돼
        # 실제 3~6위인 6개가 전부 "순위 없음"으로 적재됐다. 한글 포함 여부는
        # 모호하지 않은 신호라 최초 1회만 자동으로 채운다. 나머지는 NULL로 두고
        # 호출부가 프로젝트 로케일로 폴백한다.
        conn.execute(r"""UPDATE keywords SET locale='ko-KR'
                          WHERE locale IS NULL
                            AND keyword GLOB '*[가-힣]*'""")
        conn.commit()

    # 같은 (kind, target)을 런마다 다시 INSERT하면 목록이 같은 키워드로 채워지고,
    # 트리아지해 둔 상태(확인함·완료)가 새 'new' 행에 묻힌다. UNIQUE 인덱스로 막고
    # 적재는 upsert로 한다(scoring.md 5절). 인덱스를 걸려면 기존 중복부터 정리한다.
    idx = {r["name"] for r in conn.execute("PRAGMA index_list(opportunities)")}
    if "idx_opp_target" not in idx:
        # 남길 행: 손댄 것(status!='new')을 우선하고, 그중 최신. 트리아지 결과를 지키려는 것.
        conn.execute("""
            DELETE FROM opportunities WHERE id NOT IN (
              SELECT id FROM (
                SELECT id, ROW_NUMBER() OVER (
                         PARTITION BY project_id, kind, target
                         ORDER BY (status='new'), id DESC) rn
                  FROM opportunities)
               WHERE rn = 1)""")
        conn.execute("CREATE UNIQUE INDEX idx_opp_target "
                     "ON opportunities(project_id, kind, target)")
        conn.commit()

    # rank_snapshots에 같은 키워드·같은 날 행이 여러 개면 "최신 체크" 조회가 하루 안의
    # 아무 행이나 잡는다. 키워드·날짜별 최신(rowid 최대)만 남기고 UNIQUE 표현식
    # 인덱스로 재발을 막는다 — 쓰기 쪽은 write_rank_snapshot이 delete 후 insert.
    if "idx_rank_kw_day" not in {r["name"] for r in conn.execute("PRAGMA index_list(rank_snapshots)")}:
        conn.execute("""DELETE FROM rank_snapshots WHERE id NOT IN (
                          SELECT MAX(id) FROM rank_snapshots
                           GROUP BY keyword_id, date(checked_at))""")
        conn.execute("CREATE UNIQUE INDEX idx_rank_kw_day "
                     "ON rank_snapshots(keyword_id, date(checked_at))")
        conn.commit()


def connect() -> sqlite3.Connection:
    CAPTURE_HOME.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # ponytail: 스키마가 전부 IF NOT EXISTS라 매 연결마다 보장해도 공짜 —
    # 덕분에 사용자가 'db.py init'을 먼저 칠 일이 없다.
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def connect_ro() -> sqlite3.Connection:
    """읽기 전용 커넥션 — Brain 조회 통로(run_sql)가 쓰는 것.

    문자열 검사로 SELECT를 가려내는 건 못 믿는다: SQLite에서
    `WITH x AS (SELECT 1) DELETE FROM opportunities` 는 유효한 문법이라
    startswith(('select','with')) 가드를 그대로 통과한다. DB가 거부하게 만든다.
    """
    connect().close()                      # 없으면 만들고 스키마를 맞춘 뒤 (mode=ro는 생성을 못 한다)
    conn = sqlite3.connect(DB_PATH.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
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
        sys.exit(f"'{name}' 사이트가 아직 등록되지 않았습니다 — "
                 f"먼저 `/capture add {name}` 으로 등록하세요 "
                 "(수동: python db.py sync-project <yaml>)")
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


class Run:
    """실행 한 건. 수집기가 도중에 값을 채우고, 컨텍스트가 끝날 때 기록된다."""

    def __init__(self, conn, run_id: int) -> None:
        self.conn, self.id = conn, run_id
        self.api_calls, self.cost, self.notes = 0, 0.0, ""


@contextmanager
def run(conn: sqlite3.Connection, project_id: int, kind: str):
    """실행 기록을 예외에도 닫는다.

    try/finally 없이 start_run + finish_run을 손으로 쓰던 시절에는 수집 도중
    예외가 나면 runs.finished_at 이 NULL로 남아 "수집 이력" 화면이 거짓말을 했다.
    """
    r = Run(conn, start_run(conn, project_id, kind))
    try:
        yield r
    except BaseException as e:
        finish_run(conn, r.id, api_calls=r.api_calls, cost=r.cost,
                   notes=(r.notes + f" | 중단: {type(e).__name__}: {e}").strip(" |")[:500])
        raise
    else:
        finish_run(conn, r.id, api_calls=r.api_calls, cost=r.cost, notes=r.notes)


# ── Brain 쓰기 경로 ────────────────────────────────────────────────
# 스키마를 아는 곳은 여기뿐이다. 수집기·browse·create가 각자 INSERT를 쓰던 시절엔
# 같은 수정이 한 호출부에만 들어가 locale 누락 같은 버그가 다른 경로에 남았다.

OPP_STATUSES = ("new", "acked", "done", "dismissed")


def write_gsc_snapshot(conn: sqlite3.Connection, project_id: int, snapshot_date: str,
                       period_days: int, rows) -> int:
    """GSC 스냅샷 적재. 같은 날 다시 넣으면 덮어쓴다(하루 1스냅샷).

    rows: (query, page|None, clicks, impressions, ctr, position) 순회 가능 객체.
    page NULL 허용은 구버전(CSV 시절) 데이터 호환용이다.
    """
    rows = list(rows)
    conn.execute("DELETE FROM gsc_snapshots WHERE project_id=? AND snapshot_date=?",
                 (project_id, snapshot_date))
    conn.executemany(   # 10만 행을 한 줄씩 execute하면 눈에 띄게 느리다
        """INSERT INTO gsc_snapshots(project_id, snapshot_date, period_days,
             query, page, clicks, impressions, ctr, position)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        [(project_id, snapshot_date, period_days, q, pg,
          int(clk or 0), int(imp or 0), round(float(ctr or 0), 4),
          round(float(pos or 0), 1))
         for q, pg, clk, imp, ctr, pos in rows])
    conn.commit()
    return len(rows)


def write_rank_snapshot(conn: sqlite3.Connection, keyword_id: int,
                        position: int | None, url: str | None,
                        serp_features=None,
                        aio_present: int | None = None,
                        aio_cited: int | None = None,
                        checked_at: str | None = None) -> int:
    """SERP 순위 스냅샷 적재. 같은 키워드를 같은 날 다시 확인하면 덮어쓴다(하루 1행).

    불변식: aio_present/aio_cited 의 None은 "미측정"이며 0으로 강제 변환하면
    안 된다 (serper 경로는 AIO를 측정하지 않아 NULL로 남아야 한다).
    """
    if isinstance(serp_features, (list, dict)):
        feat_json = json.dumps(serp_features, ensure_ascii=False)
    elif serp_features is not None:
        feat_json = str(serp_features)
    else:
        feat_json = None

    ts = checked_at or now()
    # write_gsc_snapshot 과 같은 delete-then-insert — 재실행이 중복을 안 쌓는다.
    # UNIQUE 인덱스 (keyword_id, date(checked_at))가 이 불변식을 DB 수준에서도 지킨다.
    conn.execute("DELETE FROM rank_snapshots WHERE keyword_id=? AND date(checked_at)=date(?)",
                 (keyword_id, ts))
    cur = conn.execute(
        """INSERT INTO rank_snapshots(keyword_id, checked_at, position, url,
             serp_features_json, aio_present, aio_cited)
           VALUES(?,?,?,?,?,?,?)""",
        (keyword_id, ts,
         int(position) if position is not None else None,
         url, feat_json,
         int(aio_present) if aio_present is not None else None,
         int(aio_cited) if aio_cited is not None else None))
    conn.commit()
    return cur.lastrowid


def set_opportunity_status(conn: sqlite3.Connection, opp_id: int, status: str,
                           project_id: int | None = None) -> int:
    """기회 상태 갱신 (new|acked|done|dismissed). 갱신된 rowcount 반환."""
    if status not in OPP_STATUSES:
        raise ValueError(f"status must be one of {OPP_STATUSES}, got {status!r}")
    if project_id is not None:
        cur = conn.execute(
            "UPDATE opportunities SET status=? WHERE id=? AND project_id=?",
            (status, int(opp_id), int(project_id)))
    else:
        cur = conn.execute(
            "UPDATE opportunities SET status=? WHERE id=?",
            (status, int(opp_id)))
    conn.commit()
    return cur.rowcount


def upsert_opportunities(conn: sqlite3.Connection, project_id: int,
                         run_id: int | None, rows) -> int:
    """기회 목록 upsert (scoring.md §5).

    불변식: ON CONFLICT에서 기존 status는 절대 건드리지 않는다 (트리아지 보존).
    """
    n = 0
    for r in rows:
        if isinstance(r, dict):
            k, t = r["kind"], r["target"]
            s = r.get("score")
            reason = r.get("reasoning")
        else:
            k, t = r[0], r[1]
            s = r[2] if len(r) > 2 else None
            reason = r[3] if len(r) > 3 else None
        conn.execute(
            """INSERT INTO opportunities(project_id, run_id, kind, target, score, reasoning)
               VALUES(?,?,?,?,?,?)
               ON CONFLICT(project_id, kind, target) DO UPDATE SET
                 run_id=excluded.run_id, score=excluded.score,
                 reasoning=excluded.reasoning""",
            (project_id, run_id, k, str(t).strip(),
             float(s) if s is not None else None,
             str(reason) if reason is not None else None))
        n += 1
    conn.commit()
    return n


def open_opportunities(conn: sqlite3.Connection, project_id: int,
                       kinds: str | list[str] | None = None,
                       limit: int = 10) -> list[sqlite3.Row]:
    """미완료 기회 목록 (status IN ('new','acked')).

    작업 중인(acked) 기회를 먼저 보여주기 위해 scoring.opportunities(new 우선, 화면용)와 정렬이 일부러 다르다.
    """
    q = ("SELECT id, kind, target, score, reasoning, status, created_at "
         "FROM opportunities WHERE project_id=? AND status IN ('new','acked')")
    args: list = [project_id]
    if kinds:
        if isinstance(kinds, str):
            ks = [k.strip() for k in kinds.split(",") if k.strip()]
        else:
            ks = [str(k).strip() for k in kinds if str(k).strip()]
        if ks:
            q += f" AND kind IN ({','.join('?' * len(ks))})"
            args += ks
    q += " ORDER BY status='acked' DESC, score DESC LIMIT ?"
    args.append(limit)
    return conn.execute(q, args).fetchall()


def add_keyword_candidates(conn: sqlite3.Connection, project_id: int, items) -> int:
    """키워드 후보 적재 (is_active=0 — 활성화는 Claude 큐레이션 몫).

    items: (keyword, locale, source). locale은 필수 인자다 — 선택 컬럼이던 시절
    자동완성 경로가 이걸 빼먹어 후보가 NULL locale로 쌓였고, 프로젝트 로케일로
    다시 조회돼 한국어 키워드가 전부 "순위 없음"이 됐다. 모르면 명시적으로 None.
    """
    n = 0
    for kw, locale, source in items:
        kw = (kw or "").strip()
        if not kw:
            continue
        cur = conn.execute(
            """INSERT OR IGNORE INTO keywords(project_id, keyword, locale, source, is_active)
               VALUES(?,?,?,?,0)""", (project_id, kw, locale, source))
        n += cur.rowcount
    conn.commit()
    return n


def add_competitors(conn: sqlite3.Connection, project_id: int, domains, source: str) -> int:
    n = 0
    for d in domains:
        d = (d or "").strip().lower()
        if d:
            n += conn.execute(
                "INSERT OR IGNORE INTO competitors(project_id, domain, source) VALUES(?,?,?)",
                (project_id, d, source)).rowcount
    conn.commit()
    return n


def record_ai_check(conn: sqlite3.Connection, prompt_id: int, run_id: int | None,
                    engine: str, sample_idx: int, mentioned: int, cited: int,
                    cited_domains: list, excerpt: str) -> int:
    """AI 인용 체크 1건. 키 경로(collect_ai)와 브라우저 경로(browse)가 같이 쓴다."""
    cur = conn.execute(
        """INSERT INTO ai_checks(prompt_id, run_id, engine, sample_idx,
             mentioned, cited, cited_domains_json, answer_excerpt)
           VALUES(?,?,?,?,?,?,?,?)""",
        (prompt_id, run_id, engine, sample_idx, int(mentioned), int(cited),
         # 전문 저장 — 280자 절단 시절엔 답변 맥락(왜 인용됐/안 됐는지)을 검증할 수
         # 없었다. 8000자 상한은 비정상 폭주(무한 스트림 캡처 등) 방지 안전핀일 뿐.
         json.dumps(cited_domains or [], ensure_ascii=False), (excerpt or "")[:8000]))
    conn.commit()
    return cur.lastrowid


def record_creation(conn: sqlite3.Connection, project_id: int, file_path: str, *,
                    opportunity_id: int | None = None, kind: str | None = None,
                    branch: str | None = None, note: str | None = None) -> int:
    cur = conn.execute(
        """INSERT INTO creations(project_id, opportunity_id, kind, file_path, branch, note)
           VALUES(?,?,?,?,?,?)""",
        (project_id, opportunity_id, kind, file_path, branch, note))
    conn.commit()
    return cur.lastrowid


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
    """Read-only ad-hoc queries. This is how Claude consults the Brain.

    실제로 쓰기를 막는 건 connect_ro()다. 아래 문자열 검사는 ATTACH 같은
    엉뚱한 문장을 일찍 되돌려주는 용도일 뿐, 이것만으로는 안전하지 않다.
    """
    if not query.strip().lower().startswith(("select", "with")):
        sys.exit("read-only: only SELECT/WITH queries allowed here")
    conn = connect_ro()
    try:
        rows = conn.execute(query).fetchall()
    except sqlite3.OperationalError as e:
        if "readonly" not in str(e).lower():
            raise
        sys.exit("이 통로는 조회 전용입니다 — Brain을 바꾸려면 scoring.md 5절의 "
                 "파이썬 원라이너(db.connect())를 쓰세요.")
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
