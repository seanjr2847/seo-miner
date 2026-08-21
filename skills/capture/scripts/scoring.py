#!/usr/bin/env python3
"""판정 규칙 한 곳 — striking distance·움직임·남의 브랜드·pSEO 후보·기회 목록.

여기가 생기기 전에는 같은 규칙이 SQL(dashboard·collect_gsc)·Python·템플릿 JS·
references/scoring.md 산문 네 군데에 흩어져 있었고 값이 서로 어긋나 있었다
(움직임 임계 0.4 vs 0.5). 임계값을 바꾸려면 이 파일만 고친다.

references/scoring.md 는 이제 명세이고, 실행은 전부 여기서 한다.

self-check:  python scoring.py
기회 적재:   python scoring.py load <project>   (striking·ctr_gap·… 계산 → opportunities upsert)
"""
import math
import re
import sqlite3
import sys

# 1페이지 경계. 화면(깊이 그래프)·SQL·산문이 같은 값을 봐야 한다.
PAGE1 = 10

# striking_distance: 밀면 상단 진입이 가능한 평균 순위 구간 (scoring.md 1절)
STRIKING_LO, STRIKING_HI = 4, 20

# GSC Δpos 노이즈 바닥 (scoring.md 4-4). 이보다 작은 변화는 "움직였다"고 하지 않는다.
NOISE_POS = 0.5

# rank_decay 기회로 올릴 하락 폭 (scoring.md 1절). 노이즈 바닥과 다른 개념이다 —
# 노이즈는 "화면에 띄울까", 이건 "방어 기회로 적재할까".
DECAY_POS = -1.5

# pseo_pattern 후보 추출 임계 (scoring.md 1b-1). 노출 큰 사이트면 올린다.
PSEO_MIN_IMP, PSEO_MAX_CTR = 50, 1.5

# 순위 노이즈 폭 (scoring.md 4-4 산문 "순위 ±2~3 변동은 노이즈"의 코드화).
# NOISE_POS는 GSC 평균순위 Δ용이고, 이건 rank_snapshots 정수 순위용 — 다른 축이다.
RANK_NOISE = 3

# striking_distance 노출 하한 (scoring.md 1절 "노출 유의미"). 산문에만 있고
# SQL에는 없던 조건 — 노출 몇 개짜리 순위는 통계가 아니다.
STRIKING_MIN_IMP = 10

# ctr_gap: 순위별 기대 CTR(%). 업계 공개 클릭 곡선(FirstPageSage·AWR류 발표치)의
# 근사 평균이다 — 절대 진리가 아니라 "이 순위면 이 정도는 나와야 한다"는 기준선.
EXPECTED_CTR = {1: 28.0, 2: 15.0, 3: 10.0, 4: 7.0, 5: 5.0, 6: 4.0, 7: 3.0,
                8: 2.5, 9: 2.2, 10: 2.0, 11: 1.6, 12: 1.4, 13: 1.2, 14: 1.1,
                15: 1.0, 16: 0.9, 17: 0.8, 18: 0.7, 19: 0.6, 20: 0.5}
CTR_GAP_MIN_IMP = 100    # 이보다 적은 노출은 CTR 자체가 통계로 무의미
CTR_GAP_FACTOR = 0.5     # 실제 CTR < 기대 × 이 값일 때만 기회로 본다

# cannibalization: 같은 쿼리에 내 페이지 여럿이 갈릴 때
CANNI_MIN_IMP = 50       # 쿼리 합산 노출 하한
CANNI_MIN_SHARE = 0.2    # 부(副)페이지 노출 비중 하한 — 미만이면 사실상 한 페이지 독점

# device_gap: 같은 쿼리에서 모바일 평균순위가 데스크톱보다 이만큼 아래면
# "콘텐츠가 약한 게 아니라 모바일에서만 밀린다"로 본다. 1~2위 차이는 기기별
# 표본이 달라서도 생긴다 — 2.0 아래는 세지 않는다.
DEVICE_GAP_POS = 2.0
DEVICE_MIN_IMP = 50      # 모바일 노출 하한. 노출 몇 개짜리 기기 차이는 통계가 아니다

# 결정적 점수 계수 (scoring.md 2절의 프리셋별 방향 준수: saas는 w_ai 최상향,
# local_clinic은 w_fit 상향, directory는 수요·coverage 우선, game은 균형).
# 각 프리셋 합은 1.0 — score()가 0~100으로 바로 환산한다.
WEIGHTS = {
    "game":         {"w_demand": 0.30, "w_reach": 0.25, "w_fit": 0.25, "w_ai": 0.20},
    "local_clinic": {"w_demand": 0.25, "w_reach": 0.20, "w_fit": 0.45, "w_ai": 0.10},
    "saas":         {"w_demand": 0.20, "w_reach": 0.20, "w_fit": 0.15, "w_ai": 0.45},
    "directory":    {"w_demand": 0.40, "w_reach": 0.25, "w_fit": 0.20, "w_ai": 0.15},
}

# 남의 브랜드 검색 판별용 (scoring.md 1a).
BRAND_MODIFIERS = {
    "후기", "리뷰", "review", "reviews", "가격", "요금", "pricing", "price",
    "무료", "free", "다운로드", "download", "로그인", "login", "사용법",
    "tutorial", "app", "ai",
}
# 디렉터리·비교 콘텐츠가 정당하게 이길 수 있는 자리 — 남의 브랜드여도 기회로 둔다.
KEEP_INTENTS = {
    "alternative", "alternatives", "대안", "vs", "versus", "비교",
    "competitor", "competitors", "best", "추천",
}

# 의도 토큰 사전 (classify_intent). 우선순위는 위에서부터 — transactional 이
# commercial 을 이기고, commercial 이 navigational 을, 마지막에 info.
# 기본값은 코드, 보정은 Claude/사람 — NULL 인 활성 키워드만 load() 시작 시 채운다.
INTENT_TRANSACTIONAL = {
    "구매", "가격", "다운로드", "할인", "쿠폰",
    "buy", "price", "pricing", "download", "discount", "coupon",
}
INTENT_COMMERCIAL = {
    "후기", "리뷰", "비교", "추천", "순위", "랭킹",
    "vs", "best", "review", "reviews", "alternative", "alternatives",
    "top", "compare",
}
INTENT_NAVIGATIONAL = {
    "로그인", "공식", "홈페이지", "login", "official", "homepage",
}


def norm(s: str) -> str:
    """비교용 정규화 — 소문자 + 영숫자/한글만. 'Future Tools' 와 'futuretools.io' 를 같게 본다."""
    return re.sub(r"[^0-9a-z가-힣]+", "", (s or "").lower())


def tokens(s: str) -> list[str]:
    return [t for t in re.split(r"[^0-9a-zA-Z가-힣]+", (s or "").lower()) if t]


def host_of(url: str) -> str:
    """URL에서 호스트만. 스킴·www·포트·경로를 벗긴다."""
    h = re.sub(r"^[a-z]+://", "", (url or "").strip().lower())
    h = h.split("/")[0].split("?")[0].split("#")[0].split(":")[0]
    return h[4:] if h.startswith("www.") else h


def owns(domain: str, own: str) -> bool:
    """domain 이 own 자신이거나 그 하위 도메인인가. 네 군데에 복제돼 있던 두 줄."""
    d, o = host_of(domain), host_of(own)
    return bool(d and o) and (d == o or d.endswith("." + o))


def _stem(domain: str) -> str:
    """도메인에서 브랜드 이름 후보 — 'ecrett.com' -> 'ecrett'."""
    return norm(host_of(domain).split(".")[0])


def foreign_brands(conn: sqlite3.Connection, project_id: int, cfg: dict | None = None) -> set[str]:
    """남의 도구·서비스 이름 카탈로그 (정규화된 형태).

    출처: competitors 테이블 도메인 + 프로젝트 yaml 의 tools/foreign_brands.
    자기 브랜드(name·brand_aliases)는 반대로 반드시 남긴다 — 내 브랜드 검색의
    낮은 CTR은 진짜 문제다 (scoring.md 1a).
    """
    cfg = cfg or {}
    names = set()
    for r in conn.execute("SELECT domain FROM competitors WHERE project_id=?", (project_id,)):
        s = _stem(r[0])
        if len(s) >= 3:
            names.add(s)
    for key in ("foreign_brands", "tools"):
        for n in (cfg.get(key) or []):
            s = norm(n if isinstance(n, str) else (n.get("name", "") if isinstance(n, dict) else ""))
            if len(s) >= 3:
                names.add(s)
    own = {norm(cfg.get("name", ""))} | {norm(a) for a in (cfg.get("brand_aliases") or [])}
    return {n for n in names if n and n not in own}


