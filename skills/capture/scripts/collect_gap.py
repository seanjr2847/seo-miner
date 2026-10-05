#!/usr/bin/env python3
"""경쟁사 탐지·역키워드·트래픽 몫·Content Gap — DataForSEO Labs (F3-확장).

경쟁사 도메인이 랭킹하는 키워드를 끌어와, 내가 안 잡고 있는 것만 후보로
적재한다 ("경쟁사는 잡는데 나는 부재"). 무료 경로(collect_serp 의 rank 상위
수집)는 SERP를 한 번씩 다시 보는 비용이 들고 얕다 — Labs 는 도메인 한 곳
요청으로 최대 수백 개의 랭킹 키워드를 한 번에 돌려준다.

축이 셋이고, 셋 다 끌 수 있다 (0 = 끔):
  A. 역키워드     ranked_keywords/live      → keywords (source='competitor_gap')
  B. 자동 탐지·몫 competitors_domain/live   → competitors(auto_labs) + competitor_metrics
  C. Content Gap  domain_intersection/live  → keyword_gap (missing|weak|shared|unknown)
  D. 비브랜드 발견 ranked_keywords(우리) → serp_competitors/live → competitors(auto_nonbrand)
  E. 역할 판정     ranked_keywords(후보마다) + AI 한 번 → competitors.role

D·E 는 "검색결과가 겹친다 ≠ 경쟁사"라서 있다. 브랜드 사이트(gucci.com)는 검색어 대부분이
자기 브랜드명이고, 그 검색결과는 그 브랜드를 파는 쇼핑몰·포털로 가득하다 — B 만 보면
lfmall·ssg·무신사가 구찌의 경쟁사가 됐다(2026-09-29 실측). D 는 우리 검색어에서 브랜드명을
뺀 것으로 검색결과 경쟁사를 찾아 dior·louisvuitton 같은 진짜 경쟁자를 끌어오고, E 는 자동
후보마다 "그 도메인이 무슨 검색어로 트래픽을 받나"를 보고 경쟁/판매 채널/포털·미디어를
가른다(쇼핑몰은 남의 브랜드명으로, 경쟁 브랜드는 자기 이름으로 받는다). 둘 다 AI 가 있어야
돈다(OPENROUTER_API_KEY) — 없으면 예전 동작 그대로다. 판정은 도메인마다 한 번만 한다
(role 이 빈 자동 후보만). 사람이 적은 경쟁사(manual)는 판정하지 않는다.

B 는 "누가 우리와 키워드가 겹치나"를 사람 등록 없이 찾고(Ahrefs 의 Organic
competitors), 같은 응답의 `metrics.organic` 으로 도메인별 유기 규모까지 한 번에
얻는다 — 콜 하나로 둘. 우리 자신도 `is_self=1` 로 같은 표에 넣는다. 몫(share)은
**저장하지 않는다** — 조회 시 `etv / SUM(etv)` 로 계산한다. 저장하면 경쟁사가
하나 늘어 분모가 바뀔 때 어제 적은 몫이 조용히 거짓말이 된다.

C 는 A 가 못 주는 것을 준다. A 는 "경쟁사가 잡은 키워드" 목록일 뿐이라 *우리가
몇 위인지*가 없다. domain_intersection 은 두 도메인의 위치를 같이 줘서
  · missing — 우리는 순위 없음 (`intersections: false` 축)
  · weak    — 둘 다 있는데 우리가 더 아래
  · shared  — 둘 다 있고 우리가 같거나 위
  · unknown — 우리는 있는데 경쟁사 순위를 못 받았다(누가 위인지 모른다)
로 가른다. 경쟁사당 2콜이라 `limits.gap_rivals` 로 상한을 둔다.

흐름:
  1) 도메인 결정 — --domain 반복 지정, 생략 시 competitors 테이블을 경쟁사로 읽은 것
     (scoring.rivals — manual 먼저, 제3자 플랫폼 제외).
     상한 5개. 5개 초과는 안내 후 상위 5개만 사용 (사용자가 명시적으로 좁혔을
     가능성을 남겨두려고 잘라낸다).
  2) Labs POST /v3/dataforseo_labs/google/ranked_keywords/live 로 도메인당
     --limit 개 랭킹 키워드를 받는다. 응답 items[].keyword + search_volume.
  3) 필터 — (a) 내 활성/후보 keywords 에 이미 있는 것, (b) 최신 GSC 스냅샷에
     impressions>0으로 잡히는 쿼리는 제외. 남는 것만 "내 부재" 후보.
  4) 적재 — db.add_keyword_candidates(is_active=0, source='competitor_gap',
     locale=projects.locale). Labs 가 search_volume 을 주면 keywords.volume
     에 기록 (실측값이라 '볼륨 창작 금지' 규칙 위반 아님 — 주석 참조).
  5) 실행 전체를 db.run(conn, pid, "competitors") 컨텍스트로 감싸 api_calls·cost 기록.

인증: 기존 DataForSEO 자격 (DATAFORSEO_LOGIN/PASSWORD). collect_serp 와 같은
env 경로 — Labs 도 같은 키가 통한다 (유료 크레딧 차감).

비용 고지 (단가 출처):
  DataForSEO Labs `ranked_keywords/live` — DataForSEO 공식 가격표에서
  "DataForSEO Labs API / Ranked Keywords / Live" 행. 2026-08 기준 단가
  ~$0.001/domain lookup (정확한 청구액은 응답 tasks[].cost 가 알려준다 —
  실청구액을 그대로 runs.cost_estimate_usd 에 적는다).
  출처: https://dataforseo.com/apis/dataforseo-labs-api (Labs API 가격 섹션).

한계:
  · Labs 응답의 keyword 수는 도메인 위젯 크기에 따라 들쭉날쭉 — --limit 은
    "최대"이고 실제 반환은 적을 수 있다.
  · search_volume 은 키워드의 월간 검색 추정치 (Labs 자체 추정). NULL 인
    키워드는 keywords.volume 도 NULL 로 둔다 (창작 금지).
  · 'competitor_gap' 으로 적재된 후보는 큐레이션 전엔 is_active=0 — 즉시
    추적에 들어가지 않는다 (`/capture keywords` 의 승인 흐름을 그대로 탄다).
  · Labs 가 도메인을 못 찾으면 items=[] 가 와서 0건 적재 (실패가 아님).

  · 응답 필드명은 Labs 엔드포인트마다·버전마다 달라진다. 파서는 여러 모양을 다
    받고, 못 읽은 **항목만** 건너뛴다 (한 항목 때문에 나머지를 잃지 않는다).

Usage:
  python collect_gap.py --project NAME [--domain d1.com --domain d2.com]
                        [--limit 100] [--rivals 5] [--intersect 3]
                        [--throttle 0.5] [--dry-run]
  python collect_gap.py                                  # self-check
"""
import argparse
import os
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import collector  # noqa: E402
import db  # noqa: E402
import fanout  # noqa: E402
import scoring  # noqa: E402
import serp_adapter  # noqa: E402

DOMAIN_CAP = 5

# Labs 엔드포인트 경로 정본 — 문자열을 호출부에 흩지 않는다 (자체점검도 이걸 본다).
LABS_COMPETITORS = "/dataforseo_labs/google/competitors_domain/live"
LABS_INTERSECT = "/dataforseo_labs/google/domain_intersection/live"
LABS_OVERVIEW = "/dataforseo_labs/google/domain_rank_overview/live"
LABS_RANKED = "/dataforseo_labs/google/ranked_keywords/live"
LABS_SERP_COMPETITORS = "/dataforseo_labs/google/serp_competitors/live"

NONBRAND_KWS = 20      # D: serp_competitors 에 넣는 비브랜드 검색어 수(검색량 큰 순)
ROLE_EVIDENCE = 20     # E: 판정 근거로 받는 후보의 상위 검색어 수 — 12개로는 쇼핑몰의 "남의 브랜드"가
                       #    잘 안 보여 lfmall·무신사가 경쟁으로 판정됐다(실측)
ROLE_CAP = 15          # E: 한 런에 판정하는 새 후보 수 — 후보당 ranked_keywords 한 콜이다
# 판정·브랜드 표기 모델 — 새 후보가 생길 때만 도는 호출이라 작은 모델을 고집할 까닭이 없다.
# 같은 근거로 3번씩 51건을 판정해 gpt-4o-mini 는 5번 틀렸고(무신사→경쟁, 다나와→미디어),
# 이 모델은 0번이었다(2026-09-29, 구찌·aitierlist 후보).
ROLE_MODEL = "anthropic/claude-haiku-4.5"

# metrics.organic 의 top10 구간. 응답이 일부만 주면 준 것만 더한다 (없는 구간을 0으로
# 치면 "top10 이 0" 과 "top10 을 안 줬다" 가 같아진다).
TOP10_KEYS = ("pos_1", "pos_2_3", "pos_4_10")


def _num(v, cast):
    """숫자로 읽히면 숫자, 아니면 None. Labs 는 같은 필드를 문자열로 주기도 한다."""
    if v is None or isinstance(v, bool):
        return None
    try:
        return cast(v)
    except (TypeError, ValueError):
        return None


def _sub(d, keys) -> dict | None:
    """d 아래에서 keys 중 처음 발견되는 dict. 필드명이 응답마다 다른 자리에 쓴다."""
    if not isinstance(d, dict):
        return None
    for k in keys:
        if isinstance(d.get(k), dict):
            return d[k]
    return None


def _domain_of(it: dict) -> str:
    """항목이 말하는 도메인. host_of 로 정규화 — 'www.x.com' 과 'x.com' 이 따로 쌓이지 않게."""
    for k in ("domain", "target", "target2", "se_domain"):
        v = it.get(k)
        if isinstance(v, str) and v.strip():
            return scoring.host_of(v)
    return ""


def _organic(it: dict) -> dict | None:
    """metrics.organic -> {keywords, etv, top10}. 하나도 못 읽으면 None.

    모양이 최소 셋이다: {"metrics": {"organic": {...}}} / {"metrics": {...}} /
    {...}(항목이 곧 지표). 셋 다 받는다 — 엔드포인트마다 한 겹씩 다르다.
    """
    org = _sub(it, ("metrics",)) or it
    org = _sub(org, ("organic",)) or org
    if not isinstance(org, dict):
        return None
    kws = _num(org.get("count"), int)
    if kws is None:
        kws = _num(org.get("keywords_count"), int)
    etv = _num(org.get("etv"), float)
    parts = [_num(org.get(k), int) for k in TOP10_KEYS]
    top10 = sum(v for v in parts if v is not None) if any(v is not None for v in parts) else None
    if kws is None and etv is None and top10 is None:
        return None
    return {"keywords": kws, "etv": etv, "top10": top10}


