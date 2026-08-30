"""seo-miner 호스팅 SaaS — FastAPI 웹 서버.

테넌트 격리는 store.tenant() 가 env(CAPTURE_HOME, GSC_TOKEN_FILE) 를 갈아끼우는 것으로
끝난다. 엔진은 그 값을 호출할 때마다 다시 읽는다(db.py __getattr__) — 그래서 엔진 코드는
한 줄도 안 건드린다.
"""
from __future__ import annotations

import os
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# server/ 도 넣는다 — uvicorn server.app:app 로 뜨면 CWD 만 sys.path 에 있어서
# import store 가 깨진다(python server/app.py 로 직접 돌릴 때만 우연히 된다).
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(ROOT / "skills" / "capture" / "scripts"))

import asyncio
import html
import json
import re
import secrets
import sqlite3
from contextlib import asynccontextmanager
from typing import Optional
from urllib.parse import quote, urlparse

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.middleware.sessions import SessionMiddleware

import backlinks
import collect_ga4
import collect_gsc
import dashboard
import db
import doctor    # setup 스킬의 진단 — dashboard 가 이미 skills/setup/scripts 를 sys.path 에 얹는다
import exports
import gen_prompts
import gh
import identity
import pages
import run_all
import scheduler
import settings
import stage
import store
import writer


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(scheduler.loop())
    yield
    task.cancel()


# 고정 기본값을 두면 프로덕션에서 그대로 떠서 세션을 위조당한다. 없으면 랜덤 —
# 재시작 때 세션이 끊길 뿐이고, 끊기는 게 위조당하는 것보다 낫다.
# 다른 설정과 달리 여기만은 import 시점에 얼린다 — 미들웨어에 한 번 넘기면 못 바꾼다.
SESSION_SECRET = settings.get("SESSION_SECRET") or secrets.token_urlsafe(32)

app = FastAPI(lifespan=lifespan)
app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    https_only=settings.get("OAUTH_REDIRECT_URI").startswith("https://"),
)


@app.exception_handler(identity.NotConfigured)
def _not_configured(request: Request, e: identity.NotConfigured):
    """로그인 설정이 없는 배포. 문구는 유저에게 그대로 보여 준다 — 500 페이지만 뜨면
    운영자도 뭐가 빠졌는지 모른다.

    상태코드는 빠진 설정이 required 냐로 가른다. 구글 로그인(GOOGLE_CLIENT_ID 등)은
    required — 없으면 이 배포 자체가 고장난 것이니 500. GitHub 연동은 optional —
    없어도 나머지는 다 돌아가는, 그냥 안 켠 기능이니 501(Not Implemented)이다."""
    required = settings.SETTINGS[e.name].required
    return JSONResponse({"detail": str(e)}, status_code=500 if required else 501)


def _uid(request: Request) -> Optional[int]:
    return request.session.get("uid")


def _require_uid(request: Request) -> int:
    uid = _uid(request)
    if uid is None:
        raise HTTPException(status_code=401, detail="로그인이 필요합니다. 처음 화면에서 구글 계정으로 로그인해 주세요.")
    return uid


def _html(body: str, title: str = "seo-miner") -> HTMLResponse:
    return HTMLResponse(pages.document(body, title))


# 워커 실행 진입점 — 직접 부르지 않고 Depends 로 받는다. app.dependency_overrides 로
# demo() 가 실제 subprocess 를 안 띄우고 갈아끼울 수 있게(globals() 수술 대신).
def _dispatch_dep():
    return scheduler.dispatch


def _kick_dep():
    return scheduler.kick


# 로컬 플러그인이 링크로 실어 보낸 사이트 설정 중, 등록에 실제로 얹는 것.
# 이름·도메인·속성은 서버가 정한 것이 이긴다(슬러그 충돌·표기 검증). 종류(type)도
# 여기 없다 — 화면이 carry 값으로 미리 골라 두므로 types 에 담겨 오고, 사용자가
# 바꿨으면 그쪽이 이긴다. 여기 남는 건 사람이 정했고 기계가 못 짓는 것뿐이다.
# 이름의 정본은 dashboard.PREFILL_KEYS 이고 stage._check_seams 가 대조한다.
CARRY_FIELDS = ("locale", "brand_aliases", "seed_keywords",
                "competitors_manual", "tools")


def _capped(ids: list[int], limit: int, active: int) -> list[int]:
    """상한 안에서만 켠다 — SERP·AI 인용은 켜진 항목 수만큼 과금이라 상한 너머로
    켜면 그만큼 비용이 샌다. AI 질문·키워드 두 자리가 같은 규칙을 썼다."""
    return ids[:max(0, limit - active)]


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/")
def home(request: Request):
    # 로컬 플러그인이 실어 보낸 사이트 설정(?carry=). 로그인 전에 눌러도 잃지 않게
    # 세션에 둔다 — 구글 로그인이 이 화면을 한 번 떠났다 돌아오기 때문이다.
    # 정본은 dashboard.carry_pack/carry_read 다. 여기서 형식을 다시 정하지 않는다.
    if request.query_params.get("carry"):
        request.session["carry"] = request.query_params["carry"][:8000]
    carry = dashboard.carry_read(request.session.get("carry", ""))

    uid = _uid(request)
    if uid is None:
        return _html(pages.page("landing.html"), "seo-miner — 검색·AI 답변 가시성 추적")
    with store.session(uid) as conn:
        rows = store.sites(conn, uid)
    # create_project 는 name 만 정규식으로 검증한다 — domain·gsc_property 는 그대로
    # 저장되므로 <script> 가 들어올 수 있다.
    e = html.escape
    if rows:
        # 등록 직후에는 볼 데이터가 없다. 무슨 일이 일어나는 중인지 말해 주지 않으면
        # 빈 대시보드만 보고 떠난다.
        # 링크에 사이트 이름을 실어 보낸다 — 대시보드는 location.hash 로만 어느
        # 사이트를 열지 안다(dashboard.html 의 loadProjects). 여태 전부 "/d" 라
        # hash 가 비었고, 그러면 <select> 기본값인 첫 옵션이 잡혀서 무엇을 눌러도
        # 맨 처음 등록한 사이트가 열렸다. quote 는 % 인코딩만 남기므로 속성에 안전하다.
        items = "".join(
            f'<li><a href="/d#{quote(str(r["project"]), safe="")}">'
            f'<span class="nm">{e(r["project"])}</span>'
            f'<span class="pr">{e(r["gsc_property"])}</span>'
            + ('<span class="st wait">첫 분석 진행 중</span>' if not r["last_run_at"]
               else f'<span class="st">{e(str(r["last_run_at"])[:16])} 분석</span>')
            + '</a></li>' for r in rows)
        block = f'<section><p class="label">분석 중인 사이트</p><ul class="sites">{items}</ul>'
        if any(not r["last_run_at"] for r in rows):
            block += ('<p class="wait-note">검색 실적 · 색인 생성 · 키워드 순으로 몇 분에 '
                      '걸쳐 수집됩니다. 창을 닫아도 계속 진행됩니다.</p>')
        block += "</section>"
    else:
        block = ""
    who = html.escape(str(request.session.get("email") or ""))
    user_bar = (f'<span class="who">{who}</span>'
                '<form method="post" action="/auth/logout" class="lo">'
                '<button type="submit">로그아웃</button></form>') if who else ""
    doc = pages.fill(pages.page("app.html"), USER=user_bar, SITES=block)
    doc = pages.data(doc,
                     __TAKEN__=[r["gsc_property"] for r in rows],
                     __SITES__=[{"project": r["project"], "repo": r["repo"]} for r in rows],
                     __CARRY__=carry,
                     # 단계 이름표는 한 벌이다 — app.html 도 사본을 안 갖는다
                     # (대시보드가 window.__STAGES__ 로 받는 것과 같은 표다).
                     __STAGES__=stage.STAGE_LABELS)
    return _html(doc, "사이트 관리 — seo-miner")


