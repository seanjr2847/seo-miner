---
name: create
description: capture의 실행 짝 — Brain의 기회(striking_distance, ai_citation_gap, pseo_pattern, rank_decay)를 어떤 리포/스택(Astro, Next, Hugo, Jekyll, 순수 HTML, 데이터 파일 디렉터리)에서든 리포 자체 관례를 먼저 파악해 실제 콘텐츠 변경으로 바꾼다. 사용 시점 — 기회 실행, SEO/AEO 콘텐츠 작성·리프레시, pSEO 페이지 생성, "/create ...", "기회 반영해줘", "그 키워드로 글 써서 리포에 넣어줘", "pSEO 페이지 만들어줘", "이 글 리프레시해줘". capture 없이도 수동 브리프로 단독 동작.
---

# create — 스택 불문 콘텐츠 실행 엔진 (capture의 Create 카운터파트)

capture가 찾은 기회를 리포 안의 실제 파일 변경으로 바꾼다. 발행 = 파일 쓰기 + git.
스택별 지식은 이 스킬에 없다 — **리포에서 발견해 프로필로 캐싱**하고, 그 관례를 따른다.

## 철칙

1. **발견 우선.** 프로필 없는 프로젝트에서 콘텐츠를 쓰지 않는다. 먼저 `/create profile`.
2. **관례 모방.** frontmatter 키, 파일명 규칙, 디렉토리, 컴포넌트 사용은 리포의 기존
   파일에서 관찰한 것만 쓴다. 새 키를 발명하지 않는다.
3. **발행 게이트.** main에 직접 쓰지 않는다. 반드시 브랜치 `capture/{kind}-{slug}` →
   커밋(메시지에 opportunity id) → PR(또는 push 후 안내). 머지는 사람이 한다.
   이름이 이 형식이 아니면 `createdb.py done` 이 경고만 남기고 기록은 그대로 한다.
   git 리포가 아니면: 변경 전 사본 백업 + 변경 파일 목록 보고로 대체.
4. **데이터 진실성.** 콘텐츠 속 수치·주장에 근거가 필요하다. Brain 수치는 인용 표기,
   제품 주장은 리포·문서에서 확인된 것만, 없는 사실은 만들지 않는다.
5. **루프 클로즈.** 작업 완료 시 `scripts/createdb.py done`으로 Brain에 기록해
   다음 capture 런이 이 콘텐츠의 성과를 측정하게 한다.
   **이 스킬을 안 거치고 손으로 고친 것도 똑같이 기록한다.** 커밋 메시지에
   `[opp #id]` 규약을 남겨 두고 plan 시작 시 `createdb.py sync {P} --repo .` 로
   일괄 반영한다. 안 그러면 Brain은 영영 모른다 — 다음 런이 같은 기회를 또 제안하고,
   대시보드는 "실행 0건"이라고 말한다.

## 답변 형식 (채팅으로 나가는 모든 말)

한 줄 결론 → 근거 표 → **다음 한 걸음 하나** → 접어 둔 "안 한 것".
정본은 `../setup/references/reply-format.md` — 작업을 끝내고 보고하기 전에 그 뼈대를
따른다. 근거 칸은 실제로 바꾼 것만 적는다: 변경 파일·브랜치·PR·닫은 기회 id
(철칙 4·5). 쓰지 않은 파일을 근거에 올리지 않는다.

## 명령 워크플로우

### /create profile {P} — 리포 프로필 발견 (프로젝트당 1회, 결과는 캐시)
1. 사용자에게 리포 경로를 받는다. `references/repo-profiling.md`의 휴리스틱으로
   스택·콘텐츠 위치·frontmatter 스키마·URL 패턴·발행 모드를 탐지한다.
2. 기존 콘텐츠 2~3개를 읽어 보이스 특징(어조, 문장 길이, 이모지/존댓말 여부,
   제목 스타일)을 요약한다. `$CAPTURE_HOME/docs/{P}/positioning.md`(setup 이 만든 것)와
   리포 안의 `product-marketing-context.md`류 파일이 있으면 그쪽을 우선한다.
3. 결과를 `$CAPTURE_HOME/projects/{P}.repo.yaml`로 저장하고 사용자 검수를 받는다.
   (템플릿: `templates/repo-profile.template.yaml`)
   **`repo_path` 는 절대경로로, 반드시 채운다** — `db.repo_project()` 가 이 값으로
   "이 폴더가 어느 사이트냐"에 답한다. 비어 있거나 템플릿 그대로면 사이트가 여럿인
   사용자는 `/setup` 을 돌릴 때마다 어느 사이트인지 되묻는 화면을 본다.

### /create plan {P} — 작업 계획
1. `python scripts/createdb.py pick {P}` 로 Brain의 status가 'new'·'acked' 인 기회를
   읽는다(확인만 해 둔 기회도 아직 할 일이다 — 'acked' 를 먼저 보여준다).
   Brain이 없거나 비었으면 수동 브리프 모드로 전환(무엇을 쓸지 인터뷰).
