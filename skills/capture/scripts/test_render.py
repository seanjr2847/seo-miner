#!/usr/bin/env python3
"""화면이 실제로 뜨는지 — 헤드리스 브라우저로 띄워서 DOM 을 본다.

리포의 다른 검사 서른 개는 전부 파이썬 로직과 문법이다. 화면을 여는 것이 하나도
없었고, 그래서 최근 버그가 전부 화면·이음매 쪽에서 났다: 세로 메뉴가 가운데
정렬로 서 있던 것, 날짜를 "20 / 26-08-21" 로 반토막 내던 것, 사이트 링크가 hash 를
안 실어 무엇을 눌러도 첫 사이트가 열리던 것. 셋 다 `31/31 PASS` 아래에서 나갔다.

여기서 보는 것은 **눈으로 봐야 아는 것이 아니라, 열어만 봐도 아는 것**이다:
  · JS 가 터졌는가 (window.onerror / unhandledrejection / console.error / 조립 실패 배너)
  · 화면이 실제로 그려졌는가 (필수 요소가 DOM 에 있는가)
  · 사이트가 URL 이 시킨 대로 열렸는가 (hash 회귀)
정렬·대비·문구가 읽히는지는 여기서 안 나온다 — 그건 사람이 봐야 한다.

크롬(또는 엣지)이 없으면 조용히 건너뛴다. node --check 와 같은 규칙이다.
사용자의 진짜 Brain 은 안 건드린다 — 임시 CAPTURE_HOME 에 픽스처를 만든다.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(Path(__file__).resolve().parent))

# 사이트를 **둘** 만든다. 하나면 hash 회귀가 안 보인다 — 무엇을 눌러도 그 하나가
# 열리니 통과해 버린다. 그 버그가 오래 산 이유가 정확히 이것이다.
SITES = ("alpha-site", "beta-site")

BROWSERS = (
    "chrome", "chromium", "google-chrome", "google-chrome-stable", "msedge",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
)

# 페이지 맨 앞에 세우는 오류 수집기. 화면 스크립트보다 **먼저** 실행돼야 그 뒤에
# 터지는 것을 잡는다. --dump-dom 은 최종 DOM 만 주므로 콘솔을 DOM 으로 옮겨 적는다.
PROBE = """<script>
window.__ERR__ = [];
addEventListener("error", function (e) { window.__ERR__.push("onerror: " + (e.message || e)); });
addEventListener("unhandledrejection", function (e) {
  window.__ERR__.push("unhandled: " + ((e.reason && e.reason.message) || e.reason));
});
var _ce = console.error;
console.error = function () {
  window.__ERR__.push("console.error: " + [].slice.call(arguments).join(" "));
  _ce.apply(console, arguments);
};
addEventListener("DOMContentLoaded", function () {
  var d = document.createElement("div");
  d.id = "__probe__"; d.hidden = true;
  document.body.appendChild(d);
  setInterval(function () { d.textContent = JSON.stringify(window.__ERR__); }, 120);
});
</script>"""

def view_sections() -> list[tuple[str, str]]:
    """각 뷰가 자기 view-def 에 선언한 섹션 id — 그게 DOM 에 실제로 있어야 한다.

    목록을 여기 옮겨 적지 않는다. 선언이 정본이고(stage._check_seams 도 같은 것을
    읽는다), 화면이 늘면 이 검사가 저절로 따라간다.
    """
    out = []
    for p in sorted((ROOT / "skills" / "capture" / "templates" / "views").glob("*.html")):
        m = re.search(r'class="view-def">\s*(\{.*?\})\s*</script>', p.read_text("utf-8"), re.S)
        assert m, f"{p.name} 에 view-def 선언이 없다"
        d = json.loads(m.group(1))
        for sec in d["sections"]:
            out.append((rf'id="{re.escape(sec)}"',
                        f'화면 [{d["id"]}] 의 섹션이 DOM 에 없다: {sec}'))
    assert out, "뷰 선언을 하나도 못 읽었다"
    return out


# 두 화면이 함께 지켜야 하는 것. 정규식은 "그려졌는가"만 본다 — 예쁜지는 안 본다.
MUSTS = [
    (r"<option[^>]*selected[^>]*>" + SITES[1],
     "hash 가 지목한 사이트가 안 열렸다 — 링크가 실어 보낸 이름이 버려진다"),
    (r'id="content"(?![^>]*hidden)', "본문(#content)이 숨은 채로 남았다 — 데이터를 못 그렸다"),
    (r'id="meta"[^>]*>[^<]*\d{4}-\d{2}-\d{2}', "머리말이 기준 수집일을 안 적었다"),
] + view_sections()
# 호스팅 애드온이 런타임에 만드는 것 — 하나라도 없으면 조립이 조용히 멈춘 것이다.
# "!" 로 시작하면 반대다: 그 패턴이 **없어야** 통과한다.
HOSTED_MUSTS = MUSTS + [
    (r'!id="sm-fail"', "조립 실패 배너가 떴다 — 애드온 초기화가 터졌다(fail() 이 만든다)"),
    (r'id="sm-nav"', "사이드바 메뉴가 없다"),
    (r'id="smv-overview"', "화면 상자(개요)가 안 만들어졌다"),
    (r'id="smv-settings"', "화면 상자(설정)가 안 만들어졌다"),
    (r'class="sm-util"', "안내·새로고침이 바닥 통으로 안 옮겨졌다"),
]


def find_browser() -> str | None:
    for c in BROWSERS:
        p = (c if Path(c).exists() else None) if os.path.sep in c else shutil.which(c)
        if p:
            return p
    return None


def fixture(home: Path) -> None:
    """임시 Brain — 사이트 둘, 각각 GSC 스냅샷 두 날짜(비교 짝이 서야 KPI 가 산다)."""
    os.environ["CAPTURE_HOME"] = str(home)
    import db
    conn = db.connect()
    try:
        for i, name in enumerate(SITES):
            conn.execute(
                "INSERT OR IGNORE INTO projects(name,domain,gsc_property,type)"
                " VALUES(?,?,?,'saas')",
                (name, f"{name}.example", f"sc-domain:{name}.example"))
            pid = conn.execute("SELECT id FROM projects WHERE name=?", (name,)).fetchone()[0]
            for d, pos, clk in (("2026-05-01", 14.0, 3 + i), ("2026-06-01", 9.0, 7 + i)):
                conn.execute(
                    """INSERT INTO gsc_snapshots(project_id,snapshot_date,period_days,
                         query,page,clicks,impressions,ctr,position)
                       VALUES(?,?,28,?,?,?,120,0.1,?)""",
                    (pid, d, f"{name} 검색어", f"https://{name}.example/a", clk, pos))
        conn.commit()
    finally:
        conn.close()


def serve(page: bytes) -> ThreadingHTTPServer:
    """검사용 서버 — 라이브 대시보드와 같은 Handler 에 페이지만 갈아 끼운다."""
    import dashboard

    class H(dashboard.Handler):
        def do_GET(self):                       # noqa: N802
            if urlparse(self.path).path in ("/", "/d"):
                return self._send(200, page, "text/html; charset=utf-8")
            return super().do_GET()

        def log_message(self, *a):              # 요청 로그가 검사 출력을 덮는다
            pass

    ThreadingHTTPServer.allow_reuse_address = True
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def dom(browser: str, url: str, profile: Path) -> str:
    r = subprocess.run(
        [browser, "--headless=new", "--disable-gpu", "--no-sandbox",
         f"--user-data-dir={profile}", "--virtual-time-budget=9000", "--dump-dom", url],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    if not r.stdout:
        raise AssertionError(f"브라우저가 페이지를 못 열었다: {url}\n{(r.stderr or '')[-800:]}")
    return r.stdout


def js_errors(html: str) -> list[str]:
    m = re.search(r'id="__probe__"[^>]*>(.*?)</div>', html, re.S)
    if not m or not m.group(1).strip():
        # 수집기 자체가 안 붙었으면 검사가 헛돈 것이다 — 통과로 오해하면 안 된다.
        raise AssertionError("오류 수집기(#__probe__)가 DOM 에 없다 — 페이지가 안 떴다")
    try:
        return json.loads(m.group(1))
    except ValueError:
        return [m.group(1)[:300]]


def check(label: str, html: str, musts) -> None:
    errs = js_errors(html)
    assert not errs, f"{label} 에서 JS 가 터졌다:\n  " + "\n  ".join(errs[:6])
    for pat, why in musts:
        if pat.startswith("!"):
            assert not re.search(pat[1:], html, re.S), f"{label}: {why}"
        else:
            assert re.search(pat, html, re.S), f"{label}: {why}"


def run() -> None:
    browser = find_browser()
    if not browser:
        print("크롬/엣지가 없어 건너뜁니다")
        return

    home = Path(tempfile.mkdtemp(prefix="seo-miner-render-"))
    try:
        fixture(home)
        import dashboard
        # 오류 수집기를 화면 스크립트보다 앞에 세운다
        shell = dashboard.HTML.replace(b"</head>", PROBE.encode("utf-8") + b"</head>", 1)
        assert b"__probe__" in shell, "오류 수집기를 끼울 </head> 를 못 찾았다"

        targets = [("로컬 대시보드", shell, MUSTS)]
        # 호스팅 조립본은 리포에서만 만들 수 있다 (플러그인 설치본에 server/ 가 없다)
        if (ROOT / "server" / "assets" / "dash.html").exists():
            sys.path.insert(0, str(ROOT))
            from server import pages
            targets.append(("호스팅 조립본", shell + pages.addon("dash.html"), HOSTED_MUSTS))

        for label, page, musts in targets:
            srv = serve(page)
            try:
                # 두 번째 사이트를 hash 로 지목한다 — 첫 사이트가 열리면 그게 버그다
                url = f"http://127.0.0.1:{srv.server_address[1]}/d#{SITES[1]}"
                check(label, dom(browser, url, home / "chrome-profile"), musts)
                print(f"  {label}: ok")
            finally:
                srv.shutdown()
    finally:
        shutil.rmtree(home, ignore_errors=True)


if __name__ == "__main__":
    run()
    print("render self-check ok")
