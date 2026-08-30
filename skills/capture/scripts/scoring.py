#!/usr/bin/env python3
"""판정 규칙 한 곳 — striking distance·움직임·남의 브랜드·pSEO 후보·기회 목록.

여기가 생기기 전에는 같은 규칙이 SQL(dashboard·collect_gsc)·Python·템플릿 JS·
references/scoring.md 산문 네 군데에 흩어져 있었고 값이 서로 어긋나 있었다
(움직임 임계 0.4 vs 0.5). 임계값을 바꾸려면 이 파일만 고친다.

references/scoring.md 는 이제 명세이고, 실행은 전부 여기서 한다.

self-check:  python scoring.py
기회 적재:   python scoring.py load <project>   (striking·ctr_gap·… 계산 → opportunities upsert)
"""
import json
import math
import re
import sqlite3
import sys
from collections import namedtuple

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

# 순위 구간 — 화면·산문이 같은 경계를 본다. GSC 평균순위를 반올림해 넣는다.
RANK_BANDS = ((1, 3, "1–3위"), (4, PAGE1, "4–10위"),
              (PAGE1 + 1, STRIKING_HI, "11–20위"), (STRIKING_HI + 1, None, "21위+"))

# 원인 분해에서 검색어 하나를 이름 붙여 세울 노출 하한. 총계는 전부 세지만(안 그러면
# 합이 Δ클릭과 안 맞는다), 노출 몇 개짜리 CTR 흔들림을 "원인"이라 부르지는 않는다.
SHIFT_MIN_IMP = 30

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

# 이 리포가 만드는 기회 종류 한 벌 — 화면의 KIND_LABEL·PLAY 와 짝이 맞아야 한다
# (짝이 어긋나면 라벨 없는 영문 kind 가 화면에 그대로 뜬다). stage._check_seams 가
# 이 튜플을 정규식으로 읽는다 — 리터럴 문자열 나열 그대로 둬야 한다(파생시키지 않는다).
# 이름·순서의 정본은 여기다. 각 kind 의 나머지(검출기·라벨·방어 여부 등)는 아래
# KINDS 명부(_KIND_SPECS)가 이 순서를 그대로 따라가며 채운다 — DEFENSIVE_KINDS 도
# 거기서 파생된다 (is_defensive() 는 그 결과를 읽는다).
ALL_KINDS = ("striking_distance", "ctr_gap", "cannibalization", "rank_decay",
             "pseo_pattern", "device_gap", "index_blocked", "coverage",
             "ai_citation_gap", "aio_exposure", "content_gap",
             "crawl_issue", "backlink_broken", "backlink_prospect")



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

    측정 경로가 각자 조립하면 별칭 규칙이 갈라져
    judge 가 같아도 수치가 어긋난다."""
    return [a for a in [cfg.get("name", "")] + (cfg.get("brand_aliases") or []) if a]


def judge(content: str, citation_urls: list[str] | None, aliases: list[str],
          own_domain: str) -> tuple[int, int, list[str]]:
    """AI 답변 하나를 (언급됐나, 인용됐나, 대신 인용된 도메인들) 로 판정.

    인용 판정은 여기 하나뿐이어야 한다 — 부르는 쪽이 collect_ai 내부를
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


def rank_delta(prev: int | float | None, cur: int | float | None) -> dict:
    """SERP 정수 순위의 변화를 판정한다.

    |Δ| < RANK_NOISE 면 보합(flat). 마크업은 반환하지 않고 순수 값 dict만 돌려준다.
    """
    if prev is None or cur is None:
        return {"delta": None, "flat": False}
    d = int(round(float(prev) - float(cur)))
    return {"delta": d, "flat": abs(d) < RANK_NOISE}


def is_defensive(kind: str | None) -> bool:
    """기회 종류가 방어형(지키는 기회: rank_decay, cannibalization)인가."""
    return bool(kind and kind in DEFENSIVE_KINDS)



def snapshot_dates(conn: sqlite3.Connection, project_id: int, *,
                   limit: int = 60) -> list[dict]:
    """화면의 [기준 수집일] 목록 — 실제로 수집한 날만.

    달력 위젯이 아니라 목록인 이유가 여기 있다: 스냅샷은 수집한 날에만 있고,
    달력은 없는 날을 고르게 한다. 고를 수 있는 것과 데이터가 있는 것을 같게 둔다.
    """
    return [{"date": r[0], "period": r[1]} for r in conn.execute(
        """SELECT snapshot_date, MAX(period_days) FROM gsc_snapshots
            WHERE project_id=? GROUP BY snapshot_date ORDER BY 1 DESC LIMIT ?""",
        (project_id, limit))]


def snapshot_pair(conn: sqlite3.Connection, project_id: int,
                  at: str | None = None) -> tuple[str | None, str | None, int | None, bool]:
    """직전 스냅샷을 같은 period_days 중 가장 최근으로 고른다 (scoring.md 4-3b).

    기간이 다른 스냅샷끼리 빼면 Δ순위·Δ클릭이 전부 거짓이 된다.

    at: 기준 수집일을 그날로 고정한다 (화면의 [기준 수집일] 선택). None 이면 최신.
        비교 짝(prev)은 고정한 날보다 **이전** 중에서 고른다 — 과거 시점으로
        돌아가도 Δ가 "그때 기준의 변화"여야지 미래를 빼면 부호가 뒤집힌다.
        없는 날짜를 받으면 빈 짝을 준다 — 있지도 않은 날의 숫자를 지어내지 않는다.
    """
    # at 이 있으면 LIMIT 을 뺀다 — 10회보다 오래된 날을 고르면 목록에서 못 찾는다.
    snaps = [(r[0], r[1]) for r in conn.execute(
        """SELECT snapshot_date, MAX(period_days) period_days
             FROM gsc_snapshots WHERE project_id=?
            GROUP BY snapshot_date ORDER BY 1 DESC""" + ("" if at else " LIMIT 10"),
        (project_id,)).fetchall()]
    if at:
        i = next((n for n, (d, _) in enumerate(snaps) if d == at), None)
        if i is None:
            return None, None, None, False
        snaps = snaps[i:]
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


def ctr_gaps(conn: sqlite3.Connection, project_id: int, *, limit: int = 15,
             at: str | None = None) -> list[dict]:
    """1페이지(1~10위)인데 기대 CTR의 절반도 못 받는 쿼리 — 제목·설명 손볼 곳.

    한 스냅샷(같은 period_days)만 본다. 손실 클릭 = 노출 × (기대 - 실제) CTR.
    at 은 화면이 고정한 기준 수집일 — 안 주면 최신. 화면이 과거로 돌아갔는데 여기만
    최신을 보면 같은 화면 안에서 두 날짜가 섞인다.
    """
    cur, _, period, _ = snapshot_pair(conn, project_id, at)
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


# 페이지 감사 임계값 — 화면·리포트·산문이 같은 숫자를 본다.
# 검색결과가 자르는 것은 글자 수가 아니라 픽셀 폭이라, 한글은 로마자의 약 두 배를
# 먹는다. 한 벌로 두면 한국어 사이트는 멀쩡한 제목마다 "너무 길다"는 경고를 받는다.
TITLE_MAX, TITLE_MAX_KO = 60, 30
TITLE_MIN, TITLE_MIN_KO = 15, 8
DESC_MIN, DESC_MIN_KO = 70, 40
DESC_MAX, DESC_MAX_KO = 160, 80
THIN_WORDS = 300        # 이보다 얇으면 "이 주제를 다뤘다"고 보기 어렵다
_HANGUL = re.compile(r"[가-힣]")


def _wide(text: str) -> bool:
    """폭이 넓은 글자(한글)가 섞였나 — 길이 임계값을 어느 쪽으로 볼지 가른다."""
    return bool(_HANGUL.search(text or ""))


def page_advice(audit: dict | None, queries=(), *, domain: str = "") -> list[dict]:
    """이 페이지의 무엇을 바꿔야 하나 — 결정적 규칙. 화면이 그대로 그린다.

    반환: [{"tag": 손댈 자리, "level": "bad"|"warn", "now": 지금 상태, "fix": 할 일}]
    빈 리스트면 "규칙으로 잡히는 문제 없음"이지 "완벽함"이 아니다.

    queries 는 이 URL 이 실제로 걸린 검색어들(노출 많은 순). 첫 번째를 대표
    검색어로 본다 — 제목에 넣으라고 말하려면 어느 말을 넣으라는 것인지 있어야 한다.
    판정은 여기 한 곳이다: 화면이 같은 규칙을 다시 구현하면 두 벌이 되고,
    사람은 그중 어느 쪽이 맞는지 알 수 없게 된다.
    """
    if not audit:
        return []
    out: list[dict] = []

    def add(tag, level, now, fix):
        out.append({"tag": tag, "level": level, "now": now, "fix": fix})

    if audit.get("error"):
        add("가져오기", "bad", audit["error"],
            "이 URL 이 사람에게도 안 열리는지 확인하세요. 안 열리면 순위·색인 이전의 문제입니다.")
        return out

    kw = next((q for q in queries if q), "")
    title = (audit.get("title") or "").strip()
    if not title:
        add("title", "bad", "title 태그가 없습니다",
            f"<title>{kw or '대표 검색어'} — 페이지 요지</title> 를 넣으세요.")
    else:
        if kw and kw.lower() not in title.lower():
            add("title", "bad", title,
                f"검색어 '{kw}' 가 title 에 없습니다. 앞부분에 그대로 넣으세요.")
        t_max = TITLE_MAX_KO if _wide(title) else TITLE_MAX
        t_min = TITLE_MIN_KO if _wide(title) else TITLE_MIN
        if len(title) > t_max:
            add("title", "warn", f"{len(title)}자 — 검색결과에서 잘립니다",
                f"{t_max}자 이내로 줄이고, 브랜드명은 뒤로 미세요.")
        elif len(title) < t_min:
            add("title", "warn", f"{len(title)}자 — 너무 짧습니다",
                "무엇에 대한 페이지인지 title 만 읽고 알 수 있게 늘리세요.")

    desc = (audit.get("meta_description") or "").strip()
    if not desc:
        add("meta description", "bad", "설명이 없습니다",
            "검색결과에 뜰 2~3문장을 직접 쓰세요 — 없으면 구글이 본문에서 임의로 뽑습니다.")
    elif len(desc) > (DESC_MAX_KO if _wide(desc) else DESC_MAX):
        add("meta description", "warn", f"{len(desc)}자 — 뒤가 잘립니다",
            f"{DESC_MAX_KO if _wide(desc) else DESC_MAX}자 이내로. 중요한 말을 앞에 두세요.")
    elif len(desc) < (DESC_MIN_KO if _wide(desc) else DESC_MIN):
        add("meta description", "warn", f"{len(desc)}자 — 너무 짧습니다",
            "숫자·연도·구체적 이득을 넣어 클릭할 이유를 적으세요.")

    h1 = _as_list(audit.get("h1_json"))
    if not h1:
        add("H1", "bad", "H1 이 없습니다", "페이지 주제를 그대로 담은 H1 하나를 두세요.")
    elif len(h1) > 1:
        add("H1", "warn", f"H1 이 {len(h1)}개입니다 ({' / '.join(h1[:3])})",
            "H1 은 하나만 두고 나머지는 H2 로 내리세요.")
    elif kw and kw.lower() not in h1[0].lower():
        add("H1", "warn", h1[0],
            f"H1 에 '{kw}' 가 없습니다 — title 과 H1 이 같은 말을 하게 맞추세요.")

    words = audit.get("words")
    if isinstance(words, int) and words < THIN_WORDS:
        add("본문", "warn", f"{words}단어 — 얇습니다",
            f"이 검색어를 다루는 하위 질문을 채워 {THIN_WORDS}단어 이상으로 늘리세요.")

    schema = _as_list(audit.get("schema_json"))
    if not schema:
        add("구조화 데이터", "warn", "ld+json 이 없습니다",
            "Article·FAQPage·LocalBusiness 중 이 페이지에 맞는 것 하나를 넣으세요 "
            "— AI 답변과 리치 결과가 읽는 자리입니다.")

    robots = (audit.get("robots") or "").lower()
    if "noindex" in robots:
        add("robots", "bad", f"meta robots: {audit['robots']}",
            "noindex 를 지우기 전에는 이 페이지가 검색에 나오지 않습니다.")

    canon = (audit.get("canonical") or "").strip()
    if canon and host_of(canon) and domain and not owns(host_of(canon), domain):
        add("canonical", "bad", f"canonical 이 남의 도메인을 가리킵니다: {canon}",
            "자기 URL(또는 내 사이트의 정본)을 가리키게 고치세요.")
    elif canon and norm(canon) != norm(audit.get("url") or ""):
        add("canonical", "warn", f"canonical: {canon}",
            "이 URL 이 정본이 아니라고 선언돼 있습니다 — 의도한 것인지 확인하세요.")

    no_alt = audit.get("images_no_alt") or 0
    if no_alt:
        add("이미지", "warn", f"alt 없는 이미지 {no_alt}개",
            "무엇을 보여주는 그림인지 alt 에 적으세요(이미지 검색 유입과 접근성).")

    internal = audit.get("internal_links")
    if isinstance(internal, int) and internal < 3:
        add("내부 링크", "warn", f"이 페이지가 내보내는 내부 링크 {internal}개",
            "관련 글로 3개 이상 연결하세요 — 링크가 없는 페이지는 크롤러도 사람도 덜 봅니다.")
    return out


