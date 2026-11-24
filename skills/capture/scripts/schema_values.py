#!/usr/bin/env python3
"""구조화 데이터(JSON-LD) **값** 검사 — 순수 함수 한 벌. stdlib(+ 이 리포의 schema_vocab)만 쓴다.

속성이 '있나'는 scoring.SCHEMA_RULES 가 본다. 여기는 그 다음이다: 있는 값이 쓸 수 있는 값인가.
칸 존재만 보면 price "$29.99"·상대 주소 logo·"[Your Company]" 가 전부 통과한다.

쓰는 곳이 둘이다 — 그래서 수집기도 판정도 아닌 따로 선 모듈이다:
  · collect_page 가 페이지의 ld+json 을 읽을 때(page_audits.schema_issues_json)
  · create 스킬이 PR 에 넣을 JSON-LD 를 커밋 전에 볼 때(같은 규칙을 다시 짜지 않는다)

부르는 법:
    blocks = [schema_values.loads(text) for text in ld_json_texts]     # 숫자 원문을 지킨다
    issues = schema_values.value_issues(blocks, page_url="https://me.example/p")
    for i in issues:
        level, sentence = schema_values.describe(i)

돌려주는 것: [{"type": 쓴 @type, "prop": 속성 길, "code": ISSUES 의 열쇠[, "own": bool|None]}].
**값은 싣지 않는다**(이름·주소·전화 원문이 칸으로 새지 않게 — 수집기의 규칙과 같다).

규칙마다 근거(구글·schema.org 문서)를 ISSUES 옆에 적었다. 근거를 못 찾은 것은 넣지 않았다:
  · @context 의 http://schema.org — schema.org FAQ 가 http·https 둘 다 된다고 하고
    (https://schema.org/docs/faq.html), 구글 문서에 https 만 받는다는 문장이 없다. 안 잡는다.
  · availability 의 짧은 이름(InStock) — 구글이 받는다고 적었다. 안 잡는다.
"""
import hashlib
import json
import re
from urllib.parse import urlsplit

import schema_vocab

# ── 읽기 ────────────────────────────────────────────────────────────────────


class RawFloat(float):
    """JSON 숫자의 원문을 든 float — 37.56780 을 읽으면 값은 37.5678 이지만 적힌 자릿수는 5 다.
    좌표 정밀도(소수 5자리)를 값이 아니라 **적힌 글자**로 봐야 해서 원문을 지킨다."""

    def __new__(cls, raw: str):
        obj = super().__new__(cls, raw)
        obj.raw = raw
        return obj


def loads(text: str):
    """ld+json 글자 → 파이썬 값. 소수는 RawFloat 로(원문 자릿수 보존). 못 읽으면 ValueError."""
    return json.loads(text, strict=False, parse_float=RawFloat)


