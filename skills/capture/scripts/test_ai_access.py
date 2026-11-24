#!/usr/bin/env python3
"""ai_access — robots.txt(RFC 9309) 묶음 판정 · Content-Signal 원문 · llms.txt 형식 · 봇 UA 서버 반응.

  python test_ai_access.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("CAPTURE_HOME", tempfile.mkdtemp())

import ai_access  # noqa: E402

NL = "\n"
HOME = "https://x.kr/"


def rb(txt, ua, url=HOME):
    return ai_access.blocking_rule(txt, url, ua)


def test_token_match_is_whole_token_not_substring():
    """urllib.robotparser 는 UA 줄 값이 봇 이름 **안에 들어 있으면** 그 묶음을 골랐다 —
    `User-agent: bot` 이 GPTBot·ClaudeBot·Bingbot 을 전부 잡고, `User-agent: Claude` 가
    Claude-SearchBot 을 잡았다. RFC 9309 는 토큰 전체 일치(대소문자 무시)다."""
    txt = NL.join(("User-agent: bot", "Disallow: /", "", "User-agent: *", "Allow: /"))
    for ua in ("GPTBot", "ClaudeBot", "Bingbot", "PerplexityBot"):
        assert rb(txt, ua) is None, ua
    txt = NL.join(("User-agent: Claude", "Disallow: /"))
    assert rb(txt, "Claude-SearchBot") is None
    assert rb(txt, "claude") == "Disallow: /"
    # 대소문자는 안 가린다
    assert rb("user-agent: gptbot\ndisallow: /", "GPTBot") == "Disallow: /"
    assert not ai_access.named(NL.join(("User-agent: bot", "Disallow: /")), "GPTBot")
    assert ai_access.named("User-agent: GPTBOT\nDisallow: /", "GPTBot")


def test_most_specific_group_wins_regardless_of_file_order():
    """robotparser 는 파일에서 먼저 나온 묶음을 골랐다 — `*` 가 위에 있으면 이름 묶음을
    못 봤다. 이름 묶음이 있으면 `*` 는 그 봇에게 적용되지 않는다."""
    txt = NL.join(("User-agent: *", "Disallow: /", "",
                   "User-agent: OAI-SearchBot", "Allow: /"))
    assert rb(txt, "OAI-SearchBot") is None
    assert rb(txt, "PerplexityBot") == "Disallow: /"
    g = ai_access.group_for(txt, "OAI-SearchBot")
    assert g["label"] == "OAI-SearchBot" and not g["wildcard"], g
    assert ai_access.group_for(txt, "PerplexityBot")["wildcard"] is True
    assert ai_access.group_for("Sitemap: https://x.kr/s.xml", "GPTBot")["label"] is None


def test_same_token_groups_are_merged_and_shared_groups_apply_to_all():
    txt = NL.join(("User-agent: GPTBot", "Disallow: /a", "",
                   "User-agent: CCBot", "User-agent: GPTBot", "Disallow: /b"))
    assert rb(txt, "GPTBot", HOME + "a/1") == "Disallow: /a"
    assert rb(txt, "GPTBot", HOME + "b/1") == "Disallow: /b"
    assert rb(txt, "CCBot", HOME + "a/1") is None
    assert rb(txt, "CCBot", HOME + "b/1") == "Disallow: /b"
    # 빈 줄은 묶음을 끊지 않는다(RFC 9309 문법: 빈 줄은 무시)
    txt = NL.join(("User-agent: GPTBot", "", "User-agent: CCBot", "", "Disallow: /"))
    assert rb(txt, "GPTBot") == "Disallow: /" and rb(txt, "CCBot") == "Disallow: /"
    # Sitemap 같은 다른 줄도 UA 줄의 이어짐을 끊지 않는다
    txt = NL.join(("User-agent: GPTBot", "Sitemap: https://x.kr/s.xml", "User-agent: CCBot",
                   "Disallow: /"))
    assert rb(txt, "GPTBot") == "Disallow: /"


def test_longest_match_and_allow_wins_ties():
    """robotparser 는 먼저 나온 줄을 썼다. RFC 9309 는 가장 긴 줄, 길이가 같으면 Allow."""
    txt = NL.join(("User-agent: *", "Disallow: /", "Allow: /blog/"))
    assert rb(txt, "GPTBot", HOME + "blog/x") is None
    assert rb(txt, "GPTBot", HOME + "shop") == "Disallow: /"
    txt = NL.join(("User-agent: *", "Allow: /p", "Disallow: /p"))
    assert rb(txt, "GPTBot", HOME + "p") is None
    # Disallow 값이 비면 아무것도 안 막는다
    assert rb("User-agent: *\nDisallow:", "GPTBot") is None
    # * 와 $
    txt = NL.join(("User-agent: *", "Disallow: /*.pdf$", "Disallow: /private*/x"))
    assert rb(txt, "GPTBot", HOME + "a/b.pdf") == "Disallow: /*.pdf$"
    assert rb(txt, "GPTBot", HOME + "a/b.pdf?x=1") is None
    assert rb(txt, "GPTBot", HOME + "private-1/x/y") == "Disallow: /private*/x"
    # 쿼리까지 본다
    assert rb("User-agent: *\nDisallow: /*?s=", "GPTBot", HOME + "search?s=1") == "Disallow: /*?s="
    # 주석·공백
    assert rb("User-agent: * # all\nDisallow: /   # 전부", "GPTBot") == "Disallow: /"
    # 한글 경로 — 인코딩된 쪽과 안 된 쪽이 만난다
    assert rb("User-agent: *\nDisallow: /글/", "GPTBot",
              HOME + "%EA%B8%80/1") == "Disallow: /글/"
    assert rb("User-agent: *\nDisallow: /%EA%B8%80/", "GPTBot", HOME + "글/1") \
        == "Disallow: /%EA%B8%80/"
    assert rb("", "GPTBot") is None and rb("   ", "GPTBot") is None


def test_nonstandard_ua_line_is_detected():
    """`User-agent: GPTBot/1.1` — 토큰에는 글자·`_`·`-` 만 올 수 있다. 버전을 붙인 줄은
    읽는 쪽마다 다르게 읽는다. 앞머리 토큰으로 읽되(구글 파서 방식) 그 줄을 따로 알린다."""
    txt = NL.join(("User-agent: GPTBot/1.1", "Disallow: /"))
    assert rb(txt, "GPTBot") == "Disallow: /"
    notes = ai_access.ua_notes(txt, "GPTBot")
    assert notes == [{"line": "User-agent: GPTBot/1.1", "applied": True}], notes
    # UA 문자열을 통째로 적은 줄 — 앞머리 토큰이 Mozilla 라 그 봇에게 적용되지 않는다
    txt = NL.join(("User-agent: Mozilla/5.0 (compatible; GPTBot/1.1; +https://openai.com/gptbot)",
                   "Disallow: /"))
    assert rb(txt, "GPTBot") is None
    notes = ai_access.ua_notes(txt, "GPTBot")
    assert len(notes) == 1 and notes[0]["applied"] is False, notes
    # 멀쩡한 줄에는 말이 없다. 다른 봇의 줄도 안 섞인다
    ok = NL.join(("User-agent: GPTBot", "Disallow: /", "User-agent: CCBot/2.0", "Disallow: /"))
    assert ai_access.ua_notes(ok, "GPTBot") == []
    assert ai_access.ua_notes(ok, "ClaudeBot") == []
    # 이름이 다른 봇 이름 안에 든 것만으로는 안 잡는다(Claude-User ≠ Claude-User-Agent)
    assert ai_access.ua_notes("User-agent: XGPTBot/1\nDisallow: /", "GPTBot") == []


def test_content_signal_lines_are_quoted_verbatim():
    """판정하지 않는다 — 있는 줄을 그대로, 어느 묶음에 있는지만 붙여서 낸다."""
    txt = NL.join(("# 정책", "User-agent: *", "Content-Signal: search=yes, ai-train=no",
                   "Allow: /", "", "User-agent: GPTBot", "content-signal: ai-input=no  # x",
                   "Disallow: /"))
    got = ai_access.content_signals(txt)
    assert got == [{"line": "Content-Signal: search=yes, ai-train=no", "agents": ["*"]},
                   {"line": "content-signal: ai-input=no", "agents": ["GPTBot"]}], got
    assert ai_access.content_signals("User-agent: *\nAllow: /") == []
    assert ai_access.content_signals("") == []
    # 묶음 밖(맨 위)에 있어도 줄은 낸다
    assert ai_access.content_signals("Content-Signal: ai-train=no")[0]["agents"] == []


def test_llms_format_rules():
    good = "# 사이트 이름\n\n> 한 줄 소개\n\n## 문서\n\n- [시작하기](https://x.kr/start): 처음 보는 사람용 안내\n"
    assert ai_access.llms_format(good) == [], ai_access.llms_format(good)
    assert ai_access.llms_format("# 이름\n\n" + "가" * 80) == ["no_link"]
    assert ai_access.llms_format("소개 글입니다. [문서](https://x.kr/d) " + "가" * 60) == ["no_h1"]
    assert ai_access.llms_format("# x\n[a](/b)") == ["too_short"]
    assert set(ai_access.llms_format("짧다")) == {"no_h1", "no_link", "too_short"}
    # '## 소제목' 은 H1 이 아니다. 코드 표기 안의 # 도 아니다
    assert "no_h1" in ai_access.llms_format("## 소제목\n[문서](https://x.kr/d) " + "가" * 60)
    # 모든 코드에 사람이 읽는 말이 있다(화면·요청문이 이 한 벌을 쓴다)
    assert set(ai_access.LLMS_PROBLEMS) == {"no_h1", "no_link", "too_short"}
    assert ai_access.llms_format_text(["no_h1", "too_short"]) == \
        ai_access.LLMS_PROBLEMS["no_h1"] + " · " + ai_access.LLMS_PROBLEMS["too_short"]
    # 저장 꼴: 문제 없음 = "" · 모름 = None
    assert ai_access.llms_format_pack([]) == "" and ai_access.llms_format_pack(None) is None
    assert ai_access.llms_format_unpack("no_h1,no_link") == ["no_h1", "no_link"]
    assert ai_access.llms_format_unpack("") == [] and ai_access.llms_format_unpack(None) is None
    # 모르는 코드는 버린다(옛 값·오염)
    assert ai_access.llms_format_unpack("no_h1,zzz") == ["no_h1"]


def test_classify_separates_refusal_challenge_and_unknown():
    c = ai_access.classify
    base = {"status": 200, "final_url": HOME}
    assert c(base, {"status": 200, "final_url": HOME})["verdict"] == "served"
    r = c(base, {"status": 403, "final_url": HOME})
    assert r["verdict"] == "refused" and r["status"] == 403, r
    r = c(base, {"status": 403, "final_url": HOME, "headers": {"CF-Mitigated": "challenge"}})
    assert r["verdict"] == "challenge", r
    r = c(base, {"status": 200, "final_url": HOME,
                 "text": "<title>Just a moment...</title><div id='cf-challenge'></div>"})
    assert r["verdict"] == "challenge", r
    # 기준(우리 평소 UA)부터 막혔으면 봇 탓이 아니다 — 모름
    for bad in ({"status": 403, "final_url": HOME}, {"status": None, "final_url": HOME}):
        assert c(bad, {"status": 403, "final_url": HOME})["verdict"] == "unknown"
    # 연결 실패·서버 오류는 차단의 증거가 아니다
    assert c(base, {"status": None, "final_url": HOME})["verdict"] == "unknown"
    assert c(base, {"status": 500, "final_url": HOME})["verdict"] == "unknown"
    # 다른 주소로 보냈다 — 막았다고도 열었다고도 안 한다
    r = c(base, {"status": 200, "final_url": "https://x.kr/blocked"})
    assert r["verdict"] == "unknown" and "blocked" in (r.get("detail") or ""), r
    assert set(ai_access.SERVER_VERDICT) == {"served", "refused", "challenge", "unknown"}


def test_probe_asks_once_per_open_citing_bot_and_skips_robots_blocked():
    """봇당 1회 + 기준 1회. robots.txt 가 이미 막은 봇은 묻지 않는다(그 봇은 안 올 자리다).
    학습용 봇도 묻지 않는다 — 서버가 막아도 인용과 무관하다."""
    bots = [{"ua": "OAI-SearchBot", "purpose": "search"}, {"ua": "PerplexityBot", "purpose": "search"},
            {"ua": "Claude-User", "purpose": "user"}, {"ua": "GPTBot", "purpose": "training"},
            {"ua": "Mystery", "purpose": None}]
    robots = NL.join(("User-agent: PerplexityBot", "Disallow: /"))
    log = []

    def get(url, ua):
        log.append((url, ua))
        if "OAI-SearchBot" in ua:
            return {"status": 403, "final_url": url, "headers": {}, "text": ""}
        return {"status": 200, "final_url": url, "headers": {}, "text": "<html>ok</html>"}

    out = ai_access.probe_bots(HOME, bots, robots, get=get)
    assert [u for u, _ in log] == [HOME] * 3, log
    assert "seo-miner" in log[0][1] and "OAI-SearchBot" not in log[0][1], log[0]
    assert sum("OAI-SearchBot/" in ua for _, ua in log) == 1
    assert sum("Claude-User/" in ua for _, ua in log) == 1
    assert out["baseline"] == 200
    assert out["rows"]["OAI-SearchBot"]["verdict"] == "refused"
    assert out["rows"]["Claude-User"]["verdict"] == "served"
    assert set(out["rows"]) == {"OAI-SearchBot", "Claude-User"}, out
    # 기준부터 막혔으면 봇 요청을 하지 않는다 — 물어봐야 가를 수 없다
    log.clear()
    out = ai_access.probe_bots(HOME, bots, "", get=lambda url, ua: (
        log.append(ua) or {"status": 403, "final_url": url, "headers": {}, "text": ""}))
    assert len(log) == 1 and out["rows"] == {} and out["baseline"] == 403, (log, out)
    # 한 봇에서 예외가 나도 나머지는 간다
    def flaky(url, ua):
        if "OAI-SearchBot" in ua:
            raise RuntimeError("boom")
        return {"status": 200, "final_url": url, "headers": {}, "text": ""}
    out = ai_access.probe_bots(HOME, bots, "", get=flaky)
    assert out["rows"]["OAI-SearchBot"]["verdict"] == "unknown"
    assert out["rows"]["PerplexityBot"]["verdict"] == "served"
    # 저장 꼴 왕복 + 행에 붙이기
    packed = ai_access.probe_pack(out)
    rows = [{"bot": "OAI-SearchBot", "rule": None}, {"bot": "GPTBot", "rule": "Disallow: /"}]
    ai_access.attach_server(rows, packed)
    assert rows[0]["server"]["verdict"] == "unknown" and rows[0]["server"]["label"]
    assert "server" not in rows[1] or rows[1]["server"] is None
    ai_access.attach_server(rows, None)          # 옛 회차 — 조용히 지나간다
    ai_access.attach_server(rows, "{깨진 json")


def test_scoring_uses_this_module_for_every_robots_verdict():
    """이음매: 판정은 한 벌이다 — scoring.robots_blocks·_named_in_robots 가 여기를 부른다."""
    import scoring
    txt = NL.join(("User-agent: *", "Disallow: /", "", "User-agent: OAI-SearchBot", "Allow: /",
                   "", "User-agent: bot", "Disallow: /"))
    assert scoring.robots_blocks(txt, HOME, agent="OAI-SearchBot") is None
    assert scoring.robots_blocks(txt, HOME, agent="Googlebot") == "Disallow: /"
    assert scoring._named_in_robots(txt, "OAI-SearchBot") is True
    assert scoring._named_in_robots(txt, "GPTBot") is False       # `bot` 은 GPTBot 이 아니다
    st = {r["bot"]: r for r in scoring.ai_bot_status(
        NL.join(("User-agent: GPTBot/1.1", "Disallow: /", "User-agent: *", "Allow: /")), "x.kr")}
    assert st["GPTBot"]["rule"] == "Disallow: /" and st["GPTBot"]["group"] == "GPTBot", st["GPTBot"]
    assert st["GPTBot"]["ua_notes"] and st["OAI-SearchBot"]["ua_notes"] == []
    assert st["OAI-SearchBot"]["group"] == "*"


def test_config_bots_are_valid_tokens_and_unique():
    """목록의 UA 는 robots.txt 토큰이다 — 글자·`_`·`-` 밖의 문자가 있으면 어떤 묶음에도
    못 걸려 늘 '열림'으로 나온다. 같은 토큰 두 줄은 표에 두 번 선다."""
    import skill_config
    uas = [b["ua"] for b in skill_config.CONFIG["ai_bots"]]
    for ua in uas:
        assert ai_access.is_token(ua), ua
    assert len({u.lower() for u in uas}) == len(uas), uas


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
    print("test_ai_access ok")
