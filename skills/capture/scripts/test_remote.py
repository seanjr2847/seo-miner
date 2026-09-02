#!/usr/bin/env python3
"""호스팅 런이 성공했는지 본다 — 로컬 검사가 못 보는 유일한 자리.

로컬 226개는 "계산이 맞다"를 본다. 호스팅에서 자동 런이 통째로 실패한 것은
(8/30·9/1·9/2 세 번, `runs.notes` 에 `errors=100 updated=0`) 초록불 아래에서
아무도 못 봤다 — 로컬 brain 에는 그 런이 아예 없기 때문이다. 그래서 이 검사만
**진짜 호스팅 서버에 묻는다**.

두 가지를 본다:

1. 지금 런이 도는가. 돌면 FAIL — main push 가 Railway 재배포를 일으키고, 워커는
   subprocess 라 컨테이너와 함께 죽는다. 실제로 오늘 그렇게 런 하나를 죽였다.
2. 마지막 체인이 성공했는가. `errors=N(N>0)`, 유료 단계인데 `api_calls=0` 인데
   건너뜀 표식도 없음, `finished_at IS NULL` — 셋 중 하나라도 있으면 FAIL.

원격 연결(`~/.capture/remote.json`)이 없으면 건너뛴다 — CI·남의 머신에서는
물어볼 서버가 없다. 네트워크 오류·토큰 만료도 FAIL 이 아니다(사유만 찍는다):
비행기 안에서 커밋을 못 하게 만드는 검사는 곧 꺼진다.

HTTP 는 remote.py 의 api() 를 그대로 쓴다 — Bearer·401·오류 해석을 두 벌 만들지
않는다(test_seams 12번이 여기 부르는 /api/* 도 서버에 있는지 함께 본다).

self-check: 가짜 응답 네 가지(도는 중·errors=100·정상·미설정)를 못 박는다.
네트워크는 0회. 실행하면 self-check 를 먼저 돌리고 그 다음 진짜로 묻는다.
"""
import contextlib
import io
import os
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import remote  # noqa: E402

# 마지막 체인 = 가장 최근 gsc 런 이후의 runs 행들. gsc 가 체인의 첫 단계라 그것이
# 경계다. project_id 로 거른다 — 한 brain 에 사이트가 여러 개 산다(서버는 테넌트
# 단위 brain 이고 그 안에 사이트가 여럿이다).
CHAIN_SQL = """SELECT r.kind, r.started_at, r.finished_at, r.api_calls, r.notes
FROM runs r JOIN projects p ON p.id = r.project_id
WHERE p.name = '{name}'
  AND r.started_at >= COALESCE(
        (SELECT MAX(g.started_at) FROM runs g
          WHERE g.project_id = p.id AND g.kind = 'gsc'), '')
ORDER BY r.started_at"""

# 건너뛴 것은 실패가 아니다: 키가 없어 안 돈 단계는 runs 행이 아예 안 생기고,
# "오늘 이미 확인" 은 notes 에 skipped=N 으로 남는다(collector.Stage.skip_note).
SKIP_MARK = re.compile(r"건너뜀|skipped=[1-9]")
ERRORS = re.compile(r"errors=(\d+)")


def paid_stages() -> set[str]:
    """유료 단계 이름. 정본은 run_all.STAGES 의 is_paid — 사본을 안 만든다."""
    import run_all
    return {s.name for s in run_all.STAGES if s.is_paid}


def _q(name: str) -> str:
    """SQL 문자열 리터럴 이스케이프. /api/sql 은 파라미터를 안 받는다(SELECT 전용)."""
    return name.replace("'", "''")


def why_bad(row: dict, paid: set[str]) -> str | None:
    """이 runs 행이 실패인가 — 실패면 사유 한 줄, 아니면 None."""
    notes = row.get("notes") or ""
    if not row.get("finished_at"):
        return "안 끝났다 (finished_at 이 비었다)"
    m = ERRORS.search(notes)
    if m and int(m.group(1)) > 0:
        return f"errors={m.group(1)}"
    if row.get("kind") in paid and not row.get("api_calls") and not SKIP_MARK.search(notes):
        return "유료 단계인데 api_calls=0 (건너뜀 표식도 없다)"
    return None


