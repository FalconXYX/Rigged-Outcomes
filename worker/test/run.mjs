// npm test: the unit checks, then the smoke test on a throwaway local state, start to finish. It applies the
// migrations to a fresh --persist-to directory, starts `wrangler dev` on 127.0.0.1:8788 (WRANGLER_TEST_PORT picks
// another, e.g. while `npm run dev` holds 8788; it refuses if anything already listens there) with every setting
// passed by --var and wake-ups sent to a stand-in for the step function on the next port but one (it records
// each one, signature and all), and runs test/smoke.mjs, then the job's own Python client against it
// (test/client.py; SKIP_CLIENT=1 leaves it out). Then it back-dates every saved part two days, every watched
// story 40 days (one 400), stores a search 3 days old, adds an approved add with no run, and checks the cron's prune and
// watchdog (smoke.mjs --prune); then it restarts the Worker twice: with Turnstile's always-failing test secret,
// and with production settings (DEV off, strict Turnstile). It stops every process it started. Local only:
// nothing here talks to a Cloudflare or Vercel account. WRANGLER_TEST_DIR picks where the throwaway state and
// logs go (default: the system temp dir); KEEP_STATE=1 keeps them afterwards.

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
const STEP_PORT = PORT + 2;
const STEP = `http://127.0.0.1:${STEP_PORT}`;
const WRANGLER = join(ROOT, "node_modules", "wrangler", "bin", "wrangler.js");
const FAILING_SECRET = "2x0000000000000000000000000000000AA";
const env = { ...process.env, WRANGLER_SEND_METRICS: "false", CI: "1" };

/** Every setting the smoke test needs, so it never depends on what a local .dev.vars holds. */
const VARS = {
  TURNSTILE_SECRET: "1x0000000000000000000000000000000AA",
  OWNER_TOKEN: "dev-owner-token",
  JOB_TOKEN: "dev-job-token",
  VISITOR_SALT: "dev-visitor-salt",
  DEV: "on",
  TURNSTILE_STRICT: "off",
  ALLOWED_ORIGINS: "http://localhost:5190",
  STEP_URL: `${STEP}/api/step`,
  WAKE_SECRET: "test-wake-secret",
};
const vars = (extra = {}) => Object.entries({ ...VARS, ...extra }).flatMap(([k, v]) => ["--var", `${k}:${v}`]);

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

const listening = (port) =>
  new Promise((resolve) => {
    const s = createConnection({ host: "127.0.0.1", port }, () => {
      s.destroy();
      resolve(true);
    });
    s.on("error", () => resolve(false));
  });

/** Start wrangler dev in its own process group (so stopping it stops workerd too); resolves once /api/health answers. */
async function startWorker(extra = {}) {
  const args = [WRANGLER, "dev", "--port", String(PORT), "--ip", "127.0.0.1", "--inspector-port", String(PORT + 1), "--persist-to", state, ...vars(extra)];
  const child = spawn(process.execPath, args, { cwd: ROOT, env, detached: true, stdio: ["ignore", "pipe", "pipe"] });
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

/**
 * A stand-in for the step function (Vercel's /api/step): records each wake-up (its lane, x-ro-wake header,
 * User-Agent and body) and answers 200 at once; GET /__wakes lists them.
 */
function fakeStep() {
  const wakes = [];
  const server = createServer((req, res) => {
    const url = new URL(req.url, "http://x");
    if (url.pathname === "/__wakes") return res.writeHead(200, { "content-type": "application/json" }).end(JSON.stringify(wakes));
    let body = "";
    req.on("data", (c) => (body += c));
    req.on("end", () => {
      const header = req.headers["x-ro-wake"] ?? "";
      wakes.push({ path: url.pathname, method: req.method, lane: header.split(",")[2] ?? null, header, agent: req.headers["user-agent"], body, at: Date.now() });
      res.writeHead(200, { "content-type": "application/json" }).end(JSON.stringify({ lane: header.split(",")[2], jobs: 0 }));
    });
  });
  return new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(STEP_PORT, "127.0.0.1", () => resolve(server));
  });
}

