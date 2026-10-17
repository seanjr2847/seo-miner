#!/usr/bin/env python3
"""[AI 인용]·[AI 요약] 두 화면의 [처리]·[관리] 탭 — 선언·마크업·화면 동작을 한 파일에서 못 박는다.

틀 자체(view-def modes / queue, 셸의 SM.modeApply·SM.queue)는 test_seams 170·180번대가 지킨다.
여기는 **이 두 화면이 그 틀에 제대로 올라탔는가** 만 본다:

  1. 선언  — view-def 에 modes(처리·관리, count 는 queue_open_by_view.<화면>)와 queue({mode: work}).
             큐 종류 목록은 뷰에 적지 않는다(정본 scoring.KINDS 의 see → queue_kinds_by_view).
  2. 마크업 — view-def 가 담는다고 한 모든 요소는 data-mode="manage" 인 섹션 **안에** 있다(또는 그것이다).
             셸은 data-mode 가 달린 화면 상자 직계에만 hidden 을 걸고, 섹션 안쪽의 hidden 은 뷰가 쓴다 —
             섹션마다 따로 달면 [처리]→[관리]로 돌아올 때 셸이 뷰가 숨긴 빈 카드까지 펴 버린다.
  3. CSS   — display:contents 같은 id 규칙이 hidden 을 이기면 접힌 탭이 안 접힌다. 인쇄에서는 접힌 섹션이 살아난다.
  4. 화면  — 헤드리스 브라우저로 로컬·호스팅(serve_hosted)을 열어 탭 전환, 처리 큐에서 상태를 바꾼 뒤 서버 값,
             [관리] 카드가 폭 0 으로 안 그려지는지, 열린 기회 없을 때 [관리]가 먼저 서는지.

CLAUDE.md 의 규율: 검사를 추가했으면 일부러 깨서 FAIL 이 나는 것까지 확인한다.
"""
from __future__ import annotations

import html as htmlmod
import json
import re
import shutil
import sys
import tempfile
from html.parser import HTMLParser
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
TPL = ROOT / "skills" / "capture" / "templates"
DOCS = ROOT / "docs" / "screens"
VIEWS = ("aicite", "aisum")


def _shell_src() -> str:
    """조립 전 셸 원본(dashboard.html + templates/shell/*.js 조각)."""
    import dashboard
    return dashboard.shell_source()


def _src(vid: str) -> str:
    return (TPL / "views" / f"{vid}.html").read_text("utf-8")


def _def(vid: str) -> dict:
    m = re.search(r'class="view-def">\s*(\{.*?\})\s*</script>', _src(vid), re.S)
    assert m, f"{vid}.html 에 view-def 가 없다"
    return json.loads(m.group(1))


def _section_defs(vid: str) -> list[tuple[Path, dict]]:
    """이 화면에 붙는 배포 전용 섹션(templates/sections/*.html 의 section-def)."""
    out = []
    for f in sorted((TPL / "sections").glob("*.html")):
        m = re.search(r'class="section-def">\s*(\{.*?\})\s*</script>', f.read_text("utf-8"), re.S)
        if m and json.loads(m.group(1)).get("view") == vid:
            out.append((f, json.loads(m.group(1))))
    return out


class _Anc(HTMLParser):
    """id 마다 조상(자기 포함)의 속성 목록을 모은다."""
    VOID = {"br", "hr", "img", "input", "meta", "link", "col", "source", "wbr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack: list[dict] = []
        self.chain: dict[str, list[dict]] = {}

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        a["_tag"] = tag
        if tag not in self.VOID:
            self.stack.append(a)
        if a.get("id"):
            self.chain[a["id"]] = ([*self.stack] if tag in self.VOID else [*self.stack])

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i]["_tag"] == tag:
                del self.stack[i:]
                break


def _chains(text: str) -> dict[str, list[dict]]:
    body = re.sub(r"<script[\s\S]*?</script>|<style[\s\S]*?</style>|<!--[\s\S]*?-->", "", text)
    p = _Anc()
    p.feed(body)
    return p.chain


# ───────────────────────── 1. 선언 ─────────────────────────

