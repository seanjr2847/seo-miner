/* ── 차트 헬퍼(window.ch*) — Chart.js 4 위에 선다 ─────────────────────────────
   라이브러리는 templates/vendor/chart.umd.min.js(4.5.1, MIT) 하나이고 조립이 <head>
   에 인라인으로 박는다(dashboard.py) — 박제본은 파일 하나로 오프라인에서 열려야 해서
   CDN 을 못 쓴다. 뷰는 여전히 문자열만 받는다: 헬퍼는 <canvas data-ch> 자리를 돌려주고
   설정은 여기(CH_CFG)가 쥔다. 캔버스가 문서에 붙는 순간 아래 관찰자가 차트를 세우고,
   떨어지면 부순다 — 화면을 다시 그릴 때마다 옛 차트가 메모리에 남지 않게. 그래서 이
   헬퍼를 부르는 뷰는 한 글자도 안 바뀐다. new Chart( 는 이 파일의 chMake 한 곳뿐이다
   (test_seams 가 못 박는다).
   색은 CSS 토큰을 그 자리에서 읽는다(Chart.js 는 var(--x) 를 못 읽는다) — 다크로
   바뀌면 전부 다시 세운다. 명세는 dataviz 한 벌: 애니메이션 없음, 막대 두께 상한·데이터
   끝만 4px 둥글게, 격자 1px 실선, 조각 사이 2px 표면색 틈, 점에 2px 표면색 링, 글자는
   먹색 토큰(계열 색을 글자에 안 쓴다), 숫자는 등폭·이름은 본문 서체. 이중축 금지,
   순서형은 같은 hue 의 명암, 9번째 계열에 새 색을 만들지 않는다. */
const CH_TONE = {patina:"var(--patina)", copper:"var(--copper)", slate:"var(--slate)", bar:"var(--bar)"};
const chTone = t => CH_TONE[t] || "var(--slate)";      // 캔버스 밖(범례 HTML)의 색
const chVal = (v, unit) => `${num(v)}${unit || ""}`;

// 토큰 → 실제 색. 매번 읽는다: 다크로 바뀌면 같은 이름이 다른 색이다.
const chT = () => {
  const s = getComputedStyle(document.documentElement), v = n => s.getPropertyValue(n).trim();
  return {patina: v("--patina"), copper: v("--copper"), slate: v("--slate"), ink: v("--ink"),
    rule: v("--rule"), soft: v("--rule-soft"), wash: v("--wash"), bar: v("--bar"),
    card: v("--card"), sans: v("--sans") || "sans-serif", mono: v("--mono") || "monospace"};
};
// #rrggbb 에 투명도를 얹는다 — 도넛의 회색 조각, 깔때기의 단계 명암.
const chA = (hex, a) => {
  const m = /^#?([0-9a-f]{6})$/i.exec(hex || "");
  if (!m) return hex;
  const n = parseInt(m[1], 16);
  return `rgba(${n >> 16},${(n >> 8) & 255},${n & 255},${a})`;
};
// 툴팁 — 먹색 바탕에 표면색 글자. 제목은 본문 서체, 값은 등폭.
const chTip = (T, more = {}) => ({backgroundColor: T.ink, titleColor: T.card, bodyColor: T.card,
  titleFont: {family: T.sans, size: 12, weight: "600"}, bodyFont: {family: T.mono, size: 12},
  padding: 8, cornerRadius: 4, displayColors: false, caretSize: 5, ...more});
/* 본문이 숫자가 아니라 말인 툴팁 — 계열 이름("브랜드"·"모바일")이나 비교 문구
   ("직전 대비 −12%")가 본문에 들어가는 차트가 쓴다. 등폭은 숫자·날짜·식별자만이라
   한글이 섞이는 본문은 제목과 같은 본문 서체로 낸다(캔버스는 한 줄에 두 서체를
   못 섞는다 — 줄 단위로 가른다). */
const chTipSays = (T, more = {}) => chTip(T, {bodyFont: {family: T.sans, size: 12}, ...more});
// 이름 축(가로 막대의 왼쪽) — 격자·테두리 없이 이름만.
const chCat = T => ({grid: {display: false}, border: {display: false},
  ticks: {color: T.ink, font: {family: T.sans, size: 12}}});

