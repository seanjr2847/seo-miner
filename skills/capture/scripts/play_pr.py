#!/usr/bin/env python3
"""이번 달 할 일(play) 하나를 PR 로 — 로컬 대시보드의 [PR 만들기] 본체.

play 의 markdown(수정안 문서)을 요청문 파일로 쓰고, 그 사이트의 저장소 폴더에서
Claude Code 를 **헤드리스로**(`-p`) 돌린다: 새 브랜치 → 수정 → 커밋 → push →
`gh pr create`. 요청은 바로 돌아오고(백그라운드 프로세스), 화면은 상태를
GET /api/plays/apply 로 묻는다. 끝나면 PR 주소를 작업 기록(creations)에 묶인 기회
전부로 남기고 play 를 applied 로 둔다.

run_tool(dashboard.py)과 같은 규칙을 따른다:
  · 요청문 파일은 작업 자리(~/.capture/work/<사이트>/)에 남긴다 — 사용자의 리포 안에
    남의 파일을 떨구지 않는다. 이름은 play-<id>.md (기회는 opp-<id>.md).
  · 폴더의 정본은 paths.site_dirs() — 다만 run_tool 과 달리 작업 자리로 물러나지
    않는다. PR 은 git 저장소에서만 열린다.
  · 원격(호스팅) 사이트도 PR 은 이 PC 에서 만든다. play 는 호스팅 페이로드에서
    오고, 기록은 호스팅의 /api/creation 으로 간다 — 그 갈래는 dashboard 가 넘겨 주는
    load/record 가 이미 안다(여기서 remote 를 다시 판정하지 않는다).
  · 도구 표의 정본은 doctor.TOOLS — 헤드리스로 돌 도구 id 는 doctor.PR_TOOL.

일의 상태는 파일 하나(play-<id>.json)에 둔다 — 대시보드를 껐다 켜도 남고, 도는
중에 대시보드가 죽었으면 다음 조회가 로그를 읽어 마무리한다(_recover).

검사는 진짜 claude·gh·git 을 부르지 않는다 — _spawn·_start·_which 를 갈아 끼운다.

Usage:
  python play_pr.py --selfcheck
"""
import json
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "setup" / "scripts"))
import doctor  # noqa: E402  (도구 표 정본 — PR_TOOL 이 그중 하나를 가리킨다)
import paths   # noqa: E402  (사이트 ↔ 폴더 장부, 작업 자리)

TIMEOUT = 30 * 60        # 한 play 에 주는 시간(초). 넘기면 프로세스를 죽이고 실패로 둔다
BRANCH = "seo/play-{id}"  # PR 브랜치 이름 — 머지 확인(createdb sync)이 이 이름으로 PR 을 찾는다
# PR 주소 — GitHub Enterprise 도 같은 꼴(/<owner>/<repo>/pull/<n>)이라 호스트는 안 가린다.
PR_URL = re.compile(r"https?://[^\s<>\"'`]+/pull/\d+")

_which = shutil.which                                      # 검사가 갈아 끼운다
_start = lambda fn: threading.Thread(target=fn, daemon=True).start()   # noqa: E731
_lock = threading.Lock()   # 시작·마무리가 같은 파일을 두 번 쓰지 않게
_live: set[tuple[str, int]] = set()   # 이 프로세스가 지켜보는 일 — 없으면 _recover 가 맡는다


# ── 명령줄 ──────────────────────────────────────────────────────────────────

