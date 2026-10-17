/* ── 대상 하나의 지금 수와 추이 ─────────────────────────────────────
   "몇 위인가"와 "올라가는 중인가"는 다른 질문인데, 패널은 오래 첫째에만
   답했다. 둘 다 서버가 같은 스냅샷에서 센 것(d.target_trend)을 그린다 —
   화면이 표를 다시 더하면 표와 차트가 다른 수를 말한다. */
SM.api.targetStat = t => {
  const rows = (window.TARGET_TREND || {})[t] || [];
  if (!rows.length) return "";
  const r = rows[rows.length - 1];                    // 마지막 = 기준 수집일
  const ctr = r.imp ? (r.clk / r.imp * 100) : 0;
  const nums = `<p class="detk"><span class="num">${num(r.imp)}</span> 노출 ·
    <span class="num">${num(r.clk)}</span> 클릭 ·
    <span class="num">${ctr.toFixed(1)}%</span> CTR`
    + (r.pos == null ? "" : ` · 평균 <span class="num">${r.pos}</span>위`) + `</p>`;
  // 순위는 작을수록 좋다 — 축을 뒤집지 않으면 9위로 올라간 선이 아래로 꺾여
  // "떨어졌다"로 읽힌다. 회차가 하나뿐이면 chLine 이 빈 문자열을 준다(점 하나를
  // 선이라고 부르지 않는다) — 그때는 숫자 석 장만 남는다.
  const pts = rows.filter(x => x.pos != null).map(x => ({x: x.d.slice(5), y: x.pos}));
  return `<div class="detw">${nums}${pts.length > 1
    ? `<p class="detn">순위 추이 — 위로 갈수록 좋은 순위입니다.</p>`
      + window.chLine(pts, {unit: "위", height: 132, reverse: true})
    : ""}</div>`;
};

// 이 검색어가 어느 나라 구글에서 보였나 — 서버가 센 d.kw_countries(서치콘솔 나라 분해) 그대로.
// 평균 순위는 나라를 섞은 값이라, 필리핀 6.5위·미국 8.2위가 한 숫자 7위로 보였다(2026-10-14).
SM.api.relCountries = q => {
  const rows = (window.KW_COUNTRIES || {})[q];
  if (!rows || !rows.length) return "";
  return `<div class="detw"><b>나라별 노출</b>
   <span class="detn">이 검색어가 어느 나라 구글에서 보였는지입니다. 순위는 나라마다의 서치콘솔 평균입니다.</span>
   <ul class="detl">${rows.map(x => `<li>${esc(x.name)} <span class="num">${num(x.imp)}</span> 노출
     · ${x.pos == null ? "순위 모름" : `평균 <span class="num">${x.pos}</span>위`}</li>`).join("")}</ul></div>`;
};

// 이 검색어로 걸린 내 지면 — 다섯 열 표는 390px 에서 넘쳐 상자 밖으로 샜다.
// 순위는 서치콘솔의 기간 평균이다(나라·기기·날짜를 섞은 노출 가중 평균). "4.5위"로만 적으니
// 직접 검색해 본 사람이 "실제로는 4.5위가 아닌데"라고 읽었다(theotherskin 'olidia vs
// sculptra' — 미국 구글 직접 확인은 1쪽 밖, 2026-10-14). 평균이라고 적는다.
SM.api.relPages = (pages, cur) => !pages || !pages.length ? "" :
  `<div class="detw"><b>연관 페이지 ${pages.length}</b>
   <span class="detn">이 검색어로 순위에 걸린 내 지면입니다. 순위는 서치콘솔 평균이라 나라·기기마다 섞인 값이고, 직접 검색한 순위와 다를 수 있습니다.</span>
   <ul class="detl">${pages.map(r => `<li><a href="${esc(r.page)}" target="_blank"
     rel="noopener">${esc(r.page)}</a> <span class="num">${num(r.impressions)}</span> 노출
     · 평균 <span class="num">${r.position ?? "—"}</span>위${r.page === cur
       ? ` <span class="micro lat">·</span> 고칠 페이지` : ""}</li>`).join("")}</ul></div>`;

