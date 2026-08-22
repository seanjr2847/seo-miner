"""GitHub 연동 — 리포 읽기와 PR 생성.

create 스킬의 '발행 게이트'(브랜치 → 커밋 → PR, 머지는 사람이)를 웹에서 그대로 지킨다.
main 에 직접 쓰지 않는다.

로컬 클론을 두지 않는다 — Contents/Git API 로 필요한 파일만 읽고 쓴다. 호스팅에서
리포를 통째로 받으면 디스크와 시간을 먹고, 유저마다 정리 책임이 생긴다.
"""
from __future__ import annotations

import base64

import requests

API = "https://api.github.com"
TIMEOUT = 30
UA = {"User-Agent": "seo-miner", "Accept": "application/vnd.github+json"}


class GitHubError(RuntimeError):
    pass


def _req(method: str, path: str, token: str, **kw) -> dict | list:
    r = requests.request(method, API + path, timeout=TIMEOUT,
                         headers={**UA, "Authorization": f"Bearer {token}"}, **kw)
    if r.status_code >= 400:
        msg = ""
        try:
            msg = r.json().get("message", "")
        except Exception:
            msg = r.text[:200]
        raise GitHubError(f"GitHub {r.status_code}: {msg}")
    return r.json() if r.text else {}


def exchange_code(client_id: str, client_secret: str, code: str) -> str:
    """OAuth 코드 → access token."""
    r = requests.post("https://github.com/login/oauth/access_token", timeout=TIMEOUT,
                      headers={"Accept": "application/json", **UA},
                      data={"client_id": client_id, "client_secret": client_secret,
                            "code": code})
    d = r.json()
    if not d.get("access_token"):
        raise GitHubError(d.get("error_description") or "토큰 교환에 실패했습니다")
    return d["access_token"]


def login(token: str) -> str:
    return _req("GET", "/user", token)["login"]


def repos(token: str, limit: int = 100) -> list[dict]:
    """쓰기 권한이 있는 리포만 — 읽기만 되는 곳에는 PR 브랜치를 못 만든다."""
    out = []
    for page in (1, 2):
        rows = _req("GET", f"/user/repos?per_page=50&page={page}&sort=pushed"
                           f"&affiliation=owner,collaborator,organization_member", token)
        out += [{"full_name": r["full_name"], "default_branch": r["default_branch"],
                 "private": r["private"], "pushed_at": r["pushed_at"]}
                for r in rows if (r.get("permissions") or {}).get("push")]
        if len(rows) < 50:
            break
    return out[:limit]


def tree(token: str, repo: str, branch: str) -> list[str]:
    """리포의 파일 경로 전부(재귀). 관례 파악에 쓴다."""
    d = _req("GET", f"/repos/{repo}/git/trees/{branch}?recursive=1", token)
    if d.get("truncated"):
        pass                      # 큰 리포는 잘린다 — 관례 추론에는 앞부분으로 충분하다
    return [n["path"] for n in d.get("tree", []) if n["type"] == "blob"]


def read_file(token: str, repo: str, path: str, ref: str) -> str:
    d = _req("GET", f"/repos/{repo}/contents/{path}?ref={ref}", token)
    if isinstance(d, list) or d.get("encoding") != "base64":
        raise GitHubError(f"{path} 는 파일이 아닙니다")
    return base64.b64decode(d["content"]).decode("utf-8", "replace")


def open_pr(token: str, repo: str, base: str, branch: str, title: str, body: str,
            files: list[dict], commit_message: str) -> dict:
    """브랜치를 만들어 파일을 쓰고 PR 을 연다. files: [{"path":..., "content":...}]

    브랜치가 이미 있으면 그 위에 얹는다 — 같은 기회를 다시 실행했을 때 충돌로 죽지 않게.
    """
    head = _req("GET", f"/repos/{repo}/git/ref/heads/{base}", token)
    base_sha = head["object"]["sha"]
    try:
        _req("POST", f"/repos/{repo}/git/refs", token,
             json={"ref": f"refs/heads/{branch}", "sha": base_sha})
    except GitHubError as e:
        if "already exists" not in str(e).lower():
            raise

    for f in files:
        payload = {"message": commit_message, "branch": branch,
                   "content": base64.b64encode(f["content"].encode("utf-8")).decode()}
        try:                       # 이미 있는 파일은 sha 를 줘야 덮어쓴다
            cur = _req("GET", f"/repos/{repo}/contents/{f['path']}?ref={branch}", token)
            if isinstance(cur, dict) and cur.get("sha"):
                payload["sha"] = cur["sha"]
        except GitHubError:
            pass                   # 없는 파일 = 새로 만드는 것
        _req("PUT", f"/repos/{repo}/contents/{f['path']}", token, json=payload)

    try:
        pr = _req("POST", f"/repos/{repo}/pulls", token,
                  json={"title": title, "body": body, "head": branch, "base": base})
        return {"url": pr["html_url"], "number": pr["number"], "branch": branch}
    except GitHubError as e:
        if "already exist" in str(e).lower():   # 열린 PR 이 있으면 그걸 돌려준다
            prs = _req("GET", f"/repos/{repo}/pulls?head={repo.split('/')[0]}:{branch}", token)
            if prs:
                return {"url": prs[0]["html_url"], "number": prs[0]["number"],
                        "branch": branch}
        raise


def demo() -> None:
    import types
    calls = []

    def fake(method, path, token, **kw):
        calls.append((method, path))
        if path.endswith("/git/ref/heads/main"):
            return {"object": {"sha": "base123"}}
        if "/contents/" in path and method == "GET":
            raise GitHubError("GitHub 404: Not Found")      # 새 파일
        if method == "POST" and path.endswith("/pulls"):
            return {"html_url": "https://github.com/o/r/pull/7", "number": 7}
        return {}

    g = globals()
    orig, g["_req"] = g["_req"], fake
    try:
        r = open_pr("t", "o/r", "main", "capture/x", "T", "B",
                    [{"path": "src/a.md", "content": "안녕"}], "msg")
        assert r["url"].endswith("/pull/7"), r
        assert ("POST", "/repos/o/r/git/refs") in calls, "브랜치를 안 만들었다"
        assert ("PUT", "/repos/o/r/contents/src/a.md") in calls, "파일을 안 썼다"
        assert not any(m == "PUT" and p.endswith("/heads/main") for m, p in calls), \
            "main 에 직접 썼다 — 발행 게이트 위반"

        # 브랜치가 이미 있어도 죽지 않아야 한다(같은 기회 재실행).
        def fake2(method, path, token, **kw):
            if method == "POST" and path.endswith("/git/refs"):
                raise GitHubError("GitHub 422: Reference already exists")
            return fake(method, path, token, **kw)
        g["_req"] = fake2
        assert open_pr("t", "o/r", "main", "capture/x", "T", "B",
                       [{"path": "a.md", "content": "x"}], "m")["number"] == 7
        print("gh: ok")
    finally:
        g["_req"] = orig


if __name__ == "__main__":
    demo()
