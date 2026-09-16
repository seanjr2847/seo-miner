#!/usr/bin/env python3
"""자체점검 — `python test_capture.py` (임시 폴더에서만 돈다, 진짜 Brain은 안 건드림).

가장 조용히 틀리는 것들만 본다:
  · db.py sql 이 진짜 읽기 전용인지 — WITH ... DELETE 는 문자열 검사를 통과한다
  · 기간 다른 GSC 스냅샷을 비교하지 않는지 — 28일치와 90일치를 빼면 Δ가 거짓이다
  · 같은 기회를 두 번 적재해도 목록이 안 불어나는지
  · 판정 규칙(scoring) — 임계값·남의 브랜드 제외·기회 정렬이 한 곳에서 나오는지
  · 키워드 후보가 locale 없이 쌓이지 않는지 — 쓰기 경로가 하나여야 막힌다
  · 수집이 예외로 끊겨도 실행 기록이 닫히는지
  · 분석 함수(ctr_gap·cannibalization·striking 하한·rank_decay·coverage)와 결정적 점수
  · rank 스냅샷 같은 날 재실행 멱등 / AI 답변 전문 보존
  · 일별·분해·색인 3종 쓰기의 재수집 규칙(덮어쓰기/지우고 다시/upsert)
  · device_gap 임계 경계와 index_issues 버킷 우선순위 — 경계는 버그가 사는 곳이다
"""
import json
import os
import sys
import tempfile
from pathlib import Path

HOME = Path(tempfile.mkdtemp(prefix="seo-miner-test-"))
os.environ["CAPTURE_HOME"] = str(HOME)
sys.path.insert(0, str(Path(__file__).parent))

import collector          # noqa: E402
import dashboard          # noqa: E402
import db                 # noqa: E402
import scoring            # noqa: E402
import serp_adapter       # noqa: E402


def _project(conn, name="t"):
    conn.execute("INSERT OR IGNORE INTO projects(name,domain) VALUES(?, 'e.com')", (name,))
    conn.commit()
    return conn.execute("SELECT * FROM projects WHERE name=?", (name,)).fetchone()


def _snap(conn, pid, date_, days, query, pos, clicks):
    conn.execute("""INSERT INTO gsc_snapshots(project_id,snapshot_date,period_days,
                      query,page,clicks,impressions,ctr,position)
                    VALUES(?,?,?,?,NULL,?,100,0.1,?)""",
                 (pid, date_, days, query, clicks, pos))
    conn.commit()


def test_sql_is_really_read_only():
    conn = db.connect()
    p = _project(conn, "ro")
    conn.execute("INSERT INTO opportunities(project_id,kind,target) VALUES(?,'k','t')",
                 (p["id"],))
    conn.commit()
    conn.close()
    # 문자열 검사는 통과하는 문장 — 커넥션이 읽기 전용이어야 막힌다.
    q = "WITH x AS (SELECT 1) DELETE FROM opportunities"
    assert q.strip().lower().startswith(("select", "with"))   # 옛 가드는 뚫린다
    try:
        db.run_sql(q)
    except SystemExit as e:      # run_sql이 sqlite 예외를 잡아 안내 문구로 종료한다
        assert "조회 전용" in str(e), e
    except Exception as e:       # 안내 문구를 못 붙인 경우라도 DB는 막아야 한다
        assert "readonly" in str(e).lower(), e
    else:
        raise AssertionError("읽기 전용 커넥션이 쓰기를 막지 못했다")
    conn = db.connect()
    assert conn.execute("SELECT COUNT(*) FROM opportunities WHERE project_id=?",
                        (p["id"],)).fetchone()[0] == 1      # 삭제되지 않았다
    conn.close()


def test_get_project_raises_domain_exception_for_unregistered_site():
    """db.get_project — 미등록 사이트면 sys.exit 대신 도메인 예외로 알린다.

    HTTP 요청 경로가 그걸 다시 404 로 번역하는 흐름을 끊는 게 목적이라,
    str(e) 가 기존 안내 문구를 그대로 담고 있어야 한다.
    """
    conn = db.connect()
    name = "없는사이트"
    try:
        db.get_project(conn, name)
    except db.ProjectNotFound as e:
        msg = str(e)
        assert f"'{name}' 사이트가 아직 등록되지 않았습니다" in msg, msg
        assert "`/capture add" in msg or "/capture add" in msg, msg
        assert "sync-project" in msg, msg
    else:
        raise AssertionError("ProjectNotFound 를 던지지 않았다")
    conn.close()


def test_load_project_yaml_raises_domain_exception_when_missing():
    """db.load_project_yaml — yaml 이 없으면 sys.exit 대신 도메인 예외로 알린다.

    collector.project_cfg 가 그걸 잡아 경고만 찍고 빈 dict 로 진행하는 자리 —
    기존 sys.exit 메시지를 그대로 보존해야 동작이 안 흔들린다.
    """
    try:
        db.load_project_yaml("없는거")
    except db.ProjectConfigNotFound as e:
        assert str(e) == "project yaml not found: 없는거", str(e)
    else:
        raise AssertionError("ProjectConfigNotFound 를 던지지 않았다")


def test_gather_refuses_mixed_periods():
    conn = db.connect()
    p = _project(conn, "period")
    _snap(conn, p["id"], "2026-01-01", 90, "kw", 12.0, 5)
    _snap(conn, p["id"], "2026-02-01", 28, "kw", 8.0, 9)
    d = dashboard.gather(conn, p)
    assert d["gsc_date"] == "2026-02-01" and d["gsc_period"] == 28
    assert d["gsc_prev"] is None, d["gsc_prev"]       # 90일치와는 짝짓지 않는다
    assert d["period_mismatch"] is True
    assert d["ups"] == [] and d["downs"] == []

    _snap(conn, p["id"], "2026-01-15", 28, "kw", 11.0, 4)
    d = dashboard.gather(conn, p)
    assert d["gsc_prev"] == "2026-01-15", d["gsc_prev"]   # 같은 28일치를 찾아간다
    assert d["period_mismatch"] is False
    assert d["ups"] and d["ups"][0]["dpos"] == 3.0        # 11.0 -> 8.0
    conn.close()


def test_gather_pins_gsc_axis_to_chosen_date():
    """[기준 수집일]로 과거를 고르면 GSC 축만 그날로 간다 — 미래를 prev 로 안 쓴다."""
    conn = db.connect()
    p = _project(conn, "pin")
    for d_, pos, clk in (("2026-03-01", 20.0, 1), ("2026-04-01", 15.0, 4),
                         ("2026-05-01", 9.0, 9)):
        _snap(conn, p["id"], d_, 28, "kw", pos, clk)

    d = dashboard.gather(conn, p)                       # 고정 없음 = 최신
    assert (d["gsc_date"], d["gsc_prev"]) == ("2026-05-01", "2026-04-01")
    assert d["gsc_pinned"] is False
    assert [x["date"] for x in d["gsc_dates"]] == [
        "2026-05-01", "2026-04-01", "2026-03-01"]

    d = dashboard.gather(conn, p, "2026-04-01")         # 한 칸 과거로
    # 05-01 을 prev 로 끌어오면 Δ의 부호가 뒤집힌다
    assert (d["gsc_date"], d["gsc_prev"]) == ("2026-04-01", "2026-03-01"),         (d["gsc_date"], d["gsc_prev"])
    assert d["gsc_pinned"] is True
    assert d["ups"] and d["ups"][0]["dpos"] == 5.0      # 20.0 -> 15.0
    # 고를 수 있는 목록은 고정과 무관하게 전부 — 되돌아올 길이 있어야 한다
    assert len(d["gsc_dates"]) == 3

    # 수집한 적 없는 날 — 지어내지 않고 빈 축. 화면이 이걸 보고 고정을 푼다.
    assert dashboard.gather(conn, p, "2025-12-25")["gsc_date"] is None
    conn.close()


def test_opportunity_upsert_does_not_duplicate():
    conn = db.connect()
    p = _project(conn, "opp")
    db.upsert_opportunities(conn, p["id"], None,
                            [("striking_distance", "kw", 50, "첫 런")])
    oid = conn.execute("SELECT id FROM opportunities WHERE project_id=?", (p["id"],)).fetchone()[0]
    db.set_opportunity_status(conn, oid, "done")
    db.upsert_opportunities(conn, p["id"], None,
                            [("striking_distance", "kw", 80, "두 번째 런")])
    rows = conn.execute("SELECT score, reasoning, status FROM opportunities "
                        "WHERE project_id=?", (p["id"],)).fetchall()
    assert len(rows) == 1, rows                       # 같은 기회가 두 줄이 되지 않는다
    assert rows[0]["score"] == 80 and rows[0]["reasoning"] == "두 번째 런"
    assert rows[0]["status"] == "done"                # 손댄 상태는 살아남는다
    conn.close()


def test_scoring_rules():
    """판정 규칙 module 자체점검 — 임계값·남의 브랜드 제외·기회 정렬."""
    scoring._selfcheck()


def test_load_covers_every_kind():
    """scoring.load() 가 KINDS 명부의 종류를 전부 실제로 낸다 — end-to-end.

    scoring._selfcheck() 는 ALL_KINDS·KINDS·라벨이 서로 어긋나지 않는지만 본다
    (정적 대조). 이 테스트는 그걸 넘어 진짜 데이터를 깔고 load() 를 돌려서
    "명부에 있는 kind 가 실제로 opportunities 에 찍히는가"를 증명한다 — 검출기
    시그니처가 달라 명부 순회가 어느 한 kind 를 조용히 건너뛰어도 여기서 잡힌다.
    """
    conn = db.connect()
    p = _project(conn, "kinds")
    pid = p["id"]

    conn.executemany(
        "INSERT INTO gsc_snapshots(project_id,snapshot_date,period_days,query,page,"
        "clicks,impressions,ctr,position) VALUES(?,?,?,?,?,?,?,0.0,?)",
        [(pid, "2026-08-07", 28, "밀리는키워드", None, 5, 100, 12.0),
         (pid, "2026-08-07", 28, "1페이지키워드", None, 9, 300, 3.0),
         (pid, "2026-08-07", 28, "하락키워드", None, 10, 100, 5.0),
         (pid, "2026-08-14", 28, "밀리는키워드", None, 6, 110, 11.5),
         (pid, "2026-08-14", 28, "1페이지키워드", None, 9, 300, 3.0),
         (pid, "2026-08-14", 28, "하락키워드", None, 1, 100, 9.0),
         (pid, "2026-08-14", 28, "pseo후보", None, 0, 60, 6.0),
         (pid, "2026-08-14", 28, "겹치는키워드", "https://e.com/a", 3, 60, 4.0),
         (pid, "2026-08-14", 28, "겹치는키워드", "https://e.com/b", 1, 40, 7.0),
         # 의도 갈린 페이지 — 비교 200 · 구매 35(검색어 2)
         (pid, "2026-08-14", 28, "한관종 비립종 차이", "https://e.com/split/", 4, 200, 5.0),
         (pid, "2026-08-14", 28, "한관종 제거 비용", "https://e.com/split/", 1, 20, 9.0),
         (pid, "2026-08-14", 28, "비립종 제거 가격", "https://e.com/split/", 0, 15, 11.0)])

    conn.execute("INSERT INTO keywords(project_id, keyword, cluster, is_active) "
                 "VALUES(?, '미커버키워드', 'c1', 1)", (pid,))
    conn.execute("INSERT INTO keywords(project_id, keyword, is_active, volume) "
                 "VALUES(?, 'aio빠진키워드', 1, 500)", (pid,))
    aio_kw_id = conn.execute("SELECT id FROM keywords WHERE keyword='aio빠진키워드'").fetchone()[0]
    conn.execute("INSERT INTO rank_snapshots(keyword_id, checked_at, position, aio_present, "
                 "aio_cited) VALUES(?, '2026-08-14T00:00:00Z', 15, 1, 0)", (aio_kw_id,))

    conn.executemany(
        "INSERT INTO gsc_breakdown(project_id, snapshot_date, period_days, dim, dim_value, "
        "query, clicks, impressions, ctr, position) VALUES(?, '2026-08-17', 28, 'device', ?, "
        "'모바일밀림', ?, ?, 0.0, ?)",
        [(pid, "MOBILE", 20, 1240, 12.4), (pid, "DESKTOP", 50, 500, 7.1)])

    conn.execute(
        "INSERT INTO gsc_index_status(project_id, checked_date, url, verdict, coverage_state, "
        "robots_txt_state) VALUES(?, '2026-08-18', '/blocked', 'FAIL', 'Blocked by robots.txt', "
        "'DISALLOWED')", (pid,))

    conn.execute("INSERT INTO ai_prompts(project_id, prompt) VALUES(?, '이 도구 추천해줘')", (pid,))
    prompt_id = conn.execute("SELECT id FROM ai_prompts WHERE project_id=?", (pid,)).fetchone()[0]
    with db.run(conn, pid, "ai") as r:
        conn.execute("INSERT INTO ai_checks(prompt_id, run_id, engine, cited_domains_json) "
                     "VALUES(?, ?, 'chatgpt', '[\"rival.com\"]')", (prompt_id, r.id))

    conn.execute(
        "INSERT INTO keyword_gap(project_id, checked_date, keyword, domain, position, "
        "our_position, volume, kind) VALUES(?, '2026-08-14', '경쟁사만있는키워드', 'rival.com', "
        "3, NULL, 800, 'missing')", (pid,))

    # robots.txt 원문까지 남긴다 — AI 크롤러 차단(ai_bot_blocked)은 새로 가져오지
    # 않고 이 원문을 다시 읽는다. 학습 봇(GPTBot)과 검색 봇(OAI-SearchBot)을 둘 다
    # 막은 꼴 — 기회는 검색 봇 쪽 하나만 서야 한다(학습만 막는 것은 인용과 무관).
    _robots = chr(10).join(("User-agent: GPTBot", "Disallow: /", "",
                            "User-agent: OAI-SearchBot", "Disallow: /", "",
                            "User-agent: *", "Allow: /"))
    run_id = conn.execute(
        "INSERT INTO crawl_runs(project_id, finished_at, seed, robots_txt) "
        "VALUES(?, '2026-08-18T00:00:00Z', 'sitemap', ?) RETURNING id",
        (pid, _robots)).fetchone()[0]
    conn.execute("INSERT INTO crawl_issues(run_id, kind, severity, url, detail) "
                 "VALUES(?, 'http_error', 'bad', '/404', '404')", (run_id,))

    conn.execute(
        "INSERT INTO backlinks(project_id, checked_date, url_from, url_to, domain_from, rank, "
        "is_broken) VALUES(?, '2026-08-18', 'https://o.com/a', 'https://e.com/dead', 'o.com', "
        "40, 1)", (pid,))
    conn.execute(
        "INSERT INTO link_intersect(project_id, checked_date, domain, rank, hits, targets, "
        "we_have) VALUES(?, '2026-08-18', 'authority.com', 55, 2, 'r1.com,r2.com', 0)", (pid,))
    conn.commit()
    conn.close()

    scoring.load("kinds")

    conn = db.connect()
    kinds = {r[0] for r in conn.execute(
        "SELECT DISTINCT kind FROM opportunities WHERE project_id=?", (pid,))}
    bots = {r[0] for r in conn.execute(
        "SELECT target FROM opportunities WHERE project_id=? AND kind='ai_bot_blocked'", (pid,))}
    conn.close()
    assert bots == {"OAI-SearchBot"}, f"학습 봇 차단을 인용 차단으로 올렸거나 검색 봇을 놓쳤다: {bots}"
    missing = set(scoring.ALL_KINDS) - kinds
    assert not missing, f"명부엔 있는데 load() 가 안 낸 kind: {missing}"
    extra = kinds - set(scoring.ALL_KINDS)
    assert not extra, f"load() 가 냈는데 명부엔 없는 kind: {extra}"


