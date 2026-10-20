#!/usr/bin/env python3
"""[클릭 변화](analysis) — 부제·처리 큐 제목이 화면 몫과 맞는지, 그리고 pseo_pattern 기회의 근거 소카드.

고친 두 가지:

  1. 처리 큐가 맡는 종류는 클릭률·의도·주제·패턴 넷(scoring.KINDS 의 see 가 정본)인데 화면 부제와 큐
     제목은 「클릭이 왜 늘고 줄었는지」 하나뿐이었다. 큐 제목은 view-def queue 의 `title`(셸이 읽는다)로,
     부제는 [할 것]·[전체] 둘을 아울러 적는다. 둘 다 **종류 이름(kind_label·kind id)을 적지 않는다** —
     적으면 scoring.KINDS 의 사본이 된다. 업무 이름으로만 부른다.
  2. 기회 pseo_pattern 의 `see` 는 (analysis, an-cluster) 인데 그 섹션은 패턴 무리를 안 그렸다. 페이로드에
     이미 실려 있는 `pseo_groups`(scoring.pseo_groups 가 묶은 그대로 — dashboard._axis_query_pages) 를
     [반복 패턴] 소카드(#an-pseo)로 an-cluster 섹션 안에 그린다. 무리가 없으면 소카드는 접힌다.

실제로 열어서: 무리가 있으면 소카드가 서고(틀·바뀌는 칸·묶인 검색어 수·노출 합·걸린 페이지), 없으면
접히고, [개요]의 「자세히 보기」(SM.queue.goSee) 가 [전체] 탭의 이 소카드 앞으로 온다.
로컬(stdlib)과 호스팅(serve_hosted, 실제 FastAPI 앱) 둘 다.

CLAUDE.md 의 규율: 검사를 추가했으면 일부러 깨서 FAIL 이 나는 것까지 확인한다.
self-check: python test_modes_analysis_fix.py
"""
from __future__ import annotations

import html as htmlmod
import json
import os
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
VIEW = ROOT / "skills" / "capture" / "templates" / "views" / "analysis.html"
DOC = ROOT / "docs" / "screens" / "analysis.md"


def _src() -> str:
    return VIEW.read_text("utf-8")


def _def(src: str) -> dict:
    m = re.search(r'class="view-def">\s*(\{.*?\})\s*</script>', src, re.S)
    assert m, "view-def 가 없다"
    return json.loads(m.group(1))


def _markup(src: str) -> str:
    src = re.sub(r"<!--.*?-->", "", src, flags=re.S)
    return re.sub(r"<script\b.*?</script>|<style\b.*?</style>", "", src, flags=re.S)


# ── 1. 부제·큐 제목 ─────────────────────────────────────────────────────────

def test_queue_title_and_sub_do_not_copy_kind_names():
    import scoring
    d = _def(_src())
    title = (d.get("queue") or {}).get("title")
    assert title, "analysis: queue.title 이 없다 — 큐가 셸 기본 제목(개선 기회)으로 선다"
    mine = scoring.queue_kinds_by_view().get("analysis") or []
    assert mine, "analysis 가 맡은 종류가 없다 — 이 검사가 볼 것이 없다"
    # 종류 이름 사본 금지: 종류 id·한국어 라벨 전체를 제목·부제에 그대로 적지 않는다.
    names = set(mine) | {scoring.kind_label(k) for k in mine}
    for what, text in (("queue.title", title), ("sub", d.get("sub") or "")):
        for n in names:
            assert n not in text, f"analysis {what} 가 종류 이름 '{n}' 을 그대로 적었다(scoring.KINDS 사본): {text}"
    # 큐 제목은 [할 것] 탭 이름과 겹쳐 읽히면 안 되고(탭이 이미 '처리'다), 기본 제목이어도 안 된다.
    assert title != "개선 기회" and title != "처리", f"analysis queue.title 이 일반적이다: {title}"


def test_sub_speaks_both_tabs():
    d = _def(_src())
    sub = d.get("sub") or ""
    assert "원인" in sub, f"sub 가 [전체](원인별로 가르기)를 안 말한다: {sub}"
    assert "처리" in sub, f"sub 가 [할 것](큐)를 안 말한다: {sub}"
    assert sub != "클릭이 왜 늘고 줄었는지 원인별로 가릅니다.", "sub 가 옛 문구 그대로다"


