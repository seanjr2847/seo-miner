#!/usr/bin/env python3
"""키워드 지표 — 검색량·난이도·CPC (DataForSEO).

자동완성은 후보만 준다. 볼륨이 없으면 '아직 안 뜨는 검색어'의 가치를 못 매기고,
기회 점수가 이미 노출된 것만 보게 된다. keywords 표의 volume·difficulty·cpc 는
처음부터 있었지만 채우는 코드가 없어 늘 NULL 이었다 — 그 자리를 채운다.

흐름:
  1) 대상 — 이 프로젝트의 keywords 중 metrics_at 이 NULL 이거나 --max-age 보다
     오래된 행. is_active 는 안 본다 (후보의 가치를 재는 게 목적이다).
  2) 로케일별로 묶고, 1000개씩 청크로 쪼갠다 (DataForSEO 요청당 상한).
  3) 청크마다 두 번 친다 — Google Ads search_volume(볼륨·CPC) +
     Labs bulk_keyword_difficulty(난이도).
  4) 적재 — 받은 값만 UPDATE. 응답에 없는 키워드는 아예 안 건드린다
     (기존 값을 NULL 로 덮지 않는다).

인증: DATAFORSEO_LOGIN / DATAFORSEO_PASSWORD (collect_serp·collect_gap 과 같은 키).

비용 고지 (단가 출처: https://dataforseo.com/pricing):
  Keywords Data / Google Ads / Search Volume Live ≈ $0.05 / 요청(최대 1000키워드)
  DataForSEO Labs / Bulk Keyword Difficulty Live  ≈ $0.01 / 요청(최대 1000키워드)
  dry-run 고지용 추정치다 — 실청구액은 응답 cost 를 그대로 합산해 기록한다.

한계:
  · 볼륨은 Google Ads 의 월 평균 추정치다. 못 받은 키워드는 NULL 로 둔다(창작 금지).
  · 지표는 사서 오는 값이라 --max-age(기본 30일) 안쪽이면 다시 안 산다.

Usage:
  python collect_metrics.py --project NAME [--limit 500] [--max-age 30] [--dry-run]
  python collect_metrics.py                                    # self-check
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import collector  # noqa: E402
import db  # noqa: E402
import serp_adapter  # noqa: E402

# DataForSEO 요청당 키워드 상한 — 두 엔드포인트 모두 1000.
CHUNK = 1000
SV_PATH = "/keywords_data/google_ads/search_volume/live"
KD_PATH = "/dataforseo_labs/google/bulk_keyword_difficulty/live"
SV_COST_PER_CALL = 0.05     # dry-run 고지용 상한. 실청구액은 응답 cost.
KD_COST_PER_CALL = 0.01


# DataForSEO 는 키워드 하나에 금지 문자가 있으면 **묶음 전체**를 거절한다:
#   Invalid Field: 'keywords'. Keyword text has invalid characters or symbols:
#   'How much does stem cell exosome therapy cost?'
# 9/1 자동 런에서 500개 묶음 하나가 물음표 하나 때문에 통째로 날아가 updated=0
# 이었는데 로그는 "볼륨이 채워졌으니…" 라고 말했다. 그래서 경계에서 한 번 씻는다.
# 목록은 보수적으로 — 확실한 것(물음표)과 문장부호·따옴표·제어문자만.
BAD_CHARS = "?!\"'"
# 묶음이 이 문구로 거절되면 반으로 쪼개 재시도한다. 타임아웃 같은 것까지 이분하면
# 요청 수만 늘어나므로 "우리가 못 씻은 글자가 남았다" 류만 고른다.
REJECT_HINTS = ("invalid field", "invalid characters")


def clean(kw: str) -> str:
    """DataForSEO 로 보낼 모양. 금지 문자·제어문자를 공백으로 바꾸고 공백 정리.

    빈 문자열이 나오면 보낼 것이 없다는 뜻이다 — 호출부가 뺀다.
    """
    out = "".join(" " if (ch in BAD_CHARS or ord(ch) < 32) else ch for ch in kw)
    return " ".join(out.split())


def _ask(post, path: str, words: list[str], body_of) -> tuple[list[dict], float, list[str]]:
    """묶음 하나를 친다. 거절당하면 반으로 쪼개 재시도. 반환: (항목, 비용, 못 산 키워드).

    정제로 대부분 막히지만 우리가 모르는 금지 문자가 하나 남아도 500개가 같이
    날아가면 안 된다. 비용은 성공한 요청만 센다 — 거절된 요청은 청구되지 않는다.
    """
    try:
        result, cost = post(path, body_of(words))
        return _rows(result), cost, []
    except collector.Fatal:
        raise                       # 잔액·인증은 쪼개도 안 낫는다
    except Exception as e:
        if not any(h in str(e).lower() for h in REJECT_HINTS):
            raise
        if len(words) <= 1:
            return [], 0.0, list(words)
        mid = len(words) // 2
        a_items, a_cost, a_bad = _ask(post, path, words[:mid], body_of)
        b_items, b_cost, b_bad = _ask(post, path, words[mid:], body_of)
        return a_items + b_items, a_cost + b_cost, a_bad + b_bad


def _rows(result) -> list[dict]:
    """응답의 항목들. result 가 항목 배열이든 result[i]['items'] 든 둘 다 받는다.

    두 엔드포인트의 모양이 다르고(Keywords Data 는 result 직속, Labs 는 items),
    같은 엔드포인트도 버전에 따라 흔들린다. 못 읽는 모양이면 빈 리스트 —
    그 청크만 값이 안 들어가고 나머지는 산다.
    """
    out = []
    for r0 in result or []:
        if not isinstance(r0, dict):
            continue
        items = r0.get("items")
        if isinstance(items, list):
            out += [it for it in items if isinstance(it, dict)]
        else:
            out.append(r0)
    return out


def _find(obj: dict, key: str):
    """중첩 dict 어디에 있든 key 의 첫 값 (얕은 곳 우선).

    같은 값이 `search_volume` / `keyword_data.keyword_info.search_volume` /
    `keyword_info.search_volume` 로 온다 — 경로를 하나로 못 박으면 응답 모양이
    바뀌는 날 조용히 전부 NULL 이 된다.
    """
    queue = [obj]
    while queue:
        cur = queue.pop(0)
        if not isinstance(cur, dict):
            continue
        if key in cur and not isinstance(cur[key], (dict, list)):
            return cur[key]
        queue += [v for v in cur.values() if isinstance(v, dict)]
    return None


def _num(obj: dict, key: str, cast=float):
    v = _find(obj, key)
    if v is None or isinstance(v, bool):
        return None
    try:
        return cast(float(v))
    except (TypeError, ValueError):
        return None


def _kw(it: dict) -> str:
    return str(_find(it, "keyword") or "").strip()


def _cutoff(max_age_days: int) -> str:
    """이 시각보다 오래된 metrics_at 은 다시 산다. db.now() 와 같은 포맷."""
    return (datetime.now(timezone.utc)
            - timedelta(days=max_age_days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _targets(conn, project_id: int, cutoff: str, limit: int) -> list:
    """아직 지표가 없거나 오래된 키워드. 한 번도 안 산 것(NULL)이 먼저."""
    return conn.execute(
        """SELECT id, keyword, locale FROM keywords
            WHERE project_id=? AND (metrics_at IS NULL OR metrics_at < ?)
            ORDER BY (metrics_at IS NOT NULL), metrics_at, id
            LIMIT ?""",
        (project_id, cutoff, limit)).fetchall()


def _jobs(rows, default_locale: str) -> list[tuple[str, list]]:
    """(locale, rows[:CHUNK]) 목록 — 한 요청에 한 로케일, 최대 1000개."""
    by_locale: dict[str, list] = {}
    for r in rows:
        by_locale.setdefault(r["locale"] or default_locale, []).append(r)
    out = []
    for loc in sorted(by_locale):
        group = by_locale[loc]
        for i in range(0, len(group), CHUNK):
            out.append((loc, group[i:i + CHUNK]))
    return out


def collect(project: str, *,
            dry_run: bool = False,
            limit: int | None = None,
            max_age: int | None = None,
            conn=None,
            post=None,
            **_opts) -> collector.StageResult:
    """keywords 표의 volume·difficulty·cpc 를 채운다.

    Args:
        project: 사이트 이름
        dry_run: True 면 몇 개를 어느 로케일로 살지 + 예상 비용만 찍고 종료
        limit: 이번 실행에서 살 키워드 수 상한(config 키는 limits.metrics_limit).
            0 이면 끈다 — CLI 플래그(--limit)와 이름을 맞춘다.
        max_age: 이 일수 안에 산 지표는 다시 사지 않는다
        conn: 이미 열린 Brain 연결 — 주면 그것을 쓰고 닫지 않는다
        post: (path, body) -> (result, cost) — 주면 post_dataforseo 대신 이것을
              부른다 (자체점검이 갈아끼우는 자리)
    """
    ap = _parser()
    post = post or serp_adapter.post_dataforseo
    with collector.stage(project, conn=conn, dry_run=dry_run) as st:
        conn, p = st.conn, st.project
        if not serp_adapter.has_dataforseo():
            return st.skip("키워드 지표는 유료 — DATAFORSEO_LOGIN/PASSWORD 설정. "
                           "발급: https://dataforseo.com")

        s = st.settings(ap, argparse.Namespace(limit=limit, max_age=max_age))
        limit = s["limits.metrics_limit"]
        max_age_days = s["metrics_max_age_days"]
        if limit <= 0:
            return st.noop(reason="--limit 0 — 키워드 지표 수집을 껐습니다.")

        rows = _targets(conn, p["id"], _cutoff(max_age_days), limit)
        if not rows:
            reason = (f"'{p['name']}' 의 키워드 지표가 모두 {max_age_days}일 안쪽입니다 — "
                      "다시 살 것이 없습니다. 더 오래된 것까지 갱신하려면 --max-age 0.")
            print(f"[metrics] {reason}")
            return st.noop(reason=reason)

        jobs = _jobs(rows, p["locale"] or "ko-KR")
        est_cost = len(jobs) * (SV_COST_PER_CALL + KD_COST_PER_CALL)
        print(f"[metrics] project={p['name']} keywords={len(rows)} "
              f"requests={len(jobs) * 2} est_cost≈${est_cost:.3f}")
        for loc, group in jobs:
            serp_adapter.warn_unmapped(loc)   # 돈 쓰기 전에 — collect_gap 과 같은 자리
            print(f"  - {loc}: {len(group)} keywords")

        if st.dry_run:
            print(f"단가 출처: Google Ads search_volume ≈${SV_COST_PER_CALL}/req + "
                  f"Labs bulk_keyword_difficulty ≈${KD_COST_PER_CALL}/req (모듈 docstring). "
                  "실제 청구액은 응답 cost 로 기록.")
            return st.noop(cost=est_cost)

        total_cost = 0.0
        calls = 0
        updated = 0

        def one(job: tuple[str, list]) -> None:
            """청크 하나 — 죽으면 st.each 가 세고 다음 청크로 간다."""
            nonlocal total_cost, calls, updated
            loc, group = job
            loc_name, lang, _ = serp_adapter.location(loc)

            # 보내는 모양(정제본) ↔ Brain 의 행. 정제하면 글자가 달라지므로
            # 응답을 원문 keyword 로 되찾을 수 없다 — 그 다리를 여기서 놓는다.
            by_clean: dict[str, list] = {}
            for r in group:
                c = clean(r["keyword"])
                if c:
                    by_clean.setdefault(c, []).append(r)
            dropped = len(group) - sum(len(v) for v in by_clean.values())
            if dropped:
                st.fail(f"{loc}: 정제 후 빈 문자열이 된 키워드 {dropped}개는 건너뜁니다")
            if not by_clean:
                return
            words = sorted(by_clean)

            def body_of(ws):
                # 요청 수를 여기서 센다 — 이분 재시도로 한 청크가 여러 번 나갈 수
                # 있어서, 바깥에서 +1 하면 실제 호출 수와 어긋난다.
                nonlocal calls
                calls += 1
                return [{"keywords": ws, "location_name": loc_name, "language_code": lang}]

            items, cost, bad = _ask(post, SV_PATH, words, body_of)
            total_cost += cost
            metrics: dict[str, dict] = {}
            for it in items:
                kw = _kw(it)
                if kw:
                    metrics.setdefault(kw, {})["volume"] = _num(it, "search_volume", int)
                    metrics[kw]["cpc"] = _num(it, "cpc")

            # 난이도는 부차적이다 — 여기서 죽어도 이미 사 온 볼륨은 적재한다.
            try:
                items, cost, _ = _ask(post, KD_PATH, words, body_of)
                total_cost += cost
                for it in items:
                    kw = _kw(it)
                    if kw:
                        metrics.setdefault(kw, {})["difficulty"] = _num(
                            it, "keyword_difficulty")
            except collector.Fatal:
                raise
            except Exception as e:
                print(f"  ! {loc} 난이도 조회 실패 (볼륨은 적재됨): {e}", file=sys.stderr)

            if bad:
                # 끝까지 거절된 것들 — 이름을 적는다. 다음에 BAD_CHARS 를 넓힐 단서다.
                st.fail(f"{loc}: DataForSEO 가 끝까지 거절한 키워드 {len(bad)}개 — "
                        + ", ".join(bad[:3]) + ("…" if len(bad) > 3 else ""))

            stamp = db.now()
            got = 0
            for c, rows_ in by_clean.items():
                m = metrics.get(c) or {}
                if all(m.get(k) is None for k in ("volume", "difficulty", "cpc")):
                    continue        # 응답에 없거나 전부 빈 값 — 기존 값을 건드리지 않는다
                for r in rows_:
                    conn.execute(
                        """UPDATE keywords
                              SET volume     = COALESCE(?, volume),
                                  difficulty = COALESCE(?, difficulty),
                                  cpc        = COALESCE(?, cpc),
                                  metrics_at = ?
                            WHERE id=?""",
                        (m.get("volume"), m.get("difficulty"), m.get("cpc"), stamp, r["id"]))
                    got += 1
            updated += got
            print(f"  {loc}: asked={len(group)} filled={got}")

        with st.record("metrics") as r:
            st.each(jobs, one, label=lambda j: f"locale={j[0]} n={len(j[1])}")
            r.api_calls = calls
            r.cost = total_cost
            r.notes = (f"keywords={len(rows)} chunks={len(jobs)} updated={updated} "
                       f"{st.err_note}")

        print(f"\ncollected {len(rows)} keywords in {len(jobs)} chunks, "
              f"actual_cost=${total_cost:.3f} (updated={updated})\n"
              f"run_id={r.id}")
        # 다음 걸음은 **결과가 있을 때만** 말한다. updated=0 인데 "볼륨이 채워졌으니"
        # 라고 말하던 것이 9/1 실패를 성공처럼 보이게 만든 문장이다.
        if updated:
            print("Next: 볼륨이 채워졌으니 `/capture gaps` 로 기회 점수를 다시 매기세요.")
        else:
            print("채워진 볼륨이 없습니다 — 위 오류 줄을 먼저 보세요.", file=sys.stderr)
        return st.verdict(updated, rows=updated, cost=total_cost)


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__)
    collector.add_common(ap)
    collector.add_setting(ap, "--limit", key="limits.metrics_limit", fallback=500, type=int,
                          help="이번 실행에서 지표를 살 키워드 수 상한. 0 이면 끈다. 기본 500")
    collector.add_setting(ap, "--max-age", key="metrics_max_age_days", fallback=30, type=int,
                          help="이 일수 안에 산 지표는 다시 사지 않는다. 기본 30")
    return ap


def main() -> None:
    """인자가 없으면 자기검사 — run_checks.py 가 이 관례로 진입점을 찾는다."""
    if len(sys.argv) == 1:
        return _selfcheck()
    collector.cli("metrics")


def _selfcheck() -> None:
    """가짜 post_dataforseo 로 적재·신선도·청킹·로케일 분리를 전부 검증한다.

    진짜 Brain·DataForSEO 는 안 건드린다 (임시 CAPTURE_HOME + post 주입).
    """
    import contextlib
    import io
    import os
    import tempfile

    os.environ["CAPTURE_HOME"] = str(
        Path(tempfile.mkdtemp(prefix="seo-miner-metrics-selftest-")))
    os.environ["DATAFORSEO_LOGIN"] = "login"
    os.environ["DATAFORSEO_PASSWORD"] = "pw"

    conn = db.connect()
    conn.execute("INSERT INTO projects(name, domain, locale) VALUES('mt','mt.com','ko-KR')")
    p = conn.execute("SELECT * FROM projects WHERE name='mt'").fetchone()
    pid = p["id"]

    fresh = db.now()
    stale = "2000-01-01T00:00:00Z"
    conn.executemany(
        """INSERT INTO keywords(project_id, keyword, locale, source, volume, difficulty,
                                cpc, metrics_at) VALUES(?,?,?,'seed',?,?,?,?)""",
        [(pid, "볼륨 없는 키워드", None, None, None, None, None),
         (pid, "오래된 키워드", None, 11, 11.0, 1.1, stale),
         (pid, "신선한 키워드", None, 22, 22.0, 2.2, fresh),
         (pid, "응답에 없는 키워드", None, 33, 33.0, 3.3, None),
         (pid, "english keyword", "en-US", None, None, None, None)])
    conn.commit()

    calls: list[tuple[str, str, list[str]]] = []

    def fake_post(path, body):
        loc = body[0]["location_name"]
        words = body[0]["keywords"]
        calls.append((path, loc, words))
        if path == SV_PATH:
            # 응답 모양 A: result 직속 항목. "응답에 없는 키워드" 는 일부러 뺀다.
            return [{"keyword": w, "search_volume": 100 + i, "cpc": 0.5 + i}
                    for i, w in enumerate(words) if w != "응답에 없는 키워드"], 0.05
        # 응답 모양 B: result[0]['items'] + keyword_data 중첩.
        return [{"items": [
            {"keyword_data": {"keyword": w,
                              "keyword_properties": {"keyword_difficulty": 40 + i}}}
            for i, w in enumerate(words) if w != "응답에 없는 키워드"]}], 0.01

    res = collect("mt", conn=conn, post=fake_post)
    assert (res.ok, res.skipped) == (True, False), res
    conn.execute("SELECT 1")     # 빌린 conn 은 러너가 닫지 않는다

    got = {r["keyword"]: dict(r) for r in conn.execute(
        "SELECT keyword, volume, difficulty, cpc, metrics_at FROM keywords "
        "WHERE project_id=?", (pid,)).fetchall()}

    # 1. 볼륨·난이도·CPC 가 실제로 행에 들어간다.
    a = got["볼륨 없는 키워드"]
    assert a["volume"] and a["difficulty"] and a["cpc"] and a["metrics_at"], a
    assert got["오래된 키워드"]["volume"] != 11, got["오래된 키워드"]

    # 2. metrics_at 이 신선하면 다시 안 산다.
    assert got["신선한 키워드"] == {"keyword": "신선한 키워드", "volume": 22,
                                    "difficulty": 22.0, "cpc": 2.2, "metrics_at": fresh}, \
        got["신선한 키워드"]
    assert all("신선한 키워드" not in w for _, _, w in calls), calls

    # 3. 응답에 없는 키워드의 기존 값이 NULL 로 덮이지 않는다.
    assert got["응답에 없는 키워드"]["volume"] == 33, got["응답에 없는 키워드"]
    assert got["응답에 없는 키워드"]["metrics_at"] is None, got["응답에 없는 키워드"]

    # 5. 로케일이 섞이면 요청이 갈라진다 (한 요청에 한 로케일).
    locs = {loc for _, loc, _ in calls}
    assert locs == {"South Korea", "United States"}, calls
    for _, loc, words in calls:
        if loc == "United States":
            assert words == ["english keyword"], words
        else:
            assert "english keyword" not in words, words
    assert {path for path, _, _ in calls} == {SV_PATH, KD_PATH}
    assert res.cost > 0, res

    # 4. 1000개 상한 청킹 — 1200개면 로케일 하나가 두 요청으로 갈라진다.
    conn.executemany(
        "INSERT INTO keywords(project_id, keyword, locale, source) VALUES(?,?,'ko-KR','seed')",
        [(pid, f"대량 {i}") for i in range(1200)])
    conn.commit()
    calls.clear()
    res = collect("mt", limit=1200, conn=conn, post=fake_post)
    sv_calls = [w for path, _, w in calls if path == SV_PATH]
    assert len(sv_calls) == 2, [len(c) for c in sv_calls]
    assert [len(c) for c in sv_calls] == [1000, 200], [len(c) for c in sv_calls]
    assert res.rows == 1199, res     # "응답에 없는 키워드" 하나는 여전히 안 채워진다

    # 6. 경계 정제 — 물음표가 붙은 키워드가 요청에서 씻겨 나가고, 볼륨은
    #    **원문 행**에 들어간다(정제본으로 답이 오므로 매핑이 필요하다).
    #    (9/1: 물음표 하나가 500개 묶음을 통째로 날렸다)
    assert clean("stem cell cost?") == "stem cell cost"
    assert clean('a "b" c!') == "a b c"
    assert clean("???") == ""
    conn.execute("DELETE FROM keywords")
    conn.execute(
        "INSERT INTO keywords(project_id, keyword, locale, source) VALUES(?,?,?,'seed')",
        (pid, "how much does therapy cost?", "ko-KR"))
    conn.commit()
    calls.clear()
    res = collect("mt", conn=conn, post=fake_post)
    assert all("?" not in w for _, _, ws in calls for w in ws), calls
    row = conn.execute("SELECT volume FROM keywords WHERE project_id=?", (pid,)).fetchone()
    assert row["volume"], "정제본으로 받은 볼륨이 원문 행에 안 들어갔다"
    assert res.ok and res.rows == 1, res

    # 7. 그래도 거절되면 묶음을 반으로 쪼개 나머지를 살린다 — 한 개 때문에 전부
    #    잃지 않는다. 끝까지 거절되는 것은 이름과 함께 오류로 남고, 비용은
    #    성공한 요청만 센다.
    conn.execute("DELETE FROM keywords")
    conn.executemany(
        "INSERT INTO keywords(project_id, keyword, locale, source) VALUES(?,?,'ko-KR','seed')",
        [(pid, f"kw{i}") for i in range(4)] + [(pid, "독약")])
    conn.commit()

    def picky_post(path, body):
        ws = body[0]["keywords"]
        if "독약" in ws:
            raise RuntimeError("Invalid Field: 'keywords'. Keyword text has "
                               "invalid characters or symbols: '독약'")
        if path == SV_PATH:
            return [{"keyword": w, "search_volume": 7, "cpc": 0.1} for w in ws], 0.05
        return [{"items": []}], 0.01

    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        res = collect("mt", conn=conn, post=picky_post)
    filled = conn.execute("SELECT COUNT(*) c FROM keywords WHERE volume IS NOT NULL "
                          "AND project_id=?", (pid,)).fetchone()["c"]
    assert filled == 4, f"이분 재시도로 나머지 4개가 들어와야 한다: {filled}"
    assert "독약" in err.getvalue(), err.getvalue()
    assert res.ok and res.partial, res      # 일부 실패는 완료(노란 표시)
    # 거절된 요청은 청구되지 않는다 — 성공한 3+3 건만 (SV 0.05×3 + KD 0.01×3)
    assert round(res.cost, 4) == 0.18, res

    # 8. 전부 실패면 완료가 아니다 (errors>0, updated=0 → ok=False).
    conn.execute("DELETE FROM keywords")
    conn.execute("INSERT INTO keywords(project_id, keyword, locale, source) "
                 "VALUES(?,'독약','ko-KR','seed')", (pid,))
    conn.commit()
    with contextlib.redirect_stderr(io.StringIO()):
        res = collect("mt", conn=conn, post=picky_post)
    assert (res.ok, res.rows) == (False, 0), res
    assert "독약" in res.reason, res

    # 9. 402 는 Fatal 이라 묶음을 쪼개지도, 다음 청크로 넘어가지도 않는다.
    conn.execute("DELETE FROM keywords")
    conn.executemany(
        "INSERT INTO keywords(project_id, keyword, locale, source) VALUES(?,?,'ko-KR','seed')",
        [(pid, f"z{i}") for i in range(3)])
    conn.commit()
    hits = []

    def broke_post(path, body):
        hits.append(path)
        raise collector.Fatal("DataForSEO 잔액 없음(402) — 충전하세요")

    try:
        with contextlib.redirect_stderr(io.StringIO()):
            collect("mt", conn=conn, post=broke_post)
        raise AssertionError("Fatal 을 삼켰다")
    except collector.Fatal as e:
        assert "402" in str(e), e
    assert len(hits) == 1, f"Fatal 뒤로도 계속 물었다: {hits}"

    conn.execute("DELETE FROM keywords")
    conn.executemany(
        """INSERT INTO keywords(project_id, keyword, locale, source, metrics_at)
           VALUES(?,?,'ko-KR','seed',?)""", [(pid, f"q{i}", db.now()) for i in range(3)])
    conn.commit()

    # dry-run 은 호출도 적재도 안 한다 — 지뢰 post 로 확인.
    def boom(*a, **kw):
        raise AssertionError("dry-run 이 DataForSEO 를 불렀다")

    conn.execute("DELETE FROM runs")
    conn.execute("UPDATE keywords SET metrics_at=NULL")
    conn.commit()
    res = collect("mt", dry_run=True, conn=conn, post=boom)
    assert (res.ok, res.skipped) == (True, True) and res.cost > 0, res
    assert conn.execute("SELECT * FROM runs").fetchall() == []

    # --limit 0 은 끄기.
    res = collect("mt", limit=0, conn=conn, post=boom)
    assert (res.ok, res.skipped) == (True, True) and res.reason, res

    # 살 것이 없으면 건너뜀 (실패 아님) — run_all 체인을 깨지 않는다.
    conn.execute("UPDATE keywords SET metrics_at=?", (db.now(),))
    conn.commit()
    res = collect("mt", conn=conn, post=boom)
    assert (res.ok, res.skipped) == (True, True) and res.reason, res

    # 유료 키가 없으면 skip (ok=False) — run_all 이 "키 없어 건너뜀"으로 읽는 자리.
    del os.environ["DATAFORSEO_LOGIN"]
    res = collect("mt", conn=conn, post=boom)
    assert (res.ok, res.skipped) == (False, True) and res.reason, res
    os.environ["DATAFORSEO_LOGIN"] = "login"

    conn.close()
    print("collect_metrics self-check ok")


if __name__ == "__main__":
    main()
