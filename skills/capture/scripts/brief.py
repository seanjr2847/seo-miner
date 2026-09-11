#!/usr/bin/env python3
"""요청문 — 기회 한 건을 Claude Code 에 붙여 넣을 브리프로 세운다.

옛 요청문(dashboard.html 의 fixPrompt)은 틀 한 벌이었다: "아래 페이지를 고쳐 주세요"
로 시작해 그 페이지의 감사 결과를 적고, 종류마다 다른 건 가운데 할 일·산출물 두세
줄뿐이었다. 그래서 색인 막힘과 클릭률 미달과 남의 도메인에 보낼 연락문이 80% 같은
글이 됐고, 새 글 브리프에 "이 페이지에 이미 있는 내용에서만 가져오세요"가 붙었다.

여기서는 세 가지를 갈라 세운다.

1. **일의 꼴(SHAPES)이 먼저다.** 종류 14개는 실제로 다섯 가지 일이다 — 있는 페이지
   고치기 / 새 글 설계 / 주소 정리(301·canonical) / 기술 점검 / 외부 연락. 머리말·
   답의 형식·규칙은 꼴이 갖고, 종류는 그 안에 근거와 세부만 채운다.
2. **근거는 문장이 아니라 표다.** 종류마다 판정에 쓴 숫자(나눠 갖는 두 페이지,
   경쟁 도메인의 순위, 챗봇이 대신 인용한 곳과 그 답변 발췌, 모바일 vs 데스크톱)를
   그대로 낸다. `reasoning` 한 줄로 뭉개지 않는다.
3. **산출물에 형식 계약이 붙는다.** "title 3안"이 아니라 "표: 안 | 글자 수 | 검색어
   자리 | 이유". 길이 기준과 언어는 사이트의 언어-지역에서 온다.

처방(what/acts/deliver)의 정본은 그대로 scoring.KINDS 다 — 여기서 새로 판정하지
않는다. 화면(dashboard.html 의 askBlock)은 build() 가 낸 body 와 tails() 가 낸
꼴별 꼬리를 이어 붙여 그리기만 한다. 꼬리를 따로 실어 보내는 이유는 기회 200건이
같은 형식·규칙 600자를 200번 싣지 않게 하려는 것뿐이다 — text() 가 둘을 잇는다.
"""
from __future__ import annotations

import json
from typing import Callable

import scoring
import serp_adapter

# ── 일의 꼴 ──────────────────────────────────────────────────────────────────
# 이름·순서의 정본. 화면의 폴백(기회로 아직 안 올라온 행)도 이 이름만 쓴다 —
# test_seams 가 대조한다.
SHAPE_NAMES = ("fix_page", "new_content", "consolidate", "technical", "outreach")

SHAPES: dict[str, dict] = {
    "fix_page": dict(
        label="있는 페이지 고치기",
        intro="아래 페이지가 이 검색어에서 더 잘 보이게 고쳐 주세요. 새로 쓰는 일이 "
              "아닙니다 — 지금 있는 페이지의 제목·설명·본문 구조를 손보는 일입니다.",
        form=["안이 여럿인 것(title·meta description)은 표: 안 | 글자 수 | 검색어가 "
              "들어간 자리 | 이 안을 고른 이유 한 줄.",
              "본문에 보탤 구간은 H2 제목마다 그 아래에서 답할 내용 한 줄과 근거로 쓸 "
              "출처(이 페이지 안의 문장, 또는 [확인 필요]).",
              "신뢰 신호 — 저자(누가 썼는지·왜 이 사람인지), 근거 출처, 마지막 "
              "수정일 중 이 페이지에 **없는 것**과 무엇을 넣을지. 있는 것은 '있음' 한 "
              "줄로 끝냅니다.",
              "마지막은 '바꾼 것' 표: 진단 항목 | 전 | 후. 진단에 없는 것을 바꿨으면 "
              "왜 바꿨는지 한 줄."],
        graph="",
        rules=["사실은 위 '지금 이 페이지 상태'와 '근거'에 있는 것만 씁니다. 수치·후기·"
               "효능·수상 이력을 지어내지 않습니다. 모르는 것은 [확인 필요]로 남깁니다.",
               "저자·자격·경력을 지어내지 않습니다. 신뢰 신호는 '무엇을 넣어야 하는지'까지만 "
               "말하고, 이름·자격은 [저자] 자리로 비워 둡니다.",
               "이미 있는 문단은 지우지 않습니다. 보태거나 옮기는 것까지만.",
               "검색어를 억지로 반복하지 않습니다. title·H1·첫 문단에 한 번씩 자연스럽게 "
               "들어가면 충분합니다.",
               "요청하지 않은 것(디자인·URL 변경·다른 페이지)은 손대지 않습니다."],
        slot="이 검색어로 상위에 있는 페이지 2~3개의 제목과 H2 목록을 여기에 붙이면, "
             "'빠진 구간'을 짐작이 아니라 비교로 찾습니다.",
        limits=True),
    "new_content": dict(
        label="새 글 설계",
        intro="아래 주제로 새 글의 설계도를 만들어 주세요. 본문 전체가 아니라 제목·목차·"
              "각 구간에서 답할 것까지입니다 — 본문은 이 설계도가 정해진 뒤에 씁니다.",
        form=["제목 3안 표: 안 | 글자 수 | 검색어 자리 | 어떤 검색 의도에 답하는지.",
              "목차는 H1 하나 아래 H2/H3 트리로. H2 마다 그 구간이 답하는 질문 한 줄과 "
              "분량(단어 수) 눈대중.",
              "우리 제품·데이터로만 쓸 수 있는 구간은 제목 앞에 [내 데이터] 를 붙이고, "
              "무엇을 넣어야 하는지 적습니다.",
              "신뢰 신호 — 이 글을 누가 쓰는 게 맞는지(어떤 경험·자격), 1차 자료로 "
              "무엇을 쓸지, 어떤 주장에 출처가 필요한지. 이름·자격은 [저자] 자리로 둡니다.",
              "발행 뒤 내부 링크 표: 어느 글에서 | 앵커 텍스트 | 넣을 자리."],
        graph="목차는 Mermaid `flowchart TD` 트리 하나로 그립니다 — H1 아래 H2, H2 아래 "
              "H3. 질문 한 줄과 분량은 그 옆 표가 갖습니다.",
        rules=["경쟁 페이지의 문장·구성을 그대로 옮기지 않습니다. 같은 질문에 답하되 "
               "순서와 관점은 우리 것으로.",
               "저자·자격·경력을 지어내지 않습니다. 무엇이 필요한지까지만 말하고 이름은 "
               "[저자] 자리로 비워 둡니다.",
               "수치·후기·효능은 지어내지 않습니다. 근거가 필요한 자리는 [확인 필요]로 "
               "비워 둡니다.",
               "한 글이 한 검색 의도에 답합니다. 두 의도가 섞이면 글을 둘로 나누자고 "
               "말해 주세요.",
               "본문 전체를 쓰지 않습니다. 설계도까지만."],
        slot="이 검색어로 상위에 있는 페이지 2~3개의 제목과 H2 목록을 여기에 붙이면, "
             "다뤄야 할 구간을 짐작이 아니라 비교로 정합니다.",
        limits=True),
    "consolidate": dict(
        label="주소 정리",
        intro="아래 주소들을 정리해 주세요. 글을 새로 쓰거나 고치는 일이 아니라, 어느 "
              "주소를 남기고 나머지를 어디로 보낼지 정하는 일입니다.",
        form=["결정 표: 주소 | 처분(정본으로 남김 / 301 → 어디로 / canonical → 어디로 / "
              "합침) | 근거(위 표의 노출·클릭·의도).",
              "합치는 경우에만: 합친 뒤의 H2 목록과, 어느 글의 어느 문단이 어디로 가는지.",
              "리다이렉트·canonical 은 적용할 코드나 설정 예시를 `<pre>` 로. 스택을 "
              "모르면 [스택 확인] 이라 쓰고 가장 흔한 두 경우의 예시를 줍니다.",
              "적용 뒤 확인: 무엇을 어디서 보면 된 것인지 순서대로."],
        rules=["근거는 위 표의 숫자입니다. 감으로 정본을 고르지 않습니다. 숫자가 비슷하면 "
               "그렇다고 말하고 검색 의도로 가릅니다.",
               "내용을 지우자고 하지 않습니다. 합칠 때는 문단을 옮기는 것까지만.",
               "리다이렉트 사슬(A→B→C)을 만들지 않습니다. 이미 리다이렉트인 주소는 최종 "
               "주소로 바로 보냅니다.",
               "홈으로 몰지 않습니다. 가장 가까운 주제의 페이지로 보냅니다."],
        graph="주소 처분은 Mermaid `flowchart LR` 지도 하나로 그립니다 — 정본으로 남길 "
              "주소는 굵은 상자, 화살표에 처분(301·canonical·합침)을 답니다.",
        slot="", limits=False),
    "technical": dict(
        label="기술 점검",
        intro="아래 주소의 기술 문제를 잡아 주세요. 글의 내용은 손대지 않습니다 — 색인·"
              "크롤·모바일 화면처럼 검색엔진이 페이지에 닿는 길을 고치는 일입니다.",
        form=["점검 표: 항목 | 확인하는 방법(어디서 무엇을 보나) | 지금 값 | 고칠 값.",
              "고칠 값이 코드·설정이면 `<pre>` 로. 파일 경로·태그 위치까지 적습니다.",
              "손대는 순서: 무엇을 먼저 고쳐야 다음 것이 뜻이 있는지.",
              "고친 뒤 확인: 어디서(Search Console·브라우저·curl) 무엇을 보면 고쳐진 "
              "것인지."],
        rules=["위 근거에 없는 것은 짐작하지 않습니다. 원인 후보가 여럿이면 후보와 가르는 "
               "방법을 나란히 적습니다.",
               "스택(CMS·프레임워크·호스팅)을 모르면 [스택 확인] 이라 쓰고, 가장 흔한 두 "
               "경우의 예시를 줍니다.",
               "본문 문장을 고치자고 하지 않습니다. 구조·설정·자원 크기까지만.",
               "요청한 주소만 봅니다. 사이트 전체 재구성을 제안하지 않습니다."],
        graph="손대는 순서는 Mermaid `flowchart TD` 하나로 그립니다 — 앞것이 뒤것의 "
              "전제인 것만 화살표로 잇습니다. 나란히 해도 되는 것은 잇지 않습니다.",
        slot="", limits=False),
    "outreach": dict(
        label="외부 연락",
        intro="아래 사이트에 보낼 연락문을 만들어 주세요. 링크를 달라는 부탁이 아니라, "
              "그쪽 글에서 빠진 것을 우리가 채워 준다는 제안입니다.",
        form=["그쪽 글이 무엇을 다루는지 두 줄. 모르면 [글 확인] 이라 쓰고 어떤 글일지 "
              "후보를 적습니다.",
              "우리가 더 잘 답하는 지점 하나. 근거는 우리 페이지에 실제로 있는 것만.",
              "연락문: 제목 한 줄 + 본문 120단어 이내. 대상 언어로 씁니다. 보내는 사람은 "
              "[이름], 우리 페이지는 [URL] 자리로 둡니다.",
              "그쪽이 링크할 만한 우리 페이지. 없으면 무엇을 먼저 만들어야 하는지 한 줄."],
        rules=["'링크 부탁드립니다'류 문장을 쓰지 않습니다. 무엇이 빠졌고 무엇을 줄 수 "
               "있는지만.",
               "아첨·과장·긴 자기소개를 넣지 않습니다. 첫 문장에서 그쪽 글의 구체적인 한 "
               "지점을 짚습니다.",
               "우리 페이지에 없는 것을 있다고 하지 않습니다. 없으면 만들자고 합니다.",
               "한 통에 부탁 하나. 여러 페이지를 한꺼번에 밀지 않습니다."],
        graph="",
        slot="", limits=False),
}
assert tuple(SHAPES) == SHAPE_NAMES

