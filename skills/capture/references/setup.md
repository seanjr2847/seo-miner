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

```bash
pip install requests pyyaml
# GSC 자동 수집(collect_gsc.py) 쓰는 경우에만 추가:
pip install google-api-python-client
```

## 3. 상태 디렉토리

데이터는 스킬 폴더 밖 `$CAPTURE_HOME`(기본 `~/.capture`)에 산다.
스킬을 업데이트/재설치해도 brain.db, 프로젝트 설정, 리포트는 유지됨.

```
~/.capture/
├── brain.db
├── projects/{name}.yaml
├── reports/{name}/{date}.html
├── gsc_service_account.json # GSC 서비스 계정 키 — 전 사이트 공용 1개 (아래 4-B)
└── creds/{name}/            # 구버전 사이트별 OAuth 잔존물 (더 이상 안 읽음 — 지워도 됨)
```

brain.db는 아무 스크립트나 처음 돌리면 자동으로 만들어진다. 명시적으로 하려면
`python scripts/db.py init`.

## 4. GSC 실측 데이터 — 두 가지 경로

### 4-A. CSV 내보내기 (쉬움 — 설정 없음, 1분)

1. Search Console → 실적 → 기간 칩 선택(기본 **3개월**) → `검색어 수` 탭 →
   우측 상단 **내보내기 → CSV 다운로드** (zip으로 받아짐)
2. `python scripts/import_gsc_csv.py --project NAME --days 90`
   (파일 인자를 생략하면 다운로드 폴더에서 방금 받은 걸 찾는다.
   `--days`는 1번에서 고른 기간과 반드시 맞춘다 — 3개월이면 90, 28일이면 28)

Claude에게 부탁하면 1번 클릭까지 브라우저로 대신 해준다 (capture SKILL.md 마지막 절).

한계: UI 내보내기는 **상위 1,000행**까지, **페이지 단위 없음**, 매번 수동.
구글 클라우드 프로젝트·OAuth는 전혀 필요 없다.

### 4-B. 다이렉트 연동 (서비스 계정, 계정당 1회 5분 — 이후 완전 자동)

전제: 대상 사이트가 Search Console에 등록·소유권 확인돼 있어야 함.

**키 1개가 전부다.** `~/.capture/gsc_service_account.json` 하나를
- 플러그인에 번들된 **gsc MCP 서버**(`mcp-server-gsc`) — Claude가 서치콘솔을
  즉석 조회 (`search_analytics` 등)
- **collect_gsc.py** — Brain으로 벌크 수집
이 같이 쓴다. 사이트가 늘어도 키는 다시 안 만들고, Search Console에서 이메일
추가만 하면 된다. 예전 OAuth 방식의 동의 화면·앱 게시·비밀번호 복사·7일 만료는
서비스 계정에는 없다. (구버전 사이트별 OAuth 경로는 제거됐다 —
`~/.capture/creds/{사이트}/` 토큰은 더 이상 읽지 않으며, 이 5분 절차로 전환한다.)

Claude에게 부탁하면 키 다운로드 버튼(3번) 말고는 전부 브라우저로 대신 눌러준다.

1. **Search Console API 켜기** —
   https://console.cloud.google.com/apis/library/searchconsole.googleapis.com
   에서 프로젝트를 고르고 `사용` 클릭 (프로젝트가 없으면 아무거나 하나 만든다).
2. **서비스 계정 만들기** — https://console.cloud.google.com/iam-admin/serviceaccounts
   → `서비스 계정 만들기` → 이름 아무거나(예: seo-miner) → 만들기.
   역할(권한) 부여 단계는 **건너뛴다** — Search Console 접근권은 5번에서 준다.
3. **키 받기** — 만든 계정 → `키` 탭 → `키 추가` → `새 키 만들기` → `JSON` → 만들기.
   (이 다운로드 버튼만은 사람이 직접 눌러야 한다 — 브라우저 자동화가 크롬
   다운로드를 트리거하지 못함, 2026-07-26 실측.)
4. ```
   python ../setup/scripts/connect_gsc.py
   ```
   다운로드 폴더에서 키를 찾아 `~/.capture/gsc_service_account.json`으로 옮기고,
   서비스 계정 이메일과 속성별 '사용자 및 권한' URL을 출력한다.
   (`--status`로 언제든 다시 볼 수 있다.)
5. **권한 주기** — 속성마다: Search Console → 설정 → `사용자 및 권한` →
   `사용자 추가` → 4의 이메일 → 권한 **`제한된 사용자`**(읽기라 충분).
6. `python scripts/collect_gsc.py --project NAME` → **이후 무인 동작.**
   403이 나면 5번이 안 된 속성이다(스크립트가 이메일을 알려준다).
   gsc MCP 툴은 다음 Claude 세션부터 잡힌다(.mcp.json은 세션 시작 때 로드).

`.mcp.json`은 OS를 가리지 않는다 — 번들 런처(`skills/setup/scripts/gsc_mcp.mjs`)를
node로 띄우고, 런처가 플랫폼에 맞게 npx를 부르고 `CAPTURE_HOME` 기준으로 열쇠
경로도 채운다. 필요한 건 node 하나뿐이다(nodejs.org).
`GOOGLE_APPLICATION_CREDENTIALS`를 직접 걸어두면 그쪽이 우선한다.

**어느 쪽을 쓰나**: 한 번 보고 말 거면 4-A(CSV), 매주 자동으로 돌릴 거면 4-B.
4-B가 1,000행 제한·페이지 차원 없음도 같이 푼다.

## 5. OpenRouter (AI 가시성 체크 — 선택)

키 없이 무료로 하려면 `browse` 스킬(`/seo-miner:browse`)이 브라우저로 실제 앱에
직접 물어봐 같은 데이터를 남긴다. 아래는 여러 프롬프트를 자동으로 돌리고 싶을 때.

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
python scripts/import_gsc_csv.py --project myproject <내려받은.zip> --dry-run  # CSV 경로
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
