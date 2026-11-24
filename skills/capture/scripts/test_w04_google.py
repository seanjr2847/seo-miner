#!/usr/bin/env python3
"""구글 데이터·속도·분석(w04) — 받아 놓고 안 쓰던 값을 판정·근거로 쓰는지.

docs/borrow-from-claude-seo.md 의 1절 4번, 2절 4·5·8번, 3절 '속도'·GA4 행이 여기 모인다.
전부 고정 응답(fixture)으로 돈다 — 네트워크를 안 탄다.

self-check: python test_w04_google.py
"""
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
HOME = Path(tempfile.mkdtemp(prefix="seo-miner-w04-"))
os.environ["CAPTURE_HOME"] = str(HOME)

import brief  # noqa: E402
import db  # noqa: E402
import scoring  # noqa: E402


def _mem():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(db.SCHEMA)
    c.execute("INSERT INTO projects(id,name,type,domain,locale) VALUES(1,'p','saas','x.com','en-US')")
    return c


def _vit(**over):
    r = {"strategy": "mobile", "error": None, "origin_fallback": 0, "field_verdict": "AVERAGE",
         "field_lcp_ms": 1800, "field_inp_ms": 90, "field_cls": 0.02, "field_ttfb_ms": 300,
         "lab_score": 93, "lab_lcp_ms": 1900, "lab_cls": 0.02, "lab_tbt_ms": 40}
    r.update(over)
    return r


# ── 1절 4번: 현장 TTFB 판정 ─────────────────────────────────────────────────────

def test_field_ttfb_is_judged_against_one_threshold():
    """현장 TTFB 를 모으기만 하고 판정하지 않았다. 기준은 scoring.TTFB_GOOD_MS 한 벌."""
    assert scoring.TTFB_GOOD_MS == 800, "구글이 공개한 TTFB '좋음' 경계(p75 0.8초)와 다르다"
    # 다른 지표가 전부 기준 안이어도 TTFB 만 넘으면 한 줄이 선다
    adv = scoring.vitals_advice([_vit(field_ttfb_ms=scoring.TTFB_GOOD_MS + 400)])
    assert len(adv) == 1 and adv[0]["tag"] == "속도", adv
    assert f"TTFB {scoring.TTFB_GOOD_MS + 400}ms (기준 {scoring.TTFB_GOOD_MS}ms)" in adv[0]["now"], adv
    assert "서버" in adv[0]["fix"], adv[0]["fix"]
    # 경계값은 '좋음'이다(초과만 지적), 안 잰 값(None)은 통과도 실패도 아니다
    assert scoring.vitals_advice([_vit(field_ttfb_ms=scoring.TTFB_GOOD_MS)]) == []
    assert scoring.vitals_advice([_vit(field_ttfb_ms=None)]) == []
    # 실험실만 있는 행에는 TTFB 판정이 없다 — 현장 값만 모은다
    lab = _vit(field_lcp_ms=None, field_inp_ms=None, field_cls=None, field_ttfb_ms=None)
    assert scoring.vitals_advice([lab]) == []
    # LCP 와 같이 넘으면 LCP 가 먼저(고칠 것의 첫 문장), TTFB 는 같은 줄에 나란히
    both = scoring.vitals_advice([_vit(field_lcp_ms=4200, field_ttfb_ms=1900)])[0]
    assert both["now"].index("LCP") < both["now"].index("TTFB") and both["level"] == "bad", both


def test_brief_baseline_line_carries_ttfb():
    """요청문의 속도 줄 — 현장 줄에 TTFB 값, 기준 줄에 TTFB 기준. 숫자는 scoring 에서 온다."""
    url = "https://x.com/a"
    ctx = {"vitals_date": "2026-11-20",
           "vitals": {url: {"mobile": _vit(field_ttfb_ms=1200)}}}
    L = "\n".join(brief._vitals_lines(ctx, url))
    assert "TTFB 1,200ms" in L or "TTFB 1200ms" in L, L
    assert f"TTFB {scoring.TTFB_GOOD_MS}ms 이내" in L, L
    # TTFB 를 안 잰 옛 행은 값 자리를 지어내지 않는다
    L2 = "\n".join(brief._vitals_lines({"vitals": {url: {"mobile": _vit(field_ttfb_ms=None)}}}, url))
    assert "TTFB -" not in L2.split("기준:")[0], L2


