/* ── 기회 한 벌: 대상으로 찾아 쓰는 창구 ────────────────────────────────────
      개요만 이 기계에 물려 있었다. 나머지 화면은 숫자만 보여 주고 "뭘 먼저",
      "왜", "했다고 표시"를 못 했다 — 재료(d.opps)는 내내 같은 페이로드에 실려
      왔는데 아무도 안 읽었다. 화면은 아래 여섯만 안다:

        oppOf(대상)    한 건 (점수 높은 순 첫째)      oppsOf(대상)  전부
        oppScore(o)    점수 칸        oppMeter(o)    막대
        oppWhy(o)      종류 라벨 + 근거               oppActs(o)    확인·완료·제외
        oppPanel(o, 손잡이)  펼침 내용 (행동 줄·할 일·페이지·고칠 곳·프롬프트)

      대상 문자열은 화면마다 출처가 다르다(GSC 검색어·추적 키워드·URL·도메인).
      대소문자와 앞뒤 공백만 맞춰 찾는다 — 그 이상은 맞히려 들지 않는다.  ── */
// 상태 값의 정본은 db.OPP_STATUSES 다(test_seams 17). 심사가 "봤다"를 떠맡으니
// acked 는 "진행 중"이다 — "확인함"은 봤다는 건지 하기로 했다는 건지 말하지 않았다.
// resolved 는 사람이 누르는 값이 아니다 — 새 데이터가 조건이 풀렸다고 긍정할 때 서버가
// 닫는다(scoring.resolve_stale). 우리가 작업해서 끝난 done 과 갈라 둔다: 섞으면 저절로
// 풀린 것을 우리 효과로 센다. 그래서 라벨·다음 버튼 표에만 있고 OPP_SET 에는 없다.
// 버튼 이름 = 상태 이름이다(아래 OPP_SET). 예전엔 상태는 '이 기회만 뺌'인데 버튼은 '이 기회만
// 빼기', 상태는 '완료'인데 버튼은 '완료 표시'라 같은 것을 두 이름으로 불렀다.
// resolved 의 이름은 "누가 닫았나"다 — 예전 이름 '저절로 풀림'은 좋아졌다고 읽혔는데,
// theotherskin 252건 중 해결된 것은 20건이고 179건은 판정 규칙을 고친 뒤 옛 규칙 줄을 정리한 것이었다.
SM.api.OPP_LABEL = {new:"할 일", acked:"진행 중", done:"완료", dismissed:"제외",
                    resolved:"자동 종료"};
// 도구가 왜 닫았나 — 갈래의 정본은 db.RESOLVED_TAGS(test_seams 가 대조), 정하는 곳은
// scoring.Why. 배지가 이 말을 단다. 갈래가 없는 옛 줄은 OPP_LABEL.resolved 그대로다.
SM.api.OPP_TAG_LABEL = {better:"해결됨", merged:"중복 합침",
                        misjudged:"기준 바뀌어 정리", out:"판단 중단"};
SM.api.OPP_TAG_TIP = {
  better: "새 데이터가 목표를 이뤘다고 확인했습니다. 다시 나빠지면 저절로 다시 열립니다.",
  merged: "같은 일을 다른 기회 줄이 맡습니다. 할 일은 그 줄 하나입니다.",
  misjudged: "판정 규칙을 고쳤거나 나중에 들어온 데이터로 판단이 바뀌어, 지금 기준으로는 이 기회가 서지 않습니다. 좋아진 것이 아닙니다.",
  out: "판단할 조건 밖으로 나가 더 볼 수 없습니다(추적을 껐거나, 노출이 적거나, 순위·범위 밖으로 밀림). 좋아진 것이 아닙니다 — 나빠진 것일 수도 있습니다."};
// 기회 한 건의 상태 배지 — 도구가 닫은 것이면 갈래까지 말하고, 마우스를 올리면 뜻을 푼다.
SM.api.oppStBadge = (o, el = "span") => {
  const t = o.status === "resolved" && window.OPP_TAG_LABEL[o.status_tag] ? o.status_tag : "";
  const tip = t ? window.OPP_TAG_TIP[t] : "이 기회의 지금 상태입니다.";
  return `<${el} class="st st-${esc(o.status)}${t ? " st-tag-" + t : ""}" title="${esc(tip)}"
    >${t ? window.OPP_TAG_LABEL[t] : (window.OPP_LABEL[o.status] || esc(o.status))}</${el}>`;
};

