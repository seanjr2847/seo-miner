#!/usr/bin/env python3
"""AI visibility check via OpenRouter (one key, native provider search).

For each active ai_prompt x engine (x samples), asks the question with web
search enabled and records:
  mentioned       brand alias appears in the answer text
  cited           own domain appears in url_citation annotations
  cited_domains   who IS cited (fuel for the citation-gap analysis)

Notes on measurement honesty (see references/scoring.md):
  * Answers are non-deterministic -> one check is a sample, not a fact.
  * API+native search approximates, not equals, the consumer apps.
  * Search tool calls carry per-request fees on some providers -> caps + dry-run.

Env: OPENROUTER_API_KEY
Usage:
  python collect_ai.py --project NAME [--engines chatgpt,perplexity,gemini]
                       [--samples 1] [--max-prompts 30] [--ids 16,17]
                       [--category 사실] [--dry-run]
"""
import argparse
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))
import collector  # noqa: E402
import db  # noqa: E402
import scoring  # noqa: E402
import serp_adapter  # noqa: E402

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# Model slugs drift over time; override in config.yaml -> ai_engines.
DEFAULT_ENGINES = {
    "chatgpt":    "openai/gpt-4o-mini:online",
    "perplexity": "perplexity/sonar",            # search built-in
    "gemini":     "google/gemini-2.5-flash:online",
    # "claude":   "anthropic/claude-sonnet-4.5:online",   # optional, same key
}

# 로케일을 안 실으면 한국어 질문에도 미국 소스가 붙는다 — 소비자 앱이 보는 화면과
# 어긋나서, 인용 갭 분석이 엉뚱한 경쟁 도메인을 세게 된다.
SYSTEM = ("You are a helpful assistant. Answer the user's question the way you "
          "normally would for a real user, citing web sources. "
          "Answer in the language of the question, and prefer sources relevant "
          "to a {locale} audience.")


def openrouter_ok() -> tuple[bool, str]:
    """키가 살아 있나 — GET /auth/key, **무료 호출**. 반환: (쓸 수 있나, 사람 말).

    돈 쓰는 ai 단계 앞과 doctor 진단이 같은 이 함수를 쓴다(판정 두 벌 금지).
    확인 자체가 실패하면(네트워크·모르는 응답) True 다 — 카나리아가 수집을
    막으면 안 된다. 막는 건 "확실히 못 쓴다"일 때뿐.
    """
    import os
    if not serp_adapter.has_openrouter():
        return False, "OPENROUTER_API_KEY 없음"
    try:
        r = requests.get(
            "https://openrouter.ai/api/v1/auth/key",
            timeout=serp_adapter.TIMEOUTS["canary"],
            headers={"Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}"})
        if r.status_code in serp_adapter.FATAL_STATUS:
            return False, str(serp_adapter.fatal("OpenRouter", r.status_code))
        r.raise_for_status()
        d = (r.json().get("data") or {})
        limit, used = d.get("limit"), d.get("usage")
        if limit is not None and used is not None and float(used) >= float(limit):
            return False, (f"OpenRouter 크레딧 소진 (${float(used):.2f}/${float(limit):.2f}) — "
                           f"{serp_adapter.FATAL_FIX['OpenRouter']}")
        return True, "OpenRouter 키 유효"
    except Exception as e:
        return True, f"OpenRouter 키 확인 실패 ({e}) — 그대로 진행합니다"


def ask(model: str, prompt: str, api_key: str, locale: str) -> dict:
    body = {
        "model": model,
        "max_tokens": 1200,  # 700이면 긴 답변 뒤쪽 인용이 잘린다 — 토큰 비용 최대 ~1.7배
        "messages": [
            {"role": "system", "content": SYSTEM.format(locale=locale or "ko-KR")},
            {"role": "user", "content": prompt},
        ],
    }
    r = requests.post(
        OPENROUTER_URL, timeout=serp_adapter.TIMEOUTS["openrouter"],
        headers={"Authorization": f"Bearer {api_key}",
                 "Content-Type": "application/json"},
        json=body)
    # 401/402 는 Fatal — 키가 죽었거나 크레딧이 없는데 질문 60개를 계속 던지지 않는다.
    serp_adapter.raise_for(r, "OpenRouter")
    data = r.json()
    msg = data.get("choices", [{}])[0].get("message", {}) or {}
    content = msg.get("content") or ""
    urls = []
    for ann in (msg.get("annotations") or []):
        if ann.get("type") == "url_citation":
            u = (ann.get("url_citation") or {}).get("url", "")
            if u:
                urls.append(u)
    # 인용 메타데이터가 없을 때의 맨 URL fallback은 scoring.judge 가 한다.
    return {"content": content, "citation_urls": urls,
            "usage": data.get("usage", {})}


