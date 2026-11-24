#!/usr/bin/env python3
"""GA4 를 안 쓰는 사이트의 방문 분석 출처 — PostHog · Matomo.

ga4 단계 안의 **출처 분기**다. 새 단계가 아니다: collect_ga4.collect 가 GA4 속성이 없을
때만 여기를 부르고, 결과는 ga4_snapshots·ga4_breakdown·ga4_ai_referrals 에 GA4 와 같은
꼴로 들어간다(읽는 쪽 — 판정·화면·요청문 — 은 한 글자도 안 바뀐다). 어디서 왔는지는
ga4_snapshots.source / ga4_breakdown.source 가 말한다(NULL = GA4).

키가 없으면 None 을 돌려주고 collect_ga4 는 예전 안내로 건너뛴다. 키 이름의 정본은 아래
SOURCES 다(doctor 가 이걸 읽는다). 둘 다 무료 API 라 is_paid 단계가 아니다.

**이 출처들이 모르는 것은 NULL 로 둔다.** 전환(key_events)·매출은 PostHog·Matomo 에
GA4 의 '키 이벤트' 같은 한 벌의 정의가 없다 — 0 으로 채우면 "전환 0"이라는 거짓 사실이
되고 scoring.zero_conversion_pages 가 멀쩡한 페이지를 기회로 세운다.

── PostHog (HogQL 을 Query API 로 직접) ────────────────────────────────────
  문서: https://posthog.com/docs/api/queries · https://posthog.com/docs/sql
        https://posthog.com/docs/data/sessions · https://posthog.com/docs/data/channel-type
  · POST {host}/api/projects/{project_id}/query/  본문 {"query": {"kind": "HogQLQuery",
    "query": "..."}, "name": "..."}  인증 `Authorization: Bearer <personal API key>`
    (권한: Query Read). 호스트는 US `https://us.posthog.com`, EU `https://eu.posthog.com`.
  · 응답 {"results": [[...], ...], "columns": [...]} — 행은 배열이다.
  · LIMIT 을 안 쓰면 100행, 쓰면 5만 행까지. OFFSET 은 개인 키로 400 이다(그래서 한 번에 받는다).
  · sessions 표의 세션 속성: $entry_pathname · $entry_current_url · $channel_type(유기 검색은
    'Organic Search') · $is_bounce · $session_duration · $pageview_count · $start_timestamp ·
    $entry_referring_domain.
  · 값 바인딩 문서가 없다 — 쿼리에 끼우는 값(날짜·도메인·호스트)은 여기서 꼴을 검증한다.
  확인 못 한 것(그래서 안 한다): 기기·국가 분해(이벤트 속성을 세션 채널과 잇는 `session.`
  문법이 공식 문서에 없다), 신규/재방문. 이 축들은 notes 에 skip 으로 남는다.
  참여율: 1 - 이탈률. PostHog 의 이탈은 "one pageview, no autocaptures, and spent less than
  10 seconds" 다(https://posthog.com/docs/web-analytics/dashboard) — GA4 engagementRate 와
  정의가 비슷하지만 같지는 않다.

── Matomo (Reporting API) ───────────────────────────────────────────────────
  문서: https://developer.matomo.org/api-reference/reporting-api
        https://developer.matomo.org/api-reference/reporting-api-segmentation
        https://matomo.org/faq/general/faq_114/
  응답 꼴은 공개 데모(demo.matomo.cloud, Matomo 6.1)를 실제로 불러 확인했다(2026-11-24):
  · {base}/index.php?module=API&method=…&idSite=…&period=range&date=시작,끝&format=JSON
    token_auth 는 POST 본문으로("recommended … POST method with the token_auth in the body").
    filter_limit 기본 100, -1 이면 전부. format_metrics=0 이면 비율이 숫자로 온다.
  · Actions.getEntryPageUrls(flat=1) 행: label('/cart/'), url(전체 주소), entry_nb_visits,
    entry_bounce_count, entry_sum_visit_length, entry_nb_actions.
  · 유기 검색: segment=referrerType==search (값: direct, search, website, campaign, ai).
    AND 는 ';'. 기기: deviceType==desktop|smartphone|tablet.
  · 오류는 200 에 {"result": "error", "message": "…"} 로 온다. 세그먼트 실시간 처리를 끈
    서버는 처음 보는 세그먼트에 "has not yet been created in the Segment Editor" 를 준다 —
    그때는 건너뛰고 무엇을 만들어야 하는지 말한다.
  · 사이트 번호: SitesManager.getSitesIdFromSiteUrl(url) → [{"idsite": 1}].
  확인 못 한 것(그래서 안 한다): AI 유입(Matomo 5 가 AI 를 referrerType==ai 로 따로 갈라
  referrerName 이 호스트인지 서비스 이름인지 문서에 없다), 국가×페이지·신규/재방문 분해,
  랜딩 페이지별 전환(행의 goals.*.nb_conversions_entry 는 뜻을 문서로 못 확인했다).

Usage:
  python analytics_alt.py            # self-check (고정 응답 — 실호출 없음)
"""
from __future__ import annotations

