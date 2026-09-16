#!/usr/bin/env python3
"""판정 규칙 한 곳 — striking distance·움직임·남의 브랜드·pSEO 후보·기회 목록.

여기가 생기기 전에는 같은 규칙이 SQL(dashboard·collect_gsc)·Python·템플릿 JS·
references/scoring.md 산문 네 군데에 흩어져 있었고 값이 서로 어긋나 있었다
(움직임 임계 0.4 vs 0.5). 임계값을 바꾸려면 이 파일만 고친다.

references/scoring.md 는 이제 명세이고, 실행은 전부 여기서 한다.

self-check:  python scoring.py
기회 적재:   python scoring.py load <project>   (striking·ctr_gap·… 계산 → opportunities upsert)
"""
import datetime
import json
import math
import re
import sqlite3
import sys
from collections import namedtuple
from urllib import robotparser
from urllib.parse import urlsplit

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
# page_advice 의 구조 신호 — 그림 위주(이미지 N개 이상, 이미지당 본문 단어가 이보다 적음),
# 링크 과다(내보내는 내부 링크 N개 이상, 링크당 본문 단어가 이보다 적음).
IMAGE_HEAVY_MIN, IMAGE_HEAVY_WORDS = 8, 80
LINKS_HEAVY_MIN, LINKS_HEAVY_WORDS = 30, 20
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

# 순위 구간·갈래(브랜드·의도·클러스터) 표를 펼쳤을 때 딸려 보낼 검색어 목록 상한.
# 화면은 펼침 UI라 클릭 상위 몇 개면 충분하고, 다 실으면 페이로드가 무거워진다.
DETAIL_TOP_N = 15

# ── 챗봇 인용(ai_citation_gap) ──
# 답은 비결정적이다 — 한 번 빠진 것도 한 번 걸린 것도 사실이 아니라 표본이다. 그래서
# "인용 0회"가 아니라 **비율과 표본 수**로 가른다(ai-seo 점검표: "cited 3/5, n=5").
# 0회로만 가르던 동안 6번 중 1번 걸리는 질문 — 엔진이 이미 우리를 찾을 줄 알아서 가장
# 싸게 끌어올릴 자리 — 이 기회 목록에서 통째로 빠졌다.
AI_GAP_MAX_RATE = 1 / 3     # 인용률이 이 이하면 기회로 올린다(ai_is_gap)
AI_MIN_SAMPLES = 3          # 답변이 이보다 적으면 "표본 부족" — 올리되 그렇다고 말하고 점수를
                            # 깎는다. 표본 수를 늘리는 건 config ai_samples 몫이다(유료 호출
                            # 비용이 늘어나는 결정이라 여기서 대신 하지 않는다)
AI_THIN_MULT = 0.6          # 표본 부족 질문의 점수 승수(score)
AI_PRESENCE_SHARE = 0.5     # 대신 인용된 횟수 중 제3자 플랫폼 몫이 이보다 크면 처방이
                            # "내 페이지 고치기"가 아니라 "그 플랫폼에 등장하기"다
AI_EXCERPT_CHARS = 280      # 엔진별 답변 발췌 — 요청문·화면이 엔진마다 한 토막씩 보인다
AI_RIVALS_TOP = 8           # 질문 하나에 싣는 대신 인용된 도메인 수
# 가시성 사다리(인용 → 이름 나옴 → 추천 목록)를 요청문에 싣는 질문 갈래. 갈래 이름의
# 정본은 gen_prompts.CATEGORIES 다 — 여기는 그중 "추천·비교" 두 개를 고를 뿐이고,
# 자체점검이 부분집합인지 대조한다(이름이 바뀌면 거기서 죽는다).
AI_LADDER_CATEGORIES = ("추천", "비교")

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

# 이 리포가 만드는 기회 종류 한 벌 — 이름·순서의 정본은 여기다. 라벨·처방은 화면이
# 아니라 dashboard.gather() 가 KINDS 명부에서 실어 보낸다(짝이 어긋나면 라벨 없는
# 영문 kind 가 화면에 그대로 떴었다). 읽는 쪽은 전부 `scoring.ALL_KINDS` 를 import
# 한다 — brief·db·검사 어디도 이 파일 원문을 긁지 않으므로 표 모양은 자유다.
# 각 kind 의 나머지(검출기·라벨·방어 여부 등)는 아래 KINDS 명부(_KIND_SPECS)가 이
# 순서를 그대로 따라가며 채운다 — DEFENSIVE_KINDS 도 거기서 파생된다
# (is_defensive() 는 그 결과를 읽는다).
ALL_KINDS = ("striking_distance", "ctr_gap", "cannibalization", "intent_split",
             "rank_decay", "pseo_pattern", "device_gap", "index_blocked", "coverage",
             "ai_citation_gap", "aio_exposure", "content_gap",
             "crawl_issue", "backlink_broken", "backlink_prospect",
             "ai_bot_blocked")
# 심사(검색어 판정)를 거치는 종류 — 대상이 검색어·질문문인 것. 나머지(coverage 의
# cluster:, index_blocked·crawl_issue 의 URL, backlink_* 의 도메인)는 판정 없이
# 기회 목록에 바로 선다. 조회(db)·화면·검사가 이 한 벌을 가리킨다.
KEYWORD_KINDS = ("striking_distance", "ctr_gap", "cannibalization", "rank_decay",
                 "pseo_pattern", "device_gap", "ai_citation_gap", "aio_exposure",
                 "content_gap")



def norm(s: str) -> str:
    """비교용 정규화 — 소문자 + 글자·숫자만(공백·기호 제거). 'Future Tools' 와
    'futuretools.io' 를 같게 본다. 한글·영문만 남기던 시절엔 중국어·일본어 검색어가
    통째로 빈 키가 되어 심사(verdicts)에서 한 행으로 뭉치고 판정도 안 걸렸다 —
    글자면 어느 문자든 남긴다(정규식 문자 클래스 w 에서 밑줄만 뺀다)."""
    return re.sub(r"[\W_]+", "", (s or "").lower())


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


# 나라 도메인 아래 2단 접미사의 둘째 칸 — 'co.kr'·'or.kr'·'com.au'. 이 칸 앞이 이름이다.
_SLD = frozenset({"co", "or", "ac", "go", "ne", "re", "pe", "com", "net", "org", "gov", "edu"})


def _stem(domain: str) -> str:
    """도메인에서 브랜드 이름 후보 — 'ecrett.com' -> 'ecrett'.

    등록 도메인의 이름 칸을 쓴다. 예전엔 호스트의 **첫** 칸을 써서 하위 도메인이
    이름이 됐다 — 'gangnam.museclinic.co.kr' 가 'gangnam', 'blog.naver.com' 이 'blog'
    였고, theotherskin 의 'gangnam dermatology clinic' 검색어가 남의 브랜드로 걸려
    기회에서 빠졌다(2026-09-11 호스팅 실데이터).
    """
    labels = [x for x in host_of(domain).split(".") if x]
    if len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in _SLD:
        core = labels[-3]
    elif len(labels) >= 2:
        core = labels[-2]
    else:
        core = labels[0] if labels else ""
    return norm(core)


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


# 순위 수집이 경쟁사를 스스로 붙이는 문턱 — serp_rivals 가 쓴다.
# 예전 규칙은 "검색어 3개 이상에서 상위 10위"였다. 추적 검색어가 100개면 3% 라서
# 검색결과에 늘 서는 플랫폼·포털이 전부 넘었고(2026-09 호스팅: 77·54·160개, 앞자리가
# m.blog.naver·youtube·reddit·play.google), 그 표를 갭 분석(유료)·트래픽 몫·백링크
# 교집합·남의 브랜드 거름망이 그대로 썼다.
SERP_RIVAL_MIN_SHARE = 0.10     # 이번에 잰 검색어의 10% 이상에서 상위에 섰다
SERP_RIVAL_MIN_HITS = 3         # 잰 검색어가 적을 때의 바닥
SERP_RIVAL_MAX = 10             # 한 바퀴에 붙이는 수 — 많이 겹친 순


def serp_rivals(hits: dict, checked: int, own: str, platforms=None) -> list[str]:
    """순위 수집 한 바퀴가 경쟁사로 붙일 도메인. hits = {도메인: 상위에 선 검색어 수}.

    우리 자신·제3자 플랫폼(config.yaml third_party_platforms)은 빼고, 문턱을 넘은 것 중
    많이 겹친 순으로 SERP_RIVAL_MAX 개까지.
    """
    plats = third_party_platforms() if platforms is None else platforms
    need = max(SERP_RIVAL_MIN_HITS, math.ceil(checked * SERP_RIVAL_MIN_SHARE))
    ok = [(d, n) for d, n in hits.items()
          if d and n >= need and not owns(d, own) and not is_third_party(d, plats)]
    ok.sort(key=lambda x: (-x[1], x[0]))
    return [d for d, _ in ok[:SERP_RIVAL_MAX]]


def rivals(conn: sqlite3.Connection, project_id: int, own: str = "",
           platforms=None) -> tuple[list[str], list[str]]:
    """경쟁사 표를 **경쟁사로** 읽는다 → (쓸 것, 뺀 플랫폼). 돈을 쓰는 단계(갭 분석·
    백링크 교집합)는 이걸로 읽는다.

    사람이 고른 것(manual)이 먼저, 그다음 들어온 순. 우리 자신과 제3자 플랫폼은 뺀다 —
    표에 옛 규칙이 넣은 행이 남아 있어도 대상이 되지 않게(쓰는 쪽도 이미 거른다).
    """
    plats = third_party_platforms() if platforms is None else platforms
    seen, keep, dropped = set(), [], []
    for r in conn.execute(
            "SELECT domain FROM competitors WHERE project_id=?"
            " ORDER BY (source = 'manual') DESC, id", (project_id,)):
        d = host_of(str(r[0] or ""))
        if not d or d in seen or (own and owns(d, own)):
            continue
        seen.add(d)
        (dropped if is_third_party(d, plats) else keep).append(d)
    return keep, dropped


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


# 답변 본문의 "목록 줄" — 번호(1. 2) ① (3))·글머리(- * • ·)·번호 붙은 제목(### 1.)·
# 표의 행(| … |). 추천·비교 답은 후보를 거의 이 꼴로 늘어놓는다. 글머리 기호 뒤에는
# 공백을 요구한다 — 문단 첫머리의 **굵은 글씨**를 목록으로 세지 않으려고.
_LIST_LINE = re.compile(r"^\s*(?:[-*+•·▪]\s+|\d{1,2}[.)]\s+|\(\d{1,2}\)\s*|[①-⑳]\s*"
                        r"|#{1,6}\s*\d{1,2}[.)]\s*|\|)")


def recommended_in(content: str, aliases: list[str]) -> int:
    """브랜드가 답변의 **목록 줄 안에** 나오나 — 가시성 사다리의 마지막 칸(추천됨).

    휴리스틱이다: 답이 "추천 목록"을 번호·글머리·표로 적는다는 관찰에 기댄다. 목록
    밖 문단에서 이름만 지나가면 "이름 나옴"이지 추천이 아니고, 목록 안이라도 "피할
    도구" 목록일 수 있다 — 그 뜻까지는 가르지 않는다. 요청문도 휴리스틱이라고 적는다.
    """
    names = [a.lower() for a in aliases if a]
    for line in (content or "").splitlines():
        if _LIST_LINE.match(line) and any(n in line.lower() for n in names):
            return 1
    return 0


def judge(content: str, citation_urls: list[str] | None, aliases: list[str],
          own_domain: str) -> tuple[int, int, list[str], int]:
    """AI 답변 하나를 (언급됐나, 인용됐나, 대신 인용된 도메인들, 추천 목록에 들었나) 로 판정.

    인용 판정은 여기 하나뿐이어야 한다 — 부르는 쪽이 collect_ai 내부를
    가로질러 import 하던 것을 여기로 옮겼다. 인용 URL이 없으면 본문의 맨 URL을
    줍는 fallback까지 여기서 한다 — 호출부 두 곳이 각자 하던 일이다.

    cited·mentioned 둘만으로는 사다리(검색됨 → 인용됨 → 이름 나옴 → 추천됨)의 끝을
    못 본다. 추천 판정은 recommended_in 의 휴리스틱이다.
    """
    text = (content or "").lower()
    mentioned = int(any(a.lower() in text for a in aliases if a))
    urls = citation_urls or URL_RE.findall(content or "")
    domains = sorted({d for d in (host_of(u) for u in urls) if d})
    cited = int(any(owns(d, own_domain) for d in domains))
    others = [d for d in domains if not owns(d, own_domain)]
    return mentioned, cited, others, recommended_in(content, aliases)


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
# 검색결과가 자르는 것은 글자 수가 아니라 픽셀 폭이라, 한글·가나·한자는 로마자의 약
# 두 배를 먹는다. 한 벌로 두면 CJK 사이트는 멀쩡한 제목마다 "너무 길다"는 경고를 받는다.
TITLE_MAX, TITLE_MAX_KO = 60, 30
TITLE_MIN, TITLE_MIN_KO = 15, 8
DESC_MIN, DESC_MIN_KO = 70, 40
DESC_MAX, DESC_MAX_KO = 160, 80
THIN_WORDS = 300        # 이보다 얇으면 "이 주제를 다뤘다"고 보기 어렵다
_CJK = re.compile(r"[가-힣぀-ヿ一-鿿]")


def _wide(text: str) -> bool:
    """폭이 넓은 글자(한글·가나·한자)가 섞였나 — 길이 임계값을 어느 쪽으로 볼지 가른다."""
    return bool(_CJK.search(text or ""))


# Core Web Vitals 의 "좋음" 경계 — 구글이 공개한 값 그대로다. 여기 한 벌로 두고
# 수집기·요청문·화면이 갖다 쓴다(collect_vitals 가 다시 세면 두 벌이 된다).
LCP_GOOD_MS = 2500
INP_GOOD_MS = 200
CLS_GOOD = 0.1


def _sec(ms) -> str:
    return f"{ms / 1000:.1f}초" if isinstance(ms, (int, float)) else "—"


def vitals_advice(rows) -> list[dict]:
    """속도 행들 → "무엇을 고쳐야 하나" 한 줄. 기준 안이면 빈 리스트다.

    현장(CrUX) 값을 먼저 본다 — 검색이 보는 것이 그것이다. 현장이 없으면 실험실
    값으로 말하되 **어느 값인지 밝힌다**: 둘을 뭉치면 "실험실에서 느렸다" 를
    "사용자가 느리다" 로 부풀리게 된다.

    origin_fallback 인 행은 이 페이지의 값이 아니라 사이트 전체 값이라, 그렇게
    말한다. 이걸 안 가르면 멀쩡한 페이지에 없는 문제를 만들어 낸다.
    """
    out = []
    for r in rows or []:
        if not r or r.get("error"):
            continue
        dev = "모바일" if r.get("strategy") == "mobile" else "데스크톱"
        field = r.get("field_lcp_ms") is not None or r.get("field_inp_ms") is not None             or r.get("field_cls") is not None
        src = ("실제 사용자 28일치" if field else "실험실 1회 측정")
        if field and r.get("origin_fallback"):
            src = "사이트 전체(이 페이지만의 실제 사용자 값은 표본이 모자랍니다)"
        lcp = r.get("field_lcp_ms") if field else r.get("lab_lcp_ms")
        inp = r.get("field_inp_ms") if field else None
        cls = r.get("field_cls") if field else r.get("lab_cls")
        bad = []
        if isinstance(lcp, (int, float)) and lcp > LCP_GOOD_MS:
            bad.append(f"LCP {_sec(lcp)} (기준 {_sec(LCP_GOOD_MS)})")
        if isinstance(inp, (int, float)) and inp > INP_GOOD_MS:
            bad.append(f"INP {inp}ms (기준 {INP_GOOD_MS}ms)")
        if isinstance(cls, (int, float)) and cls > CLS_GOOD:
            bad.append(f"CLS {cls} (기준 {CLS_GOOD})")
        if not bad:
            continue
        fix = {"LCP": "가장 큰 이미지·글자 블록이 늦게 뜹니다. 그 자원을 먼저 받게 하고"
                      "(preload·크기 지정), 서버 응답과 이미지 용량을 줄이세요.",
               "INP": "누른 뒤 화면이 늦게 반응합니다. 메인 스레드를 오래 잡는 스크립트를"
                      " 쪼개거나 뒤로 미루세요.",
               "CLS": "그리는 도중 화면이 밀립니다. 이미지·광고·삽입물에 width/height 를"
                      " 지정하고 나중에 끼어드는 요소의 자리를 미리 잡으세요."}
        first = bad[0].split()[0]
        out.append({"tag": "속도", "level": "bad" if len(bad) > 1 else "warn",
                    "now": f"{dev} " + " · ".join(bad) + f" — {src}",
                    "fix": fix.get(first, "")})
    return out


# 목록의 정본은 config.yaml 의 ai_bots 다 — 벤더가 봇을 새로 내는 일은 코드
# 변경이 아니라 데이터 변경이라서. 읽기 실패는 수집을 막지 않는다(아래 폴백).
#
# 봇의 **용도**가 판정의 전부다. 학습 전용 봇(GPTBot·ClaudeBot·CCBot…)을 막는 것은
# 인용과 무관하고, 오히려 권장되는 중간 지점이다 — 학습은 거부하고 검색·인용은
# 받는다. 예전에는 막힌 봇을 전부 "인용 불가" 로 올려서, GPTBot 만 막은 흔한
# 사이트에 ChatGPT 인용이 불가능하다고 오진했다(ChatGPT 검색은 OAI-SearchBot 이다).
AI_BOT_PURPOSE = {"search": "검색·인용 색인", "user": "사용자 요청 페치",
                  "training": "학습"}
# 막히면 인용이 끊기는 용도 — 이 둘만 기회가 된다. training·모름(None)은 아니다.
AI_BOT_CITING = ("search", "user")
_AI_BOTS_FALLBACK = (
    {"ua": "GPTBot", "vendor": "OpenAI", "purpose": "training", "engine": "OpenAI 모델 학습"},
    {"ua": "OAI-SearchBot", "vendor": "OpenAI", "purpose": "search", "engine": "ChatGPT 검색"},
    {"ua": "ClaudeBot", "vendor": "Anthropic", "purpose": "training", "engine": "Claude 모델 학습"},
    {"ua": "PerplexityBot", "vendor": "Perplexity", "purpose": "search", "engine": "Perplexity"},
    {"ua": "Google-Extended", "vendor": "Google", "purpose": "training", "engine": "Gemini 모델 학습"},
)


def _ai_bot_entry(x) -> dict | None:
    """config 한 줄 → {ua, vendor, purpose, engine}.

    옛 꼴(UA 문자열만)도 읽는다 — 다만 용도는 None(모름)이다. 모르는 것을 "검색
    봇" 으로 짐작하면 예전 오진이 그대로 돌아오고, "학습 봇" 으로 짐작하면 진짜
    차단을 숨긴다. 모름은 기회로도, 무해로도 올리지 않고 표에 "모름" 으로 적는다.
    """
    if isinstance(x, dict):
        ua = str(x.get("ua") or "").strip()
        purpose = str(x.get("purpose") or "").strip().lower() or None
        if purpose not in AI_BOT_PURPOSE:
            purpose = None
        vendor = str(x.get("vendor") or "").strip() or None
        engine = str(x.get("engine") or "").strip() or None
    else:
        ua, purpose, vendor, engine = str(x or "").strip(), None, None, None
    return {"ua": ua, "vendor": vendor, "purpose": purpose, "engine": engine} if ua else None


def ai_bots() -> tuple[dict, ...]:
    try:
        import collector
        got = collector.config().get("ai_bots")
    except Exception:
        got = None
    if not isinstance(got, list) or not got:
        return _AI_BOTS_FALLBACK
    return tuple(e for e in map(_ai_bot_entry, got) if e)


def ai_bot_purpose(ua: str) -> str | None:
    """UA 이름 → 용도. 목록에 없거나 옛 꼴이면 None(모름)."""
    key = str(ua or "").strip().lower()
    return next((b["purpose"] for b in ai_bots() if b["ua"].lower() == key), None)


def ai_bot_status(robots_txt: str, home: str = "") -> list[dict]:
    """robots.txt 원문 → 봇마다 {bot, vendor, purpose, engine, rule}. rule=None 이면 허용.

    막힌 것만 내지 않고 전부 낸다 — 화면·요청문의 표도, 기회 판정(ai_bot_blocks)도
    이 한 벌을 읽는다. 원문이 비면 [] 다: 못 읽은 robots.txt 를 "전부 허용" 으로
    읽지 않는다(안 봤다 ≠ 봤고 안 막는다).
    """
    txt = robots_txt or ""
    if not txt.strip():
        return []
    url = home if home.startswith("http") else f"https://{home or 'example.com'}/"
    return [{"bot": b["ua"], "vendor": b["vendor"], "purpose": b["purpose"],
             "engine": b["engine"], "rule": robots_blocks(txt, url, agent=b["ua"])}
            for b in ai_bots()]


# robots.txt 의 `User-agent: *` 한 줄이 AI 검색 크롤러를 통째로 막으면 원인은 그 줄
# 하나다. 봇마다 기회를 세우면 줄 하나를 고칠 일이 기회 목록 일곱 줄로 불어난다 —
# 그래서 그 경우엔 이 대상 하나로 묶는다. 대상 문자열이 곧 고칠 자리다.
AI_BOT_WILDCARD = "User-agent: *"
# 이름으로 된 묶음에 절대 안 걸리는 가짜 UA — "와일드카드 묶음이 막나"를 물을 때 쓴다.
_WILDCARD_PROBE = "SeoMinerWildcardProbe"


def _named_in_robots(robots_txt: str, ua: str) -> bool:
    """이 봇 이름으로 된 User-agent 묶음이 있나. 묶음 고르는 규칙은 urllib.robotparser
    와 같다(UA 줄의 값이 봇 이름 안에 부분 문자열로 들어가면 그 묶음) — 판정을 두 벌로
    만들면 "와일드카드로 막혔다"와 파서의 실제 판정이 어긋난다."""
    name = ua.split("/")[0].lower()
    for line in (robots_txt or "").splitlines():
        key, _, val = line.partition(":")
        if key.strip().lower() == "user-agent":
            v = val.split("#")[0].strip().lower()
            if v and v != "*" and v in name:
                return True
    return False


def ai_bot_blocks(conn: sqlite3.Connection, project_id: int, *,
                  home: str = "") -> list[dict]:
    """robots.txt 로 막힌 **검색·인용용** AI 크롤러 (용도 search·user).

    새로 가져오지 않는다 — 크롤 회차가 남긴 원문(crawl_runs.robots_txt)을 다시
    읽을 뿐이다. 그래서 이 판정에는 네트워크도 새 단계도 없다.

    왜 이게 AI 인용 판정보다 **먼저**여야 하나: OAI-SearchBot 이 막혀 있으면
    ChatGPT 검색 답변의 출처로 실리기 어렵다. 그 상태에서 "콘텐츠가 약해서 인용이
    안 된다" 고 말하면 오진이고, 사용자는 엉뚱한 글을 쓰게 된다. 거꾸로 학습 봇만
    막힌 것을 여기 올리면 그것도 오진이다 — 학습과 인용은 다른 봇이다.
    """
    cr = conn.execute(
        "SELECT robots_txt FROM crawl_runs WHERE project_id=? AND finished_at IS NOT NULL"
        " AND robots_txt IS NOT NULL ORDER BY id DESC LIMIT 1", (project_id,)).fetchone()
    txt = (cr["robots_txt"] if cr else "") or ""
    rows = [r for r in ai_bot_status(txt, home)
            if r["rule"] and r["purpose"] in AI_BOT_CITING]
    # 자기 이름 묶음 없이 `User-agent: *` 로 막힌 봇들 — 원인이 한 줄이면 기회도 한 줄
    wild = [r for r in rows if not _named_in_robots(txt, r["bot"])]
    if len(wild) < 2:
        return rows
    url = home if home.startswith("http") else f"https://{home or 'example.com'}/"
    engines = list(dict.fromkeys(r.get("engine") or r["bot"] for r in wild))
    grouped = {"bot": AI_BOT_WILDCARD, "vendor": None, "purpose": "search",
               "engine": ", ".join(engines), "rule": wild[0]["rule"],
               "bots": [r["bot"] for r in wild],
               # 같은 줄이 구글 검색 색인도 막는가 — 그러면 AI 는 작은 쪽 문제다
               "google_blocked": robots_blocks(txt, url, agent="Googlebot") is not None}
    return [grouped] + [r for r in rows if r not in wild]


def _reason_ai_bot(r: dict, ctx: dict) -> str:
    if r["bot"] == AI_BOT_WILDCARD:
        # 봇 이름으로 센다 — 엔진 이름은 겹쳐서(Perplexity 봇 둘) 개수와 나열이 어긋난다
        bots = r.get("bots") or []
        s = (f"robots.txt 의 `User-agent: *` 묶음({r['rule']})이 AI 검색·인용 크롤러 "
             f"{len(bots)}개({'·'.join(bots)})를 한꺼번에 막습니다. 원인은 이 줄 하나입니다")
        if r.get("google_blocked"):
            # 이 줄은 AI 전용이 아니다 — 구글 검색 전체에서 빠진다는 게 먼저다
            s += (". 같은 줄이 구글 검색(Googlebot)도 막습니다 — AI 이전에 검색 전체에서 "
                  "빠지는 문제입니다")
        return s
    eng = r.get("engine") or r["bot"]
    s = (f"robots.txt 가 {r['bot']}({eng}, {AI_BOT_PURPOSE[r['purpose']]})를 막습니다 "
         f"({r['rule']}). {eng} 답변에서 우리 페이지가 출처로 실리기 어렵습니다 — 글을 "
         "고치기 전에 이 설정이 먼저입니다")
    if r["bot"].lower() == "bingbot":
        # 빙 색인은 Copilot 만의 것이 아니다 — AI 문제로만 부르면 규모를 줄여 말하게 된다
        s += ". Bingbot 차단은 AI 만의 문제가 아니라 빙 검색 전체에서 빠진다는 뜻입니다"
    return s


def robots_blocks(robots_txt: str, url: str, agent: str = "Googlebot") -> str | None:
    """이 주소를 막는 robots.txt 줄. 안 막으면 None.

    막히는지의 판정은 표준 파서(urllib.robotparser)에 맡기고, 막힐 때만 어느 줄인지
    찾는다 — 매칭 규칙을 두 벌 만들면 "막힌다는데 줄은 못 찾는다" 같은 모순이 난다.
    색인 막힘 요청문이 이 한 줄을 못 대서, 지금까지 원인 후보를 짐작으로 세웠다.
    """
    txt = robots_txt or ""
    if not txt.strip():
        return None
    rp = robotparser.RobotFileParser()
    rp.parse(txt.splitlines())
    if rp.can_fetch(agent, url):
        return None
    path = urlsplit(url).path or "/"
    best = ""
    for line in txt.splitlines():
        key, _, val = line.partition(":")
        if key.strip().lower() != "disallow":
            continue
        rule = val.split("#")[0].strip()
        if rule and path.startswith(rule.rstrip("*")) and len(rule) > len(best):
            best = rule
    return f"Disallow: {best}" if best else "robots.txt 가 이 주소를 막습니다(어느 줄인지는 못 짚었습니다)"


# 이보다 오래 안 고친 글은 "낡았을 수 있다"고 말한다. 판정이 아니라 확인 지시라
# 넉넉하게 잡는다 — 2년은 가격·화면·경쟁이 한 번은 바뀌는 시간이다.
STALE_DAYS = 730


def _stale_days(audit: dict) -> int | None:
    """마지막 수정(없으면 발행)으로부터 며칠. 날짜로 안 읽히면 None — 지어내지 않는다."""
    raw = (audit.get("modified") or audit.get("published") or "").strip()
    try:
        d = datetime.date.fromisoformat(raw[:10])
    except ValueError:
        return None
    return (datetime.date.today() - d).days


def _has_render_fields(audit: dict) -> bool:
    """이 감사 행이 모바일·언어 칸을 읽고 온 것인가.

    js_shell 은 새 감사면 늘 0/1 이라 "행의 나이"를 가르는 유일한 칸이다. 옛 행은
    이 칸들이 통째로 NULL 인데, 그것을 "viewport 가 없다"로 읽으면 멀쩡한 페이지에
    없는 문제를 만들어 낸다 — 안 본 것과 없는 것은 다르다.
    """
    return audit.get("js_shell") is not None


# ISO 639-1 언어 + 선택적 ISO 3166-1 Alpha 2 지역. 'en-UK' 는 코드가 아니다(영국은 GB).
_HREFLANG_RE = re.compile(r"^[a-z]{2,3}(-[A-Za-z]{4})?(-[A-Za-z]{2}|-\d{3})?$")


def _hreflang_advice(audit: dict) -> list[dict]:
    """한 장만 보고 확실히 말할 수 있는 hreflang 문제만.

    상호 참조(A→B 면 B→A)는 두 장을 같이 봐야 알아서 여기서 판정하지 않는다 —
    한 장에서 아는 것은 셋이다: 자기 자신이 목록에 있나, 코드가 코드인가,
    x-default 가 있나. 판정을 넓히면 근거 없는 지적이 된다.
    """
    if not _has_render_fields(audit):
        return []
    pairs = _as_list(audit.get("hreflang_json"))
    rows = [p for p in pairs if isinstance(p, list) and len(p) == 2]
    if not rows:
        return []
    out = []
    codes = [str(c).strip() for c, _ in rows]
    url = norm(audit.get("url") or "")
    canon = norm(audit.get("canonical") or "") or url
    if canon and not any(norm(h) == canon for _, h in rows):
        out.append({"tag": "hreflang", "level": "bad",
                    "now": f"이 페이지가 자기 hreflang 목록에 없습니다 ({len(rows)}개 선언)",
                    "fix": "자기 자신을 가리키는 항목을 넣으세요. 자기 참조가 빠지면 구글이 "
                           "이 페이지의 hreflang 전부를 버립니다."})
    bad = [c for c in codes if c.lower() != "x-default" and not _HREFLANG_RE.match(c)]
    bad += [c for c in codes if "-" in c and c.split("-")[-1].lower() in ("uk",)]
    if bad:
        out.append({"tag": "hreflang", "level": "bad",
                    "now": "코드가 아닌 값: " + ", ".join(sorted(set(bad))[:5]),
                    "fix": "언어는 ISO 639-1, 지역은 ISO 3166-1 Alpha 2 입니다 — 영국은 "
                           "en-UK 가 아니라 en-GB. 틀린 항목은 통째로 무시됩니다."})
    if not any(c.lower() == "x-default" for c in codes):
        out.append({"tag": "hreflang", "level": "warn",
                    "now": f"x-default 가 없습니다 ({len(rows)}개 선언)",
                    "fix": "어느 로케일에도 안 맞는 방문자가 갈 곳을 x-default 로 하나 "
                           "지정하세요(언어 선택 화면 또는 기본 로케일)."})
    return out


# ── 검색어 ↔ title/H1 대조 — 글자가 아니라 말로 ───────────────────────────
# 'syringoma vs milia' 는 title 'Syringoma & Milia' 에 **있다**. 부분 문자열로 보면
# 없다고 나와서 "앞부분에 그대로 넣으세요"라고 시켰다 — 구글은 'vs'·'&'·어순을
# 안 본다. 그래서 내용어(조사·접속사·의문사를 뺀 것)가 들어 있는지로 본다.
QUERY_STOPWORDS = frozenset(
    "vs versus or and the a an of in for to with on is are what how "
    "차이 비교 과 와 및".split())
# 비교 의도 — 이 말이 검색어에 있으면 "둘을 나란히 놓고 골라 달라"는 뜻이다.
COMPARE_WORDS = frozenset(("vs", "versus", "difference", "or", "차이", "비교"))
# 판정에 보는 검색어 수 — 한 페이지에 걸린 검색어는 보통 같은 의도의 변주라 상위
# 몇 개면 충분하고, 꼬리까지 보면 지나가는 검색어 하나가 비교 의도를 지어낸다.
TOP_QUERIES = 5
# 이만큼 긴 글에 H2 가 없으면 구조가 없는 것이고, 외부 근거 하나 없으면 신뢰가 없는
# 것이다. 짧은 글(THIN_WORDS 미만)은 이미 "얇다"가 붙어서 두 번 말하지 않는다.
H2_MIN_WORDS = 300
_WORD_RE = re.compile(r"\w+")


