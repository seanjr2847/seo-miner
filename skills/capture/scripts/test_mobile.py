#!/usr/bin/env python3
"""모바일 폭(390px)에서 화면이 넘치지 않는가 — 헤드리스 크롬을 CDP 로 직접 몰아 잰다.

test_render.py 는 `--dump-dom` 으로 열어서 DOM 만 본다. 그 방식은 **뷰포트를 못 정한다**
(창 크기만 정하고, 레이아웃 뷰포트·@media 판정은 그대로다). 그래서 모바일 폭 CSS 는 한 번도
측정된 적이 없었다. 여기서는 크롬을 `--remote-debugging-port` 로 띄워 CDP
(Emulation.setDeviceMetricsOverride)로 390×844 모바일 뷰포트를 세우고, 화면마다 JS 로 잰다.

보는 것(눈으로 봐야 아는 것 전부는 아니다 — 가로 넘침·잘림·작은 글씨·좁은 터치 영역):
  · 문서 가로 넘침: document.documentElement.scrollWidth <= 뷰포트 + 1
  · 탭 줄([처리]·[관리]·설정 탭)이 보이고, 줄의 모든 탭이 뷰포트 안에 닿거나 줄 안에서 가로로 밀린다
  · 화면 안 컨트롤(버튼·입력·링크)이 뷰포트 오른쪽 밖으로 삐져나가지 않는다
  · 눌러야 하는 것(탭·버튼)이 너무 작지 않다

대상은 둘이다 — 로컬 대시보드(stdlib Handler)와 호스팅 조립본(FastAPI, serve_hosted). 크롬이 없으면
건너뛴다. 사용자의 진짜 Brain 은 안 건드린다(임시 CAPTURE_HOME).

눈으로 보는 스크린샷은 이 파일의 Browser.shot()/shot_full() 로 남긴다(검사 아님).
"""
from __future__ import annotations

import base64
import gc
import hashlib
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_render as R  # noqa: E402  (픽스처·서버·크롬 찾기를 한 벌로 쓴다)

# 브라우저가 닫히며 끊는 연결이 서버 스레드의 트레이스백으로 검사 출력을 덮는다 — 연결 오류만 삼킨다.
_he = R.ThreadingHTTPServer.handle_error


def _quiet(self, request, client_address):
    if isinstance(sys.exc_info()[1], ConnectionError):
        return
    _he(self, request, client_address)


R.ThreadingHTTPServer.handle_error = _quiet

PHONE = (390, 844)
DESKTOP = (1280, 800)
SLACK = 1                       # 반올림 여유 px


# ── 최소 웹소켓 클라이언트 (stdlib 만 — CDP 한 줄이면 된다) ────────────────────────
class _WS:
    def __init__(self, url: str):
        host, rest = url[len("ws://"):].split("/", 1)
        h, p = host.split(":")
        self.s = socket.create_connection((h, int(p)), timeout=60)
        key = base64.b64encode(os.urandom(16)).decode()
        self.s.sendall((f"GET /{rest} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\n"
                        f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                        "Sec-WebSocket-Version: 13\r\n\r\n").encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            buf += self.s.recv(4096)
        head, self.buf = buf.split(b"\r\n\r\n", 1)
        assert b" 101 " in head.split(b"\r\n")[0], head[:200]

    def _read(self, n: int) -> bytes:
        while len(self.buf) < n:
            chunk = self.s.recv(65536)
            if not chunk:
                raise ConnectionError("CDP 연결이 끊겼다")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def send(self, text: str) -> None:
        data = text.encode("utf-8")
        n = len(data)
        hdr = bytearray([0x81])
        if n < 126:
            hdr.append(0x80 | n)
        elif n < 65536:
            hdr += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            hdr += bytes([0x80 | 127]) + struct.pack(">Q", n)
        mask = os.urandom(4)
        self.s.sendall(bytes(hdr) + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def recv(self) -> str:
        msg = b""
        while True:
            b0, b1 = self._read(2)
            n = b1 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8))[0]
            payload = self._read(n)
            op = b0 & 0x0F
            if op == 0x8:
                raise ConnectionError("CDP 가 닫았다")
            if op in (0x9, 0xA):
                continue
            msg += payload
            if b0 & 0x80:
                return msg.decode("utf-8", "replace")

    def close(self) -> None:
        try:
            self.s.close()
        except OSError:
            pass


