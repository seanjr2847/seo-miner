#!/usr/bin/env python3
"""로컬 대시보드 — Brain을 브라우저에서 실시간 조회·조작 (stdlib http.server).

화면은 하나다. 두 가지 모드로 쓴다:
  · 라이브(기본)  — 서버가 Brain을 그때그때 읽어준다. 기회 트리아지·설정이 된다.
  · 박제(--export) — 그 시점 데이터를 페이지 안에 박아 넣은 자립형 HTML 파일.
    서버도 인터넷도 필요 없고 남한테 보내도 그대로 열린다.
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
import collector  # noqa: E402  (프로젝트 설정 읽기 — 수집기와 같은 경로로)
import db         # noqa: E402
import doctor     # noqa: E402  (setup 스킬의 진단 — 대시보드 상단 배너용)
import scoring    # noqa: E402  (판정 규칙 — 화면·박제본·산문이 같은 임계값을 본다)

HTML = (Path(__file__).parent.parent / "templates" / "dashboard.html").read_bytes()

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
    """셸 rc 편집 대신 ~/.capture/env 에 모은다 (모든 스크립트가 db.load_env로 읽는다).
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

    def items(key: str) -> list[str]:    # 줄바꿈·쉼표 아무렇게나 적어도 받는다
        return [s.strip() for s in re.split(r"[,\n]", str(f.get(key, ""))) if s.strip()]

    # 폼 입력을 f-string으로 YAML에 끼우면 개행 하나로 키가 주입된다 — dump가 막는다.
    try:
        import yaml
    except ImportError:
        return {"ok": False, "error": "기본 부품(pyyaml)이 아직 없습니다 — "
                                      "위의 [기본 부품 설치]를 먼저 눌러 주세요."}
    gsc = str(f.get("gsc_property", "")).strip() or f"sc-domain:{domain}"
    doc = {"name": name, "type": f["type"], "domain": domain,
           "locale": str(f.get("locale") or "ko-KR").strip(),
           "gsc_property": gsc,
           "brand_aliases": items("brand_aliases"),
           "seed_keywords": items("seed_keywords"),
           "competitors_manual": items("competitors_manual"),
           "surfaces_ai": ["chatgpt", "perplexity", "gemini"],
           "limits": {"max_keywords": 100, "max_ai_prompts": 30}}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"# 대시보드 설정 화면에서 생성 — 손으로 고친 뒤에는\n"
        f"# python db.py sync-project {path} 를 다시 돌리면 반영됩니다.\n"
        + yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), "utf-8")
    try:
        db.sync_project(str(path))
    except SystemExit as e:
        return {"ok": False, "error": str(e)}
    except ImportError:
        return {"ok": False, "error": "기본 부품(pyyaml)이 아직 없습니다 — "
                                      "위의 [기본 부품 설치]를 먼저 눌러 주세요."}
    return {"ok": True, "name": name, "path": str(path)}


def setup_state() -> dict:
    """setup 스킬의 doctor.diagnose()를 소비해 대시보드 화면이 실제로 쓰는 평평한 키만 방출."""
    d = doctor.diagnose()
    projects = d.get("brain", {}).get("projects", [])
    no_project = not bool(projects)
    must = [s for s in d.get("must", []) if not (no_project and "/capture add" in s)]
    extra = [
        f"{c['name']} — {c['desc']}. 켜려면: {c.get('fix') or '필수 설치가 끝나면 켜집니다.'}"
        for c in d.get("locked", [])
    ] + list(d.get("later", []))
    keys = d.get("keys", {})
    deps_gsc = d.get("deps_gsc", {})
    gsc_ok = bool(keys.get("gsc_service_account"))
    show_deps_gsc_btn = gsc_ok and not bool(deps_gsc.get("googleapiclient"))
    show_setup = bool(d.get("must")) or no_project

    return {
        "verdict": d.get("verdict", ""),
        "no_project": no_project,
        "must": must,
        "extra": extra,
        "core_ok": bool(d.get("core_ok")),
        "gsc_ok": gsc_ok,
        "nkeys": sum(1 for k in ("openrouter", "serper", "dataforseo") if keys.get(k)),
        "show_deps_gsc_btn": show_deps_gsc_btn,
        "show_setup": show_setup,
    }


