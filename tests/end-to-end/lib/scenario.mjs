// What the scenarios share: the results table (one row a scenario: expected, what happened, PASS or FAIL),
// reading a Watchlist card the way a person reads it, watching what a page shows second by second, the words that
// would hedge on screen, screenshots, and one-line accounts of the backend's runs.

import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { failedJobs } from "./outcome.mjs";
import { secs, sleep } from "./proc.mjs";
import { OWNER_TOKEN } from "./worker.mjs";

/** Words that would hedge on screen (a card's or a panel's own text). */
export const HEDGE = /not available|not updated|paused|trial|beta|pending|queued|waiting|may take|up to|within|later|soon|delay|hour|minute|a day|next run|scheduled/i;

export const norm = (s) => String(s ?? "").replace(/\s+/g, " ").trim();

/** The results table of one scenario file. */
export class Report {
  constructor(name, shots) {
    this.name = name;
    this.shots = shots;
    this.rows = [];
    this.t0 = Date.now();
    mkdirSync(shots, { recursive: true });
  }

  say(s) {
    console.log(`[${String(secs(this.t0)).padStart(7)} s] ${s}`);
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

  /** Print the table, write it as JSON, and exit 0 when every row passed. */
  finish(tag = "all") {
    const out = join(this.shots, `results-${tag}.json`);
    writeFileSync(out, JSON.stringify(this.rows, null, 2));
    console.log("\nscenario | expected | what happened | result");
    for (const r of this.rows) console.log(`${r.scenario} | ${r.expected} | ${r.happened} | ${r.skipped ? "NOT RUN" : r.pass ? "PASS" : "FAIL"}`);
    const failed = this.rows.filter((r) => !r.pass);
    console.log(`\n${this.name}: ${this.rows.length - failed.length} of ${this.rows.length} passed in ${secs(this.t0)} s (results ${out}; screenshots ${this.shots})`);
    process.exit(failed.length || !this.rows.length ? 1 : 0);
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

/** The owner's review page in a context of its own: the text of its Status and Backend sections. */
export async function ownerSees(browser, stack) {
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  const page = await context.newPage();
  try {
    await page.goto(`${stack.urls.site}#/review`);
    await page.locator("form.rv-unlock input[type=password]").fill(OWNER_TOKEN);
    await page.locator(".rv-unlock button[type=submit]").click();
    const status = page.locator('section[aria-label="Status"]');
    await status.waitFor({ timeout: 30_000 });
    const backend = page.locator("section[aria-labelledby=rv-backend]");
    return norm(`${await status.innerText()} ${(await backend.count()) ? await backend.innerText() : ""}`);
  } catch (e) {
    return `(the review page did not open: ${e.message.split("\n")[0]})`;
  } finally {
    await context.close();
  }
}

// ---------- what the backend did ----------

/** The add runs of a condition id, newest first (from the owner's overview). */
export async function addRuns(stack, cid) {
  const o = await stack.overview();
  return (o?.backend?.runs ?? []).filter((r) => r.kind === "add" && String(r.slot).startsWith(`${cid}:`));
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
