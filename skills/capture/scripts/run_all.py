#!/usr/bin/env python3
"""전체 수집 체인 러너 (run_all.py) — /capture run 한 줄 실행.

수집기와 스코어링·리포트를 정해진 순서로 호출하고, 각 단계가 돌려준
StageResult 를 그대로 호출자에게 넘긴다.

실행 순서 및 이유:
  1. gsc         : 다른 모든 판정의 기본 재료. 실패 시 뒤가 빈손이므로 체인 중단.
  2. ga4         : 클릭 뒤(세션·전환·이탈) — GSC 의 page 축과 잇는다. 속성 미연결이면 건너뜀.
  3. index       : 색인은 순위 이전의 문제. GSC 최신 스냅샷 상위 페이지 대상.
  4. keywords    : 자동완성 키워드 발굴 (expand_keywords.py --mode all).
  5. rank        : 유료 SERP 순위 스냅샷. 키 없으면 건너뜀.
  6. ai          : 유료 AI 인용 체크. 키 없으면 건너뜀.
  7. competitors : 유료 DataForSEO Labs 역키워드. 키 없으면 건너뜀.
  8. gaps        : scoring.py load <project> (수집 결과를 읽어 기회 데이터 적재).
  9. pages       : 내 페이지 HTML 감사 (기회에 걸린 URL 부터, 비용 0).
  10. report     : dashboard.py --export --project <project> (리포트 HTML 박제).

설계 원칙:
  - 표(STAGES)에는 디스패치가 한 종류뿐이다. 모든 단계가
    fn(project, *, dry_run, **opts) -> StageResult 다. scoring.load / dashboard.export 는
    직접 호출이다 — 나머지 열 단계와 마찬가지로 in-process 로 돈다. 둘 다 로컬
    sqlite/파일 I/O 뿐이라(외부 네트워크·os.environ 변경 없음) 격리할 이유가 없었고,
    scoring 은 이미 collect_* 절반이 물고 와 있었다.
  - --dry-run 은 각 단계에 위임하여 비용 고지.
  - 하나의 단계가 실패해도 체인은 계속 진행 (단, gsc 실패 시 즉시 중단).
  - run_chain 은 판정도 요약도 하지 않는다 — [(단계 이름, StageResult)] 를 돌려주고,
    종료코드(chain_rc)·총비용(chain_cost)·요약표(print_summary)는 그 결과에서 나온다.
    행 수·비용·산출물 경로가 정수 exit code 로 접혀 버려지던 자리가 이것이다.
"""
import argparse
import sys
from collections import namedtuple
from pathlib import Path

# db import를 통해 CAPTURE_HOME/env 자동 로딩 및 콘솔 UTF-8 설정 적용
sys.path.insert(0, str(Path(__file__).resolve().parent))
import collect_ai          # noqa: E402
import collect_backlinks   # noqa: E402
import collect_crawl       # noqa: E402
import collect_ga4         # noqa: E402
import collect_gap         # noqa: E402
import collect_gsc         # noqa: E402
import collect_index       # noqa: E402
import collect_metrics     # noqa: E402
import collect_page        # noqa: E402
import collect_serp        # noqa: E402
import collector           # noqa: E402
import dashboard           # noqa: E402
import db                  # noqa: E402
import expand_keywords     # noqa: E402
import scoring             # noqa: E402
import serp_adapter        # noqa: E402

StageResult = collector.StageResult

ABORT_REASON = "구글 실적 수집이 실패해 여기서 멈췄습니다"

SEPARATOR = "=" * 62
SUB_SEPARATOR = "-" * 62


def load_opportunities(project: str, *, dry_run: bool = False, **_opts) -> StageResult:
    """gaps 단계 — scoring.load. 외부 호출 0건이라 --dry-run 플래그가 없다."""
    if dry_run:
        reason = "외부 호출이 없습니다. 실제로 돌리면 기회를 뽑아 저장합니다"
        print(f"[gaps] {reason}")
        return StageResult(ok=True, skipped=True, reason=reason)
    scoring.load(project)
    return StageResult(ok=True)