# ── 3절 '속도': PSI 진단 확장 ───────────────────────────────────────────────────
# Lighthouse 12 꼴(옛 진단)과 13 꼴(*-insight)의 고정 응답. 필드 이름은 Lighthouse 소스
# (core/audits, types/lhr/audit-details.d.ts)로 확인한 것이다.
LH12 = {
    "lighthouseVersion": "12.8.2",
    "categories": {
        "performance": {"score": 0.42},
        "accessibility": {"score": 0.87, "auditRefs": [
            {"id": "color-contrast"}, {"id": "image-alt"}, {"id": "html-has-lang"},
            {"id": "video-caption"}, {"id": "some-new-a11y-audit"}]},
        "best-practices": {"score": 0.96, "auditRefs": [
            {"id": "errors-in-console"}, {"id": "is-on-https"}]},
    },
    "audits": {
        "largest-contentful-paint-element": {"score": 0, "scoreDisplayMode": "metricSavings",
            "details": {"type": "list", "items": [
                {"type": "table", "items": [{"node": {
                    "type": "node", "selector": "main > img.hero",
                    "snippet": '<img class="hero" src="/img/hero.jpg" loading="lazy">'}}]},
                {"type": "table", "items": [
                    {"phase": "TTFB", "timing": 610.4, "percent": "15%"},
                    {"phase": "Load Delay", "timing": 1900.2, "percent": "46%"},
                    {"phase": "Load Time", "timing": 800, "percent": "19%"},
                    {"phase": "Render Delay", "timing": 820, "percent": "20%"}]}]}},
        "lcp-lazy-loaded": {"score": 0, "scoreDisplayMode": "binary", "details": {
            "type": "table", "items": [{"node": {"snippet": '<img class="hero" src="/img/hero.jpg" loading="lazy">'}}]}},
        "render-blocking-resources": {"score": 0, "scoreDisplayMode": "metricSavings",
            "metricSavings": {"FCP": 450, "LCP": 300},
            "details": {"type": "opportunity", "overallSavingsMs": 300, "items": [
                {"url": "https://x.com/app.css", "totalBytes": 90000, "wastedMs": 150},
                {"url": "https://x.com/vendor.js", "totalBytes": 300000, "wastedMs": 310}]}},
        "unsized-images": {"score": 0.5, "scoreDisplayMode": "metricSavings",
            "metricSavings": {"CLS": 0.12},
            "details": {"type": "table", "items": [
                {"url": "https://x.com/a.png", "node": {"snippet": '<img src="/a.png">'}}]}},
        "third-party-summary": {"score": None, "scoreDisplayMode": "informative",
            "details": {"type": "table", "items": [
                {"entity": "Google Tag Manager", "transferSize": 120000, "blockingTime": 380},
                {"entity": "Intercom", "transferSize": 400000, "blockingTime": 90}]}},
        "unused-javascript": {"score": 0, "scoreDisplayMode": "metricSavings",
            "metricSavings": {"LCP": 600, "FCP": 0},
            "details": {"type": "opportunity", "overallSavingsMs": 600,
                        "overallSavingsBytes": 512000, "items": [
                {"url": "https://x.com/vendor.js", "totalBytes": 700000, "wastedBytes": 400000},
                {"url": "https://x.com/app.js", "totalBytes": 200000, "wastedBytes": 112000}]}},
        "unused-css-rules": {"score": 1, "scoreDisplayMode": "metricSavings",
            "metricSavings": {"LCP": 0}, "details": {"type": "opportunity", "items": []}},
        "font-display": {"score": 0, "scoreDisplayMode": "metricSavings",
            "metricSavings": {"FCP": 120},
            "details": {"type": "table", "items": [{"url": "https://x.com/f.woff2", "wastedMs": 120}]}},
        "long-tasks": {"score": None, "scoreDisplayMode": "informative",
            "details": {"type": "table", "items": [
                {"url": "https://x.com/vendor.js", "startTime": 900, "duration": 240},
                {"url": "https://x.com/app.js", "startTime": 1500, "duration": 620}]}},
        "dom-size": {"score": 0.4, "scoreDisplayMode": "numeric", "numericValue": 2412,
            "details": {"type": "table", "items": [{"statistic": "Total DOM Elements", "value": 2412}]}},
        "final-screenshot": {"score": None, "scoreDisplayMode": "informative",
            "details": {"type": "screenshot", "timing": 4000, "data": "data:image/jpeg;base64,/9j/AAAA"}},
        "color-contrast": {"score": 0, "scoreDisplayMode": "binary", "details": {
            "type": "table", "items": [{"node": {}}, {"node": {}}, {"node": {}}]}},
        "image-alt": {"score": 1, "scoreDisplayMode": "binary"},
        "html-has-lang": {"score": 0, "scoreDisplayMode": "binary", "details": {"items": [{}]}},
        "video-caption": {"score": None, "scoreDisplayMode": "notApplicable"},
        "some-new-a11y-audit": {"score": 0, "scoreDisplayMode": "binary"},
        "errors-in-console": {"score": 0, "scoreDisplayMode": "binary", "details": {"items": [{}, {}]}},
        "is-on-https": {"score": 1, "scoreDisplayMode": "binary"},
    },
}
LH13 = {
    "lighthouseVersion": "13.6.0",
    "categories": {
        "performance": {"score": 0.5},
        "agentic-browsing": {"score": 0.4, "auditRefs": [
            {"id": "agent-accessibility-tree", "weight": 1}, {"id": "llms-txt", "weight": 1},
            {"id": "cumulative-layout-shift", "weight": 1}, {"id": "ard-schema", "weight": 1},
            {"id": "webmcp-registered-tools", "weight": 1},
            {"id": "webmcp-schema-validity", "weight": 1}]},
    },
    "audits": {
        "lcp-breakdown-insight": {"score": 0, "scoreDisplayMode": "metricSavings",
            "details": {"type": "list", "items": [
                {"type": "table", "items": [
                    {"subpart": "timeToFirstByte", "label": "Time to first byte", "duration": 505.6},
                    {"subpart": "resourceLoadDelay", "label": "Resource load delay", "duration": 1200},
                    {"subpart": "resourceLoadDuration", "label": "Resource load duration", "duration": 300},
                    {"subpart": "elementRenderDelay", "label": "Element render delay", "duration": 95}]},
                {"type": "node", "selector": "div.hero > img",
                 "snippet": '<img src="/hero.webp" loading="lazy">'}]}},
        "lcp-discovery-insight": {"score": 0, "scoreDisplayMode": "numeric",
            "details": {"type": "list", "items": [
                {"type": "checklist", "items": {
                    "priorityHinted": {"label": "fetchpriority=high should be applied", "value": False},
                    "requestDiscoverable": {"label": "Request is discoverable", "value": True},
                    "eagerlyLoaded": {"label": "lazy load not applied", "value": False}}},
                {"type": "node", "snippet": '<img src="/hero.webp" loading="lazy">'}]}},
        "render-blocking-insight": {"score": 0, "scoreDisplayMode": "metricSavings",
            "metricSavings": {"FCP": 200, "LCP": 200},
            "details": {"type": "table", "items": [
                {"url": "https://x.com/site.css", "totalBytes": 50000, "wastedMs": 200}]}},
        "third-parties-insight": {"score": 1, "scoreDisplayMode": "informative",
            "details": {"type": "table", "items": [
                {"entity": "Hotjar", "transferSize": 90000, "mainThreadTime": 410}]}},
        "dom-size-insight": {"score": 1, "scoreDisplayMode": "numeric", "numericValue": 310,
            "details": {"type": "table", "items": []}},
        "agent-accessibility-tree": {"score": 1, "scoreDisplayMode": "binary"},
        "llms-txt": {"score": 0, "scoreDisplayMode": "binary"},
        "cumulative-layout-shift": {"score": 1, "scoreDisplayMode": "numeric"},
        "ard-schema": {"score": 0, "scoreDisplayMode": "binary"},
        "webmcp-registered-tools": {"score": None, "scoreDisplayMode": "notApplicable"},
        "webmcp-schema-validity": {"score": 1, "scoreDisplayMode": "informative"},
    },
}


