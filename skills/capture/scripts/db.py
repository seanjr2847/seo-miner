#!/usr/bin/env python3
"""Brain: SQLite layer for the capture skill.

State lives OUTSIDE the skill folder so skill updates never touch data.
  CAPTURE_HOME (default ~/.capture)
    ├── brain.db
    ├── projects/*.yaml
    ├── reports/{project}/{date}.html
    └── gsc_token.json / gsc_oauth_client.json

경로·구글 자격증명은 paths.py 가 답한다 — 여기 있는 같은 이름들은 그쪽으로 넘기는
얇은 위임이다(호출자를 고치지 않기 위해). 새 코드는 paths 를 직접 import 하면
SQLite·SCHEMA·_migrate 를 끌고 오지 않는다.

CLI:
  python db.py init
  python db.py sync-project <path/to/project.yaml>
  python db.py stats <project>
  python db.py sql "SELECT ..." [--project NAME]
                                       # read-only, for Brain queries.
                                       # --project 가 원격 사이트면 서버 brain 에 묻는다.
  python db.py selfcheck               # 임시 brain.db 로 읽기 동사들을 돌려본다
"""
import json
import sqlite3
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import paths

# ── paths.py 로 넘긴 것들 — 예전 이름 그대로 계속 노출한다 ─────────────
# 호출자 19개(collect_*·doctor·connect_gsc·createdb·dashboard·server)가 이 이름들을
# 쓰고 있다. 마이그레이션은 이 변경의 몫이 아니다 — seam 을 낸 것이 성과물이다.
capture_home = paths.home
db_path = paths.db_file
load_env = paths.load_env
creds_dir = paths.creds_dir
docs_dir = paths.docs_dir
downloads_dir = paths.downloads_dir
repo_project = paths.repo_project
gsc_oauth_bundled = paths.bundled_oauth_client


def _gsc() -> paths.GSC:
    """해석된 자격증명 한 벌. 번들 위치는 **db 의 module 속성을 거쳐** 넘긴다 —
    기존 테스트(test_collectors·test_setup_api)가 db.gsc_oauth_bundled 를 갈아끼워
    "번들이 빠진 배포"를 흉내내고, 그 판정이 아래 함수들에 그대로 먹혀야 한다."""
    return paths.gsc(gsc_oauth_bundled())


def gsc_key() -> Path:
    """구글 서비스 계정 키 (전 사이트 공용) — 무인 수집용 선택지."""
    return _gsc().key


def gsc_oauth_client() -> Path:
    """실제로 쓸 OAuth 클라이언트 (env > 사용자가 깐 것 > 번들)."""
    return _gsc().client


def gsc_auth() -> str:
    """지금 걸려 있는 인증 — "oauth" | "service_account" | "" (아직 없음)."""
    return _gsc().auth


def gsc_token() -> Path:
    """구글 로그인 토큰을 보관하는 자리."""
    return _gsc().token


def gsc_token_legacy() -> Path:
    """예전 MCP 서버가 쓰던 토큰 자리."""
    return _gsc().token_legacy


def gsc_token_file() -> Path | None:
    """실제로 존재하는 토큰 파일 — 새 자리 우선, 없으면 레거시. 없으면 None."""
    return _gsc().token_file


def gsc_connected() -> bool:
    """**진짜 연결됐나** — 클라이언트 파일이 있느냐가 아니라 토큰이 있느냐다."""
    return _gsc().connected


def __getattr__(name):
    """CAPTURE_HOME/DB_PATH 는 읽을 때마다 계산한다 — 예전엔 import 시점에 얼어서
    테스트와 자체점검이 db 의 module 속성을 직접 갈아끼워야 했다."""
    if name == "CAPTURE_HOME":
        return capture_home()
    if name == "DB_PATH":
        return db_path()
    raise AttributeError(name)


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
  ga4_property TEXT,                          -- GA4 속성 숫자 ID만 (예: '123456789') — 'properties/' 접두는 API 부를 때 collect_ga4 가 붙인다
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
CREATE TABLE IF NOT EXISTS serp_results (     -- 검색결과 상위 — rank_snapshots 는 "내 자리"만 안다
  id INTEGER PRIMARY KEY,
  keyword_id INTEGER NOT NULL REFERENCES keywords(id),
  checked_at TEXT DEFAULT CURRENT_TIMESTAMP,
  position INTEGER NOT NULL,                  -- 검색결과에서의 자리
  url TEXT, title TEXT, domain TEXT,
  is_own INTEGER DEFAULT 0,                   -- 1 = 내 도메인
  UNIQUE(keyword_id, checked_at, position)
);
CREATE INDEX IF NOT EXISTS idx_serp_kw ON serp_results(keyword_id, checked_at);
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
-- gsc_snapshots 와 같은 축(project_id, snapshot_date, period_days)이다 — GSC 와 GA4 를
-- 나중에 이으려면 이 축이 어긋나면 안 된다. landing_page 는 GA4 landingPage 차원값을
-- collect_ga4._landing_path() 로 경로만 남긴 정규형이다(호스트 없음, 쿼리스트링 제거).
-- GSC 의 page(절대 URL)와 이을 때는 urlsplit(page).path 로 맞춘다 — 규칙의 정본은
-- collect_ga4.py 상단 주석.
CREATE TABLE IF NOT EXISTS ga4_snapshots (
  id INTEGER PRIMARY KEY,
  project_id INTEGER NOT NULL REFERENCES projects(id),
  snapshot_date TEXT NOT NULL,                -- YYYY-MM-DD (수집일)
  period_days INTEGER NOT NULL,
  landing_page TEXT NOT NULL,                 -- 경로만 (정규화 규칙은 위 주석)
  sessions INTEGER,                           -- 유기 검색(Organic Search) 세션 — GSC 클릭과 성격이 같다
  sessions_all INTEGER,                       -- 전 채널 세션 — 유기 몫을 보려면 이게 분모다
  key_events REAL,                            -- GA4 'conversions' 의 새 이름(2024-05 개명). 컨센트 모델링으로 소수일 수 있다
  total_revenue REAL,
  engagement_rate REAL,                       -- bounceRate의 여집합. 전환 미설정 사이트도 항상 값이 있어 이쪽을 남겼다(bounceRate는 버림, collect_ga4.py 참조)
  avg_session_duration REAL,                  -- 평균 세션 길이(초) — 체류
  pageviews_per_session REAL                  -- 세션당 페이지뷰 — 깊이
);
CREATE INDEX IF NOT EXISTS idx_ga4_proj_date ON ga4_snapshots(project_id, snapshot_date);
-- gsc_breakdown 과 같은 문법(dim/dim_value)에 query 대신 landing_page 축(GA4엔 검색어
-- 개념이 없다). 유기 검색 기준으로만 담는다. device는 GSC 표기(MOBILE/DESKTOP/TABLET)로,
-- country는 GA4 countryId(alpha-2)를 GSC의 alpha-3 소문자로 맞춘다 — 변환표의 정본은
-- collect_ga4.py의 _ALPHA2_ALPHA3.
CREATE TABLE IF NOT EXISTS ga4_breakdown (
  id INTEGER PRIMARY KEY,
  project_id INTEGER NOT NULL REFERENCES projects(id),
  snapshot_date TEXT NOT NULL,
  period_days INTEGER NOT NULL,
  dim TEXT NOT NULL,                          -- device|country|newvsreturning
  dim_value TEXT NOT NULL,
  landing_page TEXT NOT NULL,
  sessions INTEGER,
  key_events REAL,
  total_revenue REAL,
  engagement_rate REAL
);
CREATE INDEX IF NOT EXISTS idx_ga4_bd ON ga4_breakdown(project_id, snapshot_date, dim);
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
CREATE TABLE IF NOT EXISTS page_audits (     -- 내 페이지 HTML 감사 (collect_page.py)
  id INTEGER PRIMARY KEY,
  project_id INTEGER NOT NULL REFERENCES projects(id),
  checked_date TEXT NOT NULL,                 -- YYYY-MM-DD
  url TEXT NOT NULL,
  status INTEGER,                             -- HTTP 상태. NULL = 요청 자체가 실패
  error TEXT,                                 -- 못 가져온 사유 (타임아웃·DNS 등)
  title TEXT, meta_description TEXT,
  h1_json TEXT, h2_json TEXT,                 -- 제목 텍스트 배열
  words INTEGER,                              -- 태그 제거 후 본문 단어 수
  schema_json TEXT,                           -- ld+json 의 @type 목록
  canonical TEXT, robots TEXT,                -- <link rel=canonical>, <meta name=robots>
  internal_links INTEGER, external_links INTEGER,
  images INTEGER, images_no_alt INTEGER,
  viewport TEXT,                              -- <meta name=viewport> — 없으면 모바일에서 데스크톱 폭
  html_lang TEXT,                             -- <html lang>
  hreflang_json TEXT,                         -- [[코드, 주소], ...] 선언 순서 그대로
  published TEXT, modified TEXT,              -- 글의 발행·수정일 (우리가 점검한 날이 아니다)
  js_shell INTEGER,                           -- 1 = 정적 HTML 로는 본문이 안 보이는 페이지로 의심
  UNIQUE(project_id, checked_date, url)
);
CREATE TABLE IF NOT EXISTS page_vitals (    -- 속도 (collect_vitals.py · PageSpeed Insights)
  id INTEGER PRIMARY KEY,
  project_id INTEGER NOT NULL REFERENCES projects(id),
  checked_date TEXT NOT NULL,                 -- YYYY-MM-DD
  url TEXT NOT NULL,
  strategy TEXT NOT NULL,                     -- mobile|desktop
  error TEXT,                                 -- 못 잰 사유
  -- 현장(CrUX, 실제 사용자 28일치) — 검색이 보는 값
  origin_fallback INTEGER,                    -- 1 = 이 URL 이 아니라 사이트 전체(오리진) 값
  field_verdict TEXT,                         -- FAST|AVERAGE|SLOW (구글 표현 그대로)
  field_lcp_ms INTEGER, field_inp_ms INTEGER, field_cls REAL, field_ttfb_ms INTEGER,
  -- 실험실(Lighthouse, 지금 한 번) — 고친 뒤 바로 확인할 수 있는 유일한 숫자
  lab_score INTEGER, lab_lcp_ms INTEGER, lab_cls REAL, lab_tbt_ms INTEGER,
  UNIQUE(project_id, checked_date, url, strategy)
);
CREATE INDEX IF NOT EXISTS idx_page_vitals ON page_vitals(project_id, checked_date);
CREATE TABLE IF NOT EXISTS ai_prompts (
  id INTEGER PRIMARY KEY,
  project_id INTEGER NOT NULL REFERENCES projects(id),
  prompt TEXT NOT NULL,
  category TEXT DEFAULT 'general',            -- 갈래 정본은 gen_prompts.CATEGORY_CHOICES
                                              -- (기본값은 gen_prompts.DEFAULT_CATEGORY)
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
  kind TEXT NOT NULL,                         -- 종류 목록의 정본은 scoring.ALL_KINDS 다
                                              -- (여기 사본을 두던 동안 열넷 중 열하나만 적혀 있었다)
  target TEXT NOT NULL,                       -- keyword / prompt / page
  score REAL,
  reasoning TEXT,                             -- Claude-written, grounded in Brain data
  status TEXT DEFAULT 'new',                  -- new|acked|done|dismissed  (정본은 db.OPP_STATUSES)
  status_at TEXT,                             -- 상태가 마지막으로 바뀐 시각 — 완료 후 관찰(watch_rows)의 기준
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY,
  project_id INTEGER NOT NULL REFERENCES projects(id),
  kind TEXT NOT NULL,                         -- run_all.STAGES 의 단계 id (+ full)
  started_at TEXT DEFAULT CURRENT_TIMESTAMP,
  finished_at TEXT,
  api_calls INTEGER DEFAULT 0,
  cost_estimate_usd REAL DEFAULT 0,
  notes TEXT
);
CREATE TABLE IF NOT EXISTS backlink_summary (  -- 백링크 프로필 (server/backlinks.py 가 채운다)
  id INTEGER PRIMARY KEY,
  project_id INTEGER NOT NULL REFERENCES projects(id),
  checked_date TEXT NOT NULL,
  rank INTEGER, backlinks INTEGER, referring_domains INTEGER,
  referring_main_domains INTEGER, broken_backlinks INTEGER,
  dofollow INTEGER, nofollow INTEGER,
  UNIQUE(project_id, checked_date)
);
CREATE TABLE IF NOT EXISTS referring_domains (
  id INTEGER PRIMARY KEY,
  project_id INTEGER NOT NULL REFERENCES projects(id),
  checked_date TEXT NOT NULL,
  domain TEXT NOT NULL,
  rank INTEGER, backlinks INTEGER, dofollow INTEGER, first_seen TEXT, lost_date TEXT,
  UNIQUE(project_id, checked_date, domain)
);
CREATE INDEX IF NOT EXISTS idx_refdom ON referring_domains(project_id, checked_date);
-- ── 백링크 상세 (collect_backlinks.py) ───────────────────────────────────────
-- 요약(backlink_summary)만으로는 "무엇을 할지"가 안 나온다. 어느 페이지가 어떤
-- 앵커로 링크를 받았는지, 그리고 경쟁사는 받는데 우리는 못 받는 곳이 어디인지가
-- 실제로 손댈 자리다.
CREATE TABLE IF NOT EXISTS backlinks (
  id INTEGER PRIMARY KEY,
  project_id INTEGER NOT NULL REFERENCES projects(id),
  checked_date TEXT NOT NULL,
  url_from TEXT NOT NULL,
  url_to TEXT NOT NULL,
  domain_from TEXT,
  anchor TEXT,
  rank INTEGER,
  dofollow INTEGER,
  is_broken INTEGER DEFAULT 0,                -- url_to 가 4xx/5xx (되찾을 수 있는 링크)
  first_seen TEXT,
  last_seen TEXT,
  UNIQUE(project_id, checked_date, url_from, url_to)
);
CREATE INDEX IF NOT EXISTS idx_bl ON backlinks(project_id, checked_date);
CREATE TABLE IF NOT EXISTS backlink_anchors (
  id INTEGER PRIMARY KEY,
  project_id INTEGER NOT NULL REFERENCES projects(id),
  checked_date TEXT NOT NULL,
  anchor TEXT NOT NULL,
  backlinks INTEGER, referring_domains INTEGER, dofollow INTEGER,
  UNIQUE(project_id, checked_date, anchor)
);
CREATE TABLE IF NOT EXISTS link_intersect (   -- 경쟁사는 링크를 받는데 우리는 못 받는 곳
  id INTEGER PRIMARY KEY,
  project_id INTEGER NOT NULL REFERENCES projects(id),
  checked_date TEXT NOT NULL,
  domain TEXT NOT NULL,
  rank INTEGER,
  hits INTEGER,                               -- 이 도메인에서 링크를 받는 경쟁사 수
  targets TEXT,                               -- 쉼표 구분 — 누가 받고 있나
  we_have INTEGER DEFAULT 0,                  -- 우리도 받고 있으면 1 (제안에서 뺀다)
  UNIQUE(project_id, checked_date, domain)
);

