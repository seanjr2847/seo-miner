#!/usr/bin/env python3
"""[설정] 화면의 탭 넷 — 셸의 화면 안 탭 틀(view-def modes)에 올라탔는지 한 파일에서 못 박는다.

틀 자체(modes 의 모양·SM.modeApply·tablist·방향키·기억)는 test_seams 170 이 지킨다.
여기는 **설정이 자기 탭을 따로 짓지 않고 그 틀 하나를 쓰는가**, 그리고 설정 고유 동작이 틀 위에서
살아 있는가를 본다:

  1. 선언  — view-def modes 넷(측정 설정·연결·이 PC·계정). 로컬·호스팅 전용으로 비는 탭은 hide_empty.
  2. 마크업 — 카드는 data-mode 로 소속한다. 옛 자체 탭(#st-tabs·data-st-tab·data-tab)은 없다.
  3. 셸    — 깊은 data-mode(섹션 안 카드)·탭 점(setModeDot)·비는 탭 숨김(hide_empty)이 셸에 한 벌.
  4. CSS   — id 규칙의 display 가 접기를 이기지 않는다. !important 금지.
  5. 문서
  6. 화면  — 헤드리스 브라우저로 로컬·호스팅(serve_hosted)·박제본: 탭 전환, 점, 처음 탭, 상태 행 클릭이 탭을
             여는지, 호스팅에서 [이 PC] 가 없는지(로컬은 [계정] 이 없는지), 방향키가 숨은 탭을 건너뛰는지,
             칩 입력 저장, 박제본에 설정이 없어도 JS 가 안 터지는지.

CLAUDE.md 의 규율: 검사를 추가했으면 일부러 깨서 FAIL 이 나는 것까지 확인한다.
"""
from __future__ import annotations

import html as htmlmod
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
TPL = ROOT / "skills" / "capture" / "templates"
DOCS = ROOT / "docs" / "screens"

# 탭 이름표와 순서의 정본은 settings.html 의 view-def 한 곳이다. 이 파일은 그것을 **한 번** 못 박는다
# (카드가 어느 탭이어야 하는지는 마크업이, 탭이 몇 개인지는 선언이 말한다 — 둘이 어긋나면 여기서 걸린다).
TABS = [("measure", "측정 설정"), ("link", "연결"), ("pc", "이 PC"), ("acct", "계정")]
SETTINGS_FILES = ("views/settings.html", "sections/sm-profile.html", "sections/sm-lang.html", "sections/sm-set.html")


def _read(rel: str) -> str:
    return (TPL / rel).read_text("utf-8")


def _def() -> dict:
    m = re.search(r'class="view-def">\s*(\{.*?\})\s*</script>', _read("views/settings.html"), re.S)
    assert m, "settings.html 에 view-def 가 없다"
    return json.loads(m.group(1))


def _shell() -> str:
    import dashboard
    return dashboard.shell_source()


# ───────────────────────── 1. 선언 ─────────────────────────

def test_settings_declares_four_modes():
    ms = _def().get("modes")
    assert ms, "설정의 탭 넷이 view-def modes 로 선언되지 않았다"
    assert [(m["id"], m["label"]) for m in ms] == TABS, f"설정 탭 선언이 바뀌었다: {ms}"
    assert all("count" not in m for m in ms), "설정 탭은 페이로드 건수 배지가 아니라 점(할 일 남음)을 쓴다"
    # 로컬 전용(#setup)·호스팅 전용(#sm-set)으로 비는 탭은 선언한 쪽이 말한다
    assert {m["id"] for m in ms if m.get("hide_empty")} == {"link", "pc", "acct"}, \
        f"비면 접는 탭(hide_empty) 선언이 다르다: {ms}"
    assert "queue" not in _def(), "설정에는 처리 큐가 없다"


# ───────────────────────── 2. 마크업 ─────────────────────────

