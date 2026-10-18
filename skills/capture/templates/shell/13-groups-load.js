/* ── 묶음 머리줄 · 이 묶음 다시 재기 ─────────────────────────────────────
   묶음(run_all.GROUPS)은 메뉴의 제목이자 다시 재는 단위다. 잴 단계가 있는 묶음의
   화면마다 제목 아래 한 줄을 세운다: 단계별 마지막으로 잰 날(d.groups[].last — 서버가
   runs 에서 성공한 런만 골라 준다) + [이 묶음 다시 재기]. 전체 재기는 [진행 안내] 화면
   한 곳에만 선다(묶음 다섯을 한 번에 부른다 — 본문에 groups 를 안 싣는다).

   누르는 것은 두 배포에서 같은 요청이다: POST /api/run {project, groups}. 로컬은 이
   대시보드가 run_all.py --groups 를 띄우고(dashboard.run_route), 호스팅은 워커가
   돈다. 도는 중에 다른 묶음을 누르면 서버가 대기열에 올린다(응답 queued) — 화면은
   그 사실만 옮긴다. 상태는 GET /api/run/status 의 사이트별 groups 칸이 정본이다:
   로컬은 여기서 폴링하고, 호스팅은 애드온(dash.html)이 이미 도는 폴링 결과를
   window.grpStatus 로 넘겨준다(한 화면에 폴러가 둘이면 같은 것을 두 번 묻는다). */
let GRP_ST = {};                  // {묶음 id: {running, queued, last_run_at, due}}

/* "주기를 넘겼나"(메뉴의 점)의 판정은 서버에서 한 규칙이다(run_all.is_due): 페이로드
   (d.groups[].due)와 상태(GET /api/run/status 의 groups[].due)가 같은 답을 한다 — 호스팅
   /api/data 는 스케줄러가 쓰는 판정(store.overlay_group_due — 사이트의 자동 주기, 0 이면 끔)을
   싣고, 로컬은 둘 다 _axis_groups 다. 그래서 이 함수는 더 이상 둘을 **바로잡지** 않는다.
   남은 일은 둘뿐이다: (1) 다시 읽기 전에 시작·끝난 런의 점을 바로 반영, (2) 배포 시차 —
   옛 호스팅은 페이로드를 고정 주기로 세므로(자동 끈 사이트에 안 꺼지는 점, 6시간 주기
   사이트에 일주일 기다리는 점) 상태 값이 있으면 그걸 쓴다. 상태가 아직 없으면(첫 그림)
   페이로드 값을 쓴다. */
function grpDue(gs) {
  return (gs || []).map(g => {
    const s = GRP_ST[g.id];
    return s && typeof s.due === "boolean" ? {...g, due: s.due} : g;
  });
}
const GRP_LABEL = "이 묶음 다시 재기", GRP_ALL = "전체 다시 재기";

