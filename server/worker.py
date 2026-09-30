"""수집 워커 — 유저별 env 를 갈아끼우고 기존 수집 체인(run_chain) 을 호출만 한다.

수집 엔진(skills/capture/scripts/*) 은 한 줄도 손대지 않는다. 격리는 env
(CAPTURE_HOME, GSC_TOKEN_FILE, 유료 API 키) 를 호출 시마다 다시 읽기 때문에
process-global os.environ 을 잠깐 갈아끼우는 것으로 끝난다.

CLI:
  python server/worker.py                              # demo — API 호출 없음
  python server/worker.py --all [--idle-days 30] ...   # 스케줄 대상 직렬 실행
  python server/worker.py --user <id> --project <p> .. # 단일 사이트 (--groups / --only / --queue)
"""
from __future__ import annotations

import argparse
import contextlib
import io
import os
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(ROOT / "skills" / "capture" / "scripts"))

import collector                              # noqa: E402
import db                                     # noqa: E402
import exports                                # noqa: E402
import mailer                                 # noqa: E402
import run_all                                # noqa: E402
import scheduler                              # noqa: E402
import settings                                # noqa: E402
import store                                  # noqa: E402

# 유료 키 주입은 settings.paid_keys() 가 한다 — 명명 규칙(PAID_KEYS/SERVER_PREFIX)의
# 주인이 거기고, /api/doctor 도 같은 컨텍스트를 둘러야 판정이 런과 같아진다.

# 진단 로그. 사용자 내레이션(print)은 _Tee 가 sites.run_log 로 가져가는 **제품**이라
# 그대로 두고, traceback 처럼 개발자용인 것만 이 통로로 보낸다.
LOG = collector.LOG.getChild("worker")


# 서치콘솔 없이 시작한 사이트가 자동으로 켜는 추적 키워드 수(씨앗 포함). 순위는 키워드당
# 과금이라 작게 둔다 — 더 재고 싶으면 사람이 [키워드]에서 켠다.
NO_GSC_KEYWORDS = 20


def activate_by_volume(project: str, limit: int | None = None) -> int:
    """서치콘솔 없는 사이트 — 후보 키워드를 **검색량 순으로** 켠다. 반환: 새로 켠 개수.

    activate_from_gsc 의 짝이다. 그쪽은 "구글이 이미 내 페이지를 이 검색어에 노출했다"는
    실측으로 고르는데, 서치콘솔이 없으면 그 재료가 없어서 씨앗 몇 개만 순위를 쟀다
    (gucci: 자동완성 후보 66개가 전부 꺼진 채 순위는 '구찌' 1개). 여기서는 검색량이
    확인된(volume>0) 후보를 큰 순으로 고른다.

    **순위(rank) 직전에** 부른다(run_all.run_chain 의 before 훅) — 같은 런에서 검색량이
    나온 뒤라 첫 런부터 순위가 잡힌다. 사이트당 **한 번만** 채운다(auto_keywords 표식):
    그 뒤 사람이 끈 키워드를 런마다 되켜면 끄는 손잡이가 무의미해진다.
    """
    limit = min(limit or NO_GSC_KEYWORDS, settings.count("SEOMINER_MAX_KEYWORDS"))
    conn = db.connect()
    try:
        p = db.get_project(conn, project)
        if (p["gsc_property"] or "") or db.project_settings(conn, p["id"]).get("auto_keywords"):
            return 0
        room = max(0, limit - db.count_active_keywords(conn, p["id"]))
        ids = [r[0] for r in conn.execute(
            "SELECT id FROM keywords WHERE project_id=? AND is_active=0"
            " AND COALESCE(verdict_off,0)=0 AND volume>0"
            " ORDER BY volume DESC, id LIMIT ?", (p["id"], room)).fetchall()]
        if not ids:
            return 0        # 검색량이 아직 없다 — 표식을 안 찍고 다음 런에 다시 본다
        n = db.set_keywords_active(conn, p["id"], ids, True)
        db.set_project_settings(conn, p["id"], {"auto_keywords": time.strftime("%Y-%m-%d")})
        print(f"[rank] 서치콘솔 없는 사이트 — 검색량 상위 키워드 {n}개를 켜서 함께 잽니다")
        return n
    finally:
        conn.close()


# ── 서치콘솔 없는 사이트의 추적 키워드 — 우리 순위 검색어(DataForSEO)가 주 출처 ──────────
# 자동완성 검색량 순(activate_by_volume)으로 고르면 씨앗 하나('구찌')에서 나온 "구찌 ○○"만
# 켜졌다 — 20개 중 12개가 이미 1위라 할 일이 안 나왔고, 정작 이미 4~30위에 오른 비브랜드
# 카테고리 검색어 304개(가방 8위·토트백 6위·지갑 10위 …)는 안 봤다(2026-09-29 gucci).
LABS_EVERY_DAYS = 7          # 우리 순위 검색어를 다시 사는 주기(일) — 한 번에 ~$0.06
LABS_LIMIT = 500             # 검색량 큰 순으로 이만큼
AUTO_VERSION = "labs2"       # auto_keywords 표식의 판 — 판이 다르면 한 번 다시 고른다
                             # labs2: labs1 은 AI 판정이 잘려 빈 채로 골랐다(무관 0·매장 위치가
                             #   추적에 들었다) — 판정이 제대로 도는 판으로 한 번 다시 고른다
RISE_SHARE, RECLAIM_SHARE = 0.6, 0.25     # 나머지는 방어(1~3위)
JUDGE_POOL = 150             # AI 관련성 판정에 보여 주는 검색어 수(검색량 큰 순)


def _refresh_labs(conn, p, post) -> bool:
    """우리 순위 검색어를 LABS_EVERY_DAYS 마다 받아 labs_ranked·후보 키워드로 올린다."""
    import datetime
    import collect_gap
    d = conn.execute("SELECT MAX(checked_date) FROM labs_ranked WHERE project_id=?",
                     (p["id"],)).fetchone()[0]
    today = datetime.date.today()
    if d and (today - datetime.date.fromisoformat(d)).days < LABS_EVERY_DAYS:
        return False
    locale = db.project_locale(p)
    rows, cost = collect_gap.fetch_own_ranked(post, p["domain"], locale, LABS_LIMIT)
    db.write_labs_ranked(conn, p["id"], today.isoformat(), rows)
    db.add_keyword_candidates(conn, p["id"], [(r["keyword"], locale, "labs_ranked") for r in rows])
    collect_gap._backfill_volumes(conn, p["id"], [(r["keyword"], r.get("volume")) for r in rows])
    # 검색량을 이미 받았다 — 지표 단계(collect_metrics)가 metrics_at 빈 키워드를 전부 다시 사서
    # 이 500개에 매 런 ~$0.26 을 또 냈을 것이다. 받은 날을 찍어 둔다(난이도·CPC 는 그대로 빈다).
    conn.executemany(
        "UPDATE keywords SET metrics_at=? WHERE project_id=? AND keyword=?"
        " AND metrics_at IS NULL AND volume IS NOT NULL",
        [(db.now(), p["id"], r["keyword"]) for r in rows if r.get("volume") is not None])
    conn.commit()
    print(f"[rank] 우리 순위 검색어 {len(rows)}개를 받았습니다 (DataForSEO 추정 · ${cost:.3f})")
    return True


def _judge_keywords(ask, p, pool: list, aliases) -> tuple[set, set]:
    """(우리 브랜드 표기, 버릴 검색어) — AI 한 번. 목록 밖의 말은 버린다."""
    import scoring
    shown = [(k, pos, (url or "").split("/", 3)[-1][:60]) for k, pos, url in pool]
    got = ask(
        f"우리 사이트는 {p['domain']} 이다. 아래는 이 사이트가 검색에서 순위를 가진·후보인 검색어다 "
        "(순위, 걸린 페이지 경로).\n"
        f"1) brand_terms: 도메인 {p['domain']} 가 가리키는 **우리 이름 자체**의 표기만(번역·음역 포함). "
        "우리가 다루는 남의 제품·브랜드 이름은 빼라.\n"
        "2) drop: 추적할 가치가 **없는** 검색어만 — 우리 사업·상품과 무관한 것, 매장 위치·지점·"
        "주소·백화점 찾기, 오타·깨진 말, 다른 사이트·서비스 이름, 우리와 무관한 인물·사건. "
        "상품·카테고리를 가리키는 일반 검색어(영어 포함, '○○ 브랜드'·'○○ 추천' 포함)는 **절대 "
        "drop 하지 마라** — 그게 검색에서 경쟁하는 자리다. 애매하면 남겨라.\n"
        '둘 다 목록에 있는 표기 그대로. 답은 JSON 하나: {"brand_terms": [...], "drop": [...]}\n\n'
        + "\n".join(f"- {k} ({pos or '-'}위, /{u})" for k, pos, u in shown))
    known = {k for k, _, _ in pool}
    terms = {scoring.norm(a) for a in (aliases or []) if scoring.norm(a)}
    stem = scoring.norm(p["domain"].split(".")[0])
    if stem:
        terms.add(stem)
    # AI 가 준 표기를 데이터로 검증한다 — 우리 이름이면 그 말 자체로 상위(1~3위)에 서거나 여러
    # 검색어에 걸쳐 나온다. 귀걸이 은어(귀찌·금찌·방찌)를 브랜드로 뽑아 "되찾기" 칸에 들인 적이 있다.
    pos_of = {scoring.norm(k): pos for k, pos, _ in pool if pos}
    norms = [scoring.norm(k) for k in known]
    for t in got.get("brand_terms") or []:
        n = scoring.norm(str(t))
        if n and ((pos_of.get(n) or 99) <= 3 or sum(1 for k in norms if n in k) >= 3):
            terms.add(n)
    drop = {str(k) for k in (got.get("drop") or []) if str(k) in known}
    return terms, drop


