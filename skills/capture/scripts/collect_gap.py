#!/usr/bin/env python3
"""경쟁사 탐지·역키워드·트래픽 몫·Content Gap — DataForSEO Labs (F3-확장).

경쟁사 도메인이 랭킹하는 키워드를 끌어와, 내가 안 잡고 있는 것만 후보로
적재한다 ("경쟁사는 잡는데 나는 부재"). 무료 경로(collect_serp 의 rank 상위
수집)는 SERP를 한 번씩 다시 보는 비용이 들고 얕다 — Labs 는 도메인 한 곳
요청으로 최대 수백 개의 랭킹 키워드를 한 번에 돌려준다.

축이 셋이고, 셋 다 끌 수 있다 (0 = 끔):
  A. 역키워드     ranked_keywords/live      → keywords (source='competitor_gap')
  B. 자동 탐지·몫 competitors_domain/live   → competitors(auto_serp) + competitor_metrics
  C. Content Gap  domain_intersection/live  → keyword_gap (missing|weak|shared)

B 는 "누가 우리와 키워드가 겹치나"를 사람 등록 없이 찾고(Ahrefs 의 Organic
competitors), 같은 응답의 `metrics.organic` 으로 도메인별 유기 규모까지 한 번에
얻는다 — 콜 하나로 둘. 우리 자신도 `is_self=1` 로 같은 표에 넣는다. 몫(share)은
**저장하지 않는다** — 조회 시 `etv / SUM(etv)` 로 계산한다. 저장하면 경쟁사가
하나 늘어 분모가 바뀔 때 어제 적은 몫이 조용히 거짓말이 된다.

C 는 A 가 못 주는 것을 준다. A 는 "경쟁사가 잡은 키워드" 목록일 뿐이라 *우리가
몇 위인지*가 없다. domain_intersection 은 두 도메인의 위치를 같이 줘서
  · missing — 우리는 순위 없음 (`intersections: false` 축)
  · weak    — 둘 다 있는데 우리가 더 아래
  · shared  — 둘 다 있고 우리가 같거나 위
로 가른다. 경쟁사당 2콜이라 `limits.gap_rivals` 로 상한을 둔다.

흐름:
  1) 도메인 결정 — --domain 반복 지정, 생략 시 competitors 테이블 전부.
     상한 5개. 5개 초과는 안내 후 상위 5개만 사용 (사용자가 명시적으로 좁혔을
     가능성을 남겨두려고 잘라낸다).
  2) Labs POST /v3/dataforseo_labs/google/ranked_keywords/live 로 도메인당
     --limit 개 랭킹 키워드를 받는다. 응답 items[].keyword + search_volume.
  3) 필터 — (a) 내 활성/후보 keywords 에 이미 있는 것, (b) 최신 GSC 스냅샷에
     impressions>0으로 잡히는 쿼리는 제외. 남는 것만 "내 부재" 후보.
  4) 적재 — db.add_keyword_candidates(is_active=0, source='competitor_gap',
     locale=projects.locale). Labs 가 search_volume 을 주면 keywords.volume
     에 기록 (실측값이라 '볼륨 창작 금지' 규칙 위반 아님 — 주석 참조).
  5) 실행 전체를 db.run(conn, pid, "competitors") 컨텍스트로 감싸 api_calls·cost 기록.

인증: 기존 DataForSEO 자격 (DATAFORSEO_LOGIN/PASSWORD). collect_serp 와 같은
env 경로 — Labs 도 같은 키가 통한다 (유료 크레딧 차감).

비용 고지 (단가 출처):
  DataForSEO Labs `ranked_keywords/live` — DataForSEO 공식 가격표에서
  "DataForSEO Labs API / Ranked Keywords / Live" 행. 2026-08 기준 단가
  ~$0.001/domain lookup (정확한 청구액은 응답 tasks[].cost 가 알려준다 —
  실청구액을 그대로 runs.cost_estimate_usd 에 적는다).
  출처: https://dataforseo.com/apis/dataforseo-labs-api (Labs API 가격 섹션).

한계:
  · Labs 응답의 keyword 수는 도메인 위젯 크기에 따라 들쭉날쭉 — --limit 은
    "최대"이고 실제 반환은 적을 수 있다.
  · search_volume 은 키워드의 월간 검색 추정치 (Labs 자체 추정). NULL 인
    키워드는 keywords.volume 도 NULL 로 둔다 (창작 금지).
  · 'competitor_gap' 으로 적재된 후보는 큐레이션 전엔 is_active=0 — 즉시
    추적에 들어가지 않는다 (`/capture keywords` 의 승인 흐름을 그대로 탄다).
  · Labs 가 도메인을 못 찾으면 items=[] 가 와서 0건 적재 (실패가 아님).

  · 응답 필드명은 Labs 엔드포인트마다·버전마다 달라진다. 파서는 여러 모양을 다
    받고, 못 읽은 **항목만** 건너뛴다 (한 항목 때문에 나머지를 잃지 않는다).

Usage:
  python collect_gap.py --project NAME [--domain d1.com --domain d2.com]
                        [--limit 100] [--rivals 5] [--intersect 3]
                        [--throttle 0.5] [--dry-run]
  python collect_gap.py                                  # self-check
"""
import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import collector  # noqa: E402
import db  # noqa: E402
import scoring  # noqa: E402
import serp_adapter  # noqa: E402

DOMAIN_CAP = 5

# Labs 엔드포인트 경로 정본 — 문자열을 호출부에 흩지 않는다 (자체점검도 이걸 본다).
LABS_COMPETITORS = "/dataforseo_labs/google/competitors_domain/live"
LABS_INTERSECT = "/dataforseo_labs/google/domain_intersection/live"
LABS_OVERVIEW = "/dataforseo_labs/google/domain_rank_overview/live"

