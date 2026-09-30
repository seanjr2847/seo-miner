#!/usr/bin/env python3
"""데이터 점검(health audit) — 화면 페이로드에서 '이상한 모양'을 찾아 목록으로 낸다.

검사는 "계산이 맞다"를 본다. 운영 페이로드를 사람 눈으로 훑어야 보이던 것 — 열린 기회
226건 중 189건이 한 규칙, 추적 키워드 93%가 순위 없음, 'AI 노출' 묶음이 한 번도 안 돎,
속도 측정 10번 중 9번 실패 — 은 초록불 아래에서 몇 주씩 남았다(2026-09-30 운영 점검).
이 모듈이 그 훑기를 코드로 옮긴다.

  audit(d, ctx) -> [{sev, area, code, msg, count}]

d   : dashboard.gather() 가 낸 페이로드 그대로.
ctx : 페이로드에 안 실리는 사실(선택). gather 가 채운다 — 없으면 그 사실이 필요한
      규칙만 건너뛴다(옛 호스팅 페이로드를 로컬에서 다시 점검할 때).
        active          추적 중(is_active=1) 키워드 집합
        seeds           씨앗 키워드 수
        manual_rivals   직접 적은 경쟁사 수
        triage          심사 목록의 행 전부(판정한 것 포함) — dashboard._triage 의 rows 모양
        foreign_brands  scoring.foreign_brands 의 정규화 이름 집합
        now             datetime(UTC). 없으면 지금

등급: high(데이터가 틀렸거나 단계가 조용히 안 돈다) · mid(화면이 오해를 부른다) ·
low(알아 두면 좋은 것). '정보'(개수 요약)는 이상이 아니라서 안 낸다.

결과가 가는 곳 셋: ① 페이로드 d.health → [개요]의 "데이터 이상 N건" 접힌 띠
② gaps 단계 끝에서 한 줄 요약(log_line — Railway 로그) ③ test_remote(출시 문)가
높음을 알린다.

  python audit_data.py 페이로드.json [...]     # 저장된 /api/data 를 점검(읽기만)
  python audit_data.py --selfcheck
"""
from __future__ import annotations

import collections
import json
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
import brief    # noqa: E402
import scoring  # noqa: E402
import stage    # noqa: E402

SEV_ORDER = ("high", "mid", "low")
SEV_LABEL = {"high": "높음", "mid": "중간", "low": "낮음"}

# 런 notes 의 실패 표식 — 정본이다(dashboard._run_ok·test_remote 가 이것을 쓴다).
# \b 가 있어야 한다: rank 런은 "outline_errors=4" 를 남기는데, 앞에 경계가 없으면 그것을
# 실패로 읽는다. crawl 의 "issues=401" 도 HTTP 401 이 아니다 — 숫자로 실패를 짐작하지
# 않고 이 두 표식만 본다.
RUN_ERRORS = re.compile(r"\berrors=(\d+)")
RUN_ABORT = "중단:"
# 끝나지 않은 런을 '멈춘 런'으로 읽기까지의 시간 — 그 안이면 지금 도는 중일 수 있다.
RUN_HUNG_HOURS = 6

# 한 규칙이 열린 기회를 덮는다고 볼 몫 — 목록이 10건은 넘어야 몫이 뜻이 있다.
SKEW_SHARE, SKEW_MIN = 0.6, 10
# 추적 키워드 중 순위 없음 몫 — 넘으면 mid, 더 넘으면 high(조회 깊이·키워드 선택을 의심).
RANK_MISSING_MID, RANK_MISSING_HIGH = 0.5, 0.8
# AI 요약이 떴는데 인용을 못 받아 온(모름) 몫.
AIO_UNKNOWN_SHARE, AIO_UNKNOWN_MIN = 0.3, 5
# 심사 대기에 사이트 언어가 아닌 검색어가 섞인 몫. noti(25%)는 실제로 영어 도구 페이지를
# 가진 사이트다 — 그 정도 섞임은 정상이라 문턱을 그 위에 둔다.
TRIAGE_FOREIGN_SHARE, TRIAGE_FOREIGN_MIN = 0.3, 10
# 실패 몫이 이것을 넘으면 그 단계는 사실상 안 돈다(high).
BROKEN_SHARE = 0.5
# 근거 문장에 박힌 날짜가 이만큼 묵으면 옛 문장이다.
STALE_REASON_DAYS = 30
# 묶음 주기의 몇 배를 넘기면 '멈춘 묶음'으로 보나 — 1배는 화면의 주기 점(due)이 이미 말한다.
STALE_GROUP_FACTOR = 2

_DATE = re.compile(r"(20\d\d-\d\d-\d\d)")
# 영문자 경계만 본다 — 한글이 바로 붙으면(평균 None위) \b 가 안 먹는다.
_BAD_TXT = re.compile(r"(?<![A-Za-z])(?:None|undefined|NaN|nan)(?![A-Za-z])|\{[a-z_]+\}")
_CG_DOMAIN = re.compile(r"경쟁 도메인\s+([a-z0-9.-]+\.[a-z]{2,})", re.I)
_CITED_DOMAIN = re.compile(r"(?:경쟁 도메인|대신 인용되는 곳:?)\s*([a-z0-9.-]+\.[a-z]{2,})", re.I)
# 순위 URL 경로가 나라/언어 두 칸으로 시작하나(/us/en/ · /kr/ko/) — 섹션 이름으로만 쓴다.
_COUNTRY_SECT = re.compile(r"^/([a-z]{2})/([a-z]{2}(?:[_-][a-z]{2})?)/")
_HANGUL = re.compile(r"[가-힣ㄱ-ㆎ]")
_KANA = re.compile(r"[぀-ヿ]")
_HAN = re.compile(r"[一-鿿]")
_LATIN = re.compile(r"[a-z]", re.I)


