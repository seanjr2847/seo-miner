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
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).parent))
import db  # noqa: E402

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# Model slugs drift over time; override in config.yaml -> ai_engines.
DEFAULT_ENGINES = {
    "chatgpt":    "openai/gpt-4o-mini:online",
    "perplexity": "perplexity/sonar",            # search built-in
    "gemini":     "google/gemini-2.5-flash:online",
    # "claude":   "anthropic/claude-sonnet-4.5:online",   # optional, same key
}

SYSTEM = ("You are a helpful assistant. Answer the user's question the way you "
          "normally would for a real user, citing web sources.")


def load_config() -> dict:
    import yaml
    cfg_path = Path(__file__).parent.parent / "config.yaml"
    if cfg_path.exists():
        with open(cfg_path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    return {}


def norm_domain(url: str) -> str:
    try:
        host = urlparse(url).netloc.lower()
        return host[4:] if host.startswith("www.") else host
    except Exception:
        return ""


def ask(model: str, prompt: str, api_key: str, locale: str) -> dict:
    import requests
    body = {
        "model": model,
        "max_tokens": 700,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": prompt},
        ],
    }
    r = requests.post(
        OPENROUTER_URL, timeout=120,
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
    # fallback: some engines inline bare URLs in text
    if not urls:
        urls = re.findall(r"https?://[^\s)\]>\"']+", content)
    return {"content": content, "citation_urls": urls,
            "usage": data.get("usage", {})}


def judge(content: str, citation_urls: list[str], aliases: list[str],
          own_domain: str) -> tuple[int, int, list[str]]:
    text = content.lower()
    mentioned = int(any(a.lower() in text for a in aliases if a))
    domains = sorted({d for d in (norm_domain(u) for u in citation_urls) if d})
    own = own_domain.lower().removeprefix("www.")
    cited = int(any(d == own or d.endswith("." + own) for d in domains))
    others = [d for d in domains if not (d == own or d.endswith("." + own))]
    return mentioned, cited, others


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    ap.add_argument("--engines", default=None, help="comma list; default from config")
    ap.add_argument("--samples", type=int, default=1)
    ap.add_argument("--max-prompts", type=int, default=None)
    ap.add_argument("--throttle", type=float, default=0.5)
    ap.add_argument("--ids", help="쉼표로 구분한 ai_prompt id — 지정하면 is_active를 무시하고 이것만 실행")
    ap.add_argument("--category", help="이 카테고리의 활성 프롬프트만 실행")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    conn = db.connect()
    p = db.get_project(conn, a.project)
    cfg = db.load_project_yaml(p["config_path"] or a.project)
    gcfg = load_config()

    engines_map = {**DEFAULT_ENGINES, **(gcfg.get("ai_engines") or {})}
    engine_names = ([e.strip() for e in a.engines.split(",")] if a.engines
                    else (cfg.get("surfaces_ai") or gcfg.get("default_ai_engines")
                          or ["chatgpt", "perplexity", "gemini"]))
    engines = {e: engines_map[e] for e in engine_names if e in engines_map}

    # 부분 실행. 이게 없으면 "새로 넣은 8개만 돌려보자"에도 is_active를 손으로
    # 토글해야 하고, 되돌릴 때 통째로 UPDATE 해서 큐레이션한 활성 집합을 날린다.
    limit = a.max_prompts or (cfg.get("limits", {}) or {}).get("max_ai_prompts", 30)
    if a.ids:
        ids = [int(x) for x in a.ids.split(",") if x.strip()]
        prompts = conn.execute(
            f"""SELECT id, prompt, category FROM ai_prompts
                 WHERE project_id=? AND id IN ({','.join('?' * len(ids))}) ORDER BY id""",
            (p["id"], *ids)).fetchall()
    elif a.category:
        prompts = conn.execute(
            """SELECT id, prompt, category FROM ai_prompts
                WHERE project_id=? AND is_active=1 AND category=? ORDER BY id LIMIT ?""",
            (p["id"], a.category, limit)).fetchall()
    else:
        prompts = conn.execute(
            """SELECT id, prompt, category FROM ai_prompts
                WHERE project_id=? AND is_active=1 ORDER BY id LIMIT ?""",
            (p["id"], limit)).fetchall()
    if not prompts:
        sys.exit("no active ai_prompts. Have Claude generate them (see SKILL.md F1/F5).")

    total_calls = len(prompts) * len(engines) * a.samples
    print(f"[ai] prompts={len(prompts)} engines={list(engines)} samples={a.samples} "
          f"-> {total_calls} calls")
    print("     note: provider-native search may bill a per-search fee on top of tokens.")
    if a.dry_run:
        for r in prompts[:5]:
            print(f"     e.g. [{r['category']}] {r['prompt']}")
        return

    import os
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        sys.exit("OPENROUTER_API_KEY not set. See references/setup.md")

    aliases = [cfg.get("name", "")] + (cfg.get("brand_aliases") or [])
    own_domain = p["domain"]
    run_id = db.start_run(conn, p["id"], "ai")
    done, errors = 0, 0
    for row in prompts:
        for engine, model in engines.items():
            for s in range(a.samples):
                try:
                    res = ask(model, row["prompt"], api_key, p["locale"])
                    mentioned, cited, others = judge(
                        res["content"], res["citation_urls"], aliases, own_domain)
                    conn.execute(
                        """INSERT INTO ai_checks(prompt_id, run_id, engine, sample_idx,
                             mentioned, cited, cited_domains_json, answer_excerpt)
                           VALUES(?,?,?,?,?,?,?,?)""",
                        (row["id"], run_id, engine, s, mentioned, cited,
                         json.dumps(others, ensure_ascii=False),
                         res["content"][:280]))
                    conn.commit()
                    done += 1
                except Exception as e:
                    errors += 1
                    print(f"  ! {engine} failed on prompt#{row['id']}: {e}", file=sys.stderr)
                time.sleep(a.throttle)
        print(f"  prompt#{row['id']} [{row['category']}] done")
    db.finish_run(conn, run_id, api_calls=done,
                  notes=f"engines={list(engines)} samples={a.samples} errors={errors}")

    # summary matrix: engine x category -> cited/total
    print("\nvisibility matrix (cited / checks):")
    for r in conn.execute(
        """SELECT c.engine, p2.category,
                  SUM(c.cited) AS cited, COUNT(*) AS total
             FROM ai_checks c JOIN ai_prompts p2 ON p2.id=c.prompt_id
            WHERE c.run_id=? GROUP BY 1,2 ORDER BY 1,2""", (run_id,)):
        print(f"  {r['engine']:<11} {r['category']:<10} {r['cited']}/{r['total']}")
    print(f"\nrun_id={run_id} saved. Next: /capture gaps or /capture report")
    conn.close()


if __name__ == "__main__":
    main()
