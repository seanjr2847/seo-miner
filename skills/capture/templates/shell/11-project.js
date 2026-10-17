// 브라우저는 아무 사이트에서나 127.0.0.1로 POST를 보낼 수 있어서 서버가 1회용 토큰을
// 요구한다 — 대시보드를 띄울 때 받은 주소(?t=…)에 들어 있다.
// 서버가 페이지에 실어 준 것(__SM_TOKEN__)이 먼저다 — ?t= 없이 연 탭(자동완성·즐겨찾기)도
// 저장된다. ?t= 는 그 전 판 서버가 띄운 탭을 위한 물림이다. 그래도 서버 토큰과 어긋나
// 403 이 오면(탭 복원·서버 교체) 같은 출처의 GET /api/token 으로 현재 토큰을 받아 한 번만
// 다시 보낸다(서버가 Host·Origin 을 가려 준다). 호스팅에는 그 경로가 없고(404) 토큰도
// 안 보므로 재시도하지 않는다.
let TOKEN = window.__SM_TOKEN__ || new URLSearchParams(location.search).get("t") || "";
async function refreshToken() {
  try {
    const r = await fetch("/api/token", {headers: {"X-Requested-With": "seo-miner"}});
    if (!r.ok) return false;
    const t = (await r.json()).token || "";
    if (!t || t === TOKEN) return false;
    TOKEN = t;
    return true;
  } catch (e) { return false; }
}
async function post(path, body) {
  const send = () => fetch(path, {method:"POST", body: JSON.stringify(body),
    headers:{"Content-Type":"application/json", "X-Token": TOKEN}});
  let res = await send();
  if (res.status === 403 && await refreshToken()) res = await send();
  return [res.ok, await res.json()];
}

// 임계값은 서버 payload(d.rules)에서만 온다 — JS 리터럴 기본값은 두지 않는다.
let RULES = {};

let READONLY = false;                    // 박제된 기록에서는 상태를 바꿀 수 없다

function copyCmd(el) {
  // 칩 안의 글자만 바뀌면 눌렀는지 놓치기 쉽다 — 토스트로 한 번 더 말한다.
  // 클립보드가 막힌 환경(권한 거부·비보안 출처)에서는 조용히 끝나면 안 된다:
  // 무엇을 하면 되는지까지 말한다.
  SM.ui.copyText(el.textContent.trim()).then(ok => {
    if (!ok) return window.toast("복사하지 못했습니다. 명령 글자를 직접 선택하면 복사됩니다.", "bad");
    el.classList.add("copied");
    setTimeout(() => el.classList.remove("copied"), 1400);
    window.toast("명령을 복사했습니다. 채팅에 붙여 넣으면 실행됩니다.", "ok");
  });
}

// 화면마다 날짜 축이 다르다 — GSC 스냅샷·순위·색인·기기·AI 는 수집 주기가 따로 돈다.
// 머리말이 축별 날짜를 다 적는 이유가 그것이다: 하나만 적으면 나머지 화면의 숫자까지
// 그 날짜로 읽는다. [기준 수집일]이 되돌리는 것도 GSC 축 하나뿐이라고 여기서 말한다.
const AXES = [["순위", "rank_date"], ["색인", "index_date"],
              ["기기", "device_date"], ["AI 인용", "ai_date"]];

// 사이트 아이콘 — 도메인을 알면 그 사이트의 파비콘, 모르거나 못 받으면 앞글자 하나.
// 이름 → 도메인은 /api/domains 가 정본이다(loadProjects 가 SITE_DOM 에 채운다).
let SITE_DOM = {};
const FAVICON = "https://www.google.com/s2/favicons";   // 박제본엔 도메인 표가 없어 안 불린다
const siteFirst = n => String(n || "").trim().charAt(0).toUpperCase();
function siteIcon(name) {
  const ch = siteFirst(name), dom = SITE_DOM[name];
  if (!dom) return esc(ch);
  return `<img src="${FAVICON}?domain=${encodeURIComponent(dom)}&sz=64" ` +
    `alt="${esc(ch)}" loading="lazy" decoding="async" referrerpolicy="no-referrer" ` +
    `onerror="this.replaceWith(this.alt)">`;
}

// 접힌 레일의 사이트 카드는 이름 대신 아이콘 하나다 — 정본은 #proj 의 값이다.
function siteAv() {
  const a = $("siteav"), p = $("proj");
  if (a) a.innerHTML = siteIcon(((p && p.value) || "").trim());
  projSync();
}

/* 사이트 고르개의 보이는 쪽. 값·목록의 정본은 숨은 #proj 고, 여기는 그걸 그리기만 한다 —
   고르면 #proj.value 를 바꾸고 change 를 쏜다(원본·애드온의 듣는 자리가 그대로 돈다). */
