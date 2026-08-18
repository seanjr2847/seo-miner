# 기회 스코어링 & 분석 규칙 (Claude 분석 단계 계약)

이 문서는 /capture 분석 단계에서 Claude가 따르는 규칙이다. 핵심 원칙:
**Brain에 없는 주장은 하지 않는다.** 모든 기회의 reasoning은 db.py sql로
조회한 실제 수치를 인용해야 한다.

**이 문서는 명세이고, 실행은 `scripts/scoring.py`다.** 산문으로만 적힌 규칙은
아무도 실행하지 않아 실제로 안 돌았다(1a절이 그랬다). 그래서 기계가 판정할 수
있는 것은 전부 scoring.py로 옮겼고, 여기서는 그 함수·상수 이름을 가리킨다.
임계값을 바꾸려면 scoring.py를 고치고 이 문서를 맞춘다. 남은 산문(1b절 2~3단계)
은 **코드가 아니라 Claude 판단**이며, 그렇다고 명시해 둔다. fit·인텐트는
코드가 기본값을 깔고 Claude가 보정만 한다(2절, 1c절).
자체 점검: `python scripts/scoring.py`.
기회 적재: `python scripts/scoring.py load <project>` (5절).

## 1. 기회 종류 (kind)

| kind | 정의 | 데이터원 / 실행하는 코드 |
|------|------|----------------------|
| striking_distance | GSC 4~20위 + 노출 유의미 → 밀면 상단 진입 | `scoring.striking()` — 구간은 `STRIKING_LO=4`·`STRIKING_HI=20`, 노출 하한 `STRIKING_MIN_IMP`(=10), 노출 내림차순. 각 행에 `band`(pos ≤ 10 → `page1`, 아니면 `page2`)와 `gap`(`gap_to_page1()`, `PAGE1=10`)이 붙는다 |
| ctr_gap | 1페이지(1~10위)인데 기대 CTR의 절반도 못 받음 → 제목·설명 문제 | `scoring.ctr_gaps()` — 기대치는 `EXPECTED_CTR`(1~20위, %, 업계 클릭 곡선 근사), 노출 하한 `CTR_GAP_MIN_IMP`(=100), 판정은 실제 CTR < 기대 × `CTR_GAP_FACTOR`(=0.5). 손실 클릭(노출×(기대-실제)) 내림차순 |
| cannibalization | 같은 쿼리에 내 페이지 2개 이상이 노출을 분산 | `scoring.cannibalization()` — DISTINCT page ≥ 2, 부페이지 노출 비중 ≥ `CANNI_MIN_SHARE`(=0.2), 합산 노출 ≥ `CANNI_MIN_IMP`(=50). **page 차원은 API 수집만 채운다** — CSV 임포트는 page가 NULL이라 빈 결과가 정상(결함 아님, 데이터 부재) |
| ai_citation_gap | 관련성 높은 프롬프트에서 타 도메인만 인용 | ai_checks: cited=0, cited_domains_json 빈도. 인용/언급 판정 자체는 `scoring.judge()` |
| rank_decay | 직전 스냅샷 대비 순위·클릭 하락 (방어) | `scoring.rank_decay()` — 비교 짝은 `snapshot_pair()`(같은 period_days끼리만), `dpos <= DECAY_POS`(= -1.5, 음수=하락), 하락 큰 순. 비교 짝이 없으면 빈 결과 |
| content_gap | 경쟁사는 잡는데 나는 부재 | 완전판 구현 — `scripts/collect_gap.py`(DataForSEO Labs 키 필요), 후보는 keywords 로 적재되고 기회 판정·클러스터링은 큐레이션 후 Claude. 부분 가능(무료): rank 수확 경쟁사가 내 추적 키워드 상위에 있고 나는 부재인 경우 |
| coverage | 활성 키워드가 GSC·순위 체크 어디에도 안 잡힘 (directory 최우선) | `scoring.coverage()` — '커버됨' = 최신 GSC 스냅샷에 같은 문자열(norm 비교) 쿼리가 노출>0으로 존재하거나 rank_snapshots 최신 체크에 position 존재. **부분 일치·의미 유사는 안 본다** — 그건 Claude 몫. load는 클러스터별 1건(target=`cluster:{이름}`)으로 적재 |
| pseo_pattern | 노출은 있는데 클릭이 없는 쿼리들이 템플릿 패턴을 이룸 → pSEO 캠페인 후보 | 아래 1b절 절차 (후보 추출은 `scoring.pseo_candidates()`, `load`가 상위 10개를 개별 후보로 선적재) |
| aio_exposure | AI 오버뷰가 뜨는 내 키워드에서 인용 미확보 | rank_snapshots: aio_present=1 AND aio_cited=0 (DataForSEO 제공 시). 도메인 추출은 `serp_adapter._domains_in()`이 인용 구조(`references`·`citations`·`sources`·`links`) 안의 `url`·`domain`·`link`·`source_url`만 채택 — `images[].url`(CDN)이나 본문 안 무연결 URL은 빠진다. 자기 도메인 판정은 `scoring.owns()`/`host_of()` |