function groupHead(d) {
  const gs = d.groups || [];
  for (const v of SM.views) {
    const box = $("view-" + v.id), vh = box && box.querySelector(":scope > .vh");
    if (!vh) continue;
    const g = gs.find(x => x.id === v.group);
    // 날짜 칸은 서버가 준 last 의 칸 그대로다(서버가 묶음의 시계 단계로 채운다 — 할 일은
    // 매일 런의 단계). 버튼은 다시 잴 단계가 있는 묶음에만 선다.
    const clock = Object.keys((g && g.last) || {});
    let html = "";
    if (g && clock.length) {
      const ax = clock.map(st => {
        const at = g.last[st];
        return `<span class="ax${at ? "" : " none"}">${esc(stageName(st))}<em>${
          at ? esc(at) : "아직"}</em></span>`;
      }).join("");
      // 묶음 이름을 줄 맨 앞에 세운다 — 레일의 묶음 제목(.navgrp)은 좁은 화면에서 안
      // 그려져, 모바일에서는 "이 묶음"이 무엇인지 말하는 자리가 여기뿐이다. 버튼이 다시
      // 재는 범위(g.stages)는 이름표(title·aria-label)로 말한다.
      const scope = `${GRP_LABEL} — ${g.name}: ${(g.stages || []).map(stageName).join(" · ")}`;
      html = `<b class="gname">${esc(g.name)}</b><span class="k">마지막으로 잰 날</span>${ax}` +
        (READONLY || !(g.stages && g.stages.length) ? "" :
        `<button class="go sm sec" data-grp="${esc(g.id)}" title="${esc(scope)}"
           aria-label="${esc(scope)}" onclick="groupRun(this)">${GRP_LABEL}</button><span class="gst"></span>`);
    } else if (v.id === "guide" && !READONLY && gs.some(x => x.stages && x.stages.length)) {
      // 전체 재기는 여기 하나다 — 이름은 페이로드의 묶음에서 읽는다(사본을 안 둔다).
      // 버튼은 [기록]이 같은 일을 부르는 act() 와 같은 몸(.go)이다 — 크기가 갈리면
      // 같은 행동이 화면마다 다른 무게로 읽힌다.
      const names = gs.filter(x => x.stages && x.stages.length).map(x => x.name);
      html = `<span class="k">${esc(names.join(" · "))}</span>
        <button class="go" data-grp="" onclick="groupRun(this)">${GRP_ALL}</button>
        <span class="gst"></span>`;
    }
    let el = vh.querySelector(":scope > .vgrp");
    // 단계별 다시 재기 접힘(viewCmds 가 단다)은 묶음 버튼 옆에 선다 — 이 줄을 새로
    // 쓰는 동안 잃지 않게 들어 두었다가 되붙인다.
    const more = vh.querySelector(".vh-more");
    if (!html) { if (el) el.remove(); if (more) vh.appendChild(more); continue; }
    if (!el) {
      el = document.createElement("div");
      el.className = "vgrp";
      vh.appendChild(el);
    }
    el.innerHTML = html;
    // 넓은 화면의 머리에는 날짜가 둘까지만 서고(CSS), 전부는 이 줄에 마우스를 올리면 나온다.
    el.title = clock.length ? "마지막으로 잰 날 — " + clock.map(st =>
      `${stageName(st)} ${g.last[st] || "아직"}`).join(" · ") : "";
    if (more) el.appendChild(more);
  }
  grpPaint();
}

// 버튼의 얼굴은 상태에서 온다 — 누른 직후에도, 새로고침한 뒤에도 같은 말을 한다.
function grpPaint() {
  // 전체 버튼이 보는 묶음은 잴 단계가 있는 것뿐이다([관리]는 늘 쉬고 있다).
  const ids = SM.groups.filter(g => g.stages && g.stages.length && GRP_ST[g.id]).map(g => g.id);
  document.querySelectorAll(".vgrp [data-grp]").forEach(b => {
    const gid = b.dataset.grp, st = b.nextElementSibling;
    // 전체 버튼(gid "")은 잴 묶음이 전부 돌거나 줄 서 있으면 누를 게 없다.
    const s = gid ? (GRP_ST[gid] || {})
      : {running: ids.length > 0 && ids.every(i => GRP_ST[i].running || GRP_ST[i].queued),
         queued: false};
    const busy = !!(s.running || s.queued);
    if (b.dataset.wait !== "1") b.disabled = busy;
    if (st) st.textContent = s.running ? "재는 중입니다" : s.queued ? "지금 도는 재기가 끝나면 잽니다" : "";
  });
}

async function groupRun(btn) {
  const project = $("proj") && $("proj").value;
  if (!project || btn.disabled) return;
  const gid = btn.dataset.grp;
  btn.disabled = true; btn.dataset.wait = "1";
  let ok = false, r = {};
  try {
    [ok, r] = await post("/api/run", gid ? {project, groups: gid} : {project});
  } catch (e) { r = {}; }
  delete btn.dataset.wait;
  if (!ok) {
    btn.disabled = false;
    // 원인 + 다음 행동. 서버 문구(detail·error)가 있으면 그게 원인이다.
    return toast(`${(r && (r.detail || r.error)) || "서버가 응답하지 않았습니다."} 잠시 뒤 다시 누르면 재시도합니다.`, "bad");
  }
  const who = gid ? ((SM.groupOf(gid) || {}).name || "이 묶음") : "전체";
  toast(r.queued ? `지금 도는 재기가 끝나면 ${who}도 이어서 잽니다.`
                 : `${who} 다시 재기를 시작했습니다. 끝나면 숫자가 저절로 바뀝니다.`, "ok");
  if (gid) GRP_ST[gid] = {...(GRP_ST[gid] || {}), [r.queued ? "queued" : "running"]: true};
  grpPaint();
  // 뒤에 얹히는 층(호스팅 애드온)이 자기 진행 배너를 바로 세우게 알린다 — 안 알리면
  // 그 층의 다음 폴링(최대 8초)까지 화면이 조용하다.
  document.dispatchEvent(new CustomEvent("sm:run", {detail: {groups: gid, queued: !!r.queued}}));
  if (!window.SM_HOSTED) grpPoll(1500);
}

