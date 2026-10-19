/* ── 화면 머리의 갱신 손잡이 ─────────────────────────────────────────────
      로컬에는 이게 없었고, 명령 칩은 **빈 상태 안에만** 있었다 — 그래서 데이터가
      차는 순간 그 화면을 다시 채울 방법이 화면에서 사라졌다("빈 화면은 다음 걸음을
      말하는데 찬 화면은 말이 없다"). 호스팅은 SM.host.viewChip 을 실행 버튼으로
      갈아 끼운다 — 이 함수 자체는 배포를 모른다.

      명령은 단계 이름에서 그대로 나온다: run_all.STAGES 의 name 이 곧 인자다
      (`/capture gsc`, `/capture rank`, …). 그래서 표를 새로 두지 않는다 —
      화면이 어느 단계로 채워지는지는 view-def 의 stages 가 정본이고,
      test_seams.py 9번이 그 선언과 화면이 부르는 명령을 대조한다. ── */
function viewCmds() {
  if (window.__SNAPSHOT__) return;      // 박제본은 손댈 수 없는 기록이다 — 칩이 없다
  for (const v of SM.views) {
    const box = $("view-" + v.id);
    if (!box || !v.head.length) continue;
    const vh = box.querySelector(".vh");
    if (!vh) continue;
    // 지우고 다시 그린다 — "이미 있으면 건너뛴다"로는 안 된다: 이 함수는 SM.sync() 가
    // 다시 부를 때마다 불리는데, 호스팅 애드온이 SM.host 를 갈아 끼운 **뒤에** 한 번
    // 더 불린다(자기 스크립트가 원본 스크립트보다 뒤에 있어서 처음 호출 때는 아직
    // 로컬 기본값이다). 그때 낡은 칩이 남아 있으면 못 지우고 그대로 있는다.
    // 사이트가 바뀔 때 sm:site 가 다시 부르는 것도 같은 길을 탄다 — 로컬 칩이 실은
    // 사이트 이름(siteArg)이 그때 비로소 정해진다.
    // '다시 ~' 버튼은 화면당 하나다 — 머리의 [이 묶음 다시 재기](groupHead)만 앞에
    // 두고, 단계 하나씩 다시 재는 칩은 그 옆 접힘 메뉴 안에 둔다. 한 화면에 '실적 다시
    // 수집·색인 다시 확인·속도 다시 측정…'이 나란히 서면 무엇을 누를지 못 고른다.
    vh.querySelectorAll(".vh-more").forEach(el => el.remove());
    const chips = v.head.map(SM.host.viewChip).join("");
    if (!chips) continue;
    const more = `<details class="vh-more"><summary>단계별로 다시</summary>` +
      `<div class="vh-menu">${chips}</div></details>`;
    const row = vh.querySelector(":scope > .vgrp");
    (row || vh).insertAdjacentHTML("beforeend", more);
  }
}
// 좁은 화면의 메뉴는 가로 스크롤러다. 오른쪽 페이드는 "더 있다"는 말이라, 끝까지
// 밀었거나 애초에 안 넘치면 꺼야 한다 — 안 그러면 없는 것을 있다고 말한다.
function navVFade() {
  // 세로 레일(≥1000px): 메뉴가 넘칠 때만, 넘친 쪽 끝을 흐린다.
  const v = $("nav");
  if (!v) return;
  const over = v.scrollHeight > v.clientHeight + 1;
  v.classList.toggle("ovt", over && v.scrollTop > 4);
  v.classList.toggle("ovb", over && v.scrollTop + v.clientHeight < v.scrollHeight - 1);
}
function navFade() {
  navVFade();
  const n = $("nav") && $("nav").parentElement;      // 스크롤러(.navscroll)
  if (!n) return;
  n.classList.toggle("end", n.scrollLeft + n.clientWidth >= n.scrollWidth - 1);
  n.classList.toggle("begin", n.scrollLeft <= 1);
}
$("nav").parentElement.addEventListener("scroll", navFade, {passive: true});
$("nav").addEventListener("scroll", navVFade, {passive: true});
addEventListener("resize", navFade);

