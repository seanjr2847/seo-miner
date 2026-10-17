// ── 셸: 화면 목록 · 메뉴 · 전환 ───────────────────────────────
// 목록의 정본은 뷰 파일의 view-def 다 — 조립(dashboard.py)이 window.__VIEWS__ 로
// 실어 보낸다. 여기서 이름을 다시 나열하지 않는다: 말없이 어긋난 두 표가 예전에
// 로컬과 호스팅의 섹션 순서를 서로 다르게 만든 원인이다.
// 뒤에 얹히는 층(호스팅 애드온)은 drop() 으로 뺄 뿐이다 — 덧붙이는 쪽은 이제
// 런타임이 아니라 조립이다. 배포 전용 섹션은 templates/sections/*.html 의
// section-def 가 자리(view/after)를 선언하고 dashboard.py._assemble 이 그 자리에
// 끼운다. 런타임 삽입(SM.addSection)은 그래서 호출자가 없어져 걷어냈다.
const SM = {
  views: (window.__VIEWS__ || []).map(v => ({
    id: v.id, title: v.title, sub: v.sub || "", group: v.group || "",
    // aliases: 화면을 쪼개거나 이름을 바꾸기 전의 옛 id. 저장된 sessionStorage 값이나 옛 링크가 이 이름으로
    // 와도 새 화면으로 닿는다(at() 이 풀어 준다) — 화면 id 를 바꿀 때 북마크·복원이 끊기지 않게.
    aliases: (v.aliases || []).slice(),
    sections: (v.sections || []).slice(), stages: (v.stages || []).slice(),
    // head: 화면 제목 옆에 세울 칩. 안 쓰면 stages 그대로(예전 그대로 전부 세운다) —
    // stages 는 seam #9(이 화면에서 부를 수 있는 /capture)의 정본이라 그대로 두고,
    // head 는 그중 "이 화면의 주 단계"만 추린 부분집합이다(view-def 에 없으면 폴백).
    head: (v.head || v.stages || []).slice(),
    // modes: 화면 안 탭 선언 [{id,label,count?,hide_empty?}] — 없으면 탭 줄이 없다(예전 그대로). 섹션·카드는
    // 마크업의 data-mode 로 자기 탭을 말한다. count 는 탭 배지가 읽을 페이로드 점 경로. hide_empty 는
    // 보일 카드가 없는 탭(로컬·호스팅 전용 칸이 비는 경우)을 탭째 접는다 — 안 적으면 늘 선다.
    modes: (v.modes || []).map(m => ({id: m.id, label: m.label, count: m.count || "", hideEmpty: !!m.hide_empty})),
    // queue: 처리 큐 선언({kinds:"*"|"see", mode?, title?}) — 없으면 큐가 없다. 큐는 아래 SM.queue 한 벌이다.
    queue: v.queue || null})),
  cur: "",
  // 페이로드의 묶음(d.groups) — render() 가 setGroups 로 넣는다. 첫 페이로드 전에는
  // 비어 있어 메뉴에 묶음 이름이 안 선다(이름을 여기 적어 두지 않는다 — 위 주석).
  groups: [],
  groupOf(id) { return this.groups.find(g => g.id === id) || null; },
  setGroups(gs) {
    gs = gs || [];
    // 이름·점이 바뀔 때만 메뉴를 다시 그린다 — render() 는 자주 불린다.
    const key = JSON.stringify(gs.map(g => [g.id, g.name, !!g.due]));
    if (key === this._gkey) { this.groups = gs; return; }
    this._gkey = key;
    this.groups = gs;
    this.navDraw();
  },

  at(id) { return this.views.findIndex(v => v.id === id || v.aliases.includes(id)); },
  // 옛 id 를 새 id 로. 없는 화면이면 받은 그대로 돌려준다(없다는 말은 show 가 한다).
  canon(id) { const i = this.at(id); return i < 0 ? id : this.views[i].id; },

  // 이 배포에 없는 화면을 뺀다 — 호스팅의 [설정], 박제본의 [설정]·[진행 안내].
  drop(id) {
    const i = this.at(id);
    if (i < 0) return;
    const v = this.views.splice(i, 1)[0];
    const box = $("view-" + v.id);
    if (box) box.remove();
    if (this.cur === v.id) this.cur = "";
    this.sync();
  },

  touched: false,
  landed: 0,
  restored: false,     // sync() 가 sessionStorage 로 첫 화면을 되살렸으면 true
  // rank 가 큰 쪽이 이긴다: 설정(3) > 안내(2) > 미판정 심사(1). 사용자가 메뉴를 누르면 끝.
  land(id, rank) {
    if (this.touched || rank <= this.landed || this.at(id) < 0) return;
    this.landed = rank;
    this.show(id);
  },

  show(id) {
    id = this.canon(id);
    // 이 배포에 없는 화면으로 보내면 여태 조용히 아무 일도 안 일어났다 — 누른
    // 사람에게는 버튼이 고장 난 것으로 읽힌다. 박제본에서 [설정]·[진행 안내] 로 가는
    // 링크가 정확히 그랬다. 왜 안 가는지 말한다. 첫 적재(cur 가 아직 빈 때)는
    // 사용자가 누른 것이 아니므로 말하지 않는다.
    if (id !== this.cur && this.drawer) this.drawer.close();   // 화면을 옮기면 공용 서랍은 닫힌다
    if (this.at(id) < 0) {
      if (this.cur && window.toast)
        toast(window.__SNAPSHOT__ ? "이 보고서에는 없는 화면입니다."
                                  : "이 배포에는 없는 화면입니다.");
      return;
    }
    this.cur = id;
    // 사용자가 직접 연 화면만 적는다(touched — land() 가 고른 것은 사용자 선택이 아니다).
    if (this.touched) saveLastView(id);
    for (const v of this.views) {
      const box = $("view-" + v.id);
      if (box) box.hidden = v.id !== id;
    }
    this.modeApply(id);            // 마지막으로 본 탭을 되살린다(modes 없는 화면은 그대로)
    document.querySelectorAll("#nav button, #navfoot button").forEach(b => {
      const on = b.dataset.v === id;
      b.classList.toggle("on", on);
      b.setAttribute("aria-current", on ? "page" : "false");
      // 새로고침 복원·바로가기로 뒤쪽 탭이 열리면 가로 스크롤러가 그 탭 자리로
      // 시작해, 앞쪽 탭이 스크롤 밖에 있는 채로 열린다 — 좁은 화면(탭 줄)에서는 가운데로
      // 당기고, 레일에서는 세로 목록 안에서 보이는 자리까지만 당긴다.
      if (on) {
        const narrow = matchMedia("(max-width:999px)").matches;
        b.scrollIntoView(narrow ? {inline: "center", block: "nearest"} : {block: "nearest"});
        // 레일에서 묶음 앞쪽 항목이 열리면 그 묶음 제목까지 함께 보인다 — 제목이
        // 반쯤 잘린 채 남으면 어느 묶음인지 안 읽힌다.
        if (!narrow) {
          let g = b.previousElementSibling, k = 0;
          while (g && !g.classList.contains("navgrp") && k < 3) { g = g.previousElementSibling; k++; }
          if (g && g.classList.contains("navgrp") && k < 3) g.scrollIntoView({block: "nearest"});
          if (!g) b.parentElement.scrollTop = 0;
        }
      }
    });
    // 실행 배너는 화면에 매인 것이 아니라 **런에 매인 것**이다 — 화면을 바꿔도
    // 따라온다. 고르는 것은 id 가 아니라 표식(data-vban)이다: id 를 여기 적으면
    // 배너가 셋째로 늘어나는 날 그것만 조용히 안 따라온다.
    const moving = document.querySelectorAll("[data-vban]");
    if (moving.length) {
      const slot = this.slot();
      if (slot) moving.forEach(b => slot.appendChild(b));
    }
    window.scrollTo(0, 0);
    // 화면이 바뀌었다고 알린다. 여태 이 신호가 없어서, 뒤에 얹히는 층이 "지금 어느
    // 화면인가"를 알려면 SM.cur 를 400ms 마다 들여다보는 수밖에 없었다.
    // sm:views 는 상자를 다시 세울 때만 나므로 이 자리를 대신 못 한다.
    document.dispatchEvent(new CustomEvent("sm:view", {detail: {view: id}}));
  },

  // 메뉴. 묶음 이름은 화면의 묶음이 바뀌는 자리에만 한 번 선다. 그 배포에 없는
  // 화면은 SM.drop 이 이미 뺐으므로 빈 묶음의 이름이 남을 일이 없다.
  // 이름은 작은 제목(.rl — --micro, 자간 없음)이고, 주기를 넘긴 묶음(g.due — 출처는
  // grpDue 한 벌)에만 점 하나를 단다. 날짜는 메뉴에 안 쓴다 — 그건 각 화면 머리
  // (groupHead)의 몫이다. 다시 잴 단계가 없는 묶음(할 일 — 매일 런의 꼬리로 돈다)에는
  // 점을 안 단다: 눌러서 할 수 있는 것이 없는 경고는 소음이다.
  navDraw() {
    const nav = $("nav"), foot = $("navfoot");
    if (!nav) return;
    nav.innerHTML = "";
    if (foot) foot.innerHTML = "";
    let grp = null, due = false;
    for (const v of this.views) {
      const g = this.groupOf(v.group);
      // 다시 잴 것이 없는 묶음(주기가 없다 — 기록·안내·설정)은 메뉴가 아니라 레일 바닥 줄에
      // 선다. 어느 묶음인지는 페이로드가 말한다(every_hours 가 null) — 화면 id 를 여기
      // 적지 않는다. 첫 페이로드 전에는 묶음을 몰라 전부 메뉴에 서고, 오면 다시 그린다.
      const bottom = !!foot && !!g && g.every_hours == null;
      if (!bottom && v.group !== grp) {
        grp = v.group;
        due = !!(g && g.due && g.stages && g.stages.length);
        if (g && g.name) {
          const lab = document.createElement("span");
          lab.className = "rl navgrp";
          lab.textContent = g.name;
          if (due) {
            const dot = document.createElement("i");
            dot.className = "navdue";
            dot.setAttribute("role", "img");
            dot.setAttribute("aria-label", "다시 잴 때가 지났습니다");
            dot.title = "다시 잴 때가 지났습니다";
            lab.appendChild(dot);
          }
          nav.appendChild(lab);
        }
      }
      // 바닥 줄의 이름(접근성)은 묶음 이름 그대로다 — 글자로 적지 않고 페이로드에서 받는다.
      if (bottom && g.name) foot.setAttribute("aria-label", g.name);
      const b = document.createElement("button");
      b.dataset.v = v.id;
      // 아이콘은 화면 id 로 고른다 — 뷰 파일이 자기 아이콘을 적지 않는다(적으면
      // 열한 벌이 되고 그중 몇은 언젠가 낡는다). 모르는 id 면 글자만 선다.
      // 글자(.t)는 접힌 레일에서 감춰지므로 이름은 aria-label·title 이 대신 말한다(navMark).
      b.innerHTML = (NAV_ICON[v.id] || "") + '<span class="t"></span>';
      b.querySelector(".t").textContent = v.title;
      b.onclick = () => { SM.touched = true; SM.show(v.id); };
      const on = v.id === this.cur;
      b.classList.toggle("on", on);
      // 다시 잴 때가 지난 묶음의 화면에는 점을 단다 — 묶음 제목이 안 서는 접힌 레일·가로 탭용.
      b.classList.toggle("due", !bottom && due);
      b.setAttribute("aria-current", on ? "page" : "false");
      this.navMark(b, v.id);
      (bottom ? foot : nav).appendChild(b);
    }
    if (typeof navFade === "function") navFade();
  },

  // 화면별 건수 — 그 화면에서 사람이 할 일이 몇 건인가. 값은 화면이 이미 받은 것을 넘긴다
  // (심사: /api/triage 의 counts.none). 새로 묻지 않는다. 0 이면 알약을 뗀다.
  counts: {},
  setCount(id, n) {
    n = n > 0 ? n : 0;
    if ((this.counts[id] || 0) === n) return;
    this.counts[id] = n;
    const b = document.querySelector(`#nav button[data-v="${id}"], #navfoot button[data-v="${id}"]`);
    if (b) this.navMark(b, id);
  },
  // 건수 알약과 접근성 이름. 접힌 레일에서는 글자가 안 보이므로 이 이름이 유일한 말이다.
  navMark(b, id) {
    const n = this.counts[id] || 0, v = this.views[this.at(id)];
    let c = b.querySelector(".cnt");
    if (n && !c) { c = document.createElement("span"); c.className = "cnt"; b.appendChild(c); }
    if (c) { if (n) c.textContent = n > 99 ? "99+" : String(n); else c.remove(); }
    const name = (v ? v.title : id) + (n ? ` ${n}건` : "")
      + (b.classList.contains("due") ? " · 다시 잴 때가 지났습니다" : "");
    b.title = name;
    b.setAttribute("aria-label", name);
  },

  // ── 화면 안 두 탭 ───────────────────────────────────────────
  // 선언은 view-def 의 modes 한 곳, 소속은 섹션 마크업의 data-mode 한 곳이다. 탭 줄·기억·
  // 키보드·접기는 전부 여기 한 벌 — 뷰는 자기 탭 줄을 짓지 않는다. 고른 탭은 탭 하나가 쓰는
  // sessionStorage("sm:mode:<화면>")에 적고, SM.show 로 들어올 때 되살린다(저장소가 막혀도
  // 메모리에 남아 전환은 된다). 접는 것은 hidden 이며, 셸이 접은 것(data-vm-off)만 셸이 편다 —
  // 섹션이 스스로 hidden 을 쓰는 자리를 덮지 않는다.
  mode: {},
  // 비는 탭(hide_empty) — modeApply 가 정한다. modeNow·방향키가 이 탭들을 건너뛴다.
  modeDead: {},
  modeIsDead(v, m) { const d = this.modeDead[v.id]; return !!d && d.has(m); },
  modeNow(id) {
    const v = this.views[this.at(id)];
    if (!v || !v.modes.length) return "";
    let m = this.mode[v.id];
    if (!m) m = this.modePicked(v.id);
    if (v.modes.some(x => x.id === m) && !this.modeIsDead(v, m)) return m;
    return (v.modes.find(x => !this.modeIsDead(v, x.id)) || v.modes[0]).id;
  },
  // 사람이 골라 둔 탭(sessionStorage) — 없으면 "". 첫 탭 규칙·화면 쪽 자동 선택이 "사람이 골랐나"를 이걸로 본다.
  modePicked(id) {
    const v = this.views[this.at(id)];
    if (!v) return "";
    try { return sessionStorage.getItem("sm:mode:" + v.id) || ""; } catch (e) { return ""; }
  },
  // auto: 화면이 대신 고른 탭(처음 열 때 할 일이 있는 탭) — 사람이 고른 것이 아니라 기억에 적지 않는다.
  setMode(id, m, focus, auto) {
    const v = this.views[this.at(id)];
    if (!v || !v.modes.some(x => x.id === m)) return;
    this.mode[v.id] = m;
    if (!auto) { try { sessionStorage.setItem("sm:mode:" + v.id, m); } catch (e) {} }
    this.modeApply(v.id, focus);
    document.dispatchEvent(new CustomEvent("sm:mode", {detail: {view: v.id, mode: m, auto: !!auto}}));
  },
  // 패널 — 탭에 매이는 요소. 화면 상자 직계의 data-mode 는 모두, 섹션 안쪽은 섹션·카드·띠(.band)만 센다.
  // 안쪽 버튼·칩의 data-mode(예: [관찰 중]의 정렬 칩)는 남의 이름이다 — 탭 줄의 버튼도 마찬가지다.
  isPanel(el) {
    return !!(el.dataset && el.dataset.mode && el.parentElement
      && (el.parentElement.classList.contains("view") || el.matches("section, .card, .band")));
  },
  modePanels(box) {
    const all = [...box.querySelectorAll("[data-mode]")].filter(el => this.isPanel(el));
    return all.filter(el => !all.some(o => o !== el && o.contains(el)));   // 패널 안의 패널은 바깥이 접는다
  },
  // 요소가 속한 탭 id(없으면 ""). goSee·화면의 "그 칸으로 가기"가 먼저 그 탭을 열 때 쓴다.
  modeOf(el) {
    let hit = "";
    for (let p = el; p && !(p.classList && p.classList.contains("view")); p = p.parentElement)
      if (this.isPanel(p)) hit = p.dataset.mode;
    return hit;
  },
  // 이 패널이 지금 서 있을 수 있나 — 깊은 패널은 조상이 감춰졌거나(호스팅의 #setup) 스스로 hidden 이면 아니다.
  // 직계는 뷰가 스스로 숨기는 일이 많아(빈 카드) 비는 탭 판정에 쓰지 않는다.
  panelLive(el, box) {
    if (el.parentElement === box) return true;
    if (el.hidden) return false;
    for (let p = el.parentElement; p && p !== box; p = p.parentElement)
      if (p.hidden || getComputedStyle(p).display === "none") return false;
    return true;
  },
  // 탭 줄 — 상자를 처음 세울 때 한 번(sync). 화면 머리(.vh) 바로 아래.
  modeDraw(box, v) {
    if (!v.modes.length || box.querySelector(":scope > .vtabs")) return;
    const bar = document.createElement("div");
    bar.className = "vtabs";
    bar.setAttribute("role", "tablist");
    bar.setAttribute("aria-label", v.title);
    for (const m of v.modes) {
      const b = document.createElement("button");
      b.type = "button";
      b.id = "vtab-" + v.id + "-" + m.id;
      b.dataset.mode = m.id;
      b.setAttribute("role", "tab");
      b.appendChild(document.createTextNode(m.label));
      b.onclick = () => SM.setMode(v.id, m.id);
      bar.appendChild(b);
    }
    bar.addEventListener("keydown", e => {
      const ids = v.modes.map(m => m.id).filter(m => !SM.modeIsDead(v, m)), i = ids.indexOf(SM.modeNow(v.id));
      const to = e.key === "ArrowRight" ? ids[(i + 1) % ids.length]
               : e.key === "ArrowLeft" ? ids[(i + ids.length - 1) % ids.length]
               : e.key === "Home" ? ids[0] : e.key === "End" ? ids[ids.length - 1] : "";
      if (!to) return;
      e.preventDefault();
      SM.setMode(v.id, to, true);
    });
    const vh = box.querySelector(":scope > .vh");
    if (vh) vh.after(bar); else box.prepend(bar);
    this.modeCountsPaint(v.id);
  },
  // 고른 탭에 맞춰 패널을 접고 편다. 여러 번 불려도 공짜다(sync·show 가 부른다). 비는 탭(hide_empty)은
  // 여기서 정한다 — 화면은 칸이 늦게 서거나 감춰질 때 이걸 다시 부르면 된다.
  modeApply(id, focus) {
    const v = this.views[this.at(id)], box = v && $("view-" + v.id);
    if (!v || !box || !v.modes.length) return;
    const panels = this.modePanels(box);
    const dead = new Set(v.modes.filter(m => m.hideEmpty
      && !panels.some(el => el.dataset.mode === m.id && this.panelLive(el, box))).map(m => m.id));
    this.modeDead[v.id] = dead;
    const now = this.modeNow(v.id);
    box.querySelectorAll(":scope > .vtabs [role=tab]").forEach(b => {
      const on = b.dataset.mode === now, gone = dead.has(b.dataset.mode);
      if (b.hidden !== gone) b.hidden = gone;
      b.setAttribute("aria-selected", on ? "true" : "false");
      b.tabIndex = on ? 0 : -1;
      if (on && focus) b.focus();
    });
    let opened = false;
    for (const el of panels) {
      const off = el.dataset.mode !== now, direct = el.parentElement === box;
      el.setAttribute("role", "tabpanel");
      el.setAttribute("aria-labelledby", "vtab-" + v.id + "-" + el.dataset.mode);
      if (off) { el.dataset.vmOff = "1"; if (direct) el.hidden = true; }
      else if (el.dataset.vmOff) { delete el.dataset.vmOff; if (direct) el.hidden = false; opened = true; }
    }
    // 숨은 탭에서 그려진 차트는 폭 0 으로 남는다 — Chart.js 의 자동 재측정(ResizeObserver)은 펴지는 타이밍에
    // 따라 빠지기도 한다(호스팅에서 색인 도넛이 세 번에 두 번 0). 탭이 펴진 다음 프레임에 한 번 다시 재게 한다.
    if (opened && window.Chart) requestAnimationFrame(() => {
      for (const c of Object.values(Chart.instances || {})) { try { c.resize(); } catch (e) {} }
    });
  },
  // 탭 배지 — 그 탭에서 사람이 할 일이 몇 건인가. 값은 페이로드의 점 경로(modes[].count)에서
  // 읽는다(render 가 modeCountsFrom 을 부른다). 선언한 경로가 없으면 배지 자리만 비어 있다.
  modeCount: {},
  setModeCount(id, m, n) {
    n = n > 0 ? n : 0;
    const k = id + ":" + m;
    if ((this.modeCount[k] || 0) === n) return;
    this.modeCount[k] = n;
    this.modeCountsPaint(id);
  },
  // 탭 점 — 건수가 아니라 "여기 할 일이 남았다"는 표시. 값은 화면이 자기 판정으로 밀어 넣는다
  // (설정: 준비 상태에서 못 마친 필수 칸이 있는 탭). 배지(count)와 별개라 한 탭에 둘 다 설 수 있다.
  modeDot: {},
  setModeDot(id, m, on) {
    const k = id + ":" + m;
    on = !!on;
    if (!!this.modeDot[k] === on) return;
    this.modeDot[k] = on;
    this.modeCountsPaint(id);
  },
  modeCountsFrom(d) {
    for (const v of this.views) for (const m of v.modes) {
      if (!m.count) continue;
      const n = m.count.split(".").reduce((o, k) => (o == null ? o : o[k]), d);
      this.setModeCount(v.id, m.id, Number(n) || 0);
    }
    this.modeDefaults((d.project || {}).name || "");
  },
  // 첫 탭 — 선언 첫째가 기본이되, 그 탭의 배지(count)가 0 이면 할 일이 없는 빈 탭이라 다음 탭이 먼저 선다.
  // 사이트가 바뀔 때 한 번, 사람이 고른 탭(sessionStorage)이 없을 때만 정한다 — 재적재마다 부르면
  // 마지막 기회를 끝낸 순간 화면이 [관리]로 달아난다. 화면은 이 규칙을 따로 짓지 않는다.
  // count 가 하나도 없는 화면(설정)은 정할 것이 없다 — 선언 첫째가 곧 기본이라 건드리지 않는다(화면이
  // 대신 고른 탭을 덮어쓰지 않게).
  modeSite: {},
  modeDefaults(site) {
    for (const v of this.views) {
      if (!v.modes.length || !v.modes.some(m => m.count) || this.modeSite[v.id] === site) continue;
      this.modeSite[v.id] = site;
      if (this.modePicked(v.id)) continue;
      const pick = v.modes.find(m => !m.count || (this.modeCount[v.id + ":" + m.id] || 0) > 0);
      this.mode[v.id] = (pick || v.modes[0]).id;
    }
  },
  modeCountsPaint(id) {
    const v = this.views[this.at(id)], box = v && $("view-" + v.id);
    if (!v || !box) return;
    for (const m of v.modes) {
      const b = box.querySelector(`:scope > .vtabs [role=tab][data-mode="${m.id}"]`);
      if (!b) continue;
      const n = this.modeCount[v.id + ":" + m.id] || 0, dot = !!this.modeDot[v.id + ":" + m.id];
      let c = b.querySelector(".cnt");
      if (n && !c) { c = document.createElement("span"); c.className = "cnt"; b.appendChild(c); }
      if (c) { if (n) c.textContent = n > 99 ? "99+" : String(n); else c.remove(); }
      let t = b.querySelector(".tdot");
      if (dot && !t) { t = document.createElement("i"); t.className = "tdot"; t.setAttribute("aria-hidden", "true"); b.appendChild(t); }
      if (!dot && t) t.remove();
      b.setAttribute("aria-label", m.label + (n ? ` ${n}건` : "") + (dot ? " · 할 일 남음" : ""));
    }
  },

  // 상자를 만들고 섹션을 제자리로 옮긴다. 여러 번 불려도 공짜다 — 애드온이
  // 만드는 섹션은 늦게 생기므로 실제로 여러 번 불린다.
  sync() {
    const c = $("content");
    if (!c) return;
    // 첫 화면: 복원한 화면 > 개요 > 뷰 목록 첫째(view-def 순서는 안 바꾼다 — 메뉴
    // 순서일 뿐 여기 기본값과는 다른 이야기다). 미판정 검색어가 있으면 심사로
    // 보내는 것은 land("triage",1) 이 데이터가 온 뒤에 한다(checkTriageLand).
    if (!this.cur && this.views.length) {
      const back = lastView();
      if (this.at(back) >= 0) { this.cur = this.canon(back); this.restored = true; }
      else this.cur = this.at("overview") >= 0 ? "overview" : this.views[0].id;
    }
    for (const v of this.views) {
      let box = $("view-" + v.id);
      if (!box) {
        box = document.createElement("div");
        box.id = "view-" + v.id;
        box.className = "view";
        box.hidden = v.id !== this.cur;
        box.innerHTML = '<div class="vh"><h1></h1><span class="sp"></span>'
                      + '<p class="sub1" hidden></p></div><div class="vban"></div>';
        box.querySelector("h1").textContent = v.title;
        // 한 줄 부제. 정본은 view-def 의 sub 이고, 화면 상태에 따라 달라지는 것은
        // 뷰가 viewSub(id, 글) 로 덮는다. 빈 글이면 아예 안 그린다 — 빈 상자가
        // 제목과 다음 행동 사이에 여백만 벌려 놓는 일이 없게. 여기서 한 번만
        // 쓴다: sync() 는 여러 번 불리는데 매번 덮으면 뷰가 심은 말이 지워진다.
        c.appendChild(box);
        viewSub(v.id, v.sub);
      }
      c.appendChild(box);                       // view-def 순서가 곧 화면 순서다
      for (const id of v.sections) {
        const el = $(id);
        if (!el) continue;
        const sec = el.closest("section") || el;   // band 는 section 이 아니다
        if (sec.parentNode !== box) box.appendChild(sec);
      }
      if (v.modes.length) { this.modeDraw(box, v); this.modeApply(v.id); }
    }
    this.navDraw();
    this.show(this.at(this.cur) >= 0 ? this.cur
              : (this.views[0] ? this.views[0].id : ""));
    // 상자가 (다시) 섰다고 알린다 — 뒤에 얹히는 층이 화면 머리에 자기 버튼을 단다.
    document.dispatchEvent(new CustomEvent("sm:views"));
  },
};
