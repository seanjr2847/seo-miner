#!/usr/bin/env python3
"""Render the self-contained HTML report from the Brain.

Contract: the report always ends with Next Actions. Claude writes them to a
JSON file (list of strings) and passes --actions; without it a placeholder
reminds you to run the analysis step.

Usage:
  python report.py --project NAME [--actions actions.json] [--open]
Output:
  $CAPTURE_HOME/reports/{project}/{YYYY-MM-DD}.html   (path printed)
"""
import argparse
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import db  # noqa: E402


def q(conn, sql, args=()):
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def gather(conn, p) -> dict:
    pid = p["id"]
    dates = [r["snapshot_date"] for r in conn.execute(
        "SELECT DISTINCT snapshot_date FROM gsc_snapshots WHERE project_id=? "
        "ORDER BY 1 DESC LIMIT 2", (pid,))]
    cur, prev = (dates + [None, None])[:2]

    def gsc_agg(snap):
        if not snap:
            return {}
        return {r["query"]: r for r in q(conn,
            """SELECT query, ROUND(AVG(position),1) pos,
                      SUM(impressions) imp, SUM(clicks) clk
                 FROM gsc_snapshots WHERE project_id=? AND snapshot_date=?
                GROUP BY query""", (pid, snap))}

    now_, before = gsc_agg(cur), gsc_agg(prev)
    movers = []
    for kw, r in now_.items():
        b = before.get(kw)
        if b:
            movers.append({"query": kw, "pos": r["pos"], "dpos": round(b["pos"] - r["pos"], 1),
                           "clk": r["clk"], "dclk": r["clk"] - b["clk"], "imp": r["imp"]})
    ups = sorted([m for m in movers if m["dpos"] > 0.4 or m["dclk"] > 0],
                 key=lambda m: (-m["dclk"], -m["dpos"]))[:10]
    downs = sorted([m for m in movers if m["dpos"] < -0.4 or m["dclk"] < 0],
                   key=lambda m: (m["dclk"], m["dpos"]))[:10]
    striking = q(conn,
        """SELECT query, ROUND(AVG(position),1) pos, SUM(impressions) imp, SUM(clicks) clk
             FROM gsc_snapshots WHERE project_id=? AND snapshot_date=?
            GROUP BY query HAVING pos BETWEEN 4 AND 20
            ORDER BY imp DESC LIMIT 15""", (pid, cur)) if cur else []

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
        prev = r_prev.get(kw)
        dpos = (round(prev["position"] - r["position"], 0)
                if prev and prev["position"] and r["position"] else None)
        ranks.append({"keyword": kw, "pos": r["position"], "dpos": dpos,
                      "aio": r["aio_present"], "aio_cited": r["aio_cited"]})
        if r["aio_present"] == 1 and r["aio_cited"] == 0:
            aio_gap.append(kw)
    ranks.sort(key=lambda x: (x["pos"] is None, x["pos"] or 999))

    opps = q(conn,
        """SELECT kind, target, ROUND(score,1) score, reasoning, status
             FROM opportunities WHERE project_id=?
            ORDER BY created_at DESC, score DESC LIMIT 12""", (pid,))
    runs = q(conn,
        """SELECT kind, started_at, api_calls, cost_estimate_usd, notes
             FROM runs WHERE project_id=? ORDER BY id DESC LIMIT 8""", (pid,))
    return {"gsc_date": cur, "gsc_prev": prev, "ups": ups, "downs": downs,
            "striking": striking, "matrix": matrix, "gap_domains": gap_domains,
            "missed": missed, "opps": opps, "runs": runs,
            "rank_date": rank_dates[0] if rank_dates else None,
            "rank_prev": rank_dates[1] if len(rank_dates) > 1 else None,
            "ranks": ranks[:30], "aio_gap": aio_gap,
            "kw_active": conn.execute(
                "SELECT COUNT(*) FROM keywords WHERE project_id=? AND is_active=1",
                (pid,)).fetchone()[0]}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    ap.add_argument("--actions", help="JSON file: list of next-action strings")
    ap.add_argument("--open", action="store_true")
    a = ap.parse_args()

    from jinja2 import Environment, FileSystemLoader
    conn = db.connect()
    p = db.get_project(conn, a.project)
    ctx = gather(conn, p)
    actions = []
    if a.actions and Path(a.actions).exists():
        actions = json.loads(Path(a.actions).read_text(encoding="utf-8"))
    ctx.update({"project": dict(p), "today": str(date.today()), "actions": actions})

    env = Environment(loader=FileSystemLoader(Path(__file__).parent.parent / "templates"),
                      autoescape=True)
    html = env.get_template("report.html.j2").render(**ctx)
    out_dir = db.CAPTURE_HOME / "reports" / p["name"]
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{date.today()}.html"
    out.write_text(html, encoding="utf-8")
    print(f"report: {out}")
    if a.open:
        import webbrowser
        webbrowser.open(f"file://{out}")
    conn.close()


if __name__ == "__main__":
    main()