def test_collector_settings():
    """설정 우선순위(CLI > 프로젝트 yaml > config.yaml > 리터럴) 및 0값 존중 자체점검."""
    import argparse
    ap = argparse.ArgumentParser()
    collector.add_setting(ap, "--depth", key="serp_depth", fallback=10, type=int)
    collector.add_setting(ap, "--throttle", key="throttle", fallback=0.7, type=float)
    collector.add_setting(ap, "--max-keywords", key="limits.max_keywords", fallback=99, type=int)
    collector.add_setting(ap, "--custom", key="custom_key", fallback=42, type=int)
    specs = ap._collector_settings   # settings() 는 이제 이 명부를 명시적으로 받는다 —
                                      # 프로세스 전역이던 시절엔 수집기끼리 같은 dest 를
                                      # 다른 key 로 등록해도 서로 덮어썼다.

    # 1. CLI 최우선 + 0도 유효값 (0이 fallback 및 프로젝트 yaml을 이긴다)
    a1 = ap.parse_args(["--depth", "3", "--max-keywords", "0", "--throttle", "0"])
    s1 = collector.settings(a1, {"serp_depth": 9, "limits": {"max_keywords": 50}, "throttle": 1.0},
                            specs)
    assert s1["serp_depth"] == 3
    assert s1["limits.max_keywords"] == 0, "CLI 0이 프로젝트 yaml 및 fallback을 이겨야 한다"
    assert s1["max_keywords"] == 0
    assert s1["throttle"] == 0.0

    # 2. 프로젝트 yaml
    a2 = ap.parse_args([])
    s2 = collector.settings(a2, {"serp_depth": 9, "limits": {"max_keywords": 5}}, specs)
    assert s2["serp_depth"] == 9
    assert s2["limits.max_keywords"] == 5

    # 3. config.yaml defaults
    s3 = collector.settings(a2, None, specs)
    assert s3["throttle"] in (0.5, 0.7)
    assert s3["serp_depth"] == 10

    # 4. fallback
    s4 = collector.settings(a2, {}, specs)
    assert s4["custom_key"] == 42
    assert s4["limits.max_keywords"] == 99

    collector._selfcheck()


def test_keyword_candidates_always_carry_locale():
    """자동완성으로 캔 후보가 locale 없이 쌓이면 프로젝트 로케일로 다시 조회돼
    한국어 키워드가 전부 '순위 없음'이 된다 — 실제로 났던 버그다.
    쓰기 경로가 하나라서 호출부가 빼먹을 수 없어야 한다."""
    conn = db.connect()
    p = _project(conn, "loc")
    n = db.add_keyword_candidates(conn, p["id"], [
        ("한국어 키워드", "ko-KR", "autocomplete"),
        ("english keyword", "en-US", "autocomplete"),
        ("  ", "ko-KR", "autocomplete"),          # 빈 문자열은 세지 않는다
    ])
    assert n == 2, n
    rows = dict(conn.execute(
        "SELECT keyword, locale FROM keywords WHERE project_id=?", (p["id"],)).fetchall())
    assert rows["한국어 키워드"] == "ko-KR", rows
    assert rows["english keyword"] == "en-US", rows
    # 같은 후보를 또 넣어도 늘지 않는다
    assert db.add_keyword_candidates(conn, p["id"], [("한국어 키워드", "ko-KR", "serp")]) == 0
    conn.close()


def test_serp_adapter_selfcheck():
    """SERP 어댑터 자체점검 — _domains_in 의 인용 한정 수집, 노이즈 차단 포함.
    자체점검 안에 들어 있는 노이즈 케이스는 (인용 밖 이미지 url 빠짐 / references 안 url 잡힘)
    두 가지다 — ai_overview 응답 구조가 바뀌면 ai_overview 자리에 적재된 값이
    거짓 양성이 된다. self-check 로 막아야 다음 사람이 알아챈다."""
    serp_adapter._selfcheck()


def test_classify_intent_4_intents_and_priority():
    """classify_intent - 4 인텐트 + 우선순위 transactional > commercial > navigational > info.

    우선순위: pricing 은 transactional/commercial 양쪽 토큰 사전에 들어가 있지만
    transactional 이 이겨야 한다 — 두 분류가 동시에 매칭돼도 상위 인텐트가 채택됨을
    보장하기 위함 (그렇지 않으면 'best pricing' 같은 상업+구매 의도 구분이 흔들린다).
    """
    # 네 인텐트 각각
    assert scoring.classify_intent("비트코인 가격") == "transactional"     # 가격
    assert scoring.classify_intent("ecrett pricing") == "transactional"   # pricing
    assert scoring.classify_intent("ecrett 후기") == "commercial"         # 후기
    assert scoring.classify_intent("Best AI Tools") == "commercial"       # best
    assert scoring.classify_intent("chatgpt login") == "navigational"     # login
    assert scoring.classify_intent("example.com 공식") == "navigational"  # 공식
    assert scoring.classify_intent("외부 링크") == "info"                  # 매칭 없음
    assert scoring.classify_intent("") == "info"
    # 우선순위
    assert scoring.classify_intent("best pricing") == "transactional"     # pricing 이긴다
    assert scoring.classify_intent("Buy reviews") == "transactional"      # buy가 review보다 먼저
    assert scoring.classify_intent("login 후기 가격") == "transactional"  # 셋 다 있어도 transactional


def test_intent_dictionary_is_one_table():
    """낱말 사전은 한 벌이다 — 4분법 축(classify_intent)과 한국어 라벨(query_intent)이
    같은 표를 본다.

    두 벌이던 시절: INTENT_COMMERCIAL 에는 alternative·vs·best·추천 가 있는데
    INTENT_WORDS 의 '비교' 칸에는 없어서, 같은 `hubspot alternative` 를 요청문
    근거표는 '정보'로, 키워드 인텐트는 'commercial' 로 읽었다.
    """
    labels = {label: set(words) for label, _axis, words in scoring.INTENT_WORDS}
    axes = {label: axis for label, axis, _ in scoring.INTENT_WORDS}
    # ① 축 집합은 표에서 나온다 — 어느 칸의 낱말도 제 축 집합 안에 있다
    sets = {"transactional": scoring.INTENT_TRANSACTIONAL,
            "commercial": scoring.INTENT_COMMERCIAL,
            "navigational": scoring.INTENT_NAVIGATIONAL}
    for label, axis, words in scoring.INTENT_WORDS:
        if axis in sets:
            assert set(words) <= sets[axis], (label, set(words) - sets[axis])
    for axis, s in sets.items():
        got = {w for label, a, ws in scoring.INTENT_WORDS if a == axis for w in ws}
        assert s == got, (axis, s ^ got)
    # ② 같은 검색어를 두 분류기가 같은 갈래로 읽는다
    for q, label in (("hubspot alternative", "비교"), ("best seo tool", "비교"),
                     ("notion vs obsidian", "비교"), ("디아더피부과 리뷰", "비교"),
                     ("seo tool 추천", "비교"), ("ecrett pricing", "구매"),
                     ("milia removal", "해결"), ("chatgpt login", "탐색")):
        assert scoring.query_intent(q) == label, (q, scoring.query_intent(q))
        assert scoring.INTENT_AXIS[label] == scoring.classify_intent(q), q
    # ③ 세 벌째를 못 만든다 — 남의 브랜드를 살려 두는 낱말(KEEP_INTENTS)은 '비교' 칸의
    #    부분집합이다. 거기에만 적고 표에 안 적으면 또 어긋난다.
    assert scoring.KEEP_INTENTS <= labels["비교"], scoring.KEEP_INTENTS - labels["비교"]
    # ④ 라벨↔축은 전단사가 아니다(정보성 칸이 둘) — 그래도 모든 라벨에 축이 있다
    assert set(axes) | {scoring.INTENT_DEFAULT} == set(scoring.INTENT_AXIS)
    assert scoring.INTENT_AXIS[scoring.INTENT_DEFAULT] == "info"


def test_solving_is_not_buying():
    """'고친다'(제거·치료·수리·해결)는 '산다'(가격·구매)와 다른 칸이고 축도 다르다.

    둘을 한 칸에 묶었더니 `can you remove milia under eyes`(정보성 질문)와
    `fotor remove background`(도구 사용법)가 'transactional' 로 찍혔다 — 실측에서
    활성 키워드 432개 중 64개가 그렇게 뒤집혔다. 화면(analysis 의 AN_INTENT)은
    transactional 을 "사려는 중"이라고 읽어 주므로 그 오분류는 그대로 눈에 나간다.

    '산다' 쪽만 transactional 이다. 고치는 말은 어느 축 집합에도 안 들어간다(info).
    """
    ci, qi = scoring.classify_intent, scoring.query_intent
    # ① 고치는 쪽은 사는 쪽이 아니다
    for q in ("can you remove milia under eyes", "fotor remove background",
              "milia removal", "syringoma treatment", "how to fix 500 error",
              "밀리아 제거", "여드름 흉터 치료", "보일러 수리"):
        assert ci(q) != "transactional", (q, ci(q))
    # ② 사는 쪽은 transactional 그대로다
    for q in ("밀리아 제거 가격", "notion pricing", "juvelook 비용", "buy ecrett",
              "free trial", "구독 요금", "앱 다운로드"):
        assert ci(q) == "transactional", (q, ci(q))
    # ③ 라벨도 갈린다 — 표 순서가 '산다'를 먼저 읽는다(제거+가격은 가격 글이다)
    assert qi("밀리아 제거 가격") != qi("can you remove milia under eyes")
    assert scoring.INTENT_AXIS[qi("밀리아 제거 가격")] == "transactional"
    assert scoring.INTENT_AXIS[qi("can you remove milia under eyes")] == "info"
    # ④ 공급자 찾기(clinic·병원·업체·agency)는 아직 거래가 아니다 — 원래 어느 축
    #    집합에도 없던 낱말이라, 사는 쪽에 넣으면 없던 회귀를 새로 만든다.
    for q in ("milia removal clinic", "강남 여드름 병원", "seo agency", "이사 업체"):
        assert ci(q) != "transactional", (q, ci(q))


def test_place_intent_is_scoped_to_the_site_not_a_world_list():
    """'지역'은 세상의 지명 목록이 아니라 **그 사이트의 자리**로 판정한다.

    실물(theotherskin, ko-KR): `korean ptt` 는 그 사이트가 파는 제품인데 'korean'
    때문에 지역으로 찍혔다. 반대로 noti(ko-KR·saas)의 `7pm in korean`·`korean
    translation weekly reminder` 11건도 전부 지역이었다 — 'korean' 은 자리가 아니라
    **언어**를 가리키는 말이다. 그리고 목록에 없는 지명(대구·Austin)은 아예 못 잡았다.
    """
    qi = scoring.query_intent
    kr = scoring.site_words(locale="ko-KR", domain="theotherskin.com", aliases=("디아더피부과",))
    us = scoring.site_words(locale="en-US", domain="hubspot.com", aliases=("HubSpot",))
    # ① 언어 형용사는 자리가 아니다 — 제품명·번역 검색어가 지역으로 안 찍힌다
    assert qi("korean ptt", kr) != "지역", qi("korean ptt", kr)
    assert qi("korean translation weekly reminder", kr) != "지역"
    # ② 지명 없이 서는 꼴은 지명 목록과 무관하다 — 목록에 없는 지명이어도 잡힌다
    assert qi("대구 근처 피부과", kr) == "지역"
    assert qi("fukuoka dermatologist near me", kr) == "지역"
    assert qi("austin dermatology nearby") == "지역"          # 사이트를 몰라도 선다
    # ③ 사이트의 자리만 본다 — 같은 검색어가 사이트에 따라 다르게 읽힌다
    assert qi("dermatology seoul", kr) == "지역"
    assert qi("juvelook korea", kr) == "지역"
    assert qi("dermatology seoul", us) != "지역", "미국 사이트에 서울이 제 자리일 리 없다"
    assert qi("dermatology seoul") != "지역", "사이트를 모르면 지명은 안 본다"
    # ④ 자기 이름에 든 지명은 자리가 아니다
    own = scoring.site_words(locale="ko-KR", domain="seoulbeauty.com", aliases=("Seoul Beauty",))
    assert qi("seoul beauty lab", own) != "지역"
    assert qi("seoul beauty lab", kr) == "지역", "남의 사이트에는 그냥 지명이다"
    # ⑤ 치료·비교가 지역보다 먼저다 (표 순서)
    assert qi("milia removal seoul", kr) == "해결"
    assert qi("best dermatology clinics in korea", kr) == "비교"
    # ⑥ 나라 칸의 열쇠는 고를 수 있는 언어-지역(serp_adapter.LOCALES)의 지역이어야
    #    한다 — 아무도 못 고르는 지역에 자리를 적어 두면 그 줄은 영영 안 돈다.
    regions = {c.split("-")[1].upper() for c, _ in serp_adapter.LOCALES if "-" in c}
    assert set(scoring.REGION_PLACE) <= regions, set(scoring.REGION_PLACE) - regions
    # 지명은 '지역' 칸에 리터럴로 안 남는다 — 남으면 모든 사이트가 그걸 제 자리로 읽는다
    placeless = dict((l, w) for l, _a, w in scoring.INTENT_WORDS)["지역"]
    for words in scoring.REGION_PLACE.values():
        assert not (set(words) & set(placeless)), (words, placeless)


def test_intent_split_reads_the_place_of_its_own_site():
    """intent_split 은 그 프로젝트의 locale 로 자리를 읽는다 — 판정이 사이트마다 다르다."""
    conn = db.connect()
    p = _project(conn, "isplit_place")
    conn.execute("UPDATE projects SET locale='ko-KR' WHERE id=?", (p["id"],))
    conn.commit()
    d = "2026-04-01"
    _gsc(conn, p["id"], d, 28, "seoul dermatology", "/p/loc", 5, 120, 3.0)
    _gsc(conn, p["id"], d, 28, "dermatology gangnam", "/p/loc", 2, 90, 4.0)
    _gsc(conn, p["id"], d, 28, "milia removal", "/p/loc", 1, 40, 8.0)
    _gsc(conn, p["id"], d, 28, "syringoma treatment", "/p/loc", 1, 30, 9.0)
    out = scoring.intent_split(conn, p["id"])
    assert [r["page"] for r in out] == ["/p/loc"], out
    assert (out[0]["primary"], out[0]["secondary"]) == ("지역", "해결"), out[0]
    # 같은 데이터라도 미국 사이트면 서울·강남은 제 자리가 아니라 '정보'다 → 갈림이 아니다
    conn.execute("UPDATE projects SET locale='en-US' WHERE id=?", (p["id"],))
    conn.commit()
    assert scoring.intent_split(conn, p["id"]) == []
    conn.close()


