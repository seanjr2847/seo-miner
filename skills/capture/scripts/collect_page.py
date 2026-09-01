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
import remote  # noqa: E402
import scoring  # noqa: E402
import serp_adapter  # noqa: E402

# 자기 신원을 밝힌다 — 내 사이트를 여는 것이라 브라우저 위장을 할 이유가 없고,
# 로그에서 이 요청이 무엇인지 알아볼 수 있어야 한다(차단 규칙을 만들 때도 그렇다).
UA = {"User-Agent": "seo-miner/page-audit (+https://github.com/seanjr2847/seo-miner)"}

SKIP_TEXT = {"script", "style", "noscript", "template", "svg", "title"}
MAX_HTML = 2_000_000        # 2MB 를 넘는 문서는 앞부분만 본다 (head 와 본문 초반이면 족하다)


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
        self._text: list[str] = []
        self._stack: list[str] = []
        self._grab = None          # 지금 글자를 모으는 자리: "title" | "h1" | "h2" | "ld"

    # ── 태그 ────────────────────────────────────────────────
    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        self._stack.append(tag)
        if tag == "title" and self.title is None:
            self._grab, self._buf = "title", []
        elif tag in ("h1", "h2"):
            self._grab, self._buf = tag, []
        elif tag == "meta":
            name = (a.get("name") or a.get("property") or "").lower()
            if name == "description" and self.meta_description is None:
                self.meta_description = a.get("content", "").strip()
            elif name == "robots":
                self.robots = a.get("content", "").strip()
        elif tag == "link" and "canonical" in a.get("rel", "").lower():
            self.canonical = urljoin(self.base, a.get("href", "").strip())
        elif tag == "script" and a.get("type", "").lower() == "application/ld+json":
            self._grab, self._buf = "ld", []
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
        if self._grab == tag or (self._grab == "ld" and tag == "script") \
                or (self._grab == "title" and tag == "title"):
            text = " ".join("".join(self._buf).split())
            if self._grab == "title":
                self.title = text
            elif self._grab == "ld":
                self.schema += _schema_types(text)
            elif self._grab == "h1":
                self.h1.append(text)
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
    return {"url": url, "status": status, "error": None,
            "title": p.title, "meta_description": p.meta_description,
            "h1_json": json.dumps(p.h1, ensure_ascii=False),
            "h2_json": json.dumps(p.h2[:20], ensure_ascii=False),
            "words": p.words,
            "schema_json": json.dumps(sorted(set(p.schema)), ensure_ascii=False),
            "canonical": p.canonical, "robots": p.robots,
            "internal_links": p.internal, "external_links": p.external,
            "images": p.images, "images_no_alt": p.images_no_alt}


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
            rows.append(row)
            if row.get("error"):
                print(f"  ✗ {url} — {row['error']}")
            else:
                print(f"  ✓ {url} — title {len(row['title'] or '')}자 · "
                      f"본문 {row['words']}단어 · H1 {len(json.loads(row['h1_json']))}개")

        with st.record("pages") as r:
            done = st.each(urls, one, label=lambda u: u)
            checked = str(date.today())
            db.write_page_audits(conn, p["id"], checked, rows)
            r.api_calls = done
            r.notes = f"urls={len(rows)}/{len(urls)} checked={checked} errors={st.errors}"

        bad = [x for x in rows if x.get("error")]
        print(f"\nsaved {len(rows)} page audits (errors={st.errors})"
              + (f" · 못 가져온 URL {len(bad)}개" if bad else ""))
        return st.done(rows=len(rows))


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
    a = _parser().parse_args()
    if remote.dispatch(a, "pages"):   # 원격 사이트면 서버가 돈다
        return
    r = collect(a.project, dry_run=a.dry_run, limit=a.limit, throttle=a.throttle)
    if not r.ok and r.reason:
        sys.exit(r.reason)


def _selfcheck() -> None:
    html = """<html><head><title>  밀리아 제거 비용과 방법  </title>
      <meta name="description" content="밀리아 제거 가격과 회복 기간">
      <meta name="robots" content="index,follow">
      <link rel="canonical" href="/milia">
      <script type="application/ld+json">{"@type":"Article","author":{"@type":"Person"}}</script>
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
    # script·title 안 글자는 본문이 아니다 — 세면 얇은 페이지가 두꺼워 보인다
    assert a["words"] == 10, a["words"]       # h1 2 + h2 1 + p 5 + a 2
    assert audit_html("https://c.kr/x", "<html><body><script>가 나 다 라 마</script></body></html>"
                      )["words"] == 0, "script 본문이 단어로 새어 들어간다"

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
    print("collect_page self-check ok")


if __name__ == "__main__":
    main()
