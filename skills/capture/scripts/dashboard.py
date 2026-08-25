#!/usr/bin/env python3
"""로컬 대시보드 — Brain을 브라우저에서 실시간 조회·조작 (stdlib http.server).

화면은 하나다. 두 가지 모드로 쓴다:
  · 라이브(기본)  — 서버가 Brain을 그때그때 읽어준다. 기회 트리아지·설정이 된다.
  · 박제(--export) — 그 시점 데이터를 페이지 안에 박아 넣은 자립형 HTML 파일.
    서버도 인터넷도 필요 없고 남한테 보내도 그대로 열린다.
같은 템플릿을 쓰므로 화면이 갈라지지 않는다.
claude-mem 뷰어 구조를 참고하되, 상시 데몬·SSE·프런트 번들러는 들이지 않았다 —
데이터가 수집 스크립트 실행 시에만 바뀌므로 필요할 때 띄우는 단일 프로세스면 된다.
# ponytail: 새로고침 버튼 방식 — 수집을 상시 데몬화하면 그때 SSE 추가

Usage:
  python dashboard.py [--project NAME] [--port 8765] [--open]
  python dashboard.py --export --project NAME [--actions actions.json] [--open]
"""
import argparse
import json
import os
import re
import secrets
import subprocess
import sys
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).parent))
SETUP_SCRIPTS = Path(__file__).resolve().parents[2] / "setup" / "scripts"
sys.path.insert(0, str(SETUP_SCRIPTS))
import collector  # noqa: E402  (프로젝트 설정 읽기 — 수집기와 같은 경로로)
import db         # noqa: E402
import doctor     # noqa: E402  (setup 스킬의 진단 — 대시보드 상단 배너용)
import scoring    # noqa: E402  (판정 규칙 — 화면·박제본·산문이 같은 임계값을 본다)
import stage      # noqa: E402  (진행 상태 및 6단계 판정 정본)

TPL = Path(__file__).parent.parent / "templates"
VIEW_ORDER = ["overview", "keywords", "rank", "ai", "site", "history"]
_VIEW_DEF = re.compile(
    r'<script type="application/json" class="view-def">\s*(\{.*?\})\s*</script>', re.S)


def view_defs() -> list[dict]:
    """뷰가 자기 파일 안에서 선언한 것 — id·제목·담는 요소·그 화면을 채우는 단계.

    분할이 여태 두 번 구현돼 있었다: 여기 VIEW_ORDER 여섯 개와, 호스팅판
    dash.html 의 VIEWS 표(같은 여섯 이름 + 요소 id 열몇)다. 이제 선언은 화면
    자신에게 있고, 조립이 그걸 데이터로 방출한다(window.__VIEWS__).
    선언이 없거나 id 가 파일 이름과 어긋나면 조립을 멈춘다 — 조용히 빠지면
    읽는 쪽이 옛 목록으로 되돌아간다.
    """
    out = []
    for n in VIEW_ORDER:
        m = _VIEW_DEF.search((TPL / "views" / f"{n}.html").read_text("utf-8"))
        if not m:
            raise ValueError(f"{n}.html 에 view-def 선언이 없습니다")
        d = json.loads(m.group(1))
        if d.get("id") != n:
            raise ValueError(f"{n}.html 의 view-def id 가 {d.get('id')!r} 입니다")
        out.append(d)
    return out


def _assemble() -> bytes:
    """화면 조각을 한 장으로 잇는다 — 박제본(/capture report)은 서버 없이 열려야 한다."""
    base = (TPL / "dashboard.html").read_text("utf-8")
    parts = "".join((TPL / "views" / f"{n}.html").read_text("utf-8") for n in VIEW_ORDER)
    defs = json.dumps(view_defs(), ensure_ascii=False).replace("</", "<\\/")
    return (base
            .replace("<!--MANIFEST-->", f"<script>window.__VIEWS__={defs};</script>", 1)
            .replace("<!--VIEWS-->", parts)
            .encode("utf-8"))


HTML = _assemble()

# 구글 로그인 창을 여는 건 collect_gsc.get_service() 다 — 수집기·즉석 조회와 같은
# 경로를 그대로 빌린다. 덕분에 사이트를 하나도 등록하지 않은 사람도 로그인만 먼저
# 끝낼 수 있다. 판정은 여기서 하지 않는다: 성공 여부는 db.gsc_connected()가 답한다.
_LOGIN_PY = (
    "import sys; sys.path.insert(0, r'" + str(Path(__file__).resolve().parent) + "')\n"
    "import db, collect_gsc\n"
    "collect_gsc.get_service()\n"
    "ok = db.gsc_connected()\n"
    "print(('로그인 완료 — 토큰을 보관했습니다: ' + str(db.gsc_token())) if ok else\n"
    "      '로그인이 끝나지 않았습니다 — 열린 브라우저 창에서 구글 계정으로 "
    "로그인해 주세요.')\n"
    "sys.exit(0 if ok else 1)\n"
)

