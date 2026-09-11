# ADR 0003 — 로컬 대시보드 서버는 stdlib `http.server` 로 둔다

## 상태

채택 (2026-09-11)

## 맥락

HTTP 서버가 두 벌이다. 호스팅은 `server/app.py`(FastAPI), 로컬 대시보드는
`skills/capture/scripts/dashboard.py` 의 `Handler`(stdlib `http.server`)다. 리포 안만
보면 같은 일을 두 프레임워크로 하는 중복처럼 보이고, 아키텍처 리뷰가 "한 프레임워크로
합치자"를 제안한다 — 2026-09-09 점검에서도 그 방향이 나왔다.

실제로는 두 서버를 쓰는 사람이 다르다.

- **로컬 대시보드의 소비자는 플러그인 사용자 기기다.** 필수 의존성은 `requests`·`yaml`
  둘뿐이고(`skills/setup/scripts/doctor.py` 의 `deps_core`), 로컬 대시보드는 그 위에
  **0개를 더한다.** 플러그인 설치본은 애초에 `server/` 없이 나간다 — 조립에 쓰는
  이스케이프(`htmlsafe.py`)를 `server/` 가 아니라 capture 쪽에 둔 것도 그 때문이다.
  FastAPI 로 바꾸면 대시보드 하나 열려고 모든 사용자가 `fastapi`+`uvicorn` 과 그 의존
  나무(컴파일된 `pydantic-core` 포함)를 깔아야 한다.
- **두 서버는 하는 일이 다르다.** 로컬은 `/api/setup/*` — 로컬 키 저장, 사이트별 로컬
  폴더, 개발 도구 실행, 원격 사이트 프록시 — 를 서빙하는데 이건 **호스팅에 있으면 안
  된다**. 호스팅은 OAuth·사이트 등록·CLI 토큰·GA4 속성을 서빙하는데 이건 로컬에 없다.
- **겹치는 부분은 이미 공유된다.** 두 서버가 함께 서빙하는 라우트는 `dashboard.ROUTES`
  표 하나에 있고, 로컬 `Handler` 와 호스팅 라우트가 같은 call 을 부른다.

같은 날 점검 문서가 `ROUTES` 를 두고 "이주가 33개 라우트 중 6개에서 멈췄다"고 적었다가
정정했다. 표가 작은 것은 덜 끝난 이주가 아니라 교집합이 작아서다.

## 결정

- 로컬 대시보드 서버는 **stdlib `http.server` 로 둔다.** FastAPI·uvicorn 을 플러그인 쪽
  의존성에 넣지 않는다.
- `dashboard.ROUTES` 에는 **두 서버가 함께 서빙하는 라우트만** 둔다. 한쪽 전용 라우트를
  표로 옮기지 않는다.
- FastAPI 의 기능(`Depends`·`exception_handler`·`app.routes`)은 호스팅 쪽에서 끝까지
  쓴다. 이 결정은 그걸 막지 않는다 — 막는 것은 로컬로 번지는 것뿐이다.

## 결과

- 두 서버의 짝은 검사가 지킨다. 화면·원격 클라이언트가 부르는 `/api/*` 가 서버에
  있는지는 `test_seams.py` 가 `app.routes` 로 대조하고(`/api/setup/*` 은 로컬 전용이라
  면제), 공유 로직은 `ROUTES` 한 벌이다.
- 호스팅 고유 동작(인증·테넌트 격리)은 `server/test_app.py` 가 FastAPI 스택
  (`TestClient`)으로 본다. `test_render.py` 는 `/d` 를 로컬 `Handler` 로 띄우지만, `/d` 는
  `assemble("hosted")` + 애드온이라 어느 서버에서 나가든 같은 바이트다.
- **다시 열 조건**: 플러그인이 다른 이유로 이미 FastAPI 를 필수로 요구하게 되거나,
  로컬 전용 라우트가 사라져 두 서버가 사실상 같은 것이 되면. 그 전까지 다음 리뷰는 이
  문서를 근거로 "한 프레임워크로 합치자"를 다시 올리지 않는다.
