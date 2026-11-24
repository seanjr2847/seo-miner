#!/usr/bin/env python3
"""지역 업체(collect_local)·쇼핑 가격(collect_merchant) — 고정 응답으로 본다. 진짜 API 0건.

응답 모양은 DataForSEO 공식 문서의 칸 이름 그대로다(각 모듈 docstring 의 주소). 필드가 빠진
항목·남의 업체·늦는 과제를 일부러 섞는다.
"""
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

HOME = Path(tempfile.mkdtemp(prefix="seo-miner-local-test-"))
os.environ["CAPTURE_HOME"] = str(HOME)
os.environ["DATAFORSEO_LOGIN"] = "login"      # 가짜 post 만 부른다 — 실제 호출 0건
os.environ["DATAFORSEO_PASSWORD"] = "pw"
sys.path.insert(0, str(Path(__file__).parent))

import brief            # noqa: E402
import collect_local    # noqa: E402
import collect_merchant  # noqa: E402
import db               # noqa: E402
import run_all          # noqa: E402
import run_verdict      # noqa: E402
import serp_adapter     # noqa: E402

FAILS: list[str] = []


def check(name: str, fn) -> None:
    try:
        fn()
        print(f"  ok  {name}")
    except Exception as e:      # noqa: BLE001
        FAILS.append(name)
        print(f"FAIL  {name}: {type(e).__name__}: {e}")


def _site(name: str, ptype: str, locale: str = "ko-KR", domain: str | None = None):
    conn = db.connect()
    conn.execute("INSERT INTO projects(name, domain, locale, type) VALUES(?,?,?,?)",
                 (name, domain or f"{name}.com", locale, ptype))
    conn.commit()
    return conn, db.get_project(conn, name)["id"]


def _kw(conn, pid, kw, feats, pos=None, url=None, vol=0):
    kid = conn.execute("INSERT INTO keywords(project_id, keyword, is_active, volume)"
                       " VALUES(?,?,1,?)", (pid, kw, vol)).lastrowid
    conn.execute("INSERT INTO rank_snapshots(keyword_id, position, url, serp_features_json)"
                 " VALUES(?,?,?,?)", (kid, pos, url, json.dumps(feats)))
    conn.commit()


def _biz(title, cid, *, url=None, rating=None, votes=None, rank=None, lat=37.5, lng=127.0,
         kind="maps_search", **more):
    it = {"type": kind, "title": title, "cid": cid, "url": url, "address": f"{title} 주소",
          "phone": "02-000-0000", "category": "피부과", "latitude": lat, "longitude": lng,
          "rank_group": rank}
    if rating is not None:
        it["rating"] = {"rating_type": "Max5", "value": rating, "votes_count": votes}
    it.update(more)
    return it


class FakeTasks:
    """과제형 가짜 — post 가 번호를 주고 wait 가 준비된 결과를 돌려준다."""

    def __init__(self, results: dict, late=()):
        self.results, self.late, self.posted = results, set(late), []

    def post(self, path, body):
        self.posted.append((path, body[0]))
        key = body[0].get("cid") or body[0].get("keyword")
        return f"task-{key}", 0.003

    def wait(self, paths, timeout_s):
        out = {}
        for k, path in paths.items():
            assert path.endswith(f"task-{k}"), path
            out[k] = ("waiting", None) if k in self.late else ("done", self.results.get(k) or [])
        return out


def _reviews(total, rating, ages, replied):
    now = datetime.now(timezone.utc)
    items = [{"type": "google_reviews_search", "rating": {"value": 5},
              "timestamp": (now - timedelta(days=a)).strftime("%Y-%m-%d %H:%M:%S +00:00"),
              "owner_answer": "감사합니다" if i < replied else None}
             for i, a in enumerate(ages)]
    return [{"reviews_count": total, "rating": {"value": rating}, "items": items}]