// 상태 거르개의 정본 — 여덟 화면이 이것을 갖다 쓴다. 화면마다 목록을 베끼면 새 상태가
// 생길 때 몇 벌만 늙는다(개요에만 있던 시절, 그 한 벌에 `자동 종료` 이 빠져 있어
// 도구가 혼자 닫은 기회를 어느 화면에서도 모아 볼 수 없었다).
// 묶음 이름은 사람의 말이다: '아직 안 함' = 손 안 댄 것 + 하는 중, '끝낸 것' = 우리가
// 끝낸 것 + 제외한 것. `자동 종료` 는 따로 둔다 — 우리가 한 일이 아니라서 섞으면
// "고쳤다" 로 읽힌다.
// `관찰 중` 은 상태가 아니라 서버가 얹는 판정이다(o.hold — dashboard._axis_hold): 고친 지
// scoring.OBSERVE_DAYS 가 안 된 페이지에 걸린 열린 기회. 저장된 상태를 바꾸지 않고 할 일
// 묶음에서만 뺀다 — 앞 수정의 효과를 재기도 전에 같은 자리를 다시 고치라고 권하지 않게.
// 기간이 끝나면 서버가 hold 를 안 얹으니 저절로 돌아온다.
SM.api.OPP_ST_GROUP = {
  all: ["new", "acked", "done", "dismissed", "resolved"],
  open: ["new", "acked"], new: ["new"], acked: ["acked"],
  closed: ["done", "dismissed"], resolved: ["resolved"], hold: ["new", "acked"]
};
SM.api.OPP_ST_OPT = [["", "남은 것"], ["all", "전체"], ["open", "아직 안 함"], ["new", "할 일"],
                     ["acked", "진행 중"], ["hold", "관찰 중"], ["closed", "완료·제외"],
                     ["resolved", "자동 종료"]];
// 기회 하나가 고른 묶음에 드나. 기본값("")은 **남은 것**이다: 기회가 안 붙은 줄 + 아직 안 한
// 기회(할 일·진행 중)가 붙은 줄. 끝낸 것(완료·제외), 도구가 닫은 것, 관찰 중인 것은 빠진다.
// 개요 말고 다른 화면(분석·키워드·순위 추적·AI 인용·사이트 점검·경쟁 분석·백링크)은 목록이
// 기회 목록이 아니라 검색어·주소 측정 목록이라 기본값이 "전체"였다 — 그래서 [제외]한 검색어와
// 이미 고친 검색어가 할 일 목록처럼 계속 섰고, 띠의 "1순위"만 그 줄을 따로 걸렀다. 기회가 안
// 붙은 줄은 측정이라 기본값에 그대로 선다(o 가 null 이면 통과). 전부 보는 길은 「전체」(all)다.
// 박제본(READONLY)은 거르개가 없어 기본값이 곧 전부다 — 그때의 기록 자체가 볼거리다.
// 관찰 중인 기회는 [관찰 중] 묶음과 「전체」에서만 보인다.
SM.api.oppStMatch = (o, st) => {
  if (!st && READONLY) st = "all";
  if (st === "all") return true;
  if (!st) return !o || (window.OPP_ST_GROUP.open.includes(o.status) && !o.hold);
  return !!o && (window.OPP_ST_GROUP[st] || []).includes(o.status) && (st === "hold") === !!o.hold;
};
// 기본값(남은 것)으로 걸러 목록이 비었는데 줄은 있을 때 — 전부 끝냈거나 관찰 중이다. 화면마다
// [조건 지우기]는 기본값으로 되돌리므로 여기서 누르면 헛바퀴가 된다. 「전체」로 보내는 손잡이와
// 함께 "다 처리했다"는 좋은 소식으로 그린다. rows 는 거르기 전 줄, opp 는 줄 → 기회.
SM.api.oppLeftDone = (st, rows, opp) => !st && !READONLY && rows.length > 0
  && rows.every(r => !window.oppStMatch(opp(r), ""));
