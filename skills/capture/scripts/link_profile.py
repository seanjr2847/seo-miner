#!/usr/bin/env python3
"""백링크 프로필 읽기 — 받아 둔 표에서 '어떻게 생겼나'를 낸다. 네트워크를 안 탄다.

수집(collect_backlinks)은 줄을 쌓고, 여기는 그 줄을 **결정적 규칙**으로 접는다:

  anchor_mix    앵커 유형(브랜드·주소·정확 일치·일반 문구·빈 앵커·그 밖)
  reciprocal    상호 링크(우리가 내보낸 외부 링크 ∩ 우리에게 링크한 도메인)
  distribution  참조 도메인 분포(TLD·국가·한 도메인 집중도)
  spam          spam score 분포 — 분포만. 판정·disavow 권고는 하지 않는다
  pages         링크가 몰린 페이지와 하나도 없는 페이지
  velocity      달별 새로 생긴·사라진 참조 도메인(링크 속도)

규칙 둘이 전부에 걸린다.

  1. **점수를 만들지 않는다.** 0~100 '백링크 건강 점수' 같은 합성 숫자는 없다 — 가중치를
     지어내야 하기 때문이다. 내는 것은 센 값과 그 비율, 그리고 문턱을 넘었는지뿐이다.
  2. **근거가 모자라면 숫자 대신 '데이터 부족'.** 링크 12개의 앵커 비율, 일부만 읽은 목록의
     합계, 추적 검색어가 없는 사이트의 '정확 일치 0%'는 숫자처럼 보이지만 아무 말도 아니다.
     그때는 enough=False 이고 비율 칸이 **None** 이다(화면이 찍을 숫자가 아예 없다).
     claude-seo 의 validate_backlink_report 가 보고서 파일에 하던 검사를 페이로드에 건 것.

문턱(상수)은 구글이 공개한 숫자가 아니다 — 화면에 표시를 세우는 자리일 뿐이고, 화면은 그
숫자를 페이로드에서 읽어 그대로 말한다(여기와 화면이 두 벌이 되지 않게).

Usage:
  python link_profile.py            # self-check
"""
from __future__ import annotations

import json
import re
import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

INSUFFICIENT = "데이터 부족"

# 줄의 출처(collect_backlinks.SOURCE_DFS·collect_bing·cc_graph.SOURCE)와 그 출처가 주는 것.
# full=False 인 출처의 합계·비율은 표본이다 — 화면이 그렇게 말한다.
SOURCES = {
    "dataforseo": {"label": "DataForSEO", "full": True},
    "bing": {"label": "Bing 웹마스터", "full": False,
             "says": "Bing 웹마스터가 준 인바운드 링크입니다. 도메인 지수·순위 반영 여부·끊긴 링크는 "
                     "Bing 이 주지 않아 비어 있습니다."},
}

# ── 앵커 유형 ────────────────────────────────────────────────────────────────
ANCHOR_TYPES = ("brand", "url", "exact", "generic", "empty", "other")
ANCHOR_LABEL = {"brand": "브랜드", "url": "주소", "exact": "정확 일치", "generic": "일반 문구",
                "empty": "빈 앵커", "other": "그 밖"}
ANCHOR_HINT = {
    "brand": "우리 이름(브랜드 별칭·도메인 이름)이 든 앵커",
    "url": "주소를 그대로 적은 앵커",
    "exact": "추적 검색어와 글자 그대로 같은 앵커",
    "generic": "'여기'·'click here' 같은 뜻 없는 문구",
    "empty": "글자가 없는 링크(주로 이미지)",
    "other": "문장·부분 일치 등 위에 안 드는 앵커",
}
# 쏠림은 **정확 일치 비율로만** 본다. 브랜드·주소 앵커는 몰려도 정상이고, 한 문구가 1위인 것만
# 보면 브랜드명이 늘 걸린다. 이 숫자 넘게 추적 검색어 그대로인 앵커면 표시를 세운다.
ANCHOR_EXACT_WARN = 0.5
# 이보다 적은 링크로는 비율을 말하지 않는다 — 한 개가 5%p 를 움직인다.
MIN_LINKS = 20

