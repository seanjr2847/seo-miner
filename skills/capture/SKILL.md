---
name: capture
description: Personal SEO & AI-search visibility engine (Boring Agent 역기획 클론, Capture 전용). Use this skill whenever the user asks about their sites' search visibility, rankings, GSC data, keyword discovery/롱테일 발굴, AI citation checks (ChatGPT·Perplexity·Gemini가 누굴 인용하는지), citation gaps, SEO opportunities, or wants a visibility report — including casual phrasings like "내 사이트 요즘 어때", "키워드 좀 캐줘", "AI가 우리 추천해?", "/capture ...", "가시성 리포트 뽑아줘". Also use it to onboard a new project (game/local_clinic/saas/directory) for tracking.
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
1. 인터뷰: 타입(game|local_clinic|saas|directory), 도메인, locale, 시드 키워드 3~10개,
   브랜드 별칭, 경쟁사, gsc_property. `projects/_presets.yaml`에서 타입 프리셋을 읽고
   그 각도에 맞춰 질문을 구체화한다.
2. `projects/_template.yaml`을 복사해 `$CAPTURE_HOME/projects/{P}.yaml` 작성.
3. `python scripts/db.py sync-project $CAPTURE_HOME/projects/{P}.yaml`
4. 프리셋의 ai_prompt_templates를 프로젝트 맥락으로 치환해 AI 프롬프트 10~30개 초안 생성,
   사용자 검수 후 ai_prompts에 INSERT (scoring.md 5절의 파이썬 패턴 사용, is_active=1).

### /capture keywords {P} — 키워드 유니버스 (무료 파이프라인)
1. `python scripts/expand_keywords.py --project {P} --dry-run` 로 계획 고지 → 확인 → 실행.
   (자동완성은 비공식 엔드포인트 — 실패 시 우아하게 건너뛰고 계속한다.)
2. 후보(is_active=0)를 sql로 조회해 관련성 필터·클러스터·인텐트 라벨링을 수행하고,
   프리셋 keyword_angles와 프로젝트 코어 토픽 기준으로 limits.max_keywords 내에서
   활성화할 목록을 사용자에게 제안 → 승인분만 UPDATE로 is_active=1, cluster/intent 기록.
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
  절차를 사용자에게 읽어주지 말 것. (구버전 OAuth 토큰이 있는 사이트는 그대로 돈다.)

연동돼 있으면 **즉석 질문**("어제 클릭 몇이야", "이 페이지 어떤 쿼리로 들어와")은
Brain 수집 없이 gsc MCP 툴(`search_analytics` 등)로 바로 조회해 답해도 된다.
리포트·스코어링·추세 비교는 여전히 `collect_gsc.py`로 Brain에 적재한 스냅샷 기준.

완료 후 striking-distance 프리뷰를 요약해준다.

### /capture rank {P} — 순위 스냅샷 (SERP, 키 있을 때)
`python scripts/collect_serp.py --project {P} --dry-run` → 호출 수·비용 고지 → 확인 → 실행.
결과: 키워드별 순위·SERP 피처·AI오버뷰 인용 여부 + 부산물로 연관검색어/PAA가 키워드
후보에, 상위 빈출 도메인이 경쟁사에 자동 수확된다. 키 미설정이면 GSC 중심 모드로
동작함을 안내한다 (setup.md 7절).

### /capture ai {P}
`python scripts/collect_ai.py --project {P} --dry-run` → 호출 수·비용 어림 고지 → 확인 →
실행(중요 프롬프트만 `--samples 2~3`). 요약 매트릭스를 보여주고, 애매한 판정
(별칭 변형·한글 표기 등)은 answer_excerpt를 sql로 읽어 직접 재판정 후 결과를 설명한다.

OPENROUTER_API_KEY 미설정이면 browse 스킬(브라우저 실측, 키 불필요)을 대안으로 안내한다.

### /capture gaps {P} — 갭 분석 (API 호출 없음, Brain만)
sql로 (a) 인용 갭: cited=0 체크의 cited_domains_json 빈도 + 미노출 프롬프트,
(b) striking distance, (c) rank_decay, (d) aio_exposure(rank 데이터 있을 때:
aio_present=1 AND aio_cited=0 키워드), (e) pseo_pattern: 고노출·저CTR 쿼리를 뽑아
변수 슬롯({지역}·{기온}·{시술} 등) 하나만 다른 템플릿으로 클러스터링해 pSEO 캠페인
후보를 찾는다 — 절차·가드레일은 `references/scoring.md` 1b절을 따른다.
content_gap은 rank 수확 경쟁사 기반의 부분 분석만 가능하고,
완전판(경쟁사 역키워드)은 DataForSEO Labs 연동이 필요함을 명시한다.

### /capture run {P} — 풀런
gsc → rank(SERP 키 있으면, 확인 후) → ai(확인 후) → 분석(아래) → report 순서로 한 번에.

### 분석 단계 (gaps 이후 자동)
`references/scoring.md`를 읽고 기회 스코어링 → opportunities INSERT (상위 10개 reasoning
필수, 수치 인용) → Next Actions 3~5개를 JSON 파일로 저장.

### /capture report {P}
`python scripts/report.py --project {P} --actions <actions.json>` → 파일 경로 안내.
지난 기회들의 status(acked/done/dismissed) 갱신 여부도 물어본다.

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