def argv(exe: str, play_id: int) -> list[str]:
    """헤드리스 실행 명령줄 — 이 함수 한 자리가 짓는다.

    요청문은 argv 가 아니라 **표준입력**으로 넣는다(_spawn): 문서가 길어 윈도우 명령줄
    한도(3.2만 자)에 걸릴 수 있고, 파일 경로를 주면 저장소 밖 파일 읽기 권한을 또
    열어야 한다.

    권한: 파일 수정은 acceptEdits 가 받고, 셸 명령은 아래 허용 목록만 돈다(-p 에서는
    목록 밖 명령이 묻는 대신 거절된다). push·브랜치 만들기는 **이 play 의 브랜치로만**
    — 다른 브랜치를 밀거나 main 에 force 하는 명령은 목록에 없다. --allowedTools 는
    값을 여럿 받으므로 맨 뒤에 둔다(뒤에 오는 인자를 도구 이름으로 먹는다).
    """
    br = BRANCH.format(id=int(play_id))
    tools = ["Read", "Edit", "Write", "Glob", "Grep",
             "Bash(git status:*)", "Bash(git diff:*)", "Bash(git log:*)",
             "Bash(git branch:*)", "Bash(git rev-parse:*)", "Bash(git remote:*)",
             f"Bash(git checkout -b {br}:*)", f"Bash(git switch -c {br}:*)",
             "Bash(git add:*)", "Bash(git commit:*)",
             f"Bash(git push -u origin {br}:*)",
             "Bash(gh pr create:*)", "Bash(gh pr view:*)"]
    return [exe, "-p", "--permission-mode", "acceptEdits", "--output-format", "text",
            "--allowedTools", *tools]


def instructions(play: dict, project: str) -> str:
    """요청문 머리 — 무엇을 어디까지 하고 마지막 줄에 무엇을 남기나.

    본문(수정안)은 play.markdown 그대로다 — 여기서 다시 짓지 않는다. 마지막 줄 약속
    (PR 주소 / `실패:`)은 parse_result 가 읽는 꼴이다.
    """
    pid = int(play["id"])
    br = BRANCH.format(id=pid)
    kws = ", ".join(str(k) for k in (play.get("keywords") or []))
    return (
        f"# 적용 작업 — {project} · 이번 달 할 일 #{pid}\n\n"
        "아래 '---' 뒤의 수정안 문서를 이 저장소에 그대로 적용하고 PR 을 엽니다. "
        "사람이 지켜보지 않으니 묻지 말고 끝까지 진행합니다.\n\n"
        f"1. 커밋 안 된 변경이 있으면 아무것도 바꾸지 말고 멈춥니다(마지막 줄: `실패: 커밋 안 된 변경이 있습니다`).\n"
        f"2. `git checkout -b {br}` 로 새 브랜치를 만듭니다.\n"
        f"3. 이 페이지를 만드는 파일을 찾아 문서의 변경만 **정확히** 넣습니다: {play.get('page') or ''}\n"
        "   제목·메타 설명·H1·소제목과 문단 초안·내부 링크·신뢰 요소 중 문서가 말한 것만.\n"
        "   문서에 없는 것은 바꾸지 않습니다. 파일을 못 찾으면 멈춥니다(마지막 줄: `실패: <이유>`).\n"
        "4. `git add` 뒤 한국어 한 줄 메시지로 `git commit` 합니다.\n"
        f"5. `git push -u origin {br}`\n"
        f"6. `gh pr create --head {br} --title \"<한국어 제목>\" --body \"<한국어 본문>\"`\n"
        "   본문에는 무엇을 왜 바꿨는지(문서의 요약·근거), 움직일 검색어"
        + (f"({kws})" if kws else "") + ", 기대 효과는 추정이라는 말을 넣습니다.\n"
        "7. **마지막 줄에는 PR 주소 한 줄만** 출력합니다. 못 만들었으면 마지막 줄을 "
        "`실패: <이유>` 로 씁니다.\n\n---\n\n"
    )


def parse_result(log: str, code: int | None) -> tuple[str | None, str]:
    """로그 → (PR 주소, 실패 이유). 주소는 로그의 **마지막** PR 링크다(문서 본문이
    남의 PR 을 인용해도 마지막 줄 약속이 이긴다). 주소가 있으면 종료 코드와 상관없이
    성공으로 친다 — PR 은 이미 열렸고, 기록을 빠뜨리는 것이 더 나쁘다."""
    urls = PR_URL.findall(log or "")
    if urls:
        return urls[-1].rstrip(".,)"), ""
    lines = [ln.strip() for ln in (log or "").splitlines() if ln.strip()]
    said = next((ln for ln in reversed(lines) if ln.startswith("실패:")), None)
    if said:
        return None, said[len("실패:"):].strip() or "이유를 안 남겼습니다"
    tail = lines[-1][:200] if lines else ""
    why = f"PR 주소를 못 받았습니다(종료 코드 {code})"
    return None, why + (f" — {tail}" if tail else "")