# ── 2. 소카드 마크업 ────────────────────────────────────────────────────────

def test_pseo_target_section_is_an_cluster():
    """기회 pseo_pattern 의 see 가 가리키는 자리가 이 소카드를 품은 섹션이어야 한다(양쪽 끝)."""
    import scoring
    see = next(k.see for k in scoring.KINDS if k.name == "pseo_pattern")
    assert see == ("analysis", "an-cluster"), f"pseo_pattern 의 see 가 바뀌었다: {see} — 소카드 자리를 다시 정한다"
    m = re.search(r'<section id="an-cluster"[^>]*>(.*?)</section>', _markup(_src()), re.S)
    assert m, "an-cluster 섹션이 없다"
    assert 'id="an-pseo"' in m.group(1), "an-cluster 섹션 안에 #an-pseo 소카드가 없다"


def test_pseo_card_markup_and_rules():
    src = _src()
    mk = _markup(src)
    m = re.search(r'<section id="an-cluster"([^>]*)>(.*?)</section>', mk, re.S)
    assert m
    attrs, inner = m.group(1), m.group(2)
    assert 'data-mode="manage"' in attrs, "an-cluster 가 [전체]에 안 있다"
    card = re.search(r'<div id="an-pseo"([^>]*)>', inner)
    assert card, "#an-pseo 가 div 가 아니다"
    assert "data-an-off" in card.group(1), "#an-pseo 가 기본 접힘(data-an-off)이 아니다 — 무리가 없을 때 빈 카드가 샌다"
    assert "data-mode" not in card.group(1), "#an-pseo 가 자기 탭을 선언한다 — 섹션(관리)을 따라야 한다"
    assert " hidden" not in card.group(1), "#an-pseo 가 hidden 을 쓴다 — 접기는 data-an-off"
    assert re.search(r'id="an-pseo-body"', inner), "#an-pseo-body 가 없다"
    css = "".join(re.findall(r"<style>(.*?)</style>", src, re.S))
    assert re.search(r"#an-pseo\[data-an-off\]\s*\{\s*display\s*:\s*none", css), "접힘 규칙(#an-pseo[data-an-off])이 없다"
    assert "!important" not in css, "!important 금지"
    # 한글에 자간·대문자화 금지 — 이 소카드 규칙에는 아예 안 쓴다
    for rule in re.findall(r"[^{}]*#an-pseo[^{}]*\{[^}]*\}", css):
        assert "letter-spacing" not in rule and "text-transform" not in rule, f"소카드 규칙에 자간/대문자화: {rule}"
        assert "--mono" not in rule, f"소카드 규칙이 한글에 등폭을 건다: {rule}"
    assert not re.search(r'\$\("an-pseo[\w-]*"\)\.hidden\s*=', src), "뷰가 소카드의 hidden 을 쓴다"
    # 정본을 가리킨다 — 틀/칸/묶인 검색어는 서버가 묶은 pseo_groups 를 읽기만 한다
    fn = re.search(r"function anPseo\(.*?\n\}\n", src, re.S)
    assert fn, "anPseo() 가 없다"
    for f in ("frame", "values", "queries", "imp", "clk", "ctr_pct", "page", "own"):
        assert f"{f}" in fn.group(0), f"anPseo 가 무리의 '{f}' 를 안 읽는다"
    assert re.search(r"anPseoApply\(d\.pseo_groups\)", src), "VIEW 가 d.pseo_groups 로 소카드를 채우지 않는다"


def test_payload_group_has_the_fields_the_card_reads():
    """뷰가 읽는 필드가 실제 scoring.pseo_groups 가 내는 꼴에 있다(뷰가 지어낸 필드를 읽지 않는다)."""
    groups = _real_groups()
    assert groups, "씨앗 데이터에서 무리가 안 섰다"
    g = groups[0]
    for k in ("frame", "values", "queries", "imp", "clk", "ctr_pct", "pos"):
        assert k in g, f"무리에 '{k}' 가 없다"
    for k in ("value", "imp", "page", "own"):
        assert k in g["values"][0], f"무리의 값에 '{k}' 가 없다"
    # 페이로드가 이 무리를 최상위 pseo_groups 로 싣는다(양쪽 끝: 서버가 싣는 키 = 뷰가 읽는 키)
    dsrc = (SCRIPTS / "dashboard.py").read_text("utf-8")
    assert '"pseo_groups": pseo_groups' in dsrc, "dashboard.py 가 pseo_groups 를 페이로드에 안 싣는다"