def _kw_of(it: dict) -> tuple[str, int | None]:
    """(keyword, search_volume|None) — fetch_labs_ranked_keywords 와 같은 관용도."""
    kd = _sub(it, ("keyword_data",)) or it
    kw = kd.get("keyword") or it.get("keyword") or ""
    sv = (_sub(kd, ("keyword_info",)) or {}).get("search_volume")
    if sv is None:
        sv = kd.get("search_volume", it.get("search_volume"))
    return (kw if isinstance(kw, str) else "").strip(), _num(sv, int)


def _pos_of(it: dict, which: int) -> int | None:
    """target1(우리)/target2(경쟁사) 의 순위. serp element 이름이 응답마다 다르다."""
    node = _sub(it, (f"{'first' if which == 1 else 'second'}_domain_serp_element",
                     f"target{which}_serp_element",
                     f"domain{which}_serp_element"))
    if node is None:
        return None
    inner = _sub(node, ("serp_item",)) or node
    for k in ("rank_group", "rank_absolute", "position", "rank"):
        v = _num(inner.get(k), int)
        if v is not None:
            return v
    return None


def _kind(our_pos: int | None, rival_pos: int | None) -> str:
    """missing = 우리 부재 / weak = 둘 다 있고 우리가 아래 / shared = 같거나 위 /
    unknown = 경쟁사 순위를 못 받음. 모르는 걸 '우리가 위'(shared)로 세면 안 된다."""
    if our_pos is None:
        return "missing"
    if rival_pos is None:
        return "unknown"
    if our_pos > rival_pos:
        return "weak"
    return "shared"


def _fetch_competitors(post, target: str, locale: str, limit: int) -> tuple[list[dict], float]:
    """Organic competitors — 우리와 키워드가 겹치는 도메인 + 각자의 유기 지표.

    응답에 우리 자신이 섞여 오는 경우가 있다. 거르지 않고 그대로 돌려준다 —
    누가 '나' 인지는 호출부가 안다 (serp_adapter 와 같은 약속).
    """
    loc, lang, _ = serp_adapter.location(locale)
    result, cost = post(LABS_COMPETITORS, [{
        "target": target, "location_name": loc, "language_code": lang,
        "limit": limit, "order_by": ["intersections,desc"]}])
    out = []
    for r0 in result or []:
        for it in (r0.get("items") or []):
            if not isinstance(it, dict):
                continue
            d = _domain_of(it)
            if d:
                out.append({"domain": d, "metrics": _organic(it)})
    return out, cost


def _fetch_self_metrics(post, target: str, locale: str) -> tuple[dict | None, float]:
    """우리 도메인의 유기 규모 — competitors_domain 응답에 우리가 없을 때의 두 번째 경로."""
    loc, lang, _ = serp_adapter.location(locale)
    result, cost = post(LABS_OVERVIEW, [{
        "target": target, "location_name": loc, "language_code": lang}])
    for r0 in result or []:
        for it in ((r0.get("items") if isinstance(r0, dict) else None) or [r0]):
            m = _organic(it) if isinstance(it, dict) else None
            if m:
                return m, cost
    return None, cost


def _fetch_intersection(post, ours: str, rival: str, locale: str, limit: int,
                        intersections: bool) -> tuple[list[dict], float]:
    """두 도메인의 키워드 교집합. intersections=False 면 '경쟁사만 잡은 것'."""
    loc, lang, _ = serp_adapter.location(locale)
    result, cost = post(LABS_INTERSECT, [{
        "target1": ours, "target2": rival, "location_name": loc, "language_code": lang,
        "limit": limit, "intersections": intersections}])
    out = []
    for r0 in result or []:
        for it in (r0.get("items") or []):
            if not isinstance(it, dict):
                continue
            kw, sv = _kw_of(it)
            if not kw:
                continue        # 키워드를 못 읽은 항목만 버린다
            out.append({"keyword": kw, "volume": sv,
                        "our_position": _pos_of(it, 1), "position": _pos_of(it, 2)})
    return out, cost


def scope_body(scope) -> dict:
    """분석 범위 경로(gucci.com/kr/ko/)를 ranked_keywords 요청에 거는 필터. 이 엔드포인트의
    target 은 도메인이나 페이지 하나만 받고 경로 접두는 못 받는다 — 경로는 filters 의
    ranked_serp_element.serp_item.relative_url 로 건다(공식 문서의 페이지 필터 칸).
    범위가 없으면 빈 dict. 경쟁사 발견·교집합(competitors_domain·domain_intersection)은
    도메인만 받으므로 도메인 단위로 남는다."""
    sc = scoring.scope_of(scope)
    return ({"filters": [["ranked_serp_element.serp_item.relative_url", "like", sc + "%"]]}
            if sc else {})


def _top_keywords(post, target: str, locale: str, limit: int, order: str,
                  scope=None) -> tuple[list, float]:
    """도메인이 순위를 가진 검색어 [(검색어, 검색량|None)] — order 는 Labs order_by 한 줄."""
    loc, lang, _ = serp_adapter.location(locale)
    result, cost = post(LABS_RANKED, [{
        "target": target, "location_name": loc, "language_code": lang,
        "limit": limit, "order_by": [order], **scope_body(scope)}])
    out = []
    for r0 in result or []:
        for it in (r0.get("items") or []):
            if isinstance(it, dict):
                kw, sv = _kw_of(it)
                if kw:
                    out.append((kw, sv))
    return out, cost


# 등록 도메인을 가를 때 한 칸 더 보는 2단계 최상위(co.kr·com.au …). 목록이 아니라 모양이다:
# 끝이 두 글자 나라 코드이고 그 앞이 이 중 하나면 세 칸이 한 사이트다.
_SLD = {"co", "or", "ne", "go", "ac", "re", "pe", "com", "net", "org", "gov", "edu"}


def site_of(host: str) -> str:
    """하위 도메인을 접는다 — search.11st.co.kr·m.gmarket.co.kr·kr.louisvuitton.com 이
    따로 후보가 되면 같은 사이트를 여러 번 판정·과금한다."""
    parts = scoring.host_of(host).split(".")
    n = 3 if len(parts) >= 3 and len(parts[-1]) == 2 and parts[-2] in _SLD else 2
    return ".".join(parts[-n:])


def fetch_own_ranked(post, target: str, locale: str, limit: int = 500,
                     scope=None) -> tuple[list[dict], float]:
    """우리 도메인의 순위 검색어 — [{keyword, volume, position, url}], 검색량 큰 순.
    서치콘솔 없는 사이트의 서치콘솔 대용(db.labs_ranked). 순위는 그 검색어의 우리 최고 자리.
    scope(분석 범위 경로)가 있으면 그 경로 아래 페이지가 선 검색어만 — 요청에 필터를 걸고,
    받은 뒤에도 URL 을 한 번 더 본다(필터가 무시돼도 다른 나라 경로가 새지 않게)."""
    loc, lang, _ = serp_adapter.location(locale)
    result, cost = post(LABS_RANKED, [{
        "target": target, "location_name": loc, "language_code": lang, "limit": limit,
        "order_by": ["keyword_data.keyword_info.search_volume,desc"], **scope_body(scope)}])
    out: dict[str, dict] = {}
    for r0 in result or []:
        for it in (r0.get("items") or []):
            if not isinstance(it, dict):
                continue
            kw, sv = _kw_of(it)
            se = _sub(_sub(it, ("ranked_serp_element",)) or {}, ("serp_item",)) or {}
            pos = _num(se.get("rank_group") or se.get("rank_absolute"), int)
            if scope and not scoring.in_scope(se.get("url") or se.get("relative_url") or "", scope):
                continue
            if kw and (kw not in out or (pos or 999) < (out[kw]["position"] or 999)):
                out[kw] = {"keyword": kw, "volume": sv, "position": pos, "url": se.get("url")}
    return list(out.values()), cost


def _serp_competitors(post, keywords: list, locale: str, limit: int) -> tuple[list, float]:
    """이 검색어들의 검색결과에 많이 서는 도메인 — 겹친 검색어 수·가시성 순."""
    loc, lang, _ = serp_adapter.location(locale)
    result, cost = post(LABS_SERP_COMPETITORS, [{
        "keywords": keywords, "location_name": loc, "language_code": lang,
        "limit": limit, "item_types": ["organic"]}])
    out = []
    for r0 in result or []:
        for it in (r0.get("items") or []):
            d = site_of(_domain_of(it)) if isinstance(it, dict) else ""
            if d and d not in out:
                out.append(d)
    return out, cost