def test_modes_are_declared_with_work_and_manage_tabs():
    for vid in VIEWS:
        d = _def(vid)
        assert d.get("modes") == [
            {"id": "work", "label": "처리", "count": f"queue_open_by_view.{vid}"},
            {"id": "manage", "label": "관리"}], f"{vid}: modes 선언이 [처리]·[관리]가 아니다: {d.get('modes')}"
        assert d.get("queue") == {"mode": "work"}, \
            f"{vid}: queue 는 {{mode: work}} 만 — 종류 목록은 뷰에 적지 않는다(scoring.KINDS see 의 뒤집기): {d.get('queue')}"


def test_view_does_not_copy_the_kind_list():
    import scoring
    inv = scoring.queue_kinds_by_view()
    for vid in VIEWS:
        assert inv.get(vid), f"{vid} 가 맡은 기회 종류가 scoring.KINDS 의 see 에 없다 — 큐가 빈다"
        text = _src(vid)
        # 종류 이름이 뷰에 글자로 나오는 것은 판정(oppsOf 의 kind ===)·주석뿐이다. 선언 쪽에는 없어야 한다.
        head = text.split("</script>", 1)[0]
        for k in inv[vid]:
            assert k not in head, f"{vid} view-def 에 종류 {k} 가 적혔다 — queue_kinds_by_view 를 가리켜라"


# ───────────────────────── 2. 마크업 ─────────────────────────

def test_every_declared_section_sits_inside_a_manage_section():
    for vid in VIEWS:
        d = _def(vid)
        chain = _chains(_src(vid))
        for sid in d["sections"]:
            assert sid in chain, f"{vid}: view-def 가 담는다는 #{sid} 가 마크업에 없다"
            modes = [a.get("data-mode") for a in chain[sid] if a.get("data-mode")]
            assert modes == ["manage"], \
                f"{vid}: #{sid} 의 data-mode 조상은 manage 하나여야 한다(셸이 접는 단위): {modes}"
        wraps = [a for sid in d["sections"] for a in chain[sid] if a.get("data-mode")]
        assert wraps and all(a["_tag"] == "section" for a in wraps), \
            f"{vid}: 접는 단위는 <section> 이어야 한다(test_seams 170 이 section/.band 만 센다)"
    # 호스팅 전용 섹션(질문 편집기)은 화면 상자 직계라 스스로 [관리]에 선다.
    for f, sd in _section_defs("aicite"):
        a = _chains(f.read_text("utf-8"))[sd["id"]][-1]
        assert a.get("data-mode") == "manage", f"{f.name}: 화면 상자 직계 섹션은 data-mode=\"manage\" 가 있어야 한다"


def test_manage_wrapper_is_the_only_moved_element():
    """셸은 view-def 의 id 마다 closest('section') 을 상자 직계로 옮긴다. 안쪽 id 가 <section> 이면
    래퍼 밖으로 끌려 나가 data-mode 를 잃는다 — 래퍼 안 id 는 section 이 아니어야 한다."""
    for vid in VIEWS:
        d = _def(vid)
        chain = _chains(_src(vid))
        for sid in d["sections"]:
            secs = [a for a in chain[sid] if a["_tag"] == "section"]
            assert len(secs) == 1 and secs[0].get("data-mode") == "manage", \
                f"{vid}: #{sid} 의 가장 가까운 section 이 manage 래퍼 하나가 아니다 — 셸이 래퍼 밖으로 옮긴다: " \
                f"{[a.get('id') or a['_tag'] for a in secs]}"


# ───────────────────────── 3. CSS ─────────────────────────

def _css(vid: str) -> str:
    return "\n".join(re.findall(r"<style>([\s\S]*?)</style>", _src(vid)))


def test_wrapper_display_rules_do_not_beat_hidden():
    for vid in VIEWS:
        css = re.sub(r"/\*[\s\S]*?\*/", "", _css(vid))
        css = re.sub(r"@media\s+print\s*\{(?:[^{}]|\{[^{}]*\})*\}", "", css)   # 인쇄 규칙은 일부러 hidden 을 이긴다
        for sel, body in re.findall(r"([^{}@]+)\{([^{}]*)\}", css):
            if "display:contents" not in body.replace(" ", ""):
                continue
            for one in sel.split(","):
                one = one.strip()
                if re.search(rf"(?<![\w-])#{vid}\b(?![-\w])", one) and "[data-mode" not in one:
                    assert ":not([hidden])" in one, \
                        f"{vid}: `{one}` 의 display:contents 가 hidden 을 이긴다 — 접힌 [관리]가 안 접힌다"