def _tokens(text: str) -> list[str]:
    """소문자 낱말 — 비단어 글자(&·/·쉼표)에서 자른다. CJK 는 \\w 라 살아남는다."""
    return _WORD_RE.findall((text or "").lower())


def _content_tokens(text: str) -> list[str]:
    """검색어의 내용어 — 불용어를 뺀 나머지, 순서 유지·중복 제거."""
    return list(dict.fromkeys(t for t in _tokens(text) if t not in QUERY_STOPWORDS))


def _has_token(tok: str, have: set[str]) -> bool:
    """단순 복수 접기 — 'syringomas' 는 'syringoma' 와 같은 말이다(반대 방향도)."""
    return tok in have or (tok.endswith("s") and tok[:-1] in have) or (tok + "s") in have


def _missing_tokens(query: str, text: str) -> list[str]:
    """검색어의 내용어 중 text 에 없는 것. 어순·연결어는 안 본다."""
    have = set(_tokens(text))
    return [t for t in _content_tokens(query) if not _has_token(t, have)]


def _is_comparative(query: str) -> bool:
    return any(t in COMPARE_WORDS for t in _tokens(query))


_SENT_PUNCT = re.compile(r"[.!?。,:;—–]")


def _desc_scraped(desc: str, title: str, h1: str) -> bool:
    """meta description 이 손으로 쓴 문장이 아니라 본문 첫 줄을 긁어 온 것인가.

    두 단서다: 말줄임(…/...)으로 끝난다 — 문장을 자른 흔적. 또는 H1(없으면 title
    의 브랜드 앞부분)으로 시작하면서 그 길이 안에 문장 부호가 하나도 없다 — 제목
    다음에 부제가 그대로 이어진 꼴. 'Syringoma and Milia Similar-looking bumps, …'
    가 그것이다. 손으로 쓴 설명은 첫 마디에서 마침표나 쉼표가 온다.
    """
    d = (desc or "").strip()
    if not d:
        return False
    if d.endswith("…") or d.endswith("..."):
        return True
    head = (h1 or "").strip() or re.split(r"\s*[|—–-]\s*", title or "", 1)[0].strip()
    want = _content_tokens(head)
    if not want or len(d) <= len(head):
        return False
    got = _content_tokens(" ".join(_tokens(d)[:len(_tokens(head)) + 1]))
    if not all(_has_token(t, set(got)) for t in want):
        return False
    return not _SENT_PUNCT.search(d[:len(head) + 6])


# 어느 페이지든 같은 문장이다 — 검색어를 박으라는 지시가 아니라 판단을 시킨다.
TITLE_JUDGE = ("이 페이지에 걸린 검색어 묶음의 주 의도를 title 이 대표하는지 보고, 본문이 "
               "실제로 다루는 말로만 고치세요. 검색어를 글자 그대로 박지 않습니다 — 지금 "
               "title 이 이미 그 의도를 말하면 그대로 두는 것도 답입니다.")


def page_advice(audit: dict | None, queries=(), *, domain: str = "") -> list[dict]:
    """이 페이지의 무엇을 바꿔야 하나 — 결정적 규칙. 화면이 그대로 그린다.

    반환: [{"tag": 손댈 자리, "level": "bad"|"warn", "now": 지금 상태, "fix": 할 일}]
    빈 리스트면 "규칙으로 잡히는 문제 없음"이지 "완벽함"이 아니다.

    queries 는 이 URL 이 실제로 걸린 검색어들(노출 많은 순). title·H1 판정은 첫
    검색어의 **내용어**로 하고(글자 그대로가 아니다 — _content_tokens), 비교 의도는
    상위 TOP_QUERIES 개 중 하나라도 물으면 있는 것으로 본다.
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
            "이 주소를 브라우저로 직접 열어 보세요. 사람에게도 안 열리면 순위·색인 이전의 문제입니다.")
        return out

    top = [q for q in queries if q][:TOP_QUERIES]
    kw = top[0] if top else ""
    kw_tokens = _content_tokens(kw)
    title = (audit.get("title") or "").strip()
    if not title:
        add("title", "bad", "title 태그가 없습니다",
            "걸린 검색어 묶음의 주 의도를 말하는 <title> 하나를 넣으세요"
            + (f" — 예: <title>{kw} — 페이지 요지</title>." if kw else "."))
    else:
        missing = _missing_tokens(kw, title)
        if kw_tokens and len(missing) == len(kw_tokens):
            add("title", "bad",
                f"{title} — 검색어 '{kw}' 의 말({', '.join(kw_tokens)})이 하나도 없습니다",
                TITLE_JUDGE)
        elif len(kw_tokens) >= 2 and len(missing) > len(kw_tokens) / 2:
            add("title", "warn",
                f"{title} — 검색어 '{kw}' 의 말 중 {', '.join(missing)} 이 없습니다",
                TITLE_JUDGE)
        t_max = TITLE_MAX_KO if _wide(title) else TITLE_MAX
        t_min = TITLE_MIN_KO if _wide(title) else TITLE_MIN
        if len(title) > t_max:
            add("title", "warn", f"{len(title)}자라 검색결과에서 잘립니다",
                f"{t_max}자 이내로 줄이고, 브랜드명은 뒤로 미세요.")
        elif len(title) < t_min:
            add("title", "warn", f"{len(title)}자라 너무 짧습니다",
                "무엇에 대한 페이지인지 title 만 읽고 알 수 있게 늘리세요.")

    h1 = _as_list(audit.get("h1_json"))
    desc = (audit.get("meta_description") or "").strip()
    # 긁어 온 설명은 길이가 맞아도 설명이 아니다 — 길이 지적보다 이것을 앞세우고,
    # 한 페이지에 description 지적은 하나만 둔다(둘이면 어느 쪽을 고치라는지 흐려진다).
    if not desc:
        add("meta description", "bad", "설명이 없습니다",
            "검색결과에 뜰 2~3문장을 직접 쓰세요. 안 쓰면 구글이 본문에서 아무 데나 뽑습니다.")
    elif _desc_scraped(desc, title, h1[0] if len(h1) == 1 else ""):
        add("meta description", "warn", f"본문 첫 줄을 긁어 온 것으로 보입니다: {desc[:60]}…",
            "이 페이지가 무엇에 답하고 누구를 위한 것인지 2~3문장으로 직접 쓰세요. 지금은 "
            "본문 첫 줄을 붙여 넣은 것이 검색결과에 그대로 뜹니다.")
    elif len(desc) > (DESC_MAX_KO if _wide(desc) else DESC_MAX):
        add("meta description", "warn", f"{len(desc)}자라 뒤가 잘립니다",
            f"{DESC_MAX_KO if _wide(desc) else DESC_MAX}자 이내로. 중요한 말을 앞에 두세요.")
    elif len(desc) < (DESC_MIN_KO if _wide(desc) else DESC_MIN):
        add("meta description", "warn", f"{len(desc)}자라 너무 짧습니다",
            "숫자·연도·구체적 이득을 넣어 클릭할 이유를 적으세요.")

    if not h1:
        add("H1", "bad", "H1 이 없습니다", "페이지 주제를 그대로 담은 H1 하나를 두세요.")
    elif len(h1) > 1:
        add("H1", "warn", f"H1 이 {len(h1)}개입니다 ({' / '.join(h1[:3])})",
            "H1 은 하나만 두고 나머지는 H2 로 내리세요.")
    elif kw_tokens and len(_missing_tokens(kw, h1[0])) == len(kw_tokens):
        add("H1", "warn",
            f"{h1[0]} — 검색어 '{kw}' 의 말({', '.join(kw_tokens)})이 하나도 없습니다",
            "H1 은 이 페이지의 주제 — 걸린 검색어 묶음이 공통으로 묻는 것 — 를 말하고 "
            "title 과 같은 말을 해야 합니다. 검색어를 그대로 박지 말고 본문이 다루는 "
            "말로 쓰세요.")

    words = audit.get("words")
    long_body = isinstance(words, int) and words >= H2_MIN_WORDS
    if long_body and not _as_list(audit.get("h2_json")):
        add("H2", "warn", f"{words}단어인데 H2 가 하나도 없습니다",
            "걸린 검색어 묶음이 묻는 질문들로 본문을 나누세요 — 그 질문이 H2 후보이고, "
            "한 구간에는 한 가지 생각만 둡니다.")

    if isinstance(words, int) and words < THIN_WORDS:
        add("본문", "warn", f"{words}단어로 얇습니다",
            f"이 검색어를 다루는 하위 질문을 채워 {THIN_WORDS}단어 이상으로 늘리세요.")

    # 외부 링크 0 — 근거 없이 주장만 있는 글. 신뢰가 순위를 가르는 주제(건강·돈·법,
    # YMYL)에서 제일 약한 자리인데, 지금까지 내부 링크만 세고 이건 안 봤다.
    if long_body and audit.get("external_links") == 0:
        add("외부 링크", "warn", f"{words}단어에 외부 링크 0개",
            "본문의 주장을 받쳐 주는 출처 1~3개(학회·논문·공식 문서)로 링크하세요. 신뢰가 "
            "순위를 가르는 주제(건강·돈·법 — YMYL)에서는 이 자리가 제일 약한 곳입니다.")

    # 비교 의도인데 비교 구간이 없다 — 'A vs B' 로 온 방문자는 둘을 나란히 놓은 표를
    # 찾는다. 표 수는 정적 HTML 에서 센 것이라 JS 껍데기면 안 믿는다(extract_advice 와 같다).
    cmp_q = next((q for q in top if _is_comparative(q)), "")
    if cmp_q and _has_extract_fields(audit) and not audit.get("js_shell") \
            and audit.get("tables") == 0:
        add("비교", "warn", f"'{cmp_q}' 로 오는 비교 의도인데 본문에 표가 없습니다",
            "둘을 항목별(생김새·원인·위치·치료·재발)로 나란히 놓는 표 하나와, 그 아래 "
            "한 문단의 결론을 두세요 — 'X vs Y' 로 검색한 방문자 대부분이 그것을 보러 옵니다.")

    # 신선도 — "언제 쓴 글인가" 는 우리가 점검한 날과 다른 사실이다. 이게 없으면
    # 순위 하락의 원인 후보에서 노후화를 못 가른다(늘 "경쟁이 세졌다"로 끝났다).
    stale = _stale_days(audit)
    if stale is not None and stale >= STALE_DAYS:
        seen = audit.get("modified") or audit.get("published")
        add("갱신", "warn", f"마지막 수정 {seen} — {stale // 30}개월 전",
            "숫자·연도·화면·가격이 아직 맞는지 보고 고친 뒤 수정일을 올리세요. 안 고칠 "
            "글이면 그렇다고 답해 주세요 — 날짜만 바꾸는 것은 안 됩니다.")

    # 정적 HTML 로 가져온 값이다 — Yoast·RankMath·AIOSEO 는 ld+json 을 자바스크립트로
    # 넣는다. 그때 "없다"고 단정하면 있는 것을 또 만들게 시킨다. 껍데기로 의심되면
    # 판정을 확인 지시로 바꾼다(없앨 수는 없다 — 정말 없는 사이트가 더 많다).
    schema = _as_list(audit.get("schema_json"))
    if not schema:
        if audit.get("js_shell"):
            add("구조화 데이터", "warn", "정적 HTML 에는 ld+json 이 없습니다 "
                "(본문도 자바스크립트로 그리는 페이지로 보입니다)",
                "먼저 리치 결과 테스트(search.google.com/test/rich-results)로 렌더 후에도 "
                "없는지 확인하세요. 정말 없으면 Article·FAQPage·LocalBusiness 중 하나를 넣습니다.")
        else:
            add("구조화 데이터", "warn", "ld+json 이 없습니다",
                "Article·FAQPage·LocalBusiness 중 이 페이지에 맞는 것 하나를 넣으세요. "
                "AI 답변과 리치 결과가 읽는 자리입니다.")

    # 모바일 — 뷰포트가 없으면 모바일 브라우저가 데스크톱 폭(980px)으로 그린 뒤
    # 축소한다. 기기별 순위 차이의 원인 후보 1번인데 지금까지 아무도 안 봤다.
    if _has_render_fields(audit):
        vp = (audit.get("viewport") or "").lower()
        if not vp:
            add("모바일", "bad", "<meta name=viewport> 가 없습니다",
                '<meta name="viewport" content="width=device-width, initial-scale=1"> 를 '
                "head 에 넣으세요. 없으면 모바일에서 데스크톱 폭으로 그린 뒤 축소해 보여줍니다.")
        elif "width=device-width" not in vp.replace(" ", ""):
            add("모바일", "warn", f"viewport: {audit['viewport']}",
                "width=device-width 를 넣으세요. 고정 폭 뷰포트는 기기마다 다르게 잘립니다.")
        elif "user-scalable=no" in vp.replace(" ", "") or "maximum-scale=1" in vp.replace(" ", ""):
            add("모바일", "warn", f"viewport: {audit['viewport']}",
                "확대를 막고 있습니다. user-scalable=no·maximum-scale 을 빼세요 — 접근성 "
                "문제이고 모바일 사용성 평가에 걸립니다.")

    # 언어 선언 — 없으면 검색엔진이 본문 글자로 추측한다. 빙은 hreflang 보다
    # <html lang> 을 더 세게 본다.
    if _has_render_fields(audit) and not (audit.get("html_lang") or "").strip():
        add("언어", "warn", "<html lang> 이 없습니다",
            '<html lang="ko"> 처럼 이 페이지의 언어를 선언하세요.')

    out += _hreflang_advice(audit)

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
            "이 주소가 정본이 아니라고 선언돼 있습니다. 의도한 것인지 확인하세요.")

    no_alt = audit.get("images_no_alt") or 0
    if no_alt:
        add("이미지", "warn", f"alt 없는 이미지 {no_alt}개",
            "무엇을 보여주는 그림인지 alt 에 적으세요. 이미지 검색 유입과 접근성에 씁니다.")

    words_n = audit.get("words") if isinstance(audit.get("words"), int) else 0
    images = audit.get("images")
    # 본문보다 그림이 많은 페이지 — 그림 속 글자는 검색이 못 읽는다. 697단어에 이미지 12개인
    # 페이지에서 요청문이 alt 만 말하고 이 구조는 말하지 않았다.
    if isinstance(images, int) and images >= IMAGE_HEAVY_MIN and words_n \
            and words_n / images < IMAGE_HEAVY_WORDS:
        add("이미지", "warn", f"이미지 {images}개에 본문 {words_n}단어 — 그림 위주입니다",
            "그림 속 글자는 검색이 못 읽습니다. 핵심 설명·가격·과정이 그림이 아니라 본문 "
            "텍스트에 있는지 확인하고, 그림에만 있으면 본문으로 옮기세요.")

    internal = audit.get("internal_links")
    if isinstance(internal, int) and internal < 3:
        add("내부 링크", "warn", f"이 페이지가 내보내는 내부 링크 {internal}개",
            "관련 글로 3개 이상 연결하세요. 링크가 없는 페이지는 크롤러도 사람도 덜 봅니다.")
    elif isinstance(internal, int) and internal >= LINKS_HEAVY_MIN and words_n \
            and words_n / internal < LINKS_HEAVY_WORDS:
        # 감사가 세는 수에는 메뉴·푸터 링크가 섞인다 — 단정하지 않고 본문 안 수를 세라고 한다
        add("내부 링크", "warn",
            f"내보내는 내부 링크 {internal}개 — 본문 {round(words_n / internal)}단어당 1개",
            "메뉴·푸터를 뺀 본문 안 링크만 세어 보세요. 본문 안에서도 이 정도면 한 문단에 "
            "링크가 몰려 어느 것이 중요한지 흐려집니다 — 이 글의 주제와 가까운 것만 남기세요.")
    return out


# ── 추출성 — "인용될 블록이 있는가" (AI 종류 전용) ───────────────────────────
# page_advice 에 넣지 않는다: 모든 요청문(클릭률·순위…)이 표·질문형 H2 지적으로
# 부풀어 오른다. AI 가 이 페이지를 **뽑아 가느냐**가 걸린 기회에서만 말한다.
EXTRACT_KINDS = ("ai_citation_gap", "aio_exposure")
# AI 답변은 최근 글을 고른다 — ai-seo 점검표의 "6개월 안 갱신". 일반 기준(STALE_DAYS,
# 2년)은 "낡았을 수 있다"는 확인 지시라 그대로 둔다. 둘을 한 값으로 합치면 한쪽이
# 틀린다: 180일로 내리면 클릭률 요청문마다 갱신 지적이 붙고, 730일이면 AI 쪽이 늦다.
AI_CONTENT_STALE_DAYS = 180
# 첫 문단 — 스킬의 "핵심 답 문단 40~60단어". 단어는 공백 기준이라 한국어는 어절이고
# 같은 내용이 영어보다 적게 세진다. 그래서 목표치(40~60)가 아니라 **분명히 넘친**
# 값에서만 말한다 — 한 덩어리로 뽑히기엔 긴 문단.
LEAD_MAX_WORDS = 80
# 질문형 H2 를 따지려면 H2 가 이만큼은 있어야 한다. 하나뿐인 H2 가 질문이 아니라고
# 구조를 탓하면 말이 안 된다.
H2_FOR_QUESTIONS = 2


def _has_extract_fields(audit: dict) -> bool:
    """이 감사 행이 추출성 칸(표·목록·질문형 H2·첫 문단·저자)을 읽고 온 것인가.

    _has_render_fields 와 같은 종류의 가드다: 옛 행은 이 칸들이 통째로 NULL 인데,
    그것을 "표 0개·저자 없음"으로 읽으면 멀쩡한 페이지에 없는 문제를 만든다 — 안
    본 것과 없는 것은 다르다. tables 는 새 감사면 늘 정수라 행의 나이를 가른다.
    """
    return audit.get("tables") is not None


def extract_advice(audit: dict | None, kind: str) -> list[dict]:
    """AI 종류 요청문에만 붙는 진단 — 정적 HTML 로 잴 수 있는 것만 판정한다.

    반환은 page_advice 와 같은 꼴([{tag, level, now, fix}]). 못 재는 것(수치의 출처가
    진짜인지, 문단이 정말 혼자 서는지)은 판정하지 않고 요청문 산출물(brief.
    DELIVER_BY_TAG)로 돌린다.

    **구글 AI 요약과 챗봇은 다른 말을 한다.** 구글은 "AI 용으로 조각내지 말고 사람을
    위한 구조로 쓰라"는 입장이라(ai-seo 스킬도 같다), aio_exposure 쪽은 "사람이 훑어
    읽기 좋은 구조"로 말하고 AI 전용 블록을 시키지 않는다(tag "읽기 구조"). 챗봇
    (ai_citation_gap)은 문단을 그대로 뽑아 가므로 혼자 서는 답 블록을 시켜도 된다
    (tag "추출성"). 갱신은 둘 다 AI_CONTENT_STALE_DAYS 로 본다(tag "갱신" — page_advice
    의 같은 tag 를 요청문이 이것으로 갈아 끼운다). 저자는 tag "저자".
    """
    if not audit or audit.get("error") or kind not in EXTRACT_KINDS:
        return []
    bot = kind == "ai_citation_gap"
    tag = "추출성" if bot else "읽기 구조"
    out: list[dict] = []

    def add(t, level, now, fix):
        out.append({"tag": t, "level": level, "now": now, "fix": fix})

    # 갱신 — 날짜 칸은 추출성 칸보다 먼저 생겼다. 날짜로 안 읽히면 말하지 않는다.
    stale = _stale_days(audit)
    if stale is not None and stale >= AI_CONTENT_STALE_DAYS:
        seen = audit.get("modified") or audit.get("published")
        add("갱신", "warn", f"마지막 수정 {seen} — {stale // 30}개월 전",
            ("챗봇은 최근 글을 출처로 고릅니다. " if bot else
             "AI 요약은 최근 정보를 앞세웁니다. ")
            + "숫자·연도·가격이 아직 맞는지 보고 고친 뒤 수정일을 올리세요 — 날짜만 "
              "바꾸는 것은 안 됩니다.")

    if not _has_extract_fields(audit):
        return out
    if audit.get("js_shell"):
        # 렌더 전 HTML 에서 센 값이다 — "표가 없다"를 사실로 말하면 있는 표를 또
        # 만들게 시킨다. 구조화 데이터와 같은 규칙: 판정을 확인 지시로 바꾼다.
        add(tag, "warn", "정적 HTML 로는 본문 구조(표·목록·첫 문단)를 못 봤습니다 "
                         "(자바스크립트로 그리는 페이지로 보입니다)",
            "브라우저에서 렌더된 화면으로 첫 문단·표·목록이 있는지 먼저 확인하세요. "
            "그 뒤에 아래 산출물 중 없는 것만 만듭니다.")
        return out

    lead = audit.get("lead_words")
    if lead == 0:
        add(tag, "warn", "본문에서 <p> 문단을 못 찾았습니다 (div 로만 짠 페이지일 수 있습니다)",
            "H1 바로 아래에 이 질문에 바로 답하는 문단 하나를 두세요 — 정의나 결론부터."
            if bot else
            "H1 바로 아래 첫 문단에서 독자의 질문에 먼저 답하세요 — 결론부터, 설명은 그 뒤.")
    elif isinstance(lead, int) and lead > LEAD_MAX_WORDS:
        add(tag, "warn", f"첫 문단이 {lead}단어입니다",
            "첫 문단을 40~60단어의 직답으로 줄이고 나머지는 다음 문단으로 넘기세요. "
            "챗봇은 문단 단위로 뽑아서, 긴 문단은 통째로 버려집니다." if bot else
            "첫 문단은 요점만 두세 문장으로. 사람이 첫 화면에서 답을 못 찾으면 요약도 "
            "그 문단을 안 씁니다.")

    tables, lists = audit.get("tables"), audit.get("lists")
    if tables == 0 and lists == 0:
        add(tag, "warn", "본문에 표도 목록(ul/ol)도 없습니다",
            "비교하는 내용은 표로, 순서가 있는 내용은 번호 목록으로 바꾸세요. 챗봇은 "
            "문단보다 표·목록을 그대로 인용합니다." if bot else
            "비교하는 내용은 표로, 순서가 있는 내용은 번호 목록으로 — 사람이 훑어 읽기 "
            "좋은 구조가 요약에도 잡힙니다. AI 용 블록을 따로 만들지 않습니다.")

    h2 = _as_list(audit.get("h2_json"))
    qn = audit.get("h2_questions")
    if len(h2) >= H2_FOR_QUESTIONS and qn == 0:
        add(tag, "warn", f"H2 {len(h2)}개 중 질문형이 없습니다",
            "사람들이 챗봇에 실제로 묻는 문장을 H2 로 두고, 그 바로 아래 첫 문장에서 "
            "답하세요 — 질문과 답이 한 덩어리로 뽑힙니다." if bot else
            "독자가 묻는 순서대로 H2 를 질문 꼴로 바꾸세요 — 제목만 훑어도 답이 어디 "
            "있는지 보이게.")

    # 저자는 구조가 아니라 신뢰 신호라 tag 를 따로 둔다 — "[읽기 구조] 저자 없음"은
    # 무엇을 고치라는지 흐린다. "" 만 본다: NULL 은 저자 칸을 안 읽은 행이다.
    if audit.get("author") == "":
        add("저자", "warn", "저자 표시가 없습니다 (meta author·ld+json author 둘 다 없음)",
            "누가 썼는지(이름·자격)를 본문과 ld+json author 에 적으세요. 이름은 [저자] "
            "자리로 비워 둡니다 — 지어내지 않습니다.")
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

# GA4 교차: 클릭은 버는데 전환(key_events)이 0인 페이지. 둘 다 넘어야 잡는다 —
# 클릭만 보면 GA4 세션이 0인(태그가 안 붙었거나 광고 차단·봇으로 GA4 만 못 잡은)
# 페이지까지 "왔는데 안 샀다"로 잘못 읽는다. 클릭 20은 SHIFT_MIN_IMP(노출 30)보다
# 낮췄다 — 클릭은 노출보다 이미 한 번 걸러진 신호라 표본이 노출만큼 필요 없다.
GA4_NOCONV_MIN_CLICKS = 20
GA4_NOCONV_MIN_SESSIONS = 10

# 매출 잠재력 보정 승수 (score() 가 raw *= value_mult() 로 곱한다) — scoring.md 2절의
# 점수 프레임·WEIGHTS 는 안 건드리고 GA4 신호로 살짝만 기울인다. 범위를 좁게 두는
# 이유: 순위가 크게 뒤집히면 화면을 못 믿는다. 표본 하한은 GA4_NOCONV_MIN_* 를 그대로
# 쓴다 — "세션 몇 개짜리로 돈 안 된다고 단정하지 않는다"는 논리가 zero_conversion_pages
# 와 같아서다.
GA4_MULT_LO, GA4_MULT_HI = 0.85, 1.15
# 전환율(key_events/sessions)이 이 이상이면 승수 상한까지 다 준다(포화) — 업종마다
# 절대 전환율 스케일이 다르므로(전자상거래 vs 리드폼) 절대값이 아니라 상대 신호로만 쓴다.
GA4_MULT_SATURATE_RATE = 0.05

# GA4 승수가 붙는 kind 한 벌 — 이미 존재하는 GSC 페이지가 있는 kind 로 한정한다.
# content_gap·coverage·backlink_* 는 페이지가 없거나 페이지 개념이 아니라서 뺀다
# (붙을 자리가 없다는 뜻이지, 판정을 건너뛴다는 뜻이 아니다). dashboard.py 의 배지가
# 이 한 벌을 그대로 본다 — 두 벌 두지 않는다.
GA4_VALUE_KINDS = frozenset({
    "striking_distance", "ctr_gap", "cannibalization", "rank_decay", "device_gap",
})


# ── 검색어의 의도 — 낱말 표로 가르는 결정적 분류 ─────────────────────────────
# brief.py 에 있던 것을 여기로 내렸다: 요청문이 표를 그리는 데만 쓰던 분류를
# intent_split 검출기가 판정에 쓰게 되면서, 아래 층(scoring)이 위 층(brief)을
# import 할 수 없으니 정본이 여기여야 한다. brief 는 이 이름들을 그대로 다시 내보낸다.
#
# 도시명·브랜드는 안 본다(그건 의도가 아니라 자리다). 순서가 판정이다: 명시적
# 비교(vs·차이)가 먼저, 그다음 방법·원인·치료, 두 명사 사이의 or/and 는 가장 약한
# 비교 신호라 맨 뒤. "milia and syringoma treatment" 는 그래서 치료·구매다 — and 가
# 있어도 treatment 가 답의 꼴을 정한다.
# 라틴 낱말은 토큰 일치, 한글은 조사가 붙어 부분 일치, 띄어쓴 구는 구 일치.
INTENT_WORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("비교", ("vs", "versus", "difference", "differences", "compare", "comparison",
            "차이", "비교", "다른점")),
    ("방법", ("how to", "how do", "how can", "방법", "하는법", "하는 법")),
    ("원인·증상", ("cause", "causes", "symptom", "symptoms", "why", "원인", "증상")),
    ("치료·구매", ("removal", "remove", "treatment", "treat", "clinic", "price", "cost",
               "buy", "제거", "치료", "시술", "가격", "비용", "병원", "구매")),
    # 나라·도시 이름 자체는 의도가 아니지만, 브랜드·시술명에 붙으면 "거기서 어디서 받나"
    # 라는 지역·상업 의도다. "seoul juvelook"·"juvelook korea"를 정보로 못 박았더니
    # title 방향이 설명 글 쪽으로 틀어졌다. 목록은 이 제품의 사이트가 실제로 도는 곳만.
    ("지역", ("near me", "nearby", "korea", "korean", "seoul", "gangnam", "busan", "japan",
            "tokyo", "osaka", "근처", "한국", "서울", "강남", "부산", "잘하는곳", "잘하는 곳")),
)
# 의도 비율을 말해도 되는 노출 합 하한 — 노출 61 에서 "정보 100%"는 비율이 아니라 우연이다.
# intent_split 의 SPLIT_MIN_IMP 과는 다른 물음이다: 저쪽은 "가를 만큼 큰가", 여기는
# "비율이라고 말할 만큼 표본이 있나".
INTENT_MIN_IMPRESSIONS = 200
INTENT_LINK = ("or", "and")        # 두 명사 사이에 서면 비교 — 첫·끝 자리는 아니다
# 기본값은 "못 가른 것"이지 다섯째 의도가 아니다. intent_split 이 이걸 1·2위에서
# 빼는 이유 — 실측에서 `papular scar`(정보) 와 `papular scar treatment`(치료·구매)는
# 한 페이지가 맞았다. 분류 실패를 의도 갈림으로 읽으면 멀쩡한 페이지를 쪼갠다.
INTENT_DEFAULT = "정보"


def query_intent(q: str) -> str:
    """검색어 하나 → 의도 이름(INTENT_WORDS 의 첫째 칸 또는 INTENT_DEFAULT)."""
    low = str(q or "").lower()
    toks = tokens(low)
    joined = " ".join(toks)
    for name, words in INTENT_WORDS:
        for w in words:
            if " " in w:
                hit = w in joined
            elif w.isascii():
                hit = w in toks
            else:
                hit = w in low
            if hit:
                return name
    if any(t in INTENT_LINK for t in toks[1:-1]):
        return INTENT_WORDS[0][0]
    return INTENT_DEFAULT


def intent_share(rows: list[dict]) -> list[tuple[str, int]]:
    """의도별 노출 합, 큰 순. 표에서 잘린 행도 센다 — 비율은 전부의 것이어야 한다."""
    acc: dict[str, int] = {}
    for r in rows:
        acc[r["intent"]] = acc.get(r["intent"], 0) + int(r["impressions"] or 0)
    return sorted(acc.items(), key=lambda x: (-x[1], x[0]))


# ── 한 페이지에 두 의도 (intent_split) ───────────────────────────────────────
# cannibalization 의 반대쪽이다. 저쪽은 "한 검색어를 여러 페이지가 나눠 갖는다"이고
# 이쪽은 "한 페이지가 여러 의도를 떠안는다"다. 둘 다 페이지와 검색어의 짝이 어긋난
# 것인데, 만드는 쪽이 저쪽만 있어서 "합쳐라"는 말할 수 있고 "갈라라"는 못 했다.
#
# 하한은 실측에서 나왔다(brain.db 3사이트 292페이지, 2026-09-16):
#   · 1·2위가 둘 다 명시 의도  — 이 한 줄이 292개에서 정확히 1개를 남겼다. 2위가
#     기본값(정보)인 건은 거의 다 분류 실패였지 의도 갈림이 아니었다.
#   · 2위 의도 노출 ≥ 20 · 검색어 ≥ 2 — 검색어 하나짜리는 오분류와 구분이 안 된다.
#   · 페이지 전체 노출 ≥ 50 — 노출 2짜리 50:50 이 상위를 채우던 것을 막는다.
#   · 홈·언어 루트 제외 — 홈은 사이트 전체를 대표하므로 의도가 섞이는 게 정상이다.
SPLIT_MIN_SECOND_IMP = 20
SPLIT_MIN_SECOND_QUERIES = 2
SPLIT_MIN_IMP = 50
_LANG_SEG = re.compile(r"^[a-z]{2}(-[A-Za-z]{2,4})?$")


def is_site_root(url: str) -> bool:
    """홈이거나 언어 루트(/en/·/zh-TW/)인가 — 의도가 섞여도 나눌 수 없는 자리."""
    segs = [x for x in (urlsplit(url or "").path or "/").split("/") if x]
    return not segs or (len(segs) == 1 and bool(_LANG_SEG.match(segs[0])))


def intent_split(conn: sqlite3.Connection, project_id: int, *,
                 limit: int = 15, at: str | None = None) -> list[dict]:
    """한 페이지가 두 의도를 떠안은 곳 — 2위 의도 노출이 큰 순.

    페이지는 그 검색어로 노출이 가장 큰 것 하나로만 센다 — brief._page_queries 와
    같은 규칙이다. 두 곳이 다른 규칙을 쓰면 요청문의 표와 판정이 어긋난다.
    """
    cur, _, period, _ = snapshot_pair(conn, project_id, at)
    if not cur:
        return []
    first: dict[str, dict] = {}                 # query → 노출 1등 페이지 행
    for r in conn.execute(
        """SELECT query, page, SUM(impressions) imp, SUM(clicks) clk,
                  ROUND(AVG(position),1) pos
             FROM gsc_snapshots
            WHERE project_id=? AND snapshot_date=? AND period_days=? AND page IS NOT NULL
            GROUP BY query, page ORDER BY imp DESC""",
            (project_id, cur, period)):
        first.setdefault(r["query"], {"page": r["page"], "query": r["query"],
                                      "impressions": r["imp"] or 0, "clicks": r["clk"] or 0,
                                      "position": r["pos"],
                                      "intent": query_intent(r["query"])})
    pages: dict[str, list[dict]] = {}
    for row in first.values():
        pages.setdefault(row["page"], []).append(row)
    out = []
    for url, rows in pages.items():
        if is_site_root(url):
            continue
        total = sum(int(r["impressions"] or 0) for r in rows)
        if total < SPLIT_MIN_IMP:
            continue
        share = intent_share(rows)
        if len(share) < 2 or INTENT_DEFAULT in (share[0][0], share[1][0]):
            continue
        (p_name, p_imp), (s_name, s_imp) = share[0], share[1]
        if s_imp < SPLIT_MIN_SECOND_IMP:
            continue
        sq = sorted((r for r in rows if r["intent"] == s_name),
                    key=lambda r: (-int(r["impressions"] or 0), r["query"]))
        if len(sq) < SPLIT_MIN_SECOND_QUERIES:
            continue
        out.append({"page": url, "impressions": total, "queries": len(rows),
                    "primary": p_name, "primary_impressions": p_imp,
                    "secondary": s_name, "secondary_impressions": s_imp,
                    "secondary_queries": sq,
                    "primary_queries": sorted(
                        (r for r in rows if r["intent"] == p_name),
                        key=lambda r: (-int(r["impressions"] or 0), r["query"]))})
    out.sort(key=lambda r: (-r["secondary_impressions"], r["page"]))
    return out[:limit]


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


def _ga4_agg(conn: sqlite3.Connection, project_id: int, snapshot_date: str) -> dict[str, dict]:
    """GA4 최신 스냅샷을 landing_page(경로) 기준으로 접는다 (_page_agg 의 GA4 짝).

    키는 collect_ga4._landing_path() 가 이미 정규화한 경로 그대로다 — GSC page 와
    이을 때는 urlsplit(page).path 로 맞춘다(매칭 규칙의 정본은 collect_ga4.py).
    """
    return {r["landing_page"]: dict(r) for r in conn.execute(
        """SELECT landing_page, SUM(sessions) sessions, SUM(key_events) key_events,
                  SUM(total_revenue) revenue
             FROM ga4_snapshots WHERE project_id=? AND snapshot_date=?
            GROUP BY landing_page""", (project_id, snapshot_date))}


def ga4_funnel(conn: sqlite3.Connection, project_id: int, cur: str | None,
              period: int | None) -> tuple[dict | None, dict | None]:
    """검색 유입 깔때기 — GSC 노출·클릭 옆에 GA4 유기 세션부터 참여·전환·매출까지
    나란히 놓는다. 채널 몫(organic/all)도 같이 낸다.

    **노출·클릭과 세션 이후는 같은 것을 세지 않는다** — impressions/clicks 는 GSC
    축(구글 검색결과 노출·클릭만)이고, sessions 부터는 GA4 축(유기 검색 세션 —
    네이버·빙 등 다른 검색엔진 유입도 포함)이다. 하나로 이어진 깔때기가 아니라
    각자 자기 축의 스냅샷에서 낸 숫자를 나란히 보여줄 뿐이다. 화면은 이 사실을
    사용자에게 그대로 보여준다.

    engaged_sessions 는 GA4 가 직접 안 준다 — 랜딩페이지별 sessions × engagement_rate
    를 더한 값이다(세션 가중 — 단순 평균은 세션 적은 페이지 비중을 부풀린다),
    round() 로 정수화한다.

    channels 의 organic/all 은 sessions/sessions_all 합이다 — 랜딩페이지별로 이미
    "그 페이지의 전 채널 세션"이 들어있는 열이라(sessions_all, ga4_snapshots 테이블
    주석) 페이지 합으로 프로젝트 전체 몫이 나온다.

    GSC 기준 수집일이 없거나 GA4 미연결(ga4_snapshots 에 이 프로젝트 행이 없음)
    이면 (None, None) — 화면은 이 부재로 깔때기 자체를 접는다.
    """
    ga4_date = _latest(conn, _LATEST_GA4, (project_id,))
    if not (cur and ga4_date):
        return None, None
    imp, clk = conn.execute(
        "SELECT SUM(impressions), SUM(clicks) FROM gsc_snapshots"
        " WHERE project_id=? AND snapshot_date=? AND period_days=?",
        (project_id, cur, period)).fetchone()
    sessions = sessions_all = key_events = revenue = engaged = 0.0
    for r in conn.execute(
        "SELECT sessions, sessions_all, key_events, total_revenue, engagement_rate"
        " FROM ga4_snapshots WHERE project_id=? AND snapshot_date=?",
        (project_id, ga4_date)):
        s = r["sessions"] or 0
        sessions += s
        sessions_all += r["sessions_all"] or 0
        key_events += r["key_events"] or 0
        revenue += r["total_revenue"] or 0
        engaged += s * (r["engagement_rate"] or 0)
    funnel = {"impressions": int(imp or 0), "clicks": int(clk or 0),
              "sessions": int(sessions), "engaged_sessions": round(engaged),
              "key_events": round(key_events, 2), "revenue": round(revenue, 2)}
    channels = {"organic": int(sessions), "all": int(sessions_all)}
    return funnel, channels


# GA4 국가 분해는 country_perf(GSC)처럼 나라 수가 늘어날 수 있다 — 상위
# GA4_BD_COUNTRY_TOP 개만 남기고 나머지는 "기타"로 접는다. device·newvsreturning
# 은 값 종류 자체가 적어(2~3개) 접을 필요가 없다 — ga4_breakdown(top=None) 기본값.
GA4_BD_COUNTRY_TOP = 8


def _ga4_bd_agg(conn: sqlite3.Connection, project_id: int, snapshot_date: str,
                dim: str) -> dict[str, dict]:
    """ga4_breakdown 한 축(dim)을 dim_value 기준으로 접는다(landing_page 는 버린다).

    engaged 는 sessions × engagement_rate 합(세션 가중 참여수) — round 는 호출부
    (_ga4_bd_row)가 한다.
    """
    agg: dict[str, dict] = {}
    for r in conn.execute(
        """SELECT dim_value, sessions, key_events, total_revenue, engagement_rate
             FROM ga4_breakdown WHERE project_id=? AND snapshot_date=? AND dim=?""",
        (project_id, snapshot_date, dim)):
        a = agg.setdefault(r["dim_value"], {"sessions": 0, "key_events": 0.0,
                                            "revenue": 0.0, "engaged": 0.0})
        s = r["sessions"] or 0
        a["sessions"] += s
        a["key_events"] += r["key_events"] or 0
        a["revenue"] += r["total_revenue"] or 0
        a["engaged"] += s * (r["engagement_rate"] or 0)
    return agg


def _ga4_bd_row(label: str, v: dict) -> dict:
    s = v["sessions"]
    return {"label": label, "sessions": s, "key_events": round(v["key_events"], 2),
            "revenue": round(v["revenue"], 2),
            "engagement_rate": round(v["engaged"] / s, 4) if s else 0.0}


def ga4_breakdown(conn: sqlite3.Connection, project_id: int, dim: str, *,
                  top: int | None = None) -> list[dict]:
    """ga4_breakdown 한 축(device|country|newvsreturning)을 세션 내림차순으로.

    top 을 주면 상위 top 개만 남기고 나머지를 "기타" 한 줄로 접는다(합산 sessions·
    key_events·revenue, engagement_rate 는 세션 가중 재계산 — country 축에 쓴다,
    GA4_BD_COUNTRY_TOP 참조). GA4 미연결이면 빈 목록.
    """
    ga4_date = _latest(conn, _LATEST_GA4, (project_id,))
    if not ga4_date:
        return []
    agg = _ga4_bd_agg(conn, project_id, ga4_date, dim)
    items = sorted(agg.items(), key=lambda kv: -kv[1]["sessions"])
    if top and len(items) > top:
        head, tail = items[:top], items[top:]
        merged = {"sessions": 0, "key_events": 0.0, "revenue": 0.0, "engaged": 0.0}
        for _, v in tail:
            for k in merged:
                merged[k] += v[k]
        return [_ga4_bd_row(k, v) for k, v in head] + [_ga4_bd_row("기타", merged)]
    return [_ga4_bd_row(k, v) for k, v in items]


def page_performance(conn: sqlite3.Connection, project_id: int, *,
                     limit: int = 30) -> list[dict]:
    """최신 스냅샷의 페이지별 성과 + 직전 대비 Δ클릭 — 많이 잃은 페이지가 위다.

    비교 짝은 snapshot_pair 가 고른다 (scoring.md 4-3b) — 28일치에서 90일치를 빼면
    Δ가 전부 거짓이 된다. page 가 NULL 인 구버전 스냅샷에서는 빈 목록이다
    (결함이 아니라 데이터 부재).

    GA4 가 연결돼 있으면(ga4_snapshots 에 최신 행이 있으면) 같은 줄에 sessions·
    key_events·revenue 를 얹는다 — 없으면 그 키 자체를 안 넣는다. 화면이 열 유무로
    GA4 연결 여부를 가른다(빈 열을 보여주면 고장으로 읽힌다).
    """
    cur, prev, period, _ = snapshot_pair(conn, project_id)
    if not cur:
        return []
    now_ = _page_agg(conn, project_id, cur, period)
    before = _page_agg(conn, project_id, prev, period) if prev else {}
    ga4_date = _latest(conn, _LATEST_GA4, (project_id,))
    ga4 = _ga4_agg(conn, project_id, ga4_date) if ga4_date else {}
    rows = []
    for page, r in now_.items():
        b = before.get(page)
        row = {"page": page, "clicks": r["clk"], "impressions": r["imp"],
               "ctr": _ctr_pct(r["clk"], r["imp"], 2),
               "position": round(r["pos"], 1) if r["pos"] is not None else None,
               "queries": r["q"],
               "dclk": (r["clk"] - b["clk"]) if b else None}
        if ga4_date:
            g = ga4.get(urlsplit(page).path)
            row["sessions"] = g["sessions"] if g else None
            row["key_events"] = g["key_events"] if g else None
            row["revenue"] = g["revenue"] if g else None
        rows.append(row)
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


def zero_conversion_pages(conn: sqlite3.Connection, project_id: int, *,
                          limit: int = 20) -> list[dict]:
    """클릭은 GA4_NOCONV_MIN_CLICKS 이상 버는데 GA4 key_events 가 0인 페이지.

    GSC 가 여태 못 보던 자리 — 클릭까지는 벌어도 그 뒤(전환)가 없다. GA4 미연결
    (ga4_snapshots 에 이 프로젝트 행이 없음)이면 빈 목록. **왜 전환이 안 되는지는
    여기서 판정하지 않는다** — CTA 부재인지 상품이 안 맞는지는 이 데이터로 못 본다.
    """
    cur, _, period, _ = snapshot_pair(conn, project_id)
    ga4_date = _latest(conn, _LATEST_GA4, (project_id,))
    if not (cur and ga4_date):
        return []
    ga4 = _ga4_agg(conn, project_id, ga4_date)
    out = []
    for page, r in _page_agg(conn, project_id, cur, period).items():
        g = ga4.get(urlsplit(page).path)
        if not g:
            continue
        clicks, sessions = r["clk"] or 0, g["sessions"] or 0
        if clicks < GA4_NOCONV_MIN_CLICKS or sessions < GA4_NOCONV_MIN_SESSIONS:
            continue
        if g["key_events"]:
            continue
        out.append({"page": page, "clicks": clicks, "impressions": r["imp"],
                    "sessions": sessions, "queries": r["q"]})
    return sorted(out, key=lambda x: -x["clicks"])[:limit]


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
_LATEST_GA4 = "SELECT MAX(snapshot_date) FROM ga4_snapshots WHERE project_id=?"


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
# test_seams 가 이 튜플과 화면을 대조한다.
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
        return "robots_blocked", "robots.txt 가 크롤을 막고 있습니다. 허용으로 풀기 전에는 색인되지 않습니다"
    if fetch and fetch != "SUCCESSFUL":
        return "fetch_error", f"구글이 페이지를 못 가져왔습니다({fetch}). 서버 응답과 리다이렉트부터 확인하세요"
    if gc and uc and gc != uc:
        return "canonical_mismatch", (f"구글이 고른 대표 주소가 다릅니다: {gc} (내 선언 {uc}). "
                                      f"중복 페이지를 정리하거나 canonical 을 맞추세요")
    state = r["coverage_state"] or r["indexing_state"] or r["verdict"] or "원인 미상"
    return "not_indexed", f"아직 색인되지 않았습니다({state}). 내부 링크와 사이트맵으로 크롤을 유도하세요"


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

    각 구간에 딸린 "queries"는 그 구간에 지금 있는 검색어(클릭 내림차순, 상한
    DETAIL_TOP_N) — 화면이 펼쳤을 때 보여줄 목록이다. "was"는 그 검색어가 직전
    스냅샷에서 있던 구간 이름(직전에 없었으면 None) — "이 구간이 +1" 일 때
    어디서 밀려왔는지가 여기서 나온다.
    """
    if not (cur and period):
        return []

    def snap_bands(snap):
        """노출 하한을 넘는 검색어만 {query: (row, band)} 로."""
        rows = _snap_agg(conn, project_id, snap, period).items() if snap else ()
        return {q: (r, _band_of(r["pos"])) for q, r in rows if (r["imp"] or 0) >= SHIFT_MIN_IMP}

    now_rows, before_rows = snap_bands(cur), snap_bands(prev)
    now_ = {}
    for _, b in now_rows.values():
        now_[b] = now_.get(b, 0) + 1
    before = {}
    for _, b in before_rows.values():
        before[b] = before.get(b, 0) + 1

    def queries_of(label):
        items = [{"q": q, "clicks": r["clk"] or 0, "impressions": r["imp"] or 0,
                  "position": round(r["pos"], 1) if r["pos"] is not None else None,
                  "was": before_rows[q][1] if q in before_rows else None}
                 for q, (r, b) in now_rows.items() if b == label]
        return sorted(items, key=lambda x: -x["clicks"])[:DETAIL_TOP_N]

    return [{"label": lb, "n": now_.get(lb, 0),
             "prev_n": before.get(lb, 0) if prev else None,
             "d": now_.get(lb, 0) - before.get(lb, 0) if prev else None,
             "queries": queries_of(lb)}
            for _, _, lb in RANK_BANDS]


