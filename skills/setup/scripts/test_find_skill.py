#!/usr/bin/env python3
"""doctor.find_skill 은 **위임 대상 팩**의 스킬만 '설치됨'으로 센다 — `python test_find_skill.py`.

위임 상대는 coreyhaines31/marketingskills 의 marketing-skills 플러그인이다(external-skills.md).
예전에는 플러그인 캐시를 `*/*/*/skills/<이름>` 으로 훑어 아무 팩이든 잡았다 — claude-seo 의
`seo-audit`(주소 하나만 받는 다른 스킬)이 깔려 있으면 '설치됨'으로 세어, 정작 위임 상대가
없는데도 설치 안내가 사라졌다.
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import doctor  # noqa: E402
import install_skills  # noqa: E402


def _skill(root: Path, *parts: str) -> None:
    d = root.joinpath(*parts)
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text("# x\n", encoding="utf-8")


def _find(name: str, home: Path, cwd: Path) -> bool:
    saved = os.environ.pop("CLAUDE_SKILLS_DIR", None)      # 걸려 있으면 그쪽만 본다 — 여기선 끈다
    try:
        return doctor.find_skill(name, home=home, cwd=cwd)
    finally:
        if saved is not None:
            os.environ["CLAUDE_SKILLS_DIR"] = saved


def test_other_packs_same_name_skill_is_not_installed():
    home, cwd = Path(tempfile.mkdtemp(prefix="sm-home-")), Path(tempfile.mkdtemp(prefix="sm-cwd-"))
    cache = home / ".claude" / "plugins" / "cache"
    # 다른 팩(claude-seo)의 같은 이름 스킬 — 위임 상대가 아니다
    _skill(cache, "agricidaniel-claude-seo", "claude-seo", "2.6.2", "skills", "seo-audit")
    _skill(cache, "agricidaniel-claude-seo", "claude-seo", "2.6.2", "skills", "schema")
    assert _find("seo-audit", home, cwd) is False, "claude-seo 의 seo-audit 를 '설치됨'으로 셌다"
    assert _find("schema", home, cwd) is False

    # 위임 대상 팩의 것이면 설치됨 (버전 폴더가 여럿이어도)
    src = (doctor.SKILLS_MARKETPLACE, doctor.SKILLS_PLUGIN)
    _skill(cache, *src, "2.14.28", "skills", "seo-audit")
    assert _find("seo-audit", home, cwd) is True
    assert _find("schema", home, cwd) is False, "팩에 없는 스킬까지 설치됨으로 셌다"

    # 마켓플레이스 이름만 같고 플러그인이 다르면 아니다
    _skill(cache, doctor.SKILLS_MARKETPLACE, "other-plugin", "1.0.0", "skills", "ai-seo")
    assert _find("ai-seo", home, cwd) is False


def test_user_placed_skills_still_count():
    """사용자가 직접 놓은 자리(~/.claude/skills, <작업 폴더>/.claude/skills)는 그대로 센다 —
    거기는 팩 이름이 남지 않아 출처를 가릴 수 없고, 사용자가 고른 것이다."""
    home, cwd = Path(tempfile.mkdtemp(prefix="sm-home-")), Path(tempfile.mkdtemp(prefix="sm-cwd-"))
    assert _find("ai-seo", home, cwd) is False
    _skill(home, ".claude", "skills", "ai-seo")
    _skill(cwd, ".claude", "skills", "schema")
    assert _find("ai-seo", home, cwd) is True and _find("schema", home, cwd) is True


def test_source_pack_names_are_one_copy():
    """설치하는 쪽(install_skills)과 찾는 쪽(doctor)이 같은 팩 이름 한 벌을 쓴다."""
    assert install_skills.MARKETPLACE_NAME is doctor.SKILLS_MARKETPLACE
    assert install_skills.PLUGIN_NAME is doctor.SKILLS_PLUGIN
    assert doctor.SKILLS_MARKETPLACE in doctor.SKILLS_REPO.replace("-", "")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)} passed")