def _begin(request: Request, provider: str) -> RedirectResponse:
    url, carry = identity.start(provider)
    request.session[identity.session_key(provider)] = carry
    return RedirectResponse(url, status_code=302)


def _carry(request: Request, provider: str, state: str) -> dict:
    carry = identity.carried(request.session, provider, state)
    if carry is None:
        raise HTTPException(status_code=400, detail="로그인 정보가 만료됐습니다. 처음 화면에서 다시 로그인해 주세요.")
    return carry


@app.get("/auth/login")
def auth_login(request: Request):
    return _begin(request, "google")


@app.get("/auth/callback")
def auth_callback(request: Request, code: str, state: str):
    acct = identity.finish("google", code, _carry(request, "google", state))
    conn = store.connect()
    try:
        uid = identity.remember(conn, "google", acct)
    finally:
        conn.close()
    request.session["uid"] = uid
    request.session["email"] = acct.who
    return RedirectResponse("/", status_code=302)


@app.post("/auth/logout")
def logout(request: Request):
    """세션만 끊는다. 저장된 구글·GitHub 토큰은 남겨 둔다 — 다시 로그인하면
    그대로 이어 쓴다(계정을 바꿔 가며 쓰라는 게 이 버튼의 목적이다).

    GET 이 아니라 POST 다 — 링크였다면 브라우저 prefetch 나 남이 심은 이미지 태그로
    의도치 않게 로그아웃된다.
    """
    request.session.clear()
    return RedirectResponse("/", status_code=302)


@app.get("/api/properties")
def api_properties(request: Request):
    """서치콘솔 속성 목록 — 사이트를 등록할 때 고르는 자리.

    /api/ga4/properties 와 같은 이유로 부르기 전에 연결 상태를 본다:
    collect_gsc.get_credentials() 는 CLI 전제라 토큰이 없으면 대화형 로그인을
    시도하고(서버에는 브라우저가 없다) 수단 자체가 없으면 sys.exit 한다.
    """
    uid = _require_uid(request)
    with store.session(uid, isolate=True) as conn:
        if not db.gsc_connected():
            raise HTTPException(
                status_code=403,
                detail="구글 계정이 아직 연결되지 않았습니다 — "
                       "다시 구글 계정으로 로그인해 주세요.")
        try:
            res = collect_gsc.get_service().sites().list().execute()
        except HTTPException:
            raise
        except BaseException:      # SystemExit 도 잡는다 — 스택트레이스가 나가면 안 된다
            raise HTTPException(
                status_code=502,
                detail="서치콘솔 속성 목록을 가져오지 못했습니다. "
                       "잠시 후 다시 시도해 주세요.")
    return {"properties": [{"property": s["siteUrl"], "level": s["permissionLevel"]}
                           for s in res.get("siteEntry", [])]}


def _slug(host: str, taken: set) -> str:
    """도메인에서 프로젝트 이름을 짓는다 — 마케터에게 물어볼 일이 아니다(내부 파일명이다)."""
    base = re.sub(r"[^A-Za-z0-9_-]", "", host.split(".")[0])[:36] or "site"
    if not re.match(r"[A-Za-z0-9]", base):
        base = "s" + base
    name, n = base, 2
    while name in taken:
        name, n = f"{base}-{n}", n + 1
    return name


def _host_of(prop: str) -> str:
    if prop.startswith("sc-domain:"):
        return prop[10:]
    try:
        return urlparse(prop).hostname.removeprefix("www.")
    except Exception:
        return ""


@app.post("/api/sites")
async def api_sites(request: Request, kick=Depends(_kick_dep)):
    """속성 여러 개를 한 번에 등록한다. 이름·도메인은 속성에서 짓고, 종류만 받는다."""
    uid = _require_uid(request)
    body = await request.json()
    props = body.get("properties") or []
    if isinstance(props, str):
        props = [props]
    if not props:
        raise HTTPException(status_code=400, detail="분석할 사이트를 하나 이상 선택해 주세요.")
    types = body.get("types") or {}

    # 로컬에서 넘어온 설정은 그 속성 하나에만 얹는다 — 한 번 쓰면 세션에서 뺀다
    # (다음에 다른 사이트를 등록할 때 남의 씨앗이 섞이면 안 된다).
    carry = dashboard.carry_read(request.session.pop("carry", ""))
    carry_prop = carry.get("gsc_property", "")

    added, failed = [], []
    with store.session(uid, isolate=True) as conn:
        taken = {r["project"] for r in store.sites(conn, uid)}
        for prop in props[:20]:
            host = _host_of(str(prop))
            if not host:
                failed.append({"property": prop, "error": "도메인을 알 수 없습니다"})
                continue
            name = _slug(host, taken)
            f = {
                "name": name, "type": types.get(prop, "saas"), "domain": host,
                "gsc_property": prop, "locale": body.get("locale", "ko-KR"),
                "brand_aliases": host.split(".")[0], "seed_keywords": "",
                "competitors_manual": "",
            }
            if prop == carry_prop:
                f.update({k: carry[k] for k in CARRY_FIELDS if carry.get(k)})
            r = dashboard.create_project(f)
            if r.get("ok"):
                taken.add(name)
                added.append({"project": name, "property": prop})
            else:
                failed.append({"property": prop, "error": r.get("error", "등록 실패")})
        for a in added:
            store.add_site(conn, uid, a["project"], a["property"], _host_of(a["property"]))

    if added:
        kick()
    return {"ok": bool(added), "added": added, "failed": failed}


# --- GitHub (/create) ---------------------------------------------------------

@app.get("/auth/github")
def gh_login(request: Request):
    _require_uid(request)
    return _begin(request, "github")


@app.get("/auth/github/callback")
def gh_callback(request: Request, code: str, state: str):
    uid = _require_uid(request)
    acct = identity.finish("github", code, _carry(request, "github", state))
    with store.session(uid) as conn:
        identity.remember(conn, "github", acct, uid=uid)
    return RedirectResponse("/", status_code=302)


def _gh_token(conn, uid: int) -> str:
    got = store.github(conn, uid)
    if not got:
        raise HTTPException(status_code=428, detail="GitHub 계정을 먼저 연결해 주세요. 사이트 화면에서 연결할 수 있습니다.")
    return got[0]


@app.get("/api/repos")
def api_repos(request: Request):
    uid = _require_uid(request)
    try:
        with store.session(uid) as conn:
            return {"repos": gh.repos(_gh_token(conn, uid))}
    except gh.GitHubError as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/api/repo")
async def api_repo(request: Request):
    """사이트에 리포를 붙인다. 관례 발견은 첫 글쓰기 때 한 번 한다."""
    uid = _require_uid(request)
    b = await request.json()
    project, repo, branch = str(b.get("project") or ""), str(b.get("repo") or ""),         str(b.get("branch") or "main")
    with store.session(uid, project) as conn:
        store.set_repo(conn, uid, project, repo, branch)
    return {"ok": True}


