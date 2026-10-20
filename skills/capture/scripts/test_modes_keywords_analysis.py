#!/usr/bin/env python3
"""[순위 상승 기회](keywords)·[클릭 변화](analysis) 두 화면의 [할 것]·[전체] 탭.

틀(view-def "modes"·섹션 data-mode·"queue")은 셸이 한 벌로 갖는다(test_seams 170·180). 여기서는
**이 두 화면이 그 틀을 제대로 쓰는지**만 못 박는다:

  · 선언: modes 둘([할 것] 먼저 + 건수 경로, [전체]) · queue 는 [할 것] 탭에 선다.
  · 마크업: 화면 상자로 옮겨지는 섹션(<section>)은 전부 [전체]에 산다. 화면의 기존 내용이 [할 것] 큐
    뒤로 사라지거나 두 탭에 걸치면 안 된다.
  · 섹션이 **자기 hidden 을 쓰지 않는다** — 셸이 탭으로 접을 때도 hidden 을 쓰므로(modeApply), 같은
    속성을 뷰가 함께 쓰면 탭을 오갈 때 뷰가 숨긴 카드가 되살아난다(셸은 vmOff 표식으로 자기 것만 편다
    — 그러나 뷰가 그 사이에 hidden=false 를 쓰면 접힌 탭에서 새어 나온다). 뷰의 접기는 data-an-off.
  · 인쇄: 탭으로 접힌 섹션이 종이에서 사라지지 않는다.
  · 같은 화면 안에서 [검색어 열기]가 접힌 [전체]에 열려 허공을 가리키지 않는다.
  · 실제로 열어서: 두 탭이 서고, 처음은 [할 것], [전체]를 누르면 섹션이 전부 보이고 뷰가 스스로 접은 것은
    여전히 접혀 있다. 로컬(stdlib)과 호스팅(serve_hosted, 실제 FastAPI 앱) 둘 다.

CLAUDE.md 의 규율: 검사를 추가했으면 일부러 깨서 FAIL 이 나는 것까지 확인한다.
self-check: python test_modes_keywords_analysis.py
"""
from __future__ import annotations

import html as htmlmod
import json
import re
import shutil
import sys
import tempfile
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
ROOT = Path(__file__).resolve().parents[3]
VIEWS = ROOT / "skills" / "capture" / "templates" / "views"
DOCS = ROOT / "docs" / "screens"
SCREENS = ("keywords", "analysis")


def _read(vid: str) -> str:
    return (VIEWS / f"{vid}.html").read_text("utf-8")


def _def(src: str) -> dict:
    m = re.search(r'class="view-def">\s*(\{.*?\})\s*</script>', src, re.S)
    assert m, "view-def 가 없다"
    return json.loads(m.group(1))


def _markup(src: str) -> str:
    """스크립트·스타일·주석을 뺀 마크업 — 정규식이 코드 속 문자열을 마크업으로 읽지 않게."""
    src = re.sub(r"<!--.*?-->", "", src, flags=re.S)
    return re.sub(r"<script\b.*?</script>|<style\b.*?</style>", "", src, flags=re.S)


def test_declarations():
    for vid in SCREENS:
        d = _def(_read(vid))
        assert d.get("modes") == [
            {"id": "work", "label": "할 것", "count": f"queue_open_by_view.{vid}"},
            {"id": "manage", "label": "전체"}], f"{vid}: modes 가 [할 것](건수 경로)·[전체] 순이 아니다: {d.get('modes')}"
        assert {k: v for k, v in (d.get("queue") or {}).items() if k != "title"} == {"mode": "work"}, f"{vid}: queue 는 {{\"mode\": \"work\"}}(+선택 title) — [할 것] 탭에 선다: {d.get('queue')}"


def test_every_section_lives_in_manage():
    for vid in SCREENS:
        secs = re.findall(r"<section\b([^>]*)>", _markup(_read(vid)))
        assert secs, f"{vid}: 섹션을 하나도 못 읽었다"
        for attrs in secs:
            assert re.search(r'\bdata-mode="manage"', attrs), \
                f"{vid}: [전체]에 안 든 섹션이 있다(처리 큐 말고는 전부 관리 몫): <section{attrs}>"
        # 처리 큐는 셸이 data-mode="work" 로 세운다 — 마크업에 work 섹션을 따로 두면 큐와 두 벌이다.
        assert 'data-mode="work"' not in _markup(_read(vid)), f"{vid}: 마크업이 [할 것] 섹션을 따로 둔다 — 큐는 셸 몫"


