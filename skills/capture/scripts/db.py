#!/usr/bin/env python3
"""Brain: SQLite layer for the capture skill.

State lives OUTSIDE the skill folder so skill updates never touch data.
  CAPTURE_HOME (default ~/.capture)
    ├── brain.db
    ├── projects/*.yaml
    ├── reports/{project}/{date}.html
    └── gsc_token.json / gsc_oauth_client.json

CLI:
  python db.py init
  python db.py sync-project <path/to/project.yaml>
  python db.py stats <project>
  python db.py sql "SELECT ..."        # read-only, for Brain queries
"""
import json
import os
import re
import sqlite3
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

def capture_home() -> Path:
    """상태 디렉토리. 매번 환경변수를 다시 읽는다 — import 시점에 얼리지 않는다."""
    return Path(os.environ.get("CAPTURE_HOME", Path.home() / ".capture"))


def db_path() -> Path:
    """Brain 파일 자리."""
    env = os.environ.get("CAPTURE_DB")
    return Path(env) if env else capture_home() / "brain.db"


def __getattr__(name):
    """CAPTURE_HOME/DB_PATH 는 읽을 때마다 계산한다 — 예전엔 import 시점에 얼어서
    테스트와 자체점검이 db 의 module 속성을 직접 갈아끼워야 했다."""
    if name == "CAPTURE_HOME":
        return capture_home()
    if name == "DB_PATH":
        return db_path()
    raise AttributeError(name)


# 한국어 Windows 콘솔(cp949)에서 '—'·'✓' 출력이 UnicodeEncodeError로 죽는 것 방지.
# db는 모든 스크립트가 import하므로 여기 한 번이면 전부 커버된다 — doctor·connect_gsc·
# createdb 도 각자 갖고 있던 사본을 지우고 이 import에 기댄다.
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):  # 파이프로 감싼 경우 등
        pass


def load_env(path: Path | None = None) -> None:
    """~/.capture/env 의 KEY=VALUE를 환경변수로 — 대시보드 설정 화면이 여기 쓴다.
    셸에 이미 export한 값이 우선(setdefault). # ponytail: dotenv 패키지 대신 4줄."""
    path = path or capture_home() / "env"
    for line in (path.read_text("utf-8").splitlines() if path.exists() else []):
        k, sep, v = line.partition("=")
        if sep and k.strip() and not k.lstrip().startswith("#"):
            os.environ.setdefault(k.strip(), v.strip())


load_env()


# ── 자료가 어디 사는가 ─────────────────────────────────────────────
# 경로 규칙은 여기서만 답한다. 예전에는 db·doctor·connect_gsc·createdb·collect_gsc가
# 각자 계산해서 CAPTURE_HOME 5벌·GSC 키 4벌이 돌아다녔고 다운로드 폴더는 이미 어긋나 있었다.

def gsc_key() -> Path:
    """구글 서비스 계정 키 (전 사이트 공용) — 무인 수집용 선택지."""
    return Path(os.environ.get("GOOGLE_APPLICATION_CREDENTIALS",
                               capture_home() / "gsc_service_account.json"))


def gsc_oauth_bundled() -> Path:
    """플러그인이 동봉한 OAuth 클라이언트 — 콘솔 작업을 사용자에게서 걷어낸다.

    구글 OAuth 는 등록된 client_id/secret 없이는 성립하지 않는다. 우회로가 없어서,
    지금까지는 **사용자마다** 클라우드 콘솔에서 앱을 등록하게 했다(API 사용 설정 →
    동의 화면 → 데스크톱 앱 클라이언트 → JSON 다운로드). 그 4단계를 없애는 방법은
    "콘솔 없는 OAuth"가 아니라 **콘솔 작업을 한 번 해서 그 결과를 배포에 넣는 것**이다.

    설치형 앱의 client_secret 은 기밀이 아니다 — 구글이 그렇게 명시하고,
    rclone·gcloud 같은 CLI 가 전부 자기 클라이언트를 박아서 배포한다. 대신
    미검증 앱이라 동의 화면에 경고가 한 번 뜨고 사용자 100명 상한이 붙는다.

    이 파일이 없어도 아무것도 깨지지 않는다 — 그냥 예전처럼 각자 클라이언트를
    쓰는 흐름으로 돌아갈 뿐이다(배포에서 뺐거나, 로컬 개발 중이거나).
    """
    return Path(__file__).resolve().parents[2] / "setup" / "oauth_client.json"


