// 띠가 이미 들고 있는 단계면 머리줄의 같은 버튼은 물러난다 — 한 화면에 같은 명령이
// 두 벌 같은 무게로 서지 않게. (로컬은 .vh-cmd, 호스팅은 애드온의 .sm-refresh.)
// 물러나는 방식은 띠가 그 단계를 어느 무게로 들고 있느냐로 갈린다:
//   주요(.taken)  띠가 채운 버튼으로 들고 있다 → 머리줄에서 뺀다.
//   보조(.faded)  띠가 테두리 버튼으로 들고 있다 → 흐리게만 한다.
// 보조까지 빼면 그 행동에 닿을 자리가 한 군데로 줄고, 그대로 두면 로컬에서
// `/capture gaps beta-site` 가 머리줄과 띠에 똑같은 무게로 두 벌 선다.
function dimHead(only) {
  for (const v of SM.views) {
    if (only && v.id !== only) continue;
    const box = $("view-" + v.id);
    if (!box) continue;
    const band = box.querySelector(":scope > .next");
    const runs = [...(band ? band.querySelectorAll("[data-run]") : [])];
    const at = sec => new Set(runs.filter(b => b.classList.contains("sec") === sec)
      .map(b => b.dataset.run));
    // 한 단계를 띠가 주요로도 보조로도 들고 있으면 주요가 이긴다 — 뺀 것을 다시
    // 흐리게 칠할 일이 없게 faded 에서 taken 을 덜어 낸다.
    const taken = at(false), faded = at(true);
    // [진행 안내]의 단계 목록(.stp)은 6단계 전부를 보여주는 표다 — 항목을 지우면 "그
    // 단계가 없어졌다"로 읽힌다(목록에서 빼지 않는다는 약속). 그래서 거기는
    // 예전 그대로 흐리게만 한다: 주요 중복은 뺀다, 보조 중복은 흐린다.
    box.querySelectorAll(".stp [data-run]").forEach(b => {
      const on = taken.has(b.dataset.run);
      b.classList.toggle("taken", on);
      b.classList.toggle("faded", !on && faded.has(b.dataset.run));
    });
    // 화면 제목 옆 칩(.vh-cmd, 호스팅의 .sm-refresh 도 이 클래스를 함께 단다)은
    // 그 화면에 하나뿐이고, 바로 아래 띠가 이미 같은 단계를 들고 있으면(주요든
    // 보조든) 손 닿는 자리가 코앞에 있으니 머리에 또 세우지 않는다 — 흐리게 두면
    // "머리에도 있고 띠에도 있다"로 읽힌다(블랙박스: [순위 상승 기회] 화면에서 "기회 다시
    // 분석"이 머리와 띠에 같이 떴다). 표식은 같다: data-run 이 "이 버튼을 누르면
    // 이 단계가 돈다"는 뜻이고, 안내 칩에는 서버가 runnable 이라고 한 줄에만 붙는다
    // (질문 0건이면 ai 단계의 명령이 /capture add 다).
    box.querySelectorAll(".vh .vh-cmd[data-run]").forEach(b => {
      b.classList.toggle("taken", taken.has(b.dataset.run) || faded.has(b.dataset.run));
      b.classList.remove("faded");
    });
  }
}
// 머리줄 버튼을 다는 시점은 이 파일이 못 정한다: 로컬은 viewCmds 가, 호스팅은
// 애드온(dash.html)이 자기 초기화 중 아무 때나 단다. 이벤트 순서로 맞추려 들면
// 응답이 빠를수록 잘 지는 경합이 된다 — 늦게 붙은 버튼도 같은 규칙을 받게
// 머리줄이 바뀌는 것만 지켜본다. (바꾸는 건 class 뿐이라 되돌아 불리지 않는다.)
const HEAD_WATCH = new MutationObserver(ms => {
  if (ms.some(m => m.target.closest && m.target.closest(".vh"))) dimHead();
});
HEAD_WATCH.observe($("content"), {childList: true, subtree: true});

