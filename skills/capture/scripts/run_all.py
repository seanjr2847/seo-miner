#!/usr/bin/env python3
"""전체 수집 체인 러너 (run_all.py) — /capture run 한 줄 실행.

수집기 5~6개와 스코어링·리포트를 정해진 순서로 in-process 호출합니다.

실행 순서 및 이유:
  1. gsc      : 다른 모든 판정의 기본 재료. 실패 시 뒤가 빈손이므로 체인 중단.
  2. index    : 색인은 순위 이전의 문제. GSC 최신 스냅샷 상위 페이지 대상.
  3. keywords : 자동완성 키워드 발굴 (expand_keywords.py --mode all).
  4. rank     : 유료 SERP 순위 스냅샷. 키 없으면 건너뜀.
  5. ai       : 유료 AI 인용 체크. 키 없으면 건너뜀.
  6. gaps     : scoring.py load <project> (수집 결과를 읽어 기회 데이터 적재).
  7. report   : dashboard.py --export --project <project> (리포트 HTML 박제).

설계 원칙:
  - 각 수집기의 collect() 를 in-process 로 호출한다 (run_all.subprocess 도 있다 —
    scoring.py / dashboard.py 는 수정 금지 파일이라 그 둘은 그대로 subprocess 격리 실행).
  - sys.executable 사용 (Windows 스토어 파이썬 스텁 회피).
  - --dry-run 은 각 수집기에 위임하여 비용 고지.
  - 하나의 수집기가 실패해도 체인은 계속 진행 (단, gsc 실패 시 즉시 중단).
  - collect() 가 sys.exit 을 부르지 않고 StageResult 로 결과를 돌려주므로, 실패
    사유는 run_chain 의 요약표까지 그대로 올라온다.
"""
import argparse
import subprocess
import sys
from collections import namedtuple
from datetime import datetime
from pathlib import Path

# db import를 통해 CAPTURE_HOME/env 자동 로딩 및 콘솔 UTF-8 설정 적용
sys.path.insert(0, str(Path(__file__).resolve().parent))
import collect_ai          # noqa: E402
import collect_gap         # noqa: E402
import collect_gsc         # noqa: E402
import collect_index       # noqa: E402
import collect_serp        # noqa: E402
import db                  # noqa: E402
import expand_keywords     # noqa: E402
import serp_adapter        # noqa: E402

SCRIPTS_DIR = Path(__file__).resolve().parent
TIMEOUT_SECONDS = 1800  # 보고서 단계 subprocess 최대 30분 타임아웃

# 단계 정의. in-process 단계는 fn=collect 함수, 외부 스크립트 단계는 fn=None
# + cmd_tpl. 단계 순서·정의의 정본은 여기 하나다. 어떤 키가 있어야 유료 단계가
# 도는지는 check_paid_keys 가 답한다 — 그 판정을 여기에도 적어 두면 두 벌이 되고,
# 한쪽만 고쳐지는 사고가 난다(이 저장소가 반복해서 겪은 것).
Stage = namedtuple("Stage", ["name", "desc", "fn", "cmd_tpl", "is_paid"])
STAGES = (
    Stage("gsc",      "GSC 실적 적재 (합계·일별·디바이스 분해)",
          collect_gsc.collect,     None, False),
    Stage("index",    "URL 색인 상태 수집",
          collect_index.collect,   None, False),
    Stage("keywords", "자동완성 키워드 발굴",
          expand_keywords.collect, None, False),
    Stage("rank",     "순위 스냅샷 수집",
          collect_serp.collect,    None, True),
    Stage("ai",       "AI 인용 체크",
          collect_ai.collect,      None, True),
    Stage("gaps",     "기회 적재 (외부 호출 0)",
          None,                    ["scoring.py", "load", "{project}"], False),
    Stage("report",   "리포트 HTML 박제",
          None,                    ["dashboard.py", "--export", "--project", "{project}"], False),
)

VALID_STAGE_NAMES = tuple(s.name for s in STAGES)
DRY_RUN_UNSUPPORTED = {"gaps", "report"}  # 외부 호출 0건으로 --dry-run 플래그가 없는 단계


