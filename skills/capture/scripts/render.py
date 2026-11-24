#!/usr/bin/env python3
"""헤드리스 렌더 경로 — 정적 HTML 로는 안 보이는 페이지를 브라우저로 열어 다시 읽는다.

collect_page 는 HTML 원문만 읽는다. 본문을 자바스크립트로 그리는 페이지(js_shell)는
그래서 본문·H1·구조화 데이터가 "없음"으로 읽히고, 요청문은 그 값을 '의심'으로만 싣는다.
이 모듈은 그 페이지를 헤드리스 크롬으로 한 번 열어:

  1. 렌더된 DOM 을 collect_page.audit_html 에 **그대로** 다시 넣는다(읽는 규칙은 한 벌).
  2. 원본 대 렌더 차이(js_gap) — 렌더 뒤에만 생기는 H1·schema·canonical — 를 남긴다.
  3. 여는 김에 잰다: 콘솔 오류 수, 모바일 가로 스크롤, 작은 글씨 비율, 작은 터치 대상,
     첫 화면을 덮는 고정 요소, 로드 중 history.pushState 호출 수.

**렌더 엔진이 없으면 아무것도 안 한다.** 호스팅 컨테이너에는 크롬이 없다 — 그때는
find_browser() 가 None 이고 감사 행은 한 글자도 안 바뀐다(js_shell=1 '의심' 그대로).
끄려면 SEO_MINER_RENDER=0, 브라우저를 지정하려면 SEO_MINER_BROWSER=<실행 파일>.

판정 문턱은 전부 공개 문서의 숫자다(지어낸 가중치·점수 없음):
  · 모바일 화면: 412×823, DPR 1.75, UA 는 Lighthouse 의 모바일 기본값
    https://github.com/GoogleChrome/lighthouse/blob/main/core/config/constants.js
    (MOTOGPOWER_EMULATION_METRICS · MOTOG4_USERAGENT)
  · 작은 글씨: 12px 미만 글자의 비율. Lighthouse font-size 감사의 기준이었다("40% or
    more of the text has a font size smaller than 12 px" 이면 실패). 이 감사는
    Lighthouse 13 에서 빠졌다 — 그래서 PSI 로는 더 못 받고 여기서 직접 잰다.
    https://developer.chrome.com/docs/lighthouse/seo/font-size
  · 터치 대상: 24×24 CSS px(WCAG 2.2 SC 2.5.8, axe target-size). Lighthouse 12 가
    tap-targets(48px)를 이 감사로 바꿨다. 문장 안 인라인 링크는 규칙의 예외라 안 센다.
    간격 예외(주변이 비어 있으면 통과)는 안 본다 — 그래서 "실패"가 아니라 "개수"다.
    https://dequeuniversity.com/rules/axe/4.10/target-size
    https://github.com/GoogleChrome/lighthouse/releases/tag/v12.0.0
  · 전면 광고: 구글은 화면 비율 문턱을 주지 않는다("Don't obscure the entire page with
    interstitials"). 그래서 판정하지 않고 고정 요소가 첫 화면을 덮는 비율만 적는다.
    https://developers.google.com/search/docs/appearance/avoid-intrusive-interstitials
  · 뒤로가기 가로채기: 2026-04-13 발표, 2026-06-15 시행된 스팸 정책(악의적 관행).
    "Back button hijacking is when a site interferes with user browser navigation by
    manipulating the browser history". pushState 자체는 SPA 라우팅이라 위반이 아니다 —
    정보 한 줄로만 싣는다(기회를 만들지 않는다).
    https://developers.google.com/search/docs/essentials/spam-policies
    https://developers.google.com/search/blog/2026/04/back-button-hijacking

Usage:
  python render.py URL          # 한 장 열어 잰 값을 JSON 으로
  python render.py              # self-check (크롬이 없으면 렌더 부분만 건너뜀)
"""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

# test_render.BROWSERS 와 같은 목록이다(test_seams 가 대조한다) — 검사가 찾는 브라우저와
# 수집이 찾는 브라우저가 다르면 "검사는 도는데 렌더는 안 되는" 머신이 생긴다.
BROWSERS = (
    "chrome", "chromium", "google-chrome", "google-chrome-stable", "msedge",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
)

# Lighthouse 모바일 기본값 — 출처는 모듈 docstring.
MOBILE = {"width": 412, "height": 823, "deviceScaleFactor": 1.75, "mobile": True}
MOBILE_UA = ("Mozilla/5.0 (Linux; Android 11; moto g power (2022)) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/136.0.0.0 Mobile Safari/537.36")
SMALL_FONT_PX = 12          # Lighthouse font-size 감사의 글자 크기 기준
SMALL_TEXT_FAIL_PCT = 40.0  # 같은 감사의 실패선("40% or more")
TARGET_MIN_PX = 24          # WCAG 2.2 SC 2.5.8 / axe target-size

