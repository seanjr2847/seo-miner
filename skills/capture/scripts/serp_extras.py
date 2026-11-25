"""순위 조회(rank 단계)에 덧붙는 선택 수집 — DataForSEO 키가 있을 때만, 켠 것만 돈다.

단계를 새로 만들지 않는다: rank 단계(유료, is_paid)의 하위 걸음이다. 그래서 비용은 그
단계의 runs.cost_estimate_usd 에 같이 실리고, 건너뛴 까닭은 그 단계의 runs.notes 에
`extras=…` 로 남는다. 여기서 난 오류는 순위 조회의 성공·실패를 바꾸지 않는다(순위는 이미
적재됐다) — 개수만 notes 에 적는다.

  intent     DataForSEO Labs search_intent 로 우리 의도 휴리스틱(scoring.classify_intent)을 대조
  images     이미지 칸이 선 검색어의 이미지 SERP — 상위 출처 도메인·alt 꼴
  youtube    영상 칸이 선 검색어의 YouTube SERP — 상위 영상·채널
  bing_rank  Bing 순위(같은 검색어·같은 로케일)
  trends     Google Trends 5년치 → 달별 평균(계절성)
  ai_mode    구글 AI 모드의 인용 — AI 요약(ai_overview)과 **다른 인용 면**이라 따로 적는다

종류의 정본은 아래 KINDS 한 벌이다(이름·기본값·상한·다시 받는 간격·추정 단가). 결과는
db.serp_extras 한 표에 kind 로 갈려 쌓인다. 응답 칸 이름은 공식 문서로 확인했고(주석의
URL), 개발 중 실호출은 하지 않았다 — 파서는 전부 `.get` 으로 읽어 칸이 없으면 None 이다.

self-check:  python serp_extras.py
"""
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import db  # noqa: E402
import scoring  # noqa: E402
import serp_adapter  # noqa: E402

# usd 는 **추정** 단가다 — 문서가 값을 적지 않아 문서 표본 응답의 cost 를 옮겼다(2026-11-24).
# dry-run 고지에만 쓴다. 실청구액은 응답의 cost 로 읽어 runs 에 적는다(post_dataforseo).
#   default  설정(serp_extras)을 안 건드린 사이트에서 도는가. 전부 False 다 — 하나같이 유료라
#            켜 두면 이 기능이 생기기 전부터 쓰던 사이트의 지출이 묻지도 않고 는다(intent 만
#            해도 상한 1000개 × 단가). 켜는 것은 사람이다(사이트 설정 serp_extras · --extras).
#   needs    이 기능이 그 검색어의 검색결과에 섰을 때만 받는다(없으면 받을 까닭이 없다)
#   cap      한 런에 받는 검색어 수 상한      fresh_days  이 안에 받은 것은 다시 안 받는다
KINDS: dict[str, dict] = {
    "intent": {"name": "검색 의도 검증", "default": False, "usd": 0.012, "cap": 1000,
               "fresh_days": 90, "needs": ()},
    "images": {"name": "이미지 SERP", "default": False, "usd": 0.002, "cap": 10,
               "fresh_days": 30, "needs": ("images",)},
    "youtube": {"name": "YouTube SERP", "default": False, "usd": 0.002, "cap": 10,
                "fresh_days": 30, "needs": ("video", "short_videos")},
    "bing_rank": {"name": "Bing 순위", "default": False, "usd": 0.002, "cap": 50,
                  "fresh_days": 1, "needs": ()},
    "trends": {"name": "Google Trends 계절성", "default": False, "usd": 0.009, "cap": 10,
               "fresh_days": 90, "needs": ()},
    "ai_mode": {"name": "구글 AI 모드 인용", "default": False, "usd": 0.004, "cap": 10,
                "fresh_days": 7, "needs": ()},
}
DEFAULT = ",".join(k for k, v in KINDS.items() if v["default"])
# 검색어가 아니라 사이트 단위로 한 줄 남는 종류(serp_extras.keyword_id 가 NULL). 받는 쪽은
# rank 단계가 아니다 — llm_mentions 는 AI 인용 확인 단계(collect_ai)의 하위 걸음이다.
SITE_KINDS: dict[str, dict] = {
    "llm_mentions": {"name": "LLM 언급 집계", "usd": 0.10, "fresh_days": 30},
    # AI 가 실제로 받은 질문 원문 + 질문별 AI 검색량(LLM Mentions search_mentions). 받는 쪽은
    # 질문 만들기(gen_prompts)다 — 지어낸 질문 대신 실제로 들어온 질문을 재료로 싣는다.
    # 값 꼴은 {"by_locale": {시장: {checked_at, platform, targets, questions}}} — 시장마다 따로 받는다.
    # 단가: 과제 $0.1 + 행 $0.001 × 최대 100행(가격표 2026-11-25).
    "llm_questions": {"name": "AI 가 받은 실제 질문", "usd": 0.20, "fresh_days": 30},
}
LLM_Q_LIMIT = 100               # 한 번에 받는 질문 수(행 과금)
LLM_Q_KEEP = 40                 # 시장마다 남기는 질문 수(AI 검색량 큰 순)
# 의도 축 → 사람 말. 축 이름의 정본은 scoring.classify_intent 의 반환값 넷이다.
AXIS_NAME = {"info": "정보", "navigational": "탐색", "commercial": "비교·조사",
             "transactional": "구매"}
