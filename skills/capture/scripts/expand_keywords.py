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

import requests

sys.path.insert(0, str(Path(__file__).parent))
import collector  # noqa: E402
import db  # noqa: E402
import serp_adapter  # noqa: E402

SUGGEST_URL = "https://suggestqueries.google.com/complete/search"
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"}

_LATIN = list("abcdefghijklmnopqrstuvwxyz")
MODIFIERS = {
    "ko": list("ㄱㄴㄷㄹㅁㅂㅅㅇㅈㅊㅋㅌㅍㅎ") + _LATIN,
    "en": _LATIN,
    "ja": list("あいうえおかきくけこさしすせそたちつてとなにぬねのはひふへほまみむめもやゆよらりるれろわ") + _LATIN,
    "zh": _LATIN,                                   # 병음 — 구글 자동완성이 한자로 돌려준다
    "ru": list("абвгдежзиклмнопрстуфхцчшэюя"),
    "ar": list("ابتثجحخدذرزسشصضطظعغفقكلمنهوي"),
    "th": list("กขคงจฉชซดตถทนบปผพฟมยรลวสหอ"),
    "hi": list("अआइईउएओकखगघचछजझटठडढतथदधनपफबभमयरलवशसह"),
    # 라틴 문자권 — 특수 문자(ä·ç·ñ…)는 자동완성이 a~z 만으로도 돌려준다
    **{k: _LATIN for k in ("de", "fr", "es", "it", "pt", "nl", "pl", "tr", "vi", "id")},
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
    """글자로 언어를 가른다 — 규칙의 정본은 db.keyword_locale 하나다(여기 사본을 두지
    않는다). 적재(db.add_keyword_candidates)가 같은 판정을 한 번 더 거치므로 여기
    값은 시드 로케일이 빈 후보의 기본값일 뿐이다."""
    return db.keyword_locale(text, default)


def suggest(query: str, hl: str, gl: str) -> list[str]:
    params = {"client": "firefox", "hl": hl, "gl": gl, "q": query}
    r = requests.get(SUGGEST_URL, params=params, headers=UA, timeout=serp_adapter.TIMEOUTS["suggest"])
    r.raise_for_status()
    data = r.json()
    return [s for s in data[1] if isinstance(s, str)]


def _stderr_fail(message: str, *, item: str = "", **_kw) -> None:
    """fail 이 없을 때(모듈을 단독으로 부를 때)의 자리. 모양은 Stage.fail 과 같게."""
    print(f"  ! {item}: {message}" if item else f"  ! {message}", file=sys.stderr)


def autocomplete_expand(seeds: list[tuple[str, str | None]], locale: str, hl: str, gl: str,
                        throttle: float, per_seed_cap: int,
                        dry_run: bool, fail=_stderr_fail) -> list[tuple[str, str, str]]:
    """fail 은 Stage.fail — 실패를 여기 지역 카운터에만 세면 단계는 그것을 모른다.

    Suggest 가 IP 차단으로 전부 막혀도 이 함수는 stderr 에 경고만 찍고 빈 목록을
    돌려줬고, collect 는 st.done() 으로 초록불을 냈다.
    """
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
        # 시드가 다른 시장(다국어 사이트의 영어 시드)이면 그 시장 구글의 자동완성에 묻는다
        s_hl, s_gl = hl, gl
        if kw_locale != locale:
            s_hl, _, s_gl = kw_locale.partition("-")
            s_gl = (s_gl or serp_adapter.location(kw_locale)[2][0]).lower()
        s_mods = mods if s_hl == hl else modifiers(kw_locale, s_hl)
        found: set[str] = set()
        queries = [seed] + [f"{seed} {m}" for m in s_mods]
        for q in queries:
            if len(found) >= per_seed_cap:
                break
            reqs += 1
            try:
                for s in suggest(q, s_hl, s_gl):
                    s = s.strip()
                    if s and s.lower() != seed.lower():
                        found.add(s)
            except Exception as e:  # endpoint is unofficial: degrade gracefully
                fails += 1
                fail(str(e), item=f"suggest {q!r}", kind=type(e).__name__)
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


# 경쟁사 이름 뒤에 붙여 묻는 말 — 언어마다 (대안, 맞대결). '{경쟁사} 대안'·'{경쟁사} vs'
# 를 자동완성에 묻고, **구글이 돌려준 것만** 후보로 넣는다(돌려줬다는 것이 수요의 증거다 —
# 우리가 지은 '{경쟁사} vs {우리}' 를 그대로 넣지 않는다). 표에 없는 언어는 묻지 않는다:
# 영어 낱말을 다른 언어 구글에 물으면 그 언어의 수요가 아니다.
RIVAL_WORDS = {
    "en": ("alternatives", "vs"), "ko": ("대안", "vs"), "ja": ("代替", "比較"),
    "zh": ("替代", "对比"), "de": ("alternative", "vs"), "fr": ("alternative", "vs"),
    "es": ("alternativas", "vs"), "pt": ("alternativas", "vs"), "it": ("alternative", "vs"),
    "nl": ("alternatieven", "vs"), "pl": ("alternatywy", "vs"), "tr": ("alternatifleri", "vs"),
    "ru": ("аналоги", "vs"), "id": ("alternatif", "vs"), "vi": ("thay thế", "vs"),
}
RIVAL_MAX = 5           # 묻는 경쟁사 수 — 사람이 고른 것이 먼저다(scoring.rivals 의 순서)
RIVAL_PER = 8           # 경쟁사 하나에서 받는 후보 상한


def rival_seeds(domains, hl: str) -> list[tuple[str, str]]:
    """경쟁사 도메인들 → [(브랜드 이름, 물을 말)]. 이름은 scoring._stem(등록 도메인의 이름 칸)."""
    import scoring
    words = RIVAL_WORDS.get(hl)
    if not words:
        return []
    out, seen = [], set()
    for d in domains:
        name = scoring._stem(d)
        if len(name) < 3 or name in seen:      # 두 글자 줄기는 아무 낱말에나 걸린다
            continue
        seen.add(name)
        out += [(name, f"{name} {w}") for w in words]
        if len(seen) >= RIVAL_MAX:
            break
    return out


def rival_expand(conn, project, locale: str, hl: str, gl: str, throttle: float,
                 dry_run: bool, fail=_stderr_fail) -> list[tuple[str, str, str]]:
    """경쟁사 이름으로 '대안'·'vs' 검색어 후보를 캔다 — 구글 자동완성이 돌려준 것만.

    돌려준 것 중에서도 그 경쟁사 이름이 들어 있는 것만 남긴다(자동완성은 가끔 엉뚱한
    것으로 샌다). source 는 'rival' — 남의 브랜드가 든 검색어라는 표식이다."""
    import scoring
    use, _dropped = scoring.rivals(conn, project["id"], project["domain"])
    seeds = rival_seeds(use, hl)
    print(f"[rival] 경쟁사 {len({n for n, _ in seeds})}곳 × 대안·vs → 요청 {len(seeds)}회")
    if dry_run or not seeds:
        return []
    out: list[tuple[str, str, str]] = []
    count: dict[str, int] = {}
    for name, q in seeds:
        try:
            got = suggest(q, hl, gl)
        except Exception as e:      # 비공식 엔드포인트 — 실패는 세고 넘어간다
            fail(str(e), item=f"suggest {q!r}", kind=type(e).__name__)
            time.sleep(throttle * 4)
            continue
        for s in got:
            s = s.strip()
            if (name in s.lower() and s.lower() != q.lower() and s.lower() != name
                    and count.get(name, 0) < RIVAL_PER):
                count[name] = count.get(name, 0) + 1
                out.append((s, locale, "rival"))
        time.sleep(throttle)
    return list(dict.fromkeys(out))


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


def collect(project: str, *,
            dry_run: bool = False,
            mode: str = "all",
            per_seed_cap: int = 60,
            throttle: float | None = None,
            conn=None) -> collector.StageResult:
    """자동완성 + GSC 실측으로 키워드 후보를 적재한다.

    Args:
        project: 사이트 이름
        dry_run: True 면 호출 계획만 찍고 종료
        mode: "all" | "autocomplete" | "gsc"
        per_seed_cap: 시드별 자동완성 후보 상한
        throttle: 요청 간격(초)
        conn: 이미 열린 Brain 연결 — 주면 그것을 쓰고 닫지 않는다

    Returns:
        StageResult(ok=...). 사유 있는 비종료는 ok=False, skipped=True.
    """
    ap = _parser()
    with collector.stage(project, conn=conn, dry_run=dry_run) as st:
        conn, p, cfg = st.conn, st.project, st.cfg
        st.settings(ap, argparse.Namespace(throttle=throttle))
        throttle = st.throttle
        locale = db.project_locale(p)
        hl, _, gl = locale.partition("-")
        gl = (gl or serp_adapter.location(locale)[2][0]).lower()   # 나라 없는 로케일은 그 언어의 대표 나라

        # 시드의 locale까지 같이 읽는다 — 후보가 물려받을 값이 여기 있다.
        seeds = [(r["keyword"], r["locale"]) for r in conn.execute(
            "SELECT keyword, locale FROM keywords WHERE project_id=? AND source='seed'", (p["id"],))]
        seeds = seeds or [(kw, None) for kw in (cfg.get("seed_keywords") or [])]
        # gsc 모드는 시드가 필요 없다 — 자동완성을 쓸 때만 요구한다.
        if not seeds and mode in ("all", "autocomplete"):
            if mode == "autocomplete":
                return st.skip(
                    "시드 키워드가 없습니다. 프로젝트 yaml의 seed_keywords에 3~10개를 넣고 "
                    "sync-project 하거나, GSC 데이터만으로 시작하려면 --mode gsc 를 쓰세요.")
            print("[안내] 시드 키워드가 없어 자동완성은 건너뜁니다 — GSC 실측만 캡니다.")
            mode = "gsc"

        if st.dry_run:
            # 계획만 보여주고 아무것도 쓰지 않는다 (run 기록도 남기지 않음).
            if mode in ("all", "autocomplete"):
                autocomplete_expand(seeds, locale, hl, gl, throttle, per_seed_cap, True)
                rival_expand(conn, p, locale, hl, gl, throttle, True)
            if mode in ("all", "gsc"):
                print(f"[gsc] {len(gsc_mine(conn, p['id'], locale))}개가 후보로 들어올 예정")
            print("(--dry-run: 저장하지 않음)")
            return st.noop(rows=0)

        with st.record("keywords") as r:
            cands: list[tuple[str, str, str]] = []
            if mode in ("all", "autocomplete"):
                cands += autocomplete_expand(seeds, locale, hl, gl, throttle, per_seed_cap,
                                             False, fail=st.fail)
                cands += rival_expand(conn, p, locale, hl, gl, throttle, False, fail=st.fail)
            if mode in ("all", "gsc"):
                cands += gsc_mine(conn, p["id"], locale)
            inserted = db.add_keyword_candidates(conn, p["id"], cands)
            r.notes = (f"mode={mode} candidates={len(cands)} inserted={inserted} "
                       f"{st.err_note}")
        print(f"done: {inserted} new candidates (is_active=0). "
              f"Next: Claude curates & activates within limits.max_keywords.")
        # 후보를 한 개도 못 캤는데 Suggest 가 죽어 있었다면 그건 완료가 아니다
        # (IP 차단이 초록불로 나가던 자리).
        return st.verdict(len(cands), rows=inserted)


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser()
    collector.add_common(ap)
    collector.add_setting(ap, "--throttle", key="throttle", fallback=0.5, type=float,
                          help="요청 간격(초). 기본은 skill_config defaults.throttle")
    ap.add_argument("--mode", default="all", choices=["all", "autocomplete", "gsc"])
    ap.add_argument("--per-seed-cap", type=int, default=60)
    return ap


def main() -> None:
    collector.cli("keywords")


if __name__ == "__main__":
    main()
