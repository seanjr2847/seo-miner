#!/usr/bin/env python3
"""AI visibility check via DataForSEO LLM Responses (one key, provider-native web search).

For each active ai_prompt x engine (x samples), asks the question with web
search enabled and records:
  mentioned       brand alias appears in the answer text
  cited           own domain appears in url_citation annotations
  cited_domains   who IS cited (fuel for the citation-gap analysis)
  recommended     brand alias sits on a list line (numbered/bulleted/table) —
                  a heuristic for "made the recommendation list" (scoring.recommended_in)

Notes on measurement honesty (see references/scoring.md):
  * Answers are non-deterministic -> one check is a sample, not a fact.
  * API+native search approximates, not equals, the consumer apps.
  * Search tool calls carry per-request fees on some providers -> caps + dry-run.

Env: DATAFORSEO_LOGIN / DATAFORSEO_PASSWORD
     (OPENROUTER_API_KEY 는 이 단계가 아니라 질문 만들기(gen_prompts)·경쟁사 판정·할 일이 쓴다 —
      그쪽은 지시가 길어 DataForSEO 의 500자 상한에 안 들어간다. ask_openrouter 참고)
Usage:
  python collect_ai.py --project NAME [--engines chatgpt,perplexity,gemini]
                       [--samples 1] [--max-prompts 30] [--ids 16,17]
                       [--category 사실] [--dry-run]
"""
import argparse
import sys
from datetime import date
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))
import collector  # noqa: E402
import db  # noqa: E402
import fanout  # noqa: E402
import scoring  # noqa: E402
import serp_adapter  # noqa: E402

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# 소비자 앱이 system 자리에 싣는 것 중 답을 바꾸는 둘 — 사용자의 위치·언어와 오늘 날짜.
# 로케일을 안 실으면 한국어 질문에도 미국 소스가 붙는다(소비자 앱은 위치를 안다). 날짜가
# 없으면 "올해 추천"이 모델의 학습 시점 기준이 된다. "출처를 달아라" 같은 지시는 싣지 않는다
# — 앱은 그런 지시 없이 답하고, 지시하면 인용이 앱보다 많아진다.
SYSTEM = ("The user is in {country} and uses {lang} ({locale}). Current date: {today}. "
          "Answer in the language of the question.")
# 답 길이 상한. 앱에는 상한이 없다 — 1200 이면 긴 추천 목록 뒤쪽 인용이 잘렸고, 생각하는
# 모델은 생각 토큰도 여기서 센다. 토큰 비용 상한이기도 하다.
MAX_TOKENS = 2000


def engines(names: list[str] | None = None) -> dict[str, dict]:
    """물을 엔진 {id: 표 한 줄}. 표의 정본은 skill_config.CONFIG['ai_engines'] 다.

    names(사이트의 surfaces_ai 나 --engines)를 주면 그중 표에 있는 것만, 안 주면 표의
    default 엔진. 표에 없는 이름은 버린다 — 모델을 모르는 엔진은 물을 수 없다.
    """
    table = collector.config()["ai_engines"]
    if not names:
        return {k: e for k, e in table.items() if e["default"]}
    return {k: table[k] for k in names if k in table}


def estimate_usd(project: str | None) -> float:
    """이번 AI 인용 확인의 추정 비용 — 켠 질문 수(상한 안) × 표본 × 엔진 단가 합.
    사이트를 못 읽으면 상한·기본 엔진으로 잡는다(모르면 넉넉하게)."""
    gcfg = collector.config()
    samples = int((gcfg.get("defaults") or {}).get("ai_samples") or 2)
    cap = 30                              # _parser 의 --max-prompts 기본값과 같다(시장마다)
    n, chosen = cap, None
    if project:
        try:
            conn = db.connect()
            try:
                p = db.get_project(conn, project)
                # 상한은 시장마다다(collect) — 시장마다 켠 질문 수를 상한으로 자른 합.
                n = sum(min(m["active"], cap) for m in db.ai_prompt_markets(conn, p["id"]))
                chosen = db.project_cfg(conn, p).get("surfaces_ai")
            finally:
                conn.close()
        except Exception:
            n = cap
    usd = sum(e["usd"] for e in engines(chosen).values())
    return round(n * samples * usd, 2)


def _openrouter_state(key: str) -> tuple[bool, str, float | None]:
    """(쓸 수 있나, 사람 말, 남은 돈 | 모름). GET /auth/key 와 GET /credits — 둘 다 무료.

    남은 돈은 둘 중 작은 것이다: 키마다 건 한도(auth/key 의 limit_remaining)와 계정 잔액
    (credits 의 total_credits - total_usage). 어느 쪽이든 못 읽으면 그 쪽은 모름이다.
    """
    hdr = {"Authorization": f"Bearer {key}"}
    t = serp_adapter.TIMEOUTS["canary"]
    left: list[float] = []
    r = requests.get("https://openrouter.ai/api/v1/auth/key", timeout=t, headers=hdr)
    if r.status_code in serp_adapter.FATAL_STATUS:
        return False, str(serp_adapter.fatal("OpenRouter", r.status_code)), None
    r.raise_for_status()
    d = (r.json().get("data") or {})
    limit, used = d.get("limit"), d.get("usage")
    if limit is not None and used is not None and float(used) >= float(limit):
        return False, (f"OpenRouter 크레딧 소진 (${float(used):.2f}/${float(limit):.2f}). "
                       f"{serp_adapter.FATAL_FIX['OpenRouter']}"), 0.0
    if isinstance(d.get("limit_remaining"), (int, float)):
        left.append(float(d["limit_remaining"]))
    try:
        c = requests.get("https://openrouter.ai/api/v1/credits", timeout=t, headers=hdr)
        if c.status_code == 200:
            cd = c.json().get("data") or {}
            tot, use = cd.get("total_credits"), cd.get("total_usage")
            if isinstance(tot, (int, float)) and isinstance(use, (int, float)):
                left.append(float(tot) - float(use))
    except Exception:
        pass                              # 계정 잔액을 못 읽었다 — 키 판정만으로 간다
    return True, "OpenRouter 키 유효", (min(left) if left else None)


