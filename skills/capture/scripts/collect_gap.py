#!/usr/bin/env python3
"""경쟁사 역키워드 수집 — DataForSEO Labs ranked_keywords (F3-확장).

경쟁사 도메인이 랭킹하는 키워드를 끌어와, 내가 안 잡고 있는 것만 후보로
적재한다 ("경쟁사는 잡는데 나는 부재"). 무료 경로(collect_serp 의 rank 상위
수집)는 SERP를 한 번씩 다시 보는 비용이 들고 얕다 — Labs 는 도메인 한 곳
요청으로 최대 수백 개의 랭킹 키워드를 한 번에 돌려준다.

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
  5) 실행 전체를 db.run(conn, pid, "gap") 컨텍스트로 감싸 api_calls·cost 기록.

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

Usage:
  python collect_gap.py --project NAME [--domain d1.com --domain d2.com]
                        [--limit 100] [--throttle 0.5] [--dry-run]
  python collect_gap.py                                  # self-check
"""
import argparse
import os
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))
import collector  # noqa: E402
import db  # noqa: E402
import scoring  # noqa: E402

LABS_URL = "https://api.dataforseo.com/v3/dataforseo_labs/google/ranked_keywords/live"
# 단가 상한 (가격표 출처는 모듈 docstring). 응답의 cost 가 더 정확하지만
# dry-run 고지용으로는 이 값으로 충분.
LABS_COST_PER_CALL = 0.001
DOMAIN_CAP = 5


def _have_creds() -> bool:
    return bool(os.environ.get("DATAFORSEO_LOGIN")) and bool(os.environ.get("DATAFORSEO_PASSWORD"))


def fetch_ranked(target: str, locale: str, limit: int) -> tuple[list[dict], float]:
    """Labs ranked_keywords 한 도메인 조회.

    반환: (items, cost). items 는 [{keyword, search_volume|None}, ...].
    Labs 응답의 키워드는 lowercase 정규화해서 돌려준다 — 후속 필터(norm 비교)와
    같은 모양.
    """
    login, pw = os.environ["DATAFORSEO_LOGIN"], os.environ["DATAFORSEO_PASSWORD"]
    loc, lang, _ = collector_module_location(locale)
    body = [{
        "target": target,
        "location_name": loc,
        "language_code": lang,
        "limit": limit,
        "load_rank_absolute": False,
    }]
    r = requests.post(LABS_URL, auth=(login, pw), timeout=60, json=body)
    r.raise_for_status()
    data = r.json()
    task = (data.get("tasks") or [{}])[0]
    if task.get("status_code", 0) >= 40000:
        raise RuntimeError(f"dataforseo task error: {task.get('status_message')}")
    cost = float(task.get("cost") or data.get("cost") or 0.0)
    result = (task.get("result") or [])
    items: list[dict] = []
    for r0 in result:
        for it in (r0.get("items") or []):
            kw = it.get("keyword_data") or it.get("keyword") or ""
            if isinstance(it.get("keyword_data"), dict):
                # Live 응답은 keyword_data 가 dict 모양으로 옴 (서로 다른 필드명 모두 허용)
                kd = it["keyword_data"]
                kw = kd.get("keyword") or kw
                sv = kd.get("keyword_info", {}).get("search_volume") if isinstance(
                    kd.get("keyword_info"), dict) else None
                if sv is None:
                    sv = kd.get("search_volume")
            sv = it.get("search_volume", sv)
            if isinstance(sv, str):
                sv = int(sv) if sv.isdigit() else None
            kw = (kw or "").strip()
            if kw:
                items.append({"keyword": kw, "search_volume": sv})
    return items, cost


