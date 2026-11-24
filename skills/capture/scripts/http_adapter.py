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
import ipaddress
import os
import re
import socket
import threading
from collections import OrderedDict
from http.cookiejar import DefaultCookiePolicy

# 한 런 동안 들고 있는 원문의 합계 상한 — 큰 사이트를 훑을 때 메모리가 불지 않게 오래된 것부터 민다.
MAX_CACHE_BYTES = 32 * 1024 * 1024
MAX_ENTRY_BYTES = 2 * 1024 * 1024     # 이보다 큰 한 장은 아예 안 담는다
POOL_SIZE = 16                        # 호스트당 동시 연결 상한(수집기 일꾼은 한 호스트에 4개)

# 한 장에서 읽는 본문의 상한(압축을 푼 바이트). 이보다 길면 앞부분만 받고 Fetched.truncated 를
# 세운다 — 끝없이 보내는 응답·압축 폭탄이 일꾼의 메모리를 다 먹지 않게. 값은 사이트맵 규격의
# 한 파일 상한이다(50MB = 52,428,800바이트, https://www.sitemaps.org/protocol.html#index) —
# 사이트맵도 이 길로 받으므로 규격에 맞는 파일은 잘리지 않는다. HTML 은 collect_page.MAX_HTML
# (2MB)까지만 파싱하므로 그쪽이 먼저 걸린다.
MAX_FETCH_BYTES = 52_428_800
MAX_REDIRECTS = 30                    # requests 의 기본값(DEFAULT_REDIRECT_LIMIT)과 같다 — 넘으면 TooManyRedirects
_REDIRECT_BODY_BYTES = 65_536         # 리다이렉트 응답의 본문은 여기까지만 비우고 버린다(연결 재사용용)


# ── 주소 검사 (SSRF) ────────────────────────────────────────────────────────
# 이 모듈이 여는 주소는 남이 정한다: 호스팅에서는 가입자가 등록한 사이트이고, 로컬에서도
# 경쟁 글·sameAs 처럼 남의 페이지가 가리키는 주소다. 그 주소(또는 그 주소의 리다이렉트·DNS)가
# 이 프로세스의 안쪽 — 루프백, 사설 대역, 클라우드 메타데이터(169.254.169.254) — 을 가리키면
# 서버가 대신 열어 준 셈이 된다. 등록 때 한 번 보는 검사(server/app.py _site_url)로는 못 막는다:
# 리다이렉트와, 등록 뒤 DNS 를 바꾸는 것(rebinding)이 그 뒤에 온다.
#
# 그래서 **연결을 맺는 그 자리**에서 본다(_guard_classes): 이름을 풀고, 푼 주소가 전부 공인인지
# 보고, 검사한 그 IP 로 붙는다(다시 풀지 않는다 — 검사와 연결 사이에 답이 바뀔 틈이 없다).
# 연결마다 하므로 리다이렉트의 홉도, 런마다 새로 맺는 연결도 같은 검사를 받는다.
#
# 기본값은 **로컬·호스팅 둘 다 막는다.**
#   · 호스팅: 가입자가 주소를 정하고 컨테이너는 사설망·메타데이터에 닿는다 — 반드시 막는다.
#   · 로컬(사용자 PC): 주소를 정하는 사람은 PC 주인이지만, 남의 페이지가 리다이렉트로
#     공유기 관리 화면·로컬 개발 서버에 GET 을 쏘게 할 수 있다. 측정 대상은 검색에 나오는
#     공개 사이트라 안쪽 주소를 열 정당한 이유가 없다 — 그래서 같은 기본값이다.
#   · 사내망·스테이징 사이트를 일부러 재려면 SEO_MINER_ALLOW_PRIVATE_FETCH=1 로 푼다
#     (운영자가 프로세스 환경에 명시적으로 넣는 것만. 호스팅에는 넣지 않는다).
# 프록시(HTTPS_PROXY 등)를 거치는 연결은 여기서 못 본다 — 이름을 프록시가 푼다. 그때는
# 프록시가 경계다(호스팅은 프록시를 쓰지 않는다).
ALLOW_PRIVATE = os.environ.get("SEO_MINER_ALLOW_PRIVATE_FETCH") == "1"
_EXTRA_PUBLIC: set = set()            # 검사 전용 — '공인'으로 쳐 줄 주소(로컬 서버를 공개 사이트 삼는다)
_getaddrinfo = socket.getaddrinfo     # 검사 전용 — DNS 답을 갈아끼우는 자리

