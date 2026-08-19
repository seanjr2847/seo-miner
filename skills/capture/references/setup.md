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
├── gsc_oauth_client.json    # 자기 OAuth 클라이언트를 쓸 때만 (없으면 번들 사용, 4절)
├── gsc_service_account.json # 서비스 계정 키 — 무인 수집을 쓸 때만 (아래 4절)
└── creds/{name}/            # 구버전 사이트별 OAuth 잔존물 (더 이상 안 읽음 — 지워도 됨)
```

구글 **로그인 토큰**은 여기 없다 — gsc MCP 서버가 자기 설정 폴더
(`GSC_CONFIG_DIR` 또는 platformdirs 의 `mcp-gsc`)에 `token.json`으로 캐시한다.
`db.gsc_token()`이 그 자리를 계산하고, `db.gsc_connected()`가 그 파일로 연결을
판정한다.

brain.db는 아무 스크립트나 처음 돌리면 자동으로 만들어진다. 명시적으로 하려면
`python scripts/db.py init`.

## 4. GSC 실측 데이터 — 구글 계정 연결 (필수)

실적(클릭·노출)이 이 도구의 재료라 이 연결 없이는 측정이 시작되지 않는다.
(CSV 내보내기 임시 경로는 2026-08-18 제거 — 1,000행 제한·페이지 차원 없음·매번
수동이라 판정 재료로 부적합했다.)

전제: 대상 사이트가 Search Console에 등록·소유권 확인돼 있어야 함.

### 4-A. 두 가지 방식 — 기본은 내 구글 계정 로그인

| | 구글 계정 로그인 (기본, OAuth) | 서비스 계정 (무인 수집) |
|---|---|---|
| 콘솔 준비 | **없음** — 플러그인이 OAuth 클라이언트를 동봉 | API 사용 설정 + 서비스 계정 + 키 |
| 속성마다 권한 추가 | **없음** — 로그인 한 번에 내가 소유한 속성이 전부 붙음 | 속성마다 이메일 추가 1회 |
| 첫 조회 | 브라우저 로그인 창 1회 (+ 미검증 앱 경고 1회) | 없음 (완전 무인) |
| 만료 | 만료되면 재로그인 1회 | 없음 |
| 클라이언트 위치 | 번들 `skills/setup/oauth_client.json` (자기 것을 쓰면 `~/.capture/gsc_oauth_client.json`) | `~/.capture/gsc_service_account.json` |
| 언제 쓰나 | 대부분의 경우 | 서버·스케줄러에서 사람 없이 돌릴 때 |

번들 클라이언트는 설치형 앱의 client_secret 이 기밀이 아니라는 구글 입장에 기댄
것이고, rclone·gcloud 가 쓰는 방식이다. 대가는 미검증 앱 경고 1회와 사용자 100명
상한이다(둘 다 4-B의 자기 클라이언트 갈래로 없앨 수 있다).

어느 쪽이든 인증 한 벌이면 되고, 그것을
- 플러그인에 번들된 **gsc MCP 서버**([mcp-search-console](https://github.com/AminForou/mcp-gsc))
  — Claude가 서치콘솔을 즉석 조회 (`get_search_analytics`·`inspect_url_enhanced` 등 21개)
- **collect_gsc.py** — Brain으로 벌크 수집

이 같이 쓴다. **인증은 한 벌이다** — `collect_gsc.py`는 서비스 계정만 걸려 있으면
직결하고, 그 외에는 MCP 서버의 인증 해석기(`gsc_server.get_gsc_service()`)를 빌린다.
서버가 파이썬 패키지라 JSON-RPC를 왕복할 필요가 없어서, OAuth 로그인으로 받은
토큰을 즉석 조회와 벌크 수집이 그대로 공유한다. 어느 방식이 걸렸는지의 정본은
`db.gsc_auth()` — **사용자가 직접 놓은 것이 번들을 이긴다**(자기 OAuth 클라이언트 →
서비스 계정 키 → 번들). 번들이 서비스 계정보다 뒤인 건 의도한 것이다: 앞이면
무인 수집을 걸어 둔 사람이 설치만으로 브라우저 로그인에 끌려간다.

**"인증이 걸렸다"와 "연결됐다"는 다르다.** 번들 클라이언트는 설치만 하면 항상
존재하므로 파일 유무로는 아무것도 판정할 수 없다. 연결 판정의 정본은
`db.gsc_connected()`이고, OAuth면 **로그인 토큰이 있느냐**로 본다(서비스 계정은
로그인이 없어 키 파일 존재가 곧 연결). 셋 중 하나로 말한다:

| 상태 | 조건 | 뜻 |
|---|---|---|
| 연결됨 | `gsc_connected()` | 수집 가능 |
| **로그인 대기** | `gsc_auth() != ""` 인데 `not gsc_connected()` | 구글 로그인 한 번만 남음 |
| 인증 없음 | `gsc_auth() == ""` | 번들이 빠진 배포 — 클라이언트를 놓아야 함 |

(구버전 사이트별 OAuth 경로는 제거됐다 — `~/.capture/creds/{사이트}/` 토큰은
더 이상 읽지 않는다. 지우고 아래 절차로 다시 붙이면 된다.)

### 4-B. 연결하기 — 기본은 로그인 한 번 (무료, 계정당 1회)

**절차의 정본은 `../../setup/SKILL.md`의 "GSC 연결" 절이다** — 3-상태, 자기
클라이언트 갈래, 서비스 계정 갈래, 문제 해결까지 거기 한 벌만 있다.

콘솔 준비는 없다. 그냥 수집을 돌리면 된다:

```bash
python scripts/collect_gsc.py --project NAME
```

브라우저 로그인 창이 **한 번** 열리고, 동의 화면에 **"Google에서 확인하지 않은 앱"**
경고가 한 번 뜬다(`고급` → `...(으)로 이동`). 로그인하면 끝이고, 속성마다 권한을
주는 단계는 없다. gsc MCP 툴은 다음 Claude 세션부터 잡힌다(.mcp.json은 세션 시작
때 로드).

지금 어디까지 왔는지:

```bash
python ../setup/scripts/connect_gsc.py --status  # 걸린 방식 · 다음 할 일
```

### 4-B-2. 자기 클라이언트 / 서비스 계정을 쓸 때 (선택 — 콘솔 작업이 여기 남는다)

경고 화면과 100명 상한이 싫으면 자기 OAuth 클라이언트를 쓴다. 콘솔에서
API 사용 설정 → OAuth 동의 화면 → **데스크톱 앱** 클라이언트를 만든 뒤, 화면에 뜬
두 문자열만 넘기면 된다(JSON 다운로드 불필요):

```bash
python ../setup/scripts/connect_gsc.py --client-id ID --client-secret SECRET
```

JSON 파일을 받았거나 서비스 계정 키를 받았다면 인자 없이 실행한다 — 다운로드
폴더에서 회수해 종류를 판별하고 제자리에 깐다(사용자가 고를 필요 없음):

```bash
python ../setup/scripts/connect_gsc.py           # 다운로드 폴더에서 회수해 설치
python ../setup/scripts/connect_gsc.py --from PATH
```

Claude에게 "GSC 연동해줘"라고 하면 **JSON 다운로드 버튼 하나만 사람이 누르고**
나머지 콘솔 클릭은 브라우저로 대신 눌러준다(브라우저 자동화가 크롬 다운로드를
트리거하지 못함 — 2026-07-26 실측).

서비스 계정이면 403이 나는 속성은 아직 그 이메일을 추가하지 않은 속성이다
(스크립트가 이메일과 속성별 URL을 알려준다).

### 4-C. MCP 런처 (참고)

`.mcp.json`은 OS를 가리지 않는다 — 번들 런처(`skills/setup/scripts/gsc_mcp.mjs`)를
node로 띄우고, 런처가 파이썬을 찾아 서버를 넘긴다. 서버 부품(`mcp-search-console`)은
없으면 런처가 처음 한 번 pip으로 깐다. 인증 파일 경로는 `CAPTURE_HOME` 기준으로
채우되(`GSC_OAUTH_CLIENT_SECRETS_FILE`·`GSC_CREDENTIALS_PATH`), 그 환경변수를 직접
걸어 두면 그쪽이 우선한다.
필요한 건 node와 파이썬 3.11+ 뿐이다(파이썬은 이 플러그인이 어차피 쓴다).
윈도우에서 `python`이 마이크로소프트 스토어 스텁이면 런처가 실제 설치본을 찾아낸다 —
못 찾으면 `~/.capture/env`에 `CAPTURE_PYTHON=<python 경로>`를 넣으면 된다.

## 5. OpenRouter (AI 가시성 체크 — 권장)

키 없이 무료로 하려면 `browse` 스킬(`/browse`)이 브라우저로 실제 앱에
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
