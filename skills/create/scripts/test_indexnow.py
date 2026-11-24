#!/usr/bin/env python3
"""IndexNow 알림 자체점검 — `python test_indexnow.py` (임시 폴더, 네트워크 0).

진짜 IndexNow·사이트를 부르지 않는다: indexnow._get/_post 를 갈아 끼운다(응답 코드의 뜻은
indexnow.py 머리말의 공식 문서 근거). 보는 것은 '언제 안 보내나'다 — 키 없음·키 파일 없음·
아직 안 뜬 주소·이미 보낸 주소.
"""
import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

HOME = Path(tempfile.mkdtemp(prefix="seo-miner-indexnow-test-"))
os.environ["CAPTURE_HOME"] = str(HOME)
os.environ.pop("INDEXNOW_KEY", None)
HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.resolve().parents[1] / "capture" / "scripts"))
import createdb  # noqa: E402
import db  # noqa: E402
import indexnow  # noqa: E402

KEY = "abcd1234-efgh5678"
gets, posts = [], []
site = {"keyfile": KEY, "live": {"https://t.com/a/": 200, "https://t.com/b/": 404}, "post": 200}


def fake_get(url):
    gets.append(url)
    if url.endswith(".txt"):
        return (200, site["keyfile"]) if site["keyfile"] is not None else (404, "")
    return site["live"].get(url, 404), ""


def fake_post(url, body):
    posts.append((url, body))
    return site["post"]


indexnow._get, indexnow._post = fake_get, fake_post


def ledger() -> dict:
    return json.loads(indexnow.ledger_path().read_text(encoding="utf-8"))


# ── 노트 표기 ─────────────────────────────────────────────────────────────
assert indexnow.note_with_urls(None, []) is None
assert indexnow.note_with_urls("메모", None) == "메모"
n = indexnow.note_with_urls("메모", ["https://t.com/a/", "https://t.com/a/", "javascript:x", " https://t.com/b/ "])
assert n == "메모 [url https://t.com/a/] [url https://t.com/b/]", n
assert indexnow.urls_in(n) == ["https://t.com/a/", "https://t.com/b/"]
assert indexnow.urls_in("이미 적용된 수정(커밋 abc12345)") == []

# ── 키 없음: 건너뛰고 사유를 말한다. 장부에도 안 쌓는다(나중에 소급해 보내지 않는다) ─────
r = indexnow.notify("t", [(1, ["https://t.com/a/"])], key="")
assert r["sent"] == 0 and KEY not in str(r) and "INDEXNOW_KEY" in r["skipped"][0], r
assert not posts and not gets and not indexnow.ledger_path().exists()
# 키 꼴이 틀리면 보내지 않는다(8~128자, 영문·숫자·하이픈).
for bad in ("short", "has space 12345", "밑줄_은_안됨_12345", "x" * 129):
    assert "꼴" in indexnow.notify("t", [(1, ["https://t.com/a/"])], key=bad)["skipped"][0], bad
assert not posts

# ── 키 파일 사전 점검: 없거나 내용이 다르면 그 호스트는 안 보낸다 ─────────────────
site["keyfile"] = None
r = indexnow.notify("t", [(1, ["https://t.com/a/"])], key=KEY)
assert r["sent"] == 0 and "키 파일" in r["skipped"][0] and not posts, r
assert KEY not in r["skipped"][0], "사유 문장에 키 전체를 찍지 않는다"
# 키 파일을 나중에 올리면 다음 머지 반영이 다시 본다 — 대기로 남는다.
assert r["pending"] == 1 and ledger()["pending"]["t#1|https://t.com/a/"]["tries"] == 1, r
site["keyfile"] = "other-key-000000"
assert indexnow.notify("t", [(1, ["https://t.com/a/"])], key=KEY)["sent"] == 0 and not posts
site["keyfile"] = "﻿" + KEY + "\n"            # BOM·끝 개행은 같은 키다
assert indexnow.key_file_ok("t.com", KEY)
assert gets[-1] == f"https://t.com/{KEY}.txt"

# ── 보내기: 뜬 주소만, 안 뜬 주소는 대기 ──────────────────────────────────
gets.clear()
r = indexnow.notify("t", [(1, ["https://t.com/a/", "https://t.com/b/"])], key=KEY)
assert (r["sent"], r["pending"]) == (1, 1), r
assert posts == [(indexnow.ENDPOINT, {"host": "t.com", "key": KEY, "urlList": ["https://t.com/a/"]})], posts
led = ledger()
assert led["sent"]["t#1|https://t.com/a/"]["status"] == 200
assert led["pending"]["t#1|https://t.com/b/"]["tries"] == 1
assert "1개 알림" in indexnow.report(r) and "다음에" in indexnow.report(r)

# ── 한 번만: 같은 (기록, 주소)는 다시 안 보낸다. 대기는 다음 호출이 다시 본다 ─────────
posts.clear()
r = indexnow.notify("t", [(1, ["https://t.com/a/"])], key=KEY)
assert r["sent"] == 0 and r["pending"] == 1 and not posts, (r, posts)   # b 는 아직 404
assert ledger()["pending"]["t#1|https://t.com/b/"]["tries"] == 2
site["live"]["https://t.com/b/"] = 200                                # 배포가 떴다
r = indexnow.notify("t", [], key=KEY)
assert r["sent"] == 1 and posts[-1][1]["urlList"] == ["https://t.com/b/"], (r, posts)
assert ledger()["pending"] == {}
# 같은 주소라도 **다른 작업 기록**이면 새 변경이다.
posts.clear()
assert indexnow.notify("t", [(2, ["https://t.com/a/"])], key=KEY)["sent"] == 1 and len(posts) == 1
# 다른 사이트의 대기는 건드리지 않는다.
site["live"]["https://u.com/x"] = 404
indexnow.notify("u", [(1, ["https://u.com/x"])], key=KEY)
posts.clear()
indexnow.notify("t", [], key=KEY)
assert not posts and "u#1|https://u.com/x" in ledger()["pending"]

