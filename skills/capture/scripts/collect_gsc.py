#!/usr/bin/env python3
"""Sync Google Search Console data into the Brain (free, real measurements).

Known holes (read reports accordingly, see references/scoring.md):
  * ~2-3 day data delay; low-volume longtail queries are privacy-filtered out
  * position is an AVERAGE across impressions, not a pinpoint rank

Auth: 열쇠는 한 벌이다 — 벌크 수집(이 파일)·즉석 조회(gsc_query.py)·색인
  검사(collect_index.py)가 같은 토큰을 쓴다.
  1) 기본(OAuth): 브라우저 로그인 한 번. 토큰은 $CAPTURE_HOME/gsc_token.json.
     클라이언트는 플러그인 동봉본이라 구글 클라우드 콘솔 작업이 없다.
  2) 무인 수집(서비스 계정): $CAPTURE_HOME/gsc_service_account.json
     설치: python ../../setup/scripts/connect_gsc.py  (setup.md 4-B)
Setup walkthrough: references/setup.md

Usage:
  python collect_gsc.py --project NAME [--days 28] [--row-limit 25000]
                        [--breakdown device,country] [--dry-run]
"""
import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import collector  # noqa: E402
import db  # noqa: E402
import scoring  # noqa: E402

SCOPES = ["https://www.googleapis.com/auth/webmasters.readonly"]
PAGE = 25000   # Search Analytics API가 요청 한 번에 주는 최대 행 수 (그 이상은 startRow)


def _oauth_service():
    """내 구글 계정으로 로그인해서 서비스 객체를 만든다 — 토큰은 우리가 보관한다.

    예전에는 이 자리를 gsc MCP 서버(mcp-search-console)의 인증 해석기가 대신 했다.
    그 패키지를 걷어내면서 로그인도 우리 것이 됐다 — 하는 일은 같다:
    토큰이 있으면 쓰고, 만료면 갱신하고, 없으면 브라우저를 한 번 연다.

    이미 예전 자리(db.gsc_token_legacy)에 로그인해 둔 사람은 다시 로그인시키지
    않는다 — 형식이 google-auth 표준이라 그대로 읽어 쓰고, 새 자리에 다시 쓴다.
    """
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    creds = None
    src = db.gsc_token_file()
    if src is not None:
        try:
            creds = Credentials.from_authorized_user_file(str(src), SCOPES)
        except (ValueError, OSError):
            creds = None            # 손상된 토큰은 없는 것으로 치고 다시 로그인한다
    if creds and not creds.valid and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception:
            creds = None            # 취소·스코프 변경 등 — 로그인을 다시 받는다
    if not creds or not creds.valid:
        client = db.gsc_oauth_client()
        if not client.exists():
            return None
        try:
            from google_auth_oauthlib.flow import InstalledAppFlow
        except ImportError:
            sys.exit("구글 로그인 부품이 없습니다 — 먼저 실행: "
                     "pip install google-auth-oauthlib")
        print("[gsc] 브라우저에서 구글 로그인 창이 한 번 열립니다 "
              "(계정당 1회, 이후에는 토큰을 재사용합니다).")
        creds = InstalledAppFlow.from_client_secrets_file(
            str(client), SCOPES).run_local_server(port=0)
    # 갱신된 access token 도 남긴다 — 매번 refresh 왕복을 하지 않기 위해서다.
    tok = db.gsc_token()
    tok.parent.mkdir(parents=True, exist_ok=True)
    tok.write_text(creds.to_json(), encoding="utf-8")
    return build("searchconsole", "v1", credentials=creds, cache_discovery=False)


