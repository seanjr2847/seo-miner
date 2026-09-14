#!/usr/bin/env python3
"""자체점검 — `python test_charts.py` (차트 헬퍼가 만드는 척도를 node 로 되읽는다).

`test_render.py` 는 차트가 **섰는지**(canvas data-ch-ok)만 본다. 선 차트가 **읽히는지**는
안 본다. 클릭 변화 워터폴이 그 틈에서 살았다: 축 바닥이 0 에 못 박혀 있어서 이전(710)·
현재(736) 기둥이 화면을 다 먹고, 정작 원인인 델타 넷(+19.4 +5.6 +2 -1)은 1~4px 로
눌렸다. DOM 에는 차트가 멀쩡히 있어서 아무도 몰랐다.

그 뒤 차트는 발산 가로 막대(chDiverge)로 바뀌었다(ce08519) — 합계는 그리지 않고 원인만
0 선 좌우로 세운다. 캔버스(Chart.js)는 node 에서 못 그리므로, 헬퍼가 Chart 에 넘기는
설정(값·x 축 min/max)을 되읽어 막대 길이를 축 폭에 대한 비율로 잰다. 같은 틈이 다시
열리는지 — 가장 큰 원인이 가장 긴 막대인지, 가장 작은 원인도 눈에 남는지 — 를 본다.
"""
import json
import re
import subprocess
import sys
from pathlib import Path

SHELL = Path(__file__).resolve().parents[1] / "templates" / "dashboard.html"

# 헬퍼가 기대는 것만 세운다 — 셸 전체를 node 에 올리면 DOM 이 없어 죽는다.
# chMount 는 캔버스 대신 설정을 짓는 함수(build)를 돌려준다.
STUBS = """
const esc = s => String(s);
const num = v => String(Math.round(v * 10) / 10);
const chCat = () => ({});
const chTip = () => ({});
const chMount = (build, aria) => ({build, aria});
const window = {};
const T = {patina: "#22705F", copper: "#A9491F", ink: "#121714", slate: "#576259", rule: "#ccc"};
"""


def diverge_src() -> str:
    """셸에서 chDiverge 한 덩어리만 떼 온다 (사본을 만들면 두 벌이 된다)."""
    src = SHELL.read_text(encoding="utf-8")
    m = re.search(r"^window\.chDiverge = .*?^\};", src, re.M | re.S)
    assert m, "dashboard.html 에서 chDiverge 를 못 찾았다"
    return m.group(0)


def bars(rows: list[dict]) -> dict[str, float]:
    """이름 → 막대 길이(x 축 폭에 대한 비율, 0~1)."""
    js = (STUBS + diverge_src()
          + f"\nconst rows = {json.dumps(rows, ensure_ascii=False)};\n"
          + """
const cfg = window.chDiverge(rows).build(T);
const {min, max} = cfg.options.scales.x;
const out = {min, max, len: Object.fromEntries(cfg.data.labels.map((l, i) =>
  [l, Math.abs(cfg.data.datasets[0].data[i] || 0) / (max - min)]))};
console.log(JSON.stringify(out));
""")
    r = subprocess.run(["node", "-e", js], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    assert r.returncode == 0, f"node 가 못 돌렸다:\n{r.stderr[-800:]}"
    out = json.loads(r.stdout)
    assert out["max"] > out["min"], f"x 축 폭이 0 이하다: {out['min']}~{out['max']}"
    return out["len"]


# 실측 모양 — theotherskin 2026-09-02: 이전 710 → 현재 736, 원인 합 +26.
REAL = [
    {"label": "노출 효과", "value": 19.4},
    {"label": "클릭률 효과", "value": 5.6},
    {"label": "새 검색어", "value": 2},
    {"label": "빠진 검색어", "value": -1},
]


def test_causes_are_visible():
    # 390px 화면에서 축 폭이 300px 안팎이다 — 3% 면 9px, 그 아래는 실오라기다.
    h = bars(REAL)
    assert h["노출 효과"] >= 0.5, f"가장 큰 원인이 축의 {h['노출 효과']:.0%} 다 — 무언가에 눌렸다"
    assert h["빠진 검색어"] >= 0.03, f"가장 작은 원인이 축의 {h['빠진 검색어']:.1%} 다 — 안 보인다"


def test_longest_bar_is_the_biggest_cause():
    h = bars(REAL)
    order = sorted(h, key=lambda k: -h[k])
    assert order == ["노출 효과", "클릭률 효과", "새 검색어", "빠진 검색어"], \
        f"길이 순서가 크기 순서와 다르다: {order}"


def test_all_zero_does_not_divide_by_zero():
    flat = [{"label": "노출 효과", "value": 0}, {"label": "클릭률 효과", "value": 0}]
    assert all(v == 0 for v in bars(flat).values()), "값이 전부 0 이면 막대가 없어야 한다"


if __name__ == "__main__":
    if not __import__("shutil").which("node"):
        print("node 가 없어 건너뜁니다")
        sys.exit(0)
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)} passed")
