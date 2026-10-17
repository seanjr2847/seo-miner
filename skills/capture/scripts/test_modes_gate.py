#!/usr/bin/env python3
"""근거 소카드의 재료(intent_splits·pseo_groups)를 싣는 문 — 열린 기회가 아니라 '그 종류의 기회가 한 번이라도
있었나'를 본다. 열린 기회(opps)만 보면 끝낸 순간 [자세히 보기]의 근거 소카드가 접힌다."""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dashboard  # noqa: E402


def _conn(rows):
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE opportunities(id INTEGER PRIMARY KEY, project_id INT, kind TEXT, target TEXT, status TEXT)")
    c.executemany("INSERT INTO opportunities(project_id,kind,target,status) VALUES(?,?,?,?)", rows)
    return c


def test_gate_ignores_status():
    c = _conn([(1, "pseo_pattern", "a", "done"), (1, "intent_split", "b", "dismissed")])
    assert dashboard._ever_had_kind(c, 1, "pseo_pattern"), "끝낸 기회만 있어도 근거는 남아야 한다"
    assert dashboard._ever_had_kind(c, 1, "intent_split")


def test_gate_is_per_project_and_kind():
    c = _conn([(1, "pseo_pattern", "a", "new")])
    assert not dashboard._ever_had_kind(c, 2, "pseo_pattern"), "남의 사이트 기회로 무거운 재료를 싣는다"
    assert not dashboard._ever_had_kind(c, 1, "intent_split"), "다른 종류 기회로 싣는다"


if __name__ == "__main__":
    n = 0
    for k, f in sorted(globals().items()):
        if k.startswith("test_") and callable(f):
            f(); n += 1; print("  ok ", k)
    print(f"{n} passed")
