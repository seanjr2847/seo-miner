# ADR 0001 — plugin.json 의 version 은 설치 캐시를 깨는 신호다

## 상태

채택 (2026-08-25)

## 맥락

`.claude-plugin/plugin.json` 은 실질 6줄이고, 최근 릴리스 커밋의 대부분이 그중
`version` 한 줄만 바꾼다. 이 리포 안에는 그 값을 읽는 코드가 하나도 없다 —
git tag 없음, CHANGELOG 없음, `__version__` 없음, 어떤 스크립트도 파싱하지 않는다.
릴리스 사실은 이미 커밋 제목이 `(v1.33.0)` 형태로 담고 있다.

그래서 리포 안만 보면 이 줄은 "아무도 안 읽는 손 관리 상태"로 보이고,
아키텍처 리뷰가 주기적으로 "version 을 지우거나 자동 생성하자"를 제안한다.

실제 소비자는 리포 밖에 있다. 이 플러그인은
`/plugin marketplace add seanjr2847/seo-miner` + `/plugin install seo-miner@seo-miner`
로 설치되고, Claude Code 는 받은 사본을 `~/.claude/plugins/cache/` 아래에
캐시한다(`skills/setup/scripts/doctor.py` 가 그 경로를 뒤진다). 캐시가 갱신될지
말지를 가르는 것이 `plugin.json` 의 `version` 이다. 이 값이 그대로면 push 를 해도
설치된 쪽은 옛 사본을 계속 쓴다 — 실제로 "고쳤는데 안 고쳐졌다"가 세 번 재발했다.
갱신 명령도 풀네임(`seo-miner@seo-miner`)이어야 먹는다.

## 결정

- `plugin.json` 의 `version` 은 **릴리스마다 손으로 올린다**. 지우지 않고,
  자동 생성으로 대체하지 않는다. 이 줄은 사람이 관리하는 상태가 아니라
  설치 캐시 무효화 신호다.
- 아키텍처 산문의 집은 `README.md` 하나다. `plugin.json` 과 `marketplace.json` 의
  `description` 은 한 문장 + README 링크로 두고, **두 파일이 같은 문장을 쓴다**.
  둘 중 하나만 고쳐서 어긋나게 두지 않는다.

## 결과

- 릴리스 = `version` 범프 + push + 소비 측 `/plugin update seo-miner@seo-miner`.
  이 셋 중 하나라도 빠지면 변경이 사용자에게 도착하지 않는다.
- `version` 을 읽는 코드를 리포에서 못 찾더라도, 그것은 소비자가 없다는 뜻이
  아니다. 다음 리뷰는 이 문서를 근거로 그 제안을 다시 올리지 않는다.
- description 을 고칠 일이 생기면 두 JSON 을 함께 고친다. 긴 설명은 README 로
  가고, JSON 쪽은 짧은 채로 둔다.