-- ── 경쟁 분석 (collect_gap.py) ────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS competitor_metrics (  -- 도메인별 유기 규모 = 트래픽 몫의 재료
  id INTEGER PRIMARY KEY,
  project_id INTEGER NOT NULL REFERENCES projects(id),
  checked_date TEXT NOT NULL,
  domain TEXT NOT NULL,
  is_self INTEGER DEFAULT 0,                  -- 1 = 우리 도메인 (몫 계산의 분자)
  keywords INTEGER, etv REAL, top10 INTEGER,
  UNIQUE(project_id, checked_date, domain)
);
CREATE TABLE IF NOT EXISTS keyword_gap (      -- 경쟁사 대비 키워드 위치 (Content Gap 정본)
  id INTEGER PRIMARY KEY,
  project_id INTEGER NOT NULL REFERENCES projects(id),
  checked_date TEXT NOT NULL,
  keyword TEXT NOT NULL,
  domain TEXT NOT NULL,                       -- 그 키워드를 잡고 있는 경쟁사
  position INTEGER,
  our_position INTEGER,                       -- NULL = 우리는 아예 부재
  volume INTEGER,
  kind TEXT,                                  -- missing|weak|shared
  UNIQUE(project_id, checked_date, keyword, domain)
);
CREATE INDEX IF NOT EXISTS idx_kwgap ON keyword_gap(project_id, checked_date, kind);

