#!/usr/bin/env python3
"""http_adapter 검사 — `python test_http_adapter.py` (로컬 http.server 만, 바깥 네트워크 0).

세 가지를 못 박는다:
  · 연결 재사용 — 같은 호스트를 여러 번(스레드에서도) 받아도 TLS/TCP 연결이 몇 개로 그친다.
    예전에는 requests.get 호출마다 새 연결이었다.
  · 한 런 안의 원문 공유 — run_scope() 안에서만 같은 URL 을 한 번만 받는다(밖에서는 늘 새로).
    crawl 과 pages 의 fetch 는 결과 모양이 달라도 **같은 원문**에서 각자의 모양을 만든다.
  · 갈아끼우는 자리 — use(fake) 로 네트워크 없이 가짜 응답을 낀다(fetch 를 통째로 바꾸지 않아도).
"""
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import collect_crawl  # noqa: E402
import collect_page  # noqa: E402
import http_adapter  # noqa: E402

UA_A = {"User-Agent": "test/a"}
UA_B = {"User-Agent": "test/b"}

PAGE = ("<html><head><meta charset=euc-kr><title>루터 칼라</title></head>"
        "<body><h1>루터</h1><p>" + "본문 " * 300 + "</p></body></html>")


class Site:
    """로컬 사이트 — 요청 로그(경로·UA·Cookie)와 연결(클라이언트 포트) 수를 센다."""

    def __init__(self, delay: float = 0.0):
        site = self
        self.log: list[dict] = []
        self.ports: set[int] = set()
        self.lock = threading.Lock()

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"          # keep-alive 가 되어야 재사용을 잴 수 있다

            def log_message(self, *a):
                pass

            def do_GET(self):
                with site.lock:
                    site.log.append({"path": self.path, "ua": self.headers.get("User-Agent"),
                                     "cookie": self.headers.get("Cookie")})
                    site.ports.add(self.client_address[1])
                if delay:
                    time.sleep(delay)
                code, ctype, body, extra = 200, "text/html", PAGE.encode("euc-kr"), {}
                if self.path == "/old":
                    code, extra, body = 301, {"Location": "/mid"}, b""
                elif self.path == "/mid":
                    code, extra, body = 302, {"Location": "/page"}, b""
                elif self.path == "/missing":
                    code, body = 404, b"nope"
                elif self.path == "/boom":
                    code, body = 500, b"boom"
                elif self.path == "/img":
                    ctype, body = "image/png", b"\x89PNG" + b"\0" * 64
                elif self.path.startswith("/big"):
                    body = b"x" * 3000
                self.send_response(code)
                self.send_header("Content-Type", ctype)       # charset 없음 — 본문 meta 로 판정
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Set-Cookie", "sid=abc; Path=/")
                for k, v in extra.items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.srv.daemon_threads = True
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def hits(self, path: str) -> int:
        return sum(1 for e in self.log if e["path"] == path)

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()
        http_adapter.close()        # 열린 연결을 버린다 — 다음 서버와 섞이지 않게


def test_connections_are_reused_sequentially():
    s = Site()
    try:
        for _ in range(10):
            r = http_adapter.get(s.base + "/page", headers=UA_A, timeout=5)
            assert r.status == 200 and "루터 칼라" in r.text, r.status
        assert len(s.log) == 10
        assert len(s.ports) == 1, f"연결이 {len(s.ports)}개 — 호출마다 새로 맺고 있다"
    finally:
        s.close()


