"""백링크 — capture 단계로 내려간 뒤 남은 얇은 위임층.

수집·조회의 정본은 이제 `skills/capture/scripts/collect_backlinks.py` 다. 실행은
run_all.py 의 단계표가 collect_backlinks.collect 를 직접 부른다(run_all.py:97) —
여기 남는 것은 app.py 가 화면에 보여줄 조회 두 가지, latest()/available() 뿐이다.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "skills" / "capture" / "scripts"))

import collect_backlinks                         # noqa: E402
import serp_adapter                              # noqa: E402

# 대시보드 조회는 그대로 위임 — 반환 모양의 정본은 collect_backlinks.latest.
latest = collect_backlinks.latest


def available() -> bool:
    return serp_adapter.has_dataforseo()


def demo() -> None:
    """공개 이름이 그대로 살아 있는지 — 호출부(app.py)가 쓰는 것만 본다."""
    assert callable(available) and callable(latest)
    assert latest is collect_backlinks.latest
    print("backlinks: ok")


if __name__ == "__main__":
    demo()
