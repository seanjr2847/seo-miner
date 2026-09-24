#!/usr/bin/env python3
"""단계 안의 동시 호출 — 네트워크는 여럿이, Brain 쓰기는 한 줄로.

전체 재기 57분의 대부분은 **기다림**이었다: AI 인용 222회가 한 번에 한 개씩(1회 ~8초),
내 사이트 크롤·페이지 점검도 한 장씩. 응답을 기다리는 동안 다음 요청을 못 보낼
이유가 없다. 다만 SQLite 연결 하나를 여러 스레드가 같이 쓰면 안 된다(db.connect 는
check_same_thread=False 라 막아 주지도 않는다 — 조용히 꼬인다). 그래서 갈라 둔다:

  fetch(item)          → 일꾼 스레드. 네트워크만. conn 을 만지지 않는다.
  write(item, result)  → 부른 스레드(보통 메인). Brain 쓰기는 전부 여기.

쓰기는 **items 순서 그대로** 한다 — 먼저 끝난 응답이 먼저 적히면 같은 입력에 매번
다른 순서로 행이 쌓이고, 진행 표시·오류 목록도 런마다 뒤섞인다. 순서·오류 집계·
항목마다 commit·Fatal 즉시 중단은 전부 Stage.each 가 그대로 한다(여기서 두 벌로
다시 쓰지 않는다) — fetch 에서 난 예외는 write 차례에 그 항목의 예외로 다시 올라온다.

동시 개수의 정본은 LIMITS 한 곳이다(수집기마다 숫자를 따로 두면 한쪽만 바뀐다).
"""
from __future__ import annotations

import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import collector  # noqa: E402

# 제공자별 동시 상한 — 문서의 한도가 아니라 **우리가 쓰는 몫**이다.
#   openrouter : AI 인용. 429 는 항목 하나의 실패로 세고(다음 런이 그 항목만 다시 묻는다)
#                계속 간다 — 6 이면 1회 ~8초짜리 222회가 ~5분이다.
#   own_site   : 내 사이트(크롤·페이지 점검). 남의 서버가 아니라도 작은 호스팅은 동시
#                요청에 약하다 — 한 호스트에 4개 넘게 동시에 걸지 않는다.
#   dataforseo : Labs·Backlinks. 문서상 동시 30·분당 2000 이지만 계정 하나를 여러
#                사이트·워커가 같이 쓴다 — 한 단계가 5 를 넘기지 않는다.
#                (Google Ads 검색량은 분당 12 제한이라 여기 없다 — serp_adapter 가 간격을 둔다)
LIMITS = {"openrouter": 6, "own_site": 4, "dataforseo": 5}


class _Stopped(Exception):
    """앞선 항목이 Fatal 로 단계를 끝냈다 — 아직 시작 안 한 호출은 보내지 않는다."""


def each(st, items, fetch, write, *, workers: int, label=None) -> int:
    """items 를 workers 개씩 동시에 fetch 하고, 결과는 items 순서대로 write 한다.

    반환값은 Stage.each 와 같다 — 예외 없이 write 까지 끝난 항목 수.

    throttle(st.throttle)은 **일꾼마다** 제 호출 뒤에 쉰다. 순차일 때의 "요청 사이
    간격"을 그대로 옮긴 것이다 — 메인에서 쉬면 이미 다 받아 둔 결과를 적는 데만
    시간을 버린다. 그래서 적는 동안은 st.throttle 을 잠시 0 으로 둔다.

    Fatal(잔액·인증)은 한 번 나면 아직 안 보낸 호출을 보내지 않는다 — 순차일 때
    "첫 402 에서 멈춘다"와 같은 뜻을 지키려는 것이다. 이미 떠난 호출(최대 workers 개)
    은 돌아오길 기다린다.
    """
    items = list(items)
    if not items:
        return 0
    stop = threading.Event()
    throttle = st.throttle

    def run(item):
        if stop.is_set():
            raise _Stopped("앞 항목의 치명적 오류로 보내지 않았습니다")
        try:
            return fetch(item)
        except collector.Fatal:
            stop.set()
            raise
        finally:
            if throttle:
                time.sleep(throttle)

    pool = ThreadPoolExecutor(max_workers=max(1, min(int(workers), len(items))),
                              thread_name_prefix="seo-miner-fanout")
    try:
        futs = [pool.submit(run, it) for it in items]
        st.throttle = 0.0
        return st.each(list(zip(items, futs)),
                       lambda pair: write(pair[0], pair[1].result()),
                       label=lambda pair: label(pair[0]) if label else pair[0])
    finally:
        st.throttle = throttle
        stop.set()
        pool.shutdown(wait=True, cancel_futures=True)


