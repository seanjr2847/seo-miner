---
name: setup
description: First-run onboarding and environment doctor for the seo-miner plugin. Use IMMEDIATELY after plugin installation, whenever the user asks how to get started ("셋업 도와줘", "뭐부터 해야 해", "설치했는데 이제 뭐 함", "/setup", "doctor 돌려줘", "키 설정"), or whenever the capture/create skills hit missing prerequisites (deps, brain.db, OPENROUTER_API_KEY, GSC OAuth, SERP keys) — run this skill's doctor first instead of guessing what's missing.
---

# setup — seo-miner 최초 온보딩 & 환경 닥터

역할: "지금 뭐가 되고, 뭐가 빠졌고, 다음에 뭘 하면 되는지"를 진단하고 순서대로
고쳐준다. 추측 금지 — 항상 doctor부터.

## 말투 규칙 (사용자에게 보이는 모든 문장에 적용)

SEO를 처음 하는 사람이 읽는다고 가정한다.
- 내부 용어를 그대로 말하지 않는다: brain→보관함, capability→기능,
  OAuth→구글 로그인 연결, striking distance→조금만 밀면 1페이지 갈 키워드.
- 항목마다 "이걸 하면 뭐가 좋아지는지"를 한 줄로 붙인다. 소요 시간과
  유료/무료 여부도 같이 말한다.
- 안 해도 되는 건 "안 하셔도 됩니다"라고 분명히 말해 부담을 줄인다.
- 명령어는 사용자가 그대로 복사해 붙일 수 있는 형태로만 준다.

## 표준 경로 — 사이트 하나를 붙이는 전체 순서

**공통 1회** (컴퓨터당 한 번)
1. `/setup` → doctor 실행 → 빠진 게 있으면 `pip install requests jinja2 pyyaml`을
   내가 대신 실행. 보관함은 자동 생성이라 따로 만들 게 없다.

**사이트마다 반복** (여기부터는 사이트 단위)

2. `/capture add {사이트}` — **반드시 먼저.** 자격증명·토큰이 사이트 이름으로
   저장되므로(`creds/{사이트}/`) 등록 전에 GSC 연동을 시작하면 안 된다.
   인터뷰에서 `gsc_property`(`sc-domain:example.com`)를 꼭 받는다.
3. GSC 데이터 — 둘 중 하나를 사용자에게 고르게 한다:
   - **한 번 보고 판단만 할 거면**: `/capture gsc {사이트}` → CSV 경로. 설정 0,
     내가 브라우저로 내보내기까지 눌러준다.
   - **계속 자동으로 받을 거면**: 아래 "GSC 다이렉트 연동" 절차 (사이트당 3~5분,
     한 번 붙이면 이후 무인 + 1,000행 제한·페이지 차원 해제).
4. `/capture keywords {사이트}` → 키워드 유니버스 채우기.
5. AI 인용 확인 — `/seo-miner:browse {사이트}`(무료) 또는 OpenRouter 키(자동화).
6. `/capture run {사이트}` → 리포트까지 한 번에.

2번을 건너뛰고 3번을 하려 하면 `collect_gsc.py`가 프로젝트를 못 찾고 멈춘다.

## 워크플로우

### 1. 진단
`python scripts/doctor.py` (이 스킬 폴더 기준) 실행. stdlib 전용이라 pip 이전에도 돈다.
출력의 기능 목록(✓/✗)과 다음에 할 일을 사용자 말로 요약해 보여준다.

### 2. 수리 — next steps를 위에서부터, 하나씩 확인받고 실행
- **pip 설치**: 사용자 확인 후 직접 실행해준다. 보관함(brain.db)은 첫 실행 때
  자동 생성되므로 init을 시키지 않는다.
- **[선택] 항목은 밀지 않는다.** doctor가 [선택]으로 분류한 건 "지금 안 해도 된다"고
  분명히 말하고, 무료 우회로(GSC는 CSV 내보내기, AI 노출은 browse 스킬)를 먼저 준다.
  사용자가 자동화를 원할 때만 키·OAuth 절차로 넘어간다.
- **API 키·OAuth**: 직접 발급해줄 수 없는 항목. 정확한 절차는
  `../capture/references/setup.md`(4·5·7절)를 읽어 단계별로 안내하고,
  환경변수는 사용자의 셸 rc 파일에 추가하는 명령을 제시한다
  (값 자체는 사용자가 넣게 — 키를 채팅에 붙여넣지 않도록 권한다).
- 각 수리 후 doctor를 재실행해 ✗→✓ 전환을 확인시킨다.

