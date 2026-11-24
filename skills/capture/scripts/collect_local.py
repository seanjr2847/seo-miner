#!/usr/bin/env python3
"""지역 업체 — 구글 비즈니스 프로필·지도 순위·지오그리드·리뷰·반경 경쟁 업체.

사이트 타입이 local_business 일 때만 돈다. 여태 지도 칸은 "떴다/안 떴다"만 알았고, 그
자리를 가져가는 프로필은 요청문에서 [사람 확인] 칸이었다. 다섯 가지를 산다:

  1. /business_data/google/my_business_info/live  → local_places(gbp·self)   프로필 한 줄
  2. /serp/google/maps/live/advanced              → local_places(maps) + local_grid
     소재지 한 점에서 본 지도 순위. 격자(기본 3×3)의 한가운데가 그 한 점이다.
  3. /business_data/business_listings/search/live → local_places(listings)   같은 업종 반경
  4. /business_data/google/reviews/task_post·get  → local_places(reviews)    최근성·답글 비율
  5. Overpass(OSM, 무료)                          → local_places(osm)        반경 경쟁 업체

설계 메모:
  · 기준은 고정 숫자가 아니다. 리뷰 '10개·4.5점' 같은 선은 업종·로케일마다 달라서, 같은 지도
    칸 상위 업체의 **중앙값 대비**만 말한다(review_bench).
  · 프로필이 우리 것인지는 사이트 주소로만 판정한다(scoring.owns). 이름이 비슷한 남의 업체를
    우리 프로필로 적으면 NAP 대조가 통째로 틀린다 — 주소가 다르면 안 채우고 사유를 남긴다.
  · 유료 호출은 DataForSEO 키가 있을 때만. 키가 없으면 건너뛰고 사유를 runs.notes 에 남긴다
    (요청문의 프로필 줄은 [사람 확인] 그대로 선다). OSM 은 무료라 좌표만 알면(--coordinate) 돈다.
  · 지도 순위는 하루 단위로 안 움직인다. --max-age(기본 30일) 안이면 다시 사지 않는다 — 격자가
    검색어당 9회라 월 1회다.
  · 응답 필드는 공식 문서로만 확인했다(개발 중 유료 실호출 금지). 읽는 자리는 .get 으로 받고
    못 읽는 항목은 건너뛴다.
      https://docs.dataforseo.com/v3/business_data/google/my_business_info/live/
      https://docs.dataforseo.com/v3/serp/google/maps/live/advanced/
      https://docs.dataforseo.com/v3/business_data/business_listings/search/live/
      https://docs.dataforseo.com/v3/business_data/google/reviews/task_post/ (·task_get/)

비용(dry-run 고지용 추정 — 실청구액은 응답 cost 를 합산한다):
  지도 SERP Live $0.002/회 · 프로필 Live $0.0054/건 · 리뷰 우선 대기열 $0.0015/10건
  https://dataforseo.com/pricing/google-serp/google-maps-serp-api
  https://dataforseo.com/pricing/business-data/business-data-api
  https://dataforseo.com/pricing/business-data/google-reviews-api

Usage:
  python collect_local.py --project NAME [--keywords 3] [--grid 3] [--max-age 30] [--dry-run]
  python collect_local.py                                  # self-check
"""
from __future__ import annotations

import argparse
import json
import math
import re
import statistics
import sys
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import collect_backlinks  # noqa: E402  — 별칭 고르기(mention_aliases) 한 벌
import collector  # noqa: E402
import db  # noqa: E402
import fanout  # noqa: E402
import run_verdict  # noqa: E402
import scoring  # noqa: E402
import serp_adapter  # noqa: E402

SITE_TYPE = "local_business"
SOURCES = ("gbp", "maps", "listings", "reviews", "osm")     # local_places.source 의 정본

GBP_PATH = "/business_data/google/my_business_info/live"
MAPS_PATH = "/serp/google/maps/live/advanced"
LISTINGS_PATH = "/business_data/business_listings/search/live"
REVIEWS_POST = "/business_data/google/reviews/task_post"
REVIEWS_GET = "/business_data/google/reviews/task_get/"

GBP_COST = 0.0054
MAPS_COST = 0.002
LISTINGS_COST = 0.011           # 문서 예시 응답의 cost(가격표에 단가가 없다) — 고지용 추정일 뿐
REVIEWS_COST_PER_10 = 0.0015    # 우선 대기열(priority 2)

MAPS_ZOOM = "15z"               # 문서: 3z~21z. 동네 한 구역이 한 화면에 드는 배율
MAPS_DEPTH = 20                 # 100 까지 같은 값(SERP 단위 과금) — 순위 밖 판정의 분모
GBP_TRIES = 3                   # 별칭을 이만큼까지만 묻는다(한 번에 $0.0054)
RIVALS_KEEP = 10                # 한 점에서 적어 두는 상위 업체 수
REVIEW_RIVALS = 3               # 리뷰 최근성·답글 비율을 같이 보는 상위 업체 수
REVIEW_DEPTH = 20               # 최근 리뷰 몇 건으로 최근성·답글 비율을 보나(10건 단위 과금)
REVIEW_RECENT_DAYS = 90         # '최근'의 창 — 판정 기준이 아니라 세는 창이다(상위 업체와 같은 창)
REVIEW_WAIT_S = 300.0           # 리뷰 과제를 기다리는 상한. 넘으면 다음 수집에서 다시 산다
LISTINGS_LIMIT = 20
EARTH_M_PER_DEG = 111_320.0     # 위도 1도 ≈ 111.32km