def test_views_do_not_toggle_hidden_on_their_own_sections():
    """셸의 탭 접기(hidden + vmOff)와 같은 속성을 뷰가 쓰면 탭을 오갈 때 서로 덮는다."""
    for vid in SCREENS:
        src = _read(vid)
        ids = set(re.findall(r'<section\b[^>]*\bid="([\w-]+)"', _markup(src)))
        for i in re.findall(r'\$\("([\w-]+)"\)\.hidden\s*=', src):
            assert i not in ids, f"{vid}: 섹션 #{i} 의 hidden 을 뷰가 쓴다 — 셸 탭과 겹친다(data-an-off 로)"
        for i in re.findall(r'getElementById\("([\w-]+)"\)\.hidden\s*=', src):
            assert i not in ids, f"{vid}: 섹션 #{i} 의 hidden 을 뷰가 쓴다"
    # 섹션 id 를 배열에서 돌려 쓰는 자리(AN_FILLABLE)도 같다
    assert not re.search(r"const el = \$\(id\);\s*if \(el\) el\.hidden", _read("analysis")), \
        "analysis: 섹션 목록을 돌며 hidden 을 쓴다 — 셸 탭과 겹친다"
    assert re.search(r"#view-analysis\s*>\s*section\[data-an-off\]\s*\{\s*display\s*:\s*none", _read("analysis")), \
        "analysis: 뷰가 접는 섹션(data-an-off)을 숨기는 규칙이 없다"


def test_print_unfolds_tab_hidden_sections():
    for vid in SCREENS:
        css = "".join(re.findall(r"<style>(.*?)</style>", _read(vid), re.S))
        m = re.search(r"@media print\s*\{(.*?)\n\}", css, re.S)
        assert m, f"{vid}: 인쇄 규칙이 없다 — 탭으로 접힌 섹션이 종이에서 사라진다"
        assert re.search(rf"#view-{vid}\s*>\s*\[data-mode\]\[hidden\](?::not\(\[[\w-]+\]\))?\s*\{{[^}}]*display\s*:\s*(block|flex)", m.group(1)), \
            f"{vid}: 인쇄에서 [data-mode][hidden] 을 펴는 규칙이 없다"
        assert "!important" not in css, f"{vid}: !important 금지"


def test_open_top_goes_through_the_manage_tab():
    """[검색어 열기]는 [전체] 표의 줄을 연다 — [할 것]에 서 있으면 그 탭부터 연다."""
    kw, an = _read("keywords"), _read("analysis")
    assert re.search(r'const KW_open = tr => \{[^}]*SM\.setMode\("keywords", "manage"\)', kw, re.S), \
        "keywords: KW_open 이 [전체] 탭을 안 연다 — 접힌 표에 열려 아무 일도 안 일어난 것처럼 보인다"
    assert re.search(r'function anOpenTop\(\) \{[^}]*SM\.setMode\("analysis", "manage"\)', an, re.S), \
        "analysis: anOpenTop 이 [전체] 탭을 안 연다"


def test_docs_describe_both_tabs():
    for vid in SCREENS:
        t = (DOCS / f"{vid}.md").read_text("utf-8")
        assert "[할 것]" in t and "[전체]" in t, f"docs/screens/{vid}.md 에 [할 것]/[전체] 구성 설명이 없다"


# ── 실제로 열어서 ───────────────────────────────────────────────────────────