def _create_content(conn, uid: int, project: str, opp_id: int, row) -> dict:
    """/api/create 워크플로: 기회 조회 → 리포 프로필 → 글 작성 → PR → 기록.

    row 는 저장소가 연결된 store.site() 결과. isolate=True 세션(tenant) 안에서
    불러야 한다 — db.* 는 Brain(tenant) 을, store.* 는 서버 DB 를 함께 쓴다.
    """
    token = _gh_token(conn, uid)
    repo, branch = row["repo"], row["repo_branch"] or "main"

    c = db.connect()
    try:
        p = db.get_project(c, project)
        opp = db.get_opportunity(c, opp_id, project_id=p["id"])
        if not opp:
            raise HTTPException(status_code=404, detail="선택한 개선 기회를 찾을 수 없습니다. 새로고침 후 다시 시도해 주세요.")
        opp = dict(opp)
        perf = db.query_performance(c, p["id"], opp["target"])
    finally:
        c.close()
    evidence = ({"클릭": perf["clicks"], "노출": perf["impressions"],
                 "평균 순위": perf["position"]} if perf else {})

    profile = json.loads(row["repo_profile"]) if row["repo_profile"] else None
    if not profile:                       # 철칙 1 — 프로필 없이 쓰지 않는다
        profile = writer.discover_profile(token, repo, branch)
        store.set_profile(conn, uid, project, json.dumps(profile, ensure_ascii=False))

    doc = writer.write_for(opp, profile, dict(p), evidence)
    br = f"capture/{opp['kind']}-{writer.slug(opp['target'])}"
    body = (f"{doc.get('summary', '')}\n\n"
            f"기회 #{opp_id} · {opp['kind']} · 대상: {opp['target']}\n\n"
            f"— seo-miner")
    pr = gh.open_pr(
        token, repo, branch, br, doc["title"], body,
        [{"path": doc["path"], "content": doc["content"]}],
        # 커밋 메시지의 [opp #id] 는 create 스킬의 규약이다 — 나중에
        # createdb.py sync 가 git log 에서 이걸 읽어 Brain 과 대조한다.
        f"content: {doc['title']} [opp #{opp_id}]")

    c = db.connect()
    try:
        db.record_creation(c, p["id"], doc["path"], opportunity_id=opp_id,
                           kind=opp["kind"], branch=br, note=pr["url"])
        db.set_opportunity_status(c, opp_id, "done")
    finally:
        c.close()
    return {"pr": pr["url"], "path": doc["path"]}


@app.post("/api/create")
async def api_create(request: Request):
    """기회 하나 → 리포에 PR. /create run 의 웹 판이다."""
    uid = _require_uid(request)
    b = await request.json()
    project = str(b.get("project") or "")
    opp_id = int(b.get("opportunity_id") or 0)
    try:
        with store.session(uid, project, isolate=True) as conn:
            row = store.site(conn, uid, project)
            if not row or not row["repo"]:
                raise HTTPException(status_code=428, detail="이 사이트에 저장소가 연결되지 않았습니다. 사이트 화면에서 저장소를 먼저 선택해 주세요.")
            result = _create_content(conn, uid, project, opp_id, row)
        return {"ok": True, **result}
    except (gh.GitHubError, writer.WriterError) as e:
        raise HTTPException(status_code=502, detail=str(e))


# 단계 목록의 정본은 run_all 의 표 하나다. 여기에 사본을 두면 화면에는 있는
# 단계(crawl·metrics·backlinks)가 서버에서만 "실행할 수 없는 단계"로 튕긴다 —
# 실제로 그렇게 났다.
STAGES = run_all.VALID_STAGE_NAMES


@app.post("/api/ai/prompts")
async def api_ai_prompts(request: Request):
    """AI에 물어볼 질문을 만들어 심는다 — 웹에는 `/capture add` 를 칠 채팅이 없다.

    이 화면이 비어 있던 이유가 그것이다: ai 단계는 ai_prompts 를 재료로 도는데
    대시보드 폼으로 만든 사이트는 그 표가 비어 있었고, [AI 인용 다시 확인]은
    "질문이 아직 없습니다"로 즉시 실패했다. 만들기만 하고 인용 확인은 돌리지
    않는다 — 그쪽이 돈 나가는 단계라 사용자가 눌러서 시작해야 한다.
    """
    uid = _require_uid(request)
    body = await request.json()
    project = str(body.get("project") or "")
    n = body.get("limit")
    try:
        # 수집 런과 같은 env 를 두른다 — 유료 키는 서버가 댄다(paid_keys).
        with store.session(uid, project, isolate=True, paid=True):
            c = db.connect()
            try:
                rows = gen_prompts.suggest(project, n=int(n or 20), conn=c)
                if not rows:
                    raise HTTPException(
                        status_code=502,
                        detail="질문을 만들지 못했습니다 — 잠시 후 다시 시도해 주세요.")
                added = gen_prompts.save(c, project, rows)
            finally:
                c.close()
        return {"ok": True, "added": added, "total": len(rows),
                "prompts": [r["prompt"] for r in rows[:5]]}
    except db.ProjectNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    except RuntimeError as e:                 # 키 부재 등 — 사유를 그대로 화면에 보낸다
        raise HTTPException(status_code=503, detail=str(e))


def _ai_prompts_view(c, project: str) -> dict:
    """질문 목록 한 벌 — 화면은 조회든 편집이든 이 모양만 받아 다시 그린다.

    limit 은 사이트 yaml 의 limits.max_ai_prompts 다. collect_ai 가 켜진 질문을
    그 수만큼만 물어보므로, 화면이 다른 수를 말하면 사용자는 자기가 켠 질문이
    조용히 빠지는 걸 보게 된다.
    """
    p = db.get_project(c, project)
    limit = 30
    if p["config_path"]:
        try:
            cfg = db.load_project_yaml(p["config_path"])
            limit = int((cfg.get("limits") or {}).get("max_ai_prompts") or 30)
        except (db.ProjectConfigNotFound, ImportError, TypeError, ValueError):
            pass
    return {"prompts": [dict(r) for r in db.list_ai_prompts(c, p["id"])],
            "active_total": db.count_active_ai_prompts(c, p["id"]),
            "limit": limit}


@app.get("/api/ai/prompts")
def api_ai_prompts_list(project: str, request: Request):
    """심긴 질문 목록. 인용 확인을 한 번 돌리기 전에는 이걸 볼 곳이 없었다 —
    만들기 버튼만 있고 무엇이 만들어졌는지는 화면 어디에도 안 나왔다."""
    uid = _require_uid(request)
    try:
        with store.session(uid, project, isolate=True):
            c = db.connect()
            try:
                return _ai_prompts_view(c, project)
            finally:
                c.close()
    except db.ProjectNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.post("/api/ai/prompts/edit")
async def api_ai_prompts_edit(request: Request):
    """질문 고치기·켜기·끄기·지우기·직접 추가.

    모델이 지은 질문은 초안이다 — 우리 업종과 상관없는 게 섞이고, 정작 물어야 할
    질문이 빠진다. 그걸 사람이 손볼 데가 없으면 [AI 인용] 화면의 수치는 엉뚱한
    질문의 성적표가 된다. 어느 op 든 돌려주는 건 목록 한 벌이다(화면은 다시 그린다).
    """
    uid = _require_uid(request)
    b = await request.json()
    project, op = str(b.get("project") or ""), str(b.get("op") or "")
    if op not in ("save", "active", "delete"):
        raise HTTPException(status_code=400, detail="처리할 수 없는 요청입니다. 새로고침 후 다시 시도해 주세요.")
    try:      # 화면이 보내는 값이다 — 숫자가 아닌 게 오면 500 이 아니라 400 이다
        ids = [int(x) for x in (b.get("ids") or [])][:300]
        pid_edit = int(b["id"]) if b.get("id") else 0
    except (TypeError, ValueError):
        raise HTTPException(status_code=400,
                            detail="처리할 수 없는 요청입니다. 새로고침 후 다시 시도해 주세요.")
    text = " ".join(str(b.get("prompt") or "").split())
    cat = str(b.get("category") or "").strip()
    if cat and cat not in (*gen_prompts.CATEGORIES, "general"):
        cat = "general"

    try:
        with store.session(uid, project, isolate=True):
            c = db.connect()
            try:
                pid = db.get_project(c, project)["id"]
                view = _ai_prompts_view(c, project)
                if op == "save":
                    if not (gen_prompts.MIN_LEN <= len(text) <= gen_prompts.MAX_LEN):
                        raise HTTPException(
                            status_code=400,
                            detail=f"질문은 {gen_prompts.MIN_LEN}~{gen_prompts.MAX_LEN}자로 적어 주세요.")
                    try:
                        if pid_edit:
                            db.update_ai_prompt(c, pid, pid_edit, text, cat or None)
                        elif db.add_ai_prompts(c, pid, [{"prompt": text,
                                                         "category": cat or "general"}]) == 0:
                            # 조용히 무시하면 화면은 아무 변화 없이 다시 그려진다 —
                            # 사용자는 자기가 뭘 잘못 눌렀는지 알 길이 없다.
                            raise HTTPException(status_code=409,
                                                detail="같은 질문이 이미 있습니다.")
                    except sqlite3.IntegrityError:
                        raise HTTPException(status_code=409,
                                            detail="같은 질문이 이미 있습니다.")
                elif op == "active":
                    on = bool(b.get("active"))
                    if on:
                        # 켜진 질문 × 엔진 수 × 샘플 수만큼 돈이 나간다. 상한 너머로
                        # 켜 봐야 collect_ai 가 LIMIT 으로 자르므로, 여기서 막고 말한다.
                        ids = _capped(ids, view["limit"], view["active_total"])
                        if not ids:
                            raise HTTPException(
                                status_code=409,
                                detail=f"켤 수 있는 질문은 {view['limit']}개까지입니다 — 다른 질문을 먼저 꺼 주세요.")
                    db.set_ai_prompts_active(c, pid, ids, on)
                else:
                    db.delete_ai_prompts(c, pid, ids)
                return {"ok": True, **_ai_prompts_view(c, project)}
            finally:
                c.close()
    except db.ProjectNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.post("/api/run")
