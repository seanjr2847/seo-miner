#!/usr/bin/env python3
"""AI에 물어볼 질문 만들기 — [AI 인용] 화면이 비어 있던 진짜 이유.

`ai` 단계는 ai_prompts 를 재료로 돈다. 그런데 그 표를 채우는 코드가 이 리포에
없었다 — 질문은 채팅(`/capture add`)에서 Claude 가 직접 INSERT 해 왔고, 대시보드
폼으로 만든 사이트는 그 표가 영원히 비어 있었다. 그래서 웹 사용자는 [AI 인용
다시 확인]을 눌러도 "질문이 아직 없습니다"로 즉시 실패했고, 화면에는 아무 변화가
없어 버튼이 죽은 것처럼 보였다.

여기서는 사이트가 이미 가진 사실(이름·도메인·업종·로케일·GSC 상위 검색어)로
**사람이 실제로 AI에 물어볼 법한 질문**을 만든다. 키워드가 아니라 질문이다 —
"밀리아 제거"가 아니라 "밀리아 제거 잘하는 병원 어디야?" 쪽이다.

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
MIN_LEN, MAX_LEN = 6, 120

# 생성기 판 — 새 질문마다 ai_prompts.gen_version 으로 찍힌다(save). 판이 없던 동안,
# 사이트를 모르고 지은 옛 질문 1~20번이 생성기를 고친 뒤에도 똑같이 활성으로 남아
# 그 질문들의 "인용 0"이 실제 격차처럼 기회 목록에 올랐고, 아무도 그걸 가를 수 없었다.
#   NULL — 판을 찍기 전에 들어온 질문. 1판이 섞여 있어 구버전으로 센다(outdated)
#   0    — 생성기가 아니라 사람이 직접 적은 질문(db.add_ai_prompts 의 기본값). 구버전 아님
#   1    — (기록 없음) 업종·GSC 검색어만 보고 짓던 판
#   2    — 사이트 페이지 목록(offers)을 재료로 받고, 질문마다 겨냥(aim)을 적는 판
# 짓는 방식을 바꾸면 이 수를 올린다 — 그 전 판 질문이 전부 "다시 만들기 권함"으로 뜬다.
GEN_VERSION = 2
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
    "offers. Prefer what is specific to this site (its own named services, the niche "
    "conditions it treats, its practitioners) over generic industry terms that every "
    "competitor also targets. Never invent a service the page list does not show. "
    "For 브랜드, ask about the site's own named services and people too, not just its name."
)


# 다국어 사본은 같은 것을 두세 번 말한다 — 경로 앞의 로케일 조각을 접는다.
_LOCALE_SEG = re.compile(r"^/(?:[a-z]{2}(?:-[a-z]{2,4})?)(?=/)", re.I)
# 목록·공지처럼 무엇을 파는지 안 말하는 자리는 재료가 못 된다.
_NOISE = re.compile(r"^/(?:category|notice|page|tag|author|search|wp-)", re.I)


def offers(conn, project_id: int, *, limit: int = 24) -> list[str]:
    """사이트가 **무엇을 하는 곳인지** — 질문을 지을 때 이게 없으면 업종 일반형만 나온다.

    이 함수가 없던 시절의 결과가 그 증거다: 어느 피부과에 물어도 "강남 써마지 잘하는
    피부과 추천해줘"가 나왔다. 병원이 자기 이름을 붙여 파는 시술(디아더 PTT)도,
    특수클리닉(한관종·비립종)도 재료에 없었으니 모델이 알 길이 없었다.

    제목이 있으면 제목을 쓴다(사람 말이다). 없으면 URL 경로가 대신 말해 준다 —
    /signature/theother-ptt/ 는 경로 자체가 그 병원만의 시술 이름을 담는다.
    셋 다 없으면 빈 목록이다: 없는 것을 지어내지 않는다.
    """
    rows = []
    # 1) 페이지 감사 — 제목·H1 까지 있는 가장 좋은 재료
    try:
        rows = [(r["url"], r["title"]) for r in conn.execute(
            """SELECT url, title FROM page_audits WHERE project_id=?
                AND checked_date=(SELECT MAX(checked_date) FROM page_audits
                                   WHERE project_id=?)""", (project_id, project_id))]
    except sqlite3.Error:
        rows = []
    # 2) 크롤 회차의 제목
    if not rows:
        rows = [(r["url"], r["title"]) for r in conn.execute(
            """SELECT url, title FROM crawl_pages WHERE run_id=(
                 SELECT MAX(id) FROM crawl_runs WHERE project_id=?)""", (project_id,))]
    # 3) GSC 가 본 페이지 — 제목은 없지만 경로가 남는다. 노출 많은 순이 곧 중요한 순이다.
    if not rows:
        rows = [(r["page"], None) for r in conn.execute(
            """SELECT page, SUM(impressions) imp FROM gsc_snapshots
                WHERE project_id=? AND page IS NOT NULL
             GROUP BY page ORDER BY imp DESC""", (project_id,))]

    out, seen = [], set()
    for url, title in rows:
        path = urllib.parse.unquote(re.sub(r"^https?://[^/]+", "", str(url or "")))
        path = _LOCALE_SEG.sub("", path.split("?")[0]).rstrip("/") or "/"
        if path == "/" or _NOISE.match(path) or path in seen:
            continue
        seen.add(path)
        t = " ".join(str(title or "").split())
        out.append(f"{path} — {t}" if t else path)
        if len(out) >= limit:
            break
    return out

def brief(conn, project: str, top_n: int = 15) -> dict:
    """질문을 지을 재료 — 사이트가 이미 가진 사실만. 없으면 없는 대로 짓는다."""
    p = db.get_project(conn, project)
    cfg = {}
    if p["config_path"]:
        try:
            cfg = db.load_project_yaml(p["config_path"])
        except (db.ProjectConfigNotFound, ImportError):
            pass
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
            # 이 둘이 "이 사이트가 무엇을 하는 곳인가"를 말한다. 없으면 모델은 업종만
            # 알고 짓게 되고, 그러면 어느 병원에 물어도 같은 질문이 나온다.
            "offers": offers(conn, p["id"]),
            "seeds": (cfg.get("seed_keywords") or [])[:20],
            # 추적 중인 주제 묶음 — 질문이 무엇을 겨냥했는지(aim) 적을 세 번째 재료다.
            "clusters": [r[0] for r in conn.execute(
                """SELECT DISTINCT cluster FROM keywords
                    WHERE project_id=? AND is_active=1 AND cluster IS NOT NULL
                      AND TRIM(cluster)<>'' ORDER BY cluster LIMIT 15""", (p["id"],))]}


def targets(b: dict) -> dict[str, str]:
    """재료 목록 → 겨냥 이름. {모델이 베껴 올 글자: 'page:/경로' 같은 저장 꼴}.

    모델에게 준 목록이 곧 받을 수 있는 겨냥의 전부다 — 여기 없는 글자는 버린다.
    페이지는 offers() 가 준 "경로 — 제목" 에서 경로만 쓴다(모델은 경로를 베낀다).
    """
    out: dict[str, str] = {}
    for c in b.get("clusters") or []:
        out[str(c).strip()] = f"cluster:{str(c).strip()}"
    for k in b.get("seeds") or []:
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
    topics = ", ".join(b.get("clusters") or []) or "(none)"
    # 페이지 목록은 줄바꿈으로 준다 — 쉼표로 이으면 경로끼리 붙어 한 줄로 읽힌다.
    pages = "\n".join(f"  {x}" for x in (b.get("offers") or [])) or "  (none yet)"
    lang, country = serp_adapter.describe(b["locale"])
    return (f"Site: {b['name']} ({b['domain']})\n"
            f"Kind: {b['type']}\n"
            f"Audience: {lang} speakers in {country} ({b['locale']}) — write in {lang}.\n"
            f"Brand names: {alias}\n"
            f"Search queries this site already gets impressions for: {q}\n"
            f"Keywords this site targets: {seeds}\n"
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


def main() -> None:
    if len(sys.argv) == 1:
        _selfcheck()
        return
    ap = argparse.ArgumentParser(description="AI에 물어볼 질문 만들기")
    ap.add_argument("--project", required=True)
    ap.add_argument("--limit", type=int, default=20, help="만들 질문 수 (기본 20)")
    ap.add_argument("--dry-run", action="store_true", help="저장하지 않고 보여만 준다")
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
    finally:
        conn.close()
    print(f"\n질문 {added}개 저장 (이미 있던 것은 그대로). "
          f"이제 `python collect_ai.py --project {a.project}` 로 인용을 확인합니다.")


def _selfcheck() -> None:
    # 갈래 한 벌 — 기본값은 선택지 안에 있고, 만드는 넷은 선택지의 진부분집합이다.
    # (화면이 고를 수 있는데 여기가 모르는 값이 있으면 안 된다.)
    assert DEFAULT_CATEGORY in CATEGORY_CHOICES and DEFAULT_CATEGORY not in CATEGORIES
    assert set(CATEGORIES) < set(CATEGORY_CHOICES), (CATEGORIES, CATEGORY_CHOICES)
    assert "|".join(CATEGORIES) in SYSTEM, "프롬프트가 갈래 사본을 들고 있다"

    # 파싱: 코드펜스·설명·중복·길이 밖·잘못된 카테고리를 전부 지나간다
    raw = ('설명 한 줄\n```json\n['
           '{"prompt":"밀리아 제거 잘하는 병원 어디야?","category":"추천"},'
           '{"prompt":"밀리아 제거 잘하는 병원 어디야?","category":"추천"},'
           '{"prompt":"짧음","category":"추천"},'
           '{"prompt":"점 빼기랑 밀리아 제거 뭐가 달라?","category":"엉뚱"},'
           '"문자열로 온 질문도 받는다 밀리아"'
           ']\n```\n뒷말')
    got = parse(raw)
    assert [g["prompt"] for g in got] == [
        "밀리아 제거 잘하는 병원 어디야?", "점 빼기랑 밀리아 제거 뭐가 달라?",
        "문자열로 온 질문도 받는다 밀리아"], got
    assert got[0]["category"] == "추천" and got[1]["category"] == DEFAULT_CATEGORY, got
    assert parse("배열이 없다") == [] and parse("[깨진 json") == []

    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    conn.execute("INSERT INTO projects(id,name,type,domain,locale) "
                 "VALUES(1,'clinic','local_clinic','clinic.kr','ko-KR')")
    conn.executemany(
        "INSERT INTO gsc_snapshots(project_id,snapshot_date,period_days,query,clicks,"
        "impressions,ctr,position) VALUES(1,'2026-08-20',28,?,1,?,0.1,9.0)",
        [("밀리아 제거", 900), ("점 빼기", 100)])
    conn.commit()
    b = brief(conn, "clinic")
    assert b["queries"] == ["밀리아 제거", "점 빼기"], b        # 노출 많은 순
    assert "clinic.kr" in user_msg(b, 5) and "exactly 5" in user_msg(b, 5)

    # 사이트가 **무엇을 파는지**가 재료에 실리는가. 이게 빠져 있어서 어느 병원에
    # 물어도 업종 일반형("강남 써마지 잘하는 피부과")만 나왔다 — 자기 이름을 붙인
    # 시술도 특수클리닉도 모델이 알 길이 없었다.
    conn.executemany(
        "INSERT INTO gsc_snapshots(project_id,snapshot_date,period_days,query,page,clicks,"
        "impressions,ctr,position) VALUES(1,'2026-08-20',28,?,?,1,?,0.1,9.0)",
        [("밀리아 제거", "https://clinic.kr/signature/clinic-ptt/", 500),
         ("밀리아 제거", "https://clinic.kr/en/signature/clinic-ptt/", 400),   # 다국어 사본
         ("점 빼기", "https://clinic.kr/category/notice/", 300),               # 목록 자리
         ("점 빼기", "https://clinic.kr/special-clinic/syringoma-milia/", 200)])
    conn.commit()
    o = offers(conn, 1)
    assert "/signature/clinic-ptt" in o, o                  # 노출 많은 순으로 먼저
    assert "/special-clinic/syringoma-milia" in o, o
    assert not [x for x in o if x.startswith("/en/")], f"다국어 사본을 안 접었다: {o}"
    assert not [x for x in o if "/category/" in x], f"목록 자리를 안 걸렀다: {o}"
    msg = user_msg(brief(conn, "clinic"), 5)
    assert "/signature/clinic-ptt" in msg, "페이지 목록이 재료에 안 실렸다"
    assert "Pages this site actually has" in msg, msg

    # 키가 없으면 조용히 빈 목록이 아니라 RuntimeError — 화면이 이유를 말해야 한다
    saved = os.environ.pop("OPENROUTER_API_KEY", None)
    try:
        suggest("clinic", conn=conn)
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
        return {"content": '[{"prompt":"밀리아 제거 어디가 잘해?","category":"추천",'
                           '"aim":"/signature/clinic-ptt/"},'
                           '{"prompt":"점 빼기 비용 얼마야?","category":"문제해결",'
                           '"aim":"/지어낸-페이지"},'
                           '{"prompt":"clinic 피부과 어때?","category":"브랜드","aim":"brand"}]'}

    # 판 표시가 없던 시절의 질문 하나 — 생성기를 고친 뒤에도 똑같이 활성으로 남던 그것
    conn.execute("INSERT INTO ai_prompts(project_id,prompt,category) "
                 "VALUES(1,'판 표시 전에 들어온 옛 질문','추천')")
    conn.commit()
    rows = suggest("clinic", n=3, conn=conn, ask=fake_ask)
    assert len(rows) == 3 and seen["locale"] == "ko-KR" and seen["key"] == "test-key"
    assert "밀리아 제거" in seen["prompt"], "GSC 검색어가 재료로 안 실렸다"
    # 겨냥은 준 재료에 글자 그대로 있을 때만 받는다 — 지어낸 페이지는 None
    assert [r["aim"] for r in rows] == ["page:/signature/clinic-ptt", None, "brand"], rows
    assert save(conn, "clinic", rows) == 3
    assert save(conn, "clinic", rows) == 0, "같은 질문이 두 벌 들어간다"
    got = conn.execute("SELECT prompt, category, is_active, gen_version, aim FROM ai_prompts "
                       "ORDER BY id").fetchall()
    assert [tuple(r) for r in got] == [
        ("판 표시 전에 들어온 옛 질문", "추천", 1, None, None),   # 옛 행은 건드리지 않는다
        ("밀리아 제거 어디가 잘해?", "추천", 1, GEN_VERSION, "page:/signature/clinic-ptt"),
        ("점 빼기 비용 얼마야?", "문제해결", 1, GEN_VERSION, None),
        ("clinic 피부과 어때?", "브랜드", 1, GEN_VERSION, "brand")], [tuple(r) for r in got]
    # 사람이 직접 적은 질문은 0 — 구버전(NULL)과 섞이면 방금 넣은 질문이 "다시 만들라"에 뜬다
    assert db.add_ai_prompts(conn, 1, [{"prompt": "사람이 적은 질문입니다"}]) == 1
    hand = conn.execute("SELECT gen_version FROM ai_prompts WHERE prompt='사람이 적은 질문입니다'"
                        ).fetchone()[0]
    assert hand == 0 and not outdated(hand), hand
    assert outdated(None) and not outdated(GEN_VERSION), "판 판정이 뒤집혔다"
    assert GEN_VERSION < 2 or outdated(GEN_VERSION - 1), "옛 판을 구버전으로 안 센다"
    os.environ.pop("OPENROUTER_API_KEY", None)
    print("gen_prompts self-check ok")


if __name__ == "__main__":
    main()
