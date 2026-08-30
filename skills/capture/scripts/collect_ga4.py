#!/usr/bin/env python3
"""Sync Google Analytics 4 데이터를 Brain 에 적재 — 클릭 뒤에 무슨 일이 났는지.

GSC 는 클릭까지만 안다. 이 수집기가 그 뒤(세션이 뭘 했는지: 전환·매출·이탈)를
랜딩페이지 축으로 채운다 — GSC 의 page 와 잇는 축이 랜딩페이지이기 때문이다.

지표 이름은 구글 문서로 확인했다(추측 금지, 팀장 지시):
  https://developers.google.com/analytics/devguides/reporting/data/v1/api-schema
  · 차원: landingPage
  · 전환: keyEvents — 예전 이름 'conversions' 를 2024-05-06 개명. isConversionEvent도
    isKeyEvent 로 바뀌었다(devguides 체인지로그). 옛 이름을 쓰면 최신 GA4 문서
    기준으로 틀린다.
  · 그 외: sessions, totalRevenue, bounceRate — api-schema 목록에 그대로 있다.

랜딩페이지 ↔ GSC page 매칭 규칙 (이 연동에서 제일 조용히 틀리기 쉬운 자리):
  GA4 의 landingPage 차원은 호스트 없는 **경로**로 온다(가끔 쿼리스트링이 붙는다:
  '/blog/a' 또는 '/blog/a?utm=x'). GSC 의 page 는 **절대 URL**이다
  ('https://example.com/blog/a'). 유일한 규칙은:
      GA4 landing_page(저장값) == urlsplit(gsc_page).path
  양쪽 다 쿼리스트링·프래그먼트를 버리고 경로만 남긴다. GA4 쪽은 _landing_path()
  가 적재 시점에 정규화해서 저장한다 — GSC 쪽은 교차 분석(다음 단계)이
  urlsplit(page).path 로 맞춘다. 정규화 규칙의 정본은 이 파일, 이 함수 하나다.

날짜 창·버퍼: GSC 와 같다(오늘-3일 버퍼, 기본 28일 창) — gsc_query.py 상단 주석이
설명하듯, 스냅샷끼리 Δ를 비교하려면 나중에 값이 바뀌는 날짜가 섞이면 안 된다.
GA4 Data API 에는 GSC 의 dataState=final 같은 파라미터가 없다 — 대신 GA4 자체
문서(Audience export expectations)가 표준 속성의 데이터 반영 지연을 24~38시간
정도로 말한다. GSC 의 3일 버퍼가 이 지연도 넉넉히 덮는다.

인증: 열쇠는 한 벌이다(collect_gsc.py 의 원칙 그대로). collect_gsc.get_credentials()
가 db.gsc_auth() 로 OAuth/서비스 계정을 가르는 판정의 정본이고, collect_gsc.
get_service() 와 이 파일이 둘 다 그 위에 선다. collect_gsc.SCOPES 에
analytics.readonly 를 더했으므로 같은 Credentials 가 GA4 API 에도 그대로 통한다.

속성 미연결: project 의 ga4_property 가 비어 있으면 조용히 건너뛴다(다른 선택
단계들과 같은 약속) — 엉뚱한 속성을 자동으로 붙이면 숫자가 전부 거짓이 되므로,
속성 고르기는 여기서 하지 않는다(list_properties/suggest_property 가 후보만
낸다 — 확정은 다음 단계인 화면 몫).

Usage:
  python collect_ga4.py --project NAME [--days 28] [--row-limit 100000] [--dry-run]
  python collect_ga4.py                                  # self-check
"""
import argparse
import re
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import collect_gsc  # noqa: E402
import collector  # noqa: E402
import db  # noqa: E402

# runReport 의 metrics 순서 — 응답 row 의 metricValues 인덱스와 이 순서가 맞아야 한다.
METRICS = ("sessions", "keyEvents", "totalRevenue", "bounceRate")


def _landing_path(value: str) -> str:
    """GA4 landingPage 값을 경로만 남긴 정규형으로 — 매칭 규칙의 정본(모듈 docstring 참조)."""
    path = (value or "").split("?", 1)[0].split("#", 1)[0]
    return path or "/"


