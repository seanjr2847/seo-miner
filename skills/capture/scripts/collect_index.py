#!/usr/bin/env python3
"""URL 색인 상태 수집 — Search Console URL Inspection API.

지금까지 이 스킬은 URL 이 실제로 색인됐는지 몰랐다. scoring.coverage() 가
"GSC 노출이 있나/없나"로 색인 여부를 근사했고(노출 0 = 색인 안 됨은 거짓일 때가
많다 — 색인은 됐는데 순위가 낮아 노출이 안 잡히는 경우), SKILL.md 는 Claude 에게
URL 색인을 즉석으로만 확인하라고 시켰지만 그 결과가 아무 데도
저장되지 않아 다음 리포트에서 또 없어졌다. 이 수집기가 그 구멍을 메운다.

대상 URL: 최신 GSC 스냅샷에서 노출 합계 상위 N개 페이지. 사이트맵 전체가 아니라
"이미 트래픽이 걸린 페이지"부터 보는 이유는 쿼터 때문이다 — 하루 2,000회다.

비용: URL 당 정확히 1콜. 금전 비용은 없지만 쿼터가 곧 비용이다.
  · 속성당 하루 2,000회 / 분당 600회 (구글 문서 기준)
  · 429/403 을 만나면 거기까지 모은 것을 저장하고 멈춘다 — 전부 날리지 않는다.

권한: URL Inspection 은 Search Analytics 와 달리 '제한된 사용자' 권한으로는
거부될 수 있다(403). 소유자 또는 전체 사용자여야 한다.

인증: collect_gsc.get_service 를 그대로 빌린다 — 이 프로젝트는 열쇠 한 벌이 원칙이다.

Usage:
  python collect_index.py --project NAME [--limit N] [--dry-run]
  python collect_index.py                                  # self-check
"""
import argparse
import json
import sys
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import collector  # noqa: E402
import db  # noqa: E402
import scoring  # noqa: E402
from collect_gsc import get_service  # noqa: E402

# API 응답(camelCase) -> gsc_index_status 컬럼(snake_case).
# lastCrawlTime 만 이름이 안 맞는다(계약은 last_crawled) — 표를 두는 이유가 이것이다.
FIELDS = {
    "verdict": "verdict",
    "coverageState": "coverage_state",
    "robotsTxtState": "robots_txt_state",
    "pageFetchState": "page_fetch_state",
    "indexingState": "indexing_state",
    "googleCanonical": "google_canonical",
    "userCanonical": "user_canonical",
    "lastCrawlTime": "last_crawled",
}


# 노출 상위 페이지 목록의 정본은 scoring 이다 — 색인 검사와 페이지 감사가 같은
# 목록을 봐야 "검사한 페이지"와 "고칠 페이지"가 어긋나지 않는다.
top_pages = scoring.top_pages


def to_row(url: str, result: dict) -> dict:
    """inspectionResult -> write_index_status 가 받는 dict.

    없는 필드는 넣지 않고 None 으로 남긴다 — "모름"과 "없음"은 다르다
    (db.write_index_status 주석과 같은 규칙). richResultsResult 는 구조가
    자주 바뀌므로 해석하지 않고 원문 JSON 그대로 보관한다.
    """
    idx = result.get("indexStatusResult") or {}
    row = {"url": url}
    row.update({col: idx.get(api) for api, col in FIELDS.items()})
    rich = result.get("richResultsResult")
    row["rich_results_json"] = json.dumps(rich, ensure_ascii=False) if rich else None
    return row


