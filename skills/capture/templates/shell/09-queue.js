/* ── 처리 큐 (SM.queue) ───────────────────────────────────────────────────
   「종류 집합을 받는 처리 큐」 — 열린 기회를 한자리에서 **처리**하는 부품 한 벌이다: 줄(목록)·보드,
   상태 거르개, 종류 칩(도넛), 찾기, 스포트라이트, 오른쪽 서랍(근거·할 일·요청문), 상태 바꾸기,
   CSV, 관찰 중 한 줄, 심사 대기 안내. 구현은 여기 한 곳이다 — 화면은 줄도 서랍도 짓지 않는다.

   선언은 화면 view-def 의 `queue` 한 곳이다:
     "queue": {"kinds": "*"}              모든 종류를 받는다([개요])
     "queue": {}  또는 {"kinds": "see"}   이 화면이 see 로 가리키는 종류만 — 종류 목록은 서버가
                                           페이로드(d.queue_kinds_by_view)로 준다. 정본은
                                           scoring.KINDS 의 see 이고 여기에 사본을 두지 않는다.
     선택: "mode": 화면 modes 의 탭 id(그 탭에 선다), "title": 카드 제목
   큐 카드는 화면 상자에 셸이 세운다(섹션 id q-<화면>). 화면이 자리를 정하고 싶으면 마크업에
   `data-queue-host="<화면 id>"` 요소를 두면 셸이 그 안을 채운다([개요]의 격자 칸). 종류 도넛은
   `data-q-slot="kinds"` 요소가 있을 때만 거기 선다 — 없으면 목록 위 칩 줄이다.
   데이터는 render() 가 뷰보다 먼저 feed(d) 로 넣는다 — 화면 VIEW 는 큐 상태를 읽기만 한다.
   박제본(READONLY)에서는 상태 버튼·거르개·서랍이 사라지고 CSV·펼치기만 남는다. */