def export_report(project: str, *, dry_run: bool = False, **_opts) -> StageResult:
    """report 단계 — dashboard.export. 산출물 경로 규칙의 정본은 dashboard.export() 고,
    여기서는 그게 돌려주는 Path 를 그대로 옮긴다(파싱 없음)."""
    if dry_run:
        reason = "외부 호출이 없습니다. 실제로 돌리면 보고서 HTML 을 내보냅니다"
        print(f"[report] {reason}")
        return StageResult(ok=True, skipped=True, reason=reason)
    out = dashboard.export(project)
    return StageResult(ok=True, artifact=str(out))


# 단계 정의. 표에는 디스패치가 한 종류뿐 — fn(project, *, dry_run, **opts) -> StageResult.
# 단계 순서·정의의 정본은 여기 하나다. 어떤 키가 있어야 유료 단계가 도는지는
# check_paid_keys 가 답한다 — 그 판정을 여기에도 적어 두면 두 벌이 되고, 한쪽만
# 고쳐지는 사고가 난다(이 저장소가 반복해서 겪은 것).
Stage = namedtuple("Stage", ["name", "desc", "fn", "is_paid"])
STAGES = (
    Stage("gsc",         "구글 실적 수집 (합계·일별·기기별)",       collect_gsc.collect,     False),
    Stage("ga4",         "GA4 실적 수집 (세션·전환·이탈, 페이지별)", collect_ga4.collect,    False),
    Stage("index",       "색인 상태 확인",                          collect_index.collect,   False),
    Stage("keywords",    "자동완성으로 키워드 찾기",                expand_keywords.collect, False),
    # 볼륨은 발굴 뒤·기회 적재 앞이어야 한다 — 점수가 볼륨을 재료로 쓴다.
    Stage("metrics",     "키워드 지표 조회 (검색량·난이도·CPC)",    collect_metrics.collect, True),
    Stage("rank",        "순위 확인",                               collect_serp.collect,    True),
    Stage("crawl",       "사이트 크롤 (깨진 링크·리다이렉트·고아)", collect_crawl.collect,   False),
    Stage("ai",          "AI 인용 확인",                            collect_ai.collect,      True),
    Stage("competitors", "경쟁사 역키워드·트래픽 몫 수집",          collect_gap.collect,     True),
    Stage("backlinks",   "백링크 프로필·앵커·링크 교집합 수집",     collect_backlinks.collect, True),
    Stage("gaps",        "손댈 것 뽑기 (외부 호출 없음)",           load_opportunities,      False),
    Stage("pages",       "내 페이지 점검 (제목·설명·본문·구조화 데이터)", collect_page.collect, False),
    Stage("report",      "보고서 HTML 내보내기",                    export_report,           False),
)

VALID_STAGE_NAMES = tuple(s.name for s in STAGES)


def check_paid_keys(stage_name: str) -> tuple[bool, str]:
    """유료 수집 단계의 환경변수 키 존재 여부를 확인합니다.

    Returns:
        (키_보유_여부, 미보유시_건너뜀_사유_문구)
    """
    if stage_name == "rank":
        if not (serp_adapter.has_serper() or serp_adapter.has_dataforseo()):
            return False, ("키가 없어 건너뜁니다. SERPER_API_KEY 또는 "
                          "DATAFORSEO_LOGIN/DATAFORSEO_PASSWORD 를 넣으면 순위를 "
                          "확인합니다. 발급: https://dataforseo.com (권장) 또는 "
                          "https://serper.dev")
    elif stage_name == "ai":
        if not serp_adapter.has_openrouter():
            return False, ("키가 없어 건너뜁니다. OPENROUTER_API_KEY 를 넣으면 AI 가 "
                          "누구를 인용하는지 확인합니다. 발급: https://openrouter.ai/keys")
    elif stage_name in ("metrics", "backlinks"):
        if not serp_adapter.has_dataforseo():
            what = ("검색량과 난이도를 조회합니다" if stage_name == "metrics"
                    else "백링크 프로필과 링크 교집합을 수집합니다")
            return False, ("키가 없어 건너뜁니다. DATAFORSEO_LOGIN/DATAFORSEO_PASSWORD "
                          f"를 넣으면 {what}. 발급: https://dataforseo.com")
    elif stage_name == "competitors":
        if not serp_adapter.has_dataforseo():
            return False, ("키가 없어 건너뜁니다. DATAFORSEO_LOGIN/DATAFORSEO_PASSWORD "
                          "를 넣으면 경쟁사는 잡는데 나는 없는 검색어를 찾습니다. "
                          "발급: https://dataforseo.com")
    return True, ""


