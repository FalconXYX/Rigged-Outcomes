// One isolated copy of the whole production path on this Mac, on four ports from --port-base:
//
//   base+0  the API: `wrangler dev --test-scheduled` for worker/ (its own D1 state, every setting by --var;
//           lib/worker.mjs), its Cron Trigger fired every minute through wrangler's local scheduled path
//           (lib/cron.mjs), as Cloudflare fires it
//   base+1  Vercel: api/step.py served by backend/dev_server.py, Fluid-like, every call stopped at 300 s, with the
//           harness's fault switches (lib/vercel.mjs); the Worker wakes it with signed wake-ups
//   base+2  the site: the static build of web/, at /Rigged-Outcomes/ (lib/site.mjs); every market and both lists
//           it reads from the Worker, as on GitHub Pages
//   base+3  wrangler's inspector
//
// Starting it: a snapshot of the working tree (lib/tree.mjs), the migrations on a fresh D1 with today's and
// yesterday's daily runs recorded as done (so the first tick does not start a full daily run; --daily lets it),
// the step server and the Worker, then side by side the site's build (`npm run build`, as deploy.yml builds it),
// the 7 original markets (`backend.site originals`, from this repository's original-project branch) and the seed
// lists from live Polymarket and Google News data, saved the way the backend saves them (seed.py,
// `watchlist.state push --seed`, `watchlist.sync catalog`). Everything lives in the state
// folder (--state, default <tmp>/rigged-e2e-<base>), which a start empties first and a clean stop removes (--keep
// keeps it; a scenario keeps it when a check failed). Nothing reaches production: every process gets a clean
// environment, the Worker wakes only the local step server, and Python stubs Gemini and refuses production hosts
// (py/sitecustomize.py).
//
//   node tests/end-to-end/stack.mjs --port-base 9500 [--state DIR] [--bot-fail] [--daily] [--no-cron]
//                                   [--picks N] [--cards N] [--big N] [--crowd N] [--max-s S] [--quiet] [--keep]
//
// stays up until Ctrl-C. Scenarios import startStack() instead.