# 설정 화면이 실행할 수 있는 명령은 이 넷뿐 — 사용자 입력이 명령줄에 섞이지 않는다.
ACTIONS = {
    "deps": [sys.executable, "-m", "pip", "install", "requests", "pyyaml"],
    # google-auth-oauthlib 이 여기 있는 이유: **브라우저 로그인 창을 여는 게 그것이다.**
    # 조회만 하는 google-api-python-client 와 달리 없으면 로그인 자리에서 막힌다
    # (서비스 계정 갈래만 쓰는 사람에게는 필요 없지만, 기본은 OAuth 다).
    "deps_gsc": [sys.executable, "-m", "pip", "install", "google-api-python-client",
                 "google-auth", "google-auth-oauthlib"],
    "gsc": [sys.executable, str(SETUP_SCRIPTS / "connect_gsc.py")],
    "gsc_login": [sys.executable, "-c", _LOGIN_PY],
    # 빠진 마케팅 스킬(setup 스킬이 띄우는 doctor의 [꼭 해야 할 일])을 설치해 준다.
    # 동의는 화면(또는 채팅)이 미리 받고, 스크립트는 빈 입력을 명령줄에 섞지 않는다.
    "skills": [sys.executable, str(SETUP_SCRIPTS / "install_skills.py")],
}
KEY_FIELDS = ("OPENROUTER_API_KEY", "SERPER_API_KEY",
              "DATAFORSEO_LOGIN", "DATAFORSEO_PASSWORD")
PROJECT_TYPES = ("game", "local_clinic", "saas", "directory")
ENV_FILE = db.CAPTURE_HOME / "env"

# 로컬 전용이라 인증이 없다. 그런데 브라우저는 아무 웹페이지에서나 127.0.0.1로 POST를
# 보낼 수 있어서(pip 실행·파일 쓰기 엔드포인트가 생겼으므로) 1회용 토큰으로 막는다.
TOKEN = secrets.token_urlsafe(9)


def export(project: str, actions_file: str | None = None) -> Path:
    """그 시점 데이터를 페이지에 박아 넣은 자립형 HTML을 reports/에 남긴다.
    라이브 화면과 같은 템플릿이다 — 페이지가 window.__SNAPSHOT__을 보면 fetch 대신
    그걸 그리고, 손댈 수 없는 기록이므로 트리아지·설정 버튼을 숨긴다."""
    data = payload(project)
    actions = []
    if actions_file and Path(actions_file).exists():
        actions = json.loads(Path(actions_file).read_text(encoding="utf-8"))
    snap = {"data": data, "actions": actions, "exported": str(date.today())}
    blob = json.dumps(snap, ensure_ascii=False).replace("</", "<\\/")  # </script> 차단
    html = HTML.decode("utf-8").replace(
        "<!--SNAPSHOT-->", f"<script>window.__SNAPSHOT__={blob}</script>", 1)
    out = db.CAPTURE_HOME / "reports" / project / f"{date.today()}.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    return out


