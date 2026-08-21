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


def top_pages(conn, project_id: int, limit: int) -> list[str]:
    """최신 스냅샷의 노출 상위 페이지. page IS NULL(구버전 CSV 스냅샷)은 뺀다.

    snapshot_pair 로 period_days 까지 맞춰 고른다 — 같은 날짜에 기간이 다른
    스냅샷이 섞여 있으면 노출 합계가 이중으로 잡힌다.
    """
    cur, _, period, _ = scoring.snapshot_pair(conn, project_id)
    if not cur:
        return []
    return [r["page"] for r in conn.execute(
        """SELECT page, SUM(impressions) imp FROM gsc_snapshots
            WHERE project_id=? AND snapshot_date=? AND period_days=? AND page IS NOT NULL
            GROUP BY page ORDER BY imp DESC LIMIT ?""",
        (project_id, cur, period, limit))]


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


def main() -> None:
    ap = argparse.ArgumentParser()
    collector.add_common(ap)
    collector.add_setting(ap, "--limit", key="index_urls", fallback=20, type=int,
                          help="한 번에 검사할 URL 수. 0이면 끔")
    collector.add_setting(ap, "--throttle", key="throttle", fallback=0.5, type=float,
                          help="호출 간격(초). 분당 600회 제한을 넘지 않기 위한 것")
    # 인자 없이 실행되면 자체 점검 — collect_gap 과 같은 약속. add_common 이
    # --project 를 required 로 걸어버리므로 parse 직전에 길이를 본다.
    if len(sys.argv) == 1:
        _selfcheck()
        return
    a = ap.parse_args()

    conn, p, cfg = collector.open_project(a.project)
    s = collector.settings(a, cfg)
    limit, throttle = s["index_urls"], s["throttle"]
    prop = p["gsc_property"]
    if not prop:
        conn.close()
        sys.exit("project yaml has no gsc_property "
                 "(e.g. 'sc-domain:example.com' or 'https://example.com/'). "
                 "Add it, then: python db.py sync-project <yaml>")
    if limit <= 0:
        conn.close()
        print("[index] index_urls=0 — 색인 검사를 끄셨습니다. "
              "켜려면 config.yaml defaults.index_urls 를 올리거나 --limit N 을 주세요.")
        return

    urls = top_pages(conn, p["id"], limit)
    if not urls:
        conn.close()
        sys.exit("최신 GSC 스냅샷에 page 가 없습니다 — 먼저 `python collect_gsc.py "
                 f"--project {a.project}` 로 수집하세요. (page NULL 인 구버전 "
                 "스냅샷만 있어도 여기서 멈춥니다)")

    print(f"[index] {prop}  URL {len(urls)}개 = {len(urls)}콜 (URL 당 정확히 1콜)")
    if a.dry_run:
        for i, u in enumerate(urls, 1):
            print(f"  {i:>3}. {u}")
        print(f"쿼터: 속성당 하루 2,000회 / 분당 600회. 이번 실행은 {len(urls)}회 "
              f"(간격 {throttle}초 ≈ {len(urls) * (throttle + 0.5) / 60:.1f} min).")
        conn.close()
        return

    service = get_service()
    from googleapiclient.errors import HttpError

    rows: list[dict] = []
    stop: str | None = None
    with db.run(conn, p["id"], "index") as r:
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
    conn.close()


def _selfcheck() -> None:
    """가짜 service 로 매핑·중단저장 경로를 검증한다 — 진짜 Brain·API 안 건드림.

    db.CAPTURE_HOME/DB_PATH 는 import 시점에 계산되므로 환경변수만 바꿔선
    진짜 brain.db 를 건드린다 (collect_gap._selfcheck 와 같은 함정). 모듈 속성을
    직접 패치한다.
    """
    import os
    import tempfile
    from googleapiclient.errors import HttpError

    home = Path(tempfile.mkdtemp(prefix="seo-miner-index-selftest-"))
    db.CAPTURE_HOME = home
    db.DB_PATH = home / "brain.db"
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

    globals()["get_service"] = lambda: _Svc()
    orig_argv = sys.argv
    try:
        sys.argv = ["collect_index.py", "--project", "it", "--limit", "3", "--throttle", "0"]
        main()
    finally:
        sys.argv = orig_argv

    conn = db.connect()
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

    # dry-run 은 호출도 저장도 없어야 한다.
    conn.execute("DELETE FROM runs")
    conn.commit()
    globals()["get_service"] = lambda: (_ for _ in ()).throw(
        AssertionError("dry-run 이 인증을 건드렸다"))
    try:
        sys.argv = ["collect_index.py", "--project", "it", "--dry-run"]
        main()
    finally:
        sys.argv = orig_argv
    assert conn.execute("SELECT * FROM runs").fetchall() == [], "dry-run 은 runs 를 남기지 않는다"
    conn.close()
    print("collect_index self-check ok")


if __name__ == "__main__":
    main()
