"""JSON-LD **값** 검사 — 파싱된 ld+json 하나를 받아 '값이 틀린 자리'를 낸다. 순수 함수, stdlib 만.

정본 함수는 `value_issues()` 하나다. 유형별로 **빠진 속성**은 여기 일이 아니다 — 그건
scoring.SCHEMA_RULES 와 collect_page 가 본다. 여기는 있는 값이 쓸 수 있는 값인지만 본다:

  context      @context 가 없거나 schema.org 가 아니다                      (error)
  placeholder  자리표시가 그대로 남았다([Business Name]·{{ x }}·TODO·example.com)  (error)
  date         날짜 속성이 ISO 8601 이 아니거나 없는 날짜다                  (error)
  timezone     날짜+시각인데 시간대가 없다                                   (warn)
  relative_url URL 속성 값이 절대 주소가 아니다                              (warn)

쓰는 쪽: create 의 커밋 전 검사(skills/create/scripts/check_jsonld.py). 수집 쪽(collect_page)이
같은 규칙을 쓰게 되면 이 함수를 부른다 — 규칙 사본을 만들지 않는다.

근거(2026-11-24 공식 문서 확인):
  날짜    Article 문서가 datePublished·dateModified 를 "in ISO 8601 format" 으로 적고 시간대는
          "We recommend that you provide timezone information" 이다 — 그래서 꼴은 error, 시간대는 warn.
          https://developers.google.com/search/docs/appearance/structured-data/article
  @context schema.org FAQ: "both 'https://schema.org' and 'http://schema.org' are fine." — http 를
          틀렸다고 하지 않는다. https://schema.org/docs/faq.html
  자리표시 "Your structured data must be a true representation of the page content."
          https://developers.google.com/search/docs/appearance/structured-data/sd-policies
          example.com·.net·.org 와 .example·.test·.invalid 는 문서용 예약 이름이다(RFC 2606).
  상대 URL 구글 문서에 '절대 주소여야 한다'는 문장은 **없다**(확인한 다섯 쪽 어디에도). 있는 말은
          "Image URLs must be crawlable and indexable"·"Use valid URLs" 뿐이다. JSON-LD 는 상대
          IRI 를 문서 주소에 맞춰 풀 수 있지만 그 풀이를 읽는 쪽마다 믿을 근거가 없어 **warn** 으로만
          둔다(우리 판단이다 — error 로 올리려면 1차 출처가 먼저다).
"""
import re
from datetime import date, datetime

LEVELS = ("error", "warn")
RULES = ("context", "placeholder", "date", "timezone", "relative_url")

_SCHEMA_ORG = re.compile(r"^https?://schema\.org/?$", re.I)

# 자리표시 — 언어를 타지 않는 **꼴**만 본다(낱말 목록을 언어마다 늘리지 않는다).
_PLACEHOLDER = (
    ("대괄호 자리표시", re.compile(r"^\s*\[[^\[\]]{1,80}\]\s*$")),          # [Business Name]·[확인 필요]
    ("대괄호 자리표시", re.compile(r"\[(?:your|insert|url|todo)\b[^\]]*\]", re.I)),
    ("템플릿 표현식", re.compile(r"\{\{.*?\}\}|\$\{[^}]*\}|<%.*?%>|\{%.*?%\}")),
    ("미완 표시", re.compile(r"\b(?:TODO|TBD|FIXME|XXX+|CHANGEME|REPLACE(?:_[A-Z]+)+|REPLACE ME|"
                          r"YOUR[_ ][A-Z_ ]+)\b")),
    ("채움 글", re.compile(r"lorem ipsum", re.I)),
    ("예약 도메인", re.compile(r"(?:^|[/@.])(?:example\.(?:com|net|org)|[a-z0-9-]+\.(?:example|test|invalid))"
                          r"(?=$|[/:?#])", re.I)),
)

# 값이 주소인 속성(schema.org 의 URL 값을 받는 것 중 마크업에 흔한 것). @id 는 뺀다 —
# '#organization' 같은 조각 식별자가 정상 용법이다.
URL_PROPS = frozenset({
    "url", "image", "logo", "sameAs", "contentUrl", "embedUrl", "thumbnailUrl", "item",
    "mainEntityOfPage", "target", "urlTemplate", "discussionUrl", "downloadUrl", "installUrl",
    "hasMap", "menu", "acceptedAnswerUrl", "license", "acquireLicensePage", "codeRepository",
})
DATE_PROPS = frozenset({
    "datePublished", "dateModified", "dateCreated", "uploadDate", "startDate", "endDate",
    "validFrom", "validThrough", "priceValidUntil", "datePosted", "expires", "birthDate",
    "deathDate", "foundingDate", "dissolutionDate", "releaseDate", "availabilityStarts",
    "availabilityEnds", "doorTime", "checkinTime", "checkoutTime", "dateIssued", "dateRead",
    "dateReceived", "dateSent", "arrivalTime", "departureTime", "scheduledTime", "orderDate",
})
# 시각만 받는 속성 — 날짜 꼴을 요구하면 정상 값(14:00)이 걸린다.
_TIME_ONLY = frozenset({"doorTime", "checkinTime", "checkoutTime"})