기회 목록을 화면·리포트로 뽑을 때의 정렬은 `scoring.opportunities()` 하나뿐이다
(새 기회 먼저 → 점수 높은 순 → 최근 id 순). 정렬이 두 벌이던 시절엔 대시보드와
박제 리포트가 같은 데이터를 다른 순서로 보여줬다.

### 1a. striking_distance에서 먼저 걸러낼 것 — 남의 브랜드 검색

**쿼리가 카탈로그에 등재된 남의 도구·서비스 이름이면 기회로 올리지 않는다**
(이름 단독, `{이름} 후기/review/가격/pricing` 같은 변형 포함). 그 검색의 1순위
결과는 그 브랜드 자기 사이트이고, 디렉터리가 8위에서 클릭을 못 가져가는 건
정상이다. 밀어도 안 오르고, 올려도 클릭이 안 온다.

이 규칙은 오래 산문으로만 있었고, 그래서 **아무도 실행하지 않았다** — Claude가
기회를 적재할 때만 손으로 걸렀고 대시보드는 그대로 다 보여줬다. 지금은
`scoring.is_foreign_brand()` / `scoring.drop_foreign_brands()`가 판정하고,
`scoring.striking()`이 그 필터를 이미 걸고 결과를 돌려준다.

카탈로그(`scoring.foreign_brands()`)의 출처는 세 경로의 합이다:

- `competitors` 테이블의 도메인(브랜드 이름 부분)
- 프로젝트 yaml의 `tools` — `projects/_template.yaml`에 키가 있고, 대시보드
  사이트 등록 폼도 같은 칸으로 받는다 (`dashboard.create_project`이 `tools`
  줄바꿈·쉼표 입력을 그대로 적는다)
- 프로젝트 yaml의 `foreign_brands` — `tools`와 목적은 같은 별칭 키

세 경로 중 어느 것도 비어 있으면 카탈로그는 공집합이 되고
`is_foreign_brand()`는 아무것도 걸러내지 않는다 — striking 목록에 남의 브랜드
검색이 그대로 흘러든다. 이때 대시보드는 `gather()`가 `brand_catalog_empty=1`
로 화면에 경고를 띄운다 (yaml `tools`에 도구 이름을 한 줄 추가하거나 대시보드
폼에서 같은 칸을 채우면 해소).

자기 브랜드(`name`·`brand_aliases`)는 카탈로그에서 **명시적으로 제외**해
반드시 남긴다 — 그건 내 브랜드 검색이라 CTR이 낮으면 진짜 문제다.

실측 근거(aitierlist 2026-08-13): `ecrett` 8.6위·48노출·**0클릭**,
`future tools` 4.9위·19노출·1클릭, `paperpal 후기` 9.6위·13노출·1클릭 —
전부 등재 도구/경쟁 디렉터리의 브랜드 검색인데 striking_distance 상위로 올라와
"제목·설명을 고치라"는 처방을 받았다. 고칠 것이 없는 기회는 목록의 신뢰를 깎는다.

예외: 그 브랜드의 **비교·대안 의도**(`{이름} alternative`, `{이름} vs {이름}`)는
디렉터리가 정당하게 이길 수 있는 자리라 계속 기회로 본다.

## 1b. pseo_pattern 탐지 절차

"수요는 확인됐는데(노출) 공급이 비어 있고(저CTR), 변수 하나만 바뀌는 패턴이라
템플릿+데이터로 대량 커버 가능한" 쿼리 군집을 찾는다.

