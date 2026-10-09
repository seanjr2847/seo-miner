#!/usr/bin/env python3
"""내 페이지 HTML 감사 — 화면이 "이 페이지의 무엇을 바꿔라"라고 말할 수 있게 하는 재료.

지금까지 이 스킬은 남의 판정(GSC·SERP)만 모았다. "CTR 이 기대의 절반"이라고
말할 수는 있어도 **지금 그 페이지의 title 이 무엇인지**는 몰라서, 처방이 늘
일반론에서 멈췄다("제목을 고치세요"). 이 수집기가 그 구멍을 메운다 — 내 페이지를
직접 한 번 가져와 title·설명·H1·본문 길이·구조화 데이터·canonical·robots 를 적는다.
판정은 여기서 하지 않는다: 무엇이 문제인지는 scoring.page_advice 가 답한다.

대상 URL: 기회에 걸린 검색어의 페이지 → 노출 상위 페이지 순. 고칠 자리부터 본다.

비용: 없다. 남의 API 가 아니라 내 사이트를 여는 것뿐이다. 대신 내 서버에 요청이
가므로 동시 개수를 fanout.LIMITS["own_site"] 로 묶고 일꾼마다 throttle(기본 0.5초)을 두며, 상한
(page_urls, 기본 40)을 넘지 않는다.

의존성: requests + stdlib html.parser. HTML 파서를 새로 들이지 않는다 —
여기서 필요한 것은 head 몇 줄과 태그 개수라 정규 파서 하나면 충분하다.

Usage:
  python collect_page.py --project NAME [--limit N] [--dry-run]
  python collect_page.py                                  # self-check
"""
import argparse
import json
import re
import sys
import threading
from datetime import date
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin

sys.path.insert(0, str(Path(__file__).parent))
import collector  # noqa: E402
import db  # noqa: E402
import fanout  # noqa: E402
import scoring  # noqa: E402
import serp_adapter  # noqa: E402

# 자기 신원을 밝힌다 — 내 사이트를 여는 것이라 브라우저 위장을 할 이유가 없고,
# 로그에서 이 요청이 무엇인지 알아볼 수 있어야 한다(차단 규칙을 만들 때도 그렇다).
UA = {"User-Agent": "seo-miner/page-audit (+https://github.com/seanjr2847/seo-miner)"}

SKIP_TEXT = {"script", "style", "noscript", "template", "svg", "title"}
MAX_HTML = 2_000_000        # 2MB 를 넘는 문서는 앞부분만 본다 (head 와 본문 초반이면 족하다)

# 본문을 자바스크립트가 그리는 페이지의 껍데기 id — 이것만으로는 판정하지 않는다
# (SSR 된 Next 페이지도 __next 를 쓴다). 본문이 얇을 때만 같이 본다.
_APP_ROOTS = {"root", "__next", "app", "___gatsby", "svelte", "q-app"}
# 본문이 이보다 얇으면서 껍데기 흔적이 있으면 "정적 HTML 로는 안 보이는 페이지" 로 본다.
JS_SHELL_WORDS = 120
JS_SHELL_SCRIPTS = 5

# ── 추출성 — "인용될 블록이 있는가" ──────────────────────────────────────────
# AI 는 페이지가 아니라 문단·표·목록을 뽑는다. 정적 HTML 로 싸게 셀 수 있는 것만 센다
# — 판정은 scoring.extract_advice 가 한다. 수치의 출처가 진짜인지 같은 것은 여기서 못
# 잰다(요청문 산출물로 돌린다).
#
# 메뉴·꼬리말의 <ul> 까지 세면 거의 모든 페이지가 "목록 있음"이 된다. 본문 밖 틀로
# 보는 자리다. <header> 는 <article>·<main> 안이면 글머리(H1·리드 문단)라 틀이 아니다.
# 본문에 박힌 영상 — 검색결과에 영상 칸이 서는 검색어에서 상위 글이 영상을 품는지 본다.
_VIDEO_SRC = re.compile(r"(youtube(-nocookie)?\.com|youtu\.be|vimeo\.com)", re.I)
_CHROME = {"nav", "footer", "aside", "form"}
# 이보다 짧은 <p> 는 문단이 아니라 줄 조각이다(바이라인·"공유하기"·빵부스러기) —
# 첫 문단으로 세면 "첫 문단 3단어"라는 헛진단이 난다.
LEAD_MIN_WORDS = 5
# 열린 <p> 를 닫는 블록 — HTML 은 </p> 를 빼먹어도 되고, 브라우저는 다음 블록에서 닫는다.
_P_BREAK = {"p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "table",
            "section", "article", "blockquote", "pre", "figure"}
# 질문형 H2 — **휴리스틱이다.** 물음표, 의문사, 한국어 의문 어미 셋 중 하나. 어미만
# 보면 "평가"·"하나" 같은 명사가 걸리므로 어미 목록은 짧게 두고 의문사로 보탠다.
# 틀려도 되는 쪽은 "질문으로 잘못 세기"다 — 판정(scoring.extract_advice)은 질문형이
# **0개**일 때만 말하므로, 과하게 세면 지적 하나를 덜 할 뿐 없는 문제를 만들지 않는다.
# 그래서 "왜곡"·"대중가요" 같은 드문 헛걸림은 감수하고, 놓치는 쪽을 줄인다.
_Q_WORDS_KO = ("무엇", "뭐", "왜", "어떻게", "어떤", "언제", "어디", "누구", "누가", "얼마", "몇")
_Q_ENDINGS_KO = ("까", "까요", "나요", "가요", "는지", "을지", "인가", "는가", "은가",
                 "니까", "습니까", "일까", "할까", "될까")
_Q_WORDS_EN = ("what", "why", "how", "when", "where", "who", "which", "can", "does", "do",
               "is", "are", "should", "will")


def _is_question(text: str) -> bool:
    """H2 한 줄이 질문 꼴인가 — 휴리스틱(위 목록 참고). 판정이 아니라 셈의 재료다."""
    t = " ".join((text or "").split())
    if not t:
        return False
    if "?" in t or "？" in t:
        return True
    low = t.lower()
    if low.split()[0] in _Q_WORDS_EN and len(low.split()) >= 3:
        return True
    if any(w in t for w in _Q_WORDS_KO):
        return True
    return t.rstrip(".!…·:").endswith(_Q_ENDINGS_KO)


def _date(raw: str) -> str | None:
    """'2026-08-21T09:00:00+09:00' → '2026-08-21'. 못 읽으면 원문을 짧게 남긴다 —
    지어내지 않고, 사람이 보면 아는 글자는 버리지 않는다."""
    v = (raw or "").strip()
    if not v:
        return None
    head = v[:10]
    if len(head) == 10 and head[4] == head[7] == "-" and head.replace("-", "").isdigit():
        return head
    return v[:40]


# ld+json 을 감싼 껍데기 — 옛 브라우저용 주석·CDATA. 구글은 벗겨 읽는데 그대로 json 에 넣으면
# 멀쩡한 블록이 '깨진 블록'(bad)으로 셌다.
_LD_WRAP = re.compile(r"^\s*(?:<!--|/\*\s*<!\[CDATA\[\s*\*/|//\s*<!\[CDATA\[|<!\[CDATA\[)"
                      r"|(?:-->|/\*\s*\]\]>\s*\*/|//\s*\]\]>|\]\]>)\s*$")


def _ld_load(blob: str):
    """ld+json 블록 하나 → 파이썬 값. 못 읽으면 ValueError(깨진 블록).

    strict=False — 문자열 안의 제어 문자(CMS 가 그대로 흘린 탭 등)는 문법 오류가 아니라고 본다.
    껍데기(주석·CDATA)는 앞뒤에서 벗긴다(겹쳐 쌀 수 있어 안 바뀔 때까지)."""
    s, prev = (blob or "").strip(), None
    while s != prev:
        prev, s = s, _LD_WRAP.sub("", s).strip()
    return json.loads(s, strict=False)


def _schema_dates(blob: str) -> tuple[str | None, str | None]:
    """ld+json 의 datePublished/dateModified. 깨진 JSON 이어도 글자로 건진다 —
    _schema_types 와 같은 규칙이다."""
    try:
        data = _ld_load(blob)
    except ValueError:
        def grab(key):
            m = re.search(r'"%s"\s*:\s*"([^"]+)"' % key, blob)
            return _date(m.group(1)) if m else None
        return grab("datePublished"), grab("dateModified")
    pub = mod = None
    stack = [data]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            pub = pub or (_date(cur["datePublished"])
                          if isinstance(cur.get("datePublished"), str) else None)
            mod = mod or (_date(cur["dateModified"])
                          if isinstance(cur.get("dateModified"), str) else None)
            stack += [v for v in cur.values() if isinstance(v, (dict, list))]
        elif isinstance(cur, list):
            stack += [v for v in cur if isinstance(v, (dict, list))]
    return pub, mod


def _author_name(v) -> str | None:
    """ld+json 의 author 값 하나 → 이름. 글자·{name}·그 목록 셋 다 온다."""
    if isinstance(v, str):
        return v.strip() or None
    if isinstance(v, dict):
        n = v.get("name")
        return (n.strip() or None) if isinstance(n, str) else None
    if isinstance(v, list):
        return next((n for n in map(_author_name, v) if n), None)
    return None


def _schema_author(blob: str) -> str | None:
    """ld+json 의 author(.name). 깨진 JSON 이어도 글자로 건진다 — _schema_dates 와 같은
    규칙이다. {"@type":"Person"} 처럼 이름 없는 author 는 저자 표시가 아니다."""
    try:
        data = _ld_load(blob)
    except ValueError:
        m = (re.search(r'"author"\s*:\s*\{[^{}]*?"name"\s*:\s*"([^"]+)"', blob)
             or re.search(r'"author"\s*:\s*"([^"]+)"', blob))
        return (m.group(1).strip() or None) if m else None
    stack = [data]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            if "author" in cur:
                n = _author_name(cur["author"])
                if n:
                    return n
            stack += [v for v in cur.values() if isinstance(v, (dict, list))]
        elif isinstance(cur, list):
            stack += [v for v in cur if isinstance(v, (dict, list))]
    return None


def _filled(v) -> bool:
    """속성이 '있다'고 칠 값인가 — 빈 글자·빈 목록·빈 객체는 없는 것이다."""
    if v is None:
        return False
    if isinstance(v, str):
        return bool(v.strip())
    if isinstance(v, (list, dict)):
        return bool(v)
    return True


def _has_prop(node: dict, prop: str) -> bool:
    """'a|b' 는 둘 중 하나면 있다(scoring.SCHEMA_RULES 의 표기)."""
    return any(_filled(node.get(p)) for p in prop.split("|"))