def _select_keywords(conn, pid: int, cap: int, terms: set) -> list[int]:
    """추적할 키워드 id — 씨앗 + 올릴 기회(4~30위 비브랜드) + 되찾기(우리 상품인데 밖·10위 밖)
    + 방어(1~3위). 칸마다 검색량 큰 순, 모자란 칸은 다른 칸이 메운다."""
    import scoring
    brand = lambda k: any(t and t in scoring.norm(k) for t in terms)
    d = conn.execute("SELECT MAX(checked_date) FROM labs_ranked WHERE project_id=?", (pid,)).fetchone()[0]
    # 순위는 띄어쓰기를 무시하고 찾는다 — '구찌 가방' 1위인데 '구찌가방'을 "순위권 밖"이라며
    # 되찾기 칸에 넣었다(같은 검색어다). 변형이 여럿이면 가장 높은 자리.
    pos: dict = {}
    for kw, p_ in conn.execute(
            "SELECT keyword, position FROM labs_ranked WHERE project_id=? AND checked_date=?", (pid, d)):
        n = scoring.norm(kw)
        if p_ and (n not in pos or p_ < pos[n]):
            pos[n] = p_
    kws = [dict(r) for r in conn.execute(
        "SELECT id, keyword, volume, source FROM keywords WHERE project_id=?"
        " AND COALESCE(verdict_off,0)=0 AND COALESCE(volume,0)>0 ORDER BY volume DESC, id", (pid,))]
    seeds = [r[0] for r in conn.execute(
        "SELECT id FROM keywords WHERE project_id=? AND source='seed'", (pid,))]
    at = lambda k: pos.get(scoring.norm(k))
    rise = [k["id"] for k in kws if not brand(k["keyword"]) and 4 <= (at(k["keyword"]) or 0) <= 30]
    reclaim = [k["id"] for k in kws if brand(k["keyword"]) and (at(k["keyword"]) or 999) > 10]
    defend = [k["id"] for k in kws if 1 <= (at(k["keyword"]) or 0) <= 3]
    room = max(0, cap - len(seeds))
    want = [round(room * RISE_SHARE), round(room * RECLAIM_SHARE)]
    want.append(room - sum(want))
    # 띄어쓰기·대소문자 변형은 한 말이다('카드 지갑'·'카드지갑') — 검색량 큰 쪽 하나만.
    norm_of = {k["id"]: scoring.norm(k["keyword"]) for k in kws}
    seen = {scoring.norm(r[0]) for r in conn.execute(
        "SELECT keyword FROM keywords WHERE project_id=? AND source='seed'", (pid,))}
    picked: list[int] = list(seeds)

    def take(bucket, n):
        got = 0
        for i in bucket:
            if got >= n or len(picked) >= max(cap, len(seeds)):
                break
            if i in picked or norm_of.get(i) in seen:
                continue
            picked.append(i)
            seen.add(norm_of.get(i))
            got += 1

    buckets = [rise, reclaim, defend]
    for bucket, n in zip(buckets, want):
        take(bucket, n)
    for bucket in buckets:                      # 모자란 칸은 순서대로 메운다
        take(bucket, cap)
    return picked


def activate_keywords(project: str, limit: int | None = None, *, post=None, ask=None) -> int:
    """서치콘솔 없는 사이트의 추적 키워드를 고른다 — 순위(rank) 직전(run_all before 훅).

    주 출처는 우리 순위 검색어(DataForSEO ranked_keywords → labs_ranked). 판(AUTO_VERSION)이
    바뀐 첫 번에는 **다시 고른다**(예전 판이 켠 것을 이 선택으로 바꾼다 — 씨앗은 그대로).
    그 뒤로는 빈자리만 채운다 — 사람이 끈 키워드를 되켜지 않는다. AI 나 DataForSEO 키가
    없으면 예전 경로(activate_by_volume)로 물러선다. 반환: 새로 켠 개수.
    """
    import os
    import time
    import collect_gap
    import serp_adapter
    if ask is None and os.environ.get("OPENROUTER_API_KEY"):
        ask = collect_gap.openrouter_json
    if post is None and serp_adapter.has_dataforseo():
        post = serp_adapter.post_dataforseo
    cap = min(limit or NO_GSC_KEYWORDS, settings.count("SEOMINER_MAX_KEYWORDS"))
    conn = db.connect()
    try:
        p = db.get_project(conn, project)
        if (p["gsc_property"] or ""):
            return 0
        if ask is None or post is None or not p["domain"]:
            conn.close()
            conn = None
            return activate_by_volume(project, limit)
        cfg = db.project_cfg(conn, p)
        flag = str(cfg.get("auto_keywords") or "")
        fresh = not flag.startswith(AUTO_VERSION)
        refreshed = _refresh_labs(conn, p, post)
        active = db.count_active_keywords(conn, p["id"])
        if not fresh and not refreshed and active >= cap:
            return 0
        d = conn.execute("SELECT MAX(checked_date) FROM labs_ranked WHERE project_id=?",
                         (p["id"],)).fetchone()[0]
        pool = [(r[0], r[1], r[2]) for r in conn.execute(
            "SELECT keyword, position, url FROM labs_ranked WHERE project_id=? AND checked_date=?"
            " ORDER BY volume DESC LIMIT ?", (p["id"], d, JUDGE_POOL))]
        pool += [(r[0], None, None) for r in conn.execute(      # 자동완성 후보(우리 상품 검색 등)
            "SELECT keyword FROM keywords WHERE project_id=? AND source<>'labs_ranked'"
            " AND is_active=0 ORDER BY volume DESC LIMIT 40", (p["id"],))]
        if not pool:
            return 0
        terms, drop = _judge_keywords(ask, p, pool, cfg.get("brand_aliases"))
        for k in drop:        # 무관 — 꺼진 후보에만 표시한다(켜 둔 것은 사람이 판단한다)
            conn.execute("UPDATE keywords SET verdict_off=1 WHERE project_id=? AND keyword=?"
                         " AND is_active=0", (p["id"], k))
        conn.commit()
        want = _select_keywords(conn, p["id"], cap, terms)
        cur = {r[0] for r in conn.execute(
            "SELECT id FROM keywords WHERE project_id=? AND is_active=1", (p["id"],))}
        if fresh:
            seeds = {r[0] for r in conn.execute(
                "SELECT id FROM keywords WHERE project_id=? AND source='seed'", (p["id"],))}
            off = [i for i in cur if i not in want and i not in seeds]
            db.set_keywords_active(conn, p["id"], off, False)
            new = [i for i in want if i not in cur]
        else:
            new = [i for i in want if i not in cur][:max(0, cap - len(cur))]
        n = db.set_keywords_active(conn, p["id"], new, True) if new else 0
        db.set_project_settings(conn, p["id"], {"auto_keywords": f"{AUTO_VERSION}:{time.strftime('%Y-%m-%d')}"})
        print(f"[rank] 추적 키워드를 골랐습니다 — 새로 {n}개"
              + (f" · 뺀 것 {len(off)}개" if fresh else "")
              + f" · 무관 {len(drop)}개 · 우리 브랜드 표기 {sorted(terms)}")
        return n
    finally:
        if conn is not None:
            conn.close()


def ensure_ai_prompts(project: str, n: int = 20) -> int:
    """물어볼 질문이 하나도 없으면 만들어 둔다. 반환: 새로 넣은 개수.

    웹에는 `/capture add` 를 칠 채팅이 없다 — 온보딩으로 만든 사이트는 질문이 빈 채로
    첫 런을 돌아 AI 인용 단계가 통째로 건너뛰었다(gucci). [AI 인용] 화면의 [질문 만들기]
    와 같은 한 벌(gen_prompts.suggest/save)을 부른다. 이미 질문이 있으면(사람이 만들었거나
    지웠거나) 손대지 않는다 — 0개일 때만.
    """
    import gen_prompts
    conn = db.connect()
    try:
        pid = db.get_project(conn, project)["id"]
        if conn.execute("SELECT 1 FROM ai_prompts WHERE project_id=? LIMIT 1", (pid,)).fetchone():
            return 0
        rows = gen_prompts.suggest(project, n=n, conn=conn)
        added = gen_prompts.save(conn, project, rows) if rows else 0
        if added:
            print(f"[{project}] AI 에 물어볼 질문 {added}개를 만들었습니다 — 이번 런부터 인용을 확인합니다")
        return added
    finally:
        conn.close()