# IPv4 를 싸서 나르는 IPv6 접두 — 겉은 공인처럼 보여도 속의 IPv4 로 닿는다. 속을 꺼내 본다.
_NAT64 = ipaddress.ip_network("64:ff9b::/96")         # RFC 6052 well-known prefix


class BlockedAddress(ValueError):
    """공인 주소가 아닌 곳을 열려 했다. urllib3·requests 가 재시도로 삼키지 않게 OSError 가 아니다."""


def is_public_ip(ip) -> bool:
    """이 IP 가 공개 인터넷 주소인가 — 판정 한 벌(등록 때 server/app.py 도 이걸 쓴다).

    기준은 IANA 특수 목적 주소 등록부의 'globally reachable'(파이썬 `is_global`)이다:
    루프백·사설(10/8, 172.16/12, 192.168/16)·링크 로컬(169.254/16, fe80::/10)·CGNAT(100.64/10)·
    ULA(fc00::/7)·미지정이 전부 빠진다. IPv4 를 싼 IPv6(IPv4-mapped, 6to4, Teredo, NAT64)는
    속의 IPv4 로 다시 본다."""
    try:
        a = ip if isinstance(ip, (ipaddress.IPv4Address, ipaddress.IPv6Address)) \
            else ipaddress.ip_address(str(ip).split("%", 1)[0])
    except ValueError:
        return False
    if a in _EXTRA_PUBLIC:
        return True
    if a.version == 6:
        inner = a.ipv4_mapped or a.sixtofour or (a.teredo[1] if a.teredo else None)
        if inner is None and a in _NAT64:
            inner = ipaddress.IPv4Address(int(a) & 0xFFFFFFFF)
        if inner is not None:
            return is_public_ip(inner)
    return a.is_global


def public_addrs(host: str, port) -> list[str]:
    """host 를 풀어 붙어도 되는 IP 목록을 돌려준다. 하나라도 공인이 아니면 BlockedAddress.

    '하나라도'인 이유: 공인 주소와 안쪽 주소를 섞어 답하면(라운드 로빈) 어느 쪽에 붙을지를
    상대가 고른다. 이름을 못 풀면 socket.gaierror 그대로(막는 게 아니라 '못 열었다')."""
    infos = _getaddrinfo(host, port, type=socket.SOCK_STREAM)
    ips: list[str] = []
    for info in infos:
        ip = info[4][0]
        if ip not in ips:
            ips.append(ip)
    if ALLOW_PRIVATE:
        return ips
    bad = [ip for ip in ips if not is_public_ip(ip)]
    if bad or not ips:
        raise BlockedAddress(f"공개 인터넷 주소가 아닙니다: {host} → {', '.join(bad) or '주소 없음'}")
    return ips


_guard = None


def _guard_classes():
    """연결을 맺을 때 주소를 검사하고 그 IP 에 고정하는 urllib3 연결·풀 클래스(한 번만 만든다)."""
    global _guard
    if _guard is not None:
        return _guard
    from urllib3.connection import HTTPConnection, HTTPSConnection
    from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool
    from urllib3.exceptions import NewConnectionError

    class _Pinned:
        def _new_conn(self):
            if getattr(self, "proxy", None):          # 프록시 연결 — 대상 이름은 프록시가 푼다
                return super()._new_conn()
            name = self._dns_host
            try:
                ips = public_addrs(name, self.port)
            except socket.gaierror:
                return super()._new_conn()            # 못 푸는 이름 — urllib3 가 제 오류로 말하게 둔다
            # 검사한 IP 로만 붙는다. _dns_host 는 '어디에 붙나'일 뿐이고, 붙은 뒤 되돌리므로
            # Host 헤더·TLS SNI·인증서 대조는 원래 이름 그대로다(connect() 가 self.host 를 쓴다).
            err = None
            try:
                for ip in ips:
                    self._dns_host = ip
                    try:
                        return super()._new_conn()
                    except (NewConnectionError, OSError) as e:    # 다음 주소로(IPv6 길이 없는 PC 등)
                        err = e
                raise err
            finally:
                self._dns_host = name

    class PinnedHTTP(_Pinned, HTTPConnection):
        pass

    class PinnedHTTPS(_Pinned, HTTPSConnection):
        pass

    class HTTPPool(HTTPConnectionPool):
        ConnectionCls = PinnedHTTP

    class HTTPSPool(HTTPSConnectionPool):
        ConnectionCls = PinnedHTTPS

    _guard = {"http": HTTPPool, "https": HTTPSPool}
    return _guard


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

    __slots__ = ("url", "final_url", "status", "chain", "content_type", "bytes", "_text", "_resp",
                 "truncated")

    def __init__(self, url: str, final_url: str, status: int, chain=(), content_type: str = "",
                 *, text: str | None = None, nbytes: int | None = None, resp=None,
                 truncated: bool = False):
        self.url, self.final_url, self.status = url, final_url, status
        self.truncated = bool(truncated)      # 본문이 MAX_FETCH_BYTES 에서 잘렸다(앞부분만 있다)
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
            class Guarded(HTTPAdapter):
                # 풀이 만드는 연결을 주소 검사가 든 것으로 바꾼다(위 '주소 검사' 참고).
                def init_poolmanager(self, *a, **k):
                    super().init_poolmanager(*a, **k)
                    self.poolmanager.pool_classes_by_scheme = _guard_classes()

            s = requests.Session()
            s.cookies.set_policy(DefaultCookiePolicy(allowed_domains=[]))
            ad = Guarded(pool_connections=POOL_SIZE, pool_maxsize=POOL_SIZE)
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


