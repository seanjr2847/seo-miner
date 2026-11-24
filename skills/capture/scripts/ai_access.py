#!/usr/bin/env python3
"""AI 크롤러가 이 사이트에 들어올 수 있나 — 판정 한 벌.

네 가지를 여기서만 한다. 전부 결정적이다(같은 입력이면 같은 답, 점수·가중치 없음).

  1. robots.txt 묶음 판정 — RFC 9309 방식. `blocking_rule()`·`group_for()`·`named()`.
     scoring.robots_blocks·_named_in_robots 가 이것을 부른다(판정은 한 벌).
  2. `User-agent: GPTBot/1.1` 같은 비표준 UA 줄 감지 — `ua_notes()`.
  3. Content-Signal 줄 — 원문 그대로 낸다. 판정하지 않는다. `content_signals()`.
  4. llms.txt 형식 — `llms_format()`.
  5. AI 봇 UA 로 홈을 한 번 요청했을 때 서버(WAF·CAPTCHA)가 어떻게 답하나 —
     `probe_bots()`·`classify()`. robots.txt 판정과 **다른 칸**에 적는다.

예전 판정은 urllib.robotparser 였다. 그 파서는 (a) UA 줄 값이 봇 이름 **안에 들어
있으면** 그 묶음을 골랐고(`User-agent: bot` 이 GPTBot 을 잡는다), (b) 파일에서 먼저 나온
묶음을 골랐고(`*` 가 위에 있으면 이름 묶음을 못 본다), (c) 묶음 안에서도 먼저 나온 줄을
썼다(가장 긴 줄이 아니라). 셋 다 RFC 9309 와 다르고, 실제 크롤러의 판정과 어긋난다.

self-check:  python test_ai_access.py
"""
from __future__ import annotations

import json
import re
from urllib.parse import quote, unquote, urlsplit

# ── 1. robots.txt — RFC 9309 ────────────────────────────────────────────────
# https://www.rfc-editor.org/rfc/rfc9309.html
#   §2.2.1  product token 은 글자·`_`·`-` 만. 대소문자 무시 일치. 같은 토큰의 묶음이
#           여럿이면 규칙을 합친다. 맞는 묶음이 없으면 `*` 묶음, 그것도 없으면 제한 없음.
#   §2.2.2  가장 긴(옥텟 수) 줄이 이긴다. 길이가 같으면 Allow.
#   §2.2.3  `*` 는 아무 글자 0개 이상, `$` 는 주소의 끝.
#   §2.1    문법: 빈 줄은 무시한다 — 묶음을 끊지 않는다.
_TOKEN = re.compile(r"[A-Za-z_-]+")
# 인코딩된 쪽과 안 된 쪽을 같은 꼴로 — 한글 경로가 한쪽은 %EA.. 다른 쪽은 글자로 온다.
_SAFE = "/?=&*$:@!+,;~-._%#[]()'"


def is_token(ua: str) -> bool:
    """robots.txt 의 product token 으로 쓸 수 있는 이름인가(글자·`_`·`-` 만)."""
    return bool(ua) and _TOKEN.fullmatch(ua or "") is not None


def _norm(s: str) -> str:
    return quote(unquote(s), safe=_SAFE)


def _records(txt: str):
    """줄 → (키 소문자, 값, 주석 뗀 원문). 키:값 꼴이 아닌 줄은 버린다."""
    for raw in (txt or "").replace("﻿", "").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        key, _, val = line.partition(":")
        yield key.strip().lower(), val.strip(), line


