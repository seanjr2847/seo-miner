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
2. **비용 고지.** 외부 API를 부르는 작업(collect_ai)은 실행 전 반드시 `--dry-run`으로
   호출 수를 보여주고 사용자 확인을 받는다.
3. **리포트는 Next Actions로 끝난다.** 분석 없이 빈 액션으로 리포트를 내보내지 않는다.
4. 데이터 해석 규칙(비결정성, GSC 구멍, 노이즈 임계값)은 `references/scoring.md` 4절을 따른다.

## 상태 위치

데이터는 `$CAPTURE_HOME`(기본 `~/.capture`)에 산다 — brain.db, projects/*.yaml, reports/.
최초 1회: `python scripts/db.py init`. 미설정·의존성 오류가 감지되면 추측하지 말고
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
`python scripts/collect_gsc.py --project {P}` (첫 실행은 브라우저 OAuth — setup.md 4절).
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

## 스코프 밖 (요청받아도 이 스킬로는 하지 않는 것)

콘텐츠 초안 작성·브리프 핸드오프(create 스킬의 영역), 네이버 표면(미탑재).
부탁받으면 한계를 밝히고 대안을 안내.
