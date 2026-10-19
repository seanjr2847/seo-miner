
// SERP 정수 순위의 Δ — 판정(flat 여부)은 서버(scoring.rank_delta)가 하고,
// 여기서는 마크업만 만든다. 보합일 때 실제 값은 title로 남긴다.
const rankDelta = rd => {
  if (!rd) return "";
  const d = typeof rd === "object" ? rd.delta : rd;
  const flat = typeof rd === "object" ? rd.flat : (Number.isFinite(d) && Math.abs(d) < (RULES.rank_noise || 3));
  if (!Number.isFinite(d)) return "";
  if (flat)
    return `<span class="d muted" title="실제 변화 ${SM.ui.sign(d)}` +
           `${Math.abs(d)}칸. ±${(RULES.rank_noise || 3) - 1}칸까지는 흔들림으로 봅니다.">보합</span>`;
  return delta(d);
};
// 머리글 표기: ">"로 시작하면 숫자 열(우측 정렬), "?"뒤는 그 열의 근거 설명.
//   ">순위?구글이 보고한 평균 게재순위"
// 줄이 SORT_MIN 을 넘으면 머리글이 정렬 손잡이가 되고 머리글 자체가 붙박인다.
const SORT_MIN = 8;
// cls="cards" — 좁은 화면(≤560px)에서 줄을 카드로 쌓는다(.tw.cards). 칸 라벨은 여기서
// 머리글 이름으로 단다 — 뷰가 열 이름을 CSS 에 다시 적으면 머리글과 두 벌이 된다.
const table = (heads, rows, empty, cls) => {
  // 빈 표에도 다음 걸음이 있어야 한다 — 부르는 쪽이 empty 로 더 구체적인 말을
  // 주면 그쪽이 이긴다(그 화면이 무엇을 돌려야 채워지는지 안다).
  // 이 기본 문장은 배포로 안 가른다: 무엇을 누르는지는 화면 머리의 칩·버튼이 이미
  // 말하고, 여기서 "명령을 치세요 / 버튼을 누르세요"를 갈라 봐야 같은 말을 두 낱말로
  // 부르는 것뿐이다. 갈라야 할 것은 낱말이 아니라 행동이고, 행동은 empty 가 갖는다.
  if (!rows.length)
    return `<div class="empty">${empty ||
      "<b>아직 채울 자료가 없습니다</b><p>이 화면을 채우는 단계를 한 번 돌리면 표가 생깁니다.</p>"}</div>`;
  const long = rows.length >= SORT_MIN;
  const th = heads.map((h, i) => {
    const num = h.startsWith(">");
    const [name, why] = h.replace(/^>/, "").split("?");
    // 이름이 없는 머리글(체크 열 같은)은 정렬 손잡이가 아니다 — 탭 정지만 늘고 낭독기가 이름 없는 칸을 읽는다.
    const sortable = long && name.trim() !== "";
    return `<th class="${num ? "num" : ""}"${sortable ? ` data-s="${i}" tabindex="0" aria-sort="none"` : ""}>` +
      // 툴팁은 fixed 라 표 상자 안에서도 안 잘린다 — title 은 안 싣는다(실으면
      // 말풍선과 브라우저 툴팁이 같은 말을 두 번 띄운다). 키보드로도 닿아야 하므로
      // tabindex 를 주고 aria-label 로 같은 말을 갖는다.
      `${esc(name)}${why ? SM.ui.hint(why, {title: false}) : ""}</th>`;
  }).join("");
  let body = rows.join("");
  if (cls === "cards") {
    const names = heads.map(h => h.replace(/^>/, "").split("?")[0]);
    const tb = document.createElement("template");
    tb.innerHTML = `<table><tbody>${body}</tbody></table>`;
    const tbody = tb.content.querySelector("tbody");
    // 열 번호는 칸 수가 아니라 colspan 을 더해 센다 — 칸을 합친 줄([AI 인용] 시장별의
    // "질문이 없어…" 줄)에서 뒤 칸이 한 열씩 당겨져 [질문 만들기] 칸이 "언급률"로 붙었다.
    [...tbody.rows].filter(tr => !tr.classList.contains("exp")).forEach(tr => {
      let col = 0;
      [...tr.cells].forEach(td => {
        const i = col;
        col += td.colSpan || 1;
        if (i && names[i] && !td.hasAttribute("colspan")) td.dataset.label = names[i];
      });
    });
    body = tbody.innerHTML;
  }
  const t = `<table><thead><tr>${th}</tr></thead><tbody>${body}</tbody></table>`;
  return `<div class="tw${long ? " long" : ""}${cls ? " " + cls : ""}">${t}</div>`;
};

