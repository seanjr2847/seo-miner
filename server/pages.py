"""화면 조립 — 문서에 이름 붙은 슬롯을 채운다.

마커 문법이 둘이다: `<!--USER-->` (server/*.html) 와 "첫 `<script>` 앞에 window.__X__ 를
끼워 넣기". 같은 연산이다 — fill() 이 앞을, data() 가 뒤를 맡는다.

`@@TERMS@@` 도 있었다: terms.js 를 조각마다 끼워 넣어, 렌더된 한국어를 정규식으로
되짚어 배포별 문구로 갈아치웠다. 이제 화면이 자기 문구를 직접 고른다
(dashboard.html 의 data-web / W). 표식은 서버가 data() 로 첫 스크립트 앞에 세운다.

HTML 은 편집기가 도와주는 자리에 둔다: 페이지는 server/, 뒤에 얹는 조각은
server/assets/. 파이썬 문자열에 JS 를 1,000 줄 박아 두면 어느 쪽도 도와주지 못한다.
"""
from __future__ import annotations

import html
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 두 마커 문법을 한 정규식으로 — 이름은 대문자·밑줄만 쓴다.
SLOT = re.compile(r"<!--([A-Z_]+)-->|@@([A-Z_]+)@@")
_FIRST_SCRIPT = re.compile(r"<script[\s>]")


def asset(name: str) -> str:
    return (ROOT / "server" / "assets" / name).read_text("utf-8")


def page(name: str) -> str:
    return (ROOT / "server" / name).read_text("utf-8")


def fill(doc: str, **slots: str) -> str:
    """`<!--NAME-->` 과 `@@NAME@@` 을 한 연산으로 채운다.

    문서에 없는 이름을 주면 터진다 — 마커 이름 오타는 조용히 빈 화면으로 나가고,
    그건 서버 로그에 아무 흔적도 안 남긴다. 안 채운 슬롯은 문서에 그대로 둔다
    (뒤에 얹는 조각이 나중에 채운다).
    """
    seen = set()

    def sub(m: re.Match) -> str:
        name = m.group(1) or m.group(2)
        if name not in slots:
            return m.group(0)
        seen.add(name)
        return slots[name]

    out = SLOT.sub(sub, doc)
    unused = sorted(set(slots) - seen)
    if unused:
        raise KeyError(f"문서에 없는 슬롯: {unused}")
    return out


def js(value) -> str:
    """`<script>` 안에 박아도 안전한 JSON. `</` 가 그대로 들어가면 거기서 태그가 닫힌다."""
    return json.dumps(value, ensure_ascii=False).replace("</", r"<\/")


def data(doc: str, **values) -> str:
    """페이지 스크립트가 읽을 값을 window.<이름> 으로 앞에 세운다.

    문서의 첫 `<script>` **앞**에 넣는다 — 그 스크립트가 파싱될 때 값이 이미 있어야 한다.
    슬롯 마커가 아니라 위치에 기대는 유일한 자리다: 문서 첫 스크립트가 값을 읽는 그
    스크립트라는 데 의존한다. server/app.html 에 `<script>` 는 하나뿐이라 지금은 맞고,
    없어지면 여기서 터진다(조용히 빠지지 않는다).
    """
    m = _FIRST_SCRIPT.search(doc)
    if not m:
        raise ValueError("문서에 <script> 가 없다 — 값을 실을 자리가 없다")
    block = ("<script>" + "".join(f"window.{k}={js(v)};" for k, v in values.items())
             + "</script>")
    return doc[:m.start()] + block + doc[m.start():]


def document(body: str, title: str = "seo-miner") -> str:
    return ('<!doctype html><html lang="ko"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            f"<title>{html.escape(title)}</title></head><body>{body}</body></html>")


def addon(name: str) -> bytes:
    """대시보드·보고서 뒤에 이어붙이는 조각. HTML 뒤에 bytes 로 붙으므로 bytes 다."""
    return asset(name).encode("utf-8")


def demo() -> None:
    doc = '<p><!--USER--></p><style>@@LEFT@@</style><script>var x=1;</script>'

    out = fill(doc, USER="<b>나</b>")
    assert "<b>나</b>" in out and "@@LEFT@@" in out, out     # 안 채운 슬롯은 그대로
    assert fill(doc, LEFT="T").count("T") == 1, "@@NAME@@ 을 못 채운다"
    try:
        fill(doc, NOPE="x")
        raise AssertionError("문서에 없는 슬롯이 조용히 통과했다")
    except KeyError:
        pass

    # </script> 를 값에 넣어도 태그가 안 닫힌다.
    assert js({"a": "</script><script>evil()"}) == \
        '{"a": "<\\/script><script>evil()"}', js({"a": "</script>"})
    assert js(["가"]) == '["가"]', "한글이 이스케이프됐다 — 화면에 그대로 나와야 한다"

    got = data(doc, __TAKEN__=["a"], __N__=3)
    assert got.index("window.__TAKEN__") < got.index("var x=1"), \
        "값이 페이지 스크립트보다 뒤에 실린다 — 읽는 쪽이 undefined 를 본다"
    assert 'window.__TAKEN__=["a"];window.__N__=3;' in got, got
    try:
        data("<p>없다</p>", __X__=1)
        raise AssertionError("스크립트 없는 문서에 값이 실렸다")
    except ValueError:
        pass

    assert "<title>a&lt;b</title>" in document("", "a<b"), "제목이 이스케이프 안 됐다"

    # 실제 문서 — app.html 의 슬롯 셋이 전부 살아 있는지.
    real = data(fill(page("app.html"), USER="U", SITES="S"), __TAKEN__=[])
    assert "<!--USER-->" not in real and "<!--SITES-->" not in real, "슬롯이 안 채워졌다"
    assert "window.__TAKEN__=[]" in real, "값이 안 실렸다"
    assert isinstance(addon("report.html"), bytes)
    # 렌더된 한국어를 되짚던 층은 걷어냈다 — 되살아나면 여기서 잡는다.
    for a in ("dash.html", "report.html"):
        assert b"@@TERMS@@" not in addon(a) and b"retitle(" not in addon(a),             f"{a} 에 용어 치환 층이 되살아났다 — 문구는 화면이 data-web/W 로 고른다"

    print("pages: ok")


if __name__ == "__main__":
    demo()