def running_fails(status: dict) -> list[str]:
    """돌고 있는 런마다 한 줄. 여기서 걸리면 push 를 미루라는 뜻이다."""
    out = []
    for name, s in sorted(status.items()):
        if not (isinstance(s, dict) and s.get("running")):
            continue
        stage, pct = s.get("stage") or "?", s.get("pct")
        where = f"{stage} {pct}%" if pct is not None else stage
        out.append(f"호스팅에서 '{name}' 런이 돌고 있다({where}) — 지금 push 하면 "
                   "재배포가 그 런을 죽인다. 끝난 뒤 다시.")
    return out


def chain_fails(project: str, rows: list[dict], paid: set[str]) -> list[str]:
    out = []
    for r in rows:
        bad = why_bad(r, paid)
        if bad:
            out.append(f"'{project}' {r.get('kind')} ({r.get('started_at')}): {bad} — "
                       f"notes: {(r.get('notes') or '')[:100]}")
    return out


def check() -> tuple[list[str], list[str]]:
    """(FAIL 줄, 알림 줄). 원격이 없거나 못 닿으면 FAIL 없이 알림만."""
    cfg = remote.config()
    if not cfg:
        return [], ["원격 미설정 — 건너뜀 (~/.capture/remote.json 이 없다)"]

    def ask(what, call):
        """_raw 는 거절을 SystemExit 로 낸다 — 확인 불가는 FAIL 이 아니다.

        경로를 여기로 넘기지 않고 **호출을 통째로** 받는 이유는 test_seams 12번이
        `api("GET", "/api/…")` 모양을 소스에서 훑기 때문이다 — 한 겹 감싸 경로가
        이 함수의 인자로 밀려나면 그 이음매 검사가 이 파일을 못 본다.
        """
        try:
            return call(), None
        except (Exception, SystemExit) as e:
            return None, f"확인 불가 — {what}: {e}"

    status, err = ask("호스팅 상태를 못 물었다",
                      lambda: remote.api("GET", "/api/run/status"))
    if err:
        return [], [err]

    fails, notes = running_fails(status), []
    paid = paid_stages()
    for project in cfg.get("projects") or []:
        body = {"project": project, "sql": CHAIN_SQL.format(name=_q(project))}
        rows, err = ask(f"'{project}' 마지막 체인을 못 읽었다",
                        lambda: remote.api("POST", "/api/sql", json=body))
        if err:
            notes.append(err)
            continue
        if not rows:
            notes.append(f"'{project}': 아직 런 기록이 없다")
            continue
        bad = chain_fails(project, rows, paid)
        fails += bad
        if not bad:
            notes.append(f"'{project}': 마지막 체인 {len(rows)}단계 이상 없음 "
                         f"({rows[0].get('started_at')})")
    return fails, notes


# ── 자체점검 ────────────────────────────────────────────────────────────────