def openrouter_ok(need: float = 0.0) -> tuple[bool, str]:
    """키가 살아 있고 need 달러를 쓸 돈이 있나 — **무료 호출**. 반환: (쓸 수 있나, 사람 말).

    돈 쓰는 단계(ai·plays) 앞과 doctor 진단이 같은 이 함수를 쓴다(판정 두 벌 금지).
    확인 자체가 실패하면(네트워크·모르는 응답) True 다 — 카나리아가 수집을 막으면 안 된다.
    막는 건 "확실히 못 쓴다"일 때뿐: 키가 죽었거나, 남은 돈이 need 보다 적다. 호스팅 키가
    $0.14 로 떨어졌을 때 AI 인용·판정·할 일이 402 를 맞고서야 알았다 — 이제는 돈 쓰기 전에.
    읽은 값은 몇 분 기억한다(serp_adapter.canary_cached) — 단계마다 다시 안 묻는다.
    """
    import os
    if not serp_adapter.has_openrouter():
        return False, "OPENROUTER_API_KEY 없음"
    key = os.environ["OPENROUTER_API_KEY"]
    try:
        ok, msg, left = serp_adapter.canary_cached("openrouter", key,
                                                   lambda: _openrouter_state(key))
    except Exception as e:
        return True, f"OpenRouter 키 확인 실패 ({e}). 그대로 진행합니다"
    if not ok:
        return False, msg
    if left is not None and need > 0 and left < need:
        return False, (f"OpenRouter 잔액 ${left:.2f}: 이번 단계 예상 ${need:.2f} 보다 적어 돈 쓰기 "
                       f"전에 건너뜁니다. {serp_adapter.FATAL_FIX['OpenRouter']}")
    return True, (f"OpenRouter 키 유효 · 잔액 ${left:.2f}" if left is not None else msg)


# ── DataForSEO LLM Responses — AI 인용 확인이 묻는 길 ──────────────────────────
# https://docs.dataforseo.com/v3/ai_optimization-chat_gpt-llm_responses-live/ (claude·gemini·perplexity 같은 꼴)
# 2026-11-24 실호출로 확인한 것:
#   · user_prompt·system_message·message_chain 은 각각 **500자**가 상한이다 — 넘으면 task 가
#     40501 "Invalid Field" 로 거절된다(돈은 안 나간다). 그래서 긴 지시를 보내는 일(질문 만들기·
#     경쟁사 판정·할 일 수정안)은 이 길에 못 싣고 OpenRouter 에 남는다.
#   · 답은 result[0].items 중 type=message 의 sections[type=text].text, 인용은 그 section 의
#     annotations[{title,url}] (Gemini 는 url 이 Vertex 중계 주소라 direct_url 이 원 주소다).
#   · 실청구액은 task.cost(= 기본 단가 + money_spent) — post_dataforseo 가 그 값을 돌려준다.
#   · max_output_tokens 는 생각하는 모델이면 1024 이상이어야 한다(아니면 40501) — 안 보낸다(기본 2048).
DFS_LLM_PATH = "/ai_optimization/{platform}/llm_responses/live"
PROMPT_MAX = 500
LLM_TIMEOUT = 180            # 웹 검색까지 하면 평균 수십 초, 문서는 최대 120초 안팎이라고 한다
# web_search_country_iso_code 를 받는 플랫폼. claude 는 정해진 나라 목록만 받는다(문서의 enum).
_COUNTRY_ANY = frozenset({"chat_gpt", "perplexity"})
_COUNTRY_CLAUDE = frozenset({
    "AR", "AT", "AU", "BE", "BR", "CA", "CH", "CL", "CN", "DE", "DK", "ES", "FI", "FR", "GB", "HK",
    "ID", "IN", "IT", "JP", "KR", "MX", "MY", "NL", "NO", "NZ", "PH", "PL", "PT", "RU", "SA", "SE",
    "TR", "TW", "US", "ZA"})


def _country_of(locale: str) -> str | None:
    """'ko-KR' → 'KR'. 지역 없는 로케일은 None."""
    _, _, region = (locale or "").partition("-")
    return region.upper() if len(region) == 2 else None