// 직접 라벨 — Chart.js 본체엔 없다. 둘째 라이브러리를 들이지 않고 여기서 그린다.
// items: {i, text, at:"col"(오른쪽 값 열)|"end"(막대 끝 바로 옆)|"center"(도넛 가운데),
// bold, big}. zero: 0 선. cross: 선 차트 십자선.
const chText = {id: "chText",
  afterDatasetsDraw(ch, _a, o) {
    if (!o || !o.T) return;
    const T = o.T, {ctx, chartArea: A} = ch, meta = ch.getDatasetMeta(0);
    const horiz = ch.options.indexAxis === "y" && ch.scales.y;
    ctx.save();
    if (o.zero && ch.scales.x) {
      const x0 = Math.round(ch.scales.x.getPixelForValue(0)) + .5;
      ctx.strokeStyle = T.rule; ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(x0, A.top); ctx.lineTo(x0, A.bottom); ctx.stroke();
    }
    ctx.textBaseline = "middle";
    for (const it of o.items || []) {
      ctx.font = `${it.bold ? 700 : 400} ${it.big ? 16 : 12}px ${T.mono}`;
      ctx.fillStyle = it.bold || it.big ? T.ink : T.slate;
      if (it.at === "center") {
        ctx.textAlign = "center";
        ctx.fillText(it.text, (A.left + A.right) / 2, (A.top + A.bottom) / 2);
        continue;
      }
      // 값이 0 인 막대는 요소 좌표가 비어 있다 — 줄 자리는 이름 축에서 직접 잰다.
      const y = horiz ? ch.scales.y.getPixelForValue(it.i) : meta.data[it.i] && meta.data[it.i].y;
      if (y == null || isNaN(y)) continue;
      if (it.at === "col") {
        ctx.textAlign = "right";
        ctx.fillText(it.text, ch.width - 4, y);
      } else {
        const el = meta.data[it.i];
        if (!el || isNaN(el.x)) continue;
        ctx.textAlign = "left";
        ctx.fillText(it.text, Math.max(el.x, el.base) + 6, y);
      }
    }
    ctx.restore();
  },
  afterDraw(ch, _a, o) {
    if (!o || !o.cross || !o.T) return;
    const act = ch.tooltip && ch.tooltip.getActiveElements();
    if (!act || !act.length) return;
    const {ctx, chartArea: A} = ch, x = Math.round(act[0].element.x) + .5;
    ctx.save(); ctx.strokeStyle = o.T.rule; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(x, A.top); ctx.lineTo(x, A.bottom); ctx.stroke(); ctx.restore();
  }};

// 자리와 설정. 캔버스를 세우는 곳은 chMake 한 곳이다.
let chSeq = 0;
const CH_CFG = new Map();
const chMount = (build, aria, box) => {
  const id = `ch${++chSeq}`;
  CH_CFG.set(id, build);
  return `<div class="chwrap" style="${box}"><canvas data-ch="${id}" role="img"
    aria-label="${esc(aria)}"></canvas></div>`;
};
const chMake = cv => {
  if (cv._chart || !window.Chart) return;
  const build = CH_CFG.get(cv.dataset.ch);
  if (!build) return;
  try {
    cv._chart = new Chart(cv, build(chT()));
    cv.dataset.chOk = "1";                       // 섰다 — test_render 가 이 표식을 본다
  } catch (e) { setTimeout(() => { throw e; }); }   // 하나가 죽어도 나머지는 선다. 오류는 그대로 올린다
};
const chEach = (n, f) => {
  if (n.nodeType !== 1) return;
  if (n.matches("canvas[data-ch]")) f(n);
  else n.querySelectorAll("canvas[data-ch]").forEach(f);
};
const chGone = cv => {
  if (cv.isConnected) return;                    // 옮겨졌을 뿐이다 — SM 이 섹션을 제자리로 옮긴다
  if (cv._chart) { cv._chart.destroy(); cv._chart = null; }
  CH_CFG.delete(cv.dataset.ch);
};
if (window.Chart) {
  Chart.defaults.animation = false;
  Chart.defaults.font.family = chT().sans;
  Chart.register(chText);
  new MutationObserver(ms => {
    for (const m of ms) m.addedNodes.forEach(n => chEach(n, chMake));
    for (const m of ms) m.removedNodes.forEach(n => chEach(n, chGone));
  }).observe(document.documentElement, {childList: true, subtree: true});
  // 다크 전환 — 같은 토큰 이름이 다른 색이 된다. 설정을 새 색으로 다시 지어 세운다.
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () =>
    document.querySelectorAll("canvas[data-ch]").forEach(cv => {
      if (cv._chart) { cv._chart.destroy(); cv._chart = null; }
      chMake(cv);
    }));
  // 인쇄 — 숨은 화면이 펴지며 크기가 바뀐다. 캔버스는 스스로 못 따라온다.
  addEventListener("beforeprint", () => Object.values(Chart.instances).forEach(c => c.resize()));
}

