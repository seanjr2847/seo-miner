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
  · 화면이 부른 /api/* 가 그 서버에서 실패하지 않았는가 (4xx/5xx — 화면은 대개 삼킨다)
정렬·대비·문구가 읽히는지는 여기서 안 나온다 — 그건 사람이 봐야 한다.

대상은 셋이고 **각자 운영에서 그걸 서빙하는 서버**로 띄운다: 로컬 대시보드와
박제본은 stdlib(dashboard.Handler), 호스팅 조립본은 FastAPI(server.app 의 /d —
serve_hosted). 호스팅을 stdlib 로 띄우면 인증·타입 검증·호스팅 전용 라우트가 전부
빠진 서버를 보고 초록을 낸다(실제로 그랬다).

"있어야 한다"는 검사는 <script> 를 뺀 DOM 에서만 찾는다. 소스까지 뒤지면 렌더러가
만들 수 있는 문자열은 **그 코드가 한 번도 안 돌아도** 통과한다 — 실제로 그랬다.
그래서 픽스처 값은 리포 어디에도 없는 표식을 쓴다(화면 문구와 겹치면 같은 일이 난다).

크롬(또는 엣지)이 없으면 조용히 건너뛴다. node --check 와 같은 규칙이다.
사용자의 진짜 Brain 은 안 건드린다 — 임시 CAPTURE_HOME 에 픽스처를 만든다.
"""
from __future__ import annotations

import gc
import html as htmlmod
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(Path(__file__).resolve().parent))

# 묶음·단계 이름의 정본 — 화면 검사가 이름을 여기 옮겨 적지 않는다.
import run_all as _run_all  # noqa: E402
import stage as _stage  # noqa: E402
_GRP = {g["id"]: g for g in _run_all.GROUPS}
_STAGE_T = {k: v["t"] for k, v in _stage.STAGE_LABELS.items()}

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
#
# fetch 도 감시한다(__NET__). 화면이 부른 /api/* 가 4xx/5xx 로 돌아와도 JS 오류가
# 아니다 — 화면은 대개 그걸 catch 로 삼키고 빈 칸이나 배너로 넘어간다. 그래서 여태
# 호스팅 조립본이 부르는 /api/settings·/api/run/status 가 검사 서버에서 전부 404 였는데
# 아무 검사도 그걸 못 봤다. 여기 적힌 것은 check() 가 실패로 친다.
PROBE = """<script>
window.__ERR__ = []; window.__NET__ = [];
addEventListener("error", function (e) { window.__ERR__.push("onerror: " + (e.message || e)); });
addEventListener("unhandledrejection", function (e) {
  window.__ERR__.push("unhandled: " + ((e.reason && e.reason.message) || e.reason));
});
var _ce = console.error;
console.error = function () {
  window.__ERR__.push("console.error: " + [].slice.call(arguments).join(" "));
  _ce.apply(console, arguments);
};
var _fetch = window.fetch;
window.fetch = function (input, init) {
  var p = _fetch.apply(this, arguments);
  var u = new URL(typeof input === "string" ? input : (input && input.url) || String(input),
                  location.href);
  if (u.origin !== location.origin || u.pathname.indexOf("/api/") !== 0) return p;
  var what = String((init && init.method) || (input && input.method) || "GET").toUpperCase() +
             " " + u.pathname + u.search;
  return p.then(function (r) {
    if (!r.ok) window.__NET__.push(what + " -> " + r.status);
    return r;
  }, function (e) {
    window.__NET__.push(what + " -> 연결 실패: " + ((e && e.message) || e));
    throw e;
  });
};
addEventListener("DOMContentLoaded", function () {
  var d = document.createElement("div");
  d.id = "__probe__"; d.hidden = true;
  document.body.appendChild(d);
  setInterval(function () {
    d.textContent = JSON.stringify({err: window.__ERR__, net: window.__NET__});
  }, 120);
});
</script>"""

# 새로고침해도 보던 화면이 남는지 — 사용자가 [순위]를 직접 연 것처럼 메뉴 버튼을
# 스크립트로 눌러 SM.touched·sessionStorage 를 세우고, 딱 한 번 새로고침한다
# (sessionStorage 표식으로 두 번째 로드에서는 다시 안 누른다 — 안 그러면 무한 새로고침).
# 셸의 본 스크립트(SM.sync() 호출)보다 뒤(</body> 앞)에 심어야 #nav 버튼이 이미 서 있다.
RELOAD_TOUCH = """<script>
if (!sessionStorage.getItem("__reload_touch__")) {
  sessionStorage.setItem("__reload_touch__", "1");
  var b = document.querySelector('#nav button[data-v="rank"]');
  if (b) b.click();
  location.reload();
}
</script>"""


def view_sections() -> list[tuple[str, str]]:
    """각 뷰가 자기 view-def 에 선언한 섹션 id — 그게 DOM 에 실제로 있어야 한다.

    목록을 여기 옮겨 적지 않는다. 선언이 정본이고(test_seams 도 같은 것을
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
BL_TOTAL = 20241                  # 총 백링크. 계기판에만 나오는 수 — 문구와 안 겹친다(참조 도메인보다 커야 화면이 말이 된다)
AI_PROMPT = "AI질문Z9"               # AI 인용: 질문별 목록에만 나오는 문장
TRIAGE_KW = "심사검색어Z9"           # 심사: 미판정 검색어 — 변형 둘이 한 줄로 묶여야 한다
TRIAGE_EN = "english triage termz9"   # 심사: 다른 언어(영어) 미판정 — 처음엔 사이트 언어만 보인다
GROUP_KW = "묶음검색어Z9"            # 기회 묶음: 같은 페이지로 들어오는 AI 요약 기회 셋 — 개요에서 한 줄
AI_VISIT_PAGE = "/aivisitZ9"         # AI 에서 온 방문: GA4 AI 유입 표에만 나오는 경로
AI_VISIT_N = 4321                    # 그 페이지의 세션 — 표에 "4,321" 로 서야 한다
# 구글 AI 요약 — 요약이 떴는데 내 링크가 빠진 검색어와 대신 인용된 도메인. 이 칸은
# [순위 추적]에서 [AI 인용]으로 옮겼다 — 옮긴 자리(#ai-aio)에 서는지를 본다.
AIO_KW = "요약검색어Z9"
AIO_DOM = "aioZ9.example"
# AI 에서 온 방문의 빈 상태 둘 — 로컬 대상에서만 사이트를 바꿔 한 번씩 더 연다(ZERO_SITE 는
# 로컬 Brain 에만 있다). 문구는 <b> 제목만 본다: 소스(<script>)는 check() 가 떼고 보므로
# 렌더러가 그 분기를 실제로 탔을 때만 걸린다.
ZERO_SITE = "gamma-site"             # GA4 연결됨 + 쟀고 0
AI_VISITS_NOGA4 = "GA4를 연결하면 여기서 잽니다"
AI_VISITS_ZERO = "AI에서 온 방문이 아직 없습니다"
# ZERO_SITE 는 GSC 도 두 번 쟀고 두 번 다 클릭 0 이다 — [분석]이 맞댈 시점 유무를 클릭
# 합으로 판정하면 "한 번뿐"이라고 거짓말한다.
AN_NO_CLICKS = "두 기간 모두 클릭이 없습니다"
AN_ONE_RUN = "수집이 한 번뿐이라 맞댈 시점이 없습니다"
# 이번 달 할 일(plays) — [개요] 카드와 오른쪽 서랍. 수정안이 선 것 하나, 못 만든 것(result.error) 하나.
PLAY_TITLE = "할일제목Z9"
PLAY_DRAFT = "초안문단Z9"             # 수정안 탭의 소제목 초안 — 서랍 안에만 나온다
PLAY_ERR = "할일실패Z9"               # 수정안을 못 만든 할 일의 제목
PLAY_ERR_WHY = "모델응답없음Z9"       # 그 이유 — 서랍이 그대로 말해야 한다
PLAY_GAP = "빠진주제Z9"               # 증거 탭의 빠진 주제
EMPTY_LOADS = [
    (SITES[0], [(re.escape(AI_VISITS_NOGA4), "GA4 미연결 사이트에 'AI 에서 온 방문' 빈 상태가 안 섰다"),
                # 할 일이 없는 사이트 — 빈 상태가 만드는 손잡이(data-run="plays")를 든다.
                (r'id="plays"[^>]*><div class="empty"><b>이번 달 할 일이 아직 없습니다</b>'
                 r'(?:(?!class="card).)*<div class="acts"><button[^>]*data-run="plays"',
                 "할 일이 없는 사이트의 [이번 달 할 일] 빈 상태에 만들기 손잡이가 없다"),
                ("!" + re.escape(AI_VISITS_ZERO), "GA4 미연결인데 '쟀고 0' 이라고 말한다")]),
    (ZERO_SITE, [(re.escape(AI_VISITS_ZERO), "쟀고 0 인 사이트에 'AI 방문 없음' 이 안 섰다"),
                 ("!" + re.escape(AI_VISITS_NOGA4), "GA4 가 연결돼 쟀는데 '연결하면' 이라고 말한다"),
                 (re.escape(AN_NO_CLICKS), "두 번 재서 둘 다 클릭 0 인데 [분석]이 그렇다고 안 말한다"),
                 ("!" + re.escape(AN_ONE_RUN), "두 번 쟀는데 [분석]이 '수집이 한 번뿐'이라고 말한다"),
                 # 개요 목록은 비었는데 심사에 쌓였다 — "다 처리했다"가 아니라 [심사하기]
                 (r'id="opps"[^>]*>(?:(?!class="card).)*심사할 검색어가 1건 남았습니다'
                  r'(?:(?!class="card).)*점수 70점 이상이 1건(?:(?!class="card).)*'
                  r"SM\.show\('triage'\)[^>]*>심사하기</button>",
                  "개요 목록이 빈 채 심사에 쌓였는데 빈 상태가 [심사하기]로 안 보낸다"),
                 ("!" + re.escape("쌓인 것을 전부 처리했습니다"),
                  "심사에 쌓여 있는데 개요가 '전부 처리했다'고 말한다"),
                 # 데이터 점검 — 이상이 있으면 접힌 띠가 서고, 펼칠 목록에 등급·내용이 있다
                 (r'<details[^>]*id="ov-health"(?![^>]*hidden)[^>]*>\s*<summary>'
                  r'<span class="ovhl-t">데이터 이상 <b[^>]*>\d+</b>건',
                  "데이터 이상이 있는데 개요에 접힌 띠가 안 섰다"),
                 (r'id="ov-health-list"[^>]*>(?:(?!</ul>).)*class="badge warn">중간<'
                  r"(?:(?!</ul>).)*열리지 않는 페이지\(404\)",
                  "데이터 이상 목록에 등급·내용(404 페이지 점검)이 안 그려졌다")]),
]

# 두 화면이 함께 지켜야 하는 것. 정규식은 "그려졌는가"만 본다 — 예쁜지는 안 본다.
MUSTS = [
    # 경쟁 분석 — 판정이 판매 채널로 가른 곳은 경쟁사 몫 밑에 따로 선다(collect_gap E).
    (r'id="cp-shares"[^>]*>(?:(?!</section>).)*?판매 채널(?:(?!</section>).)*?mall\.example',
     "경쟁사에서 뺀 판매 채널이 경쟁 분석에 안 보인다"),
    # 구글이 본 SEO(사이트 점검) — 항목 이름표는 d.seo_audits(정본 collect_vitals.SEO_AUDITS)로
    # 스크립트가 그린다. 영어 id(link-text)가 그대로 뜨면 이름표가 안 온 것이다.
    (r'id="gseo"[^>]*>(?:(?!</section>).)*?링크 텍스트', "구글이 본 SEO 표가 안 그려졌거나 항목 이름이 한국어가 아니다"),
    (r'!id="gseo"[^>]*>(?:(?!</section>).)*?>link-text<', "구글이 본 SEO 표에 영어 항목 id 가 샜다"),
    # 버튼 이름 = 상태 이름(완료·제외·다시 열기). 상태만 바꾸던 [작업 시작]은 없다 — 열기
    # (로컬)·복사(호스팅)가 그 일을 한다. 옛 이름이 화면 어디에 남아도 두 이름이 된다.
    (r"!<button[^>]*>작업 시작</button>", "상태만 바꾸는 [작업 시작] 버튼이 남아 있다"),
    (r"!>(?:이 기회만 빼기|완료 표시)</button>", "옛 버튼 이름(이 기회만 빼기·완료 표시)이 남아 있다"),
    # 브랜드·경쟁사 칸은 두 배포 공통이다(sections/sm-profile.html). 호스팅 전용 섹션 안에만
    # 있던 동안 로컬 [설정]에는 이 칸이 아예 없었다.
    (r'<section id="sm-profile"(?:(?!</section>).)*id="pf-alias"(?:(?!</section>).)*id="pf-seeds"',
     "[설정]에 브랜드·경쟁사 칸(sm-profile)이 없다 — 두 배포 공통이어야 한다"),
    # 차트는 캔버스다(Chart.js) — 자리(<canvas data-ch>)만 서고 차트가 안 서면 화면은 빈
    # 칸인데 JS 오류도 안 날 수 있다. 셸의 chMake 가 세우면 data-ch-ok 를 단다.
    (r'<canvas data-ch="ch\d+"[^>]*data-ch-ok="1"', "차트가 하나도 안 섰다 — 캔버스 자리만 남았다"),
    ("!" + r'<canvas data-ch="ch\d+"(?![^>]*data-ch-ok)[^>]*>',
     "안 선 차트 자리가 있다 — Chart.js 가 그 캔버스를 못 세웠다"),
    (r"<option[^>]*selected[^>]*>" + SITES[1],
     "hash 가 지목한 사이트가 안 열렸다 — 링크가 실어 보낸 이름이 버려진다"),
    (r'id="content"(?![^>]*hidden)', "본문(#content)이 숨은 채로 남았다 — 데이터를 못 그렸다"),
    # 기준 수집일은 딱 한 자리에서만 말한다(dashboard.html renderDates) — 고를 게
    # 있으면(#gscdate 옵션 2개 이상) 셀렉트가 그 날짜를 이미 말하므로 #meta 는 되풀이
    # 안 한다. 고를 게 없으면(수집 하나뿐) #meta 가 그 정보를 대신 말한다. 그래서
    # 둘 중 어느 쪽이든 날짜가 한 번은 그려졌나를 본다 — #meta 하나로 못 박으면
    # 픽스처가 스냅샷 두 날짜를 심는 순간(셀렉트가 뜨는 순간) 이 검사가 어긋난다.
    (r'id="gscdate"[^>]*>(?:(?!</select>).)*\d{4}-\d{2}-\d{2}(?:(?!</select>).)*</select>'
     r'|id="meta"[^>]*>[^<]*\d{4}-\d{2}-\d{2}',
     "머리말도 날짜 셀렉트도 기준 수집일을 안 적었다"),
    # 새 축은 상자가 서는 것과 그 안이 채워지는 것이 다르다 — 값이 실제로 그려졌나를 본다.
    # (숨은 화면도 DOM 에는 있으므로 --dump-dom 으로 보인다.)
    (re.escape(BL_DOMAIN), "백링크 화면이 링크 교집합을 안 그렸다"),
    (r"20,241", "백링크 화면이 요약 계기판을 안 그렸다"),
    (re.escape(RIVAL), "경쟁 분석 화면이 경쟁사를 안 그렸다"),
    (re.escape(GAP_KW), "경쟁 분석 화면이 키워드 격차를 안 그렸다"),
    ("!쇼핑몰만 잡은 검색어", "판매 채널로 판정된 곳(mall.example)의 격차를 경쟁 격차로 그렸다"),
    (re.escape(CRAWL_NEW), "사이트 점검이 크롤 이슈를 안 그렸다"),
    (r"새로 생김", "크롤 회차 비교가 신규 이슈를 표시 안 했다 — 이 축의 전부가 그것이다"),
    # 속도는 표가 서는 것과 **판정이 맞는 것**이 다르다 — 픽스처는 모바일만
    # 기준을 넘긴다. "1개"가 아니라 "2개"면 데스크톱까지 느리다고 말한 것이다.
    (r"<b>1개</b>가 기준을 넘겼습니다", "속도 섹션이 기준 초과를 잘못 셌다"),
    (r"이 페이지의 실제 사용자", "속도 표가 현장·실험실 출처를 안 밝혔다"),
    (r"해결된 것 <b>0</b>건|새로 생긴 것 <b>1</b>건", "크롤 회차 비교 요약이 안 나왔다"),
    (re.escape(AI_PROMPT), "AI 인용 화면이 질문 목록을 안 그렸다"),
    # 엔진 이름 옆의 로고와, 엔진×카테고리 줄에서 질문 목록으로 내려가는 손잡이.
    # "chatgpt 브랜드 4 3 10 40%" 를 눌러도 아무 일도 안 나던 자리다.
    (r'<svg class="ailogo', "AI 인용 화면이 엔진 로고를 안 그렸다"),
    (r'<tr class="aimx"[^>]*data-e="chatgpt"[^>]*data-c="브랜드"[^>]*onclick="AI_drill',
     "엔진×카테고리 줄이 질문 목록으로 안 내려간다"),
    # 심사 — 변형 둘이 한 줄(+1)로 묶여 그려지고, 상단 카운트가 미판정 1을 센다.
    (r'<tr class="trrow[^"]*"[^>]*data-key="[^"]+"[^>]*>(?:(?!</tr>).)*' + re.escape(TRIAGE_KW)
     + r'(?:(?!</tr>).)*\(\+1\)',
     "심사 화면이 검색어를 한 줄로 묶어 안 그렸다(변형 +1)"),
    # 카운트는 거르기와 무관한 전체다(요약이 거짓말하면 안 된다) — 영어 줄까지 2.
    (r'id="tr-counts"[^>]*>(?:(?!</p>).)*미판정 <b>2</b>', "심사 화면 상단 카운트가 안 나왔다"),
    # 다국어 — 섞여 있으면 처음엔 사이트 언어(한국어)만 보이고, 언어 거르개가 영어 줄 수를
    # 말하며, 가린 줄이 있다고 부제가 말한다(안 말하면 영어 줄이 없는 것으로 읽힌다).
    (r'<select[^>]*aria-label="언어로 거르기"(?:(?!</select>).)*<option value="en"[^>]*>영어 \(1\)</option>',
     "심사 화면에 언어 거르개(영어 1줄)가 없다 — 다국어 검색어가 한 줄로 섞인다"),
    ("!" + TRIAGE_EN, "심사 화면이 처음부터 다른 언어 줄을 사이트 언어 줄과 섞어 그렸다"),
    (r'id="tr-qsub"[^>]*>(?:(?!</p>).)*한국어만 봅니다\(다른 언어 1줄', "언어로 가린 줄 수를 부제가 안 말한다"),
    # 기회 묶음 — 같은 페이지의 AI 요약 기회 셋이 개요에서 카드 **하나**(id 셋)로 서고,
    # 접힌 줄에 변형이, 펼침 패널에 묶은 이유가 그려진다.
    # 울타리가 </details> 가 아니라 다음 카드인 이유: 패널 안에 요청문 상자(<details
    # class="ask">)가 있어서, </details> 로 막으면 그 상자가 카드를 반으로 자른다.
    # 표식을 글자가 아니라 태그로 잡는 것도 같은 이유다 — 요청문 본문(textarea)에도
    # 같은 글자가 들어 있고, 그건 이 검사가 보려는 마크업이 아니다.
    (r'<details class="opp" data-opp="\d+" data-ids="\d+ \d+ \d+"[^>]*>'
     r'(?:(?!<details class="opp").)*<span class="badge"[^>]*>검색어 3개</span>'
     r'(?:(?!<details class="opp").)*' + re.escape(GROUP_KW) + r' 비교'
     r'(?:(?!<details class="opp").)*<b>묶인 검색어 3개</b>',
     "개요가 같은 지면의 기회 셋을 한 줄로 안 그렸다(묶음 배지·변형·묶은 이유)"),
    # 펼침 패널의 행동 줄은 **맨 위**다: 요청문 만들기 → 나머지 상태 버튼 → 화면이
    # 넘긴 손잡이(oppActs·다른 화면으로)가 한 줄에 서고, 근거·할 일은 그 아래다.
    # 예전엔 셋이 패널 맨 밑에 세 줄로 흩어져 있었다 — 할 일 목록이 길수록 누를 것이
    # 멀어져, 다 읽고 스크롤을 더 내려야 손이 닿았다.
    # 줄 안에 지금 상태 칩도 있어야 한다 — 버튼은 "무엇으로 바꾸나"만 말해서
    # ('완료'), 칩이 없으면 지금이 어디인지 아무도 안 말한다. [개요]는 기본
    # 상태('할 일')일 때 접힌 줄의 배지도 일부러 안 달기 때문에 특히 그렇다.
    # (요청문 상자는 호스팅에만 선다 — 그 검사는 ASK_HOSTED_MUSTS·ASK_LOCAL_MUSTS 가 갈라 본다.)
    (r'<div class="det"><div class="det-top">'
     r'(?:(?!<div class="det").)*<span class="st st-\w+"[^>]*>[^<]+</span>'
     r'(?:(?!<div class="det").)*<div class="acts oppset">'
     r'(?:(?!<div class="det").)*<b>묶인 검색어',
     "펼침 패널의 행동 줄(지금 상태·상태 바꾸기)이 할 일보다 위에 안 섰다"),
    # 펼침 패널의 수 — 서버가 센 것(target_trend)의 마지막 회차가 그대로 선다.
    # 노출 120 · 클릭 8 · CTR 6.7% · 9위(픽스처의 06-01). 상자가 서는 것과 **맞는
    # 수가 서는 것**이 다르다: 화면이 표를 다시 더하면 여기서 다른 수가 나온다.
    (r'class="detk"(?:(?!</p>).)*>120<(?:(?!</p>).)*>8<(?:(?!</p>).)*>6\.7%<'
     r'(?:(?!</p>).)*>9</span>위',
     "펼침 패널이 대상의 노출·클릭·CTR·순위를 안 그렸다"),
    # 순위 추이 — 두 회차(14위 → 9위)가 있으니 선이 선다. 캔버스만 보면 빈 차트도
    # 통과하므로, 축을 뒤집었다고 말하는 안내 줄과 같이 본다.
    (r'순위 추이 — 위로 갈수록 좋은 순위입니다\.</p>\s*<div class="chwrap"[^>]*>\s*<canvas data-ch=',
     "펼침 패널이 순위 추이 차트를 안 세웠다"),
    # 진단은 한 줄이고 표는 접혀 있다 — 한 줄에 tag 가 서고, 표는 그 details 안이다.
    # 일곱 줄(같은 tag 둘 포함)이 '손댈 곳 7군데 — title, meta description, H1, 본문
    # 외 2종' 한 줄이 된다. 수는 줄 수(7)고 이름은 **중복 없는** tag 다 — 둘을 같이
    # 보는 이유: 한쪽만 보면 tag 를 세는 실수(6군데)가 그대로 지나간다.
    (r'<details class="fixmore"><summary>손댈 곳 7군데 — '
     r'title, meta description, H1, 본문 외 2종</summary>\s*'
     # tableFade() 가 그린 뒤 넘침 표식(tw over/end)을 덧붙인다 — 클래스를 통째로
     # 못 박으면 그 표식이 붙는 순간 이 검사가 어긋난다.
     r'<div class="tw[^"]*"><table class="dett fix"',
     "페이지 진단이 한 줄 요약 + 접힘으로 안 나왔다"),
    # 연관 페이지·연관 키워드. 키워드는 **기회에 안 걸린 검색어까지** 세야 한다 —
    # 픽스처의 그 지면에는 둘이 들어오는데 기회는 하나뿐이다. '1' 이 나오면 화면이
    # 기회 표(query_pages)를 뒤집어 센 것이다.
    (r'<b>연관 페이지 1</b>', "펼침 패널이 연관 페이지를 안 그렸다"),
    (r'<b>연관 키워드 2</b>(?:(?!</div>).)*곁 검색어',
     "연관 키워드가 그 지면으로 들어오는 검색어 전부를 안 셌다"),
    # coverage 기회의 대상은 내부 꼴('cluster:이름')이 아니라 사람이 읽는 이름으로
    # 그려져야 한다 — 카드 제목이 'cluster:(미분류)' 그대로 보이던 것이 실제 발견이었다.
    # 미분류는 이제 기회가 아니다(scoring._coverage_rows) — 이름 있는 묶음으로 본다.
    (r'<div class="target">주제 묶음 · 피부 관리</div>', "coverage 기회의 대상이 사람이 읽는 이름으로 안 보인다"),
    ("!cluster:피부 관리", "기회 대상이 내부 식별자(cluster:) 그대로 화면에 보인다"),
    # 완료 후 관찰 — 그때(14위)와 지금(9위)이 한 줄에 나란히 선다.
    # 리디자인으로 표(<td>)가 행 카드(.ovw-r)가 됐다 — 한 행 카드 안에 그때·지금 칸이 같이 선다.
    (r'id="watch"[^>]*>(?:(?!</section>).)*class="ovw-r"(?:(?!class="ovw-r").)*그때 <span class="nw">14위 · 클릭'
     r'(?:(?!class="ovw-r").)*지금 <span class="nw">9위 · 클릭', "완료 후 관찰이 전·후를 안 그렸다"),
    # AI 에서 온 방문 — 페이지 줄에 그 페이지의 세션이 선다(섹션 상자가 서는 것과 다르다).
    (r'id="ai-visits"(?:(?!</section>).)*' + re.escape(AI_VISIT_PAGE)
     + r'(?:(?!</tr>).)*>' + f"{AI_VISIT_N:,}" + "<",
     "AI 인용 화면이 'AI 에서 온 방문' 을 안 그렸다"),
    # 구글 AI 요약은 [AI 인용] 화면의 몫이다 — 빠진 검색어 줄에 대신 인용된 도메인이 선다.
    # 순위 화면에는 AI 요약 칸이 없어야 한다(옮겼다 — 두 자리에 있으면 두 벌이다).
    (r'id="ai-aio"(?:(?!</section>).)*' + re.escape(AIO_KW)
     + r'(?:(?!</tr>).)*' + re.escape(AIO_DOM),
     "AI 인용 화면이 구글 AI 요약에 빠진 검색어를 안 그렸다"),
    # 표 머리글로 본다 — 기회 줄의 종류 이름표("구글 AI 요약 빠짐")는 순위 화면에 서도 된다.
    (r'!id="view-rank"(?:(?!id="view-).)*<th[^>]*>(?:<[^>]+>)*AI 요약',
     "순위 화면에 AI 요약 칸이 남아 있다 — [AI 인용]으로 옮긴 것이 두 벌이 됐다"),
    # 메뉴 = 묶음. 이름은 페이로드(d.groups ← run_all.GROUPS)에서 온다. 주기 점은 배포마다
    # 출처가 같은 한 벌(셸의 grpDue — 상태가 이긴다)이라 로컬·호스팅 검사가 따로 있다
    # (LOCAL_MUSTS · HOSTED_MUSTS). 다시 잴 단계가 없는 [할 일]에는 점이 안 선다.
    (r'class="rl navgrp">할 일</span>', "버튼 없는 [할 일]에 주기 점이 섰다 — 눌러서 할 것이 없다"),
    # [할 일]도 매일 런이 언제 돌았는지는 말한다(버튼 없이) — 심사·개요 머리줄.
    (r'id="view-triage"(?:(?!id="view-).)*class="vgrp"(?:(?!</div>).)*마지막으로 잰 날',
     "[심사] 머리줄이 매일 런의 마지막으로 잰 날을 안 그렸다"),
    # 묶음 화면 머리줄 — 단계별 마지막으로 잰 날. 방금 잰 AI 인용은 날짜가, 안 잰 순위
    # 조회는 "아직"이 선다(둘 다 한 줄 안).
    (r'id="view-ai"(?:(?!id="view-).)*class="vgrp"(?:(?!</div>).)*마지막으로 잰 날'
     r'(?:(?!</div>).)*<em>\d{4}-\d{2}-\d{2}</em>(?:(?!</div>).)*<em>아직</em>',
     "[AI 인용] 머리줄이 단계별 마지막으로 잰 날을 안 그렸다"),
] + view_sections()
# 박제본(--export)은 배포되는 산출물이다 — 메일로 나가고 저장돼서 열린다. 라이브
# 화면과 조건이 다르다: 서버가 없고, 손댈 수 없고, 인쇄된다. 그래서 따로 본다.
# 화면 목록은 여기 옮겨 적지 않는다 — view-def 에서 읽되 박제본이 빼는 둘만 뺀다.
# 요청문 상자(요청문 만들기·요청문 복사)는 **호스팅에만** 선다. 로컬은 기회마다 "도구로
# 열기"(runTool)가 요청문을 파일로 써서 도구를 바로 띄우므로, 복사해 붙여 넣는 길을 또
# 두면 같은 행동이 두 벌이다. 호스팅은 브라우저라 도구를 못 띄워 복사가 유일한 길이다.
ASK_HOSTED_MUSTS = [
    # 요청문 복사 버튼은 **펴기 전에** 보인다 — 손잡이 안이 아니라 상자 바로 뒤의
    # 독립 버튼이다(손잡이 안에 두면 막대 전체가 한 버튼으로 읽혀 패널이 열렸다).
    (r'<details class="ask"><summary>요청문 만들기(?:(?!</summary>).)*</summary>'
     r'(?:(?!</details>).)*</details><button[^>]*class="[^"]*askcopy',
     "요청문 복사 버튼이 요청문 상자 바로 뒤에 없다 — 펴야만 보이거나 손잡이 안에 있다"),
    (r'!<summary>요청문 만들기(?:(?!</summary>).)*askcopy',
     "요청문 복사 버튼이 손잡이(summary) 안에 남아 있다 — 누르면 패널이 열린다"),
    (r'!<div class="pb-h">(?:(?!</div>).)*copyPrompt',
     "복사 버튼이 패널 안에도 남아 있다 — 두 벌이다"),
    # 할 일인 기회의 [요청문 복사]는 누르면 진행 중으로 옮긴다(data-ack 에 그 id)
    (r'class="[^"]*askcopy"[^>]*data-ack="\d', "호스팅의 할 일 기회 [요청문 복사]가 진행 중으로 안 옮긴다"),
    # 패널 맨 위(행동 줄의 첫 자리)가 요청문이다
    (r'<div class="det"><div class="det-top"><details class="ask">',
     "호스팅 펼침 패널의 행동 줄 맨 앞에 요청문 상자가 없다"),
]
ASK_LOCAL_MUSTS = [
    (r'!<details class="ask">', "로컬에 요청문 상자(요청문 만들기)가 섰다 — 호스팅 전용이다"),
    (r'!class="[^"]*askcopy', "로컬에 요청문 복사 버튼이 섰다 — 호스팅 전용이다"),
]


# [이번 달 할 일] 서랍 — 서랍은 누를 때 생긴다(OV_drawerEls). 그래서 화면이 선 뒤 줄을 실제로
# 눌러 본다: ① 수정안을 못 만든 할 일을 열어 그 서랍을 떠 두고 ② 기회 줄을 눌러 같은 서랍이
# 기회로 바뀌는지(서랍은 한 번에 하나) 적고 ③ 수정안이 선 할 일을 연 채로 DOM 이 떨어진다.
# 사람이 누르는 길과 같은 onclick 을 탄다 — 함수를 직접 부르지 않는다.
PLAY_PROBE = """<script>addEventListener("load", function () {
  var n = 0, t = setInterval(function () {
    var rows = document.querySelectorAll('#plays button.ovp');
    if (rows.length < 2) { if (++n > 80) clearInterval(t); return; }
    clearInterval(t);
    var dr = function () { return document.getElementById("ov-drawer"); };
    var out = document.createElement("div"); out.id = "__plays__"; out.hidden = true;
    document.body.appendChild(out);
    rows[1].click();
    out.insertAdjacentHTML("beforeend", '<div id="__play_err__">' + dr().innerHTML + '</div>');
    var s = document.querySelector('#opplist details.opp > summary');
    if (s) s.click();
    out.insertAdjacentHTML("beforeend", '<p id="__play_opp__">marks=' +
      document.querySelectorAll('#plays .ovp.odr-on').length + ' drawers=' +
      document.querySelectorAll('aside.odr').length + ' fix=' + !!dr().querySelector('[data-panel="fix"]') +
      ' ev=' + !!dr().querySelector('[data-panel="ev"]') + '</p>');
    document.querySelectorAll('#plays button.ovp')[0].click();
  }, 100);
});</script>"""
PLAY_MUSTS = [
    # 카드 — 점수 순 첫 줄이 수정안이 선 할 일이고, 줄은 누르는 버튼(data-play = 페이로드 id)이다.
    (r'id="plays"[^>]*><ol class="ovp-list"><li><button type="button" class="ovp[^"]*" data-play="\d+"'
     r'(?:(?!</button>).)*' + re.escape(PLAY_TITLE),
     "[이번 달 할 일] 카드 첫 줄에 할 일 제목이 없다 — d.plays 를 안 그렸다"),
    (r'id="plays"(?:(?!</li>).)*<span class="ovp-u">/bagsZ9</span>',
     "[이번 달 할 일] 줄에 고칠 페이지 경로가 없다"),
    (r'id="plays"(?:(?!</li>).)*<span class="ovp-k"[^>]*>외 1</span>',
     "[이번 달 할 일] 줄의 검색어 칩이 셋에서 안 접혔다(외 N)"),
    # ③ 수정안이 선 할 일을 연 서랍 — 탭 셋, 초안, 증거의 우리 줄, 문서, 발판의 복사.
    (r'<aside class="odr" id="ov-drawer"(?![^>]*\shidden)[^>]*>(?:(?!</aside>).)*id="odr-t">'
     + re.escape(PLAY_TITLE),
     "할 일 줄을 눌렀는데 오른쪽 서랍이 그 할 일로 안 열렸다"),
    (r'id="ov-drawer"(?:(?!</aside>).)*data-t="fix"[^>]*>수정안</button>'
     r'(?:(?!</aside>).)*data-t="ev"[^>]*>증거</button>(?:(?!</aside>).)*data-t="doc"[^>]*>문서</button>',
     "할 일 서랍에 수정안·증거·문서 탭이 없다"),
    (r'id="ov-drawer"(?:(?!</aside>).)*data-panel="fix"(?:(?!</aside>).)*' + re.escape(PLAY_DRAFT),
     "할 일 서랍의 수정안 탭이 소제목 초안을 안 그렸다"),
    (r'id="ov-drawer"(?:(?!</aside>).)*data-panel="ev"(?:(?!</aside>).)*<li class="own">'
     r'(?:(?!</aside>).)*' + re.escape(PLAY_GAP),
     "할 일 서랍의 증거 탭이 상위 결과(우리 줄)나 빠진 주제를 안 그렸다"),
    (r'id="ov-drawer"(?:(?!</aside>).)*data-panel="doc"(?:(?!</aside>).)*<textarea readonly[^>]*>'
     r'(?:(?!</aside>).)*\.md 내려받기',
     "할 일 서랍의 문서 탭에 문서 전문·내려받기가 없다"),
    (r'id="ov-drawer"(?:(?!</aside>).)*<div class="odr-f">(?:(?!</aside>).)*>문서 복사</button>',
     "할 일 서랍 발판에 [문서 복사]가 없다"),
    # ① 수정안을 못 만든 할 일 — 수정안 탭 없이 이유를 말하고 증거를 보인다.
    (r'id="__play_err__">(?:(?!id="__play_opp__").)*id="odr-t">' + re.escape(PLAY_ERR)
     + r'(?:(?!id="__play_opp__").)*수정안을 못 만들었습니다(?:(?!id="__play_opp__").)*' + re.escape(PLAY_ERR_WHY),
     "수정안을 못 만든 할 일의 서랍이 그 이유를 안 말한다"),
    (r'!id="__play_err__">(?:(?!id="__play_opp__").)*data-t="fix"',
     "수정안을 못 만든 할 일의 서랍에 빈 수정안 탭이 섰다"),
    # ② 기회 줄을 누르면 같은 서랍 하나가 기회로 바뀐다 — 할 일 강조가 풀리고 서랍이 둘이 안 된다.
    (r'id="__play_opp__">marks=0 drawers=1 fix=false ev=true<',
     "할 일 서랍이 열린 채 기회를 누르면 서랍이 둘이 되거나 할 일이 남는다"),
]
PLAY_LOCAL_MUSTS = [
    (r'id="plays-tools"[^>]*><button class="cmd sec" data-run="plays"[^>]*>/capture plays ' + re.escape(SITES[1]),
     "로컬 [이번 달 할 일] 머리에 다시 만들기 명령 칩(/capture plays 사이트)이 없다"),
]
PLAY_HOSTED_MUSTS = [
    # 호스팅은 실행 버튼이다 — onclick 없이 data-run 만 달아 애드온의 위임(/api/run)이 받는다.
    (r'id="plays-tools"[^>]*><button class="go sec" data-run="plays"(?![^>]*onclick)',
     "호스팅 [이번 달 할 일] 머리에 다시 만들기 실행 버튼(data-run=plays)이 없다"),
]
PLAY_REPORT_MUSTS = [
    (r'id="plays"[^>]*><ol class="ovp-list"><li><div class="ovp">(?:(?!</li>).)*' + re.escape(PLAY_TITLE),
     "박제본 [이번 달 할 일]이 할 일을 문장으로 안 남겼다"),
    (r"!data-play=", "박제본 [이번 달 할 일] 줄이 누르는 버튼이다 — 열 서랍이 없다"),
    (r'!id="plays-tools"[^>]*><button', "박제본 [이번 달 할 일]에 다시 만들기 손잡이가 남았다"),
]


REPORT_DROPPED = ("settings", "guide", "triage")   # 심사는 서버가 있어야 저장된다


def view_defs_ids() -> list[str]:
    """뷰 선언에 있는 화면 id 전부 — 목록을 여기 옮겨 적지 않는다."""
    out = []
    for f in sorted((ROOT / "skills" / "capture" / "templates" / "views").glob("*.html")):
        m = re.search(r'class="view-def">\s*(\{.*?\})\s*</script>', f.read_text("utf-8"), re.S)
        assert m, f"{f.name} 에 view-def 선언이 없다"
        out.append(json.loads(m.group(1))["id"])
    return out
REPORT_MUSTS = [
    # 차트는 캔버스다(Chart.js) — 자리(<canvas data-ch>)만 서고 차트가 안 서면 화면은 빈
    # 칸인데 JS 오류도 안 날 수 있다. 셸의 chMake 가 세우면 data-ch-ok 를 단다.
    (r'<canvas data-ch="ch\d+"[^>]*data-ch-ok="1"', "차트가 하나도 안 섰다 — 캔버스 자리만 남았다"),
    ("!" + r'<canvas data-ch="ch\d+"(?![^>]*data-ch-ok)[^>]*>',
     "안 선 차트 자리가 있다 — Chart.js 가 그 캔버스를 못 세웠다"),
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
    # 묶음 머리줄의 날짜는 기록이라 남고, 다시 재기 버튼은 누를 서버가 없어 빠진다.
    (r'class="vgrp"(?:(?!</div>).)*마지막으로 잰 날', "박제본에 묶음별 마지막으로 잰 날이 없다"),
    (r"!data-grp=", "박제본에 [이 묶음 다시 재기]가 남았다 — 누를 서버가 없다"),
    # 값이 실제로 그려졌는가 — 라이브와 같은 표식을 쓴다
    (re.escape(BL_DOMAIN), "박제본이 링크 교집합을 안 그렸다"),
    (re.escape(RIVAL), "박제본이 경쟁사를 안 그렸다"),
    (re.escape(CRAWL_NEW), "박제본이 크롤 이슈를 안 그렸다"),
]

# 로컬에만 있는 것 — 기회 카드의 실행 자리(SM.host.oppBtn). 이 픽스처는 도구를 안
# 고른 상태(SEOMINER_TOOL 미설정)라 버튼 대신 "도구 없음" 배지가 서야 한다.
# 호스팅은 같은 자리에 다른 것(아래 HOSTED_MUSTS)이 서므로 MUSTS 에 넣지 않는다.
LOCAL_MUSTS = [
    (r'<span class="badge warn">도구 없음</span>',
     "도구를 안 고른 상태의 기회 카드에 '도구 없음' 배지가 없다 — 실행 자리가 비었다"),
    # 로컬의 주기 점 — 픽스처는 AI 인용만 방금 쟀고(runs 'ai') 나머지 묶음은 한 번도 안
    # 쟀다. 그래서 [AI 노출]에는 점이 없고 [검색 성과]에는 있어야 한다. 점을 늘 달거나
    # 늘 안 다는 렌더러는 둘 중 하나에서 걸린다.
    (r'class="rl navgrp">검색 성과<i class="navdue"', "주기를 넘긴 묶음([검색 성과])에 점이 없다"),
    (r'class="rl navgrp">AI 노출</span>', "방금 잰 묶음([AI 노출])에 점이 섰거나 묶음 이름이 없다"),
] + [
    # 두 배포 공통이지만 박제본에는 없는 것 — 재기 버튼. 묶음 버튼은 잴 단계가 있는 묶음
    # 화면에, 전체 재기는 [안내] 한 곳에만 선다(레일 바닥의 [전체 분석 실행]은 없앴다).
    (r'id="view-rank"(?:(?!id="view-).)*<button[^>]*data-grp="search"',
     "[순위 추적]에 [이 묶음 다시 재기](검색 성과)가 없다"),
    (r'id="view-guide"(?:(?!id="view-).)*<button[^>]*data-grp=""',
     "[안내]에 전체 다시 재기가 없다"),
    # 묶음 이름은 머리줄 맨 앞에 선다 — 레일의 묶음 제목은 좁은 화면에서 안 그려져,
    # 모바일에서 "이 묶음"이 무엇인지 말하는 자리가 여기뿐이다. 버튼 이름표는 다시 재는
    # 범위(g.stages 의 단계 이름)까지 말한다. 이름은 정본(run_all·stage)에서 읽는다.
    (r'id="view-rank"(?:(?!id="view-).)*class="vgrp"[^>]*><b class="gname">'
     + re.escape(_GRP["search"]["name"]) + "</b>",
     "[순위 추적] 머리줄에 묶음 이름(검색 성과)이 없다 — 모바일에서 '이 묶음'이 무엇인지 모른다"),
    (r'<button[^>]*data-grp="search"[^>]*aria-label="[^"]*'
     + re.escape(_GRP["search"]["name"]) + ": "
     + re.escape(" · ".join(_STAGE_T[s] for s in _GRP["search"]["stages"])) + '"',
     "[이 묶음 다시 재기]의 이름표가 다시 재는 단계를 안 말한다"),
    # '다시 ~' 버튼은 화면당 하나다(2026-09-27 제품 결정) — 단계별 다시 재기 칩은 전부
    # 접힘 메뉴(.vh-menu) 안이다. 메뉴 밖, 제목 뒤에 칩이 서면 한 화면에 '다시'가 여럿이다.
    (r'<details class="vh-more"><summary>단계별로 다시</summary><div class="vh-menu"><button[^>]*vh-cmd',
     "단계별 다시 재기가 접힘 메뉴 안에 없다"),
    (r'!</h1>(?:(?!vh-menu)(?!id="view-).)*class="[^"]*\bvh-cmd\b',
     "단계별 다시 재기 칩이 접힘 메뉴 밖에 섰다 — 한 화면에 '다시' 버튼이 여럿이다"),
    # 그 버튼 바로 밑 산문이 "비용이 드는 단계는 돌리기 전에 물어봅니다"라고만 하면 거짓말이다 —
    # 버튼은 두 배포 다 묻지 않고 바로 돈다. 버튼 쪽 사실을 말해야 한다.
    (r'id="view-guide"(?:(?!id="view-).)*다시 재기는 누르면 바로 실행됩니다',
     "[안내]가 다시 재기 버튼이 비용을 묻지 않고 바로 돈다는 것을 안 말한다"),
    (r'!id="view-overview"(?:(?!id="view-).)*data-grp=',
     "버튼이 없어야 할 [할 일] 묶음 화면에 다시 재기가 섰다"),
]
RUN_MUSTS = LOCAL_MUSTS[3:]

# 셸 table() 의 행 카드 옵트인(.tw.cards) — 좁은 화면에서 칸 앞에 서는 라벨(data-label)은
# 머리글 글자에서 온다. 뷰가 아직 안 붙였을 수 있어 화면 대신 셸의 table() 을 **그대로**
# 불러 본다(사본 없음). 펼침 줄(tr.exp)의 통짜 칸은 라벨을 달면 안 된다.
CARDS_PROBE = """<script>addEventListener("load", function () {
  var d = document.createElement("div"); d.id = "__cards__";
  d.innerHTML = table(["페이지", ">순위?구글이 보고한 평균 게재순위"],
    ['<tr><td>/a</td><td class="num">3</td></tr>', '<tr class="exp"><td colspan="2">펼침</td></tr>'],
    "", "cards");
  document.body.appendChild(d);
});</script>"""
SHELL_LOCAL_MUSTS = [
    (r'id="__cards__"><div class="tw cards[^"]*"><table>.*?<td>/a</td><td class="num" data-label="순위">3</td>',
     "table(..., \"cards\") 가 칸 라벨을 머리글에서 안 달았다 — 좁은 화면 행 카드에 열 이름이 없다"),
    (r'!id="__cards__">(?:(?!</table>).)*colspan="2" data-label',
     "행 카드가 펼침 줄의 통짜 칸에 라벨을 달았다"),
    # 로컬에는 사이트 목록 화면이 없다 — 로고는 글자 그대로다(링크는 호스팅 몫).
    (r'!<a class="mark"', "로컬 레일 로고가 링크가 됐다 — 갈 목록 화면이 없다"),
]           # 앞 셋(도구 없음 배지·로컬 주기 점)은 로컬 몫

# 호스팅 입구 화면 — 대시보드(/d) 밖에서 서버가 따로 내보내는 것. 로그인 전 랜딩(/)과
# 온보딩(/start). 온보딩의 고르개(종류·언어-지역)는 서버가 실은 목록으로 **스크립트가**
# 채운다 — 스크립트가 멈추면 빈 <select> 만 서 있어 주소를 적어도 다음으로 못 간다.
ENTRY_PAGES = [
    ("/", [(r'href="/auth/login"', "랜딩에 로그인 손잡이가 없다")]),
    ("/start", [
        (r'id="s1"(?![^>]*hidden)', "온보딩 첫 걸음(사이트 주소)이 안 섰다"),
        (r'id="s2"[^>]*hidden', "온보딩이 첫 걸음을 건너뛰고 둘째 걸음을 세웠다"),
        (r'<select id="type"><option value="saas"', "사이트 종류 고르개가 비었다 — 스크립트가 멈췄다"),
        (r'<select id="locale"><option value="ko-KR"', "언어-지역 고르개가 비었다 — 스크립트가 멈췄다"),
        (r'id="r1" class="here"', "진행 레일이 첫 걸음을 가리키지 않는다"),
        # 연결 안 한 사람에게 "서치콘솔 사이트에서 고르기"를 내밀면 누르자마자 오류다.
        (r'id="alt"[^>]*hidden', "서치콘솔을 연결 안 했는데 '서치콘솔에서 고르기'가 보인다"),
    ]),
]

# 호스팅 애드온이 런타임에 만드는 것 — 하나라도 없으면 조립이 조용히 멈춘 것이다.
# "!" 로 시작하면 반대다: 그 패턴이 **없어야** 통과한다.
HOSTED_MUSTS = MUSTS + RUN_MUSTS + [
    # 설정의 사이트 종류 — /api/settings 의 types 로 스크립트가 그린다. 등록 뒤 종류를
    # 바꾸는 유일한 자리라, 안 그려지면 잘못 넣은 종류(saas 로 들어간 쇼핑몰)에 영영 묶인다.
    (r'<select data-ptype[^>]*>(?:<option[^>]*>[^<]*</option>){2,}', "설정에 사이트 종류 고르개가 안 그려졌다"),
    # 배포마다 다른 문구는 화면이 data-web / W() 로 직접 고른다(dashboard.html).
    # 하나라도 남아 있으면 표식(SM_HOSTED)이 첫 렌더보다 늦게 선 것이다 — 응답이
    # 빠를수록 잘 지는 경합이라, 눈으로 보면 멀쩡한데 실서비스에서만 틀린다.
    (r"!data-web=", "호스팅인데 대체 문구가 안 입혀졌다 — SM_HOSTED 가 첫 렌더보다 늦게 섰다"),
    (r'!id="sm-fail"', "조립 실패 배너가 떴다 — 애드온 초기화가 터졌다(fail() 이 만든다)"),
    # 애드온이 서버 응답을 못 받았을 때 하는 단 한 가지 말(dash.html 의 OFFLINE).
    # stdlib 로 띄우던 시절엔 호스팅 전용 라우트가 없어 폴링이 세 번 404 를 먹고 이
    # 배너가 늘 떠 있었다 — 실제 앱으로 띄우는 지금은 서 있으면 그게 고장이다.
    (r"!서버가 응답하지 않았습니다", "'서버가 응답하지 않았습니다' 배너가 떴다 — 애드온이 "
                                 "부르는 라우트가 호스팅 앱에 없거나 실패했다"),
    # 메뉴 첫 자식은 버튼이 아닐 수 있다 — 레일이 묶음 이름(.navgrp)을 먼저 세운다.
    # 보는 것은 "메뉴 안에 화면 버튼이 있나" 하나다.
    # `[\s\S]{0,200}?` 로는 안 된다: 메뉴가 비면 </nav> 를 넘어 레일 바닥의
    # [새로고침] 버튼을 잡아 통과해 버린다(실제로 그렇게 통과했다).
    (r'id="nav"[^>]*>(?:(?!</nav>)[\s\S])*?<button',
     "레일 메뉴가 비었다 — 셸이 화면 목록을 못 세웠다"),
    (r'id="view-overview"', "화면 상자(개요)가 안 만들어졌다"),
    (r'<a class="mark" href="/"', "호스팅 레일 로고가 사이트 목록(/)으로 가는 링크가 아니다"),
    (r'id="view-backlinks"', "호스팅 전용 화면(백링크)이 안 붙었다"),
    (r'id="sm-set"', "호스팅 설정 섹션이 안 만들어졌다"),
    # 호스팅은 서버가 키를 댄다. 안내가 요청마다 오는 guide.steps[].gain(로컬 갈래)을
    # 그리면 "OpenRouter 키를 넣으면 켜집니다"가 샌다 — 조립 시점 표(gainOf)를 써야 한다.
    (r"!키를 넣으면", "호스팅 안내가 유료 키를 넣으라고 한다 — 조립 시점 용어표를 안 읽었다"),
    # 전체 재기는 [안내] 화면의 묶음 머리줄로 옮겼다 — 레일 바닥에 다시 붙으면 두 벌이다.
    (r'!id="sm-run"', "레일 바닥에 [전체 분석 실행]이 다시 붙었다 — 전체 재기는 [안내] 한 곳이다"),
    # 안내의 실행 칩은 SM.host 를 렌더 시점에 부른다(dashboard.html 의 renderGuide) —
    # 호스팅판은 그 훅을 실행 버튼으로 갈아 낀다(dash.html 의 SM.host.stepChip).
    # 이 픽스처(beta-site)는 GSC 는 읽었지만 키워드를 아직 안 캤으므로 "지금 할 것"이
    # runnable 인 키워드 단계다 — 그 칩(class="cmd", 화면 제목 옆 칩과는 클래스가
    # 다르다)이 실제로 host.run 을 부르는지를 본다. 로컬 기본값(SM.host.copy)이
    # 남아 있으면(=SM.host 가 늦게 섰거나 안 갈렸으면) 이 패턴이 없다.
    (r'class="cmd[^"]*" data-stage="[^"]+" onclick="SM\.host\.run\(',
     "안내의 실행 칩이 host.run 을 안 부른다 — 복사 칩인 채로 남았다"),
    # 기회 카드의 실행 자리 — 브라우저는 이 PC 의 프로세스를 못 띄우므로, 여기 설
    # 것은 버튼이 아니라 "어디서 누르면 되는지"다(dash.html 의 SM.host.oppBtn).
    (r"이 PC 에서 열기", "호스팅 기회 카드에 로컬 실행 안내가 없다"),
    # 호스팅의 주기 점은 스케줄러가 실제로 쓰는 판정(/api/run/status 의 groups[].due —
    # store.due_groups)을 따른다. 픽스처는 둘을 일부러 **엇갈리게** 둔다(serve_hosted):
    # 서버 시계로는 [AI 노출]이 밀렸고 [검색 성과]는 방금 쟀다 — 페이로드(runs)는 반대다.
    # 페이로드 값으로 점을 달면 둘 다 여기서 걸린다.
    (r'class="rl navgrp">AI 노출<i class="navdue"',
     "서버 시계로 밀린 [AI 노출]에 점이 없다 — 스케줄러와 다른 판정으로 점을 달았다"),
    (r'class="rl navgrp">검색 성과</span>',
     "서버가 방금 잰 [검색 성과]에 점이 섰다 — 스케줄러와 다른 판정으로 점을 달았다"),
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
        [(pid, "striking_distance", f"{SITES[1]} 검색어", 71.2, "평균 9.0위 · 노출 120 · 클릭 8. 이미 1페이지이고 상단 3위권까지 6.0칸 남았습니다 (구글 실적 2026-06-01 기준)"),
         (pid, "ctr_gap", f"{SITES[1]} 두 번째", 58.0, "노출 120에 클릭 0. 제목과 설명이 눌리지 않습니다"),
         # coverage 기회는 target 이 scoring 이 적재한 내부 꼴('cluster:{이름}')이다 —
         # 화면은 그걸 사람이 읽을 이름으로 바꿔 그려야 한다.
         # 검색어 종류가 아니라 심사(verdict) 없이도 그대로 나온다.
         (pid, "coverage", "cluster:피부 관리", 65.0, "이 주제를 다루는 페이지가 아직 없습니다")])
    # 기회 목록은 심사(작업 판정)를 통과한 검색어만 낸다 — 둘 다 작업으로 둔다
    import db, scoring
    db.set_verdicts(conn, pid, [scoring.norm(f"{SITES[1]} 검색어"), scoring.norm(f"{SITES[1]} 두 번째")], "work")
    # 심사에 남는 미판정 검색어 — 띄어쓰기 변형 둘이 한 줄이어야 한다
    conn.executemany(
        "INSERT INTO opportunities(project_id,kind,target,score,status) VALUES(?,?,?,?,'new')",
        [(pid, "striking_distance", TRIAGE_KW, 77), (pid, "aio_exposure", TRIAGE_KW.replace("Z", " Z"), 40),
         (pid, "aio_exposure", TRIAGE_EN, 30)])
    # 기회 묶음 — AI 요약 기회 셋이 GSC 에서 같은 페이지로 들어온다(최신 수집일 06-01).
    grp = [GROUP_KW, f"{GROUP_KW} 비교", f"{GROUP_KW} 가격"]
    conn.executemany(
        "INSERT INTO opportunities(project_id,kind,target,score,reasoning,status)"
        " VALUES(?,'aio_exposure',?,?,'구글이 AI 요약을 붙이는데 내 링크가 없습니다','new')",
        [(pid, t, s) for t, s in zip(grp, (52.0, 47.0, 41.0))])
    conn.executemany(
        "INSERT INTO gsc_snapshots(project_id,snapshot_date,period_days,query,page,clicks,"
        "impressions,ctr,position) VALUES(?,'2026-06-01',28,?,?,0,30,0,12)",
        [(pid, t, f"https://{SITES[1]}.example/grp") for t in grp])
    db.set_verdicts(conn, pid, [scoring.norm(t) for t in grp], "work")
    # 완료 후 관찰 — 두 수집일(05-01: 14위, 06-01: 9위) 사이에 완료한 기회. 그때 14위 → 지금 9위.
    conn.execute("INSERT INTO opportunities(project_id,kind,target,score,status,status_at)"
                 " VALUES(?,?,?,?,'done','2026-05-15 00:00:00')",
                 (pid, "rank_decay", f"{SITES[1]} 검색어", 33))
    conn.execute("INSERT INTO backlink_summary(project_id,checked_date,rank,backlinks,"
                 "referring_domains,broken_backlinks,dofollow,nofollow)"
                 " VALUES(?,?,412,?,1840,0,1512,328)", (pid, d, BL_TOTAL))   # 끊긴 링크 0 — 아래 목록(is_broken=0)과 같은 말
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
    # 역할 판정 — 검색결과는 겹쳐도 판매 채널이라 경쟁사에서 뺀 곳. 경쟁 분석 화면이 그것을
    # 이름표(d.role_labels, 정본 scoring.ROLES)로 따로 보이는지를 본다.
    conn.execute("INSERT INTO competitors(project_id, domain, source, role, role_why)"
                 " VALUES(?, 'mall.example', 'auto_labs', 'channel', ?)",
                 (pid, '["mall 몰", "남의브랜드 가방"]'))
    # 속도 — 같은 페이지인데 모바일만 기준을 넘긴다(현장 값 있음). 화면이 그 하나만
    # 세는지, 출처를 밝히는지를 본다.
    import db as _db
    _vurl = f"https://{SITES[1]}.example/a"
    _db.write_page_vitals(conn, pid, d, [
        {"url": _vurl, "strategy": "mobile", "error": None, "origin_fallback": 0,
         "field_verdict": "SLOW", "field_lcp_ms": 4200, "field_inp_ms": 310,
         "field_cls": 0.24, "field_ttfb_ms": 900,
         "lab_score": 42, "lab_lcp_ms": 4310, "lab_cls": 0.24, "lab_tbt_ms": 640,
         # 구글이 본 SEO — 실패 하나(링크 텍스트). 화면이 한국어 이름표로 그리는지를 본다.
         "seo_score": 92, "seo_json": '{"ok": ["document-title"], "fail": '
                                      '[{"id": "link-text", "n": 1, "items": ["자세히 보기"]}]}'},
        {"url": _vurl, "strategy": "desktop", "error": None, "origin_fallback": 0,
         "field_verdict": "FAST", "field_lcp_ms": 1800, "field_inp_ms": 90,
         "field_cls": 0.02, "field_ttfb_ms": 210,
         "lab_score": 93, "lab_lcp_ms": 1900, "lab_cls": 0.02, "lab_tbt_ms": 40}])
    conn.execute("INSERT INTO keyword_gap(project_id,checked_date,keyword,domain,position,"
                 "our_position,volume,kind) VALUES(?,?,?,?,2,NULL,2400,'missing')",
                 (pid, d, GAP_KW, GAP_RIVAL))
    # 격차는 지금 경쟁사로 등록된 곳의 것만 그린다(scoring.gap_rival_set) — 판매 채널(mall)의
    # 격차 행이 남아 있어도 화면·기회에 안 선다. 둘 다 깔아 경쟁사 쪽만 보이는지를 본다.
    conn.execute("INSERT INTO competitors(project_id, domain, source) VALUES(?, ?, 'manual')",
                 (pid, GAP_RIVAL))
    conn.execute("INSERT INTO keyword_gap(project_id,checked_date,keyword,domain,position,"
                 "our_position,volume,kind) VALUES(?,?,'쇼핑몰만 잡은 검색어','mall.example',1,NULL,9000,'missing')",
                 (pid, d))
    r1 = conn.execute("INSERT INTO crawl_runs(project_id,finished_at,seed,pages,issues)"
                      " VALUES(?,?,'sitemap',10,1)", (pid, prev)).lastrowid
    r2 = conn.execute("INSERT INTO crawl_runs(project_id,finished_at,seed,pages,issues)"
                      " VALUES(?,?,'sitemap',12,2)", (pid, d)).lastrowid
    conn.executemany("INSERT INTO crawl_issues(run_id,kind,severity,url,detail)"
                     " VALUES(?,?,?,?,?)",
                     [(r1, "dup_title", "warn", "/a", "같은 제목"),
                      (r2, "dup_title", "warn", "/a", "같은 제목"),
                      (r2, "broken_internal", "bad", CRAWL_NEW, "404")])
    # AI 인용 — 질문 둘 × 엔진 셋. chatgpt 만 브랜드 질문을 인용한다.
    import db
    conn.executemany("INSERT INTO ai_prompts(project_id,prompt,category) VALUES(?,?,?)",
                     [(pid, AI_PROMPT, "브랜드"), (pid, f"{AI_PROMPT} 추천", "추천")])
    pids = [r[0] for r in conn.execute("SELECT id FROM ai_prompts WHERE project_id=? ORDER BY id",
                                       (pid,))]
    with db.run(conn, pid, "ai") as r:
        for eng in ("chatgpt", "perplexity", "gemini"):
            for i, qid in enumerate(pids):
                cited = 1 if eng == "chatgpt" and i == 0 else 0
                conn.execute("INSERT INTO ai_checks(prompt_id,run_id,engine,cited,mentioned,"
                             "cited_domains_json,answer_excerpt) VALUES(?,?,?,?,?,?,?)",
                             (qid, r.id, eng, cited, cited, '["rival.example"]', "답변"))
    # 내 페이지 감사 — 진단이 여러 줄 나와야 패널의 '손댈 곳 N군데 / 자세히'가 선다.
    # (설명 없음·H1 없음·본문 짧음처럼 규칙에 걸리는 값을 일부러 넣는다.)
    db.write_page_audits(conn, pid, d, [
        {"url": f"https://{SITES[1]}.example/a", "status": 200, "title": "제목",
         "meta_description": None, "h1_json": "[]", "h2_json": "[]", "words": 120,
         "schema_json": "[]", "internal_links": 3, "external_links": 0,
         "images": 2, "images_no_alt": 2, "viewport": 1, "html_lang": "ko"}])
    # 구글 AI 요약 — 추적 검색어 하나가 요약이 떴는데 내 링크가 빠졌다(순위 조회 행).
    kid = conn.execute("INSERT INTO keywords(project_id,keyword,is_active) VALUES(?,?,1)",
                       (pid, AIO_KW)).lastrowid
    db.write_rank_snapshot(conn, kid, 14, f"https://{SITES[1]}.example/a",
                           aio_present=1, aio_cited=0, checked_at=f"{d}T00:00:00Z",
                           aio_domains=[AIO_DOM])
    _plays(conn, pid)
    # AI 에서 온 방문(GA4 부가 조회) — 두 출처가 같은 페이지로 들어왔다.
    db.write_ga4_ai_referrals(conn, pid, d, 28, ["chatgpt.com", "perplexity.ai"],
                              [("chatgpt.com", AI_VISIT_PAGE, AI_VISIT_N - 21, 3),
                               ("perplexity.ai", AI_VISIT_PAGE, 21, 0)])


def _plays(conn, pid: int) -> None:
    """이번 달 할 일 둘 — 수정안이 선 것(점수 높음)과 못 만든 것(result.error).

    표는 엔진 갈래의 db.SCHEMA 가 만든다. 그 전의 Brain 에서도 이 검사가 혼자 서도록
    계약(PLAYS_CONTRACT)의 꼴로 IF NOT EXISTS 를 한 번 더 건다 — 표가 이미 있으면 아무 일도 안 한다."""
    conn.execute("""CREATE TABLE IF NOT EXISTS plays (
        id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL REFERENCES projects(id),
        created_at TEXT DEFAULT CURRENT_TIMESTAMP, page TEXT NOT NULL, title TEXT,
        keywords_json TEXT, opp_ids_json TEXT, score REAL, evidence_json TEXT, result_json TEXT,
        markdown TEXT, model TEXT, cost REAL, status TEXT DEFAULT 'new')""")
    page = f"https://{SITES[1]}.example/bagsZ9"
    ev = {"our": {"url": page, "title": "가방", "h1": "", "words": 320, "fetched_via": "direct"},
          "serp": [{"keyword": "가방Z9", "position": 1, "url": "https://r1.example/b", "domain": "r1.example",
                    "title": "상위 가방", "is_own": 0, "headings": ["소재 고르기", "크기표"], "words": 1400},
                   {"keyword": "가방Z9", "position": 7, "url": page, "domain": f"{SITES[1]}.example",
                    "title": "가방", "is_own": 1, "headings": [], "words": 320}],
          "aio": [{"keyword": "가방Z9", "present": 1, "cited": 0, "domains": ["r1.example"]}],
          "gaps": [PLAY_GAP]}
    res = {"summary": "상위 세 곳이 다루는 소재·크기 안내가 우리 페이지에 없습니다.",
           "why": ["상위 1위는 본문 1,400단어, 우리는 320단어"],
           "title": {"now": "가방", "new": "가방 — 소재·크기로 고르는 법"},
           "meta": {"now": "", "new": "소재와 크기로 가방 고르는 법"},
           "h1": {"now": "", "new": "가방 고르기"},
           "sections": [{"h2": "소재로 고르기", "draft": PLAY_DRAFT, "for": ["가방Z9"]}],
           "links": [{"from": f"https://{SITES[1]}.example/", "anchor": "가방 고르기"}],
           "trust": ["수정일"], "expected": "추정 — 1페이지 상단"}
    rows = [
        (f"{PLAY_TITLE} — 가방 목록", ["가방Z9", "토트백Z9", "숄더백Z9", "크로스백Z9"], 173.4, ev, res,
         "# 가방 목록 페이지 고치기\n\n- 제목을 바꾼다\n"),
        (f"{PLAY_ERR} — 신발 목록", ["신발Z9"], 40.0, {"our": {"url": page, "fetched_via": "none"},
                                                    "gaps": ["상위엔 치수표가 있다"]},
         {"error": PLAY_ERR_WHY}, ""),
    ]
    conn.executemany(
        "INSERT INTO plays(project_id,created_at,page,title,keywords_json,opp_ids_json,score,"
        "evidence_json,result_json,markdown,model,cost,status)"
        " VALUES(?,'2026-06-01 07:00:00',?,?,?,'[]',?,?,?,?,'stub',0,'new')",
        [(pid, page, t, json.dumps(k, ensure_ascii=False), s, json.dumps(e, ensure_ascii=False),
          json.dumps(r, ensure_ascii=False), m) for t, k, s, e, r, m in rows])


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
            # 시장 둘(한국어·영어) — 심사 화면의 언어 거르개를 본다(시장 하나면 전부 한국어다)
            db._write_markets(conn, pid, [("ko-KR", ""), ("en-US", "")])
            for d, pos, clk in (("2026-05-01", 14.0, 3 + i), ("2026-06-01", 9.0, 7 + i)):
                conn.execute(
                    """INSERT INTO gsc_snapshots(project_id,snapshot_date,period_days,
                         query,page,clicks,impressions,ctr,position)
                       VALUES(?,?,28,?,?,?,120,0.1,?)""",
                    (pid, d, f"{name} 검색어", f"https://{name}.example/a", clk, pos))
                # 같은 지면으로 들어오는 **다른** 검색어 — 기회로 안 걸린 것. 이게
                # 없으면 '연관 키워드'가 자기 자신 하나만 세어, 그 검사가 기회 표를
                # 뒤집어 세는 옛 방식과 구별을 못 한다(두 회차 같은 수라 움직인
                # 검색어에도 안 오르고, 3위·20% CTR 이라 기회 판정에도 안 걸린다).
                conn.execute(
                    """INSERT INTO gsc_snapshots(project_id,snapshot_date,period_days,
                         query,page,clicks,impressions,ctr,position)
                       VALUES(?,?,28,?,?,8,40,0.2,3.0)""",
                    (pid, d, f"{name} 곁 검색어", f"https://{name}.example/a"))
            if name == SITES[1]:            # 테스트가 여는 사이트
                _axes(conn, pid)
        conn.commit()
    finally:
        conn.close()


def zero_site(home: Path) -> None:
    """로컬 Brain 에만 세 번째 사이트 — GA4 가 연결돼 AI 유입을 쟀고 0 이었다.

    fixture() 에 안 넣는 이유: 호스팅 대상은 사이트를 서버 저장소(store)에도 등록해야
    열리는데, 거기 SITES 밖의 사이트를 늘리면 hash·목록 검사가 보는 판이 바뀐다."""
    os.environ["CAPTURE_HOME"] = str(home)
    import db
    conn = db.connect()
    try:
        conn.execute("INSERT OR IGNORE INTO projects(name,domain,gsc_property,type,ga4_property)"
                     " VALUES(?,?,?,'saas','123')",
                     (ZERO_SITE, f"{ZERO_SITE}.example", f"sc-domain:{ZERO_SITE}.example"))
        pid = conn.execute("SELECT id FROM projects WHERE name=?", (ZERO_SITE,)).fetchone()[0]
        db.write_ga4_ai_referrals(conn, pid, "2026-06-01", 28, ["chatgpt.com"], [])
        # 두 번 쟀는데 두 번 다 클릭 0 — [분석]이 "수집이 한 번뿐"이라고 거짓말하던 판.
        for d in ("2026-05-01", "2026-06-01"):
            conn.execute("INSERT INTO gsc_snapshots(project_id,snapshot_date,period_days,"
                         "query,page,clicks,impressions,ctr,position) VALUES(?,?,28,?,?,0,50,0,30.0)",
                         (pid, d, f"{ZERO_SITE} 검색어", f"https://{ZERO_SITE}.example/a"))
        # 심사에만 쌓인 기회 하나(미판정) — 개요 목록은 비고, 빈 상태가 [심사하기]로 보낸다.
        conn.execute("INSERT INTO opportunities(project_id,kind,target,score,reasoning,status)"
                     " VALUES(?,'striking_distance',?,75,'r','new')", (pid, f"{ZERO_SITE} 대기"))
        # 열리지 않는 페이지(404)를 점검한 기록 — 데이터 점검(page_404)이 개요의 접힌 띠에 선다.
        # 개요 기회 목록은 건드리지 않는다(위의 '심사에만 쌓임' 빈 상태를 같이 봐야 한다).
        db.write_page_audits(conn, pid, "2026-06-01", [
            {"url": f"https://{ZERO_SITE}.example/gone", "status": 404, "error": "HTTP 404"},
            {"url": f"https://{ZERO_SITE}.example/a", "status": 200, "title": "살아 있는 페이지"}])
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


@contextmanager
def _stdlib(page: bytes):
    """로컬 대시보드·박제본 대상 — 로컬이 실제로 쓰는 것이 stdlib Handler 다."""
    srv = serve(page)
    try:
        yield SimpleNamespace(base=f"http://127.0.0.1:{srv.server_address[1]}",
                              failures=lambda: [], after=lambda: None)
    finally:
        srv.shutdown()
        srv.server_close()


# 호스팅 대상의 테스트 유저. 사이트 둘(SITES)을 이 유저가 소유한다.
HOSTED_EMAIL = "render-check@example.com"


@contextmanager
def serve_hosted(data: Path):
    """호스팅 조립본 대상 — 운영과 같은 **FastAPI 앱**(server.app.app)을 띄운다.

    예전엔 이것도 stdlib serve() 가 서빙했다. 그러면 화면이 부르는 /api/* 가
    dashboard.Handler 의 query.get() 으로 너그럽게 받아지고(운영은 인증·테넌트 격리·
    타입 검증을 한다 — 파라미터가 빠지면 운영은 422, 검사는 통과), 호스팅 전용 라우트
    (/api/settings·/api/run/status …)는 아예 없어서 404 였다. 검사는 그걸 참았고,
    화면에는 "서버가 응답하지 않았습니다" 배너가 떠 있었다.

    stdlib 과 다른 것 네 가지:
      · 로그인 — /d 와 모든 라우트가 `Depends(_require_uid)` 로 인증한다(한 곳). 그
        의존자만 테스트 유저로 갈아 끼운다. 구글 OAuth 는 못 왕복하므로 이게 유일한 길이다.
      · 저장소 — 서버 DB(SEOMINER_DATA)에 유저·사이트를 만들고, 픽스처는 그 유저의
        home(store.home(uid) — tenant() 가 CAPTURE_HOME 으로 세우는 곳)의 brain 에 심는다.
      · 수집기(PROBE) — /d 는 서버가 스스로 조립해 내보낸다. 앱은 안 고치고, 앞에 얇은
        ASGI 층을 세워 /d 응답의 </head> 앞에 끼운다. 못 끼우면 여기서 멈춘다 —
        수집기 없이 도는 호스팅 대상은 JS 가 터져도 초록이 된다.
      · 서버 쪽 기록 — 같은 층이 /api/* 의 4xx/5xx 를 상태·경로·detail 로 적는다.
    lifespan 은 끈다 — 켜면 죽은 런 회수와 스케줄러가 돌고, 등록 직후 사이트는
    due 라서 실제 수집 subprocess 가 뜬다.
    """
    # 요청이 두른 env(CAPTURE_HOME·GSC_TOKEN_FILE·유료 키·SEOMINER_HOSTED)는
    # 스레드풀에서 겹치면 복원 순서가 꼬일 수 있다 — 나갈 때 통째로 되돌린다.
    saved_env = dict(os.environ)
    try:
        with _hosted_app(data) as site:
            yield site
    finally:
        os.environ.clear()
        os.environ.update(saved_env)
        # 윈도우는 열린 sqlite 파일을 못 지운다. 라우트가 닫은 커넥션도 순환 참조에
        # 걸려 있으면 GC 전까지 파일을 쥐고 있다 — 임시 폴더를 지우기 전에 거둔다.
        gc.collect()


@contextmanager
def _hosted_app(data: Path):
    import uvicorn
    from cryptography.fernet import Fernet

    os.environ["SEOMINER_DATA"] = str(data)
    os.environ["SEOMINER_SECRET_KEY"] = Fernet.generate_key().decode()
    sys.path.insert(0, str(ROOT))
    from server import app as hosted                  # server/ 를 sys.path 에 얹는다
    import store                                      # app.py 가 쓰는 바로 그 모듈

    conn = store.connect()
    try:
        uid = store.upsert_user(conn, HOSTED_EMAIL)
        for name in SITES:
            sid = store.add_site(conn, uid, name, f"sc-domain:{name}.example",
                                 f"{name}.example")
            # 한 바퀴 돈 사이트로 둔다 — 등록 직후(last_run_at NULL)면 화면이
            # "첫 분석 진행 중"으로 서서 이 파일이 보려는 화면이 아니다.
            store.mark_run(conn, sid)
            store.mark_done(conn, sid, ok=True)
            # 서버 시계를 페이로드와 엇갈리게 — AI 노출만 밀렸다(HOSTED_MUSTS 의 주기 점)
            conn.execute("UPDATE site_groups SET last_run_at=datetime('now','-1000 hours') "
                         "WHERE site_id=? AND grp='ai'", (sid,))
            conn.commit()
    finally:
        conn.close()
    # tenant() 가 요청마다 CAPTURE_HOME 을 이 값으로 세웠다 되돌린다. 미리 같은 값으로
    # 두면 스레드풀에서 겹친 요청이 서로의 복원을 덮어도 엉뚱한 home 이 안 선다.
    fixture(store.home(uid))

    calls: list[str] = []               # 서버가 본 /api/* 실패
    state = {"probed": None, "entry": {}}   # /d·입구 화면에 수집기를 끼웠나(상태코드와 함께)
    ENTRY = {p for p, _ in ENTRY_PAGES}
    HOLD = ENTRY | {"/d"}
    spawned: list = []                  # 화면을 열기만 했는데 수집이 떴나

    async def app(scope, receive, send):
        if scope["type"] != "http":
            return await hosted.app(scope, receive, send)
        path, method = scope["path"], scope["method"]
        head: dict = {}
        chunks: list[bytes] = []
        mine: list[int] = []            # 이 요청이 calls 에 남긴 줄의 자리

        async def tap(msg):
            if msg["type"] == "http.response.start":
                head.update(msg)
                # 손댈 것(/d)과 적을 것(/api/* 실패)만 붙잡는다 — 나머지는 그대로 흘린다
                head["hold"] = path in HOLD or (path.startswith("/api/")
                                               and msg["status"] >= 400)
                if not head["hold"]:
                    await send(msg)
                return
            if not head["hold"]:
                return await send(msg)
            chunks.append(msg.get("body", b""))
            if msg.get("more_body"):
                return
            body = b"".join(chunks)
            if path in ENTRY:
                # 입구 화면(랜딩·온보딩)에도 수집기를 끼운다 — 못 끼우면 여기서 멈춘다.
                if head["status"] == 200 and b"</head>" in body:
                    body = body.replace(b"</head>", PROBE.encode("utf-8") + b"</head>", 1)
                    state["entry"][path] = 200
                else:
                    state["entry"][path] = f"{head['status']} </head> 없음" \
                        if head["status"] == 200 else str(head["status"])
            elif path == "/d":
                if head["status"] == 200 and b"</head>" in body:
                    body = body.replace(b"</head>", PROBE.encode("utf-8") + b"</head>", 1)
                    # 할 일 서랍 — 줄을 실제로 눌러 본다(PLAY_PROBE). 할 일이 없는 사이트면 그냥 물러난다.
                    body = body.replace(b"</body>", PLAY_PROBE.encode("utf-8") + b"</body>", 1)
                    # 새로고침 복원 검사 하나만 켠다(쿼리 표식) — 모든 /d 응답에 걸면
                    # 이 자리의 다른 검사(기본 화면이 무엇인가)가 클릭·새로고침에 덮인다.
                    if b"reload_probe=1" in scope.get("query_string", b""):
                        body = body.replace(b"</body>", RELOAD_TOUCH.encode("utf-8")
                                            + b"</body>", 1)
                    state["probed"] = 200
                elif head["status"] == 200:
                    state["probed"] = "200 인데 </head> 가 없다"
                else:
                    state["probed"] = (f"{head['status']} "
                                       f"{body[:200].decode('utf-8', 'replace')}")
            else:
                q = scope.get("query_string", b"").decode("latin-1")
                calls.append(f"{method} {path}{'?' + q if q else ''} -> {head['status']} "
                             f"{body[:300].decode('utf-8', 'replace')}")
                mine.append(len(calls) - 1)
            hdrs = [(k, v) for k, v in head["headers"] if k.lower() != b"content-length"]
            hdrs.append((b"content-length", str(len(body)).encode()))
            await send({"type": "http.response.start", "status": head["status"],
                        "headers": hdrs})
            await send({"type": "http.response.body", "body": body})

        try:
            await hosted.app(scope, receive, tap)
        except Exception as e:
            # 500 의 원인. 트레이스백은 uvicorn 이 stderr 에 남기지만 run_checks 는 꼬리
            # 몇 줄만 보여 준다 — 실패 줄에 원인을 같이 적어야 거기서 읽힌다.
            why = f"{type(e).__name__}: {str(e)[:160]}"
            if mine:
                calls[mine[-1]] += f" <- {why}"
            else:
                calls.append(f"{method} {path} -> 예외 {why}")
            raise

    ov =hosted.app.dependency_overrides
    saved_ov = dict(ov)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server = uvicorn.Server(uvicorn.Config(app, lifespan="off", log_level="warning",
                                           access_log=False))
    th = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    try:
        ov[hosted._require_uid] = lambda: uid
        # 수집 실행 진입점 — 화면을 여는 것만으로는 안 불려야 한다. 불리면 가짜로 받아
        # 적고 실패로 친다(진짜로 띄우면 이 PC 에서 워커가 돈다). 이름이 바뀌어 없으면
        # 건너뛴다 — 위 lifespan off 만으로도 자동 수집은 안 뜬다.
        for dep_name in ("_dispatch_dep", "_kick_dep"):
            dep = getattr(hosted, dep_name, None)
            if dep is not None:
                ov[dep] = (lambda n=dep_name: (lambda *a: spawned.append((n, a))))

        sock.bind(("127.0.0.1", 0))
        th.start()
        t0 = time.monotonic()
        while not server.started:
            if not th.is_alive() or time.monotonic() - t0 > 30:
                raise AssertionError("호스팅 앱(uvicorn)이 안 떴다")
            time.sleep(0.05)

        def after():
            assert state["probed"] == 200, (
                f"/d 응답에 오류 수집기를 못 끼웠다 — {state['probed'] or '/d 가 안 불렸다'}. "
                "수집기 없이 도는 호스팅 검사는 JS 가 터져도 초록이다")
            assert not spawned, f"화면을 열기만 했는데 수집이 떴다: {spawned}"

        def entry_after(path):
            assert state["entry"].get(path) == 200, (
                f"{path} 응답에 오류 수집기를 못 끼웠다 — {state['entry'].get(path) or '안 불렸다'}")

        yield SimpleNamespace(base=f"http://127.0.0.1:{sock.getsockname()[1]}",
                              failures=lambda: list(calls), after=after,
                              entry_after=entry_after)
    finally:
        server.should_exit = True
        if th.is_alive():
            th.join(15)
        sock.close()
        ov.clear()
        ov.update(saved_ov)


def dom(browser: str, url: str, profile: Path) -> str:
    r = subprocess.run(
        [browser, "--headless=new", "--disable-gpu", "--no-sandbox",
         f"--user-data-dir={profile}", "--virtual-time-budget=9000", "--dump-dom", url],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    if not r.stdout:
        raise AssertionError(f"브라우저가 페이지를 못 열었다: {url}\n{(r.stderr or '')[-800:]}")
    return r.stdout


def probe(html: str) -> dict:
    """수집기가 DOM 에 옮겨 적은 것 — {"err": JS 오류들, "net": 실패한 /api/* 호출들}."""
    m = re.search(r'id="__probe__"[^>]*>(.*?)</div>', html, re.S)
    if not m or not m.group(1).strip():
        # 수집기 자체가 안 붙었으면 검사가 헛돈 것이다 — 통과로 오해하면 안 된다.
        raise AssertionError("오류 수집기(#__probe__)가 DOM 에 없다 — 페이지가 안 떴다")
    raw = htmlmod.unescape(m.group(1))       # 텍스트 노드라 > & 가 엔티티로 나온다
    try:
        got = json.loads(raw)
    except ValueError:
        return {"err": [raw[:300]], "net": []}
    return {"err": list(got.get("err") or []), "net": list(got.get("net") or [])}


def check(label: str, html: str, musts, server_failures=()) -> None:
    """server_failures: 서버 쪽에서 본 4xx/5xx(호스팅 대상만 준다). 화면 쪽 fetch
    감시(__NET__)와 따로 받는 이유 — 화면이 fetch 가 아닌 길로 부르거나 수집기가 늦게
    서도 서버는 받은 것을 전부 보고, 422 면 무엇이 빠졌는지(detail)까지 준다."""
    p = probe(html)
    assert not p["err"], f"{label} 에서 JS 가 터졌다:\n  " + "\n  ".join(p["err"][:6])
    bad = ([f"서버: {s}" for s in server_failures] +
           [f"화면: {n}" for n in p["net"]])
    assert not bad, (f"{label}: 화면이 부른 /api/* 가 실패했다 — 운영에서도 이 칸은 "
                     "빈 채로 선다:\n  " + "\n  ".join(bad[:10]))
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
    # 쓰는 방식(모드·도구·터미널)은 픽스처가 "안 고른 상태"다. 그런데 paths 는 import 할 때
    # 이 PC 의 진짜 ~/.capture/env 를 os.environ 에 싣는다(load_env) — 사용자가 설정에서
    # 도구를 고른 PC 에서는 그 값이 새어 들어와 '도구 없음' 배지 대신 버튼이 섰고, 검사가
    # 화면이 아니라 이 PC 의 설정 때문에 빨개졌다. 픽스처가 정하는 값은 픽스처가 비운다.
    import doctor
    for k in (doctor.MODE_ENV, doctor.TOOL_ENV, doctor.TERMINAL_ENV):
        os.environ.pop(k, None)
    try:
        fixture(home)
        zero_site(home)
        import dashboard
        # 오류 수집기를 화면 스크립트보다 앞에 세운다
        shell = dashboard.HTML.replace(b"</head>", PROBE.encode("utf-8") + b"</head>", 1)
        assert b"__probe__" in shell, "오류 수집기를 끼울 </head> 를 못 찾았다"

        # 대상마다 (이름, 서버를 세우는 것, 있어야 할 것). 서버는 대상을 볼 때만 선다 —
        # 호스팅 대상은 env 를 갈아 끼우므로 다른 대상과 겹치면 안 된다.
        local = shell.replace(b"</body>", (CARDS_PROBE + PLAY_PROBE).encode("utf-8") + b"</body>", 1)
        targets = [("로컬 대시보드", lambda: _stdlib(local),
                    MUSTS + LOCAL_MUSTS + SHELL_LOCAL_MUSTS + ASK_LOCAL_MUSTS
                    + PLAY_MUSTS + PLAY_LOCAL_MUSTS)]
        # 호스팅 조립본은 리포에서만 만들 수 있다 (플러그인 설치본에 server/ 가 없다).
        # uvicorn·fastapi 도 이 안에서만 들인다(serve_hosted) — 설치본에는 없을 수 있다.
        # 페이지는 여기서 만들지 않는다: /d 가 스스로 조립한다(dashboard.assemble("hosted")
        # + 애드온). 사본을 만들어 먹이면 운영과 조립이 갈라져도 검사가 모른다.
        if (ROOT / "server" / "assets" / "dash.html").exists():
            targets.append(("호스팅 조립본", lambda: serve_hosted(home / "hosted"),
                            HOSTED_MUSTS + ASK_HOSTED_MUSTS + PLAY_MUSTS + PLAY_HOSTED_MUSTS))

        # 박제본 — 같은 템플릿에 데이터를 박아 넣은 자립형 HTML.
        report = dashboard.export(SITES[1]).read_bytes().replace(
            b"</head>", PROBE.encode("utf-8") + b"</head>", 1)
        targets.append(("박제본", lambda: _stdlib(report),
                        REPORT_MUSTS + ASK_LOCAL_MUSTS + PLAY_REPORT_MUSTS + [m for m in view_sections()
                                        if not any(f'[{v}]' in m[1] for v in REPORT_DROPPED)]))

        for label, up, musts in targets:
            with up() as site:
                # 두 번째 사이트를 hash 로 지목한다 — 첫 사이트가 열리면 그게 버그다
                url = f"{site.base}/d#{SITES[1]}"
                page = dom(browser, url, home / "chrome-profile")
                site.after()
                check(label, page, musts, site.failures())
                if label in ("로컬 대시보드", "호스팅 조립본"):
                    # 빈 상태 둘은 사이트가 달라야 선다 — 같은 서버로 hash 만 바꿔 연다.
                    # 호스팅은 저장소에 등록된 SITES 만 열린다(ZERO_SITE 는 로컬 몫). 거기서도
                    # 미연결 문구는 따로 본다: 호스팅은 W() 가 설정 화면 버튼을 끼우는 갈래다.
                    for other, extra in EMPTY_LOADS:
                        if label == "호스팅 조립본" and other not in SITES:
                            continue
                        pg = dom(browser, f"{site.base}/d#{other}", home / "chrome-profile")
                        check(f"{label} #{other}", pg,
                              [(r"<option[^>]*selected[^>]*>" + other,
                                f"hash 가 지목한 {other} 가 안 열렸다")] + extra,
                              site.failures())
                if label == "호스팅 조립본":
                    # 입구 두 화면 — 대시보드 밖에서 서버가 따로 내보낸다. 로그인은 /d 와 같은
                    # 의존자(_require_uid) 갈아 끼우기로 선다 — 랜딩(/)은 세션을 보므로 로그인
                    # 전 화면이다.
                    for path, entry_musts in ENTRY_PAGES:
                        pg2 = dom(browser, site.base + path, home / "chrome-profile")
                        site.entry_after(path)
                        check(f"{label} {path}", pg2, entry_musts, site.failures())
                    # 첫 화면 — 미판정 검색어가 있으면 심사, 없으면 개요(SM.land 순위).
                    # beta-site 는 위 EMPTY_LOADS 루프의 마지막 pg 가 alpha-site 것이다
                    # (호스팅은 SITES 밖 ZERO_SITE 를 건너뛰므로 alpha-site 하나뿐이다).
                    check(f"{label} 기본 화면 #{SITES[1]}(미판정 있음)", page,
                          [(r'id="view-triage"(?![^>]*hidden)',
                            "미판정 검색어가 있는데 기본 화면이 [심사]가 아니다"),
                           (r'id="view-overview"[^>]*hidden',
                            "미판정 검색어가 있는데 [개요]가 기본으로 앞에 섰다")],
                          site.failures())
                    check(f"{label} 기본 화면 #{SITES[0]}(미판정 없음)", pg,
                          [(r'id="view-overview"(?![^>]*hidden)',
                            "미판정 검색어가 없는데 기본 화면이 [개요]가 아니다"),
                           (r'id="view-triage"[^>]*hidden',
                            "미판정 검색어가 없는데 [심사]가 기본으로 앞에 섰다")],
                          site.failures())
                    # 새로고침 복원 — [순위]를 직접 연 뒤 새로고침해도 그 화면이 남아야
                    # 한다(sessionStorage). 표식(reload_probe=1)이 있는 요청에만
                    # RELOAD_TOUCH 를 끼운다(serve_hosted 의 tap()) — 다른 검사까지
                    # 덮으면 위 기본 화면 검사가 클릭·새로고침에 흔들린다.
                    rl = dom(browser, f"{site.base}/d?reload_probe=1#{SITES[1]}",
                            home / "chrome-profile")
                    check(f"{label} 새로고침 복원", rl,
                          [(r'id="view-rank"(?![^>]*hidden)',
                            "새로고침 후 사용자가 연 [순위] 화면이 안 남았다 — 첫 화면으로 돌아갔다"),
                           (r'id="view-triage"[^>]*hidden',
                            "새로고침 후 기본 화면([심사])이 되돌아온 화면을 덮었다")],
                          site.failures())
                if label == "박제본":
                    # 화면 상자는 런타임에 생긴다 — 소스에서 세면 0 이라 단언이 늘 참이다.
                    # 선언(view-def)에서 세고 박제본이 빼는 둘을 뺀다.
                    print_all(browser, url, home,
                              len(view_defs_ids()) - len(REPORT_DROPPED))
                print(f"  {label}: ok")
    finally:
        gc.collect()
        shutil.rmtree(home, ignore_errors=True)


if __name__ == "__main__":
    run()
    print("render self-check ok")
