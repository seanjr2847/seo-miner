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
pip install requests jinja2 pyyaml
# GSC 쓰는 경우에만 추가:
pip install google-api-python-client google-auth-oauthlib
```

## 3. 상태 디렉토리

데이터는 스킬 폴더 밖 `$CAPTURE_HOME`(기본 `~/.capture`)에 산다.
스킬을 업데이트/재설치해도 brain.db, 프로젝트 설정, 리포트는 유지됨.

```
~/.capture/
├── brain.db
├── projects/{name}.yaml
├── reports/{name}/{date}.html
├── client_secrets.json      # GSC OAuth (아래 4번)
└── gsc_token.json           # 자동 생성·캐시
```

brain.db는 아무 스크립트나 처음 돌리면 자동으로 만들어진다. 명시적으로 하려면
`python scripts/db.py init`.

## 4. GSC 실측 데이터 — 두 가지 경로

### 4-A. CSV 내보내기 (쉬움 — 설정 없음, 1분)

1. Search Console → 실적 → 기간 선택 → 우측 상단 **내보내기 → CSV** (zip으로 받아짐)
2. `python scripts/import_gsc_csv.py --project NAME ~/Downloads/받은파일.zip --days 28`
   (`--days`는 내보낼 때 화면에 걸려 있던 기간을 그대로 적는다)

한계: UI 내보내기는 **상위 1,000행**까지, **페이지 단위 없음**, 매번 수동.
구글 클라우드 프로젝트·OAuth는 전혀 필요 없다.

### 4-B. OAuth 자동 연동 (1회, ~10분)

전제: 대상 사이트가 Search Console에 등록·소유권 확인돼 있어야 함.

1. https://console.cloud.google.com → 새 프로젝트 생성
2. "API 및 서비스" → 라이브러리 → **Search Console API** 사용 설정
3. OAuth 동의 화면 → User Type **외부**, 게시 상태는 **테스트**로 두고
   본인 구글 계정을 테스트 사용자로 추가
4. 사용자 인증 정보 → OAuth 클라이언트 ID → 유형 **데스크톱 앱**
5. JSON 다운로드 → `~/.capture/client_secrets.json` 로 저장
   (다른 경로면 `GSC_CLIENT_SECRETS` 환경변수로 지정)
6. 첫 실행 `python scripts/collect_gsc.py --project NAME` 시 브라우저가 열리고
   승인하면 토큰이 `~/.capture/gsc_token.json`에 캐시됨. 이후 무인 동작.

주의: 테스트 상태의 OAuth 앱은 리프레시 토큰이 7일 만에 만료될 수 있음.
만료되면 gsc_token.json 지우고 재인증하거나, 동의 화면을 "프로덕션"으로 게시.

## 5. OpenRouter (AI 가시성 체크 — 선택)

키 없이 무료로 하려면 `browse` 스킬(`/seo-miner:browse`)이 브라우저로 실제 앱에
직접 물어봐 같은 데이터를 남긴다. 아래는 여러 프롬프트를 자동으로 돌리고 싶을 때.

1. https://openrouter.ai 가입 → 크레딧 소액 충전 → API 키 발급
2. `export OPENROUTER_API_KEY=sk-or-...` (셸 rc 파일에 추가)
3. 모델 슬러그는 시간이 지나면 바뀐다 → `config.yaml`의 `ai_engines`에서 관리.
   `:online` 접미사가 웹검색을 켠다(OpenAI·Google·Perplexity 등은 네이티브 검색 사용).
4. 비용 감각: 검색 호출료가 토큰보다 크다. 프롬프트 30개 × 3엔진 주 1회 기준
   월 수 달러~십수 달러. 반드시 `--dry-run`으로 호출 수 확인 후 실행.

## 6. 스모크 테스트 순서

```bash
cd ~/.claude/skills/capture
cp projects/_template.yaml ~/.capture/projects/myproject.yaml   # 편집 후
python scripts/db.py sync-project ~/.capture/projects/myproject.yaml
python scripts/expand_keywords.py --project myproject --dry-run
python scripts/collect_ai.py --project myproject --dry-run
python scripts/collect_gsc.py --project myproject --dry-run
python scripts/import_gsc_csv.py --project myproject <내려받은.zip> --dry-run  # CSV 경로
python scripts/report.py --project myproject     # 빈 리포트라도 렌더 확인
```

## 7. SERP 순위 추적 (선택 — 키 있으면 /capture rank 활성화)

둘 중 하나만 있으면 됨. 둘 다 있으면 config.yaml serp.provider로 선택.

**DataForSEO (추천 — AI오버뷰 데이터 포함)**
1. https://dataforseo.com 가입 → $1 무료 크레딧 (Live Advanced 기준 수백 회 분량)
2. 대시보드 API Settings에서 API 비밀번호 확인 (계정 비번과 다름)
3. `export DATAFORSEO_LOGIN=가입이메일` / `export DATAFORSEO_PASSWORD=API비밀번호`
4. 본격 사용 시 최소 충전 $50 (월 사용량 $1~2 수준이라 수년치)

**Serper.dev (단순함 우선 — AIO 없음)**
1. https://serper.dev 가입 → 무료 크레딧 2,500 (카드 불필요)
2. `export SERPER_API_KEY=...`

동작 확인: `python scripts/collect_serp.py --project NAME --dry-run`