def activate_from_gsc(project: str, limit: int | None = None) -> int:
    """서치콘솔에 노출된 키워드를 노출 순으로 활성화한다. 반환: 새로 켠 개수.

    로컬에서는 Claude 가 관련성을 보고 골라 준다(capture SKILL.md 의 큐레이션 단계).
    웹에는 그 자리에 사람이 없으므로 실측만 믿는다 — '구글이 이미 내 페이지를 이
    검색어에 노출시켰다'는 사실. 자동완성 후보(노출 0)는 관련성이 확인되지 않아
    그대로 후보로 둔다. 활성 키워드가 0 이면 rank 단계가 잴 대상 없이 돈다.
    """
    limit = limit or settings.count("SEOMINER_MAX_KEYWORDS")
    conn = db.connect()
    try:
        pid = db.get_project(conn, project)["id"]
        # 이미 켜 둔 것까지 세어 남은 자리만 채운다. 매번 limit 개씩 더 켜면
        # 추적 세트가 런마다 불어나고, SERP 는 키워드당 과금이라 비용이 샌다.
        active = db.count_active_keywords(conn, pid)
        room = max(0, limit - active)
        if room == 0:
            return 0
        # "노출 순으로 골라 room 개" 는 db.py 에 없다 (GSC 조인이 필요한 선택 로직이라
        # db.count_active_keywords/set_keywords_active 와는 층이 다르다) — 여기 남긴다.
        # TODO(db.py): 이 선택 로직이 다른 곳에서도 필요해지면 db.py 로 옮긴다.
        ids = [r[0] for r in conn.execute(
            "SELECT k.id FROM keywords k"
            "  JOIN gsc_snapshots g ON g.project_id=k.project_id AND g.query=k.keyword"
            " WHERE k.project_id=? AND k.is_active=0"
            " GROUP BY k.id ORDER BY SUM(g.impressions) DESC LIMIT ?",
            (pid, room)).fetchall()]
        return db.set_keywords_active(conn, pid, ids, True)
    finally:
        conn.close()


def _mail_stats(project: str) -> dict:
    """알림에 넣을 숫자. 없는 값은 넣지 않는다 — 0 으로 지어내면 거짓말이 된다.

    집계는 exports.summary() 하나로 — 최신 스냅샷 클릭/노출, 신규 기회 수·최상위
    기회, 활성 키워드 수는 화면 요약과 같은 쿼리여야 값이 어긋나지 않는다.
    """
    out: dict = {}
    try:
        s = exports.summary(project)
        if s["impressions"]:
            out["clicks"], out["impressions"] = s["clicks"] or 0, s["impressions"]
        if s["opportunities"]:
            out["opportunities"] = s["opportunities"]
            if s["top_opportunity"]:
                out["top"] = s["top_opportunity"]
        if s["keywords_active"]:
            out["keywords"] = s["keywords_active"]
    except Exception:
        pass                      # 통계를 못 모아도 메일은 보낸다
    return out


def _email_of(conn, site) -> str | None:
    """due_sites() 는 email 을 JOIN 해 주지만 sites() 는 아니다 — 웹에서 실행한
    단일 사이트 경로(--user --project)가 여기서 터졌다."""
    try:
        return site["email"]
    except (IndexError, KeyError):
        r = conn.execute("SELECT email FROM users WHERE id=?",
                         (site["user_id"],)).fetchone()
        return r["email"] if r else None


def _latest_report(project: str) -> bytes | None:
    """report 단계가 방금 만든 보고서. tenant 안에서만 부른다."""
    try:
        files = sorted((db.CAPTURE_HOME / "reports" / project).glob("*.html"))
        return files[-1].read_bytes() if files else None
    except Exception:
        return None


def backlinks_plan(skip: str | None, every_days: float) -> tuple[str | None, dict]:
    """백링크 주기를 체인이 알아듣는 모양으로 — (skip, opts).

    여태 이 판정이 체인 **밖**에 따로 있었다(_backlinks_due). 백링크가 수집 단계가
    되면서 신선도 판정이 두 벌이 됐고, 그건 한 런에 두 번 사는 길이다. 유료 호출이라
    그게 곧 돈이다. 0 은 신선도로 표현할 수 없으므로(0일보다 오래됐나 = 늘 참) 단계를
    통째로 건너뛴다.
    """
    if every_days <= 0:
        return ",".join(x for x in [skip, "backlinks"] if x), {}
    return skip, {"backlinks": {"max_age": every_days}}


# 런 로그를 DB 에 흘려 보내는 간격(초). 단계 하나가 14분이다(rank 100개) — 끝나고만
# 저장하면 그동안 화면이 안 움직인다. 줄마다 UPDATE 하지 않는 이유는 반대쪽이다:
# 한 런의 내레이션은 수천 줄이라 줄마다 쓰면 그게 곧 수천 번의 트랜잭션이다.
SAVE_EVERY_SECONDS = 4.0


class _Tee(io.StringIO):
    """잡으면서 흘린다 — 런 내레이션을 DB 로 가져간다고 운영자가 보던 콘솔 로그
    (Railway) 를 뺏으면 안 된다. redirect_stdout / redirect_stderr 가 이걸 받는다.

    into 를 주면 글은 그쪽 버퍼에 쌓인다. stdout 과 stderr 를 각각 감싸되 **버퍼는
    한 벌**로 두려고 있는 인자다 — 수집기의 오류 문장은 전부 stderr 인데
    (collector.Stage.each, run_all 의 `[오류]` 줄) 여태 stdout 만 잡아서 화면 로그에
    한 줄도 안 실렸다. 402 를 100번 맞은 런이 사용자에게는 조용한 성공으로 보였다.
    따로 모으면 순서가 사라지므로 버퍼를 나누지 않는다. 콘솔로 흘릴 때만 각자의
    원래 스트림으로 간다.
    """

    def __init__(self, out, into=None, save=None, every=SAVE_EVERY_SECONDS):
        super().__init__()
        self._out = out
        self._into = into
        self._save, self._every, self._last = save, every, 0.0

    def write(self, s):
        try:
            self._out.write(s)
            self._out.flush()
        except Exception:
            pass                      # 콘솔이 죽었다고 수집을 죽이지 않는다
        return (self._into or self)._append(s)

    def _append(self, s):
        n = io.StringIO.write(self, s)
        if self._save and time.monotonic() - self._last >= self._every:
            self._last = time.monotonic()
            try:
                self._save(self.getvalue())
            except Exception:
                pass                  # 로그를 못 저장했다고 수집을 죽이지 않는다
        return n


# 눌린 뒤 대기열을 가져가기까지 기다리는 시간(초). 이 안에 연달아 누른 묶음은 한 런으로
# 합쳐진다 — [검색 성과] 누르고 곧바로 [AI 노출] 을 누른 사람에게 런 두 벌(= rank 두 번)을
# 사게 하지 않는다. 길면 첫 버튼의 반응이 굼뜨다.
MERGE_SECONDS = 5.0


def _hand_off(conn, site) -> bool:
    """런을 끝낸 **뒤** 대기열에 남은 묶음을 새 워커(--queue)에 넘긴다 — 반환: 넘겼나.

    마지막 claim_queue 와 mark_done 사이에 누른 묶음은 대기열에 남는다(화면은 '대기 중').
    다음 스케줄러 틱이 잡을 거라 믿으면 안 된다: scheduler.loop 는 스윕(최대 3시간)을
    기다리는 동안 틱을 안 돈다 — 그동안 그 묶음은 아무도 안 도는 '대기 중'이다.
    잡는 것은 mark_pending 하나다: 같은 순간 api_run 이 사이트를 idle 로 보고 워커를
    띄우려 해도 둘 중 하나만 잡는다(두 벌 안 뜬다).
    """
    if not store.queued(conn, site["id"]) or not store.mark_pending(conn, site["id"]):
        return False
    store.save_run_log(conn, site["id"], "")
    scheduler.dispatch("--user", str(site["user_id"]), "--project", site["project"], "--queue")
    return True