def test_backfill_intents_preserves_manual_corrections():
    """_backfill_intents - intent IS NULL 인 활성 키워드만 채움. 보존 확인 핵심.

    기존 값이 있는 키워드는 Claude/사람 보정이다 — 코드가 그 위에 덮어쓰면
    보정이 다 사라진다 (load() 가 매 분석마다 돌기 때문에 다음 스냅샷에 흔적도
    안 남는다). 그래서 SELECT 단계에서 intent IS NULL 로만 거른다.
    """
    conn = db.connect()
    p = _project(conn, "intent")
    conn.executemany(
        "INSERT INTO keywords(project_id,keyword,cluster,is_active,intent) VALUES(?,?,?,?,?)",
        [(p["id"], "비트코인 가격", None, 1, None),       # 채움 대상
         (p["id"], "ecrett 후기", None, 1, None),         # 채움 대상
         (p["id"], "공식 홈페이지", None, 1, None),       # 채움 대상
         (p["id"], "날씨 정보", None, 1, None),           # 채움 대상 (info)
         (p["id"], "사람보정 transactional", None, 1, "transactional"),  # 보존
         (p["id"], "보정후기", None, 1, "commercial"),   # 보존
         (p["id"], "비활성널", None, 0, None),            # 비활성은 안 본다
        ])
    conn.commit()
    assert scoring._backfill_intents(conn, p["id"]) == 4
    rows = {r["keyword"]: r["intent"]
            for r in conn.execute(
        "SELECT keyword, intent FROM keywords WHERE project_id=?", (p["id"],)).fetchall()}
    assert rows["비트코인 가격"] == "transactional"
    assert rows["ecrett 후기"] == "commercial"
    assert rows["공식 홈페이지"] == "navigational"
    assert rows["날씨 정보"] == "info"
    assert rows["사람보정 transactional"] == "transactional"   # 보존
    assert rows["보정후기"] == "commercial"                     # 보존
    assert rows["비활성널"] is None                              # 비활성은 안 건드림
    # 두 번째 호출은 채울 게 없음
    assert scoring._backfill_intents(conn, p["id"]) == 0
    conn.close()


def test_fit_of_three_tiers():
    """_fit_of - 0.8 active keyword 정확 일치 / 0.65 cluster 매칭 / 0.5 무관.

    fit 은 Claude 가 보정하지만, 데이터로 답할 수 있는 건 코드에서 결정적으로
    박아야 한다 — 0.5 중립만 두면 w_fit 가 큰 local_business 같은 프리셋에서
    모든 기회가 점수 면적 한가운데만 차지한다. coverage 행의 0.65 도 확인.
    """
    conn = db.connect()
    p = _project(conn, "fit")
    conn.executemany(
        "INSERT INTO keywords(project_id,keyword,cluster,is_active) VALUES(?,?,?,?)",
        [(p["id"], "비트코인 가격", None, 1),
         (p["id"], "암호화폐 시장", "암호화폐", 1),
         (p["id"], "서울 여행", "여행", 1),
         (p["id"], "꺼짐 키워드", "비활성클러스터", 0)])  # 비활성 — fit 계산 제외
    conn.commit()
    assert scoring._fit_of(conn, p["id"], "비트코인 가격") == 0.8                # tier 1
    assert scoring._fit_of(conn, p["id"], "비트코인  가격") == 0.8              # norm 동일
    assert scoring._fit_of(conn, p["id"], "암호화폐 시세") == 0.65              # cluster 명 포함
    assert scoring._fit_of(conn, p["id"], "완전히 무관한 단어") == 0.5           # tier 3
    # coverage 행: cluster:{name} — by_cluster 에 들어온 cluster 는 active 키워드 보유
    assert scoring._fit_of(conn, p["id"], "cluster:암호화폐") == 0.65
    assert scoring._fit_of(conn, p["id"], "cluster:없는클러스터") == 0.5         # active 0개
    conn.close()


def _ai_check(conn, prompt_id, run_id, engine="chatgpt", cited=0, doms='["rival.com"]'):
    conn.execute("INSERT INTO ai_checks(prompt_id, run_id, engine, cited, cited_domains_json)"
                 " VALUES(?,?,?,?,?)", (prompt_id, run_id, engine, cited, doms))


def test_ai_gaps_skips_unfinished_runs_and_counts_unmeasured():
    """끊긴 회차가 최신이어도 그 회차에서 안 잰 질문이 빠지지 않는다 — 09-02 #56.

    예전 ai_gaps 는 "ai_checks 의 가장 큰 run_id" 를 최신 회차로 골랐다. 402 로 끊긴
    #56 이 그 자리를 차지하자 거기서 안 닿은 질문이 기회 목록에서 조용히 사라졌고,
    그 뒤 등록된 질문은 한 번도 안 재졌는데 화면 어디에도 안 나왔다. 이제:
      · 질문마다 **끝난 회차**의 최신 측정을 쓴다 (끊긴 회차의 행은 측정이 아니다)
      · 끝난 회차에서 한 번도 안 잰 질문은 "측정 안 됨"으로 센다(인용 0 이 아니다)
      · 끊겼는지는 실물 db.run 이 notes 에 남기는 표식으로 가린다 (사본 금지)
    """
    conn = db.connect()
    p = _project(conn, "ai_unfinished")
    pid = p["id"]
    conn.executemany("INSERT INTO ai_prompts(project_id, prompt) VALUES(?, ?)",
                     [(pid, "옛 질문 가"), (pid, "옛 질문 나"), (pid, "새로 넣은 질문 다")])
    qa, qb, qc = [r[0] for r in conn.execute(
        "SELECT id FROM ai_prompts WHERE project_id=? ORDER BY id", (pid,))]
    with db.run(conn, pid, "ai") as r1:                       # 끝난 회차: 가·나
        for q in (qa, qb):
            _ai_check(conn, q, r1.id)
            _ai_check(conn, q, r1.id, engine="gemini")
    try:                                                      # 끊긴 회차: 가만 묻고 402
        with db.run(conn, pid, "ai") as r2:
            _ai_check(conn, qa, r2.id)
            _ai_check(conn, qc, r2.id)                        # 다는 끊긴 회차에서만 잼
            raise collector.Fatal("OpenRouter 402 Payment Required")
    except collector.Fatal:
        pass
    # 프로세스째 죽은 회차 — finished_at 이 NULL 로 남는다(재배포 SIGKILL)
    r3 = db.start_run(conn, pid, "ai")
    _ai_check(conn, qc, r3, cited=1)
    conn.commit()

    gaps = scoring.ai_gaps(conn, pid)
    assert [g["prompt"] for g in gaps] == ["옛 질문 가", "옛 질문 나"], \
        f"끊긴 회차가 최신이 되어 질문이 빠졌다: {gaps}"
    assert all(g["run_id"] == r1.id and g["checks"] == 2 for g in gaps), gaps
    st = scoring.ai_prompt_state(conn, pid)
    assert (st["active"], st["measured"], st["unmeasured"]) == (3, 2, 1), st
    assert [r["prompt"] for r in st["rows"] if r["state"] == "unmeasured"] == ["새로 넣은 질문 다"]
    assert st["last_run"]["state"] == "open" and st["last_run"]["id"] == r3, st["last_run"]
    db.finish_run(conn, r3, notes=" | 중단: 서버 재시작".strip(" |"))   # 표식이 붙은 채 닫힘
    assert scoring.ai_prompt_state(conn, pid)["last_run"]["state"] == "aborted"
    # 끊긴 회차의 표식은 실물 db.run 이 쓴 그대로여야 한다 — 문자열 사본이 갈라지면 여기서 샌다
    note = conn.execute("SELECT notes FROM runs WHERE id=?", (r2.id,)).fetchone()[0]
    assert scoring.AI_RUN_ABORTED in note, note
    h = scoring.ai_health(conn, pid)
    assert h["unmeasured"] == 1 and h["unmeasured_eg"] == ["새로 넣은 질문 다"], h
    assert h["last_run"]["note"] == "서버 재시작", h["last_run"]

    # 오래됨 — 마지막 측정이 AI_STALE_DAYS 를 넘으면 세되, 기회에서는 빼지 않는다(날짜를 싣는다)
    st2 = scoring.ai_prompt_state(conn, pid, now="2099-01-01T00:00:00Z")
    assert (st2["stale"], st2["measured"], st2["unmeasured"]) == (2, 0, 1), st2
    assert gaps[0]["measured_at"], "근거에 날짜를 붙일 측정 시각이 없다"

    # 판(gen_version) — 표시 전(NULL) 질문 셋 전부 구버전, 사람이 적은 질문(0)은 아니다
    assert st["outdated"] == 3, st
    db.add_ai_prompts(conn, pid, [{"prompt": "사람이 적은 질문"}])
    assert scoring.ai_prompt_state(conn, pid)["outdated"] == 3
    conn.close()


def test_fit_of_question_is_low_when_nothing_on_the_site_overlaps():
    """질문 대상 fit — 사이트의 페이지·키워드와 아무것도 안 겹치면 중립 0.5 가 아니라 낮다.

    자연어 질문은 활성 키워드와 글자째 같을 일이 없어 전부 0.5 에 떨어졌고, 일반 질문
    15건이 38.5점 동점이었다. 겹치는 질문 > 무관한 질문이어야 하고, 간판말("피부과")
    하나 겹친 것은 겹친 것으로 치지 않는다. 키워드 대상의 동작은 그대로다.
    """
    conn = db.connect()
    conn.execute("INSERT INTO projects(name, domain) VALUES('디아더피부과', 'theother.kr')")
    pid = conn.execute("SELECT id FROM projects WHERE name='디아더피부과'").fetchone()[0]
    run = conn.execute("INSERT INTO crawl_runs(project_id, finished_at, seed) "
                       "VALUES(?, '2026-09-01', 'sitemap') RETURNING id", (pid,)).fetchone()[0]
    conn.executemany(
        "INSERT INTO crawl_pages(run_id, url, status, depth, title) VALUES(?,?,200,1,?)",
        [(run, "https://theother.kr/special-clinic/syringoma/", "한관종 치료 | 디아더피부과"),
         (run, "https://theother.kr/special-clinic/milia/", "비립종 제거 | 디아더피부과"),
         (run, "https://theother.kr/signature/ptt/", "PTT 리프팅 | 디아더피부과"),
         (run, "https://theother.kr/acne/", "여드름 흉터 피부과 | 디아더피부과"),
         (run, "https://theother.kr/about/", "강남 피부과 의료진 | 디아더피부과")])
    conn.executemany("INSERT INTO keywords(project_id, keyword, cluster, is_active) "
                     "VALUES(?, ?, ?, 1)",
                     [(pid, "여드름 흉터 치료", "여드름"), (pid, "비립종 제거 비용", None)])
    conn.commit()
    fit = lambda q: scoring._fit_of(conn, pid, q, question=True)   # noqa: E731

    related = fit("한관종 없애려면 어디로 가야 해?")
    unrelated = fit("제주도 맛집 추천해줘")
    generic = fit("제주도 피부과 추천해줘")          # 겹치는 건 간판말 "피부과" 하나뿐
    assert related > unrelated, (related, unrelated)
    assert related == scoring.FIT_Q_PAGE, related
    assert unrelated == scoring.FIT_Q_NONE < 0.5, unrelated
    assert generic == scoring.FIT_Q_NONE, f"간판말 하나로 같은 주제라 쳤다: {generic}"
    assert fit("여드름 흉터 치료 잘하는 곳") == 0.65, "클러스터 이름을 품은 질문"
    assert fit("비립종 제거 비용 보통 얼마야?") == scoring.FIT_Q_KEYWORD, "키워드를 통째로 품은 질문"
    assert fit("PTT 리프팅 효과 있어?") == scoring.FIT_Q_PAGE          # 라틴 낱말 겹침
    assert fit("디아더피부과 후기 어때?") == scoring.FIT_Q_BRAND        # 우리 이름을 부름
    # 키워드 대상은 예전 그대로 — 무관해도 0.5 중립
    assert scoring._fit_of(conn, pid, "제주도 맛집 추천해줘") == 0.5
    # 사이트 어휘가 아예 없으면 판단 근거가 없다 — 무관(0.2)이 아니라 중립
    conn.execute("INSERT INTO projects(name, domain) VALUES('빈사이트', 'empty.kr')")
    eid = conn.execute("SELECT id FROM projects WHERE name='빈사이트'").fetchone()[0]
    assert scoring._fit_of(conn, eid, "제주도 맛집 추천해줘", question=True) == 0.5
    conn.close()


def test_run_is_closed_even_on_crash():
    """수집 도중 예외가 나도 runs.finished_at 이 채워져야 한다.
    try/finally 없이 손으로 finish_run 하던 시절엔 '수집 이력'이 거짓말을 했다."""
    conn = db.connect()
    p = _project(conn, "crash")
    try:
        with db.run(conn, p["id"], "ai") as r:
            r.api_calls = 3
            raise RuntimeError("수집 중 폭발")
    except RuntimeError:
        pass
    row = conn.execute("SELECT finished_at, api_calls, notes FROM runs "
                       "WHERE project_id=? ORDER BY id DESC LIMIT 1", (p["id"],)).fetchone()
    assert row["finished_at"], row
    assert row["api_calls"] == 3, row
    assert "중단" in (row["notes"] or ""), row

    with db.run(conn, p["id"], "gsc") as r:
        r.notes = "정상"
    row = conn.execute("SELECT finished_at, notes FROM runs "
                       "WHERE project_id=? ORDER BY id DESC LIMIT 1", (p["id"],)).fetchone()
    assert row["finished_at"] and row["notes"] == "정상", row
    conn.close()


def test_gsc_snapshot_write_is_idempotent_per_day():
    """같은 날 다시 수집하면 덮어쓴다 (하루 1스냅샷)."""
    conn = db.connect()
    p = _project(conn, "snap")
    db.write_gsc_snapshot(conn, p["id"], "2026-03-01", 28,
                          [("kw", "https://e.com/a", 1, 10, 0.1, 8.0)])
    db.write_gsc_snapshot(conn, p["id"], "2026-03-01", 28,
                          [("kw", None, 2, 20, 0.1, 7.0), ("kw2", None, 0, 5, 0.0, 30.0)])
    rows = conn.execute("SELECT query, page, clicks FROM gsc_snapshots "
                        "WHERE project_id=? ORDER BY query", (p["id"],)).fetchall()
    assert len(rows) == 2, rows
    assert rows[0]["page"] is None and rows[0]["clicks"] == 2, dict(rows[0])
    conn.close()


def test_gsc_daily_upsert_overwrites_same_date():
    """같은 날짜를 다시 넣으면 누적이 아니라 덮어쓰기다.

    GSC 는 최근 2~3일치를 나중에 상향 보정한다 — 매일 도는 수집기가 겹치는 창을
    다시 가져오므로, 누적되면 추이 그래프의 최근 며칠만 계단처럼 뛴다
    (여기가 다른 write_* 와 달리 delete 후 insert 가 아니라 upsert 인 이유).
    """
    conn = db.connect()
    p = _project(conn, "daily")
    db.write_gsc_daily(conn, p["id"], [("2026-05-01", 10, 100, 0.1, 9.0),
                                       ("2026-05-02", 5, 50, 0.1, 8.0)])
    db.write_gsc_daily(conn, p["id"], [("2026-05-02", 7, 70, 0.1, 7.5)])   # 상향 보정
    rows = conn.execute("SELECT date, clicks, impressions, position FROM gsc_daily "
                        "WHERE project_id=? ORDER BY date", (p["id"],)).fetchall()
    assert len(rows) == 2, rows                        # 두 줄로 불어나면 안 된다
    assert rows[1]["clicks"] == 7 and rows[1]["impressions"] == 70, dict(rows[1])
    assert rows[1]["position"] == 7.5, dict(rows[1])
    assert rows[0]["clicks"] == 10, dict(rows[0])      # 안 건드린 날은 그대로
    # 넣은 순서가 뒤죽박죽이어도 화면은 날짜 오름차순으로 받는다 (x축이 곧 이 순서다)
    db.write_gsc_daily(conn, p["id"], [("2026-04-30", 1, 10, 0.1, 12.0)])
    assert [d["date"] for d in scoring.daily_trend(conn, p["id"])] == \
        ["2026-04-30", "2026-05-01", "2026-05-02"]
    conn.close()