def collector_module_location(locale: str):
    """serp_adapter.location 를 그대로 쓰면 모듈 의존이 늘지만, Labs 의
    location_name·language_code 매핑은 SERP 와 동일 — DRY 한 곳에서.
    self-check 에선 serp_adapter 가 없을 수도 있으니 inline fallback 도 둔다.
    """
    try:
        from serp_adapter import location as _loc  # type: ignore
        return _loc(locale)
    except Exception:
        key = (locale or "").split("-")[0].lower()
        return {"ko": ("South Korea", "ko"),
                "en": ("United States", "en"),
                "ja": ("Japan", "ja")}.get(key, ("United States", "en"))


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


def main() -> None:
    ap = argparse.ArgumentParser()
    collector.add_common(ap)
    ap.add_argument("--domain", action="append", default=[],
                    help="분석할 경쟁사 도메인 (반복 가능). 생략 시 competitors 테이블 도메인 전부")
    collector.add_setting(ap, "--limit", key="limits.gap_limit", fallback=100, type=int,
                          help="도메인당 키워드 수 상한. 기본 100")
    collector.add_setting(ap, "--throttle", key="throttle", fallback=0.5, type=float,
                          help="요청 간격(초). 기본은 config.yaml defaults.throttle")
    # 인자 없이 실행되면 자체 점검 — 다른 수집기와 같은 약속. add_common 이
    # --project 를 required 로 걸어버리므로 parse 직전에 길이를 본다.
    if len(sys.argv) == 1:
        _selfcheck()
        return
    a = ap.parse_args()

    conn, p, cfg = collector.open_project(a.project)
    if not _have_creds():
        conn.close()
        sys.exit("Labs 유료 키 필요 — DATAFORSEO_LOGIN/PASSWORD 설정 (references/setup.md 7절).")

    s = collector.settings(a, cfg)
    limit, throttle = s["limits.gap_limit"], s["throttle"]

    domains = _resolve_domains(conn, p["id"], a.domain)
    if not domains:
        conn.close()
        sys.exit(f"'{p['name']}' 에 등록된 경쟁사가 없습니다 — "
                 "`/capture add` 또는 `--domain` 으로 명시하세요.")
    if len(domains) > DOMAIN_CAP:
        print(f"[안내] 경쟁사 {len(domains)}개는 상한 {DOMAIN_CAP}개를 초과 — "
              f"앞 {DOMAIN_CAP}개만 사용합니다. 더 정밀하게는 `--domain` 으로 명시하세요.",
              file=sys.stderr)
        domains = domains[:DOMAIN_CAP]

    locale = p["locale"] or "ko-KR"
    est_cost = len(domains) * LABS_COST_PER_CALL
    print(f"[gap] project={p['name']} domains={len(domains)} limit={limit} "
          f"est_cost≈${est_cost:.3f} (~{len(domains) * (throttle + 2) / 60:.1f} min)")

    if a.dry_run:
        for d in domains:
            print(f"  - {d}")
        print(f"단가 출처: DataForSEO Labs ranked_keywords/live ≈ ${LABS_COST_PER_CALL}/call "
              "(모듈 docstring). 실제 청구액은 응답 cost 로 기록.")
        conn.close()
        return

    own_norm = _existing_norms(conn, p["id"])
    gsc_norm = _gsc_seen_norms(conn, p["id"])
    print(f"     filter: existing={len(own_norm)} keywords, gsc_seen={len(gsc_norm)} queries")

    total_cost = 0.0
    inserted_total = 0
    volumes_total = 0
    per_domain: list[tuple[str, int, int]] = []  # (domain, fetched, kept)

    with db.run(conn, p["id"], "gap") as r:
        for d in domains:
            try:
                items, cost = fetch_ranked(d, locale, limit)
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
                per_domain.append((d, len(items), inserted))
                r.api_calls += 1
                print(f"  {d}: fetched={len(items)} kept={len(kept)} inserted={inserted}")
            except Exception as e:
                print(f"  ! {d}: {e}", file=sys.stderr)
            conn.commit()
            time.sleep(throttle)

        r.notes = (f"domains={len(domains)} inserted={inserted_total} "
                   f"volumes_filled={volumes_total}")

    print(f"\ncollected {len(domains)} domains, "
          f"actual_cost=${total_cost:.3f} (inserted={inserted_total}, volumes={volumes_total})\n"
          f"run_id={r.id}\n"
          f"Next: 후보는 source='competitor_gap', is_active=0 — "
          f"/capture keywords 의 큐레이션 단계로 활성화하세요.")
    conn.close()


