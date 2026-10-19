/** Private process pipe only. No renderer, HTTP proxy, or model execution. */
import { app, safeStorage } from "electron";
import { createInterface } from "node:readline";
import { spawn } from "node:child_process";
import { createChatGPT, ChatGPTError } from "./vendor/local/index.js";
import { createElectronCredentialEncryption } from "./vendor/credential-encryption.js";

async function main() {
const storageDir = process.env.CAD2BIM_CHATGPT_STORAGE_DIR;
if (!storageDir) throw new Error("The parent must provide its application storage directory.");
app.setName("CAD2BIM Component Demo");
app.setPath("userData", storageDir);
app.dock?.hide();
await app.whenReady();

const client = createChatGPT({
  appName: "CAD2BIM Component Demo", appId: "cad2bim.component-demo",
  storageDir, redirectPort: 0, sendHostId: true,
  credentialEncryption: createElectronCredentialEncryption(safeStorage, () => app.isReady()),
  openBrowser: (url) => new Promise<void>((resolve, reject) => {
    // URL (including any future id_token_hint) is private stdin, never argv or a log.
    const browser = spawn("/usr/bin/osascript", ["-"], { stdio: ["pipe", "ignore", "ignore"] });
    browser.once("error", () => reject(new Error("Could not open the Chrome authorization page.")));
    browser.once("exit", (code) => code === 0 ? resolve() : reject(new Error("Chrome could not open the authorization page.")));
    browser.stdin.end(`tell application "Google Chrome" to open location ${JSON.stringify(url)}\n`);
  }),
});

const input = createInterface({ input: process.stdin, terminal: false });
for await (const line of input) {
  let id: unknown;
  try {
    const request = JSON.parse(line);
    id = request.id;
    let result: unknown;
    switch (request.command) {
      case "status": result = await client.getSession(); break;
      case "sign-in": result = await client.signIn({ label: "CAD2BIM Component Demo" }); break;
      case "sign-in-new": result = await client.signIn({ newProfile: true, label: "CAD2BIM Component Demo — another account" }); break;
      case "access-token": result = await client.getAccessToken(); break;
      case "close": app.quit(); break;
      default: throw new Error("Unknown private authentication command.");
    }
    if (request.command !== "close") process.stdout.write(JSON.stringify({ id, result }) + "\n");
  } catch (error) {
    // The official error type has safe diagnostics. Never echo requests/unknown errors.
    const detail = error instanceof ChatGPTError ? error.toJSON() : {
      code: "auth_helper_failed", message: "The local authentication helper failed.", retryable: false,
    };
    process.stdout.write(JSON.stringify({ id, error: detail }) + "\n");
  }
}
app.quit();
}
void main().catch(() => {
  process.stdout.write(JSON.stringify({ error: {
    code: "auth_helper_start_failed", message: "The authentication helper could not start.", retryable: false,
  } }) + "\n");
  app.exit(1);
});
