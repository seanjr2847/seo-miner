#!/usr/bin/env python3
"""Search Console 화면에서 '내보내기'로 받은 파일을 Brain에 넣는다 (구글 API 부품 없이).

구글 클라우드 프로젝트·OAuth 없이 GSC 실측을 쓰는 경로. 대신 UI 내보내기는
상위 1,000행까지만 준다 — 자동화·전체 행이 필요하면 collect_gsc.py(OAuth)를 쓴다.

받는 법: Search Console → 실적 → 우측 상단 '내보내기' → CSV (zip으로 받아짐)

Usage:
  python import_gsc_csv.py --project NAME [내려받은.zip|쿼리.csv] [--days 28] [--dry-run]
  # 파일을 생략하면 다운로드 폴더에서 가장 최근 내보내기를 알아서 찾는다.
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
import collector  # noqa: E402
import db  # noqa: E402
import scoring  # noqa: E402

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
    """zip/csv에서 검색어 표를 꺼낸다. 못 꺼내면 ValueError (자동 탐색이 건너뛸 수 있게)."""
    if path.suffix.lower() != ".zip":
        return path.name, path.read_bytes()
    with zipfile.ZipFile(path) as z:
        names = [n for n in z.namelist() if n.lower().endswith(".csv")]
        if not names:
            raise ValueError(f"zip 안에 CSV가 없습니다: {path}")
        hit = next((n for n in names if QUERY_FILE.search(n)), None)
        if not hit:
            # 이름을 못 알아보는 언어일 때: URL 목록(페이지 CSV)은 빼고 가장 큰 것.
            cand = [n for n in sorted(names, key=lambda n: -z.getinfo(n).file_size)
                    if not looks_like_urls(z.read(n))]
            if not cand:
                raise ValueError(f"zip 안에서 검색어 표를 찾지 못했습니다: {names}")
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


def newest_export(folder: Path) -> tuple[Path, str, bytes]:
    """다운로드 폴더에서 방금 받은 Search Console 파일을 알아서 집는다.
    최근 것부터 열어보고 검색어 표가 나오는 첫 파일을 쓴다."""
    files = sorted((p for p in folder.glob("*")
                    if p.suffix.lower() in (".zip", ".csv") and p.is_file()),
                   key=lambda p: -p.stat().st_mtime)[:20]
    for p in files:
        try:
            name, blob = pick_csv(p)
            if read_rows(blob):
                return p, name, blob
        except (ValueError, OSError, zipfile.BadZipFile, UnicodeDecodeError):
            continue
    sys.exit(f"{folder} 에서 Search Console 내보내기 파일을 찾지 못했습니다. "
             "실적 화면에서 '내보내기 → CSV'를 받은 뒤 다시 시도하거나, "
             "파일 경로를 직접 알려주세요.")


def main() -> None:
    ap = argparse.ArgumentParser()
    collector.add_common(ap)
    ap.add_argument("file", nargs="?",
                    help="Search Console에서 내보낸 zip 또는 csv "
                         "(생략하면 다운로드 폴더에서 가장 최근 것을 찾음)")
    ap.add_argument("--downloads-dir", default=None)
    collector.add_setting(ap, "--days", key="gsc_days", fallback=28, type=int,
                          help="내보낼 때 화면에 걸려 있던 기간 (기본 28일)")
    a = ap.parse_args()

    # 파일을 읽고 미리보기만 할 때는 Brain을 열지 않는다 — 이 경로는 키도 등록도
    # 없이 도는 입구라, --dry-run이 사이트 등록을 요구하면 그 약속이 깨진다.
    if a.file:
        path = Path(a.file).expanduser()
        if not path.exists():
            sys.exit(f"파일이 없습니다: {path}")
        try:
            name, blob = pick_csv(path)
        except zipfile.BadZipFile:
            sys.exit(f"{path.name} 은 정상적인 zip이 아닙니다 — 다운로드가 중간에 "
                     "끊겼을 수 있습니다. 내보내기를 다시 받아보세요.")
        except ValueError as e:
            sys.exit(str(e))
    else:
        path, name, blob = newest_export(
            Path(a.downloads_dir).expanduser() if a.downloads_dir else db.downloads_dir())
        print(f"[자동] 다운로드 폴더에서 찾음: {path.name}")
    rows = read_rows(blob)
    if not rows:
        sys.exit(f"'{name}'에서 읽을 행이 없습니다 — 실적 화면의 '쿼리' 표를 "
                 "내보낸 파일이 맞는지 확인해 주세요.")
    if a.dry_run:
        # Brain을 안 열었으므로 프로젝트 yaml의 gsc_days는 못 본다 — 안내용 숫자다.
        s_dry = collector.settings(a, None)
        print(f"[csv] {name} — {len(rows)}개 검색어, "
              f"기간 {s_dry['gsc_days']}일로 기록")
        for q, c, i, ctr, pos in rows[:5]:
            print(f"  {pos:>5}위  노출={i:<6} 클릭={c:<4} {q}")
        print("  ... (--dry-run, 저장 안 함)")
        return

    conn, p, cfg = collector.open_project(a.project)
    s = collector.settings(a, cfg)
    days = s["gsc_days"]
    print(f"[csv] {name} — {len(rows)}개 검색어, 기간 {days}일로 기록")
    snap = str(date.today())
    with db.run(conn, p["id"], "gsc") as r:
        # UI 내보내기에는 페이지 열이 없다 — page 는 NULL.
        db.write_gsc_snapshot(conn, p["id"], snap, days,
                              ((q, None, c, i, ctr, pos) for q, c, i, ctr, pos in rows))
        r.notes = f"csv-import rows={len(rows)} file={name}"

    print(f"saved snapshot {snap}. striking-distance preview "
          "(pos 4~20, impressions desc):")
    for s in scoring.striking(conn, p["id"], snap, limit=10,
                              brands=scoring.foreign_brands(conn, p["id"], cfg)):
        print(f"  {s['pos']:>5}  imp={s['imp']:<6} clk={s['clk']:<4} "
              f"gap={s['gap']:<5} {s['query']}")
    print("\n페이지별 데이터는 UI 내보내기에 없습니다 — 페이지 단위가 필요하면 "
          "collect_gsc.py(구글 로그인 연결)를 쓰세요.")
    conn.close()


if __name__ == "__main__":
    main()