def test_print_shows_folded_manage_section():
    for vid in VIEWS:
        css = re.sub(r"/\*[\s\S]*?\*/", "", _css(vid))
        blocks = re.findall(r"@media\s+print\s*\{((?:[^{}]|\{[^{}]*\})*)\}", css)
        assert any(f"#{vid}" in b and "[hidden]" in b and "display" in b for b in blocks), \
            f"{vid}: 인쇄에서 접힌 [관리] 섹션을 되살리는 @media print 규칙이 없다 — 종이에서 사라진다"


def test_no_important_and_no_tracking_on_korean_in_the_new_rules():
    for f in [TPL / "views" / f"{v}.html" for v in VIEWS] + [TPL / "sections" / "sm-aiq.html"]:
        assert "!important" not in f.read_text("utf-8"), f"{f.name}: !important 금지(CLAUDE.md)"


# ───────────────────────── 4. 첫 탭 ─────────────────────────

def test_default_tab_follows_open_queue_count():
    """열린 기회가 없을 때 [관리]를 앞에 세우는 일은 셸(SM.modeDefaults)이 한 벌로 한다 — 뷰가 제 사본을
    들면 화면마다 규칙이 갈린다. 배지(count)가 0 이면 그 탭은 첫 탭이 아니다."""
    shell = _shell_src()
    assert "modeDefaults" in shell, "셸에 첫 탭 규칙(modeDefaults)이 없다"
    for vid in VIEWS:
        t = _src(vid)
        assert "sm:mode:" not in t and "SM.mode." not in t, \
            f"{vid}: 첫 탭 규칙의 사본을 뷰가 들고 있다 — 셸 한 벌(SM.modeDefaults)로"


def test_go_see_opens_the_tab_of_its_target():
    """개요 줄의 [화면에서 자세히 보기](goSee)는 대상 섹션이 접힌 탭에 있으면 그 탭을 먼저 연다 —
    숨은 섹션은 스크롤할 수 없어 화면 맨 위(마지막 탭)에 서 버린다."""
    shell = _shell_src()
    i = shell.index("goSee(view, sec)")
    body = shell[i:i + 900]
    assert "SM.modeOf(el)" in body and "SM.setMode(view, mode)" in body, "goSee 가 대상 섹션의 탭을 열지 않는다"


# ───────────────────────── 5. 문서 ─────────────────────────

def test_docs_describe_the_two_tabs():
    for vid in VIEWS:
        t = (DOCS / f"{vid}.md").read_text("utf-8")
        assert "[처리]" in t and "[관리]" in t, f"docs/screens/{vid}.md 에 [처리]·[관리] 구성 설명이 없다"


# ───────────────────────── 6. 화면(브라우저) ─────────────────────────

