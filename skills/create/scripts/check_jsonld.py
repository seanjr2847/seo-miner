#!/usr/bin/env python3
"""create 의 커밋 전 검사 — 이번에 바꾼 파일 속 JSON-LD 의 **값**을 본다.

규칙은 여기 없다. 정본은 capture 의 `jsonld_check.value_issues()` 하나이고(자리표시·날짜·
@context·상대 URL), 이 파일은 리포에서 ld+json 을 꺼내 그 함수에 넘기는 일만 한다.

  python check_jsonld.py --repo . --staged        # 스테이징된 파일(커밋 직전)
  python check_jsonld.py --repo .                 # 기본 브랜치 대비 이 브랜치가 바꾼 파일 + 작업 트리
  python check_jsonld.py path/a.html path/b.json  # 지정한 파일만

종료코드: error 가 하나라도 있으면 1(커밋하지 않는다), warn 뿐이면 0.

못 읽는 것: 템플릿 표현식으로 짓는 블록({JSON.stringify(x)}·<?php … ?>·{{ x }})은 정적으로
값을 알 수 없다. 틀렸다고 하지 않고 '읽지 못함'으로 센다 — 빌드 산출물(HTML)을 따로 넘기면 본다.
"""
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "capture" / "scripts"))
import jsonld_check  # noqa: E402

# 따옴표 안의 '>' 가 태그를 끝내지 않게 속성 값을 한 덩이로 먹는다.
_ATTRS = r'(?:"[^"]*"|\'[^\']*\'|[^"\'>])*'
_SCRIPT = re.compile(r"<script\b(" + _ATTRS + r")>(.*?)</script\s*>", re.S | re.I)
_LD_TYPE = re.compile(r"""(?:^|\s)type\s*=\s*["']?application/ld\+json["']?(?=\s|$)""", re.I)
_TEMPLATE = re.compile(r"^\s*(?:<\?|<%|\{\{|\{%|\{@html\b|\$\{|\{\s*[A-Za-z_$(])")

MARKUP_EXT = {".html", ".htm", ".md", ".mdx", ".astro", ".vue", ".svelte", ".jsx", ".tsx",
              ".njk", ".liquid", ".hbs", ".ejs", ".php", ".erb", ".twig"}
JSON_EXT = {".json", ".jsonld"}


def blocks(text: str, filename: str = "") -> list[tuple[str, str]]:
    """파일 글 → [(종류, 원문)]. 종류: 'json'(검사할 것) | 'template'(정적으로 못 읽는 것)."""
    ext = Path(filename).suffix.lower()
    if ext in JSON_EXT:
        # 낱 JSON 파일은 JSON-LD 일 때만 본다 — package.json 을 스키마로 읽지 않는다.
        return [("json", text)] if '"@context"' in text or '"@type"' in text else []
    out = []
    for attrs, body in _SCRIPT.findall(text):
        if not _LD_TYPE.search(attrs) or not body.strip():
            continue
        out.append(("template" if _TEMPLATE.match(body) else "json", body))
    return out


def check_text(text: str, filename: str = "") -> dict:
    """→ {"issues": [...], "checked": 본 블록 수, "unread": 못 읽은 블록 수}."""
    issues, checked, unread = [], 0, 0
    for n, (kind, raw) in enumerate(blocks(text, filename), 1):
        if kind == "template":
            unread += 1
            continue
        checked += 1
        try:
            data = json.loads(raw)
        except ValueError as e:
            issues.append({"rule": "json", "level": "error", "path": f"블록 {n}", "value": "",
                           "msg": f"JSON 으로 읽히지 않습니다({str(e)[:80]})"})
            continue
        for i in jsonld_check.value_issues(data):
            issues.append({**i, "path": f"블록 {n} {i['path']}".strip()})
    return {"issues": issues, "checked": checked, "unread": unread}


def _git(repo: str, *args: str) -> str | None:
    p = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                       encoding="utf-8", errors="replace")
    return p.stdout if p.returncode == 0 else None


def _base(repo: str) -> str | None:
    for cand in ("origin/HEAD", "origin/main", "origin/master", "main", "master"):
        mb = _git(repo, "merge-base", "HEAD", cand)
        if mb and mb.strip():
            return mb.strip()
    return None


def changed(repo: str, staged: bool) -> list[str]:
    """검사할 파일 — 지워진 것은 뺀다. staged 면 스테이징만, 아니면 브랜치가 바꾼 것 + 작업 트리."""
    names: list[str] = []
    if staged:
        names += (_git(repo, "diff", "--cached", "--name-only", "--diff-filter=ACMR") or "").splitlines()
    else:
        base = _base(repo)
        if base:
            names += (_git(repo, "diff", "--name-only", "--diff-filter=ACMR", base, "HEAD") or "").splitlines()
        names += (_git(repo, "diff", "--name-only", "--diff-filter=ACMR", "HEAD") or "").splitlines()
        names += (_git(repo, "ls-files", "--others", "--exclude-standard") or "").splitlines()
    seen, out = set(), []
    for n in names:
        n = n.strip()
        if n and n not in seen and Path(n).suffix.lower() in MARKUP_EXT | JSON_EXT:
            seen.add(n)
            out.append(n)
    return out


def _read(repo: str | None, name: str, staged: bool) -> str | None:
    if staged and repo:
        return _git(repo, "show", f":{name}")          # 커밋될 내용 그대로(작업 트리와 다를 수 있다)
    p = Path(repo or ".") / name
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def run(repo: str | None, files: list[str], staged: bool) -> dict:
    names = files or changed(repo or ".", staged)
    report = {"files": [], "errors": 0, "warns": 0, "checked": 0, "unread": 0}
    for name in names:
        text = _read(repo if not files else None, name, staged and not files)
        if text is None:
            continue
        r = check_text(text, name)
        report["checked"] += r["checked"]
        report["unread"] += r["unread"]
        if r["issues"]:
            report["files"].append({"file": name, "issues": r["issues"]})
        report["errors"] += sum(i["level"] == "error" for i in r["issues"])
        report["warns"] += sum(i["level"] == "warn" for i in r["issues"])
    return report


def render(report: dict) -> str:
    lines = []
    for f in report["files"]:
        lines.append(f["file"])
        for i in f["issues"]:
            mark = "오류" if i["level"] == "error" else "주의"
            val = f" — {i['value']}" if i["value"] != "" else ""
            lines.append(f"  [{mark}] {i['path']}: {i['msg']}{val}")
    tail = (f"JSON-LD 블록 {report['checked']}개 검사: 오류 {report['errors']}건, "
            f"주의 {report['warns']}건")
    if report["unread"]:
        tail += f" · 템플릿으로 짓는 블록 {report['unread']}개는 읽지 못함(빌드 산출물로 확인)"
    lines.append(tail)
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="create 커밋 전 JSON-LD 값 검사")
    ap.add_argument("files", nargs="*")
    ap.add_argument("--repo", default=None)
    ap.add_argument("--staged", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    report = run(a.repo, a.files, a.staged)
    print(json.dumps(report, ensure_ascii=False, indent=2) if a.json else render(report))
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    for s in (sys.stdout, sys.stderr):
        s.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
