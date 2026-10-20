function renderDates(d) {
  siteAv();
  const sel = $("gscdate"), dates = d.gsc_dates || [];
  // 박제본에는 서버가 없어 되돌릴 수 없고, 고를 게 하나뿐이면 선택지가 아니다.
  sel.hidden = READONLY || dates.length < 2;
  if ($("datelab")) $("datelab").hidden = sel.hidden;   // 이름표는 고를 게 있을 때만
  if (!sel.hidden) {
    sel.innerHTML = dates.map(x =>
      `<option value="${esc(x.date)}">${esc(x.date)}` +
      `${x.period ? ` · ${x.period}일치` : ""}</option>`).join("");
    sel.value = d.gsc_date || "";
  }
  // 최신이 아닌 날을 보고 있으면 눈에 띄게 한다 — 최신인 줄 알고 읽으면 오독이다.
  const past = !!(dates.length && d.gsc_date && d.gsc_date !== dates[0].date);
  sel.classList.toggle("past", past);

  // 셀렉트가 떠 있으면(고를 게 2개 이상) 그 옵션 글자가 이미 "날짜 · N일치"를
  // 말하고 있다 — 바로 아래 이 줄이 같은 날짜를 또 적으면 "2026-06-01 · 28일치"
  // 다음 줄에 "구글 실적 2026-06-01 (28일치)"가 되풀이된다. 셀렉트가 없을
  // 때만(수집이 하나뿐이거나 없음·박제본) 이 줄이 그 정보를 대신 말한다.
  // 서치콘솔 없이 시작한 사이트는 "아직 없음"이 아니다 — 기다려도 안 온다.
  const dateInfo = !d.project.gsc_property ? " · 서치콘솔 연결 안 함"
    : sel.hidden
    ? ` · 구글 실적 ${d.gsc_date || "아직 없음"}${d.gsc_period ? ` (${d.gsc_period}일치)` : ""}`
    : "";
  $("meta").textContent = (d.project.gsc_property || d.project.domain || "") + dateInfo;
  // 종이 표지 — 화면에서는 안 보이고 인쇄에만 선다. 값은 위 머리말과 같은 것이다.
  const ph = $("printhead");
  if (ph) {
    const when = window.__SNAPSHOT__ ? `${window.__SNAPSHOT__.exported} 기록` : "";
    ph.innerHTML = `<div class="t">${esc(d.project.name)}: 검색·AI 가시성 보고서</div>` +
      `<div class="m">${esc($("meta").textContent)}${when ? " · " + esc(when) : ""}</div>`;
  }

  // 날짜만 늘어놓으면 그게 무엇의 날짜인지 안 읽힌다("AI 인용 2026-09-03" 은
  // 인용이 그날 있었다는 말처럼 보였다). 이름표 하나로 무엇의 날짜인지 못 박고,
  // 축마다 배지 하나씩 세운다 — 이름은 본문 서체, 날짜만 등폭이다.
  const ax = AXES.filter(([, key]) => d[key])
                 .map(([name, key]) =>
                   `<span class="ax">${esc(name)}<em>${esc(d[key])}</em></span>`);
  $("axes").innerHTML = (past
    ? "<b>과거 시점을 보고 있습니다. 되돌린 것은 구글 실적뿐이고 아래 날짜는 최신입니다.</b>"
    : "") + (ax.length ? `<span class="k">마지막 수집</span>` + ax.join("") : "");
  $("axes").hidden = !ax.length && !past;
  // 수집일 알약 줄은 화면 머리에서 걷었다(CSS: .hax:not(:has(b))). 정보는 레일 상태 카드가 갖는다 —
  // 마우스를 올리면 축별 마지막 수집일이 나온다.
  const axTxt = AXES.filter(([, key]) => d[key]).map(([name, key]) => `${name} ${d[key]}`).join(" · ");
  $("meta").title = axTxt ? `마지막 수집: ${axTxt}` : "";
}

