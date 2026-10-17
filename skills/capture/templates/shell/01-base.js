"use strict";
// $ · esc · num 은 문서 가운데의 작은 스크립트가 이미 세웠다(뷰보다 먼저 있어야
// 하기 때문이다). 여기서 다시 선언하면 같은 global lexical 환경에서 중복 선언이다.
// 배포마다 같은 것을 다르게 부른다: 플러그인은 쉬운 우리말("손댈 것"), 호스팅판은
// 마케터가 쓰는 서치콘솔 한국어 UI 표준어("개선 기회"). 화면이 **자기가 고른다**.
//
// 여태는 호스팅 애드온이 렌더된 한국어를 정규식으로 되짚어 갈아치웠다(terms.js).
// 그래서 원본이 다시 그릴 때마다 또 갈아야 했고, 그 때문에 MutationObserver 가
// 필요했으며, 거기 걸린 모든 손질이 idempotent 여야 했다 — 오늘 그걸 깨서 화면이
// 얼었다. 게다가 그 층은 이미 조용히 낡아 있었다: 규칙 셋은 원본이 문구를 바꾼 뒤
// 대상이 사라졌는데도 남아 있었고, 둘은 좌우가 같은 죽은 규칙이었다.
// 이제 두 문구가 같은 줄에 나란히 선다 — 한쪽만 낡을 수가 없다.
// 표식(SM_HOSTED)은 호스팅 애드온만 세운다(server/assets/dash.html).
const W = (local, web) => (window.SM_HOSTED ? web : local);

// 정적으로 박힌 제목·라벨은 W() 를 못 쓴다(파일에 글자로 있다). 그 자리에는 대체
// 문구를 data-web 속성으로 **나란히** 선언한다: <h2 data-web="개선 기회">다음에 손댈 것</h2>.
// 찾아다니지 않고 선언된 것만, 한 번만 바꾼다 — 속성을 지우므로 다시 훑을 이유가
// 없고, 그래서 MutationObserver 도 필요 없다.
function applyWording(root) {
  if (!window.SM_HOSTED) return;
  (root || document).querySelectorAll("[data-web]").forEach(el => {
    el.textContent = el.dataset.web;
    el.removeAttribute("data-web");
  });
}
/* ── 메뉴 아이콘·묶음 ──────────────────────────────────────────────────
   아이콘은 이름 옆의 눈금이다 — 열한 줄을 훑을 때 글자를 읽기 전에 자리를 잡아
   준다. 정본은 화면 id 하나뿐이고 뷰 파일은 아이콘을 모른다: 뷰가 각자 그리면
   열한 벌이 되고 그중 몇은 언젠가 낡는다(이 리포가 목록을 두 벌 만들고 겪은 일).
   16px · 스트로크 1.5 · 색은 currentColor — 활성 줄에서 글자와 같이 선명해진다.
   외부 아이콘 묶음은 안 쓴다(오프라인·사내망에서 화면이 반쪽이 된다). */
