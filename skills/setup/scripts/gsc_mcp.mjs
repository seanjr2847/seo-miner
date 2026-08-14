// 번들 gsc MCP 서버 런처 — OS 상관없이 뜨게 한다.
//
// .mcp.json이 `cmd /c npx`를 직접 부르면 윈도우에서만 돈다 (맥·리눅스에 cmd가 없다).
// 반대로 `npx`를 그냥 부르면 윈도우에서 npx.cmd를 못 찾는다. 양쪽 다 되게 하려면
// 플랫폼을 보고 고르는 수밖에 없어서 이 파일이 있다. node는 어느 OS에서나 실행
// 파일이라 .mcp.json이 바로 부를 수 있다 (이 서버는 어차피 node가 필요하다).
//
// 열쇠 경로도 여기서 정한다 — .mcp.json의 ${USERPROFILE}은 윈도우 전용인 데다
// CAPTURE_HOME 설정을 무시했다.
import { spawn } from "node:child_process";
import { homedir } from "node:os";
import path from "node:path";

const home = process.env.CAPTURE_HOME || path.join(homedir(), ".capture");
const env = { ...process.env };
env.GOOGLE_APPLICATION_CREDENTIALS ??= path.join(home, "gsc_service_account.json");

const win = process.platform === "win32";
// 윈도우의 npx는 .cmd라 shell 없이는 spawn이 거부한다. 인자는 전부 고정값이라
// 사용자 입력이 셸에 섞이지 않는다.
const child = spawn(win ? "npx.cmd" : "npx", ["-y", "mcp-server-gsc"],
                    { stdio: "inherit", env, shell: win });

child.on("error", (e) => {
  console.error(`[gsc] npx를 실행하지 못했습니다 (${e.message}) — node가 필요합니다: ` +
                `nodejs.org. 설치 없이도 CSV 내보내기와 collect_gsc.py는 그대로 됩니다.`);
  process.exit(1);
});
child.on("exit", (code) => process.exit(code ?? 1));
