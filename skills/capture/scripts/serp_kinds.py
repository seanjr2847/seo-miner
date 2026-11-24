"""검색결과에 선 것이 '무엇인가'를 가르는 판정 한 벌 — 표와 순수 함수뿐이다(네트워크·DB 없음).

  page_kind      상위 글의 페이지 종류(마크업 @type 기준). 요청문 형식 표 옆 한 줄이 쓴다.
  peer_skip      이 상위 자리가 '글로 겨룰 상대'가 아닌 까닭(제3자 플랫폼·뉴스·공공/교육·
                 마켓·SEO 도구·태그/작성자 목록). 상위 글 열기(collect_serp)·요청문 비교
                 재료(brief._serp_top)·SERP 겹침(scoring._surface_facts)이 같은 판정을 쓴다.
  snippet_form   강조 스니펫의 형식(문단·목록·표·영상).
  region_notes   구글이 검색결과 구성을 달리하는 지역(EEA·튀르키예·남아공)의 주석.
  ad_intent      SERP 광고 문구에 선 의도 낱말 — 검색어 의도 휴리스틱을 올리기만 한다.

LLM 판정·점수는 없다. 목록의 정본은 skill_config(제3자 플랫폼·마켓·SEO 도구)와
scoring.PRESS_OUTLETS(언론)이고, 여기는 그것을 읽어 한 가지 답을 낸다.

self-check:  python serp_kinds.py
"""
import functools
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).parent))
import scoring  # noqa: E402

# ── 페이지 종류 ───────────────────────────────────────────────────────────────
# (종류 id, 사람 말, 그 종류로 읽는 schema.org @type). **위에서부터** 먼저 맞는 것이
# 이긴다 — 한 페이지가 Product 와 BreadcrumbList·Organization 을 같이 달아도 상품이다.
# 종류를 말해 주지 않는 유형(BreadcrumbList·Organization·WebSite·WebPage·ImageObject·
# Person)은 표에 없다: 그것만 단 페이지는 None(모름)이지 '글'이 아니다.
PAGE_KINDS: tuple[tuple[str, str, frozenset[str]], ...] = (
    ("product", "상품", frozenset({"Product", "ProductGroup", "Offer", "AggregateOffer"})),
    ("app", "소프트웨어·도구", frozenset({"SoftwareApplication", "WebApplication",
                                          "MobileApplication", "VideoGame"})),
    ("local", "업체·매장", frozenset({
        "LocalBusiness", "Restaurant", "Store", "MedicalClinic", "MedicalBusiness",
        "Dentist", "Physician", "Hospital", "LegalService", "Hotel", "LodgingBusiness",
        "BeautySalon", "HealthAndBeautyBusiness", "AutoRepair", "RealEstateAgent",
        "FoodEstablishment", "ProfessionalService", "HomeAndConstructionBusiness"})),
    ("video", "영상", frozenset({"VideoObject", "Clip"})),
    ("recipe", "레시피", frozenset({"Recipe"})),
    ("course", "강의", frozenset({"Course", "CourseInstance"})),
    ("event", "행사", frozenset({"Event"})),
    ("job", "채용 공고", frozenset({"JobPosting"})),
    ("forum", "질문·토론", frozenset({"QAPage", "DiscussionForumPosting", "SocialMediaPosting"})),
    ("news", "뉴스 기사", frozenset({"NewsArticle", "ReportageNewsArticle", "LiveBlogPosting"})),
    ("article", "글", frozenset({"Article", "BlogPosting", "TechArticle", "ScholarlyArticle",
                                 "MedicalWebPage", "HowTo", "Report"})),
    ("faq", "FAQ", frozenset({"FAQPage"})),
    ("listing", "목록·모음", frozenset({"ItemList", "CollectionPage", "SearchResultsPage",
                                        "OfferCatalog"})),
)
PAGE_KIND_LABEL = {k: label for k, label, _ in PAGE_KINDS}


def page_kind(schema_types) -> str | None:
    """마크업 @type 목록 → 페이지 종류 id. 종류를 말해 주는 유형이 없으면 None(모름).

    근거는 그 페이지가 **스스로 단** 마크업뿐이다. 주소 꼴·본문 길이로 짐작하지 않는다 —
    마크업이 없는 글을 '글'이라고 부르면 분포가 지어낸 숫자가 된다."""
    types = {str(t) for t in (schema_types or []) if t}
    if not types:
        return None
    for kind, _label, marks in PAGE_KINDS:
        if types & marks:
            return kind
    return None