def run_site(conn, site, *, dry_run: bool = False, skip: str | None = None,
             only: str | None = None, opts: dict[str, dict] | None = None,
             groups=None, queue: bool = False, merge_wait: float = MERGE_SECONDS) -> dict:
    """사이트 1건 처리. tenant() 안에서 유료 키를 주입하고 run_chain 을 호출.

    세 갈래다:
      only   단계 몇 개만(부분 실행) — 순서대로, 주기 시계는 안 건드린다(mark_busy).
      groups 묶음 런 — 묶음을 동시에 돌리고 그 묶음들의 시계를 찍는다(mark_groups).
             둘 다 없으면 전체 재기(모든 묶음)다 — 예전 '인자 없는 단일 실행'의 뜻 그대로.
      queue  화면·원격이 누른 대기열을 가져가 돈다(app.api_run 이 띄운다). 가져가기 전에
             merge_wait 초 기다린다 — 그 사이 누른 묶음이 같은 런으로 합쳐진다.

    한 런이 끝나면 **대기열을 다시 본다**: 도는 동안 누른 묶음이 있으면 이어서 돈다
    (라운드). 다음 라운드는 앞 라운드에서 잘 돈 공유 단계(rank 등)를 다시 사지 않는다 —
    run_chain(ran=…). 꼬리(gaps·pages·report)는 매 라운드 다시 돈다: 새로 잰 것으로
    기회를 다시 세워야 한다.

    opts 는 `--opt STAGE.KEY=VALUE` 로 들어온 단계별 노브다(원격 CLI 가 쓴다).

    체인이 뱉는 내레이션은 그대로 잡아 sites.run_log 에 둔다 — 원격 CLI 는 자기가
    요약표를 다시 만들지 않고 이걸 받아 그대로 print 한다(문구는 한 벌이다). 라운드가
    이어지면 로그도 이어 붙는다(원격은 문자 오프셋으로 폴링한다 — 지우면 오프셋이 깨진다).
    ponytail: 런당 1벌만 보관. 이력이 필요해지면 runs 테이블로.
    """
    user_id = site["user_id"]
    project = site["project"]
    sid = site["id"]

    if queue:
        time.sleep(merge_wait)
        groups = store.claim_queue(conn, sid)
        if not groups:
            # 남이 이미 가져갔거나(그 런이 돈다) 누른 것이 없다 — 대기 표시만 푼다.
            store.clear_pending(conn, sid)
            return {"user_id": user_id, "project": project, "ok": True, "rc": 0, "idle": True}
    elif not only and not groups:
        groups = list(run_all.RUNNABLE_GROUPS)

    # 서버 DB 연결 하나를 조정 스레드(진행률)와 단계 스레드(로그 흘리기)가 같이 쓴다 —
    # 한 연결을 두 스레드가 겹쳐 쓰지 않게 줄 세운다(run_all._run_groups 주석).
    db_lock = threading.Lock()

    def save_log(text: str) -> None:
        if not dry_run:
            with db_lock:
                store.save_run_log(conn, sid, text)

    # 시작 전에 찍는다. 끝나고 찍으면 (1) 등록 직후 트리거와 60초 스케줄러 틱이 겹쳐
    # 같은 사이트를 두 번 수집하고, (2) 실패한 사이트가 매 틱마다 재시도해 비용이 샌다.
    # 부분 실행(only)은 주기 시계를 안 건드린다 — 찍으면 주 1회 전체 런이 매번 밀린다.
    def start(groups_now, only_now) -> None:
        if dry_run:
            return
        with db_lock:
            if only_now:
                store.mark_busy(conn, sid)
            else:
                store.mark_groups(conn, sid, run_all.covered(run_all.plan(groups_now)))

    # 로그는 단계 경계뿐 아니라 **단계 도중에도** 흘려 보낸다(_Tee 의 시간 스로틀) —
    # rank 한 단계가 14분이라 경계에서만 쓰면 그동안 화면이 멈춰 보인다.
    buf = _Tee(sys.stdout, save=save_log)
    err = _Tee(sys.stderr, into=buf)          # 오류 줄도 같은 버퍼에, 순서대로

    # 화면이 폴링으로 읽는 값. 단계가 끝난 만큼만 센다 — 3단계를 시작한 시점의
    # 진행률은 2/8 이지, 3/8 이 아니다. 묶음 런은 name 에 도는 단계들이 쉼표로 온다.
    def on_stage(idx, total, name):
        if not dry_run:
            with db_lock:
                store.mark_stage(conn, sid, name, round((idx - 1) * 100 / total))
            save_log(buf.getvalue())

    # 백링크는 하루 단위로 안 움직인다 — 자체 주기(기본 30일)로만 잰다.
    skip, chain_opts = backlinks_plan(skip, settings.num("SEOMINER_BACKLINKS_EVERY_DAYS"))
    # 요청이 준 노브가 이긴다. 단계 단위가 아니라 키 단위로 합친다 — 단계째로 덮으면
    # 위에서 정한 백링크 주기가 사라져 한 런에 두 번 산다.
    for name, kv in (opts or {}).items():
        chain_opts.setdefault(name, {}).update(kv)

    if not dry_run:
        store.save_run_log(conn, sid, "")     # 지난 런의 로그를 남기지 않는다
    start(groups, only)

    ok, error, failed, rc = False, "", [], 0
    try:
        with store.tenant(conn, user_id), settings.paid_keys():
            # 첫 런의 재료 — 질문이 없으면 AI 인용 단계가 통째로 건너뛴다. 부수 작업이라
            # 여기서 터져도(키 없음·응답 없음) 수집은 그대로 간다.
            if not dry_run and (not only or "ai" in only.split(",")):
                try:
                    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
                        ensure_ai_prompts(project)
                except Exception as e:
                    print(f"[{project}] AI 질문 만들기 건너뜀: {e}", file=err)
            ran: set[str] = set()
            rounds = 0
            while True:
                rounds += 1
                with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
                    results = run_all.run_chain(
                        project, dry_run=dry_run, skip=skip, only=only, opts=chain_opts,
                        on_stage=on_stage, groups=None if only else groups, ran=ran,
                        # 서치콘솔 없는 사이트: 검색량이 나온 뒤·순위를 재기 전에 고른다
                        before={"rank": activate_keywords})
                    run_all.print_summary(project, results, dry_run=dry_run)
                rc = max(rc, run_all.chain_rc(results))
                # 실패한 단계는 화면·메일이 읽는 사실이 된다. 여태 rc 는 반환값으로만
                # 나가고 아무도 안 읽어서, 402 를 100번 맞은 런도 화면에는 그냥 '완료'였다.
                # 건너뜀은 실패가 아니다 — 실패인지 묻는 자리는 StageResult.failed 하나다.
                # (여기가 `not r.ok` 이던 동안 GA4 미연결 같은 정상 사이트가 매 런 실패
                #  메일을 받았다: Stage.skip() 이 ok=False 를 냈기 때문이다.)
                failed += [(n, r.reason or "이유가 기록되지 않았습니다")
                           for n, r in results if r.failed]
                # 다음 라운드가 다시 안 살 것 — 잘 돈 공유 단계. 꼬리는 늘 다시 돈다.
                ran |= {n for n, r in results
                        if r.ok and not r.skipped and n not in run_all.TAIL}
                if dry_run:
                    break
                with db_lock:
                    nxt = store.claim_queue(conn, sid)
                if not nxt:
                    break
                groups, only = nxt, None
                start(groups, only)
            ok = rc == 0
            error = "; ".join(f"{n}: {why}" for n, why in failed)

            # 이번 런에서 모은 GSC 실적으로 다음 런의 순위 측정 대상을 정한다.
            # 부수 작업이다 — 여기서 터져도 이미 끝난 수집을 실패로 만들지 않는다.
            n = 0
            try:
                n = 0 if dry_run else activate_from_gsc(project)
                if n:
                    print(f"[{project}] 키워드 {n}개 활성화 (서치콘솔 노출 기준)")
            except Exception as e:
                print(f"[{project}] 키워드 활성화 건너뜀: {e}")

            # 메일 재료는 반드시 tenant 안에서 모은다 — 밖에서는 db.CAPTURE_HOME 이
            # 서버 기본 경로를 가리켜 남의(혹은 빈) Brain 을 읽는다.
            stats, rep, to = None, None, _email_of(conn, site)
            notify = not dry_run and mailer.available() and to
            if notify and rc == 0:
                stats = _mail_stats(project)
                rep = _latest_report(project)

        # 발송은 네트워크 작업이라 tenant 밖에서 한다.
        # 메일이 안 갔다고 수집을 실패로 만들지 않는다.
        if stats is not None:
            try:
                mailer.run_done(to, project, stats, report=rep)
            except Exception as e:
                print(f"[{project}] 알림 메일 건너뜀: {e}")
        # 실패도 같은 자리에서 알린다. 성공만 메일이 가면 "메일이 안 왔다"는
        # "아직 도는 중"과 구분이 안 된다 — 사용자는 조용히 낡은 숫자를 본다.
        elif notify and failed:
            try:
                mailer.run_failed(to, project, failed)
            except Exception as e:
                print(f"[{project}] 실패 알림 메일 건너뜀: {e}")
        return {"user_id": user_id, "project": project, "rc": rc, "ok": rc == 0,
                "activated": n, "rounds": rounds}
    except Exception as e:
        # 로그에도 남긴다 — 원격 CLI 는 이 텍스트가 전부라, 여기 없으면 사용자에게는
        # 런이 조용히 끊긴 것으로 보인다. 사용자에게는 한 문장, 진단에는 traceback.
        LOG.exception("[%s/%s] 런이 예외로 끝났다", user_id, project)
        ok, error = False, f"수집이 중단됐습니다: {e}"
        buf.write(f"\n[오류] {error}\n")
        return {"user_id": user_id, "project": project, "ok": False, "error": str(e)}
    finally:
        if not dry_run:
            store.save_run_log(conn, sid, buf.getvalue())
            # 마지막에 끈다 — 로그를 먼저 굳힌다. 결과도 여기서 같이 굳혀야
            # 예외로 죽은 런까지 화면에 실패로 남는다.
            store.mark_done(conn, sid, ok=ok, error=error)
            # 마지막 claim_queue 와 방금 mark_done 사이에 누른 묶음 — 새 워커에 넘긴다.
            _hand_off(conn, site)