def run_action(name: str) -> dict:
    try:
        p = subprocess.run(ACTIONS[name], capture_output=True, timeout=600,
                           encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return {"ok": False, "log": "10분이 지나도 안 끝나 중단했습니다."}
    return {"ok": p.returncode == 0, "log": ((p.stdout or "") + (p.stderr or ""))[-4000:]}


def save_keys(values: dict) -> dict:
    """셸 rc 편집 대신 ~/.capture/env 에 모은다 (모든 스크립트가 db.load_env로 읽는다).
    빈 값으로 보내면 그 키를 지운다."""
    cur = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text("utf-8").splitlines():
            k, sep, v = line.partition("=")
            if sep:
                cur[k.strip()] = v.strip()
    for k in KEY_FIELDS:
        if k not in values:
            continue
        v = str(values[k]).strip()
        if v:
            cur[k] = os.environ[k] = v   # 지금 세션의 doctor 진단에 바로 반영
        else:
            cur.pop(k, None)
            os.environ.pop(k, None)
    ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
    ENV_FILE.write_text("".join(f"{k}={v}\n" for k, v in cur.items() if v), "utf-8")
    try:
        ENV_FILE.chmod(0o600)
    except OSError:                      # 윈도우 등 — 권한 모델이 달라도 저장은 성공
        pass
    return {"ok": True, "saved": [k for k in KEY_FIELDS if cur.get(k)],
            "path": str(ENV_FILE)}


def save_gsc_client(f: dict) -> dict:
    """자기 OAuth 클라이언트 조립 — connect_gsc.py --client-id/--client-secret 과 같은 결과.

    **서브프로세스로 넘기지 않는다.** 시크릿이 명령줄에 실리면 프로세스 목록에 그대로
    뜬다(ACTIONS 가 고정 명령만 두는 것도 같은 이유다). 조립 규칙·설치 자리는
    connect_gsc 가 정본이라 그 함수를 그대로 부른다 — 여기 사본을 만들면 번들 형태가
    바뀌는 날 한쪽만 고쳐진다.
    """
    cid = str(f.get("client_id", "")).strip()
    sec = str(f.get("client_secret", "")).strip()
    if not (cid and sec):
        return {"ok": False, "error": "client_id 와 client_secret 을 둘 다 넣어 주세요 "
                                      "(하나만으로는 클라이언트가 성립하지 않습니다)."}
    import connect_gsc          # setup 스킬 쪽 — db 말고는 아무것도 안 물고 온다
    dest = connect_gsc.dest_for("oauth")
    dest.parent.mkdir(parents=True, exist_ok=True)
    # --force 를 안 묻는다: 폼에 두 값을 적고 [저장]을 누른 것 자체가 교체 의사다.
    dest.write_text(json.dumps(connect_gsc.assemble(cid, sec), indent=2), "utf-8")
    try:
        dest.chmod(0o600)
    except OSError:              # 윈도우 등 — 권한 모델이 달라도 저장은 성공
        pass
    # 시크릿은 로그에도 응답에도 넣지 않는다. client_id 도 확인용 앞부분만
    # (connect_gsc 의 CLI 출력과 같은 규칙).
    return {"ok": True, "path": str(dest), "client_id": cid[:12] + "…"}


def create_project(f: dict) -> dict:
    """폼 입력 → projects/{name}.yaml → Brain 등록. AI 프롬프트 초안은 채팅(/capture add) 몫."""
    name = str(f.get("name", "")).strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,39}", name):
        return {"ok": False, "error": "이름은 영문·숫자·-·_ 로 40자까지 (파일명이 됩니다)"}
    if f.get("type") not in PROJECT_TYPES:
        return {"ok": False, "error": f"종류는 {'/'.join(PROJECT_TYPES)} 중 하나"}
    domain = str(f.get("domain", "")).strip()
    if not domain:
        return {"ok": False, "error": "도메인을 입력해 주세요 (예: example.com)"}
    path = db.CAPTURE_HOME / "projects" / f"{name}.yaml"
    if path.exists():
        return {"ok": False, "error": f"{name} 은 이미 있습니다 — 다른 이름을 쓰세요."}

    def items(key: str) -> list[str]:    # 줄바꿈·쉼표 아무렇게나 적어도 받는다
        return [s.strip() for s in re.split(r"[,\n]", str(f.get(key, ""))) if s.strip()]

    # 폼 입력을 f-string으로 YAML에 끼우면 개행 하나로 키가 주입된다 — dump가 막는다.
    try:
        import yaml
    except ImportError:
        return {"ok": False, "error": "기본 부품(pyyaml)이 아직 없습니다 — "
                                      "위의 [기본 부품 설치]를 먼저 눌러 주세요."}
    gsc = str(f.get("gsc_property", "")).strip() or f"sc-domain:{domain}"
    doc = {"name": name, "type": f["type"], "domain": domain,
           "locale": str(f.get("locale") or "ko-KR").strip(),
           "gsc_property": gsc,
           "brand_aliases": items("brand_aliases"),
           "seed_keywords": items("seed_keywords"),
           "competitors_manual": items("competitors_manual"),
           "tools": items("tools"),
           "surfaces_ai": ["chatgpt", "perplexity", "gemini"],
           "limits": {"max_keywords": 100, "max_ai_prompts": 30}}

    preset_path = Path(__file__).resolve().parent.parent / "projects" / "_presets.yaml"
    if preset_path.exists():
        try:
            presets = yaml.safe_load(preset_path.read_text("utf-8")) or {}
            if isinstance(presets, dict) and f.get("type") in presets:
                p_data = presets[f["type"]]
                if isinstance(p_data, dict) and p_data:
                    doc["preset"] = p_data
        except Exception:
            pass

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"# 대시보드 설정 화면에서 생성 — 손으로 고친 뒤에는\n"
        f"# python db.py sync-project {path} 를 다시 돌리면 반영됩니다.\n"
        + yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), "utf-8")
    try:
        db.sync_project(str(path))
    except db.ProjectConfigNotFound as e:
        return {"ok": False, "error": str(e)}
    except ImportError:
        return {"ok": False, "error": "기본 부품(pyyaml)이 아직 없습니다 — "
                                      "위의 [기본 부품 설치]를 먼저 눌러 주세요."}
    PREFILL_FILE.unlink(missing_ok=True)   # 다 썼다 — 다음 사이트 폼에 새면 오염이다
    return {"ok": True, "name": name, "path": str(path)}