def is_foreign_brand(query: str, brands: set[str]) -> bool:
    """쿼리가 '남의 브랜드 이름(+흔한 수식어)' 인가.

    이름 단독과 `{이름} 후기/review/가격` 같은 변형은 걸러내고,
    `{이름} alternative` / `{이름} vs {이름}` 같은 비교 의도는 남긴다.
    """
    if not brands:
        return False
    ts = tokens(query)
    if not ts or any(t in KEEP_INTENTS for t in ts):
        return False
    core = [t for t in ts if t not in BRAND_MODIFIERS]
    if not core:
        return False
    return norm("".join(core)) in brands


def drop_foreign_brands(rows: list[dict], brands: set[str], key: str = "query") -> list[dict]:
    return [r for r in rows if not is_foreign_brand(r.get(key, ""), brands)]


# 일부 엔진은 인용 메타데이터 없이 본문에 URL을 그대로 적는다 — 그때의 fallback.
URL_RE = re.compile(r"https?://[^\s)\]>\"']+")


def aliases_of(cfg: dict) -> list[str]:
    """판정에 쓸 자기 브랜드 별칭 — 이름 + brand_aliases.

    두 측정 경로(collect_ai·browse)가 각자 조립하면 별칭 규칙이 갈라져
    judge 가 같아도 수치가 어긋난다."""
    return [a for a in [cfg.get("name", "")] + (cfg.get("brand_aliases") or []) if a]


def judge(content: str, citation_urls: list[str] | None, aliases: list[str],
          own_domain: str) -> tuple[int, int, list[str]]:
    """AI 답변 하나를 (언급됐나, 인용됐나, 대신 인용된 도메인들) 로 판정.

    키 경로(collect_ai)와 브라우저 경로(browse/record_check)가 같은 판정을 써야
    두 경로의 수치를 한 화면에서 비교할 수 있다. browse가 collect_ai 내부를
    가로질러 import 하던 것을 여기로 옮겼다. 인용 URL이 없으면 본문의 맨 URL을
    줍는 fallback까지 여기서 한다 — 호출부 두 곳이 각자 하던 일이다.
    """
    text = (content or "").lower()
    mentioned = int(any(a.lower() in text for a in aliases if a))
    urls = citation_urls or URL_RE.findall(content or "")
    domains = sorted({d for d in (host_of(u) for u in urls) if d})
    cited = int(any(owns(d, own_domain) for d in domains))
    others = [d for d in domains if not owns(d, own_domain)]
    return mentioned, cited, others


def gap_to_page1(pos) -> float:
    """1페이지 경계까지 되밀어야 할 거리. 1페이지 안이면 0."""
    if pos is None:
        return 0.0
    return round(max(0.0, float(pos) - PAGE1), 1)


def moved_up(dpos: float, dclk: int) -> bool:
    return dpos > NOISE_POS or dclk > 0


def moved_down(dpos: float, dclk: int) -> bool:
    return dpos < -NOISE_POS or dclk < 0


def snapshot_pair(conn: sqlite3.Connection, project_id: int) -> tuple[str | None, str | None, int | None, bool]:
    """직전 스냅샷을 같은 period_days 중 가장 최근으로 고른다 (scoring.md 4-3b).

    기간이 다른 스냅샷끼리 빼면 Δ순위·Δ클릭이 전부 거짓이 된다.
    """
    snaps = [(r[0], r[1]) for r in conn.execute(
        """SELECT snapshot_date, MAX(period_days) period_days
             FROM gsc_snapshots WHERE project_id=?
            GROUP BY snapshot_date ORDER BY 1 DESC LIMIT 10""", (project_id,)).fetchall()]
    cur, period = snaps[0] if snaps else (None, None)
    prev = next((d for d, pd in snaps[1:] if pd == period), None)
    period_mismatch = bool(snaps[1:]) and prev is None
    return cur, prev, period, period_mismatch


def movers(now_: dict, before: dict, *, limit: int = 10) -> tuple[list[dict], list[dict]]:
    """두 스냅샷 집계(query -> row)를 받아 (오른 것, 내린 것).

    호출부가 period_days 가 같은 스냅샷끼리만 넘겨야 한다 (scoring.md 4-3b).
    """
    rows = []
    for kw, r in now_.items():
        b = before.get(kw)
        if b:
            rows.append({"query": kw, "pos": r["pos"], "dpos": round(b["pos"] - r["pos"], 1),
                         "clk": r["clk"], "dclk": r["clk"] - b["clk"], "imp": r["imp"]})
    ups = sorted([m for m in rows if moved_up(m["dpos"], m["dclk"])],
                 key=lambda m: (-m["dclk"], -m["dpos"]))[:limit]
    downs = sorted([m for m in rows if moved_down(m["dpos"], m["dclk"])],
                   key=lambda m: (m["dclk"], m["dpos"]))[:limit]
    return ups, downs


_STRIKING_SQL = """
SELECT query, ROUND(AVG(position),1) pos, SUM(impressions) imp, SUM(clicks) clk
  FROM gsc_snapshots WHERE project_id=? AND snapshot_date=?
 GROUP BY query HAVING pos BETWEEN ? AND ? AND imp >= ?
 ORDER BY imp DESC LIMIT ?
"""


def striking(conn: sqlite3.Connection, project_id: int, snapshot_date: str | None,
             *, limit: int = 15, brands: set[str] | None = None) -> list[dict]:
    """조금만 밀면 1페이지 갈 검색어. 남의 브랜드 검색은 빼고, gap·band 를 붙여 돌려준다.

    band: 'page1'(4~10위, 이미 1페이지 — 상단으로) / 'page2'(11~20위 — 1페이지로).
    노출 하한(STRIKING_MIN_IMP)은 scoring.md 1절 "노출 유의미"의 코드화다.
    """
    if not snapshot_date:
        return []
    # 브랜드를 걸러내면 limit 미만이 되므로 넉넉히 뽑고 자른다.
    over = limit * 3 if brands else limit
    rows = [dict(r) for r in conn.execute(
        _STRIKING_SQL, (project_id, snapshot_date, STRIKING_LO, STRIKING_HI,
                        STRIKING_MIN_IMP, over))]
    if brands:
        rows = drop_foreign_brands(rows, brands)
    rows = rows[:limit]
    for r in rows:
        r["gap"] = gap_to_page1(r["pos"])
        r["band"] = "page1" if r["pos"] <= PAGE1 else "page2"
    return rows


def pseo_candidates(conn: sqlite3.Connection, project_id: int, snapshot_date: str | None,
                    *, limit: int = 200, min_imp: int = PSEO_MIN_IMP,
                    max_ctr: float = PSEO_MAX_CTR) -> list[dict]:
    """수요는 있는데(노출) 클릭이 비어 있는 쿼리 — pSEO 군집 후보 (scoring.md 1b-1).
    군집으로 묶는 것은 Claude 판단이고, 여기는 후보 추출까지만 한다."""
    if not snapshot_date:
        return []
    return [dict(r) for r in conn.execute(
        """SELECT query, SUM(impressions) imp, SUM(clicks) clk,
                  ROUND(SUM(clicks)*100.0/SUM(impressions),2) ctr_pct,
                  ROUND(AVG(position),1) pos
             FROM gsc_snapshots
            WHERE project_id=? AND snapshot_date=?
            GROUP BY query HAVING imp >= ? AND ctr_pct < ?
            ORDER BY imp DESC LIMIT ?""",
        (project_id, snapshot_date, min_imp, max_ctr, limit))]