-- ── 사이트 크롤 (collect_crawl.py) ────────────────────────────────────────────
-- page_audits 는 '기회가 걸린 페이지 20개'를 깊게 본다. 이쪽은 반대다: 사이트를
-- 넓게 돌아 깨진 링크·리다이렉트 사슬·고아 페이지처럼 **전수를 봐야만 나오는 것**을
-- 잡는다. 회차(crawl_runs)로 남기는 이유는 하나다 — 지난번 대비 새로 깨진 것.
CREATE TABLE IF NOT EXISTS crawl_runs (
  id INTEGER PRIMARY KEY,
  project_id INTEGER NOT NULL REFERENCES projects(id),
  started_at TEXT DEFAULT CURRENT_TIMESTAMP,
  finished_at TEXT,
  seed TEXT,                                  -- sitemap|home
  pages INTEGER DEFAULT 0,
  issues INTEGER DEFAULT 0,
  robots_txt TEXT                             -- robots.txt 원문 — 어느 줄에 걸리는지 말하려면 필요하다
);
CREATE TABLE IF NOT EXISTS sitemap_urls (     -- 사이트맵이 시드였을 때 그 목록 그대로
  id INTEGER PRIMARY KEY,
  run_id INTEGER NOT NULL REFERENCES crawl_runs(id),
  url TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sitemap_urls ON sitemap_urls(run_id);
CREATE TABLE IF NOT EXISTS crawl_pages (
  id INTEGER PRIMARY KEY,
  run_id INTEGER NOT NULL REFERENCES crawl_runs(id),
  url TEXT NOT NULL,
  status INTEGER,
  redirect_to TEXT,
  depth INTEGER,                              -- 홈에서 몇 번 눌러야 닿나
  title TEXT, description TEXT, h1 TEXT,
  canonical TEXT, robots TEXT,
  words INTEGER,
  schema_types TEXT,
  links_in INTEGER DEFAULT 0, links_out INTEGER DEFAULT 0,
  images_no_alt INTEGER DEFAULT 0,
  bytes INTEGER,
  UNIQUE(run_id, url)
);
CREATE INDEX IF NOT EXISTS idx_crawl_pages ON crawl_pages(run_id);
CREATE TABLE IF NOT EXISTS crawl_links (
  id INTEGER PRIMARY KEY,
  run_id INTEGER NOT NULL REFERENCES crawl_runs(id),
  url_from TEXT NOT NULL,
  url_to TEXT NOT NULL,
  anchor TEXT,
  is_internal INTEGER DEFAULT 1,
  nofollow INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_crawl_links ON crawl_links(run_id, url_to);
CREATE TABLE IF NOT EXISTS crawl_hreflang (   -- 페이지가 선언한 hreflang — 상호 참조는 전수를 봐야 안다
  id INTEGER PRIMARY KEY,
  run_id INTEGER NOT NULL REFERENCES crawl_runs(id),
  url TEXT NOT NULL,                          -- 선언한 쪽
  code TEXT NOT NULL,                         -- ko, en-GB, x-default …
  href TEXT NOT NULL                          -- 가리키는 쪽 (정규화된 주소)
);
CREATE INDEX IF NOT EXISTS idx_crawl_hreflang ON crawl_hreflang(run_id, href);
CREATE TABLE IF NOT EXISTS crawl_issues (     -- 도출 결과를 저장한다 — 그래야 회차 비교가 된다
  id INTEGER PRIMARY KEY,
  run_id INTEGER NOT NULL REFERENCES crawl_runs(id),
  kind TEXT NOT NULL,                         -- http_error|redirect_chain|broken_internal|...
  severity TEXT,                              -- bad|warn|info
  url TEXT,
  detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_crawl_issues ON crawl_issues(run_id, kind);

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
CREATE TABLE IF NOT EXISTS verdicts (        -- 검색어 심사 (docs/superpowers/specs/2026-09-08-keyword-triage-design.md)
  id INTEGER PRIMARY KEY,                     -- remote.merge 가 표마다 id 로 사이트 소속을 옮긴다 — 판정도 pull/push 를 탄다
  project_id INTEGER NOT NULL REFERENCES projects(id),
  key        TEXT NOT NULL,                   -- scoring.norm(대상). 띄어쓰기·대소문자 변형은 여기서 같은 값이 된다
  verdict    TEXT NOT NULL,                   -- irrelevant | hold | work  (정본은 db.VERDICTS)
  decided_at TEXT DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(project_id, key)
);
"""


def _migrate(conn: sqlite3.Connection) -> None:
    """IF NOT EXISTS가 못 하는 것만. 각 단계는 스스로 "이미 했나"를 보고 건너뛴다 —
    한 단계가 끝났다고 함수 전체를 return하면 그 뒤 단계가 영영 안 돈다.

    SCHEMA는 매 연결마다 재실행되지만 CREATE TABLE IF NOT EXISTS는 이미 존재하는
    테이블을 건드리지 않으므로, 컬럼을 새로 추가하면 기존 Brain에는 반영되지 않는다.
    """
    proj_cols = {r["name"] for r in conn.execute("PRAGMA table_info(projects)")}
    if "ga4_property" not in proj_cols:
        conn.execute("ALTER TABLE projects ADD COLUMN ga4_property TEXT")
        conn.commit()

    # 전체 유기 검색 필터·참여율/체류/깊이 지표가 새로 생겼다(collect_ga4.py 참조).
    # bounceRate는 engagementRate의 여집합이라 버렸다 — 컬럼도 같이 정리한다.
    ga4_cols = {r["name"] for r in conn.execute("PRAGMA table_info(ga4_snapshots)")}
    for col, decl in (("sessions_all", "INTEGER"), ("engagement_rate", "REAL"),
                      ("avg_session_duration", "REAL"), ("pageviews_per_session", "REAL")):
        if col not in ga4_cols:
            conn.execute(f"ALTER TABLE ga4_snapshots ADD COLUMN {col} {decl}")
            conn.commit()
    if "bounce_rate" in ga4_cols:
        conn.execute("ALTER TABLE ga4_snapshots DROP COLUMN bounce_rate")
        conn.commit()

    cols = {r["name"] for r in conn.execute("PRAGMA table_info(keywords)")}
    # volume·difficulty 는 처음부터 있었지만 채우는 코드가 없어 늘 NULL 이었다.
    # cpc·metrics_at 이 붙으면서 "언제 잰 값인가"를 말할 수 있게 된다 — 지표는
    # 유료로 사 오는 것이라 언제 산 값인지가 재구매 판단의 전부다.
    for col, decl in (("cpc", "REAL"), ("metrics_at", "TEXT")):
        if col not in cols:
            conn.execute(f"ALTER TABLE keywords ADD COLUMN {col} {decl}")
            conn.commit()
    # 페이지 감사가 모바일(viewport)·언어(html lang·hreflang)·신선도(발행·수정일)와
    # "정적 HTML 로는 안 보이는 페이지"(js_shell)를 새로 읽는다. 요청문이 이 값들을
    # 사실로 싣기 때문에, 없으면 NULL 로 남아야지 0 이나 빈 문자열이면 안 된다.
    cr_cols = {r["name"] for r in conn.execute("PRAGMA table_info(crawl_runs)")}
    if "robots_txt" not in cr_cols:
        conn.execute("ALTER TABLE crawl_runs ADD COLUMN robots_txt TEXT")
        conn.commit()

    pa_cols = {r["name"] for r in conn.execute("PRAGMA table_info(page_audits)")}
    for col, decl in (("viewport", "TEXT"), ("html_lang", "TEXT"),
                      ("hreflang_json", "TEXT"), ("published", "TEXT"),
                      ("modified", "TEXT"), ("js_shell", "INTEGER")):
        if col not in pa_cols:
            conn.execute(f"ALTER TABLE page_audits ADD COLUMN {col} {decl}")
            conn.commit()

    opp_cols = {r["name"] for r in conn.execute("PRAGMA table_info(opportunities)")}
    if "status_at" not in opp_cols:      # 완료 후 관찰의 기준 시각 (spec keyword-triage §3)
        conn.execute("ALTER TABLE opportunities ADD COLUMN status_at TEXT")
        conn.commit()

    # verdicts 는 처음 며칠 id 없이 나갔다 — remote.merge 가 id 를 요구한다. 빈 표에 가깝지만
    # 판정을 버리지 않고 옮긴다.
    v_cols = {r["name"] for r in conn.execute("PRAGMA table_info(verdicts)")}
    if v_cols and "id" not in v_cols:
        conn.executescript("""
            ALTER TABLE verdicts RENAME TO _verdicts_old;
            CREATE TABLE verdicts (id INTEGER PRIMARY KEY,
              project_id INTEGER NOT NULL REFERENCES projects(id), key TEXT NOT NULL,
              verdict TEXT NOT NULL, decided_at TEXT DEFAULT CURRENT_TIMESTAMP,
              UNIQUE(project_id, key));
            INSERT INTO verdicts(project_id,key,verdict,decided_at)
              SELECT project_id,key,verdict,decided_at FROM _verdicts_old;
            DROP TABLE _verdicts_old;""")
        conn.commit()

    if "locale" not in cols:
        conn.execute("ALTER TABLE keywords ADD COLUMN locale TEXT")
        # 한글이 든 키워드는 프로젝트 로케일이 무엇이든 en-US로 조회하면 안 된다.
        # aitierlist 실사용에서 프로젝트 로케일(en-US)이 한국어 키워드에도 적용돼
        # 실제 3~6위인 6개가 전부 "순위 없음"으로 적재됐다. 비어 있는 칸은 이 함수
        # 끝의 '갈래 5' 블록이 글자 판정(keyword_locale)으로 채운다 — 한글 판정을
        # 여기 GLOB 로 한 벌 더 두지 않는다.
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

    # runs.kind 는 단계 id 와 같은 말이어야 한다(run_all.STAGES 정본) — 화면
    # (history.html)이 단계 용어표 하나로 라벨을 달려면 그래야 한다. collect_gap.py 는
    # 'competitors' 단계인데 kind='gap' 으로, scoring.load() 는 'gaps' 단계인데
    # kind='analysis' 로 적었었다. WHERE 조건 자체가 재실행에 안전하다(이미 바뀐
    # 행은 다시 안 걸린다).
    conn.execute("UPDATE runs SET kind='competitors' WHERE kind='gap'")
    conn.execute("UPDATE runs SET kind='gaps' WHERE kind='analysis'")
    conn.commit()

    # ── 갈래 5: 키워드 언어 ──
    # 옛 행의 언어 표시를 글자 판정으로 한 번 바로잡는다(fix_keyword_locales). 표
    # offlocale_snapshots 가 있으면 이미 한 것이다 — 한 번만 도는 까닭은 그 뒤로는
    # 모든 INSERT 가 판정을 거치고, 사람이 일부러 고친 로케일을 매 연결마다 되돌리면
    # 안 되기 때문이다. 표 생성과 고치기를 한 트랜잭션에 넣는다: 중간에 죽으면 표도
    # 없어져 다음 연결이 처음부터 다시 한다.
    if "offlocale_snapshots" not in {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}:
        if conn.in_transaction:
            conn.commit()
        conn.execute("BEGIN")
        try:
            conn.execute(OFFLOCALE_DDL)
            fix_keyword_locales(conn)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise


# 키워드의 언어 표시를 고칠 때, 그 전 로케일로 잰 순위·검색결과 행을 지우지 않고 여기로
# 옮긴다 — 한국 구글에서 잰 'milia vs syringoma' 순위가 영어 키워드의 순위 이력에 남으면
# 다음 수집(영어 구글)과 비교돼 가짜 등락이 된다. 원래 행은 row_json 에 그대로 있다.
OFFLOCALE_DDL = """CREATE TABLE offlocale_snapshots (
  id INTEGER PRIMARY KEY,
  keyword_id INTEGER NOT NULL REFERENCES keywords(id),
  kind TEXT NOT NULL,                 -- rank (rank_snapshots) | serp (serp_results)
  locale TEXT NOT NULL,               -- 그 행을 잰 언어-지역 — 키워드의 지금 로케일과 다르다
  checked_at TEXT,
  row_json TEXT NOT NULL,             -- 원래 행(id 뺀 전부)
  moved_at TEXT DEFAULT CURRENT_TIMESTAMP
)"""


def fix_keyword_locales(conn: sqlite3.Connection) -> dict[str, int]:
    """언어 표시를 keyword_locale 판정값으로 바로잡는다 — 커밋은 호출자가 한다.

    고치는 행: locale 이 NULL 인 행, ko-* 인데 판정이 한국어가 아닌 행(한글이 없다).
    바뀐 키워드가 그 전 로케일(NULL 이면 사이트 로케일)로 잰 rank_snapshots·
    serp_results 는 offlocale_snapshots 로 옮긴다. keywords 의 유일 키는
    (project_id, keyword) 라 locale 을 바꿔도 부딪힐 행이 없다.
    멱등이다 — 두 번째 호출은 바꿀 것이 없다. 반환: 바뀐 수 {null, ko, rank, serp}.
    """
    n = dict.fromkeys(("null", "ko", "rank", "serp"), 0)
    for p in conn.execute("SELECT id, locale FROM projects").fetchall():
        site = project_locale(p)
        judge = keyword_judge(conn, p["id"])
        for k in conn.execute(
                """SELECT id, keyword, locale FROM keywords
                    WHERE project_id=? AND (locale IS NULL OR lower(locale) LIKE 'ko%')""",
                (p["id"],)).fetchall():
            new = judge(k["keyword"])
            if k["locale"] is None:
                n["null"] += 1
            elif new.lower().startswith("ko"):
                continue                     # 한국어 판정 — 그대로가 맞다
            else:
                n["ko"] += 1
            conn.execute("UPDATE keywords SET locale=? WHERE id=?", (new, k["id"]))
            measured = k["locale"] or site
            if measured == new:
                continue                     # 같은 로케일로 쟀다 — 기록은 그대로 유효하다
            for kind, table in (("rank", "rank_snapshots"), ("serp", "serp_results")):
                rows = conn.execute(f"SELECT * FROM {table} WHERE keyword_id=?",
                                    (k["id"],)).fetchall()
                for r in rows:
                    d = {c: r[c] for c in r.keys() if c != "id"}
                    conn.execute(
                        """INSERT INTO offlocale_snapshots(keyword_id, kind, locale,
                             checked_at, row_json) VALUES(?,?,?,?,?)""",
                        (k["id"], kind, measured, d.get("checked_at"),
                         json.dumps(d, ensure_ascii=False)))
                conn.execute(f"DELETE FROM {table} WHERE keyword_id=?", (k["id"],))
                n[kind] += len(rows)
    return n


def connect(home: Path | None = None) -> sqlite3.Connection:
    """home 을 주면 env(CAPTURE_HOME/CAPTURE_DB) 를 안 보고 그 유저의 brain 을 연다
    (server/store.py 의 Tenant.brain() 이 쓴다). 안 주면 예전처럼 env 를 읽는다."""
    dbp = db_path(home)
    dbp.parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False — FastAPI 는 sync 의존자(yield)와 sync 라우트를 스레드풀의
    # 서로 다른 스레드에서 돌린다. 의존자가 연 커넥션을 라우트가 다른 스레드에서 쓰면
    # ProgrammingError → 500 이다(동시 요청에서만 난다 — 순차면 같은 스레드를 재사용해
    # 우연히 맞는다). 커넥션은 호출마다 새로 열고 한 요청만 쓰므로 스레드를 순서대로
    # 넘겨받을 뿐 동시에 쓰이지 않는다 — 그래서 이 검사를 풀어도 안전하다.
    conn = sqlite3.connect(dbp, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    _register_norm(conn)
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
    conn = sqlite3.connect(db_path().resolve().as_uri() + "?mode=ro", uri=True,
                           check_same_thread=False)   # connect() 의 주석과 같은 이유
    conn.row_factory = sqlite3.Row
    _register_norm(conn)
    return conn


def _register_norm(conn: sqlite3.Connection) -> None:
    """SQL 안에서 norm(text) 를 쓸 수 있게 한다 — 심사(verdicts.key)와 기회·키워드를
    같은 정규화로 조인하려고. 정본은 scoring.norm 하나다(여기에 사본을 두지 않는다).
    scoring 은 db 를 함수 안에서만 부르므로 늦게 import 해도 순환이 없다."""
    import scoring
    conn.create_function("norm", 1, scoring.norm, deterministic=True)


def init_db() -> None:
    conn = connect()
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()
    print(f"Brain ready: {db_path()}")


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# 스키마의 projects.locale DEFAULT 와 같은 값 — 로케일이 비었을 때의 폴백은 이 하나다.
# 수집기마다 "ko-KR" 리터럴을 들고 있던 것을 여기로 모았다.
DEFAULT_LOCALE = "ko-KR"


def project_locale(p) -> str:
    """프로젝트 행(또는 dict)의 언어-지역 — 비어 있으면 DEFAULT_LOCALE."""
    return p["locale"] or DEFAULT_LOCALE


# ── 키워드 언어 판정 — 정본은 keyword_locale 하나다 (사본 금지) ──────────
# keywords 에 들어가는 행은 전부 이 판정을 거친다(sync_project·add_keyword_candidates,
# 그리고 옛 행을 한 번 고치는 fix_keyword_locales). 넣을 때 사이트 로케일을 그대로
# 붙이던 동안 GSC 에서 들어온 영어 검색어가 ko-KR 로 표시돼 한국 구글에서 쟀고
# ('milia vs syringoma'), 시드는 아예 언어 칸 없이 들어갔다.
# (유니코드 구간, 언어) 순서가 곧 우선순위다: 한글이 있으면 한국어, 가나가 섞이면
# 한자가 있어도 일본어. 한글은 조합형(NFD 자모)으로 오는 GSC 검색어가 실제로 있다 —
# 완성형 구간만 보면 '"reminder" korean 알림' 이 한글 없는 키워드로 읽힌다.
_SCRIPTS = [
    (("가", "힣"), "ko"),      # 한글 완성형
    (("ᄀ", "ᇿ"), "ko"),      # 한글 자모(조합형)
    (("㄰", "㆏"), "ko"),      # 한글 호환 자모 (ㅋㅋ)
    (("぀", "ヿ"), "ja"),      # 히라가나·가타카나
    (("一", "鿿"), "zh"),      # 한자
    (("Ѐ", "ӿ"), "ru"),      # 키릴
    (("؀", "ۿ"), "ar"),      # 아랍
    (("฀", "๿"), "th"),      # 타이
    (("ऀ", "ॿ"), "hi"),      # 데바나가리
]
_ENGLISH_FALLBACK = "en-US"


def _latin_only(text: str) -> bool:
    """글자가 라틴 글자뿐인가(숫자·기호·공백은 상관없다). 라틴 글자가 하나도 없으면 False."""
    import unicodedata
    seen = False
    for ch in text:
        if unicodedata.category(ch)[0] != "L":
            continue
        if not unicodedata.name(ch, "").startswith("LATIN"):
            return False
        seen = True
    return seen


def keyword_locale(text: str, site_locale: str, english: str = _ENGLISH_FALLBACK) -> str:
    """키워드 글자만 보고 그 키워드를 잴 언어-지역을 정한다.

    - 제 문자권이 있는 글자(_SCRIPTS)가 있으면 그 언어. 사이트 언어와 같으면 사이트
      로케일(zh-CN 보존), 다르면 그 언어의 대표 로케일(ko-KR·ja-JP…).
    - 라틴 글자(+숫자·기호)만 있으면: 사이트가 제 문자권이 있는 언어(한국어·일본어…)면
      영어로 본다 — `english`. 사이트가 라틴 문자권(영어·독일어·베트남어…)이면 사이트
      로케일 — 라틴 글자로는 영어와 독일어를 못 가른다.
    - 그 밖(숫자만·빈 값·그리스 문자 같은 표 밖 글자)은 사이트 로케일.
    """
    import serp_adapter          # 늦은 import: serp_adapter → collector → db
    text = text or ""
    lang = next((lg for (lo, hi), lg in _SCRIPTS
                 if any(lo <= c <= hi for c in text)), None)
    site_lang = serp_adapter.lang_of(site_locale)
    if lang is not None:
        if lang == site_lang:
            return site_locale
        return f"{lang}-{serp_adapter.location(lang)[2][0].upper()}"
    if _latin_only(text) and site_lang in {lg for _, lg in _SCRIPTS}:
        return english
    return site_locale


def english_locale(conn: sqlite3.Connection, project_id: int) -> str:
    """이 사이트가 영어 키워드를 잴 로케일. 사이트 로케일이 en 계열이면 그것, 아니면
    이 사이트 키워드가 이미 쓰는 en 계열 중 가장 많은 것, 둘 다 없으면 en-US.
    고를 수 있는 목록 정본(serp_adapter.LOCALES) 밖의 값은 고르지 않는다."""
    import serp_adapter
    en = [c for c, _ in serp_adapter.LOCALES if serp_adapter.lang_of(c) == "en"]
    row = conn.execute("SELECT locale FROM projects WHERE id=?", (project_id,)).fetchone()
    if row and row["locale"] in en:
        return row["locale"]
    for r in conn.execute(
            """SELECT locale FROM keywords WHERE project_id=? AND locale IS NOT NULL
               GROUP BY locale ORDER BY COUNT(*) DESC, locale""", (project_id,)):
        if r["locale"] in en:
            return r["locale"]
    return _ENGLISH_FALLBACK


def keyword_judge(conn: sqlite3.Connection, project_id: int):
    """(키워드, 기본값=None) → 로케일. 사이트 로케일과 영어 로케일을 한 번만 읽는다.
    기본값은 그 키워드를 캔 로케일(시드·조회 로케일)이고, 없으면 사이트 로케일이다."""
    row = conn.execute("SELECT locale FROM projects WHERE id=?", (project_id,)).fetchone()
    site = project_locale(row) if row else DEFAULT_LOCALE
    en = english_locale(conn, project_id)
    return lambda kw, default=None: keyword_locale(kw, default or site, en)


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
        """INSERT INTO projects(name,type,domain,locale,gsc_property,ga4_property,config_path)
           VALUES(?,?,?,?,?,?,?)
           ON CONFLICT(name) DO UPDATE SET type=excluded.type, domain=excluded.domain,
             locale=excluded.locale, gsc_property=excluded.gsc_property,
             ga4_property=excluded.ga4_property, config_path=excluded.config_path""",
        (cfg["name"], cfg.get("type", "saas"), cfg["domain"], cfg.get("locale", DEFAULT_LOCALE),
         cfg.get("gsc_property"), cfg.get("ga4_property"), cfg["_path"]),
    )
    pid = conn.execute("SELECT id FROM projects WHERE name=?", (cfg["name"],)).fetchone()[0]
    judge = keyword_judge(conn, pid)     # 시드도 글자로 언어를 받는다 — NULL 로 두지 않는다
    for kw in cfg.get("seed_keywords", []) or []:
        kw = kw.strip()
        conn.execute(
            """INSERT OR IGNORE INTO keywords(project_id,keyword,locale,source,is_active)
               VALUES(?,?,?, 'seed', 1)""", (pid, kw, judge(kw)))
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
# 검색어 심사의 판정 — 무관(자동완성 쓰레기) · 보류(우리 것이지만 지금 안 함) · 작업.
# 화면(triage.html 의 TR_VERDICT)과 test_seams 17 이 이 한 벌을 대조한다.
VERDICTS = ("irrelevant", "hold", "work")


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


def set_ga4_property(conn, project_id: int, property_id: str | None) -> None:
    """GA4 속성을 프로젝트에 건다. 빈 값이면 연결 해제.

    저장은 숫자 ID 문자열이다 — 'properties/' 접두는 collect_ga4 가 API 부를 때
    붙인다(ga4_snapshots 테이블 주석과 같은 계약). projects 스키마를 아는 것은
    이 파일이라, 호스팅 라우트가 UPDATE 문을 따로 들고 있지 않게 여기에 둔다.
    """
    conn.execute("UPDATE projects SET ga4_property=? WHERE id=?",
                 (property_id or None, project_id))
    conn.commit()


def set_locale(conn, project_id: int, locale: str) -> None:
    """언어-지역을 바꾼다 — Brain 과 yaml 둘 다. yaml 을 안 고치면 다음 sync-project 가
    옛 값으로 되돌린다. 이미 캔 키워드의 locale 은 그대로다(그 언어로 잰 값이다)."""
    conn.execute("UPDATE projects SET locale=? WHERE id=?", (locale, project_id))
    conn.commit()
    path = conn.execute("SELECT config_path FROM projects WHERE id=?", (project_id,)).fetchone()[0]
    if path and Path(path).exists():
        import yaml
        cfg = yaml.safe_load(Path(path).read_text("utf-8")) or {}
        cfg["locale"] = locale
        Path(path).write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), "utf-8")


def write_ga4_snapshot(conn: sqlite3.Connection, project_id: int, snapshot_date: str,
                       period_days: int, rows) -> int:
    """GA4 스냅샷 적재. write_gsc_snapshot 과 같은 규칙 — 같은 날 다시 넣으면 덮어쓴다.

    rows: (landing_page, sessions, sessions_all, key_events, total_revenue,
           engagement_rate, avg_session_duration, pageviews_per_session) 순회 가능 객체.
    sessions 는 유기 검색(Organic Search) 세션 — GSC 클릭과 성격이 같은 쪽은 이것이다.
    sessions_all(전 채널)은 유기 몫을 보여주는 맥락용(collect_ga4.py 모듈 docstring 참조).
    landing_page 는 이미 collect_ga4._landing_path() 로 정규화된 값이어야 한다(여기서는
    다시 손대지 않는다 — 정규화 규칙의 정본은 collect_ga4.py 한 곳).
    """
    rows = list(rows)
    conn.execute("DELETE FROM ga4_snapshots WHERE project_id=? AND snapshot_date=?",
                 (project_id, snapshot_date))
    conn.executemany(
        """INSERT INTO ga4_snapshots(project_id, snapshot_date, period_days,
             landing_page, sessions, sessions_all, key_events, total_revenue,
             engagement_rate, avg_session_duration, pageviews_per_session)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        [(project_id, snapshot_date, period_days, lp,
          int(sessions or 0), int(sessions_all or 0), round(float(ke or 0), 2),
          round(float(rev or 0), 2), round(float(er or 0), 4),
          round(float(dur or 0), 1), round(float(ppv or 0), 2))
         for lp, sessions, sessions_all, ke, rev, er, dur, ppv in rows])
    conn.commit()
    return len(rows)


def write_ga4_breakdown(conn: sqlite3.Connection, project_id: int, snapshot_date: str,
                        period_days: int, dim: str, rows) -> int:
    """GA4 차원 분해(device/country/newvsreturning) 적재 — write_gsc_breakdown 과 같은
    규칙(같은 (project, snapshot_date, dim)만 지우고 다시 넣는다; dim별로만 지우는 게
    핵심이다 — 하나가 실패해도 다른 축의 지난 수집분을 지우지 않는다).

    rows: (dim_value, landing_page, sessions, key_events, total_revenue, engagement_rate).
    유기 검색 기준(collect_ga4.py 모듈 docstring). dim_value 는 이미 GSC 표기로 정규화된
    값이어야 한다(device→대문자, country→alpha-3 소문자 — 정본은 collect_ga4.py).
    """
    rows = list(rows)
    conn.execute(
        "DELETE FROM ga4_breakdown WHERE project_id=? AND snapshot_date=? AND dim=?",
        (project_id, snapshot_date, dim))
    conn.executemany(
        """INSERT INTO ga4_breakdown(project_id, snapshot_date, period_days, dim,
             dim_value, landing_page, sessions, key_events, total_revenue, engagement_rate)
           VALUES(?,?,?,?,?,?,?,?,?,?)""",
        [(project_id, snapshot_date, period_days, dim, dv, lp,
          int(sessions or 0), round(float(ke or 0), 2),
          round(float(rev or 0), 2), round(float(er or 0), 4))
         for dv, lp, sessions, ke, rev, er in rows])
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


def add_ai_prompts(conn: sqlite3.Connection, project_id: int, rows) -> int:
    """AI에 물어볼 질문 적재. 이미 있는 질문은 건드리지 않는다(사람이 끈 것을 되살리지
    않는다 — is_active 는 큐레이션 결과다). 돌려주는 값은 **새로 들어간 개수**다."""
    rows = [r for r in rows if (r.get("prompt") or "").strip()]
    before = conn.execute("SELECT COUNT(*) FROM ai_prompts WHERE project_id=?",
                          (project_id,)).fetchone()[0]
    conn.executemany(
        "INSERT INTO ai_prompts(project_id, prompt, category, is_active) VALUES(?,?,?,1) "
        "ON CONFLICT(project_id, prompt) DO NOTHING",
        [(project_id, r["prompt"].strip(), r.get("category") or "general") for r in rows])
    conn.commit()
    after = conn.execute("SELECT COUNT(*) FROM ai_prompts WHERE project_id=?",
                         (project_id,)).fetchone()[0]
    return after - before


def list_ai_prompts(conn: sqlite3.Connection, project_id: int,
                    limit: int = 300) -> list[sqlite3.Row]:
    """AI에 물어볼 질문 전부 + 지금까지 물어본 횟수·인용된 횟수.

    만들기만 하고 볼 수가 없었다 — 인용 확인을 한 번 돌리기 전에는 어떤 질문이
    심겼는지 화면 어디에도 안 나왔고, 그래서 엉뚱한 질문이 섞여도 지울 방법이
    없었다. 켠 것을 먼저, 그다음 만든 순서로 준다.
    """
    return conn.execute(
        """SELECT p.id, p.prompt, p.category, p.is_active,
                  COUNT(c.id) checks, COALESCE(SUM(c.cited), 0) cited
             FROM ai_prompts p
             LEFT JOIN ai_checks c ON c.prompt_id = p.id
            WHERE p.project_id=?
            GROUP BY p.id ORDER BY p.is_active DESC, p.id LIMIT ?""",
        (int(project_id), int(limit))).fetchall()


def count_active_ai_prompts(conn: sqlite3.Connection, project_id: int) -> int:
    """켜진 질문 수. 상한 판정과 화면 표시가 같은 값을 본다 —
    한 번 물을 때 이 수 × 엔진 수 × 샘플 수만큼 돈이 나간다."""
    return conn.execute(
        "SELECT COUNT(*) FROM ai_prompts WHERE project_id=? AND is_active=1",
        (int(project_id),)).fetchone()[0]


def set_ai_prompts_active(conn: sqlite3.Connection, project_id: int, ids,
                          active: bool) -> int:
    """질문 켜기/끄기. 반환: 실제로 손댄 행 수(남의 사이트 id 는 안 세어진다).

    지우는 것과 다르다 — 끈 질문은 지금까지 쌓인 ai_checks 를 그대로 들고 있어
    추이가 끊기지 않는다. 상한은 여기 없다(값을 아는 호출부가 ids 를 자른다).
    """
    ids = [int(x) for x in ids]
    if not ids:
        return 0
    cur = conn.execute(
        f"UPDATE ai_prompts SET is_active=? WHERE project_id=? "
        f"AND id IN ({','.join('?' * len(ids))})",
        [1 if active else 0, int(project_id), *ids])
    conn.commit()
    return cur.rowcount


def update_ai_prompt(conn: sqlite3.Connection, project_id: int, prompt_id: int,
                     prompt: str, category: str | None = None) -> int:
    """질문 문구 고치기. 같은 사이트에 같은 문구가 이미 있으면 IntegrityError —
    UNIQUE(project_id, prompt) 가 그 사실을 말하게 두고 여기서 삼키지 않는다."""
    prompt = " ".join(str(prompt).split())
    if not prompt:
        return 0
    cur = conn.execute(
        "UPDATE ai_prompts SET prompt=?, category=COALESCE(?, category) "
        "WHERE project_id=? AND id=?",
        (prompt, category or None, int(project_id), int(prompt_id)))
    conn.commit()
    return cur.rowcount


def delete_ai_prompts(conn: sqlite3.Connection, project_id: int, ids) -> int:
    """질문과 그 질문으로 받은 확인 기록을 함께 지운다.

    ai_checks 를 남겨 두면 prompt_id 가 가리킬 곳이 없어 화면의 질문별 결과가
    빈 줄로 남는다. 기록만 지키고 싶으면 delete 가 아니라 끄기(set_..._active)다.
    """
    ids = [int(x) for x in ids]
    if not ids:
        return 0
    marks = ','.join('?' * len(ids))
    own = [r[0] for r in conn.execute(
        f"SELECT id FROM ai_prompts WHERE project_id=? AND id IN ({marks})",
        [int(project_id), *ids])]
    if not own:
        return 0
    marks = ','.join('?' * len(own))
    conn.execute(f"DELETE FROM ai_checks WHERE prompt_id IN ({marks})", own)
    cur = conn.execute(f"DELETE FROM ai_prompts WHERE id IN ({marks})", own)
    conn.commit()
    return cur.rowcount


def write_page_audits(conn: sqlite3.Connection, project_id: int, checked_date: str,
                      rows) -> int:
    """내 페이지 감사 결과 적재. rows: dict (키는 테이블 컬럼명 그대로, url 필수).

    write_index_status 와 같은 규칙 — 같은 날 나눠 돌려도 앞 배치를 지우지 않는
    upsert 이고, 없는 키는 NULL 이다("못 읽음"과 "없음"은 다르다).
    """
    cols = ("status", "error", "title", "meta_description", "h1_json", "h2_json",
            "words", "schema_json", "canonical", "robots",
            "internal_links", "external_links", "images", "images_no_alt",
            "viewport", "html_lang", "hreflang_json", "published", "modified", "js_shell")
    rows = list(rows)
    conn.executemany(
        f"""INSERT INTO page_audits(project_id, checked_date, url, {', '.join(cols)})
            VALUES(?,?,?,{','.join('?' * len(cols))})
            ON CONFLICT(project_id, checked_date, url) DO UPDATE SET
              {', '.join(f'{c}=excluded.{c}' for c in cols)}""",
        [(project_id, checked_date, r["url"]) + tuple(r.get(c) for c in cols)
         for r in rows])
    conn.commit()
    return len(rows)


# 검색결과에서 몇 자리까지 남기나. 요청문이 "상위에 있는 페이지"를 보여주는 데
# 쓰는 값이라 넉넉할 필요가 없다 — 사람이 실제로 비교하는 것은 위 몇 개다.
SERP_KEEP = 5


def write_page_vitals(conn: sqlite3.Connection, project_id: int, checked_date: str,
                      rows) -> int:
    """속도 측정 결과 적재. write_page_audits 와 같은 규칙 — 같은 날 나눠 돌려도 앞
    배치를 안 지우는 upsert 이고, 없는 키는 NULL 이다(안 잰 것과 0 은 다르다)."""
    cols = ("error", "origin_fallback", "field_verdict",
            "field_lcp_ms", "field_inp_ms", "field_cls", "field_ttfb_ms",
            "lab_score", "lab_lcp_ms", "lab_cls", "lab_tbt_ms")
    rows = list(rows)
    conn.executemany(
        f"""INSERT INTO page_vitals(project_id, checked_date, url, strategy, {', '.join(cols)})
            VALUES(?,?,?,?,{','.join('?' * len(cols))})
            ON CONFLICT(project_id, checked_date, url, strategy) DO UPDATE SET
              {', '.join(f'{c}=excluded.{c}' for c in cols)}""",
        [(project_id, checked_date, r["url"], r["strategy"]) + tuple(r.get(c) for c in cols)
         for r in rows])
    conn.commit()
    return len(rows)


def write_sitemap_urls(conn: sqlite3.Connection, run_id: int, urls) -> int:
    """사이트맵 URL 목록을 크롤 회차에 매단다. 재실행은 덮어쓴다."""
    urls = [u for u in urls if u]
    conn.execute("DELETE FROM sitemap_urls WHERE run_id=?", (run_id,))
    conn.executemany("INSERT INTO sitemap_urls(run_id, url) VALUES(?,?)",
                     [(run_id, u) for u in urls])
    conn.commit()
    return len(urls)


def write_serp_results(conn: sqlite3.Connection, keyword_id: int, rows,
                       checked_at: str | None = None) -> int:
    """검색결과 상위 몇 줄을 그대로 남긴다. 같은 키워드·같은 날은 덮어쓴다(하루 한 벌).

    이 표가 없던 동안, 요청문은 상위 페이지의 제목을 **사람에게 붙여 넣으라고**
    시켰다 — 방금 돈 주고 받아 온 응답 안에 그 제목이 들어 있는데도. rank_snapshots
    는 "내가 몇 위인가" 만 남기느라 나머지를 버렸다.
    """
    rows = [r for r in rows if r.get("position") is not None]
    if not rows:
        return 0
    ts = checked_at or now()
    # write_rank_snapshot 과 같은 delete-then-insert — 재실행이 중복을 안 쌓는다.
    conn.execute("DELETE FROM serp_results WHERE keyword_id=? AND date(checked_at)=date(?)",
                 (keyword_id, ts))
    conn.executemany(
        """INSERT INTO serp_results(keyword_id, checked_at, position, url, title, domain, is_own)
           VALUES(?,?,?,?,?,?,?)""",
        [(keyword_id, ts, r["position"], r.get("url"), r.get("title"),
          r.get("domain"), int(bool(r.get("is_own")))) for r in rows[:SERP_KEEP]])
    conn.commit()
    return min(len(rows), SERP_KEEP)


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
            "UPDATE opportunities SET status=?, status_at=CURRENT_TIMESTAMP WHERE id=? AND project_id=?",
            (status, int(opp_id), int(project_id)))
    else:
        cur = conn.execute(
            "UPDATE opportunities SET status=?, status_at=CURRENT_TIMESTAMP WHERE id=?",
            (status, int(opp_id)))
    conn.commit()
    return cur.rowcount


def set_verdicts(conn: sqlite3.Connection, project_id: int, keys: list[str],
                 verdict: str | None) -> int:
    """검색어 심사 저장. keys 는 이미 scoring.norm 을 거친 것(서버가 만든 key 를 화면이
    그대로 돌려보낸다). None 이면 미판정으로 되돌린다(행 삭제).
    irrelevant 는 같은 키의 키워드 추적(is_active)을 끈다 — hold 는 우리 검색어라
    측정을 계속한다. 갱신한 행 수를 돌려준다."""
    if verdict is not None and verdict not in VERDICTS:
        raise ValueError(f"verdict must be one of {VERDICTS} or None, got {verdict!r}")
    _register_norm(conn)
    keys = [str(k) for k in keys if str(k)]
    n = 0
    for k in keys:
        if verdict is None:
            n += conn.execute("DELETE FROM verdicts WHERE project_id=? AND key=?",
                              (int(project_id), k)).rowcount
            continue
        conn.execute(
            """INSERT INTO verdicts(project_id, key, verdict) VALUES(?,?,?)
               ON CONFLICT(project_id, key) DO UPDATE SET
                 verdict=excluded.verdict, decided_at=CURRENT_TIMESTAMP""",
            (int(project_id), k, verdict))
        n += 1
        if verdict == "irrelevant":
            conn.execute("UPDATE keywords SET is_active=0 WHERE project_id=? AND norm(keyword)=?",
                         (int(project_id), k))
    conn.commit()
    return n


def verdict_map(conn: sqlite3.Connection, project_id: int) -> dict[str, str]:
    """key → verdict. 미판정 키는 없다."""
    return {r[0]: r[1] for r in conn.execute(
        "SELECT key, verdict FROM verdicts WHERE project_id=?", (int(project_id),))}


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
    넘긴 locale 은 **기본값**이다 — 저장되는 값은 keyword_locale 이 글자로 정한다
    (사이트 로케일을 그대로 붙이던 동안 영어 GSC 검색어 557개가 ko-KR 이었다).
    """
    judge = keyword_judge(conn, project_id)
    n = 0
    for kw, locale, source in items:
        kw = (kw or "").strip()
        if not kw:
            continue
        cur = conn.execute(
            """INSERT OR IGNORE INTO keywords(project_id, keyword, locale, source, is_active)
               VALUES(?,?,?,?,0)""", (project_id, kw, judge(kw, locale), source))
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
                       with_id: bool = True,
                       gated: bool = False) -> list[sqlite3.Row]:
    """기회 목록 조회.

    gated=True 면 심사를 거친 것만 낸다: 검색어 종류(scoring.KEYWORD_KINDS)는
    verdicts 에 '작업' 판정이 있어야 하고, 그 밖의 종류는 그대로 통과한다. 미판정은
    안 보인다 — 심사 화면이 그 행을 본다. 열린 기회(open_opportunities)와 화면
    (scoring.opportunities)이 이걸 켠다.

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

    if gated:
        q += " AND " + gate_sql(conn)
        import scoring
        args += list(scoring.KEYWORD_KINDS)

    q += f" {order_clause} LIMIT ?"
    args.append(int(limit))
    return conn.execute(q, args).fetchall()


def watch_rows(conn: sqlite3.Connection, project_id: int) -> list[dict]:
    """완료된 검색어 기회의 전·후 — 상태를 늘리지 않고 조회로 관찰을 만든다.

    before 는 status_at 이전 마지막 스냅샷, after 는 그 뒤 최신 스냅샷의 그 검색어
    행(최신 스냅샷과 같은 period_days 만 — 기간이 다른 것을 빼면 Δ가 거짓이다).
    runs_since 는 완료 뒤 서로 다른 수집일 수. 두 번 이상 쟀는데 순위가 안 올랐으면
    stalled — 화면이 [다시 열기]를 낸다."""
    import scoring
    ph = ",".join("?" * len(scoring.KEYWORD_KINDS))
    opps = conn.execute(
        f"""SELECT id, kind, target, status_at FROM opportunities
             WHERE project_id=? AND status='done' AND status_at IS NOT NULL AND kind IN ({ph})
             ORDER BY status_at DESC LIMIT 100""",
        (int(project_id), *scoring.KEYWORD_KINDS)).fetchall()
    if not opps:
        return []
    latest = conn.execute(
        "SELECT snapshot_date, period_days FROM gsc_snapshots WHERE project_id=?"
        " ORDER BY snapshot_date DESC LIMIT 1", (int(project_id),)).fetchone()
    per = latest["period_days"] if latest else None

    def at(op: str, day: str, target: str) -> dict | None:
        if per is None:
            return None
        r = conn.execute(
            f"""SELECT SUM(clicks) c, AVG(position) p FROM gsc_snapshots
                 WHERE project_id=? AND period_days=? AND query=? AND snapshot_date {op} ?
                   AND snapshot_date=(SELECT MAX(snapshot_date) FROM gsc_snapshots
                        WHERE project_id=? AND period_days=? AND query=? AND snapshot_date {op} ?)""",
            (int(project_id), per, target, day, int(project_id), per, target, day)).fetchone()
        if not r or r["p"] is None:
            return None
        return {"clicks": int(r["c"] or 0), "position": round(float(r["p"]), 1)}

    out = []
    for o in opps:
        day = o["status_at"][:10]
        before, after = at("<=", day, o["target"]), at(">", day, o["target"])
        runs = conn.execute(
            "SELECT COUNT(DISTINCT snapshot_date) FROM gsc_snapshots WHERE project_id=?"
            " AND period_days=? AND snapshot_date>?", (int(project_id), per, day)).fetchone()[0] if per else 0
        stalled = runs >= 2 and (after is None or before is None
                                 or after["position"] >= before["position"])
        out.append({"id": o["id"], "kind": o["kind"], "target": o["target"],
                    "done_at": o["status_at"], "before": before, "after": after,
                    "runs_since": int(runs), "stalled": bool(stalled)})
    return out


def gate_sql(conn: sqlite3.Connection, table: str = "opportunities") -> str:
    """심사 통과 조건 한 벌 — 검색어 종류는 verdicts 에 '작업'이 있어야 하고 나머지는
    통과. 자리표(?)는 scoring.KEYWORD_KINDS 순서로 채운다. norm() 을 쓰므로 검사가
    만드는 맨 sqlite3 연결에도 함수를 다시 걸어 둔다(create_function 은 멱등)."""
    import scoring
    _register_norm(conn)
    ph = ",".join("?" * len(scoring.KEYWORD_KINDS))
    return (f"({table}.kind NOT IN ({ph}) OR EXISTS (SELECT 1 FROM verdicts v"
            f" WHERE v.project_id={table}.project_id AND v.key=norm({table}.target)"
            " AND v.verdict='work'))")


def open_opportunities(conn: sqlite3.Connection, project_id: int,
                       kinds: list[str] | None = None,
                       limit: int = 10) -> list[sqlite3.Row]:
    """미완료 기회 목록 (status IN ('new','acked')).

    작업 중인(acked) 기회를 먼저 보여주기 위해 scoring.opportunities(new 우선, 화면용)와 정렬이 일부러 다르다.
    """
    return list_opportunities(conn, project_id, kinds=kinds,
                              statuses=["new", "acked"], order="triage",
                              limit=limit, with_id=True, gated=True)


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
        """SELECT id, opportunity_id, kind, file_path, branch, note, merged, created_at
             FROM creations WHERE project_id=? ORDER BY id DESC LIMIT ?""",
        (int(project_id), int(limit))).fetchall()


