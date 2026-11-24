#!/usr/bin/env python3
"""쇼핑 가격 — 쇼핑 칸이 선 검색어의 상위 상품 가격 분포와 판매처 (DataForSEO Merchant).

사이트 타입이 commerce 일 때만 돈다. 쇼핑 칸은 글이 아니라 상품(가격·판매처)이 가져가는
자리라서, 순위만으로는 "왜 못 들어가나"가 안 나온다. 두 마켓을 산다:

  1. /merchant/google/products/task_post → task_get/advanced  → merchant_prices(google_shopping)
     Live 가 없다(과제형). 우선 대기열로 맡기고 기다린다.
  2. /merchant/amazon/products/live/advanced                  → merchant_prices(amazon.de …)
     그 로케일에 아마존 마켓이 있을 때만(AMAZON_MARKETS).

그리고 유료 호출 없이 이미 가진 값으로 셋을 판정한다(view):
  · 쇼핑 칸이 뜨는데 그 검색어의 우리 페이지에 Product+offers 마크업이 없음
  · 오가닉 대 쇼핑 노출 4분류(둘 다·오가닉만·쇼핑만·둘 다 없음)
  · 제조사 설명 복붙 — 우리 쇼핑 설명이 다른 판매처와 글자까지 같다

설계 메모:
  · 가격은 **분포 대비**로만 말한다(상위 N개 중 우리보다 싼 상품 수·중앙값). "비싸다"는 선을
    긋지 않는다. 통화가 섞이면 가장 많은 통화 하나만 센다(환산하지 않는다).
  · 응답 필드는 공식 문서로만 확인했다(개발 중 유료 실호출 금지).
      https://docs.dataforseo.com/v3/merchant/google/products/task_post/ (·task_get/advanced/)
      https://docs.dataforseo.com/v3/merchant/amazon/products/live/advanced/
  · 가격은 하루 단위로 보지 않는다. --max-age(기본 30일) 안이면 다시 사지 않는다.

비용(dry-run 고지용 추정 — 실청구액은 응답 cost 를 합산한다):
  구글 쇼핑 우선 대기열 $0.002/검색어(상품 40개) · 아마존 Live $0.0033/검색어
  https://dataforseo.com/pricing/merchant/google-shopping-api
  https://dataforseo.com/pricing/merchant/amazon-api

Usage:
  python collect_merchant.py --project NAME [--keywords 3] [--max-age 30] [--dry-run]
  python collect_merchant.py                               # self-check
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import unicodedata
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import collect_local  # noqa: E402  — 건너뜀 기록·과제형 호출·지역 뽑기 한 벌
import collector  # noqa: E402
import db  # noqa: E402
import run_verdict  # noqa: E402
import scoring  # noqa: E402
import serp_adapter  # noqa: E402

SITE_TYPE = "commerce"
GOOGLE = "google_shopping"
SHOPPING_POST = "/merchant/google/products/task_post"
SHOPPING_GET = "/merchant/google/products/task_get/advanced/"
AMAZON_PATH = "/merchant/amazon/products/live/advanced"
SHOPPING_COST = 0.002
AMAZON_COST = 0.0033
SHOPPING_DEPTH = 40             # SERP 한 장(40개)이 과금 단위
KEEP = 40                       # 마켓·검색어마다 적어 두는 상위 상품 수
WAIT_S = 300.0                  # 쇼핑 과제를 기다리는 상한(우선 대기열은 대개 1분 안)
SELLERS_TOP = 5

# 검색결과의 쇼핑·상품 칸 — 수집기가 싣는 이름 그대로(DataForSEO organic 의 item type,
# https://docs.dataforseo.com/v3/serp/google/organic/live/advanced/ · Serper 의 "shopping").
# commercial_units 는 뺀다: 문서 예시가 투어 상품이라 물건 파는 칸이 아니다.
SHOPPING_FEATURES = ("shopping", "popular_products", "product_considerations", "refine_products")

# 로케일(지역) → 아마존 마켓. 마켓이 없는 나라(한국·대만·베트남 등)는 아마존을 안 묻는다.
# https://sell.amazon.com/global-selling (2026-11-24 확인)
AMAZON_MARKETS = {
    "US": "amazon.com", "CA": "amazon.ca", "MX": "amazon.com.mx", "BR": "amazon.com.br",
    "GB": "amazon.co.uk", "DE": "amazon.de", "FR": "amazon.fr", "IT": "amazon.it",
    "ES": "amazon.es", "NL": "amazon.nl", "PL": "amazon.pl", "TR": "amazon.com.tr",
    "AE": "amazon.ae", "IN": "amazon.in", "JP": "amazon.co.jp", "AU": "amazon.com.au",
}
# 복붙 판정의 최소 길이 — "무료 배송" 같은 두어 낱말이 같은 것은 복붙이 아니다(판정 기준이
# 아니라 잡음 거르개).
MIN_COPY_CHARS = 30

# 오가닉 대 쇼핑 노출 4분류 — 이름과 화면 문구 한 벌(화면은 페이로드의 label 을 그린다).
CLASSES = {
    "both": "오가닉·쇼핑 둘 다 노출",
    "organic_only": "오가닉만 노출(쇼핑 칸에 우리 상품 없음)",
    "shopping_only": "쇼핑만 노출(오가닉 1페이지 밖)",
    "neither": "둘 다 없음",
}

NO_KEY = ("쇼핑 가격 수집은 DataForSEO 유료 키가 필요합니다. 없어도 기회 목록까지는 그대로 가고, "
          "DATAFORSEO_LOGIN/DATAFORSEO_PASSWORD 를 넣으면 켜집니다. 발급: https://dataforseo.com")


# ── 순수 함수 ────────────────────────────────────────────────────────────────

def amazon_market(locale: str) -> str | None:
    return AMAZON_MARKETS.get(collect_local.region_of(locale))


def amazon_language(locale: str) -> str:
    """아마존의 language_code 는 'en_GB' 꼴이다(문서의 칸 예시) — 구글 축의 'en' 과 다르다."""
    return f"{serp_adapter.lang_of(locale)}_{collect_local.region_of(locale)}"


def classify(organic_pos, in_shopping) -> str | None:
    """오가닉 1페이지(scoring.PAGE1) 안인가 × 쇼핑 결과에 우리 상품이 있나. 쇼핑을 안 쟀으면
    (in_shopping None) 가르지 않는다 — 안 본 것을 '없음'으로 적지 않는다."""
    if in_shopping is None:
        return None
    org = organic_pos is not None and organic_pos <= scoring.PAGE1
    return ("both" if org and in_shopping else "organic_only" if org
            else "shopping_only" if in_shopping else "neither")


def shopping_row(it: dict) -> dict | None:
    """google_shopping_serp 항목 → 우리 꼴."""
    if it.get("type") != "google_shopping_serp" or not it.get("title"):
        return None
    rating = it.get("product_rating") if isinstance(it.get("product_rating"), dict) else {}
    return {"position": collect_local._int(it.get("rank_group")), "title": str(it["title"]),
            "seller": it.get("seller"), "domain": it.get("domain"),
            "url": it.get("shopping_url") or it.get("url"),
            "price": collect_local._num(it.get("price")), "currency": it.get("currency"),
            "rating": collect_local._num(rating.get("value")),
            "reviews": collect_local._int(it.get("reviews_count")
                                          if it.get("reviews_count") is not None
                                          else rating.get("votes_count")),
            "description": it.get("description")}


def amazon_row(it: dict) -> dict | None:
    """amazon_serp 항목 → 우리 꼴. 판매처는 응답에 없다(마켓 자체가 판매 자리다)."""
    if it.get("type") != "amazon_serp" or not it.get("title"):
        return None
    rating = it.get("rating") if isinstance(it.get("rating"), dict) else {}
    return {"position": collect_local._int(it.get("rank_group")), "title": str(it["title"]),
            "seller": None, "domain": it.get("domain"), "url": it.get("url"),
            "price": collect_local._num(it.get("price_from")), "currency": it.get("currency"),
            "rating": collect_local._num(rating.get("value")),
            "reviews": collect_local._int(rating.get("votes_count")), "description": None}


def distribution(rows: list[dict]) -> dict | None:
    """가격 분포 — 가장 많은 통화 하나만 센다. 값이 둘 미만이면 분포가 아니다(None)."""
    priced = [r for r in rows if r.get("price") is not None and r.get("currency")]
    if not priced:
        return None
    cur = Counter(r["currency"] for r in priced).most_common(1)[0][0]
    vals = sorted(r["price"] for r in priced if r["currency"] == cur)
    if len(vals) < 2:
        return None
    q = statistics.quantiles(vals, n=4, method="inclusive")
    ours = [{"title": r["title"], "price": r["price"], "position": r.get("position"),
             "cheaper": sum(1 for v in vals if v < r["price"])}
            for r in priced if r.get("is_self") and r["currency"] == cur]
    return {"currency": cur, "n": len(vals), "min": vals[0], "q1": q[0], "median": q[1],
            "q3": q[2], "max": vals[-1], "ours": ours}


def _fold(s) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(s or "")).lower().split())


def copied(rows: list[dict]) -> list[dict]:
    """우리 쇼핑 설명이 다른 판매처와 글자까지 같은 상품 — 제조사 설명을 그대로 쓴 것이다.
    [{"title", "sellers": [같은 설명을 쓴 다른 판매처]}]"""
    out = []
    for me in (r for r in rows if r.get("is_self")):
        d = _fold(me.get("description"))
        if len(d) < MIN_COPY_CHARS:
            continue
        same = sorted({str(r.get("seller") or r.get("domain") or "") for r in rows
                       if not r.get("is_self") and _fold(r.get("description")) == d} - {""})
        if same:
            out.append({"title": me["title"], "sellers": same})
    return out


def product_gap(audit: dict | None) -> str | None:
    """이 페이지에 쇼핑 칸이 읽을 상품 마크업이 없나 — 'Product 없음'·'offers 없음'·None(있음/모름).

    구글의 판매자 목록(merchant listing)은 Product 에 name·image·offers 를 요구한다:
    https://developers.google.com/search/docs/appearance/structured-data/merchant-listing
    유형·빠진 속성 판정은 페이지 점검이 이미 했다(scoring.SCHEMA_RULES) — 여기는 그 결과만 읽는다."""
    if not audit or audit.get("error") or audit.get("schema_json") is None:
        return None
    gaps = scoring._schema_gaps(audit)
    if gaps is None:
        return None
    types = {str(t) for t in scoring._as_list(audit.get("schema_json"))}
    g = next((x for x in gaps if x.get("type") == "Product"), None)
    if g is None and "Product" not in types:
        return "Product 없음"
    if g and "offers" in set(g.get("need") or []) | set(g.get("want") or []):
        return "offers 없음"
    return None


# ── 대상 고르기·적재 ─────────────────────────────────────────────────────────

def shopping_keywords(conn, pid: int, limit: int | None = None) -> list[dict]:
    """쇼핑 칸이 선 추적 검색어(검색량 많은 순) — [{keyword, position, url}]."""
    rows = conn.execute(
        """SELECT k.keyword, r.position, r.url, r.serp_features_json f
             FROM keywords k JOIN rank_snapshots r ON r.keyword_id=k.id
            WHERE k.project_id=? AND k.is_active=1
              AND r.id=(SELECT MAX(id) FROM rank_snapshots WHERE keyword_id=k.id)
            ORDER BY COALESCE(k.volume,0) DESC, k.keyword""", (pid,)).fetchall()
    out = [{"keyword": r["keyword"], "position": r["position"], "url": r["url"]}
           for r in rows if set(scoring._as_list(r["f"])) & set(SHOPPING_FEATURES)]
    return out[:limit] if limit else out


def _put(conn, pid: int, today: str, market: str, kw: str, rows: list[dict], domain: str) -> int:
    conn.execute("DELETE FROM merchant_prices WHERE project_id=? AND checked_date=? AND market=?"
                 " AND keyword=?", (pid, today, market, kw))
    n = 0
    for i, r in enumerate([x for x in rows if x][:KEEP], 1):
        mine = bool(r.get("domain")) and market == GOOGLE and scoring.owns(r["domain"], domain)
        conn.execute(
            """INSERT INTO merchant_prices(project_id, checked_date, market, keyword, position,
                   title, seller, domain, url, price, currency, rating, reviews, description,
                   is_self) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (pid, today, market, kw, i, r["title"], r.get("seller"), r.get("domain"),
             r.get("url"), r.get("price"), r.get("currency"), r.get("rating"), r.get("reviews"),
             r.get("description"), int(mine)))
        n += 1
    return n