def _stage_names(raw: str | None, label: str, valid: tuple[str, ...]) -> set[str]:
    """쉼표 구분 문자열 하나를 단계 이름 집합으로. 이름이 틀리면 ValueError."""
    names = {x.strip() for x in (raw or "").split(",") if x.strip()}
    invalid = names - set(valid)
    if invalid:
        raise ValueError(
            f"{label} 에 없는 단계 이름이 있습니다: {', '.join(sorted(invalid))}. "
            f"쓸 수 있는 단계: {', '.join(valid)}"
        )
    return names


def _coerce(v: str):
    """--opt 값의 타입 추정. 수집기 kwargs 가 int/float/bool 을 받는 자리가 있다."""
    s = v.strip()
    low = s.lower()
    if low in ("true", "false"):
        return low == "true"
    for cast in (int, float):
        try:
            return cast(s)
        except ValueError:
            pass
    return s


def parse_opts(items: list[str] | None) -> dict[str, dict]:
    """`--opt rank.device=mobile` 형태를 {단계: {kwarg: 값}} 으로 편다.

    수집기들이 이미 노출한 노브(--provider --depth --device --ids --force --limit
    --breakdown --row-limit --mode)가 체인으로도 도달하게 하는 통로다.
    """
    out: dict[str, dict] = {}
    for item in items or []:
        target, sep, value = (item or "").partition("=")
        if not sep:
            raise ValueError(f"--opt 형식이 아닙니다 (STAGE.KEY=VALUE): {item}")
        stage_name, dot, key = target.partition(".")
        if not dot or not key.strip() or not stage_name.strip():
            raise ValueError(f"--opt 형식이 아닙니다 (STAGE.KEY=VALUE): {item}")
        out.setdefault(stage_name.strip(), {})[key.strip().replace("-", "_")] = _coerce(value)
    return out


def _run_stage(stage, project: str, *, dry_run: bool, skip_set: set[str],
               only_set: set[str], opts: dict) -> StageResult:
    """단계 하나 — 건너뛸 이유를 먼저 보고, 아니면 fn 을 부른다."""
    if stage.name in skip_set:
        reason = "--skip 으로 지정해 건너뜁니다"
        print(f"[{stage.name}] {reason}")
        return StageResult(ok=True, skipped=True, reason=reason)

    if only_set and stage.name not in only_set:
        reason = "--only 대상이 아니라 건너뜁니다"
        print(f"[{stage.name}] {reason}")
        return StageResult(ok=True, skipped=True, reason=reason)

    if stage.is_paid:
        has_keys, paid_skip_msg = check_paid_keys(stage.name)
        if not has_keys:
            print(f"[{stage.name}] {paid_skip_msg}")
            return StageResult(ok=True, skipped=True, reason=paid_skip_msg)

    try:
        return stage.fn(project=project, dry_run=dry_run, **opts)
    except Exception as e:
        return StageResult(ok=False, reason=f"이 단계에서 오류가 났습니다 ({e})")