// 첫 render() 가 자리표를 내리고 본문을 연다 — 이후 재렌더(SM.repaint 등)는
// 이미 열려 있으니 조용한 no-op 이다.
function bootDone() {
  const b = $("sm-boot");
  if (b) b.hidden = true;
  $("content").hidden = false;
}

let LAST_D = null;          // 마지막으로 그린 페이로드 — SM.repaint() 가 다시 쓴다
function render(d) {
  LAST_D = d;
  bootDone();
  if (d.schema == null) {
    // schema 필드가 없는 옛 박제본 호환 분기: 박제본 호환 분기는 schema 번호 하나로 한다.
    // 옛 박제본에 없는 규칙·필드를 여기서 한 번만 채워 하위 렌더러의 임시 분기들을 대체한다.
    if (!d.rules) {
      RULES = {page1: 10, striking_lo: 4, striking_hi: 20, rank_noise: 3,
               device_gap_pos: 2, device_min_imp: 50};
    }
    if (d.trend && d.gsc_period) {
      d.trend = d.trend.filter(x => x.p == null || x.p === d.gsc_period);
    }
    if (d.striking) {
      for (const r of d.striking) {
        if (r.gap == null && r.pos != null) r.gap = Math.max(0, r.pos - (RULES.page1 || 10));
        if (r.band == null && r.pos != null) r.band = r.pos <= (RULES.page1 || 10) ? "page1" : "page2";
      }
      if (d.striking_page2 == null) {
        d.striking_page2 = d.striking.filter(r => r.band === "page2").length;
      }
    }
    // d.opps 는 손보지 않는다. is_defensive 는 isDefensive(o) 가 필요할 때마다
    // o.is_defensive ?? 4종 목록으로 풀어내고(예전엔 이 자리가 2종만 아는 사본이었다),
    // 종류별 개수는 화면이 지금 보고 있는 상태 묶음에서 직접 센다. label·play 는 옛
    // 박제본이면 비어 있고, playList()·oppWhy() 가 빈 처방·kind 원문으로 받아들인다.
    if (d.daily && !d.daily_stats) {
      d.daily_stats = {
        clicks_max: Math.max(...d.daily.map(r => r.clicks || 0), 1),
        impressions_max: Math.max(...d.daily.map(r => r.impressions || 0), 1),
        clicks_sum: d.daily.reduce((a, r) => a + (r.clicks || 0), 0),
        impressions_sum: d.daily.reduce((a, r) => a + (r.impressions || 0), 0),
      };
    }
    if (!d.ai_trend) d.ai_trend = [];
  }

  // KPI의 증감도 같은 기간끼리만 — 서버가 고른 비교 짝(gsc_date/gsc_prev)을 그대로 쓴다.
  // 그냥 마지막 두 줄을 빼면 28일치에서 90일치를 빼는 일이 생긴다.
  if (d.rules) RULES = {...d.rules};  // 서버가 준 경계값이 이 화면의 유일한 기준
  renderDates(d);
  // 묶음 — 메뉴의 제목·점과 각 화면 머리줄. 정본은 run_all.GROUPS(서버가 싣는다).
  SM.setGroups(grpDue(d.groups));
  SM.modeCountsFrom(d);    // 화면 안 탭 배지 — view-def modes[].count 경로
  groupHead(d);
  applyWording();          // 정적 문구는 여기서 한 번 (동적 문구는 각자 W() 로 고른다)

  // 기회 한 벌은 화면 전부가 같이 본다 — 화면을 그리기 전에 대상 색인을 세운다.
  // 예전에는 개요의 VIEW 안에서 세웠고, 그래서 개요보다 먼저 그려지는 화면은
  // 같은 페이로드를 손에 쥐고도 빈손이었다.
  oppIndex(d.opps);
  window.SD_PERIOD = d.gsc_period ? `${d.gsc_period}일` : "";
  window.QUERY_PAGES = d.query_pages || {};
  window.TARGET_TREND = d.target_trend || {};  // 대상별 회차 추이 (펼침 패널의 숫자·차트)
  window.KW_COUNTRIES = d.kw_countries || {};  // 검색어 → 나라별 노출 상위 (펼침 패널의 나라별)
  window.PAGE_QUERIES = d.page_queries || {};  // 페이지별 들어오는 검색어 (연관 키워드)
  window.BRIEF = d.brief || null;            // 요청문 꼬리·머리말 한 벌 (brief.py)
  window.PAGE_AUDITS = d.page_audits || {};
  window.CREATIONS = d.creations || [];      // 기회 펼침 패널이 "이 기회로 만든 것"을 센다
  window.KIND_LABELS = d.kind_labels || {};  // kind → 통칭 라벨. [기록]·필터 칩처럼
                                              // 대상 없이 kind 만 아는 자리가 쓴다.
  window.KIND_VIEWS = d.kind_views || {};    // kind → [화면 id, 섹션 id] (정본 scoring.KINDS 의 see)

  // 처리 큐는 뷰보다 먼저 데이터를 받는다 — [개요]의 띠처럼 큐 상태를 읽는 뷰가 있다.
  SM.queue.feed(d);
  for (const v of SM.views) if (v.modes.length) SM.modeApply(v.id);   // 큐 카드는 feed 가 세우므로 접기를 한 번 더(공짜)

  // 화면 하나가 던져도 나머지는 그려져야 한다 — 예전에는 조립 순서상 뒤에 오는
  // 화면이 통째로 비었고, 왜 비었는지는 아무 데도 안 남았다.
  for (const v of VIEW_RENDER) {
    // 못 그린 화면은 콘솔에만 남기면 사용자에게는 빈 화면이다 — 이유 한 줄을 화면 머리 아래에 둔다.
    // 다음 render() 가 성공하면 이 줄은 처음에 걷힌다.
    const box = v && v.id ? $("view-" + v.id) : null;
    if (box) box.querySelectorAll(":scope > [data-render-err]").forEach(x => x.remove());
    try { (v.render || v)(d); }
    catch (e) {
      console.error(`화면 [${(v && v.id) || "?"}] 을 그리지 못했습니다`, e);
      if (box) {
        const vh = box.querySelector(":scope > .vh");
        (vh || box).insertAdjacentHTML(vh ? "afterend" : "afterbegin",
          `<p class="msg bad" role="alert" data-render-err>이 화면을 그리지 못했습니다. ` +
          `[화면 다시 불러오기]를 누르면 다시 그립니다.</p>`);
      }
    }
  }
  // 뷰가 방금 그린 자리에서도 박제본의 거짓말을 뗀다 — 뷰마다 READONLY 를 물어보게
  // 하면 열한 곳이 각자 기억해야 하고, 그중 몇은 언젠가 잊는다(실제로 잊었다).
  deadSweep($("content"));
  tableFade($("content"));      // 방금 생긴 표가 넘치는지는 그린 다음에야 안다
  for (const v of SM.views) nxDedupe(v.id);   // 본문이 다 선 뒤에야 "같은 버튼이 본문에 있나"를 안다
  nxPad();
  banSync();                    // 데이터가 생겼으면 진단 배너를 한 줄로 줄인다
}

// 서버 재요청 없이 다시 그린다 — 화면 렌더는 늘 페이로드 하나의 함수다. 호스팅
// 애드온이 자기만 아는 상태(예: 저장소를 막 연결했다)가 바뀌어서 이미 그려진
// 카드를 갱신해야 할 때 쓴다. **DOM 을 되짚어 고치는 대신** 같은 페이로드로 render()
// 를 다시 부른다 — oppActs() 등 렌더 함수가 SM.host 를 다시 물어 새로 그린다.
SM.repaint = () => { if (LAST_D) render(LAST_D); };