import { appendFileSync, existsSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { Cron } from "./lib/cron.mjs";
import { cleanEnv, killAllNow, listening, processSet, run, secs, stopAll, until } from "./lib/proc.mjs";
import { Site } from "./lib/site.mjs";
import { dayBefore, toronto } from "./lib/toronto.mjs";
import { missingPackages, snapshot, workspace } from "./lib/tree.mjs";
import { LocalVercel, WAKE_SECRET } from "./lib/vercel.mjs";
import { JOB_TOKEN, TURNSTILE, execute, migrate, startWorker } from "./lib/worker.mjs";

const HERE = dirname(fileURLToPath(import.meta.url));
export const REPO = join(HERE, "..", "..");
export const TERMINAL = ["done", "partial", "failed", "cancelled", "missed", "skipped"];

/** SQL that records a day's daily run as done (the stack's start: no full daily run unless asked for). */
const dailyDone = (day, at) =>
  `INSERT OR IGNORE INTO runs (id, kind, slot, state, meta, created_at, started_at, deadline, finished_at) ` +
  `VALUES ('daily:${day}', 'daily', '${day}', 'done', '{}', ${at}, ${at}, ${at}, ${at})`;

/**
 * Start a stack. Options: portBase (required), state (folder), botFail (Turnstile's always-failing keys), daily
 * (let the first tick start today's daily run), cron (default true: fire the trigger every minute), maxS (the
 * step server's per-call limit), seed ({picks, cards} or false), pyEnv (more environment for the step function's
 * Python), keep, quiet, log. Returns the stack: its urls, vercel, cron, site, seed, python(), api(),
 * status(), run(), runOf(), waitRun(), clock(), tick(), devTick(), sql(), stopApi()/startApi() and
 * stop({keepState}). Each stack stops only the processes it started, so several share one node process.
 */
export async function startStack(opts) {
  const base = Number(opts.portBase);
  if (!Number.isInteger(base) || base < 1024 || base > 65000) throw new Error("--port-base must be a port number (1024 to 65000)");
  const ports = { api: base, step: base + 1, site: base + 2, inspector: base + 3 };
  for (const [what, port] of Object.entries(ports)) {
    if (await listening(port)) throw new Error(`port ${port} (${what}) is in use; pick another --port-base (the harness never stops what it didn't start)`);
  }
  const missing = missingPackages(REPO);
  if (missing.length) throw new Error(`install the packages first: ${missing.map((p) => `(cd ${p} && npm install)`).join(" ")}`);

  const state = opts.state ?? join(tmpdir(), `rigged-e2e-${base}`);
  rmSync(state, { recursive: true, force: true });
  const dirs = {
    snap: join(state, "repo"),
    api: join(state, "api"),
    d1: join(state, "d1"),
    step: join(state, "step"),
    tmp: join(state, "tmp"),
    web: join(state, "web"),
    site: join(state, "site"),
    logs: join(state, "logs"),
  };
  for (const d of Object.values(dirs)) mkdirSync(d, { recursive: true });
  const stackLog = join(dirs.logs, "stack.log");
  const print = opts.quiet ? () => {} : (opts.log ?? ((s) => console.log(s)));
  const say = (s) => {
    appendFileSync(stackLog, `${new Date().toISOString()} ${s}\n`);
    print(`[e2e ${base}] ${s}`);
  };
  const t0 = Date.now();
  let worker = null;
  let site = null;
  let vercel = null;
  let cron = null;
  const procs = processSet();
  const exitHook = () => killAllNow(procs);
  const onSignal = async () => {
    await stop();
    process.exit(130);
  };
  process.on("exit", exitHook);
  process.once("SIGINT", onSignal);
  process.once("SIGTERM", onSignal);

  let stopped = false;
  const stop = async ({ keepState = false } = {}) => {
    if (stopped) return;
    stopped = true;
    process.off("exit", exitHook);
    process.off("SIGINT", onSignal);
    process.off("SIGTERM", onSignal);
    await cron?.stop();
    await vercel?.stop();
    await worker?.stop();
    await stopAll(procs);
    await site?.close();
    if (keepState || opts.keep) {
      say(`stopped; the state folder stays: ${state}`);
      return;
    }
    say("stopped");
    rmSync(state, { recursive: true, force: true });
    print(`[e2e ${base}] the state folder is removed (${state})`);
  };

  try {
    const files = snapshot(REPO, dirs.snap);
    say(`snapshot of the working tree: ${files} files -> ${dirs.snap}`);
    site = await new Site({ port: ports.site, dir: dirs.site }).listen();

    // the Worker runs from a copy of its own, so its .wrangler folder is the stack's
    workspace(dirs.snap, dirs.api, REPO);
    const migrationsLog = join(dirs.logs, "migrations.log");
    await migrate(dirs.api, dirs.d1, migrationsLog, procs);
    const now = Math.floor(Date.now() / 1000);
    const today = toronto(now).day;
    const done = opts.daily ? [dayBefore(today)] : [today, dayBefore(today)];
    await execute(dirs.api, dirs.d1, done.map((d) => dailyDone(d, now)).join("; "), migrationsLog, procs);

    const keys = opts.botFail ? TURNSTILE.fail : TURNSTILE.pass;
    const workerOpts = {
      snap: dirs.api,
      persist: dirs.d1,
      port: ports.api,
      inspectorPort: ports.inspector,
      logPath: join(dirs.logs, "worker.log"),
      procs,
      vars: {
        TURNSTILE_SECRET: keys.secret,
        JOB_TOKEN,
        VISITOR_SALT: `e2e-salt-${base}`,
        ALLOWED_ORIGINS: `${site.origin},http://localhost:${ports.site}`,
        TURNSTILE_HOSTNAMES: "example.com",
        TURNSTILE_STRICT: "off",
        DEV: "on",
        STEP_URL: `http://127.0.0.1:${ports.step}/api/step`,
        WAKE_SECRET,
      },
    };
    worker = await startWorker(workerOpts);
    say(`API ${worker.url} (D1 ${dirs.d1})`);

    // what every Python process of the stack sees: the Gemini stand-in, bytecode and /tmp in the state folder
    const pyEnv = {
      PYTHONPATH: [opts.pyPath, join(HERE, "py")].filter(Boolean).join(":"),
      PYTHONPYCACHEPREFIX: join(state, "pycache"),
      E2E_STUB: "1",
      E2E_LOG_DIR: dirs.logs,
      ...(opts.pyEnv ?? {}),
    };
    workspace(dirs.snap, dirs.step, REPO);
    vercel = await new LocalVercel({
      snap: dirs.step,
      port: ports.step,
      logPath: join(dirs.logs, "vercel.log"),
      procs,
      maxS: opts.maxS ?? 300,
      env: { ...pyEnv, WAKE_SECRET, WATCHLIST_API_URL: worker.url, WATCHLIST_JOB_TOKEN: JOB_TOKEN, GEMINI_API_KEY: "e2e-stub-gemini-key", RO_TMP_ROOT: dirs.tmp },
    }).start();
    say(`Vercel ${vercel.stepUrl} (Fluid-like, calls stopped at ${opts.maxS ?? 300} s)`);

    /** Run a command in a fresh workspace with the stack's API settings (what a person runs from the Mac). */
    let nth = 0;
    const python = async (args, { api = true, ws, env: more = {} } = {}) => {
      const dir = ws ?? workspace(dirs.snap, join(state, "mac", String(++nth), "work"), REPO);
      const env = cleanEnv({ ...pyEnv, ...(api ? { WATCHLIST_API_URL: worker.url, WATCHLIST_JOB_TOKEN: JOB_TOKEN } : {}), ...more });
      const res = await run("python3", args, { cwd: dir, env, logPath: join(dirs.logs, "python.log"), procs });
      return { ...res, ws: dir };
    };

    // the site's build, as deploy.yml makes it, side by side with the original markets and the seed
    const build = (async () => {
      const t = Date.now();
      workspace(dirs.snap, dirs.web, REPO);
      const env = cleanEnv({ VITE_API_URL: worker.url, VITE_TURNSTILE_SITEKEY: keys.sitekey });
      const b = await run("npm", ["run", "build"], { cwd: join(dirs.web, "web"), env, logPath: join(dirs.logs, "build.log"), procs });
      if (b.code !== 0) throw new Error(`the site's build failed (log: ${join(dirs.logs, "build.log")})`);
      await site.deploy(join(dirs.web, "web", "dist"), "build");
      say(`the site is built and served in ${secs(t)} s`);
    })();

    // the 7 original markets, as production has them (their CSVs from the repository's original-project branch)
    const originals = (async () => {
      const t = Date.now();
      const res = await python(["-m", "backend.site", "originals"], { env: { GIT_DIR: join(REPO, ".git") } });
      if (res.code !== 0) throw new Error(`backend.site originals failed:\n${res.out.slice(-2000)}`);
      say(`the 7 original markets saved in ${secs(t)} s`);
    })();

    let seed = null;
    const seeding = (async () => {
      if (opts.seed === false) return;
      const s = opts.seed ?? {};
      const ws = workspace(dirs.snap, join(state, "seed", "work"), REPO);
      const t = Date.now();
      const made = await python([join(HERE, "seed.py"), "--root", ws, "--out", join(ws, "web", "public", "data"), "--picks", String(s.picks ?? 3), "--cards", String(s.cards ?? 2), "--big", String(s.big ?? 0), "--crowd", String(s.crowd ?? 0)], { api: false, ws });
      if (made.code !== 0) throw new Error(`the seed failed:\n${made.out.slice(-2000)}`);
      seed = JSON.parse(made.out.trim().split("\n").pop());
      seed.ws = ws;
      for (const cmd of [
        ["-m", "watchlist.state", "push", "--seed", "--by", "local"],
        ["-m", "watchlist.sync", "catalog"],
      ]) {
        const res = await python(cmd, { ws });
        if (res.code !== 0) throw new Error(`${cmd.slice(1).join(" ")} failed:\n${res.out.slice(-2000)}`);
      }
      say(`seeded ${seed.picks.length} closed pick(s) and ${seed.cards.length} open card(s) in ${secs(t)} s: ${seed.picks.map((p) => p.slug).join(", ")}`);
    })();
    const [b, og, sd] = await Promise.allSettled([build, originals, seeding]);
    if (b.status === "rejected") throw b.reason;
    if (og.status === "rejected") throw og.reason;
    if (sd.status === "rejected") throw sd.reason;

    cron = new Cron({ api: worker.url, onError: (e) => say(`the cron tick failed: ${e.message}`) });
    if (opts.cron !== false) cron.start();
    say(`ready in ${secs(t0)} s: site ${site.url} · API ${worker.url} · Vercel ${vercel.stepUrl}`);

    const call = async (method, path, body, token) => {
      const res = await fetch(`${worker.url}${path}`, {
        method,
        headers: { authorization: `Bearer ${token}`, ...(body ? { "content-type": "application/json" } : {}) },
        body: body ? JSON.stringify(body) : undefined,
      });
      const textBody = await res.text();
      let parsed = null;
      try {
        parsed = textBody ? JSON.parse(textBody) : null;
      } catch {
        parsed = textBody;
      }
      return { status: res.status, body: parsed };
    };
    /** The job's endpoints (job token). */
    const api = (method, path, body, token = JOB_TOKEN) => call(method, path, body, token);
    /** The backend at a glance (GET /api/job/status): {now, budget, lanes, runs, failed_jobs, requests, reports}. */
    const status = async () => (await api("GET", "/api/job/status")).body;
    /** A run and its jobs ({run, jobs}), or null. */
    const getRun = async (id) => {
      const r = await api("GET", `/api/job/run?id=${encodeURIComponent(id)}`);
      return r.status === 200 ? r.body : null;
    };
    /** The add run of a condition id (the newest), from the backend's status. */
    const runOf = async (cid) => {
      const o = await status();
      const req = (o?.requests ?? []).find((q) => q.condition_id === cid);
      if (req?.run_id) return req.run_id;
      const runs = (o?.runs ?? []).filter((r) => r.kind === "add" && String(r.slot).startsWith(`${cid}:`));
      return runs[0]?.id ?? null;
    };
    /** Waits until a run is terminal (or `pred(run, jobs)` holds); returns {run, jobs}. */
    const waitRun = (id, { ms = 15 * 60_000, pred } = {}) =>
      until(
        async () => {
          const got = await getRun(id);
          if (!got) return null;
          return (pred ? pred(got.run, got.jobs) : TERMINAL.includes(got.run.state)) ? got : null;
        },
        { ms, every: 2000, what: `run ${id}` },
      );
    /** The Worker's clock: move it to `to` (epoch seconds; null puts it back); returns its new now. */
    const clock = async (to) => (await api("POST", "/api/dev/clock", { now: to })).body?.now;
    /** The Worker's now, in epoch seconds. */
    const workerNow = async () => (await status()).now;
    /** The Cron Trigger once, through wrangler's scheduled path (what Cloudflare fires). */
    const tick = () => cron.tick();
    /** The same tick through POST /api/dev/tick, which answers what it made: {created, woke, expired}. */
    const devTick = async () => (await api("POST", "/api/dev/tick")).body;

    /** The Worker down: its process stopped (its port refuses connections) until startApi(). */
    const stopApi = async () => {
      await worker.stop();
      await until(async () => !(await listening(ports.api)), { ms: 20_000, what: "the API to stop" });
      say("the API is stopped");
    };
    /** The Worker back, on the same port, settings and database. */
    const startApi = async () => {
      worker = await startWorker(workerOpts);
      say("the API is back");
    };
    /** SQL on the stack's D1, with the Worker stopped for it and started again (fault setup only). */
    const sql = async (statements) => {
      await stopApi();
      try {
        await execute(dirs.api, dirs.d1, statements, migrationsLog, procs);
      } finally {
        await startApi();
      }
    };

    /** The Gemini stand-in's calls so far ({kind: "models" | "model", ...}). */
    const geminiCalls = () => {
      const f = join(dirs.logs, "gemini.jsonl");
      return existsSync(f) ? readFileSync(f, "utf8").trim().split("\n").filter(Boolean).map((l) => JSON.parse(l)) : [];
    };

    return {
      state,
      dirs,
      ports,
      urls: { api: worker.url, site: site.url, step: vercel.stepUrl },
      vercel,
      cron,
      site,
      seed,
      today,
      python,
      api,
      status,
      run: getRun,
      runOf,
      waitRun,
      clock,
      workerNow,
      tick,
      devTick,
      sql,
      stopApi,
      startApi,
      geminiCalls,
      say,
      stop,
    };
  } catch (e) {
    say(`could not start: ${e.message}`);
    await stop({ keepState: true }); // its logs say why
    throw e;
  }
}

// ---------- the command line ----------

/** --name value / --name=value / --flag, for the scenarios too. */
export function parseArgs(argv) {
  const out = {};
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (!a.startsWith("--")) continue;
    const eq = a.indexOf("=");
    if (eq > 0) out[a.slice(2, eq)] = a.slice(eq + 1);
    else if (argv[i + 1] !== undefined && !argv[i + 1].startsWith("--")) out[a.slice(2)] = argv[++i];
    else out[a.slice(2)] = true;
  }
  return out;
}

