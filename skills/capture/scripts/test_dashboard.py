#!/usr/bin/env python3
"""자체점검 — `python test_dashboard.py` (임시 폴더에서만 돈다, 진짜 Brain은 안 건드림).

수집기의 `_selfcheck()` 는 **자기 표에 잘 넣었나**까지만 본다. 그 다음 이음매 —
넣은 것이 **화면까지 오는가** — 는 아무도 안 보고 있었다. 실제로 이 리포가 반복해서
겪은 버그가 거기 산다(적재는 멀쩡한데 화면이 비어 있음).

여기서 보는 것:
  · gather(): 백링크 5축·경쟁 2축·크롤 1축이 페이로드에 실리는가, 정렬·필터가 맞는가
  · crawl_compare(): 직전 회차 대비 신규/해결이 정확히 갈리는가 (크롤 축의 전부다)
  · scoring.score(): 검색량이 수요에 실제로 반영되는가, 없으면 예전 그대로인가
  · scoring.coverage(): 클러스터별 검색량 합이 나오는가
  · db.list_keywords(): 지표 컬럼이 실리고, 노출이 같으면 검색량 큰 순인가
"""
import os
import sys
import tempfile
from pathlib import Path

HOME = Path(tempfile.mkdtemp(prefix="seo-miner-dash-test-"))
os.environ["CAPTURE_HOME"] = str(HOME)
sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "setup" / "scripts"))

import dashboard  # noqa: E402
import db         # noqa: E402
import scoring    # noqa: E402

D = "2026-08-28"
PREV = "2026-08-14"


