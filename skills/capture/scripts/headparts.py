"""서버 화면의 머리 조각 — 토큰·서체·로고의 원본 하나.

랜딩·방침·404·입구(app/start)·대시보드가 각자 토큰 블록과 서체 링크와 로고를 사본으로
들고 있었다(사본 동일을 검사 102 가 글자 단위로 지켰다). 사본을 맞추는 대신 원본을
여기 하나 두고, 화면은 슬롯(`<!--FONTS-->` · `@@TOKENS@@` · `<!--LOGO-->`)만 적는다.

원본이 capture 쪽에 있는 이유는 htmlsafe.py 와 같다(docs/adr/0003, 0004): 로컬 대시보드는
server/ 없이 나가는데 그 조립(dashboard.py)도 토큰을 끼워야 한다. 서버는 pages.chrome()
으로 이 조각을 부른다. 표준 라이브러리만 쓴다.
"""
from __future__ import annotations

import re
from pathlib import Path

HEAD = Path(__file__).resolve().parent.parent / "templates" / "head"

# 로고 — "seo·miner" 에서 가운뎃점만 강조색(.mark b)이다. 글자는 여기 한 곳이다.
LOGO = "seo<b>·</b>miner"

# 랜딩·방침이 받는 IBM Plex Mono 스택. 대시보드·입구는 토큰의 --mono 를 쓴다.
MONO_PLEX = ('"IBM Plex Mono",ui-monospace,SFMono-Regular,Consolas,monospace')


def _read(name: str) -> str:
    return (HEAD / name).read_text("utf-8").replace("\r\n", "\n")


def tokens() -> str:
    """`/* tokens:begin */`~`/* tokens:end */` 한 블록 — 색·반경·그림자·글자 크기·서체 스택."""
    return _read("tokens.css").rstrip("\n")


def sans() -> str:
    """본문 서체 스택 — 토큰 블록의 --sans 값. 토큰 밖에서 서체만 필요한 화면(방침·404)이 쓴다."""
    m = re.search(r"--sans:([^;]+);", tokens())
    assert m, "tokens.css 에 --sans 가 없다"
    return m.group(1)


def fonts() -> str:
    return _read("fonts.html").rstrip("\n")


def fonts_plex() -> str:
    return _read("fonts_plex.html").rstrip("\n")


def slots() -> dict[str, str]:
    """pages.chrome 이 문서에 있는 이름만 골라 채우는 공용 슬롯 표."""
    return {"TOKENS": tokens(), "FONTS": fonts(), "FONTS_PLEX": fonts_plex(),
            "LOGO": LOGO, "SANS": sans(), "MONO_PLEX": MONO_PLEX}