def test_old_tab_implementation_is_gone():
    st = _read("views/settings.html")
    for pat in ("st-tabs", "data-st-tab", "ST_tabLive", "function ST_tab(", "ST_tabOf", "stTab", "stAuto", "tdot"):
        assert pat not in st, f"settings.html 에 옛 자체 탭 구현({pat})이 남았다 — 탭 틀은 셸 한 벌"
    for rel in SETTINGS_FILES:
        assert "data-tab=" not in _read(rel), f"{rel} 에 옛 data-tab 소속이 남았다 — data-mode 로"


def test_every_card_belongs_to_a_declared_tab_and_every_tab_has_cards():
    ids = {t for t, _ in TABS}
    seen: dict[str, set] = {t: set() for t in ids}
    for rel in SETTINGS_FILES:
        for tag, attrs, mode in re.findall(r'<(\w+)\b([^>]*?)\bdata-mode="(\w+)"', _read(rel)):
            assert mode in ids, f"{rel} 의 data-mode={mode!r} 가 설정 탭 선언에 없다 — 그 카드는 영영 안 보인다"
            assert tag == "section" or "card" in attrs, f"{rel}: <{tag}> 에 data-mode — 패널은 섹션이나 카드다"
            seen[mode].add(rel)
    for t, files in seen.items():
        assert files, f"설정 탭 {t} 에 카드가 하나도 없다"
    # 한 탭에 두 배포가 나눠 서는 자리: 연결 탭은 로컬(#setup)과 호스팅(#sm-set) 둘 다에 카드가 있다
    assert seen["link"] >= {"views/settings.html", "sections/sm-set.html"}, seen["link"]
    assert seen["pc"] == {"views/settings.html"} and seen["acct"] == {"sections/sm-set.html"}, seen


def test_settings_has_no_own_tab_hidden_logic_in_dash_addon():
    dash = (ROOT / "server" / "assets" / "dash.html").read_text("utf-8")
    for pat in ("vtabs", "tablist", "data-mode", "setMode"):
        assert pat not in dash, f"호스팅 애드온이 {pat!r} 로 탭을 따로 만진다 — 셸 한 벌"


# ───────────────────────── 3. 셸 ─────────────────────────

def test_shell_owns_deep_panels_dots_and_empty_tabs():
    sh = _shell()
    for needle, why in (("modePanels(", "섹션 안 카드(깊은 data-mode)를 접는 패널 목록"),
                        ("modeOf(", "요소가 속한 탭을 묻는 길(goSee·설정의 이동이 쓴다)"),
                        ("setModeDot(", "탭 점(할 일 남음)"),
                        ("hide_empty", "비는 탭 숨김 선언"),
                        ('"sm:mode"', "사람이 탭을 골랐다는 알림")):
        assert needle in sh, f"셸에 {why}가 없다 ({needle})"
    i = sh.index("goSee(view, sec)")
    body = sh[i:i + 1100]
    assert "SM.modeOf(el)" in body and "SM.setMode(view," in body, "goSee 가 깊은 카드의 탭도 열지 않는다"
    assert 'closest("#view-" + view + " > [data-mode]")' not in body, "goSee 가 옛 직계 전용 탐색을 쓴다"


def test_shell_deep_panel_rule_does_not_touch_other_data_mode():
    """남의 data-mode(예: [관찰 중]의 정렬 칩 `button.chip[data-mode]`)는 패널이 아니다 —
    패널 판정은 한 함수(isPanel)가 하고, 직계가 아니면 section·.card·.band 만 센다."""
    sh = _shell()
    m = re.search(r"isPanel\(el\)\s*\{(.*?)\n  \},", sh, re.S)
    assert m, "셸에 패널 판정(isPanel)이 없다"
    body = m.group(1)
    assert 'matches("section, .card, .band")' in body and 'classList.contains("view")' in body, body
    hold = _read("views/hold.html")
    chip = re.search(r"<(\w+)\b[^>]*data-hd-sort[^>]*data-mode=", hold)
    assert chip and chip.group(1) == "button", "hold.html 의 정렬 칩이 button 이 아니다 — 패널 판정과 겹친다"


