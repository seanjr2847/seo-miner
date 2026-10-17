#!/usr/bin/env python3
"""경쟁 검색어·백링크 화면의 [처리]·[관리] 두 탭 — 선언·마크업·실제 화면.

틀(view-def modes / 섹션 data-mode / 처리 큐 queue)은 셸이 한 벌로 갖는다(test_seams 170·180).
여기서는 **이 두 화면이 그 틀을 제대로 쓰는가**만 본다:

  · 선언: modes 는 [처리](count 경로는 queue_open_by_view.<화면 id>)·[관리], queue 는 {mode: work}.
    큐 종류 목록을 뷰에 적지 않는다(정본은 scoring.KINDS 의 see → queue_kinds_by_view).
  · 마크업: 화면 상자로 옮겨지는 섹션은 전부 [관리]에 든다. 섹션을 접는 것은 셸(hidden)이므로
    display:contents 인 섹션은 [hidden] 을 이기게 하는 규칙이 있고, 인쇄에서는 접힌 섹션이 펴진다.
  · 백링크 뷰가 섹션의 hidden 을 스스로 만지지 않는다(만지면 [처리]에 [관리]가 비어져 나온다).
  · 숨은 [관리] 섹션으로 가는 손잡이(띠의 버튼)는 탭부터 연다.
  · 실제 화면(로컬·호스팅): 처음엔 [처리]만 서고, [관리]를 누르면 섹션이 전부 크기를 갖고 서며
    (차트 폭 0 이 아니다), 다시 [처리]로 오면 접힌다.

크롬이 없으면 화면 검사는 조용히 건너뛴다. 새 검사를 추가했으면 일부러 깨서 FAIL 을 확인한다.

self-check: python test_modes_compete.py
"""
import json
import re
import shutil
import sys
import tempfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
ROOT = Path(__file__).resolve().parents[3]
VIEWS = ROOT / "skills" / "capture" / "templates" / "views"
DOCS = ROOT / "docs" / "screens"

IDS = ("competitors", "backlinks")
# 화면마다 [관리]에 들어가는 섹션(셸이 화면 상자로 옮기는 단위) — 정본은 view-def 의 sections.
MODES = [{"id": "work", "label": "처리", "count": "queue_open_by_view.{id}"},
         {"id": "manage", "label": "관리"}]


def _defn(vid: str) -> dict:
    src = (VIEWS / f"{vid}.html").read_text("utf-8")
    m = re.search(r'class="view-def">\s*(\{.*?\})\s*</script>', src, re.S)
    assert m, f"{vid}.html 에 view-def 가 없다"
    return json.loads(m.group(1))


def _markup(vid: str) -> str:
    """<style>·<script> 를 뺀 마크업 — 섹션 태그만 본다."""
    src = (VIEWS / f"{vid}.html").read_text("utf-8")
    src = re.sub(r'<script[\s\S]*?</script>|<style[\s\S]*?</style>|<!--[\s\S]*?-->', "", src)
    return src


def _style(vid: str) -> str:
    return "".join(re.findall(r"<style>([\s\S]*?)</style>", (VIEWS / f"{vid}.html").read_text("utf-8")))


def _script(vid: str) -> str:
    src = (VIEWS / f"{vid}.html").read_text("utf-8")
    return "".join(m for m in re.findall(r"<script>([\s\S]*?)</script>", src))


def test_modes_and_queue_are_declared():
    for vid in IDS:
        d = _defn(vid)
        want = json.loads(json.dumps(MODES).replace("{id}", vid))
        assert d.get("modes") == want, f"{vid}: modes 선언이 [처리]·[관리](count 경로 포함)와 다르다: {d.get('modes')}"
        assert d.get("queue") == {"mode": "work"}, \
            f"{vid}: queue 는 {{\"mode\": \"work\"}} 만 — 종류 목록을 뷰에 적으면 사본이 된다: {d.get('queue')}"
    assert _defn("competitors").get("head") == ["competitors"], "competitors 의 기존 head 선언을 건드렸다"
    assert _defn("backlinks").get("head") == ["backlinks"], "backlinks 의 기존 head 선언을 건드렸다"