# metrics.organic 의 top10 구간. 응답이 일부만 주면 준 것만 더한다 (없는 구간을 0으로
# 치면 "top10 이 0" 과 "top10 을 안 줬다" 가 같아진다).
TOP10_KEYS = ("pos_1", "pos_2_3", "pos_4_10")


def _num(v, cast):
    """숫자로 읽히면 숫자, 아니면 None. Labs 는 같은 필드를 문자열로 주기도 한다."""
    if v is None or isinstance(v, bool):
        return None
    try:
        return cast(v)
    except (TypeError, ValueError):
        return None


def _sub(d, keys) -> dict | None:
    """d 아래에서 keys 중 처음 발견되는 dict. 필드명이 응답마다 다른 자리에 쓴다."""
    if not isinstance(d, dict):
        return None
    for k in keys:
        if isinstance(d.get(k), dict):
            return d[k]
    return None


def _domain_of(it: dict) -> str:
    """항목이 말하는 도메인. host_of 로 정규화 — 'www.x.com' 과 'x.com' 이 따로 쌓이지 않게."""
    for k in ("domain", "target", "target2", "se_domain"):
        v = it.get(k)
        if isinstance(v, str) and v.strip():
            return scoring.host_of(v)
    return ""


def _organic(it: dict) -> dict | None:
    """metrics.organic -> {keywords, etv, top10}. 하나도 못 읽으면 None.

    모양이 최소 셋이다: {"metrics": {"organic": {...}}} / {"metrics": {...}} /
    {...}(항목이 곧 지표). 셋 다 받는다 — 엔드포인트마다 한 겹씩 다르다.
    """
    org = _sub(it, ("metrics",)) or it
    org = _sub(org, ("organic",)) or org
    if not isinstance(org, dict):
        return None
    kws = _num(org.get("count"), int)
    if kws is None:
        kws = _num(org.get("keywords_count"), int)
    etv = _num(org.get("etv"), float)
    parts = [_num(org.get(k), int) for k in TOP10_KEYS]
    top10 = sum(v for v in parts if v is not None) if any(v is not None for v in parts) else None
    if kws is None and etv is None and top10 is None:
        return None
    return {"keywords": kws, "etv": etv, "top10": top10}


def _kw_of(it: dict) -> tuple[str, int | None]:
    """(keyword, search_volume|None) — fetch_labs_ranked_keywords 와 같은 관용도."""
    kd = _sub(it, ("keyword_data",)) or it
    kw = kd.get("keyword") or it.get("keyword") or ""
    sv = (_sub(kd, ("keyword_info",)) or {}).get("search_volume")
    if sv is None:
        sv = kd.get("search_volume", it.get("search_volume"))
    return (kw if isinstance(kw, str) else "").strip(), _num(sv, int)


def _pos_of(it: dict, which: int) -> int | None:
    """target1(우리)/target2(경쟁사) 의 순위. serp element 이름이 응답마다 다르다."""
    node = _sub(it, (f"{'first' if which == 1 else 'second'}_domain_serp_element",
                     f"target{which}_serp_element",
                     f"domain{which}_serp_element"))
    if node is None:
        return None
    inner = _sub(node, ("serp_item",)) or node
    for k in ("rank_group", "rank_absolute", "position", "rank"):
        v = _num(inner.get(k), int)
        if v is not None:
            return v
    return None


def _kind(our_pos: int | None, rival_pos: int | None) -> str:
    """missing = 우리 부재 / weak = 둘 다 있고 우리가 아래 / shared = 같거나 위."""
    if our_pos is None:
        return "missing"
    if rival_pos is not None and our_pos > rival_pos:
        return "weak"
    return "shared"


def _fetch_competitors(post, target: str, locale: str, limit: int) -> tuple[list[dict], float]:
    """Organic competitors — 우리와 키워드가 겹치는 도메인 + 각자의 유기 지표.

    응답에 우리 자신이 섞여 오는 경우가 있다. 거르지 않고 그대로 돌려준다 —
    누가 '나' 인지는 호출부가 안다 (serp_adapter 와 같은 약속).
    """
    loc, lang, _ = serp_adapter.location(locale)
    result, cost = post(LABS_COMPETITORS, [{
        "target": target, "location_name": loc, "language_code": lang,
        "limit": limit, "order_by": ["intersections,desc"]}])
    out = []
    for r0 in result or []:
        for it in (r0.get("items") or []):
            if not isinstance(it, dict):
                continue
            d = _domain_of(it)
            if d:
                out.append({"domain": d, "metrics": _organic(it)})
    return out, cost


def _fetch_self_metrics(post, target: str, locale: str) -> tuple[dict | None, float]:
    """우리 도메인의 유기 규모 — competitors_domain 응답에 우리가 없을 때의 두 번째 경로."""
    loc, lang, _ = serp_adapter.location(locale)
    result, cost = post(LABS_OVERVIEW, [{
        "target": target, "location_name": loc, "language_code": lang}])
    for r0 in result or []:
        for it in ((r0.get("items") if isinstance(r0, dict) else None) or [r0]):
            m = _organic(it) if isinstance(it, dict) else None
            if m:
                return m, cost
    return None, cost


def _fetch_intersection(post, ours: str, rival: str, locale: str, limit: int,
                        intersections: bool) -> tuple[list[dict], float]:
    """두 도메인의 키워드 교집합. intersections=False 면 '경쟁사만 잡은 것'."""
    loc, lang, _ = serp_adapter.location(locale)
    result, cost = post(LABS_INTERSECT, [{
        "target1": ours, "target2": rival, "location_name": loc, "language_code": lang,
        "limit": limit, "intersections": intersections}])
    out = []
    for r0 in result or []:
        for it in (r0.get("items") or []):
            if not isinstance(it, dict):
                continue
            kw, sv = _kw_of(it)
            if not kw:
                continue        # 키워드를 못 읽은 항목만 버린다
            out.append({"keyword": kw, "volume": sv,
                        "our_position": _pos_of(it, 1), "position": _pos_of(it, 2)})
    return out, cost