# ── 답의 형식: 자립형 HTML 리포트 한 장 ──────────────────────────────────────
# 꼴 5개가 여기서 전부 같은 글을 쓴다 — 그래서 한 벌만 둔다. tails() 가 꼴마다 세
# 자리만 갈아 끼운다: 파일명 조각(slug)·부를 스크립트(scripts)·무엇을 그래프로
# 그리나(graph).
#
# 산출물 계약(무엇을 만드나)은 SHAPES[*]["form"] 이 갖고, 여기는 그리는 법(어디에
# 쓰나·무엇으로 그리나)만 갖는다. 둘을 섞으면 "`## 소제목`을 답니다"와 "카드 하나로
# 그립니다"가 한 요청문에 나란히 실린다 — test_brief 가 그것을 막는다.
#
# 마크다운을 시키던 옛 꼬리는 답이 채팅 스크롤 안에서 끝났다. 표 셋과 H2 목록과
# 코드 블록을 한 화면에서 견줘야 하는 일인데 위아래로 굴려야 했다.
HTML_FORM = """답은 **자립형 HTML 파일 한 장**입니다. 채팅 본문에 산출물을 늘어놓지 않습니다.

### 파일
- 임시 폴더($TMPDIR, 없으면 %TEMP%)에 `seo-{slug}-<타임스탬프>.html` 로 씁니다.
- 저장소 안에는 아무것도 남기지 않습니다.
- 다 쓰면 엽니다 — Windows `start <경로>`, macOS `open <경로>`, Linux `xdg-open <경로>`.
- 마지막 줄에 그 파일의 **절대경로**를 적어 줍니다.
- {scripts} 그 밖의 앱 코드·상호작용은 넣지 않습니다.

### 그림
- {graph}
- {graph2}
- 고치는 산출물은 **지금 값 | 고친 값**을 나란히 놓습니다 — 고친 값만으로는 안 보입니다.
- 자간(letter-spacing)·대문자화·등폭은 라틴 문자열에만 겁니다. 한글 라벨의 위계는
  크기·굵기·색으로 만들고, 등폭은 숫자·URL·날짜·식별자에만 씁니다.
- 여백을 넉넉히, 색은 아껴 씁니다 — 강조 하나, 경고에 amber, 빠진 것에 red.

### 담을 것
산출물마다 카드 하나, 아래 순서대로:"""

# 그래프로 그릴 관계가 없는 꼴(고치기·연락)에는 다이어그램 라이브러리를 아예 안
# 부른다. 한 벌로 실으면 목차도 리다이렉트 지도도 없는 답에 억지 그림이 하나 생긴다.
_SCRIPTS_PLAIN = "`<script>` 는 Tailwind CDN(cdn.tailwindcss.com) 하나뿐입니다."
_SCRIPTS_GRAPH = ("`<script>` 는 Tailwind CDN(cdn.tailwindcss.com)과 "
                  "Mermaid ESM(cdn.jsdelivr.net) 둘뿐입니다.")
_GRAPH_NONE = "이 꼴에는 그래프로 그릴 관계가 없습니다 — 다이어그램 라이브러리를 안 부릅니다."
_GRAPH_NONE2 = "카드·표·inline SVG 로 그립니다."
_GRAPH_TAIL = ("나머지는 카드·표·inline SVG 입니다 — 전부 다이어그램으로 그리면 어느 "
               "산출물이 무엇인지 안 갈립니다.")

# 종류 → 꼴. 값이 문자열이면 고정, 함수면 (gap_kind, has_page) 로 가른다 —
# 콘텐츠 공백은 '밀린다'(고친다)와 '없다'(새로 쓴다)가 정반대의 일이고,
# 챗봇·AI 요약은 이미 걸린 페이지가 있으면 고치고 없으면 새로 쓴다.
_by_page: Callable[[str | None, bool], str] = \
    lambda gk, has_page: "fix_page" if has_page else "new_content"
KIND_SHAPE: dict[str, str | Callable[[str | None, bool], str]] = {
    "striking_distance": "fix_page",
    "ctr_gap": "fix_page",
    "cannibalization": "consolidate",
    "rank_decay": "fix_page",
    "pseo_pattern": "new_content",
    "device_gap": "technical",
    "index_blocked": "technical",
    "coverage": "new_content",
    "ai_citation_gap": _by_page,
    "aio_exposure": _by_page,
    "content_gap": lambda gk, has_page: "fix_page" if gk == "weak" else "new_content",
    "crawl_issue": "consolidate",
    "backlink_broken": "consolidate",
    "backlink_prospect": "outreach",
    "ai_bot_blocked": "technical",
}
assert set(KIND_SHAPE) == set(scoring.ALL_KINDS)

