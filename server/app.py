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
import subprocess
import time
from contextlib import asynccontextmanager
from typing import Optional
from urllib.parse import quote, urlparse

import google.auth.transport.requests
import google.oauth2.id_token
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from google_auth_oauthlib.flow import Flow
from starlette.middleware.sessions import SessionMiddleware

import backlinks
import collect_gsc
import dashboard
import db
import exports
import gh
import store
import writer

OAUTH_REDIRECT_URI = os.environ.get("OAUTH_REDIRECT_URI", "http://localhost:8000/auth/callback")

# 고정 기본값을 두면 프로덕션에서 그대로 떠서 세션을 위조당한다. 없으면 랜덤 —
# 재시작 때 세션이 끊길 뿐이고, 끊기는 게 위조당하는 것보다 낫다.
SESSION_SECRET = os.environ.get("SESSION_SECRET") or secrets.token_urlsafe(32)

# --- 주기 실행 ---------------------------------------------------------------
# Railway 볼륨은 서비스 하나에만 붙는다 — 크론을 별도 서비스로 빼면 이 서비스의 /data 를
# 못 본다. 그렇다고 웹 프로세스 안에서 직접 수집하면 store.tenant() 가 갈아끼우는 전역
# os.environ 을 동시에 들어온 웹 요청이 같이 보게 된다(남의 테넌트). 그래서 subprocess 다.

def _run_every_hours() -> float:
    """0 이면 자동 수집을 끈다. 호출 시점에 읽는다 — 얼려두면 env 를 바꿔도 안 먹는다."""
    try:
        return float(os.environ.get("SEOMINER_RUN_EVERY_HOURS", "168"))
    except ValueError:
        return 168.0


def _pending() -> int:
    """지금 잴 사이트 수. 주기 판정은 사이트별로 DB 가 한다 — 전역 스탬프 파일을 쓰면
    먼저 등록한 사이트가 도장을 찍어서 방금 가입한 사람의 첫 측정이 통째로 밀렸다."""
    conn = store.connect()
    try:
        return len(store.due_sites(conn, every_hours=_run_every_hours()))
    finally:
        conn.close()


def _run_worker() -> None:
    subprocess.run([sys.executable, str(ROOT / "server" / "worker.py"), "--all"],
                   cwd=str(ROOT), timeout=3 * 3600)


def _spawn_worker(args: list[str]) -> None:
    try:
        subprocess.Popen([sys.executable, str(ROOT / "server" / "worker.py"), *args],
                         cwd=str(ROOT))
    except Exception as e:
        print(f"[worker] 실행 실패: {e}", flush=True)


def _kick() -> None:
    """등록 직후 곧바로 수집을 띄운다 — 틱을 기다리면 그동안 화면이 비어 있다.
    워커가 사이트마다 시작 전에 도장을 찍으므로 스케줄러 틱과 겹쳐도 중복되지 않는다."""
    _spawn_worker(["--all"])                       # 실패해도 다음 틱이 잡는다


async def _scheduler() -> None:
    # 60초 틱 — 등록 직후 첫 측정이 곧바로 잡혀야 온보딩이 성립한다. 잴 게 없으면
    # DB 조회 한 번으로 끝나므로 틱이 잦아도 싸다.
    while True:
        await asyncio.sleep(60)
        try:
            if await asyncio.to_thread(_pending):
                await asyncio.to_thread(_run_worker)
        except Exception as e:                # 스케줄러가 죽으면 자동 수집이 조용히 멈춘다
            print(f"[scheduler] 실패: {e}", flush=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(_scheduler())
    yield
    task.cancel()


app = FastAPI(lifespan=lifespan)
app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    https_only=OAUTH_REDIRECT_URI.startswith("https://"),
)

SCOPES = collect_gsc.SCOPES + ["openid",
                               "https://www.googleapis.com/auth/userinfo.email"]


def _client_id() -> str:
    """import 시점이 아니라 호출 시점에 읽는다 — 얼려두면 env 없이 뜬 서버가
    빈 client_id 로 조용히 굴러가고, 유저가 로그인을 눌러야 실패를 안다."""
    cid = os.environ.get("GOOGLE_CLIENT_ID")
    if not cid:
        raise HTTPException(status_code=500, detail="GOOGLE_CLIENT_ID 가 설정되지 않았습니다")
    return cid


def _flow() -> Flow:
    secret = os.environ.get("GOOGLE_CLIENT_SECRET")
    if not secret:
        raise HTTPException(status_code=500, detail="GOOGLE_CLIENT_SECRET 이 설정되지 않았습니다")
    config = {
        "web": {
            "client_id": _client_id(),
            "client_secret": secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [OAUTH_REDIRECT_URI],
        }
    }
    return Flow.from_client_config(config, scopes=SCOPES,
                                   redirect_uri=OAUTH_REDIRECT_URI)


def _uid(request: Request) -> Optional[int]:
    return request.session.get("uid")


def _require_uid(request: Request) -> int:
    uid = _uid(request)
    if uid is None:
        raise HTTPException(status_code=401, detail="login required")
    return uid


def _page(name: str) -> str:
    return (ROOT / "server" / name).read_text("utf-8")