const sql = (command) => exec(process.execPath, [WRANGLER, "d1", "execute", "rigged-outcomes", "--local", "--persist-to", state, "--command", command]);

let code = 1;
let worker;
let step;
try {
  code = await exec(process.execPath, [join(ROOT, "test", "unit.mjs")], { stdio: ["ignore", "inherit", "inherit"] });
  if (code !== 0) throw new Error("the unit checks failed");
  for (const port of [PORT, PORT + 1, STEP_PORT]) {
    if (await listening(port)) {
      throw new Error(`something already listens on port ${port}; stop it first, or pick another with WRANGLER_TEST_PORT (this test never stops what it didn't start)`);
    }
  }
  if (!existsSync(join(ROOT, ".dev.vars"))) copyFileSync(join(ROOT, ".dev.vars.example"), join(ROOT, ".dev.vars"));
  if ((await exec(process.execPath, [WRANGLER, "d1", "migrations", "apply", "rigged-outcomes", "--local", "--persist-to", state])) !== 0) {
    throw new Error(`the migrations didn't apply (log: ${logPath})`);
  }
  step = await fakeStep();
  worker = await startWorker();
  code = await exec(process.execPath, [join(ROOT, "test", "smoke.mjs"), BASE, `--step=${STEP}`], { stdio: ["ignore", "inherit", "inherit"] });
  if (code === 0 && !process.env.SKIP_CLIENT) {
    code = await exec("python3", [join(ROOT, "test", "client.py"), BASE], { stdio: ["ignore", "inherit", "inherit"] });
  }
  await stopWorker(worker);
  if (code === 0) {
    code = await sql(
      "UPDATE job_blob_parts SET created_at = created_at - 2 * 86400; " +
        "UPDATE watches SET created_at = created_at - (CASE story WHEN 'prune watch year' THEN 400 ELSE 40 END) * 86400; " +
        "INSERT INTO news_cache (key, day, fetched_at, body) VALUES ('smoke q old|7d', '2026-11-01', 0, '[]'); " +
        "INSERT INTO requests (condition_id, slug, question, url, winner, closed, day, status, requested_at, decided_at) VALUES " +
        "('0x" + "e".repeat(64) + "', 'prune-left', 'Will it be mended?', 'https://polymarket.com/event/prune-left', 'Yes', '2026-11-12T10:00:00Z', '2026-11-12', 'approved', 1, 1)",
    );
    if (code !== 0) throw new Error(`the state wasn't back-dated (log: ${logPath})`);
    worker = await startWorker();
    code = await exec(process.execPath, [join(ROOT, "test", "smoke.mjs"), BASE, "--prune", `--step=${STEP}`], { stdio: ["ignore", "inherit", "inherit"] });
    await stopWorker(worker);
  }
  const phases = [
    [{ TURNSTILE_SECRET: FAILING_SECRET }, "--bot-fail"],
    [{ DEV: "off", TURNSTILE_STRICT: "on", TURNSTILE_HOSTNAMES: "parthjain.ca" }, "--production"],
  ];
  for (const [extra, flag] of phases) {
    if (code !== 0) break;
    worker = await startWorker(extra);
    code = await exec(process.execPath, [join(ROOT, "test", "smoke.mjs"), BASE, flag, `--step=${STEP}`], { stdio: ["ignore", "inherit", "inherit"] });
    await stopWorker(worker);
  }
} catch (e) {
  console.log(`FAIL ${e.message}`);
  code = 1;
} finally {
  await stopWorker(worker);
  step?.close();
  log.end();
  if (code !== 0 && existsSync(logPath)) console.log(`Worker log: ${logPath}\n${readFileSync(logPath, "utf8").split("\n").slice(-25).join("\n")}`);
  if (!process.env.KEEP_STATE && code === 0) rmSync(state, { recursive: true, force: true });
}
process.exit(code);