KEEP = 10                       # 종류마다 상위 몇 줄을 남기나


def wanted(setting) -> list[str]:
    """설정 글자("intent,images" · "all" · "none" · 비움) → 돌 종류 목록(KINDS 순서).
    모르는 이름은 버린다(오타가 조용히 '전부'가 되지 않게)."""
    if setting is None:
        setting = DEFAULT
    names = {x.strip().lower() for x in str(setting).split(",") if x.strip()}
    if "none" in names:
        return []
    if "all" in names:
        return list(KINDS)
    return [k for k in KINDS if k in names]


def estimate(kinds, n_keywords: int) -> float:
    """dry-run 고지용 상한 — 종류마다 min(검색어 수, cap) × 추정 단가. intent 는 한 번에 묶어 한 건."""
    usd = 0.0
    for k in kinds:
        spec = KINDS[k]
        usd += spec["usd"] * (1 if k == "intent" else min(n_keywords, spec["cap"]))
    return usd


# ── 파서(순수 함수 — 고정 응답으로 검사한다) ──────────────────────────────────

# search_intent 의 이름 → 우리 축 이름(scoring.classify_intent 의 반환값).
# 문서: https://docs.dataforseo.com/v3/dataforseo_labs/google/search_intent/live/
#   items[].keyword · keyword_intent{label, probability} · secondary_keyword_intents[{label, probability}]
#   label ∈ informational | navigational | commercial | transactional
_INTENT_AXIS = {"informational": "info", "navigational": "navigational",
                "commercial": "commercial", "transactional": "transactional"}


def parse_intent(result, ours: dict[str, str]) -> dict[str, dict]:
    """search_intent 결과 → {검색어(소문자): {label, probability, secondary, ours, agree}}.

    ours 는 {검색어 소문자: 우리 휴리스틱 축}. agree 는 **대조 사실**이다 — 어긋났다고
    우리 값을 덮지 않는다(keywords.intent 는 사람·Claude 가 고친 값일 수 있다)."""
    out: dict[str, dict] = {}
    for r0 in result or []:
        for it in (r0 or {}).get("items") or []:
            kw = str((it or {}).get("keyword") or "").strip().lower()
            ki = it.get("keyword_intent") if isinstance(it.get("keyword_intent"), dict) else {}
            label = _INTENT_AXIS.get(str(ki.get("label") or "").lower())
            if not kw or not label:
                continue
            sec = [_INTENT_AXIS[str(s.get("label")).lower()]
                   for s in (it.get("secondary_keyword_intents") or [])
                   if isinstance(s, dict) and str(s.get("label")).lower() in _INTENT_AXIS]
            mine = ours.get(kw)
            out[kw] = {"label": label, "probability": ki.get("probability"), "secondary": sec,
                       "ours": mine,
                       # 둘째 의도와 맞아도 '어긋남'은 아니다 — 한 검색어에 의도가 겹친다
                       "agree": None if mine is None else (mine == label or mine in sec)}
    return out


