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
import io
import json
import re
import secrets
import sqlite3
import tempfile
import traceback
from contextlib import asynccontextmanager, redirect_stdout
from typing import Optional
from urllib.parse import quote, urlparse

from fastapi import Depends, FastAPI, HTTPException, Request
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               RedirectResponse, Response)
from starlette.background import BackgroundTask
from starlette.middleware.sessions import SessionMiddleware

import collect_ga4
import collect_gsc
import dashboard
import db
import doctor    # setup 스킬의 진단 — dashboard 가 이미 skills/setup/scripts 를 sys.path 에 얹는다
import exports
import gen_prompts
import identity
import pages
import remote      # graft — 로컬 플러그인이 올린 측정치를 테넌트 brain 에 더한다
import serp_adapter   # 언어-지역 목록 정본 — 등록·설정 화면이 이걸 그린다
import run_all
import scheduler
import settings
import stage
import store


def resume_dead_runs(dispatch=None) -> list[str]:
    """서버가 뜨는 자리에서 죽은 런을 회수하고 그 사이트만 다시 띄운다.

    Railway 는 push 마다 컨테이너를 갈아치운다 — 수집 도중 SIGKILL 이 정상 경로이고,
    워커의 finally 는 프로세스째 죽으면 안 돈다. 회수를 안 하면 running_since 가 남아
    화면이 3시간 동안 "분석 중 38%" 로 굳고 수동 재실행까지 막힌다.

    `--all` 로 띄우지 않는다 — 스윕은 due 판정을 거치므로 방금 잰 것으로 찍힌
    (mark_run 이 last_run_at 을 남겼다) 사이트가 통째로 빠진다. 죽은 사이트만
    직접 띄운다. 처음부터 다시 도는 것으로 충분하다 — 수집기들이 seen_today 로
    오늘 이미 한 항목을 건너뛰므로 싸다.

    회수(store.reclaim_dead_runs)는 사이트 brain 의 끝나지 않은 runs 행도 닫는다 —
    죽은 워커가 남긴 것과 오래전 고아까지. 새 워커는 그 **뒤에** 띄운다: 먼저 띄우면
    새 런의 행이 회수 시각 이후에 생기므로 닫히지는 않지만, 순서로도 못 박아 둔다.

    묶음 런이었으면 회수가 그 묶음들을 대기열로 되돌려 둔다 — 그 대기열을 가져가는 워커
    (--queue)로 띄워, 죽은 런이 맡았던 묶음만 다시 돈다. 띄우기 전에 '대기 중'으로 잡는다:
    안 잡으면 워커가 뜨는 몇 초 사이에 스케줄러 틱이 같은 대기열을 가져가 두 벌이 돈다.

    **대기열이 없으면 띄우지 않는다.** 그건 죽은 부분 실행(/capture gsc 같은 --only)이다 —
    무엇을 돌던지 안 남는다(run_groups=''). 인자 없이 띄우면 워커는 그걸 전체 재기로 읽어
    아무도 안 누른 유료 단계 전부(57분)를 사고 모든 묶음 시계를 민다. 회수가 "필요하면
    다시 실행하세요"라고 남긴다(store.DEAD_PARTIAL_ERROR).
    """
    dispatch = dispatch or scheduler.dispatch
    conn = store.connect()
    try:
        rows = store.reclaim_dead_runs(conn)
        plans = [r for r in rows
                 if store.queued(conn, r["id"]) and store.mark_pending(conn, r["id"])]
    finally:
        conn.close()
    for r in rows:
        print(f"[resume] 죽은 런 회수: {r['user_id']}/{r['project']}", flush=True)
    for r in plans:
        dispatch("--user", str(r["user_id"]), "--project", r["project"], "--queue")
    return [r["project"] for r in rows]


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        resume_dead_runs()
    except Exception as e:              # 회수가 안 됐다고 서버가 안 뜨면 안 된다
        print(f"[resume] 실패: {e}", flush=True)
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
    required — 없으면 이 배포 자체가 고장난 것이니 500. optional 인 설정이 빠진
    것은 안 켠 기능이니 501(Not Implemented)이다 — 배포 고장과 구분이 돼야 한다."""
    required = settings.SETTINGS[e.name].required
    return JSONResponse({"detail": str(e)}, status_code=500 if required else 501)


UNAUTHORIZED = "로그인이 풀렸습니다. 처음 화면에서 구글 계정으로 다시 로그인하세요."
# GA4 스코프가 모자랄 때 할 말. 두 자리가 이 사실을 말한다 — 부르기 전 검사
# (_require_google)와, 그걸 뚫고 구글이 직접 403 을 준 경우. 같은 사실이니
# 문구도 한 벌이다.
RELOGIN_FOR_GA4 = ("로그인한 뒤에 GA4 읽기 권한이 늘었습니다. 권한 문제가 아니라 "
                   "저장된 로그인이 오래된 것입니다. 로그아웃한 뒤 "
                   "다시 구글 계정으로 로그인하세요.")


def _uid(request: Request) -> Optional[int]:
    return request.session.get("uid")


def _bearer_uid(request: Request) -> Optional[int]:
    """`Authorization: Bearer smt_...` → user_id. 로컬 CLI 가 원격을 조작하는 통로다."""
    scheme, _, token = (request.headers.get("authorization") or "").partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    conn = store.connect()
    try:
        return store.uid_for_cli_token(conn, token.strip())
    finally:
        conn.close()


def _require_uid(request: Request) -> int:
    """세션이 없으면 CLI 토큰을 본다.

    검증 훅이 **여기 한 곳**인 것이 설계의 핵심이다 — 라우트마다 인증을 적으면 새
    라우트가 그 줄을 빠뜨린 채 조용히 열린다. 원격 CLI 가 쓰는 라우트를 따로
    고르지도 않는다: 화면이 부르는 것과 같은 API 를 같은 권한으로 부른다.
    """
    uid = _uid(request)
    if uid is None:
        uid = _bearer_uid(request)
    if uid is None:
        raise HTTPException(status_code=401, detail=UNAUTHORIZED)
    return uid


def _html(body: str, title: str = "seo-miner") -> HTMLResponse:
    return HTMLResponse(pages.document(body, title))


# 워커 실행 진입점 — 직접 부르지 않고 Depends 로 받는다. app.dependency_overrides 로
# demo() 가 실제 subprocess 를 안 띄우고 갈아끼울 수 있게(globals() 수술 대신).
def _dispatch_dep():
    return scheduler.dispatch


def _kick_dep():
    return scheduler.kick


# --- 의존자 -------------------------------------------------------------------
# 라우트가 손으로 되풀이하던 네 가지 — 인증(_require_uid) · 본문 꺼내기 ·
# store.session 열고 닫기 · brain 열고 닫기 — 를 Depends 로 옮긴다. 되풀이를 줄이는
# 것보다 **닫는 것을 빠뜨릴 수 없게** 하는 쪽이 목적이다: `c = t.brain()` +
# try/finally 를 아홉 곳에 손으로 적으면 열 번째가 빠지고, 새는 건 커넥션이다.
# _dispatch_dep/_kick_dep 이 이미 그 결이다(바로 위 주석) — 같은 방식이라
# test_app.py 가 dependency_overrides 로 갈아끼울 수 있다.


async def _body(request: Request) -> dict:
    """POST 본문 한 벌. Depends 는 한 요청에 한 번만 돌므로(use_cache) 본문도
    한 번만 푼다 — 라우트가 다시 `await request.json()` 을 적지 않는다."""
    return await request.json()


def _project_q(project: str) -> str:
    """?project= — 쿼리로 사이트를 가리키는 라우트가 이걸로 이름을 얻는다."""
    return project


async def _project_b(body: dict = Depends(_body)) -> str:
    """본문의 project — POST 라우트가 쓴다. 값 꼴은 여기 한 곳에서 정한다."""
    return str(body.get("project") or "")


def _opens(where=None, *, isolate: bool = False, paid: bool = False):
    """store.session(...) 을 여는 의존자를 만든다.

    where 는 사이트 이름을 어디서 읽나 — None(사이트를 안 가림) · _project_q ·
    _project_b. 조합마다 **모듈 전역에 한 벌씩** 만들어 두고(아래 표) 라우트는
    그걸 가리키기만 한다: 팩토리를 라우트 자리에서 부르면 매번 다른 함수 객체가
    되어 Depends 캐시가 안 먹고, 같은 요청에서 session 이 두 번 열린다.

    소유 확인(project 를 주면 기본값)과 404 도 store.session 이 그대로 한다 —
    여기서 다시 판정하지 않는다.

    받은 값(`t`)을 안 쓰는 라우트도 있다. 그때 필요한 건 값이 아니라 **두르는
    것** 이다 — 테넌트 env(CAPTURE_HOME) 가 서 있어야 엔진이 그 유저의 home 을
    본다. 안 쓴다고 빼면 남의 보관함이 열린다.
    """
    if where is None:
        def dep(uid: int = Depends(_require_uid)):
            with store.session(uid, isolate=isolate, paid=paid) as s:
                yield s
    else:
        def dep(uid: int = Depends(_require_uid), project: str = Depends(where)):
            with store.session(uid, project, isolate=isolate, paid=paid) as s:
                yield s
    return dep


def _opens_brain(of):
    """테넌트의 brain.db 를 열고 **반드시 닫는** 의존자를 만든다.

    of 로 받은 테넌트 의존자를 그대로 가리키므로(같은 함수 객체) 라우트가 둘을
    함께 선언해도 session 은 한 번만 열린다.
    """
    def dep(t=Depends(of)):
        c = t.brain()
        try:
            yield c
        finally:
            c.close()
    return dep


CONN = _opens()                                        # 서버 DB 만
CONN_Q = _opens(_project_q)                            # + ?project= 의 사이트 소유 확인
CONN_B = _opens(_project_b)                            # + 본문의 사이트 소유 확인
TENANT = _opens(isolate=True)                          # 서버 DB + 그 유저의 home
TENANT_Q = _opens(_project_q, isolate=True)
TENANT_B = _opens(_project_b, isolate=True)
TENANT_Q_PAID = _opens(_project_q, isolate=True, paid=True)     # + 서버가 대는 유료 키
TENANT_B_PAID = _opens(_project_b, isolate=True, paid=True)
BRAIN_Q = _opens_brain(TENANT_Q)
BRAIN_B = _opens_brain(TENANT_B)
BRAIN_B_PAID = _opens_brain(TENANT_B_PAID)


@app.exception_handler(db.ProjectNotFound)
def _project_not_found(request: Request, e: db.ProjectNotFound):
    """없는 사이트 → 404. 열여섯 라우트가 같은 세 줄을 손으로 적고 있었다 —
    한 곳이면 새 라우트가 그 줄을 빠뜨린 채 500 을 뱉을 수 없다(_require_uid 가
    인증에 대해 하는 것과 같은 이야기다).

    문구는 엔진이 쓴 그대로 나간다(detail=str(e)) — 화면과 검사가 읽고 있다."""
    return JSONResponse({"detail": str(e)}, status_code=404)


@app.exception_handler(sqlite3.IntegrityError)
def _integrity(request: Request, e: sqlite3.IntegrityError):
    """UNIQUE 충돌은 사용자가 같은 것을 또 넣은 것이지 서버 고장이 아니다 → 409.
    여기는 마지막 그물이다 — 사용자가 방금 한 일을 이름으로 말할 수 있는 자리는
    라우트가 자기 문구로 잡는다(질문 중복이 그 예다)."""
    return JSONResponse({"detail": str(e)}, status_code=409)


# 로컬 플러그인이 링크로 실어 보낸 사이트 설정 중, 등록에 실제로 얹는 것.
# 이름·도메인·속성은 서버가 정한 것이 이긴다(슬러그 충돌·표기 검증). 종류(type)도
# 여기 없다 — 화면이 carry 값으로 미리 골라 두므로 types 에 담겨 오고, 사용자가
# 바꿨으면 그쪽이 이긴다. 여기 남는 건 사람이 정했고 기계가 못 짓는 것뿐이다.
# 이름의 정본은 dashboard.PREFILL_KEYS 이고 test_seams 가 대조한다.
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
        # 줄 하나가 셋을 말한다: 무엇인가(이름·속성) · 지금 상태인가(배지) · 무엇을
        # 누르나(대시보드 열기). 상태는 배지고 행동은 버튼이다 — 섞이면 "첫 분석
        # 진행 중"이 누를 수 있는 것처럼 보인다.
        items = "".join(
            f'<li class="{"waiting" if not r["last_run_at"] else "done"}">'
            f'<div class="s-id"><span class="nm">{e(r["project"])}</span>'
            f'<span class="pr">{e(r["gsc_property"])}</span></div>'
            '<div class="s-state">'
            + ('<span class="badge run">첫 분석 진행 중</span><span class="when"></span>'
               if not r["last_run_at"]
               else '<span class="badge ok">분석 완료</span>'
                    f'<span class="when">{e(str(r["last_run_at"])[:16])}</span>')
            + '</div>'
            + f'<a class="go sm{"" if r["last_run_at"] else " sec"}"'
              f' href="/d#{quote(str(r["project"]), safe="")}">대시보드 열기</a>'
            + ('' if r["last_run_at"] else
               '<div class="prog"><div class="progress"><i></i></div>'
               '<p class="stg">수집을 준비합니다</p></div>')
            + '</li>' for r in rows)
        block = (f'<section id="live"><p class="eyebrow">사이트 {len(rows)}개</p>'
                 # 「분석 중인 사이트」라고 부르면 끝난 사이트가 섞인 목록을 잘못
                 # 가리킨다 — 이 목록은 등록한 것 전부다.
                 '<h2>내 사이트</h2>'
                 '<p class="sub">첫 분석이 끝나면 이 줄에서 대시보드로 들어갑니다.</p>'
                 f'<ul class="sites">{items}</ul>')
        if any(not r["last_run_at"] for r in rows):
            block += ('<p class="wait-note">검색 실적 · 색인 · 키워드 순으로 몇 분에 걸쳐 '
                      '수집됩니다. 창을 닫아도 계속 진행됩니다.</p>')
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
                     __SITES__=[{"project": r["project"]} for r in rows],
                     __CARRY__=carry,
                     # 단계 이름표는 한 벌이다 — app.html 도 사본을 안 갖는다
                     # (대시보드가 window.__STAGES__ 로 받는 것과 같은 표다).
                     __STAGES__=stage.STAGE_LABELS,
                     # 언어-지역 목록도 한 벌이다(serp_adapter.LOCALES)
                     __LOCALES__=serp_adapter.LOCALES,
                     # 사이트 종류(id·라벨)도 한 벌이다(dashboard.PROJECT_TYPES) —
                     # 받는 쪽 검증이 보는 표를 화면이 그대로 그린다.
                     __TYPES__=dashboard.PROJECT_TYPES)
    return _html(doc, "사이트 관리 — seo-miner")


def _begin(request: Request, provider: str) -> RedirectResponse:
    url, carry = identity.start(provider)
    request.session[identity.session_key(provider)] = carry
    return RedirectResponse(url, status_code=302)


def _carry(request: Request, provider: str, state: str) -> dict:
    carry = identity.carried(request.session, provider, state)
    if carry is None:
        raise HTTPException(status_code=400, detail="로그인이 만료됐습니다. 처음 화면에서 다시 로그인하세요.")
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
    """세션만 끊는다. 저장된 구글 토큰은 남겨 둔다 — 다시 로그인하면
    그대로 이어 쓴다(계정을 바꿔 가며 쓰라는 게 이 버튼의 목적이다).

    GET 이 아니라 POST 다 — 링크였다면 브라우저 prefetch 나 남이 심은 이미지 태그로
    의도치 않게 로그아웃된다.
    """
    request.session.clear()
    return RedirectResponse("/", status_code=302)


@app.get("/privacy")
def privacy():
    return _html(pages.page("privacy.html"), "개인정보처리방침 — seo-miner")


def _require_google() -> None:
    """구글 API 를 부르기 전 두 가지를 본다 — 연결됐나, **그 토큰이 지금 스코프를 덮나**.

    둘째가 없어서 났던 일: analytics.readonly 가 나중에 추가되면서, 그 전에
    로그인해 둔 토큰은 갱신할 때 "요청한 스코프를 다 못 받았다"로 죽는다.
    collect_gsc 는 그 자리에서 로그인을 다시 받으려 하고(서버엔 브라우저가 없다)
    결국 sys.exit → 라우트는 원인 없는 502. 화면에는 "권한이 있는지 확인해
    주세요"만 남아서, 사실은 재로그인 한 번이면 끝날 일을 아무도 못 알아봤다.

    라우트마다 검사를 붙이면 다음에 생기는 라우트가 또 빠진다 — 한 곳이다.
    """
    if not db.gsc_connected():
        raise HTTPException(
            status_code=403,
            detail="구글 계정이 연결돼 있지 않습니다. "
                   "다시 구글 계정으로 로그인하세요.")
    if doctor.gsc_missing_scopes():
        raise HTTPException(
            status_code=403,
            detail=RELOGIN_FOR_GA4)


@app.get("/api/properties")
def api_properties(t=Depends(TENANT)):
    """서치콘솔 속성 목록 — 사이트를 등록할 때 고르는 자리.

    /api/ga4/properties 와 같은 이유로 부르기 전에 연결 상태를 본다:
    collect_gsc.get_credentials() 는 CLI 전제라 토큰이 없으면 대화형 로그인을
    시도하고(서버에는 브라우저가 없다) 수단 자체가 없으면 sys.exit 한다.
    """
    _require_google()
    try:
        res = collect_gsc.get_service().sites().list().execute()
    except HTTPException:
        raise
    except BaseException:      # SystemExit 도 잡는다 — 스택트레이스가 나가면 안 된다
        # 로그에는 남긴다 — 안 남겨서 이 502 의 원인을 로그로는 끝내 못 봤다.
        traceback.print_exc()
        raise HTTPException(
            status_code=502,
            detail="서치콘솔이 응답하지 않아 속성 목록을 못 읽었습니다. "
                   "잠시 뒤 다시 시도하세요.")
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
def api_sites(request: Request, body: dict = Depends(_body),
              uid: int = Depends(_require_uid), t=Depends(TENANT),
              kick=Depends(_kick_dep)):
    """속성 여러 개를 한 번에 등록한다. 이름·도메인은 속성에서 짓고, 종류만 받는다."""
    props = body.get("properties") or []
    if isinstance(props, str):
        props = [props]
    if not props:
        raise HTTPException(status_code=400, detail="분석할 사이트를 하나 이상 고르세요.")
    types = body.get("types") or {}
    # 언어-지역은 사이트마다 고른다(한 계정에 한국어·영어 사이트가 같이 있다).
    # 목록 밖 값은 미국 SERP 로 조용히 떨어지므로 여기서 막는다.
    locales = body.get("locales") or {}
    known = dict(serp_adapter.LOCALES)
    for loc in set(locales.values()) | {body.get("locale", db.DEFAULT_LOCALE)}:
        if loc not in known:
            raise HTTPException(status_code=400, detail=f"고를 수 없는 언어-지역입니다: {loc}")

    # 로컬에서 넘어온 설정은 그 속성 하나에만 얹는다 — 한 번 쓰면 세션에서 뺀다
    # (다음에 다른 사이트를 등록할 때 남의 씨앗이 섞이면 안 된다).
    carry = dashboard.carry_read(request.session.pop("carry", ""))
    carry_prop = carry.get("gsc_property", "")

    added, failed = [], []
    taken = {r["project"] for r in store.sites(t.conn, uid)}
    for prop in props[:20]:
        host = _host_of(str(prop))
        if not host:
            failed.append({"property": prop, "error": "도메인을 알 수 없습니다"})
            continue
        name = _slug(host, taken)
        f = {
            "name": name, "type": types.get(prop, "saas"), "domain": host,
            "gsc_property": prop, "locale": locales.get(prop) or body.get("locale", db.DEFAULT_LOCALE),
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
        store.add_site(t.conn, uid, a["project"], a["property"], _host_of(a["property"]))

    if added:
        kick()
    return {"ok": bool(added), "added": added, "failed": failed}


# 단계 목록의 정본은 run_all 의 표 하나다. 여기에 사본을 두면 화면에는 있는
# 단계(crawl·metrics·backlinks)가 서버에서만 "실행할 수 없는 단계"로 튕긴다 —
# 실제로 그렇게 났다.
STAGES = run_all.VALID_STAGE_NAMES


def _stage_opts(raw) -> dict[str, str | list[str]]:
    """`{"rank.device": "mobile"}` 을 검증한다. 모르는 키는 **400 이다**.

    조용히 무시하면 `--device mobile` 을 준 사용자가 데스크톱 결과를 모바일 결과로
    읽는다 — 이 리포가 가장 싫어하는 실패 방식이다.

    노브 목록은 여기 사본으로 두지 않는다. 정본은 run_all.STAGES(stage.knobs) 다 —
    어느 단계가 옵션을 받나, 그 단계 파서가 노출한 어떤 키를 받나가 거기서 함께
    나온다. `**opts` 로 삼키는 자리(conn·post·fetch 같은 테스트 주입 kwarg)는 stage.knobs
    에 애초에 없다 — 조용히 삼켜서 값이 무시되는 것이 오류보다 나쁘다.
    """
    if not raw:
        return {}
    if not isinstance(raw, dict):
        raise HTTPException(status_code=400,
                            detail='opts 는 {"단계.키": "값"} 모양이어야 합니다.')
    out: dict[str, str | list[str]] = {}
    for k, v in raw.items():
        stage_name, dot, key = str(k).partition(".")
        key = key.strip().replace("-", "_")
        stg = run_all.STAGE_BY_NAME.get(stage_name) if dot else None
        ok = stg is not None and key in stg.knobs
        if not ok:
            valid = sorted(s.name for s in run_all.STAGES if s.knobs)
            raise HTTPException(
                status_code=400,
                detail=f"알 수 없는 옵션입니다: {k} — '단계.키' 모양이어야 하고, "
                       f"옵션을 받는 단계는 {', '.join(valid)} 입니다.")
        # 리스트는 접지 않는다 — str(["a.com"]) 은 "['a.com']" 이 되고 _coerce 가 그걸
        # 문자열로 되돌려 수집기가 도메인 하나도 못 읽는다(competitors.domain 이 그 자리다).
        # 여러 값은 --opt 를 여러 번 실어 보내고 parse_opts 가 리스트로 모은다.
        out[f"{stage_name}.{key}"] = [str(x) for x in v] if isinstance(v, list) else str(v)
    return out


@app.post("/api/cli/token")
def api_cli_token(request: Request):
    """로컬 CLI 가 원격을 조작할 토큰을 발급한다 — 원문은 여기서 한 번만 나온다.

    **세션 인증 전용이다.** _require_uid 를 안 쓰고 _uid 를 직접 본다 — Bearer 로
    이걸 부를 수 있으면 유출된 토큰이 스스로를 갱신해 영구히 살아남는다.
    """
    uid = _uid(request)
    if uid is None:
        raise HTTPException(status_code=401, detail=UNAUTHORIZED)
    conn = store.connect()
    try:
        return {"ok": True, "token": store.issue_cli_token(conn, uid)}
    finally:
        conn.close()


@app.post("/api/ai/prompts")
def api_ai_prompts(body: dict = Depends(_body), project: str = Depends(_project_b),
                   c=Depends(BRAIN_B_PAID)):
    """AI에 물어볼 질문을 만들어 심는다 — 웹에는 `/capture add` 를 칠 채팅이 없다.

    이 화면이 비어 있던 이유가 그것이다: ai 단계는 ai_prompts 를 재료로 도는데
    대시보드 폼으로 만든 사이트는 그 표가 비어 있었고, [AI 인용 다시 확인]은
    "질문이 아직 없습니다"로 즉시 실패했다. 만들기만 하고 인용 확인은 돌리지
    않는다 — 그쪽이 돈 나가는 단계라 사용자가 눌러서 시작해야 한다.
    """
    # BRAIN_B_PAID 가 수집 런과 같은 env 를 두르고(유료 키는 서버가 댄다, paid_keys)
    # 그 유저의 brain 을 열었다 닫는다.
    try:
        rows = gen_prompts.suggest(project, n=int(body.get("limit") or 20), conn=c)
        if not rows:
            raise HTTPException(
                status_code=502,
                detail="질문을 하나도 만들지 못했습니다. 잠시 뒤 다시 시도하세요.")
        added = gen_prompts.save(c, project, rows)
    except RuntimeError as e:                 # 키 부재 등 — 사유를 그대로 화면에 보낸다
        raise HTTPException(status_code=503, detail=str(e))
    return {"ok": True, "added": added, "total": len(rows),
            "prompts": [r["prompt"] for r in rows[:5]]}


def _ai_prompts_view(c, project: str) -> dict:
    """질문 목록 한 벌 — 화면은 조회든 편집이든 이 모양만 받아 다시 그린다.

    limit 은 사이트 설정의 limits.max_ai_prompts 다. collect_ai 가 켜진 질문을
    그 수만큼만 물어보므로, 화면이 다른 수를 말하면 사용자는 자기가 켠 질문이
    조용히 빠지는 걸 보게 된다.
    """
    p = db.get_project(c, project)
    try:
        limit = int((db.project_cfg(c, p).get("limits") or {}).get("max_ai_prompts") or 30)
    except (TypeError, ValueError):
        limit = 30
    return {"prompts": [dict(r) for r in db.list_ai_prompts(c, p["id"])],
            "active_total": db.count_active_ai_prompts(c, p["id"]),
            "limit": limit}


@app.get("/api/ai/prompts")
def api_ai_prompts_list(project: str, c=Depends(BRAIN_Q)):
    """심긴 질문 목록. 인용 확인을 한 번 돌리기 전에는 이걸 볼 곳이 없었다 —
    만들기 버튼만 있고 무엇이 만들어졌는지는 화면 어디에도 안 나왔다."""
    return _ai_prompts_view(c, project)


@app.post("/api/ai/prompts/edit")
def api_ai_prompts_edit(b: dict = Depends(_body), project: str = Depends(_project_b),
                        c=Depends(BRAIN_B)):
    """질문 고치기·켜기·끄기·지우기·직접 추가.

    모델이 지은 질문은 초안이다 — 우리 업종과 상관없는 게 섞이고, 정작 물어야 할
    질문이 빠진다. 그걸 사람이 손볼 데가 없으면 [AI 인용] 화면의 수치는 엉뚱한
    질문의 성적표가 된다. 어느 op 든 돌려주는 건 목록 한 벌이다(화면은 다시 그린다).
    """
    op = str(b.get("op") or "")
    if op not in ("save", "active", "delete"):
        raise HTTPException(status_code=400, detail="화면이 보낸 값을 알아볼 수 없습니다. 새로고침한 뒤 다시 시도하세요.")
    try:      # 화면이 보내는 값이다 — 숫자가 아닌 게 오면 500 이 아니라 400 이다
        ids = [int(x) for x in (b.get("ids") or [])][:300]
        pid_edit = int(b["id"]) if b.get("id") else 0
    except (TypeError, ValueError):
        raise HTTPException(status_code=400,
                            detail="화면이 보낸 값을 알아볼 수 없습니다. 새로고침한 뒤 다시 시도하세요.")
    text = " ".join(str(b.get("prompt") or "").split())
    cat = str(b.get("category") or "").strip()
    if cat and cat not in (*gen_prompts.CATEGORIES, "general"):
        cat = "general"

    pid = db.get_project(c, project)["id"]
    view = _ai_prompts_view(c, project)
    if op == "save":
        if not (gen_prompts.MIN_LEN <= len(text) <= gen_prompts.MAX_LEN):
            raise HTTPException(
                status_code=400,
                detail=f"질문은 {gen_prompts.MIN_LEN}~{gen_prompts.MAX_LEN}자로 적어야 저장됩니다.")
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
            # 전역 핸들러(409)로 넘기지 않는다 — 여기서는 사용자가 방금 한 일을
            # 이름으로 말할 수 있다. 전역 쪽은 그 말을 못 지어내는 자리의 그물이다.
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
                    detail=f"켤 수 있는 질문은 {view['limit']}개까지입니다. 다른 질문을 먼저 끄세요.")
        db.set_ai_prompts_active(c, pid, ids, on)
    else:
        db.delete_ai_prompts(c, pid, ids)
    return {"ok": True, **_ai_prompts_view(c, project)}


@app.post("/api/run")
def api_run(body: dict = Depends(_body), project: str = Depends(_project_b),
            uid: int = Depends(_require_uid), conn=Depends(CONN_B),
            dispatch=Depends(_dispatch_dep)):
    """'지금 다시 재기' — /capture run 에 해당한다. 웹에는 명령을 칠 곳이 없으므로 버튼이 그 자리다.

    두 갈래다:
      groups  묶음 id (쉼표) — 화면의 [이 묶음 다시 재기]. 대기열에 올린다: 안 돌고 있으면
              워커를 띄우고(started), 도는 중이면 지금 런이 끝나는 대로 이어서 돈다(queued).
              워커가 대기열을 가져가기 전(몇 초)에 또 누른 것은 같은 런으로 합쳐진다.
              stages 도 groups 도 없으면 전체 재기(모든 묶음)다.
      stages  단계 몇 개만(/capture gsc, /capture ai …) — 부분 실행이라 주기 시계를 안
              건드리고, 도는 중이면 받지 않는다(예전 그대로).

    opts 는 원격 CLI 가 실어 보내는 단계별 노브다(`--opt rank.device=mobile`).
    응답: {"ok", "started", "queued"}.
    """
    stages = [x for x in str(body.get("stages") or "").split(",") if x]
    bad = [x for x in stages if x not in STAGES]
    if bad:
        raise HTTPException(status_code=400, detail=f"이 화면에서 돌릴 수 없는 단계입니다: {', '.join(bad)}. 새로고침한 뒤 다시 눌러 보세요.")
    if stages and body.get("groups"):
        raise HTTPException(status_code=400, detail="단계와 묶음을 한 번에 고를 수 없습니다.")
    try:
        # 묶음 이름의 정본은 run_all.GROUPS — 여기 사본을 두지 않는다.
        groups = [] if stages else run_all.group_names(body.get("groups") or "")
    except ValueError:
        raise HTTPException(status_code=400, detail="이 화면에서 다시 잴 수 없는 묶음입니다. "
                                                    "새로고침한 뒤 다시 눌러 보세요.")
    opts = _stage_opts(body.get("opts"))
    row = store.site(conn, uid, project)

    if stages:
        if row["running_since"]:
            return {"ok": True, "started": False, "queued": False}      # 이미 도는 중
        # 부분 실행은 주기 판정을 건드리지 않는다 — gsc 만 다시 읽었다고
        # 전체 재측정을 한 것으로 치면 다음 자동 런이 통째로 밀린다(mark_busy).
        store.mark_busy(conn, row["id"])
        # 새 런의 로그는 여기서부터다. 워커가 뜨기까지 몇 초가 걸리는데, 그 사이
        # 폴링이 지난 런 텍스트를 읽으면 사용자는 끝난 런을 지금 도는 런으로 읽는다.
        store.save_run_log(conn, row["id"], "")
        argv = ["--user", str(uid), "--project", project, "--only", ",".join(stages)]
        for k, v in opts.items():
            for one in (v if isinstance(v, list) else [v]):
                argv += ["--opt", f"{k}={one}"]
        dispatch(*argv)
        return {"ok": True, "started": True, "queued": False}

    phase = store.run_phase(row)
    if opts and phase != "idle":
        # 대기열에 합쳐지면 옵션이 갈 곳이 없다(그 런은 다른 요청이 띄웠다). 조용히 버리면
        # --device mobile 을 준 사용자가 데스크톱 결과를 모바일 결과로 읽는다.
        raise HTTPException(status_code=409, detail="지금 도는 수집이 있어 옵션을 실을 수 "
                                                    "없습니다. 끝난 뒤 다시 실행하세요.")
    store.queue_groups(conn, row["id"], groups)
    if phase == "running":
        # 지금 도는 런이 끝나면 워커가 대기열을 다시 보고 이어서 돈다(worker.run_site).
        return {"ok": True, "started": False, "queued": True}
    if not store.mark_pending(conn, row["id"]):
        # 대기 중(pending) — 워커가 아직 대기열을 안 가져갔다. 같은 런으로 합쳐진다: 새
        # 워커를 띄우지 않는다. 대기열에는 이미 올렸다.
        return {"ok": True, "started": True, "queued": False}
    store.save_run_log(conn, row["id"], "")
    argv = ["--user", str(uid), "--project", project, "--queue"]
    for k, v in opts.items():
        for one in (v if isinstance(v, list) else [v]):
            argv += ["--opt", f"{k}={one}"]
    # kick(--all) 로 보내지 않는다 — 스윕은 due 판정을 거치고 합치기 대기가 없다.
    dispatch(*argv)
    return {"ok": True, "started": True, "queued": False}


@app.get("/api/run/log")
def api_run_log(project: str, since: int = 0, uid: int = Depends(_require_uid),
                conn=Depends(CONN_Q)):
    """런 내레이션을 흘려준다 — 원격 CLI 가 폴링으로 받아 그대로 print 한다.

    문구를 클라이언트에서 다시 만들면 요약표가 두 벌이 된다. 그래서 서버 워커가
    뱉은 텍스트 자체를 보낸다. since 는 **문자 오프셋**이고 text 는 그 뒤부터다.
    """
    full = store.load_run_log(conn, uid, project)
    row = store.site(conn, uid, project)
    since = max(0, min(int(since or 0), len(full)))
    return {"text": full[since:], "next": len(full),
            "running": bool(row and row["running_since"])}


@app.post("/api/sql")
def api_sql(body: dict = Depends(_body), t=Depends(TENANT_B)):
    """`db.py sql` 의 원격판 — Claude 가 Brain 에 묻는 통로.

    가드(읽기 전용 커넥션 + SELECT/WITH 만)를 여기서 다시 만들지 않는다. db.run_sql
    을 그대로 부르고 그것이 stdout 에 찍는 JSON 을 되돌려준다 — 로컬과 응답 모양이
    같아야 SKILL.md 가 두 벌이 되지 않는다.
    """
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            db.run_sql(str(body.get("sql") or ""))
    except SystemExit as e:
        # run_sql 은 거절을 sys.exit(문구) 로 낸다 — 문구는 그대로 사용자에게 간다.
        raise HTTPException(status_code=400, detail=str(e))
    except sqlite3.Error as e:
        raise HTTPException(status_code=400, detail=f"질의를 실행하지 못했습니다: {e}")
    return json.loads(buf.getvalue())


@app.get("/api/brain")
def api_brain(t=Depends(TENANT)):
    """테넌트 brain.db 를 통째로 내려준다 — `remote.py pull` 이 로컬에 남기는 사본의 원본.

    파일을 그냥 읽으면 안 된다: 워커가 같은 파일에 쓰는 중이면 반쯤 쓰인 페이지가
    섞여 **찢어진 DB** 를 준다. sqlite 의 backup() 은 쓰기와 겹쳐도 일관된 스냅샷을
    떠 주므로, 임시 파일로 한 벌 뜬 뒤 그것을 보내고 전송이 끝나면 지운다.

    사이트별로 자르지 않는다 — 자르는 쪽은 로컬 병합기다(remote.merge). 여기서
    한 번 더 자르면 "이 사이트에 속한 행" 규칙이 두 벌이 된다.
    """
    src = db.db_path()
    if not src.exists():
        raise HTTPException(status_code=404,
                            detail="서버에 아직 보관함이 없습니다 — 먼저 한 번 측정해 주세요.")
    fd, tmp = tempfile.mkstemp(prefix="brain-", suffix=".db")
    os.close(fd)
    try:
        s = sqlite3.connect(src.resolve().as_uri() + "?mode=ro", uri=True)
        d = sqlite3.connect(tmp)
        try:
            s.backup(d)
        finally:
            d.close()
            s.close()
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return FileResponse(tmp, media_type="application/octet-stream", filename="brain.db",
                        background=BackgroundTask(lambda: Path(tmp).unlink(missing_ok=True)))


@app.post("/api/brain/import")
async def api_brain_import(request: Request, project: str, src: str,
                           t=Depends(TENANT_Q)):
    """로컬 플러그인이 잰 것을 서버 사이트에 **더한다** — `remote.py push` 의 서버쪽.

    /api/brain(내려받기)의 역방향인데 대칭은 아니다: 받을 때는 로컬 사본을 비우고 채우지만
    (remote.merge), 올릴 때는 서버 사이트가 이미 GSC 등 자기 측정치를 갖고 있으므로 지우지
    않고 얹는다(remote.graft — UNIQUE 로 같은 키워드·질문은 잇고, 같은 값의 행은 건너뛴다).
    본문은 sqlite 파일 그대로다. 소유 확인은 store.session 이 한다.
    """
    body = await request.body()
    if len(body) > 200 * 2**20:
        raise HTTPException(status_code=413, detail="보관함 파일이 200MB 를 넘습니다")
    if body[:15] != b"SQLite format 3":
        raise HTTPException(status_code=400, detail="sqlite 파일이 아닙니다")
    fd, tmp = tempfile.mkstemp(prefix="brain-import-", suffix=".db")
    os.close(fd)
    Path(tmp).write_bytes(body)
    try:
        counts = remote.graft(tmp, src, project)
    except LookupError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except sqlite3.Error as e:
        raise HTTPException(status_code=400, detail=f"병합하지 못했습니다: {e}")
    finally:
        Path(tmp).unlink(missing_ok=True)
    return {"ok": True, "counts": counts}


@app.get("/api/report")
def api_report(project: str, t=Depends(TENANT_Q)):
    """/capture report — 그 시점 화면을 자립형 HTML 로 박제해 내려준다."""
    data = Path(dashboard.export(project)).read_bytes()
    # 보고서도 호스팅 문구를 쓴다 — 메일로 나가는 것이라 받는 사람은 웹 사용자다.
    # /d 와 같은 이유로 표식을 **첫 <script> 앞**에 세운다(app.py 의 dash 주석 참고).
    # 백링크는 여기서 박아 넣지 않는다 — 수집이 capture 단계로 내려오면서
    # payload 에 실리고, [백링크] 화면이 로컬·호스팅·박제본에서 같이 그린다.
    data = pages.data(data.decode("utf-8"), SM_HOSTED=True).encode("utf-8")
    data += _REPORT_ADDON
    return Response(data, media_type="text/html; charset=utf-8", headers={
        "Content-Disposition": f'attachment; filename="{project}-report.html"'})


@app.get("/api/perf")
def api_perf(project: str, t=Depends(TENANT_Q)):
    """서치콘솔 4대 지표와 검색어·페이지·기기 분해 — 개요 화면이 쓴다."""
    return exports.perf(project)


@app.get("/api/keywords")
def api_keywords(project: str, status: str = "candidate", c=Depends(BRAIN_Q)):
    """추적 중(active)이거나 후보(candidate)인 키워드.

    후보는 자동완성에서 캔 것이라 관련성이 확인되지 않았다 — 자동 활성화는 서치콘솔에
    노출된 것만 켠다. 나머지는 사람이 보고 골라야 해서 이 목록이 필요하다.
    """
    pid = db.get_project(c, project)["id"]
    return {"keywords": [dict(r) for r in db.list_keywords(c, pid,
                                                           active=status == "active")],
            "active_total": db.count_active_keywords(c, pid),
            "limit": settings.count("SEOMINER_MAX_KEYWORDS")}


@app.post("/api/keywords")
def api_keywords_set(b: dict = Depends(_body), project: str = Depends(_project_b),
                     c=Depends(BRAIN_B)):
    """고른 키워드를 추적 세트에 넣거나 뺀다. 상한(limit)을 넘겨 켜지 않는다 —
    SERP 는 키워드당 과금이라 여기서 새면 비용이 샌다."""
    ids = [int(x) for x in (b.get("ids") or [])][:500]
    on = bool(b.get("active"))
    if not ids:
        raise HTTPException(status_code=400, detail="추가하거나 해제할 키워드를 먼저 고르세요.")
    pid = db.get_project(c, project)["id"]
    if on:
        limit = settings.count("SEOMINER_MAX_KEYWORDS")
        ids = _capped(ids, limit, db.count_active_keywords(c, pid))
        if not ids:
            raise HTTPException(
                status_code=409,
                detail=f"추적 키워드가 한도 {limit}개를 채웠습니다. [추적 중] 탭에서 몇 개를 해제한 뒤 다시 추가하세요.")
    changed = db.set_keywords_active(c, pid, ids, on)
    return {"ok": True, "changed": changed,
            "active_total": db.count_active_keywords(c, pid)}


@app.get("/api/run/status")
def api_run_status(uid: int = Depends(_require_uid), conn=Depends(CONN)):
    """사이트별 수집 상태 — 화면이 폴링한다."""
    # last_ok/last_error 를 같이 싣는다 — 실패한 단계가 있어도 화면이 아무 말도
    # 안 하던 자리다. 폴링이 이미 도는 곳이라 새 라우트를 만들지 않는다.
    #
    # 묶음 런은 단계 여럿이 동시에 돈다 — stage 는 그중 첫째(옛 화면이 읽는 칸), stages 는
    # 전부다. groups 는 묶음별 {running, queued, last_run_at, due} — 화면이 메뉴의 점과
    # 묶음 머리의 [다시 재기] 상태를 그린다(묶음 표의 정본은 run_all.GROUPS).
    every = scheduler.every_hours()
    out = {}
    for r in store.sites(conn, uid):
        running = [x for x in (r["stage"] or "").split(",") if x]
        out[r["project"]] = {"running": bool(r["running_since"]),
                             "last_run_at": r["last_run_at"],
                             "stage": running[0] if running else None,
                             "stages": running,
                             "pct": r["stage_pct"],
                             "last_ok": r["last_ok"],
                             "last_error": r["last_error"],
                             "groups": store.group_status(conn, r, every)}
    return out


# 자동 수집 주기 프리셋 — 값(시간)과 화면에 쓸 이름. 목록의 정본은 여기다:
# 화면은 이걸 받아 그대로 그리고, 저장은 이 안의 값만 받는다(화면이 보낸 값을 안 믿는다).
# 0 은 '자동 재측정만 끔' — 첫 측정과 [전체 다시 재기]는 그대로 돈다(store.due_sites).
RUN_PRESETS = ((0, "끔"), (6, "6시간"), (12, "12시간"), (24, "하루"), (72, "3일"),
               (168, "주 1회"))


# 사이트 설정 화면(#sm-set)의 "이 사이트 설정" 칸이 다루는 프로필 필드.
# 전부 Brain 안에 산다: brand_aliases·tools 는 project_settings, seed_keywords·
# competitors_manual 은 keywords(source='seed')·competitors(source='manual') 행이다.
# 예전엔 넷 다 프로젝트 yaml 에만 있었고, 그 파일이 호스팅에서는 컨테이너 디스크에
# 살아서 동기화로는 영영 안 내려왔다(db.project_cfg 주석).
# 정본은 db.py 의 SETTING_KEYS/PREFILL_KEYS 와 같은 이름이어야 한다.
PROFILE_FIELDS = ("brand_aliases", "seed_keywords", "competitors_manual", "tools")


def _project_cfg(c, pr) -> dict:
    """사이트별 설정 — Brain 한 곳(db.project_cfg). 등록 직후면 빈 값이다."""
    return db.project_cfg(c, pr) if pr else {}


def _joined(cfg: dict, key: str) -> str:
    v = cfg.get(key) or []
    return ", ".join(v) if isinstance(v, list) else str(v)


@app.get("/api/settings")
def api_settings(project: str, uid: int = Depends(_require_uid),
                 tn=Depends(TENANT_Q), c=Depends(BRAIN_Q)):
    """사이트별 설정 — 수집 주기·언어-지역·GA4·이 사이트 프로필(브랜드·경쟁사·도구·씨앗).

    화면이 그리는 칸이 곧 이 응답의 키다. 화면에 없는 값을 여기 싣지 않는다 —
    저장소(GitHub) 키가 그렇게 남아 화면이 없는 칸을 그리려 했다.
    """
    domain = ""
    try:
        pr = db.get_project(c, project)
        ga4, locale, domain = pr["ga4_property"] or "", db.project_locale(pr), pr["domain"]
        cfg = _project_cfg(c, pr)
        # 씨앗·경쟁사는 설정 줄이 아니라 **행**이 정본이다 — keywords(source='seed')·
        # competitors(source='manual'). 여기서 사본을 들고 있으면 화면이 지운 값이
        # 다음 열람에 되살아난다(예전 yaml 이 그랬다).
        seeds = db.seed_keywords(c, pr["id"])
        rivals = db.manual_competitors(c, pr["id"])
    except db.ProjectNotFound:
        # 전역 404 핸들러로 넘기지 않는다 — 등록 직후 Brain 이 아직 없어도 설정
        # 화면은 열려야 한다. 여기서만 '없음'이 정상이다.
        ga4, locale, cfg, seeds, rivals = "", "", {}, [], []
    brand_aliases = _joined(cfg, "brand_aliases")
    # 브랜드 표기가 비어 있으면 도메인 앞부분을 초안으로 얹는다 — 검색어 화면이
    # "브랜드 표기가 비었습니다"라고 말해도 여기 채울 칸이 없던 것이 원인이다.
    # 초안은 골라 둔 값이 아니다: 화면이 이걸 입력칸 값이 아니라 placeholder/제안으로
    # 보여 주고, 사람이 [저장]을 눌러야 실제로 저장된다 — 조용히 저장하지 않는다.
    brand_suggestion = "" if brand_aliases else domain.split(".")[0] if domain else ""
    # 사이트 값이 없으면 전역 기본값이 실효값이다 — 화면은 그게 골라진 것으로 그린다.
    return {"run_every_hours": store.every_hours(tn.conn, uid, project),
            "presets": [{"h": h, "label": t} for h, t in RUN_PRESETS],
            "ga4_property": ga4,
            # 언어-지역 — 값과 고를 수 있는 목록(정본 serp_adapter.LOCALES)을 같이 준다
            "locale": locale,
            "locales": [{"code": code, "label": t} for code, t in serp_adapter.LOCALES],
            "brand_aliases": brand_aliases,
            "brand_suggestion": brand_suggestion,
            "seed_keywords": ", ".join(seeds),
            "competitors_manual": ", ".join(rivals),
            "tools": _joined(cfg, "tools")}


@app.post("/api/settings")
def api_settings_set(body: dict = Depends(_body), project: str = Depends(_project_b),
                     uid: int = Depends(_require_uid), t=Depends(TENANT_B),
                     c=Depends(BRAIN_B)):
    """수집 주기 또는 언어-지역 하나를 저장한다.

    예전에는 두 갈래가 store.session 을 각각 열었다(주기 쪽은 테넌트도 안 둘렀다).
    의존자로 옮기면서 한 벌로 합쳤다 — 요청 하나에 서버 커넥션도 하나다. 주기만
    바꾸는 호출에서도 brain 이 열리는데, 같은 화면을 그리는 GET /api/settings 가
    이미 그렇게 열고 있다(등록 직후 빈 brain.db 가 생기는 것도 거기서부터다).
    """
    if "locale" in body:      # 언어-지역만 바꾸는 호출 — 수집 주기는 안 건드린다
        loc = str(body.get("locale") or "")
        if loc not in dict(serp_adapter.LOCALES):
            raise HTTPException(status_code=400,
                                detail="고를 수 없는 언어-지역입니다. 새로고침한 뒤 다시 고르세요.")
        db.set_locale(c, db.get_project(c, project)["id"], loc)
        return {"ok": True, "locale": loc}
    if "profile" in body:     # 브랜드 표기·경쟁사 도메인·도구·씨앗 — yaml 에만 있는 값
        return _api_settings_profile(body.get("profile"), project, c)
    try:
        hours = float(body.get("run_every_hours"))
    except (TypeError, ValueError):
        hours = None
    if hours not in {float(h) for h, _ in RUN_PRESETS}:
        raise HTTPException(status_code=400,
                            detail="고를 수 없는 수집 주기입니다. 새로고침한 뒤 다시 고르세요.")
    store.set_every_hours(t.conn, uid, project, hours)
    return {"ok": True, "run_every_hours": hours}


def _api_settings_profile(profile, project: str, c) -> dict:
    """PROFILE_FIELDS 중 보낸 것만 Brain 에 쓴다.

    예전에는 프로젝트 yaml 을 고치고 db.sync_project 를 다시 돌렸다. 그 파일이
    호스팅에서는 컨테이너 디스크에 살아서, 저장은 되는데 그 값이 사용자의 PC 로는
    영영 안 내려갔다 — 동기화가 나르는 것은 Brain 뿐이기 때문이다(db.project_cfg 주석).
    이제 설정도 Brain 안이라 저장한 것이 그대로 따라간다.

    스키마를 아는 곳은 db.py 뿐이라 여기서 SQL 을 새로 짜지 않는다. 씨앗·경쟁사는
    **보낸 목록이 곧 그 사이트의 목록이다** — 뺀 값은 지워진다. 예전 INSERT OR IGNORE
    시절에는 안 지워져서, 화면에서 지워도 다음 열람에 그대로 되살아났다.
    """
    if not isinstance(profile, dict):
        raise HTTPException(status_code=400, detail="profile 은 {필드: 값} 모양이어야 합니다.")
    pr = db.get_project(c, project)
    edit = {k: [s.strip() for s in re.split(r"[,\n]", str(profile.get(k) or "")) if s.strip()]
            for k in PROFILE_FIELDS if k in profile}
    db.register_project(c, {**{f: pr[f] for f in ("name", "type", "domain", "locale",
                                                  "gsc_property", "ga4_property")}, **edit})
    cfg = db.project_cfg(c, pr)
    return {"ok": True,
            "brand_aliases": _joined(cfg, "brand_aliases"), "tools": _joined(cfg, "tools"),
            "seed_keywords": ", ".join(db.seed_keywords(c, pr["id"])),
            "competitors_manual": ", ".join(db.manual_competitors(c, pr["id"]))}


# --- GA4 ------------------------------------------------------------------
# GSC 와 달리 GA4 속성 ID 는 도메인에서 유추가 안 된다(숫자 ID 이고 도메인과 매핑이
# 없다) — 그래서 목록에서 사람이 직접 고른다. list_properties/suggest_property 는
# collect_ga4.py 가 이미 갖고 있다(여기서 새로 안 짠다).

@app.get("/api/ga4/properties")
def api_ga4_properties(project: str, c=Depends(BRAIN_Q)):
    """GA4 속성 후보 — [속성 고르기]를 누를 때만 부른다(관리자 API 호출이라 설정을
    열 때마다 부르지 않는다). 도메인·이름이 겹치는 것을
    제안으로 얹지만 확정은 사람이 한다 — 엉뚱한 속성이 붙으면 그 뒤 모든 숫자가
    조용히 거짓이 된다(collect_ga4.suggest_property 참고).

    403 은 둘 중 하나다 — 실제로 터진 건 ①(Railway 로그, 2026-08-31): 토큰이 아예
    없어서 collect_gsc._oauth_credentials() 가 브라우저를 열려다 죽었다. ②는 이미
    로그인은 했지만 GA4 스코프가 나중에 추가돼 그 전 토큰엔 없는 경우다. 우선순위
    ①→②로 먼저 본다 — 부르기 전에 _require_google() 이 그 둘을 함께 확인한다
    (같은 검사가 /api/properties 에도 필요했다 — 라우트마다 두면 새 라우트가 빠진다). 그래도 남는 예외(SystemExit 포함, collect_gsc 쪽 CLI 최종 안내가
    새는 경우)는 사람 말로 바꾼다 — 스택트레이스가 화면에 그대로 나가면 안 된다.
    """
    from googleapiclient.errors import HttpError
    need_relogin = HTTPException(status_code=403, detail=RELOGIN_FOR_GA4)
    try:
        domain = db.get_project(c, project)["domain"]
        _require_google()
        try:
            _, admin_svc = collect_ga4.get_service()
            props = collect_ga4.list_properties(admin_svc)
        except HttpError as e:
            if getattr(e.resp, "status", None) == 403:
                raise need_relogin
            raise
    except db.ProjectNotFound:
        raise            # 아래 BaseException 이 삼키기 전에 전역 404 핸들러로 보낸다
    except HTTPException:
        raise
    except BaseException as e:
        traceback.print_exc()
        raise HTTPException(
            status_code=502,
            detail="GA4 가 응답하지 않아 속성 목록을 못 읽었습니다. 잠시 뒤 다시 시도하세요.") from e
    return {"properties": props,
           "suggested": [p["id"] for p in collect_ga4.suggest_property(props, domain)]}


@app.post("/api/ga4/property")
def api_ga4_set(b: dict = Depends(_body), project: str = Depends(_project_b),
                c=Depends(BRAIN_B)):
    """GA4 속성 연결 — 목록에서 사람이 고른 것만 저장한다(자동 확정 없음).
    property_id 는 숫자 ID 문자열로만 저장한다 — 'properties/' 접두는 collect_ga4 가
    API 부를 때 붙인다(db.py 의 컬럼 주석과 같은 계약)."""
    prop_id = str(b.get("property_id") or "").strip()
    if prop_id and not prop_id.isdigit():
        raise HTTPException(status_code=400,
                            detail="알아볼 수 없는 속성입니다. 새로고침한 뒤 다시 고르세요.")
    db.set_ga4_property(c, db.get_project(c, project)["id"], prop_id)
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


# 등록은 Starlette 의 HTTPException 으로 한다 — 없는 경로의 404 는 라우터가 그걸
# 던지고, FastAPI 의 HTTPException(그 하위 클래스)으로 걸면 안 잡힌다.
@app.exception_handler(StarletteHTTPException)
async def _http_exception(request: Request, e: StarletteHTTPException):
    """/d 는 화면(HTML)이다 — _require_uid 가 던지는 401 을 그대로 흘리면 스타일 없는
    JSON {"detail":...} 만 뜬다. _require_uid 자체는 /api/* 가 계속 JSON 401 을 써야
    하므로 안 바꾸고, 여기서 /d 하나만 처음 화면(/)으로 302 돌린다. hash(#사이트)는
    서버로 안 오므로 리다이렉트로 잃는 것이 없다. 나머지는 FastAPI 기본 처리 그대로."""
    if request.url.path == "/d" and e.status_code == 401:
        return RedirectResponse("/?login=required", status_code=302)
    # 사람이 연 주소(오타·옛 링크)가 없으면 JSON 한 줄 대신 돌아갈 길이 있는 화면을 준다.
    # /api/* 는 클라이언트가 detail 을 읽으므로 JSON 그대로.
    if e.status_code == 404 and not request.url.path.startswith("/api/"):
        return HTMLResponse(
            pages.document(_not_found(_uid(request) is not None),
                           "페이지를 찾을 수 없습니다 — seo-miner"),
            status_code=404)
    return await http_exception_handler(request, e)


# 머리 줄은 랜딩 헤더(.hbar·.mark)와 같은 판·같은 서체다 — 서체를 안 불러 로고가
# 폴백 등폭으로 벌어지고 머리가 없어 다른 사이트처럼 보였다(8회차).
# 오른쪽 버튼은 로그인 상태를 본다 — 세션에 uid 가 있는 사람에게 "Google로
# 시작"을 또 보여 주면 이미 로그인했다는 걸 의심하게 만든다(9회차). 로그인
# 이면 "/" 의 "내 사이트" 목록으로 보낸다 — 다시 로그인을 거치지 않는다.
def _not_found(logged_in: bool) -> str:
    action = ('<a href="/" style="font:500 13px/1 sans-serif;color:#101513;background:#57B49C;'
              'border-radius:3px;padding:0 15px;min-height:40px;display:inline-flex;align-items:center;'
              'text-decoration:none">내 사이트</a>') if logged_in else (
              '<a href="/auth/login" style="font:500 13px/1 sans-serif;color:#101513;background:#57B49C;'
              'border-radius:3px;padding:0 15px;min-height:40px;display:inline-flex;align-items:center;'
              'text-decoration:none">Google로 시작</a>')
    return (
    '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@600&display=swap">'
    '<body style="margin:0;background:#E6E9E4">'
    '<header style="background:#121714"><div style="max-width:1120px;margin:0 auto;padding:12px 24px;'
    'display:flex;align-items:center;gap:16px">'
    '<a href="/" style="font:600 15px/1 \'IBM Plex Mono\',ui-monospace,monospace;letter-spacing:.06em;'
    'color:#E6E9E4;text-decoration:none">seo<b style="color:#57B49C;font-weight:600">·</b>miner</a>'
    '<span style="flex:1"></span>'
    + action + '</div></header>'
    '<main style="font:15px/1.7 -apple-system,BlinkMacSystemFont,\'Malgun Gothic\',sans-serif;'
    'color:#121714;max-width:36rem;margin:0 auto;padding:12vh 24px 0;word-break:keep-all">'
    '<h1 style="font-size:23px;margin:0 0 8px">페이지를 찾을 수 없습니다</h1>'
    "<p>주소가 바뀌었거나 잘못 적혔습니다. seo·miner 는 서치콘솔 숫자로 다음에 고칠 "
    "검색어를 고르는 도구입니다.</p>"
    '<p style="display:flex;gap:16px;align-items:center;flex-wrap:wrap;margin-top:20px">'
    '<a href="/" style="display:inline-flex;align-items:center;min-height:44px;padding:0 18px;'
    'border-radius:4px;background:#22705F;color:#fff;text-decoration:none;font-weight:600">처음으로</a>'
    '</p></main>')


@app.get("/d")
def dash(uid: int = Depends(_require_uid)):
    # 호스팅에서는 API 키·의존성 설치·구글 클라이언트 등록이 유저 몫이 아니다(서버가 키를 댄다).
    # "hosted" 조립본이 window.SM_HOSTED=true 를 페이지 스크립트보다 먼저 세우고
    # 호스팅 전용 섹션을 포함해 조립한다(dashboard.assemble).
    return HTMLResponse(dashboard.assemble("hosted") + _DASH_ADDON.decode("utf-8"))


@app.get("/api/projects")
def api_projects(uid: int = Depends(_require_uid), conn=Depends(CONN)):
    """존재는 dashboard.ROUTES 가 로컬 Handler 와 함께 못 박는다(둘 다 화면이 부른다).
    몸은 다르다: 호스팅은 사이트 소유를 Brain 동기화 여부와 무관하게 store.sites
    (등록 즉시 반영)로 판정해야 해서, 표의 call(Brain 의 projects 테이블)을 못 쓴다."""
    return [r["project"] for r in store.sites(conn, uid)]


@app.get("/api/doctor")
def api_doctor(project: str, full: bool = False, t=Depends(TENANT_Q_PAID)):
    """화면은 평평한 요약(setup_state)을, 원격 CLI 는 진단 전문(diagnose)을 받는다.

    두 모양은 다르다 — `doctor.render()` 가 capabilities·gsc_sites 를 읽으므로
    setup_state 를 먹이면 KeyError 로 죽는다. 화면 쪽 기본값은 건드리지 않는다.
    화면 쪽 본체는 dashboard.ROUTES 것 — 로컬 Handler 가 부르는 것과 같은 함수다.

    아래 공유 라우트 루프에 못 넣는 이유가 이 둘이다. ?full= 은 호스팅에만 있는
    갈래다(로컬은 /api/doctor 를 프록시하지 않고 늘 요약만 준다 — NEVER_PROXY).
    그리고 TENANT_Q_PAID 로 수집 런과 **같은 env** 를 두르고 진단한다 — 서버가 대는
    유료 키가 여기서도 보여야 doctor 가 "키 없음"이라고 거짓 판정하지 않는다.
    paid_keys() 가 세우는 표식(SEOMINER_HOSTED)이 준비물의 owner 도 서버로 뒤집는다.
    """
    if full:
        return doctor.diagnose(project)
    return dashboard.ROUTES[("GET", "/api/doctor")](project, {}, None)


@app.post("/api/creation/merged")
def api_creation_merged(b: dict = Depends(_body), project: str = Depends(_project_b),
                        c=Depends(BRAIN_B)):
    """작업 기록(creations)의 병합 표시를 켠다 — 로컬 `createdb.py merged` 의 호스팅 짝.

    이 창구가 없던 동안 호스팅 사이트의 기록은 영영 '병합 전'이었다: `createdb.py
    sync` 가 머지된 PR 을 찾아 기회는 /api/opp 로 닫았지만, 기록을 닫을 데가 없어
    다음 sync 가 같은 PR 을 gh 에 다시 물었다. 로컬은 db.mark_creation_merged 를
    바로 부르므로 로컬 경로에는 이 라우트가 필요 없다 — 그래서 dashboard.ROUTES
    (로컬·호스팅 공용 표)가 아니라 여기 호스팅 전용으로 선다.

    **가리키는 것은 기록 번호(id)** 다. 브랜치로 가리키면 같은 브랜치에 기록이 여럿일
    때 어느 것을 켠 건지 부르는 쪽이 모른다 — 기록 번호는 /api/data 페이로드의
    creations[].id 로 이미 부르는 쪽 손에 있다(로컬이 mark_creation_merged 에 넘기는
    것과 같은 번호다).

    남의 기록은 못 켠다. 번호만으로 UPDATE 하면 같은 테넌트의 **다른 사이트** 기록이
    번호 하나로 켜진다(사이트 소유 확인은 이 유저가 project 를 갖고 있다까지만 본다).
    그래서 켤 수 있는 것의 정본을 db.unmerged_creations(project_id) — 머지 확인의
    대상 목록 그 자체 — 로 두고, 그 안에 없는 번호는 404 다. 이미 켜진 기록도 여기
    안 들어오므로 404 이고, 그게 맞다: 부르는 쪽은 페이로드에서 merged=0 인 것만
    보내므로 정상 경로에서는 두 번 오지 않는다.
    """
    try:
        cid = int(b.get("id") or 0)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="화면이 보낸 값을 알아볼 수 없습니다. 새로고침한 뒤 다시 시도하세요.")
    pid = db.get_project(c, project)["id"]
    if cid not in {r["id"] for r in db.unmerged_creations(c, pid)}:
        raise HTTPException(status_code=404,
                            detail="이 사이트의 병합 전 작업 기록이 아닙니다.")
    db.mark_creation_merged(c, cid)
    return {"creation_id": cid, "merged": True}


# --- 공유 라우트 -----------------------------------------------------------------
# 두 서버가 함께 서빙하는 라우트는 dashboard.ROUTES 가 정본이다(ADR 0003). 예전엔
# 여기서 다섯 개를 손으로 감쌌다 — 새 공유 라우트를 표에 넣으면 로컬에만 서고,
# 호스팅은 누가 여기 한 벌 더 적을 때까지 404 였다. 이제 표를 돌며 등록한다:
# 표에 한 줄 넣으면 호스팅에도 저절로 선다.
#
# 제네릭 핸들러가 call 에 넘기는 모양은 로컬 Handler(do_GET/do_POST)와 같다 —
#   GET  : query 는 쿼리스트링을 첫 값으로 편 dict(project 는 빼서 따로), body 는 None
#   POST : query 는 {}, body 는 본문 JSON(_body — 한 요청에 한 번만 푼다)
# POST 의 project 자리에는 본문 project 를 넣는다(로컬은 "" 를 넘긴다). 표의 POST
# call 은 어차피 본문에서 읽으므로 값은 안 쓰이지만, 넘긴다면 방금 소유를 확인한
# 이름이 맞다.
#
# 인증·테넌트는 손으로 쓰던 것과 같은 의존자다: GET 은 TENANT_Q_PAID(?project= 의 소유
# 확인 + 호스팅 표식), POST 는 TENANT_B(본문 project 의 소유 확인). 둘 다 _require_uid 를 거친다 —
# test_app.py 가 라우트 표를 훑어 그걸 본다.
#
# /api/opp 도 이제 TENANT_B 다. 예전엔 혼자 TENANT(사이트를 안 가림)였는데, 그 자리
# 주석이 말하던 이유는 X-Token 헤더를 무시한다는 것뿐이었다 — 그건 호스팅 POST 전부가
# 그렇다(세션 로그인이 CSRF 를 막는다). 부르는 쪽(화면·createdb claim·run_tool)은
# 전부 본문에 project 를 싣는다. 달라지는 건 **자기 사이트가 아닌 project** 로 부른
# 요청뿐이다 — 전엔 그 유저 brain 의 아무 기회나 바꿨고, 이제 다른 POST 처럼 404 다.
#
# 손으로 남긴 것 — 호스팅 동작이 실제로 다른 둘뿐이다:
#   /api/projects — 소유를 store.sites 로 판정한다(표의 call 은 Brain 을 본다)
#   /api/doctor   — ?full= 갈래와 유료 키 env(TENANT_Q_PAID)
_HAND_ROUTES = {("GET", "/api/projects"), ("GET", "/api/doctor"),
                ("POST", "/api/run"), ("GET", "/api/run/status")}   # 묶음 런·대기열은 호스팅 몫

# 값 검증 실패(ValueError → 400)의 문구. 엔진 문구(`status must be one of ...`)는
# 개발자 말이라 화면에 그대로 내보내지 않는다. 여기 없는 경로는 str(e) 그대로 간다
# (/api/creation — 로컬 Handler 도 그렇게 낸다).
_BAD_VALUE = {
    "/api/opp": "알아볼 수 없는 상태값입니다. 새로고침한 뒤 다시 시도하세요.",
    "/api/verdict": "알아볼 수 없는 판정값입니다. 새로고침한 뒤 다시 시도하세요.",
}


def _shared_route(method: str, path: str) -> None:
    """dashboard.ROUTES 항목 하나를 호스팅에 세운다.

    call 은 요청 때 표에서 꺼낸다(등록 때 붙잡지 않는다) — 로컬 Handler 도 요청마다
    표를 조회하고, 검사가 표의 항목을 갈아끼워 넘어가는 모양을 볼 수 있다.

    예외는 로컬 Handler 와 같은 규칙으로 옮긴다: 없는 사이트는 전역 핸들러(404),
    POST 에서 LookupError 는 404(record_creation_route — 번호로 남의 Brain 을 더듬는
    것), ValueError 는 400. GET 은 로컬도 ProjectNotFound 만 잡는다.
    """
    key = (method, path)
    if method == "GET":
        # GET 은 읽기뿐이지만 PAID 로 두른다 — 페이로드의 안내(stage.from_progress)가
        # 호스팅 표식(SEOMINER_HOSTED)으로 갈래를 고르는데, 표식 밖에서 돌면 호스팅
        # 사용자에게 "OpenRouter 키를 넣으면 켜집니다"라고 말했다. /api/doctor 와 같은 env.
        def endpoint(request: Request, project: str = Depends(_project_q),
                     t=Depends(TENANT_Q_PAID)):
            # 로컬의 parse_qs 와 같게 편다 — 빈 값은 버리고 첫 값을 쓴다
            # (dict(request.query_params) 는 빈 값을 남기고 마지막 값을 준다)
            q = request.query_params
            query = {k: vs[0] for k in q if k != "project"
                     if (vs := [v for v in q.getlist(k) if v])}
            return dashboard.ROUTES[key](project, query, None)
    elif method == "POST":
        def endpoint(body: dict = Depends(_body), project: str = Depends(_project_b),
                     t=Depends(TENANT_B)):
            try:
                return dashboard.ROUTES[key](project, {}, body)
            except LookupError as e:
                raise HTTPException(status_code=404, detail=str(e))
            except ValueError as e:
                raise HTTPException(status_code=400, detail=_BAD_VALUE.get(path, str(e)))
    else:   # 로컬 Handler 는 do_GET/do_POST 뿐이다 — 다른 메서드는 표에 있을 수 없다
        raise ValueError(f"dashboard.ROUTES 에 모르는 메서드: {key}")
    # 이름은 손으로 쓰던 함수 이름 그대로(api_data …) — OpenAPI operationId 가 안 바뀐다
    app.add_api_route(path, endpoint, methods=[method],
                      name="api_" + path.rsplit("/", 1)[-1])


for _key in dashboard.ROUTES:
    if _key not in _HAND_ROUTES:
        _shared_route(*_key)
