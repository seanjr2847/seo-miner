# 검색어 심사 — 기회를 보기 전에 검색어를 거른다

2026-09-08. 상태: 승인된 설계. 구현 계획은 이 문서를 정본으로 쓴다.

## 왜

기회 목록이 사용자가 실제로 내리는 판단 순서를 따르지 않는다. 사용자는

1. 이 검색어가 우리 것인가(자동완성 쓰레기인가) 를 먼저 보고,
2. 손댈 가치가 있는가를 정하고,
3. 있으면 작업하고, 없으면 치우고,
4. 작업한 뒤에는 나아졌는지 지켜보거나(다회성 a) 글을 여러 편 더 쓴다(다회성 b).

지금 화면은 이 넷을 기회 행 하나에 붙은 버튼 셋(확인·완료·목록에서 빼기)으로
받는다. 호스팅 실 데이터(2026-09-08) 가 보여 준 문제:

| 사이트 | 열린 기회 | 서로 다른 검색어 | 그중 aio_exposure |
|---|---|---|---|
| theotherskin | 282 | 265 | 196 |
| aitierlist | 164 | 131 | 91 |
| noti | 100 | 71 | 51 |

- 같은 검색어가 진단 종류마다 따로 올라온다 ("jenni ai 후기" 가 striking_distance 와
  pseo_pattern 두 줄). 빼려면 종류 수만큼 눌러야 한다.
- 띄어쓰기 변형이 따로 산다 ("디아더피부과 가격" / "디아 더 피부과 가격").
- 지금까지 제외한 16건은 전부 자기 브랜드 검색어다. "이상한 검색어" 와 "멀쩡하지만
  안 함" 이 같은 버튼이다.
- 한 번 뺀 검색어가 다음 측정에서 다른 종류로 다시 올라온다. 판정이 기회 행에만
  남고 검색어에는 안 남기 때문이다.

## 무엇을 만드는가

**검색어 심사 단**을 기회 목록 앞에 세운다. 심사에서 "작업" 판정을 받은 검색어만
개요의 기회 목록으로 내려간다. 기회 쪽은 상태 값을 늘리지 않고 라벨과 전이를
고치고, 완료 이후의 변화와 작업 여러 건을 보여 준다.

### 1. 화면 — [심사]

측정 묶음의 개요 바로 위. 정본은 `skills/capture/templates/views/triage.html` 의
`view-def` 하나. 셸에는 `SM.addView` 로 붙인다. `stages: ["gaps"]`.

```
심사                                   미판정 231 · 무관 0 · 보류 16 · 작업 0
[미판정 ▾] [종류 ▾] [찾기……]                       선택 3건 → [무관] [보류] [작업]

☐  검색어 (변형)              종류                    점수   클릭/노출(28일)
☐  디아더피부과 가격 (+1)     순위근접 · 잠식          39.1   12 / 340
☐  피부과 레이저 토닝 후기    AI노출 · 콘텐츠갭        35.2   0 / 0
```

- 행 하나 = `scoring.norm` 으로 정규화한 검색어. 변형은 한 줄로 접히고 "(+n)".
- 열: 검색어(변형 수) · 걸린 진단 종류 칩 · 최고 점수 · GSC 28일 클릭/노출.
- 브랜드 힌트: 프로젝트 설정의 브랜드 표기 토큰이 들어 있으면 "브랜드" 칩. 자동
  판정은 하지 않는다.
- 판정 셋: **무관**(자동완성 쓰레기) · **보류**(우리 것이지만 지금 안 함, 브랜드가
  여기) · **작업**.
- 체크박스 다중 선택 + 상단 바 일괄 판정. 키보드 `j`/`k` 이동, `1`/`2`/`3` 판정.
- 판정한 줄은 즉시 사라지고 상단 카운트가 움직인다. 상단 필터를 보류·무관으로
  바꾸면 "미판정으로 되돌리기" 가 있다.
- 기본 정렬은 최고 점수 내림차순.
- 박제본(`--export`)에는 서버가 없으므로 심사 화면을 넣지 않는다.

검색어가 아닌 기회는 심사를 거치지 않는다: coverage(`cluster:`) · index_blocked ·
crawl_issue · backlink_broken · backlink_prospect. 이 목록은 `scoring.py` 의
`KEYWORD_KINDS`(심사 대상 종류) 한 벌로 두고, 나머지가 통과 종류다.

### 2. 데이터

```sql
CREATE TABLE IF NOT EXISTS verdicts (
  project_id INTEGER NOT NULL REFERENCES projects(id),
  key        TEXT NOT NULL,      -- scoring.norm(대상)
  verdict    TEXT NOT NULL,      -- irrelevant | hold | work
  decided_at TEXT DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(project_id, key)
);
ALTER TABLE opportunities ADD COLUMN status_at TEXT;   -- _migrate 로
```

키워드 행이 아니라 별도 표인 이유: 기회의 대상이 항상 `keywords` 에 있지 않다
(content_gap 은 경쟁사에서, ai_citation_gap 은 질문문).