# ── 상태 파일 ───────────────────────────────────────────────────────────────

def _box(project: str) -> Path:
    b = paths.home() / "work" / project
    b.mkdir(parents=True, exist_ok=True)
    return b


def _job_file(project: str, play_id: int) -> Path:
    return _box(project) / f"play-{int(play_id)}.json"


def _read(project: str, play_id: int) -> dict | None:
    try:
        return json.loads(_job_file(project, play_id).read_text("utf-8"))
    except (OSError, ValueError):
        return None


def _write(job: dict) -> None:
    f = _job_file(job["project"], job["id"])
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(job, ensure_ascii=False, indent=1), "utf-8")
    tmp.replace(f)


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


# ── 준비 판정 ───────────────────────────────────────────────────────────────

def ready(project: str) -> dict:
    """이 사이트에서 PR 을 만들 수 있나 — {ok, why, fix, cwd, exe}.

    fix 는 화면이 붙일 손잡이다: "settings" 면 [설정]에서 고칠 수 있는 것(폴더).
    도구·gh 가 없는 것은 설정으로 못 고친다 — 무엇을 깔면 되는지만 말한다.
    """
    tool = doctor.tool_of(doctor.PR_TOOL)
    exe = _which(tool[2]) if tool else None
    if not exe:
        label = tool[1] if tool else doctor.PR_TOOL
        return {"ok": False, "fix": "", "why": f"{label} 이(가) 이 PC 에 없습니다 — PR 자동 "
                                              f"적용은 {label} 로 돕니다. 설치한 뒤 다시 누르세요."}
    if not _which("gh"):
        return {"ok": False, "fix": "", "why": "GitHub CLI(gh)가 이 PC 에 없습니다 — 설치하고 "
                                              "gh auth login 까지 하면 PR 을 열 수 있습니다."}
    d = paths.site_dirs().get(project)
    if not d or not Path(d).is_dir():
        return {"ok": False, "fix": "settings",
                "why": "이 사이트의 저장소 폴더를 아직 안 정했습니다 — [설정]의 사이트별 "
                       "로컬 폴더에 적으면 PR 을 그 폴더에서 만듭니다."}
    if not (Path(d) / ".git").exists():      # 워크트리면 .git 이 파일이다 — exists 로 둘 다 받는다
        return {"ok": False, "fix": "settings",
                "why": f"정해 둔 폴더가 git 저장소가 아닙니다: {d}"}
    return {"ok": True, "fix": "", "why": "", "cwd": str(Path(d)), "exe": exe}


# ── 실행 ────────────────────────────────────────────────────────────────────

def _spawn(cmd: list[str], cwd: str, stdin_file: Path, log_file: Path):
    """백그라운드 프로세스 하나 — 검사가 갈아 끼운다(진짜 claude 를 안 부르게).
    돌려주는 것은 wait(timeout)·kill()·pid 를 가진 것(Popen)."""
    flags = 0x08000000 if sys.platform == "win32" else 0    # CREATE_NO_WINDOW — 창을 안 띄운다
    with open(stdin_file, "rb") as fin, open(log_file, "wb") as fout:
        return subprocess.Popen(cmd, cwd=cwd, stdin=fin, stdout=fout,
                                stderr=subprocess.STDOUT, creationflags=flags)


def _find(data: dict, play_id: int) -> dict | None:
    return next((p for p in (data.get("plays") or [])
                 if str(p.get("id")) == str(play_id)), None)