def _put_metric(conn, pid: int, day: str, domain: str, is_self: int, m: dict) -> None:
    """competitor_metrics 한 줄. share 는 넣지 않는다 — 분모가 바뀌면 낡기 때문에
    조회 시 `etv / SUM(etv)` 로 계산한다."""
    conn.execute(
        """INSERT INTO competitor_metrics(project_id, checked_date, domain, is_self,
                                          keywords, etv, top10)
             VALUES(?,?,?,?,?,?,?)
           ON CONFLICT(project_id, checked_date, domain) DO UPDATE SET
             is_self=excluded.is_self, keywords=excluded.keywords,
             etv=excluded.etv, top10=excluded.top10""",
        (pid, day, domain, is_self, m.get("keywords"), m.get("etv"), m.get("top10")))


def _put_gap(conn, pid: int, day: str, rival: str, row: dict) -> None:
    conn.execute(
        """INSERT INTO keyword_gap(project_id, checked_date, keyword, domain,
                                   position, our_position, volume, kind)
             VALUES(?,?,?,?,?,?,?,?)
           ON CONFLICT(project_id, checked_date, keyword, domain) DO UPDATE SET
             position=excluded.position, our_position=excluded.our_position,
             volume=excluded.volume, kind=excluded.kind""",
        (pid, day, row["keyword"], rival, row["position"], row["our_position"],
         row["volume"], _kind(row["our_position"], row["position"])))


def _cap(domains: list[str]) -> list[str]:
    """역키워드 대상 상한. 자동 탐지가 붙은 뒤에도 같은 자를 쓴다."""
    if len(domains) <= DOMAIN_CAP:
        return domains
    print(f"[안내] 경쟁사 {len(domains)}개는 상한 {DOMAIN_CAP}개를 초과 — "
          f"앞 {DOMAIN_CAP}개만 사용합니다. 더 정밀하게는 `--domain` 으로 명시하세요.",
          file=sys.stderr)
    return domains[:DOMAIN_CAP]


def _resolve_domains(conn, project_id: int, args_domains: list[str]) -> list[str]:
    if args_domains:
        # 사용자가 명시한 도메인은 그대로 (소문자·공백 정리만).
        out = []
        for d in args_domains:
            d = (d or "").strip().lower()
            if d:
                out.append(d)
        return out
    rows = conn.execute(
        "SELECT domain FROM competitors WHERE project_id=? ORDER BY id", (project_id,)
    ).fetchall()
    return [r["domain"] for r in rows]


def _existing_norms(conn, project_id: int) -> set[str]:
    """내 키워드(norm) — 후보·활성 모두 포함. '이미 있다' 의 기준선."""
    return {scoring.norm(r["keyword"]) for r in
            conn.execute("SELECT keyword FROM keywords WHERE project_id=?",
                         (project_id,)).fetchall()}


def _gsc_seen_norms(conn, project_id: int) -> set[str]:
    """최신 GSC 스냅샷에서 impressions>0 으로 잡힌 쿼리(norm).
    빈 스냅샷이면 공집합 — 필터가 전부 통과한다는 뜻이고, 그 자체로 데이터
    부재 신호이므로 굳이 경고하지 않는다 (gsc 미수집 프로젝트는 흔하다)."""
    latest = conn.execute(
        """SELECT snapshot_date, MAX(period_days) period_days
             FROM gsc_snapshots WHERE project_id=?
            GROUP BY snapshot_date ORDER BY 1 DESC LIMIT 1""",
        (project_id,)).fetchone()
    if not latest:
        return set()
    return {scoring.norm(r["query"]) for r in conn.execute(
        """SELECT query FROM gsc_snapshots
            WHERE project_id=? AND snapshot_date=? AND period_days=? AND impressions>0""",
        (project_id, latest["snapshot_date"], latest["period_days"])).fetchall()}


def _backfill_volumes(conn, project_id: int, items: list[tuple[str, int | None]]) -> int:
    """add_keyword_candidates 가 volume 을 받지 않으므로, INSERT 후에
    search_volume 있는 항목만 keywords.volume 을 UPDATE. NULL 은 그대로.

    items: [(keyword, search_volume|None), ...] — 이미 적재된 후보만 대상으로.
    """
    n = 0
    for kw, sv in items:
        if sv is None:
            continue
        cur = conn.execute(
            "UPDATE keywords SET volume=? WHERE project_id=? AND keyword=? AND volume IS NULL",
            (int(sv), project_id, kw))
        if cur.rowcount:
            n += cur.rowcount
    conn.commit()
    return n


