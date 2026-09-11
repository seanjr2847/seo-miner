#!/usr/bin/env python3
"""호스팅 서버(server/app.py)를 TestClient 로 실제로 눌러 보는 검사.

원래 app.py 안의 demo() 였다 — 467줄, 그 파일의 29% 가 테스트였고 TestClient 를
import 하는 유일한 자리도 거기였다. 프로덕션 모듈이 자기 테스트 클라이언트를
들고 뜰 이유가 없다.

run_checks.py 는 test_*.py 를 bare python 으로 돌린다(run_checks.discover) —
그래서 아래 __main__ 이 자기를 돌린다. 다른 test_*.py 들과 같은 관례다.

self-check: python server/test_app.py
"""
import json
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# app 을 먼저 들인다 — app.py 가 skills/capture/scripts 를 sys.path 에 얹으므로
# 아래 수집기·대시보드 import 는 그 뒤에야 풀린다.
from app import (SESSION_SECRET, _dispatch_dep, _kick_dep,   # noqa: E402
                 _require_uid, app, resume_dead_runs)

import collect_ga4      # noqa: E402
import collect_gsc      # noqa: E402
import dashboard        # noqa: E402
import db               # noqa: E402
import doctor           # noqa: E402
import gen_prompts      # noqa: E402
import store            # noqa: E402


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

        # 한 스레드에서 연 커넥션을 다른 스레드에서 쓸 수 있어야 한다. FastAPI 는 sync
        # 의존자(yield)와 sync 라우트를 스레드풀의 서로 다른 스레드에 올린다 — 의존자가
        # 연 커넥션을 라우트가 다른 스레드에서 쓰면 ProgrammingError → 500 이다. 이
        # 파일의 요청은 전부 순차라 같은 스레드를 재사용해 우연히 맞았고, 그래서 한 번도
        # 못 잡았다. 운영에서는 동시 요청 20개 중 3~4개가 500 이었다(09-09 배포 ~ 09-11).
        # 네트워크·타이밍 없이 원인만 찌른다 — 흔들리지 않는다.
        import threading
        old_home = os.environ.get("CAPTURE_HOME")
        os.environ["CAPTURE_HOME"] = str(Path(d) / "xthread")     # 진짜 brain 은 안 건드린다
        try:
            for name, opener in (("store.connect", store.connect),
                                 ("db.connect", db.connect),
                                 ("db.connect_ro", db.connect_ro)):
                conn, err = opener(), []

                def use(conn=conn, err=err):
                    try:
                        conn.execute("SELECT 1").fetchone()
                        conn.close()       # 닫는 것도 다른 스레드에서 돼야 한다 — 안 되면 샌다
                    except Exception as e:
                        err.append(e)

                t = threading.Thread(target=use)
                t.start()
                t.join()
                assert not err, (f"{name} 커넥션을 다른 스레드에서 못 쓴다 — "
                                 f"동시 요청이 500 이 된다: {err[0]}")
        finally:
            if old_home is None:
                os.environ.pop("CAPTURE_HOME", None)
            else:
                os.environ["CAPTURE_HOME"] = old_home

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

        # 대시보드 경로도 전부 로그인 뒤에 있어야 한다 — 남의 Brain 이 열리면 안 된다.
        for path in ("/d", "/api/projects", "/api/data?project=x", "/api/doctor?project=x",
                     "/api/perf?project=x",
                     "/api/settings?project=x", "/api/ai/prompts?project=x",
                     "/api/report?project=x",
                     "/api/keywords?project=x", "/api/run/status", "/api/brain",
                     "/api/ga4/properties?project=x"):
            assert c.get(path).status_code == 401, f"{path} 가 로그인 없이 열렸다"
        for path in ("/api/settings", "/api/ai/prompts",
                     "/api/ai/prompts/edit", "/api/sites", "/api/keywords", "/api/ga4/property"):
            assert c.post(path, json={}).status_code == 401, f"{path} 가 로그인 없이 열렸다"
        assert c.post("/api/opp", json={"id": 1, "status": "done"}).status_code == 401,             "/api/opp 가 로그인 없이 열렸다"
        assert c.post("/api/creation", json={"project": "x", "path": "a.md"}).status_code == 401, \
            "/api/creation 이 로그인 없이 열렸다"

        # 위 목록은 손으로 적은 것이라 **새 라우트는 영영 안 걸린다**. 인증이 정말
        # 한 곳(_require_uid)이라는 것은 라우트 표에서 본다 — 의존자 나무 어딘가에
        # 그 함수가 없으면 그 라우트는 로그인 없이 열린 것이다.
        open_on_purpose = {
            "/healthz",                                     # 상태 확인 — 로그인 이전
            "/", "/auth/login", "/auth/callback", "/auth/logout",   # 로그인 자체
            "/api/cli/token",   # 세션 전용이라 _uid 를 직접 본다(그 라우트 주석 참고)
        }

        def deps_of(dependant):
            out = {dependant.call}
            for sub in dependant.dependencies:
                out |= deps_of(sub)
            return out

        walled = 0
        for route in app.routes:
            path = getattr(route, "path", "")
            if not hasattr(route, "dependant") or path in open_on_purpose:
                continue
            assert _require_uid in deps_of(route.dependant), \
                f"{path} 가 인증 의존자를 안 건다 — 로그인 없이 열린다"
            walled += 1
        assert walled >= 25, f"라우트 표를 못 읽었다(검사한 라우트 {walled}개)"

        # 공유 라우트는 dashboard.ROUTES 가 정본이다 — 표의 항목이 전부 호스팅에 **그
        # 메서드로** 서 있어야 한다. 경로만 보면 GET 으로 선 /api/opp 를 놓친다.
        # 루프가 세운 것이든 손으로 남긴 것(app._HAND_ROUTES)이든 같은 질문이다 —
        # 손으로 남긴 쪽이 지워지면 여기서 걸린다.
        served = {(m, r.path) for r in app.routes if hasattr(r, "methods")
                  for m in r.methods}
        missing = sorted(set(dashboard.ROUTES) - served)
        assert not missing, f"dashboard.ROUTES 에 있는데 호스팅에 없다: {missing}"

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

        # NotConfigured 상태코드 — required(구글) 는 배포가 고장난 것=500.
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

        # GitHub 연동은 떼어 냈다 — 실행은 이 PC 의 개발 도구가 맡는다. 라우트가
        # 남아 있으면(되살아나면) 화면에 없는 길이 서버에만 열려 있는 것이다.
        for gone in ("/api/repos", "/auth/github", "/auth/github/callback"):
            assert c.get(gone).status_code == 404, f"{gone} 가 아직 살아 있다"
        for gone in ("/api/repo", "/api/create"):
            assert c.post(gone, json={}).status_code == 404, f"{gone} 가 아직 살아 있다"

        # 설정 — 값이 없으면 전역 기본값이 실효값이고, 프리셋 밖 값은 서버가 막는다.
        r = c.get("/api/settings?project=p1")
        assert r.status_code == 200 and r.json()["run_every_hours"] == 168.0, r.text
        assert r.json()["presets"][0]["h"] == 0, r.text
        # p1 은 아직 Brain 에 동기화되지 않았다(store.add_site 로만 등록) — GA4 는
        # 없는 것으로 답해야지, 설정 화면 전체가 깨지면 안 된다.
        assert r.json()["ga4_property"] == "", "Brain 없는 사이트에서 설정이 깨진다"
        # GitHub 연동은 떼어 냈다 — 설정 응답에 그 흔적이 남으면 화면이 없는 칸을 그린다.
        for gone in ("repo", "repo_branch", "github_connected", "github_enabled"):
            assert gone not in r.json(), f"/api/settings 에 {gone} 가 아직 실린다"
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
            # opts — 모르는 키를 조용히 무시하면 --device mobile 을 준 사용자가
            # 데스크톱 결과를 모바일 결과로 읽는다. 400 이어야 한다.
            for bad in ({"rank.없는노브": "x"}, {"device": "mobile"}, {"gaps.limit": 1},
                        {"rank.project": "남의사이트"}, {"rank.dry_run": True}, "mobile"):
                assert c.post("/api/run", json={"project": "p1", "opts": bad}
                              ).status_code == 400, f"모르는 opt 가 통과했다: {bad!r}"
            r = c.post("/api/run", json={"project": "p1", "stages": "competitors",
                                         "opts": {"rank.device": "mobile"}})
            assert r.status_code == 200 and r.json()["started"], r.text
            assert spawned and "competitors" in spawned[0], spawned
            # 검증을 통과한 값은 워커가 알아듣는 `--opt K=V` 로 간다.
            assert "--opt" in spawned[0] and "rank.device=mobile" in spawned[0], spawned

            # /api/sites — 등록 진입점. 여태 demo() 어디에도 안 나왔다.
            assert c.post("/api/sites", json={"properties": []}).status_code == 400
            r = c.post("/api/sites", json={"properties": ["sc-domain:new1.com"]})
            assert r.status_code == 200 and r.json()["ok"], r.text
            assert r.json()["added"][0]["project"] == "new1", r.json()
            assert kicked, "새 사이트 등록인데 워커를 안 띄웠다"
            # 언어-지역은 사이트마다 — 목록 밖은 400, 고른 값은 그 사이트의 yaml 로 간다.
            assert c.post("/api/sites", json={"properties": ["sc-domain:new2.com"],
                                              "locales": {"sc-domain:new2.com": "xx-XX"}}
                          ).status_code == 400, "목록 밖 언어로 등록된다"
            r = c.post("/api/sites", json={"properties": ["sc-domain:new2.com"],
                                           "locales": {"sc-domain:new2.com": "en-GB"}})
            assert r.status_code == 200 and r.json()["ok"], r.text
            assert c.get("/api/settings?project=new2").json()["locale"] == "en-GB", \
                "고른 언어가 안 실렸다"
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

        # 질문 목록·편집 — 만들기 버튼만 있고 무엇이 심겼는지 볼 데가 없었다.
        assert c.get("/api/ai/prompts?project=없는사이트").status_code == 404,             "남의 사이트 질문이 열린다"
        conn = store.connect()
        try:
            with store.tenant(conn, u2) as t:
                assert t.home == store.home(u2), "Tenant.home 이 그 유저의 home 이 아니다"
                # 격리를 env 가 아니라 객체로 확인한다: CAPTURE_DB 를 엉뚱한 곳으로 흔들어
                # 놔도(운영 실수·다른 스레드의 잔재) t.brain() 은 home= 을 직접 넘기니
                # 흔들리면 안 된다 — env(CAPTURE_HOME) 만 보던 예전 방식이면 이 경우
                # 조용히 엉뚱한 brain.db 를 연다.
                rogue = Path(d) / "rogue.db"
                os.environ["CAPTURE_DB"] = str(rogue)
                try:
                    bc = t.brain()
                    try:
                        opened = Path(bc.execute("PRAGMA database_list").fetchone()["file"])
                        assert opened == t.home / "brain.db",                             f"CAPTURE_DB 에 흔들려 엉뚱한 brain 을 열었다: {opened}"
                        assert not rogue.exists(), "env 기반 경로에 파일을 만들었다"
                    finally:
                        bc.close()
                finally:
                    os.environ.pop("CAPTURE_DB", None)
                assert dashboard.create_project(
                    {"name": "p1", "type": "local_clinic", "domain": "p1.com"})["ok"]
        finally:
            conn.close()
        # 공유 라우트(dashboard.ROUTES 를 도는 루프가 세운 것) — 로그인 뒤 실제로
        # 눌러 본다. 손으로 감싸던 시절의 상태 코드·문구가 그대로여야 한다.
        r = c.get("/api/data?project=p1")
        assert r.status_code == 200 and isinstance(r.json(), dict), r.text
        r = c.get("/api/triage?project=p1")
        assert r.status_code == 200 and r.json()["rows"] == [], r.text
        for path in ("/api/data", "/api/triage"):
            assert c.get(f"{path}?project=없는사이트").status_code == 404, \
                f"{path} 로 남의 사이트가 열린다"
        # call 에 넘어가는 모양이 로컬 Handler 와 같은가 — 표의 항목을 잠깐 갈아끼워
        # 받은 인자를 본다(제네릭 핸들러가 요청 때 표를 조회하므로 된다).
        seen, real_triage = [], dashboard.ROUTES[("GET", "/api/triage")]
        dashboard.ROUTES[("GET", "/api/triage")] = lambda *a: seen.append(a) or {}
        try:
            assert c.get("/api/triage?project=p1&date=&date=d1&date=d2&x=").status_code == 200
        finally:
            dashboard.ROUTES[("GET", "/api/triage")] = real_triage
        # project 는 빼서 따로, 빈 값은 버리고 첫 값(로컬 parse_qs 와 같다), body 는 None
        assert seen == [("p1", {"date": "d1"}, None)], seen

        r = c.post("/api/verdict", json={"project": "p1", "keys": ["a"], "verdict": "엉뚱"})
        assert r.status_code == 400 and "판정값" in r.json()["detail"], r.text
        r = c.post("/api/verdict", json={"project": "p1", "keys": ["a"], "verdict": "hold"})
        assert r.status_code == 200 and "updated" in r.json(), r.text
        r = c.post("/api/opp", json={"project": "p1", "id": 1, "status": "엉뚱"})
        assert r.status_code == 400 and "상태값" in r.json()["detail"], r.text
        r = c.post("/api/opp", json={"project": "p1", "id": 999, "status": "done"})
        assert r.status_code == 200 and r.json() == {"updated": 0}, r.text
        r = c.post("/api/creation", json={"project": "p1", "opportunity_id": 999,
                                          "path": "a.md"})
        assert r.status_code == 404, "남의 기회 번호로 기록이 남는다"
        for path in ("/api/verdict", "/api/opp", "/api/creation"):
            assert c.post(path, json={"project": "없는사이트", "id": 1, "status": "done",
                                      "keys": [], "path": "a.md"}).status_code == 404, \
                f"{path} 로 남의 사이트를 건드릴 수 있다"

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

            # 언어-지역 — 목록 밖은 400, 바꾸면 Brain 이 그 값을 들고 있다.
            s0 = c.get("/api/settings?project=p1").json()
            assert s0["locale"] == "ko-KR" and s0["locales"][0]["code"] == "ko-KR", s0
            assert c.post("/api/settings", json={"project": "p1", "locale": "xx-XX"}
                          ).status_code == 400, "목록 밖 언어가 저장된다"
            assert c.post("/api/settings", json={"project": "없는사이트", "locale": "ja-JP"}
                          ).status_code == 404
            r = c.post("/api/settings", json={"project": "p1", "locale": "ja-JP"})
            assert r.status_code == 200 and r.json()["locale"] == "ja-JP", r.text
            assert c.get("/api/settings?project=p1").json()["locale"] == "ja-JP", "언어가 저장이 안 됐다"

            # 403 — 스코프 부족(analytics.readonly 가 나중에 더해졌다). 재로그인 안내가 나와야 한다.
            def _scope_missing(admin_svc):
                from googleapiclient.errors import HttpError
                class _Resp:
                    status = 403
                    reason = "Forbidden"
                raise HttpError(_Resp(), b'{"error": "insufficient scope"}')
            collect_ga4.list_properties = _scope_missing
            r = c.get("/api/ga4/properties?project=p1")
            assert r.status_code == 403 and "다시 구글 계정으로 로그인" in r.json()["detail"], \
                r.text

            # 스코프가 모자란 걸 doctor 로 미리 알면 — API 를 아예 안 부르고 같은
            # 403 을 예측 가능하게 낸다(브라우저 로그인·SystemExit 경로를 안 탄다).
            def _must_not_call(*a, **kw):
                raise AssertionError("스코프 부족인데 GA4 API 를 불렀다")
            collect_ga4.get_service = _must_not_call
            doctor.gsc_missing_scopes = lambda: [
                "https://www.googleapis.com/auth/analytics.readonly"]
            r = c.get("/api/ga4/properties?project=p1")
            assert r.status_code == 403 and "다시 구글 계정으로 로그인" in r.json()["detail"], \
                r.text
            doctor.gsc_missing_scopes = lambda: []

            # 아예 연결이 안 된 상태(토큰 없음) — 실제로 터진 게 이거다(Railway 로그,
            # 2026-08-31: 토큰이 없어 collect_gsc 가 브라우저를 열려다 500). "재로그인"이
            # 아니라 "아직 연결 안 됨" — 한 번도 안 붙인 사람에게 "다시"는 말이 안 된다.
            db.gsc_connected = lambda: False
            r = c.get("/api/ga4/properties?project=p1")
            assert r.status_code == 403 and "연결돼 있지 않습니다" in r.json()["detail"],                 r.text
            db.gsc_connected = lambda: True

            # 같은 함정이 사이트 등록 화면(/api/properties)에도 있었다 — 스코프가
            # 모자란 토큰으로 부르면 collect_gsc 가 브라우저 로그인을 열려다 죽고
            # 502 만 남았다. 화면에는 "서치콘솔 권한이 있는지 확인해 주세요"가 떠서,
            # 재로그인 한 번이면 끝날 일을 권한 화면에서 찾게 만들었다(2026-09-01).
            real_gsc_service = collect_gsc.get_service
            collect_gsc.get_service = _must_not_call
            try:
                doctor.gsc_missing_scopes = lambda: [
                    "https://www.googleapis.com/auth/analytics.readonly"]
                r = c.get("/api/properties")
                assert r.status_code == 403 and "다시 구글 계정으로 로그인" in r.json()["detail"],                     r.text
                doctor.gsc_missing_scopes = lambda: []
                db.gsc_connected = lambda: False
                r = c.get("/api/properties")
                assert r.status_code == 403 and "연결돼 있지 않습니다" in r.json()["detail"],                     r.text
                db.gsc_connected = lambda: True
            finally:
                collect_gsc.get_service = real_gsc_service

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

        # 런 결과가 화면까지 간다 — 여태 실패한 단계가 있어도 화면은 아무 말도 안 했다.
        cs = store.connect()
        try:
            sid_st = store.site(cs, u2, "p1")["id"]
            store.mark_done(cs, sid_st, ok=False, error="rank: DataForSEO 잔액 없음(402)")
        finally:
            cs.close()
        st = c.get("/api/run/status").json()["p1"]
        assert st["last_ok"] == 0 and "402" in st["last_error"], st
        # 죽은 런 회수 — 컨테이너 교체로 워커가 통째로 죽은 자리. 회수하고 그 사이트만
        # 다시 띄운다(--all 은 due 판정에 걸려 방금 잰 사이트를 빼 버린다).
        # 죽은 워커가 사이트 brain 에 남긴 끝나지 않은 runs 행 — 09-02 호스팅 #49 가 그랬다.
        bconn = db.connect(home=store.home(u2))
        try:
            pid_b = bconn.execute("SELECT id FROM projects WHERE name='p1'").fetchone()[0]
            dead_rid = bconn.execute(
                "INSERT INTO runs(project_id,kind,started_at) VALUES(?,'rank','2026-09-02T03:00:00Z')",
                (pid_b,)).lastrowid
            bconn.commit()
        finally:
            bconn.close()
        cs = store.connect()
        try:
            store.mark_run(cs, sid_st)
        finally:
            cs.close()
        spawned = []
        assert resume_dead_runs(dispatch=lambda *a: spawned.append(a)) == ["p1"], "죽은 런을 못 찾았다"
        assert spawned and spawned[0][:1] == ("--user",) and "p1" in spawned[0], spawned
        st = c.get("/api/run/status").json()["p1"]
        assert st["running"] is False and st["last_ok"] == 0, st
        assert "서버 재시작" in c.get("/api/run/log?project=p1").json()["text"], \
            "왜 끊겼는지 사용자에게 한 줄도 안 간다"
        bconn = db.connect(home=store.home(u2))
        try:
            fin, notes = bconn.execute("SELECT finished_at, notes FROM runs WHERE id=?",
                                       (dead_rid,)).fetchone()
        finally:
            bconn.close()
        assert fin and store.ORPHAN_RUN_NOTE in (notes or ""), \
            f"회수했는데 brain 의 런이 끝나지 않은 채 남았다: {(fin, notes)}"
        assert resume_dead_runs(dispatch=lambda *a: spawned.append(a)) == [], "도는 런이 없는데 또 띄운다"
        cs = store.connect()
        try:
            store.mark_run(cs, sid_st)     # 아래 런 로그 검사가 보던 '도는 중' 으로 되돌린다
            # 도는 사이트의 런은 닫지 않는다 — 서버가 running 이라 말하는 동안은 살아 있는 워커의 것이다.
            bconn = db.connect(home=store.home(u2))
            try:
                live_rid = bconn.execute(
                    "INSERT INTO runs(project_id,kind,started_at) VALUES(?,'rank','2026-09-02T03:00:00Z')",
                    (pid_b,)).lastrowid
                bconn.commit()
                store.close_orphan_runs(cs)
                assert bconn.execute("SELECT finished_at FROM runs WHERE id=?",
                                     (live_rid,)).fetchone()[0] is None, \
                    "도는 사이트의 런을 닫았다 — 살아 있는 워커의 이력을 끊었다"
                # 아래 검사가 보는 이력을 흔들지 않게 이 두 행은 치운다
                bconn.execute("DELETE FROM runs WHERE id IN (?,?)", (dead_rid, live_rid))
                bconn.commit()
            finally:
                bconn.close()
        finally:
            cs.close()

        assert c.get("/api/keywords?project=없는사이트").status_code == 404
        r = c.get("/api/keywords?project=p1")
        assert r.status_code == 200 and r.json()["keywords"] == [], r.text
        assert c.post("/api/keywords", json={"project": "p1", "ids": []}).status_code == 400
        assert c.post("/api/keywords", json={"project": "없는사이트", "ids": [1], "active": True}
                      ).status_code == 404, "남의 사이트 키워드를 건드릴 수 있다"

        assert c.get("/api/report?project=없는사이트").status_code == 404
        r = c.get("/api/report?project=p1")
        assert r.status_code == 200 and len(r.content) > 0, r.status_code

        # brain.db 통째 — 로컬 사본(remote.py pull)의 원본. 파일이 아니라 backup()
        # 스냅샷이 나가야 하고, 나간 뒤 임시 파일은 남지 않아야 한다.
        tmps = set(Path(tempfile.gettempdir()).glob("brain-*.db"))
        r = c.get("/api/brain")
        assert r.status_code == 200 and r.content.startswith(b"SQLite format 3\x00"), \
            (r.status_code, r.content[:20])
        snap = Path(tempfile.gettempdir()) / "seo-miner-brain-check.db"
        snap.write_bytes(r.content)
        sc = sqlite3.connect(snap)
        try:                       # 찢어진 파일이 아니라 열리는 DB 여야 한다
            assert sc.execute("SELECT count(*) FROM projects").fetchone() is not None
        finally:
            sc.close()
            snap.unlink(missing_ok=True)
        assert not (set(Path(tempfile.gettempdir()).glob("brain-*.db")) - tmps), \
            "스냅샷 임시 파일이 남았다"

        # --- 원격 CLI 통로 ---------------------------------------------------
        # 런 로그 — since 는 문자 오프셋이고 text 는 그 뒤부터다. p1 은 위
        # /api/run 이 mark_run 을 찍어 놓고 워커는 가짜였으니 아직 '도는 중'이다.
        cs = store.connect()
        try:
            sid_p1 = store.site(cs, u2, "p1")["id"]
            store.save_run_log(cs, sid_p1, "첫줄\n둘째줄\n")
        finally:
            cs.close()
        assert c.get("/api/run/log?project=p1").json() == {
            "text": "첫줄\n둘째줄\n", "next": 7, "running": True}, c.get("/api/run/log?project=p1").json()
        assert c.get("/api/run/log?project=p1&since=3").json()["text"] == "둘째줄\n",             c.get("/api/run/log?project=p1&since=3").json()
        assert c.get("/api/run/log?project=p1&since=999").json()["text"] == "",             "오프셋이 넘쳐도 빈 문자열이어야 한다"
        assert c.get("/api/run/log?project=없는사이트").status_code == 404, "남의 런 로그가 열린다"

        # 새 런을 띄우면 지난 런의 텍스트는 그 자리에서 사라진다 — 워커가 뜨기까지
        # 몇 초가 걸리고, 그 사이 폴링이 옛 로그를 읽으면 끝난 런을 지금 도는 런으로 읽는다.
        app.dependency_overrides[_dispatch_dep] = lambda: (lambda *a: None)
        try:
            cs = store.connect()
            try:
                store.mark_done(cs, sid_p1)            # '도는 중' 을 풀어야 다시 뜬다
            finally:
                cs.close()
            assert c.post("/api/run", json={"project": "p1", "stages": "gsc"}
                          ).json()["started"], "런이 안 떴다"
            assert c.get("/api/run/log?project=p1").json() == {
                "text": "", "next": 0, "running": True}, c.get("/api/run/log?project=p1").json()
        finally:
            app.dependency_overrides.pop(_dispatch_dep, None)

        # /api/sql — 가드는 db.run_sql 것을 그대로 쓴다. 여기서 재구현하지 않는다.
        r = c.post("/api/sql", json={"project": "p1", "sql": "DELETE FROM keywords"})
        assert r.status_code == 400 and "read-only" in r.json()["detail"], r.text
        # 문자열 검사만으로는 못 막는 모양 — 읽기 전용 커넥션이 잡아야 한다.
        r = c.post("/api/sql", json={"project": "p1",
                                     "sql": "WITH x AS (SELECT 1) DELETE FROM keywords"})
        assert r.status_code == 400 and "조회 전용" in r.json()["detail"], r.text
        r = c.post("/api/sql", json={"project": "p1",
                                     "sql": "SELECT name FROM projects WHERE name='p1'"})
        assert r.status_code == 200 and r.json() == [{"name": "p1"}], r.text
        assert c.post("/api/sql", json={"project": "없는사이트", "sql": "SELECT 1"}
                      ).status_code == 404, "남의 Brain 에 질의할 수 있다"

        # 토큰 발급은 세션 전용이고, 발급된 토큰은 라우트 전부를 연다.
        r = c.post("/api/cli/token")
        assert r.status_code == 200 and r.json()["token"].startswith("smt_"), r.status_code
        token = r.json()["token"]

        c.cookies.clear()                       # 세션을 버리고 토큰만으로 붙어 본다
        h = {"Authorization": f"Bearer {token}"}
        assert c.get("/api/projects").status_code == 401, "세션이 안 지워졌다"
        assert "p1" in c.get("/api/projects", headers=h).json(), "Bearer 가 안 먹는다"
        assert c.get("/api/projects", headers={"Authorization": "Bearer smt_notatoken"}
                     ).status_code == 401, "아무 토큰이나 통과한다"
        assert c.get("/api/projects", headers={"Authorization": token}
                     ).status_code == 401, "Bearer 스킴 없이 통과한다"
        # 훅이 _require_uid 한 곳이라는 증거 — 라우트를 하나도 안 고쳤는데 열린다.
        assert c.post("/api/sql", json={"project": "p1", "sql": "SELECT 1 AS n"},
                      headers=h).json() == [{"n": 1}], "Bearer 로 /api/sql 이 안 열린다"
        # 토큰으로 토큰 재발급은 금지 — 유출된 토큰이 스스로를 갱신하면 영영 산다.
        assert c.post("/api/cli/token", headers=h).status_code == 401,             "Bearer 로 토큰을 재발급했다"
        assert c.post("/api/cli/token").status_code == 401, "세션 없이 토큰이 발급됐다"

        # env 가 없으면 조용히 굴러가지 말고 실패해야 한다
        os.environ.pop("GOOGLE_CLIENT_ID")
        r = c.get("/auth/login", follow_redirects=False)
        assert r.status_code == 500, f"GOOGLE_CLIENT_ID 없이 {r.status_code} 로 통과했다"
        assert "GOOGLE_CLIENT_ID" in r.json()["detail"], r.text

        # 임시 폴더를 지우기 전에 순환 참조를 거둔다. 윈도우는 **열려 있는 파일을
        # 못 지운다** — 그래서 여기서 안 거두면 아래 TemporaryDirectory 정리가
        # PermissionError 로 죽는다(검사는 다 통과한 뒤에).
        # 열려 있는 파일은 db.run_sql 의 읽기 전용 brain 커넥션이다: 조회 전용
        # 거절(`WITH … DELETE`)이 conn.close() 앞의 sys.exit 로 나가서 안 닫힌다
        # (db.py:1559-1566). 그 커넥션은 예외 트레이스백이 만든 순환에 걸려 있어
        print("app: ok")


if __name__ == "__main__":
    demo()