def test_psi_diag_names_the_resources_and_sorts_by_savings():
    """PSI 진단 — LCP 요소·렌더 차단·크기 없는 이미지·LCP 지연 로드·서드파티·안 쓰는 JS/CSS·
    글꼴·긴 작업·DOM 크기를 **줄일 시간이 큰 순**으로, 자원 이름과 함께 남긴다."""
    import collect_vitals as cv
    assert cv.parse_diag({}) is None and cv.parse_diag({"audits": {"largest-contentful-paint": {}}}) is None, \
        "진단이 없는 응답(옛 호출)에 '봤고 없음'을 지어낸다"
    d = json.loads(cv.parse_diag(LH12))
    ids = [x["id"] for x in d["items"]]
    assert set(ids) <= set(cv.DIAG_AUDITS), ids
    # 줄일 시간 순: 안 쓰는 JS 600 > 렌더 차단 450 > 글꼴 120 > (0 인 것들)
    assert ids[:3] == ["unused-javascript", "render-blocking-resources", "font-display"], ids
    assert "unused-css-rules" not in ids, "통과했고 줄일 것도 없는 진단을 적었다"
    by = {x["id"]: x for x in d["items"]}
    assert by["unused-javascript"]["kb"] == 500 and by["unused-javascript"]["ms"] == 600, by
    # 줄 안의 자원은 그 진단의 큰 것부터
    assert by["render-blocking-resources"]["items"] == ["https://x.com/vendor.js", "https://x.com/app.css"], by
    assert by["third-party-summary"]["items"][0] == "Google Tag Manager", by["third-party-summary"]
    assert by["long-tasks"]["val"] == 620 and by["long-tasks"]["n"] == 2, by["long-tasks"]
    assert by["dom-size"]["val"] == 2412 and by["dom-size"]["fail"], by["dom-size"]
    assert by["unsized-images"]["fail"] and by["unsized-images"]["items"] == ["https://x.com/a.png"]
    assert by["lcp-lazy-loaded"]["fail"], by
    # LCP 요소와 구간
    lcp = d["lcp"]
    assert "hero.jpg" in lcp["snippet"] and lcp["selector"] == "main > img.hero" and lcp["lazy"] is True, lcp
    assert lcp["phases"] == {"ttfb": 610, "load_delay": 1900, "load": 800, "render_delay": 820}, lcp

    # Lighthouse 13 — *-insight 가 옛 이름으로 접힌다. 12 로 잰 행과 같은 열쇠로 읽힌다.
    d13 = json.loads(cv.parse_diag(LH13))
    by13 = {x["id"]: x for x in d13["items"]}
    assert set(by13) == {"render-blocking-resources", "third-party-summary", "lcp-lazy-loaded"}, by13
    assert by13["render-blocking-resources"]["ms"] == 200
    assert by13["third-party-summary"]["items"] == ["Hotjar"]
    l13 = d13["lcp"]
    assert l13["phases"] == {"ttfb": 506, "load_delay": 1200, "load": 300, "render_delay": 95}, l13
    assert (l13["lazy"], l13["discoverable"], l13["priority"]) == (True, True, False), l13
    assert "hero.webp" in l13["snippet"], l13
    # 전환기 — 옛 진단과 새 진단이 한 응답에 같이 오면 새 것 하나만
    both = {"audits": {**LH12["audits"], **LH13["audits"]}}
    n = [x for x in json.loads(cv.parse_diag(both))["items"] if x["id"] == "render-blocking-resources"]
    assert len(n) == 1 and n[0]["ms"] == 200, n


def test_psi_categories_version_agentic_and_screenshot():
    """접근성·권장사항 점수와 실패 항목, lighthouseVersion, agentic-browsing X/N, 썸네일."""
    import collect_vitals as cv
    assert cv.parse_cats({}) is None, "카테고리가 없는 응답에 점수를 지어낸다"
    c = json.loads(cv.parse_cats(LH12))
    assert c["accessibility"]["score"] == 87 and c["best-practices"]["score"] == 96, c
    # 해당 없음은 실패가 아니다. 모르는 id 도 실패면 싣는다(화면이 id 로 보인다)
    assert c["accessibility"]["fail"] == [{"id": "color-contrast", "n": 3}, {"id": "html-has-lang", "n": 1},
                                          {"id": "some-new-a11y-audit", "n": 0}], c
    assert c["best-practices"]["fail"] == [{"id": "errors-in-console", "n": 2}], c
    assert set(cv.CATEGORIES) == {"accessibility", "best-practices"}
    for aid, label in cv.CAT_AUDITS.items():
        assert label and not label.endswith("."), (aid, label)

    # agentic — 점수가 아니라 X/N. 해당 없음·참고용은 N 에서 뺀다
    assert cv.parse_agentic(LH12) is None, "그 카테고리가 없는 응답에 0/0 을 지어낸다"
    a = json.loads(cv.parse_agentic(LH13))
    assert (a["pass"], a["total"]) == (2, 4) and a["fail"] == ["llms-txt", "ard-schema"], a

    row = cv.parse("https://x.com/a", "mobile", {"lighthouseResult": LH12})
    assert row["lh_version"] == "12.8.2" and row["shot"] == "data:image/jpeg;base64,/9j/AAAA", row
    assert row["diag_json"] and row["cat_json"] and row["agentic_json"] is None
    assert cv.parse_shot({"audits": {"final-screenshot": {"details": {"data": "x" * 10}}}}) is None, \
        "data: 주소가 아닌 것을 그림으로 남긴다"
    big = "data:image/jpeg;base64," + "A" * cv.SHOT_MAX
    assert cv.parse_shot({"audits": {"final-screenshot": {"details": {"data": big}}}}) is None

    # 적재 — 새 칸이 그대로 들어가고, 썸네일은 주소·기기마다 최신 한 장
    conn = _mem()
    db.write_page_vitals(conn, 1, "2026-11-20", [row])
    db.write_page_vital_shots(conn, 1, "2026-11-20", [row])
    db.write_page_vital_shots(conn, 1, "2026-11-24", [{**row, "shot": "data:image/jpeg;base64,NEW"}])
    db.write_page_vital_shots(conn, 1, "2026-11-25", [{**row, "shot": None}])    # 그림이 없던 회차는 안 지운다
    got = conn.execute("SELECT * FROM page_vitals").fetchone()
    assert got["lh_version"] == "12.8.2" and json.loads(got["diag_json"])["lcp"]["lazy"] is True
    assert json.loads(got["cat_json"])["accessibility"]["score"] == 87
    shots = conn.execute("SELECT checked_date, data FROM page_vital_shots").fetchall()
    assert [(s["checked_date"], s["data"]) for s in shots] == [("2026-11-24", "data:image/jpeg;base64,NEW")]


