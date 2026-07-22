# 리포 프로필 발견 절차 (스택 불문성의 핵심)

목표: "이 리포에서 콘텐츠는 어디에, 어떤 형식으로 살고, 어떻게 발행되는가"를
데이터로 뽑아 `{P}.repo.yaml`에 캐싱한다. 스킬 코드가 아니라 이 프로필이
스택 지식을 담는다.

## 1. 스택 탐지 (신호 → 판정)

| 신호 파일 | 스택 | 콘텐츠 기본 위치 후보 |
|-----------|------|----------------------|
| astro.config.* | Astro | src/content/{collection}/, src/pages/ |
| next.config.* | Next.js | content/, posts/, app/(...)/page.mdx, data/ |
| hugo.toml·config.toml+archetypes/ | Hugo | content/{section}/ |
| _config.yml | Jekyll | _posts/, collections |
| gatsby-config.* | Gatsby | content/, src/pages/ |
| nuxt.config.* | Nuxt | content/ |
| package.json만 + .md 다수 | 정적/커스텀 | grep으로 .md/.mdx 밀집 디렉토리 |
| *.html 다수, 빌드 설정 없음 | 순수 HTML | 페이지 파일 직접 |
| JSON/YAML/CSV 데이터 + 템플릿 | 디렉토리형(pSEO) | data/ + 다이나믹 라우트 |

판정 불가 시 사용자에게 묻는다 — 추측으로 진행하지 않는다.

## 2. 관례 학습 (기존 파일 2~3개 관찰)

- **frontmatter 스키마**: 실제 사용 중인 키만 기록 (title, description, date,
  tags, draft, image, ...). 타입·날짜 포맷·필수 여부 포함.
- **파일명 규칙**: kebab-case? 날짜 접두? 슬러그=파일명?
- **URL 패턴**: 라우팅 설정·permalink에서 유도 (예: /blog/{slug}).
- **본문 관례**: 제목 레벨 시작(H1 vs H2), 이미지 경로 방식, 내부링크 스타일,
  컴포넌트/숏코드 사용 여부.
- **pSEO 메커니즘**(디렉토리·대량 페이지용): 다이나믹 라우트 파일
  (예: Astro `[slug].astro` + 콘텐츠 컬렉션, Next `[slug]/page.tsx` +
  generateStaticParams, Hugo data 템플릿)과 데이터 파일 위치를 식별.
  못 찾으면 "pSEO 인프라 없음"으로 기록 — run 때 최소 구조 신설을 제안.

## 3. 보이스 프로필

`product-marketing-context.md`·`.claude/product-marketing.md`류가 있으면 그걸
정본으로. 없으면 기존 글에서 요약: 어조(존댓말/반말/건조), 평균 문단 길이,
제목 스타일(질문형/숫자형), 금지 표현. 3~5줄이면 충분.

## 4. 발행 모드

- `files`: 리포 파일 = 발행물 (git push → 자동 배포). 기본값.
- `drafts`: 콘텐츠가 외부 CMS(워드프레스 등)에 삶 → 산출물을
  `$CAPTURE_HOME/drafts/{P}/`에 쓰고 사용자 파이프라인으로 핸드오프.
- git 여부: `.git` 존재 확인. 없으면 게이트를 "백업+변경목록 보고"로 대체.

## 5. 프로필 저장

`templates/repo-profile.template.yaml` 형식으로
`$CAPTURE_HOME/projects/{P}.repo.yaml`에 저장 후 반드시 사용자 검수.
리포 구조가 바뀌면 `/create profile {P}` 재실행으로 갱신.
