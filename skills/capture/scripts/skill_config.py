"""스킬 레벨 설정 — 예전 `config.yaml` 이 여기로 옮겨왔다.

파일이었을 때는 호스팅(Railway 컨테이너)과 로컬(플러그인 설치 캐시)이 각자 한 벌씩
들고 있다가 서로 어긋났다. 코드면 같은 버전을 싣는 한 같은 값을 본다 — 바뀌는 길이
push·배포·플러그인 갱신 하나뿐이다. 읽는 쪽은 그대로 `collector.config()` 를 부른다.

사이트별 설정은 여기 없다 — Brain 의 project_settings 가 정본이다(db.project_cfg).
설정 우선순위: CLI > 사이트 설정 > 여기 defaults > 코드 리터럴(collector.settings).

self-check:  python skill_config.py
"""

CONFIG: dict = {
    # 표면은 설계상 상수다 — 하나 더하려면 여기 한 줄 + 어댑터.
    "surfaces_available": ["gsc", "chatgpt", "perplexity", "gemini", "claude",
                           "google_serp", "naver"],
    "default_ai_engines": ["chatgpt", "perplexity", "gemini"],

    # AI 크롤러 UA — robots.txt 로 막혔는지 본다 (scoring.ai_bot_status).
    # 벤더가 봇을 새로 내면 이 목록에 한 줄 더한다.
    #
    # purpose 가 판정의 전부다 — 같은 벤더라도 봇마다 하는 일이 다르다:
    #   search   검색·인용 색인 — 막으면 그 엔진 답변의 출처로 실리기 어렵다 → 기회
    #   user     사용자가 시켜서 그 자리에서 페치 — 막으면 그 엔진이 우리 페이지를 못 연다 → 기회
    #   training 모델 학습 수집 — 막아도 인용과 무관하다. 학습만 막는 것은 오히려
    #            권장되는 중간 지점이라 기회로 올리지 않는다(근거 표에만 적는다)
    # purpose 를 빼거나 옛 꼴(UA 문자열만)로 적으면 "모름" 이다 — 모르는 것을 인용
    # 차단이라고 부르지 않는다.
    # engine 은 사람이 읽는 이름이다(요청문이 "어느 엔진에서" 를 말할 때 쓴다).
    # 기준: 각 벤더의 크롤러 문서. 확실하지 않은 줄은 끝에 '확인 필요' 를 적어 둔다.
    "ai_bots": [
        {"ua": "GPTBot", "vendor": "OpenAI", "purpose": "training", "engine": "OpenAI 모델 학습"},
        {"ua": "OAI-SearchBot", "vendor": "OpenAI", "purpose": "search", "engine": "ChatGPT 검색"},
        # user 봇 둘(ChatGPT-User·Perplexity-User)은 벤더 문서가 "사용자가 시킨 페치라
        # robots.txt 가 적용되지 않을 수 있다" 고 적는다 — 막아도 실제로 안 막힐 수 있다.
        # 확인 필요. 그래도 기회로 두는 이유: 막는 줄이 있다는 것은 사이트 주인이 모르고
        # 있을 일이다.
        {"ua": "ChatGPT-User", "vendor": "OpenAI", "purpose": "user", "engine": "ChatGPT"},
        {"ua": "ClaudeBot", "vendor": "Anthropic", "purpose": "training", "engine": "Claude 모델 학습"},
        {"ua": "Claude-SearchBot", "vendor": "Anthropic", "purpose": "search", "engine": "Claude 검색"},
        {"ua": "Claude-User", "vendor": "Anthropic", "purpose": "user", "engine": "Claude"},
        {"ua": "anthropic-ai", "vendor": "Anthropic", "purpose": "training",
         "engine": "Claude 모델 학습 (옛 UA)"},
        {"ua": "PerplexityBot", "vendor": "Perplexity", "purpose": "search", "engine": "Perplexity"},
        {"ua": "Perplexity-User", "vendor": "Perplexity", "purpose": "user", "engine": "Perplexity"},
        # 제미나이 학습용 제어 토큰이다 — 구글 검색·AI 요약 노출은 Googlebot 이 정한다
        # (색인 막힘 쪽)
        {"ua": "Google-Extended", "vendor": "Google", "purpose": "training",
         "engine": "Gemini 모델 학습"},
        # 빙 검색 그 자체이자 Copilot 이 인용하는 색인 — 막혔으면 AI 만의 문제가 아니다
        {"ua": "Bingbot", "vendor": "Microsoft", "purpose": "search", "engine": "Bing 검색·Copilot"},
        {"ua": "Applebot-Extended", "vendor": "Apple", "purpose": "training",
         "engine": "Apple 모델 학습"},
        # 확인 필요 — 벤더 공식 문서 없음
        {"ua": "Bytespider", "vendor": "ByteDance", "purpose": "training",
         "engine": "ByteDance 모델 학습"},
        {"ua": "CCBot", "vendor": "Common Crawl", "purpose": "training",
         "engine": "Common Crawl (여러 LLM 의 학습 소스)"},
    ],

    # 제3자 플랫폼 — 챗봇이 내 사이트 대신 인용한 곳이 대부분 여기면, 처방이 "내 페이지
    # 고치기"가 아니라 "그 플랫폼에 진짜로 등장하기"(요청문 꼴 presence)로 바뀐다
    # (scoring.third_party_platforms · ai_tally). 하위 도메인은 자동으로 포함된다
    # (ko.wikipedia.org·m.blog.naver.com). 플랫폼은 한 줄로 더한다.
    # 경쟁사 자동 적재(scoring.serp_rivals·collect_gap)와 경쟁사 읽기(scoring.rivals)도
    # 이 목록으로 플랫폼을 뺀다 — 검색결과에 늘 서는 곳이라 "경쟁사"로 잡히기 쉽다.
    "third_party_platforms": [
        "reddit.com", "wikipedia.org", "namu.wiki", "youtube.com", "youtu.be", "quora.com",
        "linkedin.com", "medium.com", "g2.com", "capterra.com", "trustradius.com",
        "trustpilot.com", "producthunt.com", "stackoverflow.com", "github.com", "x.com",
        "twitter.com", "facebook.com", "instagram.com", "tiktok.com", "threads.com",
        "threads.net", "pinterest.com",
        "naver.com",        # 포털 전체 — 아래 네이버 하위 줄은 인용 집계가 더 구체적인
        "daum.net",         #   이름(blog.naver.com)으로 접으려고 남긴다(_rival_key)
        "kakao.com",        # pf.kakao.com(카카오톡 채널) 등
        "blog.naver.com", "cafe.naver.com", "kin.naver.com", "post.naver.com",
        "tistory.com", "brunch.co.kr", "velog.io", "dcinside.com", "clien.net",
        "play.google.com",  # 앱 스토어 등록 페이지 — 앱은 거기서 등장한다. 백링크
        "apps.apple.com",   #   교집합(collect_backlinks)도 이 목록으로 플랫폼을 뺀다
    ],

    # AI 답변의 링크를 타고 들어온 방문 — GA4 sessionSource 로 가른다(collect_ga4).
    # 정규식이 아니라 호스트 목록이다: 이 호스트이거나 그 하위 도메인(www.perplexity.ai)
    # 이면 그 호스트로 센다. 구글 AI 요약(AI Overviews)은 여기 없다: 그 클릭은 GA4 에서
    # google / organic 으로 잡혀 유기 검색과 안 갈린다.
    "ai_referrers": [
        "chatgpt.com",
        "chat.openai.com",  # ChatGPT 옛 주소 — 아직 이 출처로 들어오는 세션이 있다
        "perplexity.ai", "gemini.google.com", "copilot.microsoft.com", "claude.ai",
        "you.com", "poe.com", "chat.deepseek.com", "chat.mistral.ai",
    ],

    # OpenRouter 모델 슬러그는 시간이 지나면 바뀐다 — 바꿀 때는 여기 한 곳.
    # ':online' 은 OpenRouter 웹 플러그인 → 지원되는 곳은 제공자 자체 검색.
    "ai_engines": {
        "chatgpt": "openai/gpt-4o-mini:online",
        "perplexity": "perplexity/sonar",
        "gemini": "google/gemini-2.5-flash:online",
        "claude": "anthropic/claude-sonnet-4.5:online",   # 선택 4번째 엔진, 같은 키
    },

    # collector.settings() 가 읽는다: CLI > 사이트 설정 > 여기 > 코드 리터럴.
    "defaults": {
        "gsc_days": 28,
        "ai_samples": 2,        # 비결정성 완화용 2회 샘플 — API 호출·비용 2배. 급하면 1로.
        "throttle": 0.5,        # 요청 간격(초) — 자동완성·AI·SERP 공통
        "serp_depth": 10,
        "serp_device": "desktop",
        # 콤마 구분 문자열이다 — 리스트가 아니다. collector.add_setting 의 type_fn 이
        # 스칼라만 다루기 때문. ""(빈 문자열)이면 분해 수집을 아예 하지 않는다.
        "gsc_breakdown": "device,country",   # device|country — 둘 다 [분석] 화면이 쓴다
        # /capture pages 가 한 번에 감사할 내 URL 수 (0이면 끔). 무료 HTTP 요청이라 기회에 걸린
        # 페이지 수(수십 곳)를 한 회차에 덮을 만큼 둔다 — 20 이면 고칠 페이지 절반이 빠졌다.
        "page_urls": 40,
        "index_urls": 20,       # /capture index 가 한 번에 검사할 URL 수 (0이면 끔)
        # /capture vitals — 한 URL 이 기기 수만큼 호출된다(5 × 2 = 10회). PageSpeed
        # Insights 는 무료지만 한 번에 20초쯤 걸려서 page_urls 보다 작게 잡는다.
        "vitals_urls": 5,       # 속도를 잴 URL 수 (0이면 끔)
        "strategy": "mobile,desktop",   # 속도를 잴 기기 — 기기 격차를 보려면 둘 다 필요하다
    },

    "serp": {
        "provider": "auto",     # auto(키 감지) | dataforseo | serper
        # DataForSEO 는 Live 엔드포인트가 **계정당 분당 12회**다. 이 리포가 부르는
        # DataForSEO 경로는 SERP·Labs·볼륨·백링크까지 전부 /live 라 전부 여기 걸린다.
        # 간격은 serp_adapter 가 자동으로 지킨다(호출당 5초) — defaults.throttle 로는
        # 못 막는다. 계정 한도를 올렸으면 SEOMINER_DFS_RPM 로 알려 준다(0 = 간격 없음).
        # dataforseo live advanced ~$0.002-0.003/query (실청구액은 응답에서 기록됨)
        # serper: 크레딧 과금, AI오버뷰 데이터 없음(aio 필드 null)
    },
}


def _selfcheck() -> None:
    assert {"ai_bots", "third_party_platforms", "ai_referrers", "ai_engines",
            "defaults", "serp"} <= set(CONFIG)
    for b in CONFIG["ai_bots"]:
        assert b["purpose"] in ("search", "user", "training"), b
    assert "chatgpt.com" in CONFIG["ai_referrers"]
    assert CONFIG["defaults"]["serp_depth"] == 10


if __name__ == "__main__":
    _selfcheck()
    print("skill_config ok")