export function stackOptions(args) {
  return {
    portBase: Number(args["port-base"] ?? 9500),
    state: args.state,
    botFail: !!args["bot-fail"],
    daily: !!args.daily,
    cron: !args["no-cron"],
    maxS: args["max-s"] ? Number(args["max-s"]) : undefined,
    seed: args["no-seed"] ? false : { picks: Number(args.picks ?? 3), cards: Number(args.cards ?? 2), big: Number(args.big ?? 0), crowd: Number(args.crowd ?? 0) },
    keep: !!args.keep,
    quiet: !!args.quiet,
  };
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  const args = parseArgs(process.argv.slice(2));
  const stack = await startStack(stackOptions(args)).catch((e) => {
    console.error(e.message);
    process.exit(1);
  });
  const a = stack.urls.api;
  console.log(
    [
      "",
      `  Watchlist page   ${stack.urls.site}#/watchlist`,
      `  Runs             curl -s -H 'authorization: Bearer ${JOB_TOKEN}' ${a}/api/job/status | jq '.runs'`,
      `  Daily run now    curl -s -X POST -H 'authorization: Bearer ${JOB_TOKEN}' ${a}/api/dev/clock -d '{"now": <04:00 Toronto tomorrow>}'   (the next tick starts it)`,
      `  Vercel calls     curl -s ${stack.vercel.url}/__dev/calls | jq '.calls'`,
      `  Logs             ${stack.dirs.logs}`,
      "",
      `  Ctrl-C stops everything this stack started${args.keep ? "" : " and removes its state folder (--keep keeps it)"}.`,
    ].join("\n"),
  );
  writeFileSync(join(stack.dirs.logs, "ready"), new Date().toISOString());
}
