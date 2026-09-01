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
import base64
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
import remote     # noqa: E402  (원격 사이트면 박제·화면을 서버가 낸다)
import scoring    # noqa: E402  (판정 규칙 — 화면·박제본·산문이 같은 임계값을 본다)
import stage      # noqa: E402  (진행 상태 및 6단계 판정 정본)

TPL = Path(__file__).parent.parent / "templates"
# 화면 순서 = 메뉴 순서. [안내]·[설정]은 여태 헤더 토글이 본문 위에 얹던 패널이었다
# — 화면으로 세우면 열렸나 닫혔나 하는 상태가 없어지고, 설정이 데이터 위에 오지 않는다.
VIEW_ORDER = ["overview", "analysis", "keywords", "rank", "ai", "site",
              "backlinks", "competitors", "history", "guide", "settings"]
_VIEW_DEF = re.compile(
    r'<script type="application/json" class="view-def">\s*(\{.*?\})\s*</script>', re.S)
_SECTION_DEF = re.compile(
    r'<script type="application/json" class="section-def">\s*(\{.*?\})\s*</script>\s*(.*)', re.S)
SECTIONS = TPL / "sections"


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


def section_defs() -> list[dict]:
    """배포별로만 붙는 섹션 — 원본 뷰에는 없는 정적 마크업을 여기서 선언한다.

    예전에는 이 마크업이 호스팅 애드온(dash.html) 안에서 createElement/innerHTML로
    런타임에 지어졌다. 그러면 조립이 검사할 수 없는 층(JS 문자열)에 구조가 숨는다 —
    view-def 와 같은 자리(templates/)에 같은 모양(id·소속 뷰·붙일 자리)으로 둔다.
    only: "hosted" 처럼 붙는 배포를 적으면 그 variant 조립에만 낀다 — 생략하면
    전부에 낀다.
    """
    out = []
    for p in sorted(SECTIONS.glob("*.html")) if SECTIONS.is_dir() else []:
        m = _SECTION_DEF.match(p.read_text("utf-8"))
        if not m:
            raise ValueError(f"{p.name} 에 section-def 선언이 없습니다")
        d = json.loads(m.group(1))
        if d.get("id") != p.stem:
            raise ValueError(f"{p.name} 의 section-def id 가 {d.get('id')!r} 입니다")
        d["html"] = m.group(2)
        out.append(d)
    return out


def _assemble(variant: str = "local") -> bytes:
    """화면 조각을 한 장으로 잇는다 — 박제본(/capture report)은 서버 없이 열려야 한다.

    variant: "local"(플러그인) | "hosted"(server/app.py 의 /d) | "frozen"(박제본).
    hosted 전용 섹션(templates/sections/*.html, only:"hosted")은 hosted 조립에만
    낀다 — 자리는 그 섹션이 선언한 view/after 로 정한다(원본 뷰 순서 안에 끼운다).
    """
    base = (TPL / "dashboard.html").read_text("utf-8")
    parts = "".join((TPL / "views" / f"{n}.html").read_text("utf-8") for n in VIEW_ORDER)
    defs = view_defs()
    by_id = {d["id"]: d for d in defs}
    secs = [s for s in section_defs() if s.get("only") in (None, variant)]
    # 자리(after)가 다른 섹션일 수 있다(sm-dim ← sm-perf) — 파일 이름 순서로 끼우면
    # 그 짝이 아직 안 꽂힌 채로 올 수 있다. 꽂을 수 있는 것부터 반복해서 끼운다.
    pending = list(secs)
    while pending:
        i0 = len(pending)
        for s in list(pending):
            v = by_id[s["view"]]
            if s["after"] in v["sections"]:
                i = v["sections"].index(s["after"])
                v["sections"].insert(i + 1, s["id"])
                pending.remove(s)
        if len(pending) == i0:
            raise ValueError(f"섹션을 끼울 자리를 못 찾았다: {[s['id'] for s in pending]}")
    views_json = json.dumps(defs, ensure_ascii=False).replace("</", "<\\/")
    stages_json = json.dumps(stage.STAGE_LABELS, ensure_ascii=False).replace("</", "<\\/")
    hosted_flag = "window.SM_HOSTED=true;" if variant == "hosted" else ""
    manifest = (f"<script>{hosted_flag}window.__VIEWS__={views_json};"
                f"window.__STAGES__={stages_json};</script>")
    return (base
            .replace("<!--MANIFEST-->", manifest, 1)
            .replace("<!--VIEWS-->", parts)
            .replace("<!--SECTIONS-->", "".join(s["html"] for s in secs), 1)
            .encode("utf-8"))