# Claude(setup 스킬)가 레포를 읽고 판단해 둔 프리필 — 씨앗 키워드·브랜드 별칭처럼
# 코드로 못 뽑는 것들이 여기로 온다. 등록이 성공하면 지운다(다음 사이트 오염 방지).
PREFILL_FILE = db.CAPTURE_HOME / "prefill.json"
PREFILL_KEYS = ("name", "domain", "gsc_property", "locale", "type",
                "seed_keywords", "brand_aliases", "tools", "competitors_manual")


def repo_prefill(root: Path | None = None) -> dict:
    """대시보드를 띄운 폴더(레포)에서 첫 사이트 폼의 초기값을 추론한다.

    두 층의 합: ① 결정적 추론(코드) — CNAME > package.json(homepage) >
    astro.config(site) > git remote(*.github.io)에서 이름·도메인.
    ② Claude 판단(PREFILL_FILE) — setup 스킬이 레포 콘텐츠를 읽고 써 둔
    씨앗 키워드·브랜드 별칭·tools 등. 확실한 실측(CNAME)은 판단을 이기고,
    약한 신호(homepage·config)는 판단이 비운 칸만 채운다. 못 찾으면 그 키를 비운다.
    """
    root = root or Path.cwd()
    out: dict = {}
    if PREFILL_FILE.exists():
        try:
            j = json.loads(PREFILL_FILE.read_text("utf-8"))
            out = {k: j[k] for k in PREFILL_KEYS if j.get(k)}
        except (ValueError, OSError):
            pass

    def _domain(url: str) -> str:
        d = re.sub(r"^https?://", "", url.strip()).split("/")[0].strip().lower()
        return d if "." in d else ""

    cname = root / "CNAME"
    if cname.is_file():
        d = _domain(cname.read_text("utf-8").strip())
        if d:
            out["domain"] = d               # 실측이 판단 프리필을 이긴다
    pkg = root / "package.json"
    if pkg.is_file():
        try:
            p = json.loads(pkg.read_text("utf-8"))
            if not out.get("domain") and p.get("homepage"):
                out["domain"] = _domain(str(p["homepage"]))
            if p.get("name"):
                out.setdefault("name", str(p["name"]).split("/")[-1])
        except ValueError:
            pass
    if not out.get("domain"):
        for cfg in ("astro.config.mjs", "astro.config.ts", "astro.config.js"):
            f = root / cfg
            if f.is_file():
                m = re.search(r"""site\s*:\s*['"](https?://[^'"]+)['"]""",
                              f.read_text("utf-8", errors="replace"))
                if m:
                    out["domain"] = _domain(m.group(1))
                break
    if not out.get("domain"):
        gitcfg = root / ".git" / "config"
        if gitcfg.is_file():
            m = re.search(r"github\.com[:/][\w.-]+/([\w.-]+?)(?:\.git)?\s*$",
                          gitcfg.read_text("utf-8", errors="replace"), re.M)
            if m and m.group(1).endswith(".github.io"):
                out["domain"] = m.group(1).lower()
    name = out.get("name") or root.name
    name = re.sub(r"[^A-Za-z0-9_-]", "-", name).strip("-")[:40]
    if name and re.match(r"[A-Za-z0-9]", name):
        out["name"] = name
    if out.get("domain"):
        out["gsc_property"] = f"sc-domain:{out['domain']}"
    return out


def setup_state(project: str = "") -> dict:
    """setup 스킬의 doctor.diagnose()를 소비해 대시보드 화면이 실제로 쓰는 평평한 키만 방출.

    project: 화면이 보고 있는 사이트 — 안내(guide)가 그 사이트를 따라가게 한다.
    """
    return stage.setup_payload(project=project)


