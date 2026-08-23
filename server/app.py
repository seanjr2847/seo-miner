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
from fastapi.responses import HTMLResponse, RedirectResponse
from google_auth_oauthlib.flow import Flow
from starlette.middleware.sessions import SessionMiddleware

import collect_gsc
import dashboard
import db
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


def _kick() -> None:
    """등록 직후 곧바로 수집을 띄운다 — 틱을 기다리면 그동안 화면이 비어 있다.
    워커가 사이트마다 시작 전에 도장을 찍으므로 스케줄러 틱과 겹쳐도 중복되지 않는다."""
    try:
        subprocess.Popen([sys.executable, str(ROOT / "server" / "worker.py"), "--all"],
                         cwd=str(ROOT))
    except Exception as e:
        print(f"[kick] 실패: {e}", flush=True)      # 실패해도 다음 틱이 잡는다


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
            + ('<span class="st wait">첫 측정 진행 중</span>' if not r["last_run_at"]
               else f'<span class="st">{e(str(r["last_run_at"])[:16])} 측정</span>')
            + '</a></li>' for r in rows)
        block = f'<section><p class="label">측정 중인 사이트</p><ul class="sites">{items}</ul>'
        if any(not r["last_run_at"] for r in rows):
            block += ('<p class="wait-note">서치콘솔 실적 · 색인 상태 · 키워드 순으로 몇 분에 '
                      '걸쳐 채워집니다. 창을 닫아도 계속됩니다.</p>')
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


@app.post("/api/run")
async def api_run(request: Request):
    """'지금 다시 재기' — 로컬 플러그인의 /capture run 에 해당한다.
    웹에는 명령을 칠 곳이 없으므로 버튼이 그 자리를 대신한다."""
    uid = _require_uid(request)
    body = await request.json()
    project = str(body.get("project") or "")
    conn = store.connect()
    try:
        _own(conn, uid, project)
        started = store.request_run(conn, uid, project)
    finally:
        conn.close()
    if started:
        _kick()
    return {"ok": True, "started": started}


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
<style>
  #toggle-setup,#setup{display:none!important}
  #sm-run{font:inherit;font-size:13px;cursor:pointer;border:1px solid currentColor;
    background:transparent;color:inherit;border-radius:3px;padding:4px 12px;opacity:.85}
  #sm-run:hover:not(:disabled){opacity:1}
  #sm-run:disabled{opacity:.45;cursor:default}
  .cmd{display:none!important}   /* '이 명령을 치세요' — 웹에는 칠 곳이 없다 */
</style>
<script>
(function () {
  var host = document.getElementById("refresh");
  if (!host) return;
  var b = document.createElement("button");
  b.id = "sm-run"; b.className = "hbtn"; b.textContent = "지금 다시 재기";
  host.parentNode.insertBefore(b, host);

  function proj() {
    var p = document.getElementById("proj");
    return p ? p.value : "";
  }
  function paint(st) {
    var r = st && st[proj()];
    if (!r) return;
    b.disabled = !!r.running;
    b.textContent = r.running ? "측정 중…" : "지금 다시 재기";
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
  new MutationObserver(wire).observe(document.body, {childList: true, subtree: true});
  wire();
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