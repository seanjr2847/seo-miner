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
    """scoring.load() 가 KINDS 명부 14종을 전부 실제로 낸다 — end-to-end.

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
         (pid, "2026-08-14", 28, "겹치는키워드", "https://e.com/b", 1, 40, 7.0)])

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

    run_id = conn.execute(
        "INSERT INTO crawl_runs(project_id, finished_at, seed) VALUES(?, '2026-08-18T00:00:00Z', "
        "'sitemap') RETURNING id", (pid,)).fetchone()[0]
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
    conn.close()
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
    박아야 한다 — 0.5 중립만 두면 w_fit 가 큰 local_clinic 같은 프리셋에서
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
        scoring.score("ai_citation_gap", {"impressions": 100}, "local_clinic")


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


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)} passed  ({HOME})")