def _local_post(calls, own="clinic.com"):
    def post(path, body):
        b = body[0]
        calls.append((path, b))
        if path == collect_local.GBP_PATH:
            if b["keyword"] == "디아더 피부과":
                return [{"items": [
                    _biz("디아더 의원(남의 곳)", "999", url="https://other.com/", kind="google_business_info"),
                    _biz("디아더 피부과", "111", url=f"https://www.{own}/", rating=4.6, votes=12,
                         kind="google_business_info", category_ids=["dermatologist"],
                         is_claimed=True)]}], 0.0054
            return [{"items": None}], 0.0054
        if path == collect_local.MAPS_PATH:
            la, lo, z = b["location_coordinate"].split(",")
            assert z == collect_local.MAPS_ZOOM and b["depth"] == collect_local.MAPS_DEPTH, b
            center = (float(la), float(lo)) == (37.5, 127.0)
            items = [_biz("A 피부과", "201", url="https://a.com", rating=4.8, votes=300, rank=1),
                     {"type": "maps_paid_item", "title": "광고"},
                     _biz("B 피부과", "202", rating=4.2, votes=140, rank=2),
                     {"type": "maps_search", "title": "식별자 없는 곳"},
                     _biz("C 피부과", "203", rating=4.9, votes=90, rank=3)]
            if center:      # 소재지에서만 우리가 4위로 보인다
                items.append(_biz("디아더 피부과", "111", url=f"https://{own}", rating=4.6,
                                  votes=12, rank=4))
            return [{"items": items}], 0.002
        if path == collect_local.LISTINGS_PATH:
            assert b["categories"] == ["dermatologist"] and b["location_coordinate"] == "37.5,127.0,1", b
            return [{"items": [
                _biz("디아더 피부과", "111", url=f"https://{own}", kind="business_listing"),
                _biz("D 피부과", "204", rating=4.0, votes=20, kind="business_listing")]}], 0.011
        raise AssertionError(f"모르는 엔드포인트: {path}")
    return post


def test_local_collects_all_axes():
    conn, pid = _site("clinic", "local_business")
    db.set_project_settings(conn, pid, {"brand_aliases": ["디아더 피부과"]})
    _kw(conn, pid, "강남 피부과", ["local_pack", "people_also_ask"], vol=900)
    _kw(conn, pid, "피부과 추천", ["local_pack"], vol=100)
    _kw(conn, pid, "여드름 원인", ["featured_snippet"], vol=5000)
    calls: list = []
    tasks = FakeTasks({"111": _reviews(12, 4.6, [200, 300], 0),
                       "201": _reviews(300, 4.8, [1, 5, 40, 400], 3),
                       "202": _reviews(140, 4.2, [10, 20, 500], 1),
                       "203": _reviews(90, 4.9, [3, 700], 2)})

    def osm(q):
        raise AssertionError("한국은 OSM 을 끄는 지역인데 불렀다")

    r = collect_local.collect("clinic", conn=conn, post=_local_post(calls), tasks=tasks, osm=osm)
    assert r.ok and not r.skipped, r
    paths = [c[0] for c in calls]
    assert paths.count(collect_local.GBP_PATH) == 1
    assert paths.count(collect_local.MAPS_PATH) == 18, "검색어 2개 × 3×3 격자여야 한다"
    assert {c[1]["keyword"] for c in calls if c[0] == collect_local.MAPS_PATH} == {
        "강남 피부과", "피부과 추천"}, "지도 칸이 안 선 검색어를 쟀다"
    d = collect_local.latest(conn, pid, "ko-KR")
    assert d["gbp"]["name"] == "디아더 피부과" and d["gbp"]["phone"] == "02-000-0000", d["gbp"]
    k0 = d["keywords"][0]
    assert k0["keyword"] == "강남 피부과" and k0["position"] == 4, k0
    assert [x["name"] for x in k0["rivals"]] == ["A 피부과", "B 피부과", "C 피부과"], k0["rivals"]
    g0 = d["grid"][0]
    assert (g0["n"], g0["total"], g0["found"]) == (3, 9, 1), g0
    assert [c["position"] for c in g0["cells"]][4] == 4, "한가운데가 소재지가 아니다"
    assert [x["name"] for x in d["listings"]] == ["D 피부과"], "우리 업체가 경쟁 목록에 들었다"
    # 리뷰 — 같은 지도 칸 상위 업체 중앙값 대비. 고정 기준(10개·4.5점)이 아니다.
    bench = {b["what"]: b for b in d["reviews"]["bench"]}
    assert bench["리뷰 수"]["mine"] == 12 and bench["리뷰 수"]["median"] == 140, bench
    assert bench["리뷰 수"]["behind"] and bench["리뷰 수"]["n"] == 3
    assert bench["평점"]["median"] == 4.8 and bench["평점"]["behind"], bench["평점"]
    assert bench["사장 답글 비율"]["mine"] == 0 and bench["사장 답글 비율"]["behind"]
    recent = next(v for k, v in bench.items() if k.startswith("최근"))
    assert recent["mine"] == 0 and recent["median"] == 2, recent
    assert d["osm"] is None and d["platforms"][1:] == ["네이버 플레이스", "카카오맵"]
    run = conn.execute("SELECT * FROM runs WHERE kind='local' AND project_id=?", (pid,)).fetchone()
    assert abs(run["cost_estimate_usd"] - (0.0054 + 18 * 0.002 + 0.011 + 4 * 0.003)) < 1e-9, \
        run["cost_estimate_usd"]
    assert "gbp=1" in run["notes"] and "grid=18" in run["notes"], run["notes"]
    # 신선하면 다시 사지 않는다
    before = len(calls)
    r2 = collect_local.collect("clinic", conn=conn, post=_local_post(calls), tasks=tasks, osm=osm)
    assert r2.skipped and len(calls) == before, "30일 안인데 다시 샀다"
    conn.close()