// 이 페이지로 들어오는 검색어 — 정본은 서버가 센 d.page_queries 다. query_pages 를
// 뒤집어 세면 기회에 걸린 것만 세고 "연관 검색어 3개"라고 자신 있게 말한다.
SM.api.relQueries = url => {
  const g = url ? (window.PAGE_QUERIES || {})[url] : null;
  if (!g || !g.total) return "";
  const more = g.total - g.top.length;
  return `<div class="detw"><b>연관 키워드 ${num(g.total)}</b>
    <span class="detn">이 페이지로 들어오는 검색어입니다.</span>
    <ul class="detl">${g.top.map(x => `<li>${esc(x.q)}
      <span class="num">${num(x.imp)}</span> 노출
      · 평균 <span class="num">${x.pos ?? "—"}</span>위</li>`).join("")}</ul>${more > 0
    ? `<p class="detn">외 ${num(more)}개</p>` : ""}</div>`;
};

SM.api.sdAvgNote = () => `이 순위는 구글이 보고한 ${window.SD_PERIOD || "기간"} 평균 게재순위입니다.`
  + ` 노출된 순간들의 평균이라 지금 직접 검색하면 안 보일 수 있습니다.`
  + ` 실제로 검색해서 잰 순위는 ${window.go("rank")} 화면에 있습니다.`;
/* ── 펼침 패널: 무엇이 걸렸고, 어느 페이지 얘기고, 그래서 뭘 하면 되나 ──
      근거 문장은 숫자를 요약할 뿐이라 "그래서 어디를 고치냐"에 답하지 못했다.
      표(어느 URL 이 이 검색어로 걸려 있나)는 payload 의 opp_pages 가 대고,
      할 일은 o.play 가 댄다. ── */
