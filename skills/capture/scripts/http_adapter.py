#!/usr/bin/env python3
"""내 사이트(와 남의 공개 페이지) 한 장을 HTTP GET 으로 가져오는 자리 — 한 곳.

이전에는 collect_crawl.fetch 와 collect_page.fetch 가 각자 `requests.get` 을 불러 호출마다
새 연결(TCP·TLS 핸드셰이크)을 맺었고, 같은 내 사이트 URL 을 한 런 안에서 따로 받았다.
여기서 두 가지를 한다.

  1. **연결 재사용** — 프로세스에 Session 하나(스레드 안전). 쿠키는 막아서 요청끼리 상태가
     새지 않는다(`requests.get` 은 호출마다 빈 Session 이었다 — 그 성질을 지킨다).
  2. **한 런 안의 원문 공유** — `run_scope()` 안에서만, 같은 URL 은 한 번만 받는다. crawl 은
     원문+홉, pages 는 audit 한 줄로 **결과 모양**이 달라도 원문 응답은 같다 — 모양은 호출한
     쪽이 이 `Fetched` 에서 각자 만든다. 범위 밖(런 사이)에서는 절대 공유하지 않는다: 사이트를
     고치고 바로 다시 재도 옛 응답이 나오면 안 된다.

수집기(collect_crawl·collect_page)는 여기 `get()` 하나만 부른다. 테스트는 `use(fake)` 로
전송 한 줄만 갈아끼운다 — `fetch` 를 통째로 바꾸지 않아도 되고, 캐시·디코딩은 진짜가 돈다.

DataForSEO 같은 유료 API 호출(serp_adapter)은 간격·대기열 규칙이 있어 이 모듈을 거치지 않는다.
"""
from __future__ import annotations

import contextlib
import re
import threading
from collections import OrderedDict
from http.cookiejar import DefaultCookiePolicy

# 한 런 동안 들고 있는 원문의 합계 상한 — 큰 사이트를 훑을 때 메모리가 불지 않게 오래된 것부터 민다.
MAX_CACHE_BYTES = 32 * 1024 * 1024
MAX_ENTRY_BYTES = 2 * 1024 * 1024     # 이보다 큰 한 장은 아예 안 담는다
POOL_SIZE = 16                        # 호스트당 동시 연결 상한(수집기 일꾼은 한 호스트에 4개)