def test_local_wrong_type_and_no_key():
    conn, pid = _site("shop1", "commerce")
    boom = lambda *a, **k: (_ for _ in ()).throw(AssertionError("부르면 안 된다"))
    r = collect_local.collect("shop1", conn=conn, post=boom)
    assert r.skipped and "지역 비즈니스가 아니라" in r.reason, r
    assert not conn.execute("SELECT 1 FROM runs WHERE kind='local' AND project_id=?", (pid,)).fetchone(), \
        "종류가 다른 사이트에 런 기록을 남겼다"
    conn.close()
    conn, pid = _site("clinic2", "local_business")
    keys = {k: os.environ.pop(k) for k in ("DATAFORSEO_LOGIN", "DATAFORSEO_PASSWORD")}
    try:
        r = collect_local.collect("clinic2", conn=conn, post=boom)
    finally:
        os.environ.update(keys)
    assert r.ok and r.skipped and "[사람 확인]" in r.reason, r
    notes = conn.execute("SELECT notes FROM runs WHERE project_id=? AND kind='local'",
                         (pid,)).fetchone()[0]
    j = run_verdict.judge({"notes": notes, "finished_at": "t"})
    assert j["blocked"] and "DataForSEO" in j["reason"], notes
    d = collect_local.axis(conn, db.get_project(conn, "clinic2"))["local"]
    assert d["checked"] is None and d["gbp"] is None and "DataForSEO" in d["last_note"], d
    conn.close()


def test_local_profile_not_ours_and_osm():
    """프로필이 남의 것뿐이면 안 채운다. 좌표를 주면 OSM(무료)은 유럽·북미에서 돈다."""
    conn, pid = _site("praxis", "local_business", "de-DE", "praxis.de")
    calls: list = []

    def post(path, body):
        calls.append(path)
        assert path in (collect_local.GBP_PATH, collect_local.MAPS_PATH), path
        if path == collect_local.GBP_PATH:
            return [{"items": [_biz("Praxis Muster", "900", url="https://other.de",
                                    kind="google_business_info")]}], 0.0054
        return [{"items": []}], 0.002

    seen = {}

    def osm(q):
        seen["q"] = q
        return {"elements": [
            {"type": "node", "id": 1, "lat": 52.5, "lon": 13.4,
             "tags": {"name": "Praxis", "amenity": "dentist", "website": "https://www.praxis.de/"}},
            {"type": "way", "id": 2, "center": {"lat": 52.51, "lon": 13.41},
             "tags": {"name": "Zahnarzt Nord", "amenity": "dentist", "addr:street": "Hauptstr."}},
            {"type": "node", "id": 3, "lat": 52.5, "lon": 13.4,
             "tags": {"name": "Café Eins", "amenity": "cafe"}}]}

    r = collect_local.collect("praxis", conn=conn, post=post, osm=osm,
                              tasks=FakeTasks({}), coordinate="52.5,13.4", grid=1)
    assert r.ok, r
    assert "around:1000,52.5,13.4" in seen["q"] and "[out:json]" in seen["q"], seen["q"]
    d = collect_local.latest(conn, pid, "de-DE")
    assert d["gbp"] is None, "사이트 주소가 다른 프로필을 우리 것으로 적었다"
    assert d["osm"]["category"] == "amenity=dentist"
    assert [x["name"] for x in d["osm"]["rivals"]] == ["Zahnarzt Nord"], d["osm"]
    notes = conn.execute("SELECT notes FROM runs WHERE kind='local' AND project_id=?", (pid,)).fetchone()[0]
    assert "Praxis Muster" in notes and "[사람 확인]" in notes, notes
    assert collect_local.osm_rivals({"elements": []}, ["x"], "praxis.de") == (None, [])
    conn.close()


def test_local_review_task_late():
    conn, pid = _site("clinic3", "local_business", domain="clinic.com")
    db.set_project_settings(conn, pid, {"brand_aliases": ["디아더 피부과"]})
    _kw(conn, pid, "강남 피부과", ["local_pack"])
    r = collect_local.collect("clinic3", conn=conn, post=_local_post([]), grid=1,
                              tasks=FakeTasks({}, late={"111", "201", "202", "203"}))
    assert r.ok, r
    d = collect_local.latest(conn, pid, "ko-KR")
    # 과제가 안 끝나도 수·평점은 지도 행이 안다 — 그 둘만 견준다
    assert [b["what"] for b in d["reviews"]["bench"]] == ["리뷰 수", "평점"], d["reviews"]["bench"]
    assert "안 끝났습니다" in conn.execute("SELECT notes FROM runs WHERE kind='local' AND project_id=?", (pid,)).fetchone()[0]
    conn.close()