// 상태 한 벌을 받는다 — 로컬은 grpPoll, 호스팅은 애드온의 폴링이 부른다.
// 돌던 것이 다 끝났으면 화면을 다시 읽는다(호스팅은 애드온이 이미 load() 한다).
SM.api.grpStatus = st => {
  const p = $("proj") && $("proj").value;
  const was = Object.values(GRP_ST).some(s => s.running);
  GRP_ST = ((st && st[p]) || {}).groups || {};
  SM.setGroups(grpDue(SM.groups));          // 점의 출처 — grpDue 주석
  grpPaint();
  const now = Object.values(GRP_ST).some(s => s.running || s.queued);
  if (was && !now && !window.SM_HOSTED) load();
  return now;
};

let GRP_TIMER = 0;
function grpPoll(wait) {
  if (window.SM_HOSTED || READONLY) return;
  clearTimeout(GRP_TIMER);
  GRP_TIMER = setTimeout(async () => {
    const p = $("proj") && $("proj").value;
    if (!p) return;
    try {
      const res = await fetch("/api/run/status?project=" + encodeURIComponent(p));
      if (res.ok && window.grpStatus(await res.json())) grpPoll(5000);
    } catch (e) { /* 다음 새로고침에 다시 묻는다 — 폴링 실패는 버튼을 안 잠근다 */ }
  }, wait || 0);
}
document.addEventListener("sm:site", () => { GRP_ST = {}; grpPoll(0); });

async function loadProjects() {
  const names = await (await fetch("/api/projects")).json();
  const want = decodeURIComponent(location.hash.slice(1));
  // 없는 사이트를 가리키는 주소로 들어오면 여태 조용히 첫 사이트가 열렸다 —
  // 링크를 받은 사람은 자기가 부른 사이트를 보고 있다고 믿는다. 말하고, 주소를
  // 치운다(안 치우면 hashchange 가 돌 때마다 같은 말을 다시 한다).
  if (want && !names.includes(want)) {
    if (window.toast) toast("등록되지 않은 사이트입니다. 첫 사이트를 엽니다.", "bad");
    history.replaceState(null, "", location.pathname + location.search);
  }
  $("proj").innerHTML = names.map(n =>
    `<option${n === want ? " selected" : ""}>${esc(n)}</option>`).join("");
  // 도메인 표는 꾸밈이다 — 못 받아도 앞글자로 그리고 넘어간다
  try { const r = await fetch("/api/domains"); SITE_DOM = r.ok ? await r.json() : {}; }
  catch (e) { SITE_DOM = {}; }
  $("proj").hidden = !names.length;      // 사이트가 없으면 빈 칸을 띄우지 않는다
  if ($("projlab")) $("projlab").hidden = !names.length;
  siteAv();
  return names.length ? $("proj").value : null;
}

let PINNED = "";                    // [기준 수집일]로 고정한 GSC 수집일 ("" = 최신)

// 미판정 검색어가 남아 있으면 첫 화면을 개요 대신 심사로 보낸다(land 순위 1 — 안내(2)·
// 설정(3) 보다 낮다, 온보딩이 먼저다). 복원한 화면이 있으면 이미 이겼으니 안 본다.
// 판정 여부는 /api/data 에 없다 — [심사] 화면이 쓰는 것과 같은 /api/triage 가 정본이다.
async function checkTriageLand() {
  if (READONLY) return;
  const res = await fetch("/api/triage?project=" + encodeURIComponent($("proj").value));
  if (!res.ok) return;
  const t = await res.json();
  const none = (t.counts || {}).none || 0;
  SM.setCount("triage", none);          // 레일의 [심사] 건수 알약
  if (!SM.restored && none) SM.land("triage", 1);
}