class MainThreadOnly:
    """conn 대역 — 부른 스레드가 아닌 곳에서 만지면 바로 터진다(자체점검용).

    db.connect 는 check_same_thread=False 라 일꾼이 conn 을 써도 sqlite3 가 안 막는다.
    그러니 "일꾼은 안 쓴다" 는 검사로 못 박아야 한다 — 수집기 자체점검이 collect(conn=…)
    에 이걸 넣는다.
    """

    def __init__(self, conn):
        object.__setattr__(self, "_conn", conn)
        object.__setattr__(self, "_owner", threading.get_ident())

    def _check(self):
        if threading.get_ident() != self._owner:
            raise AssertionError(
                f"Brain 쓰기가 일꾼 스레드({threading.current_thread().name})에서 났다")

    def __getattr__(self, name):
        attr = getattr(self._conn, name)
        if callable(attr):
            def guarded(*a, **kw):
                self._check()
                return attr(*a, **kw)
            return guarded
        return attr

    def __setattr__(self, name, value):
        setattr(self._conn, name, value)

    def __enter__(self):
        self._check()
        return self._conn.__enter__()

    def __exit__(self, *exc):
        return self._conn.__exit__(*exc)


def _selfcheck() -> None:
    import contextlib
    import io
    import os
    import sqlite3
    import tempfile

    import db

    os.environ["CAPTURE_HOME"] = str(Path(tempfile.mkdtemp(prefix="seo-miner-fanout-selftest-")))
    boot = db.connect()
    boot.execute("INSERT INTO projects(name, domain, locale) VALUES('fo','fo.kr','ko-KR')")
    boot.commit()
    boot.close()

    main = threading.get_ident()
    lock = threading.Lock()
    live = {"now": 0, "peak": 0}

    def fetch(i):
        with lock:
            live["now"] += 1
            live["peak"] = max(live["peak"], live["now"])
        # 앞 항목일수록 늦게 끝난다 — 끝난 순서대로 적으면 순서가 뒤집힌다
        time.sleep(0.02 * (10 - i))
        with lock:
            live["now"] -= 1
        if i == 3:
            raise collector.ItemFailed("3번 죽음", status=500)
        return (i, threading.get_ident())

    # 1. 결과는 items 순서, 쓰기는 부른 스레드, 동시 개수는 상한 안, 실패는 그 항목만
    got, err = [], io.StringIO()
    with collector.stage("fo") as st:
        st.throttle = 0.25

        def write(i, res):
            assert threading.get_ident() == main, "쓰기가 일꾼 스레드에서 났다"
            assert res[1] != main, "fetch 가 일꾼이 아니라 메인에서 돌았다"
            got.append(res[0])

        t0 = time.monotonic()
        with contextlib.redirect_stderr(err):
            done = each(st, range(10), fetch, write, workers=4, label=lambda i: f"#{i}")
        took = time.monotonic() - t0
        assert st.throttle == 0.25, "throttle 을 되돌리지 않았다"
        assert done == 9 and st.errors == 1, (done, st.errors)
        assert st.failures[0].item == "#3" and st.failures[0].status == 500, st.failures
    assert got == [0, 1, 2, 4, 5, 6, 7, 8, 9], f"적는 순서가 입력 순서가 아니다: {got}"
    assert 1 < live["peak"] <= 4, f"동시 상한을 안 지킨다(또는 순차다): peak={live['peak']}"
    # 순차면 fetch 합 1.1초 + throttle 10×0.25 = 3.6초. 4 동시면 1.5초 안쪽이다
    assert took < 2.0, f"동시에 안 돌았다: {took:.2f}s"

    # 2. Fatal — 한 번 나면 아직 안 보낸 호출은 안 보낸다(순차의 "첫 402 에서 멈춤")
    sent = []

    def fatal_fetch(i):
        with lock:
            sent.append(i)
        if i == 1:
            raise collector.Fatal("잔액 없음(402)")
        time.sleep(0.05)
        return i

    with collector.stage("fo") as st:
        st.throttle = 0.0
        wrote = []
        try:
            each(st, range(40), fatal_fetch, lambda i, r: wrote.append(r), workers=2)
            raise AssertionError("Fatal 을 삼켰다")
        except collector.Fatal as e:
            assert "402" in str(e), e
    assert wrote == [0], wrote
    assert len(sent) <= 4, f"Fatal 뒤로도 호출을 계속 보냈다: {sent}"

    # 3. MainThreadOnly — 일꾼이 conn 을 만지면 잡힌다
    raw = sqlite3.connect(":memory:", check_same_thread=False)
    guard = MainThreadOnly(raw)
    guard.execute("SELECT 1")
    box = []
    t = threading.Thread(target=lambda: box.append(_try(lambda: guard.execute("SELECT 1"))))
    t.start()
    t.join()
    assert box == ["AssertionError"], box
    print("fanout self-check ok")


def _try(fn) -> str:
    try:
        fn()
        return "ok"
    except Exception as e:
        return type(e).__name__


if __name__ == "__main__":
    _selfcheck()
