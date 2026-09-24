#!/usr/bin/env python3
"""Rank snapshot collection via SERP adapter (F3).

Per active keyword: fetch SERP -> record own position, top-10, features,
AI-Overview flags + the domains the AI Overview cited, and the PAA/related
questions of *that* keyword (serp_questions — 요청문의 "함께 답해야 할 질문").
Free byproducts are harvested by default:
  * related searches + PAA  -> keyword candidates (source='serp', is_active=0)
  * domains in the top-N of many checked keywords -> competitors (source='auto_rank');
    the rule (share threshold, platforms excluded, cap) is scoring.serp_rivals

"내 도메인인가"는 여기 한 곳에서만 판정한다(scoring.owns). 어댑터와 호출부가
같은 규칙을 각자 들고 있던 시절엔 서브도메인 취급이 조용히 갈렸다.

Locale is resolved per keyword (keywords.locale, falling back to
projects.locale). A bilingual project used to send every keyword at the single
project locale, which silently stored "not ranking" for the other language.

Cost guardrails: --dry-run prints the plan (단가는 어댑터가 답한다);
DataForSEO actual billed cost is read from responses and recorded in
runs.cost_estimate_usd.

Usage:
  python collect_serp.py --project NAME [--provider dataforseo|serper]
                         [--max-keywords N] [--ids 3,7,9] [--depth 10]
                         [--throttle 0.5] [--no-harvest] [--dry-run]
  --depth / --throttle 를 안 주면 skill_config defaults(serp_depth·throttle)를 쓴다.
"""
import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import collector  # noqa: E402
import db  # noqa: E402
import scoring  # noqa: E402
import serp_adapter  # noqa: E402


# 대기열로 잴 때 결과를 기다리는 상한(초). 넘기면 단계는 **실패**이고 이번 묶음에서
# 온 것도 적지 않는다 — 반만 잰 표가 "이번 주 순위"처럼 읽히면 안 된다(이전 측정이
# 그대로 남는다). 맡긴 id 는 serp_tasks 에 남아 다음 런이 새로 맡기지 않고 받아 온다.
WAIT_LIMIT_S = 600
# 시계·잠 — 검사가 가짜로 갈아 끼운다(10분을 진짜로 기다리지 않는다).
_clock = time.monotonic
_sleep = time.sleep

# 열린 기회의 상태 정본은 scoring.OPEN_STATUSES 다(여기 사본을 적지 않는다).
# 순서: ① 열린 기회의 대상 검색어 ② 한 번도 안 잰 것 ③ 가장 오래전에 잰 것 ④ id.
# 상한(limits.max_keywords)에 걸리면 뒤쪽이 잘린다 — 여태는 `ORDER BY id LIMIT 100` 이라
# 늘 같은 100개만 재고 나머지 132개는 한 번도 안 쟀다(2026-09-24 theotherskin).
_TARGETS_SQL = """
SELECT k.id, k.keyword, k.locale,
       EXISTS(SELECT 1 FROM opportunities o
               WHERE o.project_id = k.project_id AND o.status IN ({open_ph})
                 AND lower(trim(o.target)) = lower(trim(k.keyword))) AS has_opp,
       (SELECT MAX(datetime(s.checked_at)) FROM rank_snapshots s
         WHERE s.keyword_id = k.id) AS last_checked
  FROM keywords k
 WHERE k.project_id = ? AND k.is_active = 1
 ORDER BY has_opp DESC, last_checked IS NOT NULL, last_checked, k.id
 LIMIT ?"""


def targets(conn, project_id: int, limit: int) -> list:
    """이번 런이 잴 추적 검색어 — 순서와 상한은 _TARGETS_SQL 의 주석."""
    open_st = tuple(scoring.OPEN_STATUSES)
    return conn.execute(_TARGETS_SQL.format(open_ph=",".join("?" * len(open_st))),
                        (*open_st, project_id, limit)).fetchall()