PROBE = r"""<script>
(async function () {
  var out = {views: {}}, sleep = function (ms) { return new Promise(function (r) { setTimeout(r, ms); }); };
  var vis = function (el) { return !!el && el.getClientRects().length > 0; };
  var site = decodeURIComponent(location.hash.slice(1));
  var api = function () { return fetch("/api/data?project=" + encodeURIComponent(site)).then(function (r) { return r.json(); }); };
  try {
    await sleep(2500);
    for (var V of ["aicite", "aisum"]) {
      var o = out.views[V] = {};
      var box = document.getElementById("view-" + V);
      o.initial = SM.modeNow(V);
      o.cnt = SM.modeCount[V + ":work"] || 0;
      SM.show(V);
      var tabs = [].slice.call(box.querySelectorAll(":scope > .vtabs [role=tab]"));
      o.tabs = tabs.map(function (t) { return t.textContent.replace(/\s+/g, " ").trim(); });
      o.sel0 = tabs.map(function (t) { return t.getAttribute("aria-selected"); });
      var wrap = document.getElementById(V), host = document.getElementById("q-" + V);
      var cards = function () { return [].slice.call(box.querySelectorAll(".card")).filter(function (c) { return !host || !host.contains(c); }); };
      // [처리]
      tabs[0].click(); await sleep(150);
      o.work = {wrapDisp: getComputedStyle(wrap).display, host: vis(host), rows: host ? host.querySelectorAll(".qlist details.opp").length : -1,
                manageCards: cards().filter(vis).map(function (c) { return c.id; })};
      // [관리]
      tabs[1].click(); await sleep(150);
      var shown = cards().filter(function (c) { return !c.closest("[hidden]"); });
      o.manage = {wrapDisp: getComputedStyle(wrap).display, host: vis(host),
                  invisible: shown.filter(function (c) { return !vis(c) || c.getBoundingClientRect().width < 40; }).map(function (c) { return c.id || c.className; }),
                  leaked: cards().filter(function (c) { return c.closest("[hidden]") && vis(c); }).map(function (c) { return c.id || c.className; }),
                  nShown: shown.length,
                  svgs: shown.reduce(function (n, c) { return n + c.querySelectorAll("svg").length; }, 0),
                  zeroSvg: shown.reduce(function (l, c) { return l.concat([].slice.call(c.querySelectorAll("svg")).filter(function (v) { return v.getBoundingClientRect().width < 8; }).map(function (v) { return (c.id || c.className) + " svg"; })); }, [])};
      // 띠의 단추가 있던 자리로 가는 길
      if (V === "aisum" && typeof AS_showGap === "function") { AS_showGap(); await sleep(200); o.gapBtn = {tab: SM.modeNow(V), host: vis(host)}; tabs[1].click(); await sleep(100); }
      if (V === "aicite" && typeof AI_showMissed === "function") { tabs[0].click(); await sleep(100); AI_showMissed(); await sleep(200); o.missBtn = {tab: SM.modeNow(V), sec: vis(document.getElementById("ai-prompt-sec"))}; }
      // 다른 화면에서 이 화면 섹션으로(개요 기회 줄의 '자세히 보기') — 탭을 열어 주는지는 셸 몫이라 사실만 적는다
      SM.show("overview"); tabs[0].click(); await sleep(50);
      var see = (window.KIND_VIEWS || {}); var k = Object.keys(see).filter(function (x) { return see[x][0] === V; })[0];
      if (k) { SM.queue.goSee(see[k][0], see[k][1]); await sleep(150);
               o.goSee = {sec: see[k][1], tab: SM.modeNow(V), secVisible: vis(document.getElementById(see[k][1]))}; }
      // [처리]에서 상태 바꾸기 → 서버 값
      SM.show(V); tabs[0].click(); await sleep(150);
      var row = host && host.querySelector(".qlist details.opp");
      if (row) {
        var ids = row.dataset.ids.split(" ").map(Number);
        var before = (await api()).opps.filter(function (x) { return ids.indexOf(x.id) >= 0; }).map(function (x) { return x.status; });
        await setOpps(ids, "done"); await sleep(1800);
        var d2 = await api();
        o.status = {ids: ids, before: before, after: d2.opps.filter(function (x) { return ids.indexOf(x.id) >= 0; }).map(function (x) { return x.status; }),
                    cntAfter: SM.modeCount[V + ":work"] || 0, badgeSrv: (d2.queue_open_by_view || {})[V]};
        o.statusRowGone = !host.querySelector('.qlist [data-ids~="' + ids[0] + '"]');
      }
    }
  } catch (e) { out.err = String(e && e.stack || e); }
  var d = document.createElement("div"); d.id = "__ma__";
  d.textContent = JSON.stringify(out); document.body.appendChild(d);
})();
</script>"""