// what 은 줄의 이름(검색어·질문·링크…) — 조사가 안 붙는 자리에만 쓴다. onclick 은 화면이 주는
// 식이다: 거르개를 「전체」로 바꾸고 **거르개 자신도 다시 그려야** 선택지가 따라온다.
SM.api.oppLeftEmpty = (what, n, onclick) => `<div class="empty ok"><b>남은 것이 없습니다</b>
  <p>끝냈거나 관찰 중인 ${esc(what)} ${num(n)}개는 거르개의 「전체」에 있습니다.</p>
  <div class="acts"><button class="go sec sm" onclick="${onclick}">전체 보기</button></div></div>`;
// 거르개 한 벌 — 화면은 이걸 부르고 자기 상태 변수만 갖는다.
// 박제본에는 안 세운다 — 손댈 수 없는 기록이라 그때 그 사람이 쓰던 손잡이는 뜻이
// 없고, 종이로 나가면 <select> 가 글자 하나로 굳는다. 화면마다 READONLY 를 묻지
// 않고 여기 한 곳에서 막는다(test_render 의 박제본 규칙).
SM.api.oppStSelect = (cur, fn, count) => READONLY ? "" : `<select onchange="${fn}(this.value)"
    aria-label="상태로 거르기">${window.OPP_ST_OPT.map(([v, l]) =>
    `<option value="${v}"${v === cur ? " selected" : ""}>${esc(l)}`
    + (count ? ` (${count(v)})` : "") + `</option>`).join("")}</select>`;

window.SD_PERIOD = "";        // "28일" — 평균 게재순위라는 주석에 쓰는 기간

const oppKey = t => String(t ?? "").trim().toLowerCase();
// 기회 대상을 사람에게 보여줄 이름 — coverage 기회의 target 은 scoring 이
// 'cluster:{이름}' 으로 적재한다(미분류는 'cluster:(미분류)'). API·복사 텍스트는
// 원문을 그대로 쓰고, 사람이 읽는 자리(개요 띠·카드 제목 등)만 이걸 거친다.
const targetLabel = t => {
  if (typeof t !== "string" || !t.startsWith("cluster:")) return t;
  const name = t.slice(8);
  return name === "(미분류)" ? "미분류" : `주제 묶음 · ${name}`;
};
let OPP_INDEX = {};
// render() 가 매 적재마다 다시 세운다. 같은 대상에 기회가 둘 이상 붙는다
// (검색어 하나가 striking_distance 이면서 ctr_gap 일 수 있다) — 배열로 둔다.
function oppIndex(opps) {
  OPP_INDEX = {};
  for (const o of opps || []) {
    const k = oppKey(o.target);
    (OPP_INDEX[k] || (OPP_INDEX[k] = [])).push(o);
  }
  // 대표(oppOf)는 열린 기회가 먼저다, 그다음 점수. 점수만 보면 끝난 기회가 같은 검색어의 열린
  // 기회를 가렸다: 'syringomas vs milia' 줄이 완료된 #201(66점)을 들고 서서, 같은 검색어의 관찰
  // 중인 열린 기회 둘(65.5점)이 거르개에 안 걸렸다 — 이미 고친 검색어가 [순위 상승 기회]의 할 일
  // 목록에 「완료」로 계속 섰다. 열린 것이 없으면 예전처럼 점수 1등(닫힌 것)이 대표다.
  const open = o => window.OPP_ST_GROUP.open.includes(o.status);
  for (const k in OPP_INDEX)
    OPP_INDEX[k].sort((a, b) => (open(b) - open(a)) || ((b.score ?? 0) - (a.score ?? 0)));
}
SM.api.oppsOf = t => OPP_INDEX[oppKey(t)] || [];
SM.api.oppOf = t => (OPP_INDEX[oppKey(t)] || [])[0] || null;

