#!/usr/bin/env python3
"""로컬 대시보드 — Brain을 브라우저에서 실시간 조회·조작 (stdlib http.server).

화면은 하나다. 두 가지 모드로 쓴다:
  · 라이브(기본)  — 서버가 Brain을 그때그때 읽어준다. 기회 트리아지·설정이 된다.
  · 박제(--export) — 그 시점 데이터를 페이지 안에 박아 넣은 자립형 HTML 파일.
    서버도 인터넷도 필요 없고 남한테 보내도 그대로 열린다(= 예전 report.py).
같은 템플릿을 쓰므로 화면이 갈라지지 않는다.
claude-mem 뷰어 구조를 참고하되, 상시 데몬·SSE·프런트 번들러는 들이지 않았다 —
데이터가 수집 스크립트 실행 시에만 바뀌므로 필요할 때 띄우는 단일 프로세스면 된다.
# ponytail: 새로고침 버튼 방식 — 수집을 상시 데몬화하면 그때 SSE 추가

Usage:
  python dashboard.py [--project NAME] [--port 8765] [--open]
  python dashboard.py --export --project NAME [--actions actions.json] [--open]
"""
import argparse
import json
import os
import re
import secrets
import subprocess
import sys
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).parent))
SETUP_SCRIPTS = Path(__file__).resolve().parents[2] / "setup" / "scripts"
sys.path.insert(0, str(SETUP_SCRIPTS))
import db      # noqa: E402
import doctor  # noqa: E402  (setup 스킬의 진단 — 대시보드 상단 배너용)
import report  # noqa: E402

HTML = (Path(__file__).parent.parent / "templates" / "dashboard.html").read_bytes()
OPP_STATUSES = ("new", "acked", "done", "dismissed")

# 설정 화면이 실행할 수 있는 명령은 이 셋뿐 — 사용자 입력이 명령줄에 섞이지 않는다.
ACTIONS = {
    "deps": [sys.executable, "-m", "pip", "install", "requests", "pyyaml"],
    "deps_gsc": [sys.executable, "-m", "pip", "install", "google-api-python-client",
                 "google-auth"],
    "gsc": [sys.executable, str(SETUP_SCRIPTS / "connect_gsc.py")],
}
KEY_FIELDS = ("OPENROUTER_API_KEY", "SERPER_API_KEY",
              "DATAFORSEO_LOGIN", "DATAFORSEO_PASSWORD")
PROJECT_TYPES = ("game", "local_clinic", "saas", "directory")
ENV_FILE = db.CAPTURE_HOME / "env"

# 로컬 전용이라 인증이 없다. 그런데 브라우저는 아무 웹페이지에서나 127.0.0.1로 POST를
# 보낼 수 있어서(pip 실행·파일 쓰기 엔드포인트가 생겼으므로) 1회용 토큰으로 막는다.
TOKEN = secrets.token_urlsafe(9)


def export(project: str, actions_file: str | None = None) -> Path:
    """그 시점 데이터를 페이지에 박아 넣은 자립형 HTML을 reports/에 남긴다.
    라이브 화면과 같은 템플릿이다 — 페이지가 window.__SNAPSHOT__을 보면 fetch 대신
    그걸 그리고, 손댈 수 없는 기록이므로 트리아지·설정 버튼을 숨긴다."""
    data = payload(project)
    actions = []
    if actions_file and Path(actions_file).exists():
        actions = json.loads(Path(actions_file).read_text(encoding="utf-8"))
    snap = {"data": data, "actions": actions, "exported": str(date.today())}
    blob = json.dumps(snap, ensure_ascii=False).replace("</", "<\\/")  # </script> 차단
    html = HTML.decode("utf-8").replace(
        "<!--SNAPSHOT-->", f"<script>window.__SNAPSHOT__={blob}</script>", 1)
    out = db.CAPTURE_HOME / "reports" / project / f"{date.today()}.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    return out