def test_gsc_breakdown_rewrite_leaves_no_duplicates():
    """같은 (project, snapshot_date, dim) 재수집은 지우고 다시 넣는다.

    누적되면 device_gap 이 SUM(impressions) 을 두 배로 읽어 없던 격차를 만든다.
    지우는 범위가 dim 하나뿐인 것도 같이 못 박는다 — device 를 다시 받는다고
    country 가 날아가면, 분해를 하나씩 켜고 끄는 순간 조용히 사라진다.
    """
    conn = db.connect()
    p = _project(conn, "bd")
    db.write_gsc_breakdown(conn, p["id"], "2026-05-01", 28, "device",
                           [("MOBILE", "kw", 1, 100, 0.01, 10.0),
                            ("DESKTOP", "kw", 2, 100, 0.02, 8.0)])
    db.write_gsc_breakdown(conn, p["id"], "2026-05-01", 28, "country",
                           [("kor", "kw", 3, 300, 0.01, 9.0)])
    db.write_gsc_breakdown(conn, p["id"], "2026-05-01", 28, "device",
                           [("MOBILE", "kw", 5, 500, 0.01, 11.0)])      # 재수집
    rows = conn.execute("SELECT dim, dim_value, clicks, impressions FROM gsc_breakdown "
                        "WHERE project_id=? ORDER BY dim, dim_value", (p["id"],)).fetchall()
    assert [(r["dim"], r["dim_value"]) for r in rows] == \
        [("country", "kor"), ("device", "MOBILE")], [dict(r) for r in rows]
    assert rows[1]["impressions"] == 500, dict(rows[1])   # 새 값만 남는다 (600 이면 누적)
    assert rows[0]["clicks"] == 3, dict(rows[0])          # 다른 dim 은 살아 있다
    # 수집일이 다르면 별개다 — 어제 것을 지우면 추세 비교가 사라진다
    db.write_gsc_breakdown(conn, p["id"], "2026-05-08", 28, "device",
                           [("MOBILE", "kw", 1, 10, 0.1, 3.0)])
    assert conn.execute("SELECT COUNT(*) FROM gsc_breakdown WHERE project_id=?",
                        (p["id"],)).fetchone()[0] == 3
    conn.close()


def test_index_status_upsert_keeps_one_row_per_url_per_day():
    """URL 배치를 하루에 나눠 도는 게 정상 사용이다 (쿼터가 하루 2,000회).

    그래서 둘째 배치가 첫 배치를 지우면 안 되고(=delete 후 insert 금지),
    같은 URL 을 다시 검사하면 최신 판정으로 덮어야 한다.
    """
    conn = db.connect()
    p = _project(conn, "ixw")
    db.write_index_status(conn, p["id"], "2026-05-01", [
        {"url": "/a", "verdict": "FAIL", "coverage_state": "Not found (404)"}])
    db.write_index_status(conn, p["id"], "2026-05-01", [       # 둘째 배치
        {"url": "/b", "verdict": "PASS", "coverage_state": "Submitted and indexed"}])
    db.write_index_status(conn, p["id"], "2026-05-01", [       # /a 재검사 — 고쳐졌다
        {"url": "/a", "verdict": "PASS", "coverage_state": "Submitted and indexed",
         "last_crawled": "2026-05-01T00:00:00Z"}])
    rows = conn.execute("SELECT url, verdict, coverage_state, last_crawled, robots_txt_state "
                        "FROM gsc_index_status WHERE project_id=? ORDER BY url",
                        (p["id"],)).fetchall()
    assert [r["url"] for r in rows] == ["/a", "/b"], [dict(r) for r in rows]
    assert rows[0]["verdict"] == "PASS" and rows[0]["last_crawled"], dict(rows[0])
    assert rows[0]["robots_txt_state"] is None    # 안 준 필드는 NULL — "모름"과 "없음"은 다르다
    # 날이 바뀌면 새 줄이다 (이력이 남아야 어제와 비교한다)
    db.write_index_status(conn, p["id"], "2026-05-02", [{"url": "/a", "verdict": "FAIL"}])
    assert conn.execute("SELECT COUNT(*) FROM gsc_index_status WHERE project_id=?",
                        (p["id"],)).fetchone()[0] == 3
    conn.close()


def test_device_gap_thresholds_are_inclusive_at_the_boundary():
    """Δ 정확히 2.0, 모바일 노출 정확히 50 은 '걸린다'.

    부등호 하나가 뒤집히면 임계 바로 위 쿼리가 통째로 사라지는데, 화면에서는
    빈 목록이 '문제 없음'과 구별되지 않아 아무도 못 알아챈다.
    """
    conn = db.connect()
    p = _project(conn, "dgap")
    rows = []
    for q, mpos, dpos, mimp in [("딱2.0", 10.0, 8.0, 100),      # Δ=2.0  → 걸린다
                                ("1.9", 9.9, 8.0, 100),        # Δ=1.9  → 아니다
                                ("노출50", 10.0, 8.0, 50),      # 노출 하한 정확히 → 걸린다
                                ("노출49", 10.0, 8.0, 49)]:     # 하나 모자람 → 아니다
        rows.append(("MOBILE", q, 0, mimp, 0.0, mpos))
        rows.append(("DESKTOP", q, 0, 100, 0.0, dpos))
    db.write_gsc_breakdown(conn, p["id"], "2026-05-01", 28, "device", rows)
    out = {r["query"]: r for r in scoring.device_gap(conn, p["id"])}
    assert set(out) == {"딱2.0", "노출50"}, out
    assert out["딱2.0"]["dpos"] == scoring.DEVICE_GAP_POS, out["딱2.0"]
    assert out["노출50"]["mobile_imp"] == scoring.DEVICE_MIN_IMP, out["노출50"]
    conn.close()


def test_index_bucket_priority_folds_to_the_root_cause():
    """원인이 겹칠 때 어느 버킷으로 접히는가 — 순서가 곧 고칠 순서다.

    robots 로 막혀 있으면 fetch 실패는 결과일 뿐이고, canonical 이 엇갈렸는지는
    페이지를 가져올 수 있어야 의미가 있다. 순서가 뒤집히면 사용자가 canonical 을
    만지며 시간을 버린다 (버킷 4종 자체는 scoring._selfcheck 가 따로 본다).
    """
    conn = db.connect()
    p = _project(conn, "ixpri")
    db.write_index_status(conn, p["id"], "2026-05-01", [
        {"url": "/셋다", "verdict": "FAIL", "coverage_state": "Blocked by robots.txt",
         "robots_txt_state": "DISALLOWED", "page_fetch_state": "NOT_FOUND",
         "google_canonical": "/x", "user_canonical": "/셋다"},
        {"url": "/fetch+canon", "verdict": "FAIL", "coverage_state": "Not found (404)",
         "robots_txt_state": "ALLOWED", "page_fetch_state": "NOT_FOUND",
         "google_canonical": "/x", "user_canonical": "/fetch+canon"},
        {"url": "/canon만", "verdict": "PARTIAL", "coverage_state": "Duplicate",
         "robots_txt_state": "ALLOWED", "page_fetch_state": "SUCCESSFUL",
         "google_canonical": "/x", "user_canonical": "/canon만"},
        {"url": "/canon같음", "verdict": "FAIL",
         "coverage_state": "Crawled - currently not indexed",
         "robots_txt_state": "ALLOWED", "page_fetch_state": "SUCCESSFUL",
         "google_canonical": "/canon같음", "user_canonical": "/canon같음"},
        {"url": "/정상", "verdict": "PASS", "coverage_state": "Submitted and indexed",
         "robots_txt_state": "ALLOWED", "page_fetch_state": "SUCCESSFUL"},
    ])
    got = {r["url"]: r["bucket"] for r in scoring.index_issues(conn, p["id"])}
    assert got == {"/셋다": "robots_blocked", "/fetch+canon": "fetch_error",
                   "/canon만": "canonical_mismatch", "/canon같음": "not_indexed"}, got
    conn.close()


def test_creations_table_ships_with_schema():
    """create 스킬이 capture의 Brain 안에 제 테이블을 만들던 탓에
    대시보드가 방어적 try/except를 달고 있었다. 이제 스키마에 있다."""
    conn = db.connect()
    p = _project(conn, "creat")
    db.record_creation(conn, p["id"], "src/page.md", kind="striking_distance",
                       branch="capture/striking_distance-page")
    assert conn.execute("SELECT COUNT(*) FROM creations WHERE project_id=?",
                        (p["id"],)).fetchone()[0] == 1
    conn.close()


def test_rank_snapshot_keeps_aio_none():
    """aio_present/aio_cited=None은 미측정(NULL)이어야 하며 0으로 바뀌면 안 된다."""
    conn = db.connect()
    p = _project(conn, "aio_none")
    conn.execute("INSERT OR IGNORE INTO keywords(project_id, keyword) VALUES(?, 'kw_aio')", (p["id"],))
    conn.commit()
    kw_id = conn.execute("SELECT id FROM keywords WHERE project_id=?", (p["id"],)).fetchone()[0]
    db.write_rank_snapshot(conn, kw_id, 3, "https://e.com/a",
                           serp_features=[], aio_present=None, aio_cited=None)
    row = conn.execute("SELECT position, aio_present, aio_cited FROM rank_snapshots "
                       "WHERE keyword_id=?", (kw_id,)).fetchone()
    assert row["position"] == 3
    assert row["aio_present"] is None, f"expected None, got {row['aio_present']}"
    assert row["aio_cited"] is None, f"expected None, got {row['aio_cited']}"

    # 인용 도메인도 같은 불변식 — 요약이 떴을 때만 목록이 있고, 떴는데 못 뽑은 것([])과
    # 안 떴거나 안 잰 것(NULL)은 다르다. serper 는 늘 [] 를 주므로 여기가 그 둔갑을 막는다.
    def doms(present, given):
        db.write_rank_snapshot(conn, kw_id, 3, None, aio_present=present,
                               aio_cited=0 if present else None, aio_domains=given)
        return conn.execute("SELECT aio_domains_json FROM rank_snapshots WHERE keyword_id=?",
                            (kw_id,)).fetchone()[0]
    assert doms(1, ["a.com", "b.com"]) == '["a.com", "b.com"]'
    assert doms(1, []) == "[]"
    assert doms(None, []) is None, "안 잰 조회가 '봤는데 아무도 없었다'가 됐다"
    assert doms(0, ["a.com"]) is None, "요약이 안 뜬 조회에 인용 목록이 적혔다"
    assert doms(1, None) is None
    conn.close()


def test_serp_questions_keep_the_keyword_and_overwrite_the_day():
    """함께 묻는 질문·연관 검색어는 어느 검색어에서 나왔는지와 함께, 하루 한 벌로 남는다."""
    conn = db.connect()
    p = _project(conn, "serp_q")
    kid = conn.execute("INSERT INTO keywords(project_id, keyword) VALUES(?, 'kw_q') RETURNING id",
                       (p["id"],)).fetchone()[0]
    conn.commit()
    got = lambda: [(r["kind"], r["position"], r["text"]) for r in conn.execute(
        "SELECT kind, position, text FROM serp_questions WHERE keyword_id=? ORDER BY kind, position",
        (kid,))]
    n = db.write_serp_questions(conn, kid, [("paa", " 질문 A "), ("paa", "질문 a"), ("paa", "질문 B"),
                                            ("related", "연관 1"), ("bogus", "x"), ("paa", "")],
                                checked_at="2026-09-01T01:00:00Z")
    assert n == 3 and got() == [("paa", 1, "질문 A"), ("paa", 2, "질문 B"),
                                ("related", 1, "연관 1")], got()
    # 같은 날 다시 재면 덮어쓴다 — 앞 조회의 질문이 새 조회의 사실처럼 남지 않는다
    db.write_serp_questions(conn, kid, [("related", "연관 2")], checked_at="2026-09-01T09:00:00Z")
    assert got() == [("related", 1, "연관 2")], got()
    db.write_serp_questions(conn, kid, [], checked_at="2026-09-01T10:00:00Z")
    assert got() == [], "같은 날 질문이 없는 조회가 앞 조회의 질문을 남겼다"
    # 다른 날은 따로 쌓인다
    db.write_serp_questions(conn, kid, [("paa", "질문 C")], checked_at="2026-09-02T01:00:00Z")
    db.write_serp_questions(conn, kid, [("paa", "질문 D")], checked_at="2026-09-03T01:00:00Z")
    assert len(got()) == 2, got()
    # kind 마다 상한 — 요청문을 목록으로 만들지 않는다
    db.write_serp_questions(conn, kid, [("paa", f"q{i}") for i in range(30)],
                            checked_at="2026-09-03T02:00:00Z")
    assert conn.execute("SELECT COUNT(*) FROM serp_questions WHERE keyword_id=? AND "
                        "date(checked_at)='2026-09-03'", (kid,)).fetchone()[0] == db.SERP_QUESTIONS_KEEP
    conn.close()


def _gsc(conn, pid, date_, days, query, page, clicks, imp, pos):
    """분석 함수 테스트용 — _snap과 달리 page·impressions 를 직접 정한다."""
    conn.execute("""INSERT INTO gsc_snapshots(project_id,snapshot_date,period_days,
                      query,page,clicks,impressions,ctr,position)
                    VALUES(?,?,?,?,?,?,?,0.0,?)""",
                 (pid, date_, days, query, page, clicks, imp, pos))
    conn.commit()


def test_ctr_gaps_detects_low_ctr_and_respects_floors():
    """1페이지인데 기대 CTR 절반 미만인 쿼리만 — 노출 하한·구간 밖은 제외."""
    conn = db.connect()
    p = _project(conn, "ctrgap")
    d = "2026-04-01"
    _gsc(conn, p["id"], d, 28, "저ctr", None, 46, 4200, 3.0)     # 1.1% < 10%×0.5
    _gsc(conn, p["id"], d, 28, "정상ctr", None, 500, 4200, 3.0)  # 11.9% — 문제없음
    _gsc(conn, p["id"], d, 28, "노출부족", None, 0, scoring.CTR_GAP_MIN_IMP - 1, 3.0)
    _gsc(conn, p["id"], d, 28, "2페이지", None, 0, 4200, 15.0)   # 1~10위 밖
    rows = scoring.ctr_gaps(conn, p["id"])
    assert [r["query"] for r in rows] == ["저ctr"], rows
    r = rows[0]
    assert r["position"] == 3.0 and r["impressions"] == 4200
    assert r["expected_ctr"] == scoring.EXPECTED_CTR[3]
    assert r["actual_ctr"] == 1.1, r
    # 손실 클릭 = 노출×(기대-실제) — 반환값끼리 앞뒤가 맞아야 한다
    assert r["lost_clicks"] == round(4200 * (r["expected_ctr"] - 46 * 100.0 / 4200) / 100), r
    conn.close()


