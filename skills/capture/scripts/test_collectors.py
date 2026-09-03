#!/usr/bin/env python3
"""자체점검 — `python test_collectors.py` (임시 폴더에서만 돈다, 진짜 Brain은 안 건드림).

I/O 경계(네트워크 수집기, doctor, GSC 인증)를 네트워크 호출 없이 검증한다:
  · collect_ai: OpenRouter 응답 모킹, url_citation 파싱, cited 판정, 실패 집계
  · expand_keywords: Google Suggest 모킹, is_active=0 적재, locale 보존, 50% 초과 실패율 경고
  · collect_serp: serp_adapter.fetch 모킹, write_rank_snapshot 적재, depth 밖 None, AIO 미측정 None
  · collect_gsc/collect_index --dry-run: 인증도 네트워크도 안 타고 계획만 찍는지
  · doctor --json: subprocess 실행, exit code 0, JSON 구조(verdict, next_command) 검증
  · doctor marketing_skills: CLAUDE_SKILLS_DIR 기반 탐지, must 요구 문구 및 필수 7개
    (+ 선택 aso = 명부 ALL_SKILLS 8개) 완비 시 해제 검증
  · install_skills: 스킬 완비 시 실행 방지, 누락 시 marketplace/install 호출 검증
  · gsc_query: 창 계산·필터 파싱·노출 가중평균 (즉석 조회 — MCP 서버를 대신한다)
  · run_all: 체인 순서(gsc→ga4→index→keywords→rank→ai→competitors→gaps→report), 유료 키 없을 시 건너뜀,
    gsc 실패 시 즉시 중단, 스테이지 옵션 전달, 결과 리스트(StageResult)의 행 수·비용 합산
"""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HOME = Path(tempfile.mkdtemp(prefix="seo-miner-collector-test-"))
os.environ["CAPTURE_HOME"] = str(HOME)
sys.path.insert(0, str(Path(__file__).parent))

import collect_ai         # noqa: E402
import collect_serp       # noqa: E402
import db                 # noqa: E402
import expand_keywords    # noqa: E402
import requests           # noqa: E402
import serp_adapter       # noqa: E402


class FakeResponse:
    def __init__(self, data, status_code=200):
        self._data = data
        self.status_code = status_code

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


def _project(conn, name="t", domain="e.com", locale="ko-KR"):
    conn.execute(
        "INSERT OR IGNORE INTO projects(name, domain, locale) VALUES(?, ?, ?)",
        (name, domain, locale),
    )
    conn.commit()
    return conn.execute("SELECT * FROM projects WHERE name=?", (name,)).fetchone()


def test_collect_ai_cycle_and_failure_summary():
    """collect_ai: 가짜 OpenRouter 응답으로 1건 성공(cited=1), 1건 실패 처리하여
    ai_checks 기록, cited 판정, runs 요약의 실패 카운트가 정상 반영되는지 검증."""
    conn = db.connect()
    p = _project(conn, "ai_test_proj", domain="e.com")

    # 2개 프롬프트 등록
    conn.execute(
        "INSERT INTO ai_prompts(project_id, prompt, category, is_active) VALUES(?, 'ecrett 후기 알려줘', '후기', 1)",
        (p["id"],),
    )
    conn.execute(
        "INSERT INTO ai_prompts(project_id, prompt, category, is_active) VALUES(?, '실패할 프롬프트 질문', '비교', 1)",
        (p["id"],),
    )
    conn.commit()

    prompt_rows = conn.execute(
        "SELECT id, prompt FROM ai_prompts WHERE project_id=? ORDER BY id", (p["id"],)
    ).fetchall()
    p_succ_id, p_fail_id = prompt_rows[0]["id"], prompt_rows[1]["id"]
    conn.close()

    orig_post = collect_ai.requests.post
    orig_env_key = os.environ.get("OPENROUTER_API_KEY")
    os.environ["OPENROUTER_API_KEY"] = "fake-openrouter-key"

    def fake_post(url, *args, **kwargs):
        assert url == collect_ai.OPENROUTER_URL, f"Unexpected URL: {url}"
        json_body = kwargs.get("json", {})
        messages = json_body.get("messages", [])
        user_msg = ""
        for m in messages:
            if m.get("role") == "user":
                user_msg = m.get("content", "")
        if "ecrett 후기" in user_msg:
            # annotations에 url_citation 포함 -> cited=1 기대
            return FakeResponse({
                "choices": [{
                    "message": {
                        "content": "ecrett 서비스는 매우 훌륭합니다. 자세한 내용은 공식 사이트 참고.",
                        "annotations": [{
                            "type": "url_citation",
                            "url_citation": {"url": "https://e.com/review-page"}
                        }]
                    }
                }],
                "usage": {"prompt_tokens": 15, "completion_tokens": 25}
            })
        elif "실패할 프롬프트" in user_msg:
            return FakeResponse({"error": "Rate limit exceeded"}, status_code=429)
        raise RuntimeError(f"Unexpected prompt in fake_post: {user_msg}")

    collect_ai.requests.post = fake_post
    try:
        collect_ai.collect("ai_test_proj", engines="chatgpt",
                           samples=1, throttle=0)
    finally:
        collect_ai.requests.post = orig_post
        if orig_env_key is not None:
            os.environ["OPENROUTER_API_KEY"] = orig_env_key
        else:
            os.environ.pop("OPENROUTER_API_KEY", None)

    conn = db.connect()
    # 1. 성공 프롬프트에 대해 ai_checks 가 기록되고 cited=1 판정되었는지 확인
    check_row = conn.execute(
        "SELECT prompt_id, engine, mentioned, cited, answer_excerpt FROM ai_checks WHERE prompt_id=?",
        (p_succ_id,),
    ).fetchone()
    assert check_row is not None, "성공 프롬프트의 ai_checks 행이 기록되지 않음"
    assert check_row["engine"] == "chatgpt"
    assert check_row["cited"] == 1, f"cited가 1이어야 함 (e.com 인용): {dict(check_row)}"
    assert "ecrett 서비스는 매우 훌륭합니다" in check_row["answer_excerpt"]

    # 2. 실패 프롬프트는 ai_checks에 기록되지 않음
    fail_check = conn.execute(
        "SELECT id FROM ai_checks WHERE prompt_id=?", (p_fail_id,)
    ).fetchone()
    assert fail_check is None, "실패한 프롬프트는 ai_checks에 없어야 함"

    # 3. runs 요약에 api_calls=1, errors=1 이 반영되었는지 확인
    run_row = conn.execute(
        "SELECT api_calls, notes FROM runs WHERE project_id=? AND kind='ai' ORDER BY id DESC LIMIT 1",
        (p["id"],),
    ).fetchone()
    assert run_row is not None, "run 기록이 생성되지 않음"
    assert run_row["api_calls"] == 1, f"성공 1건이어야 함: {run_row['api_calls']}"
    assert "errors=1" in (run_row["notes"] or ""), f"notes에 errors=1 반영 필요: {run_row['notes']}"
    conn.close()


def test_expand_keywords_success_and_failure_rate_warning():
    """expand_keywords: Google Suggest 호출 성공 N + 실패 M 교체 ->
    후보가 is_active=0으로 적재되고 locale이 채워지는 것,
    실패율 50% 초과 시 stderr 경고 블록이 출력되는 것 assert."""
    conn = db.connect()
    p = _project(conn, "expand_test_proj", locale="ko-KR")
    # seed keyword 등록
    db.add_keyword_candidates(conn, p["id"], [("마케팅", "ko-KR", "seed")])
    conn.close()

    orig_get = expand_keywords.requests.get
    orig_modifiers = expand_keywords.modifiers

    # modifiers를 3개로 축소하여 총 4회 요청 (seed + 3개 mod)
    # 1회 성공 + 3회 실패 -> 실패율 75% (> 50%)
    expand_keywords.modifiers = lambda locale, hl: ["ㄱ", "ㄴ", "ㄷ"]

    def fake_get(url, *args, **kwargs):
        assert url == expand_keywords.SUGGEST_URL, f"Unexpected URL: {url}"
        params = kwargs.get("params", {})
        q = params.get("q", "")
        if q == "마케팅":
            # 1회차 성공: 2개 제안어 반환
            return FakeResponse([q, ["마케팅 전략", "마케팅 자동화"]])
        else:
            # 2, 3, 4회차 실패 (HTTP 429)
            return FakeResponse({"error": "rate limit"}, status_code=429)

    expand_keywords.requests.get = fake_get
    orig_stderr = sys.stderr
    stderr_buf = io.StringIO()

    try:
        sys.stderr = stderr_buf
        expand_keywords.collect("expand_test_proj", mode="autocomplete",
                                throttle=0)
    finally:
        expand_keywords.requests.get = orig_get
        expand_keywords.modifiers = orig_modifiers
        sys.stderr = orig_stderr

    stderr_output = stderr_buf.getvalue()

    # 1. stderr에 50% 초과 경고 블록 출력 확인
    assert "Suggest 3/4 실패" in stderr_output, f"실패 카운트 경고 누락: {stderr_output}"
    assert "Suggest 실패율 75%" in stderr_output, f"실패율 75% 경고 누락: {stderr_output}"
    assert "IP 차단으로 보입니다" in stderr_output, f"IP 차단 경고 누락: {stderr_output}"
    assert "!" * 62 in stderr_output, f"구분선 누락: {stderr_output}"

    # 2. 후보가 is_active=0으로 적재되고 locale이 채워진 것 확인
    conn = db.connect()
    cands = conn.execute(
        "SELECT keyword, locale, is_active, source FROM keywords WHERE project_id=? AND source='autocomplete' ORDER BY keyword",
        (p["id"],),
    ).fetchall()
    assert len(cands) == 2, f"후보 2개가 적재되어야 함: {len(cands)}"
    for row in cands:
        assert row["is_active"] == 0, f"is_active=0 이어야 함: {dict(row)}"
        assert row["locale"] == "ko-KR", f"locale이 ko-KR 이어야 함: {dict(row)}"
        assert row["source"] == "autocomplete"
    assert {r["keyword"] for r in cands} == {"마케팅 전략", "마케팅 자동화"}
    conn.close()


