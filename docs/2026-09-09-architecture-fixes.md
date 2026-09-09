# 고칠 것들 — 2026-09-09 아키텍처 점검

`server/app.py`(HTTP), `dashboard.py`+템플릿(화면 조립), `run_all`/`collector`/`worker`
(수집 런) 세 갈래를 훑고 나온 목록이다. 근거는 전부 `파일:줄` 로 적었다 — 문서가
정본을 베끼지 않게, 개수·단계 이름·라우트 목록은 **가리키기만** 한다.

우선순위는 "지금 사용자에게 잘못된 것이 나가고 있는가" 순이다.

---

## 1. `StageResult` 끝맺음을 한 곳에서 만든다 — **먼저 한다**

### 무엇이 틀렸나

같은 `StageResult` 를 두 모듈이 만드는데 "건너뜀"의 `ok` 를 반대로 적는다.

| 만드는 곳 | 건너뜀일 때 |
|---|---|
| `collector.py:319` (`Stage.skip`) | `ok=False, skipped=True` |
| `run_all.py:300·305·311·315` (`_run_stage`) | `ok=True, skipped=True` |

판정하는 쪽은 `ok` 만 본다:

```python
# run_all.py:418-421
def chain_rc(results) -> int:
    """0: 모든 단계 성공(건너뜀 포함) / 1: 하나 이상 실패."""
    return 1 if any(not r.ok for _, r in results) else 0
```

**docstring 이 의도를 적어놨고 구현이 그걸 어긴다.** 의견이 아니라 확정된 계약 위반이다.

### 결과

`st.skip()` 호출은 11개 모듈 20곳이고, 대부분 "아직 설정 안 됨"류다 — GA4 속성 없음
(`collect_ga4.py:246`), 활성 키워드 없음(`collect_serp.py:105`), 잴 페이지 없음
(`collect_vitals.py:148`), 사이트 주소 없음(`collect_crawl.py:471`). 전부 `ok=False` 라
체인이 실패로 끝난다:

```
st.skip() → chain_rc=1 → worker.py:219-222 failed → store.mark_done(ok=False)
                                                  → mailer.run_failed  (worker.py:253)
```

**GA4 를 안 붙인 사이트는 정상인데도 매 런마다 실패로 기록되고 실패 메일이 나간다.**

### 고침

- `skipped` 를 `ok` 와 직교시킨다. 끝맺음은 `collector` 의 생성자 네 개
  (`done`/`skip`/`fail`/`partial`)로만 만들고, `run_all` 의 `StageResult` 직접 생성
  3곳을 지운다.
- `chain_rc` 는 `not r.ok and not r.skipped` 를 본다.

### 검사

`test_collectors.py` 에 "건너뜀만 있는 체인의 `chain_rc` 는 0" 을 못 박는다.
**일부러 `Stage.skip` 을 `ok=False` 로 되돌려 FAIL 나는 것까지 확인한다.**

### 크기

작다. 새 의존성 0, 빌드 단계 0.

---

## 2. FastAPI 를 끝까지 쓴다

> 질문에 대한 직답: 이 리포는 Flask 를 안 쓴다 — **FastAPI 다**(`requirements.txt`).
> Flask 로 바꾸는 건 옆걸음이고 ASGI·`Depends`·`TestClient` 를 잃는다.
> 진짜 공백은 **이미 깔린 FastAPI 를 절반만 쓰고 있는 것**이다.

### 지금

`server/app.py` 는 33개 라우트에 핸들러 코드 497줄인데, 그중 **약 140줄(28%)이
기계적 잔여**다. handler 2/3 은 진짜 일이 한 줄이고 나머지가 전부 되풀이다.

| 되풀이되는 것 | 횟수 | 흡수할 FastAPI 기능 |
|---|---|---|
| `uid = _require_uid(request)` | 27 | `Depends` |
| `with store.session(...)` (5가지 조합) | 28 | `Depends` |
| `c = t.brain()` + `try/finally: c.close()` | 9 (36줄) | `Depends` (yield) |
| `await request.json()` + `project` 꺼내기 | 11 (20줄) | 요청 모델 |
| `except db.ProjectNotFound → HTTPException(404, str(e))` | 16 | `@app.exception_handler` |
| `detail=str(e)` | 20 | 위와 같음 |