/* 접이식 레일 — 1000px 이상에서만 뜻이 있다(좁은 화면은 가로 탭). 상태는 <html data-rail>
   이 갖고, 첫 그림 전에 <body> 맨 앞 스크립트가 되살린다. 저장소가 막혀 있어도(사생활
   모드) 접고 펴는 것은 그대로 된다 — 기억만 못 한다. */
function railSet(min, save) {
  const r = document.documentElement, b = $("railfold");
  if (min) r.dataset.rail = "min"; else delete r.dataset.rail;
  if (b) {
    const label = min ? "사이드바 펼치기" : "사이드바 접기";
    b.setAttribute("aria-label", label);
    b.title = label;
    b.setAttribute("aria-expanded", String(!min));
  }
  if (save) { try { localStorage.setItem("sm:rail", min ? "min" : "wide"); } catch (e) {} }
}
$("railfold").addEventListener("click",
  () => railSet(document.documentElement.dataset.rail !== "min", true));
railSet(document.documentElement.dataset.rail === "min", false);

document.addEventListener("sm:views", () => { viewCmds(); dimHead(); navFade(); });
// 사이트가 정해지면 로컬 칩의 명령에 그 이름이 실린다 — 다시 그리고 톤도 다시 잡는다.
document.addEventListener("sm:site", () => { viewCmds(); dimHead(); });

/* ── 다음 행동 띠: 화면당 하나 ──────────────────────────────────────────────
      화면 머리(.vh) 바로 밑에 "무엇을 왜"를 한 문장으로 세우고, 그 문장 옆에
      누를 것을 둔다. 뷰는 자기 render 안에서 nextStep(id, html) 을 부른다 —
      상태(빈·진행·찬)에 따라 문장이 바뀌므로 매 렌더마다 다시 심는다.

      박제본에는 서버가 없다: 버튼을 남기면 눌러도 아무 일이 안 일어나는 거짓말이
      되므로 .acts 를 떼고 문장만 남긴다. */
/* ── 박제본에서 거짓말이 되는 것을 뗀다 ────────────────────────────────────
   박제본(--export)에는 서버가 없다. 그런데 화면에는 누를 것과 "누르면 이렇게 된다"는
   말이 그대로 남아 있었다 — 명령 칩은 복사되지만 칠 채팅이 없고, 띠 문장은 "연결하면
   여기가 채워집니다"라고 약속한다. 둘 다 파일 하나를 남에게 보낸 사람에게는 거짓이다.

   뗄 것 셋:
     .acts  행동 묶음 (nextStep 이 이미 뗀다)
     .cmd   명령 칩 — 칠 곳이 없다
     .live  뷰가 감싼 **행동 전제 구절**. 감싸는 것은 뷰의 몫이고(그 화면이 자기
            문장의 어디까지가 약속인지 안다), 떼는 것은 셸의 몫이다. 남는 문장만으로
            "지금 상태"가 읽혀야 한다는 것이 이 계약의 조건이다.

   문장 중간을 들어내면 앞뒤 공백이 둘로 붙는다 — 지운 자리의 공백도 같이 정리한다. */
function deadSweep(root) {
  if (!READONLY) return;
  const box = root || document;
  box.querySelectorAll(".cmd, .live, .acts").forEach(el => {
    const prev = el.previousSibling;
    el.remove();
    if (prev && prev.nodeType === 3) prev.textContent = prev.textContent.replace(/\s+$/, " ");
  });
}
SM.api.deadSweep = deadSweep;

/* 툴팁 자리잡기 — CSS 는 말풍선을 그릴 수 있어도 "화면 밖으로 나가면 안으로 밀어라"를
   못 한다. (i) 위 가운데를 기본으로 잡고, 양 끝에서만 뷰포트 안으로 물린다.
   위임으로 단다 — (i) 는 표를 다시 그릴 때마다 새로 생기므로 각자에게 걸 수 없다. */
