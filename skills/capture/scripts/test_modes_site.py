#!/usr/bin/env python3
"""[사이트 점검]의 [할 것]·[전체] 두 탭 — 선언·마크업·실제 화면(로컬·호스팅)을 함께 본다.

틀(view-def `modes`/`queue`, 섹션 `data-mode`)은 셸이 한 벌로 갖는다(test_seams 170·180번대).
여기서는 **이 화면이 그 틀 위에 바르게 섰는지**만 본다:

  · 선언: view-def 에 modes(처리·관리)와 queue(mode=work)가 있고, 종류 목록은 적지 않았다.
  · 마크업: 이 화면의 섹션이 전부 [전체]에 든다(큐 카드는 셸이 [할 것]에 세운다).
  · 이음매: 셸이 탭으로 접는 `hidden` 과 이 화면이 "자료 없음"으로 접는 것이 **같은 속성을
    쓰지 않는다**. 같이 쓰면 [할 것]에서 상태판이 텅 비고(숨은 섹션을 「자료 없음」으로 읽는다),
    [전체]로 넘어올 때 자료 없는 섹션이 셸 손에 펴진다.
  · 화면: 헤드리스 크롬으로 열어 [할 것]↔[전체]를 누르고, [할 것]에서 기회 상태를 바꿔 서버 값을 읽고,
    숨은 탭에서 그려진 도넛이 [전체]에서 폭 0 이 아닌지, 인쇄 규칙이 접힌 섹션을 펴는지 본다.

크롬이 없으면 화면 검사는 건너뛴다(test_render 와 같은 규칙).
"""
from __future__ import annotations

import gc
import html as htmlmod
import http.client
import json
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
ROOT = HERE.parents[2]
SITE_HTML = ROOT / "skills" / "capture" / "templates" / "views" / "site.html"

import test_render as TR  # noqa: E402  (픽스처·서버·수집기를 그대로 쓴다)

SITE = TR.SITES[1]


def _src() -> str:
    return SITE_HTML.read_text("utf-8")


def _view_def() -> dict:
    m = re.search(r'<script type="application/json" class="view-def">\s*(\{.*?\})\s*</script>', _src(), re.S)
    assert m, "site.html 에 view-def 가 없다"
    return json.loads(m.group(1))


def _markup() -> str:
    """<style>·<script> 를 뺀 마크업 — 소스에 적힌 글자가 아니라 화면에 서는 태그만 본다."""
    return re.sub(r"<script[\s\S]*?</script>|<style[\s\S]*?</style>|<!--[\s\S]*?-->", "", _src())


def _script() -> str:
    return "\n".join(re.findall(r"<script>([\s\S]*?)</script>", _src()))


# ── 정적 ──────────────────────────────────────────────────────────────────────

def test_site_declares_work_and_manage_modes_and_a_queue_without_kinds():
    d = _view_def()
    ms = d.get("modes")
    assert ms and [m["id"] for m in ms] == ["work", "manage"], f"modes 는 처리·관리 순이어야 한다: {ms}"
    assert [m["label"] for m in ms] == ["할 것", "전체"], ms
    assert ms[0].get("count") == "queue_open_by_view.site", f"[할 것] 배지는 페이로드의 열린 기회 수여야 한다: {ms[0]}"
    assert "count" not in ms[1], "[전체]에는 건수 배지가 없다"
    q = d.get("queue")
    assert q is not None and q.get("mode") == "work", f"queue 는 [할 것] 탭에 서야 한다: {q}"
    assert "kinds" not in q or q["kinds"] == "see", \
        f"큐 종류 목록을 뷰에 적으면 종류→화면 사본이다(정본 scoring.KINDS 의 see): {q}"


def test_every_site_section_is_in_the_manage_tab():
    sections = re.findall(r"<section\b([^>]*)>", _markup())
    assert len(sections) >= 10, f"섹션을 {len(sections)}개밖에 못 읽었다 — 검사가 헛돈다"
    for attrs in sections:
        sid = re.search(r'id="([^"]+)"', attrs)
        assert re.search(r'\bdata-mode="manage"', attrs), \
            f"섹션 {sid.group(1) if sid else attrs.strip()} 이 [전체]에 안 들었다 — 탭을 단 화면의 섹션은 data-mode 가 있어야 한다"
    # view-def 의 sections 목록이 가리키는 id 도 전부 이 파일 마크업에 있다(밖으로 새는 섹션 없음)
    ids = set(re.findall(r'\bid="([^"]+)"', _markup()))
    for s in _view_def()["sections"]:
        assert s in ids, f"view-def sections 의 {s} 가 마크업에 없다"


