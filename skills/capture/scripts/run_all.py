#!/usr/bin/env python3
"""전체 수집 체인 러너 (run_all.py) — /capture run 한 줄 실행.

수집기와 스코어링·리포트를 정해진 순서로 호출하고, 각 단계가 돌려준
StageResult 를 그대로 호출자에게 넘긴다.

실행 순서의 정본은 아래 STAGES 표다 — 이 산문은 **왜 그 자리인지**만 적는다.
번호를 여기 다시 매기면 표가 늘 때마다 한쪽만 낡는다(실제로 그렇게 낡아서,
단계가 열넷인데 이 목록은 열이라고 말하고 있었다).

  gsc         : 다른 모든 판정의 기본 재료. 실패 시 뒤가 빈손이므로 체인 중단.
  ga4         : 클릭 뒤(세션·전환·이탈) — GSC 의 page 축과 잇는다. 속성 미연결이면 건너뜀.
  index       : 색인은 순위 이전의 문제. GSC 최신 스냅샷 상위 페이지 대상.
  keywords    : 자동완성 키워드 발굴 (expand_keywords.py --mode all).
  metrics     : 검색량·난이도. 발굴 뒤·기회 적재 앞이어야 한다 — 점수가 볼륨을 쓴다.
  rank        : 유료 SERP 순위 스냅샷. 키 없으면 건너뜀.
  crawl       : 사이트 전수 크롤. 한 장만 봐서는 모르는 것(중복·고아·사슬)의 유일한 출처.
  ai          : 유료 AI 인용 체크. 키 없으면 건너뜀.
  competitors : 유료 DataForSEO Labs 역키워드. 키 없으면 건너뜀.
  backlinks   : 유료 백링크 프로필·링크 교집합. 키 없으면 건너뜀.
  gaps        : scoring.py load <project> (수집 결과를 읽어 기회 데이터 적재).
  pages       : 내 페이지 HTML 감사 (기회에 걸린 URL 부터, 비용 0).
  vitals      : 그 페이지들의 속도(LCP·INP·CLS)를 기기별로. pages 와 같은 URL 목록을
                본다 — 어느 페이지를 손댈지가 먼저 정해져야 같은 페이지를 잰다. 무료.
  report      : dashboard.py --export --project <project> (리포트 HTML 박제).

설계 원칙:
  - 표(STAGES)에는 디스패치가 한 종류뿐이다. 모든 단계가
    fn(project, *, dry_run, **opts) -> StageResult 다. scoring.load / dashboard.export 는
    직접 호출이다 — 나머지 열 단계와 마찬가지로 in-process 로 돈다. 둘 다 로컬
    sqlite/파일 I/O 뿐이라(외부 네트워크·os.environ 변경 없음) 격리할 이유가 없었고,
    scoring 은 이미 collect_* 절반이 물고 와 있었다.
  - --dry-run 은 각 단계에 위임하여 비용 고지.
  - 하나의 단계가 실패해도 체인은 계속 진행 (단, 순차 체인에서는 gsc 실패 시 즉시 중단).
  - 두 가지로 돈다. **묶음 런**(groups=…, CLI 인자 없음·--groups) 은 GROUPS 의 묶음을
    동시에 돌리고 AFTER 의 순서만 지킨다 — 순서는 막는 조건이 아니라 기다리는 조건이라,
    앞 단계가 실패해도 뒤 단계는 지난 데이터로 돈다(한 묶음의 실패가 다른 묶음을 안
    멈춘다). **순차 체인**(groups 없음 — --only, 그리고 가짜 표를 주입하는 검사들)은
    STAGES 순서 그대로 하나씩 돈다.
  - run_chain 은 판정도 요약도 하지 않는다 — [(단계 이름, StageResult)] 를 돌려주고,
    종료코드(chain_rc)·총비용(chain_cost)·요약표(print_summary)는 그 결과에서 나온다.
    행 수·비용·산출물 경로가 정수 exit code 로 접혀 버려지던 자리가 이것이다.
"""
import argparse
import inspect
import sys
import threading
from collections import namedtuple
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

# db import를 통해 CAPTURE_HOME/env 자동 로딩 및 콘솔 UTF-8 설정 적용
sys.path.insert(0, str(Path(__file__).resolve().parent))
import stage               # noqa: E402
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
import collect_vitals      # noqa: E402
import collector           # noqa: E402
import dashboard           # noqa: E402
import db                  # noqa: E402
import expand_keywords     # noqa: E402
import remote              # noqa: E402
import scoring             # noqa: E402
import serp_adapter        # noqa: E402

StageResult = collector.StageResult

# 진단 로그. 사용자 내레이션은 그대로 print 다 — 이건 traceback 처럼 사용자가
# 읽을 것이 아닌 것을 버리지 않기 위한 통로다(collector.setup_logging 참조).
log = collector.LOG.getChild("run_all")

ABORT_REASON = "구글 실적 수집이 실패해 여기서 멈췄습니다"

SEPARATOR = "=" * 62
SUB_SEPARATOR = "-" * 62


def load_opportunities(project: str, *, dry_run: bool = False, **_opts) -> StageResult:
    """gaps 단계 — scoring.load. 외부 호출 0건이라 --dry-run 플래그가 없다."""
    if dry_run:
        reason = "외부 호출이 없습니다. 실제로 돌리면 기회를 뽑아 저장합니다"
        print(f"[gaps] {reason}")
        return collector.skipped(reason)
    scoring.load(project)
    return collector.succeeded()


def export_report(project: str, *, dry_run: bool = False, **_opts) -> StageResult:
    """report 단계 — dashboard.export. 산출물 경로 규칙의 정본은 dashboard.export() 고,
    여기서는 그게 돌려주는 Path 를 그대로 옮긴다(파싱 없음)."""
    if dry_run:
        reason = "외부 호출이 없습니다. 실제로 돌리면 보고서 HTML 을 내보냅니다"
        print(f"[report] {reason}")
        return collector.skipped(reason)
    out = dashboard.export(project)
    return collector.succeeded(artifact=str(out))


