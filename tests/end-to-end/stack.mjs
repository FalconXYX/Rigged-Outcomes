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
// yesterday's daily runs recorded as done (so the first tick does not start a full daily run; --daily lets it, at
// any hour: dailyRun() moves the Worker's clock to 04:00 Toronto first when it is earlier),
// the step server and the Worker, then side by side the site's build (`npm run build`, as deploy.yml builds it),
// the 7 original markets (`backend.site originals`, from this repository's original-project branch) and the seed
// lists from live Polymarket and Google News data, saved the way the backend saves them (seed.py,
// `watchlist.state push --seed`, `watchlist.sync catalog`). Everything lives in the state
// folder (--state, default <tmp>/rigged-e2e-<base>), which a start empties first and a clean stop removes (--keep
// keeps it; a scenario keeps it when a check failed). Nothing reaches production: every process gets a clean
// environment, the Worker wakes only the local step server, and Python stubs Gemini and refuses production hosts
// (py/sitecustomize.py). With --scores the step function also gets WALLET_SCORES=on (an add goes on to the wallet
// rubric's wallets.score) and the Decisions API stand-in's mode file, <state>/gemini-mode, starting at the mode
// given (answer, quota, busy or fallback, then any of the switches polymarket and refuse; a scenario changes it
// with stack.gemini("answer refuse")).
//
//   node tests/end-to-end/stack.mjs --port-base 9500 [--state DIR] [--bot-fail] [--daily] [--no-cron]
//                                   [--picks N] [--cards N] [--big N] [--crowd N] [--min DOLLARS] [--days N] [--max-s S]
//                                   [--open-days-ago N] [--scores answer|quota|busy|fallback] [--quiet] [--keep]
//
// stays up until Ctrl-C. Scenarios import startStack() instead.

