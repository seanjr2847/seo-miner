---
name: setup
description: First-run onboarding and environment doctor for the seo-miner plugin. Use IMMEDIATELY after plugin installation, whenever the user asks how to get started ("셋업 도와줘", "뭐부터 해야 해", "설치했는데 이제 뭐 함", "/setup", "doctor 돌려줘", "키 설정"), or whenever the capture/create skills hit missing prerequisites (deps, brain.db, OPENROUTER_API_KEY, GSC OAuth, SERP keys) — run this skill's doctor first instead of guessing what's missing.
---

# setup — seo-miner 최초 온보딩 & 환경 닥터

역할: "지금 뭐가 되고, 뭐가 빠졌고, 다음에 뭘 하면 되는지"를 진단하고 순서대로
고쳐준다. 추측 금지 — 항상 doctor부터.

## 워크플로우

### 1. 진단
`python scripts/doctor.py` (이 스킬 폴더 기준) 실행. stdlib 전용이라 pip 이전에도 돈다.
출력의 capabilities(✓/✗)와 next steps를 사용자에게 요약해 보여준다.

### 2. 수리 — next steps를 위에서부터, 하나씩 확인받고 실행
- **pip 설치, Brain 초기화(db.py init)**: 사용자 확인 후 직접 실행해준다.
- **API 키·OAuth**: 직접 발급해줄 수 없는 항목. 정확한 절차는
  `../capture/references/setup.md`(4·5·7절)를 읽어 단계별로 안내하고,
  환경변수는 사용자의 셸 rc 파일에 추가하는 명령을 제시한다
  (값 자체는 사용자가 넣게 — 키를 채팅에 붙여넣지 않도록 권한다).
- 각 수리 후 doctor를 재실행해 ✗→✓ 전환을 확인시킨다.

### 3. 단계적 활성화 안내 (전부 없어도 시작 가능함을 강조)
- 0키 상태: keywords_free(자동완성 발굴)만으로도 굴러감
- +OPENROUTER_API_KEY: AI 인용 체크 활성화 (권장 1순위)
- +GSC OAuth: 실측 데이터 — striking_distance·pseo_pattern 발굴의 핵심
- +SERP 키(선택): 순위 추적·aio_exposure·경쟁사 자동 수확

### 4. 졸업
core ✓ + Brain ✓ + 프로젝트 0개면 → `/capture add <이름>` 으로 자연스럽게 넘긴다.
프로젝트가 이미 있으면 → `/capture run <이름>` 제안.

## 문제 해결 단서
- doctor가 brain error를 보고하면: 파일 권한 또는 손상 — `~/.capture/brain.db`
  백업 후 `db.py init` 재실행 제안.
- GSC 토큰 만료(테스트 앱 7일): `~/.capture/gsc_token.json` 삭제 후 재인증.
- 자동완성 실패 지속: 비공식 엔드포인트 특성 — 스로틀 상향(`--throttle 1.0`) 안내.
