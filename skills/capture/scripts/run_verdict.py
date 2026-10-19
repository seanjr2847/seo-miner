#!/usr/bin/env python3
"""런 한 건의 결과 판정 — 성공 · 항목 오류 · 건너뜀 · 중단 · 미완을 가르는 **유일한** 곳.

판정의 재료는 runs 행의 `notes`(수집기가 남긴 사람용 설명)와 `finished_at` 이다. 예전에는
이 재료를 읽는 자리가 여럿이었다 — 점검(audit_data)·묶음 머리(dashboard._run_ok)·출시 문
(test_remote)·AI 회차(scoring)·셸(runVerdict) 이 각자 `errors=N`·`중단:` 을 되짚었고, 한 번은
부분 일치(`outline_errors=4`)가 실패로 읽힐 뻔했다. 이제 규칙은 여기 한 벌이다:

  judge(row) -> {state, cls, label, why, errors, skipped, aborted, unfinished, failed,
                 blocked, skip_marked, reason}

  state  aborted(중단) > unfinished(미완) > errors(항목 오류) > skipped(건너뜀) > ok
  cls    화면 한 벌: bad(aborted·unfinished·errors) · skip · ok
  failed notes 만 보는 실패(중단 또는 errors>0) — 끝났는지는 부르는 쪽이 따로 본다
  reason "중단:" 뒤의 사유(없으면 "")

화면은 이 판정을 **페이로드로 받는다**: dashboard.gather 가 runs 행마다 `verdict` 를 싣는다.
셸의 runVerdict 는 그걸 돌려주기만 한다 — 셸이 notes 를 다시 읽지 않는다.

스키마에 새 칸을 만들지 않은 이유: 호스팅 DB 와 이미 쌓인 옛 행에는 칸이 없고, 배포 시차 동안
로컬이 /api/sql 로 읽는 runs 에도 없을 수 있다 — 칸을 만들어도 notes 폴백이 늘 남아야 하니
진실의 원본이 둘이 된다. 쓰는 쪽은 표식을 이 모듈의 함수(abort_note)·상수로 짓고, 읽는 쪽은 judge
하나를 부른다. 글자 모양이 쓰는 쪽과 읽는 쪽에서 갈라지는 것은 test_seams 193번이 지킨다.

stdlib 만 쓴다(서버·플러그인 둘 다 이 파일을 들인다). 자체점검: python run_verdict.py --selfcheck
"""
from __future__ import annotations

import re
import sys

# ── 쓰는 쪽이 짓는 표식 ──────────────────────────────────────────────
ABORT_MARK = "중단:"                         # db.run 이 예외로 닫은 런 · 서버가 회수한 런 · 막힌 런
BLOCKED_NOTE = "돈 쓰기 전에 건너뜀"          # run_all.note_blocked — 카나리아가 막은 유료 단계


def abort_note(reason: str) -> str:
    """중단 사유 한 토막 — notes 에 `… | 중단: <사유>` 로 붙는다."""
    return f"{ABORT_MARK} {reason}"


def sql_not_aborted(col: str) -> str:
    """SQL 조각: 이 notes 칸에 중단 표식이 없다. AI 회차 판정이 SQL 안에서 쓴다(judge 와 같은 표식)."""
    return f"instr(COALESCE({col},''), '{ABORT_MARK}')=0"


# ── 읽는 규칙 ────────────────────────────────────────────────────────
# \b 가 있어야 한다: rank 런은 `outline_errors=4` 를, 수집기는 `gone_skipped=N` 을 남기는데 경계가
# 없으면 그 꼬리를 errors·skipped 로 읽는다. crawl 의 `issues=401` 도 HTTP 401 이 아니다 — 숫자로
# 실패를 짐작하지 않고 이 표식들만 본다.
_ERRORS = re.compile(r"\berrors=(\d+)")
_SKIPPED = re.compile(r"\bskipped=(\d+)")
# 유료 단계가 api_calls=0 으로 끝난 게 '건너뜀'이었는지 가리는 표식(출시 문이 쓴다).
_SKIP_MARKED = re.compile(r"건너뜀|skipped=[1-9]")