def ctr_gaps(conn: sqlite3.Connection, project_id: int, *, limit: int = 15) -> list[dict]:
    """1페이지(1~10위)인데 기대 CTR의 절반도 못 받는 쿼리 — 제목·설명 손볼 곳.

    최신 스냅샷(같은 period_days)만 본다. 손실 클릭 = 노출 × (기대 - 실제) CTR.
    """
    cur, _, period, _ = snapshot_pair(conn, project_id)
    if not cur:
        return []
    out = []
    for r in conn.execute(
        """SELECT query, ROUND(AVG(position),1) pos, SUM(impressions) imp, SUM(clicks) clk
             FROM gsc_snapshots WHERE project_id=? AND snapshot_date=? AND period_days=?
            GROUP BY query HAVING pos BETWEEN 1 AND ? AND imp >= ?""",
            (project_id, cur, period, PAGE1, CTR_GAP_MIN_IMP)):
        expected = EXPECTED_CTR[min(max(round(r["pos"]), 1), STRIKING_HI)]
        actual = r["clk"] * 100.0 / r["imp"]
        if actual < expected * CTR_GAP_FACTOR:
            out.append({"query": r["query"], "position": r["pos"], "impressions": r["imp"],
                        "clicks": r["clk"], "actual_ctr": round(actual, 2),
                        "expected_ctr": expected,
                        "lost_clicks": round(r["imp"] * (expected - actual) / 100)})
    return sorted(out, key=lambda x: -x["lost_clicks"])[:limit]


def cannibalization(conn: sqlite3.Connection, project_id: int, *, limit: int = 15) -> list[dict]:
    """같은 쿼리에 내 페이지 2개 이상이 노출을 나눠 갖는 경우 (키워드 카니벌라이제이션).

    page 차원이 필요하다 — page가 NULL인 구버전(CSV 시절) 스냅샷에서는
    이 함수는 자동으로 빈 결과를 돌려준다 (결함이 아니라 데이터 부재).
    부페이지 노출 비중 >= CANNI_MIN_SHARE, 합산 노출 >= CANNI_MIN_IMP 일 때만 잡는다.
    """
    cur, _, period, _ = snapshot_pair(conn, project_id)
    if not cur:
        return []
    per_q: dict[str, list[dict]] = {}
    for r in conn.execute(
        """SELECT query, page, SUM(impressions) imp, SUM(clicks) clk,
                  ROUND(AVG(position),1) pos
             FROM gsc_snapshots
            WHERE project_id=? AND snapshot_date=? AND period_days=? AND page IS NOT NULL
            GROUP BY query, page""", (project_id, cur, period)):
        per_q.setdefault(r["query"], []).append(
            {"page": r["page"], "impressions": r["imp"], "clicks": r["clk"], "position": r["pos"]})
    out = []
    for q, pages in per_q.items():
        total = sum(p["impressions"] for p in pages)
        if len(pages) < 2 or total < CANNI_MIN_IMP:
            continue
        pages.sort(key=lambda p: -p["impressions"])
        if pages[1]["impressions"] < total * CANNI_MIN_SHARE:
            continue
        out.append({"query": q, "impressions": total, "pages": pages})
    return sorted(out, key=lambda x: -x["impressions"])[:limit]


def _ctr_pct(clicks, impressions, nd: int = 1) -> float:
    """CTR(%)은 언제나 clicks/impressions 로 다시 센다.

    저장된 ctr 컬럼은 아무도 안 읽는 죽은 컬럼이다 — GSC 가 준 값은 행 단위라
    기간·기기로 합치면 '평균의 평균'이 되어 실제와 어긋난다. ctr_gaps·
    pseo_candidates 가 이미 SQL 에서 같은 계산을 한다.
    """
    return round((clicks or 0) * 100.0 / impressions, nd) if impressions else 0.0


def _latest(conn: sqlite3.Connection, sql: str, params: tuple) -> str | None:
    """MAX(날짜) 한 칸 뽑기. 빈 테이블이면 NULL 이 오므로 None 으로 접는다."""
    row = conn.execute(sql, params).fetchone()
    return row[0] if row and row[0] else None


_LATEST_BD = "SELECT MAX(snapshot_date) FROM gsc_breakdown WHERE project_id=? AND dim=?"
_LATEST_IX = "SELECT MAX(checked_date) FROM gsc_index_status WHERE project_id=?"


def daily_trend(conn: sqlite3.Connection, project_id: int, days: int = 28) -> list[dict]:
    """gsc_daily 최근 days 일, 날짜 오름차순 — 화면의 추이 그래프 원본.

    gsc_snapshots 는 '기간 합계 한 덩어리'라 곡선을 못 그린다. gsc_daily 의 date 는
    성과일(수집일이 아니다)이므로 그대로 x축이 된다.
    """
    rows = conn.execute(
        """SELECT date, clicks, impressions, position FROM gsc_daily
            WHERE project_id=? ORDER BY date DESC LIMIT ?""",
        (project_id, days)).fetchall()
    return [{"date": r["date"], "clicks": r["clicks"], "impressions": r["impressions"],
             "ctr": _ctr_pct(r["clicks"], r["impressions"], 2),
             "position": round(r["position"], 1) if r["position"] is not None else None}
            for r in reversed(rows)]


def device_gap(conn: sqlite3.Connection, project_id: int, *, limit: int = 15) -> list[dict]:
    """모바일이 데스크톱보다 확 밀리는 쿼리 — 콘텐츠가 아니라 모바일 화면을 볼 곳.

    dpos 는 양수 = 모바일이 그만큼 아래. rank_decay·movers 의 dpos(음수=하락)와
    부호 규약이 반대인데, 여기선 '격차'라 크면 나쁜 쪽이 자연스럽다.
    """
    cur = _latest(conn, _LATEST_BD, (project_id, "device"))
    if not cur:
        return []
    per_q: dict[str, dict] = {}
    for r in conn.execute(
        """SELECT dim_value, query, SUM(clicks) clk, SUM(impressions) imp,
                  AVG(position) pos
             FROM gsc_breakdown
            WHERE project_id=? AND snapshot_date=? AND dim='device'
            GROUP BY dim_value, query""", (project_id, cur)):
        per_q.setdefault(r["query"], {})[(r["dim_value"] or "").upper()] = r
    out = []
    for q, d in per_q.items():
        m, k = d.get("MOBILE"), d.get("DESKTOP")
        # 한쪽만 잡힌 쿼리는 비교 자체가 불가능하다 (모바일 전용 쿼리도 실제로 있다)
        if not (m and k) or m["imp"] < DEVICE_MIN_IMP:
            continue
        dpos = round(m["pos"] - k["pos"], 1)
        if dpos < DEVICE_GAP_POS:
            continue
        out.append({"query": q, "mobile_pos": round(m["pos"], 1),
                    "desktop_pos": round(k["pos"], 1), "dpos": dpos,
                    "mobile_imp": m["imp"],
                    "mobile_ctr": _ctr_pct(m["clk"], m["imp"]),
                    "desktop_ctr": _ctr_pct(k["clk"], k["imp"])})
    return sorted(out, key=lambda x: -x["mobile_imp"])[:limit]


def _indexed(coverage_state: str | None) -> bool:
    """coverage_state 문자열이 '색인됨'인가.

    GSC 는 로케일에 따라 영/한을 섞어 준다("Submitted and indexed" /
    "제출되었으며 색인이 생성됨"). 부정형("Crawled - currently not indexed" /
    "현재 색인이 생성되지 않음")이 긍정 단어를 포함하므로 부정을 먼저 본다.
    """
    c = (coverage_state or "").lower()
    if not c:
        return False
    if "not indexed" in c or "생성되지" in c or "안 됨" in c or "안됨" in c:
        return False
    return "indexed" in c or "색인" in c


def _index_bucket(r: sqlite3.Row) -> tuple[str, str]:
    """색인 실패 원인 한 가지로 접기 → (bucket, 사람이 읽을 한 줄).

    순서가 정보다: robots 로 막혀 있으면 fetch 실패는 결과일 뿐이고, canonical 이
    엇갈렸는지는 페이지를 가져올 수 있어야 의미가 있다.
    """
    robots = (r["robots_txt_state"] or "").upper()
    fetch = (r["page_fetch_state"] or "").upper()
    gc, uc = (r["google_canonical"] or "").strip(), (r["user_canonical"] or "").strip()
    if robots and robots != "ALLOWED":
        return "robots_blocked", "robots.txt 가 크롤을 막고 있다 — 허용으로 풀기 전엔 절대 색인 안 된다"
    if fetch and fetch != "SUCCESSFUL":
        return "fetch_error", f"구글이 페이지를 못 가져왔다({fetch}) — 서버 응답·리다이렉트부터 확인"
    if gc and uc and gc != uc:
        return "canonical_mismatch", (f"구글이 고른 대표 URL 이 다르다: {gc} (내 선언 {uc}) — "
                                      f"중복 페이지를 정리하거나 canonical 을 맞춰라")
    state = r["coverage_state"] or r["indexing_state"] or r["verdict"] or "원인 미상"
    return "not_indexed", f"색인 안 됨({state}) — 내부 링크·사이트맵으로 크롤을 유도해라"