# 뜻 없는 문구 — **언어별 표 한 벌**. 앵커의 언어는 사이트 로케일과 무관하므로(영어 글이 한국
# 사이트를 링크한다) 판정은 전 언어의 합집합으로 한다. 접은 꼴(fold)로 적는다.
GENERIC_ANCHORS = {
    "en": ("click here", "here", "read more", "learn more", "more", "this", "link", "website",
           "site", "visit", "visit site", "visit website", "source", "homepage", "home", "more info",
           "details", "view", "go", "this site", "this page", "official site", "official website"),
    "ko": ("여기", "여기를 클릭", "클릭", "바로가기", "자세히 보기", "자세히", "더보기", "더 보기", "링크",
           "홈페이지", "사이트", "웹사이트", "출처", "원문", "원문 보기", "공식 홈페이지", "공식 사이트"),
    "ja": ("こちら", "ここ", "クリック", "詳細", "詳しくはこちら", "続きを読む", "リンク", "サイト",
           "ホームページ", "公式サイト", "出典", "もっと見る"),
    "zh": ("点击这里", "这里", "点击", "查看更多", "更多", "链接", "网站", "官网", "官方网站", "来源",
           "點擊這裡", "這裡", "連結", "網站", "官方網站", "來源"),
    "es": ("haz clic aquí", "clic aquí", "aquí", "leer más", "más información", "más", "enlace",
           "sitio web", "sitio", "web", "fuente", "ver más", "sitio oficial"),
    "de": ("hier klicken", "hier", "mehr lesen", "weiterlesen", "mehr erfahren", "mehr", "link",
           "webseite", "website", "seite", "quelle", "zur website", "offizielle website"),
    "fr": ("cliquez ici", "ici", "lire la suite", "en savoir plus", "plus", "lien", "site web",
           "site", "source", "voir plus", "site officiel"),
    "pt": ("clique aqui", "aqui", "leia mais", "saiba mais", "mais", "link", "site", "website",
           "fonte", "ver mais", "site oficial"),
    "it": ("clicca qui", "qui", "leggi di più", "scopri di più", "link", "sito web", "sito",
           "fonte", "sito ufficiale"),
    "ru": ("нажмите здесь", "здесь", "тут", "читать далее", "подробнее", "ссылка", "сайт",
           "источник", "официальный сайт"),
}

_URLISH = re.compile(r"^(https?://|www\.)\S+$|^[\w-]+(\.[\w-]+)+(/\S*)?$", re.I | re.U)


def fold(s) -> str:
    """비교용 — NFKC·소문자, 앞뒤 구두점을 떼고 공백을 하나로. 'Click Here!' → 'click here'."""
    t = unicodedata.normalize("NFKC", str(s or "")).lower()
    t = re.sub(r"\s+", " ", t).strip()
    return re.sub(r"^[\W_]+|[\W_]+$", "", t, flags=re.U)


_GENERIC = frozenset(fold(w) for words in GENERIC_ANCHORS.values() for w in words)


def _squash(s) -> str:
    """띄어쓰기·기호까지 접는다 — '디아더 피부과' 와 '디아더피부과' 는 같은 이름이다."""
    return re.sub(r"[\W_]+", "", fold(s), flags=re.U)


def classify_anchor(anchor, *, brands=(), domain: str = "", keywords=()) -> str:
    """앵커 하나 → ANCHOR_TYPES 중 하나. 위에서부터 먼저 걸리는 것이 답이다.

    brands 는 브랜드 별칭(scoring.aliases_of), keywords 는 추적 검색어. 둘 다 원문을 넘긴다.
    """
    a = fold(anchor)
    if not a:
        return "empty"
    import scoring
    host = scoring.host_of(domain)
    if _URLISH.match(a) or (host and host in a):
        return "url"
    sq = _squash(a)
    names = {_squash(b) for b in brands} | ({scoring._stem(domain)} if domain else set())
    if any(len(n) >= 2 and n in sq for n in names):
        return "brand"
    if sq in {_squash(k) for k in keywords}:
        return "exact"
    if a in _GENERIC:
        return "generic"
    return "other"