def test_site_never_uses_hidden_on_its_own_sections():
    """셸은 탭으로 접는 섹션에 hidden 을 걸고(data-vm-off), 탭을 바꿀 때 그것만 편다. 이 화면이 같은
    속성을 "자료 없음"에도 쓰면 두 뜻이 섞인다 — 그래서 화면 몫은 data-st-hide 로 따로 둔다."""
    mk = _markup()
    for attrs in re.findall(r"<section\b([^>]*)>", mk):
        assert not re.search(r"\bhidden\b", attrs), f"섹션이 hidden 으로 서 있다(data-st-hide 를 쓴다): {attrs.strip()}"
    sec_ids = re.findall(r'<section\b[^>]*\bid="([^"]+)"', mk)
    js = _script()
    for sid in sec_ids:
        assert not re.search(r'\$\("%s"\)\.hidden' % re.escape(sid), js), \
            f"{sid} 의 hidden 을 스크립트가 만진다 — stHide(el, off) 를 쓴다"
    assert not re.search(r"\b(sec|kp|hero)\.hidden\b", js), "섹션 변수(sec·kp·hero)의 hidden 을 만진다 — stHide 를 쓴다"
    summ = js.split("function stSummary")[1].split("\nfunction ")[0]
    assert ".hidden" not in summ.split("hero")[0], "상태판이 섹션의 hidden 으로 자료 유무를 읽는다 — stIsOff 를 쓴다"
    assert "data-st-hide" in _src() and "stHide" in js and "stIsOff" in js


def test_site_print_opens_tab_folded_sections_and_has_no_important():
    src = _src()
    assert "!important" not in src, "!important 금지"
    prints = re.findall(r"@media print\s*\{([\s\S]*?)\n\}", src)
    assert any("[data-mode][hidden]" in p for p in prints), \
        "인쇄에서 탭으로 접힌 섹션([data-mode][hidden])을 펴는 규칙이 이 화면 CSS 에 없다 — 종이에서 [전체]가 통째로 사라진다"


# ── 화면 ──────────────────────────────────────────────────────────────────────

def _patched_fixture():
    """test_render 의 픽스처 위에 이 화면 몫을 얹는다 — 색인 검사 둘(도넛이 서려면 문제 URL 이 있어야 한다)과
    사이트 점검 큐의 세 종류. 호스팅 대상도 TR.fixture 를 부르므로 같은 이름을 갈아 끼운다."""
    orig = TR.fixture

    def fx(home: Path) -> None:
        orig(home)
        import db
        conn = db.connect()
        try:
            pid = conn.execute("SELECT id FROM projects WHERE name=?", (SITE,)).fetchone()[0]
            db.write_index_status(conn, pid, "2026-06-01", [
                {"url": f"https://{SITE}.example/a", "verdict": "PASS", "coverage_state": "Submitted and indexed"},
                {"url": f"https://{SITE}.example/ixbad", "verdict": "NEUTRAL",
                 "coverage_state": "Crawled - currently not indexed"}])
            conn.executemany(
                "INSERT INTO opportunities(project_id,kind,target,score,reasoning,status) VALUES(?,?,?,?,'r','new')",
                [(pid, "index_blocked", f"https://{SITE}.example/ixbad", 40),
                 (pid, "crawl_issue", f"https://{SITE}.example/cr", 35),
                 (pid, "device_gap", f"{SITE} 모바일 검색어", 30)])
            conn.commit()
        finally:
            conn.close()
    return orig, fx