import os
import re
import sys
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import quote, urlsplit

sys.path.insert(0, str(Path(__file__).parent))

# 출처 명부 — 키 이름·순서의 정본. 앞에 있는 것이 먼저다(둘 다 있으면 PostHog).
# doctor(diagnose 의 keys)와 test_seams 가 이 표를 읽는다. 여기 말고 키 이름을 적지 않는다.
SOURCES = (
    {"id": "posthog", "name": "PostHog",
     "keys": ("POSTHOG_PERSONAL_API_KEY", "POSTHOG_PROJECT_ID"),
     # 기본은 US 클라우드. EU·자체 호스팅이면 주소를 넣는다.
     "optional": ("POSTHOG_HOST",), "url": "https://posthog.com/docs/api/personal-api-keys"},
    {"id": "matomo", "name": "Matomo",
     "keys": ("MATOMO_URL", "MATOMO_TOKEN"),
     # 사이트 번호는 도메인으로 찾는다. 못 찾을 때만 넣는다.
     "optional": ("MATOMO_SITE_ID",), "url": "https://matomo.org/faq/general/faq_114/"},
)
POSTHOG_DEFAULT_HOST = "https://us.posthog.com"
POSTHOG_LIMIT = 50000       # 개인 키로 한 번에 받을 수 있는 최대 행("up to 50k rows per query")
ORGANIC_CHANNEL = "Organic Search"            # PostHog $channel_type 값 — GA4 와 같은 글자다
MATOMO_ORGANIC = "referrerType==search"
# Matomo deviceType 세그먼트 값 → GSC 표기(ga4_breakdown 의 device 값).
MATOMO_DEVICES = (("desktop", "DESKTOP"), ("smartphone", "MOBILE"), ("tablet", "TABLET"))
TIMEOUT = 60

_DOMAIN = re.compile(r"^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$")


class AltError(Exception):
    """출처가 준 오류 한 건 — 사람이 읽을 문장."""


def configured(env=None) -> dict | None:
    """키가 다 있는 첫 출처(SOURCES 순서). 없으면 None."""
    env = os.environ if env is None else env
    for s in SOURCES:
        if all((env.get(k) or "").strip() for k in s["keys"]):
            return s
    return None


def _post(url: str, *, headers: dict | None = None, json_body=None, form=None):
    """(HTTP 상태, 본문 JSON 또는 글자) — 검사는 이 자리를 갈아 끼운다(실호출 금지)."""
    import requests
    try:
        r = requests.post(url, headers=headers, json=json_body, data=form, timeout=TIMEOUT)
    except requests.RequestException as e:       # 연결 실패도 사유 한 줄이다(단계를 안 죽인다)
        raise AltError(f"연결 실패: {type(e).__name__}") from None
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, r.text[:300]


def _row(dims, metrics) -> dict:
    """GA4 runReport 행의 꼴 — collect_ga4._merge_landing 을 그대로 쓰려고 맞춘다."""
    return {"dimensionValues": [{"value": str(d)} for d in dims],
            "metricValues": [{"value": str(float(m or 0))} for m in metrics]}


def _bare(domain: str) -> str:
    d = (domain or "").strip().lower()
    d = re.sub(r"^https?://", "", d).split("/", 1)[0]
    return d[4:] if d.startswith("www.") else d


