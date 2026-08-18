#!/usr/bin/env python3
"""Record browser-measured AI visibility checks into the Brain.

Companion to the browse skill: Claude drives the real consumer apps
(chatgpt.com / perplexity.ai / gemini.google.com / claude.ai) via claude-in-chrome
and records each answer here. Writes ai_checks rows through db.record_ai_check —
the same function collect_ai.py uses, judged by the same scoring.judge —
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
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "capture" / "scripts"))
import collector              # noqa: E402  (프로젝트 설정 읽기 — 수집기와 같은 경로로)
import db                     # noqa: E402
import scoring                # noqa: E402  (판정 규칙은 한 곳 — collect_ai 내부를 가로지르지 않는다)

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
        conn.close()
        return

    if a.cmd == "finish":
        if not a.run_id:
            sys.exit("--run-id is required for finish")
        db.finish_run(conn, a.run_id, api_calls=a.checks, notes=a.notes)
        print(f"run_id={a.run_id} finished. Next: /capture gaps or /capture report")
        conn.close()
        return

    for req in ("project", "run_id", "prompt_id", "engine", "answer_file"):
        if getattr(a, req) is None:
            sys.exit(f"--{req.replace('_', '-')} is required for record")
    if a.engine not in ENGINES:
        sys.exit(f"engine must be one of {sorted(ENGINES)} ('-web' = consumer surface)")

    p = db.get_project(conn, a.project)
    # yaml이 없어도 기록은 남아야 한다 — 별칭이 빠지면 경고만 하고 넘어간다.
    cfg = collector.project_cfg(p["config_path"] or a.project)
    content = Path(a.answer_file).read_text(encoding="utf-8")
    urls = [u.strip() for u in a.urls.split(",") if u.strip()]
    # 별칭 조립과 맨 URL fallback은 scoring 이 한다 — collect_ai 경로와 같은 입력 규칙.
    mentioned, cited, others = scoring.judge(content, urls,
                                             scoring.aliases_of(cfg), p["domain"])
    db.record_ai_check(conn, a.prompt_id, a.run_id, a.engine, a.sample,
                       mentioned, cited, others, content)
    print(json.dumps({"prompt_id": a.prompt_id, "engine": a.engine,
                      "mentioned": mentioned, "cited": cited,
                      "cited_domains": others}, ensure_ascii=False))
    conn.close()


if __name__ == "__main__":
    main()