_ISO_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_ISO_YM = re.compile(r"^\d{4}(-(0[1-9]|1[0-2]))?$")                  # 2019 · 2019-07 (foundingDate 등)
_ISO_DT = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2})(?:[.,]\d+)?)?"
                     r"(Z|[+-]\d{2}(?::?\d{2})?)?$")
_ISO_TIME = re.compile(r"^\d{2}:\d{2}(?::\d{2})?(Z|[+-]\d{2}(?::?\d{2})?)?$")
_ABS = re.compile(r"^(?:https?://|mailto:|tel:|data:|urn:)", re.I)


def _issue(rule: str, level: str, path: str, value, msg: str) -> dict:
    return {"rule": rule, "level": level, "path": path,
            "value": value if isinstance(value, (int, float)) else str(value)[:160], "msg": msg}


def _context_ok(ctx) -> bool:
    """문자열·{"@vocab": …}·목록 어느 꼴이든 schema.org 를 가리키면 된다."""
    if isinstance(ctx, str):
        return bool(_SCHEMA_ORG.match(ctx.strip()))
    if isinstance(ctx, dict):
        return _context_ok(ctx.get("@vocab"))
    if isinstance(ctx, list):
        return any(_context_ok(c) for c in ctx)
    return False


def date_state(v) -> str:
    """'ok' | 'no_tz'(날짜+시각인데 시간대 없음) | 'bad'(ISO 8601 이 아니거나 없는 날짜)."""
    if not isinstance(v, str):
        return "bad"
    s = v.strip()
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
    return "ok" if _ISO_YM.match(s) else "bad"


def _placeholder(s: str) -> str | None:
    for name, rx in _PLACEHOLDER:
        if rx.search(s):
            return name
    return None


def _walk(node, path: str, key: str | None, out: list) -> None:
    if isinstance(node, dict):
        for k, v in node.items():
            _walk(v, f"{path}.{k}" if path else str(k), str(k), out)
        return
    if isinstance(node, list):
        for i, v in enumerate(node):
            _walk(v, f"{path}[{i}]", key, out)
        return
    if not isinstance(node, str):
        if key in DATE_PROPS and node is not None:
            out.append(_issue("date", "error", path, node, "날짜가 문자열(ISO 8601)이 아닙니다"))
        return
    s = node.strip()
    if key == "@context":
        return
    hit = _placeholder(s)
    if hit:
        out.append(_issue("placeholder", "error", path, s, f"자리표시가 그대로 남았습니다({hit})"))
        return                      # 자리표시인 값에 날짜·주소 꼴까지 묻지 않는다 — 원인은 하나다
    if key in DATE_PROPS:
        if key in _TIME_ONLY and _ISO_TIME.match(s):
            return
        st = date_state(s)
        if st == "bad":
            out.append(_issue("date", "error", path, s,
                              "ISO 8601 날짜가 아닙니다(예: 2026-03-10 또는 2026-03-10T09:00:00+09:00)"))
        elif st == "no_tz":
            out.append(_issue("timezone", "warn", path, s, "날짜+시각에 시간대가 없습니다"))
    elif key in URL_PROPS and s and not _ABS.match(s):
        # 주소 속성에 주소가 아닌 글이 올 수도 있다(image 가 ImageObject 이름 등) — 빈칸이 든 글은 건너뛴다.
        if " " not in s:
            out.append(_issue("relative_url", "warn", path, s,
                              "절대 주소(https://…)가 아닙니다"))


def value_issues(data) -> list[dict]:
    """파싱된 ld+json(객체 하나 또는 목록) → [{rule, level, path, value, msg}]. 없으면 [].

    @context 는 **맨 위 객체**에만 요구한다(@graph 안의 항목은 위의 것을 물려받는다)."""
    out: list[dict] = []
    tops = data if isinstance(data, list) else [data]
    for i, top in enumerate(tops):
        base = f"[{i}]" if isinstance(data, list) else ""
        if not isinstance(top, dict):
            out.append(_issue("context", "error", base or "$", top, "JSON-LD 가 객체가 아닙니다"))
            continue
        if "@context" not in top:
            out.append(_issue("context", "error", (base + ".@context").lstrip("."), "",
                              "@context 가 없습니다"))
        elif not _context_ok(top["@context"]):
            out.append(_issue("context", "error", (base + ".@context").lstrip("."),
                              top["@context"], "@context 가 schema.org 가 아닙니다"))
        _walk(top, base, None, out)
    return out


def has_errors(issues: list[dict]) -> bool:
    return any(i["level"] == "error" for i in issues)