async function load() {
  const name = $("proj").value;
  // 사이트가 없어도 본문은 세워 둔다 — 첫 등록을 하는 곳이 [설정] 화면이고,
  // 통째로 감추면 거기 갈 길이 없다(배너만 남아 막다른 길이 된다). render() 를
  // 안 부르는 갈래라 자리표를 직접 내린다(bootDone) — 안 그러면 사이트 없는
  // 첫 방문자는 자리표만 보고 [설정]에 닿지 못한다.
  if (!name) { bootDone(); SM.show(SM.at("settings") >= 0 ? "settings" : "guide"); return; }
  location.hash = encodeURIComponent(name);
  const res = await fetch("/api/data?project=" + encodeURIComponent(name) +
                          (PINNED ? "&date=" + encodeURIComponent(PINNED) : ""));
  const d = await res.json();
  if (!res.ok) {
    // 실패해도 자리표(#sm-boot)는 내린다 — 안 내리면 오류를 말하면서 동시에
    // "아직 불러오는 중"처럼 반짝인다. 본문(#content)은 채울 자료가 없으니 그대로
    // 감춰 둔다(첫 로드가 실패한 경우) — 이미 열려 있던 뒤 새로고침이면 no-op.
    const b = $("sm-boot");
    if (b) b.hidden = true;
    $("err").hidden = false;
    $("err").textContent = (d.error || "이 사이트의 자료를 불러오지 못했습니다.") +
      " [화면 다시 불러오기]를 누르면 다시 시도합니다.";
    return;
  }
  // 그 사이트에 없는 날을 고정하면 서버가 빈 짝을 준다 — 고정을 풀고 최신으로 되돌린다.
  // (PINNED 를 먼저 비우므로 데이터가 아예 없는 사이트에서도 두 번 이상 돌지 않는다.)
  if (PINNED && !d.gsc_date) { PINNED = ""; return load(); }
  $("err").hidden = true;
  $("content").hidden = false;
  render(d);
  checkTriageLand();
  // 사이트가 확정됐다고 한 번 알린다. 뒤에 얹히는 계층(호스팅 애드온)은 자기 데이터를
  // 사이트 이름이 정해진 뒤에야 부를 수 있는데, 여태 그걸 아는 방법이 #proj 의
  // change 이벤트뿐이었다 — 그리고 hash 로 자동 선택될 때는 change 가 안 난다.
  // 그래서 백링크·설정·키워드가 각자 첫 로드에 비어 있었고(백링크는 재시도 루프로,
  // 나머지는 사용자가 탭을 한 번 눌러야 채워지는 식으로) 세 곳이 따로 때워졌다.
  document.dispatchEvent(new CustomEvent("sm:site", {detail: {project: name}}));
}

// 배너는 "할 말이 있을 때만" 선다. 안에 든 것이 빈 #nx 뿐이면 감춘다.
function banSync() {
  const b = $("docban");
  if (!b) return;
  const nx = $("nx");
  // 이 배너가 얹힌 화면에 행동 띠가 있으면 "지금 할 것" 줄을 뺀다. 띠는 이 화면에서
  // 무엇을 누르라고 말하고, 이 줄은 파이프라인 전체가 어디쯤인지를 말한다 — 나란히
  // 세우면 한 화면에 다음 행동이 둘이 된다. 그 안내를 통째로 갖는 화면은 [진행 안내]다.
  const host = b.closest(".view");
  if (nx) nx.hidden = !!(host && host.querySelector(":scope > .next"));
  // 막는 항목이 있을 때만 상자로 선다. 준비물이 다 됐는데 상자가 첫 화면을 먹으면
  // 눈이 지표보다 배너를 먼저 읽는다.
  const heavy = !!b.querySelector("ul.must");
  b.classList.toggle("thin", !heavy);
  // 막는 항목이 없으면(=준비 끝) 안내 줄은 서지 않는다 — "필수 준비는 끝났습니다 · 한 바퀴
  // 완주"가 지표 위에서 화면 머리와 같은 말을 되풀이했다. 무엇을 눌러야 하는지는 띠와 레일이 말한다.
  b.hidden = !(heavy || b.querySelector("details") || b.classList.contains("bad"));
}

/* 배너 항목을 "첫 문장 + 접은 나머지"로 가른다. 다섯 문장짜리 산문이 화면 맨 위에
   서면 눈이 지표보다 그것을 먼저 읽는다.

   정본은 서버가 갈라 주는 것이다(doctor.py 가 {msg, detail} 로 돌려주면 그대로 쓴다).
   아직 안 갈라 온 항목은 여기서 합니다체 문장 경계로 자른다 — 세 문장 이상일 때만.
   두 문장은 접을 이유가 없고, 접으면 여는 수고가 읽는 수고보다 크다. */