# 리뷰·등재 출처 — **로케일(지역) → 플랫폼** 한 벌. 구글 비즈니스 프로필은 어느 지역이든 지도
# 칸의 출처라 공통이고, 나머지는 그 나라에서 실제로 쓰이는 곳만 적는다. 목록의 출처는 차용
# 계획(docs/borrow-from-claude-seo.md 3절 '리뷰·인용')이다 — 거기 없는 나라는 공통만 선다
# (모르는 플랫폼을 지어내지 않는다). 이 도구가 재는 것은 구글뿐이고 나머지는 요청문의
# [사람 확인] 줄이다.
PLATFORM_GLOBAL = ("구글 비즈니스 프로필",)
PLATFORMS_BY_REGION = {
    "US": ("Yelp", "BBB", "Apple 지도", "Bing Places"),
    "GB": ("Trustpilot",),
    "JP": ("食べログ",),
    "KR": ("네이버 플레이스", "카카오맵"),
}

# OSM 반경 경쟁 업체를 켜는 지역 — 가게·시설 데이터가 충실한 유럽·북미만. 나머지는 끈다:
# 빈 지도에서 '경쟁 업체 0곳'을 사실처럼 말하게 된다.
OSM_REGIONS = frozenset({"US", "CA", "GB", "DE", "AT", "CH", "FR", "ES", "IT", "PT", "NL", "PL"})
OSM_URL = "https://overpass-api.de/api/interpreter"
# Overpass 는 앱을 가리는 User-Agent 를 요구한다(https://wiki.openstreetmap.org/wiki/Overpass_API)
OSM_UA = "seo-miner (local competitor radius)"
OSM_KEYS = ("amenity", "shop", "healthcare", "office", "craft", "tourism", "leisure")
OSM_MAX = 300

# 리뷰 게이팅 — 구글 지도 사용자 제작 콘텐츠 정책의 '평점 조작': 판매자는 "Discourage or
# prohibit negative reviews, or selectively solicit positive reviews from customers" 를 할 수
# 없고, 리뷰 대가(할인·무료 제공)도 금지다.
# https://support.google.com/contributionpolicy/answer/7400114 (2026-11-24 확인)
REVIEW_GATING = ("리뷰를 늘릴 때 만족한 고객에게만 요청하거나(리뷰 게이팅) 낮은 평점을 막으면 구글 "
                 "정책 위반입니다. 대가(할인·무료 제공)를 걸어도 안 됩니다. 모든 고객에게 같은 "
                 "방식으로 요청합니다")


# ── 순수 함수 ────────────────────────────────────────────────────────────────

def region_of(locale: str) -> str:
    """'en-GB' → 'GB'. 지역이 없으면 그 언어의 기본 시장(serp_adapter.location)의 gl."""
    _, _, r = (locale or "").partition("-")
    return (r or serp_adapter.location(locale)[2][0]).upper()


def platforms(locale: str) -> list[str]:
    """이 로케일에서 등재·리뷰를 볼 곳 — 공통 + 그 지역 것."""
    return [*PLATFORM_GLOBAL, *PLATFORMS_BY_REGION.get(region_of(locale), ())]


def osm_enabled(locale: str) -> bool:
    return region_of(locale) in OSM_REGIONS


def parse_coordinate(s) -> tuple[float, float] | None:
    """'37.5,127.0' → (37.5, 127.0). 못 읽으면 None."""
    m = re.fullmatch(r"\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*", str(s or ""))
    if not m:
        return None
    lat, lng = float(m.group(1)), float(m.group(2))
    return (lat, lng) if -90 <= lat <= 90 and -180 <= lng <= 180 else None


def grid_points(lat: float, lng: float, n: int, spacing_m: float) -> list[tuple[int, int, float, float]]:
    """소재지를 한가운데 둔 n×n 격자 → [(gy, gx, lat, lng)] (북→남, 서→동).
    n 은 홀수로 올린다(한가운데가 있어야 한다). n=1 이면 소재지 한 점."""
    n = max(1, int(n))
    n += 1 - n % 2
    half = n // 2
    dlat = spacing_m / EARTH_M_PER_DEG
    dlng = spacing_m / (EARTH_M_PER_DEG * max(math.cos(math.radians(lat)), 1e-6))
    return [(gy, gx, round(lat + (half - gy) * dlat, 7), round(lng + (gx - half) * dlng, 7))
            for gy in range(n) for gx in range(n)]


