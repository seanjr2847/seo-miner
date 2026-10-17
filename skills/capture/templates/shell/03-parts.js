// 최상위 const 는 global lexical 환경에 들어간다 — 다른 스크립트에서 이름으로는
// 보이지만 window 의 속성은 아니다. 뒤에 얹히는 층이 "셸이 있나"를 물을 자리를 준다.
window.SM = SM;

/* ── 공용 부품 (SM.ui) ──────────────────────────────────────────────────────
   화면마다 따로 짓던 작은 부품의 한 벌이다. 뷰는 이 interface 만 부르고, 같은 부품을
   자기 이름(AI_av·BLK_tip·CP_empty·…)으로 다시 짓지 않는다 — 한 곳을 고쳐도 나머지가
   낡던 것이 이 리포의 반복 문제였다(test_seams 의 부품 중복 검사가 지킨다).
     SM.ui.avatar(글자, {tone, size, hue, cls})   이니셜 아바타. 글자 고르기는 화면 몫이다
         (도메인·검색어·경로가 제각각이라). tone: a 녹청 · i 정보 · w 산화동 · n 중립 · us 우리(채움),
         hue(0~359)를 주면 도메인마다 고정인 색상환 면. size: 24·26·28(기본)·32·36. cls 는 화면 훅.
     SM.ui.hint(글, {title, id})       (i) 근거 풍선. title:false 면 브라우저 툴팁을 안 싣는다.
     SM.ui.empty(제목, 이유, 행동, 풍선)  빈 상태 상자(.empty). 행동·풍선은 비워도 된다.
     SM.ui.copyText(글) → Promise<bool>  클립보드. API 가 막히면(file://·비보안·권한 거부) 임시
         입력칸 복사로 물러선다. 결과 표시(버튼 글자·토스트)는 부른 쪽이 한다.
     SM.ui.sign(v, 영일 = "")          증감 부호: 양수 "+", 음수 "−"(진짜 마이너스 U+2212), 0 은 영일.
     SM.ui.act(단계, 라벨, 주요)         단계를 돌리는 손잡이(window.act — 배포에 맞춰 고른다).
   esc 는 호출 시점에 쓴다(이 블록은 esc 정의보다 앞이어도 된다). */
SM.ui = (() => {
  const AV_SIZE = {24: " s24", 26: " s26", 28: "", 32: " s32", 36: " s36"};
  const avatar = (ch, o = {}) => {
    const hue = Number.isFinite(o.hue);
    return `<span class="ui-av${AV_SIZE[o.size || 28] || ""} t-${hue ? "h" : o.tone || "n"}${
      o.cls ? " " + o.cls : ""}" aria-hidden="true" data-ch="${esc(ch)}"${
      hue ? ` style="--h:${o.hue}"` : ""}></span>`;
  };
  const hint = (t, o = {}) =>
    `<span class="hint"${o.id ? ` id="${o.id}"` : ""} tabindex="0" data-tip="${esc(t)}"${
      o.title === false ? "" : ` title="${esc(t)}"`} aria-label="${esc(t)}">i</span>`;
  const empty = (title, why, acts, tip) =>
    `<div class="empty"><b>${title}${tip ? hint(tip) : ""}</b><p>${why}</p>${acts || ""}</div>`;
  const sign = (v, zero = "") => v > 0 ? "+" : v < 0 ? "−" : zero;
  const act = (stage, label, primary = true) => window.act(stage, label, primary);
  async function copyText(text) {
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(text);
        return true;
      }
    } catch (e) { /* 권한 거부 — 아래 폴백으로 */ }
    const was = document.activeElement;
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.setAttribute("aria-hidden", "true");
    ta.style.cssText = "position:fixed;top:0;left:0;width:1px;height:1px;opacity:0";
    document.body.appendChild(ta);
    ta.select();
    ta.setSelectionRange(0, ta.value.length);
    let ok = false;
    try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
    ta.remove();
    if (was && was.focus) was.focus({preventScroll: true});
    return ok;
  }
  return {avatar, hint, empty, sign, act, copyText};
})();

/* ── 공용 서랍 (SM.drawer) ──────────────────────────────────────────────────
   개요의 오른쪽 서랍(.odr)과 같은 모양을 모든 화면이 한 벌로 쓴다. 상자는 body 맨 위에
   한 번만 서고(화면 상자 안이 아니다), 한 번에 하나만 열린다. 닫기(바깥·Esc·✕)와 열기
   전 초점 되돌리기도 여기서 한 벌이다. 화면은 안쪽 HTML 만 만든다.
     SM.drawer.open({title, sub, bodyHtml, footHtml, from, onClose})
     SM.drawer.paint({title, sub, bodyHtml, footHtml})   // 열린 채로 다시 칠함(초점 안 옮김)
     SM.drawer.close()   /   SM.drawer.isOpen()   /   SM.drawer.body()  // 본문 요소
   title·sub 는 글자(여기서 이스케이프), bodyHtml·footHtml 은 화면이 이스케이프한 HTML. */