def run_chain(
    project: str,
    *,
    dry_run: bool = False,
    skip: str | None = None,
    only: str | None = None,
    opts: dict[str, dict] | None = None,
    stages: tuple = STAGES,
    on_stage=None,
) -> list[tuple[str, StageResult]]:
    """전체 수집 체인을 순서대로 실행하고 각 단계의 결과를 그대로 돌려줍니다.

    Args:
        project: 프로젝트 이름
        dry_run: 실제 실행 대신 호출 계획 및 비용 확인 모드 여부
        skip: 건너뛸 단계 이름 (쉼표 구분 문자열 하나)
        only: 실행할 단계 이름 (쉼표 구분 문자열 하나)
        opts: {단계 이름: {kwarg: 값}} — 그 단계의 collect() 에 그대로 넘어간다
        stages: 실행할 단계 표. 테스트가 가짜 표를 주입하는 자리다.
        on_stage: fn(idx, total, 단계 이름) — 단계 시작마다 불린다. 진행률 보고용이라
            여기서 난 예외는 삼킨다(보고가 수집을 죽이면 안 된다).

    Returns:
        [(단계 이름, StageResult), ...] — stages 순서 그대로, 단계당 한 건.
        종료코드는 chain_rc, 총비용은 chain_cost, 요약표는 print_summary 가 낸다.

    Raises:
        ValueError: skip/only 동시 지정, 잘못된 단계 이름, 잘못된 --opt 대상.
    """
    if skip and only:
        raise ValueError("--skip 과 --only 옵션은 동시에 사용할 수 없습니다.")

    valid = tuple(s.name for s in stages)
    skip_set = _stage_names(skip, "skip", valid)
    only_set = _stage_names(only, "only", valid)

    opts = opts or {}
    unknown = set(opts) - set(valid)
    if unknown:
        raise ValueError(
            f"--opt 가 없는 단계를 가리킵니다: {', '.join(sorted(unknown))}. "
            f"쓸 수 있는 단계: {', '.join(valid)}"
        )

    results: list[tuple[str, StageResult]] = []
    total_stages = len(stages)
    gsc_aborted = False

    print(f"\n{SEPARATOR}")
    print(f"수집 시작: {project}" + (" (실행 없이 계획만 봅니다)" if dry_run else ""))
    print(f"{SEPARATOR}")

    for idx, stage in enumerate(stages, start=1):
        # gsc 가 실패했을 때 나머지 단계는 실행하지 않고 중단 상태로 기록
        if gsc_aborted:
            results.append((stage.name, StageResult(ok=True, skipped=True, reason=ABORT_REASON)))
            continue

        if on_stage:
            try:
                on_stage(idx, total_stages, stage.name)
            except Exception:
                pass

        print(f"\n[{idx}/{total_stages}] {stage.name} — {stage.desc}")
        print(SUB_SEPARATOR)

        r = _run_stage(stage, project, dry_run=dry_run, skip_set=skip_set,
                       only_set=only_set, opts=opts.get(stage.name) or {})
        results.append((stage.name, r))

        if not r.ok:
            print(f"\n[오류] {stage.name} 단계가 실패했습니다 ({r.reason or '원인 불명'})",
                  file=sys.stderr)
            if stage.name == "gsc":
                print(
                    "\n[중단] 구글 실적 수집이 실패해 여기서 멈춥니다. "
                    "나머지 단계가 전부 이 숫자를 재료로 쓰기 때문입니다.",
                    file=sys.stderr,
                )
                gsc_aborted = True

    return results


def chain_rc(results: list[tuple[str, StageResult]]) -> int:
    """0: 모든 단계 성공(건너뜀 포함) / 1: 하나 이상 실패."""
    return 1 if any(not r.ok for _, r in results) else 0


def chain_cost(results: list[tuple[str, StageResult]]) -> float:
    """이번 바퀴에 쓴 돈 합계. 각 단계가 StageResult.cost 로 실청구액을 올려 준다."""
    return round(sum(r.cost for _, r in results), 4)