def collect(project: str, *,
            dry_run: bool = False,
            keywords: int | None = None,
            max_age: int | None = None,
            conn=None, post=None, tasks=None,
            **opts) -> collector.StageResult:
    """쇼핑 칸이 선 검색어의 상위 상품 가격을 Brain 에 적재한다.

    Args:
        keywords: 가격을 볼 검색어 수(쇼핑 칸이 선 것 중 검색량 상위). 기본 3
        max_age: 이 일수 안에 이미 샀으면 건너뛴다. 0 이면 항상. 기본 30
        post/tasks: 시험이 갈아 끼우는 자리(DataForSEO Live·과제형)
    """
    ap = _parser()
    post = post or serp_adapter.post_dataforseo
    tasks = tasks or collect_local._Tasks
    with collector.stage(project, conn=conn, dry_run=dry_run) as st:
        conn, p = st.conn, st.project
        if (p["type"] or "") != SITE_TYPE:
            return st.skip("사이트 종류가 쇼핑몰·브랜드가 아니라 건너뜁니다")
        s = st.settings(ap, argparse.Namespace(keywords=keywords, max_age=max_age))
        n_kw, max_age = int(s["merchant_keywords"]), int(s["merchant_max_age_days"])
        pid, today = p["id"], st.today
        domain = (p["domain"] or "").strip().lower()
        locale = db.project_locale(p)
        loc_name, lang, _ = serp_adapter.location(locale)

        def skip(why: str) -> collector.StageResult:
            if not st.dry_run:
                collect_local._note_skip(conn, pid, why, kind="merchant")
                conn.commit()
            print(f"[merchant] {why}")
            return st.skip(why)

        if not serp_adapter.has_dataforseo():
            return skip(NO_KEY)
        if not serp_adapter.dfs_supported(locale):
            return skip(f"{serp_adapter.locale_label(locale)} 은 DataForSEO 가 조회하지 않는 지역입니다")
        if max_age > 0 and conn.execute(
                "SELECT 1 FROM merchant_prices WHERE project_id=? AND checked_date > date('now', ?)"
                " LIMIT 1", (pid, f"-{max_age} days")).fetchone():
            reason = (f"최근 {max_age}일 안에 이미 수집했습니다. 다시 사려면 `--max-age 0`.")
            print(f"[merchant] {reason}")
            return st.noop(reason=reason)
        kws = [k["keyword"] for k in shopping_keywords(conn, pid, n_kw)]
        if not kws:
            return skip("쇼핑 칸이 선 추적 검색어가 없습니다. 순위 확인을 먼저 돌리면 쇼핑 칸이 "
                        "선 검색어가 잡힙니다")
        amz = amazon_market(locale)
        est = len(kws) * (SHOPPING_COST + (AMAZON_COST if amz else 0))
        print(f"[merchant] project={p['name']} locale={locale} keywords={len(kws)} "
              f"amazon={amz or 'off'} est_cost≈${est:.3f}")
        if st.dry_run:
            print("단가 출처: DataForSEO 가격표(모듈 docstring). 실제 청구액은 응답 cost 로 기록.")
            return st.noop(cost=est)

        rows: dict[str, int] = {}
        notes: list[str] = []
        cost = 0.0
        calls = 0
        with st.record("merchant") as r:
            paths: dict = {}
            for kw in kws:
                try:
                    tid, c = tasks.post(SHOPPING_POST, [{
                        "keyword": kw, "location_name": loc_name, "language_code": lang,
                        "depth": SHOPPING_DEPTH, "priority": 2}])
                    calls += 1
                    cost += float(c or 0)
                    paths[kw] = SHOPPING_GET + tid
                except collector.Fatal:
                    raise
                except Exception as e:          # noqa: BLE001 — 한 검색어의 실패가 나머지를 안 버린다
                    st.fail(str(e), item=f"쇼핑 '{kw}'", kind=type(e).__name__)
            if amz:
                def buy(kw):
                    return post(AMAZON_PATH, [{"keyword": kw, "location_name": loc_name,
                                               "language_code": amazon_language(locale),
                                               "se_domain": amz}])

                def put(kw):
                    nonlocal cost, calls
                    res, c = buy(kw)
                    calls += 1
                    cost += float(c or 0)
                    got = [amazon_row(it) for it in collect_local._items(res)]
                    rows[amz] = rows.get(amz, 0) + _put(conn, pid, today, amz, kw, got, domain)
                st.each(kws, put, label=lambda kw: f"아마존 '{kw}'")
            else:
                notes.append(f"{serp_adapter.locale_label(locale)} 에는 아마존 마켓이 없어 "
                             "구글 쇼핑만 봅니다")
            got = tasks.wait(paths, WAIT_S) if paths else {}
            late = 0
            for kw in kws:
                state, val = got.get(kw, (None, None))
                if state == "waiting":
                    late += 1
                elif state == "done":
                    found = [shopping_row(it) for it in collect_local._items(val)]
                    rows[GOOGLE] = rows.get(GOOGLE, 0) + _put(conn, pid, today, GOOGLE, kw, found,
                                                              domain)
                elif state:
                    st.fail(str(val), item=f"쇼핑 '{kw}'", kind=state)
            if late:
                notes.append(f"쇼핑 과제 {late}건이 {int(WAIT_S)}초 안에 안 끝났습니다. "
                             "다음 수집에서 다시 삽니다")
            conn.commit()
            r.api_calls = calls
            r.cost = cost
            r.notes = (" ".join(f"{k}={v}" for k, v in sorted(rows.items())) + f" {st.err_note}"
                       + ("" if not notes else " | " + " | ".join(notes)))[:900]

        total = sum(rows.values())
        print(f"\ncollected {total} rows ({r.notes}), actual_cost=${cost:.4f}\nrun_id={r.id}")
        if not total and not st.errors:
            return st.skip(notes[-1] if notes else "받은 상품이 없습니다")
        return st.verdict(total, rows=total, cost=cost)


