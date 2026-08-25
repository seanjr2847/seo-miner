# 외부 마케팅 스킬 위임 — 언제, 무엇을 넘기고, 없으면 어떻게

seo-miner 는 **측정하고 기회를 만드는 도구**다. 그 기회를 "어떤 각도로 쓸지"는
전문 절차 팩(marketing skills)이 훨씬 낫다. 그래서 seo-miner 는 그 지식을 자기
안에 복제하지 않고 **위임한다**.

## 준비물 — 외부 마케팅 스킬

seo-miner 의 위임은 사용자 환경에 그 스킬이 설치돼 있어야 제대로 동작한다. 아래
표가 그 준비물이다(`aso` 는 앱 스토어 리스팅이 있는 사이트에만 해당 — **선택**).

**개수를 세는 정본은 `../../setup/scripts/doctor.py` 의 `ALL_SKILLS` 하나다** —
doctor 도 `install_skills.py` 도 거기서 세고 거기서 설명을 읽는다. 산문이 숫자를
따로 타이핑하면 어긋난다(실제로 목록은 필수+선택을 나열하면서 모수는 필수만 세어
"7개 중 8개가 설치되어 있지 않습니다"가 찍혔다).

설치 명령 (어느 한 쪽, 같은 결과):

```
claude plugin marketplace add coreyhaines31/marketingskills
claude plugin install marketing-skills@marketingskills
```

또는 setup 스킬이 같은 일을 해 준다 — 채팅에 "마케팅 스킬 설치해줘"라고 하거나
`/setup web` 의 [설정] → [5. 마케팅 스킬] 버튼을 누르면 됩니다. 그 스크립트는:

```
python ../../setup/scripts/install_skills.py
```

이 스크립트는 `python install_skills.py --check` 로 **어떤 게 빠졌는지만** 확인할
수도 있고, 인자 없이 돌리면 빠진 게 있을 때만 위 두 명령을 실행해 설치한다.
**스크립트는 동의 묻지 않는다** — 설치를 시작하기 전에 채팅(또는 버튼)이 사용자
승낙을 받아야 한다. 설치는 무료며, 끝나면 Claude Code 를 한 번 재시작해야 그
스킬이 현재 세션에 로드된다.

| 스킬 | 하는 일 | 부르는 시점 |
|---|---|---|
| `product-marketing` | 사이트 포지셔닝 문서 | `/setup` 온보딩 1회 |
| `seo-audit` | 기술·온페이지 진단 | 색인 차단·커버리지·순위 하락 기회 |
| `ai-seo` | AI 인용 갭 해석 | `ai_citation_gap` |
| `content-strategy` | 키워드 → 토픽 클러스터 | `/capture keywords` 큐레이션 직후 |
| `site-architecture` | 카니벌라이제이션·URL 구조 | `cannibalization` |
| `programmatic-seo` | pSEO 대량 생성 | `/create plan` 의 `pseo_pattern` |
| `schema` | 구조화 데이터 | `/create run` 의 페이지 발행 |
| `aso` | 앱 스토어 리스팅 | 앱이 있는 사이트만 (**선택**) |

출처: https://github.com/coreyhaines31/marketingskills. `/setup` 의 doctor 가
설치 여부를 보고, 빠진 게 있으면 사용자 승낙을 받아 `install_skills.py` 가
`claude plugin install` 로 대신 설치한다. 거절해도 작업은 계속되고 내장 규칙으로
채운다.

## 스킬이 없을 때

(a) **빠진 걸 알리고, 대신 설치해 드릴지 묻는다.** 어느 스킬이 빠졌는지 이름을
나열하고, "채팅에 마케팅 스킬 설치해줘 하시면 제가 대신 설치합니다 (무료) —
끝나면 Claude Code 재시작이 한 번 필요합니다" 라고 말한다. 사용자가 승낙하면
위 `python ../../setup/scripts/install_skills.py` 를 실행하고, 끝나면
재시작이 필요함을 알린다. **무엇을 깔게 되는지(저장소 주소)는 항상 같이
말한다** — 대시보드 버튼은 같은 자리에서 끝내지만, 화면이 닫힌 뒤엔
상위 출처(https://github.com/coreyhaines31/marketingskills) 가 사용자가
추적할 수 있는 유일한 기록이다.

(b) **사용자가 설치를 원치 않거나 지금은 넘어가겠다고 하면 내장 규칙으로 진행한다.**
작업을 거부하지 않는다. 측정과 기회 발굴은 끝까지 한다 — 각도가 내장 규칙의
것일 뿐이다.

(c) **그렇게 진행할 때는 결과물에 "이 부분은 내장 규칙으로 처리했습니다"를
한 줄 남긴다.** 나중에 "왜 이 부분은 품질이 다른가"라고 물으면 답할 수 있어야
한다.

(d) **같은 세션에서 두 번 이상 조르지 않는다.** 한 번 요구하고 답을 들었으면
그건 끝이다. 답이 없으면 (b) 로 간주한다.

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

- **허락 없이 설치하지 않는다.** 스크립트는 동의 묻지 않으므로, 채팅(또는
  대시보드 버튼)이 사용자에게 먼저 승낙을 받아야 한다 — 남의 컴퓨터에
  플러그인을 까는 일이라 이건 반드시 사용자가 결정한다.
- 외부 스킬이 준 수치를 Brain 근거처럼 말하기. 출처를 밝힌다(철칙 1은 capture/SKILL.md).
- **매번 조르기 / 없다고 작업을 거부하기.** 한 번 요구하고 답을 들었으면 끝이다.
  거절당했거나 넘어가면 내장 규칙으로 진행한다 — 그게 답이다.
- 측정 없이 위임하기. `/capture gsc` 조차 안 돈 사이트에 `seo-audit` 을 부르면
  그 진단은 재료 없이 나온 추측이다.