async def api_run(request: Request, dispatch=Depends(_dispatch_dep), kick=Depends(_kick_dep)):
    """'지금 다시 재기' — /capture run 에 해당한다. stages 를 주면 그 단계만
    돈다(/capture gsc, /capture ai …). 웹에는 명령을 칠 곳이 없으므로 버튼이 그 자리다."""
    uid = _require_uid(request)
    body = await request.json()
    project = str(body.get("project") or "")
    stages = [x for x in str(body.get("stages") or "").split(",") if x]
    bad = [x for x in stages if x not in STAGES]
    if bad:
        raise HTTPException(status_code=400, detail=f"실행할 수 없는 단계입니다: {', '.join(bad)}")

    with store.session(uid, project) as conn:
        row = store.site(conn, uid, project)
        if row and row["running_since"]:
            return {"ok": True, "started": False}      # 이미 도는 중
        if not stages:
            started = store.request_run(conn, uid, project)
        else:
            # 부분 실행은 주기 판정을 건드리지 않는다 — gsc 만 다시 읽었다고
            # 전체 재측정을 한 것으로 치면 다음 자동 런이 통째로 밀린다.
            store.mark_run(conn, row["id"])
            started = True

    if started:
        if stages:
            dispatch("--user", str(uid), "--project", project, "--only", ",".join(stages))
        else:
            kick()
    return {"ok": True, "started": started}


@app.get("/api/report")
def api_report(project: str, request: Request):
    """/capture report — 그 시점 화면을 자립형 HTML 로 박제해 내려준다."""
    uid = _require_uid(request)
    try:
        with store.session(uid, project, isolate=True):
            path = dashboard.export(project)
            data = Path(path).read_bytes()
        # 보고서도 호스팅 문구를 쓴다 — 메일로 나가는 것이라 받는 사람은 웹 사용자다.
        # /d 와 같은 이유로 표식을 **첫 <script> 앞**에 세운다(app.py 의 dash 주석 참고).
        # 백링크는 여기서 박아 넣지 않는다 — 수집이 capture 단계로 내려오면서
        # payload 에 실리고, [백링크] 화면이 로컬·호스팅·박제본에서 같이 그린다.
        data = pages.data(data.decode("utf-8"), SM_HOSTED=True).encode("utf-8")
        data += _REPORT_ADDON
    except db.ProjectNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    return Response(data, media_type="text/html; charset=utf-8", headers={
        "Content-Disposition": f'attachment; filename="{project}-report.html"'})


