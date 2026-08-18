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


class _Setting:
    def __init__(self, dest: str, key: str, fallback, type_fn, help_text: str | None):
        self.dest = dest
        self.key = key
        self.fallback = fallback
        self.type_fn = type_fn
        self.help = help_text


_REGISTRY: dict[str, _Setting] = {}


def add_setting(ap, flag: str, *, key: str, fallback, type=int, help: str | None = None) -> None:
    """argparse에 default=None으로 등록하고 설정 메타데이터(dest, key, fallback, type)를 기록한다."""
    dest = flag.lstrip("-").replace("-", "_")
    spec = _Setting(dest=dest, key=key, fallback=fallback, type_fn=type, help_text=help)
    if not hasattr(ap, "_collector_settings"):
        ap._collector_settings = []
    ap._collector_settings.append(spec)
    _REGISTRY[key] = spec

    kwargs = {"type": type, "default": None}
    if help is not None:
        kwargs["help"] = help
    ap.add_argument(flag, **kwargs)


def settings(args, cfg: dict | None) -> dict:
    """등록된 모든 설정을 우선순위대로 해석한다:
    CLI (None 아니면, 0도 유효값) > 프로젝트 yaml > config.yaml defaults > fallback.
    limits.* 키는 프로젝트 yaml의 limits 아래에서 읽는다.
    """
    out = {}
    gcfg_defaults = (config().get("defaults") or {})

    specs = list(_REGISTRY.values())
    for spec in specs:
        val = None

        # 1. CLI (0도 유효값)
        if args is not None:
            if hasattr(args, spec.dest):
                cli_v = getattr(args, spec.dest)
                if cli_v is not None:
                    val = spec.type_fn(cli_v) if spec.type_fn else cli_v
            elif isinstance(args, dict):
                if spec.dest in args and args[spec.dest] is not None:
                    val = spec.type_fn(args[spec.dest]) if spec.type_fn else args[spec.dest]
                elif spec.key in args and args[spec.key] is not None:
                    val = spec.type_fn(args[spec.key]) if spec.type_fn else args[spec.key]

        # 2. 프로젝트 yaml
        if val is None and cfg and isinstance(cfg, dict):
            if spec.key.startswith("limits."):
                subkey = spec.key.split(".", 1)[1]
                limits_sec = cfg.get("limits")
                if isinstance(limits_sec, dict) and limits_sec.get(subkey) is not None:
                    raw = limits_sec[subkey]
                    val = spec.type_fn(raw) if spec.type_fn else raw
            else:
                if cfg.get(spec.key) is not None:
                    raw = cfg[spec.key]
                    val = spec.type_fn(raw) if spec.type_fn else raw

        # 3. config.yaml defaults
        if val is None:
            if spec.key in gcfg_defaults and gcfg_defaults[spec.key] is not None:
                raw = gcfg_defaults[spec.key]
                val = spec.type_fn(raw) if spec.type_fn else raw

        # 4. Fallback
        if val is None:
            raw = spec.fallback
            val = spec.type_fn(raw) if (spec.type_fn and raw is not None) else raw

        out[spec.key] = val
        out[spec.dest] = val

    return out


def add_common(ap, *, dry_run: bool = True) -> None:
    """모든 수집기가 갖는 인자. --project 는 사이트 이름, --dry-run 은 비용 고지용."""
    ap.add_argument("--project", required=True)
    if dry_run:
        ap.add_argument("--dry-run", action="store_true",
                        help="실제 호출·저장 없이 무엇을 할지만 보여준다")


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


def _selfcheck() -> None:
    import argparse
    ap = argparse.ArgumentParser()
    add_setting(ap, "--depth", key="serp_depth", fallback=10, type=int)
    add_setting(ap, "--throttle", key="throttle", fallback=0.7, type=float)
    add_setting(ap, "--max-keywords", key="limits.max_keywords", fallback=99, type=int)
    add_setting(ap, "--custom", key="custom_key", fallback=42, type=int)

    # 1. CLI 최우선 + 0도 유효값 (fallback을 이긴다)
    a = ap.parse_args(["--depth", "3", "--max-keywords", "0", "--throttle", "0"])
    s = settings(a, {"serp_depth": 9, "limits": {"max_keywords": 50}, "throttle": 1.0})
    assert s["serp_depth"] == 3
    assert s["limits.max_keywords"] == 0, "CLI 0이 프로젝트 yaml/fallback을 이겨야 한다"
    assert s["max_keywords"] == 0
    assert s["throttle"] == 0.0, "CLI 0.0이 이겨야 한다"

    # 2. 프로젝트 yaml
    a_none = ap.parse_args([])
    s_yaml = settings(a_none, {"serp_depth": 9, "limits": {"max_keywords": 5}})
    assert s_yaml["serp_depth"] == 9
    assert s_yaml["limits.max_keywords"] == 5

    # 3. config.yaml defaults
    s_cfg = settings(a_none, None)
    assert s_cfg["throttle"] in (0.5, 0.7)
    assert s_cfg["serp_depth"] == 10

    # 4. 코드 fallback
    s_fb = settings(a_none, {})
    assert s_fb["custom_key"] == 42
    assert s_fb["limits.max_keywords"] == 99

    print("collector self-check ok")


if __name__ == "__main__":
    _selfcheck()