function tipPlace(el) {
  const r = el.getBoundingClientRect();
  const half = Math.min(280, innerWidth - 24) / 2;
  const pad = 12;
  el.style.setProperty("--tip-l",
    Math.min(Math.max(r.left + r.width / 2, pad + half), innerWidth - pad - half) + "px");
  el.style.setProperty("--tip-t", (r.top - 7) + "px");
}
for (const ev of ["pointerenter", "focusin"])
  document.addEventListener(ev, e => {
    const h = e.target.closest && e.target.closest(".hint[data-tip]");
    if (h) tipPlace(h);
  }, true);          // 캡처로 듣는다 — pointerenter 는 위로 안 올라온다

/* 표가 옆으로 넘치는지는 그려 봐야 안다 — 열 수와 글자 길이가 자료마다 다르다.
   넘칠 때만 오른쪽 페이드를 켜고, 끝까지 밀면 끈다(없는 것을 있다고 말하지 않는다).
   메뉴의 navFade 와 같은 문법이다. */
function tableFade(root) {
  (root || document).querySelectorAll(".tw").forEach(w => {
    const over = w.scrollWidth > w.clientWidth + 1;
    w.classList.toggle("over", over);
    w.classList.toggle("end", !over || w.scrollLeft + w.clientWidth >= w.scrollWidth - 1);
    const c0 = w.querySelector("tbody td:first-child");
    // 첫 칸이 숫자(점수 등)면 붙박아 봐야 어느 줄인지 안 알려 준다 — 이름 칸일 때만.
    // 체크 칸([심사]의 고르기)도 이름 칸이 아니다 — 붙박으면 상자만 남고 이름이 밀려 나간다.
    w.classList.toggle("pin", over && !!c0 && !c0.classList.contains("num")
      && !c0.querySelector("input[type=checkbox]")
      && c0.offsetWidth <= w.clientWidth * 2 / 3);
    if (over && !w.dataset.fade) {
      w.dataset.fade = "1";
      w.addEventListener("scroll", () => w.classList.toggle("end",
        w.scrollLeft + w.clientWidth >= w.scrollWidth - 1), {passive: true});
    }
  });
}
SM.api.tableFade = tableFade;
addEventListener("resize", () => tableFade());
// 그리는 순간 감춰진 화면의 표는 폭이 0 이라 "안 넘친다"로 판정된다 — 모바일 개요 표가
// 옆으로 넘치는데도 페이드·붙박이 첫 열이 꺼져 있었다. 화면이 보일 때 다시 잰다.
document.addEventListener("sm:view", () => tableFade());

/* 화면 머리의 한 줄 부제 — "여기가 어디고 무엇을 보여주나"의 나머지 절반.
   설명 산문을 본문에 풀어 놓는 대신 한 문장으로 여기 세운다. 빈 글이면 감춘다. */
function viewSub(viewId, text) {
  const box = $("view-" + viewId);
  if (!box) return;
  const el = box.querySelector(":scope > .vh > .sub1");
  if (!el) return;
  el.textContent = text || "";
  el.hidden = !text;
}
SM.api.viewSub = viewSub;

/* 배너 자리 — 화면 머리(.vh) 다음, 다음 행동 띠(.next) 앞. 폭은 .view 상자가 주므로
   본문 폭 그대로다. 뒤에 얹히는 층(호스팅 애드온)이 실행 실패·진행 배너를 여기
   넣는다 — 자리를 각자 정하면 화면 머리 위 y=0 에 폭 595px 짜리 상자가 뜨는 식으로
   제각각이 된다. 채우면 보이고 비우면(:empty) 사라진다.
   SM.slot() 은 **지금 보고 있는 화면**의 자리, viewBanner(id) 는 지정한 화면의 자리다. */