DRIVE = """<script>
(function () {
  setTimeout(async function () {
    var out = {}, sleep = function (ms) { return new Promise(function (r) { setTimeout(r, ms); }); };
    var $$ = function (s, r) { return [].slice.call((r || document).querySelectorAll(s)); };
    try {
      var box = document.getElementById("view-site");
      var shown = function (el) { return !!el && getComputedStyle(el).display !== "none" && el.getClientRects().length > 0; };
      var SECS = $$("#view-site > section[data-mode=manage]").map(function (s) { return s.id; });
      out.secs = SECS;
      var vis = function () { return SECS.filter(function (id) { return shown(document.getElementById(id)); }); };
      try { sessionStorage.removeItem("sm:mode:site"); } catch (e) {}
      SM.mode = {}; SM.touched = true; SM.show("site");
      await sleep(500);
      var tabs = $$(".vtabs [role=tab]", box);
      out.tabs = tabs.map(function (t) { return t.textContent.replace(/\\s+/g, " ").trim(); });
      out.sel0 = tabs.map(function (t) { return t.getAttribute("aria-selected"); });
      out.q0 = shown(document.getElementById("q-site"));
      out.qrows = $$("#q-site details.opp").length;
      out.vis0 = vis();
      out.gridOk0 = (function () { var t = box.querySelector(".vtabs"), q = document.getElementById("q-site");
        var w = box.getBoundingClientRect().width;
        return !!t && !!q && q.getBoundingClientRect().width > w * 0.9; })();
      tabs[1].click();
      await sleep(700);
      out.sel1 = tabs.map(function (t) { return t.getAttribute("aria-selected"); });
      out.q1 = shown(document.getElementById("q-site"));
      out.vis1 = vis();
      out.stRows = $$("#st-sum .stx").length;
      out.stBoard = shown(document.getElementById("st-board"));
      out.stOffAttr = $$("#view-site > section[data-st-hide]").map(function (s) { return s.id; });
      out.stOffShown = out.stOffAttr.filter(function (id) { return shown(document.getElementById(id)); });
      // Chart.js 는 펴진 뒤 다음 프레임에 크기를 다시 잰다 — 기다리되, 끝내 0 이면 깨진 것이다
      var cv = document.querySelector("#ix-donut canvas");
      for (var z = 0; z < 10 && cv && !cv.clientWidth; z++) await sleep(300);
      out.donut = cv ? {w: cv.clientWidth, h: cv.clientHeight, chart: !!cv._chart} : null;
      out.ixdShown = shown(document.getElementById("ixd-sec"));
      tabs[0].click();
      await sleep(300);
      var inner = [];
      for (var i = 0; i < document.styleSheets.length; i++) {
        var rs; try { rs = document.styleSheets[i].cssRules; } catch (e) { continue; }
        for (var j = 0; j < rs.length; j++) if (rs[j].type === 4 && /print/.test(rs[j].media.mediaText))
          for (var k = 0; k < rs[j].cssRules.length; k++) inner.push(rs[j].cssRules[k].cssText);
      }
      var st = document.createElement("style"); st.textContent = inner.join("\\n");
      document.head.appendChild(st);
      var folded = SECS.filter(function (id) { var e = document.getElementById(id); return e.hidden && e.dataset.vmOff === "1"; });
      out.foldedN = folded.length;
      out.printOpen = folded.filter(function (id) { var e = document.getElementById(id);
        return !e.hasAttribute("data-st-hide") && getComputedStyle(e).display === "none"; });
      st.remove();
      var row = document.querySelector("#q-site details.opp");
      if (row) {
        var ids = (row.dataset.ids || "").split(" ").map(Number);
        await setOpp(ids[0], "acked");
        await sleep(1200);
        var res = await fetch("/api/data?project=" + encodeURIComponent(document.getElementById("proj").value));
        var d = await res.json();
        var o = (d.opps || []).filter(function (x) { return x.id === ids[0]; })[0];
        out.srvStatus = o ? o.status : "(없음)";
        out.openBy = (d.queue_open_by_view || {}).site;
      }
    } catch (e) { out.err = String(e && e.stack || e); }
    var el = document.createElement("div"); el.id = "__site_modes__";
    el.textContent = JSON.stringify(out); document.body.appendChild(el);
  }, 3000);
})();
</script>"""


class _Proxy(BaseHTTPRequestHandler):
    """호스팅 앱(uvicorn) 앞의 얇은 층 — /d 응답에만 이 파일의 스크립트를 끼운다. serve_hosted 는
    자기 수집기만 끼우고 바깥 스크립트를 받지 않는다(test_render 를 안 고치려고 앞에 선다)."""
    upstream = ("127.0.0.1", 0)

    def _go(self):
        n = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(n) if n else None
        c = http.client.HTTPConnection(*self.upstream, timeout=60)
        hdr = {k: v for k, v in self.headers.items() if k.lower() not in ("host", "accept-encoding")}
        c.request(self.command, self.path, body, hdr)
        r = c.getresponse()
        data = r.read()
        if urlparse(self.path).path == "/d" and r.status == 200 and b"</body>" in data:
            data = data.replace(b"</body>", DRIVE.encode("utf-8") + b"</body>", 1)
        self.send_response(r.status)
        for k, v in r.getheaders():
            if k.lower() not in ("content-length", "transfer-encoding", "connection"):
                self.send_header(k, v)
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
        c.close()

    do_GET = do_POST = do_PUT = do_DELETE = _go

    def log_message(self, *a):
        pass