class Browser:
    """크롬 한 대 + 탭 하나. 뷰포트는 CDP 로 정한다(창 크기 플래그가 아니라)."""

    def __init__(self, exe: str, profile: Path):
        self.proc = subprocess.Popen(
            [exe, "--headless=new", "--disable-gpu", "--no-sandbox", "--remote-allow-origins=*",
             "--remote-debugging-port=0", f"--user-data-dir={profile}", "--hide-scrollbars=false",
             "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        port_file = profile / "DevToolsActivePort"
        t0 = time.monotonic()
        while not port_file.exists():
            if self.proc.poll() is not None or time.monotonic() - t0 > 30:
                raise AssertionError("크롬이 디버그 포트를 못 열었다")
            time.sleep(0.1)
        time.sleep(0.2)
        self.port = int(port_file.read_text().split()[0])
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/json/new?about:blank", method="PUT")
        tab = json.load(urllib.request.urlopen(req, timeout=30))
        self.ws = _WS(tab["webSocketDebuggerUrl"])
        self.n = 0
        self.call("Page.enable")
        self.call("Runtime.enable")

    def call(self, method: str, **params):
        self.n += 1
        mid = self.n
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params}))
        while True:
            m = json.loads(self.ws.recv())
            if m.get("id") == mid:
                if "error" in m:
                    raise AssertionError(f"CDP {method}: {m['error']}")
                return m.get("result", {})

    def viewport(self, w: int, h: int, mobile: bool) -> None:
        self.call("Emulation.setDeviceMetricsOverride", width=w, height=h,
                  deviceScaleFactor=2 if mobile else 1, mobile=mobile)
        if mobile:
            self.call("Emulation.setTouchEmulationEnabled", enabled=True)
            self.call("Emulation.setEmitTouchEventsForMouse", enabled=True, configuration="mobile")
        else:
            self.call("Emulation.setTouchEmulationEnabled", enabled=False)

    def js(self, expr: str):
        r = self.call("Runtime.evaluate", expression=expr, returnByValue=True, awaitPromise=True)
        if "exceptionDetails" in r:
            d = r["exceptionDetails"]
            raise AssertionError(f"JS 오류: {(d.get('exception') or {}).get('description') or d.get('text')}")
        return r["result"].get("value")

    def goto(self, url: str) -> None:
        self.call("Page.navigate", url="about:blank")
        self.call("Page.navigate", url=url)
        # 셸이 서고(#nav 버튼) 첫 데이터가 그려질 때까지 기다린다
        t0 = time.monotonic()
        while time.monotonic() - t0 < 40:
            time.sleep(0.4)
            try:
                if self.js("!!(window.SM && document.querySelector('#nav button') && "
                           "document.querySelector('.view:not([hidden])') && "
                           "document.readyState==='complete')"):
                    break
            except AssertionError:
                pass
        time.sleep(1.2)          # 차트·늦게 서는 섹션

    def shot(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        r = self.call("Page.captureScreenshot", format="png", captureBeyondViewport=False)
        path.write_bytes(base64.b64decode(r["data"]))
        return path

    def shot_full(self, path: Path) -> Path:
        """문서 전체(스크롤 아래까지)."""
        path.parent.mkdir(parents=True, exist_ok=True)
        m = self.call("Page.getLayoutMetrics")
        cs = m.get("cssContentSize") or m["contentSize"]
        w = int(self.js("window.innerWidth"))
        r = self.call("Page.captureScreenshot", format="png", captureBeyondViewport=True,
                      clip={"x": 0, "y": 0, "width": w, "height": min(int(cs["height"]), 6000),
                            "scale": 1})
        path.write_bytes(base64.b64decode(r["data"]))
        return path

    def close(self) -> None:
        self.ws.close()
        try:
            self.proc.terminate()
            self.proc.wait(10)
        except Exception:
            self.proc.kill()


# ── 화면 재기 ─────────────────────────────────────────────────────────────────────
# 지금 서 있는 화면(.view:not([hidden]))에서 잰다. 숨은 탭 패널은 display:none 이라 getBoundingClientRect
# 가 0 이라 저절로 빠진다.
MEASURE = r"""(function () {
  var vw = window.innerWidth, doc = document.documentElement;
  var box = document.querySelector('.view:not([hidden])');
  var out = {vw: vw, scrollW: Math.max(doc.scrollWidth, document.body.scrollWidth),
             view: box ? box.id : '', tabs: null, over: [], small: [], tiny: [], hscroll: []};
  if (!box) return out;
  var bar = box.querySelector(':scope > .vtabs');
  if (bar) {
    var r = bar.getBoundingClientRect(), bs = [].slice.call(bar.querySelectorAll('[role=tab]'))
      .filter(function (b) { return !b.hidden; });
    out.tabs = {n: bs.length, left: r.left, right: r.right, h: r.height,
                scrollable: bar.scrollWidth > bar.clientWidth + 1,
                clipped: bar.scrollWidth > bar.clientWidth + 1 &&
                         ['auto', 'scroll'].indexOf(getComputedStyle(bar).overflowX) < 0,
                wraps: new Set(bs.map(function (b) { return Math.round(b.getBoundingClientRect().top); })).size,
                labels: bs.map(function (b) { return b.textContent.trim(); }),
                btnH: Math.min.apply(null, bs.map(function (b) { return b.getBoundingClientRect().height; })),
                sels: bs.map(function (b) { return b.getAttribute('aria-selected'); })};
  }
  function vis(e) {
    var r = e.getBoundingClientRect();
    if (!r.width || !r.height) return null;
    var cs = getComputedStyle(e);
    if (cs.visibility === 'hidden' || cs.display === 'none') return null;
    return r;
  }
  // 가로로 미는 칸(표 래퍼·탭 줄) 안은 넘쳐도 정상 — 그 칸 안의 것은 건너뛴다.
  function inScroller(e) {
    for (var p = e.parentElement; p && p !== box.parentElement; p = p.parentElement) {
      var ox = getComputedStyle(p).overflowX;
      if ((ox === 'auto' || ox === 'scroll' || ox === 'hidden') && p.scrollWidth > p.clientWidth + 1) return p;
    }
    return null;
  }
  [].slice.call(box.querySelectorAll('button, a[href], input, select, textarea, summary, [role=tab], th, td, .chip, .pill'))
    .forEach(function (e) {
      var r = vis(e);
      if (!r) return;
      var tag = e.tagName.toLowerCase() + (e.id ? '#' + e.id : '') +
                (e.className && typeof e.className === 'string' ? '.' + e.className.trim().split(/\s+/).slice(0, 2).join('.') : '');
      var txt = (e.textContent || e.value || e.placeholder || '').trim().slice(0, 24);
      if (r.right > vw + 1 && !inScroller(e)) out.over.push(tag + ' "' + txt + '" right=' + Math.round(r.right));
      if (r.left < -1 && !inScroller(e)) out.over.push(tag + ' "' + txt + '" left=' + Math.round(r.left));
      if (/^(button|a|input|select|textarea|summary)$/.test(e.tagName.toLowerCase()) || e.getAttribute('role') === 'tab') {
        var type = (e.type || '').toLowerCase();
        if (type === 'hidden' || type === 'checkbox' || type === 'radio') return;
        if (e.tagName === 'A' && e.closest('p, li, td, .rsn, .why, .sub, .note')) return;   // 문장 속 링크는 제외
        if (r.height < 32 || (r.width < 32 && txt.length < 2)) out.small.push(tag + ' "' + txt + '" ' + Math.round(r.width) + 'x' + Math.round(r.height));
      }
    });
  // 13px 미만 글씨 — 보이는 글 노드가 있는 요소만
  var seen = {};
  var w = document.createTreeWalker(box, NodeFilter.SHOW_TEXT);
  var n;
  while ((n = w.nextNode())) {
    if (!n.nodeValue.trim()) continue;
    var e = n.parentElement;
    if (!e || /^(SCRIPT|STYLE)$/.test(e.tagName) || !vis(e)) continue;
    var fs = parseFloat(getComputedStyle(e).fontSize);
    if (fs < 12.5) {
      var k = e.tagName + '.' + (e.className || '') + '@' + fs;
      if (!seen[k]) { seen[k] = 1; out.tiny.push(k + ' "' + n.nodeValue.trim().slice(0, 16) + '"'); }
    }
  }
  [].slice.call(box.querySelectorAll('*')).forEach(function (e) {
    if (e.scrollWidth > e.clientWidth + 1 && ['auto', 'scroll'].indexOf(getComputedStyle(e).overflowX) >= 0 && vis(e))
      out.hscroll.push((e.className || e.tagName) + ' ' + e.scrollWidth + '>' + e.clientWidth);
  });
  // 문서 폭을 키운 장본인 — 오른쪽 끝이 뷰포트를 넘는 가장 바깥 요소들
  out.wide = [];
  [].slice.call(box.querySelectorAll('*')).forEach(function (e) {
    var r = vis(e);
    if (r && r.right > vw + 1 && !inScroller(e) && out.wide.length < 8)
      out.wide.push(e.tagName.toLowerCase() + (e.id ? '#' + e.id : '') + '.' +
                    String(e.className || '').trim().split(/\s+/).slice(0, 2).join('.') + ' right=' + Math.round(r.right));
  });
  return out;
})()"""

# 화면 id → (사람 이름). 순서는 점검표 순서다. 화면 목록의 정본은 view-def 이므로 여기서는 **고른 것만** 적고,
# 모든 id 가 실제 매니페스트에 있는지는 run 이 확인한다.
PHONE_VIEWS = ("settings", "triage", "overview", "aicite", "aisum", "competitors", "backlinks", "site")


def views_in_page(b: Browser) -> list[dict]:
    return b.js("SM.views.map(function (v) { return {id: v.id, title: v.title, "
                "modes: (v.modes || []).map(function (m) { return m.id; })}; })")


def open_view(b: Browser, vid: str, mode: str | None = None) -> None:
    b.js(f"SM.show({json.dumps(vid)}); window.scrollTo(0, 0);")
    time.sleep(0.5)
    if mode:
        b.js(f"SM.setMode({json.dumps(vid)}, {json.dumps(mode)}); window.scrollTo(0, 0);")
        time.sleep(0.6)


def measure(b: Browser) -> dict:
    return b.js(MEASURE)


def problems(label: str, m: dict, vw: int) -> list[str]:
    """측정값 → 실패 문장들. 작은 글씨·작은 터치 영역은 여기서 실패로 치지 않는다(보고용)."""
    out = []
    if m["scrollW"] > vw + SLACK:
        out.append(f"{label}: 문서가 가로로 넘친다 scrollWidth={m['scrollW']} > {vw} "
                   f"(범인 후보: {'; '.join(m.get('wide') or [])[:300]})")
    t = m.get("tabs")
    if t is not None:
        if t["n"] and (t["right"] > vw + SLACK or t["left"] < -SLACK) and not t["scrollable"]:
            out.append(f"{label}: 탭 줄이 뷰포트를 넘는데 가로로 밀 수도 없다 left={t['left']:.0f} right={t['right']:.0f}")
        if t["h"] < 28:
            out.append(f"{label}: 탭 줄 높이가 {t['h']:.0f}px — 안 보인다")
    if m["over"]:
        out.append(f"{label}: 뷰포트 밖으로 삐져나온 컨트롤 {len(m['over'])}개: " + "; ".join(m["over"][:5]))
    return out


# ── 서버 ──────────────────────────────────────────────────────────────────────────
def _targets(home: Path):
    """(이름, 서버를 세우는 것, 보는 사이트) — 서버는 대상을 볼 때만 선다(호스팅은 env 를 갈아 끼운다)."""
    import dashboard
    shell = dashboard.HTML.replace(b"</head>", R.PROBE.encode("utf-8") + b"</head>", 1)
    yield ("로컬", lambda: R._stdlib(shell))
    if (R.ROOT / "server" / "assets" / "dash.html").exists():
        yield ("호스팅", lambda: R.serve_hosted(home / "hosted"))


def run_matrix(home: Path, exe: str, widths, visit):
    """대상×폭 마다 브라우저를 한 번 세우고 visit(label, browser, base, w, h, mobile) 를 부른다."""
    for name, up in _targets(home):
        with up() as site:
            for (w, h) in widths:
                prof = Path(tempfile.mkdtemp(prefix="seo-miner-mobile-"))
                b = Browser(exe, prof)
                try:
                    b.viewport(w, h, mobile=(w <= 600))
                    b.goto(f"{site.base}/d#{R.SITES[1]}")
                    visit(name, b, site.base, w, h)
                finally:
                    b.close()
                    shutil.rmtree(prof, ignore_errors=True)


def run() -> None:
    exe = R.find_browser()
    if not exe:
        print("크롬/엣지가 없어 건너뜁니다")
        return
    home = Path(tempfile.mkdtemp(prefix="seo-miner-mobile-home-"))
    import doctor
    for k in (doctor.MODE_ENV, doctor.TOOL_ENV, doctor.TERMINAL_ENV):
        os.environ.pop(k, None)
    fails: list[str] = []
    try:
        R.fixture(home)
        R.zero_site(home)
        run_checks(home, exe, fails)
    finally:
        gc.collect()
        shutil.rmtree(home, ignore_errors=True)
    assert not fails, "모바일 폭 검사 실패:\n  " + "\n  ".join(fails)


def run_checks(home: Path, exe: str, fails: list[str]) -> None:
    def visit(name, b, base, w, h):
        have = {v["id"]: v for v in views_in_page(b)}
        for vid in PHONE_VIEWS:
            if vid not in have:
                fails.append(f"{name} {w}px: 화면 {vid} 가 매니페스트에 없다 — 이 검사의 화면 목록이 낡았다")
                continue
            modes = have[vid]["modes"] or [None]
            for mode in modes:
                label = f"{name} {w}px {vid}" + (f"[{mode}]" if mode else "")
                open_view(b, vid, mode)
                m = measure(b)
                fails.extend(problems(label, m, w))
                if have[vid]["modes"] and not m.get("tabs"):
                    fails.append(f"{label}: modes 를 선언했는데 탭 줄이 안 보인다")
                t = m.get("tabs")
                if t and t["btnH"] < 40:
                    fails.append(f"{label}: 탭 버튼 높이 {t['btnH']:.0f}px — 터치로 누르기 작다")
                if t and t["wraps"] > 1 and not t["scrollable"]:
                    fails.append(f"{label}: 탭이 {t['wraps']}줄로 갈라졌다 — 한 줄이어야 한다")
        # 설정 탭: 호스팅은 [이 PC] 가 없다(로컬만) — 서버마다 탭 이름이 갈린다
        open_view(b, "settings", "measure")
        labels = (measure(b).get("tabs") or {}).get("labels") or []
        if name == "호스팅" and any("이 PC" in x for x in labels):
            fails.append(f"호스팅 {w}px: 설정 탭에 [이 PC] 가 섰다 — {labels}")
        if name == "로컬" and not any("이 PC" in x for x in labels):
            fails.append(f"로컬 {w}px: 설정 탭에 [이 PC] 가 없다 — {labels}")
        # 칩 입력: 긴 말을 넣어도 상자·문서가 뷰포트를 안 넘는다
        r = b.js("""(function () {
          var t = [].slice.call(document.querySelectorAll('.tgbox input')).filter(function (x) { return x.getBoundingClientRect().width > 0; })[0];
          if (!t) return {err: '보이는 칩 입력칸이 없다'};
          t.focus();
          t.value = '아주아주긴경쟁사이름이면어떻게보일까요verylongcompetitornamewithoutspaces.example.com';
          t.dispatchEvent(new Event('input', {bubbles: true}));
          t.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', bubbles: true}));
          return {ok: 1};
        })()""")
        time.sleep(0.6)
        if r.get("err"):
            fails.append(f"{name} {w}px 설정: {r['err']}")
        else:
            c = b.js("""(function () {
              var o = {sw: Math.max(document.documentElement.scrollWidth, document.body.scrollWidth), boxes: []};
              [].slice.call(document.querySelectorAll('.tgbox')).forEach(function (x) {
                var r = x.getBoundingClientRect();
                if (r.width) o.boxes.push([Math.round(r.left), Math.round(r.right), x.scrollWidth > x.clientWidth + 1]);
              });
              return o;
            })()""")
            if c["sw"] > w + SLACK:
                fails.append(f"{name} {w}px 설정: 긴 칩을 넣자 문서가 넘친다 scrollWidth={c['sw']}")
            for l, rt, over in c["boxes"]:
                if rt > w + SLACK or l < -SLACK or over:
                    fails.append(f"{name} {w}px 설정: 칩 상자가 넘친다 left={l} right={rt} 안쪽넘침={over}")
        # 개요 서랍: 기회 하나를 열면 서랍이 뷰포트 안에 꽉 차고 안 넘친다
        open_view(b, "overview")
        b.js("""(function () { var q = [].slice.call(document.querySelectorAll('#view-overview button'))
          .filter(function (x) { return /기회 열기/.test(x.textContent); })[0]; if (q) q.click(); })()""")
        time.sleep(0.8)
        d = b.js("""(function () { var d = document.querySelector('.odr:not([hidden]), .sdr:not([hidden])');
          if (!d) return null; var r = d.getBoundingClientRect();
          return {l: r.left, r: r.right, t: r.top, b: r.bottom, sw: d.scrollWidth, cw: d.clientWidth, vh: innerHeight}; })()""")
        if not d:
            fails.append(f"{name} {w}px 개요: [기회 열기]를 눌러도 서랍이 안 열린다")
        else:
            if d["r"] > w + SLACK or d["l"] < -SLACK or d["sw"] > d["cw"] + SLACK:
                fails.append(f"{name} {w}px 개요 서랍이 뷰포트를 넘는다: {d}")
            if d["b"] > d["vh"] + SLACK:
                fails.append(f"{name} {w}px 개요 서랍이 아래로 넘는다: {d}")
        b.js("document.querySelectorAll('.odr-x,.sdr-x').forEach(function (x) { x.click(); })")
        time.sleep(0.4)
        # 심사 표: 체크 열이 검사어에서 멀리 떨어져 서면(남는 폭을 나눠 먹으면) 줄이 안 읽힌다
        open_view(b, "triage")
        c1 = b.js("""(function () { var tr = document.querySelector('#triage table tbody tr');
          if (!tr) return null; return Math.round(tr.children[0].getBoundingClientRect().width); })()""")
        if c1 is None:
            fails.append(f"{name} {w}px 심사: 표 줄이 없다 — 픽스처가 바뀌었다")
        elif c1 > 64:
            fails.append(f"{name} {w}px 심사: 체크 열이 {c1}px — 검색어와 떨어져 선다(44px 여야 한다)")
        print(f"  {name} {w}px: 측정 끝")
    run_matrix(home, exe, [PHONE], visit)


if __name__ == "__main__":
    run()
    print("mobile self-check ok")
