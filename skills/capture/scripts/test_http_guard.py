#!/usr/bin/env python3
"""http_adapter 의 주소 검사(SSRF) — `python test_http_guard.py` (로컬 http.server 만, 바깥 네트워크 0).

못 박는 것:
  · 공인 주소가 아닌 곳(사설·루프백·링크 로컬·메타데이터)은 **연결하기 전에** 막힌다.
  · 리다이렉트는 한 홉씩 같은 검사를 받는다 — 공개 사이트가 169.254.169.254·사설 IP·localhost 로
    넘기면 그 홉에서 멈춘다.
  · DNS 가 등록 뒤 바뀌어도(rebinding) 막힌다 — 검사는 요청마다 하고, 검사한 그 IP 로만 붙는다.
  · 본문은 상한까지만 읽고, 잘렸다는 사실이 남는다.

'공개 사이트'는 로컬 서버를 127.0.0.2 에 띄우고 그 주소 하나만 공인으로 쳐 준 것이다
(`_EXTRA_PUBLIC`). 127.0.0.1·localhost·사설 대역은 진짜 규칙 그대로 막힌다.
"""
import ipaddress
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import collect_crawl  # noqa: E402
import collect_page  # noqa: E402
import http_adapter  # noqa: E402

UA = {"User-Agent": "test/guard"}
PUBLIC_IP = "127.0.0.2"          # 이 검사에서만 '공인'으로 쳐 주는 주소(로컬 서버가 여기 뜬다)


class Site:
    def __init__(self, bind: str = PUBLIC_IP):
        site = self
        self.log: list[str] = []
        self.redirects: dict[str, str] = {}

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):
                site.log.append(self.path)
                code, body, extra = 200, b"<html><title>ok</title></html>", {}
                if self.path in site.redirects:
                    code, body, extra = 302, b"", {"Location": site.redirects[self.path]}
                elif self.path == "/huge":
                    body = b"<html>" + b"x" * 50_000
                self.send_response(code)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                for k, v in extra.items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

        self.srv = ThreadingHTTPServer((bind, 0), H)
        self.srv.daemon_threads = True
        self.port = self.srv.server_address[1]
        self.base = f"http://{bind}:{self.port}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()
        http_adapter.close()


class guard:
    """검사 동안: 사설 허용을 끄고(운영 기본값), 127.0.0.2 만 공인으로, DNS 는 표로."""

    def __init__(self, dns: dict | None = None):
        self.dns = dns or {}
        self.lookups: list[str] = []

    def __enter__(self):
        self.saved = (http_adapter.ALLOW_PRIVATE, set(http_adapter._EXTRA_PUBLIC), http_adapter._getaddrinfo)
        http_adapter.ALLOW_PRIVATE = False
        http_adapter._EXTRA_PUBLIC.add(ipaddress.ip_address(PUBLIC_IP))
        real = socket.getaddrinfo

        def fake(host, port, *a, **k):
            self.lookups.append(host)
            ans = self.dns.get(host)
            if callable(ans):
                ans = ans()
            if ans is None:
                return real(host, port, *a, **k)
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in ans]

        http_adapter._getaddrinfo = fake
        http_adapter.close()
        return self

    def __exit__(self, *exc):
        http_adapter.ALLOW_PRIVATE = self.saved[0]
        http_adapter._EXTRA_PUBLIC.clear()
        http_adapter._EXTRA_PUBLIC.update(self.saved[1])
        http_adapter._getaddrinfo = self.saved[2]
        http_adapter.close()


def _blocked(url: str) -> str:
    try:
        http_adapter.get(url, headers=UA, timeout=3)
    except http_adapter.BlockedAddress as e:
        return str(e)
    raise AssertionError(f"막혀야 하는데 열렸다: {url}")


