#!/usr/bin/env python3
"""판정 규칙 한 곳 — striking distance·움직임·남의 브랜드·pSEO 후보·기회 목록.

여기가 생기기 전에는 같은 규칙이 SQL(dashboard·collect_gsc)·Python·템플릿 JS·
references/scoring.md 산문 네 군데에 흩어져 있었고 값이 서로 어긋나 있었다
(움직임 임계 0.4 vs 0.5). 임계값을 바꾸려면 이 파일만 고친다.

references/scoring.md 는 이제 명세이고, 실행은 전부 여기서 한다.

self-check:  python scoring.py
"""
import re
import sqlite3

# 1페이지 경계. 화면(깊이 그래프)·SQL·산문이 같은 값을 봐야 한다.
PAGE1 = 10

# striking_distance: 밀면 상단 진입이 가능한 평균 순위 구간 (scoring.md 1절)
STRIKING_LO, STRIKING_HI = 4, 20

# GSC Δpos 노이즈 바닥 (scoring.md 4-4). 이보다 작은 변화는 "움직였다"고 하지 않는다.
NOISE_POS = 0.5

# rank_decay 기회로 올릴 하락 폭 (scoring.md 1절). 노이즈 바닥과 다른 개념이다 —
# 노이즈는 "화면에 띄울까", 이건 "방어 기회로 적재할까".
DECAY_POS = -1.5

# pseo_pattern 후보 추출 임계 (scoring.md 1b-1). 노출 큰 사이트면 올린다.
PSEO_MIN_IMP, PSEO_MAX_CTR = 50, 1.5

# 남의 브랜드 검색 판별용 (scoring.md 1a).
BRAND_MODIFIERS = {
    "후기", "리뷰", "review", "reviews", "가격", "요금", "pricing", "price",
    "무료", "free", "다운로드", "download", "로그인", "login", "사용법",
    "tutorial", "app", "ai",
}
# 디렉터리·비교 콘텐츠가 정당하게 이길 수 있는 자리 — 남의 브랜드여도 기회로 둔다.
KEEP_INTENTS = {
    "alternative", "alternatives", "대안", "vs", "versus", "비교",
    "competitor", "competitors", "best", "추천",
}


def norm(s: str) -> str:
    """비교용 정규화 — 소문자 + 영숫자/한글만. 'Future Tools' 와 'futuretools.io' 를 같게 본다."""
    return re.sub(r"[^0-9a-z가-힣]+", "", (s or "").lower())


def tokens(s: str) -> list[str]:
    return [t for t in re.split(r"[^0-9a-zA-Z가-힣]+", (s or "").lower()) if t]


def host_of(url: str) -> str:
    """URL에서 호스트만. 스킴·www·포트·경로를 벗긴다."""
    h = re.sub(r"^[a-z]+://", "", (url or "").strip().lower())
    h = h.split("/")[0].split("?")[0].split("#")[0].split(":")[0]
    return h[4:] if h.startswith("www.") else h


def owns(domain: str, own: str) -> bool:
    """domain 이 own 자신이거나 그 하위 도메인인가. 네 군데에 복제돼 있던 두 줄."""
    d, o = host_of(domain), host_of(own)
    return bool(d and o) and (d == o or d.endswith("." + o))


def _stem(domain: str) -> str:
    """도메인에서 브랜드 이름 후보 — 'ecrett.com' -> 'ecrett'."""
    return norm(host_of(domain).split(".")[0])


