"""주기 실행 — 잴 게 있으면 워커를 띄운다.

동사는 둘이다: due() 가 지금 잴 사이트 수를 세고, dispatch() 가 워커 프로세스를 띄운다.
웹 프로세스 안에서 직접 수집하지 않는 이유는 하나다 — store.tenant() 가 갈아끼우는 전역
os.environ 을 동시에 들어온 웹 요청이 같이 본다(남의 테넌트). 그래서 subprocess 다.
Railway 볼륨은 서비스 하나에만 붙으므로 크론을 별도 서비스로 뺄 수도 없다 — 뺀 쪽은
이 서비스의 /data 를 못 본다.
"""
from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

import settings
import store

ROOT = Path(__file__).resolve().parent.parent
WORKER = str(ROOT / "server" / "worker.py")

# 60초 틱 — 등록 직후 첫 측정이 곧바로 잡혀야 온보딩이 성립한다. 잴 게 없으면 DB 조회
# 한 번으로 끝나므로 틱이 잦아도 싸다.
TICK = 60.0
SWEEP_TIMEOUT = 3 * 3600


def every_hours() -> float:
    """0 이면 자동 재측정을 끈다(첫 측정은 store.due_sites 가 따로 잡는다).
    호출 시점에 읽는다 — 얼려두면 env 를 바꿔도 안 먹는다."""
    return settings.num("SEOMINER_RUN_EVERY_HOURS")


def due() -> int:
    """지금 잴 사이트 수. 주기 판정은 사이트별로 DB 가 한다 — 전역 스탬프 파일을 쓰면
    먼저 등록한 사이트가 도장을 찍어서 방금 가입한 사람의 첫 측정이 통째로 밀렸다."""
    conn = store.connect()
    try:
        return len(store.due_sites(conn, every_hours=every_hours()))
    finally:
        conn.close()


def dispatch(*args: str) -> None:
    """워커를 띄우고 곧바로 돌아온다. 실패는 삼킨다 — 다음 틱이 잡는다."""
    try:
        subprocess.Popen([sys.executable, WORKER, *args], cwd=str(ROOT))
    except Exception as e:
        print(f"[worker] 실행 실패: {e}", flush=True)


def kick() -> None:
    """등록 직후 곧바로 수집을 띄운다 — 틱을 기다리면 그동안 화면이 비어 있다.
    워커가 사이트마다 시작 전에 도장을 찍으므로 스케줄러 틱과 겹쳐도 중복되지 않는다."""
    dispatch("--all")


def sweep() -> None:
    """워커가 끝날 때까지 기다린다 — 안 기다리면 다음 틱이 두 벌째를 띄운다."""
    subprocess.run([sys.executable, WORKER, "--all"],
                   cwd=str(ROOT), timeout=SWEEP_TIMEOUT)


async def loop(tick: float = TICK) -> None:
    while True:
        await asyncio.sleep(tick)
        try:
            if await asyncio.to_thread(due):
                await asyncio.to_thread(sweep)
        except Exception as e:          # 여기서 죽으면 자동 수집이 조용히 멈춘다
            print(f"[scheduler] 실패: {e}", flush=True)


def demo() -> None:
    import os
    import tempfile
    from cryptography.fernet import Fernet

    saved = {n: os.environ.get(n) for n in
             ("SEOMINER_DATA", "SEOMINER_SECRET_KEY", "SEOMINER_RUN_EVERY_HOURS")}
    real_popen, real_run = subprocess.Popen, subprocess.run
    try:
        with tempfile.TemporaryDirectory() as d:
            os.environ["SEOMINER_DATA"] = d
            os.environ["SEOMINER_SECRET_KEY"] = Fernet.generate_key().decode()
            os.environ.pop("SEOMINER_RUN_EVERY_HOURS", None)

            assert due() == 0, "사이트가 없는데 잴 게 있다고 한다"
            conn = store.connect()
            uid = store.upsert_user(conn, "sched@example.com")
            store.add_site(conn, uid, "p1", "sc-domain:p1.com", "p1.com")
            conn.close()
            assert due() == 1, "등록 직후인데 첫 측정이 안 잡힌다"
            os.environ["SEOMINER_RUN_EVERY_HOURS"] = "0"
            assert due() == 1, "0 은 자동 재측정만 꺼야 한다 — 첫 측정까지 막혔다"
            os.environ["SEOMINER_RUN_EVERY_HOURS"] = "일주일"
            assert every_hours() == 168.0, "망가진 값에 기본값으로 안 떨어진다"

            # dispatch: 워커를 띄우기만 한다. 실패해도 예외가 새어 나오면 안 된다.
            seen = []
            subprocess.Popen = lambda cmd, **kw: seen.append(cmd)
            kick()
            assert seen and seen[0][-1] == "--all" and seen[0][1] == WORKER, seen
            subprocess.Popen = lambda cmd, **kw: (_ for _ in ()).throw(OSError("no"))
            dispatch("--all")           # 여기서 터지면 등록이 통째로 실패한다

            # 틱: 잴 게 있을 때만 워커를 기다린다.
            swept = []
            subprocess.run = lambda cmd, **kw: swept.append(cmd)

            async def one_tick():
                t = asyncio.create_task(loop(0.01))
                # 벽시계로 기다리면 부하 걸린 머신에서 틱이 한 번도 스케줄되지 못해
                # 헛되이 실패한다(12회 중 1회 재현). 조건이 설 때까지 기다리되 상한을
                # 둔다 — 정말 안 도는 거면 그때 아래 단언이 잡는다.
                for _ in range(500):
                    if swept:
                        break
                    await asyncio.sleep(0.01)
                t.cancel()

            asyncio.run(one_tick())
            assert swept, "잴 게 있는데 틱이 워커를 안 띄웠다"

        print("scheduler: ok")
    finally:
        subprocess.Popen, subprocess.run = real_popen, real_run
        for n, v in saved.items():
            os.environ.pop(n, None) if v is None else os.environ.__setitem__(n, v)


if __name__ == "__main__":
    demo()