SM.api.viewBanner = viewId => {
  const box = $("view-" + viewId);
  if (!box) return null;
  let s = box.querySelector(":scope > .vban");
  if (!s) {
    s = document.createElement("div");
    s.className = "vban";
    box.prepend(s);
  }
  // 부를 때마다 자리를 다시 본다. 한 번만 끼우면 안 된다: .vh 가 아직 안 선
  // 순간에 만들어진 자리는 제목 **위**에 그대로 남아, 고쳤는데도 같은 증상이
  // 그대로 재현된다(호스팅 담당이 자기 폴백에서 먼저 밟은 자리다).
  const vh = box.querySelector(":scope > .vh");
  if (vh && vh.nextElementSibling !== s) vh.insertAdjacentElement("afterend", s);
  return s;
};
SM.slot = function () { return window.viewBanner(this.cur); };

// 닫은 알림 카드 — 사이트·화면별로 닫을 때의 문장을 적어 둔다. 문장이 바뀌면(다음 걸음이
// 달라지면) 다시 뜬다. 저장이 막힌 브라우저면 이번 화면에서만 닫힌다.
const nxKey = viewId => "sm:nx:" + (($("proj") || {}).value || "") + ":" + viewId;
const NX_GONE = {};
const nxGone = (viewId, html) => {
  let v = NX_GONE[nxKey(viewId)];
  try { v = v || localStorage.getItem(nxKey(viewId)); } catch (e) {}
  return !!html && v === html;
};
document.addEventListener("click", ev => {
  const x = ev.target.closest && ev.target.closest("[data-nx-x]");
  if (!x) return;
  const el = x.closest(".next"), box = el && el.closest(".view");
  if (!box) return;
  const id = box.id.replace(/^view-/, "");
  NX_GONE[nxKey(id)] = el._html;
  try { localStorage.setItem(nxKey(id), el._html); } catch (e) {}
  window.nextStep(id, el._html);
});

SM.api.nextStep = (viewId, html) => {
  const box = $("view-" + viewId);
  if (!box) return;
  let el = box.querySelector(":scope > .next");
  if (viewId !== "guide" && nxGone(viewId, html)) html = "";
  if (!html) { if (el) el.remove(); dimHead(viewId); return banSync(); }
  if (!el) {
    el = document.createElement("div");
    el.className = "next";
    // 배너 자리가 있으면 그 **뒤**다 — 앞에 넣으면 실행 실패·진행 배너가 띠 아래로
    // 밀려, 지금 무엇이 도는지가 다음에 누를 것보다 아래에서 읽힌다.
    const anchor = box.querySelector(":scope > .vban") || box.querySelector(":scope > .vh");
    if (anchor) anchor.insertAdjacentElement("afterend", el); else box.prepend(el);
  }
  // 왼쪽 눈금은 CSS(::before)가 그린다 — 여기서 마크업에 끼우면 innerHTML 이
  // 넘겨받은 글자와 달라져, 이 띠를 자기가 세웠는지 내용으로 판별하는 층
  // (dash.html 의 paintSetNext)이 매번 "내 말이 아니다"라고 읽는다.
  el.innerHTML = html;
  el._html = html;       // 받은 글자 — 닫기 버튼을 덧붙여 innerHTML 은 이것과 다르다
  if (viewId !== "guide" && !READONLY)
    el.insertAdjacentHTML("beforeend", '<button type="button" class="nx-x" data-nx-x aria-label="이 알림 닫기">✕</button>');
  if (READONLY) el.querySelectorAll(".acts").forEach(a => a.remove());
  deadSweep(el);
  applyWording(el);
  dimHead(viewId);
  banSync();     // 이 화면에 띠가 섰으니 배너의 "지금 할 것" 줄은 물러난다
};

/* 띠를 "도는 중"으로 세운다 — 문장은 그대로 두고 상태만 바꾼다. 실행을 거는 층
   (호스팅 애드온)이 켜고 끈다. 문장까지 여기서 갈아 끼우지 않는 이유는, 무엇이
   도는 중인지는 그 층이 알고 이 함수는 모르기 때문이다. */
SM.api.nextRunning = (viewId, on) => {
  const el = $("view-" + viewId) &&
             $("view-" + viewId).querySelector(":scope > .next");
  if (el) el.classList.toggle("run", !!on);
};