def judge(row) -> dict:
    """runs 행(dict 또는 sqlite3.Row) 하나 → 판정. 칸이 없으면 없는 대로 읽는다.

    notes 가 없으면 빈 글자, finished_at 칸이 아예 없는 행(notes 만 고른 옛 질의)은 '끝났다'로 본다
    — 끝났는지를 아는 쪽이 칸을 넘겨야 미완이 잡힌다. 칸이 있고 비었으면 미완이다.
    """
    keys = row.keys()
    notes = str((row["notes"] if "notes" in keys else None) or "")
    unfinished = "finished_at" in keys and not row["finished_at"]
    em, sm = _ERRORS.search(notes), _SKIPPED.search(notes)
    errors, skipped = (int(em.group(1)) if em else 0), (int(sm.group(1)) if sm else 0)
    cut = notes.find(ABORT_MARK)
    aborted = cut >= 0
    if aborted:
        state, cls, label, why = "aborted", "bad", "중단", "도중에 예외로 멈췄습니다."
    elif unfinished:
        state, cls, label = "unfinished", "bad", "미완"
        why = "끝난 기록이 없습니다. 프로세스가 도중에 죽었을 수 있습니다."
    elif errors:
        state, cls, label, why = "errors", "bad", f"오류 {errors}", f"{errors}건이 실패했습니다."
    elif skipped:
        state, cls, label = "skipped", "skip", f"건너뜀 {skipped}"
        why = f"{skipped}건을 건너뛰었습니다. 보통 한도·키 없음입니다."
    else:
        state, cls, label, why = "ok", "ok", "완료", "끝까지 돌았고 오류가 없습니다."
    return {"state": state, "cls": cls, "label": label, "why": why,
            "errors": errors, "skipped": skipped,
            "aborted": aborted, "unfinished": unfinished,
            "failed": aborted or errors > 0,
            "blocked": BLOCKED_NOTE in notes,
            "skip_marked": bool(_SKIP_MARKED.search(notes)),
            "reason": notes[cut + len(ABORT_MARK):].strip() if aborted else ""}


def ok(row) -> bool:
    """이 런이 '잘 끝났다'인가 — 건너뜀은 실패가 아니다. 중단·미완·항목 오류만 아니다."""
    return judge(row)["cls"] != "bad"


def attach(runs) -> None:
    """runs 행 목록의 각 행에 `verdict` 를 단다. 이미 있으면(서버가 단 것) 그대로 둔다.

    페이로드를 짓는 곳(dashboard.gather)과, 호스팅이 아직 옛 판이라 verdict 없이 보낸 페이로드를
    로컬이 프록시로 받는 곳이 같은 함수를 부른다.
    """
    for r in runs or ():
        if isinstance(r, dict) and "verdict" not in r:
            r["verdict"] = judge(r)


def _selfcheck() -> None:
    j = judge
    assert j({"notes": "errors=0 outlines=36 outline_errors=4", "finished_at": "t"})["state"] == "ok"
    assert j({"notes": "outline_errors=4", "finished_at": "t"})["errors"] == 0
    assert j({"notes": "ai_errors=3", "finished_at": "t"})["state"] == "ok"
    v = j({"notes": "urls=5 errors=9 first_error=HTTP 400", "finished_at": "t"})
    assert (v["state"], v["errors"], v["label"], v["cls"]) == ("errors", 9, "오류 9", "bad"), v
    assert j({"notes": "errors=0 skipped=2", "finished_at": "t"})["label"] == "건너뜀 2"
    assert j({"notes": "gone_skipped=4", "finished_at": "t"})["skipped"] == 0
    v = j({"notes": f"errors=1 | {abort_note('Fatal: 402')}", "finished_at": "t"})
    assert v["state"] == "aborted" and v["failed"] and v["reason"] == "Fatal: 402", v
    assert j({"notes": "x", "finished_at": None})["state"] == "unfinished"
    assert j({"notes": "errors=2", "finished_at": None})["failed"], "미완이어도 notes 실패는 실패다"
    assert j({"notes": "x", "finished_at": None})["failed"] is False
    assert j({"notes": "errors=3"})["state"] == "errors" and j({})["state"] == "ok"
    assert j({"notes": f"{BLOCKED_NOTE} | {abort_note('잔액')}", "finished_at": "t"})["blocked"]
    assert j({"notes": "건너뜀 — 오늘 이미 확인"})["skip_marked"] and not j({"notes": "errors=0"})["skip_marked"]
    assert ok({"notes": "skipped=1", "finished_at": "t"}) and not ok({"notes": "중단: x", "finished_at": "t"})
    rs = [{"notes": "errors=1", "finished_at": "t"}, {"notes": "", "finished_at": "t", "verdict": "keep"}]
    attach(rs)
    assert rs[0]["verdict"]["errors"] == 1 and rs[1]["verdict"] == "keep"
    print("  ok  run_verdict self-check")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if "--selfcheck" in sys.argv[1:]:
        _selfcheck()