def count_active_keywords(conn: sqlite3.Connection, project_id: int) -> int:
    """추적 중(is_active=1) 키워드 수. 상한 판정과 화면 표시가 같은 값을 본다."""
    return conn.execute(
        "SELECT COUNT(*) FROM keywords WHERE project_id=? AND is_active=1",
        (int(project_id),)).fetchone()[0]


def list_keywords(conn: sqlite3.Connection, project_id: int, *,
                  active: bool, limit: int = 500) -> list[sqlite3.Row]:
    """추적 중(active=True) 또는 후보(False) 키워드 + 서치콘솔 누적 노출·클릭·검색량.

    노출 많은 순이되, 노출이 같으면(대개 0이다) 검색량 큰 순이다 — 후보 목록에서
    사람이 고를 때 "구글이 이미 보여주고 있는 것"이 먼저 오고, 아직 안 뜨는 것들
    사이에서는 **수요가 큰 것**이 위로 와야 한다. 검색량은 collect_metrics 가 채운다
    (NULL = 아직 안 샀다는 뜻이지 0 이 아니다).
    """
    return conn.execute(
        """SELECT k.id, k.keyword, k.source, k.cluster,
                  k.volume, k.difficulty, k.cpc,
                  COALESCE(SUM(g.impressions),0) imp, COALESCE(SUM(g.clicks),0) clk
             FROM keywords k
             LEFT JOIN gsc_snapshots g
               ON g.project_id=k.project_id AND g.query=k.keyword
            WHERE k.project_id=? AND k.is_active=?
            GROUP BY k.id
            ORDER BY imp DESC, k.volume DESC, k.keyword LIMIT ?""",
        (int(project_id), 1 if active else 0, int(limit))).fetchall()