def schema_hash(blocks) -> str:
    """읽힌 ld+json 블록 전부의 내용 지문(16자). 블록이 없으면 ''.

    @type 목록(schema_json)은 유형이 그대로면 안 바뀐다 — price 하나·sameAs 한 줄이 바뀐 것은
    모른다. 열쇠 순서·빈칸은 내용이 아니라서 정렬해 지문을 뜬다(같은 내용 = 같은 지문)."""
    blocks = [b for b in (blocks if isinstance(blocks, list) else [blocks]) if b not in (None, "", [], {})]
    if not blocks:
        return ""
    blob = json.dumps(blocks, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


# ── 낱 규칙 (순수) ───────────────────────────────────────────────────────────
# 템플릿에서 안 갈아 끼운 자리표시 — 템플릿 문법과 생성기 기본값은 어느 언어 사이트든 이 꼴이다.
PLACEHOLDER_RE = re.compile(
    r"\[\s*(your|insert|add|enter|company|business|name|city|phone|address|url|date|title|"
    r"description|price|brand|author)\b[^\]]*\]"          # [Your Company Name]
    r"|\{\{.*?\}\}|\{%.*?%\}|<%.*?%>|\$\{[^}]*\}"          # {{ name }} {% x %} <%= x %> ${x}
    r"|%[A-Z][A-Z0-9_]{2,}%"                               # %SITE_NAME%
    r"|\byour[_ -](company|business|name|site|domain|brand|url|logo|phone|address)\b"
    r"|\b(lorem ipsum|placeholder|changeme|change me|replace me|tbd|todo)\b"
    r"|\binsert\b.{0,30}\bhere\b"
    r"|\b(www\.)?(example\.(com|org|net)|yourdomain\.com|yoursite\.com|yourwebsite\.com)\b",
    re.I | re.S)


# 자리표시의 **꼴**(이름 붙은 것) — create 의 커밋 전 검사(jsonld_check)가 어느 꼴인지 말할 때 쓴다.
# 위 PLACEHOLDER_RE 와 함께 '자리표시인가'의 정본이다(두 모듈이 따로 갖지 않는다).
PLACEHOLDER_FORMS = (
    ("대괄호 자리표시", re.compile(r"^\s*\[[^\[\]]{1,80}\]\s*$")),          # [Business Name]·[확인 필요]
    ("대괄호 자리표시", re.compile(r"\[(?:your|insert|url|todo)\b[^\]]*\]", re.I)),
    ("템플릿 표현식", re.compile(r"\{\{.*?\}\}|\$\{[^}]*\}|<%.*?%>|\{%.*?%\}")),
    ("미완 표시", re.compile(r"\b(?:TODO|TBD|FIXME|XXX+|CHANGEME|REPLACE(?:_[A-Z]+)+|REPLACE ME|"
                          r"YOUR[_ ][A-Z_ ]+)\b")),
    ("채움 글", re.compile(r"lorem ipsum", re.I)),
    # example.com·.net·.org 는 문서용 예약 이름이다(RFC 2606) — 템플릿 기본값이 그대로 남은 흔적.
    # .example·.test·.invalid 최상위 이름은 잡지 않는다: 개발·검사용 사이트가 실제로 그 이름에 뜬다.
    ("예약 도메인", re.compile(r"(?:^|[/@.])example\.(?:com|net|org)(?=$|[/:?#])", re.I)),
)


def placeholder_kind(value, own_host: str = "") -> str | None:
    """자리표시면 그 꼴 이름(PLACEHOLDER_FORMS), PLACEHOLDER_RE 에만 걸리면 '자리표시', 아니면 None.

    own_host: 이 마크업이 놓인 페이지의 호스트. 그 사이트 자신이 예약 이름(.test 개발 서버 등)에
    떠 있으면 자기 주소는 자리표시가 아니다 — 남의 예약 주소(example.com 이 남은 것)만 잡는다."""
    if not isinstance(value, str):
        return None
    own = (own_host or "").lower().removeprefix("www.")
    for name, rx in PLACEHOLDER_FORMS:
        m = rx.search(value)
        if m and name == "예약 도메인" and own and own in value.lower():
            continue
        if m:
            return name
    return "자리표시" if PLACEHOLDER_RE.search(value) else None


def is_placeholder(value, own_host: str = "") -> bool:
    """글자 값이 안 갈아 끼운 자리표시인가."""
    return placeholder_kind(value, own_host) is not None


# ISO 8601 — 날짜, 또는 날짜T시각(+시간대). 구글 Article 문서: "in ISO 8601 format", 시간대는
# "We recommend that you provide timezone information" — 그래서 꼴은 틀림, 시간대 없음은 따로 말한다.
# https://developers.google.com/search/docs/appearance/structured-data/article
_ISO_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_ISO_DT = re.compile(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2})(?:[.,]\d+)?)?"
                     r"(Z|[+-]\d{2}(?::?\d{2})?)?$")
_ISO_PARTIAL = re.compile(r"^\d{4}(-(0[1-9]|1[0-2]))?$")            # 2026 · 2026-03 (foundingDate 등)
ISO_TIME = re.compile(r"^\d{2}:\d{2}(?::\d{2})?(Z|[+-]\d{2}(?::?\d{2})?)?$")


def date_state(value) -> str:
    """'ok' | 'no_tz'(날짜+시각인데 시간대 없음) | 'bad'(ISO 8601 이 아니거나 없는 날짜 — 2026-02-30)."""
    from datetime import date, datetime
    if not isinstance(value, str):
        return "bad"
    s = value.strip()
    m = _ISO_DATE.match(s)
    if m:
        try:
            date(int(m[1]), int(m[2]), int(m[3]))
        except ValueError:
            return "bad"
        return "ok"
    m = _ISO_DT.match(s)
    if m:
        try:
            datetime(int(m[1]), int(m[2]), int(m[3]), int(m[4]), int(m[5]), int(m[6] or 0))
        except ValueError:
            return "bad"
        return "ok" if m[7] else "no_tz"
    return "ok" if _ISO_PARTIAL.match(s) else "bad"


def is_iso8601(value) -> bool:
    """'2026-03-02'·'2026-03-02T10:00:00+09:00'·'2026' → True. 'March 2, 2026'·'2026/03/02' → False."""
    return date_state(value) != "bad"