def _schema_scan(data, gaps: dict, same_as: list) -> None:
    """파싱된 ld+json 하나 → 규칙(scoring.SCHEMA_RULES)마다 빠진 속성을 gaps 에, 엔티티의
    sameAs 주소를 same_as 에 보탠다. 원문은 어디에도 남기지 않는다.

    문서 순서로 훑는다(앞에서부터) — sameAs 는 앞의 것부터 열어 보므로 순서가 뜻이 있다.
    data 는 한 페이지의 블록 전부(목록)여도 된다 — @id 참조는 블록을 넘어 잇는다.

    @id 는 합쳐 읽는다(구글도 그렇게 읽는다): 같은 @id 를 단 객체는 한 노드이고, 참조만 한
    객체({"@id":"#r"})가 놓인 자리가 그 노드가 놓인 자리다. 예전엔 @graph 에 따로 두고 @id 로
    이은 평점을 '맨 위의 평점'으로 쳐서 itemReviewed 가 빠졌다는 거짓 bad 를 세웠다.
    nested: 다른 노드의 속성 값으로 든 노드(@graph·최상위 목록 안은 아니다) — 그 안의 리뷰·평점은
    대상(itemReviewed)이 바깥 노드라 그 칸을 빠졌다고 하지 않는다.
    Person 은 저자만 본다 — 글(scoring.writes_page)의 author 로 직접 들었거나 그 @id 가 가리키는
    노드. 상품 리뷰의 author 는 고객이다. 사람마다 따로 본다(each — _authors 가 같은 사람을 접고
    글의 author 순서로 늘어놓는다).
    """
    # 1) 훑기 — 객체마다 (객체, nested). 같은 노드를 가리키는 열쇠는 @id, 없으면 객체 자체.
    def key(d: dict):
        i = d.get("@id")
        return ("id", i) if isinstance(i, str) and i.strip() else ("obj", id(d))

    seen: list[tuple[dict, bool]] = []
    queue: list[tuple[object, bool]] = [(data, False)]
    i = 0
    while i < len(queue):
        cur, nested = queue[i]
        i += 1
        if isinstance(cur, list):
            queue += [(v, nested) for v in cur]
            continue
        if not isinstance(cur, dict):
            continue
        seen.append((cur, nested))
        typed = "@type" in cur
        queue += [(v, nested if k == "@graph" else (typed or nested))
                  for k, v in cur.items() if isinstance(v, (dict, list))]
    # 2) @id 로 합치기 — 속성은 먼저 채워진 값이 이긴다, 유형은 모은다, 놓인 자리는 하나라도 안이면 안
    merged: dict = {}
    for d, nested in seen:
        m = merged.setdefault(key(d), {"props": {}, "types": [], "nested": False})
        m["nested"] = m["nested"] or nested
        t = d.get("@type")
        m["types"] += [x for x in ([t] if isinstance(t, str) else t if isinstance(t, list) else [])
                       if isinstance(x, str) and x not in m["types"]]
        for p, v in d.items():
            if p not in ("@id", "@type", "@graph", "@context") and not _filled(m["props"].get(p)):
                m["props"][p] = v
    def add_same_as(rule: str, node: dict) -> None:
        sa = node.get("sameAs")
        for u in ([sa] if isinstance(sa, str) else sa if isinstance(sa, list) else []):
            u = u.strip() if isinstance(u, str) else ""
            if u.startswith("http") and [rule, u] not in same_as:
                same_as.append([rule, u])

    for m in merged.values():
        hit = scoring.schema_rule_of(m["types"])
        if not hit or hit[0] == "Person":           # 저자는 아래에서 사람 단위로
            continue
        rule, written = hit
        node, nested = m["props"], m["nested"]
        need = [p for p in scoring.schema_props(rule, "required")
                if not (nested and p in scoring.SCHEMA_NESTED_OPTIONAL) and not _has_prop(node, p)]
        want = [p for p in scoring.schema_props(rule, "recommended") if not _has_prop(node, p)]
        g = gaps.setdefault(rule, {"type": rule, "as": written, "n": 0, "need": [], "want": None})
        g["n"] += 1
        # 필수는 노드 하나라도 빠지면 그 노드가 결과에 못 나간다(합집합). 권장은 엔티티를 보는
        # 자리라 같은 유형의 어느 노드에도 없는 것만(교집합) — 글의 publisher 처럼 이름만 단
        # Organization 이 함께 있어도 온전한 주인 노드가 있으면 빠졌다고 하지 않는다.
        g["need"] += [p for p in need if p not in g["need"]]
        g["want"] = want if g["want"] is None else [p for p in g["want"] if p in want]
        if rule in scoring.OWNER_TYPES:
            add_same_as(rule, node)
    # 3) 저자 — 사람마다 따로 본다(each). 사람마다 다른 엔티티라 한데 모으면(교집합) 한 명이라도
    # url 이 있는 순간 다른 저자의 빈칸이 사라진다. want 는 합집합이다.
    for written, node in _authors(merged, key):
        want = [p for p in scoring.schema_props("Person", "recommended") if not _has_prop(node, p)]
        g = gaps.setdefault("Person", {"type": "Person", "as": written, "n": 0, "need": [],
                                       "want": None, "each": []})
        g["n"] += 1
        g["each"].append(want)
        g["want"] = [p for p in scoring.schema_props("Person", "recommended")
                     if any(p in w for w in g["each"])]
        add_same_as("Person", node)


def _authors(merged: dict, key) -> list[tuple[str, dict]]:
    """글(scoring.writes_page)의 author 가 가리키는 Person → [(쓴 @type, 합친 속성)], 사람 하나에 한 줄.

    순서는 글 노드의 author 에 적힌 순서다(글이 여럿이면 훑은 순서의 글마다). 이름을 안 남기므로
    순번이 사람을 가리키는 유일한 수단이라, 읽는 사람이 마크업에서 보는 순서여야 한다 — 예전엔
    @graph 에 놓인 순서로 세서 [A, B] 중 B 가 빠졌는데 '1번째'라고 했다.
    같은 사람은 접는다 — 이름(대소문자·앞뒤 빈칸 무시)·url·sameAs 중 하나라도 겹치면 같은 사람이다.
    노드 단위로 셌더니 글 목록(CollectionPage 안 BlogPosting 8개)의 같은 inline 저자 'Kim' 이
    '저자 8명'이 되고 같은 줄이 여덟 번 섰다. 접은 사람의 속성은 합친다(한 자리에 url 이 있으면
    이어진 사람이다). 가를 표지(이름·url·sameAs)가 하나도 없는 저자끼리는 가를 수 없으니 한 사람으로
    친다. 이름은 접는 데만 쓰고 어디에도 남기지 않는다(원문 미저장)."""
    order: list = []
    for m in merged.values():
        if not scoring.writes_page(m["types"]):
            continue
        au = m["props"].get("author")
        for x in (au if isinstance(au, list) else [au]):
            k = key(x) if isinstance(x, dict) else None
            if k in merged and k not in order and \
                    (scoring.schema_rule_of(merged[k]["types"]) or ("",))[0] == "Person":
                order.append(k)

    def marks(node: dict) -> set:
        out = set()
        n = node.get("name")
        if isinstance(n, str) and n.strip():
            out.add(("name", " ".join(n.split()).casefold()))
        for p in ("url", "sameAs"):
            v = node.get(p)
            for u in ([v] if isinstance(v, str) else v if isinstance(v, list) else []):
                if isinstance(u, str) and u.strip():
                    out.add(("url", u.strip().rstrip("/")))
        return out or {("none",)}

    people: list[dict] = []                      # {"marks", "as", "props"} — author 순서
    for k in order:
        m = merged[k]
        mk = marks(m["props"])
        same = [p for p in people if p["marks"] & mk]
        if not same:
            people.append({"marks": mk, "as": scoring.schema_rule_of(m["types"])[1],
                           "props": dict(m["props"])})
            continue
        # 이 노드가 앞의 둘을 잇기도 한다(이름은 A 와, url 은 B 와) — 앞의 사람에 다 합친다
        head = same[0]
        for p in same[1:]:
            head["marks"] |= p["marks"]
            for prop, v in p["props"].items():
                if not _filled(head["props"].get(prop)):
                    head["props"][prop] = v
            people.remove(p)
        head["marks"] |= mk
        for prop, v in m["props"].items():
            if not _filled(head["props"].get(prop)):
                head["props"][prop] = v
    return [(p["as"], p["props"]) for p in people]


def _schema_gaps_out(gaps: dict, broken: int) -> list[dict]:
    """집계 → page_audits.schema_gaps_json. 빠진 것이 있는 유형만, 표(SCHEMA_RULES) 순서로.
    깨진 블록 수는 {"broken": n} 한 줄로 — 구글도 그 블록을 버리니 그 자체가 진단이다."""
    out = [{**g, "want": g["want"] or []} for r in scoring.SCHEMA_RULES
           if (g := gaps.get(r)) and (g["need"] or g["want"])]
    # (저자별 빈칸 each 는 사람 수 n 과 짝이다 — 빠진 것 없는 저자도 [] 로 자리를 지킨다)
    return out + ([{"broken": broken}] if broken else [])


class _Page(HTMLParser):
    """한 장에서 감사에 쓰는 것만 줍는다. 모르는 태그는 그냥 지나간다."""

    def __init__(self, base: str):
        super().__init__(convert_charrefs=True)
        self.base = base
        self.host = scoring.host_of(base)
        self.title = None
        self.meta_description = None
        self.robots = None
        self.canonical = None
        self.h1: list[str] = []
        self.h2: list[str] = []
        self.schema: list[str] = []
        self.ld_blocks: list = []                # 읽힌 ld+json 블록 — 끝에 한 번 검사(_schema_scan)
        self.schema_broken = 0                   # JSON 으로 안 읽히는 ld+json 블록 수
        self.internal = self.external = 0
        self.images = self.images_no_alt = 0
        self.viewport = None
        self.html_lang = None
        self.hreflang: list[list[str]] = []      # [[코드, 주소], ...] — 선언 순서 그대로
        self.published = None
        self.modified = None
        self.scripts = 0                         # JS 껍데기 판정의 재료
        self.app_root = False
        # 추출성 — 본문 틀(_CHROME) 밖의 것만, 겹친 것은 바깥 하나로 센다
        self.tables = self.lists = 0
        self.videos = 0                          # <video>·유튜브/비메오 iframe — 상위 글 형식 비교용
        self.author = None                       # meta author 또는 ld+json author.name
        self._h1_done = False                    # 첫 H1 을 지났나 — 리드 문단은 그 뒤가 본자리
        self._p: list[str] | None = None         # 지금 모으는 <p> 글자
        self._lead_before = self._lead_after = None
        self._text: list[str] = []
        self._stack: list[str] = []
        self._grab = None          # 지금 글자를 모으는 자리: "title" | "h1" | "h2" | "ld"

    # ── 태그 ────────────────────────────────────────────────
    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        self._stack.append(tag)
        # 태그 종류와 무관한 신호라 사슬 밖에서 본다 — 사슬 안에 두면 <a id="root">
        # 하나가 링크 집계를 통째로 삼킨다.
        if a.get("id", "").strip().lower() in _APP_ROOTS or "data-reactroot" in a:
            self.app_root = True
        # 추출성 — 이것도 사슬 밖이다(<p>·<table>·<ul> 은 아래 사슬의 어느 갈래와도
        # 안 겹치지만, 사슬에 끼우면 순서 하나로 집계가 조용히 빠진다).
        if self._p is not None and tag in _P_BREAK:
            self._end_p()                        # </p> 는 빼먹어도 되는 태그다
        if not self._in_chrome():
            if tag == "table" and self._stack.count("table") == 1:
                self.tables += 1
            elif tag in ("ul", "ol") and not any(t in ("ul", "ol") for t in self._stack[:-1]):
                self.lists += 1
            elif tag == "p" and self._lead_after is None:
                self._p = []
            if tag == "video" or (tag == "iframe" and _VIDEO_SRC.search(a.get("src", ""))):
                self.videos += 1
        if tag == "html":
            # 페이지의 언어 선언. 없으면 구글·빙이 본문 글자로 추측한다 — 다국어
            # 사이트에서 로케일 판정이 어긋나는 첫 자리다.
            self.html_lang = a.get("lang", "").strip() or None
        elif tag == "title" and self.title is None:
            self._grab, self._buf = "title", []
        elif tag in ("h1", "h2"):
            self._grab, self._buf = tag, []
        elif tag == "meta":
            name = (a.get("name") or a.get("property") or "").lower()
            content = a.get("content", "").strip()
            if name == "description" and self.meta_description is None:
                self.meta_description = content
            elif name == "robots":
                self.robots = content
            elif name == "viewport" and self.viewport is None:
                self.viewport = content
            elif name == "author" and content and not self.author:
                self.author = content
            elif name in ("article:published_time", "datepublished") and not self.published:
                self.published = _date(content)
            elif name in ("article:modified_time", "og:updated_time", "datemodified")                     and not self.modified:
                self.modified = _date(content)
        elif tag == "link":
            rel = a.get("rel", "").lower()
            if "canonical" in rel:
                self.canonical = urljoin(self.base, a.get("href", "").strip())
            elif "alternate" in rel and a.get("hreflang"):
                self.hreflang.append([a["hreflang"].strip(),
                                      urljoin(self.base, a.get("href", "").strip())])
        elif tag == "script":
            self.scripts += 1
            if a.get("type", "").lower() == "application/ld+json":
                self._grab, self._buf = "ld", []
        elif tag == "time" and a.get("datetime") and not self.published:
            self.published = _date(a["datetime"])
        elif tag == "a":
            href = a.get("href", "").strip()
            if href and not href.startswith(("#", "mailto:", "tel:", "javascript:")):
                h = scoring.host_of(urljoin(self.base, href))
                if not h or h == self.host or scoring.owns(h, self.host):
                    self.internal += 1
                else:
                    self.external += 1
        elif tag == "img":
            self.images += 1
            if not a.get("alt", "").strip():
                self.images_no_alt += 1

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if self._stack and self._stack[-1] == tag:
            self._stack.pop()

    def handle_endtag(self, tag):
        if tag == "p" and self._p is not None:
            self._end_p()
        if self._grab == tag or (self._grab == "ld" and tag == "script") \
                or (self._grab == "title" and tag == "title"):
            text = " ".join("".join(self._buf).split())
            if self._grab == "title":
                self.title = text
            elif self._grab == "ld":
                self.schema += _schema_types(text)
                # 속성 검사는 JSON 으로 읽히는 블록만 — 깨진 블록은 @type 을 글자로 건지는
                # 데서 멈추고(지금처럼 수집은 이어진다) 깨졌다는 사실만 센다. 검사는 블록을 다
                # 모은 뒤 audit_html 에서 한 번 — @id 참조가 블록을 넘어 잇는다.
                try:
                    self.ld_blocks.append(_ld_load(text))
                except ValueError:
                    self.schema_broken += 1
                pub, mod = _schema_dates(text)
                self.published = self.published or pub
                self.modified = self.modified or mod
                self.author = self.author or _schema_author(text)
            elif self._grab == "h1":
                self.h1.append(text)
                self._h1_done = True
            elif self._grab == "h2":
                self.h2.append(text)
            self._grab = None
        while self._stack:                       # 안 닫힌 태그가 있어도 스택이 안 샌다
            if self._stack.pop() == tag:
                break

    def handle_data(self, data):
        if self._grab:
            self._buf.append(data)
        if not (set(self._stack) & SKIP_TEXT):
            self._text.append(data)
            if self._p is not None:
                self._p.append(data)

    # ── 추출성 ──────────────────────────────────────────────
    def _in_chrome(self) -> bool:
        """지금 자리가 본문 밖 틀(메뉴·꼬리말·곁가지·폼)인가."""
        s = self._stack
        return (any(t in _CHROME for t in s)
                or ("header" in s and "article" not in s and "main" not in s))

    def _end_p(self) -> None:
        n = len(" ".join(self._p or []).split())
        self._p = None
        if n < LEAD_MIN_WORDS:
            return
        if self._h1_done:
            if self._lead_after is None:
                self._lead_after = n
        elif self._lead_before is None:
            self._lead_before = n

    def lead_words(self) -> int:
        """첫 본문 문단의 단어 수. H1 뒤의 첫 문단이 본자리고, H1 이 없거나 그 뒤에
        문단이 없으면 앞의 것을 쓴다. 0 = <p> 문단을 못 찾았다(본 결과다 — div 로만
        짠 페이지일 수 있어서 판정 쪽이 그렇게 말한다)."""
        if self._p is not None:                  # 문서 끝까지 안 닫힌 문단
            self._end_p()
        lead = self._lead_after if self._lead_after is not None else self._lead_before
        return lead or 0

    # ── 결과 ────────────────────────────────────────────────
    @property
    def words(self) -> int:
        """공백 기준 단어 수(한국어는 어절). 절대 기준이 아니라 얇음 판정용이다."""
        return len(" ".join(self._text).split())