// base(직전 값)를 주면 증감률까지 같이 말한다 — +471 만으로는 큰 건지 알 수 없다.
const delta = (d, invert, base, tip) => {
  if (!Number.isFinite(d) || d === 0) return "";   // 스냅샷 0개면 NaN — 안 그린다
  const good = invert ? d < 0 : d > 0;
  const sign = SM.ui.sign(d);
  const pc = Number.isFinite(base) && base > 0
    ? `<em>${sign}${(Math.abs(d) / base * 100).toFixed(Math.abs(d) / base < .1 ? 1 : 0)}%</em>`
    : "";
  return `<span class="d ${good ? "up" : "down"}"${tip ? ` title="${esc(tip)}"` : ""}>` +
         `${sign}${Math.abs(d).toLocaleString("ko-KR")}${pc}</span>`;
};
/* ── 펼침 패널 한 벌 ────────────────────────────────────────────────
   기회·키워드·순위·사이트 화면이 같은 것을 보여준다: 어느 페이지 얘기인지(표),
   그 페이지의 무엇이 잘못됐는지(진단), 그래서 뭘 하는지(할 일). 화면마다 따로
   만들면 같은 사실이 네 벌이 되고, 그중 셋은 언젠가 낡는다.
   진단 문장은 여기서 만들지 않는다 — scoring.page_advice 가 만든 것을 그린다. */
// 표는 좁은 화면에서 상자 안에서 넘친다 — 열이 다섯이라 390px 를 넘고, 상자가
// 없으면 펼침 패널이 아니라 페이지 전체가 옆으로 밀린다(table() 과 같은 규칙).
// 칸에는 경로만 — 도메인은 이 사이트 것이라 줄마다 같다. 전체 주소는 title 이 든다.
const urlPath = u => String(u || "").replace(/^https?:\/\/[^/]+/, "") || "/";
const pageTable = rows => !rows || !rows.length ? "" :
  `<div class="tw"><table class="dett"><thead><tr><th>페이지</th><th class="n">노출</th>
     <th class="n">클릭</th><th class="n">CTR</th><th class="n">평균순위</th></tr></thead>
   <tbody>${rows.map(r => `<tr><td><a href="${esc(r.page)}" target="_blank"
       rel="noopener" title="${esc(r.page)}">${esc(urlPath(r.page))}</a></td>
     <td class="n">${num(r.impressions)}</td><td class="n">${num(r.clicks)}</td>
     <td class="n">${r.ctr}%</td><td class="n">${r.position ?? "-"}</td></tr>`).join("")}
   </tbody></table></div>`;

// 페이지 한 장의 진단 — "지금 이렇다 / 이렇게 바꿔라". audit 이 없으면 왜 없는지 말한다.
// extra — 부르는 화면만 아는 맥락의 판정(예: [AI 인용]의 추출성, audit.extract_advice).
// 여기서 고르지 않는다: 셸은 어느 화면에서 불렸는지 모른다.
const pageFix = (audit, url, extra) => {
  if (!audit)
    return `<p class="detn">${esc(url || "이 페이지")}는 아직 점검하지 않았습니다.
      <b>내 페이지 점검</b>이 돌면 제목·설명·본문을 직접 읽고 무엇을 바꿔야 하는지
      여기에 적습니다.</p>`;
  const head = `<p class="detn">${esc(audit.checked_date || "")} 점검 ·
    제목 ${audit.title ? `<b>${esc(audit.title)}</b>` : "없음"} ·
    본문 ${num(audit.words)}단어</p>`;
  const a = [...(audit.advice || []), ...(extra || [])];
  if (!a.length)
    return head + `<p class="detn">규칙으로 잡히는 문제가 없습니다.
      제목·설명·H1·본문 길이·구조화 데이터가 모두 기준 안입니다.</p>`;
  // 한 줄로 말하고 표는 접어 둔다. '바꿀 값' 한 칸이 세 문장짜리 산문이라(요청문에
  // 쓸 글이라 그렇게 길다) 일곱 줄이면 기회 하나가 화면 두 장을 먹었고, 정작 다음
  // 행동(요청문 만들기)은 저 위로 밀려났다. 전문은 요청문 안에 그대로 있다.
  // 요약은 여기서 판정을 다시 쓰지 않는다 — tag 만 세어 잇는다(정본 scoring.page_advice).
  const tags = [...new Set(a.map(x => x.tag))];
  const lead = `손댈 곳 ${a.length}군데: ${tags.slice(0, 4).map(esc).join(", ")}`
    + (tags.length > 4 ? ` 외 ${tags.length - 4}종` : "");
  return head + `<details class="fixmore"><summary>${lead}</summary>
    <div class="tw"><table class="dett fix"><thead><tr><th>손댈 자리</th>
      <th>지금 값</th><th>바꿀 값</th></tr></thead><tbody>${a.map(x =>
      `<tr class="lv-${esc(x.level)}"><td>${esc(x.tag)}</td><td>${esc(x.now)}</td>
        <td>${esc(x.fix)}</td></tr>`).join("")}</tbody></table></div></details>`;
};