# 갈래에 못 넣은 것을 버리면 표의 합이 총계와 안 맞고, 그러면 읽는 사람이 표
# 전체를 못 믿는다. 남는 것은 언제나 이 이름으로 묶어 낸다.
UNCLASSIFIED = "미분류"


def is_brand_query(query: str, aliases: list[str]) -> bool:
    """검색어가 내 브랜드 별칭을 품었나. 별칭 정본은 aliases_of(cfg) 하나다.

    클릭 +20% 가 브랜드 검색이 는 것이면 SEO 는 제자리다 — 이 둘을 안 가르면
    화면의 모든 증감이 "잘 되고 있다"로 읽힌다.
    """
    q = norm(query)
    return any(a in q for a in (norm(x) for x in aliases) if a)


def _perf_row(label: str, a: dict | None, b: dict | None, top: list[dict] | None = None) -> dict:
    """한 갈래의 이번/직전 누적을 화면이 읽을 한 줄로. Δ순위는 양수가 좋아진 쪽이다
    (movers·rank_decay 의 dpos 규약과 같다). top 은 이 갈래에 속한 검색어 목록
    (없으면 빈 목록) — "queries" 는 이미 개수로 쓰이므로 다른 이름을 쓴다."""
    z = {"clicks": 0, "imp": 0, "pw": 0.0, "n": 0}
    a, b = a or z, b or z
    pos = round(a["pw"] / a["imp"], 1) if a["imp"] else None
    ppos = round(b["pw"] / b["imp"], 1) if b["imp"] else None
    ctr = _ctr_pct(a["clicks"], a["imp"], 2)
    return {"label": label, "clicks": a["clicks"], "impressions": a["imp"],
            "ctr": ctr, "position": pos, "queries": a["n"],
            "has_prev": bool(b["imp"] or b["clicks"]),
            "d_clicks": a["clicks"] - b["clicks"],
            "d_impressions": a["imp"] - b["imp"],
            "d_ctr": round(ctr - _ctr_pct(b["clicks"], b["imp"], 2), 2) if b["imp"] else None,
            "d_position": round(ppos - pos, 1) if pos is not None and ppos is not None else None,
            "top": top or []}


def _bucket_perf(conn: sqlite3.Connection, project_id: int, cur: str | None,
                 prev: str | None, period: int | None, key_of) -> list[dict]:
    """스냅샷을 갈래별로 접는다 — 클릭·노출·CTR·평균순위 + 직전 대비 Δ.

    평균순위는 노출 가중이다. 검색어 수로 그냥 나누면 노출 3짜리 1위가 노출
    3천짜리 12위를 끌어내려 갈래 전체가 거짓말을 한다.
    """
    if not (cur and period):
        return []

    def fold(snap):
        acc: dict[str, dict] = {}
        rows: dict[str, dict[str, dict]] = {}
        for query, r in (_snap_agg(conn, project_id, snap, period) if snap else {}).items():
            k = key_of(query)
            a = acc.setdefault(k, {"clicks": 0, "imp": 0, "pw": 0.0, "n": 0})
            a["clicks"] += r["clk"] or 0
            a["imp"] += r["imp"] or 0
            a["pw"] += (r["pos"] or 0) * (r["imp"] or 0)
            a["n"] += 1
            rows.setdefault(k, {})[query] = r
        return acc, rows

    now_, now_rows = fold(cur)
    before, before_rows = fold(prev)

    def top_of(label):
        b_rows = before_rows.get(label, {})
        items = [{"q": q, "clicks": r["clk"] or 0, "impressions": r["imp"] or 0,
                  "position": round(r["pos"], 1) if r["pos"] is not None else None,
                  "d_clicks": (r["clk"] or 0) - (b_rows[q]["clk"] or 0) if q in b_rows else None}
                 for q, r in now_rows.get(label, {}).items()]
        return sorted(items, key=lambda x: -x["clicks"])[:DETAIL_TOP_N]

    # 클릭 내림차순이다 — 화면의 판정 문장이 "클릭이 가장 많은 갈래"를 지목하는데
    # 노출순으로 세우면 그 주인공이 표 네 번째 줄에 앉는다. 미분류는 잔여 갈래라
    # 아무리 커도 맨 아래다.
    return sorted((_perf_row(k, now_.get(k), before.get(k), top_of(k))
                   for k in set(now_) | set(before)),
                  key=lambda x: (x["label"] == UNCLASSIFIED, -x["clicks"], -x["impressions"]))


def brand_split(conn: sqlite3.Connection, project_id: int, cur: str | None,
                prev: str | None, period: int | None, aliases: list[str]) -> list[dict]:
    """브랜드 vs 논브랜드 두 줄. 별칭이 없으면 판정 자체가 불가능하므로 빈 목록."""
    if not aliases:
        return []
    rows = {r["label"]: r for r in _bucket_perf(
        conn, project_id, cur, prev, period,
        lambda q: "brand" if is_brand_query(q, aliases) else "nonbrand")}
    if not rows:
        return []
    # 한쪽이 통째로 없어도 두 줄을 낸다 — "논브랜드가 0"은 빈 표가 아니라 답이다.
    return [rows.get(k) or _perf_row(k, None, None) for k in ("brand", "nonbrand")]


def canon_intent(v: str | None) -> str:
    """DB 의 intent 표기를 classify_intent 어휘로 접는다.

    이 컬럼은 자유 문자열이라 사람·Claude 보정이 'informational' 을 넣어 왔다.
    정본은 classify_intent 가 내는 넷(info·commercial·transactional·navigational)이고,
    접지 않으면 화면이 라벨을 못 찾아 영문 원문이 그대로 뜬다 — 이 리포가 kind
    라벨에서 이미 한 번 겪은 사고다.
    """
    s = (v or "").strip().lower()
    return "info" if s == "informational" else s


def keyword_perf(conn: sqlite3.Connection, project_id: int, cur: str | None,
                 prev: str | None, period: int | None, col: str) -> list[dict]:
    """활성 키워드의 intent / cluster 별 성과.

    조인 축은 gsc_snapshots.query ↔ keywords.keyword 이고 비교는 norm() 이다.
    정보성만 잡고 거래성이 0인 상태가 제일 흔한 실패인데, 여태 화면이 그걸
    말하지 않았다 — intent 는 채워만 놓고 아무도 안 읽는 컬럼이었다.
    """
    fold_ = canon_intent if col == "intent" else (lambda v: v)
    m = {norm(r["keyword"]): fold_(r[col]) for r in conn.execute(
        f"SELECT keyword, {col} FROM keywords "
        f"WHERE project_id=? AND is_active=1 AND {col} IS NOT NULL",
        (project_id,)) if norm(r["keyword"])}
    rows = _bucket_perf(conn, project_id, cur, prev, period,
                        lambda q: m.get(norm(q)) or UNCLASSIFIED)
    # 어느 줄이 미분류인지는 여기서 못 박는다 — 화면이 "미분류" 라는 글자를
    # 자기 사본으로 들고 있으면 이름을 고칠 때 한쪽만 낡는다.
    for r in rows:
        r["unclassified"] = r["label"] == UNCLASSIFIED
    return rows


def ga4_intent_approx(conn: sqlite3.Connection, project_id: int, cur: str | None,
                      period: int | None) -> list[dict]:
    """의도 갈래별 GA4 전환 — keyword_perf(by_intent) 의 클릭·노출 옆에 세울 근사치.

    GSC 검색어와 GA4 세션은 직접 이어지지 않는다 — 다리는 페이지뿐이다: 그 의도의
    검색어가 걸린 페이지들을 모아 GA4 실적을 더한다. 그래서 이건 **그 의도 자체의
    전환이 아니라, 그 의도의 검색어가 데려온 페이지들 전체의 전환**이다. 페이지
    하나가 여러 의도의 검색어에 걸리면 그 페이지의 전환은 각 의도에 중복으로
    잡힌다 — shared_pages 가 그 개수를 말한다(화면은 이걸 숫자 옆에 반드시 적는다).

    GA4 미연결이거나 intent 붙은 활성 키워드가 없으면 빈 목록.
    """
    ga4_date = _latest(conn, _LATEST_GA4, (project_id,))
    if not (cur and period and ga4_date):
        return []
    intent_of = {norm(r["keyword"]): canon_intent(r["intent"]) for r in conn.execute(
        "SELECT keyword, intent FROM keywords "
        "WHERE project_id=? AND is_active=1 AND intent IS NOT NULL", (project_id,))
        if norm(r["keyword"])}
    if not intent_of:
        return []

    # 검색어 → 그 검색어가 걸린 페이지 경로(distinct). 의도별로 모은다.
    pages_of: dict[str, set[str]] = {}
    for r in conn.execute(
        """SELECT DISTINCT query, page FROM gsc_snapshots
            WHERE project_id=? AND snapshot_date=? AND period_days=? AND page IS NOT NULL""",
        (project_id, cur, period)):
        label = intent_of.get(norm(r["query"]))
        if not label:
            continue
        pages_of.setdefault(label, set()).add(urlsplit(r["page"]).path)
    if not pages_of:
        return []

    ga4 = _ga4_agg(conn, project_id, ga4_date)
    # 한 페이지가 몇 갈래에 걸렸는지 — 중복 집계 경고에 쓴다.
    intent_count: dict[str, int] = {}
    for paths in pages_of.values():
        for path in paths:
            intent_count[path] = intent_count.get(path, 0) + 1

    out = []
    for label, paths in pages_of.items():
        matched = [p for p in paths if p in ga4]
        out.append({"label": label, "pages": len(paths), "pages_matched": len(matched),
                    "sessions": sum(ga4[p]["sessions"] or 0 for p in matched),
                    "key_events": round(sum(ga4[p]["key_events"] or 0 for p in matched), 2),
                    "revenue": round(sum(ga4[p]["revenue"] or 0 for p in matched), 2),
                    "shared_pages": sum(1 for p in matched if intent_count[p] > 1)})
    return sorted(out, key=lambda x: -x["key_events"])


def country_perf(conn: sqlite3.Connection, project_id: int, *,
                 limit: int = 12) -> list[dict]:
    """국가별 성과 — gsc_breakdown dim='country'. 직전 수집일과 비교한다.

    스키마도 적재 함수도 내내 있었는데 기본 수집 차원이 device 뿐이라 이 축은
    한 번도 안 찍혔다. 같은 조립을 device_gap 이 dim='device' 로 하고 있다.
    """
    dates = [r[0] for r in conn.execute(
        "SELECT DISTINCT snapshot_date FROM gsc_breakdown "
        "WHERE project_id=? AND dim='country' ORDER BY 1 DESC LIMIT 2", (project_id,))]
    if not dates:
        return []

    def fold(snap):
        return {r["dim_value"]: {"clicks": r["clk"] or 0, "imp": r["imp"] or 0,
                                 "pw": r["pw"] or 0.0, "n": r["n"]}
                for r in conn.execute(
                    """SELECT dim_value, SUM(clicks) clk, SUM(impressions) imp,
                              SUM(position * impressions) pw, COUNT(*) n
                         FROM gsc_breakdown
                        WHERE project_id=? AND snapshot_date=? AND dim='country'
                        GROUP BY dim_value""", (project_id, snap))}

    now_, before = fold(dates[0]), (fold(dates[1]) if len(dates) > 1 else {})
    return sorted((_perf_row(k, now_.get(k), before.get(k)) for k in now_),
                  key=lambda x: (-x["clicks"], -x["impressions"]))[:limit]


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