def test_docs_say_it():
    t = DOC.read_text("utf-8")
    assert "반복 패턴" in t, "docs/screens/analysis.md 에 [반복 패턴] 소카드 설명이 없다"
    assert "pseo_groups" in t or "데이터" in t


# ── 실제 데이터로 무리를 하나 뽑는다 ────────────────────────────────────────

_GROUPS: list | None = None


def _real_groups() -> list:
    """임시 Brain 에 '{도시} 세무사 추천' 다섯 검색어를 심고 scoring.pseo_groups 가 묶은 그대로 받는다."""
    global _GROUPS
    if _GROUPS is not None:
        return _GROUPS
    import db
    import scoring
    old = os.environ.get("CAPTURE_HOME")
    home = Path(tempfile.mkdtemp(prefix="seo-miner-pseo-"))
    try:
        os.environ["CAPTURE_HOME"] = str(home)
        conn = db.connect()
        try:
            conn.execute("INSERT INTO projects(name,domain,gsc_property,type)"
                         " VALUES('tax','tax.example','sc-domain:tax.example','saas')")
            pid = conn.execute("SELECT id FROM projects").fetchone()[0]
            for i, c in enumerate(["서울", "부산", "대구", "인천", "광주"]):
                conn.execute(
                    "INSERT INTO gsc_snapshots(project_id,snapshot_date,period_days,query,page,clicks,"
                    "impressions,ctr,position) VALUES(?,?,28,?,?,1,?,0.005,?)",
                    (pid, "2026-06-01", f"{c} 세무사 추천",
                     "https://tax.example/guide" if i < 3 else f"https://tax.example/{i}", 200 - i * 10, 9.0 + i))
            conn.commit()
            _GROUPS = scoring.pseo_groups(conn, pid, "2026-06-01")
        finally:
            conn.close()
    finally:
        if old is None:
            os.environ.pop("CAPTURE_HOME", None)
        else:
            os.environ["CAPTURE_HOME"] = old
        import gc
        gc.collect()
        shutil.rmtree(home, ignore_errors=True)
    return _GROUPS


# ── 실제로 열어서 ───────────────────────────────────────────────────────────

def _run_script(groups: list) -> str:
    return """<script>
setTimeout(function () {
  var out = {}, G = %s;
  function card() {
    var c = document.getElementById("an-pseo");
    if (!c) return null;
    var r = c.getBoundingClientRect();
    return {display: getComputedStyle(c).display, h: c.offsetHeight, off: c.hasAttribute("data-an-off"),
      top: Math.round(r.top), vh: innerHeight, text: c.innerText.replace(/\\s+/g, " "),
      groups: c.querySelectorAll(".anpg").length,
      chips: c.querySelectorAll(".anpg .anpv").length,
      links: [].map.call(c.querySelectorAll(".anpg a"), function (a) { return a.getAttribute("href"); })};
  }
  try {
    try { sessionStorage.removeItem("sm:mode:analysis"); } catch (e) {}
    SM.mode = {}; SM.show("analysis");
    SM.setMode("analysis", "manage");
    out.real = card();                              // 픽스처 페이로드에는 무리가 없다 — 접혀 있어야
    window.anPseoApply(G);
    out.filled = card();
    SM.setMode("analysis", "work");
    out.workTab = card();                           // [할 것]에서는 섹션째 접힌다
    SM.show("overview");
    SM.queue.goSee("analysis", "an-cluster");       // [개요] 줄의 「자세히 보기」가 하는 일 그대로
    out.afterSee = card();
    out.modeAfterSee = SM.modeNow("analysis");
    window.anPseoApply([]);
    out.emptied = card();
    window.anPseoApply(undefined);
    out.undef = card();
  } catch (e) { out.err = String(e && e.stack || e); }
  var d = document.createElement("div"); d.id = "__mk__";
  d.textContent = JSON.stringify(out); document.body.appendChild(d);
}, 3500);
</script>""" % json.dumps(groups, ensure_ascii=False)


class _Proxy(BaseHTTPRequestHandler):
    target = ""
    inject = b""

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
            data = data.replace(b"</body>", self.inject + b"</body>", 1)
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


