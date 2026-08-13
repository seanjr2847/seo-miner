#!/usr/bin/env python3
"""대시보드 설정 API 자체점검 — `python test_setup_api.py` (임시 폴더에서만 돈다).

토큰 게이트 · 사이트 등록 · 키 저장/삭제만 확인한다. pip 설치 액션(deps)은
네트워크를 타므로 제외 — 액션 이름 검증까지만 본다.
"""
import json
import os
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from threading import Thread

HOME = Path(tempfile.mkdtemp(prefix="seo-miner-test-"))
os.environ["CAPTURE_HOME"] = str(HOME)          # import 전에 걸어야 db가 여기를 본다
os.environ.pop("OPENROUTER_API_KEY", None)
sys.path.insert(0, str(Path(__file__).parent))
import dashboard  # noqa: E402
from http.server import ThreadingHTTPServer  # noqa: E402

srv = ThreadingHTTPServer(("127.0.0.1", 0), dashboard.Handler)
Thread(target=srv.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{srv.server_address[1]}"


def post(path: str, body: dict, token: str = dashboard.TOKEN) -> tuple[int, dict]:
    req = urllib.request.Request(BASE + path, json.dumps(body).encode(),
                                 {"Content-Type": "application/json", "X-Token": token})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


code, _ = post("/api/setup/project", {"name": "x", "type": "saas", "domain": "x.com"},
               token="wrong")
assert code == 403, f"토큰 없이 통과됨: {code}"

code, r = post("/api/setup/run", {"action": "rm -rf /"})
assert code == 400 and "unknown" in r["error"], r

code, r = post("/api/setup/project", {"name": "../evil", "type": "saas",
                                      "domain": "x.com"})
assert code == 400, f"경로 탈출 이름이 통과됨: {r}"

code, r = post("/api/setup/project", {
    "name": "demo", "type": "saas", "domain": "demo.com", "locale": "ko-KR",
    "gsc_property": "", "brand_aliases": "데모, Demo",
    "seed_keywords": "키워드 하나\n키워드 둘", "competitors_manual": ""})
assert code == 200 and r["ok"], r
yml = (HOME / "projects" / "demo.yaml").read_text("utf-8")
assert "gsc_property: sc-domain:demo.com" in yml, yml     # 빈 값 → 도메인에서 유추
assert '["데모", "Demo"]' in yml, yml
assert '["키워드 하나", "키워드 둘"]' in yml, yml
assert dashboard.db.connect().execute(
    "SELECT COUNT(*) FROM projects WHERE name='demo'").fetchone()[0] == 1

code, r = post("/api/setup/project", {"name": "demo", "type": "saas",
                                      "domain": "demo.com"})
assert code == 400 and "이미 있습니다" in r["error"], r

post("/api/setup/keys", {"OPENROUTER_API_KEY": "sk-test", "SERPER_API_KEY": ""})
env = (HOME / "env").read_text("utf-8")
assert env.strip() == "OPENROUTER_API_KEY=sk-test", env
assert os.environ["OPENROUTER_API_KEY"] == "sk-test"      # 진단에 즉시 반영
assert dashboard.doctor.diagnose()["keys"]["openrouter"] is True

post("/api/setup/keys", {"OPENROUTER_API_KEY": ""})       # 빈 값 = 삭제
assert (HOME / "env").read_text("utf-8").strip() == ""
assert "OPENROUTER_API_KEY" not in os.environ

print(f"ok — 설정 API 정상 ({HOME})")