LOAD_TIMEOUT = 25.0         # 초 — 한 장을 여는 데 쓰는 시간 전부
SETTLE = 2.0                # 초 — load 뒤 클라이언트 렌더를 기다리는 시간
RENDER_MAX = 10             # 한 회차에 다시 읽는 js_shell 페이지 수
MEASURE_MAX = 5             # 한 회차에 재기만 하는(껍데기가 아닌) 페이지 수
PARALLEL = 2                # 동시에 띄우는 브라우저 수
MAX_HTML = 2_000_000        # collect_page.MAX_HTML 과 같은 상한 — 그 이상은 어차피 안 읽는다


def find_browser() -> str | None:
    """쓸 수 있는 크롬 계열 실행 파일. 없으면 None — 그러면 렌더 경로 전체가 꺼진다."""
    if os.environ.get("SEO_MINER_RENDER", "").strip() == "0":
        return None
    forced = os.environ.get("SEO_MINER_BROWSER", "").strip()
    if forced:
        return forced if Path(forced).exists() else shutil.which(forced)
    for c in BROWSERS:
        p = (c if Path(c).exists() else None) if os.path.sep in c else shutil.which(c)
        if p:
            return p
    return None


# ── 원문만으로 보는 것 ────────────────────────────────────────────────────────
_SCRIPT = re.compile(r"<script\b([^>]*)>(.*?)</script\s*>", re.I | re.S)
_PUSH = re.compile(r"\bhistory\s*\.\s*pushState\s*\(")


def pushstate_inline(html: str) -> int:
    """첫 로드의 **인라인** 스크립트 중 history.pushState 를 부르는 것의 수.

    외부 번들(src=)은 안 센다 — SPA 라우터가 전부 거기 있어 오탐이 된다. 인라인에서
    부르는 것만이 "페이지가 열리자마자 기록을 건드릴 수 있는" 자리다. 그래도 위반
    판정이 아니다: 정보 한 줄이다(모듈 docstring 의 정책 출처).
    """
    n = 0
    for attrs, body in _SCRIPT.findall(html or ""):
        if re.search(r"\bsrc\s*=", attrs, re.I):
            continue
        if _PUSH.search(body):
            n += 1
    return n


def _jl(v) -> list:
    try:
        got = json.loads(v) if isinstance(v, str) else v
    except ValueError:
        return []
    return got if isinstance(got, list) else []


def js_gap(raw: dict, rendered: dict) -> dict:
    """원본 감사 행 대 렌더 감사 행 — 렌더 뒤에만 생긴 것. 둘 다 audit_html 의 꼴이다.

    h1·schema 는 렌더 뒤에만 있는 항목(원문에 이미 있던 것은 뺀다), canonical 은 원문에
    없거나 다를 때만 싣는다. 빈 dict 이 아니라 words 는 늘 싣는다 — "차이 없음"도 본 결과다.
    """
    r_h1, d_h1 = _jl(raw.get("h1_json")), _jl(rendered.get("h1_json"))
    r_sc, d_sc = _jl(raw.get("schema_json")), _jl(rendered.get("schema_json"))
    gap: dict = {"words": [raw.get("words") or 0, rendered.get("words") or 0],
                 "h1": [x for x in d_h1 if x not in r_h1][:5],
                 "schema": [x for x in d_sc if x not in r_sc][:20]}
    rc, dc = raw.get("canonical") or "", rendered.get("canonical") or ""
    if dc and dc != rc:
        gap["canonical"] = [rc, dc]
    rt, dt = raw.get("title") or "", rendered.get("title") or ""
    if dt and dt != rt:
        gap["title"] = [rt, dt]
    return gap