# ── 읽기 — 화면(페이로드)이 읽는다 ───────────────────────────────────────────

def view(conn, pid: int, audits: dict | None = None) -> dict:
    """쇼핑 축 한 벌. 유료로 산 값(가격·판매처)이 없어도 순위·페이지 점검만으로 서는 판정
    (Product+offers 없음)은 낸다. audits: {url: page_audits 행} — 페이로드가 이미 읽은 것."""
    kws = shopping_keywords(conn, pid)
    day = conn.execute("SELECT MAX(checked_date) FROM merchant_prices WHERE project_id=?",
                       (pid,)).fetchone()[0]
    by: dict[tuple, list[dict]] = {}
    if day:
        for x in conn.execute("SELECT * FROM merchant_prices WHERE project_id=? AND checked_date=?"
                              " ORDER BY market, keyword, position", (pid, day)):
            by.setdefault((x["market"], x["keyword"]), []).append(dict(x))
    markets = []
    for (market, kw), rows in by.items():
        sellers = Counter(str(r["seller"]) for r in rows if r.get("seller"))
        markets.append({"market": market, "keyword": kw, "products": len(rows),
                        "dist": distribution(rows),
                        "sellers": [list(x) for x in sellers.most_common(SELLERS_TOP)]})
    classes, no_product = [], []
    for k in kws:
        g = by.get((GOOGLE, k["keyword"]))
        cls = classify(k["position"], any(r["is_self"] for r in g) if g is not None else None)
        classes.append({"keyword": k["keyword"], "position": k["position"], "cls": cls,
                        "label": CLASSES.get(cls)})
        why = product_gap((audits or {}).get(k["url"])) if k["url"] else None
        if why:
            no_product.append({"keyword": k["keyword"], "url": k["url"], "why": why})
    return {"checked": day, "markets": markets, "classes": classes, "no_product": no_product,
            "copied": [{"keyword": kw, **c} for (m, kw), rows in by.items() if m == GOOGLE
                       for c in copied(rows)]}