def test_review_stats_and_bench():
    now = datetime(2026, 11, 24, tzinfo=timezone.utc)
    s = collect_local.review_stats([{"reviews_count": 50, "rating": {"value": 4.4}, "items": [
        {"timestamp": "2026-11-20 08:00:00 +00:00", "owner_answer": "네"},
        {"timestamp": "2026-05-01 08:00:00 +00:00", "owner_answer": ""},
        {"timestamp": "깨짐"}]}], now)
    assert s == {"total": 50, "rating": 4.4, "fetched": 3, "latest": "2026-11-20",
                 "recent": 1, "replied": 1}, s
    assert collect_local.review_stats([]) is None
    assert collect_local.review_bench(None, [{"reviews": 5}]) == []
    b = collect_local.review_bench({"reviews": 50, "rating": None}, [{"reviews": 10}, {"reviews": 30}])
    assert b == [{"what": "리뷰 수", "mine": 50, "median": 20, "n": 2, "behind": False}], b
    src = Path(collect_local.__file__).read_text("utf-8")
    assert "4.5" not in src.split("def review_bench")[1].split("\ndef ")[0].replace(
        "10개·4.5점", ""), "리뷰 판정에 고정 기준이 들어갔다"


def test_brief_nap_uses_measured_profile():
    ctx = {"project": {"domain": "clinic.com"}, "page_audits": {},
           "rank_by_kw": {"강남 피부과": {"features": ["local_pack"]}}}
    o = {"target": "강남 피부과", "kind": "striking_distance"}
    base = "\n".join(brief._nap_lines(ctx))
    assert "[사람 확인]" in base and "조회" not in base
    assert brief.nap_deliver(ctx) == brief.NAP_DELIVER
    ctx["local"] = {
        "checked": "2026-11-24", "gating": collect_local.REVIEW_GATING,
        "platforms": ["구글 비즈니스 프로필", "네이버 플레이스"],
        "gbp": {"name": "디아더 피부과", "address": "서울 강남구 1", "phone": "02-000-0000"},
        "reviews": {"bench": [{"what": "리뷰 수", "mine": 12, "median": 140, "n": 3, "behind": True},
                              {"what": "평점", "mine": 4.6, "median": 4.5, "n": 3, "behind": False}]}}
    got = "\n".join(brief._nap_lines(ctx))
    assert "디아더 피부과 | 서울 강남구 1 | 02-000-0000" in got and "2026-11-24" in got, got
    assert "리뷰 수 12, 같은 지도 칸 상위 3곳의 중앙값 140" in got, got
    assert "평점 4.6" not in got, "뒤지지 않는 값을 지적했다"
    assert "리뷰 게이팅" in got and "네이버 플레이스" in got, got
    assert brief.nap_deliver(ctx) == brief.NAP_DELIVER_MEASURED and brief._local_pack(o, ctx)
    assert "[사람 확인]으로 비워" not in brief.nap_deliver(ctx)
    # 프로필을 못 찾은 회차(gbp None)는 사람 확인으로 남는다 — 플랫폼 줄은 그대로 선다
    ctx["local"]["gbp"] = None
    got = "\n".join(brief._nap_lines(ctx))
    assert brief.nap_deliver(ctx) == brief.NAP_DELIVER and "네이버 플레이스" in got


def _shop_results(own="shop.com"):
    def it(rank, title, seller, domain, price, cur="KRW", desc=None, **more):
        return {"type": "google_shopping_serp", "rank_group": rank, "title": title,
                "seller": seller, "domain": domain, "price": price, "currency": cur,
                "description": desc, "product_rating": {"value": 4.5, "votes_count": 10},
                "reviews_count": 10, "shopping_url": f"https://g/{rank}", **more}
    maker = "제조사가 쓴 공식 상품 설명입니다. 소재는 면 100% 이고 세탁기로 빨 수 있습니다."
    return [{"items": [
        {"type": "google_shopping_paid", "title": "광고 상품", "price": 1},
        it(1, "면 티셔츠 A", "쇼핑몰A", "a.com", 10000, desc=maker),
        it(2, "면 티셔츠 우리", "우리몰", f"www.{own}", 30000, desc=maker),
        it(3, "면 티셔츠 C", "쇼핑몰A", "a.com", 20000, desc="다른 설명"),
        it(4, "달러 상품", "해외몰", "x.com", 9.9, cur="USD"),
        it(5, "값 없는 상품", "쇼핑몰B", "b.com", None),
        it(6, "면 티셔츠 D", "쇼핑몰B", "b.com", 40000, desc=maker)]}]