**1단계 — 후보 추출 (코드):** `scoring.pseo_candidates(conn, pid, snap)`.
`(query, imp, clk, ctr_pct, pos)` 를 노출 내림차순으로 돌려준다.
임계값은 `scoring.PSEO_MIN_IMP`(=50)·`scoring.PSEO_MAX_CTR`(=1.5)이고,
사이트 규모에 맞춰 인자로 올릴 수 있다 — 노출이 큰 사이트면 올린다.
`scoring.py load`(5절)가 이 후보 상위 10개를 개별 `pseo_pattern` 기회로
선적재한다 — 군집으로 묶는 것은 여전히 아래 2~3단계(Claude)다.

**2단계 — 패턴 클러스터링 (코드 아님, Claude 판단):**
후보들을 "변수 슬롯 하나만 다른 템플릿"으로 묶는다.
예: `20도 옷차림 / 15도 옷차림 / 기온별 옷차림` → 템플릿 `{기온}도 옷차림`.
전형적 슬롯: {지역} {기온} {시술} {장르} {경쟁제품} {카테고리} {날짜·시즌}.
같은 템플릿에 **3개 이상** 쿼리가 모이면 군집 성립. GSC는 저노출 롱테일을
익명화하므로 보이는 군집은 빙산의 일각 — 슬롯의 전체 값 공간(기온 전 구간,
전 지역 등)이 실제 캠페인 크기다.

**3단계 — 기회 적재 (코드 아님, Claude 판단):**
- target = 템플릿 문자열 (`{기온}도 옷차림`), kind = `pseo_pattern`
- score ~ 군집 합산 노출(log 정규화) + 슬롯 값 공간 크기 + 달성가능성
- reasoning 필수 요소: 군집 크기, 합산 노출, 예시 쿼리 2~3개, 평균 순위.
  예: "3개 쿼리 군집(합산 노출 3,150·평균 CTR 0.4%) — 예: 20도/15도/기온별 옷차림.
  슬롯 값 공간(기온 구간 ~15개)만큼 확장 가능 (gsc 07-21)."

**가드레일 (reasoning 또는 Next Actions에 반드시 반영):**
얇은 템플릿 페이지 대량 발행은 저품질 판정·도메인 권위 하락 리스크가 있다.
(a) 페이지마다 실데이터로 속을 채울 것(data-rich), (b) 일괄이 아니라 단계적
롤아웃 + 색인·성과 확인 후 확장, (c) 성과 없는 페이지 정리 QA를 전제로 제안한다.

**프리셋 편향:** directory 최우선(존재 이유 그 자체), saas·local_clinic 유효
({경쟁제품} alternative, {지역}×{시술}), game은 {장르}·{유사작} 축으로 제한적.

### 1c. 인텐트 분류 — 코드 기본값, Claude는 보정만

키워드의 인텐트(`keywords.intent`)는 `scoring.classify_intent()`가
결정적으로 분류한다 — 한·영 토큰을 그대로 매칭하고 우선순위는
**transactional > commercial > navigational > info**다. 토큰 사전은
`INTENT_TRANSACTIONAL` / `INTENT_COMMERCIAL` / `INTENT_NAVIGATIONAL`
(`scoring.py` 상단):

- transactional: 구매·가격·다운로드·할인·쿠폰 + buy·price·pricing·download·
  discount·coupon
- commercial: 후기·리뷰·비교·추천·순위·랭킹 + vs·best·review·reviews·
  alternative·alternatives·top·compare
- navigational: 로그인·공식·홈페이지 + login·official·homepage
- info: 위 어디에도 안 걸리는 나머지 (기본값)

같은 토큰이 여러 사전에 있어도 우선순위가 이긴다 — `best pricing`은
`pricing`이 transactional에 속하므로 transactional이 commercial(best)을 이긴다.
`buy reviews`는 `buy`(transactional)가 `reviews`(commercial)를 이긴다.

`scoring.py load`(5절)가 시작 시 `_backfill_intents`를 한 번 부른다 —
**`intent`가 NULL인 활성 키워드만 채우고**, 이미 적힌 값은 절대 덮지 않는다.
Claude(또는 사람)가 손본 intent는 그 손이 이긴다. 비활성 키워드는 의도
추적이 아니라 후보라 안 본다.