def set_keywords_active(conn: sqlite3.Connection, project_id: int, ids,
                        active: bool) -> int:
    """키워드 추적 토글. 반환: 실제로 손댄 행 수(남의 사이트 id 는 안 세어진다).

    상한(몇 개까지 켤 수 있나)은 여기 없다 — 값을 아는 호출부가 ids 를 잘라서 넘긴다.
    """
    ids = [int(x) for x in ids]
    if not ids:
        return 0
    cur = conn.execute(
        f"UPDATE keywords SET is_active=? WHERE project_id=? AND id IN ({','.join('?' * len(ids))})",
        [1 if active else 0, int(project_id), *ids])
    conn.commit()
    return cur.rowcount


def query_performance(conn: sqlite3.Connection, project_id: int,
                      query: str) -> dict | None:
    """검색어 하나의 서치콘솔 누적 성과 — {clicks, impressions, position}.

    노출이 0 이면 None 이다. "노출 없음"과 "클릭 0"은 다르고, 근거로 쓸 수 있는 건
    노출이 있을 때뿐이라 그 판정을 호출부마다 다시 쓰지 않게 여기서 끝낸다.
    """
    r = conn.execute(
        """SELECT SUM(clicks) c, SUM(impressions) i, AVG(position) pos
             FROM gsc_snapshots WHERE project_id=? AND query=?""",
        (int(project_id), query)).fetchone()
    if not r or not r["i"]:
        return None
    return {"clicks": int(r["c"] or 0), "impressions": int(r["i"]),
            "position": round(float(r["pos"]), 1)}


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
    # finally 로 닫는다 — 거절 경로의 sys.exit 과 re-raise 가 close 를 건너뛰었다.
    # 그러면 커넥션이 예외 트레이스백의 순환에 걸려 gc 때까지 살아 있고, 윈도우는
    # 열린 파일을 못 지워 임시 brain 을 쓰는 검사가 PermissionError 로 죽는다.
    try:
        try:
            rows = conn.execute(query).fetchall()
        except sqlite3.OperationalError as e:
            if "readonly" not in str(e).lower():
                raise
            sys.exit("이 통로는 조회 전용입니다 — Brain을 바꾸려면 scoring.md 5절의 "
                     "파이썬 원라이너(db.connect())를 쓰세요.")
        print(json.dumps([dict(r) for r in rows], ensure_ascii=False, indent=2, default=str))
    finally:
        conn.close()