def test_psi_requests_the_categories_and_survives_an_unknown_one():
    """한 호출에 카테고리를 얹는다. agentic-browsing 때문에 400 이면 빼고 다시 묻는다 —
    새 카테고리 하나 때문에 속도 측정이 통째로 죽지 않는다."""
    import requests

    import collect_vitals as cv

    class Resp:
        def __init__(self, status, body):
            self.status_code, self._body = status, body

        def json(self):
            return self._body

    sent = []

    def fake_get(url, params=None, timeout=None):
        cats = [v for k, v in params if k == "category"]
        sent.append(cats)
        if cv.AGENTIC in cats:
            return Resp(400, {"error": {"message": "Invalid value at 'category'"}})
        return Resp(200, {"lighthouseResult": LH12})

    orig, cv._AGENTIC_OK[0] = requests.get, True
    requests.get = fake_get
    try:
        row = cv.fetch("https://x.com/a", "mobile")
        assert not row.get("error") and row["lab_score"] == 42, row
        assert sent[0] == list(cv.PSI_CATEGORIES) and cv.AGENTIC not in sent[1], sent
        assert {"performance", "seo", "accessibility", "best-practices"} <= set(sent[1]), sent
        cv.fetch("https://x.com/b", "mobile")
        assert len(sent) == 3 and cv.AGENTIC not in sent[2], "한 번 거절된 카테고리를 또 실었다"
        # 400 의 원인이 다른 데 있으면(빼도 400) 끄지 않는다
        cv._AGENTIC_OK[0] = True
        requests.get = lambda url, params=None, timeout=None: Resp(400, {"error": {"message": "bad url"}})
        assert cv.fetch("https://x.com/c", "mobile")["error"].startswith("HTTP 400")
        assert cv._AGENTIC_OK[0] is True
    finally:
        requests.get, cv._AGENTIC_OK[0] = orig, True


# ── 3절 '속도': CrUX History 25주 ───────────────────────────────────────────────
def _crux_body(n=3):
    """queryHistoryRecord 응답 꼴(https://developer.chrome.com/docs/crux/history-api)."""
    ends = [(2026, 11, 7 + 7 * i) for i in range(n)]
    return {"record": {
        "key": {"url": "https://x.com/a", "formFactor": "PHONE"},
        "metrics": {
            "largest_contentful_paint": {"percentilesTimeseries": {"p75s": [2100, None, 2900][:n]}},
            "interaction_to_next_paint": {"percentilesTimeseries": {"p75s": [180, 190, 210][:n]}},
            "cumulative_layout_shift": {"percentilesTimeseries": {"p75s": ["0.05", "0.05", "NaN"][:n]}},
            "experimental_time_to_first_byte": {"percentilesTimeseries": {"p75s": [700, 750, 990][:n]}}},
        "collectionPeriods": [{"firstDate": {"year": y, "month": m, "day": d - 6 if d > 6 else 1},
                               "lastDate": {"year": y, "month": m, "day": d}} for y, m, d in ends]}}


def test_crux_history_parses_weeks_and_keeps_missing_as_none():
    import collect_vitals as cv
    rows = cv.parse_crux_history(_crux_body())
    by = {(m, end): v for m, end, v in rows}
    assert by[("lcp_ms", "2026-11-07")] == 2100 and by[("lcp_ms", "2026-11-21")] == 2900, by
    assert by[("lcp_ms", "2026-11-14")] is None, "표본이 모자란 주(null)를 0 으로 적었다"
    assert by[("cls", "2026-11-07")] == 0.05, "CLS 는 문자열로 온다"
    assert by[("cls", "2026-11-21")] is None, "NaN 을 숫자로 적었다"
    assert by[("ttfb_ms", "2026-11-21")] == 990
    assert cv.parse_crux_history({}) == []
    assert set(cv.CRUX_METRICS) == {"lcp_ms", "inp_ms", "cls", "ttfb_ms"}


def test_crux_history_collects_page_or_origin_and_skips_without_key():
    """키가 없으면 건너뛰고 사유를 남긴다. 현장 값이 그 페이지 것인 주소만 주소로 묻고,
    사이트 전체 값으로 받은 주소는 오리진을 묻는다. 404(표본 부족)는 실패가 아니다."""
    import requests

    import collect_vitals as cv

    class Resp:
        def __init__(self, status, body):
            self.status_code, self._body = status, body

        def json(self):
            return self._body

    conn = _mem()
    rows = [_vit(url="https://x.com/a"), _vit(url="https://x.com/a", strategy="desktop"),
            _vit(url="https://x.com/b", origin_fallback=1),
            _vit(url="https://x.com/c", origin_fallback=1),
            _vit(url="https://x.com/d", error="HTTP 500")]
    keys = {k: os.environ.pop(k, None) for k in ("CRUX_API_KEY", "PAGESPEED_API_KEY")}
    sent = []

    def fake_post(url, params=None, json=None, timeout=None):
        sent.append((dict(params), dict(json)))
        if json.get("url") == "https://x.com/a" and json["formFactor"] == "DESKTOP":
            return Resp(404, {"error": {"code": 404, "message": "chrome ux report data not found"}})
        return Resp(200, _crux_body())

    orig = requests.post
    requests.post = fake_post
    try:
        calls, note = cv.collect_crux_history(conn, 1, rows, ["mobile", "desktop"])
        assert (calls, sent) == (0, []) and note.strip() == "crux=skip:no_key", (calls, note)
        os.environ["PAGESPEED_API_KEY"] = "k1"
        calls, note = cv.collect_crux_history(conn, 1, rows, ["mobile", "desktop"])
        asked = [(b.get("url") or b.get("origin"), "origin" in b, b["formFactor"]) for _, b in sent]
        assert asked == [("https://x.com/a", False, "PHONE"), ("https://x.com/a", False, "DESKTOP"),
                         ("https://x.com", True, "PHONE")], asked            # 오리진은 한 번만
        assert all(p == {"key": "k1"} for p, _ in sent)
        assert calls == 3 and "nodata=1" in note and "crux=" in note, note
        n = conn.execute("SELECT COUNT(*) FROM crux_history").fetchone()[0]
        assert n == 2 * 3 * 4, n          # 두 대상 × 3주 × 4지표
        org = conn.execute("SELECT DISTINCT url, origin FROM crux_history WHERE origin=1").fetchall()
        assert [(r["url"], r["origin"]) for r in org] == [("https://x.com", 1)]
        # 다시 받아도 안 늘어난다(겹치는 주)
        cv.collect_crux_history(conn, 1, rows, ["mobile", "desktop"])
        assert conn.execute("SELECT COUNT(*) FROM crux_history").fetchone()[0] == n
        # CRUX_API_KEY 가 따로 있으면 그것을 쓴다
        os.environ["CRUX_API_KEY"] = "k2"
        sent.clear()
        cv.collect_crux_history(conn, 1, rows[:1], ["mobile"])
        assert sent[0][0] == {"key": "k2"}
        # 권한 없음(403) — 남은 호출도 같은 답이다. 멈추고 사유를 남긴다(예외로 안 올린다)
        requests.post = lambda *a, **k: (sent.append(1), Resp(403, {"error": {
            "message": "Chrome UX Report API has not been used in project"}}))[1]
        sent.clear()
        calls, note = cv.collect_crux_history(conn, 1, rows, ["mobile", "desktop"])
        assert calls == 0 and len(sent) == 1 and note.startswith("crux=skip:HTTP_403"), (calls, note)
    finally:
        requests.post = orig
        for k, v in keys.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v


