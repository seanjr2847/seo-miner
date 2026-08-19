// 번들 gsc MCP 서버 런처 — OS 상관없이 뜨게 한다.
//
// 서버는 AminForou/mcp-gsc (PyPI `mcp-search-console`) — 파이썬이다. 그런데
// .mcp.json이 "python"을 직접 부르면 윈도우에서 마이크로소프트 스토어 스텁을
// 잡아 exit 49로 죽는다(이 개발기가 실제로 그 상태다). node는 어느 OS에서나
// 진짜 실행 파일이라 .mcp.json의 진입점으로 안전해서, node가 파이썬을 찾아
// 넘겨주는 이 한 겹이 있다.
//
// 열쇠 경로도 여기서 정한다 — .mcp.json의 ${USERPROFILE}은 윈도우 전용인 데다
// CAPTURE_HOME 설정을 무시했다. collect_gsc.py와 같은 열쇠 1개를 쓴다.
import { spawn, spawnSync } from "node:child_process";
import { existsSync, readFileSync, readdirSync } from "node:fs";
import { homedir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const home = process.env.CAPTURE_HOME || path.join(homedir(), ".capture");
const env = { ...process.env };
// ~/.capture/env의 KEY=VALUE를 반영 — db.load_env()와 같은 규칙: 프로세스 환경변수가
// 우선(setdefault), 빈 줄·# 주석은 건너뜀. home은 이미 위에서 확정됐으니 안 바뀐다.
const envFile = path.join(home, "env");
if (existsSync(envFile)) {
  for (const line of readFileSync(envFile, "utf-8").split(/\r?\n/)) {
    const i = line.indexOf("=");
    if (i < 0) continue;
    const k = line.slice(0, i).trim();
    if (!k || k.startsWith("#")) continue;
    env[k] ??= line.slice(i + 1).trim();
  }
}

// 인증: **OAuth가 기본이다** (db.gsc_auth()가 정본 — 여기는 그 규칙의 JS 사본이라
// 순서를 바꾸려면 양쪽을 같이 고쳐야 한다). OAuth 클라이언트 파일이 있으면 그걸 쓰고,
// 없고 서비스 계정 키만 있으면 그쪽으로 넘어간다.
// **있는 파일만 가리킨다.** 서버는 이 두 변수가 가리키는 파일이 없으면 인증을
// 시도하기도 전에 fail-fast 한다 — 서비스 계정 경로를 무조건 걸어두면 OAuth 로
// 붙은 사람도 "키 파일이 없다"에서 막힌다 (실측으로 잡은 버그).
// 번들 OAuth 클라이언트는 **서비스 계정보다 뒤다**. 설치만 하면 항상 존재하므로
// 단순히 "OAuth 파일이 있나"로 앞세우면, 서비스 계정으로 무인 수집을 걸어 둔 사람이
// 매번 브라우저 로그인으로 끌려간다. 사용자가 직접 놓은 것이 항상 이긴다.
// (db.gsc_auth()가 정본 — 여기는 그 규칙의 JS 사본이다.)
const oauthFile = path.join(home, "gsc_oauth_client.json");
const keyFile = path.join(home, "gsc_service_account.json");
// import.meta.dirname 은 Node 20.11+ 전용이다 — 이 런처는 버전·OS 의존을 없애려고
//만든 것이라 fileURLToPath 로 간다.
const here = path.dirname(fileURLToPath(import.meta.url));
const bundledOauth = path.join(here, "..", "oauth_client.json");
if (!env.GSC_OAUTH_CLIENT_SECRETS_FILE && existsSync(oauthFile))
  env.GSC_OAUTH_CLIENT_SECRETS_FILE = oauthFile;
if (!env.GSC_CREDENTIALS_PATH && existsSync(keyFile))
  env.GSC_CREDENTIALS_PATH = keyFile;
if (!env.GSC_OAUTH_CLIENT_SECRETS_FILE && !env.GSC_CREDENTIALS_PATH
    && existsSync(bundledOauth))
  env.GSC_OAUTH_CLIENT_SECRETS_FILE = bundledOauth;
// OAuth 파일이 없는데 OAuth를 켜두면 서버가 브라우저 로그인을 띄우려다 실패한다.
if (!env.GSC_OAUTH_CLIENT_SECRETS_FILE) env.GSC_SKIP_OAUTH ??= "true";

// 데이터 확정 상태는 **명시한다** — 서버 기본값(gsc_server.py의 "all")에 묻어가면
// 업스트림이 기본을 바꾸는 날 우리 답이 조용히 달라진다.
// "all"을 고른 것이지 물려받은 게 아니다: 이 서버는 **즉석 조회 창구**라
// "어제 클릭 몇이야"에 답해야 하고, 그건 미확정 데이터를 봐야 나온다(GSC 대시보드와
// 같은 값). collect_gsc.py 는 반대로 "final" + 3일 버퍼다 — 그쪽은 스냅샷끼리
// 비교하는 게 목적이라 나중에 값이 바뀌는 날짜가 섞이면 Δ가 거짓이 된다.
// **그래서 두 경로의 숫자는 원래 다르다** (capture/SKILL.md 철칙 1의 예외 조항).
env.GSC_DATA_STATE ??= "all";

// 셸을 쓰지 않는다 — 윈도우에서 shell:true면 node가 인자를 따옴표 없이 이어붙여
// `-c "import sys; print(...)"` 의 세미콜론이 명령 구분자로 쪼개진다. 예전 런처가
// shell을 켰던 건 npx.cmd(배치 파일) 때문이고, 파이썬은 어느 OS에서나 실행 파일이다.
const run = (cmd, args) => spawnSync(cmd, args, { encoding: "utf-8" });

/** 후보가 진짜 파이썬인가. 스토어 스텁은 exit 49 + 빈 stdout으로 걸러진다. */
function works(cmd, pre = []) {
  const r = run(cmd, [...pre, "-c", "import sys; print(sys.executable)"]);
  return r.status === 0 && (r.stdout || "").trim().length > 0;
}

function findPython() {
  if (env.CAPTURE_PYTHON && works(env.CAPTURE_PYTHON)) return [env.CAPTURE_PYTHON, []];
  for (const [cmd, pre] of [["python3", []], ["py", ["-3"]], ["python", []]])
    if (works(cmd, pre)) return [cmd, pre];
  // PATH에 없거나 스텁뿐일 때 — 윈도우 표준 설치 위치를 훑는다.
  const dirs = [path.join(process.env.LOCALAPPDATA || "", "Programs", "Python"),
                "C:\\", "C:\\Program Files"];
  for (const d of dirs) {
    let names = [];
    try { names = readdirSync(d); } catch { continue; }
    for (const n of names.filter(n => /^Python3\d*$/i.test(n)).sort().reverse()) {
      const exe = path.join(d, n, "python.exe");
      if (existsSync(exe) && works(exe)) return [exe, []];
    }
  }
  return [null, []];
}

const [py, pre] = findPython();
if (!py) {
  console.error("[gsc] 파이썬을 찾지 못했습니다 — python.org에서 3.11 이상을 설치하거나 " +
                "~/.capture/env 에 CAPTURE_PYTHON=<python 경로> 를 넣어 주세요.");
  process.exit(1);
}

// 서버 패키지가 없으면 깔아준다 — npx -y 가 해주던 몫을 pip으로 옮긴 것뿐이다.
const installed = run(py, [...pre, "-c", "import gsc_server"]).status === 0;
if (!installed) {
  // stdout은 MCP 프로토콜 통로다 — pip 로그가 섞이면 핸드셰이크가 깨진다.
  console.error("[gsc] 서버 부품을 설치합니다 (mcp-search-console, 처음 한 번)…");
  const r = spawnSync(py, [...pre, "-m", "pip", "install", "--quiet", "mcp-search-console"],
                      { stdio: ["ignore", "ignore", "inherit"] });
  if (r.status !== 0) {
    console.error("[gsc] 설치에 실패했습니다 — 수동으로: pip install mcp-search-console");
    process.exit(1);
  }
}

const child = spawn(py, [...pre, "-c", "import gsc_server; gsc_server.main()"],
                    { stdio: "inherit", env });

child.on("error", (e) => {
  console.error(`[gsc] 서버를 실행하지 못했습니다 (${e.message}). ` +
                `설치 없이도 collect_gsc.py 수집은 그대로 됩니다.`);
  process.exit(1);
});
child.on("exit", (code) => process.exit(code ?? 1));