def test_cannibalization_detects_split_and_ignores_null_pages():
    """같은 쿼리 2페이지 분산은 잡고, page NULL(구버전 데이터)·독점 쿼리는 무시."""
    conn = db.connect()
    p = _project(conn, "canni")
    d = "2026-04-01"
    _gsc(conn, p["id"], d, 28, "분산", "/a", 3, 60, 4.0)
    _gsc(conn, p["id"], d, 28, "분산", "/b", 1, 40, 7.0)         # 부페이지 40% ≥ 20%
    _gsc(conn, p["id"], d, 28, "널만", None, 5, 500, 5.0)        # 구버전 데이터 — page 없음
    _gsc(conn, p["id"], d, 28, "독점", "/a", 5, 95, 3.0)
    _gsc(conn, p["id"], d, 28, "독점", "/b", 0, 4, 3.0)          # 부페이지 4% — 독점
    out = scoring.cannibalization(conn, p["id"])
    assert [o["query"] for o in out] == ["분산"], out
    assert out[0]["impressions"] == 100
    assert [pg["page"] for pg in out[0]["pages"]] == ["/a", "/b"]  # 노출 내림차순
    assert out[0]["pages"][0]["clicks"] == 3 and out[0]["pages"][0]["position"] == 4.0
    conn.close()


def test_striking_band_and_min_impressions():
    """scoring.md 1절 '노출 유의미' 하한 + band(page1/page2) 라벨."""
    conn = db.connect()
    p = _project(conn, "strike2")
    d = "2026-04-01"
    _gsc(conn, p["id"], d, 28, "페이지1", None, 1, 150, 6.0)
    _gsc(conn, p["id"], d, 28, "페이지2", None, 1, 150, 15.0)
    _gsc(conn, p["id"], d, 28, "노출미달", None, 1, scoring.STRIKING_MIN_IMP - 1, 6.0)
    rows = {r["query"]: r for r in scoring.striking(conn, p["id"], d)}
    assert "노출미달" not in rows, rows
    assert rows["페이지1"]["band"] == "page1"
    assert rows["페이지2"]["band"] == "page2"
    conn.close()


def test_rank_decay_finds_defense_targets():
    """같은 period_days 페어에서 DECAY_POS 이상 하락한 쿼리만."""
    conn = db.connect()
    p = _project(conn, "decay")
    _snap(conn, p["id"], "2026-04-01", 28, "하락", 5.0, 10)
    _snap(conn, p["id"], "2026-04-01", 28, "유지", 5.0, 5)
    _snap(conn, p["id"], "2026-04-08", 28, "하락", 9.0, 2)       # Δpos=-4.0 ≤ -1.5
    _snap(conn, p["id"], "2026-04-08", 28, "유지", 5.4, 5)       # Δpos=-0.4 — 노이즈
    out = scoring.rank_decay(conn, p["id"])
    assert [o["query"] for o in out] == ["하락"], out
    assert out[0]["dpos"] == -4.0 and out[0]["dclk"] == -8, out[0]
    assert out[0]["prev_pos"] == 5.0 and out[0]["pos"] == 9.0
    conn.close()


def test_coverage_reports_uncovered_active_keywords():
    """활성인데 GSC 노출도 순위 체크도 없는 키워드만 미커버로. cluster 집계 포함."""
    conn = db.connect()
    p = _project(conn, "cover")
    _gsc(conn, p["id"], "2026-04-01", 28, "노출있음", None, 1, 30, 5.0)
    for kw, cluster, active in [("노출있음", "a", 1), ("순위있음", None, 1),
                                ("미커버", "a", 1), ("비활성", "a", 0)]:
        conn.execute("""INSERT OR IGNORE INTO keywords(project_id,keyword,cluster,is_active)
                        VALUES(?,?,?,?)""", (p["id"], kw, cluster, active))
    conn.commit()
    kw_id = conn.execute("SELECT id FROM keywords WHERE project_id=? AND keyword='순위있음'",
                         (p["id"],)).fetchone()[0]
    db.write_rank_snapshot(conn, kw_id, 5, "https://e.com/a")
    cov = scoring.coverage(conn, p["id"])
    assert [k["keyword"] for k in cov["keywords"]] == ["미커버"], cov
    assert cov["by_cluster"] == {"a": 1}, cov
    conn.close()


def test_score_is_deterministic():
    """같은 입력 → 같은 출력. 미등록 type은 saas 계수로 폴백."""
    m = {"impressions": 1234, "position": 7.0}
    a = scoring.score("striking_distance", m, "directory")
    assert a == scoring.score("striking_distance", dict(m), "directory")
    assert 0.0 <= a <= 100.0
    assert scoring.score("striking_distance", m, "없는타입") == \
        scoring.score("striking_distance", m, "saas")
    # saas 는 w_ai 최상향 (scoring.md 2절 방향)
    assert scoring.score("ai_citation_gap", {"impressions": 100}, "saas") > \
        scoring.score("ai_citation_gap", {"impressions": 100}, "local_business")


def test_rank_snapshot_same_day_rerun_is_idempotent():
    """같은 키워드를 같은 날 다시 확인하면 덮어쓴다 — gsc 스냅샷과 같은 규약."""
    conn = db.connect()
    p = _project(conn, "rankday")
    conn.execute("INSERT OR IGNORE INTO keywords(project_id, keyword) VALUES(?, 'kw_day')",
                 (p["id"],))
    conn.commit()
    kw_id = conn.execute("SELECT id FROM keywords WHERE project_id=?", (p["id"],)).fetchone()[0]
    db.write_rank_snapshot(conn, kw_id, 8, "https://e.com/a", checked_at="2026-04-01T05:00:00Z")
    db.write_rank_snapshot(conn, kw_id, 6, "https://e.com/a", checked_at="2026-04-01T09:00:00Z")
    db.write_rank_snapshot(conn, kw_id, 7, None, checked_at="2026-04-02T09:00:00Z")
    rows = conn.execute("SELECT position FROM rank_snapshots WHERE keyword_id=? "
                        "ORDER BY checked_at", (kw_id,)).fetchall()
    assert [r["position"] for r in rows] == [6, 7], rows   # 4/1은 마지막 것만 남는다
    conn.close()


def test_answer_excerpt_keeps_full_text():
    """280자 절단 제거 — 답변 전문이 남아야 검증할 수 있다 (상한 8000자)."""
    conn = db.connect()
    p = _project(conn, "excerpt")
    conn.execute("INSERT OR IGNORE INTO ai_prompts(project_id, prompt) VALUES(?, 'q')",
                 (p["id"],))
    conn.commit()
    prompt_id = conn.execute("SELECT id FROM ai_prompts WHERE project_id=?",
                             (p["id"],)).fetchone()[0]
    long_answer = "가" * 1000
    db.record_ai_check(conn, prompt_id, None, "chatgpt", 0, 1, 0, [], long_answer)
    row = conn.execute("SELECT answer_excerpt FROM ai_checks WHERE prompt_id=? "
                       "ORDER BY id DESC LIMIT 1", (prompt_id,)).fetchone()
    assert row["answer_excerpt"] == long_answer, len(row["answer_excerpt"])
    db.record_ai_check(conn, prompt_id, None, "chatgpt", 1, 1, 0, [], "나" * 9000)
    row = conn.execute("SELECT answer_excerpt FROM ai_checks WHERE prompt_id=? "
                       "ORDER BY id DESC LIMIT 1", (prompt_id,)).fetchone()
    assert len(row["answer_excerpt"]) == 8000, len(row["answer_excerpt"])   # 안전핀 상한
    conn.close()


def test_list_opportunities_orders_are_distinct():
    """list_opportunities - triage 정렬(acked 우선)과 screen 정렬(new 우선)의 차이 및 단일화 검증."""
    conn = db.connect()
    p = _project(conn, "opp_orders")
    conn.executemany(
        """INSERT INTO opportunities(project_id, kind, target, score, reasoning, status)
           VALUES(?, ?, ?, ?, 'r', ?)""",
        [(p["id"], "striking_distance", "opp_new_high", 80.0, "new"),
         (p["id"], "ctr_gap", "opp_acked_mid", 50.0, "acked"),
         (p["id"], "cannibalization", "opp_new_low", 20.0, "new"),
         (p["id"], "rank_decay", "opp_done_top", 95.0, "done"),
        ])
    conn.commit()
    # 열린 기회·화면 목록은 심사를 통과한 것만 낸다 — 넷 다 작업으로 판정해 둔다
    db.set_verdicts(conn, p["id"], [scoring.norm(t) for t in
                    ("opp_new_high", "opp_acked_mid", "opp_new_low", "opp_done_top")], "work")

    # screen 정렬: (status='new') DESC, score DESC, id DESC
    screen_rows = db.list_opportunities(conn, p["id"], order="screen", limit=10)
    screen_targets = [r["target"] for r in screen_rows]
    assert screen_targets == ["opp_new_high", "opp_new_low", "opp_done_top", "opp_acked_mid"], screen_targets

    # triage 정렬: status='acked' DESC, score DESC
    triage_rows = db.list_opportunities(conn, p["id"], order="triage", limit=10)
    triage_targets = [r["target"] for r in triage_rows]
    assert triage_targets == ["opp_acked_mid", "opp_done_top", "opp_new_high", "opp_new_low"], triage_targets

    # 두 정렬이 실제로 다르다
    assert screen_targets != triage_targets

    # open_opportunities (status IN ('new', 'acked') + triage 정렬)
    open_rows = db.open_opportunities(conn, p["id"])
    assert [r["target"] for r in open_rows] == ["opp_acked_mid", "opp_new_high", "opp_new_low"]

    # scoring.opportunities 도 db.list_opportunities(order='screen') 정렬과 일치
    scoring_rows = scoring.opportunities(conn, p["id"], limit=10)
    assert [r["target"] for r in scoring_rows] == screen_targets

    # kinds 필터
    filtered_rows = db.list_opportunities(conn, p["id"], kinds=["striking_distance"], order="triage")
    assert [r["target"] for r in filtered_rows] == ["opp_new_high"]

    # with_id=False 칼럼 검증
    no_id_rows = db.list_opportunities(conn, p["id"], order="screen", with_id=False)
    assert "id" not in dict(no_id_rows[0]) and "created" not in dict(no_id_rows[0])
    assert "target" in dict(no_id_rows[0])

    # 유효하지 않은 order 인자는 ValueError
    try:
        db.list_opportunities(conn, p["id"], order="invalid_order")
    except ValueError as e:
        assert "unknown order" in str(e)
    else:
        raise AssertionError("invalid order should raise ValueError")

    conn.close()


def test_get_opportunity():
    """get_opportunity - ID 및 project_id 단건 조회."""
    conn = db.connect()
    p = _project(conn, "get_opp")
    conn.execute(
        """INSERT INTO opportunities(project_id, kind, target, score, reasoning, status)
           VALUES(?, 'striking_distance', 'opp_target', 75.0, 'reason', 'new')""",
        (p["id"],))
    conn.commit()
    opp_id = conn.execute("SELECT id FROM opportunities WHERE project_id=?", (p["id"],)).fetchone()[0]

    opp = db.get_opportunity(conn, opp_id, project_id=p["id"])
    assert opp is not None
    assert opp["id"] == opp_id and opp["target"] == "opp_target" and opp["kind"] == "striking_distance"

    opp_no_pid = db.get_opportunity(conn, opp_id)
    assert opp_no_pid is not None and opp_no_pid["id"] == opp_id

    assert db.get_opportunity(conn, 999999, project_id=p["id"]) is None
    assert db.get_opportunity(conn, opp_id, project_id=p["id"] + 999) is None
    conn.close()


def test_creations_reader_and_merged_writer():
    """list_creations / mark_creation_merged 동작 검증."""
    conn = db.connect()
    p = _project(conn, "creations_test")
    c1 = db.record_creation(conn, p["id"], "path/1.md", branch="capture/k-1")
    c2 = db.record_creation(conn, p["id"], "path/2.md", branch="capture/k-2")

    rows = db.list_creations(conn, p["id"], limit=10)
    assert len(rows) == 2
    assert rows[0]["id"] == c2 and rows[1]["id"] == c1   # ORDER BY id DESC
    assert rows[0]["merged"] == 0 and rows[1]["merged"] == 0

    assert db.mark_creation_merged(conn, c2) == 1
    rows_after = db.list_creations(conn, p["id"], limit=10)
    assert rows_after[0]["merged"] == 1 and rows_after[1]["merged"] == 0
    conn.close()


def test_set_keyword_intent_writer():
    """set_keyword_intent 동작 검증."""
    conn = db.connect()
    p = _project(conn, "intent_writer")
    conn.execute("INSERT INTO keywords(project_id, keyword, is_active) VALUES(?, '단어', 1)", (p["id"],))
    conn.commit()
    kid = conn.execute("SELECT id FROM keywords WHERE project_id=?", (p["id"],)).fetchone()[0]

    assert db.set_keyword_intent(conn, kid, "commercial") == 1
    row = conn.execute("SELECT intent FROM keywords WHERE id=?", (kid,)).fetchone()
    assert row["intent"] == "commercial"

def test_rank_delta_noise_boundary_and_is_defensive():
    """scoring.rank_delta - 노이즈 경계 양쪽(RANK_NOISE-1 보합 vs RANK_NOISE 움직임) 및 is_defensive 검증."""
    # 1. rank_delta
    assert scoring.rank_delta(None, 5) == {"delta": None, "flat": False}
    assert scoring.rank_delta(5, None) == {"delta": None, "flat": False}
    assert scoring.rank_delta(None, None) == {"delta": None, "flat": False}

    assert scoring.rank_delta(10, 10) == {"delta": 0, "flat": True}

    # 노이즈 미만 (|Δ| < RANK_NOISE=3) -> flat=True (보합)
    assert scoring.rank_delta(10, 8) == {"delta": 2, "flat": True}     # +2: RANK_NOISE 바로 아래 (보합)
    assert scoring.rank_delta(8, 10) == {"delta": -2, "flat": True}    # -2: RANK_NOISE 바로 아래 (보합)
    assert scoring.rank_delta(10, 9) == {"delta": 1, "flat": True}     # +1 (보합)
    assert scoring.rank_delta(9, 10) == {"delta": -1, "flat": True}    # -1 (보합)

    # 노이즈 이상 (|Δ| >= RANK_NOISE=3) -> flat=False (움직임)
    assert scoring.rank_delta(10, 7) == {"delta": 3, "flat": False}    # +3: RANK_NOISE 정확히 (움직임)
    assert scoring.rank_delta(7, 10) == {"delta": -3, "flat": False}   # -3: -RANK_NOISE 정확히 (움직임)
    assert scoring.rank_delta(10, 6) == {"delta": 4, "flat": False}    # +4 (움직임)
    assert scoring.rank_delta(6, 10) == {"delta": -4, "flat": False}   # -4 (움직임)

    # 2. is_defensive
    assert scoring.is_defensive("rank_decay") is True
    assert scoring.is_defensive("cannibalization") is True
    assert scoring.is_defensive("striking_distance") is False
    assert scoring.is_defensive("ctr_gap") is False
    assert scoring.is_defensive("coverage") is False
    assert scoring.is_defensive("pseo_pattern") is False
    assert scoring.is_defensive("device_gap") is False
    assert scoring.is_defensive("index_blocked") is False
    assert scoring.is_defensive("") is False
    assert scoring.is_defensive(None) is False


def test_gap_to_page1_clamps_inside_page1():
    """scoring.gap_to_page1 - 1페이지(1~10위) 안이면 0.0 클램프, 밖이면 소수점 1자리 거리."""
    assert scoring.gap_to_page1(1.0) == 0.0
    assert scoring.gap_to_page1(5.0) == 0.0
    assert scoring.gap_to_page1(10.0) == 0.0
    assert scoring.gap_to_page1(9.9) == 0.0
    assert scoring.gap_to_page1(10.1) == 0.1
    assert scoring.gap_to_page1(14.2) == 4.2
    assert scoring.gap_to_page1(20.0) == 10.0
    assert scoring.gap_to_page1(None) == 0.0