def _verify(label: str, html: str, groups: list) -> None:
    import test_render as tr
    p = tr.probe(html)
    assert not p["err"], f"{label}: JS 가 터졌다:\n  " + "\n  ".join(p["err"][:6])
    m = re.search(r'id="__mk__"[^>]*>(.*?)</div>', html, re.S)
    assert m, f"{label}: 검사 스크립트가 결과를 못 남겼다"
    out = json.loads(htmlmod.unescape(m.group(1)))
    assert not out.get("err"), f"{label}: 검사 스크립트가 터졌다: {out.get('err')}"
    g0 = groups[0]
    r = out["real"]
    assert r and r["display"] == "none" and r["h"] == 0, f"{label}: 무리가 없는데 소카드가 보인다(빈 카드): {r}"
    f = out["filled"]
    assert f["display"] != "none" and f["h"] > 0 and not f["off"], f"{label}: 무리가 있는데 소카드가 안 선다: {f}"
    assert f["groups"] == len(groups), f"{label}: 무리 {len(groups)}개인데 {f['groups']}개 선다"
    t = f["text"]
    assert g0["frame"] in t, f"{label}: 틀 '{g0['frame']}' 이 안 보인다: {t}"
    assert g0["values"][0]["value"] in t and g0["values"][1]["value"] in t, f"{label}: 바뀌는 칸 예시가 안 보인다: {t}"
    assert f["chips"] >= 3, f"{label}: 바뀌는 칸 예시가 3개 미만이다: {f['chips']}"
    assert f"{len(g0['queries'])}" in t, f"{label}: 묶인 검색어 수({len(g0['queries'])})가 안 보인다: {t}"
    assert f"{g0['imp']:,}" in t, f"{label}: 노출 합({g0['imp']:,})이 안 보인다: {t}"
    assert any("tax.example/guide" in h for h in f["links"]), f"{label}: 걸린 페이지 링크가 없다: {f['links']}"
    assert out["workTab"]["h"] == 0, f"{label}: [할 것] 탭인데 소카드가 보인다(섹션째 접혀야 한다): {out['workTab']}"
    assert out["modeAfterSee"] == "manage", f"{label}: 「자세히 보기」가 [전체] 탭을 안 연다: {out['modeAfterSee']}"
    a = out["afterSee"]
    assert a["display"] != "none" and a["h"] > 0, f"{label}: 「자세히 보기」 뒤 소카드가 안 보인다: {a}"
    assert -2 <= a["top"] < a["vh"], f"{label}: 「자세히 보기」 뒤 소카드가 화면 밖이다(top {a['top']}, 창 {a['vh']})"
    for k in ("emptied", "undef"):
        e = out[k]
        assert e["display"] == "none" and e["h"] == 0, f"{label}: 무리를 비웠는데 소카드가 남았다({k}): {e}"


def test_render_local_and_hosted():
    import test_render as tr
    browser = tr.find_browser()
    if not browser:
        print("크롬/엣지가 없어 건너뜁니다")
        return
    groups = _real_groups()
    inject = _run_script(groups).encode("utf-8")
    home = Path(tempfile.mkdtemp(prefix="seo-miner-anfix-"))
    try:
        import doctor
        for k in (doctor.MODE_ENV, doctor.TOOL_ENV, doctor.TERMINAL_ENV):
            os.environ.pop(k, None)
        tr.fixture(home)
        import dashboard
        shell = dashboard.HTML.replace(b"</head>", tr.PROBE.encode("utf-8") + b"</head>", 1)
        page = shell.replace(b"</body>", inject + b"</body>", 1)
        with tr._stdlib(page) as site:
            html = tr.dom(browser, f"{site.base}/d#{tr.SITES[1]}", home / "chrome-profile")
        _verify("로컬", html, groups)

        if (ROOT / "server" / "assets" / "dash.html").exists():
            with tr.serve_hosted(home / "hosted") as site:
                _Proxy.target = site.base
                _Proxy.inject = inject
                ThreadingHTTPServer.allow_reuse_address = True
                srv = ThreadingHTTPServer(("127.0.0.1", 0), _Proxy)
                threading.Thread(target=srv.serve_forever, daemon=True).start()
                try:
                    url = f"http://127.0.0.1:{srv.server_address[1]}/d#{tr.SITES[1]}"
                    html = tr.dom(browser, url, home / "chrome-profile-h")
                finally:
                    srv.shutdown()
                    srv.server_close()
                _verify("호스팅", html, groups)
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