def parse(txt: str) -> list[dict]:
    """robots.txt → 묶음 목록. 묶음 = {"agents": [...], "rules": [...], "other": [...]}.

    agents 한 줄: {"token": 소문자 토큰|"*"|None, "value": 적힌 값, "line": 원문,
                   "standard": 값 전체가 토큰인가}
    rules 한 줄:  {"allow": bool, "pattern": 값, "line": 원문}
    other 한 줄:  {"key", "value", "line"} — Content-Signal·Crawl-delay 처럼 묶음 안의 다른 줄.

    User-agent 줄이 이어지면 한 묶음이다. Allow/Disallow 가 한 번 나온 뒤의 User-agent 줄이
    새 묶음을 연다 — 빈 줄·Sitemap·모르는 줄은 묶음을 끊지 않는다(구글 공개 파서와 같다:
    https://github.com/google/robotstxt).
    """
    groups: list[dict] = []
    cur = None
    open_agents = False          # 지금 User-agent 줄을 이어 받는 중인가
    for key, val, line in _records(txt):
        if key == "user-agent":
            if not open_agents or cur is None:
                cur = {"agents": [], "rules": [], "other": []}
                groups.append(cur)
                open_agents = True
            if val == "*":
                tok, std = "*", True
            else:
                m = _TOKEN.match(val)
                tok = m.group(0).lower() if m else None
                std = bool(m) and m.group(0) == val
            cur["agents"].append({"token": tok, "value": val, "line": line, "standard": std})
        elif key in ("allow", "disallow"):
            open_agents = False
            if cur is not None:
                cur["rules"].append({"allow": key == "allow", "pattern": val, "line": line})
        elif cur is not None and key != "sitemap":
            cur["other"].append({"key": key, "value": val, "line": line})
    return groups


def group_for(txt: str, ua: str) -> dict:
    """이 봇에게 적용되는 묶음. {"label", "wildcard", "rules", "agents"}.

    label: 이름 묶음이면 봇 토큰(적힌 그대로), `*` 묶음이면 "*", 아무 묶음도 없으면 None.
    이름 묶음이 하나라도 있으면 `*` 묶음은 이 봇에게 적용되지 않는다(가장 구체적인 묶음).
    """
    want = (ua or "").split("/")[0].strip().lower()
    groups = parse(txt)
    mine = [g for g in groups if any(a["token"] == want for a in g["agents"])] if want else []
    if mine:
        agents = [a for g in mine for a in g["agents"] if a["token"] == want]
        return {"label": _TOKEN.match(agents[0]["value"]).group(0), "wildcard": False,
                "rules": [r for g in mine for r in g["rules"]], "agents": agents}
    wild = [g for g in groups if any(a["token"] == "*" for a in g["agents"])]
    if wild:
        return {"label": "*", "wildcard": True,
                "rules": [r for g in wild for r in g["rules"]], "agents": []}
    return {"label": None, "wildcard": False, "rules": [], "agents": []}


def named(txt: str, ua: str) -> bool:
    """이 봇 이름으로 된 User-agent 묶음이 있나(토큰 전체 일치)."""
    g = group_for(txt, ua)
    return g["label"] is not None and not g["wildcard"]


def _rule_re(pattern: str) -> re.Pattern:
    end = pattern.endswith("$")
    body = pattern[:-1] if end else pattern
    return re.compile("".join(".*" if c == "*" else re.escape(c) for c in _norm(body))
                      + ("$" if end else ""))


def blocking_rule(txt: str, url: str, ua: str) -> str | None:
    """이 주소를 이 봇에게 막는 줄(`Disallow: …`). 안 막으면 None.

    원문이 비면 None 이다 — 부르는 쪽이 "안 봤다"와 "안 막는다"를 갈라야 한다.
    """
    if not (txt or "").strip():
        return None
    sp = urlsplit(url)
    path = _norm((sp.path or "/") + (f"?{sp.query}" if sp.query else ""))
    best = None                                   # (길이, allow, 줄)
    for r in group_for(txt, ua)["rules"]:
        pat = r["pattern"]
        if not pat or not _rule_re(pat).match(path):
            continue
        key = (len(_norm(pat).encode()), r["allow"])
        if best is None or key > best[0]:
            best = (key, r)
    if best is None or best[1]["allow"]:
        return None
    return f"Disallow: {best[1]['pattern']}"


# ── 2. 비표준 UA 줄 ─────────────────────────────────────────────────────────
def ua_notes(txt: str, ua: str) -> list[dict]:
    """이 봇을 겨냥했지만 토큰만 적지 않은 User-agent 줄. [{"line", "applied"}].

    `User-agent: GPTBot/1.1` — 토큰에는 버전을 붙일 수 없다(RFC 9309 §2.2.1). 구글 공개
    파서는 앞머리의 토큰 글자만 읽으므로 이 줄을 GPTBot 묶음으로 읽고, 우리 판정도 그렇게
    한다(applied=True). 하지만 값 전체를 견주는 파서는 이 줄을 못 알아본다 — 그래서 줄을
    따로 알린다. UA 문자열을 통째로 적은 줄(`Mozilla/5.0 (… GPTBot/1.1 …)`)은 앞머리
    토큰이 Mozilla 라 그 봇에게 적용되지 않는다(applied=False).
    """
    want = (ua or "").strip().lower()
    if not want:
        return []
    word = re.compile(r"(?<![A-Za-z_-])" + re.escape(want) + r"(?![A-Za-z_-])", re.I)
    out, seen = [], set()
    for g in parse(txt):
        for a in g["agents"]:
            if a["standard"] or not word.search(a["value"]) or a["line"] in seen:
                continue
            seen.add(a["line"])
            out.append({"line": a["line"], "applied": a["token"] == want})
    return out


