#!/usr/bin/env python3
"""SERP providers — unified adapter (DataForSEO Live Advanced / Serper.dev).

fetch(provider, keyword, locale) -> normalized dict:
  position      own-domain rank in organic (None if absent in top depth)
  url           own ranking URL
  top           [{pos, domain, url, title}] organic top-N
  serp_features [feature type strings]
  aio_present / aio_cited   AI Overview flags (DataForSEO only; Serper -> None)
  related / paa cost        free byproducts + actual billed cost when reported

Env:
  DataForSEO  DATAFORSEO_LOGIN + DATAFORSEO_PASSWORD  (~$0.002-0.003/query live adv.)
  Serper      SERPER_API_KEY                           (credits; no AIO data)
"""
import json
import os
from urllib.parse import urlparse

LOCATION_MAP = {  # locale prefix -> (dataforseo location_name, language_code, serper gl/hl)
    "ko": ("South Korea", "ko", ("kr", "ko")),
    "en": ("United States", "en", ("us", "en")),
    "ja": ("Japan", "ja", ("jp", "ja")),
}


def _norm(url: str) -> str:
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def _match(domain: str, own: str) -> bool:
    return domain == own or domain.endswith("." + own)


def fetch_dataforseo(keyword: str, locale: str, own_domain: str, depth: int = 10) -> dict:
    import requests
    login, pw = os.environ.get("DATAFORSEO_LOGIN"), os.environ.get("DATAFORSEO_PASSWORD")
    if not (login and pw):
        raise RuntimeError("DATAFORSEO_LOGIN / DATAFORSEO_PASSWORD not set")
    loc, lang, _ = LOCATION_MAP.get(locale.split("-")[0], LOCATION_MAP["en"])
    r = requests.post(
        "https://api.dataforseo.com/v3/serp/google/organic/live/advanced",
        auth=(login, pw), timeout=60,
        json=[{"keyword": keyword, "location_name": loc, "language_code": lang,
               "device": "desktop", "depth": depth}])
    r.raise_for_status()
    data = r.json()
    task = (data.get("tasks") or [{}])[0]
    if task.get("status_code", 0) >= 40000:
        raise RuntimeError(f"dataforseo task error: {task.get('status_message')}")
    items = ((task.get("result") or [{}])[0].get("items")) or []
    own = own_domain.lower().removeprefix("www.")
    top, position, url = [], None, None
    features, aio_present, aio_cited = set(), False, False
    related, paa = [], []
    for it in items:
        t = it.get("type")
        if t == "organic":
            d = _norm(it.get("url", "")) or (it.get("domain") or "").lower()
            top.append({"pos": it.get("rank_group"), "domain": d,
                        "url": it.get("url"), "title": it.get("title")})
            if position is None and _match(d, own):
                position, url = it.get("rank_group"), it.get("url")
        elif t == "ai_overview":
            aio_present = True
            aio_cited = aio_cited or (own in json.dumps(it).lower())
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
    return {"position": position, "url": url, "top": top[:depth],
            "serp_features": sorted(features), "aio_present": aio_present,
            "aio_cited": aio_cited if aio_present else None,
            "related": related, "paa": paa, "cost": float(data.get("cost") or 0)}


def fetch_serper(keyword: str, locale: str, own_domain: str, depth: int = 10) -> dict:
    import requests
    key = os.environ.get("SERPER_API_KEY")
    if not key:
        raise RuntimeError("SERPER_API_KEY not set")
    _, _, (gl, hl) = LOCATION_MAP.get(locale.split("-")[0], LOCATION_MAP["en"])
    r = requests.post("https://google.serper.dev/search", timeout=30,
                      headers={"X-API-KEY": key, "Content-Type": "application/json"},
                      json={"q": keyword, "gl": gl, "hl": hl, "num": depth})
    r.raise_for_status()
    data = r.json()
    own = own_domain.lower().removeprefix("www.")
    top, position, url = [], None, None
    for it in data.get("organic", []):
        d = _norm(it.get("link", ""))
        top.append({"pos": it.get("position"), "domain": d,
                    "url": it.get("link"), "title": it.get("title")})
        if position is None and _match(d, own):
            position, url = it.get("position"), it.get("link")
    features = sorted(k for k in ("answerBox", "knowledgeGraph", "peopleAlsoAsk",
                                  "topStories", "images") if k in data)
    return {"position": position, "url": url, "top": top,
            "serp_features": features, "aio_present": None, "aio_cited": None,
            "related": [x.get("query") for x in data.get("relatedSearches", []) if x.get("query")],
            "paa": [x.get("question") for x in data.get("peopleAlsoAsk", []) if x.get("question")],
            "cost": 0.001}  # ~1 credit; actual $/credit depends on your pack


PROVIDERS = {"dataforseo": fetch_dataforseo, "serper": fetch_serper}


def detect_provider() -> str | None:
    if os.environ.get("DATAFORSEO_LOGIN"):
        return "dataforseo"
    if os.environ.get("SERPER_API_KEY"):
        return "serper"
    return None


def fetch(provider: str, keyword: str, locale: str, own_domain: str, depth: int = 10) -> dict:
    return PROVIDERS[provider](keyword, locale, own_domain, depth)