# 꼴이 같아도 머리말이 틀리는 종류 — 템플릿 패턴은 "글 한 장"이 아니라 "찍는 틀"이다.
INTRO_BY_KIND = {
    "pseo_pattern": "아래 검색어 무리를 한 장씩 쓰지 않고 템플릿으로 찍을 설계도를 만들어 "
                    "주세요. 축(무엇이 바뀌며 여러 장이 되나)·틀 한 벌·허브 구성까지입니다 "
                    "— 페이지 본문은 그 뒤에 데이터로 채웁니다.",
    "device_gap": "아래 페이지가 모바일에서만 밀리는 원인을 잡아 주세요. 글의 내용은 손대지 "
                  "않습니다 — 화면·속도·자원 크기처럼 모바일에서 다르게 보이는 것을 고치는 "
                  "일입니다.",
    "ai_bot_blocked": "아래 AI 크롤러가 robots.txt 로 막혀 있습니다. 열지 말지 정하고, "
                      "연다면 어느 줄을 어떻게 고칠지 알려 주세요. 글은 손대지 않습니다 — "
                      "막힌 채로는 고쳐도 안 읽힙니다.",
    "backlink_broken": "아래 주소로 들어오던 링크를 되살려 주세요. 이미 번 링크라 새로 얻는 "
                       "것보다 늘 쌉니다 — 어디로 301 할지 정하고, 링크를 건 쪽에 보낼 짧은 "
                       "안내문까지입니다.",
}

# 진단 tag → 그 항목을 고치는 데 실제로 필요한 산출물. 처방(play.deliver)이 없는
# 폴백(기회로 아직 안 올라온 행)에서만 쓴다 — 화면이 window.BRIEF.by_tag 로 받는다.
DELIVER_BY_TAG = {
    "title": "새 title 3안 — 검색어를 앞에 두고, 길이 기준 안에서",
    "meta description": "meta description 2안 — 길이 기준 안에서, 클릭할 이유를 담아서",
    "H1": "H1 문안 하나 — title 과 같은 말을 하도록",
    "본문": "본문에 추가할 H2 목록과 각 항목에서 답할 내용 — 이미 있는 문단은 그대로 둡니다",
    "구조화 데이터": "이 페이지에 맞는 구조화 데이터(JSON-LD) 한 벌",
    "robots": "고칠 meta robots 값과 그 태그가 들어갈 위치",
    "canonical": "canonical 을 어느 URL 로 바꿀지와 그 근거",
    "이미지": "alt 가 빠진 이미지에 넣을 문안",
    "내부 링크": "어느 글에서 이 페이지로 링크를 걸지 — 앵커 텍스트까지",
    "가져오기": "이 URL 이 안 열리는 원인 후보와 확인 순서 — 콘텐츠는 손대지 않습니다",
    "모바일": "head 에 넣을 viewport 태그 한 줄과, 그 뒤 모바일에서 확인할 것",
    "속도": "기준을 넘긴 지표마다 무엇을 고칠지 — 파일·태그 자리까지, 그리고 고친 뒤 "
            "어느 값이 먼저 움직이는지",
    "언어": "이 페이지에 맞는 <html lang> 값 한 줄",
    "hreflang": "고칠 hreflang 목록 — 코드 | 주소 | 무엇을 바꿨나 (자기 참조·x-default 포함)",
    "갱신": "이 글에서 지금도 맞는지 확인할 것 목록과, 고칠 문장 — 날짜만 바꾸지 않습니다",
}
DELIVER_DEFAULT = "지금 이 페이지에서 가장 먼저 고칠 것 세 가지와, 각각 무엇을 무엇으로 바꿀지"


# ── 언어·길이 ────────────────────────────────────────────────────────────────
_LOCALE_LABEL = dict(serp_adapter.LOCALES)          # "ja-JP" → "일본어 · 일본"
_CJK_LANGS = {"ko", "ja", "zh"}                      # 검색결과 폭을 로마자의 두 배로 먹는 문자


def lang_label(locale: str) -> str:
    """'ja-JP' → '일본어'. 매핑에 없으면 언어 코드 그대로 — 지어내지 않는다."""
    lab = _LOCALE_LABEL.get(locale or "")
    if lab:
        return lab.split(" · ")[0]
    return serp_adapter.lang_of(locale) or "한국어"


def limits(locale: str) -> tuple[int, int]:
    """(title 최대, meta description 최대) — page_advice 와 같은 임계값을 본다.

    검색결과는 글자 수가 아니라 픽셀 폭으로 자르므로 한글·가나·한자는 로마자 기준의
    절반이다. 한 벌로 두면 CJK 사이트의 요청문마다 "60자 이내"라는 틀린 기준이 나간다.
    """
    if serp_adapter.lang_of(locale) in _CJK_LANGS:
        return scoring.TITLE_MAX_KO, scoring.DESC_MAX_KO
    return scoring.TITLE_MAX, scoring.DESC_MAX


def tails(locale: str) -> dict[str, str]:
    """꼴별 꼬리(답의 형식 + 규칙) — 프로젝트마다 한 벌. 언어·길이 기준이 여기 들어간다.

    형식의 몸통은 HTML_FORM 한 벌이고 꼴이 대는 것은 세 자리뿐이다. 꼬리를 기회마다
    싣지 않는 이유(같은 글을 200번 안 보낸다)는 그대로다 — text() 가 둘을 잇는다.
    """
    lang = lang_label(locale)
    t_max, d_max = limits(locale)
    out = {}
    for name, s in SHAPES.items():
        g = s["graph"]
        L = ["## 답의 형식",
             HTML_FORM.format(slug=name,
                              scripts=_SCRIPTS_GRAPH if g else _SCRIPTS_PLAIN,
                              graph=g or _GRAPH_NONE,
                              graph2=_GRAPH_TAIL if g else _GRAPH_NONE2),
             ""]
        L += [f"{i + 1}. {x}" for i, x in enumerate(s["form"])]
        L += ["- 안이 여럿인 자리는 안마다 배지를 답니다: 강함 / 검토 / 추측. 배지 없이 "
              "안만 늘어놓으면 무엇을 고를지 사용자가 다시 묻게 됩니다.",
              "- 확인 못 한 자리는 [확인 필요] 배지로 **화면에 보이게** 남깁니다. "
              "지어내서 채우지 않습니다.",
              "- 맨 끝에 '먼저 할 것' 카드 하나: 어느 산출물부터 적용할지 | 이유 한 줄 | "
              "그 카드로 가는 앵커 링크."]
        L.append(f"- 언어: 산출물(제목·본문·연락문)은 {lang}로 씁니다. 사이트 언어-지역 "
                 f"{locale}. 설명은 이 요청문과 같은 한국어로 해 주세요.")
        if s["limits"]:
            L.append(f"- 길이 기준({lang}): title {t_max}자 이내, meta description {d_max}자 "
                     "이내. 검색결과는 글자 수가 아니라 폭으로 자르므로 여유를 둔 값입니다.")
        L += ["", "## 규칙"]
        L += [f"- {x}" for x in s["rules"]]
        out[name] = "\n".join(L)
    return out


def shapes_payload(locale: str) -> dict:
    """화면이 받는 한 벌 — 꼬리·머리말·붙여 넣기 칸·진단별 산출물. 폴백 요청문의 재료."""
    return {"tails": tails(locale),
            "labels": {k: s["label"] for k, s in SHAPES.items()},
            "intro": {k: s["intro"] for k, s in SHAPES.items()},
            "slot": {k: s["slot"] for k, s in SHAPES.items()},
            "page_state": [k for k, s in SHAPES.items() if _shows_page(k)],
            "by_tag": DELIVER_BY_TAG, "deliver_default": DELIVER_DEFAULT,
            "locale": locale, "lang": lang_label(locale)}


# ── 조립 ────────────────────────────────────────────────────────────────────
def shape_of(kind: str, *, gap_kind: str | None = None, has_page: bool = False) -> str:
    s = KIND_SHAPE.get(kind, "fix_page")
    return s if isinstance(s, str) else s(gap_kind, has_page)