def _schema_types(blob: str) -> list[str]:
    """ld+json 에서 @type 만. 깨진 JSON 이어도 수집을 멈추지 않는다 — 흔하다."""
    try:
        data = _ld_load(blob)
    except ValueError:
        return re.findall(r'"@type"\s*:\s*"([^"]+)"', blob)
    out: list[str] = []
    stack = [data]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            t = cur.get("@type")
            if isinstance(t, str):
                out.append(t)
            elif isinstance(t, list):
                out += [x for x in t if isinstance(x, str)]
            stack += [v for v in cur.values() if isinstance(v, (dict, list))]
        elif isinstance(cur, list):
            stack += [v for v in cur if isinstance(v, (dict, list))]
    return out


def audit_html(url: str, html: str, status: int | None = 200) -> dict:
    """HTML 한 장 → page_audits 한 줄. 네트워크를 타지 않는다(자체점검이 여기를 부른다)."""
    p = _Page(url)
    try:
        p.feed(html[:MAX_HTML])
    except Exception as e:                       # 망가진 마크업에도 지금까지 읽은 것은 남긴다
        print(f"  ! {url}: 파싱 도중 중단 ({e})", file=sys.stderr)
    words = p.words
    # 정적 HTML 로는 본문이 안 보이는 페이지 — 여기서 재는 title 말고 본문·H2·
    # 구조화 데이터는 전부 못 믿는 값이 된다. 판정이 아니라 **단서**라서 요청문이
    # "없음" 을 사실처럼 말하지 않게 하는 데만 쓴다.
    js_shell = int(words < JS_SHELL_WORDS
                   and (p.app_root or p.scripts >= JS_SHELL_SCRIPTS))
    gaps: dict = {}                              # 규칙 → 빠진 속성 집계
    same_as: list[list[str]] = []                # [[규칙, 주소], ...] — 수집 중에만 쓴다
    _schema_scan(p.ld_blocks, gaps, same_as)
    return {"url": url, "status": status, "error": None,
            "title": p.title, "meta_description": p.meta_description,
            "h1_json": json.dumps(p.h1, ensure_ascii=False),
            "h2_json": json.dumps(p.h2[:20], ensure_ascii=False),
            "words": words,
            "schema_json": json.dumps(sorted(set(p.schema)), ensure_ascii=False),
            # 유형별 필수·권장 속성 중 빠진 것(scoring.SCHEMA_RULES). "[]" = 봤고 빠진 것 없음.
            "schema_gaps_json": json.dumps(_schema_gaps_out(gaps, p.schema_broken),
                                           ensure_ascii=False),
            "canonical": p.canonical, "robots": p.robots,
            "internal_links": p.internal, "external_links": p.external,
            "images": p.images, "images_no_alt": p.images_no_alt,
            "viewport": p.viewport, "html_lang": p.html_lang,
            "hreflang_json": json.dumps(p.hreflang[:30], ensure_ascii=False),
            "published": p.published, "modified": p.modified,
            "js_shell": js_shell,
            # 추출성. 저자는 못 찾으면 "" 다 — NULL 은 이 칸이 생기기 전의 행(안 봤다)이다.
            # 질문형 H2 는 h2_json 과 같은 20개 안에서 센다(요청문이 "n/전체"로 나란히 쓴다).
            "tables": p.tables, "lists": p.lists,
            "h2_questions": sum(map(_is_question, p.h2[:20])),
            "lead_words": p.lead_words(), "author": p.author or "",
            # page_audits 칸은 아니다(write_page_audits 가 모르는 키는 버린다) — 상위 글의
            # 형식을 비교할 때(serp_outlines) 쓴다.
            "videos": p.videos,
            # 이것도 칸이 아니다 — 수집(collect)이 몇 곳만 열어 살아 있는지 보고 그 결과를
            # same_as_json 에 적는다. 주소 목록 자체는 원문이라 남기지 않는다.
            "same_as": same_as[:20]}


def target_urls(conn, project_id: int, limit: int) -> list[str]:
    """감사할 URL — 고칠 자리부터. 기회에 걸린 페이지 → 노출 상위 페이지 순.

    기회 대상이 URL 이면(색인 막힘 등) 그 자체가 대상이고, 검색어면 그 검색어로
    실제 걸린 페이지가 대상이다(scoring.pages_by_query 가 정본). 걸린 페이지가 없으면
    요청문이 고치라고 할 지면(scoring.topic_page)을 본다 — 안 보면 그 요청문은 늘
    "아직 점검하지 않았습니다"로 나간다.
    """
    rows = conn.execute(
        "SELECT kind, target FROM opportunities WHERE project_id=? AND status IN ('new','acked')"
        " ORDER BY score DESC LIMIT 100", (project_id,)).fetchall()
    targets = [r["target"] for r in rows]
    by_q = scoring.pages_by_query(
        conn, project_id, [t for t in targets if not t.startswith("http")], top=2)
    by_t = scoring.pages_by_topic(
        conn, project_id, [r["target"] for r in rows if r["kind"] in scoring.KEYWORD_KINDS
                           and not r["target"].startswith("http") and not by_q.get(r["target"])])
    opp: list[str] = []
    for t in targets:
        if t.startswith("http"):
            opp.append(t)
        else:
            # 서치콘솔 페이지가 없으면 추정 순위가 건 페이지, 그것도 없으면 주제 페이지
            opp += ([pg["page"] for pg in by_q.get(t, [])]
                    or [scoring.labs_page(conn, project_id, t) or scoring.topic_page(by_t.get(t))])
    # 기회에 걸린 페이지 중 **한 번도 안 본 것 → 가장 오래전에 본 것** 순. 점수 순만 쓰면
    # 상한(page_urls) 밖의 기회 페이지는 영영 안 보이고, 매 회차 같은 앞쪽만 다시 본다 —
    # 고치기 요청문 57장 중 25장이 "아직 점검하지 않았습니다"로 나갔다. 같은 날짜 안에서는
    # 점수 순을 지킨다(sorted 는 안정 정렬이다).
    last = {r["url"]: r["checked_date"] for r in db.latest_page_audits(conn, project_id)}
    opp = sorted(dict.fromkeys(u for u in opp if u), key=lambda u: last.get(u) or "")
    out = opp + scoring.top_pages(conn, project_id, limit)
    # 방금 없는 페이지(404·410)로 확인한 주소는 GONE_RECHECK_DAYS 동안 빼고 그 자리를 다른
    # 페이지에 준다 — 매일 같은 404 를 다시 여느라 상한 안의 자리를 잃었다(aitierlist 34곳 중
    # 5곳). 그 주소의 진단은 최신 행(404)이 그대로 갖고 있다(scoring.page_advice 가 말한다).
    gone = set(recent_gone(conn, project_id))
    row = conn.execute("SELECT domain FROM projects WHERE id=?", (project_id,)).fetchone()
    domain = (row[0] if row else "") or ""
    # 홈은 한 꼴만 연다 — 후보에서 먼저 나온 꼴(기회가 걸린 주소, 그다음 최신 서치콘솔 노출 순)이다.
    # 그 꼴은 요청문·화면이 그 글자 그대로 찾는 주소이기도 하다. 예전엔 후보에 www 홈과 비www 홈이
    # 둘 다 있으면 둘 다 열어(theotherskin 은 9/02부터 매일 두 행) 홈 엔티티 진단이 두 번 섰다.
    seen, uniq, took_home = set(gone), [], False
    for u in out:
        u = clean_url(u)
        if not u:
            continue
        is_home = bool(domain) and scoring.is_home(u, domain)
        if is_home and took_home:
            continue
        if u not in seen:
            seen.add(u)
            uniq.append(u)
            took_home = took_home or is_home
    uniq = uniq[:limit]
    # 홈 — 사이트 주인 엔티티(scoring.entity_advice)를 보는 자리. 검색어가 전부 글로 가는 사이트는
    # 노출 상위에 홈이 없어 한 번도 안 열렸다. 상한과 상관없이 상한 밖 한 자리로 넣는다(기회
    # 페이지의 자리를 뺏지 않는다). 예전엔 상한 안 마지막 자리를 받되, 후보에 있다가 상한에 잘린
    # 홈은 '이미 본 후보'로 쳐서 빠졌다 — 기회가 많은 사이트일수록 홈이 영영 안 열렸다.
    # 빼는 것은 최근에 본 홈과 방금 없는 페이지로 확인한 홈뿐이다.
    home = home_of(conn, project_id, domain, out + list(last))
    seen_at = max((str(d)[:10] for u, d in last.items() if d and scoring.is_home(u, domain)),
                  default="")
    fresh = bool(seen_at) and (date.today() - date.fromisoformat(seen_at)).days < HOME_RECHECK_DAYS
    if home and home not in gone and not fresh \
            and not any(scoring.is_home(u, domain) for u in uniq):
        uniq.append(home)
    return uniq


# 홈을 이 날수 안에 봤으면 다시 안 연다(엔티티 마크업은 자주 안 바뀐다).
HOME_RECHECK_DAYS = 7


def home_of(conn, project_id: int, domain: str, known=()) -> str | None:
    """이 사이트의 홈 주소 한 꼴 — www 유무·http(s)는 사이트가 실제로 쓰는 쪽을 따른다.

    도메인 칸('c.kr')에서 지으면 사이트가 www 일 때 'https://c.kr/' 과 'https://www.c.kr/' 을
    둘 다 열고 엔티티 진단이 두 번 섰다. 서치콘솔이 본 홈(노출 많은 꼴) → 이미 아는 주소(known:
    점검 후보·크롤한 주소) → 도메인에서 지은 꼴 순으로 고른다."""
    if not home_url(domain):
        return None
    host = scoring.host_of(domain)               # www 를 뗀 호스트 — LIKE 로 좁히고 is_home 로 가린다
    gsc = [r[0] for r in conn.execute(
        "SELECT page, SUM(impressions) imp FROM gsc_snapshots WHERE project_id=? AND page IS NOT NULL"
        " AND (page LIKE ? OR page LIKE ?) GROUP BY page ORDER BY imp DESC",
        (project_id, f"%{host}/", f"%{host}")) if scoring.is_home(r[0], domain)]
    pick = next((u for u in [*gsc, *known] if u and scoring.is_home(clean_url(u), domain)), None)
    return clean_url(pick) if pick else home_url(domain)


def home_url(domain: str) -> str | None:
    """projects.domain('c.kr'·'https://www.c.kr/x') → 'https://c.kr/'. 경로는 버린다 — 사이트
    주인은 도메인 첫 화면에서 본다(분석 범위 /kr/ko/ 가 있어도). www 는 그대로 둔다."""
    from urllib.parse import urlsplit
    d = (domain or "").strip()
    if not d:
        return None
    parts = urlsplit(d if "://" in d else "https://" + d)
    return f"{parts.scheme or 'https'}://{parts.netloc}/" if parts.netloc else None


# sameAs(엔티티가 "같은 주인"이라고 가리키는 공식 계정·프로필 주소)가 살아 있나 — 남의 사이트를
# 여는 일이라 몇 곳만, 회차마다 시간 상한 안에서 본다. 홈 사이트 주인의 것이 먼저, 그다음 저자.
SAME_AS_MAX = 6
SAME_AS_BUDGET = 20.0       # 초 — 한 회차에서 sameAs 확인에 쓰는 시간 전부
SAME_AS_TIMEOUT = 6         # 초 — 주소 하나