def test_collect_serp_ranking_and_none_position_aio():
    """collect_serp: serp_adapter.fetch 결과를 고정 데이터로 교체 ->
    1) 순위권 내 키워드는 write_rank_snapshot으로 position/url/aio 적재
    2) depth 밖 키워드는 position=None 으로 저장
    3) aio 미측정(None)은 0이 아닌 None(NULL)으로 보존
    4) 부산물 키워드(related/paa)가 is_active=0, source='serp'로 적재."""
    conn = db.connect()
    p = _project(conn, "serp_test_proj", domain="e.com", locale="ko-KR")
    conn.execute(
        "INSERT INTO keywords(project_id, keyword, locale, is_active) VALUES(?, '랭킹 키워드', 'ko-KR', 1)",
        (p["id"],),
    )
    conn.execute(
        "INSERT INTO keywords(project_id, keyword, locale, is_active) VALUES(?, '순위밖 키워드', 'ko-KR', 1)",
        (p["id"],),
    )
    conn.commit()
    kw_rows = conn.execute(
        "SELECT id, keyword FROM keywords WHERE project_id=? AND is_active=1 ORDER BY id", (p["id"],)
    ).fetchall()
    kw_in_id, kw_out_id = kw_rows[0]["id"], kw_rows[1]["id"]
    conn.close()

    orig_fetch = serp_adapter.fetch

    def fake_fetch(provider, keyword, locale, depth=10, device="desktop"):
        if keyword == "랭킹 키워드":
            return {
                "top": [
                    {"pos": 1, "domain": "other1.com", "url": "https://other1.com/a", "title": "O1"},
                    {"pos": 2, "domain": "e.com", "url": "https://e.com/ranked-page", "title": "My Page"},
                ],
                "serp_features": ["people_also_ask", "ai_overview"],
                "aio_present": 1,
                "aio_domains": ["e.com", "other1.com"],
                "related": ["연관 검색어 1"],
                "paa": ["자주 묻는 질문 1"],
                "cost": 0.003,
            }
        elif keyword == "순위밖 키워드":
            return {
                "top": [
                    {"pos": 1, "domain": "comp1.com", "url": "https://comp1.com/1", "title": "C1"},
                    {"pos": 2, "domain": "comp2.com", "url": "https://comp2.com/2", "title": "C2"},
                ],
                "serp_features": ["related_searches"],
                "aio_present": None,  # 미측정
                "aio_domains": [],
                "related": [],
                "paa": [],
                "cost": 0.001,
            }
        raise ValueError(f"Unexpected keyword: {keyword}")

    serp_adapter.fetch = fake_fetch
    try:
        collect_serp.collect("serp_test_proj", provider="dataforseo",
                             throttle=0)
    finally:
        serp_adapter.fetch = orig_fetch

    conn = db.connect()
    # 1. 랭킹 키워드 검증 (position=2, aio_present=1, aio_cited=1)
    snap1 = conn.execute(
        "SELECT position, url, aio_present, aio_cited FROM rank_snapshots WHERE keyword_id=? ORDER BY id DESC LIMIT 1",
        (kw_in_id,),
    ).fetchone()
    assert snap1 is not None, "랭킹 키워드의 rank_snapshot이 기록되어야 함"
    assert snap1["position"] == 2
    assert snap1["url"] == "https://e.com/ranked-page"
    assert snap1["aio_present"] == 1
    assert snap1["aio_cited"] == 1

    # 2. 순위밖 키워드 검증 (position=None, aio_present=None, aio_cited=None)
    snap2 = conn.execute(
        "SELECT position, url, aio_present, aio_cited FROM rank_snapshots WHERE keyword_id=? ORDER BY id DESC LIMIT 1",
        (kw_out_id,),
    ).fetchone()
    assert snap2 is not None, "순위밖 키워드의 rank_snapshot이 기록되어야 함"
    assert snap2["position"] is None, f"순위 밖이면 position=None이어야 함: {snap2['position']}"
    assert snap2["url"] is None
    assert snap2["aio_present"] is None, f"aio 미측정은 None이어야 함: {snap2['aio_present']}"
    assert snap2["aio_cited"] is None, f"aio_cited 미측정은 None이어야 함: {snap2['aio_cited']}"

    # 3. 부산물 키워드(related/paa) 적재 확인
    harvested = conn.execute(
        "SELECT keyword, locale, is_active, source FROM keywords WHERE project_id=? AND source='serp' ORDER BY keyword",
        (p["id"],),
    ).fetchall()
    assert len(harvested) == 2, f"부산물 2개 적재 필요: {len(harvested)}"
    for h in harvested:
        assert h["is_active"] == 0
        assert h["locale"] == "ko-KR"
        assert h["source"] == "serp"
    assert {h["keyword"] for h in harvested} == {"연관 검색어 1", "자주 묻는 질문 1"}
    conn.close()


def test_collect_serp_today_skip_and_force():
    """(a) collect_serp: 오늘(date(checked_at)=오늘) 이미 확인된 키워드는 건너뛰고,
    --force 옵션 지정 시 재수집을 수행하는지 검증."""
    conn = db.connect()
    p = _project(conn, "serp_skip_proj", domain="e.com", locale="ko-KR")
    conn.execute(
        "INSERT INTO keywords(project_id, keyword, locale, is_active) VALUES(?, '오늘_이미_확인', 'ko-KR', 1)",
        (p["id"],),
    )
    conn.execute(
        "INSERT INTO keywords(project_id, keyword, locale, is_active) VALUES(?, '오늘_미확인', 'ko-KR', 1)",
        (p["id"],),
    )
    conn.commit()

    rows = conn.execute(
        "SELECT id, keyword FROM keywords WHERE project_id=? ORDER BY id", (p["id"],)
    ).fetchall()
    kw_done_id, kw_new_id = rows[0]["id"], rows[1]["id"]

    # 오늘 날짜로 kw_done_id 에 대한 rank_snapshot 미리 삽입
    db.write_rank_snapshot(conn, kw_done_id, 3, "https://e.com/page", checked_at=db.now())
    conn.close()

    fetch_calls = []
    orig_fetch = serp_adapter.fetch

    def mock_fetch(provider, keyword, locale, depth=10, device="desktop"):
        fetch_calls.append((keyword, device))
        return {
            "top": [{"pos": 1, "domain": "e.com", "url": "https://e.com/1", "title": "T"}],
            "serp_features": [],
            "aio_present": None,
            "aio_domains": [],
            "related": [],
            "paa": [],
            "cost": 0.002,
        }

    serp_adapter.fetch = mock_fetch

    try:
        # 1. 기본 실행: kw_done_id 는 skip -> kw_new_id 만 1회 호출
        fetch_calls.clear()
        collect_serp.collect("serp_skip_proj", provider="dataforseo",
                             throttle=0)
        assert len(fetch_calls) == 1, f"오늘 이미 확인된 키워드는 skip되어야 함 (호출수={len(fetch_calls)})"
        assert fetch_calls[0][0] == "오늘_미확인"

        # 2. --force 실행: 2개 모두 재확인
        fetch_calls.clear()
        collect_serp.collect("serp_skip_proj", provider="dataforseo",
                             throttle=0, force=True)
        assert len(fetch_calls) == 2, f"--force 시 2개 모두 호출되어야 함 (호출수={len(fetch_calls)})"
        assert {call[0] for call in fetch_calls} == {"오늘_이미_확인", "오늘_미확인"}

        # 3. --ids 지정 + skip / force 검증
        fetch_calls.clear()
        collect_serp.collect("serp_skip_proj", provider="dataforseo",
                             throttle=0, ids=str(kw_done_id))
        assert len(fetch_calls) == 0, f"--ids 로 이미 확인된 것만 지정 시 0건이어야 함: {fetch_calls}"

        fetch_calls.clear()
        collect_serp.collect("serp_skip_proj", provider="dataforseo",
                             throttle=0, ids=str(kw_done_id), force=True)
        assert len(fetch_calls) == 1, f"--ids + --force 시 1건 호출되어야 함: {fetch_calls}"
        assert fetch_calls[0][0] == "오늘_이미_확인"
    finally:
        serp_adapter.fetch = orig_fetch


def test_collect_ai_today_skip_and_force():
    """(b) collect_ai: 오늘 시작된 kind='ai' 런에서 이미 기록된 (prompt_id, engine, sample_idx)는
    건너뛰고, --force 면 재실행하는지 검증."""
    conn = db.connect()
    p = _project(conn, "ai_skip_proj", domain="e.com")
    conn.execute(
        "INSERT INTO ai_prompts(project_id, prompt, category, is_active) VALUES(?, '오늘_확인_질문', '추천', 1)",
        (p["id"],),
    )
    conn.execute(
        "INSERT INTO ai_prompts(project_id, prompt, category, is_active) VALUES(?, '오늘_미확인_질문', '비교', 1)",
        (p["id"],),
    )
    conn.commit()

    rows = conn.execute(
        "SELECT id, prompt FROM ai_prompts WHERE project_id=? ORDER BY id", (p["id"],)
    ).fetchall()
    p_done_id, p_new_id = rows[0]["id"], rows[1]["id"]

    # 오늘 날짜로 ai 런 및 prompt 1의 check 기록 생성
    with db.run(conn, p["id"], "ai") as r:
        db.record_ai_check(conn, p_done_id, r.id, "chatgpt", 0, 1, 0, [], "답변 내용")
    conn.close()

    post_calls = []
    orig_post = collect_ai.requests.post
    orig_env_key = os.environ.get("OPENROUTER_API_KEY")
    os.environ["OPENROUTER_API_KEY"] = "fake-key"

    def mock_post(url, *args, **kwargs):
        json_body = kwargs.get("json", {})
        messages = json_body.get("messages", [])
        user_msg = next((m.get("content", "") for m in messages if m.get("role") == "user"), "")
        post_calls.append(user_msg)
        return FakeResponse({
            "choices": [{
                "message": {
                    "content": "응답 내용입니다.",
                    "annotations": []
                }
            }],
            "usage": {"prompt_tokens": 10, "completion_tokens": 20}
        })

    collect_ai.requests.post = mock_post

    try:
        # 1. 기본 실행: p_done_id 는 skip -> p_new_id 만 1회 호출
        post_calls.clear()
        collect_ai.collect("ai_skip_proj", engines="chatgpt",
                           samples=1, throttle=0)
        assert len(post_calls) == 1, f"오늘 이미 확인된 질문은 skip되어야 함 (호출수={len(post_calls)})"
        assert post_calls[0] == "오늘_미확인_질문"

        # 2. --force 실행: 2개 질문 모두 호출
        post_calls.clear()
        collect_ai.collect("ai_skip_proj", engines="chatgpt",
                           samples=1, throttle=0, force=True)
        assert len(post_calls) == 2, f"--force 시 2개 모두 호출되어야 함 (호출수={len(post_calls)})"
        assert set(post_calls) == {"오늘_확인_질문", "오늘_미확인_질문"}
    finally:
        collect_ai.requests.post = orig_post
        if orig_env_key is not None:
            os.environ["OPENROUTER_API_KEY"] = orig_env_key
        else:
            os.environ.pop("OPENROUTER_API_KEY", None)


