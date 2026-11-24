#!/usr/bin/env python3
"""유료 단계 지출 상한 — `python test_spend_cap.py` (네트워크 0, 임시 보관함).

preflight 가 runs.cost_estimate_usd 합을 상한과 견줘, 넘었으면 그 유료 단계를 건너뛰고
사유를 남긴다. 범위는 사이트 하나 / 보관함 전체(호스팅에서는 테넌트 = 가입자 한 명),
창은 오늘 / 이번 달(UTC 달력). 상한이 없으면(0·미설정) 아무것도 막지 않는다.
"""
import contextlib
import io
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

os.environ["CAPTURE_HOME"] = tempfile.mkdtemp(prefix="seo-miner-spend-")
sys.path.insert(0, str(Path(__file__).parent))

import collect_ai  # noqa: E402
import db  # noqa: E402
import run_all  # noqa: E402
import run_verdict  # noqa: E402
import serp_adapter  # noqa: E402

NOW = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)
CAP_ENVS = [name for name, _, _ in run_all.SPEND_CAPS]


def _brain():
    conn = db.connect()
    conn.execute("DELETE FROM runs")
    for name in ("a", "b"):
        conn.execute("INSERT OR IGNORE INTO projects(name, domain, locale) VALUES(?,?,'en-US')",
                     (name, name + ".com"))
    conn.commit()
    return conn


def _spend(conn, project: str, started_at: str, usd: float, kind: str = "rank") -> None:
    pid = conn.execute("SELECT id FROM projects WHERE name=?", (project,)).fetchone()["id"]
    conn.execute("INSERT INTO runs(project_id, kind, started_at, finished_at, cost_estimate_usd)"
                 " VALUES(?,?,?,?,?)", (pid, kind, started_at, started_at, usd))
    conn.commit()


class caps:
    """상한 env 를 검사 동안만 세운다(나머지는 지운다)."""

    def __init__(self, **kw):
        self.want = {f"SEOMINER_SPEND_CAP_{k.upper()}_USD": str(v) for k, v in kw.items()}

    def __enter__(self):
        assert set(self.want) <= set(CAP_ENVS), set(self.want) - set(CAP_ENVS)
        self.saved = {n: os.environ.pop(n, None) for n in CAP_ENVS}
        os.environ.update(self.want)

    def __exit__(self, *exc):
        for n, v in self.saved.items():
            os.environ.pop(n, None) if v is None else os.environ.__setitem__(n, v)


def _blocked(conn, project, names, estimate=lambda n, project=None: 0.0):
    return run_all.spend_blocked(conn, project, names, estimate=estimate, now=NOW)


def test_no_cap_blocks_nothing():
    conn = _brain()
    _spend(conn, "a", "2026-11-24T01:00:00Z", 999.0)
    with caps():
        assert run_all.spend_caps() == {}
        assert _blocked(conn, "a", ["rank", "ai"]) == {}
    with caps(site_day="0", tenant_month="엉망"):          # 0·망가진 값 = 상한 없음
        assert run_all.spend_caps() == {}
        assert _blocked(conn, "a", ["rank"]) == {}
    conn.close()


def test_site_daily_cap_counts_only_today_and_only_this_site():
    conn = _brain()
    _spend(conn, "a", "2026-11-23T23:59:59Z", 5.0)     # 어제(UTC) — 오늘 몫이 아니다
    _spend(conn, "a", "2026-11-24T00:00:00Z", 0.6)
    _spend(conn, "a", "2026-11-24T09:00:00Z", 0.3, kind="ai")
    _spend(conn, "b", "2026-11-24T09:00:00Z", 7.0)     # 다른 사이트 — 사이트 상한에 안 든다
    with caps(site_day=1):
        assert _blocked(conn, "a", ["rank", "ai"]) == {}, "$0.90 < $1.00 인데 막았다"
        _spend(conn, "a", "2026-11-24T10:00:00Z", 0.1)
        b = _blocked(conn, "a", ["rank", "ai"])
        assert set(b) == {"rank", "ai"}, b
        assert "$1.00" in b["rank"] and "이 사이트" in b["rank"] and "오늘" in b["rank"], b["rank"]
    conn.close()