def test_merchant_collects_and_classifies():
    conn, pid = _site("shop", "commerce")
    _kw(conn, pid, "면 티셔츠", ["shopping", "people_also_ask"], pos=3,
        url="https://shop.com/tee", vol=500)
    _kw(conn, pid, "린넨 셔츠", ["popular_products"], pos=14, url="https://shop.com/linen", vol=300)
    _kw(conn, pid, "셔츠 세탁법", ["featured_snippet"], pos=2, vol=9000)
    tasks = FakeTasks({"면 티셔츠": _shop_results(), "린넨 셔츠": [{"items": [
        {"type": "google_shopping_serp", "rank_group": 1, "title": "린넨 A", "seller": "쇼핑몰A",
         "domain": "a.com", "price": 50000, "currency": "KRW"}]}]})
    boom = lambda *a, **k: (_ for _ in ()).throw(AssertionError("한국에는 아마존 마켓이 없다"))
    r = collect_merchant.collect("shop", conn=conn, post=boom, tasks=tasks)
    assert r.ok and not r.skipped, r
    assert [b["keyword"] for _, b in tasks.posted] == ["면 티셔츠", "린넨 셔츠"], tasks.posted
    assert all(b["priority"] == 2 and b["location_name"] == "South Korea" for _, b in tasks.posted)
    audits = {"https://shop.com/tee": {"url": "https://shop.com/tee", "schema_json": '["WebPage"]',
                                       "schema_gaps_json": "[]", "title": "t", "js_shell": 0},
              "https://shop.com/linen": {"url": "https://shop.com/linen", "js_shell": 0,
                                         "schema_json": '["Product"]', "title": "t",
                                         "schema_gaps_json": json.dumps(
                                             [{"type": "Product", "need": ["offers"], "want": []}])}}
    d = collect_merchant.axis(conn, db.get_project(conn, "shop"), audits)["merchant"]
    m = next(x for x in d["markets"] if x["keyword"] == "면 티셔츠")
    dist = m["dist"]
    assert (dist["currency"], dist["n"], dist["min"], dist["median"], dist["max"]) == (
        "KRW", 4, 10000, 25000, 40000), dist
    assert dist["ours"] == [{"title": "면 티셔츠 우리", "price": 30000.0, "position": 2,
                             "cheaper": 2}], dist["ours"]
    assert m["sellers"][0] == ["쇼핑몰A", 2] and m["products"] == 6, m
    cls = {c["keyword"]: c["cls"] for c in d["classes"]}
    assert cls == {"면 티셔츠": "both", "린넨 셔츠": "neither"}, cls
    assert all(c["label"] == collect_merchant.CLASSES[c["cls"]] for c in d["classes"])
    assert {(x["keyword"], x["why"]) for x in d["no_product"]} == {
        ("면 티셔츠", "Product 없음"), ("린넨 셔츠", "offers 없음")}, d["no_product"]
    assert d["copied"] == [{"keyword": "면 티셔츠", "title": "면 티셔츠 우리",
                            "sellers": ["쇼핑몰A", "쇼핑몰B"]}], d["copied"]
    run = conn.execute("SELECT * FROM runs WHERE kind='merchant' AND project_id=?", (pid,)).fetchone()
    assert abs(run["cost_estimate_usd"] - 0.006) < 1e-9 and "아마존 마켓이 없어" in run["notes"], dict(run)
    conn.close()