def test_serp_device_in_body_and_validation():
    """(c) device 값이 DataForSEO 요청 body에 실림 + 허용값 외 에러 검증."""
    # 1. DataForSEO 요청 body에 device 값이 실리는지 확인
    orig_env_login = os.environ.get("DATAFORSEO_LOGIN")
    orig_env_pw = os.environ.get("DATAFORSEO_PASSWORD")
    os.environ["DATAFORSEO_LOGIN"] = "fake_login"
    os.environ["DATAFORSEO_PASSWORD"] = "fake_pw"

    captured_bodies = []
    import requests
    orig_req_post = requests.post

    def mock_requests_post(url, *args, **kwargs):
        captured_bodies.append(kwargs.get("json"))
        return FakeResponse({
            "tasks": [{
                "status_code": 20000,
                "result": [{
                    "items": [{"type": "organic", "rank_group": 1, "url": "https://a.com", "title": "A"}]
                }]
            }],
            "cost": 0.002
        })

    requests.post = mock_requests_post
    try:
        # mobile 요청
        captured_bodies.clear()
        serp_adapter.fetch_dataforseo("검색어1", "ko-KR", depth=5, device="mobile")
        assert len(captured_bodies) == 1
        assert captured_bodies[0][0]["device"] == "mobile", f"device가 mobile이어야 함: {captured_bodies[0]}"

        # desktop 요청
        captured_bodies.clear()
        serp_adapter.fetch_dataforseo("검색어2", "ko-KR", depth=5, device="desktop")
        assert len(captured_bodies) == 1
        assert captured_bodies[0][0]["device"] == "desktop", f"device가 desktop이어야 함: {captured_bodies[0]}"
    finally:
        requests.post = orig_req_post
        if orig_env_login is not None:
            os.environ["DATAFORSEO_LOGIN"] = orig_env_login
        else:
            os.environ.pop("DATAFORSEO_LOGIN", None)
        if orig_env_pw is not None:
            os.environ["DATAFORSEO_PASSWORD"] = orig_env_pw
        else:
            os.environ.pop("DATAFORSEO_PASSWORD", None)

    # 2. 허용값 외 입력 시 ValueError 즉시 발생
    try:
        serp_adapter.fetch("dataforseo", "키워드", "ko-KR", device="tablet")
        raise AssertionError("허용되지 않는 device는 에러를 발생시켜야 함")
    except ValueError as e:
        assert "desktop" in str(e) and "mobile" in str(e)

    # 3. serper 경로에서 mobile 요청 시 stderr 경고 및 caveats 확인
    assert any("모바일" in c for c in serp_adapter.caveats("serper"))
    orig_key = os.environ.get("SERPER_API_KEY")
    os.environ["SERPER_API_KEY"] = "fake_serper"
    orig_stderr = sys.stderr
    buf = io.StringIO()
    try:
        sys.stderr = buf
        requests.post = lambda *args, **kwargs: FakeResponse({"organic": []})
        serp_adapter.fetch_serper("키워드", "ko-KR", depth=5, device="mobile")
    finally:
        requests.post = orig_req_post
        sys.stderr = orig_stderr
        if orig_key is not None:
            os.environ["SERPER_API_KEY"] = orig_key
        else:
            os.environ.pop("SERPER_API_KEY", None)

    assert "serper는 desktop만 — 이 런은 desktop으로 측정됩니다" in buf.getvalue()


def test_location_mapping_expansion():
    """(d) 확장 로케일 매핑 검증 (de->Germany, pt->Brazil, zh->Taiwan 등)."""
    assert serp_adapter.location("de-DE")[0] == "Germany"
    assert serp_adapter.location("pt-BR")[0] == "Brazil"
    assert serp_adapter.location("fr-FR")[0] == "France"
    assert serp_adapter.location("es-ES")[0] == "Spain"
    assert serp_adapter.location("it-IT")[0] == "Italy"
    assert serp_adapter.location("nl-NL")[0] == "Netherlands"
    assert serp_adapter.location("pl-PL")[0] == "Poland"
    assert serp_adapter.location("ru-RU")[0] == "Russia"
    assert serp_adapter.location("tr-TR")[0] == "Turkey"
    assert serp_adapter.location("vi-VN")[0] == "Vietnam"
    assert serp_adapter.location("th-TH")[0] == "Thailand"
    assert serp_adapter.location("id-ID")[0] == "Indonesia"
    assert serp_adapter.location("ar-AE")[0] == "United Arab Emirates"
    assert serp_adapter.location("hi-IN")[0] == "India"
    assert serp_adapter.location("zh-TW")[0] == "Taiwan"
    assert serp_adapter.warn_unmapped("de-DE") is False
    assert serp_adapter.warn_unmapped("pt-BR") is False
    assert serp_adapter.warn_unmapped("xx-XX") is True


def test_dry_run_never_touches_auth():
    """--dry-run 은 비용 고지다 — 인증이 하나도 없는 컴퓨터에서도 끝까지 가야 한다.

    호출 수를 보려고 dry-run 을 돌렸는데 브라우저 로그인이 열리거나 "인증이
    없습니다"로 죽으면 고지 구실을 못 한다. get_service 를 지뢰로 바꿔 놓고,
    실행 기록(runs)·적재까지 안 남는지 같이 본다.
    """
    import collect_gsc
    import collect_index

    conn = db.connect()
    conn.execute("INSERT OR IGNORE INTO projects(name, domain, locale, gsc_property) "
                 "VALUES('dry_proj', 'd.com', 'ko-KR', 'sc-domain:d.com')")
    conn.commit()
    pid = conn.execute("SELECT id FROM projects WHERE name='dry_proj'").fetchone()["id"]
    # collect_index 의 대상 URL 은 최신 스냅샷의 상위 페이지다 — 없으면 sys.exit 한다
    conn.execute("""INSERT INTO gsc_snapshots(project_id, snapshot_date, period_days,
                      query, page, clicks, impressions, ctr, position)
                    VALUES(?, '2026-08-18', 28, 'q', 'https://d.com/a', 1, 300, 0.01, 9.0)""",
                 (pid,))
    conn.commit()
    conn.close()

    def boom(*a, **k):
        raise AssertionError("dry-run 이 인증을 건드렸다")

    orig = collect_gsc.get_service, collect_index.get_service
    collect_gsc.get_service = boom
    collect_index.get_service = boom
    try:
        for mod in (collect_gsc, collect_index):
            mod.collect("dry_proj", dry_run=True)
    finally:
        collect_gsc.get_service, collect_index.get_service = orig

    conn = db.connect()
    assert conn.execute("SELECT COUNT(*) FROM runs WHERE project_id=?",
                        (pid,)).fetchone()[0] == 0, "dry-run 은 실행 기록을 남기지 않는다"
    assert conn.execute("SELECT COUNT(*) FROM gsc_index_status WHERE project_id=?",
                        (pid,)).fetchone()[0] == 0, "dry-run 이 색인 결과를 적재했다"
    conn.close()


def test_side_calls_cannot_kill_the_main_snapshot():
    """일별·분해 호출이 실패해도 이미 받아 둔 query×page 는 저장돼야 한다.

    수집이 query×page 한 방이던 시절엔 루프가 끝나면 곧바로 저장이라 이 구멍이
    없었다. 뒤에 부수 호출 둘이 붙으면서, 그중 하나만 터져도 본체 수확이 통째로
    날아가게 됐다 (리뷰에서 실측으로 잡은 회귀). 부수적인 것이 본체를 죽이면 안 된다.

    빈 값으로 대신 쓰는 것도 안 된다 — write_gsc_daily/breakdown 은 덮어쓰기라
    지난번 수집분까지 지운다. 실패한 축은 아예 건너뛰어야 한다.
    """
    import collect_gsc
    from googleapiclient.errors import HttpError

    conn = db.connect()
    conn.execute("INSERT OR IGNORE INTO projects(name, domain, locale, gsc_property) "
                 "VALUES('sidefail', 's.com', 'ko-KR', 'sc-domain:s.com')")
    conn.commit()
    pid = conn.execute("SELECT id FROM projects WHERE name='sidefail'").fetchone()["id"]
    # 지난번 수집분 — 실패한 축이 이걸 지우면 안 된다.
    conn.execute("INSERT INTO gsc_daily(project_id, date, clicks, impressions, ctr, position) "
                 "VALUES(?, '2026-08-01', 9, 90, 0.1, 5.0)", (pid,))
    conn.commit()
    conn.close()

    class _Resp:
        status = 500
        reason = "boom"

    class _Exec:
        def __init__(self, body):
            self.body = body

        def execute(self):
            dims = self.body.get("dimensions")
            if dims == ["query", "page"]:
                return {"rows": [{"keys": ["신발", "https://s.com/a"], "clicks": 3,
                                  "impressions": 100, "ctr": 0.03, "position": 8.0}]}
            raise HttpError(_Resp(), b"side call down")   # 일별·분해는 전부 실패

    class _SA:
        def query(self, siteUrl, body):
            return _Exec(body)

    class _Service:
        def searchanalytics(self):
            return _SA()

    orig = collect_gsc.get_service
    collect_gsc.get_service = lambda: _Service()
    try:
        collect_gsc.collect("sidefail", breakdown="device")
    finally:
        collect_gsc.get_service = orig

    conn = db.connect()
    assert conn.execute("SELECT COUNT(*) FROM gsc_snapshots WHERE project_id=?",
                        (pid,)).fetchone()[0] == 1, \
        "부수 호출이 실패했다고 본체 스냅샷까지 날아갔다"
    kept = conn.execute("SELECT date FROM gsc_daily WHERE project_id=?", (pid,)).fetchall()
    assert [r["date"] for r in kept] == ["2026-08-01"], \
        f"실패한 축이 지난번 수집분을 건드렸다: {[dict(r) for r in kept]}"
    assert conn.execute("SELECT COUNT(*) FROM gsc_breakdown WHERE project_id=?",
                        (pid,)).fetchone()[0] == 0, "실패한 분해가 행을 남겼다"
    notes = conn.execute("SELECT notes FROM runs WHERE project_id=? ORDER BY id DESC LIMIT 1",
                         (pid,)).fetchone()["notes"]
    assert "daily=skip" in notes and "device=skip" in notes, \
        f"건너뛴 축이 실행 기록에 안 남았다 — 나중에 '왜 비었나'를 답할 수 없다: {notes}"
    conn.close()