def get_service():
    """GA4 Data API(리포트)·Admin API(속성 목록) 서비스 객체 한 쌍. 모듈 docstring 의
    '인증' 절 참조 — collect_gsc.get_credentials() 가 인증 판정의 정본이다."""
    from googleapiclient.discovery import build

    creds = collect_gsc.get_credentials()
    data_svc = build("analyticsdata", "v1beta", credentials=creds, cache_discovery=False)
    admin_svc = build("analyticsadmin", "v1beta", credentials=creds, cache_discovery=False)
    return data_svc, admin_svc


def list_properties(admin_svc) -> list[dict]:
    """이 구글 계정이 접근 가능한 GA4 속성 전부 — [{account, id, name}].

    id 는 DB 저장 형식 그대로(숫자 문자열, 'properties/' 접두 없음). 고르는 화면은
    다음 단계 몫이라 여기서는 목록만 낸다 — 자동으로 확정하지 않는다.
    """
    out: list[dict] = []
    req = admin_svc.accountSummaries().list(pageSize=200)
    while req is not None:
        resp = req.execute()
        for acc in resp.get("accountSummaries", []) or []:
            for ps in acc.get("propertySummaries", []) or []:
                prop = ps.get("property", "")            # "properties/12345"
                out.append({"account": acc.get("displayName", ""),
                           "id": prop.rsplit("/", 1)[-1],
                           "name": ps.get("displayName", "")})
        req = admin_svc.accountSummaries().list_next(req, resp)
    return out


def suggest_property(properties: list[dict], domain: str) -> list[dict]:
    """도메인과 이름이 겹치는 속성만 추린다 — **제안일 뿐 확정이 아니다**. 엉뚱한
    속성이 붙으면 모든 숫자가 조용히 거짓이 된다. 핵심 토큰(www·서브도메인·TLD를
    뗀 첫 라벨)이 속성 표시 이름에 들어 있으면 후보로 본다."""
    core = re.sub(r"^www\.", "", (domain or "").lower()).split(".")[0]
    if not core:
        return []
    return [p for p in properties if core in (p.get("name") or "").lower()]