def _shows_page(shape: str) -> bool:
    """'지금 이 페이지 상태' 섹션을 갖는 꼴 — 페이지를 손대거나(fix) 점검하거나(technical)
    정리(consolidate)하는 일. 새 글과 연락문에는 고칠 페이지가 없다."""
    return shape in ("fix_page", "technical", "consolidate")


def _n(v) -> str:
    """천 단위 쉼표. None 은 '—'."""
    if v is None:
        return "—"
    return f"{v:,}" if isinstance(v, int) else str(v)


def _cell(v) -> str:
    return str(v if v is not None else "—").replace("|", "\\|").replace("\n", " ")


def _table(heads: list[str], rows: list[list]) -> list[str]:
    if not rows:
        return []
    return ["| " + " | ".join(heads) + " |",
            "|" + "|".join(" --- " for _ in heads) + "|",
            *("| " + " | ".join(_cell(c) for c in r) + " |" for r in rows)]


def _pages_table(pages: list[dict]) -> list[str]:
    """이 검색어로 걸린 내 페이지들 — query_pages 행 그대로."""
    return _table(["내 페이지", "노출", "클릭", "CTR", "평균 순위"],
                  [[p.get("page"), _n(p.get("impressions")), _n(p.get("clicks")),
                    f"{p.get('ctr')}%" if p.get("ctr") is not None else "—",
                    f"{p['position']}위" if p.get("position") is not None else "—"]
                   for p in pages[:6]])


def _page_state(a: dict | None, url: str) -> list[str]:
    """감사 결과를 사실 그대로 — 판정은 page_advice(진단)가 한다. 옛 요청문이 안 싣던
    H2 목록·내부 링크·alt 를 싣는다. "본문에 보탤 H2"를 시키면서 지금 H2 를 안 주면
    AI 는 이미 있는 것을 또 제안한다."""
    if not a:
        return ["## 지금 이 페이지 상태",
                "- 아직 이 페이지를 직접 점검하지 않았습니다. 아래를 스스로 확인하고 고쳐 주세요.",
                ""]
    if a.get("error"):
        return ["## 지금 이 페이지 상태", f"- 점검 실패: {a['error']}", ""]
    h1, h2 = scoring._as_list(a.get("h1_json")), scoring._as_list(a.get("h2_json"))
    sc = scoring._as_list(a.get("schema_json"))
    title, desc = a.get("title") or "", a.get("meta_description") or ""
    fresh = scoring._has_render_fields(a)
    L = [f"## 지금 이 페이지 상태 ({a.get('checked_date') or '점검일 미상'} 직접 확인)"]
    # 응답 코드부터. 200 인지 리다이렉트 끝인지 모른 채 "지금 값 → 고칠 값" 표를
    # 시키면 첫 칸부터 빈다.
    if a.get("status") is not None:
        L.append(f"- HTTP 상태: {a['status']}")
    L += [f"- title: {title or '(없음)'}" + (f" — {len(title)}자" if title else ""),
          f"- meta description: {desc or '(없음)'}" + (f" — {len(desc)}자" if desc else ""),
          f"- H1: {' / '.join(h1) if h1 else '(없음)'}"]
    if h2:
        shown = h2[:12]
        L.append(f"- H2 ({len(h2)}개): " + " / ".join(shown)
                 + (f" … 외 {len(h2) - len(shown)}개" if len(h2) > len(shown) else ""))
    else:
        L.append("- H2: (없음)")
    L.append(f"- 본문 길이: {_n(a.get('words'))}단어")
    # 이 줄이 "(없음)" 한 마디였을 때, 자바스크립트로 스키마를 넣는 사이트(Yoast·
    # RankMath·AIOSEO)에서 있는 것을 없다고 말했다. 우리가 본 것이 정적 HTML 뿐임을
    # 요청문이 먼저 밝힌다 — 그래야 AI 가 확인부터 시킬 수 있다.
    L.append(f"- 구조화 데이터: {', '.join(sc) if sc else '(없음)'}"
             + ("" if sc or not fresh else " — 정적 HTML 기준"))
    if fresh:
        L.append(f"- 뷰포트: {a.get('viewport') or '(없음 — 모바일에서 데스크톱 폭으로 그립니다)'}")
        hl = [x for x in scoring._as_list(a.get("hreflang_json")) if isinstance(x, list)]
        lang_bits = [f"html lang: {a.get('html_lang') or '(없음)'}"]
        if hl:
            lang_bits.append("hreflang " + ", ".join(str(c) for c, _ in hl[:8])
                             + (f" 외 {len(hl) - 8}개" if len(hl) > 8 else ""))
        L.append("- " + " · ".join(lang_bits))
        when = " · ".join(x for x in (f"발행 {a['published']}" if a.get("published") else "",
                                      f"수정 {a['modified']}" if a.get("modified") else "") if x)
        L.append(f"- 글의 날짜: {when or '(페이지에 안 적혀 있습니다)'}")
    if a.get("js_shell"):
        L.append("- **주의**: 이 페이지는 본문을 자바스크립트로 그리는 것으로 보입니다"
                 "(정적 HTML 에 본문이 거의 없습니다). 위의 본문 길이·H2·구조화 데이터는 "
                 "렌더 전 값이라 실제와 다를 수 있습니다 — 사실로 쓰기 전에 브라우저나 "
                 "리치 결과 테스트로 한 번 확인해 주세요.")
    if a.get("canonical"):
        L.append(f"- canonical: {a['canonical']}")
    if a.get("robots"):
        L.append(f"- meta robots: {a['robots']}")
    links = []
    if isinstance(a.get("internal_links"), int):
        # "들어오는 내부 링크" 를 같은 요청문 안에서 따로 말한다 — 방향을 안 적으면
        # 두 숫자가 같은 것의 두 값처럼 읽힌다.
        links.append(f"내보내는 내부 링크 {a['internal_links']}개")
    if isinstance(a.get("external_links"), int):
        links.append(f"외부 링크 {a['external_links']}개")
    if isinstance(a.get("images"), int):
        links.append(f"이미지 {a['images']}개" + (f" (alt 없음 {a['images_no_alt']}개)"
                                                 if a.get("images_no_alt") else ""))
    if links:
        L.append("- " + " · ".join(links))
    L.append("")
    return L


# 크롤이 이 주소에 대해 이미 아는 것 중, 페이지 한 장만 봐서는 절대 알 수 없는 것.
# 나머지(thin_content·missing_h1 등)는 page_advice 가 같은 말을 이미 한다 — 두 벌로
# 실으면 요청문 안에서 같은 지적이 두 번 나온다.
SITE_ONLY_ISSUES = ("dup_title", "dup_description", "orphan", "redirect_chain",
                    "broken_internal", "canonical_mismatch")


def _ms(v) -> str:
    return f"{v / 1000:.1f}초" if isinstance(v, (int, float)) else "—"


def _vitals_rows(ctx: dict, url: str | None) -> dict:
    return (ctx.get("vitals") or {}).get(url or "") or {}


def _vitals_lines(ctx: dict, url: str | None) -> list[str]:
    """이 페이지의 속도 — 기기별로 나란히. 없으면 아무 줄도 안 만든다.

    "지금 값 | 고칠 값" 표를 시키면서 지금 값을 안 주면 첫 칸이 늘 빈다. 이 줄들이
    없던 동안 기술 점검·기기 격차 요청문은 속도를 고치라면서 속도를 한 번도
    말하지 못했다.
    """
    rows = _vitals_rows(ctx, url)
    if not rows:
        return []
    L = [f"- 속도 ({ctx.get('vitals_date') or '측정일 미상'} · PageSpeed Insights):"]
    for dev in ("mobile", "desktop"):
        r = rows.get(dev)
        if not r:
            continue
        name = "모바일" if dev == "mobile" else "데스크톱"
        if r.get("error"):
            L.append(f"  - {name}: 못 쟀습니다 — {r['error']}")
            continue
        field = r.get("field_lcp_ms") is not None or r.get("field_cls") is not None
        if field:
            src = ("사이트 전체 값" if r.get("origin_fallback")
                   else "이 페이지의 실제 사용자 28일치")
            L.append(f"  - {name} 현장({src}): LCP {_ms(r.get('field_lcp_ms'))} · "
                     f"INP {_n(r.get('field_inp_ms'))}ms · CLS {_n(r.get('field_cls'))}"
                     + (f" · 구글 판정 {r['field_verdict']}" if r.get("field_verdict") else ""))
        else:
            L.append(f"  - {name} 현장: 실제 사용자 표본이 모자라 값이 없습니다"
                     " (검색이 보는 값도 그래서 없습니다).")
        L.append(f"  - {name} 실험실(지금 1회): 점수 {_n(r.get('lab_score'))}/100 · "
                 f"LCP {_ms(r.get('lab_lcp_ms'))} · CLS {_n(r.get('lab_cls'))} · "
                 f"TBT {_n(r.get('lab_tbt_ms'))}ms")
    L.append(f"  - 기준: LCP {_ms(scoring.LCP_GOOD_MS)} 이내 · INP {scoring.INP_GOOD_MS}ms "
             f"이내 · CLS {scoring.CLS_GOOD} 이내. 고친 뒤 바로 움직이는 것은 실험실 "
             "값이고, 현장 값은 28일이 지나야 따라옵니다.")
    return L