def test_doctor_json_subprocess():
    """doctor.py --json: 임시 CAPTURE_HOME 환경에서 실행 -> exit code 0,
    stdout이 JSON으로 정상 파싱되고 verdict, next_command 키가 존재하는지 assert."""
    doc_home = Path(tempfile.mkdtemp(prefix="seo-miner-doc-test-"))
    doctor_script = Path(__file__).resolve().parents[2] / "setup" / "scripts" / "doctor.py"
    assert doctor_script.exists(), f"doctor.py 스크립트 없음: {doctor_script}"

    # 토큰 자리도 빈 임시 폴더로 돌린다 — 이 컴퓨터의 진짜 로그인 토큰을 보면
    # "로그인 대기" 판정을 검증할 수 없다.
    env = {**os.environ, "CAPTURE_HOME": str(doc_home),
           "GSC_TOKEN_FILE": str(doc_home / "gsc_token.json"),
           "GSC_CONFIG_DIR": str(doc_home / "legacy-none")}
    env.pop("GOOGLE_APPLICATION_CREDENTIALS", None)
    env.pop("GSC_OAUTH_CLIENT_SECRETS_FILE", None)
    res = subprocess.run(
        [sys.executable, str(doctor_script), "--json"],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert res.returncode == 0, f"doctor.py 비정상 종료 (code {res.returncode}):\n{res.stderr}\n{res.stdout}"

    data = json.loads(res.stdout)
    assert "verdict" in data, f"'verdict' 키 누락: {data}"
    assert "next_command" in data, f"'next_command' 키 누락: {data}"
    assert data["core_ok"] is True, f"core_ok 가 True 여야 함: {data.get('core_ok')}"
    assert data["brain_ok"] is True, f"brain_ok 가 True 여야 함: {data.get('brain_ok')}"

    # 설치 직후 = 번들 클라이언트 + 토큰 없음 = **로그인 대기**. doctor 가 여기서
    # gsc_connected=True 라고 말하면, 한 번도 로그인 안 한 사람이 "연결됨"을 보고
    # 수집이 왜 안 되는지 영영 못 찾는다.
    assert data["gsc_mode"] == "oauth", f"번들이 있으면 oauth 여야 함: {data.get('gsc_mode')}"
    assert data["gsc_connected"] is False, "토큰이 없는데 연결됐다고 했다"
    assert data["gsc_bundled"] is True, f"번들로 로그인하는 중이어야 함: {data}"
    assert data["keys"]["gsc_service_account"] is False, \
        "대시보드가 gsc_ok 로 먹는 키가 로그인 전에 True 다 — 화면이 거짓말한다"


def test_doctor_detects_gsc_missing_scopes():
    """doctor.py: 연결된 OAuth 토큰에 analytics.readonly 가 없으면(GA4 추가 전에
    로그인해 둔 사람) 재로그인 안내가 must 에 뜨는지, 스코프가 다 있으면 안 뜨는지.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "setup" / "scripts"))
    import doctor

    doc_home = Path(tempfile.mkdtemp(prefix="seo-miner-doc-scope-test-"))
    token = doc_home / "gsc_token.json"
    env_keys = {"CAPTURE_HOME": str(doc_home), "GSC_TOKEN_FILE": str(token),
               "GSC_CONFIG_DIR": str(doc_home / "legacy-none")}
    saved = {k: os.environ.get(k) for k in env_keys}
    os.environ.update(env_keys)
    try:
        # analytics.readonly 없는 구버전 토큰 — GA4 스코프가 추가되기 전에 로그인한 사람.
        token.write_text(json.dumps({
            "token": "x", "refresh_token": "y",
            "scopes": ["https://www.googleapis.com/auth/webmasters.readonly"],
        }), encoding="utf-8")

        d = doctor.diagnose()
        assert d["gsc_connected"] is True, "토큰이 있는데 연결 안 됐다고 했다"
        assert d["gsc_missing_scopes"] == ["https://www.googleapis.com/auth/analytics.readonly"], \
            d["gsc_missing_scopes"]
        assert any(m["id"] == "gsc_rescope" for m in d["must"]), \
            f"스코프 부족인데 재로그인 안내가 없다: {d['must']}"
        rescope = next(m for m in d["must"] if m["id"] == "gsc_rescope")
        assert "GA4" in rescope["msg"], rescope["msg"]
        # 칩에는 파일 경로가 아니라 복구를 부르는 문구가 실린다 — 사용자가 경로를
        # 손으로 다룰 일이 없게. 문구의 정본은 doctor.RESCOPE_CMD 한 곳이다.
        assert rescope["cmd"] == doctor.RESCOPE_CMD, rescope
        assert str(doctor.db.gsc_token()) not in str(rescope), rescope

        # 스코프를 다 갖춘 토큰이면 안내가 사라져야 한다 — 일부러 깨 보는 대조군.
        token.write_text(json.dumps({
            "token": "x", "refresh_token": "y",
            "scopes": list(doctor.collect_gsc.SCOPES),
        }), encoding="utf-8")
        d2 = doctor.diagnose()
        assert d2["gsc_missing_scopes"] == [], d2["gsc_missing_scopes"]
        assert not any(m["id"] == "gsc_rescope" for m in d2["must"]), d2["must"]
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(doc_home, ignore_errors=True)


def test_doctor_detects_marketing_skills():
    """doctor.py: CLAUDE_SKILLS_DIR 환경변수 기반 마케팅 스킬 탐지 검증.

    ai-seo 만 설치된 상태에서 ai-seo=True, 나머지=False, must 에 누락 스킬 및
    저장소 링크가 포함되는지 검증하고, 필수 7개(MARKETING_SKILLS) 모두 설치 시 must 에서
    빠지는지 확인한다. 명부 전체(ALL_SKILLS)는 여기에 선택 aso 를 더한 8개다.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "setup" / "scripts"))
    import doctor

    skills_dir = Path(tempfile.mkdtemp(prefix="seo-miner-skills-test-"))
    orig_skills_dir = os.environ.get("CLAUDE_SKILLS_DIR")
    os.environ["CLAUDE_SKILLS_DIR"] = str(skills_dir)
    try:
        # 1. ai-seo 만 설치된 상태
        (skills_dir / "ai-seo").mkdir(parents=True, exist_ok=True)
        (skills_dir / "ai-seo" / "SKILL.md").write_text("# ai-seo\n", encoding="utf-8")

        res = doctor.diagnose()
        assert res["marketing_skills"]["ai-seo"] is True, "ai-seo 는 True 여야 함"
        for k in doctor.MARKETING_SKILLS:
            if k != "ai-seo":
                assert res["marketing_skills"][k] is False, f"{k} 는 False 여야 함"
        assert res["marketing_optional"]["aso"] is False, "aso 는 False 여야 함"

        # 누락 스킬 안내는 later([나중에])에 있어야 한다 — must 가 아니다.
        # must 로 두면 첫 세션이 Claude Code 재시작으로 끊기는데, 이 팩은 측정·수집에
        # 필요가 없다(메시지 자신이 그렇게 말한다). 권하는 자리는 첫 리포트 뒤다.
        assert any("marketingskills" in s and "product-marketing" in s for s in res["later"]), \
            f"later 에 누락 스킬 설치 안내가 있어야 함: {res['later']}"
        assert not any("marketingskills" in m["msg"] for m in res["must"]), \
            f"마케팅 스킬은 must 에 오면 안 됨: {res['must']}"

        # 2. 필수 7개 전부 설치된 상태 (선택 aso 는 일부러 빼 둔다 — 명부는 8개)
        for k in doctor.MARKETING_SKILLS:
            (skills_dir / k).mkdir(parents=True, exist_ok=True)
            (skills_dir / k / "SKILL.md").write_text(f"# {k}\n", encoding="utf-8")

        res_all = doctor.diagnose()
        for k in doctor.MARKETING_SKILLS:
            assert res_all["marketing_skills"][k] is True, f"{k} 가 True 여야 함"

        # 필수 7개 전부 설치되면 must 에 해당 요구 문장이 없어야 함 (aso 는 [선택]이라 무관)
        assert not any("marketingskills" in m["msg"] for m in res_all["must"]), \
            f"필수 7개 완비 시 must 에 마케팅 스킬 요구가 없어야 함: {res_all['must']}"
        # aso 만 빠졌으므로 later 에 aso 안내가 한 줄 있어야 함
        assert any("aso" in s and "marketingskills" in s for s in res_all["later"]), \
            f"aso 누락 시 later 에 안내가 있어야 함: {res_all['later']}"
    finally:
        if orig_skills_dir is not None:
            os.environ["CLAUDE_SKILLS_DIR"] = orig_skills_dir
        else:
            os.environ.pop("CLAUDE_SKILLS_DIR", None)
        shutil.rmtree(skills_dir, ignore_errors=True)


def test_install_skills_never_runs_when_nothing_is_missing():
    """install_skills.py: 스킬이 완비된 경우 claude CLI를 절대 실행하지 않고,
    누락 스킬이 발생했을 때만 marketplace add / plugin install을 순차 실행하는지 검증.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "setup" / "scripts"))
    import doctor
    import install_skills

    skills_dir = Path(tempfile.mkdtemp(prefix="seo-miner-install-skills-test-"))
    orig_skills_dir = os.environ.get("CLAUDE_SKILLS_DIR")
    os.environ["CLAUDE_SKILLS_DIR"] = str(skills_dir)

    run_calls = []
    orig_run = subprocess.run
    orig_which = shutil.which

    class _FakeCompletedProcess:
        def __init__(self, args, returncode=0):
            self.args = args
            self.returncode = returncode
            self.stdout = ""
            self.stderr = ""

    def fake_run(args, *a, **k):
        run_calls.append((args, k))
        return _FakeCompletedProcess(args, returncode=0)

    try:
        # 1. 필수·선택 스킬 전부 생성 (빠진 스킬 없음)
        for k in [*doctor.MARKETING_SKILLS, *doctor.OPTIONAL_SKILLS]:
            (skills_dir / k).mkdir(parents=True, exist_ok=True)
            (skills_dir / k / "SKILL.md").write_text(f"# {k}\n", encoding="utf-8")

        subprocess.run = fake_run
        install_skills.subprocess.run = fake_run
        shutil.which = lambda cmd: "/mock/bin/claude" if cmd == "claude" else orig_which(cmd)
        install_skills.shutil.which = shutil.which

        # 빠진 게 없으면 subprocess.run이 절대 불리지 않아야 함
        run_calls.clear()
        code = install_skills.install_skills()
        assert code == 0, f"빠진 것 없을 때 0 반환: {code}"
        assert len(run_calls) == 0, f"빠진 스킬이 없는데 claude를 호출함: {run_calls}"

        # 2. 필수 스킬 중 하나(예: ai-seo)를 삭제하여 빠진 스킬 생성
        ai_seo = skills_dir / "ai-seo" / "SKILL.md"
        if ai_seo.exists():
            ai_seo.unlink()

        run_calls.clear()
        code_missing = install_skills.install_skills()
        assert code_missing == 0, f"모킹 성공 시 0 반환: {code_missing}"
        assert len(run_calls) == 2, f"빠진 스킬 존재 시 add, install 2회 호출되어야 함: {run_calls}"

        # 호출 인자 검증
        cmd1 = run_calls[0][0]
        assert any("marketplace" in str(arg) for arg in cmd1) and any("add" in str(arg) for arg in cmd1), \
            f"첫 번째 명령은 marketplace add 여야 함: {cmd1}"
        assert run_calls[0][1].get("timeout") == 300, "첫 번째 명령에 timeout 300초 지정 필요"

        cmd2 = run_calls[1][0]
        assert any("plugin" in str(arg) for arg in cmd2) and any("install" in str(arg) for arg in cmd2), \
            f"두 번째 명령은 plugin install 이어야 함: {cmd2}"
        assert run_calls[1][1].get("timeout") == 300, "두 번째 명령에 timeout 300초 지정 필요"

    finally:
        subprocess.run = orig_run
        install_skills.subprocess.run = orig_run
        shutil.which = orig_which
        install_skills.shutil.which = orig_which
        if orig_skills_dir is not None:
            os.environ["CLAUDE_SKILLS_DIR"] = orig_skills_dir
        else:
            os.environ.pop("CLAUDE_SKILLS_DIR", None)
        shutil.rmtree(skills_dir, ignore_errors=True)


def test_gsc_auth_precedence():
    """db.gsc_auth(): OAuth가 기본, 서비스 계정은 무인 수집용 대안.

    collect_gsc·gsc_query·doctor가 전부 이 판정 하나를 따르므로,
    순서가 뒤집히면 세 군데가 동시에 틀린다. 여기서 못 박는다.
    """
    # **경로를 db.gsc_oauth_client() 로 받으면 안 된다.** 그건 이제 번들까지 훑는
    # 해석기라, 번들이 배포에 들어가는 순간 이 테스트가 리포 안쪽 파일을 지운다.
    # 여기서 다루는 건 "사용자가 직접 놓은 자리" 하나다.
    oauth, key = db.CAPTURE_HOME / "gsc_oauth_client.json", db.gsc_key()
    oauth.parent.mkdir(parents=True, exist_ok=True)
    bundled = HOME / "bundled_oauth.json"       # 번들 자리는 가짜로 세운다
    real_bundled = db.gsc_oauth_bundled
    db.gsc_oauth_bundled = lambda: bundled
    for f in (oauth, key, bundled):
        f.unlink(missing_ok=True)
    try:
        assert db.gsc_auth() == "", "아무것도 없으면 빈 문자열"
        key.write_text("{}", encoding="utf-8")
        assert db.gsc_auth() == "service_account", "키만 있으면 서비스 계정"
        oauth.write_text("{}", encoding="utf-8")
        assert db.gsc_auth() == "oauth", "둘 다 있으면 OAuth 가 이긴다 (기본)"
        key.unlink()
        assert db.gsc_auth() == "oauth"

        # 번들 OAuth 는 **맨 뒤**다. 설치만 하면 항상 존재하므로 앞세우면,
        # 서비스 계정으로 무인 수집을 걸어 둔 사람이 매번 브라우저 로그인으로
        # 끌려간다 — 업데이트가 남의 발밑을 바꾸는 종류의 사고다.
        oauth.unlink()
        bundled.write_text("{}", encoding="utf-8")
        assert db.gsc_auth() == "oauth", "아무것도 없으면 번들이 기본이 된다"
        assert db.gsc_oauth_client() == bundled, "번들을 가리켜야 한다"
        key.write_text("{}", encoding="utf-8")
        assert db.gsc_auth() == "service_account", \
            "번들이 서비스 계정을 이기면 안 된다 — 무인 수집이 브라우저 로그인으로 끌려간다"
        oauth.write_text("{}", encoding="utf-8")
        assert db.gsc_oauth_client() == oauth, \
            "사용자가 직접 놓은 것이 번들을 이겨야 한다"
    finally:
        db.gsc_oauth_bundled = real_bundled
        for f in (oauth, key, bundled):
            f.unlink(missing_ok=True)


def test_bundled_oauth_client_ships_with_the_plugin():
    """번들 클라이언트가 배포에 실제로 들어 있는가.

    이 파일이 빠진 채 배포되면 gsc_auth()가 "" 로 떨어져 **전 사용자**가
    "인증 없음"을 본다 — 콘솔 작업 0 이라는 이번 변경의 전제가 통째로 사라진다.
    파일 존재만으로는 부족하다: 형태가 데스크톱 앱 OAuth 클라이언트여야
    connect_gsc.assemble()이 본을 뜰 수 있고 서버가 로그인을 열 수 있다.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "setup" / "scripts"))
    import connect_gsc

    b = db.gsc_oauth_bundled()
    assert b.exists(), f"번들 OAuth 클라이언트가 배포에 없다: {b}"
    assert connect_gsc.kind(b) == "oauth", f"번들이 OAuth 클라이언트 형태가 아니다: {b}"
    inst = json.loads(b.read_text(encoding="utf-8"))["installed"]
    for k in ("client_id", "client_secret", "auth_uri", "token_uri"):
        assert inst.get(k), f"번들에 {k} 가 없다 — 로그인이 성립하지 않는다"