def _seed(home: Path) -> None:
    """test_render 의 픽스처에 [AI 인용] 기회를 더한다 — 그 픽스처에는 ai_citation_gap 이 없다."""
    import db
    import test_render as tr
    conn = db.connect()
    try:
        pid = conn.execute("SELECT id FROM projects WHERE name=?", (tr.SITES[1],)).fetchone()[0]
        conn.executemany(
            "INSERT INTO opportunities(project_id,kind,target,score,reasoning,status) VALUES(?,?,?,?,?,'new')",
            [(pid, "ai_citation_gap", f"{tr.SITES[1]} 질문 {i}", 60 - i, "AI 가 우리를 인용하지 않습니다") for i in range(3)])
        # 질문도 심사 대상이다(TRIAGE_KINDS) — 작업으로 보낸 것만 [처리] 큐에 선다
        import scoring
        db.set_verdicts(conn, pid, [scoring.verdict_key("ai_citation_gap", f"{tr.SITES[1]} 질문 {i}") for i in range(3)], "work")
        conn.commit()
    finally:
        conn.close()


def _read(html: str) -> dict:
    m = re.search(r'id="__ma__"[^>]*>(.*?)</div>', html, re.S)
    assert m, "탭 검사 스크립트가 결과를 못 남겼다"
    o = json.loads(htmlmod.unescape(m.group(1)))
    assert not o.get("err"), f"탭 검사 스크립트가 터졌다: {o.get('err')}"
    return o["views"]


def _assert_open_site(label: str, vs: dict) -> None:
    if "-v" in sys.argv:
        print(label, json.dumps(vs, ensure_ascii=False, indent=1))
    for V in VIEWS:
        o = vs[V]
        L = f"{label} {V}"
        assert o["tabs"][:2] and o["tabs"][0].startswith("처리") and o["tabs"][1].startswith("관리"), f"{L}: 탭 글 {o['tabs']}"
        assert o["cnt"] > 0, f"{L}: 열린 기회가 있는 사이트인데 [처리] 배지 값이 0 이다"
        assert o["initial"] == "work" and o["sel0"] == ["true", "false"], \
            f"{L}: 열린 기회가 있으면 [처리]가 먼저 서야 한다: {o['initial']} {o['sel0']}"
        w, m = o["work"], o["manage"]
        assert w["wrapDisp"] == "none", f"{L}: [처리]에서 [관리] 래퍼가 안 접혔다(display={w['wrapDisp']})"
        assert w["host"] and w["rows"] > 0, f"{L}: [처리]에 큐 카드/줄이 없다: {w}"
        assert not w["manageCards"], f"{L}: [처리]에 [관리] 카드가 새어 보인다: {w['manageCards']}"
        assert m["wrapDisp"] != "none" and not m["host"], f"{L}: [관리]에서 래퍼/큐 상태가 틀렸다: {m}"
        assert m["nShown"] > 0, f"{L}: [관리]에 보이는 카드가 하나도 없다"
        assert not m["invisible"], f"{L}: [관리]의 카드가 안 그려지거나 폭이 0 에 가깝다: {m['invisible']}"
        assert not m["leaked"], f"{L}: view 가 숨긴(hidden) 카드가 [처리]→[관리] 전환 뒤 펴졌다: {m['leaked']}"
        assert not m["zeroSvg"], f"{L}: [관리]의 그림(svg)이 폭 0 으로 그려졌다(숨은 탭에서 그려진 듯): {m['zeroSvg']}"
        st = o.get("status")
        assert st, f"{L}: [처리] 큐에 줄이 있는데 상태 바꾸기를 못 해 봤다"
        assert st["before"] and set(st["before"]) <= {"new", "acked"}, f"{L}: 바꾸기 전 상태 {st['before']}"
        assert st["after"] and set(st["after"]) == {"done"}, f"{L}: 서버 값이 안 바뀌었다: {st}"
        assert st["cntAfter"] == st["badgeSrv"] and st["cntAfter"] < o["cnt"], f"{L}: 탭 배지가 서버 값을 안 따라간다: {st} (전 {o['cnt']})"
        assert o["statusRowGone"], f"{L}: 끝낸 기회가 [처리] 큐에 남아 있다"
    assert vs["aisum"]["gapBtn"]["tab"] == "work" and vs["aisum"]["gapBtn"]["host"], \
        f"{label}: 띠의 [빠진 검색어 보기]가 [처리] 큐로 안 데려간다: {vs['aisum']['gapBtn']}"
    assert vs["aicite"]["missBtn"]["tab"] == "manage" and vs["aicite"]["missBtn"]["sec"], \
        f"{label}: 띠의 [질문 열기]가 근거 표([관리])를 안 연다: {vs['aicite']['missBtn']}"


