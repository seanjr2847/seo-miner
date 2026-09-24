#!/usr/bin/env python3
"""속도 수집 — Core Web Vitals(LCP·INP·CLS)를 페이지·기기별로 잰다.

이 스킬은 "모바일 12위 · 데스크톱 4위" 를 말할 수 있으면서도, **왜 모바일에서만
밀리는지**의 재료를 하나도 안 갖고 있었다. 기기 격차 요청문의 근거는 순위 차이
한 줄뿐인데 규칙은 "위 근거에 없는 것은 짐작하지 않습니다" 라, 그 요청문은 구조상
빈손으로 끝났다. 여기서 그 구멍을 메운다.

두 가지를 같이 남긴다. 뜻이 다르기 때문이다:

- **현장(field)**: 실제 크롬 사용자에게서 모은 28일치(CrUX). 검색이 보는 값이
  그것이다. 트래픽이 적은 페이지에는 없고, 그때 구글은 사이트 전체(오리진) 값을
  대신 준다 — `origin_fallback` 으로 갈라 적는다. 뭉뚱그리면 "이 페이지가 느리다"
  라고 잘못 말하게 된다.
- **실험실(lab)**: 지금 한 번 열어 잰 값(Lighthouse). 고친 뒤 바로 확인할 수 있는
  유일한 숫자다 — 현장 값은 28일이 지나야 움직인다.

비용: 없다. PageSpeed Insights API 는 무료다. 키(PAGESPEED_API_KEY)는 없어도 돌고
넣으면 한도가 넉넉해진다 — 그래서 키가 없다고 단계를 건너뛰지 않는다.

Usage:
  python collect_vitals.py --project NAME [--limit N] [--strategy mobile,desktop]
  python collect_vitals.py                                  # self-check
"""
import argparse
import os
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import collect_page  # noqa: E402
import collector  # noqa: E402
import db  # noqa: E402
import serp_adapter  # noqa: E402

API = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
STRATEGIES = ("mobile", "desktop")

# PSI 응답에서 우리가 읽는 자리. 이름을 여기 한 벌로 두는 이유는 구글이 지표를
# 갈아 끼울 때(FID → INP 가 그랬다) 고칠 자리가 한 곳이어야 하기 때문이다.
_FIELD = {"field_lcp_ms": "LARGEST_CONTENTFUL_PAINT_MS",
          "field_inp_ms": "INTERACTION_TO_NEXT_PAINT",
          "field_ttfb_ms": "EXPERIMENTAL_TIME_TO_FIRST_BYTE"}
_LAB = {"lab_lcp_ms": "largest-contentful-paint",
        "lab_tbt_ms": "total-blocking-time"}


def parse(url: str, strategy: str, data: dict) -> dict:
    """PSI 응답 한 벌 → page_vitals 한 줄. 네트워크를 안 탄다(자체점검이 여기를 부른다).

    없는 값은 None 이다. 0 으로 채우면 "안 쟀다" 가 "완벽하다" 로 읽힌다.
    """
    row = {"url": url, "strategy": strategy, "error": None, "origin_fallback": None,
           "field_verdict": None, "lab_score": None, "field_cls": None, "lab_cls": None}
    row.update({k: None for k in _FIELD})
    row.update({k: None for k in _LAB})

    exp = data.get("loadingExperience") or {}
    metrics = exp.get("metrics") or {}
    if metrics:
        row["origin_fallback"] = int(bool(exp.get("origin_fallback")))
        row["field_verdict"] = exp.get("overall_category")
        for col, key in _FIELD.items():
            v = (metrics.get(key) or {}).get("percentile")
            row[col] = int(v) if isinstance(v, (int, float)) else None
        cls = (metrics.get("CUMULATIVE_LAYOUT_SHIFT_SCORE") or {}).get("percentile")
        # CLS 만 100 배로 온다 — 그대로 적으면 0.24 인 페이지가 24 로 남는다.
        row["field_cls"] = round(cls / 100, 3) if isinstance(cls, (int, float)) else None

    lh = data.get("lighthouseResult") or {}
    audits = lh.get("audits") or {}
    score = ((lh.get("categories") or {}).get("performance") or {}).get("score")
    row["lab_score"] = round(score * 100) if isinstance(score, (int, float)) else None
    for col, key in _LAB.items():
        v = (audits.get(key) or {}).get("numericValue")
        row[col] = round(v) if isinstance(v, (int, float)) else None
    v = (audits.get("cumulative-layout-shift") or {}).get("numericValue")
    row["lab_cls"] = round(v, 3) if isinstance(v, (int, float)) else None
    return row


