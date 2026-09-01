#!/usr/bin/env python3
"""Sync Google Analytics 4 데이터를 Brain 에 적재 — 클릭 뒤에 무슨 일이 났는지.

GSC 는 클릭까지만 안다. 이 수집기가 그 뒤(세션이 뭘 했는지: 전환·매출·이탈)를
랜딩페이지 축으로 채운다 — GSC 의 page 와 잇는 축이 랜딩페이지이기 때문이다.

지표·차원 이름은 구글 문서로 확인했다(추측 금지, 팀장 지시):
  https://developers.google.com/analytics/devguides/reporting/data/v1/api-schema
  https://developers.google.com/analytics/devguides/reporting/data/v1/exploration-api-schema
  · 차원: landingPage, sessionDefaultChannelGroup(값 'Organic Search'),
    deviceCategory(값 desktop/mobile/tablet 등, 소문자), countryId(ISO-3166-1
    alpha-2, 대문자 — 'country' 차원은 사람이 읽는 나라 이름이라 못 쓴다),
    newVsReturning(값 new/returning/(not set))
  · 지표: sessions, keyEvents — 예전 이름 'conversions' 를 2024-05-06 개명.
    engagementRate(참여율, bounceRate 의 여집합), averageSessionDuration(평균
    세션 길이, 초), screenPageViewsPerSession(세션당 페이지뷰), totalRevenue

**전체 유기 검색(사용자 확정 기준)**: sessionDefaultChannelGroup == 'Organic
Search' 인 세션. 구글뿐 아니라 네이버·빙도 여기 잡힌다 — 국내 사이트는 네이버
비중이 커서 GSC(구글 전용) 클릭과 원래 1:1 이 아니다. 그래서 유기 세션과 전
채널 세션을 **둘 다** 담는다(sessions/sessions_all) — 유기 몫이 얼마나 되는지
화면이 말할 수 있게. bounceRate 는 engagementRate 의 여집합이라 하나만 남긴다
— engagementRate 를 골랐다: 전환 이벤트를 안 심은 사이트도 항상 값이 있다.

**행 수 폭발 방지**: 차원을 한 요청에 다 넣지 않는다(페이지 × 채널 × 기기 ×
국가 × 신규 는 조합 폭발). 대신 GSC 의 gsc_breakdown 과 같은 문법으로 요청을
나눈다 — 메인 스냅샷(landingPage, 유기 필터) 1회 + 전체 세션 보조 1회 + 분해
(device/country/newvsreturning) 각 1회, 총 최대 5회.

랜딩페이지 ↔ GSC page 매칭 규칙 (이 연동에서 제일 조용히 틀리기 쉬운 자리):
  GA4 의 landingPage 차원은 호스트 없는 **경로**로 온다(가끔 쿼리스트링이 붙는다:
  '/blog/a' 또는 '/blog/a?utm=x'). GSC 의 page 는 **절대 URL**이다
  ('https://example.com/blog/a'). 유일한 규칙은:
      GA4 landing_page(저장값) == urlsplit(gsc_page).path
  양쪽 다 쿼리스트링·프래그먼트를 버리고 경로만 남긴다. GA4 쪽은 _landing_path()
  가 적재 시점에 정규화해서 저장한다 — GSC 쪽은 교차 분석(다음 단계)이
  urlsplit(page).path 로 맞춘다. 정규화 규칙의 정본은 이 파일, 이 함수 하나다.
  쿼리스트링만 다른 원본 행이 정규화 후 같은 경로로 뭉칠 수 있어 _merge_landing()
  이 합친다 — 카운트류(세션 등)는 합산, 비율/시간류(참여율 등)는 세션 가중평균.

기기·국가 표기를 GSC 와 맞춘다(교차 분석의 전제):
  device: GA4 deviceCategory 를 대문자로 올리면 GSC 표기(MOBILE/DESKTOP/TABLET)
    와 그대로 맞는다.
  country: GA4 countryId(alpha-2, 'KR')를 GSC 표기(alpha-3 소문자, 'kor')로
    바꾼다 — 변환표는 _ALPHA2_ALPHA3 이 정본이다. 표에 없는 코드는 못 맞춘다는
    뜻이라 소문자 alpha-2 그대로 남긴다(교차에서는 안 붙지만 값은 보존).

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
import remote  # noqa: E402

# runReport 의 metrics 순서 — 응답 row 의 metricValues 인덱스와 이 순서가 맞아야 한다.
# 앞 3개(sessions, keyEvents, totalRevenue)는 합산 가능한 카운트류, 뒤 3개는
# 세션 가중평균으로 합쳐야 하는 비율/시간류다 — _merge_landing() 의 n_additive=3.
METRICS = ("sessions", "keyEvents", "totalRevenue",
           "engagementRate", "averageSessionDuration", "screenPageViewsPerSession")
N_ADDITIVE = 3

BREAKDOWN_METRICS = ("sessions", "keyEvents", "totalRevenue", "engagementRate")
BD_N_ADDITIVE = 3
BREAKDOWN_LIMIT = 10000   # GSC 의 PAGE 와 같은 선택 — 분해는 상위 분포만 보면 된다, 페이지네이션 안 함

# 유기 검색(사용자 확정 기준) 필터 — 모듈 docstring 참조.
ORGANIC_FILTER = {"filter": {"fieldName": "sessionDefaultChannelGroup",
                             "stringFilter": {"value": "Organic Search"}}}

# 분해 축 키 → GA4 차원 API 이름 (exploration-api-schema/api-schema 로 확인).
BREAKDOWN_DIMS = {"device": "deviceCategory", "country": "countryId",
                  "newvsreturning": "newVsReturning"}

# ISO 3166-1 alpha-2(GA4 countryId) → alpha-3 소문자(GSC 표기). 표준 ISO 목록 그대로.
_ALPHA2_ALPHA3 = dict(
    pair.split(":") for pair in """
    AD:and AE:are AF:afg AG:atg AI:aia AL:alb AM:arm AO:ago AQ:ata AR:arg AS:asm AT:aut AU:aus AW:abw AX:ala AZ:aze
    BA:bih BB:brb BD:bgd BE:bel BF:bfa BG:bgr BH:bhr BI:bdi BJ:ben BL:blm BM:bmu BN:brn BO:bol BQ:bes BR:bra BS:bhs BT:btn BV:bvt BW:bwa BY:blr BZ:blz
    CA:can CC:cck CD:cod CF:caf CG:cog CH:che CI:civ CK:cok CL:chl CM:cmr CN:chn CO:col CR:cri CU:cub CV:cpv CW:cuw CX:cxr CY:cyp CZ:cze
    DE:deu DJ:dji DK:dnk DM:dma DO:dom DZ:dza
    EC:ecu EE:est EG:egy EH:esh ER:eri ES:esp ET:eth
    FI:fin FJ:fji FK:flk FM:fsm FO:fro FR:fra
    GA:gab GB:gbr GD:grd GE:geo GF:guf GG:ggy GH:gha GI:gib GL:grl GM:gmb GN:gin GP:glp GQ:gnq GR:grc GS:sgs GT:gtm GU:gum GW:gnb GY:guy
    HK:hkg HM:hmd HN:hnd HR:hrv HT:hti HU:hun
    ID:idn IE:irl IL:isr IM:imn IN:ind IO:iot IQ:irq IR:irn IS:isl IT:ita
    JE:jey JM:jam JO:jor JP:jpn
    KE:ken KG:kgz KH:khm KI:kir KM:com KN:kna KP:prk KR:kor KW:kwt KY:cym KZ:kaz
    LA:lao LB:lbn LC:lca LI:lie LK:lka LR:lbr LS:lso LT:ltu LU:lux LV:lva LY:lby
    MA:mar MC:mco MD:mda ME:mne MF:maf MG:mdg MH:mhl MK:mkd ML:mli MM:mmr MN:mng MO:mac MP:mnp MQ:mtq MR:mrt MS:msr MT:mlt MU:mus MV:mdv MW:mwi MX:mex MY:mys MZ:moz
    NA:nam NC:ncl NE:ner NF:nfk NG:nga NI:nic NL:nld NO:nor NP:npl NR:nru NU:niu NZ:nzl
    OM:omn
    PA:pan PE:per PF:pyf PG:png PH:phl PK:pak PL:pol PM:spm PN:pcn PR:pri PS:pse PT:prt PW:plw PY:pry
    QA:qat
    RE:reu RO:rou RS:srb RU:rus RW:rwa
    SA:sau SB:slb SC:syc SD:sdn SE:swe SG:sgp SH:shn SI:svn SJ:sjm SK:svk SL:sle SM:smr SN:sen SO:som SR:sur SS:ssd ST:stp SV:slv SX:sxm SY:syr SZ:swz
    TC:tca TD:tcd TF:atf TG:tgo TH:tha TJ:tjk TK:tkl TL:tls TM:tkm TN:tun TO:ton TR:tur TT:tto TV:tuv TW:twn TZ:tza
    UA:ukr UG:uga UM:umi US:usa UY:ury UZ:uzb
    VA:vat VC:vct VE:ven VG:vgb VI:vir VN:vnm VU:vut
    WF:wlf WS:wsm
    YE:yem YT:myt
    ZA:zaf ZM:zmb ZW:zwe
    """.split()
)


def _landing_path(value: str) -> str:
    """GA4 landingPage 값을 경로만 남긴 정규형으로 — 매칭 규칙의 정본(모듈 docstring 참조)."""
    path = (value or "").split("?", 1)[0].split("#", 1)[0]
    return path or "/"


def _country_code(country_id: str) -> str:
    """GA4 countryId(alpha-2) → GSC 표기(alpha-3 소문자). 못 찾으면 alpha-2 소문자로 보존."""
    v = (country_id or "").upper()
    return _ALPHA2_ALPHA3.get(v, v.lower())


def _device_code(device_category: str) -> str:
    """GA4 deviceCategory → GSC 표기(대문자). 'desktop' → 'DESKTOP'."""
    return (device_category or "").upper()


def _merge_landing(raw_rows, dim_key_fns, metric_names, n_additive) -> dict:
    """dimensionValues 를 dim_key_fns 로 그룹 키를 만들어 합친다.

    GA4 landingPage 는 쿼리스트링이 붙은 채 올 수 있어, 정규화 후 같은 키로
    여러 원본 행이 뭉칠 수 있다. metric_names 의 앞 n_additive 개는 합산
    (세션·전환·매출류), 나머지는 세션(metric_names[0]) 가중평균(비율·시간류).

    반환: {key_tuple: [metric값...]} — key_tuple 은 dim_key_fns 순서.
    """
    merged: dict[tuple, list[float]] = {}
    weight: dict[tuple, float] = {}
    for row in raw_rows:
        key = tuple(f(row["dimensionValues"][i]["value"]) for i, f in enumerate(dim_key_fns))
        vals = [float(row["metricValues"][i]["value"]) for i in range(len(metric_names))]
        sess = vals[0]
        acc = merged.setdefault(key, [0.0] * len(metric_names))
        for i, v in enumerate(vals):
            acc[i] += v * sess if i >= n_additive else v
        weight[key] = weight.get(key, 0.0) + sess
    for key, w in weight.items():
        if w:
            for i in range(n_additive, len(metric_names)):
                merged[key][i] /= w
    return merged


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


def _run_report(data_svc, prop_id: str, body: dict) -> dict:
    return data_svc.properties().runReport(property=f"properties/{prop_id}", body=body).execute()


def collect(project: str, *,
            dry_run: bool = False,
            days: int | None = None,
            row_limit: int = 100000,
            conn=None,
            data_svc=None) -> collector.StageResult:
    """GA4 실적(세션·키 이벤트·매출·참여율·체류·깊이, 랜딩페이지별)을 Brain 에 적재한다.
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
        n_bd = len(BREAKDOWN_DIMS)
        print(f"[ga4] properties/{prop_id}  window {start} ~ {end} (period={days}d)")
        print(f"[ga4] API 호출 계획: 유기 스냅샷 1회 + 전체 세션 1회 + "
              f"분해 {n_bd}회({', '.join(BREAKDOWN_DIMS)}) = 최대 {2 + n_bd}회")
        if st.dry_run:
            return st.noop(rows=0)

        if data_svc is None:
            try:
                data_svc, _ = get_service()
            except db.ProjectNotFound as e:
                return st.skip(str(e))

        from googleapiclient.errors import HttpError

        date_range = {"startDate": str(start), "endDate": str(end)}

        try:
            resp = _run_report(data_svc, prop_id, {
                "dateRanges": [date_range],
                "dimensions": [{"name": "landingPage"}],
                "metrics": [{"name": m} for m in METRICS],
                "dimensionFilter": ORGANIC_FILTER,
                "limit": row_limit})
        except HttpError as e:
            if getattr(e.resp, "status", None) == 403:
                return st.skip(
                    f"GA4 접근 권한이 없습니다(속성 {prop_id}) — 이 구글 계정이 그 속성의 "
                    "뷰어 이상 권한을 가졌는지 확인하거나, GSC 로그인 뒤에 GA4 스코프가 "
                    f"추가됐으니 재로그인이 필요할 수 있습니다: {db.gsc_token()} 를 지우고 "
                    "채팅에 \"GSC 로그인해줘\"")
            raise

        raw_rows = resp.get("rows", []) or []
        print(f"[ga4] fetched {len(raw_rows)} landing page rows (organic)")
        if len(raw_rows) >= row_limit:
            print(f"[주의] --row-limit({row_limit})에 걸려 잘렸을 수 있습니다 — "
                  "더 필요하면 값을 올리세요.")

        main = _merge_landing(raw_rows, [_landing_path], METRICS, N_ADDITIVE)

        # 부수 호출들 — 실패해도 메인 스냅샷은 그대로 저장한다(GSC 의 _optional 과 같은 원칙).
        calls = 1

        def _optional(label: str, body: dict):
            nonlocal calls
            try:
                r = _run_report(data_svc, prop_id, body)
            except HttpError as e:
                print(f"[경고] {label} 수집을 건너뜁니다 ({e}) — "
                      "본체 스냅샷은 그대로 저장됩니다.", file=sys.stderr)
                return None
            calls += 1
            return r.get("rows", []) or []

        all_rows = _optional("전체 채널 세션", {
            "dateRanges": [date_range], "dimensions": [{"name": "landingPage"}],
            "metrics": [{"name": "sessions"}], "limit": row_limit})
        sessions_all = {}
        if all_rows is not None:
            m = _merge_landing(all_rows, [_landing_path], ("sessions",), 1)
            sessions_all = {k[0]: v[0] for k, v in m.items()}
            print(f"[ga4] fetched {len(all_rows)} landing page rows (all channels)")

        bd_results: list[tuple[str, dict]] = []
        for dim, ga4_dim in BREAKDOWN_DIMS.items():
            brows = _optional(f"{dim} 분해", {
                "dateRanges": [date_range],
                "dimensions": [{"name": ga4_dim}, {"name": "landingPage"}],
                "metrics": [{"name": m} for m in BREAKDOWN_METRICS],
                "dimensionFilter": ORGANIC_FILTER, "limit": BREAKDOWN_LIMIT})
            if brows is None:
                continue
            norm = {"device": _device_code, "country": _country_code,
                   "newvsreturning": lambda v: v}[dim]
            merged = _merge_landing(brows, [norm, _landing_path], BREAKDOWN_METRICS, BD_N_ADDITIVE)
            bd_results.append((dim, merged))
            print(f"[ga4] fetched {len(brows)} {dim} x landingPage rows")
            if len(brows) >= BREAKDOWN_LIMIT:
                print(f"[주의] {dim} 분해가 {BREAKDOWN_LIMIT:,}행에서 잘렸습니다 — "
                      "노출 상위 분포만 담겼습니다.")

        snap = str(date.today())
        with st.record("ga4") as r:
            db.write_ga4_snapshot(conn, p["id"], snap, days,
                                  ((lp, vals[0], sessions_all.get(lp, vals[0]), vals[1], vals[2],
                                    vals[3], vals[4], vals[5])
                                   for (lp,), vals in main.items()))
            for dim, merged in bd_results:
                db.write_ga4_breakdown(conn, p["id"], snap, days, dim,
                                       ((dv, lp, vals[0], vals[1], vals[2], vals[3])
                                        for (dv, lp), vals in merged.items()))
            r.api_calls = calls
            r.notes = (f"rows={len(main)} "
                       f"sessions_all={'skip' if not sessions_all else 'ok'} "
                       + "".join(f"{d}={len(m)} " for d, m in bd_results)
                       + "".join(f"{d}=skip " for d in BREAKDOWN_DIMS
                                 if d not in dict(bd_results))
                       + f"calls={calls} window={start}~{end}")

        print(f"saved ga4 snapshot {snap} ({len(main)} landing pages)")
        return st.done(rows=len(main))


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
        if remote.dispatch(a, "ga4"):   # 원격 사이트면 서버가 돈다
            return
        r = collect(a.project, dry_run=a.dry_run, days=a.days, row_limit=a.row_limit)
    except db.ProjectNotFound as e:
        sys.exit(str(e))
    if not r.ok and r.reason:
        sys.exit(r.reason)


