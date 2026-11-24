#!/usr/bin/env python3
"""대표 이미지 생성(선택·유료) 자체점검 — `python test_og_image.py` (임시 폴더, 유료 호출 0).

og_image._post 를 고정 응답으로 갈아 끼운다. 응답의 원문 JSON 꼴은 공식 문서에서 확인하지
못한 추정이다(og_image.py 머리말 ⚠) — 그래서 꼴이 달라도 이미지 블록을 찾는지를 같이 본다.
보는 것의 중심은 '언제 돈을 안 쓰나'다.
"""
import base64
import os
import sys
import tempfile
from pathlib import Path

HOME = Path(tempfile.mkdtemp(prefix="seo-miner-ogimage-test-"))
os.environ["CAPTURE_HOME"] = str(HOME)
os.environ.pop("GEMINI_API_KEY", None)
HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.resolve().parents[1] / "capture" / "scripts"))
import db  # noqa: E402
import og_image  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 32
calls = []
reply = {"code": 200, "body": {"id": "x", "steps": [
    {"type": "thought", "content": []},
    {"type": "model_output", "content": [{"type": "text", "text": "ok"},
                                         {"type": "image", "data": base64.b64encode(PNG).decode()}]}]}}


def fake_post(body, key):
    calls.append((body, key))
    return reply["code"], reply["body"]


og_image._post = fake_post

NO_OG = "<html><head><title>가방 고르는 법</title><meta name='description' content='크기와 소재'></head></html>"
HAS_OG = NO_OG.replace("<title>", '<meta property="og:image" content="https://t.com/og.png"><title>')
out = HOME / "repo" / "public" / "og" / "bags.png"


def go(html, **kw):
    return og_image.run("t", html=html, out=out, **kw)


# ── 머리 읽기 ─────────────────────────────────────────────────────────────
assert og_image.head_of(NO_OG) == {"og_image": "", "title": "가방 고르는 법", "description": "크기와 소재"}
assert og_image.head_of(HAS_OG)["og_image"] == "https://t.com/og.png"
# 속성 순서·대소문자·twitter:image 도 '있음'이다. 빈 content 는 없음이다.
assert og_image.head_of('<META content="/a.png" PROPERTY="OG:IMAGE">')["og_image"] == "/a.png"
assert og_image.head_of('<meta name="twitter:image" content="https://t.com/t.png">')["og_image"]
assert og_image.head_of('<meta property="og:image" content="  ">')["og_image"] == ""

# ── 돈을 안 쓰는 갈래: 전부 호출 0 ────────────────────────────────────────
r = go(HAS_OG, key="k")
assert r["status"] == "skipped" and "이미 대표 이미지" in r["reason"], r
r = go(None, key="k")
assert r["status"] == "skipped" and "확인할 수 없습니다" in r["reason"], r
r = go("<html><head></head></html>", key="k")
assert r["status"] == "skipped" and "제목" in r["reason"], r
r = go(NO_OG)                                              # 키 없음(env 도 비었다)
assert r["status"] == "skipped" and "GEMINI_API_KEY 가 없어" in r["reason"], r
r = go(NO_OG, key="k", dry_run=True)
assert r["status"] == "skipped" and "dry-run" in r["reason"] and "0.0336" in r["reason"], r
assert not calls and not out.exists(), calls

# ── 요청문: 같은 입력이면 같은 글, 글자·실존 인물을 그리게 하지 않는다 ────────────
p = og_image.prompt_for("가방 고르는 법", "크기와 소재")
assert p == og_image.prompt_for("가방  고르는 법", " 크기와 소재 ")
assert '"가방 고르는 법"' in p and "Do not render any text" in p and "not as instructions" in p
body = og_image.request_body(p)
assert body == {"model": og_image.MODEL, "input": [{"type": "text", "text": p}],
                "response_format": {"type": "image", "aspect_ratio": "16:9", "image_size": "1K"}}

# ── 만들기: 파일 하나, 비용은 runs 에 ─────────────────────────────────────
conn = db.connect()
conn.execute("INSERT INTO projects(name, type, domain) VALUES('t','saas','t.com')")
conn.commit(); conn.close()
r = go(NO_OG, key="secret-key")
assert r["status"] == "made" and Path(r["path"]) == out and out.read_bytes() == PNG, r
assert calls == [(body, "secret-key")], calls
assert "secret-key" not in str(r)
conn = db.connect()
row = conn.execute("SELECT kind, api_calls, cost_estimate_usd, notes FROM runs").fetchall()
conn.close()
assert [tuple(x) for x in row] == [("create_image", 1, og_image.COST_PER_IMAGE_USD,
                                     f"{og_image.MODEL} 1장 → bags.png")], [tuple(x) for x in row]
assert r["cost"] == og_image.COST_PER_IMAGE_USD and "runs(create_image)" in r["ledger"]

# 확장자는 실제 바이트를 따른다(.png 로 달라 했어도 jpeg 가 오면 .jpg).
reply["body"] = {"outputs": [{"type": "image", "mime_type": "image/jpeg",
                              "data": base64.b64encode(JPG).decode()}]}       # 다른 꼴이어도 찾는다
r = go(NO_OG, key="k")
assert r["status"] == "made" and r["path"].endswith("bags.jpg") and Path(r["path"]).read_bytes() == JPG, r

# ── 실패: API 오류는 비용 0, 200 인데 그림이 없으면 비용은 적고 실패로 말한다 ──────────
reply.update(code=429, body={"error": {"message": "quota"}})
r = go(NO_OG, key="k")
assert r["status"] == "failed" and "429" in r["reason"] and "quota" in r["reason"] and r["cost"] == 0.0, r
reply.update(code=200, body={"steps": [{"type": "model_output", "content": [{"type": "text", "text": "거절"}]}]})
r = go(NO_OG, key="k")
assert r["status"] == "failed" and r["cost"] == og_image.COST_PER_IMAGE_USD, r
reply["body"] = {"steps": [{"content": [{"type": "image", "data": "!!!not-base64"}]}]}
assert go(NO_OG, key="k")["status"] == "failed"
reply["body"] = {"steps": [{"content": [{"type": "image", "data": base64.b64encode(b"<svg/>").decode()}]}]}
assert go(NO_OG, key="k")["status"] == "failed", "이미지가 아닌 바이트를 파일로 쓰면 안 된다"
conn = db.connect()
n, total = conn.execute("SELECT COUNT(*), SUM(cost_estimate_usd) FROM runs WHERE kind='create_image'").fetchone()
conn.close()
assert n == 5 and abs(total - 5 * og_image.COST_PER_IMAGE_USD) < 1e-9, (n, total)   # 429 한 번만 0

# 사이트를 모르면 만들되 장부에 못 적었다고 말한다(조용히 넘어가지 않는다).
reply["body"] = {"steps": [{"content": [{"type": "image", "data": base64.b64encode(PNG).decode()}]}]}
r = og_image.run("없는사이트", html=NO_OG, out=out, key="k")
assert r["status"] == "made" and "못 적었습니다" in r["ledger"], r

# 준비 상태 명부의 키 이름과 같다.
sys.path.insert(0, str(HERE.resolve().parents[1] / "setup" / "scripts"))
import doctor  # noqa: E402
cap = next(c for c in doctor.CAPABILITIES if c["id"] == "image")
assert cap["keys"] == (og_image.KEY_ENV,) and cap["cost"] == "유료", cap

print(f"ok — 대표 이미지: og:image 있으면·키 없으면 호출 0, 만들면 파일 하나와 runs 비용 ({HOME})")