def index_issues(conn: sqlite3.Connection, project_id: int) -> list[dict]:
    """최신 색인 점검에서 걸러진 URL — verdict 가 PASS 가 아니거나 색인이 안 된 것.

    limit 이 없는 건 게으름이 아니다 — URL Inspection API 는 하루 할당이 작아
    (config index_urls 기본 20) 검사한 URL 자체가 이미 소수다.
    """
    cur = _latest(conn, _LATEST_IX, (project_id,))
    if not cur:
        return []
    out = []
    for r in conn.execute(
        """SELECT url, verdict, coverage_state, robots_txt_state, page_fetch_state,
                  indexing_state, google_canonical, user_canonical
             FROM gsc_index_status WHERE project_id=? AND checked_date=?
            ORDER BY url""", (project_id, cur)):
        if (r["verdict"] or "").upper() == "PASS" and _indexed(r["coverage_state"]):
            continue
        bucket, detail = _index_bucket(r)
        out.append({"url": r["url"], "bucket": bucket, "verdict": r["verdict"],
                    "coverage_state": r["coverage_state"], "detail": detail})
    # 손대는 순서: 막힌 것 → 못 가져온 것 → 대표 URL 엇갈림 → 그냥 색인 안 됨
    order = {"robots_blocked": 0, "fetch_error": 1, "canonical_mismatch": 2, "not_indexed": 3}
    return sorted(out, key=lambda x: (order[x["bucket"]], x["url"]))


def _snap_agg(conn: sqlite3.Connection, project_id: int, snapshot_date: str,
              period: int) -> dict[str, dict]:
    """스냅샷 하나를 movers() 입력 모양(query -> {pos, clk, imp})으로 집계."""
    return {r["query"]: {"pos": r["pos"], "clk": r["clk"], "imp": r["imp"]}
            for r in conn.execute(
                """SELECT query, AVG(position) pos, SUM(clicks) clk, SUM(impressions) imp
                     FROM gsc_snapshots
                    WHERE project_id=? AND snapshot_date=? AND period_days=?
                    GROUP BY query""", (project_id, snapshot_date, period))}


def rank_decay(conn: sqlite3.Connection, project_id: int, *, limit: int = 15) -> list[dict]:
    """직전 스냅샷 대비 DECAY_POS 이상 하락한 쿼리 — 방어 기회 (scoring.md 1절).

    비교 짝은 snapshot_pair()가 고른다 — 같은 period_days끼리만 (scoring.md 4-3b).
    dpos 는 movers()와 같은 부호 규약: 음수 = 하락.
    """
    cur, prev, period, _ = snapshot_pair(conn, project_id)
    if not (cur and prev):
        return []
    now_, before = _snap_agg(conn, project_id, cur, period), \
        _snap_agg(conn, project_id, prev, period)
    rows = []
    for q, r in now_.items():
        b = before.get(q)
        if not b:
            continue
        dpos = round(b["pos"] - r["pos"], 1)
        if dpos <= DECAY_POS:
            rows.append({"query": q, "pos": round(r["pos"], 1), "prev_pos": round(b["pos"], 1),
                         "dpos": dpos, "clk": r["clk"], "dclk": r["clk"] - b["clk"],
                         "imp": r["imp"]})
    return sorted(rows, key=lambda x: x["dpos"])[:limit]


def classify_intent(keyword: str) -> str:
    """결정적 인텐트 분류 — transactional > commercial > navigational > info.

    기본값은 코드, 보정은 Claude/사람. 사람이 Claude 와 같이 적어둔 intent 는
    절대 덮지 않는다 (load() 의 _backfill_intents 가 NULL 행만 건드린다).
    기존 norm()/tokens() 로 토큰화 — 한글 토큰('가격', '후기')도 그대로 매칭.
    """
    ts = set(tokens(keyword))
    if ts & INTENT_TRANSACTIONAL:
        return "transactional"
    if ts & INTENT_COMMERCIAL:
        return "commercial"
    if ts & INTENT_NAVIGATIONAL:
        return "navigational"
    return "info"


def _backfill_intents(conn: sqlite3.Connection, project_id: int) -> int:
    """intent 가 NULL 인 활성 키워드만 채운다. 이미 적힌 값은 보존.

    호출은 load() 가 한다 — 분류 기본값은 코드가 깔고, Claude/사람이 손본 건
    그 손이 이김. 비활성 키워드는 애초에 의도 추적이 아니라 후보라 안 본다.
    """
    n = 0
    for r in conn.execute(
            "SELECT id, keyword FROM keywords "
            "WHERE project_id=? AND is_active=1 AND intent IS NULL",
            (project_id,)).fetchall():
        conn.execute("UPDATE keywords SET intent=? WHERE id=?",
                     (classify_intent(r["keyword"]), r["id"]))
        n += 1
    conn.commit()
    return n


def _fit_of(conn: sqlite3.Connection, project_id: int, target: str) -> float:
    """기회 row 의 fit 근사 — 데이터로 답할 수 있는 만큼만 결정적으로.

    fit 의 진짜 판정은 Claude 가 하지만, 0.5 중립으로 두면 w_fit 가 큰 프리셋
    (local_clinic 0.45) 에서 모든 기회가 점수 면적 한가운데만 차지한다.
    활성 키워드와 일치하면 0.8, cluster 매칭이면 0.65, 그 외 0.5.
    """
    t = norm(target)

    # coverage 행은 target='cluster:{name}' — by_cluster 에 들어왔다는 건
    # 그 cluster 가 활성 키워드를 가진다는 뜻 (coverage() 의 SQL 조건)
    if target.startswith("cluster:"):
        cname = target.split(":", 1)[1].strip()
        n = conn.execute(
            "SELECT COUNT(*) FROM keywords "
            "WHERE project_id=? AND is_active=1 AND cluster=?",
            (project_id, cname)).fetchone()[0]
        return 0.65 if n > 0 else 0.5

    rows = list(conn.execute(
        "SELECT keyword, cluster FROM keywords "
        "WHERE project_id=? AND is_active=1",
        (project_id,)).fetchall())

    # tier 1: target.norm == 어떤 active keyword 의 norm → 직접 추적 중인 의제
    for r in rows:
        if norm(r["keyword"]) == t:
            return 0.8
    # tier 2: 정확 일치가 없을 때 — cluster 소속 active keyword 와 norm 같거나
    #          target 문자열이 cluster 명을 포함 (topic 만 겹친 경우)
    for r in rows:
        if r["cluster"] and norm(r["keyword"]) == t:
            return 0.65
    for r in rows:
        if r["cluster"] and norm(r["cluster"]) in t:
            return 0.65
    return 0.5


def coverage(conn: sqlite3.Connection, project_id: int) -> dict:
    """미커버 활성 키워드 — directory 프리셋의 최소 구현 (scoring.md 1절 coverage).

    정의(정직하게): '커버됨' = 최신 GSC 스냅샷에 같은 문자열(norm 비교)의 쿼리가
    노출>0으로 존재하거나, rank_snapshots 최신 체크에 position이 있음. 부분 일치·
    의미 유사·페이지 수 카운트는 보지 않는다 — 그건 Claude 판단 몫으로 남긴다.
    반환: {"keywords": [{keyword, cluster}...], "by_cluster": {cluster: 미커버 수}}.
    """
    cur, _, period, _ = snapshot_pair(conn, project_id)
    seen = set()
    if cur:
        seen = {norm(r[0]) for r in conn.execute(
            """SELECT query FROM gsc_snapshots
                WHERE project_id=? AND snapshot_date=? AND period_days=? AND impressions>0""",
            (project_id, cur, period))}
    missing, by_cluster = [], {}
    for r in conn.execute(
            "SELECT id, keyword, cluster FROM keywords WHERE project_id=? AND is_active=1",
            (project_id,)):
        if norm(r["keyword"]) in seen:
            continue
        rank = conn.execute(
            """SELECT position FROM rank_snapshots WHERE keyword_id=?
                ORDER BY checked_at DESC, id DESC LIMIT 1""", (r["id"],)).fetchone()
        if rank and rank["position"] is not None:
            continue
        cl = r["cluster"] or "(미분류)"
        missing.append({"keyword": r["keyword"], "cluster": cl})
        by_cluster[cl] = by_cluster.get(cl, 0) + 1
    return {"keywords": missing, "by_cluster": by_cluster}