# ── 작은 도구 ────────────────────────────────────────────────────────────────

def _host(u) -> str:
    try:
        h = urlsplit(u if "//" in str(u) else "//" + str(u)).hostname or ""
    except ValueError:
        return ""
    return h[4:] if h.startswith("www.") else h


def _own(h: str, dom: str) -> bool:
    return bool(dom) and (h == dom or h.endswith("." + dom))


def _day(s) -> date | None:
    try:
        return date.fromisoformat(str(s)[:10])
    except (TypeError, ValueError):
        return None


def _ts(s) -> datetime | None:
    try:
        return datetime.strptime(str(s)[:19].replace("T", " "),
                                 "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _stage_t(kind: str) -> str:
    """단계 이름 — 정본은 stage.STAGE_LABELS. 모르는 단계는 이름 그대로."""
    return (stage.STAGE_LABELS.get(kind) or {}).get("t") or kind


def _eg(items, n: int = 3) -> str:
    items = [str(x) for x in items if str(x).strip()]
    return (" — 예: " + ", ".join(items[:n])) if items else ""


def run_error_count(notes) -> int:
    """notes 의 errors=N — 없으면 0. 부분 일치(outline_errors=)는 안 센다."""
    m = RUN_ERRORS.search(notes or "")
    return int(m.group(1)) if m else 0


def run_failed(notes) -> bool:
    """이 런이 실패인가 — notes 만 보고(끝났는지는 부르는 쪽이 따로 본다)."""
    return RUN_ABORT in (notes or "") or run_error_count(notes) > 0


def site_lang(locale: str | None) -> str:
    return (locale or "ko-KR").split("-")[0].lower()


def foreign_script(text: str, lang: str) -> bool:
    """사이트 언어의 글자가 하나도 없고 다른 문자로 쓴 검색어인가.

    라틴 한 단어(sofwave·img2go)는 거의 늘 제품·도구 이름이라 언어로 안 가른다 — 두 단어
    이상(is sculptra worth it)일 때만 다른 언어로 본다. 섞인 것(7pm 알림)은 사이트 말이다.
    """
    t = str(text or "")
    has = {"ko": bool(_HANGUL.search(t)), "ja": bool(_KANA.search(t)),
           "han": bool(_HAN.search(t)), "latin": bool(_LATIN.search(t))}
    mine = {"ko": ("ko",), "ja": ("ja", "han"), "zh": ("han",)}.get(lang, ("latin",))
    if any(has[s] for s in mine):
        return False
    if has["ko"] or has["ja"] or has["han"]:
        return True
    return has["latin"] and "latin" not in mine and len(t.split()) >= 2


def _find(out: list, sev: str, area: str, code: str, msg: str, count: int = 0) -> None:
    out.append({"sev": sev, "area": area, "code": code, "msg": msg, "count": int(count)})


# ── 규칙 ─────────────────────────────────────────────────────────────────────
# 규칙마다 (d, ctx, out) 을 받는다. 새 규칙은 RULES 에 더하고 _selfcheck 에 걸리는 페이로드와
# 안 걸리는 페이로드를 같이 넣는다.

def _open(d) -> list[dict]:
    return [o for o in (d.get("opps") or []) if o.get("status") in scoring.OPEN_STATUSES]


def _ol(d, o) -> str:
    """기회 한 줄의 예시 — 검색어(종류 이름). 종류 id(ctr_gap)는 화면에 안 낸다."""
    k = o.get("kind")
    return f"{o.get('target')}({(d.get('kind_labels') or {}).get(k, k)})"


def _tracked(d, ctx) -> set[str]:
    """추적 중인 검색어 — ctx.active 가 정본. 없으면 순위 표의 검색어로 물러선다."""
    act = ctx.get("active")
    return set(act) if act is not None else set((d.get("rank_by_kw") or {}).keys())


def r_runs(d, ctx, out):
    """단계마다 가장 최근 런이 실패(errors>0·중단)했거나 오래 안 끝났나."""
    now = ctx.get("now") or datetime.now(timezone.utc)
    last: dict[str, dict] = {}
    for r in d.get("runs") or []:          # id 내림차순 — 처음 본 것이 가장 최근
        last.setdefault(r.get("kind"), r)
    bad = []
    for k, r in last.items():
        notes = r.get("notes") or ""
        if run_failed(notes):
            n = run_error_count(notes)
            m = re.search(r"first_error=(.{0,70})", notes)
            why = f"오류 {n}건" if n else "중단"
            bad.append(f"{_stage_t(k)}({why}{' · ' + m.group(1).strip() if m else ''})")
        elif not r.get("finished_at"):
            t = _ts(r.get("started_at"))
            if t and (now - t) > timedelta(hours=RUN_HUNG_HOURS):
                bad.append(f"{_stage_t(k)}(안 끝남 · {str(r.get('started_at'))[:10]} 시작)")
    if bad:
        _find(out, "high", "런", "run_failed",
              f"마지막 런이 실패한 단계 {len(bad)}개 — " + "; ".join(bad), len(bad))


def r_ai_health(d, ctx, out):
    """AI 질문이 있는데 한 번도 안 쟀나(묶음이 안 돈다) · 전부 옛 생성 판인가."""
    ah = d.get("ai_health") or {}
    active = int(ah.get("active") or 0)
    if active and not ah.get("measured") and not ah.get("last_run"):
        grp = next((g.get("name") for g in d.get("groups") or []
                    if "ai" in (g.get("stages") or [])), None)
        _find(out, "high", "AI 인용", "ai_never_measured",
              f"AI 에 물을 질문 {active}개를 한 번도 재지 않았습니다"
              + (f" — '{grp}' 묶음이 이 사이트에서 돈 적이 없습니다" if grp else ""), active)
    outd = int(ah.get("outdated") or 0)
    if outd and active and outd >= active:
        _find(out, "mid", "AI 인용", "ai_outdated",
              f"질문 {outd}개가 전부 옛 생성 판입니다(지금 판 {ah.get('gen_version')})"
              + _eg(ah.get("outdated_eg") or [], 2), outd)


def r_opp_skew(d, ctx, out):
    """한 종류가 열린 기회를 덮나 · 한 종류의 근거가 한 도메인에 쏠렸나."""
    op = _open(d)
    kinds = collections.Counter(o.get("kind") for o in op)
    labels = d.get("kind_labels") or {}
    if len(op) >= SKEW_MIN:
        for k, n in kinds.most_common(1):
            if n / len(op) > SKEW_SHARE:
                _find(out, "mid", "기회", "kind_skew",
                      f"열린 기회 {len(op)}건 중 {n}건({round(n / len(op) * 100)}%)이 "
                      f"'{labels.get(k, k)}' 한 종류입니다 — 규칙 하나가 목록을 덮습니다", n)
    doms = collections.Counter()
    for o in op:
        m = _CITED_DOMAIN.search(o.get("reasoning") or "")
        if m:
            doms[(o.get("kind"), m.group(1).lower())] += 1
    for (k, dm), n in doms.most_common():
        tot = kinds.get(k, 0)
        if tot >= 5 and n / tot >= 0.5:
            _find(out, "mid", "기회", "domain_skew",
                  f"'{labels.get(k, k)}' {tot}건 중 {n}건이 같은 도메인({dm})을 근거로 듭니다", n)


def r_aio(d, ctx, out):
    """AI 요약 빠짐 — 순위 밖 검색어에 섰나(P1) · 추적에서 뺀 검색어에 섰나."""
    op = [o for o in _open(d) if o.get("kind") == "aio_exposure"]
    if not op:
        return
    rb = d.get("rank_by_kw") or {}
    unranked = [o["target"] for o in op
                if str(o.get("target")) in rb and rb[str(o["target"])].get("pos") is None]
    if unranked:
        heavy = len(unranked) >= 5 and len(unranked) / len(op) >= 0.3
        _find(out, "high" if heavy else "mid", "기회", "aio_unranked",
              f"'구글 AI 요약 빠짐' {len(op)}건 중 {len(unranked)}건이 우리가 순위에 없는 "
              "검색어입니다 — 순위 문제를 AI 요약 문제로 말합니다" + _eg(unranked), len(unranked))
    tracked = _tracked(d, ctx)
    if tracked:
        off = [o["target"] for o in op if str(o.get("target")) not in tracked]
        if off:
            _find(out, "mid", "기회", "aio_untracked",
                  f"추적에서 뺀 검색어의 '구글 AI 요약 빠짐'이 {len(off)}건 열려 있습니다"
                  + _eg(off), len(off))


def r_content_gap(d, ctx, out):
    """콘텐츠 공백의 근거 도메인이 지금 경쟁사가 아닌가(판매 채널로 뺀 곳의 옛 행)."""
    rivals = {_host(x) for x in d.get("gap_rivals") or []}
    bad = []
    for o in _open(d):
        if o.get("kind") != "content_gap":
            continue
        m = _CG_DOMAIN.search(o.get("reasoning") or "")
        if m and _host(m.group(1)) not in rivals:
            bad.append(f"{o.get('target')}←{m.group(1)}")
    if bad:
        _find(out, "mid", "기회", "cg_not_rival",
              f"콘텐츠 공백 {len(bad)}건의 근거 도메인이 지금 경쟁사가 아닙니다" + _eg(bad), len(bad))


def r_opp_text(d, ctx, out):
    """고칠 페이지를 못 정한 고치기 · 띄어쓰기만 다른 중복 · 옛 날짜 근거 · 빈 자리 문구."""
    op = _open(d)
    nopage = [_ol(d, o) for o in op
              if (o.get("brief") or {}).get("shape")
              and brief._shows_page(o["brief"]["shape"]) and not o["brief"].get("page")]
    if nopage:
        _find(out, "mid", "기회", "fix_no_page",
              f"페이지를 손대는 요청문인데 고칠 페이지를 못 정한 기회 {len(nopage)}건"
              + _eg(nopage), len(nopage))
    dup = collections.Counter((o.get("kind"), scoring.norm(str(o.get("target")))) for o in op)
    dups = [t for (k, t), n in dup.items() if n > 1]
    if dups:
        _find(out, "low", "기회", "dup_target",
              f"같은 종류·같은 검색어(띄어쓰기만 다름)가 따로 선 기회 {len(dups)}묶음"
              + _eg(dups), len(dups))
    today = ctx.get("today") or date.today()
    fresh = _day(d.get("gsc_date"))
    old = []
    for o in op:
        ds = [x for x in (_day(s) for s in _DATE.findall(o.get("reasoning") or "")) if x]
        if ds and (today - max(ds)).days > STALE_REASON_DAYS and (not fresh or fresh > max(ds)):
            old.append(f"{_ol(d, o)} · {max(ds)}")
    if old:
        _find(out, "low", "기회", "stale_reason",
              f"근거 문장의 날짜가 {STALE_REASON_DAYS}일 넘게 지난 기회 {len(old)}건 — "
              "최신 수집이 있는데 옛 수로 말합니다" + _eg(old, 2), len(old))
    bad = [_ol(d, o) for o in op
           if _BAD_TXT.search((o.get("reasoning") or "") + " "
                              + ((o.get("brief") or {}).get("body") or ""))]
    if bad:
        _find(out, "high", "문구", "bad_text",
              f"근거·요청문에 None/undefined/{{자리}}가 찍힌 기회 {len(bad)}건" + _eg(bad), len(bad))


def r_triage(d, ctx, out):
    """심사 목록에 다른 언어 검색어가 섞였나(P13) · 남의 브랜드로 보이는 검색어.

    판정한 행까지 센다 — 심사 화면은 판정한 것도 탭으로 보여 주고, theotherskin 은 영어·
    중국어 150건이 이미 '작업'으로 넘어가 대기에는 3건만 남아 있었다(대기만 세면 못 본다)."""
    rows = ctx.get("triage")
    if not rows:
        return
    loc = (d.get("project") or {}).get("locale") or "ko-KR"
    lang, kl = site_lang(loc), d.get("kw_locales") or {}

    def other(r):
        lab = str(r.get("label") or r.get("key") or "")
        kloc = kl.get(lab)
        return (kloc and site_lang(kloc) != lang) or foreign_script(lab, lang)
    fx = [r for r in rows if not r.get("brand") and other(r)]
    if len(fx) >= TRIAGE_FOREIGN_MIN and len(fx) / len(rows) >= TRIAGE_FOREIGN_SHARE:
        _find(out, "mid", "심사", "triage_foreign",
              f"심사 목록 {len(rows)}건 중 {len(fx)}건이 사이트 언어({loc})가 아닌 검색어입니다"
              + _eg([r.get("label") for r in fx]), len(fx))
    fb = [b for b in (ctx.get("foreign_brands") or ()) if len(b) >= 3]
    theirs = [r for r in rows if not r.get("brand")
              and any(b in str(r.get("key") or "") for b in fb)]
    if theirs:
        _find(out, "low", "심사", "triage_foreign_brand",
              f"심사 목록에 남의 브랜드 이름이 든 검색어 {len(theirs)}건"
              + _eg([r.get("label") for r in theirs]), len(theirs))


def r_rank(d, ctx, out):
    """순위 — 순위 없음 몫(P6) · 추적 뺀 검색어가 순위 표에(P7) · 언어-지역 · 남의 URL · 나라 섹션."""
    rb = d.get("rank_by_kw") or {}
    act = ctx.get("active")
    tracked = [r for k, r in rb.items() if act is None or k in act]
    if tracked:
        nul = [r for r in tracked if r.get("pos") is None]
        share = len(nul) / len(tracked)
        if share >= RANK_MISSING_MID:
            _find(out, "high" if share >= RANK_MISSING_HIGH else "mid", "순위", "rank_missing",
                  f"추적 {len(tracked)}개 중 {len(nul)}개({round(share * 100)}%)가 순위 없음입니다 — "
                  "조회 깊이나 고른 키워드를 봅니다", len(nul))
    if act is not None:
        extra = [k for k in rb if k not in act]
    else:
        extra = list(rb)[int(d.get("kw_active") or 0):] if d.get("kw_active") is not None else []
    if extra:
        _find(out, "mid", "순위", "ranks_untracked",
              f"순위 표에 추적에서 뺀 검색어 {len(extra)}개가 섞여 있습니다"
              + (_eg(extra) if act is not None else ""), len(extra))
    loc = (d.get("project") or {}).get("locale") or "ko-KR"
    kl = d.get("kw_locales") or {}
    other = collections.Counter(kl[r["keyword"]] for r in tracked
                                if kl.get(r.get("keyword")) and kl[r["keyword"]] != loc)
    if other:
        n = sum(other.values())
        _find(out, "low", "순위", "kw_locale_other",
              f"추적 키워드 {n}개를 사이트({loc})와 다른 언어-지역에서 잽니다("
              + ", ".join(f"{k} {v}" for k, v in other.most_common(3)) + ")", n)
    dom = _host((d.get("project") or {}).get("domain") or "")
    foreign = [f"{r['keyword']}→{_host(r['url'])}" for r in tracked
               if r.get("url") and dom and not _own(_host(r["url"]), dom)]
    if foreign:
        _find(out, "high", "순위", "rank_foreign_url",
              f"순위 URL 이 우리 도메인이 아닌 검색어 {len(foreign)}개" + _eg(foreign), len(foreign))
    sect = collections.Counter()
    for r in tracked:
        if r.get("url"):
            m = _COUNTRY_SECT.match(urlsplit(r["url"]).path or "")
            if m:
                sect[m.group(0)] += 1
    if len(sect) > 1:
        _find(out, "mid", "순위", "rank_sections",
              "순위 URL 이 여러 나라 섹션에 흩어져 있습니다("
              + ", ".join(f"{k} {v}" for k, v in sect.most_common(4)) + ") — 다른 나라 페이지로 잡힙니다",
              len(sect))
    aio_on = [r for r in tracked if r.get("aio")]
    unk = [r for r in aio_on if r.get("aio_cited") is None]
    if len(unk) >= AIO_UNKNOWN_MIN and len(unk) / len(aio_on) >= AIO_UNKNOWN_SHARE:
        _find(out, "mid", "AI 요약", "aio_cite_unknown",
              f"AI 요약이 뜬 {len(aio_on)}개 중 {len(unk)}개는 누구를 인용했는지 못 받아 왔습니다",
              len(unk))


def r_rivals(d, ctx, out):
    """진짜 경쟁사가 없나(P5) · 역할 판정 기록이 없나."""
    roles = d.get("comp_roles") or []
    rivals = [r for r in roles if r.get("role") == "rival"]
    manual = ctx.get("manual_rivals")
    if d.get("comp_date") and not rivals and not manual:
        _find(out, "mid", "경쟁사", "no_rivals",
              "진짜 경쟁사가 한 곳도 없습니다(직접 적은 것도, 판정된 것도) — "
              "[설정]에서 경쟁사를 적거나 [경쟁 분석]에서 찾기를 돌립니다", 0)
    if not roles and (d.get("kw_gap") or d.get("gap_rivals")):
        _find(out, "low", "경쟁사", "roles_missing",
              "경쟁 후보의 역할 판정 기록이 없습니다 — 판정 전 코드로 모은 후보가 그대로 쓰입니다", 0)


def r_backlinks(d, ctx, out):
    """깨진 백링크가 사실은 크롤러 차단(403·429)인가 · 코드 없이 적은 옛 깨짐인가."""
    import collect_page     # 차단 코드의 정본(BLOCK_STATUS) — 무거워서 여기서만 부른다
    br = [x for x in d.get("bl_links") or [] if x.get("is_broken")]
    if not br:
        return
    blocked = [x for x in br if x.get("to_status") in collect_page.BLOCK_STATUS]
    if blocked:
        _find(out, "mid", "백링크", "bl_broken_blocked",
              f"깨진 링크 {len(br)}개 중 {len(blocked)}개가 크롤러 차단(403·429)입니다 — "
              "깨진 게 아닙니다", len(blocked))
    nocode = [x for x in br if x.get("to_status") is None]
    if nocode:
        _find(out, "low", "백링크", "bl_broken_nocode",
              f"깨진 링크 {len(nocode)}개가 응답 코드 없이 적힌 옛 회차입니다", len(nocode))


def r_pages(d, ctx, out):
    """페이지 점검 — 404 가 점검 대상에 남았나(P9) · 점검이 대부분 실패했나(P11)."""
    pa = d.get("page_audits") or {}
    if not pa:
        return
    dead = [u for u, a in pa.items() if a.get("status") == 404]
    if dead:
        _find(out, "mid", "페이지 점검", "page_404",
              f"점검 {len(pa)}장 중 {len(dead)}장이 열리지 않는 페이지(404)입니다 — "
              "점검 자리를 먹고 매주 다시 실패합니다" + _eg(dead, 2), len(dead))
    fail = [u for u, a in pa.items()
            if a.get("status") != 404 and (a.get("error") or (a.get("status") or 200) >= 400)]
    if fail:
        share = len(fail) / len(pa)
        _find(out, "high" if share >= BROKEN_SHARE else "mid", "페이지 점검", "page_audit_failed",
              f"점검 {len(pa)}장 중 {len(fail)}장을 못 읽었습니다"
              + (" — 봇 차단을 의심합니다" if share >= BROKEN_SHARE else "") + _eg(fail, 2), len(fail))


def r_vitals(d, ctx, out):
    """속도 측정이 반복해서 실패하나(P10). vitals 는 {url: {mobile, desktop}} 모양이다."""
    v = d.get("vitals") or {}
    rows = [s for per in (v.values() if isinstance(v, dict) else [])
            if isinstance(per, dict) for s in per.values() if isinstance(s, dict)]
    err = [s for s in rows if s.get("error")]
    if err:
        share = len(err) / len(rows)
        why = collections.Counter(re.sub(r"\s+", " ", str(s["error"]))[:60] for s in err)
        _find(out, "high" if share >= BROKEN_SHARE else "mid", "속도", "vitals_errors",
              f"속도 측정 {len(rows)}번 중 {len(err)}번 실패 — {why.most_common(1)[0][0]}", len(err))


def r_groups(d, ctx, out):
    """묶음 주기의 몇 배를 넘겨 안 잰 단계 — 한 번도 성공 못 한 단계는 다른 규칙이 본다."""
    now = ctx.get("now") or datetime.now(timezone.utc)
    late = []
    for g in d.get("groups") or []:
        every = g.get("every_hours")
        if not every:
            continue
        for st, day in (g.get("last") or {}).items():
            t = _ts(f"{day} 00:00:00") if day else None
            if t and (now - t).total_seconds() > every * 3600 * STALE_GROUP_FACTOR + 86400:
                late.append(f"{_stage_t(st)}({day})")
    late = list(dict.fromkeys(late))
    if late:
        _find(out, "mid", "신선도", "stale_group",
              f"묶음 주기의 {STALE_GROUP_FACTOR}배를 넘겨 안 잰 단계 {len(late)}개 — " + ", ".join(late[:5]),
              len(late))


def r_plays(d, ctx, out):
    bad = [p for p in d.get("plays") or []
           if isinstance(p.get("result"), dict) and p["result"].get("error")]
    if bad:
        _find(out, "mid", "할 일", "plays_failed",
              f"이번 달 할 일 {len(d.get('plays') or [])}건 중 {len(bad)}건이 수정안을 못 만들었습니다 — "
              + str(bad[0]["result"]["error"])[:80], len(bad))


def r_setup(d, ctx, out):
    """서치콘솔은 있는데 씨앗 키워드가 없음(P17) — 억지로 채우라는 게 아니라 한 줄 안내."""
    if ctx.get("seeds") == 0 and (d.get("project") or {}).get("gsc_property"):
        _find(out, "low", "설정", "no_seeds",
              "씨앗 키워드가 없습니다 — 서치콘솔 검색어로 돌지만, 노리는 주제가 따로 있으면 "
              "[설정]에 적습니다", 0)


RULES = (r_runs, r_ai_health, r_opp_skew, r_aio, r_content_gap, r_opp_text, r_triage,
         r_rank, r_rivals, r_backlinks, r_pages, r_vitals, r_groups, r_plays, r_setup)


def audit(d: dict, ctx: dict | None = None) -> list[dict]:
    """페이로드 d 를 점검해 [{sev, area, code, msg, count}] 를 등급 순으로 낸다.

    규칙 하나가 터져도 나머지는 돈다 — 점검이 화면(gather)을 깨면 안 된다. 터진 규칙은
    그 사실을 낮음 한 줄로 남긴다(조용히 삼키면 아무것도 안 보는 규칙이 된다).
    """
    ctx = dict(ctx or {})
    now = ctx.get("now")
    if now is None:
        now = ctx["now"] = datetime.now(timezone.utc)
    ctx.setdefault("today", now.date())
    out: list[dict] = []
    for rule in RULES:
        try:
            rule(d, ctx, out)
        except Exception as e:   # noqa: BLE001 — 점검 하나가 화면을 막지 않는다
            _find(out, "low", "점검", "audit_error", f"{rule.__name__} 점검이 터졌습니다: {e!r}"[:160])
    return sorted(out, key=lambda f: SEV_ORDER.index(f["sev"]))


def counts(findings: list[dict]) -> dict:
    c = collections.Counter(f["sev"] for f in findings)
    return {s: c.get(s, 0) for s in SEV_ORDER}


def log_line(project: str, findings: list[dict]) -> str:
    """Railway 로그 한 줄 — gaps 단계 끝이 찍는다. 높음이 있으면 그 이름까지."""
    if not findings:
        return f"[health] {project}: 데이터 이상 없음"
    c = counts(findings)
    sev = " · ".join(f"{SEV_LABEL[s]} {c[s]}" for s in SEV_ORDER if c[s])
    hi = [f["code"] for f in findings if f["sev"] == "high"]
    return (f"[health] {project}: 데이터 이상 {len(findings)}건 ({sev})"
            + (f" — 높음: {', '.join(hi)}" if hi else ""))


# ── 자체점검 ────────────────────────────────────────────────────────────────

def _selfcheck() -> None:
    """규칙마다 걸리는 가짜 페이로드와 안 걸리는 것. 네트워크·DB 0회."""
    now = datetime(2026, 9, 30, 12, tzinfo=timezone.utc)
    base = {"project": {"domain": "ex.com", "locale": "ko-KR", "gsc_property": "sc-domain:ex.com"},
            "gsc_date": "2026-09-29", "kind_labels": {"aio_exposure": "구글 AI 요약 빠짐"}}

    def codes(d, **ctx):
        return {f["code"] for f in audit({**base, **d}, {"now": now, **ctx})}

    def hit(code, d, **ctx):
        got = codes(d, **ctx)
        assert code in got, (code, got)

    def miss(code, d, **ctx):
        got = codes(d, **ctx)
        assert code not in got, (code, got)

    # 빈 페이로드는 아무것도 안 낸다(터지지도 않는다)
    assert audit({}, {"now": now}) == [], audit({}, {"now": now})

    # 런 — errors=N·중단은 실패, outline_errors=·issues=401 은 실패가 아니다
    run = lambda n, fin="x", st="2026-09-30T01:00:00Z": {
        "kind": "rank", "notes": n, "finished_at": fin, "started_at": st}
    hit("run_failed", {"runs": [run("urls=5 errors=9 first_error=HTTP 400 NO_FCP")]})
    hit("run_failed", {"runs": [run("중단: 키 없음")]})
    miss("run_failed", {"runs": [run("errors=0 outlines=36 outline_errors=4")]})
    miss("run_failed", {"runs": [{**run("seed=sitemap pages=300 issues=401"), "kind": "crawl"}]})
    # 최근 것만 본다 — 뒤에 성공했으면 앞 실패는 지나간 일
    miss("run_failed", {"runs": [run("errors=0"), run("errors=3")]})
    hit("run_failed", {"runs": [run("", None, "2026-09-29T01:00:00Z")]})      # 하루 넘게 안 끝남
    miss("run_failed", {"runs": [run("", None, "2026-09-30T10:00:00Z")]})     # 지금 도는 중
    assert "outline_errors=4" and run_error_count("x=1 outline_errors=4") == 0

    # AI 질문 — 한 번도 안 잼 / 잼 / 전부 옛 판
    grp = {"groups": [{"id": "ai", "name": "AI 노출", "stages": ["ai"], "last": {}}]}
    hit("ai_never_measured", {**grp, "ai_health": {"active": 19, "measured": 0, "last_run": None}})
    miss("ai_never_measured", {"ai_health": {"active": 19, "measured": 19, "last_run": {"id": 1}}})
    miss("ai_never_measured", {"ai_health": {"active": 0, "measured": 0, "last_run": None}})
    assert any("AI 노출" in f["msg"] for f in audit(
        {**grp, "ai_health": {"active": 3, "measured": 0}}, {"now": now}))
    hit("ai_outdated", {"ai_health": {"active": 5, "measured": 5, "outdated": 5, "last_run": {}}})
    miss("ai_outdated", {"ai_health": {"active": 5, "measured": 5, "outdated": 2, "last_run": {}}})

    # 기회 쏠림
    op = lambda k, t, **kw: {"kind": k, "target": t, "status": "new", "reasoning": "", **kw}
    many = [op("aio_exposure", f"q{i}") for i in range(9)] + [op("ctr_gap", "z")]
    hit("kind_skew", {"opps": many})
    miss("kind_skew", {"opps": many[:5] + [op("ctr_gap", f"c{i}") for i in range(5)]})
    miss("kind_skew", {"opps": many[:4]})                                    # 10건 미만
    miss("kind_skew", {"opps": [dict(o, status="resolved") for o in many]})  # 닫힌 건 안 센다
    dm = [op("ai_citation_gap", f"p{i}", reasoning="대신 인용되는 곳: rival.com") for i in range(5)]
    hit("domain_skew", {"opps": dm})
    miss("domain_skew", {"opps": dm[:4]})

    # AI 요약 — 순위 밖 / 추적 뺀 것
    rb = {f"q{i}": {"keyword": f"q{i}", "pos": None} for i in range(9)}
    hit("aio_unranked", {"opps": many, "rank_by_kw": rb})
    assert next(f for f in audit({**base, "opps": many, "rank_by_kw": rb}, {"now": now})
                if f["code"] == "aio_unranked")["sev"] == "high"
    miss("aio_unranked", {"opps": many,
                          "rank_by_kw": {k: dict(v, pos=4) for k, v in rb.items()}})
    hit("aio_untracked", {"opps": many, "rank_by_kw": rb}, active={"q0"})
    miss("aio_untracked", {"opps": many, "rank_by_kw": rb}, active=set(rb))

    # 콘텐츠 공백의 근거가 지금 경쟁사가 아님
    cg = [op("content_gap", "가방", reasoning="경쟁 도메인 lfmall.co.kr 이 3위")]
    hit("cg_not_rival", {"opps": cg, "gap_rivals": ["rival.com"]})
    miss("cg_not_rival", {"opps": cg, "gap_rivals": ["www.lfmall.co.kr"]})

    # 고칠 페이지 없음 — 새 글 꼴은 페이지가 없는 게 정상이다
    hit("fix_no_page", {"opps": [op("ctr_gap", "a", brief={"shape": "fix_page", "page": None})]})
    miss("fix_no_page", {"opps": [op("aio_exposure", "a", brief={"shape": "new_content"})]})
    miss("fix_no_page", {"opps": [op("ctr_gap", "a", brief={"shape": "fix_page", "page": "https://ex.com/a"})]})

    # 띄어쓰기만 다른 중복
    hit("dup_target", {"opps": [op("cannibalization", "구진성 흉터"), op("cannibalization", "구진성흉터")]})
    miss("dup_target", {"opps": [op("cannibalization", "구진성 흉터"), op("ctr_gap", "구진성흉터")]})

    # 옛 날짜 근거 — 최신 수집이 그보다 뒤일 때만
    old = [op("rank_decay", "x", reasoning="11위 → 14위 (gsc 2026-08-25→2026-08-27)")]
    hit("stale_reason", {"opps": old})
    miss("stale_reason", {"opps": [op("rank_decay", "x", reasoning="(gsc 2026-09-20→2026-09-27)")]})

    # 빈 자리 문구
    hit("bad_text", {"opps": [op("ctr_gap", "a", reasoning="평균 None위")]})
    hit("bad_text", {"opps": [op("ctr_gap", "a", brief={"body": "제목은 {title} 입니다"})]})
    miss("bad_text", {"opps": [op("ctr_gap", "a", reasoning="Nonenal 같은 말은 괜찮다 {Ab}")]})

    # 심사 — 다른 언어 섞임(몫·최소 건수) · 브랜드 행은 안 센다
    tri = ([{"key": f"k{i}", "label": f"what is milia {i}"} for i in range(10)]
           + [{"key": f"h{i}", "label": f"비립종 {i}"} for i in range(10)])
    hit("triage_foreign", {}, triage=tri)
    miss("triage_foreign", {}, triage=tri[:9] + tri[10:])                    # 9건 < 최소 10
    miss("triage_foreign", {}, triage=[dict(r, brand=True) if r["key"].startswith("k") else r
                                       for r in tri])
    miss("triage_foreign", {}, triage=[{"key": f"s{i}", "label": f"sofwave{i}"} for i in range(20)])
    hit("triage_foreign", {}, triage=[{"key": f"c{i}", "label": f"韩国皮肤科{i}"} for i in range(20)])
    hit("triage_foreign", {"kw_locales": {f"sofwave{i}": "en-US" for i in range(20)}},
        triage=[{"key": f"s{i}", "label": f"sofwave{i}"} for i in range(20)])
    assert foreign_script("7pm 알림", "ko") is False and foreign_script("비립종", "en") is True
    hit("triage_foreign_brand", {}, triage=[{"key": "unniapp후기", "label": "unni app 후기"}],
        foreign_brands={"unniapp"})
    miss("triage_foreign_brand", {}, triage=[{"key": "unniapp후기", "label": "x", "brand": True}],
         foreign_brands={"unniapp"})

    # 순위
    rk = lambda k, pos, **kw: {"keyword": k, "pos": pos, **kw}
    nulls = {f"k{i}": rk(f"k{i}", None if i < 9 else 3) for i in range(10)}
    hit("rank_missing", {"rank_by_kw": nulls})
    miss("rank_missing", {"rank_by_kw": {k: dict(v, pos=5) for k, v in nulls.items()}})
    # 추적 뺀 검색어의 행은 몫에서도 빠진다
    miss("rank_missing", {"rank_by_kw": nulls}, active={"k9"})
    hit("ranks_untracked", {"rank_by_kw": nulls}, active={"k9"})
    miss("ranks_untracked", {"rank_by_kw": nulls}, active=set(nulls))
    hit("ranks_untracked", {"rank_by_kw": nulls, "kw_active": 4})            # ctx 없는 물러섬
    miss("ranks_untracked", {"rank_by_kw": nulls, "kw_active": 10})
    hit("kw_locale_other", {"rank_by_kw": nulls, "kw_locales": {"k1": "en-US"}})
    miss("kw_locale_other", {"rank_by_kw": nulls, "kw_locales": {"k1": "ko-KR"}})
    hit("rank_foreign_url", {"rank_by_kw": {"g": rk("g", 1, url="https://gucci.com/us/en/x")}})
    miss("rank_foreign_url", {"rank_by_kw": {"g": rk("g", 1, url="https://www.ex.com/x")}})
    miss("rank_foreign_url", {"rank_by_kw": {"g": rk("g", 1, url="https://m.ex.com/x")}})
    hit("rank_sections", {"rank_by_kw": {"a": rk("a", 1, url="https://ex.com/us/en/a"),
                                         "b": rk("b", 1, url="https://ex.com/kr/ko/b")}})
    miss("rank_sections", {"rank_by_kw": {"a": rk("a", 1, url="https://ex.com/kr/ko/a"),
                                          "b": rk("b", 1, url="https://ex.com/en/porecare/b")}})
    aio = {f"a{i}": rk(f"a{i}", 3, aio=1, aio_cited=None if i < 5 else 0) for i in range(8)}
    hit("aio_cite_unknown", {"rank_by_kw": aio})
    miss("aio_cite_unknown", {"rank_by_kw": {k: dict(v, aio_cited=0) for k, v in aio.items()}})

    # 경쟁사
    hit("no_rivals", {"comp_date": "2026-09-22", "comp_roles": []})
    miss("no_rivals", {"comp_date": "2026-09-22", "comp_roles": [{"domain": "r.com", "role": "rival"}]})
    miss("no_rivals", {"comp_date": "2026-09-22"}, manual_rivals=1)
    miss("no_rivals", {})                                                     # 안 돌았으면 다른 얘기
    hit("roles_missing", {"gap_rivals": ["r.com"], "comp_roles": []})
    miss("roles_missing", {"gap_rivals": ["r.com"], "comp_roles": [{"domain": "r.com", "role": "media"}]})

    # 백링크
    hit("bl_broken_blocked", {"bl_links": [{"is_broken": 1, "to_status": 403}]})
    miss("bl_broken_blocked", {"bl_links": [{"is_broken": 1, "to_status": 404}]})
    hit("bl_broken_nocode", {"bl_links": [{"is_broken": 1, "to_status": None}]})
    miss("bl_broken_nocode", {"bl_links": [{"is_broken": 0, "to_status": None}]})

    # 페이지 점검 — 404 는 따로, 나머지 실패는 몫으로 등급
    pa = lambda st, err=None: {"status": st, "error": err, "title": "t"}
    hit("page_404", {"page_audits": {"a": pa(404, "HTTP 404"), "b": pa(200)}})
    miss("page_404", {"page_audits": {"b": pa(200)}})
    miss("page_audit_failed", {"page_audits": {"a": pa(404, "HTTP 404"), "b": pa(200)}})
    blocked = {f"u{i}": pa(403, "HTTP 403") for i in range(4)}
    assert [f["sev"] for f in audit({**base, "page_audits": blocked}, {"now": now})
            if f["code"] == "page_audit_failed"] == ["high"]
    assert [f["sev"] for f in audit({**base, "page_audits": {**blocked, **{f"o{i}": pa(200) for i in range(8)}}},
                                    {"now": now}) if f["code"] == "page_audit_failed"] == ["mid"]

    # 속도 — {url: {mobile, desktop}} 모양을 읽는다
    vit = {"https://ex.com/": {"mobile": {"error": "Lighthouse returned error: NO_FCP"},
                               "desktop": {"error": "Lighthouse returned error: NO_FCP"}},
           "https://ex.com/b": {"mobile": {"error": None}, "desktop": {"error": None}}}
    hit("vitals_errors", {"vitals": vit})
    miss("vitals_errors", {"vitals": {"https://ex.com/b": vit["https://ex.com/b"]}})

    # 묶음 주기
    g = lambda day: {"groups": [{"id": "site", "every_hours": 168, "last": {"crawl": day}}]}
    hit("stale_group", g("2026-09-01"))
    miss("stale_group", g("2026-09-25"))
    miss("stale_group", g(None))

    hit("plays_failed", {"plays": [{"result": {"error": "402 Payment Required"}}]})
    miss("plays_failed", {"plays": [{"result": {"fix": {}}}]})

    hit("no_seeds", {}, seeds=0)
    miss("no_seeds", {}, seeds=2)
    miss("no_seeds", {"project": {"domain": "ex.com"}}, seeds=0)             # 서치콘솔 없는 사이트는 다른 길

    # 터진 규칙은 삼키지 않고 한 줄 남긴다
    boom = audit({**base, "opps": "문자열"}, {"now": now})
    assert any(f["code"] == "audit_error" for f in boom), boom

    # 등급 순 · 한 줄 요약
    f = audit({**base, "runs": [run("errors=1")], "bl_links": [{"is_broken": 1}]}, {"now": now})
    assert [x["sev"] for x in f] == ["high", "low"], f
    assert set(f[0]) == {"sev", "area", "code", "msg", "count"}, f[0]
    line = log_line("site", f)
    assert "데이터 이상 2건" in line and "높음 1" in line and "run_failed" in line, line
    assert log_line("site", []) == "[health] site: 데이터 이상 없음"
    print("  ok  audit_data self-check")


def main(argv: list[str]) -> int:
    if "--selfcheck" in argv:
        _selfcheck()
        return 0
    for path in [a for a in argv if not a.startswith("-")]:
        d = json.loads(Path(path).read_text(encoding="utf-8"))
        now = _ts(f"{d.get('gsc_date')} 12:00:00") if d.get("gsc_date") else None
        f = d.get("health") or audit(d, {"now": now} if now else None)
        print(f"\n### {Path(path).name} — {log_line((d.get('project') or {}).get('name') or '?', f)}")
        for x in f:
            print(f"- [{SEV_LABEL[x['sev']]}] {x['area']} · {x['code']}: {x['msg']}")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main(sys.argv[1:]))