def fetch(url: str, strategy: str, *, timeout: int | None = None) -> dict:
    """URL 하나·기기 하나. 실패도 한 줄로 남긴다 — collect_page.fetch 와 같은 규칙이다."""
    import requests
    params = {"url": url, "strategy": strategy, "category": "performance"}
    key = os.environ.get("PAGESPEED_API_KEY")
    if key:
        params["key"] = key
    try:
        r = requests.get(API, params=params,
                         timeout=timeout or serp_adapter.TIMEOUTS["psi"])
    except Exception as e:
        return {"url": url, "strategy": strategy, "error": f"{type(e).__name__}: {e}"[:200]}
    if r.status_code != 200:
        detail = ""
        try:
            detail = ((r.json().get("error") or {}).get("message") or "")[:120]
        except Exception:
            pass
        return {"url": url, "strategy": strategy,
                "error": f"HTTP {r.status_code}" + (f" · {detail}" if detail else "")}
    try:
        return parse(url, strategy, r.json())
    except ValueError as e:
        return {"url": url, "strategy": strategy, "error": f"응답을 못 읽었습니다: {e}"[:200]}


def collect(project: str, *,
            dry_run: bool = False,
            limit: int | None = None,
            strategy: str | None = None,
            throttle: float | None = None,
            conn=None) -> collector.StageResult:
    """내 페이지의 속도를 재서 Brain 에 적재한다. sys.exit 호출 없음.

    Args:
        project: 사이트 이름
        dry_run: True 면 잴 목록만 찍고 종료
        limit: 잴 URL 수(config 키는 vitals_urls). 0이면 끔. 한 URL 이 기기 수만큼
            호출된다 — 기본 5개 × 2기기 = 10회다.
        strategy: 쉼표로 구분한 기기(mobile,desktop). 기기 격차를 보려면 둘 다 필요하다.
        throttle: 요청 간격(초)
        conn: 이미 열린 Brain 연결 — 주면 그것을 쓰고 닫지 않는다

    Returns:
        StageResult(ok=...). 사유 있는 비종료는 ok=False, skipped=True.
    """
    ap = _parser()
    with collector.stage(project, conn=conn, dry_run=dry_run) as st:
        conn, p = st.conn, st.project
        s = st.settings(ap, argparse.Namespace(limit=limit, strategy=strategy,
                                               throttle=throttle))
        limit = s["vitals_urls"]
        if limit <= 0:
            print("[vitals] vitals_urls=0 — 속도 측정을 끄셨습니다.")
            return st.noop(rows=0)
        want = [x.strip() for x in str(s["strategy"] or "").split(",") if x.strip()]
        bad = [x for x in want if x not in STRATEGIES]
        if bad or not want:
            return st.skip(f"기기 이름이 틀렸습니다: {', '.join(bad) or '(비어 있음)'} — "
                           f"쓸 수 있는 값: {', '.join(STRATEGIES)}")

        # 잴 페이지의 정본은 페이지 감사와 같다 — 두 단계가 서로 다른 페이지를 보면
        # 요청문 안에서 "이 페이지" 가 두 곳을 가리킨다.
        urls = collect_page.target_urls(conn, p["id"], limit)
        if not urls:
            return st.skip("잴 페이지가 없습니다 — 먼저 gsc 를 수집하세요 "
                           "(page 분해가 있어야 어느 URL 인지 알 수 있습니다).")

        jobs = [(u, dev) for u in urls for dev in want]
        print(f"[vitals] URL {len(urls)}개 × {len(want)}기기 = {len(jobs)}회 · 비용 없음 "
              f"(PageSpeed Insights · 간격 {st.throttle}초)")
        if not os.environ.get("PAGESPEED_API_KEY"):
            print("       키 없이 돕니다. 자주 돌려 한도(429)에 걸리면 "
                  "PAGESPEED_API_KEY 를 넣으세요 — 발급은 무료입니다.")
        if st.dry_run:
            for i, (u, dev) in enumerate(jobs, 1):
                print(f"  {i:>3}. [{dev}] {u}")
            return st.noop(rows=0)

        rows: list[dict] = []

        def one(job) -> None:
            url, dev = job
            row = fetch(url, dev)
            # 행은 남긴다 — page_vitals.error 는 "못 쟀다"를 적는 진짜 칸이다.
            # 그리고 실패는 예외로 올린다: 여기서 return 하면 실패가 데이터로만 남아
            # 전부 못 재고도 runs.notes 에 errors=0 이 적힌다(collect_page 와 같은 규칙).
            rows.append(row)
            if row.get("error"):
                raise collector.ItemFailed(row["error"])
            lcp = row.get("field_lcp_ms") or row.get("lab_lcp_ms")
            score = row.get("lab_score")
            print(f"  ✓ [{dev}] {url} — 점수 {score if score is not None else '—'}"
                  + (f" · LCP {lcp}ms" if lcp else ""))

        with st.record("vitals") as r:
            done = st.each(jobs, one, label=lambda j: f"[{j[1]}] {j[0]}")
            checked = str(date.today())
            db.write_page_vitals(conn, p["id"], checked, rows)
            r.api_calls = done
            r.notes = (f"urls={len(urls)} strategies={','.join(want)} "
                       f"rows={len(rows)} checked={checked} {st.err_note}")

        bad_rows = [x for x in rows if x.get("error")]
        print(f"\nsaved {len(rows)} vitals rows (errors={st.errors})"
              + (f" · 못 잰 것 {len(bad_rows)}개" if bad_rows else ""))
        # 실제로 잰 건수로 판정한다 — 전부 못 잰 것은 완료가 아니다.
        return st.verdict(done, rows=len(rows))


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser()
    collector.add_common(ap)
    collector.add_setting(ap, "--limit", key="vitals_urls", fallback=5, type=int,
                          help="속도를 잴 URL 수. 0이면 끔 (한 URL 이 기기 수만큼 호출됩니다)")
    # type=str 을 꼭 준다 — add_setting 의 기본은 int 라, 빼면 'mobile,desktop' 을 int() 로
    # 바꾸려다 속도 단계가 통째로 죽었다(호스팅 noti 런).
    collector.add_setting(ap, "--strategy", key="strategy", fallback="mobile,desktop", type=str,
                          help="기기 — mobile,desktop. 기기 격차를 보려면 둘 다 필요합니다")
    collector.add_setting(ap, "--throttle", key="throttle", fallback=1.0, type=float,
                          help="요청 간격(초)")
    return ap


