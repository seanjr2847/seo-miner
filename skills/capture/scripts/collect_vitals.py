#!/usr/bin/env python3
"""속도 수집 — Core Web Vitals(LCP·INP·CLS)를 페이지·기기별로 잰다.

이 스킬은 "모바일 12위 · 데스크톱 4위" 를 말할 수 있으면서도, **왜 모바일에서만
밀리는지**의 재료를 하나도 안 갖고 있었다. 기기 격차 요청문의 근거는 순위 차이
한 줄뿐인데 규칙은 "위 근거에 없는 것은 짐작하지 않습니다" 라, 그 요청문은 구조상
빈손으로 끝났다. 여기서 그 구멍을 메운다.

두 가지를 같이 남긴다. 뜻이 다르기 때문이다:

- **현장(field)**: 실제 크롬 사용자에게서 모은 28일치(CrUX). 검색이 보는 값이
  그것이다. 트래픽이 적은 페이지에는 없고, 그때 구글은 사이트 전체(오리진) 값을
  대신 준다 — `origin_fallback` 으로 갈라 적는다. 뭉뚱그리면 "이 페이지가 느리다"
  라고 잘못 말하게 된다.
- **실험실(lab)**: 지금 한 번 열어 잰 값(Lighthouse). 고친 뒤 바로 확인할 수 있는
  유일한 숫자다 — 현장 값은 28일이 지나야 움직인다.

비용: 없다. PageSpeed Insights API 는 무료다. 키(PAGESPEED_API_KEY)는 없어도 돌고
넣으면 한도가 넉넉해진다 — 그래서 키가 없다고 단계를 건너뛰지 않는다.

한도(429)는 **실패가 아니라 건너뜀**이다. 키 없이 돌면 여러 사용자와 나눠 쓰는 하루
한도에 걸리는데, 그건 내 사이트가 느리다는 뜻도 수집이 고장났다는 뜻도 아니다. 여태는
429 가 항목 실패로 세어져 속도 단계가 "전부 실패" 가 되고, 그 하나 때문에 런 전체가
last_ok=0 으로 남았다(9/24 theotherskin). 한 번 429 면 나머지도 같은 한도에 걸리므로
거기서 멈추고, 그때까지 잰 것은 남긴다.

Usage:
  python collect_vitals.py --project NAME [--limit N] [--strategy mobile,desktop]
  python collect_vitals.py                                  # self-check
"""
import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import collect_page  # noqa: E402
import collector  # noqa: E402
import db  # noqa: E402
import fanout  # noqa: E402
import serp_adapter  # noqa: E402

API = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
STRATEGIES = ("mobile", "desktop")

# PSI 응답에서 우리가 읽는 자리. 이름을 여기 한 벌로 두는 이유는 구글이 지표를
# 갈아 끼울 때(FID → INP 가 그랬다) 고칠 자리가 한 곳이어야 하기 때문이다.
_FIELD = {"field_lcp_ms": "LARGEST_CONTENTFUL_PAINT_MS",
          "field_inp_ms": "INTERACTION_TO_NEXT_PAINT",
          "field_ttfb_ms": "EXPERIMENTAL_TIME_TO_FIRST_BYTE"}
_LAB = {"lab_lcp_ms": "largest-contentful-paint",
        "lab_tbt_ms": "total-blocking-time"}

# 구글이 연 모습으로 본 SEO 점검 — 같은 PageSpeed 호출에 category=seo 를 더하면 온다(추가 비용 0).
# 왜 여기 있나: 데이터센터 IP·파이썬 클라이언트를 막는 사이트(gucci.com)는 페이지 점검
# (collect_page)도 크롤도 한 장도 못 본다. PageSpeed 는 구글 인프라가 여는 것이라 통과한다 —
# 그 사이트에서 우리가 얻을 수 있는 유일한 "페이지 안" 사실이다.
# {Lighthouse audit id: (화면 이름, 실패일 때 고칠 것)} — 이름표의 정본은 여기 한 벌이다
# (화면은 d.seo_audits 로 받는다). 여기 없는 id 는 적지 않는다(구글이 항목을 늘려도 모르는
# 영어 이름이 화면에 새지 않게).
SEO_AUDITS = {
    "http-status-code":  ("응답 상태", "검색 로봇이 받는 응답이 200 이 아닙니다."),
    "is-crawlable":      ("색인 허용", "noindex 나 robots 규칙이 이 페이지의 색인을 막습니다."),
    "robots-txt":        ("robots.txt", "robots.txt 에 문법 오류가 있습니다."),
    "document-title":    ("제목 태그", "<title> 이 없습니다."),
    "meta-description":  ("메타 설명", "meta description 이 없습니다."),
    "canonical":         ("canonical", "rel=canonical 이 없거나 잘못된 주소를 가리킵니다."),
    "hreflang":          ("hreflang", "언어·지역 대체 주소 표기가 잘못됐습니다."),
    "image-alt":         ("이미지 alt", "alt 없는 이미지가 있습니다."),
    "link-text":         ("링크 텍스트", "'더보기'·'여기'처럼 뜻 없는 링크 글자가 있습니다."),
    "crawlable-anchors": ("크롤 가능한 링크", "href 없이 스크립트로만 움직이는 링크가 있습니다."),
}


def parse_seo(lh: dict) -> tuple:
    """Lighthouse 결과 → (seo_score 0~100 | None, seo_json | None). 실패 항목만 근거를 싣는다."""
    cat = (lh.get("categories") or {}).get("seo") or {}
    score = cat.get("score")
    if not isinstance(score, (int, float)):
        return None, None
    audits = lh.get("audits") or {}
    ok, fail = [], []
    for aid in SEO_AUDITS:
        a = audits.get(aid) or {}
        s = a.get("score")
        if a.get("scoreDisplayMode") not in ("binary", "numeric") or s is None:
            continue                     # 해당 없음·수동 — 통과도 실패도 아니다
        if s >= 1:
            ok.append(aid)
            continue
        items = ((a.get("details") or {}).get("items") or [])
        ev = []
        for it in items[:3]:
            node = it.get("node") or {}
            v = node.get("snippet") or it.get("text") or it.get("href") or it.get("source") or ""
            if isinstance(v, dict):
                v = v.get("url") or ""
            if v:
                ev.append(str(v)[:120])
        fail.append({"id": aid, "n": len(items), "items": ev})
    return round(score * 100), json.dumps({"ok": ok, "fail": fail}, ensure_ascii=False)


# 이미지 용량·포맷 — 같은 응답의 performance 카테고리에 이미 있다(새 호출 0). 속도 진단은
# "LCP 가 늦다"까지만 말해서, 늦게 뜨는 그 큰 것이 어느 이미지이고 몇 KB 를 덜 수 있는지는
# 요청문이 짐작해야 했다. {Lighthouse audit id: (이름표, 고칠 것)} — 이름표·고칠 것의 정본은
# 여기 한 벌이다(진단 scoring.image_weight_advice 가 이것을 읽는다). Lighthouse 13 이
# 압축·포맷·크기 맞춤·움직이는 그림 넷을 image-delivery-insight 하나로 합쳤고(그 진단의
# replacesAudits) offscreen-images 는 없앴다 — 구글이 PSI 의 Lighthouse 를 올리는 날 칸이
# 조용히 비지 않게, 그리고 12 로 잰 페이지와 13 으로 잰 페이지가 같은 범위를 보게 옛 id 와
# 새 id 를 다 받는다.
IMAGE_AUDITS = {
    "image-delivery-insight": ("이미지 전달", "이미지를 압축하고, WebP·AVIF 로 바꾸고, 화면에 "
                               "그려지는 크기에 맞춰 내보내세요(srcset·sizes). 큰 GIF 는 "
                               "영상(MP4·WebM)으로 바꿉니다."),
    "uses-optimized-images":  ("이미지 압축", "같은 화질로 더 작게 다시 저장하세요(JPEG 품질 "
                               "80 안팎 · 메타데이터 제거)."),
    "modern-image-formats":   ("차세대 포맷", "JPEG·PNG 를 WebP·AVIF 로 바꾸세요. 옛 브라우저용 "
                               "원본은 <picture> 안에 남겨 둡니다."),
    "uses-responsive-images": ("크기 맞춤", "화면에 그려지는 크기보다 큰 원본을 받고 있습니다. "
                               "srcset·sizes 로 기기 폭에 맞는 크기를 내보내세요."),
    "efficient-animated-content": ("움직이는 GIF", "큰 GIF 는 영상(MP4·WebM)으로 바꿔 "
                                   "<video autoplay muted loop playsinline> 로 내보내세요."),
    "offscreen-images":       ("화면 밖 이미지", "첫 화면 밖 이미지는 loading=\"lazy\" 로 미루세요 "
                               "— 첫 화면의 큰 이미지에는 걸지 않습니다."),
}
# 새 판이 합친 진단과 그것이 덮는 옛 진단. 둘이 한 응답에 같이 오면(전환기의 판) 합친 쪽만
# 적는다 — 같은 그림을 압축·포맷·크기로 여러 번 세면 줄일 양이 부푼다.
IMAGE_INSIGHT = "image-delivery-insight"
IMAGE_INSIGHT_COVERS = ("uses-optimized-images", "modern-image-formats", "uses-responsive-images",
                        "efficient-animated-content")