// 할 일 목록 — kind 마다 손대는 자리가 다르다. 표는 각 화면이 갖는다.
// tail 은 이미 만든 마크업이라 esc 하지 않는다 — what 은 서버 산문이라 esc 한다.
// (둘을 한 문자열로 뭉쳐 넘기면 버튼이 글자로 나온다. 실제로 그랬다.)
const playList = (what, acts, tail) => (!what && !(acts || []).length) ? "" :
  `${what ? `<p class="detw">${esc(what)}${tail || ""}</p>` : ""}
   <ol class="deta">${(acts || []).map(x => `<li>${esc(x)}</li>`).join("")}</ol>`;

/* ── 요청문 — AI 에게 붙여 넣을 브리프 ──────────────────────────────
   글은 서버(brief.py)가 쓴다. 기회마다 o.brief = {shape, body} 로 실려 오고,
   꼬리(답의 형식·규칙·언어·길이 기준)는 프로젝트마다 한 벌(d.brief.tails)이라
   여기서 잇는다 — 기회 200건이 같은 600자를 200번 싣지 않게 하려는 것뿐이다.

   예전엔 여기 틀 한 벌이 있었다: "아래 페이지를 고쳐 주세요"로 시작해 종류마다
   할 일 두세 줄만 달랐다. 색인 막힘과 남의 도메인에 보낼 연락문이 80% 같은 글이
   됐고, 새 글 브리프에 "이 페이지에 있는 내용에서만"이 붙었다. 꼴(shape — 이름·
   개수는 brief.SHAPE_NAMES 가 정본)과 종류별 근거 표는 brief.py 가 정한다.

   기회로 아직 안 올라온 행(뷰의 폴백)은 재료를 직접 넘긴다. 같은 꼴 이름과 같은
   머리말·꼬리를 쓰되 근거 표는 없다 — 그 행은 아직 판정을 안 받았다. */
window.BRIEF = null;                       // load() 가 d.brief 를 넣는다

