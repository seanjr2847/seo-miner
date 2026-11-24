#!/usr/bin/env python3
"""IndexNow — create 작업이 **머지된 것을 확인한 뒤** 바뀐 URL 을 한 번 알린다.

부르는 자리는 createdb 의 머지 반영(sync·sync_merged·merged) 하나다. 무료이고 키는 사이트
주인이 스스로 짓는다(발급처가 없다) — `INDEXNOW_KEY`(~/.capture/env 또는 환경변수)가 없으면
건너뛰고 사유를 한 줄 찍는다. 실패가 머지 반영을 막지 않는다.

보내기 전에 코드가 확인하는 것(전부 결정적):
  1. 키 꼴(8~128자, 영문·숫자·하이픈)
  2. 키 파일 — https://<host>/<key>.txt 를 GET 해서 본문이 키와 같은지. 다르면 그 호스트는 안 보낸다
     (보내 봐야 403 이다). 하위 도메인은 별개 호스트라 각자 키 파일이 있어야 한다.
  3. URL 이 지금 200 인지 — 머지는 배포가 아니다. 아직 안 떴으면 '대기'로 장부에 두고 다음
     머지 반영 때 다시 본다(RETRY_MAX 번까지).
  4. 같은 (작업 기록, URL) 은 한 번만 — 장부($CAPTURE_HOME/indexnow.json)가 기억한다.

URL 은 작업 기록 노트의 `[url https://…]` 표기에서 읽는다(`createdb.py done … --url <주소>` 가
적는다). 파일 경로에서 공개 주소를 추측하지 않는다 — 스택마다 규칙이 달라 틀린 주소를 알리게 된다.

프로토콜 근거(2026-11-24 확인, https://www.indexnow.org/documentation · https://www.indexnow.org/faq):
  - POST https://api.indexnow.org/indexnow, `Content-Type: application/json; charset=utf-8`,
    본문 host·key·urlList(keyLocation 은 키 파일이 루트가 아닐 때만). 한 번에 10,000개까지.
  - 한 엔드포인트에 보내면 참여 엔진 전체에 공유된다("will be shared across all IndexNow-enabled
    search engines"). 참여 엔진 목록은 https://www.indexnow.org/searchengines.json 이 정본이다 —
    여기 이름을 적어 두지 않는다(낡는다).
  - 키: "8 to 128 characters", a-z·A-Z·0-9·하이픈. 키 파일은 UTF-8, 본문이 키와 정확히 같아야 한다.
  - 응답: 200 받음 · 202 받음(키 확인 대기 — 첫 요청에 정상) · 400 형식 · 403 키 무효 ·
    422 호스트에 안 속한 URL · 429 너무 잦음.
  - 바뀌지 않은 URL 을 다시 보내지 않는다("Do not resubmit unchanged URLs"), 지난 변경을 소급해
    보내는 용도가 아니다 — 그래서 '이번에 머지를 확인한 기록'만 보낸다.
"""
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "capture" / "scripts"))
import db  # noqa: E402  (CAPTURE_HOME·env 로딩의 주인)

ENDPOINT = "https://api.indexnow.org/indexnow"
KEY_ENV = "INDEXNOW_KEY"
KEY_RE = re.compile(r"^[A-Za-z0-9-]{8,128}$")
BATCH_MAX = 10_000
RETRY_MAX = 5              # 머지 뒤 배포가 안 뜬 URL 을 다시 볼 횟수(머지 반영 한 번 = 한 번)
TIMEOUT = 10
UA = "seo-miner-create"
# 응답 코드 → (받았나, 사람이 읽을 말). 표 밖의 코드는 실패로 치고 코드를 그대로 적는다.
STATUS = {200: (True, "받음"), 202: (True, "받음(키 확인 대기)"),
          400: (False, "요청 형식 오류"), 403: (False, "키가 유효하지 않음"),
          422: (False, "이 호스트의 주소가 아니거나 키가 규칙과 다름"),
          429: (False, "너무 잦은 요청")}

URL_NOTE = re.compile(r"\[url (https?://[^\s\]]+)\]")


