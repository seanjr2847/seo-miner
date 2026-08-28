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
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import db  # noqa: E402

# 검색이 필요 없는 작업이다(:online 을 안 붙인다) — 질문을 짓는 것뿐이라 싸고 빠른
# 모델로 충분하다. 인용 확인에 쓰는 엔진 표(collect_ai.DEFAULT_ENGINES)와는 별개다.
MODEL = "openai/gpt-4o-mini"
CATEGORIES = ("추천", "비교", "문제해결", "브랜드")
MIN_LEN, MAX_LEN = 6, 120

SYSTEM = (
    "You design the question set used to measure whether a site gets cited by AI "
    "assistants. Return ONLY a JSON array, no prose, no code fence. Each item: "
    '{"prompt": "...", "category": "추천|비교|문제해결|브랜드"}. '
    "Write the prompts in the site's language, phrased the way a real person types "
    "into ChatGPT — full questions, not keywords, and never mention that this is a test. "
    "Cover all four categories: 추천 (asking for recommendations in this field), "
    "비교 (comparing options), 문제해결 (solving the problem the site addresses), "
    "브랜드 (asking about this brand by name). Most prompts must NOT name the brand — "
    "the point is to find out who gets cited when the user does not already know us."
)


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
            "locale": p["locale"] or "ko-KR",
            "aliases": cfg.get("brand_aliases") or [],
            "queries": queries}


def user_msg(b: dict, n: int) -> str:
    q = ", ".join(b["queries"][:15]) or "(아직 없음)"
    alias = ", ".join(x for x in [b["name"], *b["aliases"]] if x)
    return (f"Site: {b['name']} ({b['domain']})\n"
            f"Kind: {b['type']}\nAudience locale: {b['locale']}\n"
            f"Brand names: {alias}\n"
            f"Search queries this site already gets impressions for: {q}\n\n"
            f"Write exactly {n} prompts.")


def parse(text: str) -> list[dict]:
    """모델 응답 → [{prompt, category}]. 코드펜스·설명이 붙어 와도 배열만 건진다.

    형식이 틀렸다고 통째로 버리지 않는다 — 배열 하나만 건지면 나머지 잡소리는
    무해하다. 다만 항목 단위로는 엄격하다: 질문이 아니면 안 쓴다.
    """
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
        out.append({"prompt": prompt, "category": cat if cat in CATEGORIES else "general"})
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
    return parse(res.get("content", ""))[:n]


def save(conn, project: str, rows: list[dict]) -> int:
    """만든 질문을 ai_prompts 에 넣는다. 이미 있는 질문은 건드리지 않는다."""
    pid = db.get_project(conn, project)["id"]
    return db.add_ai_prompts(conn, pid, rows)


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
        print(f"  [{r['category']}] {r['prompt']}")
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
    assert got[0]["category"] == "추천" and got[1]["category"] == "general", got
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
        return {"content": '[{"prompt":"밀리아 제거 어디가 잘해?","category":"추천"},'
                           '{"prompt":"점 빼기 비용 얼마야?","category":"문제해결"}]'}

    rows = suggest("clinic", n=2, conn=conn, ask=fake_ask)
    assert len(rows) == 2 and seen["locale"] == "ko-KR" and seen["key"] == "test-key"
    assert "밀리아 제거" in seen["prompt"], "GSC 검색어가 재료로 안 실렸다"
    assert save(conn, "clinic", rows) == 2
    assert save(conn, "clinic", rows) == 0, "같은 질문이 두 벌 들어간다"
    got = conn.execute("SELECT prompt, category, is_active FROM ai_prompts "
                       "ORDER BY id").fetchall()
    assert [(r["prompt"], r["category"], r["is_active"]) for r in got] == [
        ("밀리아 제거 어디가 잘해?", "추천", 1),
        ("점 빼기 비용 얼마야?", "문제해결", 1)], [tuple(r) for r in got]
    os.environ.pop("OPENROUTER_API_KEY", None)
    print("gen_prompts self-check ok")


if __name__ == "__main__":
    main()
