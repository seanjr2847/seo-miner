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
import brief      # noqa: E402  (요청문 — 기회마다 AI 에 붙여 넣을 브리프를 세운다)
import collect_crawl  # noqa: E402  (크롤 이슈 갈래 이름표 정본)
import collector  # noqa: E402  (프로젝트 설정 읽기 — 수집기와 같은 경로로)
import db         # noqa: E402
import doctor     # noqa: E402  (setup 스킬의 진단 — 대시보드 상단 배너용)
import gen_prompts  # noqa: E402  (AI 질문 갈래 정본 — 매니페스트가 이걸 실어 보낸다)
import htmlsafe   # noqa: E402  (문서에 값을 박을 때의 이스케이프 — 한 벌)
import paths      # noqa: E402  (사이트별 로컬 폴더 장부 — 설정 0단계)
import remote     # noqa: E402  (원격 사이트면 박제·화면을 서버가 낸다)
import scoring    # noqa: E402  (판정 규칙 — 화면·박제본·산문이 같은 임계값을 본다)
import serp_adapter  # noqa: E402  (언어-지역 목록 정본 — 설정 폼이 이걸 그린다)
import stage      # noqa: E402  (진행 상태 및 6단계 판정 정본)

TPL = Path(__file__).parent.parent / "templates"
# 화면 순서 = 메뉴 순서. [안내]·[설정]은 여태 헤더 토글이 본문 위에 얹던 패널이었다
# — 화면으로 세우면 열렸나 닫혔나 하는 상태가 없어지고, 설정이 데이터 위에 오지 않는다.
# [심사]가 맨 앞이다 — 측정이 물어온 검색어를 가리는 일이 기회를 보는 일보다 앞선다
# (docs/superpowers/specs/2026-09-08-keyword-triage-design.md).
VIEW_ORDER = ["triage", "overview", "analysis", "keywords", "rank", "ai", "site",
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


# 차트 라이브러리 — templates/vendor/chart.umd.min.js(Chart.js 4.5.1, MIT)를 <head> 에
# <script> 로 박는다. CDN 을 안 쓰는 까닭: 박제본(/capture report)은 파일 하나로
# 오프라인에서 열려야 한다. 파일은 npm 이 준 그대로 둔다(test_seams 가 해시로 대조한다) —
# 끝의 sourceMappingURL 한 줄만 박을 때 뗀다(없는 .map 을 개발자 도구가 찾으러 간다).
VENDOR_JS = TPL / "vendor" / "chart.umd.min.js"
_VENDOR: list[str] = []


def _vendor_script() -> str:
    if not _VENDOR:
        js = re.sub(r"\n//# sourceMappingURL=\S+\s*$", "\n", VENDOR_JS.read_text("utf-8"))
        if "</script" in js.lower():
            raise ValueError("vendor 스크립트에 </script 가 있다 — 인라인으로 박으면 문서가 잘린다")
        _VENDOR.append(f"<script>{js}</script>")
    return _VENDOR[0]


# 사이트 종류 — id 와 라벨이 한 벌이다. 고르는 자리는 둘(로컬 설정 폼 settings.html,
# 호스팅 등록 화면 server/app.html)이고 둘 다 이 표에서 받는다: 설정 폼은 조립이
# <option> 으로 채우고, 등록 화면은 server/app.py 가 window.__TYPES__ 로 싣는다
# (serp_adapter.LOCALES 와 같은 길이다). 라벨은 **혼자 서야** 한다 — 등록 화면은
# id 를 안 보여 준다 — 그러면서 id 를 되풀이하지 않는다: 설정 폼은 `id — 라벨` 로
# 그리고(사용자가 ~/.capture/projects/*.yaml 에 그 id 를 직접 적는다), 그 자리에서
# `directory — 디렉터리…` 처럼 겹치면 읽히지 않는다. 표기 규칙은 화면마다 다르되
# **문구는 한 벌**이다. 순서도 여기가 정본이다(흔한 것부터).
PROJECT_TYPES = (("saas", "웹 서비스 · 앱"),
                 ("game", "게임"),
                 ("local_business", "지역 비즈니스"),
                 ("directory", "목록 · 디렉터리"))
# 받는 쪽 검증이 쓰는 id 만 — 사본이 아니라 위 표에서 뽑은 것이다.
PROJECT_TYPE_IDS = tuple(i for i, _ in PROJECT_TYPES)


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
    views_json = htmlsafe.js(defs)
    # 호스팅은 유료 키 문장이 갈린다 — 서버가 키를 대므로 "키가 필요합니다"가
    # 거짓말이 된다. 갈래를 아는 것은 여기(variant)뿐이라 여기서 골라 싣는다.
    stages_json = htmlsafe.js(stage.stage_labels(variant))
    hosted_flag = "window.SM_HOSTED=true;" if variant == "hosted" else ""
    # AI 질문 갈래도 한 벌이다(gen_prompts.CATEGORY_CHOICES) — 고르는 자리는 호스팅
    # 애드온의 [AI 질문 관리] 하나뿐인데 거기가 사본을 들고 있었고, 그 사본에만 있는
    # "general" 을 사용자가 고를 수 있었다(만드는 쪽은 그 값을 모르던 시절이 있다).
    cats_json = htmlsafe.js(gen_prompts.CATEGORY_CHOICES)
    # 언어-지역 목록도 한 벌이다(serp_adapter.LOCALES) — 설정 폼의 <select> 를 여기서
    # 채운다. 매니페스트에는 안 싣는다: 조립본 안에서 그 표를 읽는 자리가 없다.
    # (등록 화면 app.html 의 window.__LOCALES__ 는 server/app.py 가 따로 실어 보낸다.)
    locale_opts = "".join(f'<option value="{htmlsafe.attr(c)}">'
                          f"{htmlsafe.attr(c)} — {htmlsafe.attr(t)}</option>"
                          for c, t in serp_adapter.LOCALES)
    # 사이트 종류도 같은 길이다(PROJECT_TYPES). 이 화면은 id 를 보여 준다 — 언어-지역
    # 과 같은 `id — 라벨` 꼴이다. 사용자가 ~/.capture/projects/*.yaml 에 적는 게 그 id 다.
    type_opts = "".join(f'<option value="{htmlsafe.attr(i)}">'
                        f"{htmlsafe.attr(i)} — {htmlsafe.attr(t)}</option>"
                        for i, t in PROJECT_TYPES)
    manifest = (f"<script>{hosted_flag}window.__VIEWS__={views_json};"
                f"window.__STAGES__={stages_json};window.__AIQ_CATS__={cats_json};</script>")
    return (base
            .replace("<!--MANIFEST-->", manifest, 1)
            .replace("<!--VENDOR-->", _vendor_script(), 1)
            .replace("<!--VIEWS-->", parts.replace("<!--LOCALE_OPTIONS-->", locale_opts, 1)
                                          .replace("<!--TYPE_OPTIONS-->", type_opts, 1))
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
# 값이 아무 문자열이 아니라 **표의 id** 여야 하는 키 — 설정 0단계의 라디오 셋.
# 선택지 사본을 여기 만들지 않는다: 표는 doctor 가 갖고 화면도 그걸 그린다.
CHOICE_FIELDS = {doctor.MODE_ENV: doctor.MODES, doctor.TOOL_ENV: doctor.TOOLS,
                 doctor.TERMINAL_ENV: doctor.TERMINALS}
KEY_FIELDS = ("OPENROUTER_API_KEY", "SERPER_API_KEY",
              "DATAFORSEO_LOGIN", "DATAFORSEO_PASSWORD") + tuple(CHOICE_FIELDS)
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
    blob = htmlsafe.js(snap)          # </script> 차단 — 이스케이프는 htmlsafe 한 벌이다
    html = assemble("frozen").replace(
        "<!--SNAPSHOT-->", f"<script>window.__SNAPSHOT__={blob}</script>", 1)
    # 박제본은 남에게 보내는 파일이라 열 때 바깥을 부르지 않는다 — 웹폰트 <link> 를
    # 뗀다(셸의 서체 스택이 시스템 폰트로 조용히 떨어진다). test_setup_api 가 지킨다.
    # href 가 http 로 시작하는 것만 — favicon 의 data: URL 안에 svg xmlns(http://…)가
    # 있어서 그걸로 잡으면 아이콘 태그가 반쯤 잘려 글자로 새어 나온다.
    html = re.sub(r'[ \t]*<link[^>]*href="https?://[^>]*>\n?', "", html)
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
    # 고르는 값은 표에 있는 것만 받는다 — 화면이 보내는 것은 늘 표의 id 이므로
    # 여기 걸리는 건 손으로 만든 요청뿐이다. 모르는 값을 env 에 적어 두면 그 뒤로
    # 화면이 "아직 안 고름"으로 보이는데 파일에는 값이 있는 상태가 된다.
    for k, table in CHOICE_FIELDS.items():
        v = str(values.get(k, "")).strip()
        if v and not any(v == row[0] for row in table):
            return {"ok": False, "error": "화면에 없는 값이 왔습니다 — 새로 고침한 뒤 "
                                          "다시 골라 주세요."}
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
    if f.get("type") not in PROJECT_TYPE_IDS:
        return {"ok": False, "error": f"종류는 {'/'.join(PROJECT_TYPE_IDS)} 중 하나"}
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
           "locale": str(f.get("locale") or db.DEFAULT_LOCALE).strip(),
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
# 2026-09-04: Railway 서비스의 실제 도메인은 `seo-miner-production.up.railway.app` 이다
# (`railway domain` 실측). 예전 기본값 `seo-miner.up.railway.app` 은 붙어 있지 않아
# "웹에서 이어 하기" 링크가 Railway 의 404("train has not arrived") 로 떨어졌다.
HOSTED_URL = os.environ.get("SEOMINER_HOSTED_URL",
                            "https://seo-miner-production.up.railway.app")


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


# ── 설정 0단계: 사이트별 로컬 폴더 · 호스팅 연결 ─────────────────────────────
# 실행 버튼이 "어느 폴더에서 도구를 여느냐"를 여기서 정한다. 장부의 정본은
# paths.site_dirs() 한 자리이고, 화면은 이 셋만 부른다.

def orca_worktrees() -> list[str]:
    """Orca 가 아는 작업 폴더 경로들 — 폴더 입력 칸의 후보 목록(datalist).

    Orca 가 없으면 빈 목록이다. 후보가 없다고 폴더를 못 적는 것은 아니다 —
    손으로 적는 길이 정본이고 이건 거들기만 한다.
    """
    d = doctor.orca_json("worktree", "ps") or {}
    rows = (d.get("result") or {}).get("worktrees") or []
    return [w["path"] for w in rows if isinstance(w, dict) and w.get("path")]


def setup_dirs() -> dict:
    """폴더 표의 재료 — 지금 저장된 것과 Orca 가 아는 후보들."""
    return {"dirs": paths.site_dirs(), "worktrees": orca_worktrees()}


def setup_dir(body: dict) -> dict:
    """폴더 한 줄 저장. 검사·저장은 paths.bind_site_dir 한 자리다 — 채팅 입구
    (`paths.py dir`)와 같은 검사를 거쳐야 두 길로 적힌 장부가 갈리지 않는다."""
    return paths.bind_site_dir(body.get("project"), body.get("path"))


# 웹 [설정]이 내는 "명령어로 연결하기" 한 줄에서 필요한 것은 주소와 그 다음 토큰뿐이다.
_REMOTE_LINE = re.compile(r"(https?://\S+)\s+(\S+)")


def setup_remote(body: dict) -> dict:
    """붙여 넣은 한 줄로 호스팅에 붙는다.

    **그 줄을 명령으로 돌리지 않는다** — 사용자가 어디선가 복사해 오는 문자열이라
    그대로 실행하면 거기 섞인 것이 같이 돈다. 두 토큰만 뽑아 remote.link 로 넘긴다.
    """
    m = _REMOTE_LINE.search(str(body.get("line") or ""))
    if not m:
        return {"ok": False, "error": "주소와 토큰을 못 찾았습니다. 웹 [설정]의 "
                                      "'명령어로 연결하기' 한 줄을 그대로 붙여 주세요."}
    try:
        remote.link(m.group(1), m.group(2))
    except Exception as e:          # 주소가 틀렸거나·토큰이 죽었거나·네트워크가 없거나
        return {"ok": False, "error": str(e)}
    c = remote.config() or {}
    return {"ok": True, "url": c.get("url") or m.group(1).rstrip("/"),
            "projects": list(c.get("projects") or [])}


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


def _axis_gsc(conn, pid: int, cfg: dict, at: str | None) -> dict:
    """검색(GSC) 축 — KPI·추이·움직인 검색어·아깝다·의도/클러스터/국가/기기/색인.

    at: 화면이 고정한 기준 수집일. 이 축 전체가 그날로 돌아간다(다른 축은 각자
        수집 주기가 달라 안 따라간다 — gather() 의 docstring 참고).
    """
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
    # 아무도 안 읽었다) 화면 어디에도 안 나오고 있었다.
    by_intent = scoring.keyword_perf(conn, pid, cur, prev, period, "intent")
    by_cluster = scoring.keyword_perf(conn, pid, cur, prev, period, "cluster")
    # 국가는 수집 차원이라 "안 캤다"와 "캤는데 없다"가 다르다 — device 와 같은 규약.
    country_date = scoring._latest(conn, scoring._LATEST_BD, (pid, "country"))
    by_country = scoring.country_perf(conn, pid)

    # 남의 브랜드 카탈로그는 yaml에 있다. Brain에는 등록됐는데 yaml을 지운
    # 프로젝트도 화면은 떠야 한다 — project_cfg가 경고만 하고 빈 설정을 준다.
    brands = scoring.foreign_brands(conn, pid, cfg)
    striking = scoring.striking(conn, pid, cur, brands=brands)
    striking_page2 = sum(1 for r in striking if r.get("band") == "page2")

    # 일별 추이·기기 격차·색인 점검. 판정 함수가 빈 목록을 주는 경우가 두 가지라
    # (아직 안 수집 / 수집했는데 문제 없음) 최신 수집일도 같이 내려보낸다 —
    # 화면이 그 둘을 구분해서 안내 문구를 갈라 쓴다.
    device_date = scoring._latest(conn, scoring._LATEST_BD, (pid, "device"))
    index_date = scoring._latest(conn, scoring._LATEST_IX, (pid,))
    daily = scoring.daily_trend(conn, pid)
    daily_stats = {
        "clicks_sum": sum(r["clicks"] or 0 for r in daily),
        "impressions_sum": sum(r["impressions"] or 0 for r in daily),
        "clicks_max": max([r["clicks"] or 0 for r in daily] + [1]),
        "impressions_max": max([r["impressions"] or 0 for r in daily] + [1]),
    }

    return {
        "gsc_date": cur, "gsc_prev": prev, "gsc_period": period,
        # 고를 수 있는 날 = 실제로 수집한 날. 화면의 [기준 수집일]이 이걸 그린다.
        "gsc_dates": scoring.snapshot_dates(conn, pid),
        "gsc_pinned": bool(at),
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
        "daily": daily, "daily_stats": daily_stats,
        "device_gap": scoring.device_gap(conn, pid),
        "index_issues": scoring.index_issues(conn, pid),
        "device_date": device_date, "index_date": index_date,
        "rules": {"page1": scoring.PAGE1, "striking_lo": scoring.STRIKING_LO,
                  "striking_hi": scoring.STRIKING_HI,
                  "rank_noise": scoring.RANK_NOISE,
                  "device_gap_pos": scoring.DEVICE_GAP_POS,
                  "device_min_imp": scoring.DEVICE_MIN_IMP,
                  # 속도 기준 — 화면이 숫자를 적으면 판정과 두 벌이 된다
                  "lcp_good_ms": scoring.LCP_GOOD_MS,
                  "inp_good_ms": scoring.INP_GOOD_MS,
                  "cls_good": scoring.CLS_GOOD},
        # KPI 추이도 비교 짝과 같은 period_days만 — 28일치 사이에 90일치가 끼면
        # 그래프·Δ가 전부 거짓이 된다. SQL이 이미 period_days로 걸렀으므로 p 필드는 뺀다.
        "trend": [dict(r) for r in conn.execute(
            """SELECT snapshot_date d, SUM(clicks) clk,
                      SUM(impressions) imp, COUNT(DISTINCT query) q
                 FROM gsc_snapshots WHERE project_id=? AND period_days=?
                GROUP BY 1 ORDER BY 1""", (pid, period))],
    }


def _axis_rank(conn, pid: int) -> dict:
    """순위(rank_snapshots) 축 — SERP 순위·AIO 인용 갭·추적 중인 키워드 수.

    "ranks" 는 여기서 자르지 않은 전체 목록이다 — gather() 가 화면용으로 30개까지
    자르고, 그 앞의 전체는 query_pages 근거 조립에 쓴다.
    """
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
                      rs.aio_present, rs.aio_cited, rs.aio_domains_json
                 FROM rank_snapshots rs JOIN keywords k ON k.id=rs.keyword_id
                WHERE k.project_id=? AND substr(rs.checked_at,1,10)=?""", (pid, d))}

    # 검색결과 상위 몇 줄 — 요청문이 "빠진 구간" 을 짐작이 아니라 비교로 찾는 재료.
    # 순위 숫자와 같은 회차에서 나온다(같은 날짜 키로 읽는다).
    serp_top: dict[str, list] = {}
    if rank_dates:
        for r in q(conn, """SELECT k.keyword, s.position, s.url, s.title, s.domain, s.is_own
                              FROM serp_results s JOIN keywords k ON k.id = s.keyword_id
                             WHERE k.project_id=? AND substr(s.checked_at,1,10)=?
                             ORDER BY k.keyword, s.position""", (pid, rank_dates[0])):
            serp_top.setdefault(r["keyword"], []).append(r)

    # 구글이 이 검색어에 같이 보여 준 질문·연관 검색어(팬아웃 재료) — 요청문이 "함께
    # 답해야 할 질문"으로 싣는다. serp_top 과 같은 회차·같은 날짜 키로 읽는다: 다른 날의
    # 질문이 이 날의 상위 목록 옆에 서면 서로 다른 검색결과를 한 장처럼 말하게 된다.
    # 키가 없는 검색어는 "안 쟀거나 구글이 아무것도 안 보여 줬다"이다 — 요청문은 둘 다
    # 블록을 안 단다(없다고 단정하지 않는다).
    serp_fanout: dict[str, list] = {}
    if rank_dates:
        for r in q(conn, """SELECT k.keyword, s.kind, s.text
                              FROM serp_questions s JOIN keywords k ON k.id = s.keyword_id
                             WHERE k.project_id=? AND substr(s.checked_at,1,10)=?
                             ORDER BY k.keyword, s.kind, s.position""", (pid, rank_dates[0])):
            serp_fanout.setdefault(r["keyword"], []).append({"kind": r["kind"], "text": r["text"]})

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
        # None = 요약이 없었거나 안 쟀다, [] = 요약은 떴는데 인용 도메인을 못 뽑았다
        # (db.write_rank_snapshot 의 불변식). 둘을 뭉치지 않는다.
        try:
            aio_doms = (json.loads(r["aio_domains_json"])
                        if r["aio_domains_json"] is not None else None)
        except (TypeError, ValueError):
            aio_doms = None
        ranks.append({"keyword": kw, "pos": cur_pos, "dpos": rd["delta"],
                      "delta": rd, "url": r["url"], "features": feats,
                      "prev_pos": prev_pos,
                      "aio": r["aio_present"], "aio_cited": r["aio_cited"],
                      "aio_domains": aio_doms,
                      # AI 요약 처방의 갈래 — 요약이 뜬 행에만. 판정은 scoring 한 곳.
                      "aio_band": scoring.aio_band(cur_pos) if r["aio_present"] == 1 else None})
        if r["aio_present"] == 1 and r["aio_cited"] == 0:
            aio_gap.append(kw)
    ranks.sort(key=lambda x: (x["pos"] is None, x["pos"] or 999))
    gap_set = set(aio_gap)

    return {
        "rank_date": rank_dates[0] if rank_dates else None,
        "rank_prev": rank_dates[1] if len(rank_dates) > 1 else None,
        "ranks": ranks, "aio_gap": aio_gap, "serp_top": serp_top,
        # 상위 글의 제목·H2 — 주소 단위로 한 벌(db.serp_outlines). 요청문이 "빠진 구간"을
        # 짐작이 아니라 비교로 찾는 재료다. 없으면 빈 dict 이고 요청문은 붙여 넣기 칸으로
        # 물러선다 — 여기서 "H2 0개"를 지어내지 않는다.
        "serp_outlines": db.serp_outlines(
            conn, [r["url"] for rows in serp_top.values() for r in rows if r.get("url")]),
        "serp_fanout": serp_fanout,
        # AI 요약 빠짐 검색어의 순위 행 전부 — gather() 가 ranks 를 화면용으로 30개까지
        # 자르는데(순위 순), AI 요약 기회는 대개 순위가 낮거나 없어서 그 30 밖에 선다.
        # 요청문(_ev_aio)이 "몇 위·누가 대신 인용됐나"를 말하려면 잘리기 전 행이 필요하다.
        "aio_gap_ranks": {r["keyword"]: r for r in ranks if r["keyword"] in gap_set},
        # 검색어 → 최신 순위 조회 행 전부(자르기 전). aio_gap_ranks 는 "최신 회차에
        # AI 요약이 떴고 우리가 인용 안 된" 검색어만 담는데, 기회는 그보다 옛 회차에서도
        # 선다 — 그러면 판정은 "실측 6위"라고 쓴 채 요청문은 그 숫자를 못 찾아 GSC
        # 평균만 보고 "가장 나은 순위도 22.4위"라고 단정했다. 한 요청문 안에 순위가
        # 세 개 적히고 어느 것이 무엇인지는 아무 데도 없었다.
        "rank_by_kw": {r["keyword"]: r for r in ranks},
        # 검색어 → 그 검색어를 조회한 언어-지역. 페이지가 없는 꼴(새 글)에는 '페이지 언어'가
        # 설 수 없어 요청문이 늘 사이트 기본으로 떨어졌다 — 영어 검색어에 "한국어로 쓰고
        # title 30자"가 나갔다(theotherskin `do papular scars go away`). 언어를 아는 것은
        # 이 열뿐이다: collect_serp 가 조회에 쓴 값도 keywords.locale 이다.
        "kw_locales": {r["keyword"]: r["locale"] for r in q(
            conn, "SELECT keyword, locale FROM keywords WHERE project_id=? AND locale IS NOT NULL",
            (pid,))},
        # 기회로 아직 안 올라온 행의 폴백 처방이 쓴다(rank.html) — 문구는 기회와 같은 한 벌.
        "aio_play": {b: scoring.kind_play("aio_exposure", band=b) for b in scoring.AIO_BANDS},
        "kw_active": db.count_active_keywords(conn, pid),
    }


def _axis_ai(conn, pid: int) -> dict:
    """AI 인용 축 (collect_ai) — 엔진×카테고리 인용률, 빠진 질문, 검색×AI 교차."""
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

        # 질문 하나 = 줄 하나. 수(인용·이름·추천)·대신 인용된 곳(도메인별 횟수)·엔진별
        # 내역(by_engine — 엔진×카테고리 표를 누르면 화면이 이걸로 거른다)·엔진별 발췌는
        # 전부 scoring.ai_tally 한 벌이다. 예전에는 여기서 MAX(cited_domains_json) 로
        # 표본 하나를 사실상 무작위로 골랐고, 기회(ai_gaps)는 전 표본을 셌다 — 요청문은
        # 이쪽을 읽었으니 틀린 쪽을 읽은 셈이었다.
        ai_by_prompt = q(conn,
            """SELECT p2.id, p2.prompt, p2.category
                 FROM ai_checks c JOIN ai_prompts p2 ON p2.id=c.prompt_id
                WHERE c.run_id=? GROUP BY p2.id
                ORDER BY SUM(c.cited) DESC, SUM(c.mentioned) DESC""", (ai_run["id"],))
        tally = scoring.ai_tally(conn, ai_run["id"])
        for r in ai_by_prompt:
            t = tally.get(r["id"]) or {}
            r.update(t)
            # 화면·교차표가 읽어 온 모양 그대로 — 엔진은 쉼표로 이은 이름
            r["engines"] = ",".join(t.get("engines") or [])
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

    # 기회를 세운 바로 그 행 — 요청문의 근거표와 처방 갈래(lean)가 이것을 읽는다.
    # ai_by_prompt 는 최신 회차 하나만 보는데 기회(scoring.ai_gaps)는 질문마다 **끝난**
    # 회차의 최신 측정을 본다. 최신 회차가 도는 중이거나 끊겼으면 두 행이 다른 표본을
    # 보고, 요청문이 기회 근거와 다른 수를 말한다. 모양은 ai_by_prompt 와 같게 맞춘다
    # (engines = 쉼표로 이은 이름) — 요청문이 어느 쪽에서 왔는지 가르지 않게.
    ai_gap_rows = [{**g, "engines": ",".join(g.get("engine_names") or [])}
                   for g in scoring.ai_gaps(conn, pid)]

    return {
        "ai_date": ai_date, "matrix": matrix, "gap_domains": gap_domains,
        "cite_share": cite_share, "ai_by_prompt": ai_by_prompt,
        "ai_gap_rows": ai_gap_rows,
        "missed": missed, "ai_trend": ai_trend,
        "ai_vs_search": ai_vs_search, "ai_outranked": ai_outranked,
        # 켜 둔 질문 중 끝난 확인에서 못 잰 것·오래된 것·옛 생성기가 지은 것의 개수와
        # 마지막 확인이 끊겼는지. 위 표들은 최신 런 하나만 보므로 거기 안 나오는 질문이
        # 있다 — 그 수를 화면이 "측정 안 됨"으로 말한다(scoring.ai_health 가 정본).
        "ai_health": scoring.ai_health(conn, pid),
    }


def _axis_page_perf(conn, pid: int) -> dict:
    """페이지 축 (scoring 의 page_performance·dead_pages·starved_pages).

    나머지 화면은 전부 검색어 단위다. 검색어 하나하나의 순위는 흔들려도 페이지는
    안 흔들린다 — 어느 페이지가 죽고 있는지는 페이지로 합쳐야 보인다. 뒤 둘은
    크롤(crawl_pages)과 GSC 를 맞대 본 결과다: 새로 캐는 것 없이 이미 있는 두
    수집본을 처음 겹쳐 놓은 것뿐이다.
    """
    return {
        "page_perf": scoring.page_performance(conn, pid),
        "dead_pages": scoring.dead_pages(conn, pid),
        "starved_pages": scoring.starved_pages(conn, pid),
        # 페이지 축 임계는 이 뭉치에 같이 싣는다 — 화면이 "왜 이 줄이 걸렸나"를
        # 말할 때 숫자를 다시 적지 않게(정본은 scoring 의 상수다).
        "page_rules": {"starved_links_in": scoring.STARVED_LINKS_IN,
                       "starved_top_n": scoring.STARVED_TOP_N,
                       "ga4_noconv_min_clicks": scoring.GA4_NOCONV_MIN_CLICKS,
                       "ga4_noconv_min_sessions": scoring.GA4_NOCONV_MIN_SESSIONS},
    }


def _axis_ga4(conn, pid: int, at: str | None) -> dict:
    """GA4 축 (collect_ga4) — 클릭 뒤에 무슨 일이 났는지.

    page_perf 는 이미 GA4 가 있으면 세션·전환을 얹어서 온다(scoring.page_performance,
    _axis_page_perf 소관 — 여기서 다시 안 구한다). ga4_date 는 "GA4 를 연결했나"를
    화면이 가르는 열쇠다 — 없으면 나머지 GA4 키를 통째로 숨긴다.

    깔때기·채널·분해(기기/국가/신규-재방문)는 funnel 이 None 이면(부재) 반환
    딕셔너리에 키 자체를 안 싣는다(zero_conv_pages 와 달리 부재 규약).
    """
    cur, _prev, period, _mismatch = scoring.snapshot_pair(conn, pid, at)
    ga4_date = scoring._latest(conn, scoring._LATEST_GA4, (pid,))
    zero_conv_pages = scoring.zero_conversion_pages(conn, pid)
    ga4_intent = scoring.ga4_intent_approx(conn, pid, cur, period)
    ga4_funnel, ga4_channels = scoring.ga4_funnel(conn, pid, cur, period)

    out = {"ga4_date": ga4_date, "zero_conv_pages": zero_conv_pages, "ga4_intent": ga4_intent,
           **_ai_referrals(conn, pid)}
    if ga4_funnel:
        out.update({"ga4_funnel": ga4_funnel, "ga4_channels": ga4_channels,
                    "ga4_by_device": scoring.ga4_breakdown(conn, pid, "device"),
                    "ga4_by_country": scoring.ga4_breakdown(conn, pid, "country",
                                                            top=scoring.GA4_BD_COUNTRY_TOP),
                    "ga4_by_newret": scoring.ga4_breakdown(conn, pid, "newvsreturning")})
    return out


def _ai_referrals_in_play(conn, pid: int, urls) -> dict:
    """요청문이 손댈 페이지의 AI 방문 — 경로 → 행. 화면 목록(ai_referral_pages)은 세션
    순 상위 100 에서 자르는데, 요청문은 **그 페이지** 를 찾는다. 긴 꼬리의 페이지가 기회에
    걸리면 방문이 있어도 그 줄이 안 섰다.

    값이 없는 페이지는 키를 안 만든다 — 안 잰 것(None)과 0 은 _ai_referrals 가 이미
    가른다(ai_referral_meta). 여기는 "잰 날에 이 페이지로 온 것" 만 싣는다.
    """
    from urllib.parse import urlsplit
    paths = {(urlsplit(u).path or "/") for u in urls if u}
    m = conn.execute("SELECT snapshot_date FROM ga4_ai_measured WHERE project_id=?"
                     " ORDER BY snapshot_date DESC LIMIT 1", (pid,)).fetchone()
    if not (m and paths):
        return {}
    out: dict[str, dict] = {}
    ph = ",".join("?" * len(paths))
    for r in conn.execute(
            f"SELECT source, landing_page, sessions, key_events FROM ga4_ai_referrals"
            f" WHERE project_id=? AND snapshot_date=? AND landing_page IN ({ph})",
            (pid, m["snapshot_date"], *sorted(paths))):
        row = out.setdefault(r["landing_page"], {"page": r["landing_page"], "sessions": 0,
                                                 "key_events": 0.0, "sources": {}})
        row["sessions"] += r["sessions"] or 0
        row["key_events"] = round(row["key_events"] + (r["key_events"] or 0), 2)
        row["sources"][r["source"]] = row["sources"].get(r["source"], 0) + (r["sessions"] or 0)
    return out


def _ai_referrals(conn, pid: int) -> dict:
    """AI 답변의 링크를 타고 들어온 방문(collect_ga4 의 부가 조회) — 최신으로 잰 날 한 벌.

    세 키는 늘 같이 움직인다:
      ai_referrals      출처(호스트)별 합계, 세션 내림차순
      ai_referral_pages 페이지(경로)별 합계와 그 페이지의 출처별 세션
      ai_referral_meta  잰 날·기간·그때 센 호스트 목록
    **None = 안 쟀다**(GA4 미연결이거나 이 조회가 생기기 전 수집, 또는 매번 실패),
    **[] = 쟀고 0**. 둘을 뭉치면 화면이 "GA4 를 연결하세요"와 "아직 아무도 안 왔다"를
    가를 수 없다. 기준은 ga4_ai_measured(쟀다는 표시)다 — 행이 없는 날도 거기엔 남는다.
    """
    m = conn.execute("SELECT snapshot_date, period_days, hosts_json FROM ga4_ai_measured"
                     " WHERE project_id=? ORDER BY snapshot_date DESC LIMIT 1", (pid,)).fetchone()
    if not m:
        return {"ai_referrals": None, "ai_referral_pages": None, "ai_referral_meta": None}
    rows = q(conn, "SELECT source, landing_page, sessions, key_events FROM ga4_ai_referrals"
                   " WHERE project_id=? AND snapshot_date=?", (pid, m["snapshot_date"]))
    by_src: dict[str, dict] = {}
    by_page: dict[str, dict] = {}
    for r in rows:
        s = by_src.setdefault(r["source"], {"source": r["source"], "sessions": 0, "key_events": 0.0})
        p = by_page.setdefault(r["landing_page"], {"page": r["landing_page"], "sessions": 0,
                                                   "key_events": 0.0, "sources": {}})
        for acc in (s, p):
            acc["sessions"] += r["sessions"] or 0
            acc["key_events"] += r["key_events"] or 0
        p["sources"][r["source"]] = p["sources"].get(r["source"], 0) + (r["sessions"] or 0)
    order = lambda xs: sorted(xs, key=lambda x: (-x["sessions"], -x["key_events"],
                                                 x.get("source") or x.get("page")))
    for x in (*by_src.values(), *by_page.values()):
        x["key_events"] = round(x["key_events"], 2)
    try:
        hosts = json.loads(m["hosts_json"] or "[]")
    except ValueError:
        hosts = []
    return {"ai_referrals": order(by_src.values()),
            # 페이지는 화면·요청문이 쓸 만큼만 — 긴 꼬리는 세션 1짜리가 수백 줄이다.
            "ai_referral_pages": order(by_page.values())[:100],
            "ai_referral_meta": {"date": m["snapshot_date"], "period_days": m["period_days"],
                                 "hosts": hosts}}


def _axis_backlinks(conn, pid: int) -> dict:
    """백링크 축 (collect_backlinks).

    요약만으로는 "무엇을 할지"가 안 나온다. 어느 페이지가 어떤 앵커로 받았는지,
    그리고 경쟁사는 받는데 우리는 못 받는 곳이 어디인지가 실제로 손댈 자리다.
    """
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
    return {"bl_date": bl_date, "bl_summary": bl_summary, "bl_domains": bl_domains,
            "bl_links": bl_links, "bl_anchors": bl_anchors, "bl_intersect": bl_intersect,
            "bl_trend": bl_trend}


def _axis_competitors(conn, pid: int) -> dict:
    """경쟁 분석 축 (collect_gap). 몫(share)은 저장하지 않는다 — 분모가 바뀌면 낡는다."""
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
    return {"comp_date": cm_date, "comp_metrics": comp_metrics,
            "gap_date": gap_date, "kw_gap": kw_gap, "kw_gap_counts": kw_gap_counts}


def _axis_ai_bots(p, crawl: dict) -> dict:
    """AI 크롤러가 robots.txt 에 막혔나. 새로 가져오지 않는다 — 크롤 회차가 남긴
    원문을 다시 읽을 뿐이다(판정의 정본은 scoring.robots_blocks).

    막힌 것만 내지 않고 허용까지 같이 낸다: 한 줄만 보여 주면 "이것만 열면 되나"
    로 읽히고, 같은 robots.txt 가 다른 봇에게 무엇을 하는지는 안 보인다. 행마다
    용도(purpose)를 싣는다 — 학습 봇 차단과 검색 봇 차단은 뜻이 반대라서.

    llms_txt: 같은 회차가 받은 /llms.txt. None = 모름(크롤이 안 받았거나 못 받음),
    {"found": False} = 봤고 없음. 둘을 뭉치면 요청문이 증거 없이 "없다" 고 말한다.

    crawl 은 _axis_crawl() 의 반환값 그대로다 — {"crawl": {"run": …}, "crawl_kinds": …}.
    예전에는 여기서 바로 .get("run") 을 해서 늘 빈 행이었다: 봇 표도, 인용 공백
    요청문의 '먼저 볼 것' 줄도 한 번도 실린 적이 없었다(test_dashboard 가 못 박는다).
    """
    run = ((crawl or {}).get("crawl") or {}).get("run") or {}
    found = run.get("llms_txt_found")
    llms = None if found is None else {"found": bool(found), "bytes": run.get("llms_txt_bytes"),
                                       "head": run.get("llms_txt_head")}
    home = f"https://{p['domain']}/" if p["domain"] else "https://example.com/"
    return {"ai_bots": scoring.ai_bot_status(run.get("robots_txt") or "", home),
            "llms_txt": llms}


def _axis_vitals(conn, pid: int) -> dict:
    """속도 축 (collect_vitals) — 최신 측정일의 페이지×기기 표.

    {url: {strategy: 행}} 로 접어 둔다. 요청문이 묻는 것이 늘 "이 페이지의, 이
    기기의" 값이라서다 — 모바일만 밀리는 검색어의 근거는 두 기기를 나란히 놓아야
    나온다.
    """
    d = conn.execute("SELECT MAX(checked_date) d FROM page_vitals WHERE project_id=?",
                     (pid,)).fetchone()["d"]
    if not d:
        return {"vitals_date": None, "vitals": {}}
    out: dict[str, dict] = {}
    for r in q(conn, "SELECT * FROM page_vitals WHERE project_id=? AND checked_date=?",
               (pid, d)):
        out.setdefault(r["url"], {})[r["strategy"]] = r
    return {"vitals_date": d, "vitals": out}


def _axis_crawl(conn, pid: int) -> dict:
    """사이트 크롤 축 (collect_crawl). 회차로 남기는 이유가 여기서 쓰인다: 지난번 대비 새로 깨진 것."""
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
    # 갈래 이름표는 만드는 쪽(collect_crawl)이 갖는다 — 화면 JS 안에 사본을 두면
    # 요청문은 그걸 못 읽어서 같은 갈래를 영어 kind 로 내보내게 된다.
    return {"crawl": crawl, "crawl_kinds": collect_crawl.ISSUE_KIND}


def _crawl_inlinks(conn, crawl: dict, urls) -> dict:
    """요청문이 손댈 페이지로 **들어오는** 내부 링크. 크롤 회차의 crawl_links 를 읽는다.

    page_audits 가 세는 internal_links 는 그 페이지가 **내보내는** 링크 수라 반대편이다.
    "어느 글에서 이 페이지로 링크를 걸지" 를 시키면서 지금 어디서 링크가 오는지를 안
    주면, AI 는 이미 링크가 있는 글을 또 제안한다.

    값이 없는 URL 은 키를 안 만든다 — 크롤이 안 본 주소를 "고아" 라고 부르지 않기
    위해서다(빈 리스트는 "보고 링크가 없었다"는 뜻으로 남겨 둔다).
    """
    run = (crawl or {}).get("run")
    want = {u for u in urls if u}
    if not (run and want):
        return {}
    by_norm = {}
    for u in want:
        by_norm.setdefault(scoring.norm(u), u)
    crawled = {scoring.norm(r["url"]) for r in conn.execute(
        "SELECT url FROM crawl_pages WHERE run_id=?", (run["id"],))}
    out = {by_norm[k]: [] for k in by_norm if k in crawled}
    if not out:
        return {}
    for r in conn.execute(
            "SELECT url_from, url_to, anchor FROM crawl_links"
            " WHERE run_id=? AND is_internal=1 AND url_from <> url_to LIMIT 20000",
            (run["id"],)):
        u = by_norm.get(scoring.norm(r["url_to"]))
        if u in out:
            out[u].append({"from": r["url_from"], "anchor": r["anchor"]})
    return {u: _inlink_rows(rows) for u, rows in out.items()}


# 들어오는 링크를 요청문에 싣는 상한(글 수). 여기서 20개로 자르고 요청문이 10개만 그리자
# "들어오는 내부 링크 20개"라는 제목 아래 표가 10줄이었고, 나머지 글에 또 걸자는 제안이
# 나왔다. 자르더라도 전체 수·앵커 분포는 잘리기 전 값으로 싣는다(_inlink_rows 의 첫 행).
INLINK_ROWS = 40


def _inlink_rows(rows: list[dict]) -> list[dict]:
    """글 하나에 한 줄로 접는다(메뉴·본문이 같은 글에서 두 번 걸어도 한 곳이다).

    잘리기 전의 total(링크 수)·pages(글 수)·anchors(앵커별 링크 수 상위 5)는 **첫 행에만**
    싣는다 — 페이로드 모양(url → 행 목록)을 안 바꾸고, 같은 값을 40번 싣지도 않는다.
    brief._site_facts 가 첫 행에서 읽는다."""
    if not rows:
        return []
    by_from: dict[str, dict] = {}
    anchors: dict[str, int] = {}
    for r in rows:
        a = " ".join(str(r.get("anchor") or "").split())
        anchors[a] = anchors.get(a, 0) + 1
        by_from.setdefault(r["from"], {"from": r["from"], "anchor": a})
    top = sorted(anchors.items(), key=lambda x: (-x[1], x[0]))[:5]
    meta = {"total": len(rows), "pages": len(by_from), "anchors": [list(x) for x in top]}
    out = list(by_from.values())[:INLINK_ROWS]
    out[0] = {**out[0], **meta}
    return out


def _site_probe(conn, crawl: dict, urls) -> dict:
    """이 주소가 robots.txt 에 막히나 · 사이트맵에 있나.

    색인 막힘 요청문이 매번 묻는 두 가지인데, 지금까지 근거는 구글 URL 검사 응답
    한 줄뿐이었다 — 원인 1순위 두 개를 안 주고 원인을 대라고 시킨 셈이다.

    주소마다 미리 재서 넣는다: robots.txt 원문과 사이트맵 목록을 통째로 페이로드에
    실으면 화면이 수천 줄을 짊어진다.
    """
    run = (crawl or {}).get("run")
    want = [u for u in urls if u]
    if not (run and want):
        return {}
    sm = {scoring.norm(r["url"]) for r in conn.execute(
        "SELECT url FROM sitemap_urls WHERE run_id=?", (run["id"],))}
    txt = run.get("robots_txt") or ""
    out = {}
    for u in want:
        out[u] = {"robots": scoring.robots_blocks(txt, u) if txt else None,
                  # None = 안 봤다(사이트맵이 시드가 아니었다), False = 보고 없었다
                  "in_sitemap": (scoring.norm(u) in sm) if sm else None}
    return out


def _aio_band_of(target: str, gap_band: dict, rank_pos: dict, gsc_pos: dict) -> str | None:
    """구글 AI 요약 기회의 갈래 — 요청문 근거(brief._ev_aio)가 순위를 읽는 순서 그대로.

    (1) 최신 회차의 AI 요약 빠짐 행(aio_gaps) → (2) 그 검색어의 최신 순위 행(ranks —
    순위 없음(None)도 "안 보였다"는 측정이라 beyond) → (3) GSC 평균 순위(query_pages).
    셋 다 없을 때만 None — kind_play 가 beyond 로 물러선다. 판정은 scoring.aio_band
    하나다. 예전엔 (1)뿐이라 최신 회차에 없는 옛 기회가 전부 "1페이지 밖"이 됐다 —
    근거표는 실측 5위라는데 처방은 순위가 먼저라고 말했다.
    """
    if target in gap_band:
        return gap_band[target]
    if target in rank_pos:
        return scoring.aio_band(rank_pos[target])
    if target in gsc_pos:
        return scoring.aio_band(gsc_pos[target])
    return None


def _axis_opps(conn, pid: int, at: str | None, striking: list[dict], kw_gap: list[dict],
               ai_rows: list[dict] = (), ranks: list[dict] = ()) -> dict:
    """기회 축 — striking(GSC 축)·kw_gap(경쟁 분석 축)·ai_rows(AI 축의 질문 행)·ranks
    (순위 축의 행, 화면용 30개로 자르기 전)가 낸 원본 행을 대상 문자열로 한 번만
    짝지어 라벨·처방·방어여부·GA4 보정을 입힌다. 화면은 그리기만 한다.
    """
    opps = scoring.opportunities(conn, pid, limit=200, with_id=True)

    # GA4 매출 잠재력 보정 배지 — scoring.load() 가 저장할 때 이미 score() 로 승수를
    # 반영해 놨다. 여기서는 화면이 "왜 이게 위로 왔는지" 말할 수 있게 같은 승수를
    # 다시 구해서 얹기만 한다(저장은 안 한다 — DB 스키마는 안 건드린다). 승수가
    # 1.0(GA4 없음·표본 미달·페이지 없는 kind)이면 키 자체를 안 붙인다 — 화면은
    # ga4_mult 유무로 배지를 켠다.
    cur, _prev, period, _mismatch = scoring.snapshot_pair(conn, pid, at)
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

    # 개수도 목록과 같은 문(심사 통과)을 센다 — 목록은 비었는데 "새 기회 546건"이라
    # 말하면 안 된다.
    opps_total = conn.execute(
        "SELECT COUNT(*) FROM opportunities WHERE project_id=? AND status='new' AND "
        + db.gate_sql(conn), (pid, *scoring.KEYWORD_KINDS)).fetchone()[0]

    # 라벨·처방·방어여부는 여기서 한 번 풀어 opps 에 싣는다 — 화면은 그리기만 한다.
    # striking_distance·content_gap 은 밴드/갈래로 처방이 갈리는데, 그 판정은 이미
    # striking()·content_gaps() 가 낸 원본 행(band/kind)에 있다 — 검색어·키워드
    # 문자열로 한 번만 짝짓는다(대상 문자열 자체가 판정하지 않는다).
    sd_band = {r["query"]: r["band"] for r in striking}
    gap_kind = {r["keyword"].strip().lower(): r["kind"] for r in kw_gap}
    # 구글 AI 요약 빠짐도 우리 순위로 처방이 갈린다(scoring._AIO_PLAY) — 원본 행은 그
    # 종류의 검출기(aio_gaps)가 낸 최신 회차다. 거기 없는 옛 기회는 순위 행·GSC 순위로
    # 물러선다(_aio_band_of) — 요청문 근거가 읽는 자리와 같다.
    aio_band = {r["keyword"]: scoring.aio_band(r["position"])
                for r in scoring.aio_gaps(conn, pid)}
    rank_pos = {r["keyword"]: r.get("pos") for r in ranks}
    # GSC 는 최신 회차에도 순위 행에도 없는 대상만 묻는다 — query_pages 와 같은
    # 함수·같은 스냅샷(at)이라 근거표의 페이지 순위와 어긋나지 않는다.
    asked = [o["target"] for o in opps if o["kind"] == "aio_exposure"
             and o["target"] not in aio_band and o["target"] not in rank_pos]
    # 밀면 오를 검색어도 같다: 최신 striking() 은 노출 순 15개로 잘려서, 거기서 빠진 열린
    # 기회는 밴드를 몰라 page2 처방("1페이지 진입까지 몇 칸")으로 떨어졌다 — 평균 3.6위인
    # 검색어에. 최신 GSC 순위로 밴드를 다시 가른다.
    asked += [o["target"] for o in opps if o["kind"] == "striking_distance"
              and o["target"] not in sd_band]
    gsc_pos: dict[str, float] = {}
    gsc_sum: dict[str, tuple[int, int]] = {}
    if asked:
        for qq, prows in scoring.pages_by_query(conn, pid, asked, at=at).items():
            rows = [p for p in prows if p.get("position") is not None]
            imp = sum(p.get("impressions") or 0 for p in rows)
            if rows:            # 노출 가중 평균 — GSC 가 검색어 순위를 내는 방식과 같다
                gsc_pos[qq] = (sum(p["position"] * (p.get("impressions") or 0) for p in rows) / imp
                               if imp else sum(p["position"] for p in rows) / len(rows))
                gsc_sum[qq] = (imp, sum(p.get("clicks") or 0 for p in rows))
    # 챗봇 인용 공백은 대신 인용된 곳의 갈래(scoring.ai_tally 의 lean)로 처방이 갈린다 —
    # 요청문 근거표와 같은 행에서 읽어야 표와 처방이 같은 말을 한다. 기회를 세운 행
    # (ai_gap_rows)이 먼저고, 거기 없는 질문만 최신 회차 행으로 물러선다(뒤가 이긴다).
    ai_lean = {str(r.get("prompt") or ""): r.get("lean") for r in ai_rows}
    sd_rows = {r["query"]: r for r in striking}
    sd_reason = scoring._KIND_BY_NAME["striking_distance"].reasoning
    for o in opps:
        o["is_defensive"] = scoring.is_defensive(o["kind"])
        if o["kind"] == "striking_distance":
            band = sd_band.get(o["target"]) or (
                ("page1" if gsc_pos[o["target"]] <= scoring.PAGE1 else "page2")
                if o["target"] in gsc_pos else None)
            # 적재 때 쓴 근거 문장은 그날의 수(6.3위·노출 39)라, 같은 요청문의 최신 표와
            # 어긋난다. 최신 행이 있으면 같은 문장 틀로 다시 쓴다 — 없으면 그날 날짜가
            # 박힌 옛 문장을 그대로 둔다(날짜가 이미 그렇다고 말한다).
            if "pos" in sd_rows.get(o["target"], {}):
                o["reasoning"] = sd_reason(sd_rows[o["target"]], {"cur": cur})
            elif o["target"] in gsc_pos and cur:
                # 최신 목록 밖(4~20위·노출 하한을 벗어났다) — 옛 문장("1페이지까지 0.0칸")이
                # 새 라벨("1페이지 상단 가능") 옆에 서지 않게 최신 수로 쓰고, 조건 밖이라고 밝힌다.
                p = round(gsc_pos[o["target"]], 1)
                imp, clk = gsc_sum[o["target"]]
                o["reasoning"] = (
                    f"평균 {p}위 · 노출 {imp:,} · 클릭 {clk:,}. 이번 구글 실적에서는 밀면 오를 "
                    f"검색어 조건({scoring.STRIKING_LO}~{scoring.STRIKING_HI}위 · 노출 "
                    f"{scoring.STRIKING_MIN_IMP} 이상) 밖입니다"
                    + (" — 이미 상단 3위권이라 남은 일은 클릭입니다" if p < scoring.STRIKING_LO else "")
                    + f" (구글 실적 {cur} 기준)")
        else:
            band = (_aio_band_of(o["target"], aio_band, rank_pos, gsc_pos)
                    if o["kind"] == "aio_exposure" else None)
        gk = (gap_kind.get(str(o["target"]).strip().lower()) if o["kind"] == "content_gap"
              else ai_lean.get(str(o["target"])) if o["kind"] == "ai_citation_gap" else None)
        o["label"] = scoring.kind_label(o["kind"], band=band)
        o["play"] = scoring.kind_play(o["kind"], band=band, gap_kind=gk)
        # 요청문(brief)이 꼴을 가를 때 다시 쓴다 — 여기서 한 번 판정한 것을 싣는다.
        o["band"], o["gap_kind"] = band, gk

    # 목록의 줄 — 같은 지면의 변형 검색어를 한 줄로 접은 것(scoring.group_opportunities).
    # opps 는 그대로 둔다: 다른 화면(순위·키워드)은 검색어 하나로 기회를 찾고(oppOf),
    # 요청문도 기회 하나마다 쓴다. 줄은 opps 의 id 를 가리키기만 한다.
    return {"opps": opps, "opps_total": opps_total,
            "opp_groups": scoring.group_opportunities(conn, pid, opps)}


def _cluster_keywords(conn, pid: int, opps: list[dict]) -> dict[str, list[dict]]:
    """coverage 기회의 근거 — 그 주제로 추적 중인 키워드와 검색량. 기회의 대상은
    'cluster:이름' 한 줄뿐이라, 요청문이 "무슨 키워드들이냐"를 말하려면 이것이 필요하다."""
    out: dict[str, list[dict]] = {}
    for o in opps:
        if o["kind"] != "coverage":
            continue
        cl = str(o["target"]).split(":", 1)[-1]
        if cl in out:
            continue
        out[cl] = q(conn,
            """SELECT keyword, volume FROM keywords
                WHERE project_id=? AND is_active=1 AND COALESCE(cluster,'(미분류)')=?
                ORDER BY volume IS NULL, volume DESC, keyword LIMIT 30""", (pid, cl))
    return out


def _axis_query_pages(conn, pid: int, p, at: str | None, *, opps: list[dict],
                      striking: list[dict], ranks_all: list[dict],
                      ups: list[dict], downs: list[dict]) -> dict:
    """행을 펼쳤을 때 보여줄 근거 — 그 검색어에 실제로 걸린 내 페이지들, 그리고 페이지
    감사. 기회·키워드·순위·움직인 검색어가 같은 한 벌을 본다(화면마다 다른 표를
    만들면 같은 검색어가 화면마다 다른 페이지를 말한다) — 그래서 이 축은 다른 축이
    이미 낸 결과(opps/striking/ranks_all/ups/downs)를 그대로 받는다.
    """
    query_pages = scoring.pages_by_query(conn, pid, [
        *(o["target"] for o in opps),
        *(r["query"] for r in striking),
        *(r["keyword"] for r in ranks_all),
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
            # 추출성은 AI 맥락에서만 뜻이 있다 — 일반 진단(advice)에 섞으면 모든 화면의
            # 진단표가 부푼다. 따로 싣고 [AI 인용] 화면만 진단표에 넘긴다. 챗봇 기준이다:
            # 그 화면은 챗봇 인용이고, 구글 AI 요약 기준 문구는 요청문이 따로 쓴다.
            a["extract_advice"] = scoring.extract_advice(a, "ai_citation_gap")
            page_audits[a["url"]] = a

    # 순위에 걸린 페이지가 없는 기회만 — 제목·H1 에 그 검색어가 있는 내 지면을 찾는다.
    # 이게 없으면 10위 밖 지면이 "페이지 없음 → 새 글"로 떨어진다(brief.page_of 가 읽는다).
    topic_pages = scoring.pages_by_topic(
        conn, pid, [o["target"] for o in opps if o["kind"] in scoring.KEYWORD_KINDS
                    and not query_pages.get(str(o["target"]))])

    # 한 페이지에 두 의도 — 판정을 여기서 다시 돌린다. query_pages 는 기회·순위의
    # 검색어만 싣기 때문에 그 페이지에 걸린 검색어 전부를 갖지 못한다: 요청문이 그걸로
    # 다시 세면 판정("해결 30")과 표(검색어 한 줄)가 어긋난다. 검출기가 낸 행을
    # 그대로 실어 양쪽이 같은 숫자를 말하게 한다.
    intent_splits = scoring.intent_split(conn, pid, at=at) if any(
        o["kind"] == "intent_split" for o in opps) else []
    # 펼침 패널이 대상 하나를 두고 말하는 수 — 지금 노출·클릭·CTR·순위와 그 추이.
    # query_pages 와 **같은 at** 으로 부른다(test_seams 47): 기준 수집일을 과거로
    # 고정했는데 여기만 최신을 보면, 한 패널 안에서 표와 차트가 다른 날을 말한다.
    target_trend = scoring.trend_by_target(conn, pid, [o["target"] for o in opps], at=at)

    # 이 페이지로 들어오는 검색어 — query_pages 를 뒤집어서는 못 센다. 그 표에는
    # 기회·순위에 걸린 검색어만 있어서 한 지면에 마흔 개가 들어와도 셋만 세고,
    # 화면은 그 셋을 "연관 검색어 전부"라고 말하게 된다.
    # 손댈 페이지는 brief.page_of 가 고르고(순위 → 주제 지면 순), 그 후보가 모두
    # 이 둘 안에 있다 — 여기서 따로 고르지 않고 둘을 합쳐 넘긴다.
    in_play = {*q_of_url, *(str(o["target"]) for o in opps
                            if str(o["target"]).startswith("http"))}
    in_play |= {r["page"] for rows in topic_pages.values() for r in rows if r.get("page")}
    page_queries = scoring.queries_by_page(conn, pid, in_play, at=at)

    return {"query_pages": query_pages, "page_audits": page_audits,
            "page_audit_date": audit_date, "topic_pages": topic_pages,
            "intent_splits": intent_splits, "target_trend": target_trend,
            "page_queries": page_queries}


def gather(conn, p, at: str | None = None) -> dict:
    """화면 하나가 쓰는 데이터 전부 — 라이브 대시보드와 박제 리포트가 같이 쓴다.

    각 축(_axis_*)이 자기 키 묶음을 내고, 여기서는 그 합집합만 한다 — 축끼리
    공유하는 중간값(스냅샷 짝 cur/prev/period 등)은 인자로 안 넘긴다. 필요한 축이
    scoring.snapshot_pair() 를 각자 다시 부른다(같은 conn/pid/at 이면 같은 값이라
    비용 말고는 잃는 게 없다). opps·query_pages 처럼 다른 축의 *결과 행*이 실제로
    필요한 경우만 그 축의 반환값을 인자로 받는다.

    at: 화면이 고정한 기준 수집일. GSC 스냅샷 축(KPI·추이·움직인 검색어·아깝다·
        근거 페이지)만 그날로 돌아간다. 순위·색인·기기·AI·기회는 수집 주기가 따로라
        각자 최신을 본다 — 그래서 화면 머리가 축별 날짜를 다 적는다. 한 날짜로
        묶으면 대부분의 축이 "그날 데이터 없음"이 된다.
    """
    pid = p["id"]
    cfg = collector.project_cfg(p["config_path"] or p["name"])

    gsc = _axis_gsc(conn, pid, cfg, at)
    rank = _axis_rank(conn, pid)
    ranks_all = rank["ranks"]                 # query_pages 근거용 — 자르기 전 전체
    rank["ranks"] = ranks_all[:30]            # 화면에 실리는 것은 30개까지

    ai = _axis_ai(conn, pid)
    comp = _axis_competitors(conn, pid)
    opps_d = _axis_opps(conn, pid, at, gsc["striking"], comp["kw_gap"],
                        ai["ai_by_prompt"] + ai["ai_gap_rows"], ranks=ranks_all)
    qp = _axis_query_pages(conn, pid, p, at, opps=opps_d["opps"], striking=gsc["striking"],
                           ranks_all=ranks_all, ups=gsc["ups"], downs=gsc["downs"])
    page_perf = _axis_page_perf(conn, pid)
    ga4 = _axis_ga4(conn, pid, at)
    bl = _axis_backlinks(conn, pid)
    crawl = _axis_crawl(conn, pid)
    vitals = _axis_vitals(conn, pid)
    ai_bots = _axis_ai_bots(p, crawl)

    runs = q(conn,
        """SELECT id, kind, started_at, finished_at, api_calls, cost_estimate_usd, notes
             FROM runs WHERE project_id=? ORDER BY id DESC LIMIT 40""", (pid,))
    # /create 가 실제로 고친 것 — 측정→기회→수정→재측정 루프가 닫혔음을 보여주는 자리.
    creations = q(conn,
        """SELECT c.id, c.kind, c.file_path, c.branch, c.note, c.merged,
                  c.created_at, c.opportunity_id, o.target opp_target
             FROM creations c LEFT JOIN opportunities o ON o.id=c.opportunity_id
            WHERE c.project_id=? ORDER BY c.id DESC LIMIT 50""", (pid,))

    # 박제본 호환 분기는 이 번호 하나로 한다 — 필드 유무를 검사하지 않는다
    d = {"schema": 1, "project": dict(p),
         **gsc, **rank, **ai, **opps_d, **qp, **page_perf, **ga4, **bl, **comp, **crawl,
         **vitals, **ai_bots,
         "runs": runs, "creations": creations,
         # kind → 한국어 라벨(밴드 없는 통칭) — [기록]처럼 kind 단위로만 아는
         # 자리, [개요] 필터 칩처럼 대상 없이 kind 만 아는 자리가 쓴다.
         "kind_labels": {k.name: k.label for k in scoring.KINDS},
         # kind → [화면 id, 섹션 id] — [개요] 기회 줄의 "자세히 보는 화면" 링크.
         # 따로 볼 화면이 없는 종류는 안 싣는다.
         "kind_views": {k.name: list(k.see) for k in scoring.KINDS if k.see},
         # 완료 후 관찰(db.watch_rows)과 심사 대상 종류 — [개요]가 그린다.
         "watch": db.watch_rows(conn, pid),
         "keyword_kinds": list(scoring.KEYWORD_KINDS),
         "progress": stage.progress(conn, pid),
         "guide": stage.state(conn, p, p["domain"] or ""),
         "cluster_keywords": _cluster_keywords(conn, pid, opps_d["opps"])}
    # 요청문은 맨 마지막이다 — 위 축이 낸 행(근거 표·페이지 감사)을 그대로 읽는다.
    # 화면이 보는 숫자와 요청문이 말하는 숫자가 같은 페이로드에서 나와야 한다.
    # 들어오는 내부 링크만 여기서 한 번 더 읽는다: 어느 페이지가 필요한지는 기회와
    # query_pages 가 정해져야 알 수 있고(brief.page_of 가 정본), 사이트 전체를 실으면
    # 페이로드가 링크 수만 명으로 부푼다.
    _pages_in_play = {brief.page_of(o, d) for o in (d.get("opps") or [])}
    d["crawl_inlinks"] = _crawl_inlinks(conn, d.get("crawl") or {}, _pages_in_play)
    d["site_probe"] = _site_probe(conn, d.get("crawl") or {}, _pages_in_play)
    d["ai_referrals_in_play"] = _ai_referrals_in_play(conn, pid, _pages_in_play)
    brief.attach(d, db.project_locale(p))
    return d


def payload(project: str, at: str | None = None) -> dict:
    conn = db.connect()
    try:
        p = db.get_project(conn, project)
        return gather(conn, p, at)
    finally:
        conn.close()


def remote_project(project: str) -> bool:
    """이 사이트는 호스팅이 갖고 있나 — 원격 판정은 이 함수 하나다.

    로컬 Handler 의 디스패치가 이걸 보고 자기 Brain 대신 서버로 넘긴다(프록시).
    판정 자체는 remote.owns 하나뿐이다 — 여기서 규칙을 다시 쓰지 않는다.
    """
    return bool(project) and remote.owns(project)


def list_projects() -> list[str]:
    """사이트 선택지 — 로컬 Brain 에 있는 이름 + 호스팅이 갖고 있는 이름.

    호스팅 사이트도 이 목록에 있어야 로컬 화면에서 고를 수 있다(고르면 Handler 가
    프록시로 넘긴다). 이름이 양쪽에 다 있으면 한 번만 나온다.
    """
    conn = db.connect()
    try:
        names = {r[0] for r in conn.execute("SELECT name FROM projects")}
    finally:
        conn.close()
    names |= {str(n) for n in ((remote.config() or {}).get("projects") or [])}
    return sorted(names)


def set_opp_status(body: dict) -> dict:
    """POST /api/opp 본체 — 로컬·호스팅이 이 함수 하나를 부른다.
    상태값 검증은 db.set_opportunity_status 가 한다(잘못되면 ValueError)."""
    conn = db.connect()
    try:
        return {"updated": db.set_opportunity_status(
            conn, int(body.get("id") or 0), body.get("status"))}
    finally:
        conn.close()


def record_creation_route(body: dict) -> dict:
    """POST /api/creation 본체 — 로컬·호스팅이 이 함수 하나를 부른다.

    개발 도구가 일을 끝내고 남기는 기록 창구다(요청문 꼬리의 `createdb.py done`
    이 원격 사이트면 이 경로로 온다). 기록은 남기고 상태는 진행 중(acked)이다 —
    완료는 대시보드의 완료 후 관찰을 보고 사람이 누른다.

    기회 번호가 그 사이트 것이 아니면 LookupError — Handler 와 호스팅 래퍼가
    404 로 옮긴다(남의 Brain 을 번호로 더듬는 일을 막는다).
    """
    conn = db.connect()
    try:
        pid = db.get_project(conn, str(body.get("project") or ""))["id"]
        oid = int(body.get("opportunity_id") or 0)
        kind = None
        if oid:
            row = db.get_opportunity(conn, oid, project_id=pid)
            if row is None:
                raise LookupError("기회를 찾을 수 없습니다")
            kind = row["kind"]
            db.set_opportunity_status(conn, oid, "acked", project_id=pid)
        cid = db.record_creation(conn, pid, str(body.get("path") or ""),
                                 opportunity_id=oid or None, kind=kind,
                                 branch=body.get("branch") or None,
                                 note=body.get("note") or None)
        return {"creation_id": cid, "status": "acked"}
    finally:
        conn.close()


def _brand_keys(conn, p) -> set[str]:
    """브랜드 힌트용 — 프로젝트 이름·별칭(brand_aliases)을 norm 한 것. yaml 이 없으면
    사이트 이름만. 자동 판정에는 안 쓴다 — 심사 화면이 칩 하나로 힌트만 준다."""
    cfg = {}
    if p["config_path"]:
        try:
            cfg = db.load_project_yaml(p["config_path"])
        except (db.ProjectConfigNotFound, ImportError):
            pass
    names = scoring.aliases_of({**cfg, "name": cfg.get("name") or p["name"]})
    return {k for k in (scoring.norm(a) for a in names) if k}


def triage_payload(project: str) -> dict:
    """GET /api/triage — 열린 기회를 정규화한 검색어(scoring.norm)로 묶는다. 행 하나가
    판정 단위다: 변형 수·걸린 종류·최고 점수·최근 GSC 클릭/노출·브랜드 힌트·판정.
    검색어가 아닌 종류(KEYWORD_KINDS 밖)는 심사에 안 오른다."""
    conn = db.connect()
    try:
        p = db.get_project(conn, project)
        pid = p["id"]
        vm = db.verdict_map(conn, pid)
        ph = ",".join("?" * len(scoring.KEYWORD_KINDS))
        rows = conn.execute(
            f"""SELECT norm(target) key, target, kind, score FROM opportunities
                 WHERE project_id=? AND status IN ('new','acked') AND kind IN ({ph})
                 ORDER BY score DESC, id""", (pid, *scoring.KEYWORD_KINDS)).fetchall()
        latest = conn.execute("SELECT MAX(snapshot_date) FROM gsc_snapshots WHERE project_id=?",
                              (pid,)).fetchone()[0]
        perf: dict[str, tuple[int, int]] = {}
        if latest:
            for r in conn.execute(
                    "SELECT norm(query) k, SUM(clicks) c, SUM(impressions) i FROM gsc_snapshots"
                    " WHERE project_id=? AND snapshot_date=? GROUP BY 1", (pid, latest)):
                perf[r["k"]] = (int(r["c"] or 0), int(r["i"] or 0))
        brands = _brand_keys(conn, p)
        groups: dict[str, dict] = {}
        for r in rows:
            g = groups.get(r["key"])
            if g is None:
                c, i = perf.get(r["key"], (0, 0))
                g = groups[r["key"]] = {
                    "key": r["key"], "label": r["target"].strip(), "variants": set(),
                    "kinds": [], "score": r["score"], "clicks": c, "impressions": i,
                    "brand": any(b in r["key"] for b in brands), "verdict": vm.get(r["key"])}
            g["variants"].add(r["target"].strip())
            if r["kind"] not in g["kinds"]:
                g["kinds"].append(r["kind"])
        out = []
        for g in groups.values():
            g["variants"] = len(g["variants"])
            g["labels"] = [scoring.kind_label(k) for k in g["kinds"]]
            g["score"] = round(g["score"], 1) if g["score"] is not None else None
            out.append(g)
        out.sort(key=lambda g: -(g["score"] or 0))
        counts = {"none": 0, "irrelevant": 0, "hold": 0, "work": 0}
        for g in out:
            counts[g["verdict"] or "none"] += 1
        return {"rows": out, "counts": counts}
    finally:
        conn.close()


def set_verdict(body: dict) -> dict:
    """POST /api/verdict 본체 — 로컬·호스팅이 같은 함수를 부른다. 값 검증은
    db.set_verdicts 가 한다(잘못되면 ValueError). verdict 가 비면 미판정으로 되돌린다."""
    conn = db.connect()
    try:
        pid = db.get_project(conn, str(body.get("project") or ""))["id"]
        return {"updated": db.set_verdicts(conn, pid, list(body.get("keys") or []),
                                           body.get("verdict") or None)}
    finally:
        conn.close()


# 전송 중립 route 표 — 원본 화면(shell+views)이 로컬·호스팅 둘 다에서 부르는 API 넷의
# 본체. 예전엔 두 서버가 이 넷을 각자 손으로 등록해서, 이음매 검사(test_seams
# #5)가 두 소스를 정규식으로 훑어 존재를 대조해야 했다. 이제 로컬 Handler 는 이 표를
# 그대로 조회해 등록·디스패치하고(do_GET/do_POST), 호스팅(server/app.py)도 자기
# 인증·세션 격리를 두른 뒤 같은 call 을 부른다 — stage.py 는 정규식 대신 이 표의
# 경로 집합을 본다.
#
# call(project, query, body) -> JSON 직렬화 가능한 값. query 는 parse_qs 를 이미
# 첫 값으로 편 문자열 dict, GET 이면 body 는 None.
#
# /api/projects 는 예외다: 존재는 이 표로 함께 못 박지만 몸은 다르다 — 호스팅은
# 사이트 소유를 Brain 동기화 여부와 무관하게 store.sites(등록 즉시 반영)로 판정해야
# 해서 여기 call 을 못 쓴다. server/app.py 가 자기 구현을 그대로 갖는다(그 자리
# 주석 참고).
ROUTES = {
    ("GET", "/api/data"):
        lambda project, query, body: payload(project, query.get("date") or None),
    ("GET", "/api/doctor"):
        lambda project, query, body: setup_state(project),
    ("POST", "/api/opp"):
        lambda project, query, body: set_opp_status(body),
    ("GET", "/api/projects"):
        lambda project, query, body: list_projects(),
    # 검색어 심사 — 화면 [심사]가 부른다. 본체는 위 두 함수 하나씩.
    ("GET", "/api/triage"):
        lambda project, query, body: triage_payload(project),
    ("POST", "/api/verdict"):
        lambda project, query, body: set_verdict(body),
    # 작업 기록 — 개발 도구가 일을 끝내고 남긴다(createdb.py done / sync).
    ("POST", "/api/creation"):
        lambda project, query, body: record_creation_route(body),
}

def _by_ok(r: dict) -> tuple[dict, int]:
    """[설정] 쓰기의 공통 꼴 — 본체가 {"ok": …} 로 답하고, 거절이면 400."""
    return r, (200 if r["ok"] else 400)


def _run_route(body: dict) -> tuple[dict, int]:
    """POST /api/setup/run — 이름이 ACTIONS 에 있어야 돈다(명령줄은 표가 갖는다).
    돌린 결과는 실패여도 200 이다: 실패 로그를 화면이 그대로 보여 준다."""
    if body.get("action") not in ACTIONS:
        return {"error": "unknown action"}, 400
    return run_action(body["action"]), 200


# 로컬 전용 route 표 — [설정] 화면(과 기회 카드의 도구 열기)이 부르는 /api/setup/*.
# 호스팅엔 이 경로가 없다(설정 화면 자체를 숨긴다) — test_seams 가 그 차이를 안다.
# ROUTES 와 같은 결이되 둘이 다르다: 원격 사이트여도 프록시하지 않고(이 PC 에 묻는
# 것이다), 상태 코드를 본체가 정한다 — call(project, query, body) -> (값, 코드).
#
# 경로 목록의 정본은 이 표 하나다. 예전엔 do_GET/do_POST 의 if 사슬과 LOCAL_ONLY_*
# 집합이 같은 목록을 두 벌 가져서, 새 경로를 한쪽에만 적으면 404 였다 — 이제
# LOCAL_ONLY_* 는 아래서 이 표로부터 뽑고, Handler 는 이 표만 조회한다.
LOCAL_ROUTES = {
    # 읽기 전용 — 레포 추론값 / 호스팅으로 넘길 링크 / 사이트별 로컬 폴더 + 후보
    ("GET", "/api/setup/prefill"):
        lambda project, query, body: (repo_prefill(), 200),
    ("GET", "/api/setup/carry"):
        lambda project, query, body: (carry_pack(project), 200),
    ("GET", "/api/setup/dirs"):
        lambda project, query, body: (setup_dirs(), 200),
    ("POST", "/api/setup/run"):
        lambda project, query, body: _run_route(body),
    ("POST", "/api/setup/keys"):
        lambda project, query, body: _by_ok(save_keys(body)),
    ("POST", "/api/setup/gsc-client"):
        lambda project, query, body: _by_ok(save_gsc_client(body)),
    ("POST", "/api/setup/project"):
        lambda project, query, body: _by_ok(create_project(body)),
    ("POST", "/api/setup/dir"):
        lambda project, query, body: _by_ok(setup_dir(body)),
    ("POST", "/api/setup/remote"):
        lambda project, query, body: _by_ok(setup_remote(body)),
    # 기회를 개발 도구로 연다 — 브라우저는 이 PC 의 프로세스를 못 띄우므로 호스팅에는
    # 이 경로가 없다(그 화면은 안내만 그린다).
    ("POST", "/api/setup/run-tool"):
        lambda project, query, body: _by_ok(run_tool(body)),
}
LOCAL_ONLY_GET = {path for method, path in LOCAL_ROUTES if method == "GET"}
LOCAL_ONLY_POST = {path for method, path in LOCAL_ROUTES if method == "POST"}
LOCAL_ONLY_PATHS = LOCAL_ONLY_GET | LOCAL_ONLY_POST
# 로컬 Handler 가 받는 API 경로 전부(공통 표 + 로컬 전용 표).
LOCAL_PATHS = {path for _, path in ROUTES} | LOCAL_ONLY_PATHS

# 호스팅 사이트라도 이 PC 가 답하는 경로. /api/doctor 는 [설정] 화면이 보는 진단 —
# "이 PC 에 무엇이 깔려 있나"를 묻는 것이라 서버에 물으면 남의 컴퓨터를 진단한다.
# /api/projects 는 애초에 사이트 이름을 안 받는다(list_projects 가 둘을 합친다).
NEVER_PROXY = {"/api/doctor", "/api/projects"}


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

    def _proxy(self, method: str, path: str, **kw) -> None:
        """호스팅 사이트의 요청은 서버가 답한다 — 응답 JSON 을 그대로 옮긴다.

        데이터가 서버 Brain 에 있어서 여기서 답하면 빈 화면이 된다. 예전엔
        호스팅 주소로 브라우저를 보냈지만(리다이렉트), 그러면 로컬에만 있는
        [설정]·실행 버튼이 사라진다 — 화면은 로컬 한 벌로 두고 데이터만 넘긴다.
        """
        try:
            return self._json(remote.api(method, path, **kw))
        except (Exception, SystemExit) as e:   # remote 는 거절을 SystemExit 로 낸다
            return self._json({"error": str(e)}, 502)

    def do_GET(self) -> None:  # noqa: N802 (http.server 규약)
        u = urlparse(self.path)
        if u.path == "/":
            return self._send(200, HTML, "text/html; charset=utf-8")
        query = {k: v[0] for k, v in parse_qs(u.query).items()}
        project = query.get("project", "")
        local = LOCAL_ROUTES.get(("GET", u.path))
        if local:   # 이 PC 가 답한다 — 프록시하지 않는다
            return self._json(*local(project, query, None))
        call = ROUTES.get(("GET", u.path))
        if not call:
            return self._send(404, b"not found", "text/plain")
        if u.path not in NEVER_PROXY and remote_project(project):
            return self._proxy("GET", u.path, params=query)
        try:
            return self._json(call(project, query, None))
        except db.ProjectNotFound as e:  # db.get_project는 미등록이면 ProjectNotFound
            return self._json({"error": str(e)}, 404)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        local = LOCAL_ROUTES.get(("POST", path))
        call = ROUTES.get(("POST", path))
        if not (local or call):
            return self._send(404, b"not found", "text/plain")
        # 토큰은 로컬 전용 경로에도 똑같이 건다 — pip 실행·파일 쓰기가 여기 있다.
        if self.headers.get("X-Token") != TOKEN:
            return self._json({"error": "이 창은 만료됐습니다 — 대시보드를 다시 띄워 주세요."},
                              403)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return self._json({"error": "bad json"}, 400)

        if local:
            return self._json(*local("", {}, body))
        if path not in NEVER_PROXY and remote_project(str(body.get("project") or "")):
            return self._proxy("POST", path, json=body)
        try:
            return self._json(call("", {}, body))
        except db.ProjectNotFound as e:
            return self._json({"error": str(e)}, 404)
        except LookupError as e:   # record_creation_route: 그 사이트 기회가 아니다
            return self._json({"error": str(e)}, 404)
        except ValueError as e:
            return self._json({"error": str(e)}, 400)

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
    for left in ("<!--MANIFEST-->", "<!--VENDOR-->", "<!--VIEWS-->", "<!--SECTIONS-->"):
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


# ── 기회 카드에서 개발 도구 열기 ─────────────────────────────────────────────
# 기회의 끝은 여태 "요청문 복사 → AI 에 붙여 넣기"였다. 사용자가 원하는 것은 그
# 자리에서 자기 도구가 **그 사이트 폴더에서** 열리고 요청문이 이미 들어가 있는
# 것이다. 그 일을 여기서 한다: 요청문을 파일로 남기고, 고른 도구의 명령을 조립하고,
# 터미널 하나를 띄운다.
#
# 도구 목록·실행 파일·argv 꼴의 정본은 doctor.TOOLS 한 벌이다 — 이 파일에 도구
# 이름을 적지 않는다(적는 순간 표가 두 벌이 되고 한쪽만 낡는다). 터미널을 무엇으로
# 볼지도 doctor.usage() 가 이미 답한다(안 골랐으면 Orca 감지 → 없으면 시스템).
#
# 브라우저는 이 PC 의 프로세스를 못 띄운다 — 그래서 이 경로는 로컬 전용이고
# (LOCAL_ROUTES), 호스팅 화면은 "이 PC 에서 열기" 안내만 그린다.

def _work_dir(project: str) -> Path:
    """도구를 열 폴더. 설정에 적어 둔 것([설정]의 사이트별 로컬 폴더)이 정본이고,
    없으면 작업 자리(~/.capture/work/<사이트>/)를 만들어 거기서 연다.

    적어 둔 값이 지금 폴더가 아니면 없는 것으로 친다 — 저장 시점에 한 번 막지만
    그 사이에 지워질 수 있고, 없는 폴더로 열면 도구가 그때 가서 죽는다.
    """
    d = paths.site_dirs().get(project)
    if d and Path(d).is_dir():
        return Path(d)
    w = paths.home() / "work" / project
    w.mkdir(parents=True, exist_ok=True)
    return w


def _tool_argv(tool: tuple, prompt: str) -> list[str]:
    """doctor.TOOLS 의 argv 꼴에서 {prompt} 자리를 채운다.

    도구마다 다른 것은 이 한 줄뿐이다 — 스킬(`/create run`)을 부르지 않으므로
    도구별 갈래가 코드에 생기지 않는다.
    """
    return [a.replace("{prompt}", prompt) for a in tool[3]]


def _open_terminal(argv: list[str], cwd: Path, terminal: str, title: str) -> dict:
    """창 하나를 띄운다. 돌려주는 것은 {"terminal": …} (물러났으면 fallback 도).

    Orca 는 워크트리 안에서만 창을 만든다 — 폴더가 워크트리가 아니면 실패하는데,
    그때 사용자에게 남길 것은 오류 메시지가 아니라 **열린 창**이다. 시스템
    터미널로 물러나고 무엇으로 열었는지만 응답에 싣는다(화면이 그걸 말한다).
    """
    import shlex
    line = (subprocess.list2cmdline(argv) if sys.platform == "win32"
            else shlex.join(argv))
    if terminal == "orca":
        # orca 호출법은 doctor.orca_json 한 자리다 — 없거나 실패하면 None 이다.
        if doctor.orca_json("terminal", "create", "--worktree", f"path:{cwd}",
                            "--title", title, "--command", line, "--focus",
                            timeout=15) is not None:
            return {"terminal": "orca"}
        _system_terminal(argv, line, cwd)
        return {"terminal": "system", "fallback": "system"}
    _system_terminal(argv, line, cwd)
    return {"terminal": "system"}


def _system_terminal(argv: list[str], line: str, cwd: Path) -> None:
    """OS 가 기본으로 주는 터미널에서 argv 를 연다 — 창은 열린 채로 남는다."""
    if sys.platform == "win32":
        # 첫 "" 는 start 의 창 제목 자리다. 빼면 argv[0] 을 제목으로 먹고 아무것도
        # 안 뜬다(경로에 공백이 있을 때 특히).
        subprocess.Popen(["cmd", "/c", "start", "", *argv], cwd=str(cwd))
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-a", "Terminal", str(cwd)])
        script = line.replace("\\", "\\\\").replace('"', '\\"')
        subprocess.Popen(["osascript", "-e",
                          f'tell application "Terminal" to do script "{script}" in front window'])
    else:
        subprocess.Popen(["x-terminal-emulator", "-e", *argv], cwd=str(cwd))


def run_tool(body: dict) -> dict:
    """POST /api/setup/run-tool 본체 — 기회 하나를 도구로 연다.

    본문은 {project, id, ids?, dry_run?}. ids 는 [개요]의 묶인 줄(opp_groups)이 싣는
    묶음 전체다 — 창과 파일은 대표(id) 하나로 열고, '작업 시작'은 묶인 id 전부에
    찍는다. 상태 버튼(setOpps)은 이미 그렇게 하는데 열기만 대표 하나를 바꾸면 그
    줄이 새로고침 뒤 두 줄로 갈라진다. 그 사이트 것이 아닌 id 는 조용히 버린다 —
    열기를 그것 때문에 실패시키지 않는다.

    dry_run 이면 창도 안 띄우고 상태도 안 바꾸고 조립한 것만 돌려준다(검사용).
    실패는 전부 {"ok": False, "error": …} 이고 그때는 상태를 건드리지 않는다 —
    "작업 시작"이라고 표시해 놓고 아무 창도 안 뜨는 것이 제일 나쁜 결과다.
    """
    import shutil
    project = str(body.get("project") or "").strip()
    try:
        opp_id = int(body.get("id") or 0)
    except (TypeError, ValueError):
        opp_id = 0
    if not project or not opp_id:
        return {"ok": False, "error": "어느 사이트의 어느 기회인지 못 받았습니다."}
    extra: list[int] = []
    for x in (body.get("ids") if isinstance(body.get("ids"), (list, tuple)) else ()):
        try:
            extra.append(int(x))
        except (TypeError, ValueError):
            pass

    use = doctor.usage()
    tool = doctor.tool_of(use.get("tool") or "")
    if not tool:
        return {"ok": False, "error": "쓸 도구를 아직 안 골랐습니다. [설정]의 "
                                      "'쓰는 방식'에서 하나 고르면 여기에 열기 버튼이 섭니다."}
    if not shutil.which(tool[2]):
        return {"ok": False, "error": f"{tool[1]} 을(를) 이 PC 에서 못 찾았습니다. "
                                      f"설치하시거나 [설정]에서 다른 도구를 고르면 됩니다."}

    # 요청문은 서버가 이미 써 둔 것을 그대로 쓴다(brief.attach) — 여기서 다시 짓지
    # 않는다. 원격 사이트의 기회는 호스팅이 갖고 있으므로 거기서 받아온다.
    try:
        data = (remote.api("GET", "/api/data", params={"project": project})
                if remote.owns(project) else payload(project))
    except db.ProjectNotFound as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:                      # 원격이 죽었거나 토큰이 끊겼거나
        return {"ok": False, "error": f"기회를 불러오지 못했습니다: {e}"}
    opps = data.get("opps") or []
    opp = next((o for o in opps if str(o.get("id")) == str(opp_id)), None)
    if not opp:
        return {"ok": False, "error": "그 기회를 못 찾았습니다. [새로고침] 뒤 다시 눌러 주세요."}
    # 묶인 id 는 이 사이트의 기회 목록에 있는 것만 — 남의 번호·낡은 번호는 버린다.
    known = {str(o.get("id")) for o in opps}
    ids = [opp_id] + [i for i in dict.fromkeys(extra) if i != opp_id and str(i) in known]

    # 요청문 전문 = 본문 + 꼴 꼬리(답의 형식·규칙). 화면의 복사 버튼(briefText)과
    # brief.text() 가 같은 글을 만든다 — 꼬리는 사이트마다 한 벌(data.brief.tails)이라
    # 기회 안(o.brief)에는 없다. 본문만 쓰면 도구가 형식·규칙 없이 시작한다(그랬다).
    b = opp.get("brief") or {}
    if b.get("body"):
        tails = (data.get("brief") or {}).get("tails") or {}
        text = b["body"] + "\n" + (tails.get(b.get("shape")) or "")
    else:
        text = opp.get("reasoning") or ""
    text = text.strip()
    if not text:
        return {"ok": False, "error": "이 기회의 요청문이 아직 없습니다."}

    # 파일은 늘 작업 자리에 남긴다 — 사용자의 리포 안에 남의 파일을 떨구지 않는다.
    box = paths.home() / "work" / project
    box.mkdir(parents=True, exist_ok=True)
    md = box / f"opp-{opp_id}.md"
    createdb = Path(__file__).resolve().parents[2] / "create" / "scripts" / "createdb.py"
    # 요청문의 답은 제안서 HTML 한 장이다 — 그것만 만들었으면 채울 '바꾼 파일'이 없다.
    # 기록은 제안을 실제로 적용해 파일이 바뀐 뒤의 일이라고 조건을 먼저 말한다. 명령의
    # 경로는 이 PC 의 것이라 다른 곳(클라우드)에서 도는 도구는 건너뛴다.
    md.write_text(
        text
        + "\n\n---\n기록 (이 PC 에서 도는 도구만):\n"
        # 설계도·제안서만 낸 답에는 채울 '바꾼 파일'이 없다. 다만 같은 대화가 이어서
        # 본문·패치까지 쓰는 요청문이 있어(새 글 설계 → 승인 → 본문) "제안서만"이 언제
        # 끝나는지를 같이 적는다 — 안 적으면 파일을 바꾸고도 기록이 안 남는다.
        + "- 설계도·제안서까지만 냈으면 기록하지 않습니다 — 채울 '바꾼 파일'이 없습니다.\n"
        + "- 같은 대화에서 이어서 파일을 바꿨으면(승인 뒤 본문·패치를 넣은 경우 포함)\n"
        + "  그 시점에 기록합니다(바꾼 파일·브랜치를 채워서):\n"
        # 숫자만 있으면 그게 무엇인지 알 수 없고(실제로 "156이 뭐냐"는 물음이 왔다),
        # 경로는 플러그인 버전이 박혀 있어 다음 릴리스에 깨진다. 둘 다 그렇다고 적는다.
        # 코드 울타리가 없으면 마크다운으로 렌더될 때 `\.claude` 의 역슬래시가 먹혀
        # 경로가 `C:\Users\user.claude\…` 로 깨진 채 복사된다 — 실제로 그렇게 나갔다.
        + "```\n"
        + f'python "{createdb}" done {project} {opp_id} --path <바꾼 파일> --branch <브랜치>\n'
        + "```\n"
        + f"  · {opp_id} 는 이 기회의 번호입니다(요청문 파일 이름 opp-{opp_id}.md 와 같은 번호).\n"
        + "  · 위 경로에는 지금 설치된 플러그인 버전이 박혀 있습니다 — 안 맞으면 그 자리에\n"
        + "    설치된 seo-miner 의 skills/create/scripts/createdb.py 를 쓰세요.\n"
        + "- 이 명령이 없는 곳(원격·클라우드)에서 돌고 있으면 건너뜁니다.\n",
        "utf-8")

    cwd = _work_dir(project)
    argv = _tool_argv(tool, f"이 파일의 요청문대로 진행해 주세요: {md}")
    terminal = use.get("terminal") or "system"
    out = {"ok": True, "cwd": str(cwd), "file": str(md), "argv": argv,
           "terminal": terminal, "ids": ids}
    if body.get("dry_run"):
        return out

    out.update(_open_terminal(argv, cwd, terminal, f"seo-miner · {project} #{opp_id}"))
    # 창이 실제로 뜬 뒤에 '작업 시작'으로 바꾼다 — 묶인 id 전부. 기록은 서버 한 곳에
    # 남아 두 화면이 같은 표를 본다 — 원격 사이트면 호스팅의 /api/opp 로 보낸다
    # (그 창구는 id 하나씩 받는다 — setOpps 와 같이 하나씩 보낸다).
    conn = None if remote.owns(project) else db.connect()
    try:
        for oid in ids:
            try:
                if conn is None:
                    remote.api("POST", "/api/opp",
                               json={"project": project, "id": oid, "status": "acked"})
                else:
                    db.set_opportunity_status(conn, oid, "acked")
            except Exception:   # 창은 이미 떴다 — 상태 하나 때문에 실패로 되돌리지 않는다
                out["status_failed"] = True
    finally:
        if conn is not None:
            conn.close()
    return out


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

    # 원격 사이트도 여기서 띄운다 — Handler 가 그 사이트의 API 를 서버로 넘긴다
    # (remote_project → _proxy). 예전엔 호스팅 주소로 브라우저를 보냈는데, 그러면
    # 로컬에만 있는 [설정]·개발 도구 실행 버튼이 통째로 사라졌다.

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