def get_service():
    """지금 걸린 인증으로 서치콘솔 서비스 객체 하나. 판정 정본은 db.gsc_auth().

    벌크 수집(이 파일)·색인 검사(collect_index)·즉석 조회(gsc_query)가 전부
    이 함수 하나를 부른다 — 열쇠는 한 벌이라는 원칙의 실행체다.
    """
    try:
        from googleapiclient.discovery import build
    except ImportError:
        sys.exit("구글 연동 부품이 없습니다 — 먼저 실행: "
                 "pip install google-api-python-client")

    mode = db.gsc_auth()

    # 서비스 계정만 걸려 있으면 직결한다 — 읽기 전용 스코프에 무인 실행이라
    # 브라우저 로그인 경로를 아예 타지 않는다.
    if mode == "service_account":
        from google.oauth2 import service_account
        creds = service_account.Credentials.from_service_account_file(
            str(db.gsc_key()), scopes=SCOPES)
        return build("searchconsole", "v1", credentials=creds,
                     cache_discovery=False)

    # 기본은 OAuth — 토큰이 아직 없으면 여기서 브라우저 로그인이 한 번 열린다.
    if mode == "oauth":
        svc = _oauth_service()
        if svc is not None:
            return svc

    sys.exit("구글 서치콘솔 인증이 없습니다 — 로그인 한 번이면 끝납니다.\n"
             f"  · 기본(OAuth): {db.gsc_oauth_client()} 에 클라이언트 파일이 없습니다 —\n"
             "    python ../../setup/scripts/connect_gsc.py  (setup.md 4-B)\n"
             "  · 무인 수집(서비스 계정)을 쓰신다면 같은 스크립트가 그쪽 키도 받습니다.")


def _query(service, prop: str, body: dict) -> dict:
    """Search Analytics 호출 한 번 + 403 안내.

    호출이 한 곳(query×page)뿐일 땐 try 블록 안에 안내문을 그냥 적어 뒀는데,
    일별·분해 호출이 붙으면서 그 두 개만 원인 없는 HttpError 로 죽게 됐다.
    권한 문제는 어느 호출에서 터지든 원인이 같으니 안내도 한 곳에 모은다.
    """
    from googleapiclient.errors import HttpError
    try:
        return service.searchanalytics().query(siteUrl=prop, body=body).execute()
    except HttpError as e:
        if getattr(e.resp, "status", None) == 403 and db.gsc_key().exists():
            email = json.loads(db.gsc_key().read_text(encoding="utf-8")) \
                .get("client_email", "?")
            sys.exit(f"권한 거부: {prop}\n"
                     "  Search Console → 설정 → 사용자 및 권한에 서비스 계정 "
                     f"이메일을 추가했는지 확인하세요: {email}\n"
                     "  (권한 '제한된 사용자'면 충분합니다)")
        raise