def page_kind_mix(schema_lists) -> list[tuple[str, int]]:
    """상위 글들의 종류 분포 [(사람 말, 곳 수)] — 많은 순, 같으면 표 순서. 모름은 '종류 모름'."""
    order = {k: i for i, (k, _, _) in enumerate(PAGE_KINDS)}
    n: dict[str | None, int] = {}
    for types in schema_lists:
        k = page_kind(types)
        n[k] = n.get(k, 0) + 1
    got = sorted(n.items(), key=lambda kv: (-kv[1], order.get(kv[0], len(order))))
    return [(PAGE_KIND_LABEL.get(k, "종류 모름"), c) for k, c in got]


# ── 비교 재료에서 뺄 자리 ─────────────────────────────────────────────────────
# 까닭 id → 사람 말. 순서가 판정 순서다(제3자 플랫폼이 언론보다 먼저 — scoring.is_press 의 규칙).
PEER_SKIP_LABEL = {
    "platform": "제3자 플랫폼",
    "press": "뉴스·언론",
    "public": "공공·교육 기관",
    "market": "마켓·오픈마켓",
    "seo_tool": "SEO 도구 사이트",
    "archive": "태그·작성자 목록",
}
# 공공·교육 기관 — 목록이 아니라 도메인 꼴이다(나라마다 수천 곳이라 목록으로 못 덮는다).
# 끝 꼬리표가 이것이거나(.gov·.edu·.mil), 나라 꼬리 바로 앞이 이것이다(.gov.uk·.go.kr·
# .ac.jp·.edu.au·.gouv.fr·.gob.mx). 'go'·'ac' 는 나라 꼬리 앞에서만 본다 — go.com 은 아니다.
_PUBLIC_TLD = frozenset({"gov", "edu", "mil"})
_PUBLIC_SLD = frozenset({"gov", "edu", "mil", "go", "ac", "gouv", "gob", "govt", "sch"})
# 글이 아니라 글 묶음인 주소 — 언어를 안 탄다(주소 조각은 CMS 가 영어로 만든다).
_ARCHIVE_PATH = re.compile(r"/(tag|tags|author|authors)/", re.I)


def _cfg_hosts(key: str) -> tuple[str, ...]:
    """skill_config 의 호스트 목록 한 줄. 읽기 실패면 빈 목록(없는 갈래를 지어내지 않는다)."""
    try:
        import collector
        got = collector.config().get(key)
    except Exception:
        got = None
    if not isinstance(got, list):
        return ()
    return tuple(h for h in (scoring.host_of(str(x)) for x in got) if h)


def is_public(domain: str) -> bool:
    labels = (domain or "").lower().strip(".").split(".")
    if len(labels) < 2:
        return False
    if labels[-1] in _PUBLIC_TLD:
        return True
    return len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in _PUBLIC_SLD


@functools.lru_cache(maxsize=1)
def _lists() -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """(제3자 플랫폼, 언론, 마켓, SEO 도구) — 한 프로세스에서 한 번만 편다. 목록은 코드
    상수(skill_config·scoring.PRESS_OUTLETS)라 도는 중에 안 바뀐다. 대시보드 한 번에
    상위 자리 수천 줄을 가르므로 줄마다 다시 펴지 않는다."""
    return (scoring.third_party_platforms(), scoring.press_outlets(),
            _cfg_hosts("marketplaces"), _cfg_hosts("seo_tool_sites"))


def _site_kind(d: str) -> str | None:
    """도메인이 속한 '사이트 종류' 까닭 — 주소 꼴(archive)은 여기서 안 본다."""
    plats, press, markets, tools = _lists()
    if scoring.is_third_party(d, plats):
        return "platform"
    if scoring.is_press(d, press):
        return "press"
    if is_public(d):
        return "public"
    if any(scoring.owns(d, m) for m in markets):
        return "market"
    if any(scoring.owns(d, m) for m in tools):
        return "seo_tool"
    return None