def test_dynamic_capture_home_resolution():
    """CAPTURE_HOME 환경변수 변경 시 db.CAPTURE_HOME 및 db.capture_home()이 즉시 새 경로를 반환한다."""
    orig = os.environ.get("CAPTURE_HOME")
    temp_dir = tempfile.mkdtemp(prefix="seo-miner-dynamic-test-")
    try:
        os.environ["CAPTURE_HOME"] = temp_dir
        assert str(db.capture_home()) == temp_dir
        assert str(db.CAPTURE_HOME) == temp_dir
        assert Path(temp_dir) == db.CAPTURE_HOME
        assert Path(temp_dir) == db.capture_home()
        assert Path(temp_dir) / "brain.db" == db.db_path()
        assert Path(temp_dir) / "brain.db" == db.DB_PATH
    finally:
        if orig is not None:
            os.environ["CAPTURE_HOME"] = orig
        else:
            os.environ.pop("CAPTURE_HOME", None)
        import shutil
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_verdicts_write_read_and_irrelevant_deactivates_keyword():
    """검색어 심사(verdicts) — 변형이 같은 키로 접히고, 무관만 키워드 추적을 끈다."""
    conn = db.connect()
    pid = _project(conn, "vd")["id"]
    conn.execute("INSERT INTO keywords(project_id,keyword,is_active) VALUES(?,?,1)",
                 (pid, "디아더피부과 가격"))
    conn.commit()
    k = scoring.norm("디아 더 피부과 가격")           # 변형도 같은 키
    assert db.set_verdicts(conn, pid, [k], "hold") == 1
    assert db.verdict_map(conn, pid) == {k: "hold"}
    active = lambda: conn.execute("SELECT is_active FROM keywords WHERE project_id=?", (pid,)).fetchone()[0]  # noqa: E731
    assert active() == 1, "보류는 측정을 계속한다"
    db.set_verdicts(conn, pid, [k], "irrelevant")
    assert active() == 0, "무관은 키워드 추적을 끈다"
    assert db.set_verdicts(conn, pid, [k], None) == 1
    assert db.verdict_map(conn, pid) == {}
    try:
        db.set_verdicts(conn, pid, [k], "maybe")
        assert False, "잘못된 판정을 받았다"
    except ValueError:
        pass
    # SQL 안에서도 같은 정규화를 쓴다 — 조회가 JS/파이썬 사본 없이 조인한다
    assert conn.execute("SELECT norm('디아 더 피부과 가격')").fetchone()[0] == k
    assert set(scoring.KEYWORD_KINDS) < set(scoring.ALL_KINDS)
    conn.close()


def test_verdict_gates_load_and_open_list():
    """무관·보류 판정은 적재에서 빠지고, 열린 기회 조회는 작업 판정만 낸다.
    검색어가 아닌 종류(index_blocked 등)는 판정 없이 통과한다."""
    conn = db.connect()
    pid = _project(conn, "vg")["id"]
    rows = [{"kind": "striking_distance", "target": "jenni ai 후기", "score": 30},
            {"kind": "pseo_pattern", "target": "jenni  ai 후기", "score": 20},   # 변형
            {"kind": "striking_distance", "target": "ecrett", "score": 25},
            {"kind": "index_blocked", "target": "https://e.com/x", "score": 10}]  # 통과 종류
    db.set_verdicts(conn, pid, [scoring.norm("jenni ai 후기")], "hold")
    db.set_verdicts(conn, pid, [scoring.norm("ecrett")], "work")
    kept = scoring.gate_rows(conn, pid, rows)
    assert {r["target"] for r in kept} == {"ecrett", "https://e.com/x"}, kept
    db.upsert_opportunities(conn, pid, None, rows)          # 직접 넣어도 조회가 거른다
    got = {r["target"] for r in db.open_opportunities(conn, pid, limit=50)}
    assert got == {"ecrett", "https://e.com/x"}, got
    allrows = {r["target"] for r in db.list_opportunities(conn, pid, order="screen", limit=50)}
    assert len(allrows) == 4, "gated=False 기본은 그대로 전부다"
    db.set_verdicts(conn, pid, [scoring.norm("ecrett")], None)   # 미판정 = 안 보인다
    assert "ecrett" not in {r["target"] for r in db.open_opportunities(conn, pid, limit=50)}
    conn.close()


def test_status_at_and_watch_rows():
    """완료 후 관찰 — 상태를 늘리지 않고 status_at 과 스냅샷 전·후로 만든다."""
    conn = db.connect()
    pid = _project(conn, "wt")["id"]
    db.upsert_opportunities(conn, pid, None, [{"kind": "striking_distance", "target": "q1", "score": 30}])
    oid = conn.execute("SELECT id FROM opportunities WHERE project_id=?", (pid,)).fetchone()[0]
    _snap(conn, pid, "2026-01-01", 28, "q1", 12.0, 3)
    db.set_opportunity_status(conn, oid, "done")
    assert conn.execute("SELECT status_at FROM opportunities WHERE id=?", (oid,)).fetchone()[0], "status_at 이 안 찍혔다"
    conn.execute("UPDATE opportunities SET status_at='2026-01-02 00:00:00' WHERE id=?", (oid,))
    _snap(conn, pid, "2026-01-10", 28, "q1", 12.0, 3)
    _snap(conn, pid, "2026-01-20", 28, "q1", 13.0, 2)
    conn.commit()
    [w] = db.watch_rows(conn, pid)
    assert w["id"] == oid and w["done_at"].startswith("2026-01-02")
    assert w["before"]["position"] == 12.0 and w["after"]["position"] == 13.0, w
    assert w["runs_since"] == 2 and w["stalled"] is True, w
    conn.execute("UPDATE gsc_snapshots SET position=8.0 WHERE project_id=? AND snapshot_date='2026-01-20'", (pid,))
    conn.commit()
    assert db.watch_rows(conn, pid)[0]["stalled"] is False
    conn.close()


# ── 기회 수명주기 (scoring.resolve_stale · db.resolve_opportunities) ─────────────
# 기준 시각: 기회는 전부 8/10 에 만들어진 것으로 둔다. 그 뒤(8/20~)의 데이터만 "새" 데이터다.
_BASE = "2026-08-10 00:00:00"


def _opps_at_base(conn, pid, rows, status="new"):
    """기회를 넣고 만든 시각을 기준 시각으로 못 박는다. {target: id}."""
    db.upsert_opportunities(conn, pid, None, rows)
    for r in rows:
        conn.execute("UPDATE opportunities SET created_at=?, status=? WHERE project_id=? "
                     "AND kind=? AND target=?", (_BASE, status, pid, r["kind"], r["target"]))
    conn.commit()
    return {r["target"]: r["id"] for r in conn.execute(
        "SELECT id, target FROM opportunities WHERE project_id=?", (pid,))}


def _states(conn, pid):
    """(kind, target) → 행. 같은 검색어가 다른 종류로 새로 잡혀도 섞이지 않게."""
    return {(r["kind"], r["target"]): dict(r) for r in conn.execute(
        "SELECT kind, target, status, status_reason, status_prev, status_at, run_id"
        " FROM opportunities WHERE project_id=?", (pid,))}


def test_resolve_kinds_are_all_decided():
    """종류마다 자동 해소 규칙이 있거나, 뺀 이유가 적혀 있다 — 새 종류가 생기면
    여기서 멈춰 결정을 요구한다(말없이 '안 닫힘'으로 떨어지지 않게)."""
    have, skip = set(scoring._RESOLVERS), set(scoring._NO_RESOLVE)
    assert not (have & skip), have & skip
    assert have | skip == set(scoring.ALL_KINDS), set(scoring.ALL_KINDS) ^ (have | skip)
    conn = db.connect()
    try:
        db.set_opportunity_status(conn, 1, db.OPP_RESOLVED)
        raise AssertionError("저절로 풀림을 사람이 누를 수 있다")
    except ValueError:
        pass
    finally:
        conn.close()


def test_resolve_stale_leaves_open_without_confirming_data():
    """가장 중요한 불변식 — 이번 회차에 **다시 안 나왔어도** 새 데이터가 풀렸다고
    긍정하지 않으면 열린 채로 남는다. '안 나오면 닫기'로 되돌리면 여기가 깨진다.

    여기 깐 기회는 전부 이번 load() 가 다시 내지 않는다(검출 조건에 안 걸리게 깔았다)
    — 그래야 '다시 나와서 열려 있는' 것과 '안 나왔는데 열려 있는' 것이 갈린다."""
    conn = db.connect()
    pid = _project(conn, "lc-open")["id"]
    kw = lambda k: conn.execute("INSERT INTO keywords(project_id, keyword, is_active) "
                                "VALUES(?,?,1) RETURNING id", (pid, k)).fetchone()[0]
    rs = lambda kid, at, pres, cited: conn.execute(
        "INSERT INTO rank_snapshots(keyword_id, checked_at, position, aio_present, aio_cited)"
        " VALUES(?,?,7,?,?)", (kid, at, pres, cited))
    # AI 요약: 옛 데이터만 인용 / 새 데이터가 미측정(NULL) / 새 데이터에 요약 자체가 없음
    rs(kw("aio옛것"), "2026-08-05T00:00:00Z", 1, 1)
    k2 = kw("aio미측정"); rs(k2, "2026-08-05T00:00:00Z", 1, 0); rs(k2, "2026-08-20T00:00:00Z", 1, None)
    rs(kw("aio사라짐"), "2026-08-20T00:00:00Z", 0, 0)
    # AI 인용: 인용이 찍힌 회차가 도는 중 / 예외로 끊김 / 새 회차가 이 질문을 안 잼
    pr = lambda q: conn.execute("INSERT INTO ai_prompts(project_id, prompt) VALUES(?,?) "
                                "RETURNING id", (pid, q)).fetchone()[0]
    chk = lambda p, run, cited, at=None: conn.execute(
        "INSERT INTO ai_checks(prompt_id, run_id, engine, cited, checked_at) "
        "VALUES(?,?,'chatgpt',?,COALESCE(?,CURRENT_TIMESTAMP))", (p, run, cited, at))
    pa, pb, pc = pr("도는중 질문"), pr("끊긴 질문"), pr("안 잰 질문")
    chk(pc, db.start_run(conn, pid, "ai"), 0, "2026-08-01 00:00:00")
    run_b = db.start_run(conn, pid, "ai")
    db.finish_run(conn, run_b, notes="engines=[] | 중단: KeyboardInterrupt: ")
    chk(pb, run_b, 1)
    chk(pa, db.start_run(conn, pid, "ai"), 1)          # finished_at 없음 = 도는 중(가장 최근 회차)
    # GSC: 20위 밖으로 밀림(풀림 아님) · 새 스냅샷에 줄 없음 · 규칙 없는 종류(rank_decay)
    _snap(conn, pid, "2026-08-20", 28, "밀려난 검색어", 25.0, 0)
    _snap(conn, pid, "2026-08-20", 28, "되찾은 검색어", 2.0, 30)
    _snap(conn, pid, "2026-08-20", 28, "1페이지 밖 클릭", 15.0, 20)   # 클릭률은 좋지만 1페이지 밖
    # 기기: 모바일만 잡힘(비교 불가) / 백링크: 우리도 받는다는 기록이 기준 시각 전의 것뿐
    conn.execute("INSERT INTO gsc_breakdown(project_id, snapshot_date, period_days, dim, dim_value,"
                 " query, clicks, impressions, ctr, position) VALUES(?, '2026-08-20', 28, 'device',"
                 " 'MOBILE', '모바일만', 5, 100, 0, 5.0)", (pid,))
    conn.execute("INSERT INTO link_intersect(project_id, checked_date, domain, hits, we_have)"
                 " VALUES(?, '2026-08-05', 'old.com', 2, 1)", (pid,))
    # 색인: 색인됨 기록이 기준 시각 전의 것뿐 / 크롤: robots.txt 가 오류 페이지(규칙 없음)
    conn.execute("INSERT INTO gsc_index_status(project_id, checked_date, url, verdict, coverage_state)"
                 " VALUES(?, '2026-08-05', '/still', 'PASS', 'Submitted and indexed')", (pid,))
    conn.execute("INSERT INTO crawl_runs(project_id, started_at, finished_at, seed, robots_txt)"
                 " VALUES(?, '2026-08-20T00:00:00Z', '2026-08-20T01:00:00Z', 'home',"
                 " '<html>not found</html>')", (pid,))
    conn.commit()
    k = lambda kind, t: {"kind": kind, "target": t, "score": 10}
    _opps_at_base(conn, pid, [k("aio_exposure", "aio옛것"), k("aio_exposure", "aio미측정"),
                              k("aio_exposure", "aio사라짐"),
                              k("ai_citation_gap", "도는중 질문"), k("ai_citation_gap", "끊긴 질문"),
                              k("ai_citation_gap", "안 잰 질문"),
                              k("striking_distance", "밀려난 검색어"),
                              k("striking_distance", "스냅샷에 없는 검색어"),
                              k("rank_decay", "되찾은 검색어"), k("ctr_gap", "1페이지 밖 클릭"),
                              k("device_gap", "모바일만"), k("backlink_prospect", "old.com"),
                              k("content_gap", "경쟁사 수집 실패"),
                              k("index_blocked", "/still"), k("ai_bot_blocked", "OAI-SearchBot")])
    conn.execute("UPDATE opportunities SET status='acked' WHERE project_id=? AND target='끊긴 질문'", (pid,))
    conn.commit()
    before = _states(conn, pid)
    conn.close()

    scoring.load("lc-open")

    conn = db.connect()
    after = _states(conn, pid)
    conn.close()
    rerun = {key for key in before if after[key]["run_id"] is not None}
    assert not rerun, f"검사 전제가 깨졌다 — 이번 load() 가 다시 낸 기회: {rerun}"
    changed = {key: (before[key]["status"], after[key]["status"], after[key]["status_reason"])
               for key in before
               if (before[key]["status"], before[key]["status_reason"])
               != (after[key]["status"], after[key]["status_reason"])}
    assert not changed, f"확인 데이터 없이 닫혔다: {changed}"