def _read_capped(r, cap: int) -> bool:
    """응답 본문을 cap 바이트까지만 읽어 r 에 채운다 → 잘렸나.

    requests 는 stream=True 로 받은 응답의 본문을 r._content 에 둔다(r.content 가 처음 읽을 때
    채우는 자리). 우리가 먼저 채워 두면 r.content·r.text·apparent_encoding 이 그 앞부분만 본다."""
    chunks, n, cut = [], 0, False
    try:
        for c in r.iter_content(65_536):
            chunks.append(c)
            n += len(c)
            if n > cap:
                cut = True
                break
    finally:
        if cut:
            r.close()               # 남은 본문을 안 받는다 — 이 연결은 버린다
    r._content = b"".join(chunks)[:cap]
    r._content_consumed = True
    return cut


def _open(url: str, headers: dict, timeout):
    """리다이렉트를 **한 홉씩** 따라가 마지막 응답을 연다 → (응답, 홉 목록). 본문은 아직 안 읽었다.

    requests 에 맡기지 않고(allow_redirects=False) 직접 따라간다: 홉마다 스킴을 보고,
    리다이렉트 응답의 본문을 통째로 읽지 않으려고. 주소 검사는 홉마다 연결을 맺는 자리에서 돈다."""
    import requests
    from urllib.parse import urljoin, urlsplit
    s, chain, cur = _the_session(), [], url
    while True:
        if urlsplit(cur).scheme.lower() not in ("http", "https"):
            raise BlockedAddress(f"http(s) 주소만 엽니다: {cur[:200]}")
        r = s.get(cur, headers=headers, timeout=timeout, allow_redirects=False, stream=True)
        nxt = s.get_redirect_target(r) if r.is_redirect else None
        if not nxt:
            return r, chain
        chain.append((r.url, r.status_code))
        try:
            r.raw.read(_REDIRECT_BODY_BYTES, decode_content=False)    # 짧은 본문은 비워 연결을 돌려준다
        except Exception:
            pass
        r.close()
        if len(chain) > MAX_REDIRECTS:
            raise requests.exceptions.TooManyRedirects(f"리다이렉트가 {MAX_REDIRECTS}번을 넘었습니다: {url}")
        cur = requests.utils.requote_uri(urljoin(r.url, nxt))


def _real(url: str, headers: dict, timeout) -> Fetched:
    r, chain = _open(url, headers, timeout)
    cut = _read_capped(r, MAX_FETCH_BYTES)
    return Fetched(url, r.url, r.status_code, chain,
                   r.headers.get("content-type") or "", resp=r, truncated=cut)


def probe(url: str, *, headers: dict, timeout) -> int:
    """주소 하나의 HTTP 상태만(본문은 안 받는다). get() 과 같은 주소 검사·홉 검사를 거친다.

    남의 페이지가 가리킨 주소가 살아 있는지 볼 때 쓴다(sameAs) — 캐시는 안 거친다."""
    r, _ = _open(url, headers, timeout)
    r.close()
    return r.status_code


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