_IMG_SKIP_MODES = ("notApplicable", "manual", "error")


def _detail_items(details: dict) -> list:
    """표(table·opportunity)의 줄. 목록(list)으로 싸여 오면 안의 표들을 편다."""
    if (details or {}).get("type") == "list":
        return [it for sub in details.get("items") or [] for it in _detail_items(sub)]
    return [it for it in (details or {}).get("items") or [] if isinstance(it, dict)]


def parse_images(lh: dict) -> str | None:
    """Lighthouse 결과 → img_json | None.

    {"save": [{id, kb, n, items}]} — kb 는 줄일 수 있는 양(KB), n 은 대상 이미지 수, items 는
    줄일 양이 큰 순으로 주소 상위 3개. 줄일 양이 큰 진단부터. 판정(얼마부터 말할지)은 여기서
    안 한다 — scoring.IMAGE_SAVINGS_MIN_KB. 이미지 진단이 하나도 없는 응답은 None(안 봤다)이고,
    봤는데 줄일 것이 없으면 {"save": []} 다. 둘을 뭉치면 "안 쟀다"가 "가볍다"로 읽힌다.
    """
    audits = (lh or {}).get("audits") or {}
    seen = [aid for aid in IMAGE_AUDITS if aid in audits]
    if not seen:
        return None
    save = []
    for aid in seen:
        a = audits.get(aid) or {}
        if a.get("scoreDisplayMode") in _IMG_SKIP_MODES:
            continue                     # 해당 없음 — 줄일 것도 없다
        details = a.get("details") or {}
        items = sorted((it for it in _detail_items(details)
                        if isinstance(it.get("wastedBytes"), (int, float)) and it["wastedBytes"] > 0),
                       key=lambda it: -it["wastedBytes"])
        total = details.get("overallSavingsBytes")
        if not isinstance(total, (int, float)):
            dbg = details.get("debugData") or a.get("debugData") or {}
            total = dbg.get("wastedBytes")
        if not isinstance(total, (int, float)):
            total = sum(it["wastedBytes"] for it in items)
        kb = round(total / 1024)
        if kb < 1:
            continue
        save.append({"id": aid, "kb": kb, "n": len(items),
                     "items": [str(it.get("url") or "")[:160] for it in items[:3] if it.get("url")]})
    if any(x["id"] == IMAGE_INSIGHT for x in save):
        save = [x for x in save if x["id"] not in IMAGE_INSIGHT_COVERS]
    save.sort(key=lambda x: -x["kb"])
    return json.dumps({"save": save}, ensure_ascii=False)


# ── 진단 더 받기 — 같은 응답의 performance 카테고리에 이미 있다(새 호출 0) ──────────────
# 속도 진단은 "LCP 가 늦다"까지만 말했다. 늦게 뜨는 그것이 **무엇**이고(LCP 요소), 무엇이 그
# 앞을 막는지(렌더 차단·지연 로드), 무엇을 줄이면 얼마나 빨라지는지(줄일 시간 순)가 여기 있다.
# {Lighthouse audit id: (이름표, 고칠 것)} — 이름표·고칠 것의 정본은 여기 한 벌이다(판정은
# scoring.vitals_diag, 화면은 d.diag_audits 로 받는다).
# Lighthouse 13 이 여러 진단을 *-insight 로 갈아 끼웠다(13.0.0 "remove audits replaced by
# performance insights" — https://github.com/GoogleChrome/lighthouse/blob/main/changelog.md).
# 12 로 잰 행과 13 으로 잰 행이 같은 이름으로 남게 새 id 는 DIAG_ALIASES 로 옛 id 에 접는다.
DIAG_AUDITS = {
    "lcp-lazy-loaded":           ("LCP 이미지 지연 로드", "가장 큰 이미지에 loading=\"lazy\" 가 걸려 "
                                  "늦게 받습니다. 그 이미지에서는 lazy 를 빼고 fetchpriority=\"high\" "
                                  "를 주세요."),
    "render-blocking-resources": ("렌더 차단 자원", "첫 화면을 막는 CSS·JS 입니다. 첫 화면에 쓰는 "
                                  "CSS 만 인라인하고 나머지는 뒤로 미루세요(defer·async·media)."),
    "unsized-images":            ("크기 없는 이미지", "width·height(또는 aspect-ratio)가 없어 그려지며 "
                                  "화면을 밉니다. 값을 지정해 자리를 미리 잡으세요."),
    "third-party-summary":       ("서드파티 스크립트", "남의 스크립트(태그·위젯·광고)가 메인 스레드를 "
                                  "잡습니다. 안 쓰는 것은 빼고, 쓰는 것은 상호작용 뒤에 불러오세요."),
    "unused-javascript":         ("안 쓰는 JS", "받기만 하고 실행하지 않는 코드가 큽니다. 페이지별로 "
                                  "쪼개 필요한 것만 보내세요(코드 분할)."),
    "unused-css-rules":          ("안 쓰는 CSS", "이 페이지에서 안 쓰는 규칙이 큽니다. 공용 CSS 를 "
                                  "쪼개거나 안 쓰는 규칙을 걷어 내세요."),
    "font-display":              ("글꼴 표시", "웹 글꼴을 받는 동안 글자가 안 보입니다. "
                                  "font-display: swap(또는 optional)을 주세요."),
    "long-tasks":                ("긴 작업", "메인 스레드를 50ms 넘게 잡는 작업입니다. 그 스크립트를 "
                                  "쪼개거나 뒤로 미루세요."),
    "dom-size":                  ("DOM 크기", "요소가 너무 많아 그리기·반응이 느립니다. 화면 밖 목록은 "
                                  "나눠 그리고 겹겹이 싼 래퍼를 줄이세요."),
}
DIAG_ALIASES = {"render-blocking-insight": "render-blocking-resources",
                "third-parties-insight": "third-party-summary",
                "font-display-insight": "font-display",
                "dom-size-insight": "dom-size"}
# LCP 요소·구간 — 12 는 largest-contentful-paint-element, 13 은 lcp-breakdown-insight.
_LCP_ELEMENT_AUDITS = ("lcp-breakdown-insight", "largest-contentful-paint-element")
# 13 의 구간 이름(subpart) → 우리 열쇠. 12 는 이름이 번역 문자열이라 순서로 읽는다(늘 이 순서).
LCP_PHASES = {"timeToFirstByte": "ttfb", "resourceLoadDelay": "load_delay",
              "resourceLoadDuration": "load", "elementRenderDelay": "render_delay"}
# LCP 가 어떻게 발견되나 — 13 의 lcp-discovery-insight 체크리스트. eagerlyLoaded=false 가
# 12 의 lcp-lazy-loaded 실패와 같은 사실이다.
_LCP_DISCOVERY = "lcp-discovery-insight"
_SAVING_KEYS = ("LCP", "FCP", "TBT", "INP")       # metricSavings 중 ms 단위인 것(CLS 는 단위가 다르다)


def _walk(obj):
    """details 안의 dict 를 깊이 우선으로 — 12·13 이 같은 것을 다른 깊이에 둔다."""
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)


