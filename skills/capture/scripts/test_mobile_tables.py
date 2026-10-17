#!/usr/bin/env python3
"""모바일 폭(390px)에서 표가 옆으로 안 밀리고, 글자 버튼의 눌리는 면이 손가락만 한가.

test_mobile.py 의 크롬·CDP 헬퍼를 그대로 쓴다(기존 검사는 건드리지 않는다 — 여기는 전용 파일).

보는 것(390px, 로컬·호스팅 둘 다):
  · 표(.tw) 가 옆으로 안 민다 — scrollWidth <= clientWidth. 좁은 화면에서는 `cls="cards"`
    (줄을 카드로 쌓는다)로 접힌다. 카드로 접힌 표는 머리글을 숨기므로, 칸마다 data-label 이
    달려 있어야 정보가 안 사라진다.
  · 문서가 가로로 안 넘친다.
  · 글자 버튼(.gov·.lnk·.badge 버튼)의 **눌리는 면**이 40px 이상이다. 보이는 크기는 작아도
    ::after 로 넓힌 면이 눌리므로, 요소 높이가 아니라 elementFromPoint 로 실제 눌리는 높이를 잰다.
1280px 에서는 같은 표가 카드로 안 접히고(머리글이 보인다) 옆으로도 안 민다 — 회귀 없음.
"""
from __future__ import annotations

import gc
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_mobile as M  # noqa: E402
R = M.R

# (화면, 탭) — 표가 있는 화면들. 화면 목록의 정본은 view-def 이라 여기는 고른 것만 적는다.
TABLE_VIEWS = (("keywords", "work"), ("keywords", "manage"), ("analysis", "work"),
               ("analysis", "manage"), ("rank", None), ("history", None), ("overview", None))
MIN_HIT = 40

TABLES = r"""(function () {
  var box = document.querySelector('.view:not([hidden])'), out = [];
  [].slice.call(box.querySelectorAll('.tw')).forEach(function (e) {
    var r = e.getBoundingClientRect();
    if (!r.width || !r.height) return;
    var head = e.querySelector('thead'), cards = e.classList.contains('cards');
    var miss = 0;
    if (cards) [].slice.call(e.querySelectorAll('tbody > tr:not(.exp)')).forEach(function (tr) {
      [].slice.call(tr.cells).forEach(function (td, i) {
        if (i && !td.hasAttribute('colspan') && !td.dataset.label) miss++;
      });
    });
    out.push({id: e.parentElement.id || e.parentElement.className || e.id, cards: cards,
              sw: e.scrollWidth, cw: e.clientWidth,
              head: head ? getComputedStyle(head).display : 'none', miss: miss});
  });
  return {doc: Math.max(document.documentElement.scrollWidth, document.body.scrollWidth), t: out};
})()"""

# 눌리는 면: 버튼 가운데 x 에서 위아래로 훑어, 그 버튼(또는 그 안)이 눌리는 연속 높이를 잰다.
HITS = r"""(function () {
  document.querySelectorAll('.odr-x,.sdr-x').forEach(function (x) { x.click(); });
  var box = document.querySelector('.view:not([hidden])'), out = [];
  [].slice.call(box.querySelectorAll('button.gov, button.lnk, button.badge')).forEach(function (b) {
    var r0 = b.getBoundingClientRect();
    if (!r0.width || !r0.height || getComputedStyle(b).visibility === 'hidden') return;
    // 접힌 <details> 안은 안 그려져 눌러 볼 수 없다 — 사람이 펼쳤을 때를 잰다.
    for (var d = b.closest('details'); d; d = d.parentElement && d.parentElement.closest('details')) d.open = true;
    // 위(고정 머리)·아래(알림 띄움)에 가려지지 않게 화면 윗부분에 놓고 잰다.
    window.scrollTo({top: window.scrollY + b.getBoundingClientRect().top - 300, behavior: 'instant'});
    var r = b.getClientRects()[0], x = r.left + r.width / 2, lo = null, hi = null;
    for (var y = Math.floor(r.top) - 60; y <= Math.ceil(r.bottom) + 60; y++) {
      if (y < 0 || y >= innerHeight) continue;
      var el = document.elementFromPoint(x, y);
      if (el && (el === b || b.contains(el))) { if (lo === null) lo = y; hi = y; }
    }
    out.push({txt: b.textContent.trim().slice(0, 20), cls: b.className, h: Math.round(r.height),
              hit: lo === null ? 0 : hi - lo + 1});
  });
  return out;
})()"""


def run() -> None:
    exe = R.find_browser()
    if not exe:
        print("크롬/엣지가 없어 건너뜁니다")
        return
    home = Path(tempfile.mkdtemp(prefix="seo-miner-mobtab-"))
    import doctor
    for k in (doctor.MODE_ENV, doctor.TOOL_ENV, doctor.TERMINAL_ENV):
        os.environ.pop(k, None)
    fails: list[str] = []
    shots = os.environ.get("MOBTAB_SHOTS")
    try:
        R.fixture(home)
        R.zero_site(home)

        def visit(name, b, base, w, h):
            have = {v["id"]: v for v in M.views_in_page(b)}
            narrow = w <= 560
            for vid, mode in TABLE_VIEWS:
                if vid not in have:
                    fails.append(f"{name} {w}px: 화면 {vid} 가 매니페스트에 없다 — 이 검사의 목록이 낡았다")
                    continue
                if mode and mode not in have[vid]["modes"]:
                    fails.append(f"{name} {w}px: {vid} 에 탭 {mode} 가 없다 — 이 검사의 목록이 낡았다")
                    continue
                label = f"{name} {w}px {vid}" + (f"[{mode}]" if mode else "")
                M.open_view(b, vid, mode)
                m = b.js(TABLES)
                if shots:
                    b.shot_full(Path(shots) / f"{name}-{w}-{vid}-{mode}.png")
                if m["doc"] > w + M.SLACK:
                    fails.append(f"{label}: 문서가 가로로 넘친다 {m['doc']} > {w}")
                for t in m["t"]:
                    if narrow and t["sw"] > t["cw"] + M.SLACK:
                        fails.append(f"{label} #{t['id']}: 표가 옆으로 밀린다 {t['sw']}>{t['cw']}"
                                     " — table(..., \"cards\") 로 접는다")
                    if t["cards"] and narrow and t["head"] != "none":
                        fails.append(f"{label} #{t['id']}: 카드 표인데 머리글이 안 접혔다")
                    if t["cards"] and not narrow and t["head"] == "none":
                        fails.append(f"{label} #{t['id']}: 넓은 화면인데 카드 표 머리글이 접혔다")
                    if t["miss"]:
                        fails.append(f"{label} #{t['id']}: 카드 표 칸 {t['miss']}개에 라벨(data-label)이 없다"
                                     " — 머리글이 접히면 무슨 값인지 사라진다")
                if narrow:
                    for x in b.js(HITS):
                        if x["hit"] < MIN_HIT:
                            fails.append(f"{label}: 글자 버튼 \"{x['txt']}\" 눌리는 면 {x['hit']}px"
                                         f" (보이는 높이 {x['h']}px, .{x['cls']}) — 40px 이상이어야 한다")

        M.run_matrix(home, exe, [M.PHONE, M.DESKTOP], visit)
    finally:
        gc.collect()
        shutil.rmtree(home, ignore_errors=True)
    assert not fails, "표·눌리는 면 검사 실패:\n  " + "\n  ".join(fails)


if __name__ == "__main__":
    run()
    print("mobile tables self-check ok")