def _serp_top(o: dict, ctx: dict) -> list[str]:
    """이 검색어의 지금 검색결과 상위 — 우리가 방금 조회한 그 응답에서 나온다.

    이 표가 없던 동안 요청문은 "상위 페이지 2~3개의 제목을 붙여 넣으세요" 라고
    사람에게 시켰다. 제목은 수집본에 있었다 — collect_serp 가 내 순위만 빼고
    버렸을 뿐이다(db.write_serp_results 가 그것을 남긴다).
    """
    rows = (ctx.get("serp_top") or {}).get(str(o.get("target") or "")) or []
    if not rows:
        return []
    return [f"검색결과 상위 {len(rows)}자리 — 이 사람들과 같은 질문에 답해야 합니다:",
            *_table(["자리", "제목", "주소"],
                    [[f"{r['position']}위" + (" (내 페이지)" if r.get("is_own") else ""),
                      r.get("title"), r.get("url")] for r in rows]),
            "",
            "위 제목이 이 검색어에 실제로 걸리는 글의 제목입니다. 제목만으로 부족하면 "
            "아래 칸에 그 글들의 H2 목록을 붙여 넣어 주세요."]


def _site_facts(ctx: dict, url: str | None) -> list[str]:
    """사이트 전체를 봐야 아는 사실 — 제목 중복, 이 페이지로 들어오는 내부 링크.

    "새 title 3안" 을 시키면서 같은 title 을 쓰는 다른 페이지가 있다는 것을 안 주면
    새 안이 또 겹친다. "어느 글에서 이 페이지로 링크를 걸지" 를 시키면서 지금 어디서
    링크가 오는지를 안 주면 이미 있는 링크를 또 제안한다 — H2 에서 한 번 배운 실수다.
    """
    if not url:
        return []
    L = []
    rows = [r for r in ((ctx.get("crawl") or {}).get("issues") or [])
            if r.get("url") == url and r.get("kind") in SITE_ONLY_ISSUES]
    if rows:
        # 갈래 이름표는 페이로드에서 온다(정본: collect_crawl.ISSUE_KIND) — 여기서
        # 한국어 사본을 만들면 화면과 두 벌이 된다.
        names = ctx.get("crawl_kinds") or {}
        L += ["사이트 크롤이 이 주소에서 본 것 (한 장만 봐서는 모르는 것):"]
        L += _table(["문제", "세부"],
                    [[(names.get(r["kind"]) or [r["kind"]])[0], r.get("detail")]
                     for r in rows[:6]])
    # None = 크롤이 이 주소를 안 봤다, [] = 보고 링크가 없었다. 둘을 뭉치면
    # 크롤 범위 밖의 멀쩡한 페이지를 "고아" 라고 부른다.
    probe = (ctx.get("site_probe") or {}).get(url) or {}
    if probe.get("robots"):
        L.append(f"- robots.txt 가 이 주소를 막습니다 — `{probe['robots']}`. 색인 문제라면 "
                 "여기부터입니다(막힌 주소는 noindex 도 못 읽힙니다).")
    elif probe:
        L.append("- robots.txt: 이 주소를 막는 줄은 없습니다.")
    if probe.get("in_sitemap") is False:
        L.append("- 사이트맵에 이 주소가 없습니다. 크롤 시드는 사이트맵이었습니다 — "
                 "빠진 것이 의도인지 확인하세요.")
    elif probe.get("in_sitemap"):
        L.append("- 사이트맵에 이 주소가 있습니다.")
    ins = (ctx.get("crawl_inlinks") or {}).get(url)
    if ins:
        L += ["", f"이 페이지로 **들어오는** 내부 링크 {len(ins)}개 "
                  "(여기 있는 글에서 또 걸지 않습니다):"]
        L += _table(["링크를 건 글", "앵커"], [[r.get("from"), r.get("anchor")] for r in ins[:10]])
    elif ins is not None:
        L.append("- 이 페이지로 들어오는 내부 링크가 크롤에서 하나도 안 잡혔습니다"
                 "(고아 페이지). 링크를 걸 자리를 찾는 것이 첫 일입니다.")
    return L


# 이 페이지의 문제이긴 하지만 "있는 페이지 고치기" 의 일이 아닌 것 — 색인·모바일·
# 속도·언어는 기술 점검이 맡는다. 한 번호 목록에 섞으면 열세 개를 늘어놓고 세 개만
# 시키는 글이 된다(실제로 그렇게 나갔다). 기술 점검 요청문에서는 안 가른다.
TECH_TAGS = ("모바일", "언어", "hreflang", "속도", "robots", "canonical", "가져오기")


def _advice(a: dict | None, extra=(), *, split: bool = False) -> list[str]:
    adv = list((a or {}).get("advice") or []) + list(extra or [])
    if not adv:
        return []
    here = [x for x in adv if not (split and x["tag"] in TECH_TAGS)]
    aside = [x for x in adv if split and x["tag"] in TECH_TAGS]
    L = []
    if here:
        L += ["## 진단 — 고쳐야 할 것",
              *(f"{i + 1}. [{x['tag']}] 지금: {x['now']} → {x['fix']}"
                for i, x in enumerate(here)), ""]
    if aside:
        L += ["## 이 페이지에서 같이 눈에 띈 것 (이번 일은 아닙니다)",
              *(f"- [{x['tag']}] {x['now']}" for x in aside),
              "이것들은 글이 아니라 설정·속도 쪽이라 이번 요청문에서는 손대지 않습니다. "
              "답 마지막에 한 줄로 '따로 볼 것' 이라고만 적어 주세요.", ""]
    return L


# ── 종류별 근거 ──────────────────────────────────────────────────────────────
# 판정에 쓴 숫자를 표로. 각 함수는 (o, ctx, pages) 를 받아 줄 목록을 돌려준다 —
# 비면 근거 섹션이 안 생긴다(없는 것을 있는 척 하지 않는다).
def _find(rows, key: str, value) -> dict | None:
    v = str(value or "").strip().lower()
    if not isinstance(rows, list):        # 축이 개수·None 을 실을 때가 있다 — 행이 아니면 없는 것
        return None
    for r in rows:
        if str(r.get(key) or "").strip().lower() == v:
            return r
    return None


def _gsc_src(ctx: dict) -> str:
    d, p = ctx.get("gsc_date"), ctx.get("gsc_period")
    return f"구글 실적 {d}, 최근 {p}일 평균" if d and p else "구글 실적"


def _ev_striking(o, ctx, pages):
    # striking 은 4~20위 전부(band 로 갈린다) — striking_page2 는 그중 2페이지 개수다.
    r = _find(ctx.get("striking"), "query", o["target"])
    L = []
    if r:
        L.append(f"- {_gsc_src(ctx)}: 평균 {r['pos']}위 · 노출 {_n(r['imp'])} · 클릭 {_n(r['clk'])}"
                 + (f" · 1페이지까지 {r['gap']}칸" if r.get("band") == "page2" else
                    f" · 상단 3위권까지 {round(max(0.0, r['pos'] - 3), 1)}칸"))
        L.append("- 이 순위는 기간 평균 게재순위입니다. 노출된 순간들의 평균이라 지금 직접 "
                 "검색하면 안 보일 수 있습니다.")
    return L + _pages_table(pages)