def _item_name(it: dict) -> str:
    """진단 줄 하나의 이름 — 주소, 없으면 서드파티 이름, 없으면 요소 토막."""
    ent = it.get("entity")
    if isinstance(ent, dict):
        ent = ent.get("text")
    node = it.get("node") if isinstance(it.get("node"), dict) else {}
    v = it.get("url") or ent or node.get("snippet") or ""
    return str(v)[:160]


def parse_lcp(lh: dict) -> dict | None:
    """LCP 요소와 그 앞 구간 → {"snippet", "selector", "phases": {ttfb,load_delay,load,render_delay},
    "lazy", "discoverable", "priority"} | None(응답에 그 진단이 없다). 모르는 칸은 뺀다."""
    audits = (lh or {}).get("audits") or {}
    out: dict = {}
    for aid in _LCP_ELEMENT_AUDITS:
        det = (audits.get(aid) or {}).get("details")
        if not det:
            continue
        phases, legacy = {}, []
        for d in _walk(det):
            if "snippet" in d and "snippet" not in out:
                out["snippet"] = str(d.get("snippet") or "")[:200]
                if d.get("selector"):
                    out["selector"] = str(d["selector"])[:160]
            if d.get("subpart") in LCP_PHASES and isinstance(d.get("duration"), (int, float)):
                phases[LCP_PHASES[d["subpart"]]] = round(d["duration"])
            elif "phase" in d and isinstance(d.get("timing"), (int, float)):
                legacy.append(round(d["timing"]))
        if not phases and len(legacy) == len(LCP_PHASES):
            phases = dict(zip(LCP_PHASES.values(), legacy))
        if phases:
            out["phases"] = phases
        if out:
            break
    lazy = audits.get("lcp-lazy-loaded") or {}
    if lazy.get("scoreDisplayMode") == "binary" and isinstance(lazy.get("score"), (int, float)):
        out["lazy"] = lazy["score"] < 1
    for d in _walk((audits.get(_LCP_DISCOVERY) or {}).get("details")):
        for key, name, flip in (("eagerlyLoaded", "lazy", True),
                                ("requestDiscoverable", "discoverable", False),
                                ("priorityHinted", "priority", False)):
            v = d.get(key)
            if isinstance(v, dict) and isinstance(v.get("value"), bool):
                out[name] = (not v["value"]) if flip else v["value"]
    return out or None


def parse_diag(lh: dict) -> str | None:
    """Lighthouse 결과 → diag_json | None.

    {"lcp": parse_lcp, "items": [{id, fail, ms, kb, n, val, items}]} — id 는 DIAG_AUDITS 의
    열쇠(13 의 *-insight 는 옛 id 로 접는다), fail 은 Lighthouse 가 통과 못 했다고 한 것,
    ms 는 줄일 수 있는 시간(metricSavings 의 LCP·FCP·TBT·INP 중 큰 값, 없으면
    overallSavingsMs), kb 는 줄일 수 있는 양, n 은 줄 수, val 은 진단의 대표 숫자(DOM 요소 수·
    긴 작업 중 가장 긴 것 ms), items 는 이름 상위 3개. **줄일 시간이 큰 순**(같으면 kb 순).
    판정(무엇을 말할지)은 여기서 안 한다 — scoring.vitals_diag. 진단이 하나도 없는 응답은
    None(안 봤다), 봤는데 걸린 것이 없으면 {"items": []}.
    """
    audits = (lh or {}).get("audits") or {}
    ids = {**{a: a for a in DIAG_AUDITS}, **DIAG_ALIASES}
    seen = [aid for aid in ids if aid in audits]
    lcp = parse_lcp(lh)
    if not seen and not lcp:
        return None
    by: dict[str, dict] = {}
    for aid in seen:
        a = audits.get(aid) or {}
        if a.get("scoreDisplayMode") in _IMG_SKIP_MODES:
            continue
        details = a.get("details") or {}
        rows = _detail_items(details)
        ms = max((v for k, v in (a.get("metricSavings") or {}).items()
                  if k in _SAVING_KEYS and isinstance(v, (int, float))), default=None)
        if ms is None and isinstance(details.get("overallSavingsMs"), (int, float)):
            ms = details["overallSavingsMs"]
        kb = details.get("overallSavingsBytes")
        if not isinstance(kb, (int, float)):
            kb = ((details.get("debugData") or {}).get("wastedBytes"))
        if not isinstance(kb, (int, float)):
            kb = sum(it["wastedBytes"] for it in rows
                     if isinstance(it.get("wastedBytes"), (int, float)))
        score = a.get("score")
        fail = isinstance(score, (int, float)) and score < 1
        key = lambda it: -max((it.get(k) for k in ("wastedMs", "wastedBytes", "blockingTime",
                                                   "mainThreadTime", "duration")
                               if isinstance(it.get(k), (int, float))), default=0)
        rows = sorted(rows, key=key)
        val = a.get("numericValue") if ids[aid] == "dom-size" else None
        if ids[aid] == "long-tasks":
            val = max((it["duration"] for it in rows
                       if isinstance(it.get("duration"), (int, float))), default=None)
        row = {"id": ids[aid], "fail": fail, "ms": round(ms) if ms else 0,
               "kb": round(kb / 1024) if kb else 0, "n": len(rows),
               "val": round(val) if isinstance(val, (int, float)) else None,
               "items": [n for it in rows[:3] if (n := _item_name(it))]}
        if not (fail or row["ms"] or row["kb"] or row["n"]):
            continue                     # 통과했고 줄일 것도 없다 — 적지 않는다
        # 12·13 이 한 응답에 같이 오면(전환기) 새 진단(*-insight)을 남긴다
        if ids[aid] not in by or aid in DIAG_ALIASES:
            by[ids[aid]] = row
    if lcp and lcp.get("lazy") and "lcp-lazy-loaded" not in by:
        # 13 에는 lcp-lazy-loaded 진단이 없다 — 체크리스트에서 읽은 사실을 같은 이름으로 적는다
        by["lcp-lazy-loaded"] = {"id": "lcp-lazy-loaded", "fail": True, "ms": 0, "kb": 0, "n": 1,
                                 "val": None, "items": [lcp["snippet"]] if lcp.get("snippet") else []}
    items = sorted(by.values(), key=lambda x: (-x["ms"], -x["kb"], x["id"]))
    return json.dumps({**({"lcp": lcp} if lcp else {}), "items": items}, ensure_ascii=False)


# ── 접근성·권장사항 — 같은 호출에 category 둘을 더하면 온다(추가 비용 0) ──────────────
# 실패 항목의 이름표 {Lighthouse audit id: 이름}. 자주 걸리는 것만 둔다 — 여기 없는 id 는
# 화면이 id 그대로(식별자) 보인다. 점수는 Lighthouse 가 낸 값이고 우리가 다시 매기지 않는다.
CAT_AUDITS = {
    "color-contrast": "글자와 바탕의 대비가 낮음", "image-alt": "alt 없는 이미지",
    "link-name": "이름 없는 링크", "button-name": "이름 없는 버튼",
    "label": "레이블 없는 입력 칸", "html-has-lang": "<html lang> 없음",
    "html-lang-valid": "<html lang> 값이 틀림", "meta-viewport": "확대를 막는 viewport",
    "heading-order": "제목 단계를 건너뜀", "document-title": "<title> 없음",
    "target-size": "누를 자리가 너무 작음", "aria-allowed-attr": "쓸 수 없는 ARIA 속성",
    "aria-required-attr": "빠진 필수 ARIA 속성", "aria-valid-attr-value": "ARIA 속성 값이 틀림",
    "aria-hidden-focus": "숨긴 영역 안에 초점이 감", "duplicate-id-aria": "ARIA 가 가리키는 id 중복",
    "list": "목록 구조가 틀림", "listitem": "목록 밖의 <li>", "tabindex": "0 보다 큰 tabindex",
    "frame-title": "제목 없는 iframe", "select-name": "이름 없는 선택 상자",
    "landmark-one-main": "main 영역 없음", "skip-link": "건너뛰기 링크가 안 감",
    "is-on-https": "HTTPS 가 아님", "errors-in-console": "콘솔에 오류가 찍힘",
    "deprecations": "없어질 API 를 씀", "third-party-cookies": "서드파티 쿠키를 씀",
    "image-aspect-ratio": "이미지 비율이 찌그러짐", "image-size-responsive": "이미지 해상도가 낮음",
    "inspector-issues": "브라우저 Issues 패널에 문제", "valid-source-maps": "소스맵이 틀림",
    "csp-xss": "CSP 가 XSS 를 못 막음", "has-hsts": "HSTS 없음",
    "redirects-http": "HTTP 를 HTTPS 로 안 넘김", "charset": "문자 인코딩 선언 없음",
    "doctype": "doctype 없음", "geolocation-on-start": "열자마자 위치 권한을 물음",
    "notification-on-start": "열자마자 알림 권한을 물음", "paste-preventing-inputs": "붙여 넣기를 막는 입력 칸",
}
CATEGORIES = {"accessibility": "접근성", "best-practices": "권장사항"}
# PSI 의 Agentic Browsing 카테고리(Lighthouse 13.3 부터, PSI API 는 13.4.1 부터) — 점수가 아니라
# "점검 N개 중 X개 통과"로 읽는다("does not have a weighted average score from 0 to 100" —
# https://developer.chrome.com/docs/lighthouse/agentic-browsing/scoring). 구글이 실험이라 적은
# 제안 표준 기준이라 **참고 칸**으로만 둔다 — 기회·판정을 만들지 않는다.
AGENTIC = "agentic-browsing"
_REF_SKIP_MODES = ("notApplicable", "informative", "manual", "error")