def foreign_brands(conn: sqlite3.Connection, project_id: int, cfg: dict | None = None) -> set[str]:
    """남의 도구·서비스 이름 카탈로그 (정규화된 형태).

    출처: competitors 테이블 도메인 + 프로젝트 yaml 의 tools/foreign_brands.
    자기 브랜드(name·brand_aliases)는 반대로 반드시 남긴다 — 내 브랜드 검색의
    낮은 CTR은 진짜 문제다 (scoring.md 1a).
    """
    cfg = cfg or {}
    names = set()
    for r in conn.execute("SELECT domain FROM competitors WHERE project_id=?", (project_id,)):
        s = _stem(r[0])
        if len(s) >= 3:
            names.add(s)
    for key in ("foreign_brands", "tools"):
        for n in (cfg.get(key) or []):
            s = norm(n if isinstance(n, str) else (n.get("name", "") if isinstance(n, dict) else ""))
            if len(s) >= 3:
                names.add(s)
    own = {norm(cfg.get("name", ""))} | {norm(a) for a in (cfg.get("brand_aliases") or [])}
    return {n for n in names if n and n not in own}


def is_foreign_brand(query: str, brands: set[str]) -> bool:
    """쿼리가 '남의 브랜드 이름(+흔한 수식어)' 인가.

    이름 단독과 `{이름} 후기/review/가격` 같은 변형은 걸러내고,
    `{이름} alternative` / `{이름} vs {이름}` 같은 비교 의도는 남긴다.
    """
    if not brands:
        return False
    ts = tokens(query)
    if not ts or any(t in KEEP_INTENTS for t in ts):
        return False
    core = [t for t in ts if t not in BRAND_MODIFIERS]
    if not core:
        return False
    return norm("".join(core)) in brands


def drop_foreign_brands(rows: list[dict], brands: set[str], key: str = "query") -> list[dict]:
    return [r for r in rows if not is_foreign_brand(r.get(key, ""), brands)]


# 일부 엔진은 인용 메타데이터 없이 본문에 URL을 그대로 적는다 — 그때의 fallback.
URL_RE = re.compile(r"https?://[^\s)\]>\"']+")


def aliases_of(cfg: dict) -> list[str]:
    """판정에 쓸 자기 브랜드 별칭 — 이름 + brand_aliases.

    두 측정 경로(collect_ai·browse)가 각자 조립하면 별칭 규칙이 갈라져
    judge 가 같아도 수치가 어긋난다."""
    return [a for a in [cfg.get("name", "")] + (cfg.get("brand_aliases") or []) if a]


def judge(content: str, citation_urls: list[str] | None, aliases: list[str],
          own_domain: str) -> tuple[int, int, list[str]]:
    """AI 답변 하나를 (언급됐나, 인용됐나, 대신 인용된 도메인들) 로 판정.

    키 경로(collect_ai)와 브라우저 경로(browse/record_check)가 같은 판정을 써야
    두 경로의 수치를 한 화면에서 비교할 수 있다. browse가 collect_ai 내부를
    가로질러 import 하던 것을 여기로 옮겼다. 인용 URL이 없으면 본문의 맨 URL을
    줍는 fallback까지 여기서 한다 — 호출부 두 곳이 각자 하던 일이다.
    """
    text = (content or "").lower()
    mentioned = int(any(a.lower() in text for a in aliases if a))
    urls = citation_urls or URL_RE.findall(content or "")
    domains = sorted({d for d in (host_of(u) for u in urls) if d})
    cited = int(any(owns(d, own_domain) for d in domains))
    others = [d for d in domains if not owns(d, own_domain)]
    return mentioned, cited, others


def gap_to_page1(pos) -> float:
    """1페이지 경계까지 되밀어야 할 거리. 1페이지 안이면 0."""
    if pos is None:
        return 0.0
    return round(max(0.0, float(pos) - PAGE1), 1)


def moved_up(dpos: float, dclk: int) -> bool:
    return dpos > NOISE_POS or dclk > 0


def moved_down(dpos: float, dclk: int) -> bool:
    return dpos < -NOISE_POS or dclk < 0