def _selfcheck() -> None:
    """가짜 응답으로 네 가지를 못 박는다. 네트워크 0회."""
    saved_home, saved_req = os.environ.get("CAPTURE_HOME"), remote._request
    d = Path(tempfile.mkdtemp(prefix="seo-miner-remote-check-"))
    os.environ["CAPTURE_HOME"] = str(d)

    def serve(status, rows):
        def fake(method, url, **kw):
            if url.endswith("/api/run/status"):
                return remote._Resp(200, status)
            if url.endswith("/api/sql"):
                assert "runs" in kw["json"]["sql"], kw["json"]["sql"]
                return remote._Resp(200, rows)
            raise AssertionError(url)
        remote._request = fake

    ok_rows = [{"kind": "gsc", "started_at": "2026-09-02T01:00:00", "finished_at": "x",
                "api_calls": 1, "notes": "rows=10 errors=0"},
               {"kind": "rank", "started_at": "2026-09-02T01:05:00", "finished_at": "x",
                "api_calls": 38, "notes": "provider=dataforseo errors=0 skipped=0"},
               {"kind": "metrics", "started_at": "2026-09-02T01:09:00", "finished_at": "x",
                "api_calls": 0, "notes": "keywords=40 updated=0 skipped=40 errors=0"}]
    try:
        # (d) 설정이 없으면 건너뜀 — 물어보러 가지도 않는다
        remote._request = lambda *a, **kw: (_ for _ in ()).throw(
            AssertionError("설정도 없는데 네트워크를 탔다"))
        fails, notes = check()
        assert not fails and "건너뜀" in notes[0], (fails, notes)

        remote._save({"url": "https://h.example", "token": "smt_secret",
                      "projects": ["theotherskin"]})

        # (c) 전부 정상 → PASS. api_calls=0 이어도 skipped=40 이면 건너뛴 것이다.
        serve({"theotherskin": {"running": False, "stage": None, "pct": 0}}, ok_rows)
        fails, notes = check()
        assert not fails, fails
        assert "3단계" in notes[0], notes

        # (a) 도는 중 → FAIL (재배포가 런을 죽인다)
        serve({"theotherskin": {"running": True, "stage": "rank", "pct": 38}}, ok_rows)
        fails, _ = check()
        assert len(fails) == 1 and "theotherskin" in fails[0] and "rank 38%" in fails[0], fails
        assert "재배포" in fails[0], fails[0]

        # (b) errors=100 → FAIL, 날짜·단계·notes 앞머리가 줄에 실린다.
        # api_calls 는 살려 둔다 — 아래 '유료인데 0' 규칙이 대신 걸리면 errors 규칙이
        # 죽어도 이 검사가 통과해 버린다(실제로 그렇게 새어 나갔다).
        bad = ok_rows[:1] + [dict(ok_rows[1],
                                  notes="provider=dataforseo errors=100 updated=0")]
        serve({"theotherskin": {"running": False}}, bad)
        fails, _ = check()
        assert len(fails) == 1, fails
        assert ": errors=100 —" in fails[0] and "rank" in fails[0] \
            and "2026-09-02T01:05:00" in fails[0], fails[0]

        # 유료 단계인데 api_calls=0 이고 건너뜀 표식도 없으면 FAIL
        silent = ok_rows[:1] + [dict(ok_rows[2], notes="keywords=40 updated=0 errors=0")]
        serve({"theotherskin": {"running": False}}, silent)
        fails, _ = check()
        assert len(fails) == 1 and "api_calls=0" in fails[0], fails

        # 안 끝난 런도 FAIL — finished_at 이 비면 "수집 이력"이 거짓말을 한다
        hung = ok_rows[:1] + [dict(ok_rows[1], finished_at=None)]
        serve({"theotherskin": {"running": False}}, hung)
        fails, _ = check()
        assert len(fails) == 1 and "안 끝났다" in fails[0], fails

        # 네트워크 오류·401 은 FAIL 이 아니라 '확인 불가'
        remote._request = lambda *a, **kw: remote._Resp(401, {"detail": "로그인이 필요합니다"})
        fails, notes = check()
        assert not fails and "확인 불가" in notes[0], (fails, notes)
        assert "smt_secret" not in " ".join(notes), "토큰이 샜다"
    finally:
        remote._request = saved_req
        if saved_home is None:
            os.environ.pop("CAPTURE_HOME", None)
        else:
            os.environ["CAPTURE_HOME"] = saved_home

    print("  ok  test_remote self-check")


def main() -> int:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):     # 자체점검은 조용히 — 진짜 결과만 읽는다
        _selfcheck()
    print(buf.getvalue(), end="")
    fails, notes = check()
    for ln in notes:
        print("  " + ln)
    for ln in fails:
        print("FAIL  " + ln)
    if fails:
        print(f"\n호스팅 런 {len(fails)}건 — push 전에 이것부터 본다.")
        return 1
    print("호스팅 런 이상 없음")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
