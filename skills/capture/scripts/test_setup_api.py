#!/usr/bin/env python3
"""대시보드 설정 API 자체점검 — `python test_setup_api.py` (임시 폴더에서만 돈다).

토큰 게이트 · 사이트 등록 · 키 저장/삭제만 확인한다. pip 설치 액션(deps)은
네트워크를 타므로 제외 — 액션 이름 검증까지만 본다.
"""
import json
import re
import os
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from threading import Thread

HOME = Path(tempfile.mkdtemp(prefix="seo-miner-test-"))
os.environ["CAPTURE_HOME"] = str(HOME)          # import 전에 걸어야 db가 여기를 본다
os.environ["GSC_CONFIG_DIR"] = str(HOME / "mcp-gsc")
os.environ["GSC_TOKEN_FILE"] = str(HOME / "gsc_token.json")
os.environ.pop("OPENROUTER_API_KEY", None)
sys.path.insert(0, str(Path(__file__).parent))
import dashboard  # noqa: E402
import stage      # noqa: E402
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
                 "gsc_ok", "nkeys", "show_deps_gsc_btn", "show_skills_btn",
                 "show_setup"}
assert expected_keys.issubset(doc.keys()), f"누락된 키: {expected_keys - doc.keys()}"
assert doc["no_project"] is True
# 산문에는 명령이 안 들어간다(복사할 것은 cmd 자리다). 여기서는 그 규칙과
# "사이트가 없으면 first_project 를 배너에 또 적지 않는다"를 같이 본다.
assert not any("/capture add" in m["msg"] for m in doc["must"]), f"must 산문에 명령이 남음: {doc['must']}"
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
pg = stage.progress(conn, pid)
assert pg["keywords"] == 2, pg                     # 등록 폼에 적은 씨앗 2개
assert pg["keywords_found"] == 0, pg               # 발굴은 아직 안 돌렸다
assert pg["gsc_days"] == 0 and pg["opps"] == 0, pg
assert pg["creations"] == 0, pg                    # 테이블이 없어도 0으로 답해야 한다

conn.execute("INSERT INTO keywords(project_id, keyword, source, is_active) "
             "VALUES(?,?,?,1)", (pid, "캔 키워드", "autocomplete"))
conn.commit()
pg = stage.progress(conn, pid)
assert pg["keywords_found"] == 1

# 안내의 "지금 할 것"은 서버 판정(stage.state) — 템플릿 JS가 판정하던 시절의 회귀 방지
st = stage.from_progress(pg, "demo", "demo.com")
assert st["here"] == 1 and st["steps"][1]["id"] == "gsc", st["here"]
assert st["steps"][3]["cmd"] == "/capture add demo", st   # 폼 등록엔 질문이 아직 없다

# ── stage.gsc_state() 3-상태 검증 (connected / pending / none) ───────
token_file = HOME / "gsc_token.json"
client_file = HOME / "gsc_oauth_client.json"

# (1) pending 상태: 클라이언트 존재 + 토큰 없음
token_file.unlink(missing_ok=True)
client_file.write_text('{"installed": {}}', "utf-8")
assert stage.gsc_state() == "pending"

# (2) connected 상태: 토큰 파일 존재
token_file.write_text('{"token": "xyz"}', "utf-8")
assert stage.gsc_state() == "connected"

# (3) none 상태: 인증 수단 없음 (클라이언트 없음 + 번들 비활성)
token_file.unlink(missing_ok=True)
client_file.unlink(missing_ok=True)
_orig_bundled = dashboard.db.gsc_oauth_bundled
try:
    dashboard.db.gsc_oauth_bundled = lambda: HOME / "nonexistent.json"
    assert stage.gsc_state() == "none"
finally:
    dashboard.db.gsc_oauth_bundled = _orig_bundled

# ── GSC 연결 여부에 따른 stage.state() here 차이 검증 ──────────────────
p = dashboard.db.get_project(conn, "demo")

# GSC 미연결 상태: gsc_days가 있어도 here는 1 (gsc 단계)
token_file.unlink(missing_ok=True)
conn.execute("INSERT INTO gsc_snapshots(project_id, snapshot_date, period_days, query, page, clicks, impressions, position) "
             "VALUES(?, '2026-08-20', 28, 'q', 'p', 1, 10, 5)", (pid,))
conn.commit()
pg_with_gsc = stage.progress(conn, pid)
assert pg_with_gsc["gsc_days"] > 0
st_disc = stage.state(conn, p)
assert st_disc["here"] == 1, f"미연결 상태에서는 here가 1이어야 함: {st_disc['here']}"
assert st_disc["steps"][1]["done"] is False

