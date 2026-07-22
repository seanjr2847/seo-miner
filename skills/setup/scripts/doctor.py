#!/usr/bin/env python3
"""seo-miner doctor — environment diagnosis (stdlib only, runs before any pip).

Checks deps, CAPTURE_HOME/Brain, API keys, GSC files, then derives which
capabilities are unlocked and prints ordered next steps.

Usage: python doctor.py [--json]
Exit code: 0 = core usable, 1 = core setup incomplete.
"""
import importlib.util
import json
import os
import sqlite3
import sys
from pathlib import Path

CAPTURE_HOME = Path(os.environ.get("CAPTURE_HOME", Path.home() / ".capture"))
DB = Path(os.environ.get("CAPTURE_DB", CAPTURE_HOME / "brain.db"))


def has(mod: str) -> bool:
    return importlib.util.find_spec(mod) is not None


def diagnose() -> dict:
    deps_core = {m: has(m) for m in ("requests", "jinja2", "yaml")}
    deps_gsc = {m: has(m) for m in ("googleapiclient", "google_auth_oauthlib")}
    brain = {"home": str(CAPTURE_HOME), "home_exists": CAPTURE_HOME.exists(),
             "db_exists": DB.exists(), "tables": 0, "projects": []}
    if DB.exists():
        try:
            conn = sqlite3.connect(DB)
            brain["tables"] = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
            brain["projects"] = [r[0] for r in conn.execute(
                "SELECT name FROM projects")] if brain["tables"] else []
            conn.close()
        except Exception as e:
            brain["error"] = str(e)
    secrets = os.environ.get("GSC_CLIENT_SECRETS",
                             str(CAPTURE_HOME / "client_secrets.json"))
    keys = {
        "openrouter": bool(os.environ.get("OPENROUTER_API_KEY")),
        "dataforseo": bool(os.environ.get("DATAFORSEO_LOGIN")
                           and os.environ.get("DATAFORSEO_PASSWORD")),
        "serper": bool(os.environ.get("SERPER_API_KEY")),
        "gsc_client_secrets": Path(secrets).exists(),
        "gsc_token_cached": (CAPTURE_HOME / "gsc_token.json").exists(),
    }
    core_ok = all(deps_core.values())
    caps = {
        "keywords_free (자동완성·GSC마이닝)": core_ok,
        "brain (SQLite)": core_ok and brain["db_exists"],
        "ai_visibility (OpenRouter 3엔진)": core_ok and keys["openrouter"],
        "gsc (실측 순위·클릭)": core_ok and all(deps_gsc.values())
                                and keys["gsc_client_secrets"],
        "rank/SERP (선택)": core_ok and (keys["dataforseo"] or keys["serper"]),
        "create (실행 스킬)": core_ok,
    }
    steps = []
    if not core_ok:
        steps.append("pip install requests jinja2 pyyaml")
    if not brain["db_exists"]:
        steps.append("python skills/capture/scripts/db.py init  (Brain 초기화)")
    if not keys["openrouter"]:
        steps.append("OPENROUTER_API_KEY 설정 — AI 인용 체크용 "
                      "(capture/references/setup.md 5절)")
    if not all(deps_gsc.values()):
        steps.append("pip install google-api-python-client google-auth-oauthlib  (GSC용)")
    if not keys["gsc_client_secrets"]:
        steps.append(f"GSC OAuth client_secrets.json → {secrets} "
                      "(setup.md 4절, ~10분)")
    if not (keys["dataforseo"] or keys["serper"]):
        steps.append("[선택] SERP 키 — DATAFORSEO_LOGIN/PASSWORD 또는 "
                      "SERPER_API_KEY (setup.md 7절)")
    if brain["db_exists"] and not brain["projects"]:
        steps.append("첫 프로젝트 등록 — /capture add <이름>")
    return {"deps_core": deps_core, "deps_gsc": deps_gsc, "brain": brain,
            "keys": keys, "capabilities": caps, "next_steps": steps,
            "core_ok": core_ok}


def main() -> None:
    d = diagnose()
    if "--json" in sys.argv:
        print(json.dumps(d, ensure_ascii=False, indent=2))
    else:
        mark = lambda b: "✓" if b else "✗"  # noqa: E731
        print(f"seo-miner doctor · CAPTURE_HOME={d['brain']['home']}\n")
        print("capabilities:")
        for k, v in d["capabilities"].items():
            print(f"  {mark(v)} {k}")
        print("\nkeys:", ", ".join(f"{k}={mark(v)}" for k, v in d["keys"].items()))
        if d["brain"]["projects"]:
            print("projects:", ", ".join(d["brain"]["projects"]))
        if d["next_steps"]:
            print("\nnext steps (순서대로):")
            for i, s in enumerate(d["next_steps"], 1):
                print(f"  {i}. {s}")
        else:
            print("\n모든 준비 완료 — /capture run <프로젝트> 부터 시작하세요.")
    sys.exit(0 if d["core_ok"] and d["brain"]["db_exists"] else 1)


if __name__ == "__main__":
    try:
        main()
    except BrokenPipeError:  # head 등 파이프 잘림 방어
        sys.exit(0)