def _fit_of(conn: sqlite3.Connection, project_id: int, target: str, *,
            question: bool = False) -> float:
    """기회 row 의 fit 근사 — 데이터로 답할 수 있는 만큼만 결정적으로.

    fit 의 진짜 판정은 Claude 가 하지만, 0.5 중립으로 두면 w_fit 가 큰 프리셋
    (local_clinic 0.45) 에서 모든 기회가 점수 면적 한가운데만 차지한다.
    활성 키워드와 일치하면 0.8, cluster 매칭이면 0.65, 그 외 0.5.

    question=True(대상이 AI 에 물은 질문 문장)면 "그 외"를 한 번 더 가른다 —
    _fit_of_question. 키워드 대상의 동작은 그대로다.
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
    if question and _names_us(conn, project_id, t):
        return FIT_Q_BRAND
    for r in rows:
        if r["cluster"] and norm(r["keyword"]) == t:
            return 0.65
    for r in rows:
        if r["cluster"] and norm(r["cluster"]) in t:
            return 0.65
    if question:
        return _fit_of_question(conn, project_id, target, rows)
    return 0.5


# ── 질문(AI 프롬프트) 대상의 fit ─────────────────────────────────────────────
# 키워드 대상은 "추적 중인 검색어인가"로 충분하다 — 검색어는 사이트가 이미 쓰는 말의
# 조각이다. 질문은 문장이라 활성 키워드와 글자째 같을 일이 거의 없어서 거의 전부 0.5
# 중립에 떨어졌고, 일반 질문 15건이 38.5점 동점이 됐다. "제주도 피부과 추천" 같은
# 사이트가 다루지도 않는 질문과 실제 격차를 점수가 못 갈랐다. 그래서 질문일 때만
# 사이트가 **실제로 가진 말**(크롤·감사한 페이지의 경로·제목, GSC 가 본 페이지 경로,
# 활성 키워드·클러스터)과 겹치는지를 본다:
FIT_Q_BRAND = 0.8     # 우리 이름을 부른 질문 — 추적 키워드 정확 일치와 같은 급. 브랜드
                      # 질문에서 빠지는 것은 남의 자리가 아니라 우리 자리를 잃는 것이다
FIT_Q_KEYWORD = 0.65  # 활성 키워드 하나가 문장 안에 통째로 들어 있다 — cluster 명 포함과
                      # 같은 급(주제가 겹친다). 단 그 키워드에 주제 낱말이 있어야 한다
FIT_Q_PAGE = 0.5      # 사이트의 페이지·키워드와 주제 낱말이 겹친다 — 다룰 자리는 있다.
                      # 판단할 근거가 이 이상은 없으므로 예전 중립값을 그대로 둔다
FIT_Q_NONE = 0.2      # 아무것과도 안 겹친다 — 사이트가 안 다루는 질문일 공산이 크다.
                      # 0 이 아닌 이유: 겹침은 낱말 수준이라 동의어·다른 표기를 못 잡는다.
                      # 틀렸을 때 목록에서 사라지지 않고 뒤로만 밀리게 둔다.
                      # local_clinic(w_fit 0.45)에서 0.5→0.2 는 13.5점이다
FIT_GENERIC_SHARE = 0.5   # 사이트 문서 절반 이상에 들어 있는 낱말은 간판말("OO피부과"의
                          # 피부과)이다 — 그것 하나 겹쳤다고 같은 주제로 치지 않는다
FIT_GENERIC_MIN_DOCS = 4  # 문서가 이보다 적으면 간판말을 가를 표본이 안 된다 — 안 거른다
FIT_PAGE_LIMIT = 300      # GSC 페이지는 노출 많은 순으로 이만큼만 — 사이트의 말은 거기서 다 나온다
# 주제가 아닌 낱말 — 의도어(INTENT_*)와 URL 부스러기. 이게 겹쳐도 같은 주제가 아니다.
_FIT_NOISE = (INTENT_TRANSACTIONAL | INTENT_COMMERCIAL | INTENT_NAVIGATIONAL
              | {"www", "http", "https", "html", "htm", "php", "asp", "aspx", "index",
                 "category", "tag", "tags", "page", "post", "posts", "notice", "amp",
                 "com", "net", "org", "co", "kr", "ko", "en", "ja", "zh"})


def _fit_word(t: str) -> bool:
    """사이트 어휘로 쓸 낱말인가 — 한글이면 두 글자, 라틴·숫자면 세 글자 이상, 소음 아님."""
    if t in _FIT_NOISE or t.isdigit():
        return False
    return len(t) >= (2 if re.search(r"[가-힣]", t) else 3)


def _fit_hits(word: str, q_norm: str, q_toks: set[str]) -> bool:
    """질문이 이 낱말을 품었나. 한글은 조사가 붙어 토큰이 안 맞으므로("피부과는")
    정규화한 문장에서 부분 문자열로, 라틴은 부분 문자열이면 'art' 가 'start' 에
    걸리므로 토큰으로 본다."""
    return word in q_norm if re.search(r"[가-힣]", word) else word in q_toks


def _brand_names(conn: sqlite3.Connection, project_id: int) -> set[str]:
    """우리 이름들(정규화) — 사이트 이름·도메인 앞부분·yaml 의 brand_aliases."""
    p = conn.execute("SELECT name, domain, config_path FROM projects WHERE id=?",
                     (project_id,)).fetchone()
    if not p:
        return set()
    names = [p["name"], _stem(p["domain"] or "")]
    if p["config_path"]:
        try:
            import db
            names += aliases_of(db.load_project_yaml(p["config_path"]))
        except Exception:     # yaml 은 부가 정보다 — 없어도 이름·도메인으로 판정한다
            pass
    return {n for n in (norm(x) for x in names if x) if len(n) >= 2}


def _names_us(conn: sqlite3.Connection, project_id: int, q_norm: str) -> bool:
    return any(b in q_norm for b in _brand_names(conn, project_id))


def _crawl_heads(conn: sqlite3.Connection, project_id: int):
    """최신 크롤 회차의 페이지 머리(url·status·title·h1)."""
    return conn.execute(
        """SELECT url, status, title, h1 FROM crawl_pages WHERE run_id=(
             SELECT MAX(id) FROM crawl_runs WHERE project_id=?)""", (project_id,)).fetchall()


TOPIC_PAGE_LIMIT = 5    # 검색어 하나에 싣는 후보 지면 — 그보다 많으면 주제가 아니라 간판말이다


def pages_by_topic(conn: sqlite3.Connection, project_id: int,
                   targets) -> dict[str, list[dict]]:
    """검색어 → 제목·H1 에 그 검색어가 통째로 들어간 내 지면(최신 크롤 회차).

    pages_by_query 는 "그 검색어로 순위에 걸린 페이지"다 — 10위 밖이면 비고, 그러면
    요청문이 "페이지 없음 → 새로 쓴다"가 된다. 그렇게 이미 있는 온다 리프팅·써마지
    지면을 두고 같은 주제의 새 글 설계도가 두 번 나갔다. 여기는 순위가 아니라 사이트가
    가진 지면에서 찾는다. 200 이 아닌 주소(리다이렉트·오류)는 고칠 지면이 아니다.
    후보가 TOPIC_PAGE_LIMIT 를 넘으면 그 말은 사이트 전체의 간판말이라 싣지 않는다.

    primary — 제목 첫 토막(구분자 앞)이나 H1 이 그 검색어로 **시작하는** 지면. 그
    주제의 전용 지면이다. 제목 한가운데 검색어가 든 것(비교 블로그 "울쎄라피 프라임,
    써마지와 뭐가 다른가요?")은 주제를 스치는 글이라 후보로만 싣는다. "기미인가요"
    처럼 검색어 뒤에 글자가 바로 붙으면 다른 낱말이라 primary 가 아니다.
    """
    want = {t: norm(t) for t in dict.fromkeys(str(x) for x in targets if x)}
    want = {t: n for t, n in want.items() if len(n) >= 2}
    if not want:
        return {}
    heads = [(r["url"], r["title"] or "", r["h1"] or "",
              norm(f"{r['title'] or ''} {r['h1'] or ''}"))
             for r in _crawl_heads(conn, project_id) if r["status"] == 200]
    out: dict[str, list[dict]] = {}
    for t, n in want.items():
        lead = re.compile(re.escape(t.strip().lower()) + r"(?![^\W_])")
        hit = [{"page": u, "title": ti, "h1": h1,
                "primary": any(lead.match(s.strip().lower())
                               for s in (re.split(r"\s[|\-–—]\s", ti)[0], h1))}
               for u, ti, h1, hn in heads if n in hn]
        if 0 < len(hit) <= TOPIC_PAGE_LIMIT:
            out[t] = sorted(hit, key=lambda h: not h["primary"])
    return out


def topic_page(hits) -> str | None:
    """pages_by_topic 후보 → 손댈 지면 하나. 전용 지면(primary)이 딱 하나일 때만 고른다 —
    둘이면 어느 쪽이 맡을지 사람이 정하고, 스치는 글뿐이면 그 글은 주제 지면이 아니다.
    요청문(brief.page_of)과 페이지 감사(collect_page.target_urls)가 같은 규칙을 쓴다."""
    lead = [h for h in hits or [] if h.get("primary")]
    return lead[0]["page"] if len(lead) == 1 else None


def _site_docs(conn: sqlite3.Connection, project_id: int) -> list[str]:
    """사이트가 실제로 가진 말 — 문서 하나 = 페이지 하나(경로+제목) 또는 키워드 하나.

    페이지는 세 출처를 합친다(gen_prompts.offers 처럼 하나만 고르지 않는다 — 여기는
    재료가 아니라 어휘라 많을수록 덜 틀린다): 최신 크롤 회차, 최신 페이지 감사,
    최신 GSC 스냅샷의 페이지. 같은 경로는 한 문서다.
    """
    import urllib.parse
    pages: dict[str, str] = {}

    def add(url, title):
        path = urllib.parse.unquote(re.sub(r"^https?://[^/]+", "", str(url or "")))
        path = path.split("?")[0].split("#")[0]
        if path.strip("/") or title:
            pages[path] = (pages.get(path, "") + " " + str(title or "")).strip()

    for r in _crawl_heads(conn, project_id):
        add(r["url"], f"{r['title'] or ''} {r['h1'] or ''}")
    for r in conn.execute(
            """SELECT url, title FROM page_audits WHERE project_id=? AND checked_date=(
                 SELECT MAX(checked_date) FROM page_audits WHERE project_id=?)""",
            (project_id, project_id)):
        add(r["url"], r["title"])
    cur, _prev, period, _mm = snapshot_pair(conn, project_id)
    if cur:
        for r in conn.execute(
                """SELECT page FROM gsc_snapshots
                    WHERE project_id=? AND snapshot_date=? AND period_days=?
                      AND page IS NOT NULL
                 GROUP BY page ORDER BY SUM(impressions) DESC LIMIT ?""",
                (project_id, cur, period, FIT_PAGE_LIMIT)):
            add(r["page"], None)
    docs = [f"{path} {title}" for path, title in pages.items()]
    docs += [f"{r['keyword']} {r['cluster'] or ''}" for r in conn.execute(
        "SELECT keyword, cluster FROM keywords WHERE project_id=? AND is_active=1",
        (project_id,))]
    return docs


def _fit_of_question(conn: sqlite3.Connection, project_id: int, prompt: str,
                     kw_rows) -> float:
    """질문 문장이 사이트가 가진 말과 겹치나 — FIT_Q_* 의 판정.

    사이트 어휘가 아예 없으면(페이지도 키워드도 안 모았다) 0.5 다: 겹칠 재료가
    없는 것을 "안 겹친다"로 읽으면 수집 전의 모든 질문이 무관 판정을 받는다.
    """
    docs = _site_docs(conn, project_id)
    if not docs:
        return FIT_Q_PAGE
    q_norm, q_toks = norm(prompt), set(tokens(prompt))
    doc_norms = [norm(d) for d in docs]

    def specific(word: str) -> bool:
        """간판말이 아닌가 — 사이트 문서 절반 이상에 있으면 그 사이트 전체의 말이다."""
        if len(doc_norms) < FIT_GENERIC_MIN_DOCS:
            return True
        share = sum(1 for d in doc_norms if word in d) / len(doc_norms)
        return share < FIT_GENERIC_SHARE

    # 활성 키워드가 문장 안에 통째로 — 단 그 키워드가 주제 낱말을 하나는 가져야 한다
    # ("피부과 추천" 이 "제주도 피부과 추천" 안에 있다고 같은 주제가 아니다).
    for r in kw_rows:
        kn = norm(r["keyword"])
        if len(kn) >= 2 and kn in q_norm and any(
                _fit_word(w) and specific(w) for w in tokens(r["keyword"])):
            return FIT_Q_KEYWORD
    vocab = {w for d in docs for w in tokens(d) if _fit_word(w)}
    if any(_fit_hits(w, q_norm, q_toks) and specific(w) for w in vocab):
        return FIT_Q_PAGE
    return FIT_Q_NONE


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


# ── AI 질문의 측정 상태 ─────────────────────────────────────────────────────
# "끝난 회차" — collect_ai 가 runs 에 남기는 흔적으로 가른다 (db.run·collector.Stage):
#   · 정상 종료       finished_at 있음, notes="engines=[..] samples=N errors=K … skipped=S".
#                     항목 실패(errors>0)가 있어도 끝난 회차다 — 실패한 (질문·엔진)은
#                     ai_checks 에 행이 없을 뿐이고, 남은 행은 온전히 받은 답이다
#   · 도중에 끊김     finished_at 있음, notes 에 "중단: <예외>" — db.run 이 예외로 닫을 때
#                     붙인다. 402 잔액·401 키(collector.Fatal)가 여기다. 09-02 의 #56 이
#                     이것이었다: 끊긴 자리의 질문은 엔진 일부만 물었고, 거기까지 못 간
#                     질문은 행이 아예 없다
#   · 프로세스째 죽음 finished_at NULL — 재배포 SIGKILL. store.reclaim_dead_runs 는 sites
#     · 도는 중       표만 고치고 이 행은 그대로 둔다. 도는 중인 것과 겉으로 같다
# 끝난 회차 = finished_at 있음 AND notes 에 AI_RUN_ABORTED 없음. 이것만 측정으로 쓴다.
# 예전엔 "ai_checks 가 가진 가장 큰 run_id"를 최신 회차로 골라, 끊긴 #56 이 최신이 되고
# 거기서 안 닿은 질문 17개가 기회 목록에서 조용히 빠졌다.
AI_RUN_ABORTED = "중단:"   # db.run 이 예외로 닫은 런의 notes 표식 — test_capture 가 실물 db.run 으로 대조
AI_STALE_DAYS = 30         # 질문의 마지막 측정이 이보다 오래되면 "오래됨". 자동 런은 며칠
                           # 간격이라, 한 달 동안 한 번도 안 잰 질문은 상한(max_ai_prompts)
                           # 밖으로 밀렸거나 확인이 계속 끊긴 것이다 — 그 인용 0 은 옛 사실이다


def ai_run_done(r: str = "r") -> str:
    """"이 ai 런은 끝났나"의 SQL 조각 — 측정(ai_prompt_state)과 오늘 건너뛰기
    (collect_ai 의 seen_today)가 같은 판정을 쓴다. 둘이 갈라지면, 끊긴 회차가 오늘
    물은 질문을 재수집이 "이미 확인"으로 건너뛰는데 측정은 그걸 안 쳐서 "측정 안 됨"
    이 다시 눌러도 안 풀린다."""
    return (f"{r}.kind='ai' AND {r}.finished_at IS NOT NULL "
            f"AND instr(COALESCE({r}.notes,''), '{AI_RUN_ABORTED}')=0")


def _ai_run_state(row) -> str:
    """runs 한 줄 → done | aborted | open (open 은 도는 중이거나 프로세스째 죽은 것)."""
    if not row["finished_at"]:
        return "open"
    return "aborted" if AI_RUN_ABORTED in (row["notes"] or "") else "done"


def ai_prompt_state(conn: sqlite3.Connection, project_id: int, *,
                    now: str | None = None) -> dict:
    """켜 둔 질문마다 **끝난 회차의 가장 최근 측정** — ai_gaps 와 [AI 인용] 화면이 같이 쓴다.

    반환:
      rows      [{id, prompt, category, gen_version, aim, run_id, measured_at, state}]
                state: measured | stale(AI_STALE_DAYS 넘음) | unmeasured(끝난 회차에 행 없음)
      active·measured·stale·unmeasured·outdated   개수. outdated 는 gen_prompts.outdated
      last_run  가장 최근 ai 런 {id, state(done|aborted|open), at, errors, note} | None
    now 는 검사용 기준 시각(없으면 지금).
    """
    import gen_prompts            # 판 판정의 정본 — 여기 사본을 두지 않는다
    rows = [dict(r) for r in conn.execute(
        f"""WITH last AS (
              SELECT c.prompt_id, MAX(c.run_id) run_id
                FROM ai_checks c JOIN runs r ON r.id=c.run_id
               WHERE r.project_id=? AND {ai_run_done("r")}
            GROUP BY c.prompt_id)
            SELECT p.id, p.prompt, p.category, p.gen_version, p.aim,
                   l.run_id, r.finished_at measured_at,
                   julianday(COALESCE(?, 'now')) - julianday(r.finished_at) age
              FROM ai_prompts p
              LEFT JOIN last l ON l.prompt_id=p.id
              LEFT JOIN runs r ON r.id=l.run_id
             WHERE p.project_id=? AND p.is_active=1
          ORDER BY p.id""", (project_id, now, project_id))]
    for r in rows:
        age = r.pop("age")
        r["state"] = ("unmeasured" if not r["run_id"]
                      else "stale" if (age or 0) > AI_STALE_DAYS else "measured")
    last = conn.execute(
        "SELECT id, started_at, finished_at, notes FROM runs"
        " WHERE project_id=? AND kind='ai' ORDER BY id DESC LIMIT 1", (project_id,)).fetchone()
    last_run = None
    if last:
        m = re.search(r"errors=(\d+)", last["notes"] or "")
        note = (last["notes"] or "")
        cut = note.find(AI_RUN_ABORTED)
        last_run = {"id": last["id"], "state": _ai_run_state(last),
                    "at": last["finished_at"] or last["started_at"],
                    "errors": int(m.group(1)) if m else 0,
                    # 끊긴 이유만 — 앞의 engines=… 와 예외 이름(Fatal:)은 사람이 읽을 말이 아니다
                    "note": re.sub(r"^\w+:\s*", "",
                                   note[cut + len(AI_RUN_ABORTED):].strip())[:200]
                            if cut >= 0 else ""}
    count = lambda s: sum(1 for r in rows if r["state"] == s)   # noqa: E731
    return {"rows": rows, "active": len(rows),
            "measured": count("measured"), "stale": count("stale"),
            "unmeasured": count("unmeasured"),
            "outdated": sum(1 for r in rows if gen_prompts.outdated(r["gen_version"])),
            "last_run": last_run}


def ai_health(conn: sqlite3.Connection, project_id: int) -> dict:
    """[AI 인용] 화면의 "측정 안 됨·오래됨·구버전" 한 벌 — 페이로드용으로 접은 꼴.

    질문 문장은 앞 몇 개만 싣는다(화면이 "예:"로 보여 줄 만큼). 개수는 전부 센다.
    """
    import gen_prompts
    st = ai_prompt_state(conn, project_id)
    pick = lambda pred: [r["prompt"] for r in st["rows"] if pred(r)][:5]   # noqa: E731
    return {"active": st["active"], "measured": st["measured"], "stale": st["stale"],
            "unmeasured": st["unmeasured"], "outdated": st["outdated"],
            "stale_days": AI_STALE_DAYS, "gen_version": gen_prompts.GEN_VERSION,
            "last_run": st["last_run"],
            "unmeasured_eg": pick(lambda r: r["state"] == "unmeasured"),
            "stale_eg": pick(lambda r: r["state"] == "stale"),
            "outdated_eg": pick(lambda r: gen_prompts.outdated(r["gen_version"]))}


# 제3자 플랫폼 — 브랜드는 자기 사이트보다 커뮤니티·위키·영상·리뷰 사이트로 훨씬 자주
# 인용된다(ai-seo 세 번째 기둥). 대신 인용된 곳이 거기면 "내 페이지를 고쳐라"는 틀린
# 처방이다. 목록의 정본은 config.yaml 의 third_party_platforms 다 — 플랫폼이 늘고
# 주는 것은 데이터 변경이지 코드 변경이 아니다. 읽기 실패면 빈 목록: 그때는 전부
# "경쟁사·일반 사이트"로 보고 예전 처방을 낸다(없는 갈래를 지어내지 않는다).
def third_party_platforms() -> tuple[str, ...]:
    try:
        import collector
        got = collector.config().get("third_party_platforms")
    except Exception:
        got = None
    if not isinstance(got, list):
        return ()
    return tuple(h for h in (host_of(str(x)) for x in got) if h)


def is_third_party(domain: str, platforms=None) -> bool:
    """하위 도메인까지 — ko.wikipedia.org·m.blog.naver.com 도 그 플랫폼이다(owns 규칙)."""
    plats = third_party_platforms() if platforms is None else platforms
    return any(owns(domain, p) for p in plats)


def _rival_key(host: str, platforms) -> str:
    """대신 인용된 곳을 셀 이름 — 제3자 플랫폼이면 플랫폼 이름으로 접는다.

    old.reddit.com·www.reddit.com 이 따로 세지면 "reddit 이 4/5"가 "3/5 + 1/5"로
    쪼개져 가장 잦은 출처가 안 보인다. 둘 이상 걸리면 가장 긴 것(blog.naver.com 이
    naver.com 보다 구체적이다). 플랫폼이 아닌 도메인은 host_of 규칙 그대로 둔다 —
    경쟁사의 blog. 하위 도메인은 따로 선 사이트일 수 있다."""
    hit = [p for p in platforms if owns(host, p)]
    return max(hit, key=len) if hit else host


def ai_is_gap(cited: int | None, checks: int | None) -> bool:
    """이 질문이 인용 공백인가 — 기회를 세우는 쪽(ai_gaps)과 닫는 쪽(_resolve_ai_citation)이
    같은 판정을 쓴다. 둘이 갈라지면 1/6 질문이 이번 load 에서 서고 같은 load 의 수명주기
    판정에서 "인용됨"으로 닫혀, 매 회차 열렸다 닫혔다 한다."""
    if not checks:
        return False
    return (cited or 0) / checks <= AI_GAP_MAX_RATE + 1e-9


def ai_cite_label(cited: int | None, checks: int | None) -> str:
    """"인용 1/6 (n=6)" — 기회 근거(reasoning)와 요청문이 같은 글로 말한다."""
    n = checks or 0
    s = f"인용 {cited or 0}/{n} (n={n})"
    return s + (" · 표본 부족" if n < AI_MIN_SAMPLES else "")


def _excerpt(text: str) -> str:
    t = re.sub(r"\s+", " ", (text or "")).strip()
    return t if len(t) <= AI_EXCERPT_CHARS else t[:AI_EXCERPT_CHARS - 1].rstrip() + "…"


def _rival_list(tally: dict[str, int], platforms, top: int) -> list[dict]:
    return [{"domain": d, "n": n, "third_party": is_third_party(d, platforms)}
            for d, n in sorted(tally.items(), key=lambda x: (-x[1], x[0]))[:top]]


def ai_tally(conn: sqlite3.Connection, run_id: int,
             prompt_ids: list[int] | None = None, *, platforms=None) -> dict[int, dict]:
    """한 회차의 질문별 집계 — "대신 인용된 곳"·엔진별 수·발췌의 **정본**.

    예전에는 두 벌이었다: 화면(dashboard._axis_ai)은 `MAX(cited_domains_json)` 로 표본
    하나(사전순 최대 JSON — 사실상 무작위)를 골랐고, 기회(ai_gaps)는 전 표본을 셌다.
    요청문은 화면 쪽을 읽었으니 틀린 쪽을 읽은 셈이다. 이제 둘 다 이것을 부른다.

    반환 {prompt_id: {...}}:
      checks·cited·mentioned·named_only(이름은 나오고 링크는 없음)
      recommended  추천 목록 줄에 든 답변 수 — 이 칸을 잰 답이 하나도 없으면 None(안 봤다).
                   rec_checks 가 그 분모다(옛 행은 NULL 이라 분모에서 빠진다)
      engines      엔진 이름 목록(정렬)
      misses       우리가 인용되지 않은 답변 수 — rivals 의 분모
      rivals       [{domain, n, third_party}] 우리가 빠진 답변에서 대신 인용된 곳, 횟수 순
      third_share  rivals 횟수 중 제3자 플랫폼 몫(0~1). 대신 인용된 곳이 없으면 None
      lean         third_party(제3자가 대부분) | sites(경쟁사·일반 사이트) | None
      excerpts     {engine: 발췌} 엔진마다 우리가 빠진 답변 중 가장 먼저 받은 것(id 순) —
                   무작위가 아니라 결정적이다. 빠진 답이 없는 엔진은 없다
      by_engine    {engine: {checks, cited, mentioned, named_only, recommended,
                             rec_checks, misses, rivals}}
    """
    plats = third_party_platforms() if platforms is None else platforms
    sql = ("SELECT prompt_id, engine, cited, mentioned, recommended, cited_domains_json,"
           " answer_excerpt FROM ai_checks WHERE run_id=?")
    args: list = [run_id]
    if prompt_ids is not None:
        if not prompt_ids:
            return {}
        sql += f" AND prompt_id IN ({','.join('?' * len(prompt_ids))})"
        args += list(prompt_ids)
    acc: dict[int, dict] = {}
    for c in conn.execute(sql + " ORDER BY id", args):
        a = acc.setdefault(c["prompt_id"], {"eng": {}, "tally": {}, "excerpts": {}})
        e = a["eng"].setdefault(c["engine"], {"checks": 0, "cited": 0, "mentioned": 0,
                                              "named_only": 0, "recommended": 0,
                                              "rec_checks": 0, "misses": 0, "tally": {}})
        e["checks"] += 1
        e["cited"] += int(bool(c["cited"]))
        e["mentioned"] += int(bool(c["mentioned"]))
        e["named_only"] += int(bool(c["mentioned"]) and not c["cited"])
        if c["recommended"] is not None:            # NULL = 이 칸 이전의 옛 답 — 안 봤다
            e["rec_checks"] += 1
            e["recommended"] += int(bool(c["recommended"]))
        if c["cited"]:
            continue
        e["misses"] += 1
        try:
            doms = {_rival_key(h, plats) for h in (host_of(str(d)) for d in
                    json.loads(c["cited_domains_json"] or "[]")) if h}
        except (TypeError, ValueError):
            doms = set()
        for h in doms:                              # 답변 하나에 한 번 — "4/5" 의 분자
            e["tally"][h] = e["tally"].get(h, 0) + 1
            a["tally"][h] = a["tally"].get(h, 0) + 1
        if c["engine"] not in a["excerpts"] and (c["answer_excerpt"] or "").strip():
            a["excerpts"][c["engine"]] = _excerpt(c["answer_excerpt"])
    out = {}
    for pid, a in acc.items():
        by = {}
        for name, e in sorted(a["eng"].items()):
            t = e.pop("tally")
            by[name] = {**e, "rivals": _rival_list(t, plats, 3),
                        "recommended": e["recommended"] if e["rec_checks"] else None}
        tot = lambda k: sum(e[k] or 0 for e in by.values())  # noqa: E731
        rivals = _rival_list(a["tally"], plats, AI_RIVALS_TOP)
        hits = sum(a["tally"].values())
        third = sum(n for d, n in a["tally"].items() if is_third_party(d, plats))
        share = round(third / hits, 3) if hits else None
        rec_checks = tot("rec_checks")
        out[pid] = {"checks": tot("checks"), "cited": tot("cited"),
                    "mentioned": tot("mentioned"), "named_only": tot("named_only"),
                    "recommended": tot("recommended") if rec_checks else None,
                    "rec_checks": rec_checks, "misses": tot("misses"),
                    "engines": list(by), "rivals": rivals, "third_share": share,
                    "lean": (None if share is None else
                             "third_party" if share > AI_PRESENCE_SHARE else "sites"),
                    "excerpts": a["excerpts"], "by_engine": by}
    return out


def ai_rivals_text(rivals: list[dict] | None, misses: int | None, top: int = 3) -> str:
    """"reddit.com 4/5, rival.com 2/5" — 기회 근거와 요청문·화면이 같은 모양으로 쓴다."""
    return ", ".join(f"{r['domain']} {r['n']}/{misses or 0}" for r in (rivals or [])[:top])


def ai_gaps(conn: sqlite3.Connection, project_id: int, *,
            limit: int = 30) -> list[dict]:
    """챗봇이 나를 거의 출처로 쓰지 않는 질문 — 질문마다 끝난 회차의 최신 측정 기준.

    ai_checks 는 엔진×표본마다 한 줄이라 질문 단위로 접어야 판정이 성립한다. 판정은
    ai_is_gap — 인용률이 AI_GAP_MAX_RATE 이하. 표본이 AI_MIN_SAMPLES 보다 적어도
    올리되 thin 을 달아 점수를 깎고 근거에 "표본 부족"을 적는다.

    끝난 회차에서 한 번도 안 잰 질문은 여기 없다 — 그건 "인용 0"이 아니라 "모름"이다.
    대신 ai_prompt_state 가 그 개수를 세어 화면이 "측정 안 됨"으로 말한다(조용히 빼지
    않는다). 오래된 측정(stale)은 그대로 쓰되 measured_at 을 실어 근거에 날짜가 붙는다.
    """
    measured = [p for p in ai_prompt_state(conn, project_id)["rows"] if p["run_id"]]
    by_run: dict[int, list[int]] = {}
    for p in measured:
        by_run.setdefault(p["run_id"], []).append(p["id"])
    plats = third_party_platforms()
    tallies = {(rid, pid): t for rid, ids in by_run.items()
               for pid, t in ai_tally(conn, rid, ids, platforms=plats).items()}
    rows = []
    for p in measured:
        t = tallies.get((p["run_id"], p["id"]))
        if not t or not ai_is_gap(t["cited"], t["checks"]):
            continue
        rows.append({**t, "id": p["id"], "prompt": p["prompt"], "category": p["category"],
                     "engines": len(t["engines"]), "engine_names": t["engines"],
                     "cite_rate": round(t["cited"] / t["checks"], 3),
                     "thin": t["checks"] < AI_MIN_SAMPLES,
                     "run_id": p["run_id"], "measured_at": p["measured_at"],
                     "stale": p["state"] == "stale"})
    # 상한(limit) 안에 무엇을 남기나 — 표본이 되는 것 먼저, 그다음 많이 물어본 것.
    # 점수 순서는 load() 가 score() 로 따로 정한다.
    rows.sort(key=lambda x: (x["thin"], -x["checks"], x["id"]))
    return rows[:limit]


# ── 검색 × AI 교차 ───────────────────────────────────────────────────────────
# 두 축(gsc_snapshots·ai_checks)은 여태 서로를 한 번도 안 봤다. 잇는 다리는 "같은
# 주제인가" 하나뿐인데, AI 질문은 문장이고 검색어는 낱말 뭉치라 문자열로는 절대
# 만나지 않는다 — 토큰 겹침으로 잰다. 휴리스틱이라 임계를 여기 이름 붙여 둔다.
XAI_MIN_TOKEN = 2      # 한 글자 토큰은 조사·수사(이·그·앱·툴)라 겹쳐도 뜻이 없다
XAI_MIN_OVERLAP = 2    # 한 낱말만 겹치면("가격") 아무 질문이 아무 검색어에나 붙는다
XAI_LIMIT = 12         # 화면이 읽는 목록이지 전수 목록이 아니다


def _xai_tokens(s: str) -> set[str]:
    return {t for t in tokens(s) if len(t) >= XAI_MIN_TOKEN}


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
        # "AI 는 지는" 의 기준은 기회와 같은 판정이다(ai_is_gap). 인용 0회로만 거르면
        # 6번 중 1번 인용된 질문이 기회로는 서는데 이 표에서는 빠져, 두 자리가 같은
        # 질문을 두고 다른 말을 한다.
        if not ai_is_gap(r.get("cited"), r.get("checks")):
            continue
        m = _xai_match(r.get("prompt") or "", tops)
        if not m:
            continue
        rows.append({"prompt": r["prompt"], "category": r.get("category") or "",
                     "query": m["query"], "pos": m["pos"], "imp": m["imp"],
                     "checks": r.get("checks") or 0, "cited": r.get("cited") or 0,
                     # 대신 인용된 곳은 ai_tally 가 센 것 그대로(한 벌) — 횟수 순 상위 셋
                     "rivals": [x["domain"] for x in (r.get("rivals") or [])][:3]})
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


def value_mult(metrics: dict) -> float:
    """GA4 전환 신호로 raw 점수를 보정하는 승수 — score() 가 raw *= 로 곱한다.

    metrics 에 ga4_sessions·ga4_key_events 가 없으면(GA4 미연결, 또는 이 kind 에
    페이지 개념이 없어 애초에 안 채움) **정확히** 1.0을 리터럴로 돌려준다(반올림으로도
    안 새게). 표본(페이지 클릭·GA4 세션)이 GA4_NOCONV_MIN_* 하한 미만이어도 1.0 —
    표본이 될 때만 판단한다. 표본이 되면: 전환이 있으면 전환율에 비례해
    GA4_MULT_HI 까지 올리고(포화 후 고정), 전환이 0으로 확인되면 GA4_MULT_LO 로 내린다.
    """
    sessions, key_events = metrics.get("ga4_sessions"), metrics.get("ga4_key_events")
    if sessions is None or key_events is None:
        return 1.0
    clicks = metrics.get("ga4_clicks")
    if clicks is None or clicks < GA4_NOCONV_MIN_CLICKS or sessions < GA4_NOCONV_MIN_SESSIONS:
        return 1.0
    if key_events <= 0:
        return GA4_MULT_LO
    rate = min(key_events / sessions, GA4_MULT_SATURATE_RATE) / GA4_MULT_SATURATE_RATE
    return round(1.0 + rate * (GA4_MULT_HI - 1.0), 4)


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
                           1.0 if kind in ("ai_citation_gap", "aio_exposure",
                                           "ai_bot_blocked") else 0.0))
    # 챗봇 인용률(ai_citation_gap 의 cite_rate). 이 kind 에는 순위가 없어 reach 가 늘
    # 보수적 0.3 이었다 — 그런데 인용률이 바로 "닿는 거리"다: 한 번이라도 인용됐으면
    # 엔진이 이미 우리 페이지를 찾아 출처로 쓸 줄 안다는 뜻이라 끌어올리기가 가장 싸다.
    # 대신 남은 몫(1 - 인용률)만큼만 AI 노출 신호를 준다 — 이미 걸리는 몫은 기회가 아니다.
    # 둘이 맞서서 1/6 과 0/6 이 비슷한 자리에 선다: 0/6 은 벌 게 크고, 1/6 은 싸다.
    rate = metrics.get("cite_rate")
    if rate is not None:
        reach = 0.3 + 0.7 * min(1.0, float(rate) / AI_GAP_MAX_RATE)
        ai *= 1.0 - float(rate)
    raw = (w["w_demand"] * demand + w["w_reach"] * reach
           + w["w_fit"] * fit + w["w_ai"] * ai)
    raw *= value_mult(metrics)
    # 표본 부족(답변 AI_MIN_SAMPLES 미만) — 올리되 깎는다. 답이 매번 달라서 두어 번
    # 빠진 것은 추세가 아니다.
    if metrics.get("thin"):
        raw *= AI_THIN_MULT
    return round(min(100.0, max(0.0, raw * 100)), 1)


# 기회 종류 명부 — 한 행이 "어떻게 찾고(detect) · score()에 뭘 넘기고(metrics) ·
# 무엇을 대상(target)이라 부르고 · 근거 문장을 어떻게 쓰는지(reasoning)"를 다 쥔다.
# load() 는 이 명부를 순회할 뿐 kind 문자열을 직접 적지 않는다 — 명부에 없는 kind 는
# 나올 수가 없다(구조적으로), 명부에 있는데 빠지는 kind 도 없다(전부 돈다).
#
# ALL_KINDS(위)가 이름·순서의 정본이다. KINDS 는 그것을 그대로 따라가며
# _KIND_SPECS 에서 나머지를 채운 파생값이고(반대 방향이 아니라 이 방향인 이유:
# 이름만 읽으면 되는 쪽 — brief·db·검사 — 이 명부 전체를 짓지 않고도 import 로
# 끝낼 수 있다), DEFENSIVE_KINDS 는 KINDS 에서 파생된다.
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
# see=(화면 id, 섹션 id) — 이 종류의 검색어를 자세히 보는 자리. [개요]의 기회 줄이
# 거기로 가는 링크를 단다(개요는 여러 화면을 모아 보는 곳이라, 모은 줄마다 원래 자리가
# 있어야 한다). 따로 보는 화면이 없는 종류는 None — 링크를 안 단다. 화면·섹션 id 가
# 뷰 파일에 실제로 있는지는 test_seams 가 본다.
_Kind = namedtuple("_Kind", "name label defensive detect metrics target reasoning play see",
                   defaults=(None,))


def _ga4_metrics(ctx: dict, *, query: str | None = None, page: str | None = None) -> dict:
    """검색어(또는 이미 아는 페이지)에서 GA4 조각을 뽑아 metrics 에 얹는다.

    GA4 미연결(ctx['ga4']가 비어 있음)이면 빈 dict — value_mult() 가 그걸 보고
    정확히 1.0을 곱한다. page 를 모르면 pages_by_query 로 그 검색어의 1등 페이지를
    찾는다 (cannibalization 처럼 detect() 가 이미 페이지를 아는 kind 는 page= 로
    바로 넘겨 이 조회를 건너뛴다). 표본 게이트(클릭·세션 하한)는 value_mult() 몫이다
    — 여기는 신호만 옮긴다.
    """
    if not ctx.get("ga4"):
        return {}
    if page is None:
        if not query:
            return {}
        rows = pages_by_query(ctx["conn"], ctx["pid"], [query], top=1, at=ctx["cur"]).get(query)
        if not rows:
            return {}
        page = rows[0]["page"]
    g = ctx["ga4"].get(urlsplit(page).path)
    if not g:
        return {}
    pg = (ctx.get("page_agg") or {}).get(page)
    return {"ga4_clicks": pg["clk"] if pg else None,
            "ga4_sessions": g["sessions"], "ga4_key_events": g["key_events"]}


def _coverage_rows(ctx: dict) -> list[dict]:
    """coverage() 는 클러스터별 dict 하나를 주지, per-row 목록을 안 준다 — 여기서 편다."""
    cov = coverage(ctx["conn"], ctx["pid"])
    cov_vol = cov.get("volume_by_cluster") or {}
    return [{"cluster": cl, "n": n, "vol": cov_vol.get(cl, 0)}
            for cl, n in sorted(cov["by_cluster"].items(), key=lambda x: -x[1])]


def _reason_backlink_prospect(r: dict, ctx: dict) -> str:
    tg = [t.strip() for t in (r["targets"] or "").split(",")[:2] if t.strip()]
    s = f"경쟁사 {r['hits']}곳이 여기서 링크를 받는데 나는 못 받았습니다"
    if tg:
        s += f". 링크가 걸린 곳: {', '.join(tg)}"
    if r["rank"]:
        s += f" (도메인 지수 {r['rank']})"
    return s


# striking_distance 는 4~20위 한 kind 를 밴드 둘(band: page1/page2)로 갈라 처방한다.
# "what" 뒤에 실제로는 옛 화면에서 sdAvgNote()(평균 게재순위 안내 + [순위] 화면
# 버튼)가 이어 붙었다 — 그 버튼(window.go)은 화면에서만 만들 수 있어 여기 텍스트에는
# 없다. dashboard.html 의 oppDetail 이 이어 붙인다(둘 다 같은 문장을 뒤에 단다).
_SD_PLAY = {
    "page1": dict(
        what="이미 1페이지 안입니다. 여기서 남은 것은 순위가 아니라 클릭입니다.",
        acts=["title 이 이 페이지에 걸린 검색어 묶음의 주 의도를 말하는지 보고, 숫자·연도를 붙여 "
              "옆 결과와 다르게 보이게 합니다. 검색어를 글자 그대로 박지 않습니다.",
              "meta description 을 검색 의도에 대한 한 문장 답으로 바꿉니다.",
              "질문 바로 아래 40~60자 직답 블록을 둡니다. 강조 스니펫이 거기서 나옵니다.",
              "상단 3위권을 노린다면 상위 페이지에만 있는 구간을 본문에 채웁니다."],
        # 1페이지 안에서 클릭이 안 나는 이유는 순위가 아니다 — 가설부터 가르게 한다.
        # 이 표가 없을 때 3.6~5.8위·클릭 0 페이지의 요청문이 순위 올리기만 시켰다.
        deliver=["클릭이 안 나는 이유 가설 표: 가설(검색결과에 보이는 title·설명이 안 눌림 · "
                 "검색 의도 어긋남 — 상위 결과가 설명 글인지 업체·가격인지 · 검색결과 기능이 클릭을 "
                 "가져감 — 지도·광고·AI 요약) | 확인한 것(검색결과를 직접 열어) | 맞나",
                 "새 title 3안. 묶음의 주 의도를 앞에 두고 숫자나 연도를 붙여서 — 지금 title 이 "
                 "이미 그 말을 하면 '안 바꿈'과 이유",
                 "meta description 2안",
                 "질문 바로 아래 넣을 40~60자 직답 문안"]),
    "page2": dict(
        what="1페이지 진입까지 몇 칸 남았습니다. 그 몇 칸이 클릭의 대부분입니다.",
        acts=["아래 페이지의 title 과 H1 이 이 검색어 묶음이 묻는 것을 정면으로 말하게 고칩니다 — "
              "본문이 실제로 다루는 말로, 검색어를 그대로 박는 게 아니라.",
              "상위 페이지가 답하는 하위 질문 중 빠진 것을 본문에 채웁니다.",
              "이미 순위가 있는 다른 글에서 이 페이지로 내부 링크를 겁니다.",
              "표·목록·정의 블록을 만들어 스니펫 후보로 올립니다."],
        deliver=["새 title 3안과 H1 문안. 묶음의 주 의도를 앞에 두고 — 안 바꾸는 게 답이면 그렇게 쓰고 이유",
                 "본문에 추가할 H2 목록(상위 페이지가 답하는데 내게 없는 질문)",
                 "이 페이지로 내부 링크를 걸 글과 앵커 텍스트 3개",
                 # 할 일 4번(스니펫 블록)에 짝이 되는 산출물이 없었다
                 "스니펫 후보 블록 하나(표·목록·정의 중) 문안과 넣을 H2 자리"]),
}

# content_gap 은 "아예 없다"(missing)와 "밀린다"(weak)를 한 kind 로 담는다 — 할 일이
# 정반대다(새로 쓴다 vs 있는 걸 고친다). 갈래는 content_gaps()가 낸 원본 행의
# kind('missing'/'weak')가 안다.
_CG_PLAY = {
    "weak": dict(
        what="경쟁 도메인이 나보다 위에 있습니다. 페이지는 이미 있으니 새로 쓰지 말고 그 페이지를 고칩니다.",
        acts=["나를 이기고 있는 그 페이지의 목차와 내 페이지를 나란히 놓고 빠진 구간을 찾습니다.",
              "title 과 H1 이 그 검색어의 의도를 정면으로 말하는지 보고, 아니면 본문이 다루는 말로 고칩니다.",
              "내 제품·데이터로만 말할 수 있는 구간을 하나 더 넣습니다. 같은 말을 길게 늘리는 건 소용없습니다.",
              "이미 순위가 있는 다른 글에서 이 페이지로 내부 링크를 겁니다."],
        deliver=["내 페이지에 추가할 H2 목록. 이기고 있는 페이지와 비교해서",
                 "새 title 3안과 H1 문안", "이 페이지로 내부 링크를 걸 글과 앵커 텍스트 3개"]),
    "missing": dict(
        what="경쟁 도메인은 잡고 있는데 나는 페이지 자체가 없는 검색어입니다.",
        acts=["상위 3개 페이지의 목차를 훑어 다뤄야 할 구간을 정합니다.",
              "내 제품·데이터로만 말할 수 있는 구간을 하나 넣습니다.",
              "발행 후 관련 글에서 내부 링크를 겁니다."],
        deliver=["이 검색어를 정면으로 다루는 새 글의 제목·목차", "내 제품·데이터로만 쓸 수 있는 구간 하나"]),
}

# aio_exposure(구글 AI 요약에 내 링크 없음)는 우리 순위로 처방이 갈린다 — band 는
# aio_band() 가 정한다. 예전 처방은 "직답 블록 + Article·FAQ 구조화 데이터"였는데
# 구글 공식 입장과 반대였다: 구글 AI 요약(AI Overviews)은 AI 전용 마크업·파일이
# 필요 없고, AI 용으로 콘텐츠를 조각낼 필요도 없으며, 출처를 핵심 순위 시스템에서
# 고른다(Search Central "AI features and your website"). 그래서 40위인 검색어에
# 필요한 것은 직답 블록이 아니라 순위다. FAQ 구조화 데이터를 뺀 근거가 하나 더
# 있다 — 구글은 2023년 8월부터 FAQ 리치 결과를 잘 알려진 정부·보건 권위 사이트로
# 제한했다. 대부분의 사이트에서 그 마크업은 검색 결과에 아무것도 더하지 않는다.
# 챗봇 인용(ai_citation_gap)은 다른 엔진이라 이 판단을 따르지 않는다.
_AIO_PLAY = {
    "page1": dict(
        what="이미 1페이지 안인데 구글 AI 요약이 우리 대신 다른 곳을 인용합니다. 구글 AI "
             "요약은 순위 시스템이 고른 상위 페이지에서 출처를 뽑고, AI 전용 마크업이나 "
             "파일이 필요 없습니다. 남은 것은 사람이 읽기에 더 나은 글입니다.",
        acts=["요약이 대신 인용한 곳이 답하는데 우리 글에는 없는 답을 찾아, 사람이 읽는 "
              "문단으로 채웁니다.",
              "제목과 H2 가 질문과 답의 흐름을 따르게 구조를 분명히 합니다. 한 문단에는 "
              "한 가지만 말합니다.",
              "누가 썼는지, 직접 해 보거나 겪은 것, 근거 출처, 마지막 수정일을 드러냅니다(E-E-A-T).",
              "AI 용으로 글을 조각내거나 따로 페이지를 만들지 않습니다. AI 전용 마크업·파일도 "
              "구글 AI 요약에는 필요 없습니다.",
              "스니펫을 막고 있지 않은지 봅니다. nosnippet·max-snippet 은 AI 요약에 인용되는 "
              "것도 막습니다."],
        deliver=["요약이 대신 인용한 곳이 답하는데 우리 글에 없는 답 목록과, 그걸 채울 H2·문단 계획",
                 "제목·H2 구조 수정안. 지금 것과 고친 것을 나란히",
                 "이 글에 빠진 신뢰 신호(저자·직접 경험·출처·수정일)와 넣을 자리"]),
    "beyond": dict(
        what="구글이 이 검색어에 AI 요약을 붙이는데 우리는 1페이지 밖이거나 순위가 없습니다. "
             "구글 AI 요약은 상위 페이지에서 출처를 뽑습니다 — 순위가 먼저입니다. 직답 "
             "블록이나 구조화 데이터로 요약에 끼어드는 길은 없습니다.",
        acts=["이 검색어로 1페이지에 드는 것을 목표로 잡습니다. 걸린 페이지가 있으면 그 "
              "페이지를, 없으면 이 검색어를 정면으로 다루는 글을 씁니다.",
              "상위 페이지가 답하는데 우리에게 없는 질문을 채웁니다. 검색결과 상위와 구글이 "
              "함께 보여 준 질문이 그 재료입니다.",
              "이미 순위가 있는 다른 글에서 이 페이지로 내부 링크를 겁니다.",
              "AI 용 조각 글이나 AI 전용 마크업을 따로 만들지 않습니다. 순위를 올리는 일이 "
              "곧 AI 요약에 드는 길입니다."],
        # 손질안 셋만 시키면 48위 페이지에도 title·H1·H2 손질이 답이 된다 — 왜 밀리는지
        # 가르고 "이 페이지로는 어렵다"고 말할 자리를 맨 앞에 둔다.
        deliver=["왜 밀리는지 원인 진단 표: 항목(검색 의도와 페이지 종류 · 다루는 깊이 · 신뢰 "
                 "신호 · 외부 링크 · 내부 링크) | 우리 페이지 | 상위 2~3개 | 차이. 모르는 칸은 "
                 "[확인 필요]. 끝에 결론 한 줄 — 이 페이지를 고쳐 1페이지를 노릴 수 있다 / 이 "
                 "페이지로는 어렵다(그러면 새 글·외부 링크·다른 검색어 중 무엇을)",
                 "1페이지에 들기 위해 채울 H2 목록. 상위 페이지가 답하는데 우리에게 없는 질문",
                 "새 title 과 H1 문안. 묶음의 주 의도를 앞에 두고 — 안 바꾸는 게 답이면 그렇게 쓰고 이유",
                 "이 페이지로 내부 링크를 걸 글과 앵커 텍스트 3개 — 지금 들어오는 링크가 없는 "
                 "글에서, 지금 앵커와 서로 겹치지 않는 말로"]),
}
AIO_BANDS = tuple(_AIO_PLAY)


def aio_band(position) -> str:
    """구글 AI 요약 기회의 처방 갈래 — 우리 순위가 1페이지(PAGE1) 안이면 page1.

    순위 없음(None: 조회 깊이 안에 우리가 없다)은 beyond 다 — 모르는 게 아니라
    "안 보였다"는 측정이다."""
    return "page1" if position is not None and position <= PAGE1 else "beyond"


_KIND_SPECS = {
    "striking_distance": dict(
        see=("keywords", "log"),
        label="밀면 오를 검색어", defensive=False,
        detect=lambda ctx: striking(ctx["conn"], ctx["pid"], ctx["cur"], brands=ctx["brands"]),
        metrics=lambda r, ctx: {"impressions": r["imp"], "position": r["pos"],
                                 "fit": _fit_of(ctx["conn"], ctx["pid"], r["query"]),
                                 **_ga4_metrics(ctx, query=r["query"])},
        target=lambda r, ctx: r["query"],
        # 이 kind 는 4~20위를 잡고 band 로 갈린다. 4~10위는 이미 1페이지라
        # "1페이지까지 0.0"이라는 문장이 뜻을 잃는다 — 밴드마다 다르게 말한다.
        # "12.8위"는 실측 순위로 읽힌다 — 직접 검색해도 안 보인다는 문의가
        # 여기서 나왔다. GSC 가 보고한 기간 평균이라고 앞에서 못 박는다.
        reasoning=lambda r, ctx: (
            f"평균 {r['pos']}위 · 노출 {r['imp']:,} · 클릭 {r['clk']:,}. "
            + (f"이미 1페이지이고 상단 3위권까지 {round(max(0.0, r['pos'] - 3), 1)}칸"
               if r["band"] == "page1" else f"1페이지까지 {r['gap']}칸")
            + f" 남았습니다 (구글 실적 {ctx['cur']} 기준)"),
        play=_SD_PLAY),
    "ctr_gap": dict(
        see=("analysis", "an-ctr"),
        label="클릭률 미달", defensive=False,
        detect=lambda ctx: ctr_gaps(ctx["conn"], ctx["pid"]),
        metrics=lambda r, ctx: {"impressions": r["impressions"], "position": r["position"],
                                 "fit": _fit_of(ctx["conn"], ctx["pid"], r["query"]),
                                 **_ga4_metrics(ctx, query=r["query"])},
        target=lambda r, ctx: r["query"],
        reasoning=lambda r, ctx: (
            f"{r['position']}위 · 노출 {r['impressions']:,} · CTR "
            f"{r['actual_ctr']}% (이 순위의 기대치 {r['expected_ctr']}%). "
            f"이 기간에 약 {r['lost_clicks']:,}클릭을 놓쳤습니다 "
            f"(구글 실적 {ctx['cur']} 기준)"),
        play=dict(
            what="1페이지인데 클릭률이 기대치의 절반도 안 됩니다. 순위가 아니라 제목·설명 문제입니다.",
            acts=["title 앞 60자 안에서 검색 의도에 바로 답하고, 브랜드명은 뒤로 밉니다.",
                  "meta description 에 숫자·연도·구체적 이득을 적습니다.",
                  "FAQ·HowTo 스키마로 검색 결과에서 차지하는 면적을 넓힙니다.",
                  "검색 의도와 제목이 어긋나 있지 않은지 확인합니다(정보형 검색에 판매 제목)."],
            deliver=["새 title 3안. 길이 기준 안에서, 검색 의도를 앞에",
                     "meta description 2안. 길이 기준 안에서",
                     "검색 결과 면적을 넓힐 FAQ·HowTo 구조화 데이터(JSON-LD)"])),
    "cannibalization": dict(
        label="내부 경쟁", defensive=True,
        detect=lambda ctx: cannibalization(ctx["conn"], ctx["pid"]),
        metrics=lambda r, ctx: {"impressions": r["impressions"],
                                 "position": r["pages"][0]["position"],
                                 "fit": _fit_of(ctx["conn"], ctx["pid"], r["query"]),
                                 **_ga4_metrics(ctx, page=r["pages"][0]["page"])},
        target=lambda r, ctx: r["query"],
        reasoning=lambda r, ctx: (
            f"페이지 {len(r['pages'])}개가 노출 {r['impressions']:,}을 나눠 갖습니다: "
            f"{' vs '.join(pg['page'] for pg in r['pages'][:2])} "
            f"(구글 실적 {ctx['cur']} 기준)"),
        play=dict(
            what="같은 검색어에 내 페이지가 둘 이상 걸려 노출을 나눠 갖습니다. 구글이 어느 쪽을 올릴지 못 정합니다.",
            acts=["아래 표에서 노출·클릭이 가장 큰 페이지를 정본으로 정합니다.",
                  "나머지는 정본으로 301 리다이렉트하거나 canonical 을 정본으로 겁니다.",
                  "합칠 수 없으면 검색 의도를 갈라 제목·H1 을 서로 다르게 씁니다.",
                  "나머지 페이지에서 정본으로 내부 링크를 겁니다."],
            deliver=["어느 페이지를 정본으로 할지와 그 근거(노출·클릭·의도 기준)",
                     "나머지 페이지 처리 계획: 301 리다이렉트 대상과 canonical 지정",
                     "합칠 경우 병합 후 목차 한 벌. 새 글을 쓰는 게 아니라 두 글을 합칩니다"])),
    # 내부 경쟁의 거울이다: 저쪽은 "한 검색어를 여러 페이지가 나눠 갖는다"고
    # 이쪽은 "한 페이지가 여러 의도를 떠안는다"다. 만드는 쪽이 저쪽만 있어서 이 리포는
    # "합쳐라"는 말할 수 있고 "갈라라"는 못 했다 — 요청문의 fix_page 는 검색어가 몇
    # 개든 title 한 벌이 전부를 맡으라고만 시킨다. 의도가 갈린 페이지에서 그 말은 틀렸다.
    "intent_split": dict(
        label="한 페이지에 두 의도", defensive=False,
        detect=lambda ctx: intent_split(ctx["conn"], ctx["pid"], at=ctx["cur"]),
        # 되찾을 몫은 2위 의도의 노출이다 — 페이지 전체 노출을 쓰면 이미 잘 걸리는
        # 주된 묶음까지 기회 점수에 얹혀 내부 경쟁보다 늘 위로 선다.
        metrics=lambda r, ctx: {"impressions": r["secondary_impressions"], "position": None,
                                 "fit": _fit_of(ctx["conn"], ctx["pid"], r["page"]),
                                 **_ga4_metrics(ctx, page=r["page"])},
        target=lambda r, ctx: r["page"],
        reasoning=lambda r, ctx: (
            f"'{r['primary']}' 검색어로 주로 걸리는 페이지인데(노출 {r['primary_impressions']:,}), "
            f"'{r['secondary']}' 검색어 {len(r['secondary_queries'])}개가 노출 "
            f"{r['secondary_impressions']:,}으로 같이 걸려 있습니다 "
            f"(구글 실적 {ctx['cur']} 기준)"),
        play=dict(
            what="한 페이지가 검색 의도 둘을 떠안고 있습니다. 적은 쪽 묶음은 이 페이지가 "
                 "답하려던 것이 아닌데도 여기로 들어옵니다.",
            acts=["두 묶음이 정말 다른 답을 원하는지 먼저 봅니다 — 같은 답이면 나누지 않습니다.",
                  "나눈다면 적은 쪽 묶음을 맡을 지면을 새로 세우고, 지금 페이지는 주된 묶음만 맡습니다.",
                  "두 지면이 서로를 가리키는 내부 링크를 걸어 넘어갈 길을 냅니다.",
                  "떼어낸 지면이 남는 페이지의 검색어를 다시 노리지 않게 제목·H1 을 갈라 둡니다.",
                  "몇 주 뒤 떼어낸 검색어가 새 지면으로 옮겨 갔는지, 남은 쪽 순위가 안 떨어졌는지 봅니다."],
            deliver=["나눌지 말지의 결정과 그 근거 한 줄 — 안 나누는 것도 답입니다",
                     "나눈다면 검색어 분배 표: 남길 것 / 떼어낼 것, 줄마다 이유 한 줄",
                     "떼어낼 지면의 제목 3안과 목차(H2 목록). 본문은 안 씁니다",
                     "두 지면을 잇는 내부 링크: 앵커 문장과 넣을 자리"])),
    "rank_decay": dict(
        see=("keywords", "movers"),
        label="순위 하락", defensive=True,
        detect=lambda ctx: rank_decay(ctx["conn"], ctx["pid"]),
        metrics=lambda r, ctx: {"impressions": r["imp"], "position": r["pos"],
                                 "fit": _fit_of(ctx["conn"], ctx["pid"], r["query"]),
                                 **_ga4_metrics(ctx, query=r["query"])},
        target=lambda r, ctx: r["query"],
        reasoning=lambda r, ctx: (
            f"{r['prev_pos']}위에서 {r['pos']}위로 {r['dpos']}칸 밀렸습니다. "
            f"같은 기간 클릭은 {abs(r['dclk'])}회 "
            f"{'줄었' if r['dclk'] < 0 else '늘었'}습니다 "
            f"(구글 실적 {ctx['prev']}과 {ctx['cur']} 비교)"),
        play=dict(
            what="잡고 있던 순위가 밀렸습니다. 새로 만드는 것보다 되찾는 쪽이 쌉니다.",
            acts=["그 페이지에 최근 무엇이 바뀌었는지 봅니다(내용 삭제·리다이렉트·템플릿 교체).",
                  "지금 그 자리를 가져간 페이지와 목차를 비교해 빠진 구간을 채웁니다.",
                  "본문을 실제로 갱신합니다. 날짜만 바꾸는 것은 효과가 없습니다.",
                  "그 페이지로 오던 내부 링크가 끊겼는지 확인합니다."],
            deliver=["되찾으려고 본문에 채울 구간(H2 목록). 지금 그 자리를 가져간 페이지와 비교해서",
                     "끊긴 내부 링크를 어디서 다시 걸지"])),
    "pseo_pattern": dict(
        label="템플릿 패턴", defensive=False,
        detect=lambda ctx: pseo_candidates(ctx["conn"], ctx["pid"], ctx["cur"], limit=10),
        metrics=lambda r, ctx: {"impressions": r["imp"], "position": r["pos"],
                                 "fit": _fit_of(ctx["conn"], ctx["pid"], r["query"])},
        target=lambda r, ctx: r["query"],
        reasoning=lambda r, ctx: (
            f"노출 {r['imp']:,} · CTR {r['ctr_pct']}% · {r['pos']}위. "
            f"같은 꼴로 여러 장 찍을 후보입니다 (구글 실적 {ctx['cur']} 기준)"),
        play=dict(
            what="같은 꼴의 검색어가 무리로 있습니다. 한 장씩 쓰지 말고 템플릿으로 찍을 자리입니다.",
            acts=["같은 패턴의 검색어를 모읍니다(도구별·지역별·비교 축).",
                  "템플릿 한 벌과 실제 데이터로 페이지를 찍습니다. 빈 껍데기는 색인에서 걸러집니다.",
                  "허브 페이지를 만들어 전부 링크합니다."],
            deliver=["이 패턴의 축과 값 목록(도구·지역·비교 대상)",
                     "템플릿 한 벌의 골격: 어느 자리에 데이터가 들어가는지",
                     "허브 페이지 구성"])),
    # 분해 수집은 gsc_snapshots 와 수집일이 어긋날 수 있다(분해 수집을 끄면 뒤처진다)
    # — 출처 표기에 cur 을 쓰면 없던 날짜를 말하게 되므로 ctx['bd']로 따로 읽는다.
    "device_gap": dict(
        see=("site", "dev-sec"),
        label="모바일 격차", defensive=False,
        detect=lambda ctx: device_gap(ctx["conn"], ctx["pid"]),
        metrics=lambda r, ctx: {"impressions": r["mobile_imp"], "position": r["mobile_pos"],
                                 "fit": _fit_of(ctx["conn"], ctx["pid"], r["query"]),
                                 **_ga4_metrics(ctx, query=r["query"])},
        target=lambda r, ctx: r["query"],
        reasoning=lambda r, ctx: (
            f"모바일 {r['mobile_pos']}위, 데스크톱 {r['desktop_pos']}위로 "
            f"{r['dpos']}칸 차이가 납니다. 모바일 노출 {r['mobile_imp']:,} · "
            f"CTR {r['mobile_ctr']}% (데스크톱은 {r['desktop_ctr']}%) "
            f"(구글 실적 {ctx['bd']} 기준)"),
        play=dict(
            what="모바일에서만 순위가 낮습니다. 글이 아니라 모바일 화면·속도가 원인인 경우가 많습니다.",
            acts=["모바일로 그 페이지를 직접 열어 첫 화면에 답이 있는지 봅니다.",
                  "LCP·CLS 를 확인합니다(이미지 크기 지정, 광고·배너 지연 로드).",
                  "표·코드 블록이 가로로 넘치는지 봅니다.",
                  "전면 팝업(인터스티셜)을 걷어냅니다."],
            deliver=["모바일에서 고칠 것 목록. 레이아웃·이미지 크기·지연 로드·팝업",
                     "첫 화면에 무엇이 보여야 하는지"])),
    "index_blocked": dict(
        see=("site", "ix-sec"),
        label="색인 막힘", defensive=False,
        detect=lambda ctx: index_issues(ctx["conn"], ctx["pid"]),
        # 색인 안 된 URL 은 순위가 없다 — position None 을 score() 가 보수적 0.3 으로 본다
        metrics=lambda r, ctx: {"impressions": 0, "position": None,
                                 "fit": _fit_of(ctx["conn"], ctx["pid"], r["url"])},
        target=lambda r, ctx: r["url"],
        reasoning=lambda r, ctx: f"{r['detail']} (색인 확인 {ctx['ix']} 기준)",
        play=dict(
            what="구글이 이 주소를 색인하지 않았습니다. 색인 전에는 순위 자체가 없습니다.",
            acts=["robots.txt·noindex·canonical 이 이 URL 을 막고 있는지 확인합니다.",
                  "사이트맵에 넣고 Search Console 에서 색인을 요청합니다.",
                  "들어오는 내부 링크가 하나도 없으면(고아 페이지) 최소 한 개를 겁니다.",
                  "중복이면 정본 하나만 남깁니다."],
            deliver=["막고 있는 규칙을 어떻게 고칠지. robots.txt 줄 또는 meta robots 값과 적용 위치",
                     "색인 요청·확인 절차 순서",
                     "(글은 손대지 않습니다. 색인 전에는 고쳐도 소용이 없습니다)"])),
    "coverage": dict(
        label="안 다룬 주제", defensive=False,
        detect=_coverage_rows,
        # 이 kind 는 정의상 노출이 0이다("아직 아무 데도 안 뜬다"). 검색량이 없으면
        # 수요 신호도 0이라 점수가 늘 바닥이었다 — "새로 쓸 글"을 고를 때 순서가
        # 뜻이 없었던 이유. volume 이 있으면 score() 가 그걸 수요로 대신 본다.
        metrics=lambda r, ctx: {"impressions": 0, "volume": r["vol"], "position": None,
                                 "fit": _fit_of(ctx["conn"], ctx["pid"], f"cluster:{r['cluster']}")},
        target=lambda r, ctx: f"cluster:{r['cluster']}",
        reasoning=lambda r, ctx: (
            f"'{r['cluster']}' 주제로 추적 중인 키워드 {r['n']}개가 노출도 순위도 "
            "잡히지 않았습니다"
            + (f" (월 검색량 합 {r['vol']:,})" if r["vol"] else "")),
        play=dict(
            what="추적은 하는데 노출도 순위도 없습니다. 이 주제를 다루는 페이지가 없다는 뜻입니다.",
            acts=["이 클러스터를 다루는 페이지가 실제로 있는지 확인합니다.",
                  "없으면 대표 글 하나부터 만듭니다. 클러스터 전체를 한 번에 찍지 않습니다.",
                  "있는데 안 걸린다면 제목·본문에 그 검색어가 실제로 등장하는지 봅니다."],
            deliver=["이 주제를 다룰 대표 글 한 장의 제목과 목차. 클러스터 전체를 한 번에 찍지 않습니다"])),
    # ── 여기까지가 GSC·색인에서 나오는 기회다. 아래는 나머지 네 화면의 재료 —
    #    라벨(KIND_LABEL)과 플레이북(PLAY)은 이미 있었는데 만드는 쪽이 없어서
    #    [AI 인용]·[경쟁 분석]·[백링크]·[사이트 점검] 이 점수도 트리아지도 못 가졌다.
    "ai_citation_gap": dict(
        see=("ai", "ai-miss-sec"),
        # "인용 없음"이었다 — 판정이 비율(ai_is_gap)이 된 뒤로 1/6 질문도 여기 선다.
        label="챗봇 인용 드묾", defensive=False,
        detect=lambda ctx: ai_gaps(ctx["conn"], ctx["pid"]),
        # cite_rate·thin 은 score() 가 읽는다 — 인용률이 reach 와 남은 몫이 되고,
        # 표본 부족이면 깎인다(이유는 score() 주석).
        metrics=lambda r, ctx: {"impressions": 0, "position": None,
                                 "cite_rate": r["cite_rate"], "thin": r["thin"],
                                 "fit": _fit_of(ctx["conn"], ctx["pid"], r["prompt"],
                                                question=True)},
        target=lambda r, ctx: r["prompt"],
        # 질문마다 잰 회차가 다를 수 있다(ai_gaps) — 날짜는 그 질문의 것을 적는다.
        reasoning=lambda r, ctx: (
            f"AI {r['engines']}곳의 답변에서 {ai_cite_label(r['cited'], r['checks'])}"
            + (f". 이름만 나온 것은 {r['named_only']}건입니다" if r["named_only"] else "")
            + (f". 대신 인용되는 곳: {ai_rivals_text(r['rivals'], r['misses'])}"
               if r["rivals"] else "")
            + (" — 대부분 제3자 플랫폼입니다" if r.get("lean") == "third_party" else "")
            + (f" (AI 확인 {str(r['measured_at'])[:10]} 기준)"
               if r.get("measured_at") else "")),
        # 처방이 대신 인용된 곳의 갈래로 갈린다(ai_tally 의 lean) — 제3자 플랫폼이
        # 대부분이면 내 페이지를 고쳐서는 그 자리에 못 들어간다. 모르면 own.
        play={
            "own": dict(
                what="ChatGPT·Perplexity 같은 챗봇이 이 주제에서 남을 출처로 쓰고 나를 빼놓습니다.",
                acts=["질문 그대로를 H2 로 두고 바로 아래에 2~3문장 정답을 둡니다.",
                      "숫자·출처·갱신 날짜를 본문에 적습니다. 인용은 검증 가능한 문장에 붙습니다.",
                      "정의·비교표처럼 그대로 인용하기 쉬운 블록을 만듭니다."],
                deliver=["질문 그대로를 쓴 H2 와 그 아래 2~3문장 직답",
                         "인용될 근거 블록(숫자·출처·갱신일이 들어간 표나 목록)",
                         "Article·FAQPage 구조화 데이터(JSON-LD)"]),
            "third_party": dict(
                what="챗봇이 이 질문에서 내 사이트 대신 커뮤니티·위키·영상·리뷰 사이트 같은 "
                     "제3자 플랫폼을 출처로 씁니다. 브랜드는 자기 사이트보다 이런 곳에서 "
                     "훨씬 자주 인용됩니다.",
                acts=["대신 인용된 곳 중 가장 잦은 플랫폼 한두 곳을 고릅니다.",
                      "그 플랫폼에서 이 질문이 어떻게 다뤄지는지, 어떤 글이 인용되는지 봅니다.",
                      "실제 사람이 소속을 밝히고 질문에 실제로 답하는 식으로만 참여합니다. "
                      "스팸·가짜 후기·대량 게시는 하지 않습니다.",
                      "그 플랫폼에서 링크할 만한 근거 페이지가 우리 사이트에 있는지 보고, "
                      "없으면 그것부터 만듭니다."],
                deliver=["플랫폼별 참여 계획 표: 어디에 · 누가 · 무엇으로 · 그 플랫폼 규칙상 "
                         "허용되는지",
                         "그 플랫폼에서 인용·링크할 만한 우리 페이지(없으면 먼저 만들 것)",
                         "4주 순서표와 다음 AI 확인에서 볼 신호"])}),
    "aio_exposure": dict(
        see=("rank", "ranks"),
        label="구글 AI 요약 빠짐", defensive=False,
        detect=lambda ctx: aio_gaps(ctx["conn"], ctx["pid"]),
        metrics=lambda r, ctx: {"impressions": 0, "volume": r["volume"] or 0,
                                 "position": r["position"],
                                 "fit": _fit_of(ctx["conn"], ctx["pid"], r["keyword"])},
        target=lambda r, ctx: r["keyword"],
        reasoning=lambda r, ctx: (
            "구글이 AI 요약을 붙이는데 내 링크가 없습니다"
            + (f" (실제 순위 {r['position']}위" if r["position"] else "")
            + (f"{' · ' if r['position'] else ' ('}월 검색량 {r['volume']:,}"
               if r["volume"] else "")
            + (")" if r["position"] or r["volume"] else "")),
        play=_AIO_PLAY),
    "content_gap": dict(
        see=("competitors", "cp-gap"),
        label="콘텐츠 공백", defensive=False,
        detect=lambda ctx: content_gaps(ctx["conn"], ctx["pid"]),
        # missing 은 our_position 이 NULL — score() 가 보수적 0.3 으로 본다(맞다: 아직 없다)
        metrics=lambda r, ctx: {"impressions": 0, "volume": r["volume"] or 0,
                                 "position": r["our_position"],
                                 "fit": _fit_of(ctx["conn"], ctx["pid"], r["keyword"])},
        target=lambda r, ctx: r["keyword"],
        reasoning=lambda r, ctx: (
            f"경쟁 도메인 {r['domain']} 순위 {r['position']}위"
            + (f". 나는 {r['our_position']}위로 밀려 있습니다" if r["our_position"]
               else ". 나는 이 검색어에 아예 없습니다")
            + (f" (월 검색량 {r['volume']:,})" if r["volume"] else "")),
        play=_CG_PLAY),
    "crawl_issue": dict(
        see=("site", "crawl-sec"),
        label="크롤에서 걸림", defensive=True,
        detect=lambda ctx: crawl_gaps(ctx["conn"], ctx["pid"]),
        metrics=lambda r, ctx: {"impressions": 0, "position": None,
                                 "fit": _fit_of(ctx["conn"], ctx["pid"], r["url"])},
        target=lambda r, ctx: r["url"],
        reasoning=lambda r, ctx: f"{r['kind']}: {r['detail'] or '크롤에서 걸렸습니다'}",
        play=dict(
            what="사이트를 전부 돌아 보니 이 주소에서 걸립니다. 404 나 리다이렉트 사슬처럼 전수를 봐야 나오는 것들입니다.",
            acts=["그 주소를 직접 열어 지금도 깨져 있는지 확인합니다.",
                  "살릴 수 있으면 살리고, 없어진 것이면 가장 가까운 페이지로 301 합니다.",
                  "그 주소로 걸린 내부 링크를 새 주소로 고칩니다. 리다이렉트는 임시 방편입니다.",
                  "고친 뒤 다시 크롤해 회차 비교에서 사라지는지 봅니다."],
            deliver=["이 주소를 살릴지 301 할지와 그 대상",
                     "고쳐야 할 내부 링크 목록: 어느 글의 어느 앵커인지"])),
    # backlink_broken·backlink_prospect 는 backlink_gaps() 한 번이 (broken, prospects)
    # 튜플을 같이 준다 — detect 가 그중 자기 절반만 골라 쓴다(호출은 두 번 하지만
    # 쿼리가 가벼워 굳이 ctx 로 캐싱하지 않는다. 억지로 공유하면 오히려 순서 의존이 생긴다).
    "backlink_broken": dict(
        see=("backlinks", "bl-broken"),
        label="깨진 백링크", defensive=True,
        detect=lambda ctx: backlink_gaps(ctx["conn"], ctx["pid"])[0],
        metrics=lambda r, ctx: {"impressions": 0, "position": None,
                                 "fit": _fit_of(ctx["conn"], ctx["pid"], r["url_to"])},
        target=lambda r, ctx: r["url_to"],
        reasoning=lambda r, ctx: (
            f"{r['domain_from'] or host_of(r['url_from'])} 에서 이 주소로 링크를 "
            "걸었는데 페이지가 없습니다. 이미 번 링크입니다"
            + (f" (도메인 지수 {r['rank']})" if r["rank"] else "")),
        play=dict(
            what="남이 우리에게 링크를 걸었는데 그 주소에 페이지가 없습니다. 이미 번 링크라 새로 얻는 것보다 늘 쌉니다.",
            acts=["그 주소에 무엇이 있었는지 확인합니다(옮겼는지, 지웠는지).",
                  "가장 가까운 지금 페이지로 301 을 겁니다. 홈으로 몰면 값이 거의 사라집니다.",
                  "옮길 곳이 없으면 그 주제로 페이지를 다시 세우는 쪽이 나을 수 있습니다.",
                  "링크를 건 쪽에 새 주소를 알려 링크 자체를 고치게 합니다."],
            deliver=["301 대상 주소와 그 근거", "링크를 건 쪽에 보낼 짧은 안내문 한 벌"])),
    # robots.txt 원문은 크롤이 이미 남겼다 — 이 kind 에는 새 수집도 새 테이블도
    # 없다. AI 인용 판정보다 먼저 봐야 하는 관문이라 종류로 세운다.
    # 검색·인용용(search·user) 봇만 선다 — 학습 봇 차단은 기회가 아니다(ai_bot_blocks).
    "ai_bot_blocked": dict(
        label="AI 크롤러 차단", defensive=False,
        detect=lambda ctx: ai_bot_blocks(ctx["conn"], ctx["pid"], home=ctx.get("domain") or ""),
        metrics=lambda r, ctx: {"impressions": 0, "position": None, "ai": 1.0},
        target=lambda r, ctx: r["bot"],
        reasoning=_reason_ai_bot,
        play=dict(
            what="robots.txt 가 AI 검색·인용용 크롤러를 막고 있습니다. 막힌 채로는 그 "
                 "엔진이 답을 만들 때 우리 글을 읽어 가지 못해서, 인용이 안 되는 이유가 "
                 "콘텐츠가 아닐 수 있습니다.",
            acts=["막는 것이 의도였는지 먼저 정합니다. 학습을 거부하려던 것이라면 이 봇이 "
                  "아니라 학습 전용 봇(GPTBot·ClaudeBot·Google-Extended·CCBot 등)만 막으면 "
                  "됩니다 — 학습만 막는 것은 인용과 무관한 흔한 선택입니다.",
                  "열기로 했으면 robots.txt 에서 그 User-agent 의 Disallow 를 지웁니다. "
                  "User-agent: * 로 한꺼번에 막혀 있으면 그 봇 이름으로 묶음을 따로 둡니다.",
                  "고친 뒤 robots.txt 를 직접 열어 확인하고, 다음 크롤에서 이 항목이 "
                  "사라지는지 봅니다."],
            deliver=["고칠 robots.txt 줄 — 지금 값과 바꿀 값을 그대로",
                     "막힌 봇을 열면 무엇이 달라지고 무엇을 내주는지 한 줄씩",
                     "(글은 손대지 않습니다. 막힌 채로는 고쳐도 안 읽힙니다)"])),
    "backlink_prospect": dict(
        see=("backlinks", "bl-intersect"),
        label="경쟁사만 받는 링크", defensive=False,
        detect=lambda ctx: backlink_gaps(ctx["conn"], ctx["pid"])[1],
        metrics=lambda r, ctx: {"impressions": 0, "position": None,
                                 "fit": _fit_of(ctx["conn"], ctx["pid"], r["domain"])},
        target=lambda r, ctx: r["domain"],
        reasoning=_reason_backlink_prospect,
        play=dict(
            what="경쟁사는 이 도메인에서 링크를 받는데 나는 못 받습니다. 이미 우리 주제를 다루는 곳이라 문이 열려 있습니다.",
            acts=["그 도메인이 경쟁사를 **어느 글에서** 링크했는지 찾습니다. 그 자리가 우리 자리입니다.",
                  "그 글이 다루는 주제에서 우리가 더 잘 답하는 지점을 하나 고릅니다.",
                  "그 지점을 근거로 짧게 연락합니다. 링크를 달라고 하지 말고 무엇이 빠졌는지를 말합니다.",
                  "받을 만한 페이지가 우리에게 없으면 먼저 그것부터 만듭니다."],
            deliver=["이 도메인에 보낼 연락문 한 벌. 경쟁사가 링크된 그 글을 짚어서",
                     "그쪽이 링크할 만한 우리 페이지. 없다면 무엇을 먼저 만들지"])),
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

    striking_distance·content_gap·aio_exposure 는 한 kind 가 처방 둘을 갖는다(밴드/갈래로
    갈린다). band·gap_kind 를 모르면(다른 kind, 대상을 못 찾은 옛 박제본) 예전 JS 삼항의
    기본값과 같은 쪽(page2/missing)으로 물러선다. aio_exposure 는 beyond 로 물러선다 —
    순위를 모르는 채로 "이미 1페이지 안"이라고 말하지 않는다. 모르는 kind 면 빈 dict —
    화면의 playList() 는 빈 처방을 아무것도 안 그리는 것으로 받아들인다.
    """
    k = _KIND_BY_NAME.get(kind)
    if not k:
        return {}
    p = k.play
    if kind == "striking_distance":
        return p.get(band) or p["page2"]
    if kind == "aio_exposure":
        return p.get(band) or p["beyond"]
    if kind == "content_gap":
        return p.get(gap_kind) or p["missing"]
    if kind == "ai_citation_gap":
        return p.get(gap_kind) or p["own"]
    return p


def gate_rows(conn: sqlite3.Connection, project_id: int, rows: list[dict]) -> list[dict]:
    """심사에서 무관·보류로 판정된 검색어의 기회는 적재하지 않는다. 검색어가 아닌
    종류(KEYWORD_KINDS 밖)는 그대로 통과한다. 미판정은 적재한다 — 심사 화면이
    그 행을 보고 판정한다."""
    import db
    vm = db.verdict_map(conn, project_id)
    return [r for r in rows if r["kind"] not in KEYWORD_KINDS
            or vm.get(norm(str(r["target"]))) not in ("irrelevant", "hold")]


# ── 기회 수명주기: 저절로 풀림 ─────────────────────────────────────────────────
# 기회는 사람이 누르기 전에는 영영 열려 있었다 — AI 요약이 이미 우리를 인용하는데
# "내 링크 없음"으로 남은 기회가 목록에 서 있었다. 그렇다고 "이번 회차에 다시 안
# 나왔다"로 닫으면 반대로 틀린다. 안 나오는 이유가 풀려서만이 아니기 때문이다:
#   · 원천 회차가 불완전하다 — AI 체크가 중간에 멈추면 못 잰 질문의 기회가 안 나온다
#   · 상위 N개만 내는 검출기(ai_gaps·ctr_gaps·content_gaps 의 LIMIT)는 순위에서
#     밀리기만 해도 안 나온다
#   · 수집 대상에서 빠졌다 — 키워드·질문을 끄거나, 크롤 상한에 안 들었다
# 그래서 규칙은 하나다: **기회가 만들어진(또는 사람이 마지막으로 만진) 뒤에 잰
# 새 데이터가, 그 대상 하나를 두고, 조건이 풀렸다고 긍정할 때만** 닫는다. 데이터가
# 없거나 옛것이거나 애매하면 그대로 둔다 — 열린 채 남는 쪽의 비용은 사람이 한 번
# 누르는 것이고, 잘못 닫는 쪽의 비용은 살아 있는 기회를 잃는 것이다.
#
# 종류마다 "무엇이 긍정 확인인가"를 아래 _RESOLVERS 가 갖는다. 없는 종류는 자동으로
# 닫지 않는다(이유는 _NO_RESOLVE). 판정만 여기서 하고 쓰기는 db.resolve_opportunities.


def _after_day(day, since: str) -> bool:
    """하루 단위 데이터(스냅샷 수집일·점검일)가 기준 시각보다 **다음 날 이후**인가.
    같은 날은 안 된다 — 그날 기준 시각보다 먼저 잰 것일 수 있다."""
    return bool(day) and str(day)[:10] > since[:10]


def _after_ts(ts, since: str) -> bool:
    """시각이 찍힌 데이터가 기준 시각보다 뒤인가 (꼴은 db.sql_ts 로 맞춘다)."""
    import db
    t = db.sql_ts(ts)
    return bool(t) and len(t) > 10 and t > since


def _resolve_aio(conn, pid: int, target: str, since: str, ctx: dict) -> str | None:
    """구글 AI 요약에 내 링크 없음 → 그 키워드의 최신 순위 기록에서 AI 요약이 우리를
    인용한다(aio_cited=1). AI 요약이 사라진 것(aio_present=0)은 풀림이 아니다 — 요약은
    붙었다 떨어졌다 한다. 미측정(NULL, serper 경로)도 아무 말도 안 한다."""
    kw = conn.execute("SELECT id FROM keywords WHERE project_id=? AND keyword=?",
                      (pid, target)).fetchone()
    if not kw:
        return None
    r = conn.execute(
        """SELECT checked_at, position, aio_present, aio_cited FROM rank_snapshots
            WHERE keyword_id=? ORDER BY replace(checked_at,'T',' ') DESC, id DESC LIMIT 1""",
        (kw[0],)).fetchone()
    if not r or not _after_ts(r["checked_at"], since):
        return None
    if r["aio_present"] == 1 and r["aio_cited"] == 1:
        pos = f", 순위 {r['position']}위" if r["position"] else ""
        return f"구글 AI 요약이 우리 링크를 인용합니다 ({str(r['checked_at'])[:10]} 순위 확인{pos})"
    return None


def _resolve_ai_citation(conn, pid: int, target: str, since: str, ctx: dict) -> str | None:
    """챗봇 인용 드묾 → 그 질문의 가장 최근 측정이 **끝난 회차**에 속하고, 그 회차에서
    인용률이 기회 문턱을 넘었다(ai_is_gap 이 거짓 — 세우는 쪽과 같은 판정). 6번 중 1번
    걸린 것은 풀림이 아니다: 그 자체가 기회로 선다. 가장 최근 측정이 도는 중이거나
    예외로 끊긴 회차면 판단하지 않는다 — 끊긴 회차에서 이 질문을 잰 몇 건만으로 말하지
    않는다."""
    p = conn.execute("SELECT id FROM ai_prompts WHERE project_id=? AND prompt=?",
                     (pid, target)).fetchone()
    if not p:
        return None
    last = conn.execute("SELECT run_id FROM ai_checks WHERE prompt_id=? ORDER BY id DESC LIMIT 1",
                        (p[0],)).fetchone()
    if not last or last[0] is None:
        return None
    run = conn.execute("SELECT finished_at, notes FROM runs WHERE id=?", (last[0],)).fetchone()
    if not run or _ai_run_state(run) != "done":     # 끝난 회차의 판정은 한 벌(갈래 2)
        return None
    a = conn.execute(
        """SELECT COUNT(*) n, SUM(cited) cited, MIN(checked_at) first FROM ai_checks
            WHERE run_id=? AND prompt_id=?""", (last[0], p[0])).fetchone()
    if not a["n"] or not _after_ts(a["first"], since):
        return None
    if not ai_is_gap(a["cited"], a["n"]):
        return f"AI 답변 {a['n']}건 중 {a['cited']}건이 우리를 인용합니다 (AI 회차 #{last[0]})"
    return None


def _gsc_now(conn, pid: int, target: str, since: str, ctx: dict):
    """최신 GSC 스냅샷에서 그 검색어 한 줄 — 스냅샷이 기준 이후일 때만."""
    if "gsc" not in ctx:
        cur, _prev, period, _mm = snapshot_pair(conn, pid)
        ctx["gsc"] = (cur, period)
    cur, period = ctx["gsc"]
    if not cur or not _after_day(cur, since):
        return None, cur
    r = conn.execute(
        """SELECT AVG(position) pos, SUM(impressions) imp, SUM(clicks) clk FROM gsc_snapshots
            WHERE project_id=? AND snapshot_date=? AND period_days=? AND query=?""",
        (pid, cur, period, target)).fetchone()
    return (r if r and r["imp"] is not None and r["pos"] is not None else None), cur


def _resolve_striking(conn, pid: int, target: str, since: str, ctx: dict) -> str | None:
    """밀면 오를 검색어(4~20위) → 새 스냅샷에서 노출 하한을 넘기며 3위 안.
    20위 밖으로 떨어진 것·노출이 줄어 하한 아래로 간 것은 풀림이 아니다."""
    r, cur = _gsc_now(conn, pid, target, since, ctx)
    if not r:
        return None
    pos = round(r["pos"], 1)
    if r["imp"] >= STRIKING_MIN_IMP and pos < STRIKING_LO:
        return f"평균 {pos}위 · 노출 {r['imp']:,}로 상단 3위권에 들었습니다 (구글 실적 {cur} 기준)"
    return None


def _resolve_ctr(conn, pid: int, target: str, since: str, ctx: dict) -> str | None:
    """클릭률 미달 → 새 스냅샷에서 여전히 1페이지·노출 하한 이상이고, 클릭률이 그 순위
    기대치의 CTR_GAP_FACTOR 배를 넘는다. 1페이지 밖으로 밀린 것은 풀림이 아니다."""
    r, cur = _gsc_now(conn, pid, target, since, ctx)
    if not r:
        return None
    pos = round(r["pos"], 1)
    if r["imp"] < CTR_GAP_MIN_IMP or not (1 <= pos <= PAGE1):
        return None
    expected = EXPECTED_CTR[min(max(round(pos), 1), STRIKING_HI)]
    actual = (r["clk"] or 0) * 100.0 / r["imp"]
    if actual >= expected * CTR_GAP_FACTOR:
        return (f"{pos}위에서 클릭률 {round(actual, 2)}% — 이 순위 기대치 {expected}%의 "
                f"절반을 넘었습니다 (구글 실적 {cur} 기준)")
    return None


def _resolve_device(conn, pid: int, target: str, since: str, ctx: dict) -> str | None:
    """모바일 격차 → 새 기기별 분해에서 모바일·데스크톱이 둘 다 잡히고 모바일 노출이
    하한 이상인데 격차가 DEVICE_GAP_POS 아래. 한쪽만 잡히면 비교가 안 되니 말하지 않는다."""
    if "bd" not in ctx:
        ctx["bd"] = _latest(conn, _LATEST_BD, (pid, "device"))
    bd = ctx["bd"]
    if not bd or not _after_day(bd, since):
        return None
    d = {(r["dim_value"] or "").upper(): r for r in conn.execute(
        """SELECT dim_value, SUM(impressions) imp, AVG(position) pos FROM gsc_breakdown
            WHERE project_id=? AND snapshot_date=? AND dim='device' AND query=?
            GROUP BY dim_value""", (pid, bd, target))}
    m, k = d.get("MOBILE"), d.get("DESKTOP")
    if not (m and k) or m["pos"] is None or k["pos"] is None or (m["imp"] or 0) < DEVICE_MIN_IMP:
        return None
    dpos = round(m["pos"] - k["pos"], 1)
    if dpos < DEVICE_GAP_POS:
        return (f"모바일 {round(m['pos'], 1)}위 · 데스크톱 {round(k['pos'], 1)}위로 격차가 "
                f"{dpos}칸입니다 (구글 실적 {bd} 기준)")
    return None


def _resolve_index(conn, pid: int, target: str, since: str, ctx: dict) -> str | None:
    """색인 막힘 → 그 주소의 가장 최근 색인 점검이 PASS 이고 색인됨."""
    r = conn.execute(
        """SELECT checked_date, verdict, coverage_state FROM gsc_index_status
            WHERE project_id=? AND url=? ORDER BY checked_date DESC LIMIT 1""",
        (pid, target)).fetchone()
    if not r or not _after_day(r["checked_date"], since):
        return None
    if (r["verdict"] or "").upper() == "PASS" and _indexed(r["coverage_state"]):
        return f"색인됐습니다: {r['coverage_state']} (색인 확인 {r['checked_date']})"
    return None


def _resolve_prospect(conn, pid: int, target: str, since: str, ctx: dict) -> str | None:
    """경쟁사만 받는 링크 → 그 도메인의 가장 최근 교집합 기록에서 우리도 받는다(we_have=1).
    we_have 는 우리 참조 도메인과 겹칠 때만 1 이라 수집이 비면 0 쪽으로만 틀린다."""
    r = conn.execute(
        """SELECT checked_date, we_have FROM link_intersect
            WHERE project_id=? AND domain=? ORDER BY checked_date DESC LIMIT 1""",
        (pid, target)).fetchone()
    if r and _after_day(r["checked_date"], since) and r["we_have"] == 1:
        return f"이제 {target} 에서 우리도 링크를 받습니다 (백링크 {r['checked_date']} 기준)"
    return None


def _resolve_ai_bot(conn, pid: int, target: str, since: str, ctx: dict) -> str | None:
    """AI 크롤러 차단 → 그 뒤에 끝난 크롤이 가져온 robots.txt 가 그 봇을 안 막는다.
    원문에 User-agent 줄이 하나도 없으면 robots.txt 가 아니다(200 으로 온 오류 페이지)
    — 규칙이 없어서 '안 막는다'로 읽히므로 판단하지 않는다.

    학습 전용 봇으로 확인된 대상도 닫는다: 용도를 가르기 전 판정이 GPTBot 같은 학습
    봇을 이 종류로 올려 두었다. 그대로 두면 막힌 채라 영영 안 닫힌다. 용도 모름(옛
    꼴 config)은 닫지 않는다 — 모르는 것을 무해라고 확인한 셈이 된다."""
    cr = conn.execute(
        "SELECT id, started_at, robots_txt FROM crawl_runs WHERE project_id=?"
        " AND finished_at IS NOT NULL AND robots_txt IS NOT NULL ORDER BY id DESC LIMIT 1",
        (pid,)).fetchone()
    if not cr or not _after_ts(cr["started_at"], since):
        return None
    if ai_bot_purpose(target) == "training":
        return (f"{target} 는 학습 전용 크롤러라 막아도 인용과 무관합니다 — 기회가 아닙니다 "
                f"(크롤 #{cr['id']})")
    txt = cr["robots_txt"] or ""
    if not re.search(r"(?im)^\s*user-agent\s*:", txt):
        return None
    home = ctx.get("domain") or ""
    url = home if home.startswith("http") else f"https://{home or 'example.com'}/"
    if target == AI_BOT_WILDCARD:
        # 묶음 기회 — 와일드카드 묶음이 더는 홈을 안 막으면 닫는다
        if robots_blocks(txt, url, agent=_WILDCARD_PROBE) is None:
            return f"robots.txt 의 User-agent: * 묶음이 더는 막지 않습니다 (크롤 #{cr['id']})"
        return None
    if robots_blocks(txt, url, agent=target) is None:
        return f"robots.txt 가 더는 {target} 를 막지 않습니다 (크롤 #{cr['id']})"
    # 봇마다 세우던 옛 기회: 이제 User-agent: * 묶음 한 줄로 선다. 여전히 막혀 있다고
    # 분명히 적는다 — "고쳐졌다"로 읽히면 안 된다.
    if (ai_bot_purpose(target) in AI_BOT_CITING and not _named_in_robots(txt, target)
            and robots_blocks(txt, url, agent=_WILDCARD_PROBE) is not None):
        return (f"{target} 는 여전히 막혀 있지만, 원인이 User-agent: * 한 줄이라 "
                f"그 묶음 기회로 합쳤습니다 (크롤 #{cr['id']})")
    return None


# 종류 → 긍정 확인 규칙. 여기 없는 종류는 저절로 닫히지 않는다.
_RESOLVERS = {
    "striking_distance": _resolve_striking,
    "ctr_gap": _resolve_ctr,
    "device_gap": _resolve_device,
    "index_blocked": _resolve_index,
    "ai_citation_gap": _resolve_ai_citation,
    "aio_exposure": _resolve_aio,
    "backlink_prospect": _resolve_prospect,
    "ai_bot_blocked": _resolve_ai_bot,
}
# 자동 해소에서 뺀 종류와 그 이유 — 규칙을 세울 수 없거나, 세우면 살아 있는 것을 닫는다.
_NO_RESOLVE = {
    "rank_decay": "연속한 두 스냅샷 사이의 하락이라 다음 스냅샷에서 저절로 사라진다 — "
                  "'더 안 떨어졌다'는 '되찾았다'가 아니고, 떨어지기 전 순위는 근거 문장에만 있다",
    "cannibalization": "풀렸다는 증거가 '둘째 페이지 줄이 없다'는 부재다 — 301 로 합친 것과 "
                       "GSC 행 상한에 잘린 것을 가르지 못한다",
    "intent_split": "풀렸다는 증거가 '2위 의도 줄이 없다'는 부재다 — 지면을 갈라 옮겨 간 것과, "
                    "그 묶음의 노출이 계절적으로 하한 밑으로 내려간 것을 가르지 못한다. 가르기는 "
                    "몇 주가 걸리는 일이라 그 사이의 부재를 완료로 읽으면 안 된다",
    "pseo_pattern": "대상은 템플릿으로 찍을 무리의 씨앗 검색어다 — 그 검색어 하나의 클릭률이 "
                    "올랐다고 템플릿을 찍은 것이 아니다",
    "coverage": "대상이 클러스터이고 그 구성은 키워드 큐레이션(켜기·끄기·재분류)으로 바뀐다 — "
                "비었다는 것이 다룬 것인지 뺀 것인지 모른다",
    "content_gap": "경쟁사별로 따로 받는 기록이라 한 경쟁사 수집이 실패하면 그 줄이 빠진다 — "
                   "남은 줄이 전부 shared 여도 우리를 이기는 경쟁사가 빠진 것일 수 있다",
    "crawl_issue": "크롤 상한에 안 든 주소는 안 보이고, 관계형 문제(중복·상호 참조)는 짝이 "
                   "크롤돼야 잡힌다 — 이번 크롤에 문제가 없다는 것이 고쳐졌다는 뜻이 아니다",
    "backlink_broken": "is_broken=0 은 '살아 있음'과 '필드가 안 왔음'(API 기본값)을 가르지 "
                       "못한다 — 응답 모양이 바뀌면 깨진 링크 전부가 한꺼번에 닫힌다",
}


def resolve_stale(conn: sqlite3.Connection, project_id: int, run_id: int | None, *,
                  domain: str = "") -> dict:
    """이번 적재(run_id)에 다시 안 나온 열린 기회(new|acked) 중 새 데이터가 조건이
    풀렸다고 긍정 확인한 것만 닫는다 — 위 머리 주석이 규칙의 전부다.

    기준 시각은 기회가 만들어진 때와 사람이 마지막으로 상태를 바꾼 때 중 늦은 쪽이다.
    사람이 [다시 열기]를 눌렀으면 그 전에 잰 데이터로는 다시 닫지 않는다.
    반환: {"resolved": n, "done": n, "checked": 후보 중 규칙이 있는 것의 수}."""
    import db
    ctx: dict = {"domain": domain}
    decisions, checked = [], 0
    for o in conn.execute(
            """SELECT id, kind, target, created_at, status_at FROM opportunities
                WHERE project_id=? AND status IN ('new','acked') AND run_id IS NOT ?""",
            (int(project_id), run_id)).fetchall():
        rule = _RESOLVERS.get(o["kind"])
        marks = [t for t in (db.sql_ts(o["created_at"]), db.sql_ts(o["status_at"])) if t]
        if not rule or not marks:
            continue
        checked += 1
        reason = rule(conn, int(project_id), o["target"], max(marks), ctx)
        if reason:
            decisions.append((o["id"], reason))
    out = db.resolve_opportunities(conn, int(project_id), decisions)
    out["checked"] = checked
    return out


def load(project: str) -> None:
    """서브커맨드 load — KINDS 명부를 순회해 opportunities 에 적재.

    projects.type 을 읽어 프리셋 계수(WEIGHTS)를 적용한다 — 분석 코드가 type 을
    안 읽던 결함의 수정. 트리아지 상태(acked·done·dismissed) 보존은
    db.upsert_opportunities 가 보장한다 (ON CONFLICT에서 status 미변경).
    적재 뒤 resolve_stale 이 이번에 다시 안 나온 열린 기회 중 조건이 풀렸음을 새
    데이터로 확인한 것만 닫는다(저절로 풀림·작업 후 완료).
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
    cur, prev, period, _ = snapshot_pair(conn, pid)
    brands = foreign_brands(conn, pid, cfg)
    # 의도 미분류(NULL)만 채움 — Claude/사람 보정은 살아남음
    n_intent = _backfill_intents(conn, pid)
    # GA4 미연결이면 둘 다 빈 dict — _ga4_metrics() 가 그대로 빈 조각을 돌려주고
    # value_mult() 는 정확히 1.0을 곱한다(점수 불변, scoring.md 2절 프레임 안 건드림).
    ga4_date = _latest(conn, _LATEST_GA4, (pid,))
    ga4 = _ga4_agg(conn, pid, ga4_date) if ga4_date else {}
    page_agg = _page_agg(conn, pid, cur, period) if ga4_date and cur else {}
    ctx = {"conn": conn, "pid": pid, "cur": cur, "prev": prev, "brands": brands,
           "bd": _latest(conn, _LATEST_BD, (pid, "device")),
           "ix": _latest(conn, _LATEST_IX, (pid,)),
           "ga4": ga4, "page_agg": page_agg,
           # robots.txt 판정은 "이 주소를 막나" 라서 사이트 주소가 필요하다
           "domain": p["domain"] or ""}
    rows = []
    for k in KINDS:
        for r in k.detect(ctx):
            rows.append({"kind": k.name, "target": k.target(r, ctx),
                         "score": score(k.name, k.metrics(r, ctx), ptype),
                         "reasoning": k.reasoning(r, ctx)})
    rows = gate_rows(conn, pid, rows)       # 심사에서 뺀 검색어는 기회가 안 된다
    with db.run(conn, pid, "gaps") as r:
        n = db.upsert_opportunities(conn, pid, r.id, rows)
        # 다시 안 나온 열린 기회 중 새 데이터가 풀렸다고 **긍정 확인**한 것만 닫는다.
        # upsert 뒤여야 한다 — 이번 회차에 다시 나온 것은 run_id 가 r.id 로 바뀌어
        # 후보에서 빠지고, 풀렸다 다시 나빠진 것은 upsert 가 이미 되열었다.
        closed = resolve_stale(conn, pid, r.id, domain=p["domain"] or "")
        r.notes = (f"scoring load: opps={n}, intents_filled={n_intent}, "
                   f"resolved={closed['resolved']}, done={closed['done']}")
    print(f"loaded {len(rows)} opportunities for '{project}' (type={ptype}, gsc {cur}; "
          f"intents_filled={n_intent}; 저절로 풀림 {closed['resolved']} · "
          f"작업 후 풀림(완료) {closed['done']})")


def opportunities(conn: sqlite3.Connection, project_id: int, *,
                  limit: int, with_id: bool = False) -> list[dict]:
    """기회 목록 — 화면과 박제본이 같은 정렬을 본다.

    정렬이 두 벌이던 시절엔 대시보드와 리포트가 같은 데이터로 다른 순서를 보여줬다.
    """
    import db
    return [dict(r) for r in db.list_opportunities(
        conn, project_id, limit=limit, order="screen", with_id=with_id, gated=True)]


# ── 기회 묶음 — 같은 답을 내야 하는 지면끼리 목록 한 줄로 ─────────────────────
# DB 에는 여전히 검색어마다 기회 하나다(사실). 묶음은 **읽을 때만** 한다(표시·우선순위).
# 기회를 저절로 닫는 쪽(검색어 단위)과 부딪히지 않고, 틀리면 이 함수만 되돌리면 된다.
#
# 묶는 종류는 aio_exposure 하나다. 그 종류의 할 일(그 페이지의 순위를 올리거나, 이미
# 1페이지면 사람이 읽기에 더 나은 글로 다듬기 — _AIO_PLAY)은 페이지에 한 번 하는
# 일이라 변형 검색어(`milia vs syringoma` / `syringomas` …)가 따로 줄을 차지할
# 이유가 없고, 실제로 한 사이트에 190건이 쌓여 무엇부터 할지 안 보였다. 나머지 검색어 종류는 안 묶는다: striking_distance·ctr_gap·rank_decay·
# device_gap 은 근거 문장이 **그 검색어의** 순위·CTR·Δ 를 말해서 합치면 어느 변형이
# 처지는지가 사라지고, cannibalization 은 정의상 한 검색어에 페이지가 여럿이라 "같은
# 페이지"라는 열쇠가 성립하지 않으며, 대상이 문장(ai_citation_gap)·URL·도메인·클러스터인
# 종류는 변형이라는 개념이 없다. 늘리려면 이 튜플에 넣는다 — 규칙은 종류를 안 가린다.
GROUP_KINDS = ("aio_exposure",)
# 열린 기회 — 이 둘만 묶는다. 닫힌 것(done·dismissed, 그리고 저절로 풀린 resolved 처럼
# 뒤에 생기는 상태)은 기록이라 한 줄씩 남는다. 거르는 쪽이 "done 이 아니면"이 아니라
# "이 둘이면"이라서 새 상태가 생겨도 열린 목록으로 새지 않는다. 화면의 [아직 안 함]
# 거르개(overview.html ST_GROUP.open)가 같은 한 벌이다(test_seams 23).
OPEN_STATUSES = ("new", "acked")
# 검색 결과 겹침(열쇠 ②): 몇 개가 같아야 같은 지면으로 보나. 보는 것은 수집기가
# 남긴 상위 전부다 — serp_results 는 상위 db.SERP_KEEP(=5)개만 남긴다. 그 다섯 중
# 셋 이상이 같은 주소면 구글이 같은 문서들로 답하는 질문으로 읽는다. 둘로 내리면
# 주제만 비슷한 검색어(같은 대형 사이트 두어 곳이 늘 끼는 분야)가 딸려 온다.
SERP_MIN_SHARED = 3
# 열쇠가 무엇이었나 — 화면이 "왜 한 줄이냐"를 말할 때 이 값을 읽는다.
#   page    : 우리 페이지가 같다 (GSC 로 그 검색어에 노출된 페이지, 없으면 검색 순위에서 잡힌 우리 주소)
#   serp    : 우리 페이지는 없지만 검색 결과 상위가 크게 겹친다
#   cluster : 둘 다 모르고, 키워드 클러스터가 같다
#   norm    : 띄어쓰기·대소문자만 다른 같은 검색어뿐이다
#   alone   : 묶을 짝이 없다
GROUP_VIA = ("page", "serp", "cluster", "norm", "alone")


def _chunks(xs: list, n: int = 400):
    for i in range(0, len(xs), n):
        yield xs[i:i + n]


def _surface_facts(conn: sqlite3.Connection, project_id: int,
                   targets: list[str]) -> dict[str, dict]:
    """검색어 → 지면을 가늠할 사실들. 없는 사실은 None/빈 집합이다(추정하지 않는다).

    page·page_src : 우리 페이지 열쇠(url_key). GSC 에서 그 검색어로 노출이 가장 큰
                    페이지를 먼저 보고, 없으면 순위 조회(rank_snapshots.url)가 잡은 우리 주소.
    serp          : 최신 조회의 상위 주소(url_key) 집합 — 수집기가 남긴 만큼(db.SERP_KEEP).
                    AI 요약 안의 인용 도메인(rank_snapshots.aio_domains_json)은 열쇠로 쓰지
                    않는다 — 요약은 조회마다 붙었다 떨어졌다 하고 옛 행에는 없다.
    cluster·volume: keywords 표. 검색어와 norm 으로 짝짓는다.
    """
    out = {t: {"page": None, "page_url": None, "page_src": None, "serp": frozenset(),
               "cluster": None, "volume": 0} for t in targets}
    by_norm: dict[str, list[str]] = {}
    for t in targets:
        by_norm.setdefault(norm(t), []).append(t)
    # 검색어 → keywords 행. 글자가 똑같은 행이 있으면 그것, 없으면 norm 이 같은 첫 행.
    chosen: dict[str, tuple[bool, sqlite3.Row]] = {}
    for r in conn.execute("SELECT id, keyword, cluster, volume FROM keywords"
                          " WHERE project_id=? ORDER BY id", (project_id,)):
        for t in by_norm.get(norm(r["keyword"]), ()):
            exact = r["keyword"].strip() == t
            if t not in chosen or (exact and not chosen[t][0]):
                chosen[t] = (exact, r)
    kid_of: dict[int, list[str]] = {}
    for t, (_exact, r) in chosen.items():
        out[t]["cluster"] = (r["cluster"] or "").strip() or None
        out[t]["volume"] = int(r["volume"] or 0)
        kid_of.setdefault(r["id"], []).append(t)

    for q, rows in pages_by_query(conn, project_id, targets, top=1).items():
        if q in out and rows:
            out[q].update(page=url_key(rows[0]["page"]), page_url=rows[0]["page"], page_src="gsc")

    for ch in _chunks(list(kid_of)):
        ph = ",".join("?" * len(ch))
        # 키워드마다 최신 조회 한 벌. url 이 NULL 이면 그 조회에서 우리가 안 잡힌 것이다 —
        # 그 전 조회의 주소로 물러서지 않는다(지금 없는 페이지를 열쇠로 쓰게 된다).
        latest: dict[int, str | None] = {}
        for r in conn.execute(
                f"""SELECT keyword_id, url FROM rank_snapshots
                     WHERE keyword_id IN ({ph}) ORDER BY checked_at DESC, id DESC""", ch):
            latest.setdefault(r["keyword_id"], r["url"])
        top: dict[int, list[str]] = {}
        for r in conn.execute(
                f"""SELECT s.keyword_id, s.url FROM serp_results s
                     WHERE s.keyword_id IN ({ph}) AND s.checked_at=(
                           SELECT MAX(checked_at) FROM serp_results x
                            WHERE x.keyword_id=s.keyword_id)
                     ORDER BY s.keyword_id, s.position""", ch):
            if r["url"]:
                top.setdefault(r["keyword_id"], []).append(url_key(r["url"]))
        for kid in ch:
            for t in kid_of[kid]:
                f = out[t]
                if f["page_src"] is None and latest.get(kid):
                    f.update(page=url_key(latest[kid]), page_url=latest[kid], page_src="rank")
                if top.get(kid):
                    f["serp"] = frozenset(top[kid])
    return out


def _group_members(members: list[dict], facts: dict[str, dict]) -> list[dict]:
    """한 종류의 열린 기회들을 지면으로 접는다. members 는 점수 내림차순이다.

    열쇠 우선순위: ① 우리 페이지 ② 검색 결과 겹침 ③ 클러스터 ④ 혼자.
    센 증거가 약한 증거를 이긴다 — 검색 결과를 재 봤는데 아무와도 안 겹친 검색어는
    클러스터가 같아도 안 붙인다(재 본 결과가 "다른 지면"이라고 말했다).
    ②는 짝을 **묶음의 씨앗**(첫 검색어)과만 잰다. 서로서로 재면 A~B, B~C 가 겹친다는
    이유로 안 겹치는 A 와 C 가 한 줄이 된다(사슬).
    """
    # 띄어쓰기·대소문자만 다른 것은 같은 검색어다 — 먼저 한 덩어리로 만든다(심사가
    # 판정을 norm 단위로 하는 것과 같은 열쇠).
    units: dict[str, list[dict]] = {}
    for o in members:
        units.setdefault(norm(o["target"]), []).append(o)

    def fact(unit: list[dict], k: str):
        for o in unit:
            v = facts.get(o["target"], {}).get(k)
            if v:
                return v
        return None

    groups: list[dict] = []
    by_page: dict[str, dict] = {}
    pending: list[list[dict]] = []
    for unit in units.values():
        page = fact(unit, "page")
        if page:
            g = by_page.get(page)
            if g is None:
                src = next(facts[o["target"]]["page_src"] for o in unit
                           if facts.get(o["target"], {}).get("page"))
                url = next(facts[o["target"]]["page_url"] for o in unit
                           if facts.get(o["target"], {}).get("page"))
                g = by_page[page] = {"via": "page", "key": url, "key_src": src,
                                     "seed": fact(unit, "serp"), "units": []}
                groups.append(g)
            g["seed"] = g["seed"] or fact(unit, "serp")
            g["units"].append((unit, "page"))
        else:
            pending.append(unit)

    loose: list[list[dict]] = []
    for unit in pending:
        s = fact(unit, "serp")
        if not s:
            loose.append(unit)
            continue
        best, hit = None, 0
        for g in groups:
            n = len(s & g["seed"]) if g["seed"] else 0
            if n >= SERP_MIN_SHARED and n > hit:
                best, hit = g, n
        if best is None:
            best = {"via": "serp", "key": unit[0]["target"], "key_src": None,
                    "seed": s, "units": []}
            groups.append(best)
        best["units"].append((unit, "serp"))

    by_cluster: dict[str, dict] = {}
    for unit in loose:
        cl = fact(unit, "cluster")
        if not cl:
            groups.append({"via": "alone", "key": None, "key_src": None, "seed": None,
                           "units": [(unit, "alone")]})
            continue
        g = by_cluster.get(cl)
        if g is None:
            g = by_cluster[cl] = {"via": "cluster", "key": cl, "key_src": None,
                                  "seed": None, "units": []}
            groups.append(g)
        g["units"].append((unit, "cluster"))

    for g in groups:
        if len(g["units"]) == 1:
            g["via"] = "norm" if len(g["units"][0][0]) > 1 else "alone"
            if g["via"] == "alone":
                g["key"] = g["key_src"] = None
    return groups


def _group_score(kind: str, lead: dict, units: list[list[dict]],
                 facts: dict[str, dict], project_type: str) -> float:
    """묶음 점수 — 대표(최고 점수) 검색어의 점수에, 변형들이 보탠 **수요만큼** 더한다.

    최댓값만 쓰면 변형 여덟 개짜리 지면과 하나짜리 지면이 같은 줄에 선다 — 무엇부터
    할지 고르려고 묶었는데 묶음이 순서에 아무 말도 안 한다. 점수를 그냥 더하면
    100점 만점 막대가 넘치고, 서로 무관한 축(순위·적합도·AI 가중)까지 변형 수만큼
    곱절이 된다. 그래서 score() 가 이미 쓰는 수요 축 하나만 다시 잰다: 대표의 검색량
    대신 지면 전체의 검색량 합을 넣었을 때 score() 가 얼마나 달라지나. 수요는
    log10 이라 변형이 늘수록 보탬이 줄고(검색량 10배에 +0.2 수요), 결과는 늘 0~100 이라
    안 묶인 종류의 줄과 같은 막대에서 견줄 수 있다. score() 는 수요에 선형이라 그
    차이는 순위·적합도와 무관하다 — 그래서 여기서 그 둘을 다시 구하지 않는다.
    띄어쓰기 변형(같은 norm)은 같은 검색이라 검색량을 한 번만 센다(큰 쪽).
    """
    vol = lambda unit: max((facts.get(o["target"], {}).get("volume") or 0) for o in unit)
    lead_unit = next(u for u in units if lead in u)
    total = sum(vol(u) for u in units)
    base = {"impressions": 0, "position": None, "fit": 0.5}
    bonus = (score(kind, {**base, "volume": total}, project_type)
             - score(kind, {**base, "volume": vol(lead_unit)}, project_type))
    return round(min(100.0, (lead.get("score") or 0.0) + max(0.0, bonus)), 1)


def group_opportunities(conn: sqlite3.Connection, project_id: int,
                        opps: list[dict], *, kinds=GROUP_KINDS) -> list[dict]:
    """기회 목록을 줄로 접는다 — 줄 하나가 판정·실행 단위다. opps 는 opportunities() 의 결과.

    돌려주는 줄 하나: {lead, ids, kind, score, status, via, key, key_src, variants}
      lead     : 대표 기회 id(점수 최고). 화면은 그 기회의 라벨·처방·요청문으로 줄을 그린다.
      ids      : 묶인 기회 id 전부. 판정 버튼은 이 전부에 먹는다 — id 는 하나도 안 잃는다.
      variants : [{id, target, score, status, volume, via}] 점수 순. 대표도 첫째로 들어 있다.
      score    : _group_score. 혼자인 줄은 그 기회의 점수 그대로.
      status   : 묶음 안에 진행 중(acked)이 하나라도 있으면 acked, 아니면 new.
    묶는 것은 kinds 의 **열린**(OPEN_STATUSES) 기회뿐이다. 그 밖(다른 종류·닫힌 기회)은
    전부 한 줄씩 그대로 나온다 — via None.

    opps 는 화면에 싣는 몫이라 개수 상한이 있다. 상한 밖으로 밀린 변형도 묶음의 id 에는
    들어가야 한다(아니면 [완료 표시]가 그것만 남기고, 다음 적재에 혼자 다시 선다) —
    그래서 묶는 종류의 열린 기회는 DB 에서 한 번 더 전부 읽는다. 대표가 상한 밖이면
    (묶음 전체가 밀린 것이다) 그 줄은 싣지 않는다 — 예전에도 안 보이던 것이다.
    """
    import db
    kinds = tuple(kinds)
    ptype = (conn.execute("SELECT type FROM projects WHERE id=?", (project_id,)).fetchone()
             or ["saas"])[0] or "saas"
    shown = {o["id"]: o for o in opps if o.get("id") is not None}
    pool = [dict(r) for r in db.list_opportunities(
        conn, project_id, kinds=list(kinds), statuses=list(OPEN_STATUSES), order="screen",
        limit=1_000_000, with_id=True, gated=True)] if kinds else []
    facts = _surface_facts(conn, project_id, list(dict.fromkeys(o["target"] for o in pool)))

    lines: list[dict] = []
    grouped: set[int] = set()
    for kind in kinds:
        members = sorted((o for o in pool if o["kind"] == kind),
                         key=lambda o: (-(o["score"] or 0), -o["id"]))
        for g in _group_members(members, facts):
            units = [u for u, _via in g["units"]]
            flat = sorted(((o, via) for u, via in g["units"] for o in u),
                          key=lambda x: (-(x[0]["score"] or 0), -x[0]["id"]))
            grouped.update(o["id"] for o, _ in flat)
            lead = next((o for o, _ in flat if o["id"] in shown), None)
            if lead is None:
                continue
            lines.append({
                "lead": lead["id"], "ids": [o["id"] for o, _ in flat], "kind": kind,
                "score": _group_score(kind, lead, units, facts, ptype) if len(units) > 1
                         else lead["score"],
                "status": "acked" if any(o["status"] == "acked" for o, _ in flat) else "new",
                "via": g["via"], "key": g["key"], "key_src": g["key_src"],
                "variants": [{"id": o["id"], "target": o["target"], "score": o["score"],
                              "status": o["status"],
                              "volume": facts.get(o["target"], {}).get("volume") or None,
                              "via": via} for o, via in flat]})
    for o in opps:
        if o.get("id") is None or o["id"] in grouped:
            continue
        lines.append({"lead": o["id"], "ids": [o["id"]], "kind": o["kind"], "score": o["score"],
                      "status": o["status"], "via": None, "key": None, "key_src": None,
                      "variants": [{"id": o["id"], "target": o["target"], "score": o["score"],
                                    "status": o["status"], "volume": None, "via": None}]})
    # 화면 정렬과 같다(db.list_opportunities 의 'screen'): 새 것 먼저, 점수, 최근 id.
    lines.sort(key=lambda x: (x["status"] != "new", -(x["score"] or 0), -x["lead"]))
    return lines



def _selfcheck() -> None:
    assert norm("Future Tools") == "futuretools"
    assert norm("丘疹性瘢痕 鼻") == "丘疹性瘢痕鼻" and norm("韓国 ジュベルック") == "韓国ジュベルック", "중국어·일본어가 빈 키가 된다"
    assert norm("디아 더 피부과, 가격!") == "디아더피부과가격" and norm("a_b c") == "abc"
    assert host_of("https://www.Ecrett.com/pricing?a=1") == "ecrett.com"
    assert owns("blog.example.com", "example.com")
    assert owns("example.com", "https://example.com/")
    assert not owns("notexample.com", "example.com")
    assert _stem("futuretools.io") == "futuretools"
    # 이름은 등록 도메인에서 — 하위 도메인 첫 칸('gangnam'·'blog')이 브랜드가 되면
    # 'gangnam dermatology clinic' 같은 일반 검색어가 남의 브랜드로 걸러진다.
    assert _stem("gangnam.museclinic.co.kr") == "museclinic"
    assert _stem("m.blog.naver.com") == "naver"
    assert _stem("www.daeskin.com.au") == "daeskin"
    assert _stem("k-health.com") == "khealth"

    # 순위 수집이 붙이는 경쟁사 — 우리·플랫폼은 빼고, 잰 검색어의 10% 문턱, 겹친 순.
    plats = ("blog.naver.com", "youtube.com")
    hits = {"m.blog.naver.com": 60, "youtube.com": 40, "rival.com": 12, "sub.me.com": 50,
            "two.com": 30, "rare.com": 9}
    assert serp_rivals(hits, 100, "me.com", plats) == ["two.com", "rival.com"]
    assert serp_rivals({"a.com": 3, "b.com": 2}, 5, "me.com", plats) == ["a.com"]   # 바닥 3
    many = {f"r{i:02d}.com": 20 + i for i in range(15)}
    assert serp_rivals(many, 100, "me.com", plats)[:2] == ["r14.com", "r13.com"]
    assert len(serp_rivals(many, 100, "me.com", plats)) == SERP_RIVAL_MAX

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

    m, c, others, rec = judge("Try Ecrett and MySite today.",
                              ["https://www.ecrett.com/a", "https://blog.mysite.com/b"],
                              ["MySite"], "mysite.com")
    assert (m, c, others, rec) == (1, 1, ["ecrett.com"], 0), (m, c, others, rec)
    assert judge("nothing here", [], ["MySite"], "mysite.com") == (0, 0, [], 0)
    # 추천 목록 — 이름이 목록 줄 **안에** 있어야 추천이다. 문단에서 지나가면 이름 나옴뿐
    listed = "좋은 도구들:\n1. Ecrett — 빠름\n2. MySite — 무료\n\n그 밖에 Other 도 있습니다."
    assert judge(listed, [], ["MySite"], "mysite.com")[3] == 1
    assert recommended_in("- **MySite**: 가볍다", ["mysite"]) == 1          # 글머리
    assert recommended_in("| MySite | 무료 |", ["MySite"]) == 1              # 비교표 행
    assert recommended_in("### 2) MySite", ["MySite"]) == 1                 # 번호 제목
    prose = "1. Ecrett 이 낫습니다.\n2. Other 도 있습니다.\nMySite 는 목록 밖에서만 나옵니다."
    assert judge(prose, [], ["MySite"], "mysite.com")[::3] == (1, 0), "목록 밖 언급을 추천으로 셌다"
    assert recommended_in("**MySite** 는 굵게 쓴 문단", ["MySite"]) == 0     # 굵은 글씨는 목록이 아니다
    assert set(AI_LADDER_CATEGORIES) <= set(__import__("gen_prompts").CATEGORIES), \
        "사다리 갈래가 gen_prompts.CATEGORIES 에 없는 이름이다"
    # 인용 공백 판정 — 비율과 표본 수
    assert ai_is_gap(0, 6) and ai_is_gap(1, 6) and ai_is_gap(2, 6)       # 1/3 까지
    assert not ai_is_gap(3, 6) and not ai_is_gap(0, 0)
    assert ai_cite_label(1, 6) == "인용 1/6 (n=6)"
    assert ai_cite_label(0, 2) == "인용 0/2 (n=2) · 표본 부족"
    assert is_third_party("ko.wikipedia.org", ("wikipedia.org",))
    assert not is_third_party("notwikipedia.org", ("wikipedia.org",))
    # 1/6 은 0/6 보다 벌 몫은 작지만 닿기 쉽다 — 둘 다 기회 점수를 받고, 표본 부족은 깎인다
    g0 = score("ai_citation_gap", {"cite_rate": 0.0}, "saas")
    g1 = score("ai_citation_gap", {"cite_rate": 1 / 6}, "saas")
    assert g1 > 0 and abs(g0 - g1) < 5, (g0, g1)
    assert score("ai_citation_gap", {"cite_rate": 0.0, "thin": True}, "saas") < g0
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
    # 경쟁사로 읽기 — manual 이 앞, 우리·플랫폼은 빠지고 뺀 것은 따로 돌려준다
    conn.executemany("INSERT INTO competitors(project_id, domain, source) VALUES(1,?,?)",
                     [("m.blog.naver.com", "auto_rank"), ("shop.selfcheck.com", "auto_rank"),
                      ("auto.com", "auto_labs"), ("hand.com", "manual")])
    assert rivals(conn, 1, "selfcheck.com", ("blog.naver.com",)) == (
        ["ecrett.com", "hand.com", "auto.com"], ["m.blog.naver.com"])
    conn.execute("DELETE FROM competitors WHERE domain != 'ecrett.com'")
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
               "h1_json": '["밀리아 제거 비용"]', "h2_json": '["가격", "회복"]', "words": 900,
               "schema_json": '["Article"]', "canonical": "https://x.com/a",
               "robots": "index,follow", "images_no_alt": 0, "internal_links": 8,
               "external_links": 2}
    assert page_advice(ok_page, ["밀리아 제거"], domain="x.com") == [],         page_advice(ok_page, ["밀리아 제거"], domain="x.com")
    bad = dict(ok_page, title="가격표", meta_description="", h1_json='["A","B"]',
               words=80, schema_json="[]", robots="noindex", images_no_alt=3,
               internal_links=1)
    tags = [a["tag"] for a in page_advice(bad, ["밀리아 제거"], domain="x.com")]
    assert tags == ["title", "title", "meta description", "H1", "본문",
                    "구조화 데이터", "robots", "이미지", "내부 링크"], tags
    assert page_advice({"error": "HTTP 404 · text/html"})[0]["level"] == "bad"
    assert page_advice(None) == []

    # ── title/H1 은 글자가 아니라 말로 대조한다 ──
    # 실제 사례(theotherskin.com 한관종·비립종): title 'Syringoma & Milia | …' 에 검색어
    # 'syringoma vs milia' 가 "없다"며 그대로 박으라고 시켰다. 'vs'·'&'·어순은 구글이
    # 안 보는 것이고, 내용어 둘은 이미 title 에 있다.
    def _tags(a, qs):
        return [x["tag"] for x in page_advice(a, qs, domain="theotherskin.com")]
    skin_q = ["syringoma vs milia", "milia vs syringoma", "milia removal seoul",
              "difference between milia and syringoma", "syringoma or milia",
              "milia and syringoma", "milia vs syringomas", "syringoma"]
    skin_url = "https://theotherskin.com/en/special-clinic/syringoma-milia/"
    skin = {"url": skin_url, "canonical": skin_url,
            "title": "Syringoma & Milia | The Other Dermatology, Seoul",
            "h1_json": '["Syringoma & Milia"]', "h2_json": "[]", "words": 766,
            "meta_description": "Syringoma and Milia Similar-looking bumps, completely "
                                "different anatomical answers The small bumps that appear "
                                "around the eyes may look similar, but their…",
            "schema_json": '["MedicalWebPage"]', "robots": "", "images_no_alt": 0, "internal_links": 12,
            "external_links": 0, "js_shell": 0, "viewport": "width=device-width",
            "html_lang": "en", "hreflang_json": "[]", "tables": 0, "lists": 1,
            "h2_questions": 0, "lead_words": 30, "author": "x"}
    skin_adv = page_advice(skin, skin_q, domain="theotherskin.com")
    skin_tags = [x["tag"] for x in skin_adv]
    assert "title" not in skin_tags and "H1" not in skin_tags, skin_adv
    # 놓치던 큰 것 넷이 이제 잡히고, 순서는 위에서 아래로(설명 → H2 → 외부 링크 → 비교)
    assert skin_tags == ["meta description", "H2", "외부 링크", "비교"], skin_tags
    assert "긁어 온" in skin_adv[0]["now"], skin_adv[0]
    # 복수 접기 — 'syringomas' 는 'syringoma' 가 있는 title 에 있는 말이다(반대 방향도)
    assert _missing_tokens("milia vs syringomas", skin["title"]) == []
    assert "title" not in _tags(dict(skin, title="Syringomas: what they are | The Other"),
                                ["syringoma"])
    # 아주 딴 title → bad. 내용어 셋 중 하나만 있으면 → warn(절반 미만). 둘 중 하나면 아무 말 없음
    assert [x["level"] for x in page_advice(dict(skin, title="Book a Visit | The Other"),
                                            skin_q) if x["tag"] == "title"] == ["bad"]
    assert [x["level"] for x in page_advice(dict(skin, title="Milia Treatment Guide for Eyelids"),
                                            ["milia removal seoul"]) if x["tag"] == "title"] == ["warn"]
    assert "title" not in _tags(dict(skin, title="Milia Treatment Guide for Eyelids"),
                                ["milia removal"])
    assert "그대로 넣으세요" not in " ".join(x["fix"] for x in page_advice(
        dict(skin, title="Book a Visit | The Other"), skin_q)), "검색어를 글자 그대로 박으라고 시킨다"
    # H1 — 내용어가 하나도 없을 때만, 주제를 말하라는 지시로
    h1_adv = [x for x in page_advice(dict(skin, h1_json='["Special Clinic"]'), skin_q)
              if x["tag"] == "H1"]
    assert len(h1_adv) == 1 and h1_adv[0]["level"] == "warn" and "주제" in h1_adv[0]["fix"], h1_adv
    assert "H1" not in _tags(dict(skin, h1_json='["Milia"]'), skin_q)
    # 검색어가 없으면 title/H1 대조 자체를 안 한다 (전과 같다) — 아주 딴 title 이어도
    assert not {"title", "H1"} & set(_tags(dict(skin, title="Book a Visit | The Other",
                                                h1_json='["Special Clinic"]'), []))
    # H2 없음 — 긴 글에서만. 짧은 글은 이미 "얇다"가 붙는다
    assert "H2" not in _tags(dict(skin, words=120), skin_q)
    assert "H2" in _tags(dict(skin, words=H2_MIN_WORDS), skin_q)
    # 긁어 온 설명 — 두 단서 각각: H1 로 시작하며 부호 없음 / H1 없이 말줄임으로 끝남
    assert _desc_scraped("Syringoma and Milia Similar-looking bumps, completely different",
                         skin["title"], "Syringoma & Milia")
    assert _desc_scraped("The small bumps around the eyes may look similar, but their…",
                         skin["title"], "Syringoma & Milia")
    # 손으로 쓴 것은 안 잡는다
    assert not _desc_scraped("Syringoma & Milia: two look-alike bumps with different causes. "
                             "How our Seoul clinic tells them apart and treats each.",
                             skin["title"], "Syringoma & Milia")
    assert not _desc_scraped(ok_page["meta_description"], ok_page["title"], "밀리아 제거 비용")
    hand = dict(skin, meta_description="Syringoma & Milia: two look-alike bumps with different "
                                       "causes. How our Seoul clinic tells them apart.")
    assert "meta description" not in _tags(hand, skin_q), _tags(hand, skin_q)
    # description 지적은 한 페이지에 하나 — 긁어 온 것이 길이보다 앞선다
    assert _tags(dict(skin, meta_description="Syringoma and Milia Similar bumps…"), skin_q) \
        .count("meta description") == 1
    # 외부 링크 0 — 긴 글에서만; 옛 행(칸 NULL)에는 아무 말 없음
    assert "외부 링크" not in _tags(dict(skin, words=100), skin_q)
    assert "외부 링크" not in _tags(dict(skin, external_links=None), skin_q)
    assert "YMYL" in next(x["fix"] for x in skin_adv if x["tag"] == "외부 링크")
    # 비교 의도 + 표 0 → 경고, 표가 있으면 없음, 비교 검색어가 아니면 없음, 옛 행·JS 껍데기면 없음
    assert "비교" not in _tags(dict(skin, tables=1), skin_q)
    assert "비교" not in _tags(skin, ["milia removal seoul", "syringoma"])
    assert "비교" in _tags(skin, ["milia removal seoul", "syringoma or milia"])   # 상위 묶음 중 하나면 된다
    assert "비교" not in _tags(dict(skin, tables=None), skin_q)
    assert "비교" not in _tags(dict(skin, js_shell=1), skin_q)
    assert _is_comparative("한관종 비립종 차이") and not _is_comparative("한관종 제거 비용")
    # canonical 이 남을 가리키면 경고가 아니라 결함이다
    other = page_advice(dict(ok_page, canonical="https://competitor.com/a"),
                        ["밀리아 제거"], domain="x.com")
    assert [a["tag"] for a in other] == ["canonical"] and other[0]["level"] == "bad", other

    # 추출성 — AI 종류에서만, 새 칸을 읽은 행에서만. 옛 행(칸 NULL)에 "표 0" 을 지어내지
    # 않고, 클릭률 같은 일반 종류에는 아무 말도 안 한다(page_advice 를 부풀리지 않는다).
    flat = dict(ok_page, h2_json='["비용", "기간"]', tables=0, lists=0, h2_questions=0,
                lead_words=0, author="", js_shell=0)
    bot = extract_advice(flat, "ai_citation_gap")
    aio = extract_advice(flat, "aio_exposure")
    assert {x["tag"] for x in bot} == {"추출성", "저자"}, bot
    assert {x["tag"] for x in aio} == {"읽기 구조", "저자"}, aio
    assert [x["fix"] for x in bot if x["tag"] == "추출성"] != \
        [x["fix"] for x in aio if x["tag"] == "읽기 구조"], "구글과 챗봇이 같은 말을 한다"
    assert extract_advice(flat, "ctr_gap") == [], "일반 종류에 추출성 진단이 붙는다"
    assert extract_advice(ok_page, "ai_citation_gap") == [], "옛 행에 없는 문제를 만든다"
    # 옛 행이라도 js_shell 은 먼저 생긴 칸이라 1 일 수 있다 — 칸을 안 읽은 행에 "본문
    # 구조를 못 봤다"를 붙이면 가드(_has_extract_fields)가 없는 것과 같다
    assert extract_advice(dict(ok_page, js_shell=1), "ai_citation_gap") == [], \
        "옛 행(추출성 칸 NULL)에 JS 껍데기 확인 지시를 붙인다"
    assert extract_advice(dict(flat, tables=2, lists=1, h2_questions=1, lead_words=45,
                               author="홍"), "ai_citation_gap") == []
    # 갱신 — AI 종류는 180일, 일반(page_advice)은 730일. 둘 사이의 글이 가르는 자리다.
    mid = str(datetime.date.today() - datetime.timedelta(days=300))
    assert [x["tag"] for x in extract_advice(dict(ok_page, modified=mid),
                                             "aio_exposure")] == ["갱신"]
    assert "갱신" not in [x["tag"] for x in page_advice(dict(ok_page, modified=mid),
                                                        ["밀리아 제거"], domain="x.com")]

    conn.executemany(
        "INSERT INTO opportunities(project_id,kind,target,score,reasoning,status,created_at) "
        "VALUES(1,'striking_distance',?,?,'r',?,'2026-08-01')",
        [("old-done", 99, "done"), ("new-low", 10, "new"), ("new-high", 50, "new")])
    # 화면 목록은 심사(작업 판정)를 통과한 검색어만 낸다
    conn.executemany("INSERT INTO verdicts(project_id,key,verdict) VALUES(1,?,'work')",
                     [(norm(t),) for t in ("old-done", "new-low", "new-high")])
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

    # 순위 구간 표를 펼쳤을 때 딸린 검색어 목록 — was 가 실제로 직전 구간을 가리키는지.
    # x1 은 4–10위(08-01)에서 1–3위(08-15)로 넘어왔다 — was 가 그 이전 구간 이름이어야
    # 한다. x3 은 08-15 에만 있으므로 was 는 None. tiny 는 노출 5 < SHIFT_MIN_IMP라
    # 목록·개수 어느 쪽에도 안 잡혀야 한다 (세는 기준과 보여주는 기준이 같아야 한다).
    conn.executemany(
        "INSERT INTO gsc_snapshots(project_id, snapshot_date, period_days, query, clicks, impressions, ctr, position) "
        "VALUES(10, ?, 28, ?, ?, ?, 0.0, ?)",
        [("2026-08-01", "x1", 5, 100, 8.0), ("2026-08-01", "x2", 1, 100, 25.0),
         ("2026-08-15", "x1", 20, 100, 2.0), ("2026-08-15", "x3", 2, 50, 2.5),
         ("2026-08-15", "tiny", 1, 5, 2.0)])
    r10 = {b["label"]: b for b in rank_bands(conn, 10, "2026-08-15", "2026-08-01", 28)}
    q13 = r10["1–3위"]["queries"]
    assert r10["1–3위"]["n"] == 2, r10["1–3위"]                      # tiny 는 노출 하한 미달이라 안 센다
    assert [q["q"] for q in q13] == ["x1", "x3"], q13                # 클릭 내림차순
    assert q13[0] == {"q": "x1", "clicks": 20, "impressions": 100,
                      "position": 2.0, "was": "4–10위"}, q13[0]      # 구간을 넘어온 검색어
    assert q13[1]["was"] is None, q13[1]                             # 직전엔 없던 검색어
    assert "tiny" not in [q["q"] for q in q13], q13

    # 상한(DETAIL_TOP_N) — 넘치면 클릭 상위만 남기고 자른다.
    conn.executemany(
        "INSERT INTO gsc_snapshots(project_id, snapshot_date, period_days, query, clicks, impressions, ctr, position) "
        "VALUES(10, '2026-08-15', 28, ?, ?, 40, 0.0, 25.0)",
        [(f"z{i}", i) for i in range(1, 18)])                        # 17개 — 상한(15) 초과
    z = {b["label"]: b for b in rank_bands(conn, 10, "2026-08-15", None, 28)}["21위+"]["queries"]
    assert len(z) == DETAIL_TOP_N == 15, len(z)
    assert [q["q"] for q in z[:3]] == ["z17", "z16", "z15"], z[:3]

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
    assert kind_label("ctr_gap", band="page1") == "클릭률 미달"          # band 는 striking_distance 전용
    assert kind_play("striking_distance", band="page1")["what"].startswith("이미 1페이지 안입니다")
    assert kind_play("striking_distance", band="page2")["what"].startswith("1페이지 진입까지")
    assert kind_play("striking_distance")["what"].startswith("1페이지 진입까지")     # 모르면 page2
    assert kind_play("content_gap", gap_kind="weak")["what"].startswith("경쟁 도메인이 나보다 위에")
    assert kind_play("content_gap", gap_kind="missing")["what"].startswith("경쟁 도메인은 잡고 있는데")
    assert kind_play("content_gap")["what"].startswith("경쟁 도메인은 잡고 있는데")   # 모르면 missing
    assert kind_play("ctr_gap")["acts"], "정적 kind 의 처방이 비었다"
    assert kind_play("없는kind") == {}
    # 구글 AI 요약은 순위로 갈린다. 모르면 beyond — "이미 1페이지"라고 지어내지 않는다.
    # 어느 쪽이든 FAQ 구조화 데이터를 시키지 않는다(test_brief 가 요청문까지 본다).
    assert aio_band(4) == "page1" and aio_band(PAGE1) == "page1"
    assert aio_band(PAGE1 + 1) == "beyond" and aio_band(None) == "beyond"
    assert kind_play("aio_exposure", band="page1")["what"].startswith("이미 1페이지 안인데")
    assert kind_play("aio_exposure", band="beyond")["what"].startswith("구글이 이 검색어에")
    assert kind_play("aio_exposure") is _AIO_PLAY["beyond"]
    assert set(AIO_BANDS) == {"page1", "beyond"}
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
    assert ix[3]["verdict"] == "PASS" and "색인되지 않았습니다" in ix[3]["detail"], ix[3]
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

    # ── 수요 구성 축 (프로젝트 9): 브랜드·intent·cluster·국가 ──
    conn.executemany(
        "INSERT INTO gsc_snapshots(project_id, snapshot_date, period_days, query, page, clicks, impressions, ctr, position) "
        "VALUES(9, ?, 28, ?, NULL, ?, ?, 0.0, ?)",
        [("2026-08-07", "마이사이트 로그인", 50, 100, 2.0),
         ("2026-08-07", "가격 비교", 10, 500, 8.0),
         ("2026-08-14", "마이사이트 로그인", 60, 120, 1.5),
         ("2026-08-14", "가격 비교", 20, 400, 6.0),
         ("2026-08-14", "새 검색어", 5, 200, 11.0),
         ("2026-08-14", "정보 검색어", 3, 300, 9.0)])
    conn.executemany(
        "INSERT INTO keywords(project_id, keyword, cluster, intent, is_active) VALUES(9, ?, ?, ?, ?)",
        [("가격 비교", "가격", "transactional", 1),
         ("마이사이트 로그인", "브랜드", "navigational", 1),
         ("정보 검색어", "가격", "Informational", 1),   # 사람이 넣은 다른 표기
         ("끈 키워드", "가격", "info", 0)])          # 비활성은 지도에 안 넣는다

    assert canon_intent("Informational") == "info" and canon_intent(" info ") == "info"
    assert canon_intent("commercial") == "commercial" and canon_intent(None) == ""

    assert is_brand_query("마이사이트 로그인", ["마이사이트"])
    assert is_brand_query("MySite Pricing", ["MySite"])            # norm 이 대소문자·공백을 접는다
    assert not is_brand_query("가격 비교", ["마이사이트"])
    assert not is_brand_query("마이사이트", [])                     # 별칭 없으면 아무것도 브랜드가 아니다

    bs = brand_split(conn, 9, "2026-08-14", "2026-08-07", 28, ["마이사이트"])
    assert [r["label"] for r in bs] == ["brand", "nonbrand"], bs    # 순서 고정 — 화면이 그 순서로 읽는다
    assert bs[0]["clicks"] == 60 and bs[0]["d_clicks"] == 10 and bs[0]["ctr"] == 50.0, bs[0]
    assert bs[0]["d_position"] == 0.5, bs[0]                        # 2.0 → 1.5, 양수가 좋아진 쪽
    assert bs[1]["clicks"] == 28 and bs[1]["impressions"] == 900, bs[1]
    # (6×400 + 11×200 + 9×300)/900, 노출 가중
    assert bs[1]["position"] == 8.1, bs[1]
    # 갈래에 속한 검색어 목록 — d_clicks 는 직전 대비, 직전에 없던 검색어는 0 이 아니라 None.
    assert bs[0]["top"] == [{"q": "마이사이트 로그인", "clicks": 60, "impressions": 120,
                            "position": 1.5, "d_clicks": 10}], bs[0]["top"]
    assert [t["q"] for t in bs[1]["top"]] == ["가격 비교", "새 검색어", "정보 검색어"], bs[1]["top"]
    assert bs[1]["top"][0]["d_clicks"] == 10, bs[1]["top"][0]           # 직전에도 있던 검색어
    assert bs[1]["top"][1]["d_clicks"] is None, bs[1]["top"][1]         # 직전엔 없던 검색어(0 이 아니다)
    assert brand_split(conn, 9, "2026-08-14", "2026-08-07", 28, []) == []   # 별칭 없으면 판정 불가

    it_rows = keyword_perf(conn, 9, "2026-08-14", "2026-08-07", 28, "intent")
    # 클릭 내림차순 + 미분류 맨 아래. 노출순으로 세우면 transactional(400노출/20클릭)이
    # 맨 위에 오는데, 화면은 "클릭이 가장 많은 갈래"를 지목하므로 표와 문장이 어긋난다.
    assert [r["label"] for r in it_rows] == ["navigational", "transactional", "info",
                                             UNCLASSIFIED], it_rows
    it = {r["label"]: r for r in it_rows}
    assert it["transactional"]["clicks"] == 20 and it["navigational"]["clicks"] == 60
    assert it["info"]["clicks"] == 3, it            # 'Informational' 이 info 로 접힌다
    assert it[UNCLASSIFIED]["clicks"] == 5, it                      # 매칭 안 된 검색어를 버리지 않는다
    assert sum(r["clicks"] for r in it.values()) == 88              # 합이 총계와 맞는다
    cl = {r["label"]: r for r in keyword_perf(conn, 9, "2026-08-14", "2026-08-07", 28, "cluster")}
    assert set(cl) == {"가격", "브랜드", UNCLASSIFIED}, cl
    assert cl["가격"]["queries"] == 2 and cl["가격"]["d_impressions"] == 200, cl["가격"]
    assert cl[UNCLASSIFIED]["unclassified"] and not cl["가격"]["unclassified"], cl
    # brand_split·keyword_perf 가 _bucket_perf 를 공유하니 여기서도 top 이 클릭 내림차순.
    assert [t["q"] for t in cl["가격"]["top"]] == ["가격 비교", "정보 검색어"], cl["가격"]["top"]
    assert keyword_perf(conn, 9, None, None, None, "intent") == []  # 수집이 없으면 빈 목록

    # top 상한(DETAIL_TOP_N) — 넘치면 클릭 상위만 남기고 자른다.
    conn.executemany(
        "INSERT INTO gsc_snapshots(project_id, snapshot_date, period_days, query, clicks, impressions, ctr, position) "
        "VALUES(11, '2026-08-14', 28, ?, ?, 50, 0.0, 5.0)",
        [(f"w{i}", i) for i in range(1, 18)])                        # 17개 — 상한(15) 초과
    top11 = brand_split(conn, 11, "2026-08-14", None, 28, ["없는브랜드"])[1]["top"]
    assert len(top11) == DETAIL_TOP_N == 15, len(top11)
    assert [t["q"] for t in top11[:3]] == ["w17", "w16", "w15"], top11[:3]

    assert country_perf(conn, 9) == []                              # 국가는 아직 안 캤다
    conn.executemany(
        "INSERT INTO gsc_breakdown(project_id, snapshot_date, period_days, dim, dim_value, query, clicks, impressions, ctr, position) "
        "VALUES(9, ?, 28, 'country', ?, ?, ?, ?, 0.0, ?)",
        [("2026-08-07", "kor", "q1", 5, 100, 10.0),
         ("2026-08-14", "kor", "q1", 10, 200, 5.0),
         ("2026-08-14", "usa", "q2", 1, 50, 20.0)])
    cp = country_perf(conn, 9)
    assert [r["label"] for r in cp] == ["kor", "usa"], cp           # 클릭 내림차순
    assert cp[0]["d_clicks"] == 5 and cp[0]["d_position"] == 5.0 and cp[0]["has_prev"], cp[0]
    assert cp[1]["ctr"] == 2.0 and not cp[1]["has_prev"], cp[1]     # 이번에 처음 뜬 나라

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
    assert "sessions" not in pp[0], pp[0]        # GA4 미연결 — 열 자체가 없다(빈 열 아님)
    assert zero_conversion_pages(conn, 8) == []  # GA4 미연결이면 이 판정 자체가 없다

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

    # ── GA4 교차: 페이지 실적에 세션·전환을 얹고, 전환 0 페이지를 잡고, 의도별
    # 근사를 낸다 (프로젝트 8, 위 GSC 픽스처를 그대로 잇는다). 클릭 하한(20)·세션
    # 하한(10) 시험용으로 페이지 둘을 더 건다.
    conn.executemany(
        "INSERT INTO gsc_snapshots(project_id, snapshot_date, period_days, query, page,"
        " clicks, impressions, ctr, position) VALUES(8, '2026-08-08', 28, ?, ?, ?, ?, 0.0, ?)",
        [("q1", "https://www.x.com/flat", 25, 400, 6.0),
         ("q1", "https://www.x.com/paid", 30, 500, 7.0),
         ("q2", "https://www.x.com/low", 10, 150, 8.0)])
    conn.executemany(
        "INSERT INTO keywords(project_id, keyword, cluster, intent, is_active) VALUES(8, ?, ?, ?, 1)",
        [("q1", None, "transactional"), ("q2", None, "info")])
    conn.executemany(
        # sessions 는 이제 **유기 검색만**이고 sessions_all 이 전 채널이다(collect_ga4).
        # bounce_rate 는 engagement_rate 의 여집합이라 컬럼째 걷어냈다 — 값도 뒤집어 옮긴다.
        "INSERT INTO ga4_snapshots(project_id, snapshot_date, period_days, landing_page,"
        " sessions, key_events, total_revenue, engagement_rate, sessions_all)"
        " VALUES(8, '2026-08-10', 28, ?, ?, ?, ?, ?, ?)",
        [("/dying/", 12, 3, 50.0, 0.6, 18),   # 클릭 15 < GA4_NOCONV_MIN_CLICKS(20) — 굶은 축엔 안 걸린다
         ("/rising", 15, 0, 0.0, 0.4, 22),    # 클릭 20·세션 15·전환 0 — 이 판정의 표본 케이스
         ("/flat", 3, 0, 0.0, 0.5, 5),        # 클릭 25 지만 세션 3 < 하한(10) — 제외
         ("/paid", 20, 2, 80.0, 0.7, 31),     # 전환이 있다 — 제외
         ("/low", 12, 0, 0.0, 0.3, 15),       # 세션 12 는 하한을 넘지만 클릭 10 < 하한(20) — 제외
         ("/orphan", 5, 0, 0.0, 0.1, 9)])     # GSC 에 없는 경로 — 어느 결과에도 안 새어 나온다

    pp2 = {r["page"]: r for r in page_performance(conn, 8)}
    assert (pp2["https://www.x.com/dying/"]["sessions"],
           pp2["https://www.x.com/dying/"]["key_events"]) == (12, 3.0), pp2["https://www.x.com/dying/"]
    assert (pp2["https://www.x.com/rising"]["sessions"],
           pp2["https://www.x.com/rising"]["key_events"]) == (15, 0.0), pp2["https://www.x.com/rising"]
    assert pp2["https://www.x.com/paid"]["sessions"] == 20, pp2["https://www.x.com/paid"]

    zc = zero_conversion_pages(conn, 8)
    assert [r["page"] for r in zc] == ["https://www.x.com/rising"], zc  # 나머지는 하한/전환 있음으로 빠진다
    assert (zc[0]["clicks"], zc[0]["sessions"], zc[0]["queries"]) == (20, 15, 1), zc[0]

    assert ga4_intent_approx(conn, 8, None, None) == []            # 기준 수집일이 없으면 빈 목록
    assert ga4_intent_approx(conn, 9, "2026-08-14", 28) == []      # GA4 미연결 프로젝트는 빈 목록
    gi = ga4_intent_approx(conn, 8, "2026-08-08", 28)
    assert [r["label"] for r in gi] == ["transactional", "info"], gi   # 전환 내림차순
    tr, info = gi
    # transactional = q1 이 걸린 4페이지(dying·rising·flat·paid) 합. dying 은 info(q2)에도
    # 걸려 있어 두 갈래 모두에 전환이 중복으로 잡힌다 — shared_pages 가 그걸 센다.
    assert (tr["pages"], tr["pages_matched"], tr["sessions"], tr["key_events"], tr["shared_pages"]) \
        == (4, 4, 50, 5.0, 1), tr
    assert (info["pages"], info["pages_matched"], info["sessions"], info["key_events"], info["shared_pages"]) \
        == (2, 2, 24, 3.0, 1), info    # info = q2 가 걸린 dying·low 합

    # ── ga4_funnel(): GSC 축(2026-08-08, period 28)·GA4 축(2026-08-10) 을 나란히.
    # 위 GA4 픽스처(프로젝트 8) 6행을 그대로 쓴다 — sessions 합 67, sessions_all 합
    # 100, key_events 합 5.0, revenue 합 130.0, engaged = Σ(sessions*engagement_rate)
    # = 7.2+6.0+1.5+14.0+3.6+0.5 = 32.8 → round 33. GSC 는 프로젝트 8·2026-08-08·
    # period 28 전체(위 세 판정이 나눠 쓴 6행) 합 — 클릭 100, 노출 2430.
    assert ga4_funnel(conn, 8, None, 28) == (None, None)           # 기준 수집일이 없으면 부재
    assert ga4_funnel(conn, 9, "2026-08-08", 28) == (None, None)   # GA4 미연결 프로젝트는 부재
    funnel, channels = ga4_funnel(conn, 8, "2026-08-08", 28)
    assert funnel == {"impressions": 2430, "clicks": 100, "sessions": 67,
                      "engaged_sessions": 33, "key_events": 5.0, "revenue": 130.0}, funnel
    assert channels == {"organic": 67, "all": 100}, channels

    # ── ga4_breakdown(): device·country·newvsreturning 을 세션 내림차순으로.
    conn.executemany(
        "INSERT INTO ga4_breakdown(project_id, snapshot_date, period_days, dim, dim_value,"
        " landing_page, sessions, key_events, total_revenue, engagement_rate)"
        " VALUES(8, '2026-08-10', 28, 'device', ?, ?, ?, ?, ?, ?)",
        [("MOBILE", "/rising", 15, 1.0, 20.0, 0.5),   # MOBILE 은 두 landing_page 에 걸쳐 있다
         ("MOBILE", "/paid", 5, 0.5, 5.0, 0.3),       # → 합산·세션가중 engagement_rate 확인용
         ("DESKTOP", "/rising", 10, 2.0, 40.0, 0.6)])
    conn.executemany(
        "INSERT INTO ga4_breakdown(project_id, snapshot_date, period_days, dim, dim_value,"
        " landing_page, sessions, key_events, total_revenue, engagement_rate)"
        " VALUES(8, '2026-08-10', 28, 'newvsreturning', ?, '/rising', ?, ?, ?, ?)",
        [("new", 12, 1.0, 10.0, 0.4), ("returning", 8, 1.0, 15.0, 0.7)])
    # 10개국 — GA4_BD_COUNTRY_TOP(8)을 넘겨 접힘을 시험한다. engagement_rate 를
    # 전부 0.5로 둬 접힌 "기타"의 세션가중 평균도 그대로 0.5가 되게 한다.
    _countries = [("kor", 100), ("usa", 90), ("jpn", 80), ("gbr", 70), ("deu", 60),
                 ("fra", 50), ("can", 40), ("aus", 30), ("ind", 20), ("bra", 10)]
    conn.executemany(
        "INSERT INTO ga4_breakdown(project_id, snapshot_date, period_days, dim, dim_value,"
        " landing_page, sessions, key_events, total_revenue, engagement_rate)"
        " VALUES(8, '2026-08-10', 28, 'country', ?, '/rising', ?, ?, ?, 0.5)",
        [(cc, s, s / 10.0, float(s)) for cc, s in _countries])

    assert ga4_breakdown(conn, 9, "device") == []                  # GA4 미연결이면 빈 목록
    bd_dev = ga4_breakdown(conn, 8, "device")
    assert [r["label"] for r in bd_dev] == ["MOBILE", "DESKTOP"], bd_dev  # 세션 내림차순
    assert (bd_dev[0]["sessions"], bd_dev[0]["key_events"], bd_dev[0]["revenue"],
           bd_dev[0]["engagement_rate"]) == (20, 1.5, 25.0, 0.45), bd_dev[0]  # 두 행 합산
    assert bd_dev[1]["engagement_rate"] == 0.6, bd_dev[1]

    bd_nr = ga4_breakdown(conn, 8, "newvsreturning")
    assert [r["label"] for r in bd_nr] == ["new", "returning"], bd_nr

    bd_co_full = ga4_breakdown(conn, 8, "country")            # top 없으면 안 접는다
    assert len(bd_co_full) == 10, bd_co_full
    bd_co = ga4_breakdown(conn, 8, "country", top=GA4_BD_COUNTRY_TOP)
    assert [r["label"] for r in bd_co] == \
        ["kor", "usa", "jpn", "gbr", "deu", "fra", "can", "aus", "기타"], bd_co
    assert (bd_co[-1]["sessions"], bd_co[-1]["key_events"], bd_co[-1]["revenue"],
           bd_co[-1]["engagement_rate"]) == (30, 3.0, 30.0, 0.5), bd_co[-1]  # ind+bra 접힘

    # ── value_mult(): GA4 매출 잠재력 보정 승수. 위 GA4 픽스처(프로젝트 8)를 그대로
    # 쓴다 — /rising 은 클릭 20·세션 15·전환 0(표본 되고 전환 없음), /paid 는 클릭
    # 30·세션 20·전환 2(표본 되고 전환 있음), /flat·/low 는 표본 미달.
    ga4_ctx = {"conn": conn, "pid": 8, "cur": "2026-08-08",
              "ga4": _ga4_agg(conn, 8, "2026-08-10"),
              "page_agg": _page_agg(conn, 8, "2026-08-08", 28)}
    assert value_mult({}) == 1.0                                    # ① GA4 미연결 — 정확히 1.0
    assert value_mult({"impressions": 100}) == 1.0
    gm_rising = _ga4_metrics(ga4_ctx, page="https://www.x.com/rising")
    assert gm_rising == {"ga4_clicks": 20, "ga4_sessions": 15, "ga4_key_events": 0}, gm_rising
    gm_paid = _ga4_metrics(ga4_ctx, page="https://www.x.com/paid")
    assert gm_paid == {"ga4_clicks": 30, "ga4_sessions": 20, "ga4_key_events": 2}, gm_paid
    assert _ga4_metrics(ga4_ctx, page="https://www.x.com/never-was") == {}   # GA4 에 없는 경로
    assert _ga4_metrics({"ga4": {}}, page="https://www.x.com/rising") == {}  # GA4 미연결(빈 ctx)

    base = score("striking_distance", {"impressions": 500, "position": 5.0}, "saas")
    assert score("striking_distance", {"impressions": 500, "position": 5.0}, "saas") == base  # ① 불변(결정적)

    up = score("striking_distance", {"impressions": 500, "position": 5.0, **gm_paid}, "saas")
    down = score("striking_distance", {"impressions": 500, "position": 5.0, **gm_rising}, "saas")
    assert down < base < up, (down, base, up)                       # ② 전환 있으면 오르고 0이면 내린다

    assert value_mult({"ga4_clicks": 5, "ga4_sessions": 20, "ga4_key_events": 3}) == 1.0    # ③ 클릭 표본 미달
    assert value_mult({"ga4_clicks": 30, "ga4_sessions": 5, "ga4_key_events": 3}) == 1.0    # ③ 세션 표본 미달

    for m in ({"ga4_clicks": 30, "ga4_sessions": 20, "ga4_key_events": 0},
              {"ga4_clicks": 30, "ga4_sessions": 20, "ga4_key_events": 2},
              {"ga4_clicks": 500, "ga4_sessions": 20, "ga4_key_events": 5000}):
        v = value_mult(m)
        assert GA4_MULT_LO <= v <= GA4_MULT_HI, (m, v)               # ④ 범위 [0.85, 1.15] 안
    assert value_mult({"ga4_clicks": 500, "ga4_sessions": 20, "ga4_key_events": 5000}) == GA4_MULT_HI  # 포화

    kb = _KIND_BY_NAME
    cg_m = kb["content_gap"].metrics(
        {"keyword": "k", "domain": "riv.com", "position": 3, "our_position": None, "volume": 100}, ga4_ctx)
    cov_m = kb["coverage"].metrics({"cluster": "clu", "n": 3, "vol": 50}, ga4_ctx)
    bb_m = kb["backlink_broken"].metrics(     # target url_to 는 GA4 실측이 있는 페이지를 일부러 쓴다 —
        {"url_to": "https://www.x.com/rising", "url_from": "https://y.com/b",   # "데이터가 없어서" 가 아니라
         "domain_from": "y.com", "rank": None}, ga4_ctx)                        # "이 kind 는 안 본다"를 증명한다
    for m in (cg_m, cov_m, bb_m):             # ⑤ 페이지 없는 kind — GA4 가 붙어 있어도 안 건드린다
        assert "ga4_sessions" not in m and "ga4_key_events" not in m, m

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
         "rivals": [{"domain": "ecrett.com", "n": 4}, {"domain": "b.com", "n": 1}]},
        {"prompt": "키워드 뭐가 좋아", "category": "추천", "cited": 0, "checks": 6,
         "rivals": []},                             # 한 낱말만 겹친다 — 짝을 안 짓는다
        {"prompt": "ecrett 어때", "category": "브랜드", "cited": 0, "checks": 6,
         "rivals": []},                             # 검색어가 한 낱말이라 임계 미달
        {"prompt": "1페이지 키워드 정리법", "category": "추천", "cited": 4, "checks": 6,
         "rivals": []},                             # 인용이 흔하다 — 여기 올 자리가 아니다
        # 6번 중 1번 — 기회(ai_is_gap)로 서는 질문은 이 표에도 선다. 인용 0회로만
        # 거르던 때는 기회 목록에는 있고 여기서는 빠졌다.
        {"prompt": "1페이지 키워드 비교법", "category": "비교", "cited": 1, "checks": 6,
         "rivals": []},
    ]
    xs = search_wins_ai_loses(conn, 1, ai_rows)
    assert xs["top_queries"] == 2, xs
    assert [r["prompt"] for r in xs["rows"]] == ["1페이지 키워드 고르는 법",
                                                 "1페이지 키워드 비교법"], xs
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
    # ── AI 크롤러 차단 — 새 수집 없이 robots.txt 원문만 다시 읽는다 ──
    _bots_conn = sqlite3.connect(":memory:")
    _bots_conn.row_factory = sqlite3.Row
    import db as _db
    _bots_conn.executescript(_db.SCHEMA)
    _bots_conn.execute("INSERT INTO projects(id,name,type,domain) VALUES(1,'b','saas','x.kr')")
    assert ai_bot_blocks(_bots_conn, 1) == [], "크롤 회차가 없는데 판정을 만든다"
    # 학습 봇만 막은 흔한 꼴 — 권장되는 중간 지점이지 인용 차단이 아니다
    _robots = chr(10).join(("User-agent: GPTBot", "Disallow: /", "",
                            "User-agent: CCBot", "Disallow: /", "",
                            "User-agent: *", "Allow: /"))
    _bots_conn.execute(
        "INSERT INTO crawl_runs(project_id,finished_at,seed,robots_txt)"
        " VALUES(1,'2026-09-09','home',?)", (_robots,))
    _bots_conn.commit()
    assert ai_bot_blocks(_bots_conn, 1, home="x.kr") == [], "학습 봇 차단을 인용 차단으로 읽는다"
    _st = {r["bot"]: r for r in ai_bot_status(_robots, "x.kr")}
    assert _st["GPTBot"]["rule"] == "Disallow: /" and _st["GPTBot"]["purpose"] == "training", _st
    assert _st["OAI-SearchBot"]["rule"] is None, _st      # 같은 벤더라도 검색 봇은 열려 있다
    # 검색 봇이 막히면 선다
    _bots_conn.execute("UPDATE crawl_runs SET robots_txt=?", (
        "User-agent: OAI-SearchBot\nDisallow: /\n\n" + _robots,))
    _bots_conn.commit()
    _blocked = ai_bot_blocks(_bots_conn, 1, home="x.kr")
    assert [r["bot"] for r in _blocked] == ["OAI-SearchBot"], _blocked
    assert _blocked[0]["rule"] == "Disallow: /" and _blocked[0]["engine"], _blocked[0]
    assert "ChatGPT" in _reason_ai_bot(_blocked[0], {}), _reason_ai_bot(_blocked[0], {})
    # Bingbot 은 빙 검색 전체라고 말한다
    _bing = {"bot": "Bingbot", "purpose": "search", "engine": "Bing 검색·Copilot",
             "rule": "Disallow: /"}
    assert "빙 검색 전체" in _reason_ai_bot(_bing, {})
    # 옛 꼴(UA 문자열 목록) — 읽되 용도는 모름이고, 모름은 기회가 아니다
    assert _ai_bot_entry("OAI-SearchBot") == {"ua": "OAI-SearchBot", "vendor": None,
                                              "purpose": None, "engine": None}
    assert _ai_bot_entry({"ua": "X", "purpose": "시험"})["purpose"] is None   # 모르는 용도도 모름
    _real_ai_bots = globals()["ai_bots"]
    globals()["ai_bots"] = lambda: tuple(map(_ai_bot_entry, ["GPTBot", "OAI-SearchBot"]))
    try:
        assert ai_bot_blocks(_bots_conn, 1, home="x.kr") == [], "용도 모름을 인용 차단으로 읽는다"
        assert [r["purpose"] for r in ai_bot_status(_robots, "x.kr")] == [None, None]
    finally:
        globals()["ai_bots"] = _real_ai_bots
    # User-agent: * 한 줄이 통째로 막으면 기회도 한 줄 — 봇마다 세우면 같은 줄 하나가
    # 기회 목록 일곱 줄로 불어난다. 이름 묶음으로 따로 막힌 봇은 따로 선다.
    _NL = chr(10)
    _wild = _NL.join(("User-agent: *", "Disallow: /"))
    _bots_conn.execute("UPDATE crawl_runs SET robots_txt=?", (_wild,))
    _bots_conn.commit()
    _g = ai_bot_blocks(_bots_conn, 1, home="x.kr")
    assert [r["bot"] for r in _g] == [AI_BOT_WILDCARD], [r["bot"] for r in _g]
    _citing = [b["ua"] for b in ai_bots() if b["purpose"] in AI_BOT_CITING]
    assert sorted(_g[0]["bots"]) == sorted(_citing), _g[0]["bots"]
    assert _g[0]["google_blocked"] is True, _g[0]
    _why = _reason_ai_bot(_g[0], {})
    assert "이 줄 하나" in _why and "구글 검색(Googlebot)도" in _why, _why
    # 개수와 나열이 같은 것을 센다(엔진 이름으로 나열하면 Perplexity 둘이 하나로 준다)
    assert f"{len(_citing)}개(" + "·".join(_g[0]["bots"]) + ")" in _why, _why
    # 이름 묶음으로 따로 막힌 봇 + 와일드카드 — 묶음 하나와 그 봇 하나
    _mixed = _NL.join(("User-agent: PerplexityBot", "Disallow: /", "",
                       "User-agent: *", "Disallow: /"))
    _bots_conn.execute("UPDATE crawl_runs SET robots_txt=?", (_mixed,))
    _bots_conn.commit()
    _m = [r["bot"] for r in ai_bot_blocks(_bots_conn, 1, home="x.kr")]
    assert _m == [AI_BOT_WILDCARD, "PerplexityBot"], _m
    # 구글은 열어 두고 AI 만 와일드카드로 막을 수는 없다 — Googlebot 묶음이 따로 있으면
    # 구글 문구가 빠진다
    _gopen = _NL.join(("User-agent: Googlebot", "Allow: /", "", "User-agent: *", "Disallow: /"))
    _bots_conn.execute("UPDATE crawl_runs SET robots_txt=?", (_gopen,))
    _bots_conn.commit()
    _go = ai_bot_blocks(_bots_conn, 1, home="x.kr")[0]
    assert _go["google_blocked"] is False and "Googlebot" not in _reason_ai_bot(_go, {}), _go
    # 해소: 묶음 기회는 와일드카드가 풀려야 닫힌다. 봇마다 세우던 옛 기회는 묶음으로
    # 옮겨 닫되 "여전히 막혀 있다"고 적는다 — 고쳐진 것으로 읽히면 안 된다.
    _bots_conn.execute("UPDATE crawl_runs SET robots_txt=?, started_at='2030-01-01 00:00:00'",
                       (_wild,))
    _bots_conn.commit()
    _rc = {"domain": "x.kr"}
    assert _resolve_ai_bot(_bots_conn, 1, AI_BOT_WILDCARD, "2029-01-01", _rc) is None
    _legacy = _resolve_ai_bot(_bots_conn, 1, "OAI-SearchBot", "2029-01-01", _rc)
    assert _legacy and "여전히 막혀" in _legacy, _legacy
    _bots_conn.execute("UPDATE crawl_runs SET robots_txt=?",
                       (_NL.join(("User-agent: *", "Disallow: /private")),))
    _bots_conn.commit()
    assert "더는 막지 않습니다" in (_resolve_ai_bot(_bots_conn, 1, AI_BOT_WILDCARD,
                                                   "2029-01-01", _rc) or "")
    # robots.txt 를 아예 못 읽은 회차는 "전부 허용" 이 아니라 "모른다" 다
    _bots_conn.execute("UPDATE crawl_runs SET robots_txt=NULL")
    _bots_conn.commit()
    assert ai_bot_blocks(_bots_conn, 1, home="x.kr") == [], "안 본 것을 허용으로 읽는다"
    _bots_conn.close()
    assert "ai_bot_blocked" in ALL_KINDS
    # 점수에서 AI 축 가중을 받는다 — 안 그러면 목록 바닥에 깔린다
    assert score("ai_bot_blocked", {"impressions": 0, "position": None, "ai": 1.0}, "saas") > 0



if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "load":
        try:
            import argparse

            import db
            import remote
            # 원격 사이트면 서버가 gaps 단계를 돈다. 이 모듈은 argparse 를 안 쓰므로
            # dispatch 가 읽는 두 이름만 세워 준다 (gaps 에는 노브가 없다).
            if not remote.dispatch(argparse.Namespace(project=sys.argv[2], dry_run=False),
                                   "gaps"):
                load(sys.argv[2])
        except (db.ProjectNotFound, db.ProjectConfigNotFound) as e:
            sys.exit(str(e))
    else:
        _selfcheck()