def axis(conn, p, audits: dict | None = None) -> dict:
    """대시보드 페이로드의 한 축 — {"merchant": …}. 쇼핑몰·브랜드가 아니면 None(화면이 안 선다)."""
    if (p["type"] or "") != SITE_TYPE:
        return {"merchant": None}
    d = view(conn, p["id"], audits)
    d["last_note"] = collect_local.last_note(conn, p["id"], "merchant")
    return {"merchant": d}


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__)
    collector.add_common(ap)
    collector.add_setting(ap, "--keywords", key="merchant_keywords", fallback=3, type=int,
                          help="가격을 볼 검색어 수(쇼핑 칸이 선 것 중 검색량 상위). 기본 3")
    collector.add_setting(ap, "--max-age", key="merchant_max_age_days", fallback=30, type=int,
                          help="이 일수 안에 이미 샀으면 건너뜀. 0이면 항상 산다. 기본 30")
    return ap


def main() -> None:
    """인자가 없으면 자기검사 — run_checks.py 가 이 관례로 진입점을 찾는다."""
    if len(sys.argv) == 1:
        return _selfcheck()
    collector.cli("merchant")


def _selfcheck() -> None:
    """순수 함수만 — 적재·건너뜀·이음매는 test_local_commerce.py 가 가짜 응답으로 본다."""
    assert classify(3, True) == "both" and classify(3, False) == "organic_only"
    assert classify(14, True) == "shopping_only" and classify(None, False) == "neither"
    assert classify(3, None) is None, "안 잰 쇼핑을 '없음'으로 갈랐다"
    assert set(CLASSES) == {classify(a, b) for a in (1, None) for b in (True, False)}
    assert amazon_market("de-DE") == "amazon.de" and amazon_market("ko-KR") is None
    assert amazon_language("en-GB") == "en_GB"
    assert set(AMAZON_MARKETS) <= {c.split("-")[1] for c, _ in serp_adapter.LOCALES}, \
        "고를 수 없는 지역이 표에 있다"
    rows = [{"title": f"p{i}", "price": float(v), "currency": "EUR", "is_self": i == 2}
            for i, v in enumerate((10, 20, 30, 40, 50))]
    d = distribution(rows + [{"title": "usd", "price": 1.0, "currency": "USD"}])
    assert (d["currency"], d["n"], d["median"], d["min"], d["max"]) == ("EUR", 5, 30, 10, 50), d
    assert d["ours"] == [{"title": "p2", "price": 30.0, "position": None, "cheaper": 2}], d["ours"]
    assert distribution([{"title": "x", "price": 1.0, "currency": "EUR"}]) is None
    print("  ok  collect_merchant self-check")


if __name__ == "__main__":
    main()