def collect(project: str, *,
            dry_run: bool = False,
            domain: list[str] | None = None,
            limit: int | None = None,
            rivals: int | None = None,
            intersect: int | None = None,
            throttle: float | None = None,
            conn=None,
            fetch=None,
            post=None) -> collector.StageResult:
    """경쟁사를 찾고(B) 역키워드를 캐고(A) 위치 차이를 적재한다(C).

    Args:
        project: 사이트 이름
        dry_run: True 면 호출 계획만 찍고 종료
        domain: 분석할 경쟁사 도메인 (반복 지정 가능). None/빈 리스트면
                competitors 테이블의 도메인 전부. 명시하면 자동 탐지는 꺼진다 —
                사용자가 이미 대상을 정한 것이다.
        limit: 도메인당 키워드 수 상한(config 키는 limits.gap_limit). 기본 100 —
            CLI 플래그(--limit)와 이름을 맞춘다.
        rivals: 자동 탐지할 경쟁사 수. 0이면 자동 탐지 끔.
        intersect: Content Gap 을 돌릴 경쟁사 수 상한. 0이면 끔.
        throttle: 요청 간격(초)
        conn: 이미 열린 Brain 연결 — 주면 그것을 쓰고 닫지 않는다
        fetch: (domain, locale, limit) -> (items, cost) — 주면 Labs 대신 이것을
               부른다 (자체점검이 requests.post 를 갈아끼우던 자리)
        post: (path, body) -> (result, cost) — 주면 serp_adapter.post_dataforseo
              대신 이것을 부른다. 새 축(B·C)의 주입 자리.

    Returns:
        StageResult(ok=...). 유료 키 부재만 ok=False, skipped=True (진짜 못 한
        것). 경쟁사 0건·dry-run 은 ok=True, skipped=True — 체인을 깨지 않는다.
    """
    ap = _parser()
    fetch = fetch or serp_adapter.fetch_labs_ranked_keywords
    post = post or serp_adapter.post_dataforseo
    with collector.stage(project, conn=conn, dry_run=dry_run) as st:
        conn, p = st.conn, st.project
        if not serp_adapter.has_dataforseo():
            return st.skip("Labs 유료 키 필요 — DATAFORSEO_LOGIN/PASSWORD 설정. "
                           "발급: https://dataforseo.com")

        s = st.settings(ap, argparse.Namespace(limit=limit, throttle=throttle,
                                           rivals=rivals, intersect=intersect))
        limit = s["limits.gap_limit"]
        # --domain 으로 좁혔으면 자동 탐지는 끈다 — 대상은 이미 사용자가 정했다.
        n_auto = 0 if domain else s["limits.auto_competitors"]
        n_gap = s["limits.gap_rivals"]
        throttle = st.throttle
        ours = scoring.host_of(p["domain"] or "")

        domains = _cap(_resolve_domains(conn, p["id"], domain))
        if not domains and not n_auto:
            # 경쟁사 부재는 실패가 아니라 아직 적재를 안 한 정상 상태다 (첫 바퀴,
            # 또는 rank harvest 가 3회 이상 잡힌 도메인을 못 찾은 사이트).
            # ok=False 로 돌리면 run_all 체인이 exit 1 로 끝나고 worker.py 의
            # 주간 리포트 메일이 조용히 안 나간다.
            reason = (f"'{p['name']}' 에 등록된 경쟁사가 아직 없습니다 — "
                      f"`/capture rank {p['name']}` 으로 순위를 한 바퀴 돌리면 "
                      "상위 도메인이 자동 적재됩니다. 급하면 프로젝트 yaml 의 "
                      "`competitors_manual` 또는 `--domain` 으로 직접 지정하세요.")
            print(f"[gap] {reason}")
            return st.noop(reason=reason)

        locale = db.project_locale(p)
        # 콜 수: 역키워드(도메인당 1) + 자동탐지 1 + 우리 지표 1 + Gap(경쟁사당 2).
        # 자동 탐지가 경쟁사를 더 붙이면 실제 콜은 이보다 늘 수 있다 — 상한이 아니라 예상치다.
        n_gap_hosts = min(n_gap, max(len(domains), n_auto)) if n_gap and ours else 0
        est_calls = len(domains) + (2 if n_auto else 0) + n_gap_hosts * 2
        est_cost = est_calls * serp_adapter.LABS_COST_PER_CALL
        print(f"[gap] project={p['name']} domains={len(domains)} limit={limit} "
              f"est_cost≈${est_cost:.3f} (~{est_calls * (throttle + 2) / 60:.1f} min)")
        serp_adapter.warn_unmapped(locale)   # 매핑 없는 로케일 경고는 돈을 쓰기 전에 — collect_serp 와 같은 자리

        if st.dry_run:
            for d in domains:
                print(f"  - {d}")
            print("  자동 경쟁사 탐지: " + (
                f"켜짐 — competitors_domain/live 1콜(limit={n_auto}) + 우리 지표 1콜 "
                f"→ competitors(source='auto_serp') · competitor_metrics"
                if n_auto else "꺼짐 (--rivals 0 또는 --domain 지정)"))
            print("  Content Gap: " + (
                f"켜짐 — domain_intersection/live {n_gap_hosts}×2콜 (교집합 + 우리 부재) "
                f"→ keyword_gap"
                if n_gap_hosts else "꺼짐 (--intersect 0 또는 프로젝트 domain 미설정)"))
            print(f"단가 출처: DataForSEO Labs ranked_keywords/live ≈ ${serp_adapter.LABS_COST_PER_CALL}/call "
                  "(모듈 docstring). 실제 청구액은 응답 cost 로 기록.")
            return st.noop(cost=est_cost)

        own_norm = _existing_norms(conn, p["id"])
        gsc_norm = _gsc_seen_norms(conn, p["id"])
        print(f"     filter: existing={len(own_norm)} keywords, gsc_seen={len(gsc_norm)} queries")

        today = st.today
        total_cost = 0.0
        inserted_total = 0
        volumes_total = 0
        auto_new = metric_rows = gap_rows = extra_calls = 0

        def _post(path, body):
            """콜 수를 세는 자리 하나 — runs.api_calls 가 새 축까지 센다."""
            nonlocal extra_calls
            extra_calls += 1
            return post(path, body)

        def one(d: str) -> None:
            """도메인 하나 — 실패는 러너가 세고 다음 도메인으로 넘어간다."""
            nonlocal total_cost, inserted_total, volumes_total
            items, cost = fetch(d, locale, limit)
            total_cost += cost
            # 필터 — norm 비교로 케이스·공백 차이 흡수.
            kept: list[tuple[str, int | None]] = []
            for it in items:
                if scoring.norm(it["keyword"]) in own_norm:
                    continue
                if scoring.norm(it["keyword"]) in gsc_norm:
                    continue
                kept.append((it["keyword"], it["search_volume"]))
            inserted = db.add_keyword_candidates(
                conn, p["id"],
                [(kw, locale, "competitor_gap") for kw, _ in kept])
            inserted_total += inserted
            volumes_total += _backfill_volumes(conn, p["id"], kept)
            print(f"  {d}: fetched={len(items)} kept={len(kept)} inserted={inserted}")

        def auto_axis() -> None:
            """자동 탐지 + 트래픽 몫 — competitors_domain 한 콜로 둘 다 나온다."""
            nonlocal total_cost, auto_new, metric_rows, extra_calls
            rows, cost = _fetch_competitors(_post, ours, locale, n_auto)
            total_cost += cost
            # 제외 필터는 새로 만들지 않는다 — scoring.foreign_brands 가 yaml 의
            # tools/foreign_brands 와 기존 competitors 를 이미 정규화해 갖고 있다.
            brands = scoring.foreign_brands(conn, p["id"], st.cfg)
            mine, cand = None, []
            for row in rows:
                d = row["domain"]
                if scoring.owns(d, ours):
                    mine = mine or row["metrics"]   # 우리는 경쟁사가 아니다 — 분자로 간다
                    continue
                if scoring._stem(d) in brands:      # 등재·경쟁 도구 이름
                    continue
                cand.append(d)
            for d in cand[:n_auto]:
                # manual 로 등록된 행은 절대 덮지 않는다 — 사람이 고른 것이 이긴다.
                auto_new += conn.execute(
                    "INSERT INTO competitors(project_id, domain, source) VALUES(?,?, 'auto_serp') "
                    "ON CONFLICT(project_id, domain) DO NOTHING", (p["id"], d)).rowcount
            print(f"  auto: found={len(rows)} kept={len(cand)} new={auto_new}")

            # 몫의 분모 — 등록된 경쟁사 중 지표를 받은 것만.
            regs = {r0["domain"] for r0 in conn.execute(
                "SELECT domain FROM competitors WHERE project_id=?", (p["id"],))}
            for row in rows:
                if row["domain"] in regs and row["metrics"]:
                    _put_metric(conn, p["id"], today, row["domain"], 0, row["metrics"])
                    metric_rows += 1
            # 몫의 분자 — 우리 자신. 응답에 우리가 없으면 domain_rank_overview 로.
            if mine is None:
                try:
                    mine, c = _fetch_self_metrics(_post, ours, locale)
                    total_cost += c
                except Exception as e:
                    print(f"  ! 우리 지표 domain_rank_overview 실패: {e}", file=sys.stderr)
            if mine is None:
                # ponytail: 근사다. Labs 두 경로가 다 실패했을 때만 — ranked_keywords 로
                # 우리 키워드 '수'만 세고 etv/top10 은 NULL 로 둔다(창작 금지). 분자가
                # 아예 비면 몫을 못 내므로 그보다는 낫다는 판단이고, 정확한 값은 위 둘이 준다.
                try:
                    items, c = fetch(ours, locale, limit)
                    extra_calls += 1
                    total_cost += c
                    mine = {"keywords": len(items), "etv": None, "top10": None}
                except Exception as e:
                    print(f"  ! 우리 지표 근사(ranked_keywords) 실패: {e}", file=sys.stderr)
            if mine:
                _put_metric(conn, p["id"], today, ours, 1, mine)
                metric_rows += 1

        def gap_axis(rival: str) -> None:
            """경쟁사 하나 — 교집합(weak/shared) 1콜 + 우리 부재(missing) 1콜."""
            nonlocal total_cost, gap_rows
            n = 0
            for inter in (True, False):
                rows, cost = _fetch_intersection(_post, ours, rival, locale, limit, inter)
                total_cost += cost
                for row in rows:
                    _put_gap(conn, p["id"], today, rival, row)
                    n += 1
            gap_rows += n
            print(f"  gap {rival}: rows={n}")

        with st.record("competitors") as r:
            if n_auto:
                try:
                    auto_axis()
                except Exception as e:      # 한 축이 죽어도 나머지 축은 산다
                    st.fail(f"자동 경쟁사 탐지 실패: {e}", first=str(e))
                conn.commit()
                if not domain:              # 새로 붙은 경쟁사도 역키워드·Gap 대상에 넣는다
                    domains = _cap(_resolve_domains(conn, p["id"], None))
            r.api_calls = st.each(domains, one)
            if n_gap and ours:
                st.each(domains[:n_gap], gap_axis)
            r.api_calls += extra_calls
            r.notes = (f"domains={len(domains)} inserted={inserted_total} "
                       f"volumes_filled={volumes_total} auto_new={auto_new} "
                       f"metrics={metric_rows} gap_rows={gap_rows} {st.err_note}")

        print(f"\ncollected {len(domains)} domains, "
              f"actual_cost=${total_cost:.3f} (inserted={inserted_total}, volumes={volumes_total})\n"
              f"auto_competitors={auto_new} competitor_metrics={metric_rows} keyword_gap={gap_rows}\n"
              f"run_id={r.id}\n"
              f"Next: 후보는 source='competitor_gap', is_active=0 — "
              f"/capture keywords 의 큐레이션 단계로 활성화하세요.")
        # 적재 0건인데 오류가 있었으면 완료가 아니다 — 판정은 collector 한 벌이다.
        # "실제로 뭔가 했나"는 세 축의 합이다. inserted_total 만 보면 자동 탐지가
        # 죽고 Gap 축은 3행을 넣은 바퀴도 실패로 읽힌다 (아래 자체점검이 그 자리).
        return st.verdict(inserted_total + gap_rows + metric_rows,
                          rows=inserted_total, cost=total_cost)


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser()
    collector.add_common(ap)
    ap.add_argument("--domain", action="append", default=[],
                    help="분석할 경쟁사 도메인 (반복 가능). 생략 시 competitors 테이블 도메인 전부")
    collector.add_setting(ap, "--limit", key="limits.gap_limit", fallback=100, type=int,
                          help="도메인당 키워드 수 상한. 기본 100")
    collector.add_setting(ap, "--rivals", key="limits.auto_competitors", fallback=5, type=int,
                          help="자동 탐지할 경쟁사 수. 0이면 자동 탐지 끔 (수동 등록만 씀)")
    collector.add_setting(ap, "--intersect", key="limits.gap_rivals", fallback=3, type=int,
                          help="Content Gap 을 돌릴 경쟁사 수 상한. 0이면 끔")
    collector.add_setting(ap, "--throttle", key="throttle", fallback=0.5, type=float,
                          help="요청 간격(초). 기본은 config.yaml defaults.throttle")
    return ap