def assemble(variant: str = "local") -> str:
    return _assemble(variant).decode("utf-8")


HTML = _assemble()   # local — assemble("local").encode("utf-8") 과 같다

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
    html = assemble("frozen").replace(
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


# 호스팅(웹) 주소 — 로컬에서 만든 "웹에서 이어 하기" 링크가 가리키는 곳.
# 배포 주소가 바뀌면 여기 한 줄만 고친다(env 로도 덮는다).
HOSTED_URL = os.environ.get("SEOMINER_HOSTED_URL",
                            "https://seo-miner.up.railway.app")


def carry_pack(project: str) -> dict:
    """로컬 사이트 설정을 호스팅 등록 폼으로 실어 보내는 링크.

    싣는 키의 정본은 PREFILL_KEYS 다 — 로컬 폼이 채우는 것, 호스팅이 등록에 쓰는 것,
    여기 싣는 것이 전부 같은 이름이어야 한다. 데이터는 안 옮긴다(웹이 다시 잰다):
    옮기는 건 사람이 정한 것뿐 — 도메인·종류·씨앗·브랜드·경쟁사.

    리스트는 문자열로 접는다. 받는 쪽(create_project.items)이 쉼표·줄바꿈을 쪼갠다.
    """
    path = db.CAPTURE_HOME / "projects" / f"{project}.yaml"
    if not path.exists():
        return {"ok": False, "error": f"{project}.yaml 을 못 찾았습니다"}
    try:
        import yaml
    except ImportError:
        return {"ok": False, "error": "기본 부품(pyyaml)이 아직 없습니다 — 1번을 먼저 눌러 주세요."}
    doc = yaml.safe_load(path.read_text("utf-8")) or {}
    f = {}
    for k in PREFILL_KEYS:
        v = doc.get(k)
        if v:
            f[k] = ", ".join(v) if isinstance(v, list) else str(v)
    blob = base64.urlsafe_b64encode(
        json.dumps(f, ensure_ascii=False).encode("utf-8")).decode("ascii")
    return {"ok": True, "url": f"{HOSTED_URL.rstrip('/')}/?carry={blob}"}


def carry_read(blob: str) -> dict:
    """carry_pack 이 실은 것을 되읽는다. 남이 보낸 문자열이므로 못 읽으면 빈손이다 —
    등록을 막지 않는다(설정만 안 채워질 뿐)."""
    try:
        d = json.loads(base64.urlsafe_b64decode(str(blob or "").encode("ascii")))
    except Exception:
        return {}
    if not isinstance(d, dict):
        return {}
    return {k: str(d[k])[:2000] for k in PREFILL_KEYS if isinstance(d.get(k), str)}


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


def crawl_compare(conn, pid: int, run_id: int) -> dict:
    """직전 회차 대비 신규·해결된 이슈. 회차를 남기는 이유가 이것 하나다.

    (kind, url) 을 이슈의 정체성으로 본다 — detail 은 같은 문제의 서술이라
    거기 숫자가 바뀌었다고 '새 이슈'가 되면 비교가 소음이 된다.
    """
    prev = conn.execute("SELECT id FROM crawl_runs WHERE project_id=? AND id<?"
                        " AND finished_at IS NOT NULL ORDER BY id DESC LIMIT 1",
                        (pid, run_id)).fetchone()
    if not prev:
        return {"prev_run_id": None, "new": [], "fixed": []}
    def keys(rid):
        return {(r["kind"], r["url"] or "") for r in
                conn.execute("SELECT kind, url FROM crawl_issues WHERE run_id=?", (rid,))}
    now, was = keys(run_id), keys(prev["id"])
    fmt = lambda s: [{"kind": k, "url": u} for k, u in sorted(s)][:100]
    return {"prev_run_id": prev["id"], "new": fmt(now - was), "fixed": fmt(was - now)}


def gather(conn, p, at: str | None = None) -> dict:
    """화면 하나가 쓰는 데이터 전부 — 라이브 대시보드와 박제 리포트가 같이 쓴다.

    at: 화면이 고정한 기준 수집일. GSC 스냅샷 축(KPI·추이·움직인 검색어·아깝다·
        근거 페이지)만 그날로 돌아간다. 순위·색인·기기·AI·기회는 수집 주기가 따로라
        각자 최신을 본다 — 그래서 화면 머리가 축별 날짜를 다 적는다. 한 날짜로
        묶으면 대부분의 축이 "그날 데이터 없음"이 된다.
    """
    pid = p["id"]
    cur, prev, period, period_mismatch = scoring.snapshot_pair(conn, pid, at)

    # 집계는 scoring._snap_agg 하나로 — 여기 사본이 있었는데 period_days 조건이
    # 빠져 있어서, 한 날짜에 28일치와 90일치가 같이 있으면 화면의 '움직인 검색어'만
    # 두 기간을 섞어 집계했다 (scoring.md 4-3b 를 어긴다). 판정 함수와 같은 걸 쓴다.
    def gsc_agg(snap):
        return scoring._snap_agg(conn, pid, snap, period) if snap else {}

    ups, downs = scoring.movers(gsc_agg(cur), gsc_agg(prev))

    # 왜 그렇게 됐나 — 클릭 변화를 노출 탓/CTR 탓으로 가르고, 순위 인원수의 이동을
    # 센다. 여태 화면은 "클릭 −12%" 까지만 말하고 원인은 사람에게 미뤘다.
    shift = scoring.click_shift(conn, pid, cur, prev, period)
    bands = scoring.rank_bands(conn, pid, cur, prev, period)
    # 순위는 이미 좋은데 안 눌리는 것 — 고치면 얼마를 되찾는지까지 이미 센다.
    # 판정 함수가 내내 있었는데 기회 적재용으로만 쓰이고 화면에는 안 왔다.
    ctr_gaps = scoring.ctr_gaps(conn, pid, at=at)

    # 총계가 어떻게 구성돼 있나 — 정보성만 잡고 거래성이 0이면 트래픽이 매출로
    # 안 간다. 셋 다 새 수집 없이 이미 DB 에 있던 축인데(intent 는 채워만 놓고
    # 아무도 안 읽었다) 화면 어디에도 안 나오고 있었다. 브랜드 축은 별칭이
    # 필요해서 cfg 를 읽은 뒤, 반환 dict 에서 조립한다.
    by_intent = scoring.keyword_perf(conn, pid, cur, prev, period, "intent")
    by_cluster = scoring.keyword_perf(conn, pid, cur, prev, period, "cluster")
    # 국가는 수집 차원이라 "안 캤다"와 "캤는데 없다"가 다르다 — device 와 같은 규약.
    country_date = scoring._latest(conn, scoring._LATEST_BD, (pid, "country"))
    by_country = scoring.country_perf(conn, pid)

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
    ai_date = (ai_run["started_at"] or "")[:10] if ai_run else None
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

    # ── 검색 × AI 교차. 재료는 위 두 블록이 이미 잡아 둔 것뿐이라 새 수집이 0이다:
    # 질문별 결과(같은 AI 런)와 GSC 스냅샷을 토큰 겹침으로 맞붙인다. 두 축을 한
    # Brain 에 담아 놓고도 여태 서로 안 보던 자리다.
    ai_vs_search = scoring.search_wins_ai_loses(conn, pid, ai_by_prompt)
    ai_outranked = scoring.ai_outranked(conn, pid, cite_share)

    rank_dates = [r[0] for r in conn.execute(
        """SELECT DISTINCT substr(rs.checked_at,1,10) d FROM rank_snapshots rs
             JOIN keywords k ON k.id=rs.keyword_id
            WHERE k.project_id=? ORDER BY d DESC LIMIT 2""", (pid,))]

    def rank_agg(d):
        if not d:
            return {}
        # url·피처까지 읽는다 — 예전엔 순위 숫자만 실어서, 화면이 "몇 위"는 알아도
        # "어느 페이지가 그 자리에 있나"를 말하지 못했다(DB 에는 내내 있었다).
        return {r["keyword"]: r for r in q(conn,
            """SELECT k.keyword, rs.position, rs.url, rs.serp_features_json,
                      rs.aio_present, rs.aio_cited
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
        try:
            feats = json.loads(r["serp_features_json"] or "[]")
        except (TypeError, ValueError):
            feats = []
        ranks.append({"keyword": kw, "pos": cur_pos, "dpos": rd["delta"],
                      "delta": rd, "url": r["url"], "features": feats,
                      "prev_pos": prev_pos,
                      "aio": r["aio_present"], "aio_cited": r["aio_cited"]})
        if r["aio_present"] == 1 and r["aio_cited"] == 0:
            aio_gap.append(kw)
    ranks.sort(key=lambda x: (x["pos"] is None, x["pos"] or 999))

    opps = scoring.opportunities(conn, pid, limit=200, with_id=True)
    # GA4 매출 잠재력 보정 배지 — scoring.load() 가 저장할 때 이미 score() 로 승수를
    # 반영해 놨다. 여기서는 화면이 "왜 이게 위로 왔는지" 말할 수 있게 같은 승수를
    # 다시 구해서 얹기만 한다(저장은 안 한다 — DB 스키마는 안 건드린다). 승수가
    # 1.0(GA4 없음·표본 미달·페이지 없는 kind)이면 키 자체를 안 붙인다 — 화면은
    # ga4_mult 유무로 배지를 켠다.
    ga4_date = scoring._latest(conn, scoring._LATEST_GA4, (pid,))
    if ga4_date:
        ga4_ctx = {"conn": conn, "pid": pid, "cur": cur,
                   "ga4": scoring._ga4_agg(conn, pid, ga4_date),
                   "page_agg": scoring._page_agg(conn, pid, cur, period) if cur else {}}
        for o in opps:
            if o["kind"] not in scoring.GA4_VALUE_KINDS:
                continue
            m = scoring.value_mult(scoring._ga4_metrics(ga4_ctx, query=o["target"]))
            if m != 1.0:
                o["ga4_mult"] = m
                o["ga4_pre_score"] = round(o["score"] / m, 1)
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

    # 행을 펼쳤을 때 보여줄 근거 — 그 검색어에 실제로 걸린 내 페이지들. 기회·키워드·
    # 순위·움직인 검색어가 같은 한 벌을 본다(화면마다 다른 표를 만들면 같은 검색어가
    # 화면마다 다른 페이지를 말한다).
    query_pages = scoring.pages_by_query(conn, pid, [
        *(o["target"] for o in opps),
        *(r["query"] for r in striking),
        *(r["keyword"] for r in ranks),
        *(r["query"] for r in ups), *(r["query"] for r in downs)], at=at)

    # 내 페이지 감사(collect_page) — 최신 검사일 한 벌. 진단 문장은 여기서 만들지
    # 않는다: scoring.page_advice 가 정본이고 화면은 그 결과를 그리기만 한다.
    # 검색어를 같이 넘기는 이유는 "title 에 무엇을 넣어라"의 '무엇'이 그것이라서다.
    audit_date = conn.execute(
        "SELECT MAX(checked_date) FROM page_audits WHERE project_id=?", (pid,)).fetchone()[0]
    q_of_url: dict[str, list] = {}
    for qq, prows in query_pages.items():
        for pr in prows:
            q_of_url.setdefault(pr["page"], []).append((pr["impressions"] or 0, qq))
    page_audits = {}
    if audit_date:
        for a in q(conn, "SELECT * FROM page_audits WHERE project_id=? AND checked_date=?",
                   (pid, audit_date)):
            qs = [x[1] for x in sorted(q_of_url.get(a["url"], []), reverse=True)]
            a["queries"] = qs
            a["advice"] = scoring.page_advice(a, qs, domain=p["domain"] or "")
            page_audits[a["url"]] = a

    # ── 페이지 축 (scoring 의 page_performance·dead_pages·starved_pages) ───
    # 나머지 화면은 전부 검색어 단위다. 검색어 하나하나의 순위는 흔들려도 페이지는
    # 안 흔들린다 — 어느 페이지가 죽고 있는지는 페이지로 합쳐야 보인다. 뒤 둘은
    # 크롤(crawl_pages)과 GSC 를 맞대 본 결과다: 새로 캐는 것 없이 이미 있는 두
    # 수집본을 처음 겹쳐 놓은 것뿐이다.
    page_perf = scoring.page_performance(conn, pid)
    dead_pages = scoring.dead_pages(conn, pid)
    starved_pages = scoring.starved_pages(conn, pid)

    # ── GA4 (collect_ga4) ───────────────────────────────────────────────
    # 클릭 뒤에 무슨 일이 났는지 — page_perf 는 이미 GA4 가 있으면 세션·전환을
    # 얹어서 온다(scoring.page_performance). ga4_date 는 "GA4 를 연결했나"를 화면이
    # 가르는 열쇠다 — 없으면 이 셋(추가 열·전환 없는 페이지·의도별 근사)을 통째로 숨긴다.
    # (opps 근처에서 이미 구해 뒀다 — 여기서 다시 안 구한다)
    zero_conv_pages = scoring.zero_conversion_pages(conn, pid)
    ga4_intent = scoring.ga4_intent_approx(conn, pid, cur, period)
    # 깔때기·채널·분해(기기/국가/신규-재방문) — funnel 이 None 이면(부재) 아래
    # return 딕셔너리에서 키 자체를 안 싣는다(zero_conv_pages 와 달리 부재 규약).
    ga4_funnel, ga4_channels = scoring.ga4_funnel(conn, pid, cur, period)

    # ── 백링크 (collect_backlinks) ────────────────────────────────────────
    # 요약만으로는 "무엇을 할지"가 안 나온다. 어느 페이지가 어떤 앵커로 받았는지,
    # 그리고 경쟁사는 받는데 우리는 못 받는 곳이 어디인지가 실제로 손댈 자리다.
    bl_date = conn.execute(
        "SELECT MAX(checked_date) FROM backlink_summary WHERE project_id=?", (pid,)).fetchone()[0]
    bl_summary, bl_domains, bl_links, bl_anchors, bl_intersect, bl_trend = {}, [], [], [], [], []
    if bl_date:
        row = conn.execute(
            "SELECT * FROM backlink_summary WHERE project_id=? AND checked_date=?",
            (pid, bl_date)).fetchone()
        bl_summary = dict(row) if row else {}
        bl_domains = q(conn, "SELECT * FROM referring_domains WHERE project_id=? AND checked_date=?"
                             " ORDER BY rank DESC LIMIT 200", (pid, bl_date))
        bl_links = q(conn, "SELECT * FROM backlinks WHERE project_id=? AND checked_date=?"
                           " ORDER BY is_broken DESC, rank DESC LIMIT 300", (pid, bl_date))
        bl_anchors = q(conn, "SELECT * FROM backlink_anchors WHERE project_id=? AND checked_date=?"
                             " ORDER BY backlinks DESC LIMIT 100", (pid, bl_date))
        # 이미 우리도 받고 있는 곳은 제안이 아니다 — 화면에 올릴 이유가 없다.
        bl_intersect = q(conn, "SELECT * FROM link_intersect WHERE project_id=? AND checked_date=?"
                               " AND we_have=0 ORDER BY hits DESC, rank DESC LIMIT 100",
                         (pid, bl_date))
        bl_trend = q(conn, "SELECT checked_date d, referring_domains rd, backlinks bl"
                           " FROM backlink_summary WHERE project_id=? ORDER BY 1", (pid,))

    # ── 경쟁 분석 (collect_gap) ───────────────────────────────────────────
    # 몫(share)은 저장하지 않는다 — 분모가 바뀌면 낡는다. 여기서 계산한다.
    cm_date = conn.execute(
        "SELECT MAX(checked_date) FROM competitor_metrics WHERE project_id=?", (pid,)).fetchone()[0]
    comp_metrics, kw_gap, kw_gap_counts = [], [], {}
    if cm_date:
        comp_metrics = q(conn, "SELECT * FROM competitor_metrics WHERE project_id=? AND"
                               " checked_date=? ORDER BY etv DESC", (pid, cm_date))
        total_etv = sum((r["etv"] or 0) for r in comp_metrics)
        for r in comp_metrics:
            r["share"] = round((r["etv"] or 0) / total_etv, 4) if total_etv else None
    gap_date = conn.execute(
        "SELECT MAX(checked_date) FROM keyword_gap WHERE project_id=?", (pid,)).fetchone()[0]
    if gap_date:
        kw_gap = q(conn, "SELECT * FROM keyword_gap WHERE project_id=? AND checked_date=?"
                         " ORDER BY volume DESC LIMIT 300", (pid, gap_date))
        kw_gap_counts = {r["kind"]: r["n"] for r in q(
            conn, "SELECT kind, COUNT(*) n FROM keyword_gap WHERE project_id=? AND checked_date=?"
                  " GROUP BY 1", (pid, gap_date))}

    # 라벨·처방·방어여부는 여기서 한 번 풀어 opps 에 싣는다 — 화면은 그리기만 한다.
    # striking_distance·content_gap 은 밴드/갈래로 처방이 갈리는데, 그 판정은 이미
    # striking()·content_gaps() 가 낸 원본 행(band/kind)에 있다 — 검색어·키워드
    # 문자열로 한 번만 짝짓는다(대상 문자열 자체가 판정하지 않는다).
    sd_band = {r["query"]: r["band"] for r in striking}
    gap_kind = {r["keyword"].strip().lower(): r["kind"] for r in kw_gap}
    for o in opps:
        o["is_defensive"] = scoring.is_defensive(o["kind"])
        band = sd_band.get(o["target"]) if o["kind"] == "striking_distance" else None
        gk = gap_kind.get(str(o["target"]).strip().lower()) if o["kind"] == "content_gap" else None
        o["label"] = scoring.kind_label(o["kind"], band=band)
        o["play"] = scoring.kind_play(o["kind"], band=band, gap_kind=gk)

    # ── 사이트 크롤 (collect_crawl) ───────────────────────────────────────
    # 회차로 남기는 이유가 여기서 쓰인다: 지난번 대비 새로 깨진 것.
    crawl = {}
    cr = conn.execute("SELECT * FROM crawl_runs WHERE project_id=? AND finished_at IS NOT NULL"
                      " ORDER BY id DESC LIMIT 1", (pid,)).fetchone()
    if cr:
        crawl = {"run": dict(cr),
                 "issues": q(conn, "SELECT * FROM crawl_issues WHERE run_id=?"
                                   " ORDER BY CASE severity WHEN 'bad' THEN 0 WHEN 'warn' THEN 1"
                                   " ELSE 2 END, kind LIMIT 500", (cr["id"],)),
                 "counts": {r["kind"]: r["n"] for r in q(
                     conn, "SELECT kind, COUNT(*) n FROM crawl_issues WHERE run_id=? GROUP BY 1",
                     (cr["id"],))},
                 "compare": crawl_compare(conn, pid, cr["id"])}

    # 박제본 호환 분기는 이 번호 하나로 한다 — 필드 유무를 검사하지 않는다
    return {"schema": 1,
            "project": dict(p), "gsc_date": cur, "gsc_prev": prev, "gsc_period": period,
            # 고를 수 있는 날 = 실제로 수집한 날. 화면의 [기준 수집일]이 이걸 그린다.
            "gsc_dates": scoring.snapshot_dates(conn, pid),
            "gsc_pinned": bool(at), "ai_date": ai_date,
            "period_mismatch": period_mismatch, "ups": ups, "downs": downs,
            "shift": shift, "rank_bands": bands, "ctr_gaps": ctr_gaps,
            # 클릭이 늘어도 그게 브랜드 검색이면 SEO 는 제자리다. 별칭 정본은
            # aliases_of 하나 — 비면 판정 불가라 빈 목록이 오고 화면이 그렇게 말한다.
            "brand_split": scoring.brand_split(conn, pid, cur, prev, period,
                                               scoring.aliases_of(cfg)),
            "by_intent": by_intent, "by_cluster": by_cluster,
            "by_country": by_country, "country_date": country_date,
            "striking": striking, "striking_page2": striking_page2,
            "brand_catalog_empty": len(brands) == 0,
            "matrix": matrix, "gap_domains": gap_domains,
            "cite_share": cite_share, "ai_by_prompt": ai_by_prompt,
            "missed": missed, "ai_trend": ai_trend,
            "ai_vs_search": ai_vs_search, "ai_outranked": ai_outranked,
            "opps": opps, "opps_total": opps_total,
            "query_pages": query_pages, "page_audits": page_audits,
            "page_audit_date": audit_date,
            "page_perf": page_perf, "dead_pages": dead_pages,
            "starved_pages": starved_pages,
            "ga4_date": ga4_date, "zero_conv_pages": zero_conv_pages, "ga4_intent": ga4_intent,
            # 페이지 축 임계는 이 뭉치에 같이 싣는다 — 화면이 "왜 이 줄이 걸렸나"를
            # 말할 때 숫자를 다시 적지 않게(정본은 scoring 의 상수다).
            "page_rules": {"starved_links_in": scoring.STARVED_LINKS_IN,
                           "starved_top_n": scoring.STARVED_TOP_N,
                           "ga4_noconv_min_clicks": scoring.GA4_NOCONV_MIN_CLICKS,
                           "ga4_noconv_min_sessions": scoring.GA4_NOCONV_MIN_SESSIONS},
            "runs": runs, "creations": creations,
            "rank_date": rank_dates[0] if rank_dates else None,
            "rank_prev": rank_dates[1] if len(rank_dates) > 1 else None,
            "ranks": ranks[:30], "aio_gap": aio_gap,
            "kw_active": db.count_active_keywords(conn, pid),
            # kind → 한국어 라벨(밴드 없는 통칭) — [기록]처럼 kind 단위로만 아는
            # 자리, [개요] 필터 칩처럼 대상 없이 kind 만 아는 자리가 쓴다.
            "kind_labels": {k.name: k.label for k in scoring.KINDS},
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
            "bl_date": bl_date, "bl_summary": bl_summary, "bl_domains": bl_domains,
            "bl_links": bl_links, "bl_anchors": bl_anchors, "bl_intersect": bl_intersect,
            "bl_trend": bl_trend,
            "comp_date": cm_date, "comp_metrics": comp_metrics,
            "gap_date": gap_date, "kw_gap": kw_gap, "kw_gap_counts": kw_gap_counts,
            "crawl": crawl,
            "progress": prog,
            "guide": guide,
            # GA4 미연결이면 이 다섯 키는 아예 안 싣는다(빈 값이 아니라 부재) —
            # 화면은 `if (!d.ga4_funnel) return;` 으로 조용히 접는다.
            **({"ga4_funnel": ga4_funnel, "ga4_channels": ga4_channels,
                "ga4_by_device": scoring.ga4_breakdown(conn, pid, "device"),
                "ga4_by_country": scoring.ga4_breakdown(conn, pid, "country",
                                                        top=scoring.GA4_BD_COUNTRY_TOP),
                "ga4_by_newret": scoring.ga4_breakdown(conn, pid, "newvsreturning")}
              if ga4_funnel else {})}


def payload(project: str, at: str | None = None) -> dict:
    conn = db.connect()
    try:
        p = db.get_project(conn, project)
        return gather(conn, p, at)
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
        if u.path == "/api/setup/carry":     # 읽기 전용 — 호스팅으로 넘길 링크
            return self._json(carry_pack(parse_qs(u.query).get("project", [""])[0]))
        if u.path == "/api/projects":
            conn = db.connect()
            names = [r[0] for r in
                     conn.execute("SELECT name FROM projects ORDER BY name")]
            conn.close()
            return self._json(names)
        if u.path == "/api/data":
            qs_ = parse_qs(u.query)
            name = qs_.get("project", [""])[0]
            at = qs_.get("date", [""])[0] or None
            try:
                return self._json(payload(name, at))
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
    html = _assemble().decode("utf-8")             # local
    hosted_html = _assemble("hosted").decode("utf-8")
    defs = view_defs()
    assert [d["id"] for d in defs] == VIEW_ORDER, "선언 순서가 조립 순서와 다르다"
    assert "window.__VIEWS__=" in html, "매니페스트가 안 실렸다"
    assert "window.__STAGES__=" in html, "단계 용어표가 안 실렸다"
    for left in ("<!--MANIFEST-->", "<!--VIEWS-->", "<!--SECTIONS-->"):
        assert left not in html, f"자리표가 안 채워졌다: {left}"
    for d in defs:
        # 마크업만 있고 그리는 코드가 없는 뷰를 막는 검사다. 셸이 직접 그리는 화면
        # ([안내]·[설정] — renderGuide/renderSetup)은 자기 선언에 그렇다고 적는다.
        # 목록을 여기 또 두지 않으려고 뷰가 말하게 한다.
        if d.get("render") != "shell":
            assert f'VIEW("{d["id"]}"' in html, f'{d["id"]} 가 VIEW() 로 등록되지 않았다'
        assert d["title"] and isinstance(d["stages"], list)
        for i in d["sections"]:                  # 담는다고 선언한 요소는 실제로 있어야
            assert f'id="{i}"' in html, f'{d["id"]} 가 없는 요소 id 를 담는다: {i}'

    # 배포 전용 섹션 — 선언한 view/after 가 실제로 있어야 하고(after 는 원본 뷰의
    # 섹션이거나, 같은 화면을 가리키는 다른 섹션의 id 여도 된다 — sm-dim 은
    # sm-perf 뒤에 붙는다), 해당 variant 조립에만 껴야 한다(local 에 새면 원본
    # 없는 요소가 로컬에도 뜬다).
    secs = section_defs()
    base_sections = {d["id"]: d["sections"] for d in defs}
    sec_ids = {s["id"] for s in secs}
    for s in secs:
        assert s.get("view") in base_sections, f"{s['id']} 가 없는 화면을 가리킨다: {s.get('view')}"
        assert s["id"] not in base_sections[s["view"]],             f"{s['id']} 가 원본 뷰에 이미 있다 — 매니페스트가 소유할 것이다"
        assert s.get("after") in base_sections[s["view"]] or s.get("after") in sec_ids,             f"{s['id']} 를 붙일 자리가 {s['view']} 에 없다: {s.get('after')}"
        if s.get("only") in (None, "hosted"):
            assert f'id="{s["id"]}"' in hosted_html, f"{s['id']} 가 hosted 조립에 안 낀다"
        if s.get("only") not in (None,):
            assert f'id="{s["id"]}"' not in html, f"{s['id']} 가 안 실려야 할 local 조립에 꼈다"

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

    # CSV 작성기는 한 벌뿐이다 — 브라우저가 실제로 받는 한 덩어리(hosted 조립 +
    # 애드온)를 본다. 애드온은 assemble() 산출물 밖에 있어서, 조립본만 보면
    # 애드온이 다시 만든 사본이 구조적으로 안 보인다.
    addon_f = Path(__file__).resolve().parents[3] / "server" / "assets" / "dash.html"
    combined = hosted_html + (addon_f.read_text("utf-8") if addon_f.exists() else "")
    # carry 는 URL 을 왕복한다 — 한글 씨앗과 쉼표가 그대로 돌아와야 하고, 남이 보낸
    # 쓰레기에는 빈손이어야 한다(등록을 막지 않는다).
    packed = base64.urlsafe_b64encode(json.dumps(
        {"gsc_property": "sc-domain:x.com", "seed_keywords": "ai 티어표, 순위표",
         "허튼키": "무시"}, ensure_ascii=False).encode("utf-8")).decode("ascii")
    got = carry_read(packed)
    assert got["seed_keywords"] == "ai 티어표, 순위표", got
    assert "허튼키" not in got, "PREFILL_KEYS 밖의 키가 새어 들어온다"
    assert carry_read("!!not base64!!") == {} and carry_read("") == {}

    assert combined.count("URL.createObjectURL") == 1, "CSV 작성기가 여러 벌이다"
    assert "String(c ?? \"\")" in combined, "CSV 가 빈 칸을 빈 칸으로 안 쓴다"
    print(f"dashboard self-check ok — 뷰 {len(defs)}개, 섹션 {len(secs)}개, "
          f"최상위 이름 {len(seen)}개")


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
        if remote.owns(a.project):
            # 서버가 자기 brain 으로 같은 템플릿을 박제한다 — 여기서 다시 만들지 않는다.
            out = db.CAPTURE_HOME / "reports" / a.project / f"{date.today()}.html"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(remote.fetch("/api/report", params={"project": a.project}))
        else:
            out = export(a.project, a.actions)
        print(f"report: {out}")
        if a.open:
            import webbrowser
            webbrowser.open(out.as_uri())
        return

    # 원격 사이트는 로컬 서버를 띄우지 않는다 — 데이터가 서버 brain 에 있어서
    # 여기서 띄우면 빈 화면을 보여 준다. 호스팅 화면의 그 사이트로 보낸다.
    if a.project and remote.owns(a.project):
        url = f"{remote.config()['url']}/d#{a.project}"
        print(f"dashboard: {url}")
        if a.open:
            import webbrowser
            webbrowser.open(url)
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