SM.queue = (() => {
  const REG = {};                              // 화면 id → 큐
  const QUEUE_PAGE = 10;                       // 처음 보이는 줄 수(brief.FIRST_PAGE 와 같은 수 — test_seams 128)
  const BOARD_PAGE = 12;
  const COLS = ["new", "acked", "done"];       // 보드 칸(상태) — 이름은 OPP_LABEL
  const VARS_SHOWN = 4;
  const TONES = [["--accent", 1], ["--info", 1], ["--accent", .5], ["--info", .5], ["--sub", .5], ["--sub", .26]];
  const toneCss = ([v, a]) => a === 1 ? `var(${v})` : `color-mix(in srgb, var(${v}) ${a * 100}%, var(--card))`;
  // 읽는 쪽이 셸 스크립트의 READONLY 선언보다 먼저 불릴 수 있다 — 모르면 읽기 전용으로 둔다.
  const RO = () => { try { return READONLY; } catch (e) { return true; } };
  let printing = false, dragId = null;
  addEventListener("beforeprint", () => { printing = true; });
  addEventListener("afterprint", () => { setTimeout(() => { printing = false; }, 0); });
  let pref = (() => {                           // 줄/보드 — 큐 전부가 같은 값을 쓴다(탭 하나가 아니라 브라우저)
    try { return localStorage.getItem("sm:oppview") === "board" ? "board" : "list"; }
    catch (e) { return "list"; }
  })();

  // 큐를 부르는 손잡이 — 인라인 핸들러는 요소 하나만 넘긴다. 큐 카드·서랍·종류 칸이 자기 화면 id 를 달고 있다.
  SM.api.QU = x => {
    if (typeof x === "string") return REG[x];
    const n = x && x.closest ? x.closest("[data-queue-host],[data-queue-of]") : null;
    return n ? REG[n.dataset.queueHost || n.dataset.queueOf] : null;
  };

  // 줄 하나의 대상·종류 — 같은 지면의 변형이 한 줄로 묶여 오고(o.group), 같은 검색어의 여러 종류도 묶인다.
  const targetsOf = o => o.group ? [...new Set(o.group.variants.map(v => v.target))] : [o.target];
  const kindsOf = o => o.group && o.group.kinds && o.group.kinds.length
    ? o.group.kinds.map((k, i) => [k, o.group.labels[i] || k]) : [[o.kind, o.label || o.kind]];
  const kindPill = (o, [k, l], D) => `<span class="kind${isDefensive(k === o.kind ? o : {kind: k}) ? " k-def" : ""}">${esc(l)}</span>`;
  // 심사를 거치지 않는 종류 — 판정 없이 바로 기회가 된다(정본 scoring.TRIAGE_KINDS 의 바깥).
  // 서버가 준 심사 종류 표(d.triage_kinds)에서 읽는다 — 옛 박제본에는 없어 그때는 안 단다.
  const direct = (o, D) => {
    const t = D && D.triage_kinds;
    return !!t && kindsOf(o).every(([k]) => !t.includes(k));
  };
  const directBadge = (o, D) => direct(o, D)
    ? `<span class="badge" title="심사를 거치지 않는 종류입니다. 검색어·주제를 가리는 판정 없이 바로 기회가 됩니다. 고칠 곳이 정해져 있습니다.">바로 고칠 것</span>` : "";

  // 서버가 접어 보낸 줄(d.opp_groups)을 카드 객체로 편다. 줄 하나 = 대표 기회 + 묶음.
  // 대표 객체는 복사해서 점수·상태만 묶음 것으로 갈아 끼운다 — 원본(d.opps)은 다른
  // 화면이 검색어 하나로 찾아 쓰는 것(oppOf)이라 건드리지 않는다.
  // 옛 박제본에는 opp_groups 가 없다 — 그때처럼 기회 하나가 한 줄이다.
  function linesOf(d, byId) {
    if (!d.opp_groups) return d.opps || [];
    return d.opp_groups.filter(g => byId[g.lead]).map(g => g.ids.length > 1
      ? Object.assign({}, byId[g.lead], {score: g.score, status: g.status, group: g})
      : byId[g.lead]);
  }

  class Queue {
    constructor(view, cfg) {
      this.view = view; this.cfg = cfg || {};
      this.all = this.cfg.kinds === "*";
      this.root = null; this.D = {}; this.opps = []; this.byId = {}; this.kindSet = null;
      this.kf = null;                // 종류 칩으로 고른 종류
      this.q = "";                   // 찾는 말
      this.st = null;                // 상태 묶음 — null 이면 아직 안 정한 것(첫 적재 때 정한다)
      this.n = QUEUE_PAGE;           // 지금 보이는 줄 수
      this.held = 0; this.heldPages = 0;
      this.colN = {}; this.kall = false; this.ksig = "";
      this.dr = null; this.drTab = "ev"; this.drFrom = null; this.ext = null;   // 서랍
    }

    // ── 자리 ──────────────────────────────────────────────────────────────
    box() { return $("view-" + this.view); }
    el(n) { return this.root && this.root.querySelector(`[data-q="${n}"]`); }
    slot() { const b = this.box(); return b && b.querySelector(`[data-q-slot="kinds"]`); }
    isBoard() { return pref === "board" && !RO(); }

    mount() {
      const box = this.box();
      if (!box) return false;
      if (this.root && this.root.isConnected) return true;
      let host = box.querySelector(`[data-queue-host="${this.view}"]`);
      if (!host) {
        host = document.createElement("section");
        host.className = "card qhost";
        host.id = "q-" + this.view;
        host.dataset.queueHost = this.view;
        if (this.cfg.mode) host.dataset.mode = this.cfg.mode;
        const after = box.querySelector(":scope > .vban") || box.querySelector(":scope > .vh");
        if (after) after.after(host); else box.prepend(host);
      }
      host.dataset.queueHost = this.view;
      host.innerHTML = `<div class="ovh"><div class="ovh-t"><h2>${esc(this.cfg.title || "개선 기회")}</h2>
          <p class="sub" data-q="sub"></p></div><div class="tools" data-q="tools"></div></div>
        <div class="ovs" data-q="spot" hidden></div>
        <div class="qchips" data-q="chips" hidden></div>
        <div class="qlist" data-q="list"></div><div data-q="hold"></div>`;
      this.root = host;
      const slot = this.slot();
      if (slot) {
        slot.dataset.queueOf = this.view;
        slot.innerHTML = `<div class="ovh"><div class="ovh-t"><h2>종류별 기회</h2>
            <p class="sub">종류를 누르면 목록이 그 종류만 남고, 다시 누르면 풀립니다.</p></div></div>
          <div class="ovk-body"><div class="ovk-donut" data-q="donut"></div><div class="qk-list" data-q="chipl"></div></div>`;
      }
      return true;
    }

    // ── 데이터 ─────────────────────────────────────────────────────────────
    feed(d) {
      if (!this.mount()) return;
      this.D = d;
      this.kindSet = this.all ? null : new Set((d.queue_kinds_by_view || {})[this.view] || []);
      const byId = {};
      for (const o of d.opps || []) byId[o.id] = o;
      this.byId = byId;
      const lines = linesOf(d, byId);
      this.opps = this.kindSet ? lines.filter(o => kindsOf(o).some(([k]) => this.kindSet.has(k))) : lines;
      const held = (d.opps || []).filter(o => window.oppStMatch(o, "hold") && (!this.kindSet || this.kindSet.has(o.kind)));
      this.held = held.length;
      this.heldPages = new Set(held.map(o => o.hold.page)).size;
      // 첫 적재에서만 기본값을 정한다 — 사람이 고른 필터를 재적재로 되돌리지 않는다.
      // 완료·제외는 끝난 일이라 목록에 안 낀다. 박제본은 그때의 기록 자체가 볼거리라 전부 남긴다.
      if (this.st === null) this.st = RO() ? "" : "open";
      this.paintSub(d);
      this.render(QUEUE_PAGE);
    }
    paintSub(d) {
      const tip = RO()
        ? "카드를 펼치면 어느 페이지가 걸렸는지와 할 일이 나옵니다. 막대는 점수(100점 만점)이고, " +
          "확인·완료·제외 표시는 이 기록을 남긴 시점의 상태라 여기서는 바꿀 수 없습니다."
        : "카드를 펼치면 어느 페이지가 걸렸는지와 할 일이 나옵니다. 막대는 점수(100점 만점)이고, " +
          "길수록 먼저 손댑니다.";
      // 서버는 점수 상위 N 건만 싣는다(dashboard._axis_opps 의 limit). 위 띠의 「개선 기회」는
      // 전수(opps_total)라 말이 안 맞는다 — 잘렸으면 잘렸다고 적는다. 줄 수는 적지 않는다.
      const gs = d.opp_groups || [];
      const capped = (d.opps || []).length < (d.opps_total || 0)
        ? ` 기회 ${num(d.opps_total)}건 중 점수 높은 ${num(d.opps.length)}개까지 이 화면에 ` +
          (gs.some(g => g.ids.length > 1) ? "오고, 같은 지면끼리는 한 줄로 묶입니다." : "옵니다.")
        : "";
      this.el("sub").innerHTML = (RO() ? "이 기록을 남긴 시점의 기회입니다." : "점수가 높은 것부터 봅니다.")
        + SM.ui.hint(tip) + esc(capped);
    }

    // ── 거르기 ──────────────────────────────────────────────────────────────
    // 상태로 먼저 가른다 — 칩 개수도 이 묶음을 세야 "CTR 미달 12" 를 눌렀는데 8건이 나오는 일이 없다.
    // 보드에서는 상태 거르개 대신 칸이 상태를 가른다 — 칸에 오르는 상태만 센다. 관찰 중(o.hold)은
    // 칸에 안 올린다(목록 기본값과 같은 규칙 — 몇 건을 뺐는지는 holdLine 이 말한다).
    byStatus() {
      return this.opps.filter(o => this.isBoard()
        ? COLS.includes(o.status) && window.oppStMatch(o, "") : window.oppStMatch(o, this.st));
    }
    varText(o) { return o.group ? o.group.variants.map(v => v.target).join(" ") : ""; }
    filtered() {
      const q = this.q.trim().toLowerCase();
      return this.byStatus().filter(o =>
        (!this.kf || kindsOf(o).some(([k]) => k === this.kf)) &&
        (!q || `${o.target} ${o.reasoning || ""} ${this.varText(o)}`.toLowerCase().includes(q)));
    }
    isFiltered() { return !!(this.q.trim() || this.kf || (this.st && this.st !== "open")); }
    top() {
      if (this.isBoard())
        return this.opps.find(o => o.status === "new") || this.opps.find(o => window.oppStMatch(o, "open")) || null;
      return this.filtered()[0] || null;
    }
    find(v) { this.q = v; this.n = QUEUE_PAGE; this.list(); }
    status(v) { this.st = v; this.n = QUEUE_PAGE; this.list(); }
    more(n) { this.n = n; this.list(); }
    // [필터 지우기]는 말 그대로 전부 보여 준다 — 첫 화면의 기본값(아직 안 함)이 아니다.
    reset() { this.q = ""; this.st = "all"; this.kf = null; this.render(QUEUE_PAGE); }
    // 거르개까지 다시 그린다 — status 는 목록만 그려서 <select> 의 선택이 안 따라온다.
    closed(st) { this.st = st || "closed"; this.kf = null; this.render(QUEUE_PAGE); }
    filterKind(el) {
      const k = el.dataset.k;
      this.kf = this.kf === k ? null : k;
      this.render(QUEUE_PAGE);
    }

    // ── 빈 상태 ──────────────────────────────────────────────────────────────
    /* 심사 대기 — 이 목록은 심사에서 [작업]으로 보낸 것만 싣는다(서버 db.gate_sql). 목록이 비었는데
       심사에 쌓여 있으면 "다 처리했다"가 아니라 "고를 차례"다 — aitierlist 는 미판정 113건을 두고
       "남은 기회가 없습니다 · 쌓인 것을 전부 처리했습니다"를 봤다. 수는 서버가 센다
       (d.triage_pending). 없거나 0 이면(옛 박제본·다 판정함) 빈 문자열 — 원래 빈 상태로 물러선다.
       모든 종류를 받는 큐만 말한다 — 심사 대기는 재료(검색어·주제·도메인) 단위라 종류 몇 개로
       좁힌 화면의 몫으로 쪼개 세면 거짓 숫자가 된다. */
    triageBox() {
      if (!this.all) return "";
      const t = this.D.triage_pending;
      if (!t || !t.n) return "";
      const hot = t.hot ? `그중 점수 ${num(t.hot_min)}점 이상이 ${num(t.hot)}건입니다. ` : "";
      // 어느 재료가 쌓였는지는 서버가 센다(t.units — 이름표 정본 scoring.TRIAGE_UNITS)
      const parts = (t.units || []).filter(u => u.n).map(u => `${esc(u.label)} ${num(u.n)}`).join(" · ");
      return `<div class="empty"><b>심사할 재료가 ${num(t.n)}건 남았습니다</b>
        <p>${parts ? `${parts}. ` : ""}${hot}심사에서 [작업]으로 보낸 것이 여기 개선 기회로 섭니다.</p>
        ${RO() ? "" : `<div class="acts"><button class="go" onclick="SM.touched=true;SM.show('triage')">심사하기</button></div>`}</div>`;
    }
    // 다 처리한 뒤의 빈 상자 — 끝난 것이 실제로 있는 상태 묶음으로 보낸다. 전부 「자동 종료」인
    // 사이트에서 [완료·제외]으로 보내면 거기서 또 빈 목록을 만났다. 이름은 거르개와 같은 말(OPP_ST_OPT).
    doneBox() {
      const tri = this.triageBox();
      if (tri) return tri;
      const n = st => this.opps.filter(o => window.oppStMatch(o, st)).length;
      const st = ["closed", "resolved"].find(s => n(s) > 0);
      const name = st && (window.OPP_ST_OPT.find(([v]) => v === st) || [, st])[1];
      return `<div class="empty ok"><b>남은 기회가 없습니다</b>
        <p>쌓인 것을 전부 처리했습니다.${name ? ` 끝난 것은 [${esc(name)}]에 그대로 있습니다.` : ""}</p>
        ${name ? `<div class="acts"><button class="go sec" onclick="QU(this).closed('${st}')">${esc(name)} 보기</button></div>` : ""}</div>`;
    }
    emptyBox() {
      return this.triageBox() || (this.all
        ? `<div class="empty"><b>아직 기회가 없습니다</b>
            <p>수집한 숫자에서 손댈 곳을 점수 순으로 찾습니다.</p></div>`
        : `<div class="empty"><b>이 화면에서 처리할 기회가 아직 없습니다</b>
            <p>이 화면이 맡은 종류의 기회가 서면 여기 점수 순으로 섭니다.</p></div>`);
    }

    // ── 그리기 ──────────────────────────────────────────────────────────────
    // 도구줄·칩은 데이터가 바뀔 때만 다시 그린다 — 매 키 입력마다 갈아치우면 찾기 칸이
    // 사라져 포커스를 잃는다. 거르기는 아래 목록만 다시 그린다(list).
    render(n) {
      this.n = n;
      const tbox = this.el("tools");
      if (!this.opps.length) {
        if (tbox) tbox.innerHTML = "";
        this.root.toggleAttribute("data-board", false);
        this.kindsHide(); this.ksig = "";
        this.el("spot").hidden = true;
        this.hero = null;
        this.el("hold").innerHTML = "";
        // 칩은 놓지 않는다 — 이 상태에서는 [개요]의 띠가 [기회 분석]을 주요 행동으로 들고 있고,
        // 화면 머리에도 같은 명령이 있다(D14: 한 화면에 같은 칩 세 번).
        this.el("list").innerHTML = this.emptyBox();
        return;
      }
      const ro = RO();
      const stn = s => this.opps.filter(o => window.oppStMatch(o, s)).length;
      // 박제본은 손댈 수 없는 기록이다 — 찾기·거르기는 그때 그 사람이 쓰던 손잡이고, 종이로 나가면
      // 뜻이 없다. CSV 는 남긴다(브라우저 안에서 끝나는 일이다). 한 번에 다 보이는 목록(첫 화면에
      // 다 들어가는)에는 찾기 칸과 종류 칩을 안 세운다 — 네 줄을 거르려고 손잡이 다섯 개를 먼저
      // 읽게 된다. 상태 거르개는 남긴다: 끝낸 것을 보는 길이 그것뿐이다.
      const few = this.opps.length <= QUEUE_PAGE;
      // 손잡이가 없는데 거른 조건이 남으면 풀 길이 없다(많던 사이트에서 옮겨 온 경우).
      if (few && !ro) { this.q = ""; this.kf = null; }
      if (tbox) tbox.innerHTML = ro
        ? `<span class="n" data-q="n"></span>
           <button class="go sm sec" onclick="QU(this).csv()">CSV 내려받기</button>`
        : this.viewSeg() + (few ? "" : `<input type="search" value="${esc(this.q)}" oninput="QU(this).find(this.value)"
            placeholder="검색어·주소로 찾기" aria-label="기회 찾기">`) + `
          ${this.isBoard() ? "" : window.oppStSelect(this.st, "QU(this).status", stn)}
          <span class="n" data-q="n"></span>
          <button class="go sm sec" onclick="QU(this).csv()"
            title="지금 걸러 놓은 목록 그대로 내려받습니다">CSV 내려받기</button>`;
      // 보드는 칸이 셋이라 대기열 카드가 전폭이 된다(격자가 이 표식을 본다 — 개요 CSS 의 :has).
      this.root.toggleAttribute("data-board", this.isBoard());
      this.list();
    }
    viewSeg() {
      return `<span class="kseg" role="group" aria-label="보기 방식">${
        [["list", "목록"], ["board", "보드"]].map(([v, l]) =>
          `<button type="button" class="chip${pref === v ? " on" : ""}" aria-pressed="${pref === v}"
            onclick="QU(this).setView('${v}')">${l}</button>`).join("")}</span>`;
    }
    setView(v, quiet) {
      pref = v;
      try { localStorage.setItem("sm:oppview", v); } catch (e) { /* 사생활 창 — 이번만 */ }
      if (!quiet) this.render(QUEUE_PAGE);
    }

    list() {
      // 거르개·상태가 바뀔 때마다 알린다 — 같은 화면의 띠가 지금 보이는 목록을 따라가게.
      document.dispatchEvent(new CustomEvent("sm:queue", {detail: {view: this.view}}));
      this.drawerSync();
      this.chips();
      this.holdLine();
      this.spot();
      const full = this.filtered();
      // 스포트라이트에 올린 기회는 목록에서 뺀다 — 같은 기회를 위아래로 두 번 읽지 않게. 목록은 2번째부터다.
      // 박제본·인쇄는 서랍이 없어 제자리 펼침이 유일한 길이라 그대로 둔다.
      const list = this.hero ? full.filter(o => o !== this.hero) : full;
      // 개수를 두 번 적지 않는다 — 상태 거르개가 이미 "아직 안 함 (2)" 로 말한다. 그 위에 무엇이 더
      // 걸렸을 때(찾는 말·종류 칩)만 몇 건이 남았는지 적는다. 박제본에는 거르개가 없다 — 거기서는
      // 이 자리가 개수를 말하는 유일한 곳이다.
      const narrowed = full.length !== this.byStatus().length;
      const nEl = this.el("n");
      if (nEl) nEl.textContent = narrowed ? `${full.length}건 / 전체 ${this.opps.length}건`
        : RO() ? `${this.opps.length}건` : "";
      const out = this.el("list");
      // 보드는 비어도 칸을 세운다 — 칸이 곧 끌어 놓을 자리다.
      if (this.isBoard()) return this.board(list);
      // 기본 상태(아직 안 함)만 걸린 채로 비었으면 그건 거른 탓이 아니라 다 처리한 것이다 — 여기서
      // "조건을 풀라"고 하면 안 건 조건을 풀라는 말이 된다.
      // 스포트라이트가 유일한 기회를 들고 있으면 목록이 비는 게 맞다 — 「모두 처리했습니다」가 아니다.
      if (!list.length && this.hero) return out.innerHTML = "";
      if (!list.length)
        return out.innerHTML = this.isFiltered()
          ? `<div class="empty"><b>거른 조건에 맞는 기회가 없습니다</b>
            <p>찾는 말이나 상태를 좁혀 놓았습니다. 조건을 풀면 전체가 다시 보입니다.</p>
            <div class="acts"><button class="go sec" onclick="QU(this).reset()">조건 지우기</button></div></div>`
          : this.doneBox();
      const rest = list.length - this.n;
      out.innerHTML = this.tagLine(list) + `<div class="ovrows">${list.slice(0, this.n).map(o => this.row(o)).join("")}</div>`
        + (rest > 0 ? `<div class="acts opp-more">
             <button onclick="QU(this).more(${this.n + 12})">${Math.min(12, rest)}건 더 보기 (남은 ${rest}건)</button></div>` : "");
    }

    // 카드 자체가 <details> 다 — 어디를 눌러도 펼쳐지고, 키보드·스크린리더가 공짜로 따라온다. 점수·
    // 트리아지·펼침·배지는 셸의 한 벌(oppScore/oppActs/oppPanel/gaBadge)이다 — 다른 화면도 같은
    // 부품으로 같은 카드를 만든다. 근거 줄만 oppWhy 를 안 쓰고 직접 짠다: 이 카드는 날짜를 라벨
    // 옆에 두고 상태(.st)를 오른쪽 끝에 세우는데 oppWhy 는 상태를 라벨 옆에 넣기 때문이다.
    // id 는 페이로드에 이미 있다 — data- 로 내보낸다(호스팅 애드온이 onclick 문자열을 정규식으로
    // 되짚던 자리다). 묶인 줄(o.group — 같은 지면의 변형 검색어)은 대표 검색어를 표제로 세우고
    // 나머지를 그 밑에 한 줄로 흘린다. 전부와 "왜 한 줄인지"는 펼침 패널(셸 oppGroup)이 말한다.
    // data-ids 는 묶인 id 전부다 — [기록]이 변형 쪽 id 로 이 카드를 찾아온다.
    // 접힌 줄에는 「할 일」 배지를 안 단다 — 기본 거르개(아직 안 함)에서는 거의 모든 줄이 같은 말이라
    // 읽을 것만 늘린다. 상태를 바꾸는 손잡이와 실행 자리(oppActs), 원래 화면으로 가는 길(see)은 셸
    // 패널의 행동 줄(맨 위)에 넘긴다 — 스무 줄에 버튼 마흔 개가 서지 않게, 그러면서 펼치면 맨 먼저 손에 닿게.
    row(o) {
      const n = targetsOf(o).length;
      const see = this.see(o);
      return `<details class="opp" data-opp="${o.id}" data-ids="${(o.group ? o.group.ids : [o.id]).join(" ")}">
        <summary>
          ${oppScore(o)}
          <div>
            <div class="ovt">
              <div class="target">${esc(targetLabel(o.target))}</div>
              ${kindsOf(o).map(kl => kindPill(o, kl)).join("")}
              ${n > 1 ? `<span class="badge" title="같은 지면이라 한 줄로 묶었습니다. 펼치면 전부와 묶은 이유가 나옵니다."
                >검색어 ${n}개</span>` : ""}
              ${directBadge(o, this.D)}
              ${window.gaBadge(o)}
              ${window.madeBadge(o)}
              ${window.holdBadge(o)}
            </div>
            ${this.vars(o)}
            ${o.reasoning ? `<div class="why">${esc(o.reasoning)}</div>` : ""}
            ${window.oppClosedWhy(o)}
          </div>
          ${o.status === "new" ? "" : window.oppStBadge(o, "div")}
        </summary>
        ${oppPanel(o, oppActs(o) + (see ? `<div class="acts">${see}</div>` : ""))}${this.more_(o)}
      </details>`;
    }
    vars(o) {
      const rest = targetsOf(o).filter(t => t !== o.target);
      if (!rest.length) return "";
      const more = rest.length - VARS_SHOWN;
      return `<div class="vars">함께: ${rest.slice(0, VARS_SHOWN).map(esc).join(" · ")}${
        more > 0 ? ` 외 ${more}개` : ""}</div>`;
    }
    /* 한 줄에 묶인 나머지 종류의 근거·할 일 — 대표 종류는 위 패널이 말한다. 종류마다 그
       종류에서 점수가 가장 높은 기회(이 화면에 실려 온 것)를 차례로 든다. */
    more_(o) {
      const ks = kindsOf(o).slice(1);
      if (!ks.length) return "";
      const members = o.group.ids.map(id => this.byId[id]).filter(Boolean);
      return ks.map(([k, l]) => {
        const m = members.find(x => x.kind === k);
        if (!m) return "";
        const p = m.play || {};
        return `<div class="det"><p class="detw"><b>${esc(l)}</b>${m.reasoning
          ? ` · ${esc(m.reasoning)}` : ""}</p>${playList(p.what, p.acts, "")}</div>`;
      }).join("");
    }
    /* 큐는 여러 화면의 기회를 모아 보는 곳일 수 있다 — 모은 줄마다 그 검색어를 자세히 보는 원래 자리로
       가는 길이 있어야 한다. 어느 화면·어느 섹션인지는 서버가 준 표(kind_views, 정본은 scoring.KINDS 의
       see)를 그대로 읽는다. 여기 옮겨 적으면 화면 목록이 두 벌이 된다. 따로 볼 화면이 없는 종류이거나,
       이 배포(박제본·호스팅)에 그 화면이 없거나, 지금 그 화면 안이면 링크를 안 단다. */
    see(o) {
      const see = (window.KIND_VIEWS || {})[o.kind];
      if (!see || see[0] === this.view || SM.at(see[0]) < 0) return "";
      const v = SM.views[SM.at(see[0])];
      return `<button class="go sm ghost" onclick="SM.queue.goSee('${esc(see[0])}','${esc(see[1] || "")}')"
        >${esc(v.title)} 화면에서 자세히 보기 →</button>`;
    }

    /* 관찰 중이라 뺀 기회 — 목록에서 조용히 사라지면 "어디 갔지"가 된다. 몇 건을 왜 뺐는지와
       모아 보는 곳을 목록 바로 아래 한 줄로 말한다. 판정은 서버(o.hold, d.holds)가 했다.
       [관찰 중] 화면이 이 배포에 없으면(옛 박제본) 거르개를 그 묶음으로 돌린다. */
    holdLine() {
      const el = this.el("hold");
      if (!el) return;
      // 세는 단위는 [관찰 중] 화면과 같다 — 묶은 줄이 아니라 페이로드에 실린 기회 하나하나.
      const n = this.held;
      if (!n || this.st === "hold" || this.st === "all") return el.innerHTML = "";
      const pages = this.heldPages;
      const go = SM.at("hold") >= 0 ? `SM.show('hold')` : `QU(this).closed('hold')`;
      // 거르개의 [관찰 중 (N)]은 이 목록의 줄(같은 지면끼리 묶음) 수라 기회 수와 다르다 — 둘을 같이 적는다.
      const lines = this.opps.filter(o => window.oppStMatch(o, "hold")).length;
      el.innerHTML = `<p class="opp-held">고친 지 얼마 안 된 페이지${pages ? ` ${pages}곳` : ""}에 걸린 기회
        ${n}건${lines && lines !== n ? `(이 목록에서 ${lines}줄)` : ""}은 효과를 지켜보는 동안 이 목록에서 뺐습니다.
        ${RO() ? "" : `<button class="go sm ghost" onclick="${go}">관찰 중인 기회 보기 →</button>`}</p>`;
    }

    /* [자동 종료]를 고르면 왜 닫혔나를 갈래별로 먼저 센다 — 줄마다 배지가 말해도 252줄을
       내려가며 세어 볼 사람은 없다. 예전 이름 '저절로 풀림' 252건은 "많이 좋아졌다"로 읽혔는데
       실제로 해결된 것은 20건이었다(theotherskin, 2026-10-17). 갈래 말은 셸의 OPP_TAG_LABEL 한 벌. */
    tagLine(list) {
      if (this.st !== "resolved") return "";
      const c = {};
      for (const o of list) if (window.OPP_TAG_LABEL[o.status_tag]) c[o.status_tag] = (c[o.status_tag] || 0) + 1;
      const parts = Object.keys(window.OPP_TAG_LABEL).filter(t => c[t])
        .map(t => `<span title="${esc(window.OPP_TAG_TIP[t])}">${esc(window.OPP_TAG_LABEL[t])} <b>${num(c[t])}</b></span>`);
      if (!parts.length) return "";
      // .opp-held 는 flex 라 글자·span 이 따로 줄을 탄다 — 한 문단으로 흐르게 span 하나로 감싼다.
      return `<p class="opp-held"><span>도구가 새 데이터를 보고 닫은 ${num(list.length)}줄: ${parts.join(" · ")}.
        좋아진 것은 「${esc(window.OPP_TAG_LABEL.better)}」뿐입니다. 나머지는 다른 줄과 겹쳤거나, 판정 기준이 바뀌었거나,
        더 판단할 수 없게 된 것입니다.</span></p>`;
    }

    // ── 종류 칩 ─────────────────────────────────────────────────────────────
    /* 종류별 기회 — 도넛은 장식이 아니라 종류 칩(filterKind)의 그림판이다. 범례 한 줄이 칩이고
       (data-k·aria-pressed 그대로), 누르면 목록이 그 종류만 남는다. 종류를 다 조각내면 안 읽히므로
       조각은 많은 순 다섯 + 기타이고, 범례는 여섯 줄에 [모두 보기]가 나머지를 편다. 도넛 칸
       (data-q-slot="kinds")이 없는 화면은 목록 위 칩 줄만 선다. 개수는 지금 보고 있는 상태 묶음
       기준이다(한 줄이 여러 종류일 수 있어 합이 줄 수보다 클 수 있다). 색은 순서를 말할 뿐 뜻이
       없다 — 방어형(산화동)은 목록의 종류 알약이 이미 말한다. */
    kindsHide() {
      const slot = this.slot();
      if (slot) slot.hidden = true;
      const c = this.el("chips");
      if (c) { c.hidden = true; c.innerHTML = ""; }
    }
    kindsAll() { this.kall = !this.kall; this.chips(); }
    chips() {
      const slot = this.slot();
      const host = slot ? slot.querySelector(`[data-q="chipl"]`) : this.el("chips");
      if (!host) return;
      const counts = {};
      for (const o of this.byStatus()) for (const [k] of kindsOf(o)) {
        if (this.kindSet && !this.kindSet.has(k)) continue;
        counts[k] = (counts[k] || 0) + 1;
      }
      if (this.kf && !counts[this.kf]) this.kf = null;  // 상태·사이트 전환 시 잔존 필터
      const kinds = Object.keys(counts).sort((a, b) => counts[b] - counts[a]);
      // 박제본·한 번에 다 보이는 목록·종류 하나뿐인 목록에는 칩을 안 세운다.
      const off = RO() || this.opps.length <= QUEUE_PAGE || kinds.length < 2;
      if (slot) { slot.hidden = off; const c = this.el("chips"); if (c) c.hidden = true; }
      else host.hidden = off;
      if (off) { host.innerHTML = ""; this.ksig = ""; return; }
      const name = k => (window.KIND_LABELS || {})[k] || k;
      const tone = i => TONES[Math.min(i, TONES.length - 1)];
      const idx = k => kinds.indexOf(k);
      const rows = kinds.filter((k, i) => this.kall || i < 6 || k === this.kf);
      const mx = counts[kinds[0]] || 1;
      if (!slot) {                    // 도넛 없는 화면 — 칩 줄
        host.innerHTML = rows.map(k => `<button type="button" class="chip${this.kf === k ? " on" : ""}"
          data-k="${esc(k)}" onclick="QU(this).filterKind(this)" aria-pressed="${this.kf === k}">${esc(name(k))} <b>${counts[k]}</b></button>`).join("")
          + (kinds.length > 6 ? `<button type="button" class="gov" onclick="QU(this).kindsAll()">${
              this.kall ? "접기" : `모두 보기 (${kinds.length}종)`}</button>` : "");
        return;
      }
      host.innerHTML = rows.map(k => `<button type="button" class="chip${this.kf === k ? " on" : ""}"
          data-k="${esc(k)}" onclick="QU(this).filterKind(this)" aria-pressed="${this.kf === k}">
          <i class="dot" style="background:${toneCss(tone(idx(k)))}"></i><span class="nm">${esc(name(k))}</span>
          <b>${counts[k]}</b><span class="bar"><em style="width:${Math.max(4, counts[k] / mx * 100).toFixed(0)}%;background:${
            toneCss(tone(idx(k)))}"></em></span></button>`).join("")
        + (kinds.length > 6 ? `<button type="button" class="gov ovk-all" onclick="QU(this).kindsAll()">${
            this.kall ? "접기" : `모두 보기 (${kinds.length}종)`}</button>` : "");
      // 도넛은 조각이 달라질 때만 다시 세운다 — 찾기 칸에 글자를 칠 때마다 갈아치우지 않는다.
      const head = kinds.slice(0, 5), tail = kinds.slice(5);
      const data = head.map(k => counts[k]).concat(tail.length ? [tail.reduce((s, k) => s + counts[k], 0)] : []);
      const names = head.map(name).concat(tail.length ? [`기타 ${tail.length}종`] : []);
      const on = this.kf == null ? -1 : (idx(this.kf) < 5 ? idx(this.kf) : 5);
      const sig = data.join(",") + "#" + on;
      const dn = slot.querySelector(`[data-q="donut"]`);
      if (sig !== this.ksig || !dn.firstElementChild) {
        this.ksig = sig;
        const total = data.reduce((s, v) => s + v, 0);
        dn.innerHTML = chMount(T => {
          const S = getComputedStyle(document.documentElement);
          return {type: "doughnut",
            data: {labels: names, datasets: [{data,
              backgroundColor: data.map((_, i) => {
                const [v, a] = tone(i);
                return chA(S.getPropertyValue(v).trim(), on < 0 || on === i ? a : a * .3);
              }),
              borderColor: T.card, borderWidth: 2}]},
            options: {responsive: true, maintainAspectRatio: false, cutout: "68%",
              plugins: {legend: {display: false},
                tooltip: chTipSays(T, {callbacks: {title: c => c[0].label,
                  label: c => `${num(c.raw)}건 (${(c.raw / total * 100).toFixed(0)}%)`}})}}};
        }, `종류별 기회: ${names.map((n, i) => `${n} ${data[i]}건`).join(", ")}`, "width:136px;height:136px")
          + `<div class="ovk-c"><b></b><span>줄</span></div>`;
      }
      dn.querySelector(".ovk-c b").textContent = num(this.filtered().length);
    }

    // ── 보드: 같은 기회를 상태 칸으로 ──────────────────────────────────────
    /* 목록은 "무엇부터"에 답하고, 보드는 "얼마나 남았고 무엇이 진행 중인가"에 답한다. 재료는 목록과
       같은 줄(묶인 줄)이고, 거르기(찾는 말·종류 칩)도 같은 것을 쓴다 — 상태 거르개만 칸이 대신한다.
       칸 이름은 OPP_LABEL 한 벌에서 온다. 카드를 다른 칸에 놓으면 그 상태로 옮긴다: 묶인 줄이면 묶인
       id 전부(oppIdsTo) — 상태 버튼·열기·복사와 같은 범위라야 새로고침 뒤 한 줄이 둘로 갈라지지 않는다.
       [(도구)로 열기]·[요청문 복사]는 목록과 같은 함수라, 누르면 진행 중으로 옮기고 load() 가 다시
       그리면서 카드가 진행 중 칸으로 간다. 제외·도구가 닫은 것은 칸을 안 만든다 — 할 일 흐름 밖이라
       목록에서 본다. 관찰 중인 기회도 칸에 안 오른다(목록 기본값과 같다) — 보드 밑 한 줄(holdLine)이 말한다. */
    colEmpty(st) {
      return {
        new: "남은 할 일이 없습니다.",
        acked: `진행 중인 기회가 없습니다. 할 일 카드에서 ${window.SM_HOSTED
          ? "[요청문 복사]를 누르거나" : "도구로 열거나"} 이 칸으로 끌어 놓으면 여기로 옵니다.`,
        done: "끝낸 기회를 이 칸으로 끌어 놓으면 완료로 옮깁니다.",
      }[st];
    }
    colMore(st) { this.colN[st] = (this.colN[st] || BOARD_PAGE) + BOARD_PAGE; this.list(); }
    // 카드의 행동 — 할 일·진행 중이면 일을 도구에 넘기는 손잡이(로컬 [(도구)로 열기], 호스팅 [요청문 복사]),
    // 그다음 상태 하나(OPP_NEXT). 둘 다 목록 줄과 같은 함수다.
    cardActs(o) {
      if (o.id == null) return "";
      const n = OPP_NEXT[o.status];
      const next = n ? oppBtn(o, n[0]) : "";
      const hand = o.status === "new" || o.status === "acked"
        ? (window.SM_HOSTED
            ? askBlock({brief: o.brief, ack: o.status === "new" ? oppIdsTo(o, "acked") : []})
            : (SM.host.oppBtn ? SM.host.oppBtn(o) : ""))
        : "";
      return hand + next;
    }
    card(o) {
      const n = targetsOf(o).length;
      return `<article class="kcard" draggable="true" data-card="${o.id}"
          ondragstart="QU(this).drag(event)" ondragend="QU(this).dragEnd(event)">
        <div class="kc-h">${oppScore(o)}<div class="target">${esc(targetLabel(o.target))}</div></div>
        <div class="kc-k">${kindsOf(o).map(kl => kindPill(o, kl)).join("")}${n > 1
          ? `<span class="badge" title="같은 지면이라 한 장으로 묶었습니다">검색어 ${n}개</span>` : ""}${
          directBadge(o, this.D)}${window.madeBadge(o)}</div>
        ${o.reasoning ? `<div class="why">${esc(o.reasoning)}</div>` : ""}
        <div class="acts">${this.cardActs(o)}<button type="button" class="go sm ghost"
          onclick="QU(this).boardOpen(${o.id})">자세히</button></div>
      </article>`;
    }
    board(list) {
      const cols = COLS.map(st => {
        const rows = list.filter(o => o.status === st);
        const shown = this.colN[st] || BOARD_PAGE, rest = rows.length - shown;
        const label = window.OPP_LABEL[st] || st;
        return `<div role="group" class="kcol kcol-${st}" data-st="${st}" aria-label="${esc(label)} ${rows.length}건"
            ondragover="QU(this).over(event)" ondragleave="QU(this).leave(event)" ondrop="QU(this).drop(event)">
          <div class="kc-top"><h3>${esc(label)}</h3><span class="kc-n">${num(rows.length)}</span></div>
          <div class="kc-list">${rows.length ? rows.slice(0, shown).map(o => this.card(o)).join("")
            : `<p class="kc-empty">${esc(this.colEmpty(st))}</p>`}</div>
          ${rest > 0 ? `<div class="acts kc-more"><button type="button" onclick="QU(this).colMore('${st}')"
            >${Math.min(BOARD_PAGE, rest)}건 더 보기 (남은 ${rest}건)</button></div>` : ""}
        </div>`;
      }).join("");
      // 칸에 안 오르는 상태도 있다고 말한다 — 안 말하면 개수가 목록과 안 맞아 보인다.
      const off = ["dismissed", "resolved"].map(st => [st, this.opps.filter(o => o.status === st).length])
        .filter(([, c]) => c);
      this.el("list").innerHTML = `<div class="kboard">${cols}</div>` + (off.length
        ? `<p class="kc-foot">${off.map(([st, c]) => `${esc(window.OPP_LABEL[st] || st)} ${num(c)}건`)
            .join(" · ")}은 보드에 올리지 않습니다. <button type="button" class="gov"
            onclick="QU(this).listAt('')">목록에서 전체 보기</button></p>` : "");
    }
    // 끌어 놓기 — 놓는 순간 카드를 그 칸으로 먼저 옮겨 두고(기다리는 동안 제자리로 튀지 않게), 서버가
    // 답하면 load() 가 다시 그린다. 실패하면 setOpp 가 말하고 여기서 원래 칸으로 되돌려 그린다(거짓
    // 성공을 남기지 않는다). 묶인 줄(setOpps)은 성패와 상관없이 load() 가 서버에 실제로 남은 상태로
    // 다시 그린다.
    drag(e) {
      const c = e.currentTarget;
      dragId = Number(c.dataset.card);
      e.dataTransfer.effectAllowed = "move";
      e.dataTransfer.setData("text/plain", String(dragId));
      c.classList.add("dragging");
    }
    dragEnd(e) {
      e.currentTarget.classList.remove("dragging");
      document.querySelectorAll(".kcol.over").forEach(x => x.classList.remove("over"));
      dragId = null;
    }
    over(e) {
      if (dragId == null) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = "move";
      e.currentTarget.classList.add("over");
    }
    leave(e) {
      if (!e.currentTarget.contains(e.relatedTarget)) e.currentTarget.classList.remove("over");
    }
    drop(e) {
      e.preventDefault();
      const col = e.currentTarget, st = col.dataset.st, id = dragId;
      col.classList.remove("over");
      dragId = null;
      const o = this.opps.find(x => x.id === id);
      if (!o || o.status === st) return;
      const card = this.root.querySelector(`.kcard[data-card="${id}"]`);
      const box = col.querySelector(".kc-list");
      if (card && box) {
        const empty = box.querySelector(".kc-empty");
        if (empty) empty.remove();
        box.prepend(card);
      }
      this.move(o, st);
    }
    async move(o, st) {
      const ids = oppIdsTo(o, st);
      if (!ids.length) return this.list();
      if (ids.length > 1) return setOpps(ids, st);
      if (!(await setOpp(ids[0], st))) this.list();
    }
    // [자세히] — 보드 카드에는 펼침 패널을 안 넣는다(좁은 칸에 표·요청문이 끼면 칸이 무너진다).
    // 화면에서는 오른쪽 서랍으로 연다. 서랍이 없는 자리(박제본)만 목록으로 돌아가 그 줄을 펴서
    // 보여 준다 — 상태 거르개는 그 줄이 드는 묶음으로.
    listAt(st) { this.setView("list", true); this.st = st; this.kf = null; this.render(QUEUE_PAGE); }
    boardOpen(id) {
      const o = this.opps.find(x => x.id === id);
      if (!o) return;
      // 화면에서는 보드 그대로 서랍을 연다 — 목록으로 옮겨 펼칠 까닭이 사라졌다(서랍은 칸을 안 무너뜨린다).
      if (!RO()) return this.drawerOpen(id, this.root.querySelector(`.kcard[data-card="${id}"] .acts .ghost`));
      this.setView("list", true);
      this.q = ""; this.kf = null;
      this.st = ["open", "closed", "resolved"].find(s => window.oppStMatch(o, s)) || "";
      const at = this.filtered().indexOf(o);
      this.render(Math.max(QUEUE_PAGE, at + 1));
      const row = this.root.querySelector(`.qlist details.opp[data-opp="${id}"]`);
      if (!row) return;
      row.open = true;
      row.scrollIntoView({behavior: "smooth", block: "center"});
    }
    // [기회 열기] — 목록 맨 위 줄(보드에서는 첫 「할 일」 카드)을 연다.
    openTop() {
      if (this.isBoard()) {
        const top = this.top();
        return top && this.boardOpen(top.id);
      }
      // 1순위는 목록에서 빠져 스포트라이트에만 있다 — 서랍으로 연다. 박제본에는 이 버튼이 없다.
      if (this.hero) return this.drawerOpen(this.hero.id, this.el("spot").querySelector(".go"));
      const card = this.root.querySelector(".qlist details.opp");
      if (!card) return;
      card.open = true;
      card.scrollIntoView({behavior: "smooth", block: "center"});
    }
    // 이 기회 줄을 찾아 연다 — [기록]이 기회 id 로 되돌아온다. 변형 쪽 id 여도 묶인 줄을 찾는다.
    openById(id, target) {
      this.reset();
      const box = this.root.querySelector("input[type=search]");
      if (box && target) { box.value = target; this.find(target); }
      const card = this.root.querySelector(`.qlist [data-ids~="${+id}"]`);
      if (!card && this.hero && (this.hero.group ? this.hero.group.ids : [this.hero.id]).includes(+id)) {
        this.drawerOpen(this.hero.id);
        return true;
      }
      if (!card) return false;
      card.open = true;
      card.scrollIntoView({behavior: "smooth", block: "center"});
      return true;
    }

    // 지금 거른 목록 그대로 표로 내보낸다. 파일로 만드는 일은 뼈대의 csv() 한 벌이 한다.
    csv() {
      csv("기회", [["점수", "종류", "상태", "대상", "함께 묶인 검색어", "근거", "생성일"]].concat(
        this.filtered().map(o => [o.score ?? "", kindsOf(o).map(([, l]) => l).join(" · "),
          (o.status === "resolved" && window.OPP_TAG_LABEL[o.status_tag]) || window.OPP_LABEL[o.status] || o.status,
          o.target,
          targetsOf(o).filter(t => t !== o.target).join(" · "),
          (o.reasoning || "").replace(/\s+/g, " "), o.created || ""])));
    }

    // ── 스포트라이트: 첫 화면의 답 ──────────────────────────────────────────
    /* "지금 무엇부터 손대나"에 대한 답이 한 장으로 선다 — 1순위 기회와 그걸 여는 버튼. 새 판정을 만들지
       않는다: 목록 맨 위가 곧 점수 1등이다(서버가 정렬해 보낸다). 이미 끝낸 것이 맨 위에 서는
       상태(전체·완료 보기)에서는 "먼저 손댈 것"이 아니므로 세우지 않는다. 모든 종류를 받는 큐만 둔다. */
    spot() {
      const el = this.el("spot");
      this.hero = null;
      if (!el) return;
      const top = this.all && this.opps.length ? this.top() : null;
      if (!top || !window.oppStMatch(top, "open")) { el.hidden = true; el.innerHTML = ""; return; }
      if (!RO() && !this.isBoard()) this.hero = top;
      const sc = Number.isFinite(top.score) ? Math.round(top.score) : null;
      const C = 2 * Math.PI * 26, n = targetsOf(top).length;
      el.classList.toggle("def", !!isDefensive(top));
      el.hidden = false;
      el.innerHTML = `<div class="ovs-ring" role="img"
          aria-label="${sc == null ? "점수 없음" : `${sc}점, 100점 만점`}">
          <svg viewBox="0 0 64 64" aria-hidden="true"><circle class="trk" cx="32" cy="32" r="26" fill="none" stroke-width="6"/>
            <circle class="arc" cx="32" cy="32" r="26" fill="none" stroke-width="6" stroke-linecap="round"
              stroke-dasharray="${(sc == null ? 0 : Math.min(100, sc) / 100 * C).toFixed(1)} ${C.toFixed(1)}"/></svg>
          <b>${sc == null ? "-" : sc}<i>점</i></b></div>
        <div class="ovs-b">
          <div class="ovs-k">점수 1위 기회</div>
          <div class="ovs-t">${esc(targetLabel(top.target))}</div>
          <div class="ovs-m">${kindsOf(top).map(kl => kindPill(top, kl)).join("")}${n > 1
            ? `<span class="badge" title="같은 지면이라 한 줄로 묶었습니다.">검색어 ${n}개</span>` : ""}${directBadge(top, this.D)}</div>
          ${top.reasoning ? `<p class="ovs-w">${esc(top.reasoning)}</p>` : ""}
        </div>
        ${RO() ? "" : `<div class="ovs-a"><button class="go" onclick="QU(this).openTop()">기회 열기</button></div>`}`;
    }

    // ── 기회 상세 서랍 ──────────────────────────────────────────────────────
    /* 목록 줄(<details class="opp">)을 여는 모든 길 — 누르기·[기회 열기]·보드 [자세히]·[기록]의
       되돌아가기 — 이 여기로 온다: 여는 쪽은 여전히 row.open = true 만 하고, toggle 을 받아 줄은
       다시 접고 서랍을 연다. 그래서 여는 쪽 네 곳을 고치지 않는다. 박제본(READONLY)과 인쇄는
       손대지 않는다 — 거기서는 제자리 펼침이 맞다(종이에 서랍은 없다).
       서랍 상자는 화면 상자마다 하나(qdr-<화면>)고, 화면이 같은 상자에 자기 내용을 칠할 수 있다
       (drawerExt — [개요]의 이번 달 할 일). 상자가 한 번에 하나라 기회와 그 내용이 겹쳐 열리지 않는다. */
    drawerEls() {
      const box = this.box();
      let dr = $("qdr-" + this.view);
      if (!dr && box) {
        box.insertAdjacentHTML("beforeend", `<div class="odr-bg" id="qdr-bg-${this.view}" hidden
          onclick="QU(this).drawerClose()" data-queue-of="${this.view}"></div><aside class="odr" id="qdr-${this.view}"
          data-queue-of="${this.view}" role="dialog" aria-modal="true" aria-labelledby="odr-t" tabindex="-1" hidden
          onkeydown="if(event.key==='Escape')QU(this).drawerClose();else SM.ui.trapTab(event,this)"></aside>`);
        dr = $("qdr-" + this.view);
      }
      return dr;
    }
    drawerShow() {
      const dr = this.drawerEls();
      dr.hidden = false;
      $("qdr-bg-" + this.view).hidden = false;
      const x = dr.querySelector(".odr-x");
      if (x) x.focus({preventScroll: true});
    }
    // 화면이 같은 상자에 자기 내용을 칠할 때 — ext: {onClose(): 강조 풀기, onTab(t): 탭 기억, back(): 되돌릴 초점}.
    // 칠하는 것(paint)은 화면 몫이다. 열려 있던 기회는 자리를 내준다.
    drawerExt(from, ext, paint) {
      if (!this.drawerEls() || RO()) return;
      this.root && this.root.querySelectorAll(".qlist details.opp.odr-on").forEach(x => x.classList.remove("odr-on"));
      this.dr = null;
      this.ext = ext;
      this.drFrom = from || null;
      paint(this.drawerEls());
      this.drawerShow();
    }
    drawerOpen(id, from) {
      const o = this.opps.find(x => x.id === id);
      const dr = this.drawerEls();
      if (!o || !dr) return;
      if (this.dr !== id) this.drTab = "ev";
      // 화면이 칠한 내용이 떠 있었으면 그 자리를 내준다 — 되돌아갈 곳도 이제 이 줄이다.
      if (this.ext) { this.ext.onClose(); this.ext = null; this.drFrom = null; }
      this.dr = id;
      if (from) this.drFrom = from;
      this.drawerPaint(o);
      this.drawerShow();
    }
    drawerClose() {
      const dr = $("qdr-" + this.view);
      if (!dr || dr.hidden) return;
      dr.hidden = true;
      $("qdr-bg-" + this.view).hidden = true;
      this.root && this.root.querySelectorAll(".qlist details.opp.odr-on").forEach(x => x.classList.remove("odr-on"));
      const ext = this.ext;
      // 연 줄로 초점을 돌려준다 — 상태를 바꿨으면 목록이 다시 그려져 그 요소는 없다. 같은 줄을 다시 찾는다.
      // (화면이 칠한 내용이면 그 화면이 되돌릴 자리를 말한다 — 강조를 풀기 전에 물어야 한다.)
      const back = this.drFrom && this.drFrom.isConnected ? this.drFrom
        : ext ? ext.back()
        : this.root && this.root.querySelector(`.qlist details.opp[data-opp="${this.dr}"] > summary`);
      if (ext) ext.onClose();
      this.dr = null; this.ext = null; this.drFrom = null;
      dr.innerHTML = "";               // 닫힌 서랍이 id(odr-t)를 쥐고 남지 않게 — 화면마다 서랍이 하나씩이다
      if (back) back.focus({preventScroll: true});
    }
    // 다시 그려진 목록에 맞춘다 — 상태를 바꾸면 load() 가 화면을 새로 그린다. 같은 줄이 아직 있으면
    // 새 값으로 다시 칠하고(보던 탭 그대로), 없어졌으면(사이트를 바꿨다) 닫는다.
    drawerSync() {
      if (this.dr == null) return;
      const o = this.opps.find(x => x.id === this.dr);
      if (!o) return this.drawerClose();
      this.drawerPaint(o);
    }
    drawerTab(t) {
      if (this.ext) this.ext.onTab(t); else this.drTab = t;
      const dr = $("qdr-" + this.view);
      dr.querySelectorAll(".odr-tabs button").forEach(b => b.setAttribute("aria-selected", b.dataset.t === t));
      dr.querySelectorAll("[data-panel]").forEach(p => { p.hidden = p.dataset.panel !== t; });
    }
    // 근거 칸 — 순위 조회가 잰 그 행(rank_by_kw)과 키워드 표의 검색량(o.volume, 묶인 줄이면 대표
    // 변형의 것)에서 읽는다. 없는 수는 칸째 뺀다(빈 칸에 "—"를 세우지 않는다).
    drTiles(o) {
      const D = this.D;
      const r = (D.rank_by_kw || {})[o.target];
      const vol = o.volume || (o.group && o.group.variants[0] && o.group.variants[0].volume);
      const t = [];
      if (r) t.push(["", `실제 순위${D.rank_date ? ` · ${esc(D.rank_date.slice(5))}` : ""}`,
        r.pos != null ? `${r.pos}위` : "잰 범위 밖"]);
      if (vol) t.push(["", "월 검색량", num(vol)]);
      if (r && r.aio != null) t.push(r.aio
        // 출처를 못 받은 줄(aio_cited NULL)은 '안 됨'이 아니라 모른다 — 빠짐으로 칠하지 않는다.
        ? (r.aio_cited == null ? ["", "구글 AI 요약", "있음 · 출처 모름"]
           : r.aio_cited ? ["ok", "구글 AI 요약", "우리 인용됨"] : ["warn", "구글 AI 요약", "우리 인용 안 됨"])
        : ["", "구글 AI 요약", "없음"]);
      return t.length ? `<div class="odr-tiles">${t.map(([c, l, v]) => `<div class="odr-tile${c ? " " + c : ""}">
        <span>${l}</span><b${v.length > 5 ? ` class="s"` : ""}>${esc(v)}</b></div>`).join("")}</div>` : "";
    }
    // 이 검색어의 검색 결과 상위 — 순위 조회가 남긴 그대로(serp_top). 우리 줄을 칠한다.
    drSerp(o) {
      const D = this.D;
      const rows = ((D.serp_top || {})[o.target] || []).slice(0, 5);
      if (!rows.length) return "";
      return `<h3 class="odr-h3">검색 결과 상위 ${rows.length}${D.rank_date
          ? ` · ${esc(D.rank_date.slice(5))}` : ""}</h3>
        <ol class="odr-serp">${rows.map(x => `<li${x.is_own ? ` class="own"` : ""}>
          <span class="p">${x.position}</span><span class="t" title="${esc(x.title || x.url)}">${
            esc(x.title || x.url)}</span><span class="d">${x.is_own ? "우리" : esc(x.domain || "")}</span></li>`
          ).join("")}</ol>`;
    }
    drawerSet(st) {
      const o = this.opps.find(x => x.id === this.dr);
      if (!o || o.status === st) return;
      const ids = oppIdsTo(o, st);
      if (!ids.length) return;
      ids.length > 1 ? setOpps(ids, st) : setOpp(ids[0], st);
    }
    // 발판의 [요청문 복사] — 요청문 탭 안의 복사 손잡이(askBlock)를 그대로 누른다. 복사하면서
    // '진행 중'으로 옮기는 규칙이 그 버튼에 있다 — 여기서 다시 짜면 두 벌이다.
    drawerCopy(btn) {
      const c = this.drawerEls().querySelector(".askcopy");
      if (!c) return;
      c.click();
      btn.textContent = "복사함";
      setTimeout(() => { btn.textContent = "요청문 복사"; }, 2000);
    }
    drawerPaint(o) {
      const dr = this.drawerEls();
      // 다시 칠하면 포커스를 쥔 버튼이 사라져 body 로 떨어진다 - 키보드 가둠과 Esc 가 서랍에 안 닿게 된다.
      const act = document.activeElement, held = !dr.hidden && act && dr.contains(act) && act !== dr;
      const segAt = held && act.closest(".odr-seg") ? [...act.parentNode.children].indexOf(act) : -1;
      const P = window.oppParts(o);
      const p = o.play || {};
      const n = targetsOf(o).length;
      const hosted = !!window.SM_HOSTED;
      const ask = hosted && P.ask && P.ask.includes('class="ask"')
        ? P.ask.replace('<details class="ask">', '<details class="ask" open>') : "";
      const deliver = (p.deliver || []).length ? `<h3 class="odr-h3" style="margin-top:0">요청문으로 받을 것</h3>
        <ol class="odr-deliver">${p.deliver.map(x => `<li><span>${esc(x)}</span></li>`).join("")}</ol>` : "";
      const see = this.see(o);
      const tabs = [["ev", "근거"], ["do", "할 일"]].concat(ask ? [["ask", "요청문"]] : []);
      if (!tabs.some(([t]) => t === this.drTab)) this.drTab = "ev";
      const seg = (o.status === "resolved" ? window.oppStBadge(o) : "")
        + `<div class="odr-seg" role="group" aria-label="상태">${["new", "acked", "done", "dismissed"].map(st =>
          `<button type="button" aria-pressed="${o.status === st}" onclick="QU(this).drawerSet('${st}')"${
            o.group && o.group.ids.length > 1 ? ` title="이 줄에 묶인 기회 ${o.group.ids.length}개에 모두 적용합니다"` : ""
          }>${window.OPP_LABEL[st]}</button>`).join("")}</div>`;
      const main = hosted ? (ask ? `<button type="button" class="go" onclick="QU(this).drawerCopy(this)">요청문 복사</button>` : "")
        : (SM.host.oppBtn ? SM.host.oppBtn(o) : "");
      this.root.querySelectorAll(".qlist details.opp").forEach(x =>
        x.classList.toggle("odr-on", +x.dataset.opp === o.id));
      dr.innerHTML = `<div class="odr-h">
          <div class="odr-top">
            <div class="odr-ring${isDefensive(o) ? " def" : ""}" title="${Number.isFinite(o.score)
              ? `${o.score}점 / 100점. 높을수록 먼저 손댑니다.` : ""}">${Number.isFinite(o.score) ? Math.round(o.score) : "-"}</div>
            <div class="odr-tt"><h2 id="odr-t">${esc(targetLabel(o.target))}</h2>
              <div class="odr-chips">${kindsOf(o).map(kl => kindPill(o, kl)).join("")}${n > 1
                ? `<span class="badge" title="${esc(targetsOf(o).join(" · "))}">검색어 ${n}개</span>` : ""}${
                directBadge(o, this.D)}${window.gaBadge(o)}${window.madeBadge(o)}${window.holdBadge(o)}</div></div>
            <button type="button" class="odr-x" aria-label="닫기" onclick="QU(this).drawerClose()">✕</button>
          </div>
          <div class="odr-tabs" role="tablist">${tabs.map(([t, l]) => `<button type="button" role="tab" data-t="${t}"
            aria-selected="${t === this.drTab}" onclick="QU(this).drawerTab('${t}')">${l}</button>`).join("")}</div>
        </div>
        <div class="odr-b">
          <div class="det" role="tabpanel" data-panel="ev"${this.drTab === "ev" ? "" : " hidden"}>
            ${o.reasoning ? `<p class="odr-why">${esc(o.reasoning)}</p>` : ""}${window.oppClosedWhy(o)}
            ${this.drTiles(o)}${P.stat}${this.drSerp(o)}
            ${P.page}${P.note}
            ${P.group}${P.rel}
            ${see ? `<div class="acts">${see}</div>` : ""}
          </div>
          <div class="det" role="tabpanel" data-panel="do"${this.drTab === "do" ? "" : " hidden"}>
            ${deliver}${P.play}${this.more_(o)}${P.works}
          </div>
          ${ask ? `<div class="det odr-ask" role="tabpanel" data-panel="ask"${this.drTab === "ask" ? "" : " hidden"}>
            ${ask}${SM.host.oppBtn ? SM.host.oppBtn(o) : ""}</div>` : ""}
        </div>
        <div class="odr-f">${seg}<span class="sp"></span>${main}</div>`;
      deadSweep(dr);
      applyWording(dr);
      if (held && !dr.contains(document.activeElement))
        ((segAt >= 0 && dr.querySelectorAll(".odr-seg button")[segAt]) || dr).focus({preventScroll: true});
    }
  }

  // 줄을 여는 모든 길이 서랍으로 온다 — 문서 전체에서 한 번만 듣고, 이벤트가 난 줄이 어느 큐의 것인지는
  // 줄의 소속(host)으로 찾는다. 박제본·인쇄는 손대지 않는다(종이에 서랍은 없다).
  document.addEventListener("click", e => {
    const sum = e.target.closest && e.target.closest(".qlist details.opp > summary");
    if (!sum || RO()) return;
    const q = window.QU(sum);
    if (!q) return;
    e.preventDefault();
    q.drawerOpen(+sum.parentElement.dataset.opp, sum);
  });
  document.addEventListener("toggle", e => {
    const el = e.target;
    if (!el.matches || !el.matches(".qlist details.opp") || !el.open || printing || RO()) return;
    const q = window.QU(el);
    if (!q) return;
    el.open = false;
    q.drawerOpen(+el.dataset.opp, el.querySelector("summary"));
  }, true);

  return {
    PAGE: QUEUE_PAGE,
    of: id => REG[id] || null,
    // 줄 하나의 대상들·종류들 — 큐를 읽는 화면([개요]의 띠)이 같은 해석을 쓴다.
    targetsOf, kindsOf,
    // render() 가 뷰보다 먼저 부른다 — view-def 에 queue 를 선언한 화면마다 한 번.
    feed(d) {
      for (const v of SM.views) {
        if (!v.queue) continue;
        const q = REG[v.id] || (REG[v.id] = new Queue(v.id, v.queue));
        try { q.feed(d); }
        catch (e) { console.error(`처리 큐 [${v.id}] 를 그리지 못했습니다`, e); }
      }
    },
    // 다른 화면의 줄에서 원래 화면·섹션으로 간다. show() 는 화면 맨 위로 올리는데, 문서가
    // scroll-behavior:smooth 라 그 애니메이션이 뒤이은 섹션 이동을 덮어쓴다(실측: 다음 프레임에 옮겨도
    // 섹션 위 300~1,200px 에서 제각각 섰다). 이 한 번만 부드러운 스크롤을 끄고 옮긴다. 섹션이 비어
    // 숨었으면 show() 가 올려 둔 화면 맨 위가 답이다.
    goSee(view, sec) {
      SM.touched = true;
      const root = document.documentElement.style, prev = root.scrollBehavior;
      root.scrollBehavior = "auto";
      SM.show(view);
      const el = sec && $(sec);
      // 대상이 접힌 탭에 있으면 그 탭을 먼저 연다 — 숨은 섹션은 스크롤할 수 없다.
      const mode = el ? SM.modeOf(el) : "";      // 섹션 안 카드(깊은 패널)여도 그 탭을 연다
      if (mode) SM.setMode(view, mode);
      if (el && el.offsetParent !== null) el.scrollIntoView({block: "start"});
      root.scrollBehavior = prev;
    },
  };
})();