def _as_list(blob) -> list:
    """JSON 배열 문자열 → list. 깨져 있으면 빈 목록(판정이 멈추지 않는다)."""
    if isinstance(blob, list):
        return blob
    try:
        v = json.loads(blob or "[]")
    except (TypeError, ValueError):
        return []
    return v if isinstance(v, list) else []


def top_pages(conn: sqlite3.Connection, project_id: int, limit: int) -> list[str]:
    """최신 스냅샷의 노출 상위 페이지. page IS NULL(구버전 CSV 스냅샷)은 뺀다.

    snapshot_pair 로 period_days 까지 맞춰 고른다 — 같은 날짜에 기간이 다른
    스냅샷이 섞여 있으면 노출 합계가 이중으로 잡힌다.
    """
    cur, _, period, _ = snapshot_pair(conn, project_id)
    if not cur:
        return []
    return [r["page"] for r in conn.execute(
        """SELECT page, SUM(impressions) imp FROM gsc_snapshots
            WHERE project_id=? AND snapshot_date=? AND period_days=? AND page IS NOT NULL
            GROUP BY page ORDER BY imp DESC LIMIT ?""",
        (project_id, cur, period, limit))]


def pages_by_query(conn: sqlite3.Connection, project_id: int, queries,
                   *, top: int = 5, at: str | None = None) -> dict[str, list[dict]]:
    """검색어 → 그 검색어로 노출된 내 페이지들 (노출 많은 순).

    기회를 펼쳤을 때 "그래서 어느 페이지 얘기냐"에 답하는 자리다. 근거 문장은
    숫자를 요약할 뿐 어느 URL 인지는 말하지 않아서, 화면에서 손댈 곳을 못 찾았다.
    cannibalization 과 같은 스냅샷·같은 period_days 를 본다 — 두 화면이 다른 날짜를
    말하면 같은 검색어가 서로 다른 페이지 수를 갖는다.

    page 가 NULL 인 구버전 스냅샷에서는 빈 dict — 데이터 부재이지 결함이 아니다.
    at 은 화면이 고정한 기준 수집일이다 — 화면이 과거를 보고 있으면 근거 페이지도
    그때 것이어야 한다. 안 넘기면 여기만 최신을 봐서 표와 근거가 어긋난다.
    """
    cur, _, period, _ = snapshot_pair(conn, project_id, at)
    qs = [q for q in dict.fromkeys(queries) if q]
    if not cur or not qs:
        return {}
    out: dict[str, list[dict]] = {}
    for i in range(0, len(qs), 200):        # SQLite 바인딩 상한(999) 안에서 끊는다
        chunk = qs[i:i + 200]
        for r in conn.execute(
            f"""SELECT query, page, SUM(impressions) imp, SUM(clicks) clk,
                       ROUND(AVG(position),1) pos
                  FROM gsc_snapshots
                 WHERE project_id=? AND snapshot_date=? AND period_days=?
                   AND page IS NOT NULL AND query IN ({','.join('?' * len(chunk))})
                 GROUP BY query, page ORDER BY imp DESC""",
                (project_id, cur, period, *chunk)):
            rows = out.setdefault(r["query"], [])
            if len(rows) < top:
                rows.append({"page": r["page"], "impressions": r["imp"], "clicks": r["clk"],
                             "position": r["pos"],
                             "ctr": round(r["clk"] * 100.0 / r["imp"], 2) if r["imp"] else 0.0})
    return out


# ── 페이지 축 ──────────────────────────────────────────────────────────
# 이 리포의 판정은 내내 검색어 단위였다. gsc_snapshots.page 는 내내 있었는데
# pages_by_query 룩업으로만 쓰였다. 검색어 하나하나의 순위는 흔들려도 페이지는 안
# 흔들린다 — 어느 페이지가 죽고 있는지는 페이지로 합쳐야 보인다. 크롤(crawl_pages)과
# GSC 가 서로를 처음 보는 자리이기도 하다.

# 내부링크 굶음: 노출 상위 이만큼 안에 드는 페이지만 본다. 꼬리까지 세면 링크 굶은
# 페이지가 수백 개 나오고, 그건 목록이 아니라 사이트 전체 얘기다.
STARVED_TOP_N = 20
# 들어오는 내부 링크가 이보다 적으면 굶었다고 본다. 3 은 "헤더·푸터·사이트맵에서만
# 걸린 상태"의 경계다 — 그 위면 누군가 본문에서 그 페이지를 가리키고 있다는 뜻이다.
STARVED_LINKS_IN = 3


def url_key(url: str) -> str:
    """GSC 의 page 와 크롤의 url 을 같은 것으로 보기 위한 열쇠.

    둘은 출처가 달라 표기가 어긋난다 — GSC 는 속성에 등록된 형태로, 크롤은 링크에
    적힌 그대로 준다. 스킴·www·끝 슬래시만 벗긴다(host_of 가 앞을, 여기가 뒤를).
    쿼리는 남긴다 — `?page=2` 는 다른 페이지다.
    """
    rest = re.sub(r"^[a-z]+://", "", (url or "").strip().lower()).split("/", 1)
    tail = ("/" + rest[1]) if len(rest) > 1 else "/"
    return host_of(url) + (tail.split("#")[0].rstrip("/") or "/")


def _page_agg(conn: sqlite3.Connection, project_id: int, snapshot_date: str,
              period: int) -> dict[str, dict]:
    """스냅샷 하나를 페이지 단위로 접는다 (_snap_agg 의 페이지 축 짝)."""
    return {r["page"]: dict(r) for r in conn.execute(
        """SELECT page, SUM(clicks) clk, SUM(impressions) imp, AVG(position) pos,
                  COUNT(DISTINCT query) q
             FROM gsc_snapshots
            WHERE project_id=? AND snapshot_date=? AND period_days=? AND page IS NOT NULL
            GROUP BY page""", (project_id, snapshot_date, period))}


def page_performance(conn: sqlite3.Connection, project_id: int, *,
                     limit: int = 30) -> list[dict]:
    """최신 스냅샷의 페이지별 성과 + 직전 대비 Δ클릭 — 많이 잃은 페이지가 위다.

    비교 짝은 snapshot_pair 가 고른다 (scoring.md 4-3b) — 28일치에서 90일치를 빼면
    Δ가 전부 거짓이 된다. page 가 NULL 인 구버전 스냅샷에서는 빈 목록이다
    (결함이 아니라 데이터 부재).
    """
    cur, prev, period, _ = snapshot_pair(conn, project_id)
    if not cur:
        return []
    now_ = _page_agg(conn, project_id, cur, period)
    before = _page_agg(conn, project_id, prev, period) if prev else {}
    rows = []
    for page, r in now_.items():
        b = before.get(page)
        rows.append({"page": page, "clicks": r["clk"], "impressions": r["imp"],
                     "ctr": _ctr_pct(r["clk"], r["imp"], 2),
                     "position": round(r["pos"], 1) if r["pos"] is not None else None,
                     "queries": r["q"],
                     "dclk": (r["clk"] - b["clk"]) if b else None})
    # 잃은 쪽이 먼저다 — 이 표의 존재 이유가 "어느 페이지가 죽고 있나"라서다.
    # 비교 짝이 없거나 이번에 새로 뜬 페이지는 0 자리에 두고 노출 큰 순으로 민다.
    return sorted(rows, key=lambda r: (r["dclk"] or 0, -r["impressions"]))[:limit]


def _latest_crawl_run(conn: sqlite3.Connection, project_id: int) -> int | None:
    """마지막으로 끝까지 돈 크롤 회차. 돌다 만 회차는 전수가 아니라 조각이다."""
    r = conn.execute("SELECT id FROM crawl_runs WHERE project_id=? AND finished_at IS NOT NULL"
                     " ORDER BY id DESC LIMIT 1", (project_id,)).fetchone()
    return r["id"] if r else None


def _gsc_page_keys(conn: sqlite3.Connection, project_id: int) -> set[str]:
    """최신 스냅샷에서 노출이 한 번이라도 있었던 페이지의 열쇠."""
    cur, _, period, _ = snapshot_pair(conn, project_id)
    if not cur:
        return set()
    return {url_key(r[0]) for r in conn.execute(
        """SELECT page FROM gsc_snapshots
            WHERE project_id=? AND snapshot_date=? AND period_days=? AND page IS NOT NULL
            GROUP BY page HAVING SUM(impressions) > 0""", (project_id, cur, period))}


def dead_pages(conn: sqlite3.Connection, project_id: int, *,
               limit: int = 30) -> list[dict]:
    """크롤에는 200 으로 있는데 GSC 노출이 0 인 페이지 — 아무도 안 오는 자리.

    크롤엔 있는데 검색엔 없다는 건 색인·수요·내부링크 중 하나가 없다는 뜻이다.
    GSC 쪽 페이지 축이 통째로 없으면(구버전 스냅샷) 빈 목록을 준다 — 그때는 전
    페이지가 '노출 0' 으로 보이는데, 그건 판정이 아니라 착시다.
    """
    run = _latest_crawl_run(conn, project_id)
    seen = _gsc_page_keys(conn, project_id)
    if run is None or not seen:
        return []
    rows = [{"url": r["url"], "depth": r["depth"], "words": r["words"],
             "links_in": r["links_in"], "title": r["title"]}
            for r in conn.execute(
                "SELECT url, depth, words, links_in, title FROM crawl_pages"
                " WHERE run_id=? AND status=200", (run,))
            if url_key(r["url"]) not in seen]
    # 얕고 링크 많은 페이지가 먼저다 — 사이트가 이미 밀어 주는데도 안 온다는 뜻이라
    # 색인이나 수요 쪽을 봐야 한다. 깊은 페이지는 링크부터가 원인이다.
    return sorted(rows, key=lambda r: (r["depth"] if r["depth"] is not None else 99,
                                       -(r["links_in"] or 0), r["url"]))[:limit]


def starved_pages(conn: sqlite3.Connection, project_id: int, *,
                  limit: int = 15) -> list[dict]:
    """노출은 상위인데 들어오는 내부 링크가 굶은 페이지 — 제일 싸게 손대는 자리.

    성과는 이미 증명됐는데 사이트가 안 밀어 주고 있다는 신호다. 링크 한 줄은 글 한
    편보다 훨씬 싸다. 크롤이 없으면 links_in 을 알 길이 없어 빈 목록이다.
    """
    run = _latest_crawl_run(conn, project_id)
    cur, _, period, _ = snapshot_pair(conn, project_id)
    if run is None or not cur:
        return []
    crawled = {}
    for r in conn.execute("SELECT url, links_in FROM crawl_pages"
                          " WHERE run_id=? AND status=200", (run,)):
        crawled[url_key(r["url"])] = (r["url"], r["links_in"] or 0)
    out = []
    for n, r in enumerate(conn.execute(
        """SELECT page, SUM(clicks) clk, SUM(impressions) imp, AVG(position) pos
             FROM gsc_snapshots
            WHERE project_id=? AND snapshot_date=? AND period_days=? AND page IS NOT NULL
            GROUP BY page ORDER BY imp DESC LIMIT ?""",
            (project_id, cur, period, STARVED_TOP_N)), 1):
        m = crawled.get(url_key(r["page"]))
        if m is None or m[1] >= STARVED_LINKS_IN:
            continue
        out.append({"page": r["page"], "crawl_url": m[0], "links_in": m[1], "rank": n,
                    "impressions": r["imp"], "clicks": r["clk"],
                    "position": round(r["pos"], 1) if r["pos"] is not None else None})
    return sorted(out, key=lambda x: (x["links_in"], -x["impressions"]))[:limit]


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