def check_link(url: str, timeout: float | None = None) -> int | None:
    """주소 하나의 HTTP 상태. 못 열면 None(모름 — 죽었다고 하지 않는다). 본문은 안 받는다."""
    import requests
    try:
        r = requests.get(url, headers=UA, timeout=timeout or SAME_AS_TIMEOUT,
                         allow_redirects=True, stream=True)
    except Exception:
        return None
    r.close()
    return r.status_code


def check_same_as(rows: list[dict], domain: str) -> int:
    """rows 의 sameAs 를 몇 곳 열어 그 결과를 행의 same_as_json 에 적는다 → 연 주소 수.

    죽었다(dead)는 404·410 뿐이다: 소셜 사이트는 봇에 403·429·로그인 벽을 주는 일이 흔해서
    그것을 죽은 주소로 읽으면 멀쩡한 공식 계정을 빼라고 시킨다. 안 연 주소는 안 적는다(안 봄).
    홈 행은 도메인으로 알아본다(scoring.is_home — www 유무 무관). 도메인에서 지은 주소와 글자가
    같은 행만 홈으로 쳤더니 www 사이트의 주인 sameAs 를 하나도 안 열었다.
    """
    import time

    def home(r) -> bool:
        return scoring.is_home(str(r.get("url") or ""), domain)
    first = [u for r in rows if home(r)
             for t, u in r.get("same_as") or [] if t in scoring.OWNER_TYPES]
    rest = [u for r in rows for t, u in r.get("same_as") or [] if t == "Person"]
    status: dict[str, int | None] = {}
    deadline = time.monotonic() + SAME_AS_BUDGET
    for u in list(dict.fromkeys(first + rest))[:SAME_AS_MAX]:
        left = deadline - time.monotonic()
        if left <= 0:
            break
        status[u] = check_link(u, timeout=min(SAME_AS_TIMEOUT, max(1.0, left)))
    for r in rows:
        own = [(t, u) for t, u in r.get("same_as") or []
               if u in status and (t == "Person" or home(r))]
        if own:
            r["same_as_json"] = json.dumps(
                [{"url": u, "of": t, "status": status[u], "dead": status[u] in GONE_STATUS}
                 for t, u in own], ensure_ascii=False)
    return len(status)


# 광고·추천 추적 꼬리표 — 같은 페이지인데 주소만 다르다. 기회·검색결과에서 온 주소에
# `?gclid=…`·`?ref=…` 가 붙은 채로 점검 대상이 되면 같은 페이지를 이름만 바꿔 여러 번 보고,
# 상한(page_urls) 안의 자리를 그만큼 잃는다(gucci: 10자리 중 둘이 홈의 꼬리표 사본이었다).
TRACKING_PARAMS = frozenset({"gclid", "gclsrc", "dclid", "fbclid", "msclkid", "yclid",
                             "ref", "ref_src", "_ga", "mc_cid", "mc_eid", "igshid"})


def clean_url(u: str) -> str:
    """추적 꼬리표(utm_*·gclid·ref …)와 #조각을 뗀다. 나머지 쿼리는 순서째 그대로 둔다 —
    ?page=2·?lang=ko 는 다른 페이지다."""
    if not u or "?" not in u and "#" not in u:
        return u
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
    parts = urlsplit(u)
    kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if not (k.lower() in TRACKING_PARAMS or k.lower().startswith("utm_"))]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(kept), ""))


# 없는 페이지 — 오류가 아니라 사실(collect 의 one 이 가른다)
GONE_STATUS = (404, 410)
# 없는 페이지로 확인한 주소를 다시 여는 간격(일). 되살아났거나 리다이렉트를 걸었는지는
# 이 간격으로 다시 본다 — 영영 빼면 고친 뒤에도 화면이 계속 404 라고 말한다.
GONE_RECHECK_DAYS = 7


def recent_gone(conn, project_id: int, days: int = GONE_RECHECK_DAYS) -> list[str]:
    """주소마다 최신 감사가 404·410 이고 그게 days 일 안인 주소."""
    return [r["url"] for r in db.latest_page_audits(conn, project_id)
            if r["status"] in GONE_STATUS and r["checked_date"]
            and (date.today() - date.fromisoformat(str(r["checked_date"])[:10])).days < days]


# 사이트가 **이 서버를** 막을 때의 모양 — 응답이 없거나(시간 초과·연결 거부, status 없음)
# 거절한다(403·429). 대형 브랜드 사이트(gucci.com)는 데이터센터 IP 를 봇으로 보고 응답을
# 안 준다. 이건 그 사이트 페이지의 결함이 아니라 우리가 못 본 것이다 — 페이지 점검·크롤이
# 이걸 "고칠 거리"(깨진 페이지·실패)로 적으면 없는 문제를 기회로 세우고 매 런 실패 메일을 낸다.
BLOCK_STATUS = frozenset({403, 429})


def blocked(status) -> bool:
    return status is None or status in BLOCK_STATUS


def blocked_note(host: str, n: int) -> str:
    """전부 막혔을 때의 사유 한 벌 — 페이지 점검·크롤이 같은 말을 한다."""
    return (f"{host} 이 이 서버의 요청에 응답하지 않습니다 — 주소 {n}개 전부 시간 초과·거부. "
            "봇 차단(방화벽)이거나 사이트가 내려가 있습니다. 막는 동안 이 단계는 비어 있고, "
            "우리 쪽에서 고칠 수 있는 문제가 아닙니다.")


