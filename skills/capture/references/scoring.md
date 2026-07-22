# 기회 스코어링 & 분석 규칙 (Claude 분석 단계 계약)

이 문서는 /capture 분석 단계에서 Claude가 따르는 규칙이다. 핵심 원칙:
**Brain에 없는 주장은 하지 않는다.** 모든 기회의 reasoning은 db.py sql로
조회한 실제 수치를 인용해야 한다.

## 1. 기회 종류 (kind)

| kind | 정의 | 데이터원 (SQL 스케치) |
|------|------|----------------------|
| striking_distance | GSC 4~20위 + 노출 유의미 → 밀면 상단 진입 | gsc_snapshots: AVG(position) BETWEEN 4 AND 20, SUM(impressions) 상위 |
| ai_citation_gap | 관련성 높은 프롬프트에서 타 도메인만 인용 | ai_checks: cited=0, cited_domains_json 빈도 |
| rank_decay | 직전 스냅샷 대비 순위·클릭 하락 (방어) | gsc 두 스냅샷 조인, Δpos < -1.5 또는 Δclicks 음수 큰 순 |
| content_gap | 경쟁사는 잡는데 나는 부재 | 부분 가능: rank 수확 경쟁사가 내 추적 키워드 상위에 있고 나는 부재인 경우. 완전판(경쟁사 역키워드)은 여전히 DataForSEO Labs 필요 |
| coverage | (directory) 노출 페이지 수 부족 군집 | gsc_snapshots page 차원 DISTINCT 카운트 |
| pseo_pattern | 노출은 있는데 클릭이 없는 쿼리들이 템플릿 패턴을 이룸 → pSEO 캠페인 후보 | 아래 1b절 절차 |
| aio_exposure | AI 오버뷰가 뜨는 내 키워드에서 인용 미확보 | rank_snapshots: aio_present=1 AND aio_cited=0 (DataForSEO 제공 시) |

## 1b. pseo_pattern 탐지 절차

"수요는 확인됐는데(노출) 공급이 비어 있고(저CTR), 변수 하나만 바뀌는 패턴이라
템플릿+데이터로 대량 커버 가능한" 쿼리 군집을 찾는다.

**1단계 — 후보 추출 (SQL, 최신 스냅샷 기준):**

```sql
SELECT query, SUM(impressions) imp, SUM(clicks) clk,
       ROUND(SUM(clicks)*100.0/SUM(impressions),2) ctr_pct,
       ROUND(AVG(position),1) pos
  FROM gsc_snapshots
 WHERE project_id = {pid}
   AND snapshot_date = (SELECT MAX(snapshot_date) FROM gsc_snapshots
                         WHERE project_id = {pid})
 GROUP BY query
HAVING imp >= 50 AND ctr_pct < 1.5
 ORDER BY imp DESC LIMIT 200
```

임계값(imp 50 / CTR 1.5%)은 사이트 규모에 맞춰 조정 — 노출이 큰 사이트면 올린다.

**2단계 — 패턴 클러스터링 (Claude 판단):**
후보들을 "변수 슬롯 하나만 다른 템플릿"으로 묶는다.
예: `20도 옷차림 / 15도 옷차림 / 기온별 옷차림` → 템플릿 `{기온}도 옷차림`.
전형적 슬롯: {지역} {기온} {시술} {장르} {경쟁제품} {카테고리} {날짜·시즌}.
같은 템플릿에 **3개 이상** 쿼리가 모이면 군집 성립. GSC는 저노출 롱테일을
익명화하므로 보이는 군집은 빙산의 일각 — 슬롯의 전체 값 공간(기온 전 구간,
전 지역 등)이 실제 캠페인 크기다.

**3단계 — 기회 적재:**
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

## 2. 점수 프레임 (0~100)

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
4. 순위 ±2~3 변동과 GSC Δpos ±0.5 미만은 노이즈 취급.
5. 순위 ≠ 트래픽 (제로클릭) → 최종 지표는 클릭.
6. 볼륨·난이도는 전부 추정치 — 우선순위용 상대값으로만.

## 5. opportunities 적재 방법

분석 후 INSERT는 파이썬 원라이너로 (db.py sql은 읽기 전용):

```bash
python3 - << 'PY'
import sys; sys.path.insert(0, 'scripts'); import db, json
conn = db.connect(); pid = db.get_project(conn, "PROJECT")["id"]
run = db.start_run(conn, pid, "analysis")
rows = [  # (kind, target, score, reasoning)
  ("striking_distance", "예시 키워드", 82, "노출 1,840·8.2위 …"),
]
for k,t,s,r in rows:
    conn.execute("""INSERT INTO opportunities(project_id,run_id,kind,target,score,reasoning)
                    VALUES(?,?,?,?,?,?)""",(pid,run,k,t,s,r))
conn.commit(); db.finish_run(conn, run, notes=f"opps={len(rows)}"); print("ok")
PY
```

Next actions는 3~5개 구체 행동을 JSON 배열로 저장 후 report.py --actions 로 전달.
빈 Next actions로 리포트를 끝내는 것은 계약 위반이다.
