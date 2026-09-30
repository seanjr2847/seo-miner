#!/usr/bin/env python3
"""이번 달 할 일 — 열린 기회를 **우리 페이지 하나**씩 묶고, 실제로 읽은 증거와 AI 수정안을 붙인다.

기회는 검색어마다 한 줄이다. 같은 페이지에 걸린 검색어 다섯이 기회 다섯 줄로 서고, 사람이
그걸 읽어 "결국 이 페이지 하나를 고치면 된다"로 모으고, 상위 글을 열어 보고, 요청문을
도구에 붙여 넣어야 고칠 안이 나왔다. 이 단계가 그 셋을 사람 손 없이 한다:

  1. 후보 — 열린 기회(new·acked) 중 '있는 페이지 고치기' 종류(PLAY_KINDS). **심사 대기 중인
     것도 넣는다**(dashboard.gather(gated=False)) — 이 흐름은 도구가 고른다. 고칠 페이지는
     요청문과 같은 규칙(brief.page_of → o.brief["page"])으로 정한다 — 여기서 두 번째 규칙을
     만들면 화면은 A 를, 할 일은 B 를 고치라고 한다. 페이지가 없는 기회는 뺀다. 관찰 중인
     페이지(d.holds)와 최근에 PR 을 만들었거나 사람이 뺀 페이지(PLAY_HOLD_DAYS)도 뺀다.
     주소 단위로 묶어(scoring.url_key) 점수 합으로 줄 세우고 위에서 PLAY_MAX 개.
  2. 증거 — 우리 페이지와 검색결과 상위(serp_top) 중 남의 페이지 PLAY_SERP_N 장을 직접 연다
     (collect_page.fetch). 봇 차단(collect_page.blocked)이면 DataForSEO On-Page
     content_parsing 으로 읽는다. 제목·H1·H2·단어 수, AI 요약(rank_by_kw), 그리고 gaps —
     상위 글 둘 이상에 있는데 우리에 없는 소제목(규칙으로 뽑는다, AI 요약이 아니다).
  3. 수정안 — 프롬프트는 요청문(o.brief body)을 그대로 쓰고 증거를 덧붙인다(글을 두 벌 만들지
     않는다). OpenRouter 는 collect_gap.openrouter_json 한 길이다(잘림·JSON 없음을 오류로
     올리는 판정이 거기 있다). 실패하면 result={"error": ...} 로 남기고 다음 묶음으로 간다.
  4. 문서 — 결과와 증거로 사람이 바로 쓰는 마크다운(복사·내려받기·PR 입력).

신선함: 마지막 할 일이 PLAY_FRESH_DAYS 안이고 후보 페이지 집합이 같고 실패한 줄이 없으면
건너뛴다(자동 런이 매일 돈을 쓰지 않게). --force 는 그 판정을 끈다 — 화면의 버튼
(/api/run {stages:"plays"}, 서버가 force 를 싣는다)과 `/capture plays <사이트>` 가 쓴다.
새로 만들면 'new' 인 옛 할 일을 갈아 끼우고, applied·dismissed 는 둔다(db.replace_plays).

돈: 페이지 읽기(직접은 무료, DataForSEO 폴백은 응답의 실청구액), AI 한 번(묶음마다,
응답 usage.cost). 합계를 StageResult.cost 에 싣는다.

페이로드 꼴(d.plays)은 db.list_plays 가 풀고 dashboard._axis_plays 가 싣는다.

Usage:
  python plays.py --project NAME [--force] [--max 4] [--serp 3] [--dry-run]
  python plays.py                                  # self-check
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import brief  # noqa: E402
import collect_page  # noqa: E402
import collector  # noqa: E402
import dashboard  # noqa: E402
import db  # noqa: E402
import fanout  # noqa: E402
import scoring  # noqa: E402
import serp_adapter  # noqa: E402

# 모델 — 수정안은 긴 한국어 문안이라 판정용 작은 모델(collect_gap.ROLE_MODEL)과 따로 둔다.
PLAY_MODEL = "anthropic/claude-sonnet-5.5"
PLAY_MAX_TOKENS = 12000
PLAY_TIMEOUT = 300          # 긴 답 한 번 — 판정용 기본(TIMEOUTS["openrouter"])보다 넉넉히
PLAY_MAX = 4                # 한 번에 내는 할 일 수
PLAY_SERP_N = 3             # 묶음마다 직접 여는 상위 글 수
PLAY_KW_N = 3               # 묶음마다 검색결과를 보는 검색어 수(점수 순)
PLAY_SERP_ROWS = 5          # 검색어마다 증거에 싣는 상위 줄 수
PLAY_FRESH_DAYS = 7
PLAY_HOLD_DAYS = 30         # PR 을 만들었거나(applied) 사람이 뺀(dismissed) 페이지는 이만큼 안 다시 낸다
PLAY_GAPS = 12
PROMPT_BRIEF_MAX = 12000    # 요청문 본문 상한(글자) — 프롬프트가 요청문 하나로 다 차지 않게
# 묶음 하나의 AI 한 번 **추정** 단가($) — 긴 입력(요청문+증거 ~15k 토큰)과 긴 답(최대
# PLAY_MAX_TOKENS). 실청구는 응답 usage.cost 다. 잔액 카나리아(run_all.preflight)만 쓴다.
PLAY_CALL_USD = 0.25


def estimate_usd() -> float:
    """이번 할 일 만들기의 추정 비용 — 묶음 수 상한 × PLAY_CALL_USD."""
    return round(PLAY_MAX * PLAY_CALL_USD, 2)

# '있는 페이지 고치기' 종류 — 첫 판은 이것만. content_gap 은 밀림(weak)만이다(없음은 새 글이다).
PLAY_KINDS = ("striking_distance", "aio_exposure", "ctr_gap", "content_gap")

ON_PAGE_PARSE = collect_page.ON_PAGE_PARSE


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _age_days(ts: str | None) -> float | None:
    try:
        return (_utcnow() - datetime.fromisoformat(str(ts)[:19])).total_seconds() / 86400
    except (TypeError, ValueError):
        return None


def _key(url: str) -> str:
    """묶는 열쇠 — 추적 꼬리표를 떼고(collect_page.clean_url) 표기 차이를 접는다(scoring.url_key)."""
    return scoring.url_key(collect_page.clean_url(url or ""))


# ── 1. 후보 ──────────────────────────────────────────────────────────────────

def _is_weak_gap(o: dict) -> bool:
    """콘텐츠 공백 중 '페이지가 있는데 밀림' — 갈래를 모르면 요청문이 고른 꼴(fix_page)을 본다."""
    gk = o.get("gap_kind")
    if gk:
        return gk == "weak"
    return (o.get("brief") or {}).get("shape") == "fix_page"


def candidates(d: dict, *, limit: int = PLAY_MAX, exclude=()) -> list[dict]:
    """gather 페이로드 → 페이지 묶음 목록(점수 합 순, 위에서 limit 개).

    묶음: {"key", "page", "score", "opps": [기회…(점수 순)], "keywords": [...]}.
    exclude 는 뺄 페이지 열쇠(_key) — 최근 PR·뺀 페이지.
    """
    skip = set(exclude) | {_key(h["page"]) for h in d.get("holds") or [] if h.get("page")}
    groups: dict[str, dict] = {}
    for o in d.get("opps") or []:
        if o.get("status") not in scoring.OPEN_STATUSES or o.get("kind") not in PLAY_KINDS:
            continue
        if o["kind"] == "content_gap" and not _is_weak_gap(o):
            continue
        page = (o.get("brief") or {}).get("page")
        if not page:
            continue
        k = _key(page)
        if not k or k in skip:
            continue
        g = groups.setdefault(k, {"key": k, "page": page, "score": 0.0, "opps": []})
        g["opps"].append(o)
        g["score"] += float(o.get("score") or 0)
    out = sorted(groups.values(), key=lambda g: (-g["score"], g["key"]))[:max(0, limit)]
    for g in out:
        g["opps"].sort(key=lambda o: -float(o.get("score") or 0))
        g["page"] = g["opps"][0]["brief"]["page"]       # 가장 무거운 기회가 고른 표기 그대로
        g["score"] = round(g["score"], 1)
        g["keywords"] = list(dict.fromkeys(
            str(o["target"]) for o in g["opps"] if not str(o["target"]).startswith("http")))
    return out


def _held_pages(conn, pid: int) -> set[str]:
    """최근 PLAY_HOLD_DAYS 안에 PR 을 만들었거나 사람이 뺀 할 일의 페이지."""
    out = set()
    for p in db.list_plays(conn, pid, statuses=("applied", "dismissed")):
        age = _age_days(p["created_at"])
        if age is not None and age < PLAY_HOLD_DAYS:
            out.add(_key(p["page"]))
    return out


def fresh(conn, pid: int, keys: set[str]) -> str | None:
    """지금 있는 할 일을 그대로 둬도 되나 — 되면 사유 한 줄, 아니면 None."""
    cur = db.list_plays(conn, pid, statuses=("new",))
    if not cur:
        return None
    if any((p["result"] or {}).get("error") for p in cur):
        return None                     # 지난번에 못 만든 줄이 있다 — 다시 해 본다
    if {_key(p["page"]) for p in cur} != set(keys):
        return None
    ages = [a for a in (_age_days(p["created_at"]) for p in cur) if a is not None]
    if not ages or min(ages) >= PLAY_FRESH_DAYS:
        return None
    return (f"할 일이 {int(min(ages))}일 전에 만든 그대로입니다(후보 페이지가 같습니다) — "
            f"{PLAY_FRESH_DAYS}일 안이라 다시 만들지 않습니다. 새로 만들려면 --force.")


# ── 2. 증거 ──────────────────────────────────────────────────────────────────

def _jl(v) -> list:
    try:
        x = json.loads(v) if isinstance(v, str) else v
    except (TypeError, ValueError):
        return []
    return [str(i) for i in x] if isinstance(x, list) else []


# 한 장 읽기(직접 → 막히면 DataForSEO On-Page)는 페이지 점검(collect_page)과 한 벌이다 —
# 봇 차단 사이트(gucci)에서 페이지 점검도 같은 폴백을 쓴다. 정본은 collect_page 에 있다.
parse_content = collect_page.parse_content
read_page = collect_page.read_page


def gaps(ours: list[str], tops: list[list[str]], *, min_pages: int = 2,
         cap: int = PLAY_GAPS) -> list[str]:
    """상위 글 min_pages 곳 이상에 있고 우리에 없는 소제목 — 규칙이다(AI 가 아니다).

    같다의 기준: scoring.norm 이 같거나, 4글자 이상에서 한쪽이 다른 쪽을 품는다
    ('가격' 과 '가격 정리' 는 같은 구간으로 본다). 대표 문구는 처음 본 것.
    """
    def same(a: str, b: str) -> bool:
        return a == b or (min(len(a), len(b)) >= 4 and (a in b or b in a))

    mine = [k for k in (scoring.norm(h) for h in ours) if k]
    clusters: list[dict] = []        # {"key", "text", "pages": set}
    for i, hs in enumerate(tops):
        for h in hs:
            k = scoring.norm(h)
            if len(k) < 2:
                continue
            c = next((c for c in clusters if same(c["key"], k)), None)
            if c is None:
                clusters.append({"key": k, "text": " ".join(str(h).split()), "pages": {i}})
            else:
                c["pages"].add(i)
    hit = [c for c in clusters if len(c["pages"]) >= min_pages
           and not any(same(c["key"], m) for m in mine)]
    hit.sort(key=lambda c: -len(c["pages"]))           # 안정 정렬 — 같은 수면 처음 본 순
    return [c["text"] for c in hit[:cap]]


def evidence(g: dict, d: dict, *, fetch, post, dfs: bool, serp_n: int = PLAY_SERP_N
             ) -> tuple[dict, float, list[str]]:
    """묶음 하나의 증거 → (evidence, 비용, 못 읽은 주소의 사유들). Brain 을 안 만진다(일꾼 스레드)."""
    page = g["page"]
    cost, misses = 0.0, []
    info, c, why = read_page(page, fetch=fetch, post=post, dfs=dfs)
    cost += c
    audit = (d.get("page_audits") or {}).get(page) or {}
    if info["fetched_via"] == "none" and audit:
        # 지금은 못 읽었어도 페이지 점검(pages 단계)이 전에 읽은 값이 있다 — 비워 두지 않는다.
        info.update(title=audit.get("title"), h1=(_jl(audit.get("h1_json")) or [None])[0],
                    meta=audit.get("meta_description"), headings=_jl(audit.get("h2_json")),
                    words=audit.get("words"), audit_date=audit.get("checked_date"))
    if why:
        misses.append(f"{page}: {why}")
    our = {"url": page, **info}

    serp_top = d.get("serp_top") or {}
    kws = [k for k in g["keywords"] if serp_top.get(k)][:PLAY_KW_N]
    rows, to_read = [], []
    for kw in kws:
        for r in serp_top[kw][:PLAY_SERP_ROWS]:
            row = {"keyword": kw, "position": r.get("position"), "url": r.get("url"),
                   "domain": r.get("domain"), "title": r.get("title"),
                   "is_own": int(bool(r.get("is_own"))), "headings": [], "words": None}
            rows.append(row)
            u = r.get("url")
            if row["is_own"] or not u or _key(u) == _key(page):
                continue
            if u not in to_read and len(to_read) < serp_n:
                to_read.append(u)
    read: dict[str, dict] = {}
    for u in to_read:
        got, c, why = read_page(u, fetch=fetch, post=post, dfs=dfs)
        cost += c
        read[u] = got
        if why:
            misses.append(f"{u}: {why}")
    for row in rows:
        if row["is_own"]:
            row.update(headings=our.get("headings") or [], words=our.get("words"))
        elif row["url"] in read:
            got = read[row["url"]]
            row.update(headings=got["headings"], words=got["words"],
                       fetched_via=got["fetched_via"])

    aio = []
    rank_by_kw = d.get("rank_by_kw") or {}
    for kw in g["keywords"]:
        r = rank_by_kw.get(kw)
        if r and r.get("aio") is not None:
            aio.append({"keyword": kw, "present": r.get("aio"), "cited": r.get("aio_cited"),
                        "domains": list(r.get("aio_domains") or [])})

    tops = [read[u]["headings"] for u in to_read if read[u]["headings"]]
    return ({"our": our, "serp": rows, "aio": aio,
             "gaps": gaps(our.get("headings") or [], tops)}, round(cost, 4), misses)


# ── 3. 수정안 ────────────────────────────────────────────────────────────────

def _lang(g: dict, d: dict) -> tuple[str, str]:
    """문안 언어 (코드, 모델에게 말할 영어 이름) — 페이지 언어가 먼저, 없으면 사이트 언어.
    판정은 요청문의 언어 줄과 같은 함수(brief.page_locale)다."""
    locale = db.project_locale(d.get("project") or {})
    audit = (d.get("page_audits") or {}).get(g["page"])
    pl = brief.page_locale(audit, g["page"])
    lang = pl[0] if pl else serp_adapter.lang_of(locale)
    return lang, serp_adapter.LANG_NAME.get(lang, "English")


def _ev_for_prompt(ev: dict) -> dict:
    """프롬프트에 싣는 증거 — 남의 글은 한 줄로 가두고(brief._ext) 길이를 자른다."""
    x = brief._ext
    return {
        "our": {k: (x(v, 200) if isinstance(v, str) else v) for k, v in ev["our"].items()
                if k != "headings"} | {"headings": [x(h, 120) for h in ev["our"].get("headings") or []][:30]},
        "serp": [{"keyword": r["keyword"], "position": r["position"], "domain": r["domain"],
                  "url": r["url"], "title": x(r["title"], 160), "is_own": r["is_own"],
                  "headings": [x(h, 120) for h in r["headings"]][:15], "words": r["words"]}
                 for r in ev["serp"]],
        "aio": ev["aio"],
        "gaps": [x(h, 120) for h in ev["gaps"]],
    }


def prompt(g: dict, d: dict, ev: dict) -> str:
    """요청문(o.brief body) + 묶인 다른 기회 + 증거 → 엄격한 JSON 하나를 달라는 프롬프트."""
    lead = g["opps"][0]
    body = str((lead.get("brief") or {}).get("body") or "")
    if len(body) > PROMPT_BRIEF_MAX:
        body = body[:PROMPT_BRIEF_MAX] + "\n…(요청문이 길어 여기서 자름)"
    lang, lang_name = _lang(g, d)
    t_max, d_max = brief.limits(lang)
    others = [f"- [{o.get('label') or o['kind']}] {brief._ext(o['target'], 120)} — "
              f"{brief._ext(o.get('reasoning') or '', 240)}" for o in g["opps"]]
    schema = {
        "summary": "왜 이 페이지를 이렇게 고치나 — 두세 문장 (한국어)",
        "why": ["근거 한 줄씩 — 아래 증거·요청문에서 인용 (한국어)"],
        "title": {"now": "지금 title (증거 our.title 그대로)", "new": f"고친 title ({lang_name})"},
        "meta": {"now": "지금 meta description", "new": f"고친 meta description ({lang_name})"},
        "h1": {"now": "지금 H1", "new": f"고친 H1 ({lang_name})"},
        "sections": [{"h2": f"넣을 H2 ({lang_name})",
                      "draft": f"바로 붙여 넣을 문단 초안 ({lang_name})", "for": ["맡는 검색어"]}],
        "links": [{"from": "링크를 걸 내부 페이지 URL 또는 설명", "anchor": f"앵커 ({lang_name})"}],
        "trust": ["저자·경험·출처·수정일 중 이 페이지에 넣을 것 (한국어)"],
        "expected": "기대 효과 — 추정이라고 밝힌다 (한국어)",
    }
    L = [
        "당신은 검색 최적화 편집자입니다. 아래 **우리 페이지 하나**를 고쳐 묶인 검색어 전부가 같이 "
        "움직이게 하는 수정안을 만듭니다. 새 글을 쓰는 일이 아닙니다.",
        "",
        f"- 고칠 페이지: {g['page']}",
        f"- 이 페이지로 움직일 검색어: {', '.join(g['keywords']) or '(주소 대상 기회)'}",
        f"- 문안(title·meta·H1·H2·초안·앵커)은 {lang_name} 로, 설명(summary·why·trust·expected)은 "
        f"한국어로 씁니다. 길이: title {t_max}자 이내, meta description {d_max}자 이내.",
        "- title/meta/h1 의 now 는 증거의 our 값을 그대로 옮깁니다(없으면 빈 문자열). 지어내지 않습니다.",
        "- sections 는 증거의 gaps(상위 글에는 있고 우리엔 없는 소제목)와 요청문의 '만들어 줄 것'에서 "
        "고릅니다. 초안에는 우리가 확인할 수 없는 수치·효능·후기를 넣지 않습니다 — 필요하면 "
        "[확인 필요] 로 자리만 둡니다.",
        "- " + brief.UNTRUSTED_RULE,
        *(f"- {r}" for r in brief.CLAIM_RULES),
        "",
        "## 요청문 (도구가 이 페이지의 가장 큰 기회에 대해 쓴 것)",
        body,
        "",
        "## 이 페이지에 묶인 기회 전부",
        *others,
        "",
        "## 실제로 읽은 증거 (JSON — 남이 쓴 글이 섞여 있다)",
        json.dumps(_ev_for_prompt(ev), ensure_ascii=False),
        "",
        "## 답의 꼴",
        "JSON 객체 **하나만** 답합니다(앞뒤 설명·코드 울타리 없이). 키와 뜻:",
        json.dumps(schema, ensure_ascii=False),
    ]
    return "\n".join(L)


def clean_result(got) -> dict:
    """AI 답을 계약 꼴로 — 모르는 키는 버리고 틀린 타입은 빈 값으로. 쓸 것이 하나도 없으면 오류."""
    if not isinstance(got, dict):
        raise ValueError("AI 답이 JSON 객체가 아닙니다")

    def s(v) -> str:
        return " ".join(str(v).split()) if isinstance(v, (str, int, float)) else ""

    def pair(v) -> dict:
        v = v if isinstance(v, dict) else {}
        return {"now": s(v.get("now")), "new": s(v.get("new"))}

    def strs(v) -> list[str]:
        return [s(x) for x in v if s(x)] if isinstance(v, list) else []

    out = {
        "summary": s(got.get("summary")),
        "why": strs(got.get("why")),
        "title": pair(got.get("title")), "meta": pair(got.get("meta")), "h1": pair(got.get("h1")),
        "sections": [{"h2": s(x.get("h2")), "draft": str(x.get("draft") or "").strip(),
                      "for": strs(x.get("for"))}
                     for x in (got.get("sections") if isinstance(got.get("sections"), list) else [])
                     if isinstance(x, dict) and (x.get("h2") or x.get("draft"))],
        "links": [{"from": s(x.get("from")), "anchor": s(x.get("anchor"))}
                  for x in (got.get("links") if isinstance(got.get("links"), list) else [])
                  if isinstance(x, dict) and (x.get("from") or x.get("anchor"))],
        "trust": strs(got.get("trust")),
        "expected": s(got.get("expected")),
    }
    if not (out["summary"] or out["sections"] or out["title"]["new"] or out["h1"]["new"]):
        raise ValueError("AI 답에 수정안이 없습니다(summary·sections·title·h1 이 전부 비었습니다)")
    return out


def _ask_openrouter(text: str) -> tuple[dict, float]:
    import collect_gap
    usage: dict = {}
    got = collect_gap.openrouter_json(text, model=PLAY_MODEL, max_tokens=PLAY_MAX_TOKENS,
                                      timeout=PLAY_TIMEOUT, usage=usage)
    try:
        cost = float(usage.get("cost") or 0.0)
    except (TypeError, ValueError):
        cost = 0.0
    return got, cost


# ── 4. 문서 ──────────────────────────────────────────────────────────────────

def title_of(g: dict, ev: dict) -> str:
    """한 줄 제목 — 페이지 이름(우리 title, 없으면 경로) — 무엇이 움직이나."""
    from urllib.parse import urlsplit
    name = (ev["our"].get("title") or ev["our"].get("h1") or "").strip()
    if not name:
        path = urlsplit(g["page"]).path.strip("/")
        name = path.split("/")[-1] if path else "홈"
    name = name if len(name) <= 40 else name[:39] + "…"
    labels = list(dict.fromkeys(str(o.get("label") or o["kind"]) for o in g["opps"]))
    n = len(g["keywords"])
    tail = " · ".join(labels[:2]) + (f" · 검색어 {n}개" if n else "")
    return f"{name} — {tail}"


def render_markdown(p: dict) -> str:
    """할 일 한 건 → 마크다운. 남의 글은 한 줄로 가둔다(brief._ext)."""
    x = brief._ext
    ev, res = p.get("evidence") or {}, p.get("result") or {}
    L = [f"# {p['title']}", "", f"- 고칠 페이지: {p['page']}"]
    if p.get("keywords"):
        L.append(f"- 움직일 검색어: {', '.join(p['keywords'])}")
    L.append(f"- 기대 이득 점수: {p.get('score')}" + (f" · 모델 {p['model']}" if p.get("model") else ""))
    L.append("")
    if res.get("error"):
        L += ["## 수정안을 못 만들었습니다", f"이유: {res['error']}", "",
              "아래 증거는 실제로 읽은 것 그대로입니다.", ""]
    else:
        if res.get("summary") or res.get("why"):
            L += ["## 왜 이 페이지를 이렇게 고치나", res.get("summary") or ""]
            L += [f"- {w}" for w in res.get("why") or []]
            L.append("")
        pairs = [(lab, res.get(k) or {}) for lab, k in (("title", "title"),
                                                        ("meta description", "meta"),
                                                        ("H1", "h1"))]
        if any(v.get("new") for _, v in pairs):
            L += ["## 지금 → 고친 값", "| 자리 | 지금 | 고친 값 |", "| --- | --- | --- |"]
            L += [f"| {lab} | {x(v.get('now')) or '—'} | {x(v.get('new')) or '—'} |"
                  for lab, v in pairs if v.get("new")]
            L.append("")
        if res.get("sections"):
            L.append("## 넣을 구간")
            for s in res["sections"]:
                L += [f"### {s.get('h2') or '(소제목 없음)'}", s.get("draft") or ""]
                if s.get("for"):
                    L.append(f"_맡는 검색어: {', '.join(s['for'])}_")
                L.append("")
        if res.get("links"):
            L += ["## 내부 링크"] + [f"- {l_['from']} → 앵커 \"{l_['anchor']}\"" for l_ in res["links"]]
            L.append("")
        if res.get("trust"):
            L += ["## 신뢰 신호"] + [f"- {t}" for t in res["trust"]] + [""]
        if res.get("expected"):
            L += ["## 기대 효과 (추정)", res["expected"], ""]
    our = ev.get("our") or {}
    L += ["## 증거 요약",
          f"- 우리 페이지: title {x(our.get('title')) or '—'} · H1 {x(our.get('h1')) or '—'} · "
          f"본문 {our.get('words') if our.get('words') is not None else '—'}단어 "
          f"(읽은 길: {our.get('fetched_via') or 'none'})"]
    for r in ev.get("serp") or []:
        mark = " (우리)" if r.get("is_own") else ""
        hs = f" · 소제목 {len(r['headings'])}개" if r.get("headings") else ""
        L.append(f"- 「{x(r['keyword'], 60)}」 {r.get('position') or '—'}위 {r.get('domain') or ''}"
                 f"{mark} — {x(r.get('title'), 120) or '—'}{hs}")
    for a in ev.get("aio") or []:
        state = ("AI 요약 없음" if not a.get("present") else
                 "AI 요약에 인용됨" if a.get("cited") else "AI 요약에 인용 안 됨")
        doms = f" — 인용된 곳: {', '.join(a['domains'][:5])}" if a.get("domains") else ""
        L.append(f"- 「{x(a['keyword'], 60)}」 {state}{doms}")
    if ev.get("gaps"):
        L.append("- 상위 글엔 있고 우리엔 없는 소제목: " + " · ".join(x(h, 80) for h in ev["gaps"]))
    return "\n".join(L).rstrip() + "\n"


# ── 단계 ────────────────────────────────────────────────────────────────────

def collect(project: str, *,
            dry_run: bool = False,
            force: bool = False,
            max: int | None = None,         # noqa: A002 — CLI 플래그(--max)와 이름을 맞춘다
            serp: int | None = None,
            conn=None,
            fetch=None,
            post=None,
            ask=None) -> collector.StageResult:
    """열린 기회 → 페이지 묶음 → 증거 → AI 수정안 → plays 표.

    Args:
        project: 사이트 이름
        dry_run: True 면 묶음과 읽을 계획만 찍는다(외부 호출 없음)
        force: 신선함 판정(PLAY_FRESH_DAYS · 같은 후보)을 끈다 — 사람이 누른 것
        max: 낼 할 일 수(설정 키 plays_max, 기본 PLAY_MAX)
        serp: 묶음마다 직접 여는 상위 글 수(설정 키 plays_serp, 기본 PLAY_SERP_N)
        conn: 이미 열린 Brain — 주면 그것을 쓰고 닫지 않는다
        fetch: (url) -> page_audits 꼴 한 줄. 기본 collect_page.fetch
        post: (path, body) -> (result, cost). 기본 serp_adapter.post_dataforseo
        ask: (prompt) -> (dict, cost). 기본 OpenRouter(collect_gap.openrouter_json)
    """
    ap = _parser()
    fetch = fetch or collect_page.fetch
    dfs = post is not None or serp_adapter.has_dataforseo()
    post = post or serp_adapter.post_dataforseo
    with collector.stage(project, conn=conn, dry_run=dry_run) as st:
        conn, p = st.conn, st.project
        s = st.settings(ap, argparse.Namespace(max=max, serp=serp))
        n_max, n_serp = s["plays_max"], s["plays_serp"]
        if ask is None:
            if not os.environ.get("OPENROUTER_API_KEY") and not dry_run:
                return st.skip("키가 없어 건너뜁니다. OPENROUTER_API_KEY 를 넣으면 기회를 페이지별 "
                               "할 일로 묶고 수정안을 씁니다. 발급: https://openrouter.ai/keys")
            ask = _ask_openrouter
        if n_max <= 0:
            return st.noop(reason="plays_max=0 — 이번 달 할 일을 끄셨습니다")

        d = dashboard.gather(conn, p, gated=False)
        groups = candidates(d, limit=n_max, exclude=_held_pages(conn, p["id"]))
        if not groups:
            return st.skip("할 일로 묶을 기회가 없습니다 — 고칠 페이지가 정해진 열린 기회"
                           f"({', '.join(PLAY_KINDS)})가 없습니다. 먼저 gaps 를 돌리세요.")
        keys = {g["key"] for g in groups}
        why_fresh = None if force else fresh(conn, p["id"], keys)
        if why_fresh:
            return st.skip(why_fresh)

        print(f"[plays] 페이지 {len(groups)}곳 · 묶음마다 상위 글 {n_serp}장 읽기 + AI 한 번 "
              f"({PLAY_MODEL})")
        for i, g in enumerate(groups, 1):
            print(f"  {i}. {g['page']} — 기회 {len(g['opps'])}건 · 점수 {g['score']}")
        if st.dry_run:
            return st.noop(rows=0)

        made: list[dict] = []
        total = 0.0
        fatal: list[str] = []

        def work(g: dict) -> dict:
            # 일꾼 스레드 — 네트워크만(Brain 을 안 만진다). AI 실패는 값으로 돌려준다:
            # 증거는 이미 샀고, 화면은 증거라도 보여 준다(result={"error"}).
            ev, cost, misses = evidence(g, d, fetch=fetch, post=post, dfs=dfs, serp_n=n_serp)
            try:
                got, c = ask(prompt(g, d, ev))
                cost += float(c or 0)
                result, err = clean_result(got), None
            except collector.Fatal as e:
                # 잔액 없음(402)·키 없음 같은 치명 오류도 증거는 버리지 않는다 — 이미 산 것이고
                # 화면은 증거라도 보여 준다. 단계는 저장한 뒤 그 이유로 실패시킨다(알림·출시 문).
                # gucci: 페이지 4곳을 다 읽고 AI 에서 402 가 나 증거까지 통째로 사라졌다.
                err = str(e)[:300]
                return {"ev": ev, "cost": round(cost, 4), "misses": misses,
                        "result": {"error": err}, "err": err, "fatal": err}
            except Exception as e:
                err = f"{type(e).__name__}: {e}"[:300]
                result = {"error": err}
            return {"ev": ev, "cost": round(cost, 4), "misses": misses, "result": result,
                    "err": err}

        def write(g: dict, out: dict) -> None:
            nonlocal total
            total += out["cost"]
            for m in out["misses"]:
                print(f"  · 못 읽음 — {m}")
            play = {"page": g["page"], "keywords": g["keywords"],
                    "opp_ids": [o["id"] for o in g["opps"]], "score": g["score"],
                    "evidence": out["ev"], "result": out["result"], "model": PLAY_MODEL,
                    "cost": out["cost"], "status": "new"}
            play["title"] = title_of(g, out["ev"])
            play["markdown"] = render_markdown(play)
            made.append(play)
            if out.get("fatal"):
                fatal.append(out["fatal"])
            if out["err"]:
                # 줄은 남긴다(증거는 쓸모 있다) — 실패는 오류 목록으로 세어 요약·notes 에 싣는다.
                st.fail(out["err"], item=g["page"], kind="plays")
            else:
                print(f"  ✓ {g['page']} — 구간 {len(out['result']['sections'])}개 · "
                      f"${out['cost']:.4f}")

        with st.record("plays") as r:
            fanout.each(st, groups, work, write, workers=min(len(groups), PLAY_MAX),
                        label=lambda g: g["page"])
            ok = sum(1 for m in made if not (m["result"] or {}).get("error"))
            if made:
                db.replace_plays(conn, p["id"], made)
            r.api_calls = len(made)
            r.cost = total
            r.notes = f"pages={len(made)} opps={sum(len(g['opps']) for g in groups)} {st.err_note}"
        print(f"\nsaved {len(made)} plays (수정안 {ok}건) · ${total:.4f}")
        if fatal:
            raise collector.Fatal(fatal[0])
        return st.verdict(ok, rows=len(made), cost=round(total, 4))


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser()
    collector.add_common(ap)
    ap.add_argument("--force", action="store_true",
                    help="마지막 할 일이 신선해도 다시 만든다")
    collector.add_setting(ap, "--max", key="plays_max", fallback=PLAY_MAX, type=int,
                          help=f"낼 할 일 수. 기본 {PLAY_MAX}. 0이면 끔")
    collector.add_setting(ap, "--serp", key="plays_serp", fallback=PLAY_SERP_N, type=int,
                          help=f"묶음마다 직접 여는 상위 글 수. 기본 {PLAY_SERP_N}")
    return ap


def main() -> None:
    if len(sys.argv) == 1:
        _selfcheck()
        return
    collector.cli("plays")


# ── 자체 점검 ────────────────────────────────────────────────────────────────

def _selfcheck() -> None:
    """임시 Brain + 가짜 fetch·DataForSEO·AI. 외부 호출 0건."""
    import contextlib
    import io
    import tempfile

    os.environ["CAPTURE_HOME"] = str(Path(tempfile.mkdtemp(prefix="seo-miner-plays-selftest-")))
    os.environ.pop("OPENROUTER_API_KEY", None)
    conn = db.connect()
    conn.execute("INSERT INTO projects(name, type, domain, locale) "
                 "VALUES('pl','saas','s.kr','ko-KR')")
    pid = conn.execute("SELECT id FROM projects WHERE name='pl'").fetchone()[0]
    day = "2026-09-20"
    # GSC — 검색어가 걸린 페이지(brief.page_of 가 이걸로 고른다)
    gsc = [("가방", "https://s.kr/bags", 900, 6.0), ("토트백", "https://s.kr/bags", 400, 8.0),
           ("구찌가방", "https://s.kr/bags", 300, 7.0), ("지갑", "https://s.kr/wallet", 500, 9.0),
           ("벨트", "https://s.kr/belt", 200, 11.0)]
    for q, page, imp, pos in gsc:
        conn.execute("INSERT INTO gsc_snapshots(project_id,snapshot_date,period_days,query,page,"
                     "clicks,impressions,ctr,position) VALUES(?,?,?,?,?,?,?,?,?)",
                     (pid, day, 28, q, page, 5, imp, 5 / imp, pos))
    opps = [("striking_distance", "가방", 90.0), ("ctr_gap", "토트백", 50.0),
            ("aio_exposure", "구찌가방", 33.4),          # 심사 대기(판정 없음) — 그래도 후보
            ("striking_distance", "지갑", 60.0),
            ("striking_distance", "모자", 80.0),          # 걸린 페이지 없음 — 빠진다
            ("striking_distance", "벨트", 10.0),
            ("ai_citation_gap", "가방 추천", 99.0)]       # 고치기 종류가 아니다 — 빠진다
    for kind, t, sc in opps:
        conn.execute("INSERT INTO opportunities(project_id,kind,target,score,reasoning,status)"
                     " VALUES(?,?,?,?,?,'new')", (pid, kind, t, sc, f"{t} 근거"))
    # 가방만 '작업' 판정 — 나머지 검색어는 심사 대기다
    db.set_verdicts(conn, pid, [scoring.norm("가방")], "work")
    # 검색결과 상위와 AI 요약
    for kw in ("가방", "구찌가방", "토트백", "지갑", "벨트"):
        conn.execute("INSERT INTO keywords(project_id,keyword,locale,source,is_active)"
                     " VALUES(?,?,?,?,1)", (pid, kw, "ko-KR", "seed"))
    kid = {r["keyword"]: r["id"] for r in conn.execute("SELECT id, keyword FROM keywords")}
    ts = "2026-09-21 00:00:00"
    db.write_rank_snapshot(conn, kid["구찌가방"], 7, "https://s.kr/bags", checked_at=ts,
                           aio_present=1, aio_cited=0, aio_domains=["a.com", "b.com"])
    db.write_rank_snapshot(conn, kid["가방"], 6, "https://s.kr/bags", checked_at=ts)
    db.write_serp_results(conn, kid["가방"], [
        {"position": 1, "url": "https://a.com/bags", "title": "A 가방", "domain": "a.com"},
        {"position": 2, "url": "https://b.com/bags", "title": "B 가방", "domain": "b.com"},
        {"position": 3, "url": "https://c.com/bags", "title": "C 가방", "domain": "c.com"},
        {"position": 4, "url": "https://d.com/bags", "title": "D 가방", "domain": "d.com"},
        {"position": 6, "url": "https://s.kr/bags", "title": "우리", "domain": "s.kr", "is_own": 1},
    ], checked_at=ts)
    conn.commit()

    fetched: list[str] = []

    def audit_row(title, h2s, url=""):
        return {"url": url, "status": 200, "error": None, "title": title,
                "meta_description": f"{title} 설명", "h1_json": json.dumps([title]),
                "h2_json": json.dumps(h2s, ensure_ascii=False), "words": 800}

    pages = {
        "https://s.kr/bags": audit_row("여성 가방", ["신상 가방"]),
        "https://s.kr/wallet": audit_row("지갑", []),
        "https://s.kr/belt": audit_row("벨트", []),
        "https://a.com/bags": audit_row("A", ["가방 가격 정리", "소재별 차이", "관리 방법"]),
        "https://c.com/bags": audit_row("C", ["가방 가격", "관리 방법", "브랜드 역사"]),
    }

    def fake_fetch(url):
        fetched.append(url)
        if url == "https://b.com/bags":             # 봇 차단 → DataForSEO 로 읽는다
            return {"url": url, "status": 403, "error": "HTTP 403 · text/html"}
        return pages.get(url) or {"url": url, "status": 500, "error": "HTTP 500"}

    posts: list = []

    def fake_post(path, body):
        posts.append((path, body))
        assert path == ON_PAGE_PARSE, path
        return ([{"items": [{"page_content": {"main_topic": [
            {"h_title": "B 가방", "level": 1, "main_title": "B 가방 모음",
             "primary_content": [{"text": "가방 이야기 하나 둘 셋"}]},
            {"h_title": "소재별 차이점", "level": 2, "primary_content": [{"text": "가죽 캔버스"}]},
        ]}}]}], 0.00125)

    asks: list[str] = []
    fail_on: set[str] = set()

    def fake_ask(text):
        asks.append(text)
        if any(f in text for f in fail_on):
            raise RuntimeError("AI 답이 길이 상한에서 잘렸습니다")
        return ({"summary": "가방 페이지 하나로 세 검색어를 민다.", "why": ["상위 셋이 가격을 다룬다"],
                 "title": {"now": "여성 가방", "new": "여성 가방 — 가격·소재·관리"},
                 "h1": {"now": "여성 가방", "new": "여성 가방 고르는 법"},
                 "sections": [{"h2": "가방 가격", "draft": "가격대는 [확인 필요].", "for": ["가방"]}],
                 "links": [{"from": "https://s.kr/", "anchor": "여성 가방"}],
                 "trust": ["수정일"], "expected": "추정: 1페이지 상단", "junk": 1}, 0.02)

    def run(**kw):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            r = collect("pl", conn=conn, fetch=fake_fetch, post=fake_post, ask=fake_ask, **kw)
        return r, buf.getvalue()

    # ── 1. 묶기: 페이지 단위 · 심사 대기 포함 · 페이지 없는 것·다른 종류는 빠짐
    d = dashboard.gather(conn, db.get_project(conn, "pl"), gated=False)
    gs = candidates(d, limit=4)
    by_page = {g["page"]: g for g in gs}
    assert list(by_page) == ["https://s.kr/bags", "https://s.kr/wallet", "https://s.kr/belt"], \
        f"묶음·순서가 틀렸다: {list(by_page)}"
    bags = by_page["https://s.kr/bags"]
    assert {o["target"] for o in bags["opps"]} == {"가방", "토트백", "구찌가방"}, \
        f"한 페이지의 기회가 한 묶음이 아니다(심사 대기 '구찌가방' 포함): {[o['target'] for o in bags['opps']]}"
    assert bags["score"] == 173.4 and bags["keywords"][0] == "가방", bags
    assert not any(o["target"] in ("모자", "가방 추천") for g in gs for o in g["opps"]), \
        "페이지 없는 기회나 고치기 종류가 아닌 기회가 묶였다"
    # 화면 쪽(gated)은 여전히 심사 대기를 안 싣는다 — 이 흐름만 문을 건너뛴다
    shown = {o["target"] for o in dashboard.gather(conn, db.get_project(conn, "pl"))["opps"]}
    assert "구찌가방" not in shown, "화면 페이로드에 심사 대기 기회가 섞였다"
    assert len(candidates(d, limit=1)) == 1

    # ── 2. 돌린다: 차단 → DataForSEO · gaps · 결과 정리 · 비용
    r, out = run(max=2)
    assert r.ok and not r.failed and r.rows == 2, (r, out)
    rows = db.list_plays(conn, pid)
    assert [p_["page"] for p_ in rows] == ["https://s.kr/bags", "https://s.kr/wallet"], rows
    p0 = rows[0]
    ev = p0["evidence"]
    assert ev["our"]["fetched_via"] == "direct" and ev["our"]["title"] == "여성 가방", ev["our"]
    via = {x["url"]: x.get("fetched_via") for x in ev["serp"] if not x["is_own"]}
    assert via.get("https://b.com/bags") == "dataforseo", f"막힌 글을 DataForSEO 로 안 읽었다: {via}"
    assert posts and posts[0][1] == [{"url": "https://b.com/bags"}], posts
    assert "https://d.com/bags" not in fetched, "상위 글을 PLAY_SERP_N 장보다 많이 열었다"
    b_row = next(x for x in ev["serp"] if x["url"] == "https://b.com/bags")
    assert b_row["headings"] == ["소재별 차이점"] and b_row["words"] == 7, b_row
    assert ev["gaps"] == ["가방 가격 정리", "소재별 차이", "관리 방법"], f"gaps: {ev['gaps']}"
    assert ev["aio"] == [{"keyword": "구찌가방", "present": 1, "cited": 0,
                          "domains": ["a.com", "b.com"]}], ev["aio"]
    assert "junk" not in p0["result"] and p0["result"]["sections"][0]["h2"] == "가방 가격", p0["result"]
    assert p0["title"].startswith("여성 가방 — ") and "검색어 3개" in p0["title"], p0["title"]
    assert "## 지금 → 고친 값" in p0["markdown"] and "## 증거 요약" in p0["markdown"], p0["markdown"]
    assert abs(r.cost - (0.00125 + 0.02 + 0.02)) < 1e-4, r.cost
    assert sorted(p0["opp_ids"]) == sorted(o["id"] for o in bags["opps"]), p0["opp_ids"]
    # 프롬프트는 요청문 본문을 그대로 싣는다(두 벌이 아니다)
    assert bags["opps"][0]["brief"]["body"][:200] in asks[0], "프롬프트가 요청문 본문을 안 싣는다"

    # ── 3. 신선하면 건너뛴다 / force 는 다시 만든다 / 새 것은 갈아 끼우고 applied 는 둔다
    n_ask = len(asks)
    r, out = run(max=2)
    assert r.skipped and len(asks) == n_ask, f"신선한데 다시 만들었다: {r} {out}"
    db.set_play_status(conn, pid, rows[1]["id"], "applied")
    r, out = run(max=2, force=True)
    assert r.ok and len(asks) > n_ask, (r, out)
    after = db.list_plays(conn, pid)
    # applied 를 먼저 본다 — 전부 지우면 SQLite 가 id 를 다시 1부터 매겨 아래 대조가 헛짚는다
    assert rows[1]["id"] in {x["id"] for x in after if x["status"] == "applied"}, \
        "PR 을 만든(applied) 할 일을 지웠다"
    assert rows[0]["id"] not in {x["id"] for x in after}, "옛 'new' 할 일이 안 갈렸다"
    # applied 페이지(wallet)는 다시 안 낸다 — 그 자리는 다음 묶음(belt)이 채운다
    assert [x["page"] for x in after if x["status"] == "new"] == \
        ["https://s.kr/bags", "https://s.kr/belt"], after
    # 오래되면 다시 만든다
    conn.execute("UPDATE plays SET created_at=datetime('now','-8 days') WHERE status='new'")
    conn.commit()
    n_ask = len(asks)
    r, _ = run(max=2)
    assert r.ok and not r.skipped and len(asks) > n_ask, "7일 지난 할 일을 다시 안 만들었다"

    # ── 4. AI 실패 → 결과 자리에 이유, 증거는 남는다, 다음 묶음은 계속 · 다음 런은 신선 판정 안 함
    fail_on.add("https://s.kr/bags")
    r, out = run(max=2, force=True)
    got = {x["page"]: x for x in db.list_plays(conn, pid, statuses=("new",))}
    bad = got["https://s.kr/bags"]
    assert "잘렸습니다" in bad["result"].get("error", ""), bad["result"]
    assert bad["evidence"]["our"]["title"] == "여성 가방", "AI 가 실패하자 증거도 버렸다"
    assert "수정안을 못 만들었습니다" in bad["markdown"], bad["markdown"]
    assert not got["https://s.kr/belt"]["result"].get("error"), "한 묶음 실패가 다음 묶음을 멈췄다"
    assert r.partial and not r.failed, r
    fail_on.clear()
    n_ask = len(asks)
    r, _ = run(max=2)
    assert r.ok and not r.skipped and len(asks) > n_ask, "실패한 줄이 있는데 신선하다고 건너뛰었다"

    # ── 4b. 치명 오류(OpenRouter 잔액 없음 402) — 증거는 저장하고, 단계는 그 이유로 실패
    real_ask = fake_ask

    def broke_ask(text):
        raise collector.Fatal("OpenRouter 잔액 없음(402)")
    fake_ask = broke_ask
    try:
        run(max=2, force=True)
        raised = None
    except collector.Fatal as e:
        raised = str(e)
    finally:
        fake_ask = real_ask
    assert raised and "402" in raised, "잔액 없음이 단계 실패로 안 올라갔다 — 알림·출시 문이 못 본다"
    got = {x["page"]: x for x in db.list_plays(conn, pid, statuses=("new",))}
    assert got and all("402" in (x["result"] or {}).get("error", "") for x in got.values()), got
    assert got["https://s.kr/bags"]["evidence"]["our"]["title"] == "여성 가방", \
        "치명 오류에 읽어 온 증거까지 버렸다"
    r, _ = run(max=2, force=True)
    assert r.ok, "치명 오류 뒤 정상 런이 안 섰다"

    # ── 5. 키 없음·dry-run·DataForSEO 응답 꼴
    with contextlib.redirect_stdout(io.StringIO()):
        r = collect("pl", conn=conn, fetch=fake_fetch, post=fake_post)
    assert r.skipped and "OPENROUTER_API_KEY" in r.reason, r
    n_ask = len(asks)
    r, _ = run(dry_run=True, force=True)
    assert r.skipped and len(asks) == n_ask, "dry-run 인데 AI 를 불렀다"
    assert parse_content([]) is None and parse_content([{"items": []}]) is None
    assert gaps(["관리 방법"], [["관리 방법 안내", "가격"], ["관리방법", "가격"]]) == ["가격"]

    # ── 6. 페이로드 — d.plays 꼴
    d = dashboard.gather(conn, db.get_project(conn, "pl"))
    assert d["plays"] and d["plays_at"], "페이로드에 plays 가 없다"
    one = d["plays"][0]
    for k in ("id", "page", "title", "keywords", "opp_ids", "score", "created_at", "status",
              "model", "cost", "evidence", "result", "markdown"):
        assert k in one, f"페이로드 plays 줄에 {k} 가 없다"
    assert isinstance(one["evidence"], dict) and isinstance(one["keywords"], list), one
    conn.close()
    print("plays self-check ok")


if __name__ == "__main__":
    main()
