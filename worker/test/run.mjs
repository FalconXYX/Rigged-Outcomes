// npm test: the smoke test on a throwaway local state, start to finish. It applies the migrations to a fresh
// --persist-to directory, starts `wrangler dev` on 127.0.0.1:8788 (WRANGLER_TEST_PORT picks another, e.g. while
// `npm run dev` holds 8788; it refuses if anything already listens there) and runs test/smoke.mjs; then it restarts the Worker three times: with Turnstile's always-failing test
// secret, with production settings (DEV off, strict Turnstile), and with a dispatch token aimed at a stand-in
// for GitHub on a local port; and it stops every process it started. Local only: nothing here talks to a
// Cloudflare or GitHub account. WRANGLER_TEST_DIR picks where the throwaway state and logs go (default: the
// system temp dir); KEEP_STATE=1 keeps them afterwards.

import { spawn } from "node:child_process";
import { copyFileSync, createWriteStream, existsSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import { createServer } from "node:http";
import { createConnection } from "node:net";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");
const PORT = Number(process.env.WRANGLER_TEST_PORT || 8788);
const BASE = `http://127.0.0.1:${PORT}`;
const WRANGLER = join(ROOT, "node_modules", "wrangler", "bin", "wrangler.js");
const FAILING_SECRET = "2x0000000000000000000000000000000AA";
const env = { ...process.env, WRANGLER_SEND_METRICS: "false", CI: "1" };

const state = mkdtempSync(join(process.env.WRANGLER_TEST_DIR || tmpdir(), "ro-api-smoke-"));
const logPath = join(state, "worker.log");
const log = createWriteStream(logPath, { flags: "a" });

function exec(cmd, args, opts = {}) {
  return new Promise((resolve) => {
    const p = spawn(cmd, args, { cwd: ROOT, env, ...opts });
    p.stdout?.pipe(log, { end: false });
    p.stderr?.pipe(log, { end: false });
    p.on("exit", (code) => resolve(code ?? 1));
  });
}

const listening = () =>
  new Promise((resolve) => {
    const s = createConnection({ host: "127.0.0.1", port: PORT }, () => {
      s.destroy();
      resolve(true);
    });
    s.on("error", () => resolve(false));
  });

/** Start wrangler dev in its own process group (so stopping it stops workerd too); resolves once /api/health answers. */
async function startWorker(extra = []) {
  const child = spawn(process.execPath, [WRANGLER, "dev", "--port", String(PORT), "--ip", "127.0.0.1", "--persist-to", state, ...extra], {
    cwd: ROOT,
    env,
    detached: true,
    stdio: ["ignore", "pipe", "pipe"],
  });
  child.stdout.pipe(log, { end: false });
  child.stderr.pipe(log, { end: false });
  for (let i = 0; i < 120; i++) {
    if (child.exitCode !== null) break;
    const ok = await fetch(`${BASE}/api/health`).then((r) => r.ok, () => false);
    if (ok) return child;
    await new Promise((r) => setTimeout(r, 500));
  }
  await stopWorker(child);
  throw new Error(`the Worker didn't start (log: ${logPath})`);
}

async function stopWorker(child) {
  if (!child || child.exitCode !== null) return;
  const gone = new Promise((r) => child.once("exit", r));
  try {
    process.kill(-child.pid, "SIGTERM");
  } catch {
    child.kill("SIGTERM");
  }
  await Promise.race([gone, new Promise((r) => setTimeout(r, 5000))]);
  if (child.exitCode === null) {
    try {
      process.kill(-child.pid, "SIGKILL");
    } catch {
      /* already gone */
    }
  }
}

/** A stand-in for api.github.com: records the last dispatch and answers with the status it was told to. */
function fakeGitHub() {
  let answer = 204;
  let last = null;
  let count = 0;
  const server = createServer((req, res) => {
    const url = new URL(req.url, "http://x");
    const reply = (data) => res.writeHead(200, { "content-type": "application/json" }).end(JSON.stringify(data));
    if (url.pathname === "/__answer") return reply({ answer: (answer = Number(url.searchParams.get("status"))) });
    if (url.pathname === "/__last") return reply(last);
    if (url.pathname === "/__count") return reply({ count });
    let body = "";
    req.on("data", (c) => (body += c));
    req.on("end", () => {
      count++;
      last = { method: req.method, path: url.pathname, headers: req.headers, body: JSON.parse(body || "null") };
      if (answer === 204) res.writeHead(204).end();
      else res.writeHead(answer, { "content-type": "application/json" }).end(JSON.stringify({ message: "Stand-in refusal" }));
    });
  });
  return new Promise((resolve) => server.listen(0, "127.0.0.1", () => resolve(server)));
}

let code = 1;
let worker;
let github;
try {
  if (await listening()) {
    throw new Error(`something already listens on port ${PORT}; stop it first, or pick another with WRANGLER_TEST_PORT (this test never stops what it didn't start)`);
  }
  if (!existsSync(join(ROOT, ".dev.vars"))) copyFileSync(join(ROOT, ".dev.vars.example"), join(ROOT, ".dev.vars"));
  if ((await exec(process.execPath, [WRANGLER, "d1", "migrations", "apply", "rigged-outcomes", "--local", "--persist-to", state])) !== 0) {
    throw new Error(`the migrations didn't apply (log: ${logPath})`);
  }
  worker = await startWorker();
  code = await exec(process.execPath, [join(ROOT, "test", "smoke.mjs"), BASE], { stdio: ["ignore", "inherit", "inherit"] });
  await stopWorker(worker);
  github = await fakeGitHub();
  const fake = `http://127.0.0.1:${github.address().port}`;
  const phases = [
    [["--var", `TURNSTILE_SECRET:${FAILING_SECRET}`], "--bot-fail"],
    [["--var", "DEV:off", "--var", "TURNSTILE_STRICT:on", "--var", "TURNSTILE_HOSTNAMES:parthjain.ca"], "--production"],
    [["--var", "GITHUB_DISPATCH_TOKEN:fake-pat", "--var", `GITHUB_API:${fake}`], `--dispatch=${fake}`],
  ];
  for (const [vars, flag] of phases) {
    if (code !== 0) break;
    worker = await startWorker(vars);
    code = await exec(process.execPath, [join(ROOT, "test", "smoke.mjs"), BASE, flag], { stdio: ["ignore", "inherit", "inherit"] });
    await stopWorker(worker);
  }
} catch (e) {
  console.log(`FAIL ${e.message}`);
  code = 1;
} finally {
  await stopWorker(worker);
  github?.close();
  log.end();
  if (code !== 0) console.log(`Worker log: ${logPath}\n${readFileSync(logPath, "utf8").split("\n").slice(-25).join("\n")}`);
  if (!process.env.KEEP_STATE && code === 0) rmSync(state, { recursive: true, force: true });
}
process.exit(code);