def anchor_mix(rows, *, brands=(), domain: str = "", keywords=()) -> dict:
    """backlink_anchors 줄들 → 유형별 링크 수·비율과 쏠림 표시.

    쏠림(warn)은 정확 일치 비율이 ANCHOR_EXACT_WARN 을 넘을 때만. 링크가 MIN_LINKS 보다
    적거나, 추적 검색어가 없어 정확 일치를 가릴 수 없으면 판단하지 않는다(enough=False,
    비율 None)."""
    kw = [k for k in keywords if _squash(k)]
    by: dict[str, dict] = {t: {"type": t, "label": ANCHOR_LABEL[t], "hint": ANCHOR_HINT[t],
                              "backlinks": 0, "anchors": 0, "top": []} for t in ANCHOR_TYPES}
    for r in rows or []:
        n = int(r.get("backlinks") or 0)
        t = classify_anchor(r.get("anchor"), brands=brands, domain=domain, keywords=kw)
        by[t]["backlinks"] += n
        by[t]["anchors"] += 1
        by[t]["top"].append((n, str(r.get("anchor") or "")))
    total = sum(b["backlinks"] for b in by.values())
    enough = total >= MIN_LINKS
    for b in by.values():
        b["top"] = [a for _, a in sorted(b["top"], key=lambda x: (-x[0], x[1]))[:3] if a]
        b["share"] = round(b["backlinks"] / total, 4) if enough else None
    exact_known = bool(kw)
    exact_share = by["exact"]["share"] if exact_known else None
    why = None
    if not total:
        why = "앵커를 아직 받지 않았습니다"
    elif not enough:
        why = f"앵커가 붙은 링크가 {total}개뿐입니다. {MIN_LINKS}개부터 비율을 말합니다"
    elif not exact_known:
        why = "추적 검색어가 없어 정확 일치 앵커를 가릴 수 없습니다"
    return {"types": [by[t] for t in ANCHOR_TYPES if by[t]["anchors"]], "total": total,
            "enough": enough, "exact_known": exact_known, "exact_share": exact_share,
            "warn": bool(enough and exact_known and exact_share is not None
                         and exact_share > ANCHOR_EXACT_WARN),
            "warn_at": ANCHOR_EXACT_WARN, "min_links": MIN_LINKS, "why": why}


# ── 상호 링크 ────────────────────────────────────────────────────────────────

def reciprocal(conn, pid: int, bl_date: str | None) -> dict:
    """우리가 본문·메뉴에서 내보낸 외부 링크의 도메인 ∩ 우리에게 링크한 도메인.

    서로 링크하는 것 자체는 잘못이 아니다(제휴사·모회사·인용한 출처). 그래서 판정 없이 목록만
    낸다. crawled=False 면 크롤 전이라 '모른다'지 '없다'가 아니다."""
    import scoring
    run = conn.execute("SELECT MAX(id) FROM crawl_runs WHERE project_id=? AND finished_at IS NOT NULL",
                       (pid,)).fetchone()[0]
    if not run or not bl_date:
        return {"crawled": bool(run), "rows": []}
    out: dict[str, int] = {}
    for r in conn.execute("SELECT url_to FROM crawl_links WHERE run_id=? AND is_internal=0", (run,)):
        h = scoring.host_of(r["url_to"])
        if h:
            out[h] = out.get(h, 0) + 1
    rows = []
    for r in conn.execute("SELECT domain, rank, backlinks FROM referring_domains"
                          " WHERE project_id=? AND checked_date=? AND lost_date IS NULL", (pid, bl_date)):
        h = scoring.host_of(r["domain"])
        if h in out:
            rows.append({"domain": r["domain"], "rank": r["rank"], "backlinks": r["backlinks"],
                         "out_links": out[h]})
    rows.sort(key=lambda r: (-(r["backlinks"] or 0), r["domain"]))
    return {"crawled": True, "rows": rows}


# ── 참조 도메인 분포 ─────────────────────────────────────────────────────────
# 한 도메인이 전체 링크에서 차지하는 몫의 눈금. 넘은 눈금 중 가장 큰 것을 말한다.
CONCENTRATION_TIERS = (0.05, 0.10, 0.25)
# DataForSEO 가 응답에 싣는 나라 칸의 특수 값(문서 표본) — 나라가 아니다.
_COUNTRY_SPECIAL = {"": "미상", "WW": "특정 나라 아님"}