def test_merchant_amazon_by_locale_and_skips():
    conn, pid = _site("laden", "commerce", "de-DE", "laden.de")
    _kw(conn, pid, "leinenhemd", ["shopping"], pos=None)
    calls = []

    def post(path, body):
        calls.append((path, body[0]))
        return [{"items": [
            {"type": "amazon_paid", "title": "Anzeige", "price_from": 1.0},
            {"type": "amazon_serp", "rank_group": 1, "title": "Hemd A", "price_from": 29.9,
             "currency": "EUR", "rating": {"value": 4.4, "votes_count": 120}, "data_asin": "B0"},
            {"type": "amazon_serp", "rank_group": 2, "title": "Hemd B", "price_from": 49.9,
             "currency": "EUR"}]}], 0.0033

    r = collect_merchant.collect("laden", conn=conn, post=post,
                                 tasks=FakeTasks({}, late={"leinenhemd"}))
    assert r.ok, r
    assert calls[0][0] == collect_merchant.AMAZON_PATH and calls[0][1]["se_domain"] == "amazon.de"
    assert calls[0][1]["language_code"] == "de_DE" and calls[0][1]["location_name"] == "Germany"
    d = collect_merchant.view(conn, pid)
    assert [(m["market"], m["dist"]["median"]) for m in d["markets"]] == [("amazon.de", 39.9)], d
    assert d["classes"][0]["cls"] is None, "구글 쇼핑을 못 받았는데 '없음'으로 갈랐다"
    conn.close()
    # 종류가 다르면 조용히, 쇼핑 칸 검색어가 없으면 사유를 남기고 건너뛴다
    boom = lambda *a, **k: (_ for _ in ()).throw(AssertionError("부르면 안 된다"))
    conn, pid = _site("saas1", "saas")
    assert collect_merchant.collect("saas1", conn=conn, post=boom).skipped
    assert collect_merchant.axis(conn, db.get_project(conn, "saas1")) == {"merchant": None}
    assert collect_local.axis(conn, db.get_project(conn, "saas1")) == {"local": None}
    conn.close()
    conn, pid = _site("shop9", "commerce")
    r = collect_merchant.collect("shop9", conn=conn, post=boom, tasks=FakeTasks({}))
    assert r.ok and r.skipped and "쇼핑 칸이 선" in r.reason, r
    assert "쇼핑 칸이 선" in collect_local.last_note(conn, pid, "merchant")
    conn.close()


def test_stage_wiring():
    """새 단계가 단계표·묶음·잔액 카나리아에 다 걸렸다 — 한 곳만 빠지면 화면에서 못 돌린다."""
    for name, mod in (("local", collect_local), ("merchant", collect_merchant)):
        s = run_all.STAGE_BY_NAME[name]
        assert s.is_paid and s.module is mod, name
        assert name in run_all.DFS_STAGES, f"{name} 가 잔액 카나리아에 없다"
        assert name in run_all.GROUP_BY_ID["compete"]["stages"], name
        assert "rank" in run_all.AFTER[name], "지도·쇼핑 칸 검색어는 rank 가 먼저 안다"
        assert "max_age" in s.knobs and "conn" not in s.knobs, s.knobs
    assert set(collect_local.PLATFORMS_BY_REGION) | set(collect_merchant.AMAZON_MARKETS) <= {
        c.split("-")[1] for c, _ in serp_adapter.LOCALES}
    # 로케일 표는 한 벌 — 고를 수 있는 모든 로케일이 플랫폼 목록을 갖는다(나라를 거르지 않는다)
    assert all(collect_local.platforms(c) for c, _ in serp_adapter.LOCALES)


def test_chain_notes_no_key_only_for_that_site_type():
    """체인(run_all)에서 키가 없으면 수집기를 부르지 않고 건너뛴다 — 사유는 그 종류의 사이트에만
    runs 로 남는다(웹 서비스 사이트의 기록에 '지역 업체: 키 없음'이 쌓이지 않는다)."""
    conn, lb = _site("clinic8", "local_business")
    conn.close()
    conn, sa = _site("saas8", "saas")
    conn.close()
    keys = {k: os.environ.pop(k) for k in ("DATAFORSEO_LOGIN", "DATAFORSEO_PASSWORD")}
    try:
        for name in ("clinic8", "saas8"):
            for st in ("local", "merchant"):
                boom = lambda **k: (_ for _ in ()).throw(AssertionError("키가 없는데 수집기를 불렀다"))
                stg = run_all.STAGE_BY_NAME[st]._replace(fn=boom)
                r = run_all._run_stage(stg, name, dry_run=False, skip_set=set(), only_set=set(),
                                       opts={})
                assert r.ok and r.skipped and "DataForSEO" in r.reason, (name, st, r)
    finally:
        os.environ.update(keys)
    conn = db.connect()
    got = [(r["project_id"], r["kind"]) for r in conn.execute(
        "SELECT project_id, kind FROM runs WHERE project_id IN (?,?)", (lb, sa))]
    assert got == [(lb, "local")], got
    assert "DataForSEO" in collect_local.last_note(conn, lb, "local")
    conn.close()


def test_task_helpers():
    """과제형 도우미 — 늦는 과제는 상한까지 두드리고 waiting 으로 남긴다."""
    seq = {"/a": [("waiting", None), ("done", [1])], "/b": [("waiting", None)] * 9}
    t = [0.0]

    def get(path):
        return seq[path].pop(0) if len(seq[path]) > 1 else seq[path][0]

    out = serp_adapter.wait_dataforseo_tasks(
        {"a": "/a", "b": "/b"}, timeout_s=25, get=get, clock=lambda: t[0],
        sleep=lambda s: t.__setitem__(0, t[0] + s))
    assert out == {"a": ("done", [1]), "b": ("waiting", None)}, out


