#!/usr/bin/env python3
"""create 커밋 전 JSON-LD 검사 — `python test_check_jsonld.py` (임시 git 리포에서만 돈다).

두 층을 같이 본다: 규칙의 정본(capture/scripts/jsonld_check.value_issues)과, 그걸 리포의
바뀐 파일에 물리는 훅(check_jsonld). 훅이 규칙 사본을 갖기 시작하면 여기서 걸린다.
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.resolve().parents[1] / "capture" / "scripts"))
import check_jsonld  # noqa: E402
import jsonld_check  # noqa: E402

GOOD = {"@context": "https://schema.org", "@type": "Article", "headline": "제목",
        "datePublished": "2026-03-10T09:00:00+09:00", "dateModified": "2026-03-11",
        "image": ["https://t.com/a.png"], "author": {"@type": "Person", "name": "홍길동",
                                                      "url": "https://t.com/about"}}


def rules(data) -> list[tuple[str, str]]:
    return sorted((i["rule"], i["level"]) for i in jsonld_check.value_issues(data))


# ── 규칙(정본) ────────────────────────────────────────────────────────────
assert rules(GOOD) == [], rules(GOOD)
# @context — 없음·남의 어휘는 오류, http·끝 슬래시·@vocab·목록은 통과(schema.org FAQ).
assert rules({"@type": "Article"}) == [("context", "error")]
assert rules({"@context": "https://example.org/v", "@type": "X"}) == [("context", "error")]
for ctx in ("http://schema.org", "https://schema.org/", {"@vocab": "https://schema.org/"},
            ["https://schema.org", {"x": "y"}]):
    assert rules({"@context": ctx, "@type": "Thing"}) == [], ctx
# @graph 안의 항목은 맨 위 @context 를 물려받는다 — 항목마다 요구하면 정상 마크업이 전부 걸린다.
assert rules({"@context": "https://schema.org",
              "@graph": [{"@type": "WebSite", "url": "https://t.com/"}]}) == []
# 자리표시 — 꼴로 잡는다(언어 불문). 한 값에 오류는 하나다.
for v in ("[Business Name]", "[확인 필요]", "{{ site.title }}", "${title}", "TODO", "REPLACE_ME",
          "Lorem ipsum dolor", "https://www.example.com/a", "info@example.org"):
    got = rules({**GOOD, "headline": v})
    assert got == [("placeholder", "error")], (v, got)
# 멀쩡한 글이 자리표시로 잡히면 훅이 꺼진다 — 대괄호가 문장 속에 있거나 낱말이 겹칠 뿐인 것.
for v in ("가격 [부가세 포함] 안내", "Todos los productos", "examplecompany.com 소개", "XS 사이즈"):
    assert rules({**GOOD, "headline": v}) == [], v
# 날짜 — ISO 8601 이 아니면 오류, 없는 날짜도 오류, 시각에 시간대가 없으면 주의.
for v in ("2026/03/10", "March 10, 2026", "10-03-2026", "2026-02-30", "2026-13-01", 20260310):
    assert rules({**GOOD, "datePublished": v}) == [("date", "error")], v
assert rules({**GOOD, "datePublished": "2026-03-10T09:00:00"}) == [("timezone", "warn")]
assert rules({**GOOD, "datePublished": "2026-03-10T09:00Z"}) == []
assert rules({"@context": "https://schema.org", "@type": "Organization", "foundingDate": "1999"}) == []
assert rules({"@context": "https://schema.org", "@type": "Event", "doorTime": "19:30"}) == []
# 상대 URL — 주의(구글 문서에 '절대 주소여야 한다'는 문장이 없다. jsonld_check 머리말).
assert rules({**GOOD, "image": "/img/a.png"}) == [("relative_url", "warn")]
assert rules({**GOOD, "author": {"@type": "Person", "url": "about"}}) == [("relative_url", "warn")]
assert rules({**GOOD, "@id": "#article"}) == []                      # 조각 식별자는 정상 용법
bad = jsonld_check.value_issues({**GOOD, "image": ["https://t.com/a.png", "//cdn/x.png"]})
assert [i["path"] for i in bad] == ["image[1]"], bad                 # 어느 자리인지 짚는다
assert jsonld_check.has_errors([{"level": "warn"}]) is False
assert jsonld_check.has_errors(jsonld_check.value_issues({"@type": "X"}))
# 목록(맨 위가 배열)도 받는다.
assert rules([GOOD, {"@type": "Thing"}]) == [("context", "error")]

# ── 블록 꺼내기 ───────────────────────────────────────────────────────────
html = ('<script nonce="a>b" type="application/ld+json">' + json.dumps(GOOD) + "</script>"
        "<script>var x = 1</script>"
        "<script type='application/ld+json'>{ \"@type\": \"Article\", }</script>")
r = check_jsonld.check_text(html, "a.html")
assert r["checked"] == 2 and [i["rule"] for i in r["issues"]] == ["json"], r
# 템플릿으로 짓는 블록은 틀렸다고 하지 않고 '못 읽음'으로 센다.
for body, name in (("{JSON.stringify(schema)}", "a.tsx"), ("<?php echo $ld; ?>", "a.php"),
                   ("{{ ld | safe }}", "a.njk")):
    r = check_jsonld.check_text(f'<script type="application/ld+json">{body}</script>', name)
    assert r == {"issues": [], "checked": 0, "unread": 1}, (name, r)
# 낱 JSON 파일은 JSON-LD 일 때만 본다.
assert check_jsonld.check_text('{"name": "pkg", "version": "1.0.0"}', "package.json")["checked"] == 0
assert check_jsonld.check_text(json.dumps({"@type": "Thing"}), "ld.json")["issues"][0]["rule"] == "context"

# ── 훅: 임시 리포에서 스테이징된 파일만 ───────────────────────────────────
repo = Path(tempfile.mkdtemp(prefix="seo-miner-jsonld-"))


def git(*a):
    p = subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t.t",
                        "-c", "core.autocrlf=false", *a], capture_output=True, text=True)
    assert p.returncode == 0, (a, p.stderr)
    return p.stdout


def page(ld: dict) -> str:
    return f'<html><script type="application/ld+json">{json.dumps(ld, ensure_ascii=False)}</script></html>'


git("init", "-q")
(repo / "ok.html").write_text(page(GOOD), encoding="utf-8")
(repo / "old.html").write_text(page({**GOOD, "headline": "[Title]"}), encoding="utf-8")
git("add", "."); git("commit", "-q", "-m", "base")
# 이미 커밋된 옛 하자(old.html)는 이번 커밋의 일이 아니다 — 바뀐 파일만 본다.
assert check_jsonld.main(["--repo", str(repo), "--staged"]) == 0
(repo / "new.html").write_text(page({**GOOD, "datePublished": "2026/03/10", "image": "/a.png"}),
                               encoding="utf-8")
git("add", "new.html")
rep = check_jsonld.run(str(repo), [], True)
assert (rep["errors"], rep["warns"]) == (1, 1) and rep["files"][0]["file"] == "new.html", rep
assert check_jsonld.main(["--repo", str(repo), "--staged"]) == 1          # 오류 → 커밋하지 않는다
# 스테이징한 뒤 작업 트리만 고친 것은 커밋에 안 들어간다 — 커밋될 내용(인덱스)을 읽어야 한다.
(repo / "new.html").write_text(page(GOOD), encoding="utf-8")
assert check_jsonld.main(["--repo", str(repo), "--staged"]) == 1
git("add", "new.html")
assert check_jsonld.main(["--repo", str(repo), "--staged"]) == 0
# 주의뿐이면 통과(0)하되 줄은 낸다.
(repo / "warn.html").write_text(page({**GOOD, "image": "/a.png"}), encoding="utf-8")
git("add", "warn.html")
rep = check_jsonld.run(str(repo), [], True)
assert rep["errors"] == 0 and rep["warns"] == 1, rep
assert "주의" in check_jsonld.render(rep) and "/a.png" in check_jsonld.render(rep)
# --staged 없이: 아직 add 안 한 새 파일도 본다(작업 트리).
(repo / "untracked.md").write_text(page({"@type": "Article"}), encoding="utf-8")
assert check_jsonld.run(str(repo), [], False)["errors"] == 1
# 파일을 직접 지정하면 리포 상태와 무관하게 그 파일을 본다.
assert check_jsonld.main([str(repo / "old.html")]) == 1

# 훅은 규칙 사본을 갖지 않는다 — 규칙 이름은 정본에만 있다.
src = (HERE / "check_jsonld.py").read_text(encoding="utf-8")
assert "jsonld_check.value_issues(" in src
for word in ("DATE_PROPS", "URL_PROPS", "_PLACEHOLDER", "schema\\.org"):
    assert word not in src, f"check_jsonld.py 가 규칙 사본을 갖기 시작했다: {word}"

print(f"ok — JSON-LD 값 규칙(@context·자리표시·날짜·상대 URL)과 커밋 전 훅 ({repo})")
