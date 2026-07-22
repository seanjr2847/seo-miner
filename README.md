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
- 필수: `pip install requests jinja2 pyyaml`
- AI 인용 체크: `OPENROUTER_API_KEY`
- GSC: `pip install google-api-python-client google-auth-oauthlib` + OAuth (setup.md 4절)
- SERP(선택): `DATAFORSEO_LOGIN`/`DATAFORSEO_PASSWORD` 또는 `SERPER_API_KEY` (setup.md 7절)

## 시작
```
/setup                        # 설치 직후 환경 진단 — 뭐가 되고 뭐가 빠졌는지
/capture add <프로젝트>        # 온보딩 (game|local_clinic|saas|directory)
/capture run <프로젝트>        # gsc → rank → ai → 분석 → HTML 리포트
/browse <프로젝트>             # API 키 없이 브라우저(claude-in-chrome)로 실제 앱 인용 실측
/create profile <프로젝트>     # 리포 관례 발견·캐싱
/create plan <프로젝트>        # Brain 기회 → 작업 배치 → 브랜치+PR
```
데이터는 `~/.capture/`에 저장되어 플러그인 업데이트와 무관하게 유지됩니다.
