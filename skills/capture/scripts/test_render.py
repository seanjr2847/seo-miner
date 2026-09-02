#!/usr/bin/env python3
"""화면이 실제로 뜨는지 — 헤드리스 브라우저로 띄워서 DOM 을 본다.

리포의 다른 검사 서른 개는 전부 파이썬 로직과 문법이다. 화면을 여는 것이 하나도
없었고, 그래서 최근 버그가 전부 화면·이음매 쪽에서 났다: 세로 메뉴가 가운데
정렬로 서 있던 것, 날짜를 "20 / 26-08-21" 로 반토막 내던 것, 사이트 링크가 hash 를
안 실어 무엇을 눌러도 첫 사이트가 열리던 것. 셋 다 `31/31 PASS` 아래에서 나갔다.

여기서 보는 것은 **눈으로 봐야 아는 것이 아니라, 열어만 봐도 아는 것**이다:
  · JS 가 터졌는가 (window.onerror / unhandledrejection / console.error / 조립 실패 배너)
  · 화면이 실제로 그려졌는가 (필수 요소가 DOM 에 있는가)
  · 데이터를 준 화면이 그 값을 **그렸는가** (백링크·경쟁 분석·크롤 회차 비교)
  · 사이트가 URL 이 시킨 대로 열렸는가 (hash 회귀)
정렬·대비·문구가 읽히는지는 여기서 안 나온다 — 그건 사람이 봐야 한다.

"있어야 한다"는 검사는 <script> 를 뺀 DOM 에서만 찾는다. 소스까지 뒤지면 렌더러가
만들 수 있는 문자열은 **그 코드가 한 번도 안 돌아도** 통과한다 — 실제로 그랬다.
그래서 픽스처 값은 리포 어디에도 없는 표식을 쓴다(화면 문구와 겹치면 같은 일이 난다).

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


# 새 축(백링크·경쟁·크롤)에 넣는 값. 화면이 이 글자를 실제로 그리는지로 검사한다 —
# 섹션 상자가 DOM 에 있는 것과 그 안이 채워진 것은 다른 이야기다.
# 값은 리포 어디에도 없는 표식이어야 한다 — 화면 문구와 겹치면 렌더러를 꺼도
# 통과한다(실제로 "빠진 검색어"가 rank.html 본문에 이미 있어서 그랬다).
BL_DOMAIN = "ixZ9.example"          # 링크 교집합: 경쟁사는 받는데 우리는 못 받는 곳
RIVAL = "rivalZ9.example"           # 경쟁 분석: 트래픽 몫이 가장 큰 곳
GAP_KW = "격차검색어Z9"             # Content Gap: 우리가 아예 없는 것
CRAWL_NEW = "/crawlZ9"              # 크롤: 직전 회차에 없던 이슈
GAP_RIVAL = "gapZ9.example"         # 격차 표에만 나오는 도메인 — RIVAL 과 갈라 둔다
#   (같은 값을 두 자리에 쓰면 한쪽을 꺼도 다른 쪽 때문에 검사가 통과한다)
BL_REFDOMS = 20241                  # 요약 계기판에만 나오는 수 — 문구와 안 겹친다

# 두 화면이 함께 지켜야 하는 것. 정규식은 "그려졌는가"만 본다 — 예쁜지는 안 본다.
MUSTS = [
    (r"<option[^>]*selected[^>]*>" + SITES[1],
     "hash 가 지목한 사이트가 안 열렸다 — 링크가 실어 보낸 이름이 버려진다"),
    (r'id="content"(?![^>]*hidden)', "본문(#content)이 숨은 채로 남았다 — 데이터를 못 그렸다"),
    (r'id="meta"[^>]*>[^<]*\d{4}-\d{2}-\d{2}', "머리말이 기준 수집일을 안 적었다"),
    # 새 축은 상자가 서는 것과 그 안이 채워지는 것이 다르다 — 값이 실제로 그려졌나를 본다.
    # (숨은 화면도 DOM 에는 있으므로 --dump-dom 으로 보인다.)
    (re.escape(BL_DOMAIN), "백링크 화면이 링크 교집합을 안 그렸다"),
    (r"20,241", "백링크 화면이 요약 계기판을 안 그렸다"),
    (re.escape(RIVAL), "경쟁 분석 화면이 경쟁사를 안 그렸다"),
    (re.escape(GAP_KW), "경쟁 분석 화면이 키워드 격차를 안 그렸다"),
    (re.escape(CRAWL_NEW), "사이트 점검이 크롤 이슈를 안 그렸다"),
    (r"새로 생김", "크롤 회차 비교가 신규 이슈를 표시 안 했다 — 이 축의 전부가 그것이다"),
    (r"해결된 것 <b>0</b>건|새로 생긴 것 <b>1</b>건", "크롤 회차 비교 요약이 안 나왔다"),
] + view_sections()
# 박제본(--export)은 배포되는 산출물이다 — 메일로 나가고 저장돼서 열린다. 라이브
# 화면과 조건이 다르다: 서버가 없고, 손댈 수 없고, 인쇄된다. 그래서 따로 본다.
# 화면 목록은 여기 옮겨 적지 않는다 — view-def 에서 읽되 박제본이 빼는 둘만 뺀다.
REPORT_DROPPED = ("settings", "guide")


def view_defs_ids() -> list[str]:
    """뷰 선언에 있는 화면 id 전부 — 목록을 여기 옮겨 적지 않는다."""
    out = []
    for f in sorted((ROOT / "skills" / "capture" / "templates" / "views").glob("*.html")):
        m = re.search(r'class="view-def">\s*(\{.*?\})\s*</script>', f.read_text("utf-8"), re.S)
        assert m, f"{f.name} 에 view-def 선언이 없다"
        out.append(json.loads(m.group(1))["id"])
    return out
REPORT_MUSTS = [
    (r'id="content"(?![^>]*hidden)', "본문(#content)이 숨은 채로 남았다"),
    (r'id="meta"[^>]*>[^<]*\d{4}-\d{2}-\d{2}', "머리말이 기준 수집일을 안 적었다"),
    # 종이 표지 — 인쇄하면 레일이 빠지므로 여기 말고는 "누구의 무엇을 언제"가 없다.
    (r'id="printhead"[^>]*>\s*<div class="t">[^<]+—', "종이 표지가 안 채워졌다"),
    # 손댈 수 없는 기록이라고 말만 하고 손잡이를 남겨 두면 안 된다.
    (r"!검색어·주소로 찾기", "박제본에 기회 찾기 칸이 남아 있다 — 서버가 없어 아무 일도 안 한다"),
    (r"!상태로 거르기", "박제본에 상태 필터가 남아 있다"),
    # 기회 칩만 콕 집는다 — 다른 화면(경쟁 분석·크롤)의 칩은 읽는 도구라 남아도 된다.
    (r"!filterKind\(", "박제본에 기회 갈래 필터가 남아 있다"),
    (r"여기서는 바꿀 수 없습니다", "박제본이 트리아지가 되는 것처럼 말한다"),
    (r"!id=\"view-settings\"", "박제본에 [설정] 화면이 남았다 — 남한테 보내는 파일이다"),
    (r"!id=\"view-guide\"", "박제본에 [안내] 화면이 남았다"),
    # 값이 실제로 그려졌는가 — 라이브와 같은 표식을 쓴다
    (re.escape(BL_DOMAIN), "박제본이 링크 교집합을 안 그렸다"),
    (re.escape(RIVAL), "박제본이 경쟁사를 안 그렸다"),
    (re.escape(CRAWL_NEW), "박제본이 크롤 이슈를 안 그렸다"),
]

# 호스팅 애드온이 런타임에 만드는 것 — 하나라도 없으면 조립이 조용히 멈춘 것이다.
# "!" 로 시작하면 반대다: 그 패턴이 **없어야** 통과한다.
HOSTED_MUSTS = MUSTS + [
    # 배포마다 다른 문구는 화면이 data-web / W() 로 직접 고른다(dashboard.html).
    # 하나라도 남아 있으면 표식(SM_HOSTED)이 첫 렌더보다 늦게 선 것이다 — 응답이
    # 빠를수록 잘 지는 경합이라, 눈으로 보면 멀쩡한데 실서비스에서만 틀린다.
    (r"!data-web=", "호스팅인데 대체 문구가 안 입혀졌다 — SM_HOSTED 가 첫 렌더보다 늦게 섰다"),
    (r'!id="sm-fail"', "조립 실패 배너가 떴다 — 애드온 초기화가 터졌다(fail() 이 만든다)"),
    (r'id="nav"[^>]*>\s*<button', "레일 메뉴가 비었다 — 셸이 화면 목록을 못 세웠다"),
    (r'id="view-overview"', "화면 상자(개요)가 안 만들어졌다"),
    (r'id="view-backlinks"', "호스팅 전용 화면(백링크)이 안 붙었다"),
    (r'id="sm-set"', "호스팅 설정 섹션이 안 만들어졌다"),
    (r'id="sm-run"', "레일 바닥의 [전체 분석 실행]이 안 붙었다"),
    # 안내의 실행 칩은 SM.host 를 렌더 시점에 부른다(dashboard.html 의 renderGuide) —
    # 호스팅판은 그 훅을 실행 버튼으로 갈아 낀다(dash.html 의 SM.host.stepChip).
    # 이 픽스처(beta-site)는 GSC 는 읽었지만 키워드를 아직 안 캤으므로 "지금 할 것"이
    # runnable 인 키워드 단계다 — 그 칩(class="cmd", 화면 제목 옆 칩과는 클래스가
    # 다르다)이 실제로 host.run 을 부르는지를 본다. 로컬 기본값(SM.host.copy)이
    # 남아 있으면(=SM.host 가 늦게 섰거나 안 갈렸으면) 이 패턴이 없다.
    (r'class="cmd" data-stage="[^"]+" onclick="SM\.host\.run\(',
     "안내의 실행 칩이 host.run 을 안 부른다 — 복사 칩인 채로 남았다"),
]


def find_browser() -> str | None:
    for c in BROWSERS:
        p = (c if Path(c).exists() else None) if os.path.sep in c else shutil.which(c)
        if p:
            return p
    return None



def _axes(conn, pid: int) -> None:
    """백링크·경쟁·크롤 — 화면이 이걸 그리는지 보려고 최소한만 심는다."""
    d, prev = "2026-06-01", "2026-05-01"
    # 기회가 0건이면 [다음에 손댈 것]이 빈 상태에서 끝나 도구줄(찾기·거르기)이 아예
    # 안 그려진다 — "박제본에 찾기 칸이 없다"는 검사가 그 때문에 헛통과했다.
    # 갈래가 둘 이상이어야 칩 필터가 그려진다 — 하나면 그 검사도 헛통과한다.
    conn.executemany(
        "INSERT INTO opportunities(project_id,kind,target,score,reasoning,status)"
        " VALUES(?,?,?,?,?,'new')",
        [(pid, "striking_distance", f"{SITES[1]} 검색어", 71.2, "평균 12.4위 — 1페이지까지 2.4칸"),
         (pid, "ctr_gap", f"{SITES[1]} 두 번째", 58.0, "노출은 나오는데 클릭이 0")])
    conn.execute("INSERT INTO backlink_summary(project_id,checked_date,rank,backlinks,"
                 "referring_domains,broken_backlinks,dofollow,nofollow)"
                 " VALUES(?,?,412,1840,?,17,1512,328)", (pid, d, BL_REFDOMS))
    conn.execute("INSERT INTO referring_domains(project_id,checked_date,domain,rank,backlinks)"
                 " VALUES(?,?,'ref.example',700,9)", (pid, d))
    conn.execute("INSERT INTO backlinks(project_id,checked_date,url_from,url_to,anchor,"
                 "rank,dofollow,is_broken)"
                 " VALUES(?,?,'https://ref.example/1','https://x/y','앵커',700,1,0)", (pid, d))
    conn.execute("INSERT INTO backlink_anchors(project_id,checked_date,anchor,backlinks,"
                 "referring_domains) VALUES(?,?,'브랜드',12,4)", (pid, d))
    conn.execute("INSERT INTO link_intersect(project_id,checked_date,domain,rank,hits,"
                 "targets,we_have) VALUES(?,?,?,810,3,'r1,r2,r3',0)", (pid, d, BL_DOMAIN))
    conn.executemany(
        "INSERT INTO competitor_metrics(project_id,checked_date,domain,is_self,keywords,etv,"
        "top10) VALUES(?,?,?,?,?,?,?)",
        [(pid, d, RIVAL, 0, 4120, 8800.0, 610), (pid, d, "me.example", 1, 380, 900.0, 41)])
    conn.execute("INSERT INTO keyword_gap(project_id,checked_date,keyword,domain,position,"
                 "our_position,volume,kind) VALUES(?,?,?,?,2,NULL,2400,'missing')",
                 (pid, d, GAP_KW, GAP_RIVAL))
    r1 = conn.execute("INSERT INTO crawl_runs(project_id,finished_at,seed,pages,issues)"
                      " VALUES(?,?,'sitemap',10,1)", (pid, prev)).lastrowid
    r2 = conn.execute("INSERT INTO crawl_runs(project_id,finished_at,seed,pages,issues)"
                      " VALUES(?,?,'sitemap',12,2)", (pid, d)).lastrowid
    conn.executemany("INSERT INTO crawl_issues(run_id,kind,severity,url,detail)"
                     " VALUES(?,?,?,?,?)",
                     [(r1, "dup_title", "warn", "/a", "같은 제목"),
                      (r2, "dup_title", "warn", "/a", "같은 제목"),
                      (r2, "broken_internal", "bad", CRAWL_NEW, "404")])


def fixture(home: Path) -> None:
    """임시 Brain — 사이트 둘, 각각 GSC 스냅샷 두 날짜(비교 짝이 서야 KPI 가 산다).

    두 번째 사이트(테스트가 여는 쪽)에는 백링크·경쟁·크롤 축도 심는다 — 그 화면들이
    데이터를 받았을 때 실제로 그리는지가 이 파일이 볼 수 있는 유일한 자리다.
    """
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
            if name == SITES[1]:            # 테스트가 여는 사이트
                _axes(conn, pid)
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
    # --dump-dom 은 <script>·<style> 안의 소스까지 준다. 양쪽 다 그걸 빼고 본다.
    #
    # "없어야 한다"는 당연하고("화면에 안 보이는 코드 조각"), **"있어야 한다"도 그렇다**:
    # 소스까지 뒤지면 렌더러가 만들 수 있는 문자열은 그 코드가 한 번도 안 돌아도
    # 통과한다. 실제로 그랬다 — 화면이 그리는 걸 통째로 꺼도 검사가 초록이었다.
    # 그림에 있는 것만 근거로 삼는다.
    seen = re.sub(r"<script[\s\S]*?</script>|<style[\s\S]*?</style>", "", html)
    for pat, why in musts:
        if pat.startswith("!"):
            assert not re.search(pat[1:], seen, re.S), f"{label}: {why}"
        else:
            assert re.search(pat, seen, re.S), f"{label}: {why}"


def print_all(browser: str, url: str, home: Path, views: int) -> None:
    """인쇄하면 **모든 화면**이 나오는가.

    화면은 한 번에 하나만 세운다 — 눌러서 옮겨 다니는 것이니까. 종이에는 누를 데가
    없어서, 인쇄 규칙이 숨긴 화면을 안 펴면 보고서를 인쇄했는데 열 화면 중 한 장만
    나온다(실제로 그랬다: 2쪽). 쪽수로 못 박는다 — 화면 수보다는 많이 나와야 한다.
    """
    pdf = home / "report.pdf"
    subprocess.run(
        [browser, "--headless=new", "--disable-gpu", "--no-sandbox",
         f"--user-data-dir={home / 'chrome-profile'}", "--virtual-time-budget=9000",
         f"--print-to-pdf={pdf}", "--no-pdf-header-footer", url],
        capture_output=True, timeout=180)
    assert pdf.exists(), "인쇄본을 못 만들었다"
    pages_n = len(re.findall(rb"/Type\s*/Page[^s]", pdf.read_bytes()))
    assert pages_n >= views, (
        f"인쇄하면 {pages_n}쪽뿐이다 — 화면이 {views}개인데 숨은 것이 안 펴졌다")


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
            # /d 와 똑같이 만든다: assemble("hosted") 가 호스팅 전용 섹션(성과·키워드
            # 관리·AI 질문·고급 분석·설정)을 원본 뷰 순서 안에 끼우고 SM_HOSTED 를 첫
            # <script> 로 세운 뒤, 애드온을 뒤에 붙인다. local 조립(dashboard.HTML)으로
            # 대신 만들면 그 섹션들이 없어서 검사가 실제로 나가는 화면을 안 보게 된다.
            hosted = dashboard.assemble("hosted").encode("utf-8").replace(
                b"</head>", PROBE.encode("utf-8") + b"</head>", 1)
            assert b"__probe__" in hosted, "오류 수집기를 끼울 </head> 를 못 찾았다"
            hosted += pages.addon("dash.html")
            targets.append(("호스팅 조립본", hosted, HOSTED_MUSTS))

        # 박제본 — 같은 템플릿에 데이터를 박아 넣은 자립형 HTML.
        report = dashboard.export(SITES[1]).read_bytes().replace(
            b"</head>", PROBE.encode("utf-8") + b"</head>", 1)
        targets.append(("박제본", report,
                        REPORT_MUSTS + [m for m in view_sections()
                                        if not any(f'[{v}]' in m[1] for v in REPORT_DROPPED)]))

        for label, page, musts in targets:
            srv = serve(page)
            try:
                # 두 번째 사이트를 hash 로 지목한다 — 첫 사이트가 열리면 그게 버그다
                url = f"http://127.0.0.1:{srv.server_address[1]}/d#{SITES[1]}"
                check(label, dom(browser, url, home / "chrome-profile"), musts)
                if label == "박제본":
                    # 화면 상자는 런타임에 생긴다 — 소스에서 세면 0 이라 단언이 늘 참이다.
                    # 선언(view-def)에서 세고 박제본이 빼는 둘을 뺀다.
                    print_all(browser, url, home,
                              len(view_defs_ids()) - len(REPORT_DROPPED))
                print(f"  {label}: ok")
            finally:
                srv.shutdown()
    finally:
        shutil.rmtree(home, ignore_errors=True)


if __name__ == "__main__":
    run()
    print("render self-check ok")