def run_all_due(conn, *, idle_days: int = 30, every_hours: float = 168.0,
                dry_run: bool = False, skip: str | None = None) -> list[dict]:
    """store.due_sites() 를 직렬로 돌린다 — tenant() 가 process-global env 를
    갈아끼우므로 사이트끼리 병렬은 안전하지 않다(스레드/프로세스 모두). 한 사이트
    **안의** 묶음은 run_chain 이 동시에 돌린다(env 는 그 사이트 것 하나다).

    사이트마다 주기를 넘긴 묶음(store.due_groups)과 대기열에 걸린 묶음을 한 런으로 돈다.

    목록은 스윕을 시작할 때 한 번 읽는다 — 앞 사이트를 몇 분 도는 사이에 사람이 이
    사이트의 버튼을 눌러 워커가 떴을 수 있다. 그래서 돌기 전에 사이트를 **원자적으로
    잡는다**(store.claim_site). 못 잡으면 남이 도는 중이다 — 건너뛴다. 판정도 잡은 뒤
    새로 읽은 행으로 한다.
    """
    results: list[dict] = []
    for site in store.due_sites(conn, idle_days=idle_days, every_hours=every_hours):
        fresh = site
        if not dry_run:
            fresh = store.claim_site(conn, site["id"])
            if fresh is None:
                print(f"[{site['user_id']}/{site['project']}] 건너뜀 — 다른 워커가 도는 중")
                continue
        want = set(store.due_groups(conn, fresh, every_hours))
        if not dry_run:
            want |= set(store.claim_queue(conn, site["id"]))
        groups = [g for g in run_all.RUNNABLE_GROUPS if g in want]
        if not groups:
            if not dry_run:                   # 잡았는데 잴 것이 없다 — 잡은 것을 푼다
                store.mark_done(conn, site["id"])
                _hand_off(conn, site)
            continue
        print(f"[{site['user_id']}/{site['project']}] 시작 — {','.join(groups)}")
        r = run_site(conn, site, dry_run=dry_run, skip=skip, groups=groups)
        results.append(r)
        print(f"[{site['user_id']}/{site['project']}] {'ok' if r.get('ok') else '실패'}")
    return results


def main() -> int:
    ap = argparse.ArgumentParser(description="seo-miner 수집 워커")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--all", action="store_true", help="스케줄 대상 전부")
    g.add_argument("--user", type=int, help="특정 유저 ID (--project 필수)")
    ap.add_argument("--project", help="--user 와 함께 — 사이트 프로젝트 이름")
    ap.add_argument("--idle-days", type=int, default=30)
    ap.add_argument("--every-hours", type=float,
                    default=settings.num("SEOMINER_RUN_EVERY_HOURS"),
                    help="사이트별 재측정 주기. 0 이면 자동 수집을 끈다")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--skip", help="건너뛸 단계 (쉼표 구분, 예: rank,ai)")
    ap.add_argument("--only", help="이 단계만 (쉼표 구분, 예: gsc)")
    ap.add_argument("--groups", help="이 묶음만 (쉼표 구분, 없으면 전체) — run_all.GROUPS 의 id")
    ap.add_argument("--queue", action="store_true",
                    help="--user/--project 와 함께 — 화면이 누른 대기열을 가져가 돈다")
    # 형식·타입 추정의 정본은 run_all.parse_opts 다 — 여기서 다시 파싱하지 않는다.
    ap.add_argument("--opt", action="append", metavar="STAGE.KEY=VALUE",
                    help="단계별 옵션 (반복 지정). 예: --opt rank.device=mobile")
    args = ap.parse_args()

    if args.user is not None and not args.project:
        ap.error("--user 는 --project 와 함께 써야 합니다")
    if args.only and args.groups:
        ap.error("--only 와 --groups 는 같이 쓸 수 없습니다")
    if args.opt and args.all:
        # 조용히 무시하면 옵션을 줬다고 믿는 쪽이 틀린 결과를 맞는 결과로 읽는다.
        ap.error("--opt 는 --user/--project 단일 실행에만 쓸 수 있습니다")

    conn = store.connect()
    try:
        if args.all:
            results = run_all_due(conn, idle_days=args.idle_days,
                                  every_hours=args.every_hours,
                                  dry_run=args.dry_run, skip=args.skip)
        else:
            sites = store.sites(conn, args.user)
            site = next((s for s in sites if s["project"] == args.project), None)
            if site is None:
                print(f"프로젝트 '{args.project}' 없음 (user={args.user})", file=sys.stderr)
                return 1
            groups = run_all.group_names(args.groups) if args.groups else None
            results = [run_site(conn, site, dry_run=args.dry_run, skip=args.skip,
                                only=args.only, opts=run_all.parse_opts(args.opt),
                                groups=groups, queue=args.queue)]
    finally:
        conn.close()

    ok = sum(1 for r in results if r.get("ok"))
    fail = len(results) - ok
    print(f"완료 {ok} / 실패 {fail}")
    return 1 if fail else 0