_META_CHARSET = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?\s*([A-Za-z0-9_\-]+)""", re.I)


def html_text(r) -> str:
    """응답 본문을 글자로 — 헤더에 charset 이 없으면 본문의 <meta charset>, 그것도 없으면 내용 판정.

    requests 의 r.text 는 'text/html' 에 charset 이 없으면 ISO-8859-1 로 읽는다(RFC 2616 기본값).
    그래서 상위 글 개요가 'ë¥í° ì¹¼ë¼' 로 저장돼 요청문에 나갔다(cnpskin — 본문은 utf-8).
    우리 페이지 감사(fetch)와 크롤(collect_crawl.fetch)이 같은 길이라 둘 다 이걸 쓴다."""
    if "charset" in (r.headers.get("content-type") or "").lower():
        return r.text
    m = _META_CHARSET.search(r.content[:4096])
    for enc in ((m.group(1).decode("ascii", "ignore") if m else None),
                r.apparent_encoding, "utf-8"):
        if not enc:
            continue
        try:
            return r.content.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return r.content.decode("utf-8", errors="replace")


def fetch(url: str, timeout: int | None = None) -> dict:
    """URL 한 장. 실패도 한 줄로 남긴다 — "못 가져왔다"는 것 자체가 진단이다."""
    import requests
    try:
        r = requests.get(url, headers=UA, timeout=timeout or serp_adapter.TIMEOUTS["page"],
                         allow_redirects=True)
    except Exception as e:
        return {"url": url, "status": None, "error": f"{type(e).__name__}: {e}"[:200]}
    if r.status_code >= 400 or "html" not in (r.headers.get("content-type") or "").lower():
        return {"url": url, "status": r.status_code,
                "error": f"HTTP {r.status_code} · {r.headers.get('content-type', '?')}"}
    return audit_html(url, html_text(r), r.status_code)


# ── 막힌 페이지 대신 읽기 — DataForSEO On-Page content_parsing ────────────────
# 데이터센터 IP 를 봇으로 보는 사이트(gucci.com)는 fetch 가 한 장도 못 본다. 그때 DataForSEO
# 가 대신 열어 본문 구조(제목·H1·H2·단어 수)를 준다. 이번 달 할 일(plays)의 증거 읽기와 이
# 단계가 **같은 함수**를 쓴다 — 두 벌이면 한쪽만 응답 꼴 변화를 따라간다.
#
# 대신 읽기가 주는 것은 본문 구조뿐이다: meta description·구조화 데이터·viewport·링크 수는
# 안 온다. 그래서 page_audits 에는 그 칸을 **NULL(안 봄)** 으로 둔다 — audit_html 은
# schema_json 을 늘 채우므로(없으면 "[]"), schema_json 이 NULL 인 성공 행이 "머리를 못 본
# 행"이다(scoring._has_head_fields). 빈 칸을 "없음"으로 읽으면 멀쩡한 페이지에 "설명이
# 없습니다"를 세운다.
ON_PAGE_PARSE = "/on_page/content_parsing/live"


def _jl(v) -> list:
    try:
        x = json.loads(v) if isinstance(v, str) else v
    except (TypeError, ValueError):
        return []
    return [str(i) for i in x] if isinstance(x, list) else []


def parse_content(result) -> dict | None:
    """DataForSEO On-Page content_parsing 응답 → {title, h1, headings, words}. 못 읽으면 None.

    응답 꼴은 문서와 버전마다 조금씩 다르다 — items[0].page_content 의 main_topic·
    secondary_topic(각각 h_title·level·primary_content[].text)을 읽고, 없는 칸은 건너뛴다.
    """
    try:
        items = (result[0] or {}).get("items") or []
        item = items[0] if items else None
    except (IndexError, TypeError, AttributeError):
        return None
    if not isinstance(item, dict):
        return None
    pc = item.get("page_content") or {}
    topics = [t for t in (pc.get("main_topic") or []) + (pc.get("secondary_topic") or [])
              if isinstance(t, dict)]
    h1: list[str] = []
    h2: list[str] = []
    words = 0
    for t in topics:
        ht = " ".join(str(t.get("h_title") or "").split())
        if ht:
            (h1 if t.get("level") == 1 else h2 if t.get("level") == 2 else []).append(ht)
        for part in ("primary_content", "secondary_content"):
            for c in t.get(part) or []:
                if isinstance(c, dict):
                    words += len(str(c.get("text") or "").split())
    if not (h1 or h2 or words):
        return None
    title = next((str(t.get("main_title")) for t in topics if t.get("main_title")), None) \
        or (h1[0] if h1 else None)
    return {"title": title, "h1": h1[0] if h1 else None, "headings": h2[:30], "words": words}


def read_blocked(url: str, post) -> tuple[dict | None, float, str | None]:
    """막힌 한 장을 DataForSEO 로 읽기 → (parse_content 꼴 | None, 비용, 못 읽은 사유).
    잔액·인증(collector.Fatal)은 올린다 — 다음 장에서도 안 낫는다."""
    try:
        res, cost = post(ON_PAGE_PARSE, [{"url": url}])
    except collector.Fatal:
        raise
    except Exception as e:
        return None, 0.0, f"DataForSEO: {e}"
    got = parse_content(res)
    return got, float(cost or 0), (None if got else "DataForSEO 로도 본문을 못 읽었습니다")


def read_page(url: str, *, fetch, post, dfs: bool) -> tuple[dict, float, str | None]:
    """한 장 읽기 → (정보, 비용, 못 읽은 사유). 직접 → (막히면) DataForSEO → 못 읽음.

    정보: {title, h1, meta, headings(H2), words, fetched_via}. 못 읽어도 한 줄은 낸다 —
    "못 읽었다"가 증거다(화면이 그렇게 말한다).
    """
    a = fetch(url) or {}
    if not a.get("error"):
        return ({"title": a.get("title"), "h1": (_jl(a.get("h1_json")) or [None])[0],
                 "meta": a.get("meta_description"), "headings": _jl(a.get("h2_json")),
                 "words": a.get("words"), "fetched_via": "direct"}, 0.0, None)
    why = str(a.get("error") or "")
    none = {"title": None, "h1": None, "meta": None, "headings": [], "words": None,
            "fetched_via": "none"}
    if blocked(a.get("status")) and dfs:
        got, cost, miss = read_blocked(url, post)
        if got:
            return ({**got, "meta": None, "fetched_via": "dataforseo"}, cost, None)
        return (none, cost, f"{why} · {miss}")
    return (none, 0.0, why)


def audit_from_parsed(url: str, got: dict) -> dict:
    """대신 읽은 본문 구조 → page_audits 한 줄. 안 온 칸은 넣지 않는다(= NULL, 안 봄)."""
    return {"url": url, "status": None, "error": None, "title": got.get("title"),
            "h1_json": json.dumps([got["h1"]] if got.get("h1") else [], ensure_ascii=False),
            "h2_json": json.dumps((got.get("headings") or [])[:20], ensure_ascii=False),
            "words": got.get("words")}


# 대신 읽기 상한 — 페이지 점검은 매일 도는 꼬리 단계다. 막힌 사이트에서 40장을 매일 사면
# 한 달 1,200건이다. 한 회차에 PAGE_DFS_MAX 장까지만, 그리고 최근 PAGE_DFS_FRESH_DAYS 안에
# 읽은(오류 없는 행이 있는) 주소는 다시 안 산다 — 그 주소는 오늘 막힌 줄을 적지 않고 지난
# 행을 그대로 둔다(주소마다 최신 행을 읽으므로, 막힌 줄을 적으면 멀쩡한 지난 값을 덮는다).
PAGE_DFS_MAX = 10
PAGE_DFS_FRESH_DAYS = 7


def _recent_ok(conn, project_id: int, days: int) -> set[str]:
    return {r["url"] for r in conn.execute(
        "SELECT DISTINCT url FROM page_audits WHERE project_id=? AND error IS NULL "
        "AND julianday('now') - julianday(checked_date) < ?", (project_id, days))}


def collect(project: str, *,
            dry_run: bool = False,
            limit: int | None = None,
            throttle: float | None = None,
            dfs_max: int | None = None,
            conn=None,
            post=None) -> collector.StageResult:
    """내 페이지를 가져와 감사 결과를 Brain 에 적재한다. sys.exit 호출 없음.

    Args:
        project: 사이트 이름
        dry_run: True 면 가져올 목록만 찍고 종료
        limit: 한 번에 감사할 URL 수(config 키는 page_urls). 0이면 끔 —
            CLI 플래그(--limit)와 이름을 맞춘다. 어긋나면 `--opt pages.limit=5` 가
            TypeError 로 죽는다(collect_index 의 --limit vs index_urls 가 그 사례).
        throttle: 요청 간격(초) — 내 서버를 두드리는 속도다
        dfs_max: 막힌 페이지를 DataForSEO 로 대신 읽을 상한(설정 키 page_dfs_max,
            기본 PAGE_DFS_MAX). 0 이면 대신 읽기 끔. 키가 없으면 안 한다
        conn: 이미 열린 Brain 연결 — 주면 그것을 쓰고 닫지 않는다
        post: (path, body) -> (result, cost). 기본 serp_adapter.post_dataforseo(검사용 주입)

    Returns:
        StageResult(ok=...). 사유 있는 비종료는 ok=False, skipped=True.
    """
    ap = _parser()
    with collector.stage(project, conn=conn, dry_run=dry_run) as st:
        conn, p = st.conn, st.project
        s = st.settings(ap, argparse.Namespace(limit=limit, throttle=throttle,
                                               dfs_max=dfs_max))
        limit = s["page_urls"]
        use_dfs = post is not None or serp_adapter.has_dataforseo()
        post = post or serp_adapter.post_dataforseo
        n_dfs = s["page_dfs_max"] if use_dfs else 0
        if limit <= 0:
            print("[pages] page_urls=0 — 페이지 감사를 끄셨습니다.")
            return st.noop(rows=0)

        gone = recent_gone(conn, p["id"])
        urls = target_urls(conn, p["id"], limit)
        if not urls:
            return st.skip("감사할 페이지가 없습니다 — 먼저 gsc 를 수집하세요"
                           " (page 분해가 있어야 어느 URL 인지 알 수 있습니다).")

        print(f"[pages] URL {len(urls)}개 · 비용 없음 · 내 사이트 직접 조회 "
              f"(간격 {st.throttle}초)"
              + (f" · 막히면 DataForSEO 로 최대 {n_dfs}장 대신 읽기" if n_dfs else "")
              + (f" · 최근 없는 페이지(404·410) {len(gone)}곳은 {GONE_RECHECK_DAYS}일 뒤 다시 봅니다"
                 if gone else ""))
        if st.dry_run:
            for i, u in enumerate(urls, 1):
                print(f"  {i:>3}. {u}")
            return st.noop(rows=0)

        rows: list[dict] = []
        kept: list[str] = []
        fresh_ok = _recent_ok(conn, p["id"], PAGE_DFS_FRESH_DAYS)
        lock = threading.Lock()
        spend = {"n": 0, "cost": 0.0, "off": ""}

        def get(url: str) -> dict:
            # 일꾼 스레드 — 네트워크만(Brain 을 안 만진다). 막혔으면: 최근에 잘 읽은 주소는
            # 지난 값을 그대로 두고(_keep), 아니면 상한 안에서 DataForSEO 로 대신 읽는다.
            row = fetch(url)
            if not (row.get("error") and blocked(row.get("status"))):
                return row
            if url in fresh_ok:
                return {**row, "_keep": True}
            with lock:
                if spend["off"] or spend["n"] >= n_dfs:
                    return row
                spend["n"] += 1
            try:
                got, cost, miss = read_blocked(url, post)
            except collector.Fatal as e:
                # 잔액·인증 — 이 회차의 대신 읽기만 멈춘다. 페이지 점검 자체를 죽이지 않는다.
                with lock:
                    spend["off"] = str(e)[:160]
                return row
            with lock:
                spend["cost"] += cost
            if got:
                return audit_from_parsed(url, got)
            return {**row, "error": f"{row['error']} · {miss}"[:300]}

        def one(url: str, row: dict) -> None:
            # 가져오기(fetch)는 일꾼이 동시에 하고, 여기는 이 스레드가 urls 순서대로
            # 받는다(fanout) — rows 순서·오류 목록이 순차일 때와 같다.
            # 행은 남긴다 — page_audits.error 는 "못 가져왔다"를 적는 진짜 칸이다.
            # 하지만 거기서 끝내면 실패가 **데이터**가 되어 st.errors 를 못 지나간다:
            # URL 이 전부 죽어도 runs.notes 에 errors=0 이 적히던 자리다.
            if row.get("_keep"):
                kept.append(url)
                print(f"  · {url} — 막힘, {PAGE_DFS_FRESH_DAYS}일 안에 읽은 지난 값을 그대로 둡니다")
                return
            rows.append(row)
            # 404·410 은 못 가져온 게 아니라 "가져왔고 페이지가 없다"는 사실이다 — 깨진 백링크·
            # 크롤 이슈 기회는 대상이 원래 없는 주소다. 이걸 오류로 세면 그런 기회가 열려
            # 있는 동안 매 런이 errors>0 이 되고, 릴리스 게이트(test_remote)가 영영 빨갛다.
            # 5xx·403·429·네트워크 실패는 그대로 오류다(진짜로 못 본 것).
            if row.get("status") in GONE_STATUS:
                print(f"  · {url} — 없는 페이지({row['status']}), 사실로 적습니다")
                return
            if row.get("error"):
                raise collector.ItemFailed(row["error"], status=row.get("status"))
            print(f"  ✓ {url} — title {len(row['title'] or '')}자 · "
                  f"본문 {row['words']}단어 · H1 {len(json.loads(row['h1_json']))}개"
                  + (" (막혀서 DataForSEO 로 대신 읽음 — 설명·구조화 데이터는 못 봄)"
                     if row.get("schema_json") is None else ""))

        with st.record("pages") as r:
            # 내 사이트다 — 한 호스트에 동시 4개까지(fanout.LIMITS["own_site"]).
            # throttle 은 일꾼마다 제 요청 뒤에 쉰다.
            done = fanout.each(st, urls, get, one,
                               workers=fanout.LIMITS["own_site"], label=lambda u: u)
            # 엔티티의 sameAs — 일꾼이 끝난 뒤 이 스레드에서 몇 곳만(상한·시간 안에서). runs.notes
            # 에는 안 싣는다: 그 키는 [기록] 화면의 이름표(HS_NOTE_LABEL)까지 같이 늘려야 한다.
            n_sa = check_same_as(rows, p["domain"] or "")
            if n_sa:
                print(f"  · sameAs {n_sa}곳을 열어 살아 있는지 봤습니다")
            checked = str(date.today())
            db.write_page_audits(conn, p["id"], checked, rows)
            r.api_calls = done
            r.cost = round(spend["cost"], 4)
            r.notes = (f"urls={len(rows)}/{len(urls)} checked={checked} "
                       + (f"dfs={spend['n']} " if spend["n"] else "")
                       + (f"kept={len(kept)} " if kept else "")
                       + (f"gone_skipped={len(gone)} " if gone else "")
                       + f"{st.err_note}")
        if spend["off"]:
            print(f"  ! DataForSEO 대신 읽기를 이 회차에서 멈췄습니다 — {spend['off']}",
                  file=sys.stderr)

        bad = [x for x in rows if x.get("error")]
        print(f"\nsaved {len(rows)} page audits (errors={st.errors})"
              + (f" · 못 가져온 URL {len(bad)}개" if bad else ""))
        # 한 장도 못 봤고 전부 "막힘"이면 실패가 아니라 건너뜀이다 — 원인이 그 사이트의
        # 방화벽이라 다음 런도 똑같이 막힌다. 실패로 두면 매일 실패 메일이 가고, 사유는
        # 파이썬 예외 문장(ReadTimeout: HTTPSConnectionPool…)이라 무엇을 할지 모른다.
        if not done and bad and len(bad) == len(rows) and all(blocked(x.get("status")) for x in bad):
            from urllib.parse import urlparse
            return st.skip(blocked_note(urlparse(urls[0]).hostname or p["domain"], len(bad)))
        # 실제로 감사한 건수로 판정한다 — 전부 못 가져온 것은 완료가 아니다.
        return st.verdict(done, rows=len(rows), cost=round(spend["cost"], 4))


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser()
    collector.add_common(ap)
    collector.add_setting(ap, "--limit", key="page_urls", fallback=40, type=int,
                          help="한 번에 감사할 URL 수. 0이면 끔")
    collector.add_setting(ap, "--throttle", key="throttle", fallback=0.5, type=float,
                          help="요청 간격(초) — 내 서버를 두드리는 속도")
    collector.add_setting(ap, "--dfs-max", key="page_dfs_max", fallback=PAGE_DFS_MAX, type=int,
                          help="막힌 페이지를 DataForSEO 로 대신 읽을 상한(장). 0이면 끔")
    return ap


def main() -> None:
    if len(sys.argv) == 1:
        _selfcheck()
        return
    collector.cli("pages")


def _selfcheck() -> None:
    html = """<html lang="ko"><head><title>  밀리아 제거 비용과 방법  </title>
      <meta name="description" content="밀리아 제거 가격과 회복 기간">
      <meta name="robots" content="index,follow">
      <meta name="viewport" content="width=device-width, initial-scale=1">
      <link rel="canonical" href="/milia">
      <link rel="alternate" hreflang="ko" href="/milia">
      <link rel="alternate" hreflang="en-UK" href="/en/milia">
      <script type="application/ld+json">{"@type":"Article","author":{"@type":"Person"},
        "datePublished":"2024-03-02T10:00:00+09:00","dateModified":"2024-05-01"}</script>
      </head><body>
      <h1>밀리아 제거</h1><h2>비용</h2>
      <p>본문 단어 하나 둘 셋</p>
      <script>var hidden = "이 글자는 본문이 아니다";</script>
      <a href="/other">내부</a><a href="https://other.com/x">외부</a>
      <img src="a.png" alt="설명"><img src="b.png">
      </body></html>"""
    a = audit_html("https://clinic.kr/milia", html)
    assert a["title"] == "밀리아 제거 비용과 방법", a["title"]
    assert a["meta_description"] == "밀리아 제거 가격과 회복 기간", a
    assert json.loads(a["h1_json"]) == ["밀리아 제거"], a["h1_json"]
    assert json.loads(a["h2_json"]) == ["비용"], a["h2_json"]
    assert json.loads(a["schema_json"]) == ["Article", "Person"], a["schema_json"]
    assert a["canonical"] == "https://clinic.kr/milia", a["canonical"]
    assert a["robots"] == "index,follow"
    assert (a["internal_links"], a["external_links"]) == (1, 1), a
    assert (a["images"], a["images_no_alt"]) == (2, 1), a
    assert a["viewport"].startswith("width=device-width"), a["viewport"]
    assert a["html_lang"] == "ko", a["html_lang"]
    assert json.loads(a["hreflang_json"]) == [["ko", "https://clinic.kr/milia"],
                                              ["en-UK", "https://clinic.kr/en/milia"]],         a["hreflang_json"]
    # 글이 언제 쓰였는지는 우리가 점검한 날과 다른 사실이다 — 순위 하락의 원인
    # 후보에서 "낡았다"를 가르는 유일한 재료다.
    assert (a["published"], a["modified"]) == ("2024-03-02", "2024-05-01"), a
    assert a["js_shell"] == 0, "본문이 있는 페이지를 JS 껍데기로 본다"

    # 정적 HTML 로는 본문이 안 보이는 페이지 — "구조화 데이터 없음" 을 사실로
    # 말하면 안 되는 자리다. 판정이 아니라 단서라서 얇을 때만 켜진다.
    shell = audit_html("https://spa.kr/x", '<html><body><div id="root"></div>'
                       '<script src="/a.js"></script></body></html>')
    assert shell["js_shell"] == 1, shell
    assert audit_html("https://ssr.kr/x",
                      '<html><body><div id="__next">' + "글자 " * 200
                      + "</div></body></html>")["js_shell"] == 0, "본문이 두꺼운 SSR 을 껍데기로 본다"
    # <a id="root"> 하나가 링크 집계를 삼키지 않는다 (판정을 elif 사슬 밖에 둔 이유)
    links = audit_html("https://s.kr/x", '<html><body><a id="root" href="/b">x</a></body></html>')
    assert links["internal_links"] == 1, links

    # 날짜는 meta·time·ld+json 어디서 와도 같은 꼴이 된다
    assert _date("2026-08-21T09:00:00+09:00") == "2026-08-21"
    assert _date("Aug 21, 2026") == "Aug 21, 2026"        # 못 읽으면 원문 — 지어내지 않는다
    assert _date("") is None
    assert _schema_dates('{"dateModified":"2026-01-02",,,}') == (None, "2026-01-02")
    # script·title 안 글자는 본문이 아니다 — 세면 얇은 페이지가 두꺼워 보인다
    assert a["words"] == 10, a["words"]       # h1 2 + h2 1 + p 5 + a 2
    assert audit_html("https://c.kr/x", "<html><body><script>가 나 다 라 마</script></body></html>"
                      )["words"] == 0, "script 본문이 단어로 새어 들어간다"

    # 추출성 — 표·목록·질문형 H2·첫 문단·저자. 메뉴·꼬리말·곁가지의 틀은 안 센다
    # (안 빼면 거의 모든 페이지가 "목록 있음"이 된다). 겹친 표·목록은 바깥 하나로.
    ext = audit_html("https://x.kr/a", """<html><head><meta name="author" content="김의사">
      </head><body><nav><ul><li>메뉴</li></ul><p>메뉴 안의 긴 문단 하나 둘 셋 넷 다섯</p></nav>
      <header><p>사이트 태그라인 한 줄 입니다 정말로</p></header>
      <main><article><header><h1>밀리아란 무엇인가</h1></header>
      <p>by 홍길동</p>
      <p>밀리아는 피부 아래 생기는 작은 흰 알갱이로 각질이 갇혀 생깁니다 보통 저절로 없어집니다
      <h2>비용은 얼마인가요?</h2><h2>회복 기간</h2><h2>시술은 아픈가요</h2>
      <table><tr><td><table><tr><td>x</td></tr></table></td></tr></table>
      <ol><li>하나<ul><li>안</li></ul></li></ol></article></main>
      <aside><table><tr><td>곁</td></tr></table></aside>
      <footer><ul><li>f</li></ul></footer></body></html>""")
    assert (ext["tables"], ext["lists"]) == (1, 1), ext
    assert ext["h2_questions"] == 2, ext               # 물음표 하나 + 의문 어미 하나
    # H1 뒤 첫 문단 — 바이라인(5단어 미만)은 건너뛰고, </p> 를 빼먹어도 다음 블록에서 닫는다
    assert ext["lead_words"] == 13, ext
    assert ext["author"] == "김의사", ext
    # ld+json 의 author — 목록·{name}·깨진 JSON 어디서 와도. 이름 없는 Person 은 저자가 아니다
    assert audit_html("https://x.kr/b", '<script type="application/ld+json">{"@type":"Article",'
                      '"author":[{"@type":"Person","name":"홍길동"}]}</script>')["author"] == "홍길동"
    assert _schema_author('{"author":{"@type":"Person","name":"이몽룡"},,,}') == "이몽룡"
    assert _schema_author('{"author":{"@type":"Person"}}') is None
    # 저자·표를 못 찾은 것은 "봤고 없다" — 0 과 "" 로 적는다(NULL 은 옛 행의 몫이다)
    assert (a["tables"], a["lists"], a["h2_questions"], a["lead_words"], a["author"]) \
        == (0, 0, 0, 5, ""), a
    assert audit_html("https://x.kr/c", "<html><body><h1>x</h1><div>글자만 있는 본문 하나 둘"
                      " 셋</div></body></html>")["lead_words"] == 0, "문단 없음이 0 이 아니다"
    # 질문형 H2 는 휴리스틱이다 — 명사 끝의 '가'·'하나' 에 안 걸리는지만 못 박는다
    assert _is_question("비용은 얼마인가요") and _is_question("How does it work")
    assert not _is_question("비용 평가") and not _is_question("관리 요령")

    # 깨진 ld+json 이어도 @type 은 건진다
    assert _schema_types('{"@type":"FAQPage",,,}') == ["FAQPage"]
    assert _schema_types('[{"@type":["Article","BlogPosting"]}]') == ["Article", "BlogPosting"]

    # 못 가져온 페이지도 한 줄로 남는다 — 그 자체가 진단이다
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    conn.execute("INSERT INTO projects(id,name,type,domain) VALUES(1,'p','saas','c.kr')")
    conn.executemany(
        "INSERT INTO gsc_snapshots(project_id,snapshot_date,period_days,query,page,"
        "clicks,impressions,ctr,position) VALUES(1,'2026-08-20',28,?,?,?,?,0.0,?)",
        [("밀리아", "https://c.kr/a", 1, 500, 8.0), ("점 빼기", "https://c.kr/b", 0, 100, 15.0)])
    conn.execute("INSERT INTO opportunities(project_id,kind,target,score,status) "
                 "VALUES(1,'ctr_gap','밀리아',80,'new')")
    conn.commit()
    urls = target_urls(conn, 1, 5)
    assert urls[0] == "https://c.kr/a", urls          # 기회에 걸린 페이지가 먼저
    assert "https://c.kr/b" in urls, urls             # 나머지는 노출 상위로 채운다
    # 상한은 기회·노출 페이지의 것이다 — 홈은 그 밖의 한 자리(_home_check)
    assert [u for u in target_urls(conn, 1, 1) if u != "https://c.kr/"] == ["https://c.kr/a"], \
        "상한을 넘긴다"

    n = db.write_page_audits(conn, 1, "2026-08-20", [a])
    assert n == 1
    assert db.write_page_audits(conn, 1, "2026-08-20", [a]) == 1, "같은 날 두 번이 늘어난다"
    got = conn.execute("SELECT title, words FROM page_audits").fetchall()
    assert len(got) == 1 and got[0]["title"] == a["title"], [tuple(r) for r in got]
    # 추출성 칸이 실제로 적힌다 — "" 와 0 이 NULL 로 뭉개지지 않는다
    row = conn.execute("SELECT tables, lead_words, author FROM page_audits").fetchone()
    assert tuple(row) == (0, 5, ""), tuple(row)
    # 옛 행 — 추출성 칸이 없는 dict 는 NULL 로 남는다(안 봤다 ≠ 없다)
    old = {k: v for k, v in a.items()
           if k not in ("tables", "lists", "h2_questions", "lead_words", "author")}
    db.write_page_audits(conn, 1, "2026-08-19", [old])
    row = conn.execute("SELECT tables, author FROM page_audits WHERE checked_date='2026-08-19'"
                       ).fetchone()
    assert tuple(row) == (None, None), tuple(row)

    # 칸이 생기기 전의 Brain — _migrate 가 PRAGMA 로 보고 칸을 보탠다
    old_db = sqlite3.connect(":memory:")
    old_db.row_factory = sqlite3.Row
    old_db.execute("CREATE TABLE page_audits (id INTEGER PRIMARY KEY, project_id INTEGER,"
                   " checked_date TEXT, url TEXT, js_shell INTEGER)")
    old_db.executescript(db.SCHEMA)
    db._migrate(old_db)
    cols = {r["name"] for r in old_db.execute("PRAGMA table_info(page_audits)")}
    assert {"tables", "lists", "h2_questions", "lead_words", "author"} <= cols, cols
    assert {"schema_gaps_json", "same_as_json"} <= cols, cols
    # 구조화 데이터 검증 칸도 실제로 적힌다 — "[]"(봤고 빠진 것 없음)가 NULL 로 뭉개지지 않는다
    row = conn.execute("SELECT schema_gaps_json FROM page_audits WHERE checked_date='2026-08-20'"
                       ).fetchone()
    # (이름 없는 저자 {"@type":"Person"} — author 칸은 있지만 그 Person 이 이름을 안 갖는다)
    assert row[0] == a["schema_gaps_json"] and json.loads(row[0]) == [
        {"type": "Article", "as": "Article", "n": 1, "need": [], "want": ["headline", "image"]},
        {"type": "Person", "as": "Person", "n": 1, "need": [],
         "want": ["name", "url|sameAs"], "each": [["name", "url|sameAs"]]}], row[0]

    _concurrency_check(conn)
    _blocked_check(conn)
    _dfs_check(conn)
    # 추적 꼬리표는 떼고, 다른 페이지를 가르는 쿼리는 남긴다
    assert clean_url("https://g.kr/?ref=ed3sign") == "https://g.kr/"
    assert clean_url("https://g.kr/uk/?gclid=EAIa&gclsrc=aw.ds") == "https://g.kr/uk/"
    assert clean_url("https://g.kr/a?utm_source=x&page=2#top") == "https://g.kr/a?page=2"
    assert clean_url("https://g.kr/a?lang=ko") == "https://g.kr/a?lang=ko"
    assert clean_url("https://g.kr/a") == "https://g.kr/a"
    # 점검 대상을 고르는 자리가 실제로 떼는지 — 꼬리표 사본은 한 자리로 합쳐진다.
    conn.executemany("INSERT INTO opportunities(project_id, kind, target, score) VALUES(1,?,?,?)",
                     [("crawl_issue", "https://tu.kr/?ref=ed3sign", 9),
                      ("crawl_issue", "https://tu.kr/", 8),
                      ("crawl_issue", "https://tu.kr/uk/?gclid=EAIa", 7)])
    conn.commit()
    got = [u for u in target_urls(conn, 1, 40) if u.startswith("https://tu.kr")]
    assert got == ["https://tu.kr/", "https://tu.kr/uk/"], f"점검 대상에 추적 꼬리표 사본이 남았다: {got}"
    _schema_check()
    _home_check(conn)
    _same_as_check(conn)
    print("collect_page self-check ok")


def _schema_check() -> None:
    """구조화 데이터 검증 — 유형별 필수·권장 속성 표(scoring.SCHEMA_RULES)로 '빠진 것'만 남긴다.

    예전엔 @type 이름만 남겨서(["Article","Person"]) "있다"까지만 말했다. 필수 속성이 빠진
    마크업은 구글이 리치 결과에 안 쓰는데, 화면·요청문은 '구조화 데이터: Product'로 끝났다."""
    graph = """<html><head><script type="application/ld+json">{"@context":"https://schema.org",
      "@graph":[
        {"@type":"MedicalClinic","@id":"#org","name":"우리","url":"https://c.kr/",
         "sameAs":["https://sns.example/c","https://gone.example/x"]},
        {"@type":"BlogPosting","headline":"h","author":{"@id":"#p"}},
        {"@type":"Person","@id":"#p","name":"홍길동"},
        {"@type":"Person","name":"소개만 된 사람"},
        {"@type":"Product","name":"상품"},
        {"@type":"Event","name":"행사","location":{"@type":"Place","name":"x"}},
        {"@type":"Review","author":"a","reviewRating":{"@type":"Rating","ratingValue":5}},
        {"@type":"VideoObject","name":"v","thumbnailUrl":"t.png","uploadDate":"2026-01-01"}]}
      </script>
      <script type="application/ld+json">{"@type":"FAQPage",,,}</script></head><body></body></html>"""
    a = audit_html("https://c.kr/", graph)
    rows = json.loads(a["schema_gaps_json"])
    gaps = {g["type"]: g for g in rows if g.get("type")}
    # 하위 유형은 상위 규칙으로 — MedicalClinic 은 LocalBusiness(필수 name·address)
    assert gaps["LocalBusiness"]["as"] == "MedicalClinic", gaps
    assert gaps["LocalBusiness"]["need"] == ["address"], gaps["LocalBusiness"]
    assert set(gaps["LocalBusiness"]["want"]) == {"logo", "telephone", "geo",
                                                  "openingHoursSpecification"}, gaps
    assert gaps["Product"]["need"] == ["offers|review|aggregateRating"], gaps["Product"]
    assert gaps["Event"]["need"] == ["startDate"], gaps["Event"]
    assert gaps["Review"]["need"] == ["itemReviewed"], gaps["Review"]   # 맨 위의 리뷰는 대상이 필수
    # 빠진 것이 없는 유형은 안 남는다(VideoObject 필수 셋이 다 있다 — 권장만 빠짐)
    assert gaps["VideoObject"]["need"] == [], gaps["VideoObject"]
    # Person 은 **저자**만 본다(@id 로 이어진 것 포함) — 글이 소개하는 인물은 엔티티 검사 밖
    assert gaps["Person"]["n"] == 1 and gaps["Person"]["want"] == ["url|sameAs"], gaps["Person"]
    # 깨진 블록은 수집을 멈추지 않고(@type 은 지금처럼 건진다) 깨졌다는 사실만 남는다
    assert [g["broken"] for g in rows if g.get("broken")] == [1], rows
    assert "FAQPage" in json.loads(a["schema_json"]), a["schema_json"]
    # 원문은 저장하지 않는다 — 값(이름·주소)이 칸에 새지 않는다
    assert "홍길동" not in a["schema_gaps_json"] and "우리" not in a["schema_gaps_json"]
    # sameAs 주소는 행 칸이 아니라 수집 중에 살아 있는지 볼 재료다(write_page_audits 가 버린다)
    assert a["same_as"] == [["LocalBusiness", "https://sns.example/c"],
                            ["LocalBusiness", "https://gone.example/x"]], a["same_as"]
    # 다 갖춘 마크업은 "[]" — 봤고 빠진 것이 없다(NULL 은 안 본 행이다)
    ok = audit_html("https://c.kr/a", '<script type="application/ld+json">{"@type":"BreadcrumbList",'
                    '"itemListElement":[{"@type":"ListItem","position":1,"name":"홈","item":"https://c.kr/"}]}'
                    "</script>")
    assert ok["schema_gaps_json"] == "[]", ok["schema_gaps_json"]
    assert audit_html("https://c.kr/b", "<html><body>x</body></html>")["schema_gaps_json"] == "[]"
    # 다른 노드 안에 든 리뷰·평점은 대상(itemReviewed)이 그 바깥 노드다 — 빠졌다고 안 한다
    nested = audit_html("https://c.kr/p", '<script type="application/ld+json">{"@type":"Product",'
                        '"name":"p","review":{"@type":"Review","author":"a","reviewRating":{"ratingValue":4}},'
                        '"aggregateRating":{"@type":"AggregateRating","ratingValue":4,"reviewCount":3}}'
                        "</script>")
    ng = {g["type"]: g for g in json.loads(nested["schema_gaps_json"])}
    assert set(ng) == {"Product", "Review", "AggregateRating"}, ng       # 권장만 빠짐
    assert all(g["need"] == [] for g in ng.values()), ng
    # 유형을 여럿 단 노드는 좁은 쪽 규칙으로(Organization + LocalBusiness → LocalBusiness)
    multi = audit_html("https://c.kr/m", '<script type="application/ld+json">{"@type":["Organization",'
                       '"Dentist"],"name":"n","address":"a","url":"u","logo":"l","sameAs":"https://s.example/",'
                       '"telephone":"1","geo":{"latitude":1},"openingHoursSpecification":[{"opens":"09:00"}]}'
                       "</script>")
    assert multi["schema_gaps_json"] == "[]", multi["schema_gaps_json"]
    # 대신 읽은 행(DataForSEO)은 마크업을 못 봤다 — 칸이 NULL(안 봄)
    assert "schema_gaps_json" not in audit_from_parsed("https://g.kr/", {"title": "t", "h1": "h"})

    def gaps_of(blob: str) -> dict:
        r = audit_html("https://c.kr/z", f'<script type="application/ld+json">{blob}</script>')
        return {g["type"]: g for g in json.loads(r["schema_gaps_json"]) if g.get("type")}
    # 저자는 글(Article 계열·WebPage)의 author 뿐이다 — 상품 리뷰의 author 는 고객이다. 고객에게
    # 소개 페이지·외부 프로필을 적으라는 [엔티티] 가 리뷰 단 상품 페이지마다 섰다.
    rv = gaps_of('{"@type":"Product","name":"p","offers":{"price":1},"review":[{"@type":"Review",'
                 '"reviewRating":{"ratingValue":5},"author":{"@type":"Person","name":"고객"}}]}')
    assert "Person" not in rv, rv
    assert audit_html("https://c.kr/z", '<script type="application/ld+json">{"@type":"Product",'
                      '"review":{"@type":"Review","author":{"@type":"Person","name":"고객",'
                      '"sameAs":"https://sns.example/u"}}}</script>')["same_as"] == [], \
        "고객의 sameAs 를 열어 본다"
    wp = gaps_of('{"@type":"WebPage","author":{"@type":"Person","name":"쓴 사람"}}')
    assert wp["Person"]["want"] == ["url|sameAs"], wp
    # 저자가 여럿이면 저자마다 — 한 명이라도 url 이 있으면 다른 저자의 빈칸이 사라졌다(교집합)
    many = gaps_of('{"@type":"Article","headline":"h","image":"i","datePublished":"d",'
                   '"dateModified":"d","author":[{"@type":"Person","name":"A","url":"https://c.kr/a"},'
                   '{"@type":"Person","name":"B"}]}')
    assert many["Person"]["n"] == 2 and many["Person"]["each"] == [[], ["url|sameAs"]], many
    assert many["Person"]["want"] == ["url|sameAs"], many
    # 저자는 사람 단위로 센다 — 노드 단위로 셌더니 글 목록(CollectionPage > ItemList 안 BlogPosting
    # 8개)에 같은 'Kim' 을 inline 으로 적은 페이지가 '저자 8명'이 되고 같은 줄이 여덟 번 섰다.
    # 이름·url·sameAs 가 겹치면 같은 사람이다(이름은 접는 데만 쓰고 남기지 않는다).
    posts = [{"@type": "ListItem", "position": i + 1,
              "item": {"@type": "BlogPosting", "headline": f"h{i}",
                       "author": {"@type": "Person", "name": "Kim"}}} for i in range(8)]
    listing = gaps_of(json.dumps({"@type": "CollectionPage", "mainEntity": {
        "@type": "ItemList", "itemListElement": posts}}))
    assert listing["Person"]["n"] == 1 and listing["Person"]["each"] == [["url|sameAs"]], listing
    # 글과 쪽에 같은 사람을 두 번 적은 테마 — 한 명이고, 한 자리에 url 이 있으면 그 사람은 이어졌다
    twice = gaps_of('[{"@type":"WebPage","author":{"@type":"Person","name":"Kim"}},'
                    '{"@type":"Article","author":{"@type":"Person","name":" kim ","url":"https://c.kr/kim"}}]')
    assert "Person" not in twice, twice
    by_url = gaps_of('{"@type":"Article","author":[{"@type":"Person","name":"김","url":"https://c.kr/kim/"},'
                     '{"@type":"Person","name":"Kim","url":"https://c.kr/kim"}]}')
    assert "Person" not in by_url, by_url
    # 순번은 글의 author 순서다(@graph·훑은 순서가 아니다) — 이름을 안 남기니 사람을 가리키는 수단이
    # 순번뿐이다. @graph 에 B(url 없음)를 먼저 두고 author 는 [A, B] 인 글에서 빠진 사람은 2번째다.
    order = gaps_of('{"@graph":[{"@type":"Person","@id":"#b","name":"B"},'
                    '{"@type":"BlogPosting","author":[{"@id":"#a"},{"@id":"#b"}]},'
                    '{"@type":"Person","@id":"#a","name":"A","url":"https://c.kr/a"}]}')
    assert order["Person"]["n"] == 2 and order["Person"]["each"] == [[], ["url|sameAs"]], order
    # @graph 에 따로 두고 @id 로 잇는 평점은 그 바깥 노드(Product) 안에 든 것이다 — 구글은 @id 를
    # 합쳐 읽는다. 같은 @id 를 두 자리에 나눠 적은 노드도 한 노드로 합친다.
    ref = gaps_of('{"@context":"https://schema.org","@graph":[{"@type":"Product","@id":"#p",'
                  '"name":"p","aggregateRating":{"@id":"#r"}},{"@type":"AggregateRating","@id":"#r",'
                  '"ratingValue":4,"reviewCount":3},{"@id":"#p","offers":{"price":1}}]}')
    assert ref.get("AggregateRating", {}).get("need", []) == [], ref
    assert ref.get("Product", {}).get("need", []) == [], ref
    # 주석·CDATA 로 감싼 블록, 제어 문자가 든 문자열은 깨진 블록이 아니다(strict=False)
    for blob in ('<!-- {"@type":"BreadcrumbList","itemListElement":[1]} -->',
                 '//<![CDATA[\n{"@type":"BreadcrumbList","itemListElement":[1]}\n//]]>',
                 '/*<![CDATA[*/{"@type":"BreadcrumbList","itemListElement":[1]}/*]]>*/'):
        got = audit_html("https://c.kr/w", f'<script type="application/ld+json">{blob}</script>')
        assert got["schema_gaps_json"] == "[]" and json.loads(got["schema_json"]) == ["BreadcrumbList"], \
            (blob, got["schema_gaps_json"], got["schema_json"])
    assert _ld_load('{"name":"a\tb\x07c"}') == {"name": "a\tb\x07c"}


def _home_check(conn) -> None:
    """홈은 엔티티(사이트 주인 마크업)를 보는 자리라 점검 대상에 늘 든다 — 상한과 상관없이(상한
    밖 한 자리), 단 최근에 본 홈은 다시 안 연다. 상위 노출 페이지 목록에 홈이 없는 사이트(검색어가
    전부 글로 가는 곳)는 홈을 한 번도 안 열어 엔티티 판정이 영영 안 섰다."""
    urls = target_urls(conn, 1, 1)
    assert urls == ["https://c.kr/a", "https://c.kr/"], f"상한이 작아도 홈은 따로 한 자리: {urls}"
    # 홈이 후보(노출 상위)에 있되 상한 밖으로 잘렸어도 넣는다 — 예전엔 '이미 본 후보'로 쳐서 뺐다
    # (기회 둘이 a·b 를 앞에 세우고, 노출 상위 둘 중 홈이 셋째 자리로 밀려 잘린다)
    conn.execute("INSERT INTO gsc_snapshots(project_id,snapshot_date,period_days,query,page,"
                 "clicks,impressions,ctr,position) VALUES(1,'2026-08-20',28,'브랜드','https://c.kr/',"
                 "0,300,0.0,3.0)")
    conn.execute("INSERT INTO opportunities(project_id,kind,target,score,status) "
                 "VALUES(1,'ctr_gap','점 빼기',70,'new')")
    conn.commit()
    urls = target_urls(conn, 1, 2)
    assert urls == ["https://c.kr/a", "https://c.kr/b", "https://c.kr/"], urls
    conn.execute("DELETE FROM gsc_snapshots WHERE page='https://c.kr/'")
    conn.execute("DELETE FROM opportunities WHERE target='점 빼기'")
    # www — 서치콘솔·크롤이 아는 주소 꼴을 따른다. 도메인이 'c.kr' 이어도 사이트가 www 면 www 한 번
    conn.execute("INSERT INTO gsc_snapshots(project_id,snapshot_date,period_days,query,page,"
                 "clicks,impressions,ctr,position) VALUES(1,'2026-08-20',28,'브랜드',"
                 "'https://www.c.kr/',0,1,0.0,3.0)")
    conn.commit()
    urls = target_urls(conn, 1, 40)
    homes = [u for u in urls if scoring.is_home(u, "c.kr")]
    assert homes == ["https://www.c.kr/"], f"홈을 두 꼴로 연다: {homes}"
    # 후보(노출 상위)에 www 홈과 비www 홈이 둘 다 있어도 한 꼴만 연다 — 서치콘솔이 더 많이 보인 꼴.
    # 예전엔 덧붙이는 홈만 한 번이었고 후보에 든 두 꼴은 둘 다 열었다(theotherskin 은 9/02부터 매일
    # 두 행이 생겨 홈 엔티티 진단이 두 번 섰다).
    conn.execute("INSERT INTO gsc_snapshots(project_id,snapshot_date,period_days,query,page,"
                 "clicks,impressions,ctr,position) VALUES(1,'2026-08-20',28,'브랜드2',"
                 "'https://c.kr/',0,5,0.0,3.0)")
    conn.commit()
    homes = [u for u in target_urls(conn, 1, 40) if scoring.is_home(u, "c.kr")]
    assert homes == ["https://c.kr/"], f"후보에 든 두 꼴을 다 연다: {homes}"
    # 기회가 콕 집은 홈 주소가 있으면 그 꼴 하나 — 요청문이 그 글자로 감사 행을 찾는다
    conn.execute("INSERT INTO opportunities(project_id,kind,target,score,status) "
                 "VALUES(1,'crawl_issue','https://www.c.kr/',99,'new')")
    conn.commit()
    homes = [u for u in target_urls(conn, 1, 40) if scoring.is_home(u, "c.kr")]
    assert homes == ["https://www.c.kr/"], f"기회의 홈과 노출 상위의 홈을 둘 다 연다: {homes}"
    conn.execute("DELETE FROM opportunities WHERE target='https://www.c.kr/'")
    conn.execute("DELETE FROM gsc_snapshots WHERE page='https://c.kr/'")
    conn.execute("DELETE FROM gsc_snapshots WHERE page='https://www.c.kr/'")
    # 어제 www 꼴로 본 홈이면(크롤 주소) 도메인 꼴로 또 열지 않는다
    conn.execute("INSERT INTO page_audits(project_id,checked_date,url,status) "
                 "VALUES(1,date('now','-1 day'),'https://www.c.kr/',200)")
    conn.commit()
    assert not [u for u in target_urls(conn, 1, 40) if scoring.is_home(u, "c.kr")], "어제 본 홈을 또 연다"
    conn.execute("UPDATE page_audits SET checked_date=date('now','-30 day') WHERE url='https://www.c.kr/'")
    conn.commit()
    assert [u for u in target_urls(conn, 1, 40) if scoring.is_home(u, "c.kr")] == ["https://www.c.kr/"]
    conn.execute("DELETE FROM page_audits WHERE url='https://www.c.kr/'")
    conn.commit()


def _same_as_check(conn) -> None:
    """sameAs 는 몇 곳만 열어 본다 — 홈 사이트 주인의 것 먼저, 그다음 저자. 상한(SAME_AS_MAX)과
    시간(SAME_AS_BUDGET) 안에서만. 없는 페이지(404·410)만 '죽었다'고 적는다: 소셜 사이트는
    봇에 403·429·로그인 벽을 주는 일이 흔해서 그걸 죽은 주소로 읽으면 멀쩡한 계정을 빼라고 한다."""
    import contextlib
    import io

    conn.execute("INSERT INTO projects(id,name,type,domain) VALUES(9,'sa','saas','sa.kr')")
    conn.commit()
    home = ('<script type="application/ld+json">{"@type":"Organization","name":"n","url":"u",'
            '"logo":"l","sameAs":["https://gone.example/",'
            + ",".join(f'"https://s{i}.example/"' for i in range(8)) + ']}</script>')
    post = ('<script type="application/ld+json">{"@type":"Article","headline":"h","author":'
            '{"@type":"Person","name":"p","sameAs":["https://gone.example/",'
            '"https://author.example/"]}}</script>')
    pages = {"https://sa.kr/": home, "https://sa.kr/post": post}
    opened: list[str] = []

    def link(u, timeout=None):
        opened.append(u)
        return 404 if "gone" in u else 403 if u.startswith("https://s1.") else 200

    g = globals()
    orig = g["target_urls"], g["fetch"], g["check_link"]
    g["target_urls"] = lambda c, pid, limit: list(pages)
    g["fetch"] = lambda u, timeout=None: audit_html(u, pages[u])
    g["check_link"] = link
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            collect("sa", conn=fanout.MainThreadOnly(conn), throttle=0)
    finally:
        g["target_urls"], g["fetch"], g["check_link"] = orig
    assert opened == ["https://gone.example/"] + [f"https://s{i}.example/"
                                                   for i in range(SAME_AS_MAX - 1)], \
        f"홈 사이트 주인의 sameAs 부터 상한까지만 연다(같은 주소는 한 번): {opened}"
    got = {r["url"]: r["same_as_json"] for r in conn.execute(
        "SELECT url, same_as_json FROM page_audits WHERE project_id=9")}
    sa = json.loads(got["https://sa.kr/"])
    assert {x["url"]: x["status"] for x in sa}["https://s1.example/"] == 403, sa
    assert [x["url"] for x in sa if x["dead"]] == ["https://gone.example/"], sa
    # 저자 쪽 같은 주소도 같은 결과를 받는다(한 번 열고 둘 다 적는다). 상한 밖 주소는 안 적는다
    assert [(x["url"], x["dead"]) for x in json.loads(got["https://sa.kr/post"])] \
        == [("https://gone.example/", True)], got
    # 시간 상한 — 이미 넘겼으면 한 곳도 안 연다(안 본 칸은 NULL 이다)
    opened.clear()
    g["target_urls"], g["fetch"], g["check_link"] = (lambda c, pid, limit: list(pages)), \
        (lambda u, timeout=None: audit_html(u, pages[u])), link
    budget = g["SAME_AS_BUDGET"]
    g["SAME_AS_BUDGET"] = 0
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            collect("sa", conn=fanout.MainThreadOnly(conn), throttle=0)
    finally:
        g["target_urls"], g["fetch"], g["check_link"] = orig
        g["SAME_AS_BUDGET"] = budget
    assert not opened, opened
    assert conn.execute("SELECT same_as_json FROM page_audits WHERE project_id=9 "
                        "AND url='https://sa.kr/'").fetchone()[0] is None
    # 홈 행은 도메인으로 알아본다 — 사이트가 www 면 'https://www.sa.kr/' 이 홈이다. 예전엔 도메인에서
    # 지은 주소와 글자가 같은 행만 홈으로 쳐서 www 홈의 주인 sameAs 를 하나도 안 열었다.
    rows = [{"url": "https://sa.kr/p", "same_as": [["Person", "https://p.example/"]]},
            {"url": "https://www.sa.kr/", "same_as": [["Organization", "https://o.example/"]]}]
    g["check_link"] = link
    try:
        opened.clear()
        assert check_same_as(rows, "sa.kr") == 2
    finally:
        g["check_link"] = orig[2]
    assert opened == ["https://o.example/", "https://p.example/"], opened
    assert json.loads(rows[1]["same_as_json"])[0]["url"] == "https://o.example/", rows


def _dfs_check(conn) -> None:
    """막힌 페이지 대신 읽기(P11)와 없는 페이지 건너뛰기(P9) — 가짜 fetch·post(네트워크 0).

    ① 막힌 주소는 상한(dfs_max) 안에서 DataForSEO 로 읽고, 행은 머리 칸이 NULL(안 봄)이다
    ② 상한을 넘은 주소는 막힌 줄 그대로 ③ 최근에 읽은 주소는 막혀도 지난 값을 덮지 않는다
    ④ 잔액(Fatal)이면 대신 읽기만 멈추고 점검은 산다 ⑤ 최근 404 는 대상에서 빠진다.
    """
    import contextlib
    import io

    conn.execute("INSERT INTO projects(id,name,type,domain) VALUES(7,'dfs','saas','g.kr')")
    conn.commit()
    urls = [f"https://g.kr/p{i}" for i in range(4)]
    g = globals()
    orig = g["target_urls"], g["fetch"]
    calls: list[str] = []

    def post(path, body):
        assert path == ON_PAGE_PARSE, path
        calls.append(body[0]["url"])
        return ([{"items": [{"page_content": {"main_topic": [
            {"h_title": "가방", "level": 1, "main_title": "구찌 가방",
             "primary_content": [{"text": "하나 둘 셋"}]},
            {"h_title": "크기", "level": 2}]}}]}], 0.002)

    def go(project, post_fn, dfs_max, urls_now=urls):
        g["target_urls"] = lambda c, pid, limit: list(urls_now)
        g["fetch"] = lambda u, timeout=None: {"url": u, "status": 403, "error": "HTTP 403"}
        try:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                return collect(project, conn=fanout.MainThreadOnly(conn), throttle=0,
                               dfs_max=dfs_max, post=post_fn)
        finally:
            g["target_urls"], g["fetch"] = orig

    res = go("dfs", post, 2)
    assert len(calls) == 2, f"상한(2)을 넘겨 샀다: {calls}"
    got = {r["url"]: dict(r) for r in conn.execute(
        "SELECT * FROM page_audits WHERE project_id=7")}
    ok = [u for u, r in got.items() if not r["error"]]
    assert len(ok) == 2 and all(got[u]["schema_json"] is None and got[u]["meta_description"] is None
                                for u in ok), got
    assert got[ok[0]]["title"] == "구찌 가방" and json.loads(got[ok[0]]["h2_json"]) == ["크기"], got[ok[0]]
    assert res.partial and abs(res.cost - 0.004) < 1e-9, res
    # ③ 다음 날 — 대신 읽은 주소는 7일 안이라 다시 안 사고, 막힌 줄로 덮지도 않는다
    conn.execute("UPDATE page_audits SET checked_date=date('now','-1 day') WHERE project_id=7")
    conn.commit()
    calls.clear()
    go("dfs", post, 10)
    assert not set(calls) & set(ok), f"최근에 읽은 주소를 또 샀다: {calls}"
    today = {r["url"] for r in conn.execute(
        "SELECT url FROM page_audits WHERE project_id=7 AND checked_date=date('now')")}
    assert not today & set(ok), "최근에 잘 읽은 주소에 막힌 줄을 덮어 적었다"
    # ④ 잔액 없음 — 점검은 끝까지 가고(막힌 줄), 단계가 Fatal 로 죽지 않는다
    conn.execute("DELETE FROM page_audits WHERE project_id=7")
    conn.commit()

    def broke(path, body):
        raise collector.Fatal("DataForSEO 잔액 없음(402)")
    res = go("dfs", broke, 10)
    assert res.skipped and "응답하지 않습니다" in res.reason, res
    # ⑤ 최근 404 는 대상에서 빠지고, 오래된 404 는 다시 본다
    conn.execute("INSERT INTO page_audits(project_id,checked_date,url,status,error) "
                 "VALUES(1,date('now','-1 day'),'https://c.kr/gone',404,'HTTP 404')")
    conn.execute("INSERT INTO page_audits(project_id,checked_date,url,status,error) "
                 "VALUES(1,date('now','-30 day'),'https://c.kr/old-gone',404,'HTTP 404')")
    conn.executemany("INSERT INTO opportunities(project_id,kind,target,score,status) "
                     "VALUES(1,'crawl_issue',?,50,'new')",
                     [("https://c.kr/gone",), ("https://c.kr/old-gone",)])
    conn.commit()
    tu = target_urls(conn, 1, 40)
    assert "https://c.kr/gone" not in tu, f"방금 404 였던 주소를 또 연다: {tu}"
    assert "https://c.kr/old-gone" in tu, "404 를 영영 빼 버렸다 — 고친 뒤에도 404 로 남는다"


def _blocked_check(conn) -> None:
    """사이트가 이 서버를 막으면(전부 무응답·403·429) 실패가 아니라 건너뜀이고, 사유가
    사람 말이다. 하나라도 보이면(또는 막힘이 아닌 오류가 섞이면) 예전 판정 그대로다."""
    import contextlib
    import io

    import fanout
    urls = [f"https://blk.kr/p{i}" for i in range(3)]
    g = globals()
    orig = g["target_urls"], g["fetch"]

    def go(fetch_fn):
        g["target_urls"], g["fetch"] = (lambda c, pid, limit: list(urls)), fetch_fn
        try:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                return collect("p", conn=fanout.MainThreadOnly(conn), throttle=0)
        finally:
            g["target_urls"], g["fetch"] = orig

    timeout = "ReadTimeout: HTTPSConnectionPool(host='blk.kr', port=443): Read timed out."
    res = go(lambda u, timeout_=None, **k: {"url": u, "status": None, "error": timeout}
             if not u.endswith("p2") else {"url": u, "status": 403, "error": "HTTP 403"})
    assert res.skipped and not res.failed, f"전부 막혔는데 건너뜀이 아니다: {res}"
    assert "blk.kr" in res.reason and "응답하지 않습니다" in res.reason and "ReadTimeout" not in res.reason, res.reason
    # 서버 오류(503)가 섞이면 막힘이 아니다 — 사이트 쪽 결함일 수 있으니 실패 그대로.
    res = go(lambda u, timeout_=None, **k: {"url": u, "status": 503, "error": "HTTP 503"})
    assert res.failed, f"503 전부를 막힘으로 삼켰다: {res}"


def _concurrency_check(conn) -> None:
    """동시에 가져오되 적재는 한 스레드·urls 순서 — 가짜 fetch 로 돈다(네트워크 0).

    앞 URL 일수록 늦게 끝나게 해서, 끝난 순서대로 적으면 page_audits 순서가 뒤집히게
    만든다. conn 은 fanout.MainThreadOnly 로 싸서 일꾼이 만지면 바로 터진다.
    """
    import contextlib
    import io
    import threading
    import time

    import fanout
    urls = [f"https://c.kr/p{i}" for i in range(8)]
    lock = threading.Lock()
    live = {"now": 0, "peak": 0}
    on: set[int] = set()

    def slow(url, timeout=None):
        with lock:
            live["now"] += 1
            live["peak"] = max(live["peak"], live["now"])
            on.add(threading.get_ident())
        time.sleep(0.01 * (8 - int(url.rsplit("p", 1)[1])))
        with lock:
            live["now"] -= 1
        if url.endswith("p3"):
            return {"url": url, "status": 503, "error": "HTTP 503"}
        if url.endswith("p5"):              # 없는 페이지는 사실이지 오류가 아니다
            return {"url": url, "status": 404, "error": "HTTP 404 · text/html"}
        return audit_html(url, "<html><head><title>t</title></head><body><h1>h</h1></body></html>")

    g = globals()
    orig = g["target_urls"], g["fetch"]
    g["target_urls"], g["fetch"] = (lambda c, pid, limit: list(urls)), slow
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            res = collect("p", conn=fanout.MainThreadOnly(conn), throttle=0)
    finally:
        g["target_urls"], g["fetch"] = orig
    assert (res.ok, res.partial, [e.item for e in res.errors]) == (True, True, [urls[3]]), res
    got = [r["url"] for r in conn.execute(
        "SELECT url FROM page_audits WHERE url LIKE 'https://c.kr/p%' ORDER BY id")]
    assert got == urls, f"적재 순서가 urls 순서가 아니다: {got}"
    assert 1 < live["peak"] <= fanout.LIMITS["own_site"], f"동시 상한: peak={live['peak']}"
    assert threading.get_ident() not in on, "fetch 가 메인 스레드에서 돌았다"


if __name__ == "__main__":
    main()