// 펼침 패널의 조각들 — 제자리 펼침(oppDetail)과 [개요]의 오른쪽 서랍(OV_drawer)이 같은
// 조각을 다른 순서·자리에 놓는다. 조각을 두 벌 만들면 두 자리가 다른 말을 한다.
SM.api.oppParts = o => {
  const pages = (window.QUERY_PAGES || {})[o.target] || [];
  const p = o.play || {};
  // striking_distance 의 "무엇을" 뒤에는 평균 게재순위 안내 + [순위] 화면 버튼이
  // 이어 붙는다 — 그 버튼(window.go)은 화면에서만 만들 수 있어 서버 문구에는
  // 없다(scoring._SD_PLAY 주석 참고). 여기서만 이어 붙인다.
  const sdTail = o.kind === "striking_distance" && p.what
    ? ` ${window.sdAvgNote()}` : "";
  // 이 기회에서 실제로 손댈 페이지 — 서버가 요청문과 같은 규칙(brief.page_of)으로
  // 고른 것. 순위에 걸린 페이지가 없어도 제목·H1 로 찾은 지면일 수 있다. 여기서
  // 따로 고르면 요청문과 다른 말을 한다. brief.page 가 없는 옛 박제본만 표에서 고른다.
  // 감사 결과가 있으면 "지금 title 이 무엇이고 무엇을 바꿔라"까지 나온다.
  const url = o.target.startsWith("http") ? o.target
    : o.brief && "page" in o.brief ? o.brief.page : (pages[0] && pages[0].page);
  const audit = url ? (window.PAGE_AUDITS || {})[url] : null;
  // 못 찾았을 때 왜 없는지는 대상이 무엇이냐에 달렸다 — 챗봇 질문은 문장이라
  // 애초에 GSC 검색어 목록에 없고, 도메인·클러스터는 페이지 개념이 아니다.
  // 한 문장으로 뭉치면 "대상이 URL 이거나…"처럼 틀린 이유를 대게 된다.
  const NOTE = {
    ai_citation_gap: "이 질문으로 걸린 내 페이지가 아직 없습니다. 챗봇 질문은 문장이라 "
      + "구글 검색어 목록에 그대로 잡히는 일이 드뭅니다. 아래 할 일은 새로 쓸 글 기준입니다.",
    backlink_prospect: "이 대상은 남의 도메인입니다. 고칠 내 페이지가 아니라 연락할 곳입니다.",
    backlink_mention: "이 대상은 우리 이름을 쓴 남의 도메인입니다. 고칠 내 페이지가 아니라 연락할 곳입니다.",
    coverage: "이 대상은 주제 묶음입니다. 제목·H1 에 이 주제가 든 내 지면을 찾지 못했습니다 — "
      + "사이트에 아예 없다는 뜻은 아니니, 새로 쓰기 전에 사이트에서 먼저 찾아봅니다.",
  };
  // 주제 묶음은 검색어가 아니라 키워드 여럿이다 — 문장의 주어를 맞춘다.
  const subj = o.kind === "coverage" ? "이 주제로 묶인 키워드" : "이 검색어";
  // 고르지 못한 후보(o.brief.candidates)는 요청문 '대상'이 싣는 목록 그대로다.
  const cands = (o.brief && o.brief.candidates) || [];
  const note = pages.length || o.target.startsWith("http") ? ""
    : url ? `<p class="detn">${subj}로 순위에 걸린 페이지는 없습니다. 제목·H1 이 `
      + `${o.kind === "coverage" ? "이 주제로" : "이 검색어로"} 시작하는 내 지면이 있어, 새로 쓰지 않고 그 지면을 고칩니다.</p>`
    : cands.length ? `<div class="detw"><p class="detn">${subj}로 순위에 걸린 페이지는 `
      + `없지만, 제목·H1 에 ${o.kind === "coverage" ? "이 주제" : "이 검색어"}가 있는 내 지면이 ${cands.length}개 있습니다. 이 중 `
      + `하나가 ${o.kind === "coverage" ? "이 주제" : "이 검색어"}를 맡으면 새로 쓰지 말고 그 지면을 고칩니다.</p>`
      + `<ul class="detl">${cands.map(c => `<li><a href="${esc(c.page)}" target="_blank" `
      + `rel="noopener">${esc(c.page)}</a>${c.title ? ` · ${esc(c.title)}` : ""}</li>`).join("")}</ul></div>`
    : `<p class="detn">${NOTE[o.kind] || "이 검색어로 걸린 페이지를 아직 모으지 않았습니다. "
      + "페이지별 실적을 수집하면 어느 페이지가 걸렸는지 여기에 나옵니다."}</p>`;
  // 요청문은 서버가 이 기회의 근거 표·감사까지 모아 써 놓았다(o.brief) — 여기서는
  // 꼬리만 잇는다. 옛 박제본(o.brief 없음)은 askBlock 이 그렇다고 말한다.
  // 할 일인 기회는 복사하면서 진행 중으로 옮긴다(묶인 줄이면 묶인 id 전부).
  const ask = askBlock({brief: o.brief,
                        ack: o.status === "new" && !READONLY && o.id != null ? oppIdsTo(o, "acked") : []});
  // 이 기회로 만든 것(creations) — pSEO·콘텐츠 갭처럼 글 여러 편이 필요한 기회는
  // 진행 중에 머물며 여기 쌓이고, 완료는 사람이 누른다. 세는 규칙은 madeFor 한 벌이다
  // (접힌 줄의 배지와 같은 것을 세야 한다 — 여기만 대표 id 를 보면 묶인 줄에서
  // 배지는 '적용 3건'인데 펼치면 1건만 나온다).
  const made = window.madeFor(o);
  const works = made.length ? `<div class="detw"><b>이 기회로 만든 것 ${made.length}건</b>
    <ul class="detl">${made.map(c => `<li><span class="num">${esc(c.file_path)}</span>` +
      `${c.branch ? ` · ${esc(c.branch)}` : ""} · ${c.merged ? "머지됨" : "PR 열림"}` +
      `${c.note && /^https?:/.test(c.note) ? ` · <a href="${esc(c.note)}" target="_blank" rel="noopener">보기</a>` : ""}</li>`).join("")}</ul></div>` : "";
  // 패널 **맨 위**가 이 기회의 다음 행동이다: 요청문 만들기(호스팅만 — 로컬은 접힌 줄의
  // "도구로 열기"가 그 자리다)가 주요고, 상태 바꾸기는
  // 보조다. 그 사이에 지금 상태 칩(oppNow)이 서서 "무엇으로 바꾸나"만 말하는 버튼들
  // 앞에 "지금 무엇인가"를 댄다. extra 는 그 화면만 아는 손잡이(실행 버튼·다른 화면
  // 으로)를 같은 줄에 세우는 자리다 — 화면이 패널 뒤에 제 상자를 또 달면 버튼 줄이
  // 둘로 갈라진다.
  // 수(지금 몇 위고 어디로 가고 있나)가 맨 위다 — 처방과 페이지보다 먼저 온다.
  // 연관 페이지·연관 키워드는 맨 아래다: 이 기회를 끝낸 다음에 가 볼 곳이다.
  return {ask, url, note, works,
    stat: window.targetStat(o.target) + window.relCountries(o.target), group: oppGroup(o),
    play: playList(p.what, p.acts, sdTail),
    page: url ? `<p class="detw">고칠 페이지: <a href="${esc(url)}" target="_blank"
      rel="noopener">${esc(url)}</a></p>${pageFix(audit, url)}` : "",
    rel: window.relPages(pages, url) + window.relQueries(url)};
};
function oppDetail(o, extra) {
  const P = window.oppParts(o);
  return `<div class="det"><div class="det-top">${P.ask}${oppNow(o)}${oppRest(o)}${extra || ""}</div>
    ${P.stat}${P.group}${P.play}${P.note}
    ${P.page}
    ${P.rel}${P.works}</div>`;
}

