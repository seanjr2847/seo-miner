# seo-miner

개인용 검색·AI 가시성 플러그인. Boring Agent 역기획 — Capture(측정·발굴)와
Create(실행)를 로컬 Brain(SQLite)으로 잇는 복리 루프.

## 설치

**GitHub 경유(권장)** — 이 리포를 GitHub에 올린 뒤:
```
/plugin marketplace add <user>/<repo>
/plugin install seo-miner@seo-miner
```
**로컬 경로**:
```
/plugin marketplace add /path/to/seo-miner
/plugin install seo-miner@seo-miner
```

## 준비물 (references/setup.md 상세)

필수는 하나뿐입니다: `pip install requests jinja2 pyyaml`.
API 키는 **하나도 없어도** 키워드 발굴·구글 실적 읽기·AI 인용 확인·리포트가 다 됩니다.

| 원하는 것 | 키 없이 (기본) | 키를 쓰면 |
|---|---|---|
| 구글 실적(순위·클릭) | Search Console 내보내기 → CSV. **클릭은 Claude가 브라우저로 대신** 하고 다운로드 폴더에서 알아서 집어감 | OAuth 연결 시 자동 수집 + 1,000행 제한 해제 (setup.md 4-B) |
| AI 인용 확인 | `/browse` — 브라우저로 실제 앱에 직접 질문 | `OPENROUTER_API_KEY`로 프롬프트 수십 개 자동 (setup.md 5절) |
| 검색 순위 추적 | — | `DATAFORSEO_LOGIN`/`PASSWORD` 또는 `SERPER_API_KEY` (setup.md 7절) |

brain.db(로컬 저장소)는 첫 실행 때 자동으로 만들어집니다 — 초기화 명령 필요 없음.
GSC 자격증명은 `~/.capture/creds/{사이트}/`에 **사이트별로 분리** 저장됩니다 —
한 사이트의 구글 클라이언트가 다른 사이트의 Search Console을 열지 않습니다.

## 시작
```
/setup                        # 설치 직후 환경 진단 — 뭐가 되고 뭐가 빠졌는지
/setup web                    # 같은 걸 화면으로 — 부품 설치·사이트 등록·키 저장까지 폼에서
/capture add <프로젝트>        # 온보딩 (game|local_clinic|saas|directory)
/capture run <프로젝트>        # gsc → rank → ai → 분석 → HTML 리포트
/browse <프로젝트>             # API 키 없이 브라우저(claude-in-chrome)로 실제 앱 인용 실측
/create profile <프로젝트>     # 리포 관례 발견·캐싱
/create plan <프로젝트>        # Brain 기회 → 작업 배치 → 브랜치+PR
```
데이터는 `~/.capture/`에 저장되어 플러그인 업데이트와 무관하게 유지됩니다.
