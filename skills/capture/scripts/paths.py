#!/usr/bin/env python3
"""자료가 어디 사는가 — 경로와 구글 자격증명만 답한다. SQL 은 여기 없다.

db.py 한 파일에 module 이 둘 들어 있었다: 앞쪽 절반은 파일시스템 경로와 OAuth
상태를 해석하고, 뒤쪽 절반이 Brain(SQLite)이다. 둘을 의미 있게 함께 쓰는 호출자는
collect_gsc 하나뿐인데, 토큰 파일 위치만 알고 싶은 doctor 도 `import db` 로
SQLite·SCHEMA·_migrate 를 통째로 끌고 들어와야 했다. 그 seam 을 여기서 가른다.

**db.py 는 예전 이름들을 그대로 계속 노출한다** — 호출자 19개는 아무것도 안 고친다.

  CAPTURE_HOME (default ~/.capture)
    ├── brain.db
    ├── projects/*.yaml
    ├── docs/{사이트}/…            (docs_dir)
    ├── creds/{사이트}/…           (creds_dir — 레거시)
    ├── env
    └── gsc_token.json / gsc_oauth_client.json / gsc_service_account.json

불변식: **값은 부를 때마다 env 를 다시 읽는다.** import 시점에 얼리면
server/store.py 의 tenant() 가 CAPTURE_HOME·GSC_TOKEN_FILE 을 갈아끼우는
멀티테넌시가 조용히 깨진다.
"""
import os
import re
import sys
from pathlib import Path
from typing import NamedTuple


def home() -> Path:
    """상태 디렉토리. 매번 환경변수를 다시 읽는다 — import 시점에 얼리지 않는다."""
    return Path(os.environ.get("CAPTURE_HOME", Path.home() / ".capture"))


def db_file(home_dir: Path | None = None) -> Path:
    """Brain 파일 자리. home_dir 을 주면 env 를 안 보고 그 경로를 쓴다 —
    server/store.py 의 Tenant.brain() 이 이걸로 특정 유저의 brain 을 연다."""
    if home_dir is not None:
        return Path(home_dir) / "brain.db"
    env = os.environ.get("CAPTURE_DB")
    return Path(env) if env else home() / "brain.db"


# 한국어 Windows 콘솔(cp949)에서 '—'·'✓' 출력이 UnicodeEncodeError로 죽는 것 방지.
# db가 paths를 import하고 모든 스크립트가 db를 import하므로 여기 한 번이면 전부
# 커버된다 — doctor·connect_gsc·createdb 도 각자 갖고 있던 사본을 지우고 이것에 기댄다.
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):  # 파이프로 감싼 경우 등
        pass


def load_env(path: Path | None = None) -> None:
    """~/.capture/env 의 KEY=VALUE를 환경변수로 — 대시보드 설정 화면이 여기 쓴다.
    셸에 이미 export한 값이 우선(setdefault). # ponytail: dotenv 패키지 대신 4줄."""
    path = path or home() / "env"
    for line in (path.read_text("utf-8").splitlines() if path.exists() else []):
        k, sep, v = line.partition("=")
        if sep and k.strip() and not k.lstrip().startswith("#"):
            os.environ.setdefault(k.strip(), v.strip())


load_env()


# ── 구글 자격증명 ──────────────────────────────────────────────────
# 예전에는 여기 함수가 8개였고(gsc_key·gsc_oauth_bundled·gsc_oauth_client·gsc_auth·
# gsc_token·gsc_token_legacy·gsc_token_file·gsc_connected) 서로가 서로를 불러
# 호출자마다 "무엇부터 물어봐야 하나"가 달랐다. 이제 한 번 물으면 한 벌이 나온다.

class GSC(NamedTuple):
    """지금 env 기준으로 해석된 구글 자격증명 한 벌.

    auth        "oauth" | "service_account" | "" (아직 없음)
    connected   **진짜 연결됐나** — 파일이 있느냐가 아니라 토큰이 있느냐다
    key         서비스 계정 키 (전 사이트 공용, 무인 수집용)
    client      실제로 쓸 OAuth 클라이언트 (env > 사용자 것 > 번들)
    bundled     플러그인이 동봉한 OAuth 클라이언트 자리
    token       로그인 토큰을 **쓸** 자리
    token_legacy  예전 mcp-gsc 서버가 쓰던 토큰 자리
    token_file  실제로 존재하는 토큰 (없으면 None)
    """
    auth: str
    connected: bool
    key: Path
    client: Path
    bundled: Path
    token: Path
    token_legacy: Path
    token_file: Path | None


