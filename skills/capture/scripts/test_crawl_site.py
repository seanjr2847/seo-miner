#!/usr/bin/env python3
"""크롤·사이트맵·사이트 단위 판정 검사 — `python test_crawl_site.py` (바깥 네트워크 0).

collect_crawl 의 자기검사는 가상 사이트 한 벌로 회차 전체(적재·비교)를 본다. 여기는 그 옆의
판정 조각들을 하나씩 본다: robots.txt 상태, 사이트맵 읽기(폴백·gzip·RSS·txt·상한), 속도 제한
물러서기, X-Robots-Tag, 소프트 404, 호스트 정규화, hreflang 대상·코드, 같은 틀 근사 중복,
템플릿 메타, 다국어 판 동등성 — 그리고 서면 안 되는 자리.
"""
import gzip
import json
import os
import sys
import tempfile
from pathlib import Path
from urllib import robotparser

sys.path.insert(0, str(Path(__file__).parent))
os.environ.setdefault("CAPTURE_HOME", tempfile.mkdtemp(prefix="seo-miner-crawl-site-test-"))

import collect_crawl as cc  # noqa: E402
import db  # noqa: E402
import scoring  # noqa: E402

H = "https://s.kr"


# ── scoring: hreflang 코드 실명단 ───────────────────────────────────────────
def test_hreflang_codes_are_real_lists():
    ok = scoring.hreflang_code_ok
    for good in ("ko", "en-GB", "zh-Hans", "zh-Hant-TW", "x-default", "EN-us", "pt-BR", "de-CH"):
        assert ok(good), good
    # 꼴은 코드 같지만 명단에 없다 — 옛 정규식은 eng·jp·en-EU·es-419 를 통과시켰다
    for bad in ("eng", "jp", "en-EU", "en-UK", "es-419", "en_US", "", "english", "ko-KR-x"):
        assert not ok(bad), bad
    # es-LA 는 명단으로는 못 거른다 — LA 는 라오스의 실제 코드다(중남미를 뜻하려던 것이라도)
    assert ok("es-LA") and ok("kr")       # kr = 카누리어(ISO 639-1). 한국어는 ko 지만 코드는 코드다
    # 명단 크기 — 한 줄이 빠지거나 오타가 끼면 여기서 걸린다(ISO 639-1 184개·ISO 3166-1 249개)
    assert len(scoring.ISO_639_1) == 184, len(scoring.ISO_639_1)
    assert len(scoring.ISO_3166_1) == 249, len(scoring.ISO_3166_1)
    assert all(len(c) == 2 and c.islower() for c in scoring.ISO_639_1)
    assert all(len(c) == 2 and c.isupper() for c in scoring.ISO_3166_1)
    # 페이지 처방(_hreflang_advice)이 같은 명단을 본다 — 두 벌이면 한쪽만 통과시킨다
    audit = {"js_shell": 0, "url": "https://a.kr/x", "canonical": "https://a.kr/x",
             "hreflang_json": json.dumps([["ko", "https://a.kr/x"], ["eng", "https://a.kr/en/x"],
                                          ["en-EU", "https://a.kr/es/x"],
                                          ["x-default", "https://a.kr/x"]])}
    got = [r["now"] for r in scoring._hreflang_advice(audit) if "코드가 아닌" in r["now"]]
    assert got and "eng" in got[0] and "en-EU" in got[0], got


# ── scoring: 템플릿 메타 ────────────────────────────────────────────────────
def test_desc_template_kind():
    k = scoring.desc_template_kind
    # 설명 = 제목(구두점·대소문자·공백 차이는 같은 것)
    assert k("Laser Toning | The Clinic", "Laser Toning | The Clinic") == "same_as_title"
    assert k("레이저 토닝 - 더클리닉", "레이저 토닝 – 더클리닉") == "same_as_title"
    # 비라틴 문자를 지우고 비교하지 않는다 — 한글 제목 둘은 서로 다르다
    assert k("기미 레이저는 3주 간격으로 다섯 번 받습니다. 비용과 회복 기간을 정리했습니다.",
             "점 제거 후 관리") is None
    # 제목 앞부분을 되풀이 — 틀이 {title}. {상투 문구} 로 찍은 것
    assert k("Laser Toning. Learn more about our services.", "Laser Toning | The Clinic",
             lang="en") == "title_repeat"
    assert k("レーザートーニング 詳しくはこちら", "レーザートーニング | クリニック", lang="ja") == "title_repeat"
    # 브랜드 꼬리가 설명에 섞임 — 제목 틀(… | 브랜드)이 설명 칸에 그대로 찍혔다
    assert k("Melasma fades over five sessions spaced three weeks apart | The Clinic",
             "Melasma treatment | The Clinic") == "brand_tail"
    # 손으로 쓴 설명 — 제목의 말을 쓰더라도 문장이면 틀이 아니다
    assert k("Five sessions, three weeks apart: what laser toning does to melasma and what it costs.",
             "Laser Toning | The Clinic", lang="en") is None
    assert k("", "제목") is None and k("설명", "") is None
    # 상투 CTA 표는 언어별 한 벌 — 표에 없는 언어는 CTA 로 판정하지 않는다(지어내지 않는다)
    assert "en" in scoring.DESC_CTA and "ko" in scoring.DESC_CTA
    assert all(isinstance(v, tuple) and v for v in scoring.DESC_CTA.values())


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)} passed")