@app.get("/api/export")
def api_export(project: str, table: str, request: Request):
    """CSV 내려받기 — 마케터는 결국 엑셀로 옮긴다."""
    uid = _require_uid(request)
    try:
        with store.session(uid, project, isolate=True):
            data, name = exports.csv_bytes(project, table)
    except ValueError:
        # exports 의 ValueError 는 개발자용 문구다 — 그대로 내보내지 않는다.
        raise HTTPException(status_code=400,
                            detail="내려받을 수 없는 항목입니다. 화면에서 다시 선택해 주세요.")
    except db.ProjectNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    return Response(data, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.get("/api/perf")
def api_perf(project: str, request: Request):
    """서치콘솔 4대 지표와 검색어·페이지·기기 분해 — 개요 화면이 쓴다."""
    uid = _require_uid(request)
    try:
        with store.session(uid, project, isolate=True):
            return exports.perf(project)
    except db.ProjectNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.get("/api/overview")
def api_overview(request: Request):
    """사이트 전부를 한 줄씩 — 드롭다운으로 하나씩 전환하지 않아도 되게."""
    uid = _require_uid(request)
    with store.session(uid, isolate=True) as conn:
        out = []
        for r in store.sites(conn, uid):
            try:
                d = exports.summary(r["project"])
            except db.ProjectNotFound:
                d = {"project": r["project"], "domain": r["domain"]}
            d["running"] = bool(r["running_since"])
            d["last_run_at"] = r["last_run_at"]
            out.append(d)
        return {"sites": out}


@app.get("/api/keywords")
def api_keywords(project: str, request: Request, status: str = "candidate"):
    """추적 중(active)이거나 후보(candidate)인 키워드.

    후보는 자동완성에서 캔 것이라 관련성이 확인되지 않았다 — 자동 활성화는 서치콘솔에
    노출된 것만 켠다. 나머지는 사람이 보고 골라야 해서 이 목록이 필요하다.
    """
    uid = _require_uid(request)
    active = status == "active"
    try:
        with store.session(uid, project, isolate=True):
            c = db.connect()
            try:
                pid = db.get_project(c, project)["id"]
                rows = db.list_keywords(c, pid, active=active)
                n_active = db.count_active_keywords(c, pid)
            finally:
                c.close()
        return {"keywords": [dict(r) for r in rows], "active_total": n_active,
                "limit": settings.count("SEOMINER_MAX_KEYWORDS")}
    except db.ProjectNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.post("/api/keywords")
async def api_keywords_set(request: Request):
    """고른 키워드를 추적 세트에 넣거나 뺀다. 상한(limit)을 넘겨 켜지 않는다 —
    SERP 는 키워드당 과금이라 여기서 새면 비용이 샌다."""
    uid = _require_uid(request)
    b = await request.json()
    project = str(b.get("project") or "")
    ids = [int(x) for x in (b.get("ids") or [])][:500]
    on = bool(b.get("active"))
    if not ids:
        raise HTTPException(status_code=400, detail="추가하거나 해제할 키워드를 선택해 주세요.")
    try:
        with store.session(uid, project, isolate=True):
            c = db.connect()
            try:
                pid = db.get_project(c, project)["id"]
                if on:
                    limit = settings.count("SEOMINER_MAX_KEYWORDS")
                    ids = _capped(ids, limit, db.count_active_keywords(c, pid))
                    if not ids:
                        raise HTTPException(
                            status_code=409,
                            detail=f"추적 키워드가 한도({limit}개)에 도달했습니다. [추적 중] 탭에서 일부를 해제한 뒤 추가해 주세요.")
                changed = db.set_keywords_active(c, pid, ids, on)
                n = db.count_active_keywords(c, pid)
            finally:
                c.close()
        return {"ok": True, "changed": changed, "active_total": n}
    except db.ProjectNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.get("/api/backlinks")
def api_backlinks(project: str, request: Request):
    """백링크 프로필 — 없으면 빈 값. 지어내지 않는다."""
    uid = _require_uid(request)
    try:
        with store.session(uid, project, isolate=True):
            data = backlinks.latest(project)
            data["available"] = backlinks.available()
        return data
    except db.ProjectNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.get("/api/creations")
def api_creations(project: str, request: Request):
    """/create status — 이 사이트에서 실제로 고친 것들."""
    uid = _require_uid(request)
    try:
        with store.session(uid, project, isolate=True):
            c = db.connect()
            try:
                pid = db.get_project(c, project)["id"]
                rows = db.list_creations(c, pid, limit=50)
            finally:
                c.close()
        return {"creations": [dict(r) for r in rows]}
    except db.ProjectNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.get("/api/run/status")
def api_run_status(request: Request):
    """사이트별 수집 상태 — 화면이 폴링한다."""
    uid = _require_uid(request)
    with store.session(uid) as conn:
        return {r["project"]: {"running": bool(r["running_since"]),
                               "last_run_at": r["last_run_at"],
                               "stage": r["stage"],
                               "pct": r["stage_pct"]}
                for r in store.sites(conn, uid)}


# 자동 수집 주기 프리셋 — 값(시간)과 화면에 쓸 이름. 목록의 정본은 여기다:
# 화면은 이걸 받아 그대로 그리고, 저장은 이 안의 값만 받는다(화면이 보낸 값을 안 믿는다).
# 0 은 '자동 재측정만 끔' — 첫 측정과 [전체 분석 실행]은 그대로 돈다(store.due_sites).
RUN_PRESETS = ((0, "끔"), (6, "6시간"), (12, "12시간"), (24, "하루"), (72, "3일"),
               (168, "주 1회"))


@app.get("/api/settings")
def api_settings(project: str, request: Request):
    """사이트별 설정 — 수집 주기와 연결된 저장소.

    저장소를 여기 같이 싣는 이유: 대시보드가 [콘텐츠 작성]을 심을지 말지 판단해야
    한다. 예전에는 저장소가 없어도 버튼이 그대로 떴고, **눌러야** 428 로 "저장소를
    먼저 연결하세요"를 알았다. 못 쓰는 버튼을 눌러 보게 하지 않는다.
    """
    uid = _require_uid(request)
    with store.session(uid, project, isolate=True) as conn:
        row = store.site(conn, uid, project)
        try:
            c = db.connect()
            try:
                ga4 = db.get_project(c, project)["ga4_property"] or ""
            finally:
                c.close()
        except db.ProjectNotFound:
            ga4 = ""      # 등록 직후 Brain 이 아직 없어도 설정 화면은 열려야 한다
        # 사이트 값이 없으면 전역 기본값이 실효값이다 — 화면은 그게 골라진 것으로 그린다.
        return {"run_every_hours": store.every_hours(conn, uid, project),
                "presets": [{"h": h, "label": t} for h, t in RUN_PRESETS],
                "repo": (row["repo"] if row else None) or "",
                "repo_branch": (row["repo_branch"] if row else None) or "main",
                # 계정 연결과 사이트-저장소 연결은 다른 단계다. 둘을 한 값으로 뭉치면
                # 화면이 "무엇을 먼저 하라"를 말할 수 없다.
                "github_connected": bool(store.github(conn, uid)),
                # "이 배포가 GitHub 연동을 지원하나" — 키가 없으면 화면은 버튼 대신
                # 안내만 낸다. 존재 여부만 준다, 값은 절대 안 내보낸다.
                "github_enabled": bool(settings.get("GITHUB_CLIENT_ID")),
                "ga4_property": ga4}


@app.post("/api/settings")
async def api_settings_set(request: Request):
    uid = _require_uid(request)
    body = await request.json()
    project = str(body.get("project") or "")
    try:
        hours = float(body.get("run_every_hours"))
    except (TypeError, ValueError):
        hours = None
    if hours not in {float(h) for h, _ in RUN_PRESETS}:
        raise HTTPException(status_code=400,
                            detail="선택할 수 없는 수집 주기입니다. 새로고침 후 다시 시도해 주세요.")
    with store.session(uid, project) as conn:
        store.set_every_hours(conn, uid, project, hours)
        return {"ok": True, "run_every_hours": hours}


# --- GA4 ------------------------------------------------------------------
# GSC 와 달리 GA4 속성 ID 는 도메인에서 유추가 안 된다(숫자 ID 이고 도메인과 매핑이
# 없다) — 그래서 목록에서 사람이 직접 고른다. list_properties/suggest_property 는
# collect_ga4.py 가 이미 갖고 있다(여기서 새로 안 짠다).

@app.get("/api/ga4/properties")
def api_ga4_properties(project: str, request: Request):
    """GA4 속성 후보 — [속성 고르기]를 누를 때만 부른다(관리자 API 호출이라 설정을
    열 때마다 부르지 않는다, /api/repos 와 같은 자리). 도메인·이름이 겹치는 것을
    제안으로 얹지만 확정은 사람이 한다 — 엉뚱한 속성이 붙으면 그 뒤 모든 숫자가
    조용히 거짓이 된다(collect_ga4.suggest_property 참고).

    403 은 둘 중 하나다 — 실제로 터진 건 ①(Railway 로그, 2026-08-31): 토큰이 아예
    없어서 collect_gsc._oauth_credentials() 가 브라우저를 열려다 죽었다. ②는 이미
    로그인은 했지만 GA4 스코프가 나중에 추가돼 그 전 토큰엔 없는 경우다. 우선순위
    ①→②로 먼저 본다 — 부르기 전에 doctor/db 로 미리 확인해 그 무엇도 부르지 않고
    바로 답한다. 그래도 남는 예외(SystemExit 포함, collect_gsc 쪽 CLI 최종 안내가
    새는 경우)는 사람 말로 바꾼다 — 스택트레이스가 화면에 그대로 나가면 안 된다.
    """
    uid = _require_uid(request)
    from googleapiclient.errors import HttpError
    not_connected = HTTPException(
        status_code=403,
        detail="구글 계정이 아직 연결되지 않았습니다 — 로그인해야 GA4 속성을 "
               "볼 수 있습니다. 구글 계정으로 로그인해 주세요.")
    need_relogin = HTTPException(
        status_code=403,
        detail="GA4 접근 권한이 없습니다 — 로그인 뒤에 권한이 추가되어 "
               "재로그인이 필요합니다. 다시 구글 계정으로 로그인해 주세요.")
    try:
        with store.session(uid, project, isolate=True):
            c = db.connect()
            try:
                domain = db.get_project(c, project)["domain"]
            finally:
                c.close()
            if not db.gsc_connected():
                raise not_connected
            if doctor.gsc_missing_scopes():
                raise need_relogin
            try:
                _, admin_svc = collect_ga4.get_service()
                props = collect_ga4.list_properties(admin_svc)
            except HttpError as e:
                if getattr(e.resp, "status", None) == 403:
                    raise need_relogin
                raise
    except db.ProjectNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    except HTTPException:
        raise
    except BaseException as e:
        raise HTTPException(
            status_code=502,
            detail="GA4 속성 목록을 가져오지 못했습니다. 잠시 후 다시 시도해 주세요.") from e
    return {"properties": props,
           "suggested": [p["id"] for p in collect_ga4.suggest_property(props, domain)]}


@app.post("/api/ga4/property")
async def api_ga4_set(request: Request):
    """GA4 속성 연결 — 목록에서 사람이 고른 것만 저장한다(자동 확정 없음).
    property_id 는 숫자 ID 문자열로만 저장한다 — 'properties/' 접두는 collect_ga4 가
    API 부를 때 붙인다(db.py 의 컬럼 주석과 같은 계약)."""
    uid = _require_uid(request)
    b = await request.json()
    project = str(b.get("project") or "")
    prop_id = str(b.get("property_id") or "").strip()
    if prop_id and not prop_id.isdigit():
        raise HTTPException(status_code=400,
                            detail="올바르지 않은 속성입니다. 새로고침 후 다시 선택해 주세요.")
    try:
        with store.session(uid, project, isolate=True):
            c = db.connect()
            try:
                db.set_ga4_property(c, db.get_project(c, project)["id"], prop_id)
            finally:
                c.close()
    except db.ProjectNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"ok": True, "ga4_property": prop_id}


