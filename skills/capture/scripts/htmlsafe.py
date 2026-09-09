#!/usr/bin/env python3
"""문서에 값을 박아 넣을 때의 이스케이프 — 한 벌.

같은 관용구가 다섯 자리에 손으로 적혀 있었다: `json.dumps(..., ensure_ascii=False)`
뒤에 `.replace("</", "<\\/")`. 한 자리만 빼먹으면 값에 든 `</script>` 가 거기서
태그를 닫고 그 뒤가 통째로 마크업이 된다 — 화면은 반쪽만 서고 콘솔에도 검사에도
아무것도 안 남는다. 실제로 `<option>` 을 짓는 자리 하나는 이스케이프가 **아예
없었다**(지금 출처가 고정 상수라 안 터졌을 뿐이다). 빼먹기 쉬운 것은 함수로 좁힌다.

여기(capture 스크립트)에 두는 이유: 조립(dashboard.py)은 server/ 가 없는 설치본에서도
돌아야 한다. 그래서 server/pages.py 가 이 함수를 가져다 쓴다 — 반대 방향이 아니다.

self-check: python htmlsafe.py
"""
from __future__ import annotations

import html
import json


def js(value) -> str:
    """`<script>` 안에 박아도 안전한 JSON. `</` 가 그대로 들어가면 거기서 태그가 닫힌다.

    한글은 이스케이프하지 않는다(ensure_ascii=False) — 화면에 그대로 나와야 한다.
    """
    return json.dumps(value, ensure_ascii=False).replace("</", r"<\/")


def attr(value) -> str:
    """따옴표로 감싼 HTML 속성값·본문에 박는 문자열. `"` 까지 막는다.

    `<option value="{c}">{c} — {t}</option>` 처럼 값을 마크업 사이에 끼우는 자리가
    쓴다. 지금 출처가 고정 상수여도 붙인다 — 없는 것과 필요 없는 것은 다르다.
    """
    return html.escape("" if value is None else str(value), quote=True)


def _selfcheck() -> None:
    # </script> 를 값에 넣어도 태그가 안 닫힌다.
    assert js({"a": "</script><script>evil()"}) == \
        '{"a": "<\\/script><script>evil()"}', js({"a": "</script>"})
    assert "</" not in js(["</b>", {"</i>": "</u>"}]), js(["</b>"])
    assert js(["가"]) == '["가"]', "한글이 이스케이프됐다 — 화면에 그대로 나와야 한다"
    assert js([("a", "b")]) == '[["a", "b"]]', "튜플이 배열로 안 나간다"

    assert attr('x"><script>bad()</script>') == \
        "x&quot;&gt;&lt;script&gt;bad()&lt;/script&gt;", attr('x">')
    assert attr("a&b") == "a&amp;b" and attr("'") == "&#x27;", attr("a&b")
    assert attr("한국어 · 한국") == "한국어 · 한국", "한글이 이스케이프됐다"
    assert attr(None) == "", "None 이 'None' 으로 박힌다"

    print("htmlsafe: ok")


if __name__ == "__main__":
    _selfcheck()