def _cat_refs(lh: dict, cat: str) -> list[tuple[str, dict]]:
    """그 카테고리에 속한 진단 중 통과·실패를 말할 수 있는 것 — (id, audit)."""
    refs = ((lh.get("categories") or {}).get(cat) or {}).get("auditRefs") or []
    audits = lh.get("audits") or {}
    out = []
    for ref in refs:
        a = audits.get((ref or {}).get("id")) or {}
        if a.get("scoreDisplayMode") in _REF_SKIP_MODES \
                or not isinstance(a.get("score"), (int, float)):
            continue
        out.append((ref["id"], a))
    return out


def parse_cats(lh: dict) -> str | None:
    """Lighthouse 결과 → cat_json | None. {카테고리: {"score": 0~100, "fail": [{id, n}]}} —
    응답에 온 카테고리만 싣는다(옛 호출에는 없다 — None 은 '안 봤다')."""
    out = {}
    for cat in CATEGORIES:
        score = ((lh.get("categories") or {}).get(cat) or {}).get("score")
        if not isinstance(score, (int, float)):
            continue
        fail = [{"id": aid, "n": len(_detail_items(a.get("details") or {}))}
                for aid, a in _cat_refs(lh, cat) if a["score"] < 1]
        out[cat] = {"score": round(score * 100), "fail": fail}
    return json.dumps(out, ensure_ascii=False) if out else None


def parse_agentic(lh: dict) -> str | None:
    """Lighthouse 결과 → agentic_json | None. {"pass": X, "total": N, "fail": [id]} — 해당 없음·
    참고용 진단은 N 에서 뺀다(Lighthouse 가 분수를 세는 방식 그대로)."""
    if AGENTIC not in ((lh or {}).get("categories") or {}):
        return None
    refs = _cat_refs(lh, AGENTIC)
    fail = [aid for aid, a in refs if a["score"] < 1]
    return json.dumps({"pass": len(refs) - len(fail), "total": len(refs), "fail": fail},
                      ensure_ascii=False)


SHOT_MAX = 200_000        # 썸네일(data: 주소) 글자 상한 — 넘으면 안 남긴다(보통 수십 KB 다)


def parse_shot(lh: dict) -> str | None:
    """'구글이 연 모습' — final-screenshot 의 data: 주소. page_vitals 칸이 아니라
    page_vital_shots 로 간다(주소·기기마다 최신 한 장)."""
    data = (((lh or {}).get("audits") or {}).get("final-screenshot") or {}).get("details") or {}
    data = data.get("data")
    if isinstance(data, str) and data.startswith("data:image/") and len(data) <= SHOT_MAX:
        return data
    return None


# ── CrUX History — 주마다 한 점, 지난 25주(기본)의 현장 p75 ──────────────────────────
# https://developer.chrome.com/docs/crux/history-api · PageSpeed 와 다른 서비스
# (chromeuxreport.googleapis.com)라 키의 Cloud 프로젝트에 'Chrome UX Report API' 가 켜져
# 있어야 한다. 키가 없으면 못 부른다(무키 호출이 없다) — 그때는 건너뛰고 사유를 notes 에.
CRUX_API = "https://chromeuxreport.googleapis.com/v1/records:queryHistoryRecord"
CRUX_METRICS = {"lcp_ms": "largest_contentful_paint", "inp_ms": "interaction_to_next_paint",
                "cls": "cumulative_layout_shift", "ttfb_ms": "experimental_time_to_first_byte"}
CRUX_FORM = {"mobile": "PHONE", "desktop": "DESKTOP"}
CRUX_NO_DATA = 404        # 표본이 모자란 주소 — 실패가 아니라 그 주소의 사실이다


def crux_key() -> str | None:
    """CrUX 를 부를 키 — CRUX_API_KEY 가 있으면 그것, 없으면 PAGESPEED_API_KEY(같은 Cloud
    프로젝트에 두 API 를 같이 켜 둔 흔한 경우)."""
    return os.environ.get("CRUX_API_KEY") or os.environ.get("PAGESPEED_API_KEY") or None


def parse_crux_history(data: dict) -> list[tuple]:
    """queryHistoryRecord 응답 → [(metric, period_end, p75)] (metric 은 CRUX_METRICS 의 열쇠).

    p75 는 표본이 모자란 주에 null 로 온다 — None 으로 남긴다(0 이 아니다). CLS 는 문자열로
    온다("CLS uses strings, even if they look like numbers")."""
    rec = (data or {}).get("record") or {}
    ends = []
    for cp in rec.get("collectionPeriods") or []:
        d = (cp or {}).get("lastDate") or {}
        try:
            ends.append(f"{int(d['year']):04d}-{int(d['month']):02d}-{int(d['day']):02d}")
        except (KeyError, TypeError, ValueError):
            ends.append(None)
    out = []
    for col, name in CRUX_METRICS.items():
        p75s = (((rec.get("metrics") or {}).get(name) or {}).get("percentilesTimeseries")
                or {}).get("p75s") or []
        for end, v in zip(ends, p75s):
            if not end:
                continue
            try:
                v = None if v is None else float(v)
            except (TypeError, ValueError):
                v = None
            if v is not None and v != v:          # "NaN"
                v = None
            out.append((col, end, v))
    return out


def fetch_crux_history(target: str, form_factor: str, key: str, *, origin: bool = False,
                       timeout: int | None = None) -> dict:
    """주소(또는 오리진) 하나·기기 하나의 25주. {"rows": [...]} | {"status": n, "error": 사유}."""
    import requests
    body = {("origin" if origin else "url"): target, "formFactor": form_factor,
            "metrics": list(CRUX_METRICS.values())}
    try:
        r = requests.post(CRUX_API, params={"key": key}, json=body,
                          timeout=timeout or serp_adapter.TIMEOUTS["psi"])
    except Exception as e:
        return {"status": None, "error": f"{type(e).__name__}: {e}"[:160]}
    if r.status_code != 200:
        detail = ""
        try:
            detail = ((r.json().get("error") or {}).get("message") or "")[:100]
        except Exception:
            pass
        return {"status": r.status_code,
                "error": f"HTTP {r.status_code}" + (f" · {detail}" if detail else "")}
    try:
        return {"rows": parse_crux_history(r.json())}
    except ValueError as e:
        return {"status": None, "error": f"응답을 못 읽었습니다: {e}"[:160]}