def q(conn, sql, args=()):
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def gather(conn, p) -> dict:
    """화면 하나가 쓰는 데이터 전부 — 라이브 대시보드와 박제 리포트가 같이 쓴다."""
    pid = p["id"]
    cur, prev, period, period_mismatch = scoring.snapshot_pair(conn, pid)

    # 집계는 scoring._snap_agg 하나로 — 여기 사본이 있었는데 period_days 조건이
    # 빠져 있어서, 한 날짜에 28일치와 90일치가 같이 있으면 화면의 '움직인 검색어'만
    # 두 기간을 섞어 집계했다 (scoring.md 4-3b 를 어긴다). 판정 함수와 같은 걸 쓴다.
    def gsc_agg(snap):
        return scoring._snap_agg(conn, pid, snap, period) if snap else {}

    ups, downs = scoring.movers(gsc_agg(cur), gsc_agg(prev))

    # 남의 브랜드 카탈로그는 yaml에 있다. Brain에는 등록됐는데 yaml을 지운
    # 프로젝트도 화면은 떠야 한다 — project_cfg가 경고만 하고 빈 설정을 준다.
    cfg = collector.project_cfg(p["config_path"] or p["name"])
    brands = scoring.foreign_brands(conn, pid, cfg)
    striking = scoring.striking(conn, pid, cur, brands=brands)

    # 일별 추이·기기 격차·색인 점검. 판정 함수가 빈 목록을 주는 경우가 두 가지라
    # (아직 안 수집 / 수집했는데 문제 없음) 최신 수집일도 같이 내려보낸다 —
    # 화면이 그 둘을 구분해서 안내 문구를 갈라 쓴다.
    device_date = scoring._latest(conn, scoring._LATEST_BD, (pid, "device"))
    index_date = scoring._latest(conn, scoring._LATEST_IX, (pid,))

    ai_run = conn.execute(
        "SELECT id, started_at FROM runs WHERE project_id=? AND kind='ai' "
        "ORDER BY id DESC LIMIT 1", (pid,)).fetchone()
    matrix, gap_domains, missed = [], [], []
    cite_share, ai_by_prompt = [], []
    if ai_run:
        matrix = q(conn,
            """SELECT c.engine, p2.category, SUM(c.cited) cited,
                      SUM(c.mentioned) mentioned, COUNT(*) total
                 FROM ai_checks c JOIN ai_prompts p2 ON p2.id=c.prompt_id
                WHERE c.run_id=? GROUP BY 1,2 ORDER BY 1,2""", (ai_run["id"],))
        freq: dict[str, int] = {}
        for r in q(conn, "SELECT cited_domains_json FROM ai_checks WHERE run_id=? AND cited=0",
                   (ai_run["id"],)):
            for d in json.loads(r["cited_domains_json"] or "[]"):
                freq[d] = freq.get(d, 0) + 1
        gap_domains = sorted(({"domain": k, "n": v} for k, v in freq.items()),
                             key=lambda x: -x["n"])[:15]

        # 점유는 우리가 빠진 답변만 세면 과소집계다 — 같이 인용된 경우도 세야
        # 우리 도메인과 나란히 놓을 수 있다. gap_domains 는 그대로 둔다(하위호환).
        share: dict[str, int] = {}
        for r in q(conn, "SELECT cited_domains_json FROM ai_checks WHERE run_id=?",
                   (ai_run["id"],)):
            for d in json.loads(r["cited_domains_json"] or "[]"):
                share[d] = share.get(d, 0) + 1
        cite_share = sorted(({"domain": k, "n": v} for k, v in share.items()),
                            key=lambda x: -x["n"])[:15]

        # 질문 하나 = 줄 하나. "왜 빠졌나"의 증거는 답변 원문뿐인데 여태 버려졌다.
        # 발췌는 길다 — 박제본에도 들어가므로 화면이 접어 보여줄 만큼만 자른다.
        ai_by_prompt = q(conn,
            """SELECT p2.id, p2.prompt, p2.category,
                      SUM(c.cited) cited, SUM(c.mentioned) mentioned, COUNT(*) checks,
                      GROUP_CONCAT(DISTINCT c.engine) engines,
                      MAX(CASE WHEN c.cited=0 THEN c.answer_excerpt END) miss_answer,
                      MAX(CASE WHEN c.cited=0 THEN c.cited_domains_json END) miss_domains
                 FROM ai_checks c JOIN ai_prompts p2 ON p2.id=c.prompt_id
                WHERE c.run_id=? GROUP BY p2.id
                ORDER BY cited DESC, mentioned DESC""", (ai_run["id"],))
        for r in ai_by_prompt:
            r["miss_answer"] = (r["miss_answer"] or "")[:600]
        missed = q(conn,
            """SELECT p2.prompt, p2.category,
                      GROUP_CONCAT(DISTINCT c.engine) engines
                 FROM ai_checks c JOIN ai_prompts p2 ON p2.id=c.prompt_id
                WHERE c.run_id=? AND c.cited=0 AND c.mentioned=0
                GROUP BY p2.id ORDER BY p2.category LIMIT 20""", (ai_run["id"],))

    # 런별 인용률 추이 — 최신 런 1건(matrix)만 보면 개선/악화가 안 보인다.
    ai_trend = q(conn,
        """SELECT COALESCE(r.finished_at, r.started_at) t,
                  SUM(c.cited) cited, SUM(c.mentioned) mentioned, COUNT(*) checks
             FROM runs r JOIN ai_checks c ON c.run_id=r.id
            WHERE r.project_id=? AND r.kind='ai'
            GROUP BY r.id ORDER BY r.id""", (pid,))
    for r in ai_trend:
        r["cite_rate"] = round(100 * r["cited"] / r["checks"], 1)
        r["mention_rate"] = round(100 * r["mentioned"] / r["checks"], 1)

    rank_dates = [r[0] for r in conn.execute(
        """SELECT DISTINCT substr(rs.checked_at,1,10) d FROM rank_snapshots rs
             JOIN keywords k ON k.id=rs.keyword_id
            WHERE k.project_id=? ORDER BY d DESC LIMIT 2""", (pid,))]

    def rank_agg(d):
        if not d:
            return {}
        return {r["keyword"]: r for r in q(conn,
            """SELECT k.keyword, rs.position, rs.aio_present, rs.aio_cited
                 FROM rank_snapshots rs JOIN keywords k ON k.id=rs.keyword_id
                WHERE k.project_id=? AND substr(rs.checked_at,1,10)=?""", (pid, d))}

    r_cur = rank_agg(rank_dates[0] if rank_dates else None)
    r_prev = rank_agg(rank_dates[1] if len(rank_dates) > 1 else None)
    ranks, aio_gap = [], []
    for kw, r in r_cur.items():
        prev_r = r_prev.get(kw)
        prev_pos = prev_r["position"] if prev_r else None
        cur_pos = r["position"]
        rd = scoring.rank_delta(prev_pos, cur_pos)
        ranks.append({"keyword": kw, "pos": cur_pos, "dpos": rd["delta"],
                      "delta": rd,
                      "aio": r["aio_present"], "aio_cited": r["aio_cited"]})
        if r["aio_present"] == 1 and r["aio_cited"] == 0:
            aio_gap.append(kw)
    ranks.sort(key=lambda x: (x["pos"] is None, x["pos"] or 999))

    opps = scoring.opportunities(conn, pid, limit=200, with_id=True)
    for o in opps:
        o["is_defensive"] = scoring.is_defensive(o["kind"])
    opp_counts = {}
    for o in opps:
        opp_counts[o["kind"]] = opp_counts.get(o["kind"], 0) + 1
    opps_total = conn.execute(
        "SELECT COUNT(*) FROM opportunities WHERE project_id=? AND status='new'",
        (pid,)).fetchone()[0]
    runs = q(conn,
        """SELECT id, kind, started_at, finished_at, api_calls, cost_estimate_usd, notes
             FROM runs WHERE project_id=? ORDER BY id DESC LIMIT 40""", (pid,))
    # /create 가 실제로 고친 것 — 측정→기회→수정→재측정 루프가 닫혔음을 보여주는 자리.
    creations = q(conn,
        """SELECT c.id, c.kind, c.file_path, c.branch, c.note, c.merged,
                  c.created_at, c.opportunity_id, o.target opp_target
             FROM creations c LEFT JOIN opportunities o ON o.id=c.opportunity_id
            WHERE c.project_id=? ORDER BY c.id DESC LIMIT 50""", (pid,))
    prog = stage.progress(conn, pid)
    guide = stage.state(conn, p, p["domain"] or "")
    daily = scoring.daily_trend(conn, pid)
    daily_stats = {
        "clicks_sum": sum(r["clicks"] or 0 for r in daily),
        "impressions_sum": sum(r["impressions"] or 0 for r in daily),
        "clicks_max": max([r["clicks"] or 0 for r in daily] + [1]),
        "impressions_max": max([r["impressions"] or 0 for r in daily] + [1]),
    }
    striking_page2 = sum(1 for r in striking if r.get("band") == "page2")

    # 박제본 호환 분기는 이 번호 하나로 한다 — 필드 유무를 검사하지 않는다
    return {"schema": 1,
            "project": dict(p), "gsc_date": cur, "gsc_prev": prev, "gsc_period": period,
            "period_mismatch": period_mismatch, "ups": ups, "downs": downs,
            "striking": striking, "striking_page2": striking_page2,
            "brand_catalog_empty": len(brands) == 0,
            "matrix": matrix, "gap_domains": gap_domains,
            "cite_share": cite_share, "ai_by_prompt": ai_by_prompt,
            "missed": missed, "ai_trend": ai_trend,
            "opps": opps, "opps_total": opps_total, "opp_counts": opp_counts,
            "runs": runs, "creations": creations,
            "rank_date": rank_dates[0] if rank_dates else None,
            "rank_prev": rank_dates[1] if len(rank_dates) > 1 else None,
            "ranks": ranks[:30], "aio_gap": aio_gap,
            "kw_active": conn.execute(
                "SELECT COUNT(*) FROM keywords WHERE project_id=? AND is_active=1",
                (pid,)).fetchone()[0],
            "daily": daily, "daily_stats": daily_stats,
            "device_gap": scoring.device_gap(conn, pid),
            "index_issues": scoring.index_issues(conn, pid),
            "device_date": device_date, "index_date": index_date,
            "rules": {"page1": scoring.PAGE1, "striking_lo": scoring.STRIKING_LO,
                      "striking_hi": scoring.STRIKING_HI,
                      "rank_noise": scoring.RANK_NOISE,
                      "device_gap_pos": scoring.DEVICE_GAP_POS,
                      "device_min_imp": scoring.DEVICE_MIN_IMP},
            # KPI 추이도 비교 짝과 같은 period_days만 — 28일치 사이에 90일치가 끼면
            # 그래프·Δ가 전부 거짓이 된다. SQL이 이미 period_days로 걸렀으므로 p 필드는 뺀다.
            "trend": [dict(r) for r in conn.execute(
                """SELECT snapshot_date d, SUM(clicks) clk,
                          SUM(impressions) imp, COUNT(DISTINCT query) q
                     FROM gsc_snapshots WHERE project_id=? AND period_days=?
                    GROUP BY 1 ORDER BY 1""", (pid, period))],
            "progress": prog,
            "guide": guide}


