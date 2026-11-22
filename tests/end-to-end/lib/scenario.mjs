// What the scenarios share: the results table (one row a scenario: expected, what happened, PASS or FAIL),
// reading a Watchlist card the way a person reads it, watching what a page shows second by second, the words that
// would hedge on screen, screenshots, and one-line accounts of the backend's runs.

import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { failedJobs } from "./outcome.mjs";
import { secs, sleep } from "./proc.mjs";

/** Words that would hedge on screen (a card's or a panel's own text). */
export const HEDGE = /not available|not updated|paused|trial|beta|pending|queued|waiting|may take|up to|within|later|soon|delay|hour|minute|a day|next run|scheduled/i;

export const norm = (s) => String(s ?? "").replace(/\s+/g, " ").trim();

/** The most minutes a scenario process runs (E2E_CAP_MIN, default 60): then it fails and stops its stacks (cap()). */
export const CAP_MIN = Number(process.env.E2E_CAP_MIN) > 0 ? Number(process.env.E2E_CAP_MIN) : 60;

/** The results table of one scenario file (`tag` names its results-<tag>.json), and the process's cap. */
export class Report {
  constructor(name, shots, tag = "all") {
    this.name = name;
    this.shots = shots;
    this.tag = tag;
    this.rows = [];
    this.t0 = Date.now();
    this.last = ""; // the last say() line: what the scenario is doing
    this.ended = false; // finish() ran, or the cap fired
    mkdirSync(shots, { recursive: true });
    this.capTimer = setTimeout(() => this.cap(), Math.min(CAP_MIN * 60_000, 2 ** 31 - 1));
    this.capTimer.unref();
  }

  say(s) {
    this.last = s;
    console.log(`[${String(secs(this.t0)).padStart(7)} s] ${s}`);
  }

  /**
   * CAP_MIN minutes after the start (at most once, never after finish()): a FAIL row naming the cap and the last
   * thing said, the table and its JSON as finish() writes them, then SIGTERM, which stack.mjs answers by stopping
   * every stack of the process (each state folder removed) and exiting; exit 1 regardless 20 s later.
   */
  cap() {
    if (this.ended) return;
    this.ended = true;
    process.exitCode = 1;
    this.row(`${this.name}: the ${CAP_MIN}-minute cap`, `the scenario ends within ${CAP_MIN} min (E2E_CAP_MIN)`, `still going after ${CAP_MIN} min, so it was stopped while: ${norm(this.last) || "starting"}`, false);
    this.table(this.tag);
    setTimeout(() => process.exit(1), 20_000).unref();
    process.kill(process.pid, "SIGTERM");
  }

  /** No cap from here on: the scenario is over and keeps its stack up for a person (--keep). */
  uncap() {
    clearTimeout(this.capTimer);
  }

  row(scenario, expected, happened, pass, extra = {}) {
    this.rows.push({ scenario, expected, happened, pass: !!pass, ...extra });
    this.say(`${pass ? "PASS" : "FAIL"} ${scenario}: ${happened}`);
    return !!pass;
  }

  /** A row saying a scenario could not run (counts as failed). */
  skip(scenario, why) {
    return this.row(scenario, "-", `could not run: ${why}`, false, { skipped: true });
  }

  failedSince(i) {
    return this.rows.slice(i).some((r) => !r.pass);
  }

  async shot(page, name) {
    const file = join(this.shots, `${name}.png`);
    await page.screenshot({ path: file }).catch(() => {});
    return file;
  }

  /** Write the table as JSON and print it; returns how many rows failed. */
  table(tag = this.tag) {
    const out = join(this.shots, `results-${tag}.json`);
    writeFileSync(out, JSON.stringify(this.rows, null, 2));
    console.log("\nscenario | expected | what happened | result");
    for (const r of this.rows) console.log(`${r.scenario} | ${r.expected} | ${r.happened} | ${r.skipped ? "NOT RUN" : r.pass ? "PASS" : "FAIL"}`);
    const failed = this.rows.filter((r) => !r.pass);
    console.log(`\n${this.name}: ${this.rows.length - failed.length} of ${this.rows.length} passed in ${secs(this.t0)} s (results ${out}; screenshots ${this.shots})`);
    return failed.length;
  }

  /**
   * Print the table, write it as JSON, and exit 0 when every row passed. After the cap it does nothing: the cap
   * printed the table, and the signal it sent ends the process once every stack is stopped.
   */
  finish(tag = this.tag) {
    clearTimeout(this.capTimer);
    if (this.ended) return;
    this.ended = true;
    const failed = this.table(tag);
    process.exit(failed || !this.rows.length ? 1 : 0);
  }
}

// ---------- what the person sees ----------

export const cardOf = (page, pick) => page.locator("article.cl-card").filter({ has: page.locator("h3.cl-card-title", { hasText: pick.question }) });
export const addButton = (page, pick) => cardOf(page, pick).locator("button.wl-act-btn", { hasText: "Add to main page" });
export const onMainLink = (page, pick) => cardOf(page, pick).locator("a.wl-act-btn", { hasText: "On the main page" });

