import base64
import os
import requests

API_URL = "https://api.resend.com/emails"
DEFAULT_FROM = "seo-miner <onboarding@resend.dev>"
DEFAULT_PUBLIC_URL = "http://localhost:8000"
TIMEOUT_SECONDS = 10

LABELS = {
    "clicks": "클릭",
    "impressions": "노출",
    "opportunities": "개선 기회",
    "keywords": "추적 키워드 수",
    "top": "상위 키워드",
}


def available() -> bool:
    return bool(os.environ.get("RESEND_API_KEY"))


def send(to: str, subject: str, html: str,
         attachments: list[dict] | None = None) -> bool:
    """attachments: [{"filename": ..., "content": bytes}] — Resend 는 base64 를 받는다.
    한도는 첨부 인코딩 후 메일당 40MB 라 보고서(수십 KB)는 여유롭다."""
    api_key = os.environ.get("RESEND_API_KEY")
    if not api_key:
        return False
    from_addr = os.environ.get("SEOMINER_MAIL_FROM", DEFAULT_FROM)
    try:
        resp = requests.post(
            API_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=_payload(from_addr, to, subject, html, attachments),
            timeout=TIMEOUT_SECONDS,
        )
        return 200 <= resp.status_code < 300
    except Exception as exc:
        print(f"[mailer] send failed: {exc}")
        return False


def _payload(from_addr: str, to: str, subject: str, html: str,
             attachments: list[dict] | None) -> dict:
    body = {"from": from_addr, "to": [to], "subject": subject, "html": html}
    if attachments:
        body["attachments"] = [
            {"filename": a["filename"],
             "content": base64.b64encode(a["content"]).decode("ascii")}
            for a in attachments if a.get("content")]
    return body


def _stats_row(label: str, value) -> str:
    return (
        '<tr>'
        '<td style="padding:10px 12px;border-bottom:1px solid #e5e7eb;'
        'font-size:13px;color:#6b7280;">'
        f'{label}</td>'
        '<td style="padding:10px 12px;border-bottom:1px solid #e5e7eb;'
        'font-size:14px;color:#121714;text-align:right;font-weight:bold;">'
        f'{value}</td>'
        '</tr>'
    )


def run_done(to: str, project: str, stats: dict,
             report: bytes | None = None) -> bool:
    """분석 완료 알림. report 를 주면 보고서 HTML 을 첨부한다 —
    "들어와서 보세요"보다 "보고서가 왔습니다"가 마케터의 기대에 맞다."""
    public_url = os.environ.get("SEOMINER_PUBLIC_URL", DEFAULT_PUBLIC_URL).rstrip("/")
    dashboard_url = f"{public_url}/d"

    rows = []
    for key, label in LABELS.items():
        if key in stats:
            rows.append(_stats_row(label, stats[key]))

    if rows:
        rows_html = "".join(rows)
    else:
        rows_html = (
            '<tr><td style="padding:10px 12px;font-size:13px;color:#6b7280;">'
            "수집된 데이터가 없습니다.</td></tr>"
        )

    subject = f"{project} 주간 SEO 보고서"

    html = (
        '<html><body style="margin:0;padding:0;background:#F7F8F5;'
        'font-family:Arial,Helvetica,sans-serif;color:#121714;">'
        '<table role="presentation" cellpadding="0" cellspacing="0" '
        'border="0" width="100%" style="background:#F7F8F5;">'
        '<tr><td align="center" style="padding:24px 12px;">'
        '<table role="presentation" cellpadding="0" cellspacing="0" '
        'border="0" width="560" style="max-width:560px;background:#ffffff;'
        'border:1px solid #e5e7eb;">'
        '<tr><td style="padding:28px 32px 8px 32px;font-size:20px;'
        'font-weight:bold;color:#22705F;">주간 SEO 보고서</td></tr>'
        '<tr><td style="padding:0 32px 24px 32px;font-size:14px;'
        'line-height:1.6;color:#121714;">'
        f'<strong style="color:#22705F;">{project}</strong> 프로젝트의 '
        "측정이 완료되었습니다. 아래 결과를 확인하세요.</td></tr>"
        '<tr><td style="padding:0 32px 24px 32px;">'
        '<table role="presentation" cellpadding="0" cellspacing="0" '
        'border="0" width="100%" style="border-collapse:collapse;">'
        f"{rows_html}"
        '</table></td></tr>'
        '<tr><td align="center" style="padding:8px 32px 32px 32px;">'
        f'<a href="{dashboard_url}" style="display:inline-block;'
        'background:#22705F;color:#ffffff;text-decoration:none;'
        'padding:12px 24px;font-size:14px;font-weight:bold;'
        'border-radius:4px;">대시보드 열기</a>'
        '</td></tr>'
        '<tr><td style="padding:16px 32px 24px 32px;font-size:12px;'
        'color:#6b7280;text-align:center;border-top:1px solid #e5e7eb;">'
        "seo-miner</td></tr>"
        "</table></td></tr></table></body></html>"
    )

    return send(to, subject, html)