남은 Claude 몫: 코드 토큰 사전을 빠져나간 케이스(예: `견적`, `가격표`,
`어디서 사`, 의도 모호한 브랜드 결합)는 사람이 직접 intent를 적는다 — 그
값은 다음 load에서도 보존된다. 볼륨 추정이나 디바이스별 차이는 여기서
다루지 않는다.

## 2. 점수 프레임 (0~100) — 계수는 코드(WEIGHTS), fit은 코드 기본값 + Claude 보정

계수는 이제 Claude가 매번 정하지 않는다. `scoring.WEIGHTS`(프리셋별 고정, 각 합
1.0)를 `scoring.score(kind, metrics, project_type)`가 적용해 **결정적으로**
계산한다 — 같은 입력이면 언제나 같은 점수다. 예전엔 계수가 런마다 Claude 재량이라
두 런의 점수를 비교할 수 없었다.

```
score = w_demand · 수요        min(1, log10(1+impressions)/5)  — 노출 10만이면 1.0
      + w_reach  · 달성가능성   1 - gap_to_page1(pos)/10. 순위 미확인이면 보수적 0.3
      + w_fit    · 관련성      metrics["fit"] 0~1, 기본은 _fit_of() 근사
      + w_ai     · AI 노출     metrics["ai"], 미지정 시 ai_citation_gap·aio_exposure만 1.0
```

`w_fit`(관련성)의 기본값은 `scoring._fit_of()`의 결정적 근사다 — 0.5 중립이면
`local_clinic`(w_fit=0.45) 같은 프리셋에서 모든 기회가 점수 면적 한가운데만
차지해서 순위가 안 갈라지므로, 활성 키워드·클러스터 매칭 정도로 단계값을
준다:

- **0.8** — `target`을 `norm()` 비교했을 때 활성 키워드와 정확히 일치
  (공백·대소문자·문자 부호 차이는 정규화해서 같게 본다)
- **0.65** — 정확 일치는 없지만 활성 키워드의 `cluster` 명이 target 안에
  들어가거나(target = `cluster:{name}`인 coverage 행도 같은 값)
- **0.5** — 어느 매칭도 없을 때

이걸 `score()`가 `metrics["fit"]`로 넘겨 받는다 — `load`가 기회마다
`_fit_of()`로 미리 채워서 넘긴다(5절).

| type | w_demand | w_reach | w_fit | w_ai |
|------|---------|---------|-------|------|
| game | .30 | .25 | .25 | .20 |
| local_clinic | .25 | .20 | .45 | .10 |
| saas | .20 | .20 | .15 | .45 |
| directory | .40 | .25 | .20 | .15 |

미등록 type은 saas 계수로 폴백. 프리셋별 방향(saas는 w_ai 최상향 — best-of
리스트 인용이 전장, local_clinic은 w_fit 상향 — 네이버 미측정 한계는 reasoning에
명시, directory는 수요·coverage 우선)은 이 표에 굳어 있다.

**Claude 재량으로 남은 것:** (a) 관련성 보정 — `metrics["fit"]`를 직접
덮어쓰거나, 적재 후 점수·reasoning을 보정한다. 기본은 `_fit_of()` 근사
(위 셋 단계값)이고, 안 넘기면 그대로 돈다. 코어가 안 맞는 기회·시즌성·
정책적으로 막힌 페이지는 0~1로 명시해 결정성을 유지한다. (b) reasoning 보강 —
load가 적는 템플릿 문장 위에 원인 가설·맥락을 얹는다 (3절). (c) 예외 판단
— 수치로 못 거르는 케이스는 reasoning에 근거를 적고 조정한다.

## 3. reasoning 작성 규칙

- `scoring.py load`가 적재하는 기회에는 수치 포함 템플릿 reasoning이 이미 붙는다.
  Claude의 일은 **상위 10개를 보강**하는 것: 원인 가설·맥락·fit 판단을 1~2문장으로.
  예: "노출 1,840·평균 8.2위로 1페이지 하단 — 타이틀·본문 보강만으로 상단 진입 여지 (gsc 07-21)."
- 금지: Brain에 없는 추정 수치, 볼륨이 NULL인데 볼륨 언급, 단발 AI 체크를 사실처럼 단정.
- AI 관련 기회는 표본 수를 병기: "3엔진×2샘플 기준" 식으로.

## 4. 데이터 리터러시 (리포트 해석 규칙 — 사용자에게도 상기)