def payload(project: str) -> dict:
    conn = db.connect()
    try:
        p = db.get_project(conn, project)
        return gather(conn, p)
    finally:
        conn.close()


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes,
              ctype: str = "application/json; charset=utf-8") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def do_GET(self) -> None:  # noqa: N802 (http.server 규약)
        u = urlparse(self.path)
        if u.path == "/":
            return self._send(200, HTML, "text/html; charset=utf-8")
        if u.path == "/api/doctor":
            return self._json(setup_state(parse_qs(u.query).get("project", [""])[0]))
        if u.path == "/api/setup/prefill":   # 읽기 전용 — 레포 추론값
            return self._json(repo_prefill())
        if u.path == "/api/projects":
            conn = db.connect()
            names = [r[0] for r in
                     conn.execute("SELECT name FROM projects ORDER BY name")]
            conn.close()
            return self._json(names)
        if u.path == "/api/data":
            name = parse_qs(u.query).get("project", [""])[0]
            try:
                return self._json(payload(name))
            except db.ProjectNotFound as e:  # db.get_project는 미등록이면 ProjectNotFound
                return self._json({"error": str(e)}, 404)
        return self._send(404, b"not found", "text/plain")

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path not in ("/api/opp", "/api/setup/run", "/api/setup/keys",
                        "/api/setup/project", "/api/setup/gsc-client"):
            return self._send(404, b"not found", "text/plain")
        if self.headers.get("X-Token") != TOKEN:
            return self._json({"error": "이 창은 만료됐습니다 — 대시보드를 다시 띄워 주세요."},
                              403)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return self._json({"error": "bad json"}, 400)

        if path == "/api/setup/run":
            if body.get("action") not in ACTIONS:
                return self._json({"error": "unknown action"}, 400)
            return self._json(run_action(body["action"]))
        if path == "/api/setup/keys":
            return self._json(save_keys(body))
        if path == "/api/setup/gsc-client":
            r = save_gsc_client(body)
            return self._json(r, 200 if r["ok"] else 400)
        if path == "/api/setup/project":
            r = create_project(body)
            return self._json(r, 200 if r["ok"] else 400)

        if body.get("status") not in db.OPP_STATUSES:
            return self._json({"error": f"status must be one of {db.OPP_STATUSES}"}, 400)
        conn = db.connect()
        updated = db.set_opportunity_status(conn, int(body.get("id") or 0), body["status"])
        conn.close()
        return self._json({"updated": updated})

    def log_message(self, fmt, *args) -> None:  # 요청 로그 소음 제거
        pass