def score(kind: str, metrics: dict, project_type: str) -> float:
    """결정적 0~100 점수 — 같은 입력이면 언제나 같은 출력 (계수는 WEIGHTS).

    scoring.md 2절의 점수 프레임을 코드화한 최소판이다. w_fit(관련성)은 Claude가
    metrics["fit"](0~1)로 넘길 수 있고, 안 넘기면 중립값 0.5 — 그래도 결정적이다.
    """
    w = WEIGHTS.get(project_type) or WEIGHTS["saas"]
    imp = float(metrics.get("impressions") or metrics.get("imp") or 0)
    demand = min(1.0, math.log10(1 + imp) / 5.0)          # 노출 10만이면 1.0
    pos = metrics.get("position", metrics.get("pos"))
    # 순위 미확인이면 보수적(0.3) — 신뢰 낮은 추정치엔 보수적 (scoring.md 2절)
    reach = 0.3 if pos is None else max(0.0, 1.0 - gap_to_page1(pos) / PAGE1)
    fit = float(metrics.get("fit", 0.5))
    ai = float(metrics.get("ai",
                           1.0 if kind in ("ai_citation_gap", "aio_exposure") else 0.0))
    raw = (w["w_demand"] * demand + w["w_reach"] * reach
           + w["w_fit"] * fit + w["w_ai"] * ai)
    return round(min(100.0, max(0.0, raw * 100)), 1)


def load(project: str) -> None:
    """서브커맨드 load — 분석 함수 전부 돌려 opportunities 에 적재.

    projects.type 을 읽어 프리셋 계수(WEIGHTS)를 적용한다 — 분석 코드가 type 을
    안 읽던 결함의 수정. 트리아지 상태(acked·done·dismissed) 보존은
    db.upsert_opportunities 가 보장한다 (ON CONFLICT에서 status 미변경).
    """
    import db  # lazy — 모듈을 불러도 진짜 Brain 은 안 건드린다 (self-check 는 SCHEMA 문자열만 읽는다)
    conn = db.connect()
    p = db.get_project(conn, project)
    pid, ptype = p["id"], p["type"] or "saas"
    cfg = {}
    if p["config_path"]:
        try:
            cfg = db.load_project_yaml(p["config_path"])
        except (SystemExit, ImportError):   # yaml 이 없어도 적재는 계속한다 (브랜드 필터만 얕아짐)
            pass
    cur, prev, _, _ = snapshot_pair(conn, pid)
    brands = foreign_brands(conn, pid, cfg)
    # 의도 미분류(NULL)만 채움 — Claude/사람 보정은 살아남음
    n_intent = _backfill_intents(conn, pid)
    rows = []
    for r in striking(conn, pid, cur, brands=brands):
        rows.append({"kind": "striking_distance", "target": r["query"],
                     "score": score("striking_distance",
                                    {"impressions": r["imp"], "position": r["pos"],
                                     "fit": _fit_of(conn, pid, r["query"])}, ptype),
                     "reasoning": f"{r['pos']}위·노출 {r['imp']:,}·클릭 {r['clk']:,} — "
                                  f"1페이지까지 {r['gap']} ({r['band']}) (gsc {cur})"})
    for r in ctr_gaps(conn, pid):
        rows.append({"kind": "ctr_gap", "target": r["query"],
                     "score": score("ctr_gap",
                                    {"impressions": r["impressions"],
                                     "position": r["position"],
                                     "fit": _fit_of(conn, pid, r["query"])}, ptype),
                     "reasoning": f"{r['position']}위·노출 {r['impressions']:,}·CTR "
                                  f"{r['actual_ctr']}%(기대 {r['expected_ctr']}%) — "
                                  f"손실 약 {r['lost_clicks']:,}클릭/기간 (gsc {cur})"})
    for r in cannibalization(conn, pid):
        tops = " vs ".join(pg["page"] for pg in r["pages"][:2])
        rows.append({"kind": "cannibalization", "target": r["query"],
                     "score": score("cannibalization",
                                    {"impressions": r["impressions"],
                                     "position": r["pages"][0]["position"],
                                     "fit": _fit_of(conn, pid, r["query"])}, ptype),
                     "reasoning": f"페이지 {len(r['pages'])}개가 노출 {r['impressions']:,} 분산 — "
                                  f"{tops} (gsc {cur})"})
    for r in rank_decay(conn, pid):
        rows.append({"kind": "rank_decay", "target": r["query"],
                     "score": score("rank_decay",
                                    {"impressions": r["imp"], "position": r["pos"],
                                     "fit": _fit_of(conn, pid, r["query"])}, ptype),
                     "reasoning": f"{r['prev_pos']}위 → {r['pos']}위 (Δ{r['dpos']})·"
                                  f"클릭 {r['dclk']:+d} — 방어 필요 (gsc {prev}→{cur})"})
    for r in pseo_candidates(conn, pid, cur, limit=10):
        rows.append({"kind": "pseo_pattern", "target": r["query"],
                     "score": score("pseo_pattern",
                                    {"impressions": r["imp"], "position": r["pos"],
                                     "fit": _fit_of(conn, pid, r["query"])}, ptype),
                     "reasoning": f"노출 {r['imp']:,}·CTR {r['ctr_pct']}%·{r['pos']}위 — "
                                  f"pSEO 군집 후보, 군집화는 Claude 판단 (scoring.md 1b) "
                                  f"(gsc {cur})"})
    # 분해·색인은 gsc_snapshots 와 수집일이 어긋날 수 있다 (분해 수집을 끄면 뒤처진다)
    # — 출처 표기에 cur 을 쓰면 없던 날짜를 말하게 되므로 각자의 최신일을 따로 읽는다.
    bd = _latest(conn, _LATEST_BD, (pid, "device"))
    for r in device_gap(conn, pid):
        rows.append({"kind": "device_gap", "target": r["query"],
                     "score": score("device_gap",
                                    {"impressions": r["mobile_imp"], "position": r["mobile_pos"],
                                     "fit": _fit_of(conn, pid, r["query"])}, ptype),
                     "reasoning": f"모바일 {r['mobile_pos']}위 vs 데스크톱 {r['desktop_pos']}위 "
                                  f"(Δ{r['dpos']})·모바일 노출 {r['mobile_imp']:,}·"
                                  f"CTR {r['mobile_ctr']}% vs {r['desktop_ctr']}% — "
                                  f"모바일에서만 밀린다 (gsc {bd})"})
    ix = _latest(conn, _LATEST_IX, (pid,))
    for r in index_issues(conn, pid):
        # 색인 안 된 URL 은 순위가 없다 — position None 을 score() 가 보수적 0.3 으로 본다
        rows.append({"kind": "index_blocked", "target": r["url"],
                     "score": score("index_blocked",
                                    {"impressions": 0, "position": None,
                                     "fit": _fit_of(conn, pid, r["url"])}, ptype),
                     "reasoning": f"{r['detail']} (gsc 색인 {ix})"})
    for cl, n in sorted(coverage(conn, pid)["by_cluster"].items(), key=lambda x: -x[1]):
        rows.append({"kind": "coverage", "target": f"cluster:{cl}",
                     "score": score("coverage",
                                    {"impressions": 0, "position": None,
                                     "fit": _fit_of(conn, pid, f"cluster:{cl}")}, ptype),
                     "reasoning": f"활성 키워드 {n}개가 GSC 노출·순위 체크 모두 부재 — "
                                  f"클러스터 '{cl}'"})
    with db.run(conn, pid, "analysis") as r:
        n = db.upsert_opportunities(conn, pid, r.id, rows)
        r.notes = f"scoring load: opps={n}, intents_filled={n_intent}"
    print(f"loaded {len(rows)} opportunities for '{project}' (type={ptype}, gsc {cur}; "
          f"intents_filled={n_intent})")