# ── 3. Content-Signal ───────────────────────────────────────────────────────
# robots.txt 안의 `Content-Signal: search=yes, ai-input=no, ai-train=no` 줄
# (https://contentsignals.org/ · https://blog.cloudflare.com/content-signals-policy/).
# 신호는 셋(search·ai-input·ai-train), 값은 yes/no, 안 적은 신호는 아무 뜻도 없다. 크롤
# 허용·차단이 아니라 "가져간 뒤 무엇에 써도 되나"에 대한 사이트의 의사 표시이고, 지키는지는
# 받는 쪽에 달려 있다(IETF 초안 draft-romm-aipref-contentsignals 은 낱말만 정했고 이 줄의
# 문법은 Cloudflare 정책에만 있다). 그래서 판정하지 않는다 — 원문만 낸다.
def content_signals(txt: str) -> list[dict]:
    """[{"line": 원문 한 줄, "agents": 그 줄이 든 묶음의 User-agent 값들}] — 파일 순서대로."""
    out = []
    cur: list[str] = []
    open_agents = False
    for key, val, line in _records(txt):
        if key == "user-agent":
            if not open_agents:
                cur, open_agents = [], True
            cur.append(val)
        elif key in ("allow", "disallow"):
            open_agents = False
        elif key == "content-signal":
            out.append({"line": line, "agents": list(cur)})
    return out


# ── 4. llms.txt 형식 ────────────────────────────────────────────────────────
# 기준은 Lighthouse 의 llms-txt 검사(agentic-browsing 묶음, 13.2.0~)가 코드로 보는 세 가지다:
# https://github.com/GoogleChrome/lighthouse/blob/main/core/audits/agentic/llms-txt.js
#     const hasH1 = /^\s*#\s+.+/m.test(content);
#     const hasLink = /\[.+\]\(.+\)/.test(content);
#     const isTooShort = content.length < 50;
# 정규식·길이 기준을 그대로 옮겼다(2026-11-24, 13.6.0 기준 확인). Lighthouse 는 404 를
# '해당 없음'으로 두고 Content-Type 은 보지 않는다 — 없는 파일을 결함으로 부르지 않는 것은
# 우리와 같다(probe_llms_txt 의 세 상태). 이 판정도 "형식이 깨졌다"까지만 말한다: 구글
# 검색은 이 파일을 쓰지 않는다.
# 코드 → 사람이 읽는 말. 화면과 요청문이 이 한 벌을 쓴다.
LLMS_PROBLEMS = {
    "no_h1": "H1 제목(`# 이름`) 없음",
    "no_link": "마크다운 링크(`[글](주소)`) 없음",
    "too_short": "내용이 50자 미만",
}
LLMS_MIN_CHARS = 50
_MD_H1 = re.compile(r"^\s*#\s+.+", re.M)
_MD_LINK = re.compile(r"\[.+\]\(.+\)")


def llms_format(text: str) -> list[str]:
    """llms.txt 본문 → 걸린 문제 코드(LLMS_PROBLEMS 의 키, 그 순서). 없으면 [].

    "있다"와 "읽을 수 있는 꼴이다"는 다르다 — 빈 파일·링크 없는 한 줄짜리도 200 으로 온다.
    """
    t = text or ""
    bad = []
    if not _MD_H1.search(t):
        bad.append("no_h1")
    if not _MD_LINK.search(t):
        bad.append("no_link")
    if len(t) < LLMS_MIN_CHARS:
        bad.append("too_short")
    return bad


def llms_format_text(codes) -> str:
    return " · ".join(LLMS_PROBLEMS[c] for c in codes or [] if c in LLMS_PROBLEMS)