def test_queue_kinds_come_from_the_server_not_the_view():
    import scoring
    inv = scoring.queue_kinds_by_view()
    assert inv.get("competitors") == ["content_gap"], inv.get("competitors")
    assert set(inv.get("backlinks", [])) == {"backlink_broken", "backlink_prospect", "backlink_mention"}, inv.get("backlinks")
    for vid in IDS:
        for kind in inv[vid]:
            assert kind not in json.dumps(_defn(vid)), f"{vid} 의 view-def 에 종류 이름({kind})이 적혀 있다 — 사본"


def test_every_section_is_in_the_manage_tab():
    for vid in IDS:
        d = _defn(vid)
        tags = re.findall(r"<section\b([^>]*)>", _markup(vid))
        ids = [re.search(r'\bid="([^"]+)"', a).group(1) for a in tags]
        assert ids == d["sections"], f"{vid}: view-def sections 와 마크업 섹션이 다르다: {ids} / {d['sections']}"
        for a, i in zip(tags, ids):
            assert re.search(r'\bdata-mode="manage"', a), f"{vid}: 섹션 #{i} 가 [관리]에 안 들었다 — 처리 탭에 현황이 섞인다"


def test_contents_sections_lose_to_hidden():
    """display:contents 인 섹션은 id 규칙이 UA 의 [hidden] 을 이긴다 — 접히려면 [hidden] 규칙이 따로 있어야 한다."""
    css = {vid: _style(vid) for vid in IDS}
    for sid in ("cp-share", "cp-gap"):
        assert re.search(rf"#{sid}\[hidden\]\s*[,{{]", css["competitors"]), f"#{sid}[hidden] 이 display:none 으로 접히지 않는다"
    assert re.search(r"#bl-profile\[hidden\]", css["backlinks"]), "#bl-profile[hidden] 이 접히지 않는다"
    # 인쇄: 접힌 섹션이 사라지지 않는다
    for vid in IDS:
        pr = re.findall(r"@media print\s*\{([\s\S]*?)\n\}", css[vid])
        assert pr and any("[hidden]" in b and "display" in b for b in pr), f"{vid}: 인쇄에서 접힌 섹션을 펴는 규칙이 없다"
    assert "#view-competitors > .qhost" in css["competitors"], "처리 큐 카드가 12열 격자 한 줄을 안 쓴다"


def test_backlinks_view_does_not_touch_section_hidden():
    """[처리]·[관리] 접기는 셸의 hidden 이다 — 뷰가 같은 섹션의 hidden 을 쓰면 [처리]에 [관리]가 비어져 나온다."""
    js = _script("backlinks")
    for sid in ("bl-profile", "bl-broken", "bl-mentions", "bl-intersect", "bl-links"):
        assert not re.search(rf'\$\("{sid}"\)\.hidden', js), f"{sid} 의 hidden 을 뷰가 직접 쓴다"
    assert "bl-none" in js and "bl-none" in _style("backlinks"), "자료 없음으로 섹션을 감추는 길(bl-none)이 없다"


def test_jump_handles_open_the_manage_tab_first():
    cp, bl = _script("competitors"), _script("backlinks")
    open_fn = re.search(r"function CP_open\(\)\s*\{([\s\S]*?)\n\}", cp).group(1)
    assert 'SM.setMode("competitors", "manage")' in cp and "CP_manage()" in open_fn, \
        "띠의 버튼(CP_open)이 [관리] 탭을 안 연다 — 숨은 목록으로 스크롤하면 아무 일도 안 일어난다"
    for fn in ("BLK_jump", "BLK_open", "BLK_lostView"):
        body = re.search(rf"function {fn}\([^)]*\)\s*\{{([\s\S]*?)\n\}}", bl).group(1)
        assert "BLK_manage()" in body or fn != "BLK_jump" and "BLK_jump(" in body, f"{fn} 가 [관리] 탭을 안 연다"
    assert 'SM.setMode("backlinks", "manage")' in bl


def test_docs_describe_both_tabs():
    for name in ("competitors", "backlinks"):
        t = (DOCS / f"{name}.md").read_text("utf-8")
        assert "[처리]" in t and "[관리]" in t, f"docs/screens/{name}.md 에 [처리]/[관리] 구성 단락이 없다"


