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
   Brain 근거는 `gsc_snapshots`(기간 합계)만이 아니다 — 날짜별 추이는 `gsc_daily`,
   디바이스·국가 분해는 `gsc_breakdown`, 색인 상태는 `gsc_index_status` 에 있다.
   "언제부터 떨어졌나"·"모바일만 나쁜가"·"색인은 됐나"는 합계 스냅샷으로는 답이
   안 나온다 — 이 세 테이블을 조회해서 답한다.
   (예외: `scripts/gsc_query.py` 로 서치콘솔을 직접 조회한 결과도 근거로 인정 —
   원본 데이터라 Brain 스냅샷보다 신선하다. 어느 쪽 근거인지는 밝힌다.)

   **두 경로의 숫자는 원래 다르다 — 섞어서 비교하지 마라.**

   | | 즉석 조회 (`gsc_query.py`) | Brain (`collect_gsc.py`) |
   |---|---|---|
   | 데이터 상태 | `all` — 미확정 포함 (GSC 대시보드와 같은 값) | `final` — 확정만 |
   | 창 끝 | 오늘 | 오늘 − 3일 |
   | 쓰임 | 지금 이 순간을 묻는 즉석 질문 | 스냅샷끼리 비교·결정적 스코어링 |

   같은 "지난 28일"이 두 경로에서 다른 값으로 나오는 건 **버그가 아니라 설계다**.
   구글은 최근 2~3일 수치를 나중에 채워 넣는다 — 즉석 답은 그걸 보여줘야 쓸모가 있고,
   Δ 비교는 그걸 빼야 거짓이 안 된다. 그래서 **즉석 조회 수치와 Brain 수치를 빼서
   증감을 말하면 안 된다**. 증감은 Brain 안에서만, 즉석 현황은 gsc_query 로.
2. **비용 고지.** 외부 API를 부르는 작업(collect_ai)은 실행 전 반드시 `--dry-run`으로
   호출 수를 보여주고 사용자 확인을 받는다.
3. **리포트는 Next Actions로 끝난다.** 분석 없이 빈 액션으로 리포트를 내보내지 않는다.
4. 데이터 해석 규칙(비결정성, GSC 구멍, 노이즈 임계값)은 `references/scoring.md` 4절을 따른다.

## 답변 형식 (채팅으로 나가는 모든 말)

한 줄 결론 → 근거 표 → **다음 한 걸음 하나** → 접어 둔 "안 한 것".
정본은 `../setup/references/reply-format.md` — 작업을 끝내고 보고하기 전에 그 뼈대를
따른다. 근거는 Brain 조회 결과여야 하고(철칙 1), 다음 걸음의 판정은
`stage.state()` 가 한다(내가 다시 판단하지 않는다).

## 상태 위치