def peer_skip(url: str | None, domain: str | None = None, own: str | None = None) -> str | None:
    """이 상위 자리를 비교 재료에서 빼는 까닭 id(PEER_SKIP_LABEL 의 키), 뺄 까닭이 없으면 None.

    뺀다는 것은 '검색결과에 없었다'가 아니다 — 상위 표에는 그대로 선다. 그 글의 H2·형식을
    우리 글과 견주지 않고, 두 검색어가 같은 지면인지(SERP 겹침) 세는 데도 안 쓴다는 뜻이다:
    위키백과·유튜브는 어느 검색어에나 서서, 그것이 겹쳤다고 같은 주제가 되지 않는다.

    own(우리 도메인)을 주면 **우리와 같은 종류**의 사이트는 빼지 않는다 — 대학 사이트에게
    다른 대학(.edu)은, 언론사에게 다른 언론은, 마켓에게 다른 마켓은 바로 그 겨룰 상대다."""
    d = scoring.host_of(domain or url or "")
    if not d:
        return None
    why = _site_kind(d)
    if why:
        mine = _site_kind(scoring.host_of(own)) if own else None
        return None if mine == why else why
    try:
        path = urlsplit(url or "").path
    except ValueError:
        path = ""
    if _ARCHIVE_PATH.search(path + "/"):
        return "archive"
    return None


def skip_summary(reasons) -> str:
    """까닭 id 들 → '제3자 플랫폼 2 · 뉴스·언론 1'. 표 순서대로."""
    n: dict[str, int] = {}
    for r in reasons:
        if r:
            n[r] = n.get(r, 0) + 1
    return " · ".join(f"{PEER_SKIP_LABEL[k]} {n[k]}" for k in PEER_SKIP_LABEL if k in n)


# ── 지도 칸 안의 내 업체 ──────────────────────────────────────────────────────
_NAME_MIN = 2   # 이보다 짧은 별칭은 이름 대조에 안 쓴다(한 글자는 아무 업체 이름에나 든다)


def local_pack_own(items, own_domain: str, aliases=()) -> dict:
    """지도 칸 항목들 → {"items", "own_pos", "own_by"}.

    내 업체 판정은 ① 항목의 사이트 도메인이 내 도메인(scoring.owns) ② 그게 없으면 항목
    이름에 브랜드 별칭이 통째로 들었나(scoring.norm 으로 견준다). 지도 칸 항목은 사이트를
    안 건 업체가 많아 도메인만 보면 '없음'으로 잘못 읽는다. 별칭이 없고 도메인도 안 맞으면
    own_pos 는 None 이고, 그것은 "이 조회의 지도 칸에서 못 찾았다"이지 프로필이 없다는
    뜻이 아니다."""
    rows = [x for x in (items or []) if isinstance(x, dict)]
    names = [scoring.norm(str(a)) for a in (aliases or []) if len(scoring.norm(str(a))) >= _NAME_MIN]
    for by in ("domain", "name"):
        for x in rows:
            if by == "domain":
                hit = bool(x.get("domain") and own_domain and scoring.owns(x["domain"], own_domain))
            else:
                title = scoring.norm(str(x.get("title") or ""))
                hit = bool(title and any(a in title for a in names))
            if hit:
                return {"items": rows, "own_pos": x.get("pos"), "own_by": by}
    return {"items": rows, "own_pos": None, "own_by": None}


# ── 강조 스니펫 형식 ──────────────────────────────────────────────────────────
SNIPPET_FORMS = ("paragraph", "list", "table", "video")
_LIST_LINES = 3   # 줄이 이만큼 갈려 오면 목록이다(문단 스니펫은 한 덩어리로 온다)