def test_gsc_connected_is_decided_by_the_token_not_the_file():
    """**이번 변경의 핵심 회귀.** 번들은 설치만 하면 항상 있다 — 그걸로 판정하면
    한 번도 로그인 안 한 사람 전원이 '연결됨'으로 보인다(= doctor 가 거짓말).

    3-상태가 여기서 갈린다:
      · 인증 없음   gsc_auth()==""
      · 로그인 대기 gsc_auth()!="" and not gsc_connected()   ← 새로 생긴 상태
      · 연결됨      gsc_connected()
    서비스 계정만 예외다 — 로그인 자체가 없어 키 파일이 곧 연결이다.
    """
    oauth, key = db.CAPTURE_HOME / "gsc_oauth_client.json", db.gsc_key()
    oauth.parent.mkdir(parents=True, exist_ok=True)
    bundled = HOME / "conn_bundled.json"
    real_bundled = db.gsc_oauth_bundled
    db.gsc_oauth_bundled = lambda: bundled
    # 토큰 자리를 임시 폴더로 돌린다 — 이 컴퓨터에 진짜 로그인 토큰이 있으면
    # 테스트가 그걸 보고 통과해 버린다(정확히 우리가 막으려는 거짓 양성).
    cfg = HOME / "token-conf"
    orig_cfg = (os.environ.get("GSC_TOKEN_FILE"), os.environ.get("GSC_CONFIG_DIR"))
    cfg.mkdir(parents=True, exist_ok=True)
    os.environ["GSC_TOKEN_FILE"] = str(cfg / "gsc_token.json")
    # 레거시(예전 MCP 서버) 자리도 빈 폴더로 돌린다 — 이 컴퓨터에 그 시절 토큰이
    # 남아 있으면 gsc_connected 가 그걸 보고 통과해 버린다.
    os.environ["GSC_CONFIG_DIR"] = str(cfg / "legacy")
    token = db.gsc_token()
    for f in (oauth, key, bundled, token):
        f.unlink(missing_ok=True)
    try:
        assert db.gsc_auth() == "" and db.gsc_connected() is False, "인증 없음"

        # 번들만 있고 토큰이 없다 = 로그인 대기. 여기서 True 가 나오면 doctor 가
        # "연결됨"이라 말하고 사용자는 로그인 없이 수집을 기다린다.
        bundled.write_text("{}", encoding="utf-8")
        assert db.gsc_auth() == "oauth"
        assert db.gsc_connected() is False, \
            "번들 파일이 있다고 연결됐다고 답했다 — 로그인 대기를 연결됨으로 오인한다"

        token.write_text("{}", encoding="utf-8")
        assert db.gsc_connected() is True, "토큰이 생기면 연결됨"

        # 사용자가 직접 깐 클라이언트도 같은 규칙이다 (파일 유무가 아니다).
        oauth.write_text("{}", encoding="utf-8")
        token.unlink()
        assert db.gsc_auth() == "oauth" and db.gsc_connected() is False

        # 서비스 계정은 로그인이 없다 — 토큰이 없어도 연결됨이어야 한다.
        oauth.unlink()
        bundled.unlink()
        key.write_text("{}", encoding="utf-8")
        assert db.gsc_auth() == "service_account"
        assert db.gsc_connected() is True, \
            "서비스 계정에 로그인 토큰을 요구하면 무인 수집이 영영 '로그인 대기'가 된다"
    finally:
        db.gsc_oauth_bundled = real_bundled
        for var, val in zip(("GSC_TOKEN_FILE", "GSC_CONFIG_DIR"), orig_cfg):
            if val is None:
                os.environ.pop(var, None)
            else:
                os.environ[var] = val
        for f in (oauth, key, bundled, token):
            f.unlink(missing_ok=True)


def test_gsc_token_lives_with_the_rest_of_the_state():
    """토큰은 CAPTURE_HOME 에 산다 — 그리고 예전 MCP 자리를 승계한다.

    승계가 끊기면 이미 로그인한 사람이 아무 이유 없이 다시 로그인하게 되고,
    doctor 는 그동안 "로그인 대기"라고 말한다. 그래서 여기서 못 박는다.
    """
    orig = (os.environ.get("GSC_TOKEN_FILE"), os.environ.get("GSC_CONFIG_DIR"))
    new, legacy_dir = HOME / "tok" / "gsc_token.json", HOME / "legacydir"
    try:
        os.environ["GSC_TOKEN_FILE"] = str(new)
        os.environ["GSC_CONFIG_DIR"] = str(legacy_dir)
        assert db.gsc_token() == new, db.gsc_token()
        assert db.gsc_token_legacy() == legacy_dir / "token.json", db.gsc_token_legacy()
        assert db.gsc_token_file() is None, "아무 토큰도 없는데 있다고 했다"

        # 레거시만 있으면 그걸 쓴다 — 다시 로그인시키지 않는다.
        legacy_dir.mkdir(parents=True, exist_ok=True)
        (legacy_dir / "token.json").write_text("{}", encoding="utf-8")
        assert db.gsc_token_file() == legacy_dir / "token.json"

        # 새 자리가 생기면 그쪽이 이긴다 (승계 후 다시 쓴 결과가 정본).
        new.parent.mkdir(parents=True, exist_ok=True)
        new.write_text("{}", encoding="utf-8")
        assert db.gsc_token_file() == new

        # 환경변수가 없을 때의 기본 자리도 CAPTURE_HOME 안이어야 한다.
        os.environ.pop("GSC_TOKEN_FILE")
        t = db.gsc_token()
        assert t == db.CAPTURE_HOME / "gsc_token.json" and t.is_absolute(), t
    finally:
        for var, val in zip(("GSC_TOKEN_FILE", "GSC_CONFIG_DIR"), orig):
            if val is None:
                os.environ.pop(var, None)
            else:
                os.environ[var] = val
        shutil.rmtree(legacy_dir, ignore_errors=True)
        shutil.rmtree(new.parent, ignore_errors=True)


def test_connect_gsc_assembles_client_and_never_prints_the_secret():
    """--client-id/--client-secret 로 조립 — JSON 다운로드 없이 자기 클라이언트를 쓰는 길.

    서브프로세스로 도는 이유는 stdout/stderr 을 통째로 볼 수 있어야 하기 때문이다.
    시크릿이 화면에 찍히면 그대로 로그·스크린샷·이슈로 새 나간다.
    설치 자리는 반드시 CAPTURE_HOME 이다 — 플러그인 안쪽(번들 자리)에 깔면
    다음 업데이트에 날아가고 번들까지 덮어쓴다.
    """
    home = Path(tempfile.mkdtemp(prefix="seo-miner-assemble-"))
    script = Path(__file__).resolve().parents[2] / "setup" / "scripts" / "connect_gsc.py"
    secret = "GOCSPX-테스트시크릿절대출력금지"
    env = {**os.environ, "CAPTURE_HOME": str(home)}
    env.pop("GSC_OAUTH_CLIENT_SECRETS_FILE", None)   # 환경변수가 자리를 가로채면 안 된다
    res = subprocess.run(
        [sys.executable, str(script), "--client-id", "123-abc.apps.googleusercontent.com",
         "--client-secret", secret],
        env=env, capture_output=True, text=True, encoding="utf-8")
    assert res.returncode == 0, f"조립 실패:\n{res.stdout}\n{res.stderr}"
    assert secret not in res.stdout and secret not in res.stderr, \
        "client_secret 이 화면에 새어 나왔다"

    dest = home / "gsc_oauth_client.json"
    assert dest.exists(), f"사용자 자리에 설치되지 않았다: {dest}"
    assert not (script.parent.parent / "oauth_client.json").read_text(
        encoding="utf-8").count(secret), "번들 파일을 덮어썼다"
    d = json.loads(dest.read_text(encoding="utf-8"))
    inst = d["installed"]                      # 데스크톱 앱 형태여야 한다
    assert inst["client_id"] == "123-abc.apps.googleusercontent.com"
    assert inst["client_secret"] == secret
    for k in ("auth_uri", "token_uri", "redirect_uris"):
        assert inst.get(k), f"{k} 가 없다 — 로그인 흐름이 성립하지 않는다: {inst}"

    # 두 번째 실행은 기존 파일을 말없이 덮지 않는다 (--force 가 있어야 한다).
    res2 = subprocess.run(
        [sys.executable, str(script), "--client-id", "x", "--client-secret", "y"],
        env=env, capture_output=True, text=True, encoding="utf-8")
    assert res2.returncode != 0 and "--force" in (res2.stdout + res2.stderr), \
        f"이미 있는 클라이언트를 말없이 덮어썼다:\n{res2.stdout}\n{res2.stderr}"
    shutil.rmtree(home, ignore_errors=True)


