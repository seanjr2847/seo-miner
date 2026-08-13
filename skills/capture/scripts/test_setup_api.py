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

# ── 안내 판정(progress) ─────────────────────────────────────────────
# 씨앗만 있는 새 프로젝트를 "키워드 캐기 완료"로 치면 안내가 한 단계를 건너뛴다.
conn = dashboard.db.connect()
pid = conn.execute("SELECT id FROM projects WHERE name='demo'").fetchone()[0]
pg = dashboard.progress(conn, pid)
assert pg["keywords"] == 2, pg                     # 등록 폼에 적은 씨앗 2개
assert pg["keywords_found"] == 0, pg               # 발굴은 아직 안 돌렸다
assert pg["gsc_days"] == 0 and pg["opps"] == 0, pg
assert pg["creations"] == 0, pg                    # 테이블이 없어도 0으로 답해야 한다

conn.execute("INSERT INTO keywords(project_id, keyword, source, is_active) "
             "VALUES(?,?,?,1)", (pid, "캔 키워드", "autocomplete"))
conn.commit()
assert dashboard.progress(conn, pid)["keywords_found"] == 1
conn.close()

# ── 박제본(export) ──────────────────────────────────────────────────
out = dashboard.export("demo", None)
html = out.read_text("utf-8")
assert out.parent.name == "demo" and out.suffix == ".html", out
assert "window.__SNAPSHOT__=" in html, "스냅샷이 안 박혔다"
assert "<!--SNAPSHOT-->" not in html, "치환이 안 됐다"
assert 'src="http' not in html and 'href="http' not in html, "외부 요청이 섞였다"
snap = json.loads(html.split("window.__SNAPSHOT__=", 1)[1].split("</script>", 1)[0])
assert snap["data"]["project"]["name"] == "demo" and snap["actions"] == [], snap
assert "progress" in snap["data"], "안내 자료가 박제본에 빠졌다"

print(f"ok — 설정 API · 안내 판정 · 박제본 정상 ({HOME})")