def gsc_oauth_client() -> Path:
    """OAuth 클라이언트 시크릿(데스크톱 앱) — 기본 인증 방식.

    이 파일만 있으면 구글 로그인 한 번으로 끝난다. 속성마다 사용자를 추가하는
    단계가 없다 — 내 구글 계정이 이미 그 속성의 소유자이기 때문이다.

    우선순위(이게 정본이다): 환경변수 > 사용자가 깐 것(~/.capture) > 번들.
    **사용자 것이 번들을 이긴다** — 이미 자기 클라이언트로 붙여 둔 사람의 발밑을
    업데이트가 바꿔치기하면 안 된다. 번들은 아무것도 안 한 사람의 기본값일 뿐이다.
    """
    env = os.environ.get("GSC_OAUTH_CLIENT_SECRETS_FILE")
    if env:
        return Path(env)
    own = capture_home() / "gsc_oauth_client.json"
    if own.exists():
        return own
    bundled = gsc_oauth_bundled()
    # 없으면 사용자 경로를 돌려준다 — "무엇이 없어서 막혔나"를 말할 때 가리킬 자리는
    # 배포 안쪽이 아니라 사용자가 파일을 놓을 자리여야 한다.
    return bundled if bundled.exists() else own


def gsc_auth() -> str:
    """지금 걸려 있는 인증 — "oauth" | "service_account" | "" (아직 없음).

    **이 순서가 정본이다.** collect_gsc·gsc_query·doctor가 전부
    이 판정을 따른다 — 예전에 같은 규칙이 여러 방언으로 흩어져 한쪽만 고쳐지는
    일이 반복됐다. OAuth가 기본이고, 서비스 계정은 무인 수집이 필요할 때 쓴다.

    **번들 OAuth 는 서비스 계정보다 뒤다.** 번들 파일은 설치만 하면 항상 존재하므로,
    단순히 "OAuth 파일이 있나"로 판정하면 서비스 계정으로 무인 수집을 걸어 둔 사람이
    매번 브라우저 로그인으로 끌려간다. 사용자가 **직접 놓은 것**이 항상 이긴다.
    """
    env = os.environ.get("GSC_OAUTH_CLIENT_SECRETS_FILE")
    if (env and Path(env).exists()) or (capture_home() / "gsc_oauth_client.json").exists():
        return "oauth"
    if gsc_key().exists():
        return "service_account"
    if gsc_oauth_bundled().exists():
        return "oauth"
    return ""


def gsc_token() -> Path:
    """구글 로그인 토큰을 보관하는 자리 — **이제 우리가 정한다.**

    예전에는 gsc MCP 서버(mcp-search-console)가 로그인을 대신 해 줬고, 토큰도 그
    패키지의 설정 폴더에 있었다. 그 서버를 걷어내면서 로그인도 우리 것이 됐으니
    (collect_gsc._oauth_service) 토큰도 나머지 상태와 같은 곳 — CAPTURE_HOME — 에 둔다.
    """
    return Path(os.environ.get("GSC_TOKEN_FILE", capture_home() / "gsc_token.json"))


def gsc_token_legacy() -> Path:
    """예전 MCP 서버가 쓰던 토큰 자리 — 이미 로그인한 사람을 다시 로그인시키지 않는다.

    업스트림 gsc_server.py 규칙이었다: GSC_CONFIG_DIR 또는 platformdirs 의 mcp-gsc
    아래 token.json. 파일 형식은 google-auth 표준(Credentials.to_json)이라 그대로
    읽힌다 — collect_gsc 가 이걸 읽어 쓰고 새 자리에 다시 쓴다.
    윈도우 경로가 mcp-gsc 를 두 번 지나는 건 오타가 아니라 platformdirs 실측값이다.
    """
    d = os.environ.get("GSC_CONFIG_DIR")
    if not d:
        if sys.platform == "win32":
            base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local")
        elif sys.platform == "darwin":
            base = Path.home() / "Library" / "Application Support"
        else:
            base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
        d = base / "mcp-gsc" / "mcp-gsc" if sys.platform == "win32" else base / "mcp-gsc"
    return Path(d) / "token.json"