const PM = { open: false, act: 0, q: "", names: [] };
const PM_CHECK = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12.5l4.5 4.5L19 7.5"/></svg>';
const PM_SEARCH_AT = 7;                  // 이보다 많으면 위에 찾기 칸을 세운다
const PM_PLUS = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v14M5 12h14"/></svg>';
const PM_ADD = "+add";                   // 사이트 이름이 될 수 없는 표식(이름은 영문·숫자·-·_ 뿐)

function projSync() {
  const p = $("proj"), b = $("projbtn");
  if (!p || !b) return;
  $("projnm").textContent = p.value || "";
  b.setAttribute("aria-label", "보는 사이트: " + (p.value || "없음") + " — 바꾸기");
  if (PM.open) projPaint();
}

function projMenu() {
  let m = $("projmenu");
  if (m) return m;
  m = document.createElement("div");
  m.id = "projmenu";
  m.className = "projmenu";
  m.hidden = true;
  m.innerHTML = '<div class="pm-hd"><span>보는 사이트</span><span class="pm-n"></span></div>' +
    '<input class="pm-q" type="text" placeholder="사이트 찾기" aria-label="사이트 찾기" ' +
    'spellcheck="false" autocomplete="off">' +
    '<div class="pm-ls" role="listbox" id="projlist" tabindex="-1" aria-label="사이트"></div>';
  document.body.appendChild(m);
  const q = m.querySelector(".pm-q"), ls = m.querySelector(".pm-ls");
  q.addEventListener("input", () => { PM.q = q.value; PM.act = 0; projPaint(); });
  m.addEventListener("keydown", projKey);
  // 누르는 순간 포커스가 옮겨 가면 목록이 닫히지 않게 — 고르기는 click 에서
  ls.addEventListener("mousedown", ev => ev.preventDefault());
  ls.addEventListener("click", ev => {
    const o = ev.target.closest(".pm-o");
    if (o) projPick(o.dataset.v);
  });
  ls.addEventListener("mousemove", ev => {
    const o = ev.target.closest(".pm-o");
    if (o && +o.dataset.i !== PM.act) { PM.act = +o.dataset.i; projActive(); }
  });
  return m;
}

function projPaint() {
  const p = $("proj"), m = projMenu();
  const all = [...p.options].map(o => o.value);
  const q = PM.q.trim().toLowerCase();
  PM.names = q ? all.filter(n => n.toLowerCase().includes(q)) : all;
  PM.act = Math.max(0, Math.min(PM.act, PM.names.length));   // 끝 칸(새 사이트 추가)까지
  m.querySelector(".pm-n").textContent = all.length + "개";
  m.querySelector(".pm-q").hidden = all.length < PM_SEARCH_AT;
  const k = PM.names.length;
  m.querySelector(".pm-ls").innerHTML = (k
    ? PM.names.map((n, i) => `<div class="pm-o" role="option" id="pm-o${i}" data-i="${i}" ` +
        `data-v="${esc(n)}" aria-selected="${n === p.value}"><span class="av" aria-hidden="true">` +
        `${siteIcon(n.trim())}</span><span class="nm">${esc(n)}</span>${PM_CHECK}</div>`).join("")
    : '<div class="pm-none">맞는 사이트가 없습니다.</div>') +
    `<div class="pm-o pm-add" role="option" id="pm-o${k}" data-i="${k}" data-v="${PM_ADD}" ` +
    `aria-selected="false"><span class="av" aria-hidden="true">${PM_PLUS}</span>` +
    `<span class="nm">새 사이트 추가</span></div>`;
  PM.names = PM.names.concat(PM_ADD);
  projActive();
}

function projActive() {
  const ls = $("projlist");
  if (!ls) return;
  ls.querySelectorAll(".pm-o").forEach(o => o.classList.toggle("act", +o.dataset.i === PM.act));
  const cur = $("pm-o" + PM.act);
  if (cur) {
    ls.setAttribute("aria-activedescendant", cur.id);
    cur.scrollIntoView({ block: "nearest" });
  } else ls.removeAttribute("aria-activedescendant");
}

// 버튼(=사이트 카드) 아래에 띄운다. 접힌 레일이면 카드 오른쪽으로, 아래가 모자라면 위로.
function projPlace() {
  const m = $("projmenu"), card = $("projbtn").closest(".site") || $("projbtn");
  const r = card.getBoundingClientRect(), W = innerWidth, H = innerHeight;
  const narrow = r.width < 120;
  const w = Math.min(Math.max(narrow ? 240 : r.width, 240), W - 16);
  m.style.width = w + "px";
  const left = narrow ? r.right + 8 : r.left;
  let top = narrow ? r.top : r.bottom + 6;
  const h = m.offsetHeight;
  if (top + h > H - 8) top = narrow ? Math.max(8, H - 8 - h) : Math.max(8, r.top - 6 - h);
  m.style.left = Math.max(8, Math.min(left, W - 8 - w)) + "px";
  m.style.top = top + "px";
}