# ── 실제 화면 ────────────────────────────────────────────────────────────────
# [경쟁 검색어] → [전체] 탭까지 눌러 들어가 두 카드를 읽어 DOM(#__lc__)에 옮겨 적는다.
RUN = """<script>
(function () {
  var wait = function (ms) { return new Promise(function (r) { setTimeout(r, ms); }); };
  var one = function (id) {
    var s = document.getElementById(id), c = s.querySelector(".card"), r = c.getBoundingClientRect();
    var typo = [].map.call(c.querySelectorAll("h2,h3,th,td,dt,dd,p,figcaption,small,span"), function (e) {
      var st = getComputedStyle(e); return st.letterSpacing + "|" + st.textTransform; })
      .filter(function (v) { return v !== "normal|none" && v !== "0px|none"; });
    return {box: s.parentNode.id, disp: getComputedStyle(c).display, w: Math.round(r.width),
            text: c.innerText.replace(/\\s+/g, " "), typo: typo, over: c.scrollWidth > c.clientWidth + 1,
            cells: [].map.call(c.querySelectorAll(".lc-cells span"), function (x) { return x.textContent; })};
  };
  (async function () {
    var out = {};
    try {
      await wait(2500);
      SM.touched = true; SM.show("competitors"); await wait(400);
      var box = document.getElementById("view-competitors");
      box.querySelector(':scope > .vtabs [data-mode="manage"]').click(); await wait(1200);
      out.local = one("sm-local"); out.shop = one("sm-shop");
    } catch (e) { out.err = String(e && e.stack || e); }
    var d = document.createElement("div"); d.id = "__lc__";
    d.textContent = JSON.stringify(out); document.body.appendChild(d);
  })();
})();
</script>"""


def _seed_screens(tr) -> None:
    """test_render 의 두 사이트를 지역 업체·쇼핑몰로 바꾸고 가짜 응답으로 한 바퀴 수집한다."""
    lb, sh = tr.SITES[1], tr.SITES[0]
    conn = db.connect()
    try:
        conn.execute("UPDATE projects SET type='local_business', locale='ko-KR' WHERE name=?", (lb,))
        conn.execute("UPDATE projects SET type='commerce', locale='ko-KR' WHERE name=?", (sh,))
        conn.commit()
        pid = db.get_project(conn, lb)["id"]
        db.set_project_settings(conn, pid, {"brand_aliases": ["디아더 피부과"]})
        _kw(conn, pid, "강남 피부과", ["local_pack"], vol=900)
        r = collect_local.collect(lb, conn=conn, post=_local_post([], f"{lb}.example"),
                                  tasks=FakeTasks({"111": _reviews(12, 4.6, [200, 300], 0),
                                                   "201": _reviews(300, 4.8, [1, 5], 2),
                                                   "202": _reviews(140, 4.2, [10], 1),
                                                   "203": _reviews(90, 4.9, [3], 1)}))
        assert r.ok and not r.skipped, r
        pid = db.get_project(conn, sh)["id"]
        _kw(conn, pid, "면 티셔츠", ["shopping"], pos=3, url=f"https://{sh}.example/a", vol=500)
        boom = lambda *a, **k: (_ for _ in ()).throw(AssertionError("한국에는 아마존 마켓이 없다"))
        r = collect_merchant.collect(sh, conn=conn, post=boom,
                                     tasks=FakeTasks({"면 티셔츠": _shop_results(f"{sh}.example")}))
        assert r.ok and not r.skipped, r
    finally:
        conn.close()


def _read_screen(label: str, html: str) -> dict:
    import html as htmlmod
    import re
    m = re.search(r'id="__lc__"[^>]*>(.*?)</div>', html, re.S)
    assert m, f"{label}: 화면 검사 스크립트가 결과를 못 남겼다"
    o = json.loads(htmlmod.unescape(m.group(1)))
    assert not o.get("err"), f"{label}: 검사 스크립트가 터졌다: {o.get('err')}"
    for k in ("local", "shop"):
        assert o[k]["box"] == "view-competitors", f"{label}: 섹션이 [경쟁 검색어] 화면 밖에 있다 {o[k]['box']}"
    return o