def snapshot_pair(conn: sqlite3.Connection, project_id: int) -> tuple[str | None, str | None, int | None, bool]:
    """직전 스냅샷을 같은 period_days 중 가장 최근으로 고른다 (scoring.md 4-3b).

    기간이 다른 스냅샷끼리 빼면 Δ순위·Δ클릭이 전부 거짓이 된다.
    """
    snaps = [(r[0], r[1]) for r in conn.execute(
        """SELECT snapshot_date, MAX(period_days) period_days
             FROM gsc_snapshots WHERE project_id=?
            GROUP BY snapshot_date ORDER BY 1 DESC LIMIT 10""", (project_id,)).fetchall()]
    cur, period = snaps[0] if snaps else (None, None)
    prev = next((d for d, pd in snaps[1:] if pd == period), None)
    period_mismatch = bool(snaps[1:]) and prev is None
    return cur, prev, period, period_mismatch


def movers(now_: dict, before: dict, *, limit: int = 10) -> tuple[list[dict], list[dict]]:
    """두 스냅샷 집계(query -> row)를 받아 (오른 것, 내린 것).

    호출부가 period_days 가 같은 스냅샷끼리만 넘겨야 한다 (scoring.md 4-3b).
    """
    rows = []
    for kw, r in now_.items():
        b = before.get(kw)
        if b:
            rows.append({"query": kw, "pos": r["pos"], "dpos": round(b["pos"] - r["pos"], 1),
                         "clk": r["clk"], "dclk": r["clk"] - b["clk"], "imp": r["imp"]})
    ups = sorted([m for m in rows if moved_up(m["dpos"], m["dclk"])],
                 key=lambda m: (-m["dclk"], -m["dpos"]))[:limit]
    downs = sorted([m for m in rows if moved_down(m["dpos"], m["dclk"])],
                   key=lambda m: (m["dclk"], m["dpos"]))[:limit]
    return ups, downs


_STRIKING_SQL = """
SELECT query, ROUND(AVG(position),1) pos, SUM(impressions) imp, SUM(clicks) clk
  FROM gsc_snapshots WHERE project_id=? AND snapshot_date=?
 GROUP BY query HAVING pos BETWEEN ? AND ?
 ORDER BY imp DESC LIMIT ?
"""


def striking(conn: sqlite3.Connection, project_id: int, snapshot_date: str | None,
             *, limit: int = 15, brands: set[str] | None = None) -> list[dict]:
    """조금만 밀면 1페이지 갈 검색어. 남의 브랜드 검색은 빼고, gap 을 붙여 돌려준다."""
    if not snapshot_date:
        return []
    # 브랜드를 걸러내면 limit 미만이 되므로 넉넉히 뽑고 자른다.
    over = limit * 3 if brands else limit
    rows = [dict(r) for r in conn.execute(
        _STRIKING_SQL, (project_id, snapshot_date, STRIKING_LO, STRIKING_HI, over))]
    if brands:
        rows = drop_foreign_brands(rows, brands)
    rows = rows[:limit]
    for r in rows:
        r["gap"] = gap_to_page1(r["pos"])
    return rows


def pseo_candidates(conn: sqlite3.Connection, project_id: int, snapshot_date: str | None,
                    *, limit: int = 200, min_imp: int = PSEO_MIN_IMP,
                    max_ctr: float = PSEO_MAX_CTR) -> list[dict]:
    """수요는 있는데(노출) 클릭이 비어 있는 쿼리 — pSEO 군집 후보 (scoring.md 1b-1).
    군집으로 묶는 것은 Claude 판단이고, 여기는 후보 추출까지만 한다."""
    if not snapshot_date:
        return []
    return [dict(r) for r in conn.execute(
        """SELECT query, SUM(impressions) imp, SUM(clicks) clk,
                  ROUND(SUM(clicks)*100.0/SUM(impressions),2) ctr_pct,
                  ROUND(AVG(position),1) pos
             FROM gsc_snapshots
            WHERE project_id=? AND snapshot_date=?
            GROUP BY query HAVING imp >= ? AND ctr_pct < ?
            ORDER BY imp DESC LIMIT ?""",
        (project_id, snapshot_date, min_imp, max_ctr, limit))]