// 점수가 없는 줄(기회가 안 붙은 대상)에서는 단위도 설명도 안 붙인다 — 좁은 칸에서
// "—" 와 "점" 이 두 줄로 갈라져 고장난 것처럼 읽히고, 뜯어보면 "— 점 / 100점"이라는
// 말이 안 되는 안내가 붙어 있었다. 자리(width)는 그대로 잡아 둔다: 빼 버리면
// 옆 열이 줄마다 다른 자리에서 시작한다.
SM.api.oppScore = o => Number.isFinite(o && o.score)
  ? `<div class="score" title="${o.score}점 / 100점. 높을수록 먼저 손댑니다.">`
    + `${Math.round(o.score)}<span class="u">점</span></div>`
  : `<div class="score none" title="이 줄에는 아직 붙은 기회가 없습니다.">—</div>`;

SM.api.oppMeter = o => Number.isFinite(o && o.score)
  ? `<div class="meter${isDefensive(o) ? " def" : ""}" title="${o.score}점 / 100점">
       <i style="width:${Math.min(100, Math.max(0, o.score))}%"></i></div>` : "";

const isDefensive = o => o && (o.is_defensive
  ?? ["rank_decay", "cannibalization", "backlink_broken", "crawl_issue"].includes(o.kind));

// GA4 매출 잠재력 배지 — dashboard.py gather() 가 승수가 1.0이 아닐 때만 ga4_mult 를
// 얹어 보낸다(scoring.value_mult). 값이 없으면 아무것도 안 그린다 — GA4 미연결
// 프로젝트는 지금까지와 똑같다.
SM.api.gaBadge = o => o && o.ga4_mult != null
  ? `<span class="gm lat ${o.ga4_mult > 1 ? "gm-up" : "gm-down"}"
       title="GA4 전환 반영 전 ${o.ga4_pre_score ?? "—"}점 → ${o.ga4_mult > 1 ? "전환 확인" : "전환 0 확인"} ×${o.ga4_mult}"
       >GA4 ${o.ga4_mult > 1 ? "▲" : "▼"} ×${o.ga4_mult}</span>` : "";

// 이 기회로 실제로 고친 것이 있나 — 접힌 줄에서 "열어만 놓음"과 "적용까지 함"을 가른다.
// 열기(run_tool)는 창이 뜨면 묶인 기회 전부를 '진행 중'으로 찍으므로, 그 상태만으로는
// 도구가 파일을 고쳤는지 알 수 없었다(기록은 펼쳐야만 보였다). 완료는 여전히 사람이
// 누른다 — 완료 후 관찰의 기준 시각이 '실제로 끝난 그때' 여야 하기 때문이다.
// 세는 범위는 묶인 id 전부 — 상태 버튼(setOpps)·열기(run_tool)와 같아야 한 줄이
// 새로고침 뒤 둘로 갈라지지 않는다.
SM.api.madeFor = o => {
  if (!o) return [];
  const ids = new Set((o.group ? o.group.ids : [o.id]).map(String));
  return (window.CREATIONS || []).filter(c => ids.has(String(c.opportunity_id)));
};
SM.api.madeBadge = o => {
  const made = window.madeFor(o);
  if (!made.length) return "";
  const merged = made.filter(c => c.merged).length;
  const label = merged === made.length ? `머지됨 ${merged}건`
              : merged ? `적용 ${made.length}건 · 머지 ${merged}`
              : `적용 ${made.length}건`;
  return `<span class="badge made" title="이 기회로 고친 파일이 ${made.length}건 기록돼 있습니다${
    merged ? ` (그중 ${merged}건 머지됨)` : " — 아직 머지된 것은 없습니다"
  }. [완료]는 '완료 후 관찰'을 보고 직접 누르세요.">${label}</span>`;
};

// 관찰 중 배지 — 이 기회가 할 일 목록에서 빠져 있는 이유를 줄 위에서 말한다. 판정은
// 서버(o.hold)가 했다. 끝나는 날을 같이 적는다: "언제 돌아오나"가 없으면 사라진 줄로 읽힌다.
SM.api.holdBadge = o => o && o.hold
  ? `<span class="badge adv" title="이 페이지(${esc(o.hold.page)})를 고친 지 얼마 안 돼 효과를 지켜보는 중입니다. ${
      esc(o.hold.until)}에 할 일 목록으로 돌아옵니다.">관찰 중 · ${esc(String(o.hold.until).slice(5))}까지</span>` : "";