def apply(body: dict, *, load, record, mark) -> dict:
    """POST /api/plays/apply 본체 — {project, id} 로 PR 만들기를 시작한다.

    load(project)   → 화면 페이로드(d.plays 가 든 것). 원격이면 호스팅 것.
    record(project, body) → 작업 기록 하나(/api/creation 과 같은 본문 꼴).
    mark(project, play_id) → play 를 applied 로.
    셋 다 dashboard 가 로컬/원격 갈래를 골라 넘긴다.

    실패는 {"ok": False, "error": …} 이고 그때는 아무것도 띄우지 않는다.
    """
    project = str(body.get("project") or "").strip()
    try:
        play_id = int(body.get("id") or 0)
    except (TypeError, ValueError):
        play_id = 0
    if not project or not play_id:
        return {"ok": False, "error": "어느 사이트의 어느 할 일인지 못 받았습니다."}

    with _lock:
        cur = _read(project, play_id)
        if cur and cur.get("status") == "running":
            return {"ok": False, "error": "이 할 일은 이미 PR 을 만드는 중입니다.",
                    "job": _public(cur)}
        if cur and cur.get("status") == "done" and not body.get("again"):
            return {"ok": False, "error": "이 할 일은 이미 PR 을 만들었습니다.",
                    "job": _public(cur)}
        r = ready(project)
        if not r["ok"]:
            return {"ok": False, "error": r["why"], "fix": r["fix"]}
        try:
            data = load(project) or {}
        except Exception as e:                  # 원격이 죽었거나 토큰이 끊겼거나
            return {"ok": False, "error": f"할 일을 불러오지 못했습니다: {e}"}
        play = _find(data, play_id)
        if not play:
            return {"ok": False, "error": "그 할 일을 못 찾았습니다. [새로고침] 뒤 다시 눌러 주세요."}
        if play.get("status") == "applied" and not body.get("again"):
            return {"ok": False, "error": "이 할 일은 이미 PR 을 만들었습니다."}
        md_text = (play.get("markdown") or "").strip()
        if not md_text:
            return {"ok": False, "error": "이 할 일에는 수정안 문서가 없습니다 — 할 일을 다시 "
                                          "만든 뒤 눌러 주세요."}

        box = _box(project)
        md = box / f"play-{play_id}.md"
        log = box / f"play-{play_id}.log"
        md.write_text(instructions(play, project) + md_text + "\n", "utf-8")
        cmd = argv(r["exe"], play_id)
        opp_ids = []
        for x in play.get("opp_ids") or []:
            try:
                opp_ids.append(int(x))
            except (TypeError, ValueError):
                pass
        job = {"id": play_id, "project": project, "status": "running",
               "started_at": _now(), "started_ts": time.time(), "ended_at": None,
               "branch": BRANCH.format(id=play_id), "page": play.get("page") or "",
               "opp_ids": opp_ids, "pr_url": None, "error": "",
               "file": str(md), "log": str(log), "cwd": r["cwd"], "argv": cmd}
        if body.get("dry_run"):
            return {"ok": True, "dry_run": True, "job": _public(job)}
        try:
            proc = _spawn(cmd, r["cwd"], md, log)
        except OSError as e:
            return {"ok": False, "error": f"실행하지 못했습니다: {e}"}
        job["pid"] = getattr(proc, "pid", None)
        _write(job)
        _live.add((project, play_id))

    def watch():
        try:
            code = proc.wait(timeout=TIMEOUT)
            timed_out = False
        except subprocess.TimeoutExpired:
            timed_out = True
            code = None
            try:
                proc.kill()
            except Exception:
                pass
        try:
            _finish(project, play_id, code, timed_out, record=record, mark=mark)
        finally:
            _live.discard((project, play_id))

    _start(watch)
    return {"ok": True, "job": _public(job)}


def _log_text(job: dict) -> str:
    try:
        return Path(job["log"]).read_text("utf-8", errors="replace")
    except (OSError, KeyError, TypeError):
        return ""