def demo() -> None:
    """API 호출 없이 워커 동작을 검증한다."""
    import tempfile
    from cryptography.fernet import Fernet

    for k in ("CAPTURE_HOME", "GSC_TOKEN_FILE", "OPENROUTER_API_KEY",
              "SERPER_API_KEY", "DATAFORSEO_LOGIN", "DATAFORSEO_PASSWORD",
              "SEOMINER_OPENROUTER_API_KEY"):
        os.environ.pop(k, None)

    with tempfile.TemporaryDirectory() as d:
        os.environ["SEOMINER_DATA"] = d
        os.environ["SEOMINER_SECRET_KEY"] = Fernet.generate_key().decode()
        conn = store.connect()

        uid = store.upsert_user(conn, "demo@example.com")
        store.add_site(conn, uid, "demo-proj", "sc-domain:demo.com", "demo.com")
        os.environ["SEOMINER_OPENROUTER_API_KEY"] = "test-key"
        # 서버 env 에 짝이 없는 잔여 키는 엔진에 보이면 안 된다 — 보이면 그냥 쓰고 돈이 샌다.
        os.environ["SERPER_API_KEY"] = "잔여값"

        called: list[dict] = []

        def fake_chain(project, *, dry_run=False, skip=None, only=None, opts=None,
                       on_stage=None, groups=None, ran=(), before=None):
            print("[가짜체인] 내레이션 한 줄")     # run_log 에 잡혀야 한다
            # 수집기의 오류 문장은 전부 stderr 다(collector.Stage.each) — 여태 화면
            # 로그에 한 줄도 안 실렸다. stdout 줄과 순서대로 섞여야 한다.
            print("[가짜체인] 402 Payment Required", file=sys.stderr)
            if on_stage:
                on_stage(3, 8, "keywords")  # 화면이 읽는 진행률이 실제로 찍히는지 본다
            called.append({
                # 단계 콜백이 지금까지의 텍스트를 이미 흘려 보냈는지 — 끝나고 한 번만
                # 쓰면 몇 분짜리 런 내내 원격 화면이 비어 있다.
                "log_midrun": conn.execute("SELECT run_log FROM sites").fetchone()[0],
                "project": project,
                "skip": skip,
                "opts": opts,
                "groups": groups,
                "only": only,
                "ran": set(ran or ()),
                "capture_home": os.environ["CAPTURE_HOME"],
                "openrouter": os.environ.get("OPENROUTER_API_KEY"),
                "serper": os.environ.get("SERPER_API_KEY"),
                "progress": conn.execute(
                    "SELECT stage, stage_pct FROM sites").fetchone()[:2],
            })
            return []                       # run_chain 은 [(단계, StageResult)] 를 돌려준다

        _real_chain[:] = [run_all.run_chain]      # 묶음 검사(_group_demo)가 진짜를 한 번 쓴다
        # 워커가 워커를 띄우는 자리(_hand_off) — 검사에서 진짜 프로세스를 띄우면 안 된다.
        scheduler.dispatch = lambda *a, **kw: _handed.append(a)
        run_all.run_chain = fake_chain

        results = run_all_due(conn)

        assert len(called) == 1, f"run_chain 호출 {len(called)}번"
        assert called[0]["capture_home"] == str(store.home(uid)), "CAPTURE_HOME 불일치"
        assert called[0]["openrouter"] == "test-key", "OPENROUTER_API_KEY 주입 실패"
        assert called[0]["serper"] is None, "짝 없는 잔여 키가 엔진에 노출됐다"
        assert called[0]["progress"] == ("keywords", 25), called[0]["progress"]
        assert "내레이션" in (called[0]["log_midrun"] or ""), \
            "단계 중간에 런 로그가 흘러가지 않는다 — 원격은 런이 끝날 때까지 빈 화면이다"
        log = conn.execute("SELECT run_log FROM sites").fetchone()[0]
        assert "내레이션" in log, "런이 끝났는데 로그가 안 남았다"
        # stderr 도 같은 버퍼에, 순서대로. 402 를 100번 맞은 런이 사용자에게는
        # 조용한 성공으로 보이던 자리다.
        assert "402 Payment Required" in log, "stderr 가 화면 로그에 안 실린다"
        assert log.index("내레이션") < log.index("402"), "stdout·stderr 순서가 섞였다"

        # 런 결과가 사이트에 굳는다 — 화면 배너와 실패 메일이 읽는 값이다.
        assert conn.execute("SELECT last_ok, last_error FROM sites").fetchone()[:2] == (1, None), \
            conn.execute("SELECT last_ok, last_error FROM sites").fetchone()[:2]

        # 단계 경계가 아니라 **단계 도중**에도 저장돼야 한다 — rank 한 단계가 14분이다.
        sid_t = store.sites(conn, uid)[0]["id"]
        rlog = lambda: conn.execute("SELECT run_log FROM sites WHERE id=?",  # noqa: E731
                                    (sid_t,)).fetchone()[0]
        store.save_run_log(conn, sid_t, "")
        tee = _Tee(io.StringIO(), save=lambda s: store.save_run_log(conn, sid_t, s),
                   every=0.05)
        tee.write("첫 줄\n")
        assert rlog() == "첫 줄\n", f"첫 줄이 안 흘렀다: {rlog()!r}"
        tee.write("바로 다음 줄\n")     # 아직 간격 안 지남 — 매 줄 UPDATE 하면 안 된다
        assert rlog() == "첫 줄\n", "줄마다 DB 를 쓴다 — 수천 줄이면 수천 번의 트랜잭션이다"
        time.sleep(0.06)
        tee.write("간격 지난 줄\n")
        assert "간격 지난 줄" in rlog(), "간격이 지났는데 중간 저장이 안 된다"
        # 백링크 주기는 체인 **안**에서 정해져야 한다. 여기 밖에 또 두면 한 런에 두 번
        # 사게 된다 — 유료 호출이라 그게 곧 돈이다.
        assert (called[0]["opts"] or {}).get("backlinks", {}).get("max_age") == 30.0,             f"백링크 주기가 단계 옵션으로 안 갔다: {called[0]['opts']}"
        assert "backlinks" not in (called[0]["skip"] or ""), "주기가 켜져 있는데 단계를 건너뛴다"
        # 0 = 끔. 신선도로는 못 끄므로(0일보다 오래됐나 = 늘 참) 단계를 건너뛰어야 한다.
        assert backlinks_plan(None, 0) == ("backlinks", {}), backlinks_plan(None, 0)
        assert backlinks_plan("rank", 0) == ("rank,backlinks", {}), backlinks_plan("rank", 0)
        assert backlinks_plan("rank", 7) == ("rank", {"backlinks": {"max_age": 7}})
        assert conn.execute("SELECT stage_pct FROM sites").fetchone()[0] is None, \
            "끝났는데 진행률이 남았다"
        assert len(results) == 1 and results[0]["ok"] is True, f"결과 이상: {results}"

        assert "CAPTURE_HOME" not in os.environ, "CAPTURE_HOME 복원 실패"
        assert "OPENROUTER_API_KEY" not in os.environ, "OPENROUTER_API_KEY 복원 실패"
        assert os.environ.pop("SERPER_API_KEY") == "잔여값", "바깥 env 를 복원하지 않았다"

        # 활성 키워드 상한 — 런마다 limit 개씩 더 켜면 추적 세트가 불어나 SERP 비용이 샌다.
        with store.tenant(conn, uid):
            c = db.connect()
            c.execute("INSERT INTO projects(name, type, domain) VALUES (?,?,?)",
                      ("demo-proj", "saas", "demo.com"))
            c.commit()
            pid = db.get_project(c, "demo-proj")["id"]
            c.executemany(
                "INSERT INTO keywords(project_id, keyword, source, is_active) VALUES (?,?,?,0)",
                [(pid, f"kw{i}", "gsc") for i in range(8)])
            c.executemany(
                "INSERT INTO gsc_snapshots(project_id, snapshot_date, period_days, query, "
                "impressions) VALUES (?,'2026-01-01',28,?,?)",
                [(pid, f"kw{i}", 100 - i) for i in range(8)])
            c.commit(); c.close()
            assert activate_from_gsc("demo-proj", limit=5) == 5, "상한만큼 안 켜진다"
            assert activate_from_gsc("demo-proj", limit=5) == 0, "상한을 넘겨 또 켠다"

            # 서치콘솔 없는 사이트 — 검색량 순으로, 사이트당 한 번만(사람이 끈 것을 되켜지 않게).
            c = db.connect()
            c.execute("INSERT INTO projects(name, type, domain, gsc_property) VALUES (?,?,?,'')",
                      ("nogsc", "commerce", "nogsc.test"))
            c.execute("INSERT INTO projects(name, type, domain, gsc_property) VALUES (?,?,?,?)",
                      ("hasgsc", "saas", "hasgsc.test", "sc-domain:hasgsc.test"))
            c.commit()
            ng, hg = db.get_project(c, "nogsc")["id"], db.get_project(c, "hasgsc")["id"]
            c.executemany(
                "INSERT INTO keywords(project_id, keyword, source, is_active, volume, verdict_off)"
                " VALUES (?,?,?,?,?,?)",
                # 넣는 순서(id)와 검색량 순서를 일부러 엇갈린다 — 같으면 정렬이 빠져도 통과한다.
                [(ng, "씨앗", "seed", 1, None, 0), (ng, "작은말", "autocomplete", 0, 10, 0),
                 (ng, "무관말", "autocomplete", 0, 99999, 1), (ng, "큰말", "autocomplete", 0, 9000, 0),
                 (ng, "모름말", "autocomplete", 0, None, 0), (ng, "중간말", "autocomplete", 0, 500, 0),
                 (hg, "남의말", "autocomplete", 0, 9000, 0)])
            c.commit(); c.close()
            assert activate_by_volume("hasgsc") == 0, "서치콘솔 있는 사이트를 검색량으로 켰다"
            assert activate_by_volume("nogsc", limit=3) == 2, "씨앗 포함 3개가 되게 2개만 켜야 한다"
            c = db.connect()
            on = {r[0] for r in c.execute(
                "SELECT keyword FROM keywords WHERE project_id=? AND is_active=1", (ng,))}
            assert on == {"씨앗", "큰말", "중간말"}, f"검색량 순·무관 판정 제외가 아니다: {on}"
            c.execute("UPDATE keywords SET is_active=0 WHERE project_id=? AND keyword='큰말'", (ng,))
            c.commit(); c.close()
            assert activate_by_volume("nogsc", limit=3) == 0, "사람이 끈 키워드를 다음 런에 되켰다"

            # 우리 순위 검색어가 주 출처 — gucci 모양: 브랜드 1위투성이 + 이미 순위권인 비브랜드.
            c = db.connect()
            c.execute("INSERT INTO projects(name, type, domain, gsc_property) VALUES (?,?,?,'')",
                      ("brandx", "commerce", "gc.test"))
            c.commit()
            bx = db.get_project(c, "brandx")["id"]
            db.set_seed_keywords(c, bx, ["구찌"])
            # 예전 판(검색량 순)이 켠 브랜드 검색어 — 새 판은 이걸 다시 고른다
            c.executemany("INSERT INTO keywords(project_id, keyword, source, is_active, volume) VALUES (?,?,?,?,?)",
                          [(bx, "구찌 로고", "autocomplete", 1, 90000), (bx, "구찌 지갑", "autocomplete", 0, 30000),
                           (bx, "구찌 모자", "autocomplete", 1, 1000),     # 새 선택에 안 드는 예전 것
                           # '구찌 가방'(1위)의 붙여 쓴 변형 — 순위권 밖이 아니다(되찾기 칸에 들면 안 된다)
                           (bx, "구찌가방", "autocomplete", 0, 50000)])
            db.set_project_settings(c, bx, {"auto_keywords": "2026-09-29"})
            c.commit(); c.close()
            labs_rows = [("구찌 로고", 1, 90000), ("구찌 벨트", 1, 60000), ("가방", 8, 60500),
                         ("카드 지갑", 11, 25000), ("카드지갑", 13, 24900), ("귀찌", 41, 1900),
                         ("구찌 가방", 1, 40000),
                         ("토트백", 6, 12100), ("지갑", 10, 14800), ("하남스타필드", 29, 8100),
                         ("your.gg", 25, 27100), ("셔츠", 19, 22200), ("구찌 가방 인기 순위", 7, 390)]
            posts = []

            def fake_post(path, body):
                posts.append(path)
                return [{"items": [{"keyword_data": {"keyword": k, "keyword_info": {"search_volume": v}},
                                    "ranked_serp_element": {"serp_item": {"rank_group": r,
                                                                          "url": f"https://gc.test/{k}"}}}
                                   for k, r, v in labs_rows]}], 0.06

            def fake_ask(prompt):
                assert "하남스타필드" in prompt and "/가방" in prompt, "판정에 걸린 페이지가 안 실렸다"
                # '귀찌'는 41위·검색어 하나뿐 — 우리 이름일 수 없다(데이터 검증으로 버려져야 한다)
                return {"brand_terms": ["구찌", "지어낸", "귀찌"], "drop": ["하남스타필드", "your.gg", "없는말"]}

            n = activate_keywords("brandx", limit=6, post=fake_post, ask=fake_ask)
            c = db.connect()
            on = {r[0] for r in c.execute("SELECT keyword FROM keywords WHERE project_id=? AND is_active=1", (bx,))}
            off = {r[0] for r in c.execute("SELECT keyword FROM keywords WHERE project_id=? AND verdict_off=1", (bx,))}
            flag = db.project_settings(c, bx).get("auto_keywords", "")
            nlabs = c.execute("SELECT COUNT(*) FROM labs_ranked WHERE project_id=?", (bx,)).fetchone()[0]
            c.close()
            assert nlabs == len(labs_rows), "우리 순위 검색어를 labs_ranked 에 안 적었다"
            c = db.connect()
            unbought = c.execute("SELECT COUNT(*) FROM keywords WHERE project_id=? AND source='labs_ranked'"
                                 " AND metrics_at IS NULL", (bx,)).fetchone()[0]
            c.close()
            assert unbought == 0, "검색량을 받은 순위 검색어를 지표 단계가 또 사게 남겼다(metrics_at 빈 채)"
            assert {"하남스타필드", "your.gg"} <= off, f"무관 판정이 안 걸렸다: {off}"
            # 씨앗 1 + 5칸: 올릴 기회 3(가방·카드 지갑·셔츠 — '카드지갑'은 같은 말이라 빠진다) + 되찾기 1
            #   (구찌 지갑 — '귀찌'는 브랜드로 안 받아서 이 칸 후보가 아니다) + 방어 1(구찌 로고).
            # 새 판 첫 번은 다시 고른다 — 예전 판이 켠 '구찌 모자'는 꺼진다(빈자리 채우기만 하면 남는다)
            assert on == {"구찌", "가방", "카드 지갑", "셔츠", "구찌 지갑", "구찌 로고"}, on
            assert flag.startswith(AUTO_VERSION), flag
            # 브랜드 표기는 AI 말만 믿지 않는다 — 그 말 자체로 1~3위이거나 3개 이상 검색어에 나와야 한다
            terms, _ = _judge_keywords(
                lambda _p: {"brand_terms": ["구찌", "귀찌", "구찌벨트"], "drop": []},
                {"domain": "gc.test"},
                [("구찌", 1, None), ("구찌 벨트", 2, None), ("구찌 지갑", None, None),
                 ("구찌 로고", 1, None), ("귀찌", 41, None)], [])
            assert "구찌" in terms and "구찌벨트" in terms and "귀찌" not in terms, terms
            # 다음 런 — 7일 안에는 다시 안 사고, 자리가 차 있으면 아무것도 안 바꾼다
            posts.clear()
            assert activate_keywords("brandx", limit=6, post=fake_post, ask=fake_ask) == 0
            assert posts == [], f"주기 안에 순위 검색어를 또 샀다: {posts}"
            # AI 가 없으면 예전 경로(검색량 순) — 이 사이트는 이미 새 판이라 아무것도 안 한다
            assert activate_keywords("brandx", limit=6, post=fake_post, ask=None) == 0
            c = db.connect()
            n = c.execute("SELECT COUNT(*) FROM keywords WHERE project_id=? AND is_active=1",
                          (pid,)).fetchone()[0]
            c.close()
            assert n == 5, f"활성 키워드가 {n}개 — 상한 5를 넘었다"

        # 메일 재료는 tenant 안에서 모아야 한다 — 밖에서 모으면 db.CAPTURE_HOME 이
        # 서버 기본 경로를 가리켜 빈 Brain 을 읽고 보고서 파일도 못 찾는다.
        with store.tenant(conn, uid):
            c = db.connect()
            pid2 = db.get_project(c, "demo-proj")["id"]
            c.execute("INSERT INTO opportunities(project_id, kind, target, score, status)"
                      " VALUES (?,?,?,?,'new')", (pid2, "striking_distance", "kw0", 90))
            c.commit(); c.close()
            rp = db.CAPTURE_HOME / "reports" / "demo-proj"
            rp.mkdir(parents=True, exist_ok=True)
            (rp / "2026-01-01.html").write_bytes(b"<html>report</html>")
            assert _latest_report("demo-proj") == b"<html>report</html>", "보고서를 못 읽는다"
            assert _mail_stats("demo-proj").get("opportunities") == 1, "통계를 못 읽는다"
        assert _latest_report("demo-proj") is None,             "tenant 밖인데 보고서를 찾았다 — 남의 경로를 보고 있다"

        # sites() 에는 email 컬럼이 없다 — 웹의 단일 사이트 실행이 여기서 터졌다.
        assert _email_of(conn, store.sites(conn, uid)[0]) == "demo@example.com",             "sites() 행에서 이메일을 못 얻는다"

        # 돌고 나면 사이트에 도장이 찍혀서 다음 틱에 또 돌지 않아야 한다.
        assert run_all_due(conn) == [], "방금 쟀는데 또 잰다"
        assert len(called) == 1, "중복 실행됐다"

        # --opt 로 들어온 노브(원격 CLI 의 `--device mobile`)가 체인까지 간다.
        # 단계째로 덮으면 안 된다 — 위에서 정한 백링크 주기가 사라져 한 런에 두 번 산다.
        run_site(conn, store.sites(conn, uid)[0], opts={"rank": {"device": "mobile"}})
        assert called[-1]["opts"]["rank"] == {"device": "mobile"}, called[-1]["opts"]
        assert called[-1]["opts"]["backlinks"]["max_age"] == 30.0, \
            f"요청 옵션이 백링크 주기를 덮어썼다: {called[-1]['opts']}"
        assert conn.execute("SELECT run_log FROM sites").fetchone()[0].count("내레이션") == 1, \
            "런 로그가 지난 런에 이어붙었다 — 런당 1벌이어야 한다"

        # 실패한 단계가 있으면 화면과 메일이 그것을 안다. 여태 chain_rc 는 반환값으로만
        # 나가고 아무도 안 읽어서, 402 를 100번 맞은 런도 화면에는 그냥 '완료'였다.
        from collector import StageResult          # 계약: run_chain 은 [(단계, StageResult)]
        run_all.run_chain = lambda project, **kw: [
            ("gsc", StageResult(ok=True)),
            ("rank", StageResult(ok=False, reason="DataForSEO 잔액 없음(402)"))]
        sent, real_avail, real_failed = [], mailer.available, mailer.run_failed
        mailer.available = lambda: True
        mailer.run_failed = lambda *a, **kw: sent.append(a)
        try:
            r = run_site(conn, store.sites(conn, uid)[0])
        finally:
            mailer.available, mailer.run_failed = real_avail, real_failed
        assert r["ok"] is False and r["rc"] == 1, r
        row = conn.execute("SELECT last_ok, last_error FROM sites").fetchone()
        assert (row["last_ok"], row["last_error"]) == (0, "rank: DataForSEO 잔액 없음(402)"), \
            tuple(row)
        assert sent and sent[0][2] == [("rank", "DataForSEO 잔액 없음(402)")], sent

        # 반대쪽: **건너뜀만 있는 런은 실패가 아니다.** GA4 속성 미연결·활성 키워드
        # 없음 같은 정상 사이트가 Stage.skip() 의 ok=False 때문에 매 런 실패로 끝나고
        # 실패 메일을 받던 자리다.
        # 두 번째 줄은 **옛 꼴**(ok=False + skipped=True)을 일부러 넣는다 — 판정이
        # `not r.ok` 로 돌아가면 여기서 걸린다. 실패인지는 skipped 까지 같이 봐야 한다.
        run_all.run_chain = lambda project, **kw: [
            ("gsc", StageResult(ok=True)),
            ("ga4", StageResult(ok=False, skipped=True,
                                reason="GA4 속성이 연결되어 있지 않습니다")),
            ("rank", collector.skipped("활성 키워드 없음"))]
        sent.clear()
        mailer.available, mailer.run_failed = (lambda: True), lambda *a, **kw: sent.append(a)
        try:
            r = run_site(conn, store.sites(conn, uid)[0])
        finally:
            mailer.available, mailer.run_failed = real_avail, real_failed
        assert r["ok"] is True and r["rc"] == 0, f"건너뜀이 실패로 셌다: {r}"
        row = conn.execute("SELECT last_ok, last_error FROM sites").fetchone()
        assert (row["last_ok"], row["last_error"]) == (1, None), tuple(row)
        assert not sent, f"건너뜀뿐인 런에 실패 메일이 나갔다: {sent}"

        # 예외로 죽은 런도 실패로 남아야 한다 — 조용히 '완료' 로 남으면 안 된다.
        def boom(project, **kw):
            raise RuntimeError("체인이 터졌다")

        run_all.run_chain = boom
        run_site(conn, store.sites(conn, uid)[0])
        row = conn.execute("SELECT last_ok, last_error FROM sites").fetchone()
        assert row["last_ok"] == 0 and "체인이 터졌다" in (row["last_error"] or ""), tuple(row)
        run_all.run_chain = fake_chain

        _group_demo(conn, uid, called)

        conn.execute("UPDATE users SET last_seen_at=datetime('now','-60 days')")
        conn.commit()
        before = len(called)
        results = run_all_due(conn, idle_days=30)
        assert results == [], f"휴면 유저인데 결과 {results}"
        assert len(called) == before, "run_chain 이 추가로 호출됨"

        conn.close()                            # 윈도우: 열린 파일이 있으면 임시 디렉토리 삭제 실패
        print("worker: ok")