# ── PostHog ──────────────────────────────────────────────────────────────────
def posthog_queries(domain: str, start: date, end: date, ai_hosts) -> dict[str, str]:
    """HogQL 세 벌(유기 스냅샷·전 채널 세션·AI 유입). 끼우는 값은 전부 꼴을 검증한 것이다."""
    d = _bare(domain)
    if not _DOMAIN.match(d):
        raise AltError(f"도메인 꼴이 아닙니다: {domain!r}")
    hosts = [h for h in (x.strip().lower() for x in ai_hosts) if _DOMAIN.match(h)]
    # 끝 날짜까지 포함(GA4 의 endDate 와 같게) — 다음 날 0시 앞까지.
    when = (f"$start_timestamp >= toDateTime('{start:%Y-%m-%d} 00:00:00') AND "
            f"$start_timestamp < toDateTime('{end + timedelta(days=1):%Y-%m-%d} 00:00:00')")
    # 한 PostHog 프로젝트가 여러 사이트를 받을 수 있다 — 이 사이트로 들어온 세션만.
    site = (f"($entry_current_url LIKE '%://{d}/%' OR $entry_current_url LIKE '%://www.{d}/%')")
    q = {
        "main": ("SELECT $entry_pathname AS path, count() AS sessions, "
                 "countIf($is_bounce) AS bounces, sum($session_duration) AS duration, "
                 "sum($pageview_count) AS pageviews FROM sessions "
                 f"WHERE {when} AND {site} AND $channel_type = '{ORGANIC_CHANNEL}' "
                 f"GROUP BY path ORDER BY sessions DESC LIMIT {POSTHOG_LIMIT}"),
        "all": ("SELECT $entry_pathname AS path, count() AS sessions FROM sessions "
                f"WHERE {when} AND {site} "
                f"GROUP BY path ORDER BY sessions DESC LIMIT {POSTHOG_LIMIT}"),
    }
    if hosts:
        likes = " OR ".join(f"$entry_referring_domain LIKE '%{h}'" for h in hosts)
        q["ai"] = ("SELECT $entry_referring_domain AS source, $entry_pathname AS path, "
                   "count() AS sessions FROM sessions "
                   f"WHERE {when} AND {site} AND ({likes}) "
                   f"GROUP BY source, path ORDER BY sessions DESC LIMIT {POSTHOG_LIMIT}")
    return q


def fetch_posthog(env, domain: str, start: date, end: date, ai_hosts, post, fail) -> dict:
    host = (env.get("POSTHOG_HOST") or POSTHOG_DEFAULT_HOST).strip().rstrip("/")
    pid = str(env.get("POSTHOG_PROJECT_ID") or "").strip()
    if not re.fullmatch(r"[\w-]+", pid):
        raise AltError("POSTHOG_PROJECT_ID 가 프로젝트 번호 꼴이 아닙니다")
    url = f"{host}/api/projects/{pid}/query/"
    headers = {"Authorization": f"Bearer {env['POSTHOG_PERSONAL_API_KEY'].strip()}"}
    calls = 0

    def run(name: str, sql: str) -> list:
        nonlocal calls
        status, body = post(url, headers=headers, json_body={
            "query": {"kind": "HogQLQuery", "query": sql}, "name": f"seo-miner {name}"})
        if status != 200 or not isinstance(body, dict) or not isinstance(body.get("results"), list):
            why = (body.get("detail") or body.get("code")) if isinstance(body, dict) else body
            raise AltError(f"PostHog HTTP {status}: {str(why or '')[:200]}")
        calls += 1
        return body["results"]

    qs = posthog_queries(domain, start, end, ai_hosts)
    main = []
    for path, sessions, bounces, dur, pv in run("organic", qs["main"]):
        n = float(sessions or 0)
        if n <= 0:
            continue
        main.append(_row([path or "/"], [n, 0, 0, 1 - float(bounces or 0) / n,
                                         float(dur or 0) / n, float(pv or 0) / n]))
    out = {"main": main, "all": None, "bd": {}, "ai": None,
           "unsupported": ("device", "country", "newvsreturning")}

    def optional(label: str, name: str, key: str):
        try:
            return run(name, qs[key])
        except AltError as e:
            fail(label, str(e))
            return None

    rows = optional("전체 채널 세션", "all channels", "all")
    if rows is not None:
        out["all"] = [_row([p or "/"], [n]) for p, n in rows]
    if "ai" in qs:
        rows = optional("AI 유입", "ai referrals", "ai")
        if rows is not None:
            out["ai"] = [_row([src or "", p or "/"], [n, 0]) for src, p, n in rows]
    out["calls"] = calls
    return out