def test_deep_panel_fold_does_not_use_hidden():
    """섹션 안 카드는 다른 코드가 스스로 hidden 을 쓴다(호스팅 선택이면 등록·구글·키 카드를 감춘다).
    셸이 그 위에 hidden 을 쓰면 접었다 펼 때 남이 숨긴 카드를 펴 버린다 — 깊은 패널은 속성으로만 접는다."""
    sh = _shell()
    assert re.search(r"\.view \[data-mode\]\[data-vm-off\]:not\(\[hidden\]\)\s*\{\s*display:none", sh), \
        "깊은 패널을 접는 CSS(.view [data-mode][data-vm-off])가 없다"
    m = re.search(r"modeApply\(id, focus\) \{(.*?)\n  \},", sh, re.S)
    assert m and "direct" in m.group(1), "modeApply 가 직계(hidden)와 깊은 것(속성)을 가르지 않는다"


# ───────────────────────── 4. CSS ─────────────────────────

def test_no_important_and_no_display_rule_that_beats_the_fold():
    for rel in SETTINGS_FILES:
        assert "!important" not in _read(rel), f"{rel}: !important 금지(CLAUDE.md)"
    css = re.sub(r"/\*[\s\S]*?\*/", "", "\n".join(re.findall(r"<style>([\s\S]*?)</style>", _read("views/settings.html")
                                                           + _read("sections/sm-lang.html"))))
    for sel, body in re.findall(r"([^{}@]+)\{([^{}]*)\}", css):
        if not re.search(r"(?<![-\w])display\s*:", body):
            continue
        for one in (s.strip() for s in sel.split(",")):
            # 패널 id(카드·sm-lang)에 직접 거는 display 는 접기(display:none)를 이긴다
            assert not re.fullmatch(r"(?:#view-settings\s+)?#(sm-lang|set-cycle|set-type|set-account|usage|st-dirs|st-parts|st-gsc|st-keys|st-carry|st-project)", one) \
                or "display:none" in body.replace(" ", ""), \
                f"`{one}` 의 display 가 접힌 탭을 안 접히게 만든다: {body.strip()}"


# ───────────────────────── 5. 문서 ─────────────────────────

def test_docs_describe_tabs_once():
    st = (DOCS / "settings.md").read_text("utf-8")
    assert all(label in st for _, label in TABS), "docs/screens/settings.md 에 탭 넷 설명이 없다"
    rd = (DOCS / "README.md").read_text("utf-8")
    assert "hide_empty" in rd and "setModeDot" in rd, "README 의 modes 단락이 점·비는 탭·깊은 패널 규칙을 안 적었다"
    assert "별개입니다" not in rd.split("## 처리 큐", 1)[0], "README 가 설정 탭이 이 틀과 별개라고 말한다 — 이제 한 틀이다"


# ───────────────────────── 6. 화면(브라우저) ─────────────────────────