def test_is_public_ip_table():
    pub = http_adapter.is_public_ip
    for ip in ("8.8.8.8", "93.184.216.34", "2606:4700:4700::1111"):
        assert pub(ip), ip
    for ip in ("127.0.0.1", "10.0.0.5", "172.16.3.4", "192.168.1.1", "169.254.169.254", "0.0.0.0",
               "100.64.0.1",                       # CGNAT(공유 주소 공간)
               "::1", "fe80::1", "fc00::1", "fd00:ec2::254",
               "::ffff:127.0.0.1", "::ffff:169.254.169.254",      # IPv4-mapped
               "64:ff9b::a9fe:a9fe",               # NAT64 로 싼 169.254.169.254
               "2002:7f00:1::",                    # 6to4 로 싼 127.0.0.1
               "not-an-ip"):
        assert not pub(ip), ip


def test_private_targets_are_blocked_before_connecting():
    inner = Site(bind="127.0.0.1")           # '서버 안쪽' 서비스 — 여기에 요청이 닿으면 안 된다
    try:
        with guard({"internal.test": ["10.1.2.3"], "meta.test": ["169.254.169.254"],
                    "mixed.test": ["93.184.216.34", "127.0.0.1"]}):
            for url in (f"http://127.0.0.1:{inner.port}/", f"http://localhost:{inner.port}/",
                        "http://169.254.169.254/latest/meta-data/", "http://[::1]/",
                        "http://internal.test/", "http://meta.test/",
                        f"http://mixed.test:{inner.port}/"):       # 하나라도 안쪽이면 통째로 막는다
                assert "공개 인터넷 주소" in _blocked(url), url
            assert inner.log == [], f"막았다면서 안쪽 서버에 요청이 갔다: {inner.log}"
            # 수집기는 예외가 아니라 '못 가져왔다' 한 줄로 남긴다
            row = collect_page.fetch(f"http://127.0.0.1:{inner.port}/")
            assert row["status"] is None and row["error"].startswith("BlockedAddress"), row
            c = collect_crawl.fetch("http://169.254.169.254/")
            assert c["status"] is None and c["error"].startswith("BlockedAddress"), c
    finally:
        inner.close()


def test_redirect_hops_are_each_checked():
    pub, inner = Site(), Site(bind="127.0.0.1")
    try:
        pub.redirects = {
            "/to-meta": "http://169.254.169.254/latest/meta-data/",
            "/to-private": "http://10.0.0.7/admin",
            "/to-localhost": f"http://localhost:{inner.port}/secret",
            "/to-loopback": f"http://127.0.0.1:{inner.port}/secret",
            "/two-hops": "/to-loopback",
            "/to-file": "file:///etc/passwd",
            "/ok": "/page",
        }
        with guard():
            ok = http_adapter.get(pub.base + "/ok", headers=UA, timeout=3)
            assert ok.status == 200 and ok.final_url == pub.base + "/page", (ok.status, ok.final_url)
            assert ok.chain == ((pub.base + "/ok", 302),), ok.chain
            for path in ("/to-meta", "/to-private", "/to-localhost", "/to-loopback", "/two-hops"):
                assert "공개 인터넷 주소" in _blocked(pub.base + path), path
            assert inner.log == [], f"리다이렉트를 따라 안쪽 서버에 닿았다: {inner.log}"
            try:
                http_adapter.get(pub.base + "/to-file", headers=UA, timeout=3)
                raise AssertionError("file:// 로 넘어갔다")
            except http_adapter.BlockedAddress:
                pass
            # sameAs 확인(check_link)도 같은 길이다 — 남의 주소를 여는 자리
            assert collect_page.check_link(pub.base + "/ok") == 200
            assert collect_page.check_link(pub.base + "/to-loopback") is None
            assert inner.log == []
    finally:
        pub.close()
        inner.close()


