// Process and network helpers the harness shares: starting a long-lived process in its own process group (so
// stopping it stops its children too: wrangler's workerd, a job step's python), waiting for a port or a URL,
// and a clean environment for anything the harness runs, so nothing of the person's own shell (a production
// API address, a real token) reaches a test process.

import { spawn } from "node:child_process";
import { createWriteStream, existsSync } from "node:fs";
import { createConnection } from "node:net";
import { delimiter, join } from "node:path";

export const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/**
 * The process groups started here and still going (a job step's script, a long-lived server), all of them; each
 * stack also keeps its own set (procs below), so stopping one stack leaves another's alone.
 */
const active = new Set();

/** A set of process groups of one owner (a stack): pass it as `procs` to run and startProcess. */
export const processSet = () => new Set();

/** Stop every process group still going in `procs` (default: all; SIGTERM, then SIGKILL a few seconds later). */
export async function stopAll(procs = active) {
  const pids = [...procs];
  pids.forEach((pid) => killGroup(pid, "SIGTERM"));
  if (pids.length) await sleep(3000);
  pids.forEach((pid) => killGroup(pid, "SIGKILL"));
  for (const pid of pids) active.delete(pid);
  procs.clear();
}

/** The same, at once and without waiting (for process.on("exit")). */
export function killAllNow(procs = active) {
  for (const pid of procs) {
    killGroup(pid, "SIGKILL");
    active.delete(pid);
  }
  procs.clear();
}

const track = (procs, pid) => {
  active.add(pid);
  if (procs && procs !== active) procs.add(pid);
};
const untrack = (procs, pid) => {
  active.delete(pid);
  procs?.delete(pid);
};

/** Whether something listens on 127.0.0.1:port. */
export const listening = (port) =>
  new Promise((resolve) => {
    const s = createConnection({ host: "127.0.0.1", port }, () => {
      s.destroy();
      resolve(true);
    });
    s.on("error", () => resolve(false));
  });

/** Waits until `probe()` is truthy (checked every `every` ms), up to `ms`; returns its value or throws `what`. */
export async function until(probe, { ms = 60_000, every = 250, what = "the condition" } = {}) {
  const end = Date.now() + ms;
  for (;;) {
    const v = await probe();
    if (v) return v;
    if (Date.now() > end) throw new Error(`timed out after ${Math.round(ms / 1000)} s waiting for ${what}`);
    await sleep(every);
  }
}

/** The first executable named `name` on PATH (skipping `skip`), or null. */
export function which(name, skip = []) {
  for (const dir of (process.env.PATH ?? "").split(delimiter)) {
    if (!dir || skip.includes(dir)) continue;
    const p = join(dir, name);
    if (existsSync(p)) return p;
  }
  return null;
}

/**
 * The environment every process the harness starts gets: PATH, HOME, the user and the locale, and nothing else
 * of this shell's (no WATCHLIST_*, GEMINI_*, VITE_* or tokens), plus `extra`.
 */
export function cleanEnv(extra = {}) {
  const keep = ["PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "NVM_DIR", "NVM_BIN"];
  const env = {};
  for (const k of keep) if (process.env[k] !== undefined) env[k] = process.env[k];
  return { ...env, ...extra };
}

/**
 * A long-lived child in its own process group, its output appended to `logPath` (tracked in `procs` too, when
 * given). Returns { child, stop() }: stop sends SIGTERM to the whole group, then SIGKILL after `graceMs`.
 */
export function startProcess(cmd, args, { cwd, env, logPath, graceMs = 5000, procs } = {}) {
  const log = createWriteStream(logPath, { flags: "a" });
  const child = spawn(cmd, args, { cwd, env, detached: true, stdio: ["ignore", "pipe", "pipe"] });
  track(procs, child.pid);
  child.stdout.pipe(log, { end: false });
  child.stderr.pipe(log, { end: false });
  const gone = new Promise((r) => child.once("exit", r));
  child.once("exit", () => {
    untrack(procs, child.pid);
    log.end();
  });
  return {
    child,
    async stop() {
      if (child.exitCode !== null || child.signalCode !== null) return;
      killGroup(child.pid, "SIGTERM");
      await Promise.race([gone, sleep(graceMs)]);
      if (child.exitCode === null && child.signalCode === null) killGroup(child.pid, "SIGKILL");
      await Promise.race([gone, sleep(1000)]);
    },
  };
}

export function killGroup(pid, signal) {
  try {
    process.kill(-pid, signal);
  } catch {
    try {
      process.kill(pid, signal);
    } catch {
      /* already gone */
    }
  }
}

/**
 * Run a command to its end: { code, out } with stdout and stderr together (also appended to `logPath` when
 * given). `timeoutMs` stops its process group (code 124, as GNU timeout reports it). `procs`: the owner's set.
 */
export function run(cmd, args, { cwd, env, logPath, input, timeoutMs, procs } = {}) {
  return new Promise((resolve) => {
    const log = logPath ? createWriteStream(logPath, { flags: "a" }) : null;
    const child = spawn(cmd, args, { cwd, env, detached: true, stdio: [input === undefined ? "ignore" : "pipe", "pipe", "pipe"] });
    if (child.pid) track(procs, child.pid);
    let out = "";
    let timedOut = false;
    const take = (d) => {
      out += d;
      log?.write(d);
    };
    child.stdout.on("data", take);
    child.stderr.on("data", take);
    if (input !== undefined) child.stdin.end(input);
    const timer = timeoutMs
      ? setTimeout(() => {
          timedOut = true;
          killGroup(child.pid, "SIGTERM");
          setTimeout(() => killGroup(child.pid, "SIGKILL"), 5000).unref();
        }, timeoutMs)
      : null;
    child.on("close", (code) => {
      untrack(procs, child.pid);
      if (timer) clearTimeout(timer);
      log?.end();
      resolve({ code: timedOut ? 124 : (code ?? 1), out });
    });
    child.on("error", (e) => {
      if (timer) clearTimeout(timer);
      log?.end();
      resolve({ code: 127, out: String(e) });
    });
  });
}

/** Seconds since `t0` (ms), one decimal. */
export const secs = (t0, t1 = Date.now()) => Math.round((t1 - t0) / 100) / 10;