SM.drawer = (() => {
  let from = null, onClose = null, open = false;
  const q = id => document.getElementById(id);
  const esc1 = s => String(s == null ? "" : s).replace(/[&<>"']/g,
    c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
  function els() {
    let dr = q("sdr");
    if (!dr) {
      document.body.insertAdjacentHTML("beforeend", `<div class="sdr-bg" id="sdr-bg" hidden></div>
        <aside class="sdr" id="sdr" role="dialog" aria-modal="true" aria-labelledby="sdr-t" hidden></aside>`);
      dr = q("sdr");
      q("sdr-bg").addEventListener("click", () => SM.drawer.close());
      dr.addEventListener("keydown", e => { if (e.key === "Escape") { e.stopPropagation(); SM.drawer.close(); } });
      dr.addEventListener("click", e => { if (e.target.closest(".sdr-x")) SM.drawer.close(); });
    }
    return dr;
  }
  function paint(o) {
    const dr = els();
    dr.innerHTML = `<div class="sdr-h"><div class="sdr-top">
        <div class="sdr-tt"><h2 id="sdr-t">${esc1(o.title)}</h2>${o.sub ? `<div class="sdr-sub">${esc1(o.sub)}</div>` : ""}</div>
        <button type="button" class="sdr-x" aria-label="닫기">✕</button></div></div>
      <div class="sdr-b">${o.bodyHtml || ""}</div>${o.footHtml ? `<div class="sdr-f">${o.footHtml}</div>` : ""}`;
  }
  return {
    open(o) {
      o = o || {};
      // 한 번에 하나 — 열려 있으면 앞 것의 onClose 를 부르되 초점 기억은 처음 연 줄을 지킨다.
      const keep = open ? from : (o.from || document.activeElement);
      if (open && onClose) { const f = onClose; onClose = null; try { f(); } catch (e) {} }
      from = keep; onClose = o.onClose || null;
      paint(o);
      els().hidden = false; q("sdr-bg").hidden = false; open = true;
      const x = q("sdr").querySelector(".sdr-x");
      if (x) x.focus({preventScroll: true});
    },
    paint(o) { if (open) paint(o || {}); },
    close() {
      if (!open) return;
      open = false;
      q("sdr").hidden = true; q("sdr-bg").hidden = true;
      const back = from, f = onClose;
      from = null; onClose = null;
      if (f) { try { f(); } catch (e) {} }
      if (back && back.isConnected && back.focus) back.focus({preventScroll: true});
    },
    isOpen() { return open; },
    body() { return open ? q("sdr").querySelector(".sdr-b") : null; },
  };
})();

/* ── 배포 손잡이 (로컬 ↔ 호스팅) ───────────────────────────────────────────
      명령 칩·기회 카드는 배포마다 하는 일이 다르다 — 로컬은 터미널이 있어
      "복사해서 붙여넣으세요"가 맞고, 호스팅은 터미널이 없어 그 자리에서 API 를
      불러야 한다. 예전에는 그 차이를 dash.html 이 원본이 **그려 놓은 DOM 을
      되짚어**(렌더된 버튼을 찾아 onclick 을 갈아 끼우고, 안 되는 칩은 지우는 식으로)
      만들었다 — MutationObserver 로 매번 다시 훑어야 했고, 못 잡는 경합(SM_HOSTED 가
      늦게 서는 것)과 죽은 버튼(.acts 의 클릭 위임 실패)이 둘 다 여기서 났다.
      이제 그 차이는 이 객체 하나에 있다 — 렌더 함수(renderGuide·viewCmds·oppActs)가
      그릴 때마다 이 훅을 불러 HTML 을 **직접** 받는다. 호스팅 애드온(dash.html)은
      자기 스크립트 맨 앞에서 이 객체를 통째로 갈아 끼울 뿐, 다 그려진 뒤에
      DOM 을 다시 손대지 않는다. */
SM.host = {
  // 안내 단계 칩 — 로컬 기본은 명령을 클립보드로 복사한다. 그 명령이 정말 그 단계를
  // 돌 때만(s.runnable, 판정은 stage.py) data-run 을 싣는다 — 띠가 같은 단계를 주요
  // 행동으로 들고 있으면 dimHead 가 이 표식을 보고 목록에서 뺀다.
  stepChip: s => s.cmd
    ? `<button class="cmd" data-stage="${esc(s.id)}"${s.runnable ? ` data-run="${esc(s.id)}"` : ""}
         onclick="SM.host.copy(this)">${esc(s.cmd)}</button>` : "",
  // 화면 제목 옆 칩 — 로컬 기본도 같은 명령을 복사한다. 사이트가 여럿인 로컬에서
  // 이름 없는 명령은 doctor 가 되묻는다. siteArg() 는 레일의 선택이 정해진 뒤에야
  // 값을 내므로, 사이트가 바뀌면 sm:site 가 viewCmds() 를 다시 불러 새로 그린다.
  viewChip: st => `<button class="cmd vh-cmd" data-run="${esc(st)}" onclick="SM.host.copy(this)"
      title="이 화면을 채우는 단계입니다. 눌러서 복사한 뒤 채팅에 붙여 넣습니다."
      >/capture ${esc(st)}${siteArg()}</button>`,
  copy(btn) { copyCmd(btn); },
  /* 기회 카드 접힌 줄의 실행 자리. 로컬에는 이 PC 가 있으니 고른 개발 도구를 그
     사이트 폴더에서 바로 연다(호스팅은 브라우저라 못 띄운다 — dash.html 이 안내로
     갈아 끼운다). 도구 라벨·설치 여부는 window.SETUP(=/api/doctor 응답)에서 읽는다:
     목록의 정본은 doctor.TOOLS 한 벌이고 화면은 사본을 안 갖는다.
     아직 고르기 전이면 버튼 대신 무엇이 없는지와 어디서 고르는지를 말한다.
     받는 것은 기회 한 건(o)이다 — [개요]의 묶인 줄(o.group, 서버 scoring.group_opportunities)
     이면 창은 대표 하나로 열고 '진행 중'은 묶인 id 전부에 찍어야 한다(상태 버튼
     oppIdsTo→setOpps 와 같은 범위). 대표 id 만 넘기면 새로고침 뒤 그 줄이 둘로 갈라진다. */
  oppBtn(o) {
    const s = window.SETUP || {}, t = (s.tools || []).find(x => x.id === s.tool);
    if (!t || !t.installed)
      return `<span class="badge warn">도구 없음</span>` +
        `<button class="go sm ghost" onclick="SM.touched=true;SM.show('settings')"` +
        `>설정에서 고르기</button>`;
    const ids = o.group ? o.group.ids : [o.id];
    // 열면 '진행 중'으로 옮긴다(run_tool) — 이 버튼이 곧 작업 시작이다.
    return `<button class="go sm" onclick="runTool(${o.id}, [${ids.join(",")}])"` +
      ` title="${ids.length > 1 ? `이 줄에 묶인 기회 ${ids.length}개를 함께 ` : "이 기회를 "}` +
      `진행 중으로 옮기고 ${esc(t.label)}를 이 사이트 폴더에서 엽니다"` +
      `>${esc(t.label)}로 열기</button>`;
  },
  /* 이번 달 할 일(play) 서랍 발판의 실행 자리 — [PR 만들기]. 누르면 이 PC 가 그 사이트의
     저장소 폴더에서 수정안을 적용하고 PR 을 연다(서버 play_pr — 헤드리스로 돈다).
     호스팅은 브라우저라 못 띄운다 — dash.html 이 "이 PC 에서 열기" 안내로 갈아 끼운다.
     할 수 있나(도구·gh·폴더)와 도는 일의 상태는 서버가 판정해 window.PLAY_PR 로 준다
     (playSync) — 화면은 그걸 그리기만 한다. 받는 것은 play 한 건(p, d.plays 의 한 줄). */
  playBtn(p) {
    if (!p || READONLY || p.id == null) return "";
    const s = window.PLAY_PR, j = s && s.jobs ? s.jobs[p.id] : null;
    if (j && j.status === "running")
      return `<button class="go sm" disabled title="저장소에서 고치고 PR 을 여는 중입니다. 끝나면 알려 드립니다."` +
        `>PR 만드는 중…</button>`;
    if (j && j.status === "done" && j.pr_url)
      return `<a class="go sm sec" href="${esc(j.pr_url)}" target="_blank" rel="noopener"` +
        ` title="이 할 일로 연 PR 입니다">PR 보기</a>`;
    if (p.status === "applied") return `<span class="badge">PR 만듦</span>`;
    if (s && !s.ready)
      return `<span class="badge warn" title="${esc(s.why)}">PR 준비 안 됨</span>` +
        (s.fix === "settings"
          ? `<button class="go sm ghost" onclick="SM.touched=true;SM.show('settings')" title="${esc(s.why)}"` +
            `>설정에서 폴더 정하기</button>`
          : `<span class="sub">${esc(s.why)}</span>`);
    const failed = j && j.status === "failed";
    // 실패 이유는 툴팁에만 두지 않는다 — 눌러서 보는 화면에서는 호버가 없다.
    return (failed ? `<span class="badge warn" title="${esc(j.error || "")}">지난번 실패</span>` +
        (j.error ? `<span class="sub">${esc(j.error)}</span>` : "") : "") +
      `<button class="go sm" onclick="prAsk(${+p.id})"` +
      ` title="저장소를 확인하고, 시작할지 물어봅니다. 머지는 직접 합니다."` +
      `>${failed ? "PR 다시 만들기" : "PR 만들기"}</button>`;
  },
  // 준비 상태·도는 일을 서버에서 받아 온다(로컬 전용 경로라 호스팅 SM.host 에는 없다).
  playSync() { prSync(); },
};