# ── Matomo ───────────────────────────────────────────────────────────────────
def _matomo_call(env, post, method: str, **params):
    base = env["MATOMO_URL"].strip().rstrip("/")
    if base.endswith("/index.php"):
        base = base[: -len("/index.php")]
    q = {"module": "API", "method": method, "format": "JSON", **params}
    # 세그먼트는 값만 인코딩한다("The segment value … must be URL encoded") — 통째로 한 번.
    url = base + "/index.php?" + "&".join(f"{k}={quote(str(v), safe='')}" for k, v in q.items())
    status, body = post(url, form={"token_auth": env["MATOMO_TOKEN"].strip()})
    if isinstance(body, dict) and body.get("result") == "error":
        raise AltError(str(body.get("message") or "Matomo 오류")[:300])
    if status != 200 or not isinstance(body, (list, dict)):
        raise AltError(f"Matomo HTTP {status}: {str(body)[:200]}")
    return body


def _matomo_entries(rows, lead=()) -> list[dict]:
    """getEntryPageUrls 행 → GA4 꼴 행(차원: lead… + 경로, 지표: collect_ga4.METRICS 순서)."""
    out = []
    for r in rows if isinstance(rows, list) else []:
        n = float(r.get("entry_nb_visits") or 0)
        if n <= 0:
            continue                             # 들어온 방문이 없는 줄(나가기만 한 페이지)
        path = urlsplit(r["url"]).path if r.get("url") else str(r.get("label") or "")
        if not path.startswith("/"):
            path = "/" + path
        out.append(_row([*lead, path], [
            n, 0, 0, 1 - float(r.get("entry_bounce_count") or 0) / n,
            float(r.get("entry_sum_visit_length") or 0) / n,
            float(r.get("entry_nb_actions") or 0) / n]))
    return out


def _segment_missing(msg: str) -> bool:
    return "Segment Editor" in msg or "segment" in msg.lower() and "pre-processed" in msg


def fetch_matomo(env, domain: str, start: date, end: date, ai_hosts, post, fail) -> dict:
    calls = 0

    def call(method: str, **params):
        nonlocal calls
        got = _matomo_call(env, post, method, **params)
        calls += 1
        return got

    site = str(env.get("MATOMO_SITE_ID") or "").strip()
    if not site:
        found = call("SitesManager.getSitesIdFromSiteUrl", url=f"https://{_bare(domain)}")
        ids = [str(x.get("idsite")) for x in found if isinstance(x, dict) and x.get("idsite")] \
            if isinstance(found, list) else []
        if not ids:
            raise AltError(f"Matomo 에 {_bare(domain)} 로 등록된 사이트가 없습니다 "
                           "(사이트 번호를 MATOMO_SITE_ID 로 직접 넣을 수 있습니다)")
        site = ids[0]
    if not site.isdigit():
        raise AltError("MATOMO_SITE_ID 가 숫자가 아닙니다")
    common = {"idSite": site, "period": "range", "date": f"{start},{end}",
              "flat": 1, "filter_limit": -1, "format_metrics": 0}
    try:
        rows = call("Actions.getEntryPageUrls", segment=MATOMO_ORGANIC, **common)
    except AltError as e:
        if _segment_missing(str(e)):
            raise AltError(
                f"Matomo 가 유기 검색 세그먼트({MATOMO_ORGANIC})를 아직 처리하지 않았습니다. "
                "Matomo 의 세그먼트 편집기에서 '리퍼러 유형 = 검색 엔진' 세그먼트를 하나 "
                "만들어 두면 몇 시간 뒤부터 수집됩니다") from None
        raise
    out = {"main": _matomo_entries(rows), "all": None, "bd": {}, "ai": None,
           "unsupported": ("country", "newvsreturning", "ai_ref")}
    try:
        allr = call("Actions.getEntryPageUrls", **common)
        out["all"] = [_row([x["dimensionValues"][0]["value"]], [x["metricValues"][0]["value"]])
                      for x in _matomo_entries(allr)]
    except AltError as e:
        fail("전체 채널 세션", str(e))
    dev_rows: list[dict] = []
    for seg, code in MATOMO_DEVICES:
        try:
            got = call("Actions.getEntryPageUrls",
                       segment=f"{MATOMO_ORGANIC};deviceType=={seg}", **common)
        except AltError as e:
            # 세그먼트 미처리는 기기마다 똑같이 난다 — 한 번만 적고 이 축을 접는다.
            fail("device 분해", str(e) if not _segment_missing(str(e)) else
                 "Matomo 가 기기 세그먼트를 아직 처리하지 않았습니다")
            dev_rows = None
            break
        for x in _matomo_entries(got, lead=(code,)):
            m = x["metricValues"]
            dev_rows.append({"dimensionValues": x["dimensionValues"],
                             "metricValues": [m[0], m[1], m[2], m[3]]})
    if dev_rows is not None:
        out["bd"]["device"] = dev_rows
    out["calls"] = calls
    return out