// 칩에 손을 얹으면 여태 `striking_distance` 같은 **내부 id** 가 떴다 — 사용자가
// 본 적 없는 말이고, 라벨이 이미 한글인데 그 옆에 영문 id 를 들이미는 셈이었다.
// 통칭표(kind_labels, 정본은 scoring.KINDS)의 한글 말로 답한다.
SM.api.kindTip = o => (window.KIND_LABELS || {})[o.kind] || o.label || "";
SM.api.oppWhy = o => !o ? "" : `<div class="oh">
    <span class="kind${isDefensive(o) ? " k-def" : ""}" title="${esc(window.kindTip(o))}"
      >${esc(o.label || o.kind)}</span>
    ${window.oppStBadge(o)}
    ${window.holdBadge(o)}
    ${window.gaBadge(o)}
  </div>${o.reasoning ? `<div class="why">${esc(o.reasoning)}</div>` : ""}${window.oppClosedWhy(o)}`;

// 왜 닫혔나 — 도구가 혼자 닫은 것(자동 종료)과 작업 기록을 보고 닫은 것에만 사유가
// 붙는다(db 의 status_reason, scoring.resolve_stale·close_opportunity_by_work 가 쓴다).
// 표에는 내내 있었는데 읽는 화면이 없어서, "판정이 틀려서 닫혔나"를 볼 길이 없었다.
// 사람이 누른 상태에는 사유가 없다 — 그때는 이 줄이 안 뜬다.
SM.api.oppClosedWhy = o => o && o.status_reason
  ? `<div class="why closed-why">닫힌 이유: ${esc(o.status_reason)}</div>` : "";

/* 상태 바꾸기. 라벨은 누른 뒤에 무엇이 보이는지로 짓는다 — "확인"은 무엇이
   달라지는지를 말하지 않는다. 누르면 카드의 상태 배지가 그 말로 바뀐다.

   접힌 줄에는 **다음 하나만** 둔다. 셋을 다 두면 두 줄짜리 목록에 버튼이 여섯 개
   서고, 그중 다섯은 지금 누를 일이 없는 것이라 목록이 버튼밭으로 읽힌다.
   나머지는 펼침 패널이 갖는다 — 거기서는 진짜 다음 행동(요청문 만들기)이
   주요고 상태 바꾸기는 보조다. */
// '할 일'의 다음 걸음은 상태 버튼이 아니라 **일을 도구에 넘기는 행동**이다 — 로컬은
// [(도구)로 열기](SM.host.oppBtn → run_tool), 호스팅은 [요청문 복사](copyPrompt). 둘 다
// 넘기면서 '진행 중'으로 옮긴다. 예전엔 상태만 바꾸는 [작업 시작]이 따로 있어서, 로컬에서는
// 열기와 같은 일을 반쪽만 하는 버튼이 나란히 섰다.
const OPP_NEXT = {acked: ["done", "완료"],
                  done: ["new", "다시 열기"], dismissed: ["new", "다시 열기"],
                  resolved: ["new", "다시 열기"]};
// [제외]는 펼침 패널(oppRest)에만 선다 — 검색어는 맞는데 이 진단 하나가 틀렸을 때 쓰는
// 자리다. 검색어째 빼는 일은 [심사]가 한다.
const OPP_SET = {done: "완료", dismissed: "제외", new: "다시 열기"};
// 묶인 줄([개요]의 목록 — o.group 은 서버 scoring.group_opportunities 가 준 것)은
// 대표 하나가 아니라 묶인 id 전부에 먹는다. 이미 그 상태인 것은 다시 안 보낸다.
const oppIdsTo = (o, st) => o.group
  ? o.group.variants.filter(v => v.status !== st).map(v => v.id) : [o.id];
const oppBtn = (o, st) => o.group && o.group.ids.length > 1
  ? `<button onclick="setOpps([${oppIdsTo(o, st).join(",")}],'${st}')"
      title="이 줄에 묶인 기회 ${o.group.ids.length}개에 모두 적용합니다">${OPP_SET[st]}</button>`
  : `<button onclick="setOpp(${o.id},'${st}')">${OPP_SET[st]}</button>`;