function banSplit(s) {
  // msg 는 항상 한 문장이고 그것만 읽어도 뜻이 선다 — doctor._selfcheck 가 못 박는다.
  // detail 은 없으면 아예 안 온다(그때는 <details> 를 만들지 않는다).
  return [(s && s.msg) || s || "", (s && s.detail) || ""];
}

/* 배너 항목의 행동. 두 갈래뿐이고 **둘 다 서버가 준다.**
     cmd  로컬에만 온다 — 칠 채팅이 있는 배포다. 복사 칩.
     go   {label, href} — 갈 곳이 있는 항목에만 온다. 없으면 버튼을 안 만든다.

   셸이 doctor 의 id 를 보고 목적지를 고르지 않는다. 한 번 그렇게 짰다가 틀렸다:
   호스팅에서 실제로 뜨는 배너는 구글 재연결인데 [설정] 화면에는 구글 연결 항목이
   없어서, 눌러도 아무것도 못 하는 막다른 길로 보내고 있었다. 게다가 서버가 고칠
   문제(부품 설치·Brain 손상)는 사용자가 갈 데 자체가 없다 — 거기 버튼을 붙이면
   눌러도 아무 일이 없다. 어느 항목이 어디로 가는지는 서버만 안다. */
function banAct(s, local) {
  if (local && s.cmd)
    return ` <button class="cmd" onclick="copyCmd(this)">${esc(s.cmd)}</button>`;
  if (s.go && s.go.href)
    return ` <a class="go sm sec" href="${esc(s.go.href)}">${esc(s.go.label)}</a>`;
  return "";
}