def llm_body(e: dict, prompt: str, locale: str) -> dict:
    """엔진 표 한 줄 + 질문 → DataForSEO task 하나. 500자를 넘는 질문은 ValueError(그 항목의 실패)."""
    if len(prompt) > PROMPT_MAX:
        raise ValueError(f"질문이 {len(prompt)}자라 DataForSEO 상한({PROMPT_MAX}자)을 넘습니다 — 줄여 주세요")
    locale = locale or db.DEFAULT_LOCALE
    lang, country = serp_adapter.describe(locale)
    system = SYSTEM.format(locale=locale, lang=lang, country=country, today=date.today().isoformat())
    plat = e["platform"]
    body = {**(e.get("body") or {}), "user_prompt": prompt, "model_name": e["model"],
            "system_message": system[:PROMPT_MAX]}
    if plat != "perplexity":            # sonar 는 검색이 모델에 들어 있다 — 그 칸이 없다
        body["web_search"] = True
    cc = _country_of(locale)
    if cc and (plat in _COUNTRY_ANY or (plat == "claude" and cc in _COUNTRY_CLAUDE)):
        body["web_search_country_iso_code"] = cc
    return body


def parse_llm(result: list) -> dict:
    """DataForSEO result → {content, citation_urls, citations, model}. 인용은 같은 URL 한 번."""
    r0 = (result or [{}])[0] or {}
    texts: list[str] = []
    urls: list[str] = []
    cites: list[dict] = []
    seen: dict[str, dict] = {}
    for it in r0.get("items") or []:
        if not isinstance(it, dict) or it.get("type") != "message":
            continue
        for s in it.get("sections") or []:
            if s.get("type") == "text" and s.get("text"):
                texts.append(s["text"])
            for a in s.get("annotations") or []:
                u = (a.get("direct_url") or a.get("url") or "").strip()
                if not u:
                    continue
                urls.append(u)
                title = (a.get("title") or "").strip() or None
                if u not in seen:
                    seen[u] = {"url": u, "title": title}
                    cites.append(seen[u])
                elif seen[u]["title"] is None:
                    seen[u]["title"] = title
    return {"content": "\n\n".join(texts), "citation_urls": urls, "citations": cites,
            "model": r0.get("model_name")}


def ask(e: dict, prompt: str, locale: str) -> dict:
    """AI 인용 확인 한 번 — 엔진 표의 그 줄(platform·model·body)로 DataForSEO 에 묻는다.
    401/402(키·잔액)는 serp_adapter.raise_for 가 Fatal 로 올린다 — 질문 60개를 계속 던지지 않는다."""
    result, cost = serp_adapter.post_dataforseo(
        DFS_LLM_PATH.format(platform=e["platform"]), [llm_body(e, prompt, locale)],
        timeout=LLM_TIMEOUT)
    return {**parse_llm(result), "usage": {"cost": cost}}


def ask_openrouter(model: str, prompt: str, api_key: str, locale: str,
                   body: dict | None = None) -> dict:
    """OpenRouter 로 한 번 묻는다 — 질문 만들기(gen_prompts)만 쓴다. 그쪽 지시는 사이트 소개를
    통째로 실어 DataForSEO 의 500자 상한을 넘는다. body 는 요청에 더 싣는 값."""
    locale = locale or db.DEFAULT_LOCALE
    lang, country = serp_adapter.describe(locale)
    body = {
        **(body or {}),
        "model": model,
        "max_tokens": MAX_TOKENS,
        # 응답이 실청구액(usage.cost)을 싣게 한다 — runs.cost_estimate_usd 가 추정이 아니게.
        "usage": {"include": True},
        "messages": [
            {"role": "system", "content": SYSTEM.format(
                locale=locale, lang=lang, country=country, today=date.today().isoformat())},
            {"role": "user", "content": prompt},
        ],
    }
    r = requests.post(
        OPENROUTER_URL, timeout=serp_adapter.TIMEOUTS["openrouter"],
        headers={"Authorization": f"Bearer {api_key}",
                 "Content-Type": "application/json"},
        json=body)
    # 401/402 는 Fatal — 키가 죽었거나 크레딧이 없는데 질문 60개를 계속 던지지 않는다.
    serp_adapter.raise_for(r, "OpenRouter")
    data = r.json()
    msg = data.get("choices", [{}])[0].get("message", {}) or {}
    content = msg.get("content") or ""
    urls = []
    cites = []                  # [{url, title}] — 같은 URL 은 한 번(제목은 먼저 나온 비지 않은 것)
    seen: dict[str, dict] = {}
    for ann in (msg.get("annotations") or []):
        if ann.get("type") == "url_citation":
            uc = ann.get("url_citation") or {}
            u = uc.get("url", "")
            if u:
                urls.append(u)
                title = (uc.get("title") or "").strip() or None
                if u not in seen:
                    seen[u] = {"url": u, "title": title}
                    cites.append(seen[u])
                elif seen[u]["title"] is None:
                    seen[u]["title"] = title
    # 인용 메타데이터가 없을 때의 맨 URL fallback은 scoring.judge·scoring.citations_of 가 한다.
    return {"content": content, "citation_urls": urls, "citations": cites,
            "usage": data.get("usage", {})}


