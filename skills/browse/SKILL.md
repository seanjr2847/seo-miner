---
name: browse
description: Browser-measured AI visibility via claude-in-chrome — 실제 소비자 앱(chatgpt.com·perplexity.ai·gemini.google.com)에 프롬프트를 직접 입력해 브랜드 언급·인용을 실측하고 Brain(ai_checks)에 기록. Use when the user wants AI citation checks without API keys ("공짜로 인용 체크", "브라우저로 실측", "/browse ..."), when OPENROUTER_API_KEY is missing but an AI visibility check was requested, or when the user wants consumer-surface ground truth to compare against API-based collect_ai samples.
---

# browse — 브라우저 실측 AI 가시성 (키 불필요)

collect_ai(API)의 무료 대체 표면. Claude가 claude-in-chrome으로 실제 소비자 앱을
조작해 답변·인용을 수집하고, capture의 Brain에 같은 형식으로 기록한다.
API가 "근사치"라면 이건 사용자가 실제로 보는 화면 그 자체다 — 대신 느리고 깨지기 쉽다.

## 철칙

1. **capture의 Brain-first 철칙 상속.** 프롬프트는 Brain에서 읽고, 결과는 즉시
   `scripts/record_check.py`로 기록한다. 기록 안 된 관찰은 없는 것이다.
2. **조작 횟수 사전 고지.** 실행 전 프롬프트 수 × 엔진 수 = 브라우저 왕복 횟수를
   보여주고 사용자 확인을 받는다 (collect_ai의 dry-run에 해당). 기본 상한 10프롬프트.
3. **저빈도·인간적 속도.** 소비자 앱 자동화는 각 서비스 ToS 회색지대다 — 월 1~2회
   측정 용도로만, 프롬프트 사이 자연스러운 간격을 두고, 캡차·봇 감지가 뜨면 그
   엔진은 즉시 중단한다. 이 리스크는 실행 전 고지한 적 없다면 한 번 고지한다.
4. **엔진 값은 `-web` 접미사** (chatgpt-web 등). API 샘플과 소비자 표면 샘플을
   같은 엔진명으로 섞지 않는다 — 집계·비교 시에도 두 표면을 분리해 말한다.

## 준비물

- claude-in-chrome MCP 연결 (없으면 브라우저 도구 ToolSearch 로드부터). 세션 시작은
  반드시 `tabs_context`로.
- 로그인: chatgpt.com은 비로그인 동작(2026-07 실측, 웹검색 인용 포함).
  perplexity.ai는 첫 질문부터 로그인 요구(2026-07 실측), gemini.google.com도 구글
  로그인 필요 — 로그인 벽·캡차를 만나면 **대신 로그인하려 하지 말고** 그 엔진을
  스킵하고 마지막에 사용자에게 보고한다.
- Brain: capture 스킬이 초기화돼 있고(ai_prompts에 활성 프롬프트 존재) python 사용 가능.

## /browse {P} [engines] [max] 워크플로우

`{P}` = 프로젝트 이름. engines 기본 `chatgpt,perplexity,gemini`, max 기본 10.

1. **프롬프트 로드** — `python ../capture/scripts/db.py sql "SELECT id, prompt, category
   FROM ai_prompts WHERE project_id=(SELECT id FROM projects WHERE name='{P}')
   AND is_active=1 ORDER BY id LIMIT {max}"`. 없으면 capture add/F1 절차를 안내하고 중단.
2. **고지·확인** — 왕복 횟수, 예상 소요(왕복당 ~30초+), ToS 리스크(최초 1회) → 사용자 확인.
3. **run 시작** — `python scripts/record_check.py start --project {P}` → run_id 확보.
4. **엔진별 수집** — 엔진당 탭 1개를 만들어 재사용하고, 프롬프트마다 **새 채팅**으로
   시작한다(이전 답변이 다음 답변을 오염시키지 않게).
   - **perplexity.ai** — 검색창에 프롬프트 입력 → 답변 완료 대기 → 본문 + 출처(Sources)
     링크 URL 수집.
   - **chatgpt.com** — 웹 검색이 켜지는 UI(토글·버튼)가 보이면 활성화, 없으면 기본
     모드로 진행하고 그 사실을 기록 노트에 남긴다. 본문 + 인용 링크 수집.
   - **gemini.google.com** — 본문 + 출처 링크 수집.
   - 추출은 `get_page_text`/`read_page`로 하되, 답변 스트리밍이 끝난 뒤에 읽는다.
     인라인 링크는 답변 본문 URL로 수집되며, 없으면 record가 본문에서 자동 추출한다.
5. **즉시 기록** — 답변 하나 끝날 때마다: 전문을 임시 파일(세션 스크래치 디렉터리)에
   저장하고
   `python scripts/record_check.py record --project {P} --run-id {R} --prompt-id {id}
    --engine {engine}-web --answer-file {tmp} --urls "{u1,u2,...}"`
   출력(mentioned/cited/cited_domains)을 진행 로그로 보여준다.
6. **마감** — `python scripts/record_check.py finish --run-id {R} --checks {n}
   --notes "browser engines=... skipped=..."` → 매트릭스는 capture와 동일하게 sql로
   집계해 보여주고, `/capture gaps`·`/capture report`로의 다음 스텝을 안내한다.

## 스코프 밖

로그인 대행·캡차 우회(절대 금지), 대량·고빈도 수집(수집기가 아니라 측정기다),
API 수집(collect_ai의 영역), 네이버 표면(미탑재).