def opportunities(conn: sqlite3.Connection, project_id: int, *,
                  limit: int, with_id: bool = False) -> list[dict]:
    """기회 목록 — 화면과 박제본이 같은 정렬을 본다.

    정렬이 두 벌이던 시절엔 대시보드와 리포트가 같은 데이터로 다른 순서를 보여줬다.
    """
    cols = ("id, kind, target, ROUND(score,1) score, reasoning, status, "
            "substr(created_at,1,10) created") if with_id else \
           "kind, target, ROUND(score,1) score, reasoning, status"
    return [dict(r) for r in conn.execute(
        f"""SELECT {cols} FROM opportunities WHERE project_id=?
             ORDER BY (status='new') DESC, score DESC, id DESC LIMIT ?""",
        (project_id, limit))]


def stage(p: dict, name: str, domain: str) -> dict:
    """갱도 6단계 판정 — "지금 할 것"을 정하는 곳은 여기 하나다.

    p 는 dashboard.progress() 가 센 단계별 실적. done 은 그 실적으로만 판정한다 —
    "했다 치고" 넘어가지 않는다. 순서가 정보다: 앞 단계가 없으면 뒤 단계는 재료가
    없어 돌지 않는다. 이 판정이 템플릿 JS 에 있던 시절엔 Python 에서 시험할 수
    없었고, doctor·화면이 서로 다른 다음 할 일을 말할 수 있었다.
    """
    steps = [
        {"id": "register", "t": "사이트 등록",
         "gain": "측정할 도메인과 시작 검색어를 정합니다. 여기서 출발합니다.",
         "done": True, "state": domain or "", "cmd": None},
        {"id": "gsc", "t": "구글 실적 읽기",
         "gain": "서치콘솔에서 실제 노출·클릭·순위를 가져옵니다. 이 도구의 모든 판단이 "
                 "이 숫자 위에서 이뤄집니다 — 추측을 안 하려고 제일 먼저 합니다.",
         "done": p["gsc_days"] > 0,
         "state": (f"{p['gsc_days']}번 읽음 · 최근 {p['gsc_last']}" if p["gsc_days"]
                   else "아직 안 읽음"),
         "cmd": f"/capture gsc {name}"},
        {"id": "keywords", "t": "키워드 캐기",
         "gain": "자동완성으로 후보를 모아 추적할 목록을 만듭니다. 무료이고, 여기서 "
                 "늘린 만큼 다음 단계가 볼 게 많아집니다.",
         "done": p["keywords_found"] > 0,
         "state": (f"캔 것 {p['keywords_found']}개 · 추적 {p['keywords']}개"
                   if p["keywords_found"]
                   else (f"직접 적은 {p['keywords']}개뿐" if p["keywords"] else "아직 없음")),
         "cmd": f"/capture keywords {name}"},
        {"id": "ai", "t": "AI 노출 확인",
         "gain": "ChatGPT·Perplexity·Gemini가 이 주제에서 누구를 인용하는지 봅니다. "
                 "브라우저로 직접 물어보면 키 없이 무료입니다.",
         "done": p["ai_checks"] > 0,
         "state": (f"답변 {p['ai_checks']}개 확인 · 질문 {p['ai_prompts']}개"
                   if p["ai_checks"]
                   else ("질문은 준비됨 · 아직 안 물어봄" if p["ai_prompts"]
                         else "물어볼 질문부터 필요")),
         "cmd": (f"/seo-miner:browse {name}" if p["ai_prompts"] else f"/capture add {name}")},
        {"id": "gaps", "t": "손댈 것 뽑기",
         "gain": "모은 숫자에서 기회를 계산합니다 — 조금만 밀면 1페이지인 검색어, 우리 "
                 "대신 인용되는 곳, 같은 틀로 여러 장 찍을 수 있는 페이지.",
         "done": p["opps"] > 0,
         "state": f"{p['opps']}건 뽑음" if p["opps"] else "아직 없음",
         "cmd": f"/capture gaps {name}"},
        {"id": "create", "t": "실제로 고치기",
         "gain": "뽑은 기회를 리포의 진짜 콘텐츠 변경으로 만듭니다. 브랜치와 PR로 "
                 "나가고, 끝나면 그 기회가 완료로 닫힙니다.",
         "done": p["creations"] > 0,
         "state": f"{p['creations']}건 고침" if p["creations"] else "아직 한 건도 안 함",
         "cmd": f"/create plan {name}"},
    ]
    here = next((i for i, s in enumerate(steps) if not s["done"]), -1)
    return {"steps": steps, "here": here}