# ── 실제 화면 ────────────────────────────────────────────────────────────────

# 두 화면을 차례로 열어 탭을 눌러 보고 결과를 DOM(#__mc__)에 옮겨 적는다. 가상 시계라 setTimeout 이 빨리 돈다.
RUN = """<script>
(function () {
  var wait = function (ms) { return new Promise(function (r) { setTimeout(r, ms); }); };
  var IDS = ["competitors", "backlinks"], out = {};
  var secs = function (box) {
    return [].slice.call(box.children).filter(function (e) { return e.tagName === "SECTION"; }).map(function (e) {
      var r = e.getBoundingClientRect();
      return {id: e.id, mode: e.dataset.mode, hid: e.hidden, disp: getComputedStyle(e).display,
              w: Math.round(r.width), kids: e.children.length};
    });
  };
  var cv = function (box) {
    return [].slice.call(box.querySelectorAll("canvas")).filter(function (c) {
      return getComputedStyle(c).display !== "none"; }).map(function (c) { return c.parentNode.clientWidth * (c.dataset.chOk ? 1 : 0); });
  };
  var tabsOf = function (box) {
    return [].slice.call(box.querySelectorAll(":scope > .vtabs [role=tab]")).map(function (b) {
      return {mode: b.dataset.mode, text: b.textContent.replace(/\\s+/g, " ").trim(), sel: b.getAttribute("aria-selected")}; });
  };
  (async function () {
    await wait(2500);
    for (var i = 0; i < IDS.length; i++) {
      var id = IDS[i], o = out[id] = {};
      try {
        try { sessionStorage.removeItem("sm:mode:" + id); } catch (e) {}
        SM.mode = {}; SM.touched = true; SM.show(id); await wait(400);
        var box = document.getElementById("view-" + id);
        o.tabs0 = tabsOf(box); o.secs0 = secs(box); o.q0 = !!box.querySelector("#q-" + id);
        o.qh = (box.querySelector("#q-" + id) || {}).hidden;
        var mb = box.querySelector(':scope > .vtabs [data-mode="manage"]');
        mb.click(); await wait(1500);
        o.tabs1 = tabsOf(box); o.secs1 = secs(box); o.cv1 = cv(box); o.qh1 = (box.querySelector("#q-" + id) || {}).hidden;
        box.querySelector(':scope > .vtabs [data-mode="work"]').click(); await wait(400);
        o.secs2 = secs(box);
        // 띠의 버튼이 가리키는 자리 — [처리]에 서 있어도 [관리]를 열어야 한다
        if (id === "competitors") CP_open(); else BLK_jump("bl-intersect");
        await wait(500);
        o.jump = SM.modeNow(id); o.secs3 = secs(box);
      } catch (e) { o.err = String(e && e.stack || e); }
    }
    var d = document.createElement("div"); d.id = "__mc__";
    d.textContent = JSON.stringify(out); document.body.appendChild(d);
  })();
})();
</script>"""