# ── 응답 ────────────────────────────────────────────────────────────────────
_META_CHARSET = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?\s*([A-Za-z0-9_\-]+)""", re.I)


def html_text(r) -> str:
    """응답 본문을 글자로 — 헤더에 charset 이 없으면 본문의 <meta charset>, 그것도 없으면 내용 판정.

    requests 의 r.text 는 'text/html' 에 charset 이 없으면 ISO-8859-1 로 읽는다(RFC 2616 기본값).
    그래서 상위 글 개요가 'ë¥í° ì¹¼ë¼' 로 저장돼 요청문에 나갔다(cnpskin — 본문은 utf-8).
    우리 페이지 감사(fetch)와 크롤(collect_crawl.fetch)이 같은 길이라 둘 다 이걸 쓴다.
    r 은 content·headers·text·apparent_encoding 을 가진 requests 응답(꼴만 맞으면 된다)."""
    if "charset" in (r.headers.get("content-type") or "").lower():
        return r.text
    m = _META_CHARSET.search(r.content[:4096])
    for enc in ((m.group(1).decode("ascii", "ignore") if m else None),
                r.apparent_encoding, "utf-8"):
        if not enc:
            continue
        try:
            return r.content.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return r.content.decode("utf-8", errors="replace")


class Fetched:
    """받은 응답 한 장. 호출한 쪽이 이것에서 제 모양(크롤 dict·감사 행)을 만든다.

    text 는 처음 읽을 때 한 번 디코딩한다 — 안 읽는 쪽(HTML 이 아니라 오류로 돌려보내는 pages)이
    큰 바이너리를 판정(chardet)하느라 시간을 쓰지 않게."""

    __slots__ = ("url", "final_url", "status", "chain", "content_type", "bytes", "_text", "_resp")

    def __init__(self, url: str, final_url: str, status: int, chain=(), content_type: str = "",
                 *, text: str | None = None, nbytes: int | None = None, resp=None):
        self.url, self.final_url, self.status = url, final_url, status
        self.chain = tuple(chain)
        self.content_type = content_type or ""
        self._text, self._resp = text, resp
        self.bytes = nbytes if nbytes is not None else (
            len(resp.content) if resp is not None else len((text or "").encode("utf-8")))

    @property
    def text(self) -> str:
        if self._text is None:
            self._text = html_text(self._resp) if self._resp is not None else ""
        return self._text


# ── 전송 (갈아끼우는 자리) ───────────────────────────────────────────────────
_session = None
_session_lock = threading.Lock()


def _the_session():
    """프로세스 하나의 Session. 쿠키 정책으로 모든 쿠키를 거절한다 — 요청끼리 상태가 안 샌다."""
    global _session
    with _session_lock:
        if _session is None:
            import requests
            from requests.adapters import HTTPAdapter
            s = requests.Session()
            s.cookies.set_policy(DefaultCookiePolicy(allowed_domains=[]))
            ad = HTTPAdapter(pool_connections=POOL_SIZE, pool_maxsize=POOL_SIZE)
            s.mount("http://", ad)
            s.mount("https://", ad)
            _session = s
        return _session


def close() -> None:
    """열린 연결을 버린다(테스트가 서버를 닫을 때·연결이 의심스러울 때)."""
    global _session
    with _session_lock:
        s, _session = _session, None
    if s is not None:
        s.close()


def _real(url: str, headers: dict, timeout) -> Fetched:
    r = _the_session().get(url, headers=headers, timeout=timeout, allow_redirects=True)
    return Fetched(url, r.url, r.status_code, [(h.url, h.status_code) for h in r.history],
                   r.headers.get("content-type") or "", resp=r)


_transport = _real


@contextlib.contextmanager
def use(transport):
    """전송 `fn(url, headers, timeout) -> Fetched` 를 갈아끼운다(예외는 그대로 던지면 된다).
    캐시도 비운다 — 가짜 응답이 다음 검사로 새지 않게."""
    global _transport
    prev, _transport = _transport, transport
    _reset()
    try:
        yield
    finally:
        _transport = prev
        _reset()


# ── 한 런 안의 공유 ─────────────────────────────────────────────────────────
_lock = threading.Lock()
_depth = 0                                # run_scope 가 열린 수(겹쳐도 되고, 동시에 열려도 된다)
_cache: "OrderedDict[str, Fetched]" = OrderedDict()
_cache_bytes = 0
_inflight: dict[str, threading.Event] = {}


def _reset() -> None:
    global _cache_bytes
    with _lock:
        _cache.clear()
        _cache_bytes = 0


def cache_bytes() -> int:
    return _cache_bytes


@contextlib.contextmanager
def run_scope():
    """이 안에서만 같은 URL 의 원문을 공유한다. 마지막 범위가 닫히면 전부 버린다.

    한 번의 런(체인)을 감싸는 자리에서 연다 — 런 사이에는 늘 새로 받는다."""
    global _depth
    with _lock:
        _depth += 1
    try:
        yield
    finally:
        with _lock:
            _depth -= 1
            last = _depth == 0
        if last:
            _reset()


def _shareable(f: Fetched) -> bool:
    # 서버 오류·429·403 은 다음 시도에서 달라질 수 있다 — 한 번 못 본 것을 사실로 굳히지 않는다.
    # 정상(<400)과 없는 페이지(404·410)는 사실이라 공유한다.
    return (f.status < 400 or f.status in (404, 410)) and f.bytes <= MAX_ENTRY_BYTES


def _store(url: str, f: Fetched) -> None:
    global _cache_bytes
    with _lock:
        old = _cache.pop(url, None)
        if old is not None:
            _cache_bytes -= old.bytes
        _cache[url] = f
        _cache_bytes += f.bytes
        while _cache_bytes > MAX_CACHE_BYTES and _cache:
            _, gone = _cache.popitem(last=False)
            _cache_bytes -= gone.bytes


def get(url: str, *, headers: dict, timeout) -> Fetched:
    """URL 한 장. 리다이렉트는 따라가고 홉은 chain 에 남는다. 연결 실패·시간 초과는 예외 그대로.

    run_scope() 안이면 같은 URL 은 한 번만 받는다(동시에 부르면 하나가 받고 나머지는 기다린다).
    공유된 응답은 처음 받은 쪽의 UA·타임아웃으로 받은 것이다 — 두 UA 는 모두 같은 신원(seo-miner)이다."""
    if _depth == 0:
        return _transport(url, headers, timeout)
    while True:
        with _lock:
            hit = _cache.get(url)
            if hit is not None:
                _cache.move_to_end(url)
                return hit
            wait = _inflight.get(url)
            if wait is None:
                _inflight[url] = mine = threading.Event()
        if wait is not None:
            wait.wait()
            continue                       # 받은 쪽이 캐시에 넣었거나(적중), 못 넣었으면 내가 받는다
        try:
            f = _transport(url, headers, timeout)
            if _shareable(f):
                with _lock:
                    scoped = _depth > 0
                if scoped:
                    _store(url, f)
            return f
        finally:
            with _lock:
                _inflight.pop(url, None)
            mine.set()