def _serve():
    """라이브 대시보드와 같은 Handler 를 임의 포트에 띄운다 — 프록시는 디스패치
    자리에 있어서 함수 호출로는 안 지나간다(test_render.serve 와 같은 꼴)."""
    import threading
    from http.server import ThreadingHTTPServer
    ThreadingHTTPServer.allow_reuse_address = True
    srv = ThreadingHTTPServer(("127.0.0.1", 0), dashboard.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _brain(name="t"):
    """빈 Brain + 사이트 하나. 테스트마다 새 프로젝트를 쓴다(행이 서로 안 섞이게)."""
    conn = db.connect()
    conn.execute("INSERT INTO projects(name, domain, locale, type)"
                 " VALUES(?,?,?,'saas')", (name, f"{name}.example", "ko-KR"))
    conn.commit()
    return conn, db.get_project(conn, name)["id"]


# ── 백링크 ────────────────────────────────────────────────────────────────
def test_gather_backlinks_axes():
    conn, pid = _brain("bl")
    conn.execute("INSERT INTO backlink_summary(project_id,checked_date,rank,backlinks,"
                 "referring_domains,broken_backlinks,dofollow,nofollow)"
                 " VALUES(?,?,412,1840,214,17,1512,328)", (pid, D))
    conn.execute("INSERT INTO backlink_summary(project_id,checked_date,backlinks,"
                 "referring_domains) VALUES(?,?,1620,197)", (pid, PREV))
    conn.executemany(
        "INSERT INTO referring_domains(project_id,checked_date,domain,rank,backlinks)"
        " VALUES(?,?,?,?,?)",
        [(pid, D, "a.kr", 700, 9), (pid, D, "b.kr", None, 3), (pid, D, "c.kr", 800, 1)])
    conn.executemany(
        "INSERT INTO backlinks(project_id,checked_date,url_from,url_to,anchor,rank,"
        "dofollow,is_broken) VALUES(?,?,?,?,?,?,?,?)",
        [(pid, D, "https://a.kr/1", "https://t/x", "좋은 앵커", 700, 1, 0),
         (pid, D, "https://b.kr/2", "https://t/gone", "여기", 100, 1, 1)])
    conn.execute("INSERT INTO backlink_anchors(project_id,checked_date,anchor,backlinks,"
                 "referring_domains) VALUES(?,?,'브랜드',12,4)", (pid, D))
    conn.executemany(
        "INSERT INTO link_intersect(project_id,checked_date,domain,rank,hits,targets,we_have)"
        " VALUES(?,?,?,?,?,?,?)",
        [(pid, D, "want.kr", 810, 3, "r1,r2,r3", 0),
         (pid, D, "a.kr", 700, 2, "r1,r2", 1)])
    conn.commit()

    d = dashboard.gather(conn, db.get_project(conn, "bl"))
    assert d["bl_date"] == D, d["bl_date"]
    assert d["bl_summary"]["referring_domains"] == 214

    # 지수 높은 순 — NULL 은 뒤로 (NULLS LAST 가 실제로 먹는지)
    assert [r["domain"] for r in d["bl_domains"]] == ["c.kr", "a.kr", "b.kr"], \
        [r["domain"] for r in d["bl_domains"]]

    # 깨진 링크가 먼저 — 되찾을 수 있는 것이라 목록 위로 와야 한다
    assert d["bl_links"][0]["is_broken"] == 1, d["bl_links"]

    assert d["bl_anchors"][0]["anchor"] == "브랜드"

    # 이미 우리도 받고 있는 곳(we_have=1)은 제안이 아니다 — 화면에 올리지 않는다
    assert [r["domain"] for r in d["bl_intersect"]] == ["want.kr"], d["bl_intersect"]

    # 추이는 오래된 것부터 — 화면이 기울기를 그린다
    assert [r["d"] for r in d["bl_trend"]] == [PREV, D], d["bl_trend"]
    conn.close()


def test_gather_backlinks_empty_is_not_zero():
    """수집한 적이 없으면 0 이 아니라 '없음'이다 — 지어내지 않는다."""
    conn, _ = _brain("bl0")
    d = dashboard.gather(conn, db.get_project(conn, "bl0"))
    assert d["bl_date"] is None and d["bl_summary"] == {}
    assert d["bl_links"] == [] and d["bl_intersect"] == []
    conn.close()


# ── 경쟁 분석 ─────────────────────────────────────────────────────────────
def test_gather_competitor_share_is_computed_not_stored():
    """몫은 저장하지 않는다 — 분모(전체 etv)가 바뀌면 낡기 때문이다."""
    conn, pid = _brain("cp")
    conn.executemany(
        "INSERT INTO competitor_metrics(project_id,checked_date,domain,is_self,keywords,etv,top10)"
        " VALUES(?,?,?,?,?,?,?)",
        [(pid, D, "rival.com", 0, 400, 700.0, 55),
         (pid, D, "cp.example", 1, 100, 300.0, 10)])
    conn.commit()
    d = dashboard.gather(conn, db.get_project(conn, "cp"))

    # etv 큰 순
    assert [r["domain"] for r in d["comp_metrics"]] == ["rival.com", "cp.example"]
    assert d["comp_metrics"][0]["share"] == 0.7, d["comp_metrics"][0]
    assert d["comp_metrics"][1]["share"] == 0.3
    assert sum(r["share"] for r in d["comp_metrics"]) == 1.0

    cols = {r["name"] for r in conn.execute("PRAGMA table_info(competitor_metrics)")}
    assert "share" not in cols, "몫이 표에 저장되면 분모가 바뀔 때 낡는다"
    conn.close()


def test_gather_competitor_share_survives_zero_etv():
    """etv 가 전부 0/NULL 이어도 0으로 나누지 않는다."""
    conn, pid = _brain("cp0")
    conn.executemany(
        "INSERT INTO competitor_metrics(project_id,checked_date,domain,is_self,keywords,etv)"
        " VALUES(?,?,?,?,?,?)",
        [(pid, D, "a.com", 0, 5, None), (pid, D, "cp0.example", 1, 3, 0)])
    conn.commit()
    d = dashboard.gather(conn, db.get_project(conn, "cp0"))
    assert all(r["share"] is None for r in d["comp_metrics"]), d["comp_metrics"]
    conn.close()


def test_gather_keyword_gap_counts_and_order():
    conn, pid = _brain("gp")
    conn.executemany(
        "INSERT INTO keyword_gap(project_id,checked_date,keyword,domain,position,"
        "our_position,volume,kind) VALUES(?,?,?,?,?,?,?,?)",
        [(pid, D, "큰 것", "r.com", 3, None, 2400, "missing"),
         (pid, D, "작은 것", "r.com", 4, None, 320, "missing"),
         (pid, D, "밀림", "r.com", 1, 14, 1900, "weak"),
         (pid, D, "우위", "r.com", 6, 3, 880, "shared")])
    conn.commit()
    d = dashboard.gather(conn, db.get_project(conn, "gp"))
    assert d["kw_gap_counts"] == {"missing": 2, "weak": 1, "shared": 1}, d["kw_gap_counts"]
    # 검색량 큰 순 — 무엇부터 쓸지가 이 목록의 용도다
    assert [r["volume"] for r in d["kw_gap"]] == [2400, 1900, 880, 320]
    conn.close()


# ── 기회 라벨·처방 — 밴드/갈래로 갈리는 자리가 v1.38.2 버그(6.4위에 "11~20위"가
#    붙음)가 났던 곳이다. gather() 가 원본 행(striking/kw_gap)과 대상 문자열로
#    한 번만 짝지어 label·play 를 붙인다 — 여기서 그 짝짓기를 검증한다.
def test_gather_resolves_striking_band_and_content_gap_kind():
    conn, pid = _brain("lp")
    conn.executemany(
        "INSERT INTO gsc_snapshots(project_id,snapshot_date,period_days,query,clicks,"
        "impressions,ctr,position) VALUES(?,?,?,?,?,?,0.0,?)",
        [(pid, D, 28, "1페이지상단권", 5, 200, 6.0),      # band=page1 (4~10위)
         (pid, D, 28, "2페이지권", 2, 150, 15.0),          # band=page2 (11~20위)
         (pid, D, 28, "_meta", 0, 1, 50.0)])
    conn.executemany(
        "INSERT INTO keyword_gap(project_id,checked_date,keyword,domain,position,"
        "our_position,volume,kind) VALUES(?,?,?,?,?,?,?,?)",
        [(pid, D, "약한글", "r.com", 2, 9, 500, "weak"),
         (pid, D, "없는글", "r.com", 3, None, 400, "missing")])
    conn.executemany(
        "INSERT INTO opportunities(project_id,kind,target,score,reasoning,status,created_at)"
        " VALUES(?,?,?,?,?,?,?)",
        [(pid, "striking_distance", "1페이지상단권", 80, "r", "new", D),
         (pid, "striking_distance", "2페이지권", 70, "r", "new", D),
         (pid, "striking_distance", "안잡히는검색어", 10, "r", "new", D),  # 원본 행이 없다
         (pid, "content_gap", "약한글", 60, "r", "new", D),
         (pid, "content_gap", "  없는글  ", 50, "r", "new", D),   # 앞뒤 공백 — 정규화 확인
         (pid, "rank_decay", "아무거나", 40, "r", "new", D)])
    conn.commit()
    # 화면 목록은 심사(작업 판정)를 통과한 것만 낸다
    db.set_verdicts(conn, pid, [scoring.norm(t) for t in
                    ("1페이지상단권", "2페이지권", "안잡히는검색어", "약한글", "없는글", "아무거나")], "work")
    d = dashboard.gather(conn, db.get_project(conn, "lp"))
    by_target = {o["target"]: o for o in d["opps"]}

    p1 = by_target["1페이지상단권"]
    assert p1["label"] == "1페이지 상단 가능", p1["label"]
    assert p1["play"]["what"] == "이미 1페이지 안입니다. 여기서 남은 것은 순위가 아니라 클릭입니다."
    assert p1["is_defensive"] is False

    p2 = by_target["2페이지권"]
    assert p2["label"] == "1페이지 진입 가능", p2["label"]
    assert p2["play"]["what"].startswith("1페이지 진입까지")

    # 밴드를 모르면(원본 striking 행이 없다) 통칭 라벨·page2 기본 처방으로 물러선다
    unknown = by_target["안잡히는검색어"]
    assert unknown["label"] == "밀면 오를 검색어", unknown["label"]
    assert unknown["play"]["what"].startswith("1페이지 진입까지")

    weak = by_target["약한글"]
    assert weak["play"]["what"].startswith("경쟁 도메인이 나보다 위에")

    missing = by_target["  없는글  "]
    assert missing["play"]["what"].startswith("경쟁 도메인은 잡고 있는데")

    decay = by_target["아무거나"]
    assert decay["label"] == "순위 하락"
    assert decay["is_defensive"] is True
    conn.close()


# ── 사이트 크롤 ───────────────────────────────────────────────────────────
def _run(conn, pid, finished, issues):
    rid = conn.execute("INSERT INTO crawl_runs(project_id,finished_at,seed,pages,issues)"
                       " VALUES(?,?,'sitemap',10,?)",
                       (pid, finished, len(issues))).lastrowid
    conn.executemany("INSERT INTO crawl_issues(run_id,kind,severity,url,detail)"
                     " VALUES(?,?,?,?,?)", [(rid, *i) for i in issues])
    conn.commit()
    return rid


def test_crawl_compare_splits_new_and_fixed():
    """회차 비교가 크롤 축의 전부다 — 목록이 아니라 '지난번 대비 새로 깨진 것'."""
    conn, pid = _brain("cr")
    r1 = _run(conn, pid, "2026-08-21", [
        ("dup_title", "warn", "/a", "같은 제목 3개"),
        ("http_error", "bad", "/old", "404")])
    r2 = _run(conn, pid, D, [
        ("dup_title", "warn", "/a", "같은 제목 5개"),      # detail 만 바뀜 = 같은 이슈
        ("broken_internal", "bad", "/new", "404")])

    cmp_ = dashboard.crawl_compare(conn, pid, r2)
    assert cmp_["prev_run_id"] == r1
    assert cmp_["new"] == [{"kind": "broken_internal", "url": "/new"}], cmp_["new"]
    assert cmp_["fixed"] == [{"kind": "http_error", "url": "/old"}], cmp_["fixed"]
    conn.close()


def test_crawl_compare_first_run_has_no_baseline():
    """첫 바퀴는 기준선이다 — 전부 '새로 생김'이라고 말하면 소음이 된다."""
    conn, pid = _brain("cr1")
    r1 = _run(conn, pid, D, [("orphan", "warn", "/x", "링크 0")])
    cmp_ = dashboard.crawl_compare(conn, pid, r1)
    assert cmp_ == {"prev_run_id": None, "new": [], "fixed": []}, cmp_
    conn.close()


def test_crawl_compare_ignores_unfinished_runs():
    """끝나지 않은 회차를 기준으로 삼으면 '다 해결됐다'고 거짓말한다."""
    conn, pid = _brain("cr2")
    _run(conn, pid, "2026-08-21", [("http_error", "bad", "/old", "404")])
    conn.execute("INSERT INTO crawl_runs(project_id,finished_at,seed) VALUES(?,NULL,'home')",
                 (pid,))                       # 돌다 만 회차
    r3 = _run(conn, pid, D, [("http_error", "bad", "/old", "404")])
    conn.commit()
    cmp_ = dashboard.crawl_compare(conn, pid, r3)
    assert cmp_["new"] == [] and cmp_["fixed"] == [], cmp_
    conn.close()


def test_gather_crawl_uses_latest_finished_run():
    conn, pid = _brain("cr3")
    _run(conn, pid, "2026-08-21", [("http_error", "bad", "/old", "404")])
    _run(conn, pid, D, [("orphan", "warn", "/x", "링크 0"),
                        ("orphan", "warn", "/y", "링크 0")])
    conn.execute("INSERT INTO crawl_runs(project_id,finished_at,seed) VALUES(?,NULL,'home')",
                 (pid,))                       # 지금 돌고 있는 것은 화면에 안 올린다
    conn.commit()
    d = dashboard.gather(conn, db.get_project(conn, "cr3"))
    assert d["crawl"]["run"]["finished_at"] == D
    assert d["crawl"]["counts"] == {"orphan": 2}, d["crawl"]["counts"]
    assert d["crawl"]["compare"]["fixed"] == [{"kind": "http_error", "url": "/old"}]
    # 심각도 순 — bad 가 먼저
    conn.close()


def test_gather_crawl_orders_by_severity():
    conn, pid = _brain("cr4")
    _run(conn, pid, D, [("img_no_alt", "info", "/a", ""),
                        ("dup_title", "warn", "/b", ""),
                        ("http_error", "bad", "/c", "500")])
    d = dashboard.gather(conn, db.get_project(conn, "cr4"))
    assert [i["severity"] for i in d["crawl"]["issues"]] == ["bad", "warn", "info"], \
        [i["severity"] for i in d["crawl"]["issues"]]
    conn.close()


def test_gather_ai_bots_carry_purpose_and_llms_three_states():
    """크롤 회차 → 페이로드: 봇 행은 용도를 싣고, llms.txt 는 셋을 가른다.

    llms_txt_found 가 NULL(옛 회차·못 받음)이면 None, 0 이면 {"found": False}.
    둘을 뭉치면 요청문이 증거 없이 "llms.txt 없음" 이라고 말한다.
    """
    conn, pid = _brain("llms")
    robots = "User-agent: GPTBot\nDisallow: /\n\nUser-agent: *\nAllow: /"
    rid = conn.execute("INSERT INTO crawl_runs(project_id,finished_at,seed,robots_txt)"
                       " VALUES(?,?,'home',?)", (pid, D, robots)).lastrowid
    conn.commit()
    d = dashboard.gather(conn, db.get_project(conn, "llms"))
    assert d["llms_txt"] is None, d["llms_txt"]                 # 안 받은 회차 = 모름
    bots = {r["bot"]: r for r in d["ai_bots"]}
    assert bots["GPTBot"]["purpose"] == "training" and bots["GPTBot"]["rule"], bots["GPTBot"]
    assert bots["OAI-SearchBot"]["purpose"] == "search" and not bots["OAI-SearchBot"]["rule"]
    db.write_llms_txt(conn, rid, {"found": 0, "bytes": 99, "head": "x"})
    d = dashboard.gather(conn, db.get_project(conn, "llms"))
    assert d["llms_txt"] == {"found": False, "bytes": None, "head": None}, d["llms_txt"]
    db.write_llms_txt(conn, rid, {"found": 1, "bytes": 12, "head": "# 사이트"})
    d = dashboard.gather(conn, db.get_project(conn, "llms"))
    assert d["llms_txt"] == {"found": True, "bytes": 12, "head": "# 사이트"}, d["llms_txt"]
    db.write_llms_txt(conn, rid, {"found": None})
    assert dashboard.gather(conn, db.get_project(conn, "llms"))["llms_txt"] is None
    conn.close()


# ── 축 함수 단독 — gather() 를 통째로 안 돌리고 축 하나만 부른다 ─────────────
def test_axis_gsc_pairs_same_period_snapshots_only():
    """gather() 가 아니라 _axis_gsc() 자체가 period_days 를 가려 짝짓는지.

    scoring.md 4-3b — 28일치와 90일치를 섞으면 Δ가 거짓이 된다. 이 규칙이
    gather() 안이 아니라 축 함수 안에 있어야 exports.summary/perf 도 안전하다.
    """
    conn, pid = _brain("ax_gsc")
    conn.executemany(
        "INSERT INTO gsc_snapshots(project_id,snapshot_date,period_days,query,clicks,"
        "impressions,ctr,position) VALUES(?,?,?,?,?,?,0.0,?)",
        [(pid, "2026-01-01", 90, "kw", 5, 100, 12.0),
         (pid, "2026-02-01", 28, "kw", 9, 100, 8.0)])
    conn.commit()
    d = dashboard._axis_gsc(conn, pid, {}, None)
    assert d["gsc_date"] == "2026-02-01" and d["gsc_period"] == 28
    assert d["gsc_prev"] is None, d["gsc_prev"]           # 90일치와는 짝짓지 않는다
    assert d["period_mismatch"] is True
    assert d["ups"] == [] and d["downs"] == []

    conn.execute(
        "INSERT INTO gsc_snapshots(project_id,snapshot_date,period_days,query,clicks,"
        "impressions,ctr,position) VALUES(?,?,?,?,?,?,0.0,?)",
        (pid, "2026-01-15", 28, "kw", 4, 90, 11.0))
    conn.commit()
    d2 = dashboard._axis_gsc(conn, pid, {}, None)
    assert d2["gsc_prev"] == "2026-01-15", d2["gsc_prev"]  # 같은 28일치를 찾아간다
    assert d2["period_mismatch"] is False
    assert d2["ups"] and d2["ups"][0]["dpos"] == 3.0        # 11.0 -> 8.0
    conn.close()


def test_axis_opps_resolves_band_and_gap_kind_standalone():
    """_axis_opps() 가 gsc/경쟁 축의 *결과 행*(striking/kw_gap)만 받아도 라벨·
    처방을 똑같이 붙이는지 — gather() 를 거치지 않고 손으로 만든 목록으로 확인한다.
    """
    conn, pid = _brain("ax_opps")
    conn.executemany(
        "INSERT INTO opportunities(project_id,kind,target,score,reasoning,status,created_at)"
        " VALUES(?,?,?,?,?,?,?)",
        [(pid, "striking_distance", "1페이지권", 80, "r", "new", D),
         (pid, "content_gap", "새글", 50, "r", "new", D),
         (pid, "rank_decay", "아무거나", 40, "r", "new", D),
         (pid, "content_gap", "먹은기회", 10, "r", "acked", D)])
    conn.commit()
    db.set_verdicts(conn, pid, [scoring.norm(t) for t in ("1페이지권", "새글", "아무거나", "먹은기회")], "work")
    striking = [{"query": "1페이지권", "band": "page1"}]
    kw_gap = [{"keyword": "새글", "kind": "missing"}]

    d = dashboard._axis_opps(conn, pid, None, striking, kw_gap)
    by_target = {o["target"]: o for o in d["opps"]}

    assert by_target["1페이지권"]["label"] == "1페이지 상단 가능", by_target["1페이지권"]
    assert by_target["새글"]["play"]["what"].startswith("경쟁 도메인은 잡고 있는데")
    decay = by_target["아무거나"]
    assert decay["label"] == "순위 하락" and decay["is_defensive"] is True
    # opps_total 은 status='new' 만 — 'acked' 는 새 기회 개수에서 빠진다
    assert d["opps_total"] == 3, d["opps_total"]
    conn.close()


def test_aio_opportunity_play_follows_our_rank_and_rows_carry_citations():
    """구글 AI 요약 빠짐은 우리 순위로 처방이 갈린다 — 1페이지 안이면 사람을 위한 글,
    밖이거나 순위가 없으면 "순위가 먼저". 순위 축은 누가 대신 인용됐는지와 함께 묻는
    질문을 싣고, 요약 기회의 행은 화면용 30개 자르기 밖에서도 요청문에 닿는다."""
    conn, pid = _brain("aio_band")
    kws = {"안쪽": 4, "바깥": None}
    kws.update({f"상위{i}": 1 + i % 3 for i in range(35)})     # 화면용 30개를 채우는 행들
    kid = {}
    for kw, pos in kws.items():
        kid[kw] = conn.execute("INSERT INTO keywords(project_id,keyword,is_active) VALUES(?,?,1)"
                               " RETURNING id", (pid, kw)).fetchone()[0]
        gap = kw in ("안쪽", "바깥")
        db.write_rank_snapshot(conn, kid[kw], pos, None, aio_present=1 if gap else 0,
                               aio_cited=0 if gap else None, checked_at=D + "T01:00:00Z",
                               aio_domains=["rival.example", "wiki.example"] if kw == "바깥" else
                               [] if kw == "안쪽" else None)
    db.write_serp_questions(conn, kid["바깥"], [("paa", "질문 하나"), ("related", "연관 하나")],
                            checked_at=D + "T01:00:00Z")
    # 다른 날의 질문은 이 회차 상위 옆에 서지 않는다
    db.write_serp_questions(conn, kid["안쪽"], [("paa", "옛 질문")], checked_at=PREV + "T01:00:00Z")
    conn.executemany(
        "INSERT INTO opportunities(project_id,kind,target,score,reasoning,status,created_at)"
        " VALUES(?,?,?,?,?,?,?)",
        [(pid, "aio_exposure", "안쪽", 60, "r", "new", D),
         (pid, "aio_exposure", "바깥", 50, "r", "new", D),
         (pid, "aio_exposure", "옛기회", 40, "r", "new", D)])     # 최신 회차에 없는 대상
    conn.commit()
    db.set_verdicts(conn, pid, [scoring.norm(t) for t in ("안쪽", "바깥", "옛기회")], "work")

    by = {o["target"]: o for o in dashboard._axis_opps(conn, pid, None, [], [])["opps"]}
    assert by["안쪽"]["band"] == "page1" and by["바깥"]["band"] == "beyond", \
        {t: o["band"] for t, o in by.items()}
    assert by["안쪽"]["play"] != by["바깥"]["play"], "1페이지 안과 밖이 같은 처방을 받는다"
    assert by["안쪽"]["play"]["what"].startswith("이미 1페이지 안인데")
    assert "순위가 먼저" in by["바깥"]["play"]["what"]
    # 순위를 모르면 "이미 1페이지"라고 지어내지 않는다
    assert by["옛기회"]["band"] is None and "순위가 먼저" in by["옛기회"]["play"]["what"]

    rk = dashboard._axis_rank(conn, pid)
    rows = {r["keyword"]: r for r in rk["ranks"]}
    assert rows["바깥"]["aio_domains"] == ["rival.example", "wiki.example"]
    # [] 는 "요약은 떴는데 인용을 못 뽑았다", None 은 "요약이 없었다" — 둘을 뭉치지 않는다
    assert rows["안쪽"]["aio_domains"] == [] and rows["상위0"]["aio_domains"] is None
    assert rows["바깥"]["aio_band"] == "beyond" and rows["안쪽"]["aio_band"] == "page1"
    assert rows["상위0"]["aio_band"] is None
    assert set(rk["aio_play"]) == set(scoring.AIO_BANDS)
    assert rk["serp_fanout"] == {"바깥": [{"kind": "paa", "text": "질문 하나"},
                                          {"kind": "related", "text": "연관 하나"}]}, rk["serp_fanout"]

    # gather 는 ranks 를 순위 순 30개로 자른다 — 순위 없는 '바깥'은 그 밖이지만 요청문은
    # 누가 대신 인용됐는지를 말해야 한다
    p = db.get_project(conn, "aio_band")
    d = dashboard.gather(conn, p)
    assert "바깥" not in {r["keyword"] for r in d["ranks"]}, "픽스처가 30개를 못 채웠다"
    body = next(o for o in d["opps"] if o["target"] == "바깥")["brief"]["body"]
    assert "구글 AI 요약이 대신 인용한 곳: rival.example, wiki.example" in body, body
    assert "## 함께 답해야 할 질문" in body and "질문 하나" in body, body
    conn.close()


def test_aio_band_falls_back_to_rank_row_then_gsc():
    """최신 AI 요약 회차에 없는 옛 기회의 갈래 — 요청문 근거(brief._ev_aio)가 순위를
    읽는 순서대로 물러선다: 순위 행(실측 5위 → page1, 안 보임 → beyond) → GSC 평균
    순위(노출 가중 11위 → beyond) → 아무것도 없으면 None. 실제로 theotherskin #173 이
    실측 5위(근거표)인데 처방은 "1페이지 밖"이었다 — 갈래와 근거가 다른 자리를 읽었다."""
    conn, pid = _brain("aio_fb")
    kid = {}
    for kw, pos in (("순위만", 5), ("안보임", None)):
        kid[kw] = conn.execute("INSERT INTO keywords(project_id,keyword,is_active) VALUES(?,?,1)"
                               " RETURNING id", (pid, kw)).fetchone()[0]
        # aio_present=0: 최신 회차의 AI 요약 빠짐(aio_gaps)에는 없고 순위 행에만 있다
        db.write_rank_snapshot(conn, kid[kw], pos, "https://aio_fb.example/p" if pos else None,
                               aio_present=0, checked_at=D + "T01:00:00Z")
    # GSC 만 있는 검색어 — 지면 둘, 노출 가중이면 11위(beyond), 단순 평균이면 7위(page1)
    conn.executemany(
        "INSERT INTO gsc_snapshots(project_id,snapshot_date,period_days,query,page,"
        "clicks,impressions,ctr,position) VALUES(?,?,28,?,?,1,?,0.02,?)",
        [(pid, D, "GSC만", "https://aio_fb.example/g1", 90, 12.0),
         (pid, D, "GSC만", "https://aio_fb.example/g2", 10, 2.0)])
    targets = ("순위만", "안보임", "GSC만", "모름")
    conn.executemany(
        "INSERT INTO opportunities(project_id,kind,target,score,reasoning,status,created_at)"
        " VALUES(?,?,?,?,?,?,?)",
        [(pid, "aio_exposure", t, 50, "r", "new", D) for t in targets])
    conn.commit()
    db.set_verdicts(conn, pid, [scoring.norm(t) for t in targets], "work")

    ranks = dashboard._axis_rank(conn, pid)["ranks"]
    by = {o["target"]: o for o in dashboard._axis_opps(conn, pid, None, [], [], ranks=ranks)["opps"]}
    assert by["순위만"]["band"] == "page1", by["순위만"]["band"]
    assert by["순위만"]["play"]["what"].startswith("이미 1페이지 안인데"), by["순위만"]["play"]["what"]
    assert by["안보임"]["band"] == "beyond", by["안보임"]["band"]
    assert by["GSC만"]["band"] == "beyond", by["GSC만"]["band"]
    assert by["GSC만"]["play"]["what"].startswith("구글이 이 검색어에")
    assert by["모름"]["band"] is None and "순위가 먼저" in by["모름"]["play"]["what"]
    # 순위 행을 안 넘기면 그 갈래는 못 난다(GSC 도 없다) — gather 가 ranks_all 을 넘겨야 한다
    no_ranks = {o["target"]: o["band"] for o in dashboard._axis_opps(conn, pid, None, [], [])["opps"]}
    assert no_ranks["순위만"] is None, no_ranks

    # gather 는 자르기 전 순위 행(ranks_all)을 넘긴다 — 갈래와 요청문 근거가 같은 5위를 말한다
    d = dashboard.gather(conn, db.get_project(conn, "aio_fb"))
    o = next(o for o in d["opps"] if o["target"] == "순위만")
    assert o["band"] == "page1", o["band"]
    assert "5위" in o["brief"]["body"], o["brief"]["body"]
    assert next(o for o in d["opps"] if o["target"] == "GSC만")["band"] == "beyond"
    conn.close()


# ── 검색량이 점수에 닿는가 ─────────────────────────────────────────────────
def test_volume_feeds_demand():
    """노출만 보면 아직 안 뜨는 검색어는 영원히 0점이다 — 검색량이 그 자리를 채운다."""
    none_ = scoring.score("coverage", {"impressions": 0, "position": None}, "directory")
    small = scoring.score("coverage", {"impressions": 0, "volume": 30, "position": None},
                          "directory")
    big = scoring.score("coverage", {"impressions": 0, "volume": 9000, "position": None},
                        "directory")
    assert none_ < small < big, (none_, small, big)


def test_volume_absent_keeps_old_score():
    """검색량을 안 산 Brain 의 점수는 하나도 안 변해야 한다(회귀 없음)."""
    import math
    m = {"impressions": 4200, "position": 3.0}
    w = scoring.WEIGHTS["saas"]
    demand = min(1.0, math.log10(1 + 4200) / 5.0)
    reach = max(0.0, 1.0 - scoring.gap_to_page1(3.0) / scoring.PAGE1)
    want = round(min(100.0, (w["w_demand"] * demand + w["w_reach"] * reach
                             + w["w_fit"] * 0.5) * 100), 1)
    assert scoring.score("striking_distance", m, "saas") == want


def test_volume_never_beats_real_impressions():
    """실측(노출)이 추정(검색량)보다 크면 실측을 쓴다 — 큰 쪽이지 합이 아니다."""
    a = scoring.score("coverage", {"impressions": 50000, "volume": 10}, "saas")
    b = scoring.score("coverage", {"impressions": 50000}, "saas")
    assert a == b, (a, b)


def test_coverage_sums_cluster_volume():
    conn, pid = _brain("cv")
    conn.executemany(
        "INSERT INTO keywords(project_id,keyword,cluster,volume,is_active)"
        " VALUES(?,?,?,?,1)",
        [(pid, "가", "묶음A", 500), (pid, "나", "묶음A", 300),
         (pid, "다", "묶음A", None),            # 모르는 것은 0 으로 — 있다고 치지 않는다
         (pid, "라", "묶음B", 40)])
    conn.commit()
    cov = scoring.coverage(conn, pid)
    assert cov["by_cluster"] == {"묶음A": 3, "묶음B": 1}, cov["by_cluster"]
    assert cov["volume_by_cluster"] == {"묶음A": 800, "묶음B": 40}, cov["volume_by_cluster"]
    conn.close()


# ── 후보 목록이 "왜 켜야 하나"에 답하는가 ──────────────────────────────────
def test_list_keywords_carries_metrics_and_sorts_by_volume():
    conn, pid = _brain("kw")
    conn.executemany(
        "INSERT INTO keywords(project_id,keyword,volume,difficulty,cpc,is_active)"
        " VALUES(?,?,?,?,?,0)",
        [(pid, "작은 수요", 100, 12.0, 0.3),
         (pid, "큰 수요", 5000, 61.0, 1.2),
         (pid, "모르는 것", None, None, None)])
    conn.commit()
    rows = db.list_keywords(conn, pid, active=False)
    got = [(r["keyword"], r["volume"], r["difficulty"], r["cpc"]) for r in rows]
    # 노출이 전부 0 이면 검색량 큰 순, 모르는 것(NULL)은 맨 뒤
    assert [g[0] for g in got] == ["큰 수요", "작은 수요", "모르는 것"], got
    assert got[0][1:] == (5000, 61.0, 1.2), got[0]
    conn.close()


def test_list_keywords_still_puts_impressions_first():
    """검색량 정렬이 노출 정렬을 밀어내면 안 된다 — 실측이 먼저다."""
    conn, pid = _brain("kw2")
    conn.executemany("INSERT INTO keywords(project_id,keyword,volume,is_active)"
                     " VALUES(?,?,?,0)", [(pid, "노출 있음", 10), (pid, "검색량만", 9000)])
    conn.execute("INSERT INTO gsc_snapshots(project_id,snapshot_date,period_days,query,"
                 "page,clicks,impressions,ctr,position)"
                 " VALUES(?,?,28,'노출 있음','p',1,700,0.1,5)", (pid, D))
    conn.commit()
    rows = db.list_keywords(conn, pid, active=False)
    assert [r["keyword"] for r in rows] == ["노출 있음", "검색량만"], \
        [r["keyword"] for r in rows]
    conn.close()


# ── 검색어 심사 ───────────────────────────────────────────────────────────
def test_triage_payload_groups_variants_and_counts():
    """/api/triage 는 열린 기회를 정규화한 검색어로 묶는다 — 행 하나가 판정 단위다.
    검색어가 아닌 종류(index_blocked)는 심사에 안 오른다."""
    conn, pid = _brain("tri")
    db.upsert_opportunities(conn, pid, None, [
        {"kind": "striking_distance", "target": "디아더피부과 가격", "score": 39.1},
        {"kind": "cannibalization", "target": "디아 더 피부과 가격", "score": 20.0},
        {"kind": "aio_exposure", "target": "레이저 토닝 후기", "score": 35.2},
        {"kind": "index_blocked", "target": "https://tri.example/x", "score": 10}])
    conn.execute("INSERT INTO gsc_snapshots(project_id,snapshot_date,period_days,query,clicks,impressions,ctr,position)"
                 " VALUES(?,?,28,'디아더피부과 가격',12,340,0.03,9.0)", (pid, D))
    conn.commit()
    conn.close()
    t = dashboard.triage_payload("tri")
    assert t["counts"] == {"none": 2, "irrelevant": 0, "hold": 0, "work": 0}, t["counts"]
    a, b = t["rows"]
    assert a["label"] == "디아더피부과 가격" and a["variants"] == 2, a
    assert set(a["kinds"]) == {"striking_distance", "cannibalization"} and a["score"] == 39.1
    assert a["labels"] and a["clicks"] == 12 and a["impressions"] == 340
    assert b["verdict"] is None and b["clicks"] == 0 and a["brand"] is False
    assert dashboard.set_verdict({"project": "tri", "keys": [a["key"]], "verdict": "hold"}) == {"updated": 1}
    t = dashboard.triage_payload("tri")
    assert t["counts"]["hold"] == 1
    assert [r for r in t["rows"] if r["key"] == a["key"]][0]["verdict"] == "hold"
    try:
        dashboard.set_verdict({"project": "tri", "keys": [a["key"]], "verdict": "nope"})
        assert False, "잘못된 판정을 받았다"
    except ValueError:
        pass
    assert ("GET", "/api/triage") in dashboard.ROUTES and ("POST", "/api/verdict") in dashboard.ROUTES
    conn = db.connect()
    d = dashboard.gather(conn, db.get_project(conn, "tri"))
    assert "watch" in d and d["keyword_kinds"] == list(scoring.KEYWORD_KINDS)
    conn.close()


# ── 기회 묶음 — 같은 지면의 변형 검색어는 목록 한 줄 ─────────────────────────
def _surface_fixture(name: str):
    """AI 요약 기회 여럿과 그 지면 사실(GSC 페이지·SERP 상위·클러스터).

      A·B·C  : GSC 에서 같은 우리 페이지(/syringoma)로 들어온다        → 한 줄(page)
      E      : 우리 페이지 없음, SERP 상위 5개 중 3개가 A 와 같다        → A 줄에 붙음(serp)
      D·D2   : 다른 페이지(/milia). D2 는 D 의 띄어쓰기 변형              → 한 줄, A 와 안 섞임
      F      : 페이지 없음, SERP 가 누구와도 2개 이하로만 겹친다          → 혼자
      G·H    : 페이지·SERP 없음, 같은 클러스터                           → 한 줄(cluster)
      J·K    : A 와 같은 페이지지만 done·resolved                       → 열린 줄에 안 낀다
      S      : A 와 같은 검색어의 다른 종류(striking_distance)            → 안 묶이는 종류, 따로
    """
    conn, pid = _brain(name)
    t = {"A": "syringoma", "B": "syringomas", "C": "syringoma treatment",
         "E": "milia vs syringoma", "D": "milia", "D2": "mil ia", "F": "eye bumps",
         "G": "stye home remedy", "H": "stye remedy", "J": "syringoma cost", "K": "syringoma price"}
    score = {"A": 60, "B": 55, "C": 40, "E": 50, "D": 58, "D2": 20, "F": 45, "G": 30,
             "H": 25, "J": 70, "K": 65}
    status = {"J": "done", "K": "resolved"}
    vol = {"A": 1000, "B": 300, "C": 90, "E": 200, "D": 800}
    for k, kw in t.items():
        conn.execute("INSERT INTO keywords(project_id,keyword,cluster,volume,is_active)"
                     " VALUES(?,?,?,?,1)", (pid, kw, "stye" if k in "GH" else None, vol.get(k)))
    page = {"A": "/syringoma", "B": "/syringoma/", "C": "/syringoma", "J": "/syringoma",
            "K": "/syringoma", "D": "/milia", "D2": "/milia"}
    for k, path in page.items():
        conn.execute("INSERT INTO gsc_snapshots(project_id,snapshot_date,period_days,query,page,"
                     "clicks,impressions,ctr,position) VALUES(?,?,28,?,?,1,50,0.02,8)",
                     (pid, D, t[k], f"https://{name}.example{path}"))
    # SERP 상위 5개(수집기가 남기는 만큼, db.SERP_KEEP): A 는 r0~r4, E 는 r0~r2 + 딴 곳
    # 둘(3개 겹침 — 붙는다), F 는 r3·r4 + 딴 곳 셋(2개 겹침 — 안 붙는다)
    serp = {"A": [f"r{i}" for i in range(5)],
            "E": ["r0", "r1", "r2", "e0", "e1"],
            "F": ["r3", "r4", "f0", "f1", "f2"]}
    for k, hosts in serp.items():
        kid = conn.execute("SELECT id FROM keywords WHERE project_id=? AND keyword=?",
                           (pid, t[k])).fetchone()[0]
        db.write_serp_results(conn, kid, [{"position": i + 1, "url": f"https://{h}.example/x",
                                           "domain": f"{h}.example"}
                                          for i, h in enumerate(hosts)], checked_at=D)
    conn.executemany(
        "INSERT INTO opportunities(project_id,kind,target,score,reasoning,status,created_at)"
        " VALUES(?,?,?,?,?,?,?)",
        [(pid, "aio_exposure", t[k], score[k], "r", status.get(k, "new"), D) for k in t]
        + [(pid, "striking_distance", t["A"], 75, "r", "new", D)])
    conn.commit()
    db.set_verdicts(conn, pid, [scoring.norm(v) for v in t.values()], "work")
    ids = {k: conn.execute("SELECT id FROM opportunities WHERE project_id=? AND kind="
                           "'aio_exposure' AND target=?", (pid, t[k])).fetchone()[0] for k in t}
    ids["S"] = conn.execute("SELECT id FROM opportunities WHERE project_id=? AND kind="
                            "'striking_distance'", (pid,)).fetchone()[0]
    return conn, pid, ids


def test_opp_groups_fold_same_surface_into_one_line():
    """같은 지면의 변형 셋(A·B·C)과 SERP 로 붙는 E 가 **한 줄**이고, id 는 넷 다 남는다.
    다른 지면(D)·혼자(F)·클러스터(G·H)는 안 섞인다. 묶는 종류가 아닌 기회(S)는 따로 선다."""
    conn, pid, ids = _surface_fixture("grp")
    d = dashboard._axis_opps(conn, pid, None, [], [])
    lines = d["opp_groups"]
    of = {i: ln for ln in lines for i in ln["ids"]}
    a = of[ids["A"]]
    assert set(a["ids"]) == {ids["A"], ids["B"], ids["C"], ids["E"]}, a
    assert a["lead"] == ids["A"] and a["via"] == "page" and a["key_src"] == "gsc", a
    assert a["key"].endswith("/syringoma"), a["key"]
    assert [v["id"] for v in a["variants"]] == [ids["A"], ids["B"], ids["E"], ids["C"]], a
    assert {v["id"]: v["via"] for v in a["variants"]}[ids["E"]] == "serp", a["variants"]
    assert sum(1 for ln in lines if ids["A"] in ln["ids"]) == 1, "대표가 두 줄에 선다"
    dd = of[ids["D"]]
    # 띄어쓰기 변형뿐인 줄은 "같은 검색어"라고 말한다(같은 페이지인 것은 key 가 남긴다)
    assert set(dd["ids"]) == {ids["D"], ids["D2"]} and dd["via"] == "norm", dd
    assert dd["key"].endswith("/milia") and not set(dd["ids"]) & set(a["ids"]), dd
    assert of[ids["F"]]["ids"] == [ids["F"]] and of[ids["F"]]["via"] == "alone"
    gh = of[ids["G"]]
    assert set(gh["ids"]) == {ids["G"], ids["H"]} and gh["via"] == "cluster" and gh["key"] == "stye"
    s = of[ids["S"]]
    assert s["ids"] == [ids["S"]] and s["via"] is None and s["kind"] == "striking_distance"
    # 묶음 점수: 대표 점수 + (지면 전체 검색량으로 다시 잰 수요 - 대표 검색량의 수요)
    base = {"impressions": 0, "position": None, "fit": 0.5}
    want = round(60 + scoring.score("aio_exposure", {**base, "volume": 1000 + 300 + 90 + 200}, "saas")
                 - scoring.score("aio_exposure", {**base, "volume": 1000}, "saas"), 1)
    assert a["score"] == want and 60 < a["score"] <= 100, (a["score"], want)
    # 줄 순서 = 화면 순서(새 것 먼저, 점수 내림차순)
    news = [ln["score"] for ln in lines if ln["status"] == "new"]
    assert news == sorted(news, reverse=True), news
    conn.close()


def test_opp_groups_keep_closed_out_of_open_lines():
    """열린 줄에는 done·resolved 가 없다 — 같은 페이지여도 안 묶이고, 기록으로 한 줄씩 남는다.
    거르는 쪽이 "done 이 아니면"이었다면 resolved(저절로 풀린 기회)가 새어 들어온다."""
    conn, pid, ids = _surface_fixture("grp_closed")
    lines = dashboard._axis_opps(conn, pid, None, [], [])["opp_groups"]
    for ln in lines:
        if ln["status"] in scoring.OPEN_STATUSES:
            assert all(v["status"] in scoring.OPEN_STATUSES for v in ln["variants"]), ln
            assert ids["J"] not in ln["ids"] and ids["K"] not in ln["ids"], ln
    closed = {ln["lead"]: ln for ln in lines if ln["status"] not in scoring.OPEN_STATUSES}
    assert closed[ids["J"]]["ids"] == [ids["J"]] and closed[ids["J"]]["status"] == "done"
    assert closed[ids["K"]]["ids"] == [ids["K"]] and closed[ids["K"]]["status"] == "resolved"
    assert "resolved" not in scoring.OPEN_STATUSES and "done" not in scoring.OPEN_STATUSES
    conn.close()


def test_opp_groups_keep_ids_beyond_the_cap():
    """화면에 싣는 기회는 상한이 있다. 상한 밖으로 밀린 변형도 묶음 id 에 남아야 한다 —
    아니면 [완료 표시]가 그것만 남기고, 다음 적재에 혼자 다시 선다."""
    conn, pid, ids = _surface_fixture("grp_cap")
    opps = [o for o in scoring.opportunities(conn, pid, limit=200, with_id=True)
            if o["id"] == ids["A"]]                     # 대표 하나만 실린 상태
    lines = scoring.group_opportunities(conn, pid, opps)
    assert len(lines) == 1, lines                        # 대표가 없는 묶음은 줄로 안 선다
    assert set(lines[0]["ids"]) == {ids["A"], ids["B"], ids["C"], ids["E"]}, lines[0]
    conn.close()


# ── 온보딩 0단계 ──────────────────────────────────────────────────────────
def test_setup_payload_carries_usage_choices():
    """쓰는 방식·도구·터미널의 정본은 doctor 의 표 셋이고, 그 선택이 설정 화면
    페이로드까지 그대로 온다 — 화면은 여기 실린 것만 그린다(사본을 두지 않는다)."""
    import doctor
    ids = [t[0] for t in doctor.TOOLS]
    assert ids == ["claude", "codex", "opencode", "pi"]
    assert [m[0] for m in doctor.MODES] == ["hosted", "local"]
    assert [t[0] for t in doctor.TERMINALS] == ["orca", "system"]
    assert doctor.tool_of("codex")[1] == "Codex" and doctor.tool_of("nope") is None
    os.environ["SEOMINER_TOOL"] = "codex"; os.environ["SEOMINER_MODE"] = "local"
    try:
        p = dashboard.setup_state("")
    finally:
        os.environ.pop("SEOMINER_TOOL"); os.environ.pop("SEOMINER_MODE")
    assert p["tool"] == "codex" and p["mode"] == "local" and p["terminal"] in ("orca", "system")
    assert {t["id"] for t in p["tools"]} == set(ids) and all("installed" in t for t in p["tools"])
    assert isinstance(p["orca_ok"], bool)


def test_setup_dirs_and_remote_line():
    """사이트별 로컬 폴더는 ~/.capture/dirs.json 한 자리에 살고, 호스팅 연결은
    웹 [설정]이 내는 한 줄에서 url·token 두 토큰만 뽑아 remote.link 로 넘긴다."""
    import paths
    home = Path(os.environ["CAPTURE_HOME"])
    assert paths.site_dirs() == {}
    d = tempfile.mkdtemp(prefix="seo-miner-dir-")
    assert paths.set_site_dir("mysite", d) == {"mysite": d}
    assert (home / "dirs.json").exists()
    assert paths.set_site_dir("mysite", None) == {}
    r = dashboard.setup_dir({"project": "mysite", "path": str(home / "없는폴더")})
    assert r["ok"] is False
    r = dashboard.setup_dir({"project": "mysite", "path": d})
    assert r["ok"] and r["dirs"] == {"mysite": d}
    got = dashboard.setup_dirs()
    assert got["dirs"] == {"mysite": d} and isinstance(got["worktrees"], list)
    called = {}
    import remote
    orig = remote.link
    remote.link = lambda url, token: called.update(url=url, token=token)
    try:
        r = dashboard.setup_remote({"line": 'python "C:/x/remote.py" connect https://h.example/ abc123'})
        assert r["ok"] and called == {"url": "https://h.example/", "token": "abc123"}, (r, called)
        assert dashboard.setup_remote({"line": "아무 말"})["ok"] is False
    finally:
        remote.link = orig


# ── 호스팅 사이트를 로컬 화면에 (프록시) ──────────────────────────────────
def test_local_handler_proxies_remote_sites():
    """사이트 이름이 호스팅 것이면 로컬 Handler 는 자기 Brain 을 안 읽고 서버에
    그대로 넘긴다 — 화면은 한 벌이고 데이터가 있는 쪽이 답한다."""
    import remote
    calls = []
    orig_owns, orig_api = remote.owns, remote.api
    remote.owns = lambda p: p == "webonly"
    remote.api = lambda method, path, **kw: calls.append((method, path, kw)) or {"proxied": True}
    try:
        assert dashboard.remote_project("webonly") and not dashboard.remote_project("t")
        assert not dashboard.remote_project("")
        srv = _serve()
        try:
            import json as _j
            import urllib.request
            base = f"http://127.0.0.1:{srv.server_address[1]}"
            u = f"{base}/api/data?project=webonly&date=2026-01-01"
            assert _j.loads(urllib.request.urlopen(u).read()) == {"proxied": True}
            assert calls[-1][0] == "GET" and calls[-1][1] == "/api/data"
            assert calls[-1][2]["params"]["project"] == "webonly"
            assert calls[-1][2]["params"]["date"] == "2026-01-01"
            req = urllib.request.Request(
                f"{base}/api/opp",
                data=_j.dumps({"project": "webonly", "id": 1, "status": "acked"}).encode(),
                headers={"Content-Type": "application/json", "X-Token": dashboard.TOKEN},
                method="POST")
            assert _j.loads(urllib.request.urlopen(req).read()) == {"proxied": True}
            assert calls[-1][0] == "POST" and calls[-1][2]["json"]["id"] == 1
            names = _j.loads(urllib.request.urlopen(f"{base}/api/projects").read())
            assert "webonly" not in names   # config() 를 안 흉내 냈으니 로컬 이름만
            # [설정] 진단은 이 PC 를 묻는 것이다 — 원격 사이트를 보고 있어도 서버에
            # 안 넘긴다(넘기면 남의 컴퓨터에 무엇이 깔렸는지를 답한다).
            n = len(calls)
            d = _j.loads(urllib.request.urlopen(f"{base}/api/doctor?project=webonly").read())
            assert len(calls) == n and "proxied" not in d, (len(calls) - n, d)
        finally:
            srv.shutdown()
    finally:
        remote.owns, remote.api = orig_owns, orig_api


# ── 기록 창구 ─────────────────────────────────────────────────────────────
def test_creation_route_records_and_marks_acked():
    """작업 기록은 /api/creation 한 창구다 — 로컬 ROUTES 본체가 기록하고 기회를
    진행 중으로 옮긴다. 남의 사이트 기회 번호는 LookupError(=404)."""
    conn, pid = _brain("cre")
    db.upsert_opportunities(conn, pid, None,
                            [{"kind": "striking_distance", "target": "q", "score": 10}])
    oid = conn.execute("SELECT id FROM opportunities WHERE project_id=?", (pid,)).fetchone()[0]
    conn.close()
    r = dashboard.ROUTES[("POST", "/api/creation")](
        "", {}, {"project": "cre", "opportunity_id": oid,
                 "path": "content/a.md", "branch": "capture/x-a", "note": "n"})
    assert r["status"] == "acked" and r["creation_id"]
    conn = db.connect()
    assert conn.execute("SELECT status FROM opportunities WHERE id=?",
                        (oid,)).fetchone()[0] == "acked"
    row = conn.execute("SELECT file_path, branch, kind FROM creations WHERE opportunity_id=?",
                       (oid,)).fetchone()
    assert tuple(row) == ("content/a.md", "capture/x-a", "striking_distance"), tuple(row)
    conn.close()
    try:
        dashboard.ROUTES[("POST", "/api/creation")](
            "", {}, {"project": "cre", "opportunity_id": 999999, "path": "x"})
        assert False, "남의 기회 번호가 통과했다"
    except LookupError:
        pass


# ── 기회 카드에서 도구 열기 ────────────────────────────────────────────────
def _opp_status(opp_id: int) -> str:
    conn = db.connect()
    try:
        return conn.execute("SELECT status FROM opportunities WHERE id=?",
                            (opp_id,)).fetchone()[0]
    finally:
        conn.close()


def _opp_fixture(name: str, target: str) -> int:
    """기회 하나짜리 사이트 — 심사를 통과시켜야 화면(payload)에 실린다."""
    conn, pid = _brain(name)
    db.upsert_opportunities(conn, pid, None,
                            [{"kind": "striking_distance", "target": target, "score": 30}])
    db.set_verdicts(conn, pid, [scoring.norm(target)], "work")
    conn.execute("INSERT INTO gsc_snapshots(project_id,snapshot_date,period_days,query,"
                 "clicks,impressions,ctr,position) VALUES(?,?,28,?,1,10,0.1,9.0)",
                 (pid, D, target))
    conn.commit()
    oid = conn.execute("SELECT id FROM opportunities WHERE project_id=?", (pid,)).fetchone()[0]
    conn.close()
    return oid


def test_run_tool_builds_command_and_writes_brief():
    """실행 버튼의 몸 — 요청문을 파일로 쓰고, 고른 도구의 argv 를 조립하고, 어느
    폴더에서 열지 정한다. dry_run 은 터미널도 안 띄우고 상태도 안 바꾼다.

    도구 이름·실행 파일·argv 꼴의 정본은 doctor.TOOLS 다 — 여기서 사본을 안 만든다.
    """
    import shutil as _sh
    import doctor
    import paths
    oid = _opp_fixture("rt", "q1")
    os.environ.pop("SEOMINER_TOOL", None)
    orig_which = _sh.which
    try:
        # 도구를 안 골랐다 — 상태를 안 바꾸고 왜 못 여는지 말한다
        r = dashboard.run_tool({"project": "rt", "id": oid, "dry_run": True})
        assert r["ok"] is False and "도구" in r["error"], r

        # 골랐는데 안 깔렸다 — 열기 전에 여기서 막는다(열고 나서 실패하지 않는다)
        os.environ["SEOMINER_TOOL"] = "codex"
        _sh.which = lambda c: None
        r = dashboard.run_tool({"project": "rt", "id": oid, "dry_run": True})
        assert r["ok"] is False and doctor.tool_of("codex")[1] in r["error"], r

        _sh.which = lambda c: "/bin/codex" if c == "codex" else None
        r = dashboard.run_tool({"project": "rt", "id": oid, "dry_run": True})
        assert r["ok"], r
        assert r["argv"][0] == doctor.tool_of("codex")[2], r["argv"]
        assert r["file"].endswith(f"opp-{oid}.md") and r["file"] in r["argv"][-1], r
        assert Path(r["cwd"]) == paths.home() / "work" / "rt", r["cwd"]  # 폴더 없는 사이트
        body = Path(r["file"]).read_text("utf-8")
        assert "createdb.py" in body and f"done rt {oid}" in body, body[-400:]
        assert "## " in body, "요청문 본문이 안 들어갔다"
        # 파일은 화면의 복사 버튼(briefText)과 같은 전문이다 — 본문 뒤에 꼴 꼬리(답의
        # 형식·규칙, d.brief.tails[shape])까지. 본문만 쓰면 도구가 형식 없이 시작한다.
        d = dashboard.payload("rt")
        o = next(o for o in d["opps"] if o["id"] == oid)
        assert d["brief"]["tails"][o["brief"]["shape"]] in body, "꼴 꼬리가 파일에 없다"
        assert "## 답의 형식" in body and "## 규칙" in body, body[-600:]
        assert r["ids"] == [oid], r["ids"]      # ids 를 안 보내면 대표 하나뿐

        # 폴더를 적어 두면 거기서 연다
        d = tempfile.mkdtemp(prefix="seo-miner-rt-")
        paths.set_site_dir("rt", d)
        try:
            r = dashboard.run_tool({"project": "rt", "id": oid, "dry_run": True})
            assert Path(r["cwd"]) == Path(d), r["cwd"]
        finally:
            paths.set_site_dir("rt", None)

        # 없는 기회는 400 이고, dry_run 은 상태를 안 건드린다
        assert dashboard.run_tool({"project": "rt", "id": oid + 9999,
                                   "dry_run": True})["ok"] is False
        assert _opp_status(oid) == "new", "dry_run 이 상태를 바꿨다"
    finally:
        _sh.which = orig_which
        os.environ.pop("SEOMINER_TOOL", None)


def test_run_tool_opens_terminal_and_acks():
    """터미널을 띄우는 갈래 — Orca 가 안 되면 시스템으로 물러나고, 열렸으면 그 기회는
    '작업 시작'(acked)이 된다. 검사에서 진짜 창을 띄우지 않는다(_open_terminal 을 흉내)."""
    import shutil as _sh
    oid = _opp_fixture("rt2", "q2")
    orig_which, orig_open = _sh.which, dashboard._open_terminal
    seen = {}
    os.environ["SEOMINER_TOOL"] = "claude"
    os.environ["SEOMINER_TERMINAL"] = "orca"
    try:
        _sh.which = lambda c: "/bin/" + c
        dashboard._open_terminal = lambda argv, cwd, terminal, title: (
            seen.update(argv=argv, cwd=str(cwd), terminal=terminal, title=title)
            or {"terminal": "system", "fallback": "system"})
        r = dashboard.run_tool({"project": "rt2", "id": oid})
        assert r["ok"] and r["terminal"] == "system" and r["fallback"] == "system", r
        assert seen["terminal"] == "orca" and str(oid) in seen["title"], seen
        assert _opp_status(oid) == "acked", "열었는데 작업 시작으로 안 바뀌었다"
    finally:
        _sh.which, dashboard._open_terminal = orig_which, orig_open
        os.environ.pop("SEOMINER_TOOL", None)
        os.environ.pop("SEOMINER_TERMINAL", None)


def test_run_tool_acks_the_whole_group():
    """[개요]의 묶인 줄(opp_groups)에서 열면 창·파일은 대표 하나지만 '작업 시작'은 묶인
    id 전부다 — 상태 버튼(setOpps)과 같은 범위. 아니면 새로고침 뒤 한 줄이 둘로 갈라진다.
    그 사이트 것이 아닌 id·못 읽는 값은 조용히 버리고 열기는 성공한다."""
    import shutil as _sh
    a = _opp_fixture("rt3", "q3")
    conn = db.connect()
    pid = db.get_project(conn, "rt3")["id"]
    db.upsert_opportunities(conn, pid, None,
                            [{"kind": "striking_distance", "target": "q3 변형", "score": 20}])
    db.set_verdicts(conn, pid, [scoring.norm("q3 변형")], "work")
    b = conn.execute("SELECT id FROM opportunities WHERE project_id=? AND target='q3 변형'",
                     (pid,)).fetchone()[0]
    conn.close()
    other = _opp_fixture("rt3b", "q3b")          # 남의 사이트 기회 — 건드리면 안 된다
    orig_which, orig_open = _sh.which, dashboard._open_terminal
    os.environ["SEOMINER_TOOL"] = "claude"
    os.environ["SEOMINER_TERMINAL"] = "system"
    try:
        _sh.which = lambda c: "/bin/" + c
        dashboard._open_terminal = lambda argv, cwd, terminal, title: {"terminal": terminal}
        r = dashboard.run_tool({"project": "rt3", "id": a, "ids": [b, a, other, "x", None]})
        assert r["ok"] and not r.get("status_failed"), r
        assert r["ids"] == [a, b], r["ids"]
        assert r["file"].endswith(f"opp-{a}.md"), r["file"]
        assert _opp_status(a) == "acked" and _opp_status(b) == "acked", \
            "묶인 id 가 전부 작업 시작으로 안 바뀌었다"
        assert _opp_status(other) == "new", "남의 사이트 기회를 바꿨다"
    finally:
        _sh.which, dashboard._open_terminal = orig_which, orig_open
        os.environ.pop("SEOMINER_TOOL", None)
        os.environ.pop("SEOMINER_TERMINAL", None)


def test_gather_ai_health_reaches_the_screen():
    """AI 화면의 "측정 안 됨·구버전" 개수가 페이로드까지 온다.

    화면 위 표들(matrix·ai_by_prompt)은 최신 확인 한 번만 본다. 그 확인이 끊겼으면
    거기서 안 잰 질문은 표에도 기회에도 없다 — 그 수가 페이로드에 없으면 화면은
    말할 수가 없다(09-02 #56 이 그랬다: 질문 17개가 한 번도 안 재졌는데 조용했다).
    """
    import collector
    conn, pid = _brain("aihealth")
    conn.executemany("INSERT INTO ai_prompts(project_id, prompt, gen_version) VALUES(?,?,?)",
                     [(pid, "잰 질문", None), (pid, "안 잰 질문", 2), (pid, "손으로 적은 질문", 0)])
    qa = conn.execute("SELECT id FROM ai_prompts WHERE prompt='잰 질문'").fetchone()[0]
    qb = conn.execute("SELECT id FROM ai_prompts WHERE prompt='안 잰 질문'").fetchone()[0]
    with db.run(conn, pid, "ai") as r:
        conn.execute("INSERT INTO ai_checks(prompt_id,run_id,engine) VALUES(?,?,'chatgpt')",
                     (qa, r.id))
    try:
        with db.run(conn, pid, "ai") as r:
            conn.execute("INSERT INTO ai_checks(prompt_id,run_id,engine) VALUES(?,?,'chatgpt')",
                         (qb, r.id))
            raise collector.Fatal("OpenRouter 크레딧 소진")
    except collector.Fatal:
        pass
    d = dashboard.gather(conn, db.get_project(conn, "aihealth"))
    h = d["ai_health"]
    assert (h["active"], h["measured"], h["unmeasured"]) == (3, 1, 2), h
    assert h["outdated"] == 1 and h["outdated_eg"] == ["잰 질문"], h   # NULL 만 구버전
    assert h["last_run"]["state"] == "aborted", h["last_run"]
    assert "크레딧 소진" in h["last_run"]["note"], h["last_run"]
    # 기회는 끝난 회차만 쓴다 — 끊긴 회차에서 잰 "안 잰 질문"은 인용 0 기회가 아니다
    assert [g["prompt"] for g in scoring.ai_gaps(conn, pid)] == ["잰 질문"]
    conn.close()


def test_gather_ai_referrals_none_is_not_zero():
    """AI 에서 온 방문 — "안 쟀다"(None)와 "쟀고 0"([])이 페이로드에서 갈린다.

    갈리지 않으면 화면이 GA4 를 연결한 사이트에 "연결하세요"라고 하거나, 안 잰 사이트에
    "아무도 안 왔다"라고 한다. 세 키는 늘 같이 움직인다(ai_referrals·_pages·_meta).
    """
    conn, pid = _brain("airef")
    p = db.get_project(conn, "airef")
    d = dashboard.gather(conn, p)
    assert (d["ai_referrals"], d["ai_referral_pages"], d["ai_referral_meta"]) == (None, None, None), d["ai_referrals"]

    db.write_ga4_ai_referrals(conn, pid, PREV, 28, ["chatgpt.com"], [])
    d = dashboard.gather(conn, p)
    assert d["ai_referrals"] == [] and d["ai_referral_pages"] == [], d["ai_referrals"]
    assert d["ai_referral_meta"] == {"date": PREV, "period_days": 28, "hosts": ["chatgpt.com"]}

    # 최신 잰 날 한 벌만 — 출처별 합계와 페이지별(출처별 세션 포함)이 세션 내림차순.
    db.write_ga4_ai_referrals(conn, pid, D, 28, ["chatgpt.com", "perplexity.ai"],
                              [("chatgpt.com", "/a", 5, 1), ("perplexity.ai", "/a", 2, 0),
                               ("perplexity.ai", "/b", 9, 0.5)])
    d = dashboard.gather(conn, p)
    assert [(r["source"], r["sessions"]) for r in d["ai_referrals"]] == \
        [("perplexity.ai", 11), ("chatgpt.com", 5)], d["ai_referrals"]
    assert [(r["page"], r["sessions"], r["key_events"]) for r in d["ai_referral_pages"]] == \
        [("/b", 9, 0.5), ("/a", 7, 1.0)], d["ai_referral_pages"]
    assert d["ai_referral_pages"][1]["sources"] == {"chatgpt.com": 5, "perplexity.ai": 2}
    assert d["ai_referral_meta"]["date"] == D
    conn.close()


def test_ai_visits_reach_the_brief_past_the_top_100():
    """요청문은 **그 페이지** 의 AI 방문을 찾는다 — 화면 목록의 상위 100 밖이어도.

    화면 목록(ai_referral_pages)은 세션 순 상위 100 에서 자른다. 요청문이 그 목록에서만
    찾으면, 긴 꼬리의 페이지가 기회에 걸렸을 때 방문이 있어도 그 줄이 안 선다.
    """
    import brief
    conn, pid = _brain("airef_tail")
    rows = [("chatgpt.com", f"/p{i:03d}", 200 - i, 0) for i in range(150)]
    rows.append(("perplexity.ai", "/tail", 1, 0))
    db.write_ga4_ai_referrals(conn, pid, D, 28, ["chatgpt.com", "perplexity.ai"], rows)
    d = dashboard.gather(conn, db.get_project(conn, "airef_tail"))
    assert len(d["ai_referral_pages"]) == 100
    assert not any(r["page"] == "/tail" for r in d["ai_referral_pages"])
    got = dashboard._ai_referrals_in_play(conn, pid, ["https://airef.example/tail"])
    assert got["/tail"]["sessions"] == 1 and got["/tail"]["sources"] == {"perplexity.ai": 1}, got
    # 안 쟀으면 아무 키도 없다 — 0 이라고 지어내지 않는다
    assert dashboard._ai_referrals_in_play(conn, pid, ["https://airef.example/none"]) == {}
    conn.close()
    # 요청문은 그 몫을 먼저 본다
    ctx = {"ai_referral_meta": d["ai_referral_meta"], "ai_referral_pages": d["ai_referral_pages"],
           "ai_referrals_in_play": got}
    ev, _after = brief._ai_visits({"kind": "ai_citation_gap"}, ctx, "https://airef.example/tail")
    assert ev and "세션 1" in ev[0] and "perplexity.ai 1" in ev[0], ev


if __name__ == "__main__":
    import shutil
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    try:
        for t in tests:
            t()
            print(f"  ok  {t.__name__}")
        print(f"\n{len(tests)} passed  ({HOME})")
    finally:
        shutil.rmtree(HOME, ignore_errors=True)