def openrouter_json(prompt: str, *, model: str = ROLE_MODEL, max_tokens: int = 4000,
                    timeout: int | None = None, usage: dict | None = None) -> dict:
    """AI 한 번 — JSON 객체 하나를 돌려받는다. 판정·브랜드명 뽑기·할 일 수정안(plays)이 쓴다
    (답변 수집은 collect_ai).

    collect_ai.ask 를 안 쓰는 이유: 그쪽 system 문구는 "실제 사용자처럼 답하라"다.
    잘림·JSON 없음을 오류로 올리는 판정이 여기 한 벌이라, 긴 답을 받는 쪽(plays)도 모델·
    상한만 바꿔 이 길을 탄다. usage 를 주면 응답의 사용량(OpenRouter 가 세는 cost 포함)을
    거기 채운다 — 청구액을 추정하지 않고 응답이 말하는 값을 쓰려고.
    """
    import json
    import requests
    import collect_ai
    body = {"model": model, "temperature": 0, "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "user", "content": prompt}]}
    if usage is not None:
        body["usage"] = {"include": True}
    r = requests.post(
        collect_ai.OPENROUTER_URL, timeout=timeout or serp_adapter.TIMEOUTS["openrouter"],
        headers={"Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}",
                 "Content-Type": "application/json"},
        json=body)
    serp_adapter.raise_for(r, "OpenRouter")
    data = r.json()
    if usage is not None:
        usage.update(data.get("usage") or {})
    choice = (data.get("choices") or [{}])[0]
    # 잘린 답을 빈 답으로 읽으면 안 된다 — 800토큰 상한에서 판정 목록이 잘리자 아래 파싱이
    # 조용히 {} 를 돌려, 걸러야 할 검색어(매장 위치)를 거르지 않은 채 키워드를 골랐다(gucci).
    if choice.get("finish_reason") == "length":
        raise RuntimeError("AI 답이 길이 상한에서 잘렸습니다 — 판정을 쓰지 않습니다")
    content = (choice.get("message") or {}).get("content") or ""
    # 모델에 따라 ```json 울타리를 두르거나 앞뒤에 한 줄을 붙인다 — 객체만 떼어 읽는다.
    start, end = content.find("{"), content.rfind("}")
    if start < 0 or end < start:
        raise RuntimeError("AI 답에 JSON 객체가 없습니다 — 판정을 쓰지 않습니다")
    got = json.loads(content[start:end + 1])
    return got if isinstance(got, dict) else {}


def _brand_terms(ask, ours: str, top: list, aliases) -> set[str]:
    """우리 브랜드를 부르는 말(모든 표기·언어) — 설정의 brand_aliases + 도메인 줄기 + AI.

    한국어 표기('구찌')는 도메인에서 못 짓는다. 우리가 트래픽을 받는 검색어를 보여 주고
    그 안에서 우리 이름을 고르게 한다 — 목록 밖의 말을 지어내면 버린다.
    """
    terms = {scoring.norm(a) for a in (aliases or []) if scoring.norm(a)}
    stem = scoring.norm(ours.split(".")[0])
    if stem:
        terms.add(stem)
    shown = [kw for kw, _ in top[:40]]
    got = ask(f"우리 사이트는 {ours} 이다. 아래는 이 사이트가 검색에서 트래픽을 받는 검색어다.\n"
              "이 중에서 **도메인 " + ours + " 가 가리키는 우리 이름 자체**의 표기(번역·음역·띄어쓰기 "
              "변형 포함)만 골라라. 우리 사이트가 다루거나 파는 **남의 제품·도구·브랜드 이름은 빼라** "
              "— 우리가 그것을 소개하는 사이트여도 그건 우리 이름이 아니다. 일반 명사도 빼라. "
              "목록에 있는 표기에서만 뽑고, 없으면 빈 배열.\n"
              '답은 JSON 하나: {"brand_terms": ["..."]}\n\n' + "\n".join(f"- {k}" for k in shown))
    for t in got.get("brand_terms") or []:
        n = scoring.norm(str(t))
        if n and any(n in scoring.norm(k) for k in shown):
            terms.add(n)
    return terms


def _nonbrand(top: list, terms: set[str], n: int) -> list[str]:
    """브랜드명이 안 든 검색어를 검색량 큰 순으로 n 개."""
    keep = [(kw, sv or 0) for kw, sv in top
            if not any(t and t in scoring.norm(kw) for t in terms)]
    keep.sort(key=lambda x: -x[1])
    return [kw for kw, _ in keep[:n]]


def _classify(ask, ours: str, type_label: str, evidence: dict) -> dict:
    """{도메인: 역할} — 역할 id 는 scoring.ROLES 한 벌. 모르는 값을 돌려주면 버린다(판정 안 함)."""
    roles = scoring.ROLES
    got = ask(
        f"우리 사이트는 {ours} (종류: {type_label})이다. 아래 각 도메인이 검색에서 트래픽을 가장 많이 받는 "
        "검색어다. 각 도메인을 하나로 분류하라. **이 순서로** 판단한다:\n"
        "1) media: 주제를 가리지 않는 포털·검색·위키·SNS·동영상·블로그 플랫폼·종합 언론\n"
        # theotherskin(피부과)의 경쟁 표에 pmc.ncbi.nlm.nih.gov·ovid.com 이 섰는데 갈래가 없어
        # 경쟁사로 남았고, 콘텐츠 공백 요청문이 "논문 저장소보다 위"를 목표로 삼았다(#654).
        "2) ref: 논문 저장소·학술지·학술 DB·정부·공공기관·대학처럼 자료를 싣는 곳(우리가 그런 "
        "곳이 아니면). 같은 주제를 다뤄도 rival 이 아니다\n"
        "3) rival: 우리와 같은 방문자를 두고 **같은 종류의 것**을 내놓는 사이트. 우리가 브랜드·"
        "서비스면 경쟁 브랜드·경쟁 서비스의 자체 사이트, 우리가 정보·비교·목록·커뮤니티 사이트면 "
        "같은 주제를 다루는 그런 사이트\n"
        "4) channel: 우리가 브랜드·서비스인데, 그 도메인은 **남의 브랜드** 상품·여러 판매자를 모아 "
        "파는 곳(쇼핑몰·편집숍·백화점몰·오픈마켓·리셀·중고거래·가격비교·예약 플랫폼). 자체 상품이 "
        "일부 있어도 여러 브랜드를 팔면 channel\n"
        "5) other: 어느 것도 아님\n"
        "근거는 검색어다: 브랜드·서비스 사이트는 자기 이름과 자기 제품 이름으로 트래픽을 받고, 쇼핑몰은 "
        "남의 브랜드 이름으로 받는다. 같은 업종이라는 것만으로 rival 이 아니다.\n"
        f'답은 JSON 하나: {{"도메인": "{"|".join(roles)}", ...}}\n\n'
        + "\n".join(f"- {d}: {', '.join(k)}" for d, k in evidence.items()))
    return {d: str(v) for d, v in got.items() if d in evidence and str(v) in roles}


def _put_metric(conn, pid: int, day: str, domain: str, is_self: int, m: dict) -> None:
    """competitor_metrics 한 줄. share 는 넣지 않는다 — 분모가 바뀌면 낡기 때문에
    조회 시 `etv / SUM(etv)` 로 계산한다."""
    conn.execute(
        """INSERT INTO competitor_metrics(project_id, checked_date, domain, is_self,
                                          keywords, etv, top10)
             VALUES(?,?,?,?,?,?,?)
           ON CONFLICT(project_id, checked_date, domain) DO UPDATE SET
             is_self=excluded.is_self, keywords=excluded.keywords,
             etv=excluded.etv, top10=excluded.top10""",
        (pid, day, domain, is_self, m.get("keywords"), m.get("etv"), m.get("top10")))


def _put_gap(conn, pid: int, day: str, rival: str, row: dict) -> None:
    conn.execute(
        """INSERT INTO keyword_gap(project_id, checked_date, keyword, domain,
                                   position, our_position, volume, kind)
             VALUES(?,?,?,?,?,?,?,?)
           ON CONFLICT(project_id, checked_date, keyword, domain) DO UPDATE SET
             position=excluded.position, our_position=excluded.our_position,
             volume=excluded.volume, kind=excluded.kind""",
        (pid, day, row["keyword"], rival, row["position"], row["our_position"],
         row["volume"], _kind(row["our_position"], row["position"])))


def dashboard_types() -> tuple:
    """사이트 종류 (id, 라벨) — 정본은 dashboard.PROJECT_TYPES. 늦게 불러온다(순환 import 방지)."""
    import dashboard
    return dashboard.PROJECT_TYPES


def _cap(domains: list[str]) -> list[str]:
    """역키워드 대상 상한. 자동 탐지가 붙은 뒤에도 같은 자를 쓴다."""
    if len(domains) <= DOMAIN_CAP:
        return domains
    print(f"[안내] 경쟁사 {len(domains)}개는 상한 {DOMAIN_CAP}개를 초과 — "
          f"앞 {DOMAIN_CAP}개만 사용합니다. 더 정밀하게는 `--domain` 으로 명시하세요.",
          file=sys.stderr)
    return domains[:DOMAIN_CAP]


def _resolve_domains(conn, project_id: int, args_domains: list[str], own: str = "") -> list[str]:
    if args_domains:
        # 사용자가 명시한 도메인은 그대로 (소문자·공백 정리만).
        out = []
        for d in args_domains:
            d = (d or "").strip().lower()
            if d:
                out.append(d)
        return out
    # 표를 id 순으로 잘라 쓰던 때는 앞자리가 순위 수집이 넣은 플랫폼이었다 — 돈을 내고
    # m.blog.naver.com·brunch.co.kr·play.google.com 의 키워드를 캤다(2026-09 호스팅).
    return scoring.rivals(conn, project_id, own)[0]


def _existing_norms(conn, project_id: int) -> set[str]:
    """내 키워드(norm) — 후보·활성 모두 포함. '이미 있다' 의 기준선."""
    return {scoring.norm(r["keyword"]) for r in
            conn.execute("SELECT keyword FROM keywords WHERE project_id=?",
                         (project_id,)).fetchall()}


def _gsc_seen_norms(conn, project_id: int) -> set[str]:
    """최신 GSC 스냅샷에서 impressions>0 으로 잡힌 쿼리(norm).
    빈 스냅샷이면 공집합 — 필터가 전부 통과한다는 뜻이고, 그 자체로 데이터
    부재 신호이므로 굳이 경고하지 않는다 (gsc 미수집 프로젝트는 흔하다)."""
    latest = conn.execute(
        """SELECT snapshot_date, MAX(period_days) period_days
             FROM gsc_snapshots WHERE project_id=?
            GROUP BY snapshot_date ORDER BY 1 DESC LIMIT 1""",
        (project_id,)).fetchone()
    if not latest:
        return set()
    return {scoring.norm(r["query"]) for r in conn.execute(
        """SELECT query FROM gsc_snapshots
            WHERE project_id=? AND snapshot_date=? AND period_days=? AND impressions>0""",
        (project_id, latest["snapshot_date"], latest["period_days"])).fetchall()}


def _backfill_volumes(conn, project_id: int, items: list[tuple[str, int | None]]) -> int:
    """add_keyword_candidates 가 volume 을 받지 않으므로, INSERT 후에
    search_volume 있는 항목만 keywords.volume 을 UPDATE. NULL 은 그대로.

    items: [(keyword, search_volume|None), ...] — 이미 적재된 후보만 대상으로.
    """
    n = 0
    for kw, sv in items:
        if sv is None:
            continue
        cur = conn.execute(
            "UPDATE keywords SET volume=? WHERE project_id=? AND keyword=? AND volume IS NULL",
            (int(sv), project_id, kw))
        if cur.rowcount:
            n += cur.rowcount
    conn.commit()
    return n


def collect(project: str, *,
            dry_run: bool = False,
            domain: list[str] | None = None,
            limit: int | None = None,
            rivals: int | None = None,
            intersect: int | None = None,
            throttle: float | None = None,
            conn=None,
            fetch=None,
            post=None,
            ask=None) -> collector.StageResult:
    """경쟁사를 찾고(B·D) 가르고(E) 역키워드를 캐고(A) 위치 차이를 적재한다(C).

    Args:
        project: 사이트 이름
        dry_run: True 면 호출 계획만 찍고 종료
        domain: 분석할 경쟁사 도메인 (반복 지정 가능). None/빈 리스트면
                competitors 테이블의 도메인 전부. 명시하면 자동 탐지는 꺼진다 —
                사용자가 이미 대상을 정한 것이다.
        limit: 도메인당 키워드 수 상한(config 키는 limits.gap_limit). 기본 100 —
            CLI 플래그(--limit)와 이름을 맞춘다.
        rivals: 자동 탐지할 경쟁사 수. 0이면 자동 탐지 끔.
        intersect: Content Gap 을 돌릴 경쟁사 수 상한. 0이면 끔.
        throttle: 요청 간격(초)
        conn: 이미 열린 Brain 연결 — 주면 그것을 쓰고 닫지 않는다
        fetch: (domain, locale, limit) -> (items, cost) — 주면 Labs 대신 이것을
               부른다 (자체점검이 requests.post 를 갈아끼우던 자리)
        post: (path, body) -> (result, cost) — 주면 serp_adapter.post_dataforseo
              대신 이것을 부른다. 새 축(B·C·D·E)의 주입 자리.
        ask: (prompt) -> dict — AI 한 번(JSON). 주면 OpenRouter 대신 이것을 부른다.
             없고 OPENROUTER_API_KEY 도 없으면 D·E 는 끈다(예전 동작).

    Returns:
        StageResult(ok=...). 유료 키 부재만 ok=False, skipped=True (진짜 못 한
        것). 경쟁사 0건·dry-run 은 ok=True, skipped=True — 체인을 깨지 않는다.
    """
    ap = _parser()
    fetch = fetch or serp_adapter.fetch_labs_ranked_keywords
    post = post or serp_adapter.post_dataforseo
    if ask is None and os.environ.get("OPENROUTER_API_KEY"):
        ask = openrouter_json
    with collector.stage(project, conn=conn, dry_run=dry_run) as st:
        conn, p = st.conn, st.project
        if not serp_adapter.has_dataforseo():
            return st.skip("Labs 유료 키 필요 — DATAFORSEO_LOGIN/PASSWORD 설정. "
                           "발급: https://dataforseo.com")

        s = st.settings(ap, argparse.Namespace(limit=limit, throttle=throttle,
                                           rivals=rivals, intersect=intersect))
        limit = s["limits.gap_limit"]
        # --domain 으로 좁혔으면 자동 탐지는 끈다 — 대상은 이미 사용자가 정했다.
        n_auto = 0 if domain else s["limits.auto_competitors"]
        n_gap = s["limits.gap_rivals"]
        throttle = st.throttle
        ours = scoring.host_of(p["domain"] or "")

        domains = _cap(_resolve_domains(conn, p["id"], domain, ours))
        if not domains and not n_auto:
            # 경쟁사 부재는 실패가 아니라 아직 적재를 안 한 정상 상태다 (첫 바퀴,
            # 또는 rank harvest 가 3회 이상 잡힌 도메인을 못 찾은 사이트).
            # ok=False 로 돌리면 run_all 체인이 exit 1 로 끝나고 worker.py 의
            # 주간 리포트 메일이 조용히 안 나간다.
            reason = (f"'{p['name']}' 에 등록된 경쟁사가 아직 없습니다 — "
                      f"`/capture rank {p['name']}` 으로 순위를 한 바퀴 돌리면 "
                      "상위 도메인이 자동 적재됩니다. 급하면 프로젝트 yaml 의 "
                      "대시보드 [설정]의 경쟁사 칸 또는 `--domain` 으로 직접 지정하세요.")
            print(f"[gap] {reason}")
            return st.noop(reason=reason)

        locale = db.project_locale(p)
        # 콜 수: 역키워드(도메인당 1) + 자동탐지 1 + 우리 지표 1 + Gap(경쟁사당 2).
        # 자동 탐지가 경쟁사를 더 붙이면 실제 콜은 이보다 늘 수 있다 — 상한이 아니라 예상치다.
        n_gap_hosts = min(n_gap, max(len(domains), n_auto)) if n_gap and ours else 0
        est_calls = len(domains) + (2 if n_auto else 0) + n_gap_hosts * 2
        est_cost = est_calls * serp_adapter.LABS_COST_PER_CALL
        # 소요 시간: Labs 는 간격이 없고(serp_adapter.pace_seconds 가 경로로 가른다 — 경로 없이
        # 물으면 Google Ads 6초로 어림해 몇 배 길게 말한다) 동시에 fanout.LIMITS 개씩 간다.
        per_call = max(throttle, serp_adapter.pace_seconds(LABS_INTERSECT)) + 2
        print(f"[gap] project={p['name']} domains={len(domains)} limit={limit} "
              f"est_cost≈${est_cost:.3f} "
              f"(~{est_calls * per_call / fanout.LIMITS['dataforseo'] / 60:.1f} min)")
        serp_adapter.warn_unmapped(locale)   # 매핑 없는 로케일 경고는 돈을 쓰기 전에 — collect_serp 와 같은 자리

        if st.dry_run:
            for d in domains:
                print(f"  - {d}")
            print("  자동 경쟁사 탐지: " + (
                f"켜짐 — competitors_domain/live 1콜(limit={n_auto}) + 우리 지표 1콜 "
                f"→ competitors(source='auto_labs') · competitor_metrics"
                if n_auto else "꺼짐 (--rivals 0 또는 --domain 지정)"))
            print("  Content Gap: " + (
                f"켜짐 — domain_intersection/live {n_gap_hosts}×2콜 (교집합 + 우리 부재) "
                f"→ keyword_gap"
                if n_gap_hosts else "꺼짐 (--intersect 0 또는 프로젝트 domain 미설정)"))
            print(f"단가 출처: DataForSEO Labs ranked_keywords/live ≈ ${serp_adapter.LABS_COST_PER_CALL}/call "
                  "(모듈 docstring). 실제 청구액은 응답 cost 로 기록.")
            return st.noop(cost=est_cost)

        own_norm = _existing_norms(conn, p["id"])
        gsc_norm = _gsc_seen_norms(conn, p["id"])
        print(f"     filter: existing={len(own_norm)} keywords, gsc_seen={len(gsc_norm)} queries")

        today = st.today
        total_cost = 0.0
        inserted_total = 0
        volumes_total = 0
        auto_new = metric_rows = gap_rows = extra_calls = kw_done = 0
        nonbrand_new = judged = 0

        calls_lock = threading.Lock()

        def _post(path, body):
            """콜 수를 세는 자리 하나 — runs.api_calls 가 새 축까지 센다.
            Gap 축은 일꾼 스레드에서 부르므로 += 를 잠가 센다(안 잠그면 개수가 샌다)."""
            nonlocal extra_calls
            with calls_lock:
                extra_calls += 1
            return post(path, body)

        def one(d: str, got) -> None:
            """도메인 하나 — 가져온 역키워드를 거르고 적재한다(이 스레드에서만).
            실패는 러너가 세고 다음 도메인으로 넘어간다."""
            nonlocal total_cost, inserted_total, volumes_total, kw_done
            items, cost = got
            total_cost += cost
            # 필터 — norm 비교로 케이스·공백 차이 흡수.
            kept: list[tuple[str, int | None]] = []
            for it in items:
                if scoring.norm(it["keyword"]) in own_norm:
                    continue
                if scoring.norm(it["keyword"]) in gsc_norm:
                    continue
                kept.append((it["keyword"], it["search_volume"]))
            inserted = db.add_keyword_candidates(
                conn, p["id"],
                [(kw, locale, "competitor_gap") for kw, _ in kept])
            inserted_total += inserted
            volumes_total += _backfill_volumes(conn, p["id"], kept)
            kw_done += 1
            print(f"  {d}: fetched={len(items)} kept={len(kept)} inserted={inserted}")

        labs = {"rows": [], "mine": None}      # B 의 응답 — 몫(metric_axis)이 판정 뒤에 쓴다

        def auto_axis() -> None:
            """자동 탐지 — competitors_domain 한 콜. 같은 응답의 지표는 판정 뒤 몫에 쓴다."""
            nonlocal total_cost, auto_new
            rows, cost = _fetch_competitors(_post, ours, locale, n_auto)
            total_cost += cost
            labs["rows"] = rows
            # 제외 필터는 새로 만들지 않는다 — scoring.foreign_brands 가 yaml 의
            # tools/foreign_brands 와 기존 competitors 를 이미 정규화해 갖고 있다.
            brands = scoring.foreign_brands(conn, p["id"], st.cfg)
            plats = scoring.third_party_platforms()
            mine, cand = None, []
            for row in rows:
                d = row["domain"]
                if scoring.owns(d, ours):
                    mine = mine or row["metrics"]   # 우리는 경쟁사가 아니다 — 분자로 간다
                    continue
                if scoring._stem(d) in brands:      # 등재·경쟁 도구 이름
                    continue
                if scoring.is_third_party(d, plats):   # 키워드가 겹쳐도 플랫폼은 경쟁사가 아니다
                    continue
                cand.append(d)
            for d in cand[:n_auto]:
                # manual 로 등록된 행은 절대 덮지 않는다 — 사람이 고른 것이 이긴다.
                auto_new += conn.execute(
                    "INSERT INTO competitors(project_id, domain, source) VALUES(?,?, 'auto_labs') "
                    "ON CONFLICT(project_id, domain) DO NOTHING", (p["id"], d)).rowcount
            print(f"  auto: found={len(rows)} kept={len(cand)} new={auto_new}")
            labs["mine"] = mine

        def nonbrand_axis() -> None:
            """D — 우리 검색어에서 브랜드명을 빼고 그 검색결과의 경쟁사를 찾는다."""
            nonlocal total_cost, nonbrand_new
            top, c = _top_keywords(_post, ours, locale, 100,
                                   "keyword_data.keyword_info.search_volume,desc",
                                   scope=p["scope_path"] if "scope_path" in p.keys() else None)
            total_cost += c
            terms = _brand_terms(ask, ours, top, (st.cfg or {}).get("brand_aliases"))
            kws = _nonbrand(top, terms, NONBRAND_KWS)
            if not kws:
                print(f"  nonbrand: 브랜드명 없는 검색어가 없습니다 (브랜드 표기 {sorted(terms)})")
                return
            doms, c = _serp_competitors(_post, kws, locale, n_auto * 3)
            total_cost += c
            brands = scoring.foreign_brands(conn, p["id"], st.cfg)
            plats = scoring.third_party_platforms()
            cand = [d for d in doms if not scoring.owns(d, ours)
                    and scoring._stem(d) not in brands and not scoring.is_third_party(d, plats)]
            for d in cand[:n_auto]:
                nonbrand_new += conn.execute(
                    "INSERT INTO competitors(project_id, domain, source) VALUES(?,?, 'auto_nonbrand') "
                    "ON CONFLICT(project_id, domain) DO NOTHING", (p["id"], d)).rowcount
            print(f"  nonbrand: 브랜드 표기 {sorted(terms)} · 검색어 {len(kws)}개 → "
                  f"found={len(doms)} kept={len(cand)} new={nonbrand_new}")

        def role_axis() -> None:
            """E — 판정 안 한 자동 후보마다 상위 검색어를 받고 AI 한 번으로 역할을 가른다."""
            nonlocal total_cost, judged
            import json
            todo = scoring.unjudged(conn, p["id"], ROLE_CAP)
            if not todo:
                return
            evidence: dict = {}

            def got_one(d, got):
                nonlocal total_cost
                kws, c = got
                total_cost += c
                if kws:
                    evidence[d] = [k for k, _ in kws]

            fanout.each(st, todo,
                        lambda d: _top_keywords(_post, d, locale, ROLE_EVIDENCE,
                                                "ranked_serp_element.serp_item.etv,desc"),
                        got_one, workers=fanout.LIMITS["dataforseo"], label=lambda d: d)
            roles = {}
            if evidence:
                label = dict(dashboard_types()).get(p["type"] or "", p["type"] or "")
                roles = _classify(ask, ours, label, evidence)
            for d, role in roles.items():
                judged += conn.execute(
                    "UPDATE competitors SET role=?, role_why=? WHERE project_id=? AND domain=?",
                    (role, json.dumps(evidence[d][:ROLE_EVIDENCE], ensure_ascii=False),
                     p["id"], d)).rowcount
            # 물어봤는데 판정이 안 선 후보(근거 검색어 0개·AI 가 빠뜨림)는 시도 흔적만 남긴다 —
            # role 은 빈 채라 경쟁사 읽기는 그대로고, scoring.unjudged 가 다음 런에 한 번도 안
            # 물어본 후보를 먼저 준다. 흔적이 없으면 같은 후보가 매 런 앞자리를 먹는다.
            # 네트워크가 죽어 근거를 못 받은 후보(st.fail 로 세어진 것)도 여기 든다 — 뒤로
            # 밀릴 뿐 빠지지 않는다(한 번도 안 물어본 후보가 다 판정된 뒤 다시 온다).
            for d in todo:
                if d not in roles:
                    conn.execute(
                        "UPDATE competitors SET role_why=? WHERE project_id=? AND domain=?"
                        " AND role IS NULL",
                        (json.dumps(evidence.get(d, [])[:ROLE_EVIDENCE], ensure_ascii=False),
                         p["id"], d))
            conn.commit()
            print("  roles: " + ", ".join(f"{d}={scoring.ROLES.get(r, r)}" for d, r in roles.items()))

        def metric_axis() -> None:
            """트래픽 몫 — B 의 지표를, 판정이 끝난 경쟁사로만 적는다(판매 채널이 분모에 안 섞이게)."""
            nonlocal total_cost, metric_rows, extra_calls
            rows, mine = labs["rows"], labs["mine"]
            # 몫의 분모 — 경쟁사로 읽힌 것(플랫폼·판매 채널 제외) 중 지표를 받은 것만.
            regs = set(scoring.rivals(conn, p["id"], ours)[0])
            for row in rows:
                if row["domain"] in regs and row["metrics"]:
                    _put_metric(conn, p["id"], today, row["domain"], 0, row["metrics"])
                    metric_rows += 1
            # 몫의 분자 — 우리 자신. 응답에 우리가 없으면 domain_rank_overview 로.
            if mine is None:
                try:
                    mine, c = _fetch_self_metrics(_post, ours, locale)
                    total_cost += c
                except Exception as e:
                    print(f"  ! 우리 지표 domain_rank_overview 실패: {e}", file=sys.stderr)
            if mine is None:
                # ponytail: 근사다. Labs 두 경로가 다 실패했을 때만 — ranked_keywords 로
                # 우리 키워드 '수'만 세고 etv/top10 은 NULL 로 둔다(창작 금지). 분자가
                # 아예 비면 몫을 못 내므로 그보다는 낫다는 판단이고, 정확한 값은 위 둘이 준다.
                try:
                    items, c = fetch(ours, locale, limit)
                    extra_calls += 1
                    total_cost += c
                    mine = {"keywords": len(items), "etv": None, "top10": None}
                except Exception as e:
                    print(f"  ! 우리 지표 근사(ranked_keywords) 실패: {e}", file=sys.stderr)
            if mine:
                _put_metric(conn, p["id"], today, ours, 1, mine)
                metric_rows += 1

        def gap_fetch(rival: str) -> list:
            """경쟁사 하나 — 교집합(weak/shared) 1콜 + 우리 부재(missing) 1콜. 네트워크만."""
            return [_fetch_intersection(_post, ours, rival, locale, limit, inter)
                    for inter in (True, False)]

        def gap_axis(rival: str, got: list) -> None:
            nonlocal total_cost, gap_rows
            n = 0
            for rows, cost in got:
                total_cost += cost
                for row in rows:
                    _put_gap(conn, p["id"], today, rival, row)
                    n += 1
            gap_rows += n
            print(f"  gap {rival}: rows={n}")

        with st.record("competitors") as r:
            if n_auto:
                try:
                    auto_axis()
                except Exception as e:      # 한 축이 죽어도 나머지 축은 산다
                    st.fail(str(e), item="자동 경쟁사 탐지", kind=type(e).__name__)
                conn.commit()
                # D·E 는 AI 가 있을 때만 — 없으면 예전 동작 그대로(B 가 붙인 것을 그대로 쓴다).
                if ask is not None and ours:
                    for axis, item in ((nonbrand_axis, "비브랜드 경쟁사 발견"),
                                       (role_axis, "경쟁사 역할 판정")):
                        try:
                            axis()
                        except collector.Fatal:
                            raise
                        except Exception as e:
                            st.fail(str(e), item=item, kind=type(e).__name__)
                        conn.commit()
                try:
                    metric_axis()
                except Exception as e:
                    st.fail(str(e), item="트래픽 몫", kind=type(e).__name__)
                conn.commit()
                if not domain:              # 새로 붙은 경쟁사도 역키워드·Gap 대상에 넣는다
                    domains = _cap(_resolve_domains(conn, p["id"], None, ours))
            # 역키워드(도메인마다)와 Content Gap(경쟁사마다)은 서로 독립이다 — 한 줄로 펴서
            # 동시에 산다(fanout.LIMITS["dataforseo"]). 자동 탐지는 위에서 먼저 끝냈다:
            # 새로 붙은 경쟁사가 이 목록을 바꾸기 때문이다. 적재는 이 스레드가 목록
            # 순서대로 한다(역키워드 전부 → Gap) — 순차일 때와 같은 순서다.
            jobs = [("kw", d) for d in domains]
            if n_gap and ours:
                jobs += [("gap", d) for d in domains[:n_gap]]
            fanout.each(
                st, jobs,
                lambda j: fetch(j[1], locale, limit) if j[0] == "kw" else gap_fetch(j[1]),
                lambda j, got: one(j[1], got) if j[0] == "kw" else gap_axis(j[1], got),
                workers=fanout.LIMITS["dataforseo"], label=lambda j: j[1])
            r.api_calls = kw_done + extra_calls
            r.notes = (f"domains={len(domains)} inserted={inserted_total} "
                       f"volumes_filled={volumes_total} auto_new={auto_new} "
                       f"nonbrand_new={nonbrand_new} judged={judged} "
                       f"metrics={metric_rows} gap_rows={gap_rows} {st.err_note}")

        print(f"\ncollected {len(domains)} domains, "
              f"actual_cost=${total_cost:.3f} (inserted={inserted_total}, volumes={volumes_total})\n"
              f"auto_competitors={auto_new} competitor_metrics={metric_rows} keyword_gap={gap_rows}\n"
              f"run_id={r.id}\n"
              f"Next: 후보는 source='competitor_gap', is_active=0 — "
              f"/capture keywords 의 큐레이션 단계로 활성화하세요.")
        # 적재 0건인데 오류가 있었으면 완료가 아니다 — 판정은 collector 한 벌이다.
        # "실제로 뭔가 했나"는 세 축의 합이다. inserted_total 만 보면 자동 탐지가
        # 죽고 Gap 축은 3행을 넣은 바퀴도 실패로 읽힌다 (아래 자체점검이 그 자리).
        return st.verdict(inserted_total + gap_rows + metric_rows + nonbrand_new + judged,
                          rows=inserted_total, cost=total_cost)


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser()
    collector.add_common(ap)
    ap.add_argument("--domain", action="append", default=[],
                    help="분석할 경쟁사 도메인 (반복 가능). 생략 시 competitors 테이블 도메인 전부")
    collector.add_setting(ap, "--limit", key="limits.gap_limit", fallback=100, type=int,
                          help="도메인당 키워드 수 상한. 기본 100")
    collector.add_setting(ap, "--rivals", key="limits.auto_competitors", fallback=5, type=int,
                          help="자동 탐지할 경쟁사 수. 0이면 자동 탐지 끔 (수동 등록만 씀)")
    collector.add_setting(ap, "--intersect", key="limits.gap_rivals", fallback=3, type=int,
                          help="Content Gap 을 돌릴 경쟁사 수 상한. 0이면 끔")
    collector.add_setting(ap, "--throttle", key="throttle", fallback=0.5, type=float,
                          help="요청 간격(초). 기본은 skill_config defaults.throttle")
    return ap


def main() -> None:
    # 인자 없이 실행되면 자체 점검 — 다른 수집기와 같은 약속. add_common 이
    # --project 를 required 로 걸어버리므로 parse 직전에 길이를 본다.
    if len(sys.argv) == 1:
        _selfcheck()
        return
    collector.cli("competitors")


def _selfcheck() -> None:
    """가짜 fetch 로 필터·적재 경로 전부 검증 — 진짜 Brain·Labs 안 건드림.

    의존물(conn·fetch)을 인자로 주입한다. 예전에는 requests.post 를 갈아끼우고
    sys.argv 를 세워 main() 을 부르는 우회로였다.
    """
    import tempfile

    home = Path(tempfile.mkdtemp(prefix="seo-miner-gap-selftest-"))
    os.environ["CAPTURE_HOME"] = str(home)
    # 키는 모킹 단계에서만 필요 — 실제 호출 안 함.
    os.environ["DATAFORSEO_LOGIN"] = "login"
    os.environ["DATAFORSEO_PASSWORD"] = "pw"
    # AI 키가 이 PC 의 env 에 있으면 D·E 축이 켜져 아래 기존 검사가 흔들린다 — 끄고 시작한다.
    # D·E 는 맨 아래에서 ask 를 주입해 따로 본다.
    os.environ.pop("OPENROUTER_API_KEY", None)

    # 사이트 설정 — tools 필터(자동 탐지 제외 목록)가 여기서 온다(정본 db.SETTING_KEYS).
    conn = db.connect()
    pid = db.register_project(conn, {"name": "gt", "domain": "gt.com", "locale": "ko-KR",
                                     "tools": ["ToolCo"]})
    p = conn.execute("SELECT * FROM projects WHERE name='gt'").fetchone()

    # 내가 이미 가진 키워드 — 필터에서 빠져야 한다.
    db.add_keyword_candidates(conn, pid, [
        ("공통 키워드", "ko-KR", "seed"),
        ("내 시드", "ko-KR", "seed"),
    ])
    conn.execute("UPDATE keywords SET is_active=1 WHERE keyword='내 시드'")

    # GSC 스냅샷에 잡힌 쿼리 — 필터에서 빠져야 한다.
    conn.execute(
        """INSERT INTO gsc_snapshots(project_id, snapshot_date, period_days,
              query, page, clicks, impressions, ctr, position)
             VALUES(?, '2026-08-18', 28, ?, NULL, 1, 10, 0.1, 5.0)""",
        (pid, "gsc 잡힌 쿼리"))

    # 경쟁사 한 곳 등록 (--domain 생략 시 이게 쓰인다).
    conn.execute(
        "INSERT INTO competitors(project_id, domain, source) VALUES(?, 'rival.com', 'manual')",
        (pid,))
    # 두 번째 수동 등록 — 어간이 두 글자라 foreign_brands 의 len>=3 문턱에 안 걸린다.
    # 그래서 자동 탐지가 실제로 INSERT 를 시도하고, ON CONFLICT DO NOTHING 이
    # manual 을 지키는지가 여기서만 검사된다 (rival.com 은 필터 단계에서 이미 빠진다).
    conn.execute(
        "INSERT INTO competitors(project_id, domain, source) VALUES(?, 'hq.io', 'manual')",
        (pid,))
    conn.commit()

    # Labs 모킹 — 의존물은 인자로 준다 (예전에는 requests.post 를 갈아끼웠다).
    # "공통 키워드" / "gsc 잡힌 쿼리" 는 필터에 걸리고,
    # "신규 후보 A" / "신규 후보 B" 만 남아야 한다.
    def fake_fetch(domain, locale, limit):
        assert (domain, locale) == ("rival.com", "ko-KR"), (domain, locale)
        return [
            {"keyword": "공통 키워드", "search_volume": 50},
            {"keyword": "신규 후보 A", "search_volume": 1200},
            {"keyword": "GSC 잡힌 쿼리", "search_volume": None},
            {"keyword": "신규 후보 B", "search_volume": 7},
        ], 0.0007

    # 꺼진 축이 Labs 를 부르면 여기 남는다. 예외만 던지면 st.each 가 삼켜서 검사가
    # 조용히 통과한다 — 그래서 '불렀다'는 사실 자체를 기록한다.
    off_calls: list = []

    def no_post(path, body):
        off_calls.append(path)
        raise AssertionError(f"꺼진 축이 Labs 를 불렀다: {path}")

    # 새 축을 둘 다 끄면 기존 동작 그대로여야 한다 (회귀 방어 — 아래 단언은 원본 그대로).
    res = collect("gt", domain=["rival.com"], throttle=0, conn=conn, fetch=fake_fetch,
                  rivals=0, intersect=0, post=no_post)
    assert (res.ok, res.skipped, res.rows) == (True, False, 2), res
    assert off_calls == [], off_calls
    assert conn.execute("SELECT COUNT(*) FROM competitor_metrics").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM keyword_gap").fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM competitors WHERE source='auto_labs'").fetchone()[0] == 0
    conn.execute("SELECT 1")     # 빌린 conn 은 러너가 닫지 않는다

    added = [dict(r) for r in conn.execute(
        "SELECT keyword, source, is_active, locale, volume "
        "FROM keywords WHERE project_id=? AND source='competitor_gap' ORDER BY keyword",
        (pid,)).fetchall()]
    assert len(added) == 2, f"필터 후 신규 후보 2개여야 함: {added}"
    assert {r["keyword"] for r in added} == {"신규 후보 A", "신규 후보 B"}
    assert all(r["is_active"] == 0 for r in added)
    assert all(r["locale"] == "ko-KR" for r in added)
    by_kw = {r["keyword"]: r["volume"] for r in added}
    assert by_kw["신규 후보 A"] == 1200, by_kw
    assert by_kw["신규 후보 B"] == 7, by_kw

    # dry-run 은 Labs 를 부르지도, run 을 남기지도 않아야 한다 — 지뢰 fetch 로 확인.
    conn.execute("DELETE FROM runs")
    conn.commit()

    def boom(*a, **kw):
        raise AssertionError("dry-run 이 Labs 를 불렀다")

    res = collect("gt", dry_run=True, conn=conn, fetch=boom, post=boom)
    assert (res.ok, res.skipped) == (True, True), res
    runs = conn.execute("SELECT * FROM runs").fetchall()
    assert runs == [], f"dry-run 은 runs 에 아무것도 남기지 않아야 함: {runs}"

    # ── 자동 탐지 + 트래픽 몫 + Content Gap ──────────────────────────
    posts: list = []

    def fake_post(path, body):
        posts.append((path, body[0]))
        if path == LABS_COMPETITORS:
            return [{"items": [
                # 우리 자신 — 경쟁사 목록에서 빠지고 is_self=1 로 간다
                {"domain": "gt.com", "metrics": {"organic": {
                    "count": 40, "etv": 400.0, "pos_1": 1, "pos_2_3": 2, "pos_4_10": 3}}},
                # yaml tools 의 'ToolCo' — 등재 도구라 경쟁사가 아니다
                {"domain": "toolco.io", "metrics": {"organic": {"count": 10, "etv": 10.0}}},
                # 이미 manual 로 등록됨 — source 를 덮으면 안 된다
                {"domain": "rival.com", "metrics": {"organic": {
                    "count": 30, "etv": 300.0, "pos_2_3": 4}}},
                # 키워드가 겹쳐도 플랫폼은 경쟁사가 아니다 — 안 빼면 cand[:3] 이 밀려
                # second.co 가 빠진다(아래 1) 이 잡는다)
                {"domain": "m.blog.naver.com", "metrics": {"organic": {"count": 99, "etv": 9.0}}},
                # www 는 host_of 가 벗긴다
                {"domain": "www.newrival.com", "metrics": {"organic": {
                    "count": 20, "etv": 200.0, "pos_4_10": 5}}},
                # 숫자를 문자열로 주는 응답도 읽는다 / top10 은 안 주면 NULL
                {"domain": "second.co", "metrics": {"organic": {"count": "9", "etv": "90"}}},
                # 이미 manual 인데 필터를 통과한다 — INSERT 가 실제로 conflict 를 만난다
                {"domain": "hq.io", "metrics": {"organic": {"count": 5, "etv": 10.0}}},
                {"no_domain_field": 1},          # 못 읽는 항목은 건너뛰고 나머지는 산다
            ]}], 0.002
        if path == LABS_INTERSECT:
            rival, inter = body[0]["target2"], body[0]["intersections"]
            if inter:
                return [{"items": [
                    {"keyword_data": {"keyword": f"{rival} weak",
                                      "keyword_info": {"search_volume": 100}},
                     "first_domain_serp_element": {"rank_group": 9},
                     "second_domain_serp_element": {"rank_group": 2}},
                    {"keyword_data": {"keyword": f"{rival} shared",
                                      "keyword_info": {"search_volume": 50}},
                     "first_domain_serp_element": {"rank_group": 3},
                     "second_domain_serp_element": {"rank_group": 7}},
                    {"keyword_data": {"keyword": ""}},      # 키워드 못 읽음 — 이 항목만 버린다
                ]}], 0.001
            # intersections:false = 경쟁사만 잡은 것. 필드 모양을 일부러 다르게 준다.
            return [{"items": [
                {"keyword": f"{rival} missing", "search_volume": "30",
                 "target2_serp_element": {"rank_absolute": 4}},
            ]}], 0.001
        raise AssertionError(f"예상 밖 엔드포인트: {path}")

    def fake_fetch2(domain, locale, limit):
        return [{"keyword": f"{domain} 키워드", "search_volume": 5}], 0.0001

    # 순위 수집이 예전 규칙으로 넣어 둔 플랫폼 — 역키워드·Gap 대상이 되면 안 된다
    # (되면 역키워드가 5곳이 되어 아래 4) 의 콜 수가 8 이 된다)
    conn.execute("INSERT INTO competitors(project_id, domain, source)"
                 " VALUES(?, 'youtube.com', 'auto_rank')", (pid,))
    conn.commit()
    res = collect("gt", throttle=0, conn=conn, fetch=fake_fetch2, post=fake_post,
                  rivals=3, intersect=1)
    assert res.ok and res.cost > 0, res

    # 1) auto_labs 로 들어가고 manual 은 그대로 — 우리 자신·도구 이름·플랫폼은 빠진다
    comp = dict(conn.execute(
        "SELECT domain, source FROM competitors WHERE project_id=?", (pid,)).fetchall())
    assert comp == {"rival.com": "manual", "hq.io": "manual", "youtube.com": "auto_rank",
                    "newrival.com": "auto_labs", "second.co": "auto_labs"}, comp

    # 2) 우리(is_self=1)와 경쟁사가 같은 표에 — 등록 안 한 toolco.io 는 몫에도 없다
    met = {r0["domain"]: dict(r0) for r0 in conn.execute(
        "SELECT domain, is_self, keywords, etv, top10 FROM competitor_metrics "
        "WHERE project_id=?", (pid,)).fetchall()}
    assert set(met) == {"gt.com", "rival.com", "hq.io", "newrival.com", "second.co"}, met
    assert (met["gt.com"]["is_self"], met["gt.com"]["etv"]) == (1, 400.0), met["gt.com"]
    assert met["gt.com"]["top10"] == 6, met["gt.com"]        # pos_1 + pos_2_3 + pos_4_10
    assert (met["rival.com"]["is_self"], met["rival.com"]["top10"]) == (0, 4), met["rival.com"]
    assert (met["second.co"]["keywords"], met["second.co"]["etv"]) == (9, 90.0), met["second.co"]
    assert met["second.co"]["top10"] is None, "안 준 구간을 0으로 만들지 않는다"
    cols = {r0[1] for r0 in conn.execute("PRAGMA table_info(competitor_metrics)")}
    assert "share" not in cols, f"몫은 저장하지 않는다 — 조회 시 etv/SUM(etv): {cols}"
    denom = conn.execute(
        "SELECT SUM(etv) FROM competitor_metrics WHERE project_id=?", (pid,)).fetchone()[0]
    assert denom == 1000.0, denom                                  # 400+300+200+90+10
    assert abs(met["gt.com"]["etv"] / denom - 0.4) < 1e-9          # 몫은 이렇게 조회한다

    # 3) kind 셋이 위치 비교로 갈린다
    gaps = {r0["keyword"]: dict(r0) for r0 in conn.execute(
        "SELECT keyword, domain, position, our_position, volume, kind FROM keyword_gap "
        "WHERE project_id=?", (pid,)).fetchall()}
    assert set(gaps) == {"rival.com weak", "rival.com shared", "rival.com missing"}, gaps
    assert gaps["rival.com weak"]["kind"] == "weak", gaps          # 우리 9위 < 경쟁사 2위
    assert gaps["rival.com shared"]["kind"] == "shared", gaps      # 우리 3위 > 경쟁사 7위
    assert gaps["rival.com missing"]["kind"] == "missing", gaps    # 우리 위치 없음
    assert (gaps["rival.com missing"]["position"],
            gaps["rival.com missing"]["our_position"],
            gaps["rival.com missing"]["volume"]) == (4, None, 30), gaps
    assert gaps["rival.com weak"]["domain"] == "rival.com", gaps
    assert _kind(None, 3) == "missing" and _kind(5, 5) == "shared" and _kind(5, 2) == "weak"
    assert _kind(5, None) == "unknown"          # 경쟁사 순위를 모르면 누가 위인지 모른다

    # 4) 콜 모양 — 자동 탐지 1회, 교집합/부재 2축, 우리가 응답에 있으면 overview 안 부름
    paths = [q[0] for q in posts]
    assert paths.count(LABS_COMPETITORS) == 1, paths
    assert paths.count(LABS_INTERSECT) == 2, paths
    assert LABS_OVERVIEW not in paths, paths
    b0 = posts[0][1]
    assert (b0["target"], b0["limit"], b0["order_by"]) == ("gt.com", 3, ["intersections,desc"]), b0
    assert (b0["location_name"], b0["language_code"]) == ("South Korea", "ko"), b0
    assert {q[1]["intersections"] for q in posts if q[0] == LABS_INTERSECT} == {True, False}, posts
    assert {q[1]["target1"] for q in posts if q[0] == LABS_INTERSECT} == {"gt.com"}, posts
    calls = conn.execute("SELECT api_calls FROM runs ORDER BY id DESC LIMIT 1").fetchone()[0]
    assert calls == 7, f"역키워드 4 + 자동탐지 1 + 교집합 2 = 7: {calls}"

    # 우리 자신이 응답에 없으면 domain_rank_overview 가 분자를 채운다
    conn.execute("DELETE FROM competitor_metrics")
    conn.commit()
    posts.clear()

    def post_overview(path, body):
        posts.append((path, body[0]))
        if path == LABS_COMPETITORS:
            return [{"items": [{"domain": "rival.com",
                                "metrics": {"organic": {"count": 30, "etv": 300.0}}}]}], 0.001
        if path == LABS_OVERVIEW:
            # items 없이 result 가 곧 지표인 모양도 받는다
            return [{"metrics": {"organic": {"count": 44, "etv": 444.0, "pos_1": 2}}}], 0.001
        raise AssertionError(path)

    collect("gt", throttle=0, conn=conn, fetch=fake_fetch2, post=post_overview,
            rivals=1, intersect=0)
    assert LABS_OVERVIEW in [q[0] for q in posts], posts
    mine = conn.execute(
        "SELECT * FROM competitor_metrics WHERE project_id=? AND is_self=1", (pid,)).fetchone()
    assert (mine["domain"], mine["keywords"], mine["etv"], mine["top10"]) \
        == ("gt.com", 44, 444.0, 2), dict(mine)

    # 두 경로가 다 죽어도 축 하나가 전체를 죽이지 않는다 — 근사(개수만)로 분자를 남긴다
    conn.execute("DELETE FROM competitor_metrics")
    conn.commit()

    def post_no_overview(path, body):
        if path == LABS_COMPETITORS:
            return [{"items": [{"domain": "rival.com",
                                "metrics": {"organic": {"count": 30}}}]}], 0.001
        raise RuntimeError("overview 없음")

    res = collect("gt", throttle=0, conn=conn, fetch=fake_fetch2, post=post_no_overview,
                  rivals=1, intersect=0)
    assert res.ok, res
    mine = conn.execute(
        "SELECT * FROM competitor_metrics WHERE project_id=? AND is_self=1", (pid,)).fetchone()
    assert (mine["keywords"], mine["etv"], mine["top10"]) == (1, None, None), dict(mine)

    # 자동 탐지 축이 통째로 죽어도 역키워드·Gap 은 산다 (한 콜이 나머지를 못 죽인다)
    conn.execute("DELETE FROM keyword_gap")
    conn.commit()

    def post_dead(path, body):
        if path == LABS_COMPETITORS:
            raise RuntimeError("competitors_domain 장애")
        return fake_post(path, body)

    res = collect("gt", throttle=0, conn=conn, fetch=fake_fetch2, post=post_dead,
                  rivals=1, intersect=1)
    assert res.ok and not res.skipped, res
    assert conn.execute("SELECT COUNT(*) FROM keyword_gap").fetchone()[0] == 3, \
        "자동 탐지가 죽어도 Content Gap 축은 돌아야 한다"

    # 경쟁사 0건 = 건너뜀이지 실패가 아니다. ok=False 로 돌아가면 run_all 체인이
    # exit 1 → worker.py 의 주간 리포트 메일이 안 나간다 (test_collectors 의
    # run_all 테스트는 가짜 fn 을 주입해서 이 경로를 못 잡는다).
    # rivals=0/intersect=0 이면 새 축은 아무것도 안 부른다 — 지뢰로 확인.
    conn.execute("DELETE FROM competitors WHERE project_id=?", (pid,))
    conn.commit()
    res = collect("gt", conn=conn, fetch=boom, post=no_post, rivals=0, intersect=0)
    assert (res.ok, res.skipped) == (True, True), res
    assert res.reason, "왜 건너뛰었는지·다음에 뭘 할지 말해야 한다"
    assert off_calls == [], off_calls

    # 동시에 사되 적재는 한 스레드·목록 순서 — 앞 도메인일수록 늦게 오게 해도
    # 후보 키워드는 도메인 순서대로 쌓이고, 일꾼에서 센 콜 수가 새지 않는다.
    import threading
    import time
    rivals4 = ["c1.com", "c2.com", "c3.com", "c4.com"]
    conn.executemany("INSERT INTO competitors(project_id, domain, source) VALUES(?,?,'manual')",
                     [(pid, d) for d in rivals4])
    conn.commit()
    lock, live, on = threading.Lock(), {"now": 0, "peak": 0}, set()

    def busy(wait):
        with lock:
            live["now"] += 1
            live["peak"] = max(live["peak"], live["now"])
            on.add(threading.get_ident())
        time.sleep(wait)
        with lock:
            live["now"] -= 1

    def slow_fetch(domain, locale, limit):
        busy(0.02 * (5 - int(domain[1])))
        return [{"keyword": f"{domain} 동시 후보", "search_volume": 3}], 0.0001

    def slow_post(path, body):
        busy(0.01)
        return fake_post(path, body)

    res = collect("gt", throttle=0, conn=fanout.MainThreadOnly(conn), fetch=slow_fetch,
                  post=slow_post, rivals=0, intersect=2)
    assert (res.ok, res.partial) == (True, False), res
    got = [r0["keyword"] for r0 in conn.execute(
        "SELECT keyword FROM keywords WHERE keyword LIKE '%동시 후보' ORDER BY id")]
    assert got == [f"{d} 동시 후보" for d in rivals4], f"적재 순서가 도메인 순서가 아니다: {got}"
    assert conn.execute("SELECT COUNT(*) FROM keyword_gap WHERE domain IN ('c1.com','c2.com')"
                        ).fetchone()[0] == 6, "Gap 두 경쟁사 × 3행"
    calls = conn.execute("SELECT api_calls FROM runs ORDER BY id DESC LIMIT 1").fetchone()[0]
    assert calls == 8, f"역키워드 4 + 교집합 2×2 = 8: {calls}"
    assert 1 < live["peak"] <= fanout.LIMITS["dataforseo"], f"동시 상한: peak={live['peak']}"
    assert threading.get_ident() not in on, "Labs 호출이 메인 스레드에서 돌았다"

    conn.close()
    _roles_check()
    _openrouter_check()
    assert [site_of(h) for h in ("search.11st.co.kr", "m.gmarket.co.kr", "kr.louisvuitton.com",
                                 "www.dior.com", "shop.brand.com.au", "musinsa.com")] == \
        ["11st.co.kr", "gmarket.co.kr", "louisvuitton.com", "dior.com", "brand.com.au", "musinsa.com"]
    print("collect_gap self-check ok")