def _selfcheck() -> None:
    """HTTP 모킹으로 파싱·필터·적재 경로 전부 검증 — 진짜 Brain 안 건드림.

    패턴: test_collectors.py 가 collect_ai/serp 에 쓰는 방식과 같은 family.
    다른 작업자가 동시에 test_collectors.py 를 만지는 중이라 거기는 안 건드리고,
    본 파일 하단의 _selfcheck 만 두는 게 책임 경계가 명확하다.

    db.CAPTURE_HOME/DB_PATH 는 모듈 import 시점에 한 번 계산된다. 본 파일이
    이미 db 를 import 한 뒤라 os.environ["CAPTURE_HOME"] 만 바꿔선 진짜
    Brain(~/.capture/brain.db) 을 건드린다 — 모듈 속성을 직접 패치한다.
    """
    import tempfile

    home = Path(tempfile.mkdtemp(prefix="seo-miner-gap-selftest-"))
    db.CAPTURE_HOME = home
    db.DB_PATH = home / "brain.db"
    os.environ["CAPTURE_HOME"] = str(home)
    # 키는 모킹 단계에서만 필요 — 실제 호출 안 함.
    os.environ["DATAFORSEO_LOGIN"] = "login"
    os.environ["DATAFORSEO_PASSWORD"] = "pw"

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
    conn.commit()

    # Labs 모킹 응답 — 두 도메인 모두 같은 페이로드로 단순화.
    # "공통 키워드" / "gsc 잡힌 쿼리" 는 필터에 걸리고,
    # "신규 후보 A" / "신규 후보 B" 만 남아야 한다.
    fake_resp = {
        "tasks": [{
            "status_code": 20000,
            "cost": 0.0007,
            "result": [{
                "items": [
                    {"keyword_data": {"keyword": "공통 키워드", "keyword_info": {"search_volume": 50}}},
                    {"keyword_data": {"keyword": "신규 후보 A", "keyword_info": {"search_volume": 1200}}},
                    {"keyword_data": {"keyword": "GSC 잡힌 쿼리", "keyword_info": {"search_volume": None}}},
                    {"keyword_data": {"keyword": "신규 후보 B", "keyword_info": {"search_volume": 7}}},
                ]
            }]
        }]
    }

    class _R:
        def __init__(self, d):
            self._d, self.status_code = d, 200
        def json(self):
            return self._d
        def raise_for_status(self):
            return None

    orig_post = requests.post

    def fake_post(url, *args, **kwargs):
        assert "ranked_keywords" in url, url
        return _R(fake_resp)

    requests.post = fake_post
    orig_argv = sys.argv
    try:
        sys.argv = [
            "collect_gap.py", "--project", "gt",
            "--domain", "rival.com", "--throttle", "0",
        ]
        main()
    finally:
        requests.post = orig_post
        sys.argv = orig_argv

    conn = db.connect()
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

    # dry-run 도 같은 DB 가서 run 기록이 남지 않는지 — 추가 검증.
    conn.execute("DELETE FROM runs")
    conn.commit()
    sys.argv = ["collect_gap.py", "--project", "gt", "--dry-run"]
    try:
        main()
    finally:
        sys.argv = orig_argv
    runs = conn.execute("SELECT * FROM runs").fetchall()
    assert runs == [], f"dry-run 은 runs 에 아무것도 남기지 않아야 함: {runs}"
    conn.close()
    print("collect_gap self-check ok")


if __name__ == "__main__":
    main()
