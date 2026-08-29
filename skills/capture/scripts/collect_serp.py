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
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import collector  # noqa: E402
import db  # noqa: E402
import scoring  # noqa: E402
import serp_adapter  # noqa: E402


def collect(project: str, *,
            dry_run: bool = False,
            provider: str | None = None,
            max_keywords: int | None = None,
            serp_depth: int | None = None,
            throttle: float | None = None,
            device: str | None = None,
            no_harvest: bool = False,
            ids: str | None = None,
            force: bool = False,
            conn=None) -> collector.StageResult:
    """SERP 순위 스냅샷 + 부산물(키워드 후보·경쟁사)을 Brain 에 적재한다.

    Args:
        project: 사이트 이름
        dry_run: True 면 호출 계획만 찍고 종료
        provider: dataforseo|serper — 미지정시 환경에서 자동 판정
        max_keywords: is_active 키워드 상한 (limits.max_keywords)
        serp_depth: top-N 깊이
        throttle: 요청 간격(초)
        device: desktop|mobile
        no_harvest: True 면 부산물(키워드 후보·경쟁사) 적재 안 함
        ids: 쉼표 구분 keyword id — 지정하면 is_active 무시하고 이것만
        force: 오늘 이미 확인한 키워드도 재확인
        conn: 이미 열린 Brain 연결 — 주면 그것을 쓰고 닫지 않는다

    Returns:
        StageResult(ok=...). 사유 있는 비종료는 ok=False, skipped=True.
    """
    ap = _parser()
    with collector.stage(project, conn=conn, dry_run=dry_run) as st:
        conn, p = st.conn, st.project
        s = st.settings(ap, argparse.Namespace(
            max_keywords=max_keywords, depth=serp_depth,
            throttle=throttle, device=device))
        provider = provider or serp_adapter.detect_provider()
        if not provider:
            return st.skip("SERP 키 없음 — DATAFORSEO_LOGIN/PASSWORD 또는 SERPER_API_KEY 설정. "
                           "발급: https://dataforseo.com (권장) 또는 "
                           "https://serper.dev")
        depth = s["serp_depth"]
        throttle = st.throttle
        limit = s["max_keywords"]
        device = (s["device"] or "desktop").strip().lower()

        if device not in ("desktop", "mobile"):
            return st.skip(f"유효하지 않은 device '{device}' — desktop 또는 mobile만 허용됩니다")

        if device != "desktop":
            print("[경고] 모바일 측정 시 직전 스냅샷(desktop)과의 순위 비교(Δ)가 한 번 왜곡될 수 있습니다.",
                  file=sys.stderr)

        if ids:
            # 부분 실행. 이게 없으면 "이 몇 개만 다시" 하려고 is_active를 직접 토글하게
            # 되는데, 되돌릴 때 통째로 UPDATE 해버려 큐레이션한 활성 집합이 날아간다.
            ids_list = [int(x) for x in ids.split(",") if x.strip()]
            target_kws = conn.execute(
                f"""SELECT id, keyword, locale FROM keywords
                     WHERE project_id=? AND id IN ({','.join('?' * len(ids_list))}) ORDER BY id""",
                (p["id"], *ids_list)).fetchall()
        else:
            target_kws = conn.execute(
                """SELECT id, keyword, locale FROM keywords
                    WHERE project_id=? AND is_active=1 ORDER BY id LIMIT ?""",
                (p["id"], limit)).fetchall()
        if not target_kws:
            return st.skip("활성 키워드 없음 — /capture keywords 로 유니버스부터 구축")

        # 오늘 이미 확인한 키워드는 재실행 비용 절감을 위해 건너뛴다 (--force로 무시 가능).
        # '오늘'을 만드는 자리와 --force 의 뜻은 러너가 갖는다 (collector.Stage.seen_today).
        checked_today = st.seen_today(
            "SELECT keyword_id FROM rank_snapshots "
            f"WHERE {collector.today_clause('checked_at')}", force=force)
        kws = [k for k in target_kws if k["id"] not in checked_today]
        skipped = len(target_kws) - len(kws)

        # 키워드별 로케일. 어떤 로케일로 조회하는지 보여주지 않으면, 한국어 키워드를
        # en-US로 긁어 전부 "순위 없음"으로 적재해도 아무도 눈치채지 못한다.
        default_locale = p["locale"] or "ko-KR"
        locales = Counter((k["locale"] or default_locale) for k in (kws if kws else target_kws))
        est = serp_adapter.cost_per_query(provider) * len(kws)
        print(f"[serp] provider={provider} keywords={len(kws)}{st.skip_note(skipped)} "
              f"depth={depth} device={device} "
              f"est_cost≈${est:.2f} (~{len(kws) * (throttle + 2) / 60:.0f} min)")
        if locales:
            print("       locales: " + ", ".join(f"{loc}×{n}" for loc, n in locales.most_common()))
            for loc in locales:   # 매핑 없는 로케일 경고는 여기서 떠야 한다 — 돈을 쓰기 전에
                serp_adapter.warn_unmapped(loc)
        for note in serp_adapter.caveats(provider):
            print(f"       note: {note}")
        if st.dry_run:
            return st.noop(cost=est)

        if not kws:
            print(f"\nsaved 0 snapshots{st.skip_note(skipped)} "
                  f"actual_cost=$0.000 | 부산물: 키워드 후보 +0, 경쟁사 +0")
            return st.noop(rows=0, cost=0.0)

        own = p["domain"]
        total_cost = 0.0
        domain_hits: Counter = Counter()
        harvested_kw = set()
        new_kw = new_comp = 0

        def one(row) -> None:
            """키워드 하나 — 가져와서 쓴다. 실패는 러너가 세고 다음으로 넘어간다."""
            nonlocal total_cost
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
            if not no_harvest:
                # 후보는 조회에 쓴 로케일을 물려받는다. 안 그러면 한국어 SERP에서
                # 캔 후보가 locale NULL로 들어가 다음 런에서 프로젝트 로케일로
                # 조회되고, 방금 고친 버그가 후보 전체에 그대로 재현된다.
                for kw in (res["related"] + res["paa"]):
                    harvested_kw.add((kw.strip(), kw_locale))
            pos = position if position is not None else "-"
            aio = " AIO" + ("✓" if aio_cited else "") if res["aio_present"] else ""
            print(f"  {pos!s:>3}  {row['keyword']}{aio}")

        with st.record("rank") as r:
            done = st.each(kws, one, label=lambda row: row["keyword"])

            if not no_harvest:
                new_kw = db.add_keyword_candidates(
                    conn, p["id"], [(kw, loc, "serp") for kw, loc in harvested_kw])
                new_comp = db.add_competitors(
                    conn, p["id"], [d for d, n in domain_hits.items() if n >= 3], "auto_serp")

            r.api_calls, r.cost = done, total_cost
            r.notes = (f"provider={provider} device={device} errors={st.errors} skipped={skipped} "
                       f"harvest_kw={new_kw} harvest_comp={new_comp}")

        print(f"\nsaved {done} snapshots{st.skip_note(skipped)} (errors={st.errors}) "
              f"actual_cost=${total_cost:.3f} | 부산물: 키워드 후보 +{new_kw}, "
              f"경쟁사 +{new_comp}\nrun_id={r.id}")
        return st.done(rows=done, cost=total_cost)


def _parser() -> argparse.ArgumentParser:
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
    return ap


def main() -> None:
    a = _parser().parse_args()
    r = collect(a.project, dry_run=a.dry_run,
                provider=a.provider,
                max_keywords=a.max_keywords,
                serp_depth=a.depth,
                throttle=a.throttle,
                device=a.device,
                no_harvest=a.no_harvest,
                ids=a.ids,
                force=a.force)
    if not r.ok and r.reason:
        sys.exit(r.reason)


if __name__ == "__main__":
    main()