# 색인 실패 원인 갈래 — _index_bucket() 반환값의 정본이자 손대는 순서(막힌 것 →
# 못 가져온 것 → 대표 URL 엇갈림 → 그냥 색인 안 됨). site.html 의 ST_IX 가 갈래마다
# 라벨·심각도·처방을 갖는다 — 이름이 늘거나 바뀌면 거기서도 고쳐야 한다.
# stage._check_seams 가 이 튜플과 화면을 대조한다.
INDEX_BUCKETS = ("robots_blocked", "fetch_error", "canonical_mismatch", "not_indexed")


def _index_bucket(r: sqlite3.Row) -> tuple[str, str]:
    """색인 실패 원인 한 가지로 접기 → (bucket, 사람이 읽을 한 줄).

    순서가 정보다: robots 로 막혀 있으면 fetch 실패는 결과일 뿐이고, canonical 이
    엇갈렸는지는 페이지를 가져올 수 있어야 의미가 있다. 반환하는 이름은
    INDEX_BUCKETS 의 정본 그대로다.
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
    order = {b: i for i, b in enumerate(INDEX_BUCKETS)}
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


def _band_of(pos) -> str:
    """평균순위를 구간 이름으로. 3.4위는 3위로 읽는다 — 소수점은 표본의 흔들림이다."""
    if pos is None:
        return RANK_BANDS[-1][2]
    n = round(pos)
    for lo, hi, label in RANK_BANDS:
        if n >= lo and (hi is None or n <= hi):
            return label
    return RANK_BANDS[-1][2]


def click_shift(conn: sqlite3.Connection, project_id: int, cur: str | None,
                prev: str | None, period: int | None, *, limit: int = 8) -> dict:
    """Δ클릭이 어디서 왔나 — 노출이 준 건지, 안 눌린 건지, 검색어가 빠진 건지.

    "클릭 -12%" 만으로는 손댈 자리가 안 나온다. 노출이 줄어서 준 것이면 색인·수요를
    봐야 하고, 순위는 그대로인데 CTR 이 준 것이면 제목·설명을 봐야 한다 — 정반대의
    일이다. 쿼리별로 clk = imp × ctr 이므로 두 항으로 정확히 갈린다(중간점 분해):

        Δclk = Δimp × (ctr₀+ctr₁)/2   노출 효과
             + Δctr × (imp₀+imp₁)/2   CTR 효과

    교호항을 따로 두는 흔한 분해 대신 이걸 쓰는 이유는 사람이 읽을 항이 둘뿐이어서다
    (교호항은 부호가 뒤집혀도 뜻을 말할 수 없다). 항등식이라 합은 정확히 맞는다.
    한쪽 스냅샷에만 있는 쿼리는 갈리지 않는다 — 새로 뜬 것·사라진 것으로 센다.
    네 항의 합은 언제나 Δ클릭과 같다 (_selfcheck 가 못 박는다).

    cur/prev/period 는 호출부(snapshot_pair)가 고른 짝을 그대로 받는다 — 여기서
    다시 고르면 화면이 고정한 기준일과 어긋난다.
    """
    out = {"clicks": 0, "prev_clicks": 0, "d_clicks": 0,
           "imp_effect": 0.0, "ctr_effect": 0.0, "new": 0, "lost": 0,
           "up": [], "down": [], "thin": 0}
    if not (cur and prev and period):
        return out
    now_ = _snap_agg(conn, project_id, cur, period)
    before = _snap_agg(conn, project_id, prev, period)
    out["clicks"] = sum(r["clk"] or 0 for r in now_.values())
    out["prev_clicks"] = sum(r["clk"] or 0 for r in before.values())
    out["d_clicks"] = out["clicks"] - out["prev_clicks"]

    rows = []
    for query, r in now_.items():
        b = before.get(query)
        if not b:
            out["new"] += r["clk"] or 0
            rows.append({"query": query, "dclk": r["clk"] or 0, "cause": "new",
                         "imp_effect": 0.0, "ctr_effect": 0.0,
                         "imp": r["imp"], "prev_imp": 0,
                         "ctr": _ctr_pct(r["clk"], r["imp"], 2), "prev_ctr": None,
                         "pos": round(r["pos"], 1), "prev_pos": None})
            continue
        c0 = (b["clk"] or 0) / b["imp"] if b["imp"] else 0.0
        c1 = (r["clk"] or 0) / r["imp"] if r["imp"] else 0.0
        ie = ((r["imp"] or 0) - (b["imp"] or 0)) * (c0 + c1) / 2
        ce = (c1 - c0) * ((b["imp"] or 0) + (r["imp"] or 0)) / 2
        out["imp_effect"] += ie
        out["ctr_effect"] += ce
        rows.append({"query": query, "dclk": (r["clk"] or 0) - (b["clk"] or 0),
                     "cause": "imp" if abs(ie) >= abs(ce) else "ctr",
                     "imp_effect": round(ie, 1), "ctr_effect": round(ce, 1),
                     "imp": r["imp"], "prev_imp": b["imp"],
                     "ctr": round(c1 * 100, 2), "prev_ctr": round(c0 * 100, 2),
                     "pos": round(r["pos"], 1), "prev_pos": round(b["pos"], 1)})
    for query, b in before.items():
        if query in now_:
            continue
        out["lost"] -= b["clk"] or 0
        rows.append({"query": query, "dclk": -(b["clk"] or 0), "cause": "lost",
                     "imp_effect": 0.0, "ctr_effect": 0.0,
                     "imp": 0, "prev_imp": b["imp"], "ctr": None,
                     "prev_ctr": _ctr_pct(b["clk"], b["imp"], 2),
                     "pos": None, "prev_pos": round(b["pos"], 1)})

    for k in ("imp_effect", "ctr_effect"):
        out[k] = round(out[k], 1)
    # 이름 붙여 세우는 자리에서만 표본을 거른다 — 위 총계는 전부 셌다.
    named = [r for r in rows if max(r["imp"] or 0, r["prev_imp"] or 0) >= SHIFT_MIN_IMP]
    out["thin"] = len(rows) - len(named)
    out["down"] = sorted([r for r in named if r["dclk"] < 0],
                         key=lambda r: r["dclk"])[:limit]
    out["up"] = sorted([r for r in named if r["dclk"] > 0],
                       key=lambda r: -r["dclk"])[:limit]
    return out


def rank_bands(conn: sqlite3.Connection, project_id: int, cur: str | None,
               prev: str | None, period: int | None) -> list[dict]:
    """순위 구간별 검색어 수와 그 이동 — 어디에 몰려 있고, 어디로 밀렸나.

    개별 순위 변동은 노이즈가 많지만(4-4), 구간 인원수의 이동은 그 노이즈가 상쇄돼
    "1페이지에서 밀려난 게 몇 개"를 말한다. 노출 하한을 넘는 쿼리만 센다 — 노출
    한두 번짜리 순위는 구간을 말할 표본이 아니다.
    """
    if not (cur and period):
        return []

    def tally(snap):
        c = {}
        for r in _snap_agg(conn, project_id, snap, period).values() if snap else ():
            if (r["imp"] or 0) < SHIFT_MIN_IMP:
                continue
            b = _band_of(r["pos"])
            c[b] = c.get(b, 0) + 1
        return c

    now_, before = tally(cur), tally(prev)
    return [{"label": lb, "n": now_.get(lb, 0),
             "prev_n": before.get(lb, 0) if prev else None,
             "d": now_.get(lb, 0) - before.get(lb, 0) if prev else None}
            for _, _, lb in RANK_BANDS]


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
    import db
    pairs = [(r["id"], classify_intent(r["keyword"])) for r in conn.execute(
        "SELECT id, keyword FROM keywords "
        "WHERE project_id=? AND is_active=1 AND intent IS NULL",
        (project_id,)).fetchall()]
    db.set_keyword_intents(conn, pairs)
    return len(pairs)


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
    missing, by_cluster, vol_by_cluster = [], {}, {}
    for r in conn.execute(
            "SELECT id, keyword, cluster, volume FROM keywords"
            " WHERE project_id=? AND is_active=1", (project_id,)):
        if norm(r["keyword"]) in seen:
            continue
        rank = conn.execute(
            """SELECT position FROM rank_snapshots WHERE keyword_id=?
                ORDER BY checked_at DESC, id DESC LIMIT 1""", (r["id"],)).fetchone()
        if rank and rank["position"] is not None:
            continue
        cl = r["cluster"] or "(미분류)"
        missing.append({"keyword": r["keyword"], "cluster": cl,
                        "volume": r["volume"]})
        by_cluster[cl] = by_cluster.get(cl, 0) + 1
        # 아직 안 뜨는 키워드는 노출이 0이라 수요 신호가 없다 — 검색량이 유일한 근거다.
        # 없으면(NULL) 0으로 둔다: 모르는 것을 있다고 치지 않는다.
        vol_by_cluster[cl] = vol_by_cluster.get(cl, 0) + (r["volume"] or 0)
    return {"keywords": missing, "by_cluster": by_cluster,
            "volume_by_cluster": vol_by_cluster}


_LATEST_KG = "SELECT MAX(checked_date) FROM keyword_gap WHERE project_id=?"
_LATEST_BL = "SELECT MAX(checked_date) FROM backlinks WHERE project_id=?"
_LATEST_LI = "SELECT MAX(checked_date) FROM link_intersect WHERE project_id=?"


def ai_gaps(conn: sqlite3.Connection, project_id: int, *,
            limit: int = 30) -> list[dict]:
    """챗봇이 한 번도 나를 출처로 쓰지 않은 질문 — 최신 ai 회차 기준.

    ai_checks 는 엔진×표본마다 한 줄이라 질문 단위로 접어야 "인용 0"이 성립한다.
    답이 비결정적이라 한 번 빠진 것은 신호가 아니다 — 그 회차 전부에서 빠진 것만 센다.
    """
    run = conn.execute(
        "SELECT MAX(c.run_id) FROM ai_checks c JOIN ai_prompts p ON p.id=c.prompt_id"
        " WHERE p.project_id=?", (project_id,)).fetchone()[0]
    if not run:
        return []
    rows = []
    for r in conn.execute(
            """SELECT p.id, p.prompt, p.category, COUNT(*) checks,
                      SUM(c.mentioned) mentioned, COUNT(DISTINCT c.engine) engines
                 FROM ai_checks c JOIN ai_prompts p ON p.id=c.prompt_id
                WHERE c.run_id=? AND p.is_active=1
             GROUP BY p.id HAVING SUM(c.cited)=0
             ORDER BY COUNT(*) DESC, p.id LIMIT ?""", (run, limit)):
        # 나 대신 누가 인용됐나 — "그래서 무엇을 이겨야 하나"가 근거의 나머지 절반이다
        tally: dict[str, int] = {}
        for c in conn.execute(
                "SELECT cited_domains_json FROM ai_checks"
                " WHERE run_id=? AND prompt_id=?", (run, r["id"])):
            try:
                for dom in json.loads(c[0] or "[]"):
                    h = host_of(str(dom))
                    if h:
                        tally[h] = tally.get(h, 0) + 1
            except (TypeError, ValueError):
                continue
        rivals = sorted(tally.items(), key=lambda x: (-x[1], x[0]))[:2]
        rows.append({"prompt": r["prompt"], "category": r["category"],
                     "checks": r["checks"], "mentioned": r["mentioned"] or 0,
                     "engines": r["engines"], "rivals": [d for d, _ in rivals]})
    return rows


# ── 검색 × AI 교차 ───────────────────────────────────────────────────────────
# 두 축(gsc_snapshots·ai_checks)은 여태 서로를 한 번도 안 봤다. 잇는 다리는 "같은
# 주제인가" 하나뿐인데, AI 질문은 문장이고 검색어는 낱말 뭉치라 문자열로는 절대
# 만나지 않는다 — 토큰 겹침으로 잰다. 휴리스틱이라 임계를 여기 이름 붙여 둔다.
XAI_MIN_TOKEN = 2      # 한 글자 토큰은 조사·수사(이·그·앱·툴)라 겹쳐도 뜻이 없다
XAI_MIN_OVERLAP = 2    # 한 낱말만 겹치면("가격") 아무 질문이 아무 검색어에나 붙는다
XAI_LIMIT = 12         # 화면이 읽는 목록이지 전수 목록이 아니다


def _xai_tokens(s: str) -> set[str]:
    return {t for t in tokens(s) if len(t) >= XAI_MIN_TOKEN}


def _xai_doms(raw) -> list[str]:
    """그 질문에서 대신 인용된 도메인 — 근거의 나머지 절반이다."""
    try:
        return [h for h in (host_of(str(d)) for d in json.loads(raw or "[]")) if h][:3]
    except (TypeError, ValueError):
        return []


def _xai_match(prompt: str, queries: list[dict]) -> dict | None:
    """질문에 가장 많이 겹치는 상위 검색어 하나. 동점이면 노출이 큰 쪽 — 더 큰 자리다."""
    ts, best, hit = _xai_tokens(prompt), None, 0
    for q in queries:
        n = len(ts & q["_ts"])
        if n > hit or (n == hit and n and q["imp"] > best["imp"]):
            best, hit = q, n
    return best if hit >= XAI_MIN_OVERLAP else None


def _xai_top_queries(conn: sqlite3.Connection, project_id: int) -> list[dict]:
    """최신 스냅샷에서 우리가 상위(평균순위 ≤ PAGE1)인 검색어 — 교차의 왼쪽 축."""
    cur, _prev, period, _mm = snapshot_pair(conn, project_id)
    if not cur:
        return []
    return [{"query": q, "pos": round(r["pos"], 1), "imp": r["imp"] or 0,
             "_ts": _xai_tokens(q)}
            for q, r in _snap_agg(conn, project_id, cur, period).items()
            if r["pos"] is not None and r["pos"] <= PAGE1]


def search_wins_ai_loses(conn: sqlite3.Connection, project_id: int,
                         ai_rows: list[dict]) -> dict:
    """검색은 이기는데 AI 는 지는 질문 — "구글 3위인데 ChatGPT 는 우리를 안 쓴다".

    ai_rows 는 gather() 가 이미 최신 AI 런에서 접어 둔 질문별 결과다 — 같은 런을
    본다. 겹치는 검색어가 없는 질문은 여기 넣지 않는다: 그건 [어디에도 안 잡힌
    질문]이 이미 하는 일이고, 섞으면 두 표가 같은 말을 한다.

    비는 이유가 둘이라(상위 검색어가 없다 / 겹치는 질문이 없다) 개수도 같이 낸다.
    """
    tops = _xai_top_queries(conn, project_id)
    rows = []
    for r in ai_rows:
        if r.get("cited"):
            continue
        m = _xai_match(r.get("prompt") or "", tops)
        if not m:
            continue
        rows.append({"prompt": r["prompt"], "category": r.get("category") or "",
                     "query": m["query"], "pos": m["pos"], "imp": m["imp"],
                     "checks": r.get("checks") or 0,
                     "rivals": _xai_doms(r.get("miss_domains"))})
    rows.sort(key=lambda x: (x["pos"], -x["imp"]))
    return {"rows": rows[:XAI_LIMIT], "top_queries": len(tops)}


def ai_outranked(conn: sqlite3.Connection, project_id: int,
                 cite_share: list[dict]) -> dict:
    """AI 는 저쪽을 쓰는데 검색에서는 우리가 위인 경쟁사.

    순위로 지고 있는 게 아니다 — 인용될 근거가 우리 페이지에 없다는 뜻이라, 손댈
    자리가 순위가 아니라 페이지의 모양이다. 그래서 위 표와 처방이 다르다.

    비는 이유가 셋이라(경쟁사 미등록 / keyword_gap 미수집 / 겹치는 자리 없음)
    개수를 같이 낸다 — 화면이 빈 표 대신 이유를 말한다.
    """
    comps = [r[0] for r in conn.execute(
        "SELECT domain FROM competitors WHERE project_id=?", (project_id,))]
    d = _latest(conn, _LATEST_KG, (project_id,))
    cites: dict[str, int] = {}
    for s in cite_share:
        h = host_of(s.get("domain") or "")
        if h:
            cites[h] = cites.get(h, 0) + (s.get("n") or 0)
    rows, cited = [], 0
    for c in comps:
        n = cites.get(host_of(c), 0)
        if not n:
            continue
        cited += 1
        if not d:
            continue
        wins = [dict(r) for r in conn.execute(
            """SELECT keyword, position, our_position, volume FROM keyword_gap
                WHERE project_id=? AND checked_date=? AND domain=?
                  AND our_position IS NOT NULL AND our_position<=?
                  AND our_position<position
             ORDER BY our_position, keyword""", (project_id, d, c, PAGE1))]
        if wins:
            rows.append({"domain": host_of(c), "cites": n, "won": len(wins),
                         "top": wins[:3]})
    rows.sort(key=lambda x: (-x["cites"], x["domain"]))
    return {"rows": rows, "competitors": len(comps), "cited": cited, "gap_date": d}


def aio_gaps(conn: sqlite3.Connection, project_id: int) -> list[dict]:
    """구글이 AI 요약을 붙이는데 거기 내 링크가 없는 검색어 — 최신 순위 회차 기준."""
    d = conn.execute(
        "SELECT MAX(substr(rs.checked_at,1,10)) FROM rank_snapshots rs"
        " JOIN keywords k ON k.id=rs.keyword_id WHERE k.project_id=?",
        (project_id,)).fetchone()[0]
    if not d:
        return []
    return [dict(r) for r in conn.execute(
        """SELECT k.keyword, k.volume, rs.position
             FROM rank_snapshots rs JOIN keywords k ON k.id=rs.keyword_id
            WHERE k.project_id=? AND substr(rs.checked_at,1,10)=?
              AND rs.aio_present=1 AND rs.aio_cited=0
         ORDER BY k.volume IS NULL, k.volume DESC, k.keyword""", (project_id, d))]


def content_gaps(conn: sqlite3.Connection, project_id: int, *,
                 limit: int = 25) -> list[dict]:
    """경쟁사는 잡는데 나는 없거나(missing) 밀리는(weak) 검색어 — keyword_gap 정본."""
    d = _latest(conn, _LATEST_KG, (project_id,))
    if not d:
        return []
    return [dict(r) for r in conn.execute(
        """SELECT keyword, domain, position, our_position, volume, kind
             FROM keyword_gap
            WHERE project_id=? AND checked_date=? AND kind IN ('missing','weak')
         ORDER BY volume IS NULL, volume DESC, keyword LIMIT ?""",
        (project_id, d, limit))]


def crawl_gaps(conn: sqlite3.Connection, project_id: int, *,
               limit: int = 20) -> list[dict]:
    """크롤에서 걸린 것 중 심각한 것만 — 회차 전체는 [사이트 점검] 화면이 표로 본다.

    ponytail: severity='bad' 만 기회로 올린다. 500건을 다 올리면 기회 목록이 크롤
    로그가 된다. warn·info 는 화면의 표에 그대로 있고, 거기서 심각도로 정렬된다.
    """
    cr = conn.execute(
        "SELECT id FROM crawl_runs WHERE project_id=? AND finished_at IS NOT NULL"
        " ORDER BY id DESC LIMIT 1", (project_id,)).fetchone()
    if not cr:
        return []
    return [dict(r) for r in conn.execute(
        """SELECT kind, url, detail FROM crawl_issues
            WHERE run_id=? AND severity='bad' AND url IS NOT NULL
         ORDER BY kind, url LIMIT ?""", (cr["id"], limit))]


def backlink_gaps(conn: sqlite3.Connection, project_id: int, *,
                  limit: int = 15) -> tuple[list[dict], list[dict]]:
    """되찾을 링크(깨진 것)와 새로 받을 곳(경쟁사만 받는 도메인).

    깨진 링크는 이미 번 것이다 — 새로 얻는 것보다 늘 싸다. 그래서 둘을 함께 낸다.
    """
    broken, prospects = [], []
    d = _latest(conn, _LATEST_BL, (project_id,))
    if d:
        broken = [dict(r) for r in conn.execute(
            """SELECT url_from, url_to, domain_from, anchor, rank FROM backlinks
                WHERE project_id=? AND checked_date=? AND is_broken=1
             ORDER BY rank IS NULL, rank DESC LIMIT ?""", (project_id, d, limit))]
    di = _latest(conn, _LATEST_LI, (project_id,))
    if di:
        prospects = [dict(r) for r in conn.execute(
            """SELECT domain, rank, hits, targets FROM link_intersect
                WHERE project_id=? AND checked_date=? AND we_have=0
             ORDER BY hits DESC, rank IS NULL, rank DESC LIMIT ?""",
            (project_id, di, limit))]
    return broken, prospects


def score(kind: str, metrics: dict, project_type: str) -> float:
    """결정적 0~100 점수 — 같은 입력이면 언제나 같은 출력 (계수는 WEIGHTS).

    scoring.md 2절의 점수 프레임을 코드화한 최소판이다. w_fit(관련성)은 Claude가
    metrics["fit"](0~1)로 넘길 수 있고, 안 넘기면 중립값 0.5 — 그래도 결정적이다.
    """
    w = WEIGHTS.get(project_type) or WEIGHTS["saas"]
    imp = float(metrics.get("impressions") or metrics.get("imp") or 0)
    # 수요는 여태 GSC 노출수 하나였다 — 그러면 **이미 뜨는 검색어만** 점수를 받는다.
    # 아직 순위가 없는 검색어는 노출이 0이라 늘 바닥이었고, 그게 "새로 쓸 글"을
    # 고를 때 이 점수가 쓸모없던 이유다. 월 검색량(collect_metrics)이 있으면 큰 쪽을
    # 쓴다: GSC 노출은 28일치, 검색량은 월 단위라 자릿수가 서로 견줄 만하다.
    vol = float(metrics.get("volume") or 0)
    demand = min(1.0, math.log10(1 + max(imp, vol)) / 5.0)   # 10만이면 1.0
    pos = metrics.get("position", metrics.get("pos"))
    # 순위 미확인이면 보수적(0.3) — 신뢰 낮은 추정치엔 보수적 (scoring.md 2절)
    reach = 0.3 if pos is None else max(0.0, 1.0 - gap_to_page1(pos) / PAGE1)
    fit = float(metrics.get("fit", 0.5))
    ai = float(metrics.get("ai",
                           1.0 if kind in ("ai_citation_gap", "aio_exposure") else 0.0))
    raw = (w["w_demand"] * demand + w["w_reach"] * reach
           + w["w_fit"] * fit + w["w_ai"] * ai)
    return round(min(100.0, max(0.0, raw * 100)), 1)


# 기회 종류 명부 — 한 행이 "어떻게 찾고(detect) · score()에 뭘 넘기고(metrics) ·
# 무엇을 대상(target)이라 부르고 · 근거 문장을 어떻게 쓰는지(reasoning)"를 다 쥔다.
# load() 는 이 명부를 순회할 뿐 kind 문자열을 직접 적지 않는다 — 명부에 없는 kind 는
# 나올 수가 없다(구조적으로), 명부에 있는데 빠지는 kind 도 없다(전부 돈다).
#
# ALL_KINDS(위)가 이름·순서의 정본이다 — stage._check_seams 가 그 튜플을 정규식으로
# 읽는다. KINDS 는 ALL_KINDS 를 그대로 따라가며 _KIND_SPECS 에서 나머지를 채운
# 파생값이다(반대 방향이 아니라 이 방향인 이유: ALL_KINDS 가 문자열 리터럴 나열
# 그대로여야 그 정규식이 계속 읽을 수 있다). DEFENSIVE_KINDS 는 KINDS 에서 파생된다.
#
# 검출기 시그니처가 저마다 달라(striking 은 brands=, pseo_pattern 은 limit=10,
# backlink_* 는 backlink_gaps() 한 번의 앞/뒤 절반) 억지로 한 모양에 밀어 넣지
# 않는다 — detect 는 ctx(dict) 하나만 받는 걸로 통일해 그 안에서 각자 필요한 인자를
# 골라 쓰게 한다.
#
# play(=처방: what/acts/deliver)는 detect/metrics/target/reasoning 과 성격이 다르다 —
# load() 가 원본 행(r)을 도는 동안 쓰이는 게 아니라, gather() 가 이미 적재된 기회를
# 화면에 낼 때(kind_play()를 통해) 쓰인다. dashboard.html 의 window.PLAY 산문이
# 그대로 옮겨왔다 — 문구는 한 글자도 새로 쓰지 않았다.
_Kind = namedtuple("_Kind", "name label defensive detect metrics target reasoning play")


def _coverage_rows(ctx: dict) -> list[dict]:
    """coverage() 는 클러스터별 dict 하나를 주지, per-row 목록을 안 준다 — 여기서 편다."""
    cov = coverage(ctx["conn"], ctx["pid"])
    cov_vol = cov.get("volume_by_cluster") or {}
    return [{"cluster": cl, "n": n, "vol": cov_vol.get(cl, 0)}
            for cl, n in sorted(cov["by_cluster"].items(), key=lambda x: -x[1])]


def _reason_backlink_prospect(r: dict, ctx: dict) -> str:
    tg = [t.strip() for t in (r["targets"] or "").split(",")[:2] if t.strip()]
    s = f"경쟁사 {r['hits']}곳이 여기서 링크를 받는데 나는 없습니다"
    if tg:
        s += f" — {', '.join(tg)}"
    if r["rank"]:
        s += f" (지수 {r['rank']})"
    return s


# striking_distance 는 4~20위 한 kind 를 밴드 둘(band: page1/page2)로 갈라 처방한다.
# "what" 뒤에 실제로는 옛 화면에서 sdAvgNote()(평균 게재순위 안내 + [순위] 화면
# 버튼)가 이어 붙었다 — 그 버튼(window.go)은 화면에서만 만들 수 있어 여기 텍스트에는
# 없다. dashboard.html 의 oppDetail 이 이어 붙인다(둘 다 같은 문장을 뒤에 단다).
_SD_PLAY = {
    "page1": dict(
        what="이미 1페이지 안입니다 — 여기서 남은 것은 순위보다 클릭입니다.",
        acts=["title 앞쪽에 검색어를 두고 숫자·연도를 붙여 옆 결과와 다르게 보이게 합니다.",
              "meta description 을 검색 의도에 대한 한 문장 답으로 바꿉니다.",
              "질문 바로 아래 40~60자 직답 블록을 둡니다 — 강조 스니펫이 거기서 나옵니다.",
              "상단 3위권을 노린다면 상위 페이지에만 있는 구간을 본문에 채웁니다."],
        deliver=["새 title 3안 — 검색어를 앞에, 숫자나 연도를 붙여서",
                 "meta description 2안",
                 "질문 바로 아래 넣을 40~60자 직답 문안"]),
    "page2": dict(
        what="1페이지 진입까지 몇 칸 남았고, 그 몇 칸이 클릭의 대부분입니다.",
        acts=["아래 페이지의 title 과 H1 을 이 검색어로 시작하게 고칩니다.",
              "상위 페이지가 답하는 하위 질문 중 빠진 것을 본문에 채웁니다.",
              "이미 순위가 있는 다른 글에서 이 페이지로 내부 링크를 겁니다.",
              "표·목록·정의 블록을 만들어 스니펫 후보로 올립니다."],
        deliver=["새 title 3안과 H1 문안 — 검색어를 앞에 두고",
                 "본문에 추가할 H2 목록(상위 페이지가 답하는데 내게 없는 질문)",
                 "이 페이지로 내부 링크를 걸 글과 앵커 텍스트 3개"]),
}

# content_gap 은 "아예 없다"(missing)와 "밀린다"(weak)를 한 kind 로 담는다 — 할 일이
# 정반대다(새로 쓴다 vs 있는 걸 고친다). 갈래는 content_gaps()가 낸 원본 행의
# kind('missing'/'weak')가 안다.
_CG_PLAY = {
    "weak": dict(
        what="경쟁 도메인이 나보다 위에 있습니다 — 페이지는 있으니 새로 쓰는 게 아니라 그 페이지를 고칩니다.",
        acts=["나를 이기고 있는 그 페이지의 목차와 내 페이지를 나란히 놓고 빠진 구간을 찾습니다.",
              "그 검색어를 title 과 H1 앞쪽으로 올립니다.",
              "내 제품·데이터로만 말할 수 있는 구간을 하나 더 넣습니다 — 같은 말을 더 길게 쓰는 건 소용없습니다.",
              "이미 순위가 있는 다른 글에서 이 페이지로 내부 링크를 겁니다."],
        deliver=["내 페이지에 추가할 H2 목록 — 이기고 있는 페이지와 비교해서",
                 "새 title 3안과 H1 문안", "이 페이지로 내부 링크를 걸 글과 앵커 텍스트 3개"]),
    "missing": dict(
        what="경쟁 도메인은 잡고 있는데 나는 페이지 자체가 없는 검색어입니다.",
        acts=["상위 3개 페이지의 목차를 훑어 다뤄야 할 구간을 정합니다.",
              "내 제품·데이터로만 말할 수 있는 구간을 하나 넣습니다.",
              "발행 후 관련 글에서 내부 링크를 겁니다."],
        deliver=["이 검색어를 정면으로 다루는 새 글의 제목·목차", "내 제품·데이터로만 쓸 수 있는 구간 하나"]),
}


_KIND_SPECS = {
    "striking_distance": dict(
        label="밀면 오를 검색어", defensive=False,
        detect=lambda ctx: striking(ctx["conn"], ctx["pid"], ctx["cur"], brands=ctx["brands"]),
        metrics=lambda r, ctx: {"impressions": r["imp"], "position": r["pos"],
                                 "fit": _fit_of(ctx["conn"], ctx["pid"], r["query"])},
        target=lambda r, ctx: r["query"],
        # 이 kind 는 4~20위를 잡고 band 로 갈린다. 4~10위는 이미 1페이지라
        # "1페이지까지 0.0"이라는 문장이 뜻을 잃는다 — 밴드마다 다르게 말한다.
        # "12.8위"는 실측 순위로 읽힌다 — 직접 검색해도 안 보인다는 문의가
        # 여기서 나왔다. GSC 가 보고한 기간 평균이라고 앞에서 못 박는다.
        reasoning=lambda r, ctx: (
            f"평균 {r['pos']}위·노출 {r['imp']:,}·클릭 {r['clk']:,} — "
            + (f"이미 1페이지, 상단(3위권)까지 {round(max(0.0, r['pos'] - 3), 1)}칸"
               if r["band"] == "page1" else f"1페이지까지 {r['gap']}")
            + f" ({r['band']}) (gsc {ctx['cur']})"),
        play=_SD_PLAY),
    "ctr_gap": dict(
        label="CTR 미달", defensive=False,
        detect=lambda ctx: ctr_gaps(ctx["conn"], ctx["pid"]),
        metrics=lambda r, ctx: {"impressions": r["impressions"], "position": r["position"],
                                 "fit": _fit_of(ctx["conn"], ctx["pid"], r["query"])},
        target=lambda r, ctx: r["query"],
        reasoning=lambda r, ctx: (
            f"{r['position']}위·노출 {r['impressions']:,}·CTR "
            f"{r['actual_ctr']}%(기대 {r['expected_ctr']}%) — "
            f"손실 약 {r['lost_clicks']:,}클릭/기간 (gsc {ctx['cur']})"),
        play=dict(
            what="순위는 1페이지인데 클릭률이 기대치의 절반 이하 — 순위가 아니라 제목·설명 문제입니다.",
            acts=["title 앞 60자 안에 검색어를 넣고, 브랜드명은 뒤로 밉니다.",
                  "meta description 에 숫자·연도·구체적 이득을 적습니다.",
                  "FAQ·HowTo 스키마로 검색 결과에서 차지하는 면적을 넓힙니다.",
                  "검색 의도와 제목이 어긋나 있지 않은지 확인합니다(정보형 검색에 판매 제목)."],
            deliver=["새 title 3안 — 한글 30자 이내, 검색어를 앞에",
                     "meta description 2안 — 한글 80자 이내",
                     "검색 결과 면적을 넓힐 FAQ·HowTo 구조화 데이터(JSON-LD)"])),
    "cannibalization": dict(
        label="내부 경쟁", defensive=True,
        detect=lambda ctx: cannibalization(ctx["conn"], ctx["pid"]),
        metrics=lambda r, ctx: {"impressions": r["impressions"],
                                 "position": r["pages"][0]["position"],
                                 "fit": _fit_of(ctx["conn"], ctx["pid"], r["query"])},
        target=lambda r, ctx: r["query"],
        reasoning=lambda r, ctx: (
            f"페이지 {len(r['pages'])}개가 노출 {r['impressions']:,} 분산 — "
            f"{' vs '.join(pg['page'] for pg in r['pages'][:2])} (gsc {ctx['cur']})"),
        play=dict(
            what="같은 검색어에 내 페이지가 둘 이상 걸려 노출을 나눠 갖습니다 — 구글이 어느 쪽을 올릴지 못 정합니다.",
            acts=["아래 표에서 노출·클릭이 가장 큰 페이지를 정본으로 정합니다.",
                  "나머지는 정본으로 301 리다이렉트하거나 canonical 을 정본으로 겁니다.",
                  "합칠 수 없으면 검색 의도를 갈라 제목·H1 을 서로 다르게 씁니다.",
                  "나머지 페이지에서 정본으로 내부 링크를 겁니다."],
            deliver=["어느 페이지를 정본으로 할지와 그 근거(노출·클릭·의도 기준)",
                     "나머지 페이지 처리 계획 — 301 리다이렉트 대상과 canonical 지정",
                     "합칠 경우 병합 후 목차 한 벌 — 새 글을 쓰는 게 아니라 두 글을 합칩니다"])),
    "rank_decay": dict(
        label="순위 하락", defensive=True,
        detect=lambda ctx: rank_decay(ctx["conn"], ctx["pid"]),
        metrics=lambda r, ctx: {"impressions": r["imp"], "position": r["pos"],
                                 "fit": _fit_of(ctx["conn"], ctx["pid"], r["query"])},
        target=lambda r, ctx: r["query"],
        reasoning=lambda r, ctx: (
            f"{r['prev_pos']}위 → {r['pos']}위 (Δ{r['dpos']})·"
            f"클릭 {r['dclk']:+d} — 방어 필요 (gsc {ctx['prev']}→{ctx['cur']})"),
        play=dict(
            what="잡고 있던 순위가 밀렸습니다 — 새로 만드는 것보다 되찾는 쪽이 쌉니다.",
            acts=["그 페이지에 최근 무엇이 바뀌었는지 봅니다(내용 삭제·리다이렉트·템플릿 교체).",
                  "지금 그 자리를 가져간 페이지와 목차를 비교해 빠진 구간을 채웁니다.",
                  "본문을 실제로 갱신합니다 — 날짜만 바꾸는 것은 효과가 없습니다.",
                  "그 페이지로 오던 내부 링크가 끊겼는지 확인합니다."],
            deliver=["되찾기 위해 본문에 채울 구간(H2 목록) — 지금 그 자리를 가져간 페이지와 비교해서",
                     "끊긴 내부 링크를 어디서 다시 걸지"])),
    "pseo_pattern": dict(
        label="pSEO 패턴", defensive=False,
        detect=lambda ctx: pseo_candidates(ctx["conn"], ctx["pid"], ctx["cur"], limit=10),
        metrics=lambda r, ctx: {"impressions": r["imp"], "position": r["pos"],
                                 "fit": _fit_of(ctx["conn"], ctx["pid"], r["query"])},
        target=lambda r, ctx: r["query"],
        reasoning=lambda r, ctx: (
            f"노출 {r['imp']:,}·CTR {r['ctr_pct']}%·{r['pos']}위 — "
            f"pSEO 군집 후보, 군집화는 Claude 판단 (scoring.md 1b) (gsc {ctx['cur']})"),
        play=dict(
            what="같은 꼴의 검색어가 무리로 있습니다 — 한 장씩 쓰는 대신 템플릿으로 찍을 자리입니다.",
            acts=["같은 패턴의 검색어를 모읍니다(도구별·지역별·비교 축).",
                  "템플릿 한 벌 + 실제 데이터로 페이지를 찍습니다 — 빈 껍데기는 색인에서 걸러집니다.",
                  "허브 페이지를 만들어 전부 링크합니다."],
            deliver=["이 패턴의 축과 값 목록(도구·지역·비교 대상)",
                     "템플릿 한 벌의 골격 — 어느 자리에 데이터가 들어가는지",
                     "허브 페이지 구성"])),
    # 분해 수집은 gsc_snapshots 와 수집일이 어긋날 수 있다(분해 수집을 끄면 뒤처진다)
    # — 출처 표기에 cur 을 쓰면 없던 날짜를 말하게 되므로 ctx['bd']로 따로 읽는다.
    "device_gap": dict(
        label="모바일 격차", defensive=False,
        detect=lambda ctx: device_gap(ctx["conn"], ctx["pid"]),
        metrics=lambda r, ctx: {"impressions": r["mobile_imp"], "position": r["mobile_pos"],
                                 "fit": _fit_of(ctx["conn"], ctx["pid"], r["query"])},
        target=lambda r, ctx: r["query"],
        reasoning=lambda r, ctx: (
            f"모바일 {r['mobile_pos']}위 vs 데스크톱 {r['desktop_pos']}위 "
            f"(Δ{r['dpos']})·모바일 노출 {r['mobile_imp']:,}·"
            f"CTR {r['mobile_ctr']}% vs {r['desktop_ctr']}% — "
            f"모바일에서만 밀린다 (gsc {ctx['bd']})"),
        play=dict(
            what="모바일에서만 순위가 낮습니다 — 콘텐츠가 아니라 모바일 화면·속도 문제인 경우가 많습니다.",
            acts=["모바일로 그 페이지를 직접 열어 첫 화면에 답이 있는지 봅니다.",
                  "LCP·CLS 를 확인합니다(이미지 크기 지정, 광고·배너 지연 로드).",
                  "표·코드 블록이 가로로 넘치는지 봅니다.",
                  "전면 팝업(인터스티셜)을 걷어냅니다."],
            deliver=["모바일에서 고칠 것 목록 — 레이아웃·이미지 크기·지연 로드·팝업(코드/설정 수준)",
                     "첫 화면에 무엇이 보여야 하는지"])),
    "index_blocked": dict(
        label="색인 막힘", defensive=False,
        detect=lambda ctx: index_issues(ctx["conn"], ctx["pid"]),
        # 색인 안 된 URL 은 순위가 없다 — position None 을 score() 가 보수적 0.3 으로 본다
        metrics=lambda r, ctx: {"impressions": 0, "position": None,
                                 "fit": _fit_of(ctx["conn"], ctx["pid"], r["url"])},
        target=lambda r, ctx: r["url"],
        reasoning=lambda r, ctx: f"{r['detail']} (gsc 색인 {ctx['ix']})",
        play=dict(
            what="색인되지 않은 URL 입니다 — 색인 전에는 순위 자체가 없습니다.",
            acts=["robots.txt·noindex·canonical 이 이 URL 을 막고 있는지 확인합니다.",
                  "사이트맵에 넣고 Search Console 에서 색인을 요청합니다.",
                  "들어오는 내부 링크가 하나도 없으면(고아 페이지) 최소 한 개를 겁니다.",
                  "중복이면 정본 하나만 남깁니다."],
            deliver=["막고 있는 규칙을 어떻게 고칠지 — robots.txt 줄 또는 meta robots 값과 적용 위치",
                     "색인 요청·확인 절차 순서",
                     "(콘텐츠는 손대지 않습니다 — 색인 전에는 글을 고쳐도 소용이 없습니다)"])),
    "coverage": dict(
        label="미커버", defensive=False,
        detect=_coverage_rows,
        # 이 kind 는 정의상 노출이 0이다("아직 아무 데도 안 뜬다"). 검색량이 없으면
        # 수요 신호도 0이라 점수가 늘 바닥이었다 — "새로 쓸 글"을 고를 때 순서가
        # 뜻이 없었던 이유. volume 이 있으면 score() 가 그걸 수요로 대신 본다.
        metrics=lambda r, ctx: {"impressions": 0, "volume": r["vol"], "position": None,
                                 "fit": _fit_of(ctx["conn"], ctx["pid"], f"cluster:{r['cluster']}")},
        target=lambda r, ctx: f"cluster:{r['cluster']}",
        reasoning=lambda r, ctx: (
            f"활성 키워드 {r['n']}개가 GSC 노출·순위 체크 모두 부재 — 클러스터 '{r['cluster']}'"
            + (f" · 월 검색량 합 {r['vol']:,}" if r["vol"] else "")),
        play=dict(
            what="추적은 하는데 노출도 순위도 없습니다 — 이 주제를 다루는 페이지가 없다는 뜻입니다.",
            acts=["이 클러스터를 다루는 페이지가 실제로 있는지 확인합니다.",
                  "없으면 대표 글 하나부터 만듭니다 — 클러스터 전체를 한 번에 찍지 않습니다.",
                  "있는데 안 걸린다면 제목·본문에 그 검색어가 실제로 등장하는지 봅니다."],
            deliver=["이 주제를 다룰 대표 글 한 장의 제목과 목차 — 클러스터 전체를 한 번에 찍지 않습니다"])),
    # ── 여기까지가 GSC·색인에서 나오는 기회다. 아래는 나머지 네 화면의 재료 —
    #    라벨(KIND_LABEL)과 플레이북(PLAY)은 이미 있었는데 만드는 쪽이 없어서
    #    [AI 인용]·[경쟁 분석]·[백링크]·[사이트 점검] 이 점수도 트리아지도 못 가졌다.
    "ai_citation_gap": dict(
        label="AI 챗봇 인용 없음", defensive=False,
        detect=lambda ctx: ai_gaps(ctx["conn"], ctx["pid"]),
        metrics=lambda r, ctx: {"impressions": 0, "position": None,
                                 "fit": _fit_of(ctx["conn"], ctx["pid"], r["prompt"])},
        target=lambda r, ctx: r["prompt"],
        reasoning=lambda r, ctx: (
            f"엔진 {r['engines']}곳 답변 {r['checks']}건 중 인용 0"
            + (f"·언급만 {r['mentioned']}건" if r["mentioned"] else "")
            + (f" — 대신 {', '.join(r['rivals'])} 가 인용됩니다" if r["rivals"] else "")),
        play=dict(
            what="ChatGPT·Perplexity 같은 챗봇이 이 주제에서 남을 출처로 쓰고 나를 빼놓습니다.",
            acts=["질문 그대로를 H2 로 두고 바로 아래에 2~3문장 정답을 둡니다.",
                  "숫자·출처·갱신 날짜를 본문에 박습니다 — 인용은 검증 가능한 문장을 고릅니다.",
                  "정의·비교표처럼 그대로 인용하기 쉬운 블록을 만듭니다."],
            deliver=["질문 그대로를 쓴 H2 와 그 아래 2~3문장 직답",
                     "인용될 근거 블록(숫자·출처·갱신일이 들어간 표나 목록)",
                     "Article·FAQPage 구조화 데이터(JSON-LD)"])),
    "aio_exposure": dict(
        label="구글 AI 요약 빠짐", defensive=False,
        detect=lambda ctx: aio_gaps(ctx["conn"], ctx["pid"]),
        metrics=lambda r, ctx: {"impressions": 0, "volume": r["volume"] or 0,
                                 "position": r["position"],
                                 "fit": _fit_of(ctx["conn"], ctx["pid"], r["keyword"])},
        target=lambda r, ctx: r["keyword"],
        reasoning=lambda r, ctx: (
            "구글이 AI 요약을 붙이는데 내 링크가 없습니다"
            + (f" · 실측 {r['position']}위" if r["position"] else "")
            + (f" · 월 검색량 {r['volume']:,}" if r["volume"] else "")),
        play=dict(
            what="구글이 이 검색어에 AI 요약을 붙입니다 — 거기 내 링크가 없으면 순위가 그대로여도 클릭이 줄어듭니다.",
            acts=["요약이 답하는 질문에 직답 블록을 만듭니다.",
                  "Article·FAQ 구조화 데이터를 붙입니다."],
            deliver=["구글 AI 요약이 답하는 질문에 대한 직답 블록",
                     "Article·FAQ 구조화 데이터(JSON-LD)"])),
    "content_gap": dict(
        label="콘텐츠 공백", defensive=False,
        detect=lambda ctx: content_gaps(ctx["conn"], ctx["pid"]),
        # missing 은 our_position 이 NULL — score() 가 보수적 0.3 으로 본다(맞다: 아직 없다)
        metrics=lambda r, ctx: {"impressions": 0, "volume": r["volume"] or 0,
                                 "position": r["our_position"],
                                 "fit": _fit_of(ctx["conn"], ctx["pid"], r["keyword"])},
        target=lambda r, ctx: r["keyword"],
        reasoning=lambda r, ctx: (
            f"{r['domain']} 가 {r['position']}위"
            + (f", 나는 {r['our_position']}위 — 밀립니다" if r["our_position"] else ", 나는 아예 없습니다")
            + (f" · 월 검색량 {r['volume']:,}" if r["volume"] else "")),
        play=_CG_PLAY),
    "crawl_issue": dict(
        label="크롤에서 걸림", defensive=True,
        detect=lambda ctx: crawl_gaps(ctx["conn"], ctx["pid"]),
        metrics=lambda r, ctx: {"impressions": 0, "position": None,
                                 "fit": _fit_of(ctx["conn"], ctx["pid"], r["url"])},
        target=lambda r, ctx: r["url"],
        reasoning=lambda r, ctx: f"{r['kind']} — {r['detail'] or '크롤에서 걸렸습니다'}",
        play=dict(
            what="사이트를 전부 돌아 보니 이 주소에서 걸립니다 — 404·리다이렉트 사슬처럼 전수를 봐야 나오는 것들입니다.",
            acts=["그 주소를 직접 열어 지금도 깨져 있는지 확인합니다.",
                  "살릴 수 있으면 살리고, 없어진 것이면 가장 가까운 페이지로 301 합니다.",
                  "그 주소로 걸린 내부 링크를 새 주소로 고칩니다 — 리다이렉트는 임시 방편입니다.",
                  "고친 뒤 다시 크롤해 회차 비교에서 사라지는지 봅니다."],
            deliver=["이 주소를 살릴지 301 할지와 그 대상",
                     "고쳐야 할 내부 링크 목록 — 어느 글의 어느 앵커인지"])),
    # backlink_broken·backlink_prospect 는 backlink_gaps() 한 번이 (broken, prospects)
    # 튜플을 같이 준다 — detect 가 그중 자기 절반만 골라 쓴다(호출은 두 번 하지만
    # 쿼리가 가벼워 굳이 ctx 로 캐싱하지 않는다. 억지로 공유하면 오히려 순서 의존이 생긴다).
    "backlink_broken": dict(
        label="깨진 백링크", defensive=True,
        detect=lambda ctx: backlink_gaps(ctx["conn"], ctx["pid"])[0],
        metrics=lambda r, ctx: {"impressions": 0, "position": None,
                                 "fit": _fit_of(ctx["conn"], ctx["pid"], r["url_to"])},
        target=lambda r, ctx: r["url_to"],
        reasoning=lambda r, ctx: (
            f"{r['domain_from'] or host_of(r['url_from'])} 가 이 주소로 링크를 걸었는데 "
            f"페이지가 없습니다"
            + (f" (지수 {r['rank']})" if r["rank"] else "")
            + " — 이미 번 링크입니다"),
        play=dict(
            what="남이 우리에게 링크를 걸었는데 그 주소에 페이지가 없습니다 — 이미 번 링크라 새로 얻는 것보다 늘 쌉니다.",
            acts=["그 주소에 무엇이 있었는지 확인합니다(옮겼는지, 지웠는지).",
                  "가장 가까운 지금 페이지로 301 을 겁니다 — 홈으로 몰면 값이 거의 사라집니다.",
                  "옮길 곳이 없으면 그 주제로 페이지를 다시 세우는 쪽이 나을 수 있습니다.",
                  "링크를 건 쪽에 새 주소를 알려 링크 자체를 고치게 합니다."],
            deliver=["301 대상 주소와 그 근거", "링크를 건 쪽에 보낼 짧은 안내문 한 벌"])),
    "backlink_prospect": dict(
        label="경쟁사만 받는 링크", defensive=False,
        detect=lambda ctx: backlink_gaps(ctx["conn"], ctx["pid"])[1],
        metrics=lambda r, ctx: {"impressions": 0, "position": None,
                                 "fit": _fit_of(ctx["conn"], ctx["pid"], r["domain"])},
        target=lambda r, ctx: r["domain"],
        reasoning=_reason_backlink_prospect,
        play=dict(
            what="경쟁사는 이 도메인에서 링크를 받는데 나는 못 받습니다 — 이미 우리 주제를 다루는 곳이라 문이 열려 있습니다.",
            acts=["그 도메인이 경쟁사를 **어느 글에서** 링크했는지 찾습니다 — 그게 우리 자리입니다.",
                  "그 글이 다루는 주제에서 우리가 더 잘 답하는 지점을 하나 고릅니다.",
                  "그 지점을 근거로 짧게 연락합니다 — 링크를 달라고 하지 말고 무엇이 빠졌는지를 말합니다.",
                  "받을 만한 페이지가 우리에게 없으면 먼저 그것부터 만듭니다."],
            deliver=["이 도메인에 보낼 연락문 한 벌 — 경쟁사가 링크된 그 글을 짚어서",
                     "그쪽이 링크할 만한 우리 페이지와, 없다면 무엇을 먼저 만들지"])),
}

# ALL_KINDS 의 순서·이름 그대로 명부를 만든다 — 이름은 위 한 곳(ALL_KINDS)에만 적혀
# 있고, 여기서 빠진 이름이 있으면 KeyError 로 즉시 죽는다(조용히 빠지지 않는다).
KINDS: tuple[_Kind, ...] = tuple(_Kind(name, **_KIND_SPECS[name]) for name in ALL_KINDS)
_KIND_BY_NAME = {k.name: k for k in KINDS}
DEFENSIVE_KINDS = frozenset(k.name for k in KINDS if k.defensive)


# striking_distance 밴드별 라벨. "1페이지 상단"은 상태로 읽히면 이미 거기 있다는
# 뜻이 된다 — 가능성으로 적는다: 지금 위치가 아니라 밀면 닿을 자리.
SD_LABEL = {"page1": "1페이지 상단 가능", "page2": "1페이지 진입 가능"}


def kind_label(kind: str, *, band: str | None = None) -> str:
    """기회 종류의 한국어 라벨 — 화면이 그리기만 하도록 여기서 다 정한다.

    band 를 주면(striking_distance 한정) 밴드별 라벨로 갈린다 — 4~10위와 11~20위는
    할 일이 달라 통칭("밀면 오를 검색어")으로는 몇 칸 남았는지가 안 읽힌다. 밴드를
    모르면(다른 kind, 또는 [기록] 화면처럼 kind 단위로만 아는 자리, 옛 박제본)
    통칭으로 물러선다. 모르는 kind 면 원문을 그대로 돌려준다.
    """
    if kind == "striking_distance" and band in SD_LABEL:
        return SD_LABEL[band]
    k = _KIND_BY_NAME.get(kind)
    return k.label if k else (kind or "")


def kind_play(kind: str, *, band: str | None = None, gap_kind: str | None = None) -> dict:
    """이 kind 의 처방(what/acts/deliver) — dashboard.html 의 옛 window.PLAY 산문이 여기로 옮겨왔다.

    striking_distance·content_gap 은 한 kind 가 처방 둘을 갖는다(밴드/갈래로 갈린다).
    band·gap_kind 를 모르면(다른 kind, 대상을 못 찾은 옛 박제본) 예전 JS 삼항의
    기본값과 같은 쪽(page2/missing)으로 물러선다. 모르는 kind 면 빈 dict —
    화면의 playList() 는 빈 처방을 아무것도 안 그리는 것으로 받아들인다.
    """
    k = _KIND_BY_NAME.get(kind)
    if not k:
        return {}
    p = k.play
    if kind == "striking_distance":
        return p.get(band) or p["page2"]
    if kind == "content_gap":
        return p.get(gap_kind) or p["missing"]
    return p


def load(project: str) -> None:
    """서브커맨드 load — KINDS 명부를 순회해 opportunities 에 적재.

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
        except (db.ProjectConfigNotFound, ImportError):   # yaml 이 없어도 적재는 계속한다 (브랜드 필터만 얕아짐)
            pass
    cur, prev, _, _ = snapshot_pair(conn, pid)
    brands = foreign_brands(conn, pid, cfg)
    # 의도 미분류(NULL)만 채움 — Claude/사람 보정은 살아남음
    n_intent = _backfill_intents(conn, pid)
    ctx = {"conn": conn, "pid": pid, "cur": cur, "prev": prev, "brands": brands,
           "bd": _latest(conn, _LATEST_BD, (pid, "device")),
           "ix": _latest(conn, _LATEST_IX, (pid,))}
    rows = []
    for k in KINDS:
        for r in k.detect(ctx):
            rows.append({"kind": k.name, "target": k.target(r, ctx),
                         "score": score(k.name, k.metrics(r, ctx), ptype),
                         "reasoning": k.reasoning(r, ctx)})
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
    import db
    return [dict(r) for r in db.list_opportunities(
        conn, project_id, limit=limit, order="screen", with_id=with_id)]



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

    # 기회를 펼쳤을 때 보여줄 페이지 표 — 노출 많은 순, CTR 은 여기서 계산한다.
    conn.executemany(
        "INSERT INTO gsc_snapshots(project_id, snapshot_date, period_days, query, page,"
        " clicks, impressions, ctr, position) VALUES(1, '2026-08-14', 28, ?, ?, ?, ?, 0.0, ?)",
        [("내 키워드", "https://x.com/a", 3, 300, 12.0),
         ("내 키워드", "https://x.com/b", 1, 100, 18.0)])
    pg = pages_by_query(conn, 1, ["내 키워드", "없는 검색어"])
    assert list(pg) == ["내 키워드"], pg
    assert [r["page"] for r in pg["내 키워드"]] == ["https://x.com/a", "https://x.com/b"], pg
    assert pg["내 키워드"][0]["ctr"] == 1.0, pg["내 키워드"][0]
    assert pages_by_query(conn, 1, ["내 키워드"], top=1)["내 키워드"] == pg["내 키워드"][:1]
    assert pages_by_query(conn, 1, []) == {}

    # 페이지 진단 — 규칙이 무는지 하나씩. 여기가 화면 문구의 정본이다.
    ok_page = {"url": "https://x.com/a", "title": "밀리아 제거 비용과 회복 기간 총정리",
               "meta_description": "밀리아 제거 가격, 시술 방법, 회복 기간을 실제 사례와 "
                                   "함께 정리했습니다. 2026년 기준 서울 평균가 포함.",
               "h1_json": '["밀리아 제거 비용"]', "words": 900,
               "schema_json": '["Article"]', "canonical": "https://x.com/a",
               "robots": "index,follow", "images_no_alt": 0, "internal_links": 8}
    assert page_advice(ok_page, ["밀리아 제거"], domain="x.com") == [],         page_advice(ok_page, ["밀리아 제거"], domain="x.com")
    bad = dict(ok_page, title="가격표", meta_description="", h1_json='["A","B"]',
               words=80, schema_json="[]", robots="noindex", images_no_alt=3,
               internal_links=1)
    tags = [a["tag"] for a in page_advice(bad, ["밀리아 제거"], domain="x.com")]
    assert tags == ["title", "title", "meta description", "H1", "본문",
                    "구조화 데이터", "robots", "이미지", "내부 링크"], tags
    assert page_advice({"error": "HTTP 404 · text/html"})[0]["level"] == "bad"
    assert page_advice(None) == []
    # canonical 이 남을 가리키면 경고가 아니라 결함이다
    other = page_advice(dict(ok_page, canonical="https://competitor.com/a"),
                        ["밀리아 제거"], domain="x.com")
    assert [a["tag"] for a in other] == ["canonical"] and other[0]["level"] == "bad", other

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

    # ── 기준 수집일 고정(화면의 [기준 수집일] 선택) ──
    # 8/10 은 90일치라 28일치 8/01 과 짝이 안 맞는다 — 짝 없음 + mismatch 가 정답이다.
    assert snapshot_pair(conn, 2, "2026-08-10") == ("2026-08-10", None, 90, True)
    # 가장 오래된 날을 고르면 비교할 이전이 없다 — mismatch 가 아니라 그냥 없는 것이다.
    assert snapshot_pair(conn, 2, "2026-08-01") == ("2026-08-01", None, 28, False)
    # 미래를 prev 로 끌어오지 않는다 (부호가 뒤집힌다)
    assert snapshot_pair(conn, 2, "2026-08-20")[1] == "2026-08-01"
    # 수집한 적 없는 날 — 지어내지 않고 빈 짝
    assert snapshot_pair(conn, 2, "2026-07-04") == (None, None, None, False)
    assert [x["date"] for x in snapshot_dates(conn, 2)] == [
        "2026-08-20", "2026-08-10", "2026-08-01"]

    # ── 클릭 변화 분해 ──
    # 항등식이므로 합은 반올림 오차 안에서 Δ클릭과 정확히 같아야 한다. 이게 깨지면
    # 화면이 "원인 합계"라고 부르는 것이 원인이 아니게 된다.
    conn.executemany(
        "INSERT INTO gsc_snapshots(project_id,snapshot_date,period_days,query,clicks,"
        "impressions,ctr,position) VALUES(4,?,28,?,?,?,0.0,?)",
        [("2026-08-01", "a", 100, 1000, 2.0),    # CTR 만 반토막 → CTR 효과 −50
         ("2026-08-15", "a", 50, 1000, 2.0),
         ("2026-08-01", "b", 50, 500, 7.0),      # 노출만 반토막 → 노출 효과 −25
         ("2026-08-15", "b", 25, 250, 7.0),
         ("2026-08-15", "c", 20, 200, 15.0),     # 새로 뜬 것 → new +20
         ("2026-08-01", "d", 10, 100, 25.0),     # 사라진 것 → lost −10
         # 둘 다 움직인 쿼리 — 교호분을 어느 항에 얹느냐로 답이 갈리는 유일한 모양이다.
         # 이 줄이 없으면 어떤 분해를 써도 검사가 통과한다(실제로 그랬다).
         ("2026-08-01", "f", 40, 400, 5.0),      # 10% → 15%, 노출 2배
         ("2026-08-15", "f", 120, 800, 5.0)])    # ie +50, ce +30
    sh = click_shift(conn, 4, "2026-08-15", "2026-08-01", 28)
    assert (sh["d_clicks"], sh["imp_effect"], sh["ctr_effect"], sh["new"], sh["lost"]) ==         (15, 25.0, -20.0, 20, -10), sh
    total = sh["imp_effect"] + sh["ctr_effect"] + sh["new"] + sh["lost"]
    assert abs(total - sh["d_clicks"]) < 1, (total, sh["d_clicks"])
    assert [r["query"] for r in sh["down"]] == ["a", "b", "d"], sh["down"]
    assert [r["query"] for r in sh["up"]] == ["f", "c"], sh["up"]
    assert [r["cause"] for r in sh["down"]] == ["ctr", "imp", "lost"], sh["down"]
    assert sh["up"][0]["cause"] == "imp", sh["up"][0]
    # 표본이 얇은 검색어는 총계에는 들어가되 원인으로 이름 붙지 않는다
    conn.execute("INSERT INTO gsc_snapshots(project_id,snapshot_date,period_days,query,"
                 "clicks,impressions,ctr,position) VALUES(4,'2026-08-15',28,'e',3,5,0.0,2.0)")
    sh2 = click_shift(conn, 4, "2026-08-15", "2026-08-01", 28)
    assert sh2["new"] == 23 and sh2["thin"] == 1, sh2
    assert "e" not in [r["query"] for r in sh2["up"]], sh2["up"]
    assert click_shift(conn, 4, "2026-08-15", None, 28)["d_clicks"] == 0

    # 순위 구간 이동 — 개별 변동이 아니라 인원수의 이동이다
    assert _band_of(3.4) == "1–3위" and _band_of(3.6) == "4–10위"
    assert _band_of(None) == "21위+" and _band_of(999) == "21위+"
    bands = {b["label"]: b["d"] for b in rank_bands(conn, 4, "2026-08-15", "2026-08-01", 28)}
    assert bands == {"1–3위": 0, "4–10위": 0, "11–20위": 1, "21위+": -1}, bands
    assert rank_bands(conn, 4, None, None, 28) == []

    # ── 신규 판정 함수들 ──
    assert RANK_NOISE == 3
    assert rank_delta(None, 5) == {"delta": None, "flat": False}
    assert rank_delta(5, None) == {"delta": None, "flat": False}
    assert rank_delta(10, 10) == {"delta": 0, "flat": True}
    assert rank_delta(10, 8) == {"delta": 2, "flat": True}           # +2 < RANK_NOISE (보합)
    assert rank_delta(8, 10) == {"delta": -2, "flat": True}          # -2 (보합)
    assert rank_delta(10, 7) == {"delta": 3, "flat": False}          # +3 == RANK_NOISE (움직임)
    assert rank_delta(7, 10) == {"delta": -3, "flat": False}         # -3 == -RANK_NOISE (움직임)
    assert is_defensive("rank_decay") is True
    assert is_defensive("cannibalization") is True
    assert is_defensive("striking_distance") is False
    assert is_defensive("") is False
    assert is_defensive(None) is False

    # ── KINDS 명부 — 정본(ALL_KINDS)·산출물(load 가 도는 명부)·라벨이 한 벌인지.
    # 일부러 하나를 빼거나 더하면 여기서 바로 터져야 한다(조용히 안 맞는 채로 못 있는다).
    assert set(_KIND_SPECS) == set(ALL_KINDS),                         f"_KIND_SPECS 와 ALL_KINDS 가 어긋났다: {set(_KIND_SPECS) ^ set(ALL_KINDS)}"
    assert tuple(k.name for k in KINDS) == ALL_KINDS                   # 순서까지 같다
    assert DEFENSIVE_KINDS == {"rank_decay", "cannibalization",
                               "backlink_broken", "crawl_issue"}, DEFENSIVE_KINDS
    assert all(k.label for k in KINDS), "라벨 없는 kind 가 있다"
    assert len(set(k.label for k in KINDS)) == len(KINDS), "라벨이 겹치는 kind 가 있다"
    assert all(k.play for k in KINDS), "처방(play) 없는 kind 가 있다"
    assert kind_label("striking_distance") == "밀면 오를 검색어"
    assert kind_label("crawl_issue") == "크롤에서 걸림"
    assert kind_label("없는kind") == "없는kind"      # 모르면 원문 그대로 — 화면의 옛 폴백과 같다
    assert kind_label("") == ""

    # ── 밴드·갈래로 갈리는 라벨·처방 — striking_distance(band)·content_gap(gap_kind).
    # 모르면(band/gap_kind 없음) 예전 JS 삼항의 기본값과 같은 쪽(page2/missing)으로
    # 물러선다 — 여기가 v1.38.2 버그(6.4위에 "11~20위"가 붙음)가 났던 자리다.
    assert kind_label("striking_distance", band="page1") == "1페이지 상단 가능"
    assert kind_label("striking_distance", band="page2") == "1페이지 진입 가능"
    assert kind_label("striking_distance", band=None) == "밀면 오를 검색어"
    assert kind_label("striking_distance", band="???") == "밀면 오를 검색어"
    assert kind_label("ctr_gap", band="page1") == "CTR 미달"          # band 는 striking_distance 전용
    assert kind_play("striking_distance", band="page1")["what"].startswith("이미 1페이지 안입니다")
    assert kind_play("striking_distance", band="page2")["what"].startswith("1페이지 진입까지")
    assert kind_play("striking_distance")["what"].startswith("1페이지 진입까지")     # 모르면 page2
    assert kind_play("content_gap", gap_kind="weak")["what"].startswith("경쟁 도메인이 나보다 위에")
    assert kind_play("content_gap", gap_kind="missing")["what"].startswith("경쟁 도메인은 잡고 있는데")
    assert kind_play("content_gap")["what"].startswith("경쟁 도메인은 잡고 있는데")   # 모르면 missing
    assert kind_play("ctr_gap")["acts"], "정적 kind 의 처방이 비었다"
    assert kind_play("없는kind") == {}
    assert set(INDEX_BUCKETS) == {"robots_blocked", "fetch_error",
                                  "canonical_mismatch", "not_indexed"}, INDEX_BUCKETS
    assert gap_to_page1(10.0) == 0.0                                 # 1페이지 안이면 0 클램프
    assert gap_to_page1(9.9) == 0.0
    assert gap_to_page1(1.0) == 0.0
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

    # ── 페이지 축: 성과·무노출·내부링크 굶음 (프로젝트 8·9) ──
    # 열쇠가 스킴·www·끝 슬래시를 벗기는지. 이게 안 맞으면 크롤과 GSC 가 서로를
    # 영영 못 알아보고, 모든 페이지가 '아무도 안 온다'로 나온다.
    assert url_key("https://www.x.com/a/") == url_key("http://x.com/a") == "x.com/a"
    assert url_key("https://x.com") == "x.com/"
    assert url_key("https://x.com/a?p=2") != url_key("https://x.com/a")

    assert page_performance(conn, 8) == [] and dead_pages(conn, 8) == []
    conn.executemany(
        "INSERT INTO gsc_snapshots(project_id, snapshot_date, period_days, query, page,"
        " clicks, impressions, ctr, position) VALUES(8, ?, 28, ?, ?, ?, ?, 0.0, ?)",
        [("2026-08-01", "q1", "https://www.x.com/dying/", 40, 1000, 5.0),
         ("2026-08-01", "q2", "https://www.x.com/dying/", 10, 200, 8.0),
         ("2026-08-01", "q1", "https://www.x.com/rising", 5, 100, 9.0),
         ("2026-08-08", "q1", "https://www.x.com/dying/", 12, 900, 7.0),
         ("2026-08-08", "q2", "https://www.x.com/dying/", 3, 180, 9.0),
         ("2026-08-08", "q1", "https://www.x.com/rising", 20, 300, 4.0)])
    # 사이에 90일치가 끼어 있어도 28일치끼리만 뺀다 — 섞으면 Δ가 전부 거짓이 된다
    conn.execute("INSERT INTO gsc_snapshots(project_id,snapshot_date,period_days,query,page,"
                 "clicks,impressions,ctr,position)"
                 " VALUES(8,'2026-08-05',90,'q1','https://www.x.com/dying/',999,9999,0.0,1.0)")
    pp = page_performance(conn, 8)
    assert [r["page"] for r in pp] == ["https://www.x.com/dying/",
                                       "https://www.x.com/rising"], pp   # 잃은 쪽이 위
    assert (pp[0]["dclk"], pp[1]["dclk"]) == (-35, 15), pp
    assert pp[0]["queries"] == 2 and pp[0]["impressions"] == 1080, pp[0]
    assert pp[0]["ctr"] == 1.39, pp[0]                                   # 저장값이 아닌 재계산

    conn.execute("INSERT INTO crawl_runs(id, project_id, finished_at, seed, pages)"
                 " VALUES(80, 8, '2026-08-09', 'sitemap', 4)")
    conn.executemany(
        "INSERT INTO crawl_pages(run_id, url, status, depth, words, links_in, title)"
        " VALUES(80, ?, ?, ?, ?, ?, ?)",
        # 크롤 URL 은 스킴·www·끝 슬래시가 GSC 와 일부러 어긋나 있다 — 같은 페이지다
        [("http://x.com/dying", 200, 1, 800, 9, "죽는 중"),
         ("https://www.x.com/rising/", 200, 2, 600, 1, "뜨는 중"),
         ("https://www.x.com/nobody", 200, 3, 400, 2, "아무도 안 옴"),
         ("https://www.x.com/broken", 404, 3, 0, 0, "깨짐")])
    dp = dead_pages(conn, 8)
    assert [r["url"] for r in dp] == ["https://www.x.com/nobody"], dp    # 200 이고 노출 0 만
    sp = starved_pages(conn, 8)
    assert [r["page"] for r in sp] == ["https://www.x.com/rising"], sp   # links_in 1 < 3
    assert (sp[0]["crawl_url"], sp[0]["rank"]) == ("https://www.x.com/rising/", 2), sp[0]

    # page 가 NULL 인 구버전 스냅샷 — 크롤이 있어도 전 페이지를 '노출 0' 이라 부르지 않는다
    conn.execute("INSERT INTO gsc_snapshots(project_id,snapshot_date,period_days,query,page,"
                 "clicks,impressions,ctr,position) VALUES(9,'2026-08-08',28,'q',NULL,1,10,0.0,5.0)")
    conn.execute("INSERT INTO crawl_runs(id, project_id, finished_at, seed, pages)"
                 " VALUES(90, 9, '2026-08-09', 'home', 1)")
    conn.execute("INSERT INTO crawl_pages(run_id,url,status,depth,words,links_in,title)"
                 " VALUES(90,'https://y.com/a',200,0,500,0,'a')")
    assert page_performance(conn, 9) == [] and dead_pages(conn, 9) == []
    assert starved_pages(conn, 9) == []

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

    # ── 검색 × AI 교차. 프로젝트 1 의 스냅샷이 왼쪽 축이다:
    #    '1페이지 키워드' 3.0위 · 'ecrett' 8.6위가 상위, '내 키워드' 14.0위는 밖.
    assert _xai_tokens("AI 툴 고르는 법") == {"ai", "고르는"}, _xai_tokens("AI 툴 고르는 법")
    ai_rows = [
        {"prompt": "1페이지 키워드 고르는 법", "category": "추천", "cited": 0, "checks": 6,
         "miss_domains": '["https://www.ecrett.com/a", "b.com"]'},
        {"prompt": "키워드 뭐가 좋아", "category": "추천", "cited": 0, "checks": 6,
         "miss_domains": None},                     # 한 낱말만 겹친다 — 짝을 안 짓는다
        {"prompt": "ecrett 어때", "category": "브랜드", "cited": 0, "checks": 6,
         "miss_domains": None},                     # 검색어가 한 낱말이라 임계 미달
        {"prompt": "1페이지 키워드 정리법", "category": "추천", "cited": 2, "checks": 6,
         "miss_domains": None},                     # 이미 인용된다 — 여기 올 자리가 아니다
    ]
    xs = search_wins_ai_loses(conn, 1, ai_rows)
    assert xs["top_queries"] == 2, xs
    assert [r["prompt"] for r in xs["rows"]] == ["1페이지 키워드 고르는 법"], xs
    assert xs["rows"][0]["query"] == "1페이지 키워드" and xs["rows"][0]["pos"] == 3.0, xs
    assert xs["rows"][0]["rivals"] == ["ecrett.com", "b.com"], xs["rows"][0]
    assert search_wins_ai_loses(conn, 999, ai_rows) == {"rows": [], "top_queries": 0}

    # 경쟁사(ecrett.com)는 등록돼 있고, keyword_gap 이 "검색은 우리가 위"를 말한다.
    conn.executemany(
        "INSERT INTO keyword_gap(project_id, checked_date, keyword, domain, position,"
        " our_position, volume, kind) VALUES(1, '2026-08-20', ?, 'ecrett.com', ?, ?, ?, ?)",
        [("1페이지 키워드", 7, 3, 400, "shared"),      # 우리가 위 — 이 자리를 센다
         ("둘 다 1페이지", 2, 9, 300, "shared"),        # 둘 다 상위인데 우리가 아래
         ("2페이지 싸움", 30, 15, 200, "shared"),       # 저쪽보단 위지만 우리도 밖이다
         ("내 키워드", 2, 12, 300, "weak")])           # 우리가 아래 — 안 센다
    xo = ai_outranked(conn, 1, [{"domain": "ecrett.com", "n": 4},
                                {"domain": "www.other.com", "n": 9}])
    assert (xo["competitors"], xo["cited"], xo["gap_date"]) == (1, 1, "2026-08-20"), xo
    assert [r["domain"] for r in xo["rows"]] == ["ecrett.com"], xo
    assert xo["rows"][0]["cites"] == 4 and xo["rows"][0]["won"] == 1, xo["rows"][0]
    assert xo["rows"][0]["top"][0]["keyword"] == "1페이지 키워드", xo["rows"][0]
    # 등록 안 된 도메인은 아무리 인용돼도 이 표에 없다 — competitors 가 정본이다
    assert ai_outranked(conn, 1, [{"domain": "other.com", "n": 9}])["cited"] == 0

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
        try:
            import db
            load(sys.argv[2])
        except (db.ProjectNotFound, db.ProjectConfigNotFound) as e:
            sys.exit(str(e))
    else:
        _selfcheck()