async function loadDoctor() {
  try {
    // 화면이 보고 있는 사이트를 같이 보낸다 — 안 보내면 서버가 폴더로 추론하고,
    // 추론이 안 되면(사이트 여럿 + 리포 미등록) 안내가 비어 버린다.
    const cur = $("proj") ? $("proj").value : "";
    if (window.SM_HOSTED && !cur) return;    // 호스팅은 사이트 없이 물으면 404 다 — 고른 뒤에 다시 불린다
    const d = await (await fetch("/api/doctor?project=" + encodeURIComponent(cur || ""))).json();
    window.SETUP = d; SM.repaint();   // 기회 카드의 실행 자리(SM.host.oppBtn)가 여기서 도구 상태를 읽는다 — 늦게 오면 다시 그린다
    if (SM.host.playSync) SM.host.playSync();   // 할 일의 [PR 만들기] 준비·진행 상태(로컬만 — 호스팅 SM.host 에는 없다)
    // verdict·must·extra 는 이제 청중별로 갈려서 온다(doctor.py 가 SEOMINER_HOSTED
    // 로 가른다) — 호스팅 갈래에는 명령·파일 경로·"채팅에"가 안 들어가고,
    // doctor._selfcheck() 가 그걸 못 박는다. 그래서 화면이 다시 가릴 이유가 없다.
    // 오히려 가리면 호스팅에서 **유일한 사용자 몫**(구글 계정 연결)이 안 보인다 —
    // 그건 서버가 대신 로그인해 줄 수 없어 owner 가 user 이고 blocking 이다.
    // 여기서 배포로 가르는 것은 복사할 것(cmd) 하나뿐이다: 웹에는 칠 곳이 없다.
    const local = !window.SM_HOSTED;
    // verdict 는 "한 줄 요약", must 는 "다음 할 일 목록"이다(doctor.py 의 설계).
    // 할 일이 하나뿐이면 그 항목 자신의 글(+자세히+버튼)이 이미 요약과 같은
    // 사실을 다른 말로 되풀이한다 — 구글 미연결일 때 실제로 그랬다: 제목이
    // "구글 계정 연결 한 번만 남았습니다. 그래야 실적이 쌓입니다"라고 하고,
    // 바로 아래 유일한 항목이 "구글 계정을 연결하면 실적이 자동으로 쌓입니다"라고
    // 다시 말했다. 할 일이 둘 이상이면 verdict 가 진짜 종합이니 그대로 둔다.
    const soleMustSaysIt = (d.must || []).length === 1;
    let h = (d.verdict && !soleMustSaysIt) ? `<div class="verdict">${esc(d.verdict)}</div>` : "";
    let nxHtml = "";
    if (d.no_project) {
      nxHtml = `<span class="nxk">지금 할 것</span>` +
        `<span>아래 <b>2. 첫 사이트</b> 칸을 채우면 여기서 시작됩니다.</span>`;
    } else if (d.guide) {
      const g = d.guide, gh = g.here, gs = g.steps;
      // 돌아갈 단계 이름을 여기 옮겨 적지 않는다 — 정본은 stage.py 가 실어 보낸 목록이다.
      const back = gs[1] ? `<b>2. ${esc(gs[1].t)}</b>` : "<b>2번 단계</b>";
      nxHtml = gh === -1
        ? `<span class="nxk">한 바퀴 완주</span>` +
          `<span>${back}부터 다시 재면 그동안 고친 것이 숫자로 보입니다.</span>`
        : `<span class="nxk">지금 할 것</span>` +
          `<span><b>${esc(gs[gh].t)}</b></span>` +
          (local && gs[gh].cmd ? ` <code>${esc(gs[gh].cmd)}</code>` : "");
    }
    h += `<div class="nx" id="nx">${nxHtml}</div>`;
    // must 는 {msg, cmd} 다. 산문은 칩 없이도 혼자 읽히고, cmd 는 "그대로 복사해
    // 쓰는 것 한 줄"이라 안내 화면 단계와 같은 .cmd 칩으로 그린다(호스팅은 항상 없다).
    if (d.must && d.must.length)
      // 명령 칩은 칠 곳이 있는 배포에서만 낸다. 서버가 이미 청중별로 갈라 보내지만
      // (doctor.py 의 SEOMINER_HOSTED), 화면도 같은 선을 긋는다 — 이 한 줄이
      // 없으면 서버 쪽 분기가 한 번 새는 날 호스팅 화면에 로컬 전용 명령 칩이 뜬다.
      h += `<ul class="must">${d.must.map(s => {
        const [lead, detail] = banSplit(s);
        // 행동은 접힌 곳 **밖**이다 — 접은 자리에 누를 것을 숨기지 않는다.
        return `<li>${esc(lead)}` + banAct(s, local) +
          (detail ? `<details><summary>자세히</summary><p>${esc(detail)}</p></details>` : "") +
          `</li>`;
      }).join("")}</ul>`;
    if (d.extra && d.extra.length)
      h += `<details><summary>더 켤 수 있는 기능 ${d.extra.length}개 보기</summary>
        <ul>${d.extra.map(s => `<li>${esc(s)}</li>`).join("")}</ul></details>`;
    // 사이트가 없으면 render() 가 아예 안 돈다(부를 /api/data 가 없다) — 그래서
    // 여태 [진행 안내] 화면이 **첫 사용자에게만** 통째로 비어 있었다. 제목만 있고 6단계가
    // 없는 화면이 정확히 이 경우였다. doctor 도 같은 guide 를 실어 보내므로 여기서
    // 그린다. docban 을 쓰기 **전에** 부른다 — #nx 가 아직 없어 배너를 안 건드린다
    // (배너는 이 상태에서 [설정]의 사이트 등록 칸을 가리키는 쪽이 더 구체적이다).
    for (const h of DOCTOR_RENDER) if (h.before) h.before(d);
    $("docban").innerHTML = h;
    // 할 말이 없으면 상자도 안 세운다 — 호스팅에서 준비물 목록을 뺀 뒤 빈 띠만
    // 남아 화면 맨 위에 뜻 없는 상자가 서는 일이 없게 한다. 뒤늦게 renderGuide 가
    // #nx 를 채우면 그쪽이 다시 연다.
    banSync();
    for (const h of DOCTOR_RENDER) if (h.after) h.after(d);
  } catch (e) {
    // 배너 실패는 치명이 아님 — 본문은 그대로 뜬다. 다만 조용히 넘어가면 상태 배지
    // 5개(#s-*)와 조건부 버튼 3개가 영영 안 채워지는데 그 사실이 화면 어디에도
    // 안 남는다 — 그러니 실패했다는 말과 되돌릴 손잡이는 남긴다.
    // "다시 시도합니다"라고 글로 적어 두면 어디를 누르라는 말인지 화면이 안 말한다 —
    // 누를 것을 그 자리에 둔다.
    $("docban").className = "bad";
    $("docban").innerHTML = `<div class="verdict">준비 상태를 확인하지 못했습니다</div>` +
      `<p class="nx">서버가 응답하지 않았습니다. 다시 시도하면 대개 붙습니다.</p>` +
      `<div class="acts"><button class="go sec" onclick="loadDoctor()">다시 시도</button></div>`;
    $("docban").hidden = false;
  }
}