1. AI 답변은 비결정적 → 단발 수치가 아니라 주 단위 추세. 기본 2샘플(config
   `ai_samples`=2, 비용 2배 트레이드오프), 중요 프롬프트는 `--samples 3`.
2. API+네이티브 검색 ≈ 소비자 앱 (메모리·개인화 없음) → 방향 지표로만.
3. GSC: 2~3일 지연, 저노출 롱테일은 익명화로 누락, position은 평균값.
3b. **기간이 다른 스냅샷을 빼지 않는다.** `gsc_snapshots.period_days`가 다르면
   (예: CSV 90일치 vs 자동수집 28일치) Δ순위·Δ클릭은 전부 거짓이다. 두 스냅샷을
   비교하는 SQL에는 반드시 `period_days`가 같다는 조건을 넣고, 짝이 없으면
   "비교할 같은 기간 기록이 없다"고 말한다. 비교 짝 선택은 `scoring.snapshot_pair()`가
   실행한다 (대시보드와 리포트가 같이 쓴다).
4. 순위 ±3 변동(`scoring.RANK_NOISE`=3, rank_snapshots 정수 순위용)과
   GSC Δpos ±0.5 미만(`scoring.NOISE_POS`=0.5, 평균 순위 Δ용 — 다른 축이다)은
   노이즈 취급. `scoring.movers()`가 NOISE_POS로 오름/내림을 가른다.
   대시보드는 예전에 0.4를 썼다(문서와 어긋나 있었다). 지금은 문서대로 0.5다.
5. 순위 ≠ 트래픽 (제로클릭) → 최종 지표는 클릭.
6. 볼륨·난이도는 전부 추정치 — 우선순위용 상대값으로만.

## 5. opportunities 적재 방법

**기본 경로 — 기계 판정분은 CLI 한 줄:**

```bash
python scripts/scoring.py load <project>
```

projects.type을 읽어 WEIGHTS를 적용하고, striking / ctr_gaps / cannibalization /
rank_decay / pseo_candidates(상위 10) / coverage(클러스터별 1건)를 전부 돌려
`score()` 점수 + 수치 포함 템플릿 reasoning으로 적재한다. 적재 kind:
`striking_distance | ctr_gap | cannibalization | rank_decay | pseo_pattern | coverage`.
`db.run(kind="analysis")` 컨텍스트 안에서 돌고, 완료 시
`loaded N opportunities for '<project>' (type=..., gsc <date>)` 를 출력한다.

**Claude가 직접 적재하는 것은 load가 못 하는 것만** — pseo_pattern 군집(1b절
2~3단계), fit 보정, ai_citation_gap 등 Brain 조회 + 판단이 필요한 kind.
그때는 아래 파이썬 패턴으로 (db.py sql은 읽기 전용 커넥션이라 쓸 수 없다):

**반드시 `db.upsert_opportunities`로 쓴다.** `(project_id, kind, target)`에 UNIQUE 인덱스가 걸려 있어
그냥 INSERT하면 두 번째 런에서 터진다. 그리고 `status`를 건드리지 않는 것(트리아지 보존 — 사용자가
확인함·완료·제외로 정리해 둔 것을 새 런이 'new'로 되돌리지 않음)은 **함수가 보장한다**.

실행 기록은 `db.run` 컨텍스트로 연다 — 스크립트가 중간에 터져도 `runs.finished_at`이
NULL로 남지 않는다(남으면 "수집 이력" 화면이 안 끝난 런을 계속 진행 중으로 보여준다).

```bash
python3 - << 'PY'
import sys; sys.path.insert(0, 'scripts'); import db
conn = db.connect(); pid = db.get_project(conn, "PROJECT")["id"]
rows = [  # (kind, target, score, reasoning)
  ("striking_distance", "예시 키워드", 82, "노출 1,840·8.2위 …"),
]
with db.run(conn, pid, "analysis") as r:
    db.upsert_opportunities(conn, pid, r.id, rows)
    r.notes = f"opps={len(rows)}"
print("ok")
PY
```

Next actions는 3~5개 구체 행동을 JSON 배열로 저장 후
`python scripts/dashboard.py --export --project NAME --actions FILE` 로 전달.
빈 Next actions로 리포트를 끝내는 것은 계약 위반이다.