def collect_crux_history(conn, project_id: int, rows: list[dict], want: list[str],
                         *, throttle: float = 0.0) -> tuple[int, str]:
    """이번에 잰 페이지들의 CrUX History 를 적재한다 → (성공한 호출 수, notes 조각).

    현장 값이 **그 페이지 것**인 주소만 주소로 묻는다 — 사이트 전체 값으로 대신 받은 주소
    (origin_fallback)는 물어도 404 다. 그런 주소가 하나라도 있으면 오리진을 한 번 묻는다.
    부수 조회다: 실패해도 속도 행은 이미 저장됐고 단계는 안 죽는다. 권한·한도(403·429·400)는
    남은 호출도 똑같이 죽으므로 거기서 멈추고 사유를 남긴다.
    """
    import time
    from urllib.parse import urlsplit
    key = crux_key()
    if not key:
        return 0, "crux=skip:no_key "
    jobs, seen = [], set()
    for r in rows:
        if r.get("error") or r.get("strategy") not in CRUX_FORM or r["strategy"] not in want:
            continue
        has_field = any(r.get(c) is not None for c in _FIELD) or r.get("field_cls") is not None
        u = urlsplit(r["url"])
        org = f"{u.scheme}://{u.netloc}"
        job = ((r["url"], False) if has_field and not r.get("origin_fallback") else (org, True))
        if (job, r["strategy"]) not in seen:
            seen.add((job, r["strategy"]))
            jobs.append((job[0], job[1], CRUX_FORM[r["strategy"]]))
    calls = saved = nodata = 0
    stop = ""
    for i, (target, origin, form) in enumerate(jobs):
        got = fetch_crux_history(target, form, key, origin=origin)
        if "rows" in got:
            calls += 1
            saved += db.write_crux_history(
                conn, project_id, [(target, int(origin), form, m, end, v)
                                   for m, end, v in got["rows"]])
        elif got.get("status") == CRUX_NO_DATA:
            calls += 1
            nodata += 1
        else:
            stop = got["error"]
            print(f"  ! CrUX History: {stop} — 추세는 건너뜁니다(속도 행은 저장됐습니다)",
                  file=sys.stderr)
            break
        if throttle and i < len(jobs) - 1:
            time.sleep(throttle)
    note = (f"crux=skip:{stop.replace(' ', '_')[:60]} " if stop and not calls
            else f"crux={saved}rows/{calls}calls" + (f"/nodata={nodata}" if nodata else "")
            + (f"/stop:{stop.replace(' ', '_')[:40]}" if stop else "") + " ")
    return calls, note


QUOTA_STATUS = 429
QUOTA_REASON = ("PageSpeed 오늘 한도를 다 썼습니다. 내일 다시 "
                "(PAGESPEED_API_KEY 를 넣으면 한도가 커집니다)")


# 측정 불가 — PageSpeed 는 응답했는데 Lighthouse 가 페이지를 못 그렸다고 답한 것
# ("Lighthouse returned error: NO_FCP"). 우리 수집의 고장도, 그 페이지가 느리다는 뜻도
# 아니다. noti 는 매주 10회 중 9회가 이것이었고, 항목 실패로 세어져 런이 매주 errors=9 로
# 남았다. 코드의 뜻(Lighthouse 문서 기준):
#   NO_FCP / NO_LCP      측정 시간 안에 화면에 아무것도 안 그려졌다 — 첫 화면이 자바스크립트
#                        뒤에야 나타나거나(투명도 0 에서 시작하는 등장 효과·로딩 화면),
#                        측정기(구글 데이터센터의 헤드리스 크롬)를 막거나 빈 화면을 줄 때
#   PAGE_HUNG            페이지가 멈춰 측정을 끝내지 못했다
#   NO_SPEEDLINE_FRAMES  그려진 프레임을 하나도 못 모았다
UNMEASURABLE = ("NO_FCP", "NO_LCP", "PAGE_HUNG", "NO_SPEEDLINE_FRAMES")
# 화면(site.html 의 vitMissWhy)은 page_vitals.error 를 쪼개지 않고 **통째로** 보여 준다 —
# 그래서 이 문장은 사람이 읽을 완성 문장이고, 코드는 맨 끝 괄호 한 곳에만 있다(test_seams 가 대조).
UNMEASURABLE_WHY = ("구글 측정기(Lighthouse)가 이 페이지에서 화면이 그려지는 것을 못 봤습니다. "
                    "첫 화면이 자바스크립트가 끝난 뒤에야 나타나거나(투명도 0 에서 시작하는 등장 "
                    "효과·로딩 화면) 측정기를 막으면 납니다. 사람 눈에는 보여도 구글 렌더러에는 "
                    "빈 화면일 수 있으니 그 자체로 확인할 거리입니다")
# 측정 불가였던 주소는 이 기간 동안 다른 페이지로 바꿔 잰다 — 같은 주소를 매주 다시 재면
# 같은 답(NO_FCP)을 사러 가는 것이다. 기간이 지나면 한 번 다시 본다(고쳤을 수 있다).
UNMEASURABLE_DAYS = 28


def unmeasurable(error: str | None) -> str | None:
    """오류 문장 → 측정 불가 코드(NO_FCP 등) | None. 옛 행의 원문("HTTP 400 · Lighthouse
    returned error: NO_FCP …")과 새 행("…확인할 거리입니다. (측정기 코드 NO_FCP)")을 다 읽는다."""
    for code in UNMEASURABLE:
        if error and code in error:
            return code
    return None


def recent_unmeasurable(conn, project_id: int, days: int = UNMEASURABLE_DAYS) -> set[str]:
    """최근 days 일 안의 마지막 측정에서 **모든 기기가** 측정 불가였던 주소."""
    rows = conn.execute(
        "SELECT v.url, v.error FROM page_vitals v WHERE v.project_id=? AND v.checked_date = ("
        " SELECT MAX(b.checked_date) FROM page_vitals b WHERE b.project_id=v.project_id"
        " AND b.url=v.url) AND julianday('now') - julianday(v.checked_date) < ?",
        (project_id, days)).fetchall()
    by: dict[str, list] = {}
    for r in rows:
        by.setdefault(r["url"], []).append(unmeasurable(r["error"]))
    return {u for u, codes in by.items() if codes and all(codes)}


class _QuotaHit(collector.Fatal):
    """PageSpeed 한도(429) — 남은 URL 도 같은 한도에 걸린다. Stage.each 는 Fatal 만
    삼키지 않고 올리므로 그 통로로 멈추되, collect 가 받아서 **건너뜀**으로 바꾼다
    (Fatal 이 그대로 러너까지 가면 단계 실패가 된다)."""


def parse(url: str, strategy: str, data: dict) -> dict:
    """PSI 응답 한 벌 → page_vitals 한 줄. 네트워크를 안 탄다(자체점검이 여기를 부른다).

    없는 값은 None 이다. 0 으로 채우면 "안 쟀다" 가 "완벽하다" 로 읽힌다.
    """
    row = {"url": url, "strategy": strategy, "error": None, "origin_fallback": None,
           "field_verdict": None, "lab_score": None, "field_cls": None, "lab_cls": None}
    row.update({k: None for k in _FIELD})
    row.update({k: None for k in _LAB})

    exp = data.get("loadingExperience") or {}
    metrics = exp.get("metrics") or {}
    if metrics:
        row["origin_fallback"] = int(bool(exp.get("origin_fallback")))
        row["field_verdict"] = exp.get("overall_category")
        for col, key in _FIELD.items():
            v = (metrics.get(key) or {}).get("percentile")
            row[col] = int(v) if isinstance(v, (int, float)) else None
        cls = (metrics.get("CUMULATIVE_LAYOUT_SHIFT_SCORE") or {}).get("percentile")
        # CLS 만 100 배로 온다 — 그대로 적으면 0.24 인 페이지가 24 로 남는다.
        row["field_cls"] = round(cls / 100, 3) if isinstance(cls, (int, float)) else None

    lh = data.get("lighthouseResult") or {}
    audits = lh.get("audits") or {}
    score = ((lh.get("categories") or {}).get("performance") or {}).get("score")
    row["lab_score"] = round(score * 100) if isinstance(score, (int, float)) else None
    for col, key in _LAB.items():
        v = (audits.get(key) or {}).get("numericValue")
        row[col] = round(v) if isinstance(v, (int, float)) else None
    v = (audits.get("cumulative-layout-shift") or {}).get("numericValue")
    row["lab_cls"] = round(v, 3) if isinstance(v, (int, float)) else None
    row["seo_score"], row["seo_json"] = parse_seo(lh)
    row["img_json"] = parse_images(lh)
    row["diag_json"] = parse_diag(lh)
    row["cat_json"] = parse_cats(lh)
    row["agentic_json"] = parse_agentic(lh)
    v = lh.get("lighthouseVersion")
    row["lh_version"] = str(v)[:20] if v else None
    row["shot"] = parse_shot(lh)          # page_vitals 칸이 아니다 — db.write_page_vital_shots
    return row


