#!/usr/bin/env python3
"""오늘 화면을 파일로 박제한다 — `dashboard.py --export` 와 같은 일을 하는 이름.

리포트 = 대시보드와 같은 화면 + 그날 데이터가 페이지에 박힌 자립형 HTML.
서버 없이 열리고 남한테 보내도 그대로다. 데이터 수집(gather)과 렌더링(export)은
둘 다 dashboard.py에 있다 — 화면이 갈라지지 않게 한 군데만 둔다.

Contract: 리포트는 Next Actions로 끝난다. Claude가 JSON 파일(문자열 배열)로
써서 --actions로 넘긴다.

Usage:
  python report.py --project NAME [--actions actions.json] [--open]
Output:
  $CAPTURE_HOME/reports/{project}/{YYYY-MM-DD}.html   (경로를 출력)
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import dashboard  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    ap.add_argument("--actions", help="JSON file: list of next-action strings")
    ap.add_argument("--open", action="store_true")
    a = ap.parse_args()

    out = dashboard.export(a.project, a.actions)
    print(f"report: {out}")
    if a.open:
        import webbrowser
        webbrowser.open(out.as_uri())


if __name__ == "__main__":
    main()