def _selfcheck() -> None:
    """임시 brain.db 하나로 읽기 동사들을 실제로 돌려본다 — 서버 라우트가 SQL 대신
    이 이름들을 부르므로, 여기서 깨지면 웹 화면이 같이 깨진다."""
    import os
    import tempfile

    old = os.environ.get("CAPTURE_HOME")
    with tempfile.TemporaryDirectory() as d:
        os.environ["CAPTURE_HOME"] = d

        # home= 을 주면 env(CAPTURE_HOME) 을 안 보고 그 경로의 brain 을 연다 —
        # server/store.py 의 Tenant.brain() 이 기대는 계약.
        other = Path(d) / "other-tenant"
        c2 = connect(home=other)
        try:
            assert (other / "brain.db").exists(), "home= 이 준 경로에 안 생겼다"
        finally:
            c2.close()

        conn = connect()
        try:
            # 백링크 테이블의 소유자가 db.py 다 — server/backlinks.py 가 만들지 않는다.
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            assert {"backlink_summary", "referring_domains"} <= tables, tables

            conn.execute("INSERT INTO projects(name,type,domain) VALUES('p','saas','example.com')")
            conn.commit()
            pid = get_project(conn, "p")["id"]

            add_keyword_candidates(conn, pid, [("가", None, "autocomplete"),
                                               ("나", None, "autocomplete"),
                                               ("다", None, "autocomplete")])
            kids = [r["id"] for r in conn.execute(
                "SELECT id FROM keywords WHERE project_id=? ORDER BY keyword", (pid,))]
            assert count_active_keywords(conn, pid) == 0

            write_gsc_snapshot(conn, pid, "2026-08-01", 28,
                               [("나", "/n", 3, 100, 0.03, 7.0),
                                ("나", "/n2", 1, 50, 0.02, 9.0)])

            cands = list_keywords(conn, pid, active=False)
            assert [r["keyword"] for r in cands] == ["나", "가", "다"], \
                [r["keyword"] for r in cands]          # 노출 많은 순, 그다음 가나다
            assert cands[0]["imp"] == 150 and cands[0]["clk"] == 4, dict(cands[0])
            assert cands[1]["imp"] == 0, dict(cands[1])   # 노출 없음은 0 (NULL 아님)

            assert set_keywords_active(conn, pid, [kids[0], kids[1]], True) == 2
            assert set_keywords_active(conn, pid, [999999], True) == 0, "남의 id 가 세어진다"
            assert count_active_keywords(conn, pid) == 2
            assert [r["keyword"] for r in list_keywords(conn, pid, active=True)] == ["나", "가"]
            assert set_keywords_active(conn, pid, [], False) == 0, "빈 목록이 IN () 을 만든다"
            assert set_keywords_active(conn, pid, [kids[1]], False) == 1
            assert count_active_keywords(conn, pid) == 1

            perf = query_performance(conn, pid, "나")
            assert perf == {"clicks": 4, "impressions": 150, "position": 8.0}, perf
            assert query_performance(conn, pid, "가") is None, "노출 0 인데 근거가 있다고 한다"

            upsert_opportunities(conn, pid, None, [("striking_distance", "나", 80.0, "왜")])
            opp = list_opportunities(conn, pid, order="screen", limit=5)[0]
            got = get_opportunity(conn, opp["id"], project_id=pid)
            assert got["target"] == "나" and got["status"] == "new", dict(got)

            cid = record_creation(conn, pid, "docs/n.md", opportunity_id=opp["id"],
                                  kind="striking_distance", branch="capture/x", note="pr-url")
            rows = list_creations(conn, pid, limit=50)
            assert len(rows) == 1 and rows[0]["id"] == cid, rows
            assert rows[0]["note"] == "pr-url", dict(rows[0])   # 웹 화면이 PR 링크로 쓴다

            # AI 질문 — 만들고, 보고, 고치고, 끄고, 지운다 (웹 화면이 이 순서로 쓴다)
            assert add_ai_prompts(conn, pid, [
                {"prompt": "밀리아 제거 잘하는 병원 어디야?", "category": "추천"},
                {"prompt": "점 빼기랑 밀리아 제거 뭐가 달라?", "category": "비교"}]) == 2
            qs = list_ai_prompts(conn, pid)
            assert [r["prompt"] for r in qs] == ["밀리아 제거 잘하는 병원 어디야?",
                                                 "점 빼기랑 밀리아 제거 뭐가 달라?"], qs
            assert qs[0]["checks"] == 0 and qs[0]["cited"] == 0, dict(qs[0])
            assert count_active_ai_prompts(conn, pid) == 2

            record_ai_check(conn, qs[0]["id"], None, "chatgpt", 0, True, True,
                            ["rival.com"], "답변 본문")
            assert dict(list_ai_prompts(conn, pid)[0])["checks"] == 1

            assert update_ai_prompt(conn, pid, qs[1]["id"], "  밀리아 왜   생겨? ") == 1
            assert list_ai_prompts(conn, pid)[1]["prompt"] == "밀리아 왜 생겨?"
            try:      # 같은 사이트에 같은 문구 둘 — UNIQUE 가 말하게 둔다
                update_ai_prompt(conn, pid, qs[1]["id"], "밀리아 제거 잘하는 병원 어디야?")
                raise AssertionError("중복 문구가 통과했다")
            except sqlite3.IntegrityError:
                conn.rollback()
            assert update_ai_prompt(conn, pid, 999999, "남의 질문") == 0, "남의 id 가 고쳐진다"

            assert set_ai_prompts_active(conn, pid, [qs[1]["id"]], False) == 1
            assert count_active_ai_prompts(conn, pid) == 1
            assert set_ai_prompts_active(conn, pid, [], True) == 0, "빈 목록이 IN () 을 만든다"
            assert [r["is_active"] for r in list_ai_prompts(conn, pid)] == [1, 0]  # 켠 것 먼저

            assert delete_ai_prompts(conn, pid, [999999]) == 0, "남의 id 가 지워진다"
            assert delete_ai_prompts(conn, pid, [qs[0]["id"]]) == 1
            assert conn.execute("SELECT COUNT(*) FROM ai_checks").fetchone()[0] == 0,                 "질문은 지웠는데 확인 기록이 남아 화면에 빈 줄이 선다"
        finally:
            conn.close()
    if old is None:
        os.environ.pop("CAPTURE_HOME", None)
    else:
        os.environ["CAPTURE_HOME"] = old
    _check_keyword_locale()
    print("db: ok")