### 고침

1. **`Depends` 로 인증·테넌트·brain 을 흡수한다.** `_require_uid` → `Depends(current_uid)`,
   `store.session(...)` + `t.brain()` → `Depends(tenant_brain)` (yield 로 닫는다).
   빠뜨릴 자리가 없어지고, `demo()` 가 20개 경로에 401 을 손으로 넣어 확인하는
   `app.py:1191-1203` 이 필요 없어진다.
   - 모양은 이미 증명돼 있다: `_dispatch_dep`/`_kick_dep`(`app.py:164-169`)이
     `dependency_overrides` 로 대체 가능한 유일한 seam 이고, 그 이유가 `:162-163` 에
     적혀 있다("`globals()` 수술 대신").
2. **예외를 한 곳에서 사상한다.** `db.ProjectNotFound` → 404, `sqlite3.IntegrityError`
   → 409 를 `@app.exception_handler` 로 옮긴다. `_not_configured`(`app.py:106-115`)가
   이미 그 자리를 하나 갖고 있다 — 거기에 합류시킨다.
3. **`app.routes` 를 라우트 정본으로 쓴다.** 아래 4번과 같은 작업이다.
4. **`demo()` 를 `app.py` 밖으로 뺀다.** 467줄, 파일의 29%가 테스트다
   (`app.py:1148` ~ 끝). `TestClient` 를 쓰는 유일한 검사인데 프로덕션 모듈 안에 산다.
   `run_checks.py:36-38` 이 `def demo(` 규약으로 줍고 있으므로 옮길 때 그 규약을 지킨다.

### 하지 않을 것

- 응답 모델(`response_model`) 전면 도입 — 3번만으로 이음매 값은 다 나온다.
- Flask 이관.

### 크기

중간. 새 의존성 0. 라우트를 한꺼번에 고치지 말고 `Depends` 를 만든 뒤 한 번에 몇
개씩 옮긴다. `demo()` 가 실제로 401·404 를 확인하므로 이주 중에 계속 돌린다.

---

## 3. 실패가 이음매를 건널 때마다 얇아지는 것을 멈춘다

### 무엇이 틀렸나

실패는 발생 즉시 문자열로 납작해지고, 건널 때마다 더 깎인다. 소실 지점:

| # | 자리 | 잃는 것 |
|---|---|---|
| 1 | `collector.py:307-308` | `first_error` 만 보관 — 나머지 오류 전부 |
| 2 | `collector.py:316` | 100자 절단 |
| 3 | `collector.py:347-348` | 부분 실패 사유 200자 절단, 그래도 `ok=True` |
| 4 | `run_all.py:322-323` | traceback 폐기 (`logging` 사용처가 리포 전체에 0회) |
| 5 | 12개 중 7개 단계 | `st.verdict()` 대신 `st.done()` 이라 `errors>0` 이 `ok=False` 가 못 된다 |
| 6 | `collect_page.py:286-297`, `collect_vitals.py:83-106` | HTTP 실패를 데이터 값으로 바꿔서 전부 실패해도 `errors=0` 이 적힌다 |
| 7 | `collect_index.py:141-146` | 첫 `HttpError` 에서 멈추고도 `ok=True` |
| 8 | `store.py:252-253` | `last_error` 2000자 절단 |
| 9 | `remote.py:189-197` | **로컬 CLI 종료 코드가 항상 0** — 호스팅 런이 실패해도 |

9번이 특히 아프다: ADR 0002 가 만든 릴리스 0번 다리가 실패를 **문장으로만** 보고,
`last_ok`/`last_error` 는 `remote.py` 가 부르지 않는 다른 라우트(`app.py:865-878`)에 있다.

### 고침

- 실패를 구조를 가진 값(`kind`·`status`·`item`·`message`)으로 끝까지 옮기고,
  **문자열로 접는 것은 저장 직전 한 번**만. 절단은 표현이지 저장이 아니다.