def q(conn, sql, args=()):
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def gather(conn, p) -> dict:
    """화면 하나가 쓰는 데이터 전부 — 라이브 대시보드와 박제 리포트가 같이 쓴다."""
    pid = p["id"]
    cur, prev, period, period_mismatch = scoring.snapshot_pair(conn, pid)

    def gsc_agg(snap):
        if not snap:
            return {}
        return {r["query"]: r for r in q(conn,
            """SELECT query, ROUND(AVG(position),1) pos,
                      SUM(impressions) imp, SUM(clicks) clk
                 FROM gsc_snapshots WHERE project_id=? AND snapshot_date=?
                GROUP BY query""", (pid, snap))}

    now_, before = gsc_agg(cur), gsc_agg(prev)
    ups, downs = scoring.movers(now_, before)

    # 남의 브랜드 카탈로그는 yaml에 있다. Brain에는 등록됐는데 yaml을 지운
    # 프로젝트도 화면은 떠야 한다 — project_cfg가 경고만 하고 빈 설정을 준다.
    cfg = collector.project_cfg(p["config_path"] or p["name"])
    striking = scoring.striking(conn, pid, cur,
                                brands=scoring.foreign_brands(conn, pid, cfg))

    ai_run = conn.execute(
        "SELECT id, started_at FROM runs WHERE project_id=? AND kind='ai' "
        "ORDER BY id DESC LIMIT 1", (pid,)).fetchone()
    matrix, gap_domains, missed = [], [], []
    if ai_run:
        matrix = q(conn,
            """SELECT c.engine, p2.category, SUM(c.cited) cited,
                      SUM(c.mentioned) mentioned, COUNT(*) total
                 FROM ai_checks c JOIN ai_prompts p2 ON p2.id=c.prompt_id
                WHERE c.run_id=? GROUP BY 1,2 ORDER BY 1,2""", (ai_run["id"],))
        freq: dict[str, int] = {}
        for r in q(conn, "SELECT cited_domains_json FROM ai_checks WHERE run_id=? AND cited=0",
                   (ai_run["id"],)):
            for d in json.loads(r["cited_domains_json"] or "[]"):
                freq[d] = freq.get(d, 0) + 1
        gap_domains = sorted(({"domain": k, "n": v} for k, v in freq.items()),
                             key=lambda x: -x["n"])[:15]
        missed = q(conn,
            """SELECT p2.prompt, p2.category,
                      GROUP_CONCAT(DISTINCT c.engine) engines
                 FROM ai_checks c JOIN ai_prompts p2 ON p2.id=c.prompt_id
                WHERE c.run_id=? AND c.cited=0 AND c.mentioned=0
                GROUP BY p2.id ORDER BY p2.category LIMIT 20""", (ai_run["id"],))

    rank_dates = [r[0] for r in conn.execute(
        """SELECT DISTINCT substr(rs.checked_at,1,10) d FROM rank_snapshots rs
             JOIN keywords k ON k.id=rs.keyword_id
            WHERE k.project_id=? ORDER BY d DESC LIMIT 2""", (pid,))]

    def rank_agg(d):
        if not d:
            return {}
        return {r["keyword"]: r for r in q(conn,
            """SELECT k.keyword, rs.position, rs.aio_present, rs.aio_cited
                 FROM rank_snapshots rs JOIN keywords k ON k.id=rs.keyword_id
                WHERE k.project_id=? AND substr(rs.checked_at,1,10)=?""", (pid, d))}

    r_cur = rank_agg(rank_dates[0] if rank_dates else None)
    r_prev = rank_agg(rank_dates[1] if len(rank_dates) > 1 else None)
    ranks, aio_gap = [], []
    for kw, r in r_cur.items():
        prev_r = r_prev.get(kw)
        dpos = (round(prev_r["position"] - r["position"], 0)
                if prev_r and prev_r["position"] and r["position"] else None)
        ranks.append({"keyword": kw, "pos": r["position"], "dpos": dpos,
                      "aio": r["aio_present"], "aio_cited": r["aio_cited"]})
        if r["aio_present"] == 1 and r["aio_cited"] == 0:
            aio_gap.append(kw)
    ranks.sort(key=lambda x: (x["pos"] is None, x["pos"] or 999))

    opps = scoring.opportunities(conn, pid, limit=200, with_id=True)
    opps_total = conn.execute(
        "SELECT COUNT(*) FROM opportunities WHERE project_id=? AND status='new'",
        (pid,)).fetchone()[0]
    runs = q(conn,
        """SELECT kind, started_at, api_calls, cost_estimate_usd, notes
             FROM runs WHERE project_id=? ORDER BY id DESC LIMIT 8""", (pid,))
    prog = progress(conn, pid)
    return {"project": dict(p), "gsc_date": cur, "gsc_prev": prev, "gsc_period": period,
            "period_mismatch": period_mismatch, "ups": ups, "downs": downs,
            "striking": striking, "matrix": matrix, "gap_domains": gap_domains,
            "missed": missed, "opps": opps, "opps_total": opps_total, "runs": runs,
            "rank_date": rank_dates[0] if rank_dates else None,
            "rank_prev": rank_dates[1] if len(rank_dates) > 1 else None,
            "ranks": ranks[:30], "aio_gap": aio_gap,
            "kw_active": conn.execute(
                "SELECT COUNT(*) FROM keywords WHERE project_id=? AND is_active=1",
                (pid,)).fetchone()[0],
            "rules": {"page1": scoring.PAGE1, "striking_lo": scoring.STRIKING_LO,
                      "striking_hi": scoring.STRIKING_HI},
            "trend": [dict(r) for r in conn.execute(
                """SELECT snapshot_date d, SUM(clicks) clk, SUM(impressions) imp,
                          COUNT(DISTINCT query) q
                     FROM gsc_snapshots WHERE project_id=?
                    GROUP BY 1 ORDER BY 1""", (pid,))],
            "progress": prog,
            "guide": scoring.stage(prog, p["name"], p["domain"] or "")}


