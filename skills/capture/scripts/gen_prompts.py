#!/usr/bin/env python3
"""AI에 물어볼 질문 만들기 — [AI 인용] 화면이 비어 있던 진짜 이유.

`ai` 단계는 ai_prompts 를 재료로 돈다. 그런데 그 표를 채우는 코드가 이 리포에
없었다 — 질문은 채팅(`/capture add`)에서 Claude 가 직접 INSERT 해 왔고, 대시보드
폼으로 만든 사이트는 그 표가 영원히 비어 있었다. 그래서 웹 사용자는 [AI 인용
다시 확인]을 눌러도 "질문이 아직 없습니다"로 즉시 실패했고, 화면에는 아무 변화가
없어 버튼이 죽은 것처럼 보였다.

여기서는 사이트가 이미 가진 사실(이름·도메인·업종·로케일·GSC 상위 검색어)로
**사람이 실제로 AI에 물어볼 법한 질문**을 만든다. 키워드가 아니라 질문이다 —
"ai 티어리스트"가 아니라 "ai 티어리스트 만들 수 있는 사이트 있어?" 쪽이다.

비용: 생성 1회당 OpenRouter 호출 한 번(수백 토큰). 만들기만 하고 인용 확인은
돌리지 않는다 — 그쪽이 진짜 돈이 나가는 단계라 사용자가 눌러서 시작해야 한다.

Usage:
  python gen_prompts.py --project NAME [--limit 20] [--dry-run]
  python gen_prompts.py                                  # self-check
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import db  # noqa: E402
import serp_adapter  # noqa: E402  (로케일 → 영어 언어·나라 이름)

# 검색이 필요 없는 작업이다(:online 을 안 붙인다) — 질문을 짓는 것뿐이라 싸고 빠른
# 모델로 충분하다. 인용 확인에 쓰는 엔진 표(collect_ai.DEFAULT_ENGINES)와는 별개다.
MODEL = "openai/gpt-4o-mini"
# 질문 갈래는 여기 한 벌이고, 만드는 쪽(이 파일)·고르는 쪽(화면의 <select>)·받는
# 쪽(server/app.py)이 전부 이걸 가리킨다. 셋으로 갈라져 있던 시절 실제로 어긋났다:
# 화면만 "general" 을 선택지에 넣고 있었고 이 파일은 그 값을 몰랐다.
#   · CATEGORIES        — 모델이 짓는 넷. 프롬프트(SYSTEM)가 요구하는 것과 같다.
#   · DEFAULT_CATEGORY  — 갈래를 못 정했을 때 떨어지는 자리(db 의 기본값과 같다).
#     모델이 지어낸 이름은 여기로 접는다.
#   · CATEGORY_CHOICES  — 사람이 화면에서 고를 수 있는 것. 위 둘을 합친 것이다.
CATEGORIES = ("추천", "비교", "문제해결", "브랜드")
DEFAULT_CATEGORY = "general"
CATEGORY_CHOICES = CATEGORIES + (DEFAULT_CATEGORY,)
#   · INTENT_LABELS     — 화면에 쓸 이름. 기본값 id("general")가 영문 그대로 목록에 떴다.
#     호스팅 질문 목록 API(intent_labels)·대시보드 페이로드(ai_intent_labels)가 이걸 싣는다.
INTENT_LABELS = {**{c: c for c in CATEGORIES}, DEFAULT_CATEGORY: "일반"}
MIN_LEN, MAX_LEN = 6, 120

# 생성기 판 — 새 질문마다 ai_prompts.gen_version 으로 찍힌다(save). 판이 없던 동안,
# 사이트를 모르고 지은 옛 질문 1~20번이 생성기를 고친 뒤에도 똑같이 활성으로 남아
# 그 질문들의 "인용 0"이 실제 격차처럼 기회 목록에 올랐고, 아무도 그걸 가를 수 없었다.
#   NULL — 판을 찍기 전에 들어온 질문. 1판이 섞여 있어 구버전으로 센다(outdated)
#   0    — 생성기가 아니라 사람이 직접 적은 질문(db.add_ai_prompts 의 기본값). 구버전 아님
#   1    — (기록 없음) 업종·GSC 검색어만 보고 짓던 판
#   2    — 사이트 페이지 목록(offers)을 재료로 받고, 질문마다 겨냥(aim)을 적는 판
#   3    — 재료를 **수요 순으로 고르게** 싣는 판. 2판은 페이지 목록을 주소 가나다순으로
#          앞 24개만 잘라 실었고(latest_page_audits 가 ORDER BY url 이다) 주제도 이름순
#          15개였다 — gucci 는 /beauty… 가 /handbags·/women 보다 앞이라 질문 7개 중 5개가
#          뷰티였다. 이제 페이지는 노출·추정 검색량 순으로 섹션마다 돌아가며, 주제는
#          추적 검색량 순으로 싣고, 서치콘솔이 없으면 추정 순위 검색어를 대신 싣는다.
# 짓는 방식을 바꾸면 이 수를 올린다 — 그 전 판 질문이 전부 "다시 만들기 권함"으로 뜬다.
GEN_VERSION = 3
# 겨냥(ai_prompts.aim)의 꼴. 모델이 준 겨냥은 재료 목록에 **글자 그대로** 있어야만
# 받는다(parse) — 없는 페이지를 겨냥했다고 적지 않는다.
AIM_KINDS = ("page", "keyword", "cluster", "brand")


def outdated(gen_version) -> bool:
    """이 판의 질문을 다시 만들기를 권하나 — 판 표시 전(NULL)이거나 지금보다 옛 판.

    사람이 적은 질문(0)은 아니다: 생성기가 좋아져도 사람이 고른 질문은 낡지 않는다.
    끄거나 지우는 판정이 아니다 — 인용 이력이 붙어 있으므로 권하기만 한다.
    """
    return gen_version is None or 0 < int(gen_version) < GEN_VERSION


SYSTEM = (
    "You design the question set used to measure whether a site gets cited by AI "
    "assistants. Return ONLY a JSON array, no prose, no code fence. Each item: "
    '{"prompt": "...", "category": "' + "|".join(CATEGORIES) + '", "aim": "..."}. '
    'For "aim", copy character for character the ONE page path, keyword or topic from the '
    "lists in the message that the prompt is about; use \"brand\" for a prompt about the "
    'brand itself, and "" if nothing in the lists fits. Never make up an aim. '
    "Write every prompt in the audience language named in the message (never in another "
    "language), phrased the way a real person there types into ChatGPT — full questions, "
    "not keywords, and never mention that this is a test. Keep the category values exactly "
    "as given even though they are Korean labels. "
    f"Cover all {len(CATEGORIES)} categories: 추천 (asking for recommendations in this field), "
    "비교 (comparing options), 문제해결 (solving the problem the site addresses), "
    "브랜드 (asking about this brand by name). Most prompts must NOT name the brand — "
    "the point is to find out who gets cited when the user does not already know us. "
    "Ground every prompt in the page list you are given — that is what this site actually "
    "offers. Prefer what is specific to this site (its own named offerings, the specific "
    "problems it solves, the people behind it) over generic industry terms that every "
    "competitor also targets. Never invent a service the page list does not show. "
    "For 브랜드, ask about the site's own named services and people too, not just its name. "
    "Every list is ordered by search demand, most first. Spread the prompts across the "
    "different topics and page sections roughly in proportion to that demand — no single "
    "topic or section may take more than a third of the prompts."
)


# 다국어 사본은 같은 것을 두세 번 말한다 — 경로 앞의 로케일 조각을 접는다.
# 나라·언어 두 칸(gucci 의 /kr/ko/…)까지 접는다 — 한 칸만 접던 때는 /ko/… 가 남았다.
_LOCALE_SEG = re.compile(r"^(?:/(?:[a-z]{2}(?:-[a-z]{2,4})?)(?=/)){1,2}", re.I)
# 목록·공지처럼 무엇을 파는지 안 말하는 자리는 재료가 못 된다.
_NOISE = re.compile(r"^/(?:category|notice|page|tag|author|search|wp-)", re.I)


def _path_of(url) -> str:
    """주소 → 재료로 쓸 경로(호스트·쿼리·로케일 조각을 뗀다). 루트는 '/'."""
    path = urllib.parse.unquote(re.sub(r"^https?://[^/]+", "", str(url or "")))
    return _LOCALE_SEG.sub("", path.split("?")[0].split("#")[0]).rstrip("/") or "/"


def _section(path: str) -> str:
    """경로의 섹션 — 고르게 나눌 단위. 두 글자 이하 칸(gucci 의 /ca/·/pr/ 같은 꼴 표지)은
    건너뛰고 첫 이름 칸을 쓴다: /ca/women/handbags → women, /acne-scar/papular → acne-scar."""
    segs = [s for s in path.strip("/").split("/") if s]
    return next((s.lower() for s in segs if len(s) > 2), segs[0].lower() if segs else "")


def _page_demand(conn, project_id: int) -> dict[str, float]:
    """경로 → 수요(최신 서치콘솔 노출 + 최신 추정 순위의 월 검색량). 둘 다 "이 페이지로
    사람이 오는 정도"다 — 서치콘솔이 없는 사이트(gucci)는 추정 순위만 있다."""
    import scoring
    out: dict[str, float] = {}
    cur, _, period, _ = scoring.snapshot_pair(conn, project_id)
    if cur:
        for r in conn.execute(
                """SELECT page, SUM(impressions) imp FROM gsc_snapshots
                    WHERE project_id=? AND snapshot_date=? AND period_days=? AND page IS NOT NULL
                    GROUP BY page""", (project_id, cur, period)):
            p = _path_of(r["page"])
            out[p] = out.get(p, 0) + float(r["imp"] or 0)
    try:
        for r in conn.execute(
                """SELECT url, SUM(COALESCE(volume,0)) v FROM labs_ranked
                    WHERE project_id=? AND url IS NOT NULL AND checked_date=(
                      SELECT MAX(checked_date) FROM labs_ranked WHERE project_id=?)
                    GROUP BY url""", (project_id, project_id)):
            p = _path_of(r["url"])
            out[p] = out.get(p, 0) + float(r["v"] or 0)
    except sqlite3.OperationalError:
        pass
    return out


