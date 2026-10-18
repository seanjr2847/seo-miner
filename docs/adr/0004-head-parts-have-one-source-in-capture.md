# ADR 0004 — 서버 화면의 머리(토큰·서체·로고)는 capture 쪽 원본 하나에서 끼운다

## 상태

채택 (2026-10-17)

## 맥락

화면 파일이 각자 머리를 사본으로 들고 있었다.

- 디자인 토큰 블록(`/* tokens:begin */`~`end`, 42줄)이 `dashboard.html`·`server/landing.html`·
  `server/assets/entry.html` 세 벌이었고, 검사 102 가 **글자 하나 안 다르게** 맞추는지
  지켰다. 맞추는 검사가 필요하다는 것 자체가 사본이라는 뜻이다.
- Pretendard 링크가 아홉 번, IBM Plex Mono 링크가 여러 번, 로고 `seo<b>·</b>miner` 가
  여섯 곳. 404 는 `server/app.py` 안에서 랜딩 html 의 `--sans` 를 정규식으로 긁어 왔고
  색 hex 를 인라인으로 적었다. 방침(`privacy.html`)은 `--sans`·`--mono` 를 다시 적었다.

`app.html`·`start.html` 은 이미 `ENTRY_HEAD` 슬롯 + `pages.fill()` 로 입구 머리 하나를
끼우고 있었다. 같은 방식을 넓히면 된다.

## 결정

- 원본은 **capture 쪽**에 둔다: `skills/capture/templates/head/`(`tokens.css`·`fonts.html`·
  `fonts_plex.html`)와 이를 읽는 `skills/capture/scripts/headparts.py`. 로컬 대시보드는
  `server/` 없이 나가고(ADR 0003, `htmlsafe.py` 를 capture 에 둔 것과 같은 이유) 그 조립
  (`dashboard._assemble`)도 토큰을 끼워야 하기 때문이다. 서버는 이미 capture 의 조립을 부른다.
  표준 라이브러리만 쓴다 — 빌드 도구·새 의존성 없음.
- 화면은 슬롯만 적는다: `@@TOKENS@@` · `<!--FONTS-->` · `<!--FONTS_PLEX-->` · `<!--LOGO-->` ·
  `@@SANS@@` · `@@MONO_PLEX@@`. 서버는 `pages.chrome()`(입구는 `pages.entry_page()`)이,
  로컬 대시보드는 `dashboard._assemble` 이 채운다. 사용자 값(USER·SITES)은 머리를 채운 **뒤에**
  채운다 — 순서가 거꾸로면 사용자가 쓴 글자 속 슬롯 이름까지 갈아치운다.
- 404 는 `server/notfound.html` 로 옮겼다(인라인 hex → 클래스). 서체 스택은 토큰의
  `--sans` 에서 `headparts.sans()` 로 파생한다(정규식으로 랜딩을 긁지 않는다).
- **팔레트가 다른 문서는 합치지 않는다.** 방침·404 의 색(`#E6E9E4` 판)은 랜딩 토큰과 값이
  다르고, 합치는 것은 디자인 결정이다. 없앤 것은 서체 스택·서체 링크·로고의 사본뿐이다.
- 검사 102 를 "사본 동일"에서 "원본 하나, 화면은 슬롯만"으로 바꿨다: 화면에 토큰 마커·
  토큰 색·`--sans` 값·서체 URL·로고 글자가 직접 있으면 FAIL, 조립 결과에는 원본이 정확히
  한 번 실린다. 일부러 깨서 FAIL 나는 것을 확인했다.

## 결과

- 토큰·서체 버전·로고를 바꾸는 곳이 한 곳이다. 조립본은 이전과 같다(대시보드는 바이트
  동일, 랜딩·방침·입구는 머리 주석·서체 링크 순서만 다르다).
- 404 와 방침의 서체 링크는 이제 막지 않는 방식(`media=print` + onload)이다. 이전 404 는
  평범한 stylesheet 링크였다.
- **남은 사본 둘**(이 결정이 다루지 않은 자리): `dashboard.html` 의 Pretendard 링크(셸 파일이라
  이 변경에서 안 건드렸다 — 검사 102 가 주소가 원본과 같은지만 지킨다)와 `dash.html`·
  `dashboard.html:1744` 의 로고/Plex 링크. 셸 쪽 정리가 끝나면 슬롯으로 옮긴다.