def collect(project: str, *,
            dry_run: bool = False,
            provider: str | None = None,
            max_keywords: int | None = None,
            depth: int | None = None,
            throttle: float | None = None,
            device: str | None = None,
            no_harvest: bool = False,
            ids: str | None = None,
            force: bool = False,
            conn=None) -> collector.StageResult:
    """SERP 순위 스냅샷 + 부산물(키워드 후보·경쟁사)을 Brain 에 적재한다.

    Args:
        project: 사이트 이름
        dry_run: True 면 호출 계획만 찍고 종료
        provider: dataforseo|serper — 미지정시 환경에서 자동 판정
        max_keywords: 한 런의 상한 (limits.max_keywords, 기본 db.RANK_KEYWORDS_CAP)
        depth: top-N 깊이(config 키는 serp_depth) — CLI 플래그(--depth)와 이름을 맞춘다.
        throttle: 요청 간격(초) — 한 건씩 재는 제공자(serper)와 상위 글 열기에만
        device: desktop|mobile
        no_harvest: True 면 부산물(키워드 후보·경쟁사) 적재 안 함
        ids: 쉼표 구분 keyword id — 지정하면 is_active 무시하고 이것만
        force: 오늘 이미 확인한 키워드도 재확인
        conn: 이미 열린 Brain 연결 — 주면 그것을 쓰고 닫지 않는다

    Returns:
        StageResult(ok=...). 사유 있는 비종료는 ok=True, skipped=True.
        대기열이 상한 안에 다 안 오면 실패(아무것도 안 적는다).
    """
    ap = _parser()
    with collector.stage(project, conn=conn, dry_run=dry_run) as st:
        conn, p = st.conn, st.project
        s = st.settings(ap, argparse.Namespace(
            max_keywords=max_keywords, depth=depth,
            throttle=throttle, device=device))
        provider = provider or serp_adapter.detect_provider()
        if not provider:
            return st.skip("SERP 키 없음 — DATAFORSEO_LOGIN/PASSWORD 또는 SERPER_API_KEY 설정. "
                           "발급: https://dataforseo.com (권장) 또는 "
                           "https://serper.dev")
        depth = s["serp_depth"]
        throttle = st.throttle
        limit = s["max_keywords"]
        device = (s["device"] or "desktop").strip().lower()
        queued = serp_adapter.queued(provider)

        if device not in ("desktop", "mobile"):
            return st.skip(f"유효하지 않은 device '{device}' — desktop 또는 mobile만 허용됩니다")

        if device != "desktop":
            print("[경고] 모바일 측정 시 직전 스냅샷(desktop)과의 순위 비교(Δ)가 한 번 왜곡될 수 있습니다.",
                  file=sys.stderr)

        if ids:
            # 부분 실행. 이게 없으면 "이 몇 개만 다시" 하려고 is_active를 직접 토글하게
            # 되는데, 되돌릴 때 통째로 UPDATE 해버려 큐레이션한 활성 집합이 날아간다.
            ids_list = [int(x) for x in ids.split(",") if x.strip()]
            target_kws = conn.execute(
                f"""SELECT id, keyword, locale FROM keywords
                     WHERE project_id=? AND id IN ({','.join('?' * len(ids_list))}) ORDER BY id""",
                (p["id"], *ids_list)).fetchall()
        else:
            target_kws = targets(conn, p["id"], limit)
        if not target_kws:
            return st.skip("활성 키워드 없음 — /capture keywords 로 유니버스부터 구축")

        # 오늘 이미 확인한 키워드는 재실행 비용 절감을 위해 건너뛴다 (--force로 무시 가능).
        # '오늘'을 만드는 자리와 --force 의 뜻은 러너가 갖는다 (collector.Stage.seen_today).
        checked_today = st.seen_today(
            "SELECT keyword_id FROM rank_snapshots "
            f"WHERE {collector.today_clause('checked_at')}", force=force)
        kws = [k for k in target_kws if k["id"] not in checked_today]
        skipped = len(target_kws) - len(kws)

        # 키워드별 로케일. 어떤 로케일로 조회하는지 보여주지 않으면, 한국어 키워드를
        # en-US로 긁어 전부 "순위 없음"으로 적재해도 아무도 눈치채지 못한다.
        default_locale = db.project_locale(p)
        locales = Counter((k["locale"] or default_locale) for k in (kws if kws else target_kws))
        # 대기열은 앞 런이 맡겨 둔 과제를 먼저 받는다 — 그 몫은 이번에 돈이 안 든다.
        reuse = _reusable(conn, p["name"], kws, default_locale, depth, device) if queued else {}
        est = serp_adapter.cost_per_query(provider) * (len(kws) - len(reuse))
        took = (f"대기열 — 보통 1~2분, 최대 {WAIT_LIMIT_S // 60}분" if queued else
                f"~{len(kws) * (throttle + 2) / 60:.0f} min")
        print(f"[serp] provider={provider} keywords={len(kws)}{st.skip_note(skipped)} "
              f"depth={depth} device={device} "
              + (f"reuse={len(reuse)} " if reuse else "")
              + f"est_cost≈${est:.2f} ({took})")
        if locales:
            print("       locales: " + ", ".join(f"{loc}×{n}" for loc, n in locales.most_common()))
            for loc in locales:   # 매핑 없는 로케일 경고는 여기서 떠야 한다 — 돈을 쓰기 전에
                serp_adapter.warn_unmapped(loc)
        for note in serp_adapter.caveats(provider):
            print(f"       note: {note}")
        if st.dry_run:
            return st.noop(cost=est)

        if not kws:
            print(f"\nsaved 0 snapshots{st.skip_note(skipped)} "
                  f"actual_cost=$0.000 | 부산물: 키워드 후보 +0, 경쟁사 +0")
            return st.noop(rows=0, cost=0.0)

        own = p["domain"]
        total_cost = 0.0
        domain_hits: Counter = Counter()
        harvested_kw = set()
        outline_urls: list[str] = []
        new_kw = new_comp = 0

        def write(row, res) -> None:
            """키워드 하나의 응답을 쓴다 — 한 건씩 재든 대기열로 재든 같은 한 벌."""
            nonlocal total_cost
            kw_locale = row["locale"] or default_locale
            at = res.get("checked_at")      # 대기열: 구글을 실제로 본 시각. 없으면 지금
            # 내 순위와 경쟁사 집계는 같은 한 바퀴에서 같은 규칙으로 갈린다.
            position = url = None
            top_rows = []
            seen = set()        # 한 검색결과에 두 번 선 도메인도 그 검색어 하나로 센다
            for t in res["top"]:
                d = t.get("domain") or ""
                # 상위 몇 줄은 그대로 남긴다. 예전엔 이 응답에서 내 순위만 빼고
                # 나머지를 버렸고, 그래서 요청문이 "상위 페이지 제목을 붙여 넣으세요"
                # 라고 사람에게 시켰다 — 방금 받아 온 것을 버린 채로.
                top_rows.append({"position": t.get("pos"), "url": t.get("url"),
                                 "title": t.get("title"), "domain": d or None,
                                 "is_own": bool(d and scoring.owns(d, own))})
                if not d:
                    continue
                if scoring.owns(d, own):
                    if position is None:
                        position, url = t.get("pos"), t.get("url")
                elif d not in seen:
                    seen.add(d)
                    domain_hits[d] += 1
            aio_cited = (int(any(scoring.owns(d, own) for d in res["aio_domains"]))
                         if res["aio_present"] else None)
            # 인용 도메인은 0/1 로 접고 끝내지 않는다 — "누가 대신 인용됐나"가 AI 요약
            # 요청문의 근거다. 요약이 없었거나(0) 안 쟀으면(None, serper) 목록도 None 이다.
            db.write_rank_snapshot(
                conn, row["id"], position, url,
                res["serp_features"], res["aio_present"], aio_cited, checked_at=at,
                aio_domains=res["aio_domains"] if res["aio_present"] == 1 else None)
            db.write_serp_results(conn, row["id"], top_rows, checked_at=at)
            # 상위 글 몇 개의 주소만 모아 둔다 — 여는 것은 런 끝에 한 번, 주소 단위로.
            outline_urls.extend(
                t["url"] for t in top_rows[:OUTLINE_PER_KEYWORD]
                if t.get("url") and not t.get("is_own"))
            # 함께 묻는 질문·연관 검색어를 **어느 검색어에서 나왔는지와 함께** 남긴다 —
            # 아래 키워드 후보 적재와 별개다(그쪽은 --no-harvest 로 끌 수 있는 부산물이고,
            # 이쪽은 이 검색어의 요청문이 쓰는 사실이다).
            db.write_serp_questions(
                conn, row["id"],
                [("paa", q) for q in res.get("paa") or []]
                + [("related", q) for q in res.get("related") or []], checked_at=at)
            total_cost += res["cost"]
            if not no_harvest:
                # 후보는 조회에 쓴 로케일을 물려받는다. 안 그러면 한국어 SERP에서
                # 캔 후보가 locale NULL로 들어가 다음 런에서 프로젝트 로케일로
                # 조회되고, 방금 고친 버그가 후보 전체에 그대로 재현된다.
                for kw in (res["related"] + res["paa"]):
                    harvested_kw.add((kw.strip(), kw_locale))
            pos = position if position is not None else "-"
            aio = " AIO" + ("✓" if aio_cited else "") if res["aio_present"] else ""
            print(f"  {pos!s:>3}  {row['keyword']}{aio}")

        def one(row) -> None:
            """한 건씩 재는 제공자 — 가져와서 쓴다. 실패는 러너가 세고 다음으로 넘어간다."""
            kw_locale = row["locale"] or default_locale
            write(row, serp_adapter.fetch(provider, row["keyword"], kw_locale, depth, device=device))

        timed_out = None
        with st.record("rank") as r:
            if queued:
                q = _queue(st, kws, reuse, default_locale=default_locale,
                           depth=depth, device=device)
                total_cost += q["cost"]
                if q["left"]:
                    # 다 안 왔다 — 온 것도 안 쓴다. id 는 serp_tasks 에 남아 있다.
                    timed_out = (f"순위 대기열이 {WAIT_LIMIT_S // 60}분 안에 다 안 왔습니다 "
                                 f"({q['left']}/{q['asked']}개 남음) — 이번 결과는 적재하지 않았고 "
                                 f"이전 측정이 그대로입니다. 맡긴 조회는 다음 런이 새로 맡기지 "
                                 f"않고 받아 옵니다")
                    done = 0
                else:
                    # 쓰기만 남았다 — 네트워크가 없으니 항목 사이에 쉴 까닭이 없다(throttle 은
                    # 한 건씩 재는 제공자의 것이다. 상위 글 열기는 지역변수 throttle 을 쓴다).
                    st.throttle = 0.0
                    done = st.each(q["results"], lambda pair: write(*pair),
                                   label=lambda pair: pair[0]["keyword"])
                    db.drop_serp_tasks(conn, q["task_ids"])
                calls = q["calls"]
            else:
                done = st.each(kws, one, label=lambda row: row["keyword"])
                calls = done

            if not timed_out:
                if not no_harvest:
                    new_kw = db.add_keyword_candidates(
                        conn, p["id"], [(kw, loc, "serp") for kw, loc in harvested_kw])
                    new_comp = db.add_competitors(
                        conn, p["id"], scoring.serp_rivals(domain_hits, done, own), "auto_rank")

                # 상위 글의 제목·H2 — 요청문이 "빠진 구간"을 짐작이 아니라 비교로 찾는 재료다.
                # 여태 이걸 안 모아서 요청문이 사람에게 붙여 넣으라고 시켰다(제목은 이미 있었다).
                out_ok, out_bad = _collect_outlines(conn, outline_urls, throttle=throttle)
            else:
                out_ok = out_bad = 0
            r.api_calls, r.cost = calls, total_cost
            r.notes = (f"provider={provider} device={device} {st.err_note} skipped={skipped} "
                       + (f"queue_reused={len(reuse)} " if queued else "")
                       + ("timeout=1 " if timed_out else "")
                       + f"harvest_kw={new_kw} harvest_comp={new_comp} "
                       f"outlines={out_ok} outline_errors={out_bad}"
                       # 실패 표식은 판정 규칙 한 벌("중단:" · errors=N — dashboard._run_ok,
                       # 셸 runVerdict)이 읽는 꼴로 남긴다. timeout=1 만 두면 그 규칙이 이
                       # 런을 성공으로 읽어 "순위 오늘 잼"이라 말하고 다시 재기도 안 권한다.
                       + (f" | 중단: 순위 대기열 {WAIT_LIMIT_S // 60}분 상한" if timed_out else ""))

        if timed_out:
            print(f"\n[실패] {timed_out}\nrun_id={r.id}", file=sys.stderr)
            return collector.failed(timed_out, errors=tuple(st.failures), cost=total_cost)
        print(f"\nsaved {done} snapshots{st.skip_note(skipped)} (errors={st.errors}) "
              f"actual_cost=${total_cost:.3f} | 부산물: 키워드 후보 +{new_kw}, "
              f"경쟁사 +{new_comp}\nrun_id={r.id}")
        return st.verdict(done, rows=done, cost=total_cost)