# 주소가 아니어도 되는 절대 참조 — 연락 링크·인라인 데이터·URN 은 그 자체로 온전하다.
_ABS_OTHER = ("mailto", "tel", "data", "urn")


def is_absolute_url(value) -> bool:
    """'https://a.b/c'·'mailto:a@b.c' → True. '/logo.png'·'logo.png'·'//cdn.a/x.png' → False."""
    v = value.strip() if isinstance(value, str) else ""
    u = urlsplit(v)
    return (u.scheme in ("http", "https") and bool(u.netloc)) or u.scheme.lower() in _ABS_OTHER


def context_ok(ctx) -> bool:
    """@context 가 schema.org 를 가리키나 — http·https, www 유무, 끝 '/' 무관. 목록·{"@vocab"} 도 본다."""
    if isinstance(ctx, str):
        u = urlsplit(ctx.strip())
        host = u.netloc.lower()
        return u.scheme in ("http", "https") and (host[4:] if host.startswith("www.") else host) == "schema.org"
    if isinstance(ctx, list):
        return any(context_ok(c) for c in ctx)
    if isinstance(ctx, dict):
        return context_ok(ctx.get("@vocab")) or any(context_ok(v) for v in ctx.values() if isinstance(v, str))
    return False


def decimals(value) -> int | None:
    """숫자 값에 **적힌** 소수 자릿수. 모르면 None(원문 없는 float — 끝의 0 이 사라졌을 수 있다)."""
    raw = value if isinstance(value, str) else getattr(value, "raw", None)
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return 0
    if not isinstance(raw, str):
        return None
    m = re.match(r"^\s*-?\d+(?:\.(\d+))?\s*$", raw)
    return len(m.group(1) or "") if m else None


_PLAIN_NUMBER = re.compile(r"^\d+(\.\d+)?$")