# 단계 정의. 표에는 디스패치가 한 종류뿐 — fn(project, *, dry_run, **opts) -> StageResult.
# 단계 순서·정의의 정본은 여기 하나다. 어떤 키가 있어야 유료 단계가 도는지는
# check_paid_keys 가 답한다 — 그 판정을 여기에도 적어 두면 두 벌이 되고, 한쪽만
# 고쳐지는 사고가 난다(이 저장소가 반복해서 겪은 것).
#
# module·knobs 는 collector.cli(원격 위임·인자 전달)와 remote.opts_of/app._stage_opts
# (원격 --opt 검증)가 함께 보는 자리다 — "이 단계가 무슨 모듈이고 어떤 노브를
# 받나"를 여기 말고 또 어딘가(STAGE_MODULES 사본, inspect.signature 재조회)에
# 적으면 둘 중 하나만 고쳐지는 사고가 난다.
Stage = namedtuple("Stage", ["name", "desc", "fn", "is_paid", "module", "knobs"],
                    defaults=(None, {}))


def _knobs(mod) -> dict:
    """mod._parser() 가 노출하는 CLI 플래그를 dest -> (type, default) 로 편다.

    collect() 의 명시 키워드 인자가 아닌 dest(conn·post·fetch 같은 테스트 주입
    전용, 또는 이름이 어긋난 것)는 걸러낸다 — --opt STAGE.KEY 도, 원격 opts_of 도
    거기까지는 안 닿게. mod 가 없으면(gaps·report — 자체 모듈이 아니라 이 파일의
    함수라 노브가 없다) 빈 dict.
    """
    if mod is None:
        return {}
    params = inspect.signature(mod.collect).parameters
    out = {}
    for act in mod._parser()._actions:
        dest = act.dest
        if dest in ("help", "project", "dry_run"):
            continue
        if dest not in params or params[dest].kind == inspect.Parameter.VAR_KEYWORD:
            continue
        out[dest] = (act.type, act.default)
    return out


def _stage(name, detail, fn, is_paid, module=None) -> Stage:
    # 단계 이름은 stage.STAGE_LABELS 한 벌이다 — 여기는 괄호 안 세부만 갖는다.
    t = stage.STAGE_LABELS[name]["t"]
    return Stage(name, f"{t} ({detail})" if detail else t, fn, is_paid, module, _knobs(module))


STAGES = (
    _stage("gsc",         "합계·일별·기기별",       collect_gsc.collect,     False, collect_gsc),
    _stage("ga4",         "세션·전환·이탈, 페이지별", collect_ga4.collect,    False, collect_ga4),
    _stage("index",       "",                          collect_index.collect,   False, collect_index),
    _stage("keywords",    "자동완성",                expand_keywords.collect, False, expand_keywords),
    # 볼륨은 발굴 뒤·기회 적재 앞이어야 한다 — 점수가 볼륨을 재료로 쓴다.
    _stage("metrics",     "검색량·난이도·CPC",    collect_metrics.collect, True, collect_metrics),
    _stage("rank",        "",                               collect_serp.collect,    True, collect_serp),
    _stage("crawl",       "깨진 링크·리다이렉트·고아", collect_crawl.collect,   False, collect_crawl),
    _stage("ai",          "",                            collect_ai.collect,      True, collect_ai),
    _stage("competitors", "경쟁사 검색어·트래픽 몫",          collect_gap.collect,     True, collect_gap),
    _stage("backlinks",   "프로필·앵커·링크 교집합",     collect_backlinks.collect, True, collect_backlinks),
    _stage("gaps",        "외부 호출 없음",           load_opportunities,      False),
    _stage("pages",       "제목·설명·본문·구조화 데이터", collect_page.collect, False, collect_page),
    # 속도는 페이지 점검 뒤다 — 같은 URL 목록(collect_page.target_urls)을 보기 때문에,
    # 어느 페이지를 손댈지가 먼저 정해져 있어야 같은 페이지를 잰다.
    _stage("vitals",      "LCP·INP·CLS, 모바일·데스크톱", collect_vitals.collect, False, collect_vitals),
    _stage("report",      "HTML",                    export_report,           False),
)

VALID_STAGE_NAMES = tuple(s.name for s in STAGES)
STAGE_BY_NAME = {s.name: s for s in STAGES}


# ── 묶음 — 메뉴 = 사용자의 질문 = 다시 재는 단위 ─────────────────────────────
#
# 묶음의 정본은 여기 한 곳이다(id·이름·단계·주기·화면). 서버의 주기 시계(store)·
# 대기열(app)·화면의 메뉴 제목과 [이 묶음 다시 재기] 버튼(dashboard 페이로드 d.groups)
# 이 전부 이 표를 읽는다 — 뷰의 view-def 는 "group": "<id>" 만 적는다. 이름이나 단계를
# 화면에 다시 적으면 두 벌이 되고, 이 리포는 그렇게 "9단계인데 8단계라고 말하는" 사고를
# 여러 번 냈다.
#
#   stages       이 묶음을 다시 재면 도는 단계. 꼬리(TAIL)는 적지 않는다 — 모든 재기에
#                자동으로 붙는다. 한 단계가 두 묶음에 있어도 된다(rank: 검색 성과와 AI
#                노출이 같이 쓴다) — 한 런 안에서는 한 번만 돈다(plan 이 합집합을 낸다).
#   every_hours  자동 주기(시간). None 이면 시계가 없는 묶음(관리). WEEKLY_HOURS 인 묶음은
#                사이트 설정 run_every_hours(주간 묶음 주기)를 따른다 — store 가 그렇게
#                읽는다. 0 이면 자동 재기 전부 끔.
#   views        이 묶음에 드는 화면(view-def 의 id), 메뉴 순서 그대로.
#
# todo(할 일)는 버튼이 없다 — stages 를 비워 둔다(화면은 stages 가 있는 묶음에만 버튼을
# 단다). 대신 그 묶음의 시계는 **매일 런**(DAILY + TAIL)이 찍는다: 심사·개요가 읽는 건
# 검색 실적과 기회 목록이고, 그 둘이 매일 도는 것이다. group_stages() 가 그 연결이다.
WEEKLY_HOURS = 168
TAIL = ("gaps", "pages", "report")
DAILY = ("gsc", "ga4")

