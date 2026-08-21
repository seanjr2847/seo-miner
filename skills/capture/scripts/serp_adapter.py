#!/usr/bin/env python3
"""SERP providers — unified adapter (DataForSEO Live Advanced / Serper.dev).

제공자를 아는 곳은 여기 하나다. 단가표·"이 제공자는 뭘 못 잰다" 주의사항·
제공자별 None 정규화가 호출부에도 복제돼 있던 시절엔, 제공자를 하나 더 붙이려면
collect_serp.py의 가격표와 `if provider == "serper"` 분기까지 같이 고쳐야 했고
실제로 한쪽만 고쳐 값이 어긋났다.

fetch(provider, keyword, locale, depth) -> normalized dict:
  top           [{pos, domain, url, title}] organic top-N
                (내 도메인이 몇 위인지는 호출부가 판단한다 — 어댑터는 누가 '나'인지
                 알 필요가 없고, 알면 같은 규칙이 양쪽에 두 벌로 생긴다)
  serp_features [feature type strings]
  aio_present   1 / 0 / None(제공자가 측정하지 않음) — 그대로 DB에 넣을 수 있는 값
  aio_domains   AI Overview 안에서 인용된 도메인들 (판정은 호출부의 scoring.owns)
  related / paa 무료 부산물
  cost          제공자가 보고한 실청구액, 없으면 제공자 단가

Env:
  DataForSEO  DATAFORSEO_LOGIN + DATAFORSEO_PASSWORD  (~$0.002-0.003/query live adv.)
  Serper      SERPER_API_KEY                           (credits; no AIO data)

self-check:  python serp_adapter.py
"""
import os
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))
import collector  # noqa: E402
import scoring  # noqa: E402

# HTTP 타임아웃 정본 — 각 스크립트에 리터럴로 적지 않는다
TIMEOUTS = {
    "dataforseo": 60,
    "serper": 30,
    "openrouter": 120,
    "suggest": 10,
}


def has_dataforseo() -> bool:
    return bool(os.environ.get("DATAFORSEO_LOGIN")) and bool(os.environ.get("DATAFORSEO_PASSWORD"))


def has_serper() -> bool:
    return bool(os.environ.get("SERPER_API_KEY"))


def has_openrouter() -> bool:
    return bool(os.environ.get("OPENROUTER_API_KEY"))


def _check_dataforseo_task(data: dict) -> dict:
    task = (data.get("tasks") or [{}])[0]
    if task.get("status_code", 0) >= 40000:
        raise RuntimeError(f"dataforseo task error: {task.get('status_message')}")
    return task

LOCATION_MAP = {  # locale prefix -> (dataforseo location_name, language_code, serper gl/hl)
    "ko": ("South Korea", "ko", ("kr", "ko")),
    "en": ("United States", "en", ("us", "en")),
    "ja": ("Japan", "ja", ("jp", "ja")),
    "de": ("Germany", "de", ("de", "de")),
    "fr": ("France", "fr", ("fr", "fr")),
    "es": ("Spain", "es", ("es", "es")),
    "it": ("Italy", "it", ("it", "it")),
    "pt": ("Brazil", "pt", ("br", "pt")),  # Brazil — 화자 다수
    "nl": ("Netherlands", "nl", ("nl", "nl")),
    "pl": ("Poland", "pl", ("pl", "pl")),
    "ru": ("Russia", "ru", ("ru", "ru")),
    "tr": ("Turkey", "tr", ("tr", "tr")),
    "vi": ("Vietnam", "vi", ("vn", "vi")),
    "th": ("Thailand", "th", ("th", "th")),
    "id": ("Indonesia", "id", ("id", "id")),
    "ar": ("United Arab Emirates", "ar", ("ae", "ar")),
    "hi": ("India", "hi", ("in", "hi")),
    "zh": ("Taiwan", "zh", ("tw", "zh")),  # Taiwan — 구글 접근 가능 지역
}


def location(locale: str) -> tuple:
    """locale -> (location_name, language_code, (gl, hl)). 매핑에 없으면 미국/영어.

    조회용 순수 함수다. 경고는 warn_unmapped()가 한다 — 예전엔 이 함수가 경고까지
    겸해서, 호출부가 값도 안 쓸 거면서 부작용만 보고 이걸 호출하고 있었다.
    """
    return LOCATION_MAP.get((locale or "").split("-")[0].lower(), LOCATION_MAP["en"])