def test_resolve_stale_closes_on_positive_confirmation():
    """새 데이터가 조건이 풀렸다고 긍정하면 닫는다 — 작업 기록이 없으면 resolved(이전
    상태를 기억), 있으면 done(완료 시각 = 작업 기록 시각). 완료 후 관찰은 done 만 잡고
    resolved 는 뺀다. 조건이 다시 잡히면 resolved 는 이전 상태로 되열린다."""
    conn = db.connect()
    pid = _project(conn, "lc-close")["id"]
    kid = conn.execute("INSERT INTO keywords(project_id, keyword, is_active) "
                       "VALUES(?, 'aio인용', 1) RETURNING id", (pid,)).fetchone()[0]
    conn.execute("INSERT INTO rank_snapshots(keyword_id, checked_at, position, aio_present, aio_cited)"
                 " VALUES(?, '2026-08-20T00:00:00Z', 9, 1, 1)", (kid,))
    pq = conn.execute("INSERT INTO ai_prompts(project_id, prompt) VALUES(?, '인용된 질문') "
                      "RETURNING id", (pid,)).fetchone()[0]
    with db.run(conn, pid, "ai") as r:
        conn.execute("INSERT INTO ai_checks(prompt_id, run_id, engine, cited) VALUES(?,?,'chatgpt',0)", (pq, r.id))
        conn.execute("INSERT INTO ai_checks(prompt_id, run_id, engine, cited) VALUES(?,?,'gemini',1)", (pq, r.id))
    _snap(conn, pid, "2026-08-01", 28, "올라간 검색어", 6.0, 3)
    conn.execute("""INSERT INTO gsc_snapshots(project_id,snapshot_date,period_days,query,page,
                      clicks,impressions,ctr,position) VALUES
                    (?, '2026-08-20', 28, '올라간 검색어', NULL, 40, 200, 0, 2.5),
                    (?, '2026-08-20', 28, '클릭 회복', NULL, 10, 200, 0, 5.0)""", (pid, pid))
    conn.executemany(
        "INSERT INTO gsc_breakdown(project_id, snapshot_date, period_days, dim, dim_value, query,"
        " clicks, impressions, ctr, position) VALUES(?, '2026-08-20', 28, 'device', ?, '모바일 회복',"
        " 5, 100, 0, ?)", [(pid, "MOBILE", 5.0), (pid, "DESKTOP", 4.0)])
    conn.execute("INSERT INTO gsc_index_status(project_id, checked_date, url, verdict, coverage_state)"
                 " VALUES(?, '2026-08-20', '/fixed', 'PASS', 'Submitted and indexed')", (pid,))
    conn.execute("INSERT INTO link_intersect(project_id, checked_date, domain, hits, we_have)"
                 " VALUES(?, '2026-08-20', 'got.com', 2, 1)", (pid,))
    conn.execute("INSERT INTO crawl_runs(project_id, started_at, finished_at, seed, robots_txt)"
                 " VALUES(?, '2026-08-20T00:00:00Z', '2026-08-20T01:00:00Z', 'home', ?)",
                 (pid, "User-agent: GPTBot\nDisallow: /\n\nUser-agent: *\nAllow: /"))
    conn.commit()
    k = lambda kind, t: {"kind": kind, "target": t, "score": 10}
    # GPTBot 은 용도를 가르기 전 판정이 올려 둔 옛 기회다 — 여전히 막혀 있지만 학습
    # 전용이라 닫혀야 한다(안 그러면 막힌 채라 영영 안 닫힌다)
    ids = _opps_at_base(conn, pid, [
        k("aio_exposure", "aio인용"), k("ai_citation_gap", "인용된 질문"),
        k("striking_distance", "올라간 검색어"), k("ctr_gap", "클릭 회복"),
        k("device_gap", "모바일 회복"), k("index_blocked", "/fixed"),
        k("backlink_prospect", "got.com"), k("ai_bot_blocked", "OAI-SearchBot"),
        k("ai_bot_blocked", "GPTBot")])
    conn.execute("UPDATE opportunities SET status='acked' WHERE id=?", (ids["인용된 질문"],))
    cid = db.record_creation(conn, pid, "a.md", opportunity_id=ids["올라간 검색어"])
    work_at = conn.execute("SELECT created_at FROM creations WHERE id=?", (cid,)).fetchone()[0]
    conn.close()

    scoring.load("lc-close")

    conn = db.connect()
    # 이번 load() 가 새로 낸 기회(예: '클릭 회복' 이 5위라 striking_distance 로도 잡힌다)는 빼고
    # 기준 시각에 깔아 둔 것만 본다
    st = {t: r for (kind, t), r in _states(conn, pid).items() if ids.get(t) and r["run_id"] is None}
    assert set(st) == set(ids), set(ids) ^ set(st)
    resolved = {t for t, r in st.items() if r["status"] == db.OPP_RESOLVED}
    assert resolved == {"aio인용", "인용된 질문", "클릭 회복", "모바일 회복", "/fixed",
                        "got.com", "OAI-SearchBot", "GPTBot"}, st
    assert all(st[t]["status_reason"] and st[t]["status_at"] for t in resolved), st
    assert "더는" in st["OAI-SearchBot"]["status_reason"], st["OAI-SearchBot"]
    assert "학습 전용" in st["GPTBot"]["status_reason"], st["GPTBot"]
    assert "인용" in st["aio인용"]["status_reason"], st["aio인용"]
    assert st["인용된 질문"]["status_prev"] == "acked" and st["aio인용"]["status_prev"] == "new", st
    done = st["올라간 검색어"]
    assert done["status"] == "done" and done["status_at"] == work_at, done   # 완료 시각 = 작업 시각
    # 완료 후 관찰은 우리 효과만 — done 은 잡고 resolved 는 뺀다
    watched = {w["target"] for w in db.watch_rows(conn, pid)}
    assert watched == {"올라간 검색어"}, watched
    # 조건이 다시 잡혔다 — resolved 는 이전 상태로 되열리고 사유가 지워진다
    conn.execute("INSERT INTO rank_snapshots(keyword_id, checked_at, position, aio_present, aio_cited)"
                 " VALUES(?, '2026-08-21T00:00:00Z', 9, 1, 0)", (kid,))
    with db.run(conn, pid, "ai") as r:
        conn.execute("INSERT INTO ai_checks(prompt_id, run_id, engine, cited) VALUES(?,?,'chatgpt',0)", (pq, r.id))
    conn.commit()
    conn.close()

    scoring.load("lc-close")

    conn = db.connect()
    again = {r["target"]: (r["status"], r["status_reason"]) for r in conn.execute(
        "SELECT target, status, status_reason FROM opportunities WHERE project_id=? AND id IN "
        f"({','.join('?' * len(ids))})", (pid, *ids.values()))}
    conn.close()
    assert again["aio인용"] == ("new", None), again["aio인용"]
    assert again["인용된 질문"] == ("acked", None), again["인용된 질문"]     # 진행 중이던 것은 진행 중으로
    assert again["올라간 검색어"][0] == "done", "사람 쪽 완료(done)를 되열었다"
    assert again["/fixed"][0] == db.OPP_RESOLVED


def _ai_row(conn, pid_, run_id, engine, cited, doms, answer, rec=None, mentioned=0):
    conn.execute("INSERT INTO ai_checks(prompt_id, run_id, engine, mentioned, cited,"
                 " cited_domains_json, answer_excerpt, recommended) VALUES(?,?,?,?,?,?,?,?)",
                 (pid_, run_id, engine, mentioned, cited, json.dumps(doms), answer, rec))


def test_ai_citation_rate_rivals_and_prescription_split():
    """챗봇 인용 — 비율·표본 수로 가르고, 대신 인용된 곳은 전 표본으로 한 번만 센다.

    · 1/6 근접 질문도 기회로 서고 근거에 `인용 1/6 (n=6)` 이 적힌다(예전엔 0회만 섰다)
    · n<3 은 서되 "표본 부족"이라고 말한다. 3/6 은 기회가 아니다
    · 대신 인용된 곳은 도메인별 횟수(reddit.com 4/5)고, 발췌는 엔진마다 빠진 답 중
      먼저 받은 것 — 예전 화면은 MAX(문자열)로 표본 하나를 사실상 무작위로 골랐다
    · 요청문(gather 의 ai_gap_rows)과 기회 근거가 같은 집계를 읽는다
    · 제3자 플랫폼이 대부분이면 presence, 경쟁사가 대부분이면 예전 꼴
    """
    import brief
    conn = db.connect()
    p = _project(conn, "ai_rate")
    pid = p["id"]
    conn.executemany("INSERT INTO ai_prompts(project_id, prompt, category) VALUES(?,?,?)",
                     [(pid, "근접 추천 질문", "추천"), (pid, "경쟁사 질문", "문제해결"),
                      (pid, "잘 걸리는 질문", "브랜드"), (pid, "표본 적은 질문", "추천")])
    qa, qb, qc, qd = [r[0] for r in conn.execute(
        "SELECT id FROM ai_prompts WHERE project_id=? ORDER BY id", (pid,))]
    with db.run(conn, pid, "ai") as r:
        # 근접: 6건 중 1건 인용. 빠진 5건 중 reddit 4, rival 2. chatgpt 의 빠진 답은
        # "b…"가 먼저, "z…"가 나중 — 문자열 MAX 면 z 를, 결정적 발췌면 b 를 고른다.
        _ai_row(conn, qa, r.id, "chatgpt", 1, ["e.com"], "a 인용된 답", rec=1, mentioned=1)
        _ai_row(conn, qa, r.id, "chatgpt", 0, ["reddit.com", "rival.com"], "b 첫 빠진 답", rec=0,
                mentioned=1)
        _ai_row(conn, qa, r.id, "chatgpt", 0, ["www.reddit.com"], "z 나중 빠진 답", rec=None)
        _ai_row(conn, qa, r.id, "perplexity", 0, ["reddit.com"], "p 첫 답", rec=0)
        _ai_row(conn, qa, r.id, "perplexity", 0, ["old.reddit.com", "rival.com"], "p 둘째", rec=0)
        _ai_row(conn, qa, r.id, "perplexity", 0, [], "p 셋째", rec=0)
        for i in range(3):
            _ai_row(conn, qb, r.id, "chatgpt", 0, ["rival.com"], f"경쟁 {i}")
        for i in range(6):
            _ai_row(conn, qc, r.id, "chatgpt", int(i < 3), ["x.com"], f"잘 {i}")
        for i in range(2):
            _ai_row(conn, qd, r.id, "chatgpt", 0, ["ko.wikipedia.org"], f"적은 {i}")
    conn.commit()

    t = scoring.ai_tally(conn, r.id)[qa]
    assert (t["checks"], t["cited"], t["misses"]) == (6, 1, 5), t
    # 하위 도메인(www.·old.)은 reddit.com 으로 접힌다 — 답변 하나에 한 번씩
    assert t["rivals"][0] == {"domain": "reddit.com", "n": 4, "third_party": True}, t["rivals"]
    assert {"domain": "rival.com", "n": 2, "third_party": False} in t["rivals"], t["rivals"]
    assert t["lean"] == "third_party" and t["third_share"] > scoring.AI_PRESENCE_SHARE, t
    assert t["excerpts"] == {"chatgpt": "b 첫 빠진 답", "perplexity": "p 첫 답"}, t["excerpts"]
    # 추천 목록 — NULL(안 봤다)은 분모에서 빠진다. 0 과 뭉치지 않는다
    assert (t["recommended"], t["rec_checks"]) == (1, 5), t
    assert t["by_engine"]["chatgpt"]["rec_checks"] == 2, t["by_engine"]
    assert scoring.ai_tally(conn, r.id)[qb]["recommended"] is None       # 한 건도 안 잰 질문
    assert scoring.ai_rivals_text(t["rivals"], t["misses"]) == "reddit.com 4/5, rival.com 2/5"

    gaps = {g["prompt"]: g for g in scoring.ai_gaps(conn, pid)}
    assert set(gaps) == {"근접 추천 질문", "경쟁사 질문", "표본 적은 질문"}, set(gaps)   # 3/6 은 아님
    assert gaps["근접 추천 질문"]["rivals"] == t["rivals"], "기회와 집계가 다른 표를 봤다"
    assert gaps["표본 적은 질문"]["thin"] and not gaps["근접 추천 질문"]["thin"]
    conn.close()

    scoring.load("ai_rate")
    conn = db.connect()
    why = {row[0]: row[1] for row in conn.execute(
        "SELECT target, reasoning FROM opportunities WHERE project_id=? AND kind='ai_citation_gap'",
        (pid,))}
    assert "인용 1/6 (n=6)" in why["근접 추천 질문"], why
    assert "reddit.com 4/5" in why["근접 추천 질문"], why
    assert "제3자 플랫폼" in why["근접 추천 질문"], why
    assert "표본 부족" in why["표본 적은 질문"], why
    assert "잘 걸리는 질문" not in why

    # 질문도 검색어 종류라 심사('작업')를 거쳐야 화면·요청문에 오른다(db.gate_sql)
    db.set_verdicts(conn, pid, [scoring.norm(q) for q in why], "work")
    d = dashboard.gather(conn, db.get_project(conn, "ai_rate"))
    conn.close()
    opps = {o["target"]: o for o in d["opps"] if o["kind"] == "ai_citation_gap"}
    # 요청문이 읽는 행은 기회를 세운 집계 그대로다(두 벌 아님)
    gr = {g["prompt"]: g for g in d["ai_gap_rows"]}
    assert gr["근접 추천 질문"]["rivals"] == t["rivals"]
    near = opps["근접 추천 질문"]
    assert near["gap_kind"] == "third_party" and near["brief"]["shape"] == "presence", near
    body = near["brief"]["body"]
    assert "인용 1/6 (n=6)" in body and "| reddit.com | 4/5 | 제3자 플랫폼 |" in body, body
    assert "| 엔진 | 인용 | 이름만 | 표본 | 대신 인용된 곳 |" in body, body
    assert "| chatgpt | 1/3 |" in body, body
    assert "추천 목록 1/5" in body and "휴리스틱" in body, body
    assert "- chatgpt:\n  > b 첫 빠진 답" in body and "z 나중" not in body, body
    rival = opps["경쟁사 질문"]
    assert rival["gap_kind"] == "sites" and rival["brief"]["shape"] == "new_content", rival
    assert "가시성 사다리" not in rival["brief"]["body"]          # 추천·비교 질문만
    thin = opps["표본 적은 질문"]
    assert thin["gap_kind"] == "third_party"                     # ko.wikipedia.org → wikipedia.org
    assert "표본 부족" in thin["brief"]["body"], thin["brief"]["body"]
    assert "presence" in brief.SHAPE_NAMES


def test_ai_recommended_column_migrates_as_null():
    """옛 Brain 의 ai_checks 에 recommended 칸이 생기고, 옛 행은 NULL(안 봤다)이다 — 0 이 아니다."""
    import sqlite3
    # 따로 선 메모리 Brain — 공용 임시 Brain 의 표를 갈아 끼우면 뒤 검사가 옛 표를 본다
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    # 옛 Brain 흉내: recommended 칸이 없던 때의 ai_checks
    conn.executescript("""DROP TABLE ai_checks;
        CREATE TABLE ai_checks (id INTEGER PRIMARY KEY, prompt_id INTEGER, run_id INTEGER,
          engine TEXT, sample_idx INTEGER, mentioned INTEGER DEFAULT 0,
          cited INTEGER DEFAULT 0, cited_domains_json TEXT, answer_excerpt TEXT);""")
    conn.execute("INSERT INTO projects(id, name, domain) VALUES(1, 'ai_mig', 'e.com')")
    conn.execute("INSERT INTO ai_prompts(project_id, prompt) VALUES(1, 'q')")
    qid = conn.execute("SELECT id FROM ai_prompts").fetchone()[0]
    conn.execute("INSERT INTO ai_checks(prompt_id, engine, mentioned, cited) VALUES(?, 'chatgpt', 1, 0)",
                 (qid,))
    conn.commit()
    db._migrate(conn)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(ai_checks)")}
    assert "recommended" in cols
    assert conn.execute("SELECT recommended FROM ai_checks WHERE prompt_id=?",
                        (qid,)).fetchone()[0] is None
    db.record_ai_check(conn, qid, None, "chatgpt", 1, 1, 0, [], "1. q 추천", recommended=1)
    db.record_ai_check(conn, qid, None, "chatgpt", 2, 1, 0, [], "문단")          # 안 넘기면 NULL
    got = [r[0] for r in conn.execute(
        "SELECT recommended FROM ai_checks WHERE prompt_id=? ORDER BY id", (qid,))]
    assert got == [None, 1, None], got
    conn.close()