def llms_format_pack(codes) -> str | None:
    """DB 칸(crawl_runs.llms_txt_format) 꼴 — 문제 없음 "" · 안 봄 None."""
    return None if codes is None else ",".join(c for c in codes if c in LLMS_PROBLEMS)


def llms_format_unpack(s) -> list[str] | None:
    if s is None:
        return None
    return [c for c in str(s).split(",") if c in LLMS_PROBLEMS]


# ── 5. 봇 UA 로 요청했을 때 서버의 반응 ─────────────────────────────────────
# robots.txt 가 열어 둬도 방화벽(WAF)이 그 UA 를 막으면 봇은 못 들어온다 — robots.txt 만
# 봐서는 모른다. 그래서 등록된 내 사이트의 홈을, 봇 이름을 실은 UA 로 **봇당 한 번** 연다.
#
# 이 결과의 한계를 같이 말해야 한다: 요청은 우리 서버에서 나간다. 벤더의 IP 가 아니다.
# 방화벽이 IP 로 진짜 봇을 가려내면(검증된 봇만 통과) 우리 요청은 막혀도 진짜 봇은
# 들어오고, 그 반대도 있다. 그래서 "막는다"가 아니라 "이 UA 로 온 요청을 거절했다"고 적는다.
SERVER_VERDICT = {
    "served": "응답함",
    "refused": "거절함",
    "challenge": "확인 화면(CAPTCHA·자바스크립트 확인)",
    "unknown": "모름",
}
# 이 사실을 싣는 모든 자리(화면·요청문)가 붙이는 한 줄 — 한 벌.
SERVER_CAVEAT = ("서버 반응은 이 봇 이름을 실은 UA 로 우리 쪽에서 홈을 한 번 요청해 본 "
                 "결과입니다. 진짜 봇의 IP 가 아니라서, 방화벽이 IP 로 봇을 가리면 실제와 "
                 "다를 수 있습니다. 방화벽(WAF)·CDN 의 봇 설정에서 확인하세요.")
# 거절로 읽는 상태 코드 — 인증 요구·금지·수용 불가·요청 과다·법적 차단. 5xx 는 서버가
# 아팠을 수 있어 차단의 증거로 안 쓴다(503 은 확인 화면 표식이 있을 때만).
REFUSE_STATUS = frozenset({401, 403, 406, 429, 451})
# 확인 화면의 표식. 헤더는 Cloudflare 가 문서로 약속한 것이다:
# https://developers.cloudflare.com/cloudflare-challenges/challenge-types/challenge-pages/detect-response/
CHALLENGE_HEADER = ("cf-mitigated", "challenge")
CHALLENGE_MARKS = ("just a moment...", "cf-challenge", "g-recaptcha", "h-captcha",
                   "captcha-delivery.com", "px-captcha")
PROBE_UA = "Mozilla/5.0 (compatible; {token}/1.0; +https://github.com/seanjr2847/seo-miner access-check)"
BASELINE_UA = "seo-miner/access-check (+https://github.com/seanjr2847/seo-miner)"
# 막히면 인용이 끊기는 용도만 묻는다(scoring.AI_BOT_CITING 과 같은 뜻 — 거기서 넘겨받는다).
PROBE_PURPOSES = ("search", "user")


def _same_place(a: str, b: str) -> bool:
    x, y = urlsplit(a or ""), urlsplit(b or "")
    host = lambda h: (h or "").lower().removeprefix("www.")   # noqa: E731
    return host(x.hostname) == host(y.hostname) and (x.path or "/").rstrip("/") == (y.path or "/").rstrip("/")