판정이 미치는 곳 — 전부 `db.py` 함수 하나씩:

| 곳 | 무관 | 보류 | 작업 |
|---|---|---|---|
| `scoring.load` 기회 적재 | 건너뜀 | 건너뜀 | 적재 |
| `db.open_opportunities` · `/api/data` 기회 목록 | 빠짐 | 빠짐 | 나옴 |
| `keywords.is_active` | 0 으로 | 안 건드림 | 안 건드림 |

이미 생긴 기회 행은 지우지 않는다. 조회에서 빠질 뿐이다. `/create plan` 은
`db.open_opportunities` 를 쓰므로 따로 손대지 않는다. 미판정(verdicts 에 없음)은
기회 목록에서 **빠진다** — 심사를 거쳐야 기회가 된다.

API — 둘 다 `dashboard.ROUTES` 에 두고 `server/app.py` 가 `/api/data` 처럼 감싼다:

- `GET /api/triage?project=` → `{rows: [{key, label, variants: n, kinds: [...],
  score, clicks, impressions, brand: bool, verdict: null|...}], counts: {...}}`
- `POST /api/verdict` `{project, keys: [...], verdict: "irrelevant"|"hold"|"work"|null}`
  → 일괄 저장. `null` 은 미판정으로 되돌림. 셋 밖의 값은 400.

정규화는 서버 한 곳(`scoring.norm`)에서만 한다. 화면은 서버가 준 `key` 를 그대로
돌려보낸다.

### 3. 기회 상태

값은 `db.OPP_STATUSES` 네 개 그대로. 라벨과 접힌 줄의 다음 버튼만 바꾼다.

| 값 | 라벨 | 접힌 줄 다음 버튼 |
|---|---|---|
| new | 할 일 | 작업 시작 → acked |
| acked | 진행 중 | 완료 표시 → done |
| done | 완료 | 다시 열기 → new |
| dismissed | 이 기회만 뺌 | 다시 열기 → new |

`dismissed` 로 보내는 "이 기회만 빼기" 는 펼침 패널 안으로 내린다 — 검색어는 맞는데
이 진단 하나가 틀렸을 때 쓰는 자리다.

**관찰(다회성 a)** — 상태를 늘리지 않는다. `status_at` 을 남기고, 개요에 "완료 후
관찰" 묶음을 둔다. 줄마다 완료 시점의 순위·클릭과 최신 측정값을 전·후 두 숫자로
놓는다. 완료 뒤 두 번 이상 측정했는데 나아지지 않았으면 줄 끝에 "다시 열기".

**작업 여러 건(다회성 b)** — 기존 `creations`(`opportunity_id`) 를 쓴다. 기회 펼침
패널에 그 기회로 만든 작업 목록(파일·브랜치·머지 여부). 묶음 기회는 진행 중에
머물며 작업이 쌓이고 완료는 사람이 누른다.

**호스팅 동작 변경** — `server/app.py` `/api/create` 가 PR 을 만든 뒤 기회를 `done`
으로 돌리던 것을 `acked` 로 바꾼다. 글 하나로 묶음 기회가 닫히면 안 되고, 완료는
관찰까지 보고 사람이 정한다. 단건도 PR 뒤 완료 버튼 한 번이 더 든다(승인됨).

### 4. 검사

`test_seams.py` 에 못 박고, 일부러 깨서 FAIL 을 확인한다.

- 정규화 한 벌: JS 에 norm 사본이 없고, 서버 저장·조회가 `scoring.norm` 을 쓴다.
- `/api/triage` · `/api/verdict` 가 로컬 `ROUTES` 와 호스팅 양쪽에 있다(기존 6번).
- `KEYWORD_KINDS` ⊂ `ALL_KINDS`, 통과 종류는 판정 없이도 열린 기회에 나온다.
- `OPP_STATUSES` 와 셸 `OPP_LABEL` · `OPP_NEXT` 키가 양방향으로 같다.

`test_capture.py`: irrelevant/hold 판정 뒤 `scoring.load` 가 그 키의 기회를 안
만들고 work 는 만든다. 변형("디아 더 피부과") 도 같은 판정에 걸린다. irrelevant 가
`is_active` 를 내리고 hold 는 안 내린다. `null` 되돌리기.

`test_render.py`: 심사 화면이 JS 오류 없이 그려지고 일괄 판정 뒤 행이 사라진다.
커밋 전 Orca 브라우저로 심사 → 개요 → 기회 펼침 경로를 실제로 누른다.

오류: 판정 저장 실패면 행을 되살리고 상단 한 줄(지금 `setOpp` 방식). 잘못된
verdict 는 400.

## 안 하는 것

- 자동 판정(브랜드 자동 보류, aio_exposure 자동 무관). 힌트 칩까지만.
- 다섯 번째 상태(`watching`). 조회로 대신한다.
- 채점(`scoring`) 가중치 손보기. 별건.
- 박제본의 심사 화면.
