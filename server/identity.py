"""로그인 제공자 둘(구글·GitHub)을 같은 두 동사 뒤에 둔다 — start() / finish().

구글은 로그인이자 서치콘솔을 읽을 자격증명이고, GitHub 은 PR 을 낼 토큰이다. 절차는
둘 다 같은 모양이라 라우트가 어느 쪽인지 몰라도 되게 맞췄다:

    url, carry = identity.start("google")   # carry 를 세션에 실어 콜백까지 나른다
    carry = identity.carried(session, "google", state)
    acct = identity.finish("google", code, carry)
    uid = identity.remember(conn, "google", acct)

설정은 settings.py 가 소유한다. 없으면 NotConfigured 를 던지고, 그 문구는 유저에게
그대로 보인다 — 빈 client_id 로 조용히 굴러가면 유저가 로그인을 눌러야 실패를 안다.
"""
from __future__ import annotations

import secrets
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

# collect_gsc(스코프의 주인)는 엔진 쪽에 산다. app.py 를 거치지 않고 이 파일만 돌려도
# demo() 가 떠야 한다.
_SCRIPTS = str(Path(__file__).resolve().parent.parent / "skills" / "capture" / "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import google.auth.transport.requests
import google.oauth2.id_token
from google_auth_oauthlib.flow import Flow

import collect_gsc
import gh
import settings
import store

PROVIDERS = ("google", "github")
SESSION_KEY = "oauth"


def session_key(provider: str) -> str:
    """carry 를 담는 세션 자리 — 제공자마다 다르다.

    한 자리를 둘이 같이 쓰면 나중에 시작한 흐름이 앞의 carry 를 덮고, carried() 는
    provider 불일치를 만료로 처리한다 — 먼저 시작한 콜백이 반드시 400 으로 떨어진다
    (탭 두 개, GitHub 연결 도중 재로그인).
    """
    return f"{SESSION_KEY}:{provider}"

SCOPES = collect_gsc.SCOPES + ["openid",
                               "https://www.googleapis.com/auth/userinfo.email"]


class NotConfigured(RuntimeError):
    """이 제공자를 쓸 설정이 없다. 문구는 유저에게 그대로 보인다."""


@dataclass(frozen=True)
class Account:
    who: str        # 구글은 이메일, GitHub 은 로그인 이름
    token: str      # 저장할 자격증명 — 구글은 Credentials JSON, GitHub 은 액세스 토큰


def _need(name: str, what: str) -> str:
    try:
        v = settings.get(name)
    except settings.Missing:
        v = None
    if not v:
        raise NotConfigured(
            f"{what}이 아직 연결되지 않았습니다. 운영자에게 문의해 주세요. ({name})")
    return v


def _flow() -> Flow:
    redirect = settings.get("OAUTH_REDIRECT_URI")
    config = {"web": {
        "client_id": _need("GOOGLE_CLIENT_ID", "구글 로그인"),
        "client_secret": _need("GOOGLE_CLIENT_SECRET", "구글 로그인"),
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": [redirect],
    }}
    return Flow.from_client_config(config, scopes=SCOPES, redirect_uri=redirect)


def start(provider: str) -> tuple[str, dict]:
    """(인가 URL, 세션에 실어 콜백까지 나를 것)."""
    if provider not in PROVIDERS:
        raise ValueError(provider)
    state = secrets.token_urlsafe(16)
    if provider == "google":
        flow = _flow()
        # include_granted_scopes 는 쓰지 않는다 — 과거에 승인해 둔 스코프(webmasters 쓰기
        # 등)까지 토큰에 합쳐진다. 여기는 읽기만 필요하다.
        # select_account 가 없으면 로그아웃해도 구글이 직전 계정으로 그냥 들여보내서
        # 계정 전환이 성립하지 않는다. consent 는 refresh token 을 받기 위해 유지한다.
        url, _ = flow.authorization_url(access_type="offline",
                                        prompt="select_account consent", state=state)
        # PKCE: authorization_url() 이 만든 verifier 를 콜백까지 넘겨야 한다. 콜백은 Flow 를
        # 새로 만들기 때문에, 안 넘기면 토큰 교환이 'Missing code verifier' 로 죽는다.
        return url, {"provider": provider, "state": state,
                     "code_verifier": flow.code_verifier}

    cid = _need("GITHUB_CLIENT_ID", "GitHub 연동")
    # repo 스코프: PR 브랜치를 만들려면 쓰기가 필요하다. 머지는 사람이 한다(발행 게이트).
    url = ("https://github.com/login/oauth/authorize"
           f"?client_id={cid}&scope=repo&state={state}"
           f"&redirect_uri={quote(settings.github_redirect(), safe='')}")
    return url, {"provider": provider, "state": state}


def carried(session, provider: str, state: str) -> dict | None:
    """콜백이 세션에서 carry 를 꺼낸다. 제공자나 state 가 안 맞으면 None — 만료됐거나
    남이 붙인 콜백이다. 꺼내는 순간 지운다(한 번만 쓴다)."""
    # 옛 공용 자리도 본다 — 배포 직전에 로그인을 시작한 세션은 아직 SESSION_KEY 에
    # carry 를 담고 있다. 아래 provider/state 검사가 남의 carry 를 걸러내므로 안전하다.
    # 다음 릴리스에서 지운다.
    carry = session.pop(session_key(provider), None) or session.pop(SESSION_KEY, None)
    if not carry or carry.get("provider") != provider or carry.get("state") != state:
        return None
    return carry


def finish(provider: str, code: str, carry: dict) -> Account:
    """인가 코드를 계정으로 바꾼다."""
    if provider not in PROVIDERS:
        raise ValueError(provider)
    if provider == "google":
        flow = _flow()
        flow.code_verifier = carry.get("code_verifier")
        flow.fetch_token(code=code)
        creds = flow.credentials
        email = google.oauth2.id_token.verify_oauth2_token(
            creds.id_token, google.auth.transport.requests.Request(),
            _need("GOOGLE_CLIENT_ID", "구글 로그인"))["email"]
        return Account(email, creds.to_json())

    token = gh.exchange_code(_need("GITHUB_CLIENT_ID", "GitHub 연동"),
                             _need("GITHUB_CLIENT_SECRET", "GitHub 연동"), code)
    return Account(gh.login(token), token)


def remember(conn, provider: str, acct: Account, uid: int | None = None) -> int:
    """토큰을 저장하고 유저 id 를 돌려준다. 구글은 계정 자체를 만들고(로그인), GitHub 은
    이미 로그인한 유저에 얹는다."""
    if provider == "google":
        uid = store.upsert_user(conn, acct.who)
        store.save_token(conn, uid, acct.token)
        return uid
    if uid is None:
        raise ValueError("GitHub 연결은 로그인한 유저에게만 얹는다")
    store.save_github(conn, uid, acct.token, acct.who)
    return uid


def demo() -> None:
    import os
    import tempfile
    from cryptography.fernet import Fernet

    saved = {n: os.environ.get(n) for n in
             ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GITHUB_CLIENT_ID",
              "GITHUB_CLIENT_SECRET", "GITHUB_REDIRECT_URI", "OAUTH_REDIRECT_URI",
              "SEOMINER_DATA", "SEOMINER_SECRET_KEY")}
    try:
        for n in saved:
            os.environ.pop(n, None)

        # 설정이 없으면 조용히 굴러가지 않는다.
        for p in PROVIDERS:
            try:
                start(p)
                raise AssertionError(f"{p}: 설정 없이 인가 URL 이 나왔다")
            except NotConfigured as e:
                assert "운영자에게" in str(e), e
        try:
            start("gitlab")
            raise AssertionError("모르는 제공자가 통과했다")
        except ValueError:
            pass

        os.environ["GOOGLE_CLIENT_ID"] = "dummy.apps.googleusercontent.com"
        os.environ["GOOGLE_CLIENT_SECRET"] = "dummy"
        os.environ["GITHUB_CLIENT_ID"] = "gh-dummy"
        os.environ["GITHUB_CLIENT_SECRET"] = "gh-secret"

        url, carry = start("google")
        assert url.startswith("https://accounts.google.com"), url
        assert "dummy.apps.googleusercontent.com" in url, "client_id 가 안 실렸다"
        assert "code_challenge=" in url, "PKCE 가 꺼졌다"
        assert "include_granted_scopes" not in url, \
            "과거 승인 스코프까지 합쳐진다 — 읽기 전용만 받아야 한다"
        assert carry["code_verifier"], "verifier 를 콜백까지 못 나른다"

        ghurl, ghcarry = start("github")
        assert "client_id=gh-dummy" in ghurl and "scope=repo" in ghurl, ghurl
        assert quote("http://localhost:8000/auth/github/callback", safe="") in ghurl, ghurl
        assert "code_verifier" not in ghcarry, "GitHub 은 PKCE 를 안 쓴다"

        # carry 는 제공자·state 가 맞을 때만, 그리고 한 번만 나온다.
        sess = {SESSION_KEY: dict(carry)}
        assert carried(dict(sess), "google", "틀린state") is None
        assert carried(dict(sess), "github", carry["state"]) is None, "제공자를 섞었다"
        assert carried(sess, "google", carry["state"])["code_verifier"] == carry["code_verifier"]
        assert carried(sess, "google", carry["state"]) is None, "carry 가 두 번 쓰인다"

        # 두 흐름이 동시에 살아 있어도 서로를 안 덮는다 — 자리가 제공자마다 다르다.
        both = {session_key("google"): dict(carry),
                session_key("github"): dict(ghcarry)}
        assert carried(both, "github", ghcarry["state"])["provider"] == "github"
        assert carried(both, "google", carry["state"])["code_verifier"] \
            == carry["code_verifier"], "나중에 시작한 흐름이 앞의 carry 를 덮었다"

        # 토큰 왕복 — 저장한 것이 그대로 돌아와야 한다.
        with tempfile.TemporaryDirectory() as d:
            os.environ["SEOMINER_DATA"] = d
            os.environ["SEOMINER_SECRET_KEY"] = Fernet.generate_key().decode()
            conn = store.connect()
            uid = remember(conn, "google", Account("a@example.com", '{"t":1}'))
            assert store.load_token(conn, uid) == '{"t":1}', "구글 토큰이 안 돌아온다"
            remember(conn, "github", Account("octocat", "ghtok"), uid=uid)
            assert store.github(conn, uid) == ("ghtok", "octocat"), store.github(conn, uid)
            try:
                remember(conn, "github", Account("octocat", "ghtok"))
                raise AssertionError("uid 없이 GitHub 토큰이 저장됐다")
            except ValueError:
                pass
            conn.close()

        print("identity: ok")
    finally:
        for n, v in saved.items():
            os.environ.pop(n, None) if v is None else os.environ.__setitem__(n, v)


if __name__ == "__main__":
    demo()