def warn_unmapped(locale: str) -> bool:
    """매핑 없는 로케일이면 경고하고 True. 돈을 쓰기 전에 호출부가 부른다.

    폴백 자체는 막지 않는다. 다만 조용히 하면 안 된다 — 독일어 프로젝트가 미국
    SERP를 재고 "순위 없음"으로 적재돼도 아무도 눈치채지 못한다(한국어 키워드에서
    이미 한 번 겪은 일).
    """
    key = (locale or "").split("-")[0].lower()
    if key in LOCATION_MAP:
        return False
    print(f"[주의] '{locale}' 로케일 매핑이 없어 United States/en 으로 조회합니다 — "
          f"이 언어권의 순위가 아닙니다. serp_adapter.py의 LOCATION_MAP에 "
          f"'{key}' 한 줄을 추가하세요.", file=sys.stderr)
    return True


def _domains_in(obj) -> list[str]:
    """AI Overview 응답에서 '인용된 도메인'만 호스트 목록으로.

    예전에는 응답 트리 전체에서 url/domain/link/source_url 을 무차별 수집해
    ai_overview 안의 images[].url (이미지 CDN) 이나 본문 내 참조 없는 URL 까지
    섞였다 — '내 도메인이 인용됐다' 판정의 거짓 양성이 여기서 들어왔다.

    DataForSEO ai_overview 의 실제 인용 필드 구조 (live advanced 응답):
      items: [
        {
          "type": "ai_overview_element",
          "markdown": "...",
          "title":   "...",
          "images":     [{"url": "...",   source_url": "..."}],  # 인용 아님 — CDN/원본
          "links":      [{"url": "...",   "title": "..."}],      # 인용 — 인라인 링크
          "references": [{"url": "...",   "title": "..."}],      # 인용 — 하단 출처
          "citations":  [{"url": "...",   "domain": "..."}],      # 인용(일부 응답)
          "sources":    [{"url": "...",   "source": "..."}],      # 인용(보수적 별칭)
        }
      ]
    따라서 조상 경로에 references / citations / sources / links 중 하나가 있는
    url·domain·link·source_url 만 채택 — 인용 구조 안의 링크만 본다.
    """
    out, stack = [], [(obj, ())]
    cite = {"references", "citations", "sources", "links"}
    while stack:
        cur, ancestors = stack.pop()
        if isinstance(cur, dict):
            for k, v in cur.items():
                if isinstance(v, str) and k in ("url", "domain", "link", "source_url"):
                    if any(p in cite for p in ancestors):
                        host = scoring.host_of(v)
                        if host:
                            out.append(host)
                else:
                    stack.append((v, ancestors + (k,)))
        elif isinstance(cur, list):
            # 리스트 항목 자체엔 키가 없다 — 조상 키를 그대로 넘긴다
            for item in cur:
                stack.append((item, ancestors))
    return sorted(set(out))


def fetch_dataforseo(keyword: str, locale: str, depth: int = 10, device: str = "desktop") -> dict:
    if device not in ("desktop", "mobile"):
        raise ValueError(f"device must be 'desktop' or 'mobile', got {device!r}")
    if not has_dataforseo():
        raise RuntimeError("DATAFORSEO_LOGIN / DATAFORSEO_PASSWORD not set")
    login, pw = os.environ["DATAFORSEO_LOGIN"], os.environ["DATAFORSEO_PASSWORD"]
    loc, lang, _ = location(locale)
    r = requests.post(
        "https://api.dataforseo.com/v3/serp/google/organic/live/advanced",
        auth=(login, pw), timeout=TIMEOUTS["dataforseo"],
        json=[{"keyword": keyword, "location_name": loc, "language_code": lang,
               "device": device, "depth": depth}])
    r.raise_for_status()
    data = r.json()
    task = _check_dataforseo_task(data)
    items = ((task.get("result") or [{}])[0].get("items")) or []
    top, features, aio_present, aio_domains = [], set(), 0, []
    related, paa = [], []
    for it in items:
        t = it.get("type")
        if t == "organic":
            # url이 비면 domain 필드로. 둘 다 같은 정규화를 거쳐야 경쟁사 집계에
            # 'www.x.com'과 'x.com'이 따로 쌓이지 않는다.
            d = scoring.host_of(it.get("url") or it.get("domain") or "")
            top.append({"pos": it.get("rank_group"), "domain": d,
                        "url": it.get("url"), "title": it.get("title")})
        elif t == "ai_overview":
            aio_present = 1
            aio_domains += _domains_in(it)
            features.add(t)
        elif t == "related_searches":
            related += [x for x in (it.get("items") or []) if isinstance(x, str)]
        elif t == "people_also_ask":
            for q in (it.get("items") or []):
                if isinstance(q, dict) and q.get("title"):
                    paa.append(q["title"])
            features.add(t)
        else:
            features.add(t)
    return {"top": top[:depth], "serp_features": sorted(features),
            "aio_present": aio_present, "aio_domains": sorted(set(aio_domains)),
            "related": related, "paa": paa, "cost": float(data.get("cost") or 0)}