def _group_demo(conn, uid, called) -> None:
    """묶음 런 — 누른 묶음만 돌고 그 시계만 찍힌다, 몇 초 안에 누른 것은 한 런, 도는 중에
    누른 것은 다음 라운드(공유 단계는 안 산다), 부분 실행은 시계를 안 건드린다,
    동시 단계가 한 런 로그에 줄 단위로 온전히 모인다."""
    import threading as _th
    from collector import StageResult
    site = lambda: store.sites(conn, uid)[0]           # noqa: E731
    sid = site()["id"]
    ALL = list(run_all.RUNNABLE_GROUPS)

    def age(hours):
        conn.execute("UPDATE site_groups SET last_run_at=datetime('now', ?) WHERE site_id=?",
                     (f"-{hours} hours", sid))
        conn.commit()

    # 1. 누른 묶음만 돈다 — 시계도 그 묶음만(검색 성과는 매일 런까지 한 셈이다)
    age(1000)
    run_site(conn, site(), groups=["search"])
    assert called[-1]["groups"] == ["search"] and called[-1]["only"] is None, called[-1]
    due = store.due_groups(conn, site(), 168.0)
    assert "search" not in due and "todo" not in due and "ai" in due, \
        f"누른 묶음 말고 다른 시계까지 찍혔다: {due}"
    # 2. 부분 실행(only)은 묶음 시계를 안 건드린다
    before = store.group_clocks(conn, site())
    run_site(conn, site(), only="gsc")
    assert called[-1]["only"] == "gsc" and called[-1]["groups"] is None, called[-1]
    assert store.group_clocks(conn, site()) == before, "부분 실행이 묶음 시계를 찍었다"

    # 3. 몇 초 안에 연달아 누른 것은 한 런으로 — 워커가 기다렸다가 가져간다
    n0 = len(called)
    assert store.mark_pending(conn, sid)
    store.queue_groups(conn, sid, ["ai"])
    t = _th.Thread(target=lambda: run_site(conn, site(), queue=True, merge_wait=0.4))
    t.start()
    time.sleep(0.1)
    c2 = store.connect()                                 # 화면의 두 번째 누름(다른 요청)
    store.queue_groups(c2, sid, ["site"])
    c2.close()
    t.join()
    assert len(called) == n0 + 1, f"연달아 누른 것이 런 {len(called) - n0}벌로 갈렸다"
    assert called[-1]["groups"] == ["ai", "site"], called[-1]["groups"]
    assert store.run_phase(site()) == "idle" and store.queued(conn, sid) == []

    # 4. 가져갈 대기열이 없으면(남이 가져갔다) 대기 표시만 푼다 — 체인을 안 부른다
    assert store.mark_pending(conn, sid)
    r = run_site(conn, site(), queue=True, merge_wait=0)
    assert r.get("idle") and len(called) == n0 + 1, r
    assert store.run_phase(site()) == "idle", "빈 대기열인데 '대기 중'이 남았다"

    # 5. 도는 중에 누른 것은 다음 라운드 — 앞 라운드에서 잘 돈 공유 단계는 안 산다
    rounds = []

    def chain(project, **kw):
        rounds.append({"groups": kw.get("groups"), "ran": set(kw.get("ran") or ())})
        if len(rounds) == 1:                            # 도는 동안 [AI 노출] 을 누른다
            c3 = store.connect()
            assert store.run_phase(store.site(c3, uid, project)) == "running"
            store.queue_groups(c3, sid, ["ai"])
            c3.close()
        print(f"라운드 {len(rounds)}")
        return [("rank", StageResult(ok=True)), ("gsc", StageResult(ok=False, reason="x")),
                ("gaps", StageResult(ok=True))]

    prev = run_all.run_chain
    run_all.run_chain = chain
    try:
        r = run_site(conn, site(), groups=["search"])
    finally:
        run_all.run_chain = prev
    assert [x["groups"] for x in rounds] == [["search"], ["ai"]], rounds
    assert rounds[1]["ran"] == {"rank"}, \
        f"다음 라운드가 공유 단계를 또 산다(또는 실패·꼬리까지 건너뛴다): {rounds[1]['ran']}"
    assert r["rounds"] == 2 and r["ok"] is False, r
    log = conn.execute("SELECT run_log FROM sites WHERE id=?", (sid,)).fetchone()[0]
    assert log.index("라운드 1") < log.index("라운드 2"), "라운드 로그가 이어 붙지 않았다"

    # 6. 진짜 run_chain(가짜 단계표)으로 — 동시 단계의 줄이 런 로그에 온전히, 진행률이 찍힌다
    real = _real_chain[0]
    seen_stage = []

    def fake_stage(name):
        def fn(project, *, dry_run=False, **opts):
            for i in range(30):
                print(name, "줄", i)                     # print 는 write 를 여러 번 부른다
            # 자기 연결로 읽는다 — 워커의 conn 을 단계 스레드들이 겹쳐 쓰면 sqlite3 가
            # InterfaceError 를 낸다(이 검사가 처음에 그렇게 흔들렸다: 워커가 db_lock 을
            # 두는 이유와 같다).
            c4 = store.connect()
            try:
                seen_stage.append(c4.execute("SELECT stage FROM sites WHERE id=?",
                                             (sid,)).fetchone()[0])
            finally:
                c4.close()
            return StageResult(ok=True)
        return fn

    table = tuple(s._replace(fn=fake_stage(s.name), is_paid=False) for s in run_all.STAGES)
    run_all.run_chain = lambda project, **kw: real(project, stages=table,
                                                   preflight=lambda s: {}, **kw)
    try:
        with store.tenant(conn, uid):                 # 가짜 단계도 Brain(WAL 전환)을 연다
            pass
        r = run_site(conn, site(), groups=["ai", "site"])
    finally:
        run_all.run_chain = prev
    assert r["ok"], r
    log = conn.execute("SELECT run_log FROM sites WHERE id=?", (sid,)).fetchone()[0]
    for n in run_all.plan(["ai", "site"]):
        for i in range(30):
            assert f"[{n}] {n} 줄 {i}\n" in log, f"줄이 섞이거나 빠졌다: [{n}] {n} 줄 {i}"
    assert any(s_ and "," in s_ for s_ in seen_stage), \
        f"동시에 도는 단계가 진행 상태에 안 실렸다: {seen_stage}"

    # 7. 스윕은 목록을 읽은 뒤 남이 잡은 사이트를 안 돈다 — 두 워커가 한 사이트를 같이 돈다
    age(1000)
    listed = store.due_sites(conn)
    assert [r["id"] for r in listed] == [sid], "검사 전제: 이 사이트가 밀려 있어야 한다"
    store.mark_busy(conn, sid)                        # 그 사이 [구글 실적만 다시 읽기]
    real_due, n0 = store.due_sites, len(called)
    store.due_sites = lambda *a, **kw: listed          # 스윕이 시작할 때 읽은 목록
    try:
        assert run_all_due(conn) == [], "남이 도는 사이트를 스윕이 또 돌았다"
    finally:
        store.due_sites = real_due
    assert len(called) == n0, "남이 도는 사이트에서 체인이 또 불렸다 — 유료 단계가 겹친다"
    assert store.run_phase(site()) == "running", "남이 도는 런의 표시를 스윕이 껐다"
    store.mark_done(conn, sid)

    # 8. 마지막 claim_queue 와 mark_done 사이에 누른 묶음 — 새 워커에 넘긴다
    real_done = store.mark_done
    pressed = []

    def done_after_press(c, site_id, **kw):
        if not pressed:
            pressed.append(1)
            c5 = store.connect()                       # 화면의 누름(다른 요청)
            store.queue_groups(c5, sid, ["compete"])
            c5.close()
        return real_done(c, site_id, **kw)

    _handed.clear()
    store.mark_done = done_after_press
    try:
        run_site(conn, site(), groups=["search"])
    finally:
        store.mark_done = real_done
    assert _handed and _handed[-1][-1] == "--queue" and str(uid) in _handed[-1], \
        f"끝나는 순간 누른 묶음을 아무도 안 돈다(스윕 중이면 몇 시간): {_handed}"
    assert store.run_phase(site()) == "pending", "넘기기 전에 '대기 중'으로 안 잡았다"
    assert store.claim_queue(conn, sid) == ["compete"]
    store.mark_done(conn, sid)
    # 대기열이 비었으면 아무도 안 띄운다
    _handed.clear()
    run_site(conn, site(), groups=["search"])
    assert not _handed, f"넘길 것이 없는데 워커를 띄웠다: {_handed}"


_real_chain: list = []
_handed: list = []


if __name__ == "__main__":
    if len(sys.argv) == 1:
        demo()
    else:
        sys.exit(main())