def _ev_ctr(o, ctx, pages):
    r = _find(ctx.get("ctr_gaps"), "query", o["target"])
    L = []
    if r:
        L.append(f"- {_gsc_src(ctx)}: {r['position']}위 · 노출 {_n(r['impressions'])} · "
                 f"클릭 {_n(r['clicks'])} · CTR {r['actual_ctr']}% (이 순위의 기대치 "
                 f"{r['expected_ctr']}%) · 놓친 클릭 약 {_n(r['lost_clicks'])}")
    return L + _pages_table(pages)


def _ev_cannibal(o, ctx, pages):
    L = _pages_table(pages)
    if L:
        L.append("- 노출이 가장 큰 페이지가 정본 후보입니다. 표의 숫자로 판단하세요.")
    audits = ctx.get("page_audits") or {}
    rows = []
    for p in pages[:6]:
        a = audits.get(p.get("page"))
        if a and not a.get("error"):
            h1 = scoring._as_list(a.get("h1_json"))
            rows.append([p["page"], a.get("title") or "(없음)", " / ".join(h1) or "(없음)",
                         _n(a.get("words"))])
    if rows:
        L += ["", "페이지별 제목 — 검색 의도가 정말 같은지 여기서 가릅니다:"]
        L += _table(["내 페이지", "title", "H1", "본문 단어"], rows)
    return L


def _ev_decay(o, ctx, pages):
    r = _find(ctx.get("downs"), "query", o["target"])
    L = []
    if r:
        L.append(f"- 순위 {r.get('prev_pos', '—')}위 → {r['pos']}위 ({r['dpos']}칸) · 클릭 "
                 f"{r['dclk']:+} · 노출 {_n(r['imp'])} (구글 실적 {ctx.get('gsc_prev')} → "
                 f"{ctx.get('gsc_date')})")
    return L + _pages_table(pages)


def _ev_pseo(o, ctx, pages):
    L = _pages_table(pages)
    sibs = _siblings(o["target"], ctx)
    if sibs:
        L += ["", "같은 꼴로 보이는 검색어(낱말 2개 이상 겹침 — 축 후보):"]
        L += _table(["검색어", "노출", "클릭", "평균 순위"],
                    [[q, _n(s["imp"]), _n(s["clk"]), f"{s['pos']}위"] for q, s in sibs])
    return L


def _siblings(target: str, ctx: dict, limit: int = 8) -> list[tuple[str, dict]]:
    """query_pages 의 검색어 중 대상과 낱말 2개 이상 겹치는 것 — 템플릿 축의 재료.
    문자열 겹침일 뿐이라 '후보'라고만 말한다."""
    base = {t for t in scoring.tokens(target) if len(t) >= 2}
    if len(base) < 2:
        return []
    out = []
    for q, prs in (ctx.get("query_pages") or {}).items():
        if q == target or not prs:
            continue
        if len(base & {t for t in scoring.tokens(q) if len(t) >= 2}) >= 2:
            out.append((q, {"imp": sum(p.get("impressions") or 0 for p in prs),
                            "clk": sum(p.get("clicks") or 0 for p in prs),
                            "pos": prs[0].get("position")}))
    return sorted(out, key=lambda x: -x[1]["imp"])[:limit]


def _ev_device(o, ctx, pages):
    r = _find(ctx.get("device_gap"), "query", o["target"])
    L = []
    if r:
        L.append(f"- 모바일 {r['mobile_pos']}위 vs 데스크톱 {r['desktop_pos']}위 ({r['dpos']}칸 "
                 f"차이) · 모바일 노출 {_n(r['mobile_imp'])} · 모바일 CTR {r['mobile_ctr']}% vs "
                 f"데스크톱 {r['desktop_ctr']}%")
        L.append("- 같은 페이지·같은 검색어에서 기기만 다릅니다. 글이 아니라 모바일 화면·"
                 "속도가 원인일 가능성이 큽니다.")
    # 이 표가 없던 동안 이 요청문은 "모바일이 밀린다" 한 줄만 주고 원인을 대라고
    # 시켰다 — 규칙은 "위 근거에 없는 것은 짐작하지 않습니다" 인데. 기기 격차의
    # 원인 후보를 가르는 숫자가 바로 이것이다.
    vit = _vitals_rows(ctx, page_of(o, ctx))
    if vit.get("mobile") and vit.get("desktop"):
        m, d = vit["mobile"], vit["desktop"]

        def cell(row, col):
            v = row.get(col)
            if col.endswith("_ms"):
                return _ms(v)
            return _n(v)
        rows = [["LCP (현장)", cell(m, "field_lcp_ms"), cell(d, "field_lcp_ms"),
                 _ms(scoring.LCP_GOOD_MS)],
                ["INP (현장)", f"{_n(m.get('field_inp_ms'))}ms", f"{_n(d.get('field_inp_ms'))}ms",
                 f"{scoring.INP_GOOD_MS}ms"],
                ["CLS (현장)", _n(m.get("field_cls")), _n(d.get("field_cls")),
                 str(scoring.CLS_GOOD)],
                ["점수 (실험실)", _n(m.get("lab_score")), _n(d.get("lab_score")), "90"],
                ["LCP (실험실)", cell(m, "lab_lcp_ms"), cell(d, "lab_lcp_ms"),
                 _ms(scoring.LCP_GOOD_MS)],
                ["TBT (실험실)", f"{_n(m.get('lab_tbt_ms'))}ms", f"{_n(d.get('lab_tbt_ms'))}ms",
                 "200ms"]]
        L += ["", f"기기별 속도 ({ctx.get('vitals_date') or '측정일 미상'} · 같은 페이지):"]
        L += _table(["지표", "모바일", "데스크톱", "기준"], rows)
        if m.get("origin_fallback") or d.get("origin_fallback"):
            L.append("- 현장 값 일부는 이 페이지가 아니라 사이트 전체(오리진) 값입니다 — "
                     "이 페이지의 실제 사용자 표본이 모자랍니다.")
    return L + _pages_table(pages)


def _ev_index(o, ctx, pages):
    r = _find(ctx.get("index_issues"), "url", o["target"])
    if not r:
        return []
    said = " · ".join(x for x in (r.get("coverage_state"), r.get("verdict")) if x)
    return [f"- 구글 응답(URL 검사): {said or '—'} · 갈래: {r.get('bucket')}"]\
        + ([f"- 세부: {r['detail']}"] if r.get("detail") else [])


def _ev_coverage(o, ctx, pages):
    cl = str(o["target"]).split(":", 1)[-1]
    kws = (ctx.get("cluster_keywords") or {}).get(cl) or []
    if not kws:
        return []
    return ["이 주제로 추적 중인데 노출도 순위도 없는 키워드:",
            *_table(["키워드", "월 검색량"], [[k["keyword"], _n(k.get("volume"))] for k in kws[:15]])]


def _ev_ai(o, ctx, pages):
    r = _find(ctx.get("ai_by_prompt"), "prompt", o["target"])
    L = []
    if r:
        L.append(f"- AI {r.get('engines') or '—'} · 답변 {r['checks']}건 중 인용 {r['cited']}건, "
                 f"이름만 {r['mentioned']}건")
        doms = scoring._xai_doms(r.get("miss_domains"))
        if doms:
            L.append(f"- 대신 인용된 곳: {', '.join(doms)}")
        ans = (r.get("miss_answer") or "").strip()
        if ans:
            L += ["- AI 가 지금 하는 답변(발췌) — 여기 없는 것을 우리가 답해야 인용됩니다:",
                  *(f"  > {ln}" for ln in ans.splitlines() if ln.strip())]
    # 크롤러가 막혀 있으면 글을 고쳐도 안 읽힌다. 이 줄이 없으면 이 요청문과
    # AI 크롤러 차단 기회가 서로 모순되는 말을 한다.
    blocked = [r["bot"] for r in (ctx.get("ai_bots") or []) if r.get("rule")]
    if blocked:
        L.append(f"- **먼저 볼 것**: robots.txt 가 {', '.join(blocked)} 를 막고 있습니다. "
                 "그 크롤러를 쓰는 엔진에서는 무엇을 써도 인용되지 않습니다 — 글보다 "
                 "그 설정이 먼저입니다.")
    return L + _pages_table(pages)