def fetch_serper(keyword: str, locale: str, depth: int = 10, device: str = "desktop") -> dict:
    if device not in ("desktop", "mobile"):
        raise ValueError(f"device must be 'desktop' or 'mobile', got {device!r}")
    if device == "mobile":
        print("[경고] serper는 desktop만 — 이 런은 desktop으로 측정됩니다", file=sys.stderr)
    if not has_serper():
        raise RuntimeError("SERPER_API_KEY not set")
    key = os.environ["SERPER_API_KEY"]
    _, _, (gl, hl) = location(locale)
    r = requests.post("https://google.serper.dev/search", timeout=TIMEOUTS["serper"],
                      headers={"X-API-KEY": key, "Content-Type": "application/json"},
                      json={"q": keyword, "gl": gl, "hl": hl, "num": depth})
    r.raise_for_status()
    data = r.json()
    top = [{"pos": it.get("position"), "domain": scoring.host_of(it.get("link", "")),
            "url": it.get("link"), "title": it.get("title")}
           for it in data.get("organic", [])]
    features = sorted(k for k in ("answerBox", "knowledgeGraph", "peopleAlsoAsk",
                                  "topStories", "images") if k in data)
    # aio_present=None: "AI Overview가 없었다"가 아니라 "재지 않았다". 0으로 적으면
    # 나중에 노출률 계산에서 분모에 섞여 들어간다.
    return {"top": top, "serp_features": features,
            "aio_present": None, "aio_domains": [],
            "related": [x.get("query") for x in data.get("relatedSearches", []) if x.get("query")],
            "paa": [x.get("question") for x in data.get("peopleAlsoAsk", []) if x.get("question")]}


# 제공자 지식은 전부 여기: 호출 함수 + 단가 + 이 제공자로 재면 무엇이 빠지는지.
PROVIDERS = {
    "dataforseo": {
        "fetch": fetch_dataforseo,
        "cost": 0.003,   # live advanced 상한. 실청구액은 응답에서 덮어쓴다.
        "caveats": [],
    },
    "serper": {
        "fetch": fetch_serper,
        "cost": 0.001,   # ~1 credit; actual $/credit depends on your pack
        "caveats": [
            "serper는 AI Overview를 측정하지 않는다 (aio_* = NULL, '없음'이 아님)",
            "serper는 모바일 측정을 지원하지 않는다 (desktop 고정)",
        ],
    },
}

# Labs 단가 상한 (출처: https://dataforseo.com/apis/dataforseo-labs-api).
# 응답의 cost 가 더 정확하지만 dry-run 고지용으로는 이 값으로 충분.
LABS_COST_PER_CALL = 0.001


def fetch_labs_ranked_keywords(target: str, locale: str, limit: int = 100) -> tuple[list[dict], float]:
    """Labs ranked_keywords 한 도메인 조회.

    반환: (items, cost). items 는 [{keyword, search_volume|None}, ...].
    Labs 응답의 키워드는 lowercase 정규화해서 돌려준다 — 후속 필터(norm 비교)와
    같은 모양.
    """
    if not has_dataforseo():
        raise RuntimeError("DATAFORSEO_LOGIN / DATAFORSEO_PASSWORD not set")
    login, pw = os.environ["DATAFORSEO_LOGIN"], os.environ["DATAFORSEO_PASSWORD"]
    loc, lang, _ = location(locale)
    body = [{
        "target": target,
        "location_name": loc,
        "language_code": lang,
        "limit": limit,
        "load_rank_absolute": False,
    }]
    r = requests.post(
        "https://api.dataforseo.com/v3/dataforseo_labs/google/ranked_keywords/live",
        auth=(login, pw), timeout=TIMEOUTS["dataforseo"], json=body)
    r.raise_for_status()
    data = r.json()
    task = _check_dataforseo_task(data)
    cost = float(task.get("cost") or data.get("cost") or 0.0)
    result = (task.get("result") or [])
    items: list[dict] = []
    for r0 in result:
        for it in (r0.get("items") or []):
            kw = it.get("keyword_data") or it.get("keyword") or ""
            if isinstance(it.get("keyword_data"), dict):
                # Live 응답은 keyword_data 가 dict 모양으로 옴 (서로 다른 필드명 모두 허용)
                kd = it["keyword_data"]
                kw = kd.get("keyword") or kw
                sv = kd.get("keyword_info", {}).get("search_volume") if isinstance(
                    kd.get("keyword_info"), dict) else None
                if sv is None:
                    sv = kd.get("search_volume")
            sv = it.get("search_volume", sv)
            if isinstance(sv, str):
                sv = int(sv) if sv.isdigit() else None
            kw = (kw or "").strip()
            if kw:
                items.append({"keyword": kw, "search_volume": sv})
    return items, cost