# 두 화면을 차례로 열어 탭을 눌러 보고, 본 것을 #__mk__ 에 옮겨 적는다.
RUN = """<script>
setTimeout(function () {
  var out = {};
  function snap(V) {
    var box = document.getElementById("view-" + V);
    var bar = box.querySelector(":scope > .vtabs");
    var tabs = bar ? [].slice.call(bar.querySelectorAll('[role="tab"]')) : [];
    var q = document.getElementById("q-" + V);
    var secs = [].slice.call(box.children).filter(function (e) { return e.tagName === "SECTION" && e.dataset.mode === "manage"; });
    return {
      tabs: tabs.map(function (t) { return t.firstChild.textContent.trim(); }),
      badge: tabs.map(function (t) { return (t.querySelector(".cnt") || {}).textContent || ""; }),
      sel: tabs.map(function (t) { return t.getAttribute("aria-selected"); }),
      now: SM.modeNow(V),
      q: q ? {mode: q.dataset.mode, display: getComputedStyle(q).display, rows: q.querySelectorAll("[data-q=list] > *").length} : null,
      manage: secs.map(function (s) { return {id: s.id || "", off: s.hasAttribute("data-an-off") || s.hasAttribute("data-kw-off"),
        display: getComputedStyle(s).display, h: s.offsetHeight}; })
    };
  }
  try {
    ["keywords", "analysis"].forEach(function (V) {
      var o = out[V] = {};
      try { sessionStorage.removeItem("sm:mode:" + V); } catch (e) {}
      SM.mode = {}; SM.show(V);
      o.first = snap(V);
      var tabs = document.getElementById("view-" + V).querySelectorAll(":scope > .vtabs [role=tab]");
      tabs[1].click(); o.manage = snap(V);
      tabs[0].click(); o.work = snap(V);
      tabs[1].click(); o.manage2 = snap(V);           // 오간 뒤에도 뷰가 접은 것은 접혀 있어야 한다
      tabs[0].click();
      SM.drawer && SM.drawer.close && SM.drawer.close();
      (V === "keywords" ? window.KW_openTop : window.anOpenTop)();
      o.afterOpen = SM.modeNow(V);
      SM.drawer && SM.drawer.close && SM.drawer.close();
    });
  } catch (e) { out.err = String(e && e.stack || e); }
  var d = document.createElement("div"); d.id = "__mk__";
  d.textContent = JSON.stringify(out); document.body.appendChild(d);
}, 3500);
</script>"""