def _counts(raw) -> dict[str, int]:
    try:
        d = json.loads(raw) if isinstance(raw, str) else (raw or {})
    except (TypeError, ValueError):
        return {}
    return {str(k): int(v) for k, v in d.items() if isinstance(v, (int, float))} if isinstance(d, dict) else {}


def _shares(counts: dict[str, int], label, top: int = 8) -> list[dict]:
    total = sum(counts.values())
    rows = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [{"key": k, "label": label(k), "n": n, "share": round(n / total, 4) if total else None}
            for k, n in rows[:top]]


def serp_adapter_label(code: str) -> str:
    """ISO 두 글자 나라 코드 → 나라 이름. 이름표는 serp_adapter 한 벌이다(거기는 서치콘솔의
    세 글자 코드로 찾으므로 로케일 지역 칸을 거쳐 바꾼다). 모르는 코드는 대문자 그대로."""
    import serp_adapter
    return serp_adapter.country_label(serp_adapter.locale_country("xx-" + str(code)) or str(code))


def distribution(summary: dict, domains) -> dict:
    """TLD·국가 분포(요약이 준 것)와 한 도메인 집중도(참조 도메인 표 ÷ 요약의 총 백링크)."""
    s = summary or {}
    tld = _counts(s.get("tld_json"))
    country = _counts(s.get("country_json"))
    total = s.get("backlinks")
    top = max((d for d in (domains or []) if d.get("backlinks")), key=lambda d: d["backlinks"], default=None)
    conc = {"enough": False, "why": None, "domain": None, "share": None, "tier": None,
            "tiers": list(CONCENTRATION_TIERS)}
    if not top or not total:
        conc["why"] = "참조 도메인별 링크 수와 총 백링크가 둘 다 있어야 잽니다"
    elif total < MIN_LINKS:
        conc["why"] = f"총 백링크가 {total}개뿐입니다. {MIN_LINKS}개부터 비율을 말합니다"
    else:
        share = min(1.0, top["backlinks"] / total)
        conc.update(enough=True, domain=top["domain"], share=round(share, 4),
                    tier=max((t for t in CONCENTRATION_TIERS if share > t), default=None))
    return {"tld": _shares(tld, lambda k: "." + k if k else "미상"),
            "tld_total": sum(tld.values()),
            "country": _shares(country, lambda k: _COUNTRY_SPECIAL.get(k) or serp_adapter_label(k)),
            "country_total": sum(country.values()),
            "concentration": conc}


# ── spam score 분포 ──────────────────────────────────────────────────────────
# 눈금은 DataForSEO 가 문서에 적은 세 구간 그대로다(0~30 낮음 · 31~60 중간 · 61~100 높음):
# https://dataforseo.com/help-center/what-is-spam-score-and-how-is-it-calculated
# 우리는 구간 이름을 붙이지 않고 범위만 말한다 — '독성'·'위험' 같은 판정은 만들지 않는다.
SPAM_BANDS = ((0, 30), (31, 60), (61, 100))
# 스팸 링크에 대해 할 말 한 벌(화면·요청문 공용). 구글은 스팸 링크를 대부분 스스로 무시한다:
#   "Penguin now devalues spam by adjusting ranking based on spam signals, rather than
#    affecting ranking of the whole site."
#    https://developers.google.com/search/blog/2016/09/penguin-is-now-part-of-our-core
# 링크 거부(disavow)는 수동 조치가 있거나 곧 있을 때만의 도구다:
#   "The links have caused a manual action, or likely will cause a manual action, on your site."
#   "If used incorrectly, this feature can potentially harm your site's performance"
#    https://support.google.com/webmasters/answer/2648487
SPAM_SAYS = ("spam score 는 수집원이 매긴 추정치입니다. 구글은 스팸 링크를 대부분 스스로 무시하므로 "
             "점수가 높은 링크가 있다는 것만으로 할 일은 없습니다. 링크 거부(disavow)는 서치콘솔에 "
             "'부자연스러운 링크' 수동 조치가 떴을 때만 검토합니다. 잘못 쓰면 순위를 해칩니다.")


