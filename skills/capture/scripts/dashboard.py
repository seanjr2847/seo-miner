#!/usr/bin/env python3
"""로컬 대시보드 — Brain을 브라우저에서 실시간 조회·조작 (stdlib http.server).

report.py(날짜별 정적 아카이브)와 역할이 다르다: 이쪽은 항상 최신 Brain을 읽고,
기회 상태(acked/done/dismissed)를 표에서 클릭으로 갱신한다.
claude-mem 뷰어 구조를 참고하되, 상시 데몬·SSE·프런트 번들러는 들이지 않았다 —
데이터가 수집 스크립트 실행 시에만 바뀌므로 필요할 때 띄우는 단일 프로세스면 된다.
# ponytail: 새로고침 버튼 방식 — 수집을 상시 데몬화하면 그때 SSE 추가

Usage:
  python dashboard.py [--project NAME] [--port 8765] [--open]
"""
import argparse
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).parent))
import db      # noqa: E402
import report  # noqa: E402

HTML = (Path(__file__).parent.parent / "templates" / "dashboard.html").read_bytes()
OPP_STATUSES = ("new", "acked", "done", "dismissed")


def payload(project: str) -> dict:
    conn = db.connect()
    try:
        p = db.get_project(conn, project)
        data = report.gather(conn, p)
        data["project"] = dict(p)
        # 대시보드는 트리아지용이라 리포트의 12개 컷 대신 id 포함 전체를 준다.
        data["opps"] = [dict(r) for r in conn.execute(
            """SELECT id, kind, target, ROUND(score,1) score, reasoning, status,
                      substr(created_at,1,10) created
                 FROM opportunities WHERE project_id=?
                ORDER BY (status='new') DESC, score DESC LIMIT 200""", (p["id"],))]
        data["trend"] = [dict(r) for r in conn.execute(
            """SELECT snapshot_date d, SUM(clicks) clk, SUM(impressions) imp,
                      COUNT(DISTINCT query) q
                 FROM gsc_snapshots WHERE project_id=?
                GROUP BY 1 ORDER BY 1""", (p["id"],))]
        return data
    finally:
        conn.close()


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes,
              ctype: str = "application/json; charset=utf-8") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def do_GET(self) -> None:  # noqa: N802 (http.server 규약)
        u = urlparse(self.path)
        if u.path == "/":
            return self._send(200, HTML, "text/html; charset=utf-8")
        if u.path == "/api/projects":
            conn = db.connect()
            names = [r[0] for r in
                     conn.execute("SELECT name FROM projects ORDER BY name")]
            conn.close()
            return self._json(names)
        if u.path == "/api/data":
            name = parse_qs(u.query).get("project", [""])[0]
            try:
                return self._json(payload(name))
            except SystemExit as e:  # db.get_project는 미등록이면 exit한다
                return self._json({"error": str(e)}, 404)
        return self._send(404, b"not found", "text/plain")

    def do_POST(self) -> None:  # noqa: N802
        if urlparse(self.path).path != "/api/opp":
            return self._send(404, b"not found", "text/plain")
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return self._json({"error": "bad json"}, 400)
        if body.get("status") not in OPP_STATUSES:
            return self._json({"error": f"status must be one of {OPP_STATUSES}"}, 400)
        conn = db.connect()
        cur = conn.execute("UPDATE opportunities SET status=? WHERE id=?",
                           (body["status"], int(body.get("id") or 0)))
        conn.commit()
        updated = cur.rowcount
        conn.close()
        return self._json({"updated": updated})

    def log_message(self, fmt, *args) -> None:  # 요청 로그 소음 제거
        pass


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", help="시작 시 선택할 사이트 (생략하면 첫 번째)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--open", action="store_true")
    a = ap.parse_args()

    # 외부 노출 금지 — 로컬 전용이라 인증이 없다. 바인딩으로 막는다.
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    url = f"http://127.0.0.1:{a.port}/" + (
        f"#{a.project}" if a.project else "")
    print(f"dashboard: {url}  (Ctrl+C로 종료)")
    if a.open:
        import webbrowser
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