### 3. 단계적 활성화 안내 (키 0개로도 거의 다 된다는 걸 먼저 말한다)
키 없이 오늘 되는 것: 키워드 발굴(자동완성), 구글 실적 읽기(Search Console CSV
내보내기 → `import_gsc_csv.py`), AI 인용 확인(browse 스킬), 리포트, 글 만들기.

돈·시간을 쓰면 좋아지는 것 — 전부 "자동화"를 사는 것이지 새 기능이 아니다:
- OpenRouter 키(유료, 5분, 계정당 1회): AI 인용 확인을 프롬프트 수십 개씩 자동으로
- 구글 연동(무료, **사이트당** 3~5분): 매번 CSV 내보내기 안 해도 됨 +
  1,000행 제한·페이지 단위 해제. 사이트마다 자기 클라이언트로 붙는다
- SERP 키(유료, 선택, 계정당 1회): 순위 매일 기록, 경쟁사 자동 수확

### 4. 졸업
core ✓ + 프로젝트 0개면 → `/capture add <이름>` 으로 자연스럽게 넘긴다.
프로젝트가 이미 있으면 → `/capture run <이름>` 제안.

## GSC 다이렉트 연동을 내가 대신 깔아주기 (claude-in-chrome, 2026-07-26 실측)

"GSC 연동해줘"라고 하면 절차를 읽어주지 말고 브라우저로 직접 눌러준다.
계정을 바꾸는 조작이므로 **시작 전에 무엇을 만들지 알리고 확인을 받는다.**

**사이트 단위로 붙인다.** 자격증명은 `~/.capture/creds/{사이트}/`에 사이트별로 들어가고,
한 사이트의 클라이언트를 다른 사이트에 재사용하지 않는다(스크립트가 중복을 감지해 막는다).
어느 사이트를 붙일지 먼저 정한 뒤 시작하고, 여러 사이트면 한 번에 하나씩 끝낸다.

1. `console.cloud.google.com/apis/library/searchconsole.googleapis.com` →
   상단에서 **이 사이트에 쓸 구글 클라우드 프로젝트** 선택 → `사용` 버튼
   (이미 켜져 있으면 `관리`로 보인다). 사이트별로 구글 클라우드 프로젝트를 따로
   두고 싶으면 여기서 새 프로젝트를 만든다.
2. `console.cloud.google.com/auth/audience` → `게시 상태` 확인.
   **테스트**면 `앱 게시`로 프로덕션 전환을 권한다(안 하면 7일마다 재인증).
   이미 프로덕션이면 넘어간다.
3. `console.cloud.google.com/auth/clients` → `클라이언트 만들기` →
   유형 **데스크톱 앱** → 이름 → 만들기 → JSON 다운로드.
   유형을 웹으로 고르지 않도록 주의(연동 후 redirect_uri 오류의 원인).
4. 다운로드한 JSON은 옮기지 않아도 된다 — `collect_gsc.py`가 다운로드 폴더에서
   `client_secret*.json`(installed 유형)을 찾아 `~/.capture/creds/{P}/`로 옮긴다.
   이미 다른 사이트가 쓰는 클라이언트면 복제하지 않고 멈춘다 — 그때는 이 사이트용
   클라이언트를 새로 만든다(3번 반복).
5. `pip install google-api-python-client google-auth-oauthlib` 후
   `python ../capture/scripts/collect_gsc.py --project {P}` → 브라우저 동의 1회.
   "확인되지 않은 앱" 경고가 뜨면 사용자에게 `고급 → 이동`을 안내한다(본인 앱이므로 정상).

로그인·동의 클릭은 **사용자 몫**이다. 대신 눌러주지 않는다.

## 문제 해결 단서
- doctor가 brain error를 보고하면: 파일 권한 또는 손상 — `~/.capture/brain.db`를
  다른 이름으로 옮기면 다음 실행 때 새로 생성된다(모은 자료는 사라짐을 고지).
- GSC 토큰 문제: `~/.capture/creds/{사이트}/gsc_token.json` 삭제 후 재실행하면
  다시 승인 창이 뜬다(스크립트가 만료를 감지하면 알아서 지우고 재인증한다).
  7일마다 반복되면 그 사이트가 쓰는 구글 클라우드 프로젝트의 동의 화면이
  **테스트** 상태다 — `앱 게시`로 프로덕션 전환을 안내한다.
- 자동완성 실패 지속: 비공식 엔드포인트 특성 — 스로틀 상향(`--throttle 1.0`) 안내.
