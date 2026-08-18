#!/usr/bin/env python3
"""Rank snapshot collection via SERP adapter (F3).

Per active keyword: fetch SERP -> record own position, top-10, features,
AI-Overview flags. Free byproducts are harvested by default:
  * related searches + PAA  -> keyword candidates (source='serp', is_active=0)
  * domains in top-10 of >=3 keywords -> competitors (source='auto_serp')

"내 도메인인가"는 여기 한 곳에서만 판정한다(scoring.owns). 어댑터와 호출부가
같은 규칙을 각자 들고 있던 시절엔 서브도메인 취급이 조용히 갈렸다.

Locale is resolved per keyword (keywords.locale, falling back to
projects.locale). A bilingual project used to send every keyword at the single
project locale, which silently stored "not ranking" for the other language.

Cost guardrails: --dry-run prints the plan (단가는 어댑터가 답한다);
DataForSEO actual billed cost is read from responses and recorded in
runs.cost_estimate_usd.

Usage:
  python collect_serp.py --project NAME [--provider dataforseo|serper]
                         [--max-keywords N] [--ids 3,7,9] [--depth 10]
                         [--throttle 0.5] [--no-harvest] [--dry-run]
  --depth / --throttle 를 안 주면 config.yaml defaults(serp_depth·throttle)를 쓴다.
"""
import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import collector  # noqa: E402
import db  # noqa: E402
import scoring  # noqa: E402
import serp_adapter  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    collector.add_common(ap)
    ap.add_argument("--provider", choices=list(serp_adapter.PROVIDERS))
    collector.add_setting(ap, "--max-keywords", key="limits.max_keywords", fallback=100, type=int)
    collector.add_setting(ap, "--depth", key="serp_depth", fallback=10, type=int)
    collector.add_setting(ap, "--throttle", key="throttle", fallback=0.5, type=float,
                          help="요청 간격(초). 기본은 config.yaml defaults.throttle")
    collector.add_setting(ap, "--device", key="serp_device", fallback="desktop", type=str,
                          help="측정 디바이스 (desktop|mobile). 기본은 desktop")
    ap.add_argument("--no-harvest", action="store_true")
    ap.add_argument("--ids", help="쉼표로 구분한 keyword id — 지정하면 is_active를 무시하고 이것만 조회")
    ap.add_argument("--force", action="store_true", help="오늘 이미 확인한 키워드도 건너뛰지 않고 재확인")
    a = ap.parse_args()

    conn, p, cfg = collector.open_project(a.project)
    provider = a.provider or serp_adapter.detect_provider()
    if not provider:
        sys.exit("SERP 키 없음 — DATAFORSEO_LOGIN/PASSWORD 또는 SERPER_API_KEY 설정 "
                 "(references/setup.md 참조)")
    s = collector.settings(a, cfg)
    depth = s["serp_depth"]
    throttle = s["throttle"]
    limit = s["limits.max_keywords"]
    device = (s.get("serp_device") or s.get("device") or "desktop").strip().lower()

    if device not in ("desktop", "mobile"):
        sys.exit(f"유효하지 않은 device '{device}' — desktop 또는 mobile만 허용됩니다")

    if device != "desktop":
        print("[경고] 모바일 측정 시 직전 스냅샷(desktop)과의 순위 비교(Δ)가 한 번 왜곡될 수 있습니다.",
              file=sys.stderr)

    if a.ids:
        # 부분 실행. 이게 없으면 "이 몇 개만 다시" 하려고 is_active를 직접 토글하게
        # 되는데, 되돌릴 때 통째로 UPDATE 해버려 큐레이션한 활성 집합이 날아간다.
        ids = [int(x) for x in a.ids.split(",") if x.strip()]
        target_kws = conn.execute(
            f"""SELECT id, keyword, locale FROM keywords
                 WHERE project_id=? AND id IN ({','.join('?' * len(ids))}) ORDER BY id""",
            (p["id"], *ids)).fetchall()
    else:
        target_kws = conn.execute(
            """SELECT id, keyword, locale FROM keywords
                WHERE project_id=? AND is_active=1 ORDER BY id LIMIT ?""",
            (p["id"], limit)).fetchall()
    if not target_kws:
        sys.exit("활성 키워드 없음 — /capture keywords 로 유니버스부터 구축")

    from datetime import datetime
    today = datetime.now().strftime("%Y-%m-%d")
    if not a.force:
        # 오늘 이미 확인한 키워드는 재실행 비용 절감을 위해 건너뛴다 (--force로 무시 가능)
        checked_today = {
            r[0] for r in conn.execute(
                """SELECT keyword_id FROM rank_snapshots
                    WHERE date(checked_at) = ? OR date(checked_at, 'localtime') = ?""",
                (today, today)
            ).fetchall()
        }
        kws = [k for k in target_kws if k["id"] not in checked_today]
        skipped = len(target_kws) - len(kws)
    else:
        kws = list(target_kws)
        skipped = 0

    # 키워드별 로케일. 어떤 로케일로 조회하는지 보여주지 않으면, 한국어 키워드를
    # en-US로 긁어 전부 "순위 없음"으로 적재해도 아무도 눈치채지 못한다.
    default_locale = p["locale"] or "ko-KR"
    locales = Counter((k["locale"] or default_locale) for k in (kws if kws else target_kws))
    est = serp_adapter.cost_per_query(provider) * len(kws)
    skip_str = f" (skipped {skipped} 오늘 이미 확인)" if skipped else ""
    print(f"[serp] provider={provider} keywords={len(kws)}{skip_str} depth={depth} device={device} "
          f"est_cost≈${est:.2f} (~{len(kws) * (throttle + 2) / 60:.0f} min)")
    if locales:
        print("       locales: " + ", ".join(f"{loc}×{n}" for loc, n in locales.most_common()))
        for loc in locales:   # 매핑 없는 로케일 경고는 여기서 떠야 한다 — 돈을 쓰기 전에
            serp_adapter.warn_unmapped(loc)
    for note in serp_adapter.caveats(provider):
        print(f"       note: {note}")
    if a.dry_run:
        conn.close()
        return

    if not kws:
        print(f"\nsaved 0 snapshots (skipped {skipped} 오늘 이미 확인) "
              f"actual_cost=$0.000 | 부산물: 키워드 후보 +0, 경쟁사 +0")
        conn.close()
        return

    own = p["domain"]
    total_cost, done, errors = 0.0, 0, 0
    domain_hits: Counter = Counter()
    harvested_kw = set()
    new_kw = new_comp = 0

    with db.run(conn, p["id"], "rank") as r:
        for row in kws:
            try:
                kw_locale = row["locale"] or default_locale
                res = serp_adapter.fetch(provider, row["keyword"], kw_locale, depth, device=device)
                # 내 순위와 경쟁사 집계는 같은 한 바퀴에서 같은 규칙으로 갈린다.
                position = url = None
                for t in res["top"]:
                    d = t.get("domain") or ""
                    if not d:
                        continue
                    if scoring.owns(d, own):
                        if position is None:
                            position, url = t.get("pos"), t.get("url")
                    else:
                        domain_hits[d] += 1
                aio_cited = (int(any(scoring.owns(d, own) for d in res["aio_domains"]))
                             if res["aio_present"] else None)
                db.write_rank_snapshot(
                    conn, row["id"], position, url,
                    res["serp_features"], res["aio_present"], aio_cited)
                total_cost += res["cost"]
                done += 1
                if not a.no_harvest:
                    # 후보는 조회에 쓴 로케일을 물려받는다. 안 그러면 한국어 SERP에서
                    # 캔 후보가 locale NULL로 들어가 다음 런에서 프로젝트 로케일로
                    # 조회되고, 방금 고친 버그가 후보 전체에 그대로 재현된다.
                    for kw in (res["related"] + res["paa"]):
                        harvested_kw.add((kw.strip(), kw_locale))
                pos = position if position is not None else "-"
                aio = " AIO" + ("✓" if aio_cited else "") if res["aio_present"] else ""
                print(f"  {pos!s:>3}  {row['keyword']}{aio}")
            except Exception as e:
                errors += 1
                print(f"  !   {row['keyword']}: {e}", file=sys.stderr)
            conn.commit()
            time.sleep(throttle)

        if not a.no_harvest:
            new_kw = db.add_keyword_candidates(
                conn, p["id"], [(kw, loc, "serp") for kw, loc in harvested_kw])
            new_comp = db.add_competitors(
                conn, p["id"], [d for d, n in domain_hits.items() if n >= 3], "auto_serp")

        r.api_calls, r.cost = done, total_cost
        r.notes = (f"provider={provider} device={device} errors={errors} skipped={skipped} "
                   f"harvest_kw={new_kw} harvest_comp={new_comp}")

    skip_summary = f" (skipped {skipped} 오늘 이미 확인)" if skipped else ""
    print(f"\nsaved {done} snapshots{skip_summary} (errors={errors}) "
          f"actual_cost=${total_cost:.3f} | 부산물: 키워드 후보 +{new_kw}, "
          f"경쟁사 +{new_comp}\nrun_id={r.id}")
    conn.close()


if __name__ == "__main__":
    main()