def bundled_oauth_client() -> Path:
    """플러그인이 동봉한 OAuth 클라이언트 — 콘솔 작업을 사용자에게서 걷어낸다.

    구글 OAuth 는 등록된 client_id/secret 없이는 성립하지 않는다. 우회로가 없어서,
    지금까지는 **사용자마다** 클라우드 콘솔에서 앱을 등록하게 했다(API 사용 설정 →
    동의 화면 → 데스크톱 앱 클라이언트 → JSON 다운로드). 그 4단계를 없애는 방법은
    "콘솔 없는 OAuth"가 아니라 **콘솔 작업을 한 번 해서 그 결과를 배포에 넣는 것**이다.

    설치형 앱의 client_secret 은 기밀이 아니다 — 구글이 그렇게 명시하고,
    rclone·gcloud 같은 CLI 가 전부 자기 클라이언트를 박아서 배포한다. 대신
    미검증 앱이라 동의 화면에 경고가 한 번 뜨고 사용자 100명 상한이 붙는다.

    이 파일이 없어도 아무것도 깨지지 않는다 — 그냥 예전처럼 각자 클라이언트를
    쓰는 흐름으로 돌아갈 뿐이다(배포에서 뺐거나, 로컬 개발 중이거나).
    """
    return Path(__file__).resolve().parents[2] / "setup" / "oauth_client.json"


def _legacy_token() -> Path:
    """예전 MCP 서버가 쓰던 토큰 자리 — 이미 로그인한 사람을 다시 로그인시키지 않는다.

    업스트림 gsc_server.py 규칙이었다: GSC_CONFIG_DIR 또는 platformdirs 의 mcp-gsc
    아래 token.json. 파일 형식은 google-auth 표준(Credentials.to_json)이라 그대로
    읽힌다 — collect_gsc 가 이걸 읽어 쓰고 새 자리에 다시 쓴다.
    윈도우 경로가 mcp-gsc 를 두 번 지나는 건 오타가 아니라 platformdirs 실측값이다.
    """
    d = os.environ.get("GSC_CONFIG_DIR")
    if not d:
        if sys.platform == "win32":
            base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local")
        elif sys.platform == "darwin":
            base = Path.home() / "Library" / "Application Support"
        else:
            base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
        d = base / "mcp-gsc" / "mcp-gsc" if sys.platform == "win32" else base / "mcp-gsc"
    return Path(d) / "token.json"