// 박제본에는 서버가 없다 — 누르면 저장된다는 말이 거짓이 되므로 버튼을 안 낸다.
// 콘텐츠 작성 버튼은 호스팅에만 있다(SM.host.oppBtn) — 플러그인은 Claude 채팅으로 간다.
SM.api.oppActs = o => {
  if (!o || READONLY || o.id == null) return "";
  // 접힌 줄에는 상태를 바꾸는 다음 하나 + 콘텐츠 작성(호스팅 전용)뿐이다.
  // 작성은 상태 바꾸기와 다른 축이라 여기 남는다 — 이 기회로 실제로 할 일이다.
  const n = OPP_NEXT[o.status];
  const write = SM.host.oppBtn ? SM.host.oppBtn(o) : "";
  if (!n && !write) return "";
  return `<div class="acts" onclick="event.stopPropagation()"` +
    `>${n ? oppBtn(o, n[0]) : ""}${write}</div>`;
};

/* 지금 상태 칩 — 상태 버튼 바로 앞에 선다.

   버튼은 **무엇으로 바꾸나**만 말한다("완료"). 지금이 무엇인지는 접힌 줄이
   말해 왔는데, [개요]는 '할 일'일 때 그 배지를 일부러 안 단다(기본 거르개가 이미
   그 말이라 스무 줄이 같은 배지를 단다). 그래서 기본 상태의 카드를 펼치면
   지금이 어디인지 말해 주는 것이 하나도 없이 버튼 셋만 서 있었다.

   READONLY·박제본에서도 낸다 — 거기선 버튼이 아예 없어서 이게 유일한 상태 표시다.
   말은 OPP_LABEL 한 벌에서 온다(정본은 db.OPP_STATUSES). */
const oppNow = o => !o || !o.status ? "" : window.oppStBadge(o);

// 펼침 패널이 갖는 나머지 상태 — 지금 상태와 접힌 줄이 이미 낸 것은 뺀다.
const oppRest = o => {
  if (!o || READONLY || o.id == null) return "";
  const shown = (OPP_NEXT[o.status] || [])[0];
  const rest = Object.keys(OPP_SET).filter(st => st !== o.status && st !== shown);
  return rest.length
    ? `<div class="acts oppset">${rest.map(st => oppBtn(o, st)).join("")}</div>` : "";
};

// extra 는 이 화면만 아는 손잡이(oppActs·다른 화면으로 가기)를 패널 맨 위 행동 줄에
// 함께 세우는 자리다. 안 넘기면 요청문 + 나머지 상태 버튼만 선다.
SM.api.oppPanel = (o, extra) => o ? oppDetail(o, extra) : "";

/* 기회 하나를 이 PC 의 개발 도구로 연다. 여기서 하는 일은 누른 것을 서버에 넘기고
   무엇으로 열렸는지 말하는 것뿐이다 — 어느 도구·어느 터미널·어느 폴더인지는 전부
   서버(run_tool)가 정한다. 화면이 다시 판정하면 같은 규칙이 두 벌이 된다.
   터미널 이름도 payload 에서 읽는다(정본은 doctor.TERMINALS).
   ids 는 묶인 줄의 id 전부(SM.host.oppBtn 이 싣는다) — 서버가 그 사이트 것만 골라
   '진행 중'으로 둔다. 본문 꼴 {project, id, ids} 은 run_tool 이 정본이다. */
async function runTool(id, ids) {
  const [ok, r] = await post("/api/setup/run-tool",
                             {project: $("proj").value, id, ids: ids || [id]});
  if (!ok || !r.ok) return toast((r && r.error) ||
    "열지 못했습니다. [설정]에서 도구를 확인한 뒤 다시 눌러 주세요.", "bad");
  const t = ((window.SETUP || {}).terminals || []).find(x => x.id === r.terminal);
  toast((t ? t.label : r.terminal) + "에서 열었습니다." +
        (r.fallback ? " Orca 워크트리가 아니어서 이쪽으로 열었습니다." : ""), "ok");
  load();
}

/* 할 일(play) 하나를 PR 로 — 누른 것을 서버(play_pr.apply)에 넘기고, 도는 동안 상태를
   묻는다. 어느 폴더·어느 도구·명령줄은 전부 서버가 정한다(runTool 과 같다). 요청은 바로
   돌아오고 일은 이 PC 의 백그라운드에서 돈다 — 브라우저 창을 닫아도 멈추지 않는다.
   window.PLAY_PR = {ready, why, fix, jobs: {play id: 일}} — 모양은 play_pr.status 가 정본. */