def _label(r: StageResult, dry_run: bool) -> str:
    if not r.ok:
        return "실패"
    if r.reason == ABORT_REASON:
        return "안 돎"
    if r.skipped:
        return "건너뜀"
    return "돌 예정" if dry_run else "완료"


def print_summary(project: str, results: list[tuple[str, StageResult]], *,
                  dry_run: bool = False) -> None:
    """결과 리스트를 사람이 읽는 요약표와 다음 작업 안내로 편다."""
    aborted = any(r.reason == ABORT_REASON for _, r in results)

    print(f"\n{SEPARATOR}")
    print(f"수집 결과 ({project})")
    print(SUB_SEPARATOR)
    for name, r in results:
        detail = []
        if r.rows:
            detail.append(f"{r.rows:,}행")
        if r.cost:
            detail.append(f"${r.cost:.4f}")
        if r.reason:
            detail.append(r.reason)
        tail = f" ({' · '.join(detail)})" if detail else ""
        print(f"  {name:<12} | {_label(r, dry_run)}{tail}")
    print(SEPARATOR)

    cost = chain_cost(results)
    if cost:
        print(f"\n이번 바퀴 비용: ${cost:.4f}")

    print("\n다음 작업:")
    if aborted:
        # 여기서 끝나면 사용자는 빈손이다. 인증 없이 도는 유일한 수집으로 안내해
        # 첫 수확이라도 남긴다 — 로그인 실패가 곧 "아무것도 못 봄"이 되지 않게.
        print('  구글 로그인부터 하세요: 채팅에 "GSC 로그인해줘"')
        print(f"  로그인 없이 지금 되는 것: /capture keywords {project} "
              "(자동완성이라 인증도 키도 안 씁니다)\n")
        return

    report = next((r.artifact for name, r in results if name == "report" and r.artifact), "")
    if report:
        print(f"  보고서 파일: {report}")
    print(f"  대시보드 열기: /capture dash {project}")
    top = _top_opportunity(project)
    if top:
        # 리포트 경로만 주고 끝내면 "그래서 뭘 고치나"로 안 이어진다 — 이 도구의
        # 유일한 실제 성과는 고치기(create)이고, 그 문턱을 여기서 한 줄로 낮춘다.
        print(f"  손댈 것 1순위: [{top['kind']}] {top['target']}")
        print(f"  고치러 가기: /create plan {project}")
    # 2회차 수집부터 Δ가 나온다. 그 2회차를 부르는 것이 아무데도 없었다.
    print(f"  다음 바퀴: 1~2주 뒤 같은 명령을 한 번 더 — /capture run {project}")
    print("  그때 가서 재는 이유: 구글이 최근 3일 수치를 나중에 채우고, "
          "순위도 주 단위로 움직입니다\n")


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


# 단계 이름 -> 그 수집기 모듈. --opt STAGE.KEY 의 STAGE 가 어느 모듈의 _parser() 를
# 봐야 하는지 여기서 정한다. gaps/report 는 자체 모듈이 아니라 이 파일의 함수라 없다 —
# 애초에 --opt 를 받지 않는다(외부 호출 0건, 노브도 없다).
STAGE_MODULES = {
    "gsc": collect_gsc, "ga4": collect_ga4, "index": collect_index, "keywords": expand_keywords,
    "metrics": collect_metrics, "rank": collect_serp, "crawl": collect_crawl,
    "ai": collect_ai, "competitors": collect_gap, "backlinks": collect_backlinks,
    "pages": collect_page,
}


