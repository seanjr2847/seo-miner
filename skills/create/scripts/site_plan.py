#!/usr/bin/env python3
"""신규 사이트 계획 요청문 — **아직 한 번도 재지 않은 사이트**에서 출발하는 최소판.

  python site_plan.py <project>                              # 등록만 해 둔 사이트(Brain 에서 종류·로케일을 읽는다)
  python site_plan.py --domain example.com --type saas --locale en-US     # 등록 전

내는 것은 **요청문 한 장**(AI 에게 붙여 넣는 글)이다: 사이트 종류·로케일에 맞춘 권장 URL 트리
뼈대, 페이지별 권장 @type, 첫 측정 계획. seo-miner 의 원칙은 '실측 → 결정적 판정'이고 이
사이트에는 실측이 없다 — 그래서 이 글에는 **숫자가 없다**(검색량·순위·예상 유입·목표치 전부).
숫자는 첫 측정이 돈 뒤 기회 목록이 낸다. 이미 측정이 있는 사이트면 이 글을 내지 않고
`/create plan` 으로 보낸다(측정을 두고 뼈대부터 다시 짜지 않는다).

왜 create 에 두나: capture 는 재는 스킬이고 이 글은 잰 것이 하나도 없다. 리포에 무엇을
지을지 정하는 일이라 create 의 plan 앞 단계다. 요청문 꼴 표(brief.SHAPE_NAMES)에는 넣지
않았다 — 그 표는 **기회 하나**를 푸는 꼴이고 화면이 기회마다 그린다. 이 글에는 기회가 없다.

정본을 가리키기만 하는 것: 사이트 종류(dashboard.PROJECT_TYPES), 로케일(serp_adapter.LOCALES),
유형 이름(schema_vocab), 유형별 속성(scoring.SCHEMA_RULES), 측정 묶음·단계(run_all.GROUPS·STAGES).
여기 있는 것은 종류별 페이지 뼈대(TREES) 한 벌뿐이다.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "capture" / "scripts"))
import brief  # noqa: E402
import dashboard  # noqa: E402
import db  # noqa: E402
import remote  # noqa: E402
import run_all  # noqa: E402
import schema_vocab  # noqa: E402
import scoring  # noqa: E402
import serp_adapter  # noqa: E402

# (깊이, 역할, 무엇을 답하는 페이지인가, 권장 @type). 깊이 0 = 홈 바로 아래.
# 주소(slug)는 적지 않는다 — 사이트 언어로 짓는 것이고, 그건 받는 쪽이 로케일을 보고 정한다.
# FAQPage 는 넣지 않았다: 권할지 말지가 따로 정해지는 중이다(docs/borrow-from-claude-seo.md 7절 2번) —
# 정해지기 전에 뼈대가 먼저 권하면 요청문끼리 말이 갈린다.
COMMON = (
    (0, "홈", "누구의 사이트이고 무엇을 하는 곳인가", ("WebSite", "Organization")),
    (0, "소개", "누가 만들고 운영하는가", ("AboutPage",)),
    (0, "연락처", "어떻게 닿는가", ("ContactPage",)),
    (0, "개인정보처리방침·이용약관", "법적 고지(로케일이 요구하는 것까지)", ("WebPage",)),
)
TREES: dict[str, tuple] = {
    "saas": (
        (0, "기능", "무엇을 할 수 있나 — 기능 묶음마다 한 장", ("WebPage", "BreadcrumbList")),
        (1, "기능 상세", "그 기능 하나가 어떤 문제를 푸는가", ("WebPage", "BreadcrumbList")),
        (0, "요금", "얼마이고 무엇이 포함되나", ("SoftwareApplication",)),
        (0, "쓰는 사람별·상황별", "누가 어떤 일에 쓰는가", ("WebPage", "BreadcrumbList")),
        (0, "비교·대안", "다른 선택지와 무엇이 다른가", ("WebPage", "BreadcrumbList")),
        (0, "글 허브", "주제별 글 목록", ("CollectionPage",)),
        (1, "글", "질문 하나에 답하는 글", ("Article", "BreadcrumbList")),
        (0, "도움말·문서", "어떻게 쓰는가", ("TechArticle", "BreadcrumbList")),
    ),
    "game": (
        (0, "게임 소개", "어떤 게임이고 어디서 하나", ("VideoGame",)),
        (0, "받기·시작하기", "어떻게 설치하고 시작하나", ("WebPage", "BreadcrumbList")),
        (0, "공략·가이드 허브", "주제별 공략 목록", ("CollectionPage",)),
        (1, "공략 글", "질문 하나에 답하는 공략", ("Article", "BreadcrumbList")),
        (0, "소식·업데이트", "무엇이 바뀌었나", ("CollectionPage",)),
        (1, "소식 글", "업데이트 하나", ("Article", "BreadcrumbList")),
    ),
    "local_business": (
        (0, "서비스·메뉴 허브", "무엇을 해 주는 곳인가", ("CollectionPage",)),
        (1, "서비스 상세", "그 서비스 하나 — 대상·과정·비용 안내", ("Service", "BreadcrumbList")),
        (0, "지점·오시는 길", "어디 있고 언제 여나 — 지점마다 한 장", ("LocalBusiness",)),
        (0, "사람", "누가 맡는가(자격·경력)", ("Person",)),
        (0, "글 허브", "손님이 묻는 질문에 답하는 글 목록", ("CollectionPage",)),
        (1, "글", "질문 하나에 답하는 글", ("Article", "BreadcrumbList")),
    ),
    "directory": (
        (0, "분류 목록", "어떤 분류가 있나", ("CollectionPage", "ItemList")),
        (1, "분류 한 장", "그 분류의 항목들", ("CollectionPage", "ItemList", "BreadcrumbList")),
        (2, "항목 상세", "그 항목 하나 — 유형은 항목의 실제 종류를 따른다", ("ItemPage", "BreadcrumbList")),
        (0, "비교", "항목 둘 이상을 나란히", ("WebPage", "BreadcrumbList")),
        (0, "고르는 법 글", "어떻게 고르나", ("Article", "BreadcrumbList")),
        (0, "등록·수정 안내", "항목을 어떻게 올리고 고치나", ("WebPage",)),
    ),
    "commerce": (
        (0, "분류", "무엇을 파나", ("CollectionPage", "BreadcrumbList")),
        (1, "하위 분류", "그 분류 안의 갈래", ("CollectionPage", "ItemList", "BreadcrumbList")),
        (2, "상품", "그 상품 하나 — 값·재고·옵션", ("Product", "BreadcrumbList")),
        (0, "브랜드 이야기", "누가 만드나", ("AboutPage",)),
        (0, "배송·반품·교환", "사고 난 뒤 어떻게 되나", ("WebPage",)),
        (0, "고르는 법·관리법 글", "사기 전에 묻는 질문", ("Article", "BreadcrumbList")),
    ),
}
# 사이트 종류의 정본은 dashboard.PROJECT_TYPES 다 — 종류가 늘면 여기서 바로 걸린다(뼈대 없이 내보내지 않는다).
assert set(TREES) == set(dashboard.PROJECT_TYPE_IDS), set(dashboard.PROJECT_TYPE_IDS) ^ set(TREES)
# local_business 의 홈은 Organization 이 아니라 지점 유형이 주인이다.
HOME_TYPES = {"local_business": ("WebSite", "LocalBusiness")}
# 유형 이름은 schema.org 어휘에 있어야 한다(오타·없어진 유형을 권하지 않는다).
_VOCAB = schema_vocab.descendants("Thing") | {"Thing"}
for _rows in (COMMON, *TREES.values(), *( ((0, "", "", v),) for v in HOME_TYPES.values())):
    for _d, _r, _w, _types in _rows:
        assert set(_types) <= _VOCAB, (_r, set(_types) - _VOCAB)

UNKNOWN = "[확인 필요]"
NO_NUMBER_RULE = ("이 사이트는 **아직 한 번도 측정하지 않았습니다.** 검색량·순위·예상 방문 수·기간별 목표 같은 "
                  f"숫자를 쓰지 않습니다. 지어낸 숫자 대신 {UNKNOWN}라고 적고, 그 숫자는 첫 측정 뒤에 채웁니다.")


def _period(hours) -> str:
    """주기(시간) → 말. 숫자를 글에 싣지 않는다 — 이 글은 숫자가 없는 글이다."""
    if hours is None:
        return "주기 없음"
    if hours <= 24:
        return "매일"
    if hours <= run_all.WEEKLY_HOURS:
        return "매주"
    return "매달"


def tree_rows(type_id: str) -> list[tuple]:
    home = HOME_TYPES.get(type_id)
    common = [(d, r, w, home if (r == "홈" and home) else t) for d, r, w, t in COMMON]
    return common[:1] + list(TREES[type_id]) + common[1:]


def _type_lines(rows) -> list[str]:
    """권한 유형 중 속성 표(SCHEMA_RULES)가 있는 것만 — 채울 속성 이름을 표에서 읽어 적는다."""
    used = list(dict.fromkeys(t for _d, _r, _w, types in rows for t in types))
    out = []
    for t in used:
        if t not in scoring.SCHEMA_RULES:
            continue
        need = scoring.schema_props(t, "required")
        want = scoring.schema_props(t, "recommended")
        parts = ([f"필수 {', '.join(need)}"] if need else []) + ([f"권장 {', '.join(want)}"] if want else [])
        if parts:
            out.append(f"- {t}: {' · '.join(parts)}")
    return out


def measure_lines() -> list[str]:
    """첫 측정 계획 — 묶음·단계는 run_all.GROUPS·STAGES 에서 읽는다(여기 다시 적지 않는다)."""
    out = []
    for g in run_all.GROUPS:
        if not g["stages"]:
            continue
        paid = [s for s in g["stages"] if run_all.STAGE_BY_NAME[s].is_paid]
        free = [s for s in g["stages"] if s not in paid]
        bits = [f"무료 단계 {', '.join(free)}"] if free else []
        if paid:
            bits.append(f"유료 키가 있어야 도는 단계 {', '.join(paid)}")
        out.append(f"- {g['name']}({_period(g['every_hours'])}): {' · '.join(bits)}")
    return out


def build(domain: str, type_id: str, locale: str) -> str:
    """요청문 본문. 같은 입력이면 같은 글이다."""
    if type_id not in TREES:
        raise ValueError(f"사이트 종류 '{type_id}' 를 모릅니다 — {', '.join(dashboard.PROJECT_TYPE_IDS)} 중 하나")
    locales = dict(serp_adapter.LOCALES)
    if locale not in locales:
        raise ValueError(f"로케일 '{locale}' 를 모릅니다 — serp_adapter.LOCALES 에 있는 코드를 씁니다")
    type_label = dict(dashboard.PROJECT_TYPES)[type_id]
    lang = brief.lang_label(locale)
    rows = tree_rows(type_id)
    L = [f"아래 새 사이트의 구조 계획서를 만들어 주세요. 대상은 {domain} 입니다.",
         "",
         "## 한눈에",
         f"- 사이트: {domain}",
         f"- 종류: {type_label} ({type_id})",
         f"- 언어·지역: {locales[locale]} ({locale})",
         "- 측정: 아직 없음(검색 실적·순위·크롤 전부 없음)",
         "",
         "## 규칙",
         f"- {NO_NUMBER_RULE}",
         f"- 주소(slug)·페이지 제목·메뉴 이름은 {lang}로 짓습니다. 그 언어권 사람이 실제로 검색창에 치는 말을 "
         "쓰고, 다른 언어의 말을 옮겨 적지 않습니다.",
         "- 아래 뼈대는 이 종류의 사이트에 흔한 출발점입니다. 이 사이트에 없는 것(파는 것·하는 일·지점)을 "
         f"지어 채우지 않습니다. 사이트 주인에게 확인할 것은 {UNKNOWN}라고 남기고 맨 끝에 질문으로 모읍니다.",
         "- 같은 질문에 답하는 페이지를 둘 만들지 않습니다. 겹치면 하나로 합치고 이유를 적습니다.",
         "- 리포가 있으면 리포의 기존 경로 규칙·구조화 데이터 방식이 이 뼈대를 이깁니다.",
         "",
         "## 권장 URL 트리 뼈대",
         "역할 | 답하는 질문 | 권장 @type 순서입니다. 들여쓰기는 상위·하위입니다."]
    for depth, role, what, types in rows:
        L.append(f"{'  ' * depth}- {role} | {what} | {' + '.join(types)}")
    tl = _type_lines(rows)
    if tl:
        L += ["", "## 유형별로 채울 속성", "빠지면 첫 페이지 점검이 '빠진 것'으로 잡는 속성입니다."] + tl
    L += ["",
          "## 첫 측정 계획",
          "사이트가 뜬 뒤 이 순서로 잽니다. 묶음 이름은 대시보드 메뉴와 같습니다.",
          "- 준비: 구글 서치콘솔에 이 사이트를 속성으로 등록하고 소유를 확인합니다. 사이트맵을 제출합니다. "
          "실적은 등록한 뒤부터 쌓입니다.",
          *measure_lines(),
          "- 첫 기회 목록은 검색 실적이 쌓인 뒤에 나옵니다. 그 전에는 사이트 건강 묶음(색인·크롤·속도)이 "
          "먼저 쓸모가 있습니다 — 실적 없이도 페이지를 직접 열어 재기 때문입니다.",
          "",
          "## 만들어 줄 것",
          "- URL 트리: 위 뼈대를 이 사이트에 맞춰 편 것. 페이지마다 주소 | 역할 | 답하는 질문 | @type | 상위 페이지.",
          "- 페이지별 구조화 데이터 계획: 페이지 종류마다 유형 하나씩, 채울 속성과 그 값이 어디서 오는지"
          f"(없으면 {UNKNOWN}).",
          "- 내부 링크 뼈대: 허브와 하위 페이지가 서로 어떻게 잇는지, 홈에서 몇 번 눌러 닿는지는 세지 말고 경로로.",
          "- 첫 측정 전 점검표: 서치콘솔 등록·사이트맵·robots.txt·대표 주소(canonical)·언어 표기.",
          f"- 사이트 주인에게 물을 것: {UNKNOWN}라고 남긴 자리 전부.",
          "",
          "## 답의 형식",
          "표와 목록으로 답합니다. 추정으로 채운 칸에는 '추정'이라고 적고 근거를 답니다. 숫자는 쓰지 않습니다."]
    return "\n".join(L)


def site_of(project: str) -> dict:
    """등록한 사이트의 {domain, type, locale, measured}. 웹에 등록한 사이트는 서버 페이로드에서 읽는다."""
    if remote.owns(project):
        d = remote.api("GET", "/api/data", params={"project": project}) or {}
        p = d.get("project") if isinstance(d.get("project"), dict) else d
        return {"domain": p.get("domain") or "", "type": p.get("type") or "", "locale": p.get("locale") or "",
                "measured": bool(d.get("opps") or d.get("gsc") or d.get("last_run_at"))}
    if not db.DB_PATH.exists():
        raise SystemExit("보관함이 없습니다 — --domain --type --locale 로 직접 넘깁니다")
    conn = db.connect()
    try:
        row = db.get_project(conn, project)
        ran = conn.execute("SELECT COUNT(*) FROM runs WHERE project_id=? AND kind IN "
                           f"({','.join('?' * len(run_all.VALID_STAGE_NAMES))})",
                           (row["id"], *run_all.VALID_STAGE_NAMES)).fetchone()[0]
    finally:
        conn.close()
    return {"domain": row["domain"], "type": row["type"], "locale": row["locale"] or "",
            "measured": bool(ran)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="측정 전 사이트의 구조 계획 요청문")
    ap.add_argument("project", nargs="?")
    ap.add_argument("--domain")
    ap.add_argument("--type", dest="type_id")
    ap.add_argument("--locale")
    ap.add_argument("--force", action="store_true", help="측정이 있는 사이트에도 낸다")
    a = ap.parse_args(argv)
    site = {"domain": "", "type": "", "locale": "", "measured": False}
    if a.project:
        try:
            site = site_of(a.project)
        except db.ProjectNotFound as e:
            print(str(e), file=sys.stderr)
            return 1
    domain = a.domain or site["domain"]
    type_id = a.type_id or site["type"]
    locale = a.locale or site["locale"]
    missing = [n for n, v in (("--domain", domain), ("--type", type_id), ("--locale", locale)) if not v]
    if missing:
        print(f"{', '.join(missing)} 가 필요합니다(등록한 사이트 이름을 주면 거기서 읽습니다)", file=sys.stderr)
        return 1
    if site["measured"] and not a.force:
        print("이 사이트에는 이미 측정이 있습니다 — 뼈대부터 다시 짜지 않고 `/create plan` 으로 기회에서 "
              "출발합니다(그래도 내려면 --force).", file=sys.stderr)
        return 1
    try:
        print(build(domain, type_id, locale))
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    for s in (sys.stdout, sys.stderr):
        s.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