def check_paid_keys(stage_name: str) -> tuple[bool, str]:
    """유료 수집 단계의 환경변수 키 존재 여부를 확인합니다.

    Returns:
        (키_보유_여부, 미보유시_건너뜀_사유_문구)
    """
    if stage_name == "rank":
        if not (serp_adapter.has_serper() or serp_adapter.has_dataforseo()):
            return False, ("SERPER_API_KEY 또는 DATAFORSEO_LOGIN/DATAFORSEO_PASSWORD 가 없어 건너뜀 — "
                          "있으면 순위 스냅샷을 캘 수 있습니다. 발급: https://dataforseo.com "
                          "(권장) 또는 https://serper.dev")
    elif stage_name == "ai":
        if not serp_adapter.has_openrouter():
            return False, ("OPENROUTER_API_KEY 가 없어 건너뜀 — 있으면 AI 인용 갭을 캘 수 있습니다. "
                          "발급: https://openrouter.ai/keys")
    return True, ""


def run_chain(
    project: str,
    *,
    dry_run: bool = False,
    skip: str | list[str] | set[str] | None = None,
    only: str | list[str] | set[str] | None = None,
) -> int:
    """전체 수집 체인을 순서대로 실행합니다.

    Args:
        project: 프로젝트 이름
        dry_run: 실제 실행 대신 호출 계획 및 비용 확인 모드 여부
        skip: 건너뛸 단계 이름들 (쉼표 구분 문자열 또는 컬렉션)
        only: 실행할 단계 이름들 (쉼표 구분 문자열 또는 컬렉션)

    Returns:
        0: 모든 단계 성공 (건너뜀 포함)
        1: 하나 이상의 단계 실패 또는 gsc 실패로 중단
    """
    # 1. skip / only 인자 파싱 및 검증
    if skip and only:
        print("[오류] --skip 과 --only 옵션은 동시에 사용할 수 없습니다.", file=sys.stderr)
        return 1

    skip_set: set[str] = set()
    if isinstance(skip, str):
        skip_set = {x.strip() for x in skip.split(",") if x.strip()}
    elif skip:
        skip_set = {str(x).strip() for x in skip if str(x).strip()}

    only_set: set[str] = set()
    if isinstance(only, str):
        only_set = {x.strip() for x in only.split(",") if x.strip()}
    elif only:
        only_set = {str(x).strip() for x in only if str(x).strip()}

    invalid_skips = skip_set - set(VALID_STAGE_NAMES)
    if invalid_skips:
        print(
            f"[오류] 유효하지 않은 skip 단계 이름: {', '.join(sorted(invalid_skips))} — "
            f"가능한 단계: {', '.join(VALID_STAGE_NAMES)}",
            file=sys.stderr,
        )
        return 1

    invalid_only = only_set - set(VALID_STAGE_NAMES)
    if invalid_only:
        print(
            f"[오류] 유효하지 않은 only 단계 이름: {', '.join(sorted(invalid_only))} — "
            f"가능한 단계: {', '.join(VALID_STAGE_NAMES)}",
            file=sys.stderr,
        )
        return 1

    results: list[tuple[str, str, str]] = []  # (stage_name, status, reason)
    total_stages = len(STAGES)
    has_failure = False
    gsc_aborted = False

    separator = "=" * 62
    sub_separator = "-" * 62

    print(f"\n{separator}")
    print(f"체인 러너 시작: 프로젝트 '{project}' (dry_run={dry_run})")
    print(f"{separator}")

    for idx, stage in enumerate(STAGES, start=1):
        # gsc 가 실패했을 때 나머지 단계는 실행하지 않고 중단 상태로 기록
        if gsc_aborted:
            results.append((stage.name, "미실행", "GSC 수집 실패로 체인 중단됨"))
            continue

        print(f"\n[{idx}/{total_stages}] {stage.name} — {stage.desc}")
        print(sub_separator)

        # 필터링 1: --skip 옵션 확인
        if stage.name in skip_set:
            skip_reason = "--skip 옵션으로 건너뜀"
            print(f"[{stage.name}] {skip_reason}")
            results.append((stage.name, "건너뜀", skip_reason))
            continue

        # 필터링 2: --only 옵션 확인
        if only_set and stage.name not in only_set:
            skip_reason = "--only 대상이 아니므로 건너뜀"
            print(f"[{stage.name}] {skip_reason}")
            results.append((stage.name, "건너뜀", skip_reason))
            continue

        # 필터링 3: 유료 API 키 확인
        if stage.is_paid:
            has_keys, paid_skip_msg = check_paid_keys(stage.name)
            if not has_keys:
                print(f"[{stage.name}] {paid_skip_msg}")
                results.append((stage.name, "건너뜀", paid_skip_msg))
                continue

        # dry-run 처리: scoring load 및 dashboard export 는 --dry-run 플래그가 없으므로 예정 문구만 출력
        if dry_run and stage.name in DRY_RUN_UNSUPPORTED:
            if stage.name == "gaps":
                print(f"[gaps] 외부 호출 0건 — 실제 실행 시 기회를 적재합니다 (돌 예정)")
            elif stage.name == "report":
                print(f"[report] 외부 호출 0건 — 실제 실행 시 대시보드 리포트 HTML을 내보냅니다 (돌 예정)")
            results.append((stage.name, "돌 예정", ""))
            continue

        # 실행 — in-process 호출이 표준. 외부 스크립트(gaps/report)만 subprocess 격리.
        try:
            if stage.fn is not None:
                # in-process 호출. 각 collect() 의 kwargs 는 project + dry_run 만.
                # 나머지는 그 파일의 기본값으로 떨어진다 (CLI 의 default 와 일치).
                r = stage.fn(project=project, dry_run=dry_run)
                if not r.ok:
                    fail_reason = r.reason or "실패"
                    results.append((stage.name, "실패", fail_reason))
                    has_failure = True
                    print(f"\n[오류] 단계 '{stage.name}' 실행 실패 ({fail_reason})", file=sys.stderr)
                    if stage.name == "gsc":
                        print(
                            "\n[중단] GSC 실적 수집이 실패하여 체인을 중단합니다. "
                            "나머지 모든 단계가 GSC 데이터를 기본 재료로 사용하므로 진행할 수 없습니다.",
                            file=sys.stderr,
                        )
                        gsc_aborted = True
                elif r.skipped:
                    # dry-run 의 정상 종료도 skipped=True 로 들어온다 — 표시를 구분한다.
                    status = "완료 (계획 확인)" if dry_run else "건너뜀"
                    results.append((stage.name, status, r.reason or ""))
                else:
                    status = "완료 (계획 확인)" if dry_run else "완료"
                    results.append((stage.name, status, ""))
            else:
                # 외부 스크립트 단계 (gaps, report). 수정 금지 파일이라 subprocess 그대로.
                cmd = [sys.executable, str(SCRIPTS_DIR / stage.cmd_tpl[0])]
                for arg in stage.cmd_tpl[1:]:
                    cmd.append(arg.format(project=project))
                if dry_run:
                    cmd.append("--dry-run")
                try:
                    res = subprocess.run(cmd, timeout=TIMEOUT_SECONDS)
                except subprocess.TimeoutExpired:
                    fail_reason = f"타임아웃 ({TIMEOUT_SECONDS}초 초과)"
                    results.append((stage.name, "실패", fail_reason))
                    has_failure = True
                    print(f"\n[오류] 단계 '{stage.name}' 타임아웃 ({TIMEOUT_SECONDS}초 초과)", file=sys.stderr)
                    continue
                if res.returncode == 0:
                    results.append((stage.name, "완료", ""))
                else:
                    fail_reason = f"exit code {res.returncode}"
                    results.append((stage.name, "실패", fail_reason))
                    has_failure = True
                    print(f"\n[오류] 단계 '{stage.name}' 실행 실패 ({fail_reason})", file=sys.stderr)
                    if stage.name == "gsc":
                        print(
                            "\n[중단] GSC 실적 수집이 실패하여 체인을 중단합니다. "
                            "나머지 모든 단계가 GSC 데이터를 기본 재료로 사용하므로 진행할 수 없습니다.",
                            file=sys.stderr,
                        )
                        gsc_aborted = True
        except Exception as e:
            fail_reason = f"예외 발생 ({e})"
            results.append((stage.name, "실패", fail_reason))
            has_failure = True
            print(f"\n[오류] 단계 '{stage.name}' 실행 중 예외 발생: {e}", file=sys.stderr)
            if stage.name == "gsc":
                print(
                    "\n[중단] GSC 실적 수집 중 예외가 발생하여 체인을 중단합니다. "
                    "나머지 모든 단계가 GSC 데이터를 기본 재료로 사용하므로 진행할 수 없습니다.",
                    file=sys.stderr,
                )
                gsc_aborted = True

    # 최종 결과 요약 표 출력
    # 경로 규칙의 정본은 dashboard.export() 다. 여기서는 출력을 흘려보내느라
    # 그 반환값을 못 받아서 같은 규칙으로 다시 계산한다 — 저쪽이 바뀌면 여기도 바꾼다.
    today_str = datetime.now().strftime("%Y-%m-%d")
    report_file = db.CAPTURE_HOME / "reports" / project / f"{today_str}.html"

    print(f"\n{separator}")
    print(f"체인 실행 결과 요약 ({project})")
    print(sub_separator)
    for stage_name, status, reason in results:
        if reason:
            print(f"  {stage_name:<10} | {status} ({reason})")
        else:
            print(f"  {stage_name:<10} | {status}")
    print(separator)

    print("\n다음 작업:")
    if gsc_aborted:
        # 여기서 끝나면 사용자는 빈손이다. 인증 없이 도는 유일한 수집으로 안내해
        # 첫 수확이라도 남긴다 — 로그인 실패가 곧 "아무것도 못 봄"이 되지 않게.
        print('  구글 로그인 먼저: 채팅에 "GSC 로그인해줘"')
        print(f"  로그인 없이 지금 되는 것: /capture keywords {project} "
              "(자동완성 — 인증·키 안 씀)\n")
        return 1

    print(f"  리포트 파일: {report_file}")
    print(f"  대시보드 실행: /capture dash {project}")
    top = _top_opportunity(project)
    if top:
        # 리포트 경로만 주고 끝내면 "그래서 뭘 고치나"로 안 이어진다 — 이 도구의
        # 유일한 실제 성과는 고치기(create)이고, 그 문턱을 여기서 한 줄로 낮춘다.
        print(f"  손댈 것 1순위: [{top['kind']}] {top['target']}")
        print(f"  고치러 가기: /create plan {project}")
    # 2회차 수집부터 Δ가 나온다. 그 2회차를 부르는 것이 아무데도 없었다.
    print(f"  다음 바퀴: 1~2주 뒤 같은 명령 한 줄 — /capture run {project} "
          "(구글이 최근 3일 수치를 나중에 채우고, 순위도 주 단위로 움직인다)\n")

    return 1 if has_failure else 0


