#!/usr/bin/env python3
"""[순위 상승 기회](keywords)의 「내부 경쟁(카니벌라이제이션)」 소카드 — 근거 보기가 항상 선다.

문제였던 것: 내부 경쟁(cannibalization) 기회의 근거 섹션이 `log`(검색어별 표)였는데, 그 표는 4~20위 상위 15개만 싣는다.
기회의 검색어가 그 표에 없으면 개요/큐의 [자세히 보기]가 근거를 못 보여 줬다. 지금은 [전체] 탭에 소카드가 서고,
기회로 선 검색어마다 그 검색어를 나눠 가진 내 페이지들(주소·노출·클릭·순위·점유율)과 정본 후보를 보인다.

못 박는 것:
  · 마크업 — `kw-cannibal` 섹션이 [전체] 탭 안에 있고, 비면 뷰가 data-kw-off 로 접는다(hidden 은 셸 탭이 쓴다).
  · 이음매 — scoring 의 cannibalization see 가 그 섹션을 가리킨다(섹션 존재·탭 검사는 test_modes_assign 이 이미 한다).
  · 사본 금지 — 소카드는 기회(d.opps)와 페이지(d.query_pages)를 셸에서 읽는다. 정본 후보의 기준은 처방(scoring play)의
    한 곳이라 이 화면은 그 순서(클릭 → 노출)를 따르고 새 기준을 만들지 않는다.
  · 화면(브라우저) — 상위 15개 밖 검색어도 소카드에 서고, 페이지별 숫자가 나오고, 정본 후보가 하나만 달리고,
    [상태 바꾸기]가 [할 것] 큐의 서랍(같은 기회 한 벌)을 열고, 개요 줄의 [자세히 보기]가 [전체] 탭의 소카드로 온다.
    기회가 없는 사이트에서는 소카드가 접힌다. 로컬·호스팅(serve_hosted) 둘 다.

CLAUDE.md 의 규율: 검사를 추가했으면 일부러 깨서 FAIL 이 나는 것까지 확인한다.
self-check: python test_modes_keywords_fix.py [--static] [-v]
"""
from __future__ import annotations

import json
import re
import shutil
import sys
import tempfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

import test_modes_assign as ma  # noqa: E402  (헬퍼: _chains·_all_defs·_read·_view_def)

VIEWS = ma.TPL / "views"
DOCS = ma.DOCS
SEC = "kw-cannibal"
TITLE = "내부 경쟁(카니벌라이제이션)"


def _kw() -> str:
    return (VIEWS / "keywords.html").read_text("utf-8")


def _markup(src: str) -> str:
    src = re.sub(r"<!--.*?-->", "", src, flags=re.S)
    return re.sub(r"<script\b.*?</script>|<style\b.*?</style>", "", src, flags=re.S)


# ───────────────────────── 정적 ─────────────────────────

def test_section_exists_in_manage_and_is_not_the_old_log():
    chain = ma._chains("keywords")
    assert SEC in chain, f"keywords.html 마크업에 소카드 섹션 #{SEC} 가 없다"
    tabs = [a.get("data-mode") for a in chain[SEC] if a.get("data-mode")]
    assert tabs == ["manage"], f"#{SEC} 가 [전체] 탭 래퍼 안에 없다: {tabs}"
    sec = chain[SEC][-1]
    assert sec["_tag"] == "section", f"#{SEC} 는 화면 상자로 옮겨지는 <section> 이어야 한다(탭 래퍼): {sec['_tag']}"
    body = _markup(_kw())
    assert TITLE in body, f"소카드 제목 「{TITLE}」 이 마크업에 없다"
    assert f'id="{SEC}-body"' in body, f"소카드 본문 자리 #{SEC}-body 가 없다"


def test_see_points_at_the_card_not_the_log_table():
    import scoring
    k = next(x for x in scoring.KINDS if x.name == "cannibalization")
    assert k.see == ("keywords", SEC), \
        f"cannibalization 의 see 가 {k.see} — 15개로 잘리는 log 표가 아니라 소카드({SEC})를 가리켜야 한다"


