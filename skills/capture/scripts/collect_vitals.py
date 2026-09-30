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

한도(429)는 **실패가 아니라 건너뜀**이다. 키 없이 돌면 여러 사용자와 나눠 쓰는 하루
한도에 걸리는데, 그건 내 사이트가 느리다는 뜻도 수집이 고장났다는 뜻도 아니다. 여태는
429 가 항목 실패로 세어져 속도 단계가 "전부 실패" 가 되고, 그 하나 때문에 런 전체가
last_ok=0 으로 남았다(9/24 theotherskin). 한 번 429 면 나머지도 같은 한도에 걸리므로
거기서 멈추고, 그때까지 잰 것은 남긴다.

Usage:
  python collect_vitals.py --project NAME [--limit N] [--strategy mobile,desktop]
  python collect_vitals.py                                  # self-check
"""
import argparse
import json
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

# 구글이 연 모습으로 본 SEO 점검 — 같은 PageSpeed 호출에 category=seo 를 더하면 온다(추가 비용 0).
# 왜 여기 있나: 데이터센터 IP·파이썬 클라이언트를 막는 사이트(gucci.com)는 페이지 점검
# (collect_page)도 크롤도 한 장도 못 본다. PageSpeed 는 구글 인프라가 여는 것이라 통과한다 —
# 그 사이트에서 우리가 얻을 수 있는 유일한 "페이지 안" 사실이다.
# {Lighthouse audit id: (화면 이름, 실패일 때 고칠 것)} — 이름표의 정본은 여기 한 벌이다
# (화면은 d.seo_audits 로 받는다). 여기 없는 id 는 적지 않는다(구글이 항목을 늘려도 모르는
# 영어 이름이 화면에 새지 않게).
SEO_AUDITS = {
    "http-status-code":  ("응답 상태", "검색 로봇이 받는 응답이 200 이 아닙니다."),
    "is-crawlable":      ("색인 허용", "noindex 나 robots 규칙이 이 페이지의 색인을 막습니다."),
    "robots-txt":        ("robots.txt", "robots.txt 에 문법 오류가 있습니다."),
    "document-title":    ("제목 태그", "<title> 이 없습니다."),
    "meta-description":  ("메타 설명", "meta description 이 없습니다."),
    "canonical":         ("canonical", "rel=canonical 이 없거나 잘못된 주소를 가리킵니다."),
    "hreflang":          ("hreflang", "언어·지역 대체 주소 표기가 잘못됐습니다."),
    "image-alt":         ("이미지 alt", "alt 없는 이미지가 있습니다."),
    "link-text":         ("링크 텍스트", "'더보기'·'여기'처럼 뜻 없는 링크 글자가 있습니다."),
    "crawlable-anchors": ("크롤 가능한 링크", "href 없이 스크립트로만 움직이는 링크가 있습니다."),
}


def parse_seo(lh: dict) -> tuple:
    """Lighthouse 결과 → (seo_score 0~100 | None, seo_json | None). 실패 항목만 근거를 싣는다."""
    cat = (lh.get("categories") or {}).get("seo") or {}
    score = cat.get("score")
    if not isinstance(score, (int, float)):
        return None, None
    audits = lh.get("audits") or {}
    ok, fail = [], []
    for aid in SEO_AUDITS:
        a = audits.get(aid) or {}
        s = a.get("score")
        if a.get("scoreDisplayMode") not in ("binary", "numeric") or s is None:
            continue                     # 해당 없음·수동 — 통과도 실패도 아니다
        if s >= 1:
            ok.append(aid)
            continue
        items = ((a.get("details") or {}).get("items") or [])
        ev = []
        for it in items[:3]:
            node = it.get("node") or {}
            v = node.get("snippet") or it.get("text") or it.get("href") or it.get("source") or ""
            if isinstance(v, dict):
                v = v.get("url") or ""
            if v:
                ev.append(str(v)[:120])
        fail.append({"id": aid, "n": len(items), "items": ev})
    return round(score * 100), json.dumps({"ok": ok, "fail": fail}, ensure_ascii=False)


QUOTA_STATUS = 429
QUOTA_REASON = ("PageSpeed 오늘 한도 — 내일 다시 "
                "(PAGESPEED_API_KEY 를 넣으면 한도가 커집니다)")


# 측정 불가 — PageSpeed 는 응답했는데 Lighthouse 가 페이지를 못 그렸다고 답한 것
# ("Lighthouse returned error: NO_FCP"). 우리 수집의 고장도, 그 페이지가 느리다는 뜻도
# 아니다. noti 는 매주 10회 중 9회가 이것이었고, 항목 실패로 세어져 런이 매주 errors=9 로
# 남았다. 코드의 뜻(Lighthouse 문서 기준):
#   NO_FCP / NO_LCP      측정 시간 안에 화면에 아무것도 안 그려졌다 — 첫 화면이 자바스크립트
#                        뒤에야 나타나거나(투명도 0 에서 시작하는 등장 효과·로딩 화면),
#                        측정기(구글 데이터센터의 헤드리스 크롬)를 막거나 빈 화면을 줄 때
#   PAGE_HUNG            페이지가 멈춰 측정을 끝내지 못했다
#   NO_SPEEDLINE_FRAMES  그려진 프레임을 하나도 못 모았다
UNMEASURABLE = ("NO_FCP", "NO_LCP", "PAGE_HUNG", "NO_SPEEDLINE_FRAMES")
UNMEASURABLE_WHY = ("구글 측정기(Lighthouse)가 이 페이지에서 화면이 그려지는 것을 못 봤습니다 — "
                    "첫 화면이 자바스크립트가 끝난 뒤에야 나타나거나(투명도 0 에서 시작하는 등장 "
                    "효과·로딩 화면) 측정기를 막으면 납니다. 사람 눈에는 보여도 구글 렌더러에는 "
                    "빈 화면일 수 있으니 그 자체로 확인할 거리입니다")
# 측정 불가였던 주소는 이 기간 동안 다른 페이지로 바꿔 잰다 — 같은 주소를 매주 다시 재면
# 같은 답(NO_FCP)을 사러 가는 것이다. 기간이 지나면 한 번 다시 본다(고쳤을 수 있다).
UNMEASURABLE_DAYS = 28


def unmeasurable(error: str | None) -> str | None:
    """오류 문장 → 측정 불가 코드(NO_FCP 등) | None. 옛 행의 원문("HTTP 400 · Lighthouse
    returned error: NO_FCP …")과 새 행("측정 불가(NO_FCP) — …")을 다 읽는다."""
    for code in UNMEASURABLE:
        if error and code in error:
            return code
    return None


def recent_unmeasurable(conn, project_id: int, days: int = UNMEASURABLE_DAYS) -> set[str]:
    """최근 days 일 안의 마지막 측정에서 **모든 기기가** 측정 불가였던 주소."""
    rows = conn.execute(
        "SELECT v.url, v.error FROM page_vitals v WHERE v.project_id=? AND v.checked_date = ("
        " SELECT MAX(b.checked_date) FROM page_vitals b WHERE b.project_id=v.project_id"
        " AND b.url=v.url) AND julianday('now') - julianday(v.checked_date) < ?",
        (project_id, days)).fetchall()
    by: dict[str, list] = {}
    for r in rows:
        by.setdefault(r["url"], []).append(unmeasurable(r["error"]))
    return {u for u, codes in by.items() if codes and all(codes)}


class _QuotaHit(collector.Fatal):
    """PageSpeed 한도(429) — 남은 URL 도 같은 한도에 걸린다. Stage.each 는 Fatal 만
    삼키지 않고 올리므로 그 통로로 멈추되, collect 가 받아서 **건너뜀**으로 바꾼다
    (Fatal 이 그대로 러너까지 가면 단계 실패가 된다)."""


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
    row["seo_score"], row["seo_json"] = parse_seo(lh)
    return row


def fetch(url: str, strategy: str, *, timeout: int | None = None) -> dict:
    """URL 하나·기기 하나. 실패도 한 줄로 남긴다 — collect_page.fetch 와 같은 규칙이다."""
    import requests
    # category 는 여러 번 싣는다(PSI 는 반복 키로 받는다) — seo 는 같은 호출에 얹혀 공짜다.
    params = [("url", url), ("strategy", strategy),
              ("category", "performance"), ("category", "seo")]
    key = os.environ.get("PAGESPEED_API_KEY")
    if key:
        params.append(("key", key))
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
        code = unmeasurable(detail)
        if code:
            return {"url": url, "strategy": strategy, "status": r.status_code,
                    "unmeasurable": code, "error": f"측정 불가({code}) — {UNMEASURABLE_WHY}"}
        return {"url": url, "strategy": strategy, "status": r.status_code,
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
        # 측정 불가였던 주소는 빼고 그만큼 다음 페이지로 채운다(UNMEASURABLE_DAYS 동안).
        dead = recent_unmeasurable(conn, p["id"])
        urls = [u for u in collect_page.target_urls(conn, p["id"], limit + len(dead))
                if u not in dead][:limit]
        if dead:
            print(f"[vitals] 최근 측정 불가(NO_FCP 등)였던 {len(dead)}곳은 "
                  f"{UNMEASURABLE_DAYS}일 동안 다른 페이지로 바꿔 잽니다")
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
        unmeas: list[str] = []

        def one(job) -> None:
            url, dev = job
            row = fetch(url, dev)
            if row.get("status") == QUOTA_STATUS:
                # 이 행은 안 남긴다 — "이 페이지를 못 쟀다" 가 아니라 "오늘은 더 못 잰다" 다
                raise _QuotaHit(row["error"])
            # 행은 남긴다 — page_vitals.error 는 "못 쟀다"를 적는 진짜 칸이다.
            # 그리고 실패는 예외로 올린다: 여기서 return 하면 실패가 데이터로만 남아
            # 전부 못 재고도 runs.notes 에 errors=0 이 적힌다(collect_page 와 같은 규칙).
            rows.append(row)
            if row.get("unmeasurable"):
                # 페이지 쪽 사실이다 — 행은 남기되(화면이 사유를 말한다) 실패로 안 센다
                unmeas.append(url)
                print(f"  · [{dev}] {url} — 측정 불가({row['unmeasurable']})")
                return
            if row.get("error"):
                raise collector.ItemFailed(row["error"])
            lcp = row.get("field_lcp_ms") or row.get("lab_lcp_ms")
            score = row.get("lab_score")
            print(f"  ✓ [{dev}] {url} — 점수 {score if score is not None else '—'}"
                  + (f" · LCP {lcp}ms" if lcp else ""))

        quota = ""
        with st.record("vitals") as r:
            try:
                done = st.each(jobs, one, label=lambda j: f"[{j[1]}] {j[0]}")
            except _QuotaHit as e:
                quota = str(e)
                done = sum(1 for x in rows if not x.get("error"))
                print(f"  ! {QUOTA_REASON} ({quota})", file=sys.stderr)
            checked = str(date.today())
            db.write_page_vitals(conn, p["id"], checked, rows)
            r.api_calls = done
            measured = sum(1 for x in rows if not x.get("error"))    # 측정 불가는 잰 것이 아니다
            r.notes = (f"urls={len(urls)} strategies={','.join(want)} "
                       f"rows={len(rows)} checked={checked} "
                       + (f"unmeasurable={len(unmeas)} " if unmeas else "")
                       + f"{st.err_note}"
                       + (f" | quota={QUOTA_STATUS} — {QUOTA_REASON}" if quota else ""))

        bad_rows = [x for x in rows if x.get("error")]
        print(f"\nsaved {len(rows)} vitals rows (errors={st.errors})"
              + (f" · 못 잰 것 {len(bad_rows)}개" if bad_rows else ""))
        if quota and not done:
            # 한 건도 못 잰 채 한도 — 건너뜀이다. 앞 URL 에 다른 실패가 있었어도 오늘 이
            # 단계가 말할 수 있는 것은 "한도라서 못 쟀다" 하나다.
            return st.skip(QUOTA_REASON)
        if unmeas:
            print(f"  ! 측정 불가 {len(unmeas)}건 — {UNMEASURABLE_WHY}", file=sys.stderr)
        if unmeas and not measured and not st.errors:
            # 전부 측정 불가 — 실패가 아니라 건너뜀이다. 사유는 한 번만 사람 말로.
            return st.skip(f"잰 페이지 {len(set(unmeas))}곳이 전부 측정 불가입니다. {UNMEASURABLE_WHY}.")
        # 실제로 잰 건수로 판정한다 — 전부 못 잰 것은 완료가 아니다. 한도 전까지 잰 것이
        # 있으면 그만큼으로 완료다(한도는 실패로 안 센다 — each 가 세기 전에 멈췄다).
        return st.verdict(measured, rows=len(rows))


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
    # 구글이 연 모습의 SEO 점검 — 실패 항목만 근거와 함께, 수동·해당 없음은 빼고, 모르는 id 는 버린다.
    assert parse_seo({}) == (None, None), "SEO 카테고리가 없는 응답(옛 호출)에 점수를 지어낸다"
    lh = {"categories": {"seo": {"score": 0.92}},
          "audits": {"document-title": {"score": 1, "scoreDisplayMode": "binary"},
                     "link-text": {"score": 0, "scoreDisplayMode": "binary", "details": {"items": [
                         {"href": "https://g.kr/a", "text": "더보기"}, {"href": "https://g.kr/b", "text": "여기"}]}},
                     "image-alt": {"score": 0, "scoreDisplayMode": "binary", "details": {"items": [
                         {"node": {"snippet": '<img src="x.jpg">'}}]}},
                     "structured-data": {"score": None, "scoreDisplayMode": "manual"},
                     "new-google-audit": {"score": 0, "scoreDisplayMode": "binary"}}}
    sc, sj = parse_seo(lh)
    got = json.loads(sj)
    assert sc == 92 and got["ok"] == ["document-title"], (sc, got)
    assert [(f["id"], f["n"], f["items"]) for f in got["fail"]] == [
        ("image-alt", 1, ['<img src="x.jpg">']), ("link-text", 2, ["더보기", "여기"])], got["fail"]
    r = parse("https://g.kr/", "mobile", {"lighthouseResult": lh})
    assert r["seo_score"] == 92 and r["seo_json"] == sj, "parse 가 SEO 점검을 행에 안 싣는다"
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
    _quota_and_key_check(sample)
    print("collect_vitals self-check ok")


def _quota_and_key_check(sample: dict) -> None:
    """키를 실어 보내나 · 429(한도)가 실패가 아니라 건너뜀인가 — 가짜 requests.get 으로(네트워크 0)."""
    import contextlib
    import io
    import tempfile

    import requests

    class Resp:
        def __init__(self, status, body):
            self.status_code, self._body = status, body

        def json(self):
            return self._body

    quota_body = {"error": {"code": 429, "message": "Quota exceeded for quota metric "
                            "'Queries' and limit 'Queries per day'"}}
    sent: list[dict] = []
    script: list = []

    def fake_get(url, params=None, timeout=None):
        sent.append(dict(params or {}))
        return script.pop(0) if script else Resp(QUOTA_STATUS, quota_body)

    orig_get, orig_key = requests.get, os.environ.pop("PAGESPEED_API_KEY", None)
    orig_targets = collect_page.target_urls
    os.environ["CAPTURE_HOME"] = str(Path(tempfile.mkdtemp(prefix="seo-miner-vitals-selftest-")))
    boot = db.connect()
    boot.execute("INSERT INTO projects(name, domain, locale) VALUES('vt','vt.kr','ko-KR')")
    boot.commit()
    boot.close()
    requests.get = fake_get
    collect_page.target_urls = lambda c, pid, limit: ["https://vt.kr/a", "https://vt.kr/b"]

    def run():
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return collect("vt", strategy="mobile", throttle=0)

    try:
        # 1. 키 — 환경변수가 있으면 key= 로 싣고, 없으면 안 싣는다(빈 key= 는 400 이다)
        sent.clear()
        fetch("https://vt.kr/a", "mobile")
        assert "key" not in sent[-1], sent[-1]
        os.environ["PAGESPEED_API_KEY"] = "selftest-key"
        fetch("https://vt.kr/a", "mobile")
        assert sent[-1].get("key") == "selftest-key", f"키를 요청에 안 실었다: {sent[-1]}"
        del os.environ["PAGESPEED_API_KEY"]

        # 2. 처음부터 한도 — 실패가 아니라 건너뜀, 사유는 사람 말, 첫 429 에서 멈춘다
        sent.clear()
        res = run()
        assert (res.ok, res.skipped, res.failed) == (True, True, False), res
        assert res.reason == QUOTA_REASON, res.reason
        assert len(sent) == 1, f"한도에 걸린 뒤로도 계속 불렀다: {len(sent)}회"
        conn = db.connect()
        assert conn.execute("SELECT COUNT(*) c FROM page_vitals").fetchone()["c"] == 0, \
            "한도를 '이 페이지를 못 쟀다' 행으로 남겼다"
        notes = conn.execute("SELECT notes FROM runs WHERE kind='vitals' ORDER BY id DESC"
                             ).fetchone()["notes"]
        assert "quota=429" in notes, notes
        conn.close()

        # 3. 한 장 잰 뒤 한도 — 잰 것은 남기고 완료(실패·부분 실패 아님)
        script[:] = [Resp(200, sample)]
        res = run()
        assert (res.ok, res.skipped, res.partial, res.rows) == (True, False, False, 1), res
        conn = db.connect()
        assert conn.execute("SELECT COUNT(*) c FROM page_vitals WHERE error IS NULL"
                            ).fetchone()["c"] == 1
        conn.close()

        # 4. 한도가 아닌 실패(500)는 여전히 실패다 — 429 만 건너뜀으로 바꾼다
        script[:] = [Resp(500, {}), Resp(500, {})]
        res = run()
        assert res.failed, f"500 을 건너뜀으로 삼켰다: {res}"

        # 5. 측정 불가(NO_FCP) — noti 가 매주 10회 중 9회 이것이었다. 실패가 아니라 페이지의
        #    사실이다: 전부 그러면 건너뜀(사유 한 번), 섞이면 잰 것만으로 완료(errors=0).
        nofcp = {"error": {"code": 400, "message": "Lighthouse returned error: NO_FCP. The page "
                           "did not paint any content."}}
        script[:] = [Resp(400, nofcp), Resp(400, nofcp)]
        res = run()
        assert (res.ok, res.skipped, res.failed) == (True, True, False), res
        assert "측정 불가" in res.reason and "NO_FCP" not in res.reason, res.reason
        conn = db.connect()
        errs = [r["error"] for r in conn.execute(
            "SELECT error FROM page_vitals WHERE checked_date=date('now') AND error IS NOT NULL")]
        assert errs and all(e.startswith("측정 불가(NO_FCP)") for e in errs), errs
        # 다음 회차 — 측정 불가였던 두 곳은 빼고 다음 페이지를 잰다
        asked: list = []
        collect_page.target_urls = lambda c, pid, limit: (asked.append(limit) or
                                                          ["https://vt.kr/a", "https://vt.kr/b",
                                                           "https://vt.kr/c"])[:limit]
        sent.clear()
        script[:] = [Resp(200, sample)]
        res = run()
        assert [p["url"] for p in sent] == ["https://vt.kr/c"], f"측정 불가 주소를 또 쟀다: {sent}"
        assert res.ok and not res.partial and asked == [7], (res, asked)
        # 섞인 회차 — 잰 것은 완료, 측정 불가는 errors 에 안 들어간다
        conn.execute("DELETE FROM page_vitals")
        conn.commit()
        collect_page.target_urls = lambda c, pid, limit: ["https://vt.kr/a", "https://vt.kr/b"]
        script[:] = [Resp(200, sample), Resp(400, nofcp)]
        res = run()
        assert (res.ok, res.partial, res.failed) == (True, False, False), res
        notes = conn.execute("SELECT notes FROM runs WHERE kind='vitals' ORDER BY id DESC"
                             ).fetchone()["notes"]
        assert "unmeasurable=1" in notes and "errors=0" in notes, notes
        conn.close()
        # 옛 행의 원문도 같은 판정이다(바꾸기 전 저장된 noti 의 행)
        assert unmeasurable("HTTP 400 · Lighthouse returned error: NO_FCP. The page did not") == "NO_FCP"
        assert unmeasurable("HTTP 500") is None
    finally:
        requests.get = orig_get
        collect_page.target_urls = orig_targets
        if orig_key is not None:
            os.environ["PAGESPEED_API_KEY"] = orig_key
        else:
            os.environ.pop("PAGESPEED_API_KEY", None)


if __name__ == "__main__":
    main()