// 폴백 본문 — 기회가 아닌 행. 머리말·붙여 넣기 칸·진단별 산출물은 BRIEF 가 댄다.
function fallbackBrief(c) {
  const B = window.BRIEF;
  const shape = c.shape || (c.url ? "fix_page" : "new_content");
  const L = [B.intro[shape] || "", "", "## 대상"];
  (c.queries || []).slice(0, 5).forEach(q => L.push(
    `- 검색어: ${q.q}` + (q.note ? ` (${q.note})` : "")));
  if (c.url) L.push(`- 페이지: ${c.url}`);
  else L.push(B.no_page[shape === "new_content" ? "new_content" : "unknown"]);
  if (c.problem) L.push(`- 왜 걸렸나: ${c.problem}`);
  // 언어·길이 기준은 '대상'의 줄 하나가 말한다 — 꼬리는 그 줄을 가리키기만 한다.
  // 주소의 첫 경로가 언어(/en/)면 사이트 기본이 아니라 그 언어의 줄이다(서버 요청문의
  // brief.page_locale 과 같은 규칙). 어느 언어를 알아보는지와 그 문장은 서버가 준다
  // (BRIEF.page_lang: 사이트 기본과 다른 언어 코드 → 줄) — 여기서 언어 목록을 안 갖는다.
  let seg = "";
  try { seg = (new URL(c.url, location.href).pathname.split("/")[1] || "").toLowerCase(); }
  catch (e) { /* 주소가 아니면 사이트 기본 */ }
  // /zh-hans/ 처럼 지역이 붙은 조각은 언어 코드(앞 토막)로도 찾는다 — 서버 lang_of 와 같은 결론.
  const pl = B.page_lang || {};
  const pageLang = c.url && shape !== "outreach" && (pl[seg] || pl[seg.split("-")[0]]);
  if (pageLang) L.push(pageLang);
  else if (B.lang_line && B.lang_line[shape]) L.push(B.lang_line[shape]);
  L.push("");

  const a = c.audit;
  if (B.page_state.includes(shape) && c.url) {
    if (a && !a.error) {
      const arr = k => { try { return JSON.parse(a[k] || "[]"); } catch (e) { return []; } };
      const h1 = arr("h1_json"), h2 = arr("h2_json"), sc = arr("schema_json");
      L.push(`## 지금 이 페이지 상태 (${a.checked_date || "점검일 미상"} 직접 확인)`);
      L.push(`- title: ${a.title || "(없음)"}${a.title ? ` (${a.title.length}자)` : ""}`);
      L.push(`- meta description: ${a.meta_description || "(없음)"}`
             + (a.meta_description ? ` (${a.meta_description.length}자)` : ""));
      L.push(`- H1: ${h1.length ? h1.join(" / ") : "(없음)"}`);
      L.push(h2.length ? `- H2 (${h2.length}개): ${h2.slice(0, 12).join(" / ")}`
             + (h2.length > 12 ? ` … 외 ${h2.length - 12}개` : "") : "- H2: (없음)");
      L.push(`- 본문 길이: ${num(a.words)}단어`);
      L.push(`- 구조화 데이터: ${sc.length ? sc.join(", ") : "(없음)"}`);
      if (a.canonical) L.push(`- canonical: ${a.canonical}`);
      if (a.robots) L.push(`- meta robots: ${a.robots}`);
      L.push("");
      if ((a.advice || []).length && shape !== "consolidate") {
        L.push(B.advice_head);   // 정본은 brief.ADVICE_HEAD (페이로드)
        a.advice.forEach((x, i) => L.push(`${i + 1}. [${x.tag}] 지금: ${x.now} → ${x.fix}`));
        L.push("");
      }
    } else {
      L.push("## 지금 이 페이지 상태",
             a && a.error ? `- 점검 실패: ${a.error}`
                          : "- 아직 이 페이지를 직접 점검하지 않았습니다. 아래를 스스로 확인하고 고쳐 주세요.",
             "");
    }
  }
  if ((c.acts || []).length) {
    L.push("## 이 상황에서 할 일");
    c.acts.forEach((x, i) => L.push(`${i + 1}. ${x}`));
    L.push("");
  }
  // 만들어 줄 것은 상황이 정한다 — 호출부가 주면 그쪽이 이긴다(그 화면이 상황을
  // 더 잘 안다). 단서가 진단뿐이면 진단 tag 별 산출물(BRIEF.by_tag)로 물러선다.
  let want = (c.deliver && c.deliver.length) ? c.deliver : [];
  if (!want.length) {
    const tags = [...new Set(((a && a.advice) || []).map(x => x.tag))];
    want = tags.map(t => B.by_tag[t]).filter(Boolean);
    if (!want.length) want = [B.deliver_default];
  }
  L.push("## 만들어 줄 것");
  want.forEach((x, i) => L.push(`${i + 1}. ${x}`));
  L.push("");
  if (B.slot[shape]) L.push("## 있으면 붙여 넣을 것 (선택)", B.slot[shape], "[여기에 붙여 넣기]", "");
  return {shape, body: L.join("\n")};
}

// 요청문 전문 = 머리말 → 진행 순서(heads) → 본문 나머지 → 꼬리. brief.join() 과 같은 규칙이다
// (test_seams 가 대조한다). 진행 순서가 데이터 뒤에 있으면 받는 쪽이 사이트를 안 보고 산출물로 간다.
const briefText = b => {
  const B = window.BRIEF || {}, head = (B.heads || {})[b.shape] || "";
  const i = b.body.indexOf("\n\n");
  const body = head && i >= 0 ? `${b.body.slice(0, i)}\n\n${head}\n\n${b.body.slice(i + 2)}` : b.body;
  return body + "\n" + ((B.tails || {})[b.shape] || "");
};