def opportunities(conn: sqlite3.Connection, project_id: int, *,
                  limit: int, with_id: bool = False) -> list[dict]:
    """기회 목록 — 화면과 박제본이 같은 정렬을 본다.

    정렬이 두 벌이던 시절엔 대시보드와 리포트가 같은 데이터로 다른 순서를 보여줬다.
    """
    cols = ("id, kind, target, ROUND(score,1) score, reasoning, status, "
            "substr(created_at,1,10) created") if with_id else \
           "kind, target, ROUND(score,1) score, reasoning, status"
    return [dict(r) for r in conn.execute(
        f"""SELECT {cols} FROM opportunities WHERE project_id=?
             ORDER BY (status='new') DESC, score DESC, id DESC LIMIT ?""",
        (project_id, limit))]


def stage(p: dict, name: str, domain: str) -> dict:
    """갱도 6단계 판정 — "지금 할 것"을 정하는 곳은 여기 하나다.

    p 는 dashboard.progress() 가 센 단계별 실적. done 은 그 실적으로만 판정한다 —
    "했다 치고" 넘어가지 않는다. 순서가 정보다: 앞 단계가 없으면 뒤 단계는 재료가
    없어 돌지 않는다. 이 판정이 템플릿 JS 에 있던 시절엔 Python 에서 시험할 수
    없었고, doctor·화면이 서로 다른 다음 할 일을 말할 수 있었다.
    """
    steps = [
        {"id": "register", "t": "사이트 등록",
         "gain": "측정할 도메인과 시작 검색어를 정합니다. 여기서 출발합니다.",
         "done": True, "state": domain or "", "cmd": None},
        {"id": "gsc", "t": "구글 실적 읽기",
         "gain": "서치콘솔에서 실제 노출·클릭·순위를 가져옵니다. 이 도구의 모든 판단이 "
                 "이 숫자 위에서 이뤄집니다 — 추측을 안 하려고 제일 먼저 합니다.",
         "done": p["gsc_days"] > 0,
         "state": (f"{p['gsc_days']}번 읽음 · 최근 {p['gsc_last']}" if p["gsc_days"]
                   else "아직 안 읽음"),
         "cmd": f"/capture gsc {name}"},
        {"id": "keywords", "t": "키워드 캐기",
         "gain": "자동완성으로 후보를 모아 추적할 목록을 만듭니다. 무료이고, 여기서 "
                 "늘린 만큼 다음 단계가 볼 게 많아집니다.",
         "done": p["keywords_found"] > 0,
         "state": (f"캔 것 {p['keywords_found']}개 · 추적 {p['keywords']}개"
                   if p["keywords_found"]
                   else (f"직접 적은 {p['keywords']}개뿐" if p["keywords"] else "아직 없음")),
         "cmd": f"/capture keywords {name}"},
        {"id": "ai", "t": "AI 노출 확인",
         "gain": "ChatGPT·Perplexity·Gemini가 이 주제에서 누구를 인용하는지 봅니다. "
                 "브라우저로 직접 물어보면 키 없이 무료입니다.",
         "done": p["ai_checks"] > 0,
         "state": (f"답변 {p['ai_checks']}개 확인 · 질문 {p['ai_prompts']}개"
                   if p["ai_checks"]
                   else ("질문은 준비됨 · 아직 안 물어봄" if p["ai_prompts"]
                         else "물어볼 질문부터 필요")),
         "cmd": (f"/seo-miner:browse {name}" if p["ai_prompts"] else f"/capture add {name}")},
        {"id": "gaps", "t": "손댈 것 뽑기",
         "gain": "모은 숫자에서 기회를 계산합니다 — 조금만 밀면 1페이지인 검색어, 우리 "
                 "대신 인용되는 곳, 같은 틀로 여러 장 찍을 수 있는 페이지.",
         "done": p["opps"] > 0,
         "state": f"{p['opps']}건 뽑음" if p["opps"] else "아직 없음",
         "cmd": f"/capture gaps {name}"},
        {"id": "create", "t": "실제로 고치기",
         "gain": "뽑은 기회를 리포의 진짜 콘텐츠 변경으로 만듭니다. 브랜치와 PR로 "
                 "나가고, 끝나면 그 기회가 완료로 닫힙니다.",
         "done": p["creations"] > 0,
         "state": f"{p['creations']}건 고침" if p["creations"] else "아직 한 건도 안 함",
         "cmd": f"/create plan {name}"},
    ]
    here = next((i for i, s in enumerate(steps) if not s["done"]), -1)
    return {"steps": steps, "here": here}