def classify(base: dict, got: dict) -> dict:
    """기준 응답(평소 UA)과 봇 UA 응답 → {"verdict", "status", "detail"}.

    기준부터 막혔거나 못 받았으면 모름이다 — 그 차이는 봇 이름 탓이 아니다.
    """
    st = got.get("status")
    out = {"verdict": "unknown", "status": st, "detail": None}
    bs = base.get("status")
    if bs is None or not 200 <= bs < 300:
        out["detail"] = "평소 요청부터 응답이 없거나 거절돼 견줄 수 없음"
        return out
    headers = {str(k).lower(): str(v).lower() for k, v in (got.get("headers") or {}).items()}
    body = (got.get("text") or "")[:20000].lower()
    if headers.get(CHALLENGE_HEADER[0]) == CHALLENGE_HEADER[1]:
        return {**out, "verdict": "challenge", "detail": "cf-mitigated: challenge"}
    mark = next((m for m in CHALLENGE_MARKS if m in body), None)
    if mark and st is not None and (st >= 400 or len(body) < 20000):
        return {**out, "verdict": "challenge", "detail": f"본문에 '{mark}'"}
    if st is None:
        return {**out, "detail": "응답 없음(연결 실패·시간 초과)"}
    if st in REFUSE_STATUS:
        return {**out, "verdict": "refused", "detail": f"HTTP {st}"}
    if not 200 <= st < 300:
        return {**out, "detail": f"HTTP {st}"}
    if not _same_place(base.get("final_url") or "", got.get("final_url") or ""):
        return {**out, "detail": f"다른 주소로 보냄: {got.get('final_url')}"}
    return {**out, "verdict": "served"}


def http_get(url: str, ua: str) -> dict:
    """진짜 요청 — http_adapter 한 곳을 거친다. 한 런 안의 원문 공유는 타지 않는다
    (같은 주소를 UA 만 바꿔 여는 것이라 공유하면 전부 첫 응답이 된다)."""
    import http_adapter
    import serp_adapter
    try:
        f = http_adapter.get_fresh(url, headers={"User-Agent": ua},
                                   timeout=serp_adapter.TIMEOUTS["page"])
    except Exception as e:
        return {"status": None, "final_url": url, "headers": {}, "text": "",
                "error": f"{type(e).__name__}: {e}"[:200]}
    return {"status": f.status, "final_url": f.final_url, "headers": f.headers,
            "text": f.text if "html" in f.content_type.lower() else ""}


def probe_bots(home: str, bots, robots_txt: str = "", *, get=None, pause=None) -> dict:
    """{"baseline": 상태 코드, "rows": {봇: {"verdict","status","detail"}}}.

    요청 수: 기준 1회 + (검색·인용용이고 robots.txt 가 막지 않은 봇)당 1회. robots.txt 가
    이미 막은 봇은 묻지 않는다 — 그 봇 이름으로 요청하면서 그 봇에게 한 말을 어기지 않는다.
    기준부터 막혔으면 거기서 멈춘다(견줄 수 없는 요청을 더 보내지 않는다).
    get(url, ua) -> {"status","final_url","headers","text"} 를 갈아끼울 수 있다(테스트).
    """
    get = get or http_get
    base = get(home, BASELINE_UA)
    out = {"baseline": base.get("status"), "rows": {}}
    if base.get("status") is None or not 200 <= base["status"] < 300:
        return out
    for b in bots or ():
        ua = b.get("ua") or ""
        if b.get("purpose") not in PROBE_PURPOSES or not is_token(ua):
            continue
        if blocking_rule(robots_txt, home, ua):
            continue
        if pause:
            pause()
        try:
            got = get(home, PROBE_UA.format(token=ua))
        except Exception as e:
            got = {"status": None, "final_url": home, "error": f"{type(e).__name__}: {e}"[:200]}
        out["rows"][ua] = classify(base, got)
    return out


def probe_pack(probe: dict | None) -> str | None:
    """DB 칸(crawl_runs.ai_ua_probe) 꼴. 안 물었으면 None."""
    return None if not probe else json.dumps(probe, ensure_ascii=False)


def attach_server(rows: list[dict], packed) -> list[dict]:
    """봇 행(scoring.ai_bot_status)에 서버 반응을 붙인다 — r["server"] = {"verdict",
    "label", "status", "detail"} 또는 None(안 물었다: 옛 회차·학습용·robots 가 막은 봇)."""
    try:
        data = json.loads(packed) if packed else {}
    except (TypeError, ValueError):
        data = {}
    got = (data or {}).get("rows") or {}
    for r in rows:
        s = got.get(r.get("bot"))
        r["server"] = ({"verdict": s.get("verdict"), "status": s.get("status"),
                        "detail": s.get("detail"),
                        "label": SERVER_VERDICT.get(s.get("verdict"), SERVER_VERDICT["unknown"])}
                       if isinstance(s, dict) else None)
    return rows