def collect(project: str, *,
            dry_run: bool = False,
            engines: str | None = None,
            samples: int | None = None,
            max_prompts: int | None = None,
            throttle: float | None = None,
            ids: str | None = None,
            category: str | None = None,
            force: bool = False,
            llm_mentions: int | None = None,
            conn=None) -> collector.StageResult:
    """AI 가시성 체크 (DataForSEO LLM Responses) — 결과를 Brain 에 적재한다.

    Args:
        project: 사이트 이름
        dry_run: True 면 호출 계획만 찍고 종료
        engines: comma list (예: "chatgpt,perplexity,gemini"). 미지정시 config.
        samples: 프롬프트×엔진 당 샘플 수(config 키는 ai_samples) —
            CLI 플래그(--samples)와 이름을 맞춘다.
        max_prompts: 활성 프롬프트 상한(config 키는 limits.max_ai_prompts) —
            CLI 플래그(--max-prompts)와 이름을 맞춘다.
        throttle: 요청 간격(초)
        ids: 쉼표 구분 ai_prompt id — 지정하면 is_active 무시
        category: 이 카테고리의 활성 프롬프트만 실행
        force: 오늘 이미 확인한 질문도 재확인
        llm_mentions: 1 이면 LLM 언급 집계도 받는다(config 키는 ai_llm_mentions, 유료)
        conn: 이미 열린 Brain 연결 — 주면 그것을 쓰고 닫지 않는다

    Returns:
        StageResult(ok=...). 사유 있는 비종료는 ok=False, skipped=True.
    """
    ap = _parser()
    with collector.stage(project, conn=conn, dry_run=dry_run) as st:
        conn, p, cfg = st.conn, st.project, st.cfg
        s = st.settings(ap, argparse.Namespace(
            throttle=throttle, samples=samples, max_prompts=max_prompts,
            llm_mentions=llm_mentions))
        samples = s["ai_samples"]
        max_ai_prompts = s["limits.max_ai_prompts"]

        # 인자 engines(쉼표 목록)가 모듈 함수 engines 를 가린다 — 함수는 모듈에서 꺼낸다.
        pick_engines = globals()["engines"]
        engines_d = (pick_engines([e.strip() for e in engines.split(",")]) if engines
                     else pick_engines(cfg.get("surfaces_ai")))

        # 부분 실행. 이게 없으면 "새로 넣은 8개만 돌려보자"에도 is_active를 손으로
        # 토글해야 하고, 되돌릴 때 통째로 UPDATE 해서 큐레이션한 활성 집합을 날린다.
        limit = max_ai_prompts
        # 상한보다 켜 둔 질문이 많을 때 **무엇을 먼저** 묻나. 예전엔 ORDER BY id 라 새로
        # 만든 질문(큰 id)이 옛 질문에 밀려 상한 밖으로 나갔고, 끄지 않는 한 영영 안
        # 재졌다 — 3차 감사가 본 "뒤에 등록된 시술 질문 17개가 한 번도 측정 안 됨"이
        # 이것이다. 순서: ① 끝난 회차에서 한 번도 안 잰 것 ② 마지막 측정이
        # AI_STALE_DAYS 를 넘은 것 ③ 나머지는 id 순. 매번 가장 오래된 것부터 돌리면
        # 회차마다 묻는 묶음이 바뀌어 회차 사이 인용률 추세를 못 견준다 — 그래서 ③ 은
        # 고정 순서다. "잰 것"은 측정과 같은 판정(scoring.ai_run_done)을 쓴다.
        #
        # 상한은 **시장마다**다. 질문에 시장이 붙기 전에는 사이트에 한 벌이었는데, 시장마다
        # 질문을 지으면 앞 시장(대개 기본 시장, 작은 id)이 상한을 다 먹어 뒤 시장은 영영 안
        # 재진다. 같은 순서로 줄 세운 뒤 시장마다 앞에서 limit 개씩 고른다. 시장 목록 밖
        # 로케일의 질문(시장을 뺀 뒤 남은 것)은 묻지 않는다 — 그 시장은 이제 안 잰다.
        markets = db.site_locales(conn, p["id"])
        pick = ("WITH last AS (SELECT c.prompt_id, MAX(r.finished_at) at FROM ai_checks c "
                "JOIN runs r ON r.id=c.run_id WHERE " + scoring.ai_run_done("r") +
                " GROUP BY c.prompt_id) "
                "SELECT p.id, p.prompt, p.category, COALESCE(p.locale, ?) locale "
                "FROM ai_prompts p "
                "LEFT JOIN last l ON l.prompt_id=p.id "
                "WHERE p.project_id=? AND p.is_active=1 {cat}"
                "ORDER BY l.at IS NOT NULL, "
                "COALESCE(julianday('now') - julianday(l.at) <= ?, 1), p.id")
        if ids:
            ids_list = [int(x) for x in ids.split(",") if x.strip()]
            prompts = conn.execute(
                f"""SELECT id, prompt, category, COALESCE(locale, ?) locale FROM ai_prompts
                     WHERE project_id=? AND id IN ({','.join('?' * len(ids_list))}) ORDER BY id""",
                (markets[0], p["id"], *ids_list)).fetchall()
        else:
            cat_sql, cat_arg = ("AND p.category=? ", (category,)) if category else ("", ())
            seen: dict[str, int] = {}
            prompts = []
            for row in conn.execute(pick.format(cat=cat_sql),
                                    (markets[0], p["id"], *cat_arg, scoring.AI_STALE_DAYS)):
                if row["locale"] in markets and seen.get(row["locale"], 0) < limit:
                    seen[row["locale"]] = seen.get(row["locale"], 0) + 1
                    prompts.append(row)
        if not prompts:
            return st.skip(f"AI에 물어볼 질문이 아직 없습니다 ({p['name']}). 채팅에 "
                           f"`/capture add {p['name']}` 이라고 하시면 프로젝트에 맞는 질문 10~30개를 "
                           "만들어 드립니다 (대시보드 폼으로 만든 사이트는 이 단계가 비어 있습니다).")

        # 오늘(started_at이 오늘인 kind='ai' 런) 이미 기록된 (prompt_id, engine, sample_idx)는
        # 건너뛴다. '오늘'을 만드는 자리와 --force 의 뜻은 러너가 갖는다 (Stage.seen_today).
        # **끝난 회차의 것만** 친다(scoring.ai_run_done) — 측정도 끝난 회차만 쓰기 때문이다.
        # 402 로 끊긴 회차가 물은 질문을 여기서 "이미 확인"으로 건너뛰면, 충전 후 다시
        # 눌러도 그 질문들은 끝난 회차에 행이 안 생겨 화면의 "측정 안 됨"이 안 풀린다.
        checked_today = st.seen_today(
            """SELECT c.prompt_id, c.engine, c.sample_idx
                 FROM ai_checks c
                 JOIN runs r ON r.id = c.run_id
                WHERE r.project_id = ?
                  AND """ + scoring.ai_run_done("r") + """
                  AND """ + collector.today_clause("r.started_at"),
            (p["id"],), force=force)

        total_calls = len(prompts) * len(engines_d) * samples
        # 프롬프트마다 "아직 안 본" 작업 목록. 프롬프트 단위로 묶어 두는 이유는
        # 한 프롬프트가 끝날 때마다 진행 표시를 찍기 위해서다 (긴 런의 유일한 신호).
        todo = [(row, [(engine, sample) for engine in engines_d for sample in range(samples)
                       if (row["id"], engine, sample) not in checked_today])
                for row in prompts]
        calls_to_make = sum(len(tasks) for _, tasks in todo)
        skipped_calls = total_calls - calls_to_make

        print(f"[ai] prompts={len(prompts)} engines={list(engines_d)} samples={samples} "
              f"-> {calls_to_make} calls{st.skip_note(skipped_calls)}")
        print("     note: 엔진 자체 웹 검색 요금이 토큰 값에 더해질 수 있습니다(실청구는 응답의 cost).")
        if st.dry_run:
            for row in prompts[:5]:
                print(f"     e.g. [{row['category']}] {row['prompt']}")
            return st.noop(rows=0)

        if calls_to_make == 0:
            print(f"\nsaved 0 checks{st.skip_note(skipped_calls)}. failures=0/0. "
                  f"Next: /capture gaps or /capture report")
            return st.noop(rows=0)

        if not serp_adapter.has_dataforseo():
            return st.skip("DATAFORSEO_LOGIN / DATAFORSEO_PASSWORD not set. See references/setup.md")

        aliases = scoring.aliases_of(cfg)   # 별칭 규칙 정본은 scoring — 갈라지면 비교 불능
        own_domain = p["domain"]
        # r.api_calls를 직접 센다 — 도중에 죽어도 그때까지 부른 횟수가 남는다.
        with st.record("ai") as r:
            run_id = r.id
            # 질문×엔진×표본을 한 줄로 펴서 동시에 묻는다(fanout.LIMITS["dfs_llm"]).
            # 순차일 때 1회 ~8초 × 222회가 이 단계의 25분이었다. 묻기(ask)만 일꾼이
            # 하고, 판정·적재는 이 스레드가 입력 순서대로 한다 — conn 을 일꾼과 나눠
            # 쓰지 않는다. 429 는 여전히 **그 항목 하나의 실패**다: 세고 넘어가고,
            # 끝난 회차에 행이 없으니 다음 런이 그 항목만 다시 묻는다(checked_today).
            flat = [(row, engine, sample) for row, tasks in todo for engine, sample in tasks]
            last_of = {row["id"]: (row, *tasks[-1]) for row, tasks in todo if tasks}

            def ask_one(item) -> dict:
                row, engine, _ = item
                # 그 질문의 시장으로 묻는다 — 예전엔 늘 사이트 기본(p.locale)이라 영어 질문도
                # "한국 사용자에게 맞는 출처"를 달라고 했다.
                return ask(engines_d[engine], row["prompt"], row["locale"])

            def write_one(item, res) -> None:
                """질문 하나 × 엔진 × 샘플 — 실패는 러너가 세고 다음으로 넘어간다."""
                row, engine, sample = item
                mentioned, cited, others, recommended = scoring.judge(
                    res["content"], res["citation_urls"], aliases, own_domain)
                db.record_ai_check(conn, row["id"], run_id, engine, sample,
                                   mentioned, cited, others, res["content"],
                                   recommended=recommended,
                                   citations=scoring.citations_of(
                                       res.get("citations"), res["content"]))
                # 도중에 Fatal 로 끊겨도 그때까지 부른 횟수·청구액이 runs 에 남게 여기서 센다
                r.api_calls += 1
                r.cost += float((res.get("usage") or {}).get("cost") or 0)
                if last_of.get(row["id"]) == item:
                    print(f"  prompt#{row['id']} [{row['category']}] done")

            fanout.each(st, flat, ask_one, write_one, workers=fanout.LIMITS["dfs_llm"],
                        label=lambda t: f"{t[1]} failed on prompt#{t[0]['id']}")
            # 모델까지 적는다 — 표의 모델을 바꾼 회차부터 인용률이 달라질 수 있고,
            # ai_checks 에는 엔진 id 만 남는다. 추세가 꺾인 자리를 여기서 찾는다.
            r.notes = (f"engines={list(engines_d)} samples={samples} "
                       f"models={','.join(e['model'] for e in engines_d.values())} "
                       f"{st.err_note} skipped={skipped_calls}")
            # 선택 하위 걸음 — 내 샘플 질문 밖에서 누가 인용되나(DataForSEO AI Optimization).
            # 켠 사이트만, 키가 있을 때만. 여기서 난 오류는 이 단계의 성패를 안 바꾼다.
            if s.get("ai_llm_mentions"):
                note, c = _llm_mentions(conn, p)
                r.cost += c
                r.notes += f" {note}"
            cost = r.cost

        # 실패는 카운트만 하고 계속 갔으니, 끝에서 한 번 크게 말한다 —
        # 아래 매트릭스의 분모가 그만큼 줄어든 것을 보이게.
        if st.errors:
            print(f"[ai] {st.errors}/{calls_to_make} 요청 실패 — 매트릭스 분모가 그만큼 축소됨",
                  file=sys.stderr)

        # summary matrix: engine x category -> cited/total
        print("\nvisibility matrix (cited / checks):")
        for m in conn.execute(
            """SELECT c.engine, p2.category,
                      SUM(c.cited) AS cited, COUNT(*) AS total
                 FROM ai_checks c JOIN ai_prompts p2 ON p2.id=c.prompt_id
                WHERE c.run_id=? GROUP BY 1,2 ORDER BY 1,2""", (run_id,)):
            print(f"  {m['engine']:<11} {m['category']:<10} {m['cited']}/{m['total']}")
        print(f"\nrun_id={run_id} saved{st.skip_note(skipped_calls)}. "
              f"failures={st.errors}/{calls_to_make}. "
              f"Next: /capture gaps or /capture report")
        return st.verdict(calls_to_make - st.errors, rows=calls_to_make - st.errors,
                          cost=round(cost, 4))