def test_speed_advice_names_this_pages_resources():
    """권고가 고정 문장에서 멈추지 않고 이 페이지의 자원을 댄다 — 진단이 있을 때만."""
    import collect_vitals as cv
    diag = cv.parse_diag(LH12)
    slow = _vit(field_lcp_ms=4200, field_cls=0.3)
    plain = scoring.vitals_advice([slow])[0]["fix"]
    assert "이 페이지에서는" not in plain, "진단이 없는 행에 자원을 지어낸다"
    fix = scoring.vitals_advice([{**slow, "diag_json": diag}])[0]["fix"]
    assert fix.startswith(plain) and "이 페이지에서는:" in fix, fix
    assert "hero.jpg" in fix and "LCP 이미지 지연 로드" in fix, fix
    assert "렌더 차단 자원(vendor.js, app.css)" in fix and "가장 긴 구간은 자원을 늦게 발견" in fix, fix
    # CLS 도 넘었으니 크기 없는 이미지가 같이 선다. INP 는 안 넘었으니 서드파티는 안 선다
    assert "크기 없는 이미지(a.png)" in fix and "서드파티" not in fix, fix
    # 지표마다 그 지표의 진단만 — CLS 만 넘은 행에 LCP 쪽 자원(렌더 차단)을 대지 않는다
    cls_only = scoring.vitals_advice([{**_vit(field_cls=0.3), "diag_json": diag}])[0]["fix"]
    assert "크기 없는 이미지(a.png)" in cls_only and "렌더 차단" not in cls_only         and "LCP 요소" not in cls_only, cls_only
    d = scoring.vitals_diag({"diag_json": diag})
    assert d["items"][0]["label"] == cv.DIAG_AUDITS["unused-javascript"][0], d["items"][0]
    assert scoring.vitals_diag({}) == {"lcp": None, "items": []}
    assert scoring.vitals_diag({"diag_json": "{bad"}) == {"lcp": None, "items": []}
    # 지표 → 진단 표는 진단 이름표의 부분집합이다(없는 id 를 가리키면 그 줄은 영영 안 선다)
    for metric, ids in scoring._DIAG_OF.items():
        assert set(ids) <= set(cv.DIAG_AUDITS), (metric, ids)


# ── 2절 5번: page_vitals 상대 악화 ──────────────────────────────────────────────
def test_vitals_regression_compares_only_the_same_scope():
    P75, LAB = scoring.VITALS_REGRESS_P75, scoring.VITALS_REGRESS_LAB
    assert (P75, LAB) == (0.20, 10)
    prev = _vit(field_lcp_ms=2000, field_inp_ms=100, field_cls=0.05, field_ttfb_ms=400, lab_score=90)
    # 20% 는 걸리고 19% 는 안 걸린다. 기준을 넘었는지는 over 로 따로 적는다
    cur = _vit(field_lcp_ms=2400, field_inp_ms=119, field_cls=0.05, field_ttfb_ms=900, lab_score=81)
    got = scoring.vitals_regression(prev, cur)
    assert [(c["metric"], c["pct"], c["over"]) for c in got["changes"]] == [
        ("LCP", 20, False), ("TTFB", 125, True)], got
    assert got["lab"] is None, "9점 내린 것을 악화로 셌다"
    assert scoring.vitals_regression(prev, _vit(lab_score=80, field_lcp_ms=2000, field_inp_ms=100,
                                                field_cls=0.05, field_ttfb_ms=400))["lab"] == {
        "prev": 90, "cur": 80, "drop": 10}
    # 범위가 다르면(이 페이지 값 대 사이트 전체 값) 견주지 않는다
    assert scoring.vitals_regression(prev, {**cur, "origin_fallback": 1}) is None
    # 못 잰 행·없는 행·좋아진 행
    assert scoring.vitals_regression(prev, {**cur, "error": "HTTP 500"}) is None
    assert scoring.vitals_regression(None, cur) is None
    assert scoring.vitals_regression(cur, prev) is None
    # Lighthouse 판이 바뀐 회차끼리는 실험실 점수를 견주지 않는다(현장 값은 견준다)
    v = scoring.vitals_regression({**prev, "lh_version": "12.8.2"},
                                  {**prev, "lab_score": 60, "lh_version": "13.6.0"})
    assert v is None, v
    assert scoring.vitals_regression({**prev, "lh_version": "13.6.0"},
                                     {**prev, "lab_score": 60, "lh_version": "13.6.0"})["lab"]["drop"] == 30

    conn = _mem()
    u = "https://x.com/a"
    db.write_page_vitals(conn, 1, "2026-11-03", [{**prev, "url": u}])
    db.write_page_vitals(conn, 1, "2026-11-10", [{**prev, "url": u, "error": "HTTP 500"}])   # 못 잰 날은 건너뛴다
    db.write_page_vitals(conn, 1, "2026-11-17", [{**cur, "url": u},
                                                 {**prev, "url": u, "strategy": "desktop"}])
    reg = scoring.vitals_regressions(conn, 1)
    assert set(reg) == {u} and set(reg[u]) == {"mobile"}, reg
    assert reg[u]["mobile"]["prev_date"] == "2026-11-03", reg
    # 요청문 — 속도 줄에 '지난 측정 대비'가 선다
    ctx = {"vitals_date": "2026-11-17", "vitals": {u: {"mobile": {**cur, "url": u}}},
           "vitals_regress": reg}
    L = "\n".join(brief._vitals_lines(ctx, u))
    assert "지난 측정(2026-11-03) 대비" in L and "LCP 2.0초 → 2.4초(+20%)" in L, L
    assert "TTFB 400ms → 900ms(+125%, 기준 넘음)" in L, L
    assert "대비" not in "\n".join(brief._vitals_lines({**ctx, "vitals_regress": {}}, u))