// 프롬프트 상자 — 접혀 있다가 펴면 본문이 보이고, 버튼 하나로 복사한다.
// c.brief(서버가 만든 것)가 있으면 그것, 없으면 폴백. BRIEF 자체가 없는 것은
// 요청문이 생기기 전의 옛 박제본이다 — 없는 글을 만들지 않고 그렇다고 말한다.
// 복사 버튼은 **패널 밖**, 손잡이 바로 뒤에 선다. 이 상자의 유일한 목적이 복사인데
// 버튼이 안에 있어서 누르려면 먼저 펴야 했다. 그다음엔 초록 손잡이(summary) 안에
// 넣었는데, 막대 전체가 한 버튼으로 읽혀 복사하려던 손가락이 패널을 열었다.
// 그래서 손잡이와 나란한 독립 보조 버튼이다 — 펴기 전에도 보이고, 누르면 복사만 한다.
// 요청문 상자는 **호스팅에만** 선다. 로컬(이 PC)에는 기회마다 "도구로 열기"(SM.host.oppBtn →
// runTool)가 있어서, 요청문을 파일로 써서 도구를 그 사이트 폴더에서 바로 띄운다 — 같은
// 글을 복사해 붙여 넣는 길을 하나 더 두면 행동이 두 벌이다. 호스팅은 브라우저라 도구를
// 못 띄우니 복사가 유일한 길이다. 박제본도 서버가 없는 로컬 조립이라 안 선다.
const askBlock = c => {
  if (!window.SM_HOSTED) return "";
  if (!window.BRIEF)
    return `<p class="detn">요청문은 이 보고서를 다시 만들면 생깁니다.</p>`;
  const b = c.brief || fallbackBrief(c);
  const t = briefText(b);
  // ack: 복사하면서 '진행 중'으로 옮길 기회 id(할 일인 기회만 — oppDetail 이 싣는다).
  // 호스팅에서는 복사가 곧 작업 시작이다(로컬의 [(도구)로 열기]와 같은 규칙).
  const ack = (c.ack || []).length ? ` data-ack="${c.ack.join(",")}"` : "";
  return `<details class="ask"><summary>요청문 만들기
      <span class="micro">${esc(window.BRIEF.labels[b.shape] || "")}</span></summary>
    <div class="pbox">
      <div class="pb-h"><span>그대로 복사해 Claude Code 에 붙여 넣습니다. ChatGPT 같은 AI 나 담당자에게 넘겨도 됩니다. 답이 HTML 리포트로 열리는 건 Claude Code 에서입니다</span></div>
      <textarea readonly rows="14" spellcheck="false" aria-label="요청문 전문">${esc(t)}</textarea>
    </div></details><button type="button" class="go sm sec askcopy"${ack}
      ${ack ? `title="복사하면 이 기회를 진행 중으로 옮깁니다" ` : ""}onclick="copyPrompt(this)">요청문 복사</button>`;
};

// 박제 보고서는 file:// 로도 열린다 — 거기서는 clipboard API 가 막히므로
// SM.ui.copyText 가 임시 입력칸으로 물러선다(둘 다 실패하면 사용자가 직접 고르게 안내한다).
// 버튼은 요청문 상자(details.ask) 바로 뒤에 선다 — askBlock 이 둘을 붙여 낸다.
SM.api.copyPrompt = function (btn) {
  const ta = btn.previousElementSibling.querySelector("textarea");
  const done = ok => {
    btn.textContent = ok ? "복사함" : "직접 선택";
    setTimeout(() => { btn.textContent = "요청문 복사"; }, 2000);
    // 할 일인 기회를 복사했으면 '진행 중'으로 옮긴다 — 호스팅에서는 복사가 작업 시작이다.
    // 복사에 실패했으면 안 옮긴다(넘기지 못한 일을 시작했다고 적지 않는다).
    const ids = (btn.dataset.ack || "").split(",").filter(Boolean).map(Number);
    if (ok && ids.length) ids.length > 1 ? setOpps(ids, "acked") : setOpp(ids[0], "acked");
  };
  // 접혀 있어도 되도록 보이는 입력칸이 아니라 글을 넘긴다(폴백은 SM.ui.copyText 가 한 벌로 가진다).
  return SM.ui.copyText(ta.value).then(done);
};


