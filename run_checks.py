#!/usr/bin/env python3
"""리포 전체 검사 진입점을 한 번에 돌린다. stdlib 만 쓴다.

  python run_checks.py            # 전부
  python run_checks.py collector  # 이름에 문자열이 든 것만

찾는 것: test_*.py, demo() 가 있는 server/*.py, _selfcheck() 가 있는 스크립트,
그리고 문법 경고(-W error::SyntaxWarning)와 node --check.
하나라도 실패하면 종료코드 1.
"""
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv"}


def py_files() -> list[Path]:
    return sorted(p for p in ROOT.rglob("*.py")
                  if not SKIP_DIRS & set(p.relative_to(ROOT).parts))


def discover() -> list[tuple[str, list[str]]]:
    """(라벨, argv) 목록. 진입점 관례를 소스에서 읽어 인자를 정한다."""
    out = []
    for p in py_files():
        src = p.read_text(encoding="utf-8", errors="replace")
        rel = p.relative_to(ROOT).as_posix()
        if p.name.startswith("test_"):   # 모듈 최상위에서 도는 것도 있어 __main__ 은 안 본다
            out.append((rel, [sys.executable, str(p)]))
        elif "__main__" in src and re.search(r"^def (demo|_selfcheck)\(", src, re.M):
            # 자체점검을 어떻게 부르는지: --selfcheck 플래그 > selfcheck 서브명령 > 무인자
            if "--selfcheck" in src:
                args = ["--selfcheck"]
            elif re.search(r'==\s*"selfcheck"', src):
                args = ["selfcheck"]
            else:
                args = []
            out.append((rel + (" " + " ".join(args) if args else ""),
                        [sys.executable, str(p), *args]))
    for js in sorted((ROOT / "server" / "assets").glob("*.js")):
        out.append((js.relative_to(ROOT).as_posix(), ["node", "--check", str(js)]))
    # 화면 스크립트는 조립된 뒤에야 한 덩어리가 된다 — 뷰마다 따로 보면 못 잡는 것이 있다.
    out.append(("dashboard 조립본 JS",
                [sys.executable, str(ROOT / "run_checks.py"), "--check-dashboard-js"]))
    out.append(("SyntaxWarning (compile all)",
                [sys.executable, "-W", "error::SyntaxWarning", str(ROOT / "run_checks.py"),
                 "--compile-only"]))
    return out


def check_dashboard_js() -> int:
    """조립된 대시보드의 <script> 를 전부 이어 붙여 node 로 파싱한다.

    이어 붙이는 이유는 브라우저가 그렇게 보기 때문이다 — 서로 다른 <script> 라도
    top-level let 은 같은 전역 렉시컬 환경에 들어간다. 뷰마다 따로 검사하면
    같은 이름을 두 화면이 선언한 것도, 문자열 안에 개행이 박힌 것도 못 잡는다.
    """
    import shutil
    import tempfile
    if not shutil.which("node"):
        print("node 가 없어 건너뜁니다")
        return 0
    sys.path.insert(0, str(ROOT / "skills" / "capture" / "scripts"))
    os.environ.setdefault("CAPTURE_HOME", tempfile.mkdtemp())
    import dashboard                       # noqa: E402 (경로를 먼저 세운다)
    # 호스팅은 이 조립본 뒤에 애드온(server/assets/dash.html)을 얹어서 낸다 —
    # 브라우저가 보는 한 덩어리는 그것까지다. 빼고 보면 애드온의 문법 오류도,
    # 셸과 이름이 겹치는 top-level 선언도 배포에 가서야 드러난다.
    src = dashboard.HTML.decode("utf-8")
    addon = ROOT / "server" / "assets" / "dash.html"
    if addon.exists():
        src += "\n" + addon.read_text(encoding="utf-8")
    blocks = re.findall(r"<script>\s*(.*?)</script>", src, re.S)
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "assembled.js"
        f.write_text("\n;\n".join(blocks), encoding="utf-8")
        r = subprocess.run(["node", "--check", str(f)], capture_output=True,
                           text=True, encoding="utf-8", errors="replace")
    if r.returncode:
        print((r.stderr or "").strip()[:1500])
        return 1
    print(f"조립본 {len(blocks)}블록 파싱 OK")
    return 0


def compile_only() -> int:
    """모든 .py 를 compile 한다 — SyntaxWarning 이 -W 로 에러가 되어 잡힌다."""
    bad = 0
    for p in py_files():
        try:
            compile(p.read_text(encoding="utf-8"), str(p), "exec")
        except (SyntaxError, SyntaxWarning) as e:
            print(f"{p.relative_to(ROOT).as_posix()}: {e}")
            bad = 1
    return bad


def run(label: str, argv: list[str]) -> tuple[bool, str, float]:
    t0 = time.monotonic()
    try:
        r = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=300)
    except FileNotFoundError:
        return True, f"SKIP  {argv[0]} 없음", 0.0   # node 미설치 등은 실패로 안 친다
    except subprocess.TimeoutExpired:
        return False, "300초 초과", time.monotonic() - t0
    ok = r.returncode == 0
    tail = [ln for ln in (r.stdout + r.stderr).splitlines() if ln.strip()]
    return ok, "\n".join(tail[-12:]) if not ok else "", time.monotonic() - t0


def main() -> int:
    if "--compile-only" in sys.argv:
        return compile_only()
    if "--check-dashboard-js" in sys.argv:
        return check_dashboard_js()
    want = [a for a in sys.argv[1:] if not a.startswith("-")]
    checks = [c for c in discover() if not want or any(w in c[0] for w in want)]
    fails = []
    for label, argv in checks:
        ok, detail, secs = run(label, argv)
        skipped = detail.startswith("SKIP")
        print(f"{'SKIP' if skipped else 'PASS' if ok else 'FAIL'}  {label}  ({secs:.1f}s)")
        if not ok:
            fails.append(label)
            print("\n".join("      " + ln for ln in detail.splitlines()))
    print(f"\n{len(checks) - len(fails)}/{len(checks)} PASS")
    if fails:
        print("실패: " + ", ".join(fails))
    return 1 if fails else 0


if __name__ == "__main__":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")   # Windows cp949 방어
    sys.exit(main())