def test_crux_trend_then_and_now():
    conn = _mem()
    u = "https://x.com/a"
    db.write_crux_history(conn, 1, [(u, 0, "PHONE", "lcp_ms", "2026-06-06", 2100.0),
                                    (u, 0, "PHONE", "lcp_ms", "2026-06-13", None),
                                    (u, 0, "PHONE", "lcp_ms", "2026-11-21", 2900.0),
                                    (u, 0, "PHONE", "cls", "2026-11-21", 0.05)])
    t = scoring.crux_trend(conn, 1)
    assert t[u]["PHONE"]["lcp_ms"] == [["2026-06-06", 2100.0], ["2026-11-21", 2900.0]], t
    tn = scoring.crux_then_now(t[u]["PHONE"]["lcp_ms"])
    assert (tn["then"], tn["now"], tn["pct"]) == (["2026-06-06", 2100.0], ["2026-11-21", 2900.0], 38), tn
    assert scoring.crux_then_now(t[u]["PHONE"]["cls"]) is None, "점 하나로 추세를 말한다"
    # since — 고치기 전 마지막 점이 '그때'다
    s = [["2026-06-06", 3000.0], ["2026-08-01", 2800.0], ["2026-11-21", 2000.0]]
    assert scoring.crux_then_now(s, since="2026-08-15")["then"] == ["2026-08-01", 2800.0]
    assert scoring.crux_trend(conn, 99) == {}
    L = "\n".join(brief._vitals_lines({"vitals": {u: {"mobile": _vit()}}, "crux_trend": t}, u))
    assert "현장 추세(CrUX 주별 p75)" in L and "LCP 2.1초(2026-06-06) → 2.9초(2026-11-21)" in L, L


# ── 2절 4번: monthly_searches ───────────────────────────────────────────────────
def test_monthly_searches_are_stored_and_read_as_trend():
    import collect_metrics as cm
    ms = [{"year": 2026, "month": 10, "search_volume": 900}, {"year": 2025, "month": 11, "search_volume": 100},
          {"year": 2026, "month": 9, "search_volume": None}, {"year": "x"}, None]
    assert json.loads(cm.monthly_series(ms)) == [["2025-11", 100], ["2026-10", 900]]
    assert cm.monthly_series(None) is None and cm.monthly_series([]) is None

    # 12달: 12월이 중앙값의 두 배 이상 → 철을 탄다. 최근 3달 평균이 앞 3달보다 20% 이상 ↑
    year = [[f"2026-{m:02d}", v] for m, v in zip(range(1, 13), (100, 100, 100, 100, 100, 100,
                                                                100, 100, 100, 120, 150, 400))]
    t = scoring.volume_trend(year)
    assert t["months"] == 12 and t["peak"] == ["2026-12", 400] and t["seasonal"] is True, t
    assert t["recent"] == "up" and t["recent_pct"] == 123, t
    flat = scoring.volume_trend(json.dumps([[f"2026-{m:02d}", 100] for m in range(1, 13)]))
    assert (flat["seasonal"], flat["recent"], flat["recent_pct"]) == (False, "flat", 0), flat
    down = scoring.volume_trend([[f"2026-{m:02d}", v] for m, v in zip(range(1, 7), (100, 100, 100, 70, 70, 70))])
    assert down["recent"] == "down" and down["seasonal"] is None, down      # 6달로는 계절성을 안 말한다
    assert scoring.volume_trend([["2026-01", 5]])["recent"] is None
    assert scoring.volume_trend(None) is None and scoring.volume_trend("[") is None

    # 수집 — 같은 응답의 monthly_searches 가 keywords.monthly_json 에 남고, 없는 응답은 안 덮는다
    os.environ["DATAFORSEO_LOGIN"], os.environ["DATAFORSEO_PASSWORD"] = "login", "pw"
    conn = db.connect()
    conn.execute("INSERT INTO projects(name, domain, locale) VALUES('mw','mw.com','en-US')")
    pid = conn.execute("SELECT id FROM projects WHERE name='mw'").fetchone()["id"]
    conn.executemany("INSERT INTO keywords(project_id, keyword, locale, source, monthly_json)"
                     " VALUES(?,?,'en-US','seed',?)", [(pid, "winter coat", None),
                                                       (pid, "old one", '[["2025-01", 7]]')])
    conn.commit()

    def fake_post(path, body):
        if path == cm.SV_PATH:
            return [{"keyword": "winter coat", "search_volume": 400, "cpc": 1.0, "monthly_searches": [
                        {"year": 2026, "month": 10, "search_volume": 400},
                        {"year": 2026, "month": 9, "search_volume": 150}]},
                    {"keyword": "old one", "search_volume": 9, "monthly_searches": None}], 0.05
        return [], 0.0
    import contextlib
    import io
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        res = cm.collect("mw", conn=conn, post=fake_post)
    assert res.ok, res
    got = {r["keyword"]: r["monthly_json"] for r in conn.execute(
        "SELECT keyword, monthly_json FROM keywords WHERE project_id=?", (pid,))}
    assert json.loads(got["winter coat"]) == [["2026-09", 150], ["2026-10", 400]], got
    assert got["old one"] == '[["2025-01", 7]]', "월별 값이 없는 응답이 지난 값을 지웠다"
    conn.close()


# ── 2절 8번 ①: GA4 전환 이벤트 미설정 ───────────────────────────────────────────
def _ga4_site(conn, key_events=0.0):
    """클릭 50·세션 40 인 페이지 하나. GSC 한 회차 + GA4 유기 스냅샷."""
    conn.execute("INSERT INTO gsc_snapshots(project_id,snapshot_date,period_days,query,page,"
                 "clicks,impressions,ctr,position) VALUES(1,'2026-11-20',28,'q','https://x.com/a',50,900,0.05,4.0)")
    db.write_ga4_snapshot(conn, 1, "2026-11-20", 28,
                          [("/a", 40, 60, key_events, 0, 0.5, 30, 1.2)])