def test_monthly_and_tenant_scopes():
    conn = _brain()
    _spend(conn, "a", "2026-10-31T23:00:00Z", 50.0)    # 지난달
    _spend(conn, "a", "2026-11-01T00:00:00Z", 4.0)
    _spend(conn, "b", "2026-11-10T00:00:00Z", 5.0)
    with caps(site_month=5):
        assert _blocked(conn, "a", ["rank"]) == {}, "이 사이트 이번 달 $4 < $5"
    with caps(site_month=4):
        assert "이번 달" in _blocked(conn, "a", ["rank"])["rank"]
    with caps(tenant_month=10):
        assert _blocked(conn, "a", ["rank"]) == {}, "전체 $9 < $10"
    with caps(tenant_month=9):
        b = _blocked(conn, "a", ["rank"])
        assert "전체" in b["rank"] and "$9.00" in b["rank"], b
        assert set(_blocked(conn, "b", ["rank"])) == {"rank"}, "테넌트 상한은 어느 사이트든 막는다"
    with caps(tenant_day=1):
        assert _blocked(conn, "a", ["rank"]) == {}, "오늘은 아무도 안 썼다"
    conn.close()


def test_estimate_is_counted_before_spending():
    """추정 비용을 아는 단계(ai·plays)는 '쓰면 넘는가'로 본다 — 앞 단계 몫을 더해 뒤 단계를 본다."""
    conn = _brain()
    _spend(conn, "a", "2026-11-24T01:00:00Z", 0.5)
    est = lambda n, project=None: {"ai": 0.3, "plays": 0.3}.get(n, 0.0)     # noqa: E731
    with caps(site_day=1):
        b = _blocked(conn, "a", ["rank", "ai", "plays"], est)
        assert set(b) == {"plays"}, f"0.5+0.3 은 되고 0.5+0.3+0.3 은 넘는다: {b}"
        assert "$0.30" in b["plays"], b["plays"]
    with caps(site_day=0.7):
        b = _blocked(conn, "a", ["ai", "plays"], est)
        assert set(b) == {"ai", "plays"}, b
    conn.close()


def test_preflight_applies_the_cap_and_the_skip_is_recorded():
    conn = _brain()
    _spend(conn, "a", db.now(), 2.0)
    saved = (serp_adapter.has_openrouter, serp_adapter.has_dataforseo)
    serp_adapter.has_openrouter = serp_adapter.has_dataforseo = lambda: False     # 잔액 카나리아는 끈다
    try:
        stages = [run_all.STAGE_BY_NAME[n] for n in ("gsc", "rank", "ai")]
        out = io.StringIO()
        with caps(site_day=1), contextlib.redirect_stdout(out):
            b = run_all.preflight(stages, project="a")
        assert set(b) == {"rank", "ai"}, f"유료 단계만 막는다(gsc 는 무료): {b}"
        assert "지출 상한" in out.getvalue(), out.getvalue()
        with caps(), contextlib.redirect_stdout(io.StringIO()):
            assert run_all.preflight(stages, project="a") == {}
        # 건너뜀은 runs 한 줄(중단: 사유)로 남는다 — 화면·출시 문이 그 사유를 그대로 읽는다
        run_all.note_blocked("a", "rank", b["rank"])
        row = conn.execute("SELECT notes, finished_at, cost_estimate_usd FROM runs WHERE kind='rank'"
                           " ORDER BY id DESC LIMIT 1").fetchone()
        v = run_verdict.judge({"notes": row["notes"], "finished_at": row["finished_at"]})
        assert v["aborted"] and v["blocked"] and "지출 상한" in v["reason"], (v, row["notes"])
        assert not row["cost_estimate_usd"], "건너뛴 기록이 지출로 세어진다"
    finally:
        serp_adapter.has_openrouter, serp_adapter.has_dataforseo = saved
        conn.close()


def test_balance_canary_still_wins_its_own_reason():
    """잔액 카나리아가 이미 막은 단계의 사유를 지출 상한이 덮지 않는다(둘 다 걸리면 먼저 안 쪽)."""
    conn = _brain()
    _spend(conn, "a", db.now(), 2.0)
    # OpenRouter 잔액으로 막히는 단계는 이제 plays 다(AI 인용은 2026-11-24 부터 DataForSEO)
    saved = (serp_adapter.has_openrouter, serp_adapter.has_dataforseo, collect_ai.openrouter_ok,
             run_all.paid_estimate)
    serp_adapter.has_openrouter, serp_adapter.has_dataforseo = (lambda: True), (lambda: False)
    collect_ai.openrouter_ok = lambda need=0.0: (False, "OpenRouter 잔액 $0.14")
    run_all.paid_estimate = lambda n, project=None: 0.5
    try:
        with caps(site_day=1), contextlib.redirect_stdout(io.StringIO()):
            b = run_all.preflight([run_all.STAGE_BY_NAME["plays"]], project="a")
        assert b == {"plays": "OpenRouter 잔액 $0.14"}, b
    finally:
        (serp_adapter.has_openrouter, serp_adapter.has_dataforseo, collect_ai.openrouter_ok,
         run_all.paid_estimate) = saved
        conn.close()


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)} passed")