/* ── 토스트 ────────────────────────────────────────────────────────────
   눌렀는데 화면이 그대로면 눌렸는지 알 수 없다. 복사됨·실행 시작·실패를 우하단에
   3초 띄운다(실패는 읽을 틈이 있게 9초). 여러 개면 쌓인다. 컨테이너가 live 영역(role="status")이라
   화면 낭독기도 같이 읽는다 —
   색만으로 성공과 실패를 가르지 않는다(글자가 정본이다).
   kind: "ok" | "bad" | 없음(중립). */
SM.api.toast = (msg, kind) => {
  if (!msg) return;
  let box = toastBox();
  const t = document.createElement("div");
  t.className = "toast" + (kind ? " " + kind : "");
  t.textContent = msg;
  // 다음 걸음 카드가 우하단에 떠 있으면 그 위로 쌓는다
  const nx = document.querySelector(".view:not([hidden]):not(#view-guide) > .next");
  box.style.bottom = nx && nx.offsetHeight ? `calc(var(--sp-4) + ${nx.offsetHeight + 8}px)` : "";
  box.appendChild(t);
  setTimeout(() => t.remove(), kind === "bad" ? 9000 : 3000);
};
// 낭독기는 **이미 있던** live 영역에 글자가 들어올 때 읽는다 — 토스트와 같이 태어난 영역은 안 읽는다.
// 그래서 컨테이너를 첫 토스트보다 먼저 세워 두고(아래 즉시 호출), 토스트 자신은 role 을 안 단다.
function toastBox() {
  let box = $("toasts");
  if (!box) {
    box = document.createElement("div");
    box.id = "toasts";
    box.setAttribute("role", "status");
    box.setAttribute("aria-live", "polite");
    document.body.appendChild(box);
  }
  return box;
}
if (document.body) toastBox();

/* 단계 하나를 실행하는 버튼. 배포가 모양을 고른다 — 로컬에는 누를 서버가 없고
   (명령을 복사해 채팅에 붙인다), 호스팅에는 칠 채팅이 없다(버튼이 서버를 부른다).
   호스팅의 클릭은 애드온(dash.html)이 [data-run] 위임으로 잇는다. */
// .sec 는 두 배포에서 같은 뜻이다(이 행동은 주요가 아니다) — 그래야 머리줄 톤을
// 정하는 쪽이 배포마다 다른 규칙을 갖지 않는다.
// 라벨을 비우면 단계 용어표(stage.STAGE_LABELS)의 run 이 들어온다 — 뷰가 단계 이름을
// 글자로 옮겨 적지 않고도 버튼을 낼 수 있는 자리다. 여태 빈 라벨은 호스팅에서
// **글자 없는 버튼**으로 나갔다(로컬은 명령을 찍으니 티가 안 났다).
SM.api.act = (stage, label, primary = true) => {
  const t = label || window.stageRun(stage);
  return window.SM_HOSTED
    ? `<button class="go${primary ? "" : " sec"}" data-run="${esc(stage)}">${esc(t)}</button>`
    : `<button class="cmd${primary ? "" : " sec"}" data-run="${esc(stage)}" onclick="copyCmd(this)"
         >/capture ${esc(stage)}${siteArg()}</button>`;
};
// 사이트가 여럿인 로컬에서 이름 없는 명령은 doctor 가 되묻는다. 그래서 칩은 이름을 싣는다.
const siteArg = () => { const p = $("proj"); return p && p.value ? ` ${esc(p.value)}` : ""; };