function projOpen() {
  const p = $("proj"), b = $("projbtn"), m = projMenu();
  if (!p || p.hidden || !p.options.length) return;
  PM.open = true; PM.q = "";
  m.querySelector(".pm-q").value = "";
  PM.act = Math.max(0, [...p.options].findIndex(o => o.value === p.value));
  projPaint();
  m.hidden = false;
  b.setAttribute("aria-expanded", "true");
  projPlace();
  const q = m.querySelector(".pm-q");
  (q.hidden ? $("projlist") : q).focus();
}

function projClose(refocus) {
  const m = $("projmenu");
  if (!PM.open || !m) return;
  PM.open = false;
  m.hidden = true;
  $("projbtn").setAttribute("aria-expanded", "false");
  if (refocus) $("projbtn").focus();
}

// 새 사이트 — 등록하는 자리는 셋이다. 호스팅 화면은 온보딩(/start, app.html 의 [사이트 추가]와
// 같은 곳). 로컬이라도 호스팅 측정 모드면 등록은 웹이 맡는다(설정의 등록 칸이 숨는다) —
// 연결된 웹의 /start 를 새 탭으로 열고, 연결 전이면 [웹에 연결] 칸으로 보낸다.
// 그 밖의 로컬은 [설정]의 새 사이트 등록 칸을 펼쳐 보여 준다.
function projAdd() {
  projClose(false);
  if (window.SM_HOSTED) { location.href = "/start"; return; }
  const S = window.SETUP || {};
  if (S.mode === "hosted") {
    if (S.web_url) { window.open(S.web_url + "/start", "_blank", "noopener"); return; }
    SM.show("settings");
    projReveal($("u-remote"), $("u-line"));
    if (window.toast) toast("사이트는 웹에서 등록합니다. 먼저 웹에 연결해 주세요.");
    return;
  }
  SM.show("settings");
  const card = $("st-project");
  if (card && !card.hidden) {
    card.open = true;
    projReveal(card, card.querySelector("input:not([type=hidden]), select, textarea"));
  }
}

// 화면을 막 바꾼 뒤라 바로 스크롤하면 전환이 맨 위로 되돌린다 — 한 틱 뒤에 그 칸으로 간다.
function projReveal(el, field) {
  if (!el || el.hidden) return;
  setTimeout(() => {
    el.scrollIntoView({ block: "center", behavior: "smooth" });
    if (field) setTimeout(() => field.focus({ preventScroll: true }), 350);
  }, 60);
}

function projPick(v) {
  const p = $("proj");
  if (v === PM_ADD) return projAdd();
  projClose(true);
  if (v == null || v === p.value) return;
  p.value = v;
  p.dispatchEvent(new Event("change", { bubbles: true }));
}

function projKey(ev) {
  const n = PM.names.length;
  if (ev.key === "ArrowDown" || ev.key === "ArrowUp") {
    ev.preventDefault();
    if (n) { PM.act = (PM.act + (ev.key === "ArrowDown" ? 1 : n - 1)) % n; projActive(); }
  } else if (ev.key === "Home" || ev.key === "End") {
    if (ev.target.classList.contains("pm-q")) return;     // 찾기 칸에서는 글자 커서가 움직인다
    ev.preventDefault();
    PM.act = ev.key === "Home" ? 0 : Math.max(0, n - 1);
    projActive();
  } else if (ev.key === "Enter") {
    ev.preventDefault();
    if (n) projPick(PM.names[PM.act]);
  } else if (ev.key === "Escape") {
    ev.preventDefault();
    projClose(true);
  } else if (ev.key === "Tab") {
    projClose(false);
  }
}

if ($("projbtn")) {
  $("projbtn").addEventListener("click", () => PM.open ? projClose(true) : projOpen());
  $("projbtn").addEventListener("keydown", ev => {
    if (ev.key === "ArrowDown" || ev.key === "ArrowUp") { ev.preventDefault(); projOpen(); }
  });
  // 바깥을 누르면 닫는다. 화면이 움직이면 붙은 자리가 어긋나므로 닫는다(목록 안 스크롤은 빼고).
  document.addEventListener("pointerdown", ev => {
    if (PM.open && !ev.target.closest("#projmenu, #projbtn")) projClose(false);
  }, true);
  addEventListener("resize", () => projClose(false));
  addEventListener("scroll", ev => {
    if (PM.open && !(ev.target.closest && ev.target.closest("#projmenu"))) projClose(false);
  }, true);
  // 목록을 다시 쓰는 곳이 여럿이다(첫 적재·사이트 추가·애드온) — 바뀌면 보이는 쪽도 따라간다.
  new MutationObserver(() => projSync()).observe($("proj"), { childList: true });
}