def _llm_mentions(conn, p) -> tuple[str, float]:
    """LLM 언급 집계 한 번 — (runs.notes 에 붙일 조각, 실청구액). 주제는 추적 검색어 중
    씨앗 먼저·검색량 큰 순 10개다(serp_extras.llm_mentions 가 부르고 적는다)."""
    import serp_adapter
    import serp_extras
    kws = [r["keyword"] for r in conn.execute(
        """SELECT keyword FROM keywords WHERE project_id=? AND is_active=1
            ORDER BY (source='seed') DESC, COALESCE(volume, 0) DESC, id LIMIT 10""", (p["id"],))]
    try:
        _data, cost, note = serp_extras.llm_mentions(conn, p, kws, db.project_locale(p))
        return note, cost
    except serp_adapter.Fatal as e:
        return f"llm_mentions=stopped({str(e)[:80]})", 0.0
    except Exception as e:
        print(f"[ai] LLM 언급 집계 실패: {e}", file=sys.stderr)
        return f"llm_mentions=failed({type(e).__name__})", 0.0


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser()
    collector.add_common(ap)
    collector.add_setting(ap, "--throttle", key="throttle", fallback=0.5, type=float,
                          help="요청 간격(초). 기본은 skill_config defaults.throttle")
    # 0 = 끔(기본). 한 번에 $0.1 안팎이라 사람이 켠다 — 30일에 한 번만 받는다.
    collector.add_setting(ap, "--llm-mentions", key="ai_llm_mentions", fallback=0, type=int,
                          help="1 이면 DataForSEO AI Optimization 으로 LLM 언급 집계를 받는다(유료, 30일에 한 번)")
    ap.add_argument("--engines", default=None, help="comma list; default from config")
    # 기본 2회 샘플(skill_config ai_samples와 일치) — 비결정성 완화, 대신 호출 비용 2배.
    collector.add_setting(ap, "--samples", key="ai_samples", fallback=2, type=int)
    collector.add_setting(ap, "--max-prompts", key="limits.max_ai_prompts", fallback=30, type=int)
    ap.add_argument("--ids", help="쉼표로 구분한 ai_prompt id — 지정하면 is_active를 무시하고 이것만 실행")
    ap.add_argument("--category", help="이 카테고리의 활성 프롬프트만 실행")
    ap.add_argument("--force", action="store_true", help="오늘 이미 확인한 질문도 건너뛰지 않고 재확인")
    return ap