# ── 끝내 안 뜨면 그만 본다 ────────────────────────────────────────────────
for _ in range(indexnow.RETRY_MAX):
    r = indexnow.notify("u", [], key=KEY)
assert ledger()["pending"] == {} and not posts, ledger()

# ── 거절(403·422·429)도 장부에 남긴다 — 머지 반영마다 같은 요청을 되풀이하지 않는다 ────
site["post"], site["live"]["https://t.com/c/"] = 403, 200
r = indexnow.notify("t", [(3, ["https://t.com/c/"])], key=KEY)
assert r["sent"] == 0 and "키가 유효하지 않음" in r["skipped"][0], r
assert ledger()["sent"]["t#3|https://t.com/c/"]["ok"] is False
posts.clear()
indexnow.notify("t", [(3, ["https://t.com/c/"])], key=KEY)
assert not posts
site["post"] = 202                                                    # 첫 요청의 정상 응답
site["live"]["https://t.com/d/"] = 200
assert indexnow.notify("t", [(4, ["https://t.com/d/"])], key=KEY)["sent"] == 1
# 하위 도메인은 별개 호스트 — 호스트마다 키 파일을 보고 따로 보낸다.
posts.clear(); gets.clear()
site["live"].update({"https://blog.t.com/p": 200, "https://t.com/e/": 200})
indexnow.notify("t", [(5, ["https://blog.t.com/p", "https://t.com/e/"])], key=KEY)
assert sorted(p[1]["host"] for p in posts) == ["blog.t.com", "t.com"], posts
assert f"https://blog.t.com/{KEY}.txt" in gets

# ── createdb 이음매: done --url → 노트 → 머지 확인 → 알림 한 번 ─────────────────
conn = db.connect()
conn.execute("INSERT INTO projects(name, type, domain) VALUES('t','saas','t.com')")
pid = conn.execute("SELECT id FROM projects WHERE name='t'").fetchone()[0]
conn.execute("INSERT INTO opportunities(project_id, kind, target, score, status) "
             "VALUES(?, 'striking_distance', 'kw', 50, 'new')", (pid,))
conn.commit()
oid = conn.execute("SELECT id FROM opportunities").fetchone()[0]
conn.close()


def quiet(fn, *a, **k):
    buf = io.StringIO()
    with redirect_stdout(buf):
        res = fn(*a, **k)
    return res, buf.getvalue()


site["live"]["https://t.com/new/"] = 200
site["post"] = 200
quiet(createdb.done, "t", oid, "src/new.md", "capture/striking_distance-kw", "메모", None,
      ["https://t.com/new/"])
conn = db.connect()
cid, note = conn.execute("SELECT id, note FROM creations ORDER BY id DESC").fetchone()
conn.close()
assert note == "메모 [url https://t.com/new/]", note
posts.clear()
os.environ["INDEXNOW_KEY"] = KEY
import subprocess  # noqa: E402
_orig_gh = createdb._gh
createdb._gh = lambda args, cwd: subprocess.CompletedProcess(
    ["gh"], 0, json.dumps([{"number": 9, "mergedAt": "2026-11-20T00:00:00Z"}]), "")
try:
    # 기록만 했을 때(머지 전)는 알리지 않는다.
    assert not posts
    res, out = quiet(createdb.sync_merged, "t", None)
    assert res["merged"] == 1 and len(posts) == 1, (res, posts)
    assert posts[0][1]["urlList"] == ["https://t.com/new/"] and "IndexNow: 바뀐 주소 1개 알림" in out, out
    # 다시 sync 해도 또 보내지 않는다(기록이 머지 목록에서 빠지고, 장부도 기억한다).
    quiet(createdb.sync_merged, "t", None)
    quiet(createdb.merged, cid, "t")
    assert len(posts) == 1, posts
    # 키가 없으면 머지 반영은 그대로 되고 알림만 건너뛴다.
    os.environ.pop("INDEXNOW_KEY")
    quiet(createdb.done, "t", oid, "src/x.md", "capture/striking_distance-x", None, None,
          ["https://t.com/x/"])
    res, out = quiet(createdb.sync_merged, "t", None)
    assert res["merged"] == 1 and len(posts) == 1 and "INDEXNOW_KEY 가 없어" in out, (res, out)
    # 알림이 터져도 머지 반영은 성공이다.
    os.environ["INDEXNOW_KEY"] = KEY
    quiet(createdb.done, "t", oid, "src/y.md", "capture/striking_distance-y", None, None,
          ["https://t.com/y/"])
    boom = indexnow.notify
    indexnow.notify = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down"))
    try:
        res, out = quiet(createdb.sync_merged, "t", None)
    finally:
        indexnow.notify = boom
    assert res["merged"] == 1 and "IndexNow 건너뜀: RuntimeError" in out, (res, out)
finally:
    createdb._gh = _orig_gh
    os.environ.pop("INDEXNOW_KEY", None)

# 준비 상태 명부에 자리가 있다(키 이름이 두 벌이 되지 않게 명부의 문구가 이 상수를 쓴다).
sys.path.insert(0, str(HERE.resolve().parents[1] / "setup" / "scripts"))
import doctor  # noqa: E402
cap = next((c for c in doctor.CAPABILITIES if c["id"] == "indexnow"), None)
assert cap and indexnow.KEY_ENV in cap["fix"], cap

print(f"ok — IndexNow: 키·키 파일 사전 점검, 뜬 주소만 한 번, 대기·거절 장부, createdb 머지 이음매 ({HOME})")
