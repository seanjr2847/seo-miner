#!/usr/bin/env python3
"""[페이지별] 본체(page_view) — 언어판 묶기·합계·추이·검색어·AI 인용.

  · hreflang 이 말한 언어판은 경로가 달라도 한 글이다(우리 도메인 것만)
  · hreflang 을 모르는 주소는 시장 경로(/kr/ko/ · /us/en/)를 뗀 나머지로 묶는다
  · 합계는 일별의 마지막 28일, 일별을 받기 전이면 최근 스냅샷
  · AI 인용은 가장 최근 ai 런에서 이 주소들을 인용한 답만, 함께 인용된 남의 도메인과
  · 모르는 주소로 불러도 그 주소 하나로 선다(오류가 아니다)
  · 두 배포가 같은 본체를 부른다 — dashboard.ROUTES 에 /api/pages · /api/page
"""
import json
import os
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

HOME = Path(tempfile.mkdtemp(prefix="seo-miner-pageview-test-"))
os.environ["CAPTURE_HOME"] = str(HOME)
sys.path.insert(0, str(Path(__file__).parent))

import db  # noqa: E402
import page_view  # noqa: E402

D = "https://shop.com"
KO, EN, JA = f"{D}/kr/ko/serum", f"{D}/us/en/serum", f"{D}/jp/ja/biyoueki"


_SEQ = [0]


def _site(name):
    # 이름에 번호를 붙인다 — 같은 프로세스에서 다시 불러도(깨 보기 검사) 앞 판의 행과 안 섞인다.
    _SEQ[0] += 1
    name = f"{name}{_SEQ[0]}"
    conn = db.connect()
    conn.execute("INSERT INTO projects(name, domain, locale) VALUES(?, 'shop.com', 'ko-KR')", (name,))
    pid = conn.execute("SELECT id FROM projects WHERE name=?", (name,)).fetchone()["id"]
    db.set_markets(conn, pid, [("ko-KR", "/kr/ko/"), ("en-US", "/us/en/"), ("ja-JP", "/jp/ja/")])
    conn.commit()
    return conn, pid


def _daily(conn, pid, page, days, clicks, imp=100, pos=5.0):
    last = date.today() - timedelta(days=3)
    db.write_gsc_page_daily(conn, pid, [(str(last - timedelta(days=i)), page, clicks, imp,
                                         clicks / imp, pos) for i in range(days)])


def test_groups_join_hreflang_and_market_paths():
    conn, pid = _site("grp")
    _daily(conn, pid, KO, 3, 10)
    _daily(conn, pid, EN, 3, 2)
    _daily(conn, pid, f"{D}/kr/ko/faq", 3, 1)
    _daily(conn, pid, f"{D}/us/en/faq", 3, 1)
    # 일본어판은 슬러그가 달라 경로로는 못 묶는다 — 한국어판의 hreflang 이 말해 준다.
    # 남의 도메인으로 가는 alternate 는 언어판이 아니다.
    db.write_page_audits(conn, pid, str(date.today()), [{
        "url": KO, "title": "레티놀 세럼", "modified": "2026-09-14",
        "hreflang_json": json.dumps([["ko", KO], ["en", EN], ["ja", JA], ["fr", "https://other.com/fr"]])}])
    gs = sorted(sorted(g) for g in page_view.groups(conn, pid))
    assert sorted([EN, JA, KO]) in gs, gs
    assert sorted([f"{D}/kr/ko/faq", f"{D}/us/en/faq"]) in gs, f"시장 경로로 못 묶었다: {gs}"
    assert not any("other.com" in u for g in gs for u in g), "남의 도메인을 언어판으로 묶었다"

    idx = page_view.page_index(conn, pid)
    top = idx["pages"][0]
    assert top["label"] == "레티놀 세럼" and top["key"] == KO, top
    assert {u["locale"] for u in top["urls"]} == {"ko-KR", "en-US", "ja-JP"}, top
    assert top["clicks"] == 36, top
    assert [m["locale"] for m in idx["markets"]] == ["ko-KR", "en-US", "ja-JP"], idx["markets"]
    conn.close()