const NAV_ICON = {
  triage: `<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M2.5 4.5h8M2.5 8h5.5M2.5 11.5h3"/><path d="M9.5 10.8l1.7 1.7L14.2 9"/></svg>`,
  hold: `<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M1.8 8S4.3 3.8 8 3.8 14.2 8 14.2 8 11.7 12.2 8 12.2 1.8 8 1.8 8z"/><circle cx="8" cy="8" r="1.9"/></svg>`,
  overview: `<svg viewBox="0 0 16 16" aria-hidden="true"><rect x="2" y="2" width="5" height="5" rx="1"/><rect x="9" y="2" width="5" height="5" rx="1"/><rect x="2" y="9" width="5" height="5" rx="1"/><rect x="9" y="9" width="5" height="5" rx="1"/></svg>`,
  analysis: `<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M2 14h12"/><path d="M4.5 14V8.5"/><path d="M8 14V3"/><path d="M11.5 14V6"/></svg>`,
  keywords: `<svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="7" cy="7" r="4.5"/><path d="M10.4 10.4 14 14"/></svg>`,
  kwmanage: `<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M2.5 4h3M8.5 4h5M2.5 8h7M12.5 8h1M2.5 12h1.5M7 12h6.5"/><circle cx="7" cy="4" r="1.5"/><circle cx="11" cy="8" r="1.5"/><circle cx="5.5" cy="12" r="1.5"/></svg>`,
  rank: `<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M2 11.5 6 7l3 3 5-5.5"/><path d="M10.5 4.5H14V8"/></svg>`,
  aicite: `<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M7 2.2 8.3 5.4 11.5 6.7 8.3 8 7 11.2 5.7 8 2.5 6.7 5.7 5.4z"/><path d="M12 10l.6 1.4 1.4.6-1.4.6-.6 1.4-.6-1.4-1.4-.6 1.4-.6z"/></svg>`,
  aisum: `<svg viewBox="0 0 16 16" aria-hidden="true"><rect x="2" y="2.5" width="12" height="11" rx="1.5"/><path d="M4.8 6h6.4M4.8 8.5h6.4M4.8 11h3.4"/></svg>`,
  site: `<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M12.5 6.5V13a1.5 1.5 0 0 1-1.5 1.5H5A1.5 1.5 0 0 1 3.5 13V3A1.5 1.5 0 0 1 5 1.5h4z"/><path d="M9 1.5v4a1 1 0 0 0 1 1h2.5"/><path d="M6 10.2 7.4 11.6 10 9"/></svg>`,
  backlinks: `<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M6.6 9.4a3 3 0 0 0 4.2 0l2-2a3 3 0 0 0-4.2-4.2l-1 1"/><path d="M9.4 6.6a3 3 0 0 0-4.2 0l-2 2a3 3 0 0 0 4.2 4.2l1-1"/></svg>`,
  competitors: `<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M2.5 5h8"/><path d="M8 2.5 10.5 5 8 7.5"/><path d="M13.5 11h-8"/><path d="M8 8.5 5.5 11 8 13.5"/></svg>`,
  history: `<svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="6"/><path d="M8 4.6V8l2.4 1.5"/></svg>`,
  guide: `<svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="6"/><path d="M10.6 5.4 9.2 9.2 5.4 10.6 6.8 6.8z"/></svg>`,
  settings: `<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M2.5 4.5h4M11 4.5h2.5"/><circle cx="8.8" cy="4.5" r="1.8"/><path d="M2.5 11.5h2.2M9 11.5h4.5"/><circle cx="6.9" cy="11.5" r="1.8"/></svg>`,
};
// 메뉴 묶음 = 사용자의 질문 = 다시 재는 단위(할 일 · 검색 성과 · AI 노출 · …).
// 정본은 run_all.GROUPS 한 벌이다. 화면은 자기 view-def 에 묶음 id("group")만 적고,
// 묶음 이름·단계·주기·마지막으로 잰 날은 페이로드(d.groups)로 받는다 — 여기서 이름을
// 다시 적지 않는다(적는 순간 두 벌이 되고 한쪽만 낡는다).

// 새로고침해도 보던 화면을 잃지 않는다 — 탭 하나가 쓰는 sessionStorage 에 마지막으로
// **직접 연** 화면만 적는다(SM.show 가 touched 일 때만 적는다 — land() 가 고른 것은
// 사용자 선택이 아니다). 사이트는 이미 hash 로 남으니 여기서는 화면 id 하나만.
// 사생활 모드·저장소 차단이면 조용히 무시한다 — 복원이 안 되는 것뿐 화면은 그대로 선다.
function saveLastView(id) { try { sessionStorage.setItem("sm:lastView", id); } catch (e) {} }
function lastView() { try { return sessionStorage.getItem("sm:lastView") || ""; } catch (e) { return ""; } }

