#!/usr/bin/env python3
"""Free keyword discovery pipeline (no paid APIs).

Sources:
  autocomplete  Google suggest endpoint (unofficial; throttled; treat as fragile)
  gsc           queries with impressions in gsc_snapshots not yet in keywords

Candidates land with is_active=0. Claude curates (relevance filter, cluster,
intent label) and activates the tracked set within limits.max_keywords.

Usage:
  python expand_keywords.py --project NAME [--mode all|autocomplete|gsc]
                            [--throttle 0.5] [--per-seed-cap 60] [--dry-run]
"""
import argparse
import sys
import time
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import db  # noqa: E402

SUGGEST_URL = "https://suggestqueries.google.com/complete/search"
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"}

MODIFIERS = {
    "ko": list("ㄱㄴㄷㄹㅁㅂㅅㅇㅈㅊㅋㅌㅍㅎ") + list("abcdefghijklmnopqrstuvwxyz"),
    "en": list("abcdefghijklmnopqrstuvwxyz"),
}


def suggest(query: str, hl: str, gl: str) -> list[str]:
    import requests  # lazy
    params = {"client": "firefox", "hl": hl, "gl": gl, "q": query}
    r = requests.get(SUGGEST_URL, params=params, headers=UA, timeout=10)
    r.raise_for_status()
    data = r.json()
    return [s for s in data[1] if isinstance(s, str)]


def autocomplete_expand(seeds: list[str], hl: str, gl: str, throttle: float,
                        per_seed_cap: int, dry_run: bool) -> list[tuple[str, str]]:
    lang = "ko" if hl.startswith("ko") else "en"
    mods = MODIFIERS[lang]
    out: list[tuple[str, str]] = []
    planned = len(seeds) * (1 + len(mods))
    print(f"[autocomplete] seeds={len(seeds)} planned_requests≈{planned} "
          f"throttle={throttle}s (~{planned * throttle / 60:.1f} min)")
    if dry_run:
        return []
    for seed in seeds:
        found: set[str] = set()
        queries = [seed] + [f"{seed} {m}" for m in mods]
        for q in queries:
            if len(found) >= per_seed_cap:
                break
            try:
                for s in suggest(q, hl, gl):
                    s = s.strip()
                    if s and s.lower() != seed.lower():
                        found.add(s)
            except Exception as e:  # endpoint is unofficial: degrade gracefully
                print(f"  ! suggest failed for {q!r}: {e}", file=sys.stderr)
                time.sleep(throttle * 4)
            time.sleep(throttle)
        print(f"  {seed!r} -> {len(found)} suggestions")
        out += [(kw, "autocomplete") for kw in found]
    return out


def gsc_mine(conn, project_id: int) -> list[tuple[str, str]]:
    rows = conn.execute(
        """SELECT g.query, SUM(g.impressions) AS imp
             FROM gsc_snapshots g
            WHERE g.project_id = ?
              AND g.query NOT IN (SELECT keyword FROM keywords WHERE project_id = ?)
            GROUP BY g.query HAVING imp >= 3
            ORDER BY imp DESC LIMIT 500""",
        (project_id, project_id)).fetchall()
    print(f"[gsc] {len(rows)} new query candidates (real-user longtail)")
    return [(r["query"], "gsc") for r in rows]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    ap.add_argument("--mode", default="all", choices=["all", "autocomplete", "gsc"])
    ap.add_argument("--throttle", type=float, default=0.5)
    ap.add_argument("--per-seed-cap", type=int, default=60)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    conn = db.connect()
    p = db.get_project(conn, a.project)
    cfg = db.load_project_yaml(p["config_path"] or a.project)
    hl, _, gl = (p["locale"] or "ko-KR").partition("-")
    gl = (gl or "KR").lower()

    seeds = [r["keyword"] for r in conn.execute(
        "SELECT keyword FROM keywords WHERE project_id=? AND source='seed'", (p["id"],))]
    seeds = seeds or cfg.get("seed_keywords", [])
    # gsc 모드는 시드가 필요 없다 — 자동완성을 쓸 때만 요구한다.
    if not seeds and a.mode in ("all", "autocomplete"):
        if a.mode == "autocomplete":
            sys.exit("시드 키워드가 없습니다. 프로젝트 yaml의 seed_keywords에 3~10개를 넣고 "
                     "sync-project 하거나, GSC 데이터만으로 시작하려면 --mode gsc 를 쓰세요.")
        print("[안내] 시드 키워드가 없어 자동완성은 건너뜁니다 — GSC 실측만 캡니다.")
        a.mode = "gsc"

    if a.dry_run:
        # 계획만 보여주고 아무것도 쓰지 않는다 (run 기록도 남기지 않음).
        if a.mode in ("all", "autocomplete"):
            autocomplete_expand(seeds, hl, gl, a.throttle, a.per_seed_cap, True)
        if a.mode in ("all", "gsc"):
            print(f"[gsc] {len(gsc_mine(conn, p['id']))}개가 후보로 들어올 예정")
        print("(--dry-run: 저장하지 않음)")
        conn.close()
        return

    run_id = db.start_run(conn, p["id"], "keywords")
    cands: list[tuple[str, str]] = []
    if a.mode in ("all", "autocomplete"):
        cands += autocomplete_expand(seeds, hl, gl, a.throttle, a.per_seed_cap, False)
    if a.mode in ("all", "gsc"):
        cands += gsc_mine(conn, p["id"])

    inserted = 0
    for kw, src in cands:
        cur = conn.execute(
            """INSERT OR IGNORE INTO keywords(project_id, keyword, source, is_active)
               VALUES(?,?,?,0)""", (p["id"], kw, src))
        inserted += cur.rowcount
    conn.commit()
    db.finish_run(conn, run_id, api_calls=0, cost=0.0,
                  notes=f"mode={a.mode} candidates={len(cands)} inserted={inserted}")
    print(f"done: {inserted} new candidates (is_active=0). "
          f"Next: Claude curates & activates within limits.max_keywords.")
    conn.close()


if __name__ == "__main__":
    main()