def collect(project: str, *,
            dry_run: bool = False,
            days: int | None = None,
            row_limit: int = 100000,
            conn=None,
            data_svc=None) -> collector.StageResult:
    """GA4 실적(세션·키 이벤트·매출·이탈률, 랜딩페이지별)을 Brain 에 적재한다.
    sys.exit 호출 없음 — 결과를 StageResult 로 반환.

    Args:
        project: 사이트 이름 (project yaml 의 name)
        dry_run: True 면 호출 계획만 찍고 종료
        days: 수집 창(config 키는 ga4_days, 오늘-3일 기준 N일) — GSC 와 같은 기본값
        row_limit: 가져올 최대 랜딩페이지 행 수
        conn: 이미 열린 Brain 연결 — 주면 그것을 쓰고 닫지 않는다
        data_svc: GA4 Data API 서비스 객체 — 주면 인증을 타지 않는다(자체점검용)

    Returns:
        StageResult(ok=...) — 정상 종료면 ok=True. 속성 미연결·인증 없음은
        ok=False, skipped=True, reason 에 한국어 안내.
    """
    ap = _parser()
    with collector.stage(project, conn=conn, dry_run=dry_run) as st:
        conn, p = st.conn, st.project
        s = st.settings(ap, argparse.Namespace(days=days))
        days = s["ga4_days"]
        prop_id = p["ga4_property"]
        if not prop_id:
            return st.skip(f"'{project}' 에 GA4 속성이 연결되어 있지 않습니다 — "
                           "project yaml 에 ga4_property: '숫자 ID' 를 넣고 "
                           "python db.py sync-project <yaml> (연결 화면은 다음 단계)")

        end = date.today() - timedelta(days=3)     # GSC 와 같은 버퍼 — 모듈 docstring 참조
        start = end - timedelta(days=days)
        print(f"[ga4] properties/{prop_id}  window {start} ~ {end} (period={days}d)")
        print(f"[ga4] API 호출 계획: runReport 1회 (limit={row_limit})")
        if st.dry_run:
            return st.noop(rows=0)

        if data_svc is None:
            try:
                data_svc, _ = get_service()
            except db.ProjectNotFound as e:
                return st.skip(str(e))

        from googleapiclient.errors import HttpError
        try:
            resp = data_svc.properties().runReport(
                property=f"properties/{prop_id}",
                body={"dateRanges": [{"startDate": str(start), "endDate": str(end)}],
                      "dimensions": [{"name": "landingPage"}],
                      "metrics": [{"name": m} for m in METRICS],
                      "limit": row_limit}).execute()
        except HttpError as e:
            if getattr(e.resp, "status", None) == 403:
                return st.skip(
                    f"GA4 접근 권한이 없습니다(속성 {prop_id}) — 이 구글 계정이 그 속성의 "
                    "뷰어 이상 권한을 가졌는지 확인하거나, GSC 로그인 뒤에 GA4 스코프가 "
                    f"추가됐으니 재로그인이 필요할 수 있습니다: {db.gsc_token()} 를 지우고 "
                    "채팅에 \"GSC 로그인해줘\"")
            raise

        raw_rows = resp.get("rows", []) or []
        print(f"[ga4] fetched {len(raw_rows)} landing page rows")
        if len(raw_rows) >= row_limit:
            print(f"[주의] --row-limit({row_limit})에 걸려 잘렸을 수 있습니다 — "
                  "더 필요하면 값을 올리세요.")

        def val(row, i):
            return row["metricValues"][i]["value"]

        parsed = [(_landing_path(row["dimensionValues"][0]["value"]),
                  val(row, 0), val(row, 1), val(row, 2), val(row, 3))
                 for row in raw_rows]

        snap = str(date.today())
        with st.record("ga4") as r:
            db.write_ga4_snapshot(conn, p["id"], snap, days, parsed)
            r.api_calls = 1
            r.notes = f"rows={len(parsed)} window={start}~{end}"

        print(f"saved ga4 snapshot {snap} ({len(parsed)} landing pages)")
        return st.done(rows=len(parsed))


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser()
    collector.add_common(ap)
    collector.add_setting(ap, "--days", key="ga4_days", fallback=28, type=int)
    ap.add_argument("--row-limit", type=int, default=100000,
                    help="가져올 최대 랜딩페이지 행 수")
    return ap


def main() -> None:
    if len(sys.argv) == 1:
        _selfcheck()
        return
    try:
        a = _parser().parse_args()
        r = collect(a.project, dry_run=a.dry_run, days=a.days, row_limit=a.row_limit)
    except db.ProjectNotFound as e:
        sys.exit(str(e))
    if not r.ok and r.reason:
        sys.exit(r.reason)