# ── CDP — stdlib 만으로 (websocket 의존성을 안 늘린다) ───────────────────────
class _WS:
    """DevTools 와 말할 만큼만 하는 websocket 클라이언트(RFC 6455, 텍스트 프레임)."""

    def __init__(self, url: str, timeout: float = 10.0):
        m = re.match(r"ws://([^:/]+):(\d+)(/.*)$", url)
        if not m:
            raise ValueError(f"ws 주소가 아니다: {url}")
        host, port, path = m.group(1), int(m.group(2)), m.group(3)
        self.s = socket.create_connection((host, port), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        # Origin 을 안 보낸다 — 크롬 111+ 는 Origin 이 있으면 --remote-allow-origins 를 요구한다.
        self.s.sendall((f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\n"
                        "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                        f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        self.buf = b""
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.s.recv(4096)
            if not chunk:
                raise ConnectionError("websocket 핸드셰이크 도중 끊겼다")
            head += chunk
        head, self.buf = head.split(b"\r\n\r\n", 1)
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise ConnectionError(head[:120].decode("latin-1"))

    def send(self, text: str, opcode: int = 1) -> None:
        data = text.encode()
        n, mask = len(data), os.urandom(4)
        head = bytes([0x80 | opcode])
        if n < 126:
            head += bytes([0x80 | n])
        elif n < 65536:
            head += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            head += bytes([0x80 | 127]) + struct.pack(">Q", n)
        self.s.sendall(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def _read(self, n: int) -> bytes:
        while len(self.buf) < n:
            chunk = self.s.recv(65536)
            if not chunk:
                raise ConnectionError("websocket 이 끊겼다")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def recv(self, timeout: float) -> str | None:
        """텍스트 메시지 하나. 시간 안에 안 오면 None."""
        self.s.settimeout(max(0.05, timeout))
        parts: list[bytes] = []
        try:
            while True:
                b1, b2 = self._read(2)
                n = b2 & 0x7F
                if n == 126:
                    n = struct.unpack(">H", self._read(2))[0]
                elif n == 127:
                    n = struct.unpack(">Q", self._read(8))[0]
                mask = self._read(4) if b2 & 0x80 else b""
                data = self._read(n)
                if mask:
                    data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
                op = b1 & 0x0F
                if op == 8:
                    raise ConnectionError("websocket 이 닫혔다")
                if op == 9:                      # ping → pong
                    self.send(data.decode("utf-8", "replace"), opcode=10)
                    continue
                if op == 10:
                    continue
                parts.append(data)
                if b1 & 0x80:
                    return b"".join(parts).decode("utf-8", "replace")
                self.s.settimeout(10.0)          # 조각난 메시지의 나머지는 끝까지 받는다
        except socket.timeout:
            if parts:
                raise ConnectionError("메시지 도중 시간 초과")
            return None

    def close(self) -> None:
        try:
            self.s.close()
        except OSError:
            pass


# 페이지 스크립트보다 먼저 심는다 — 로드 중 history.pushState 호출을 센다.
_PRELOAD_JS = """(() => {
  let n = 0; const o = history.pushState;
  history.pushState = function () { n++; window.__sm_push = n; return o.apply(this, arguments); };
})()"""

# 렌더가 끝난 뒤 한 번 — 재는 것 전부. 문턱 숫자는 파이썬 상수에서 끼운다(두 벌 금지).
_MEASURE_JS = """(() => {
  const vw = window.innerWidth, vh = window.innerHeight, de = document.documentElement;
  const body = document.body || de;
  const hidden = (cs) => cs.display === 'none' || cs.visibility === 'hidden';
  let total = 0, small = 0, guard = 0, n;
  const w = document.createTreeWalker(body, NodeFilter.SHOW_TEXT);
  while ((n = w.nextNode()) && guard++ < 20000) {
    const t = n.nodeValue.trim(); if (!t) continue;
    const el = n.parentElement; if (!el) continue;
    if (['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE'].includes(el.tagName)) continue;
    const cs = getComputedStyle(el);
    if (hidden(cs) || !el.getClientRects().length) continue;
    total += t.length; if (parseFloat(cs.fontSize) < %(font)d) small += t.length;
  }
  let targets = 0, tiny = 0;
  const sel = 'a[href],button,input:not([type=hidden]),select,textarea,[role=button],[role=link]';
  for (const el of body.querySelectorAll(sel)) {
    if (targets >= 5000) break;
    const cs = getComputedStyle(el);
    if (hidden(cs) || cs.display === 'inline') continue;
    const r = el.getBoundingClientRect(); if (!r.width || !r.height) continue;
    targets++; if (r.width < %(target)d || r.height < %(target)d) tiny++;
  }
  let cover = 0, k = 0;
  for (const el of body.querySelectorAll('*')) {
    if (k++ > 8000) break;
    const cs = getComputedStyle(el);
    if (cs.position !== 'fixed' || hidden(cs) || parseFloat(cs.opacity) === 0
        || cs.pointerEvents === 'none') continue;
    const r = el.getBoundingClientRect();
    const cw = Math.max(0, Math.min(r.right, vw) - Math.max(r.left, 0));
    const ch = Math.max(0, Math.min(r.bottom, vh) - Math.max(r.top, 0));
    cover = Math.max(cover, (cw * ch) / (vw * vh));
  }
  return JSON.stringify({
    html: de.outerHTML.slice(0, %(max_html)d),
    vw: vw, scroll_w: Math.max(de.scrollWidth, body.scrollWidth),
    text: total, small_text: small, targets: targets, small_targets: tiny,
    fixed_cover: cover, pushstate: window.__sm_push || 0 });
})()""" % {"font": SMALL_FONT_PX, "target": TARGET_MIN_PX, "max_html": MAX_HTML}


def _launch(browser: str, profile: Path) -> tuple[subprocess.Popen, int]:
    args = [browser, "--headless=new", "--disable-gpu", "--remote-debugging-port=0",
            f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check",
            "--mute-audio", "--disable-extensions", "about:blank"]
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        args.insert(1, "--no-sandbox")           # root 로 도는 컨테이너에서만
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    port_file = profile / "DevToolsActivePort"
    end = time.time() + 20
    while time.time() < end:
        if proc.poll() is not None:
            raise RuntimeError(f"브라우저가 바로 끝났다(exit {proc.returncode})")
        try:
            port = int(port_file.read_text().split()[0])
            return proc, port
        except (OSError, ValueError, IndexError):
            time.sleep(0.1)
    proc.kill()
    raise RuntimeError("브라우저 디버그 포트가 안 열렸다")


def _page_ws(port: int) -> str:
    end = time.time() + 10
    while True:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=5) as r:
                for t in json.loads(r.read().decode()):
                    if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
                        return t["webSocketDebuggerUrl"]
        except (OSError, ValueError):
            pass
        if time.time() > end:
            raise RuntimeError("브라우저에 열린 탭이 없다")
        time.sleep(0.2)


def render_page(browser: str, url: str, *, mobile: bool = True,
                timeout: float = LOAD_TIMEOUT, settle: float = SETTLE) -> dict:
    """URL 한 장을 헤드리스로 열어 렌더된 DOM 과 잰 값을 돌려준다. 예외를 안 던진다.

    성공: {"html", "console_errors", "hscroll_px", "small_text_pct", "targets",
           "small_targets", "fixed_cover_pct", "pushstate_load", "loaded"}
    실패: {"error": 사유} — 부르는 쪽은 감사 행을 그대로 둔다.
    """
    profile = Path(tempfile.mkdtemp(prefix="sm-render-"))
    proc = ws = None
    try:
        proc, port = _launch(browser, profile)
        ws = _WS(_page_ws(port))
        seq = 0
        errors = 0
        loaded = False

        def on_event(msg: dict) -> None:
            nonlocal errors, loaded
            m, p = msg.get("method"), msg.get("params") or {}
            if m == "Runtime.exceptionThrown":
                errors += 1
            elif m == "Runtime.consoleAPICalled" and p.get("type") == "error":
                errors += 1
            elif m == "Log.entryAdded" and (p.get("entry") or {}).get("level") == "error":
                errors += 1
            elif m == "Page.loadEventFired":
                loaded = True

        def call(method: str, params: dict | None = None, wait: float = 15.0) -> dict:
            nonlocal seq
            seq += 1
            me = seq
            ws.send(json.dumps({"id": me, "method": method, "params": params or {}}))
            end = time.time() + wait
            while time.time() < end:
                raw = ws.recv(end - time.time())
                if raw is None:
                    break
                msg = json.loads(raw)
                if msg.get("id") == me:
                    if "error" in msg:
                        raise RuntimeError(f"{method}: {msg['error'].get('message')}")
                    return msg.get("result") or {}
                on_event(msg)
            raise TimeoutError(f"{method} 응답 없음")

        for m in ("Page.enable", "Runtime.enable", "Log.enable"):
            call(m)
        if mobile:
            call("Emulation.setDeviceMetricsOverride", MOBILE)
            call("Emulation.setUserAgentOverride", {"userAgent": MOBILE_UA})
        call("Page.addScriptToEvaluateOnNewDocument", {"source": _PRELOAD_JS})
        nav = call("Page.navigate", {"url": url})
        if nav.get("errorText"):
            return {"error": f"열지 못함: {nav['errorText']}"[:200]}
        end = time.time() + timeout
        settle_end = None
        while time.time() < end:
            if loaded and settle_end is None:
                settle_end = time.time() + settle
            if settle_end is not None and time.time() >= settle_end:
                break
            raw = ws.recv(min(0.25, max(0.05, end - time.time())))
            if raw is not None:
                on_event(json.loads(raw))
        got = call("Runtime.evaluate", {"expression": _MEASURE_JS, "returnByValue": True})
        val = (got.get("result") or {}).get("value")
        if not isinstance(val, str):
            return {"error": "렌더된 문서를 못 읽음"}
        m = json.loads(val)
        text = m.get("text") or 0
        return {"html": m.get("html") or "", "loaded": loaded, "console_errors": errors,
                # 기기 폭과 견준다(innerWidth 가 아니다) — 모바일 브라우저는 넘치는 문서를
                # 축소해 맞추느라 innerWidth 를 키운다. viewport 메타가 없는 페이지(980px 로
                # 그림)도 그래서 여기 잡힌다. Lighthouse content-width 가 보던 것과 같은 현상이다.
                "hscroll_px": max(0, int(max(m.get("scroll_w") or 0, m.get("vw") or 0)
                                         - (MOBILE["width"] if mobile else (m.get("vw") or 0)))),
                # 글자가 하나도 없으면 비율이 없다(None) — 0% 라고 하면 "전부 크다"가 된다.
                "small_text_pct": (round(100.0 * (m.get("small_text") or 0) / text, 1)
                                   if text else None),
                "targets": int(m.get("targets") or 0),
                "small_targets": int(m.get("small_targets") or 0),
                "fixed_cover_pct": round(100.0 * float(m.get("fixed_cover") or 0), 1),
                "pushstate_load": int(m.get("pushstate") or 0)}
    except Exception as e:                       # 렌더는 부가 경로다 — 수집을 죽이지 않는다
        return {"error": f"{type(e).__name__}: {e}"[:200]}
    finally:
        if ws is not None:
            try:
                ws.send(json.dumps({"id": 0, "method": "Browser.close"}))
            except OSError:
                pass
            ws.close()
        if proc is not None:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
        shutil.rmtree(profile, ignore_errors=True)


# ── collect_page 가 거는 훅 ──────────────────────────────────────────────────
METRIC_COLS = ("console_errors", "hscroll_px", "small_text_pct", "targets",
               "small_targets", "fixed_cover_pct", "pushstate_load")


class Session:
    """한 회차의 렌더 예산. collect_page.collect 가 하나 만들어 일꾼들이 같이 쓴다.

    reread() 는 일꾼 스레드에서 불린다(네트워크만, Brain 을 안 만진다) — 그래서 예산은
    잠금 아래에서 세고, 브라우저는 PARALLEL 개까지만 동시에 띄운다.
    """

    def __init__(self, browser: str | None = "auto", *, render=render_page):
        self.browser = find_browser() if browser == "auto" else browser
        self._render = render
        self._lock = threading.Lock()
        self._gate = threading.Semaphore(PARALLEL)
        self.shells = 0          # 다시 읽은 js_shell 페이지
        self.measured = 0        # 재기만 한 페이지
        self.failed = 0

    def _take(self, shell: bool) -> bool:
        with self._lock:
            if shell and self.shells < RENDER_MAX:
                self.shells += 1
                return True
            if not shell and self.measured < MEASURE_MAX:
                self.measured += 1
                return True
        return False

    def reread(self, url: str, row: dict, audit_html) -> dict:
        """감사 행 한 줄 → (렌더 경로가 있으면) 렌더해 다시 읽은 행.

        js_shell 이면 렌더된 DOM 을 audit_html 에 넣은 행으로 **바꾼다** — 본문·H1·schema
        가 이제 실제 값이고, js_shell 도 그 DOM 으로 다시 판정된다(여전히 비면 1 그대로).
        껍데기가 아니면 행은 그대로 두고 잰 값만 붙인다. 잰 값은 "_render" 에 싣는다 —
        page_audits 칸이 아니라 write_page_audits 가 버리고, save() 가 page_renders 에 적는다.
        브라우저가 없거나 예산 밖이거나 렌더가 실패하면 받은 행을 그대로 돌려준다.
        """
        if not self.browser or row.get("error") or row.get("words") is None:
            return row
        shell = bool(row.get("js_shell"))
        if not self._take(shell):
            return row
        with self._gate:
            got = self._render(self.browser, url)
        if got.get("error"):
            with self._lock:
                self.failed += 1
            return {**row, "_render": {"error": got["error"]}}
        extra = {c: got.get(c) for c in METRIC_COLS}
        if not shell:
            return {**row, "_render": extra}
        new = audit_html(url, got.get("html") or "", row.get("status"))
        extra["js_gap"] = js_gap(row, new)
        # 인라인 pushState 는 **원문**의 사실이다 — 렌더된 DOM 의 값으로 덮지 않는다.
        if "pushstate_inline" in row:
            new["pushstate_inline"] = row["pushstate_inline"]
        return {**new, "_render": extra}

    @property
    def summary(self) -> str:
        if not self.browser:
            return "렌더 엔진 없음 — js_shell 페이지는 '의심'으로 남습니다"
        return (f"렌더 {self.shells}장 다시 읽음 · {self.measured}장 측정"
                + (f" · 실패 {self.failed}" if self.failed else ""))

    def save(self, conn, project_id: int, checked_date: str, rows) -> int:
        return save(conn, project_id, checked_date, rows)


def save(conn, project_id: int, checked_date: str, rows) -> int:
    """감사 행들에 붙어 온 렌더 결과·인라인 pushState 를 page_renders 에 적는다.

    못 가져온 행(error)은 안 적는다. rendered=0 인 줄은 "정적 HTML 만 봤다"는 뜻이다 —
    그 줄의 측정 칸은 NULL(안 쟀다)이지 0 이 아니다.
    """
    out = []
    for r in rows:
        if r.get("error") or "pushstate_inline" not in r and "_render" not in r:
            continue
        x = r.get("_render") or {}
        ok = bool(x) and not x.get("error")
        out.append((project_id, checked_date, r["url"], int(ok), x.get("error"),
                    json.dumps(x["js_gap"], ensure_ascii=False) if x.get("js_gap") else None,
                    *(x.get(c) if ok else None for c in METRIC_COLS),
                    r.get("pushstate_inline")))
    cols = ("rendered", "error", "js_gap_json", *METRIC_COLS, "pushstate_inline")
    conn.executemany(
        f"""INSERT INTO page_renders(project_id, checked_date, url, {', '.join(cols)})
            VALUES(?,?,?,{','.join('?' * len(cols))})
            ON CONFLICT(project_id, checked_date, url) DO UPDATE SET
              {', '.join(f'{c}=excluded.{c}' for c in cols)}""", out)
    conn.commit()
    return len(out)


def latest(conn, project_id: int) -> dict[str, dict]:
    """주소 → 가장 최근 렌더 줄. 표가 없는 옛 Brain 에서는 빈 dict(읽는 쪽을 안 죽인다)."""
    try:
        rows = conn.execute(
            """SELECT a.* FROM page_renders a WHERE a.project_id=? AND a.checked_date = (
                 SELECT MAX(b.checked_date) FROM page_renders b
                  WHERE b.project_id=a.project_id AND b.url=a.url)""", (project_id,)).fetchall()
    except Exception:
        return {}
    return {r["url"]: dict(r) for r in rows}


def attach(conn, project_id: int, audit: dict, _cache: dict | None = None) -> None:
    """감사 한 줄에 그 주소의 렌더 줄을 붙인다(audit["render"]). 없으면 안 붙인다."""
    got = latest(conn, project_id) if _cache is None else _cache
    r = got.get(audit.get("url"))
    if r:
        audit["render"] = r


# ── 요청문에 싣는 줄 — 사실만. 판정·기회는 만들지 않는다 ─────────────────────
def brief_lines(audit: dict) -> list[str]:
    """감사 행(audit["render"] 가 붙은) → 요청문의 정보 줄들. 렌더 줄이 없으면 빈 목록."""
    r = audit.get("render") or {}
    L: list[str] = []
    if not r:
        return L
    if r.get("rendered"):
        gap = {}
        try:
            gap = json.loads(r.get("js_gap_json") or "{}")
        except ValueError:
            pass
        if gap:
            w = gap.get("words") or [0, 0]
            bits = [f"본문 {w[0]}단어 → {w[1]}단어"]
            if gap.get("h1"):
                bits.append("H1 " + ", ".join(f"'{x}'" for x in gap["h1"][:2]))
            if gap.get("schema"):
                bits.append("구조화 데이터 " + ", ".join(gap["schema"][:6]))
            if gap.get("canonical"):
                bits.append(f"canonical {gap['canonical'][1]}")
            L.append("- 렌더 차이: 이 페이지는 자바스크립트가 돈 뒤에야 내용이 채워집니다. "
                     "원본 HTML 대 브라우저로 연 결과: " + " · ".join(bits) + ". "
                     "위의 본문·H1·구조화 데이터는 브라우저로 연 뒤의 값입니다. "
                     "원본 HTML 만 읽는 크롤러(대부분의 AI 봇)에게는 렌더 전 값만 보입니다.")
        m = []
        if isinstance(r.get("console_errors"), int):
            m.append(f"콘솔 오류 {r['console_errors']}건")
        if isinstance(r.get("hscroll_px"), int):
            m.append(f"화면 폭보다 {r['hscroll_px']}px 넓게 그려짐(가로 스크롤)"
                     if r["hscroll_px"] else "가로 스크롤 없음")
        if r.get("small_text_pct") is not None:
            pct = float(r["small_text_pct"])
            m.append(f"{SMALL_FONT_PX}px 미만 글자 {pct:g}%"
                     + (f"(Lighthouse 기준 {SMALL_TEXT_FAIL_PCT:g}% 이상이면 읽기 어려움)"
                        if pct >= SMALL_TEXT_FAIL_PCT else ""))
        if isinstance(r.get("targets"), int) and r["targets"]:
            m.append(f"{TARGET_MIN_PX}px 보다 작은 터치 대상 {r.get('small_targets') or 0}/"
                     f"{r['targets']}개")
        if r.get("fixed_cover_pct"):
            m.append(f"고정 요소가 첫 화면의 {float(r['fixed_cover_pct']):g}%를 덮음")
        if m:
            L.append(f"- 모바일 화면({MOBILE['width']}×{MOBILE['height']})으로 연 결과: "
                     + " · ".join(m))
    n_load, n_inline = r.get("pushstate_load") or 0, r.get("pushstate_inline") or 0
    if n_load or n_inline:
        what = (f"로드 중 history.pushState 가 {n_load}번 불렸습니다" if n_load
                else f"인라인 스크립트 {n_inline}곳이 history.pushState 를 부릅니다")
        L.append(f"- 참고(정보): {what}. 화면 전환에 흔히 쓰는 기능이라 그 자체로는 문제가 "
                 "아닙니다. 다만 방문자가 뒤로가기를 눌렀을 때 온 곳으로 못 돌아가게 기록을 "
                 "끼워 넣는 것은 구글 스팸 정책 위반입니다(뒤로가기 가로채기, 2026-06-15 "
                 "시행). 광고·외부 스크립트가 넣은 것일 수 있으니 뒤로가기를 한 번 눌러 "
                 "확인합니다.")
    return L


# ── 자체점검 ─────────────────────────────────────────────────────────────────
_SHELL_PAGE = """<!doctype html><html lang="en"><head><title>Shell</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<script>history.pushState({}, '', '#trap');</script>
</head><body><div id="root"></div>
<script>
console.error('boom');
const r = document.getElementById('root');
r.innerHTML = '<h1>Rendered headline</h1><p style="font-size:10px">' + 'tiny words here '.repeat(60)
  + '</p><p>' + 'normal sized body words '.repeat(60) + '</p>'
  + '<div style="width:900px;height:10px;background:#eee"></div>'
  + '<button style="width:16px;height:16px;padding:0;border:0"></button>'
  + '<button style="width:60px;height:40px">ok</button>'
  + '<div style="position:fixed;left:0;top:0;width:100%;height:50%;background:#000"></div>';
const l = document.createElement('link'); l.rel = 'canonical'; l.href = 'https://x.example/shell';
document.head.appendChild(l);
const s = document.createElement('script'); s.type = 'application/ld+json';
s.textContent = JSON.stringify({'@context': 'https://schema.org', '@type': 'Article', headline: 'x'});
document.head.appendChild(s);
</script></body></html>"""


def _selfcheck() -> None:
    import sqlite3
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    sys.path.insert(0, str(Path(__file__).parent))
    import collect_page
    import db

    # ① 원문만으로 보는 것 — 인라인만 센다(외부 번들은 SPA 라우터라 오탐)
    assert pushstate_inline(_SHELL_PAGE) == 1
    assert pushstate_inline('<script src="/app.js">history.pushState(1)</script>') == 0
    assert pushstate_inline("<script>history.replaceState({}, '')</script>") == 0
    assert pushstate_inline("") == 0

    # ② 차이 — 렌더 뒤에만 생긴 것만
    raw = collect_page.audit_html("https://x.example/shell", _SHELL_PAGE)
    assert raw["js_shell"] == 1 and raw["pushstate_inline"] == 1, raw
    done = collect_page.audit_html(
        "https://x.example/shell",
        "<html><head><title>Shell</title><link rel=canonical href='https://x.example/shell'>"
        "<script type='application/ld+json'>{\"@type\":\"Article\"}</script></head>"
        "<body><h1>Rendered headline</h1><p>" + "word " * 300 + "</p></body></html>")
    gap = js_gap(raw, done)
    assert gap["h1"] == ["Rendered headline"] and gap["schema"] == ["Article"], gap
    assert gap["canonical"] == ["", "https://x.example/shell"] and "title" not in gap, gap
    assert gap["words"][0] < collect_page.JS_SHELL_WORDS <= gap["words"][1], gap
    assert js_gap(done, done) == {"words": [done["words"]] * 2, "h1": [], "schema": []}

    # ③ 렌더 엔진이 없으면 한 글자도 안 바뀐다(호스팅 컨테이너)
    off = Session(browser=None)
    assert off.reread("https://x.example/shell", raw, collect_page.audit_html) is raw
    assert "없음" in off.summary
    old = os.environ.get("SEO_MINER_RENDER")
    os.environ["SEO_MINER_RENDER"] = "0"
    try:
        assert find_browser() is None
    finally:
        os.environ.pop("SEO_MINER_RENDER") if old is None else os.environ.update(SEO_MINER_RENDER=old)

    # ④ 훅 — 가짜 렌더로: 껍데기는 행을 바꾸고, 아니면 재기만, 실패·예산 밖은 그대로
    fake_html = ("<html><head><title>Shell</title></head><body><h1>Rendered headline</h1><p>"
                 + "word " * 300 + "</p></body></html>")
    calls = []

    def fake(browser, url):
        calls.append(url)
        if "fail" in url:
            return {"error": "열지 못함: net::ERR"}
        return {"html": fake_html, "console_errors": 2, "hscroll_px": 30,
                "small_text_pct": 45.0, "targets": 4, "small_targets": 1,
                "fixed_cover_pct": 50.0, "pushstate_load": 1, "loaded": True}

    ses = Session(browser="fake", render=fake)
    got = ses.reread("https://x.example/shell", raw, collect_page.audit_html)
    assert got["js_shell"] == 0 and got["words"] >= 300, "렌더된 DOM 으로 다시 읽지 않았다"
    assert got["pushstate_inline"] == 1, "원문의 인라인 pushState 를 렌더 값으로 덮었다"
    assert got["_render"]["js_gap"]["h1"] == ["Rendered headline"], got["_render"]
    plain = dict(done, url="https://x.example/plain")
    kept = ses.reread(plain["url"], plain, collect_page.audit_html)
    assert kept["words"] == plain["words"] and "js_gap" not in kept["_render"], kept["_render"]
    bad = ses.reread("https://x.example/fail", dict(raw, url="https://x.example/fail"),
                     collect_page.audit_html)
    assert bad["js_shell"] == 1 and bad["_render"] == {"error": "열지 못함: net::ERR"}, bad
    err_row = {"url": "https://x.example/404", "status": 404, "error": "HTTP 404"}
    assert ses.reread(err_row["url"], err_row, collect_page.audit_html) is err_row
    n0 = len(calls)
    for i in range(MEASURE_MAX + 3):
        ses.reread(f"https://x.example/p{i}", plain, collect_page.audit_html)
    assert len(calls) - n0 == MEASURE_MAX - 1, "측정 상한을 넘겨 브라우저를 띄웠다"

    # ⑤ Brain 왕복 — 적고, 주소마다 최신 한 줄을 읽고, 요청문 줄이 선다
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    conn.execute("INSERT INTO projects(id,name,type,domain) VALUES(1,'r','saas','x.example')")
    static_only = dict(done, url="https://x.example/static", pushstate_inline=0)
    assert save(conn, 1, "2026-11-01", [got, kept, bad, err_row, static_only]) == 4
    assert save(conn, 1, "2026-11-01", [got]) == 1, "같은 날 다시 적으면 덮어쓴다(upsert)"
    L = latest(conn, 1)
    assert set(L) == {got["url"], plain["url"], bad["url"], static_only["url"]}, set(L)
    assert L[got["url"]]["rendered"] == 1 and L[got["url"]]["console_errors"] == 2
    assert L[bad["url"]]["rendered"] == 0 and L[bad["url"]]["console_errors"] is None, \
        "렌더 실패 줄의 측정 칸이 0 으로 찼다(안 쟀다 ≠ 0)"
    assert L[static_only["url"]]["rendered"] == 0 and L[static_only["url"]]["pushstate_inline"] == 0
    a = {"url": got["url"]}
    attach(conn, 1, a)
    lines = "\n".join(brief_lines(a))
    for must in ("렌더 차이", "Rendered headline", "콘솔 오류 2건", "화면 폭보다 30px 넓게",
                 "12px 미만 글자 45%", "작은 터치 대상 1/4개", "첫 화면의 50%", "pushState 가 1번"):
        assert must in lines, f"요청문 줄에 '{must}' 가 없다:\n{lines}"
    assert brief_lines({"url": "x"}) == [] and brief_lines({"render": L[static_only["url"]]}) == []
    b = {"url": bad["url"]}
    attach(conn, 1, b)
    only_inline = "\n".join(brief_lines(b))
    assert "인라인 스크립트 1곳" in only_inline and "렌더 차이" not in only_inline, only_inline
    conn.close()

    # ⑥ 실제 브라우저 — 있을 때만. 껍데기 페이지를 로컬에 띄워 끝에서 끝까지 연다.
    browser = find_browser()
    if not browser:
        print("selfcheck ok — 브라우저가 없어 실제 렌더는 건너뜀")
        return

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            body = _SHELL_PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}/shell"
        real = Session(browser=browser).reread(url, dict(raw, url=url), collect_page.audit_html)
        x = real.get("_render") or {}
        assert not x.get("error"), f"실제 렌더 실패: {x}"
        assert real["js_shell"] == 0 and real["words"] > 300, (real["js_shell"], real["words"])
        assert x["js_gap"]["h1"] == ["Rendered headline"], x["js_gap"]
        assert x["js_gap"]["schema"] == ["Article"], x["js_gap"]
        assert x["js_gap"]["canonical"][1] == "https://x.example/shell", x["js_gap"]
        assert x["console_errors"] >= 1, x
        assert x["hscroll_px"] > 0, f"900px 상자가 412px 화면을 안 넘쳤다: {x}"
        assert 30 < x["small_text_pct"] < 50, x     # 10px 문단이 글자의 약 40%
        assert (x["targets"], x["small_targets"]) == (2, 1), x
        assert 45 <= x["fixed_cover_pct"] <= 55, x
        assert x["pushstate_load"] == 1, x
        miss = render_page(browser, "http://127.0.0.1:9/nothing", timeout=8)
        assert miss.get("error"), "못 여는 주소인데 오류가 없다"
    finally:
        srv.shutdown()
    print("selfcheck ok — 실제 브라우저로 렌더·측정까지 확인")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] != "--selfcheck":
        b = find_browser()
        if not b:
            sys.exit("렌더 엔진(크롬 계열 브라우저)을 못 찾았습니다.")
        out = render_page(b, sys.argv[1])
        out.pop("html", None)
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        _selfcheck()