def snippet_form(item: dict | None) -> str | None:
    """DataForSEO `featured_snippet` 항목 → 형식. 못 가르면 None(모름).

    응답에 형식을 말해 주는 칸은 없다(문서 확인 2026-11-24:
    https://docs.dataforseo.com/v3/serp/google/organic/live/advanced/ — featured_snippet 의
    칸은 domain·title·featured_title·description·url·images·table 이고 구조 단서는 table
    하나다). 그래서: table 이 있으면 표, 출처가 영상 플랫폼이면 영상, description 이 세 줄
    이상으로 갈려 오면 목록, 한 덩어리 글이면 문단. description 이 비면 모름이다."""
    if not isinstance(item, dict):
        return None
    table = item.get("table")
    if isinstance(table, dict) and (table.get("table_content") or table.get("table_header")):
        return "table"
    host = scoring.host_of(item.get("url") or item.get("domain") or "")
    if host and any(scoring.owns(host, v) for v in ("youtube.com", "youtu.be", "vimeo.com")):
        return "video"
    desc = item.get("description")
    if not isinstance(desc, str) or not desc.strip():
        return None
    lines = [ln for ln in desc.splitlines() if ln.strip()]
    return "list" if len(lines) >= _LIST_LINES else "paragraph"


# ── 지역 검색 유닛 ────────────────────────────────────────────────────────────
# 구글은 몇 지역에서 검색결과 구성 자체가 다르다. 출처는 구글 검색 문서(2026-11-24 확인):
#   https://developers.google.com/search/docs/appearance/aggregator-features
#     — 지역별 기능 목록: EEA(aggregator unit·supplier unit·ecosystem carousel·Job sites),
#       튀르키예(Places sites 캐러셀·칩), 남아공('South African' 배지·칩)
#   https://developers.google.com/search/blog/2024/02/search-experiences-in-eea
#   https://developers.google.com/search/docs/appearance/places-sites
#   https://developers.google.com/search/blog/2024/09/search-experiences-in-sa
#     — 남아공 배지는 여행·렌터카·버스 예약·쇼핑·음식 배달 검색어에 서고 순위를 바꾸지 않는다
# 여기 적는 것은 '그 지역에는 이런 칸이 따로 선다'까지다. 그 칸이 이 검색어에 실제로
# 섰는지는 수집이 재지 않는다 — 그래서 주석이지 판정이 아니다.
EEA = frozenset({  # EU 27 + 아이슬란드·리히텐슈타인·노르웨이
    "AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU", "IE",
    "IT", "LV", "LT", "LU", "MT", "NL", "PL", "PT", "RO", "SK", "SI", "ES", "SE",
    "IS", "LI", "NO"})
REGION_UNIT_NOTE: tuple[tuple[str, frozenset[str], str], ...] = (
    ("eea", EEA,
     "이 검색어는 유럽경제지역(EEA) 구글에서 잽니다. 이 지역은 디지털시장법(DMA) 때문에 "
     "비교·중개 사이트 전용 칸(aggregator unit)과 공급자 사이트 칸(supplier unit)이 따로 서고, "
     "호텔·항공·상품·지역 업체 검색어에서 지도 칸·쇼핑 칸의 자리가 다른 지역과 다릅니다. "
     "지도 칸·쇼핑 칸이 '없다'는 조회 결과를 그 형식의 수요가 없다는 뜻으로 읽지 않습니다."),
    ("tr", frozenset({"TR"}),
     "이 검색어는 튀르키예 구글에서 잽니다. 이 지역은 지역 업체·호텔 검색어에 중개 사이트 "
     "캐러셀(Places sites)이 따로 섭니다. 지도 칸 옆에 디렉터리·예약 사이트가 한 줄로 서므로 "
     "지도 칸 경쟁을 볼 때 그 사이트 등재도 같이 봅니다."),
    ("za", frozenset({"ZA"}),
     "이 검색어는 남아프리카공화국 구글에서 잽니다. 이 지역은 여행·쇼핑·음식 배달 같은 "
     "검색어에서 현지 사이트에 'South African' 배지와 거르기 칩이 붙습니다(순위는 안 바꿉니다)."),
)


def region_of(locale: str | None) -> str | None:
    """로케일(ko-KR·de-DE) → 지역 주석 id. 나라가 없는 로케일·해당 없는 나라는 None."""
    cc = (locale or "").replace("_", "-").partition("-")[2].upper()
    for rid, countries, _ in REGION_UNIT_NOTE:
        if cc in countries:
            return rid
    return None


def region_note(locale: str | None) -> str | None:
    rid = region_of(locale)
    return next((note for r, _, note in REGION_UNIT_NOTE if r == rid), None)