def _selfcheck() -> None:
    """--opt STAGE.KEY=VALUE 통로가 실제로 도는지 두 겹으로 못 박는다.

    이 통로는 수집기가 이미 CLI 로 노출한 노브(--limit --depth --device ...)를
    체인으로도 쓰게 하려는 것이다. 그런데 CLI 플래그 이름과 collect() 의 키워드
    인자 이름이 어긋나면 --opt rank.depth=20 같은 흔한 사용이 TypeError 로
    죽는다(collect_index 의 --limit vs 예전 index_urls 가 그 사례). 이 자체점검은
    그 어긋남을 회귀로 못 박는다.

    1. 계약 검사 — 각 단계가 등록한 설정의 dest 가 collect() 의 '명시' 인자에
       있는지 inspect.signature 로 본다. **opts 로 삼키는 것은 안 쳐준다 —
       조용히 삼켜서 값이 무시되는 것이 TypeError 보다 나쁘다(사용자는 옵션을
       줬다고 믿는다).
    2. 실측 — 12단계 전부에 그 설정의 fallback 값을 --opt 로 흘려 dry_run 호출.
       dry_run 이면 대부분 DB 도 안 건드리고 바로 반환하므로 가짜 project 로도
       된다. ProjectNotFound 는 "인자는 받아들여졌다"는 뜻이라 통과로 친다 —
       TypeError 만 이 검사가 잡으려는 것이다.
    """
    import inspect
    import os
    import tempfile

    for stage in STAGES:
        mod = STAGE_MODULES.get(stage.name)
        if mod is None:
            continue
        specs = getattr(mod._parser(), "_collector_settings", [])
        params = inspect.signature(stage.fn).parameters
        for spec in specs:
            ok = (spec.dest in params
                 and params[spec.dest].kind != inspect.Parameter.VAR_KEYWORD)
            assert ok, (
                f"[{stage.name}] --opt {stage.name}.{spec.dest}=... 가 "
                f"{mod.__name__}.collect() 의 명시 인자가 아니다 (**opts 로 "
                "삼키는 것도 불허) — collect() 의 키워드 인자 이름을 CLI 플래그"
                f"(--{spec.dest.replace('_', '-')})와 맞춰라.")

    os.environ["CAPTURE_HOME"] = str(Path(tempfile.mkdtemp(prefix="seo-miner-runall-selftest-")))
    for stage in STAGES:
        mod = STAGE_MODULES.get(stage.name)
        specs = getattr(mod._parser(), "_collector_settings", []) if mod else []
        opts = {spec.dest: spec.fallback for spec in specs}
        try:
            stage.fn(project="__selfcheck__", dry_run=True, **opts)
        except db.ProjectNotFound:
            pass    # 인자는 받아들여졌다 — 여기서 잡으려는 건 TypeError 뿐
        except TypeError as e:
            raise AssertionError(f"[{stage.name}] --opt 통로가 TypeError 로 죽는다: {e}") from e

    print("run_all self-check ok")


def main() -> None:
    if len(sys.argv) == 1:
        _selfcheck()
        return
    ap = argparse.ArgumentParser(
        description="전체 수집 체인 실행 — "
                    "gsc → ga4 → index → keywords → rank → ai → competitors → gaps → pages → report"
    )
    ap.add_argument("--project", required=True, help="프로젝트 이름")
    ap.add_argument("--dry-run", action="store_true", help="실제 실행 없이 호출 계획 및 비용만 확인")
    ap.add_argument("--skip", help="건너뛸 단계 (쉼표 구분, 예: index,ai)")
    ap.add_argument("--only", help="실행할 단계 (쉼표 구분, 예: gsc,gaps)")
    ap.add_argument("--opt", action="append", metavar="STAGE.KEY=VALUE",
                    help="스테이지별 옵션 (반복 지정). 예: --only rank --opt rank.device=mobile")
    args = ap.parse_args()

    try:
        results = run_chain(
            args.project,
            dry_run=args.dry_run,
            skip=args.skip,
            only=args.only,
            opts=parse_opts(args.opt),
        )
    except ValueError as e:
        print(f"[오류] {e}", file=sys.stderr)
        sys.exit(1)

    print_summary(args.project, results, dry_run=args.dry_run)
    sys.exit(chain_rc(results))


if __name__ == "__main__":
    main()
