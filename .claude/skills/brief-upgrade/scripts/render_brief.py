"""기회 하나의 요청문을 **지금 작업 트리의 brief.py** 로 다시 뽑는다.

호스팅 사이트(remote.owns)는 화면이 보여 주는 요청문을 서버가 만든다 — 로컬에서 brief.py 를
고쳐도 배포 전에는 화면에 안 보인다. 그래서 호스팅의 페이로드(/api/data)를 받아 로컬 코드로
brief.attach 를 다시 돌린다. 로컬 사이트는 dashboard.gather 로 같은 페이로드를 만든다.

    python render_brief.py <project> <opp_id> [--out 파일] [--payload 캐시.json]

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


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("project")
    ap.add_argument("opp_id", type=int)
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
    opp = next((o for o in d.get("opps") or [] if int(o.get("id") or -1) == a.opp_id), None)
    if opp is None:
        ids = sorted(int(o["id"]) for o in d.get("opps") or [] if o.get("id") is not None)
        sys.exit(f"기회 #{a.opp_id} 가 페이로드에 없습니다 (있는 것: {ids[:40]}…). "
                 "닫혔거나 다른 사이트의 번호일 수 있습니다.")
    # 서버가 실은 o.brief·o.play 는 옛 코드의 것 — 지우고 로컬 코드로 다시 단다
    for o in d.get("opps") or []:
        o.pop("brief", None)
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