def gsc_token_file():
    """실제로 존재하는 토큰 파일 — 새 자리 우선, 없으면 레거시. 없으면 None."""
    for t in (gsc_token(), gsc_token_legacy()):
        if t.exists():
            return t
    return None


def gsc_connected() -> bool:
    """**진짜 연결됐나** — 클라이언트 파일이 있느냐가 아니라 토큰이 있느냐다.

    번들 OAuth 클라이언트(gsc_oauth_bundled)는 설치만 하면 항상 존재한다. 그래서
    "파일이 있다"가 더 이상 "연결됐다"를 뜻하지 않는다 — 파일 유무로 판정하면
    설치 직후 전원이 '연결됨'으로 보인다.

    이건 실제로 한 번 당해 본 거짓말이다(예전 gsc MCP 서버는 인증 파일이 없어도
    `Connected` 로 떠서 사용자가 한 번도 인증되지 않은 상태로 지냈다).
    **우리 doctor 가 같은 거짓말을 하면 안 된다.**

    서비스 계정은 로그인 자체가 없어서 키 파일 존재가 곧 연결이다.
    """
    auth = gsc_auth()
    if auth == "service_account":
        return gsc_key().exists()
    if auth == "oauth":
        return gsc_token_file() is not None
    return False


def creds_dir(project: str) -> Path:
    """레거시 사이트별 OAuth 자격증명 폴더 — 서비스 계정 키로 대체됐다."""
    return capture_home() / "creds" / project


def downloads_dir() -> Path:
    """브라우저 다운로드 폴더 — GSC 서비스 계정 키(connect_gsc)를 여기서 찾는다."""
    return Path(os.environ.get("DOWNLOADS_DIR", Path.home() / "Downloads")).expanduser()


_REPO_PATH_RE = re.compile(r"^repo_path:[ 	]*(.+?)[ 	]*$", re.M)