$("proj").addEventListener("change", () => { PINNED = ""; siteAv(); load(); });
/* 사이트 목록 → 대시보드의 이음매는 URL 의 hash 하나다(이음매 5번). 여태 그 hash 를
   **첫 적재에만** 읽었다: 대시보드를 열어 둔 채 #alpha-site → #beta-site 로 옮기면
   주소는 바뀌는데 화면은 그대로였다. 뒤로 가기도 같은 일이다.
   load() 가 다시 hash 를 쓰므로 이 핸들러가 또 돈다 — 값이 같으면 그냥 나간다. */
addEventListener("hashchange", () => {
  const want = decodeURIComponent(location.hash.slice(1));
  const sel = $("proj");
  if (!sel || !want || sel.value === want) return;
  if (![...sel.options].some(o => o.value === want)) return;   // 없는 사이트는 무시
  sel.value = want;
  PINNED = "";
  load();
});
$("gscdate").addEventListener("change", () => { PINNED = $("gscdate").value; load(); });
$("refresh").addEventListener("click", () => { loadDoctor(); load(); });

SM.sync();

// 인쇄 전에 접힌 것을 편다. `<details>` 는 CSS 로 못 연다(open 은 속성이다) —
// 안 펴면 보고서를 인쇄했는데 "무엇을 할지"가 접힌 채로 나간다. 끝나면 되돌린다.
let PRINT_OPENED = [];
addEventListener("beforeprint", () => {
  PRINT_OPENED = [...document.querySelectorAll("details:not([open])")];
  PRINT_OPENED.forEach(x => { x.open = true; });
});
addEventListener("afterprint", () => {
  PRINT_OPENED.forEach(x => { x.open = false; });
  PRINT_OPENED = [];
});

// 박제된 기록(--export)이면 서버가 없다 — 페이지에 박힌 데이터를 그리고, 손댈 수
// 없는 것들(트리아지·설정·새로고침·사이트 전환)은 아예 감춘다.
if (window.__SNAPSHOT__) {
  const s = window.__SNAPSHOT__;
  READONLY = true;
  $("proj").outerHTML = `<span class="sitename">${esc(s.data.project.name)}</span>`;
  if ($("projbtn")) $("projbtn").remove();
  for (const id of ["refresh", "gscdate", "datelab"]) if ($(id)) $(id).hidden = true;
  SM.drop("settings");                   // 남한테 보낼 수도 있는 파일이다 — 입력칸을 남기지 않는다
  SM.drop("guide");
  SM.drop("triage");                     // 판정은 서버가 저장한다 — 종이에서는 누를 수 없다
  SM.drop("kwmanage");                   // 추적을 켜고 끄는 화면이다 — 종이에서는 바꿀 수 없다
  if ($("sm-kwnudge")) $("sm-kwnudge").remove();   // [개요]의 그 화면으로 가는 알림도 같이 뺀다
  document.querySelector(".tag").textContent = `${s.exported} 기록`;
  if (s.actions.length) {
    $("acts").hidden = false;
    $("acts-list").innerHTML = s.actions.map(x => `<li>${esc(x)}</li>`).join("");
  }
  render(s.data);
} else {
  // 진단은 사이트를 고른 뒤에 묻는다. 먼저 물으면 빈 project 로 가는데, 로컬은 그걸
  // 폴더로 추론하지만 호스팅은 사이트 소유를 확인하므로 404 이고 다시 안 불려서,
  // 호스팅의 유일한 사용자 몫('구글 연결' 안내)이 첫 화면에 영영 안 떴다.
  // 목록을 못 받아도 진단은 띄운다 — 로컬에선 그게 "무엇이 빠졌나"를 보는 창이다.
  const listed = loadProjects();
  listed.then(load);
  listed.then(() => loadDoctor(), () => loadDoctor());
}
