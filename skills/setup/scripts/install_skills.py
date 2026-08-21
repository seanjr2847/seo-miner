#!/usr/bin/env python3
"""빠진 마케팅 스킬 설치 — Claude Code 플러그인 마켓플레이스에서 일괄 설치 (stdlib 전용).

이 스크립트가 존재하는 이유:
  기존에는 스킬 누락 시 '저장소 링크'만 안내하여 사용자가 수동으로 설치해야 했습니다.
  이 스크립트는 빠진 필수 마케팅 스킬을 확인하고, Claude Code CLI를 통해
  마켓플레이스 등록 및 플러그인 설치를 자동으로 수행합니다.

사용자 동의에 대한 원칙:
  동의는 이 스크립트가 받지 않습니다. 호출하는 쪽(Claude 대화 또는 대시보드 버튼)에서
  이미 사용자 허락을 받은 뒤에 호출하며, 스크립트는 실제 설치만 수행합니다. (input() 사용 금지)

마켓플레이스 및 플러그인 정보:
  - 저장소: https://github.com/coreyhaines31/marketingskills
  - 마켓플레이스 이름: marketingskills (coreyhaines31/marketingskills)
  - 플러그인 이름: marketing-skills
  - CLI 명령:
      claude plugin marketplace add coreyhaines31/marketingskills
      claude plugin install marketing-skills@marketingskills

CLI Usage:
  python install_skills.py          # 빠진 것 확인 후 설치 실행
  python install_skills.py --check  # 무엇이 빠졌는지만 출력하고 종료 (설치 안 함)
"""
import argparse
import shutil
import subprocess
import sys
from pathlib import Path

# 경로·콘솔 인코딩은 db 모듈에 위임한다 (stdlib 전용)
CAPTURE_SCRIPTS = Path(__file__).resolve().parents[2] / "capture" / "scripts"
sys.path.insert(0, str(CAPTURE_SCRIPTS))
import db  # noqa: E402

# 탐지 규칙은 doctor.py 를 그대로 import 한다 (중복 정의 방지)
sys.path.insert(0, str(Path(__file__).resolve().parent))
import doctor  # noqa: E402

MARKETPLACE_REPO = "coreyhaines31/marketingskills"
MARKETPLACE_NAME = "marketingskills"
PLUGIN_NAME = "marketing-skills"
TIMEOUT_SECONDS = 300


def get_missing_skills() -> list[str]:
    """빠진 필수 마케팅 스킬 이름 목록을 반환합니다 (doctor.py 의 판정 기준)."""
    return [k for k in doctor.MARKETING_SKILLS if not doctor.find_skill(k)]


def check_skills() -> int:
    """--check: 빠진 필수 스킬 이름과 개수를 출력하고 설치 없이 종료합니다."""
    missing = get_missing_skills()
    if not missing:
        print("모든 마케팅 스킬이 이미 설치돼 있습니다.")
        return 0

    total = len(doctor.MARKETING_SKILLS)
    print(f"필수 마케팅 스킬 {total}개 중 {len(missing)}개가 설치되어 있지 않습니다:")
    for k in missing:
        desc = doctor.MARKETING_SKILLS.get(k, "")
        if desc:
            print(f"  · {k}: {desc}")
        else:
            print(f"  · {k}")
    return 0


def install_skills() -> int:
    """빠진 마케팅 스킬을 확인하고 마켓플레이스 등록 및 플러그인 설치를 실행합니다."""
    missing = get_missing_skills()
    if not missing:
        print("모든 마케팅 스킬이 이미 설치돼 있습니다.")
        return 0

    print(f"빠진 마케팅 스킬 {len(missing)}개를 확인했습니다: {', '.join(missing)}")

    claude_bin = shutil.which("claude")
    if not claude_bin:
        print("claude 실행 파일을 찾을 수 없습니다.")
        print("PATH 환경변수에 claude가 있는지 확인하거나, 아래 명령을 직접 실행해 주세요:")
        print(f"  claude plugin marketplace add {MARKETPLACE_REPO}")
        print(f"  claude plugin install {PLUGIN_NAME}@{MARKETPLACE_NAME}")
        return 1

    # [1] 마켓플레이스 추가 (이미 등록되어 있으면 실패할 수 있으나 정상으로 취급하고 계속 진행)
    print(f"\n[1/2] 마켓플레이스 등록: claude plugin marketplace add {MARKETPLACE_REPO}")
    cmd_add = [claude_bin, "plugin", "marketplace", "add", MARKETPLACE_REPO]
    try:
        res_add = subprocess.run(cmd_add, timeout=TIMEOUT_SECONDS)
        if res_add.returncode != 0:
            print("마켓플레이스가 이미 등록돼 있는 것 같습니다. 플러그인 설치를 계속 진행합니다.")
    except subprocess.TimeoutExpired:
        print(f"마켓플레이스 등록 명령 시간이 초과({TIMEOUT_SECONDS}초)되었습니다.")
        return 1
    except Exception as e:
        print(f"마켓플레이스 등록 중 오류 발생 ({e}). 플러그인 설치를 계속 진행합니다.")

    # [2] 플러그인 설치 (성공 여부 판정 기준)
    print(f"\n[2/2] 플러그인 설치: claude plugin install {PLUGIN_NAME}@{MARKETPLACE_NAME}")
    # -y 는 대시보드 경로에서 **선택이 아니다**: capture_output 으로 부르면 stdout 이
    # TTY 가 아니고, 그때 이 플래그가 없으면 CLI 가 확인 프롬프트에서 막힌다
    # (`claude plugin install --help` 가 그렇게 명시한다).
    # -y 가 자동 승인하는 것은 "마켓플레이스가 선언한 실행 명령"인데, 이 플러그인은
    # source 가 "./" 인 순수 디렉토리라 실행할 명령이 없다(실측). 업스트림이 나중에
    # 실행 명령을 붙이면 이 자동 승인이 위험해진다 — 그때는 여기를 다시 봐야 한다.
    cmd_install = [claude_bin, "plugin", "install",
                   f"{PLUGIN_NAME}@{MARKETPLACE_NAME}", "-y"]
    try:
        res_install = subprocess.run(cmd_install, timeout=TIMEOUT_SECONDS)
        if res_install.returncode != 0:
            print(f"\n플러그인 설치에 실패했습니다 (exit code: {res_install.returncode}).")
            return 1
    except subprocess.TimeoutExpired:
        print(f"플러그인 설치 명령 시간이 초과({TIMEOUT_SECONDS}초)되었습니다.")
        return 1
    except Exception as e:
        print(f"\n플러그인 설치 실행 중 오류 발생: {e}")
        return 1

    # [3] 성공 안내
    print(f"\n마켓플레이스 '{MARKETPLACE_NAME}'의 '{PLUGIN_NAME}' 플러그인이 성공적으로 설치되었습니다.")
    print("적용은 Claude Code 재시작 후 이루어집니다. 진행 중인 Claude Code 세션을 다시 시작해 주세요.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="빠진 마케팅 스킬 설치 도구")
    parser.add_argument(
        "--check",
        action="store_true",
        help="무엇이 빠졌는지만 출력하고 종료합니다 (설치 안 함).",
    )
    args = parser.parse_args()
    if args.check:
        return check_skills()
    return install_skills()


if __name__ == "__main__":
    sys.exit(main())