def demo() -> None:
    env_keys = ("RESEND_API_KEY", "SEOMINER_MAIL_FROM", "SEOMINER_PUBLIC_URL")
    saved_env = {k: os.environ.get(k) for k in env_keys}
    saved_post = requests.post
    calls = []

    class _OkResp:
        status_code = 200

    class _FailResp:
        status_code = 500

    def fake_ok(url, **kwargs):
        calls.append((url, kwargs))
        return _OkResp()

    def fake_fail(url, **kwargs):
        calls.append((url, kwargs))
        return _FailResp()

    def fake_raising(url, **kwargs):
        calls.append((url, kwargs))
        raise requests.RequestException("boom")

    try:
        os.environ.pop("RESEND_API_KEY", None)
        requests.post = fake_ok
        assert available() is False, "case1: available() should be False without key"
        assert send("a@b.c", "s", "<p>h</p>") is False, "case1: send() should be False"
        assert calls == [], f"case1: no network calls expected, got {calls}"

        os.environ["RESEND_API_KEY"] = "test_key_123"
        os.environ["SEOMINER_MAIL_FROM"] = "test <t@e.com>"
        calls.clear()
        assert send("a@b.c", "subj", "<p>hi</p>") is True, "case2: send() should be True"
        assert len(calls) == 1, f"case2: expected 1 call, got {len(calls)}"
        url, kwargs = calls[0]
        assert url == API_URL, f"case2: wrong URL {url}"
        assert kwargs["headers"]["Authorization"] == "Bearer test_key_123", (
            f"case2: wrong auth header: {kwargs['headers'].get('Authorization')}"
        )
        body = kwargs["json"]
        assert body["to"] == ["a@b.c"], f"case2: wrong to: {body['to']}"
        assert body["subject"] == "subj", f"case2: wrong subject: {body['subject']}"
        assert body["html"] == "<p>hi</p>", f"case2: wrong html: {body['html']}"
        assert body["from"] == "test <t@e.com>", f"case2: wrong from: {body['from']}"

        requests.post = fake_fail
        calls.clear()
        assert send("a@b.c", "subj", "<p>hi</p>") is False, (
            "case2b: non-2xx response should return False"
        )
        assert len(calls) == 1, "case2b: expected exactly one call"

        requests.post = fake_raising
        calls.clear()
        try:
            result = send("a@b.c", "subj", "<p>hi</p>")
        except Exception as exc:
            raise AssertionError(f"case3: send() leaked exception: {exc}") from exc
        assert result is False, "case3: send() should return False on exception"
        assert len(calls) == 1, "case3: requests.post should have been invoked"

        requests.post = fake_ok
        calls.clear()
        partial_stats = {"clicks": 38, "impressions": 3524, "keywords": 100}
        ok = run_done("user@x.com", "테스트 프로젝트", partial_stats)
        assert ok is True, "case4: run_done() should succeed"
        assert len(calls) == 1, "case4: run_done should make exactly one HTTP call"
        _, kwargs = calls[0]
        body = kwargs["json"]
        html_body = body["html"]
        assert "테스트 프로젝트" in html_body, "case4: project name missing in HTML"
        assert "/d" in html_body, "case4: dashboard link missing in HTML"
        assert "개선 기회" not in html_body, (
            "case4: opportunities label should be absent when not in stats"
        )
        assert "클릭" in html_body, "case4: clicks label missing"
        assert "38" in html_body, "case4: clicks value missing"
        assert "노출" in html_body, "case4: impressions label missing"
        # 첨부: Resend 는 base64 문자열을 받는다. bytes 를 그대로 넣으면 직렬화가 깨진다.
        calls.clear()
        requests.post = fake_ok
        send("a@b.c", "s", "<p>x</p>",
             attachments=[{"filename": "r.html",
                           "content": "<html>한</html>".encode("utf-8")}])
        att = calls[-1][1]["json"]["attachments"][0]
        assert att["filename"] == "r.html", att
        assert isinstance(att["content"], str), "base64 문자열이어야 한다"
        assert base64.b64decode(att["content"]) == "<html>한</html>".encode("utf-8"), "내용 손상"
        assert "attachments" not in _payload("f", "t", "s", "h", None),             "첨부가 없으면 필드를 넣지 않는다"

        assert body["subject"] == "테스트 프로젝트 주간 SEO 보고서", (
            f"case4: wrong subject: {body['subject']}"
        )

        calls.clear()
        empty_stats = {}
        ok = run_done("user@x.com", "빈 프로젝트", empty_stats)
        assert ok is True
        _, kwargs = calls[0]
        empty_html = kwargs["json"]["html"]
        assert "수집된 데이터가 없습니다." in empty_html, (
            "case4b: empty-state row missing"
        )
        for label in LABELS.values():
            assert label not in empty_html, (
                f"case4b: label {label!r} should be absent with empty stats"
            )

        assert available() is True, "available() should be True when key set"
    finally:
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        requests.post = saved_post

    print("mailer: ok")


if __name__ == "__main__":
    demo()
