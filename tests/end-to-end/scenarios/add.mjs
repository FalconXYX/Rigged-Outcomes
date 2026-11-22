// "+ Add to main page" on Recently closed cards, every way a visitor's add can go, through the whole stack with
// real timing (tests/end-to-end/stack.mjs). Each scenario drives Chromium the way a person does (real clicks, a page
// left open where it matters, a reload) and lets the system do its own work: the click queues an add run in D1,
// the Worker wakes the local Vercel, its steps build the page and publish it from D1, and nothing is run by hand
// after a click. It records what the card reads at each moment and how long each stage took, and compares it with
// what the site promises: an add is on the main page within minutes by itself, nothing on screen hedges, and an
// error shows only when the person must act.
//
// The scenarios run in four groups, each on a stack of its own (a visitor may add 5 markets a day):
//
//   main      (base+0)   the page left open, then the market's page and the home page tile; the details panel's
//                        button and the card's agreeing; a reload mid-way; two adds a minute apart; the daily add
//                        limit ("Try again tomorrow.")
//   botfail   (base+4)   Turnstile's always-failing keys
//   faults    (base+8)   Vercel answering 503 DEPLOYMENT_PAUSED (then taking calls again); a lost wake-up; a step
//                        killed part way (every Vercel instance killed mid-add); an add that fails (its winner is
//                        not Polymarket's); the Worker not answering the browser at the click
//   daily     (base+12)  an add while the daily run is going (its heavy Open now walk holds the one heavy lease),
//                        and the daily run keeping the picks it could not check again
//
//   node tests/end-to-end/scenarios/add.mjs [--port-base 9520] [--only main,faults] [--wait MIN] [--shots DIR]
//                                           [--state DIR] [--headed] [--verbose] [--keep]
//
// Groups run one after another in one process (each stack stops only what it started); for groups side by side,
// run one process per group with --only. A wait for an add to land stops as soon as the outcome is clearly wrong
// (lib/outcome.mjs). Each group's stack removes its state folder when the group passed (--keep keeps it). Prints a
// table (scenario | expected | what happened | PASS/FAIL) and writes it as JSON next to the screenshots. Exit 0
// when every scenario passed.

import { readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { launch } from "../lib/browser.mjs";
import { addGoesWrong, showsUnless } from "../lib/outcome.mjs";
import { secs, sleep, until } from "../lib/proc.mjs";
import {
  Report,
  addButton,
  addRuns,
  callsLine,
  cardOf,
  faceOf,
  hedges,
  norm,
  onMainLink,
  openWatchlist,
  panelOf,
  request,
  runLine,
  shows,
  watch,
} from "../lib/scenario.mjs";
import { TERMINAL, parseArgs, stackOptions, startStack } from "../stack.mjs";

const args = parseArgs(process.argv.slice(2));
const BASE = Number(args["port-base"] ?? 9520);
const ONLY = args.only ? String(args.only).split(",") : null;
const STATE = args.state ?? join(tmpdir(), "rigged-e2e-add");
/** the longest an add may take to land before its scenario stops waiting (minutes) */
const WAIT_MS = Number(args.wait ?? 15) * 60_000;
/** an on-demand add is "live on the main page within minutes": this long */
const WITHIN_MS = 5 * 60_000;
const report = new Report("add", args.shots ?? join(tmpdir(), "e2e-shots", "add"), ONLY ? ONLY.join("-") : "all");
const say = (s) => report.say(s);

// ---------- one add, the page left open ----------

/**
 * Click "+ Add to main page" on `pick`'s card in `page` (opened by openWatchlist) and leave the page open until
 * the card reads "On the main page →", `ms` passes or the outcome is clearly wrong (addGoesWrong). Returns the
 * timings, why it stopped waiting when it never landed, and the watcher.
 */
async function addAndWait(stack, page, pick, tag, { ms = WAIT_MS, reads, before } = {}) {
  const at = Date.now();
  const w = watch(at, reads ?? { card: () => faceOf(page, pick) }, tag, say);
  let posted = null;
  page.on("response", (res) => {
    if (res.request().method() === "POST" && res.url().endsWith("/api/request") && !posted) posted = { at: Date.now(), status: res.status() };
  });
  await (before ? before() : addButton(page, pick).click());
  const got = await showsUnless(onMainLink(page, pick), ms, addGoesWrong(stack, cardOf(page, pick), at, pick.condition_id));
  const landed = got.shown;
  const t = { click: at, landed: landed ? Date.now() : null, posted };
  await sleep(1500);
  await w.stop();
  return { pick, t, w, landed, why: got.why };
}

/** The stages of an add from its click: the POST, its run and the card (skew: the Worker's clock minus the real one, ms). */
async function stages(stack, res, skew = 0) {
  const runs = await addRuns(stack, res.pick.condition_id);
  const s = (x) => (x ? `+${secs(res.t.click, x)} s` : "-");
  const lines = await Promise.all(runs.map((r) => runLine(stack, r.id, res.t.click + skew)));
  return (
    `POST ${res.t.posted ? `${res.t.posted.status} ${s(res.t.posted.at)}` : "-"}; ${lines.join(" | ") || "no add run"}; ` +
    `card "On the main page →" ${res.landed ? s(res.t.landed) : `never (stopped waiting: ${res.why})`}`
  );
}

const took = (res) => (res.landed ? res.t.landed - res.t.click : null);

// ---------- the groups ----------

async function group(name, offset, extra, picks, body) {
  if (ONLY && !ONLY.includes(name)) return;
  const opts = stackOptions({ "port-base": BASE + offset, picks, cards: 2, quiet: !args.verbose, keep: args.keep, ...extra });
  Object.assign(opts, extra.opts ?? {});
  opts.state = join(STATE, name);
  say(`${name}: starting a stack on ports ${opts.portBase}-${opts.portBase + 3}${extra["bot-fail"] ? " (failing bot check)" : ""}`);
  let stack;
  let browser;
  const from = report.rows.length;
  try {
    stack = await startStack(opts);
    say(`${name}: stack ready (${stack.seed.picks.length} pick(s): ${stack.seed.picks.map((p) => `${p.slug} ${p.day}`).join(", ")}; state ${stack.state})`);
    browser = await launch({ headed: !!args.headed });
    await body(stack, browser, stack.seed.picks);
  } catch (e) {
    report.row(`${name} (group)`, "the group runs", `stopped: ${e.stack ?? e.message}`.slice(0, 600), false);
  } finally {
    await browser?.close().catch(() => {});
    await stack?.stop({ keepState: report.failedSince(from) });
  }
}

/** The pick a scenario needs, or a row saying it could not run. */
function need(picks, n, scenario) {
  if (picks.length > n) return picks[n];
  report.skip(scenario, `the seed found only ${picks.length} closed pick(s)`);
  return null;
}

// main: the page left open, the panel, a reload, two adds, the limit, the market's page
await group("main", 0, {}, 6, async (stack, browser, picks) => {
  // 1. the page left open
  {
    const p = need(picks, 0, "page left open");
    if (p) {
      const { context, page } = await openWatchlist(browser, stack, p);
      let navs = 0;
      page.on("framenavigated", (f) => f === page.mainFrame() && navs++);
      const res = await addAndWait(stack, page, p, "open");
      const runs = await addRuns(stack, p.condition_id);
      const adding = res.w.first("card", /^Adding to the main page$/);
      const api = await request(stack, p);
      const href = res.landed ? await onMainLink(page, p).getAttribute("href") : null;
      const pass = res.landed && took(res) <= WITHIN_MS && adding !== null && adding < 5 && navs === 0 && !hedges(res.w).length && runs.length === 1 && runs[0].state === "done" && api.request?.status === "added";
      report.row(
        "page left open",
        `"Adding to the main page" at once, then "On the main page →" by itself within ${WITHIN_MS / 60_000} min; one add run, done; no site build`,
        `${res.w.seen("card")}. ${await stages(stack, res)}. API: ${api.request?.status} as ${api.request?.name}. ${await callsLine(stack, res.t.click)}. Site builds: ${stack.site.deploys.length}`,
        pass && stack.site.deploys.length === 1,
      );
      if (!pass) await report.shot(page, "open-page");

      // the market's own page and the home page tile, in the same tab, then a fresh tab
      if (res.landed) {
        const notes = [];
        let ok = true;
        await onMainLink(page, p).click();
        const title = await page.locator("h1.market-title").waitFor({ timeout: 30_000 }).then(() => page.locator("h1.market-title").innerText(), () => null);
        await sleep(3000);
        const charts = await page.locator("svg, canvas").count();
        notes.push(`link → ${page.url().split("#")[1]} "${title ?? "no title"}" (${charts} svg/canvas)`);
        ok &&= !!title && page.url().endsWith(href) && charts > 0;
        await page.evaluate(() => (window.location.hash = "#/"));
        const tile = page.locator(`a.mc-link[href="#/m/${api.request?.name}"]`);
        const tileShown = await shows(tile, 30_000);
        const tiles = await page.locator("a.mc-link").count();
        notes.push(`home tile in the same tab: ${tileShown ? "shown" : "missing"} (${tiles} tiles: the 7 original markets and the added one)`);
        ok &&= tileShown && tiles >= 8;
        if (tileShown) {
          await tile.click();
          const again = await shows(page.locator("h1.market-title"), 30_000);
          notes.push(`tile → market page: ${again ? "opened" : "did not open"}`);
          ok &&= again;
        }
        const fresh = await context.newPage();
        await fresh.goto(stack.urls.site);
        const listed = await shows(fresh.locator(`a.mc-link[href="#/m/${api.request?.name}"]`), 30_000);
        notes.push(`fresh tab home: ${listed ? "listed" : "not listed"}`);
        ok &&= listed;
        if (listed) {
          await fresh.locator(`a.mc-link[href="#/m/${api.request?.name}"]`).click();
          const opened = await shows(fresh.locator("h1.market-title"), 30_000);
          notes.push(`fresh tab tile → market page: ${opened ? "opened" : "did not open"}`);
          ok &&= opened;
        }
        notes.push(`page errors: ${page.errors.length ? page.errors.slice(0, 2).join(" | ") : "none"}`);
        ok &&= !page.errors.length;
        report.row("the added market's page and the home page tile", "the link opens its page; the home page (same tab and a fresh tab) lists it beside the 7 original markets and the tile opens it; no page errors", notes.join("; "), ok);
        if (!ok) await report.shot(page, "market-page");
      } else report.skip("the added market's page and the home page tile", "the market never landed");
      await context.close();
    }
  }

  // 2. the details panel's button and the card's agreeing
  {
    const p = need(picks, 1, "details panel and card face agree");
    if (p) {
      const { context, page } = await openWatchlist(browser, stack, p);
      await cardOf(page, p).locator("button.cl-card-more").click();
      const panelBtn = page.locator("button.cl-add-btn", { hasText: "Add to main page" });
      await panelBtn.waitFor({ timeout: 15_000 });
      const before = { card: await faceOf(page, p), panel: await panelOf(page) };
      const res = await addAndWait(stack, page, p, "panel", {
        reads: { card: () => faceOf(page, p), panel: () => panelOf(page) },
        before: () => panelBtn.click(),
      });
      const panelLink = page.locator("a.cl-add-btn", { hasText: "On the main page" });
      const panelLanded = await shows(panelLink, 90_000);
      const cardAdding = res.w.first("card", /^Adding to the main page$/);
      const panelAdding = res.w.first("panel", /^Adding to the main page$/);
      const cardOn = res.w.first("card", /On the main page/);
      const panelOn = res.w.first("panel", /On the main page/);
      const hrefs = res.landed && panelLanded ? [await onMainLink(page, p).getAttribute("href"), await panelLink.getAttribute("href")] : [];
      const agree = cardAdding !== null && panelAdding !== null && Math.abs(cardAdding - panelAdding) <= 2 && cardOn !== null && panelOn !== null && Math.abs(cardOn - panelOn) <= 2 && hrefs[0] === hrefs[1];
      const pass = agree && took(res) <= WITHIN_MS && !hedges(res.w).length;
      report.row(
        "details panel and card face agree",
        'a click in the panel turns both the panel and the card face to "Adding" together, then both to "On the main page →" (same link) by themselves',
        `before: card "${before.card}", panel "${before.panel}"; ${res.w.seen()}. Links: ${hrefs.join(" / ") || "-"}. ${await stages(stack, res)}`,
        pass,
      );
      if (!pass) await report.shot(page, "panel-card");
      await context.close();
    }
  }

  // 3. a reload mid-way
  {
    const p = need(picks, 2, "reload mid-way");
    if (p) {
      const { context, page } = await openWatchlist(browser, stack, p);
      let reloadedAt = null;
      let afterReload = null;
      const res = await addAndWait(stack, page, p, "reload", {
        before: async () => {
          // the first wake-up lost, so the run is still waiting at the reload (a small add lands in seconds)
          await stack.vercel.fault({ drop: 1 });
          await addButton(page, p).click();
          await cardOf(page, p).locator(".wl-act-state", { hasText: "Adding to the main page" }).waitFor({ timeout: 60_000 });
          // reload once its run is made and before it is done, as a person checking on it would
          await until(async () => (await addRuns(stack, p.condition_id)).length > 0, { ms: 60_000, every: 500, what: "the add's run" }).catch(() => null);
          await sleep(3000);
          reloadedAt = Date.now();
          await page.reload();
          await cardOf(page, p).locator(".cl-card-foot .wl-act").waitFor({ timeout: 30_000 });
          await sleep(1000);
          afterReload = await faceOf(page, p);
        },
      });
      const pass = afterReload === "Adding to the main page" && res.landed && took(res) <= WITHIN_MS && !hedges(res.w).length;
      report.row(
        "reload mid-way",
        'after a reload the card reads "Adding to the main page" (never "+ Add" again), then "On the main page →" by itself',
        `reloaded +${secs(res.t.click, reloadedAt)} s; 1 s after the reload: "${afterReload}". ${res.w.seen("card")}. ${await stages(stack, res)}`,
        pass,
      );
      if (!pass) await report.shot(page, "reload");
      await context.close();
    }
  }

  // 4. two adds a minute apart (two tabs, each left open)
  {
    const a = need(picks, 3, "two adds a minute apart");
    const b = a && need(picks, 4, "two adds a minute apart");
    if (a && b) {
      const A = await openWatchlist(browser, stack, a);
      const B = await openWatchlist(browser, stack, b);
      const first = addAndWait(stack, A.page, a, "two/1st");
      await sleep(60_000);
      const second = addAndWait(stack, B.page, b, "two/2nd");
      const [ra, rb] = await Promise.all([first, second]);
      const pass = ra.landed && rb.landed && took(ra) <= WITHIN_MS && took(rb) <= WITHIN_MS && !hedges(ra.w).length && !hedges(rb.w).length;
      report.row(
        "two adds a minute apart",
        `each card reads "On the main page →" by itself within ${WITHIN_MS / 60_000} min of its own click; one add run each`,
        `1st: ${await stages(stack, ra)}. 2nd (clicked +${secs(ra.t.click, rb.t.click)} s after the 1st): ${await stages(stack, rb)}. ${await callsLine(stack, ra.t.click)}`,
        pass,
      );
      if (!pass) {
        await report.shot(A.page, "two-adds-1st");
        await report.shot(B.page, "two-adds-2nd");
      }
      await A.context.close();
      await B.context.close();
    }
  }

  // 5. the daily add limit (this visitor's 6th add today)
  {
    const p = need(picks, 5, "daily add limit");
    if (p) {
      const { context, page } = await openWatchlist(browser, stack, p);
      const at = Date.now();
      const w = watch(at, { card: () => faceOf(page, p) }, "limit", say);
      await addButton(page, p).click();
      const alert = cardOf(page, p).locator(".wl-act-error[role=alert]");
      const shown = await shows(alert, 60_000);
      await sleep(20_000);
      await w.stop();
      const text = shown ? norm(await alert.innerText()) : null;
      const button = await addButton(page, p).count();
      const api = await request(stack, p);
      const runs = await addRuns(stack, p.condition_id);
      const pass = shown && /limit/i.test(text) && /Try again tomorrow\.$/.test(text) && !/UTC/.test(text) && button === 1 && !api.request && !runs.length;
      report.row(
        "daily add limit",
        'the 6th add of the day shows the limit message under the button ("Try again tomorrow.", no UTC); nothing recorded, no run',
        `${w.seen("card")}; alert ${shown ? `+${w.first("card", /limit/i)} s "${text}"` : "never shown"}; button still there: ${button === 1}; API request: ${api.request?.status ?? "none"}; add runs: ${runs.length}`,
        pass,
      );
      if (!pass) await report.shot(page, "limit");
      await context.close();
    }
  }
});

// botfail: Turnstile's always-failing keys
await group("botfail", 4, { "bot-fail": true }, 1, async (stack, browser, picks) => {
  const p = need(picks, 0, "bot check fails");
  if (!p) return;
  const { context, page } = await openWatchlist(browser, stack, p);
  const at = Date.now();
  const w = watch(at, { card: () => faceOf(page, p) }, "botfail", say);
  await addButton(page, p).click();
  const alert = cardOf(page, p).locator(".wl-act-error[role=alert]");
  const shown = await shows(alert, 90_000);
  const text = shown ? norm(await alert.innerText()) : null;
  // the person tries again, as the message says
  let again = null;
  if (await addButton(page, p).count()) {
    await addButton(page, p).click();
    await sleep(25_000);
    again = await faceOf(page, p);
  }
  await sleep(5000);
  await w.stop();
  const api = await request(stack, p);
  const runs = await addRuns(stack, p.condition_id);
  const calls = (await stack.vercel.calls()).filter((c) => c.at * 1000 >= at && c.lane === "add");
  const pass = shown && /bot check/i.test(text) && !api.request && !runs.length && !calls.length;
  report.row(
    "bot check fails",
    "an error the person can act on (try again) under the button; no add recorded, no run, Vercel never woken",
    `${w.seen("card")}; alert ${shown ? `+${w.first("card", /bot check|Try again|Couldn't/i)} s "${text}"` : "never shown"}; after a second click: "${again}"; API request: ${api.request?.status ?? "none"}; add runs: ${runs.length}; add-lane Vercel calls: ${calls.length}`,
    pass,
  );
  if (!pass) await report.shot(page, "bot-fail");
  await context.close();
});

// faults: Vercel paused, a lost wake-up, a step killed part way, an add that fails, the Worker down at the click
await group("faults", 8, { big: 1 }, 5, async (stack, browser, all) => {
  const picks = all.filter((p) => !p.big);
  const bigPick = all.find((p) => p.big);
  // 1. Vercel answers 503 DEPLOYMENT_PAUSED for a while, then takes calls again
  {
    const p = need(picks, 0, "Vercel paused (503 DEPLOYMENT_PAUSED)");
    if (p) {
      const { context, page } = await openWatchlist(browser, stack, p);
      await stack.vercel.fault({ status: 503, error: "DEPLOYMENT_PAUSED" });
      let during = null;
      let lane = null;
      let clearedAt = null;
      const res = await addAndWait(stack, page, p, "paused", {
        before: async () => {
          await addButton(page, p).click();
          await sleep(90_000); // a tick or two: the wake-up refused, the lane backing off
          during = await faceOf(page, p);
          lane = (await stack.status()).lanes?.find((l) => l.lane === "add");
          await stack.vercel.fault({});
          clearedAt = Date.now();
        },
      });
      const after = res.landed ? res.t.landed - clearedAt : null;
      const pass = during === "Adding to the main page" && lane?.wake_error === "HTTP 503" && res.landed && after <= WITHIN_MS && !hedges(res.w).length;
      report.row(
        "Vercel paused (503 DEPLOYMENT_PAUSED)",
        `while Vercel refuses, the card keeps reading "Adding to the main page" (no error: the visitor can do nothing), the add lane records "HTTP 503"; once Vercel takes calls the add lands by itself within ${WITHIN_MS / 60_000} min`,
        `90 s after the click: "${during}"; add lane: unanswered ${lane?.unanswered}, wake_error ${lane?.wake_error}. Fault cleared +${secs(res.t.click, clearedAt)} s; landed ${after !== null ? `${Math.round(after / 1000)} s after that` : "never"}. ${await stages(stack, res)}. ${await callsLine(stack, res.t.click, "add")}`,
        pass,
      );
      if (!pass) await report.shot(page, "paused");
      await context.close();
    }
  }

  // 2. a lost wake-up: the Worker's wake-up is taken and nothing runs
  {
    const p = need(picks, 1, "a lost wake-up");
    if (p) {
      const { context, page } = await openWatchlist(browser, stack, p);
      await stack.vercel.fault({ drop: 1 });
      const res = await addAndWait(stack, page, p, "lost");
      const fault = (await stack.vercel.state()).fault;
      const pass = res.landed && took(res) <= WITHIN_MS && !hedges(res.w).length && !Object.keys(fault).length;
      report.row(
        "a lost wake-up",
        `the first wake-up is lost (held unanswered, nothing runs); the cron's watchdog wakes the lane again after its back-off and the card reads "On the main page →" by itself within ${WITHIN_MS / 60_000} min`,
        `${res.w.seen("card")}. ${await stages(stack, res)}. ${await callsLine(stack, res.t.click, "add")}`,
        pass,
      );
      if (!pass) await report.shot(page, "lost-wake");
      await context.close();
    }
  }

  // 3. a step killed part way: every Vercel instance killed while the add's step runs (a market large enough
  // that its fills and whales take a while)
  {
    const p = bigPick ?? need(picks, 2, "a step killed part way");
    if (p) {
      const { context, page } = await openWatchlist(browser, stack, p);
      let killed = null;
      const res = await addAndWait(stack, page, p, "killed", {
        before: async () => {
          await addButton(page, p).click();
          const leased = await until(
            async () => {
              const r = (await addRuns(stack, p.condition_id))[0];
              const got = r && (await stack.run(r.id));
              return got?.jobs.find((j) => j.state === "leased") ?? null;
            },
            { ms: 120_000, every: 250, what: "the add's step to run" },
          ).catch(() => null);
          killed = { job: leased?.kind, ...(await stack.vercel.kill()) };
          // the killed step's lease runs out (it was left_s + 30 s): time passes on the Worker's clock
          const now = await stack.workerNow();
          await stack.clock(now + 330);
          await stack.tick();
        },
      });
      const r = (await addRuns(stack, p.condition_id))[0];
      const jobs = r ? (await stack.run(r.id)).jobs : [];
      const retried = jobs.filter((j) => j.attempt > 0);
      await stack.clock(null);
      const pass = !!killed?.job && res.landed && !hedges(res.w).length && retried.length > 0 && jobs.every((j) => j.state === "done");
      report.row(
        "a step killed part way",
        'the step is killed mid-way (its Vercel instance gone); once its lease runs out the watchdog queues the job again, it resumes from its last checkpoint and the card reads "On the main page →" by itself',
        `killed during ${killed?.job ?? "no step"} (${killed?.killed ?? 0} instance(s), ${killed?.calls ?? 0} call(s)); jobs retried: ${retried.map((j) => `${j.kind} attempt ${j.attempt}`).join(", ") || "none"}. ${res.w.seen("card")}. ${await stages(stack, res)}. ${await callsLine(stack, res.t.click, "add")}`,
        pass,
      );
      if (!pass) await report.shot(page, "killed");
      await context.close();
    }
  }

  // 4. an add that fails: its winner is not Polymarket's
  {
    const f = need(picks, 3, "an add that fails");
    if (f) {
      const ws = stack.seed.ws;
      const bent = join(stack.state, "closed-bent.json");
      const doc = JSON.parse(readFileSync(join(ws, "web", "public", "data", "closed.json"), "utf8"));
      for (const d of doc.days) for (const x of d.picks) if (x.condition_id === f.condition_id) x.winner = "Nobody (e2e)";
      writeFileSync(bent, JSON.stringify(doc));
      const pushed = await stack.python(["-m", "watchlist.sync", "catalog", "--closed", bent], { ws });
      if (pushed.code !== 0) throw new Error(`the bent catalog was not pushed:\n${pushed.out.slice(-800)}`);
      const { context, page } = await openWatchlist(browser, stack, f);
      const at = Date.now();
      const w = watch(at, { card: () => faceOf(page, f) }, "failed", say);
      await addButton(page, f).click();
      const r = await until(async () => (await addRuns(stack, f.condition_id))[0]?.finished_at ?? null, { ms: WAIT_MS, every: 2000, what: "the add's run to end" }).catch(() => null);
      await sleep(60_000); // the open page's next asks
      await w.stop();
      const api = await request(stack, f);
      const final = await faceOf(page, f);
      const pass = !!r && api.request?.status === "failed" && !/^Adding/.test(final) && !hedges(w).length;
      report.row(
        "an add that fails",
        'the card stops reading "Adding" once the run reports it failed and shows what the person can do; never "Adding" forever',
        `${w.seen("card")}; a minute after its run ended: "${final}". API: ${api.request?.status} (${api.request?.note ?? "no note"}). ${await runLine(stack, (await addRuns(stack, f.condition_id))[0]?.id, at)}`,
        pass,
      );
      if (!pass) await report.shot(page, "add-failed");
      await context.close();
    }
  }

  // 5. the Worker not answering the browser when the click is made (it is back a minute later)
  {
    const d = need(picks, 4, "Worker down at the click");
    if (d) {
      const { context, page } = await openWatchlist(browser, stack, d);
      const api = new URL(stack.urls.api).origin;
      let blocked = 0;
      const down = (route) => {
        blocked++;
        return route.abort("connectionrefused");
      };
      let upAt = null;
      const res = await addAndWait(stack, page, d, "down", {
        before: async () => {
          await page.route(`${api}/**`, down);
          await addButton(page, d).click();
          await sleep(60_000);
          await page.unroute(`${api}/**`, down);
          upAt = Date.now();
        },
      });
      const keptAt = res.w.first("card", /^Adding to the main page$/);
      const pass = keptAt !== null && keptAt < 5 && res.landed && took(res) <= WITHIN_MS + 60_000 && !hedges(res.w).length;
      report.row(
        "Worker down at the click",
        'the card reads "Adding" at once (the click kept in the browser), the click is sent again once the API answers, and it lands by itself',
        `${res.w.seen("card")}; ${blocked} API call(s) failed while it was down; API back +${secs(res.t.click, upAt)} s; the re-sent POST answered ${res.t.posted ? `${res.t.posted.status} +${secs(res.t.click, res.t.posted.at)} s` : "never"}. ${await stages(stack, res)}`,
        pass,
      );
      if (!pass) await report.shot(page, "worker-down");
      await context.close();
    }
  }
});

// daily: an add while the daily run goes; the picks it could not check again
await group("daily", 12, { daily: true, opts: { cron: false } }, 4, async (stack, browser, picks) => {
  const a = need(picks, 0, "add while the daily run is going");
  if (!a) return;
  // the picks whose saved checks are taken out: the run must check them again and cannot (the model is stubbed)
  const unchecked = picks.slice(1, 3);
  const forgotten = await forgetChecks(stack, unchecked);
  const seeded = await fetch(`${stack.urls.api}/api/data/closed.json`).then((r) => r.json());
  const A = await openWatchlist(browser, stack, a);
  // the day's daily run, made now at any hour (the stack let today's run start; before 04:00 Toronto the Worker's
  // clock is moved there first, so its times are `skew` ms ahead of the real ones)
  const { id: daily, skew } = await stack.dailyRun();
  stack.cron.start();
  await stack.tick();
  // asked every second (the walk's lease can be short); a run that ends first stops the wait there
  const walking = (jobs) => jobs.some((j) => j.kind === "open.walk" && j.state === "leased");
  const walk = await stack
    .waitRun(daily, { ms: 10 * 60_000, every: 1000, pred: (run, jobs) => walking(jobs) || TERMINAL.includes(run.state) })
    .then((got) => (walking(got.jobs) ? got : null))
    .catch(() => null);
  if (!walk) {
    report.skip("add while the daily run is going", `the daily run's Open now walk did not start (${await runLine(stack, daily, Date.now() + skew)})`);
    return;
  }
  const res = await addAndWait(stack, A.page, a, "daily/walk");
  report.row(
    "add while the daily run is going",
    `"On the main page →" by itself within ${WITHIN_MS / 60_000} min of the click (an add does not wait for the daily lists; it waits at most for the heavy walk's lease)`,
    `${res.w.seen("card")}. ${await stages(stack, res, skew)}. The daily run meanwhile: ${await runLine(stack, daily, res.t.click + skew)}`,
    res.landed && took(res) <= WITHIN_MS && !hedges(res.w).length,
  );
  if (!res.landed) await report.shot(A.page, "daily-add");
  await A.context.close();

  // the daily run to its end (it took about 6 min in watch and score on 2026-11-21), then the picks it could not
  // check again on the list it published
  let why = "";
  const end = await stack.waitRun(daily, { ms: 30 * 60_000 }).catch((e) => ((why = e.message), null));
  const published = await fetch(`${stack.urls.api}/api/data/closed.json`).then((r) => r.json(), () => ({}));
  const pickIn = (doc, p) => (doc.days ?? []).find((d) => d.day === p.day)?.picks?.find((x) => x.condition_id === p.condition_id);
  const kept = unchecked.map((p) => ({ p, was: pickIn(seeded, p), now: pickIn(published, p) }));
  const same = kept.length > 0 && kept.every((k) => k.was && k.now && JSON.stringify(k.now) === JSON.stringify(k.was));
  const over = (published.days ?? []).filter((d) => (d.picks ?? []).length > 3).map((d) => `${d.day}: ${d.picks.length}`);
  report.row(
    "the daily run keeps the picks it could not check again",
    "a pick whose check the daily run could not make again (its model did not answer) stays on the list it published, as it was; no day has more than 3",
    `${forgotten} saved check(s) taken out before the run (${unchecked.map((p) => p.slug).join(", ")}); ${end ? await runLine(stack, daily, Date.now() + skew) : `the daily run did not end (${why})`}; ` +
      `on the published list: ${kept.map((k) => `${k.p.slug} ${k.now ? (JSON.stringify(k.now) === JSON.stringify(k.was) ? "as it was" : `changed (verdict ${k.now.verdict})`) : "missing"}`).join(", ")}; days over 3: ${over.join(", ") || "none"}`,
    !!end && forgotten === unchecked.length && same && !over.length,
  );
});

/**
 * The saved closed-list checks of `picks` taken out of the backend's state in the stack's database (pulled into a
 * workspace, deleted from its store, pushed back), so the next daily run must check those markets again; returns
 * how many were taken out.
 */
async function forgetChecks(stack, picks) {
  const step = async (argv, ws) => {
    const res = await stack.python(argv, ws ? { ws } : {});
    if (res.code !== 0) throw new Error(`${argv.slice(0, 3).join(" ")} failed:\n${res.out.slice(-1500)}`);
    return res;
  };
  const { ws } = await step(["-m", "watchlist.state", "pull"]);
  const code = [
    "import sys",
    "from watchlist import store",
    "conn = store.default_conn()",
    "n = conn.execute('DELETE FROM closed_checks WHERE condition_id IN (%s)' % ','.join('?' * (len(sys.argv) - 1)), sys.argv[1:]).rowcount",
    "conn.commit()",
    "print(n)",
  ].join("\n");
  const gone = await step(["-c", code, ...picks.map((p) => p.condition_id)], ws);
  await step(["-m", "watchlist.state", "push", "--by", "local"], ws);
  return Number(gone.out.trim().split("\n").pop());
}

report.finish(ONLY ? ONLY.join("-") : "all");
