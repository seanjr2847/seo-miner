#!/usr/bin/env python3
"""Common Crawl 웹 그래프에서 도메인 하나의 순위를 읽는다 (무료, 키 없음).

DataForSEO 키가 없으면 백링크 단계가 통째로 비었다. 그때만 쓰는 폴백이다 — 링크 목록은
못 주고 "웹 그래프에서 이 도메인이 어디쯤인가" 한 줄만 준다.

자료 (https://commoncrawl.org/web-graphs):
  회차 목록  https://index.commoncrawl.org/graphinfo.json  — 최신이 맨 앞.
             [{id, crawls, index, location, stats: {host: {nodes, arcs}, domain: {nodes, arcs}}}]
  순위 파일  https://data.commoncrawl.org/projects/hyperlinkgraph/<id>/domain/<id>-domain-ranks.txt.gz
             탭 구분, 머리줄 `#harmonicc_pos #harmonicc_val #pr_pos #pr_val #host_rev #n_hosts`.
             host_rev 는 뒤집은 도메인(com.google). 줄은 조화 중심성 순위 오름차순이다
             (2026-11 실측 — 문서에 적힌 규칙은 아니라서 정렬을 가정하지 않고 읽은 줄을 센다).

조회 API 가 없다. 파일은 gzip 한 덩어리(최신 회차 약 2.4GiB, 도메인 1.35억)라 임의 접근도
안 된다. 그래서 **앞에서부터 MAX_BYTES 만** 읽는다 — 순위가 높은 도메인은 앞에 있다. 그 안에
없으면 "읽은 범위 안에 없다"까지만 말한다.

    미수록 ≠ 권위 낮음.  Common Crawl 은 웹의 표본이다("Common Crawl's dataset is a sample
    of the web" — https://commoncrawl.org/faq). 못 찾았다는 것은 표본에 없거나 읽은 범위
    밖이라는 뜻이지 그 도메인이 약하다는 뜻이 아니다. 화면·요청문 문구는 NOT_FOUND_SAYS 한
    벌을 쓴다(test_link_axis 가 금칙어를 본다).

Usage:
  python cc_graph.py                 # self-check (네트워크 안 탐)
  python cc_graph.py --lookup DOMAIN # 실제로 한 번 읽어 본다
"""
from __future__ import annotations

import sys
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

GRAPHINFO = "https://index.commoncrawl.org/graphinfo.json"
BASE = "https://data.commoncrawl.org/projects/hyperlinkgraph"
SOURCE = "commoncrawl"
MAX_BYTES = 64 * 1024 * 1024      # 압축 기준. 대략 상위 300만 도메인
TIMEOUT = 60
UA = {"User-Agent": "seo-miner/cc-graph (+https://github.com/seanjr2847/seo-miner)"}

# 못 찾았을 때의 말 — 한 벌. '낮다'·'약하다'를 쓰지 않는다.
NOT_FOUND_SAYS = ("Common Crawl 웹 그래프의 읽은 범위(조화 중심성 상위 {scanned:,}위) 안에 이 도메인이 "
                  "없습니다. Common Crawl 은 웹의 표본이라, 없다는 것이 이 도메인의 힘을 말해 "
                  "주지는 않습니다.")
FOUND_SAYS = ("Common Crawl 웹 그래프({release})에서 도메인 {total:,}개 중 조화 중심성 {harmonic_pos:,}위, "
              "PageRank {pagerank_pos:,}위입니다. 링크 목록이 아니라 그래프에서의 자리입니다.")


def ranks_url(release: str) -> str:
    return f"{BASE}/{release}/domain/{release}-domain-ranks.txt.gz"


def rev(domain: str) -> str:
    """'www.Example.co.kr' → 'kr.co.example'. 그래프는 등록 도메인 단위다."""
    import scoring
    return ".".join(reversed(scoring.host_of(domain).split(".")))


def latest_release(info) -> tuple[str | None, int | None]:
    """graphinfo.json → (최신 회차 id, 그 회차의 도메인 수)."""
    first = info[0] if isinstance(info, list) and info and isinstance(info[0], dict) else {}
    nodes = ((first.get("stats") or {}).get("domain") or {}).get("nodes")
    return first.get("id"), nodes if isinstance(nodes, int) else None


def scan(lines, want: str) -> tuple[dict | None, int]:
    """순위 파일의 줄들 → (찾은 줄, 읽은 자료 줄 수). 칸이 모자란 줄·머리줄은 건너뛴다."""
    n = 0
    for ln in lines:
        if not ln or ln.startswith("#"):
            continue
        f = ln.rstrip("\r\n").split("\t")
        if len(f) < 5:
            continue
        n += 1
        if f[4] == want:
            num = lambda v: int(v) if str(v).isdigit() else None
            return {"harmonic_pos": num(f[0]), "pagerank_pos": num(f[2]),
                    "hosts": num(f[5]) if len(f) > 5 else None}, n
    return None, n


