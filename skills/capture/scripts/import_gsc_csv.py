#!/usr/bin/env python3
"""Search Console 화면에서 '내보내기'로 받은 파일을 Brain에 넣는다 (stdlib 전용).

구글 클라우드 프로젝트·OAuth 없이 GSC 실측을 쓰는 경로. 대신 UI 내보내기는
상위 1,000행까지만 준다 — 자동화·전체 행이 필요하면 collect_gsc.py(OAuth)를 쓴다.

받는 법: Search Console → 실적 → 우측 상단 '내보내기' → CSV (zip으로 받아짐)

Usage:
  python import_gsc_csv.py --project NAME <내려받은.zip|쿼리.csv> [--days 28] [--dry-run]
"""
import argparse
import csv
import io
import re
import sys
import zipfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import db  # noqa: E402
from collect_gsc import preview  # noqa: E402  (import 시점엔 stdlib만 씀)

# zip 안에는 쿼리·페이지·국가·기기 CSV가 같이 들어있다. 쿼리 파일만 고른다.
QUERY_FILE = re.compile(r"quer|쿼리|검색어|クエリ|consult|requêt|abfrag", re.I)


def parse_num(s: str) -> float:
    """'1,234' → 1234 · '3.5%' → 0.035 · '3,5' → 3.5 (로케일별 표기 흡수)."""
    s = (s or "").strip().replace(" ", "").replace(" ", "")
    pct = s.endswith("%")
    s = s.rstrip("%")
    if "," in s and "." not in s:          # 소수점 콤마(3,5) vs 천단위(1,234)
        s = s.replace(",", "." if len(s.split(",")[-1]) != 3 else "")
    else:
        s = s.replace(",", "")
    if not s:
        return 0.0
    v = float(s)
    return v / 100 if pct else v


def pick_csv(path: Path) -> tuple[str, bytes]:
    if path.suffix.lower() != ".zip":
        return path.name, path.read_bytes()
    with zipfile.ZipFile(path) as z:
        names = [n for n in z.namelist() if n.lower().endswith(".csv")]
        if not names:
            sys.exit(f"zip 안에 CSV가 없습니다: {path}")
        hit = next((n for n in names if QUERY_FILE.search(n)), None)
        if not hit:
            # 이름을 못 알아보는 언어일 때: URL 목록(페이지 CSV)은 빼고 가장 큰 것.
            cand = [n for n in sorted(names, key=lambda n: -z.getinfo(n).file_size)
                    if not looks_like_urls(z.read(n))]
            if not cand:
                sys.exit(f"zip 안에서 검색어 표를 찾지 못했습니다: {names}")
            hit = cand[0]
            print(f"[주의] 쿼리 파일 이름을 못 알아봐서 '{hit}'를 검색어 표로 씁니다.")
        return hit, z.read(hit)


def looks_like_urls(blob: bytes) -> bool:
    """페이지 CSV는 첫 칸이 URL — 검색어로 잘못 넣지 않도록 걸러낸다."""
    for line in blob.decode("utf-8-sig", "replace").splitlines()[1:3]:
        if line.strip():
            return line.split(",")[0].strip().lower().startswith("http")
    return False


def read_rows(blob: bytes) -> list[tuple]:
    """열 순서(검색어, 클릭수, 노출수, CTR, 게재순위)는 언어와 무관하게 고정."""
    rdr = csv.reader(io.StringIO(blob.decode("utf-8-sig")))
    rows = []
    for i, row in enumerate(rdr):
        if len(row) < 5 or not row[0].strip():
            continue
        try:
            nums = [parse_num(c) for c in row[1:5]]
        except ValueError:
            if i == 0:
                continue                    # 헤더
            print(f"[건너뜀] 숫자로 못 읽은 줄: {row[:5]}")
            continue
        clicks, imps, ctr, pos = nums
        rows.append((row[0].strip(), int(clicks), int(imps), round(ctr, 4), round(pos, 1)))
    return rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    ap.add_argument("file", help="Search Console에서 내보낸 zip 또는 csv")
    ap.add_argument("--days", type=int, default=28,
                    help="내보낼 때 화면에 걸려 있던 기간 (기본 28일)")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    path = Path(a.file).expanduser()
    if not path.exists():
        sys.exit(f"파일이 없습니다: {path}")
    name, blob = pick_csv(path)
    rows = read_rows(blob)
    if not rows:
        sys.exit(f"'{name}'에서 읽을 행이 없습니다 — 실적 화면의 '쿼리' 표를 "
                 "내보낸 파일이 맞는지 확인해 주세요.")
    print(f"[csv] {name} — {len(rows)}개 검색어, 기간 {a.days}일로 기록")
    if a.dry_run:
        for q, c, i, ctr, p in rows[:5]:
            print(f"  {p:>5}위  노출={i:<6} 클릭={c:<4} {q}")
        print("  ... (--dry-run, 저장 안 함)")
        return

    conn = db.connect()
    p = db.get_project(conn, a.project)
    run_id = db.start_run(conn, p["id"], "gsc")
    snap = str(date.today())
    conn.execute(  # 같은 날 다시 넣으면 덮어쓴다 (collect_gsc와 동일)
        "DELETE FROM gsc_snapshots WHERE project_id=? AND snapshot_date=?",
        (p["id"], snap))
    conn.executemany(
        """INSERT INTO gsc_snapshots(project_id, snapshot_date, period_days,
             query, page, clicks, impressions, ctr, position)
           VALUES(?,?,?,?,NULL,?,?,?,?)""",
        [(p["id"], snap, a.days, q, c, i, ctr, pos) for q, c, i, ctr, pos in rows])
    conn.commit()
    db.finish_run(conn, run_id, api_calls=0,
                  notes=f"csv-import rows={len(rows)} file={name}")
    preview(conn, p["id"], snap)
    print("\n페이지별 데이터는 UI 내보내기에 없습니다 — 페이지 단위가 필요하면 "
          "collect_gsc.py(구글 로그인 연결)를 쓰세요.")
    conn.close()


if __name__ == "__main__":
    main()