def parse_images(result, own: str) -> dict:
    """이미지 SERP → {top: [{pos, domain, alt, title}], own: 우리 이미지 자리 목록, alt_filled}.

    문서: https://docs.dataforseo.com/v3/serp/google/images/live/advanced/
      항목 type "images_search": rank_group · title · subtitle · alt · url · source_url · encoded_url
    source_url 이 이미지가 실린 **페이지**다(우리 것인지는 이것으로 가른다)."""
    top, mine = [], []
    for it in _items(result):
        if it.get("type") != "images_search":
            continue
        d = scoring.host_of(it.get("url") or it.get("source_url") or "")
        pos = it.get("rank_group")
        if d and own and scoring.owns(d, own):
            mine.append(pos)
        if len(top) < KEEP:
            top.append({"pos": pos, "domain": d or None, "alt": it.get("alt"),
                        "title": it.get("title")})
    return {"top": top, "own": [p for p in mine if p is not None],
            "alt_filled": sum(1 for t in top if str(t.get("alt") or "").strip())}


def parse_youtube(result) -> dict:
    """YouTube SERP → {videos: [{pos, title, channel, url, views, seconds, shorts}]}.

    문서: https://docs.dataforseo.com/v3/serp/youtube/organic/live/advanced/
      항목 type "youtube_video": rank_group · title · url · video_id · channel_name ·
      views_count · duration_time_seconds · is_shorts · timestamp"""
    vids = []
    for it in _items(result):
        if it.get("type") != "youtube_video":
            continue
        vids.append({"pos": it.get("rank_group"), "title": it.get("title"),
                     "channel": it.get("channel_name"), "url": it.get("url"),
                     "views": it.get("views_count"),
                     "seconds": it.get("duration_time_seconds"),
                     "shorts": bool(it.get("is_shorts"))})
        if len(vids) >= KEEP:
            break
    return {"videos": vids}


def parse_bing(result, own: str, scope="") -> dict:
    """Bing SERP → {position, url, top: [{pos, domain, url, title}]}.

    문서: https://docs.dataforseo.com/v3/serp/bing/organic/live/advanced/
      organic 항목: rank_group · domain · url · title (구글 organic 과 같은 칸 이름)
    내 순위 규칙은 구글 순위와 같다(scoring.owns + 분석 범위 경로)."""
    top, position, url = [], None, None
    for it in _items(result):
        if it.get("type") != "organic":
            continue
        d = scoring.host_of(it.get("url") or it.get("domain") or "")
        if (position is None and d and own and scoring.owns(d, own)
                and scoring.in_scope(it.get("url") or "", scope)):
            position, url = it.get("rank_group"), it.get("url")
        if len(top) < db.SERP_KEEP:
            top.append({"pos": it.get("rank_group"), "domain": d or None,
                        "url": it.get("url"), "title": it.get("title")})
    return {"position": position, "url": url, "top": top}


def parse_trends(result) -> dict | None:
    """Trends 그래프 → {months: {"01": 평균, …}, peak, low, points}. 값이 없으면 None.

    문서: https://docs.dataforseo.com/v3/keywords_data/google_trends/explore/live/
      항목 type "google_trends_graph": data[]{date_from "yyyy-mm-dd", date_to, timestamp,
      missing_data, values[]} — 값은 0~100(그 기간 최고점이 100).
    검색어 하나만 보내므로 values 의 첫 값이 그 검색어다. 달별 평균은 여러 해를 겹쳐 낸
    **사실**이다 — '성수기' 같은 판정은 하지 않고 가장 높은 달·낮은 달만 말한다.
    missing_data 인 점(아직 덜 찬 마지막 구간)은 뺀다."""
    by_month: dict[str, list[float]] = {}
    n = 0
    for it in _items(result):
        if it.get("type") != "google_trends_graph":
            continue
        for pt in it.get("data") or []:
            if not isinstance(pt, dict) or pt.get("missing_data"):
                continue
            vals = pt.get("values")
            v = vals[0] if isinstance(vals, list) and vals else vals
            month = str(pt.get("date_from") or "")[5:7]
            if not isinstance(v, (int, float)) or len(month) != 2:
                continue
            by_month.setdefault(month, []).append(float(v))
            n += 1
    if len(by_month) < 12:          # 열두 달이 다 없으면 달끼리 견줄 수 없다
        return None
    months = {m: round(statistics.fmean(v), 1) for m, v in sorted(by_month.items())}
    if max(months.values()) <= 0:
        return None
    return {"months": months, "peak": max(months, key=months.get),
            "low": min(months, key=months.get), "points": n}