def _selfcheck() -> None:
    assert norm("Future Tools") == "futuretools"
    assert host_of("https://www.Ecrett.com/pricing?a=1") == "ecrett.com"
    assert owns("blog.example.com", "example.com")
    assert owns("example.com", "https://example.com/")
    assert not owns("notexample.com", "example.com")
    assert _stem("futuretools.io") == "futuretools"

    brands = {"ecrett", "futuretools", "paperpal"}
    assert is_foreign_brand("ecrett", brands)
    assert is_foreign_brand("paperpal 후기", brands)
    assert is_foreign_brand("future tools", brands)
    assert is_foreign_brand("Ecrett Pricing", brands)
    assert not is_foreign_brand("ecrett alternative", brands)     # 비교 의도는 남긴다
    assert not is_foreign_brand("ecrett vs paperpal", brands)
    assert not is_foreign_brand("aitierlist", brands)             # 내 브랜드
    assert not is_foreign_brand("ai 툴 순위", brands)
    assert not is_foreign_brand("", brands)
    assert not is_foreign_brand("ecrett", set())                  # 카탈로그 없으면 아무것도 안 뺀다

    m, c, others = judge("Try Ecrett and MySite today.",
                         ["https://www.ecrett.com/a", "https://blog.mysite.com/b"],
                         ["MySite"], "mysite.com")
    assert (m, c, others) == (1, 1, ["ecrett.com"]), (m, c, others)
    assert judge("nothing here", [], ["MySite"], "mysite.com") == (0, 0, [])
    # 인용 메타데이터가 없으면 본문의 맨 URL을 줍는다 — collect_ai·record_check 공통
    assert judge("see https://blog.mysite.com/x", [], ["MySite"],
                 "mysite.com")[1] == 1
    assert aliases_of({"name": "MySite", "brand_aliases": ["마이사이트", ""]}) \
        == ["MySite", "마이사이트"]

    pr = {"gsc_days": 0, "gsc_last": "", "keywords": 2, "keywords_found": 0,
          "ai_checks": 0, "ai_prompts": 0, "opps": 0, "creations": 0}
    st = stage(pr, "demo", "demo.com")
    assert st["here"] == 1 and st["steps"][1]["id"] == "gsc", st["here"]
    assert st["steps"][3]["cmd"] == "/capture add demo"       # 질문이 없으면 add 부터
    st = stage({**pr, "gsc_days": 3, "gsc_last": "2026-08-14", "keywords_found": 5,
                "ai_prompts": 10}, "demo", "demo.com")
    assert st["here"] == 3
    assert st["steps"][3]["cmd"] == "/seo-miner:browse demo"  # 질문이 있으면 browse
    st = stage({**pr, "gsc_days": 1, "keywords_found": 1, "ai_checks": 1,
                "ai_prompts": 1, "opps": 1, "creations": 1}, "demo", "demo.com")
    assert st["here"] == -1                                    # 한 바퀴 다 돎

    assert gap_to_page1(14.2) == 4.2
    assert gap_to_page1(3.0) == 0.0
    assert gap_to_page1(None) == 0.0

    # 0.5 이하 변화는 노이즈 — 예전 0.4 임계에서는 통과하던 값
    assert not moved_up(0.45, 0)
    assert moved_up(0.6, 0)
    assert moved_up(0.0, 3)
    assert moved_down(-0.6, 0)
    assert not moved_down(-0.45, 0)

    now_ = {"a": {"pos": 5.0, "clk": 10, "imp": 100},
            "b": {"pos": 12.0, "clk": 1, "imp": 90}}
    before = {"a": {"pos": 8.0, "clk": 4, "imp": 100},
              "b": {"pos": 9.0, "clk": 5, "imp": 90}}
    ups, downs = movers(now_, before)
    assert [u["query"] for u in ups] == ["a"], ups
    assert [d["query"] for d in downs] == ["b"], downs

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
      CREATE TABLE competitors(project_id INT, domain TEXT);
      CREATE TABLE gsc_snapshots(project_id INT, snapshot_date TEXT, period_days INT,
        query TEXT, page TEXT, clicks INT, impressions INT, ctr REAL, position REAL);
      CREATE TABLE opportunities(id INTEGER PRIMARY KEY, project_id INT, kind TEXT,
        target TEXT, score REAL, reasoning TEXT, status TEXT, created_at TEXT);
    """)
    conn.execute("INSERT INTO competitors VALUES(1,'ecrett.com')")
    conn.executemany(
        "INSERT INTO gsc_snapshots VALUES(1,'2026-08-14',28,?,NULL,?,?,0.0,?)",
        [("ecrett", 0, 48, 8.6), ("내 키워드", 2, 400, 12.0), ("1페이지 키워드", 9, 300, 3.0)])
    brands = foreign_brands(conn, 1, {"name": "aitierlist"})
    assert brands == {"ecrett"}, brands
    rows = striking(conn, 1, "2026-08-14", brands=brands)
    assert [r["query"] for r in rows] == ["내 키워드"], rows   # 3.0위는 구간 밖, ecrett는 남의 브랜드
    assert rows[0]["gap"] == 2.0
    assert striking(conn, 1, None) == []
    cands = pseo_candidates(conn, 1, "2026-08-14")
    assert [c["query"] for c in cands] == ["내 키워드"], cands

    conn.executemany(
        "INSERT INTO opportunities(project_id,kind,target,score,reasoning,status,created_at) "
        "VALUES(1,'striking_distance',?,?,'r',?,'2026-08-01')",
        [("old-done", 99, "done"), ("new-low", 10, "new"), ("new-high", 50, "new")])
    got = [o["target"] for o in opportunities(conn, 1, limit=10)]
    assert got == ["new-high", "new-low", "old-done"], got
    assert "id" in opportunities(conn, 1, limit=1, with_id=True)[0]

    conn.execute("INSERT INTO gsc_snapshots(project_id,snapshot_date,period_days) VALUES(2,'2026-08-20',28)")
    conn.execute("INSERT INTO gsc_snapshots(project_id,snapshot_date,period_days) VALUES(2,'2026-08-10',90)")
    conn.execute("INSERT INTO gsc_snapshots(project_id,snapshot_date,period_days) VALUES(2,'2026-08-01',28)")
    cur, prev, period, mismatch = snapshot_pair(conn, 2)
    assert (cur, prev, period, mismatch) == ("2026-08-20", "2026-08-01", 28, False), (cur, prev, period, mismatch)

    conn.execute("INSERT INTO gsc_snapshots(project_id,snapshot_date,period_days) VALUES(3,'2026-08-20',28)")
    conn.execute("INSERT INTO gsc_snapshots(project_id,snapshot_date,period_days) VALUES(3,'2026-08-10',90)")
    cur, prev, period, mismatch = snapshot_pair(conn, 3)
    assert (cur, prev, period, mismatch) == ("2026-08-20", None, 28, True), (cur, prev, period, mismatch)

    print("scoring self-check ok")


if __name__ == "__main__":
    _selfcheck()