/* 묶인 줄의 변형 목록 — 어느 검색어가 한 줄이 됐고 **왜** 한 줄인지. 열쇠(via)는
   서버가 정한다(scoring.GROUP_VIA 가 정본, 여기 키가 그것과 같은지는 test_seams 23).
   화면은 그 열쇠를 말로 옮기기만 한다 — 다시 판정하지 않는다. */
const GROUP_WHY = {
  page: g => g.key_src === "rank"
    ? "검색 순위에서 같은 우리 페이지로 잡힌 검색어입니다."
    : "구글 실적에서 같은 우리 페이지로 들어오는 검색어입니다.",
  serp: () => "우리 페이지는 아직 없지만 검색 결과 상위가 크게 겹칩니다. 구글이 같은 문서들로 답하는 질문입니다.",
  cluster: g => `같은 주제 묶음(${esc(g.key)})입니다. 페이지·검색 결과로는 아직 가르지 못했습니다.`,
  norm: () => "띄어쓰기·대소문자만 다른 같은 검색어입니다.",
  alone: () => "",
};
// 한 줄에 같은 검색어의 여러 종류가 묶이면(scoring._merge_kinds) variants 에 같은 대상이
// 종류 수만큼 든다 — 「묶인 검색어」는 대상 수로 센다. 대상이 하나뿐이면 표가 할 말이 없다.
const oppTargets = g => [...new Set((g.variants || []).map(v => v.target))];
function oppGroup(o) {
  const g = o.group;
  if (!g || oppTargets(g).length < 2) return "";
  const url = g.via === "page" || (g.via === "norm" && /^https?:/.test(g.key || "")) ? g.key : "";
  const seen = new Set();
  const rows = g.variants.filter(v => !seen.has(v.target) && seen.add(v.target)).map((v, i) => `<tr><td>${esc(v.target)}${i === 0
      ? ` <span class="muted">대표</span>` : v.via === "serp" && g.via === "page"
      ? ` <span class="muted" title="우리 페이지는 없고, 검색 결과가 대표 검색어와 크게 겹쳐 붙었습니다">검색 결과 겹침</span>` : ""}</td>
    <td class="n">${v.volume ? num(v.volume) : "—"}</td>
    <td class="n">${Number.isFinite(v.score) ? Math.round(v.score) : "—"}</td>
    <td>${window.OPP_LABEL[v.status] || esc(v.status)}</td></tr>`).join("");
  const lead = g.variants[0] || {};
  return `<p class="detw"><b>묶인 검색어 ${oppTargets(g).length}개</b> · ${(GROUP_WHY[g.via] || GROUP_WHY.alone)(g)}${url
      ? ` <a href="${esc(url)}" target="_blank" rel="noopener">${esc(url)}</a>` : ""}</p>
    <table class="dett"><thead><tr><th>검색어</th><th class="n">월 검색량</th><th class="n">점수</th>
      <th>상태</th></tr></thead><tbody>${rows}</tbody></table>
    <p class="detn">${Number.isFinite(g.score) && g.score !== lead.score
      ? `이 줄의 ${Math.round(g.score)}점은 대표 검색어 ${Math.round(lead.score)}점에 나머지 검색어의 검색량을 더해 다시 잰 값입니다. `
      : ""}열기(호스팅은 요청문 복사)·완료·제외는 묶인 ${g.ids.length}개에 함께 적용됩니다. 할 일과 요청문은 대표 검색어 기준입니다.</p>`;
}

