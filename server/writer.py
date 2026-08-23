"""기회 → 리포에 낼 콘텐츠. create 스킬이 Claude 에게 시키던 일을 서버에서 한다.

create SKILL.md 의 철칙을 그대로 옮긴 것:
  1. 발견 우선 — 프로필 없이 쓰지 않는다 (discover_profile 먼저)
  2. 관례 모방 — frontmatter 키·파일명·디렉토리는 리포에서 관찰한 것만 쓴다
  3. 발행 게이트 — main 에 직접 쓰지 않는다 (gh.open_pr 가 브랜치를 판다)
  4. 데이터 진실성 — 수치는 Brain 값만, 없는 사실은 만들지 않는다

모델은 OpenRouter 로 부른다. AI 인용 체크가 쓰는 키(OPENROUTER_API_KEY)를 그대로 쓴다 —
별도 Anthropic 키를 두지 않는다.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "skills" / "capture" / "scripts"))

import gh                                     # noqa: E402
import serp_adapter                           # noqa: E402

MODEL = os.environ.get("SEOMINER_WRITER_MODEL", "anthropic/claude-sonnet-4.5")
CONTENT_HINT = re.compile(
    r"\.(md|mdx|markdown|html|astro|njk|liquid)$", re.I)
SKIP = re.compile(r"(^|/)(node_modules|\.git|dist|build|vendor|\.next|public/assets)/", re.I)


class WriterError(RuntimeError):
    pass


def _ask(prompt: str, max_tokens: int = 4000) -> str:
    """OpenRouter 한 방. collect_ai 와 같은 키를 쓴다."""
    import requests
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise WriterError("콘텐츠 작성 기능이 아직 연결되지 않았습니다 (OPENROUTER_API_KEY 미설정)")
    r = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        timeout=serp_adapter.TIMEOUTS.get("openrouter", 120),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={"model": MODEL, "max_tokens": max_tokens,
              "messages": [{"role": "user", "content": prompt}]})
    if r.status_code >= 400:
        raise WriterError(f"글 작성에 실패했습니다. 잠시 후 다시 시도해 주세요 ({r.status_code})")
    return r.json()["choices"][0]["message"]["content"]


def _json_block(text: str) -> dict:
    """모델이 앞뒤에 말을 붙여도 JSON 만 꺼낸다."""
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise WriterError("글 작성에 실패했습니다. 다시 시도해 주세요")
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError as e:
        raise WriterError(f"글 작성에 실패했습니다. 다시 시도해 주세요 (응답 형식 오류)")


def discover_profile(token: str, repo: str, branch: str) -> dict:
    """리포의 관례를 찾아 캐시한다 — /create profile 에 해당.

    스택별 지식을 코드에 두지 않는다. 리포에서 관찰한 것만 쓴다(철칙 2).
    """
    paths = [p for p in gh.tree(token, repo, branch) if not SKIP.search(p)]
    content = [p for p in paths if CONTENT_HINT.search(p)][:400]
    if not content:
        raise WriterError("저장소에서 글이 있는 위치를 찾지 못했습니다. 다른 저장소를 연결해 주세요")

    # 가장 깊고 흔한 디렉토리의 파일을 본보기로 읽는다 — 거기가 보통 글이 사는 곳이다.
    from collections import Counter
    dirs = Counter(str(Path(p).parent) for p in content if "/" in p)
    top = [d for d, _ in dirs.most_common(3)]
    samples = [p for p in content if str(Path(p).parent) in top][:3]
    bodies = []
    for p in samples:
        try:
            bodies.append(f"--- {p} ---\n{gh.read_file(token, repo, p, branch)[:2500]}")
        except gh.GitHubError:
            pass
    if not bodies:
        raise WriterError("저장소의 글 내용을 읽지 못했습니다. 다른 저장소를 연결해 주세요")

    out = _ask(
        "너는 이 저장소의 콘텐츠 관례를 파악한다. 추측하지 말고 아래에서 관찰된 것만 적어라.\n\n"
        f"[파일 경로 일부]\n" + "\n".join(content[:120]) + "\n\n"
        f"[본보기 파일]\n" + "\n\n".join(bodies) + "\n\n"
        "JSON 만 출력해라:\n"
        '{"stack":"...","content_dir":"글이 실제로 사는 디렉토리",'
        '"extension":".md 등","filename_rule":"관찰된 파일명 규칙",'
        '"frontmatter_keys":["실제로 쓰인 키만"],'
        '"frontmatter_example":"본보기에서 그대로 옮긴 frontmatter 한 벌",'
        '"notes":"글 쓸 때 지켜야 할 관례 2~3줄"}', 2000)
    prof = _json_block(out)
    if not prof.get("content_dir"):
        raise WriterError("저장소 구조를 파악하지 못했습니다. 다른 저장소를 연결해 주세요")
    prof["samples"] = samples
    return prof


def write_for(opportunity: dict, profile: dict, project: dict,
              evidence: dict | None = None) -> dict:
    """기회 하나 → 파일 하나. 반환 {path, content, title, summary}."""
    ev = evidence or {}
    facts = "\n".join(f"- {k}: {v}" for k, v in ev.items()) or "- (추가 수치 없음)"
    out = _ask(
        f"너는 {project.get('domain')} 의 콘텐츠를 쓴다. 아래 저장소 관례를 그대로 따른다.\n\n"
        f"[저장소 관례]\n{json.dumps(profile, ensure_ascii=False, indent=1)}\n\n"
        f"[다룰 기회]\n종류: {opportunity.get('kind')}\n대상: {opportunity.get('target')}\n"
        f"근거: {opportunity.get('reasoning') or '(없음)'}\n\n"
        f"[검색 실적 — 이 수치만 인용할 수 있다]\n{facts}\n\n"
        "규칙:\n"
        "- frontmatter 키는 관례에 있는 것만 쓴다. 새 키를 발명하지 않는다.\n"
        "- 위에 없는 수치·기능·가격을 지어내지 않는다. 모르면 쓰지 않는다.\n"
        f"- 언어: {project.get('locale', 'ko-KR')}\n"
        "- 본문은 검색 의도에 바로 답하고 시작한다. 서론으로 미루지 않는다.\n\n"
        "JSON 만 출력해라:\n"
        '{"path":"저장소 루트 기준 전체 경로","title":"...",'
        '"summary":"이 글이 무엇을 노리는지 2줄 — PR 설명에 쓴다",'
        '"content":"frontmatter 를 포함한 파일 전문"}')
    d = _json_block(out)
    for k in ("path", "content", "title"):
        if not d.get(k):
            raise WriterError(f"글 작성에 실패했습니다. 다시 시도해 주세요 ({k} 누락)")
    if d["path"].startswith("/") or ".." in d["path"]:
        raise WriterError(f"저장소 밖의 경로라 작성을 중단했습니다 ({d['path']})")
    return d


def slug(text: str, n: int = 40) -> str:
    s = re.sub(r"[^a-z0-9가-힣]+", "-", str(text).lower()).strip("-")
    return (s[:n].strip("-") or "opp")


def demo() -> None:
    g = globals()
    orig = g["_ask"]
    try:
        g["_ask"] = lambda p, max_tokens=4000: (
            '설명 붙임 {"path":"src/content/blog/a.md","title":"T","summary":"S",'
            '"content":"---\\ntitle: T\\n---\\n본문"} 끝')
        d = write_for({"kind": "striking_distance", "target": "ai tier list"},
                      {"content_dir": "src/content/blog"}, {"domain": "x.com"})
        assert d["path"] == "src/content/blog/a.md", d
        assert d["content"].startswith("---"), d

        g["_ask"] = lambda p, max_tokens=4000: '{"path":"../../etc/passwd","title":"t","content":"x"}'
        try:
            write_for({}, {}, {})
            raise AssertionError("저장소 밖 경로를 막지 못했다")
        except WriterError as e:
            assert "저장소 밖" in str(e), e

        g["_ask"] = lambda p, max_tokens=4000: "JSON 아님"
        try:
            write_for({}, {}, {})
            raise AssertionError("JSON 아닌 응답을 통과시켰다")
        except WriterError:
            pass

        assert slug("AI Tier List!! 2026") == "ai-tier-list-2026", slug("AI Tier List!! 2026")
        print("writer: ok")
    finally:
        g["_ask"] = orig


if __name__ == "__main__":
    demo()