def test_default_market_without_path_joins_by_path():
    """기본 시장이 경로 없이 도메인 전체(theotherskin: ko-KR = '', en-US = /en/ …)여도 경로로 묶는다.
    /en/ 아래가 아닌 주소는 기본 시장의 것이고, 그 경로 그대로가 '나머지'다."""
    conn = db.connect()
    _SEQ[0] += 1
    name = f"dflt{_SEQ[0]}"
    conn.execute("INSERT INTO projects(name, domain, locale) VALUES(?, 'skin.com', 'ko-KR')", (name,))
    pid = conn.execute("SELECT id FROM projects WHERE name=?", (name,)).fetchone()["id"]
    db.set_markets(conn, pid, [("ko-KR", ""), ("en-US", "/en/"), ("ja-JP", "/jp/")])
    for u in ("https://skin.com/treat/laser", "https://skin.com/en/treat/laser",
              "https://skin.com/jp/treat/laser", "https://skin.com/faq"):
        _daily(conn, pid, u, 2, 1)
    gs = sorted(sorted(g) for g in page_view.groups(conn, pid))
    assert ["https://skin.com/en/treat/laser", "https://skin.com/jp/treat/laser",
            "https://skin.com/treat/laser"] in gs, gs
    assert ["https://skin.com/faq"] in gs, gs
    d = page_view.page_detail(conn, pid, "https://skin.com/treat/laser")
    assert {v["url"]: v["locale"] for v in d["versions"]}["https://skin.com/treat/laser"] == "ko-KR"
    conn.close()


def test_split_addresses_fold_into_one_and_foreign_hreflang_names_the_language():
    """서치콘솔이 같은 페이지를 http·https·www·끝 '/'·?utm_ 로 갈라 보고해도 한 주소다 — 숫자는 더한다.
    시장 설정에 없는 언어판(/ja/)은 hreflang 코드가 언어를 말한다(도메인 전체 시장 ko 로 찍지 않는다).
    실데이터(theotherskin 홈): 갈린 주소 둘이 언어판 일곱 개 중 두 칸을 차지했고 /ja/ 가 한국어로 섰다."""
    conn = db.connect()
    _SEQ[0] += 1
    name = f"fold{_SEQ[0]}"
    conn.execute("INSERT INTO projects(name, domain, locale) VALUES(?, 'skin.com', 'ko-KR')", (name,))
    pid = conn.execute("SELECT id FROM projects WHERE name=?", (name,)).fetchone()["id"]
    db.set_markets(conn, pid, [("ko-KR", ""), ("en-US", "/en/")])
    _daily(conn, pid, "https://skin.com/", 2, 10)
    _daily(conn, pid, "https://skin.com/?utm_source=google_gbp&utm_medium=search", 2, 3)
    _daily(conn, pid, "http://www.skin.com/en/", 2, 1)
    _daily(conn, pid, "https://skin.com/en", 2, 2)
    db.write_page_audits(conn, pid, str(date.today()), [{
        "url": "https://skin.com/", "title": "홈",
        "hreflang_json": json.dumps([["ko", "https://skin.com/"], ["en", "https://skin.com/en/"],
                                     ["ja", "https://skin.com/ja/"], ["x-default", "https://skin.com/"]])},
        # 옛 주소 — 어느 hreflang 에도 없지만 /ja/ 아래다(형제 /ja/ 의 코드를 따른다). 자기 hreflang 이
        # 홈을 가리켜 같은 글로 묶인다.
        {"url": "https://skin.com/ja/old", "hreflang_json": json.dumps([["ko", "https://skin.com/"]])}])
    d = page_view.page_detail(conn, pid, "http://skin.com/en/")
    got = {v["url"]: (v["locale"], v["locale_label"], v["clicks"]) for v in d["versions"]}
    assert got == {"https://skin.com/": ("ko-KR", "한국어", 26),
                   "https://skin.com/en": ("en-US", "영어", 6),
                   "https://skin.com/ja/": ("ja", "일본어", 0),
                   "https://skin.com/ja/old": ("ja", "일본어", 0)}, got
    assert len(d["daily"]["series"]["https://skin.com/"]) == 2, "갈린 주소의 같은 날을 더하지 않았다"
    assert d["daily"]["series"]["https://skin.com/"][0][1] == 13
    conn.close()


