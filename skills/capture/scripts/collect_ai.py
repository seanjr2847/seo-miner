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
import time
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
    r.raise_for_status()
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
            ai_samples: int | None = None,
            max_ai_prompts: int | None = None,
            throttle: float | None = None,
            ids: str | None = None,
            category: str | None = None,
            force: bool = False) -> collector.StageResult:
    """AI 가시성 체크 (OpenRouter) — 결과를 Brain 에 적재한다.

    Args:
        project: 사이트 이름
        dry_run: True 면 호출 계획만 찍고 종료
        engines: comma list (예: "chatgpt,perplexity,gemini"). 미지정시 config.
        ai_samples: 프롬프트×엔진 당 샘플 수
        max_ai_prompts: 활성 프롬프트 상한
        throttle: 요청 간격(초)
        ids: 쉼표 구분 ai_prompt id — 지정하면 is_active 무시
        category: 이 카테고리의 활성 프롬프트만 실행
        force: 오늘 이미 확인한 질문도 재확인

    Returns:
        StageResult(ok=...). 사유 있는 비종료는 ok=False, skipped=True.
    """
    _parser()
    conn, p, cfg = collector.open_project(project)
    gcfg = collector.config()
    ns = argparse.Namespace(
        throttle=throttle,
        samples=ai_samples,
        max_prompts=max_ai_prompts,
    )
    s = collector.settings(ns, cfg)
    samples = s["ai_samples"]
    throttle = s["throttle"]
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
        conn.close()
        return collector.StageResult(ok=False, skipped=True,
                                     reason=f"AI에 물어볼 질문이 아직 없습니다 ({p['name']}). 채팅에 "
                                            f"`/capture add {p['name']}` 이라고 하시면 프로젝트에 맞는 질문 10~30개를 "
                                            "만들어 드립니다 (대시보드 폼으로 만든 사이트는 이 단계가 비어 있습니다).")

    from datetime import datetime
    today = datetime.now().strftime("%Y-%m-%d")
    if not force:
        # 오늘(started_at이 오늘인 kind='ai' 런) 이미 기록된 (prompt_id, engine, sample_idx)는 건너뛴다
        checked_today = {
            (r[0], r[1], r[2])
            for r in conn.execute(
                """SELECT c.prompt_id, c.engine, c.sample_idx
                     FROM ai_checks c
                     JOIN runs r ON r.id = c.run_id
                    WHERE r.project_id = ?
                      AND r.kind = 'ai'
                      AND (date(r.started_at) = ? OR date(r.started_at, 'localtime') = ?)""",
                (p["id"], today, today)
            ).fetchall()
        }
    else:
        checked_today = set()

    total_calls = len(prompts) * len(engines_d) * samples
    tasks_to_run = []
    for row in prompts:
        for engine in engines_d:
            for sample in range(samples):
                if (row["id"], engine, sample) not in checked_today:
                    tasks_to_run.append((row["id"], engine, sample))
    skipped_calls = total_calls - len(tasks_to_run)
    calls_to_make = len(tasks_to_run)

    skip_msg = f" (skipped {skipped_calls} 오늘 이미 확인)" if skipped_calls else ""
    print(f"[ai] prompts={len(prompts)} engines={list(engines_d)} samples={samples} "
          f"-> {calls_to_make} calls{skip_msg}")
    print("     note: provider-native search may bill a per-search fee on top of tokens.")
    if dry_run:
        for r in prompts[:5]:
            print(f"     e.g. [{r['category']}] {r['prompt']}")
        conn.close()
        return collector.StageResult(ok=True, skipped=True, rows=0)

    if calls_to_make == 0:
        print(f"\nsaved 0 checks (skipped {skipped_calls} 오늘 이미 확인). failures=0/0. "
              f"Next: /capture gaps or /capture report")
        conn.close()
        return collector.StageResult(ok=True, skipped=True, rows=0)

    import os
    if not serp_adapter.has_openrouter():
        conn.close()
        return collector.StageResult(ok=False, skipped=True,
                                     reason="OPENROUTER_API_KEY not set. See references/setup.md")
    api_key = os.environ["OPENROUTER_API_KEY"]

    aliases = scoring.aliases_of(cfg)   # 별칭 규칙 정본은 scoring — 갈라지면 비교 불능
    own_domain = p["domain"]
    # r.api_calls를 직접 센다 — 도중에 죽어도 그때까지 부른 횟수가 남는다.
    with db.run(conn, p["id"], "ai") as r:
        run_id = r.id
        errors = 0
        for row in prompts:
            prompt_done = False
            for engine, model in engines_d.items():
                for sample in range(samples):   # 이름 주의: s는 위의 settings dict
                    if (row["id"], engine, sample) in checked_today:
                        continue
                    try:
                        res = ask(model, row["prompt"], api_key, p["locale"])
                        mentioned, cited, others = scoring.judge(
                            res["content"], res["citation_urls"], aliases, own_domain)
                        db.record_ai_check(conn, row["id"], run_id, engine, sample,
                                           mentioned, cited, others, res["content"])
                        r.api_calls += 1
                        prompt_done = True
                    except Exception as e:
                        errors += 1
                        print(f"  ! {engine} failed on prompt#{row['id']}: {e}", file=sys.stderr)
                    time.sleep(throttle)
            if prompt_done:
                print(f"  prompt#{row['id']} [{row['category']}] done")
        r.notes = f"engines={list(engines_d)} samples={samples} errors={errors} skipped={skipped_calls}"

    # 실패는 카운트만 하고 계속 갔으니, 끝에서 한 번 크게 말한다 —
    # 아래 매트릭스의 분모가 그만큼 줄어든 것을 보이게.
    if errors:
        print(f"[ai] {errors}/{calls_to_make} 요청 실패 — 매트릭스 분모가 그만큼 축소됨",
              file=sys.stderr)

    # summary matrix: engine x category -> cited/total
    print("\nvisibility matrix (cited / checks):")
    for r in conn.execute(
        """SELECT c.engine, p2.category,
                  SUM(c.cited) AS cited, COUNT(*) AS total
             FROM ai_checks c JOIN ai_prompts p2 ON p2.id=c.prompt_id
            WHERE c.run_id=? GROUP BY 1,2 ORDER BY 1,2""", (run_id,)):
        print(f"  {r['engine']:<11} {r['category']:<10} {r['cited']}/{r['total']}")
    skip_summary = f" (skipped {skipped_calls} 오늘 이미 확인)" if skipped_calls else ""
    print(f"\nrun_id={run_id} saved{skip_summary}. failures={errors}/{calls_to_make}. "
          f"Next: /capture gaps or /capture report")
    conn.close()
    return collector.StageResult(ok=True, skipped=False, rows=calls_to_make - errors)


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
    a = _parser().parse_args()
    r = collect(a.project, dry_run=a.dry_run,
                engines=a.engines,
                ai_samples=a.samples,
                max_ai_prompts=a.max_prompts,
                throttle=a.throttle,
                ids=a.ids,
                category=a.category,
                force=a.force)
    if not r.ok and r.reason:
        sys.exit(r.reason)


if __name__ == "__main__":
    main()
