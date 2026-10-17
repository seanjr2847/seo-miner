#!/usr/bin/env python3
"""기회 종류 ↔ 화면 배정 — 어느 종류도 [개요]에만 갇혀 있지 않고, 각 업무가 자기 화면의 [처리] 큐에 선다.

정본은 scoring.KINDS 의 see=(화면 id, 섹션 id) 한 벌이고 화면 큐 소속은 그 뒤집기(queue_kinds_by_view)다.
여기에 종류↔화면 표를 또 적지 않는다 — 이 파일이 못 박는 것은 **배정이 빠짐없고 서로 맞물린다**는 것뿐이다:

  1. 빠짐 — see 가 없는 종류는 의도한 예외(NO_HOME) 밖에 없다. 새 종류를 만들고 see 를 잊으면
             그 종류는 [개요]에서만 처리되고 자기 업무 화면은 모른다.
  2. 맞물림 — see 의 화면은 큐(view-def queue)를 선언하고, 섹션은 그 화면 마크업에 있으며 [관리] 탭 안에 있다
             (개요 줄의 '자세히 보기'(goSee)가 탭을 열고 스크롤하려면 섹션이 탭 래퍼 안에 있어야 한다).
  3. 합집합 — 화면 큐들의 종류 합집합 + 예외 = 개요(모든 종류)의 종류. 한 종류가 두 화면 큐에 서지 않는다.
  4. 사본 금지 — 뷰 view-def 에 종류 이름을 적지 않는다.
  5. 화면(브라우저) — 배정된 종류의 기회가 그 화면 [처리] 큐에 서고, 개요 줄의 '자세히 보기'가 그 화면·탭·섹션으로
             가고, 상태를 바꾸면 서버 값과 탭 배지가 따라간다. 로컬·호스팅(serve_hosted) 둘 다.

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

# 따로 볼 화면이 없어 [개요] 큐에만 서도 되는 종류 — 의도한 예외다. 새 종류가 여기 들어오려면 이유가 있어야 한다.
NO_HOME: frozenset[str] = frozenset()

# 이 파일이 브라우저로 확인하는 종류(= 이 배정 작업에서 새로 see 를 얻은 것). 어느 화면인지는 적지 않는다 —
# scoring.KINDS 의 see 에서 읽는다.
CHECK_KINDS = ("cannibalization", "intent_split", "coverage", "pseo_pattern", "ai_bot_blocked")


def _view_def(vid: str) -> dict | None:
    t = (TPL / "views" / f"{vid}.html").read_text("utf-8")
    m = re.search(r'class="view-def">\s*(\{.*?\})\s*</script>', t, re.S)
    return json.loads(m.group(1)) if m else None


def _all_defs() -> dict[str, dict]:
    out = {}
    for f in sorted((TPL / "views").glob("*.html")):
        d = _view_def(f.stem)
        if d:
            out[d["id"]] = d
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
            self.chain[a["id"]] = [*self.stack]

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i]["_tag"] == tag:
                del self.stack[i:]
                break


def _chains(vid: str) -> dict[str, list[dict]]:
    text = (TPL / "views" / f"{vid}.html").read_text("utf-8")
    body = re.sub(r"<script[\s\S]*?</script>|<style[\s\S]*?</style>|<!--[\s\S]*?-->", "", text)
    p = _Anc()
    p.feed(body)
    return p.chain


# ───────────────────────── 1~4. 정적 ─────────────────────────

def test_every_kind_has_a_home_screen_except_the_intended():
    import scoring
    homeless = {k.name for k in scoring.KINDS if not k.see}
    assert homeless == set(NO_HOME), \
        f"see 가 없는 종류가 의도한 예외와 다르다 — 있는데 예외 아님: {sorted(homeless - NO_HOME)}, " \
        f"예외인데 see 가 생김: {sorted(NO_HOME - homeless)}. [개요]에서만 처리되는 종류를 만들지 않는다"


def test_check_kinds_are_assigned():
    import scoring
    by = {k.name: k for k in scoring.KINDS}
    for name in CHECK_KINDS:
        assert name in by, f"{name} 이 scoring.KINDS 에 없다"
        assert by[name].see, f"{name} 에 see 가 없다 — 어느 화면 큐에도 안 든다"


def test_see_screen_declares_a_queue_and_section_sits_in_a_tab():
    import scoring
    defs = _all_defs()
    for k in scoring.KINDS:
        if not k.see:
            continue
        view, sec = k.see
        assert view in defs, f"{k.name}: see 화면 {view!r} 이 없다"
        q = defs[view].get("queue")
        assert q is not None and q.get("kinds", "see") == "see", \
            f"{k.name}: 화면 {view} 가 see 큐({{}} 또는 kinds=\"see\")를 선언하지 않았다 — 종류가 어느 큐에도 안 선다: {q}"
        chain = _chains(view)
        assert sec in chain, f"{k.name}: {view}.html 마크업에 섹션 id {sec!r} 가 없다 — 자세히 보기가 화면 맨 위로 떨어진다"
        modes = defs[view].get("modes")
        if modes:
            tabs = [a.get("data-mode") for a in chain[sec] if a.get("data-mode")]
            assert len(tabs) == 1 and tabs[0] in [m["id"] for m in modes], \
                f"{k.name}: #{sec} 가 {view} 의 탭 래퍼(data-mode) 안에 없다 — goSee 가 탭을 못 연다: {tabs}"
            # 큐는 [처리] 탭에 서므로, 이 종류의 근거 섹션이 같은 탭이면 서랍과 섹션이 한 탭에 겹친다.
            assert tabs[0] != (q or {}).get("mode"), \
                f"{k.name}: #{sec} 가 큐와 같은 탭({tabs[0]})에 있다 — '자세히 보기'가 갈 데가 없다"


def test_queue_union_matches_overview():
    import scoring
    inv = scoring.queue_kinds_by_view()
    homed = [k for ks in inv.values() for k in ks]
    assert len(homed) == len(set(homed)), f"한 종류가 두 화면 큐에 선다: {sorted(k for k in set(homed) if homed.count(k) > 1)}"
    assert set(homed) | set(NO_HOME) == set(scoring.ALL_KINDS), \
        f"화면 큐 합집합 + 예외가 개요의 종류 전체와 다르다: 빠진 것 {sorted(set(scoring.ALL_KINDS) - set(homed) - NO_HOME)}, " \
        f"모르는 것 {sorted(set(homed) - set(scoring.ALL_KINDS))}"
    defs = _all_defs()
    assert defs["overview"].get("queue") == {"kinds": "*"}, "[개요]는 모든 종류를 받는 큐여야 한다"
    assert set(inv) <= {v for v, d in defs.items() if d.get("queue") is not None}, \
        f"종류가 배정됐는데 큐를 선언하지 않은 화면: {sorted(set(inv) - {v for v, d in defs.items() if d.get('queue') is not None})}"


def test_views_do_not_copy_the_kind_list():
    import scoring
    for vid, d in _all_defs().items():
        if vid == "overview":
            continue
        head = (TPL / "views" / f"{vid}.html").read_text("utf-8").split("</script>", 1)[0]
        for name in scoring.ALL_KINDS:
            assert name not in head, f"{vid} view-def 에 종류 {name} 가 적혔다 — queue_kinds_by_view 를 가리켜라(사본 금지)"


def test_every_assigned_screen_doc_mentions_its_queue_kinds_by_label():
    """화면 문서는 개수·목록을 나열하지 않는다 — 다만 이 화면 큐가 맡은 종류의 **라벨**이 문서에 한 번은 나와
    읽는 사람이 '이 화면에서 무엇을 처리하나'를 안다(라벨의 정본은 scoring.KINDS)."""
    import scoring
    inv = scoring.queue_kinds_by_view()
    for vid, kinds in inv.items():
        f = DOCS / f"{vid}.md"
        if not f.exists():
            continue
        t = f.read_text("utf-8")
        for name in kinds:
            if name in CHECK_KINDS:
                label = scoring.kind_label(name)
                assert label in t, f"docs/screens/{vid}.md 에 {name} 의 라벨 「{label}」 이 없다 — 이 화면이 그 업무도 처리한다고 적는다"


# ───────────────────────── 5. 화면(브라우저) ─────────────────────────

PROBE = r"""<script>
(async function () {
  var out = {kinds: {}}, sleep = function (ms) { return new Promise(function (r) { setTimeout(r, ms); }); };
  var vis = function (el) { return !!el && el.getClientRects().length > 0; };
  var site = decodeURIComponent(location.hash.slice(1));
  var api = function () { return fetch("/api/data?project=" + encodeURIComponent(site)).then(function (r) { return r.json(); }); };
  try {
    await sleep(2500);
    var d0 = await api();
    out.badge0 = d0.queue_open_by_view || {};
    out.inv = d0.queue_kinds_by_view || {};
    var see = window.KIND_VIEWS || {}, NEEDLE = %NEEDLES%;
    out.see = see;
    for (var K of %KINDS%) {
      var o = out.kinds[K] = {}, sv = see[K];
      if (!sv) { o.noSee = true; continue; }
      var V = sv[0], box = document.getElementById("view-" + V), host = null;
      SM.show(V);
      var tabs = [].slice.call(box.querySelectorAll(":scope > .vtabs [role=tab]"));
      tabs[0].click(); await sleep(150);
      host = document.getElementById("q-" + V);
      var rows = [].slice.call(host.querySelectorAll(".qlist details.opp"));
      o.rows = rows.length;
      o.kindRows = rows.filter(function (r) { return (r.textContent || "").indexOf(NEEDLE[K]) >= 0; }).length;
      // 개요 줄 → 자세히 보기
      SM.show("overview"); await sleep(60);
      SM.queue.goSee(sv[0], sv[1]); await sleep(200);
      var el = document.getElementById(sv[1]);
      o.goSee = {view: V, sec: sv[1], tab: SM.modeNow(V), exists: !!el, visible: vis(el),
                 top: el ? Math.round(el.getBoundingClientRect().top) : null};
    }
  } catch (e) { out.err = String(e && e.stack || e); }
  var d = document.createElement("div"); d.id = "__mas__";
  d.textContent = JSON.stringify(out); document.body.appendChild(d);
})();
</script>"""

# 상태 바꾸기는 종류마다 따로 하지 않고 한 번만 — 화면은 같은 큐 부품이다. 첫 종류의 첫 줄을 끝낸다.
STATUS = r"""<script>
(async function () {
  var out = {}, sleep = function (ms) { return new Promise(function (r) { setTimeout(r, ms); }); };
  var site = decodeURIComponent(location.hash.slice(1));
  var api = function () { return fetch("/api/data?project=" + encodeURIComponent(site)).then(function (r) { return r.json(); }); };
  try {
    await sleep(2500);
    var V = "%VIEW%", K = "%KIND%";
    SM.show(V);
    var box = document.getElementById("view-" + V), host = document.getElementById("q-" + V);
    [].slice.call(box.querySelectorAll(":scope > .vtabs [role=tab]"))[0].click(); await sleep(150);
    var row = [].slice.call(host.querySelectorAll(".qlist details.opp")).filter(function (r) { return (r.textContent || "").indexOf("%NEEDLE%") >= 0; })[0];
    out.hadRow = !!row;
    if (row) {
      var ids = row.dataset.ids.split(" ").map(Number);
      var d1 = await api();
      out.badgeBefore = (d1.queue_open_by_view || {})[V];
      out.before = d1.opps.filter(function (x) { return ids.indexOf(x.id) >= 0; }).map(function (x) { return x.status; });
      await setOpps(ids, "done"); await sleep(1800);
      var d2 = await api();
      out.after = d2.opps.filter(function (x) { return ids.indexOf(x.id) >= 0; }).map(function (x) { return x.status; });
      out.badgeAfter = (d2.queue_open_by_view || {})[V];
      out.shellBadge = SM.modeCount[V + ":work"] || 0;
      out.gone = !host.querySelector('.qlist [data-ids~="' + ids[0] + '"]');
    }
  } catch (e) { out.err = String(e && e.stack || e); }
  var d = document.createElement("div"); d.id = "__mas2__";
  d.textContent = JSON.stringify(out); document.body.appendChild(d);
})();
</script>"""


# 심은 기회의 대상 중 줄에 글자로 나오는 토막 — 큐 줄이 이 종류의 것인지 알아보는 데 쓴다.
NEEDLE = {"cannibalization": "내부 경쟁 검색어", "intent_split": "split-page", "coverage": "배정 점검",
          "pseo_pattern": "템플릿 씨앗", "ai_bot_blocked": "OAI-SearchBot"}


def _seed(home: Path) -> str:
    """test_render 의 픽스처에 다섯 종류의 열린 기회를 더한다. 반환: 심은 사이트 이름."""
    import db
    import scoring
    import test_render as tr
    conn = db.connect()
    try:
        site = tr.SITES[1]
        pid = conn.execute("SELECT id FROM projects WHERE name=?", (site,)).fetchone()[0]
        targets = {"cannibalization": f"{site} 내부 경쟁 검색어", "intent_split": f"https://{site}/split-page",
                   "coverage": "cluster:배정 점검", "pseo_pattern": f"{site} 템플릿 씨앗",
                   "ai_bot_blocked": "OAI-SearchBot"}
        for kind in CHECK_KINDS:
            conn.execute(
                "INSERT INTO opportunities(project_id,kind,target,score,reasoning,status) VALUES(?,?,?,?,?,'new')",
                (pid, kind, targets[kind], 70, "배정 점검용"))
        # ai_bot_blocked 의 근거 카드는 크롤이 남긴 robots.txt 로 선다 — 없으면 카드째 접힌다.
        conn.execute("INSERT INTO crawl_runs(project_id,finished_at,seed,pages,robots_txt) VALUES(?,?,?,?,?)",
                     (pid, "2026-10-01 00:00:00", "home", 1,
                      "User-agent: OAI-SearchBot\nDisallow: /\n\nUser-agent: *\nAllow: /\n"))
        triage = [scoring.verdict_key(k, targets[k]) for k in CHECK_KINDS if k in scoring.TRIAGE_KINDS]
        if triage:
            db.set_verdicts(conn, pid, triage, "work")
        conn.commit()
        return site
    finally:
        conn.close()


def _read(html: str, ident: str) -> dict:
    m = re.search(rf'id="{ident}"[^>]*>(.*?)</div>', html, re.S)
    assert m, f"검사 스크립트({ident})가 결과를 못 남겼다"
    o = json.loads(htmlmod.unescape(m.group(1)))
    assert not o.get("err"), f"검사 스크립트가 터졌다: {o.get('err')}"
    return o


def _probe_src():
    import scoring
    return PROBE.replace("%KINDS%", json.dumps(list(CHECK_KINDS))).replace("%NEEDLES%", json.dumps(NEEDLE))


def _status_src():
    import scoring
    k = CHECK_KINDS[0]
    view = scoring.KINDS and next(x.see[0] for x in scoring.KINDS if x.name == k)
    return STATUS.replace("%VIEW%", view).replace("%KIND%", k).replace("%NEEDLE%", NEEDLE[k])


def _assert_site(label: str, o: dict, st: dict) -> None:
    import scoring
    if "-v" in sys.argv:
        print(label, json.dumps({"probe": o, "status": st}, ensure_ascii=False, indent=1))
    inv = scoring.queue_kinds_by_view()
    assert o["inv"] == inv, f"{label}: 페이로드 queue_kinds_by_view 가 scoring 과 다르다: {o['inv']}"
    for name in CHECK_KINDS:
        k = o["kinds"][name]
        assert not k.get("noSee"), f"{label}: {name} 이 화면의 KIND_VIEWS(= 페이로드 kind_views)에 없다"
        view, sec = o["see"][name]
        L = f"{label} {name}→{view}/{sec}"
        assert k["kindRows"] > 0, f"{L}: 그 화면 [처리] 큐에 이 종류의 줄이 없다(줄 {k['rows']}개)"
        g = k["goSee"]
        assert g["exists"] and g["visible"], f"{L}: 개요의 '자세히 보기'가 보이는 섹션에 닿지 못했다: {g}"
        assert g["tab"] == "manage", f"{L}: '자세히 보기'가 [관리] 탭을 안 열었다: {g}"
        assert -40 <= g["top"] <= 400, f"{L}: 섹션이 화면 위쪽에 서지 않았다(top={g['top']})"
    for view in {o["see"][n][0] for n in CHECK_KINDS}:
        assert o["badge0"].get(view, 0) >= sum(1 for n in CHECK_KINDS if o["see"][n][0] == view), \
            f"{label}: 탭 배지 queue_open_by_view[{view}]={o['badge0'].get(view)} 가 새 종류의 열린 기회를 안 센다"
    assert st["hadRow"], f"{label}: 상태를 바꿀 줄이 [처리] 큐에 없다"
    assert set(st["before"]) <= {"new", "acked"} and st["after"] and set(st["after"]) == {"done"}, \
        f"{label}: 상태가 서버에 안 바뀌었다: {st}"
    assert st["badgeAfter"] == st["shellBadge"] and st["badgeAfter"] < st["badgeBefore"], \
        f"{label}: 탭 배지가 서버 값을 안 따라간다: {st}"
    assert st["gone"], f"{label}: 끝낸 기회가 [처리] 큐에 남아 있다"


def run() -> None:
    import test_render as tr
    browser = tr.find_browser()
    if not browser:
        print("크롬/엣지가 없어 건너뜁니다")
        return
    home = Path(tempfile.mkdtemp(prefix="seo-miner-modes-assign-"))
    saved = tr.RELOAD_TOUCH
    try:
        tr.fixture(home)
        site_name = _seed(home)
        import dashboard
        shell = dashboard.HTML.replace(b"</head>", tr.PROBE.encode("utf-8") + b"</head>", 1)
        prof = home / "chrome-profile"
        with tr._stdlib(shell.replace(b"</body>", _probe_src().encode("utf-8") + b"</body>", 1)) as site:
            o = _read(tr.dom(browser, f"{site.base}/d#{site_name}", prof), "__mas__")
        with tr._stdlib(shell.replace(b"</body>", _status_src().encode("utf-8") + b"</body>", 1)) as site:
            st = _read(tr.dom(browser, f"{site.base}/d#{site_name}", prof), "__mas2__")
        _assert_site("로컬", o, st)
        print("  로컬: ok")
        hosted_home = home / "hosted"
        probe, status = _probe_src(), _status_src()
        try:
            with tr.serve_hosted(hosted_home) as site:
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
                    _seed(store.home(uid))
                finally:
                    if old is None:
                        os.environ.pop("CAPTURE_HOME", None)
                    else:
                        os.environ["CAPTURE_HOME"] = old
                tr.RELOAD_TOUCH = probe
                pg = tr.dom(browser, f"{site.base}/d?reload_probe=1#{site_name}", prof)
                site.after()
                assert not tr.probe(pg)["err"], "호스팅: JS 가 터졌다:\n  " + "\n  ".join(tr.probe(pg)["err"][:6])
                assert not site.failures(), "호스팅: 서버가 4xx/5xx 를 냈다:\n  " + "\n  ".join(site.failures()[:8])
                o2 = _read(pg, "__mas__")
                tr.RELOAD_TOUCH = status
                st2 = _read(tr.dom(browser, f"{site.base}/d?reload_probe=1#{site_name}", prof), "__mas2__")
            _assert_site("호스팅", o2, st2)
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
    print("modes_assign self-check ok")
