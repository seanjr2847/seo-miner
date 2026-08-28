#!/usr/bin/env python3
"""백링크 — 요약·참조 도메인·개별 링크·앵커·Link Intersect (DataForSEO).

백링크는 코드가 아니라 인프라다(자체 크롤러로 링크 그래프를 만드는 일). 사서 쓴다.
여태 server/backlinks.py 가 요약과 참조 도메인만 호스팅에서 받아 왔다 — 로컬에는
백링크 축이 아예 없었다. 단계로 내려서 두 배포가 같은 경로를 쓴다.

요약만으로는 "무엇을 할지"가 안 나온다. 그래서 다섯 축을 함께 캔다:

  1. /backlinks/summary/live            → backlink_summary   (프로필 한 줄)
  2. /backlinks/referring_domains/live  → referring_domains  (누가 링크를 주나)
  3. /backlinks/backlinks/live          → backlinks          (어느 페이지가 어떤 앵커로)
  4. /backlinks/anchors/live            → backlink_anchors   (앵커 분포)
  5. /backlinks/domain_intersection/live→ link_intersect     (경쟁사는 받는데 우리는 못 받는 곳)

설계 메모:
  · 인증·타임아웃·task 오류 판정은 serp_adapter.post_dataforseo 한 곳이다.
    이 파일은 requests 를 import 하지 않는다 (server/backlinks.py 가 자기 _post 를
    따로 갖고 있어서 오류 문구가 두 벌이던 자리).
  · 응답 필드명은 DataForSEO 가 엔드포인트마다 조금씩 다르게 준다. 읽는 자리는
    전부 _g() 로 여러 이름을 받아 보고, 못 읽는 항목은 건너뛰고 나머지는 살린다.
  · 호출 하나가 죽어도 나머지는 간다 — st.each 가 항목별로 세고 넘어간다.
  · 백링크는 하루 단위로 안 움직인다. --max-age(기본 7일) 안이면 다시 사지 않는다.

비용 (2026-08 가격표, https://dataforseo.com/pricing):
  요청당 $0.024 + 행당 $0.000036. 실청구액은 응답의 cost 를 그대로 합산한다 —
  아래 상수는 --dry-run 고지용 추정치일 뿐이다.

Usage:
  python collect_backlinks.py --project NAME [--limit 200] [--max-age 7] [--dry-run]
  python collect_backlinks.py                                  # self-check
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).parent))

import collector  # noqa: E402
import db  # noqa: E402
import serp_adapter  # noqa: E402

REQUEST_COST = 0.024        # 요청 1건 (dry-run 고지용 추정)
ROW_COST = 0.000036         # 행 1개
INTERSECT_MIN_RIVALS = 2    # 교집합은 둘 이상이어야 '교집합'이다


def _g(d, *names, default=None):
    """여러 필드명 중 먼저 값이 있는 것. 응답 스키마가 엔드포인트마다 달라서 필요하다."""
    if not isinstance(d, dict):
        return default
    for n in names:
        v = d.get(n)
        if v is not None:
            return v
    return default


def _int(v) -> int | None:
    """숫자/불리언/숫자문자열을 정수로. 못 읽으면 None (창작하지 않는다)."""
    if v is None or isinstance(v, dict) or isinstance(v, list):
        return None
    if isinstance(v, bool):
        return int(v)
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _host(url: str | None) -> str | None:
    """url_from 에서 도메인 — domain_from 이 안 왔을 때의 대체."""
    try:
        return (urlparse(url or "").netloc or "").lower() or None
    except ValueError:
        return None


def _items(res) -> list[dict]:
    """DataForSEO result → items. result 가 여럿이면 전부 이어 붙인다."""
    out = []
    for r0 in (res or []):
        out.extend([it for it in (_g(r0, "items", default=[]) or []) if isinstance(it, dict)])
    return out


# ── 표별 적재 ────────────────────────────────────────────────────────────────

def _put_summary(conn, pid: int, today: str, res) -> int:
    s = (res[0] if res else {}) or {}
    attrs = _g(s, "referring_links_attributes") or _g(
        _g(s, "info", default={}) or {}, "referring_links_attributes") or {}
    conn.execute(
        "INSERT INTO backlink_summary(project_id, checked_date, rank, backlinks,"
        " referring_domains, referring_main_domains, broken_backlinks, dofollow, nofollow)"
        " VALUES (?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(project_id, checked_date) DO UPDATE SET"
        " rank=excluded.rank, backlinks=excluded.backlinks,"
        " referring_domains=excluded.referring_domains,"
        " referring_main_domains=excluded.referring_main_domains,"
        " broken_backlinks=excluded.broken_backlinks,"
        " dofollow=excluded.dofollow, nofollow=excluded.nofollow",
        (pid, today, _int(_g(s, "rank")), _int(_g(s, "backlinks")),
         _int(_g(s, "referring_domains")), _int(_g(s, "referring_main_domains")),
         _int(_g(s, "broken_backlinks")),
         _int(_g(attrs, "dofollow")), _int(_g(attrs, "nofollow"))))
    return 1 if s else 0


def _put_refdomains(conn, pid: int, today: str, res) -> int:
    rows = []
    for it in _items(res):
        dom = _g(it, "domain", "target", "referring_domain")
        if not dom:
            continue
        rows.append((pid, today, str(dom).lower(), _int(_g(it, "rank", "domain_rank")),
                     _int(_g(it, "backlinks")), _int(_g(it, "dofollow")),
                     _g(it, "first_seen"), _g(it, "lost_date")))
    conn.executemany(
        "INSERT INTO referring_domains(project_id, checked_date, domain, rank,"
        " backlinks, dofollow, first_seen, lost_date) VALUES (?,?,?,?,?,?,?,?)"
        " ON CONFLICT(project_id, checked_date, domain) DO UPDATE SET"
        " rank=excluded.rank, backlinks=excluded.backlinks, dofollow=excluded.dofollow,"
        " first_seen=excluded.first_seen, lost_date=excluded.lost_date", rows)
    return len(rows)


def _put_backlinks(conn, pid: int, today: str, res) -> int:
    rows = []
    for it in _items(res):
        u_from = _g(it, "url_from", "page_from_url", "url")
        u_to = _g(it, "url_to", "page_to_url")
        if not u_from or not u_to:
            continue                     # 링크의 양 끝이 없으면 백링크가 아니다
        broken = _g(it, "is_broken", "is_lost", default=False)
        if broken is False:
            status = _int(_g(it, "url_to_status_code", "page_to_status_code"))
            broken = bool(status and status >= 400)
        rows.append((pid, today, str(u_from), str(u_to),
                     (_g(it, "domain_from", "page_from_domain") or _host(u_from)),
                     _g(it, "anchor", "text"),
                     _int(_g(it, "rank", "page_from_rank", "domain_from_rank")),
                     _int(_g(it, "dofollow")), int(bool(broken)),
                     _g(it, "first_seen"), _g(it, "last_seen", "last_visited")))
    conn.executemany(
        "INSERT INTO backlinks(project_id, checked_date, url_from, url_to, domain_from,"
        " anchor, rank, dofollow, is_broken, first_seen, last_seen)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(project_id, checked_date, url_from, url_to) DO UPDATE SET"
        " domain_from=excluded.domain_from, anchor=excluded.anchor, rank=excluded.rank,"
        " dofollow=excluded.dofollow, is_broken=excluded.is_broken,"
        " first_seen=excluded.first_seen, last_seen=excluded.last_seen", rows)
    return len(rows)


def _put_anchors(conn, pid: int, today: str, res) -> int:
    rows = []
    for it in _items(res):
        anchor = _g(it, "anchor", "text")
        if anchor is None or str(anchor).strip() == "":
            continue                     # 빈 앵커(이미지 링크)는 앵커 분포에 뜻이 없다
        types = _g(it, "referring_links_types", default={}) or {}
        rows.append((pid, today, str(anchor), _int(_g(it, "backlinks")),
                     _int(_g(it, "referring_domains", "referring_main_domains")),
                     _int(_g(it, "dofollow")) if _g(it, "dofollow") is not None
                     else _int(_g(types, "anchor"))))
    conn.executemany(
        "INSERT INTO backlink_anchors(project_id, checked_date, anchor, backlinks,"
        " referring_domains, dofollow) VALUES (?,?,?,?,?,?)"
        " ON CONFLICT(project_id, checked_date, anchor) DO UPDATE SET"
        " backlinks=excluded.backlinks, referring_domains=excluded.referring_domains,"
        " dofollow=excluded.dofollow", rows)
    return len(rows)


def _intersect_rows(res, pos: dict[str, str]) -> list[tuple]:
    """domain_intersection 응답 → (domain, rank, hits, targets).

    응답 모양이 둘 있다: 항목이 도메인을 직접 들고 오는 것, 그리고 targets 번호를
    키로 한 dict 를 들고 오는 것("1": {...}, "2": {...}). 둘 다 받는다.
    """
    out = []
    for it in _items(res):
        hit_keys = [k for k, v in it.items() if k in pos and v]
        nested = [it[k] for k in hit_keys if isinstance(it[k], dict)]
        dom = _g(it, "domain", "target", "referring_domain")
        rank = _int(_g(it, "rank", "domain_rank"))
        for n in nested:
            dom = dom or _g(n, "domain", "domain_from", "target")
            if rank is None:
                rank = _int(_g(n, "rank", "domain_from_rank"))
        if not dom:
            continue
        hits = _int(_g(it, "hits", "intersections")) or len(hit_keys) or len(pos)
        out.append((str(dom).lower(), rank, hits,
                    ",".join(pos[k] for k in hit_keys) or ",".join(pos.values())))
    return out


def _put_intersect(conn, pid: int, today: str, res, pos: dict[str, str]) -> int:
    have = {r["domain"] for r in conn.execute(
        "SELECT domain FROM referring_domains WHERE project_id=? AND checked_date=?",
        (pid, today)).fetchall()}
    rows = [(pid, today, dom, rank, hits, targets, int(dom in have))
            for dom, rank, hits, targets in _intersect_rows(res, pos)]
    conn.executemany(
        "INSERT INTO link_intersect(project_id, checked_date, domain, rank, hits,"
        " targets, we_have) VALUES (?,?,?,?,?,?,?)"
        " ON CONFLICT(project_id, checked_date, domain) DO UPDATE SET"
        " rank=excluded.rank, hits=excluded.hits, targets=excluded.targets,"
        " we_have=excluded.we_have", rows)
    return len(rows)


# ── 단계 ─────────────────────────────────────────────────────────────────────

def collect(project: str, *,
            dry_run: bool = False,
            backlink_limit: int | None = None,
            max_age: int | None = None,
            conn=None,
            post=None,
            **opts) -> collector.StageResult:
    """백링크 다섯 축을 Brain 에 적재한다.

    Args:
        project: 사이트 이름
        dry_run: True 면 호출 계획 + 예상 비용만 찍고 종료 (실제 호출 0건)
        backlink_limit: 참조 도메인·개별 링크·앵커 공통 상한. 0이면 요약만 산다.
        max_age: 이 일수 안에 이미 샀으면 건너뛴다. 0이면 항상 산다.
        conn: 이미 열린 Brain 연결 — 주면 그것을 쓰고 닫지 않는다
        post: (path, body) -> (result, cost) — 주면 DataForSEO 대신 이것을 부른다

    Returns:
        StageResult. 유료 키 부재만 ok=False(skip). 신선도·dry-run 은 ok=True, skipped=True.
    """
    _parser()
    post = post or serp_adapter.post_dataforseo
    with collector.stage(project, conn=conn, dry_run=dry_run) as st:
        conn, p = st.conn, st.project
        if not serp_adapter.has_dataforseo():
            return st.skip("백링크는 DataForSEO 유료 키가 필요합니다 — "
                           "DATAFORSEO_LOGIN/DATAFORSEO_PASSWORD 를 설정하세요. "
                           "발급: https://dataforseo.com")

        s = st.settings(argparse.Namespace(limit=backlink_limit, max_age=max_age))
        limit = int(s["limits.backlink_limit"])
        max_age = int(s["backlink_max_age_days"])
        pid, today = p["id"], st.today
        target = (p["domain"] or "").strip().lower()

        if max_age > 0 and conn.execute(
                "SELECT 1 FROM backlink_summary WHERE project_id=? AND"
                " checked_date > date('now', ?) LIMIT 1",
                (pid, f"-{max_age} days")).fetchone():
            reason = (f"최근 {max_age}일 안에 이미 수집했습니다 — 백링크는 하루 단위로 "
                      f"움직이지 않습니다. 다시 사려면 `--max-age 0`.")
            print(f"[backlinks] {reason}")
            return st.noop(reason=reason)

        rivals = [str(r["domain"]).strip().lower() for r in conn.execute(
            "SELECT domain FROM competitors WHERE project_id=? ORDER BY id",
            (pid,)).fetchall() if r["domain"]]
        rivals = [d for d in rivals if d and d != target][:20]
        pos = {str(i + 1): d for i, d in enumerate(rivals)}

        rows = {}
        cost = 0.0
        notes: list[str] = []

        def _bump(name: str, n: int, c: float) -> None:
            nonlocal cost
            rows[name] = rows.get(name, 0) + n
            cost += float(c or 0)

        def _summary(_):
            res, c = post("/backlinks/summary/live",
                          [{"target": target, "internal_list_limit": 1}])
            _bump("summary", _put_summary(conn, pid, today, res), c)

        def _refdomains(_):
            res, c = post("/backlinks/referring_domains/live",
                          [{"target": target, "limit": limit, "order_by": ["rank,desc"]}])
            _bump("referring_domains", _put_refdomains(conn, pid, today, res), c)

        def _links(_):
            res, c = post("/backlinks/backlinks/live",
                          [{"target": target, "limit": limit, "mode": "one_per_domain",
                            "order_by": ["rank,desc"]}])
            _bump("backlinks", _put_backlinks(conn, pid, today, res), c)

        def _anchors(_):
            res, c = post("/backlinks/anchors/live",
                          [{"target": target, "limit": limit, "order_by": ["backlinks,desc"]}])
            _bump("anchors", _put_anchors(conn, pid, today, res), c)

        def _intersect(_):
            res, c = post("/backlinks/domain_intersection/live",
                          [{"targets": pos, "limit": limit, "exclude_targets": [target]}])
            _bump("intersect", _put_intersect(conn, pid, today, res, pos), c)

        tasks = [("summary", _summary)]
        if limit > 0:
            tasks += [("referring_domains", _refdomains), ("backlinks", _links),
                      ("anchors", _anchors)]
        else:
            notes.append("limit=0 — 요약만 삽니다 (참조 도메인·개별 링크·앵커 생략)")
        # 교집합은 경쟁사가 둘 이상일 때만 뜻이 있다. 없다고 단계를 죽이지는 않는다.
        if limit > 0 and len(rivals) >= INTERSECT_MIN_RIVALS:
            tasks.append(("intersect", _intersect))
        elif limit > 0:
            notes.append(f"Link Intersect 생략 — 경쟁사가 {len(rivals)}개뿐입니다"
                         f"(교집합은 {INTERSECT_MIN_RIVALS}개부터). "
                         "`/capture rank` 를 한 바퀴 돌리면 상위 도메인이 자동 적재됩니다.")

        est = len(tasks) * REQUEST_COST + limit * (3 + (1 if len(tasks) == 5 else 0)) * ROW_COST
        print(f"[backlinks] project={p['name']} target={target} limit={limit} "
              f"calls={len(tasks)} rivals={len(rivals)} est_cost≈${est:.3f}")
        for n in notes:
            print(f"  · {n}")

        if st.dry_run:
            for name, _ in tasks:
                print(f"  - {name}")
            print(f"단가 출처: DataForSEO Backlinks — 요청당 ${REQUEST_COST} + "
                  f"행당 ${ROW_COST} (모듈 docstring). 실제 청구액은 응답 cost 로 기록.")
            return st.noop(cost=est)

        with st.record("backlinks") as r:
            r.api_calls = st.each(tasks, lambda t: t[1](t), label=lambda t: t[0])
            r.cost = cost
            r.notes = (" ".join(f"{k}={v}" for k, v in sorted(rows.items()))
                       + f" errors={st.errors}"
                       + ("" if not notes else " | " + " | ".join(notes)))

        total = sum(rows.values())
        print(f"\ncollected {total} rows ({r.notes}), actual_cost=${cost:.4f}\n"
              f"run_id={r.id}")
        return st.done(rows=total, cost=cost)


def latest(project: str, top: int = 25) -> dict:
    """대시보드용 조회. 수집한 적이 없으면 빈 값을 돌려준다(없는 걸 지어내지 않는다).

    server/backlinks.py 가 re-export 한다 — 호스팅 화면(/api/backlinks)이 부르는 것이
    이 함수다. 반환 모양을 바꾸면 그 화면이 같이 바뀐다.
    """
    conn = db.connect()
    try:
        p = db.get_project(conn, project)
        s = conn.execute(
            "SELECT * FROM backlink_summary WHERE project_id=? ORDER BY checked_date DESC"
            " LIMIT 1", (p["id"],)).fetchone()
        if not s:
            return {"summary": None, "domains": [], "history": []}
        doms = conn.execute(
            "SELECT domain, rank, backlinks, dofollow, first_seen FROM referring_domains"
            " WHERE project_id=? AND checked_date=? ORDER BY rank DESC LIMIT ?",
            (p["id"], s["checked_date"], top)).fetchall()
        hist = conn.execute(
            "SELECT checked_date, backlinks, referring_domains FROM backlink_summary"
            " WHERE project_id=? ORDER BY checked_date DESC LIMIT 12", (p["id"],)).fetchall()
        return {"summary": dict(s), "domains": [dict(d) for d in doms],
                "history": [dict(h) for h in hist][::-1]}
    finally:
        conn.close()


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__)
    collector.add_common(ap)
    collector.add_setting(ap, "--limit", key="limits.backlink_limit", fallback=200, type=int,
                          help="참조 도메인·개별 링크·앵커 공통 상한. 0이면 요약만. 기본 200")
    collector.add_setting(ap, "--max-age", key="backlink_max_age_days", fallback=7, type=int,
                          help="이 일수 안에 이미 샀으면 건너뜀. 0이면 항상 산다. 기본 7")
    return ap


def main() -> None:
    """인자가 없으면 자기검사 — run_checks.py 가 이 관례로 진입점을 찾는다."""
    if len(sys.argv) == 1:
        return _selfcheck()
    a = _parser().parse_args()
    r = collect(a.project, dry_run=a.dry_run, backlink_limit=a.limit, max_age=a.max_age)
    if not r.ok and r.reason:
        sys.exit(r.reason)


# ── 자기검사 ─────────────────────────────────────────────────────────────────

def _fake_post(calls: list):
    """다섯 엔드포인트의 가짜 응답. 필드가 빠진 항목·못 읽는 항목을 일부러 섞는다."""
    def post(path, body):
        calls.append((path, body[0]))
        if path.endswith("/summary/live"):
            return [{"rank": 120, "backlinks": 340, "referring_domains": 55,
                     "referring_main_domains": 51, "broken_backlinks": 3,
                     "info": {"referring_links_attributes": {"dofollow": 300,
                                                             "nofollow": 40}}}], 0.024
        if path.endswith("/referring_domains/live"):
            return [{"items": [
                {"domain": "Shared.com", "rank": 90, "backlinks": 5, "dofollow": 4,
                 "first_seen": "2026-01-01"},
                {"domain": "d2.com"},                       # 필드가 거의 없다 — 살아야 한다
                {"rank": 10},                               # 도메인이 없다 — 건너뛴다
            ]}], 0.03
        if path.endswith("/backlinks/backlinks/live"):
            return [{"items": [
                {"url_from": "https://a.com/p", "url_to": "https://bt.com/x",
                 "anchor": "좋은 앵커", "domain_from": "a.com", "rank": 55,
                 "dofollow": True, "is_broken": False, "first_seen": "2026-02-02",
                 "last_seen": "2026-08-01"},
                {"page_from_url": "https://b.com/q", "page_to_url": "https://bt.com/404",
                 "text": "깨진 링크", "url_to_status_code": 404},   # 다른 필드명 + 파생 is_broken
                {"anchor": "끝이 없다"},                     # url 이 없다 — 건너뛴다
            ]}], 0.04
        if path.endswith("/anchors/live"):
            return [{"items": [
                {"anchor": "좋은 앵커", "backlinks": 12, "referring_domains": 4,
                 "dofollow": 9},
                {"anchor": "빈약", "backlinks": 1},          # referring_domains 없음
                {"anchor": "  "},                            # 빈 앵커 — 건너뛴다
            ]}], 0.05
        if path.endswith("/domain_intersection/live"):
            return [{"items": [
                {"1": {"domain": "shared.com", "rank": 70}, "2": {"domain": "shared.com"}},
                {"domain": "only-rivals.com", "rank": 61, "hits": 2},
            ]}], 0.06
        raise AssertionError(f"모르는 엔드포인트: {path}")
    return post


def _boom(*a, **kw):
    raise AssertionError("부르면 안 되는 자리에서 DataForSEO 를 불렀다")


def _selfcheck() -> None:
    """가짜 post 로 다섯 축의 적재·중복·교집합·신선도를 전부 확인 — 진짜 API 안 건드림."""
    import os
    import tempfile

    os.environ["CAPTURE_HOME"] = str(
        Path(tempfile.mkdtemp(prefix="seo-miner-backlinks-selftest-")))
    os.environ["DATAFORSEO_LOGIN"] = "login"      # 모킹 단계에서만 필요 — 실제 호출 0건
    os.environ["DATAFORSEO_PASSWORD"] = "pw"

    conn = db.connect()
    conn.execute("INSERT INTO projects(name, domain, locale) VALUES('bt','bt.com','ko-KR')")
    pid = db.get_project(conn, "bt")["id"]
    conn.executemany(
        "INSERT INTO competitors(project_id, domain, source) VALUES(?,?,'manual')",
        [(pid, "r1.com"), (pid, "r2.com")])
    conn.commit()

    def rows(table: str) -> list[dict]:
        return [dict(r) for r in conn.execute(f"SELECT * FROM {table}").fetchall()]

    # 1. 다섯 엔드포인트가 각각 맞는 표로 들어간다.
    calls: list = []
    res = collect("bt", conn=conn, post=_fake_post(calls), max_age=0)
    assert (res.ok, res.skipped) == (True, False), res
    assert [c[0] for c in calls] == [
        "/backlinks/summary/live", "/backlinks/referring_domains/live",
        "/backlinks/backlinks/live", "/backlinks/anchors/live",
        "/backlinks/domain_intersection/live"], calls
    assert abs(res.cost - 0.204) < 1e-9, res.cost         # 응답 cost 합산

    smry = rows("backlink_summary")
    assert len(smry) == 1 and smry[0]["backlinks"] == 340, smry
    assert (smry[0]["dofollow"], smry[0]["nofollow"]) == (300, 40), smry

    # 5. 응답에 없는 필드가 있어도 안 죽는다 — 값 없는 항목은 NULL 로 살아남고,
    #    키(도메인·url·앵커)가 없는 항목만 건너뛴다.
    rd = {r["domain"]: r for r in rows("referring_domains")}
    assert set(rd) == {"shared.com", "d2.com"}, rd        # 소문자 정규화 + 도메인 없는 항목 제외
    assert rd["d2.com"]["rank"] is None and rd["shared.com"]["rank"] == 90, rd

    bl = {r["url_to"]: r for r in rows("backlinks")}
    assert set(bl) == {"https://bt.com/x", "https://bt.com/404"}, bl
    assert bl["https://bt.com/x"]["anchor"] == "좋은 앵커"
    assert bl["https://bt.com/x"]["dofollow"] == 1 and bl["https://bt.com/x"]["is_broken"] == 0
    assert bl["https://bt.com/404"]["is_broken"] == 1, bl  # 404 → 파생
    assert bl["https://bt.com/404"]["domain_from"] == "b.com", bl  # url 에서 파생

    an = {r["anchor"]: r for r in rows("backlink_anchors")}
    assert set(an) == {"좋은 앵커", "빈약"}, an
    assert an["빈약"]["referring_domains"] is None, an

    # 4. we_have 는 우리 참조 도메인과 겹칠 때만 1.
    li = {r["domain"]: r for r in rows("link_intersect")}
    assert set(li) == {"shared.com", "only-rivals.com"}, li
    assert li["shared.com"]["we_have"] == 1, li            # referring_domains 에 있다
    assert li["only-rivals.com"]["we_have"] == 0, li       # 여기가 제안거리다
    assert li["shared.com"]["targets"] == "r1.com,r2.com", li
    assert li["shared.com"]["hits"] == 2 and li["shared.com"]["rank"] == 70, li

    run = conn.execute("SELECT * FROM runs WHERE kind='backlinks'").fetchone()
    assert "errors=0" in run["notes"], dict(run)           # 항목이 조용히 죽지 않았다
    assert run["api_calls"] == 5 and run["finished_at"], dict(run)

    # 2. 같은 날 두 번 돌려도 행이 안 늘어난다 (UNIQUE + ON CONFLICT).
    before = {t: len(rows(t)) for t in ("backlink_summary", "referring_domains",
                                        "backlinks", "backlink_anchors", "link_intersect")}
    collect("bt", conn=conn, post=_fake_post([]), max_age=0)
    after = {t: len(rows(t)) for t in before}
    assert before == after, (before, after)
    # ON CONFLICT 가 없으면 행은 안 늘지만 INSERT 가 터진다 — st.each 가 그 예외를
    # 세고 넘어가므로 행 수만 보면 통과해 버린다. 오류 수까지 본다.
    note = conn.execute("SELECT notes FROM runs WHERE kind='backlinks'"
                        " ORDER BY id DESC LIMIT 1").fetchone()["notes"]
    assert "errors=0" in note, note

    # 6. 신선도 안이면 재구매하지 않는다 — 지뢰 post 로 확인.
    res = collect("bt", conn=conn, post=_boom, max_age=7)
    assert (res.ok, res.skipped, res.cost) == (True, True, 0.0), res
    assert "이미 수집" in res.reason, res.reason

    # dry-run 은 호출도 기록도 없다.
    conn.execute("DELETE FROM runs")
    conn.commit()
    res = collect("bt", conn=conn, post=_boom, max_age=0, dry_run=True)
    assert (res.ok, res.skipped) == (True, True) and res.cost > 0, res
    assert rows("runs") == [], "dry-run 은 runs 에 아무것도 남기지 않아야 한다"

    # 3. 경쟁사 0~1개면 Link Intersect 만 건너뛰고 나머지는 적재된다.
    conn.execute("DELETE FROM competitors WHERE domain='r2.com'")
    conn.execute("DELETE FROM link_intersect")
    conn.execute("DELETE FROM backlink_summary")           # 신선도 통과용
    conn.commit()
    calls = []
    res = collect("bt", conn=conn, post=_fake_post(calls), max_age=0)
    assert res.ok and not res.skipped, res
    assert len(calls) == 4 and all("intersection" not in c[0] for c in calls), calls
    assert rows("link_intersect") == [], rows("link_intersect")
    assert len(rows("backlink_summary")) == 1 and len(rows("referring_domains")) == 2
    note = conn.execute("SELECT notes FROM runs WHERE kind='backlinks'"
                        " ORDER BY id DESC LIMIT 1").fetchone()["notes"]
    assert "Link Intersect 생략" in note, note             # 왜 건너뛰었는지 남긴다

    # limit=0 이면 요약만 산다.
    conn.execute("DELETE FROM backlink_summary")
    conn.commit()
    calls = []
    res = collect("bt", conn=conn, post=_fake_post(calls), max_age=0, backlink_limit=0)
    assert len(calls) == 1 and calls[0][0].endswith("/summary/live"), calls

    conn.execute("SELECT 1")            # 빌린 conn 은 러너가 닫지 않는다
    conn.close()
    print("collect_backlinks self-check ok")


if __name__ == "__main__":
    main()