def payload(project: str) -> dict:
    conn = db.connect()
    try:
        p = db.get_project(conn, project)
        return gather(conn, p)
    finally:
        conn.close()


def progress(conn, pid: int) -> dict:
    """안내 화면이 "지금 어디까지 팠는지"를 말하려면 단계별 실적이 필요하다.
    각 값은 그 단계를 했다는 증거 — 0이면 아직 안 한 것."""
    def one(sql: str, args=()) -> int:
        return conn.execute(sql, args).fetchone()[0] or 0

    return {
        "gsc_days": one("SELECT COUNT(DISTINCT snapshot_date) FROM gsc_snapshots "
                        "WHERE project_id=?", (pid,)),
        "gsc_last": (conn.execute("SELECT MAX(snapshot_date) FROM gsc_snapshots "
                                  "WHERE project_id=?", (pid,)).fetchone()[0] or ""),
        "keywords": one("SELECT COUNT(*) FROM keywords WHERE project_id=? AND is_active=1",
                        (pid,)),
        # 씨앗은 등록할 때 손으로 넣은 것이다 — 발굴을 돌렸는지는 씨앗이 아닌 것으로 센다.
        "keywords_found": one("SELECT COUNT(*) FROM keywords WHERE project_id=? "
                              "AND is_active=1 AND source!='seed'", (pid,)),
        "ai_checks": one("""SELECT COUNT(*) FROM ai_checks c JOIN ai_prompts p
                              ON p.id=c.prompt_id WHERE p.project_id=?""", (pid,)),
        "ai_prompts": one("SELECT COUNT(*) FROM ai_prompts WHERE project_id=? "
                          "AND is_active=1", (pid,)),
        "opps": one("SELECT COUNT(*) FROM opportunities WHERE project_id=?", (pid,)),
        "creations": one("SELECT COUNT(*) FROM creations WHERE project_id=?", (pid,)),
    }


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
            return self._json(setup_state())
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

        if body.get("status") not in db.OPP_STATUSES:
            return self._json({"error": f"status must be one of {db.OPP_STATUSES}"}, 400)
        conn = db.connect()
        updated = db.set_opportunity_status(conn, int(body.get("id") or 0), body["status"])
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