def test_screen_folds_addresses_with_the_same_rule():
    """화면(views/page.html 의 PG_norm)이 고친 날(d.page_works)을 언어판에 맞출 때 쓰는 규칙이
    서버의 norm 과 같다 — 한쪽만 바뀌면 고친 날 세로선이 말없이 사라진다."""
    import re
    import shutil
    import subprocess
    if not shutil.which("node"):
        print("  (node 가 없어 화면 쪽 대조를 건너뜁니다)")
        return
    src = (Path(__file__).parent.parent / "templates" / "views" / "page.html").read_text("utf-8")
    m = re.search(r"^const PG_TRACK = .*?;\n^function PG_norm\(u\) \{.*?^\}", src, re.M | re.S)
    assert m, "page.html 에서 PG_norm 을 못 찾았다"
    samples = ["https://www.Skin.com/a/", "http://skin.com/a", "https://skin.com/?utm_source=x&utm_medium=y",
               "https://skin.com/p?id=3&utm_campaign=z", "https://skin.com/#top", "/kr/ko/", "https://skin.com",
               "https://skin.com/p?gclid=1&b=2&a=1", "https://skin.com/q?x=a%20b&_gl=9"]
    js = m.group(0) + "\nconsole.log(JSON.stringify(" + json.dumps(samples) + ".map(PG_norm)));"
    r = subprocess.run(["node", "-e", js], capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr
    want = [page_view.norm(u) for u in samples]
    assert json.loads(r.stdout) == want, f"화면과 서버의 주소 열쇠가 다르다: {r.stdout} ↔ {want}"


def test_single_market_site_groups_only_by_hreflang():
    conn = db.connect()
    _SEQ[0] += 1
    conn.execute("INSERT INTO projects(name, domain, locale) VALUES(?, 'solo.com', 'ko-KR')",
                 (f"solo{_SEQ[0]}",))
    pid = conn.execute("SELECT id FROM projects WHERE name=?", (f"solo{_SEQ[0]}",)).fetchone()["id"]
    _daily(conn, pid, "https://solo.com/a", 2, 3)
    _daily(conn, pid, "https://solo.com/en/a", 2, 3)
    gs = page_view.groups(conn, pid)
    assert len(gs) == 2, f"시장 경로가 없는데 묶었다: {gs}"
    conn.close()


def test_detail_totals_trend_queries_and_ai():
    conn, pid = _site("det")
    _daily(conn, pid, KO, 40, 10, imp=200, pos=4.0)        # 28일 창만 센다
    _daily(conn, pid, EN, 5, 1, imp=100, pos=12.0)
    db.write_page_audits(conn, pid, str(date.today()), [{
        "url": KO, "title": "레티놀 세럼", "modified": "2026-09-14",
        "hreflang_json": json.dumps([["en", EN]])}])
    snap = str(date.today())
    db.write_gsc_snapshot(conn, pid, snap, 28, [
        ("세럼 추천", KO, 50, 1000, 0.05, 3.0), ("retinol serum", EN, 4, 300, 0.013, 11.0),
        ("다른 글", f"{D}/kr/ko/other", 9, 90, 0.1, 2.0)])
    run = db.start_run(conn, pid, "ai")
    conn.execute("INSERT INTO ai_prompts(project_id, prompt, locale) VALUES(?, '세럼 추천해줘', 'ko-KR')", (pid,))
    qid = conn.execute("SELECT id FROM ai_prompts WHERE project_id=?", (pid,)).fetchone()["id"]
    db.record_ai_check(conn, qid, run, "chatgpt", 0, 1, 1, ["rival.com"], "…",
                       citations=[{"url": KO + "/", "title": "t"}, {"url": "https://rival.com/x"}])
    db.record_ai_check(conn, qid, run, "gemini", 0, 0, 0, ["rival.com"], "…",
                       citations=[{"url": "https://rival.com/x"}])
    conn.commit()

    d = page_view.page_detail(conn, pid, EN)
    assert d["key"] == KO and [v["url"] for v in d["versions"]] == [KO, EN], d["versions"]
    ko = d["versions"][0]
    assert ko["clicks"] == 280 and ko["impressions"] == 5600, f"28일 창이 아니다: {ko}"
    assert ko["position"] == 4.0 and ko["modified"] == "2026-09-14" and ko["locale"] == "ko-KR", ko
    assert d["window"]["source"] == "daily"
    assert len(d["daily"]["series"][KO]) == 40 and d["daily"]["series"][KO][0][0] < d["daily"]["series"][KO][-1][0]
    assert {q["query"] for q in d["queries"]} == {"세럼 추천", "retinol serum"}, d["queries"]
    rows = d["ai"]["rows"]
    assert [(r["engine"], r["urls"], r["others"]) for r in rows] == [("chatgpt", [KO], ["rival.com"])], \
        f"인용한 답만, 주소는 끝 '/' 를 무시하고 찾아야 한다: {rows}"
    assert rows[0]["locale"] == "ko-KR" and rows[0]["samples"] == 1
    conn.close()


def test_unknown_url_and_snapshot_fallback():
    conn, pid = _site("fb")
    db.write_gsc_snapshot(conn, pid, str(date.today()), 28, [
        ("q1", KO, 5, 100, 0.05, 3.0), ("q2", KO, 1, 100, 0.01, 7.0)])
    d = page_view.page_detail(conn, pid, KO)
    assert d["window"]["source"] == "snapshot" and d["versions"][0]["clicks"] == 6, d
    assert d["versions"][0]["position"] == 5.0, "평균 순위는 노출 가중이다"
    assert d["daily"]["series"] == {}, "일별을 받기 전이면 추이가 비어야 한다(지어내지 않는다)"
    x = page_view.page_detail(conn, pid, f"{D}/kr/ko/new")
    assert [v["url"] for v in x["versions"]] == [f"{D}/kr/ko/new"] and x["versions"][0]["clicks"] == 0
    conn.close()


def test_routes_are_shared():
    import dashboard
    assert ("GET", "/api/pages") in dashboard.ROUTES and ("GET", "/api/page") in dashboard.ROUTES
    conn, pid = _site("rt")
    name = conn.execute("SELECT name FROM projects WHERE id=?", (pid,)).fetchone()["name"]
    _daily(conn, pid, KO, 2, 3)
    conn.close()
    got = dashboard.ROUTES[("GET", "/api/page")](name, {"url": KO}, None)
    assert got["versions"][0]["clicks"] == 6, got
    assert dashboard.ROUTES[("GET", "/api/pages")](name, {}, None)["pages"][0]["key"] == KO


def test_open_page_handoff_both_ends():
    """다른 화면 → [페이지별] 넘김은 이음매다 — 넘기는 쪽(셸 SM.api.openPage)이 쓰는 저장 키·신호
    이름을 받는 쪽(views/page.html)이 그대로 읽어야 한다. 한쪽 이름만 바뀌면 주소를 눌러도
    빈 고르는 칸만 열린다(오류도 없다)."""
    import re
    tpl = Path(__file__).parent.parent / "templates"
    shell = (tpl / "shell" / "06-opps.js").read_text("utf-8")
    view = (tpl / "views" / "page.html").read_text("utf-8")
    m = re.search(r"SM\.api\.openPage = url => \{(.*?)\n\};", shell, re.S)
    assert m, "셸에 SM.api.openPage 가 없다"
    keys = re.findall(r'sessionStorage\.setItem\("([^"]+)"', m.group(1))
    events = re.findall(r'new CustomEvent\("([^"]+)"', m.group(1))
    assert keys == ["sm:page"] and events == ["sm:page"], (keys, events)
    assert 'sessionStorage.getItem("sm:page")' in view, "받는 쪽이 넘긴 주소를 안 읽는다"
    assert 'addEventListener("sm:page"' in view, "받는 쪽이 넘김 신호를 안 듣는다"
    assert re.search(r'SM\.show\("page"\)', m.group(1)) and '"id": "page"' in view, "화면 id 가 어긋났다"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)} passed  ({HOME})")
