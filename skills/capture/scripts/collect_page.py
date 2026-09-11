#!/usr/bin/env python3
"""내 페이지 HTML 감사 — 화면이 "이 페이지의 무엇을 바꿔라"라고 말할 수 있게 하는 재료.

지금까지 이 스킬은 남의 판정(GSC·SERP)만 모았다. "CTR 이 기대의 절반"이라고
말할 수는 있어도 **지금 그 페이지의 title 이 무엇인지**는 몰라서, 처방이 늘
일반론에서 멈췄다("제목을 고치세요"). 이 수집기가 그 구멍을 메운다 — 내 페이지를
직접 한 번 가져와 title·설명·H1·본문 길이·구조화 데이터·canonical·robots 를 적는다.
판정은 여기서 하지 않는다: 무엇이 문제인지는 scoring.page_advice 가 답한다.

대상 URL: 기회에 걸린 검색어의 페이지 → 노출 상위 페이지 순. 고칠 자리부터 본다.

비용: 없다. 남의 API 가 아니라 내 사이트를 여는 것뿐이다. 대신 내 서버에 요청이
가므로 throttle 을 기본 0.5초로 두고, 상한(page_urls, 기본 20)을 넘지 않는다.

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
from datetime import date
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin

sys.path.insert(0, str(Path(__file__).parent))
import collector  # noqa: E402
import db  # noqa: E402
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


def _schema_dates(blob: str) -> tuple[str | None, str | None]:
    """ld+json 의 datePublished/dateModified. 깨진 JSON 이어도 글자로 건진다 —
    _schema_types 와 같은 규칙이다."""
    try:
        data = json.loads(blob)
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
        data = json.loads(blob)
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
        data = json.loads(blob)
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
    return {"url": url, "status": status, "error": None,
            "title": p.title, "meta_description": p.meta_description,
            "h1_json": json.dumps(p.h1, ensure_ascii=False),
            "h2_json": json.dumps(p.h2[:20], ensure_ascii=False),
            "words": words,
            "schema_json": json.dumps(sorted(set(p.schema)), ensure_ascii=False),
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
            "lead_words": p.lead_words(), "author": p.author or ""}


def target_urls(conn, project_id: int, limit: int) -> list[str]:
    """감사할 URL — 고칠 자리부터. 기회에 걸린 페이지 → 노출 상위 페이지 순.

    기회 대상이 URL 이면(색인 막힘 등) 그 자체가 대상이고, 검색어면 그 검색어로
    실제 걸린 페이지가 대상이다(scoring.pages_by_query 가 정본).
    """
    rows = conn.execute(
        "SELECT target FROM opportunities WHERE project_id=? AND status IN ('new','acked')"
        " ORDER BY score DESC LIMIT 100", (project_id,)).fetchall()
    targets = [r["target"] for r in rows]
    by_q = scoring.pages_by_query(
        conn, project_id, [t for t in targets if not t.startswith("http")], top=2)
    out: list[str] = []
    for t in targets:
        if t.startswith("http"):
            out.append(t)
        else:
            out += [pg["page"] for pg in by_q.get(t, [])]
    out += scoring.top_pages(conn, project_id, limit)
    seen, uniq = set(), []
    for u in out:
        if u and u not in seen:
            seen.add(u)
            uniq.append(u)
    return uniq[:limit]


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
    return audit_html(url, r.text, r.status_code)


def collect(project: str, *,
            dry_run: bool = False,
            limit: int | None = None,
            throttle: float | None = None,
            conn=None) -> collector.StageResult:
    """내 페이지를 가져와 감사 결과를 Brain 에 적재한다. sys.exit 호출 없음.

    Args:
        project: 사이트 이름
        dry_run: True 면 가져올 목록만 찍고 종료
        limit: 한 번에 감사할 URL 수(config 키는 page_urls). 0이면 끔 —
            CLI 플래그(--limit)와 이름을 맞춘다. 어긋나면 `--opt pages.limit=5` 가
            TypeError 로 죽는다(collect_index 의 --limit vs index_urls 가 그 사례).
        throttle: 요청 간격(초) — 내 서버를 두드리는 속도다
        conn: 이미 열린 Brain 연결 — 주면 그것을 쓰고 닫지 않는다

    Returns:
        StageResult(ok=...). 사유 있는 비종료는 ok=False, skipped=True.
    """
    ap = _parser()
    with collector.stage(project, conn=conn, dry_run=dry_run) as st:
        conn, p = st.conn, st.project
        s = st.settings(ap, argparse.Namespace(limit=limit, throttle=throttle))
        limit = s["page_urls"]
        if limit <= 0:
            print("[pages] page_urls=0 — 페이지 감사를 끄셨습니다.")
            return st.noop(rows=0)

        urls = target_urls(conn, p["id"], limit)
        if not urls:
            return st.skip("감사할 페이지가 없습니다 — 먼저 gsc 를 수집하세요"
                           " (page 분해가 있어야 어느 URL 인지 알 수 있습니다).")

        print(f"[pages] URL {len(urls)}개 · 비용 없음 · 내 사이트 직접 조회 "
              f"(간격 {st.throttle}초)")
        if st.dry_run:
            for i, u in enumerate(urls, 1):
                print(f"  {i:>3}. {u}")
            return st.noop(rows=0)

        rows: list[dict] = []

        def one(url: str) -> None:
            row = fetch(url)
            # 행은 남긴다 — page_audits.error 는 "못 가져왔다"를 적는 진짜 칸이다.
            # 하지만 거기서 끝내면 실패가 **데이터**가 되어 st.errors 를 못 지나간다:
            # URL 이 전부 죽어도 runs.notes 에 errors=0 이 적히던 자리다.
            rows.append(row)
            if row.get("error"):
                raise collector.ItemFailed(row["error"], status=row.get("status"))
            print(f"  ✓ {url} — title {len(row['title'] or '')}자 · "
                  f"본문 {row['words']}단어 · H1 {len(json.loads(row['h1_json']))}개")

        with st.record("pages") as r:
            done = st.each(urls, one, label=lambda u: u)
            checked = str(date.today())
            db.write_page_audits(conn, p["id"], checked, rows)
            r.api_calls = done
            r.notes = (f"urls={len(rows)}/{len(urls)} checked={checked} "
                       f"{st.err_note}")

        bad = [x for x in rows if x.get("error")]
        print(f"\nsaved {len(rows)} page audits (errors={st.errors})"
              + (f" · 못 가져온 URL {len(bad)}개" if bad else ""))
        # 실제로 감사한 건수로 판정한다 — 전부 못 가져온 것은 완료가 아니다.
        return st.verdict(done, rows=len(rows))


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser()
    collector.add_common(ap)
    collector.add_setting(ap, "--limit", key="page_urls", fallback=20, type=int,
                          help="한 번에 감사할 URL 수. 0이면 끔")
    collector.add_setting(ap, "--throttle", key="throttle", fallback=0.5, type=float,
                          help="요청 간격(초) — 내 서버를 두드리는 속도")
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
    assert target_urls(conn, 1, 1) == ["https://c.kr/a"], "상한을 넘긴다"

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
    print("collect_page self-check ok")


if __name__ == "__main__":
    main()
