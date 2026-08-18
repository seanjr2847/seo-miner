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


def get(path: str) -> tuple[int, dict]:
    try:
        with urllib.request.urlopen(BASE + path) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def post(path: str, body: dict, token: str = dashboard.TOKEN) -> tuple[int, dict]:
    req = urllib.request.Request(BASE + path, json.dumps(body).encode(),
                                 {"Content-Type": "application/json", "X-Token": token})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


# ── /api/doctor (setup_state) 검증 — no_project 상태 ────────────────
code, doc = get("/api/doctor")
assert code == 200, code
expected_keys = {"verdict", "no_project", "must", "extra", "core_ok",
                 "gsc_ok", "nkeys", "show_deps_gsc_btn", "show_setup"}
assert expected_keys.issubset(doc.keys()), f"누락된 키: {expected_keys - doc.keys()}"
assert doc["no_project"] is True
assert not any("/capture add" in s for s in doc["must"]), f"must에 /capture add가 남음: {doc['must']}"
assert doc["show_setup"] is True


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
import yaml  # noqa: E402  (sync_project가 이미 요구하는 부품 — 테스트도 그걸로 읽는다)
doc = yaml.safe_load((HOME / "projects" / "demo.yaml").read_text("utf-8"))
assert doc["gsc_property"] == "sc-domain:demo.com", doc   # 빈 값 → 도메인에서 유추
assert doc["brand_aliases"] == ["데모", "Demo"], doc
assert doc["seed_keywords"] == ["키워드 하나", "키워드 둘"], doc
assert doc["tools"] == [], doc                            # (a) 빈 tools는 빈 리스트
assert "preset" in doc, doc                               # (b) preset 블록 포함
assert doc["preset"]["scoring_bias"] == "ai_citation_gap dominates — best-of list inclusion is the battlefield"
assert "keyword_angles" in doc["preset"] and "ai_prompt_templates" in doc["preset"]
assert dashboard.db.connect().execute(
    "SELECT COUNT(*) FROM projects WHERE name='demo'").fetchone()[0] == 1

# tools 입력 및 다른 type(game)의 preset 블록 검증
code, r = post("/api/setup/project", {
    "name": "gamedemo", "type": "game", "domain": "game.com",
    "tools": "tool1, tool2\ntool3"})
assert code == 200 and r["ok"], r
doc_game = yaml.safe_load((HOME / "projects" / "gamedemo.yaml").read_text("utf-8"))
assert doc_game["tools"] == ["tool1", "tool2", "tool3"], doc_game   # (a) tools 리스트 저장
assert doc_game["preset"]["scoring_bias"] == "list-inclusion citations weigh heaviest (추천 리스트에 끼는 게 전부)"  # (b) game 프리셋

# ── /api/data brand_catalog_empty 검증 ─────────────────────────────
# (c) tools·competitors 없는 demo는 brand_catalog_empty=true, tools 있는 gamedemo는 false
code, d_demo = get("/api/data?project=demo")
assert code == 200, code
assert d_demo.get("brand_catalog_empty") is True, d_demo

code, d_game = get("/api/data?project=gamedemo")
assert code == 200, code
assert d_game.get("brand_catalog_empty") is False, d_game

# 폼 입력에 개행으로 YAML 키를 끼워 넣어도 값으로만 남아야 한다 (f-string 시절의 구멍)
code, r = post("/api/setup/project", {"name": "evil", "type": "saas",
                                      "domain": "evil.com\nname: hacked"})
assert code == 200, r
doc = yaml.safe_load((HOME / "projects" / "evil.yaml").read_text("utf-8"))
assert doc["name"] == "evil" and "hacked" in doc["domain"], doc

code, r = post("/api/setup/project", {"name": "demo", "type": "saas",
                                      "domain": "demo.com"})
assert code == 400 and "이미 있습니다" in r["error"], r

post("/api/setup/keys", {"OPENROUTER_API_KEY": "sk-test", "SERPER_API_KEY": ""})
env = (HOME / "env").read_text("utf-8")
assert env.strip() == "OPENROUTER_API_KEY=sk-test", env
assert os.environ["OPENROUTER_API_KEY"] == "sk-test"      # 진단에 즉시 반영
assert dashboard.doctor.diagnose()["keys"]["openrouter"] is True
code, doc = get("/api/doctor")
assert code == 200 and doc["nkeys"] == 1 and doc["no_project"] is False, doc

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
pg = dashboard.progress(conn, pid)
assert pg["keywords_found"] == 1

# 안내의 "지금 할 것"은 서버 판정(scoring.stage) — 템플릿 JS가 판정하던 시절의 회귀 방지
st = dashboard.scoring.stage(pg, "demo", "demo.com")
assert st["here"] == 1 and st["steps"][1]["id"] == "gsc", st["here"]
assert st["steps"][3]["cmd"] == "/capture add demo", st   # 폼 등록엔 질문이 아직 없다

# ── gather / payload wire format 검증 ────────────────────────────────
p = dashboard.db.get_project(conn, "demo")
gdata = dashboard.gather(conn, p)
assert gdata["project"]["name"] == "demo"
assert isinstance(gdata["opps"], list) and len(gdata["opps"]) <= 200
assert gdata["rules"]["page1"] == dashboard.scoring.PAGE1
assert "trend" in gdata and "progress" in gdata and "guide" in gdata
assert gdata["opps_total"] == 0
assert gdata["brand_catalog_empty"] is True
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
assert snap["data"]["guide"]["here"] == 1, "서버 판정(guide)이 payload에 없다"

print(f"ok — 설정 API · 안내 판정 · 박제본 정상 ({HOME})")