def parse_ai_mode(result, own: str) -> dict:
    """AI 모드 응답 → {present, domains, cited}.

    문서: https://docs.dataforseo.com/v3/serp/google/ai_mode/live/advanced/
      항목 type "ai_overview"(이 엔드포인트의 답 본문) · items[].references[]{source, domain, url, title, text}
    인용 뽑기는 AI 요약과 같은 한 벌(serp_adapter._domains_in)이고, 세 값의 뜻도 같다:
    답이 없으면 domains [] · cited None, 답은 있는데 인용을 못 뽑았으면 domains None(모름)."""
    present, doms = 0, []
    for it in _items(result):
        if it.get("type") == "ai_overview":
            present = 1
            doms += serp_adapter._domains_in(it)
    doms = sorted(set(doms))
    if not present:
        return {"present": 0, "domains": [], "cited": None}
    if not doms:
        return {"present": 1, "domains": None, "cited": None}
    return {"present": 1, "domains": doms,
            "cited": int(any(scoring.owns(d, own) for d in doms)) if own else None}


def parse_llm_mentions(result, own: str) -> dict:
    """LLM 언급 집계(top_mentioned_domains) → {top: [{domain, mentions, ai_search_volume,
    own, third_party}], own_rank}.

    문서: https://docs.dataforseo.com/v3/ai_optimization/llm_mentions/top_mentioned_domains/live/
      result[].items[] — 도메인마다 한 항목이고 합계는 total 아래 mentions·ai_search_volume.
    문서의 표본 응답을 글자 그대로 확인하지는 못했다(2026-11-24): 도메인 칸은 domain 또는
    key, 합계는 dict 또는 [{key, mentions, ai_search_volume}] 둘 다 받는다. 못 읽은 값은 None."""
    top = []
    for it in _items(result):
        d = scoring.host_of(str(it.get("domain") or it.get("key") or ""))
        if not d:
            continue
        tot = it.get("total")
        if isinstance(tot, list):
            rows = [t for t in tot if isinstance(t, dict)]
            nums = lambda k: [t[k] for t in rows if isinstance(t.get(k), (int, float))]  # noqa: E731
            mentions = sum(nums("mentions")) if nums("mentions") else None
            vol = sum(nums("ai_search_volume")) if nums("ai_search_volume") else None
        else:
            tot = tot if isinstance(tot, dict) else it
            mentions, vol = tot.get("mentions"), tot.get("ai_search_volume")
        top.append({"domain": d, "mentions": mentions, "ai_search_volume": vol,
                    "own": bool(own and scoring.owns(d, own)),
                    "third_party": scoring.is_third_party(d)})
    top.sort(key=lambda x: -(x["mentions"] or 0))
    own_rank = next((n for n, x in enumerate(top, 1) if x["own"]), None)
    return {"top": top[:KEEP * 2], "own_rank": own_rank}


def llm_mentions(conn, project, keywords, locale: str) -> tuple[dict | None, float, str]:
    """이 주제(검색어 최대 10개)로 LLM 답변에 자주 인용되는 도메인 — (값 | None, 실청구액, note).

    내 샘플 질문(ai_prompts) 밖의 인용 지형이다. 한 번에 한 건이고 한 건이 비싸서
    (SITE_KINDS 의 추정 단가) fresh_days 안에는 다시 안 받는다. 키가 없거나 검색어가 없으면
    받지 않고 까닭을 note 로 돌려준다."""
    spec = SITE_KINDS["llm_mentions"]
    if not serp_adapter.has_dataforseo():
        return None, 0.0, "llm_mentions=skipped(DataForSEO 키 없음)"
    if None in db.serp_extras_fresh(conn, project["id"], "llm_mentions", spec["fresh_days"]):
        return None, 0.0, "llm_mentions=fresh"
    targets = [{"keyword": str(k)[:250], "search_filter": "include"}
               for k in list(dict.fromkeys(keywords))[:10] if str(k).strip()]
    if not targets:
        return None, 0.0, "llm_mentions=skipped(검색어 없음)"
    res, cost = serp_adapter.post_dataforseo(
        "/ai_optimization/llm_mentions/top_mentioned_domains/live",
        [{"target": targets, **_geo(locale), "limit": KEEP * 2}], timeout=130)
    data = parse_llm_mentions(res, project["domain"])
    data["targets"] = [t["keyword"] for t in targets]
    db.write_serp_extra(conn, project["id"], None, "llm_mentions", data)
    return data, cost, f"llm_mentions={len(data['top'])}"


