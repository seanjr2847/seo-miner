#!/usr/bin/env python3
"""Rank snapshot collection via SERP adapter (F3).

Per active keyword: fetch SERP -> record own position, top-10, features,
AI-Overview flags. Free byproducts are harvested by default:
  * related searches + PAA  -> keyword candidates (source='serp', is_active=0)
  * domains in top-10 of >=3 keywords -> competitors (source='auto_serp')

Locale is resolved per keyword (keywords.locale, falling back to
projects.locale). A bilingual project used to send every keyword at the single
project locale, which silently stored "not ranking" for the other language.

Cost guardrails: --dry-run prints the plan; DataForSEO actual billed cost is
read from responses and recorded in runs.cost_estimate_usd.

Usage:
  python collect_serp.py --project NAME [--provider dataforseo|serper]
                         [--max-keywords N] [--ids 3,7,9] [--depth 10]
                         [--throttle 0.3] [--no-harvest] [--dry-run]
"""
import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import db  # noqa: E402
import serp_adapter  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    ap.add_argument("--provider", choices=["dataforseo", "serper"])
    ap.add_argument("--max-keywords", type=int, default=None)
    ap.add_argument("--depth", type=int, default=10)
    ap.add_argument("--throttle", type=float, default=0.3)
    ap.add_argument("--no-harvest", action="store_true")
    ap.add_argument("--ids", help="쉼표로 구분한 keyword id — 지정하면 is_active를 무시하고 이것만 조회")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    conn = db.connect()
    p = db.get_project(conn, a.project)
    cfg = db.load_project_yaml(p["config_path"] or a.project)
    provider = a.provider or serp_adapter.detect_provider()
    if not provider:
        sys.exit("SERP 키 없음 — DATAFORSEO_LOGIN/PASSWORD 또는 SERPER_API_KEY 설정 "
                 "(references/setup.md 참조)")

    limit = a.max_keywords or (cfg.get("limits", {}) or {}).get("max_keywords", 100)
    if a.ids:
        # 부분 실행. 이게 없으면 "이 몇 개만 다시" 하려고 is_active를 직접 토글하게
        # 되는데, 되돌릴 때 통째로 UPDATE 해버려 큐레이션한 활성 집합이 날아간다.
        ids = [int(x) for x in a.ids.split(",") if x.strip()]
        kws = conn.execute(
            f"""SELECT id, keyword, locale FROM keywords
                 WHERE project_id=? AND id IN ({','.join('?' * len(ids))}) ORDER BY id""",
            (p["id"], *ids)).fetchall()
    else:
        kws = conn.execute(
            """SELECT id, keyword, locale FROM keywords
                WHERE project_id=? AND is_active=1 ORDER BY id LIMIT ?""",
            (p["id"], limit)).fetchall()
    if not kws:
        sys.exit("활성 키워드 없음 — /capture keywords 로 유니버스부터 구축")

    # 키워드별 로케일. 어떤 로케일로 조회하는지 보여주지 않으면, 한국어 키워드를
    # en-US로 긁어 전부 "순위 없음"으로 적재해도 아무도 눈치채지 못한다.
    default_locale = p["locale"] or "ko-KR"
    locales = Counter((k["locale"] or default_locale) for k in kws)
    est = {"dataforseo": 0.003, "serper": 0.001}[provider] * len(kws)
    print(f"[serp] provider={provider} keywords={len(kws)} depth={a.depth} "
          f"est_cost≈${est:.2f} (~{len(kws) * (a.throttle + 2) / 60:.0f} min)")
    print("       locales: " + ", ".join(f"{loc}×{n}" for loc, n in locales.most_common()))
    if provider == "serper":
        print("       note: serper는 AI Overview를 측정하지 않는다 (aio_* = NULL, '없음'이 아님)")
    if a.dry_run:
        return

    run_id = db.start_run(conn, p["id"], "rank")
    total_cost, done, errors = 0.0, 0, 0
    domain_hits: Counter = Counter()
    harvested_kw = set()
    own = p["domain"].lower().removeprefix("www.")

    for row in kws:
        try:
            kw_locale = row["locale"] or default_locale
            res = serp_adapter.fetch(provider, row["keyword"], kw_locale,
                                     p["domain"], a.depth)
            conn.execute(
                """INSERT INTO rank_snapshots(keyword_id, checked_at, position, url,
                     serp_features_json, aio_present, aio_cited)
                   VALUES(?,?,?,?,?,?,?)""",
                (row["id"], db.now(), res["position"], res["url"],
                 json.dumps(res["serp_features"], ensure_ascii=False),
                 None if res["aio_present"] is None else int(res["aio_present"]),
                 None if res["aio_cited"] is None else int(res["aio_cited"])))
            total_cost += res.get("cost") or 0
            done += 1
            for t in res["top"]:
                d = t.get("domain") or ""
                if d and not (d == own or d.endswith("." + own)):
                    domain_hits[d] += 1
            if not a.no_harvest:
                # 후보는 조회에 쓴 로케일을 물려받는다. 안 그러면 한국어 SERP에서
                # 캔 후보가 locale NULL로 들어가 다음 런에서 프로젝트 로케일로
                # 조회되고, 방금 고친 버그가 후보 전체에 그대로 재현된다.
                for kw in (res["related"] + res["paa"]):
                    harvested_kw.add((kw.strip(), kw_locale))
            pos = res["position"] if res["position"] is not None else "-"
            aio = " AIO" + ("✓" if res["aio_cited"] else "") if res["aio_present"] else ""
            print(f"  {pos!s:>3}  {row['keyword']}{aio}")
        except Exception as e:
            errors += 1
            print(f"  !   {row['keyword']}: {e}", file=sys.stderr)
        conn.commit()
        time.sleep(a.throttle)

    new_kw = new_comp = 0
    if not a.no_harvest:
        for kw, loc in harvested_kw:
            cur = conn.execute(
                """INSERT OR IGNORE INTO keywords(project_id, keyword, locale,
                                                  source, is_active)
                   VALUES(?,?,?, 'serp', 0)""", (p["id"], kw, loc))
            new_kw += cur.rowcount
        for d, n in domain_hits.items():
            if n >= 3:
                cur = conn.execute(
                    """INSERT OR IGNORE INTO competitors(project_id, domain, source)
                       VALUES(?,?, 'auto_serp')""", (p["id"], d))
                new_comp += cur.rowcount
        conn.commit()

    db.finish_run(conn, run_id, api_calls=done, cost=total_cost,
                  notes=f"provider={provider} errors={errors} "
                        f"harvest_kw={new_kw} harvest_comp={new_comp}")
    print(f"\nsaved {done} snapshots (errors={errors}) "
          f"actual_cost=${total_cost:.3f} | 부산물: 키워드 후보 +{new_kw}, "
          f"경쟁사 +{new_comp}\nrun_id={run_id}")
    conn.close()


if __name__ == "__main__":
    main()