def test_no_conversions_anywhere_means_unset_not_a_bad_page():
    """속성 전체 key events 가 0 이면 '전환 없는 페이지' 표와 0.85 승수를 끄고 미설정이라 말한다."""
    conn = _mem()
    _ga4_site(conn)
    # 채널 합계가 있다: 전체 세션 300 에 전환 5 — 설정돼 있다. 이 페이지의 0 은 페이지의 사실이다
    db.write_ga4_channels(conn, 1, "2026-11-20", 28, [("Organic Search", 40, 35, 0.0),
                                                      ("Paid Search", 260, 200, 5.0)], (300, 230, 5.0))
    assert scoring.ga4_conversions_unset(conn, 1) is False
    assert [r["page"] for r in scoring.zero_conversion_pages(conn, 1)] == ["https://x.com/a"]
    ctx = {"conn": conn, "pid": 1, "cur": "2026-11-20", "ga4": scoring._ga4_agg(conn, 1, "2026-11-20"),
           "page_agg": scoring._page_agg(conn, 1, "2026-11-20", 28)}
    m = scoring._ga4_metrics(ctx, page="https://x.com/a")
    assert "ga4_conv_unset" not in m and scoring.value_mult(m) == scoring.GA4_MULT_LO, m

    # 속성 전체 전환 0 — 미설정. 표는 비고 승수는 정확히 1.0
    db.write_ga4_channels(conn, 1, "2026-11-20", 28, [("Organic Search", 40, 35, 0.0),
                                                      ("Paid Search", 260, 200, 0.0)], (300, 230, 0.0))
    assert scoring.ga4_conversions_unset(conn, 1) is True
    assert scoring.zero_conversion_pages(conn, 1) == [], "미설정 사이트에 '전환 없는 페이지'를 세웠다"
    ctx.pop("_ga4_conv_unset")
    m = scoring._ga4_metrics(ctx, page="https://x.com/a")
    assert m["ga4_conv_unset"] is True and scoring.value_mult(m) == 1.0, m
    st = scoring.ga4_property_state(conn, 1)
    assert st["conv_unset"] is True and st["state"] == "ok", st
    # 승수 자체의 규칙은 그대로다 — 깃발이 없으면 0 전환은 여전히 내린다
    assert scoring.value_mult({"ga4_clicks": 50, "ga4_sessions": 40, "ga4_key_events": 0}) == scoring.GA4_MULT_LO
    assert scoring.value_mult({"ga4_clicks": 50, "ga4_sessions": 40, "ga4_key_events": 0,
                               "ga4_conv_unset": True}) == 1.0

    # 채널 합계가 없는 옛 수집 — 유기 스냅샷 합으로 대신한다
    old = _mem()
    _ga4_site(old)
    assert scoring.ga4_conversions_unset(old, 1) is True and scoring.zero_conversion_pages(old, 1) == []
    old2 = _mem()
    _ga4_site(old2)
    db.write_ga4_snapshot(old2, 1, "2026-11-20", 28, [("/a", 40, 60, 0, 0, 0.5, 30, 1.2),
                                                      ("/b", 30, 30, 2, 0, 0.5, 30, 1.2)])
    assert scoring.ga4_conversions_unset(old2, 1) is False
    assert [r["page"] for r in scoring.zero_conversion_pages(old2, 1)] == ["https://x.com/a"]
    # GA4 미연결·표본 미달은 미설정이라고 말하지 않는다
    assert scoring.ga4_conversions_unset(_mem(), 1) is False
    few = _mem()
    db.write_ga4_channels(few, 1, "2026-11-20", 28, [], (scoring.GA4_NOCONV_MIN_SESSIONS - 1, 3, 0.0))
    assert scoring.ga4_conversions_unset(few, 1) is False


def test_ga4_channel_groups_and_daily():
    conn = _mem()
    assert scoring.ga4_channel_groups(conn, 1) is None and scoring.ga4_daily(conn, 1) == []
    assert scoring.ga4_ai_channel_check(conn, 1) is None
    db.write_ga4_channels(conn, 1, "2026-11-20", 28, [("Direct", 90, 70, 5.0), ("Organic Search", 30, 25, 2.0)],
                          (120, 90, 7.0))
    db.write_ga4_ai_channel(conn, 1, "2026-11-20", 28, [])
    g = scoring.ga4_channel_groups(conn, 1)
    assert [(r["channel"], r["share"]) for r in g["rows"]] == [("Direct", 75.0), ("Organic Search", 25.0)], g
    assert g["total"] == {"sessions": 120, "active_users": 90, "key_events": 7.0}, g
    chk = scoring.ga4_ai_channel_check(conn, 1)
    assert chk["channel_sessions"] == 0 and chk["unlisted"] == [], chk       # 쟀고 0
    db.write_ga4_daily(conn, 1, [("2026-11-02", 5, 4, 0), ("2026-11-01", 3, 3, 1)])
    db.write_ga4_daily(conn, 1, [("2026-11-02", 6, 5, 0)])                    # 보정 — 덮는다
    assert [(r["day"], r["sessions"]) for r in scoring.ga4_daily(conn, 1)] == [("2026-11-01", 3), ("2026-11-02", 6)]


# ── 2절 4번: URL 검사가 같이 주던 것 ────────────────────────────────────────────
RICH = {"verdict": "FAIL", "detectedItems": [
    {"richResultType": "Products", "items": [
        {"name": "Coat", "issues": [
            {"issueMessage": "Missing field 'price'", "severity": "ERROR"},
            {"issueMessage": "Missing field 'review'", "severity": "WARNING"}]}]},
    {"richResultType": "Breadcrumbs", "items": [{"name": "Unnamed item"}]}]}


def test_rich_results_become_a_google_verdict_line():
    """rich_results_json 을 저장만 하고 아무도 안 읽었다 — 구글이 직접 낸 판정 한 줄로."""
    v = scoring.rich_results_verdict(json.dumps(RICH))
    assert (v["verdict"], v["types"], v["errors"], v["warnings"]) == ("FAIL", ["Products", "Breadcrumbs"], 1, 1), v
    assert v["issues"][0]["severity"] == "ERROR", "오류가 먼저다"
    assert scoring.rich_results_line(v) == "리치 결과 FAIL · 유형 Products, Breadcrumbs · 오류 1 · 경고 1"
    adv = scoring.google_verdict_advice({"rich_results_json": json.dumps(RICH)})
    assert len(adv) == 1 and adv[0]["tag"] == scoring.GOOGLE_VERDICT_TAG and adv[0]["level"] == "bad", adv
    assert "Products: Missing field 'price'(오류)" in adv[0]["now"], adv[0]["now"]
    warn = {"verdict": "PASS", "detectedItems": [{"richResultType": "FAQ", "items": [
        {"name": "x", "issues": [{"issueMessage": "m", "severity": "WARNING"}]}]}]}
    assert scoring.google_verdict_advice({"rich_results_json": json.dumps(warn)})[0]["level"] == "warn"
    # 문제 없는 PASS 는 고칠 것이 아니다 — 줄은 안 서고, 요약 줄은 남는다
    ok = {"verdict": "PASS", "detectedItems": [{"richResultType": "FAQ", "items": [{"name": "x"}]}]}
    assert scoring.google_verdict_advice({"rich_results_json": json.dumps(ok)}) == []
    assert scoring.rich_results_line(scoring.rich_results_verdict(ok)) == "리치 결과 PASS · 유형 FAQ · 오류 0 · 경고 0"
    # 구글이 안 준 것(NULL)·깨진 값은 '문제 없음'이 아니라 '없음'이다
    for nothing in (None, "", "{bad", "[]", {}):
        assert scoring.rich_results_verdict(nothing) is None, nothing
        assert scoring.google_verdict_advice({"rich_results_json": nothing}) == []
    assert scoring.rich_results_line(None) == ""


