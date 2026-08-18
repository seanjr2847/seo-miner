# 기회 스코어링 & 분석 규칙 (Claude 분석 단계 계약)

이 문서는 /capture 분석 단계에서 Claude가 따르는 규칙이다. 핵심 원칙:
**Brain에 없는 주장은 하지 않는다.** 모든 기회의 reasoning은 db.py sql로
조회한 실제 수치를 인용해야 한다.

**이 문서는 명세이고, 실행은 `scripts/scoring.py`다.** 산문으로만 적힌 규칙은
아무도 실행하지 않아 실제로 안 돌았다(1a절이 그랬다). 그래서 기계가 판정할 수
있는 것은 전부 scoring.py로 옮겼고, 여기서는 그 함수·상수 이름을 가리킨다.
임계값을 바꾸려면 scoring.py를 고치고 이 문서를 맞춘다. 남은 산문(2절 점수
프레임, 1b절 2~3단계)은 **코드가 아니라 Claude 판단**이며, 그렇다고 명시해 둔다.
자체 점검: `python scripts/scoring.py`.

## 1. 기회 종류 (kind)

| kind | 정의 | 데이터원 / 실행하는 코드 |
|------|------|----------------------|
| striking_distance | GSC 4~20위 + 노출 유의미 → 밀면 상단 진입 | `scoring.striking()` — 구간은 `STRIKING_LO=4`·`STRIKING_HI=20`, 노출 내림차순. 1페이지까지 남은 거리는 `gap_to_page1()`(`PAGE1=10`) |
| ai_citation_gap | 관련성 높은 프롬프트에서 타 도메인만 인용 | ai_checks: cited=0, cited_domains_json 빈도. 인용/언급 판정 자체는 `scoring.judge()` |
| rank_decay | 직전 스냅샷 대비 순위·클릭 하락 (방어) | gsc 두 스냅샷 조인, **period_days가 같은 것끼리만**, Δpos < `scoring.DECAY_POS`(= -1.5) 또는 Δclicks 음수 큰 순 |
| content_gap | 경쟁사는 잡는데 나는 부재 | 부분 가능: rank 수확 경쟁사가 내 추적 키워드 상위에 있고 나는 부재인 경우. 완전판(경쟁사 역키워드)은 여전히 DataForSEO Labs 필요 |
| coverage | (directory) 노출 페이지 수 부족 군집 | gsc_snapshots page 차원 DISTINCT 카운트 |
| pseo_pattern | 노출은 있는데 클릭이 없는 쿼리들이 템플릿 패턴을 이룸 → pSEO 캠페인 후보 | 아래 1b절 절차 (후보 추출은 `scoring.pseo_candidates()`) |
| aio_exposure | AI 오버뷰가 뜨는 내 키워드에서 인용 미확보 | rank_snapshots: aio_present=1 AND aio_cited=0 (DataForSEO 제공 시). 자기 도메인 판정은 `scoring.owns()`/`host_of()` |

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

카탈로그(`scoring.foreign_brands()`)의 출처는 두 곳이다:
`competitors` 테이블의 도메인(브랜드 이름 부분) + 프로젝트 yaml의
`tools` / `foreign_brands` 목록. 프로젝트 자기 브랜드(`name`·`brand_aliases`)는
카탈로그에서 **명시적으로 제외**해 반드시 남긴다 — 그건 내 브랜드 검색이라
CTR이 낮으면 진짜 문제다.

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

## 2. 점수 프레임 (0~100) — 코드 아님, Claude 판단

이 절만은 일부러 코드로 옮기지 않았다. 관련성(w_fit)은 프로젝트 코어 토픽에 대한
판단이라 임계값으로 굳힐 수 없다. 대신 **코드로 도는 것과 산문으로 남은 것을
헷갈리지 않도록** 여기 명시한다: 아래 계수는 Claude가 매번 정하고,
`opportunities.score` 에 그 결과만 적재된다.

```
score = w_demand · 수요        log10(1+impressions or volume) 정규화
      + w_reach  · 달성가능성   4~20위 근접 보너스, 신뢰 낮은 추정치엔 보수적
      + w_fit    · 관련성      프로젝트 코어 토픽 적합도 (Claude 판정 0~1)
      + w_ai     · AI 노출     citation gap / 미노출 프롬프트 연관 시 가산
```

정확한 계수는 고정하지 않는다. 대신 프리셋별 방향(불변):
- **saas**: w_ai 최상향 — best-of 리스트 인용이 전장
- **local_clinic**: 지역 한정 키워드 w_fit 상향 (네이버 미측정 한계를 reasoning에 명시)
- **game**: 추천·리스트형 인용 가산
- **directory**: coverage kind를 개별 키워드보다 우선

## 3. reasoning 작성 규칙

- 상위 10개 기회에만 작성. 1~2문장, 반드시 수치 포함.
  예: "노출 1,840·평균 8.2위로 1페이지 하단 — 타이틀·본문 보강만으로 상단 진입 여지 (gsc 07-21)."
- 금지: Brain에 없는 추정 수치, 볼륨이 NULL인데 볼륨 언급, 단발 AI 체크를 사실처럼 단정.
- AI 관련 기회는 표본 수를 병기: "3엔진×1샘플 기준" 식으로.

## 4. 데이터 리터러시 (리포트 해석 규칙 — 사용자에게도 상기)

1. AI 답변은 비결정적 → 단발 수치가 아니라 주 단위 추세. 중요 프롬프트는 --samples 2~3.
2. API+네이티브 검색 ≈ 소비자 앱 (메모리·개인화 없음) → 방향 지표로만.
3. GSC: 2~3일 지연, 저노출 롱테일은 익명화로 누락, position은 평균값.
3b. **기간이 다른 스냅샷을 빼지 않는다.** `gsc_snapshots.period_days`가 다르면
   (예: CSV 90일치 vs 자동수집 28일치) Δ순위·Δ클릭은 전부 거짓이다. 두 스냅샷을
   비교하는 SQL에는 반드시 `period_days`가 같다는 조건을 넣고, 짝이 없으면
   "비교할 같은 기간 기록이 없다"고 말한다. 비교 짝 선택은 `scoring.snapshot_pair()`가
   실행한다 (대시보드와 리포트가 같이 쓴다).
4. 순위 ±2~3 변동과 GSC Δpos ±0.5 미만은 노이즈 취급 — 이 바닥값이
   `scoring.NOISE_POS`(=0.5)이고 `scoring.movers()`가 그걸로 오름/내림을 가른다.
   대시보드는 예전에 0.4를 썼다(문서와 어긋나 있었다). 지금은 문서대로 0.5다.
5. 순위 ≠ 트래픽 (제로클릭) → 최종 지표는 클릭.
6. 볼륨·난이도는 전부 추정치 — 우선순위용 상대값으로만.

## 5. opportunities 적재 방법

분석 후 INSERT는 파이썬 원라이너로 (db.py sql은 읽기 전용 커넥션이라 쓸 수 없다):

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