# GSC 연결 상태: gsc_days가 있으면 gsc 단계 완료되어 here가 다음 단계(3)로 넘어감
token_file.write_text('{"token": "xyz"}', "utf-8")
st_conn = stage.state(conn, p)
assert st_conn["here"] != 1, f"연결 상태에서는 here가 1이 아니어야 함: {st_conn['here']}"
assert st_conn["steps"][1]["done"] is True
# 이 테스트는 맨 위에서 OPENROUTER_API_KEY 를 지운다 = 키 없는 로컬 사용자다.
# 그러면 AI 단계는 이 환경에서 못 하는 선택 단계라 안내가 건너뛴다 — 예전에는 여기
# 붙어서 아하 모먼트(gaps)에 영영 못 갔다.
assert st_conn["here"] == 4 and st_conn["steps"][4]["id"] == "gaps",     f"키 없으면 ai 를 건너뛰고 4(gaps): {st_conn['here']}"
assert st_conn["steps"][3]["skip"] is True
os.environ["OPENROUTER_API_KEY"] = "k"
try:
    assert stage.state(conn, p)["here"] == 3, "키가 있으면 ai(3)가 다음 걸음이어야 한다"
finally:
    os.environ.pop("OPENROUTER_API_KEY", None)

# ── /api/doctor 와 /api/data 의 「다음 할 일」 일치 검증 ──────────────
# 화면이 보고 있는 사이트를 같이 보낸다 — 안내는 그 사이트를 따라가야 한다.
code, doc = get("/api/doctor?project=demo")
assert code == 200, code
code, d_demo = get("/api/data?project=demo")
assert code == 200, d_demo
assert doc["guide"] is not None, "doctor에 guide가 없음"
assert doc["project"] == "demo", doc["project"]

# 사이트가 여럿인데 어느 것인지 안 알려주면 **아무거나 집지 않는다**.
# 예전에는 projects[0](먼저 등록한 것)을 집어서, 무관한 리포에서 /setup 을 돌려도
# 늘 같은 사이트를 띄웠다 (사용자 신고).
code, doc_amb = get("/api/doctor")
assert code == 200, code
assert len(doc_amb["projects"]) > 1, doc_amb["projects"]
assert doc_amb["project"] is None and doc_amb["guide"] is None, doc_amb["project"]
# 상황은 요약(verdict)이 말하고, 할 일은 must 가 말한다 — 둘 다 있어야 한다.
assert "어느 것인지 모릅니다" in doc_amb["verdict"], doc_amb["verdict"]
assert any("등록된 사이트:" in m["msg"] for m in doc_amb["must"]), doc_amb["must"]
# 복사할 것은 cmd 로 따로 실린다 — 산문에 도로 섞이면 호스팅 배너까지 따라간다.
amb = next(m for m in doc_amb["must"] if "등록된 사이트:" in m["msg"])
assert amb["cmd"] == "/create profile <이름>", amb
assert doc["guide"]["here"] == d_demo["guide"]["here"], (doc["guide"]["here"], d_demo["guide"]["here"])
assert doc["guide"]["steps"][doc["guide"]["here"]]["cmd"] == d_demo["guide"]["steps"][d_demo["guide"]["here"]]["cmd"]

# 테스트 환경 정리: 이후 export 등 기존 테스트를 위해 토큰 및 추가 스냅샷 정리
token_file.unlink(missing_ok=True)
conn.execute("DELETE FROM gsc_snapshots WHERE project_id=?", (pid,))
conn.commit()

# ── gather / payload wire format 검증 ────────────────────────────────
# striking·trend 행 검증을 위해 스냅샷 1건 추가
conn.execute("""INSERT INTO gsc_snapshots(project_id, snapshot_date, period_days,
                  query, page, clicks, impressions, ctr, position)
                VALUES(?, '2026-08-20', 28, '스트라이킹', NULL, 5, 200, 0.025, 8.0)""",
             (pid,))
conn.commit()

p = dashboard.db.get_project(conn, "demo")
gdata = dashboard.gather(conn, p)
assert gdata["schema"] == 1, f"schema 필드 1 기대, 실제: {gdata.get('schema')}"
assert gdata["project"]["name"] == "demo"
assert isinstance(gdata["opps"], list) and len(gdata["opps"]) <= 200
assert gdata["rules"]["page1"] == dashboard.scoring.PAGE1
assert "trend" in gdata and "progress" in gdata and "guide" in gdata
assert gdata["opps_total"] == 0
assert gdata["brand_catalog_empty"] is True