- 5번: 끝맺음을 `verdict` 로 통일한다 — 1번 작업과 같은 자리다.
- 6번: fetch 실패를 데이터가 아니라 실패로 돌려준다.
- 9번: `remote` 가 `/api/run/status` 의 `last_ok` 를 읽고 종료 코드에 반영한다.
- `print()` 를 표준 `logging` 으로 옮긴다 (현재 `logging.getLogger` 0회). 그래야
  에러 추적(Sentry 등)을 걸 자리가 생긴다.

### ADR 0002 와 충돌하지 않는다

그 ADR 이 스스로 *"이 검사는 실패를 고치지 않는다 — 보이게만 한다"* 고 적고 원인
규명을 열어뒀다. 이 작업은 게이트를 바꾸지 않고 **게이트가 볼 데이터**를 만든다.

### 크기

중간~큼. 1번을 먼저 하면 5번이 딸려 온다.

---

## 4. 정본을 데이터로 내고, 검사가 정규식 고고학을 그만둔다

### 무엇이 틀렸나

`test_seams.py` 는 813줄인데 정본을 `import` 로 읽는 건 `db`·`stage` 둘뿐이고,
나머지는 소스 텍스트를 긁는다 — 정규식 34개, `read_text` 32번.

- 라우트 표: `re.findall(r'@app\.(?:get|post)\("([^"]+)"', ...)` — `test_seams.py:211`, `:505`
- 기회 종류: `scoring.py:2008` 주석이 *"test_seams 가 그 튜플을 정규식으로 읽는다"* 고 적어놨다
- `CARRY_FIELDS`: `test_seams.py:470`
- `ST_IX`: `test_seams.py:288`

표 모양이 바뀌면 검사는 깨지지 않고 **조용히 빈 집합을 본다**. 그래서 두 곳에
`"표 모양이 바뀌었다"` 가드가 손으로 붙어 있다(`test_seams.py:212`, `:507`).

이건 CLAUDE.md 규칙 1(*"렌더된 글자를 정규식으로 되짚지 않는다"*)을 검사 자신이
서버 쪽에서 어기고 있는 모양이다.

### 이미 절반 해놨다

`dashboard.py:1305` 주석:

> *"(이음매 #5)가 두 소스를 정규식으로 훑어 존재를 대조해야 했다. 이제 로컬 Handler 는
> 이 표를 그대로 조회해 등록·디스패치하고, 호스팅(`server/app.py`)도 같은 call 을
> 부른다 — `stage.py` 는 정규식 대신 이 표의 경로 집합을 본다."*

`dashboard.ROUTES` 가 정답 모양이다. 이주가 33개 라우트 중 6개에서 멈췄을 뿐이다.

### 고침

- 라우트: `test_seams` 가 `server.app.app.routes` 를 읽는다. 정규식과 가드가 사라진다.
- 나머지 어휘표: `dashboard.ROUTES` 가 라우트에 한 일을 그대로 한다 — 표를 데이터로
  내고 서버·셸·검사가 그 한 interface 를 읽는다.

### 검사

바꾼 검사마다 **일부러 깨서 FAIL 나는 것까지 확인**한다. 지금 형태는 정본이
사라져도 통과할 수 있으므로, 이 확인이 특히 중요하다.

---

## 5. 프롬프트 분류 어휘가 세 벌이고 이미 어긋났다

```
dash.html:1283      ["추천","비교","문제해결","브랜드","general"]   ← 사용자가 고르는 select
gen_prompts.py:39   ("추천","비교","문제해결","브랜드")             ← 실제로 만드는 쪽
db.py:274 (주석)     추천|비교|문제해결|브랜드|general
```

사용자는 화면에서 `general` 을 고를 수 있는데 생성하는 쪽은 그 값을 모른다.
`test_seams.py` 는 이 어휘를 **안 본다**(0회). 규칙 8·12 가 막으려던 바로 그 모양인데
적용 대상에서 빠져 있었다.