class _Proxy(BaseHTTPRequestHandler):
    """호스팅 앱 앞의 얇은 층 — /d 응답 끝에 검사 스크립트를 끼운다(앱은 안 고친다)."""
    target = ""

    def _fwd(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else None
        req = urllib.request.Request(self.target + self.path, data=body, method=self.command,
                                     headers={k: v for k, v in self.headers.items()
                                              if k.lower() not in ("host", "content-length", "accept-encoding")})
        try:
            r = urllib.request.urlopen(req, timeout=60)
            code, hdr, data = r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:
            code, hdr, data = e.code, e.headers, e.read()
        if self.path.split("?")[0] == "/d" and b"</body>" in data:
            data = data.replace(b"</body>", RUN.encode("utf-8") + b"</body>", 1)
        self.send_response(code)
        for k, v in hdr.items():
            if k.lower() not in ("content-length", "transfer-encoding", "connection", "content-encoding"):
                self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = do_POST = do_PUT = do_DELETE = _fwd

    def log_message(self, *a):
        pass


def _verify(label: str, html: str) -> None:
    import test_render as tr
    p = tr.probe(html)
    assert not p["err"], f"{label}: JS 가 터졌다:\n  " + "\n  ".join(p["err"][:6])
    m = re.search(r'id="__mk__"[^>]*>(.*?)</div>', html, re.S)
    assert m, f"{label}: 검사 스크립트가 결과를 못 남겼다"
    out = json.loads(htmlmod.unescape(m.group(1)))
    assert not out.get("err"), f"{label}: 검사 스크립트가 터졌다: {out.get('err')}"
    for V in SCREENS:
        o = out[V]
        f = o["first"]
        assert f["tabs"] == ["할 것", "전체"], f"{label} {V}: 탭 줄이 [할 것]·[전체] 가 아니다: {f['tabs']}"
        assert f["sel"] == ["true", "false"] and f["now"] == "work", f"{label} {V}: 처음 탭이 [할 것]가 아니다: {f['sel']}"
        assert f["q"] and f["q"]["mode"] == "work", f"{label} {V}: 처리 큐가 [할 것] 탭에 안 섰다: {f['q']}"
        assert f["q"]["display"] != "none", f"{label} {V}: [할 것]에서 큐가 안 보인다"
        assert f["manage"], f"{label} {V}: [전체] 섹션이 하나도 없다"
        assert all(s["display"] == "none" for s in f["manage"]), \
            f"{label} {V}: [할 것]인데 [전체] 섹션이 보인다: {[s for s in f['manage'] if s['display'] != 'none']}"
        m1 = o["manage"]
        assert m1["now"] == "manage" and m1["sel"] == ["false", "true"], f"{label} {V}: [전체]를 눌렀는데 안 바뀐다"
        assert m1["q"]["display"] == "none", f"{label} {V}: [전체]인데 처리 큐가 남아 있다"
        for s in m1["manage"]:
            if s["off"]:
                assert s["display"] == "none", f"{label} {V}: 뷰가 접은 #{s['id']} 가 [전체]에서 보인다"
            else:
                assert s["display"] != "none", f"{label} {V}: [전체]인데 #{s['id']} 가 안 보인다"
        assert any(not s["off"] and s["h"] > 0 for s in m1["manage"]), f"{label} {V}: [전체]에 보이는 섹션이 없다"
        if V == "analysis":
            # 픽스처는 GA4 가 없고 실적은 있다 — 이 넷은 뷰가 스스로 접는 카드다. data-an-off 속성이 아니라
            # 결과(안 보임)로 본다: 접는 방법이 hidden 으로 되돌아가 셸 탭과 겹치면 여기서 보인다.
            shut = {s["id"]: s["display"] for s in m1["manage"]}
            for i in ("an-newret-sec", "an-funnel-sec", "an-channels-sec", "an-empty-fill"):
                assert shut.get(i) == "none", f"{label} {V}: GA4 없는 사이트인데 #{i} 가 [전체]에서 보인다: {shut.get(i)}"
        assert [s for s in o["work"]["manage"] if s["display"] != "none"] == [], f"{label} {V}: [할 것]로 돌아왔는데 [전체]가 남았다"
        assert [(s["id"], s["display"] == "none") for s in o["manage2"]["manage"]] == \
               [(s["id"], s["off"]) for s in m1["manage"]], \
            f"{label} {V}: 탭을 오간 뒤 뷰가 접은 섹션이 되살아났다(셸 hidden 과 겹침)"
        assert o["afterOpen"] == "manage", f"{label} {V}: [검색어 열기]가 [전체] 탭을 안 연다: {o['afterOpen']}"


def test_render_local_and_hosted():
    import test_render as tr
    browser = tr.find_browser()
    if not browser:
        print("크롬/엣지가 없어 건너뜁니다")
        return
    home = Path(tempfile.mkdtemp(prefix="seo-miner-modes-"))
    try:
        import os
        import doctor
        for k in (doctor.MODE_ENV, doctor.TOOL_ENV, doctor.TERMINAL_ENV):
            os.environ.pop(k, None)
        tr.fixture(home)
        import dashboard
        shell = dashboard.HTML.replace(b"</head>", tr.PROBE.encode("utf-8") + b"</head>", 1)
        page = shell.replace(b"</body>", RUN.encode("utf-8") + b"</body>", 1)
        with tr._stdlib(page) as site:
            html = tr.dom(browser, f"{site.base}/d#{tr.SITES[1]}", home / "chrome-profile")
        _verify("로컬", html)

        if (ROOT / "server" / "assets" / "dash.html").exists():
            with tr.serve_hosted(home / "hosted") as site:
                _Proxy.target = site.base
                ThreadingHTTPServer.allow_reuse_address = True
                srv = ThreadingHTTPServer(("127.0.0.1", 0), _Proxy)
                threading.Thread(target=srv.serve_forever, daemon=True).start()
                try:
                    url = f"http://127.0.0.1:{srv.server_address[1]}/d#{tr.SITES[1]}"
                    html = tr.dom(browser, url, home / "chrome-profile-h")
                finally:
                    srv.shutdown()
                    srv.server_close()
                # 프록시가 끼운 PROBE 가 없다 — 오류 수집기는 호스팅 앱 쪽 래퍼가 /d 에 이미 끼웠다.
                _verify("호스팅", html)
                assert not site.failures(), f"호스팅: 화면이 부른 /api/* 가 실패했다: {site.failures()[:5]}"
    finally:
        import gc
        gc.collect()
        shutil.rmtree(home, ignore_errors=True)


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)} passed")