def _finish(project: str, play_id: int, code, timed_out: bool, *, record, mark) -> dict:
    """끝난 일 하나를 마무리한다 — 한 번만(running 일 때만) 쓴다.

    성공이면 묶인 기회 전부에 작업 기록을 남기고(PR 주소는 note, 브랜치는 branch —
    머지 확인이 그 브랜치로 PR 을 찾는다) play 를 applied 로 둔다. 기록 하나가
    실패해도 PR 은 이미 열렸다 — 성공은 그대로 두고 record_failed 만 적는다.
    """
    with _lock:
        job = _read(project, play_id)
        if not job or job.get("status") != "running":
            return job or {}
        url, why = parse_result(_log_text(job), code)
        if timed_out and not url:
            why = f"{TIMEOUT // 60}분 안에 끝나지 않아 멈췄습니다."
        job["ended_at"] = _now()
        if not url:
            job.update(status="failed", error=why)
            _write(job)
            return job
        job.update(status="done", pr_url=url, error="")
        fails = 0
        for oid in (job.get("opp_ids") or [None]):
            try:
                record(project, {"project": project, "opportunity_id": oid,
                                 "path": job.get("page") or url,
                                 "branch": job.get("branch"), "note": url})
            except Exception:
                fails += 1
        if fails:
            job["record_failed"] = fails
        try:
            mark(project, play_id)
        except Exception:
            job["mark_failed"] = True
        _write(job)
        return job


def _recover(job: dict, *, record, mark) -> dict:
    """도는 중이라고 적혀 있는데 이 프로세스가 안 지켜보는 일 — 대시보드가 그 사이에
    꺼졌다 켜졌다. 자식 프로세스는 살아 있을 수 있으니 서두르지 않는다: 로그에 PR
    주소가 벌써 있으면 성공으로, 시간이 다 지났으면 로그로 판정한다."""
    key = (job.get("project"), int(job.get("id") or 0))
    if job.get("status") != "running" or key in _live:
        return job
    url, _ = parse_result(_log_text(job), None)
    over = time.time() - float(job.get("started_ts") or 0) > TIMEOUT
    if url or over:
        return _finish(job["project"], int(job["id"]), None, over and not url,
                       record=record, mark=mark) or job
    return job


def _public(job: dict | None) -> dict | None:
    """화면에 주는 꼴 — 명령줄 전문은 뺀다(화면이 안 쓴다)."""
    if not job:
        return None
    return {k: v for k, v in job.items() if k not in ("argv", "started_ts")}


def status(project: str, play_id: int | None, *, record, mark) -> dict:
    """GET /api/plays/apply 본체. id 가 있으면 그 일 하나, 없으면 사이트의 일 전부 +
    준비 상태(화면이 버튼을 그릴지 정한다)."""
    project = str(project or "").strip()
    if not project:
        return {"ok": False, "error": "사이트를 못 받았습니다."}
    r = ready(project)
    out = {"ok": True, "ready": r["ok"], "why": r["why"], "fix": r["fix"]}
    if play_id:
        job = _read(project, int(play_id))
        out["job"] = _public(_recover(job, record=record, mark=mark) if job else None)
        return out
    jobs = {}
    for f in sorted(_box(project).glob("play-*.json")):
        try:
            job = json.loads(f.read_text("utf-8"))
        except (OSError, ValueError):
            continue
        job = _recover(job, record=record, mark=mark)
        jobs[str(job.get("id"))] = _public(job)
    out["jobs"] = jobs
    return out


# ── 자체 점검 ───────────────────────────────────────────────────────────────