/* 단계를 부르는 이름은 stage.STAGE_LABELS 한 벌이고, 조립이 window.__STAGES__ 로
   실어 보낸다(dashboard.py·app.py 둘 다). 뷰가 그 라벨을 글자로 옮겨 적으면
   파이썬이 이름을 바꿀 때 화면만 낡는다 — 실제로 index 가 "색인 다시 검사"에서
   "색인 다시 확인"으로 바뀌었을 때 뷰 두 곳이 옛 이름으로 남았다.
   run 은 실행 버튼용(화면 제목과 안 겹치게 다르게 부른다), t 는 단계 이름이다.

   "다시"는 정말 다시 할 때만 쓴다 — 이 사이트에서 이 단계가 한 번도 안 돈
   흔적(LAST_D.runs 에 그 kind 가 없음)이면 s.run("색인 다시 검사")이 아니라
   s.t("색인 검사")로 물러선다. 예전엔 늘 s.run 이라, 빈 화면 문구가 "아직
   검사하지 않았습니다"라고 말하는 바로 위에서 버튼은 "색인 다시 검사"라고
   말했다(블랙박스 8회차) — 하지도 않은 걸 "다시" 하라고 시킨 것이다.
   runs 는 gather() 가 담는 최근 40건뿐이지만(runVerdict·runFailed 와 같은
   한계) "한 번도 안 했는데 다시 하라"는 확실한 거짓말은 없어진다. */
SM.api.stageRun = id => {
  const s = (window.__STAGES__ || {})[id] || {};
  const everRan = ((LAST_D || {}).runs || []).some(r => r.kind === id);
  return (everRan && s.run) || s.t || id;
};
// 짝: 단계 **이름**. stageRun 이 "돌리는 버튼에 쓸 말"이라면 이쪽은 그 단계를
// 문장 안에서 부르는 이름이다("색인 다시 확인" ↔ "내 페이지 색인"). run 으로
// 물러서지 않는다 — 물러서면 문장 안에 버튼 문구가 들어앉는다.
SM.api.stageName = id => {
  const s = (window.__STAGES__ || {})[id] || {};
  return s.t || id;
};

/* ── 이 단계의 마지막 런이 실패했나 ────────────────────────────────────────
   유료 단계는 키·잔액 때문에 통째로 실패하고도 화면에는 "아직 안 했음"으로만
   보였다 — 자동 런 3회가 `errors=100` 만 남기고 죽었는데 화면이 아무 말도 안 한
   사고가 그것이다(docs/adr/0002). 그 뒤로 [기록]이 판정을 갖고, [백링크]·[경쟁 검색어]
   이 그중 "실패했나" 한 줄씩을 축약 복사해 들고 있었다 — 사본 셋이다.

   여기서 새 사실을 만들지 않는다 — **판정도 하지 않는다.** 런 한 건의 결과(완료·오류 N·
   건너뜀 N·중단·미완)는 서버가 run_verdict.judge 로 가려 페이로드의 runs[].verdict 에 싣는다
   ({cls: ok|skip|bad, label, why, errors, skipped, reason}). 셸은 그걸 돌려줄 뿐 runs.notes
   를 다시 읽지 않는다 — 예전엔 여기와 파이썬이 같은 정규식을 따로 들고 있었다.
   notes 는 사람에게 보여 주는 설명으로만 남았다(기록 화면이 옮겨 적는다).

   verdict 가 없는 행은 서버(또는 로컬 프록시)가 판정을 안 실은 것이다 — 성공으로 읽지 않고
   "판정 없음"(건너뜀 색)으로 드러낸다. 실패로도 읽지 않는다: 모르는 것을 단정하지 않는다. ── */
const NO_VERDICT = {cls: "skip", label: "판정 없음", errors: 0, skipped: 0, reason: "",
                    why: "서버가 이 런의 결과 판정을 보내지 않았습니다."};
SM.api.runVerdict = r => r ? (r.verdict || NO_VERDICT) : null;
/* 실패한 런 한 건을 돌려준다(없으면 null). 실패는 runVerdict 의 bad 하나다 —
   건너뜀은 실패가 아니다(한도·키 없음은 사용자가 고를 수 있는 상태다).
   d 를 안 주면 마지막으로 그린 페이로드를 본다. 화면이 자기 render 안에서 부를
   때는 그게 곧 자기가 받은 d 다(render 첫 줄이 LAST_D 를 세운다). */
SM.api.runFailed = (stage, d) => {
  const runs = ((d || LAST_D || {}).runs) || [];
  const r = runs.find(x => x.kind === stage);
  const v = window.runVerdict(r);
  return v && v.cls === "bad" ? r : null;
};