# --- 대시보드 -----------------------------------------------------------------
# 화면은 로컬 플러그인의 것을 그대로 쓴다(skills/capture/templates/dashboard.html).
# 아래 경로 이름은 그 HTML 이 부르는 것이라 바꿀 수 없다.

# 로컬 대시보드는 조회 전용이고, 실행은 Claude Code 에서 /capture run 으로 했다.
# 웹에는 명령을 칠 곳이 없다 — 원본 HTML 을 고치는 대신 뒤에 얹어서 버튼을 만든다.
# 보고서용 애드온 — 대시보드와 같은 용어·자간을 쓰되 실행 버튼과 외부 폰트는 뺀다.
# 보고서는 손댈 수 없는 기록이고, 서버도 인터넷도 없이 열려야 한다(원본 export 의 약속).
_REPORT_ADDON = pages.addon("report.html")


_DASH_ADDON = pages.addon("dash.html")


@app.get("/d")
def dash(request: Request):
    _require_uid(request)
    # 호스팅에서는 API 키·의존성 설치·구글 클라이언트 등록이 유저 몫이 아니다(서버가 키를 댄다).
    # "hosted" 조립본이 window.SM_HOSTED=true 를 페이지 스크립트보다 먼저 세우고
    # 호스팅 전용 섹션을 포함해 조립한다(dashboard.assemble).
    return HTMLResponse(dashboard.assemble("hosted") + _DASH_ADDON.decode("utf-8"))


@app.get("/api/projects")
def api_projects(request: Request):
    uid = _require_uid(request)
    with store.session(uid) as conn:
        return [r["project"] for r in store.sites(conn, uid)]


@app.get("/api/data")
def api_data(project: str, request: Request, date: str = ""):
    """date: 화면이 고정한 GSC 기준 수집일 (없으면 최신). 로컬판과 같은 계약이다."""
    uid = _require_uid(request)
    try:
        with store.session(uid, project, isolate=True):
            return dashboard.payload(project, date or None)
    except db.ProjectNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.get("/api/doctor")
def api_doctor(project: str, request: Request):
    uid = _require_uid(request)
    # 수집 런과 **같은 env** 를 두르고 진단한다 — 서버가 대는 유료 키가 여기서도
    # 보여야 doctor 가 "키 없음"이라고 거짓 판정하지 않는다. paid_keys() 가 세우는
    # 표식(SEOMINER_HOSTED)이 준비물의 owner 도 서버로 뒤집는다.
    with store.session(uid, project, isolate=True, paid=True):
        return dashboard.setup_state(project)


@app.post("/api/opp")
async def api_opp(request: Request):
    # 로컬 대시보드는 X-Token 으로 CSRF 를 막았다. 여기서는 세션 로그인이 그 역할을 하므로
    # 화면이 보내는 헤더는 무시한다.
    uid = _require_uid(request)
    body = await request.json()
    if body.get("status") not in db.OPP_STATUSES:
        raise HTTPException(status_code=400, detail="처리할 수 없는 상태값입니다. 새로고침 후 다시 시도해 주세요.")
    with store.session(uid, isolate=True):
        c = db.connect()
        try:
            return {"updated": db.set_opportunity_status(c, int(body.get("id") or 0),
                                                         body["status"])}
        finally:
            c.close()


