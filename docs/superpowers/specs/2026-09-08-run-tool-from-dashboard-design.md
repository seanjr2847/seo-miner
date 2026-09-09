# 대시보드에서 개발 도구 실행 — 요청문을 복사하지 않고 바로 연다

2026-09-08. 상태: 승인된 설계. 앞선 설계 `2026-09-08-keyword-triage-design.md` 위에 선다.

## 왜

기회 카드의 끝은 "요청문 복사 → AI 에 붙여 넣기"였다. 사용자는 그 자리에서 **자기
개발 도구(Claude Code·Codex·OpenCode·pi)가 그 사이트 폴더에서 열리고 요청문이
이미 들어가 있기**를 원한다. 호스팅 화면에서도 로컬에서도 같은 결과를 봐야 한다.

호스팅 화면(브라우저)은 사용자 PC 의 프로세스를 못 띄운다. 그래서 실행은 로컬
대시보드가 맡고, 호스팅 화면은 "이 PC 에서 로컬 대시보드를 띄우세요"라고 안내한다.
결과(작업 기록·진행 중)는 서버 Brain 한 곳에 남아 두 화면이 같은 표를 본다.

서버가 글을 써서 GitHub PR 을 내던 길(`/api/create`·writer·gh)은 이 흐름과 겹치고
필요가 없어졌다 — 떼어 낸다.

## 결정 요약

| 물음 | 답 |
|---|---|
| 실행 모양 | 터미널이 열리고 대화로 이어간다(헤드리스 아님) |
| 어디서 누르나 | 로컬 대시보드. 호스팅은 안내만 |
| 어느 폴더에서 | 사이트별 로컬 폴더(`~/.capture/dirs.json`). 없으면 `~/.capture/work/<사이트>/` |
| 도구별 차이 | 요청문이 첫 프롬프트. 도구는 실행 명령 한 줄만 다르다(`/create run` 안 씀) |
| 작업 기록 | 누르는 순간 진행 중(acked). 요청문 꼬리에 `createdb.py done …` 기록 명령 |
| 터미널 | Orca(기본, 감지되면) / 시스템 |
| 온보딩 | 설정 0단계 "쓰는 방식": 호스팅/이 PC · 도구 · 터미널 |
| GitHub 연동 | 제거 |

## 1. 온보딩 — 설정 화면 0단계 "쓰는 방식"

`views/settings.html` 의 1단계 위에 0단계가 선다. 세 항목이고 전부 이 PC 의 설정이다.
`~/.capture/env` 에 API 키와 같은 방식(`dashboard.save_keys`, `KEY_FIELDS`)으로 저장한다.

| 키 | 값 | 뜻 |
|---|---|---|
| `SEOMINER_MODE` | `hosted` \| `local` | 호스팅이 측정·키를 맡는가, 이 PC 가 맡는가 |
| `SEOMINER_TOOL` | `claude` \| `codex` \| `opencode` \| `pi` | 실행 버튼이 띄울 도구 |
| `SEOMINER_TERMINAL` | `orca` \| `system` | 터미널. 비면 orca 감지 시 orca, 아니면 system |

정본은 `skills/setup/scripts/doctor.py`:

```python
MODES = (("hosted", "호스팅 — 웹이 주기 측정과 키를 맡습니다"),
         ("local",  "이 PC — 측정·보관함·키 전부 여기서"))
TOOLS = (  # id, 라벨, 실행 파일, 첫 프롬프트를 넘기는 argv 꼴
    ("claude",   "Claude Code", "claude",   ["claude", "{prompt}"]),
    ("codex",    "Codex",       "codex",    ["codex", "{prompt}"]),
    ("opencode", "OpenCode",    "opencode", ["opencode", "--prompt", "{prompt}"]),
    ("pi",       "pi",          "pi",       ["pi", "{prompt}"]),
)
TERMINALS = (("orca", "Orca"), ("system", "시스템 터미널"))
```

`doctor.diagnose()` 가 `setup_payload` 에 `mode`·`tool`·`terminal`(고른 값), `tools`
(id 마다 `installed: bool`, `shutil.which`), `orca_ok: bool`(`orca` 가 PATH 에 있고
`orca status --json` 이 ok) 를 싣는다. CLI 진단 맨 아래 "호스팅 연결:" 옆에
"쓰는 방식: … / 도구: … / 터미널: …" 을 찍는다.

화면 동작:
- `hosted` 를 고르면 2단계(사이트 등록)·3단계(서치콘솔)·4단계(API 키)가 접히고
  (`hidden`), 대신 "웹 [설정] → 명령어로 연결하기 한 줄을 붙여 넣으세요" 칸이 선다.
  붙여 넣으면 `POST /api/setup/remote {line}` 이 `remote.link(url, token)` 을 돌린다.
  한 줄의 꼴은 웹 [설정]이 내는 것 그대로(`python … remote.py connect <url> <token>`)이고
  서버는 그 문자열에서 url 과 token 두 토큰만 뽑는다.