// 발산 가로 막대 — 한 변화를 원인별로 가른다(양수는 녹청이 0 선 오른쪽, 음수는 산화동이
// 왼쪽). 예전엔 워터폴이었다: 이전·현재 합계를 0 부터 막대로 세워 변화가 작으면 원인이
// 꼭대기에 실오라기로 눌렸다. 합계는 부른 쪽이 글자로 내고 여기는 원인만 그린다.
// 극성은 색 하나에 싣지 않는다 — 방향(0 의 좌우)과 부호(+/−)로 겹친다. 녹청은 채도가
// 낮아 회색처럼 읽힐 수 있어서다(dataviz 검증기). 값은 오른쪽 한 열에 — 음수 막대 옆에
// 두면 이름 칸과 부딪힌다. 가장 크게 움직인 값만 먹색 굵게.
window.chDiverge = (rows, opt = {}) => {
  if (!rows || !rows.length) return "";
  const fmt = opt.fmt || (v => SM.ui.sign(v) + num(Math.abs(v)));
  const vals = rows.map(r => r.value || 0);
  let lo = Math.min(0, ...vals), hi = Math.max(0, ...vals);
  if (hi === lo) hi = 1;
  // 음수가 양수에 비해 아주 작으면 0 선이 이름 칸에 붙어 음수 막대가 안 보인다 —
  // 그쪽에 너비의 8% 를 남긴다(영역만 넓힐 뿐 척도는 한 줄이다). 반대도 같다.
  const room = (hi - lo) * 0.08;
  if (lo < 0 && lo > -room) lo = -room;
  if (hi > 0 && hi < room) hi = room;
  const top = vals.reduce((m, v) => Math.abs(v) > Math.abs(m) ? v : m, 0);
  const topI = top ? vals.indexOf(top) : -1;
  const aria = rows.map((r, i) => `${r.label} ${fmt(vals[i])}`).join(", ");
  return chMount(T => ({type: "bar",
    data: {labels: rows.map(r => r.label), datasets: [{data: vals.map(v => v || null),
      backgroundColor: vals.map(v => v >= 0 ? T.patina : T.copper),
      borderRadius: 4, borderSkipped: "start", maxBarThickness: 14}]},
    options: {responsive: true, maintainAspectRatio: false, indexAxis: "y",
      layout: {padding: {right: 72}},
      scales: {x: {min: lo, max: hi, display: false}, y: chCat(T)},
      plugins: {legend: {display: false},
        tooltip: chTip(T, {callbacks: {title: c => c[0].label, label: c => fmt(vals[c.dataIndex])}}),
        chText: {T, zero: true,
          items: rows.map((r, i) => ({i, text: fmt(vals[i]), at: "col", bold: i === topI}))}}}}),
    aria, `height:${rows.length * 30 + 8}px`);
};

// 한 줄 100% 스택 — 값이 전부 0 이면(총합 0) 회색 트랙만 그린다(안 깨진다).
// 조각 사이는 2px 표면색 틈, 바깥 두 끝만 둥글다. 범례는 캔버스 밖 HTML(이름·값).
window.chStackRow = (rows, opt = {}) => {
  if (!rows || !rows.length) return "";
  const unit = opt.unit || "";
  const total = rows.reduce((s, r) => s + Math.max(0, r.value || 0), 0);
  const legend = `<div class="chleg">${rows.map(r => `<span><i style="background:${chTone(r.tone)}"></i>${esc(r.label)}
    ${chVal(r.value, unit)}</span>`).join("")}</div>`;
  const aria = total > 0 ? `구성비 — ${rows.map(r =>
    `${r.label} ${(Math.max(0, r.value || 0) / total * 100).toFixed(0)}%`).join(", ")}` : "데이터 없음";
  const last = rows.length - 1;
  const pct = r => (Math.max(0, r.value || 0) / total * 100).toFixed(0);
  return chMount(T => {
    const tone = t => ({patina: T.patina, copper: T.copper, slate: T.slate, bar: T.bar})[t] || T.slate;
    const sets = total > 0 ? rows.map((r, i) => ({label: r.label, data: [Math.max(0, r.value || 0)],
      backgroundColor: tone(r.tone), borderColor: T.card, borderSkipped: false,
      borderWidth: {left: i ? 1 : 0, right: i < last ? 1 : 0, top: 0, bottom: 0},
      borderRadius: {topLeft: i ? 0 : 4, bottomLeft: i ? 0 : 4,
        topRight: i < last ? 0 : 4, bottomRight: i < last ? 0 : 4},
      maxBarThickness: 22, categoryPercentage: 1, barPercentage: 1}))
      : [{data: [1], backgroundColor: T.bar, borderRadius: 4, maxBarThickness: 22}];
    return {type: "bar", data: {labels: [""], datasets: sets},
      options: {responsive: true, maintainAspectRatio: false, indexAxis: "y",
        scales: {x: {stacked: true, min: 0, max: total > 0 ? total : 1, display: false},
          y: {stacked: true, display: false}},
        plugins: {legend: {display: false}, tooltip: total > 0
          ? chTipSays(T, {callbacks: {title: () => "", label: c =>
              `${c.dataset.label} — ${chVal(rows[c.datasetIndex].value, unit)} (${pct(rows[c.datasetIndex])}%)`}})
          : {enabled: false}}}};
  }, aria, "height:30px") + legend;
};