def _openrouter_check() -> None:
    """잘린 AI 답·JSON 없는 답은 오류다 — 빈 판정({})으로 읽으면 거르기 없이 골라 버린다."""
    import requests

    class R:
        def __init__(self, body):
            self.status_code, self._b = 200, body
        def json(self):
            return self._b

        def raise_for_status(self):
            return None

        text = ""

    real = requests.post
    os.environ["OPENROUTER_API_KEY"] = "test"
    try:
        for body, ok in (
                ({"choices": [{"finish_reason": "stop", "message": {"content": '```json\n{"a": 1}\n```'}}]}, True),
                # 진짜 잘림 모양 — 안쪽 } 가 있어 괄호만 보면 멀쩡해 보인다
                ({"choices": [{"finish_reason": "length",
                               "message": {"content": '{"brand_terms": {"a": 1}, "drop": ["매장'}}]}, False),
                ({"choices": [{"finish_reason": "stop", "message": {"content": "모르겠습니다"}}]}, False)):
            requests.post = lambda *a, _b=body, **k: R(_b)
            try:
                got = openrouter_json("x")
                assert ok and got == {"a": 1}, got
            except RuntimeError:
                assert not ok, f"멀쩡한 답을 오류로 봤다: {body}"
    finally:
        requests.post = real
        os.environ.pop("OPENROUTER_API_KEY", None)