def demo() -> None:
    import base64
    import tempfile
    from cryptography.fernet import Fernet
    from fastapi.testclient import TestClient
    from itsdangerous import TimestampSigner

    def login_as(client, uid: int, email: str) -> None:
        """SessionMiddleware 가 굽는 것과 같은 쿠키를 심는다 — 구글을 왕복할 수 없으니
        로그인 뒤 화면은 이렇게만 눌러 볼 수 있다."""
        blob = base64.b64encode(json.dumps({"uid": uid, "email": email}).encode())
        client.cookies.set("session", TimestampSigner(SESSION_SECRET).sign(blob).decode())

    with tempfile.TemporaryDirectory() as d:
        os.environ["SEOMINER_DATA"] = d
        os.environ["SEOMINER_SECRET_KEY"] = Fernet.generate_key().decode()
        os.environ["GOOGLE_CLIENT_ID"] = "dummy.apps.googleusercontent.com"
        os.environ["GOOGLE_CLIENT_SECRET"] = "dummy"
        os.environ["OAUTH_REDIRECT_URI"] = "http://localhost:8000/auth/callback"
        os.environ.pop("SEOMINER_RUN_EVERY_HOURS", None)   # 전역 폴백의 기본값을 본다

        c = TestClient(app)

        r = c.get("/healthz")
        assert r.status_code == 200 and r.json() == {"ok": True}, r.text

        r = c.get("/api/properties")
        assert r.status_code == 401, r.text

        # 로그인 전 첫 화면은 랜딩이다 — 사이트 관리 화면이 새어 나오면 안 된다.
        r = c.get("/")
        assert r.status_code == 200 and "<!--SITES-->" not in r.text, r.status_code

        c2 = store.connect()
        u2 = store.upsert_user(c2, "sched@example.com")
        store.add_site(c2, u2, "p1", "sc-domain:p1.com", "p1.com")
        c2.close()

        # dash() 가 dashboard.assemble("hosted") 뒤에 애드온 bytes 를 이어붙인다.
        # assemble() 이 str 이 아니게 바뀌면 그 자리에서 500 이 난다.
        assert isinstance(dashboard.assemble("hosted"), str), "assemble() 이 str 이 아니다"

        # 대시보드·GitHub 경로도 전부 로그인 뒤에 있어야 한다 — 남의 Brain·리포가 열리면 안 된다.
        for path in ("/d", "/api/projects", "/api/data?project=x", "/api/doctor?project=x",
                     "/api/perf?project=x", "/api/repos", "/auth/github",
                     "/api/settings?project=x", "/api/ai/prompts?project=x",
                     "/api/report?project=x", "/api/export?project=x&table=keywords",
                     "/api/keywords?project=x", "/api/backlinks?project=x",
                     "/api/creations?project=x", "/api/run/status",
                     "/api/ga4/properties?project=x"):
            assert c.get(path).status_code == 401, f"{path} 가 로그인 없이 열렸다"
        for path in ("/api/repo", "/api/create", "/api/settings", "/api/ai/prompts",
                     "/api/ai/prompts/edit", "/api/sites", "/api/keywords", "/api/ga4/property"):
            assert c.post(path, json={}).status_code == 401, f"{path} 가 로그인 없이 열렸다"
        assert c.post("/api/opp", json={"id": 1, "status": "done"}).status_code == 401,             "/api/opp 가 로그인 없이 열렸다"

        r = c.get("/auth/login", follow_redirects=False)
        assert r.status_code == 302, (r.status_code, r.text)
        assert r.headers["location"].startswith("https://accounts.google.com"), r.headers
        assert "dummy.apps.googleusercontent.com" in r.headers["location"],             "client_id 가 인가 URL 에 안 실렸다 — import 시점에 얼어붙었을 수 있다"
        # PKCE 가 켜져 있으면 콜백까지 code_verifier 를 넘겨야 한다(세션에 저장).
        assert "code_challenge=" in r.headers["location"], "PKCE 가 꺼졌다"
        assert "include_granted_scopes" not in r.headers["location"],             "과거 승인 스코프까지 합쳐진다 — 읽기 전용만 받아야 한다"

        # 남이 붙인 콜백은 state 가 안 맞는다 — 토큰 교환까지 가면 안 된다.
        r = c.get("/auth/callback?code=x&state=위조", follow_redirects=False)
        assert r.status_code == 400, r.status_code

        # --- 로그인 뒤 화면 --------------------------------------------------
        login_as(c, u2, "sched@example.com")

        # NotConfigured 상태코드 — required(구글) 는 배포가 고장난 것=500,
        # optional(GitHub) 은 그냥 안 켠 기능=501. GITHUB_CLIENT_ID 는 여기까지
        # 한 번도 안 세웠다.
        r = c.get("/auth/github", follow_redirects=False)
        assert r.status_code == 501 and "GITHUB_CLIENT_ID" in r.json()["detail"],             "안 켠 기능인데 500 이다 — 배포 고장과 구분이 안 된다"
        saved_gid = os.environ.pop("GOOGLE_CLIENT_ID")
        try:
            r = c.get("/auth/login", follow_redirects=False)
            assert r.status_code == 500, "필수 설정이 빠졌다 — 배포가 고장난 것이니 500 이어야 한다"
        finally:
            os.environ["GOOGLE_CLIENT_ID"] = saved_gid

        r = c.get("/")
        assert r.status_code == 200, r.text
        assert "<!--USER-->" not in r.text and "<!--SITES-->" not in r.text, "슬롯이 안 채워졌다"
        assert "sched@example.com" in r.text and "sc-domain:p1.com" in r.text, "슬롯이 비었다"
        assert 'window.__TAKEN__=["sc-domain:p1.com"]' in r.text, "값이 안 실렸다"
        assert r.text.index("window.__SITES__") < r.text.index("const $ = id =>"),             "값이 페이지 스크립트보다 뒤에 실린다 — 화면이 undefined 를 읽는다"

        assert c.get("/api/projects").json() == ["p1"], c.get("/api/projects").text
        assert c.get("/d").status_code == 200

        # 설정 — 값이 없으면 전역 기본값이 실효값이고, 프리셋 밖 값은 서버가 막는다.
        r = c.get("/api/settings?project=p1")
        assert r.status_code == 200 and r.json()["run_every_hours"] == 168.0, r.text
        assert r.json()["presets"][0]["h"] == 0, r.text
        # p1 은 아직 Brain 에 동기화되지 않았다(store.add_site 로만 등록) — GA4 는
        # 없는 것으로 답해야지, 설정 화면 전체가 깨지면 안 된다.
        assert r.json()["ga4_property"] == "", "Brain 없는 사이트에서 설정이 깨진다"
        # GITHUB_CLIENT_ID 가 없는 배포다 — "이 배포가 지원하나"는 false, 값은 안 실린다.
        assert r.json()["github_enabled"] is False, "키 없이도 지원한다고 답한다"
        assert "GITHUB_CLIENT_ID" not in json.dumps(r.json()), "설정 값 자체가 새어 나간다"
        os.environ["GITHUB_CLIENT_ID"] = "gh-dummy"
        try:
            assert c.get("/api/settings?project=p1").json()["github_enabled"] is True,                 "운영자가 키를 넣어도 안 켜진다"
        finally:
            os.environ.pop("GITHUB_CLIENT_ID", None)
        assert c.get("/api/settings?project=p1").json()["github_enabled"] is False
        assert c.get("/api/settings?project=없는사이트").status_code == 404
        for bad in (5, "매일", None):
            assert c.post("/api/settings", json={"project": "p1", "run_every_hours": bad}
                          ).status_code == 400, f"프리셋 밖 값이 통과했다: {bad!r}"
        assert c.post("/api/settings", json={"project": "없는사이트", "run_every_hours": 24}
                      ).status_code == 404, "남의 사이트 설정이 열렸다"
        assert c.post("/api/settings", json={"project": "p1", "run_every_hours": 24}
                      ).status_code == 200
        assert c.get("/api/settings?project=p1").json()["run_every_hours"] == 24.0, "저장이 안 됐다"

        # 실행 단계 이름 — 목록에 없는 단계는 400, 있는 단계는 워커로 간다.
        # dispatch/kick 은 Depends 로 받는다 — 실제 subprocess 를 안 띄우고
        # app.dependency_overrides 로 갈아끼운다(globals() 수술 대신 FastAPI 표준).
        spawned, kicked = [], []
        app.dependency_overrides[_dispatch_dep] = lambda: (lambda *a: spawned.append(a))
        app.dependency_overrides[_kick_dep] = lambda: (lambda: kicked.append(True))
        try:
            assert c.post("/api/run", json={"project": "p1", "stages": "없는단계"}
                          ).status_code == 400
            assert c.post("/api/run", json={"project": "없는사이트"}).status_code == 404
            r = c.post("/api/run", json={"project": "p1", "stages": "competitors"})
            assert r.status_code == 200 and r.json()["started"], r.text
            assert spawned and "competitors" in spawned[0], spawned

            # /api/sites — 등록 진입점. 여태 demo() 어디에도 안 나왔다.
            assert c.post("/api/sites", json={"properties": []}).status_code == 400
            r = c.post("/api/sites", json={"properties": ["sc-domain:new1.com"]})
            assert r.status_code == 200 and r.json()["ok"], r.text
            assert r.json()["added"][0]["project"] == "new1", r.json()
            assert kicked, "새 사이트 등록인데 워커를 안 띄웠다"
        finally:
            app.dependency_overrides.pop(_dispatch_dep, None)
            app.dependency_overrides.pop(_kick_dep, None)

        # 질문 만들기 — 웹에는 `/capture add` 를 칠 채팅이 없어서 생긴 자리다.
        # 키가 없으면 조용히 빈 목록이 아니라 503 + 사유(화면이 그대로 보여 준다).
        saved_key = os.environ.pop("OPENROUTER_API_KEY", None)
        r = c.post("/api/ai/prompts", json={"project": "p1"})
        assert r.status_code == 503 and "OPENROUTER_API_KEY" in r.json()["detail"], r.text
        assert c.post("/api/ai/prompts", json={"project": "없는사이트"}).status_code == 404,             "남의 사이트에 질문을 심을 수 있다"
        made, real_suggest, real_save = [], gen_prompts.suggest, gen_prompts.save
        gen_prompts.suggest = lambda project, **kw: [
            {"prompt": f"{project} 어디가 잘해?", "category": "추천"}]
        gen_prompts.save = lambda conn, project, rows: made.extend(rows) or len(rows)
        try:
            r = c.post("/api/ai/prompts", json={"project": "p1", "limit": 1})
            assert r.status_code == 200 and r.json()["added"] == 1, r.text
            assert made and made[0]["prompt"].startswith("p1"), made
        finally:
            gen_prompts.suggest, gen_prompts.save = real_suggest, real_save
            if saved_key:
                os.environ["OPENROUTER_API_KEY"] = saved_key

        # Brain 이 아직 없는 사이트 — 지어내지 말고 404 여야 한다.
        assert c.get("/api/data?project=p1").status_code == 404
        assert c.get("/api/overview").json()["sites"][0]["project"] == "p1"

        # 질문 목록·편집 — 만들기 버튼만 있고 무엇이 심겼는지 볼 데가 없었다.
        assert c.get("/api/ai/prompts?project=없는사이트").status_code == 404,             "남의 사이트 질문이 열린다"
        conn = store.connect()
        try:
            with store.tenant(conn, u2):
                assert dashboard.create_project(
                    {"name": "p1", "type": "local_clinic", "domain": "p1.com"})["ok"]
        finally:
            conn.close()
        r = c.get("/api/ai/prompts?project=p1")
        assert r.status_code == 200 and r.json()["prompts"] == [], r.text
        # 상한은 사이트 yaml 의 limits.max_ai_prompts 다 — collect_ai 가 그 수만큼만 묻는다.
        assert r.json()["limit"] == 30, r.text

        def edit(**b):
            return c.post("/api/ai/prompts/edit", json={"project": "p1", **b})

        d = edit(op="save", prompt="  밀리아  제거 잘하는 곳 어디야? ", category="추천").json()
        assert [q["prompt"] for q in d["prompts"]] == ["밀리아 제거 잘하는 곳 어디야?"], d
        assert d["prompts"][0]["category"] == "추천" and d["active_total"] == 1, d
        assert edit(op="save", prompt="짧음").status_code == 400, "한 글자짜리도 질문이 된다"
        assert edit(op="save", prompt="같은 질문 또 넣기 되나요?").status_code == 200
        assert edit(op="save", prompt="같은 질문 또 넣기 되나요?").status_code == 409,             "중복 추가가 조용히 무시된다 — 화면은 아무 변화 없이 다시 그려진다"
        qid = d["prompts"][0]["id"]
        d = edit(op="active", ids=[qid], active=False).json()
        assert d["active_total"] == 1 and d["prompts"][-1]["is_active"] == 0, d
        d = edit(op="save", id=qid, prompt="밀리아는 왜 생겨?").json()
        assert "밀리아는 왜 생겨?" in [q["prompt"] for q in d["prompts"]], d
        assert edit(op="save", id=qid, prompt="같은 질문 또 넣기 되나요?").status_code == 409,             "다른 질문과 같은 문구로 덮어써진다"
        assert len(edit(op="delete", ids=[qid]).json()["prompts"]) == 1
        assert edit(op="드롭테이블", ids=[qid]).status_code == 400
        assert edit(op="delete", ids=["1; DROP TABLE"]).status_code == 400, "숫자가 아닌 id 가 500 을 낸다"

        # GA4 속성 고르기 — GSC 와 달리 도메인에서 유추가 안 되니 목록에서 사람이
        # 고른다. list_properties/get_service 를 가짜로 갈아끼운다(네트워크 없이).
        real_get_service, real_list_props = collect_ga4.get_service, collect_ga4.list_properties
        real_missing_scopes, real_gsc_connected = doctor.gsc_missing_scopes, db.gsc_connected
        collect_ga4.get_service = lambda: (None, "admin-svc-stub")
        collect_ga4.list_properties = lambda admin_svc: [
            {"account": "A", "id": "111", "name": "p1.com - GA4"},
            {"account": "A", "id": "222", "name": "다른회사"}]
        # 테스트 환경엔 실제 구글 토큰이 없다 — 아래 성공 경로를 보려면 "스코프도
        # 토큰도 이미 멀쩡하다"를 흉내낸다. 진짜 값은 별도로 아래에서 검사한다.
        doctor.gsc_missing_scopes = lambda: []
        db.gsc_connected = lambda: True
        try:
            assert c.get("/api/ga4/properties?project=없는사이트").status_code == 404,                 "남의 사이트 GA4 속성이 열린다"
            r = c.get("/api/ga4/properties?project=p1")
            assert r.status_code == 200, r.text
            assert r.json()["suggested"] == ["111"], r.json()   # 도메인과 겹치는 것만 제안
            assert len(r.json()["properties"]) == 2, r.json()

            assert c.post("/api/ga4/property", json={"project": "p1", "property_id": "abc"}
                          ).status_code == 400, "숫자가 아닌 속성이 저장된다"
            assert c.post("/api/ga4/property", json={"project": "없는사이트", "property_id": "111"}
                          ).status_code == 404, "남의 사이트에 GA4 속성을 저장할 수 있다"
            r = c.post("/api/ga4/property", json={"project": "p1", "property_id": "111"})
            assert r.status_code == 200 and r.json()["ga4_property"] == "111", r.text
            assert c.get("/api/settings?project=p1").json()["ga4_property"] == "111",                 "설정 화면이 연결된 속성을 안 보여준다"

            # 403 — 스코프 부족(analytics.readonly 가 나중에 더해졌다). 재로그인 안내가 나와야 한다.
            def _scope_missing(admin_svc):
                from googleapiclient.errors import HttpError
                class _Resp:
                    status = 403
                    reason = "Forbidden"
                raise HttpError(_Resp(), b'{"error": "insufficient scope"}')
            collect_ga4.list_properties = _scope_missing
            r = c.get("/api/ga4/properties?project=p1")
            assert r.status_code == 403 and "재로그인" in r.json()["detail"], r.text

            # 스코프가 모자란 걸 doctor 로 미리 알면 — API 를 아예 안 부르고 같은
            # 403 을 예측 가능하게 낸다(브라우저 로그인·SystemExit 경로를 안 탄다).
            def _must_not_call(*a, **kw):
                raise AssertionError("스코프 부족인데 GA4 API 를 불렀다")
            collect_ga4.get_service = _must_not_call
            doctor.gsc_missing_scopes = lambda: [
                "https://www.googleapis.com/auth/analytics.readonly"]
            r = c.get("/api/ga4/properties?project=p1")
            assert r.status_code == 403 and "재로그인" in r.json()["detail"], r.text
            doctor.gsc_missing_scopes = lambda: []

            # 아예 연결이 안 된 상태(토큰 없음) — 실제로 터진 게 이거다(Railway 로그,
            # 2026-08-31: 토큰이 없어 collect_gsc 가 브라우저를 열려다 500). "재로그인"이
            # 아니라 "아직 연결 안 됨" — 한 번도 안 붙인 사람에게 "다시"는 말이 안 된다.
            db.gsc_connected = lambda: False
            r = c.get("/api/ga4/properties?project=p1")
            assert r.status_code == 403 and "아직 연결되지 않았습니다" in r.json()["detail"],                 r.text
            db.gsc_connected = lambda: True

            # 그래도 남는 예외(SystemExit 포함, collect_gsc.get_credentials 가 실제로
            # 던지는 것) — 스택트레이스 대신 사람 말로 된 예측 가능한 502.
            def _boom():
                sys.exit("구글 서치콘솔 인증이 없습니다")
            collect_ga4.get_service = _boom
            r = c.get("/api/ga4/properties?project=p1")
            assert r.status_code == 502, r.text
            assert "다시 시도" in r.json()["detail"], r.json()
        finally:
            collect_ga4.get_service, collect_ga4.list_properties = real_get_service, real_list_props
            doctor.gsc_missing_scopes, db.gsc_connected = real_missing_scopes, real_gsc_connected

        # 세션 모듈로 옮기면서 demo() 에 한 번도 안 나오던 라우트들 — 최소한 로그인
        # 상태에서 200/404 를 확인한다.
        # 위 /api/run(stages=competitors) 가 mark_run 을 찍어 놓고 워커는 가짜였다 —
        # 그래서 아직 '도는 중'이다. 화면 폴링이 읽는 값이 실제로 반영되는지만 본다.
        assert c.get("/api/run/status").json()["p1"]["running"] is True

        assert c.get("/api/keywords?project=없는사이트").status_code == 404
        r = c.get("/api/keywords?project=p1")
        assert r.status_code == 200 and r.json()["keywords"] == [], r.text
        assert c.post("/api/keywords", json={"project": "p1", "ids": []}).status_code == 400
        assert c.post("/api/keywords", json={"project": "없는사이트", "ids": [1], "active": True}
                      ).status_code == 404, "남의 사이트 키워드를 건드릴 수 있다"

        assert c.get("/api/backlinks?project=없는사이트").status_code == 404
        r = c.get("/api/backlinks?project=p1")
        assert r.status_code == 200 and "available" in r.json(), r.text

        assert c.get("/api/creations?project=없는사이트").status_code == 404
        r = c.get("/api/creations?project=p1")
        assert r.status_code == 200 and r.json()["creations"] == [], r.text

        assert c.get("/api/export?project=없는사이트&table=keywords").status_code == 404
        assert c.get("/api/export?project=p1&table=nope").status_code == 400
        r = c.get("/api/export?project=p1&table=keywords")
        assert r.status_code == 200 and r.content.startswith(b"\xef\xbb\xbf"), "CSV BOM 누락"

        assert c.get("/api/report?project=없는사이트").status_code == 404
        r = c.get("/api/report?project=p1")
        assert r.status_code == 200 and len(r.content) > 0, r.status_code

        c.cookies.clear()

        # env 가 없으면 조용히 굴러가지 말고 실패해야 한다
        os.environ.pop("GOOGLE_CLIENT_ID")
        r = c.get("/auth/login", follow_redirects=False)
        assert r.status_code == 500, f"GOOGLE_CLIENT_ID 없이 {r.status_code} 로 통과했다"
        assert "GOOGLE_CLIENT_ID" in r.json()["detail"], r.text

        print("app: ok")


if __name__ == "__main__":
    demo()