/** What the card's action reads now (its button, its state or its link, with an error under it). */
export async function faceOf(page, pick) {
  const card = cardOf(page, pick);
  if (!(await card.count().catch(() => 0))) return "(the card is not shown)";
  return norm(await card.locator(".cl-card-foot .wl-act").innerText({ timeout: 1500 }));
}

/** What the open details panel's action reads now. */
export async function panelOf(page) {
  const add = page.locator(".cl-add, a.cl-add-btn").first();
  if (!(await add.count().catch(() => 0))) return "(no panel open)";
  return norm(await add.innerText({ timeout: 1500 }));
}

/** Waits for `locator`, at most `ms`; true when it showed. */
export const shows = (locator, ms) => locator.waitFor({ timeout: ms }).then(() => true, () => false);

/**
 * Watch what each read gives, once a second, from `t0`: every change is one line ({t, what, v}). stop() ends it.
 * A read that throws (the page reloading) reads "(the page is loading)".
 */
export function watch(t0, reads, tag, say = console.log) {
  const log = [];
  const last = {};
  let on = true;
  const loop = (async () => {
    while (on) {
      for (const [what, read] of Object.entries(reads)) {
        const v = await read().catch(() => "(the page is loading)");
        if (v !== last[what]) {
          last[what] = v;
          log.push({ t: secs(t0), what, v });
          say(`  ${tag} +${secs(t0)} s ${what}: "${v}"`);
        }
      }
      await sleep(1000);
    }
  })();
  return {
    log,
    last,
    stop: async () => {
      on = false;
      await loop;
    },
    /** the first time `what` read something matching `re` */
    first: (what, re) => log.find((l) => l.what === what && re.test(l.v))?.t ?? null,
    /** every distinct text shown, in order */
    seen: (what) => log.filter((l) => !what || l.what === what).map((l) => `+${l.t}s "${l.v}"`).join(" → "),
  };
}

/** The texts a watcher saw that hedge (a limit's or the bot check's words are errors the person acts on). */
export const hedges = (w) => [...new Set(w.log.map((l) => l.v).filter((v) => HEDGE.test(v) && !/limit|bot check/i.test(v)))];

/** A fresh browser context (its own storage) on the Watchlist's day for `pick`, its card's action shown. */
export async function openWatchlist(browser, stack, pick, { timezoneId } = {}) {
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, ...(timezoneId ? { timezoneId } : {}) });
  const page = await context.newPage();
  page.errors = [];
  page.on("pageerror", (e) => page.errors.push(String(e)));
  await page.goto(`${stack.urls.site}#/watchlist?day=${pick.day}`);
  await cardOf(page, pick).waitFor({ timeout: 60_000 });
  await cardOf(page, pick).locator(".cl-card-foot .wl-act").waitFor({ timeout: 30_000 });
  await sleep(1500); // the card's first ask of the API
  return { context, page };
}

/** The visitor API's record of an add ({request: {...}} or {}). */
export const request = (stack, pick) => fetch(`${stack.urls.api}/api/request?cid=${pick.condition_id}`).then((r) => r.json(), () => ({}));

// ---------- what the backend did ----------

/** The add runs of a condition id, newest first (from the backend's status). */
export async function addRuns(stack, cid) {
  const o = await stack.status();
  return (o?.runs ?? []).filter((r) => r.kind === "add" && String(r.slot).startsWith(`${cid}:`));
}

/** One line on a run: its state, when it was made, first claimed and finished (seconds after `t0` ms), its jobs. */
export async function runLine(stack, id, t0) {
  const got = id ? await stack.run(id) : null;
  if (!got) return `run ${id ?? "none"}: not found`;
  const { run, jobs } = got;
  const s = (x) => (x ? `+${secs(t0, x * 1000)} s` : "-");
  const counts = {};
  for (const j of jobs) counts[`${j.kind} ${j.state}`] = (counts[`${j.kind} ${j.state}`] ?? 0) + 1;
  const bad = failedJobs(jobs);
  return (
    `run ${run.id.length > 40 ? `${run.id.slice(0, 18)}…${run.id.slice(-11)}` : run.id} ${run.state}: made ${s(run.created_at)}, first claim ${s(run.started_at)}, ` +
    `finished ${s(run.finished_at)}; jobs ${Object.entries(counts).map(([k, n]) => `${n} ${k}`).join(", ")}${bad ? ` (${bad})` : ""}`
  );
}

/** The Vercel calls since `t0` (ms) in one line. */
export async function callsLine(stack, t0, lane) {
  const calls = (await stack.vercel.calls()).filter((c) => c.at * 1000 >= t0 && (!lane || c.lane === lane));
  return `${calls.length} Vercel call(s)${calls.length ? `: ${calls.map((c) => `${c.lane} ${c.status} +${secs(t0, c.at * 1000)} s (${c.s} s${c.cold ? ", cold" : ""})`).join(", ")}` : ""}`;
}