def test_url_inspection_keeps_referrers_crawler_and_sitemaps():
    import collect_index as ci
    res = {"indexStatusResult": {"verdict": "NEUTRAL", "coverageState": "Discovered - currently not indexed",
                                 "referringUrls": ["https://x.com/", "https://y.com/p"],
                                 "sitemap": ["https://x.com/sitemap.xml"], "crawledAs": "MOBILE"},
           "richResultsResult": RICH}
    row = ci.to_row("https://x.com/a", res)
    assert json.loads(row["referring_urls_json"]) == ["https://x.com/", "https://y.com/p"], row
    assert json.loads(row["sitemaps_json"]) == ["https://x.com/sitemap.xml"] and row["crawled_as"] == "MOBILE"
    # 색인 결과는 왔는데 그 칸이 없다 — '구글이 하나도 모른다'('[]'). 모름 표기는 적지 않는다
    bare = ci.to_row("https://x.com/b", {"indexStatusResult": {
        "verdict": "NEUTRAL", "crawledAs": "CRAWLING_USER_AGENT_UNSPECIFIED"}})
    assert (bare["referring_urls_json"], bare["sitemaps_json"], bare["crawled_as"]) == ("[]", "[]", None), bare
    # 색인 결과 자체가 없으면 칸을 안 만든다(NULL = 안 받았다)
    assert "referring_urls_json" not in ci.to_row("https://x.com/c", {})

    conn = _mem()
    db.write_index_status(conn, 1, "2026-11-20", [row, bare, ci.to_row("https://x.com/c", {})])
    ev = scoring.index_evidence(conn, 1)
    a = ev["https://x.com/a"]
    assert (a["found"], a["referring_n"], a["crawled_as"]) == ("links", 2, "MOBILE"), a
    assert a["rich"]["errors"] == 1, a
    assert ev["https://x.com/b"]["found"] == "none" and ev["https://x.com/c"]["found"] is None, ev
    conn.execute("UPDATE gsc_index_status SET referring_urls_json='[]' WHERE url='https://x.com/a'")
    assert scoring.index_evidence(conn, 1)["https://x.com/a"]["found"] == "sitemap"
    # 요청문 — 색인 막힘 근거에 발견 경로와 구글 판정이 선다
    ctx = {"index_issues": [{"url": "https://x.com/b", "bucket": "not_indexed", "verdict": "NEUTRAL",
                             "coverage_state": "Discovered - currently not indexed", "detail": "d"}],
           "index_evidence": ev}
    L = "\n".join(brief._ev_index({"target": "https://x.com/b"}, ctx, []))
    assert "사이트맵에도 없고" in L, L
    L = "\n".join(brief._ev_index({"target": "https://x.com/b"}, {**ctx, "index_evidence": {
        "https://x.com/b": a}}, []))
    assert "가리키는 페이지 2개" in L and "https://y.com/p" in L and "리치 결과 FAIL" in L, L


def test_gsc_sitemaps_are_loaded_every_round():
    import collect_index as ci
    resp = {"sitemap": [
        {"path": "https://x.com/sitemap.xml", "type": "sitemap", "isSitemapsIndex": True, "isPending": False,
         "lastSubmitted": "2026-10-01T00:00:00Z", "lastDownloaded": "2026-11-19T00:00:00Z",
         "errors": "2", "warnings": "0",
         "contents": [{"type": "web", "submitted": "120", "indexed": "0"},
                      {"type": "image", "submitted": "30"}]},
        {"path": "https://x.com/feed", "type": "rssFeed", "isPending": True}]}
    rows = ci.sitemap_rows(resp)
    assert rows[0]["errors"] == 2 and rows[0]["submitted"] == 150 and rows[0]["is_index"] == 1, rows[0]
    assert rows[1]["is_pending"] == 1 and rows[1]["errors"] is None and rows[1]["submitted"] is None, rows[1]
    assert ci.sitemap_rows({}) == [{"path": ""}], "빈 목록이 표식 줄 없이 사라진다"

    class Svc:
        def __init__(self, resp=None, boom=None):
            self.resp, self.boom = resp, boom

        def sitemaps(self):
            return self

        def list(self, siteUrl):
            assert siteUrl == "sc-domain:x.com"
            return self

        def execute(self):
            if self.boom:
                raise self.boom
            return self.resp

    conn = _mem()
    assert scoring.gsc_sitemaps(conn, 1) is None
    import contextlib
    import io
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        assert ci.collect_sitemaps(conn, 1, Svc(resp), "sc-domain:x.com", "2026-11-13").strip() == "sitemaps=2"
        resp["sitemap"][0]["errors"] = "5"
        ci.collect_sitemaps(conn, 1, Svc(resp), "sc-domain:x.com", "2026-11-20")
        g = scoring.gsc_sitemaps(conn, 1)
        assert (g["date"], g["prev_date"]) == ("2026-11-20", "2026-11-13"), g
        assert g["rows"][0]["path"].endswith("sitemap.xml") and g["rows"][0]["d_errors"] == 3, g["rows"][0]
        assert g["rows"][1]["d_errors"] is None, g["rows"][1]
        # 실패한 날은 적재하지 않는다 — 지난 목록이 산다
        note = ci.collect_sitemaps(conn, 1, Svc(boom=RuntimeError("x")), "sc-domain:x.com", "2026-11-27")
        assert note.startswith("sitemaps=skip:") and scoring.gsc_sitemaps(conn, 1)["date"] == "2026-11-20", note
        # 받았고 0 — '안 받음'과 갈린다
        assert ci.collect_sitemaps(conn, 1, Svc({}), "sc-domain:x.com", "2026-11-28").strip() == "sitemaps=0"
    g = scoring.gsc_sitemaps(conn, 1)
    assert g["date"] == "2026-11-28" and g["rows"] == [], g


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)} passed  ({HOME})")
