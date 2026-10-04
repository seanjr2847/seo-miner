"""기회 하나의 요청문을 **지금 작업 트리의 brief.py** 로 다시 뽑는다.

호스팅 사이트(remote.owns)는 화면이 보여 주는 요청문을 서버가 만든다 — 로컬에서 brief.py 를
고쳐도 배포 전에는 화면에 안 보인다. 그래서 호스팅의 페이로드(/api/data)를 받아 로컬 코드로
brief.attach 를 다시 돌린다. 로컬 사이트는 dashboard.gather 로 같은 페이로드를 만든다.

    python render_brief.py <project> <opp_id> [--out 파일] [--payload 캐시.json]
    python render_brief.py <project> --list [--payload 캐시.json]

--list 는 열린 기회(new·acked, --all 이면 닫힌 것도)를 (종류 · 꼴 · 갈래)별로 묶어 번호를 보여 준다 — 붙여 넣은 요청문 없이
"알아서" 훑을 때 갈래마다 대표 하나씩 고르는 데 쓴다.

--payload 를 주면 첫 실행에 받은 페이로드를 그 파일에 저장하고, 다음부터는 그걸 읽는다 —
고치기 전/후를 같은 데이터로 견주려면 꼭 준다(그 사이 호스팅에 런이 돌면 데이터가 바뀐다).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "skills" / "capture" / "scripts"))

import brief  # noqa: E402


def _payload(project: str) -> dict:
    import remote
    if remote.owns(project):
        return remote.api("GET", "/api/data", params={"project": project})
    import dashboard
    import db
    conn = db.connect()
    return dashboard.gather(conn, db.get_project(conn, project))


def _relocal(d: dict) -> None:
    """서버가 실은 요청문·처방은 옛 코드의 것 — 지우고 처방은 로컬 scoring 으로 다시 단다.
    (기회의 '왜 걸렸나'는 기회가 설 때 DB 에 박힌 문장이라 여기서 못 바꾼다 — 새 기회부터 바뀐다.)"""
    import scoring
    for o in d.get("opps") or []:
        o.pop("brief", None)
        o["play"] = scoring.kind_play(o["kind"], band=o.get("band"), gap_kind=o.get("gap_kind"))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("project")
    ap.add_argument("opp_id", type=int, nargs="?")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--all", action="store_true", help="--list 에 닫힌 기회(resolved·done·dismissed)도")
    ap.add_argument("--out")
    ap.add_argument("--payload")
    a = ap.parse_args()

    cache = Path(a.payload) if a.payload else None
    if cache and cache.exists():
        d = json.loads(cache.read_text(encoding="utf-8"))
    else:
        d = _payload(a.project)
        if cache:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")

    locale = str((d.get("project") or {}).get("locale") or "ko-KR")
    if a.list or a.opp_id is None:
        _relocal(d)
        brief.attach(d, locale)
        groups: dict[tuple, list] = {}
        # 열린 기회만(new·acked) — 닫힌 기회로 진단하면 이미 닫힌 일을 고친다
        live = [o for o in d.get("opps") or [] if a.all or o.get("status") in ("new", "acked")]
        for o in live:
            key = (o["kind"], o["brief"]["shape"], str(o.get("gap_kind") or o.get("band") or "-"),
                   "page" if o["brief"]["page"] else ("cands" if o["brief"]["candidates"] else "none"))
            groups.setdefault(key, []).append(o)
        for key, os_ in sorted(groups.items()):
            ex = ", ".join(f"#{o.get('id')} {str(o.get('target'))[:30]}" for o in os_[:3])
            print(f"{len(os_):>3}  {' · '.join(key)}  —  {ex}")
        return
    opp = next((o for o in d.get("opps") or [] if int(o.get("id") or -1) == a.opp_id), None)
    if opp is None:
        ids = sorted(int(o["id"]) for o in d.get("opps") or [] if o.get("id") is not None)
        sys.exit(f"기회 #{a.opp_id} 가 페이로드에 없습니다 (있는 것: {ids[:40]}…). "
                 "닫혔거나 다른 사이트의 번호일 수 있습니다.")
    _relocal(d)
    brief.attach(d, locale)
    text = brief.text(opp, d, locale)
    head = f"<!-- {a.project} #{a.opp_id} · shape={opp['brief']['shape']} · locale={locale} -->\n"
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(head + text, encoding="utf-8", newline="\n")
        print(a.out)
    else:
        sys.stdout.write(head + text)


if __name__ == "__main__":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    main()