GROUPS = (
    {"id": "todo", "name": "할 일", "stages": (), "every_hours": 24,
     "views": ("triage", "overview")},
    {"id": "search", "name": "검색 성과",
     "stages": ("gsc", "ga4", "keywords", "metrics", "rank"), "every_hours": WEEKLY_HOURS,
     "views": ("analysis", "keywords", "rank")},
    {"id": "ai", "name": "AI 노출", "stages": ("ai", "rank"), "every_hours": WEEKLY_HOURS,
     "views": ("ai",)},
    {"id": "site", "name": "사이트 건강", "stages": ("index", "crawl", "vitals"),
     "every_hours": WEEKLY_HOURS, "views": ("site",)},
    {"id": "compete", "name": "경쟁·링크", "stages": ("competitors", "backlinks"),
     "every_hours": 720, "views": ("competitors", "backlinks")},
    {"id": "admin", "name": "관리", "stages": (), "every_hours": None,
     "views": ("history", "guide", "settings")},
)
GROUP_BY_ID = {g["id"]: g for g in GROUPS}
# 시계가 있는 묶음 = 잴 수 있는 묶음. 전체 재기 = 이것 전부.
RUNNABLE_GROUPS = tuple(g["id"] for g in GROUPS if g["every_hours"] is not None)

# 순서 — "X 는 (이 런에 있으면) 이것들이 끝난 뒤에". 막는 조건이 아니다: 앞 단계가 실패하거나
# 이번 런에 없으면 뒤 단계는 지난 데이터로 돈다. 근거는 코드에서 확인한 읽기 관계다.
#   index       최신 GSC 스냅샷의 상위 페이지를 검사한다 (collect_index → top_pages)
#   keywords    --mode all 이 GSC 쿼리를 후보로 읽는다 (expand_keywords)
#   metrics     발굴한 키워드의 검색량 — 발굴 뒤
#   rank        추적 키워드(발굴·지표 뒤)
#   competitors 경쟁사 목록이 rank 의 수확(harvest_comp → competitors.auto_rank)이다 —
#               scoring.rivals 로 읽는다. 이미 있는 키워드·GSC 쿼리는 걸러 낸다
#   backlinks   링크 교집합의 경쟁사가 competitors 의 자동 탐지(auto_labs)까지다
#   pages       기회에 걸린 페이지부터 본다 (collect_page.target_urls → opportunities)
#   vitals      pages 와 같은 URL 목록 — 어느 페이지를 손댈지가 먼저 정해져야 같은 페이지를 잰다
# gaps 와 report 는 표 대신 규칙이다(_waits): gaps 는 꼬리·vitals 를 뺀 전부 뒤(scoring 은
# page_vitals 를 안 읽는다), report 는 나머지 전부 뒤.
AFTER = {
    "index": ("gsc",),
    "keywords": ("gsc",),
    "metrics": ("keywords",),
    "rank": ("keywords", "metrics"),
    "competitors": ("gsc", "keywords", "rank"),
    "backlinks": ("competitors",),
    "pages": ("gaps",),
    "vitals": ("pages",),
}


def group_stages(gid: str) -> tuple:
    """이 묶음을 재면 도는 단계(꼬리 제외). todo 는 매일 런이다 — 위 주석."""
    return DAILY if gid == "todo" else GROUP_BY_ID[gid]["stages"]


def group_names(raw) -> list[str]:
    """쉼표 문자열·목록 → 묶음 id 목록(GROUPS 순서). 'all' 이나 빈 값이면 전체 재기.
    모르는 id 면 ValueError — 조용히 버리면 누른 묶음이 안 돈 채로 '완료'가 된다."""
    names = [x.strip() for x in (raw.split(",") if isinstance(raw, str) else (raw or []))
             if str(x).strip()]
    if not names or names == ["all"]:
        return list(RUNNABLE_GROUPS)
    bad = [x for x in names if x not in RUNNABLE_GROUPS]
    if bad:
        raise ValueError(f"없는 묶음입니다: {', '.join(bad)}. "
                         f"쓸 수 있는 묶음: {', '.join(RUNNABLE_GROUPS)}")
    return [g for g in RUNNABLE_GROUPS if g in names]


def plan(groups) -> tuple:
    """묶음들 → 이번 런에 돌 단계(STAGES 순서, 겹치는 단계는 한 번). 꼬리는 늘 붙는다."""
    want = set(TAIL)
    for gid in group_names(groups):
        want |= set(group_stages(gid))
    return tuple(n for n in VALID_STAGE_NAMES if n in want)


def covered(stages) -> list[str]:
    """이 단계들을 돌았으면 시계를 찍어도 되는 묶음들. 서버가 런 뒤 묶음 시계를 찍을 때
    쓴다 — 검색 성과 런은 gsc·ga4·꼬리를 다 돌았으니 매일 런(todo)도 한 셈이다."""
    have = set(stages)
    return [g for g in RUNNABLE_GROUPS
            if set(group_stages(g)) <= have and set(TAIL) <= have]


def _waits(names) -> dict[str, set]:
    """이번 런의 단계별 '먼저 끝나야 하는 것' — 이번 런에 없는 단계는 기다리지 않는다."""
    here = set(names)
    out = {n: {d for d in AFTER.get(n, ()) if d in here} for n in names}
    body = here - set(TAIL) - {"vitals"}
    if "gaps" in here:
        out["gaps"] |= body
    if "report" in here:
        out["report"] |= here - {"report"}
    return out


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