def llm_platform(locale: str) -> str:
    """search_mentions 의 플랫폼 — ChatGPT 질문은 미국·영어만 있다(locations_and_languages,
    2026-11-25 확인: 2840/en 만 chat_gpt). 나머지 시장(ko·ja·es·ar 포함)은 구글 AI 개요다."""
    loc, lang, _ = serp_adapter.location(locale)
    return "chat_gpt" if (loc == "United States" and lang == "en") else "google"


def parse_llm_questions(result) -> list[dict]:
    """search_mentions → [{question, ai_search_volume}] (AI 검색량 큰 순, 같은 질문 하나).

    문서: https://docs.dataforseo.com/v3/ai_optimization/llm_mentions/search_mentions/live/
      result[].items[] — 답변 하나에 한 항목. question·ai_search_volume 을 쓴다. 볼륨을 못 읽으면 None."""
    out: list[dict] = []
    seen: set[str] = set()
    for it in _items(result):
        q = " ".join(str(it.get("question") or "").split())
        if not q or len(q) > 300 or q.casefold() in seen:
            continue
        seen.add(q.casefold())
        v = it.get("ai_search_volume")
        out.append({"question": q, "ai_search_volume": v if isinstance(v, (int, float)) else None})
    out.sort(key=lambda x: -(x["ai_search_volume"] or 0))
    return out[:LLM_Q_KEEP]


def llm_questions(conn, project, keywords, locale: str) -> tuple[list | None, float, str]:
    """이 시장에서 AI 가 실제로 받은 질문 — (질문 목록 | None, 실청구액, note).

    주제는 그 시장에서 재는 검색어 최대 10개(질문 안에 그 낱말이 든 것 — 문서상 여러 대상은
    한 목록으로 합쳐 온다). fresh_days 안에 받은 시장은 다시 안 받고 받아 둔 것을 준다.
    키가 없거나 검색어가 없으면 받지 않고 까닭을 note 로 돌려준다. 실패는 부르는 쪽이 삼킨다."""
    spec = SITE_KINDS["llm_questions"]
    pid = project["id"]
    got = (db.serp_extras(conn, pid, "llm_questions").get(None) or {}).get("data") or {}
    by = dict(got.get("by_locale") or {})
    old = by.get(locale) or {}
    try:
        age = (datetime.now(timezone.utc)
               - datetime.fromisoformat(str(old.get("checked_at")).replace("Z", "+00:00"))).days
    except (TypeError, ValueError):
        age = None
    if old.get("questions") is not None and age is not None and age < spec["fresh_days"]:
        return old["questions"], 0.0, "llm_questions=fresh"
    if not serp_adapter.has_dataforseo():
        return None, 0.0, "llm_questions=skipped(DataForSEO 키 없음)"
    targets = [{"keyword": str(k)[:250], "search_scope": ["question"]}
               for k in list(dict.fromkeys(str(k).strip() for k in keywords))[:10] if k]
    if not targets:
        return None, 0.0, "llm_questions=skipped(검색어 없음)"
    plat = llm_platform(locale)
    res, cost = serp_adapter.post_dataforseo(
        "/ai_optimization/llm_mentions/search_mentions/live",
        [{"target": targets, **_geo(locale), "platform": plat,
          "order_by": ["ai_search_volume,desc"], "limit": LLM_Q_LIMIT}], timeout=130)
    qs = parse_llm_questions(res)
    by[locale] = {"checked_at": db.now(), "platform": plat,
                  "targets": [t["keyword"] for t in targets], "questions": qs}
    db.write_serp_extra(conn, pid, None, "llm_questions", {"by_locale": by})
    return qs, cost, f"llm_questions={len(qs)}"