def test_card_folds_with_own_attribute_not_hidden():
    src = _kw()
    head = src.split("</script>", 1)[1]
    css = "".join(re.findall(r"<style>(.*?)</style>", head, re.S))
    assert re.search(r"#view-keywords\s*>\s*section\[data-kw-off\]\s*\{\s*display\s*:\s*none", css), \
        "비면 소카드를 접는 규칙(#view-keywords > section[data-kw-off])이 없다 — hidden 은 셸 탭이 쓴다"
    assert not re.search(r'\$\("%s"\)\.hidden\s*=' % SEC, src), f"뷰가 #{SEC} 의 hidden 을 쓴다 — 셸 탭과 겹친다"
    assert "data-kw-off" in src and "removeAttribute" in src, "비었을 때 data-kw-off 를 달고 데이터가 있으면 떼야 한다"


def test_new_css_follows_repo_rules():
    src = _kw()
    css = "".join(re.findall(r"<style>(.*?)</style>", src, re.S))
    assert "!important" not in css, "!important 금지"
    for m in re.finditer(r"([^{}]*kwc[^{}]*)\{([^}]*)\}", css):
        body = m.group(2)
        assert "letter-spacing" not in body and "uppercase" not in body, \
            f"한글에 자간·대문자 변환을 걸지 않는다: {m.group(1).strip()}"


def test_card_reads_shell_data_and_does_not_copy_rules():
    src = _kw()
    assert "KW_PAGES" in src and "d.opps" in src, "소카드가 셸이 준 기회·페이지를 읽지 않는다"
    # 기회 종류 이름은 view-def 에 적지 않는다(test_modes_assign 이 지키는 사본 금지) — 코드 안 비교 한 곳만 허용
    head = src.split("</script>", 1)[0]
    assert "cannibalization" not in head, "view-def 에 종류 이름을 적지 않는다"


def test_doc_describes_the_card():
    t = (DOCS / "keywords.md").read_text("utf-8")
    assert TITLE in t, f"docs/screens/keywords.md 에 「{TITLE}」 소카드 설명이 없다"
    assert "정본 후보" in t, "docs/screens/keywords.md 에 정본 후보 설명이 없다"


# ───────────────────────── 화면(브라우저) ─────────────────────────

# 심은 검색어 — 어느 것도 리포 다른 데 없는 표식이다.
TOP = "카니벌 상위밖 검색어"          # 순위 8위 구간인데 노출이 적어 상위 15개에 못 든다
FAR = "카니벌 먼 검색어"              # 순위 25위 — 4~20위 표에 아예 안 든다
NOPG = "카니벌 페이지모름 검색어"     # 기회만 있고 페이지별 실적 행이 없다
FILL = "카니벌 채움"                  # 상위 15개를 채우는 검색어(노출 큼)
PG = lambda site, n: f"https://{site}.example/kwc-{n}"  # noqa: E731