def _job_key(keyword: str, locale: str, depth: int, device: str) -> tuple:
    """맡긴 과제와 이번 조회가 같은 것인가 — 글자·로케일·깊이·기기가 다 같아야 같다."""
    return (keyword, locale, int(depth), device)


def _reusable(conn, project: str, kws, default_locale: str, depth: int, device: str) -> dict:
    """{keyword id: 과제 id} — 앞 런이 맡겨 두고 적재 못 한 것 중 이번 조회와 같은 것.

    보관 기간(serp_adapter.QUEUE_KEEP_DAYS)을 넘긴 id 는 db.serp_tasks_pending 이 지운다.
    한 검색어에 여럿이면 가장 새것을 쓴다(pending 이 새것부터 온다).
    """
    pend: dict = {}
    for t in db.serp_tasks_pending(conn, project, serp_adapter.QUEUE_KEEP_DAYS):
        pend.setdefault(_job_key(t["keyword"], t["locale"], t["depth"], t["device"]), t["task_id"])
    out = {}
    for k in kws:
        tid = pend.get(_job_key(k["keyword"], k["locale"] or default_locale, depth, device))
        if tid:
            out[k["id"]] = tid
    return out


def _queue(st, kws, reuse: dict, *, default_locale: str, depth: int, device: str) -> dict:
    """대기열로 잰다 — 앞 런 것을 먼저 받고, 나머지를 맡기고, **전부** 올 때까지 기다린다.

    반환 {results: [(row, res)], left, asked, cost, calls, task_ids}.
    left>0 이면 상한에 걸린 것이다 — 부르는 쪽이 아무것도 안 쓴다.
    쓰지 않는다(DB 에 쓰는 것은 맡긴 id 뿐이다): 적재는 다 온 뒤 한 번에 한다.
    """
    by_id = {k["id"]: k for k in kws}
    name = st.project["name"]
    task_of: dict[str, int] = {tid: kid for kid, tid in reuse.items()}
    calls = 0

    # ① 앞 런이 맡긴 것부터 곧장 받아 본다(받기는 무료). 못 받는 것(만료·없음)은 새로 맡긴다.
    got, bad, _ = serp_adapter.wait_serp_tasks(
        list(task_of), depth=depth, timeout_s=0, probe=list(task_of),
        clock=_clock, sleep=_sleep)
    gone = [tid for tid, (state, _) in bad.items() if state == "gone"]
    db.drop_serp_tasks(conn=st.conn, task_ids=gone)
    fresh = [k for k in kws if k["id"] not in reuse
             or reuse[k["id"]] in gone]
    for tid in gone:
        task_of.pop(tid, None)

    # ② 나머지를 맡기고 id 를 **묶음마다 바로** 남긴다 — 기다리다 죽거나 다음 묶음이
    #    402 로 멈춰도 돈 낸 과제는 남는다.
    cost = 0.0
    jobs = [{"tag": k["id"], "keyword": k["keyword"],
             "locale": k["locale"] or default_locale, "depth": depth, "device": device}
            for k in fresh]
    for i in range(0, len(jobs), serp_adapter.QUEUE_MAX_POST):
        chunk = jobs[i:i + serp_adapter.QUEUE_MAX_POST]
        posted, c = serp_adapter.post_serp_tasks(chunk)
        cost += c
        calls += 1
        keep = []
        for j, x in zip(chunk, posted):
            if x["id"]:
                task_of[x["id"]] = int(x["tag"])
                keep.append((j["keyword"], j["locale"], device, depth, x["id"]))
            else:
                st.fail(x["error"], item=j["keyword"], kind="task_post")
        db.add_serp_tasks(st.conn, name, keep)

    # ③ 전부 올 때까지 — 상한은 WAIT_LIMIT_S.
    wait = [tid for tid in task_of if tid not in got]
    more, bad2, left = serp_adapter.wait_serp_tasks(
        wait, depth=depth, timeout_s=WAIT_LIMIT_S, clock=_clock, sleep=_sleep)
    got.update(more)
    bad.update(bad2)
    # 과제는 끝났는데 실패한 것 — 항목 오류다. 다시 받아도 같은 답이라 id 를 버린다.
    # 앞 ①의 gone 은 새로 맡겼으니 오류가 아니다.
    dead = []
    for tid, (state, why) in bad.items():
        if tid in gone:
            continue
        dead.append(tid)
        st.fail(f"{why}", item=by_id[task_of[tid]]["keyword"] if tid in task_of else tid,
                kind=f"task_{state}")
    db.drop_serp_tasks(st.conn, dead)
    order = {k["id"]: n for n, k in enumerate(kws)}
    results = sorted(((by_id[task_of[tid]], res) for tid, res in got.items() if tid in task_of),
                     key=lambda pair: order[pair[0]["id"]])
    # 호출 수 = 맡기기 묶음 + 받은 과제(받기 하나가 요청 하나다). 아직 안 된 과제를 다시
    # 두드린 것(task_get 40602)은 무료라 안 센다.
    return {"results": results, "left": len(left), "asked": len(task_of),
            "cost": cost, "calls": calls + len(got) + len(bad), "task_ids": list(task_of)}


