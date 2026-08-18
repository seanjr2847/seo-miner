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
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import collector  # noqa: E402
import db  # noqa: E402

SUGGEST_URL = "https://suggestqueries.google.com/complete/search"
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"}

MODIFIERS = {
    "ko": list("ㄱㄴㄷㄹㅁㅂㅅㅇㅈㅊㅋㅌㅍㅎ") + list("abcdefghijklmnopqrstuvwxyz"),
    "en": list("abcdefghijklmnopqrstuvwxyz"),
}
_WARNED: set[str] = set()


def modifiers(locale: str, hl: str) -> list[str]:
    """매핑에 없는 언어는 영문 알파벳으로 떨어진다 — 조용히 그러면 안 된다.
    독일어 프로젝트가 a~z만 붙여 캐면 그 언어의 롱테일은 아예 안 나오는데,
    후보 수만 보고는 알아챌 수 없다. 폴백은 하되 한 번은 말한다
    (serp_adapter.location 과 같은 규칙)."""
    if hl not in MODIFIERS and hl not in _WARNED:
        _WARNED.add(hl)
        print(f"[주의] '{locale}' 로케일용 자동완성 수식어가 없어 영문 a~z로 캡니다 — "
              f"이 언어의 롱테일은 빠집니다. expand_keywords.py의 MODIFIERS에 "
              f"'{hl}' 한 줄을 추가하세요.", file=sys.stderr)
    return MODIFIERS.get(hl, MODIFIERS["en"])


def locale_of(text: str, default: str) -> str:
    """한글이 든 키워드는 프로젝트 로케일이 무엇이든 ko-KR로 본다.
    db._migrate 가 기존 행에 쓴 것과 같은 규칙 — en-US 프로젝트의 한국어 후보가
    다음 런에서 미국 SERP로 조회돼 전부 '순위 없음'이 되던 것을 막는다."""
    return "ko-KR" if any("가" <= c <= "힣" for c in (text or "")) else default


def suggest(query: str, hl: str, gl: str) -> list[str]:
    import requests  # lazy
    params = {"client": "firefox", "hl": hl, "gl": gl, "q": query}
    r = requests.get(SUGGEST_URL, params=params, headers=UA, timeout=10)
    r.raise_for_status()
    data = r.json()
    return [s for s in data[1] if isinstance(s, str)]


def autocomplete_expand(seeds: list[tuple[str, str | None]], locale: str, hl: str, gl: str,
                        throttle: float, per_seed_cap: int,
                        dry_run: bool) -> list[tuple[str, str, str]]:
    mods = modifiers(locale, hl)
    out: list[tuple[str, str, str]] = []
    reqs = fails = 0
    planned = len(seeds) * (1 + len(mods))
    print(f"[autocomplete] seeds={len(seeds)} planned_requests≈{planned} "
          f"throttle={throttle}s (~{planned * throttle / 60:.1f} min)")
    if dry_run:
        return []
    for seed, seed_locale in seeds:
        # 후보는 시드의 로케일을 물려받는다 (collect_serp 의 부산물과 같은 규칙).
        # 한국어 시드에서 캔 후보가 locale NULL로 들어가면 다음 런에서 프로젝트
        # 로케일로 조회돼, 이중언어 프로젝트의 한쪽이 통째로 "순위 없음"이 된다.
        kw_locale = seed_locale or locale_of(seed, locale)
        found: set[str] = set()
        queries = [seed] + [f"{seed} {m}" for m in mods]
        for q in queries:
            if len(found) >= per_seed_cap:
                break
            reqs += 1
            try:
                for s in suggest(q, hl, gl):
                    s = s.strip()
                    if s and s.lower() != seed.lower():
                        found.add(s)
            except Exception as e:  # endpoint is unofficial: degrade gracefully
                fails += 1
                print(f"  ! suggest failed for {q!r}: {e}", file=sys.stderr)
                time.sleep(throttle * 4)
            time.sleep(throttle)
        print(f"  {seed!r} -> {len(found)} suggestions")
        out += [(kw, kw_locale, "autocomplete") for kw in found]
    # 실패를 조용히 삼키면 후보 수만 보고는 차단을 알아챌 수 없다 — 끝에서 말한다.
    if fails:
        print(f"[주의] Suggest {fails}/{reqs} 실패 — 차단 가능성, 후보 축소됨",
              file=sys.stderr)
        if fails > reqs * 0.5:
            print("!" * 62, file=sys.stderr)
            print(f"!! Suggest 실패율 {fails / reqs:.0%} — IP 차단으로 보입니다. "
                  f"이 런의 후보는 대부분 빠졌으니 결과를 신뢰하지 마세요.", file=sys.stderr)
            print("!" * 62, file=sys.stderr)
    return out


def gsc_mine(conn, project_id: int, locale: str) -> list[tuple[str, str, str]]:
    rows = conn.execute(
        """SELECT g.query, SUM(g.impressions) AS imp
             FROM gsc_snapshots g
            WHERE g.project_id = ?
              AND g.query NOT IN (SELECT keyword FROM keywords WHERE project_id = ?)
            GROUP BY g.query HAVING imp >= 3
            ORDER BY imp DESC LIMIT 500""",
        (project_id, project_id)).fetchall()
    print(f"[gsc] {len(rows)} new query candidates (real-user longtail)")
    # 실검색어에는 시드가 없으니 글자로 판별한다 — 한글 쿼리는 ko-KR.
    return [(r["query"], locale_of(r["query"], locale), "gsc") for r in rows]


def main() -> None:
    ap = argparse.ArgumentParser()
    collector.add_common(ap)
    collector.add_setting(ap, "--throttle", key="throttle", fallback=0.5, type=float,
                          help="요청 간격(초). 기본은 config.yaml defaults.throttle")
    ap.add_argument("--mode", default="all", choices=["all", "autocomplete", "gsc"])
    ap.add_argument("--per-seed-cap", type=int, default=60)
    a = ap.parse_args()

    conn, p, cfg = collector.open_project(a.project)
    s = collector.settings(a, cfg)
    throttle = s["throttle"]
    locale = p["locale"] or "ko-KR"
    hl, _, gl = locale.partition("-")
    gl = (gl or "KR").lower()

    # 시드의 locale까지 같이 읽는다 — 후보가 물려받을 값이 여기 있다.
    seeds = [(r["keyword"], r["locale"]) for r in conn.execute(
        "SELECT keyword, locale FROM keywords WHERE project_id=? AND source='seed'", (p["id"],))]
    seeds = seeds or [(kw, None) for kw in (cfg.get("seed_keywords") or [])]
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
            autocomplete_expand(seeds, locale, hl, gl, throttle, a.per_seed_cap, True)
        if a.mode in ("all", "gsc"):
            print(f"[gsc] {len(gsc_mine(conn, p['id'], locale))}개가 후보로 들어올 예정")
        print("(--dry-run: 저장하지 않음)")
        conn.close()
        return

    with db.run(conn, p["id"], "keywords") as r:
        cands: list[tuple[str, str, str]] = []
        if a.mode in ("all", "autocomplete"):
            cands += autocomplete_expand(seeds, locale, hl, gl, throttle, a.per_seed_cap, False)
        if a.mode in ("all", "gsc"):
            cands += gsc_mine(conn, p["id"], locale)
        inserted = db.add_keyword_candidates(conn, p["id"], cands)
        r.notes = f"mode={a.mode} candidates={len(cands)} inserted={inserted}"
    print(f"done: {inserted} new candidates (is_active=0). "
          f"Next: Claude curates & activates within limits.max_keywords.")
    conn.close()


if __name__ == "__main__":
    main()