def _selfcheck() -> None:
    """조립이 지켜야 할 것 — 브라우저를 안 띄우고 확인할 수 있는 만큼만."""
    html = _assemble().decode("utf-8")
    defs = view_defs()
    assert [d["id"] for d in defs] == VIEW_ORDER, "선언 순서가 조립 순서와 다르다"
    assert "window.__VIEWS__=" in html, "매니페스트가 안 실렸다"
    for left in ("<!--MANIFEST-->", "<!--VIEWS-->"):
        assert left not in html, f"자리표가 안 채워졌다: {left}"
    for d in defs:
        assert f'VIEW("{d["id"]}"' in html, f'{d["id"]} 가 VIEW() 로 등록되지 않았다'
        assert d["title"] and isinstance(d["stages"], list)
        for i in d["sections"]:                  # 담는다고 선언한 요소는 실제로 있어야
            assert f'id="{i}"' in html, f'{d["id"]} 가 없는 요소 id 를 담는다: {i}'

    # 접두사 관례(KW_·RK_…) 대신 기계가 센다 — 조립하면 한 문서라 최상위 이름이
    # 겹치면 뒤가 앞을 조용히 덮는다. 실제로 두 파일이 관례를 어기고 있었다.
    seen: dict[str, str] = {}
    srcs = [((TPL / "dashboard.html").read_text("utf-8"), "dashboard.html")]
    srcs += [((TPL / "views" / f"{n}.html").read_text("utf-8"), n) for n in VIEW_ORDER]
    for src, who in srcs:
        for _, name in re.findall(r"^(const|let|var|function)\s+([A-Za-z_$][\w$]*)",
                                  src, re.M):
            assert name not in seen, f"최상위 이름이 겹친다: {name} ({seen[name]} ↔ {who})"
            seen[name] = who

    # CSV 작성기는 한 벌뿐이다 — 뷰가 자기 사본을 다시 만들면 null 처리가 또 갈라진다
    # (여덟 벌이던 시절 일곱 벌이 빈 칸을 literal "null" 로 내보냈다).
    assert html.count("URL.createObjectURL") == 1, "CSV 작성기가 여러 벌이다"
    assert "String(c ?? \"\")" in html, "CSV 가 빈 칸을 빈 칸으로 안 쓴다"
    print(f"dashboard self-check ok — 뷰 {len(defs)}개, 최상위 이름 {len(seen)}개")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", help="시작 시 선택할 사이트 (생략하면 첫 번째)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--open", action="store_true")
    ap.add_argument("--export", action="store_true",
                    help="서버를 띄우는 대신 그 시점 화면을 HTML 파일로 남긴다")
    ap.add_argument("--actions", help="--export 전용: Next Actions JSON 파일")
    ap.add_argument("--selfcheck", action="store_true",
                    help="조립 결과만 점검하고 끝낸다 (서버·Brain 필요 없음)")
    a = ap.parse_args()

    if a.selfcheck:
        return _selfcheck()

    if a.export:
        if not a.project:
            sys.exit("--export 에는 --project 가 필요합니다.")
        out = export(a.project, a.actions)
        print(f"report: {out}")
        if a.open:
            import webbrowser
            webbrowser.open(out.as_uri())
        return

    # 외부 노출 금지 — 로컬 전용이라 인증이 없다. 바인딩으로 막는다.
    # allow_reuse_address 기본값(True)이면 Windows에서 같은 포트에 서버가 겹쳐
    # 떠서 구버전이 응답하는 사고가 난다 — 겹침 금지하고, 점유면 다음 포트로.
    # 레포마다 대시보드를 따로 띄우는 사용(멀티 프로젝트)도 이걸로 자연히 된다.
    ThreadingHTTPServer.allow_reuse_address = False
    srv = None
    for port in range(a.port, a.port + 20):
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            continue
    if srv is None:
        sys.exit(f"포트 {a.port}~{a.port + 19}가 모두 사용 중입니다 — "
                 f"기존 대시보드 창을 쓰거나 종료한 뒤 다시 실행하세요.")
    if port != a.port:
        print(f"포트 {a.port}는 이미 사용 중 — {port}로 띄웁니다.")
    url = f"http://127.0.0.1:{port}/?t={TOKEN}" + (
        f"#{a.project}" if a.project else "")
    print(f"dashboard: {url}  (Ctrl+C로 종료)")
    if a.open:
        import webbrowser
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