# ── 광고 문구로 의도 보강 ─────────────────────────────────────────────────────
_AXIS_RANK = {"info": 0, "navigational": 1, "commercial": 2, "transactional": 3}


def ad_intent(ads) -> str | None:
    """SERP 광고(제목·설명)에 선 의도 낱말의 축 — transactional > commercial. 없으면 None.

    낱말 사전은 scoring.INTENT_WORDS 한 벌이다(여기 사본을 두지 않는다). 광고가 **섰다**는
    것만으로는 올리지 않는다 — 브랜드 방어 광고는 정보 검색어에도 선다. 광고주가 문구에
    값·구매·비교 낱말을 썼을 때만 그 축을 말한다."""
    best = None
    for a in ads or []:
        if not isinstance(a, dict):
            continue
        ts = set(scoring.tokens(" ".join(str(a.get(k) or "") for k in ("title", "description"))))
        axis = ("transactional" if ts & scoring.INTENT_TRANSACTIONAL else
                "commercial" if ts & scoring.INTENT_COMMERCIAL else None)
        if axis and (best is None or _AXIS_RANK[axis] > _AXIS_RANK[best]):
            best = axis
    return best


def raise_intent(base: str, ads) -> str:
    """휴리스틱 의도를 광고 문구로 **올리기만** 한다(내리지 않는다)."""
    up = ad_intent(ads)
    if up and _AXIS_RANK[up] > _AXIS_RANK.get(base, 0):
        return up
    return base


def _selfcheck() -> None:
    assert page_kind(["BreadcrumbList", "Product", "Organization"]) == "product"
    assert page_kind(["Article", "FAQPage"]) == "article"
    assert page_kind(["BreadcrumbList", "Organization"]) is None and page_kind([]) is None
    assert page_kind_mix([["Article"], ["BlogPosting"], ["Product"], []]) == [
        ("글", 2), ("상품", 1), ("종류 모름", 1)]
    ids = [k for k, _, _ in PAGE_KINDS]
    assert len(ids) == len(set(ids))
    assert is_public("www.cdc.gov") and is_public("nhs.gov.uk") and is_public("snu.ac.kr")
    assert is_public("mit.edu") and is_public("service-public.gouv.fr")
    assert not is_public("go.com") and not is_public("ac.com") and not is_public("example.com")
    assert peer_skip("https://blog.example.com/tag/seo/") == "archive"
    assert peer_skip("https://example.com/authors/kim") == "archive"
    assert peer_skip("https://example.com/tagline/") is None
    assert peer_skip("https://www.cdc.gov/flu") == "public"
    assert peer_skip("https://www.cdc.gov/flu", own="nih.gov") is None       # 같은 종류는 겨룰 상대다
    assert peer_skip("https://www.cdc.gov/flu", own="example.com") == "public"
    lp = [{"pos": 1, "title": "가나 치과", "domain": None},
          {"pos": 2, "title": "우리 피부과 강남점", "domain": None},
          {"pos": 3, "title": "다라", "domain": "www.ours.com"}]
    assert local_pack_own(lp, "ours.com")["own_pos"] == 3
    assert local_pack_own(lp, "x.com", ["우리 피부과"]) == {"items": lp, "own_pos": 2, "own_by": "name"}
    assert local_pack_own(lp, "x.com", ["우"])["own_pos"] is None
    assert snippet_form({"table": {"table_header": ["a"], "table_content": [["1"]]}}) == "table"
    assert snippet_form({"url": "https://www.youtube.com/watch?v=1", "description": "x"}) == "video"
    assert snippet_form({"description": "1. a\n2. b\n3. c"}) == "list"
    assert snippet_form({"description": "한 문단짜리 직답입니다."}) == "paragraph"
    assert snippet_form({"description": ""}) is None and snippet_form(None) is None
    assert region_of("de-DE") == "eea" and region_of("tr-TR") == "tr" and region_of("en-ZA") == "za"
    assert region_of("ko-KR") is None and region_of("en") is None and region_of("en-GB") is None
    assert ad_intent([{"title": "Buy now", "description": "free shipping"}]) in (
        "transactional", "commercial", None)
    assert raise_intent("commercial", []) == "commercial"
    print("serp_kinds ok")


if __name__ == "__main__":
    _selfcheck()