def test_redirect_loop_stops():
    pub = Site()
    try:
        pub.redirects = {"/a": "/b", "/b": "/a"}
        with guard():
            try:
                http_adapter.get(pub.base + "/a", headers=UA, timeout=3)
                raise AssertionError("끝없는 리다이렉트를 계속 따라갔다")
            except Exception as e:
                assert type(e).__name__ == "TooManyRedirects", repr(e)
            assert len(pub.log) <= http_adapter.MAX_REDIRECTS + 1, len(pub.log)
    finally:
        pub.close()


def test_dns_rebinding_is_checked_per_request_and_pinned():
    pub, inner = Site(), Site(bind="127.0.0.1")
    try:
        # 1) 등록 때는 공인 주소였던 이름이 나중에 안쪽으로 바뀐다 — 요청마다 다시 보므로 막힌다.
        answers = [[PUBLIC_IP], ["127.0.0.1"]]
        with guard({"rebind.test": lambda: answers.pop(0) if len(answers) > 1 else answers[0]}) as g:
            url = f"http://rebind.test:{pub.port}/page"
            assert http_adapter.get(url, headers=UA, timeout=3).status == 200
            http_adapter.close()             # 살아 있던 연결을 버린다 → 다음 요청은 새로 푼다
            assert "공개 인터넷 주소" in _blocked(url)
        # 2) 검사한 그 IP 로만 붙는다 — 검사와 연결 사이에 이름을 다시 풀지 않는다.
        #    (두 번 풀면 두 번째 답이 안쪽이라 inner 에 닿는다.)
        flips = [[PUBLIC_IP]]
        with guard({"pin.test": lambda: flips.pop(0) if flips else ["127.0.0.1"]}) as g:
            # inner 와 pub 의 포트가 달라 잘못 붙으면 연결 실패로 드러나지만, 조회 횟수로도 못 박는다
            r = http_adapter.get(f"http://pin.test:{pub.port}/page", headers=UA, timeout=3)
            assert r.status == 200 and g.lookups.count("pin.test") == 1, g.lookups
        assert inner.log == [], inner.log
    finally:
        pub.close()
        inner.close()


def test_body_is_capped_and_marked_truncated():
    pub = Site()
    saved = http_adapter.MAX_FETCH_BYTES
    try:
        with guard():
            full = http_adapter.get(pub.base + "/huge", headers=UA, timeout=3)
            assert full.bytes == 50_006 and not full.truncated, (full.bytes, full.truncated)
            http_adapter.MAX_FETCH_BYTES = 10_000
            cut = http_adapter.get(pub.base + "/huge", headers=UA, timeout=3)
            assert cut.truncated and cut.bytes == 10_000 and len(cut.text) == 10_000, (cut.bytes, cut.truncated)
            assert http_adapter.get(pub.base + "/page", headers=UA, timeout=3).status == 200, "자른 뒤 연결이 망가졌다"
    finally:
        http_adapter.MAX_FETCH_BYTES = saved
        pub.close()


def test_allow_private_is_an_explicit_opt_in():
    inner = Site(bind="127.0.0.1")
    try:
        with guard():
            http_adapter.ALLOW_PRIVATE = True            # SEO_MINER_ALLOW_PRIVATE_FETCH=1 과 같은 상태
            assert http_adapter.get(inner.base + "/page", headers=UA, timeout=3).status == 200
    finally:
        inner.close()


def test_registration_uses_the_same_rule():
    """등록 때의 검사(server/app.py _site_url)와 가져올 때의 검사가 같은 판정 한 벌을 쓴다."""
    src = (Path(__file__).resolve().parents[3] / "server" / "app.py").read_text(encoding="utf-8")
    body = src.split("def _site_url(", 1)[1].split("\ndef ", 1)[0]
    assert "http_adapter.is_public_ip" in body, "_site_url 이 http_adapter.is_public_ip 를 안 쓴다 — 판정이 두 벌이다"
    assert ".is_global" not in body, "_site_url 에 제 판정(.is_global)이 남았다"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)} passed")