# 한 호출에 싣는 카테고리(PSI 는 안 주면 performance 만 돈다). 값은 PSI 의 category 열거
# (PERFORMANCE·SEO·ACCESSIBILITY·BEST_PRACTICES·AGENTIC_BROWSING —
# https://pagespeedonline.googleapis.com/$discovery/rest?version=v5)의 소문자·붙임표 꼴이다.
PSI_CATEGORIES = ("performance", "seo", "accessibility", "best-practices", AGENTIC)
_AGENTIC_OK = [True]      # 이번 실행에서 agentic-browsing 을 실을지 — 400 을 한 번 맞으면 끈다


def _err_detail(r) -> str:
    try:
        return ((r.json().get("error") or {}).get("message") or "")[:120]
    except Exception:
        return ""


def fetch(url: str, strategy: str, *, timeout: int | None = None) -> dict:
    """URL 하나·기기 하나. 실패도 한 줄로 남긴다 — collect_page.fetch 와 같은 규칙이다."""
    import requests
    # category 는 여러 번 싣는다(PSI 는 반복 키로 받는다) — seo 는 같은 호출에 얹혀 공짜다.
    params = [("url", url), ("strategy", strategy),
              *(("category", c) for c in PSI_CATEGORIES if c != AGENTIC or _AGENTIC_OK[0])]
    key = os.environ.get("PAGESPEED_API_KEY")
    if key:
        params.append(("key", key))
    try:
        r = requests.get(API, params=params,
                         timeout=timeout or serp_adapter.TIMEOUTS["psi"])
        if r.status_code == 400 and ("category", AGENTIC) in params \
                and not unmeasurable(_err_detail(r)):
            # agentic-browsing 은 새 카테고리다(PSI 가 13.4.1 에서 켰다). 이 값 때문에 호출이
            # 통째로 거절되면 속도까지 잃는다 — 빼고 한 번 다시 묻는다. 그래서 통과하면 이 값이
            # 원인이었으니 이번 실행에서는 더 안 싣고, 그래도 400 이면 원인이 다른 데 있다.
            params.remove(("category", AGENTIC))
            r2 = requests.get(API, params=params,
                              timeout=timeout or serp_adapter.TIMEOUTS["psi"])
            if r2.status_code != 400:
                _AGENTIC_OK[0] = False
                r = r2
    except Exception as e:
        return {"url": url, "strategy": strategy, "error": f"{type(e).__name__}: {e}"[:200]}
    if r.status_code != 200:
        detail = ""
        try:
            detail = ((r.json().get("error") or {}).get("message") or "")[:120]
        except Exception:
            pass
        code = unmeasurable(detail)
        if code:
            return {"url": url, "strategy": strategy, "status": r.status_code,
                    "unmeasurable": code, "error": f"{UNMEASURABLE_WHY}. (측정기 코드 {code})"}
        return {"url": url, "strategy": strategy, "status": r.status_code,
                "error": f"HTTP {r.status_code}" + (f" · {detail}" if detail else "")}
    try:
        return parse(url, strategy, r.json())
    except ValueError as e:
        return {"url": url, "strategy": strategy, "error": f"응답을 못 읽었습니다: {e}"[:200]}


def collect(project: str, *,
            dry_run: bool = False,
            limit: int | None = None,
            strategy: str | None = None,
            throttle: float | None = None,
            conn=None) -> collector.StageResult:
    """내 페이지의 속도를 재서 Brain 에 적재한다. sys.exit 호출 없음.

    Args:
        project: 사이트 이름
        dry_run: True 면 잴 목록만 찍고 종료
        limit: 잴 URL 수(config 키는 vitals_urls). 0이면 끔. 한 URL 이 기기 수만큼
            호출된다 — 기본 5개 × 2기기 = 10회다.
        strategy: 쉼표로 구분한 기기(mobile,desktop). 기기 격차를 보려면 둘 다 필요하다.
        throttle: 요청 간격(초)
        conn: 이미 열린 Brain 연결 — 주면 그것을 쓰고 닫지 않는다

    Returns:
        StageResult(ok=...). 사유 있는 비종료는 ok=False, skipped=True.
    """
    ap = _parser()
    with collector.stage(project, conn=conn, dry_run=dry_run) as st:
        conn, p = st.conn, st.project
        s = st.settings(ap, argparse.Namespace(limit=limit, strategy=strategy,
                                               throttle=throttle))
        limit = s["vitals_urls"]
        if limit <= 0:
            print("[vitals] vitals_urls=0 — 속도 측정을 끄셨습니다.")
            return st.noop(rows=0)
        want = [x.strip() for x in str(s["strategy"] or "").split(",") if x.strip()]
        bad = [x for x in want if x not in STRATEGIES]
        if bad or not want:
            return st.skip(f"기기 이름이 틀렸습니다: {', '.join(bad) or '(비어 있음)'}. "
                           f"쓸 수 있는 값: {', '.join(STRATEGIES)}")

        # 잴 페이지의 정본은 페이지 감사와 같다 — 두 단계가 서로 다른 페이지를 보면
        # 요청문 안에서 "이 페이지" 가 두 곳을 가리킨다.
        # 측정 불가였던 주소는 빼고 그만큼 다음 페이지로 채운다(UNMEASURABLE_DAYS 동안).
        dead = recent_unmeasurable(conn, p["id"])
        urls = [u for u in collect_page.target_urls(conn, p["id"], limit + len(dead))
                if u not in dead][:limit]
        if dead:
            print(f"[vitals] 최근 측정 불가(NO_FCP 등)였던 {len(dead)}곳은 "
                  f"{UNMEASURABLE_DAYS}일 동안 다른 페이지로 바꿔 잽니다")
        if not urls:
            return st.skip("잴 페이지가 없습니다. 먼저 gsc 를 수집하세요 "
                           "(page 분해가 있어야 어느 URL 인지 알 수 있습니다).")

        jobs = [(u, dev) for u in urls for dev in want]
        print(f"[vitals] URL {len(urls)}개 × {len(want)}기기 = {len(jobs)}회 · 비용 없음 "
              f"(PageSpeed Insights · 간격 {st.throttle}초)")
        if not os.environ.get("PAGESPEED_API_KEY"):
            print("       키 없이 돕니다. 자주 돌려 한도(429)에 걸리면 "
                  "PAGESPEED_API_KEY 를 넣으세요 — 발급은 무료입니다.")
        if st.dry_run:
            for i, (u, dev) in enumerate(jobs, 1):
                print(f"  {i:>3}. [{dev}] {u}")
            return st.noop(rows=0)

        rows: list[dict] = []
        unmeas: list[str] = []

        def measure(job) -> dict:
            # 일꾼 스레드 — 네트워크만. 한도(429)는 여기서 Fatal 로 올려 fanout 이 아직 안 보낸
            # 호출을 멈추게 한다(이미 떠난 최대 workers-1 개만 더 나간다).
            url, dev = job
            row = fetch(url, dev)
            if row.get("status") == QUOTA_STATUS:
                # 이 행은 안 남긴다 — "이 페이지를 못 쟀다" 가 아니라 "오늘은 더 못 잰다" 다
                raise _QuotaHit(row["error"])
            return row

        def one(job, row) -> None:
            # 메인 스레드, 작업 순서대로 — 행이 쌓이는 순서·출력·실패 집계는 순차일 때와 같다.
            url, dev = job
            # 행은 남긴다 — page_vitals.error 는 "못 쟀다"를 적는 진짜 칸이다.
            # 그리고 실패는 예외로 올린다: 여기서 return 하면 실패가 데이터로만 남아
            # 전부 못 재고도 runs.notes 에 errors=0 이 적힌다(collect_page 와 같은 규칙).
            rows.append(row)
            if row.get("unmeasurable"):
                # 페이지 쪽 사실이다 — 행은 남기되(화면이 사유를 말한다) 실패로 안 센다
                unmeas.append(url)
                print(f"  · [{dev}] {url} — 측정 불가({row['unmeasurable']})")
                return
            if row.get("error"):
                raise collector.ItemFailed(row["error"])
            lcp = row.get("field_lcp_ms") or row.get("lab_lcp_ms")
            score = row.get("lab_score")
            print(f"  ✓ [{dev}] {url} - 점수 {score if score is not None else '-'}"
                  + (f" · LCP {lcp}ms" if lcp else ""))

        quota = ""
        with st.record("vitals") as r:
            try:
                done = fanout.each(st, jobs, measure, one, workers=fanout.LIMITS["pagespeed"],
                                   label=lambda j: f"[{j[1]}] {j[0]}")
            except _QuotaHit as e:
                quota = str(e)
                done = sum(1 for x in rows if not x.get("error"))
                print(f"  ! {QUOTA_REASON} ({quota})", file=sys.stderr)
            checked = str(date.today())
            db.write_page_vitals(conn, p["id"], checked, rows)
            db.write_page_vital_shots(conn, p["id"], checked, rows)
            # 부수 조회 — 25주 현장 추세. 한도로 멈춘 날은 안 부른다(같은 키의 한도다).
            crux_calls, crux_note = ((0, "") if quota else collect_crux_history(
                conn, p["id"], rows, want, throttle=st.throttle))
            done += crux_calls
            r.api_calls = done
            measured = sum(1 for x in rows if not x.get("error"))    # 측정 불가는 잰 것이 아니다
            r.notes = (f"urls={len(urls)} strategies={','.join(want)} "
                       f"rows={len(rows)} checked={checked} "
                       + (f"unmeasurable={len(unmeas)} " if unmeas else "")
                       + crux_note
                       + ("" if _AGENTIC_OK[0] else "agentic=unsupported ")
                       + f"{st.err_note}"
                       + (f" | quota={QUOTA_STATUS}: {QUOTA_REASON}" if quota else ""))

        bad_rows = [x for x in rows if x.get("error")]
        print(f"\nsaved {len(rows)} vitals rows (errors={st.errors})"
              + (f" · 못 잰 것 {len(bad_rows)}개" if bad_rows else ""))
        if quota and not done:
            # 한 건도 못 잰 채 한도 — 건너뜀이다. 앞 URL 에 다른 실패가 있었어도 오늘 이
            # 단계가 말할 수 있는 것은 "한도라서 못 쟀다" 하나다.
            return st.skip(QUOTA_REASON)
        if unmeas:
            print(f"  ! 측정 불가 {len(unmeas)}건 — {UNMEASURABLE_WHY}", file=sys.stderr)
        if unmeas and not measured and not st.errors:
            # 전부 측정 불가 — 실패가 아니라 건너뜀이다. 사유는 한 번만 사람 말로.
            return st.skip(f"잰 페이지 {len(set(unmeas))}곳이 전부 측정 불가입니다. {UNMEASURABLE_WHY}.")
        # 실제로 잰 건수로 판정한다 — 전부 못 잰 것은 완료가 아니다. 한도 전까지 잰 것이
        # 있으면 그만큼으로 완료다(한도는 실패로 안 센다 — each 가 세기 전에 멈췄다).
        return st.verdict(measured, rows=len(rows))


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser()
    collector.add_common(ap)
    collector.add_setting(ap, "--limit", key="vitals_urls", fallback=5, type=int,
                          help="속도를 잴 URL 수. 0이면 끔 (한 URL 이 기기 수만큼 호출됩니다)")
    # type=str 을 꼭 준다 — add_setting 의 기본은 int 라, 빼면 'mobile,desktop' 을 int() 로
    # 바꾸려다 속도 단계가 통째로 죽었다(호스팅 noti 런).
    collector.add_setting(ap, "--strategy", key="strategy", fallback="mobile,desktop", type=str,
                          help="기기 — mobile,desktop. 기기 격차를 보려면 둘 다 필요합니다")
    collector.add_setting(ap, "--throttle", key="throttle", fallback=1.0, type=float,
                          help="요청 간격(초)")
    return ap