def _check_keyword_locale() -> None:
    """갈래 5 — 키워드 언어 판정 / 시드가 NULL 을 안 남긴다 / 한 번 도는 마이그레이션이
    멱등이다 / 유일 제약과 부딪히지 않는다. 임시 CAPTURE_HOME 에서만 돈다."""
    import os
    import tempfile

    import serp_adapter

    kl = keyword_locale
    # 1. 판정 — 한글 / 영어 / 섞임
    assert kl("밀리아 제거", "ko-KR") == "ko-KR"
    assert kl("밀리아 제거", "en-US") == "ko-KR", "영어 사이트의 한글 키워드는 한국어"
    assert kl("알림", "en-US") == "ko-KR", \
        "조합형(NFD) '알림' 을 한글로 못 읽는다 — GSC 가 실제로 이렇게 준다"
    assert kl("ㅋㅋ", "en-US") == "ko-KR"
    assert kl("milia vs syringoma", "ko-KR") == "en-US", "한국어 사이트의 영어 키워드는 영어"
    assert kl("milia vs syringoma", "ko-KR", english="en-GB") == "en-GB"
    assert kl("milia vs syringoma", "en-GB") == "en-GB", "사이트가 쓰는 en 로케일을 따른다"
    assert kl("kaufen", "de-DE") == "de-DE", "라틴 문자권 사이트의 라틴 키워드는 사이트 언어"
    assert kl("café 2024!", "ja-JP") == "en-US"
    assert kl("botox 강남", "en-US") == "ko-KR", "섞임 — 한글이 있으면 한국어"
    assert kl("강남 botox", "ko-KR") == "ko-KR" and kl("botox 강남", "ja-JP") == "ko-KR", \
        "섞임 — 라틴이 섞여도 한글이 있으면 한국어"
    assert kl("東京 ラーメン", "ko-KR") == "ja-JP"
    assert kl("2024", "ko-KR") == "ko-KR" and kl("", "ko-KR") == "ko-KR", "숫자만·빈 값은 기본값"
    assert kl("αβγ test", "ko-KR") == "ko-KR", "표 밖 글자가 섞이면 사이트 기본값"
    codes = {c for c, _ in serp_adapter.LOCALES}
    for s in ("milia", "밀리아", "東京", "купить", "ราคา"):
        assert kl(s, "ko-KR") in codes, f"{s!r} 의 판정 {kl(s, 'ko-KR')} 이 고를 수 있는 목록 밖"

    old = os.environ.get("CAPTURE_HOME")
    with tempfile.TemporaryDirectory() as d:
        os.environ["CAPTURE_HOME"] = d
        try:
            # 2. sync_project 가 언어 칸을 비워 두지 않는다
            y = Path(d) / "lp.yaml"
            y.write_text("name: lp\ndomain: lp.com\nlocale: ko-KR\n"
                         "seed_keywords: ['밀리아 제거', 'milia removal', '2024']\n", "utf-8")
            sync_project(str(y))
            conn = connect()
            pid = get_project(conn, "lp")["id"]
            got = {r["keyword"]: r["locale"] for r in conn.execute(
                "SELECT keyword, locale FROM keywords WHERE project_id=?", (pid,))}
            assert got == {"밀리아 제거": "ko-KR", "milia removal": "en-US", "2024": "ko-KR"}, got

            # 3. 후보 적재는 넘겨받은 로케일을 기본값으로만 쓴다 — 이 사이트가 en-US 를
            #    이미 쓰니 그걸 고른다
            conn.execute("UPDATE keywords SET locale='en-GB' WHERE keyword='milia removal'")
            add_keyword_candidates(conn, pid, [("milia vs syringoma", "ko-KR", "gsc"),
                                               ("밀리아 원인", None, "gsc")])
            got = {r["keyword"]: r["locale"] for r in conn.execute(
                "SELECT keyword, locale FROM keywords WHERE project_id=?", (pid,))}
            assert got["milia vs syringoma"] == "en-GB", got
            assert got["밀리아 원인"] == "ko-KR", got

            # 4. 옛 brain 을 흉내 낸다 — 판정 전 행 + 그 로케일로 잰 순위·검색결과
            conn.execute("DROP TABLE offlocale_snapshots")
            conn.execute("DELETE FROM keywords")
            conn.execute("INSERT INTO projects(name,domain,locale) VALUES('en','en.com','en-US')")
            epid = get_project(conn, "en")["id"]
            rows = [(pid, "milia vs syringoma", "ko-KR"),   # 영어인데 ko — 고친다
                    (pid, "밀리아", "ko-KR"),                # 한글 ko — 그대로
                    (pid, "2024", "ko-KR"),                  # 판정도 ko — 그대로
                    (pid, "시드 키워드", None),              # NULL → ko-KR (잰 로케일과 같다)
                    (pid, "seed keyword", None),             # NULL → en-US (ko 로 쟀다)
                    (epid, "milia", "ko-KR")]                # 영어 사이트 — en-US
            conn.executemany("INSERT INTO keywords(project_id,keyword,locale) VALUES(?,?,?)", rows)
            kid = {r["keyword"]: r["id"] for r in conn.execute("SELECT id, keyword FROM keywords")}
            for kw in ("milia vs syringoma", "밀리아", "시드 키워드", "seed keyword"):
                write_rank_snapshot(conn, kid[kw], 7, "https://lp.com/x",
                                    checked_at="2026-09-01T00:00:00Z")
            write_rank_snapshot(conn, kid["milia vs syringoma"], 5, None,
                                checked_at="2026-09-08T00:00:00Z")
            write_serp_results(conn, kid["milia vs syringoma"],
                               [{"position": 1, "url": "https://kr.example/a", "domain": "kr.example"}],
                               checked_at="2026-09-08T00:00:00Z")
            conn.commit()
            conn.close()

            def state(c):
                return ([tuple(r) for r in c.execute(
                            "SELECT id, project_id, keyword, locale FROM keywords ORDER BY id")],
                        [tuple(r) for r in c.execute("SELECT * FROM rank_snapshots ORDER BY id")],
                        [tuple(r) for r in c.execute("SELECT * FROM serp_results ORDER BY id")],
                        [tuple(r) for r in c.execute(
                            "SELECT id, keyword_id, kind, locale, checked_at, row_json"
                            " FROM offlocale_snapshots ORDER BY id")])

            conn = connect()                          # 여기서 마이그레이션이 돈다
            loc = {r["keyword"]: r["locale"] for r in conn.execute(
                "SELECT keyword, locale FROM keywords")}
            assert loc == {"milia vs syringoma": "en-US", "밀리아": "ko-KR", "2024": "ko-KR",
                           "시드 키워드": "ko-KR", "seed keyword": "en-US", "milia": "en-US"}, loc
            assert conn.execute("SELECT COUNT(*) FROM keywords WHERE locale IS NULL"
                                ).fetchone()[0] == 0, "NULL 이 남았다"
            # 잘못된 로케일로 잰 기록은 지우지 않고 옮긴다 — 비교(순위 등락·최신 순위)에서 빠진다
            ranks = {r["keyword_id"] for r in conn.execute("SELECT keyword_id FROM rank_snapshots")}
            assert ranks == {kid["밀리아"], kid["시드 키워드"]}, ranks
            assert conn.execute("SELECT COUNT(*) FROM serp_results").fetchone()[0] == 0
            parked = [(r["keyword_id"], r["kind"], r["locale"]) for r in conn.execute(
                "SELECT * FROM offlocale_snapshots ORDER BY id")]
            assert sorted(parked) == sorted([
                (kid["milia vs syringoma"], "rank", "ko-KR"),
                (kid["milia vs syringoma"], "rank", "ko-KR"),
                (kid["milia vs syringoma"], "serp", "ko-KR"),
                (kid["seed keyword"], "rank", "ko-KR")]), parked
            row = json.loads(conn.execute(
                "SELECT row_json FROM offlocale_snapshots WHERE kind='rank' ORDER BY checked_at DESC"
                ).fetchone()[0])
            assert row["position"] == 5 and row["checked_at"] == "2026-09-08T00:00:00Z", row

            # 5. 멱등 — 다시 연결해도(한 번만 돈다), 본체를 직접 다시 돌려도 같다
            before = state(conn)
            conn.close()
            conn = connect()
            assert state(conn) == before, "두 번째 연결이 뭔가를 바꿨다"
            assert fix_keyword_locales(conn) == {"null": 0, "ko": 0, "rank": 0, "serp": 0}
            conn.commit()
            assert state(conn) == before, "본체를 두 번 돌리니 달라졌다"
            #    한 번만 돈다 — 사람이 일부러 ko-KR 로 되돌린 영어 키워드를 다음 연결이
            #    다시 뒤집지 않는다
            conn.execute("UPDATE keywords SET locale='ko-KR' WHERE keyword='milia'")
            conn.commit()
            conn.close()
            conn = connect()
            assert conn.execute("SELECT locale FROM keywords WHERE keyword='milia'"
                                ).fetchone()[0] == "ko-KR", "손으로 고친 로케일을 연결이 되돌렸다"

            # 6. 유일 제약 — keywords 의 유일 키에 locale 이 없어야 locale 을 바꿔도 안
            #    부딪힌다. 누가 (project_id, keyword, locale) 로 넓히면 같은 글자의
            #    ko/en 두 행이 생길 수 있고 fix_keyword_locales 가 합치기를 해야 한다.
            uniq = [[c["name"] for c in conn.execute(f"PRAGMA index_info('{ix['name']}')")]
                    for ix in conn.execute("PRAGMA index_list(keywords)") if ix["unique"]]
            assert uniq == [["project_id", "keyword"]], \
                f"keywords 유일 키가 바뀌었다 {uniq} — fix_keyword_locales 의 충돌 처리를 다시 봐라"
            #    옮긴 날과 같은 날 새 로케일로 다시 재도 (keyword_id, date) 유일 인덱스와
            #    부딪히지 않는다 — 옛 행은 이미 비교 표에 없다
            write_rank_snapshot(conn, kid["milia vs syringoma"], 2, None,
                                checked_at="2026-09-08T12:00:00Z")
            assert [tuple(r) for r in conn.execute(
                "SELECT position FROM rank_snapshots WHERE keyword_id=?",
                (kid["milia vs syringoma"],))] == [(2,)]

            # 7. 옮긴 표도 사이트에 딸린 표다 — 원격 병합이 함께 나른다
            import remote
            assert "offlocale_snapshots" in remote._plan(conn)[0]
            conn.close()
        finally:
            if old is None:
                os.environ.pop("CAPTURE_HOME", None)
            else:
                os.environ["CAPTURE_HOME"] = old

    # 8. keywords 에 넣는 **제품 코드**는 전부 판정을 거친다 — 검사·데모 픽스처는 뺀다.
    #    판정 없이 넣는 경로가 하나라도 생기면 언어 칸이 다시 비거나 사이트 로케일로 찬다.
    import ast
    import re
    ins = re.compile(r"INTO\s+keywords\s*\(", re.I)
    fixture = re.compile(r"^(test_|_selfcheck|_runner_check|_check|demo$)")
    root = Path(__file__).resolve().parents[3]
    bare = []
    for f in [*(root / "skills").rglob("*.py"), *(root / "server").rglob("*.py")]:
        if f.name.startswith("test_"):
            continue
        tree = ast.parse(f.read_text("utf-8"))
        fns = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
               and n in tree.body + [m for c in tree.body if isinstance(c, ast.ClassDef)
                                     for m in c.body]]
        for fn in fns:
            if fixture.match(fn.name):
                continue
            names = {n.id if isinstance(n, ast.Name) else getattr(n, "attr", None)
                     for n in ast.walk(fn)}
            if (any(isinstance(n, ast.Constant) and isinstance(n.value, str)
                    and ins.search(n.value) for n in ast.walk(fn))
                    and not names & {"keyword_judge", "keyword_locale"}):
                bare.append(f"{f.relative_to(root)}:{fn.name}")
    assert not bare, f"판정(db.keyword_locale) 없이 keywords 에 넣는 경로: {bare}"


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        sys.exit(__doc__)
    cmd = args[0]
    if cmd == "init":
        init_db()
    elif cmd == "selfcheck":
        _selfcheck()
    elif cmd == "sync-project" and len(args) > 1:
        sync_project(args[1])
    elif cmd == "stats" and len(args) > 1:
        stats(args[1])
    elif cmd == "sql" and len(args) > 1:
        # `sql "<쿼리>" [--project NAME]` — 이름이 원격이면 서버 brain 에 묻는다.
        # 플래그를 안 주면 여태와 똑같이 로컬 Brain 이다(원격 경로가 아예 안 열린다).
        rest = args[2:]
        proj = rest[rest.index("--project") + 1] if "--project" in rest[:-1] else ""
        import remote
        if proj and remote.owns(proj):
            # 가드(읽기 전용 커넥션 + SELECT/WITH)는 서버가 이 함수로 그대로 건다.
            res = remote.api("POST", "/api/sql", json={"project": proj, "sql": args[1]})
            print(res if isinstance(res, str)
                  else json.dumps(res, ensure_ascii=False, indent=2, default=str))
        else:
            run_sql(args[1])
    else:
        sys.exit(__doc__)