def _top_opportunity(project: str):
    """요약에 붙일 기회 1건. 실패해도 요약을 깨뜨리지 않는다 — 장식이지 결과가 아니다."""
    try:
        conn = db.connect()
        try:
            p = db.get_project(conn, project)
            row = conn.execute(
                "SELECT kind, target FROM opportunities "
                " WHERE project_id=? AND status='new' "
                " ORDER BY score DESC, id DESC LIMIT 1", (p["id"],)).fetchone()
            return {"kind": row["kind"], "target": row["target"]} if row else None
        finally:
            conn.close()
    except Exception:
        return None



def main() -> None:
    ap = argparse.ArgumentParser(
        description="전체 수집 체인 실행 — gsc → index → keywords → rank → ai → gaps → report"
    )
    ap.add_argument("--project", required=True, help="프로젝트 이름")
    ap.add_argument("--dry-run", action="store_true", help="실제 실행 없이 호출 계획 및 비용만 확인")
    ap.add_argument("--skip", help="건너뛸 단계 (쉼표 구분, 예: index,ai)")
    ap.add_argument("--only", help="실행할 단계 (쉼표 구분, 예: gsc,gaps)")
    args = ap.parse_args()

    code = run_chain(
        args.project,
        dry_run=args.dry_run,
        skip=args.skip,
        only=args.only,
    )
    sys.exit(code)


if __name__ == "__main__":
    main()