def _selfcheck() -> None:
    assert norm("Future Tools") == "futuretools"
    assert host_of("https://www.Ecrett.com/pricing?a=1") == "ecrett.com"
    assert owns("blog.example.com", "example.com")
    assert owns("example.com", "https://example.com/")
    assert not owns("notexample.com", "example.com")
    assert _stem("futuretools.io") == "futuretools"

    brands = {"ecrett", "futuretools", "paperpal"}
    assert is_foreign_brand("ecrett", brands)
    assert is_foreign_brand("paperpal 후기", brands)
    assert is_foreign_brand("future tools", brands)
    assert is_foreign_brand("Ecrett Pricing", brands)
    assert not is_foreign_brand("ecrett alternative", brands)     # 비교 의도는 남긴다
    assert not is_foreign_brand("ecrett vs paperpal", brands)
    assert not is_foreign_brand("aitierlist", brands)             # 내 브랜드
    assert not is_foreign_brand("ai 툴 순위", brands)
    assert not is_foreign_brand("", brands)
    assert not is_foreign_brand("ecrett", set())                  # 카탈로그 없으면 아무것도 안 뺀다

    m, c, others = judge("Try Ecrett and MySite today.",
                         ["https://www.ecrett.com/a", "https://blog.mysite.com/b"],
                         ["MySite"], "mysite.com")
    assert (m, c, others) == (1, 1, ["ecrett.com"]), (m, c, others)
    assert judge("nothing here", [], ["MySite"], "mysite.com") == (0, 0, [])
    # 인용 메타데이터가 없으면 본문의 맨 URL을 줍는다 — collect_ai·record_check 공통
    assert judge("see https://blog.mysite.com/x", [], ["MySite"],
                 "mysite.com")[1] == 1
    assert aliases_of({"name": "MySite", "brand_aliases": ["마이사이트", ""]}) \
        == ["MySite", "마이사이트"]

    pr = {"gsc_days": 0, "gsc_last": "", "keywords": 2, "keywords_found": 0,
          "ai_checks": 0, "ai_prompts": 0, "opps": 0, "creations": 0}
    st = stage(pr, "demo", "demo.com")
    assert st["here"] == 1 and st["steps"][1]["id"] == "gsc", st["here"]
    assert st["steps"][3]["cmd"] == "/capture add demo"       # 질문이 없으면 add 부터
    st = stage({**pr, "gsc_days": 3, "gsc_last": "2026-08-14", "keywords_found": 5,
                "ai_prompts": 10}, "demo", "demo.com")
    assert st["here"] == 3
    assert st["steps"][3]["cmd"] == "/seo-miner:browse demo"  # 질문이 있으면 browse
    st = stage({**pr, "gsc_days": 1, "keywords_found": 1, "ai_checks": 1,
                "ai_prompts": 1, "opps": 1, "creations": 1}, "demo", "demo.com")
    assert st["here"] == -1                                    # 한 바퀴 다 돎

    assert gap_to_page1(14.2) == 4.2
    assert gap_to_page1(3.0) == 0.0
    assert gap_to_page1(None) == 0.0

    # 0.5 이하 변화는 노이즈 — 예전 0.4 임계에서는 통과하던 값
    assert not moved_up(0.45, 0)
    assert moved_up(0.6, 0)
    assert moved_up(0.0, 3)
    assert moved_down(-0.6, 0)
    assert not moved_down(-0.45, 0)

    now_ = {"a": {"pos": 5.0, "clk": 10, "imp": 100},
            "b": {"pos": 12.0, "clk": 1, "imp": 90}}
    before = {"a": {"pos": 8.0, "clk": 4, "imp": 100},
              "b": {"pos": 9.0, "clk": 5, "imp": 90}}
    ups, downs = movers(now_, before)
    assert [u["query"] for u in ups] == ["a"], ups
    assert [d["query"] for d in downs] == ["b"], downs

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    import db  # lazy — self-check 는 진짜 Brain 을 안 건드리고 정본 SCHEMA 만 읽는다
    conn.executescript(db.SCHEMA)
    conn.execute("INSERT INTO projects(id, name, type, domain) "
                 "VALUES(1, 'selfcheck', 'saas', 'selfcheck.com')")
    conn.execute("INSERT INTO competitors(project_id, domain) VALUES(1, 'ecrett.com')")
    conn.executemany(
        "INSERT INTO gsc_snapshots(project_id, snapshot_date, period_days, query, page, clicks, impressions, ctr, position) "
        "VALUES(1, '2026-08-14', 28, ?, NULL, ?, ?, 0.0, ?)",
        [("ecrett", 0, 48, 8.6), ("내 키워드", 2, 400, 12.0), ("1페이지 키워드", 9, 300, 3.0)])
    brands = foreign_brands(conn, 1, {"name": "aitierlist"})
    assert brands == {"ecrett"}, brands
    rows = striking(conn, 1, "2026-08-14", brands=brands)
    assert [r["query"] for r in rows] == ["내 키워드"], rows   # 3.0위는 구간 밖, ecrett는 남의 브랜드
    assert rows[0]["gap"] == 2.0
    assert striking(conn, 1, None) == []
    cands = pseo_candidates(conn, 1, "2026-08-14")
    assert [c["query"] for c in cands] == ["내 키워드"], cands

    conn.executemany(
        "INSERT INTO opportunities(project_id,kind,target,score,reasoning,status,created_at) "
        "VALUES(1,'striking_distance',?,?,'r',?,'2026-08-01')",
        [("old-done", 99, "done"), ("new-low", 10, "new"), ("new-high", 50, "new")])
    got = [o["target"] for o in opportunities(conn, 1, limit=10)]
    assert got == ["new-high", "new-low", "old-done"], got
    assert "id" in opportunities(conn, 1, limit=1, with_id=True)[0]

    conn.execute("INSERT INTO gsc_snapshots(project_id,snapshot_date,period_days,query) "
                 "VALUES(2,'2026-08-20',28,'_meta')")
    conn.execute("INSERT INTO gsc_snapshots(project_id,snapshot_date,period_days,query) "
                 "VALUES(2,'2026-08-10',90,'_meta')")
    conn.execute("INSERT INTO gsc_snapshots(project_id,snapshot_date,period_days,query) "
                 "VALUES(2,'2026-08-01',28,'_meta')")
    cur, prev, period, mismatch = snapshot_pair(conn, 2)
    assert (cur, prev, period, mismatch) == ("2026-08-20", "2026-08-01", 28, False), (cur, prev, period, mismatch)

    conn.execute("INSERT INTO gsc_snapshots(project_id,snapshot_date,period_days,query) "
                 "VALUES(3,'2026-08-20',28,'_meta')")
    conn.execute("INSERT INTO gsc_snapshots(project_id,snapshot_date,period_days,query) "
                 "VALUES(3,'2026-08-10',90,'_meta')")
    cur, prev, period, mismatch = snapshot_pair(conn, 3)
    assert (cur, prev, period, mismatch) == ("2026-08-20", None, 28, True), (cur, prev, period, mismatch)

    # ── 신규 판정 함수들 ──
    assert RANK_NOISE == 3
    assert sorted(EXPECTED_CTR) == list(range(1, 21))                 # 1~20위 전부
    assert all(EXPECTED_CTR[i] >= EXPECTED_CTR[i + 1] for i in range(1, 20))  # 단조 감소
    for w in WEIGHTS.values():
        assert abs(sum(w.values()) - 1.0) < 1e-9

    # ctr_gap: 1페이지 키워드(3.0위·300노출·9클릭=CTR 3%)만 기대 10%의 절반 미만.
    # ecrett 는 노출 48 < CTR_GAP_MIN_IMP, 내 키워드는 12위라 1페이지 밖.
    gaps = ctr_gaps(conn, 1)
    assert [g["query"] for g in gaps] == ["1페이지 키워드"], gaps
    assert gaps[0]["expected_ctr"] == EXPECTED_CTR[3]
    assert gaps[0]["lost_clicks"] == 21, gaps[0]                      # 300×(10%-3%)

    # 프로젝트 5: 스냅샷 페어 + band·노출 하한·카니벌·decay·coverage 한 번에
    conn.executemany(
        "INSERT INTO gsc_snapshots(project_id, snapshot_date, period_days, query, page, clicks, impressions, ctr, position) "
        "VALUES(5, '2026-08-07', 28, ?, NULL, ?, ?, 0.0, ?)",
        [("하락", 10, 100, 5.0), ("유지", 5, 100, 5.0)])
    conn.executemany(
        "INSERT INTO gsc_snapshots(project_id, snapshot_date, period_days, query, page, clicks, impressions, ctr, position) "
        "VALUES(5, '2026-08-14', 28, ?, ?, ?, ?, 0.0, ?)",
        [("하락", None, 2, 100, 9.0), ("유지", None, 5, 100, 5.4),
         ("b1", None, 1, 150, 6.0), ("b2", None, 1, 150, 15.0),
         ("tiny", None, 1, 5, 6.0),
         ("canni", "/a", 3, 60, 4.0), ("canni", "/b", 1, 40, 7.0),
         ("solo", "/a", 5, 95, 3.0), ("solo", "/b", 0, 4, 3.0)])
    srows = {r["query"]: r for r in striking(conn, 5, "2026-08-14")}
    assert "tiny" not in srows, srows                                  # 노출 하한
    assert srows["b1"]["band"] == "page1" and srows["b2"]["band"] == "page2"

    c = cannibalization(conn, 5)
    assert [x["query"] for x in c] == ["canni"], c    # solo 는 부페이지 비중 4% — 독점
    assert c[0]["impressions"] == 100 and len(c[0]["pages"]) == 2

    rd = rank_decay(conn, 5)
    assert [x["query"] for x in rd] == ["하락"], rd    # 유지(-0.4)는 DECAY_POS 위
    assert rd[0]["dpos"] == -4.0 and rd[0]["dclk"] == -8, rd[0]

    conn.executemany("INSERT INTO keywords(id, project_id, keyword, cluster, intent, is_active) "
                       "VALUES(?, ?, ?, ?, ?, ?)",
                       [(1, 5, "B1", "c1", None, 1),          # gsc 노출 있음(b1, norm 일치) → 커버
                        (2, 5, "순위만", None, None, 1),        # rank 체크로 커버
                        (3, 5, "미커버", "c1", None, 1),
                        (4, 5, "꺼짐", "c1", None, 0)])        # 비활성은 안 본다
    conn.execute("INSERT INTO rank_snapshots(keyword_id, checked_at, position) "
                 "VALUES(2, '2026-08-14T00:00:00Z', 7)")
    cov = coverage(conn, 5)
    assert [k["keyword"] for k in cov["keywords"]] == ["미커버"], cov
    assert cov["by_cluster"] == {"c1": 1}, cov

    # ── daily_trend / device_gap / index_issues (프로젝트 7) ──
    assert _ctr_pct(20, 1240) == 1.6 and _ctr_pct(3, 0) == 0.0      # 0 노출은 0%, 나눗셈 안 함
    assert daily_trend(conn, 7) == []                                # 데이터 없으면 빈 목록

    conn.executemany(
        "INSERT INTO gsc_daily(project_id, date, clicks, impressions, ctr, position) "
        "VALUES(7, ?, ?, ?, 0.0, ?)",
        [("2026-08-01", 5, 500, 9.0), ("2026-08-02", 10, 400, 8.0),
         ("2026-08-03", 0, 0, None)])
    tr = daily_trend(conn, 7)
    assert [d["date"] for d in tr] == ["2026-08-01", "2026-08-02", "2026-08-03"], tr  # 오름차순
    assert tr[1]["ctr"] == 2.5 and tr[1]["position"] == 8.0, tr[1]   # ctr 은 저장값 아닌 재계산
    assert tr[2]["ctr"] == 0.0 and tr[2]["position"] is None
    assert [d["date"] for d in daily_trend(conn, 7, 2)] == ["2026-08-02", "2026-08-03"]  # 최근 N

    conn.executemany(
        "INSERT INTO gsc_breakdown(project_id, snapshot_date, period_days, dim, dim_value, query, clicks, impressions, ctr, position) "
        "VALUES(7, ?, 28, 'device', ?, ?, ?, ?, 0.0, ?)",
        [("2026-08-10", "MOBILE", "옛날", 0, 900, 30.0),             # 옛 수집일 — 안 본다
         ("2026-08-10", "DESKTOP", "옛날", 0, 900, 3.0),
         ("2026-08-17", "MOBILE", "모바일밀림", 20, 1240, 12.4),
         ("2026-08-17", "DESKTOP", "모바일밀림", 50, 500, 7.1),
         ("2026-08-17", "MOBILE", "차이없음", 5, 300, 5.0),          # Δ0.5 — 임계 미만
         ("2026-08-17", "DESKTOP", "차이없음", 5, 300, 4.5),
         ("2026-08-17", "MOBILE", "노출작음", 0, 10, 20.0),          # 모바일 노출 하한 미만
         ("2026-08-17", "DESKTOP", "노출작음", 3, 100, 5.0),
         ("2026-08-17", "MOBILE", "모바일만", 1, 900, 25.0)])        # 짝이 없으면 비교 불가
    dg = device_gap(conn, 7)
    assert [d["query"] for d in dg] == ["모바일밀림"], dg
    assert dg[0]["dpos"] == 5.3 and dg[0]["mobile_imp"] == 1240
    assert dg[0]["mobile_ctr"] == 1.6 and dg[0]["desktop_ctr"] == 10.0, dg[0]
    assert set(dg[0]) == {"query", "mobile_pos", "desktop_pos", "dpos", "mobile_imp",
                          "mobile_ctr", "desktop_ctr"}, dg[0]

    assert index_issues(conn, 7) == []
    conn.executemany(
        "INSERT INTO gsc_index_status(project_id, checked_date, url, verdict, coverage_state, robots_txt_state, page_fetch_state, indexing_state, google_canonical, user_canonical, last_crawled, rich_results_json) "
        "VALUES(7, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)",
        [("2026-08-10", "/old", "FAIL", "Blocked", "DISALLOWED", None, None, None, None),
         ("2026-08-18", "/ok", "PASS", "Submitted and indexed", "ALLOWED", "SUCCESSFUL",
          "INDEXING_ALLOWED", "/ok", "/ok"),
         ("2026-08-18", "/ok-ko", "PASS", "제출되었으며 색인이 생성됨", "ALLOWED",
          "SUCCESSFUL", None, None, None),
         ("2026-08-18", "/robots", "FAIL", "Blocked by robots.txt", "DISALLOWED",
          "BLOCKED_ROBOTS_TXT", None, None, None),
         ("2026-08-18", "/fetch", "FAIL", "Not found (404)", "ALLOWED", "NOT_FOUND",
          None, None, None),
         ("2026-08-18", "/canon", "PARTIAL", "Duplicate", "ALLOWED", "SUCCESSFUL",
          "INDEXING_ALLOWED", "/canon-real", "/canon"),
         ("2026-08-18", "/ko-notidx", "PASS", "크롤링됨 - 현재 색인이 생성되지 않음",
          "ALLOWED", "SUCCESSFUL", None, "/ko-notidx", "/ko-notidx")])
    ix = index_issues(conn, 7)
    assert [i["url"] for i in ix] == ["/robots", "/fetch", "/canon", "/ko-notidx"], ix
    assert [i["bucket"] for i in ix] == ["robots_blocked", "fetch_error",
                                         "canonical_mismatch", "not_indexed"], ix
    # verdict 가 PASS 여도 coverage 가 색인됨이 아니면 잡힌다 (한국어 부정형)
    assert ix[3]["verdict"] == "PASS" and "색인 안 됨" in ix[3]["detail"], ix[3]
    assert set(ix[0]) == {"url", "bucket", "verdict", "coverage_state", "detail"}, ix[0]
    assert _indexed("Submitted and indexed") and not _indexed("Crawled - currently not indexed")
    assert _indexed("색인이 생성됨") and not _indexed("색인 생성 안 됨") and not _indexed(None)

    # ── classify_intent: 4개 인텐트 + 우선순위 (transactional > commercial > navigational > info)
    assert classify_intent("비트코인 가격") == "transactional"          # 가격
    assert classify_intent("ecrett pricing") == "transactional"        # pricing
    assert classify_intent("ecrett 후기") == "commercial"               # 후기
    assert classify_intent("Best AI Tools") == "commercial"             # best
    assert classify_intent("chatgpt login") == "navigational"           # login
    assert classify_intent("example.com 공식") == "navigational"        # 공식
    assert classify_intent("날씨") == "info"
    assert classify_intent("외부 링크") == "info"
    assert classify_intent("") == "info"
    # 우선순위: pricing 은 transactional/commercial 양쪽 사전에 있지만 transactional 이 이김
    assert classify_intent("best pricing") == "transactional"
    assert classify_intent("Buy reviews") == "transactional"            # buy가 review보다 먼저
    # 사전 자체: 우선순위대로 정확히 매칭되는지 (한 토큰씩 확인)
    assert "가격" in INTENT_TRANSACTIONAL and "후기" in INTENT_COMMERCIAL \
        and "공식" in INTENT_NAVIGATIONAL

    # ── _backfill_intents: NULL 인 활성만 채우고, 값 있는 건 보존
    conn.execute("DELETE FROM keywords")
    conn.executemany(
        "INSERT INTO keywords(id, project_id, keyword, cluster, intent, is_active) "
        "VALUES(?, ?, ?, ?, ?, ?)",
        [(10, 5, "비트코인 가격", None, None, 1),         # NULL → 채움
         (11, 5, "ecrett 후기", None, None, 1),           # NULL → 채움
         (12, 5, "공식 홈페이지", None, None, 1),         # NULL → 채움
         (13, 5, "외부 링크", None, None, 1),             # NULL → info
         (14, 5, "사람보정", None, "transactional", 1),   # 보존
         (15, 5, "비활성널", None, None, 0)])              # 비활성 — 안 본다
    assert _backfill_intents(conn, 5) == 4
    got = {r["keyword"]: r["intent"]
           for r in conn.execute("SELECT keyword, intent FROM keywords WHERE project_id=5",
                                  ()).fetchall()}
    assert got["비트코인 가격"] == "transactional"
    assert got["ecrett 후기"] == "commercial"
    assert got["공식 홈페이지"] == "navigational"
    assert got["외부 링크"] == "info"
    assert got["사람보정"] == "transactional"                          # 보존 확인
    assert got["비활성널"] is None                                      # 그대로
    # 두 번째 호출은 채울 게 없음
    assert _backfill_intents(conn, 5) == 0

    # ── _fit_of: 0.8 활성 키워드 일치 / 0.65 cluster 매칭 / 0.5 무관 / coverage 0.65
    conn.execute("DELETE FROM keywords")
    conn.executemany(
        "INSERT INTO keywords(id, project_id, keyword, cluster, intent, is_active) "
        "VALUES(?, ?, ?, ?, ?, ?)",
        [(20, 6, "비트코인 가격", None, None, 1),
         (21, 6, "암호화폐 시장", "암호화폐", None, 1),
         (22, 6, "서울 여행", "여행", None, 1)])
    assert _fit_of(conn, 6, "비트코인 가격") == 0.8                 # 정확 일치
    assert _fit_of(conn, 6, "비트코인  가격") == 0.8               # 공백·norm 동일
    assert _fit_of(conn, 6, "암호화폐 시세") == 0.65               # target 안에 cluster 명
    assert _fit_of(conn, 6, "완전히 다른 검색어") == 0.5            # 무관
    assert _fit_of(conn, 6, "cluster:암호화폐") == 0.65            # coverage 행
    assert _fit_of(conn, 6, "cluster:없는클러스터") == 0.5          # active 키워드 0개
    # 일치하는 active 키워드는 없지만 다른 cluster 키워드와 겹치지 않는 경우 — 0.5
    assert _fit_of(conn, 6, "서울 맛집") == 0.5

    s = score("striking_distance", {"impressions": 4200, "position": 3.0}, "saas")
    assert s == score("striking_distance", {"impressions": 4200, "position": 3.0}, "saas")
    assert 0.0 <= s <= 100.0
    assert score("ai_citation_gap", {"impressions": 100}, "saas") > \
        score("ai_citation_gap", {"impressions": 100}, "local_clinic")  # saas 는 w_ai 최상향
    assert score("striking_distance", {"impressions": 100, "position": 5.0}, "없는타입") == \
        score("striking_distance", {"impressions": 100, "position": 5.0}, "saas")

    print("scoring self-check ok")


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "load":
        load(sys.argv[2])
    else:
        _selfcheck()