def main() -> None:
    if len(sys.argv) == 1:
        _selfcheck()
        return
    collector.cli("ai")


def _selfcheck() -> None:
    """동시에 묻되, 적재는 한 스레드·입력 순서 — 가짜 ask 로 돈다(유료 호출 0).

    ask 를 통째로 갈아 끼운다: 앞 항목일수록 늦게 끝나게 해서, 끝난 순서대로 적으면
    ai_checks 의 id 순서가 뒤집히게 만든다. conn 은 fanout.MainThreadOnly 로 싸서
    일꾼이 만지면 바로 터지게 한다(db.connect 는 check_same_thread=False 라 sqlite3 가
    안 막는다).
    """
    import contextlib
    import io
    import os
    import tempfile
    import threading
    import time

    os.environ["CAPTURE_HOME"] = str(Path(tempfile.mkdtemp(prefix="seo-miner-ai-selftest-")))
    saved_dfs = {k: os.environ.get(k) for k in ("DATAFORSEO_LOGIN", "DATAFORSEO_PASSWORD")}
    os.environ["DATAFORSEO_LOGIN"], os.environ["DATAFORSEO_PASSWORD"] = "fake", "fake"
    boot = db.connect()
    boot.execute("INSERT INTO projects(name, domain, locale) VALUES('ai','ai.kr','ko-KR')")
    pid = boot.execute("SELECT id FROM projects WHERE name='ai'").fetchone()["id"]
    boot.executemany("INSERT INTO ai_prompts(project_id, prompt, category, is_active) "
                     "VALUES(?,?,'추천',1)", [(pid, f"질문{i}") for i in range(6)])
    boot.commit()
    boot.close()

    lock = threading.Lock()
    live = {"now": 0, "peak": 0}
    main_id = threading.get_ident()
    asked_on: set[int] = set()
    orig_ask = globals()["ask"]

    def fake_ask(e, prompt, locale):
        with lock:
            live["now"] += 1
            live["peak"] = max(live["peak"], live["now"])
            asked_on.add(threading.get_ident())
        n = int(prompt.removeprefix("질문"))
        time.sleep(0.01 * (6 - n))
        with lock:
            live["now"] -= 1
        if prompt == "질문2" and e["model"] == engines()["gemini"]["model"]:
            # 429 는 그 항목 하나의 실패다 — 단계를 멈추지 않는다(serp_adapter.raise_for)
            raise requests.HTTPError("429 Client Error: Too Many Requests")
        return {"content": f"{prompt} 답 — ai.kr 추천", "citation_urls": ["https://ai.kr/x"],
                "usage": {"cost": 0.001}}

    conn = fanout.MainThreadOnly(db.connect())
    globals()["ask"] = fake_ask
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            res = collect("ai", engines="chatgpt,perplexity,gemini", samples=2, throttle=0,
                          conn=conn)
    finally:
        globals()["ask"] = orig_ask
    # 6질문 × 3엔진 × 2표본 = 36, 그중 질문2×gemini 두 표본이 429
    assert (res.ok, res.partial, len(res.errors)) == (True, True, 2), res
    assert all("prompt#" in e.item and e.item.startswith("gemini") for e in res.errors), res.errors
    rows = conn.execute("SELECT p.prompt, c.engine, c.sample_idx FROM ai_checks c "
                        "JOIN ai_prompts p ON p.id=c.prompt_id ORDER BY c.id").fetchall()
    want = [(f"질문{i}", e, s) for i in range(6) for e in ("chatgpt", "perplexity", "gemini")
            for s in range(2) if not (i == 2 and e == "gemini")]
    assert [tuple(r) for r in rows] == want, "적재 순서가 입력 순서가 아니다(끝난 순서로 적혔다)"
    assert 1 < live["peak"] <= fanout.LIMITS["dfs_llm"], f"동시 상한: peak={live['peak']}"
    assert main_id not in asked_on, "ask 가 메인 스레드에서 돌았다 — 동시에 안 묻는다"
    run = conn.execute("SELECT api_calls, notes, cost_estimate_usd FROM runs WHERE kind='ai'").fetchone()
    assert run["api_calls"] == 34 and "errors=2" in run["notes"], dict(run)
    assert abs(run["cost_estimate_usd"] - 34 * 0.001) < 1e-9, "응답의 실청구액을 안 적었다"
    assert engines()["gemini"]["model"] in run["notes"], "물은 모델을 노트에 안 적었다"

    # 402(Fatal) — 아직 안 보낸 질문은 보내지 않고 단계를 끝낸다
    sent: list[str] = []

    def broke(e, prompt, locale):
        with lock:
            sent.append(prompt)
        raise collector.Fatal("DataForSEO 잔액 없음(402)")

    globals()["ask"] = broke
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            collect("ai", engines="chatgpt,perplexity,gemini", samples=2, throttle=0,
                    force=True, conn=conn)
        raise AssertionError("402 를 삼켰다")
    except collector.Fatal:
        pass
    finally:
        globals()["ask"] = orig_ask
    assert len(sent) <= fanout.LIMITS["dfs_llm"], f"402 뒤로도 계속 물었다: {len(sent)}회"
    conn.close()
    for k, v in saved_dfs.items():
        os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
    _llm_shape_check()
    _canary_check()
    print("collect_ai self-check ok")