PROBE = r"""<script>
(async function () {
  var out = {}, sleep = function (ms) { return new Promise(function (r) { setTimeout(r, ms); }); };
  var vis = function (el) { return !!el && el.getClientRects().length > 0; };
  var $ = function (i) { return document.getElementById(i); };
  try {
    await sleep(3000);
    var inSM = SM.at("settings") >= 0;
    out.inSM = inSM;
    if (inSM) {
      SM.show("settings");
      await sleep(300);
      var box = $("view-settings");
      var bar = box.querySelector(":scope > .vtabs");
      var tabs = function () { return [].slice.call(box.querySelectorAll(":scope > .vtabs [role=tab]")); };
      var panels = function () { return [].slice.call(box.querySelectorAll("[data-mode]")).filter(function (e) { return !e.closest(".vtabs"); }); };
      var snap = function () {
        var now = SM.modeNow("settings");
        return {now: now,
          tabs: tabs().map(function (t) { return {id: t.dataset.mode, hidden: t.hidden, sel: t.getAttribute("aria-selected"),
                                                   dot: !!t.querySelector(".tdot"), label: (t.getAttribute("aria-label") || "")}; }),
          shown: panels().filter(vis).map(function (e) { return {id: e.id || e.className, mode: e.dataset.mode}; })};
      };
      out.bar = !!bar; out.role = bar && bar.getAttribute("role");
      out.own = !!$("st-tabs");
      // 처음 탭 — 사람이 아무것도 안 눌렀다. 기대값은 준비 상태 모델의 첫 할 일 칸이 속한 탭.
      var first = ST_paint.first && ST_paint.first.jump;
      var where = function (j) { return j && (j.ids ? j.ids.map($).find(Boolean) : (document.querySelector("#sm-set") || document).querySelector("." + j.jump)); };
      out.expect = (first && where(first) && SM.modeOf(where(first))) || "measure";
      out.firstJump = first ? JSON.stringify(first) : "";
      out.ready = !!ST_paint.model && !ST_paint.model.dead && !ST_paint.model.rows.some(function (r) { return r.st === "wait"; });
      out.init = snap();
      // 점 — 필수 칸이 todo 인 탭에 찍힌다
      var todo = {};
      (ST_paint.model ? ST_paint.model.rows : []).forEach(function (r) {
        if (r.req && r.st === "todo" && r.jump) { var e = where(r.jump); var t = e && SM.modeOf(e); if (t) todo[t] = 1; } });
      out.todo = Object.keys(todo).sort();
      out.dots = snap().tabs.filter(function (t) { return t.dot; }).map(function (t) { return t.id; }).sort();
      // 탭마다 눌러 본다 — 그 탭의 카드만 서고, 접힌 탭의 카드는 하나도 새지 않는다
      out.per = {};
      var live = tabs().filter(function (t) { return !t.hidden; });
      for (var t of live) { t.click(); await sleep(120); var s = snap();
        out.per[t.dataset.mode] = {now: s.now, shown: s.shown.length, leaked: s.shown.filter(function (x) { return x.mode !== t.dataset.mode; }).map(function (x) { return x.id; }),
                                   sel: s.tabs.filter(function (x) { return x.sel === "true"; }).map(function (x) { return x.id; }) }; }
      // 방향키 — 숨은 탭을 건너뛴다
      live[0].click(); await sleep(80);
      var seq = [live[0].dataset.mode];
      for (var i = 0; i < live.length; i++) {
        tabs().find(function (x) { return x.getAttribute("aria-selected") === "true"; }).dispatchEvent(new KeyboardEvent("keydown", {key: "ArrowRight", bubbles: true}));
        await sleep(80); seq.push(SM.modeNow("settings")); }
      out.seq = seq;
      tabs().find(function (x) { return x.getAttribute("aria-selected") === "true"; }).dispatchEvent(new KeyboardEvent("keydown", {key: "End", bubbles: true}));
      await sleep(80); out.end = SM.modeNow("settings");
      // 상태 행·GA4 칸 이동 — 다른 탭에 서 있다가 눌러서 대상 카드의 탭이 열리는가
      out.rows = [];
      var btns = [].slice.call(document.querySelectorAll("#st-rows button.st-row"));
      for (var b of btns) {
        var dst = b.dataset.stGo ? b.dataset.stGo.split(" ").map($).find(function (e) { return e && !e.hidden; })
                                 : (document.querySelector("#sm-set") || document).querySelector("." + b.dataset.smJump);
        var want = dst && SM.modeOf(dst);
        if (!want) { out.rows.push({name: b.textContent.trim().slice(0, 12), want: null}); continue; }
        var other = live.map(function (t) { return t.dataset.mode; }).find(function (m) { return m !== want; });
        SM.setMode("settings", other); await sleep(80);
        b.click(); await sleep(250);
        out.rows.push({name: b.textContent.trim().slice(0, 12), want: want, got: SM.modeNow("settings"), vis: vis(dst)});
      }
      // 사람이 눌렀다 — 이후 준비 상태가 바뀌어도 자동 이동은 없다(sessionStorage 에 남는다)
      out.stored = sessionStorage.getItem("sm:mode:settings");
      // 칩 입력 저장 — 측정 설정 탭의 브랜드 표기
      var mt = live[0]; SM.setMode("settings", "measure"); await sleep(120);
      var inp = $("pf-alias");
      if (inp && vis(inp)) {
        var n0 = document.querySelectorAll('.tgbox[data-tg="alias"] .tg').length;
        inp.focus(); inp.value = "탭검사브랜드";
        inp.dispatchEvent(new KeyboardEvent("keydown", {key: "Enter", bubbles: true, cancelable: true}));
        await sleep(1800);
        var chips = [].slice.call(document.querySelectorAll('.tgbox[data-tg="alias"] .tg')).map(function (c) { return c.textContent; });
        var site = $("proj") && $("proj").value;
        var srv = await (await fetch("/api/settings?project=" + encodeURIComponent(site))).json();
        out.chip = {n0: n0, n1: chips.length, hasChip: chips.join("|").indexOf("탭검사브랜드") >= 0, saved: String(srv.brand_aliases || ""), msg: ($("pf-alias-m") || {}).textContent};
      }
    }
  } catch (e) { out.err = String(e && e.stack || e); }
  var d = document.createElement("div"); d.id = "__ms__";
  d.textContent = JSON.stringify(out); document.body.appendChild(d);
})();
</script>"""