def _num(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _int(v):
    f = _num(v)
    return int(f) if f is not None else None


def place_of(it: dict) -> dict | None:
    """DataForSEO 업체 항목(프로필·지도·목록이 같은 칸 이름을 쓴다) → 우리 꼴. 이름도 식별자도
    없는 항목은 None."""
    name = str(it.get("title") or "").strip()
    key = str(it.get("cid") or it.get("place_id") or "").strip()
    if not name or not key:
        return None
    rating = it.get("rating") if isinstance(it.get("rating"), dict) else {}
    return {"place_key": key, "name": name,
            "address": str(it.get("address") or "").strip() or None,
            "phone": str(it.get("phone") or "").strip() or None,
            "website": str(it.get("url") or it.get("domain") or "").strip() or None,
            "category": str(it.get("category") or "").strip() or None,
            "rating": _num(rating.get("value")), "reviews": _int(rating.get("votes_count")),
            "lat": _num(it.get("latitude")), "lng": _num(it.get("longitude")),
            "position": _int(it.get("rank_group")),
            "extra": {k: it[k] for k in ("category_ids", "is_claimed", "additional_categories")
                      if it.get(k) is not None}}


def _items(res, kind: str | None = None) -> list[dict]:
    out = []
    for r in res or []:
        for it in (r or {}).get("items") or []:
            if isinstance(it, dict) and (kind is None or it.get("type") == kind):
                out.append(it)
    return out


def is_ours(pl: dict, domain: str, cid: str = "") -> bool:
    """이 업체가 우리인가 — 프로필 식별자가 같거나, 사이트 주소가 우리 도메인이다."""
    if cid and pl.get("place_key") == cid:
        return True
    return bool(pl.get("website")) and scoring.owns(pl["website"], domain)


def _when(s) -> datetime | None:
    """'2026-10-02 08:15:11 +00:00' → aware datetime. 못 읽으면 None."""
    m = re.match(r"(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})", str(s or ""))
    if not m:
        return None
    return datetime.strptime(f"{m.group(1)} {m.group(2)}", "%Y-%m-%d %H:%M:%S").replace(
        tzinfo=timezone.utc)


def review_stats(res, now: datetime | None = None) -> dict | None:
    """리뷰 과제 결과 → 집계. 최근 REVIEW_DEPTH 건 안에서만 센다(전체가 아니다 — fetched 가 분모).
    {total, rating, fetched, latest, recent, replied}"""
    r0 = next((r for r in res or [] if isinstance(r, dict)), None)
    if r0 is None:
        return None
    now = now or datetime.now(timezone.utc)
    items = [it for it in r0.get("items") or [] if isinstance(it, dict)]
    times = [t for t in (_when(it.get("timestamp")) for it in items) if t]
    rating = r0.get("rating") if isinstance(r0.get("rating"), dict) else {}
    return {"total": _int(r0.get("reviews_count")), "rating": _num(rating.get("value")),
            "fetched": len(items),
            "latest": max(times).strftime("%Y-%m-%d") if times else None,
            "recent": sum(1 for t in times if now - t <= timedelta(days=REVIEW_RECENT_DAYS)),
            "replied": sum(1 for it in items if str(it.get("owner_answer") or "").strip())}


def _median(vals):
    vals = [v for v in vals if v is not None]
    return statistics.median(vals) if vals else None


def review_bench(mine: dict | None, rivals: list[dict]) -> list[dict]:
    """우리 값 대 **같은 지도 칸 상위 업체의 중앙값**. 고정 기준(리뷰 10개·4.5점)은 쓰지 않는다 —
    업종·지역마다 다르다. 견줄 값이 양쪽에 다 있는 줄만 낸다.
    [{"what", "mine", "median", "n", "behind"}]"""
    if not mine:
        return []
    out = []

    def row(what, key, fn=lambda x: x):
        m = fn(mine.get(key)) if mine.get(key) is not None else None
        rs = [fn(r.get(key)) for r in rivals if r.get(key) is not None]
        med = _median(rs)
        if m is None or med is None:
            return
        out.append({"what": what, "mine": m, "median": med, "n": len(rs), "behind": m < med})

    row("리뷰 수", "reviews")
    row("평점", "rating")
    row(f"최근 {REVIEW_RECENT_DAYS}일 리뷰(최근 {REVIEW_DEPTH}건 중)", "recent")
    # 답글 비율은 받은 리뷰가 있는 업체끼리만 — 0건을 0% 로 견주지 않는다
    def ratio(p):
        return round(p["replied"] / p["fetched"], 2) if p.get("fetched") else None
    m, rs = ratio(mine), [x for x in (ratio(r) for r in rivals) if x is not None]
    if m is not None and rs:
        med = _median(rs)
        out.append({"what": "사장 답글 비율", "mine": m, "median": med, "n": len(rs),
                    "behind": m < med})
    return out


def _fold(s) -> str:
    return "".join(unicodedata.normalize("NFKC", str(s or "")).lower().split())


def osm_query(lat: float, lng: float, radius_m: int) -> str:
    """반경 안의 이름 있는 가게·시설. 업종 태그는 받아서 고른다(우리 업체의 태그를 먼저 알아야 한다)."""
    keys = "".join(f"nwr(around:{int(radius_m)},{lat},{lng})[name][{k}];" for k in OSM_KEYS)
    return f"[out:json][timeout:25];({keys});out center tags {OSM_MAX};"


def osm_rivals(data: dict, names: list[str], domain: str) -> tuple[dict | None, list[dict]]:
    """Overpass 응답 → (우리 업체 또는 None, 같은 업종 태그의 다른 업체들).

    업종은 **우리 업체의 OSM 태그**로 정한다(amenity=dentist 면 반경 안의 dentist). 우리가 OSM 에
    없으면 업종을 지어내지 않고 ([None], []) 다 — 부르는 쪽이 사유를 남긴다."""
    els = [e for e in (data or {}).get("elements") or [] if isinstance(e.get("tags"), dict)]
    folded = {_fold(n) for n in names if _fold(n)}

    def kind(tags):
        return next(((k, tags[k]) for k in OSM_KEYS if tags.get(k)), None)

    def mine(e):
        t = e["tags"]
        site = t.get("website") or t.get("contact:website") or ""
        return (site and scoring.owns(site, domain)) or _fold(t.get("name")) in folded

    def row(e):
        t = e["tags"]
        c = e.get("center") or e
        k = kind(t)
        addr = " ".join(str(t[x]) for x in ("addr:street", "addr:housenumber", "addr:city")
                        if t.get(x)) or None
        return {"place_key": f"{e.get('type')}/{e.get('id')}", "name": str(t.get("name") or ""),
                "address": addr, "phone": t.get("phone") or t.get("contact:phone"),
                "website": t.get("website") or t.get("contact:website"),
                "category": f"{k[0]}={k[1]}" if k else None, "rating": None, "reviews": None,
                "lat": _num(c.get("lat")), "lng": _num(c.get("lon")), "position": None,
                "extra": {}}

    me = next((e for e in els if mine(e)), None)
    if me is None or kind(me["tags"]) is None:
        return None, []
    k = kind(me["tags"])
    return row(me), [row(e) for e in els if e is not me and e["tags"].get(k[0]) == k[1]]


def _fetch_osm(query: str) -> dict:
    import requests
    r = requests.post(OSM_URL, data={"data": query}, headers={"User-Agent": OSM_UA}, timeout=40)
    r.raise_for_status()
    return r.json()


class _Tasks:
    """과제형 엔드포인트의 진짜 구현 — 시험은 같은 모양의 가짜를 넣는다."""
    post = staticmethod(serp_adapter.post_dataforseo_task)

    @staticmethod
    def wait(paths: dict, timeout_s: float) -> dict:
        return serp_adapter.wait_dataforseo_tasks(paths, timeout_s=timeout_s)


# ── 대상 고르기·적재 ─────────────────────────────────────────────────────────

def pack_keywords(conn, pid: int, limit: int) -> list[str]:
    """지도 칸이 선 추적 검색어, 검색량 많은 순. 가장 최근 순위 행의 기능 목록으로 본다."""
    rows = conn.execute(
        """SELECT k.keyword FROM keywords k JOIN rank_snapshots r ON r.keyword_id=k.id
            WHERE k.project_id=? AND k.is_active=1
              AND r.id=(SELECT MAX(id) FROM rank_snapshots WHERE keyword_id=k.id)
              AND r.serp_features_json LIKE '%"local_pack"%'
            ORDER BY COALESCE(k.volume,0) DESC, k.keyword LIMIT ?""", (pid, int(limit))).fetchall()
    return [r[0] for r in rows]


def _put(conn, pid: int, today: str, source: str, role: str, pl: dict, keyword: str = "") -> int:
    assert source in SOURCES, source
    conn.execute(
        """INSERT INTO local_places(project_id, checked_date, source, role, keyword, position,
               place_key, name, address, phone, website, category, rating, reviews, lat, lng,
               extra_json)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(project_id, checked_date, source, keyword, place_key) DO UPDATE SET
               role=excluded.role, position=excluded.position, name=excluded.name,
               address=excluded.address, phone=excluded.phone, website=excluded.website,
               category=excluded.category, rating=excluded.rating, reviews=excluded.reviews,
               lat=excluded.lat, lng=excluded.lng, extra_json=excluded.extra_json""",
        (pid, today, source, role, keyword, pl.get("position"), pl["place_key"], pl.get("name"),
         pl.get("address"), pl.get("phone"), pl.get("website"), pl.get("category"),
         pl.get("rating"), pl.get("reviews"), pl.get("lat"), pl.get("lng"),
         json.dumps(pl.get("extra") or {}, ensure_ascii=False)))
    return 1


def _note_skip(conn, pid: int, reason: str, kind: str = "local") -> None:
    """돈을 안 쓰고 건너뛴 사유를 runs 한 줄로 — 잰 날이 아니라는 표식(run_verdict)을 단다."""
    rid = db.start_run(conn, pid, kind)
    db.finish_run(conn, rid, notes=f"{run_verdict.BLOCKED_NOTE} | {run_verdict.abort_note(reason)}"[:500])


def last_note(conn, pid: int, kind: str) -> str:
    """이 단계의 마지막 런이 남긴 사람 말 — 건너뛴 사유, 또는 수집하며 남긴 주석. 없으면 ''."""
    row = conn.execute("SELECT notes FROM runs WHERE project_id=? AND kind=? ORDER BY id DESC"
                       " LIMIT 1", (pid, kind)).fetchone()
    notes = (row[0] if row else "") or ""
    j = run_verdict.judge({"notes": notes, "finished_at": "t"})
    return j["reason"] if j["aborted"] else " | ".join(notes.split(" | ")[1:])


NO_KEY = ("지역 업체 수집은 DataForSEO 유료 키가 필요합니다. 없어도 기회 목록까지는 그대로 가고, "
          "요청문의 구글 비즈니스 프로필 줄은 [사람 확인]으로 남습니다. "
          "DATAFORSEO_LOGIN/DATAFORSEO_PASSWORD 를 넣으면 켜집니다. 발급: https://dataforseo.com")


def collect(project: str, *,
            dry_run: bool = False,
            keywords: int | None = None,
            grid: int | None = None,
            spacing: int | None = None,
            radius: int | None = None,
            max_age: int | None = None,
            coordinate: str | None = None,
            conn=None, post=None, tasks=None, osm=None,
            **opts) -> collector.StageResult:
    """지역 업체 다섯 축을 Brain 에 적재한다.

    Args:
        keywords: 지도 순위·격자를 잴 검색어 수(지도 칸이 선 것 중 검색량 상위). 기본 3
        grid: 격자 한 변(홀수). 1 이면 소재지 한 점만. 기본 3
        spacing: 격자 간격(m). 기본 1000
        radius: 반경 경쟁 업체를 볼 반경(m). 기본 1000
        max_age: 이 일수 안에 이미 샀으면 건너뛴다. 0 이면 항상. 기본 30
        coordinate: '위도,경도' — 프로필에서 못 얻을 때 소재지를 직접 준다
        post/tasks/osm: 시험이 갈아 끼우는 자리(DataForSEO Live·과제형·Overpass)
    """
    ap = _parser()
    post = post or serp_adapter.post_dataforseo
    tasks = tasks or _Tasks
    osm = osm or _fetch_osm
    with collector.stage(project, conn=conn, dry_run=dry_run) as st:
        conn, p = st.conn, st.project
        if (p["type"] or "") != SITE_TYPE:
            return st.skip("사이트 종류가 지역 비즈니스가 아니라 건너뜁니다")
        s = st.settings(ap, argparse.Namespace(keywords=keywords, grid=grid, spacing=spacing,
                                               radius=radius, max_age=max_age,
                                               coordinate=coordinate))
        n_kw, n_grid = int(s["local_keywords"]), int(s["local_grid"])
        spacing_m, radius_m = int(s["local_spacing_m"]), int(s["local_radius_m"])
        max_age = int(s["local_max_age_days"])
        fixed = parse_coordinate(s["local_coordinate"])
        pid, today = p["id"], st.today
        domain = (p["domain"] or "").strip().lower()
        locale = db.project_locale(p)
        loc_name, lang, _ = serp_adapter.location(locale)
        paid = serp_adapter.has_dataforseo() and serp_adapter.dfs_supported(locale)
        use_osm = osm_enabled(locale)

        if not paid and not (fixed and use_osm):
            why = NO_KEY if not serp_adapter.has_dataforseo() else (
                f"{serp_adapter.locale_label(locale)} 은 DataForSEO 가 조회하지 않는 지역입니다")
            if not st.dry_run:
                _note_skip(conn, pid, why)
                conn.commit()
            print(f"[local] {why}")
            return st.skip(why)

        if max_age > 0 and conn.execute(
                "SELECT 1 FROM local_places WHERE project_id=? AND checked_date > date('now', ?)"
                " LIMIT 1", (pid, f"-{max_age} days")).fetchone():
            reason = (f"최근 {max_age}일 안에 이미 수집했습니다: 지도 순위와 프로필은 하루 단위로 "
                      f"움직이지 않습니다. 다시 사려면 `--max-age 0`.")
            print(f"[local] {reason}")
            return st.noop(reason=reason)

        names = collect_backlinks.mention_aliases(st.cfg or {})[0] or [p["name"]]
        kws = pack_keywords(conn, pid, n_kw) if paid else []
        n_pts = len(grid_points(0, 0, n_grid, spacing_m))
        est = ((min(len(names), GBP_TRIES) * GBP_COST
                + max(len(kws), 1) * n_pts * MAPS_COST + LISTINGS_COST
                + (1 + REVIEW_RIVALS) * (REVIEW_DEPTH / 10) * REVIEWS_COST_PER_10) if paid else 0.0)
        print(f"[local] project={p['name']} locale={locale} names={len(names)} "
              f"keywords={len(kws)} grid={n_grid}x{n_grid} osm={'on' if use_osm else 'off'} "
              f"est_cost≈${est:.3f}")
        if st.dry_run:
            print("단가 출처: DataForSEO 가격표(모듈 docstring). 실제 청구액은 응답 cost 로 기록.")
            return st.noop(cost=est)

        rows: dict[str, int] = {}
        notes: list[str] = []
        cost = 0.0
        calls = 0

        def bump(name: str, n: int, c: float = 0.0) -> None:
            nonlocal cost
            rows[name] = rows.get(name, 0) + n
            cost += float(c or 0)

        with st.record("local") as r:
            me: dict | None = None
            # 1. 프로필 — 별칭마다 묻고, 사이트 주소가 우리 것인 프로필만 받는다.
            if paid:
                others: list[str] = []
                for nm in names[:GBP_TRIES]:
                    try:
                        res, c = post(GBP_PATH, [{"keyword": nm, "location_name": loc_name,
                                                  "language_code": lang}])
                    except collector.Fatal:
                        raise
                    except Exception as e:      # noqa: BLE001 — 한 별칭의 실패가 단계를 안 죽인다
                        st.fail(str(e), item=f"프로필 '{nm}'", kind=type(e).__name__)
                        continue
                    calls += 1
                    cost += float(c or 0)
                    cands = [x for x in map(place_of, _items(res, "google_business_info")) if x]
                    me = next((x for x in cands if is_ours(x, domain)), None)
                    if me:
                        bump("gbp", _put(conn, pid, today, "gbp", "self", me))
                        break
                    others += [x["name"] for x in cands]
                if me is None:
                    notes.append("구글 비즈니스 프로필을 찾지 못했습니다"
                                 + (f"(같은 이름의 다른 업체: {', '.join(others[:3])}, 사이트 주소가 "
                                    "다릅니다)" if others else "")
                                 + ". 요청문의 프로필 줄은 [사람 확인]으로 남습니다")
            center = fixed or ((me["lat"], me["lng"]) if me and me.get("lat") is not None
                               and me.get("lng") is not None else None)
            cid = me["place_key"] if me else ""

            # 2. 지도 순위 — 격자점마다 한 번. 한가운데(소재지)의 상위 업체를 경쟁 업체로 적는다.
            top_rivals: list[dict] = []
            if paid and center:
                if not kws and me and me.get("category"):
                    kws = [me["category"]]
                    notes.append(f"지도 칸이 선 추적 검색어가 없어 프로필 업종('{kws[0]}')으로 쟀습니다")
                pts = grid_points(center[0], center[1], n_grid, spacing_m)
                mid = len(pts) // 2
                jobs = [(kw, i, pt) for kw in kws for i, pt in enumerate(pts)]

                def buy(job):
                    kw, _, (_, _, la, lo) = job
                    return post(MAPS_PATH, [{"keyword": kw, "language_code": lang,
                                             "location_coordinate": f"{la},{lo},{MAPS_ZOOM}",
                                             "depth": MAPS_DEPTH}])

                def put(job, got):
                    kw, i, (gy, gx, la, lo) = job
                    res, c = got
                    found = [x for x in map(place_of, _items(res, "maps_search")) if x]
                    mine = next((x for x in found if is_ours(x, domain, cid)), None)
                    conn.execute(
                        """INSERT INTO local_grid(project_id, checked_date, keyword, gy, gx, lat,
                               lng, position, seen, top_name) VALUES(?,?,?,?,?,?,?,?,?,?)
                           ON CONFLICT(project_id, checked_date, keyword, gy, gx) DO UPDATE SET
                               position=excluded.position, seen=excluded.seen,
                               top_name=excluded.top_name""",
                        (pid, today, kw, gy, gx, la, lo, mine["position"] if mine else None,
                         len(found), found[0]["name"] if found else None))
                    bump("grid", 1, c)
                    if i == mid:
                        for x in found[:RIVALS_KEEP]:
                            ours = is_ours(x, domain, cid)
                            bump("maps", _put(conn, pid, today, "maps", "self" if ours else "rival",
                                              x, kw))
                        if kw == kws[0]:
                            top_rivals.extend(x for x in found if not is_ours(x, domain, cid))

                calls += fanout.each(st, jobs, buy, put, workers=fanout.LIMITS["dataforseo"],
                                     label=lambda j: f"지도 '{j[0]}' 격자 {j[1] + 1}")
            elif paid:
                notes.append("소재지 좌표를 몰라 지도 순위·격자를 건너뜁니다(프로필을 못 찾았습니다. "
                             "`--coordinate 위도,경도` 로 줄 수 있습니다)")

            # 3. 같은 업종 반경 — 프로필이 준 업종 식별자가 있어야 묻는다.
            cat_ids = ((me or {}).get("extra") or {}).get("category_ids") or []
            if paid and center and cat_ids:
                try:
                    res, c = post(LISTINGS_PATH, [{
                        "categories": [cat_ids[0]],
                        "location_coordinate": f"{center[0]},{center[1]},{max(1, round(radius_m / 1000))}",
                        "order_by": ["rating.votes_count,desc"], "limit": LISTINGS_LIMIT}])
                    calls += 1
                    cost += float(c or 0)
                    for x in map(place_of, _items(res, "business_listing")):
                        if x and not is_ours(x, domain, cid):
                            x["position"] = None
                            bump("listings", _put(conn, pid, today, "listings", "rival", x))
                except collector.Fatal:
                    raise
                except Exception as e:          # noqa: BLE001
                    st.fail(str(e), item="같은 업종 반경", kind=type(e).__name__)

            # 4. 리뷰 — 우리와 지도 상위 업체의 최근 리뷰. 과제형이라 맡기고 기다린다.
            if paid and me:
                who = [("self", me)] + [("rival", x) for x in top_rivals[:REVIEW_RIVALS]]
                paths: dict = {}
                for role, x in who:
                    try:
                        tid, c = tasks.post(REVIEWS_POST, [{
                            "cid": x["place_key"], "location_name": loc_name,
                            "language_code": lang, "depth": REVIEW_DEPTH, "sort_by": "newest",
                            "priority": 2}])
                        calls += 1
                        cost += float(c or 0)
                        paths[x["place_key"]] = REVIEWS_GET + tid
                    except collector.Fatal:
                        raise
                    except Exception as e:      # noqa: BLE001
                        st.fail(str(e), item=f"리뷰 '{x['name']}'", kind=type(e).__name__)
                got = tasks.wait(paths, REVIEW_WAIT_S) if paths else {}
                late = 0
                for role, x in who:
                    state, val = got.get(x["place_key"], (None, None))
                    if state == "waiting":
                        late += 1
                    elif state == "done":
                        stt = review_stats(val)
                        if stt:
                            bump("reviews", _put(conn, pid, today, "reviews", role, {
                                **x, "position": None,
                                "rating": stt["rating"] if stt["rating"] is not None else x.get("rating"),
                                "reviews": stt["total"] if stt["total"] is not None else x.get("reviews"),
                                "extra": {k: stt[k] for k in ("fetched", "latest", "recent", "replied")}}))
                    elif state:
                        st.fail(str(val), item=f"리뷰 '{x['name']}'", kind=state)
                if late:
                    notes.append(f"리뷰 과제 {late}건이 {int(REVIEW_WAIT_S)}초 안에 안 끝났습니다. "
                                 "다음 수집에서 다시 삽니다")

            # 5. OSM 반경 경쟁 업체 — 무료. 데이터가 충실한 지역에서만.
            if center and use_osm:
                try:
                    o_me, o_rivals = osm_rivals(osm(osm_query(center[0], center[1], radius_m)),
                                                [*names, (me or {}).get("name") or ""], domain)
                    calls += 1
                    if o_me is None:
                        notes.append("OSM 에 우리 업체가 없어 반경 경쟁 업체의 업종을 정하지 못했습니다")
                    else:
                        bump("osm", _put(conn, pid, today, "osm", "self", o_me))
                        for x in o_rivals:
                            bump("osm", _put(conn, pid, today, "osm", "rival", x))
                except Exception as e:          # noqa: BLE001 — 무료 축의 실패가 유료 축을 안 버린다
                    st.fail(str(e), item="OSM 반경", kind=type(e).__name__)
            elif center:
                notes.append(f"OSM 반경 경쟁 업체는 {serp_adapter.locale_label(locale)} 에서 끕니다"
                             "(지도 데이터가 충실한 지역만 켭니다)")

            conn.commit()
            r.api_calls = calls
            r.cost = cost
            r.notes = (" ".join(f"{k}={v}" for k, v in sorted(rows.items())) + f" {st.err_note}"
                       + ("" if not notes else " | " + " | ".join(notes)))[:900]

        total = sum(rows.values())
        print(f"\ncollected {total} rows ({r.notes}), actual_cost=${cost:.4f}\nrun_id={r.id}")
        if not total and not st.errors:
            return st.skip(notes[0] if notes else "적재한 것이 없습니다")
        return st.verdict(total, rows=total, cost=cost)


# ── 읽기 — 화면(페이로드)과 요청문이 같이 읽는다 ─────────────────────────────

def latest(conn, pid: int, locale: str = "") -> dict | None:
    """가장 최근 회차의 지역 업체 값. 수집한 적이 없으면 None(없는 걸 지어내지 않는다)."""
    day = conn.execute("SELECT MAX(checked_date) FROM local_places WHERE project_id=?",
                       (pid,)).fetchone()[0]
    if not day:
        return None
    rows = [dict(x) for x in conn.execute(
        "SELECT * FROM local_places WHERE project_id=? AND checked_date=? ORDER BY id",
        (pid, day))]
    for x in rows:
        try:
            x["extra"] = json.loads(x.pop("extra_json") or "{}")
        except ValueError:
            x["extra"] = {}

    def pick(source, role=None):
        return [x for x in rows if x["source"] == source and (role is None or x["role"] == role)]

    def slim(x, *more):
        return {k: x.get(k) for k in ("name", "address", "phone", "website", "category",
                                      "rating", "reviews", "position", *more)}

    gbp = next(iter(pick("gbp", "self")), None)
    kws: dict[str, dict] = {}
    for x in pick("maps"):
        k = kws.setdefault(x["keyword"], {"keyword": x["keyword"], "position": None, "rivals": []})
        if x["role"] == "self":
            k["position"] = x["position"]
        else:
            k["rivals"].append(slim(x))
    grid: dict[str, dict] = {}
    for g in conn.execute(
            "SELECT keyword, gy, gx, position, seen, top_name FROM local_grid"
            " WHERE project_id=? AND checked_date=? ORDER BY keyword, gy, gx", (pid, day)):
        e = grid.setdefault(g["keyword"], {"keyword": g["keyword"], "cells": []})
        e["cells"].append({"gy": g["gy"], "gx": g["gx"], "position": g["position"],
                           "seen": g["seen"], "top": g["top_name"]})
    for e in grid.values():
        e["n"] = max(c["gy"] for c in e["cells"]) + 1
        e["found"] = sum(1 for c in e["cells"] if c["position"] is not None)
        e["total"] = len(e["cells"])

    def rev(x):
        return {**slim(x), **{k: x["extra"].get(k) for k in ("fetched", "latest", "recent", "replied")}}

    rv_me = next(iter(pick("reviews", "self")), None)
    rv_riv = [rev(x) for x in pick("reviews", "rival")]
    # 리뷰 과제가 안 끝났어도 수·평점은 지도 행이 이미 안다 — 그 둘만으로 견준다
    first = next(iter(kws.values()), None)
    base_me = rev(rv_me) if rv_me else (slim(gbp) if gbp else None)
    base_riv = rv_riv or (first["rivals"][:REVIEW_RIVALS] if first else [])
    osm_me = next(iter(pick("osm", "self")), None)
    return {"checked": day,
            "gbp": slim(gbp, "lat", "lng") if gbp else None,
            "keywords": list(kws.values()),
            "grid": list(grid.values()),
            "reviews": {"mine": base_me, "rivals": base_riv,
                        "bench": review_bench(base_me, base_riv)},
            "listings": [slim(x) for x in pick("listings")],
            "osm": ({"category": osm_me["category"],
                     "rivals": [slim(x) for x in pick("osm", "rival")]} if osm_me else None),
            "platforms": platforms(locale), "gating": REVIEW_GATING}


def axis(conn, p) -> dict:
    """대시보드 페이로드의 한 축 — {"local": …}. 지역 비즈니스가 아니면 None 이다(화면이 안 선다).
    수집 전이면 {"checked": None, …} 로 플랫폼 표와 마지막 사유만 싣는다."""
    if (p["type"] or "") != SITE_TYPE:
        return {"local": None}
    locale = db.project_locale(p)
    d = latest(conn, p["id"], locale) or {"checked": None, "gbp": None, "keywords": [],
                                          "grid": [], "reviews": {"mine": None, "rivals": [],
                                                                  "bench": []},
                                          "listings": [], "osm": None,
                                          "platforms": platforms(locale), "gating": REVIEW_GATING}
    d["last_note"] = last_note(conn, p["id"], "local")
    return {"local": d}


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__)
    collector.add_common(ap)
    collector.add_setting(ap, "--keywords", key="local_keywords", fallback=3, type=int,
                          help="지도 순위·격자를 잴 검색어 수(지도 칸이 선 것 중 검색량 상위). 기본 3")
    collector.add_setting(ap, "--grid", key="local_grid", fallback=3, type=int,
                          help="격자 한 변(홀수). 1 이면 소재지 한 점만. 기본 3")
    collector.add_setting(ap, "--spacing", key="local_spacing_m", fallback=1000, type=int,
                          help="격자 간격(m). 기본 1000")
    collector.add_setting(ap, "--radius", key="local_radius_m", fallback=1000, type=int,
                          help="반경 경쟁 업체를 볼 반경(m). 기본 1000")
    collector.add_setting(ap, "--max-age", key="local_max_age_days", fallback=30, type=int,
                          help="이 일수 안에 이미 샀으면 건너뜀. 0이면 항상 산다. 기본 30")
    collector.add_setting(ap, "--coordinate", key="local_coordinate", fallback="", type=str,
                          help="소재지 '위도,경도'. 프로필에서 못 얻을 때만 준다")
    return ap


