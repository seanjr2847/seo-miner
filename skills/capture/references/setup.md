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

1. Search Console → 실적 → 기간 칩 선택(기본 **3개월**) → `검색어 수` 탭 →
   우측 상단 **내보내기 → CSV 다운로드** (zip으로 받아짐)
2. `python scripts/import_gsc_csv.py --project NAME --days 90`
   (파일 인자를 생략하면 다운로드 폴더에서 방금 받은 걸 찾는다.
   `--days`는 1번에서 고른 기간과 반드시 맞춘다 — 3개월이면 90, 28일이면 28)

Claude에게 부탁하면 1번 클릭까지 브라우저로 대신 해준다 (capture SKILL.md 마지막 절).

한계: UI 내보내기는 **상위 1,000행**까지, **페이지 단위 없음**, 매번 수동.
구글 클라우드 프로젝트·OAuth는 전혀 필요 없다.

### 4-B. 다이렉트 연동 (OAuth, 1회 3~5분 — 이후 완전 자동)

전제: 대상 사이트가 Search Console에 등록·소유권 확인돼 있어야 함.
콘솔 화면 이름은 2026-07-26 실측 기준(구 "OAuth 동의 화면 / 사용자 인증 정보"는
**Google 인증 플랫폼**의 `대상` / `클라이언트`로 바뀌었다).

**자격증명은 사이트마다 따로 잡힌다.** 사이트 A의 구글 클라이언트가 사이트 B의
Search Console을 대신 열지 않는다 — 사이트별로 이 절차를 한 번씩 밟는다(사이트당 3~5분).

```
~/.capture/creds/{사이트}/client_secrets.json   # 이 사이트 전용 데스크톱 클라이언트
~/.capture/creds/{사이트}/gsc_token.json        # 이 사이트 전용 승인 토큰
~/.capture/client_secrets.json                  # (선택) 일부러 공용으로 쓸 때만
```

일부러 여러 사이트가 클라이언트 하나를 공유하고 싶으면 공용 경로에 두면 된다.
그렇게 하지 않았는데 이미 다른 사이트가 쓰는 JSON을 집으면 스크립트가 막고 알려준다.

Claude에게 부탁하면 1~3번을 브라우저로 대신 눌러준다 — 사용자는 계정 승인만 하면 된다.

1. **Search Console API 켜기** —
   https://console.cloud.google.com/apis/library/searchconsole.googleapis.com
   에서 프로젝트를 고르고 `사용` 클릭. (프로젝트가 없으면 아무거나 하나 만든다.
   이미 다른 용도로 쓰던 프로젝트를 재사용해도 된다.)
2. **동의 화면** — Google 인증 플랫폼 → `대상`. 처음이면 사용자 유형 **외부**로 생성.
   이미 만들어 둔 프로젝트라면 이 단계는 건너뛴다.
   - 게시 상태가 **테스트**면 리프레시 토큰이 7일 만에 만료된다. `앱 게시`를 눌러
     **프로덕션**으로 두면 만료 없이 계속 쓴다(미확인 앱 경고 화면은 뜨지만
     본인 계정 사용에는 지장 없음, OAuth 사용자 한도 100명).
3. **클라이언트 만들기** — Google 인증 플랫폼 → `클라이언트` → `클라이언트 만들기`
   → 유형 **데스크톱 앱** → 이름 아무거나 → 만들기 → **JSON 다운로드**.
   - ⚠️ 유형을 '웹 애플리케이션'으로 만들면 나중에 redirect_uri 오류가 난다.
     `collect_gsc.py`가 이 실수를 감지해 경고해 준다.
4. 받은 JSON은 **다운로드 폴더에 그대로 두면 된다** — 첫 실행 때
   `~/.capture/creds/{사이트}/client_secrets.json`으로 알아서 옮긴다.
   (다른 경로에 두려면 `GSC_CLIENT_SECRETS` 환경변수로 지정)
5. `pip install google-api-python-client google-auth-oauthlib`
6. `python scripts/collect_gsc.py --project NAME` → 브라우저 승인 1회 →
   토큰이 `~/.capture/gsc_token.json`에 캐시됨. **이후 무인 동작.**
   토큰이 만료·취소되면 자동으로 지우고 재승인 창을 띄운다.

**어느 쪽을 쓰나**: 한 번 보고 말 거면 4-A(CSV), 매주 자동으로 돌릴 거면 4-B.
4-B가 1,000행 제한·페이지 차원 없음도 같이 푼다.

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