- `local` 이면 지금 1~6단계 그대로.
- 도구 항목마다 "설치됨 / 없음" 배지. 고른 도구가 없으면 경고 배지.
- 채팅 `/setup`(setup SKILL.md 표준 경로 1번)도 같은 세 질문을 묻고 같은 파일에 쓴다.
  선택지 문구는 doctor 의 표를 읽어 말한다(SKILL.md 에 사본을 두지 않는다).

호스팅 화면에는 이 단계가 없다(설정 화면 자체가 없다).

## 2. 로컬 대시보드 — 프록시, 로컬 폴더, 실행

### 2-1. 호스팅 사이트를 로컬 화면에 그린다 (프록시)

`dashboard.py main()` 의 "원격 사이트면 호스팅 주소로 넘긴다" 분기를 없앤다. 로컬
`Handler` 는 요청의 사이트가 `remote.owns(project)` 이면 `ROUTES` 의 그 경로를
로컬 함수 대신 `remote.api(method, path, params=…, json=…)` 로 서버에 보내고 응답
JSON 을 그대로 돌려준다. `/api/projects` 는 로컬 이름과 `remote.config()["projects"]`
를 합쳐(중복 제거, 정렬) 낸다. `/api/opp` 의 몸에 `project` 를 싣는다(셸의
`setOpp` 가 현재 사이트 이름을 넣는다) — 어느 서버로 보낼지 그걸로 안다.
`/api/doctor` 는 로컬 것(설정 화면용)이다.

원격 판정 함수는 하나: `dashboard.remote_project(project) -> bool` (= `remote.owns`).

### 2-2. 로컬 폴더

`~/.capture/dirs.json` = `{"<사이트>": "<절대경로>"}`. 읽기·쓰기는 `paths.py`:
`site_dirs() -> dict`, `set_site_dir(name, path|None)`. 설정 화면에 "사이트별 로컬
폴더" 표(사이트 이름 · 폴더 입력 · 저장)가 0단계 아래에 서고, 사이트 등록 폼에도
"로컬 폴더" 칸이 붙는다(등록 시 같이 저장). 입력 칸의 datalist 는
`orca worktree ps --json` 의 `path` 들이다(orca 가 있을 때만, `GET /api/setup/dirs`
가 `{dirs, worktrees}` 로 함께 준다). `POST /api/setup/dir {project, path}` 가 저장.
빈 path 는 지운다. 경로가 폴더가 아니면 400.

### 2-3. 실행

기회 카드 접힌 줄, 상태 버튼 옆에 **"<도구 이름> 로 열기"** 버튼(로컬 전용 —
`SM.host.oppBtn` 의 로컬 기본 구현). 도구를 안 골랐거나 없으면 그 자리에
`<span class="badge warn">도구 없음</span>` + "[설정]에서 고르기" 보조 버튼.

`POST /api/setup/run-tool {project, id}` (로컬 전용, X-Token):

1. 기회와 요청문을 얻는다 — 로컬이면 `payload(project)["opps"]` 에서, 원격이면
   프록시 `/api/data` 에서. `o.brief.body` 가 요청문이다(brief.attach 가 만든 것).
2. `~/.capture/work/<사이트>/opp-<id>.md` 에 쓴다: 요청문 + 빈 줄 + 기록 꼬리
   `끝나면 이 명령으로 기록해 주세요: python "<createdb.py 절대경로>" done <사이트> <id> --path <바꾼 파일> --branch <브랜치>`.
3. 작업 폴더 = `site_dirs().get(project)` 가 있고 폴더면 그것, 아니면
   `~/.capture/work/<사이트>/` (만든다). 폴더 없는 경우는 (c) 모드다.
4. 도구 명령 = `doctor.TOOLS` 의 argv 에 `{prompt}` = `이 파일의 요청문대로 진행해 주세요: <md 절대경로>`.
5. 터미널:
   - `orca`: `orca terminal create --worktree path:<폴더> --title "seo-miner · <사이트> #<id>" --command "<argv 를 셸 한 줄로>" --focus --json`. 실패(폴더가 워크트리가 아님 등)면 system 으로 물러나고 응답에 `fallback: "system"` 을 싣는다.
   - `system`: Windows `subprocess.Popen(["cmd", "/c", "start", "", *argv], cwd=폴더)`,
     macOS `open -a Terminal` 로 폴더를 열고 `osascript` 로 명령을 넣는다, Linux
     `x-terminal-emulator -e <argv>`.
6. 기회를 `acked` 로 (로컬 `db.set_opportunity_status` / 원격 프록시 `/api/opp`).
7. 응답 `{ok, terminal: "orca"|"system", cwd, file, fallback?}`. 실패는 `{ok:false, error}` 400:
   도구 미선택·도구 없음·요청문 없음. 상태는 안 바꾼다.