def _ev_aio(o, ctx, pages):
    r = _find(ctx.get("ranks"), "keyword", o["target"])
    L = []
    if r:
        pos = f"{r['pos']}위" if r.get("pos") is not None else "순위 없음"
        L.append(f"- 실제 검색 결과: {pos}" + (f" (그 자리의 내 페이지: {r['url']})" if r.get("url") else "")
                 + " · 구글 AI 요약 있음, 내 링크 없음")
        if r.get("features"):
            L.append(f"- 검색결과 기능: {', '.join(map(str, r['features']))}")
    return L + _pages_table(pages)


def _ev_content_gap(o, ctx, pages):
    t = str(o["target"]).strip().lower()
    rows = [r for r in (ctx.get("kw_gap") or [])
            if str(r.get("keyword") or "").strip().lower() == t]
    L = _table(["경쟁 도메인", "그쪽 순위", "내 순위", "월 검색량", "갈래"],
               [[r["domain"], f"{r['position']}위",
                 f"{r['our_position']}위" if r.get("our_position") else "없음",
                 _n(r.get("volume")), r.get("kind")] for r in rows[:6]])
    return L + _pages_table(pages)


def _ev_crawl(o, ctx, pages):
    issues = ((ctx.get("crawl") or {}).get("issues") or [])
    rows = [r for r in issues if r.get("url") == o["target"]]
    names = ctx.get("crawl_kinds") or {}
    return _table(["문제", "심각도", "세부"],
                  [[(names.get(r["kind"]) or [r["kind"]])[0], r.get("severity"),
                    r.get("detail")] for r in rows[:8]])


def _ev_bl_broken(o, ctx, pages):
    rows = [r for r in (ctx.get("bl_links") or [])
            if r.get("url_to") == o["target"] and r.get("is_broken")]
    L = _table(["링크를 건 곳", "앵커", "도메인 지수", "dofollow"],
               [[r["url_from"], r.get("anchor"), _n(r.get("rank")),
                 "예" if r.get("dofollow") else "아니오"] for r in rows[:8]])
    if L:
        L.append("- 이 주소는 지금 열리지 않습니다(4xx/5xx). 링크는 살아 있고 페이지만 없습니다.")
    return L


def _ev_bl_prospect(o, ctx, pages):
    r = _find(ctx.get("bl_intersect"), "domain", o["target"])
    if not r:
        return []
    tg = [t.strip() for t in str(r.get("targets") or "").split(",") if t.strip()]
    L = [f"- 도메인 지수 {_n(r.get('rank'))} · 여기서 링크를 받는 경쟁사 {r.get('hits')}곳"
         + (f": {', '.join(tg)}" if tg else "")]
    L.append("- 경쟁사가 링크된 글이 어느 것인지는 수집하지 않았습니다. 그 글을 찾는 것이 "
             "첫 일입니다 — 아래 답의 형식 1번.")
    return L


def _ev_ai_bot(o, ctx, pages):
    """어느 줄이 막는지 + 나머지 봇은 어떤 상태인지.

    한 봇만 보여 주면 "이것만 열면 되나" 로 읽힌다. 같은 robots.txt 가 다른
    봇에게 무엇을 하고 있는지 한 표에 놓아야 열고 닫는 결정을 한 번에 한다.
    """
    rows = ctx.get("ai_bots") or []
    if not rows:
        return []
    L = [f"- robots.txt 판정: {len(rows)}개 크롤러 중 "
         f"{sum(1 for r in rows if r.get('rule'))}개가 막혀 있습니다."]
    L += _table(["크롤러", "지금", "막는 줄"],
                [[r["bot"], "차단" if r.get("rule") else "허용", r.get("rule") or "—"]
                 for r in rows])
    L.append("- 학습과 인용은 다른 봇일 수 있습니다(예: Google-Extended 는 제미나이 "
             "학습이고, 검색 색인의 Googlebot 과 별개입니다). 막는 것이 의도였다면 "
             "그렇다고 답해 주세요 — 여는 것이 늘 정답은 아닙니다.")
    return L


# AI 쪽 기회 — 고친 뒤 "AI 에서 온 방문"(collect_ga4 의 부가 조회)으로 루프가 닫히는 것.
_AI_VISIT_KINDS = ("ai_citation_gap", "aio_exposure")
assert set(_AI_VISIT_KINDS) <= set(scoring.ALL_KINDS)
# 요청문이 "어디를 보라"고 가리키는 화면·섹션 이름. 정본은 뷰 쪽이다(view-def 의 title,
# ai.html #ai-visits 의 h2) — 여기는 가리키기만 하고, test_seams 가 둘을 대조한다.
# 화면 이름을 바꾸고 이쪽을 안 고치면 요청문이 없는 화면을 보라고 한다.
SCREEN_TITLES = {"ai": "AI 인용", "rank": "순위 추적"}
AI_VISITS_SECTION = ("ai-visits", "AI 에서 온 방문")


def _ai_visits(o: dict, ctx: dict, url: str | None) -> tuple[list[str], list[str]]:
    """AI 종류 요청문에만 붙는 두 조각 — (근거에 더할 줄, '고친 뒤 볼 것' 줄).

    근거 줄은 그 페이지로 AI 답변을 타고 들어온 방문이 **있을 때만** 낸다(0 을 근거로
    늘어놓지 않는다). 뒤 조각은 늘 낸다: 인용을 고치고 끝나면 측정 → 수정 → 재측정
    루프가 AI 쪽에서만 안 닫힌다. 페이지 짝은 GA4 매칭 규칙 그대로(경로만 —
    collect_ga4 모듈 docstring)다.
    """
    if o.get("kind") not in _AI_VISIT_KINDS:
        return [], []
    from urllib.parse import urlsplit

    meta = ctx.get("ai_referral_meta")
    pages = ctx.get("ai_referral_pages") or []
    ev: list[str] = []
    path = (urlsplit(url).path or "/") if url else None
    row = next((p for p in pages if path and p.get("page") == path), None)
    if meta and row and row.get("sessions"):
        srcs = ", ".join(f"{h} {_n(n)}" for h, n in
                         sorted((row.get("sources") or {}).items(), key=lambda x: -x[1]))
        ev.append(f"- AI 답변의 링크를 타고 이 페이지로 들어온 방문: 세션 {_n(row['sessions'])}"
                  f" · 키 이벤트 {_n(row.get('key_events'))}"
                  + (f" ({srcs})" if srcs else "")
                  + f" — GA4 {meta.get('date')} 기준 최근 {meta.get('period_days')}일")
    where = "이 페이지" if url else "새로 올린 페이지"
    if o["kind"] == "aio_exposure":
        # 구글 AI 요약에서 온 클릭은 GA4 에서 google / organic 이라 AI 유입으로 안 갈린다 —
        # 여기서 "AI 방문이 느는지 보라" 고 하면 영영 안 느는 수를 보게 한다.
        after = ["구글 AI 요약에서 온 클릭은 GA4 에서 구글 유기 검색으로 잡혀 따로 갈리지 "
                 f"않습니다. 고친 뒤에는 [{SCREEN_TITLES['rank']}] 화면에서 이 검색어의 AI 요약에 "
                 "내 링크가 붙는지와 구글 실적의 클릭을 봅니다."]
    elif meta:
        after = [f"다음 GA4 수집에서 [{SCREEN_TITLES['ai']}] 화면의 '{AI_VISITS_SECTION[1]}'에 "
                 f"{where}의 세션이 느는지 봅니다. 인용이 붙어도 이 수가 그대로면 답변이 "
                 "링크를 누를 이유를 주지 못한 것입니다."]
    else:
        after = ["다음 인용 확인에서 이 질문에 우리 링크가 붙는지 봅니다. GA4 를 연결하면 "
                 "그 링크를 타고 실제로 들어온 방문까지 잽니다."]
    return ev, after


EVIDENCE: dict[str, Callable] = {
    "striking_distance": _ev_striking, "ctr_gap": _ev_ctr, "cannibalization": _ev_cannibal,
    "rank_decay": _ev_decay, "pseo_pattern": _ev_pseo, "device_gap": _ev_device,
    "index_blocked": _ev_index, "coverage": _ev_coverage, "ai_citation_gap": _ev_ai,
    "aio_exposure": _ev_aio, "content_gap": _ev_content_gap, "crawl_issue": _ev_crawl,
    "backlink_broken": _ev_bl_broken, "backlink_prospect": _ev_bl_prospect,
    "ai_bot_blocked": _ev_ai_bot,
}
assert set(EVIDENCE) == set(scoring.ALL_KINDS)