FETCHERS = {"posthog": fetch_posthog, "matomo": fetch_matomo}


# ── ga4 단계가 부르는 자리 ───────────────────────────────────────────────────
def collect(st, *, days: int, scope=(), env=None, post=None):
    """GA4 속성이 없는 사이트를 대체 출처로 수집한다. 키가 없으면 None(부른 쪽이 건너뛴다).

    st 는 collect_ga4 가 연 collector.Stage 그대로다 — 같은 run('ga4') 줄에 적는다.
    """
    env = os.environ if env is None else env
    src = configured(env)
    if not src:
        return None
    import collect_ga4
    import db
    import scoring

    conn, p = st.conn, st.project
    end = date.today() - timedelta(days=3)       # GA4·GSC 와 같은 버퍼(collect_ga4 docstring)
    start = end - timedelta(days=days)
    print(f"[ga4] GA4 속성이 없어 {src['name']} 에서 수집합니다  window {start} ~ {end} "
          f"(period={days}d)")
    if st.dry_run:
        return st.noop(rows=0)
    hosts = collect_ga4.ai_referrers()

    def fail(label: str, msg: str) -> None:
        st.fail(f"수집을 건너뜁니다 ({msg}). 본체 스냅샷은 그대로 저장됩니다.",
                item=label, kind="AltError")

    try:
        got = FETCHERS[src["id"]](env, p["domain"] or "", start, end, hosts,
                                  post or _post, fail)
    except AltError as e:
        return st.skip(f"{src['name']} 수집을 건너뜁니다: {e}")

    keep = (lambda rows, i: [r for r in rows
                             if scoring.in_scope(r["dimensionValues"][i]["value"], scope)]) \
        if scope else (lambda rows, i: rows)
    lp = collect_ga4._landing_path
    main = collect_ga4._merge_landing(keep(got["main"], 0), [lp], collect_ga4.METRICS,
                                      collect_ga4.N_ADDITIVE)
    sessions_all = {}
    if got["all"] is not None:
        m = collect_ga4._merge_landing(keep(got["all"], 0), [lp], ("sessions",), 1)
        sessions_all = {k[0]: v[0] for k, v in m.items()}
    bd = [(dim, collect_ga4._merge_landing(keep(rows, 1), [lambda v: v, lp],
                                           collect_ga4.BREAKDOWN_METRICS,
                                           collect_ga4.BD_N_ADDITIVE))
          for dim, rows in got["bd"].items()]
    ai = None
    if got["ai"] is not None:
        kept = [r for r in keep(got["ai"], 1)
                if collect_ga4.ai_host(r["dimensionValues"][0]["value"], hosts)]
        ai = collect_ga4._merge_landing(kept, [lambda v: collect_ga4.ai_host(v, hosts), lp],
                                        collect_ga4.AI_REFERRAL_METRICS,
                                        len(collect_ga4.AI_REFERRAL_METRICS))
    snap = str(date.today())
    with st.record("ga4") as r:
        db.write_ga4_snapshot(conn, p["id"], snap, days,
                              ((k, v[0], sessions_all.get(k, v[0]), v[1], v[2], v[3], v[4], v[5])
                               for (k,), v in main.items()))
        for dim, merged in bd:
            db.write_ga4_breakdown(conn, p["id"], snap, days, dim,
                                   ((dv, k, v[0], v[1], v[2], v[3])
                                    for (dv, k), v in merged.items()))
        if ai is not None:
            db.write_ga4_ai_referrals(conn, p["id"], snap, days, hosts,
                                      ((s, k, v[0], v[1]) for (s, k), v in ai.items()))
            conn.execute("UPDATE ga4_ai_referrals SET key_events=NULL "
                         "WHERE project_id=? AND snapshot_date=?", (p["id"], snap))
        # 출처를 적고, 이 출처가 모르는 전환·매출은 NULL 로 되돌린다(모듈 docstring).
        for tbl in ("ga4_snapshots", "ga4_breakdown"):
            conn.execute(f"UPDATE {tbl} SET source=?, key_events=NULL, total_revenue=NULL "
                         "WHERE project_id=? AND snapshot_date=?", (src["id"], p["id"], snap))
        conn.commit()
        r.api_calls = got["calls"]
        done = dict(bd)
        r.notes = (f"rows={len(main)} sessions_all={'skip' if not sessions_all else 'ok'} "
                   + "".join(f"{d}={len(done[d])} " if d in done else f"{d}=skip "
                             for d in collect_ga4.BREAKDOWN_DIMS)
                   + f"ai_ref={'skip' if ai is None else len(ai)} "
                   + f"calls={got['calls']} window={start}~{end} {st.err_note}")
    print(f"saved {src['name']} snapshot {snap} ({len(main)} landing pages) — "
          "전환·매출은 이 출처에서 받지 않습니다")
    return st.verdict(got["calls"], rows=len(main))