def main() -> None:
    """인자가 없으면 자기검사 — run_checks.py 가 이 관례로 진입점을 찾는다."""
    if len(sys.argv) == 1:
        return _selfcheck()
    collector.cli("local")


def _selfcheck() -> None:
    """순수 함수만 — 적재·건너뜀·이음매는 test_local_commerce.py 가 가짜 응답으로 본다."""
    pts = grid_points(37.5, 127.0, 3, 1000)
    assert len(pts) == 9 and pts[4][2:] == (37.5, 127.0), pts[4]
    assert pts[0][2] > pts[8][2] and pts[0][3] < pts[8][3], "북서 → 남동 순서가 아니다"
    assert len(grid_points(0, 0, 2, 500)) == 9 and len(grid_points(0, 0, 1, 500)) == 1
    assert parse_coordinate("37.5, 127.0") == (37.5, 127.0) and parse_coordinate("x") is None
    assert parse_coordinate("95,10") is None
    assert platforms("ko-KR") == ["구글 비즈니스 프로필", "네이버 플레이스", "카카오맵"]
    assert platforms("de-DE") == ["구글 비즈니스 프로필"] and osm_enabled("de-DE")
    assert not osm_enabled("ko-KR") and osm_enabled("en") and region_of("en") == "US"
    for code, _ in serp_adapter.LOCALES:
        assert platforms(code)[0] == PLATFORM_GLOBAL[0], code
    assert set(PLATFORMS_BY_REGION) | OSM_REGIONS <= {c.split("-")[1] for c, _ in serp_adapter.LOCALES}, \
        "고를 수 없는 지역이 표에 있다"
    print("  ok  collect_local self-check")


if __name__ == "__main__":
    main()