def run_action(name: str) -> dict:
    try:
        p = subprocess.run(ACTIONS[name], capture_output=True, timeout=600,
                           encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return {"ok": False, "log": "10분이 지나도 안 끝나 중단했습니다."}
    return {"ok": p.returncode == 0, "log": ((p.stdout or "") + (p.stderr or ""))[-4000:]}


def save_keys(values: dict) -> dict:
    """셸 rc 편집 대신 ~/.capture/env 에 모은다 (db.load_env/doctor.load_env가 읽는다).
    빈 값으로 보내면 그 키를 지운다."""
    cur = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text("utf-8").splitlines():
            k, sep, v = line.partition("=")
            if sep:
                cur[k.strip()] = v.strip()
    for k in KEY_FIELDS:
        if k not in values:
            continue
        v = str(values[k]).strip()
        if v:
            cur[k] = os.environ[k] = v   # 지금 세션의 doctor 진단에 바로 반영
        else:
            cur.pop(k, None)
            os.environ.pop(k, None)
    ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
    ENV_FILE.write_text("".join(f"{k}={v}\n" for k, v in cur.items() if v), "utf-8")
    try:
        ENV_FILE.chmod(0o600)
    except OSError:                      # 윈도우 등 — 권한 모델이 달라도 저장은 성공
        pass
    return {"ok": True, "saved": [k for k in KEY_FIELDS if cur.get(k)],
            "path": str(ENV_FILE)}


def create_project(f: dict) -> dict:
    """폼 입력 → projects/{name}.yaml → Brain 등록. AI 프롬프트 초안은 채팅(/capture add) 몫."""
    name = str(f.get("name", "")).strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,39}", name):
        return {"ok": False, "error": "이름은 영문·숫자·-·_ 로 40자까지 (파일명이 됩니다)"}
    if f.get("type") not in PROJECT_TYPES:
        return {"ok": False, "error": f"종류는 {'/'.join(PROJECT_TYPES)} 중 하나"}
    domain = str(f.get("domain", "")).strip()
    if not domain:
        return {"ok": False, "error": "도메인을 입력해 주세요 (예: example.com)"}
    path = db.CAPTURE_HOME / "projects" / f"{name}.yaml"
    if path.exists():
        return {"ok": False, "error": f"{name} 은 이미 있습니다 — 다른 이름을 쓰세요."}

    def items(key: str) -> str:          # 줄바꿈·쉼표 아무렇게나 적어도 받는다
        vals = [s.strip() for s in re.split(r"[,\n]", str(f.get(key, ""))) if s.strip()]
        return json.dumps(vals, ensure_ascii=False)   # JSON 배열 = 유효한 YAML

    gsc = str(f.get("gsc_property", "")).strip() or f"sc-domain:{domain}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"# 대시보드 설정 화면에서 생성 — 손으로 고친 뒤에는\n"
        f"# python db.py sync-project {path} 를 다시 돌리면 반영됩니다.\n"
        f"name: {name}\ntype: {f['type']}\ndomain: {domain}\n"
        f"locale: {str(f.get('locale') or 'ko-KR').strip()}\n"
        f"gsc_property: {gsc}\n"
        f"brand_aliases: {items('brand_aliases')}\n"
        f"seed_keywords: {items('seed_keywords')}\n"
        f"competitors_manual: {items('competitors_manual')}\n"
        "surfaces_ai: [chatgpt, perplexity, gemini]\n"
        "limits:\n  max_keywords: 100\n  max_ai_prompts: 30\n", "utf-8")
    try:
        db.sync_project(str(path))
    except SystemExit as e:
        return {"ok": False, "error": str(e)}
    except ImportError:
        return {"ok": False, "error": "기본 부품(pyyaml)이 아직 없습니다 — "
                                      "위의 [기본 부품 설치]를 먼저 눌러 주세요."}
    return {"ok": True, "name": name, "path": str(path)}


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
        if u.path == "/api/doctor":
            return self._json(doctor.diagnose())
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
        path = urlparse(self.path).path
        if path not in ("/api/opp", "/api/setup/run", "/api/setup/keys",
                        "/api/setup/project"):
            return self._send(404, b"not found", "text/plain")
        if self.headers.get("X-Token") != TOKEN:
            return self._json({"error": "이 창은 만료됐습니다 — 대시보드를 다시 띄워 주세요."},
                              403)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return self._json({"error": "bad json"}, 400)

        if path == "/api/setup/run":
            if body.get("action") not in ACTIONS:
                return self._json({"error": "unknown action"}, 400)
            return self._json(run_action(body["action"]))
        if path == "/api/setup/keys":
            return self._json(save_keys(body))
        if path == "/api/setup/project":
            r = create_project(body)
            return self._json(r, 200 if r["ok"] else 400)

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
    ap.add_argument("--export", action="store_true",
                    help="서버를 띄우는 대신 그 시점 화면을 HTML 파일로 남긴다")
    ap.add_argument("--actions", help="--export 전용: Next Actions JSON 파일")
    a = ap.parse_args()

    if a.export:
        if not a.project:
            sys.exit("--export 에는 --project 가 필요합니다.")
        out = export(a.project, a.actions)
        print(f"report: {out}")
        if a.open:
            import webbrowser
            webbrowser.open(out.as_uri())
        return

    # 외부 노출 금지 — 로컬 전용이라 인증이 없다. 바인딩으로 막는다.
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    url = f"http://127.0.0.1:{a.port}/?t={TOKEN}" + (
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
