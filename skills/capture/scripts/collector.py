#!/usr/bin/env python3
"""수집기 공통 서두 — 인자·설정 병합·프로젝트 열기.

수집기 다섯 개가 각자 갖고 있던 argparse 블록·부팅·설정 읽기를 여기로 모았다.
그 전에는 --throttle 기본값만 0.5/0.3/0.5 세 벌이었고 config.yaml 의
defaults·serp 섹션은 읽는 코드가 아예 없었다(장식).

우선순위:  CLI > 프로젝트 yaml > config.yaml defaults > 코드 리터럴

self-check:  python collector.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import db  # noqa: E402

CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"
_cache: dict | None = None


def config() -> dict:
    """스킬 레벨 config.yaml. 없거나 깨졌으면 빈 dict — 수집을 막지는 않는다."""
    global _cache
    if _cache is None:
        try:
            import yaml  # lazy: pyyaml 없이도 import는 되게
            _cache = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
        except Exception as e:
            print(f"[경고] config.yaml 을 읽지 못했습니다 ({e}) — 코드 기본값으로 진행합니다.",
                  file=sys.stderr)
            _cache = {}
    return _cache


def resolve(cli, project_cfg: dict | None, key: str, fallback):
    """설정 하나를 우선순위대로 고른다. CLI 기본값은 반드시 None 이어야 한다 —
    argparse 리터럴이 들어 있으면 그게 항상 이겨서 설정 파일이 죽는다."""
    if cli is not None:
        return cli
    if project_cfg and project_cfg.get(key) is not None:
        return project_cfg[key]
    d = (config().get("defaults") or {}).get(key)
    return fallback if d is None else d


def add_common(ap, *, dry_run: bool = True) -> None:
    """모든 수집기가 갖는 인자. --project 는 사이트 이름, --dry-run 은 비용 고지용."""
    ap.add_argument("--project", required=True)
    if dry_run:
        ap.add_argument("--dry-run", action="store_true",
                        help="실제 호출·저장 없이 무엇을 할지만 보여준다")


def add_throttle(ap) -> None:
    ap.add_argument("--throttle", type=float, default=None,
                    help="요청 간격(초). 기본은 config.yaml defaults.throttle")


def project_cfg(name: str) -> dict:
    """프로젝트 yaml — 없거나 pyyaml이 없으면 경고하고 빈 dict.

    yaml은 기본값·브랜드 별칭 같은 부가 정보라 없다고 수집을 막으면 안 된다.
    "키 하나 없이도 ②③④⑤가 돈다"가 이 도구의 약속인데, CSV 가져오기가
    yaml/pyyaml 때문에 죽으면 그 약속이 깨진다. 대신 조용히 넘어가지는 않는다 —
    별칭이 빠지면 브랜드 판정이 달라지므로 왜 달라졌는지 말해야 한다.
    """
    try:
        return db.load_project_yaml(name)
    except (SystemExit, Exception) as e:   # load_project_yaml은 없으면 sys.exit 한다
        print(f"[경고] '{name}' 프로젝트 설정(yaml)을 읽지 못했습니다 ({e or '없음'}) — "
              "기본값으로 진행합니다. 브랜드 별칭·한도 설정은 적용되지 않습니다.",
              file=sys.stderr)
        return {}


def open_project(name: str):
    """(conn, project_row, project_cfg) — 수집기 다섯 개가 같은 세 줄을 쓰던 자리.

    yaml은 등록할 때 기록해 둔 config_path 로 찾는다. 이름만으로 찾으면
    표준 폴더(~/.capture/projects) 밖에 둔 설정을 조용히 놓친다.
    """
    conn = db.connect()
    p = db.get_project(conn, name)
    return conn, p, project_cfg(p["config_path"] or name)


def limit_of(cfg: dict | None, key: str, fallback: int) -> int:
    """프로젝트 yaml limits.* — 수집기마다 다르게 파던 idiom 하나로."""
    return int(((cfg or {}).get("limits") or {}).get(key, fallback))


def _selfcheck() -> None:
    assert resolve(3, {"depth": 9}, "depth", 1) == 3           # CLI 최우선
    assert resolve(None, {"depth": 9}, "depth", 1) == 9        # 프로젝트 yaml
    assert resolve(None, None, "throttle", 0.7) in (0.5, 0.7)  # config.yaml 또는 리터럴
    assert resolve(None, {}, "없는키", 42) == 42
    assert limit_of({"limits": {"max_keywords": 5}}, "max_keywords", 99) == 5
    assert limit_of(None, "max_keywords", 99) == 99
    assert limit_of({}, "max_keywords", 99) == 99
    print("collector self-check ok")


if __name__ == "__main__":
    _selfcheck()
