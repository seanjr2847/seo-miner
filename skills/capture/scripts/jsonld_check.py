"""JSON-LD **값** 검사 — create 의 커밋 전 검사가 쓰는 꼴. 순수 함수, stdlib + schema_values.

규칙(자리표시·ISO 8601·절대 주소·@context·날짜/주소 속성 목록)의 정본은 schema_values 다 —
수집 쪽(collect_page → page_audits.schema_issues_json)과 같은 함수를 부른다. 여기는 그 판정을
훅이 말하기 좋은 꼴({rule, level, path, value, msg})로 옮기고, 커밋을 막을지(level)만 정한다.
유형별로 **빠진 속성**은 여기 일이 아니다 — 그건 scoring.SCHEMA_RULES 와 collect_page 가 본다.

  context      @context 가 없거나 schema.org 가 아니다                      (error)
  placeholder  자리표시가 그대로 남았다([Business Name]·{{ x }}·TODO·example.com)  (error)
  date         날짜 속성이 ISO 8601 이 아니거나 없는 날짜다                  (error)
  timezone     날짜+시각인데 시간대가 없다                                   (warn)
  relative_url URL 속성 값이 절대 주소가 아니다                              (warn)

근거(2026-11-24 공식 문서 확인):
  날짜    Article 문서가 datePublished·dateModified 를 "in ISO 8601 format" 으로 적고 시간대는
          "We recommend that you provide timezone information" 이다 — 그래서 꼴은 error, 시간대는 warn.
          https://developers.google.com/search/docs/appearance/structured-data/article
  @context schema.org FAQ: "both 'https://schema.org' and 'http://schema.org' are fine." — http 를
          틀렸다고 하지 않는다. https://schema.org/docs/faq.html
  자리표시 "Your structured data must be a true representation of the page content."
          https://developers.google.com/search/docs/appearance/structured-data/sd-policies
  상대 URL 구글 문서에 '절대 주소여야 한다'는 일반 문장은 **없다** — 속성별 'fully-qualified URL'
          요구만 있다. 그래서 커밋을 막지 않고 **warn** 으로만 둔다(우리 판단이다).
"""
import schema_values as sv

LEVELS = ("error", "warn")
RULES = ("context", "placeholder", "date", "timezone", "relative_url")

URL_PROPS = frozenset(sv.URL_PROPS)
DATE_PROPS = frozenset(sv.DATE_PROPS)
date_state = sv.date_state


def _issue(rule: str, level: str, path: str, value, msg: str) -> dict:
    return {"rule": rule, "level": level, "path": path,
            "value": value if isinstance(value, (int, float)) else str(value)[:160], "msg": msg}


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
    hit = sv.placeholder_kind(s)
    if hit:
        out.append(_issue("placeholder", "error", path, s, f"자리표시가 그대로 남았습니다({hit})"))
        return                      # 자리표시인 값에 날짜·주소 꼴까지 묻지 않는다 — 원인은 하나다
    if key in DATE_PROPS:
        if key in sv.TIME_ONLY_PROPS and sv.ISO_TIME.match(s):
            return
        st = sv.date_state(s)
        if st == "bad":
            out.append(_issue("date", "error", path, s,
                              "ISO 8601 날짜가 아닙니다(예: 2026-03-10 또는 2026-03-10T09:00:00+09:00)"))
        elif st == "no_tz":
            out.append(_issue("timezone", "warn", path, s, "날짜+시각에 시간대가 없습니다"))
    elif key in URL_PROPS and s and not sv.is_absolute_url(s):
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
        elif not sv.context_ok(top["@context"]):
            out.append(_issue("context", "error", (base + ".@context").lstrip("."),
                              top["@context"], "@context 가 schema.org 가 아닙니다"))
        _walk(top, base, None, out)
    return out


def has_errors(issues: list[dict]) -> bool:
    return any(i["level"] == "error" for i in issues)
