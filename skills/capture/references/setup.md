# capture — 설치 & 셋업

## 1. 스킬 설치 (Claude Code)

```bash
# 개인 스킬(모든 프로젝트에서 사용)
cp -r capture ~/.claude/skills/capture
# 새 세션 시작 후 확인: "/skills" 또는 "무슨 스킬 있어?"
```

프로젝트 한정으로 쓰려면 `.claude/skills/capture`에 두면 된다.
문서: https://code.claude.com/docs/en/skills

## 2. 파이썬 의존성

**설치 명령을 여기 적지 않는다** — 정본은 doctor 의 명부
(`../../setup/scripts/doctor.py` 의 `CORE_PIP`·`GSC_PIP`)이고,
`python ../../setup/scripts/doctor.py` 가 **빠진 것만** 골라 그 명령을 찍어 준다.
두 벌이 되면 한쪽만 낡는다.

이 파일이 기억할 것은 하나뿐이다: 구글 **로그인 창을 여는** 부품은
`google-auth-oauthlib` 이고 **조회용** `google-api-python-client` 와 다른
패키지다. 서비스 계정으로 붙이면 로그인이 없어 앞의 것만으로 돈다.

## 3. 상태 디렉토리

데이터는 스킬 폴더 밖 `$CAPTURE_HOME`(기본 `~/.capture`)에 산다.
스킬을 업데이트/재설치해도 brain.db, 프로젝트 설정, 리포트는 유지됨.

```
~/.capture/
├── brain.db
├── projects/{name}.yaml
├── reports/{name}/{date}.html
├── gsc_oauth_client.json    # 자기 OAuth 클라이언트를 쓸 때만 (없으면 번들 사용, 4절)
├── gsc_service_account.json # 서비스 계정 키 — 무인 수집을 쓸 때만 (아래 4절)
├── gsc_token.json           # 구글 로그인 토큰 (로그인 한 번이면 생긴다)
├── docs/{name}/             # 포지셔닝·ASO·콘텐츠 계획 (setup·capture 가 만든다)
└── creds/{name}/            # 구버전 사이트별 OAuth 잔존물 (더 이상 안 읽음 — 지워도 됨)
```

구글 **로그인 토큰**도 여기 산다(`db.gsc_token()`), 연결 판정은 그 파일로 한다
(`db.gsc_connected()`). 예전 gsc MCP 서버가 자기 설정 폴더에 캐시해 두던 토큰이
남아 있으면 그대로 승계한다(`db.gsc_token_legacy()`) — 이미 로그인한 사람은 다시
로그인하지 않는다.

brain.db는 아무 스크립트나 처음 돌리면 자동으로 만들어진다. 명시적으로 하려면
`python scripts/db.py init`.

## 4. GSC 실측 데이터 — 구글 계정 연결 (필수)

실적(클릭·노출)이 이 도구의 재료라 이 연결 없이는 측정이 시작되지 않는다.
(CSV 내보내기 임시 경로는 2026-08-18 제거 — 1,000행 제한·페이지 차원 없음·매번
수동이라 판정 재료로 부적합했다.)

전제: 대상 사이트가 Search Console에 등록·소유권 확인돼 있어야 함.

**절차·상태 판정·문제 해결의 정본은 `../../setup/SKILL.md` 의 "GSC 연결" 절
하나다.** 3-상태(연결됨 / 로그인 대기 / 인증 없음), 자기 OAuth 클라이언트 갈래,
서비스 계정 갈래, 403·토큰 만료 대응이 거기 한 벌씩 있다. **여기 다시 적지
않는다** — 예전에는 이 절이 그 비교표·3상태표·번들 근거를 60줄에 걸쳐 베껴 두고
있었고, 콘솔 절차가 없어졌을 때 이쪽만 낡아 사용자에게 있지도 않은 할 일을 시켰다.

기본은 로그인 한 번이다(무료, 계정당 1회). 콘솔 준비는 없고, 그냥 수집을 돌리면
브라우저 로그인 창이 한 번 열린다:

```bash
python scripts/collect_gsc.py --project NAME
python ../setup/scripts/connect_gsc.py --status   # 지금 걸린 방식 · 다음 할 일
```

이 파일에만 있는 사실 하나 — **인증은 한 벌이다.** `collect_gsc.py`(벌크 수집)·
`gsc_query.py`(즉석 조회)·`collect_index.py`(색인 상태) 셋이 전부
`collect_gsc.get_service()` 하나를 부른다. 걸린 방식의 정본은 `db.gsc_auth()`,
연결됐는지의 정본은 `db.gsc_connected()` 다 — **파일이 있다 ≠ 연결됐다**(번들
클라이언트는 설치만 하면 항상 존재한다).