// 가로 막대 목록 — right 가 있으면 값 대신 그 글자를 오른쪽 열에 낸다(순위·배지 등).
// flag 는 방어형/경고 항목 표시(산화동), 아니면 기본(녹청).
window.chBarList = (rows, opt = {}) => {
  if (!rows || !rows.length) return "";
  const unit = opt.unit || "";
  const aria = `${rows.length}개 항목 — ` + rows.map(r => `${r.label} ${chVal(r.value, unit)}`).join(", ");
  return chMount(T => ({type: "bar",
    data: {labels: rows.map(r => r.label), datasets: [{data: rows.map(r => Math.abs(r.value || 0)),
      backgroundColor: rows.map(r => r.flag ? T.copper : T.patina),
      borderRadius: 4, borderSkipped: "start", maxBarThickness: 14, minBarLength: 1}]},
    options: {responsive: true, maintainAspectRatio: false, indexAxis: "y",
      layout: {padding: {right: 64}},
      scales: {x: {min: 0, display: false}, y: chCat(T)},
      plugins: {legend: {display: false},
        tooltip: chTip(T, {callbacks: {title: c => c[0].label,
          label: c => chVal(rows[c.dataIndex].value, unit)}}),
        chText: {T, items: rows.map((r, i) => ({i, at: "col",
          text: r.right != null ? String(r.right) : chVal(r.value, unit)}))}}}}),
    aria, `height:${rows.length * 26 + 8}px`);
};

// 도넛 — on 조각만 강조(녹청), 나머지는 회색 명암만 다르다(새 색 안 만든다).
// 조각 사이 2px 표면색 틈. 한 조각뿐이거나 총합이 0 이어도(빈 도넛) 안 깨진다.
window.chDonut = (rows, opt = {}) => {
  if (!rows || !rows.length) return "";
  const S = opt.width || 120, unit = opt.unit || "";
  const total = rows.reduce((s, r) => s + Math.max(0, r.value || 0), 0);
  const onSum = rows.filter(r => r.on).reduce((s, r) => s + Math.max(0, r.value || 0), 0);
  const centerPct = total > 0 && onSum > 0 ? `${Math.round(onSum / total * 100)}%` : "";
  const pct = r => total > 0 ? ` (${(Math.max(0, r.value || 0) / total * 100).toFixed(0)}%)` : "";
  const aria = total > 0 ? `도넛 — ${rows.map(r =>
    `${r.label} ${(Math.max(0, r.value || 0) / total * 100).toFixed(0)}%`).join(", ")}` : "데이터 없음";
  return chMount(T => ({type: "doughnut",
    data: {labels: rows.map(r => r.label), datasets: [{
      data: rows.map(r => total > 0 ? Math.max(0, r.value || 0) : 1),
      backgroundColor: rows.map((r, i) => r.on ? T.patina : chA(T.slate, .3 + (i % 3) * .12)),
      borderColor: T.card, borderWidth: 2}]},
    options: {responsive: true, maintainAspectRatio: false, cutout: "60%",
      plugins: {legend: {display: false},
        tooltip: chTip(T, {callbacks: {title: c => c[0].label,
          label: c => chVal(rows[c.dataIndex].value, unit) + pct(rows[c.dataIndex])}}),
        chText: {T, items: centerPct ? [{text: centerPct, at: "center", big: true}] : []}}}}),
    aria, `width:${S}px;height:${S}px;flex:none`);
};