def _seed(site: str) -> None:
    """test_render 픽스처 위에 — 상위 15개를 채우는 검색어 16개, 내부 경쟁 기회 셋(+ma._seed 의 한 건)."""
    import db
    import scoring
    conn = db.connect()
    try:
        pid = conn.execute("SELECT id FROM projects WHERE name=?", (site,)).fetchone()[0]
        d = "2026-06-01"

        def snap(q, page, imp, clk, pos):
            conn.execute(
                "INSERT INTO gsc_snapshots(project_id,snapshot_date,period_days,query,page,clicks,impressions,ctr,position)"
                " VALUES(?,?,28,?,?,?,?,0.05,?)", (pid, d, q, page, clk, imp, pos))

        for i in range(16):
            snap(f"{FILL} {i}", PG(site, f"fill{i}"), 5000 - i, 50, 12.0 + (i % 4) * 0.5)
        # TOP: 8위 구간, 두 페이지가 나눠 가진다 — 클릭은 b 가 많고 노출은 a 가 많다(두 기준이 갈리는 표)
        snap(TOP, PG(site, "a"), 90, 1, 8.0)
        snap(TOP, PG(site, "b"), 60, 4, 9.0)
        # FAR: 25위 — 클릭·노출 모두 a 가 앞선다
        snap(FAR, PG(site, "c"), 300, 9, 25.0)
        snap(FAR, PG(site, "d"), 100, 2, 27.0)
        for q in (TOP, FAR, NOPG):
            conn.execute(
                "INSERT INTO opportunities(project_id,kind,target,score,reasoning,status) VALUES(?,?,?,?,?,'new')",
                (pid, "cannibalization", q, 68, "카니벌 점검용"))
        if "cannibalization" in scoring.TRIAGE_KINDS:
            db.set_verdicts(conn, pid, [scoring.verdict_key("cannibalization", q) for q in (TOP, FAR, NOPG)], "work")
        conn.commit()
    finally:
        conn.close()


PROBE = r"""<script>
(async function () {
  var out = {}, sleep = function (ms) { return new Promise(function (r) { setTimeout(r, ms); }); };
  var vis = function (el) { return !!el && el.getClientRects().length > 0; };
  var txt = function (el) { return (el && el.textContent || "").replace(/\s+/g, " ").trim(); };
  var site = decodeURIComponent(location.hash.slice(1));
  try {
    await sleep(2500);
    SM.show("keywords");
    var box = document.getElementById("view-keywords");
    var tabs = [].slice.call(box.querySelectorAll(":scope > .vtabs [role=tab]"));
    out.tabs = tabs.map(function (t) { return t.firstChild.textContent.trim(); });
    tabs[0].click(); await sleep(150);
    out.firstOnWork = !vis(document.getElementById("kw-cannibal"));      // [할 것] 탭에서는 소카드가 안 보인다
    tabs[1].click(); await sleep(250);
    var sec = document.getElementById("kw-cannibal");
    out.sec = {exists: !!sec, visible: vis(sec), off: !!sec && sec.hasAttribute("data-kw-off"),
               h2: txt(sec && sec.querySelector("h2")), mode: SM.modeNow("keywords")};
    var logq = [].slice.call(document.querySelectorAll("#log tr[data-kwq]")).map(function (r) { return r.dataset.kwq; });
    out.logN = logq.length;
    out.rows = [].slice.call(sec ? sec.querySelectorAll("[data-kwc]") : []).map(function (el) {
      var cand = el.querySelectorAll(".kwc-cand");
      return {q: el.dataset.kwc, inLog: logq.indexOf(el.dataset.kwc) >= 0,
              pages: [].slice.call(el.querySelectorAll("tr[data-kwc-page]")).map(function (tr) {
                return {url: tr.dataset.kwcPage, cand: tr.querySelectorAll(".kwc-cand").length, text: txt(tr)}; }),
              cand: cand.length, text: txt(el), hasOpen: !!el.querySelector("[data-kwc-open]")};
    });
    // 소카드에서 상태 서랍을 연다 — [할 것] 큐의 서랍과 같은 기회 한 벌.
    var first = sec && sec.querySelector("[data-kwc-open]");
    if (first) {
      first.click(); await sleep(400);
      var dr = document.getElementById("qdr-keywords");
      out.drawer = {exists: !!dr, visible: vis(dr), text: txt(dr).slice(0, 200),
                    forQ: first.closest("[data-kwc]").dataset.kwc};
      SM.queue.of("keywords").drawerClose();
    }
    // 개요 줄 → 자세히 보기 → 소카드
    SM.show("overview"); await sleep(80);
    var see = (window.KIND_VIEWS || {}).cannibalization;
    out.see = see;
    SM.queue.goSee(see[0], see[1]); await sleep(250);
    var el = document.getElementById(see[1]);
    out.goSee = {tab: SM.modeNow("keywords"), visible: vis(el), top: el ? Math.round(el.getBoundingClientRect().top) : null};
  } catch (e) { out.err = String(e && e.stack || e); }
  var d = document.createElement("div"); d.id = "__kwf__";
  d.textContent = JSON.stringify(out); document.body.appendChild(d);
})();
</script>"""