def collect(project: str, *,
            dry_run: bool = False,
            index_urls: int | None = None,
            throttle: float | None = None,
            conn=None,
            service=None) -> collector.StageResult:
    """URL Inspection 으로 색인 상태를 Brain 에 적재한다. sys.exit 호출 없음.

    Args:
        project: 사이트 이름
        dry_run: True 면 검사 계획만 찍고 종료
        index_urls: 한 번에 검사할 URL 수. 0이면 끔
        throttle: 호출 간격(초). 분당 600회 제한을 넘지 않기 위한 것
        conn: 이미 열린 Brain 연결 — 주면 그것을 쓰고 닫지 않는다
        service: 서치콘솔 서비스 객체 — 주면 인증을 타지 않는다
                 (자체점검이 globals() 를 갈아끼우던 자리)

    Returns:
        StageResult(ok=...). 정상 종료면 ok=True. 사유가 있는 비종료는
        ok=False, skipped=True, reason 에 한국어 안내.
    """
    _parser()
    with collector.stage(project, conn=conn, dry_run=dry_run) as st:
        conn, p = st.conn, st.project
        s = st.settings(argparse.Namespace(limit=index_urls, throttle=throttle))
        limit = s["index_urls"]
        throttle = st.throttle
        prop = p["gsc_property"]
        if not prop:
            return st.skip("project yaml has no gsc_property "
                           "(e.g. 'sc-domain:example.com' or 'https://example.com/'). "
                           "Add it, then: python db.py sync-project <yaml>")
        if limit <= 0:
            print("[index] index_urls=0 — 색인 검사를 끄셨습니다. "
                  "켜려면 config.yaml defaults.index_urls 를 올리거나 --limit N 을 주세요.")
            return st.noop(rows=0)

        urls = top_pages(conn, p["id"], limit)
        if not urls:
            return st.skip("최신 GSC 스냅샷에 page 가 없습니다 — 먼저 `python collect_gsc.py "
                           f"--project {project}` 로 수집하세요. (page NULL 인 구버전 "
                           "스냅샷만 있어도 여기서 멈춥니다)")

        print(f"[index] {prop}  URL {len(urls)}개 = {len(urls)}콜 (URL 당 정확히 1콜)")
        if st.dry_run:
            for i, u in enumerate(urls, 1):
                print(f"  {i:>3}. {u}")
            print(f"쿼터: 속성당 하루 2,000회 / 분당 600회. 이번 실행은 {len(urls)}회 "
                  f"(간격 {throttle}초 ≈ {len(urls) * (throttle + 0.5) / 60:.1f} min).")
            return st.noop(rows=0)

        if service is None:
            try:
                service = get_service()
            except db.ProjectNotFound as e:
                return st.skip(str(e))
        from googleapiclient.errors import HttpError

        rows: list[dict] = []
        stop: str | None = None
        # 항목별 오류를 세고 넘어가는 st.each 를 쓰지 않는 유일한 수집기다 —
        # 여기서 오류는 쿼터·권한이라 다음 URL 도 똑같이 죽는다. 세지 말고 멈춘다.
        with st.record("index") as r:
            for i, url in enumerate(urls, 1):
                try:
                    resp = service.urlInspection().index().inspect(body={
                        "inspectionUrl": url, "siteUrl": prop}).execute()
                except HttpError as e:
                    # 쿼터(429)·권한(403) 어느 쪽이든 여기까지 모은 것은 살린다.
                    # 예전 방식(예외를 그대로 올림)은 19개 검사하고 20번째에서
                    # 죽으면 19개도 같이 날아갔다.
                    stop = f"HTTP {getattr(e.resp, 'status', '?')} at {i}/{len(urls)}"
                    break
                row = to_row(url, resp.get("inspectionResult") or {})
                rows.append(row)
                r.api_calls += 1
                print(f"  {i:>3}/{len(urls)}  {row['verdict'] or '?':<8} "
                      f"{row['coverage_state'] or ''}  {url}")
                if i < len(urls):
                    time.sleep(throttle)

            checked = str(date.today())
            db.write_index_status(conn, p["id"], checked, rows)
            r.notes = f"urls={len(rows)}/{len(urls)} checked={checked}" + (
                f" 중단: {stop}" if stop else "")

        if stop:
            print(f"\n[중단] {stop} — 여기까지 {len(rows)}개는 저장했습니다.", file=sys.stderr)
            if "403" in stop:
                print("  URL Inspection 은 속성의 '소유자' 또는 '전체 사용자' 권한이 필요합니다.\n"
                      "  '제한된 사용자'는 Search Analytics 는 되지만 이 API 는 거부됩니다 —\n"
                      "  Search Console → 설정 → 사용자 및 권한에서 권한을 올리세요.",
                      file=sys.stderr)
            else:
                print("  쿼터(하루 2,000회/분당 600회)일 가능성이 큽니다 — "
                      "--throttle 을 올리거나 내일 이어서 돌리세요.", file=sys.stderr)

        bad = [x for x in rows if (x["verdict"] or "") != "PASS"]
        print(f"\nsaved {len(rows)} URLs. 색인 문제 {len(bad)}개:")
        for x in bad[:10]:
            print(f"  {x['verdict'] or '?':<8} {x['coverage_state'] or '?':<28} {x['url']}")
        if not bad:
            print("  (없음 — 검사한 URL 전부 PASS)")
        return st.done(rows=len(rows))


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser()
    collector.add_common(ap)
    collector.add_setting(ap, "--limit", key="index_urls", fallback=20, type=int,
                          help="한 번에 검사할 URL 수. 0이면 끔")
    collector.add_setting(ap, "--throttle", key="throttle", fallback=0.5, type=float,
                          help="호출 간격(초). 분당 600회 제한을 넘지 않기 위한 것")
    return ap


