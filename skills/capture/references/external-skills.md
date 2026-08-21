# 외부 마케팅 스킬 위임 — 언제, 무엇을 넘기고, 없으면 어떻게

seo-miner 는 **측정하고 기회를 만드는 도구**다. 그 기회를 "어떤 각도로 쓸지"는
전문 절차 팩(marketing skills)이 훨씬 낫다. 그래서 seo-miner 는 그 지식을 자기
안에 복제하지 않고 **위임한다**.

이 스킬들은 seo-miner 가 동봉하지 않는다 — 사용자 환경에 이미 설치돼 있으면 쓰고,
없으면 내장 규칙으로 계속 간다. **없다고 멈추지 않는다.**

## 철칙 세 개

1. **근거는 Brain 이 준다.** 외부 스킬에 넘기는 건 언제나 조회 결과(수치·기회 행)다.
   "감으로 SEO 조언"을 받으러 가는 게 아니다. 넘길 때 어느 테이블·어느 스냅샷에서
   나온 숫자인지 같이 준다.
2. **결과는 Brain 이나 문서로 되돌아온다.** 위임한 결과가 아무 데도 안 남으면 다음
   런에서 또 없다. 산문 결과물은 `$CAPTURE_HOME/docs/{사이트}/` 에, 실행으로 바뀐
   것은 `createdb.py done` 으로 기회를 닫아 남긴다.
3. **한 번에 하나.** 기회 하나에 스킬 셋을 겹쳐 부르지 않는다 — 아래 표의
   "트리거"가 실제로 걸린 것만 부른다.

## 위임 표

| 스킬 | 언제 부르나 (트리거) | 넘기는 것 | 없으면 |
|---|---|---|---|
| `product-marketing` | `/setup` 온보딩 마지막, 사이트 등록 직후 1회 | 인터뷰 답변(타입·도메인·시드 키워드·경쟁사·도구 이름) | 인터뷰 요약만 문서로 저장 |
| `aso` | 프로젝트가 앱 스토어 리스팅을 가질 때 (`/setup` 온보딩, 또는 사용자가 앱 URL을 줄 때) | 앱 스토어/구글 플레이 URL, 경쟁 앱 | 건너뛴다 (웹 SEO 경로만) |
| `seo-audit` | `/capture gaps` 이후 기회가 `index_blocked`·`coverage`·`rank_decay` 쪽으로 몰릴 때 | `gsc_index_status` 행, 하락 키워드 목록, 사이트 URL | `references/scoring.md` 판정만으로 안내 |
| `ai-seo` | `ai_citation_gap` 기회가 있을 때, 또는 `/capture ai` 결과에서 우리가 인용되지 않을 때 | `ai_checks` 의 답변 전문·인용 도메인, 갭 프롬프트 목록 | `content-rules.md` 의 AEO 구조 규칙 |
| `content-strategy` | `/capture keywords` 큐레이션 직후 — 키워드를 토픽 클러스터로 묶을 때 | 활성 키워드 목록(+ 노출·순위), 사이트 타입 | 키워드를 그대로 목록으로 둔다 |
| `site-architecture` | `cannibalization`·`coverage` 기회, 또는 같은 쿼리에 여러 URL 이 물릴 때 | 겹치는 URL 쌍과 각각의 노출·순위 | 카니벌라이제이션 판정만 보고 |
| `programmatic-seo` | `pseo_pattern` 기회를 `/create plan` 에서 집을 때 | 패턴(템플릿 축), 1차 롤아웃 규모, 데이터 소스 | `content-rules.md` 의 pSEO 레시피 |
| `schema` | `/create run` 이 페이지를 새로 낼 때·리프레시할 때 (항상 후보) | 페이지 종류(리뷰·비교·목록·글), 리포의 기존 구조화 데이터 방식 | 리포에서 관찰된 기존 방식만 모방 |

## `$CAPTURE_HOME/docs/{사이트}/`

| 파일 | 만드는 곳 | 읽는 곳 |
|---|---|---|
| `positioning.md` | `/setup` → `product-marketing` | capture(AI 프롬프트 작성), create(보이스·주장) |
| `aso.md` | `/setup` → `aso` | create(스토어 리스팅 문구) |
| `content-plan.md` | `/capture keywords` → `content-strategy` | create(무엇을 먼저 쓸지) |

파일이 없으면 없는 대로 간다 — 이 문서들은 **품질을 올리는 재료**이지 준비물이 아니다.
오래된 문서는 사이트가 크게 바뀐 뒤에만 다시 만든다(매 런마다 갱신하지 않는다).

## 하지 말 것

- 외부 스킬이 준 수치를 Brain 근거처럼 말하기. 출처를 밝힌다(철칙 1은 capture/SKILL.md).
- 스킬이 없다고 사용자에게 설치를 요구하기. 없으면 내장 규칙으로 하고, 있으면 좋다는
  것만 한 줄로 알린다.
- 측정 없이 위임하기. `/capture gsc` 조차 안 돈 사이트에 `seo-audit` 을 부르면
  그 진단은 재료 없이 나온 추측이다.