def payload(conn, project_id: int) -> dict:
    """화면·요청문이 읽는 한 벌 — {종류: {검색어: {checked_at, data}}}. 사이트 단위 종류는
    {종류: {checked_at, data}}. 받은 적 없는 종류는 키가 없다."""
    out: dict = {}
    for kind in KINDS:
        got = {kw: v for kw, v in db.serp_extras(conn, project_id, kind).items() if kw is not None}
        if got:
            out[kind] = got
    for kind in SITE_KINDS:
        got = db.serp_extras(conn, project_id, kind).get(None)
        if got:
            out[kind] = got
    return out


def _items(result) -> list[dict]:
    return [it for r0 in (result or []) if isinstance(r0, dict)
            for it in (r0.get("items") or []) if isinstance(it, dict)]


# ── 호출(네트워크) — 검사는 serp_adapter.post_dataforseo 를 갈아 끼운다 ────────

def _geo(locale: str) -> dict:
    loc, lang, _ = serp_adapter.location(locale)
    return {"location_name": loc, "language_code": lang}


def _fetch(kind: str, keyword: str, locale: str, own: str, scope) -> tuple[dict | None, float]:
    """검색어 하나의 한 종류 — (적을 값 | None, 실청구액)."""
    post = serp_adapter.post_dataforseo
    if kind == "images":
        res, cost = post("/serp/google/images/live/advanced",
                         [{"keyword": keyword, **_geo(locale), "depth": KEEP * 2}])
        return parse_images(res, own), cost
    if kind == "youtube":
        res, cost = post("/serp/youtube/organic/live/advanced",
                         [{"keyword": keyword, **_geo(locale), "device": "desktop"}])
        return parse_youtube(res), cost
    if kind == "bing_rank":
        res, cost = post("/serp/bing/organic/live/advanced",
                         [{"keyword": keyword, **_geo(locale), "device": "desktop", "depth": 10}])
        return parse_bing(res, own, scope), cost
    if kind == "trends":
        res, cost = post("/keywords_data/google_trends/explore/live",
                         [{"keywords": [keyword], **_geo(locale), "type": "web",
                           "time_range": "past_5_years"}])
        return parse_trends(res), cost
    if kind == "ai_mode":
        res, cost = post("/serp/google/ai_mode/live/advanced",
                         [{"keyword": keyword, **_geo(locale)}])
        return parse_ai_mode(res, own), cost
    raise ValueError(kind)


def run(conn, project, kws, *, setting=None, default_locale: str, scopes: dict | None = None,
        features_of: dict | None = None, ads_of: dict | None = None) -> dict:
    """켠 종류를 돈다. 반환 {cost, calls, rows, note} — note 는 runs.notes 에 붙일 한 조각이다.

    kws 는 이번 런이 순위를 잰 keywords 행(id·keyword·locale). features_of 는
    {keyword id: 그 조회의 검색결과 기능 목록}, ads_of 는 {keyword id: 광고 문구}.
    DataForSEO 키가 없으면 아무것도 안 하고 까닭을 note 에 남긴다. 계정 오류(잔액·인증,
    serp_adapter.Fatal)는 남은 종류를 전부 멈춘다 — 같은 오류를 검색어 수만큼 맞지 않는다."""
    kinds = wanted(setting)
    out = {"cost": 0.0, "calls": 0, "rows": 0, "note": ""}
    if not kinds:
        out["note"] = "extras=off"
        return out
    if not serp_adapter.has_dataforseo():
        out["note"] = "extras=skipped(DataForSEO 키 없음)"
        return out
    own, pid = project["domain"], project["id"]
    scopes = scopes or {}
    base_scope = next(iter(scopes.values()), "")
    features_of, ads_of = features_of or {}, ads_of or {}
    done: dict[str, int] = {}
    errors = 0
    stop = ""
    for kind in kinds:
        spec = KINDS[kind]
        fresh = db.serp_extras_fresh(conn, pid, kind, spec["fresh_days"])
        todo = [k for k in kws if k["id"] not in fresh
                and (not spec["needs"]
                     or set(spec["needs"]) & set(map(str, features_of.get(k["id"]) or [])))]
        todo = todo[:spec["cap"]]
        if not todo:
            continue
        try:
            if kind == "intent":
                n, cost = _run_intent(conn, pid, todo, default_locale, ads_of)
                out["calls"] += 1 if n or cost else 0
            else:
                n, cost = 0, 0.0
                for k in todo:
                    loc = k["locale"] or default_locale
                    try:
                        data, c = _fetch(kind, k["keyword"], loc, own, scopes.get(loc, base_scope))
                    except serp_adapter.Fatal:
                        raise
                    except Exception as e:      # 한 검색어의 실패 — 다음 검색어로
                        errors += 1
                        print(f"  [extras:{kind}] {k['keyword']}: {e}", file=sys.stderr)
                        continue
                    out["calls"] += 1
                    cost += c
                    if data is not None:
                        db.write_serp_extra(conn, pid, k["id"], kind, data)
                        n += 1
            out["cost"] += cost
            out["rows"] += n
            done[kind] = n
        except serp_adapter.Fatal as e:
            stop = f" stopped={kind}:{str(e)[:80]}"
            print(f"  [extras] {KINDS[kind]['name']}에서 멈춤: {e}", file=sys.stderr)
            break
    out["note"] = ("extras=" + (",".join(f"{k}:{n}" for k, n in done.items()) or "none")
                   + (f" extras_errors={errors}" if errors else "") + stop)
    return out