def main() -> None:
    if len(sys.argv) == 1:
        _selfcheck()
        return
    collector.cli("vitals")


def _selfcheck() -> None:
    import sqlite3

    import scoring
    sample = {
        "loadingExperience": {
            "overall_category": "SLOW", "origin_fallback": True,
            "metrics": {
                "LARGEST_CONTENTFUL_PAINT_MS": {"percentile": 4200},
                "INTERACTION_TO_NEXT_PAINT": {"percentile": 310},
                "CUMULATIVE_LAYOUT_SHIFT_SCORE": {"percentile": 24},
                "EXPERIMENTAL_TIME_TO_FIRST_BYTE": {"percentile": 900}}},
        "lighthouseResult": {
            "categories": {"performance": {"score": 0.42}},
            "audits": {"largest-contentful-paint": {"numericValue": 4310.5},
                       "cumulative-layout-shift": {"numericValue": 0.2412},
                       "total-blocking-time": {"numericValue": 640}}}}
    # 기기 설정은 문자열이다 — int() 로 바꾸려다 속도 단계가 통째로 죽었다(호스팅 noti 런)
    import argparse
    ap = _parser()
    s = collector.settings(argparse.Namespace(limit=None, strategy=None, throttle=None), {},
                           ap._collector_settings)
    assert s["strategy"] == "mobile,desktop" and s["vitals_urls"] == 5, s

    r = parse("https://x.kr/a", "mobile", sample)
    assert (r["field_lcp_ms"], r["field_inp_ms"], r["field_ttfb_ms"]) == (4200, 310, 900), r
    # CLS 만 100 배로 온다 — 그대로 적으면 0.24 인 페이지가 24 로 남는다
    assert r["field_cls"] == 0.24, r["field_cls"]
    assert r["origin_fallback"] == 1 and r["field_verdict"] == "SLOW", r
    assert (r["lab_score"], r["lab_lcp_ms"], r["lab_cls"], r["lab_tbt_ms"]) \
        == (42, 4310, 0.241, 640), r

    # 현장 값이 없는 페이지(트래픽이 적으면 흔하다) — 0 이 아니라 None 이어야 한다
    thin = parse("https://x.kr/b", "desktop", {"lighthouseResult": sample["lighthouseResult"]})
    assert thin["field_lcp_ms"] is None and thin["field_verdict"] is None, thin
    assert thin["lab_score"] == 42, thin

    # 판정은 scoring 한 곳이다 — 임계값을 여기서 다시 안 센다
    adv = scoring.vitals_advice([r])
    assert adv and adv[0]["tag"] == "속도", adv
    assert "4.2초" in adv[0]["now"], adv[0]["now"]
    fast = parse("https://x.kr/c", "mobile", {"loadingExperience": {
        "overall_category": "FAST", "metrics": {
            "LARGEST_CONTENTFUL_PAINT_MS": {"percentile": 1800},
            "INTERACTION_TO_NEXT_PAINT": {"percentile": 90},
            "CUMULATIVE_LAYOUT_SHIFT_SCORE": {"percentile": 3}}}})
    assert not scoring.vitals_advice([fast]), "기준 안에 드는 페이지에 속도 지적을 만든다"

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    conn.execute("INSERT INTO projects(id,name,type,domain) VALUES(1,'p','saas','x.kr')")
    assert db.write_page_vitals(conn, 1, "2026-09-09", [r, thin]) == 2
    assert db.write_page_vitals(conn, 1, "2026-09-09", [r, thin]) == 2, "같은 날 두 번이 늘어난다"
    got = conn.execute("SELECT COUNT(*) c FROM page_vitals").fetchone()["c"]
    assert got == 2, got
    print("collect_vitals self-check ok")


if __name__ == "__main__":
    main()