def test_collect_gsc_auth_routes_by_mode():
    """collect_gsc: OAuth면 브라우저 로그인 경로, 서비스 계정이면 직결, 없으면 안내.
    collect_ga4 도 같은 판정(get_credentials) 위에 서는지 함께 본다 — 열쇠는 한 벌
    이라 private 속성(service._http.credentials)을 빌리지 않는다.

    로그인은 이제 우리 것이다(예전엔 MCP 서버가 대신했다). 갈래가 실제로 갈라지는지
    본다 (네트워크는 타지 않는다 — Credentials 는 가짜, build() 는 static discovery
    라 네트워크를 안 탄다).
    """
    import collect_ga4
    import collect_gsc
    from google.oauth2.credentials import Credentials

    # 사용자 자리를 직접 가리킨다 — db.gsc_oauth_client() 는 번들까지 훑는 해석기라
    # 여기서 쓰면 배포에 들어간 번들 파일을 테스트가 지운다.
    oauth, key = db.CAPTURE_HOME / "gsc_oauth_client.json", db.gsc_key()
    oauth.parent.mkdir(parents=True, exist_ok=True)
    for f in (oauth, key):
        f.unlink(missing_ok=True)
    # 번들도 가짜로 세운다. 번들이 배포에 들어간 뒤로 "파일을 지우면 인증이 없다"가
    # 성립하지 않는다 — 실제로 ③이 여기서 깨졌다(번들 때문에 oauth 로 판정됐다).
    bundled = HOME / "routes_bundled.json"
    real_bundled = db.gsc_oauth_bundled
    db.gsc_oauth_bundled = lambda: bundled
    bundled.unlink(missing_ok=True)
    called = []
    real_oauth = collect_gsc._oauth_credentials
    fake_creds = Credentials(token="fake")
    collect_gsc._oauth_credentials = lambda: (called.append("oauth"), fake_creds)[1]
    try:
        # ① OAuth 클라이언트가 있으면 로그인 경로로 간다.
        oauth.write_text(json.dumps({"installed": {"client_id": "x"}}), encoding="utf-8")
        assert collect_gsc.get_credentials() is fake_creds, "OAuth면 로그인 경로여야 한다"
        assert called == ["oauth"]

        # get_service(gsc) 는 get_credentials() 위에 선다 — 같은 Credentials 로 짓는다.
        called.clear()
        gsc_svc = collect_gsc.get_service()
        assert called == ["oauth"], "get_service 가 get_credentials 를 거치지 않는다"
        assert hasattr(gsc_svc, "searchanalytics"), "서치콘솔 서비스가 아니다"

        # collect_ga4 는 그 함수를 직접 빌려 쓴다 — private 속성 우회가 없어야 한다.
        called.clear()
        data_svc, admin_svc = collect_ga4.get_service()
        assert called == ["oauth"], "collect_ga4 가 collect_gsc 의 인증을 빌리지 않는다"
        assert hasattr(data_svc, "properties"), "GA4 Data 서비스가 아니다"
        assert hasattr(admin_svc, "accountSummaries"), "GA4 Admin 서비스가 아니다"

        # ② 서비스 계정만 있으면 직결한다 (가짜 키라 파싱에서 터지는 건 정상 —
        #    여기서 보는 건 "로그인 경로로 새지 않는다" 하나다).
        oauth.unlink()
        called.clear()
        key.write_text(json.dumps({"type": "service_account",
                                   "client_email": "x@y.iam.gserviceaccount.com"}),
                       encoding="utf-8")
        try:
            assert collect_gsc.get_credentials() is not fake_creds
        except SystemExit:
            raise
        except Exception:
            pass
        assert not called, "서비스 계정이 걸려 있으면 브라우저 로그인을 부르지 않아야 한다"

        # ③ 번들까지 하나도 없으면 두 경로를 다 알려주고 멈춘다.
        key.unlink()
        try:
            collect_gsc.get_credentials()
            raise AssertionError("인증이 하나도 없는데 그냥 진행했다")
        except SystemExit as e:
            msg = str(e)
            assert "connect_gsc.py" in msg and "OAuth" in msg, \
                f"안내가 두 경로를 다 말하지 않는다: {msg}"
    finally:
        collect_gsc._oauth_credentials = real_oauth
        db.gsc_oauth_bundled = real_bundled
        for f in (oauth, key, bundled):
            f.unlink(missing_ok=True)


def test_oauth_credentials_skips_browser_when_none_available():
    """_oauth_credentials(): 브라우저가 없으면(_can_open_browser()==False) 그
    조건만 보고 InstalledAppFlow 를 아예 안 부른다 — 실제로 호스팅에서 이게
    webbrowser.Error 로 500 을 냈다(Railway 로그, 2026-08-31: 토큰 없는 사이트에서
    /api/ga4/properties 를 부르니 서버가 브라우저를 열려다 죽었다). 브라우저가
    있으면(CLI) 로컬 로그인 흐름은 그대로 열려야 한다 — 이것도 여기서 같이 본다.
    """
    import collect_gsc
    from google_auth_oauthlib.flow import InstalledAppFlow

    oauth = db.CAPTURE_HOME / "gsc_oauth_client.json"
    oauth.parent.mkdir(parents=True, exist_ok=True)
    oauth.write_text(json.dumps({"installed": {"client_id": "x"}}), encoding="utf-8")
    token = db.gsc_token()
    token.unlink(missing_ok=True)              # 토큰이 없어야 로그인 경로로 떨어진다

    class _FakeCreds:
        def to_json(self):
            return '{"token":"fake"}'

    class _FakeFlow:
        def run_local_server(self, port=0):
            return _FakeCreds()

    real_from_secrets = InstalledAppFlow.from_client_secrets_file
    real_can_open = collect_gsc._can_open_browser
    try:
        # 브라우저 없음 — 로그인 흐름을 부르면 그 자체가 실패다.
        def _must_not_call(*a, **kw):
            raise AssertionError("브라우저가 없는데 InstalledAppFlow 를 불렀다")
        InstalledAppFlow.from_client_secrets_file = classmethod(_must_not_call)
        collect_gsc._can_open_browser = lambda: False
        assert collect_gsc._oauth_credentials() is None, "브라우저 없이도 로그인을 시도했다"

        # 브라우저 있음(CLI) — 로컬 로그인 흐름은 그대로 살아 있어야 한다.
        called = []
        InstalledAppFlow.from_client_secrets_file = classmethod(
            lambda cls, *a, **kw: (called.append(1), _FakeFlow())[1])
        collect_gsc._can_open_browser = lambda: True
        creds = collect_gsc._oauth_credentials()
        assert called, "브라우저가 있는데도 CLI 로그인 흐름을 안 불렀다 — 로컬 로그인이 깨졌다"
        assert creds is not None and token.exists(), "로그인 뒤 토큰을 저장하지 않았다"
    finally:
        InstalledAppFlow.from_client_secrets_file = real_from_secrets
        collect_gsc._can_open_browser = real_can_open
        oauth.unlink(missing_ok=True)
        token.unlink(missing_ok=True)


def test_gsc_query_selfcheck():
    """gsc_query: 창 계산·필터 파싱·노출 가중평균 — 즉석 조회가 MCP 서버를 대신한다.

    네트워크는 타지 않는다. 여기서 지키는 건 두 가지다: 콜론이 든 필터 값(URL)이
    잘리지 않는 것, 그리고 합계의 position 이 단순평균이 아니라 노출 가중평균인 것.
    """
    import gsc_query
    gsc_query._selfcheck()

    # 즉석 조회의 기본 상태는 all 이어야 한다 — final 이면 수집기와 같은 숫자가 되고
    # "지금 이 순간"을 묻는 자리가 사라진다.
    ap_help = subprocess.run(
        [sys.executable, str(Path(__file__).parent / "gsc_query.py"), "search", "--help"],
        capture_output=True, text=True, encoding="utf-8")
    assert "기본 all" in ap_help.stdout, ap_help.stdout


