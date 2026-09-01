"""동기화 패키지 (Sync Package) — 웹(호스팅 SaaS)과 로컬(플러그인) 간 전체 설정 및 수집 데이터 이동.

패키지 형식 (.json):
  schema: 1
  type: "seo-miner-sync-package"
  project: <name>
  yaml_raw: <yaml_text>
  config: <parsed_dict>
  data: {keywords, gsc_snapshots, gsc_daily, gsc_breakdown, ga4_snapshots, ...}
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import db


def export_package(conn: sqlite3.Connection, project: str) -> dict:
    """사이트의 YAML 설정과 brain.db 에 쌓인 수집 데이터를 단일 dict 로 패킹한다."""
    p = db.get_project(conn, project)
    pid = p["id"]

    # 1. YAML 설정
    yaml_path = db.CAPTURE_HOME / "projects" / f"{project}.yaml"
    if yaml_path.exists():
        import yaml
        yaml_raw = yaml_path.read_text("utf-8")
        try:
            cfg = yaml.safe_load(yaml_raw) or {}
        except Exception:
            cfg = {}
    else:
        import yaml
        yaml_raw = ""
        cfg = {"name": p["name"], "domain": p["domain"], "type": p["type"],
               "locale": p["locale"], "gsc_property": p["gsc_property"]}

    # 2. DB 수집 데이터
    def rows_of(query: str, args=()) -> list[dict]:
        cur = conn.execute(query, args)
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    keywords = rows_of(
        "SELECT keyword, locale, cluster, intent, volume, difficulty, source, is_active "
        "FROM keywords WHERE project_id=?", (pid,))

    gsc_snapshots = rows_of(
        "SELECT snapshot_date, period_days, query, page, clicks, impressions, ctr, position "
        "FROM gsc_snapshots WHERE project_id=?", (pid,))

    gsc_daily = rows_of(
        "SELECT date, clicks, impressions, ctr, position "
        "FROM gsc_daily WHERE project_id=?", (pid,))

    gsc_breakdown = rows_of(
        "SELECT snapshot_date, period_days, dim, dim_value, query, clicks, impressions, ctr, position "
        "FROM gsc_breakdown WHERE project_id=?", (pid,))

    ga4_snapshots = rows_of(
        "SELECT snapshot_date, period_days, landing_page, sessions, sessions_all, key_events, "
        "total_revenue, engagement_rate, avg_session_duration, pageviews_per_session "
        "FROM ga4_snapshots WHERE project_id=?", (pid,))

    ga4_breakdown = rows_of(
        "SELECT snapshot_date, period_days, dim, dim_value, landing_page, sessions, "
        "key_events, total_revenue, engagement_rate "
        "FROM ga4_breakdown WHERE project_id=?", (pid,))

    gsc_index_status = rows_of(
        "SELECT checked_date, url, verdict, coverage_state, robots_txt_state, page_fetch_state, "
        "indexing_state, google_canonical, user_canonical, last_crawled, rich_results_json "
        "FROM gsc_index_status WHERE project_id=?", (pid,))

    page_audits = rows_of(
        "SELECT checked_date, url, status, error, title, meta_description, h1_json, h2_json, "
        "words, schema_json, canonical, robots, internal_links, external_links, images, images_no_alt "
        "FROM page_audits WHERE project_id=?", (pid,))

    ai_prompts = rows_of(
        "SELECT id, prompt, category, is_active "
        "FROM ai_prompts WHERE project_id=?", (pid,))

    prompt_map = {r["id"]: r["prompt"] for r in ai_prompts}
    ai_checks_raw = rows_of(
        "SELECT c.prompt_id, c.engine, c.sample_idx, c.checked_at, c.mentioned, c.cited, "
        "c.cited_domains_json, c.answer_excerpt "
        "FROM ai_checks c JOIN ai_prompts p ON c.prompt_id=p.id "
        "WHERE p.project_id=?", (pid,))

    ai_checks = []
    for r in ai_checks_raw:
        item = dict(r)
        item["prompt"] = prompt_map.get(item.pop("prompt_id"))
        if item["prompt"]:
            ai_checks.append(item)

    competitors = rows_of(
        "SELECT domain, source FROM competitors WHERE project_id=?", (pid,))

    opportunities = rows_of(
        "SELECT kind, target, score, status, reasoning, created_at "
        "FROM opportunities WHERE project_id=?", (pid,))

    return {
        "schema": 1,
        "type": "seo-miner-sync-package",
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "project": project,
        "yaml_raw": yaml_raw,
        "config": cfg,
        "data": {
            "keywords": keywords,
            "gsc_snapshots": gsc_snapshots,
            "gsc_daily": gsc_daily,
            "gsc_breakdown": gsc_breakdown,
            "ga4_snapshots": ga4_snapshots,
            "ga4_breakdown": ga4_breakdown,
            "gsc_index_status": gsc_index_status,
            "page_audits": page_audits,
            "ai_prompts": [dict(p, id=None) for p in ai_prompts],
            "ai_checks": ai_checks,
            "competitors": competitors,
            "opportunities": opportunities,
        }
    }


def import_package(conn: sqlite3.Connection, pkg: dict) -> dict:
    """동기화 패키지를 받아 프로젝트 YAML 파일 갱신 및 brain.db 에 데이터 병합."""
    if not isinstance(pkg, dict):
        return {"ok": False, "error": "패키지 형식이 올바르지 않습니다 (JSON 객체여야 함)."}
    if pkg.get("type") != "seo-miner-sync-package":
        return {"ok": False, "error": "seo-miner 동기화 패키지 파일이 아닙니다."}
    project = str(pkg.get("project") or "").strip()
    if not project:
        return {"ok": False, "error": "패키지에 프로젝트 이름이 누락되었습니다."}

    # 1. YAML 파일 복원 및 db.sync_project
    yaml_raw = pkg.get("yaml_raw")
    if not yaml_raw:
        import yaml
        cfg = pkg.get("config") or {"name": project, "domain": "example.com", "type": "saas"}
        yaml_raw = yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False)

    yaml_dir = db.CAPTURE_HOME / "projects"
    yaml_dir.mkdir(parents=True, exist_ok=True)
    yaml_path = yaml_dir / f"{project}.yaml"
    yaml_path.write_text(yaml_raw, "utf-8")

    try:
        db.sync_project(str(yaml_path))
    except Exception as e:
        return {"ok": False, "error": f"프로젝트 동기화 실패: {e}"}

    pid = conn.execute("SELECT id FROM projects WHERE name=?", (project,)).fetchone()[0]
    data = pkg.get("data") or {}
    counts = {}

    # 2. DB 데이터 병합 (UPSERT / INSERT OR IGNORE)
    kw_count = 0
    for kw in data.get("keywords") or []:
        kword = (kw.get("keyword") or "").strip()
        if not kword:
            continue
        conn.execute(
            """INSERT INTO keywords(project_id, keyword, locale, cluster, intent, volume, difficulty, source, is_active)
               VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(project_id, keyword) DO UPDATE SET
                 locale=COALESCE(excluded.locale, keywords.locale),
                 cluster=COALESCE(excluded.cluster, keywords.cluster),
                 intent=COALESCE(excluded.intent, keywords.intent),
                 volume=COALESCE(excluded.volume, keywords.volume),
                 difficulty=COALESCE(excluded.difficulty, keywords.difficulty),
                 source=COALESCE(excluded.source, keywords.source),
                 is_active=MAX(keywords.is_active, excluded.is_active)""",
            (pid, kword, kw.get("locale"), kw.get("cluster"), kw.get("intent"),
             kw.get("volume"), kw.get("difficulty"), kw.get("source") or "seed",
             kw.get("is_active", 0)))
        kw_count += 1
    counts["keywords"] = kw_count

    gsc_count = 0
    for g in data.get("gsc_snapshots") or []:
        exists = conn.execute(
            "SELECT 1 FROM gsc_snapshots WHERE project_id=? AND snapshot_date=? AND period_days=? AND query=? AND COALESCE(page,'')=?",
            (pid, g["snapshot_date"], g["period_days"], g["query"], g.get("page") or "")).fetchone()
        if not exists:
            conn.execute(
                """INSERT INTO gsc_snapshots(project_id, snapshot_date, period_days, query, page, clicks, impressions, ctr, position)
                   VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (pid, g["snapshot_date"], g["period_days"], g["query"], g.get("page"),
                 g.get("clicks"), g.get("impressions"), g.get("ctr"), g.get("position")))
            gsc_count += 1
    counts["gsc_snapshots"] = gsc_count

    daily_count = 0
    for d in data.get("gsc_daily") or []:
        conn.execute(
            """INSERT INTO gsc_daily(project_id, date, clicks, impressions, ctr, position)
               VALUES(?, ?, ?, ?, ?, ?)
               ON CONFLICT(project_id, date) DO UPDATE SET
                 clicks=excluded.clicks, impressions=excluded.impressions,
                 ctr=excluded.ctr, position=excluded.position""",
            (pid, d["date"], d.get("clicks"), d.get("impressions"), d.get("ctr"), d.get("position")))
        daily_count += 1
    counts["gsc_daily"] = daily_count

    bd_count = 0
    for b in data.get("gsc_breakdown") or []:
        exists = conn.execute(
            "SELECT 1 FROM gsc_breakdown WHERE project_id=? AND snapshot_date=? AND period_days=? AND dim=? AND dim_value=? AND query=?",
            (pid, b["snapshot_date"], b["period_days"], b["dim"], b["dim_value"], b["query"])).fetchone()
        if not exists:
            conn.execute(
                """INSERT INTO gsc_breakdown(project_id, snapshot_date, period_days, dim, dim_value, query, clicks, impressions, ctr, position)
                   VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (pid, b["snapshot_date"], b["period_days"], b["dim"], b["dim_value"], b["query"],
                 b.get("clicks"), b.get("impressions"), b.get("ctr"), b.get("position")))
            bd_count += 1
    counts["gsc_breakdown"] = bd_count

    ga4_count = 0
    for ga in data.get("ga4_snapshots") or []:
        exists = conn.execute(
            "SELECT 1 FROM ga4_snapshots WHERE project_id=? AND snapshot_date=? AND period_days=? AND landing_page=?",
            (pid, ga["snapshot_date"], ga["period_days"], ga["landing_page"])).fetchone()
        if not exists:
            conn.execute(
                """INSERT INTO ga4_snapshots(project_id, snapshot_date, period_days, landing_page, sessions, sessions_all,
                     key_events, total_revenue, engagement_rate, avg_session_duration, pageviews_per_session)
                   VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (pid, ga["snapshot_date"], ga["period_days"], ga["landing_page"],
                 ga.get("sessions"), ga.get("sessions_all"), ga.get("key_events"),
                 ga.get("total_revenue"), ga.get("engagement_rate"),
                 ga.get("avg_session_duration"), ga.get("pageviews_per_session")))
            ga4_count += 1
    counts["ga4_snapshots"] = ga4_count

    ga4_bd_count = 0
    for gb in data.get("ga4_breakdown") or []:
        exists = conn.execute(
            "SELECT 1 FROM ga4_breakdown WHERE project_id=? AND snapshot_date=? AND period_days=? AND dim=? AND dim_value=? AND landing_page=?",
            (pid, gb["snapshot_date"], gb["period_days"], gb["dim"], gb["dim_value"], gb["landing_page"])).fetchone()
        if not exists:
            conn.execute(
                """INSERT INTO ga4_breakdown(project_id, snapshot_date, period_days, dim, dim_value, landing_page,
                     sessions, key_events, total_revenue, engagement_rate)
                   VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (pid, gb["snapshot_date"], gb["period_days"], gb["dim"], gb["dim_value"], gb["landing_page"],
                 gb.get("sessions"), gb.get("key_events"), gb.get("total_revenue"), gb.get("engagement_rate")))
            ga4_bd_count += 1
    counts["ga4_breakdown"] = ga4_bd_count

    idx_count = 0
    for ix in data.get("gsc_index_status") or []:
        conn.execute(
            """INSERT INTO gsc_index_status(project_id, checked_date, url, verdict, coverage_state, robots_txt_state,
                 page_fetch_state, indexing_state, google_canonical, user_canonical, last_crawled, rich_results_json)
               VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(project_id, checked_date, url) DO UPDATE SET
                 verdict=excluded.verdict, coverage_state=excluded.coverage_state,
                 robots_txt_state=excluded.robots_txt_state, page_fetch_state=excluded.page_fetch_state,
                 indexing_state=excluded.indexing_state, google_canonical=excluded.google_canonical,
                 user_canonical=excluded.user_canonical, last_crawled=excluded.last_crawled,
                 rich_results_json=excluded.rich_results_json""",
            (pid, ix["checked_date"], ix["url"], ix.get("verdict"), ix.get("coverage_state"),
             ix.get("robots_txt_state"), ix.get("page_fetch_state"), ix.get("indexing_state"),
             ix.get("google_canonical"), ix.get("user_canonical"), ix.get("last_crawled"),
             ix.get("rich_results_json")))
        idx_count += 1
    counts["gsc_index_status"] = idx_count

    pa_count = 0
    for pa in data.get("page_audits") or []:
        conn.execute(
            """INSERT INTO page_audits(project_id, checked_date, url, status, error, title, meta_description,
                 h1_json, h2_json, words, schema_json, canonical, robots, internal_links, external_links, images, images_no_alt)
               VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(project_id, checked_date, url) DO UPDATE SET
                 status=excluded.status, error=excluded.error, title=excluded.title,
                 meta_description=excluded.meta_description, h1_json=excluded.h1_json,
                 h2_json=excluded.h2_json, words=excluded.words, schema_json=excluded.schema_json,
                 canonical=excluded.canonical, robots=excluded.robots,
                 internal_links=excluded.internal_links, external_links=excluded.external_links,
                 images=excluded.images, images_no_alt=excluded.images_no_alt""",
            (pid, pa["checked_date"], pa["url"], pa.get("status"), pa.get("error"), pa.get("title"),
             pa.get("meta_description"), pa.get("h1_json"), pa.get("h2_json"), pa.get("words"),
             pa.get("schema_json"), pa.get("canonical"), pa.get("robots"), pa.get("internal_links"),
             pa.get("external_links"), pa.get("images"), pa.get("images_no_alt")))
        pa_count += 1
    counts["page_audits"] = pa_count

    prompt_ids = {}
    for ap in data.get("ai_prompts") or []:
        ptext = (ap.get("prompt") or "").strip()
        if not ptext:
            continue
        conn.execute(
            "INSERT OR IGNORE INTO ai_prompts(project_id, prompt, category, is_active) VALUES(?, ?, ?, ?)",
            (pid, ptext, ap.get("category") or "general", ap.get("is_active", 1)))
        row = conn.execute("SELECT id FROM ai_prompts WHERE project_id=? AND prompt=?", (pid, ptext)).fetchone()
        if row:
            prompt_ids[ptext] = row[0]

    ai_checks_count = 0
    for ac in data.get("ai_checks") or []:
        pr_id = prompt_ids.get(ac.get("prompt"))
        if not pr_id:
            continue
        exists = conn.execute(
            "SELECT 1 FROM ai_checks WHERE prompt_id=? AND engine=? AND sample_idx=? AND checked_at=?",
            (pr_id, ac["engine"], ac.get("sample_idx", 0), ac.get("checked_at"))).fetchone()
        if not exists:
            conn.execute(
                """INSERT INTO ai_checks(prompt_id, run_id, engine, sample_idx, checked_at, mentioned, cited, cited_domains_json, answer_excerpt)
                   VALUES(?, NULL, ?, ?, ?, ?, ?, ?, ?)""",
                (pr_id, ac["engine"], ac.get("sample_idx", 0), ac.get("checked_at"),
                 ac.get("mentioned", 0), ac.get("cited", 0), ac.get("cited_domains_json"),
                 ac.get("answer_excerpt")))
            ai_checks_count += 1
    counts["ai_checks"] = ai_checks_count

    comp_count = 0
    for cp in data.get("competitors") or []:
        cdom = (cp.get("domain") or "").strip().lower()
        if cdom:
            conn.execute(
                "INSERT OR IGNORE INTO competitors(project_id, domain, source) VALUES(?, ?, ?)",
                (pid, cdom, cp.get("source") or "manual"))
            comp_count += 1
    counts["competitors"] = comp_count

    opp_count = 0
    for op in data.get("opportunities") or []:
        exists = conn.execute(
            "SELECT 1 FROM opportunities WHERE project_id=? AND kind=? AND target=?",
            (pid, op["kind"], op["target"])).fetchone()
        if not exists:
            conn.execute(
                """INSERT INTO opportunities(project_id, run_id, kind, target, score, status, reasoning, created_at)
                   VALUES(?, NULL, ?, ?, ?, ?, ?, ?)""",
                (pid, op["kind"], op["target"], op.get("score", 0.0), op.get("status") or "new",
                 op.get("reasoning") or "", op.get("created_at")))
            opp_count += 1
    counts["opportunities"] = opp_count

    conn.commit()
    return {"ok": True, "project": project, "counts": counts}