def _selfcheck() -> None:
    """가짜 data_svc 로 매칭 규칙·적재·건너뜀 경로를 검증한다 — 진짜 Brain·API 안 건드림."""
    import os
    import tempfile

    # 1. 랜딩페이지 ↔ GSC page 매칭 규칙 — 이 연동에서 제일 조용히 틀리기 쉬운 자리다.
    assert _landing_path("/blog/a") == "/blog/a"
    assert _landing_path("/blog/a?utm=x&y=1") == "/blog/a", "쿼리스트링을 안 버렸다"
    assert _landing_path("/blog/a#section") == "/blog/a", "프래그먼트를 안 버렸다"
    assert _landing_path("") == "/", "빈 값은 루트로"
    from urllib.parse import urlsplit
    assert _landing_path("/x/y?q=1") == urlsplit("https://site.com/x/y?q=1").path, \
        "GA4 landing_page 와 urlsplit(gsc_page).path 가 어긋난다 — 교차가 깨진다"

    # 2. 속성 제안 — 도메인과 겹치는 것만, 확정은 하지 않는다.
    props = [{"account": "A", "id": "111", "name": "example.com - GA4"},
             {"account": "A", "id": "222", "name": "다른회사"}]
    got = suggest_property(props, "www.example.com")
    assert [p["id"] for p in got] == ["111"], got
    assert suggest_property(props, "") == [], "빈 도메인이 아무거나 후보로 낸다"

    home = Path(tempfile.mkdtemp(prefix="seo-miner-ga4-selftest-"))
    os.environ["CAPTURE_HOME"] = str(home)

    conn = db.connect()
    conn.execute("INSERT INTO projects(name, domain, locale, ga4_property) "
                 "VALUES('g4', 'g4.com', 'ko-KR', '999')")
    conn.commit()

    # 3. 속성 미연결 프로젝트는 조용히 건너뛴다 (다른 단계를 막지 않는다).
    conn.execute("INSERT INTO projects(name, domain, locale) VALUES('noga4', 'x.com', 'ko-KR')")
    conn.commit()

    class _Boom:
        def properties(self):
            raise AssertionError("속성 미연결인데 API 를 건드렸다")

    res = collect("noga4", conn=conn, data_svc=_Boom())
    assert (res.ok, res.skipped) == (False, True), res
    assert "ga4_property" in res.reason or "연결" in res.reason, res.reason

    # 4. 정상 경로 — dimensionValues/metricValues 응답 모양, METRICS 순서와의 정합.
    class _Req:
        def __init__(self, body):
            self.body = body

        def execute(self):
            return {"rows": [
                {"dimensionValues": [{"value": "/a"}],
                 "metricValues": [{"value": "10"}, {"value": "2"}, {"value": "5.5"}, {"value": "0.3"}]},
                {"dimensionValues": [{"value": "/b?utm=x"}],
                 "metricValues": [{"value": "20"}, {"value": "0"}, {"value": "0"}, {"value": "0.9"}]},
            ]}

    class _Props:
        def runReport(self, property, body):
            assert property == "properties/999", property
            assert body["dimensions"] == [{"name": "landingPage"}], body
            assert [m["name"] for m in body["metrics"]] == list(METRICS), body
            return _Req(body)

    class _Svc:
        def properties(self):
            return _Props()

    res = collect("g4", days=14, conn=conn, data_svc=_Svc())
    assert (res.ok, res.skipped, res.rows) == (True, False, 2), res

    pid = conn.execute("SELECT id FROM projects WHERE name='g4'").fetchone()["id"]
    saved = {r["landing_page"]: dict(r) for r in
             conn.execute("SELECT * FROM ga4_snapshots WHERE project_id=?", (pid,))}
    assert set(saved) == {"/a", "/b"}, saved            # 쿼리스트링이 정규화됐다
    assert saved["/a"]["sessions"] == 10 and saved["/a"]["key_events"] == 2.0, saved["/a"]
    assert saved["/a"]["total_revenue"] == 5.5 and saved["/a"]["bounce_rate"] == 0.3, saved["/a"]
    assert saved["/a"]["period_days"] == 14, saved["/a"]

    run_row = conn.execute("SELECT api_calls, notes FROM runs WHERE project_id=? AND kind='ga4'",
                           (pid,)).fetchone()
    assert run_row["api_calls"] == 1 and "rows=2" in run_row["notes"], dict(run_row)

    # 5. 같은 날 재수집은 델리트 후 인서트 — 중복이 쌓이지 않는다.
    res2 = collect("g4", conn=conn, data_svc=_Svc())
    assert res2.rows == 2
    assert conn.execute("SELECT COUNT(*) c FROM ga4_snapshots WHERE project_id=?",
                        (pid,)).fetchone()["c"] == 2, "같은 날 재수집이 중복을 쌓았다"

    # 6. dry-run 은 호출도 저장도 없다.
    conn.execute("DELETE FROM runs")
    conn.commit()
    res3 = collect("g4", dry_run=True, conn=conn, data_svc=_Boom())
    assert (res3.ok, res3.skipped) == (True, True), res3
    assert conn.execute("SELECT * FROM runs").fetchall() == [], "dry-run 이 runs 를 남겼다"

    conn.execute("SELECT 1")     # 빌린 conn 은 러너가 닫지 않는다
    conn.close()
    print("collect_ga4 self-check ok")


if __name__ == "__main__":
    main()