def urls_in(note: str | None) -> list[str]:
    """작업 기록 노트에서 `[url …]` 로 적힌 공개 주소(순서 유지, 중복 제거)."""
    return list(dict.fromkeys(URL_NOTE.findall(note or "")))


def note_with_urls(note: str | None, urls: list[str] | None) -> str | None:
    """노트 끝에 `[url …]` 를 붙인다. 주소가 없으면 노트 그대로(None 도 그대로)."""
    good = [u.strip() for u in (urls or []) if re.match(r"^https?://[^\s\]]+$", (u or "").strip())]
    if not good:
        return note
    return " ".join([note] if note else []) + (" " if note else "") + \
        " ".join(f"[url {u}]" for u in dict.fromkeys(good))


def ledger_path() -> Path:
    return db.CAPTURE_HOME / "indexnow.json"


def _load() -> dict:
    try:
        d = json.loads(ledger_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        d = {}
    return {"sent": dict(d.get("sent") or {}), "pending": dict(d.get("pending") or {})}


def _save(led: dict) -> None:
    p = ledger_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(led, ensure_ascii=False, indent=1), encoding="utf-8")


def _get(url: str) -> tuple[int, str]:
    """GET 한 번 → (상태, 본문). 검사가 이 함수를 갈아 끼운다 — 진짜 네트워크를 안 부른다."""
    import requests
    r = requests.get(url, timeout=TIMEOUT, headers={"User-Agent": UA}, allow_redirects=True)
    return r.status_code, r.text


def _post(url: str, body: dict) -> int:
    """POST 한 번 → 상태. 검사가 갈아 끼운다."""
    import requests
    r = requests.post(url, data=json.dumps(body).encode("utf-8"), timeout=TIMEOUT,
                      headers={"Content-Type": "application/json; charset=utf-8", "User-Agent": UA})
    return r.status_code


def key_file_ok(host: str, key: str) -> bool:
    """https://<host>/<key>.txt 본문이 키와 같은가. 못 읽으면 False(보내지 않는다)."""
    try:
        code, body = _get(f"https://{host}/{key}.txt")
    except Exception:   # noqa: BLE001 — 네트워크가 어떻게 죽든 '확인 못 함'이다
        return False
    return code == 200 and body.lstrip("﻿").strip() == key