`dry_run: true` 를 몸에 실으면 6 을 빼고 5 도 안 띄우며 조립한 명령만 돌려준다
(검사용).

## 3. 호스팅 쪽과 기록 이음매

- **호스팅 기회 카드**: `server/assets/dash.html` 의 `SM.host.oppBtn(id)` 는
  `<span class="badge">이 PC 에서 열기</span>` + 명령 칩 `/capture dash <사이트>`
  (복사) + 짧은 설명 "로컬 대시보드를 띄우면 이 자리에 실행 버튼이 생깁니다".
  GitHub 분기는 없다.
- **`POST /api/creation {project, opportunity_id, path, branch, note}`**:
  `db.record_creation` + `db.set_opportunity_status(acked)`. 본체는 `dashboard.ROUTES`
  (`dashboard.record_creation_route(body)`), 호스팅 `app.py` 가 `/api/opp` 처럼 감싼다.
  `opportunity_id` 가 그 사이트 것이 아니면 404.
- **`createdb.py` 가 원격을 안다**: 사이트가 `remote.owns` 이면
  `pick` → `remote.api("GET", "/api/data")["opps"]` 에서 `new|acked` 만,
  `claim` → `/api/opp {project, id, status:"acked"}`,
  `done`/`sync` 의 `_mark_done` → `/api/creation`,
  `list` → `/api/data["creations"]`, `merged` → 로컬만(원격은 "웹에서는 안 됩니다" 안내).
  로컬 사이트는 지금 그대로.
- 요청문 꼬리의 기록 명령은 로컬·원격 구분 없이 같은 한 줄이다.

## 4. GitHub 연동 제거

- `server/app.py`: `/auth/github`·`/auth/github/callback`·`/api/repos`·`/api/repo`·
  `/api/create` 와 `_create_content`, `import writer`, `gh` 사용. `/api/settings` 응답의
  `repo`·`repo_branch`·`github_connected`·`github_enabled` 키.
- `server/writer.py`·`server/gh.py` 삭제. `server/settings.py` 의 `GITHUB_*` 셋.
- `server/store.py` 의 `set_repo`·`set_profile`·`github`(토큰) 함수와 그 자체점검.
  `sites` 표의 `repo*` 열은 남긴다(안 읽는다).
- `server/assets/dash.html`: "저장소 연결" 칸(`repoRender`·`repoPick`·`repoSave`),
  `firstTodo` 의 repo 분기, `SM.host.write`. `test_seams` 5·12 가 죽은 호출을 잡는다.
- README·SKILL·`docs/` 의 "PR 자동 생성" 문장.
- `requirements` 에서 GitHub 전용 의존이 있으면 뺀다.

## 5. 검사

- `test_seams 18`: `doctor.TOOLS` 의 id 가 `settings.html` 선택지와 `dashboard.py`
  `run-tool` 본체가 쓰는 것과 같은 한 벌. `/api/creation` 이 로컬 `ROUTES` 와 호스팅에
  다 있다. `createdb.py` 가 `remote.owns` 를 보고 `/api/creation` 을 부른다.
  `dash.html` 에 `github`·`/api/create`·`/api/repo` 가 없다.
- `test_dashboard`: 프록시 — `remote.owns` 를 참으로 흉내 내고 `remote.api` 를 가짜로
  바꿔 `/api/data?project=원격` 이 그 가짜를 부르는지. `run-tool dry_run` 이 md 파일을
  쓰고 도구별 argv 를 맞게 조립하는지, 도구 미선택·없음이 400 인지, 폴더 없는 사이트가
  `work/` 로 가는지. `/api/creation` 이 기록하고 acked 로 바꾸는지.
- `test_render`: 설정 0단계 섹션 id 가 DOM 에 있다(view-def 가 `sections` 에 `usage`
  를 더한다). 도구 미선택 상태의 기회 카드에 "도구 없음" 배지.
- `test_createdb`: 원격 흉내(`remote.owns` 참 + 가짜 `api`)로 `done` 이 `/api/creation`
  을 부르는지.
- `server/app.py demo()`: 지워진 라우트가 404, `/api/creation` 이 401 without login.
- 브라우저: 로컬 대시보드에서 theotherskin 을 프록시로 열어 심사·기회 숫자가 호스팅과
  같은지, 실행 버튼이 Orca 터미널을 여는지 실제로 누른다.

## 안 하는 것

- 헤드리스 실행·결과 되받기.
- 도구별 스킬 이식(`/create` 를 Codex 에 옮기는 것).
- 호스팅 화면에서 PC 프로세스를 띄우는 동반 프로세스.
- `sites.repo*` 열 마이그레이션.