def _selfcheck() -> None:
    """진짜 claude·gh·git 을 부르지 않는다 — _spawn·_start·_which 를 갈아 끼운다."""
    import os
    import tempfile
    global _spawn, _start, _which
    home = Path(tempfile.mkdtemp(prefix="play-pr-"))
    saved_env = os.environ.get("CAPTURE_HOME")
    os.environ["CAPTURE_HOME"] = str(home)
    repo = home / "repo"
    (repo / ".git").mkdir(parents=True)
    paths.dirs_file().parent.mkdir(parents=True, exist_ok=True)
    paths.dirs_file().write_text(json.dumps({"s": str(repo)}), "utf-8")
    real = (_spawn, _start, _which)
    try:
        # 1) 명령줄 — 헤드리스·권한·이 브랜치로만 push
        a = argv("/bin/claude", 7)
        assert a[:2] == ["/bin/claude", "-p"], a
        assert a[a.index("--permission-mode") + 1] == "acceptEdits", a
        after = a[a.index("--allowedTools") + 1:]
        assert after and not any(x.startswith("-") for x in after), \
            "--allowedTools 는 값을 여럿 먹는다 — 맨 뒤에 있어야 한다(뒤 플래그를 도구로 먹는다)"
        assert "Bash(git push -u origin seo/play-7:*)" in a, a
        assert not any(x.startswith("Bash(git push") and "seo/play-7" not in x for x in a), \
            "다른 브랜치로 push 할 수 있는 허용이 있다"
        assert "Bash(gh pr create:*)" in a
        assert "--dangerously-skip-permissions" not in a, "헤드리스는 허용 목록으로만 돈다"

        # 2) PR 주소 읽기 — 마지막 링크, 실패 줄, 빈 로그
        log = ("참고: https://github.com/x/y/pull/1 을 봤습니다\n작업 끝\n"
               "https://github.com/me/site/pull/42\n")
        assert parse_result(log, 0) == ("https://github.com/me/site/pull/42", "")
        assert parse_result("…\n실패: 커밋 안 된 변경이 있습니다\n", 1) == \
            (None, "커밋 안 된 변경이 있습니다")
        assert parse_result("", 3)[0] is None and "3" in parse_result("", 3)[1]
        assert parse_result("PR: https://ghe.corp/a/b/pull/9.", 0)[0] == "https://ghe.corp/a/b/pull/9"

        # 3) 준비 판정 — 도구·gh·폴더
        _which = lambda exe: None                                   # noqa: E731
        assert not ready("s")["ok"] and ready("s")["fix"] == ""
        _which = lambda exe: None if exe == "gh" else f"/bin/{exe}"   # noqa: E731
        assert "gh" in ready("s")["why"]
        _which = lambda exe: f"/bin/{exe}"                          # noqa: E731
        assert ready("없는사이트")["fix"] == "settings"
        assert ready("s")["ok"] and ready("s")["cwd"] == str(repo)

        # 4) 한 번의 일 — 가짜 프로세스가 로그를 쓰고 끝난다
        plays = {"plays": [{"id": 5, "page": "https://s.kr/bags", "keywords": ["가방"],
                            "opp_ids": [71, 72], "status": "new",
                            "markdown": "# 가방 페이지\n- 제목을 바꾼다"}]}
        recs, marks, spawned = [], [], []

        class Fake:
            pid = 4242

            def __init__(self, out, code=0, hang=False):
                self.out, self.code, self.hang = out, code, hang

            def wait(self, timeout=None):
                if self.hang:
                    raise subprocess.TimeoutExpired("claude", timeout)
                return self.code

            def kill(self):
                self.killed = True

        def fake_spawn(out, code=0, hang=False):
            def sp(cmd, cwd, stdin_file, log_file):
                spawned.append((cmd, cwd, Path(stdin_file).read_text("utf-8")))
                Path(log_file).write_text(out, "utf-8")
                return Fake(out, code, hang)
            return sp

        pending: list = []
        _start = pending.append                     # 지켜보기를 손으로 돌린다 — 도는 중을 본다
        _spawn = fake_spawn("브랜치를 만들었습니다\nhttps://github.com/me/site/pull/42\n")
        kw = {"load": lambda p: plays, "record": lambda p, b: recs.append(b),
              "mark": lambda p, i: marks.append(i)}
        skw = {k: kw[k] for k in ("record", "mark")}
        r = apply({"project": "s", "id": 5}, **kw)
        assert r["ok"] and r["job"]["status"] == "running", r
        cmd, cwd, prompt = spawned[0]
        assert cwd == str(repo) and cmd[0] == "/bin/claude"
        assert prompt.startswith("# 적용 작업") and "# 가방 페이지" in prompt, "요청문에 문서가 없다"
        assert "seo/play-5" in prompt and "gh pr create" in prompt

        # 5) 같은 일은 한 번에 하나
        dup = apply({"project": "s", "id": 5}, **kw)
        assert not dup["ok"] and "만드는 중" in dup["error"], dup
        assert len(spawned) == 1, "도는 중인데 또 띄웠다"
        st = status("s", 5, **skw)
        assert st["job"]["status"] == "running" and st["ready"]

        # 6) 끝나면 — PR 주소, 묶인 기회 전부 기록, applied
        pending.pop()()
        st = status("s", 5, **skw)
        assert st["job"]["status"] == "done", st
        assert st["job"]["pr_url"] == "https://github.com/me/site/pull/42"
        assert [b["opportunity_id"] for b in recs] == [71, 72], recs
        assert all(b["branch"] == "seo/play-5" and b["note"].endswith("/pull/42")
                   and b["path"] == "https://s.kr/bags" for b in recs), recs
        assert marks == [5]
        assert "5" in status("s", None, **skw)["jobs"]
        again = apply({"project": "s", "id": 5}, **kw)
        assert not again["ok"] and "이미" in again["error"], "끝난 일을 또 띄운다"

        # 7) 실패 — 주소 없이 끝나면 이유를 말하고 아무것도 기록하지 않는다
        plays["plays"].append({"id": 6, "page": "/p", "opp_ids": [80], "markdown": "# x"})
        recs.clear()
        marks.clear()
        _spawn = fake_spawn("살펴봤습니다\n실패: 이 페이지를 만드는 파일을 못 찾았습니다\n", code=1)
        assert apply({"project": "s", "id": 6}, **kw)["ok"]
        pending.pop()()
        j = status("s", 6, **skw)["job"]
        assert j["status"] == "failed" and "파일을 못 찾았습니다" in j["error"], j
        assert not recs and not marks, "실패했는데 기록을 남겼다"

        # 8) 시간 초과 — 죽이고 실패로 둔다
        plays["plays"].append({"id": 8, "page": "/q", "opp_ids": [], "markdown": "# y"})
        _spawn = fake_spawn("도는 중…\n", hang=True)
        assert apply({"project": "s", "id": 8}, **kw)["ok"]
        pending.pop()()
        j = status("s", 8, **skw)["job"]
        assert j["status"] == "failed" and "분 안에" in j["error"], j

        # 9) 대시보드가 도중에 꺼졌다 켜진 일 — 로그에 주소가 있으면 조회가 마무리한다
        plays["plays"].append({"id": 9, "page": "/r", "opp_ids": [90], "markdown": "# z"})
        _spawn = fake_spawn("https://github.com/me/site/pull/50\n")
        assert apply({"project": "s", "id": 9}, **kw)["ok"]
        pending.clear()                              # 지켜보던 스레드가 죽었다
        _live.clear()
        j = status("s", 9, **skw)["job"]
        assert j["status"] == "done" and j["pr_url"].endswith("/pull/50"), j
        assert recs[-1]["opportunity_id"] == 90

        # 10) 문서 없는 할 일·모르는 번호·준비 안 됨은 띄우지 않는다
        n = len(spawned)
        plays["plays"].append({"id": 10, "page": "/e", "opp_ids": [], "markdown": ""})
        assert not apply({"project": "s", "id": 10}, **kw)["ok"]
        assert not apply({"project": "s", "id": 999}, **kw)["ok"]
        _which = lambda exe: None                                   # noqa: E731
        assert not apply({"project": "s", "id": 10}, **kw)["ok"]
        assert len(spawned) == n, "실패해야 할 요청이 프로세스를 띄웠다"
    finally:
        _spawn, _start, _which = real
        if saved_env is None:
            os.environ.pop("CAPTURE_HOME", None)
        else:
            os.environ["CAPTURE_HOME"] = saved_env
    print("play_pr self-check ok — 명령줄·주소 읽기·준비·시작·중복·완료 기록·실패·시간 초과·복구")


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        _selfcheck()
    else:
        print(__doc__)