const PR_POLL = {};
async function prSync() {
  if (window.SM_HOSTED || READONLY || !$("proj")) return;
  const project = $("proj").value;
  if (!project) return;
  let r;
  try {
    r = await (await fetch("/api/plays/apply?project=" + encodeURIComponent(project))).json();
  } catch (e) { return; }
  if (!r || !r.ok || $("proj").value !== project) return;
  window.PLAY_PR = {ready: r.ready, why: r.why, fix: r.fix, jobs: r.jobs || {}};
  Object.values(window.PLAY_PR.jobs).forEach(j => prWatch(project, j));
  SM.repaint();
}
/* [PR 만들기]를 누르면 바로 돌리지 않고 창을 먼저 연다 — 서버(play_pr.preflight)가 저장소를
   읽어 어느 폴더·어느 브랜치에서 갈라지는지, 막는 것(커밋 안 된 변경)이 있는지 말하고, 사람이
   [시작]을 눌러야 prApply 가 돈다. 막힌 채로 돌리면 12초 뒤 '지난번 실패'만 남는다. */
function prDialog() {
  let d = $("prbox");
  if (!d) {
    d = document.createElement("dialog");
    d.id = "prbox"; d.className = "prbox";
    d.setAttribute("aria-labelledby", "prbox-h");
    d.addEventListener("click", e => { if (e.target === d) d.close(); });   // 바깥을 누르면 닫힌다
    document.body.appendChild(d);
  }
  return d;
}
async function prAsk(id) {
  const project = $("proj").value;
  const d = prDialog();
  const paint = (body, acts) => {
    d.innerHTML = `<h3 id="prbox-h">PR 만들기 — 시작하기 전에</h3>${body}` +
      `<div class="prbox-acts">${acts}</div>`;
  };
  const close = `<button class="go sm ghost" onclick="$('prbox').close()">닫기</button>`;
  paint(`<p class="sub" role="status">저장소를 확인하는 중…</p>`, close);
  if (!d.open) d.showModal();
  let r = null;
  try {
    r = await (await fetch("/api/plays/apply?check=1&project=" + encodeURIComponent(project) +
                           "&id=" + encodeURIComponent(id))).json();
  } catch (e) { /* 아래서 안내 */ }
  if (!d.open) return;                          // 확인하는 사이 닫았다
  if (!r || !r.ok)
    return paint(`<p class="prbox-warn">저장소를 확인하지 못했습니다 — ${esc((r && r.error) || "대시보드에 닿지 않습니다")}</p>`,
                 `<button class="go sm" onclick="prAsk(${+id})">다시 확인</button>${close}`);
  const row = (k, v) => v ? `<dt>${k}</dt><dd class="mono">${esc(v)}</dd>` : "";
  const facts = `<dl class="prbox-facts">${row("페이지", r.page)}${row("저장소 폴더", r.cwd)}` +
    `${row("지금 브랜치", r.branch)}${row("새 브랜치", r.new_branch)}</dl>`;
  if (!r.ready)
    return paint(`<p class="prbox-warn">${esc(r.why)}</p>`,
      (r.fix === "settings"
        ? `<button class="go sm" onclick="$('prbox').close();SM.touched=true;SM.show('settings')">설정에서 폴더 정하기</button>`
        : `<button class="go sm" onclick="prAsk(${+id})">다시 확인</button>`) + close);
  if (r.blocked)
    return paint(facts +
      `<p class="prbox-warn">저장소에 커밋 안 된 변경이 ${+r.dirty_n}개 있어 시작할 수 없습니다. ` +
      `수정안이 이 변경과 섞이지 않게 하려는 것입니다. 커밋하거나 치운 뒤 [다시 확인]을 누르세요.</p>` +
      `<ul class="prbox-files mono">${(r.dirty || []).map(f => `<li>${esc(f)}</li>`).join("")}` +
      `${r.dirty_n > (r.dirty || []).length ? `<li>…외 ${r.dirty_n - r.dirty.length}개</li>` : ""}</ul>`,
      `<button class="go sm" onclick="prAsk(${+id})">다시 확인</button>${close}`);
  paint(facts +
    (r.off_base
      ? `<p class="prbox-warn">지금 브랜치가 기본 브랜치(${esc(r.base)})가 아닙니다. 새 브랜치는 지금 브랜치에서 갈라지므로 ` +
        `그 브랜치의 커밋이 PR 에 같이 실립니다. 기본 브랜치에서 시작하려면 먼저 옮겨 두세요.</p>` : "") +
    (r.git_ok ? "" : `<p class="sub">git 상태를 읽지 못했습니다 — 시작하면 돌리는 쪽이 다시 확인합니다.</p>`) +
    `<p>이 PC 가 위 폴더에서 새 브랜치를 만들어 수정안을 적용하고, 커밋·push 한 뒤 PR 을 엽니다. ` +
    `몇 분 걸리고 창을 닫아도 계속 돕니다. 머지는 직접 합니다.</p>`,
    `<button class="go sm" onclick="$('prbox').close();prApply(${+id})">시작</button>` +
    `<button class="go sm ghost" onclick="$('prbox').close()">취소</button>`);
}
async function prApply(id, btn) {
  if (btn) btn.disabled = true;
  const project = $("proj").value;
  const [ok, r] = await post("/api/plays/apply", {project, id});
  if (!ok || !r.ok) {
    if (btn) btn.disabled = false;
    if (r && r.job) prWatch(project, r.job);
    return toast((r && r.error) || "PR 만들기를 시작하지 못했습니다. 잠시 뒤 다시 눌러 주세요.", "bad");
  }
  toast("PR 만들기를 시작했습니다. 저장소에서 고치고 PR 을 여는 데 몇 분 걸립니다.", "ok");
  prWatch(project, r.job);
}
function prWatch(project, j) {
  if (!j || j.id == null) return;
  const s = window.PLAY_PR || (window.PLAY_PR = {ready: true, why: "", fix: "", jobs: {}});
  s.jobs[j.id] = j;
  SM.repaint();
  const key = project + "#" + j.id;
  if (j.status !== "running" || PR_POLL[key]) return;
  PR_POLL[key] = setInterval(async () => {
    let r;
    try {
      r = await (await fetch("/api/plays/apply?project=" + encodeURIComponent(project) +
                             "&id=" + encodeURIComponent(j.id))).json();
    } catch (e) { return; }                   // 한 번 못 물었다고 멈추지 않는다
    const now = r && r.job;
    if (!now || now.status === "running") return;
    clearInterval(PR_POLL[key]); delete PR_POLL[key];
    if ($("proj").value !== project) return;  // 그새 다른 사이트로 갔다 — 돌아오면 playSync 가 다시 받는다
    s.jobs[j.id] = now;
    if (now.status === "done") toast("PR 을 열었습니다. 서랍의 [PR 보기]로 갑니다.", "ok");
    else toast("PR 을 못 만들었습니다 — " + (now.error || "이유를 못 받았습니다."), "bad");
    load();
  }, 4000);
}
document.addEventListener("sm:site", () => {
  window.PLAY_PR = null;
  if (SM.host.playSync) SM.host.playSync();
});

// 글자로만 "[개요]에서" 라고 말하던 자리를 실제로 눌리게 한다. 화면 이름은
// SM.views(=view-def)의 정본에서 읽는다 — 라벨을 두 벌로 만들지 않는다.
// 그 배포에 없는 화면(호스팅의 [설정], 박제본의 [진행 안내])이면 글자로 물러선다.
SM.api.go = (id, label) => {
  // 이름은 매니페스트(window.__VIEWS__)에서 읽는다 — SM.views 는 이 배포에서 뺀
  // 화면이 이미 빠져 있어서, 거기서 찾으면 없는 화면이 영문 id 로 찍힌다.
  const def = (window.__VIEWS__ || []).find(v => v.id === id);
  const t = label || `[${(def && def.title) || id}]`;
  return SM.at(id) >= 0
    ? `<button type="button" class="gov" onclick="SM.touched=true;SM.show('${id}')">${esc(t)}</button>`
    : esc(t);          // 이 배포에 없는 화면(호스팅의 설정, 박제본의 안내)은 글자로 물러선다
};