# 상위 글 몇 개까지 열어 H2 를 볼 것인가 — 요청문이 "상위 2~3개와 비교하라"고 시키므로
# 그 수다. 한 런에서 여는 주소 전체 상한도 둔다: 검색어 200개면 남의 서버를 수백 번
# 두드리게 되고, 그건 수집이 아니라 민폐다.
OUTLINE_PER_KEYWORD = 3
OUTLINE_MAX_PER_RUN = 40


def _collect_outlines(conn, urls: list[str], *, throttle: float | None = None) -> tuple[int, int]:
    """상위 글의 제목·H2 를 가져와 남긴다 (가져온 수, 실패 수).

    검색어가 아니라 **주소** 단위다 — 한 경쟁 페이지가 여러 검색어에서 상위에 서므로
    검색어마다 가져오면 같은 글을 그 수만큼 다시 연다. 실패도 남긴다: 안 남기면 다음
    런이 같은 주소를 또 두드린다.
    """
    import time

    import collect_page
    todo = db.serp_outlines_stale(conn, urls)[:OUTLINE_MAX_PER_RUN]
    ok = bad = 0
    for i, u in enumerate(todo):
        a = collect_page.fetch(u)
        if a.get("error"):
            db.write_serp_outline(conn, u, status=a.get("status"), title=None, h2=[], words=None)
            bad += 1
        else:
            try:
                h2 = json.loads(a.get("h2_json") or "[]")
            except (TypeError, ValueError):
                h2 = []
            db.write_serp_outline(conn, u, status=a.get("status"), title=a.get("title"),
                                  h2=h2, words=a.get("words"), tables=a.get("tables"),
                                  lists=a.get("lists"), images=a.get("images"),
                                  videos=a.get("videos"))
            ok += 1
        if throttle and i + 1 < len(todo):
            time.sleep(throttle)
    return ok, bad


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser()
    collector.add_common(ap)
    ap.add_argument("--provider", choices=list(serp_adapter.PROVIDERS))
    collector.add_setting(ap, "--max-keywords", key="limits.max_keywords",
                          fallback=db.RANK_KEYWORDS_CAP, type=int)
    collector.add_setting(ap, "--depth", key="serp_depth", fallback=10, type=int)
    collector.add_setting(ap, "--throttle", key="throttle", fallback=0.5, type=float,
                          help="요청 간격(초). 기본은 skill_config defaults.throttle")
    collector.add_setting(ap, "--device", key="serp_device", fallback="desktop", type=str,
                          help="측정 디바이스 (desktop|mobile). 기본은 desktop")
    ap.add_argument("--no-harvest", action="store_true")
    ap.add_argument("--ids", help="쉼표로 구분한 keyword id — 지정하면 is_active를 무시하고 이것만 조회")
    ap.add_argument("--force", action="store_true", help="오늘 이미 확인한 키워드도 건너뛰지 않고 재확인")
    return ap


def main() -> None:
    collector.cli("rank")


if __name__ == "__main__":
    main()