def _run_intent(conn, pid: int, todo, default_locale: str, ads_of: dict) -> tuple[int, float]:
    """의도 대조 — 언어마다 한 번에 묶어 보낸다(한 과제 최대 1000개).

    language_code 는 보낸다: 이 API 는 언어별 모델로 가르는데, 문서의 요청 칸 표에서 이
    칸을 확인하지 못했다(2026-11-24). 계정이 이 칸을 거부하면 그 묶음은 오류로 세고 넘어간다."""
    by_lang: dict[str, list] = {}
    for k in todo:
        lang = serp_adapter.location(k["locale"] or default_locale)[1]
        by_lang.setdefault(lang, []).append(k)
    n, cost = 0, 0.0
    for lang, rows in by_lang.items():
        ours = {r["keyword"].strip().lower(): scoring.classify_intent(r["keyword"], ads_of.get(r["id"]))
                for r in rows}
        try:
            res, c = serp_adapter.post_dataforseo(
                "/dataforseo_labs/google/search_intent/live",
                [{"keywords": list(ours)[:1000], "language_code": lang}])
        except serp_adapter.Fatal:
            raise
        except Exception as e:
            print(f"  [extras:intent] {lang}: {e}", file=sys.stderr)
            continue
        cost += c
        got = parse_intent(res, ours)
        for r in rows:
            hit = got.get(r["keyword"].strip().lower())
            if hit:
                db.write_serp_extra(conn, pid, r["id"], "intent", hit)
                n += 1
    return n, cost


def _selfcheck() -> None:
    assert wanted(None) == [k for k, v in KINDS.items() if v["default"]]
    assert wanted("none") == [] and wanted("") == [] and wanted("all") == list(KINDS)
    assert wanted("bing_rank, nope ,intent") == ["intent", "bing_rank"]
    assert estimate(["intent", "images"], 100) == KINDS["intent"]["usd"] + 10 * KINDS["images"]["usd"]
    got = parse_intent([{"items": [{"keyword": "Buy Shoes", "keyword_intent": {
        "label": "transactional", "probability": 0.9},
        "secondary_keyword_intents": [{"label": "commercial", "probability": 0.4}]}]}],
        {"buy shoes": "commercial"})
    assert got["buy shoes"]["agree"] is True and got["buy shoes"]["label"] == "transactional"
    data = [{"date_from": f"20{y}-{m:02d}-01", "values": [100 if m == 12 else 20]}
            for y in (22, 23) for m in range(1, 13)]
    t = parse_trends([{"items": [{"type": "google_trends_graph", "data": data}]}])
    assert t["peak"] == "12" and t["points"] == 24 and t["months"]["12"] == 100.0, t
    assert parse_trends([{"items": [{"type": "google_trends_graph", "data": data[:5]}]}]) is None
    assert parse_ai_mode([{"items": []}], "x.com") == {"present": 0, "domains": [], "cited": None}
    print("serp_extras ok")


if __name__ == "__main__":
    _selfcheck()