def gsc(bundled: Path | None = None) -> GSC:
    """지금 걸려 있는 구글 자격증명을 통째로 해석한다. 매번 다시 읽는다.

    **이 우선순위가 정본이다.** collect_gsc·gsc_query·doctor·connect_gsc 가 전부
    이 판정을 따른다 — 예전에 같은 규칙이 여러 방언으로 흩어져 한쪽만 고쳐지는
    일이 반복됐다. OAuth 가 기본이고, 서비스 계정은 무인 수집이 필요할 때 쓴다.

    client 우선순위: 환경변수 > 사용자가 깐 것(~/.capture) > 번들. **사용자 것이
    번들을 이긴다** — 이미 자기 클라이언트로 붙여 둔 사람의 발밑을 업데이트가
    바꿔치기하면 안 된다. 셋 다 없으면 사용자 자리를 돌려준다: "무엇이 없어서
    막혔나"를 말할 때 가리킬 자리는 배포 안쪽이 아니라 사용자가 놓을 자리다.

    auth 에서 **번들 OAuth 는 서비스 계정보다 뒤다.** 번들은 설치만 하면 항상
    존재하므로, 단순히 "OAuth 파일이 있나"로 판정하면 서비스 계정으로 무인 수집을
    걸어 둔 사람이 매번 브라우저 로그인으로 끌려간다.

    connected 도 같은 이유로 **토큰**이 정본이다 — 파일 유무로 판정하면 설치 직후
    전원이 '연결됨'으로 보인다. 실제로 한 번 당해 본 거짓말이다(예전 gsc MCP 서버는
    인증 파일이 없어도 `Connected` 로 떠서 사용자가 한 번도 인증되지 않은 채 지냈다).
    서비스 계정은 로그인 자체가 없어서 키 파일 존재가 곧 연결이다.

    bundled: 번들 자리를 바꿔 끼울 자리 — 배포에서 번들을 뺀 상태를 테스트가 흉내낸다.
    """
    h = home()
    bundled = bundled_oauth_client() if bundled is None else Path(bundled)
    key = Path(os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", h / "gsc_service_account.json"))
    env_client = os.environ.get("GSC_OAUTH_CLIENT_SECRETS_FILE")
    own = h / "gsc_oauth_client.json"

    if env_client:
        client = Path(env_client)
    elif own.exists():
        client = own
    else:
        client = bundled if bundled.exists() else own

    if (env_client and Path(env_client).exists()) or own.exists():
        auth = "oauth"
    elif key.exists():
        auth = "service_account"
    elif bundled.exists():
        auth = "oauth"
    else:
        auth = ""

    token = Path(os.environ.get("GSC_TOKEN_FILE", h / "gsc_token.json"))
    legacy = _legacy_token()
    token_file = next((t for t in (token, legacy) if t.exists()), None)

    if auth == "service_account":
        connected = key.exists()
    else:
        connected = auth == "oauth" and token_file is not None

    return GSC(auth, connected, key, client, bundled, token, legacy, token_file)


def creds_dir(project: str) -> Path:
    """레거시 사이트별 OAuth 자격증명 폴더 — 서비스 계정 키로 대체됐다."""
    return home() / "creds" / project


def docs_dir(project: str) -> Path:
    """사이트별 마케팅 문서 자리 — positioning.md, aso.md 등.

    setup 이 외부 스킬(product-marketing·aso)로 만들어 여기 두면, capture 는 AI
    프롬프트를 쓸 때, create 는 콘텐츠 보이스를 잡을 때 같은 문서를 읽는다.
    Brain(brain.db) 밖에 산문으로 두는 이유는 사람이 직접 고쳐야 하는 것이기 때문이다.
    """
    return home() / "docs" / project


def downloads_dir() -> Path:
    """브라우저 다운로드 폴더 — GSC 서비스 계정 키(connect_gsc)를 여기서 찾는다."""
    return Path(os.environ.get("DOWNLOADS_DIR", Path.home() / "Downloads")).expanduser()


_REPO_PATH_RE = re.compile(r"^repo_path:[ 	]*(.+?)[ 	]*$", re.M)


def repo_project(cwd=None) -> str | None:
    """지금 이 폴더가 어느 사이트의 리포인가 — `projects/{P}.repo.yaml` 의 repo_path 로 판정.

    Brain 은 컴퓨터 전역(`~/.capture/brain.db`)이라 사이트가 여럿이면 "지금 이 폴더가
    어느 사이트냐"에 아무도 답하지 못했다. 그 답은 `/create profile` 이 이미
    `repo.yaml` 에 적어 두고 있었는데 읽는 코드가 없었다.

    ponytail: yaml 파서 대신 한 줄 정규식으로 읽는다 — doctor 가 pip 이전에도 돌아야 해서
    stdlib 밖으로 못 나간다. repo_path 를 블록 스칼라(`|`)로 적으면 못 읽는다.
    """
    try:
        here = Path(cwd or Path.cwd()).resolve()
    except OSError:
        return None
    d = home() / "projects"
    if not d.exists():
        return None
    best = None
    for f in sorted(d.glob("*.repo.yaml")):
        try:
            m = _REPO_PATH_RE.search(f.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        if not m:
            continue
        raw = m.group(1).strip().strip('"').strip("'")
        if not raw or raw.startswith("/path/to/"):   # 템플릿 그대로면 무시
            continue
        try:
            root = Path(raw).expanduser().resolve()
        except OSError:
            continue
        if root == here or root in here.parents:
            # 가장 깊은 매치가 이긴다 — 리포 안에 리포가 있을 때 안쪽이 답이다.
            if best is None or len(root.parts) > len(best[1].parts):
                best = (f.name[: -len(".repo.yaml")], root)
    return best[0] if best else None


def _selfcheck() -> None:
    """env 를 다시 읽는가, 레거시 토큰으로 폴백하는가 — 두 불변식만 본다."""
    import tempfile

    saved = {k: os.environ.get(k) for k in
             ("CAPTURE_HOME", "CAPTURE_DB", "GSC_TOKEN_FILE", "GSC_CONFIG_DIR",
              "GSC_OAUTH_CLIENT_SECRETS_FILE", "GOOGLE_APPLICATION_CREDENTIALS")}
    with tempfile.TemporaryDirectory() as d:
        h1, h2 = Path(d) / "one", Path(d) / "two"
        legacy = Path(d) / "legacy"
        for p in (h1, h2, legacy):
            p.mkdir(parents=True)
        for k in list(saved):
            os.environ.pop(k, None)
        os.environ["GSC_CONFIG_DIR"] = str(legacy)
        missing = Path(d) / "no-bundle.json"     # 번들이 빠진 배포를 흉내낸다

        # ── env 를 다시 읽는가 (store.tenant 가 여기 기댄다)
        os.environ["CAPTURE_HOME"] = str(h1)
        assert home() == h1, home()
        assert db_file() == h1 / "brain.db", db_file()
        assert docs_dir("p") == h1 / "docs" / "p"
        assert creds_dir("p") == h1 / "creds" / "p"
        os.environ["CAPTURE_HOME"] = str(h2)
        assert home() == h2, "CAPTURE_HOME 을 갈아끼웠는데 따라오지 않았다"
        assert db_file() == h2 / "brain.db", "DB 경로가 import 시점에 얼었다"
        assert db_file(h1) == h1 / "brain.db", "home_dir 을 주면 그 경로를 써야 한다"
        assert home() == h2, "home_dir 인자가 전역 CAPTURE_HOME 을 건드리면 안 된다"

        # ── 인증 3-상태
        assert gsc(missing).auth == "", "아무것도 없으면 빈 문자열"
        (h2 / "gsc_service_account.json").write_text("{}", "utf-8")
        assert gsc(missing).auth == "service_account", "키만 있으면 서비스 계정"
        assert gsc(missing).connected is True, "서비스 계정은 키 존재가 곧 연결"
        bundle = Path(d) / "bundled.json"
        bundle.write_text("{}", "utf-8")
        assert gsc(bundle).auth == "service_account", \
            "번들은 사용자가 놓은 서비스 계정 키를 이기면 안 된다"
        (h2 / "gsc_oauth_client.json").write_text("{}", "utf-8")
        g = gsc(bundle)
        assert g.auth == "oauth", "사용자 OAuth 클라이언트가 최우선"
        assert g.client == h2 / "gsc_oauth_client.json", "사용자 것이 번들을 이긴다"
        assert g.connected is False, "클라이언트만 있고 토큰이 없으면 '로그인 대기'다"

        # ── 토큰: 새 자리 우선, 없으면 레거시
        assert gsc(bundle).token_legacy == legacy / "token.json"
        (legacy / "token.json").write_text("{}", "utf-8")
        g = gsc(bundle)
        assert g.token_file == legacy / "token.json", "레거시 토큰 폴백이 안 돈다"
        assert g.connected is True, "레거시 토큰이면 이미 로그인한 것"
        (h2 / "gsc_token.json").write_text("{}", "utf-8")
        assert gsc(bundle).token_file == h2 / "gsc_token.json", "새 자리가 우선"
        os.environ["GSC_TOKEN_FILE"] = str(h1 / "tok.json")
        assert gsc(bundle).token == h1 / "tok.json", "GSC_TOKEN_FILE 을 따라야 한다"

        # ── env 파일 읽기: 셸에 이미 있는 값이 이긴다
        (h2 / "env").write_text("SEO_MINER_SELFCHECK=from-file\n# 주석=무시\n", "utf-8")
        load_env()
        assert os.environ.pop("SEO_MINER_SELFCHECK") == "from-file"

        # ── repo_project: 등록된 리포 안이면 그 사이트, 밖이면 None
        (h2 / "projects").mkdir()
        repo = Path(d) / "repo"
        (repo / "src").mkdir(parents=True)
        (h2 / "projects" / "alpha.repo.yaml").write_text(
            f"repo_path: {repo}\n", "utf-8")
        assert repo_project(repo / "src") == "alpha", "하위 폴더에서도 붙어야 한다"
        assert repo_project(Path(d)) is None, "무관한 폴더는 매치가 없어야 한다"

    for k, v in saved.items():
        os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
    print("paths: OK")


if __name__ == "__main__":
    _selfcheck()