def _coerce(v: str, type_fn=None):
    """--opt 값의 타입 변환. type_fn 이 있으면 그걸 신뢰한다(stage.knobs 가 정본 —
    파서가 --depth 를 int 로 등록했으면 그대로 int() 다. bool("false")==True 인
    파이썬 함정이 있어 bool 은 따로 다룬다). 모르는 단계·키(방금 추가돼 아직 표에
    없는 것 등)라 type_fn 이 없으면 예전처럼 이름으로 추정한다."""
    s = v.strip()
    if type_fn is bool:
        return s.lower() == "true"
    if type_fn is not None:
        return type_fn(s)
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
    --breakdown --row-limit --mode)가 체인으로도 도달하게 하는 통로다. 값의 타입은
    STAGE_BY_NAME[stage].knobs 가 안다 — 없으면(단계 이름 자체가 틀린 경우 등)
    이름으로 추정한다.
    """
    out: dict[str, dict] = {}
    for item in items or []:
        target, sep, value = (item or "").partition("=")
        if not sep:
            raise ValueError(f"--opt 형식이 아닙니다 (STAGE.KEY=VALUE): {item}")
        stage_name, dot, key = target.partition(".")
        if not dot or not key.strip() or not stage_name.strip():
            raise ValueError(f"--opt 형식이 아닙니다 (STAGE.KEY=VALUE): {item}")
        bag = out.setdefault(stage_name.strip(), {})
        k = key.strip().replace("-", "_")
        stg = STAGE_BY_NAME.get(stage_name.strip())
        type_fn = stg.knobs.get(k, (None, None))[0] if stg else None
        coerced = _coerce(value, type_fn)
        # 같은 키가 두 번 이상 오면 리스트로 모은다 — argparse 의 append 인자
        # (competitors.domain)가 통로를 지나 리스트로 도착해야 한다. 덮어쓰면
        # 도메인 세 개를 준 사용자가 마지막 하나만 잰 결과를 받는다.
        if k in bag:
            cur = bag[k]
            bag[k] = (cur if isinstance(cur, list) else [cur]) + [coerced]
        else:
            bag[k] = coerced
    return out


# 카나리아 — 돈 쓰기 전 무료 점검 한 번. 이게 없어서 잔액 0 인 계정이 키워드마다
# 402 를 맞고 "완료"로 끝났다 (8/30·9/1·9/2 자동 런).
MIN_BALANCE = 1.0                       # 이보다 적으면 유료 단계는 어차피 실패한다
DFS_STAGES = ("metrics", "rank", "competitors", "backlinks")


def preflight(stages=STAGES) -> dict[str, str]:
    """유료 단계를 돌리기 전 한 번. 반환: {건너뛸 단계: 사유}.

    두 호출(DataForSEO user_data, OpenRouter auth/key)이 다 무료라 dry-run 에서도
    한다. 확인 자체가 실패하면(네트워크 등) 아무것도 막지 않는다 — 카나리아가
    수집을 죽이면 안 된다.
    """
    blocked: dict[str, str] = {}
    paid = {s.name for s in stages if s.is_paid}
    # rank 는 serper 로도 돈다 — 지금 쓸 제공자가 DataForSEO 일 때만 잔액이 문제다.
    dfs = [n for n in DFS_STAGES
           if n in paid and (n != "rank" or serp_adapter.detect_provider() == "dataforseo")]
    if dfs and serp_adapter.has_dataforseo():
        try:
            bal = serp_adapter.dataforseo_balance()
        except Exception as e:
            print(f"[사전점검] DataForSEO 잔액을 못 읽었습니다 ({e}) — 그대로 진행합니다.")
            bal = None
        if bal is not None and bal < MIN_BALANCE:
            print(f"[경고] DataForSEO 잔액 ${bal:.2f} — {'·'.join(dfs)} 가 실패합니다. "
                  f"충전: https://app.dataforseo.com")
            for n in dfs:
                blocked[n] = f"DataForSEO 잔액 없음 (${bal:.2f})"
    if "ai" in paid and serp_adapter.has_openrouter():
        ok, msg = collect_ai.openrouter_ok()
        if not ok:
            print(f"[경고] {msg} — ai 단계를 건너뜁니다.")
            blocked["ai"] = msg
    return blocked


def _run_stage(stage, project: str, *, dry_run: bool, skip_set: set[str],
               only_set: set[str], opts: dict, blocked: dict | None = None) -> StageResult:
    """단계 하나 — 건너뛸 이유를 먼저 보고, 아니면 fn 을 부른다."""
    blocked = blocked or {}

    def _skip(reason: str) -> StageResult:
        print(f"[{stage.name}] {reason}")
        return collector.skipped(reason)

    if stage.name in skip_set:
        return _skip("--skip 으로 지정해 건너뜁니다")

    if only_set and stage.name not in only_set:
        return _skip("--only 대상이 아니라 건너뜁니다")

    if stage.is_paid:
        has_keys, paid_skip_msg = check_paid_keys(stage.name)
        if not has_keys:
            return _skip(paid_skip_msg)
        if stage.name in blocked:
            # 카나리아가 이미 답을 알고 있다 — 402 를 100번 사러 가지 않는다.
            return _skip(blocked[stage.name])

    try:
        return stage.fn(project=project, dry_run=dry_run, **opts)
    except collector.Fatal as e:
        # 잔액·인증 — 사람 말 그대로 사유로 싣는다("예외 발생 (...)" 로 감싸지 않는다)
        log.warning("[%s] Fatal: %s", stage.name, e)
        return collector.failed(str(e))
    except Exception as e:
        # 사용자에게 가는 것은 아래 한 문장이지만, traceback 을 그냥 버리면 원인이
        # 어디에도 안 남는다 — 여기가 이 저장소에서 진단이 사라지던 자리다.
        log.exception("[%s] 단계에서 처리하지 못한 예외", stage.name)
        return collector.failed(f"이 단계에서 오류가 났습니다 ({e})")


RAN_REASON = "바로 앞 런에서 이미 돌아 건너뜁니다"

# 동시 실행의 출력 — 지금 이 스레드가 도는 단계 이름. 줄 머리표(_Lines)가 읽는다.
_CURRENT = threading.local()


class _Lines:
    """여러 단계가 한 stdout 에 동시에 쓰는 동안 **줄 단위로** 내보낸다.

    print("a", "b") 는 write 를 여러 번 부른다 — 두 스레드가 같이 쓰면 한 줄 안에서
    글자가 섞인다. 스레드마다 줄 끝까지 모았다가 락 아래서 한 번에 내보낸다. 받는 쪽
    (워커의 _Tee — 런 로그를 서버 DB 에 흘리는 버퍼)도 그래서 한 번에 한 스레드만 받는다.

    줄 앞에 [단계] 를 붙인다 — 동시에 도는 단계의 줄이 섞이면 "  12  신발 추천"이 어느
    단계의 말인지 모른다. 이미 [..] 로 시작하는 줄(수집기가 자기 머리표를 단 것)은 둔다.
    """

    def __init__(self, raw, lock):
        self.raw, self._lock, self._tl = raw, lock, threading.local()

    def write(self, s):
        buf = getattr(self._tl, "buf", "") + s
        *lines, rest = buf.split("\n")
        self._tl.buf = rest
        if lines:
            self._emit("".join(self._tag(ln) + "\n" for ln in lines))
        return len(s)

    def _tag(self, ln: str) -> str:
        name = getattr(_CURRENT, "name", None)
        return f"[{name}] {ln}" if name and ln.strip() and not ln.lstrip().startswith("[") else ln

    def _emit(self, text: str) -> None:
        with self._lock:
            self.raw.write(text)

    def drain(self) -> None:
        """이 스레드가 줄바꿈 없이 남긴 꼬리를 내보낸다(단계가 끝날 때)."""
        rest = getattr(self._tl, "buf", "")
        if rest:
            self._tl.buf = ""
            self._emit(self._tag(rest) + "\n")

    def flush(self):
        with self._lock:
            try:
                self.raw.flush()
            except Exception:
                pass

    def __getattr__(self, a):
        return getattr(self.raw, a)


def _wal() -> None:
    """이 Brain 을 WAL 로 — 동시 단계가 읽는 동안 다른 단계가 쓸 수 있게.

    기본(rollback journal)은 쓰는 동안 읽기까지 막는다. WAL 은 파일에 남는 설정이라 한 번
    켜면 된다. 켜지 못해도 막지 않는다 — 그러면 collector.stage 의 busy_timeout 이 기다려
    준다(느릴 뿐 틀리지 않는다). 서버가 Brain 을 내려보낼 때는 backup() 을 쓰므로 -wal
    파일에만 있는 행도 빠지지 않는다(app.api_brain).
    """
    try:
        c = db.connect()
        try:
            c.execute("PRAGMA journal_mode=WAL")
        finally:
            c.close()
    except Exception as e:
        log.warning("WAL 전환 실패: %s", e)


def _run_groups(project: str, table: tuple, *, dry_run: bool, skip_set: set, opts: dict,
                blocked: dict, ran: set, on_stage) -> list[tuple[str, StageResult]]:
    """묶음 런 — 준비된 단계부터 동시에 띄우고, 끝나는 대로 다음을 띄운다.

    스레드다(프로세스가 아니다): 단계가 하는 일은 거의 네트워크 대기라 GIL 이 문제가
    안 되고, 워커가 tenant() 로 갈아 끼운 env·유료 키를 그대로 봐야 한다(자식 프로세스로
    넘기면 그 격리를 다시 짜야 한다). DB 는 단계마다 **자기 연결**을 연다(collector.stage)
    — 연결을 스레드끼리 나누지 않는다. 쓰기는 SQLite 가 한 번에 하나로 줄 세우고(WAL +
    busy_timeout), 수집기는 항목마다 commit 하므로 한 단계가 오래 쥐고 있지 않는다.

    진행률 보고(on_stage)는 이 조정 스레드에서만 부른다 — 받는 쪽(워커)이 서버 DB 연결
    하나로 쓰는데, 그걸 단계 스레드들이 동시에 부르면 한 연결을 여러 스레드가 겹쳐 쓴다.
    """
    names = [s.name for s in table]
    by_name = {s.name: s for s in table}
    waits = _waits(names)
    done: dict[str, StageResult] = {}
    running: dict = {}
    pending = list(names)
    total = len(names)
    lock = threading.Lock()
    out, err = _Lines(sys.stdout, lock), _Lines(sys.stderr, lock)

    def report():
        if on_stage:
            try:
                on_stage(len(done) + 1, total, ",".join(n for n in names if n in running.values()))
            except Exception:
                pass

    def one(stg) -> StageResult:
        _CURRENT.name = stg.name
        try:
            print(f"\n{stg.name} 시작 — {stg.desc}")
            if stg.name in ran:
                print(f"[{stg.name}] {RAN_REASON}")
                return collector.skipped(RAN_REASON)
            return _run_stage(stg, project, dry_run=dry_run, skip_set=skip_set, only_set=set(),
                              opts=opts.get(stg.name) or {}, blocked=blocked)
        finally:
            out.drain()
            err.drain()
            _CURRENT.name = None

    saved = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = out, err
    try:
        with ThreadPoolExecutor(max_workers=max(1, total), thread_name_prefix="stage") as ex:
            while pending or running:
                for n in [n for n in pending if waits[n] <= done.keys()]:
                    pending.remove(n)
                    running[ex.submit(one, by_name[n])] = n
                report()
                finished, _ = wait(list(running), return_when=FIRST_COMPLETED)
                for f in finished:
                    n = running.pop(f)
                    try:
                        done[n] = f.result()
                    except BaseException as e:     # _run_stage 가 삼키지 못한 것(KeyboardInterrupt 등)
                        log.exception("[%s] 동시 실행 중 예외", n)
                        done[n] = collector.failed(f"이 단계에서 오류가 났습니다 ({e})")
                    if done[n].failed:
                        print(f"\n[오류] {n} 단계가 실패했습니다 ({done[n].reason or '원인 불명'})"
                              " — 다른 묶음은 계속 돕니다", file=sys.stderr)
        report()
    finally:
        out.drain()
        err.drain()
        sys.stdout, sys.stderr = saved
    return [(n, done[n]) for n in names]


def run_chain(
    project: str,
    *,
    dry_run: bool = False,
    skip: str | None = None,
    only: str | None = None,
    opts: dict[str, dict] | None = None,
    stages: tuple = STAGES,
    on_stage=None,
    preflight=preflight,
    groups=None,
    ran=(),
) -> list[tuple[str, StageResult]]:
    """수집 체인을 실행하고 각 단계의 결과를 그대로 돌려줍니다.

    Args:
        project: 프로젝트 이름
        dry_run: 실제 실행 대신 호출 계획 및 비용 확인 모드 여부
        skip: 건너뛸 단계 이름 (쉼표 구분 문자열 하나)
        only: 실행할 단계 이름 (쉼표 구분 문자열 하나) — 순차 체인 전용
        opts: {단계 이름: {kwarg: 값}} — 그 단계의 collect() 에 그대로 넘어간다
        stages: 실행할 단계 표. 테스트가 가짜 표를 주입하는 자리다.
        preflight: fn(stages) -> {건너뛸 단계: 사유}. 유료 단계 전에 한 번 불린다
            (무료 호출). 테스트가 네트워크를 막는 자리다.
        on_stage: fn(idx, total, 단계 이름) — 진행률 보고용이라 여기서 난 예외는 삼킨다
            (보고가 수집을 죽이면 안 된다). 순차 체인은 단계 시작마다, 묶음 런은 단계가
            뜨고 끝날 때마다 부르고, 이름 자리에 **지금 도는 단계들**을 쉼표로 싣는다
            (idx-1 = 끝난 단계 수 — 진행률은 끝난 만큼만 센다).
        groups: 묶음 id 들(쉼표 문자열·목록, 'all'·빈 목록은 전체) — 주면 묶음 런이다
            (plan + 동시 실행). None 이면 순차 체인.
        ran: 묶음 런 전용 — 바로 앞 런에서 이미 돈 단계. 대기열의 다음 런이 공유 단계
            (rank 등)를 또 사지 않게 서버가 준다. 건너뜀으로 남는다.

    Returns:
        [(단계 이름, StageResult), ...] — 단계당 한 건, STAGES 순서.
        순차 체인은 표의 모든 단계(안 도는 것은 건너뜀으로), 묶음 런은 이번 런에
        든 단계만 싣는다 — 안 누른 묶음의 단계 열 줄이 "건너뜀"으로 요약표를 덮지 않게.
        종료코드는 chain_rc, 총비용은 chain_cost, 요약표는 print_summary 가 낸다.

    Raises:
        ValueError: skip/only 동시 지정, groups/only 동시 지정, 잘못된 단계·묶음 이름,
            잘못된 --opt 대상.
    """
    if skip and only:
        raise ValueError("--skip 과 --only 옵션은 동시에 사용할 수 없습니다.")
    if groups is not None and only:
        raise ValueError("--groups 와 --only 옵션은 동시에 사용할 수 없습니다.")

    # 체인이 시작하는 자리는 여기 하나다(로컬 CLI 도, 워커도) — 진단 로그를 켜는
    # 자리도 하나여야 한다.
    collector.setup_logging()

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

    if groups is not None:
        gids = group_names(groups)
        want = set(plan(gids))
        table = tuple(s for s in stages if s.name in want)
        ran = set(ran or ())
        print(f"\n{SEPARATOR}")
        print(f"수집 시작: {project} — "
              + " · ".join(GROUP_BY_ID[g]["name"] for g in gids)
              + (" (실행 없이 계획만 봅니다)" if dry_run else ""))
        print(f"{SEPARATOR}")
        to_run = [s for s in table if s.name not in skip_set and s.name not in ran]
        blocked = preflight(to_run) if any(s.is_paid for s in to_run) else {}
        _wal()
        return _run_groups(project, table, dry_run=dry_run, skip_set=skip_set, opts=opts,
                           blocked=blocked, ran=ran, on_stage=on_stage)

    print(f"\n{SEPARATOR}")
    print(f"수집 시작: {project}" + (" (실행 없이 계획만 봅니다)" if dry_run else ""))
    print(f"{SEPARATOR}")

    # 돈 쓰는 단계가 하나라도 남아 있으면 그 앞에서 무료로 한 번 묻는다.
    to_run = [s for s in stages
              if s.name not in skip_set and (not only_set or s.name in only_set)]
    blocked = preflight(to_run) if any(s.is_paid for s in to_run) else {}

    for idx, stage in enumerate(stages, start=1):
        # gsc 가 실패했을 때 나머지 단계는 실행하지 않고 중단 상태로 기록
        if gsc_aborted:
            results.append((stage.name, collector.skipped(ABORT_REASON)))
            continue

        if on_stage:
            try:
                on_stage(idx, total_stages, stage.name)
            except Exception:
                pass

        print(f"\n[{idx}/{total_stages}] {stage.name} — {stage.desc}")
        print(SUB_SEPARATOR)

        r = _run_stage(stage, project, dry_run=dry_run, skip_set=skip_set,
                       only_set=only_set, opts=opts.get(stage.name) or {}, blocked=blocked)
        results.append((stage.name, r))

        if r.failed:
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
    """0: 모든 단계 성공(건너뜀 포함) / 1: 하나 이상 실패.

    산문이 "건너뜀 포함"이라고 말하는 동안 구현은 `not r.ok` 만 봤다 — 그리고
    Stage.skip() 이 ok=False 를 냈으므로, GA4 미연결·활성 키워드 없음 같은 사이트는
    매 런 rc=1 로 끝나 실패 메일을 받았다. 실패인지는 StageResult.failed 가 답한다.
    """
    return 1 if any(r.failed for _, r in results) else 0


def chain_cost(results: list[tuple[str, StageResult]]) -> float:
    """이번 바퀴에 쓴 돈 합계. 각 단계가 StageResult.cost 로 실청구액을 올려 준다."""
    return round(sum(r.cost for _, r in results), 4)


def _label(r: StageResult, dry_run: bool) -> str:
    # 건너뜀을 "실패"로 찍지 않는다 — 실패인지 묻는 자리는 failed 하나다.
    if r.failed:
        return "실패"
    if r.reason == ABORT_REASON:
        return "안 돎"
    if r.skipped:
        return "건너뜀"
    if r.partial:
        # 초록불 아래로 나가지 않게 — 사유(N건 실패: 첫 오류)는 아래 detail 이 붙인다
        return "완료 ⚠"
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


def _selfcheck() -> None:
    """--opt STAGE.KEY=VALUE 통로(및 collector.cli 가 쓰는 같은 통로)가 실제로
    도는지 못 박는다.

    이 통로는 수집기가 이미 CLI 로 노출한 노브(--limit --depth --device ...)를
    체인으로도, collector.cli 로도 쓰게 하려는 것이다. 그런데 CLI 플래그 이름과
    collect() 의 키워드 인자 이름이 어긋나면 --opt rank.depth=20 같은 흔한 사용이
    TypeError 로 죽는다(collect_index 의 --limit vs 예전 index_urls 가 그 사례).

    각 단계의 파서를 실제로 파싱해(기본값만, --project·--dry-run 만 채워서) 그
    결과를 collect() 에 그대로 흘린다 — collector.cli 가 하는 것과 같은 일이다.
    ProjectNotFound 는 "인자는 받아들여졌다"는 뜻이라 통과로 친다 — TypeError 만
    이 검사가 잡으려는 것이다.
    """
    import os
    import tempfile

    os.environ["CAPTURE_HOME"] = str(Path(tempfile.mkdtemp(prefix="seo-miner-runall-selftest-")))
    for stg in STAGES:
        if stg.module is None:
            continue
        args = stg.module._parser().parse_args(["--project", "__selfcheck__", "--dry-run"])
        kwargs = {k: v for k, v in vars(args).items() if k not in ("project", "dry_run")}
        try:
            stg.module.collect(project=args.project, dry_run=True, **kwargs)
        except db.ProjectNotFound:
            pass    # 인자는 받아들여졌다 — 여기서 잡으려는 건 TypeError 뿐
        except TypeError as e:
            raise AssertionError(f"[{stg.name}] 파서 기본값으로 collect() 를 못 부른다: {e}") from e

    _check_groups()
    print("run_all self-check ok")


def _check_groups() -> None:
    """묶음 런을 가짜 단계로 실제로 돌린다 — 표의 모양·순서·동시성·실패 격리·공유 단계
    한 번·Brain 동시 쓰기. 유료 호출 0건(단계 fn 을 갈아 끼운다)."""
    import contextlib
    import io
    import time

    # ── 표의 모양 — 묶음이 단계표와 어긋나면 누른 묶음이 안 도는 단계를 말한다
    ids = [g["id"] for g in GROUPS]
    assert len(ids) == len(set(ids)), ids
    for g in GROUPS:
        assert set(g) == {"id", "name", "stages", "every_hours", "views"}, g
        assert set(g["stages"]) <= set(VALID_STAGE_NAMES) - set(TAIL), g
    grouped = {n for g in RUNNABLE_GROUPS for n in group_stages(g)}
    orphan = set(VALID_STAGE_NAMES) - set(TAIL) - grouped
    assert not orphan, f"어느 묶음에도 안 든 단계 — 전체 재기로도 영영 안 돈다: {orphan}"
    views = [v for g in GROUPS for v in g["views"]]
    assert len(views) == len(set(views)), f"한 화면이 두 묶음에 있다: {views}"
    assert set(DAILY) <= set(GROUP_BY_ID["search"]["stages"]), "매일 런이 검색 성과 밖에 있다"
    assert set(AFTER) | {d for v in AFTER.values() for d in v} <= set(VALID_STAGE_NAMES), AFTER
    # 순서 표에 고리가 있으면 _run_groups 가 영영 안 끝난다 — 전체 단계로 위상 정렬해 본다.
    waits, seen = _waits(VALID_STAGE_NAMES), set()
    while len(seen) < len(waits):
        nxt = [n for n in waits if n not in seen and waits[n] <= seen]
        assert nxt, f"순서 표에 고리가 있다: {sorted(set(waits) - seen)}"
        seen |= set(nxt)

    # ── plan — 공유 단계는 한 번, 꼬리는 늘, 매일 런은 gsc·ga4+꼬리
    p = plan(["search", "ai"])
    assert p.count("rank") == 1 and set(TAIL) <= set(p) and "crawl" not in p, p
    assert plan(["todo"]) == ("gsc", "ga4", "gaps", "pages", "report"), plan(["todo"])
    assert set(plan("all")) == set(VALID_STAGE_NAMES), plan("all")
    assert covered(plan(["search"])) == ["todo", "search"], covered(plan(["search"]))
    assert covered(plan(["ai"])) == ["ai"], covered(plan(["ai"]))
    try:
        group_names("search,없는묶음")
        raise AssertionError("모르는 묶음이 통과했다")
    except ValueError:
        pass

    # ── 실제로 돌린다: 가짜 단계가 시작·끝 시각을 남긴다
    log_lock = threading.Lock()
    spans: dict[str, tuple] = {}
    calls: list[str] = []

    def table(fail=(), slow=None):
        slow = slow or {}

        def fake(name):
            def fn(project, *, dry_run=False, **opts):
                t0 = time.monotonic()
                with log_lock:
                    calls.append(name)
                print(f"{name} 한 줄")
                time.sleep(slow.get(name, 0.05))
                with log_lock:
                    spans[name] = (t0, time.monotonic())
                if name in fail:
                    raise RuntimeError(f"{name} 터짐")
                return collector.succeeded(rows=1)
            return fn
        return tuple(s._replace(fn=fake(s.name), is_paid=False) for s in STAGES)

    def go(groups, **kw):
        calls.clear()
        spans.clear()
        progress = []
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            res = run_chain("__selfcheck__", stages=kw.pop("stages", None) or table(),
                            groups=groups, preflight=lambda s: {},
                            on_stage=lambda i, t, n: progress.append((i, t, n)), **kw)
        return res, buf.getvalue(), progress

    res, out, progress = go(["search", "ai", "site", "compete"],
                            stages=table(slow={"ai": 0.4, "crawl": 0.4}))
    names = [n for n, _ in res]
    assert names == list(plan(["search", "ai", "site", "compete"])), names
    # 공유 단계는 한 번 — rank 가 검색 성과와 AI 노출에 둘 다 있다
    assert calls.count("rank") == 1, calls
    # 순서: 앞이 **끝난 뒤에** 시작한다. 기대는 _waits 에서 뽑지 않고 여기 적는다 —
    # 거기서 뽑으면 표가 비어도 이 검사가 같이 비어 통과한다(실제로 그랬다).
    must = [("gsc", "keywords"), ("keywords", "metrics"), ("metrics", "rank"),
            ("rank", "competitors"), ("competitors", "backlinks"), ("gsc", "index"),
            ("ai", "gaps"), ("crawl", "gaps"), ("backlinks", "gaps"), ("gaps", "pages"),
            ("pages", "vitals"), ("vitals", "report"), ("pages", "report")]
    for d_, n in must:
        assert spans[d_][1] <= spans[n][0], f"{n} 이 {d_} 가 끝나기 전에 시작했다"
    # 동시성: ai 와 crawl 은 서로 안 기다린다 — 겹쳐서 돌아야 한다
    a, c = spans["ai"], spans["crawl"]
    assert a[0] < c[1] and c[0] < a[1], f"ai·crawl 이 겹치지 않았다(순차로 돌았다): {a} {c}"
    # 줄은 섞이지 않고 단계 머리표가 붙는다
    assert "[ai] ai 한 줄" in out and "[crawl] crawl 한 줄" in out, out
    # 진행률: 끝난 만큼만 센다, 마지막은 다 끝남, 이름 자리에 도는 단계들
    assert progress[-1][0] == progress[-1][1] + 1 and progress[-1][2] == "", progress[-1]
    assert any("," in n for _, _, n in progress), f"동시에 도는 단계가 보고되지 않았다: {progress}"
    assert [i for i, _, _ in progress] == sorted(i for i, _, _ in progress), progress

    # ── 실패 격리: 한 묶음이 터져도 다른 묶음은 끝까지, gaps 는 끝난 데이터로 선다
    res, out, _ = go(["search", "ai", "site"], stages=table(fail=("ai", "gsc")))
    got = dict(res)
    assert got["ai"].failed and got["gsc"].failed, got
    assert {"crawl", "rank", "keywords", "index", "gaps", "pages", "report"} <= set(calls), calls
    assert not got["gaps"].failed and not got["report"].failed, got
    assert chain_rc(res) == 1, "실패가 있는데 rc 가 0 이다"
    assert "다른 묶음은 계속" in out, out

    # ── 대기열의 다음 런: 방금 돈 공유 단계는 안 산다
    res, _, _ = go(["ai"], ran={"rank"})
    assert "rank" not in calls and "ai" in calls, calls
    assert dict(res)["rank"].reason == RAN_REASON and not dict(res)["rank"].failed, res

    # ── --skip 은 묶음 런에서도 듣는다 / --only 와 같이 쓰면 거절
    go(["compete"], skip="backlinks")
    assert "backlinks" not in calls and "competitors" in calls, calls
    try:
        run_chain("__selfcheck__", stages=table(), groups=["ai"], only="ai",
                  preflight=lambda s: {})
        raise AssertionError("--groups 와 --only 를 같이 받았다")
    except ValueError:
        pass

    # ── Brain 동시 쓰기: 한 단계가 쓰기 잠금을 5초 넘게 쥐어도 다른 단계의 쓰기가
    #    "database is locked" 로 버려지지 않는다(collector.stage 의 busy_timeout).
    #    잠금을 쥐는 시간은 기본 5초보다 넉넉해야 한다 — 5.5초로 두었을 때는 busy_timeout
    #    을 빼도 통과했다(여는 데 걸린 시간만큼 기다림이 5초 안에 들었다).
    #    기본 5초면 여기서 터진다.
    boot = db.connect()
    boot.execute("INSERT OR IGNORE INTO projects(name, domain, locale) "
                 "VALUES('__selfcheck__','sc.com','ko-KR')")
    boot.commit()
    boot.close()

    # 연결은 writer 가 먼저 연다 — db.connect 자체(_migrate 의 UPDATE)도 쓰기라, 잠금이
    # 잡힌 뒤에 열면 여기가 아니라 db.connect 의 기본 5초에서 먼저 막힌다(그건 db.py 몫).
    def holder(project, *, dry_run=False, **opts):
        time.sleep(0.5)                      # writer 가 연결을 먼저 열게
        with collector.stage(project) as st:
            st.conn.execute("INSERT INTO keywords(project_id, keyword, locale, source) "
                            "VALUES(?,?,?,?)", (st.pid, "hold", "ko-KR", "seed"))
            time.sleep(7.5)                  # 쓰기 잠금을 쥔 채로 — 기본 5초보다 넉넉히
            st.conn.commit()
            return st.done(rows=1)

    def writer(project, *, dry_run=False, **opts):
        with collector.stage(project) as st:
            time.sleep(1.5)                  # holder 가 잠금을 잡은 뒤에 쓰게
            n = st.each([f"w{i}" for i in range(5)], lambda kw: st.conn.execute(
                "INSERT INTO keywords(project_id, keyword, locale, source) VALUES(?,?,?,?)",
                (st.pid, kw, "ko-KR", "seed")))
            return st.verdict(n, rows=n)

    idle = lambda project, **kw: collector.succeeded()     # noqa: E731
    fns = {"ai": holder, "crawl": writer}
    dbt = tuple(s._replace(fn=fns.get(s.name, idle), is_paid=False) for s in STAGES)
    res, out, _ = go(["ai", "site"], stages=dbt)
    got = dict(res)
    assert not got["crawl"].failed and got["crawl"].rows == 5, (got["crawl"], out)
    c2 = db.connect()
    assert c2.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal", "WAL 이 안 켜졌다"
    n = c2.execute("SELECT COUNT(*) FROM keywords WHERE keyword IN "
                   "('hold','w0','w1','w2','w3','w4')").fetchone()[0]
    c2.close()
    assert n == 6, f"동시 쓰기 중 행을 잃었다: {n}/6"
    print("run_all group check ok")


def main() -> None:
    if len(sys.argv) == 1:
        _selfcheck()
        return
    ap = argparse.ArgumentParser(
        # 순서를 문자열로 다시 적지 않는다 — 표에서 뽑는다. 사본을 두면 단계가
        # 늘 때 도움말만 옛 순서를 말한다.
        description="전체 수집 체인 실행 — " + " → ".join(VALID_STAGE_NAMES)
    )
    ap.add_argument("--project", required=True, help="프로젝트 이름")
    ap.add_argument("--dry-run", action="store_true", help="실제 실행 없이 호출 계획 및 비용만 확인")
    ap.add_argument("--skip", help="건너뛸 단계 (쉼표 구분, 예: index,ai)")
    ap.add_argument("--only", help="실행할 단계 (쉼표 구분, 예: gsc,gaps) — 순서대로 하나씩 돈다")
    # 묶음 이름도 표에서 뽑는다 — 도움말에 사본을 적지 않는다.
    ap.add_argument("--groups", help="다시 잴 묶음 (쉼표 구분, 없으면 전체): "
                    + ", ".join(RUNNABLE_GROUPS))
    ap.add_argument("--opt", action="append", metavar="STAGE.KEY=VALUE",
                    help="스테이지별 옵션 (반복 지정). 예: --only rank --opt rank.device=mobile")
    args = ap.parse_args()

    # 원격 사이트면 서버가 같은 run_chain 을 돌고, 그 내레이션을 그대로 받아 찍는다.
    if remote.dispatch(args, None):
        return

    try:
        results = run_chain(
            args.project,
            dry_run=args.dry_run,
            skip=args.skip,
            only=args.only,
            opts=parse_opts(args.opt),
            # --only 는 단계를 손으로 고른 것이라 그 순서대로 돈다. 그 밖(인자 없음·
            # --groups·--skip)은 묶음 런 — 전체 재기는 다섯 묶음을 동시에 돌린다.
            # 둘 다 주면 run_chain 이 ValueError 로 막는다(조용히 한쪽을 버리지 않는다).
            groups=args.groups or (None if args.only else "all"),
        )
    except ValueError as e:
        print(f"[오류] {e}", file=sys.stderr)
        sys.exit(1)

    print_summary(args.project, results, dry_run=args.dry_run)
    sys.exit(chain_rc(results))


if __name__ == "__main__":
    main()