def price_number(value) -> float | None:
    """price 값 → 숫자. 숫자가 아니면 None('$29.99'·'29,99'·'1,299.00'·'29.99 USD').
    schema.org/price: 소수점은 '.', 통화 기호·천 단위 구분 기호를 넣지 않는다."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and _PLAIN_NUMBER.match(value.strip()):
        return float(value.strip())
    return None


# ── 표 ──────────────────────────────────────────────────────────────────────
# 날짜·주소가 값인 속성. 여기 없는 속성은 꼴을 안 본다(모르는 속성에 규칙을 대지 않는다).
DATE_PROPS = ("datePublished", "dateModified", "dateCreated", "uploadDate", "startDate", "endDate",
              "validFrom", "validThrough", "priceValidUntil", "foundingDate", "expires",
              "datePosted", "birthDate", "deathDate", "dissolutionDate", "releaseDate",
              "availabilityStarts", "availabilityEnds", "doorTime", "checkinTime", "checkoutTime",
              "dateIssued", "dateRead", "dateReceived", "dateSent", "arrivalTime", "departureTime",
              "scheduledTime", "orderDate")
# 시각만 받는 속성 — 날짜 꼴을 요구하면 정상 값(14:00)이 걸린다.
TIME_ONLY_PROPS = frozenset({"doorTime", "checkinTime", "checkoutTime"})
# 구글 문서가 속성별로 'fully-qualified URL'·'valid URL' 을 요구하는 것들. JSON-LD 문법으로는
# 상대 주소도 되지만 구글은 속성마다 전체 주소를 적으라고 한다(일반 규칙으로 적힌 문장은 없다 —
# Article 문서 "Use valid URLs for the url or sameAs properties", LocalBusiness menu "the
# fully-qualified URL of the menu", Carousel item.url "The fully-qualified URL").
URL_PROPS = ("url", "sameAs", "logo", "image", "contentUrl", "embedUrl", "thumbnailUrl", "menu",
             "hasMenu", "hasMap", "item", "license", "acquireLicensePage",
             "mainEntityOfPage", "target", "urlTemplate", "discussionUrl", "downloadUrl", "installUrl",
             "acceptedAnswerUrl", "codeRepository")
# 구글 merchant listing 이 받는 availability 값 — 전체 주소든 짧은 이름이든.
# https://developers.google.com/search/docs/appearance/structured-data/merchant-listing
AVAILABILITY = frozenset({"BackOrder", "Discontinued", "InStock", "InStoreOnly", "LimitedAvailability",
                          "OnlineOnly", "OutOfStock", "PreOrder", "PreSale", "SoldOut"})
_PRODUCT_IDS = ("gtin", "gtin8", "gtin12", "gtin13", "gtin14", "isbn", "mpn")
_PRODUCT = frozenset({"Product", *schema_vocab.descendants("Product")})
_APP = frozenset({"SoftwareApplication", *schema_vocab.descendants("SoftwareApplication")})
_LOCAL = frozenset({"LocalBusiness", *schema_vocab.descendants("LocalBusiness")})
_ORG = frozenset({"Organization", *schema_vocab.descendants("Organization")})
_FOOD = frozenset({"FoodEstablishment", *schema_vocab.descendants("FoodEstablishment")})
# 더 좁은 하위 유형이 있는 일반형 — 구글 LocalBusiness 문서: "Use the most specific LocalBusiness
# sub-type possible". 유형을 여럿 단 노드는 전부 일반형일 때만 말한다.
GENERIC_LOCAL_TYPES = frozenset({"LocalBusiness", "MedicalBusiness"})
# schema.org 가 스스로 쓰지 말라고 적은 유형·속성 → 대신 쓸 것.
#   https://schema.org/Attorney — "This type is deprecated - LegalService is more inclusive and less ambiguous."
#   https://schema.org/branchOf — SupersededBy: parentOrganization
DEPRECATED_TYPES = {"Attorney": "LegalService"}
SUPERSEDED_PROPS = {"branchOf": "parentOrganization"}
GEO_MIN_DECIMALS = 5          # 구글 LocalBusiness: "The precision must be at least 5 decimal places."
PRICE_RANGE_MAX = 100         # 구글 LocalBusiness: "This field must be shorter than 100 characters."

# 코드 → (수준, 문장). 수준: bad(그 리치 결과에 못 나간다) · warn(값이 틀렸다) · note(알아 둘 것 —
# 진단이 아니라 정보 줄). merchant: 판매 페이지(상점 목록 결과)에만 해당 — 부르는 쪽이 사이트
# 타입으로 가른다(MERCHANT_CODES). 문장의 {type}·{prop}·{to} 는 describe 가 채운다.
ISSUES = {
    "no_context": ("bad", "{type} 블록에 @context 가 없습니다. 어느 어휘인지 몰라 통째로 안 읽힙니다"),
    "bad_context": ("bad", "{type} 블록의 @context 가 schema.org 를 가리키지 않습니다"),
    "placeholder": ("bad", "{type} 의 {prop} 값이 템플릿 자리표시 그대로입니다"),
    "relative_url": ("warn", "{type} 의 {prop} 가 전체 주소(https://…)가 아닙니다"),
    "bad_date": ("warn", "{type} 의 {prop} 가 ISO 8601 날짜(2026-03-02 또는 2026-03-02T10:00:00+09:00)가 아닙니다"),
    "price_not_number": ("bad", "{type} 의 {prop} 가 숫자가 아닙니다(통화 기호·쉼표·단위가 섞였습니다)"),
    "no_currency": ("bad", "{type} 에 price 는 있는데 priceCurrency 가 없습니다"),
    "bad_currency": ("bad", "{type} 의 {prop} 가 ISO 4217 세 글자 통화 코드(USD·KRW·EUR)가 아닙니다"),
    "bad_availability": ("warn", "{type} 의 {prop} 가 구글이 받는 재고 값이 아닙니다(값 하나만, InStock·OutOfStock 등)"),
    "app_no_price": ("bad", "{type} 의 offers 에 price 가 없습니다(무료 앱이면 0)"),
    "geo_precision": ("warn", "{type} 의 {prop} 가 소수 5자리 미만입니다"),
    "price_range_long": ("warn", "{type} 의 priceRange 가 100자 이상이라 가격대가 표시되지 않습니다"),
    "self_review": ("warn", "{type} 에 자기 자신에 대한 별점·리뷰({prop})가 달려 있습니다"),
    "merchant_aggregate_offer": ("bad", "{type} 의 offers 가 AggregateOffer 입니다. 상점 목록 결과는 Offer 만 받습니다"),
    "merchant_no_price": ("bad", "{type} 의 offers 에 price 가 없습니다"),
    "merchant_price_zero": ("bad", "{type} 의 {prop} 가 0 이하입니다. 상점 목록 결과는 0 보다 큰 값만 받습니다"),
    "merchant_no_image": ("bad", "{type} 에 image 가 없습니다"),
    "merchant_no_id": ("warn", "{type} 에 상품 식별자(gtin·mpn·isbn)가 없습니다"),
    "merchant_no_shipping": ("warn", "{type} 의 offers 에 shippingDetails 가 없습니다"),
    "merchant_no_returns": ("warn", "{type} 의 offers 에 hasMerchantReturnPolicy 가 없습니다"),
    "generic_local_type": ("note", "{type} 은 일반형입니다. 업종에 맞는 더 좁은 하위 유형이 있으면 그것을 씁니다"),
    "deprecated_type": ("note", "{type} 은 schema.org 가 쓰지 말라고 적은 유형입니다. 대신 {to}"),
    "superseded_prop": ("note", "{type} 의 {prop} 는 schema.org 에서 {to} 로 바뀐 속성입니다"),
    "no_menu": ("note", "{type} 에 메뉴 주소(menu·hasMenu)가 없습니다"),
}
MERCHANT_CODES = frozenset(c for c in ISSUES if c.startswith("merchant_"))
MAX_ISSUES = 40               # 한 페이지에 남기는 상한(같은 실수가 상품 200개에 반복되는 목록 페이지)


def describe(issue: dict) -> tuple[str, str]:
    """이슈 한 줄 → (수준, 문장). 모르는 코드는 ('note', 코드 그대로) — 옛 행이 새 코드보다 오래 산다."""
    level, text = ISSUES.get(issue.get("code"), ("note", str(issue.get("code") or "")))
    return level, text.format(type=issue.get("type") or "마크업", prop=issue.get("prop") or "",
                              to=issue.get("to") or "")


# ── 훑기 ────────────────────────────────────────────────────────────────────


def type_names(node: dict) -> list[str]:
    """@type → schema.org 이름 목록. 'schema:Product'·'https://schema.org/Product' → 'Product'."""
    t = node.get("@type")
    out = []
    for x in ([t] if isinstance(t, str) else t if isinstance(t, list) else []):
        if not isinstance(x, str):
            continue
        x = x.strip()
        if x.startswith("schema:"):
            x = x[len("schema:"):]
        elif "://" in x:
            x = x.rstrip("/").rsplit("/", 1)[-1]
        out.append(x)
    return out


def _host(url: str) -> str:
    h = urlsplit(url if "://" in (url or "") else "//" + (url or "")).netloc.lower()
    return h[4:] if h.startswith("www.") else h


def _listed(v) -> list:
    return v if isinstance(v, list) else [] if v is None else [v]


def _offer_issues(written: str, node: dict, add, *, app: bool) -> None:
    offers = [o for o in _listed(node.get("offers")) if isinstance(o, dict)]
    for o in offers:
        spec = o.get("priceSpecification")
        spec = spec[0] if isinstance(spec, list) and spec else spec
        spec = spec if isinstance(spec, dict) else {}
        in_spec = "price" not in o and "price" in spec
        price = spec.get("price") if in_spec else o.get("price")
        p_prop = "offers.priceSpecification.price" if in_spec else "offers.price"
        cur = o.get("priceCurrency") or spec.get("priceCurrency")
        aggregate = "AggregateOffer" in type_names(o)
        if aggregate and not app:
            add("merchant_aggregate_offer", "offers")
        if price in (None, ""):
            if app:
                add("app_no_price", "offers.price")
            elif not aggregate:
                add("merchant_no_price", "offers.price")
        elif not is_placeholder(price):                       # 자리표시는 그 규칙이 따로 말한다
            n = price_number(price)
            if n is None:
                add("price_not_number", p_prop)
            elif n <= 0 and not app:
                add("merchant_price_zero", p_prop)
            if cur in (None, "") and not (app and n == 0):    # 무료 앱(0)은 통화가 필요 없다
                add("no_currency", "offers.priceCurrency")
        if cur not in (None, "") and not is_placeholder(cur) \
                and not (isinstance(cur, str) and re.fullmatch(r"[A-Z]{3}", cur.strip())):
            add("bad_currency", "offers.priceCurrency")
        av = o.get("availability")
        if av not in (None, ""):
            name = av.strip().rstrip("/").rsplit("/", 1)[-1] if isinstance(av, str) else ""
            if name not in AVAILABILITY:
                add("bad_availability", "offers.availability")
        if not app and not aggregate:
            if not o.get("shippingDetails"):
                add("merchant_no_shipping", "offers.shippingDetails")
            if not o.get("hasMerchantReturnPolicy"):
                add("merchant_no_returns", "offers.hasMerchantReturnPolicy")


def value_issues(data, page_url: str = "") -> list[dict]:
    """파싱된 JSON-LD(블록 하나 또는 블록 목록) → 값 이슈 목록. 같은 (유형·속성·코드)는 한 번.

    page_url: 이 마크업이 놓인 페이지 — 자기 리뷰 판정에만 쓴다(노드의 url 이 같은 호스트인가).
    """
    out: list[dict] = []
    seen: set = set()

    def emit(written: str, code: str, prop: str = "", **more) -> None:
        k = (written, prop, code)
        if k not in seen and len(out) < MAX_ISSUES:
            seen.add(k)
            out.append({"type": written, "prop": prop, "code": code, **more})

    def walk(node, parent_type: str, top: bool) -> None:
        if isinstance(node, list):
            for v in node:
                walk(v, parent_type, top)
            return
        if not isinstance(node, dict):
            return
        types = type_names(node)
        written = types[0] if types else parent_type

        def add(code: str, prop: str = "", **more) -> None:
            emit(written, code, prop, **more)

        if top and (types or "@graph" in node):
            if "@context" not in node:
                add("no_context")
            elif not context_ok(node["@context"]):
                add("bad_context")
        for prop, v in node.items():
            if prop.startswith("@"):
                continue
            for x in _listed(v):
                if not isinstance(x, str):
                    continue
                if is_placeholder(x, _host(page_url) if page_url else ""):
                    add("placeholder", prop)
                elif prop in TIME_ONLY_PROPS and ISO_TIME.match(x.strip()):
                    pass                                   # 시각만 받는 속성의 정상 값(14:00)
                elif prop in DATE_PROPS and x.strip() and not is_iso8601(x):
                    add("bad_date", prop)
                elif prop in URL_PROPS and x.strip() and not is_absolute_url(x):
                    add("relative_url", prop)
            if prop in SUPERSEDED_PROPS and v not in (None, "", [], {}):
                add("superseded_prop", prop, to=SUPERSEDED_PROPS[prop])
        for t in types:
            if t in DEPRECATED_TYPES:
                add("deprecated_type", to=DEPRECATED_TYPES[t])
        ts = set(types)
        if ts & _PRODUCT:
            if node.get("offers") not in (None, "", [], {}):
                _offer_issues(written, node, add, app=False)
                if not node.get("image"):
                    add("merchant_no_image", "image")
                if not any(node.get(p) for p in _PRODUCT_IDS):
                    add("merchant_no_id")
        elif ts & _APP and node.get("offers") not in (None, "", [], {}):
            _offer_issues(written, node, add, app=True)
        if ts & _ORG:
            rated = [p for p in ("aggregateRating", "review") if node.get(p) not in (None, "", [], {})]
            if rated:
                u = node.get("url")
                own = (_host(u) == _host(page_url)) if isinstance(u, str) and u.strip() and page_url \
                    else None
                add("self_review", "·".join(rated), own=own)
        if ts & _LOCAL:
            if ts <= GENERIC_LOCAL_TYPES:
                add("generic_local_type")
            geo = node.get("geo")
            for g in (x for x in _listed(geo) if isinstance(x, dict)):
                for p in ("latitude", "longitude"):
                    d = decimals(g.get(p))
                    if d is not None and d < GEO_MIN_DECIMALS:
                        add("geo_precision", f"geo.{p}")
            pr = node.get("priceRange")
            if isinstance(pr, str) and len(pr) >= PRICE_RANGE_MAX:
                add("price_range_long", "priceRange")
            if ts & _FOOD and not (node.get("menu") or node.get("hasMenu")):
                add("no_menu")
        for prop, v in node.items():
            if prop != "@context" and isinstance(v, (dict, list)):
                walk(v, written, False)

    for block in (data if isinstance(data, list) else [data]):
        walk(block, "", True)
    return out


def _selfcheck() -> None:
    def codes(obj, url="https://me.example/p", merchant=True):
        got = value_issues(obj, url)
        return sorted({(i["code"], i["prop"]) for i in got if merchant or i["code"] not in MERCHANT_CODES})

    C = "https://schema.org"
    # 낱 규칙
    assert is_iso8601("2026-03-02") and is_iso8601("2026-03-02T10:00:00+09:00") and is_iso8601("2026")
    assert is_iso8601("2026-03-02T10:00Z") and is_iso8601("2026-03-02T10:00:00.5+0900")
    for bad in ("March 2, 2026", "2026/03/02", "02-03-2026", "2026-13-01", "2026-03-02 10:00", ""):
        assert not is_iso8601(bad), bad
    assert is_absolute_url("https://a.b/c") and not is_absolute_url("/logo.png")
    assert not is_absolute_url("//cdn.a/x.png") and not is_absolute_url("logo.png")
    for ok in ("https://schema.org", "http://schema.org/", "https://www.schema.org",
               ["https://schema.org", {"x": "y"}], {"@vocab": "https://schema.org/"}):
        assert context_ok(ok), ok
    for bad in ("schema.org", "https://schema.com", "", None, "https://json-ld.org"):
        assert not context_ok(bad), bad
    for ph in ("[Your Company Name]", "{{ site.title }}", "%SITE_NAME%", "https://example.com/logo.png",
               "Lorem ipsum dolor", "YOUR_BUSINESS", "Insert phone number here", "${price}"):
        assert is_placeholder(ph), ph
    for real in ("Acme Dental Clinic", "https://acme-example-shop.kr/a", "[2026] 가격 안내", "30% 할인",
                 "밀리아 제거 비용"):
        assert not is_placeholder(real), real
    assert price_number("29.99") == 29.99 and price_number(1200) == 1200.0
    assert price_number("$29.99") is None and price_number("1,299.00") is None and price_number("29,99") is None
    # 숫자 원문 — 끝의 0 을 지킨다(37.56780 은 5자리로 적은 것이다)
    geo = loads('{"latitude": 37.56780, "longitude": 126.97}')
    assert decimals(geo["latitude"]) == 5 and decimals(geo["longitude"]) == 2
    assert decimals(37.5678) is None and decimals("37.56780") == 5 and decimals(37) == 0

    # 상품 — 상점 목록 수준. 통화 기호 든 가격·통화 없음·AggregateOffer·식별자 없음
    bad_product = {"@context": C, "@type": "Product", "name": "p",
                   "offers": {"@type": "Offer", "price": "$29.99",
                              "availability": "https://schema.org/Available"}}
    got = codes(bad_product)
    assert ("price_not_number", "offers.price") in got and ("no_currency", "offers.priceCurrency") in got, got
    assert ("bad_availability", "offers.availability") in got and ("merchant_no_image", "image") in got, got
    assert ("merchant_no_id", "") in got and ("merchant_no_shipping", "offers.shippingDetails") in got, got
    good_product = {"@context": C, "@type": "Product", "name": "p", "image": "https://me.example/p.png",
                    "gtin13": "8801234567890",
                    "offers": {"@type": "Offer", "price": "29.99", "priceCurrency": "USD",
                               "availability": "InStock", "shippingDetails": {"@type": "OfferShippingDetails"},
                               "hasMerchantReturnPolicy": {"@type": "MerchantReturnPolicy"}}}
    assert codes(good_product) == [], codes(good_product)
    assert codes({**good_product, "offers": {**good_product["offers"], "price": 0}}) \
        == [("merchant_price_zero", "offers.price")]
    assert ("merchant_aggregate_offer", "offers") in codes(
        {**good_product, "offers": {"@type": "AggregateOffer", "lowPrice": 1, "priceCurrency": "USD"}})
    assert ("bad_currency", "offers.priceCurrency") in codes(
        {**good_product, "offers": {**good_product["offers"], "priceCurrency": "$"}})
    # priceSpecification 안의 값도 같은 규칙
    spec = {**good_product, "offers": {**{k: v for k, v in good_product["offers"].items()
                                          if k not in ("price", "priceCurrency")},
                                       "priceSpecification": {"price": "1,200", "priceCurrency": "KRW"}}}
    assert codes(spec) == [("price_not_number", "offers.priceSpecification.price")], codes(spec)
    # 리뷰만 단 상품(offers 없음)은 상점 목록 대상이 아니다 — 가격 규칙을 안 댄다
    assert codes({"@context": C, "@type": "Product", "name": "p",
                  "aggregateRating": {"@type": "AggregateRating", "ratingValue": 4}}) == []
    # 판매 페이지가 아닌 사이트는 merchant_ 를 뺀 것만 본다(부르는 쪽이 가른다)
    assert codes(bad_product, merchant=False) == [("bad_availability", "offers.availability"),
                                                  ("no_currency", "offers.priceCurrency"),
                                                  ("price_not_number", "offers.price")]
    # 앱 — offers.price 필수, 무료(0)는 통화 없이 된다
    app = {"@context": C, "@type": "MobileApplication", "name": "a", "offers": {"@type": "Offer"}}
    assert codes(app) == [("app_no_price", "offers.price")], codes(app)
    assert codes({**app, "offers": {"@type": "Offer", "price": 0}}) == []
    assert codes({**app, "offers": {"@type": "Offer", "price": "4.99"}}) == [("no_currency", "offers.priceCurrency")]

    # 자리표시·상대 주소·날짜·@context
    art = {"@type": "Article", "headline": "[Your Title Here]", "image": "/img/a.png",
           "datePublished": "March 2, 2026", "dateModified": "2026-03-02",
           "publisher": {"@type": "Organization", "name": "n", "logo": "logo.png"}}
    got = codes(art)
    assert got == [("bad_date", "datePublished"), ("no_context", ""), ("placeholder", "headline"),
                   ("relative_url", "image"), ("relative_url", "logo")], got
    assert codes({"@context": "https://schema.com", "@type": "Thing"}) == [("bad_context", "")]
    assert codes({"@context": "http://schema.org", "@type": "Thing"}) == [], "http @context 를 잡는다"
    # @graph 안의 노드는 바깥 @context 를 쓴다 — 노드마다 없다고 하지 않는다
    assert codes({"@context": C, "@graph": [{"@type": "WebSite", "url": "https://me.example/"}]}) == []
    assert codes({"@graph": [{"@type": "WebSite"}]}) == [("no_context", "")]

    # 지역 업체 — 일반형·좌표 자릿수·가격대 길이·메뉴·바뀐 속성·쓰지 말라는 유형·자기 리뷰
    lb = loads('{"@context":"https://schema.org","@type":"LocalBusiness","name":"n","url":"https://me.example/",'
               '"geo":{"@type":"GeoCoordinates","latitude":37.56,"longitude":126.97800},'
               '"priceRange":"' + "₩" * 100 + '","branchOf":{"@type":"Organization","name":"본사"},'
               '"aggregateRating":{"@type":"AggregateRating","ratingValue":4.9,"reviewCount":10}}')
    got = value_issues(lb, "https://www.me.example/about")
    by = {i["code"]: i for i in got}
    assert set(by) == {"generic_local_type", "geo_precision", "price_range_long", "superseded_prop",
                       "self_review"}, sorted(by)
    assert by["geo_precision"]["prop"] == "geo.latitude", "5자리로 적은 경도까지 잡는다"
    assert by["self_review"]["own"] is True and by["superseded_prop"]["to"] == "parentOrganization"
    assert value_issues({"@context": C, "@type": "Restaurant", "name": "n", "url": "https://other.example/",
                         "review": {"@type": "Review"}}, "https://me.example/x")[0]["own"] is False
    assert [i["code"] for i in value_issues({"@context": C, "@type": "Restaurant", "name": "n"})] == ["no_menu"]
    assert value_issues({"@context": C, "@type": "Restaurant", "hasMenu": "https://me.example/menu"}) == []
    assert [(i["code"], i["to"]) for i in value_issues({"@context": C, "@type": "Attorney"})] \
        == [("deprecated_type", "LegalService")]
    assert value_issues({"@context": C, "@type": ["LocalBusiness", "Dentist"], "name": "n"}) == []
    # 값은 어디에도 안 실린다
    assert "본사" not in json.dumps(got, ensure_ascii=False) and "37.56" not in json.dumps(got)
    # 문장 — 코드마다 수준·문장이 있고, 틀의 자리가 다 채워진다
    for i in got + value_issues(bad_product) + value_issues(art) + value_issues({"@type": "Attorney"}):
        level, text = describe(i)
        assert level in ("bad", "warn", "note") and "{" not in text and text, (i, text)
    assert describe({"code": "새코드"}) == ("note", "새코드")
    # 상한 — 상품 200개가 같은 실수를 해도 줄은 (유형·속성·코드)마다 한 번
    many = {"@context": C, "@graph": [dict(bad_product, name=f"p{i}") for i in range(200)]}
    assert len(value_issues(many)) == len(value_issues(bad_product)) - 0, "같은 이슈를 노드마다 센다"

    # 내용 지문 — 열쇠 순서·빈칸은 내용이 아니고, 값 하나가 바뀌면 지문이 바뀐다
    a = loads('{"@type":"Product","name":"p","offers":{"price":"10.00"}}')
    b = loads('{ "offers" : {"price":"10.00"}, "name":"p", "@type":"Product" }')
    c = loads('{"@type":"Product","name":"p","offers":{"price":"12.00"}}')
    assert schema_hash([a]) == schema_hash([b]) != schema_hash([c]) and len(schema_hash([a])) == 16
    assert schema_hash([]) == "" and schema_hash([None]) == ""
    print("schema_values self-check ok")


if __name__ == "__main__":
    _selfcheck()