def _selfcheck() -> None:
    """가짜 data_svc 로 매칭 규칙·유기 필터·분해·표기 변환을 검증한다 — 진짜 Brain·API 안 건드림."""
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

    # 1b. 기기·국가 표기가 GSC 와 맞는지.
    assert _device_code("mobile") == "MOBILE", _device_code("mobile")
    assert _device_code("desktop") == "DESKTOP", _device_code("desktop")
    assert _country_code("KR") == "kor", _country_code("KR")
    assert _country_code("US") == "usa", _country_code("US")
    assert _country_code("zz") == "zz", "모르는 코드는 보존해야 하는데 사라졌다"

    # 2. 속성 제안 — 도메인과 겹치는 것만, 확정은 하지 않는다.
    props = [{"account": "A", "id": "111", "name": "example.com - GA4"},
             {"account": "A", "id": "222", "name": "다른회사"}]
    got = suggest_property(props, "www.example.com")
    assert [p["id"] for p in got] == ["111"], got
    assert suggest_property(props, "") == [], "빈 도메인이 아무거나 후보로 낸다"

    # 3. 정규화 후 같은 경로로 뭉치는 원본 행 — 카운트는 합산, 비율은 세션 가중평균.
    dup_rows = [
        {"dimensionValues": [{"value": "/a?utm=x"}],
         "metricValues": [{"value": "10"}, {"value": "1"}, {"value": "5"},
                          {"value": "0.8"}, {"value": "60"}, {"value": "2"}]},
        {"dimensionValues": [{"value": "/a?utm=y"}],
         "metricValues": [{"value": "30"}, {"value": "2"}, {"value": "15"},
                          {"value": "0.4"}, {"value": "100"}, {"value": "4"}]},
    ]
    merged = _merge_landing(dup_rows, [_landing_path], METRICS, N_ADDITIVE)
    assert list(merged) == [("/a",)], merged
    sessions, ke, rev, er, dur, ppv = merged[("/a",)]
    assert (sessions, ke, rev) == (40.0, 3.0, 20.0), merged[("/a",)]     # 합산
    assert abs(er - (0.8 * 10 + 0.4 * 30) / 40) < 1e-9, er               # 세션 가중평균
    assert abs(dur - (60 * 10 + 100 * 30) / 40) < 1e-9, dur

    home = Path(tempfile.mkdtemp(prefix="seo-miner-ga4-selftest-"))
    os.environ["CAPTURE_HOME"] = str(home)

    conn = db.connect()
    conn.execute("INSERT INTO projects(name, domain, locale, ga4_property) "
                 "VALUES('g4', 'g4.com', 'ko-KR', '999')")
    conn.commit()

    # 4. 속성 미연결 프로젝트는 조용히 건너뛴다 (다른 단계를 막지 않는다).
    conn.execute("INSERT INTO projects(name, domain, locale) VALUES('noga4', 'x.com', 'ko-KR')")
    conn.commit()

    class _Boom:
        def properties(self):
            raise AssertionError("속성 미연결인데 API 를 건드렸다")

    res = collect("noga4", conn=conn, data_svc=_Boom())
    assert (res.ok, res.skipped) == (False, True), res
    assert "ga4_property" in res.reason or "연결" in res.reason, res.reason

    # 5. 정상 경로 — 유기 필터가 실제로 실리는지, 유기/전체가 따로 담기는지, 분해 세 축.
    class _Req:
        def __init__(self, rows):
            self.rows = rows

        def execute(self):
            return {"rows": self.rows}

    ORGANIC_ROWS = [
        {"dimensionValues": [{"value": "/a"}],
         "metricValues": [{"value": "10"}, {"value": "2"}, {"value": "5.5"},
                          {"value": "0.6"}, {"value": "50"}, {"value": "1.5"}]},
        {"dimensionValues": [{"value": "/b?utm=x"}],
         "metricValues": [{"value": "20"}, {"value": "0"}, {"value": "0"},
                          {"value": "0.3"}, {"value": "20"}, {"value": "1.1"}]},
    ]
    ALL_ROWS = [
        {"dimensionValues": [{"value": "/a"}], "metricValues": [{"value": "40"}]},
        {"dimensionValues": [{"value": "/b"}], "metricValues": [{"value": "90"}]},
    ]
    DEVICE_ROWS = [
        {"dimensionValues": [{"value": "mobile"}, {"value": "/a"}],
         "metricValues": [{"value": "6"}, {"value": "1"}, {"value": "3"}, {"value": "0.5"}]},
        {"dimensionValues": [{"value": "desktop"}, {"value": "/a"}],
         "metricValues": [{"value": "4"}, {"value": "1"}, {"value": "2.5"}, {"value": "0.7"}]},
    ]
    COUNTRY_ROWS = [
        {"dimensionValues": [{"value": "KR"}, {"value": "/a"}],
         "metricValues": [{"value": "9"}, {"value": "2"}, {"value": "5"}, {"value": "0.6"}]},
    ]
    NEWVR_ROWS = [
        {"dimensionValues": [{"value": "new"}, {"value": "/a"}],
         "metricValues": [{"value": "7"}, {"value": "1"}, {"value": "4"}, {"value": "0.5"}]},
    ]

    class _Props:
        def __init__(self):
            self.seen: list[dict] = []

        def runReport(self, property, body):
            assert property == "properties/999", property
            self.seen.append(body)
            dims = [d["name"] for d in body["dimensions"]]
            if dims == ["landingPage"] and "dimensionFilter" in body:
                assert body["dimensionFilter"] == ORGANIC_FILTER, body
                assert [m["name"] for m in body["metrics"]] == list(METRICS), body
                return _Req(ORGANIC_ROWS)
            if dims == ["landingPage"]:                       # 전체 채널(필터 없음)
                assert "dimensionFilter" not in body, "전체 세션 요청에 유기 필터가 섞였다"
                assert [m["name"] for m in body["metrics"]] == ["sessions"], body
                return _Req(ALL_ROWS)
            assert "dimensionFilter" in body, f"{dims} 분해에 유기 필터가 빠졌다"
            assert body["dimensionFilter"] == ORGANIC_FILTER, body
            return _Req({"deviceCategory": DEVICE_ROWS, "countryId": COUNTRY_ROWS,
                        "newVsReturning": NEWVR_ROWS}[dims[0]])

    class _Svc:
        def __init__(self):
            self.props = _Props()

        def properties(self):
            return self.props

    svc = _Svc()
    res = collect("g4", days=14, conn=conn, data_svc=svc)
    assert (res.ok, res.skipped, res.rows) == (True, False, 2), res
    assert len(svc.props.seen) == 5, "유기 1 + 전체 1 + 분해 3 = 5회가 아니다"

    pid = conn.execute("SELECT id FROM projects WHERE name='g4'").fetchone()["id"]
    saved = {r["landing_page"]: dict(r) for r in
             conn.execute("SELECT * FROM ga4_snapshots WHERE project_id=?", (pid,))}
    assert set(saved) == {"/a", "/b"}, saved            # 쿼리스트링이 정규화됐다
    assert saved["/a"]["sessions"] == 10 and saved["/a"]["sessions_all"] == 40, saved["/a"]
    assert saved["/a"]["key_events"] == 2.0 and saved["/a"]["total_revenue"] == 5.5, saved["/a"]
    assert saved["/a"]["engagement_rate"] == 0.6, saved["/a"]
    assert saved["/a"]["avg_session_duration"] == 50.0, saved["/a"]
    assert saved["/a"]["pageviews_per_session"] == 1.5, saved["/a"]
    assert saved["/a"]["period_days"] == 14, saved["/a"]
    assert saved["/b"]["sessions"] == 20 and saved["/b"]["sessions_all"] == 90, saved["/b"]
    assert "bounce_rate" not in saved["/a"], "bounceRate 컬럼을 정리하지 않았다"

    bd = {(r["dim"], r["dim_value"]): dict(r) for r in
          conn.execute("SELECT * FROM ga4_breakdown WHERE project_id=?", (pid,))}
    assert set(bd) == {("device", "MOBILE"), ("device", "DESKTOP"),
                       ("country", "kor"), ("newvsreturning", "new")}, bd
    assert bd[("device", "MOBILE")]["landing_page"] == "/a", bd[("device", "MOBILE")]
    assert bd[("device", "MOBILE")]["sessions"] == 6, bd[("device", "MOBILE")]
    assert bd[("country", "kor")]["sessions"] == 9, bd[("country", "kor")]

    run_row = conn.execute("SELECT api_calls, notes FROM runs WHERE project_id=? AND kind='ga4'",
                           (pid,)).fetchone()
    assert run_row["api_calls"] == 5, dict(run_row)
    assert "device=" in run_row["notes"] and "country=" in run_row["notes"] \
        and "newvsreturning=" in run_row["notes"], run_row["notes"]

    # 6. 같은 날 재수집은 델리트 후 인서트 — 중복이 쌓이지 않는다.
    res2 = collect("g4", conn=conn, data_svc=svc)
    assert res2.rows == 2
    assert conn.execute("SELECT COUNT(*) c FROM ga4_snapshots WHERE project_id=?",
                        (pid,)).fetchone()["c"] == 2, "같은 날 재수집이 중복을 쌓았다"
    assert conn.execute("SELECT COUNT(*) c FROM ga4_breakdown WHERE project_id=?",
                        (pid,)).fetchone()["c"] == 4, "같은 날 재수집이 분해를 중복 적재했다"

    # 7. 분해 호출 하나가 실패해도 본체 스냅샷·나머지 분해는 살아남는다 (부수 호출 원칙).
    # 새 프로젝트로 — 기존 g4 의 지난 device 행이 남아 있어 "안 씀"과 "지워짐"을 못 가른다.
    from googleapiclient.errors import HttpError

    conn.execute("INSERT INTO projects(name, domain, locale, ga4_property) "
                 "VALUES('g5', 'g5.com', 'ko-KR', '999')")
    conn.commit()

    class _FlakyProps(_Props):
        def runReport(self, property, body):
            dims = [d["name"] for d in body["dimensions"]]
            if dims == ["deviceCategory", "landingPage"]:
                raise HttpError(type("R", (), {"status": 500, "reason": "x"})(), b"boom")
            return super().runReport(property, body)

    class _FlakySvc(_Svc):
        def __init__(self):
            self.props = _FlakyProps()

    res3 = collect("g5", conn=conn, data_svc=_FlakySvc())
    assert res3.ok and res3.rows == 2, res3
    pid5 = conn.execute("SELECT id FROM projects WHERE name='g5'").fetchone()["id"]
    bd3 = {r["dim"] for r in conn.execute(
        "SELECT DISTINCT dim FROM ga4_breakdown WHERE project_id=?", (pid5,))}
    assert bd3 == {"country", "newvsreturning"}, "device 분해 실패가 나머지까지 지웠다"

    # 8. dry-run 은 호출도 저장도 없다.
    conn.execute("DELETE FROM runs")
    conn.commit()
    res4 = collect("g4", dry_run=True, conn=conn, data_svc=_Boom())
    assert (res4.ok, res4.skipped) == (True, True), res4
    assert conn.execute("SELECT * FROM runs").fetchall() == [], "dry-run 이 runs 를 남겼다"

    conn.execute("SELECT 1")     # 빌린 conn 은 러너가 닫지 않는다
    conn.close()
    print("collect_ga4 self-check ok")


if __name__ == "__main__":
    main()