def _html(body: str, title: str = "seo-miner") -> HTMLResponse:
    return HTMLResponse(
        '<!doctype html><html lang="ko"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{html.escape(title)}</title></head><body>{body}</body></html>")


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/")
def home(request: Request):
    uid = _uid(request)
    if uid is None:
        return _html(_page("landing.html"), "seo-miner — 검색·AI 답변 가시성 측정")
    conn = store.connect()
    try:
        rows = store.sites(conn, uid)
    finally:
        conn.close()
    # create_project 는 name 만 정규식으로 검증한다 — domain·gsc_property 는 그대로
    # 저장되므로 <script> 가 들어올 수 있다.
    e = html.escape
    if rows:
        # 등록 직후에는 볼 데이터가 없다. 무슨 일이 일어나는 중인지 말해 주지 않으면
        # 빈 대시보드만 보고 떠난다.
        items = "".join(
            f'<li><a href="/d"><span class="nm">{e(r["project"])}</span>'
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
    taken = json.dumps([r["gsc_property"] for r in rows], ensure_ascii=False)
    site_list = json.dumps([{"project": r["project"], "repo": r["repo"]} for r in rows],
                           ensure_ascii=False)
    page = (_page("app.html")
            .replace("<!--USER-->", user_bar)
            .replace("<!--SITES-->", block)
            .replace("<script>",
                     f"<script>window.__TAKEN__={taken};window.__SITES__={site_list};", 1))
    return _html(page, "내 사이트 — seo-miner")


@app.get("/auth/login")
def auth_login(request: Request):
    flow = _flow()
    state = secrets.token_urlsafe(16)
    request.session["oauth_state"] = state
    # include_granted_scopes 는 쓰지 않는다 — 과거에 승인해 둔 스코프(webmasters 쓰기 등)까지
    # 토큰에 합쳐진다. 여기는 읽기만 필요하다.
    # select_account 가 없으면 로그아웃해도 구글이 직전 계정으로 그냥 들여보내서
    # 계정 전환이 성립하지 않는다. consent 는 refresh token 을 받기 위해 유지한다.
    url, _ = flow.authorization_url(access_type="offline",
                                    prompt="select_account consent", state=state)
    # PKCE: authorization_url() 이 만든 verifier 를 콜백까지 넘겨야 한다. 콜백은 Flow 를
    # 새로 만들기 때문에, 안 넘기면 토큰 교환이 'Missing code verifier' 로 죽는다.
    request.session["code_verifier"] = flow.code_verifier
    return RedirectResponse(url, status_code=302)


@app.get("/auth/callback")
def auth_callback(request: Request, code: str, state: str):
    expected = request.session.pop("oauth_state", None)
    if expected is None or state != expected:
        raise HTTPException(status_code=400, detail="state mismatch")
    verifier = request.session.pop("code_verifier", None)
    if verifier is None:
        raise HTTPException(status_code=400, detail="code_verifier 없음 — /auth/login 부터 다시")
    flow = _flow()
    flow.code_verifier = verifier
    flow.fetch_token(code=code)
    creds = flow.credentials
    email = google.oauth2.id_token.verify_oauth2_token(
        creds.id_token, google.auth.transport.requests.Request(), _client_id(),
    )["email"]
    conn = store.connect()
    try:
        uid = store.upsert_user(conn, email)
        store.save_token(conn, uid, creds.to_json())
    finally:
        conn.close()
    request.session["uid"] = uid
    request.session["email"] = email
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
    uid = _require_uid(request)
    conn = store.connect()
    try:
        with store.tenant(conn, uid):
            res = collect_gsc.get_service().sites().list().execute()
    finally:
        conn.close()
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
async def api_sites(request: Request):
    """속성 여러 개를 한 번에 등록한다. 이름·도메인은 속성에서 짓고, 종류만 받는다."""
    uid = _require_uid(request)
    body = await request.json()
    props = body.get("properties") or []
    if isinstance(props, str):
        props = [props]
    if not props:
        raise HTTPException(status_code=400, detail="고를 속성이 없습니다")
    types = body.get("types") or {}

    conn = store.connect()
    added, failed = [], []
    try:
        taken = {r["project"] for r in store.sites(conn, uid)}
        with store.tenant(conn, uid):
            for prop in props[:20]:
                host = _host_of(str(prop))
                if not host:
                    failed.append({"property": prop, "error": "도메인을 알 수 없습니다"})
                    continue
                name = _slug(host, taken)
                r = dashboard.create_project({
                    "name": name, "type": types.get(prop, "saas"), "domain": host,
                    "gsc_property": prop, "locale": body.get("locale", "ko-KR"),
                    "brand_aliases": host.split(".")[0], "seed_keywords": "",
                    "competitors_manual": "",
                })
                if r.get("ok"):
                    taken.add(name)
                    added.append({"project": name, "property": prop})
                else:
                    failed.append({"property": prop, "error": r.get("error", "등록 실패")})
        for a in added:
            store.add_site(conn, uid, a["project"], a["property"], _host_of(a["property"]))
    finally:
        conn.close()

    if added:
        _kick()
    return {"ok": bool(added), "added": added, "failed": failed}


# --- GitHub (/create) ---------------------------------------------------------

@app.get("/auth/github")
def gh_login(request: Request):
    _require_uid(request)
    cid = os.environ.get("GITHUB_CLIENT_ID")
    if not cid:
        raise HTTPException(status_code=500, detail="GITHUB_CLIENT_ID 가 설정되지 않았습니다")
    state = secrets.token_urlsafe(16)
    request.session["gh_state"] = state
    # repo 스코프: PR 브랜치를 만들려면 쓰기가 필요하다. 머지는 사람이 한다(발행 게이트).
    return RedirectResponse(
        "https://github.com/login/oauth/authorize"
        f"?client_id={cid}&scope=repo&state={state}"
        f"&redirect_uri={quote(_gh_redirect(), safe='')}", status_code=302)


def _gh_redirect() -> str:
    return os.environ.get("GITHUB_REDIRECT_URI",
                          OAUTH_REDIRECT_URI.replace("/auth/callback", "/auth/github/callback"))


@app.get("/auth/github/callback")
def gh_callback(request: Request, code: str, state: str):
    uid = _require_uid(request)
    if state != request.session.pop("gh_state", None):
        raise HTTPException(status_code=400, detail="state mismatch")
    cid = os.environ.get("GITHUB_CLIENT_ID")
    sec = os.environ.get("GITHUB_CLIENT_SECRET")
    if not (cid and sec):
        raise HTTPException(status_code=500, detail="GitHub 클라이언트가 설정되지 않았습니다")
    token = gh.exchange_code(cid, sec, code)
    conn = store.connect()
    try:
        store.save_github(conn, uid, token, gh.login(token))
    finally:
        conn.close()
    return RedirectResponse("/", status_code=302)


def _gh_token(conn, uid: int) -> str:
    got = store.github(conn, uid)
    if not got:
        raise HTTPException(status_code=428, detail="GitHub 연결이 필요합니다")
    return got[0]


@app.get("/api/repos")
def api_repos(request: Request):
    uid = _require_uid(request)
    conn = store.connect()
    try:
        return {"repos": gh.repos(_gh_token(conn, uid))}
    except gh.GitHubError as e:
        raise HTTPException(status_code=502, detail=str(e))
    finally:
        conn.close()


@app.post("/api/repo")
async def api_repo(request: Request):
    """사이트에 리포를 붙인다. 관례 발견은 첫 글쓰기 때 한 번 한다."""
    uid = _require_uid(request)
    b = await request.json()
    project, repo, branch = str(b.get("project") or ""), str(b.get("repo") or ""),         str(b.get("branch") or "main")
    conn = store.connect()
    try:
        _own(conn, uid, project)
        store.set_repo(conn, uid, project, repo, branch)
    finally:
        conn.close()
    return {"ok": True}


@app.post("/api/create")
async def api_create(request: Request):
    """기회 하나 → 리포에 PR. /create run 의 웹 판이다."""
    uid = _require_uid(request)
    b = await request.json()
    project = str(b.get("project") or "")
    opp_id = int(b.get("opportunity_id") or 0)
    conn = store.connect()
    try:
        _own(conn, uid, project)
        row = store.site(conn, uid, project)
        if not row or not row["repo"]:
            raise HTTPException(status_code=428, detail="이 사이트에 연결된 저장소가 없습니다")
        token = _gh_token(conn, uid)
        repo, branch = row["repo"], row["repo_branch"] or "main"

        with store.tenant(conn, uid):
            c = db.connect()
            try:
                p = db.get_project(c, project)
                opp = c.execute("SELECT * FROM opportunities WHERE id=? AND project_id=?",
                                (opp_id, p["id"])).fetchone()
                if not opp:
                    raise HTTPException(status_code=404, detail="그런 기회가 없습니다")
                opp = dict(opp)
                ev = c.execute(
                    "SELECT SUM(clicks) c, SUM(impressions) i, AVG(position) pos "
                    "FROM gsc_snapshots WHERE project_id=? AND query=?",
                    (p["id"], opp["target"])).fetchone()
            finally:
                c.close()
            evidence = ({"클릭": ev["c"], "노출": ev["i"],
                         "평균 순위": round(ev["pos"], 1)} if ev and ev["i"] else {})

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
        return {"ok": True, "pr": pr["url"], "path": doc["path"]}
    except (gh.GitHubError, writer.WriterError) as e:
        raise HTTPException(status_code=502, detail=str(e))
    finally:
        conn.close()


STAGES = ("gsc", "index", "keywords", "rank", "ai", "gaps", "report")


@app.post("/api/run")
async def api_run(request: Request):
    """'지금 다시 재기' — /capture run 에 해당한다. stages 를 주면 그 단계만
    돈다(/capture gsc, /capture ai …). 웹에는 명령을 칠 곳이 없으므로 버튼이 그 자리다."""
    uid = _require_uid(request)
    body = await request.json()
    project = str(body.get("project") or "")
    stages = [x for x in str(body.get("stages") or "").split(",") if x]
    bad = [x for x in stages if x not in STAGES]
    if bad:
        raise HTTPException(status_code=400, detail=f"모르는 단계: {', '.join(bad)}")

    conn = store.connect()
    try:
        _own(conn, uid, project)
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
    finally:
        conn.close()

    if started:
        if stages:
            _spawn_worker(["--user", str(uid), "--project", project,
                           "--only", ",".join(stages)])
        else:
            _kick()
    return {"ok": True, "started": started}


@app.get("/api/report")
def api_report(project: str, request: Request):
    """/capture report — 그 시점 화면을 자립형 HTML 로 박제해 내려준다."""
    uid = _require_uid(request)
    conn = store.connect()
    try:
        _own(conn, uid, project)
        with store.tenant(conn, uid):
            path = dashboard.export(project)
            data = Path(path).read_bytes()
    except db.ProjectNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    finally:
        conn.close()
    return Response(data, media_type="text/html; charset=utf-8", headers={
        "Content-Disposition": f'attachment; filename="{project}-report.html"'})


@app.get("/api/export")
def api_export(project: str, table: str, request: Request):
    """CSV 내려받기 — 마케터는 결국 엑셀로 옮긴다."""
    uid = _require_uid(request)
    conn = store.connect()
    try:
        _own(conn, uid, project)
        with store.tenant(conn, uid):
            data, name = exports.csv_bytes(project, table)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except db.ProjectNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    finally:
        conn.close()
    return Response(data, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.get("/api/overview")
def api_overview(request: Request):
    """사이트 전부를 한 줄씩 — 드롭다운으로 하나씩 전환하지 않아도 되게."""
    uid = _require_uid(request)
    conn = store.connect()
    try:
        out = []
        for r in store.sites(conn, uid):
            with store.tenant(conn, uid):
                try:
                    d = exports.summary(r["project"])
                except db.ProjectNotFound:
                    d = {"project": r["project"], "domain": r["domain"]}
            d["running"] = bool(r["running_since"])
            d["last_run_at"] = r["last_run_at"]
            out.append(d)
        return {"sites": out}
    finally:
        conn.close()


@app.get("/api/keywords")
def api_keywords(project: str, request: Request, status: str = "candidate"):
    """추적 중(active)이거나 후보(candidate)인 키워드.

    후보는 자동완성에서 캔 것이라 관련성이 확인되지 않았다 — 자동 활성화는 서치콘솔에
    노출된 것만 켠다. 나머지는 사람이 보고 골라야 해서 이 목록이 필요하다.
    """
    uid = _require_uid(request)
    active = 1 if status == "active" else 0
    conn = store.connect()
    try:
        _own(conn, uid, project)
        with store.tenant(conn, uid):
            c = db.connect()
            try:
                pid = db.get_project(c, project)["id"]
                rows = c.execute(
                    "SELECT k.id, k.keyword, k.source, k.cluster,"
                    "       COALESCE(SUM(g.impressions),0) imp, COALESCE(SUM(g.clicks),0) clk"
                    "  FROM keywords k"
                    "  LEFT JOIN gsc_snapshots g"
                    "    ON g.project_id=k.project_id AND g.query=k.keyword"
                    " WHERE k.project_id=? AND k.is_active=?"
                    " GROUP BY k.id ORDER BY imp DESC, k.keyword LIMIT 500",
                    (pid, active)).fetchall()
                n_active = c.execute(
                    "SELECT COUNT(*) FROM keywords WHERE project_id=? AND is_active=1",
                    (pid,)).fetchone()[0]
            finally:
                c.close()
        return {"keywords": [dict(r) for r in rows], "active_total": n_active,
                "limit": int(os.environ.get("SEOMINER_MAX_KEYWORDS", "100"))}
    except db.ProjectNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    finally:
        conn.close()


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
        raise HTTPException(status_code=400, detail="고른 키워드가 없습니다")
    conn = store.connect()
    try:
        _own(conn, uid, project)
        with store.tenant(conn, uid):
            c = db.connect()
            try:
                pid = db.get_project(c, project)["id"]
                if on:
                    limit = int(os.environ.get("SEOMINER_MAX_KEYWORDS", "100"))
                    cur = c.execute("SELECT COUNT(*) FROM keywords WHERE project_id=?"
                                    " AND is_active=1", (pid,)).fetchone()[0]
                    room = max(0, limit - cur)
                    if room == 0:
                        raise HTTPException(
                            status_code=409,
                            detail=f"추적 상한 {limit}개가 찼습니다 — 먼저 뺄 것을 고르세요")
                    ids = ids[:room]
                q = ",".join("?" * len(ids))
                c.execute(f"UPDATE keywords SET is_active=? WHERE project_id=? AND id IN ({q})",
                          [1 if on else 0, pid, *ids])
                c.commit()
                n = c.execute("SELECT COUNT(*) FROM keywords WHERE project_id=? AND is_active=1",
                              (pid,)).fetchone()[0]
            finally:
                c.close()
        return {"ok": True, "changed": len(ids), "active_total": n}
    except db.ProjectNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    finally:
        conn.close()


@app.get("/api/backlinks")
def api_backlinks(project: str, request: Request):
    """백링크 프로필 — 없으면 빈 값. 지어내지 않는다."""
    uid = _require_uid(request)
    conn = store.connect()
    try:
        _own(conn, uid, project)
        with store.tenant(conn, uid):
            data = backlinks.latest(project)
            data["available"] = backlinks.available()
        return data
    except db.ProjectNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    finally:
        conn.close()


@app.get("/api/creations")
def api_creations(project: str, request: Request):
    """/create status — 이 사이트에서 실제로 고친 것들."""
    uid = _require_uid(request)
    conn = store.connect()
    try:
        _own(conn, uid, project)
        with store.tenant(conn, uid):
            c = db.connect()
            try:
                pid = db.get_project(c, project)["id"]
                rows = c.execute(
                    "SELECT id, kind, file_path, branch, note, merged, created_at "
                    "FROM creations WHERE project_id=? ORDER BY id DESC LIMIT 50",
                    (pid,)).fetchall()
            finally:
                c.close()
        return {"creations": [dict(r) for r in rows]}
    except db.ProjectNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    finally:
        conn.close()


@app.get("/api/run/status")
def api_run_status(request: Request):
    """사이트별 수집 상태 — 화면이 폴링한다."""
    uid = _require_uid(request)
    conn = store.connect()
    try:
        return {r["project"]: {"running": bool(r["running_since"]),
                               "last_run_at": r["last_run_at"]}
                for r in store.sites(conn, uid)}
    finally:
        conn.close()


def _own(conn, uid: int, project: str) -> None:
    if not any(r["project"] == project for r in store.sites(conn, uid)):
        raise HTTPException(status_code=404, detail="not found")


# --- 대시보드 -----------------------------------------------------------------
# 화면은 로컬 플러그인의 것을 그대로 쓴다(skills/capture/templates/dashboard.html).
# 아래 경로 이름은 그 HTML 이 부르는 것이라 바꿀 수 없다.

# 로컬 대시보드는 조회 전용이고, 실행은 Claude Code 에서 /capture run 으로 했다.
# 웹에는 명령을 칠 곳이 없다 — 원본 HTML 을 고치는 대신 뒤에 얹어서 버튼을 만든다.
_DASH_ADDON = """
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans+KR:wght@300;400;500;600;700&display=swap" rel="stylesheet">
<style>
  /* 원본은 시스템 폰트(Segoe UI/Cascadia)라 한글이 맑은고딕으로 떨어져 수치와 섞이면
     지저분하다. 변수만 갈아끼우면 화면 전체가 따라온다 — 원본 규칙은 손대지 않는다. */
  :root{
    --sans:"IBM Plex Sans KR","Segoe UI",system-ui,sans-serif;
    --mono:"IBM Plex Mono",ui-monospace,SFMono-Regular,monospace;
  }
  body{word-break:keep-all}          /* 한글은 어절 단위로 끊는다 */

  #toggle-setup,#setup{display:none!important}
  #nx code{display:none}             /* 배너의 명령어 — 웹에는 칠 곳이 없다 */

  /* 실행 줄 — 헤더에 붙여 한 덩어리로 보이게 한다. 주 액션 하나만 진하게 두고
     부분 실행은 가볍게 늘어놓는다(전에는 같은 무게의 버튼 일곱 개가 떠 있었다). */
  #sm-bar{display:flex;align-items:center;gap:8px 14px;flex-wrap:wrap;
    padding:9px 22px 11px;border-top:1px solid var(--rule-soft);
    background:var(--card);font-size:13px}
  #sm-bar .lbl{color:var(--slate);font-size:11.5px;font-family:var(--mono);
    letter-spacing:.04em}
  #sm-run{font:inherit;font-weight:500;cursor:pointer;border:1px solid var(--patina);
    background:var(--patina);color:var(--card);border-radius:3px;padding:6px 14px}
  #sm-run:hover:not(:disabled){filter:brightness(1.12)}
  #sm-run:disabled{opacity:.5;cursor:default}
  #sm-bar .cmd{font:inherit;font-size:13px;cursor:pointer;border:0;background:none;
    color:var(--patina);padding:4px 2px;border-bottom:1px solid transparent}
  #sm-bar .cmd:hover:not(:disabled){border-bottom-color:var(--patina)}
  #sm-bar .cmd:disabled{color:var(--slate);cursor:default}
  #sm-bar .sp{flex:1}
  #sm-bar a.cmd{text-decoration:none}
  :is(#sm-bar,.opp) button:focus-visible{outline:2px solid var(--patina);outline-offset:2px}
  /* 원본 .cmd 는 '복사' 칩이라 ⧉ 를 달고 있다. 이제 실행 버튼이므로 뗀다. */
  :is(#sm-bar,.stp) .cmd::after{content:none}

  /* ── 좌측 메뉴 ──────────────────────────────────────────────
     원본은 상단 바다. 넓은 화면에서만 세로로 세운다 — 실행 메뉴가 여섯 줄이라
     가로로 늘어놓으면 본문 폭을 먹고, 세로로 세우면 위계가 그대로 읽힌다.
     좁은 화면에서는 원본 그대로 위에 눕는다. */
  @media (min-width:1000px){
    body{display:grid;grid-template-columns:232px minmax(0,1fr);align-items:start}
    header{grid-column:1;position:sticky;top:0;height:100vh;overflow-y:auto;
      display:flex;flex-direction:column;      /* #sm-bar 의 margin-top:auto 가 먹으려면 */
      border-bottom:0;border-right:1px solid var(--ink)}
    .hbar{flex-direction:column;align-items:stretch;gap:14px;padding:22px 20px 10px;
      max-width:none;margin:0}
    .hbar .sp{display:none}
    .tag{display:block!important;letter-spacing:.06em;font-size:10.5px;
      color:var(--stone);opacity:.5;margin-top:-8px}
    #proj{max-width:none;width:100%}
    /* 원본은 한 줄 바에 있어서 nowrap 이었다 — 세로로 세우면 잘린다. */
    #meta{font-size:11.5px;line-height:1.65;opacity:.7;white-space:normal;
      word-break:break-all;display:block}
    .hbar .hbtn{width:100%;text-align:left;padding-left:0;padding-right:0}
    #sm-bar{flex-direction:column;align-items:stretch;gap:2px;border-top:0;
      background:transparent;padding:4px 20px 26px;margin-top:auto}
    #sm-bar .sp{display:none}
    #sm-bar .lbl{margin:12px 0 2px}
    #sm-run{width:100%;text-align:center;padding:9px 14px;margin-bottom:4px}
    #sm-bar .cmd{text-align:left;padding:5px 2px;color:var(--stone);opacity:.72}
    #sm-bar .cmd:hover:not(:disabled){opacity:1;border-bottom-color:transparent}
    #sm-bar a.cmd{margin-top:10px}
    main{grid-column:2;max-width:1080px;margin:0;padding-left:34px;padding-right:34px}
  }

  /* 원본 자간은 영문 대문자 라벨 기준(.24em)이라 한글에서는 글자가 흩어진다.
     "성 과 일  기 준" 처럼 읽히던 것 — 값만 낮추고 서체는 그대로 둔다. */
  /* KPI 라벨은 한글이다 — mono 로 두면 한글이 폴백 서체로 떨어져 자간이 벌어진다. */
  .band .l{font-family:var(--sans);font-weight:500}

  /* 백링크 표: 마지막 두 칸이 붙어 숫자와 날짜가 겹쳐 보였다. */
  #sm-bl{padding-top:26px}
  #sm-bl table{width:100%;table-layout:fixed}
  #sm-bl th:first-child,#sm-bl td:first-child{width:42%}
  #sm-bl th:last-child,#sm-bl td:last-child{text-align:right;width:20%}
  #sm-bl .sub{color:var(--slate);font-size:13px;margin:4px 0 18px}

  /* 키워드 선별 — 자동은 서치콘솔 노출만 켠다. 나머지는 사람이 골라야 한다. */
  #sm-kw{padding:26px 0 10px}
  #sm-kw .sub{color:var(--slate);font-size:13px;margin:4px 0 14px}
  #sm-kw .tabs{display:flex;gap:6px;margin-bottom:10px}
  #sm-kw .tabs button{font:400 12px/1 var(--mono);color:var(--slate);background:transparent;
    border:1px solid var(--rule);border-radius:3px;padding:6px 12px;cursor:pointer}
  #sm-kw .tabs button.on{color:var(--patina);border-color:var(--patina);background:var(--wash)}
  #sm-kw .list{max-height:340px;overflow:auto;border:1px solid var(--rule-soft)}
  /* flex 로 두면 자식이 넘칠 때 .kw 의 flex-basis:0 이 0px 으로 굳어 글자가 사라진다.
     grid 의 1fr 은 넘쳐도 자기 몫을 지킨다. */
  #sm-kw label{display:grid;grid-template-columns:auto minmax(0,1fr) auto;
    align-items:center;gap:10px;padding:7px 12px;cursor:pointer;
    border-bottom:1px solid var(--rule-soft);font-size:13.5px}
  #sm-kw label:hover{background:var(--bar)}
  #sm-kw label input{accent-color:var(--patina);flex:none;margin:0}
  /* 원본 어딘가의 text-transform 이 상속돼 검색어가 전부 대문자로 보였다.
     검색어는 사용자가 실제로 친 문자열이라 그대로 보여야 한다. */
  #sm-kw .kw{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
    color:var(--ink);text-transform:none}
  #sm-kw .n{font:400 11.5px/1 var(--mono);color:var(--slate);white-space:nowrap}
  #sm-kw .list{overflow-x:hidden}
  #sm-kw .bar2{display:flex;align-items:center;gap:12px;margin-top:12px;flex-wrap:wrap}
  #sm-kw .go2{font:500 13px/1 var(--sans);color:var(--card);background:var(--patina);
    border:0;border-radius:3px;padding:9px 16px;cursor:pointer}
  #sm-kw .go2:disabled{opacity:.4;cursor:default}
  #sm-kw .cnt{font-size:12.5px;color:var(--slate)}
  #sm-kw .warn{color:var(--copper)}

  /* 플러그인으로 넘기는 일 — 조용한 목록. 대시보드 본문의 결론이 아니라 각주다. */
  #sm-cc{padding:26px 0 10px}
  #sm-cc .sub{color:var(--slate);font-size:13px;margin:4px 0 16px}
  #sm-cc ul{list-style:none;padding:0;margin:0;border-top:1px solid var(--rule-soft)}
  #sm-cc li{display:flex;align-items:center;gap:18px;padding:12px 0;
    border-bottom:1px solid var(--rule-soft)}
  #sm-cc li div{flex:1;min-width:0}
  #sm-cc li b{display:block;font-weight:600;font-size:14px}
  #sm-cc li span{color:var(--slate);font-size:12.5px}
  #sm-cc button{font:400 12px/1 var(--mono);color:var(--slate);background:transparent;
    border:1px solid var(--rule);border-radius:3px;padding:6px 10px;cursor:pointer;flex:none}
  #sm-cc button:hover{color:var(--patina);border-color:var(--patina)}
  @media(max-width:640px){#sm-cc li{flex-direction:column;align-items:flex-start;gap:8px}}

  /* 모바일: 차트 SVG 는 720px 고정이고 부모(.logwrap)가 overflow-x:auto 로 받는다.
     스크롤이 된다는 걸 알려주지 않으면 잘린 그림으로만 보인다. */
  @media(max-width:900px){
    .logwrap{position:relative;-webkit-overflow-scrolling:touch;
      /* 오른쪽 끝에 페이드 — 끝나지 않았다는 신호 */
      -webkit-mask-image:linear-gradient(90deg,#000 88%,transparent);
      mask-image:linear-gradient(90deg,#000 88%,transparent)}
    .logwrap.at-end{-webkit-mask-image:none;mask-image:none}
    .sm-swipe{font:400 11px/1 var(--mono);color:var(--slate);margin:6px 0 0;
      display:flex;align-items:center;gap:6px}
  }
  @media(min-width:901px){.sm-swipe{display:none}}

  .eyebrow{letter-spacing:.08em}
  .band .l{letter-spacing:.03em}
  .tag{letter-spacing:.12em}
  th{letter-spacing:.05em}
  .opp .kind{letter-spacing:.05em}

  /* 안내 단계의 실행 버튼 — 원본 .cmd 는 명령어를 복사하는 칩이었다. */
  .stp .cmd{font:inherit;font-size:13px;cursor:pointer;border:1px solid var(--patina);
    background:transparent;color:var(--patina);border-radius:3px;padding:5px 12px}
  .stp .cmd:hover:not(:disabled){background:var(--wash)}
  .stp .cmd:disabled{opacity:.5;cursor:default}

  /* 기회 카드: 버튼 넷이 카드마다 반복돼 소음이 된다. 평소엔 물러나 있게. */
  .opp .acts button{opacity:.72;transition:opacity .12s}
  .opp:hover .acts button,.opp .acts button:focus-visible{opacity:1}
  .opp [data-write]{color:var(--copper);border-color:var(--copper)}
</style>
<script>
(function () {
  var b = document.createElement("button");
  b.id = "sm-run"; b.textContent = "전체 분석 실행";

  function proj() {
    var p = document.getElementById("proj");
    return p ? p.value : "";
  }
  function paint(st) {
    var r = st && st[proj()];
    if (!r) return;
    b.disabled = !!r.running;
    b.textContent = r.running ? "분석 중…" : "전체 분석 실행";
  }
  async function poll() {
    try { paint(await (await fetch("/api/run/status")).json()); } catch (e) {}
  }
  b.addEventListener("click", async function () {
    if (!proj()) return;
    b.disabled = true; b.textContent = "시작하는 중…";
    try {
      await fetch("/api/run", {method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({project: proj()})});
    } catch (e) {}
    poll();
  });
  var sel = document.getElementById("proj");
  if (sel) sel.addEventListener("change", poll);
  poll();
  setInterval(poll, 15000);   // 측정이 끝나면 버튼이 스스로 풀린다

  // 기회 카드마다 '글 쓰기' — /create run 의 자리다. 리포가 안 붙어 있으면 서버가
  // 428 로 알려 주고, 그때 연결 링크를 보여 준다.
  document.addEventListener("click", async function (ev) {
    var b = ev.target.closest ? ev.target.closest("[data-write]") : null;
    if (!b) return;
    var id = b.getAttribute("data-write");
    b.disabled = true; b.textContent = "쓰는 중…";
    try {
      var r = await fetch("/api/create", {method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({project: proj(), opportunity_id: Number(id)})});
      var d = await r.json();
      if (r.ok && d.pr) { b.outerHTML = '<a href="' + d.pr + '" target="_blank" rel="noopener">PR 열림 →</a>'; return; }
      if (r.status === 428) { b.outerHTML = '<a href="/">저장소 연결 필요 →</a>'; return; }
      b.textContent = "실패: " + (d.detail || "").slice(0, 60); b.disabled = false;
    } catch (e) { b.textContent = "실패"; b.disabled = false; }
  });

  // ── 용어 통일 ──────────────────────────────────────────────
  // 원본은 쉬운 우리말을 골랐지만("손댈 것", "채굴 로그"), 마케터는 서치콘솔 한국어
  // UI 와 업계 표준어로 생각한다. 원본 HTML 은 그대로 두고 표시 문구만 바꿔 끼운다.
  var TERMS = {
    "채굴 로그": "SEO 대시보드",
    "다음에 뭘 하면 되나": "분석 절차",
    "1페이지까지 남은 거리": "순위 상승 기회",
    "4~20위 · 노출 많은 순": "11~20위 · 노출수 많은 순",
    "다음에 손댈 것": "개선 기회",
    "다음 행동": "권장 조치",
    "움직인 검색어": "순위 변동",
    "모바일에서 밀리는 검색어": "모바일 순위 격차",
    "MOBILE vs DESKTOP": "MOBILE vs DESKTOP",
    "색인 문제": "색인 생성 오류",
    "URL 검사": "URL 검사",
    "AI가 누구를 인용하나": "AI 인용 현황",
    "수집 이력": "실행 기록",
    "순위 기록": "순위 추적 기록",
    "성과일 기준 · 하루 = 점 하나": "성과일 기준 · 일별",
    "이 도구를 쓰는 순서 · 6단계": "분석 절차 · 6단계",
    // KPI 라벨 — 서치콘솔 한국어 표기를 따른다
    "클릭": "총 클릭수",
    "노출": "총 노출수",
    "노출된 검색어": "검색어 수",
    "남은 기회": "개선 기회"
  };

  function retitle(root) {
    // 제목·라벨만 건드린다. 본문까지 훑으면 데이터(검색어·URL)를 바꿔 버린다.
    (root || document).querySelectorAll(
      "h2,.eyebrow,.band .l,.tag,#nx b,.stp h3,.opp .kind"
    ).forEach(function (el) {
      var t = (el.firstChild && el.firstChild.nodeValue || "").trim();
      if (t && TERMS[t] && el.firstChild.nodeValue.indexOf(TERMS[t]) === -1) {
        el.firstChild.nodeValue = el.firstChild.nodeValue.replace(t, TERMS[t]);
      }
    });
  }
  retitle();
  document.title = "SEO 대시보드 — seo-miner";   // 원본 <title> 은 애드온보다 앞에 있다

  // 안내의 '이 명령을 치세요' 버튼을 실제 실행 버튼으로 바꾼다. 원본은 명령어를
  // 클립보드로 복사할 뿐이라, 터미널이 없는 웹에서는 막다른 길이었다.
  var STAGE_LABEL = {gsc: "검색 실적 수집", index: "색인 생성 검사",
    keywords: "키워드 발굴", rank: "순위 추적", ai: "AI 인용 확인",
    gaps: "개선 기회 분석", report: "보고서 생성"};

  async function runStage(stage, btn) {
    var was = btn.textContent;
    btn.disabled = true; btn.textContent = "실행 중…";
    try {
      var r = await fetch("/api/run", {method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({project: proj(), stages: stage})});
      if (!r.ok) { var d = await r.json(); btn.textContent = (d.detail || "실패").slice(0, 40); }
      else { btn.textContent = "실행 시작"; }
    } catch (e) { btn.textContent = "실패"; }
    poll();
    setTimeout(function () { btn.disabled = false; btn.textContent = was; }, 4000);
  }

  function wireSteps() {
    document.querySelectorAll("button.cmd").forEach(function (b) {
      if (b.dataset.smWired) return;
      b.dataset.smWired = "1";
      var t = (b.textContent || "").trim();
      var m = t.match(/^\/capture\s+(\w+)/);
      if (m && STAGE_LABEL[m[1]]) {
        var st = m[1];
        b.textContent = STAGE_LABEL[st] + " 실행";
        b.onclick = function () { runStage(st, b); };
        return;
      }
      if (/^\/create/.test(t)) {          // 기회는 아래 목록에 이미 있다
        b.textContent = "개선 기회 보기";
        b.onclick = function () {
          var o = document.getElementById("opps") || document.querySelector(".opp");
          if (o) o.scrollIntoView({behavior: "smooth", block: "start"});
        };
        return;
      }
      b.remove();      // 사이트 등록·구글 연동은 웹에서 이미 끝난 단계다
    });
  }

  // 실행 줄 — 안내가 접혀 있어도 쓸 수 있어야 한다. 주 액션(다시 재기) 하나만
  // 진하게, 부분 실행은 텍스트로 늘어놓는다.
  (function bar() {
    if (document.getElementById("sm-bar")) return;
    var head = document.querySelector("header");
    if (!head) return;
    var box = document.createElement("div");
    box.id = "sm-bar";
    box.appendChild(b);                       // 위에서 만든 '지금 다시 재기'
    var lbl = document.createElement("span");
    lbl.className = "lbl"; lbl.textContent = "개별 실행";
    box.appendChild(lbl);
    [["gsc", "검색 실적"], ["index", "색인 생성"], ["keywords", "키워드 발굴"],
     ["rank", "순위 추적"], ["ai", "AI 인용"], ["gaps", "개선 기회"]].forEach(function (p) {
      var t = document.createElement("button");
      t.className = "cmd"; t.dataset.smWired = "1"; t.textContent = p[1];
      t.title = STAGE_LABEL[p[0]];
      t.onclick = function () { runStage(p[0], t); };
      box.appendChild(t);
    });
    var sp = document.createElement("span"); sp.className = "sp"; box.appendChild(sp);
    var rep = document.createElement("a");
    rep.className = "cmd"; rep.textContent = "보고서 다운로드 ↓";
    rep.addEventListener("click", function () {
      rep.href = "/api/report?project=" + encodeURIComponent(proj());
    });
    box.appendChild(rep);
    head.appendChild(box);
  })();

  // ── 백링크 ───────────────────────────────────────────────────
  // 원본 화면에 없는 축이다. 섹션을 만들어 끼운다(원본 HTML 은 그대로).
  var blDone = "";
  async function backlinks() {
    var pj = proj();
    if (!pj || blDone === pj) return;
    var host = document.querySelector("main");
    if (!host) return;
    var d;
    try { d = await (await fetch("/api/backlinks?project=" + encodeURIComponent(pj))).json(); }
    catch (e) { return; }
    blDone = pj;
    var sec = document.getElementById("sm-bl") || document.createElement("section");
    sec.id = "sm-bl";
    var s = d.summary;
    if (!s) {
      sec.innerHTML = '<p class="eyebrow">BACKLINKS</p><h2>백링크</h2>' +
        '<p class="sub">' + (d.available
          ? "아직 수집 전입니다 — 다음 분석에서 함께 수집합니다(30일 주기)."
          : "DataForSEO 연동이 없어 수집하지 않습니다. 백링크는 서치콘솔이 제공하지 않아 외부 데이터가 필요합니다.") +
        "</p>";
    } else {
      var rows = (d.domains || []).map(function (x) {
        return "<tr><td>" + esc(x.domain) + '</td><td class="num">' + (x.rank == null ? "" : x.rank) +
               '</td><td class="num">' + (x.backlinks || 0) + '</td><td class="num">' +
               (x.dofollow || 0) + "</td><td>" + esc(x.first_seen ? String(x.first_seen).slice(0, 10) : "") +
               "</td></tr>";
      }).join("");
      sec.innerHTML = '<p class="eyebrow">BACKLINKS · ' + esc(s.checked_date) + '</p>' +
        "<h2>백링크 프로필</h2>" +
        '<p class="sub">참조 도메인 수가 백링크 총수보다 중요합니다 — 한 도메인에서 100개보다 100개 도메인에서 1개씩이 낫습니다.</p>' +
        '<div class="band" style="margin-bottom:18px">' +
        '<div class="m"><div class="v">' + (s.referring_domains || 0) + '</div><div class="l">참조 도메인</div></div>' +
        '<div class="m"><div class="v">' + (s.backlinks || 0) + '</div><div class="l">총 백링크</div></div>' +
        '<div class="m"><div class="v">' + (s.dofollow || 0) + '</div><div class="l">dofollow</div></div>' +
        '<div class="m"><div class="v">' + (s.broken_backlinks || 0) + '</div><div class="l">손실 백링크</div></div>' +
        '<div class="m"><div class="v">' + (s.rank == null ? "—" : s.rank) + '</div><div class="l">도메인 지수</div></div>' +
        "</div>" +
        (rows ? '<table><thead><tr><th>도메인</th><th class="num">랭크</th>' +
          '<th class="num">링크</th><th class="num">dofollow</th><th>처음 발견</th></tr></thead>' +
          "<tbody>" + rows + "</tbody></table>" : "");
    }
    if (!sec.parentNode) host.appendChild(sec);
  }
  if (sel) sel.addEventListener("change", function () { blDone = ""; backlinks(); });
  backlinks();

  // 좁은 화면에서 차트는 가로로 스크롤한다(원본 .logwrap 설계). 그 사실을 알린다 —
  // 모르면 잘린 그림으로만 보인다.
  function swipeHint() {
    document.querySelectorAll(".logwrap").forEach(function (w) {
      if (w.scrollWidth <= w.clientWidth + 2) return;      // 넘칠 때만
      if (!w.dataset.smHint) {
        w.dataset.smHint = "1";
        var p = document.createElement("p");
        p.className = "sm-swipe";
        p.textContent = "← 좌우로 스크롤하세요";
        w.parentNode.insertBefore(p, w.nextSibling);
        w.addEventListener("scroll", function () {
          var end = w.scrollLeft + w.clientWidth >= w.scrollWidth - 4;
          w.classList.toggle("at-end", end);
          p.style.visibility = end ? "hidden" : "";
        });
      }
    });
  }
  swipeHint();
  window.addEventListener("resize", swipeHint);

  // ── 키워드 선별 ────────────────────────────────────────────
  // 자동 활성화는 '구글이 이미 노출시킨' 검색어만 켠다. 자동완성에서 캔 후보는
  // 관련성이 확인되지 않아 그대로 잠들어 있다 — 여기서 사람이 고른다.
  var kwMode = "candidate", kwWired = false;

  async function kwLoad() {
    var box = document.getElementById("sm-kw");
    if (!box) return;
    var list = box.querySelector(".list");
    list.innerHTML = '<p style="padding:14px;color:var(--slate);font-size:13px">불러오는 중…</p>';
    var d;
    try {
      d = await (await fetch("/api/keywords?status=" + kwMode +
                             "&project=" + encodeURIComponent(proj()))).json();
    } catch (e) { list.innerHTML = '<p style="padding:14px">불러오지 못했습니다</p>'; return; }
    var ks = d.keywords || [];
    box.querySelector(".cnt").innerHTML = "추적 키워드 " + d.active_total + " / " + d.limit +
      (d.active_total >= d.limit ? ' <b class="warn">· 한도 도달</b>' : "");
    if (!ks.length) {
      list.innerHTML = '<p style="padding:14px;color:var(--slate);font-size:13px">' +
        (kwMode === "candidate" ? "발굴된 후보가 없습니다 — 키워드 발굴을 먼저 실행하세요."
                                : "추적 중인 키워드가 없습니다.") + "</p>";
      return;
    }
    list.innerHTML = ks.map(function (k) {
      return '<label><input type="checkbox" value="' + k.id + '">' +
             '<span class="kw">' + esc(k.keyword) + "</span>" +
             '<span class="n">' + (k.imp ? "노출 " + k.imp : esc(k.source || "")) + "</span></label>";
    }).join("");
  }

  async function kwApply(on) {
    var box = document.getElementById("sm-kw");
    var ids = [...box.querySelectorAll(".list input:checked")].map(function (i) {
      return Number(i.value);
    });
    if (!ids.length) return;
    var btn = box.querySelector(".go2");
    btn.disabled = true;
    try {
      var r = await fetch("/api/keywords", {method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({project: proj(), ids: ids, active: on})});
      var d = await r.json();
      if (!r.ok) { box.querySelector(".cnt").innerHTML =
        '<b class="warn">' + esc(d.detail || "실패") + "</b>"; }
    } catch (e) {}
    btn.disabled = false;
    kwLoad();
  }

  function keywords() {
    var host = document.querySelector("main");
    if (!host || document.getElementById("sm-kw")) return;
    var sec = document.createElement("section");
    sec.id = "sm-kw";
    sec.innerHTML = '<p class="eyebrow">KEYWORDS</p><h2>추적 키워드 관리</h2>' +
      '<p class="sub">서치콘솔에 노출된 검색어는 자동으로 추적합니다. 자동완성으로 발굴한 후보는 ' +
      "관련 있는 것만 직접 선택하세요 — 추적 키워드 수만큼 순위 추적 비용이 발생합니다.</p>" +
      '<div class="tabs"><button data-m="candidate" class="on">후보</button>' +
      '<button data-m="active">추적 중</button></div>' +
      '<div class="list"></div>' +
      '<div class="bar2"><button class="go2">고른 것 추적하기</button>' +
      '<span class="cnt"></span></div>';
    host.appendChild(sec);

    sec.querySelector(".tabs").addEventListener("click", function (ev) {
      var b = ev.target.closest ? ev.target.closest("[data-m]") : null;
      if (!b) return;
      kwMode = b.getAttribute("data-m");
      sec.querySelectorAll(".tabs button").forEach(function (x) {
        x.classList.toggle("on", x === b);
      });
      sec.querySelector(".go2").textContent =
        kwMode === "candidate" ? "선택 항목 추적" : "추적 해제";
      kwLoad();
    });
    sec.querySelector(".go2").addEventListener("click", function () {
      kwApply(kwMode === "candidate");
    });
    kwLoad();
  }
  keywords();
  if (sel) sel.addEventListener("change", function () { kwLoad(); });

  // ── Claude Code 로 넘길 일 ─────────────────────────────────
  // 웹에서 못 하는 게 분명히 있다(관련성 판단·스킬 해석·자유 질문). 없는 척하지 말고
  // 명령어를 그대로 준다 — 여기서만은 '복사'가 맞는 동작이다.
  var HANDOFF = [
    ["발굴 후보 키워드 선별",
     "관련성 판단이 필요합니다. 대시보드는 서치콘솔 노출 기준으로만 자동 선택합니다.",
     "/capture keywords "],
    ["개선 기회 심층 분석",
     "seo-audit·ai-seo 전문 지침으로 원인과 조치 방안까지 도출합니다.",
     "/capture gaps "],
    ["데이터 질의응답",
     "“클릭수 0인 검색어의 원인은?” 같은 질문에 수집 데이터를 조회해 답변합니다.",
     "/capture ask "],
    ["콘텐츠 작업 계획 수립",
     "저장소 커밋 이력과 대조해 작업 우선순위를 배치합니다.",
     "/create plan "]
  ];

  function handoff() {
    var host = document.querySelector("main");
    if (!host || document.getElementById("sm-cc")) return;
    var sec = document.createElement("section");
    sec.id = "sm-cc";
    sec.innerHTML = '<p class="eyebrow">CLAUDE CODE</p>' +
      "<h2>고급 분석</h2>" +
      '<p class="sub">해석과 판단이 필요한 작업은 Claude Code 플러그인에서 실행합니다. 명령어를 복사해 붙여 넣으세요.</p>' +
      "<ul>" + HANDOFF.map(function (h) {
        return "<li><div><b>" + esc(h[0]) + "</b><span>" + esc(h[1]) + "</span></div>" +
               '<button data-cc="' + esc(h[2]) + '">' + esc(h[2].trim()) + "</button></li>";
      }).join("") + "</ul>";
    host.appendChild(sec);
    sec.addEventListener("click", function (ev) {
      var b = ev.target.closest ? ev.target.closest("[data-cc]") : null;
      if (!b) return;
      var cmd = b.getAttribute("data-cc") + proj();
      navigator.clipboard.writeText(cmd).then(function () {
        var was = b.textContent;
        b.textContent = "복사됨 ✓";
        setTimeout(function () { b.textContent = was; }, 1400);
      });
    });
  }
  handoff();

  // 기회 목록이 다시 그려질 때마다 버튼을 심는다. 원본에는 id 를 담은 속성이 없고
  // 트리아지 버튼의 onclick="setOpp(<id>,...)" 안에만 있다 — 거기서 꺼낸다.
  var wire = function () {
    document.querySelectorAll('button[onclick^="setOpp("]').forEach(function (b) {
      var box = b.parentNode;
      if (!box || box.querySelector("[data-write]")) return;
      var m = (b.getAttribute("onclick") || "").match(/setOpp\((\d+)/);
      if (!m) return;
      var w = document.createElement("button");
      w.setAttribute("data-write", m[1]);
      w.textContent = "글 쓰기";
      box.appendChild(w);
    });
  };
  var all = function () { wire(); wireSteps(); retitle(); };
  new MutationObserver(all).observe(document.body, {childList: true, subtree: true});
  all();
})();
</script>
""".encode("utf-8")


@app.get("/d")
def dash(request: Request):
    _require_uid(request)
    # 호스팅에서는 API 키·의존성 설치·구글 클라이언트 등록이 유저 몫이 아니다(서버가 키를 댄다).
    return HTMLResponse(dashboard.HTML + _DASH_ADDON)


@app.get("/api/projects")
def api_projects(request: Request):
    uid = _require_uid(request)
    conn = store.connect()
    try:
        return [r["project"] for r in store.sites(conn, uid)]
    finally:
        conn.close()


@app.get("/api/data")
def api_data(project: str, request: Request):
    uid = _require_uid(request)
    conn = store.connect()
    try:
        _own(conn, uid, project)
        with store.tenant(conn, uid):
            return dashboard.payload(project)
    except db.ProjectNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    finally:
        conn.close()


@app.get("/api/doctor")
def api_doctor(project: str, request: Request):
    uid = _require_uid(request)
    conn = store.connect()
    try:
        _own(conn, uid, project)
        with store.tenant(conn, uid):
            return dashboard.setup_state(project)
    finally:
        conn.close()


@app.post("/api/opp")
async def api_opp(request: Request):
    # 로컬 대시보드는 X-Token 으로 CSRF 를 막았다. 여기서는 세션 로그인이 그 역할을 하므로
    # 화면이 보내는 헤더는 무시한다.
    uid = _require_uid(request)
    body = await request.json()
    if body.get("status") not in db.OPP_STATUSES:
        raise HTTPException(status_code=400, detail=f"status must be one of {db.OPP_STATUSES}")
    conn = store.connect()
    try:
        with store.tenant(conn, uid):
            c = db.connect()
            try:
                return {"updated": db.set_opportunity_status(c, int(body.get("id") or 0),
                                                             body["status"])}
            finally:
                c.close()
    finally:
        conn.close()


def demo() -> None:
    import tempfile
    from cryptography.fernet import Fernet
    from fastapi.testclient import TestClient

    with tempfile.TemporaryDirectory() as d:
        os.environ["SEOMINER_DATA"] = d
        os.environ["SEOMINER_SECRET_KEY"] = Fernet.generate_key().decode()
        os.environ["GOOGLE_CLIENT_ID"] = "dummy.apps.googleusercontent.com"
        os.environ["GOOGLE_CLIENT_SECRET"] = "dummy"
        os.environ["SESSION_SECRET"] = "x" * 32
        os.environ["OAUTH_REDIRECT_URI"] = "http://localhost:8000/auth/callback"

        c = TestClient(app)

        r = c.get("/healthz")
        assert r.status_code == 200 and r.json() == {"ok": True}, r.text

        r = c.get("/api/properties")
        assert r.status_code == 401, r.text

        # 스케줄러 판정 — 사이트가 없으면 0, 새로 등록하면 즉시 대상, 0 시간이면 꺼진다.
        os.environ["SEOMINER_RUN_EVERY_HOURS"] = "168"
        assert _pending() == 0, "사이트가 없는데 잴 게 있다고 한다"
        c2 = store.connect()
        u2 = store.upsert_user(c2, "sched@example.com")
        store.add_site(c2, u2, "p1", "sc-domain:p1.com", "p1.com")
        c2.close()
        assert _pending() == 1, "등록 직후인데 첫 측정이 안 잡힌다"
        os.environ["SEOMINER_RUN_EVERY_HOURS"] = "0"
        assert _pending() == 1, "0 은 자동 재측정만 꺼야 한다 — 첫 측정까지 막혔다"
        os.environ.pop("SEOMINER_RUN_EVERY_HOURS")

        # dash() 가 여기에 bytes 를 이어붙인다. str 로 바뀌면 그 자리에서 500 이 난다.
        assert isinstance(dashboard.HTML, bytes), "dashboard.HTML 이 bytes 가 아니다"

        # 대시보드·GitHub 경로도 전부 로그인 뒤에 있어야 한다 — 남의 Brain·리포가 열리면 안 된다.
        for path in ("/d", "/api/projects", "/api/data?project=x", "/api/doctor?project=x",
                     "/api/repos", "/auth/github"):
            assert c.get(path).status_code == 401, f"{path} 가 로그인 없이 열렸다"
        for path in ("/api/repo", "/api/create"):
            assert c.post(path, json={}).status_code == 401, f"{path} 가 로그인 없이 열렸다"
        assert c.post("/api/opp", json={"id": 1, "status": "done"}).status_code == 401, \
            "/api/opp 가 로그인 없이 열렸다"

        r = c.get("/auth/login", follow_redirects=False)
        assert r.status_code == 302, (r.status_code, r.text)
        assert r.headers["location"].startswith("https://accounts.google.com"), r.headers
        assert "dummy.apps.googleusercontent.com" in r.headers["location"], \
            "client_id 가 인가 URL 에 안 실렸다 — import 시점에 얼어붙었을 수 있다"
        # PKCE 가 켜져 있으면 콜백까지 code_verifier 를 넘겨야 한다(세션에 저장).
        assert "code_challenge=" in r.headers["location"], "PKCE 가 꺼졌다"
        assert "include_granted_scopes" not in r.headers["location"], \
            "과거 승인 스코프까지 합쳐진다 — 읽기 전용만 받아야 한다"

        # env 가 없으면 조용히 굴러가지 말고 실패해야 한다
        os.environ.pop("GOOGLE_CLIENT_ID")
        r = c.get("/auth/login", follow_redirects=False)
        assert r.status_code == 500, f"GOOGLE_CLIENT_ID 없이 {r.status_code} 로 통과했다"

        print("app: ok")


if __name__ == "__main__":
    demo()