def _out(html: str) -> dict:
    m = re.search(r'id="__ms__"[^>]*>(.*?)</div>', html, re.S)
    assert m, "설정 탭 검사 스크립트가 결과를 못 남겼다"
    o = json.loads(htmlmod.unescape(m.group(1)))
    assert not o.get("err"), f"설정 탭 검사 스크립트가 터졌다: {o.get('err')}"
    return o


def _check(label: str, o: dict, *, gone: str, kept: list[str], chip: bool) -> None:
    """gone: 이 배포에서 탭 줄에 없어야 하는 탭. kept: 있어야 하는 탭(순서대로)."""
    if "-v" in sys.argv:
        print(label, json.dumps(o, ensure_ascii=False, indent=1))
    assert o["inSM"], f"{label}: 설정이 셸 화면 목록에 없다"
    assert o["bar"] and o["role"] == "tablist", f"{label}: 셸 탭 줄(tablist)이 안 섰다"
    assert not o["own"], f"{label}: 옛 자체 탭 줄(#st-tabs)이 남았다"
    ids = [t["id"] for t in o["init"]["tabs"]]
    assert ids == [t for t, _ in TABS], f"{label}: 탭 버튼이 선언과 다르다: {ids}"
    hidden = [t["id"] for t in o["init"]["tabs"] if t["hidden"]]
    assert hidden == [gone], f"{label}: 숨은 탭은 [{gone}] 하나여야 한다: {hidden}"
    live = [t["id"] for t in o["init"]["tabs"] if not t["hidden"]]
    assert live == kept, f"{label}: 서야 할 탭 {kept} ≠ {live}"
    # 처음 탭 — 첫 할 일 칸의 탭(없으면 측정 설정). 준비가 끝난 뒤라 이 값이어야 한다.
    assert o["ready"], f"{label}: 준비 상태가 읽히지 않았다(검사 환경)"
    exp = o["expect"] if o["expect"] in kept else kept[0]
    assert o["init"]["now"] == exp, f"{label}: 처음 탭이 {o['init']['now']} — 첫 할 일 칸의 탭({exp})이어야 한다 {o['firstJump']}"
    # 점
    assert o["dots"] == [t for t in o["todo"] if t in kept], f"{label}: 탭 점 {o['dots']} ≠ 할 일이 남은 탭 {o['todo']}"
    # 탭마다 그 탭 카드만
    assert sorted(o["per"]) == sorted(kept), f"{label}: 눌러 본 탭 {sorted(o['per'])}"
    for t, p in o["per"].items():
        assert p["now"] == t and p["sel"] == [t], f"{label}: [{t}] 를 눌렀는데 선택이 {p}"
        assert p["shown"] > 0, f"{label}: [{t}] 탭에 보이는 카드가 하나도 없다"
        assert not p["leaked"], f"{label}: [{t}] 에 다른 탭 카드가 새어 보인다: {p['leaked']}"
    # 방향키
    assert o["seq"][:len(kept)] == kept[:1] + kept[1:] and o["seq"][-1] == kept[0], \
        f"{label}: → 키가 숨은 탭을 건너뛰며 한 바퀴 돌지 않는다: {o['seq']}"
    assert o["end"] == kept[-1], f"{label}: End 키가 마지막 보이는 탭이 아니다: {o['end']}"
    # 상태 행
    went = [r for r in o["rows"] if r.get("want")]
    assert went, f"{label}: 눌러 볼 상태 행이 없다"
    for r in went:
        assert r["got"] == r["want"], f"{label}: 상태 행 [{r['name']}] 이 대상 카드의 탭({r['want']})을 안 연다: {r}"
    assert o["stored"], f"{label}: 사람이 고른 탭이 sessionStorage 에 안 남았다"
    if chip:
        c = o.get("chip")
        assert c, f"{label}: 측정 설정 탭의 칩 입력이 안 보인다"
        assert c["hasChip"] and c["n1"] == c["n0"] + 1, f"{label}: 칩이 안 들어갔다: {c}"
        assert "탭검사브랜드" in c["saved"], f"{label}: 칩이 서버에 저장되지 않았다: {c}"