// 표는 화면 밖에서도 이어진다 — 지금 걸러 놓은 목록 그대로 파일로 준다.
// 맨 앞 BOM 이 있어야 엑셀이 한글을 안 깬다. 화면마다 이 함수를 다시 만들고 있었고
// (여덟 벌) 그 사이 처리가 갈라졌다 — 한 벌만 String(c ?? "") 이고 나머지는
// String(c) 라, 빈 칸이 literal "null" 로 나갔다. 여기가 그 한 자리다.
const csv = (name, rows) => {
  const body = "\uFEFF" + rows.map(r => r.map(
    c => `"${String(c ?? "").replace(/"/g, '""')}"`).join(",")).join("\r\n");
  saveFile(`seo-miner-${name}-${new Date().toISOString().slice(0, 10)}.csv`, body, "text/csv;charset=utf-8");
};
// \uBE0C\uB77C\uC6B0\uC800 \uC548\uC5D0\uC11C \uD30C\uC77C \uD558\uB098\uB97C \uB0B4\uB824\uBC1B\uAC8C \uD558\uB294 \uC790\uB9AC\uB294 \uC774 \uD55C \uBC8C\uC774\uB2E4 \u2014 CSV \uB3C4, [\uAC1C\uC694] \uD560 \uC77C
// \uC11C\uB78D\uC758 .md \uBB38\uC11C\uB3C4 \uC5EC\uAE30\uB85C \uC628\uB2E4(\uD654\uBA74\uC774 Blob \uC744 \uB530\uB85C \uBE5A\uC73C\uBA74 \uCC98\uB9AC\uAC00 \uB610 \uAC08\uB77C\uC9C4\uB2E4).
function saveFile(filename, body, type) {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([body], {type}));
  a.download = filename;
  a.click();
  URL.revokeObjectURL(a.href);
}

// 정렬은 위임 한 곳에서. 표를 그리는 쪽은 아무것도 안 해도 된다.
// 마우스(클릭)와 키보드(Enter·Space)가 같은 함수를 쓴다. 정렬 상태는 클래스(모양)와
// aria-sort(낭독기)가 함께 말한다.
const sortTh = th => {
  const i = +th.dataset.s, tbl = th.closest("table");
  const desc = !th.classList.contains("desc");
  tbl.querySelectorAll("th[data-s]").forEach(o => {
    o.classList.remove("asc", "desc");
    o.setAttribute("aria-sort", "none");
  });
  th.classList.add(desc ? "desc" : "asc");
  th.setAttribute("aria-sort", desc ? "descending" : "ascending");
  const body = tbl.tBodies[0];
  // 숫자 열이면 숫자로, 아니면 글자로. "12위"·"1,204"·"+8" 같은 표기에서 수를 꺼낸다.
  const key = tr => {
    const c = tr.cells[i]; if (!c) return desc ? -Infinity : Infinity;
    const t = c.textContent.trim();
    if (!th.classList.contains("num")) return t;
    const n = parseFloat(t.replace(/[^\d.+-]/g, ""));
    return Number.isFinite(n) ? n : (desc ? -Infinity : Infinity);
  };
  [...body.rows]
    .sort((a, b) => { const x = key(a), y = key(b);
      const r = typeof x === "string" ? x.localeCompare(y, "ko") : x - y;
      return desc ? -r : r; })
    .forEach(tr => body.appendChild(tr));
};
document.addEventListener("click", e => {
  const th = e.target.closest("th[data-s]");
  // 머리글 안의 (i) 는 근거를 보려고 누르는 것이다 — 표 순서를 바꾸면 안 된다.
  if (!th || e.target.closest(".hint")) return;
  sortTh(th);
});
document.addEventListener("keydown", e => {
  if (e.key !== "Enter" && e.key !== " ") return;
  const th = e.target.closest && e.target.closest("th[data-s]");
  if (!th || e.target.closest(".hint")) return;
  e.preventDefault();          // Space 가 화면을 내리지 않게
  // 정렬을 직접 부르지 않고 클릭을 쏜다 — 클릭 경로에만 걸린 일(예: 사이트 점검 표가 펼친 줄
  // tr.exp 를 접는 stExpBind)이 키보드에서도 똑같이 돈다. 두 길이 갈라지면 한쪽만 낡는다.
  th.click();
  th.focus();                   // 표 순서가 바뀌어도 머리글에 포커스가 남는다
});

