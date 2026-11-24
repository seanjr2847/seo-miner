"""스킬 레벨 설정 — 예전 `config.yaml` 이 여기로 옮겨왔다.

파일이었을 때는 호스팅(Railway 컨테이너)과 로컬(플러그인 설치 캐시)이 각자 한 벌씩
들고 있다가 서로 어긋났다. 코드면 같은 버전을 싣는 한 같은 값을 본다 — 바뀌는 길이
push·배포·플러그인 갱신 하나뿐이다. 읽는 쪽은 그대로 `collector.config()` 를 부른다.

사이트별 설정은 여기 없다 — Brain 의 project_settings 가 정본이다(db.project_cfg).
설정 우선순위: CLI > 사이트 설정 > 여기 defaults > 코드 리터럴(collector.settings).

self-check:  python skill_config.py
"""

CONFIG: dict = {
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
    #   other    위 셋이 아닌 일(광고 랜딩 확인·사이트 주인이 시킨 수집) — 인용과 무관하다
    # engine 은 사람이 읽는 이름이다(요청문이 "어느 엔진에서" 를 말할 때 쓴다).
    # may_ignore: 벤더 문서가 "사용자가 시킨 페치라 robots.txt 를 따르지 않을 수 있다"고
    #   적은 봇. 막는 줄이 있어도 실제로는 들어올 수 있다 — 표와 요청문이 그 말을 붙인다.
    #   그래도 기회로 두는 이유: 막는 줄이 있다는 것은 사이트 주인이 모르고 있을 일이다.
    #
    # 한 줄 한 줄의 기준은 **벤더 공식 문서**다(2026-11-24 확인). 제3자 목록에만 있는
    # 이름은 넣지 않는다 — 벤더가 안 쓰는 UA 의 행은 "열림/막힘"이 아무 뜻도 없다:
    #   - anthropic-ai·Claude-Web: Anthropic 문서에 없다(현행 봇은 아래 셋). 뺐다.
    #   - cohere-ai: Cohere 문서(docs.cohere.com/docs/cohere-web-crawlers)의 봇 표는
    #     이름이 "N/A" 이고 학습용 크롤을 하지 않는다고 적는다. 넣지 않았다.
    # ua 는 robots.txt 토큰이다 — 글자·`_`·`-` 만(test_ai_access 가 못 박는다).
    "ai_bots": [
        # OpenAI — https://developers.openai.com/api/docs/bots (platform.openai.com/docs/bots)
        {"ua": "GPTBot", "vendor": "OpenAI", "purpose": "training", "engine": "OpenAI 모델 학습"},
        {"ua": "OAI-SearchBot", "vendor": "OpenAI", "purpose": "search", "engine": "ChatGPT 검색"},
        # "Because these actions are initiated by a user, robots.txt rules may not apply."
        {"ua": "ChatGPT-User", "vendor": "OpenAI", "purpose": "user", "engine": "ChatGPT",
         "may_ignore": True},
        # ChatGPT 광고로 제출된 랜딩 페이지만 방문한다. robots.txt 적용 여부는 문서에 없다
        {"ua": "OAI-AdsBot", "vendor": "OpenAI", "purpose": "other",
         "engine": "ChatGPT 광고 랜딩 확인"},
        # Anthropic — https://support.claude.com/en/articles/8896518 (셋 다 robots.txt 를 따른다)
        {"ua": "ClaudeBot", "vendor": "Anthropic", "purpose": "training", "engine": "Claude 모델 학습"},
        {"ua": "Claude-SearchBot", "vendor": "Anthropic", "purpose": "search", "engine": "Claude 검색"},
        {"ua": "Claude-User", "vendor": "Anthropic", "purpose": "user", "engine": "Claude"},
        # Perplexity — https://docs.perplexity.ai/guides/bots
        {"ua": "PerplexityBot", "vendor": "Perplexity", "purpose": "search", "engine": "Perplexity"},
        # "Since a user requested the fetch, this fetcher generally ignores robots.txt rules."
        {"ua": "Perplexity-User", "vendor": "Perplexity", "purpose": "user", "engine": "Perplexity",
         "may_ignore": True},
        # Google — https://developers.google.com/crawling/docs/crawlers-fetchers/google-common-crawlers
        #          https://developers.google.com/crawling/docs/crawlers-fetchers/google-user-triggered-fetchers
        # Google-Extended 는 제미나이 학습·그라운딩용 제어 토큰이다(따로 도는 봇이 아니다) —
        # 구글 검색·AI 요약 노출은 Googlebot 이 정한다(색인 막힘 쪽)
        {"ua": "Google-Extended", "vendor": "Google", "purpose": "training",
         "engine": "Gemini 모델 학습"},
        # 사이트 주인이 Vertex AI 에이전트를 만들려고 시킨 수집
        {"ua": "Google-CloudVertexBot", "vendor": "Google", "purpose": "other",
         "engine": "Vertex AI 에이전트 (사이트 주인이 시킨 수집)"},
        # 사용자 요청 페치 둘 — "these fetchers generally ignore robots.txt rules"
        {"ua": "Google-Agent", "vendor": "Google", "purpose": "user",
         "engine": "Google 에이전트", "may_ignore": True},
        # 옛 이름 Google-NotebookLM 은 2026-08 까지만 지원됐다
        {"ua": "Google-GeminiNotebook", "vendor": "Google", "purpose": "user",
         "engine": "Gemini 노트북 (NotebookLM)", "may_ignore": True},
        # 빙 검색 그 자체이자 Copilot 이 인용하는 색인 — 막혔으면 AI 만의 문제가 아니다.
        # AI 전용 토큰은 따로 없다
        {"ua": "Bingbot", "vendor": "Microsoft", "purpose": "search", "engine": "Bing 검색·Copilot"},
        # Apple — https://support.apple.com/en-us/119829 (크롤하지 않는 제어 토큰)
        {"ua": "Applebot-Extended", "vendor": "Apple", "purpose": "training",
         "engine": "Apple 모델 학습"},
        # Meta — https://developers.facebook.com/docs/sharing/webmasters/web-crawlers
        {"ua": "meta-externalagent", "vendor": "Meta", "purpose": "training",
         "engine": "Meta 모델 학습"},
        {"ua": "meta-webindexer", "vendor": "Meta", "purpose": "search", "engine": "Meta AI"},
        # "may bypass robots.txt because it performs fetches that were requested by the user"
        {"ua": "meta-externalfetcher", "vendor": "Meta", "purpose": "user", "engine": "Meta AI",
         "may_ignore": True},
        # Mistral — https://docs.mistral.ai/robots
        {"ua": "MistralAI-Training", "vendor": "Mistral", "purpose": "training",
         "engine": "Mistral 모델 학습"},
        {"ua": "MistralAI-Index", "vendor": "Mistral", "purpose": "search", "engine": "Mistral"},
        {"ua": "MistralAI-User", "vendor": "Mistral", "purpose": "user", "engine": "Mistral"},
        # DuckDuckGo — https://duckduckgo.com/duckduckgo-help-pages/results/duckassistbot
        # AI 보조 답변을 만들 때 그 자리에서 페이지를 읽는다. 학습에는 쓰지 않는다
        {"ua": "DuckAssistBot", "vendor": "DuckDuckGo", "purpose": "search",
         "engine": "DuckDuckGo AI 답변"},
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
        "google.com",       # 검색 자체(google.com/search·support 등) — 경쟁사 후보로 섞였다
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

    # AI 인용 확인 엔진 — 이 표 한 벌이 정본이다. 엔진 id 는 ai_checks.engine 에 남고
    # 화면(shell/03-parts.js 의 ENG_NAME)이 그 id 로 이름·로고를 찾는다(test_seam_202).
    #   model    OpenRouter 슬러그. 시간이 지나면 바뀐다 — 바꿀 때는 여기 한 곳.
    #   default  사이트가 surfaces_ai 로 고르지 않았을 때 묻는 엔진인가
    #   usd      한 번 묻는 **추정** 단가($) — 잔액 카나리아만 쓴다. 실청구는 응답 usage.cost.
    #   body     요청에 더 싣는 값(소비자 앱의 기본 모드를 흉내 내는 자리)
    # 기준은 "사람이 그 앱을 그냥 열었을 때 받는 답"이다(2026-10-23 OpenRouter 목록):
    #   chatgpt  chat-latest 는 OpenAI 가 ChatGPT 에 쓰는 Instant 모델을 따라가는 별칭이다.
    #            gpt-4o-mini 는 앱에서 내려간 지 오래라 출처 고르는 방식이 앱과 달랐다.
    #   gemini   앱 기본(빠른 모드)은 최신 Flash 에 생각을 짧게 하는 것이다. 생각 토큰도
    #            max_tokens 에 들어가 길게 생각하면 답 뒤쪽 인용이 잘린다.
    #   perplexity  무료 사용자의 기본 검색이 Sonar 다.
    #   claude   claude.ai 기본 모델. 기본으로는 안 묻는다(비싸다) — 사이트 surfaces_ai 로 켠다.
    # ':online' 은 OpenRouter 웹 플러그인이고, 네 제공자 모두 제공자 자체 검색(native)을 탄다
    # (Gemini 는 구글 검색 그라운딩). sonar 는 검색이 모델에 들어 있다.
    "ai_engines": {
        "chatgpt":    {"model": "openai/gpt-chat-latest:online", "default": True, "usd": 0.08},
        "perplexity": {"model": "perplexity/sonar", "default": True, "usd": 0.01},
        "gemini":     {"model": "google/gemini-3.8-flash:online", "default": True, "usd": 0.03,
                       "body": {"reasoning": {"effort": "low"}}},
        "claude":     {"model": "anthropic/claude-sonnet-5.5:online", "default": False,
                       "usd": 0.10},
    },

    # 구글 업데이트 원장 + 서치콘솔 데이터 이상 구간 — 순위 하락·클릭 변화의 **원인 가설**로만
    # 쓴다(scoring.ledger_between). 방어형 기회는 원인을 잘못 짚으면 멀쩡한 페이지를 고친다:
    # 떨어진 구간이 구글 업데이트·계측 오류와 겹치면 그 사실을 근거 표에 한 줄 적는다.
    #
    # **구글 공지로 확인된 것만** 넣는다. 항목마다 url(그 공지)·last_verified(그 주소를 연 날).
    # 블로그·도구 회사의 '업데이트 감지'는 넣지 않는다. 가장 최근 확인일에서 stale_days 가
    # 지나면 stale 이다 — 그 뒤 업데이트가 빠졌을 수 있다고 화면·요청문·점검이 말한다.
    # 갱신: 아래 두 주소를 열어 새 줄을 더하고 last_verified 를 그날로 바꾼다.
    #   updates       https://status.search.google.com/summary (기계가 읽는 꼴: /incidents.json)
    #   gsc_anomalies https://support.google.com/webmasters/answer/6211453 (최근 3~16개월만 남는다)
    # end 가 None 이면 구글이 아직 끝났다고 공지하지 않은 것이다(진행 중).
    # kind: core | spam | discover | serving (라벨은 scoring.LEDGER_KIND_LABEL).
    #   discover 는 디스커버 피드의 일이라 웹 검색 실적의 가설로는 안 붙인다.
    "google_ledger": {
        "stale_days": 30,
        "updates": [
            {"kind": "core", "name": "December 2025 core update",
             "start": "2025-12-11", "end": "2025-12-29",
             "url": "https://status.search.google.com/incidents/DsirqJ1gpPRgVQeccPRv",
             "last_verified": "2026-11-24"},
            {"kind": "discover", "name": "February 2026 Discover update",
             "start": "2026-02-05", "end": "2026-02-27",
             "url": "https://status.search.google.com/incidents/mYbNTqV1ytDc2fA8hUz4",
             "last_verified": "2026-11-24"},
            {"kind": "serving", "name": "Serving was experiencing an issue",
             "start": "2026-02-25", "end": "2026-02-25",
             "url": "https://status.search.google.com/incidents/TZF4pLmbzFdoBPP6o3iv",
             "last_verified": "2026-11-24"},
            {"kind": "spam", "name": "March 2026 spam update",
             "start": "2026-03-24", "end": "2026-03-25",
             "url": "https://status.search.google.com/incidents/VbnSXAH4SmEcxPtx4YSD",
             "last_verified": "2026-11-24"},
            {"kind": "core", "name": "March 2026 core update",
             "start": "2026-03-27", "end": "2026-04-08",
             "url": "https://status.search.google.com/incidents/7eTbAa2jWdToLkraZj5y",
             "last_verified": "2026-11-24"},
            {"kind": "core", "name": "May 2026 core update",
             "start": "2026-05-21", "end": "2026-06-02",
             "url": "https://status.search.google.com/incidents/wdAXJk6LRRihEjpzEeWE",
             "last_verified": "2026-11-24"},
            {"kind": "spam", "name": "June 2026 spam update",
             "start": "2026-06-24", "end": "2026-06-26",
             "url": "https://status.search.google.com/incidents/YUX1peHev5a4fkxLDiUQ",
             "last_verified": "2026-11-24"},
            {"kind": "spam", "name": "August 2026 spam update",
             "start": "2026-08-18", "end": "2026-08-21",
             "url": "https://status.search.google.com/incidents/LEubPCm2octf2uMqCFKE",
             "last_verified": "2026-11-24"},
            {"kind": "spam", "name": "September 2026 spam update",
             "start": "2026-09-24", "end": "2026-10-08",
             "url": "https://status.search.google.com/incidents/XhUDXP7A67iHCD2kmbVu",
             "last_verified": "2026-11-24"},
            # 2026-11-24 확인 시점에 구글이 종료를 공지하지 않았다("최대 2주") — end 는 비워 둔다
            {"kind": "core", "name": "November 2026 core update",
             "start": "2026-11-10", "end": None,
             "url": "https://status.search.google.com/incidents/WLxcJyC6jMWMfryxsKMc",
             "last_verified": "2026-11-24"},
        ],
        # 서치콘솔 '검색결과' 실적의 **검색어 단위 숫자**(gsc_snapshots)를 건드린 것만 넣는다.
        # 같은 공지 페이지의 디스커버·생성형 AI 보고서·검색 노출 유형(FAQ·채용 공고) 항목은
        # 우리가 읽는 숫자가 아니라 뺐다. metrics = 구글이 영향받았다고 적은 지표.
        "gsc_anomalies": [
            # "A logging error prevented Search Console from accurately reporting impressions
            #  from May 13, 2025 until April 27, 2026 … Only impressions and related metrics -
            #  CTR and average position - were affected; clicks were not affected"
            {"start": "2025-05-13", "end": "2026-04-27",
             "metrics": ["impressions", "ctr", "position"],
             "note": "서치콘솔 노출 로깅 오류 — 노출과 거기서 나오는 클릭률·평균 순위가 "
                     "부정확했고 클릭은 영향이 없었다(구글 공지)",
             "url": "https://support.google.com/webmasters/answer/6211453",
             "last_verified": "2026-11-24"},
        ],
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
        "gsc_breakdown": "device,country",   # device|country — 둘 다 [클릭 변화] 화면이 쓴다
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
        # DataForSEO 간격은 경로마다 다르고, 정본은 serp_adapter.PACED 다(어느 경로에
        # 얼마를 쉬는지 여기 베끼지 않는다). 간격은 serp_adapter 가 자동으로 지킨다 —
        # defaults.throttle 로는 못 막는다. Google Ads 계정 한도를 올렸으면
        # SEOMINER_DFS_RPM 로 알려 준다(0 = 간격 없음). 순위는 우선 대기열(task_post)이고
        # 단가는 serp_adapter.PROVIDERS 가 정본이다(실청구액은 응답에서 기록됨).
        # serper: 크레딧 과금, AI오버뷰 데이터 없음(aio 필드 null)
    },
}


def _selfcheck() -> None:
    assert {"ai_bots", "third_party_platforms", "ai_referrers", "ai_engines",
            "defaults", "serp"} <= set(CONFIG)
    for eid, e in CONFIG["ai_engines"].items():
        assert {"model", "default", "usd"} <= set(e) <= {"model", "default", "usd", "body"}, eid
    assert any(e["default"] for e in CONFIG["ai_engines"].values())
    for b in CONFIG["ai_bots"]:
        assert b["purpose"] in ("search", "user", "training", "other"), b
    assert "chatgpt.com" in CONFIG["ai_referrers"]
    assert CONFIG["defaults"]["serp_depth"] == 10
    led = CONFIG["google_ledger"]
    for e in [*led["updates"], *led["gsc_anomalies"]]:
        assert {"start", "end", "url", "last_verified"} <= set(e), e
        assert e["url"].startswith("https://"), e


if __name__ == "__main__":
    _selfcheck()
    print("skill_config ok")