def _llm_shape_check() -> None:
    """DataForSEO 요청·응답 꼴 — 2026-11-24 실호출 응답을 줄인 고정 값(네트워크 0)."""
    e = {"platform": "chat_gpt", "model": "gpt-x"}
    b = llm_body(e, "강남 비립종 잘하는 곳?", "ko-KR")
    assert b["web_search"] is True and b["web_search_country_iso_code"] == "KR", b
    assert len(b["system_message"]) <= PROMPT_MAX and b["model_name"] == "gpt-x", b
    assert "max_output_tokens" not in b, "생각하는 모델은 1024 미만이면 거절한다 — 보내지 않는다"
    pb = llm_body({"platform": "perplexity", "model": "sonar"}, "q", "en-US")
    assert "web_search" not in pb and pb["web_search_country_iso_code"] == "US", pb
    gb = llm_body({"platform": "gemini", "model": "g"}, "q", "ko-KR")
    assert "web_search_country_iso_code" not in gb, "gemini 에는 나라 칸이 없다"
    cb = llm_body({"platform": "claude", "model": "c"}, "q", "vi-VN")
    assert "web_search_country_iso_code" not in cb, "claude 는 정해진 나라만 받는다"
    try:
        llm_body(e, "가" * (PROMPT_MAX + 1), "ko-KR")
        raise AssertionError("500자를 넘는 질문을 보냈다 — DataForSEO 가 40501 로 거절한다")
    except ValueError:
        pass
    result = [{"model_name": "gpt-x-2026", "items": [
        {"type": "reasoning", "sections": [{"type": "summary_text", "text": "생각 — 답이 아니다"}]},
        {"type": "message", "sections": [{"type": "text", "text": "1. 우리피부과 — 추천", "annotations": [
            {"title": "우리피부과", "url": "https://ai.kr/x?utm_source=openai"},
            {"title": "", "url": "https://ai.kr/x?utm_source=openai"},
            {"title": "남", "url": "https://vertex.example/r", "direct_url": "https://other.kr/a"}]}]}]}]
    got = parse_llm(result)
    assert got["content"] == "1. 우리피부과 — 추천", "생각(reasoning) 글을 답에 섞었다"
    assert got["citation_urls"] == ["https://ai.kr/x?utm_source=openai"] * 2 + ["https://other.kr/a"]
    assert [c["url"] for c in got["citations"]] == ["https://ai.kr/x?utm_source=openai",
                                                    "https://other.kr/a"], "같은 URL 은 한 번"
    assert got["model"] == "gpt-x-2026"
    assert parse_llm([]) == {"content": "", "citation_urls": [], "citations": [], "model": None}


