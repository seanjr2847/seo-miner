---
name: capture
description: 검색·AI 가시성 측정·채굴 (Boring Agent 역기획, Capture 전용) — GSC 실적, 순위, 키워드/롱테일 발굴, AI 인용 체크(ChatGPT·Perplexity·Gemini가 누굴 인용하는지), 인용 갭, SEO 기회, 가시성 리포트. 사용 시점 — "내 사이트 요즘 어때", "키워드 좀 캐줘", "AI가 우리 추천해?", "/capture ...", "가시성 리포트 뽑아줘" 같은 요청 전부, 그리고 새 사이트(game/local_clinic/saas/directory) 추적 온보딩.
---

# capture — 개인용 검색·AI 가시성 Capture 엔진

측정하고, 캐고, 갭을 찾고, 리포트를 뱉는다. 콘텐츠 제작은 스코프 밖.
수집은 scripts/가, 판단은 Claude가, 기억은 SQLite(Brain)가, 출력은 로컬 HTML이 맡는다.

## 철칙 (Brain-first)

1. **즉흥 금지.** 순위·노출·인용·키워드에 대한 모든 주장은 Brain 조회 결과를 근거로 한다.
   조회는 `python scripts/db.py sql "SELECT ..."` (읽기 전용). 데이터가 없으면 "없다"고 말하고 수집을 제안한다.
   (예외: GSC 실측 수치는 gsc MCP 툴로 서치콘솔에서 직접 조회한 결과도 근거로 인정 —
   원본 데이터라 Brain 스냅샷보다 신선하다. 어느 쪽 근거인지는 밝힌다.)
2. **비용 고지.** 외부 API를 부르는 작업(collect_ai)은 실행 전 반드시 `--dry-run`으로
   호출 수를 보여주고 사용자 확인을 받는다.
3. **리포트는 Next Actions로 끝난다.** 분석 없이 빈 액션으로 리포트를 내보내지 않는다.
4. 데이터 해석 규칙(비결정성, GSC 구멍, 노이즈 임계값)은 `references/scoring.md` 4절을 따른다.

## 상태 위치