def _spread(items: list, key, weight) -> list:
    """섹션마다 돌아가며 뽑는다 — 섹션은 수요 합이 큰 순, 섹션 안은 수요 큰 순.

    한 섹션에 페이지가 몰린 사이트(gucci 의 뷰티)에서 앞에서부터 자르면 그 섹션이
    재료를 다 먹는다. 돌아가며 뽑으면 가방·의류가 같은 무게로 한 자리씩 들어온다.
    """
    groups: dict[str, list] = {}
    for it in items:
        groups.setdefault(key(it), []).append(it)
    order = sorted(groups.values(),
                   key=lambda g: (-sum(weight(x) for x in g), -len(g)))
    for g in order:
        g.sort(key=lambda x: -weight(x))        # 안정 정렬 — 같은 수요는 들어온 순
    out = []
    while any(order):
        for g in order:
            if g:
                out.append(g.pop(0))
    return out


def offers(conn, project_id: int, *, limit: int = 24) -> list[str]:
    """사이트가 **무엇을 하는 곳인지** — 질문을 지을 때 이게 없으면 업종 일반형만 나온다.

    이 함수가 없던 시절의 결과가 그 증거다: 어느 사이트에 물어도 "무료 영상 편집
    사이트 추천해줘"가 나왔다. 사이트가 자기 이름을 붙여 파는 기능(ecrett 의 장면별
    BGM 생성)도, 좁은 쓰임(paperpal 의 논문 교정)도 재료에 없었으니 모델이 알 길이
    없었다.

    제목이 있으면 제목을 쓴다(사람 말이다). 없으면 URL 경로가 대신 말해 준다 —
    /signature/scene-bgm/ 은 경로 자체가 그 사이트만의 기능 이름을 담는다.
    셋 다 없으면 빈 목록이다: 없는 것을 지어내지 않는다.

    **무엇을 먼저 싣나**가 질문의 쏠림을 정한다. 예전엔 페이지 감사를 주소 가나다순으로
    받아 앞 24개를 잘랐다 — gucci 는 /beauty 가 /handbags·/women 보다 앞이라 질문 7개 중
    5개가 뷰티였다. 이제 세 출처(감사·크롤·서치콘솔)를 합쳐 수요(_page_demand) 순으로,
    섹션마다 돌아가며(_spread) 싣는다. 못 가져온 페이지(4xx·오류 — 봇 차단의
    "Access Denied" 제목)는 무엇을 파는지 말하지 않으므로 뺀다.
    """
    rows: list[tuple[str, str | None]] = []
    # 1) 페이지 감사 — 제목·H1 까지 있는 가장 좋은 재료
    try:
        rows += [(r["url"], r["title"]) for r in db.latest_page_audits(conn, project_id)
                 if not r["error"] and not (r["status"] and int(r["status"]) >= 400)]
    except sqlite3.Error:
        pass
    # 2) 크롤 회차의 제목
    rows += [(r["url"], r["title"]) for r in conn.execute(
        """SELECT url, title FROM crawl_pages WHERE run_id=(
             SELECT MAX(id) FROM crawl_runs WHERE project_id=?)
             AND (status IS NULL OR status < 400)""", (project_id,))]
    # 3) GSC 가 본 페이지 — 제목은 없지만 경로가 남는다.
    rows += [(r["page"], None) for r in conn.execute(
        """SELECT page, SUM(impressions) imp FROM gsc_snapshots
            WHERE project_id=? AND page IS NOT NULL
         GROUP BY page ORDER BY imp DESC""", (project_id,))]

    pages: dict[str, str] = {}
    for url, title in rows:
        path = _path_of(url)
        if path == "/" or _NOISE.match(path):
            continue
        t = " ".join(str(title or "").split())
        if path not in pages or (t and not pages[path]):
            pages[path] = t
    demand = _page_demand(conn, project_id)
    picked = _spread(list(pages), _section, lambda p: demand.get(p, 0))[:limit]
    return [f"{p} — {pages[p]}" if pages[p] else p for p in picked]