def collect(project: str, *,
            dry_run: bool = False,
            row_limit: int = 100000,
            gsc_days: int | None = None,
            gsc_breakdown: str | None = None) -> collector.StageResult:
    """GSC 실측을 Brain 에 적재한다. sys.exit 호출 없음 — 결과를 StageResult 로 반환.

    Args:
        project: 사이트 이름 (project yaml 의 name)
        dry_run: True 면 호출 계획만 찍고 종료
        row_limit: 가져올 최대 행 수 (25,000행씩 나눠 받는다)
        gsc_days: 수집 창 (오늘-3일 기준 N일)
        gsc_breakdown: 분해 수집 차원 (콤마 구분 문자열)

    Returns:
        StageResult(ok=...) — 정상 종료면 ok=True. 인증 누락·구성 누락은
        ok=False, skipped=True, reason 에 한국어 안내.
    """
    _parser()
    conn, p, cfg = collector.open_project(project)
    ns = argparse.Namespace(
        days=gsc_days,
        breakdown=gsc_breakdown,
    )
    s = collector.settings(ns, cfg)
    days = s["gsc_days"]
    breakdown = s["gsc_breakdown"]
    dims = [d.strip().lower() for d in (breakdown or "").split(",") if d.strip()]
    prop = p["gsc_property"]
    if not prop:
        conn.close()
        return collector.StageResult(ok=False, skipped=True,
                                     reason="project yaml has no gsc_property "
                                            "(e.g. 'sc-domain:example.com' or 'https://example.com/'). "
                                            "Add it, then: python db.py sync-project <yaml>")

    end = date.today() - timedelta(days=3)     # GSC delay buffer
    start = end - timedelta(days=days)
    print(f"[gsc] {prop}  window {start} ~ {end} (period={days}d) "
          f"rowLimit={row_limit}")
    # 비용 고지 — dry-run 이 말한 호출 수와 실제 호출 수가 어긋나면 안 된다.
    # query×page 만 "최대"다: 마지막 장이 덜 차면 거기서 멈추므로 실제로는 더 적을 수 있다.
    qp_max = max(1, -(-row_limit // PAGE))
    print(f"[gsc] API 호출 계획: query×page 최대 {qp_max}회 + 일별 1회 + "
          f"분해 {len(dims)}회({', '.join(dims) or '없음'}) = 최대 {qp_max + 1 + len(dims)}회")
    if dry_run:
        conn.close()
        return collector.StageResult(ok=True, skipped=True, rows=0)

    try:
        service = get_service()
    except db.ProjectNotFound as e:
        conn.close()
        return collector.StageResult(ok=False, skipped=True, reason=str(e))
    rows: list = []
    calls = 0
    # API는 한 번에 최대 PAGE행만 준다 — 그 이상은 startRow로 이어받아야 한다.
    # 예전엔 한 번만 불러서, 큰 사이트가 정확히 25,000행에서 조용히 잘렸다.
    while len(rows) < row_limit:
        want = min(PAGE, row_limit - len(rows))
        resp = _query(service, prop, {
            "startDate": str(start), "endDate": str(end),
            "dimensions": ["query", "page"], "rowLimit": want,
            "startRow": len(rows), "dataState": "final"})
        batch = resp.get("rows", [])
        rows += batch
        calls += 1
        if len(batch) < want:          # 마지막 장 — 더 없다
            break
        print(f"[gsc] … {len(rows)} rows")
    print(f"[gsc] fetched {len(rows)} query x page rows")
    if len(rows) >= row_limit:
        print(f"[주의] --row-limit({row_limit})에 걸려 여기서 멈췄습니다 — "
              "더 필요하면 값을 올리세요. 지금 스냅샷은 전체가 아닙니다.")

    qp_calls = calls

    # 여기서부터는 **부수 호출**이다 — 실패해도 본체 스냅샷을 죽이지 못하게 한다.
    # 예전엔 본체 루프가 끝나면 곧바로 저장이라 이 문제가 없었는데, 뒤에 호출이
    # 둘 붙으면서 그중 하나만 실패해도 이미 받아 둔 query×page 를 통째로 잃게 됐다.
    # 실패한 축은 **쓰지 않고 건너뛴다** — 빈 값으로 쓰면 delete-then-insert 가
    # 지난번 수집분까지 지운다.
    def _optional(label: str, body: dict):
        nonlocal calls
        from googleapiclient.errors import HttpError
        try:
            resp = _query(service, prop, body)
        except (HttpError, db.ProjectConfigNotFound) as e:
            # 부수 호출의 실패도 본체를 죽이면 안 된다 — 이미 받아 둔 query×page 를
            # 통째로 잃지 않게 여기서 잡아 건너뛴다.
            print(f"[경고] {label} 수집을 건너뜁니다 ({e}) — "
                  "본체 스냅샷은 그대로 저장됩니다.", file=sys.stderr)
            return None
        calls += 1
        return resp.get("rows", [])

    # 창(window) 안의 날짜별 추이. query×page 요청에 date 를 끼우면 같은 조합이
    # 날짜 수만큼 쪼개져 행이 28배로 터지고 rowLimit 벽에 훨씬 먼저 닿는다 —
    # 그래서 dimensions=["date"] 단독으로 따로 부른다. 응답이 창 길이(28행 남짓)라
    # 호출 1회로 끝나고 비용이 사실상 없다. 그래서 옵션이 아니라 항상 한다.
    drows = _optional("일별 추이", {
        "startDate": str(start), "endDate": str(end),
        "dimensions": ["date"], "rowLimit": days + 10, "dataState": "final"})
    if drows is not None:
        print(f"[gsc] fetched {len(drows)} daily rows")

    # device/country 분해. 응답 행의 keys 순서는 요청한 dimensions 순서와 같다 →
    # keys[0]=dim_value, keys[1]=query.
    # startRow 페이지네이션은 하지 않는다 — 분해는 "모바일이 어디서 밀리나" 같은
    # 상위 분포만 보면 되는데, 페이지를 넘기기 시작하면 호출 수가 차원 수만큼
    # 곱해져서 정작 본체(query×page)보다 비싸진다.
    bd: list[tuple[str, list]] = []
    for dim in dims:
        brows = _optional(f"{dim} 분해", {
            "startDate": str(start), "endDate": str(end),
            "dimensions": [dim, "query"], "rowLimit": PAGE, "dataState": "final"})
        if brows is None:
            continue
        bd.append((dim, brows))
        print(f"[gsc] fetched {len(brows)} {dim} x query rows")
        if len(brows) >= PAGE:
            # 페이지를 안 넘기기로 한 대가다 — 잘렸으면 잘렸다고 말한다.
            print(f"[주의] {dim} 분해가 {PAGE:,}행에서 잘렸습니다 — "
                  "노출 상위 분포만 담겼습니다.")

    snap = str(date.today())
    with db.run(conn, p["id"], "gsc") as r:
        db.write_gsc_snapshot(conn, p["id"], snap, days,
                              ((x["keys"][0], x["keys"][1], x.get("clicks"),
                                x.get("impressions"), x.get("ctr"), x.get("position"))
                               for x in rows))
        if drows is not None:
            db.write_gsc_daily(conn, p["id"],
                               ((x["keys"][0], x.get("clicks"), x.get("impressions"),
                                 x.get("ctr"), x.get("position"))
                                for x in drows))
        for dim, brows in bd:
            db.write_gsc_breakdown(conn, p["id"], snap, days, dim,
                                   ((x["keys"][0], x["keys"][1], x.get("clicks"),
                                     x.get("impressions"), x.get("ctr"), x.get("position"))
                                    for x in brows))
        r.api_calls = calls
        # notes 의 pages 는 query×page 페이지 수를 뜻했다 — 이제 호출이 세 종류라
        # calls 와 다르다. 둘을 따로 적는다. 건너뛴 축은 'skip' 으로 남긴다 —
        # 나중에 "그날 왜 추이가 비었나"를 여기서 답할 수 있어야 한다.
        r.notes = (f"rows={len(rows)} pages={qp_calls} "
                   f"daily={'skip' if drows is None else len(drows)} "
                   + "".join(f"{d}={len(br)} " for d, br in bd)
                   + "".join(f"{d}=skip " for d in dims if d not in dict(bd))
                   + f"calls={calls} window={start}~{end}")

    print(f"saved snapshot {snap}. striking-distance preview "
          "(pos 4~20, impressions desc):")
    for s in scoring.striking(conn, p["id"], snap, limit=10,
                              brands=scoring.foreign_brands(conn, p["id"], cfg)):
        print(f"  {s['pos']:>5}  imp={s['imp']:<6} clk={s['clk']:<4} "
              f"gap={s['gap']:<5} {s['query']}")
    conn.close()
    return collector.StageResult(ok=True, skipped=False, rows=len(rows))


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser()
    collector.add_common(ap)
    collector.add_setting(ap, "--days", key="gsc_days", fallback=28, type=int)
    # 콤마 구분 문자열이다 — add_setting 의 type_fn 이 스칼라만 다루므로 리스트를 못 받는다.
    collector.add_setting(ap, "--breakdown", key="gsc_breakdown", fallback="device",
                          type=str,
                          help="분해 수집 차원 (콤마 구분: device,country / 빈 문자열이면 안 함)")
    ap.add_argument("--row-limit", type=int, default=100000,
                    help="가져올 최대 행 수 (25,000행씩 나눠 받는다)")
    return ap


def main() -> None:
    try:
        a = _parser().parse_args()
        r = collect(a.project, dry_run=a.dry_run, row_limit=a.row_limit,
                    gsc_days=a.days, gsc_breakdown=a.breakdown)
    except db.ProjectNotFound as e:
        sys.exit(str(e))
    if not r.ok and r.reason:
        sys.exit(r.reason)


if __name__ == "__main__":
    main()
