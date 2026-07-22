#!/usr/bin/env python3
"""Record browser-measured AI visibility checks into the Brain.

Companion to the browse skill: Claude drives the real consumer apps
(chatgpt.com / perplexity.ai / gemini.google.com) via claude-in-chrome and
records each answer here. Writes the same ai_checks rows as collect_ai.py,
but engine carries a '-web' suffix so API samples and consumer-surface
samples never mix silently (see capture/references/scoring.md).

Usage:
  python record_check.py start  --project NAME                    # prints run_id
  python record_check.py record --project NAME --run-id N --prompt-id N
        --engine chatgpt-web --answer-file ans.txt [--urls "u1,u2"] [--sample 0]
  python record_check.py finish --run-id N [--checks N] [--notes "..."]
"""
import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "capture" / "scripts"))
import db                     # noqa: E402
from collect_ai import judge  # noqa: E402  (stdlib-only at import time)

ENGINES = {"chatgpt-web", "perplexity-web", "gemini-web", "claude-web"}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["start", "record", "finish"])
    ap.add_argument("--project")
    ap.add_argument("--run-id", type=int)
    ap.add_argument("--prompt-id", type=int)
    ap.add_argument("--engine")
    ap.add_argument("--answer-file")
    ap.add_argument("--urls", default="")
    ap.add_argument("--sample", type=int, default=0)
    ap.add_argument("--checks", type=int, default=0)
    ap.add_argument("--notes", default="browser (claude-in-chrome)")
    a = ap.parse_args()
    conn = db.connect()

    if a.cmd == "start":
        if not a.project:
            sys.exit("--project is required for start")
        p = db.get_project(conn, a.project)
        print(db.start_run(conn, p["id"], "ai"))
        return

    if a.cmd == "finish":
        if not a.run_id:
            sys.exit("--run-id is required for finish")
        db.finish_run(conn, a.run_id, api_calls=a.checks, notes=a.notes)
        print(f"run_id={a.run_id} finished. Next: /capture gaps or /capture report")
        return

    for req in ("project", "run_id", "prompt_id", "engine", "answer_file"):
        if getattr(a, req) is None:
            sys.exit(f"--{req.replace('_', '-')} is required for record")
    if a.engine not in ENGINES:
        sys.exit(f"engine must be one of {sorted(ENGINES)} ('-web' = consumer surface)")

    p = db.get_project(conn, a.project)
    cfg = db.load_project_yaml(p["config_path"] or a.project)
    aliases = [cfg.get("name", "")] + (cfg.get("brand_aliases") or [])
    content = Path(a.answer_file).read_text(encoding="utf-8")
    urls = [u.strip() for u in a.urls.split(",") if u.strip()]
    if not urls:  # same fallback as collect_ai: bare URLs inlined in the answer
        urls = re.findall(r"https?://[^\s)\]>\"']+", content)
    mentioned, cited, others = judge(content, urls, aliases, p["domain"])
    conn.execute(
        """INSERT INTO ai_checks(prompt_id, run_id, engine, sample_idx,
             mentioned, cited, cited_domains_json, answer_excerpt)
           VALUES(?,?,?,?,?,?,?,?)""",
        (a.prompt_id, a.run_id, a.engine, a.sample, mentioned, cited,
         json.dumps(others, ensure_ascii=False), content[:280]))
    conn.commit()
    print(json.dumps({"prompt_id": a.prompt_id, "engine": a.engine,
                      "mentioned": mentioned, "cited": cited,
                      "cited_domains": others}, ensure_ascii=False))
    conn.close()


if __name__ == "__main__":
    main()