def collect(project: str, *,
            dry_run: bool = False,
            engines: str | None = None,
            samples: int | None = None,
            max_prompts: int | None = None,
            throttle: float | None = None,
            ids: str | None = None,
            category: str | None = None,
            force: bool = False,
            conn=None) -> collector.StageResult:
    """AI 가시성 체크 (OpenRouter) — 결과를 Brain 에 적재한다.

    Args:
        project: 사이트 이름
        dry_run: True 면 호출 계획만 찍고 종료
        engines: comma list (예: "chatgpt,perplexity,gemini"). 미지정시 config.
        samples: 프롬프트×엔진 당 샘플 수(config 키는 ai_samples) —
            CLI 플래그(--samples)와 이름을 맞춘다.
        max_prompts: 활성 프롬프트 상한(config 키는 limits.max_ai_prompts) —
            CLI 플래그(--max-prompts)와 이름을 맞춘다.
        throttle: 요청 간격(초)
        ids: 쉼표 구분 ai_prompt id — 지정하면 is_active 무시
        category: 이 카테고리의 활성 프롬프트만 실행
        force: 오늘 이미 확인한 질문도 재확인
        conn: 이미 열린 Brain 연결 — 주면 그것을 쓰고 닫지 않는다

    Returns:
        StageResult(ok=...). 사유 있는 비종료는 ok=False, skipped=True.
    """
    ap = _parser()
    with collector.stage(project, conn=conn, dry_run=dry_run) as st:
        conn, p, cfg = st.conn, st.project, st.cfg
        gcfg = collector.config()
        s = st.settings(ap, argparse.Namespace(
            throttle=throttle, samples=samples, max_prompts=max_prompts))
        samples = s["ai_samples"]
        max_ai_prompts = s["limits.max_ai_prompts"]

        engines_map = {**DEFAULT_ENGINES, **(gcfg.get("ai_engines") or {})}
        engine_names = ([e.strip() for e in engines.split(",")] if engines
                        else (cfg.get("surfaces_ai") or gcfg.get("default_ai_engines")
                              or ["chatgpt", "perplexity", "gemini"]))
        engines_d = {e: engines_map[e] for e in engine_names if e in engines_map}

        # 부분 실행. 이게 없으면 "새로 넣은 8개만 돌려보자"에도 is_active를 손으로
        # 토글해야 하고, 되돌릴 때 통째로 UPDATE 해서 큐레이션한 활성 집합을 날린다.
        limit = max_ai_prompts
        if ids:
            ids_list = [int(x) for x in ids.split(",") if x.strip()]
            prompts = conn.execute(
                f"""SELECT id, prompt, category FROM ai_prompts
                     WHERE project_id=? AND id IN ({','.join('?' * len(ids_list))}) ORDER BY id""",
                (p["id"], *ids_list)).fetchall()
        elif category:
            prompts = conn.execute(
                """SELECT id, prompt, category FROM ai_prompts
                    WHERE project_id=? AND is_active=1 AND category=? ORDER BY id LIMIT ?""",
                (p["id"], category, limit)).fetchall()
        else:
            prompts = conn.execute(
                """SELECT id, prompt, category FROM ai_prompts
                    WHERE project_id=? AND is_active=1 ORDER BY id LIMIT ?""",
                (p["id"], limit)).fetchall()
        if not prompts:
            return st.skip(f"AI에 물어볼 질문이 아직 없습니다 ({p['name']}). 채팅에 "
                           f"`/capture add {p['name']}` 이라고 하시면 프로젝트에 맞는 질문 10~30개를 "
                           "만들어 드립니다 (대시보드 폼으로 만든 사이트는 이 단계가 비어 있습니다).")

        # 오늘(started_at이 오늘인 kind='ai' 런) 이미 기록된 (prompt_id, engine, sample_idx)는
        # 건너뛴다. '오늘'을 만드는 자리와 --force 의 뜻은 러너가 갖는다 (Stage.seen_today).
        checked_today = st.seen_today(
            """SELECT c.prompt_id, c.engine, c.sample_idx
                 FROM ai_checks c
                 JOIN runs r ON r.id = c.run_id
                WHERE r.project_id = ?
                  AND r.kind = 'ai'
                  AND """ + collector.today_clause("r.started_at"),
            (p["id"],), force=force)

        total_calls = len(prompts) * len(engines_d) * samples
        # 프롬프트마다 "아직 안 본" 작업 목록. 프롬프트 단위로 묶어 두는 이유는
        # 한 프롬프트가 끝날 때마다 진행 표시를 찍기 위해서다 (긴 런의 유일한 신호).
        todo = [(row, [(engine, sample) for engine in engines_d for sample in range(samples)
                       if (row["id"], engine, sample) not in checked_today])
                for row in prompts]
        calls_to_make = sum(len(tasks) for _, tasks in todo)
        skipped_calls = total_calls - calls_to_make

        print(f"[ai] prompts={len(prompts)} engines={list(engines_d)} samples={samples} "
              f"-> {calls_to_make} calls{st.skip_note(skipped_calls)}")
        print("     note: provider-native search may bill a per-search fee on top of tokens.")
        if st.dry_run:
            for row in prompts[:5]:
                print(f"     e.g. [{row['category']}] {row['prompt']}")
            return st.noop(rows=0)

        if calls_to_make == 0:
            print(f"\nsaved 0 checks{st.skip_note(skipped_calls)}. failures=0/0. "
                  f"Next: /capture gaps or /capture report")
            return st.noop(rows=0)

        import os
        if not serp_adapter.has_openrouter():
            return st.skip("OPENROUTER_API_KEY not set. See references/setup.md")
        api_key = os.environ["OPENROUTER_API_KEY"]

        aliases = scoring.aliases_of(cfg)   # 별칭 규칙 정본은 scoring — 갈라지면 비교 불능
        own_domain = p["domain"]
        # r.api_calls를 직접 센다 — 도중에 죽어도 그때까지 부른 횟수가 남는다.
        with st.record("ai") as r:
            run_id = r.id

            def one(row, task) -> None:
                """질문 하나 × 엔진 × 샘플 — 실패는 러너가 세고 다음으로 넘어간다."""
                engine, sample = task
                res = ask(engines_d[engine], row["prompt"], api_key, p["locale"])
                mentioned, cited, others = scoring.judge(
                    res["content"], res["citation_urls"], aliases, own_domain)
                db.record_ai_check(conn, row["id"], run_id, engine, sample,
                                   mentioned, cited, others, res["content"])

            for row, tasks in todo:
                n = st.each(tasks, lambda t, row=row: one(row, t),
                            label=lambda t, row=row: f"{t[0]} failed on prompt#{row['id']}")
                r.api_calls += n
                if n:
                    print(f"  prompt#{row['id']} [{row['category']}] done")
            r.notes = (f"engines={list(engines_d)} samples={samples} "
                       f"{st.err_note} skipped={skipped_calls}")

        # 실패는 카운트만 하고 계속 갔으니, 끝에서 한 번 크게 말한다 —
        # 아래 매트릭스의 분모가 그만큼 줄어든 것을 보이게.
        if st.errors:
            print(f"[ai] {st.errors}/{calls_to_make} 요청 실패 — 매트릭스 분모가 그만큼 축소됨",
                  file=sys.stderr)

        # summary matrix: engine x category -> cited/total
        print("\nvisibility matrix (cited / checks):")
        for m in conn.execute(
            """SELECT c.engine, p2.category,
                      SUM(c.cited) AS cited, COUNT(*) AS total
                 FROM ai_checks c JOIN ai_prompts p2 ON p2.id=c.prompt_id
                WHERE c.run_id=? GROUP BY 1,2 ORDER BY 1,2""", (run_id,)):
            print(f"  {m['engine']:<11} {m['category']:<10} {m['cited']}/{m['total']}")
        print(f"\nrun_id={run_id} saved{st.skip_note(skipped_calls)}. "
              f"failures={st.errors}/{calls_to_make}. "
              f"Next: /capture gaps or /capture report")
        return st.verdict(calls_to_make - st.errors, rows=calls_to_make - st.errors)


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser()
    collector.add_common(ap)
    collector.add_setting(ap, "--throttle", key="throttle", fallback=0.5, type=float,
                          help="요청 간격(초). 기본은 config.yaml defaults.throttle")
    ap.add_argument("--engines", default=None, help="comma list; default from config")
    # 기본 2회 샘플(config.yaml ai_samples와 일치) — 비결정성 완화, 대신 호출 비용 2배.
    collector.add_setting(ap, "--samples", key="ai_samples", fallback=2, type=int)
    collector.add_setting(ap, "--max-prompts", key="limits.max_ai_prompts", fallback=30, type=int)
    ap.add_argument("--ids", help="쉼표로 구분한 ai_prompt id — 지정하면 is_active를 무시하고 이것만 실행")
    ap.add_argument("--category", help="이 카테고리의 활성 프롬프트만 실행")
    ap.add_argument("--force", action="store_true", help="오늘 이미 확인한 질문도 건너뛰지 않고 재확인")
    return ap


def main() -> None:
    collector.cli("ai")


if __name__ == "__main__":
    main()