def _ranked(conn, project_id: int, n: int) -> list[str]:
    """추정 순위(labs_ranked)에서 우리가 걸린 검색어 — 월 검색량 큰 순. 서치콘솔이 없는
    사이트(gucci)에서 "사람들이 우리로 무엇을 찾나"를 말하는 유일한 재료다."""
    try:
        return [r[0] for r in conn.execute(
            """SELECT keyword FROM labs_ranked WHERE project_id=? AND checked_date=(
                 SELECT MAX(checked_date) FROM labs_ranked WHERE project_id=?)
               ORDER BY COALESCE(volume,0) DESC, keyword LIMIT ?""",
            (project_id, project_id, n))]
    except sqlite3.OperationalError:
        return []


def _tracked(conn, project_id: int, n: int) -> list[str]:
    """추적 중인 검색어 — 주제(cluster)마다 돌아가며, 주제 안은 검색량 큰 순."""
    rows = [dict(r) for r in conn.execute(
        """SELECT keyword, COALESCE(cluster,'') cluster, COALESCE(volume,0) volume
             FROM keywords WHERE project_id=? AND is_active=1""", (project_id,))]
    return [r["keyword"] for r in _spread(rows, lambda r: r["cluster"],
                                          lambda r: r["volume"])[:n]]


def brief(conn, project: str, top_n: int = 15) -> dict:
    """질문을 지을 재료 — 사이트가 이미 가진 사실만. 없으면 없는 대로 짓는다."""
    p = db.get_project(conn, project)
    cfg = db.project_cfg(conn, p)
    import scoring
    cur, _, period, _ = scoring.snapshot_pair(conn, p["id"])
    queries = []
    if cur:
        queries = [r["query"] for r in conn.execute(
            """SELECT query, SUM(impressions) imp FROM gsc_snapshots
                WHERE project_id=? AND snapshot_date=? AND period_days=?
                GROUP BY query ORDER BY imp DESC LIMIT ?""",
            (p["id"], cur, period, top_n))]
    return {"name": p["name"], "domain": p["domain"], "type": p["type"],
            "locale": db.project_locale(p),
            "aliases": cfg.get("brand_aliases") or [],
            "queries": queries,
            # 서치콘솔이 없을 때만 — 있으면 실측이 이긴다(추정을 같이 실으면 같은 수요를
            # 두 번 센다).
            "ranked": [] if queries else _ranked(conn, p["id"], top_n),
            # 이 둘이 "이 사이트가 무엇을 하는 곳인가"를 말한다. 없으면 모델은 업종만
            # 알고 짓게 되고, 그러면 어느 사이트에 물어도 같은 질문이 나온다.
            "offers": offers(conn, p["id"]),
            "seeds": (cfg.get("seed_keywords") or [])[:20],
            "tracked": _tracked(conn, p["id"], top_n),
            # 추적 중인 주제 묶음 — 질문이 무엇을 겨냥했는지(aim) 적을 세 번째 재료다.
            # 이름순이 아니라 추적 검색량 순이다(이름순 15개는 가나다 앞 주제만 실었다).
            "clusters": [r[0] for r in conn.execute(
                """SELECT cluster FROM keywords
                    WHERE project_id=? AND is_active=1 AND cluster IS NOT NULL
                      AND TRIM(cluster)<>''
                    GROUP BY cluster
                    ORDER BY SUM(COALESCE(volume,0)) DESC, COUNT(*) DESC, cluster
                    LIMIT 15""", (p["id"],))]}