def test_retire_auto_serp_cleans_only_what_it_made():
    """옛 'auto_serp' 경쟁사를 한 번 걷는다 — 그 경쟁사에서 나온 것만, 사람 손이 닿은 것은 남긴다.

    2026-09 호스팅: 순위 수집이 "검색어 3개 이상에서 상위 10위" 로 네이버 블로그·유튜브
    ·앱스토어를 경쟁사로 넣었고(291개, manual 0), 갭 분석이 그 앞 5개로 돈을 썼다.
    """
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    db._register_norm(conn)
    x = conn.execute
    x("INSERT INTO projects(id, name, domain) VALUES(1, 'all_auto', 'a.com'), (2, 'mixed', 'b.com')")
    x("""INSERT INTO competitors(project_id, domain, source) VALUES
         (1, 'm.blog.naver.com', 'auto_serp'), (1, 'play.google.com', 'auto_serp'),
         (2, 'youtube.com', 'auto_serp'), (2, 'hand.com', 'manual'), (2, 'lab.com', 'auto_labs')""")
    gap = "INSERT INTO keyword_gap(project_id, checked_date, keyword, domain, kind) VALUES(?,?,?,?,?)"
    for row in ((1, "2026-09-08", "플랫폼만", "m.blog.naver.com", "missing"),
                (2, "2026-09-08", "플랫폼만2", "youtube.com", "weak"),
                (2, "2026-09-08", "섞임", "youtube.com", "missing"),
                (2, "2026-09-08", "섞임", "hand.com", "missing")):
        x(gap, row)
    opp = "INSERT INTO opportunities(project_id, kind, target, score, status) VALUES(?,?,?,50,?)"
    for row in ((1, "content_gap", "플랫폼만", "new"), (2, "content_gap", "플랫폼만2", "acked"),
                (2, "content_gap", "섞임", "new")):        # 사람이 고른 경쟁사 근거도 있다 — 남는다
        x(opp, row)
    met = "INSERT INTO competitor_metrics(project_id, checked_date, domain, is_self, etv) VALUES(?,?,?,?,1)"
    for row in ((1, "2026-09-08", "a.com", 1), (1, "2026-09-08", "play.google.com", 0),
                (2, "2026-09-08", "b.com", 1), (2, "2026-09-08", "youtube.com", 0),
                (2, "2026-09-08", "hand.com", 0)):
        x(met, row)
    cand = "INSERT INTO keywords(project_id, keyword, source, is_active) VALUES(?,?,?,?)"
    for row in ((1, "갭 후보", "competitor_gap", 0), (1, "심사한 후보", "competitor_gap", 0),
                (1, "켠 후보", "competitor_gap", 1), (1, "시드", "seed", 0),
                (2, "섞인 사이트 후보", "competitor_gap", 0)):
        x(cand, row)
    db.set_verdicts(conn, 1, [scoring.norm("심사한 후보")], "hold")
    conn.commit()

    db._migrate(conn)

    comp = {(r[0], r[1]): r[2] for r in x("SELECT project_id, domain, source FROM competitors")}
    assert comp == {(2, "hand.com"): "manual", (2, "lab.com"): "auto_labs"}, comp
    st = {(r[0], r[1]): (r[2], r[3], r[4]) for r in x(
        "SELECT project_id, target, status, status_prev, status_reason FROM opportunities")}
    assert st[(1, "플랫폼만")][:2] == ("resolved", "new") and "경쟁사가 아니었습니다" in st[(1, "플랫폼만")][2], st
    assert st[(2, "플랫폼만2")][:2] == ("resolved", "acked"), st
    assert st[(2, "섞임")][0] == "new", st
    assert [tuple(r) for r in x("SELECT project_id, keyword, domain FROM keyword_gap")] \
        == [(2, "섞임", "hand.com")]
    assert sorted(tuple(r) for r in x("SELECT project_id, domain FROM competitor_metrics")) \
        == [(2, "b.com"), (2, "hand.com")], "상대가 다 빠진 날의 우리 줄은 같이 빠진다"
    kws = {(r[0], r[1]) for r in x("SELECT project_id, keyword FROM keywords")}
    assert kws == {(1, "심사한 후보"), (1, "켠 후보"), (1, "시드"), (2, "섞인 사이트 후보")}, kws

    # 한 번만 — 새 규칙이 'auto_rank' 로 다시 채운 것은 다음 연결이 건드리지 않는다
    x("INSERT INTO competitors(project_id, domain, source) VALUES(1, 'rival.com', 'auto_rank')")
    x("INSERT INTO keyword_gap(project_id, checked_date, keyword, domain, kind)"
      " VALUES(1, '2026-09-12', '새 갭', 'rival.com', 'missing')")
    conn.commit()
    db._migrate(conn)
    assert x("SELECT COUNT(*) FROM competitors WHERE project_id=1").fetchone()[0] == 1
    assert x("SELECT COUNT(*) FROM keyword_gap WHERE project_id=1").fetchone()[0] == 1
    conn.close()


# ── 한 페이지에 두 의도 (intent_split) ──────────────────────────────────────
# 실측(brain.db 3사이트·292페이지)에서 나온 규칙이다: 2위 의도가 기본값(정보)인 경우는
# 거의 다 "같은 의도인데 분류기가 못 가른 것"이었다 — `papular scar` 와 `papular scar
# treatment` 는 한 페이지가 맞다. 1·2위가 **둘 다 명시 의도**일 때만 진짜 갈림이었고,
# 그 규칙이 292페이지에서 정확히 한 건(syringoma-milia: 비교 202 · 해결 30)을
# 남겼다. 그 한 건이 이 검사의 픽스처다.

def test_intent_split_needs_two_named_intents():
    """1·2위가 둘 다 명시 의도일 때만 선다 — 2위가 기본값(정보)이면 같은 페이지다."""
    conn = db.connect()
    p = _project(conn, "isplit1")
    d = "2026-04-01"
    # 진짜 갈림: 비교 202 · 해결 30
    _gsc(conn, p["id"], d, 28, "syringoma vs milia", "/x/a", 5, 118, 3.0)
    _gsc(conn, p["id"], d, 28, "milia vs syringoma", "/x/a", 2, 84, 4.0)
    _gsc(conn, p["id"], d, 28, "milia removal seoul", "/x/a", 1, 25, 8.0)
    _gsc(conn, p["id"], d, 28, "syringoma removal", "/x/a", 0, 5, 9.0)
    # 가짜 갈림: 해결 160 · 정보 150 — 분류 실패일 뿐 같은 주제의 머리말이다.
    # 두 묶음 다 검색어 2개·노출 하한을 넘겨 둔다 — 다른 하한이 먼저 걸러내면
    # 이 검사는 "명시 의도 둘" 규칙을 안 보게 된다(실제로 그래서 헛돌았다).
    _gsc(conn, p["id"], d, 28, "papular scar treatment", "/x/b", 3, 120, 5.0)
    _gsc(conn, p["id"], d, 28, "papular acne scar treatment", "/x/b", 1, 40, 5.0)
    _gsc(conn, p["id"], d, 28, "papular scar", "/x/b", 2, 100, 6.0)
    _gsc(conn, p["id"], d, 28, "papular scars", "/x/b", 1, 50, 6.0)
    out = scoring.intent_split(conn, p["id"])
    assert [r["page"] for r in out] == ["/x/a"], out
    r = out[0]
    assert (r["primary"], r["primary_impressions"]) == (scoring.query_intent("syringoma vs milia"), 202), r
    assert (r["secondary"], r["secondary_impressions"]) == (scoring.query_intent("syringoma removal"), 30), r
    assert [q["query"] for q in r["secondary_queries"]] == ["milia removal seoul", "syringoma removal"]
    assert r["impressions"] == 232, r
    conn.close()


def test_intent_split_floors_and_home_exclusion():
    """하한 미달·홈(루트·언어 루트)은 안 선다 — 홈은 원래 의도가 섞인다."""
    conn = db.connect()
    p = _project(conn, "isplit2")
    d = "2026-04-01"
    # 홈·언어 루트 — 하한은 전부 넘지만 홈이라서 안 선다(홈은 원래 의도가 섞인다)
    for home in ("https://e.com/", "https://e.com/en/"):
        _gsc(conn, p["id"], d, 28, f"a vs b {home}", home, 5, 200, 3.0)
        _gsc(conn, p["id"], d, 28, f"c removal {home}", home, 1, 40, 8.0)
        _gsc(conn, p["id"], d, 28, f"c price {home}", home, 1, 40, 9.0)
    # 2위 의도 노출 미달
    _gsc(conn, p["id"], d, 28, "low vs x", "/y/low", 5, 200, 3.0)
    _gsc(conn, p["id"], d, 28, "low removal", "/y/low", 0, scoring.SPLIT_MIN_SECOND_IMP - 2, 9.0)
    _gsc(conn, p["id"], d, 28, "low price", "/y/low", 0, 1, 9.0)
    # 2위 의도 검색어 1개뿐 — 오분류 한 건과 구분이 안 된다
    _gsc(conn, p["id"], d, 28, "one vs x", "/y/one", 5, 200, 3.0)
    _gsc(conn, p["id"], d, 28, "one removal", "/y/one", 0, 90, 9.0)
    # 페이지 전체 노출 미달 — 2위 의도는 하한을 넘는다(가르는 것이 전체 하한뿐이게)
    _gsc(conn, p["id"], d, 28, "tiny vs x", "/y/tiny", 0, 25, 3.0)
    _gsc(conn, p["id"], d, 28, "tiny removal", "/y/tiny", 0, scoring.SPLIT_MIN_SECOND_IMP - 10, 9.0)
    _gsc(conn, p["id"], d, 28, "tiny cost", "/y/tiny", 0, 10, 9.0)
    assert scoring.intent_split(conn, p["id"]) == []
    conn.close()


def test_intent_split_page_is_the_one_that_ranks_first():
    """검색어가 여러 페이지에 걸리면 노출 1등 페이지 몫으로만 센다 — _page_queries 와 같은 규칙."""
    conn = db.connect()
    p = _project(conn, "isplit3")
    d = "2026-04-01"
    _gsc(conn, p["id"], d, 28, "k vs j", "/z/main", 5, 200, 3.0)
    _gsc(conn, p["id"], d, 28, "k cost", "/z/main", 1, 40, 8.0)
    _gsc(conn, p["id"], d, 28, "k price", "/z/main", 1, 40, 8.0)
    # 같은 검색어가 딴 페이지에도 걸리지만 노출이 적다 — 그 페이지 몫으로 세면 안 된다
    _gsc(conn, p["id"], d, 28, "k cost", "/z/other", 0, 3, 30.0)
    _gsc(conn, p["id"], d, 28, "k price", "/z/other", 0, 3, 30.0)
    _gsc(conn, p["id"], d, 28, "k vs j", "/z/other", 0, 3, 30.0)
    out = scoring.intent_split(conn, p["id"])
    assert [r["page"] for r in out] == ["/z/main"], out
    assert out[0]["secondary_impressions"] == 80, out[0]
    conn.close()


def test_page_advice_catches_a_split_page_name():
    """URL 이 말하는 것과 title·H1 이 말하는 것이 다르면 짚는다.

    실물: /autologous-exosome-therapy/ 인데 title·H1 은 Autologous Cell Regeneration
    (NOVASTEM) 이었다. 들어오는 앵커 70/71 도 'Autologous Exosome Therapy' 였다.
    페이지가 무엇인지에 대한 신호가 서로 부딪히는데 — 순위가 낮은 유력한 원인인데 —
    진단표에 그 줄이 없었다(meta description·외부 링크·이미지 넷만 있었다).
    """
    a = {"url": "https://e.com/en/special-clinic/autologous-exosome-therapy/",
         "title": "Autologous Cell Regeneration | The Other Dermatology, Seoul",
         "h1_json": '["Autologous Cell Regeneration (NOVASTEM)"]',
         "meta_description": "설명 문장을 직접 쓴 것입니다. 무엇에 답하는지 적었습니다.",
         "words": 1303, "js_shell": 0, "status": 200}
    adv = {x["tag"]: x for x in scoring.page_advice(a, ["autologous cell regeneration"],
                                                    domain="e.com")}
    assert scoring.NAME_SPLIT_TAG in adv, sorted(adv)
    x = adv[scoring.NAME_SPLIT_TAG]
    assert "exosome" in x["now"] and "autologous-exosome-therapy" in x["now"], x
    assert "title" in x["fix"] and "URL" in x["fix"], x

    # 주소와 제목이 같은 것을 말하면 안 짚는다
    ok = {**a, "title": "Autologous Exosome Therapy | The Other Dermatology, Seoul",
          "h1_json": '["Autologous Exosome Therapy"]'}
    assert scoring.NAME_SPLIT_TAG not in {y["tag"] for y in scoring.page_advice(ok, [], domain="e.com")}
    # 겹치는 낱말이 하나도 없으면 '갈렸다'가 아니라 다른 얘기다 — 짚지 않는다
    far = {**a, "title": "Thermage FLX | The Other Dermatology", "h1_json": '["Thermage FLX"]'}
    assert scoring.NAME_SPLIT_TAG not in {y["tag"] for y in scoring.page_advice(far, [], domain="e.com")}
    # 슬러그가 한 낱말이면 근거가 약하다 — 짚지 않는다
    thin = {**a, "url": "https://e.com/en/exosome/"}
    assert scoring.NAME_SPLIT_TAG not in {y["tag"] for y in scoring.page_advice(thin, [], domain="e.com")}


def test_serp_outlines_are_stored_per_url_and_reused():
    """상위 글의 H2 목록은 주소 단위로 한 벌 남긴다.

    요청문은 "상위 2~3개의 제목과 H2 목록을 여기에 붙이면" 이라며 사람에게 붙여 넣기를
    시켰다 — 제목은 이미 serp_results 에 있는데도, H2 는 아무도 안 모았기 때문이다.
    검색어 단위로 모으면 같은 경쟁 페이지를 검색어 수만큼 다시 가져온다(한 도메인이
    여러 검색어에서 상위에 선다) — 그래서 주소 단위다.
    """
    conn = db.connect()
    p = _project(conn, "outline")
    db.write_serp_outline(conn, "https://rival.com/a", status=200,
                          title="Rival A", h2=["비용", "과정"], words=900)
    db.write_serp_outline(conn, "https://rival.com/b", status=403, title=None, h2=[], words=None)
    got = db.serp_outlines(conn, ["https://rival.com/a", "https://rival.com/b",
                                   "https://rival.com/none"])
    assert set(got) == {"https://rival.com/a", "https://rival.com/b"}, sorted(got)
    assert got["https://rival.com/a"]["h2"] == ["비용", "과정"], got
    assert got["https://rival.com/a"]["title"] == "Rival A"
    # 가져오기 실패도 남긴다 — 남기지 않으면 다음 런이 또 두드린다
    assert got["https://rival.com/b"]["status"] == 403 and got["https://rival.com/b"]["h2"] == []
    # 다시 쓰면 덮어쓴다(주소 하나에 한 줄)
    db.write_serp_outline(conn, "https://rival.com/a", status=200, title="Rival A2",
                          h2=["비용"], words=950)
    again = db.serp_outlines(conn, ["https://rival.com/a"])
    assert again["https://rival.com/a"]["title"] == "Rival A2"
    assert len(conn.execute("SELECT * FROM serp_outlines").fetchall()) == 2
    # 오래된 것만 다시 가져온다
    stale = db.serp_outlines_stale(conn, ["https://rival.com/a", "https://rival.com/new"],
                                    days=30)
    assert stale == ["https://rival.com/new"], stale
    conn.close()


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)} passed  ({HOME})")

