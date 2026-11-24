# claude-seo에서 건질 것 — 차용 계획

외부 플러그인 [`AgriciDaniel/claude-seo`](https://github.com/AgriciDaniel/claude-seo)
(Claude Code 플러그인, v2.6.2, MIT, 별 약 3.2만)를 seo-miner와 대조한 결과와, 가져올 때의
착지점을 적는다. 2026-11-24 이 PC에 설치(사용자 범위)한 뒤, 스킬 29개·에이전트 20개마다
에이전트를 하나씩 붙여 대조하고 종합했다.

표기:
- ✔ 이 문서를 쓰며 코드로 직접 확인했다
- ◇ 대조 에이전트가 보고한 파일:줄이다 — 손대기 전에 다시 본다
- ⚠ 구글 정책·날짜 같은 외부 사실이고 claude-seo 쪽 주장이다 — 공지로 확인한 뒤에 넣는다

크기: S(반나절 이하) · M(하루~이틀) · L(그 이상, 새 수집기·새 출처).

---

## 요약 — 두 도구의 성격

claude-seo는 seo-miner의 경쟁자라기보다 **보조 점검표**다.

- **claude-seo**: URL 하나를 받아 대화 안에서 한 번 감사한다. 스크립트는 HTML·API를 가져오는 데서
  대부분 멈추고, 판정과 0~100 점수(Health·E-E-A-T·GEO·SXO 등)는 LLM이 참고 문서를 읽고 매긴다.
  결과는 마크다운·PDF로 끝난다. 누적 저장은 drift 기준선 SQLite 하나뿐이다. 범위(API·표면 종류)는 넓다.
- **seo-miner**: 단계를 주기적으로 돌려 brain.db에 쌓고, `scoring.py`의 결정적 코드와 테스트로
  판정한다. 점수 단위는 사이트가 아니라 기회 하나다. 요청문 → plays → PR → 관찰 중으로 이어진다.
  깊이·재현성·비용 통제·로케일 대응(`serp_adapter.LOCALES`·`LOCATION_MAP`, CJK 폭 기준)이 앞선다.

**대상은 전 세계다.** seo-miner 는 로케일 단위로 돈다 — 한 나라 기준으로 거르지 않는다. 그래서 이 문서는
"어느 시장에 쓸모 있나"가 아니라 "사이트의 로케일에 맞춰 켤 수 있나"로 가른다. 지역 출처(리뷰 플랫폼·
지도·검색엔진)는 로케일별 표 한 벌로 두고, 영어 전용 규칙(가독성·CTA 목록)은 언어별 규칙으로 들인다.
- 지식(봇 용도 구분, llms.txt 무가중, 추출성)은 많이 겹친다. 다른 건 그 지식을 체크리스트 문서로
  두느냐 코드와 테스트로 두느냐다.

**의존하지 말고 판정 규칙과 사실 갱신만 가져온다.** 들일 때는 결정적 코드 + 테스트로 바꾼다.

---

## 0. 먼저 — 호스팅 SSRF ✔

- 사이트 **등록 때 한 번** 그 도메인이 내부 주소로 풀리는지 본다(`server/app.py:588-594`).
- 실제 가져오기는 `skills/capture/scripts/http_adapter.py:114` 가 `allow_redirects=True` 로
  리다이렉트를 그대로 따라간다. 등록된 사이트가 `169.254.169.254`·사설 IP로 리다이렉트하거나,
  등록 뒤 DNS를 내부 IP로 바꾸면(DNS rebinding) Railway 컨테이너가 그 주소를 연다.
- claude-seo 방식: 요청마다 DNS를 풀어 공인 주소인지 보고 그 IP에 고정, 리다이렉트는 수동으로
  한 홉씩 따라가며 홉마다 같은 검사, 바이트 상한, DOCTYPE 거부.
- 착지점: `http_adapter.py` 한 곳(초크포인트). 호스팅에서만 켤지, 로컬도 켤지 정한다.
- 크기: M. 기능보다 먼저다.

---

## 1. 고칠 거리 — 이미 확인된 것 ✔

| # | 무엇 | 근거 | 크기 |
|---|---|---|---|
| 1 | **FAQPage·HowTo를 권하는 문구가 리포 안에서 엇갈린다.** `_CTR_ACTS` 주석은 FAQ·HowTo 리치 결과가 대부분 안 나온다고 적는데, 처방은 FAQPage를 넣으라고 한다 | `scoring.py:5099` vs `:2235`·`:2238`, `skills/create/references/content-rules.md:26` ("FAQPage/HowTo 구조화 데이터를 관례대로 추가"). ⚠ FAQ 리치 결과 전면 종료일(2026-05-07)은 claude-seo 주장 | S |
| 2 | **robots.txt가 200이 아니면 전부 허용으로 본다.** 5xx는 준수 크롤러가 사이트 전체 차단으로 읽고, 429는 판정 불가다 | `collect_crawl.py:425-428` | S |
| 3 | **`gsc_query.py` 의 `totals` 는 사이트 합계가 아니라 상위 N행 합이다.** `cmd_search` 가 `--limit` 로 자른 행을 합쳐 `totals` 라는 이름으로 돌려준다 | `gsc_query.py:113`·`:141`. 차원 없는 질의를 한 번 더 해 진짜 합계를 받고, 지금 값은 이름을 갈라 표시 | S |
| 4 | **현장 TTFB를 모으기만 하고 판정하지 않는다** | `field_ttfb_ms` 가 `collect_vitals.py:52` 에만 있고 `scoring`·`brief` 에 없다. 구글 기준 800ms | S |
| 5 | **2MB 넘는 HTML을 조용히 잘라 읽는다** | `collect_page.py:46` `MAX_HTML`. 잘렸다는 사실을 행에 남기고 경고로 | S |
| 6 | **`doctor.find_skill` 이 claude-seo의 `seo-audit` 도 '설치됨'으로 센다.** 위임 상대는 coreyhaines31 판인데, 캐시를 `*/*/*/skills/<이름>/SKILL.md` 로 훑어 아무 팩이든 잡는다. claude-seo의 seo-audit는 `[url]` 하나만 받는 다른 스킬이라 위임이 조용히 어긋난다 | `skills/setup/scripts/doctor.py:265-283`. 출처 팩까지 확인 | S |

---

## 2. 가져올 만한 것 — 상위 (여러 항목에서 겹쳐 나온 제안)

| # | 무엇 | 왜 | 어디에 ◇ | 크기 |
|---|---|---|---|---|
| 1 | **폐기·리치결과 없는 유형 표 한 벌.** HowTo·SpecialAnnouncement·ClaimReview·VehicleListing·EstimatedSalary·LearningVideo·CourseInfo. 권하지 않고, 이미 단 마크업은 '리치 결과 없음'으로 말한다. 'ld+json 없음' 처방을 사이트 타입별 권장 @type으로(saas→Organization·WebSite·SoftwareApplication, commerce→Product·BreadcrumbList, local_business→LocalBusiness, 글→Article) | 1절 1번 모순의 근본 처방. SaaS 홈에 LocalBusiness를 권하는 일반 문장도 나간다 | `scoring.py` SCHEMA_RULES(1626) 옆 `RETIRED_TYPES`, `page_advice`(2221-2244, 시그니처에 site type), `_structured_data_advice`, `content-rules.md`, `test_seams` 에 한 벌 고정 | S (타입별 @type까지 M) |
| 2 | **구글 업데이트 원장 + GSC 데이터 이상 구간.** 날짜 구간·종류·구글 URL·last_verified, 30일 넘으면 stale. `rank_decay`·`click_shift` 구간과 겹치면 "원인 가설일 뿐" 한 줄. ⚠ claude-seo가 주장하는 GSC 노출·CTR·순위 로깅 오류 구간(2025-05-13~2026-04-27, 클릭은 무관)은 확인 후 | 방어형 기회는 원인을 잘못 짚으면 멀쩡한 페이지를 고친다. `docs/borrow-from-google-seo-mcp.md` 2번에 같은 제안이 있고 아직 안 했다 | `skill_config.py` 상수(yaml 금지), `scoring.rank_decay`(3459)·`click_shift`(3577), `brief._ev_decay`, `analysis.html`, `audit_data.py` 이상 띠 | M |
| 3 | **크롤·색인 판정 정확성 묶음.** ① robots.txt 상태 저장(5xx 차단·429 판정 불가) ② 사이트맵 흔한 경로 폴백(`/sitemap.xml`·`/sitemap_index.xml`·`/sitemap-index.xml`·`/wp-sitemap.xml`)·gzip 해제(매직 1f8b)·상한/파싱 실패 기록 ③ 크롤 중 429·430·503 을 `http_error` 에서 빼고 물러서기, Crawl-delay 준수 ④ X-Robots-Tag 헤더, nosnippet·max-snippet:0·data-nosnippet ⑤ 소프트 404 탐침(없는 경로 GET 1회) | Sitemap: 줄이 없으면 orphan·in_sitemap 근거가 통째로 사라진다. 일부만 속도 제한에 걸려도 없는 crawl_issue 기회가 선다. 전부 이미 받는 응답이나 요청 1~3회 | `collect_crawl.py` discover_seeds(413)·_sitemap_urls(392)·_ISSUE_SQL(613)·ISSUE_KIND, `collect_page.py` BLOCK_STATUS(805)·fetch(823), `scoring.page_advice` robots 분기(2271) | 각 S, 합쳐 M |
| 4 | **받아 놓고 버리는 데이터 쓰기.** URL Inspection `rich_results_json`(저장만 하고 아무도 안 읽음)·`referringUrls`·`crawledAs`, search_volume 의 `monthly_searches`(12개월), 상위 글 `schema_json`(페이지 종류 분포), backlinks summary 의 spam score, local_pack 항목(지도 칸에 내가 있나), PSI 의 LCP 요소·렌더 차단·unsized-images·final-screenshot | 추가 비용 0~거의 0. 요청문의 [확인 필요] 칸(페이지 종류·구글 판정·지도 칸 경쟁)을 숫자로 채운다. 응답 필드 이름은 실제 응답으로 먼저 확인 | `collect_index.py`(41-82), `collect_metrics.py`(352), `collect_serp._collect_outlines`(558), `collect_backlinks.py`(132), `serp_adapter.py`(667-690), `collect_vitals.py` parse·IMAGE_AUDITS | 항목마다 S~M |
| 5 | **회차 사이 필드 값 비교(drift).** `crawl_compare` 에 changed(canonical 변경, noindex 추가, H1·title 변경, schema 유형 소멸, 2xx→4xx/5xx). `rank_decay` 요청문에 '떨어진 기간 그 페이지에서 바뀐 것' 표. `watch_rows` 에 page_diff(고친 게 배포됐나). page_vitals 같은 범위끼리 상대 악화(p75 20%, lab 10점). 별도 기준선 저장소는 안 만든다 | 원재료가 날짜별로 쌓여 있는데 화면은 최신만 읽는다. 유효한 값끼리 바뀐 canonical은 지금 이슈가 아니라서 안 잡힌다 | `dashboard.py` crawl_compare(616)·vitals(1257), `db.watch_rows`(3354), `brief._ev_decay`(2360), `scoring.vitals_advice`(1320), `site.html` | M |
| 6 | **템플릿 메타 감지(언어별).** 설명이 title 앞부분을 되풀이, 설명=title, 브랜드 꼬리가 설명에 섞임. 사이트 단위 비율(3쪽 이상·10%/30%)로 심각도. 상투 CTA 목록은 언어별 표 한 벌 | `dup_description` 은 완전 일치만 잡는다. 원본 `metadata_template.py` 는 영어에선 동작하지만 비라틴 문자(한글·CJK 등)를 지운 뒤 비교해서 그 언어에선 아무것도 못 잡는다 — 정규화를 유니코드 기준으로 새로 짜고 CTA 목록은 영어부터 언어별로 | `collect_crawl.py` ISSUE_KIND·_ISSUE_SQL(627 부근), `scoring._desc_scraped`(2003) 옆에 한 벌 | S~M |
| 7 | **구조화 데이터 값 검사.** Product Merchant listing 수준(price>0 숫자·priceCurrency·availability 전체 URL·Offer 유형·gtin|mpn·shippingDetails·hasMerchantReturnPolicy). 자리표시 문구·상대 URL·ISO 8601 아닌 날짜·https @context. Microdata만 있는 페이지를 'ld+json 없음'으로 오판하지 않기 | 지금은 칸 존재만 봐서 "$29.99"·상대 logo도 통과. Microdata 사이트에 있는 마크업을 또 만들라고 시킨다 | `scoring.py` SCHEMA_RULES(1626), `collect_page._schema_scan`(203)·`_schema_gaps_out`(353)·handle_starttag(440) | M |
| 8 | **측정은 있는데 다음 걸음이 없는 자리.** ① GA4 속성 전체 key_events 합이 0이면 '전환 없는 페이지' 표와 value_mult 0.85를 끄고 "전환 이벤트 미설정"으로 ② 전환 없는 페이지에서 전환 점검 요청문 또는 cro 위임 줄 ③ backlink_mention·prospect 해소를 무료 GET으로 즉시 확인(403·429·JS 껍데기는 '확인 불가') ④ hreflang 코드 실명단(eng·jp·es-LA·en-EU 통과 중) | 전환 이벤트를 안 심은 사이트는 점수가 일괄 0.85배. 해소 판정은 30일 유료 수집을 기다린다 | `scoring.py` zero_conversion_pages(3215)·value_mult(4939)·_resolve_prospect·_resolve_mention(6086-6110)·_HREFLANG_RE(1915), `external-skills.md`, `doctor.py` OPTIONAL_SKILLS | 각 S~M |

---

## 3. 가져올 만한 것 — 영역별 추가

### 크롤·기술

| 무엇 | 착지점 ◇ | 크기 |
|---|---|---|
| 호스트 정규화: http→https 301 여부, www/비www 둘 다 200인지(중복 호스트), 인증서 만료 임박. 보안 헤더 등급은 빼고 HTTPS 강제·HSTS만 | `collect_crawl.py` 홈 점검 + ISSUE_KIND | S~M |
| 사이트맵에 넣으면 안 되는 주소(비200·리다이렉트·noindex·canonical 다름) / 색인 가능한데 사이트맵에 없는 페이지(info) | `collect_crawl.py` _ISSUE_SQL | S~M |
| 사이트맵 lastmod 저장 — 전부 같은 날짜 비율·W3C 형식 오류, page_audits dateModified와 어긋남 | `db.py` sitemap_urls·`_sitemap_urls` | M |
| 사이트맵 위생: 토큰·세션 파라미터 URL, 파일 간 중복, 다른 호스트 loc, 5만 URL·50MB·loc 2048자 한도, RSS·Atom·txt 사이트맵 | `_sitemap_urls` | S |
| GSC 사이트맵 목록(errors·warnings·last_downloaded·is_pending)을 회차마다 적재 | `collect_index.py` | S |
| URL 위생(쿼리 파라미터 색인 대상·대문자·과도한 길이·밑줄·이중 슬래시) — 저장된 crawl_pages에 SQL만 | `_ISSUE_SQL` | S |
| canonical 대상을 한 번 더 열어 리다이렉트·고리·4xx 확인 | `collect_page.collect`·`page_advice` canonical 갈래 | M |
| hreflang 대상이 리다이렉트·4xx·비canonical(`hreflang_bad_target`), 묶음 안 http/https 섞임·끝 슬래시 불일치, 사이트맵 `xhtml:link`·HTTP Link 헤더 hreflang 읽기 | `collect_crawl.py` ISSUE_KIND(60-117)·_ISSUE_SQL(669)·crawl_hreflang(595) | M |
| 같은 경로 묶음(`/city/*`) 안 본문 근사 중복 — pSEO·다지점 도어웨이(`template_thin`) | `collect_crawl.py` shingle 해시, `db.py` crawl_pages | M |
| 숨긴 텍스트 세기(인라인 style·hidden만, 아코디언 접힘은 따로) | `collect_page.py` 파서, `page_advice` | M |
| mixed content·http 링크·텍스트 없는 링크·'여기를 클릭' 일반 앵커 | `collect_page.py`·`collect_crawl.py` | S |
| meta refresh·charset 불일치·lorem ipsum 잔존 | `collect_page.py` | S |
| H3~H6 수집, 제목 단계 건너뜀(H2→H4) | `collect_page.py`(지금 H1·H2만) | S |
| 크롤 표본을 URL 꼴(템플릿)별로 층화 — 상한 안에서 글 목록 하나가 표본을 다 먹지 않게 | `collect_crawl.py` 큐 | M |
| 크롤 응답 지연 p50·p95 | `crawl_runs` | S |
| js_shell 페이지만 헤드리스로 렌더해 다시 읽기, 원본 대 렌더 차이(js_gap: 렌더 뒤에만 생기는 H1·schema·canonical) | `collect_page.py` js_shell(579) 옆 | L |

### AI 검색

| 무엇 | 착지점 ◇ | 크기 |
|---|---|---|
| AI 봇 UA로 직접 요청해 WAF·CAPTCHA 반응 보기 — Cloudflare 기본값이 AI 크롤러를 막는 일이 흔한데 robots만 봐선 모른다. 내 사이트에만, '서버가 막음'과 'robots가 막음'을 갈라 말한다 | `collect_crawl.py` 또는 `collect_page.py`, `scoring.ai_bot_status` | M |
| AI 봇 그룹 판정을 RFC 9309 방식으로(토큰 전체 일치, 가장 구체적 그룹 우선, `GPTBot/1.1` 같은 비표준 UA 줄 감지) | `scoring.py` robots_blocks(1558)·ai_bot_status(1465) | M |
| 봇 목록 갱신: OAI-AdsBot·Google-Agent·Google-CloudVertexBot·Google-GeminiNotebook·cohere-ai. ⚠ claude-seo는 `anthropic-ai` 를 미확인 UA라 뺐다(우리는 `skill_config.py:37` 에 학습용 옛 UA로 둠) | `skill_config.py` ai_bots | S |
| robots.txt Content-Signal 줄(search·ai-input·ai-train) — 원문 한 줄 표시, 판정 없음 | `scoring.ai_bot_status` 옆, `brief._ev_ai_bot` | S |
| llms.txt 형식 판정(Lighthouse 통과 규칙: H1·마크다운 링크 1개 이상·50자 이상) — '있지만 형식이 깨짐' | `collect_crawl.probe_llms_txt`(448), `brief._llms_lines`(2971) | S |
| 추출성을 H2 구간별 첫 문단 길이·긴 문단 수로(지금은 첫 문단 하나) | `collect_page.py` lead_words(530), `scoring.extract_advice`(2343) | M |
| 본문만 뽑기(내비·푸터 제외, trafilatura 방식)와 발행일 추출(htmldate 방식) | `collect_page.py` | M |
| GA4 'AI Assistants' 기본 채널과 우리 AI 출처 호스트 목록 대조 | `collect_ga4.py` | S |
| AI Mode를 AI 요약과 다른 인용 면으로 따로 표기 | `collect_ai.py`·`aicite.html` | S |
| aio_exposure 확인 목록에 Search Console 'Search generative AI' 설정 한 줄. ⚠ 설정 존재부터 확인 | `scoring.py` _AIO_PLAY(5205) | S |
| PSI `category=agentic-browsing` X/N — 참고 칸만 | `collect_vitals.py` | M |
| DataForSEO AI Optimization LLM 언급 집계 — 내 샘플 질문 밖에서 누가 인용되나 | `collect_ai.py` 선택 하위 단계 | M |

### 요청문·콘텐츠

| 무엇 | 착지점 ◇ | 크기 |
|---|---|---|
| new_content 꼴에 **Information Gain 한 줄 필수** — 상위 글에 없고 우리만 쓸 수 있는 것, '더 자세히' 금지, 없으면 [확인 필요] | `brief.py` SHAPES['new_content'].form(179-197) — brief-upgrade 흐름으로 테스트부터 | S |
| 쓰기 전 카니벌 예방 — new_content를 내기 전에 기존 페이지와 SERP가 많이 겹치면(claude-seo 기준 7개 이상) '새 글 말고 보강'으로 돌림 | `scoring.py` coverage 판정, `_surface_facts` | M |
| 요청문마다 '실패를 어떻게 알까'(성공 기준·선행 지표) 한 줄 — 관찰 중 화면의 판정 기준이 된다 | `brief.py` 공통 끝절 | S |
| 상위 글 비교 재료에서 제3자 플랫폼(Wikipedia·Reddit·Quora·YouTube·Medium, 로케일별로 나무위키·네이버 블로그·知乎 등 — `skill_config` third_party_platforms 한 벌)·뉴스·.gov/.edu·마켓·SEO 도구 사이트·`/tag/`·`/author/` 빼기. SERP 겹침 계산에서도 | `collect_serp.py:323-326`, `plays.py:232-241`, `brief.py` _serp_top·_format_lines, `scoring._surface_facts`(6720) | S |
| 허브·coverage 요청문에 사이트에 실제로 있는 하위 페이지 목록 + '빠뜨리지도 지어내지도 말 것'(Site Structure Coverage Rule) | `brief.py` coverage(3203)·_pseo_hub(2463), 재료는 크롤 페이로드 | M |
| coverage 요청문에 같은 클러스터 기존 페이지와 주고받을 내부 링크 절 | `brief.py`, `dashboard.py` topic_pages·_crawl_inlinks | M |
| H2별 보조 키워드 배정(이 소제목에 어느 GSC 검색어) | `brief.py` | M |
| 내부 앵커 다양성 — 같은 앵커 40% 초과, 들어오는 내부 링크 3개 미만 글 | 저장된 crawl_links | S~M |
| 클러스터 라벨을 SERP 겹침으로 검증(같은 클러스터인데 안 겹침 / 다른 클러스터인데 많이 겹침) | `audit_data.py` 또는 `/capture keywords` 큐레이션 | M |
| 비교 글 공정성 규칙 — 값마다 확인 날짜·출처, 우리 것임을 밝힘, 상대가 나은 항목도 남김, 숨은 비용 표기, 경쟁사 설명 구간에 공격적 CTA 금지, A vs B ↔ A vs C 교차 링크 | `brief.py:621` DELIVER_BY_TAG['비교'], `content-rules.md:24` | S |
| 경쟁사 이름으로 '{경쟁사} 대안'·'{경쟁사} vs {우리}' 자동완성 씨앗 — 수요 확인된 것만 후보로 | `expand_keywords.py` | M |
| 지역 pSEO '바꿔 끼우기 검사' — 지역명만 바꿔도 말이 되는 문단은 고유 내용이 아니다 | `scoring.py` _PSEO_DELIVER(850) | S |
| pSEO 게이트 — 100장 경고·500장 정지, 5~10% 사람 표본 검토, 데이터 원본 행 수·필드 겹침·신선도 점검 | create·pSEO 규칙 | S |
| plays 문안 저장 전 보이지 않는 유니코드(제로폭·방향·태그 문자) 제거, 이모지 ZWJ는 남김 | `plays.py` strs()(400) | S |
| 강조 스니펫 형식(문단·목록·표·영상) 가르기 | `serp_adapter.py:667-690`, `brief.py:1343` SERP_FORMAT_HINT | S |
| SERP 광고 문구 주제로 상업 의도 보강 | `scoring.classify_intent` | M |
| 여정 단계(awareness·consideration·decision) 표기 | 의도 옆 | S |
| 사이트 신뢰 페이지(연락처·개인정보처리방침·약관·후기) 존재 점검 | 크롤 결과 | S |

### 구조화 데이터·이미지

| 무엇 | 착지점 ◇ | 크기 |
|---|---|---|
| create PR의 JSON-LD를 커밋 전에 검사(claude-seo PostToolUse 훅과 같은 일) — 자리표시·상대 URL·날짜·@context | `skills/create/scripts` | S~M |
| 스키마 속성 변화 감지 — `schema_json` 에 @type 목록만 있어 속성 하나 바뀐 건 모른다. 내용 해시 같이 저장 | `collect_page.py`, `db.py` page_audits | M |
| SCHEMA_RULES 에 ItemList·SoftwareApplication(name·offers, aggregateRating|review) | `scoring.py:1621-1641` | S |
| js_shell인데 ld+json이 없으면 '없음'으로 단정 말고 '클라이언트 주입 확인' | `collect_page.py` js_shell(579), `page_advice` | S |
| 자기 리뷰 마크업 경고 — ⚠ Organization·LocalBusiness 자기 별점은 표시 안 됨(구글 정책 확인) | `scoring.py` | S |
| 업종별 스키마: 병원 MedicalClinic·Physician, 음식점 Menu, 법률 LegalService(Attorney 대체 경고). 일반형 LocalBusiness·MedicalBusiness면 좁은 하위 유형 권장. 다지점 `@id`·`branchOf`. geo 소수 5자리·priceRange 길이·areaServed | `scoring.entity_advice`(1805), `schema_vocab.py` | S~M |
| LocalBusiness에 hasMap·openingHoursSpecification·sameAs(GBP·Apple·Bing 프로필) | SCHEMA_RULES LocalBusiness(1622), `nap_state`(1882) | S |
| alt 판정: 장식 이미지(aria-hidden·role=presentation/none) 제외, 파일명·'image'·'이미지'·IMG_1234 같은 뜻 없는 alt 따로 | `collect_page.py` img(463-466), 크롤 쪽 같은 파서 | S |
| og:image·og:title·og:description·twitter:card 유무 — info 한 줄 | `collect_page.py` meta, `page_advice` | S |

### 속도

| 무엇 | 착지점 ◇ | 크기 |
|---|---|---|
| CrUX History(같은 무료 키)로 25주 p75 추세 — 첫 회차부터 추세, 관찰 중 '그때·지금'을 현장값으로 | `collect_vitals.py`, `scoring.vitals_advice`(1320) | M |
| PSI final-screenshot 썸네일 — '구글이 연 모습'을 기기 차이 요청문 근거로 | `collect_vitals.py`, `page_vitals` | M |
| PSI 진단 더 받기: third-party-summary·unused JS/CSS·font-display·long-tasks·dom-size, opportunity 를 savings_ms 순으로. LCP·INP·CLS 권고를 고정 문장 대신 자원 이름으로 | `collect_vitals.py` parse(236-269) | S~M |
| PSI accessibility·best-practices 카테고리 점수와 실패 항목 | `collect_vitals.py` | S |
| lighthouseVersion 기록 | `page_vitals` | S |
| 콘솔 오류 수, 모바일 가로 스크롤·본문 글씨 크기 | 렌더 경로가 생기면 | M |

### 백링크·분석·키워드·비용

| 무엇 | 착지점 ◇ | 크기 |
|---|---|---|
| 앵커 유형 분류(브랜드 별칭·도메인/URL·정확 일치·일반·빈 앵커), 쏠림 경고는 정확 일치 비율로만 | `dashboard.py` bl_anchors(1148) 또는 `scoring.anchor_mix()` | S |
| 상호 링크 감지(크롤 외부 링크 ∩ 참조 도메인) | `backlinks.html` | S |
| 참조 도메인 분포 — TLD·국가·한 도메인 집중도(5%·10%·25%) | 받은 데이터로 | S |
| 백링크 많은 페이지(link magnet)와 0개 페이지 — 내부 링크를 어디서 흘릴지 | DataForSEO 페이지 단위 | S~M |
| 백링크 시계열(backlinks_timeseries_summary) — 링크 속도 경고 | `collect_backlinks.py` | M |
| 이미지 SERP — 이미지 칸이 뜨는 검색어에 한해 DataForSEO `serp/google/images` 상위 결과의 출처 도메인·alt 꼴 대비 우리 이미지(6절 검증에서 옮김) | `serp_adapter.py` 선택 과제, `collect_serp.py` | M |
| IndexNow — create PR 머지 확인 뒤 바뀐 URL 1회 알림(Bing·Yandex·Naver·Seznam·Yep), 키 파일 사전 점검, 키 없으면 건너뜀 | `skills/create/scripts/createdb.py` merged()(359) 뒤 | M |
| **Bing 축.** Copilot 은 Bing 검색 서비스로 질의를 보내고, ChatGPT 검색도 Microsoft 를 포함한 검색 제공자와 협력한다(단독 의존은 아님 — 6절 검증). AI 가시성과 이어진다. Bing Webmaster API(무료, 옛 SOAP·POX 는 2026-08-31 종료 — 지금 API 로)로 ① 색인·크롤 상태 ② 인바운드 링크(DataForSEO 없을 때 백링크 폴백, source=bing 표기) ③ 검색어 성과. 순위는 DataForSEO `se=bing` 으로 같은 rank 단계에서 | `collect_backlinks.py` 제공자 분기, 새 하위 단계 또는 `collect_index.py`, `serp_adapter.py`, `doctor.py` CAPABILITIES | M~L |
| GA4 날짜별 유기 세션 표(`ga4_daily`), 채널 그룹 분해, 고유 방문자(activeUsers), '속성에 행 0'과 '트래픽 0' 구분 | `collect_ga4.py` 부수 조회(363-379), `analysis.html` | S~M |
| 유료 단계 지출 상한(사이트·테넌트별 일·월) — 지금은 잔액 카나리아뿐. `runs.cost_estimate_usd` 합을 preflight에서 비교 | `run_all.py` preflight, `server/settings.py` | M |
| DataForSEO search_intent API로 우리 의도 휴리스틱 검증, Google Trends로 계절성, YouTube SERP로 영상 기회 | `collect_metrics.py`·새 하위 단계 | M |

### 지역·이커머스·다국어

| 무엇 | 착지점 ◇ | 크기 |
|---|---|---|
| 본문 `tel:` 전화·주소를 뽑아 마크업 telephone과 자동 대조 — 요청문 NAP 표 본문 줄을 코드가 채움 | `collect_page.py`, `scoring.nap_state`(1882), `brief._nap_lines`(1434) | M |
| Serper 경로에서도 지도 칸 감지(`places` → local_pack) | `serp_adapter.py:876` | S |
| 지도 칸 항목 저장 — 내 업체가 들었나·몇 위, 상위 업체 평점·리뷰 수 | `serp_adapter.py:667-690`, `rank_snapshots` | M |
| Maps SERP 한 점(소재 도시) + `my_business_info` 로 GBP 줄 채우기(키 없을 때만 사람 확인) | 새 선택 유료 수집, 30일 캐시 | M |
| 지도 임베드·매장 찾기 페이지 크롤 가능성 | 크롤 결과 | S |
| 이커머스: 쇼핑 칸 뜨는 검색어인데 Product+offers 없음, 오가닉 대 Shopping 노출 4분류, 제조사 설명 복붙 감지 | `scoring.page_advice`, serp_features_json | M |
| 다국어: 대체 판 본문이 서로 같음(미번역)·inLanguage 누락, 언어 판 사이 H2·이미지 수 동등성 | `collect_crawl.intl_issues` | S~M |
| 다국어 판 형식: 그 로케일에 맞는 날짜·숫자·통화 표기, 법적 고지(독일어권 Impressum 등 로케일별 필수 페이지), 번역 신선도(원문 수정 뒤 7/30/90일) | `collect_crawl.intl_issues`, 로케일별 규칙 표 | M |
| 지역 검색 유닛 안내 — EEA(DMA)·튀르키예·남아공처럼 SERP 구성이 다른 지역은 지도 칸·쇼핑 칸 해석에 주석 | `brief.py` SERP_FORMAT_HINT 옆 로케일 주석 | S |
| **리뷰·인용(로케일별 출처).** 리뷰 수·평점·최근성·사장 답글 비율, 주요 디렉터리 등재(미국 Yelp·BBB·Apple Maps·Bing Places, 영국 Trustpilot, 일본 食べログ, 한국 네이버 플레이스·카카오맵 등). 출처 표는 로케일 → 플랫폼 한 벌. 리뷰 게이팅 금지 경고 | 새 선택 유료 수집(DataForSEO business_data 리뷰), `scoring.nap_state`, `brief._nap_lines` | M~L |
| 무료 반경 경쟁 업체(OSM·Overpass·Geoapify) — OSM 데이터가 충실한 지역(유럽·북미)에서만, 품질 낮은 로케일은 끔 | 선택 하위 단계, 로케일 표 | M |
| 사이트 타입 추가(publisher·agency), 홈 신호로 업종 자동 추정 | `_presets.yaml` 대신 코드 상수(메모리 규칙) | S |

---

## 4. 일부러 뺐던 것 — 바꾸면 쓸 만한 것

뺀 이유는 하나다: seo-miner 원칙은 "실측 → 결정적 판정"인데, 이것들은 대부분 LLM 주관 점수·영어 전용·
근거 없는 고정 숫자였다. 그대로는 안 맞지만 seo-miner식(측정·결정적 규칙·요청문)으로 바꾸면 쓸 데가 있다.

| 항목 | 뺀 이유 | seo-miner식으로 바꾸면 | 크기 |
|---|---|---|---|
| 지오그리드 순위(격자점별 Maps SERP, SoLV) | 검색어당 49회 호출, 비쌈 | local_business 상위 3개 검색어만 3×3, 월 1회. 같은 도시 안에서도 구역마다 다른 지도 순위 | M |
| 클러스터 계획 산출물·cluster-map·작성 후 scorecard | 앞으로 지을 구조를 설계하는 도구 | 이미 있는 클러스터 + 크롤 링크로 클러스터별 작성 수·필러 연결률·상호 링크 집계, 내부 링크 지도 | S~M |
| E-E-A-T 가중 점수 | LLM 주관 점수, 지어낸 가중치 | 점수 버리고 결정적 신호만 센다: 저자 표기·저자 페이지·Person 스키마·보이는 수정일·소개/연락처. 주인·저자 엔티티 판정에 붙인다 | S |
| 페이지 유형별 최소 단어 수(홈 500·서비스 800·블로그 1,500) | 근거 없는 고정 숫자, 단어 단위가 언어마다 다름(CJK는 띄어쓰기로 못 셈) | 같은 로케일 상위 글 중앙값 대비('상위 5개 중앙값 1,800단어, 이 페이지 450단어'), `serp_outlines` 재료, 언어별 단위(단어·글자) | S |
| Flesch 가독성 | 공식이 영어 전용 | 언어별 가독성 한 벌: 영어는 Flesch-Kincaid, 독일어 Amstad, 스페인어 Fernández-Huerta 처럼 공식이 있는 언어는 그것, 없는 언어는 문장 길이·긴 문단 수만. 점수로 판정하지 말고 '상위 글 대비'로 | S~M |
| 영어 AI 투 표현 치환표·보이지 않는 유니코드 정리(content_humanize) | 영어 전용이라 뺐음 | 유니코드 정리는 3절에 이미 넣었다. AI 투 표현은 plays 초안 검사(치환 말고 '이런 표현 N개' 표시)로, 영어부터 언어별 목록 | S |
| seo-flow 프롬프트 메뉴(41개 중 38개가 같은 틀) | 측정 없는 프롬프트 | 쓸 만한 2개(conversion-audit, bofu-page-brief)만 요청문 꼴로 — 구매 결정 요인·반론·증거·CTA 마찰을 근거 표와 함께. `SHAPE_NAMES` 에 추가(이음매 12번) | M |
| 1차 고객 언어 입력(통화·채팅·후기) | 측정 데이터 아님 | 요청문에 선택 칸, 없으면 [확인 필요] | S |
| 페르소나·사용자 스토리·IST/SOLL 와이어프레임 | LLM 정성 산출물 | seo-miner가 계산하지 않고 요청문 '만들어 줄 것'에 현재→목표 섹션 틀을 맡김. 상위 글 형식 데이터로 채우게 | S |
| 사이트 합성 점수(SEO Health·GEO·SXO Gap·Backlink·Maps·Local·pSEO /100) | 가중치 날조, 기회 점수와 뜻 충돌 | 0~100 버리고 묶음(`GROUPS`)마다 나쁨·주의 개수와 지난 회차 대비 증감. Backlink Health의 '근거 요인 4/7 미만이면 점수 대신 데이터 부족' 게이트는 그대로 | S~M |
| 보고서 검사기(validate_backlink_report: 근거 부족한데 점수 못 내게, 'CC 미수록 ≠ 권위 낮음') | 보고서 파일 전용 | 요청문·화면 문구 단언 테스트로(test_brief·test_seams) | S |
| PDF/XLSX 보고서 | HTML 내보내기가 있음 | 리포트 HTML에 인쇄 CSS — 브라우저에서 PDF. 에이전시·고객 보고용 | S |
| Agent-UX 0~100(접근성 트리 휴리스틱) | Playwright 휴리스틱 | PSI accessibility 카테고리(결정적)로 대신 | S |
| 이미지 생성(Gemini·nanobanana, 용도별 비율 프리셋, 비용 장부) | 측정 범위 밖, 비용 | og:image 없는 페이지에 한해 create·plays 선택 단계로 PR에 | M~L |
| Common Crawl PageRank·harmonic centrality, Moz DA/PA | DataForSEO보다 부정확 | DataForSEO 키 없을 때만 폴백(지금은 단계가 통째로 빠짐) | M |
| Merchant API(Shopping·Amazon 가격 분포·셀러 점유) | 유료, 이커머스 전용 | commerce 사이트에서만, 그 로케일의 Shopping·Amazon 마켓(amazon.com·.de·.co.jp …)으로 상위 상품 가격 분포 대비 내 가격 | M |
| 업종 로드맵·콘텐츠 캘린더·3/6/12개월 KPI 표 | 측정 없는 계획 문서 | GSC 추세 + plays로 '다음 분기에 확인할 숫자'. 예측 말고 현재 값과 방향만 | M |
| 신규 사이트 계획 모드(URL·데이터 없음) | seo-miner는 측정된 사이트에서 출발 | 성격을 바꾸는 일 — 따로 결정 | L |
| PostHog 분석 출처 | 크기 L | GA4 대신 PostHog 를 쓰는 사이트(해외 SaaS에 흔함)용 선택 어댑터. 헤드리스라 MCP 말고 HogQL API 직접. Matomo(EU, GDPR 때문에 GA4를 피하는 사이트)도 같은 자리 | L |

---

## 5. 정보 한 줄로만 둘 것 (기회는 만들지 않는다)

| 항목 | 뺀 이유 | 바꾸면 | 크기 |
|---|---|---|---|
| WebMCP·ai-catalog.json(ARD)·Agentmap·/.well-known 5종(api-catalog RFC 9727, OAuth RFC 9728/8414, A2A agent-card, UCP)·Markdown 협상(Accept: text/markdown, .md 형제) | 초안 표준, 엔진이 쓴다는 근거 없음 | 크롤 때 GET 한 번씩 '있음·없음'만 기록. 표준이 자리 잡으면 판정 | S |
| RSL 1.0 라이선스 | 근거 얇음 | `license.xml` 존재만 | S |
| 이미지 IPTC 메타데이터(6절 검증에서 옮김) | 파일 가공으로 봤음 | Google Images 가 뽑는 Creator·Credit Line·Copyright Notice 유무만. commerce 사이트는 AI 생성 상품 이미지에 DigitalSourceType 이 필요하다는 Merchant Center 요구를 요청문 확인 항목으로 | S~M |
| 독성 링크 30패턴·PBN·disavow 초안 | 구글은 스팸 링크를 무시한다고 함, disavow 오용은 해로움 | spam score 분포만 보여 줌. disavow는 수동 조치 받은 경우만 언급 | S |
| 보안 헤더 등급(HSTS·CSP·X-Frame·nosniff·Referrer·Permissions-Policy)·쿠키 플래그 | 순위 신호 아님(claude-seo도 그렇게 적음) | HTTPS 강제·HSTS만 3절 호스트 정규화에 | S |
| 서버 스택 CVE 노출(X-Powered-By 등) | SEO 아님, 오탐 | 버전 드러나는 헤더가 있다는 정보만 | S |
| 뒤로가기 가로채기(history.pushState) 스팸 정책 | 정적 감지 오탐 많음 | 첫 로드 inline script의 pushState만 정보로. ⚠ 정책 확인 후 | S |
| 침입형 전면 광고·광고 밀도·터치 타깃·모바일/데스크톱 동등성(LLM 판독) | LLM 판단 | 렌더 경로가 생기면 Lighthouse 감사로 대체 | M |
| 문화권 적응 중 CTA 어조·현지 브랜드 대체 | LLM 판단 | 요청문(다국어 판 작업)에 확인 항목으로만. 결정적으로 볼 수 있는 형식·법적 고지·신선도는 3절로 옮겼다 | S |
| Shopify Crawler Access 서명 크롤(web-bot-auth, 430 대응) | Shopify 사이트에만 | Shopify 사용자가 생기면 | S |
| 4개 뷰포트 스크린샷·above-the-fold H1/CTA 시각 판단 | 모델 시각 판단 | PSI final-screenshot(3절)을 요청문에 붙이고 판단은 받는 AI에 | S |
| E-E-A-T와 별개로 'QRG 저품질 AI 글 징후(여러 쪽 반복 구조)' | LLM 판단 | 3절 근사 중복·템플릿 메타가 결정적 대체 | — |

---

## 6. 바꿔도 거의 안 맞는 것

2026-11-24 이 절의 외부 사실을 1차 출처로 검증했다(브라우저로 공식 문서 확인, 읽기만). 판정 칸:
**맞음**(근거대로 남김) · **고침**(근거가 달라 옮기거나 고침) · **판단**(외부 사실이 아니라 우리 판단이라 검증 대상 아님).

| 항목 | 이유 | 검증 |
|---|---|---|
| GBP Q&A 조회 | 읽을 길이 없다. My Business Q&A API 는 2025-11-03 종료됐고, 공개 Q&A 는 Maps 의 AI "Ask"(업체 답변·리뷰로 즉답)로 바뀌는 중이다. 도움말에는 "일부 카테고리·지역만" 남아 있다고 적혀 있다 | 맞음 — [Q&A API change log](https://developers.google.com/my-business/content/qanda/change-log)(2026-08-28 갱신), [GBP 커뮤니티 공지](https://support.google.com/business/thread/392024106)(2025-12-03), [도움말](https://support.google.com/business/answer/3039617). 기능 '완전 종료'라는 공식 문장은 없다 — '중단 중'이 정확하다 |
| Google Indexing API URL_UPDATED 제출 | 일반 페이지에 쓰면 지원 범위 밖 | 맞음 — "can only be used to crawl pages with either `JobPosting` or `BroadcastEvent` embedded in a `VideoObject`" ([quickstart](https://developers.google.com/search/apis/indexing-api/v3/quickstart), 2026-11-07 갱신) |
| 프롬프트 업스트림 받기 + SHA-256 잠금(sync_flow.py) | seo-miner에는 받아 올 외부 프롬프트가 없다 | 판단 — 잠금 구현은 로컬에서 확인(`scripts/sync_flow.py:43`·`:269`) |
| 리뷰 '10개·4.5점' 같은 고정 기준 | 업종·로케일마다 다르다. 리뷰 수집 자체는 3절(로케일별 출처)로 옮겼고, 기준은 같은 지도 칸 상위 업체 대비로 | 판단 |
| 데이터 집계사(Data Axle·Foursquare) 등록 권고 | 측정할 방법이 없는 권고. 요청문 확인 항목 한 줄 이상은 못 된다 | 판단 |
| Yahoo SERP | 미국·영국 등은 Bing 이 결과를 낸다("All algorithmic search results for Yahoo Search are provided by Microsoft Bing"). Yahoo! JAPAN 은 Google 엔진을 쓴다. 따로 잴 결과가 없다 — Bing 축(3절)과 Google 로 덮인다 | 맞음 — [Yahoo 도움말 SLN35619](https://uk.help.yahoo.com/kb/SLN35619.html), [LY Corporation](https://privacy.lycorp.co.jp/en/utilization) |
| Bing 계정 두 사이트 링크 갭 | API 는 내 계정에 추가·인증한 사이트 기준이고, claude-seo 스스로 "두 사이트를 다 관리할 때만, 임의 경쟁사는 DataForSEO·Moz·Common Crawl 로" 라고 적는다(`skills/seo-bing/SKILL.md:47-48`). Bing Webmaster **화면**에는 아무 사이트 백링크 비교(최대 3개, 상위 참조 도메인 500개)가 있지만 API 로는 못 받는다 | 맞음(부분) — [getting access](https://learn.microsoft.com/en-us/bingwebmaster/getting-access), [Bing 블로그 2023-09](https://blogs.bing.com/webmaster/2023/9/Bing-Webmaster-Tools-to-Remove-Disavow-Links-Feature-in-October-2023), [Backlinks 도움말](https://www.bing.com/webmasters/help/backlinks-c3a334e8). 미인증 사이트 호출이 거부된다는 명시 문장은 없다(호출해 보지 않음) |
| 기술 스택 감지·WHOIS·도메인 트래픽 일괄 추정·하위 도메인 | 우리 판정에 쓰는 자리가 없다 | 판단 |
| Google Cloud NLP 엔티티·감성, Knowledge Graph 조회, Web Risk | 판정으로 이어지는 자리가 없고 키가 하나 더 든다 | 판단 |
| Keyword Planner 직결 | DataForSEO search_volume 이 같은 출처(Google Ads API)를 이미 주고, `monthly_searches` 도 기본 12개월(최대 4년) 준다 | 맞음 — "We use Google Ads API as a data source" ([개요](https://docs.dataforseo.com/v3/keywords_data/google_ads/overview/), [search_volume](https://docs.dataforseo.com/v3/keywords_data/google_ads/search_volume/live/)). 문서에 'Keyword Planner' 라는 말은 없다 |
| ImageMagick 변환 레시피, IPTC/XMP **주입** | 측정이 아니라 파일 가공. create가 이미지를 만들게 되면 그때 | 판단 |
| 대화 중 부분 결과 파일 먼저 쓰기(findings/*.md, 턴 예산 대비) | 대화형 감사 전용 절차. 우리는 단계·DB가 그 역할 | 판단 |
| 자격 단계(tier 0/1/2)별 검사 고르기 | `doctor.py` CAPABILITIES 가 이미 한다 | 판단 |
| sitemap `priority`·`changefreq` 사용 지적 | 구글이 안 읽는다 — 지적할 가치가 없다. `lastmod` 는 "일관되고 검증 가능하게 정확할 때만" 쓴다(3절 lastmod 품질의 근거) | 맞음 — "Google ignores `<priority>` and `<changefreq>` values" ([build-sitemap](https://developers.google.com/search/docs/crawling-indexing/sitemaps/build-sitemap), 2026-07-08 갱신) |

### 검증하고 6절에서 뺀 것

| 항목 | 원래 이유 | 검증 결과 | 옮긴 곳 |
|---|---|---|---|
| 이미지 SERP 분석 | "claude-seo 스스로 전용 도구가 없다고 적음" | **틀렸다.** DataForSEO 에 `serp/google/images`(상위 100개, `title`·`alt`·`url`·`source_url`)가 있다. 도메인 칸은 없어 `url` 에서 뽑는다 ([문서](https://docs.dataforseo.com/v3/serp/google/images/live/advanced/)) | 아래 '이미지 SERP' — 이미지 칸이 뜨는 검색어에 한해 상위 이미지 출처 도메인·alt 꼴 대비 우리 이미지. 선택 유료, M |
| IPTC 메타데이터 | 파일 가공이라 측정 아님 | **일부 측정 가능.** Google Images 는 IPTC Creator·Credit Line·Copyright Notice 를 뽑아 보여 주고, DigitalSourceType(`trainedAlgorithmicMedia` 등)을 지원한다. Merchant Center 는 AI 생성 상품 이미지에 이 값을 요구한다. 단 "이 값이 'AI 생성' 라벨을 단다"는 공식 근거는 없다 — About this image 의 생성 정보는 C2PA 쪽이다 ([image-license-metadata](https://developers.google.com/search/docs/appearance/structured-data/image-license-metadata), [gen-AI content](https://developers.google.com/search/docs/fundamentals/using-gen-ai-content)) | 5절 정보 줄 — 이미지 IPTC Creator·Credit·Copyright 유무(바이트를 받는 이미지 진단 경로에서). commerce 사이트는 AI 이미지 DigitalSourceType 요구를 요청문 확인 항목으로 |
| 뉴스 사이트맵 규칙 | "뉴스 사이트가 대상이 아니다" | 사실은 맞다(`<news:news>` 태그 1,000개 한도, 최근 2일 기사만 — [news-sitemap](https://developers.google.com/search/docs/crawling-indexing/sitemaps/news-sitemap)). 하지만 대상이 전 세계이고 3절에서 publisher 타입을 제안했다 — **publisher 사이트에선 맞는다** | publisher 타입이 생기면 사이트맵 위생(3절)에 news 확장 규칙 추가. 이미지·영상 확장 사이트맵도 같은 자리, S |

### 검증하며 다른 절에서 고친 것

- **Bing 축(3절)**: Copilot 은 Bing 검색 서비스로 질의를 보낸다(Microsoft Learn 2026-10-29). ChatGPT 검색은 OpenAI 도움말이
  "sometimes partners with other search providers"(Microsoft 링크)라고만 한다 — Bing 단독 의존은 1차 출처가 없다. OpenAI 가
  명시한 포함 조건은 OAI-SearchBot 허용이다. 그래서 "Copilot 은 확실, ChatGPT 는 도움이 될 수 있음"으로 쓴다.
- **Bing API 경로**: Bing Webmaster 문서에 "Legacy SOAP and POX APIs will be retired on August 31, 2026" 공지가 있다. 이미
  지났다 — 붙인다면 처음부터 지금 API 형태(JSON)로, 붙이기 전에 엔드포인트를 다시 확인한다.

---

## 7. 두 도구가 겹쳐서 생기는 문제

1. **doctor 위임 오판** — 1절 6번.
2. **반대 처방** — FAQPage를 claude-seo는 '권하지 마라', 우리 요청문은 '넣어라'. 2절 1번이 푼다.
3. **0~100 점수 두 개** — claude-seo는 사이트 건강, 우리는 기회 하나의 값어치. 이름이 비슷하고 뜻이 다르다.
4. **같은 API, 다른 숫자** — quick_wins(4~10위·노출 50 초과) 대 striking(4~20위). GSC 합계는 claude-seo가
   차원 없는 질의(`totals_complete`), 우리는 상위 N행 합(1절 3번). claude-seo 기본 로케일은 US·en이라
   미국 밖 사이트를 물으면 사이트 로케일과 다른 SERP를 준다 — 우리는 사이트 로케일로 잰다.
5. **비영어 거짓 음성** — `metadata_template.py` 는 비라틴 문자를 지운 뒤 비교하고, CTA·Flesch·AI 투 치환표는
   영어 전용이다. 영어 사이트에선 동작하지만 한국어·일본어·중국어 등에선 '템플릿 메타 없음'이 실제 검사 없이 나온다.
6. **저장소·비용 장부 두 벌** — drift 기준선 `~/.cache/claude-seo/drift/baselines.db` 대 brain.db,
   DataForSEO 일일 장부 `dataforseo-ledger.json` 대 `runs.cost_estimate_usd`. 같은 키를 쓰면 서로의 지출을 모른다.
7. **트리거 충돌** — 'SEO 감사'·'AI 인용'·'llms.txt'·'스키마'·'pSEO'에 claude-seo, 위임 대상 팩(coreyhaines31),
   seo-miner capture가 함께 걸린다. `seo-audit` 는 두 팩에 같은 이름. capture SKILL.md 위임 절에 어느 팩을
   가리키는지 한 줄.
8. **쓰기 동작은 claude-seo에만** — IndexNow 제출, Indexing API, JSON-LD 생성. 사용자가 claude-seo로 제출·수정하면
   우리 관찰 중 상태가 그 변화를 모르고 원인을 다른 데로 돌릴 수 있다.
9. **PostToolUse 훅** — claude-seo는 모든 Edit/Write 뒤에 `validate-schema.py` 를 돌린다(최대 30초). seo-miner
   작업에도 걸린다.

---

## 8. 영역별 대조표

(s)=스킬, (a)=에이전트.

| 영역 | claude-seo | seo-miner 대응 | 겹침 | 핵심 차이 |
|---|---|---|---|---|
| 허브·종합 감사 | seo(s), seo-audit(s) | `skills/capture/SKILL.md`, `run_all.py` STAGES·GROUPS, `doctor.py` | 부분 | 하위 에이전트 라우터 + 사이트 건강 점수 대 누적 수집 + 기회 점수. 구글 업데이트 원장은 저쪽만 |
| 전략·프롬프트 | seo-plan(s), seo-flow(s·a) | `_presets.yaml`, `references/external-skills.md`, `brief.py` SHAPES | 부분 | 둘 다 측정 없는 메뉴 대 같은 원칙(근거 없는 숫자 금지·[확인 필요])을 실측과 함께 코드로 강제 |
| 기술·크롤 | seo-technical(s·a), seo-shopify(s) | `collect_crawl.py`, `collect_index.py`, `collect_page.py` js_shell | 부분~강함 | 한 호스트 깊게(TLS·헤더·렌더 diff·물러서기) 대 전수 크롤 회차 비교·GSC 색인 실측·다국어 |
| 사이트맵 | seo-sitemap(s·a) | `collect_crawl.py` _sitemap_urls·discover_seeds, `db.py` sitemap_urls | 부분 | 파일 위생 대 크롤 시드·orphan 근거 |
| 온페이지 | seo-page(s) | `collect_page.py` audit_html, `scoring.page_advice` | 강함 | HTML 위생 넓게 대 GSC 검색어 맥락·CJK 폭·스키마 속성 깊게 |
| 콘텐츠·브리프 | seo-content(s·a), seo-content-brief(s) | `page_advice`·`extract_advice`, `brief.py`, `plays.py` | 부분~강함 | LLM 주관 점수 대 저장된 SERP 개요·셀 수 있는 형식 비교, 저자·자격 날조 금지 |
| 구조화 데이터 | seo-schema(s·a) | SCHEMA_RULES, `_schema_scan`, `schema_vocab.py` | 부분 | 넓게(Microdata·폐기 유형·값 형식·생성) 대 깊게(@id 병합·중첩 면제·주인/저자 엔티티·NAP) |
| AI 검색 | seo-geo(s·a), seo-agentic(s·a) | `skill_config` ai_bots, `ai_bot_status`, `probe_llms_txt`, `extract_advice`, `collect_ai.py` | geo 강함·agentic 부분 | 휴리스틱 점수 대 엔진별 답변·AI 요약 인용 실측. agentic은 '조작 가능성'이라는 다른 축 |
| 구글 API·속도 | seo-google(s·a), seo-performance(a) | `collect_gsc`·`collect_index`·`collect_vitals`·`collect_ga4`·`gsc_query` | 부분~강함 | API 폭(CrUX History·NLP·진단 15종) 대 같은 핵심 API를 누적·출처 구분 |
| 백링크·빙 | seo-backlinks(s·a), seo-bing(s) | `collect_backlinks.py`, `scoring.backlink_gaps`, `backlinks.html` | 부분(빙 없음) | 여러 출처 건강 점수·독성 대 DataForSEO 누적 → 할 일 3종·자동 해소 |
| DataForSEO | seo-dataforseo(s·a) | `serp_adapter.post_dataforseo` 외 | 부분 | MCP 약 40개 넓고 얕게, 기본 US·en 대 고정 엔드포인트 REST, 로케일 매핑, 잔액 카나리아, 실청구액 |
| 클러스터·비교·pSEO | seo-cluster(s·a), seo-competitor-pages(s), seo-programmatic(s) | `keywords.cluster`, `_group_members`, coverage, pseo_groups, `brief.py` template | 부분 | 앞으로 지을 구조 설계 대 GSC 수요에서 기회 → PR |
| SXO | seo-sxo(s·a) | serp_outlines, `brief._format_lines`, SERP_FORMAT_HINT, classify_intent | 부분 | LLM 페이지 종류 분류·페르소나 대 형식 수치 비교, 페이지 종류는 요청문 빈칸 |
| 이미지·시각 | seo-images(s), seo-image-gen(s·a), seo-visual(a) | images_no_alt, `image_weight_advice`, IMAGE_AUDITS | 부분 | 정적 속성·포맷 탐침·생성·스크린샷 대 Lighthouse 실측 용량·전수 alt |
| 지역 | seo-local(s·a), seo-maps(s·a) | `brief._local_pack`·`_nap_lines`, `nap_state`, SCHEMA_RULES LocalBusiness | 부분 | GBP·리뷰·인용·지오그리드 대 지도 칸 검색어에만 NAP 대조, GBP는 사람 확인 |
| 다국어 | seo-hreflang(s) | `collect_crawl.intl_issues`, `_hreflang_advice` | 강함 | 코드 실명단·전달 방식·문화권 대 전수 상호 참조·새는 링크·회차 비교 |
| 이커머스 | seo-ecommerce(s·a) | SCHEMA_RULES["Product"], commerce 프리셋 | 부분 | Merchant API·머천트 정책 필드 대 범용 스키마 규칙 |
| 변화 감시 | seo-drift(s·a) | crawl_compare, page_audits·crawl_pages·page_vitals 누적, watch_rows | 부분 | URL 하나 필드 값 대 기준선 대 이슈 집합 차이·순위 전후. 원재료는 우리가 더 넓다 |
| 분석 | seo-posthog(s·a) | `collect_ga4.py` | 부분 | PostHog 즉석 HogQL 대 GA4 누적·GSC 결합·전환·AI 유입 |

**seo-miner가 더 나은 것**: 누적과 회차 비교(brain.db, crawl_runs 신규·해결), 결정적 판정과 테스트(기회 종류·
selfcheck·test_seams), AI 가시성 실측(엔진별 질문×샘플, aio_exposure, presence, GA4 AI 유입), 검색어 맥락 판정,
없음과 모름의 구분(llms.txt 세 상태·js_shell 보류·schema_unknown·403/429를 결함으로 안 적음), 로케일 대응
(사이트 로케일로 SERP·지표를 재고, CJK 폭 기준 길이 판정), 비용 통제(잔액 카나리아·dry-run·실청구액·캐시·로케일), 실행 루프(심사→요청문→plays→PR·create→
자동 해소→관찰 중).

---

## 원자료

대조 원자료(항목별 결과 49개, 제안 136개, claude-seo에만 있는 것 목록)는 세션 스크래치에 있다 — 지워질 수 있다.
다시 뽑으려면 claude-seo 설치 캐시(`~/.claude/plugins/cache/agricidaniel-claude-seo/claude-seo/<버전>/`)의
`skills/*/SKILL.md`·`agents/*.md` 를 같은 방식으로 대조한다.