def targets(b: dict) -> dict[str, str]:
    """재료 목록 → 겨냥 이름. {모델이 베껴 올 글자: 'page:/경로' 같은 저장 꼴}.

    모델에게 준 목록이 곧 받을 수 있는 겨냥의 전부다 — 여기 없는 글자는 버린다.
    페이지는 offers() 가 준 "경로 — 제목" 에서 경로만 쓴다(모델은 경로를 베낀다).
    """
    out: dict[str, str] = {}
    for c in b.get("clusters") or []:
        out[str(c).strip()] = f"cluster:{str(c).strip()}"
    for k in [*(b.get("seeds") or []), *(b.get("tracked") or [])]:
        out[str(k).strip()] = f"keyword:{str(k).strip()}"
    for o in b.get("offers") or []:
        path = str(o).split(" — ", 1)[0].strip()
        if path.startswith("/"):
            out[path] = f"page:{path}"
    out.pop("", None)
    return out


def user_msg(b: dict, n: int) -> str:
    q = ", ".join(b["queries"][:15]) or "(none yet)"
    alias = ", ".join(x for x in [b["name"], *b["aliases"]] if x)
    seeds = ", ".join(b.get("seeds") or []) or "(none)"
    tracked = ", ".join(b.get("tracked") or []) or "(none)"
    topics = ", ".join(b.get("clusters") or []) or "(none)"
    # 페이지 목록은 줄바꿈으로 준다 — 쉼표로 이으면 경로끼리 붙어 한 줄로 읽힌다.
    pages = "\n".join(f"  {x}" for x in (b.get("offers") or [])) or "  (none yet)"
    lang, country = serp_adapter.describe(b["locale"])
    ranked = (f"Search queries this site ranks for (estimated, by monthly volume): "
              f"{', '.join(b['ranked'])}\n") if b.get("ranked") else ""
    return (f"Site: {b['name']} ({b['domain']})\n"
            f"Kind: {b['type']}\n"
            f"Audience: {lang} speakers in {country} ({b['locale']}) — write in {lang}.\n"
            f"Brand names: {alias}\n"
            f"Search queries this site already gets impressions for: {q}\n"
            f"{ranked}"
            f"Keywords this site targets: {seeds}\n"
            f"Keywords this site tracks (spread across its topics): {tracked}\n"
            f"Topics this site tracks: {topics}\n"
            f"Pages this site actually has (its services, in its own words):\n{pages}\n\n"
            f"Write exactly {n} prompts.")