# striking 행 검증: gap, band가 실려 있음
assert len(gdata["striking"]) > 0, "striking 행이 있어야 함"
for srow in gdata["striking"]:
    assert "gap" in srow, f"striking 행에 gap 필드 누락: {srow}"
    assert "band" in srow, f"striking 행에 band 필드 누락: {srow}"
    assert srow["band"] in ("page1", "page2")

# trend 행 검증: period_days를 나타내던 'p' 필드가 없음
assert len(gdata["trend"]) > 0, "trend 행이 있어야 함"
for trow in gdata["trend"]:
    assert "d" in trow and "clk" in trow and "imp" in trow and "q" in trow
    assert "p" not in trow, f"trend 행에 p 필드가 제거되어야 함: {trow}"

# 검증 후 스냅샷 정리
conn.execute("DELETE FROM gsc_snapshots WHERE project_id=?", (pid,))
conn.commit()
conn.close()

# ── 레포 프리필 — CNAME > package.json, 이름은 파일명 규칙으로 정제 ──
repo = HOME / "fake-repo"
repo.mkdir()
(repo / "CNAME").write_text("https://mysite.example.com\n", "utf-8")
(repo / "package.json").write_text('{"name": "@me/My Site!", "homepage": "https://ignored.com"}', "utf-8")
pf = dashboard.repo_prefill(repo)
assert pf["domain"] == "mysite.example.com", pf          # CNAME이 homepage보다 우선
assert pf["gsc_property"] == "sc-domain:mysite.example.com", pf
assert pf["name"] == "My-Site", pf                       # 허용 문자만 남는다
assert dashboard.repo_prefill(HOME / "빈폴더없음") == {}  # 없어도 안 죽고, 못 찾으면 빈 dict

# Claude 판단 프리필(prefill.json) 병합 — 판단은 빈 칸을 채우고, 실측(CNAME)이 이긴다
dashboard.PREFILL_FILE.write_text(json.dumps({
    "domain": "judged.com", "seed_keywords": ["씨앗 하나", "씨앗 둘"],
    "tools": ["ecrett"], "ignored_key": "버려짐"}, ensure_ascii=False), "utf-8")
pf2 = dashboard.repo_prefill(repo)
assert pf2["domain"] == "mysite.example.com", pf2         # CNAME > 판단
assert pf2["seed_keywords"] == ["씨앗 하나", "씨앗 둘"] and pf2["tools"] == ["ecrett"], pf2
assert "ignored_key" not in pf2                           # 허용 키만 통과
# 등록이 성공하면 프리필은 삭제된다 — 다음 사이트 폼에 새면 오염
code, r = post("/api/setup/project", {"name": "prefilled", "type": "saas",
                                      "domain": "judged.com"})
assert code == 200 and r["ok"], r
assert not dashboard.PREFILL_FILE.exists(), "등록 후에도 prefill.json이 남아 있다"

# ── 박제본(export) ──────────────────────────────────────────────────
out = dashboard.export("demo", None)
html = out.read_text("utf-8")
assert out.parent.name == "demo" and out.suffix == ".html", out
assert "window.__SNAPSHOT__=" in html, "스냅샷이 안 박혔다"
assert "<!--SNAPSHOT-->" not in html, "치환이 안 됐다"
# 금지 대상은 **외부 요청**이지 외부 링크가 아니다 — <a href> 는 사용자가 눌러야
# 열리므로 페이지를 열 때 아무것도 안 부른다. [설정] 패널의 키 발급 링크가 여기 걸렸다.
assert 'src="http' not in html, "외부 요청이 섞였다 (src)"
assert "@import" not in html, "외부 요청이 섞였다 (@import)"
# <link> 자체는 금지가 아니다 — 파비콘이 data: URI 로 인라인돼 있다. 막을 것은
# 바깥에서 끌어오는 스타일시트뿐이다.
assert not re.search(r'<link\b[^>]*?href="https?:', html), "외부 요청이 섞였다 (스타일시트)"
snap = json.loads(html.split("window.__SNAPSHOT__=", 1)[1].split("</script>", 1)[0])
assert snap["data"]["schema"] == 1, f"박제본 schema 필드 1 기대, 실제: {snap['data'].get('schema')}"
assert snap["data"]["project"]["name"] == "demo" and snap["actions"] == [], snap
assert "progress" in snap["data"], "안내 자료가 박제본에 빠졌다"
assert snap["data"]["guide"]["here"] == 1, "서버 판정(guide)이 payload에 없다"

print(f"ok — 설정 API · 안내 판정 · 박제본 정상 ({HOME})")