def _fetch_info():
    import requests
    r = requests.get(GRAPHINFO, headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def _open_lines(url: str, max_bytes: int):
    """gzip 스트림을 앞에서부터 max_bytes(압축 기준)까지만 풀어 줄 단위로 낸다."""
    import requests
    with requests.get(url, headers=UA, timeout=TIMEOUT, stream=True) as r:
        r.raise_for_status()
        z = zlib.decompressobj(16 + zlib.MAX_WBITS)
        got, tail = 0, b""
        for chunk in r.iter_content(chunk_size=1 << 20):
            got += len(chunk)
            data = tail + z.decompress(chunk)
            *lines, tail = data.split(b"\n")
            for ln in lines:
                yield ln.decode("utf-8", "replace")
            if got >= max_bytes:
                return                       # 마지막 반쪽 줄(tail)은 버린다 — 잘린 줄이다
        if tail:
            yield tail.decode("utf-8", "replace")


def lookup(domain: str, *, fetch_info=None, open_lines=None, max_bytes: int = MAX_BYTES) -> dict:
    """도메인 하나 → {source, release, found, scanned, total, harmonic_pos, pagerank_pos, hosts}.

    네트워크 실패는 예외로 올린다(부른 쪽이 사유로 적는다). found=0 은 실패가 아니다.
    """
    release, total = latest_release((fetch_info or _fetch_info)())
    if not release:
        raise RuntimeError("Common Crawl 그래프 회차 목록을 읽지 못했습니다")
    row, scanned = scan((open_lines or _open_lines)(ranks_url(release), max_bytes), rev(domain))
    return {"source": SOURCE, "release": release, "found": int(bool(row)), "scanned": scanned,
            "total": total, **(row or {"harmonic_pos": None, "pagerank_pos": None, "hosts": None})}


def says(row: dict | None) -> str:
    """그래프 줄 하나를 사람 말로 — 화면과 요청문이 같은 문장을 쓴다."""
    if not row:
        return ""
    if row.get("found") and row.get("harmonic_pos"):
        return FOUND_SAYS.format(release=row.get("release") or "최신 회차", total=row.get("total") or 0,
                                 harmonic_pos=row["harmonic_pos"], pagerank_pos=row.get("pagerank_pos") or 0)
    return NOT_FOUND_SAYS.format(scanned=row.get("scanned") or 0)


def put(conn, pid: int, today: str, row: dict) -> int:
    conn.execute(
        "INSERT INTO backlink_graph_rank(project_id, checked_date, source, release, found, scanned,"
        " total, harmonic_pos, pagerank_pos, hosts) VALUES (?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(project_id, checked_date, source) DO UPDATE SET release=excluded.release,"
        " found=excluded.found, scanned=excluded.scanned, total=excluded.total,"
        " harmonic_pos=excluded.harmonic_pos, pagerank_pos=excluded.pagerank_pos, hosts=excluded.hosts",
        (pid, today, row["source"], row["release"], row["found"], row["scanned"], row["total"],
         row["harmonic_pos"], row["pagerank_pos"], row["hosts"]))
    return 1


FAKE_INFO = [{"id": "cc-main-2026-aug-sep-oct", "crawls": ["CC-MAIN-2026-34"],
              "stats": {"host": {"nodes": 9, "arcs": 9}, "domain": {"nodes": 134979706, "arcs": 9}}},
             {"id": "cc-main-2026-may-jun-jul"}]
FAKE_LINES = ["#harmonicc_pos\t#harmonicc_val\t#pr_pos\t#pr_val\t#host_rev\t#n_hosts",
              "1\t3.1E7\t1\t0.01\tcom.google\t5000",
              "2\t3.0E7\t3\t0.009\tcom.facebook\t900",
              "broken line",
              "3\t2.9E7\t2\t0.0095\tkr.co.example\t12"]


def _selfcheck() -> None:
    assert rev("https://www.Example.co.kr/x") == "kr.co.example"
    assert latest_release(FAKE_INFO) == ("cc-main-2026-aug-sep-oct", 134979706)
    assert latest_release([]) == (None, None) and latest_release({"x": 1}) == (None, None)
    seen = []

    def lines(url, max_bytes):
        seen.append((url, max_bytes))
        return iter(FAKE_LINES)
    r = lookup("example.co.kr", fetch_info=lambda: FAKE_INFO, open_lines=lines)
    assert seen[0][0].endswith("/cc-main-2026-aug-sep-oct/domain/cc-main-2026-aug-sep-oct-domain-ranks.txt.gz")
    assert (r["found"], r["harmonic_pos"], r["pagerank_pos"], r["hosts"], r["scanned"]) == (1, 3, 2, 12, 3), r
    # 꼬리만 같은 남의 도메인(notexample.co.kr)·하위 도메인은 같은 줄이 아니다.
    miss = lookup("ample.co.kr", fetch_info=lambda: FAKE_INFO, open_lines=lines)
    assert (miss["found"], miss["harmonic_pos"], miss["scanned"]) == (0, None, 3), miss
    # 못 찾은 말에는 '낮다'·'약하다'가 없고, 표본이라는 말이 있다.
    t = says(miss)
    assert "표본" in t and "3위" in t and not any(w in t for w in ("낮", "약합", "약한")), t
    assert "3위" in says(r) and "134,979,706" in says(r) and says(None) == ""
    try:
        lookup("a.com", fetch_info=lambda: [], open_lines=lines)
        raise AssertionError("회차를 못 읽었는데 조용히 지나갔다")
    except RuntimeError:
        pass
    print("cc_graph self-check ok")


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--lookup":
        mb = 4 * 1024 * 1024
        got = lookup(sys.argv[2], max_bytes=mb)
        print(got)
        print(says(got))
    else:
        _selfcheck()