def _check_screens(label: str, html: str) -> None:
    import html as htmlmod
    m = re.search(r'id="__mc__"[^>]*>(.*?)</div>', html, re.S)
    assert m, f"{label}: 탭 검사 스크립트가 결과를 못 남겼다"
    out = json.loads(htmlmod.unescape(m.group(1)))
    for vid in IDS:
        o = out.get(vid) or {}
        assert not o.get("err"), f"{label} {vid}: 검사 스크립트가 터졌다: {o.get('err')}"
        assert [t["text"].split(" ")[0] for t in o["tabs0"]] == ["처리", "관리"], f"{label} {vid}: 탭 줄 {o['tabs0']}"
        assert [t["sel"] for t in o["tabs0"]] == ["true", "false"], f"{label} {vid}: 처음 탭은 [처리] {o['tabs0']}"
        assert o["q0"] and not o["qh"], f"{label} {vid}: [처리] 큐 카드(#q-{vid})가 안 섰다"
        manage = [s for s in o["secs0"] if s["mode"] == "manage"]
        assert manage, f"{label} {vid}: [관리] 섹션이 하나도 없다 {o['secs0']}"
        for s in manage:
            assert s["hid"] and s["disp"] == "none", \
                f"{label} {vid}: [처리] 탭인데 [관리] 섹션 #{s['id']} 이 보인다(hidden={s['hid']}, display={s['disp']})"
        assert [t["sel"] for t in o["tabs1"]] == ["false", "true"], f"{label} {vid}: [관리]를 눌렀는데 안 바뀐다"
        assert o["qh1"], f"{label} {vid}: [관리] 탭인데 처리 큐가 안 접혔다"
        shown = [s for s in o["secs1"] if s["mode"] == "manage" and s["kids"]]
        assert shown, f"{label} {vid}: [관리]에 섹션이 없다"
        for s in o["secs1"]:
            if s["mode"] != "manage":
                continue
            assert not s["hid"] and s["disp"] != "none", f"{label} {vid}: [관리] 섹션 #{s['id']} 이 안 펴졌다"
            if s["disp"] != "contents":
                assert s["w"] > 0, f"{label} {vid}: [관리] 섹션 #{s['id']} 폭이 0"
        assert all(w > 0 for w in o["cv1"]), f"{label} {vid}: [관리]에서 차트 상자가 폭 0 이거나 차트가 안 섰다 {o['cv1']}"
        for s in o["secs2"]:
            if s["mode"] == "manage":
                assert s["hid"] and s["disp"] == "none", f"{label} {vid}: [처리]로 돌아왔는데 #{s['id']} 이 안 접혔다"
        assert o["jump"] == "manage", f"{label} {vid}: 띠의 버튼이 [관리] 탭을 안 열었다: {o['jump']}"
    # 박제 눈속임 방지: 탭 배지 값(count 경로)이 페이로드에서 온다 — 건수가 있으면 .cnt 가 서야 한다는 것은 test_render 가 본다.


def _run_screens() -> None:
    import test_render as tr
    browser = tr.find_browser()
    if not browser:
        print("크롬/엣지가 없어 화면 검사는 건너뜁니다")
        return
    import doctor
    import os
    for k in (doctor.MODE_ENV, doctor.TOOL_ENV, doctor.TERMINAL_ENV):
        os.environ.pop(k, None)
    home = Path(tempfile.mkdtemp(prefix="seo-miner-modes-compete-"))
    try:
        tr.fixture(home)
        import dashboard
        shell = dashboard.HTML.replace(b"</head>", tr.PROBE.encode("utf-8") + b"</head>", 1)
        local = shell.replace(b"</body>", RUN.encode("utf-8") + b"</body>", 1)
        with tr._stdlib(local) as site:
            html = tr.dom(browser, f"{site.base}/d#{tr.SITES[1]}", home / "chrome-profile")
        p = tr.probe(html)
        assert not p["err"], "로컬: JS 가 터졌다:\n  " + "\n  ".join(p["err"][:6])
        _check_screens("로컬", html)
        print("  화면(로컬): ok")
        if (ROOT / "server" / "assets" / "dash.html").exists():
            saved = tr.ALIAS_TOUCH            # 호스팅 앱 앞 층이 ?alias_probe=1 요청에 끼우는 조각을 우리 것으로 바꾼다
            tr.ALIAS_TOUCH = RUN
            try:
                with tr.serve_hosted(home / "hosted") as site:
                    html = tr.dom(browser, f"{site.base}/d?alias_probe=1#{tr.SITES[1]}", home / "chrome-profile")
                    site.after()
                    fails = site.failures()
            finally:
                tr.ALIAS_TOUCH = saved
            p = tr.probe(html)
            assert not p["err"], "호스팅: JS 가 터졌다:\n  " + "\n  ".join(p["err"][:6])
            assert not fails, "호스팅: 서버가 본 실패 " + "; ".join(fails[:5])
            _check_screens("호스팅", html)
            print("  화면(호스팅): ok")
    finally:
        import gc
        gc.collect()
        shutil.rmtree(home, ignore_errors=True)


def test_screens_in_a_real_browser():
    _run_screens()


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)} passed")