def _judge_local(label: str, o: dict) -> None:
    c = o["local"]
    assert c["disp"] != "none" and c["w"] > 300, f"{label}: 지역 업체 카드가 안 보인다 {c['disp']} {c['w']}"
    for want in ("디아더 피부과", "02-000-0000", "강남 피부과", "4위", "A 피부과", "상위 업체 중앙값",
                 "리뷰 게이팅", "네이버 플레이스", "9개 구역 중 1곳에서 노출", "D 피부과"):
        assert want in c["text"], f"{label}: 지역 업체 카드에 '{want}' 이 없다: {c['text'][:400]}"
    assert c["cells"] == ["밖"] * 4 + ["4"] + ["밖"] * 4, f"{label}: 격자가 3×3 한가운데 4위가 아니다 {c['cells']}"
    assert not c["typo"], f"{label}: 한글 칸에 자간·대문자 변환이 걸렸다 {c['typo'][:3]}"
    assert not c["over"], f"{label}: 지역 업체 카드가 옆으로 넘친다"
    assert o["shop"]["disp"] == "none", f"{label}: 지역 업체 사이트에 쇼핑 카드가 섰다"


def _judge_shop(label: str, o: dict) -> None:
    c = o["shop"]
    assert c["disp"] != "none" and c["w"] > 300, f"{label}: 쇼핑 카드가 안 보인다 {c['disp']} {c['w']}"
    for want in ("구글 쇼핑", "면 티셔츠", "오가닉·쇼핑 둘 다 노출", "더 싼 상품 2개", "쇼핑몰A 2개",
                 "같은 설명을 쓴 판매처", "25,000 KRW"):
        assert want in c["text"], f"{label}: 쇼핑 카드에 '{want}' 이 없다: {c['text'][:400]}"
    assert not c["typo"], f"{label}: 한글 칸에 자간·대문자 변환이 걸렸다 {c['typo'][:3]}"
    assert o["local"]["disp"] == "none", f"{label}: 쇼핑몰 사이트에 지역 업체 카드가 섰다"


def test_screens_in_a_real_browser():
    """두 카드를 실제 브라우저로 — 로컬(stdlib)과 호스팅(실제 FastAPI 앱) 둘 다. 메뉴에서
    [경쟁 검색어]로 들어가 [전체] 탭을 눌러야 보인다(그 경로를 그대로 누른다)."""
    import gc
    import shutil
    import test_render as tr
    browser = tr.find_browser()
    if not browser:
        print("크롬/엣지가 없어 화면 검사는 건너뜁니다")
        return
    import doctor
    saved_env = dict(os.environ)
    for k in (doctor.MODE_ENV, doctor.TOOL_ENV, doctor.TERMINAL_ENV):
        os.environ.pop(k, None)
    home = Path(tempfile.mkdtemp(prefix="seo-miner-local-screens-"))
    lb, sh = tr.SITES[1], tr.SITES[0]
    try:
        tr.fixture(home)
        _seed_screens(tr)
        import dashboard
        page = dashboard.HTML.replace(b"</head>", tr.PROBE.encode("utf-8") + b"</head>", 1)
        page = page.replace(b"</body>", RUN.encode("utf-8") + b"</body>", 1)
        with tr._stdlib(page) as site:
            for name, judge in ((lb, _judge_local), (sh, _judge_shop)):
                html = tr.dom(browser, f"{site.base}/d#{name}", home / "chrome-profile")
                p = tr.probe(html)
                assert not p["err"], "로컬: JS 가 터졌다:\n  " + "\n  ".join(p["err"][:6])
                judge(f"로컬 {name}", _read_screen(f"로컬 {name}", html))
        print("  화면(로컬): ok")
        if (Path(tr.ROOT) / "server" / "assets" / "dash.html").exists():
            saved = tr.ALIAS_TOUCH       # 호스팅 앱 앞 층이 ?alias_probe=1 요청에 끼우는 조각을 우리 것으로
            tr.ALIAS_TOUCH = RUN
            try:
                with tr.serve_hosted(home / "hosted") as site:
                    _seed_screens(tr)    # fixture 가 CAPTURE_HOME 을 그 유저의 brain 으로 세워 뒀다
                    for name, judge in ((lb, _judge_local), (sh, _judge_shop)):
                        html = tr.dom(browser, f"{site.base}/d?alias_probe=1#{name}",
                                      home / "chrome-profile")
                        p = tr.probe(html)
                        assert not p["err"], "호스팅: JS 가 터졌다:\n  " + "\n  ".join(p["err"][:6])
                        judge(f"호스팅 {name}", _read_screen(f"호스팅 {name}", html))
                    site.after()
                    fails = site.failures()
            finally:
                tr.ALIAS_TOUCH = saved
            assert not fails, "호스팅: 서버가 본 실패 " + "; ".join(fails[:5])
            print("  화면(호스팅): ok")
    finally:
        os.environ.clear()
        os.environ.update(saved_env)
        gc.collect()
        shutil.rmtree(home, ignore_errors=True)


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            check(_name, _fn)
    print(f"\n{'FAIL' if FAILS else 'PASS'}  test_local_commerce ({len(FAILS)} 실패)")
    sys.exit(1 if FAILS else 0)