// 단계별 감소 — 막대는 전부 같은 hue(녹청)의 명암만 달라진다(단계는 순서형이다).
// 값은 막대 바로 옆, 직전 대비 감소는 오른쪽 열 — 색 위에 글자를 얹지 않는다.
// 감소 글자도 먹색 토큰이다(계열 색을 글자에 안 쓴다).
window.chFunnel = (steps, opt = {}) => {
  if (!steps || !steps.length) return "";
  const unit = opt.unit || "", n = steps.length;
  const mx = Math.max(1, ...steps.map(s => s.value || 0));
  const drop = steps.map((s, i) => {
    const prev = i > 0 ? (steps[i - 1].value || 0) : null;
    return prev ? Math.round((1 - (s.value || 0) / prev) * 100) : null;
  });
  const first = steps[0], last = steps[n - 1];
  const overall = first.value > 0 ? Math.round((1 - last.value / first.value) * 100) : 0;
  const aria = `${n}단계 퍼널 — ${first.label} ${chVal(first.value, unit)}에서 `
    + `${last.label} ${chVal(last.value, unit)}까지, 총 ${overall}% 감소`;
  return chMount(T => ({type: "bar",
    data: {labels: steps.map(s => s.label), datasets: [{data: steps.map(s => s.value || 0),
      backgroundColor: steps.map((s, i) => chA(T.patina, .85 - i * (.35 / Math.max(1, n - 1)))),
      borderRadius: 4, borderSkipped: "start", maxBarThickness: 18, minBarLength: 2}]},
    options: {responsive: true, maintainAspectRatio: false, indexAxis: "y",
      layout: {padding: {right: 72}},
      // 가장 긴 막대 끝에도 값 글자가 설 자리를 남긴다(1.2)
      scales: {x: {min: 0, max: mx * 1.2, display: false}, y: chCat(T)},
      plugins: {legend: {display: false},
        tooltip: chTipSays(T, {callbacks: {title: c => c[0].label,
          label: c => chVal(steps[c.dataIndex].value, unit)
            + (drop[c.dataIndex] != null ? ` · 직전 대비 −${drop[c.dataIndex]}%` : "")}}),
        chText: {T, items: steps.flatMap((s, i) => [{i, text: chVal(s.value, unit), at: "end"},
          ...(drop[i] != null ? [{i, text: s.note || `−${drop[i]}%`, at: "col"}] : [])])}}}}),
    aria, `height:${n * 34 + 8}px`);
};

// 작은 배수 — 스케일이 다른 지표를 한 축에 욱여넣는 대신 그룹마다 자기 축(lo~hi)을
// 갖는다. 그래서 공용 눈금을 안 그린다(그리면 거짓말이 된다) — 트랙만 긋고 점을 그
// 그룹의 0~1 에 놓는다. 값은 툴팁이 원래 단위로 말한다. items[0] 가 주인공(녹청),
// 나머지는 중립(회색), 점은 2px 표면색 링. items 가 비어도 안 깨진다.
window.chDumbbell = (groups, opt = {}) => {
  if (!groups || !groups.length) return "";
  const unit = opt.unit || "";
  const G = groups.map(g => {
    const items = g.items || [], vals = items.map(it => it.pos ?? it.value ?? 0);
    const lo = g.lo != null ? g.lo : Math.min(...vals, 0);
    const hi = g.hi != null ? g.hi : Math.max(...vals, 1);
    const span = hi - lo || 1;
    return {items, vals, pos: vals.map(v => (Math.min(hi, Math.max(lo, v)) - lo) / span)};
  });
  const maxK = Math.max(0, ...G.map(g => g.items.length));
  const titled = groups.some(g => g.title);
  const aria = `작은 배수 ${groups.length}개 — ` + groups.map((g, gi) => g.title
    || G[gi].items.map((it, k) => `${it.label} ${chVal(G[gi].vals[k], unit)}`).join(" · ")).join(", ");
  return chMount(T => ({type: "bar",
    data: {labels: groups.map(g => g.title || ""), datasets: [
      {data: G.map(() => [0, 1]), backgroundColor: T.bar, barThickness: 4, grouped: false, order: 3},
      {data: G.map(g => g.pos.length > 1 ? [Math.min(...g.pos), Math.max(...g.pos)] : null),
        backgroundColor: T.rule, barThickness: 2, grouped: false, order: 2},
      ...Array.from({length: maxK}, (_, k) => ({type: "scatter", order: 1,
        data: G.flatMap((g, gi) => k < g.items.length
          ? [{x: g.pos[k], y: gi, v: g.vals[k], label: g.items[k].label}] : []),
        backgroundColor: k === 0 ? T.patina : T.slate, borderColor: T.card, borderWidth: 2,
        pointRadius: 5, pointHoverRadius: 6}))]},
    options: {responsive: true, maintainAspectRatio: false, indexAxis: "y",
      scales: {x: {min: -0.03, max: 1.03, display: false}, y: {...chCat(T), display: titled}},
      plugins: {legend: {display: false},
        tooltip: chTipSays(T, {filter: c => c.dataset.type === "scatter",
          callbacks: {title: () => "", label: c => `${c.raw.label} — ${chVal(c.raw.v, unit)}`}})}}}),
    aria, `height:${groups.length * 44 + 8}px`);
};

