"""서버 설정 한 곳 — "이 배포에 뭘 넣어야 하나"의 답은 이 파일이다.

읽기 규율은 하나다: **호출 시점에 읽는다.** import 시점에 얼리지 않는다.
  - store.tenant() 가 실행 중에 env 를 갈아끼우고 엔진(db/paths)은 매번 다시 읽는다.
    얼리면 테넌트 격리가 조용히 깨진다.
  - demo()/스크립트가 import 뒤에 env 를 세우고 부른다.
required 는 없으면 Missing 을 던진다 — 조용한 기본값으로 굴러가 다르게 동작하는 쪽이
부팅에서 죽는 것보다 나쁘다. optional 의 기본값은 여기 말고 어디에도 적지 않는다.

테넌트 env(CAPTURE_HOME, GSC_TOKEN_FILE)는 설정이 아니다 — store.tenant() 가 유저마다
세운다. 여기서 읽거나 기본값을 주면 안 된다.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


class Missing(RuntimeError):
    """required 설정이 비어 있다."""


@dataclass(frozen=True)
class Setting:
    name: str
    default: object = None          # str, 또는 호출 시점에 부를 무인자 함수
    required: bool = False
    desc: str = ""
    hint: str = ""                  # 없을 때 사람에게 할 말


# 워커가 tenant 안에서만 엔진에 노출하는 유료 키.
# 서버 env 이름은 SEOMINER_<NAME>, 엔진이 보는 이름은 <NAME> — 이 명명 규칙의 주인은 여기다.
PAID_KEYS = (
    "SERPER_API_KEY",
    "DATAFORSEO_LOGIN",
    "DATAFORSEO_PASSWORD",
    "OPENROUTER_API_KEY",
)
SERVER_PREFIX = "SEOMINER_"

# 설정이 아니다 — paid_keys() 안에서만 서 있는 **런타임 표식**이다. 뜻은 하나:
# "이 런의 준비물은 서버가 댄다"(유료 키·pip 설치·구글 클라이언트 등록이 유저 몫이 아님).
# 엔진 쪽(skills/setup/scripts/doctor.py)이 이 이름을 읽어 owner 판정을 뒤집는다 —
# 플러그인 설치본에는 server/ 가 없어 import 를 못 하므로 이름만 저쪽에 적혀 있고,
# 값의 주인은 여기다. SETTINGS 에 넣지 않는다(사람이 설정할 값이 아니다).
HOSTED_ENV = "SEOMINER_HOSTED"

_KEYGEN = ('python -c "from cryptography.fernet import Fernet;'
           'print(Fernet.generate_key().decode())"')

SETTINGS: dict[str, Setting] = {s.name: s for s in (
    # --- 필수 ---------------------------------------------------------------
    Setting("SEOMINER_SECRET_KEY", required=True,
            desc="구글·GitHub 토큰 암호화 키(Fernet)",
            hint=f"{_KEYGEN} 로 키를 생성해 환경 변수에 넣어 주세요"),
    Setting("GOOGLE_CLIENT_ID", required=True,
            desc="구글 로그인·서치콘솔 OAuth 클라이언트 ID"),
    Setting("GOOGLE_CLIENT_SECRET", required=True,
            desc="구글 로그인·서치콘솔 OAuth 클라이언트 시크릿"),

    # --- 선택 (기본값이 있다) -------------------------------------------------
    Setting("SEOMINER_DATA", default=lambda: str(Path.home() / ".seominer"),
            desc="서버 데이터 루트 — server.db 와 유저별 Brain 이 여기 산다"),
    Setting("OAUTH_REDIRECT_URI", default="http://localhost:8000/auth/callback",
            desc="구글 OAuth 콜백 주소"),
    Setting("SEOMINER_PUBLIC_URL", default="http://localhost:8000",
            desc="메일 링크에 쓰는 서비스 바깥 주소"),
    Setting("SEOMINER_RUN_EVERY_HOURS", default="168",
            desc="재측정 주기(시간) 기본값. 0 이면 자동 수집을 끈다. "
                 "사이트가 설정 화면에서 자기 주기를 정하면 그 값이 이긴다 — 여기는 폴백이다"),
    Setting("SEOMINER_MAX_KEYWORDS", default="100",
            desc="사이트당 추적 키워드 상한 — SERP 는 키워드당 과금이다"),
    Setting("SEOMINER_BACKLINKS_EVERY_DAYS", default="30",
            desc="백링크 재측정 주기(일). 0 이면 끈다"),
    Setting("SEOMINER_MAIL_FROM", default="seo-miner <onboarding@resend.dev>",
            desc="알림 메일 발신자"),
    Setting("SEOMINER_WRITER_MODEL", default="anthropic/claude-sonnet-4.5",
            desc="/create 글쓰기에 쓰는 OpenRouter 모델"),

    # --- 선택 (없으면 그 기능이 꺼진다) ----------------------------------------
    Setting("SESSION_SECRET",
            desc="세션 서명 키. 없으면 프로세스마다 랜덤 — 재시작에 세션이 끊긴다"),
    Setting("RESEND_API_KEY", desc="없으면 알림 메일을 보내지 않는다"),
    Setting("GITHUB_CLIENT_ID", desc="없으면 /create 의 GitHub 연동이 꺼진다"),
    Setting("GITHUB_CLIENT_SECRET", desc="없으면 /create 의 GitHub 연동이 꺼진다"),
    Setting("GITHUB_REDIRECT_URI",
            desc="없으면 OAUTH_REDIRECT_URI 에서 유도한다 (github_redirect())"),
    *(Setting(SERVER_PREFIX + k,
              desc=f"유료 키 — tenant 안에서만 {k} 라는 이름으로 엔진에 노출된다")
      for k in PAID_KEYS),
)}


def get(name: str) -> str | None:
    """설정 하나를 호출 시점에 읽는다. required 인데 비어 있으면 Missing."""
    s = SETTINGS[name]
    v = os.environ.get(name)
    if v:
        return v
    if s.required:
        raise Missing(f"{name} 가 설정되지 않았습니다"
                      + (f". {s.hint}" if s.hint else ""))
    d = s.default
    return d() if callable(d) else d


def num(name: str) -> float:
    """숫자 설정. 값이 망가졌으면 기본값으로 — 오타 하나에 서버가 죽지 않는다."""
    d = SETTINGS[name].default
    try:
        return float(get(name))
    except (TypeError, ValueError):
        return float(d() if callable(d) else d)


def count(name: str) -> int:
    return int(num(name))


def paid_key(name: str) -> str | None:
    """엔진이 보는 이름으로 유료 키를 읽는다 (tenant 안에서만 값이 있다)."""
    assert name in PAID_KEYS, name
    return os.environ.get(name) or None


def paid_env() -> dict[str, str | None]:
    """엔진 이름 -> 서버 env(SEOMINER_<NAME>) 값. None 이면 지워야 한다 —
    외부 env 에 남은 잔여값을 엔진이 그대로 쓰면 남의 돈이 나간다."""
    return {k: os.environ.get(SERVER_PREFIX + k) or None for k in PAID_KEYS}


@contextmanager
def paid_keys():
    """서버 env(SEOMINER_<NAME>) 를 <NAME> 으로 노출, 끝나면 복원. + HOSTED_ENV 표식.

    SEOMINER_<NAME> 가 없으면 그 키를 pop — 외부 env 에 남아있던 잔여값을 엔진이
    그대로 쓰면 비용 누수가 된다(엔진은 키가 보이면 그냥 쓴다).

    worker 에만 있던 시절엔 /api/doctor 가 이걸 두를 수 없었고, 그래서 서버가 키를
    대는 호스팅에서도 doctor 가 "키 없음"으로 판정해 유저에게 키 발급을 시켰다.
    명명 규칙(PAID_KEYS/SERVER_PREFIX)의 주인이 여기니 컨텍스트도 여기 산다.
    """
    saved = {k: os.environ.get(k) for k in PAID_KEYS}
    saved[HOSTED_ENV] = os.environ.get(HOSTED_ENV)
    for k, v in paid_env().items():
        if v:
            os.environ[k] = v
        else:
            os.environ.pop(k, None)
    os.environ[HOSTED_ENV] = "1"
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def github_redirect() -> str:
    return get("GITHUB_REDIRECT_URI") or \
        get("OAUTH_REDIRECT_URI").replace("/auth/callback", "/auth/github/callback")


def missing() -> list[str]:
    """비어 있는 required 설정 이름들. 부팅에서 한 번에 알려 줄 때 쓴다."""
    return [n for n, s in SETTINGS.items() if s.required and not os.environ.get(n)]


def demo() -> None:
    saved = {n: os.environ.get(n) for n in SETTINGS}
    try:
        for n in SETTINGS:
            os.environ.pop(n, None)

        # required: 없으면 raise. 조용한 기본값으로 굴러가지 않는다.
        assert missing() == ["SEOMINER_SECRET_KEY", "GOOGLE_CLIENT_ID",
                             "GOOGLE_CLIENT_SECRET"], missing()
        for n in missing():
            try:
                get(n)
                raise AssertionError(f"{n} 없이 통과했다")
            except Missing as e:
                assert n in str(e), e
        # optional: 기본값은 이 파일에만 있다.
        assert get("SEOMINER_RUN_EVERY_HOURS") == "168"
        assert num("SEOMINER_RUN_EVERY_HOURS") == 168.0
        assert count("SEOMINER_MAX_KEYWORDS") == 100
        assert get("SESSION_SECRET") is None, "기능 스위치는 None 이어야 한다"
        assert get("SEOMINER_DATA") == str(Path.home() / ".seominer")

        # 호출 시점에 읽는다 — import 때 얼리면 tenant 스왑이 안 먹는다.
        os.environ["SEOMINER_MAX_KEYWORDS"] = "5"
        assert count("SEOMINER_MAX_KEYWORDS") == 5, "얼려 읽고 있다"
        os.environ["SEOMINER_MAX_KEYWORDS"] = "다섯"
        assert count("SEOMINER_MAX_KEYWORDS") == 100, "망가진 값에 기본값으로 안 떨어진다"

        os.environ["SEOMINER_SECRET_KEY"] = "k"
        assert get("SEOMINER_SECRET_KEY") == "k" and missing() == [
            "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"]

        # SEOMINER_<NAME> 재매핑 — 이 명명 규칙의 주인은 여기다.
        os.environ["SEOMINER_SERPER_API_KEY"] = "serp"
        env = paid_env()
        assert env["SERPER_API_KEY"] == "serp", env
        assert env["OPENROUTER_API_KEY"] is None, env
        assert set(env) == set(PAID_KEYS), env
        os.environ["OPENROUTER_API_KEY"] = "engine-side"
        assert paid_key("OPENROUTER_API_KEY") == "engine-side"
        assert paid_env()["OPENROUTER_API_KEY"] is None, "서버 env 와 엔진 env 를 섞었다"
        os.environ.pop("OPENROUTER_API_KEY")

        # paid_keys() 는 worker 에서 여기로 옮겨 왔다 — /api/doctor 도 두를 수 있어야
        # doctor 가 "런이 실제로 보는 env"를 보고 판정한다.
        os.environ["SEOMINER_OPENROUTER_API_KEY"] = "srv"
        os.environ.pop("SEOMINER_SERPER_API_KEY", None)   # 서버에 짝이 없는 키
        os.environ["SERPER_API_KEY"] = "잔여값"           # 외부 env 에 남은 잔여값
        with paid_keys():
            assert os.environ["OPENROUTER_API_KEY"] == "srv", "서버 키가 엔진 이름으로 안 뜬다"
            assert "SERPER_API_KEY" not in os.environ, "짝 없는 잔여 키가 엔진에 노출됐다"
            assert os.environ[HOSTED_ENV] == "1", "호스팅 표식이 안 섰다"
        assert "OPENROUTER_API_KEY" not in os.environ, "엔진 이름을 복원하지 않았다"
        assert os.environ.pop("SERPER_API_KEY") == "잔여값", "바깥 env 를 복원하지 않았다"
        assert HOSTED_ENV not in os.environ, "표식을 복원하지 않았다"
        assert HOSTED_ENV not in SETTINGS, "표식은 설정이 아니다"

        assert github_redirect() == "http://localhost:8000/auth/github/callback"
        os.environ["GITHUB_REDIRECT_URI"] = "https://x/cb"
        assert github_redirect() == "https://x/cb"

        print("settings: ok")
    finally:
        for n, v in saved.items():
            os.environ.pop(n, None) if v is None else os.environ.__setitem__(n, v)


if __name__ == "__main__":
    demo()