# 대상 줄의 이름 — 검색어·질문·주소·도메인·주제는 다른 것이다. 한 낱말("페이지")로
# 부르면 연락문 요청문이 남의 도메인을 "고칠 페이지"라고 부른다(실제로 그랬다).
_TARGET_NOUN = {
    "ai_citation_gap": "질문 (챗봇에 실제로 물은 문장)",
    "index_blocked": "주소", "crawl_issue": "주소", "backlink_broken": "깨진 주소 (링크가 향하는 곳)",
    "backlink_prospect": "연락할 도메인", "coverage": "주제 (추적 키워드 묶음)",
    "ai_bot_blocked": "막힌 AI 크롤러 (robots.txt 의 User-agent)",
}


def _target_lines(o: dict, url: str | None, shape: str) -> list[str]:
    kind = o["kind"]
    t = str(o["target"])
    if kind == "coverage":
        t = t.split(":", 1)[-1]
    L = ["## 대상", f"- {_TARGET_NOUN.get(kind, '검색어')}: {t}"]
    if url and url != t:
        L.append(f"- {'정본 후보 페이지' if shape == 'consolidate' else '페이지'}: {url}")
    elif not url and shape == "new_content":
        L.append("- 페이지: 없음 — 이 검색어로 걸린 내 페이지가 아직 없어서 새로 씁니다.")
    elif not url and _shows_page(shape):
        # 고칠 페이지를 모르는 채로 고치라고 할 수는 없다 — 사람이 채울 자리를 둔다.
        L.append("- 페이지: 아직 모릅니다 — 이 검색어로 걸린 내 페이지가 수집본에 없습니다. "
                 "고칠 페이지를 직접 적어 주세요: [URL]")
    why = " — ".join(x for x in (o.get("label"), o.get("reasoning")) if x)
    if why:
        L.append(f"- 왜 걸렸나: {why}")
    return L + [""]


# 대상 자체가 주소인 종류 — 크롤 이슈는 '/path' 처럼 상대 경로로도 온다. "http" 로
# 시작하느냐로 가르면 그 주소를 "아직 모르는 페이지"라고 부른다(실제로 그랬다).
URL_KINDS = frozenset({"index_blocked", "crawl_issue", "backlink_broken"})


def page_of(o: dict, ctx: dict) -> str | None:
    """이 기회에서 손댈 페이지 — 대상이 주소면 그것, 검색어면 노출이 가장 큰 페이지."""
    t = str(o.get("target") or "")
    if o.get("kind") in URL_KINDS or t.startswith("http"):
        return t
    pages = (ctx.get("query_pages") or {}).get(t) or []
    return pages[0].get("page") if pages else None


def build(o: dict, ctx: dict) -> dict:
    """기회 한 건 → {"shape", "body"}. body 는 '만들어 줄 것'까지, 꼬리는 tails() 가 댄다.

    ctx 는 dashboard.gather() 가 모은 페이로드 그대로다(query_pages·page_audits·각 축의
    행). 여기서 DB 를 읽지 않는다 — 화면이 보는 것과 요청문이 말하는 것이 같아야 한다.
    """
    kind = o["kind"]
    url = page_of(o, ctx)
    pages = (ctx.get("query_pages") or {}).get(str(o.get("target") or "")) or []
    shape = shape_of(kind, gap_kind=o.get("gap_kind"), has_page=bool(url))
    s = SHAPES[shape]
    audit = (ctx.get("page_audits") or {}).get(url) if url else None
    play = o.get("play") or {}

    L = [INTRO_BY_KIND.get(kind) or s["intro"], ""]
    L += _target_lines(o, url, shape)
    visits, after = _ai_visits(o, ctx, url)   # AI 종류만 — 나머지는 빈 둘
    ev = EVIDENCE[kind](o, ctx, pages) + visits
    if ev:
        L += ["## 근거 (수집한 데이터)", *ev, ""]
    had_top = False
    if s["slot"]:                             # 상위와 비교해야 하는 일(고치기·새 글)만
        top = _serp_top(o, ctx)
        if top:
            L += ["## 지금 이 검색어의 검색결과 상위", *top, ""]
            had_top = True
    if _shows_page(shape) and url:
        ps = _page_state(audit, url)
        vit = _vitals_lines(ctx, url)
        if vit:
            ps = ps[:-1] + vit + [""] if ps and ps[-1] == "" else ps + vit
        L += ps
        sf = _site_facts(ctx, url)
        if sf:
            L += ["## 사이트 전체에서 본 이 주소", *sf, ""]
        if shape != "consolidate":            # 정리는 페이지 안을 안 고친다
            L += _advice(audit, scoring.vitals_advice(_vitals_rows(ctx, url).values()),
                         split=shape != "technical")
    if play.get("what"):
        L += ["## 상황", play["what"], ""]
    if play.get("acts"):
        L += ["## 이 상황에서 할 일", *(f"{i + 1}. {x}" for i, x in enumerate(play["acts"])), ""]
    want = play.get("deliver") or _deliver_from(
        audit, scoring.vitals_advice(_vitals_rows(ctx, url).values()) if url else ())
    L += ["## 만들어 줄 것", *(f"{i + 1}. {x}" for i, x in enumerate(want)), ""]
    if after:
        L += ["## 고친 뒤 볼 것", *after, ""]
    if s["slot"]:
        # 상위 목록을 이미 위에 줬으면 여기서 또 "제목과 H2 를 붙여 넣으세요" 라고
        # 하지 않는다 — 같은 부탁이 한 요청문에 두 벌이 된다.
        ask = ("위 상위 목록의 글들을 열어 H2 목록을 붙이면, '빠진 구간'을 짐작이 "
               "아니라 비교로 찾습니다. 제목은 이미 위에 있습니다."
               if had_top else s["slot"])
        L += ["## 있으면 붙여 넣을 것 (선택)", ask, "[여기에 붙여 넣기]", ""]
    return {"shape": shape, "body": "\n".join(L)}


def _deliver_from(audit: dict | None, extra=()) -> list[str]:
    tags = list(dict.fromkeys(
        x["tag"] for x in (list((audit or {}).get("advice") or []) + list(extra or []))))
    out = [DELIVER_BY_TAG[t] for t in tags if t in DELIVER_BY_TAG]
    return out or [DELIVER_DEFAULT]


def text(o: dict, ctx: dict, locale: str) -> str:
    """요청문 전문 — 화면이 이어 붙이는 것과 같은 글. 검사·CLI 가 쓴다."""
    b = build(o, ctx)
    return b["body"] + "\n" + tails(locale)[b["shape"]]


def attach(d: dict, locale: str) -> None:
    """gather() 가 모은 페이로드에 요청문을 싣는다 — 기회마다 brief, 프로젝트마다 한 벌의 꼴."""
    for o in d.get("opps") or []:
        o["brief"] = build(o, d)
    d["brief"] = shapes_payload(locale)


def _selfcheck() -> None:
    assert lang_label("ja-JP") == "일본어" and lang_label("en-US") == "영어"
    assert limits("ko-KR") == (scoring.TITLE_MAX_KO, scoring.DESC_MAX_KO)
    assert limits("en-GB") == (scoring.TITLE_MAX, scoring.DESC_MAX)
    assert shape_of("content_gap", gap_kind="weak") == "fix_page"
    assert shape_of("content_gap", gap_kind="missing") == "new_content"
    assert shape_of("ai_citation_gap", has_page=True) == "fix_page"
    assert shape_of("ai_citation_gap") == "new_content"
    assert shape_of("backlink_prospect") == "outreach"
    for k in scoring.ALL_KINDS:
        assert shape_of(k) in SHAPES
    for name, t in tails("ko-KR").items():
        assert "## 답의 형식" in t and "## 규칙" in t, name
    assert json.dumps(shapes_payload("ko-KR"), ensure_ascii=False)


if __name__ == "__main__":
    _selfcheck()
    print("brief ok")