def test_connections_are_bounded_across_threads():
    s = Site(delay=0.02)
    try:
        def work():
            for _ in range(5):
                http_adapter.get(s.base + "/page", headers=UA_A, timeout=5)
        ts = [threading.Thread(target=work) for _ in range(4)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        assert len(s.log) == 20
        assert len(s.ports) <= 4, f"스레드 4개가 연결 {len(s.ports)}개를 맺었다 — 재사용이 안 된다"
    finally:
        s.close()


def test_no_cookies_carry_over_and_ua_is_per_call():
    s = Site()
    try:
        http_adapter.get(s.base + "/page", headers=UA_A, timeout=5)
        http_adapter.get(s.base + "/missing", headers=UA_B, timeout=5)
        assert [e["cookie"] for e in s.log] == [None, None], s.log
        assert [e["ua"] for e in s.log] == ["test/a", "test/b"], s.log
    finally:
        s.close()


def test_fetched_shape_chain_and_decoding():
    s = Site()
    try:
        r = http_adapter.get(s.base + "/old", headers=UA_A, timeout=5)
        assert r.final_url == s.base + "/page" and r.status == 200
        assert list(r.chain) == [(s.base + "/old", 301), (s.base + "/mid", 302)], r.chain
        assert "루터 칼라" in r.text, "헤더에 charset 이 없어도 본문 meta(euc-kr)로 읽어야 한다"
        assert r.bytes == len(PAGE.encode("euc-kr")) and r.content_type.startswith("text/html")
        assert http_adapter.get(s.base + "/missing", headers=UA_A, timeout=5).status == 404
    finally:
        s.close()


def test_errors_are_raised_not_swallowed():
    try:
        http_adapter.get("http://127.0.0.1:9/x", headers=UA_A, timeout=2)
    except Exception as e:
        assert type(e).__name__ in ("ConnectionError", "ConnectTimeout"), type(e)
    else:
        raise AssertionError("닫힌 포트인데 예외가 안 났다")


def test_no_sharing_outside_a_run_scope():
    s = Site()
    try:
        http_adapter.get(s.base + "/page", headers=UA_A, timeout=5)
        http_adapter.get(s.base + "/page", headers=UA_B, timeout=5)
        assert s.hits("/page") == 2, "런 범위 밖인데 응답이 공유됐다 — 고친 사이트를 바로 다시 재도 옛 응답이 나온다"
    finally:
        s.close()


def test_same_url_is_shared_inside_a_run_scope_and_cleared_after():
    s = Site()
    try:
        with http_adapter.run_scope():
            a = http_adapter.get(s.base + "/page", headers=UA_A, timeout=5)
            b = http_adapter.get(s.base + "/page", headers=UA_B, timeout=5)
            assert s.hits("/page") == 1 and a.text == b.text and a.status == b.status
            http_adapter.get(s.base + "/old", headers=UA_A, timeout=5)
            http_adapter.get(s.base + "/old", headers=UA_B, timeout=5)
            assert s.hits("/old") == 1, "리다이렉트 시작 주소도 같은 키로 공유돼야 한다"
            # 못 받은 것·서버 오류는 공유하지 않는다 — 다음 시도는 다를 수 있다
            for _ in range(2):
                http_adapter.get(s.base + "/boom", headers=UA_A, timeout=5)
            assert s.hits("/boom") == 2
            # 없는 페이지(404)는 사실이라 공유한다
            for _ in range(2):
                http_adapter.get(s.base + "/missing", headers=UA_A, timeout=5)
            assert s.hits("/missing") == 1
        http_adapter.get(s.base + "/page", headers=UA_A, timeout=5)
        assert s.hits("/page") == 3, "런이 끝났는데 응답이 남아 있다"  # 직접 1 + /old 의 도착지 1 + 범위 밖 1
    finally:
        s.close()


def test_concurrent_requests_for_one_url_make_one_request():
    s = Site(delay=0.15)
    try:
        with http_adapter.run_scope():
            out = []
            ts = [threading.Thread(target=lambda: out.append(
                http_adapter.get(s.base + "/page", headers=UA_A, timeout=5))) for _ in range(6)]
            [t.start() for t in ts]
            [t.join() for t in ts]
            assert len(out) == 6 and all(o.status == 200 for o in out)
            assert s.hits("/page") == 1, f"동시에 같은 주소를 {s.hits('/page')}번 받았다"
    finally:
        s.close()


def test_cache_is_bounded():
    s = Site()
    saved = http_adapter.MAX_CACHE_BYTES, http_adapter.MAX_ENTRY_BYTES
    http_adapter.MAX_CACHE_BYTES, http_adapter.MAX_ENTRY_BYTES = 7000, 4000
    try:
        with http_adapter.run_scope():
            for p in ("/big?1", "/big?2", "/big?3"):          # 3000 바이트씩 — 셋은 못 담는다
                http_adapter.get(s.base + p, headers=UA_A, timeout=5)
            assert http_adapter.cache_bytes() <= 7000, http_adapter.cache_bytes()
            http_adapter.get(s.base + "/big?3", headers=UA_A, timeout=5)
            assert s.hits("/big?3") == 1, "가장 최근 것은 남아 있어야 한다"
            http_adapter.get(s.base + "/big?1", headers=UA_A, timeout=5)
            assert s.hits("/big?1") == 2, "가장 오래된 것은 밀려나야 한다"
        http_adapter.MAX_ENTRY_BYTES = 100              # 한 장이 상한보다 크면 아예 안 담는다
        with http_adapter.run_scope():
            http_adapter.get(s.base + "/page", headers=UA_A, timeout=5)
            http_adapter.get(s.base + "/page", headers=UA_A, timeout=5)
            assert s.hits("/page") == 2
    finally:
        http_adapter.MAX_CACHE_BYTES, http_adapter.MAX_ENTRY_BYTES = saved
        s.close()


def test_crawl_and_page_fetch_share_one_response_but_keep_their_own_shape():
    s = Site()
    try:
        url = s.base + "/old"
        with http_adapter.run_scope():
            c = collect_crawl.fetch(url)
            p = collect_page.fetch(url)
        assert s.hits("/old") == 1 and s.hits("/page") == 1, (s.hits("/old"), s.hits("/page"))
        # 크롤 모양: 원문 + 홉
        assert set(c) == {"final_url", "status", "chain", "text", "bytes", "content_type", "error"}, set(c)
        assert c["chain"] == [(s.base + "/old", 301), (s.base + "/mid", 302)] and "루터 칼라" in c["text"]
        assert [e["ua"] for e in s.log if e["path"] == "/old"] == [collect_crawl.UA["User-Agent"]]
        # 페이지 모양: audit_html 한 줄
        assert p["status"] == 200 and p["title"] == "루터 칼라" and p.get("error") is None, p
        assert p["url"] == url, "audit 행의 url 은 요청한 주소다"
        # 오류 모양도 그대로
        assert collect_page.fetch(s.base + "/missing") == {
            "url": s.base + "/missing", "status": 404, "error": "HTTP 404 · text/html"}
        assert collect_page.fetch(s.base + "/img")["error"] == "HTTP 200 · image/png"
        dead = collect_page.fetch("http://127.0.0.1:9/x", timeout=2)
        assert dead["status"] is None and dead["error"].startswith(("ConnectionError", "ConnectTimeout")), dead
        cd = collect_crawl.fetch("http://127.0.0.1:9/x")
        assert cd["status"] is None and cd["chain"] == [] and cd["text"] == "" and cd["error"], cd
    finally:
        s.close()


def test_fake_transport_replaces_network():
    seen = []

    def fake(url, headers, timeout):
        seen.append((url, headers, timeout))
        if "dead" in url:
            raise ConnectionError("down")
        return http_adapter.Fetched(url, url, 200, (), "text/html", text="<html><title>가짜</title></html>")

    with http_adapter.use(fake):
        assert collect_page.fetch("https://fake.kr/a")["title"] == "가짜"
        assert collect_crawl.fetch("https://fake.kr/a")["text"].startswith("<html>")
        assert collect_crawl.fetch("https://dead.kr/")["error"].startswith("ConnectionError")
    assert seen[0][2] == 20 and seen[0][1] == collect_page.UA, seen[0]
    try:
        with http_adapter.use(fake):
            raise RuntimeError("x")
    except RuntimeError:
        pass
    assert http_adapter._transport is http_adapter._real, "use() 가 예외에도 원래 전송을 되돌려야 한다"


def test_run_chain_shares_one_scope_across_stages_and_clears_after():
    """run_all.run_chain 한 번 = 범위 하나: 크롤(crawl)과 페이지 감사(pages)가 같은 URL 을 서버에
    한 번만 보내고, 체인이 끝나면 캐시가 비며, 다음 체인은 다시 받는다(런 사이 공유 금지).
    순차 체인과 묶음 런(스레드) 둘 다 같은 범위 안이다."""
    import contextlib
    import io

    import collector
    import run_all
    seen = []

    def fake(url, headers, timeout):
        seen.append(url)
        return http_adapter.Fetched(url, url, 200, (), "text/html",
                                    text="<html><head><title>가짜</title></head><body>본문</body></html>")

    def table():
        def fetches(fn):
            def stage_fn(project, *, dry_run=False, **opts):
                fn("https://fake.kr/shared")
                return collector.succeeded(rows=1)
            return stage_fn
        want = {"crawl": collect_crawl.fetch, "pages": collect_page.fetch}
        return tuple(s._replace(fn=fetches(want[s.name]), is_paid=False)
                     for s in run_all.STAGES if s.name in want)

    def chain(**kw):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return run_all.run_chain("__chain_scope__", stages=table(),
                                     preflight=lambda s: {}, **kw)

    with http_adapter.use(fake):
        chain()
        assert seen == ["https://fake.kr/shared"], f"한 체인 안에서 같은 URL 을 {len(seen)}번 보냈다: {seen}"
        assert http_adapter.cache_bytes() == 0 and not http_adapter._cache, "체인이 끝났는데 캐시가 남았다"
        chain()
        assert len(seen) == 2, "다음 체인이 이전 체인의 응답을 재사용했다 — 런 사이 공유 금지"
        seen.clear()
        chain(groups=["site"])          # 묶음 런(스레드 풀) — crawl 은 site 묶음이다
        assert seen == ["https://fake.kr/shared"], seen
        assert http_adapter._depth == 0 and not http_adapter._cache

    with http_adapter.use(fake):        # 체인이 예외로 끝나도 범위가 닫힌다
        def boom(project, *, dry_run=False, **opts):
            http_adapter.get("https://fake.kr/x", headers={}, timeout=1)
            raise KeyboardInterrupt
        t = tuple(s._replace(fn=boom, is_paid=False) for s in run_all.STAGES if s.name == "crawl")
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                run_all.run_chain("__chain_scope__", stages=t, preflight=lambda s: {})
        except KeyboardInterrupt:
            pass
        assert http_adapter._depth == 0 and not http_adapter._cache, "중단된 체인이 범위를 열어 둔 채 남았다"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)} passed")