def main() -> None:
    if len(sys.argv) == 1:
        _selfcheck()
        return
    collector.cli("vitals")


def _selfcheck() -> None:
    import sqlite3

    import scoring
    sample = {
        "loadingExperience": {
            "overall_category": "SLOW", "origin_fallback": True,
            "metrics": {
                "LARGEST_CONTENTFUL_PAINT_MS": {"percentile": 4200},
                "INTERACTION_TO_NEXT_PAINT": {"percentile": 310},
                "CUMULATIVE_LAYOUT_SHIFT_SCORE": {"percentile": 24},
                "EXPERIMENTAL_TIME_TO_FIRST_BYTE": {"percentile": 900}}},
        "lighthouseResult": {
            "categories": {"performance": {"score": 0.42}},
            "audits": {"largest-contentful-paint": {"numericValue": 4310.5},
                       "cumulative-layout-shift": {"numericValue": 0.2412},
                       "total-blocking-time": {"numericValue": 640}}}}
    # 구글이 연 모습의 SEO 점검 — 실패 항목만 근거와 함께, 수동·해당 없음은 빼고, 모르는 id 는 버린다.
    assert parse_seo({}) == (None, None), "SEO 카테고리가 없는 응답(옛 호출)에 점수를 지어낸다"
    lh = {"categories": {"seo": {"score": 0.92}},
          "audits": {"document-title": {"score": 1, "scoreDisplayMode": "binary"},
                     "link-text": {"score": 0, "scoreDisplayMode": "binary", "details": {"items": [
                         {"href": "https://g.kr/a", "text": "더보기"}, {"href": "https://g.kr/b", "text": "여기"}]}},
                     "image-alt": {"score": 0, "scoreDisplayMode": "binary", "details": {"items": [
                         {"node": {"snippet": '<img src="x.jpg">'}}]}},
                     "structured-data": {"score": None, "scoreDisplayMode": "manual"},
                     "new-google-audit": {"score": 0, "scoreDisplayMode": "binary"}}}
    sc, sj = parse_seo(lh)
    got = json.loads(sj)
    assert sc == 92 and got["ok"] == ["document-title"], (sc, got)
    assert [(f["id"], f["n"], f["items"]) for f in got["fail"]] == [
        ("image-alt", 1, ['<img src="x.jpg">']), ("link-text", 2, ["더보기", "여기"])], got["fail"]
    r = parse("https://g.kr/", "mobile", {"lighthouseResult": lh})
    assert r["seo_score"] == 92 and r["seo_json"] == sj, "parse 가 SEO 점검을 행에 안 싣는다"
    # 이미지 진단이 없는 응답은 None(안 봤다) — 갈래별 검사는 test_collectors 가 한다
    assert r["img_json"] is None, r["img_json"]
    # 기기 설정은 문자열이다 — int() 로 바꾸려다 속도 단계가 통째로 죽었다(호스팅 noti 런)
    import argparse
    ap = _parser()
    s = collector.settings(argparse.Namespace(limit=None, strategy=None, throttle=None), {},
                           ap._collector_settings)
    assert s["strategy"] == "mobile,desktop" and s["vitals_urls"] == 5, s

    r = parse("https://x.kr/a", "mobile", sample)
    assert (r["field_lcp_ms"], r["field_inp_ms"], r["field_ttfb_ms"]) == (4200, 310, 900), r
    # CLS 만 100 배로 온다 — 그대로 적으면 0.24 인 페이지가 24 로 남는다
    assert r["field_cls"] == 0.24, r["field_cls"]
    assert r["origin_fallback"] == 1 and r["field_verdict"] == "SLOW", r
    assert (r["lab_score"], r["lab_lcp_ms"], r["lab_cls"], r["lab_tbt_ms"]) \
        == (42, 4310, 0.241, 640), r

    # 현장 값이 없는 페이지(트래픽이 적으면 흔하다) — 0 이 아니라 None 이어야 한다
    thin = parse("https://x.kr/b", "desktop", {"lighthouseResult": sample["lighthouseResult"]})
    assert thin["field_lcp_ms"] is None and thin["field_verdict"] is None, thin
    assert thin["lab_score"] == 42, thin

    # 판정은 scoring 한 곳이다 — 임계값을 여기서 다시 안 센다
    adv = scoring.vitals_advice([r])
    assert adv and adv[0]["tag"] == "속도", adv
    assert "4.2초" in adv[0]["now"], adv[0]["now"]
    fast = parse("https://x.kr/c", "mobile", {"loadingExperience": {
        "overall_category": "FAST", "metrics": {
            "LARGEST_CONTENTFUL_PAINT_MS": {"percentile": 1800},
            "INTERACTION_TO_NEXT_PAINT": {"percentile": 90},
            "CUMULATIVE_LAYOUT_SHIFT_SCORE": {"percentile": 3}}}})
    assert not scoring.vitals_advice([fast]), "기준 안에 드는 페이지에 속도 지적을 만든다"

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    conn.execute("INSERT INTO projects(id,name,type,domain) VALUES(1,'p','saas','x.kr')")
    assert db.write_page_vitals(conn, 1, "2026-09-09", [r, thin]) == 2
    assert db.write_page_vitals(conn, 1, "2026-09-09", [r, thin]) == 2, "같은 날 두 번이 늘어난다"
    got = conn.execute("SELECT COUNT(*) c FROM page_vitals").fetchone()["c"]
    assert got == 2, got
    _quota_and_key_check(sample)
    print("collect_vitals self-check ok")