def main() -> None:
    # 인자 없이 실행되면 자체 점검 — 다른 수집기와 같은 약속. add_common 이
    # --project 를 required 로 걸어버리므로 parse 직전에 길이를 본다.
    if len(sys.argv) == 1:
        _selfcheck()
        return
    collector.cli("competitors")


def _selfcheck() -> None:
    """가짜 fetch 로 필터·적재 경로 전부 검증 — 진짜 Brain·Labs 안 건드림.

    의존물(conn·fetch)을 인자로 주입한다. 예전에는 requests.post 를 갈아끼우고
    sys.argv 를 세워 main() 을 부르는 우회로였다.
    """
    import tempfile

    home = Path(tempfile.mkdtemp(prefix="seo-miner-gap-selftest-"))
    os.environ["CAPTURE_HOME"] = str(home)
    # 키는 모킹 단계에서만 필요 — 실제 호출 안 함.
    os.environ["DATAFORSEO_LOGIN"] = "login"
    os.environ["DATAFORSEO_PASSWORD"] = "pw"

    # 프로젝트 yaml — tools 필터(자동 탐지 제외 목록)가 여기서 온다.
    (home / "projects").mkdir(parents=True, exist_ok=True)
    (home / "projects" / "gt.yaml").write_text(
        "name: gt\ndomain: gt.com\nlocale: ko-KR\ntools:\n  - ToolCo\n", encoding="utf-8")

    conn = db.connect()
    conn.execute(
        "INSERT INTO projects(name, domain, locale) VALUES('gt', 'gt.com', 'ko-KR')")
    p = conn.execute("SELECT * FROM projects WHERE name='gt'").fetchone()
    pid = p["id"]

    # 내가 이미 가진 키워드 — 필터에서 빠져야 한다.
    db.add_keyword_candidates(conn, pid, [
        ("공통 키워드", "ko-KR", "seed"),
        ("내 시드", "ko-KR", "seed"),
    ])
    conn.execute("UPDATE keywords SET is_active=1 WHERE keyword='내 시드'")

    # GSC 스냅샷에 잡힌 쿼리 — 필터에서 빠져야 한다.
    conn.execute(
        """INSERT INTO gsc_snapshots(project_id, snapshot_date, period_days,
              query, page, clicks, impressions, ctr, position)
             VALUES(?, '2026-08-18', 28, ?, NULL, 1, 10, 0.1, 5.0)""",
        (pid, "gsc 잡힌 쿼리"))

    # 경쟁사 한 곳 등록 (--domain 생략 시 이게 쓰인다).
    conn.execute(
        "INSERT INTO competitors(project_id, domain, source) VALUES(?, 'rival.com', 'manual')",
        (pid,))
    # 두 번째 수동 등록 — 어간이 두 글자라 foreign_brands 의 len>=3 문턱에 안 걸린다.
    # 그래서 자동 탐지가 실제로 INSERT 를 시도하고, ON CONFLICT DO NOTHING 이
    # manual 을 지키는지가 여기서만 검사된다 (rival.com 은 필터 단계에서 이미 빠진다).
    conn.execute(
        "INSERT INTO competitors(project_id, domain, source) VALUES(?, 'hq.io', 'manual')",
        (pid,))
    conn.commit()

    # Labs 모킹 — 의존물은 인자로 준다 (예전에는 requests.post 를 갈아끼웠다).
    # "공통 키워드" / "gsc 잡힌 쿼리" 는 필터에 걸리고,
    # "신규 후보 A" / "신규 후보 B" 만 남아야 한다.
    def fake_fetch(domain, locale, limit):
        assert (domain, locale) == ("rival.com", "ko-KR"), (domain, locale)
        return [
            {"keyword": "공통 키워드", "search_volume": 50},
            {"keyword": "신규 후보 A", "search_volume": 1200},
            {"keyword": "GSC 잡힌 쿼리", "search_volume": None},
            {"keyword": "신규 후보 B", "search_volume": 7},
        ], 0.0007

    # 꺼진 축이 Labs 를 부르면 여기 남는다. 예외만 던지면 st.each 가 삼켜서 검사가
    # 조용히 통과한다 — 그래서 '불렀다'는 사실 자체를 기록한다.
    off_calls: list = []

    def no_post(path, body):
        off_calls.append(path)
        raise AssertionError(f"꺼진 축이 Labs 를 불렀다: {path}")

    # 새 축을 둘 다 끄면 기존 동작 그대로여야 한다 (회귀 방어 — 아래 단언은 원본 그대로).
    res = collect("gt", domain=["rival.com"], throttle=0, conn=conn, fetch=fake_fetch,
                  rivals=0, intersect=0, post=no_post)
    assert (res.ok, res.skipped, res.rows) == (True, False, 2), res
    assert off_calls == [], off_calls
    assert conn.execute("SELECT COUNT(*) FROM competitor_metrics").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM keyword_gap").fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM competitors WHERE source='auto_serp'").fetchone()[0] == 0
    conn.execute("SELECT 1")     # 빌린 conn 은 러너가 닫지 않는다

    added = [dict(r) for r in conn.execute(
        "SELECT keyword, source, is_active, locale, volume "
        "FROM keywords WHERE project_id=? AND source='competitor_gap' ORDER BY keyword",
        (pid,)).fetchall()]
    assert len(added) == 2, f"필터 후 신규 후보 2개여야 함: {added}"
    assert {r["keyword"] for r in added} == {"신규 후보 A", "신규 후보 B"}
    assert all(r["is_active"] == 0 for r in added)
    assert all(r["locale"] == "ko-KR" for r in added)
    by_kw = {r["keyword"]: r["volume"] for r in added}
    assert by_kw["신규 후보 A"] == 1200, by_kw
    assert by_kw["신규 후보 B"] == 7, by_kw

    # dry-run 은 Labs 를 부르지도, run 을 남기지도 않아야 한다 — 지뢰 fetch 로 확인.
    conn.execute("DELETE FROM runs")
    conn.commit()

    def boom(*a, **kw):
        raise AssertionError("dry-run 이 Labs 를 불렀다")

    res = collect("gt", dry_run=True, conn=conn, fetch=boom, post=boom)
    assert (res.ok, res.skipped) == (True, True), res
    runs = conn.execute("SELECT * FROM runs").fetchall()
    assert runs == [], f"dry-run 은 runs 에 아무것도 남기지 않아야 함: {runs}"

    # ── 자동 탐지 + 트래픽 몫 + Content Gap ──────────────────────────
    posts: list = []

    def fake_post(path, body):
        posts.append((path, body[0]))
        if path == LABS_COMPETITORS:
            return [{"items": [
                # 우리 자신 — 경쟁사 목록에서 빠지고 is_self=1 로 간다
                {"domain": "gt.com", "metrics": {"organic": {
                    "count": 40, "etv": 400.0, "pos_1": 1, "pos_2_3": 2, "pos_4_10": 3}}},
                # yaml tools 의 'ToolCo' — 등재 도구라 경쟁사가 아니다
                {"domain": "toolco.io", "metrics": {"organic": {"count": 10, "etv": 10.0}}},
                # 이미 manual 로 등록됨 — source 를 덮으면 안 된다
                {"domain": "rival.com", "metrics": {"organic": {
                    "count": 30, "etv": 300.0, "pos_2_3": 4}}},
                # www 는 host_of 가 벗긴다
                {"domain": "www.newrival.com", "metrics": {"organic": {
                    "count": 20, "etv": 200.0, "pos_4_10": 5}}},
                # 숫자를 문자열로 주는 응답도 읽는다 / top10 은 안 주면 NULL
                {"domain": "second.co", "metrics": {"organic": {"count": "9", "etv": "90"}}},
                # 이미 manual 인데 필터를 통과한다 — INSERT 가 실제로 conflict 를 만난다
                {"domain": "hq.io", "metrics": {"organic": {"count": 5, "etv": 10.0}}},
                {"no_domain_field": 1},          # 못 읽는 항목은 건너뛰고 나머지는 산다
            ]}], 0.002
        if path == LABS_INTERSECT:
            rival, inter = body[0]["target2"], body[0]["intersections"]
            if inter:
                return [{"items": [
                    {"keyword_data": {"keyword": f"{rival} weak",
                                      "keyword_info": {"search_volume": 100}},
                     "first_domain_serp_element": {"rank_group": 9},
                     "second_domain_serp_element": {"rank_group": 2}},
                    {"keyword_data": {"keyword": f"{rival} shared",
                                      "keyword_info": {"search_volume": 50}},
                     "first_domain_serp_element": {"rank_group": 3},
                     "second_domain_serp_element": {"rank_group": 7}},
                    {"keyword_data": {"keyword": ""}},      # 키워드 못 읽음 — 이 항목만 버린다
                ]}], 0.001
            # intersections:false = 경쟁사만 잡은 것. 필드 모양을 일부러 다르게 준다.
            return [{"items": [
                {"keyword": f"{rival} missing", "search_volume": "30",
                 "target2_serp_element": {"rank_absolute": 4}},
            ]}], 0.001
        raise AssertionError(f"예상 밖 엔드포인트: {path}")

    def fake_fetch2(domain, locale, limit):
        return [{"keyword": f"{domain} 키워드", "search_volume": 5}], 0.0001

    res = collect("gt", throttle=0, conn=conn, fetch=fake_fetch2, post=fake_post,
                  rivals=3, intersect=1)
    assert res.ok and res.cost > 0, res

    # 1) auto_serp 로 들어가고 manual 은 그대로 — 우리 자신·도구 이름은 빠진다
    comp = dict(conn.execute(
        "SELECT domain, source FROM competitors WHERE project_id=?", (pid,)).fetchall())
    assert comp == {"rival.com": "manual", "hq.io": "manual",
                    "newrival.com": "auto_serp", "second.co": "auto_serp"}, comp

    # 2) 우리(is_self=1)와 경쟁사가 같은 표에 — 등록 안 한 toolco.io 는 몫에도 없다
    met = {r0["domain"]: dict(r0) for r0 in conn.execute(
        "SELECT domain, is_self, keywords, etv, top10 FROM competitor_metrics "
        "WHERE project_id=?", (pid,)).fetchall()}
    assert set(met) == {"gt.com", "rival.com", "hq.io", "newrival.com", "second.co"}, met
    assert (met["gt.com"]["is_self"], met["gt.com"]["etv"]) == (1, 400.0), met["gt.com"]
    assert met["gt.com"]["top10"] == 6, met["gt.com"]        # pos_1 + pos_2_3 + pos_4_10
    assert (met["rival.com"]["is_self"], met["rival.com"]["top10"]) == (0, 4), met["rival.com"]
    assert (met["second.co"]["keywords"], met["second.co"]["etv"]) == (9, 90.0), met["second.co"]
    assert met["second.co"]["top10"] is None, "안 준 구간을 0으로 만들지 않는다"
    cols = {r0[1] for r0 in conn.execute("PRAGMA table_info(competitor_metrics)")}
    assert "share" not in cols, f"몫은 저장하지 않는다 — 조회 시 etv/SUM(etv): {cols}"
    denom = conn.execute(
        "SELECT SUM(etv) FROM competitor_metrics WHERE project_id=?", (pid,)).fetchone()[0]
    assert denom == 1000.0, denom                                  # 400+300+200+90+10
    assert abs(met["gt.com"]["etv"] / denom - 0.4) < 1e-9          # 몫은 이렇게 조회한다

    # 3) kind 셋이 위치 비교로 갈린다
    gaps = {r0["keyword"]: dict(r0) for r0 in conn.execute(
        "SELECT keyword, domain, position, our_position, volume, kind FROM keyword_gap "
        "WHERE project_id=?", (pid,)).fetchall()}
    assert set(gaps) == {"rival.com weak", "rival.com shared", "rival.com missing"}, gaps
    assert gaps["rival.com weak"]["kind"] == "weak", gaps          # 우리 9위 < 경쟁사 2위
    assert gaps["rival.com shared"]["kind"] == "shared", gaps      # 우리 3위 > 경쟁사 7위
    assert gaps["rival.com missing"]["kind"] == "missing", gaps    # 우리 위치 없음
    assert (gaps["rival.com missing"]["position"],
            gaps["rival.com missing"]["our_position"],
            gaps["rival.com missing"]["volume"]) == (4, None, 30), gaps
    assert gaps["rival.com weak"]["domain"] == "rival.com", gaps
    assert _kind(None, 3) == "missing" and _kind(5, 5) == "shared" and _kind(5, 2) == "weak"

    # 4) 콜 모양 — 자동 탐지 1회, 교집합/부재 2축, 우리가 응답에 있으면 overview 안 부름
    paths = [q[0] for q in posts]
    assert paths.count(LABS_COMPETITORS) == 1, paths
    assert paths.count(LABS_INTERSECT) == 2, paths
    assert LABS_OVERVIEW not in paths, paths
    b0 = posts[0][1]
    assert (b0["target"], b0["limit"], b0["order_by"]) == ("gt.com", 3, ["intersections,desc"]), b0
    assert (b0["location_name"], b0["language_code"]) == ("South Korea", "ko"), b0
    assert {q[1]["intersections"] for q in posts if q[0] == LABS_INTERSECT} == {True, False}, posts
    assert {q[1]["target1"] for q in posts if q[0] == LABS_INTERSECT} == {"gt.com"}, posts
    calls = conn.execute("SELECT api_calls FROM runs ORDER BY id DESC LIMIT 1").fetchone()[0]
    assert calls == 7, f"역키워드 4 + 자동탐지 1 + 교집합 2 = 7: {calls}"

    # 우리 자신이 응답에 없으면 domain_rank_overview 가 분자를 채운다
    conn.execute("DELETE FROM competitor_metrics")
    conn.commit()
    posts.clear()

    def post_overview(path, body):
        posts.append((path, body[0]))
        if path == LABS_COMPETITORS:
            return [{"items": [{"domain": "rival.com",
                                "metrics": {"organic": {"count": 30, "etv": 300.0}}}]}], 0.001
        if path == LABS_OVERVIEW:
            # items 없이 result 가 곧 지표인 모양도 받는다
            return [{"metrics": {"organic": {"count": 44, "etv": 444.0, "pos_1": 2}}}], 0.001
        raise AssertionError(path)

    collect("gt", throttle=0, conn=conn, fetch=fake_fetch2, post=post_overview,
            rivals=1, intersect=0)
    assert LABS_OVERVIEW in [q[0] for q in posts], posts
    mine = conn.execute(
        "SELECT * FROM competitor_metrics WHERE project_id=? AND is_self=1", (pid,)).fetchone()
    assert (mine["domain"], mine["keywords"], mine["etv"], mine["top10"]) \
        == ("gt.com", 44, 444.0, 2), dict(mine)

    # 두 경로가 다 죽어도 축 하나가 전체를 죽이지 않는다 — 근사(개수만)로 분자를 남긴다
    conn.execute("DELETE FROM competitor_metrics")
    conn.commit()

    def post_no_overview(path, body):
        if path == LABS_COMPETITORS:
            return [{"items": [{"domain": "rival.com",
                                "metrics": {"organic": {"count": 30}}}]}], 0.001
        raise RuntimeError("overview 없음")

    res = collect("gt", throttle=0, conn=conn, fetch=fake_fetch2, post=post_no_overview,
                  rivals=1, intersect=0)
    assert res.ok, res
    mine = conn.execute(
        "SELECT * FROM competitor_metrics WHERE project_id=? AND is_self=1", (pid,)).fetchone()
    assert (mine["keywords"], mine["etv"], mine["top10"]) == (1, None, None), dict(mine)

    # 자동 탐지 축이 통째로 죽어도 역키워드·Gap 은 산다 (한 콜이 나머지를 못 죽인다)
    conn.execute("DELETE FROM keyword_gap")
    conn.commit()

    def post_dead(path, body):
        if path == LABS_COMPETITORS:
            raise RuntimeError("competitors_domain 장애")
        return fake_post(path, body)

    res = collect("gt", throttle=0, conn=conn, fetch=fake_fetch2, post=post_dead,
                  rivals=1, intersect=1)
    assert res.ok and not res.skipped, res
    assert conn.execute("SELECT COUNT(*) FROM keyword_gap").fetchone()[0] == 3, \
        "자동 탐지가 죽어도 Content Gap 축은 돌아야 한다"

    # 경쟁사 0건 = 건너뜀이지 실패가 아니다. ok=False 로 돌아가면 run_all 체인이
    # exit 1 → worker.py 의 주간 리포트 메일이 안 나간다 (test_collectors 의
    # run_all 테스트는 가짜 fn 을 주입해서 이 경로를 못 잡는다).
    # rivals=0/intersect=0 이면 새 축은 아무것도 안 부른다 — 지뢰로 확인.
    conn.execute("DELETE FROM competitors WHERE project_id=?", (pid,))
    conn.commit()
    res = collect("gt", conn=conn, fetch=boom, post=no_post, rivals=0, intersect=0)
    assert (res.ok, res.skipped) == (True, True), res
    assert res.reason, "왜 건너뛰었는지·다음에 뭘 할지 말해야 한다"
    assert off_calls == [], off_calls

    conn.close()
    print("collect_gap self-check ok")


if __name__ == "__main__":
    main()