// 묶인 줄의 상태 바꾸기 — 기회 창구(/api/opp)는 한 번에 하나를 받는다. 새 창구를
// 만들지 않고 하나씩 보낸다(두 서버에 같은 라우트를 또 세우면 이음매가 하나 는다).
// 하나라도 못 바꾸면 그렇다고 말한다 — 반쯤 바뀐 줄은 새로고침 뒤 두 줄로 갈라져 보인다.
async function setOpps(ids, status) {
  let failed = 0, why = "";
  for (const id of ids) {
    const [ok, r] = await post("/api/opp", {project: $("proj").value, id, status});
    if (!ok) { failed++; why = why || (r && r.error) || ""; }
  }
  if (failed) toast(why || `${ids.length}개 중 ${failed}개를 못 바꿨습니다. [화면 다시 불러오기] 뒤 다시 하면 됩니다.`, "bad");
  else toast(`묶인 기회 ${ids.length}개에 적용했습니다. ` + (OPP_DONE[status] || ""), "ok");
  load();
}

/* 상태 바꾸기. 여태 응답을 안 봤다 — 403(토큰 만료)이 와도 load() 가 돌고 화면은
   바뀐 것이 없는 채로 조용했다. 누른 사람에게는 버튼이 죽은 것으로 보인다.
   실패하면 말하고 배지는 그대로 둔다(거짓 성공을 그리지 않는다). */
async function setOpp(id, status) {
  // 사이트 이름을 같이 싣는다 — 로컬 대시보드는 이 이름으로 "내 Brain 이냐 호스팅이냐"를
  // 갈라 호스팅이면 서버로 넘긴다(프록시). 호스팅 서버는 로그인 세션으로 이미 안다.
  const [ok, r] = await post("/api/opp", {project: $("proj").value, id, status});
  // 됐는지를 돌려준다 — 보드처럼 먼저 옮겨 그린 화면이 실패하면 되돌려 그린다.
  if (!ok) {
    toast((r && r.error) || "상태를 바꾸지 못했습니다. [화면 다시 불러오기] 뒤 다시 하면 됩니다.", "bad");
    return false;
  }
  toast(OPP_DONE[status] || "바꿨습니다.", "ok");
  load();
  return true;
}
// 누른 뒤에 무엇이 달라졌는지로 말한다 — "저장됨"은 무엇이 저장됐는지 말하지 않는다.
const OPP_DONE = {acked: "진행 중으로 옮겼습니다.", done: "완료로 옮겼습니다. [개요]의 완료 후 관찰에서 전·후를 봅니다.",
                  dismissed: "제외했습니다.", new: "다시 열었습니다."};

