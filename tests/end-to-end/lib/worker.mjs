// The stack's API: `wrangler dev --test-scheduled` for the snapshot's worker/ on its own port, with its own D1 state
// folder (--persist-to, the migrations applied there first) and every setting passed with --var, so no .dev.vars
// file is read (the snapshot has none): the dev tokens, Turnstile's test secret, DEV=on, the static site's origin
// allowed, and the wake-ups sent to the stack's own step server (STEP_URL, WAKE_SECRET). Its Cron Trigger is
// fired by lib/cron.mjs through wrangler's local scheduled path. Local only: wrangler dev never talks to the
// Cloudflare account, and with DEV=on the Worker wakes only a 127.0.0.1 step URL.

import { join } from "node:path";
import { cleanEnv, run, startProcess, until } from "./proc.mjs";

export const JOB_TOKEN = "dev-job-token";
export const TURNSTILE = {
  pass: { sitekey: "1x00000000000000000000AA", secret: "1x0000000000000000000000000000000AA" },
  fail: { sitekey: "2x00000000000000000000AB", secret: "2x0000000000000000000000000000000AA" },
};

const wrangler = (snap) => join(snap, "worker", "node_modules", "wrangler", "bin", "wrangler.js");
const env = () => cleanEnv({ CI: "1", WRANGLER_SEND_METRICS: "false", NO_UPDATE_NOTIFIER: "1" });

/** Create the tables in the state folder's local D1 (wrangler d1 migrations apply --local). */
export async function migrate(snap, persist, logPath, procs) {
  const res = await run(process.execPath, [wrangler(snap), "d1", "migrations", "apply", "rigged-outcomes", "--local", "--persist-to", persist], {
    cwd: join(snap, "worker"),
    env: env(),
    logPath,
    procs,
  });
  if (res.code !== 0) throw new Error(`the D1 migrations did not apply (log: ${logPath})`);
}

/** Run SQL on the stack's local D1 while no Worker holds it (before startWorker, or between stopApi and startApi). */
export async function execute(snap, persist, sql, logPath, procs) {
  const res = await run(process.execPath, [wrangler(snap), "d1", "execute", "rigged-outcomes", "--local", "--persist-to", persist, "--command", sql], {
    cwd: join(snap, "worker"),
    env: env(),
    logPath,
    procs,
  });
  if (res.code !== 0) throw new Error(`the SQL did not run on the local D1 (log: ${logPath})`);
}

/**
 * Start the Worker; resolves once /api/health answers. vars: what the stack sets (see startStack), each one
 * replacing wrangler.toml's [vars] value. procs: the stack's process set (tests/end-to-end/lib/proc.mjs).
 */
export async function startWorker({ snap, persist, port, inspectorPort, vars, logPath, procs }) {
  const args = [wrangler(snap), "dev", "--port", String(port), "--ip", "127.0.0.1", "--inspector-port", String(inspectorPort), "--persist-to", persist, "--test-scheduled"];
  for (const [k, v] of Object.entries(vars)) if (v !== undefined && v !== null) args.push("--var", `${k}:${v}`);
  const proc = startProcess(process.execPath, args, { cwd: join(snap, "worker"), env: env(), logPath, procs });
  const url = `http://127.0.0.1:${port}`;
  try {
    await until(
      async () => {
        if (proc.child.exitCode !== null) throw new Error(`wrangler dev exited (log: ${logPath})`);
        return fetch(`${url}/api/health`).then((r) => r.ok, () => false);
      },
      { ms: 90_000, every: 300, what: `the Worker on ${url}` },
    );
  } catch (e) {
    await proc.stop();
    throw e;
  }
  return { url, stop: () => proc.stop() };
}