import { appendFileSync, existsSync, mkdirSync, readFileSync, renameSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { Cron } from "./lib/cron.mjs";
import { cleanEnv, killAllNow, listening, processSet, run, secs, stopAll, until } from "./lib/proc.mjs";
import { Site } from "./lib/site.mjs";
import { dayBefore, toronto, torontoAt } from "./lib/toronto.mjs";
import { missingPackages, snapshot, workspace } from "./lib/tree.mjs";
import { LocalVercel, WAKE_SECRET } from "./lib/vercel.mjs";
import { JOB_TOKEN, TURNSTILE, execute, migrate, startWorker } from "./lib/worker.mjs";

const HERE = dirname(fileURLToPath(import.meta.url));
export const REPO = join(HERE, "..", "..");
export const TERMINAL = ["done", "partial", "failed", "cancelled", "missed", "skipped"];

/** The Decisions API stand-in's modes, and the switches that may follow one (py/sitecustomize.py). */
export const GEMINI_MODES = ["answer", "quota", "busy", "fallback"];
export const GEMINI_SWITCHES = ["polymarket", "refuse"];

/** A stand-in mode line ("answer", "answer refuse polymarket"), or an error naming what is wrong with it. */
function modeLine(spec) {
  const [mode, ...switches] = String(spec).trim().toLowerCase().split(/\s+/);
  if (!GEMINI_MODES.includes(mode)) throw new Error(`the Decisions stand-in's mode must be one of ${GEMINI_MODES.join(", ")} (got "${spec}")`);
  const bad = switches.filter((w) => !GEMINI_SWITCHES.includes(w));
  if (bad.length) throw new Error(`no Decisions stand-in switch ${bad.join(", ")} (there are ${GEMINI_SWITCHES.join(", ")})`);
  return [mode, ...switches].join(" ");
}

/** The mode file written whole (a call reading it never sees it half written). */
function writeMode(file, line) {
  writeFileSync(`${file}.tmp`, `${line}\n`);
  renameSync(`${file}.tmp`, file);
}

/** SQL that records a day's daily run as done (the stack's start: no full daily run unless asked for). */
const dailyDone = (day, at) =>
  `INSERT OR IGNORE INTO runs (id, kind, slot, state, meta, created_at, started_at, deadline, finished_at) ` +
  `VALUES ('daily:${day}', 'daily', '${day}', 'done', '{}', ${at}, ${at}, ${at}, ${at})`;

/** The stacks of this process not stopped yet: each one's stop() (a second call waits for the first). */
const live = new Set();
let ending = false;
/**
 * The state folders a stop removed (and no stack has started in since): removed again at exit, since a stack
 * stopped by a signal while it started, or in a startApi(), can still write to its folder after its stop.
 */
const removed = new Set();

/**
 * Ctrl-C or SIGTERM (a scenario's cap sends one): every stack stopped, each state folder removed (unless kept),
 * then exit 130; a second signal exits at once. Installed with the first stack, for the life of the process, with
 * an exit hook that kills whatever any stack started and is still going, then removes those folders again.
 */
function onSignal() {
  if (ending) process.exit(130);
  ending = true;
  Promise.allSettled([...live].map((stop) => stop())).then(() => process.exit(130));
}
let hooked = false;
function hook() {
  if (hooked) return;
  hooked = true;
  process.on("SIGINT", onSignal);
  process.on("SIGTERM", onSignal);
  process.on("exit", () => {
    killAllNow();
    for (const dir of removed) {
      try {
        rmSync(dir, { recursive: true, force: true });
      } catch {
        /* the exit goes on */
      }
    }
  });
}

/** The schedule's daily_at ("HH:MM", Toronto) in a copy's worker/src/contract.ts. */
const dailyAt = (dir) => /"daily_at":\s*"(\d\d:\d\d)"/.exec(readFileSync(join(dir, "worker", "src", "contract.ts"), "utf8"))?.[1] ?? "04:00";

/**
 * Start a stack. Options: portBase (required), state (folder), botFail (Turnstile's always-failing keys), daily
 * (today's daily run not recorded as done: dailyRun() before the first tick has it made at any hour), cron
 * (default true: fire the trigger every minute), maxS (the step server's per-call limit), seed ({picks, cards,
 * big, crowd, min, days, openDaysAgo} or false), pyEnv (more environment for the step function's Python), scores
 * (a Decisions stand-in mode: the step function gets WALLET_SCORES=on and the mode file), keep, quiet, log.
 * Returns the stack: its urls, vercel, cron, site, seed, python(), api(), status(), run(), runOf(), waitRun(), clock(),
 * dailyRun(), tick(), devTick(), sql(), stopApi()/startApi(), gemini(mode), geminiCalls(), polymarketCalls() and
 * stop({keepState}). Each stack stops only the processes it started, so several share one node process.
 */
export async function startStack(opts) {
  hook();
  const base = Number(opts.portBase);
  if (!Number.isInteger(base) || base < 1024 || base > 65000) throw new Error("--port-base must be a port number (1024 to 65000)");
  const ports = { api: base, step: base + 1, site: base + 2, inspector: base + 3 };
  for (const [what, port] of Object.entries(ports)) {
    if (await listening(port)) throw new Error(`port ${port} (${what}) is in use; pick another --port-base (the harness never stops what it didn't start)`);
  }
  const missing = missingPackages(REPO);
  if (missing.length) throw new Error(`install the packages first: ${missing.map((p) => `(cd ${p} && npm install)`).join(" ")}`);
  const scores = opts.scores ? modeLine(opts.scores) : null; // checked before the state folder is touched

  const state = opts.state ?? join(tmpdir(), `rigged-e2e-${base}`);
  if (ending) throw new Error("this process is stopping (a signal or the scenario's cap): no new stack");
  removed.delete(state); // this stack's folder from now on (kept or removed by its own stop)
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
  const modeFile = join(state, "gemini-mode");
  if (scores) writeMode(modeFile, scores);
  const stackLog = join(dirs.logs, "stack.log");
  const print = opts.quiet ? () => {} : (opts.log ?? ((s) => console.log(s)));
  const say = (s) => {
    try {
      appendFileSync(stackLog, `${new Date().toISOString()} ${s}\n`);
    } catch {
      /* the state folder is already removed (a signal stopped the stack while it started) */
    }
    print(`[e2e ${base}] ${s}`);
  };
  const t0 = Date.now();
  let worker = null;
  let site = null;
  let vercel = null;
  let cron = null;
  const procs = processSet();

  // once: a second call (the scenario's own, after a signal's) waits for the first
  let stopping = null;
  const stop = ({ keepState = false } = {}) =>
    (stopping ??= (async () => {
      try {
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
        removed.add(state);
        print(`[e2e ${base}] the state folder is removed (${state})`);
      } finally {
        live.delete(stop);
      }
    })());
  live.add(stop);

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
      env: {
        ...pyEnv,
        WAKE_SECRET,
        WATCHLIST_API_URL: worker.url,
        WATCHLIST_JOB_TOKEN: JOB_TOKEN,
        GEMINI_API_KEY: "e2e-stub-gemini-key",
        RO_TMP_ROOT: dirs.tmp,
        ...(scores ? { WALLET_SCORES: "on", E2E_GEMINI_MODE_FILE: modeFile } : {}),
      },
    }).start();
    say(`Vercel ${vercel.stepUrl} (Fluid-like, calls stopped at ${opts.maxS ?? 300} s)${scores ? `; wallet scores on, Decisions stand-in: ${scores}` : ""}`);

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
      const made = await python(
        [
          join(HERE, "seed.py"),
          ...["--root", ws, "--out", join(ws, "web", "public", "data")],
          ...["--picks", String(s.picks ?? 3), "--cards", String(s.cards ?? 2), "--big", String(s.big ?? 0), "--crowd", String(s.crowd ?? 0)],
          ...(s.min ? ["--min", String(s.min)] : []),
          ...(s.days ? ["--days", String(s.days)] : []),
          ...(s.openDaysAgo ? ["--open-days-ago", String(s.openDaysAgo)] : []),
        ],
        { api: false, ws },
      );
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
    /**
     * Waits until a run is terminal (or `pred(run, jobs)` holds), asking every `every` ms; returns {run, jobs}.
     * Rejects with "run <id> was never made" when the run is still not there `madeMs` after the wait starts,
     * instead of waiting out `ms`.
     */
    const waitRun = (id, { ms = 15 * 60_000, pred, madeMs = 3 * 60_000, every = 2000 } = {}) => {
      const from = Date.now();
      let seen = false;
      return until(
        async () => {
          const got = await getRun(id);
          if (!got) {
            if (!seen && Date.now() - from >= madeMs) throw new Error(`run ${id} was never made`);
            return null;
          }
          seen = true;
          return (pred ? pred(got.run, got.jobs) : TERMINAL.includes(got.run.state)) ? got : null;
        },
        { ms, every, what: `run ${id}` },
      );
    };
    /** The Worker's clock: move it to `to` (epoch seconds; null puts it back); returns its new now. */
    const clock = async (to) => (await api("POST", "/api/dev/clock", { now: to })).body?.now;
    /** The Worker's now, in epoch seconds. */
    const workerNow = async () => (await status()).now;
    /**
     * The daily run the next tick makes, at any real hour (a stack started with daily: true, called before its
     * first tick): the Worker's clock is moved to daily_at Toronto on the Worker's day when it is earlier, and runs
     * on from there. {id, day, skew}: skew is the Worker's clock minus the real one (ms), so a Worker time t (epoch
     * seconds) is real time t * 1000 - skew.
     */
    const dailyRun = async () => {
      if (!opts.daily) throw new Error("dailyRun() needs a stack started with daily: true (else today's daily run is recorded as done)");
      const was = await workerNow();
      const { day } = toronto(was);
      const at = torontoAt(day, dailyAt(dirs.api));
      if (was < at) await clock(at);
      return { id: `daily:${day}`, day, skew: (await workerNow()) * 1000 - Date.now() };
    };
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

    /** The Decisions stand-in's mode (and switches) from now on, read on its next call; a stack with scores only. */
    const gemini = (spec) => {
      if (!scores) throw new Error("this stack was started without scores");
      const line = modeLine(spec);
      writeMode(modeFile, line);
      say(`the Decisions stand-in now answers: ${line}`);
    };

    /** The Gemini stand-in's calls so far ({kind: "models" | "model" | "decisions", ...}). */
    const geminiCalls = () => {
      const f = join(dirs.logs, "gemini.jsonl");
      return existsSync(f) ? readFileSync(f, "utf8").trim().split("\n").filter(Boolean).map((l) => JSON.parse(l)) : [];
    };

    /** The step function's Polymarket requests so far ({at, host, path, status}; a stack with scores only). */
    const polymarketCalls = () => {
      const f = join(dirs.logs, "polymarket.jsonl");
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
      dailyRun,
      tick,
      devTick,
      sql,
      stopApi,
      startApi,
      geminiCalls,
      polymarketCalls,
      gemini,
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
    seed: args["no-seed"]
      ? false
      : {
          picks: Number(args.picks ?? 3),
          cards: Number(args.cards ?? 2),
          big: Number(args.big ?? 0),
          crowd: Number(args.crowd ?? 0),
          min: args.min ? Number(args.min) : undefined,
          days: args.days ? Number(args.days) : undefined,
          openDaysAgo: args["open-days-ago"] ? Number(args["open-days-ago"]) : undefined,
        },
    scores: args.scores === true ? "answer" : args.scores || undefined,
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
  if (args.daily) console.log(`  Daily run        ${(await stack.dailyRun()).id}, made by the next tick`);
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