1-b. **쓰기 전에 리포와 대조.** plan 시작 시 `python scripts/createdb.py sync {P} --repo .`
   를 먼저 실행해 커밋 메시지 `[opp #id]` 규약으로 이미 반영된 기회를 done으로 닫고
   목록에서 뺀다. 이 대조 없이 쓰면 남이 방금 손본 문구를 덮어쓴다.
2. kind별 레시피(`references/content-rules.md`)에 따라 작업 배치를 제안:
   대상 파일(신규/수정), 예상 분량, pSEO면 1차 롤아웃 규모. 사용자 승인 후 진행.
3. 승인된 항목은 `createdb.py claim {P} <ids>` 로 status='acked' 처리.

승인이 떨어지면 곧바로 `/create run {P}` 으로 이어간다 — 사용자가 두 번
치지 않게. **발행 게이트(철칙 3)는 그대로다** — 자동 이어지기는 "승인 이후
run 까지"이지 "승인 없이 PR"이 아니다. main 직접 쓰기 금지, 브랜치 → 커밋 → PR,
머지는 사람.

### /create run {P} — 실행
(`/create plan` 의 승인을 받아 이어지는 자리다 — 단독으로 부를 수도 있다.)

1. 브랜치 생성 → 항목별로 content-rules.md 레시피대로 파일 생성/수정.
2. 수정(striking_distance, rank_decay)은 **최소 diff** — 전면 재작성 금지.
3. 항목당 커밋: `capture(<kind>): <요약> [opp #id]`.
4. PR 생성(가능하면 gh CLI) — 본문에 기회의 reasoning과 Brain 수치 근거를 붙인다.
   publish_mode가 files가 아니면(외부 CMS): `$CAPTURE_HOME/drafts/{P}/`에 산출 후
   사용자 파이프라인(n8n 등)으로 넘기라고 안내한다.
5. `createdb.py done {P} <id> --path <파일> --branch <브랜치>` 로 기록.

### /create status {P}
`createdb.py list {P}` 로 작성 이력·머지 여부를 보여주고, 머지된 건
`createdb.py merged <creation_id>` 로 갱신을 제안한다.

## 글 품질 위임 — 외부 마케팅 스킬

전문 절차 팩이 설치돼 있으면 그 지침을 함께 적용한다. 빠진 게 있으면 **빠진 걸
알리고 대신 설치해 드릴지 물은 뒤** 사용자가 승낙하면
`../../setup/scripts/install_skills.py` 를 실행한다 (성공 후 Claude Code
재시작이 한 번 필요함을 같이 알린다). 거절하거나 넘어가면
`references/content-rules.md` 의 내장 AEO/SEO 구조 규칙으로 진행한다 — 그때는
결과물에 "내장 규칙으로 처리했습니다"를 한 줄 남긴다. 같은 세션에서 두 번
조르지 않는다. 계약 전문: `../capture/references/external-skills.md`.

| 시점 | 부를 스킬 | 넘기는 것 |
|---|---|---|
| `pseo_pattern` 기회를 plan 에서 집을 때 | `programmatic-seo` | 패턴(템플릿 축)·1차 롤아웃 규모·데이터 소스 |
| `ai_citation_gap` 기회를 쓸 때 | `ai-seo` | 갭 프롬프트, 지금 인용되는 도메인 |
| 페이지를 새로 내거나 리프레시할 때 (항상 후보) | `schema` | 페이지 종류 + **리포의 기존 구조화 데이터 방식** |
| 무엇을 먼저 쓸지 순서를 못 정할 때 | `content-strategy` | Brain 기회 목록 + 노출·순위 |
| 페이지를 어디에 붙일지(허브·경로) 애매할 때 | `site-architecture` | 기존 URL 구조, 겹치는 페이지 |
| 스토어 리스팅 문구를 쓸 때 | `aso` | `docs/{P}/aso.md`, 경쟁 앱 |

**리포 관례가 외부 스킬 권고를 이긴다** (철칙 2). `schema` 가 JSON-LD 를 권해도
리포가 마이크로데이터를 쓰고 있으면 리포를 따른다 — 새 방식을 이 페이지 하나에
들이지 않는다.

`$CAPTURE_HOME/docs/{P}/positioning.md` 가 있으면 **먼저 읽는다** — 주장·대상·
쓰는 말이 거기 있고, 그게 리포 보이스와 함께 글의 기준이 된다 (setup 이 만든다).

## 스코프 밖

배포 인프라 조작(머지·프로덕션 배포는 사람), 측정(capture의 영역),
소셜·이메일 배포(Expand 영역 — 요청 시 한계를 밝히고 대안 안내).

**콘텐츠가 파일이 아니라 DB에 사는 리포**도 이 스킬의 편집 모델 밖이다. 예:
aitierlist는 도구 요약·티어 근거가 Postgres에 있어 도구 상세 페이지의
striking_distance는 파일 diff로 못 고친다. 이런 프로젝트에서는 파일로 존재하는
면(홈·허브·정적 페이지)만 이 스킬로 처리하고, DB 쪽은 한계를 밝힌 뒤 리포의
자체 스크립트·어드민을 쓰도록 안내한다 — 프로필의 `publish_mode`에 적어 둔다.