def _assert_quiet_site(label: str, vs: dict) -> None:
    for V in VIEWS:
        o = vs[V]
        L = f"{label} {V}"
        assert o["cnt"] == 0, f"{L}: 열린 기회가 없는 사이트가 아니다({o['cnt']})"
        assert o["initial"] == "manage" and o["sel0"] == ["false", "true"], \
            f"{L}: 열린 기회가 없으면 [관리]가 먼저 서야 한다: {o['initial']} {o['sel0']}"
        assert o["manage"]["wrapDisp"] != "none" and not o["manage"]["invisible"], f"{L}: [관리] 상태 {o['manage']}"


def run() -> None:
    import test_render as tr
    browser = tr.find_browser()
    if not browser:
        print("크롬/엣지가 없어 건너뜁니다")
        return
    import os
    home = Path(tempfile.mkdtemp(prefix="seo-miner-modes-ai-"))
    saved = tr.RELOAD_TOUCH
    try:
        tr.fixture(home)
        _seed(home)
        import dashboard
        shell = dashboard.HTML.replace(b"</head>", tr.PROBE.encode("utf-8") + b"</head>", 1)
        local = shell.replace(b"</body>", PROBE.encode("utf-8") + b"</body>", 1)
        alpha, beta = tr.SITES
        prof = home / "chrome-profile"
        with tr._stdlib(local) as site:
            vs = _read(tr.dom(browser, f"{site.base}/d#{beta}", prof))
            _assert_open_site("로컬", vs)
        # 같은 픽스처를 다시 쓰려면 위에서 끝낸 기회가 되살아나야 한다 — 호스팅은 새 임시 폴더에 다시 심는다.
        for k in (alpha,):
            pass
        with tr._stdlib(local) as site:
            vs = _read(tr.dom(browser, f"{site.base}/d#{alpha}", prof))
            _assert_quiet_site("로컬", vs)
        print("  로컬: ok")
        hosted_home = home / "hosted"
        # serve_hosted 의 /d 에는 RELOAD_TOUCH 자리에만 스크립트를 끼울 수 있다 — 표식(reload_probe=1)이 켠다.
        tr.RELOAD_TOUCH = PROBE
        try:
            with tr.serve_hosted(hosted_home) as site:
                import store
                conn = store.connect()
                try:
                    uid = store.upsert_user(conn, tr.HOSTED_EMAIL)
                finally:
                    conn.close()
                _seed_hosted(store.home(uid))
                pg = tr.dom(browser, f"{site.base}/d?reload_probe=1#{beta}", prof)
                site.after()
                assert not tr.probe(pg)["err"], "호스팅: JS 가 터졌다:\n  " + "\n  ".join(tr.probe(pg)["err"][:6])
                assert not site.failures(), "호스팅: 서버가 4xx/5xx 를 냈다:\n  " + "\n  ".join(site.failures()[:8])
                _assert_open_site("호스팅", _read(pg))
                pg2 = tr.dom(browser, f"{site.base}/d?reload_probe=1#{alpha}", prof)
                _assert_quiet_site("호스팅", _read(pg2))
                assert 'id="sm-aiq"' in re.sub(r"<script[\s\S]*?</script>", "", pg), "호스팅: 질문 편집기(sm-aiq)가 [관리]에 안 섰다"
        finally:
            tr.RELOAD_TOUCH = saved
        print("  호스팅: ok")
    finally:
        tr.RELOAD_TOUCH = saved
        import gc
        gc.collect()
        shutil.rmtree(home, ignore_errors=True)


def _seed_hosted(uid_home: Path) -> None:
    """호스팅 사용자의 Brain(픽스처는 이미 심었다)에 [AI 인용] 기회를 더한다."""
    import os
    saved = os.environ.get("CAPTURE_HOME")
    os.environ["CAPTURE_HOME"] = str(uid_home)
    try:
        _seed(uid_home)
    finally:
        if saved is None:
            os.environ.pop("CAPTURE_HOME", None)
        else:
            os.environ["CAPTURE_HOME"] = saved


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
    print("modes_ai self-check ok")