def spam(summary: dict, domains) -> dict | None:
    """spam score 분포 — 요약 점수와 참조 도메인의 구간별 개수. 받은 점수가 없으면 None."""
    s = summary or {}
    scored = [int(d["spam_score"]) for d in (domains or []) if d.get("spam_score") is not None]
    if s.get("spam_score") is None and not scored:
        return None
    return {"score": s.get("spam_score"), "target_score": s.get("target_spam_score"),
            "bands": [{"lo": lo, "hi": hi, "n": sum(1 for v in scored if lo <= v <= hi)}
                      for lo, hi in SPAM_BANDS],
            "scored": len(scored), "domains": len(domains or []), "says": SPAM_SAYS}


# ── 링크가 몰린 페이지 · 하나도 없는 페이지 ──────────────────────────────────

def pages(conn, pid: int, bl_date: str | None, *, top: int = 10, zero_top: int = 20) -> dict:
    """우리 페이지별 백링크 → 많이 받은 페이지(magnets)와 하나도 없는 페이지(zero).

    zero 는 **크롤이 본 200 페이지 중 페이지별 표에 없는 것**이다. 페이지별 표가 상한에 걸려
    잘렸으면(줄 수 = PAGES_LIMIT) '표에 없다'가 '링크가 없다'가 아니므로 zero 를 말하지 않는다
    (zero=None, zero_why). 크롤 전이어도 같다."""
    import collect_backlinks
    import scoring
    rows = [dict(r) for r in conn.execute(
        "SELECT url, backlinks, referring_domains, rank FROM backlink_pages"
        " WHERE project_id=? AND checked_date=? ORDER BY backlinks IS NULL, backlinks DESC, url",
        (pid, bl_date))] if bl_date else []
    out = {"magnets": [r for r in rows if r["backlinks"]][:top], "listed": len(rows),
           "zero": None, "zero_n": None, "zero_why": None}
    run = conn.execute("SELECT MAX(id) FROM crawl_runs WHERE project_id=? AND finished_at IS NOT NULL",
                       (pid,)).fetchone()[0]
    if not rows:
        out["zero_why"] = "페이지별 백링크를 아직 받지 않았습니다"
    elif len(rows) >= collect_backlinks.PAGES_LIMIT:
        out["zero_why"] = (f"페이지별 표가 상한({collect_backlinks.PAGES_LIMIT}줄)까지 찼습니다. 표에 없는 "
                           "페이지가 링크가 없는 것인지 잘린 것인지 가를 수 없습니다")
    elif not run:
        out["zero_why"] = "크롤 전이라 우리 페이지 목록이 없습니다"
    else:
        key = lambda u: scoring.host_of(u) + "/" + re.sub(r"^[a-z]+://[^/]+/?", "", str(u or "").lower()).rstrip("/")
        have = {key(r["url"]) for r in rows if r["backlinks"]}
        zero = [dict(r) for r in conn.execute(
            "SELECT url, links_in, depth FROM crawl_pages WHERE run_id=? AND status=200"
            " ORDER BY depth IS NULL, depth, links_in DESC, url", (run,)) if key(r["url"]) not in have]
        out["zero"], out["zero_n"] = zero[:zero_top], len(zero)
    return out


# ── 링크 속도 ────────────────────────────────────────────────────────────────
VELOCITY_MIN_MONTHS = 4     # 끝난 달이 이만큼은 있어야 비교한다
VELOCITY_SPIKE_X = 3        # 직전 달들 중앙값의 몇 배부터 '급증'인가
VELOCITY_SPIKE_MIN = 10     # 그리고 최소 몇 곳 — 1곳→3곳을 급증이라 하지 않는다
VELOCITY_DECLINE_MONTHS = 3  # 잃은 곳이 얻은 곳보다 많은 달이 이만큼 이어지면 '감소'