def _assert_with_cards(label: str, o: dict) -> None:
    assert o["tabs"][:2] == ["할 것", "전체"] or o["tabs"][0].startswith("할 것"), f"{label}: 탭 줄이 이상하다: {o['tabs']}"
    s = o["sec"]
    assert s["exists"] and s["visible"], f"{label}: [전체] 탭에서 소카드가 안 보인다: {s}"
    assert s["h2"] == TITLE, f"{label}: 소카드 제목이 다르다: {s['h2']!r}"
    assert o["firstOnWork"], f"{label}: [할 것] 탭인데 소카드가 보인다 — 탭 래퍼가 아니다"
    rows = {r["q"]: r for r in o["rows"]}
    for q in (TOP, FAR, NOPG):
        assert q in rows, f"{label}: 기회로 선 검색어 {q!r} 가 소카드에 없다: {sorted(rows)}"
        assert rows[q]["hasOpen"], f"{label}: {q!r} 에 상태 바꾸기 손잡이가 없다"
    # 이 시험의 전제 — 상위 15개 밖이라야 의미가 있다
    assert not rows[TOP]["inLog"] and o["logN"] <= 15, \
        f"{label}: 전제가 깨졌다 — {TOP!r} 가 검색어별 표에 이미 있다(표 {o['logN']}줄)"
    assert not rows[FAR]["inLog"], f"{label}: 전제가 깨졌다 — {FAR!r} 가 검색어별 표에 있다"
    # 페이지별 분해
    t = rows[TOP]["pages"]
    assert [p["url"].rsplit("-", 1)[-1] for p in t] == ["a", "b"], f"{label}: {TOP!r} 의 페이지 순서(노출 순): {t}"
    assert "90" in t[0]["text"] and "60" in t[1]["text"], f"{label}: 노출이 안 나온다: {t}"
    assert "1" in t[0]["text"] and "4" in t[1]["text"], f"{label}: 클릭이 안 나온다: {t}"
    assert "8" in t[0]["text"] and "9" in t[1]["text"], f"{label}: 순위가 안 나온다: {t}"
    assert "60%" in t[0]["text"] and "40%" in t[1]["text"], f"{label}: 점유율(노출 비중)이 안 나온다: {t}"
    # 정본 후보 — 한 검색어에 하나, 처방의 순서(클릭 → 노출)를 따른다: TOP 은 클릭이 많은 b, FAR 는 둘 다 앞서는 c
    assert rows[TOP]["cand"] == 1 and t[1]["cand"] == 1 and t[0]["cand"] == 0, \
        f"{label}: {TOP!r} 의 정본 후보가 클릭 많은 쪽(b)이어야 한다: {t}"
    f = rows[FAR]["pages"]
    assert rows[FAR]["cand"] == 1 and f[0]["cand"] == 1, f"{label}: {FAR!r} 의 정본 후보가 c 가 아니다: {f}"
    # 페이지별 실적이 없으면 비워 두지 않고 이유를 말한다
    assert rows[NOPG]["pages"] == [] and rows[NOPG]["cand"] == 0, f"{label}: 페이지 없는 검색어에 후보가 달렸다"
    assert "아직" in rows[NOPG]["text"], f"{label}: 페이지별 실적이 없는 이유를 안 말한다: {rows[NOPG]['text']}"
    # 상태 서랍
    dr = o["drawer"]
    assert dr["exists"] and dr["visible"], f"{label}: 소카드의 [상태 바꾸기]가 서랍을 못 열었다: {dr}"
    assert dr["forQ"] in dr["text"], f"{label}: 서랍이 누른 검색어({dr['forQ']!r})가 아니다: {dr['text']!r}"
    # 개요 줄 → 자세히 보기
    assert o["see"] == ["keywords", SEC], f"{label}: 셸이 아는 cannibalization see 가 {o['see']}"
    g = o["goSee"]
    assert g["tab"] == "manage" and g["visible"], f"{label}: '자세히 보기'가 [전체] 탭의 소카드에 못 닿았다: {g}"
    assert -40 <= g["top"] <= 400, f"{label}: 소카드가 화면 위쪽에 서지 않았다(top={g['top']})"


