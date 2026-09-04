#!/usr/bin/env python3
"""요청문 — 기회 한 건을 Claude·ChatGPT 에 붙여 넣을 브리프로 세운다.

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
        form=["산출물마다 `## 산출물 이름` 소제목을 답니다. 요청한 순서대로.",
              "안이 여럿인 것(title·meta description)은 표로: 안 | 글자 수 | 검색어가 "
              "들어간 자리 | 이 안을 고른 이유 한 줄.",
              "본문에 보탤 구간은 H2 제목마다 그 아래에서 답할 내용 한 줄과 근거로 쓸 "
              "출처(이 페이지 안의 문장, 또는 [확인 필요]).",
              "마지막에 `## 바꾼 것` 표: 진단 항목 | 전 | 후. 진단에 없는 것을 바꿨으면 "
              "왜 바꿨는지 한 줄."],
        rules=["사실은 위 '지금 이 페이지 상태'와 '근거'에 있는 것만 씁니다. 수치·후기·"
               "효능·수상 이력을 지어내지 않습니다. 모르는 것은 [확인 필요]로 남깁니다.",
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
              "발행 뒤 내부 링크 표: 어느 글에서 | 앵커 텍스트 | 넣을 자리."],
        rules=["경쟁 페이지의 문장·구성을 그대로 옮기지 않습니다. 같은 질문에 답하되 "
               "순서와 관점은 우리 것으로.",
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
              "리다이렉트·canonical 은 적용할 코드나 설정 예시를 코드 블록으로. 스택을 "
              "모르면 [스택 확인] 이라 쓰고 가장 흔한 두 경우의 예시를 줍니다.",
              "적용 뒤 확인: 무엇을 어디서 보면 된 것인지 순서대로."],
        rules=["근거는 위 표의 숫자입니다. 감으로 정본을 고르지 않습니다. 숫자가 비슷하면 "
               "그렇다고 말하고 검색 의도로 가릅니다.",
               "내용을 지우자고 하지 않습니다. 합칠 때는 문단을 옮기는 것까지만.",
               "리다이렉트 사슬(A→B→C)을 만들지 않습니다. 이미 리다이렉트인 주소는 최종 "
               "주소로 바로 보냅니다.",
               "홈으로 몰지 않습니다. 가장 가까운 주제의 페이지로 보냅니다."],
        slot="", limits=False),
    "technical": dict(
        label="기술 점검",
        intro="아래 주소의 기술 문제를 잡아 주세요. 글의 내용은 손대지 않습니다 — 색인·"
              "크롤·모바일 화면처럼 검색엔진이 페이지에 닿는 길을 고치는 일입니다.",
        form=["점검 표: 항목 | 확인하는 방법(어디서 무엇을 보나) | 지금 값 | 고칠 값.",
              "고칠 값이 코드·설정이면 코드 블록으로. 파일 경로·태그 위치까지 적습니다.",
              "손대는 순서: 무엇을 먼저 고쳐야 다음 것이 뜻이 있는지.",
              "고친 뒤 확인: 어디서(Search Console·브라우저·curl) 무엇을 보면 고쳐진 "
              "것인지."],
        rules=["위 근거에 없는 것은 짐작하지 않습니다. 원인 후보가 여럿이면 후보와 가르는 "
               "방법을 나란히 적습니다.",
               "스택(CMS·프레임워크·호스팅)을 모르면 [스택 확인] 이라 쓰고, 가장 흔한 두 "
               "경우의 예시를 줍니다.",
               "본문 문장을 고치자고 하지 않습니다. 구조·설정·자원 크기까지만.",
               "요청한 주소만 봅니다. 사이트 전체 재구성을 제안하지 않습니다."],
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
        slot="", limits=False),
}
assert tuple(SHAPES) == SHAPE_NAMES

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
    """꼴별 꼬리(답의 형식 + 규칙) — 프로젝트마다 한 벌. 언어·길이 기준이 여기 들어간다."""
    lang = lang_label(locale)
    t_max, d_max = limits(locale)
    out = {}
    for name, s in SHAPES.items():
        L = ["## 답의 형식"]
        L += [f"{i + 1}. {x}" for i, x in enumerate(s["form"])]
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
    L = [f"## 지금 이 페이지 상태 ({a.get('checked_date') or '점검일 미상'} 직접 확인)",
         f"- title: {title or '(없음)'}" + (f" — {len(title)}자" if title else ""),
         f"- meta description: {desc or '(없음)'}" + (f" — {len(desc)}자" if desc else ""),
         f"- H1: {' / '.join(h1) if h1 else '(없음)'}"]
    if h2:
        shown = h2[:12]
        L.append(f"- H2 ({len(h2)}개): " + " / ".join(shown)
                 + (f" … 외 {len(h2) - len(shown)}개" if len(h2) > len(shown) else ""))
    else:
        L.append("- H2: (없음)")
    L.append(f"- 본문 길이: {_n(a.get('words'))}단어")
    L.append(f"- 구조화 데이터: {', '.join(sc) if sc else '(없음)'}")
    if a.get("canonical"):
        L.append(f"- canonical: {a['canonical']}")
    if a.get("robots"):
        L.append(f"- meta robots: {a['robots']}")
    links = []
    if isinstance(a.get("internal_links"), int):
        links.append(f"내부 링크 {a['internal_links']}개")
    if isinstance(a.get("external_links"), int):
        links.append(f"외부 링크 {a['external_links']}개")
    if isinstance(a.get("images"), int):
        links.append(f"이미지 {a['images']}개" + (f" (alt 없음 {a['images_no_alt']}개)"
                                                 if a.get("images_no_alt") else ""))
    if links:
        L.append("- " + " · ".join(links))
    L.append("")
    return L


def _advice(a: dict | None) -> list[str]:
    adv = (a or {}).get("advice") or []
    if not adv:
        return []
    return ["## 진단 — 고쳐야 할 것",
            *(f"{i + 1}. [{x['tag']}] 지금: {x['now']} → {x['fix']}" for i, x in enumerate(adv)),
            ""]


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
    return _table(["문제", "심각도", "세부"],
                  [[r["kind"], r.get("severity"), r.get("detail")] for r in rows[:8]])


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


EVIDENCE: dict[str, Callable] = {
    "striking_distance": _ev_striking, "ctr_gap": _ev_ctr, "cannibalization": _ev_cannibal,
    "rank_decay": _ev_decay, "pseo_pattern": _ev_pseo, "device_gap": _ev_device,
    "index_blocked": _ev_index, "coverage": _ev_coverage, "ai_citation_gap": _ev_ai,
    "aio_exposure": _ev_aio, "content_gap": _ev_content_gap, "crawl_issue": _ev_crawl,
    "backlink_broken": _ev_bl_broken, "backlink_prospect": _ev_bl_prospect,
}
assert set(EVIDENCE) == set(scoring.ALL_KINDS)

# 대상 줄의 이름 — 검색어·질문·주소·도메인·주제는 다른 것이다. 한 낱말("페이지")로
# 부르면 연락문 요청문이 남의 도메인을 "고칠 페이지"라고 부른다(실제로 그랬다).
_TARGET_NOUN = {
    "ai_citation_gap": "질문 (챗봇에 실제로 물은 문장)",
    "index_blocked": "주소", "crawl_issue": "주소", "backlink_broken": "깨진 주소 (링크가 향하는 곳)",
    "backlink_prospect": "연락할 도메인", "coverage": "주제 (추적 키워드 묶음)",
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
    ev = EVIDENCE[kind](o, ctx, pages)
    if ev:
        L += ["## 근거 (수집한 데이터)", *ev, ""]
    if _shows_page(shape) and url:
        L += _page_state(audit, url)
        if shape != "consolidate":            # 정리는 페이지 안을 안 고친다
            L += _advice(audit)
    if play.get("what"):
        L += ["## 상황", play["what"], ""]
    if play.get("acts"):
        L += ["## 이 상황에서 할 일", *(f"{i + 1}. {x}" for i, x in enumerate(play["acts"])), ""]
    want = play.get("deliver") or _deliver_from(audit)
    L += ["## 만들어 줄 것", *(f"{i + 1}. {x}" for i, x in enumerate(want)), ""]
    if s["slot"]:
        L += ["## 있으면 붙여 넣을 것 (선택)", s["slot"], "[여기에 붙여 넣기]", ""]
    return {"shape": shape, "body": "\n".join(L)}


def _deliver_from(audit: dict | None) -> list[str]:
    tags = list(dict.fromkeys(x["tag"] for x in ((audit or {}).get("advice") or [])))
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