def _roles_check() -> None:
    """D·E — 브랜드 사이트(구찌 모양): 겹침만 보면 쇼핑몰이 경쟁사가 된다(2026-09-29 실측).

    D 는 브랜드명을 뺀 검색어로 경쟁 브랜드를 찾고, E 는 후보의 상위 검색어로 쇼핑몰을
    판매 채널로 가른다. 판매 채널은 경쟁사로 안 읽히고(scoring.rivals) 몫의 분모에도 안
    든다. 사람이 적은 경쟁사는 판정하지 않는다. AI 가 지어낸 역할·목록 밖 브랜드 표기는 버린다.
    """
    import contextlib
    import io
    import json
    conn = db.connect()
    pid = db.register_project(conn, {"name": "gc", "domain": "gucci.test", "locale": "ko-KR",
                                     "type": "commerce", "brand_aliases": ["gucci"]})
    conn.execute("INSERT INTO competitors(project_id, domain, source) VALUES(?, 'hermes.test', 'manual')",
                 (pid,))
    conn.commit()
    posts, asked = [], []

    def post(path, body):
        b0 = body[0]
        posts.append((path, b0))
        if path == LABS_COMPETITORS:      # B — 겹침 순: 쇼핑몰이 위
            return [{"items": [{"domain": "gucci.test", "metrics": {"organic": {"count": 50, "etv": 500.0}}},
                               {"domain": "lfmall.test", "metrics": {"organic": {"count": 40, "etv": 400.0}}},
                               {"domain": "hermes.test", "metrics": {"organic": {"count": 9, "etv": 90.0}}}]}], 0.01
        if path == LABS_RANKED and b0["target"] == "gucci.test":     # D — 우리 검색어
            return [{"items": [{"keyword_data": {"keyword": k, "keyword_info": {"search_volume": v}}}
                               for k, v in (("구찌 가방", 9000), ("구찌", 50000), ("명품 가방", 7000),
                                            ("gucci bag", 800), ("여성 지갑", 3000))]}], 0.01
        if path == LABS_RANKED:                                      # E — 후보의 상위 검색어
            ev = {"lfmall.test": ["lf몰", "구찌 가방", "크롬하츠 후드"],
                  "dior.test": ["디올", "디올 가방", "디올 향수"],
                  "old3.test": ["올드쓰리", "올드쓰리 가방"]}.get(b0["target"], [])
            return [{"items": [{"keyword_data": {"keyword": k}} for k in ev]}], 0.01
        if path == LABS_SERP_COMPETITORS:
            assert set(b0["keywords"]) == {"명품 가방", "여성 지갑"}, \
                f"브랜드명(구찌·gucci)이 든 검색어가 비브랜드 발견에 섞였다: {b0['keywords']}"
            assert b0["keywords"] == ["명품 가방", "여성 지갑"], "검색량 큰 순이 아니다"
            return [{"items": [{"domain": "www.dior.test"}, {"domain": "gucci.test"},
                               {"domain": "namu.wiki"}]}], 0.01
        if path == LABS_INTERSECT:
            return [{"items": []}], 0.0
        raise AssertionError(f"예상 밖 엔드포인트: {path}")

    def ask(prompt):
        asked.append(prompt)
        if "brand_terms" in prompt:
            return {"brand_terms": ["구찌", "지어낸이름"]}       # 목록 밖 표기는 버려져야 한다
        return {"lfmall.test": "channel", "dior.test": "rival", "hermes.test": "channel",
                "old3.test": "rival",
                "nowhere.test": "rival", "x": "지어낸역할"}

    def fetch(domain, locale, limit):
        return [], 0.0

    with contextlib.redirect_stdout(io.StringIO()):
        res = collect("gc", throttle=0, conn=conn, fetch=fetch, post=post, ask=ask,
                      rivals=5, intersect=0)
    assert res.ok, res
    rows = {r["domain"]: dict(r) for r in conn.execute(
        "SELECT domain, source, role, role_why FROM competitors WHERE project_id=?", (pid,))}
    assert rows["dior.test"]["source"] == "auto_nonbrand", rows      # D 가 경쟁 브랜드를 찾았다
    assert "namu.wiki" not in rows and "gucci.test" not in rows, "플랫폼·우리 자신이 후보가 됐다"
    assert rows["lfmall.test"]["role"] == "channel" and rows["dior.test"]["role"] == "rival", rows
    assert json.loads(rows["lfmall.test"]["role_why"])[0] == "lf몰", "판정 근거가 안 남았다"
    assert rows["hermes.test"]["role"] is None, "사람이 적은 경쟁사를 판정했다"
    # 판정 프롬프트에 사람이 적은 것은 안 들어간다 — 역할을 돌려줘도 안 쓴다
    judge = [q for q in asked if "brand_terms" not in q][0]
    assert "hermes.test" not in judge and "쇼핑몰 · 브랜드" in judge, judge
    # 경쟁사로 읽기: 판매 채널은 빠지고 사람이 적은 것·경쟁은 남는다
    keep, dropped = scoring.rivals(conn, pid, "gucci.test")
    assert keep == ["hermes.test", "dior.test"] and "lfmall.test" in dropped, (keep, dropped)
    assert scoring.confirmed_rivals(conn, pid, "gucci.test") == ["hermes.test", "dior.test"]
    # 몫의 분모에 판매 채널이 없다
    met = {r[0] for r in conn.execute("SELECT domain FROM competitor_metrics WHERE project_id=?", (pid,))}
    assert "lfmall.test" not in met and {"gucci.test", "hermes.test"} <= met, met
    # 두 번째 런 — 이미 판정한 도메인은 다시 사지 않는다(후보당 ranked_keywords 한 콜이다)
    posts.clear()
    with contextlib.redirect_stdout(io.StringIO()):
        collect("gc", throttle=0, conn=conn, fetch=fetch, post=post, ask=ask, rivals=5, intersect=0)
    again = [b["target"] for pth, b in posts if pth == LABS_RANKED and b["target"] != "gucci.test"]
    assert again == [], f"이미 판정한 도메인의 근거를 또 샀다: {again}"
    # 새 후보가 없어도 비브랜드 축(D)은 돈다 — 새 후보가 생길 때만 도는 축이 아니다
    assert any(pth == LABS_SERP_COMPETITORS for pth, _ in posts), "새 후보가 없다고 D 를 건너뛰었다"

    # 판정 전에 들어온 옛 후보(순위 수집이 붙인 source='auto')도 판정한다. 근거를 못 받는
    # 후보가 앞자리에 있어도 뒤 후보가 굶지 않는다 — 들어온 순으로만 자르던 때는 근거 없는
    # 옛 후보 ROLE_CAP 개가 매 런 같은 자리를 먹어 old3 이 영영 판정을 못 받았다.
    global ROLE_CAP
    cap = ROLE_CAP
    conn.executemany("INSERT INTO competitors(project_id, domain, source) VALUES(?, ?, 'auto')",
                     [(pid, d) for d in ("old1.test", "old2.test", "old3.test")])
    conn.commit()
    try:
        ROLE_CAP = 2
        for _ in range(2):
            with contextlib.redirect_stdout(io.StringIO()):
                collect("gc", throttle=0, conn=conn, fetch=fetch, post=post, ask=ask,
                        rivals=5, intersect=0)
    finally:
        ROLE_CAP = cap
    old = {r[0]: (r[1], r[2]) for r in conn.execute(
        "SELECT domain, role, role_why FROM competitors WHERE domain LIKE 'old%'")}
    assert old["old3.test"][0] == "rival", f"근거 없는 옛 후보에 막혀 old3 을 판정 못 했다: {old}"
    assert old["old1.test"] == (None, "[]"), f"근거 없는 후보의 시도 흔적이 안 남았다: {old}"
    assert "old3.test" in scoring.rivals(conn, pid, "gucci.test")[0]
    # 판정 전 후보(old1)는 경쟁사로 읽히지만(rivals) 확인된 경쟁사는 아니다 — 화면이
    # "경쟁사를 적거나 찾으세요"를 이 수로 가른다
    assert "old1.test" in scoring.rivals(conn, pid, "gucci.test")[0]
    assert "old1.test" not in scoring.confirmed_rivals(conn, pid, "gucci.test")
    assert "old3.test" in scoring.confirmed_rivals(conn, pid, "gucci.test")
    # AI 가 없으면 D·E 는 안 돈다 — 예전 동작 그대로
    posts.clear()
    with contextlib.redirect_stdout(io.StringIO()):
        collect("gc", throttle=0, conn=conn, fetch=fetch, post=post, rivals=5, intersect=0)
    assert not [1 for pth, _ in posts if pth in (LABS_RANKED, LABS_SERP_COMPETITORS)], posts
    conn.close()


if __name__ == "__main__":
    main()