def repo_project(cwd=None) -> str | None:
    """지금 이 폴더가 어느 사이트의 리포인가 — `projects/{P}.repo.yaml` 의 repo_path 로 판정.

    Brain 은 컴퓨터 전역(`~/.capture/brain.db`)이라 사이트가 여럿이면 "지금 이 폴더가
    어느 사이트냐"에 아무도 답하지 못했다. 그 답은 `/create profile` 이 이미
    `repo.yaml` 에 적어 두고 있었는데 읽는 코드가 없었다.

    ponytail: yaml 파서 대신 한 줄 정규식으로 읽는다 — doctor 가 pip 이전에도 돌아야 해서
    stdlib 밖으로 못 나간다. repo_path 를 블록 스칼라(`|`)로 적으면 못 읽는다.
    """
    try:
        here = Path(cwd or Path.cwd()).resolve()
    except OSError:
        return None
    d = capture_home() / "projects"
    if not d.exists():
        return None
    best = None
    for f in sorted(d.glob("*.repo.yaml")):
        try:
            m = _REPO_PATH_RE.search(f.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        if not m:
            continue
        raw = m.group(1).strip().strip('"').strip("'")
        if not raw or raw.startswith("/path/to/"):   # 템플릿 그대로면 무시
            continue
        try:
            root = Path(raw).expanduser().resolve()
        except OSError:
            continue
        if root == here or root in here.parents:
            # 가장 깊은 매치가 이긴다 — 리포 안에 리포가 있을 때 안쪽이 답이다.
            if best is None or len(root.parts) > len(best[1].parts):
                best = (f.name[: -len(".repo.yaml")], root)
    return best[0] if best else None


def docs_dir(project: str) -> Path:
    """사이트별 마케팅 문서 자리 — positioning.md, aso.md 등.

    setup 이 외부 스킬(product-marketing·aso)로 만들어 여기 두면, capture 는 AI
    프롬프트를 쓸 때, create 는 콘텐츠 보이스를 잡을 때 같은 문서를 읽는다.
    Brain(brain.db) 밖에 산문으로 두는 이유는 사람이 직접 고쳐야 하는 것이기 때문이다.
    """
    return capture_home() / "docs" / project


class ProjectNotFound(Exception):
    """등록되지 않은 사이트를 찾았을 때. 사용자 문구는 str(e) 에 담는다."""


class ProjectConfigNotFound(Exception):
    """프로젝트 yaml 파일을 못 찾았을 때."""


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
CREATE TABLE IF NOT EXISTS gsc_daily (
  id INTEGER PRIMARY KEY,
  project_id INTEGER NOT NULL REFERENCES projects(id),
  date TEXT NOT NULL,                         -- YYYY-MM-DD  성과일이다. 수집일이 아니다.
  clicks INTEGER, impressions INTEGER, ctr REAL, position REAL,
  UNIQUE(project_id, date)
);
CREATE TABLE IF NOT EXISTS gsc_breakdown (
  id INTEGER PRIMARY KEY,
  project_id INTEGER NOT NULL REFERENCES projects(id),
  snapshot_date TEXT NOT NULL,                -- 수집일 (gsc_snapshots 와 같은 값으로 짝을 맞춘다)
  period_days INTEGER NOT NULL,
  dim TEXT NOT NULL,                          -- device|country
  dim_value TEXT NOT NULL,                    -- MOBILE|DESKTOP|TABLET  /  kor|usa|...
  query TEXT NOT NULL,
  clicks INTEGER, impressions INTEGER, ctr REAL, position REAL
);
CREATE INDEX IF NOT EXISTS idx_gsc_bd ON gsc_breakdown(project_id, snapshot_date, dim);
CREATE TABLE IF NOT EXISTS gsc_index_status (
  id INTEGER PRIMARY KEY,
  project_id INTEGER NOT NULL REFERENCES projects(id),
  checked_date TEXT NOT NULL,                 -- YYYY-MM-DD
  url TEXT NOT NULL,
  verdict TEXT,                               -- PASS|PARTIAL|FAIL|NEUTRAL
  coverage_state TEXT, robots_txt_state TEXT, page_fetch_state TEXT,
  indexing_state TEXT, google_canonical TEXT, user_canonical TEXT,
  last_crawled TEXT, rich_results_json TEXT,
  UNIQUE(project_id, checked_date, url)
);
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
  kind TEXT NOT NULL,   -- striking_distance|ai_citation_gap|rank_decay|content_gap|coverage|pseo_pattern|aio_exposure|ctr_gap|cannibalization|device_gap|index_blocked
  target TEXT NOT NULL,                       -- keyword / prompt / page
  score REAL,
  reasoning TEXT,                             -- Claude-written, grounded in Brain data
  status TEXT DEFAULT 'new',                  -- new|acked|done|dismissed
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY,
  project_id INTEGER NOT NULL REFERENCES projects(id),
  kind TEXT NOT NULL,                         -- gsc|ai|keywords|analysis|report|full|index
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
    capture_home().mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path())
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
    conn = sqlite3.connect(db_path().resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = connect()
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()
    print(f"Brain ready: {db_path()}")


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def get_project(conn: sqlite3.Connection, name: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM projects WHERE name=?", (name,)).fetchone()
    if not row:
        raise ProjectNotFound(f"'{name}' 사이트가 아직 등록되지 않았습니다 — "
                              f"먼저 `/capture add {name}` 으로 등록하세요 "
                              "(수동: python db.py sync-project <yaml>)")
    return row


def load_project_yaml(name_or_path: str) -> dict:
    import yaml  # lazy
    p = Path(name_or_path)
    if not p.exists():
        p = capture_home() / "projects" / f"{name_or_path}.yaml"
    if not p.exists():
        raise ProjectConfigNotFound(f"project yaml not found: {name_or_path}")
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
# 스키마를 아는 곳은 여기뿐이다. 수집기·create가 각자 INSERT를 쓰던 시절엔
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


def write_gsc_daily(conn: sqlite3.Connection, project_id: int, rows) -> int:
    """날짜별 성과 적재. rows: (date, clicks, impressions, ctr, position) 순회 가능 객체.

    여기만 delete 후 insert가 아니라 upsert다. GSC는 최근 2~3일치를 나중에 상향
    보정하므로, 같은 날짜를 다시 수집하면 값이 바뀐다 — 덮어써야 맞다.
    date 는 성과일이지 수집일이 아니다.
    """
    rows = list(rows)
    conn.executemany(
        """INSERT INTO gsc_daily(project_id, date, clicks, impressions, ctr, position)
           VALUES(?,?,?,?,?,?)
           ON CONFLICT(project_id, date) DO UPDATE SET
             clicks=excluded.clicks, impressions=excluded.impressions,
             ctr=excluded.ctr, position=excluded.position""",
        [(project_id, d, int(clk or 0), int(imp or 0),
          round(float(ctr or 0), 4), round(float(pos or 0), 1))
         for d, clk, imp, ctr, pos in rows])
    conn.commit()
    return len(rows)


def write_gsc_breakdown(conn: sqlite3.Connection, project_id: int, snapshot_date: str,
                        period_days: int, dim: str, rows) -> int:
    """차원 분해 스냅샷(device/country) 적재. rows: (dim_value, query, clicks, impressions, ctr, position).

    write_gsc_snapshot 과 같은 규칙 — 같은 (project, snapshot_date, dim)은 지우고 다시 넣는다.
    dim 별로만 지우는 게 핵심이다: device를 다시 수집한다고 country까지 날아가면 안 된다.
    """
    rows = list(rows)
    conn.execute(
        "DELETE FROM gsc_breakdown WHERE project_id=? AND snapshot_date=? AND dim=?",
        (project_id, snapshot_date, dim))
    conn.executemany(
        """INSERT INTO gsc_breakdown(project_id, snapshot_date, period_days, dim,
             dim_value, query, clicks, impressions, ctr, position)
           VALUES(?,?,?,?,?,?,?,?,?,?)""",
        [(project_id, snapshot_date, period_days, dim, dv, q,
          int(clk or 0), int(imp or 0), round(float(ctr or 0), 4),
          round(float(pos or 0), 1))
         for dv, q, clk, imp, ctr, pos in rows])
    conn.commit()
    return len(rows)


def write_index_status(conn: sqlite3.Connection, project_id: int, checked_date: str,
                       rows) -> int:
    """URL 검사 결과 적재. rows: dict 순회 가능 객체 (키는 테이블 컬럼명 그대로).

    upsert인 이유: 하루에 URL 배치를 나눠 돌리는 게 정상 사용이라, 같은 날 두 번째
    배치가 첫 배치를 지우면 안 된다. 없는 키는 NULL로 들어간다 — 검사 API가 필드를
    통째로 안 주는 경우가 있어 빈 문자열로 채우지 않는다("모름"과 "없음"은 다르다).
    """
    cols = ("verdict", "coverage_state", "robots_txt_state", "page_fetch_state",
            "indexing_state", "google_canonical", "user_canonical",
            "last_crawled", "rich_results_json")
    rows = list(rows)
    conn.executemany(
        f"""INSERT INTO gsc_index_status(project_id, checked_date, url, {', '.join(cols)})
            VALUES(?,?,?,{','.join('?' * len(cols))})
            ON CONFLICT(project_id, checked_date, url) DO UPDATE SET
              {', '.join(f'{c}=excluded.{c}' for c in cols)}""",
        [(project_id, checked_date, r["url"]) + tuple(r.get(c) for c in cols)
         for r in rows])
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
    """AI 인용 체크 1건 (collect_ai)."""
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


def mark_creation_merged(conn: sqlite3.Connection, creation_id: int) -> int:
    """작업물 병합 완료(merged=1) 표시. 갱신된 rowcount 반환."""
    cur = conn.execute("UPDATE creations SET merged=1 WHERE id=?", (int(creation_id),))
    conn.commit()
    return cur.rowcount


def set_keyword_intent(conn: sqlite3.Connection, keyword_id: int, intent: str | None) -> int:
    """키워드 검색 의도(intent) 갱신. 갱신된 rowcount 반환."""
    return set_keyword_intents(conn, [(keyword_id, intent)])


def set_keyword_intents(conn: sqlite3.Connection, pairs) -> int:
    """(keyword_id, intent) 쌍을 한 트랜잭션으로 갱신. 갱신된 rowcount 반환.

    한 건씩 커밋하면 백필 한 번에 fsync 가 행 수만큼 난다 — 묶어서 한 번만 커밋한다.
    """
    rows = [(intent, int(kid)) for kid, intent in pairs]
    if not rows:
        return 0
    cur = conn.executemany("UPDATE keywords SET intent=? WHERE id=?", rows)
    conn.commit()
    return cur.rowcount


# ── Brain 읽기 — 테이블·컬럼·정렬을 아는 곳은 여기뿐이다 ───────────────


def list_opportunities(conn: sqlite3.Connection, project_id: int, *,
                       kinds: list[str] | None = None,
                       statuses: list[str] | tuple[str, ...] | None = None,
                       order: str,
                       limit: int = 10,
                       with_id: bool = True) -> list[sqlite3.Row]:
    """기회 목록 조회.

    order:
      'triage': 작업 중(acked) 우선, 점수 내림차순 (status='acked' DESC, score DESC)
      'screen': 신규(new) 우선, 점수 내림차순 (status='new' DESC, score DESC, id DESC)
    """
    if order == "triage":
        order_clause = "ORDER BY status='acked' DESC, score DESC"
    elif order == "screen":
        order_clause = "ORDER BY (status='new') DESC, score DESC, id DESC"
    else:
        raise ValueError(f"unknown order: {order!r} (expected 'triage' or 'screen')")

    cols = ("id, kind, target, ROUND(score,1) score, reasoning, status, "
            "substr(created_at,1,10) created") if with_id else \
           "kind, target, ROUND(score,1) score, reasoning, status"

    q = f"SELECT {cols} FROM opportunities WHERE project_id=?"
    args: list = [int(project_id)]

    if statuses:
        st_list = [str(s).strip() for s in statuses if str(s).strip()]
        if st_list:
            q += f" AND status IN ({','.join('?' * len(st_list))})"
            args += st_list

    if kinds:
        k_list = [str(k).strip() for k in kinds if str(k).strip()]
        if k_list:
            q += f" AND kind IN ({','.join('?' * len(k_list))})"
            args += k_list

    q += f" {order_clause} LIMIT ?"
    args.append(int(limit))
    return conn.execute(q, args).fetchall()


def open_opportunities(conn: sqlite3.Connection, project_id: int,
                       kinds: list[str] | None = None,
                       limit: int = 10) -> list[sqlite3.Row]:
    """미완료 기회 목록 (status IN ('new','acked')).

    작업 중인(acked) 기회를 먼저 보여주기 위해 scoring.opportunities(new 우선, 화면용)와 정렬이 일부러 다르다.
    """
    return list_opportunities(conn, project_id, kinds=kinds,
                              statuses=["new", "acked"], order="triage",
                              limit=limit, with_id=True)


def get_opportunity(conn: sqlite3.Connection, opp_id: int,
                    project_id: int | None = None) -> sqlite3.Row | None:
    """기회 단건 조회."""
    if project_id is not None:
        return conn.execute(
            "SELECT id, project_id, run_id, kind, target, score, reasoning, status, created_at "
            "FROM opportunities WHERE id=? AND project_id=?",
            (int(opp_id), int(project_id))).fetchone()
    return conn.execute(
        "SELECT id, project_id, run_id, kind, target, score, reasoning, status, created_at "
        "FROM opportunities WHERE id=?",
        (int(opp_id),)).fetchone()


def list_creations(conn: sqlite3.Connection, project_id: int,
                   limit: int = 30) -> list[sqlite3.Row]:
    """작업 완료(creations) 목록 조회."""
    return conn.execute(
        """SELECT id, opportunity_id, kind, file_path, branch, merged, created_at
             FROM creations WHERE project_id=? ORDER BY id DESC LIMIT ?""",
        (int(project_id), int(limit))).fetchall()


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