def _canary_check() -> None:
    """잔액 카나리아(P4) — 가짜 requests.get(네트워크 0). 남은 돈 = min(키 한도 잔여,
    계정 total_credits - total_usage). 추정 비용보다 적으면 막고, 몇 분은 다시 안 묻는다."""
    import os
    real_get = requests.get
    seen: list[str] = []

    class R:
        def __init__(self, status, body):
            self.status_code, self._b = status, body

        def json(self):
            return self._b

        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError(self.status_code)

    state = {"credits": (10.0, 9.86), "key": {"limit": None, "usage": 3.0},
             "credits_status": 200}

    def fake_get(url, timeout=None, headers=None):
        seen.append(url.rsplit("/", 1)[-1])
        if url.endswith("/auth/key"):
            return R(200, {"data": state["key"]})
        tot, use = state["credits"]
        return R(state["credits_status"], {"data": {"total_credits": tot, "total_usage": use}})

    os.environ["OPENROUTER_API_KEY"] = "canary-selftest"
    requests.get = fake_get
    serp_adapter._canary.clear()
    try:
        ok, msg = openrouter_ok(1.0)                # 호스팅 키가 $0.14 로 떨어졌던 그때
        assert not ok and "$0.14" in msg and "$1.00" in msg, msg
        assert seen == ["key", "credits"], seen
        ok, _ = openrouter_ok(0.1)                  # 같은 값을 기억해서 판정만 다시
        assert ok and seen == ["key", "credits"], f"몇 분 안에 잔액을 또 물었다: {seen}"
        serp_adapter._canary.clear()
        state["credits"] = (10.0, 1.0)
        state["key"] = {"limit": 5.0, "usage": 4.5, "limit_remaining": 0.5}
        ok, msg = openrouter_ok(1.0)                # 계정엔 $9 있어도 키 한도 잔여가 $0.50
        assert not ok and "$0.50" in msg, msg
        serp_adapter._canary.clear()
        state["credits_status"] = 403               # credits 를 못 읽으면 키 판정만으로
        state["key"] = {"limit": None, "usage": 1.0}
        ok, msg = openrouter_ok(5.0)
        assert ok and "잔액" not in msg, msg
        serp_adapter._canary.clear()

        def down(url, timeout=None, headers=None):
            raise OSError("network down")
        requests.get = down
        ok, msg = openrouter_ok(5.0)
        assert ok and "그대로 진행" in msg, "확인이 실패했는데 수집을 막았다"
        per = sum(e["usd"] for e in engines().values())
        assert estimate_usd(None) == round(30 * 2 * per, 2), estimate_usd(None)
        assert "claude" not in engines() and set(engines(["claude", "없는엔진"])) == {"claude"}
    finally:
        requests.get = real_get
        serp_adapter._canary.clear()
        os.environ.pop("OPENROUTER_API_KEY", None)


if __name__ == "__main__":
    main()