def test_run_all_chain_order_and_paid_skips():
    """run_all: 체인 순서, 유료 키 미보유 시 건너뜀, gsc 실패 시 중단, skip/only,
    dry-run 전달, 스테이지별 옵션 전달, 그리고 결과 리스트에 실려 오는 행 수·비용.

    run_all.STAGES 를 mutate 하지 않는다 — run_chain(stages=...) 로 가짜 표를 주입한다.
    표의 디스패치가 fn 한 종류로 균질해져서 가짜도 한 종류면 된다.
    run_chain 은 [(단계 이름, StageResult)] 를 돌려주고, 판정은 chain_rc 가 한다.
    """
    import collector as _collector
    import run_all

    calls: list[str] = []
    seen: dict[str, dict] = {}

    def fake(name, result=None):
        def fn(project, *, dry_run=False, **opts):
            calls.append(name)
            seen[name] = {"dry_run": dry_run, **opts}
            return result or _collector.StageResult(ok=True)
        return fn

    def table(results=None):
        results = results or {}
        return tuple(s._replace(fn=fake(s.name, results.get(s.name))) for s in run_all.STAGES)

    def run(**kw):
        """체인 한 번 + 요약 출력 한 번. (결과 리스트, 찍힌 글) 을 돌려준다."""
        calls.clear()
        seen.clear()
        stages = kw.pop("stages", None) or table()
        # 카나리아는 네트워크를 탄다 — 여기서는 아무것도 안 막는 가짜를 준다.
        kw.setdefault("preflight", lambda stgs: {})
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            results = run_all.run_chain("test_proj", stages=stages, **kw)
            run_all.print_summary("test_proj", results, dry_run=kw.get("dry_run", False))
        return results, buf.getvalue()

    orig_env = {k: os.environ.get(k) for k in
                ("SERPER_API_KEY", "DATAFORSEO_LOGIN", "DATAFORSEO_PASSWORD", "OPENROUTER_API_KEY")}
    try:
        for k in orig_env:
            os.environ.pop(k, None)

        # 1. 유료 키 없음 — rank/ai/competitors 는 건너뜀, 나머지는 순서대로
        results, out = run()
        assert calls == ["gsc", "ga4", "index", "keywords", "crawl", "gaps", "pages", "report"], calls
        assert run_all.chain_rc(results) == 0, results
        assert [n for n, _ in results] == list(run_all.VALID_STAGE_NAMES), results
        for name in ("rank", "ai", "competitors"):
            r = dict(results)[name]
            assert r.ok and r.skipped, f"{name} 은 유료 키 없으면 건너뜀이어야 한다: {r}"
        # 첫 바퀴가 리포트 경로에서 끊기지 않게 — 고치러 가는 길과 다음 바퀴 시점.
        assert "다음 바퀴" in out, out

        # 2. 유료 키를 넣으면 전 단계가 돈다 (competitors 는 DataForSEO Labs)
        os.environ["SERPER_API_KEY"] = "mock_serper_key"
        os.environ["OPENROUTER_API_KEY"] = "mock_openrouter_key"
        os.environ["DATAFORSEO_LOGIN"] = "mock_login"
        os.environ["DATAFORSEO_PASSWORD"] = "mock_pw"
        results, _ = run()
        assert calls == list(run_all.VALID_STAGE_NAMES), calls
        assert run_all.chain_rc(results) == 0, results

        # DataForSEO 키 조합만으로도 rank 가 활성화된다
        os.environ.pop("SERPER_API_KEY", None)
        results, _ = run()
        assert "rank" in calls and "competitors" in calls, calls

        # 3. 행 수·비용이 결과에 그대로 실려 오고 합계가 나온다 (exit code 로 접히지 않는다)
        results, out = run(stages=table({
            "rank": _collector.StageResult(ok=True, rows=120, cost=0.03),
            "ai": _collector.StageResult(ok=True, rows=8, cost=0.12),
        }))
        assert run_all.chain_cost(results) == 0.15, results
        assert "$0.1500" in out and "120행" in out, out

        # 4. gsc 실패 — 뒤 단계가 하나도 안 불리고, 사유가 요약표까지 그대로 올라온다
        for k in orig_env:
            os.environ.pop(k, None)
        gsc_reason = "구글 서치콘솔 인증이 없습니다 — 로그인 한 번이면 끝납니다."
        results, out = run(stages=table({
            "gsc": _collector.StageResult(ok=False, skipped=True, reason=gsc_reason)}))
        assert calls == ["gsc"], f"gsc 실패 시 후속 단계가 호출되지 않아야 함: {calls}"
        assert run_all.chain_rc(results) == 1, results
        assert gsc_reason in out, out
        assert run_all.ABORT_REASON in out, f"뒷 단계가 '미실행' 으로 표시돼야 함: {out}"
        # 여기서 끝나면 사용자는 빈손이다 — 없는 리포트 경로 대신 복구 안내를 준다.
        assert "리포트 파일" not in out, out
        assert "/capture keywords test_proj" in out, out

        # 5. gsc 외 단계가 실패해도 뒤는 계속 간다
        results, out = run(only="gsc,index,keywords,report",
                           stages=table({"index": _collector.StageResult(ok=False, reason="index 실패")}))
        assert calls == ["gsc", "index", "keywords", "report"], calls
        assert run_all.chain_rc(results) == 1, results

        # 6. dry-run 이 각 단계까지 내려간다
        run(dry_run=True)
        assert seen and all(v["dry_run"] is True for v in seen.values()), seen

        # 7. skip / only
        run(skip="index,ai")
        assert calls == ["gsc", "ga4", "keywords", "crawl", "gaps", "pages", "report"], calls
        run(only="gsc,gaps")
        assert calls == ["gsc", "gaps"], calls

        # 8. 스테이지별 옵션 — `--only rank --opt rank.device=mobile` 이 도달한다
        os.environ["SERPER_API_KEY"] = "mock_serper_key"
        run(only="rank", opts={"rank": {"device": "mobile", "serp_depth": 20}})
        assert calls == ["rank"], calls
        assert seen["rank"]["device"] == "mobile" and seen["rank"]["serp_depth"] == 20, seen

        # 9. 잘못된 입력은 ValueError — main() 이 받아 종료코드 1 로 바꾼다
        for kw in ({"skip": "index", "only": "gsc"}, {"skip": "invalid_stage"},
                   {"only": "invalid_stage"}, {"opts": {"nope": {"a": 1}}}):
            try:
                run_all.run_chain("test_proj", stages=table(), **kw)
            except ValueError:
                continue
            assert False, f"ValueError 가 나야 함: {kw}"

        # 10. --opt 문자열 파싱 (STAGE.KEY=VALUE, 타입 추정 포함)
        assert run_all.parse_opts(
            ["rank.device=mobile", "gsc.row-limit=50000", "rank.force=true"]) == {
            "rank": {"device": "mobile", "force": True},
            "gsc": {"row_limit": 50000},
        }
        for bad in ("device=mobile", "rank.device", "rank.=x"):
            try:
                run_all.parse_opts([bad])
            except ValueError:
                continue
            assert False, f"ValueError 가 나야 함: {bad}"
    finally:
        for k, v in orig_env.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v


def test_paid_failures_are_failures_and_the_canary_runs_first():
    """유료 단계가 통째로 실패했는데 rc=0 "완료"로 나가던 자리를 못 박는다.

    실측(8/30·9/1·9/2 자동 런): rank 는 402 를 키워드 100개에 100번 맞고
    errors=100/api_calls=0 인 채로 "완료", metrics 는 updated=0 인데 "볼륨이
    채워졌으니…". 셋 다 초록불이었다.

    여기서 지키는 것:
      1. 잔액 < $1 이면 유료 단계를 **부르지도 않는다** (402 를 100번 사지 않는다)
      2. Fatal 이 올라오면 그 단계는 ok=False 이고 사유가 그 사람 말 그대로다
      3. 전부 실패한 단계는 chain_rc 가 1 이다 / 일부 실패는 0 이되 요약표에 표시
    """
    import collector as _collector
    import run_all

    calls: list[str] = []

    def fake(name, result=None, raises=None):
        def fn(project, *, dry_run=False, **opts):
            calls.append(name)
            if raises:
                raise raises
            return result or _collector.StageResult(ok=True)
        return fn

    def table(results=None, raises=None):
        results, raises = results or {}, raises or {}
        return tuple(s._replace(fn=fake(s.name, results.get(s.name), raises.get(s.name)))
                     for s in run_all.STAGES)

    def run(**kw):
        calls.clear()
        buf = io.StringIO()
        stages = kw.pop("stages", None) or table()
        with contextlib.redirect_stdout(buf):
            results = run_all.run_chain("test_proj", stages=stages, **kw)
            run_all.print_summary("test_proj", results, dry_run=False)
        return results, buf.getvalue()

    orig = {k: os.environ.get(k) for k in
            ("SERPER_API_KEY", "DATAFORSEO_LOGIN", "DATAFORSEO_PASSWORD", "OPENROUTER_API_KEY")}
    try:
        for k in orig:
            os.environ.pop(k, None)
        os.environ["DATAFORSEO_LOGIN"] = "u"
        os.environ["DATAFORSEO_PASSWORD"] = "p"

        # 1. 잔액이 없으면 DataForSEO 단계를 아예 안 부른다.
        broke = {n: "DataForSEO 잔액 없음 ($0.12)" for n in run_all.DFS_STAGES}
        results, out = run(preflight=lambda stgs: broke)
        for name in run_all.DFS_STAGES:
            assert name not in calls, f"잔액 0 인데 {name} 을 불렀다: {calls}"
            r = dict(results)[name]
            assert (r.ok, r.skipped) == (True, True) and "$0.12" in r.reason, r
        assert run_all.chain_rc(results) == 0, results
        assert "$0.12" in out, out

        # 2. Fatal 은 사유가 사람 말 그대로 올라오고 rc 가 1 이 된다.
        msg = "DataForSEO 잔액 없음(402) — https://app.dataforseo.com 에서 잔액·키 확인"
        results, out = run(only="rank", stages=table(raises={"rank": _collector.Fatal(msg)}))
        assert dict(results)["rank"].reason == msg, dict(results)["rank"]
        assert run_all.chain_rc(results) == 1, results
        assert msg in out, out          # 요약표에 첫 오류 문장이 붙는다

        # 3. 전부 실패(ok=False)는 rc 1, 일부 실패(partial)는 rc 0 + 노란 표시
        results, out = run(only="rank", stages=table({
            "rank": _collector.StageResult(ok=False, reason="402 Payment Required")}))
        assert run_all.chain_rc(results) == 1 and "402" in out, out
        results, out = run(only="rank", stages=table({
            "rank": _collector.StageResult(ok=True, rows=3, partial=True,
                                           reason="97건 실패: 402 Payment Required")}))
        assert run_all.chain_rc(results) == 0, results
        assert "완료 ⚠" in out and "97건 실패" in out, out

        # 4. 카나리아 자체는 유료 단계가 남아 있을 때만 돈다 (돈 안 드는 바퀴엔 불필요)
        seen: list = []
        run(only="gsc,gaps", preflight=lambda stgs: seen.append(stgs) or {})
        assert seen == [], "유료 단계가 없는데 카나리아를 불렀다"
        run(only="rank", preflight=lambda stgs: seen.append([s.name for s in stgs]) or {})
        assert seen == [["rank"]], seen
    finally:
        for k, v in orig.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v


def test_run_all_script_stages_are_uniform_and_report_path_comes_from_dashboard():
    """gaps/report 는 이제 scoring.load / dashboard.export 직접 호출이다 — 표에서는
    다른 단계와 같은 모양(fn(project, *, dry_run) -> StageResult).

    리포트 경로는 dashboard.export() 가 돌려주는 Path 를 그대로 옮긴다(파싱 없음) —
    run_all 이 같은 경로 규칙을 다시 계산하면 dashboard.export() 와 두 벌이 된다.
    예외는 여기서 삼키지 않는다 — run_chain 의 _run_stage 가 StageResult(ok=False) 로 접는다.
    """
    import run_all

    orig_load, orig_export = run_all.scoring.load, run_all.dashboard.export
    try:
        # dry-run 이면 외부 호출이 아예 없다
        def boom(*a, **k):
            raise AssertionError("dry-run 인데 scoring.load/dashboard.export 를 탔다")
        run_all.scoring.load = boom
        run_all.dashboard.export = boom
        with contextlib.redirect_stdout(io.StringIO()):
            assert run_all.load_opportunities("p", dry_run=True).skipped
            assert run_all.export_report("p", dry_run=True).skipped

        seen: dict = {}

        fake_path = Path(tempfile.gettempdir()) / "reports" / "p" / "2026-08-25.html"

        def fake_export(project, actions_file=None):
            seen["project"] = project
            return fake_path

        run_all.dashboard.export = fake_export
        r = run_all.export_report("p")
        assert r.ok and r.artifact == str(fake_path), r
        assert seen["project"] == "p", seen

        def fake_load(project):
            seen["loaded"] = project

        run_all.scoring.load = fake_load
        g = run_all.load_opportunities("p")
        assert g.ok and seen["loaded"] == "p", seen

        # 예외는 여기서 삼키지 않고 그대로 올린다 — _run_stage 가 StageResult(ok=False)로 접는다.
        def raises(*a, **k):
            raise RuntimeError("db 잠김")
        run_all.dashboard.export = raises
        stage = run_all.Stage("report", "리포트", run_all.export_report, False)
        bad = run_all._run_stage(stage, "p", dry_run=False, skip_set=set(), only_set=set(), opts={})
        assert not bad.ok and "db 잠김" in bad.reason, bad
    finally:
        run_all.scoring.load = orig_load
        run_all.dashboard.export = orig_export