데이터는 `$CAPTURE_HOME`(기본 `~/.capture`)에 산다 — brain.db, projects/*.yaml, reports/.
brain.db는 첫 접속 때 자동 생성된다(`db.connect`) — 사용자에게 init을 시키지 말 것.
미설정·의존성 오류가 감지되면 추측하지 말고
setup 스킬의 doctor(`../setup/scripts/doctor.py`)를 먼저 돌려 진단 기반으로 안내한다.
상세 절차 문서는 `references/setup.md`.

## 명령 워크플로우

사용자가 `/capture <cmd>`라고 하거나 같은 의미로 말하면 아래를 수행한다.
`{P}` = 프로젝트 이름.

### /capture add {P} — 프로젝트 온보딩
**이미 등록된 이름이면**(대시보드 설정 폼으로 만든 경우) 1~3단계는 건너뛰고 4단계만
한다 — yaml을 덮어쓰지 말 것. 비어 있는 건 AI 프롬프트뿐이다.

1. 인터뷰: 타입(game|local_clinic|saas|directory), 도메인, locale, 시드 키워드 3~10개,
   브랜드 별칭, 경쟁사, gsc_property, 그리고 **도구 이름(`tools`)** — directory·saas
   타입은 이게 비면 남의 브랜드 카탈로그가 비어서 striking_distance에 노이즈가
   흘러든다 (`scoring.md` 1a). `projects/_presets.yaml`에서 타입 프리셋을 읽고
   그 각도에 맞춰 질문을 구체화한다.
2. `projects/_template.yaml`을 복사해 `$CAPTURE_HOME/projects/{P}.yaml` 작성.
3. `python scripts/db.py sync-project $CAPTURE_HOME/projects/{P}.yaml`
4. 프리셋의 ai_prompt_templates를 프로젝트 맥락으로 치환해 AI 프롬프트 10~30개 초안 생성,
   사용자 검수 후 ai_prompts에 INSERT (scoring.md 5절의 파이썬 패턴 사용, is_active=1).

### /capture keywords {P} — 키워드 유니버스 (무료 파이프라인)
1. `python scripts/expand_keywords.py --project {P} --dry-run` 로 계획 고지 → 확인 → 실행.
   (자동완성은 비공식 엔드포인트 — 실패 시 우아하게 건너뛰고 계속한다.)
2. 후보(is_active=0)를 sql로 조회해 관련성 필터·클러스터 라벨링을 수행하고,
   프리셋 keyword_angles와 프로젝트 코어 토픽 기준으로 limits.max_keywords 내에서
   활성화할 목록을 사용자에게 제안 → 승인분만 UPDATE로 is_active=1, cluster 기록.
   **인텐트 라벨링은 별도 단계가 아니다** — `scoring.py load`가 시작 시
   `_backfill_intents`로 `intent`가 NULL인 활성 키워드만 `classify_intent()`로
   채우고, 이미 적힌 값은 보존한다 (트랜잭셔널 > 커머셜 > 내비게이셔널 > info
   우선순위는 코드가 결정). `classify_intent()`가 잘못 잡은 것만 직접 고친다
   (`scoring.md` 1c).
3. 수요 근거는 "자동완성 등장 = 수요 존재, GSC 노출 = 실측"으로만 말한다 (볼륨 창작 금지).

### /capture gsc {P}
두 경로가 있다. **연동이 안 돼 있으면 설정 절차부터 시키지 말고 CSV 경로를
먼저 제안한다** (설정 0, 1분).

- **CSV(쉬움, 기본)**: 사용자에게 파일을 받아오라고 시키지 말고 **claude-in-chrome으로
  내가 직접 눌러준다.** 아래 5절 참조. 파일만 있으면
  `python scripts/import_gsc_csv.py --project {P}` (인자 없이 실행하면 다운로드 폴더에서
  방금 받은 내보내기를 알아서 찾는다. 경로를 알면 뒤에 붙여도 된다).
  한계: 상위 1,000행, 페이지 단위 없음 — 이 점을 먼저 고지한다.
- **다이렉트 연동(서비스 계정)**: `python scripts/collect_gsc.py --project {P}`.
  키 1개(`~/.capture/gsc_service_account.json`)로 모든 사이트 — 1,000행 제한도
  페이지 차원 없음도 풀린다. 아직 안 붙어 있으면 setup 스킬의
  "GSC 연결(서비스 계정)" 절차대로 콘솔 클릭을 내가 대신 해준다(계정당 1회 5분).
  절차를 사용자에게 읽어주지 말 것. (구버전 사이트별 OAuth 경로는 제거됐다 —
  남은 토큰은 무시되며, 서비스 계정으로 전환하면 된다.)

연동돼 있으면 **즉석 질문**("어제 클릭 몇이야", "이 페이지 어떤 쿼리로 들어와")은
Brain 수집 없이 gsc MCP 툴(`search_analytics` 등)로 바로 조회해 답해도 된다.
리포트·스코어링·추세 비교는 여전히 `collect_gsc.py`로 Brain에 적재한 스냅샷 기준.

완료 후 striking-distance 프리뷰를 요약해준다.

### /capture rank {P} — 순위 스냅샷 (SERP, 키 있을 때)
`python scripts/collect_serp.py --project {P} --dry-run` → 호출 수·비용 고지 → 확인 → 실행.
결과: 키워드별 순위·SERP 피처·AI오버뷰 인용 여부 + 부산물로 연관검색어/PAA가 키워드
후보에, 상위 빈출 도메인이 경쟁사에 자동 수확된다. 키 미설정이면 GSC 중심 모드로
동작함을 안내한다 (setup.md 7절).

디바이스: `--device mobile` 로 설정하면 `serp_device` 가 mobile 로 고정된다(기본
desktop). **serper 는 desktop 만 지원 — mobile 요청은 desktop 으로 폴백된다**.
device 를 바꾸면 직전 스냅샷과 디바이스가 달라져 Δ 비교가 한 번 흐려진다(같은
device 끼리만 비교 가능, `scoring.md` 4-3b 의 period_days 짝 규칙과 같은 맥락).
재실행 안전망: **오늘 이미 확인한 키워드는 자동 건너뜀** (rank_snapshots 가 같은
날 덮어쓰는 구조를 그대로 활용해 재호출 비용을 아낀다). 강제 재확인은 `--force`.

여러 번 돌아도 `rank_snapshots.position`이 계속 NULL인 키워드가 있다 — 그 키워드는
**순위 문제가 아니라 인덱싱 문제일 수 있다**. 번들된 gsc MCP의 `index_inspect`
툴로 해당 URL의 인덱스 여부를 먼저 확인한다 — 미인덱스면 노리는 페이지 자체가
검색에 없으니 순위를 따져도 의미가 없다 (`scoring.md` 4절 — `RANK_NOISE`도
NULL엔 적용되지 않는다).

### /capture ai {P}
`python scripts/collect_ai.py --project {P} --dry-run` → 호출 수·비용 어림 고지 → 확인 →
실행(기본 2샘플 — config `ai_samples`, 비용 2배 트레이드오프. 중요 프롬프트는
`--samples 3`). 요약 매트릭스를 보여주고, 요청 실패가 있으면 매트릭스 분모가 그만큼
줄어든 것임을 함께 말한다. 애매한 판정(별칭 변형·한글 표기 등)은 answer_excerpt를
sql로 읽어 직접 재판정 후 결과를 설명한다 — 답변 전문이 저장되므로(상한 8000자)
재판정 근거로 충분하다.

재실행 안전망: **오늘 이미 기록된 (프롬프트, 엔진, 샘플) 조합은 자동 건너뜀** —
같은 날 두 번 돌면 두 번째는 비용·시간만 들고 ai_checks 행은 늘리지 않는다.
강제 재기록은 `--force`. (AI 답변은 비결정적이라 1샘플은 "사실"이 아니다 — `scoring.md`
4-1 의 주 단위 추세 원칙과 같은 이유다.)

OPENROUTER_API_KEY 미설정이면 browse 스킬(브라우저 실측, 키 불필요)을 대안으로 안내한다.

### /capture gap {P} — 경쟁사 역키워드 (DataForSEO Labs, 유료 키 필요)
`python scripts/collect_gap.py --project {P} --dry-run` 으로 도메인 수·요청
수·비용 어림을 먼저 고지하고 확인을 받은 뒤 실행. **dry-run 선행·비용 고지는
이 작업의 철칙** — Labs 는 도메인당 ~$0.001 청구(`references/setup.md` 7절
DataForSEO 가격표 링크)이고 적재량에 따라 비용이 빠르게 늘어난다.

**Labs 유료 키가 필요하다** — 기존 DataForSEO 자격 (`DATAFORSEO_LOGIN` /
`DATAFORSEO_PASSWORD`)을 그대로 쓴다. 같은 키로 SERP 와 Labs 가 모두 청구된다.
키가 없으면 다른 수집기와 같은 톤으로 정중히 종료한다(에러 아님).

흐름: 경쟁사 도메인(생략 시 `competitors` 테이블, 상한 5개)에 대해 DataForSEO
Labs `ranked_keywords/live` 를 호출해 랭킹 키워드를 받아, (a) 내 키워드와
(b) 최신 GSC 스냅샷 노출>0 쿼리에 이미 있는 것은 제외 — **남는 것이 "경쟁사는
잡는데 나는 부재"** 후보다.

결과는 `keywords` 후보(`source='competitor_gap'`, `is_active=0`)로 적재된다 —
즉 **`/capture keywords` 의 큐레이션 단계(승인분만 활성화)** 를 그대로 탄다.
Labs 가 `search_volume` 을 주면 `keywords.volume` 에 기록한다(실측 추정치라
'볼륨 창작 금지' 규칙 위반 아님). 기회 판정·군집화는 큐레이션 후 Claude
(`scoring.md` 1절 content_gap 행).

### /capture gaps {P} — 갭 분석 (API 호출 없음, Brain만)
sql로 (a) 인용 갭: cited=0 체크의 cited_domains_json 빈도 + 미노출 프롬프트,
(b) striking distance, (c) rank_decay, (d) aio_exposure(rank 데이터 있을 때:
aio_present=1 AND aio_cited=0 키워드), (e) pseo_pattern: 고노출·저CTR 쿼리를 뽑아
변수 슬롯({지역}·{기온}·{시술} 등) 하나만 다른 템플릿으로 클러스터링해 pSEO 캠페인
후보를 찾는다 — 절차·가드레일은 `references/scoring.md` 1b절을 따른다.
content_gap 후보는 별도 명령 `/capture gap {P}` (Labs 유료 키)로 먼저 적재한다 —
여기서는 적재된 후보를 기회로 정리·판정한다.

### /capture run {P} — 풀런
gsc → rank(SERP 키 있으면, 확인 후) → ai(확인 후) → 분석(아래) → report 순서로 한 번에.

### 분석 단계 (gaps 이후 자동)
1. `python scripts/scoring.py load {P}` — 기계 판정분(striking_distance·ctr_gap·
   cannibalization·rank_decay·pseo 후보·coverage)을 결정적 점수·수치 reasoning과
   함께 opportunities에 적재한다.
2. 적재된 기회를 sql로 검토하고 상위 10개의 reasoning을 보강한다 — 원인 가설·
   fit 판단·맥락 (`references/scoring.md` 2~3절).
3. pseo_pattern 군집 등 Claude 판단이 필요한 kind는 scoring.md 1b·5절대로 추가
   적재 → Next Actions 3~5개를 JSON 파일로 저장.

### /capture dash {P} — 로컬 대시보드
`python scripts/dashboard.py --project {P} --open` 을 **백그라운드로** 띄운다
(포그라운드로 돌리면 세션이 막힌다). 127.0.0.1 전용 웹 UI로 Brain을 실시간
조회하고, 기회 상태(확인/완료/기각)를 표에서 바로 갱신한다.
**dash와 report는 같은 화면이다.** dash는 서버가 Brain을 실시간으로 읽는 모드,
report는 그 화면을 그날 데이터째 파일로 박제한 모드(`--export`). 지금 상태를 보고
손댈 때는 dash, 남겨 두거나 남한테 보낼 때는 report.
헤더 [설정] 버튼에 온보딩 패널(부품 설치·사이트 등록·GSC 열쇠·API 키)이 있다 —
사용자가 채팅 말고 직접 하고 싶어 하면 여기로 보낸다 (setup 스킬 참조).
헤더 [안내] 버튼에는 **이 도구를 쓰는 순서 6단계와 지금 프로젝트가 어디까지 왔는지**가
Brain 실적 기준으로 뜬다(GSC 스냅샷이 0이면 자동으로 펴진다). "뭘 해야 하냐"는
질문에는 말로 설명하지 말고 이 화면을 띄워 "지금 할 것"을 짚어준다.

### /capture report {P} — 오늘 화면을 파일로 박제
`python scripts/dashboard.py --export --project {P} --actions <actions.json>` → 파일 경로 안내.
(대시보드와 같은 화면·같은 데이터를 파일로 굽는 모드다 — 별도 스크립트가 아니다.)
서버 없이 열리는 자립형 HTML이라 남한테 보내도 되고, 기회 상태는 못 바꾼다 —
상태 갱신은 `/capture dash`에서 클릭으로 하는 게 빠르다고 안내한다.
Next Actions가 리포트의 결론이므로 `--actions` 없이 내보내지 않는다(철칙 3).

### /capture ask {P} "..."
자유 질문. sql로 근거를 조회해 수치와 함께 답한다. 없는 데이터는 없다고 답한다.

## GSC CSV를 브라우저로 직접 받아오기 (claude-in-chrome, 2026-07-26 실측)

사용자에게 "내보내기 눌러서 파일 주세요"라고 시키지 않는다. 내가 누른다.

1. `tabs_context`로 시작 → `search.google.com/search-console/performance/search-analytics?resource_id={속성}`
   으로 이동. 속성 ID를 모르면 `search.google.com/search-console`을 열어 좌상단
   속성 선택기에서 확인한다 (`sc-domain:example.com` 형태).
2. **기간을 먼저 맞춘다.** 상단 칩: `24시간 / 7일 / 28일 / 3개월 / 더보기`.
   **기본값이 3개월**이므로 그대로 받으면 `--days 90`, 28일 칩을 눌렀으면 `--days 28`.
   여기서 고른 기간과 `--days`가 어긋나면 Brain의 기간 표기가 틀어진다.
3. 하단 표에서 **`검색어 수` 탭**이 선택돼 있는지 확인 (페이지/국가/기기 탭이 아니라).
4. 우상단 **`내보내기`** 버튼 → 메뉴 3개(`Google Sheets` / `Excel 다운로드` /
   **`CSV 다운로드`**) → CSV 다운로드. **다운로드는 사용자 확인을 받고 누른다.**
5. `python scripts/import_gsc_csv.py --project {P} --days {2번에서 고른 기간}` —
   인자 없이도 다운로드 폴더에서 방금 받은 zip을 찾아낸다.

로그인 안 돼 있으면 **대신 로그인하지 않는다** — 사용자에게 로그인만 부탁하고 대기.
속성이 없으면 CSV 경로 자체가 불가하므로 Search Console 소유권 확인부터 안내한다.

## 스코프 밖 (요청받아도 이 스킬로는 하지 않는 것)

콘텐츠 초안 작성·브리프 핸드오프(create 스킬의 영역), 네이버 표면(미탑재).
부탁받으면 한계를 밝히고 대안을 안내.