def run() -> None:
    import test_render as tr
    browser = tr.find_browser()
    if not browser:
        print("크롬/엣지가 없어 건너뜁니다")
        return
    import os
    import doctor
    for k in (doctor.MODE_ENV, doctor.TOOL_ENV, doctor.TERMINAL_ENV):
        os.environ.pop(k, None)
    home = Path(tempfile.mkdtemp(prefix="seo-miner-modes-settings-"))
    saved = tr.RELOAD_TOUCH
    try:
        tr.fixture(home)
        import dashboard
        shell = dashboard.HTML.replace(b"</head>", tr.PROBE.encode("utf-8") + b"</head>", 1)
        local = shell.replace(b"</body>", PROBE.encode("utf-8") + b"</body>", 1)
        beta = tr.SITES[1]
        prof = home / "chrome-profile"
        with tr._stdlib(local) as site:
            pg = tr.dom(browser, f"{site.base}/d#{beta}", prof)
            assert not tr.probe(pg)["err"], "로컬: JS 가 터졌다:\n  " + "\n  ".join(tr.probe(pg)["err"][:6])
            _check("로컬", _out(pg), gone="acct", kept=["measure", "link", "pc"], chip=True)
        print("  로컬: ok")
        # 박제본 — 설정 화면이 없다. 스크립트만 남아도 안 터지고, 셸 목록에 설정이 없어야 한다.
        report = dashboard.export(beta).read_bytes().replace(b"</head>", tr.PROBE.encode("utf-8") + b"</head>", 1)
        report = report.replace(b"</body>", PROBE.encode("utf-8") + b"</body>", 1)
        with tr._stdlib(report) as site:
            pg = tr.dom(browser, f"{site.base}/d#{beta}", prof)
            assert not tr.probe(pg)["err"], "박제본: JS 가 터졌다:\n  " + "\n  ".join(tr.probe(pg)["err"][:6])
            o = _out(pg)
            assert o["inSM"] is False, "박제본 셸 화면 목록에 설정이 남았다 — 남한테 보내는 파일이다"
            assert 'id="view-settings"' not in re.sub(r"<script[\s\S]*?</script>", "", pg), "박제본에 [설정] 화면이 남았다"
        print("  박제본: ok")
        tr.RELOAD_TOUCH = PROBE
        try:
            with tr.serve_hosted(home / "hosted") as site:
                pg = tr.dom(browser, f"{site.base}/d?reload_probe=1#{beta}", prof)
                site.after()
                assert not tr.probe(pg)["err"], "호스팅: JS 가 터졌다:\n  " + "\n  ".join(tr.probe(pg)["err"][:6])
                # 설정을 열면 GA4 속성 목록을 부르는데, 픽스처에는 구글 연결이 없어 403 이다(설정이 이 사실을 화면에 말한다)
                bad = [f for f in site.failures() if "/api/ga4/properties" not in f]
                assert not bad, "호스팅: 서버가 4xx/5xx 를 냈다:\n  " + "\n  ".join(bad[:8])
                _check("호스팅", _out(pg), gone="pc", kept=["measure", "link", "acct"], chip=True)
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
    print("modes_settings self-check ok")