def main() -> None:
    # 인자 없이 실행되면 자체 점검 — collect_gap 과 같은 약속. add_common 이
    # --project 를 required 로 걸어버리므로 parse 직전에 길이를 본다.
    if len(sys.argv) == 1:
        _selfcheck()
        return
    try:
        a = _parser().parse_args()

        r = collect(a.project, dry_run=a.dry_run,
                    index_urls=a.limit, throttle=a.throttle)
    except db.ProjectNotFound as e:
        sys.exit(str(e))
    if not r.ok and r.reason:
        sys.exit(r.reason)


def _selfcheck() -> None:
    """가짜 service 로 매핑·중단저장 경로를 검증한다 — 진짜 Brain·API 안 건드림."""
    import os
    import tempfile
    from googleapiclient.errors import HttpError

    home = Path(tempfile.mkdtemp(prefix="seo-miner-index-selftest-"))
    os.environ["CAPTURE_HOME"] = str(home)

    conn = db.connect()
    conn.execute("INSERT INTO projects(name, domain, locale, gsc_property) "
                 "VALUES('it', 'it.com', 'ko-KR', 'sc-domain:it.com')")
    pid = conn.execute("SELECT id FROM projects WHERE name='it'").fetchone()["id"]
    for page, imp in [("https://it.com/a", 300), ("https://it.com/b", 200),
                      ("https://it.com/c", 100)]:
        conn.execute(
            """INSERT INTO gsc_snapshots(project_id, snapshot_date, period_days,
                 query, page, clicks, impressions, ctr, position)
               VALUES(?, '2026-08-18', 28, 'q', ?, 1, ?, 0.01, 9.0)""", (pid, page, imp))
    conn.commit()

    assert top_pages(conn, pid, 2) == ["https://it.com/a", "https://it.com/b"], "노출순 상위 N"

    # 세 번째 URL 에서 429 — 앞의 두 개는 저장되어야 한다.
    class _Req:
        def __init__(self, body):
            self.url = body["inspectionUrl"]

        def execute(self):
            if self.url.endswith("/c"):
                # HttpError 는 생성 시점에 resp.reason 을 읽는다 — 가짜 resp 에도 있어야 한다.
                raise HttpError(type("R", (), {"status": 429, "reason": "quota"})(),
                                b'{"error":"quota"}')
            return {"inspectionResult": {
                "indexStatusResult": {
                    "verdict": "PASS" if self.url.endswith("/a") else "FAIL",
                    "coverageState": "Submitted and indexed" if self.url.endswith("/a")
                                     else "Blocked by robots.txt",
                    "robotsTxtState": "ALLOWED", "pageFetchState": "SUCCESSFUL",
                    "indexingState": "INDEXING_ALLOWED",
                    "googleCanonical": self.url, "userCanonical": self.url,
                    "lastCrawlTime": "2026-08-15T00:00:00Z"},
                "richResultsResult": {"verdict": "PASS"}}}

    class _Svc:
        def urlInspection(self):
            return self

        def index(self):
            return self

        def inspect(self, body):
            return _Req(body)

    # 의존물은 인자로 준다 — 예전에는 globals()["get_service"] 를 갈아끼웠다.
    # conn 도 같이 넘겨 러너가 빌린 것을 닫지 않는지까지 본다.
    res = collect("it", index_urls=3, throttle=0, conn=conn, service=_Svc())
    assert (res.ok, res.skipped, res.rows) == (True, False, 2), res
    conn.execute("SELECT 1")     # 빌린 conn 은 러너가 닫지 않는다

    saved = [dict(r) for r in conn.execute(
        "SELECT * FROM gsc_index_status WHERE project_id=? ORDER BY url", (pid,))]
    assert len(saved) == 2, f"429 앞의 2개는 살아야 함: {saved}"
    assert saved[0]["verdict"] == "PASS" and saved[1]["verdict"] == "FAIL"
    assert saved[0]["last_crawled"] == "2026-08-15T00:00:00Z", "lastCrawlTime -> last_crawled"
    assert saved[1]["coverage_state"] == "Blocked by robots.txt"
    assert json.loads(saved[0]["rich_results_json"]) == {"verdict": "PASS"}
    notes = conn.execute("SELECT notes, api_calls FROM runs WHERE project_id=?",
                         (pid,)).fetchone()
    assert notes["api_calls"] == 2 and "429" in notes["notes"], dict(notes)

    # dry-run 은 호출도 저장도 없어야 한다 — 지뢰 service 를 쥐여 주고 확인한다.
    conn.execute("DELETE FROM runs")
    conn.commit()

    class _Boom:
        def urlInspection(self):
            raise AssertionError("dry-run 이 API 를 건드렸다")

    res = collect("it", dry_run=True, conn=conn, service=_Boom())
    assert (res.ok, res.skipped) == (True, True), res
    assert conn.execute("SELECT * FROM runs").fetchall() == [], "dry-run 은 runs 를 남기지 않는다"
    conn.close()
    print("collect_index self-check ok")


if __name__ == "__main__":
    main()