**고침**: 4번의 표에 넣고 셸은 페이로드로 받는다. 기본값(`general`)과 선택지를 구분해
선언한다. 별도 작업이 아니라 4번의 첫 입주자다.

---

## 6. 화면 조립에는 손대지 않는다

`dashboard.py` 의 `assemble()` 은 `str.replace` 라서 원시적으로 **보이지만** 이미
deep 하다. 템플릿 엔진이 못 하는 일을 한다:

- 마크업 파일에서 선언(`view-def`/`section-def`)을 **역추출**하고 `id == 파일명` 을 검증
- 섹션 `after` 사슬을 고정점 반복으로 해결 (`dashboard.py:116-125`)
- 같은 선언을 마크업 순서와 런타임 JSON **두 형태로 발행**
- 조립본 전체를 상대로 실패-크게 검사 (남은 마커, JS 중복 식별자 등, `:1453-1520`)
- 배포 표면 0 — hosted 와 frozen 이 같은 함수를 지나므로 **갈릴 수가 없다**

Jinja2 를 넣으면 `replace` 40줄이 옮겨가고 검증·순서해결 60줄은 별도 pre-pass 로
남는다. 복잡도가 집중되는 게 아니라 흩어지고, 빌드 단계가 배포 마지막 한 뼘에
생긴다 — CLAUDE.md 가 경계하는 그 자리다.

**다만 이 모듈 안의 진짜 얕은 지점 하나는 고친다**: JSON 이스케이프가 같은 관용구
다섯 구현이고(`dashboard.py:126·129·133·201`, 그리고 아무도 안 쓰는 `pages.js`),
`dashboard.py:134` 의 `<option>` 생성은 이스케이프가 아예 없다. 이스케이프를 한
함수로 좁힌다.

---

## 7. 문서 자체의 수정

### 한 것

- **CLAUDE.md 이음매 규칙 3** — `SM.addView`/`addSection` 은 없는 interface 다.
  `addView` 는 리포 전체에 0회, `addSection`(`dashboard.html:1151`)은 호출자 없는
  죽은 코드다. 실제 규약은 `SM.sync()` + `section-def` 조립이고 강제하는 검사는
  `test_seam_02` 안에 있다. 실제 모양으로 고쳤다.
- **CLAUDE.md 목록의 자리** — 12개를 적어놨는데 `test_seams.py` 에는 그보다 많고
  번호도 일대일이 아니다(`07` 결번, `09a`~`09c` 존재). "목록·문구를 두 벌 만들지
  않는다"는 자기 규칙대로, 정본이 `test_seams.py` 임을 명시하고 아래 목록은 발췌임을
  적었다.

### 남은 것

- `dashboard.py:132` 주석이 `dash.html` 이 `window.__LOCALES__` 를 읽는다고 말하는데
  그 문서에는 읽는 곳이 없다. 소비자는 `server/app.html:318`(다른 페이지)뿐이고,
  `test_seams.py:558` 이 그 죽은 페이로드를 존재만으로 못 박고 있다. 주석을 고치거나
  페이로드를 걷는다.
- `templates/dashboard.html:1079-1080` 주석이 옛 런타임 삽입 경로를 설명한다.
- `dash.html:585` 의 `window.SM_STAGE` 는 읽는 곳이 없다(`views/history.html:129-130`
  에 쓰지 말라는 주석만 있다).

---

## 순서

```
1. StageResult 끝맺음        ← 지금 잘못된 메일이 나가고 있다. 작다.
2. 실패 정보 유지 (3번)       ← 1번을 하면 5·7번 항목이 딸려 온다
3. FastAPI Depends (2번)     ← 원 질문의 직답. 새 의존성 0
4. 정본을 데이터로 (4·5번)    ← 4번의 라우트부터, 어휘는 그 위에
5. 이스케이프 한 함수 (6번)
```

세 번째까지 하면 ADR 0002 의 게이트가 처음으로 "왜 실패했는가"에 답할 수 있게 된다.
지금은 게이트가 있는데 볼 데이터가 `errors=100` 뿐이다.