def _live(url: str) -> bool:
    try:
        return _get(url)[0] == 200
    except Exception:   # noqa: BLE001
        return False


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def notify(project: str, items: list[tuple[int, list[str]]], *, key: str | None = None) -> dict:
    """이번에 머지를 확인한 (작업 기록 id, [주소]) 를 알린다. 지난번에 '대기'로 남긴 것도 같이 본다.

    반환 {"sent": 보낸 주소 수, "pending": 아직 안 떠서 미룬 수, "skipped": 사유 문장들}.
    """
    out = {"sent": 0, "pending": 0, "skipped": []}
    led = _load()
    want: dict[str, dict] = {}                    # 장부 키 → {url, creation, tries}
    for k, v in led["pending"].items():
        if v.get("project") == project:
            want[k] = dict(v)
    for cid, urls in items:
        for u in urls:
            k = f"{project}#{cid}|{u}"
            if k not in led["sent"] and k not in want:
                want[k] = {"project": project, "creation": cid, "url": u, "tries": 0}
    if not want:
        return out
    key = (key if key is not None else os.environ.get(KEY_ENV, "")).strip()
    if not key:
        # 키가 없을 때는 대기로도 안 쌓는다 — 나중에 키를 넣었다고 지난 변경을 소급해 보내지 않는다.
        out["skipped"].append(f"{KEY_ENV} 가 없어 IndexNow 알림을 건너뜁니다(주소 {len(want)}개)")
        return out
    if not KEY_RE.match(key):
        out["skipped"].append(f"{KEY_ENV} 꼴이 규칙과 다릅니다(8~128자, 영문·숫자·하이픈)")
        return out

    by_host: dict[str, list[str]] = {}
    for k, v in want.items():
        host = (urlsplit(v["url"]).hostname or "").lower()
        by_host.setdefault(host, []).append(k)
    for host, keys in by_host.items():
        if not host or not key_file_ok(host, key):
            out["skipped"].append(f"{host or '(주소 없음)'}: 키 파일 https://{host}/{key[:4]}….txt 가 "
                                  f"없거나 내용이 키와 달라 보내지 않습니다(주소 {len(keys)}개)")
            # 키 파일을 올린 뒤의 머지 반영이 다시 보게 대기로 둔다(안 뜬 주소와 같은 횟수 상한).
            for k in keys:
                v = want[k]
                v["tries"] = int(v.get("tries") or 0) + 1
                if host and v["tries"] < RETRY_MAX:
                    led["pending"][k] = v
                    out["pending"] += 1
                else:
                    led["pending"].pop(k, None)
            continue
        ready = []
        for k in keys:
            v = want[k]
            if _live(v["url"]):
                ready.append(k)
                continue
            v["tries"] = int(v.get("tries") or 0) + 1
            if v["tries"] >= RETRY_MAX:
                led["pending"].pop(k, None)
                out["skipped"].append(f"{v['url']}: {RETRY_MAX}번 봐도 200 이 아니라 알리지 않습니다")
            else:
                led["pending"][k] = v
                out["pending"] += 1
        for i in range(0, len(ready), BATCH_MAX):
            chunk = ready[i:i + BATCH_MAX]
            body = {"host": host, "key": key, "urlList": [want[k]["url"] for k in chunk]}
            try:
                code = _post(ENDPOINT, body)
            except Exception as e:   # noqa: BLE001
                out["skipped"].append(f"{host}: 전송 실패({type(e).__name__}) — 다음 머지 반영 때 다시 봅니다")
                for k in chunk:
                    led["pending"][k] = want[k]
                continue
            ok, word = STATUS.get(code, (False, f"HTTP {code}"))
            for k in chunk:
                led["pending"].pop(k, None)
                # 실패도 장부에 남긴다 — 같은 요청을 머지 반영마다 되풀이하지 않는다(429·403).
                led["sent"][k] = {"url": want[k]["url"], "at": _now(), "status": code, "ok": ok}
            if ok:
                out["sent"] += len(chunk)
            else:
                out["skipped"].append(f"{host}: IndexNow 가 받지 않았습니다 — {word}(주소 {len(chunk)}개)")
    _save(led)
    return out


def report(res: dict) -> str:
    """사람이 읽을 한두 줄. 할 말이 없으면 빈 문자열."""
    lines = []
    if res["sent"] or res["pending"]:
        lines.append(f"IndexNow: 바뀐 주소 {res['sent']}개 알림"
                     + (f", {res['pending']}개는 아직 안 떠서 다음에 다시 봅니다" if res["pending"] else ""))
    lines += [f"IndexNow 건너뜀: {s}" for s in res["skipped"]]
    return "\n".join(lines)


if __name__ == "__main__":
    # 사전 점검만 한다(보내지 않는다): python indexnow.py check <host>
    for s in (sys.stdout, sys.stderr):
        s.reconfigure(encoding="utf-8", errors="replace")
    if len(sys.argv) == 3 and sys.argv[1] == "check":
        k = os.environ.get(KEY_ENV, "").strip()
        if not k:
            sys.exit(f"{KEY_ENV} 가 없습니다 — 8~128자(영문·숫자·하이픈) 키를 지어 env 에 넣고, "
                     f"같은 글자를 담은 <키>.txt 를 사이트 루트에 올립니다")
        if not KEY_RE.match(k):
            sys.exit(f"{KEY_ENV} 꼴이 규칙과 다릅니다(8~128자, 영문·숫자·하이픈)")
        ok = key_file_ok(sys.argv[2], k)
        print(f"{sys.argv[2]}: 키 파일 " + ("확인됨 — 머지 반영 때 알림이 나갑니다" if ok
                                         else "없음 또는 내용 불일치 — 알림이 나가지 않습니다"))
        sys.exit(0 if ok else 1)
    sys.exit("usage: python indexnow.py check <host>")