def _assert_empty(label: str, o: dict) -> None:
    s = o["sec"]
    assert s["exists"] and not s["visible"] and s["off"], f"{label}: 기회가 없는데 빈 소카드가 보인다: {s}"
    assert not o["rows"], f"{label}: 기회가 없는데 줄이 있다: {o['rows']}"


def run() -> None:
    import test_render as tr
    browser = tr.find_browser()
    if not browser:
        print("크롬/엣지가 없어 건너뜁니다")
        return
    home = Path(tempfile.mkdtemp(prefix="seo-miner-kwfix-"))
    saved = tr.RELOAD_TOUCH
    try:
        tr.fixture(home)
        full, empty = tr.SITES[1], tr.SITES[0]
        ma._seed(home)            # 다른 종류의 기회도 같이 서 있는 사이트에서 본다
        _seed(full)
        import dashboard
        shell = dashboard.HTML.replace(b"</head>", tr.PROBE.encode("utf-8") + b"</head>", 1)
        shell = shell.replace(b"</body>", PROBE.encode("utf-8") + b"</body>", 1)
        prof = home / "chrome-profile"
        with tr._stdlib(shell) as site:
            o = ma._read(tr.dom(browser, f"{site.base}/d#{full}", prof), "__kwf__")
            e = ma._read(tr.dom(browser, f"{site.base}/d#{empty}", prof), "__kwf__")
        if "-v" in sys.argv:
            print(json.dumps({"full": o, "empty": e}, ensure_ascii=False, indent=1))
        _assert_with_cards("로컬", o)
        _assert_empty("로컬(기회 없는 사이트)", e)
        print("  로컬: ok")
        try:
            with tr.serve_hosted(home / "hosted") as site:
                import os
                import store
                conn = store.connect()
                try:
                    uid = store.upsert_user(conn, tr.HOSTED_EMAIL)
                finally:
                    conn.close()
                old = os.environ.get("CAPTURE_HOME")
                os.environ["CAPTURE_HOME"] = str(store.home(uid))
                try:
                    ma._seed(store.home(uid))
                    _seed(full)
                finally:
                    if old is None:
                        os.environ.pop("CAPTURE_HOME", None)
                    else:
                        os.environ["CAPTURE_HOME"] = old
                tr.RELOAD_TOUCH = PROBE
                pg = tr.dom(browser, f"{site.base}/d?reload_probe=1#{full}", prof)
                site.after()
                assert not tr.probe(pg)["err"], "호스팅: JS 가 터졌다:\n  " + "\n  ".join(tr.probe(pg)["err"][:6])
                assert not site.failures(), "호스팅: 서버가 4xx/5xx 를 냈다:\n  " + "\n  ".join(site.failures()[:8])
                _assert_with_cards("호스팅", ma._read(pg, "__kwf__"))
        finally:
            tr.RELOAD_TOUCH = saved
        print("  호스팅: ok")
    finally:
        tr.RELOAD_TOUCH = saved
        import gc
        gc.collect()
        shutil.rmtree(home, ignore_errors=True)


def _static() -> list:
    return [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]


if __name__ == "__main__":
    failed = 0
    for fn in _static():
        try:
            fn()
            print(f"  ok  {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL  {fn.__name__}: {e}")
    if "--static" not in sys.argv:
        try:
            run()
        except AssertionError as e:
            failed += 1
            print(f"FAIL  화면: {e}")
    if failed:
        sys.exit(1)
    print("modes_keywords_fix self-check ok")
