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
    d = dashboard.gather(conn, db.get_project(conn, "lp"))
    by_target = {o["target"]: o for o in d["opps"]}

    p1 = by_target["1페이지상단권"]
    assert p1["label"] == "1페이지 상단 가능", p1["label"]
    assert p1["play"]["what"] == "이미 1페이지 안입니다 — 여기서 남은 것은 순위보다 클릭입니다."
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