def velocity(series, today: str) -> dict:
    """달별 시계열 → 끝난 달들과 속도 표시(flag: spike | decline | None).

    이번 달(끝 날이 오늘 이후)은 아직 덜 찼으므로 비교에서 뺀다. 표시는 사실 두 가지뿐이다:
    지난달 새 참조 도메인이 평소(직전 달들의 중앙값)의 VELOCITY_SPIKE_X 배 이상, 또는 잃은
    곳이 얻은 곳보다 많은 달이 VELOCITY_DECLINE_MONTHS 달 연속. 원인은 말하지 않는다 —
    급증은 기사 한 편일 수도 스팸일 수도 있다."""
    rows = sorted((dict(r) for r in series or [] if r.get("month")), key=lambda r: r["month"])
    done = [r for r in rows if r["month"] < today and r.get("new_referring_domains") is not None
            and r.get("lost_referring_domains") is not None]
    out = {"months": rows, "enough": len(done) >= VELOCITY_MIN_MONTHS, "flag": None, "why": None,
           "rule": {"min_months": VELOCITY_MIN_MONTHS, "spike_x": VELOCITY_SPIKE_X,
                    "spike_min": VELOCITY_SPIKE_MIN, "decline_months": VELOCITY_DECLINE_MONTHS}}
    if not out["enough"]:
        out["why"] = (f"끝난 달의 증감이 {len(done)}달 치뿐입니다. {VELOCITY_MIN_MONTHS}달부터 "
                      "평소와 견줍니다")
        return out
    last, prev = done[-1], sorted(r["new_referring_domains"] for r in done[:-1])
    mid = prev[len(prev) // 2] if len(prev) % 2 else (prev[len(prev) // 2 - 1] + prev[len(prev) // 2]) / 2
    new = last["new_referring_domains"]
    if new >= VELOCITY_SPIKE_MIN and new >= VELOCITY_SPIKE_X * max(mid, 1):
        out.update(flag="spike", month=last["month"], new=new, usual=mid)
    elif all(r["lost_referring_domains"] > r["new_referring_domains"]
             for r in done[-VELOCITY_DECLINE_MONTHS:]):
        out.update(flag="decline", month=last["month"],
                   lost=sum(r["lost_referring_domains"] for r in done[-VELOCITY_DECLINE_MONTHS:]),
                   new=sum(r["new_referring_domains"] for r in done[-VELOCITY_DECLINE_MONTHS:]))
    return out


# ── 자기검사 ─────────────────────────────────────────────────────────────────

def _selfcheck() -> None:
    C = lambda a, **kw: classify_anchor(a, brands=["디아더 피부과", "The Other Skin"],
                                        domain="theotherskin.com", keywords=["강남 피부과", "sofwave"], **kw)
    assert C(None) == "empty" and C("   ") == "empty" and C("—") == "empty"
    assert C("https://theotherskin.com/x") == "url" and C("www.naver.com") == "url"
    assert C("theotherskin.com") == "url" and C("example.co.kr/a") == "url"
    assert C("디아더피부과") == "brand" and C("The Other Skin 후기") == "brand"
    assert C("TheOtherSkin") == "brand", "도메인 이름도 브랜드다"
    assert C("강남 피부과") == "exact" and C("강남피부과!") == "exact" and C("Sofwave") == "exact"
    assert C("강남 피부과 추천") == "other", "부분 일치는 정확 일치가 아니다"
    for g in ("Click Here", "여기", "こちら", "点击这里", "aquí", "hier klicken", "ici", "clique aqui"):
        assert C(g) == "generic", g
    assert C("이 글은 피부 관리법을 다룹니다") == "other"
    # 언어별 표는 한 벌이고 접은 꼴이 서로 안 겹쳐도 된다 — 다만 빈 낱말이 들어가면 전부 일반이 된다.
    assert "" not in _GENERIC and all(GENERIC_ANCHORS.values())

    # 쏠림은 정확 일치 비율로만. 브랜드 앵커가 90% 여도 표시는 없다.
    rows = [{"anchor": "디아더피부과", "backlinks": 90}, {"anchor": "여기", "backlinks": 10}]
    m = anchor_mix(rows, brands=["디아더 피부과"], domain="theotherskin.com", keywords=["강남 피부과"])
    assert m["enough"] and not m["warn"] and m["exact_share"] == 0, m
    assert [t["type"] for t in m["types"]] == ["brand", "generic"] and m["types"][0]["share"] == 0.9, m
    rows = [{"anchor": "강남 피부과", "backlinks": 60}, {"anchor": "디아더피부과", "backlinks": 40}]
    m = anchor_mix(rows, brands=["디아더 피부과"], domain="theotherskin.com", keywords=["강남 피부과"])
    assert m["warn"] and m["exact_share"] == 0.6 and m["warn_at"] == ANCHOR_EXACT_WARN, m
    # 근거 부족 — 링크가 적으면 비율 칸이 아예 None 이다(찍을 숫자가 없다).
    m = anchor_mix([{"anchor": "강남 피부과", "backlinks": 5}], keywords=["강남 피부과"])
    assert not m["enough"] and not m["warn"] and m["exact_share"] is None, m
    assert all(t["share"] is None for t in m["types"]) and str(MIN_LINKS) in m["why"], m
    # 추적 검색어가 없으면 '정확 일치 0%'라고 말하지 않는다.
    m = anchor_mix([{"anchor": "x", "backlinks": 50}], keywords=[])
    assert m["enough"] and not m["exact_known"] and m["exact_share"] is None and not m["warn"], m
    assert anchor_mix([])["why"] and anchor_mix([])["types"] == []

    # 분포 — 집중도 눈금과 근거 부족.
    doms = [{"domain": "a.com", "backlinks": 30}, {"domain": "b.com", "backlinks": 5}]
    d = distribution({"backlinks": 100, "tld_json": '{"com": 40, "kr": 10}',
                      "country_json": '{"": 20, "US": 25, "WW": 5}'}, doms)
    c = d["concentration"]
    assert (c["enough"], c["domain"], c["share"], c["tier"]) == (True, "a.com", 0.3, 0.25), c
    assert d["tld"][0] == {"key": "com", "label": ".com", "n": 40, "share": 0.8}, d["tld"]
    assert [x["key"] for x in d["country"]] == ["US", "", "WW"], d["country"]
    lab = {x["key"]: x["label"] for x in d["country"]}
    # 나라 이름은 serp_adapter 의 표 한 벌에서 온다(ISO 두 글자 → 그 표의 이름). 특수 값은 나라가 아니다.
    assert lab["WW"] == "특정 나라 아님" and lab[""] == "미상", lab
    assert lab["US"] == serp_adapter_label("US") != "US", lab
    assert distribution({"backlinks": 1000}, doms)["concentration"]["tier"] is None
    for s in ({}, {"backlinks": 10}, {"backlinks": None}):
        c = distribution(s, doms)["concentration"]
        assert not c["enough"] and c["share"] is None and c["why"], c
    assert distribution({"tld_json": "not json"}, [])["tld"] == []

    # spam — 분포만. 받은 점수가 없으면 통째로 None(0 점이 아니다).
    assert spam({}, [{"domain": "a"}]) is None
    sp = spam({"spam_score": 14}, [{"spam_score": 0}, {"spam_score": 30}, {"spam_score": 31},
                                   {"spam_score": 99}, {"spam_score": None}])
    assert [b["n"] for b in sp["bands"]] == [2, 1, 1] and (sp["scored"], sp["domains"]) == (4, 5), sp
    assert "수동 조치" in sp["says"] and "disavow" in sp["says"]

    # 링크 속도 — 덜 찬 이번 달은 빼고, 평소의 몇 배인지로만.
    mk = lambda m, new, lost: {"month": m, "new_referring_domains": new, "lost_referring_domains": lost}
    base = [mk("2026-06-30", 4, 1), mk("2026-07-31", 5, 2), mk("2026-08-31", 3, 1), mk("2026-09-30", 4, 2)]
    v = velocity(base + [mk("2026-10-31", 30, 1), mk("2026-11-30", 900, 0)], "2026-11-24")
    assert (v["flag"], v["month"], v["new"], v["usual"]) == ("spike", "2026-10-31", 30, 4.0), v
    assert velocity(base + [mk("2026-10-31", 9, 1)], "2026-11-24")["flag"] is None, "9곳은 최소치 아래다"
    dec =[mk("2026-06-30", 4, 1), mk("2026-07-31", 5, 2), mk("2026-08-31", 1, 5),
           mk("2026-09-30", 2, 6), mk("2026-10-31", 0, 3)]
    v = velocity(dec, "2026-11-24")
    assert (v["flag"], v["lost"], v["new"]) == ("decline", 14, 3), v
    v = velocity(base[:3], "2026-11-24")
    assert not v["enough"] and v["flag"] is None and str(VELOCITY_MIN_MONTHS) in v["why"], v
    assert velocity([{"month": "2026-10-31", "backlinks": 5}], "2026-11-24")["enough"] is False
    print("link_profile self-check ok")


if __name__ == "__main__":
    _selfcheck()