# ── 자체점검 — 고정 응답만(실호출 없음) ──────────────────────────────────────
# Matomo 행은 demo.matomo.cloud 의 실제 응답에서 칸 이름을 그대로 가져왔다.
_MATOMO_ROWS = [
    {"label": "/cart/", "nb_visits": 9028, "entry_nb_visits": 200, "entry_nb_actions": 700,
     "entry_sum_visit_length": 60000, "entry_bounce_count": 50,
     "goals": {"idgoal=ecommerceOrder": {"nb_conversions_entry": 319, "revenue_entry": 78952.6}},
     "bounce_rate": 0.25, "url": "https://shop.example/cart/"},
    {"label": "/blog/a", "entry_nb_visits": 100, "entry_nb_actions": 150,
     "entry_sum_visit_length": 9000, "entry_bounce_count": 80,
     "url": "https://shop.example/blog/a?utm=x"},
    {"label": "/exit-only", "nb_visits": 5, "exit_nb_visits": 5},
]
_SEGMENT_ERR = {"result": "error", "message": "The Segment you requested has not yet been "
                "created in the Segment Editor and so the report data has not been pre-processed."}


def _selfcheck() -> None:
    import tempfile
    os.environ["CAPTURE_HOME"] = tempfile.mkdtemp(prefix="sm-alt-")
    import collect_ga4
    import collector
    import db
    import scoring

    # ① 명부 — 키가 다 있어야 켜지고, 순서가 우선순위다
    assert configured({}) is None
    assert configured({"POSTHOG_PERSONAL_API_KEY": "k"}) is None, "키가 반만 있는데 켜졌다"
    ph_env = {"POSTHOG_PERSONAL_API_KEY": "phx_k", "POSTHOG_PROJECT_ID": "12345"}
    mt_env = {"MATOMO_URL": "https://stats.example/", "MATOMO_TOKEN": "tok"}
    assert configured(ph_env)["id"] == "posthog" and configured(mt_env)["id"] == "matomo"
    assert configured({**mt_env, **ph_env})["id"] == SOURCES[0]["id"]
    assert set(FETCHERS) == {s["id"] for s in SOURCES}

    # ② HogQL — 문서에 있는 이름만 쓰고, 끼우는 값은 검증한다
    q = posthog_queries("https://www.Shop.example/", date(2026, 11, 1), date(2026, 11, 7),
                        ["chatgpt.com", "bad' OR 1=1 --"])
    assert "$channel_type = 'Organic Search'" in q["main"] and "FROM sessions" in q["main"]
    assert "toDateTime('2026-11-01 00:00:00')" in q["main"]
    assert "toDateTime('2026-11-08 00:00:00')" in q["main"], "끝 날짜가 창에서 빠졌다"
    assert "'%://shop.example/%'" in q["main"] and "'%://www.shop.example/%'" in q["main"]
    assert "Organic Search" not in q["all"]
    assert "chatgpt.com" in q["ai"] and "1=1" not in q["ai"], "검증 안 된 값이 쿼리에 들어갔다"
    assert "ai" not in posthog_queries("shop.example", date(2026, 11, 1), date(2026, 11, 7), [])
    try:
        posthog_queries("shop.example' OR '1", date(2026, 11, 1), date(2026, 11, 7), [])
        raise AssertionError("도메인 꼴이 아닌데 쿼리를 지었다")
    except AltError:
        pass

    db.connect().close()
    conn = db.connect()
    conn.execute("INSERT INTO projects(name,type,domain) VALUES('alt','saas','shop.example')")
    conn.commit()
    pid = conn.execute("SELECT id FROM projects WHERE name='alt'").fetchone()["id"]

    def run(env, post, scope=()):
        with collector.stage("alt", conn=conn) as st:
            return collect(st, days=28, scope=scope, env=env, post=post)

    # ③ 키 없음 → None (collect_ga4 가 예전 안내로 건너뛴다)
    assert run({}, None) is None

    # ④ PostHog — 고정 응답
    seen = []

    def ph_post(url, *, headers=None, json_body=None, form=None):
        seen.append((url, headers, json_body))
        sql = json_body["query"]["query"]
        assert json_body["query"]["kind"] == "HogQLQuery"
        if "$entry_referring_domain AS source" in sql:
            return 200, {"results": [["chatgpt.com", "/blog/a", 7], ["www.perplexity.ai", "/", 3],
                                     ["notchatgpt.com", "/", 9]], "columns": []}
        if "Organic Search" in sql:
            return 200, {"results": [["/blog/a", 100, 20, 9000, 250], ["/blog/a?x=1", 100, 60, 3000, 150],
                                     ["/kr/b", 10, 10, 0, 10]],
                         "columns": ["path", "sessions", "bounces", "duration", "pageviews"]}
        return 200, {"results": [["/blog/a", 500], ["/kr/b", 40], ["/only-direct", 5]],
                     "columns": ["path", "sessions"]}

    res = run(ph_env, ph_post)
    assert res.ok and not res.skipped, res
    assert seen[0][0] == "https://us.posthog.com/api/projects/12345/query/", seen[0][0]
    assert seen[0][1] == {"Authorization": "Bearer phx_k"}
    rows = {r["landing_page"]: dict(r) for r in conn.execute(
        "SELECT * FROM ga4_snapshots WHERE project_id=?", (pid,))}
    assert set(rows) == {"/blog/a", "/kr/b"}, set(rows)
    a = rows["/blog/a"]
    assert (a["sessions"], a["sessions_all"], a["source"]) == (200, 500, "posthog"), a
    assert a["engagement_rate"] == 0.6 and a["avg_session_duration"] == 60.0, a   # 1 - 80/200
    assert a["pageviews_per_session"] == 2.0, a
    assert a["key_events"] is None and a["total_revenue"] is None, "모르는 전환을 0 으로 적었다"
    ai = {(r["source"], r["landing_page"]): r["sessions"] for r in conn.execute(
        "SELECT * FROM ga4_ai_referrals WHERE project_id=?", (pid,))}
    assert ai == {("chatgpt.com", "/blog/a"): 7, ("perplexity.ai", "/"): 3}, ai
    note = conn.execute("SELECT notes FROM runs WHERE project_id=? ORDER BY id DESC",
                        (pid,)).fetchone()["notes"]
    assert "device=skip" in note and "ai_ref=2" in note and "errors=0" in note, note
    # 읽는 쪽이 그대로 읽는다 — 그리고 전환을 모르는 출처로는 '전환 0' 기회를 안 세운다
    agg = scoring._ga4_agg(conn, pid, str(date.today()))
    assert agg["/blog/a"]["sessions"] == 200 and agg["/blog/a"]["key_events"] is None, agg

    # 범위(scope)는 GA4 와 같은 규칙으로 거른다
    res = run(ph_env, ph_post, scope=["/kr/"])
    got = [r[0] for r in conn.execute("SELECT landing_page FROM ga4_snapshots WHERE project_id=?", (pid,))]
    assert got == ["/kr/b"], got

    # 본체가 죽으면 건너뛰고 사유를 남긴다 / 부수 조회만 죽으면 본체는 저장된다
    res = run(ph_env, lambda *a, **k: (401, {"detail": "Invalid personal API key."}))
    assert res.skipped and "Invalid personal API key" in res.reason, res

    def ph_half(url, *, headers=None, json_body=None, form=None):
        if "Organic Search" in json_body["query"]["query"]:
            return ph_post(url, headers=headers, json_body=json_body)
        return 429, {"code": "api_queries_budget_exceeded"}

    res = run(ph_env, ph_half)
    assert res.ok and len(res.errors) == 2, res
    note = conn.execute("SELECT notes FROM runs WHERE project_id=? ORDER BY id DESC",
                        (pid,)).fetchone()["notes"]
    assert "sessions_all=skip" in note and "ai_ref=skip" in note and "errors=2" in note, note

    # ⑤ Matomo — 고정 응답(칸 이름은 실제 데모 응답 그대로)
    urls = []

    def mt_post(url, *, headers=None, json_body=None, form=None):
        urls.append(url)
        assert form == {"token_auth": "tok"} and "token_auth" not in url, "토큰이 주소에 실렸다"
        if "getSitesIdFromSiteUrl" in url:
            return 200, [{"idsite": 7}]
        if "deviceType%3D%3Dsmartphone" in url:
            return 200, [_MATOMO_ROWS[1]]
        if "deviceType" in url:
            return 200, []
        return 200, list(_MATOMO_ROWS)

    res = run(mt_env, mt_post)
    assert res.ok and not res.skipped, res
    assert urls[0].startswith("https://stats.example/index.php?module=API&method="
                              "SitesManager.getSitesIdFromSiteUrl"), urls[0]
    assert "idSite=7" in urls[1] and "segment=referrerType%3D%3Dsearch" in urls[1], urls[1]
    assert "period=range" in urls[1] and "filter_limit=-1" in urls[1] and "flat=1" in urls[1]
    rows = {r["landing_page"]: dict(r) for r in conn.execute(
        "SELECT * FROM ga4_snapshots WHERE project_id=?", (pid,))}
    assert set(rows) == {"/cart/", "/blog/a"}, set(rows)      # 쿼리스트링 제거, 나가기만 한 줄 제외
    c = rows["/cart/"]
    assert (c["sessions"], c["source"], c["engagement_rate"]) == (200, "matomo", 0.75), c
    assert c["avg_session_duration"] == 300.0 and c["pageviews_per_session"] == 3.5, c
    assert c["key_events"] is None, "뜻을 확인 못 한 goals.*_entry 를 전환으로 적었다"
    dev = [(r["dim_value"], r["landing_page"], r["sessions"], r["source"]) for r in conn.execute(
        "SELECT * FROM ga4_breakdown WHERE project_id=? AND dim='device'", (pid,))]
    assert dev == [("MOBILE", "/blog/a", 100, "matomo")], dev
    assert not conn.execute("SELECT 1 FROM ga4_ai_measured WHERE project_id=? AND snapshot_date=?"
                            " AND hosts_json='x'", (pid, str(date.today()))).fetchone()

    # 세그먼트 미처리 — 본체면 건너뛰고 무엇을 만들지 말한다
    res = run(mt_env, lambda url, **k: (200, [{"idsite": 7}]) if "SitesManager" in url
              else (200, _SEGMENT_ERR))
    assert res.skipped and "세그먼트 편집기" in res.reason, res
    # 사이트를 못 찾음
    res = run(mt_env, lambda url, **k: (200, []))
    assert res.skipped and "MATOMO_SITE_ID" in res.reason, res
    # 기기 세그먼트만 미처리 — 본체는 저장, 실패는 한 건만
    res = run(mt_env, lambda url, **k: (200, _SEGMENT_ERR) if "deviceType" in url
              else mt_post(url, **k))
    assert res.ok and len(res.errors) == 1, res

    # ⑥ collect_ga4 가 실제로 이 길로 온다 — 키가 있으면 수집, 없으면 예전 안내
    old = {k: os.environ.get(k) for s in SOURCES for k in s["keys"]}
    try:
        for k in old:
            os.environ.pop(k, None)
        r0 = collect_ga4.collect("alt", conn=conn)
        assert r0.skipped and "GA4 속성" in r0.reason, r0
        os.environ.update(mt_env)
        mod = collect_ga4.analytics_alt          # 직접 실행이면 이 파일과 다른 모듈 객체다
        real, mod._post = mod._post, mt_post
        try:
            r1 = collect_ga4.collect("alt", conn=conn)
        finally:
            mod._post = real
        assert r1.ok and not r1.skipped and r1.rows == 2, r1
        # 연결이 안 되는 서버 — 예외로 단계를 죽이지 않고 사유를 남긴다(실제 _post 경로)
        os.environ["MATOMO_URL"] = "http://127.0.0.1:9"
        r2 = collect_ga4.collect("alt", conn=conn)
        assert r2.skipped and "연결 실패" in r2.reason, r2
    finally:
        for k, v in old.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
    conn.close()
    print("analytics_alt self-check ok")


if __name__ == "__main__":
    _selfcheck()