(구버전 사이트별 OAuth 경로는 제거됐다 — `~/.capture/creds/{사이트}/` 토큰은
더 이상 읽지 않는다. 지우고 위 절차로 다시 붙이면 된다.)

### 4-C. 즉석 조회 (참고)

Brain 에 적재하지 않고 서치콘솔 원본을 바로 읽는다 — 출력은 JSON 이다.

```bash
python scripts/gsc_query.py properties
python scripts/gsc_query.py search  --project NAME --days 7 --dim query,page
python scripts/gsc_query.py compare --project NAME --days 28
python scripts/gsc_query.py inspect --project NAME https://example.com/a
python scripts/gsc_query.py sitemaps --project NAME
```

**수집기와 숫자가 다른 게 정상이다.** 즉석 조회는 `dataState=all` + 창 끝이 오늘,
수집기는 `final` + 3일 버퍼다. 지금 현황은 이쪽, 스냅샷 Δ 비교는 저쪽 —
두 값을 빼서 증감을 말하면 안 된다(capture/SKILL.md 철칙 1).

## 5. OpenRouter (AI 가시성 체크 — 권장)

AI 인용 확인(`/capture ai`)에는 이 키가 필요하다. 나머지 기능은 키 없이 그대로 된다.

1. https://openrouter.ai 가입 → 크레딧 소액 충전 → API 키 발급
2. 키는 **`~/.capture/env`(KEY=VALUE) 한 곳**에 둔다: `OPENROUTER_API_KEY=sk-or-...`.
   대시보드 [설정] 패널에 붙여넣으면 거기 저장되고, 모든 스크립트가
   `db.load_env()`로 읽는다 — 셸 rc를 편집할 필요가 없다. 이 절의 다른 키(7절
   포함)도 같은 파일이다. 셸에 `export`한 값이 있으면 그쪽이 우선한다(setdefault).
3. 모델 슬러그는 시간이 지나면 바뀐다 → `config.yaml`의 `ai_engines`에서 관리.
   `:online` 접미사가 웹검색을 켠다(OpenAI·Google·Perplexity 등은 네이티브 검색 사용).
4. 비용 감각: 검색 호출료가 토큰보다 크다. 프롬프트 30개 × 3엔진 주 1회 기준
   월 수 달러~십수 달러. 반드시 `--dry-run`으로 호출 수 확인 후 실행.

## 6. 스모크 테스트 순서

```bash
cd ~/.claude/skills/capture
python scripts/scoring.py            # 판정 규칙 자체점검 (임계값·브랜드 제외·정렬)
python scripts/collector.py          # 설정 우선순위 자체점검
python scripts/test_capture.py       # 임시 폴더에서 도는 회귀 테스트
cp projects/_template.yaml ~/.capture/projects/myproject.yaml   # 편집 후
python scripts/db.py sync-project ~/.capture/projects/myproject.yaml
python scripts/expand_keywords.py --project myproject --dry-run
python scripts/collect_ai.py --project myproject --dry-run
python scripts/collect_gsc.py --project myproject --dry-run
python scripts/dashboard.py --export --project myproject   # 빈 리포트라도 렌더 확인
python scripts/dashboard.py --open               # 로컬 대시보드 (Ctrl+C로 종료)
```

## 7. SERP 순위 추적 (선택 — 키 있으면 /capture rank 활성화)

둘 중 하나만 있으면 됨. 둘 다 있으면 config.yaml serp.provider로 선택.

**DataForSEO (추천 — AI오버뷰 데이터 포함)**
1. https://dataforseo.com 가입 → $1 무료 크레딧 (Live Advanced 기준 수백 회 분량)
2. 대시보드 API Settings에서 API 비밀번호 확인 (계정 비번과 다름)
3. `~/.capture/env` 에 `DATAFORSEO_LOGIN=가입이메일` / `DATAFORSEO_PASSWORD=API비밀번호`
   (5-2절과 같은 파일 — 대시보드 [설정] 패널이 여기 쓴다)
4. 본격 사용 시 최소 충전 $50 (월 사용량 $1~2 수준이라 수년치)
   **같은 키로 DataForSEO Labs (역키워드 — `scripts/collect_gap.py` 의
   `ranked_keywords/live`) 가 함께 청구된다** — `/capture gap` 사용 시 추가로
   도메인당 ~$0.001/콜이 나간다. 가격표:
   https://dataforseo.com/apis/dataforseo-labs-api

**Serper.dev (단순함 우선 — AIO 없음)**
1. https://serper.dev 가입 → 무료 크레딧 2,500 (카드 불필요)
2. `~/.capture/env` 에 `SERPER_API_KEY=...`

동작 확인: `python scripts/collect_serp.py --project NAME --dry-run`