데이터는 `$CAPTURE_HOME`(기본 `~/.capture`)에 산다 — brain.db, projects/*.yaml, reports/.
brain.db는 첫 접속 때 자동 생성된다(`db.connect`) — 사용자에게 init을 시키지 말 것.
**보관함은 컴퓨터 전역이다** — 사이트가 여럿일 때 이름 없이 "내 사이트 요즘 어때"라고
하면 어느 것인지 정해지지 않는다. `stage.pick_project()` 로 판정하고(이 폴더의 리포
매치 > 사이트가 하나뿐이면 그것 > 못 고름), 못 고르면 **되묻는다 — 아무거나 집지 않는다**.
이 폴더에 사이트를 붙여 두려면 `/create profile {P}` 한 번(설명: setup 스킬 3-c).

**원격(호스팅) 사이트** — 웹에 등록해 두고 연결한 사이트(`setup` 스킬의 "명령어로
연결하기")는 이름으로 갈린다. 명령은 바뀌지 않는다: 같은 명령이 서버에서 돌고,
서버가 찍은 그 결과를 그대로 받아 적는다 — 여기서 요약을 다시 만들지 않는다.
달라지는 것만:
- **dry-run 이 없다.** 비용을 서버가 내므로 계획만 보는 모드가 없다 — 그렇게 부르면
  한 줄 알리고 끝난다. "계획만 봤다"고 믿고 다음 명령을 치지 않게 한다.
- **`/capture ask` 는 서버 brain 에 묻는다.** 로컬 sql 대신 서버 조회 통로로 가고,
  가드(읽기 전용·SELECT/WITH)와 결과 모양은 그대로다.
- **자료의 정본은 서버 brain 이다.** 런이 끝나면 로컬에도 사본이 남지만, 근거는
  서버 것이다 — 웹 화면과 다른 숫자를 말하지 않는다.

미설정·의존성 오류가 감지되면 추측하지 말고
setup 스킬의 doctor(`../setup/scripts/doctor.py`)를 먼저 돌려 진단 기반으로 안내한다.
상세 절차 문서는 `references/setup.md`.

## 명령 워크플로우

사용자가 `/capture <cmd>`라고 하거나 같은 의미로 말하면 아래를 수행한다.
`{P}` = 프로젝트 이름.

### /capture add {P} — 프로젝트 온보딩
**이미 등록된 이름이면**(대시보드 설정 폼으로 만든 경우) 1~4단계는 건너뛰고 5단계만
한다 — yaml을 덮어쓰지 말 것. 비어 있는 건 AI 프롬프트뿐이다. 단, 폼은
`gsc_property` 를 도메인에서 추정하므로 **2번의 대조는 한 번 해 준다**.

1. **인터뷰는 3문항이다** — 타입(game|local_clinic|saas|directory), 도메인,
   시드 키워드 3~10개. `projects/_presets.yaml`에서 타입 프리셋을 읽고 그 각도에
   맞춰 질문을 구체화한다. 나머지는 **묻지 않는다**:
   - `locale` — 도메인·사용자가 쓰는 언어에서 추론하고 한 줄로 확인만 받는다.
   - `tools` — directory·saas 타입에서만 묻는다. 이 두 타입은 비면 남의 브랜드
     카탈로그가 비어서 striking_distance에 노이즈가 흘러든다 (`scoring.md` 1a).
     game·local_clinic은 해당 없음.
   - `brand_aliases`·`competitors_manual` — 여기서 묻지 않는다. `/capture keywords`
     단계에서 실제 후보 목록을 보면서 채운다. 빈 화면에 대고 답하는 것보다
     그때 답이 정확하고, 값을 보기 전에 물으면 그냥 마찰이다.
   - `gsc_property` — **아래 2번이 정한다. 사용자에게 표기를 묻지 마라.**
     (`sc-domain:` vs `https://` 를 처음 하는 사람이 구분할 이유가 없고, 틀리면
     첫 수집이 빈손·403으로 끝난다.)
2. `gsc_property` 정하기 — **실제 목록에서 고른다**:
   - 구글이 이미 연결돼 있으면 `python scripts/gsc_query.py properties` 로 속성
     목록을 뽑아 도메인이 맞는 것을 **그대로** 쓴다. 여러 개면 사용자에게 번호로
     고르게 한다.
   - 아직 연결 전이면 `sc-domain:{도메인}` 으로 채워 두고(대시보드 폼과 같은 규칙),
     **로그인 직후 위 명령으로 대조해** 다르면 yaml을 고치고 3번을 다시 돌린다.
     추정이 틀리는 건 흔하다 — URL-prefix 속성(`https://example.com/`)만 가진
     계정에는 `sc-domain:` 이 아예 없다.
3. `projects/_template.yaml`을 복사해 `$CAPTURE_HOME/projects/{P}.yaml` 작성.
4. `python scripts/db.py sync-project $CAPTURE_HOME/projects/{P}.yaml`
5. 프리셋의 ai_prompt_templates를 프로젝트 맥락으로 치환해 AI 프롬프트 10~30개 초안 생성,
   사용자 검수 후 ai_prompts에 INSERT (scoring.md 5절의 파이썬 패턴 사용, is_active=1).
   손으로 짓기 어려우면 `python scripts/gen_prompts.py --project {P} --dry-run` 이
   사이트의 업종·GSC 상위 검색어를 재료로 초안을 뽑아 준다(OpenRouter 호출 1회).
   검수 후 `--dry-run` 없이 다시 돌리면 저장된다 — 이미 있는 질문은 안 건드린다.
   **호스팅 대시보드에는 [AI 인용] 화면의 [질문 만들기] 버튼이 이 자리다**(웹에는
   명령을 칠 채팅이 없다).

등록이 끝나면 **구글 연결 여부로 갈린다** — 첫 수확을 로그인 뒤로 미루지 않는다:

- **연결됨** → 곧바로 `/capture run {P}` 풀런을 제안한다.
- **미연결(로그인 대기·인증 없음)** → 먼저 `/capture keywords {P}` 를 돌린다.
  자동완성은 인증도 키도 안 쓰는 유일한 수집이라 **지금 당장** 후보 수십 개가
  나온다. 그 목록을 보여준 다음에 로그인을 요청한다. 로그인 전에 풀런을 돌리면
  `run_all.py` 가 1단계에서 중단되어 사용자는 빈손으로 첫 런을 끝낸다.

(등록 인터뷰 자체는 사람이 답해야 하므로 자동화 대상이 아니다.)

### /capture keywords {P} — 키워드 유니버스 (무료 파이프라인)
**풀런(`/capture run`)에 포함된다** — 3단계(`keywords`).

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
**풀런(`/capture run`)에 포함된다** — 1단계(`gsc`). `gsc` 가 실패하면 풀런은
거기서 멈춘다(나머지가 그 데이터를 재료로 쓰기 때문).

경로는 하나다 — **구글 계정 연결(필수)**: `python scripts/collect_gsc.py --project {P}`.
연결 한 벌로 모든 사이트를 자동 수집한다(행 제한 사실상 없음). 한 번 돌면 세 벌이
들어온다:

1. **query×page 스냅샷**(`gsc_snapshots`) — 기간 합계. 기존 기회 판정의 기본 재료.
2. **날짜별 추이**(`gsc_daily`) — 같은 창을 하루 단위로. 합계만 있던 시절엔
   "언제부터 떨어졌나"에 답할 수가 없었다. 날짜 키로 **upsert** 한다 — GSC는 최근
   2~3일 값을 나중에 채우므로 다시 돌리면 그 며칠이 갱신된다.
3. **디바이스·국가 분해**(`gsc_breakdown`) — dim=device 로 쿼리별
   MOBILE/DESKTOP/TABLET, dim=country 로 국가별. 앞은 `device_gap` 기회의 유일한
   근거이고, 뒤는 [분석] 화면의 국가 축이다.

분해 축은 `--breakdown` 으로 바꾼다 — **콤마 구분 문자열**이다(`--breakdown device,country`).
기본값은 config `gsc_breakdown` 이 정본이고, `--breakdown ""` 이면 분해 수집을 아예 건너뛴다.
축 하나가 API 요청 하나다 — 줄이려면 config 에서 뺀다.

기본은 내 구글 계정 로그인(OAuth)이고, 로그인 한 번이면 소유한 속성이 전부 붙는다 —
**속성마다 권한을 주는 단계는 없다**(그건 무인 수집용 서비스 계정 갈래에만 있다).
아직 안 붙어 있으면 수집을 시도하지 말고 **구글 로그인 한 번**을 요청한다 —
**콘솔 클릭은 없다**(무료, 30초, 준비물 없음). 할 말의 정본은 doctor 명부
(`../setup/scripts/doctor.py` 의 `CAPABILITIES` → `구글 실적 읽기`)이고 절차의
정본은 setup 스킬의 "GSC 연결" 절이다 — 어느 쪽도 사용자에게 읽어주지 말고,
브라우저 로그인 창이 한 번 열린다는 것만 미리 말해준다.
(구버전 사이트별 OAuth 경로와 CSV 내보내기 임시 경로는 제거됐다 — 2026-08-18.)

연동돼 있으면 **즉석 질문**("어제 클릭 몇이야", "이 페이지 어떤 쿼리로 들어와")은
Brain 수집 없이 `scripts/gsc_query.py` 로 바로 조회해 답해도 된다 (JSON 출력):

```
python scripts/gsc_query.py properties
python scripts/gsc_query.py search  --project {P} --days 7 --dim query,page --limit 25
python scripts/gsc_query.py search  --project {P} --days 3 --dim date
python scripts/gsc_query.py search  --project {P} --filter page:contains:/blog/
python scripts/gsc_query.py compare --project {P} --days 28
python scripts/gsc_query.py inspect --project {P} https://example.com/a
python scripts/gsc_query.py sitemaps --project {P}
```

리포트·스코어링·추세 비교는 여전히 `collect_gsc.py`로 Brain에 적재한 스냅샷 기준.
즉석 조회는 **아무것도 저장하지 않는다** — 근거로 쓸 땐 그 사실을 밝힌다(철칙 1).

완료 후 striking-distance 프리뷰를 요약해준다.

### /capture index {P} — 색인 상태 적재 (구글 URL Inspection, 돈 안 듦)
**풀런(`/capture run`)에 포함된다** — 2단계(`index`).

`python scripts/collect_index.py --project {P} --dry-run` → 검사할 URL 수 고지 → 확인 →
실행. 인증은 `/capture gsc` 와 **같은 구글 연결을 그대로 쓴다** — 따로 붙일 것이 없다.

**언제 도나** (매일 돌 명령이 아니다 — 색인 상태는 하루 단위로 요동치지 않는다):
- `rank_snapshots.position` 이 여러 번 돌아도 계속 NULL 인 키워드가 있을 때
- 페이지를 낸 지 며칠 지났는데 GSC 노출이 안 붙을 때
- robots·canonical·사이트 구조를 건드린 직후 (내가 뭘 막았는지 확인)

**비용:** 돈은 안 나가지만 **URL 당 API 1콜**이다. 한 번에 검사할 수는 config
`index_urls`(기본 20)이고 `--limit N` 으로 덮는다. `index_urls: 0` 이면 이 명령을 끈다.
재실행 안전망: 같은 날 두 번 돌면 `(프로젝트, 검사일, URL)` 로 덮어쓴다 — 행이 늘지
않고 최신 판정만 남는다.

**결과:** URL별 verdict·coverage_state·robots_txt_state·page_fetch_state·indexing_state·
구글이 고른 canonical vs 내가 선언한 canonical·마지막 크롤 시각·리치결과 판정(JSON)이
`gsc_index_status` 에 적재된다.
그다음 `python scripts/scoring.py load {P}` 가 이걸 읽어 **index_blocked** 기회를
만든다 — `robots_blocked | fetch_error | canonical_mismatch | not_indexed` 네 버킷
중 하나로 갈리고(`references/scoring.md` 1절·1d절), target 은 URL 이다.
**색인이 막힌 URL 은 제목·본문을 고쳐도 소용이 없다** — 같은 URL 에 걸린 다른 기회보다
먼저 처리하라고 안내한다.

### /capture rank {P} — 순위 스냅샷 (SERP, 키 있을 때)
**풀런(`/capture run`)에 포함된다** — 4단계(`rank`). 키가 없으면 풀런은 이 단계를
조용히 건너뛴다(에러 아님).

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
**순위 문제가 아니라 인덱싱 문제일 수 있다**. 미인덱스면 노리는 페이지 자체가
검색에 없으니 순위를 따져도 의미가 없다 (`scoring.md` 4절 — `RANK_NOISE`도
NULL엔 적용되지 않는다).

확인은 두 갈래로 역할이 갈린다:
- **반복해서 볼 것은 `/capture index`** — 결과가 `gsc_index_status` 에 남아 날짜별로
  비교되고 `index_blocked` 기회로 올라온다. 여러 URL·다음 런과의 대조는 전부 이쪽이다.
- **`gsc_query.py inspect` 는 단발 즉석 확인용** — "이 URL 지금 색인됐어?" 한 번
  물어보는 자리(여러 URL 을 한 줄에 이어 줄 수 있다). 저장되지 않으므로 이걸로
  본 결과를 근거로 삼을 땐 Brain 이 아니라 즉석 조회임을 밝힌다(철칙 1).

### /capture ai {P}
**풀런(`/capture run`)에 포함된다** — 5단계(`ai`). 키가 없으면 풀런은 이 단계를
조용히 건너뛴다(에러 아님).

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

OPENROUTER_API_KEY 미설정이면 이 명령은 돌지 않는다 — 키가 필요하다고 말하고
(`setup.md` 5절) 나머지 기능은 그대로 됨을 알린다.

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
**풀런(`/capture run`)에 포함된다** — 7단계(`gaps`). 그 단계가 실제로 하는 일은
`scoring.py load {P}` **하나뿐이다**(외부 호출 0건). 따로 부를 때만 직접 실행한다.

`scoring.py load` 가 적재하는 기회는 **기계 판정분 8종**이다 — striking_distance ·
ctr_gap · cannibalization · rank_decay · pseo_pattern · device_gap · index_blocked ·
coverage.

**풀런이 대신 해 주지 않는 것 셋** (기대하고 기다리면 안 나온다):

- `ai_citation_gap` — sql 로 cited=0 체크의 `cited_domains_json` 빈도와 미노출
  프롬프트를 내가 직접 뽑는다.
- `aio_exposure` — rank 데이터가 있을 때 `aio_present=1 AND aio_cited=0` 키워드.
  역시 sql.
- `content_gap` — **적재는 풀런의 `competitors` 단계가 한다**(DataForSEO 키가
  있을 때). 다만 키가 없거나 그 사이트에 등록된 경쟁사가 아직 없으면 그 단계는
  조용히 건너뛰므로 후보가 하나도 안 생긴다 — 그때는 `/capture gap {P}` 로 먼저
  적재한다(`--domain` 으로 도메인을 직접 줄 수도 있다). 어느 쪽으로 들어왔든
  후보를 정리하고 기회로 판정하는 것은 `scoring.py load` 가 아니라 내 몫이다.

`pseo_pattern` 은 기계가 후보만 올린다 — 고노출·저CTR 쿼리를 변수 슬롯
({지역}·{기온}·{시술} 등) 하나만 다른 템플릿으로 묶는 판단과 가드레일은
`references/scoring.md` 1b절.

### /capture run {P} — 풀런
수집부터 리포트까지를 스크립트 한 번에 끝낸다. 단계 순서는 고정이다 —
`scripts/run_all.py` 의 `STAGES` 표가 정본이고, 거기 적힌 순서대로 전 단계를 부른다.

1. 먼저 `python scripts/run_all.py --project {P} --dry-run` 으로 각 수집기의
   호출 수·비용 계획을 모아 보여주고 사용자 확인을 받는다 (철칙 2 — 비용 고지).
   유료 축은 **셋**이고, 각각 무엇에 돈이 나가는지가 다르다:
   - `rank` — SERP 조회 1건당 과금(`SERPER_API_KEY` 또는 `DATAFORSEO_LOGIN`/
     `DATAFORSEO_PASSWORD`). 추적 키워드 수에 비례한다.
   - `ai` — AI 엔진 답변 1건당 과금(`OPENROUTER_API_KEY`). 프롬프트 × 엔진 ×
     샘플 수에 비례한다.
   - `competitors` — DataForSEO Labs `ranked_keywords/live`, 경쟁사 **도메인
     한 곳당** 과금(~$0.001). 도메인은 상한 5개라 한 바퀴에 **~$0.005** 다.
     키는 `rank` 와 같은 DataForSEO 자격을 쓴다.
   셋 다 키가 없으면 **조용히 건너뛴다** — 에러가 아니고, 키가 없다고 사용자에게
   되묻지 않는다. `competitors` 는 키가 있어도 등록된 경쟁사가 없으면 같은 톤으로
   건너뛴다.
2. 확인되면 `python scripts/run_all.py --project {P}` 를 **백그라운드로** 돌린다.
   수집이 길어질 수 있어 포그라운드로 잡으면 세션이 막힌다 — `/capture dash` 가
   같은 이유로 백그라운드인 것과 같은 톤이다.
3. **`gsc` 가 실패하면 거기서 멈춘다** — 나머지 전부(gaps, report, 의사결정)가
   그 데이터를 재료로 쓰기 때문이다. 다른 단계는 하나 실패해도 나머지가 계속
   간다.
4. 끝나면 단계별 `완료 / 건너뜀(이유) / 실패(이유)` 표와 리포트 파일 경로를
   찍는다. exit 0 = 전부 성공(건너뜀 포함), 1 = 하나라도 실패.
5. 체인이 끝난 뒤 Claude가 하는 일은 그대로다 — 아래 "분석 단계"의 2·3번
   (기회 reasoning 보강, pseo 군집 판단, Next Actions). 그건 사람 판단이라
   스크립트가 못 한다.

`--only gsc,gaps` · `--skip index,ai` 처럼 한 축만 빼거나 골라 돌릴 수도 있다.
축 이름은 아래 개별 명령과 같다.

### 분석 단계 (gaps 이후 자동)
1. `python scripts/scoring.py load {P}` — 기계 판정분 8종(위 `/capture gaps` 절의
   목록이 정본)을 결정적 점수·수치 reasoning과 함께 opportunities에 적재한다.
   `/capture run` 으로 들어온 경우 `run_all.py` 의 `gaps` 단계가 이걸 대신
   부른다 — **다시 부르지 않는다** (중복 실행). `/capture gaps` 를 따로 부른
   경우에만 직접 실행한다.
2. 적재된 기회를 sql로 검토하고 상위 10개의 reasoning을 보강한다 — 원인 가설·
   fit 판단·맥락 (`references/scoring.md` 2~3절).
3. pseo_pattern 군집 등 Claude 판단이 필요한 kind는 scoring.md 1b·5절대로 추가
   적재 → Next Actions 3~5개를 JSON 파일로 저장.

### /capture pages {P} — 내 페이지 감사 (내 사이트 직접 조회, 돈 안 듦)
**풀런(`/capture run`)에 포함된다** — 8단계(`pages`, gaps 다음).

`python scripts/collect_page.py --project {P} --dry-run` → 가져올 URL 목록 확인 →
실행. 남의 API 가 아니라 **내 페이지를 열어 보는 것**이라 키가 필요 없다.

**무엇을 읽나:** title · meta description · H1/H2 · 본문 단어 수(script·style 제외) ·
ld+json 의 @type · canonical · meta robots · 내부/외부 링크 수 · alt 없는 이미지 수.
`page_audits` 에 `(프로젝트, 검사일, URL)` 로 적재된다 — 같은 날 두 번 돌아도 행이
늘지 않는다.

**대상 URL:** 기회에 걸린 검색어의 페이지(노출 상위 2개) → 노출 상위 페이지 순으로
`page_urls`(기본 20)개. `--limit N` 으로 덮고, `page_urls: 0` 이면 끈다. 요청 간격은
`throttle`(기본 0.5초) — 내 서버를 두드리는 속도다.

**왜 필요한가:** 이것 없이는 처방이 일반론에서 멈춘다("제목을 고치세요"). 이 단계가
돌고 나면 화면이 **"지금 title 이 X 인데 검색어 Y 가 없다 → 앞부분에 넣어라"** 라고
말한다. 판정 규칙의 정본은 `scoring.page_advice` 이고, 대시보드는 그 결과를 그리기만
한다 — 화면이 같은 규칙을 다시 구현하지 않는다.

### /capture dash {P} — 로컬 대시보드
`python scripts/dashboard.py --project {P} --open` 을 **백그라운드로** 띄운다
(포그라운드로 돌리면 세션이 막힌다). 127.0.0.1 전용 웹 UI로 Brain을 실시간
조회하고, 기회 상태(확인/완료/기각)를 표에서 바로 갱신한다.
**dash와 report는 같은 화면이다.** dash는 서버가 Brain을 실시간으로 읽는 모드,
report는 그 화면을 그날 데이터째 파일로 박제한 모드(`--export`). 지금 상태를 보고
손댈 때는 dash, 남겨 두거나 남한테 보낼 때는 report.
헤더 [설정] 버튼에 온보딩 패널(부품 설치·사이트 등록·구글 연결 파일·API 키)이 있다 —
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
"지금·어제·이번 주"처럼 최신을 묻는 질문은 Brain 대신 `gsc_query.py` 즉석 조회로
답한다 (Brain 스냅샷은 3일 전까지다 — 철칙 1의 표).

## 외부 마케팅 스킬 위임

측정은 여기서, **해석과 각도는 전문 스킬에**. 설치돼 있으면 아래 시점에 넘긴다.
빠진 게 있으면 **빠진 걸 알리고 대신 설치해 드릴지 물은 뒤** 사용자가 승낙하면
`../../setup/scripts/install_skills.py` 를 실행한다 (성공 후 Claude Code 재시작이
한 번 필요함을 같이 알린다). 거절하거나 넘어가면 내장 규칙
(`references/scoring.md`·`../create/references/content-rules.md`)으로 진행한다 —
그때는 결과물에 "내장 규칙으로 처리했습니다"를 한 줄 남긴다. 같은 세션에서
두 번 조르지 않는다. 계약 전문: `references/external-skills.md`.

| 시점 | 부를 스킬 | 넘기는 근거 |
|---|---|---|
| `/capture keywords` 큐레이션 직후 | `content-strategy` | 활성 키워드 + 노출·순위 |
| `/capture gaps` 가 `index_blocked`·`coverage`·`rank_decay` 로 몰릴 때 | `seo-audit` | `gsc_index_status` 행, 하락 키워드 |
| `ai_citation_gap` 이 있을 때 | `ai-seo` | `ai_checks` 답변 전문·인용 도메인 |
| `cannibalization` — 같은 쿼리에 여러 URL | `site-architecture` | 겹치는 URL 쌍의 노출·순위 |
| 앱 스토어 리스팅을 가진 프로젝트 | `aso` | 스토어 URL, 경쟁 앱 |

산문 결과물은 `$CAPTURE_HOME/docs/{P}/` 에 남긴다(`content-plan.md` 등) — 안 남기면
다음 런에서 또 없다. 외부 스킬이 준 수치는 Brain 근거가 아니다: 출처를 밝힌다.

## 스코프 밖 (요청받아도 이 스킬로는 하지 않는 것)

콘텐츠 초안 작성·브리프 핸드오프(create 스킬의 영역), 네이버 표면(미탑재).
부탁받으면 한계를 밝히고 대안을 안내.