def _quota_and_key_check(sample: dict) -> None:
    """키를 실어 보내나 · 429(한도)가 실패가 아니라 건너뜀인가 — 가짜 requests.get 으로(네트워크 0)."""
    import contextlib
    import io
    import tempfile

    import requests

    class Resp:
        def __init__(self, status, body):
            self.status_code, self._body = status, body

        def json(self):
            return self._body

    quota_body = {"error": {"code": 429, "message": "Quota exceeded for quota metric "
                            "'Queries' and limit 'Queries per day'"}}
    sent: list[dict] = []
    script: list = []

    def fake_get(url, params=None, timeout=None):
        sent.append(dict(params or {}))
        return script.pop(0) if script else Resp(QUOTA_STATUS, quota_body)

    orig_get, orig_key = requests.get, os.environ.pop("PAGESPEED_API_KEY", None)
    orig_targets = collect_page.target_urls
    os.environ["CAPTURE_HOME"] = str(Path(tempfile.mkdtemp(prefix="seo-miner-vitals-selftest-")))
    boot = db.connect()
    boot.execute("INSERT INTO projects(name, domain, locale) VALUES('vt','vt.kr','ko-KR')")
    boot.commit()
    boot.close()
    requests.get = fake_get
    collect_page.target_urls = lambda c, pid, limit: ["https://vt.kr/a", "https://vt.kr/b"]

    def run():
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return collect("vt", strategy="mobile", throttle=0)

    try:
        # 1. 키 — 환경변수가 있으면 key= 로 싣고, 없으면 안 싣는다(빈 key= 는 400 이다)
        sent.clear()
        fetch("https://vt.kr/a", "mobile")
        assert "key" not in sent[-1], sent[-1]
        os.environ["PAGESPEED_API_KEY"] = "selftest-key"
        fetch("https://vt.kr/a", "mobile")
        assert sent[-1].get("key") == "selftest-key", f"키를 요청에 안 실었다: {sent[-1]}"
        del os.environ["PAGESPEED_API_KEY"]

        # 2. 처음부터 한도 — 실패가 아니라 건너뜀, 사유는 사람 말, 첫 429 에서 멈춘다
        sent.clear()
        res = run()
        assert (res.ok, res.skipped, res.failed) == (True, True, False), res
        assert res.reason == QUOTA_REASON, res.reason
        # 동시에 떠 있던 호출(최대 workers 개)은 이미 나간 뒤라 1회로 못 박을 수 없다
        assert len(sent) <= fanout.LIMITS["pagespeed"], f"한도에 걸린 뒤로도 계속 불렀다: {len(sent)}회"
        conn = db.connect()
        assert conn.execute("SELECT COUNT(*) c FROM page_vitals").fetchone()["c"] == 0, \
            "한도를 '이 페이지를 못 쟀다' 행으로 남겼다"
        notes = conn.execute("SELECT notes FROM runs WHERE kind='vitals' ORDER BY id DESC"
                             ).fetchone()["notes"]
        assert "quota=429" in notes, notes
        conn.close()

        # 3. 한 장 잰 뒤 한도 — 잰 것은 남기고 완료(실패·부분 실패 아님)
        script[:] = [Resp(200, sample)]
        res = run()
        assert (res.ok, res.skipped, res.partial, res.rows) == (True, False, False, 1), res
        conn = db.connect()
        assert conn.execute("SELECT COUNT(*) c FROM page_vitals WHERE error IS NULL"
                            ).fetchone()["c"] == 1
        conn.close()

        # 4. 한도가 아닌 실패(500)는 여전히 실패다 — 429 만 건너뜀으로 바꾼다
        script[:] = [Resp(500, {}), Resp(500, {})]
        res = run()
        assert res.failed, f"500 을 건너뜀으로 삼켰다: {res}"

        # 5. 측정 불가(NO_FCP) — noti 가 매주 10회 중 9회 이것이었다. 실패가 아니라 페이지의
        #    사실이다: 전부 그러면 건너뜀(사유 한 번), 섞이면 잰 것만으로 완료(errors=0).
        nofcp = {"error": {"code": 400, "message": "Lighthouse returned error: NO_FCP. The page "
                           "did not paint any content."}}
        script[:] = [Resp(400, nofcp), Resp(400, nofcp)]
        res = run()
        assert (res.ok, res.skipped, res.failed) == (True, True, False), res
        assert "측정 불가" in res.reason and "NO_FCP" not in res.reason, res.reason
        conn = db.connect()
        errs = [r["error"] for r in conn.execute(
            "SELECT error FROM page_vitals WHERE checked_date=date('now') AND error IS NOT NULL")]
        assert errs and all(e.startswith(UNMEASURABLE_WHY) and e.endswith("(측정기 코드 NO_FCP)")
                                for e in errs), errs
        # 다음 회차 — 측정 불가였던 두 곳은 빼고 다음 페이지를 잰다
        asked: list = []
        collect_page.target_urls = lambda c, pid, limit: (asked.append(limit) or
                                                          ["https://vt.kr/a", "https://vt.kr/b",
                                                           "https://vt.kr/c"])[:limit]
        sent.clear()
        script[:] = [Resp(200, sample)]
        res = run()
        assert [p["url"] for p in sent] == ["https://vt.kr/c"], f"측정 불가 주소를 또 쟀다: {sent}"
        assert res.ok and not res.partial and asked == [7], (res, asked)
        # 섞인 회차 — 잰 것은 완료, 측정 불가는 errors 에 안 들어간다
        conn.execute("DELETE FROM page_vitals")
        conn.commit()
        collect_page.target_urls = lambda c, pid, limit: ["https://vt.kr/a", "https://vt.kr/b"]
        script[:] = [Resp(200, sample), Resp(400, nofcp)]
        res = run()
        assert (res.ok, res.partial, res.failed) == (True, False, False), res
        notes = conn.execute("SELECT notes FROM runs WHERE kind='vitals' ORDER BY id DESC"
                             ).fetchone()["notes"]
        assert "unmeasurable=1" in notes and "errors=0" in notes, notes
        conn.close()
        # 옛 행의 원문도 같은 판정이다(바꾸기 전 저장된 noti 의 행)
        assert unmeasurable("HTTP 400 · Lighthouse returned error: NO_FCP. The page did not") == "NO_FCP"
        assert unmeasurable("HTTP 500") is None
    finally:
        requests.get = orig_get
        collect_page.target_urls = orig_targets
        if orig_key is not None:
            os.environ["PAGESPEED_API_KEY"] = orig_key
        else:
            os.environ.pop("PAGESPEED_API_KEY", None)


if __name__ == "__main__":
    main()