// 시계열 · 면적 + 선(2px). mark 가 있는 점만 산화동으로 찍는다(문자열이면 툴팁에 얹는다).
// 선은 십자선 + 툴팁 — 날짜 어디에 올려도 그날 값이 나온다.
window.chLine = (series, opt = {}) => {
  if (!series || series.length < 2) return "";
  const H = opt.height || 180, unit = opt.unit || "";
  const ys = series.map(p => p.y || 0);
  const mn = Math.min(...ys, 0);
  const last = series[series.length - 1];
  const aria = `${series.length}개 지점 추이 — 최근 ${String(last.x)} ${chVal(last.y, unit)}`;
  return chMount(T => ({type: "line",
    data: {labels: series.map(p => String(p.x)), datasets: [{data: ys,
      borderColor: T.patina, borderWidth: 2, fill: "start", tension: .35, cubicInterpolationMode: "monotone",
      backgroundColor: c => { const a = c.chart.chartArea;
        if (!a) return T.wash;
        const g = c.chart.ctx.createLinearGradient(0, a.top, 0, a.bottom);
        g.addColorStop(0, chA(T.patina, .22)); g.addColorStop(1, chA(T.patina, 0)); return g; },
      borderCapStyle: "round", borderJoinStyle: "round",
      pointRadius: series.map(p => p.mark ? 4 : 0), pointHoverRadius: 4,
      pointBackgroundColor: series.map(p => p.mark ? T.copper : T.patina),
      pointBorderColor: T.card, pointBorderWidth: series.map(p => p.mark ? 2 : 0)}]},
    options: {responsive: true, maintainAspectRatio: false,
      interaction: {mode: "index", intersect: false},
      scales: {
        x: {grid: {display: false}, border: {display: false},
          ticks: {color: T.slate, font: {family: T.mono, size: 12}, maxTicksLimit: 6,
            maxRotation: 0, autoSkipPadding: 12}},
        // reverse — 순위처럼 **작을수록 좋은** 값. 안 뒤집으면 12위→9위로 올라간
        // 선이 아래로 꺾여서 떨어진 것처럼 읽힌다.
        y: {min: mn, reverse: !!opt.reverse,
          grid: {color: T.soft, lineWidth: 1}, border: {display: false},
          ticks: {color: T.slate, font: {family: T.mono, size: 12}, maxTicksLimit: 3,
            callback: v => chVal(Math.round(v), unit)}}},
      plugins: {legend: {display: false},
        tooltip: chTip(T, {callbacks: {title: c => c[0].label,
          label: c => chVal(ys[c.dataIndex], unit) + (series[c.dataIndex].mark
            && series[c.dataIndex].mark !== true ? ` · ${series[c.dataIndex].mark}` : "")}}),
        chText: {T, cross: true}}}}),
    aria, `height:${H}px`);
};

// 기회 kind 의 한국어 라벨·처방(what/acts/deliver)은 이제 파이썬(scoring.py 의
// KINDS 명부)이 정하고, dashboard.py 의 gather() 가 각 기회에 label·play 로 실어
// 보낸다 — 화면은 그리기만 한다. striking_distance(밴드)·content_gap(갈래)도
// 서버가 이미 갈라 낸 것을 받는다. 예전에는 이 산문이 여기 KIND_LABEL·SD_LABEL·
// PLAY 세 벌로 있었고, SD_BAND·GAP_KIND 곁표를 매 렌더마다 되짚었다.