def _aim_of(raw, allowed: dict[str, str]) -> str | None:
    """모델이 적은 겨냥 → 저장 꼴. 재료 목록에 글자 그대로 있을 때만 받는다.

    'page:/x' 처럼 머리말을 붙여 오거나 경로 끝 / 가 달라도 같은 것으로 본다.
    못 알아보면 None — 없는 페이지를 겨냥했다고 적는 것보다 모른다가 낫다.
    """
    s = " ".join(str(raw or "").split())
    if not s:
        return None
    if s.lower() == "brand":
        return "brand"
    head, _, rest = s.partition(":")
    if head in AIM_KINDS and rest:
        s = rest.strip()
    for cand in (s, s.rstrip("/"), s.rstrip("/") + "/"):
        if cand in allowed:
            return allowed[cand]
    return None


def parse(text: str, allowed: dict[str, str] | None = None) -> list[dict]:
    """모델 응답 → [{prompt, category, aim}]. 코드펜스·설명이 붙어 와도 배열만 건진다.

    형식이 틀렸다고 통째로 버리지 않는다 — 배열 하나만 건지면 나머지 잡소리는
    무해하다. 다만 항목 단위로는 엄격하다: 질문이 아니면 안 쓴다.
    allowed 는 targets() — 모델에게 준 재료 목록이다. 거기 없는 겨냥은 None.
    """
    allowed = allowed or {}
    m = re.search(r"\[.*\]", text or "", re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return []
    out, seen = [], set()
    for item in data if isinstance(data, list) else []:
        if isinstance(item, str):
            item = {"prompt": item}
        if not isinstance(item, dict):
            continue
        prompt = " ".join(str(item.get("prompt") or "").split())
        if not (MIN_LEN <= len(prompt) <= MAX_LEN) or prompt.lower() in seen:
            continue
        seen.add(prompt.lower())
        cat = str(item.get("category") or "").strip()
        out.append({"prompt": prompt,
                    "category": cat if cat in CATEGORIES else DEFAULT_CATEGORY,
                    "aim": _aim_of(item.get("aim"), allowed)})
    return out


def suggest(project: str, *, n: int = 20, conn=None, model: str = MODEL,
            ask=None) -> list[dict]:
    """질문 n개를 지어 돌려준다. 저장은 하지 않는다(save 가 한다).

    Args:
        project: 사이트 이름
        n: 만들 질문 수 (1~40)
        conn: 이미 열린 Brain 연결 — 주면 그것을 쓰고 닫지 않는다
        model: OpenRouter 모델 슬러그
        ask: fn(model, prompt, api_key, locale) -> {"content": ...}
             기본값은 collect_ai.ask — 자체점검이 여기를 갈아끼운다

    Raises:
        RuntimeError: OPENROUTER_API_KEY 가 없을 때
    """
    n = max(1, min(40, int(n)))
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise RuntimeError(
            "OPENROUTER_API_KEY 가 없어 질문을 만들 수 없습니다 — "
            "발급: https://openrouter.ai/keys")
    if ask is None:
        import collect_ai
        ask = collect_ai.ask
    own = conn is None
    conn = conn or db.connect()
    try:
        b = brief(conn, project)
    finally:
        if own:
            conn.close()
    # SYSTEM 을 프롬프트 앞에 붙여 보낸다 — collect_ai.ask 의 system 자리는 "실제
    # 사용자처럼 답하라"라서 그대로 쓰면 질문이 아니라 답이 온다.
    res = ask(model, SYSTEM + "\n\n" + user_msg(b, n), key, b["locale"])
    return parse(res.get("content", ""), targets(b))[:n]


def save(conn, project: str, rows: list[dict]) -> int:
    """만든 질문을 ai_prompts 에 넣는다. 이미 있는 질문은 건드리지 않는다.

    판(GEN_VERSION)을 여기서 찍는다 — 로컬(main)·웹(server/app.py) 둘 다 이 문으로
    들어온다. 이미 있던 질문에는 새 판을 덮어 찍지 않는다: 그 질문은 옛 판이 지었다.
    """
    pid = db.get_project(conn, project)["id"]
    return db.add_ai_prompts(conn, pid, [
        {**r, "gen_version": GEN_VERSION, "aim": r.get("aim")} for r in rows])


def retire_outdated(conn, project: str) -> int:
    """옛 판 질문(outdated)을 **끈다** — 지우지 않는다. 반환: 끈 개수.

    [질문 다시 만들기]가 새 질문을 넣기만 하던 동안, 옛 판 37개(theotherskin)는 그대로
    켜진 채 남아 새 질문과 함께 상한을 나눠 먹었고, 그 인용 0 이 기회 목록에 계속 섰다.
    화면은 "직접 끄세요"라고만 했다 — 다시 만들기는 있는데 바꾸기는 없었다.
    끄기만 하므로 지금까지의 인용 이력(ai_checks)은 그대로고(db.set_ai_prompts_active),
    사람이 적은 질문(판 0)은 outdated 가 아니라 안 건드린다. 판정은 outdated 한 벌이다.
    부르는 쪽은 새 질문이 실제로 들어갔을 때만 부른다 — 다 실패했는데 옛 것까지 끄면
    물어볼 질문이 0 이 된다.
    """
    pid = db.get_project(conn, project)["id"]
    ids = [r["id"] for r in conn.execute(
        "SELECT id, gen_version FROM ai_prompts WHERE project_id=? AND is_active=1", (pid,))
        if outdated(r["gen_version"])]
    return db.set_ai_prompts_active(conn, pid, ids, False)


def main() -> None:
    if len(sys.argv) == 1:
        _selfcheck()
        return
    ap = argparse.ArgumentParser(description="AI에 물어볼 질문 만들기")
    ap.add_argument("--project", required=True)
    ap.add_argument("--limit", type=int, default=20, help="만들 질문 수 (기본 20)")
    ap.add_argument("--dry-run", action="store_true", help="저장하지 않고 보여만 준다")
    ap.add_argument("--replace", action="store_true",
                    help="새 질문이 들어가면 옛 판 질문을 끈다(지우지 않는다 — 인용 이력은 남는다)")
    a = ap.parse_args()
    rows = suggest(a.project, n=a.limit)
    if not rows:
        sys.exit("질문을 만들지 못했습니다 — 모델 응답이 비었거나 형식이 달랐습니다.")
    for r in rows:
        print(f"  [{r['category']}] {r['prompt']}"
              + (f"   ← {r['aim']}" if r.get("aim") else ""))
    if a.dry_run:
        print(f"\n{len(rows)}개 (dry-run — 저장하지 않았습니다)")
        return
    conn = db.connect()
    try:
        added = save(conn, a.project, rows)
        retired = retire_outdated(conn, a.project) if a.replace and added else 0
    finally:
        conn.close()
    if retired:
        print(f"옛 판 질문 {retired}개를 껐습니다 (지우지 않았습니다 — 인용 이력은 그대로).")
    print(f"\n질문 {added}개 저장 (이미 있던 것은 그대로). "
          f"이제 `python collect_ai.py --project {a.project}` 로 인용을 확인합니다.")


def _selfcheck() -> None:
    # 갈래 한 벌 — 기본값은 선택지 안에 있고, 만드는 넷은 선택지의 진부분집합이다.
    # (화면이 고를 수 있는데 여기가 모르는 값이 있으면 안 된다.)
    assert DEFAULT_CATEGORY in CATEGORY_CHOICES and DEFAULT_CATEGORY not in CATEGORIES
    assert set(CATEGORIES) < set(CATEGORY_CHOICES), (CATEGORIES, CATEGORY_CHOICES)
    # 이름표는 고를 수 있는 갈래 전부에 있고, 영문 id 를 그대로 이름으로 쓰지 않는다
    assert tuple(INTENT_LABELS) == CATEGORY_CHOICES, INTENT_LABELS
    assert all(not v.isascii() for v in INTENT_LABELS.values()), INTENT_LABELS
    assert "|".join(CATEGORIES) in SYSTEM, "프롬프트가 갈래 사본을 들고 있다"

    # 파싱: 코드펜스·설명·중복·길이 밖·잘못된 카테고리를 전부 지나간다
    raw = ('설명 한 줄\n```json\n['
           '{"prompt":"브이로그 배경음악 어디서 받아?","category":"추천"},'
           '{"prompt":"브이로그 배경음악 어디서 받아?","category":"추천"},'
           '{"prompt":"짧음","category":"추천"},'
           '{"prompt":"무료 bgm 이랑 유료 bgm 뭐가 달라?","category":"엉뚱"},'
           '"문자열로 온 질문도 받는다 bgm"'
           ']\n```\n뒷말')
    got = parse(raw)
    assert [g["prompt"] for g in got] == [
        "브이로그 배경음악 어디서 받아?", "무료 bgm 이랑 유료 bgm 뭐가 달라?",
        "문자열로 온 질문도 받는다 bgm"], got
    assert got[0]["category"] == "추천" and got[1]["category"] == DEFAULT_CATEGORY, got
    assert parse("배열이 없다") == [] and parse("[깨진 json") == []

    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    conn.execute("INSERT INTO projects(id,name,type,domain,locale) "
                 "VALUES(1,'ecrett','local_business','ecrett.kr','ko-KR')")
    conn.executemany(
        "INSERT INTO gsc_snapshots(project_id,snapshot_date,period_days,query,clicks,"
        "impressions,ctr,position) VALUES(1,'2026-08-20',28,?,1,?,0.1,9.0)",
        [("무료 배경음악", 900), ("브이로그 bgm", 100)])
    conn.commit()
    b = brief(conn, "ecrett")
    assert b["queries"] == ["무료 배경음악", "브이로그 bgm"], b   # 노출 많은 순
    assert "ecrett.kr" in user_msg(b, 5) and "exactly 5" in user_msg(b, 5)

    # 사이트가 **무엇을 파는지**가 재료에 실리는가. 이게 빠져 있어서 어느 사이트에
    # 물어도 업종 일반형("무료 영상 편집 사이트 추천해줘")만 나왔다 — 자기 이름을
    # 붙인 기능도 좁은 쓰임도 모델이 알 길이 없었다.
    conn.executemany(
        "INSERT INTO gsc_snapshots(project_id,snapshot_date,period_days,query,page,clicks,"
        "impressions,ctr,position) VALUES(1,'2026-08-20',28,?,?,1,?,0.1,9.0)",
        [("무료 배경음악", "https://ecrett.kr/signature/scene-bgm/", 500),
         ("무료 배경음악", "https://ecrett.kr/en/signature/scene-bgm/", 400),  # 다국어 사본
         ("브이로그 bgm", "https://ecrett.kr/category/notice/", 300),          # 목록 자리
         ("브이로그 bgm", "https://ecrett.kr/niche/short-loop-bgm/", 200)])
    conn.commit()
    o = offers(conn, 1)
    assert "/signature/scene-bgm" in o, o                   # 노출 많은 순으로 먼저
    assert "/niche/short-loop-bgm" in o, o
    assert not [x for x in o if x.startswith("/en/")], f"다국어 사본을 안 접었다: {o}"
    assert not [x for x in o if "/category/" in x], f"목록 자리를 안 걸렀다: {o}"
    msg = user_msg(brief(conn, "ecrett"), 5)
    assert "/signature/scene-bgm" in msg, "페이지 목록이 재료에 안 실렸다"
    assert "Pages this site actually has" in msg, msg

    # 키가 없으면 조용히 빈 목록이 아니라 RuntimeError — 화면이 이유를 말해야 한다
    saved = os.environ.pop("OPENROUTER_API_KEY", None)
    try:
        suggest("ecrett", conn=conn)
        raise AssertionError("키가 없는데 그냥 진행했다")
    except RuntimeError as e:
        assert "OPENROUTER_API_KEY" in str(e), e
    finally:
        if saved:
            os.environ["OPENROUTER_API_KEY"] = saved

    os.environ["OPENROUTER_API_KEY"] = "test-key"
    seen = {}

    def fake_ask(model, prompt, api_key, locale):
        seen.update(model=model, prompt=prompt, key=api_key, locale=locale)
        return {"content": '[{"prompt":"무료 배경음악 어디가 잘해?","category":"추천",'
                           '"aim":"/signature/scene-bgm/"},'
                           '{"prompt":"브이로그 bgm 비용 얼마야?","category":"문제해결",'
                           '"aim":"/지어낸-페이지"},'
                           '{"prompt":"ecrett 써본 사람 있어?","category":"브랜드","aim":"brand"}]'}

    # 판 표시가 없던 시절의 질문 하나 — 생성기를 고친 뒤에도 똑같이 활성으로 남던 그것
    conn.execute("INSERT INTO ai_prompts(project_id,prompt,category) "
                 "VALUES(1,'판 표시 전에 들어온 옛 질문','추천')")
    conn.commit()
    rows = suggest("ecrett", n=3, conn=conn, ask=fake_ask)
    assert len(rows) == 3 and seen["locale"] == "ko-KR" and seen["key"] == "test-key"
    assert "무료 배경음악" in seen["prompt"], "GSC 검색어가 재료로 안 실렸다"
    # 겨냥은 준 재료에 글자 그대로 있을 때만 받는다 — 지어낸 페이지는 None
    assert [r["aim"] for r in rows] == ["page:/signature/scene-bgm", None, "brand"], rows
    assert save(conn, "ecrett", rows) == 3
    assert save(conn, "ecrett", rows) == 0, "같은 질문이 두 벌 들어간다"
    got = conn.execute("SELECT prompt, category, is_active, gen_version, aim FROM ai_prompts "
                       "ORDER BY id").fetchall()
    assert [tuple(r) for r in got] == [
        ("판 표시 전에 들어온 옛 질문", "추천", 1, None, None),   # 옛 행은 건드리지 않는다
        ("무료 배경음악 어디가 잘해?", "추천", 1, GEN_VERSION, "page:/signature/scene-bgm"),
        ("브이로그 bgm 비용 얼마야?", "문제해결", 1, GEN_VERSION, None),
        ("ecrett 써본 사람 있어?", "브랜드", 1, GEN_VERSION, "brand")], [tuple(r) for r in got]
    # 사람이 직접 적은 질문은 0 — 구버전(NULL)과 섞이면 방금 넣은 질문이 "다시 만들라"에 뜬다
    assert db.add_ai_prompts(conn, 1, [{"prompt": "사람이 적은 질문입니다"}]) == 1
    hand = conn.execute("SELECT gen_version FROM ai_prompts WHERE prompt='사람이 적은 질문입니다'"
                        ).fetchone()[0]
    assert hand == 0 and not outdated(hand), hand
    assert outdated(None) and not outdated(GEN_VERSION), "판 판정이 뒤집혔다"
    assert GEN_VERSION < 2 or outdated(GEN_VERSION - 1), "옛 판을 구버전으로 안 센다"

    # 다시 만들기 = 바꾸기 — 옛 판(NULL)만 끄고, 새 판·사람이 적은 질문은 그대로. 지우지 않는다.
    conn.execute("INSERT INTO ai_checks(prompt_id, engine, cited) SELECT id, 'chatgpt', 0 "
                 "FROM ai_prompts WHERE gen_version IS NULL")
    assert retire_outdated(conn, "ecrett") == 1
    act = {r[0]: r[1] for r in conn.execute("SELECT prompt, is_active FROM ai_prompts")}
    assert act["판 표시 전에 들어온 옛 질문"] == 0, act
    assert act["사람이 적은 질문입니다"] == 1 and act["ecrett 써본 사람 있어?"] == 1, act
    assert conn.execute("SELECT COUNT(*) FROM ai_checks").fetchone()[0] == 1, "인용 이력이 지워졌다"
    assert retire_outdated(conn, "ecrett") == 0, "두 번 끄면 0 이어야 한다"

    # 재료 쏠림(gucci) — 페이지 감사를 주소 가나다순으로 앞에서 자르면 /beauty… 가 자리를
    # 다 먹었다. 수요 순·섹션마다 돌아가며 실어야 가방·의류가 들어온다. 봇 차단(403)
    # 페이지의 제목("Access Denied")은 재료가 아니다.
    g = sqlite3.connect(":memory:")
    g.row_factory = sqlite3.Row
    g.executescript(db.SCHEMA)
    g.execute("INSERT INTO projects(id,name,type,domain,locale) "
              "VALUES(1,'gucci','commerce','gucci.com','ko-KR')")
    audits = [(f"https://www.gucci.com/kr/ko/ca/beauty/item-{i:02d}", 200, f"뷰티 {i}")
              for i in range(30)]
    audits += [("https://www.gucci.com/kr/ko/ca/women/handbags", 200, "여성 가방"),
               ("https://www.gucci.com/kr/ko/ca/men/ready-to-wear", 200, "남성 의류"),
               ("https://www.gucci.com/kr/ko/ca/women/shoes", 403, "Access Denied")]
    g.executemany("INSERT INTO page_audits(project_id,checked_date,url,status,title) "
                  "VALUES(1,'2026-09-20',?,?,?)", audits)
    g.executemany("INSERT INTO labs_ranked(project_id,checked_date,keyword,url,volume) "
                  "VALUES(1,'2026-09-20',?,?,?)",
                  [("구찌 가방", "https://www.gucci.com/kr/ko/ca/women/handbags", 9000),
                   ("구찌 남자 옷", "https://www.gucci.com/kr/ko/ca/men/ready-to-wear", 4000),
                   ("구찌 향수", "https://www.gucci.com/kr/ko/ca/beauty/item-29", 3000)])
    g.executemany("INSERT INTO keywords(project_id,keyword,cluster,volume,is_active) "
                  "VALUES(1,?,?,?,1)",
                  [("구찌 립스틱", "beauty", 100), ("구찌 향수", "beauty", 200),
                   ("구찌 가방", "handbags", 9000), ("구찌 벨트", "accessories", 800)])
    g.commit()
    o = offers(g, 1, limit=6)
    assert o[0] == "/ca/women/handbags — 여성 가방", o          # 수요 1위 섹션이 먼저
    assert "/ca/men/ready-to-wear — 남성 의류" in o, o
    assert sum(x.startswith("/ca/beauty/") for x in o) <= 4, f"뷰티가 재료를 다 먹었다: {o}"
    assert o.index("/ca/beauty/item-29 — 뷰티 29") < o.index("/ca/beauty/item-00 — 뷰티 0"), o
    assert not [x for x in o if "Access Denied" in x or "/shoes" in x], f"차단 페이지가 실렸다: {o}"
    gb = brief(g, "gucci")
    assert gb["queries"] == [] and gb["ranked"][0] == "구찌 가방", gb     # 서치콘솔 없음 → 추정 순위
    assert gb["clusters"][0] == "handbags" and gb["clusters"][-1] == "beauty", gb   # 이름순 아님
    assert gb["tracked"][:3] == ["구찌 가방", "구찌 벨트", "구찌 향수"], gb        # 주제마다 한 자리씩
    m = user_msg(gb, 7)
    assert "ranks for (estimated" in m and "구찌 가방" in m, m
    assert targets(gb).get("구찌 벨트") == "keyword:구찌 벨트", "추적 검색어를 겨냥으로 못 받는다"
    assert "no single topic" in SYSTEM, "프롬프트가 고르게 나누라고 말하지 않는다"
    os.environ.pop("OPENROUTER_API_KEY", None)
    print("gen_prompts self-check ok")


if __name__ == "__main__":
    main()