def test_collector_cli_forwards_knob_and_dispatches_correct_stage_name():
    """collector.cli 가 열 개 수집기의 main() 을 대신하는 공통 통로다.

    main() 열 벌을 이 한 함수로 접으면서 "CLI 플래그 하나가 실제로 collect() 까지
    도달하는가"와 "remote.dispatch 에 넘기는 단계 이름이 그 단계 자신과 같은가"를
    개별 파일마다 손으로 확인하던 걸 표(run_all.STAGES) 하나를 돌며 검사한다.
    """
    import collector
    import run_all

    orig_dispatch = collector.remote.dispatch
    orig_argv = sys.argv
    try:
        for stg in run_all.STAGES:
            if stg.module is None:      # gaps·report — 자체 모듈이 아니다
                continue
            specs = getattr(stg.module._parser(), "_collector_settings", [])
            assert specs, f"[{stg.name}] add_setting 으로 등록한 노브가 하나도 없다"
            spec = specs[0]
            probe = {"int": "97", "float": "0.97"}.get(
                spec.type_fn.__name__ if spec.type_fn else "", "probe")
            sys.argv = ["prog", "--project", "p", "--dry-run",
                       f"--{spec.dest.replace('_', '-')}", probe]

            captured: dict = {}

            def fake_collect(project, *, dry_run=False, **kwargs):
                captured.update(project=project, dry_run=dry_run, kwargs=kwargs)
                return collector.StageResult(ok=True, skipped=True)

            dispatched: dict = {}

            def fake_dispatch(args, stage_name):
                dispatched.update(name=stage_name, project=getattr(args, "project", None))
                return False   # 로컬로 계속 진행 — collect() 까지 가는지를 봐야 한다

            orig_collect = stg.module.collect
            stg.module.collect = fake_collect
            collector.remote.dispatch = fake_dispatch
            try:
                collector.cli(stg.name)
            finally:
                stg.module.collect = orig_collect

            assert dispatched.get("name") == stg.name, (stg.name, dispatched)
            assert dispatched.get("project") == "p", (stg.name, dispatched)
            expected = spec.type_fn(probe) if spec.type_fn else probe
            assert captured.get("kwargs", {}).get(spec.dest) == expected,                 (stg.name, spec.dest, expected, captured)
            assert captured.get("dry_run") is True, (stg.name, captured)
    finally:
        collector.remote.dispatch = orig_dispatch
        sys.argv = orig_argv


def test_serp_adapter_credentials_timeouts_and_labs():
    """serp_adapter: 타임아웃 상수 정본, 키 판정 정본, Labs ranked_keywords 단일화 검증."""
    # 1. 타임아웃 정본
    assert serp_adapter.TIMEOUTS == {
        "dataforseo": 60,
        "serper": 30,
        "openrouter": 120,
        "suggest": 10,
        "page": 20,
        "canary": 15,
    }
    assert serp_adapter.LABS_COST_PER_CALL == 0.001

    # 2. 자격증명 판정 함수
    orig_env = {
        "DATAFORSEO_LOGIN": os.environ.get("DATAFORSEO_LOGIN"),
        "DATAFORSEO_PASSWORD": os.environ.get("DATAFORSEO_PASSWORD"),
        "SERPER_API_KEY": os.environ.get("SERPER_API_KEY"),
        "OPENROUTER_API_KEY": os.environ.get("OPENROUTER_API_KEY"),
    }
    try:
        os.environ.pop("DATAFORSEO_LOGIN", None)
        os.environ.pop("DATAFORSEO_PASSWORD", None)
        os.environ.pop("SERPER_API_KEY", None)
        os.environ.pop("OPENROUTER_API_KEY", None)

        assert serp_adapter.has_dataforseo() is False
        assert serp_adapter.has_serper() is False
        assert serp_adapter.has_openrouter() is False

        os.environ["DATAFORSEO_LOGIN"] = "login_test"
        assert serp_adapter.has_dataforseo() is False  # password 누락
        os.environ["DATAFORSEO_PASSWORD"] = "pw_test"
        assert serp_adapter.has_dataforseo() is True

        os.environ["SERPER_API_KEY"] = "serper_test"
        assert serp_adapter.has_serper() is True

        os.environ["OPENROUTER_API_KEY"] = "openrouter_test"
        assert serp_adapter.has_openrouter() is True

        # 3. fetch_labs_ranked_keywords 모킹 검증
        orig_post = serp_adapter.requests.post
        captured = []

        def mock_post(url, *args, **kwargs):
            captured.append((url, kwargs))
            return FakeResponse({
                "tasks": [{
                    "status_code": 20000,
                    "cost": 0.001,
                    "result": [{
                        "items": [
                            {"keyword_data": {"keyword": "test kw", "keyword_info": {"search_volume": 500}}}
                        ]
                    }]
                }]
            })

        serp_adapter.requests.post = mock_post
        try:
            items, cost = serp_adapter.fetch_labs_ranked_keywords("test.com", "ko-KR", limit=10)
            assert len(items) == 1
            assert items[0] == {"keyword": "test kw", "search_volume": 500}
            assert cost == 0.001
            assert len(captured) == 1
            url, kw = captured[0]
            assert "ranked_keywords" in url
            assert kw["timeout"] == serp_adapter.TIMEOUTS["dataforseo"]
            assert kw["auth"] == ("login_test", "pw_test")

            # task error 검증 — 인증·결제 계열(401xx·402xx)은 Fatal 이다.
            # 다음 키워드에서 낫지 않는 오류라 각 항목마다 다시 사러 가면 안 된다.
            serp_adapter.requests.post = lambda *a, **k: FakeResponse({
                "tasks": [{"status_code": 40100, "status_message": "Invalid auth"}]
            })
            try:
                serp_adapter.fetch_labs_ranked_keywords("test.com", "ko-KR", limit=10)
                raise AssertionError("status_code >= 40000 은 에러를 내야 함")
            except serp_adapter.Fatal as e:
                assert "401" in str(e) and "Invalid auth" in str(e), e

            # 그 밖의 task 오류는 그대로 항목 오류(RuntimeError) — 다음 항목은 살 수 있다
            serp_adapter.requests.post = lambda *a, **k: FakeResponse({
                "tasks": [{"status_code": 40501, "status_message": "no rows"}]
            })
            try:
                serp_adapter.fetch_labs_ranked_keywords("test.com", "ko-KR", limit=10)
                raise AssertionError("status_code >= 40000 은 에러를 내야 함")
            except RuntimeError as e:
                assert "dataforseo task error" in str(e)
        finally:
            serp_adapter.requests.post = orig_post
    finally:
        for k, v in orig_env.items():
            if v is not None:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)


def test_doctor_never_says_the_same_thing_twice():
    """같은 안내가 locked(잠긴 기능)와 later(나중에)에 두 벌 들어가면 안 된다.

    실제로 유료 키 두 항목이 양쪽에 있었고 later 쪽 사본이 낡아 있었다
    (없어진 "setup.md 5절"을 계속 가리켰다). 화면 절반이 "못 하는 것" 목록이 되는
    원인이기도 했다 — 발급처·설치 명령의 정본은 caps/locked 한 곳이다.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "setup" / "scripts"))
    import doctor

    orig = {k: os.environ.get(k) for k in
            ("OPENROUTER_API_KEY", "DATAFORSEO_LOGIN", "DATAFORSEO_PASSWORD", "SERPER_API_KEY")}
    try:
        for k in orig:
            os.environ.pop(k, None)          # 키 0개 — 두 항목이 다 잠긴 상태
        d = doctor.diagnose()
        # 이름을 여기 옮겨 적지 않는다 — 정본은 명부다. 문구가 바뀌었다고 검사가
        # 깨지면 안 되고, 그 두 항목이 잠겼는지는 id 로 물어야 한다.
        paid = {c["name"] for c in doctor.CAPABILITIES if c["id"] in ("ai", "rank")}
        assert len(paid) == 2, doctor.CAPABILITIES
        assert paid <= {c["name"] for c in d["locked"]}, d["locked"]
        for marker in ("openrouter.ai", "dataforseo.com", "serper.dev", "pip install google-"):
            assert not any(marker in s for s in d["later"]), \
                f"{marker} 안내가 later 에도 있다(locked 와 두 벌): {d['later']}"
    finally:
        for k, v in orig.items():
            if v is not None:
                os.environ[k] = v


def test_repo_binds_project_and_ambiguity_is_not_guessed():
    """다른 리포에서 /setup 을 돌려도 늘 먼저 등록한 사이트가 뜨던 버그.

    Brain 은 컴퓨터 전역이라 프로젝트가 여럿이면 "이 폴더가 어느 사이트냐"를 따로
    정해야 한다. 답은 `/create profile` 이 남기는 repo.yaml 의 repo_path 다.
    못 고르는 경우에 아무거나 집으면 사용자는 그게 이 폴더의 사이트인 줄 안다.
    """
    import stage

    proj_dir = HOME / "projects"
    proj_dir.mkdir(parents=True, exist_ok=True)
    repo_a = HOME / "repos" / "alpha"
    (repo_a / "src").mkdir(parents=True, exist_ok=True)
    (proj_dir / "alpha.repo.yaml").write_text(
        f"repo_path: {repo_a}\nstack: astro\n", encoding="utf-8")
    # 템플릿 그대로인 파일은 무시돼야 한다 — 안 그러면 모든 폴더가 여기 걸린다.
    (proj_dir / "zeta.repo.yaml").write_text(
        "repo_path: /path/to/repo\n", encoding="utf-8")

    both = ["alpha", "zeta"]
    assert db.repo_project(repo_a) == "alpha"
    assert db.repo_project(repo_a / "src") == "alpha", "하위 폴더에서도 붙어야 함"
    assert db.repo_project(HOME) is None, "무관한 폴더는 매치가 없어야 함"

    assert stage.pick_project(both, cwd=repo_a) == "alpha"
    assert stage.pick_project(both, cwd=HOME) is None, "모르면 아무거나 집지 않는다"
    assert stage.pick_project(["solo"], cwd=HOME) == "solo", "하나뿐이면 그것"
    assert stage.pick_project([], cwd=repo_a) is None

    # 등록 안 된 이름이 repo.yaml 에 남아 있어도 Brain 목록 밖이면 안 고른다
    assert stage.pick_project(["zeta"], cwd=repo_a) == "zeta"   # 하나뿐이라 그것
    assert stage.pick_project(["zeta", "other"], cwd=repo_a) is None


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)} passed  ({HOME})")
