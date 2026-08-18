#!/usr/bin/env python3
"""자체점검 — `python test_collectors.py` (임시 폴더에서만 돈다, 진짜 Brain은 안 건드림).

I/O 경계(네트워크 수집기, doctor, MCP 런처)를 네트워크 호출 없이 검증한다:
  · collect_ai: OpenRouter 응답 모킹, url_citation 파싱, cited 판정, 실패 집계
  · expand_keywords: Google Suggest 모킹, is_active=0 적재, locale 보존, 50% 초과 실패율 경고
  · collect_serp: serp_adapter.fetch 모킹, write_rank_snapshot 적재, depth 밖 None, AIO 미측정 None
  · doctor --json: subprocess 실행, exit code 0, JSON 구조(verdict, next_command) 검증
  · gsc_mcp.mjs: node --check 문법 검증 (node 없으면 skip)
"""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HOME = Path(tempfile.mkdtemp(prefix="seo-miner-collector-test-"))
os.environ["CAPTURE_HOME"] = str(HOME)          # import 전에 걸어야 db가 여기를 본다
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
    orig_argv = sys.argv
    try:
        sys.argv = [
            "collect_ai.py",
            "--project", "ai_test_proj",
            "--engines", "chatgpt",
            "--samples", "1",
            "--throttle", "0",
        ]
        collect_ai.main()
    finally:
        collect_ai.requests.post = orig_post
        sys.argv = orig_argv
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
    orig_argv = sys.argv
    orig_stderr = sys.stderr
    stderr_buf = io.StringIO()

    try:
        sys.stderr = stderr_buf
        sys.argv = [
            "expand_keywords.py",
            "--project", "expand_test_proj",
            "--mode", "autocomplete",
            "--throttle", "0",
        ]
        expand_keywords.main()
    finally:
        expand_keywords.requests.get = orig_get
        expand_keywords.modifiers = orig_modifiers
        sys.argv = orig_argv
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

    def fake_fetch(provider, keyword, locale, depth=10):
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
    orig_argv = sys.argv
    try:
        sys.argv = [
            "collect_serp.py",
            "--project", "serp_test_proj",
            "--provider", "dataforseo",
            "--throttle", "0",
        ]
        collect_serp.main()
    finally:
        serp_adapter.fetch = orig_fetch
        sys.argv = orig_argv

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


def test_doctor_json_subprocess():
    """doctor.py --json: 임시 CAPTURE_HOME 환경에서 실행 -> exit code 0,
    stdout이 JSON으로 정상 파싱되고 verdict, next_command 키가 존재하는지 assert."""
    doc_home = Path(tempfile.mkdtemp(prefix="seo-miner-doc-test-"))
    doctor_script = Path(__file__).resolve().parents[2] / "setup" / "scripts" / "doctor.py"
    assert doctor_script.exists(), f"doctor.py 스크립트 없음: {doctor_script}"

    env = {**os.environ, "CAPTURE_HOME": str(doc_home)}
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


def test_gsc_mcp_mjs_syntax():
    """gsc_mcp.mjs: node --check 로 구문 검증. node가 없으면 skip 안내 후 통과."""
    node_bin = shutil.which("node")
    if not node_bin:
        print("  skip: node 없음")
        return
    mjs_path = Path(__file__).resolve().parents[2] / "setup" / "scripts" / "gsc_mcp.mjs"
    assert mjs_path.exists(), f"gsc_mcp.mjs 없음: {mjs_path}"

    res = subprocess.run(
        [node_bin, "--check", str(mjs_path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert res.returncode == 0, f"gsc_mcp.mjs 문법 오류 (code {res.returncode}):\n{res.stderr}"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)} passed  ({HOME})")