def _proxy(upstream_base: str) -> ThreadingHTTPServer:
    host, port = urlparse(upstream_base).hostname, urlparse(upstream_base).port
    h = type("H", (_Proxy,), {"upstream": (host, port)})
    srv = ThreadingHTTPServer(("127.0.0.1", 0), h)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _open(browser: str, base: str, home: Path, query: str = "") -> dict:
    r = subprocess.run(
        [browser, "--headless=new", "--disable-gpu", "--no-sandbox", "--window-size=1280,2400",
         f"--user-data-dir={home / 'chrome-profile'}", "--virtual-time-budget=30000", "--dump-dom",
         f"{base}/d{query}#{SITE}"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)
    html = r.stdout
    assert html, f"브라우저가 페이지를 못 열었다\n{(r.stderr or '')[-600:]}"
    p = TR.probe(html)
    assert not p["err"], "JS 가 터졌다:\n  " + "\n  ".join(p["err"][:6])
    m = re.search(r'id="__site_modes__"[^>]*>(.*?)</div>', html, re.S)
    assert m, "화면 검사 스크립트가 결과를 못 남겼다"
    o = json.loads(htmlmod.unescape(m.group(1)))
    assert not o.get("err"), f"화면 검사 스크립트가 터졌다: {o['err']}"
    o["_net"] = p["net"]
    return o


def _judge(label: str, o: dict) -> None:
    assert [re.sub(r"\d+$", "", t) for t in o["tabs"]] == ["할 것", "전체"], f"{label}: 탭 줄 {o['tabs']}"
    assert o["sel0"] == ["true", "false"], f"{label}: 처음은 [할 것]여야 한다(선언 첫째): {o['sel0']}"
    assert o["q0"] and o["qrows"] >= 1, f"{label}: [할 것]에 큐 줄이 안 섰다: q={o['q0']} rows={o['qrows']}"
    assert o["vis0"] == [], f"{label}: [할 것]인데 [전체] 섹션이 보인다: {o['vis0']}"
    assert o["gridOk0"], f"{label}: 탭 줄·큐 카드가 12열 격자에서 전폭으로 안 섰다"
    assert o["sel1"] == ["false", "true"] and not o["q1"], f"{label}: [전체]를 눌렀는데 큐가 남았다: {o['sel1']} q={o['q1']}"
    assert o["stRows"] >= 3, f"{label}: [할 것]에서 자료가 그려진 뒤 [전체]를 열었는데 상태판이 비었다({o['stRows']}줄) — 접힌 섹션을 「자료 없음」으로 읽었다"
    assert o["stBoard"], f"{label}: 상태판 카드가 [전체]에서 안 보인다"
    assert o["stOffAttr"], f"{label}: 자료 없는 섹션이 하나도 없어 「스스로 접기」 검사가 헛돈다(픽스처)"
    assert not o["stOffShown"], f"{label}: 자료 없어 접힌 섹션이 [전체]에서 펴졌다: {o['stOffShown']}"
    assert set(o["secs"]) - set(o["stOffAttr"]) == set(o["vis1"]), \
        f"{label}: [전체]에서 자료 있는 섹션이 정확히 서야 한다: 보임={o['vis1']} 접힘={o['stOffAttr']}"
    d = o["donut"]
    assert o["ixdShown"] and d and d["w"] > 100 and d["h"] > 100 and d["chart"], \
        f"{label}: 숨은 탭에서 그려진 색인 도넛이 [전체]에서 폭 0 이다: {d}"
    assert o["foldedN"] >= 5 and o["printOpen"] == [], \
        f"{label}: 인쇄에서 접힌 섹션이 안 펴진다(접힘 {o['foldedN']}): {o['printOpen']}"
    assert o.get("srvStatus") == "acked", f"{label}: [할 것]에서 진행 중으로 바꿨는데 서버 값이 {o.get('srvStatus')!r}"
    assert not o["_net"], f"{label}: /api/* 실패: {o['_net']}"


def test_site_tabs_in_a_real_browser_local_and_hosted():
    browser = TR.find_browser()
    if not browser:
        print("크롬/엣지가 없어 건너뜁니다")
        return
    import os
    import dashboard
    home = Path(tempfile.mkdtemp(prefix="seo-miner-modes-site-"))
    orig, fx = _patched_fixture()
    saved = dict(os.environ)
    TR.fixture = fx
    try:
        fx(home)
        # 로컬 서버는 상태를 바꾸는 요청에 토큰을 본다 — 진짜 / 응답이 싣는 것과 같게 싣는다
        tok = ("<script>window.__SM_TOKEN__=" + json.dumps(dashboard.TOKEN) + "</script>").encode("utf-8")
        shell = dashboard.HTML.replace(b"</head>", tok + TR.PROBE.encode("utf-8") + b"</head>", 1)
        page = shell.replace(b"</body>", DRIVE.encode("utf-8") + b"</body>", 1)
        with TR._stdlib(page) as site:
            _judge("로컬", _open(browser, site.base, home))
        if (ROOT / "server" / "assets" / "dash.html").exists():
            with TR.serve_hosted(home / "hosted") as site:
                px = _proxy(site.base)
                try:
                    _judge("호스팅", _open(browser, f"http://127.0.0.1:{px.server_address[1]}", home))
                finally:
                    px.shutdown()
                    px.server_close()
    finally:
        TR.fixture = orig
        os.environ.clear()
        os.environ.update(saved)
        gc.collect()
        shutil.rmtree(home, ignore_errors=True)


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)} passed")