def cost_per_query(provider: str) -> float:
    """제공자 단가($/쿼리) — 예산 고지용. 가격표는 이 모듈에만 있다."""
    return PROVIDERS[provider]["cost"]


def caveats(provider: str) -> list[str]:
    """이 제공자로 재면 무엇이 빠지는지. 호출부가 제공자 이름으로 분기하지 않게 한다."""
    return PROVIDERS[provider]["caveats"]


def detect_provider() -> str | None:
    """config.yaml serp.provider 가 이름을 지정했으면 그게 이긴다(auto = 키 감지).

    설정 파일에 항목만 있고 읽는 코드가 없어서, 키를 둘 다 넣어둔 사람은
    dataforseo로 고정돼 있었다.
    """
    want = ((collector.config().get("serp") or {}).get("provider") or "auto")
    if want in PROVIDERS:
        return want
    if want != "auto":
        print(f"[경고] config.yaml serp.provider='{want}' 는 모르는 제공자입니다 — "
              f"키 감지로 진행합니다.", file=sys.stderr)
    if has_dataforseo():
        return "dataforseo"
    if has_serper():
        return "serper"
    return None


def fetch(provider: str, keyword: str, locale: str, depth: int = 10, device: str = "desktop") -> dict:
    """정규화된 SERP 한 건. 제공자가 안 채운 자리는 여기서 메운다 —
    호출부가 `if provider == ...` 로 None을 해석하지 않게."""
    if device not in ("desktop", "mobile"):
        raise ValueError(f"device must be 'desktop' or 'mobile', got {device!r}")
    spec = PROVIDERS[provider]
    res = spec["fetch"](keyword, locale, depth, device=device)
    res.setdefault("cost", spec["cost"])   # 실청구액을 안 주는 제공자는 단가로 계상
    res.setdefault("aio_domains", [])
    res.setdefault("aio_present", None)    # None = 측정 안 함. 0("없었다")과 다르다.
    if res["aio_present"] is not None:
        res["aio_present"] = int(res["aio_present"])
    return res


def _selfcheck() -> None:
    assert location("ko-KR")[0] == "South Korea"
    assert location("de-DE")[0] == "Germany"
    assert location("pt-BR")[0] == "Brazil"
    assert location("zh-TW")[0] == "Taiwan"
    assert location("xx-XX") == LOCATION_MAP["en"]      # 매핑 없으면 미국/영어 폴백
    assert warn_unmapped("ko-KR") is False
    assert warn_unmapped("de-DE") is False
    assert warn_unmapped("xx-XX") is True               # 폴백은 하되 한 번은 말한다
    # 인용 밖의 url/domain은 빠진다 — 'items' 리스트 아래 직속 dict엔 citation 키 없음
    assert _domains_in({"items": [{"url": "https://www.Example.com/a"},
                                  {"x": {"domain": "b.co"}}]}) == []
    assert _domains_in({"title": "example.com 은 텍스트일 뿐"}) == []
    # DataForSEO ai_overview 형태: images[].url 은 CDN 이라 인용 아님, links/references 만 채택
    aio = {"type": "ai_overview",
           "items": [{"type": "ai_overview_element",
                      "title": "AI 답변",
                      "markdown": "본문 안의 텍스트",
                      "images":     [{"url":      "https://img-cdn.example.com/p.png",
                                      "source_url": "https://example.com/img"}],
                      "links":      [{"url": "https://link-a.com/",      "title": "A"},
                                     {"url": "https://link-b.com/page",  "title": "B"}],
                      "references": [{"url": "https://ref-c.com/",       "title": "C"}]},
                     {"type": "ai_overview_element",
                      "citations": [{"url": "https://cite-d.com/",       "domain": "d.com"}]}]}
    assert _domains_in(aio) == ["cite-d.com", "d.com", "link-a.com", "link-b.com", "ref-c.com"], \
        _domains_in(aio)
    # 조상 깊이 무관 — images > items 까지 깊어도 images 가 cite 키 아니라 차단
    nest = {"items": [{"type": "ai_overview_element",
                       "images": [{"items": [{"url": "https://nested.example.com/x"}]}]}]}
    assert _domains_in(nest) == [], _domains_in(nest)
    assert cost_per_query("serper") < cost_per_query("dataforseo")
    assert caveats("dataforseo") == []
    assert any("모바일" in c for c in caveats("serper"))
    assert set(PROVIDERS) == {"dataforseo", "serper"}
    assert TIMEOUTS == {"dataforseo": 60, "serper": 30, "openrouter": 120, "suggest": 10}
    assert LABS_COST_PER_CALL == 0.001
    print("serp_adapter self-check ok")


if __name__ == "__main__":
    _selfcheck()
