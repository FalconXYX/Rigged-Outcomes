// The wallet rubric's insider score after "+ Add to main page", through the whole stack with real timing
// (tests/end-to-end/stack.mjs with scores on: the step function has WALLET_SCORES=on, and py/sitecustomize.py's
// Decisions API stand-in answers as its mode says). A person adds a closed market from the Watchlist, follows
// "On the main page →" to its page and keeps it open: add.export reports the market added and spawns
// wallets.score, which walks the fills, reads each whale's history from Polymarket's Data API, asks the rubric's
// questions and publishes the page again with the scores; the page should then show the "Insider score" tab.
// Nothing is run by hand after a click. The cron, the wake-ups and the lease watchdog do the work; the Worker's
// clock is moved only where a group says so (quota: past the quota's reset; daily: to the next 04:00 Toronto).
// Every stage's real duration goes into the results.
//
//   main         (base+0)   a large market (the seed's big pick): the add lands, and counts as added before its
//                           scoring starts; its page, left open, shows the tab by itself, and after a reload; the
//                           tab's band legend and counts against the page the Worker publishes, the strip,
//                           constellation and treemap, a pinned wallet's card and its question rows, the rubric's
//                           paragraph at the foot; Flagged traders and the home card as before scoring; 390 px and
//                           dark; no hedging words, no console errors; each whale asked once
//   concurrency  (base+4)   two adds a few seconds apart, then an add made while another market's wallets.score
//                           runs: all land within minutes and all are scored; neither later add waits for the other
//                           market's scoring
//   quota        (base+8)   the Decisions API's daily quota spent (the fallback's too): the add lands; wallets.score
//                           waits for the quota's reset (the page keeps its two tabs meanwhile); an add made while
//                           it waits lands as usual; the clock moved past the reset: both are scored and the open
//                           page shows the tab
//   busy         (base+12)  the Decisions API busy: wallets.score comes back by itself 5 to 10 minutes later and
//                           publishes; the adder's open page, its page opened again, another visitor's open page and
//                           a background tab coming back into view all show the tab without a reload
//   killed       (base+16)  every Vercel instance killed mid-scoring: the lease runs out, the watchdog queues the
//                           step again, it resumes from its checkpoints (no whale asked twice) and the tab appears
//   fallback     (base+20)  the Decisions API out for the day, the regular-Gemini fallback answering: the tab
//                           appears with the fallback's scores
//   daily        (base+24)  scores on through the daily run (the clock moved to the next 04:00 Toronto): a watched
//                           market the run adds is scored and shows the tab; a visitor's add made during the run
//                           lands within minutes; the day's lists still publish; what the scoring adds to each
//   large        (base+28)  the seed's largest market: the Decisions pacer's token window fills and wallets.score
//                           spans more than one Vercel call; no whale asked twice; per-whale rates and what 100 and
//                           600 whales would take
//   faults       (base+32)  Polymarket down during wallets.score (it comes back by itself), the Worker down while
//                           it asks (the lease runs out, it resumes), one wallet's request refused (that wallet
//                           stays unscored, the rest show), the open page showing the tab after all of it
//
//   node tests/end-to-end/scenarios/score.mjs [--port-base 9740] [--only main,busy] [--wait MIN] [--shots DIR]
//                                             [--state DIR] [--headed] [--verbose] [--keep]
//
// Groups run one after another in one process; for groups side by side, one process per group with --only (each
// on its own ports). A group's stack removes its state folder when the group passed (--keep keeps it). Prints a
// table (scenario | expected | what happened | PASS/FAIL/NOT RUN) and the stages' durations, and writes both as
// JSON next to the screenshots. Exit 0 when every scenario passed.

import { readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { launch } from "../lib/browser.mjs";
import { addGoesWrong, showsUnless } from "../lib/outcome.mjs";
import { secs, sleep, until } from "../lib/proc.mjs";
import { HEDGE, Report, addButton, callsLine, cardOf, faceOf, hedges, norm, onMainLink, request, runLine, shows, watch } from "../lib/scenario.mjs";
import { dayShift, torontoAt } from "../lib/toronto.mjs";
import { TERMINAL, parseArgs, stackOptions, startStack } from "../stack.mjs";

const args = parseArgs(process.argv.slice(2));
const BASE = Number(args["port-base"] ?? 9740);
const ONLY = args.only ? String(args.only).split(",") : null;
const STATE = args.state ?? join(tmpdir(), "rigged-e2e-score");
/** the longest wait for an add to land or a market to be scored (minutes) */
const WAIT_MS = Number(args.wait ?? 25) * 60_000;
/** an on-demand add is "live on the main page within minutes": this long */
const WITHIN_MS = 5 * 60_000;
/** once the scores are published, the adder's open page shows the tab within this (it reads the index every 30 s after its own add) */
const SHOWS_MS = 3 * 60_000;
/** any other page in view reads the index every 5 minutes (web/src/lib/data.ts REFRESH_MS) */
const REFRESH_MS = 5 * 60_000;
/** the seed's picks for these groups: at least this much traded, so a market has several whales */
const MID = 25_000;
/** the fewest whales a group's market needs for what the group claims (fewer: a NOT RUN row naming the count) */
const LEAST = { main: 10, scoring: 10, killed: 5, faults: 5 };
/** seconds a Decisions request took live once the rubric's token window bound (540 requests in 1,079 s) */
const LIVE_Q_S = 2.0;
const report = new Report("score", args.shots ?? join(tmpdir(), "e2e-shots", "score"), ONLY ? ONLY.join("-") : "all");
const say = (s) => report.say(s);
/** every stage's duration, for the summary: [group, what, seconds] */
const TIMES = [];
const time = (g, what, s) => {
  if (s !== null && s !== undefined) TIMES.push([g, what, s]);
};

// ---------- what the person sees ----------

/** Errors a page throws or logs (collected from before its first load); `quiet` leaves out failed loads. */
function listen(page) {
  page.errors = [];
  page.loads = 0;
  page.on("pageerror", (e) => page.errors.push(`pageerror: ${e}`));
  page.on("console", (m) => m.type() === "error" && page.errors.push(`console: ${m.text()}`));
  page.on("load", () => page.loads++);
  return page;
}

/** A page's errors from `from` on, without the browser's own lines for a request that got no answer (the API down). */
const errorsOf = (page, from = 0, { unanswered = false } = {}) =>
  page.errors.slice(from).filter((e) => !unanswered || !/Failed to load resource|net::ERR_/.test(e));

async function newPage(context) {
  return listen(await context.newPage());
}

/** A fresh browser context on the Watchlist's day for `pick`, its card's action shown. */
async function openWatchlist(browser, stack, pick) {
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  const page = await newPage(context);
  await page.goto(`${stack.urls.site}#/watchlist?day=${pick.day}`);
  await cardOf(page, pick).waitFor({ timeout: 60_000 });
  await cardOf(page, pick).locator(".cl-card-foot .wl-act").waitFor({ timeout: 30_000 });
  await sleep(1500); // the card's first ask of the API
  return { context, page };
}

/** A market's page in a new tab of `context`, its title shown. */
async function marketPage(context, stack, name) {
  const page = await newPage(context);
  await page.goto(`${stack.urls.site}#/m/${encodeURIComponent(name)}`);
  await page.locator("h1.market-title").waitFor({ timeout: 30_000 });
  return page;
}

/** The page as a background tab (hidden: true) or back in view, the way the browser tells it (visibilitychange). */
const setHidden = (page, hidden) =>
  page.evaluate((h) => {
    Object.defineProperty(document, "hidden", { configurable: true, get: () => h });
    Object.defineProperty(document, "visibilityState", { configurable: true, get: () => (h ? "hidden" : "visible") });
    document.dispatchEvent(new Event("visibilitychange"));
  }, hidden);

/** The market page's view tabs in one line ("Timeline | Flagged traders 3 | Insider score 2"). */
const tabsOf = (page) =>
  page.locator("nav.view-tabs a.view-tab").evaluateAll((as) =>
    as
      .map((a) => {
        const label = [...a.childNodes].filter((n) => n.nodeType === 3).map((n) => n.textContent).join("").trim();
        const count = a.querySelector(".tab-count")?.textContent.trim();
        return count ? `${label} ${count}` : label;
      })
      .join(" | "),
  );
const scoreTab = (page, label) => page.locator("nav.view-tabs a.view-tab", { hasText: label });
/** A legend's items as "<label> <count>" (the label's own text, then its count). */
const legendOf = (page, sel) =>
  page.locator(sel).evaluateAll((bs) =>
    bs.map((b) => {
      const label = [...b.childNodes].filter((n) => n.nodeType === 3).map((n) => n.textContent).join("").trim();
      return `${label} ${b.querySelector(".tr-legend-count")?.textContent.trim() ?? ""}`.trim();
    }),
  );
const tradersTab = (page) => page.locator("nav.view-tabs a.view-tab", { hasText: "Flagged traders" });
const twoTabs = (t, label) => /^Timeline \| Flagged traders( [\d,]+)?$/.test(t) && !t.includes(label);

/** What the Flagged traders tab shows: its four categories with counts, its stat tiles and its tab. */
async function flaggedView(page) {
  const sel = "ul.tr-legend[aria-label^='Risk categories'] button.legend-item";
  await page.locator(sel).first().waitFor({ timeout: 30_000 });
  await sleep(500);
  return {
    legend: await legendOf(page, sel),
    tiles: (await page.locator(".traders .stat-strip .stat").allInnerTexts()).map(norm),
    tab: norm(await tabsOf(page)).split(" | ").find((t) => t.startsWith("Flagged traders")) ?? "(none)",
  };
}

/** The market's card on the home page, in a new tab of `context`: its text and its flag bar. */
async function homeCard(context, stack, name) {
  const page = await newPage(context);
  try {
    await page.goto(stack.urls.site);
    const link = page.locator(`a.mc-link[href="#/m/${name}"]`);
    await link.waitFor({ timeout: 30_000 });
    const card = page.locator("article.market-card", { has: link });
    await sleep(1000);
    return {
      text: norm(await card.innerText()),
      bar: await card.locator(".fm-seg").evaluateAll((els) => els.map((e) => e.getAttribute("style"))),
      errors: page.errors,
    };
  } finally {
    await page.close();
  }
}

/** Errors shown on the page itself (an error note or an alert). */
const alertsOn = async (page) => (await page.locator(".error-note, [role=alert]").allInnerTexts().catch(() => [])).map(norm);

/** The rubric's name in the snapshot the stack runs (the tab's label). */
function rubricName(stack) {
  const text = readFileSync(join(stack.dirs.snap, "rubric", "wallet_rubric.toml"), "utf8");
  return text.match(/^name\s*=\s*"([^"]+)"/m)?.[1] ?? "Insider score";
}

const straight = (s) => norm(s).replace(/[‘’]/g, "'").replace(/[“”]/g, '"');
/** The same, lowercased (headings and tiles are uppercased by CSS in innerText). */
const plainText = (s) => straight(s).toLowerCase();

/** Hedging words in a view's text once the rubric's own wording (labels, levels, the paragraph) is taken out. */
function hedgesIn(text, rubric) {
  const own = [
    ...(rubric?.own ?? []),
    rubric?.name,
    rubric?.about?.heading,
    rubric?.about?.text,
    ...(rubric?.bands ?? []).map((b) => b.label),
    ...(rubric?.questions ?? []).flatMap((q) => [q.label, ...(q.levels ?? []), ...(q.options ?? []).map((o) => o.label)]),
    ...(rubric?.factors ?? []).flatMap((f) => [f.label, f.description]),
  ]
    .filter(Boolean)
    .map(plainText)
    .sort((a, b) => b.length - a.length);
  let t = ` ${plainText(text)} `;
  for (const s of own) t = t.split(s).join(" | ");
  return [...new Set((t.match(new RegExp(HEDGE.source, "gi")) ?? []).map((m) => m.toLowerCase()))];
}

/** A disclaimer in the rubric's own paragraph (the timing words of HEDGE are its subject there, so not these). */
const DISCLAIM = /not proof|\bbeta\b|\btrial\b|caveat|disclaimer|\bmay be\b|not available|paused|estimate/gi;

// ---------- what the backend did ----------

/** The market's page as the Worker publishes it now, or null. */
const marketData = (stack, name) =>
  fetch(`${stack.urls.api}/api/data/markets/${encodeURIComponent(name)}.json`, { cache: "no-store" }).then((r) => (r.ok ? r.json() : null), () => null);

/** Scored whales of a page and its rubric's band counts (as the tab computes them from each score). */
function scoredOf(data) {
  const rubric = data?.traders?.rubric ?? null;
  const scored = (data?.traders?.traders ?? []).filter((t) => t.spectrum && Number.isFinite(t.spectrum.score));
  if (!rubric) return { rubric, scored, bands: [], flagged: null, traders: data?.traders?.traders?.length ?? 0 };
  const bandOf = (s) => rubric.bands.reduce((b, x) => (s >= x.min ? x : b), rubric.bands[0]);
  const any = rubric.bands.some((b) => b.flagged);
  const top = rubric.bands[rubric.bands.length - 1]?.id;
  const bands = rubric.bands.map((b) => ({ label: b.label, n: scored.filter((t) => bandOf(t.spectrum.score).id === b.id).length }));
  const flagged = scored.filter((t) => (any ? bandOf(t.spectrum.score).flagged : bandOf(t.spectrum.score).id === top)).length;
  return { rubric, scored, bands, flagged, traders: data.traders.traders.length };
}

const scoreJob = (jobs) => (jobs ?? []).find((j) => j.kind === "wallets.score") ?? null;

/** The stand-in's calls of `kind` since `t0` (ms). */
const callsOf = (stack, kind, t0 = 0) => stack.geminiCalls().filter((c) => c.kind === kind && c.at * 1000 >= t0);
const decisionsSince = (stack, t0) => callsOf(stack, "decisions", t0);
/** How many answered inputs were asked more than once. */
const askedTwice = (calls) => {
  const ok = calls.filter((c) => c.status === 200);
  return ok.length - new Set(ok.map((c) => c.input)).size;
};

/** The step log's outcome lines for the market's wallets.score (in order): "later (model_quota)", "partial", ... */
function stepLines(stack, runId) {
  const text = readFileSync(join(stack.dirs.logs, "vercel.log"), "utf8");
  const id = `${runId}|wallets.score|0: `;
  return text
    .split("\n")
    .filter((l) => l.includes(id))
    .map((l) => l.slice(l.indexOf(id) + id.length).replace(/, [\d.]+ s.*$/, ""));
}

/**
 * When Gemini's daily quota is next fresh, as backend/contract.py's next_quota_reset has it: the next midnight in
 * Los Angeles, plus quota_reset_delay_s (300), in epoch seconds.
 */
function nextQuotaReset(epoch) {
  const fmt = new Intl.DateTimeFormat("en-US", { timeZone: "America/Los_Angeles", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", hourCycle: "h23" });
  const parts = (t) => Object.fromEntries(fmt.formatToParts(new Date(t * 1000)).map((p) => [p.type, p.value]));
  const p = parts(epoch);
  for (const off of [7, 8]) {
    const t = Date.UTC(Number(p.year), Number(p.month) - 1, Number(p.day) + 1, off) / 1000;
    if (parts(t).hour === "00") return t + 300;
  }
  return null;
}

/** Every backend tracker still polling (a group's end stops them, whatever happened). */
const TRACKERS = new Set();

/**
 * Every second, what the backend did for the add of `cid`: the first time each job reached each state, attempt,
 * later and partial; the run (its Worker times in real ms: created, started); the request (when the Worker marked
 * it added); and the market's index entry (when it was listed, when its scores were).
 */
function track(stack, cid) {
  const t = { first: {}, jobs: [], run: null, entry: null, name: null, req: null, created: null, started: null, addedAt: null };
  let on = true;
  const loop = (async () => {
    while (on) {
      try {
        const o = await stack.status();
        const now = Date.now();
        const skew = o.now * 1000 - now; // the Worker's clock minus the real one (moved in quota and daily)
        const req = (o.requests ?? []).find((q) => q.condition_id === cid) ?? null;
        if (req) {
          t.req = req;
          if (req.status === "added") {
            t.first.added ??= now;
            if (req.done_at) t.addedAt ??= req.done_at * 1000 - skew;
          }
        }
        const id = req?.run_id ?? (o.runs ?? []).find((r) => r.kind === "add" && String(r.slot).startsWith(`${cid}:`))?.id ?? null;
        const got = id ? await stack.run(id) : null;
        if (got) {
          t.run = got.run;
          t.jobs = got.jobs;
          if (got.run.created_at) t.created ??= got.run.created_at * 1000 - skew;
          if (got.run.started_at) t.started ??= got.run.started_at * 1000 - skew;
          for (const j of got.jobs) {
            const k = `${j.kind}#${j.part}`;
            t.first[`${k} ${j.state}`] ??= now;
            if (j.attempt) t.first[`${k} attempt ${j.attempt}`] ??= now;
            if (j.laters) t.first[`${k} later ${j.laters}`] ??= now;
            if (j.partials) t.first[`${k} partial ${j.partials}`] ??= now;
            if (j.state === "leased" && (j.attempt || j.laters)) t.first[`${k} leased again`] ??= now;
          }
          t.first[`run ${got.run.state}`] ??= now;
          t.name ??= got.run.meta?.name ?? null;
        }
        if (t.name) {
          const idx = await fetch(`${stack.urls.api}/api/data/index.json`, { cache: "no-store" }).then((r) => r.json());
          const e = (idx.markets ?? []).find((m) => m.name === t.name) ?? null;
          if (e) {
            t.first.listed ??= now;
            if (e.spectrum?.bands?.length) t.first["scores published"] ??= now;
            t.entry = e;
          }
        }
      } catch {
        /* the next second */
      }
      await sleep(1000);
    }
  })();
  const handle = {
    t,
    stop: async () => {
      on = false;
      TRACKERS.delete(handle);
      await loop;
    },
  };
  TRACKERS.add(handle);
  return handle;
}

/** A NOT RUN row: what the scenario would check, and why it could not (counts as failed). */
const notRun = (scenario, expected, why) => report.row(scenario, expected, `could not run: ${why}`, false, { skipped: true });

/**
 * When the market's wallets.score came back after `t0` (real ms): the first of its lease seen again, an add-lane
 * Vercel call that ran a job, or a Decisions request (each is seen whatever the job's length; a 1 s poll of the lease
 * alone can miss a short one).
 */
async function cameBack(stack, tr, t0) {
  const seen = tr.t.first["wallets.score#0 leased again"];
  const call = (await stack.vercel.calls().catch(() => [])).find((c) => c.lane === "add" && c.jobs > 0 && c.at * 1000 > t0);
  const dec = decisionsSince(stack, t0)[0];
  const read = polymarketBetween(stack, t0)[0];
  const xs = [seen, call ? call.at * 1000 : null, dec ? dec.at * 1000 : null, read ? read.at * 1000 : null].filter(Boolean);
  return xs.length ? Math.min(...xs) : null;
}

/** The step function's Polymarket requests between `t0` and `t1` (real ms). */
const polymarketBetween = (stack, t0, t1 = Infinity) => stack.polymarketCalls().filter((c) => c.at * 1000 > t0 && c.at * 1000 < t1);

/** The add's run ended with no wallets.score job: nothing will score the market. */
const neverSpawned = (tr) => !!tr.t.run && TERMINAL.includes(tr.t.run.state) && !scoreJob(tr.t.jobs);

/** Waits until the market's wallets.score job is terminal (or `pred(job)`); returns the job or null (also when none was spawned). */
const scoreEnds = (tr, ms = WAIT_MS, pred = (j) => ["done", "failed", "cancelled"].includes(j.state)) =>
  until(
    () => {
      const j = scoreJob(tr.t.jobs);
      if (j && pred(j)) return j;
      return neverSpawned(tr) ? { never: true } : null;
    },
    { ms, every: 1000, what: "wallets.score" },
  ).then((x) => (x?.never ? null : x), () => null);

/** Why a wait for the scores stopped short, in words (or ""). */
const whyNot = (tr) => (neverSpawned(tr) ? `wallets.score never spawned (the add's run is ${tr.t.run.state})` : "");

/**
 * Waits on an open page for the score tab: shown, or not `within` ms after the scores were published, or
 * wallets.score failed or never spawned, or `ms` passed. {at} or {at: null, why}.
 */
async function tabAppears(page, label, tr, ms = WAIT_MS, within = SHOWS_MS) {
  const end = Date.now() + ms;
  for (;;) {
    if (await scoreTab(page, label).count().catch(() => 0)) return { at: Date.now() };
    const pub = tr.t.first["scores published"];
    if (pub && Date.now() - pub >= within) return { at: null, why: `not within ${Math.round(within / 1000)} s of the scores being published` };
    const j = scoreJob(tr.t.jobs);
    if (j && ["failed", "cancelled"].includes(j.state)) return { at: null, why: `wallets.score ${j.state}: ${j.error}` };
    if (j?.state === "done" && !pub && Date.now() - tr.t.first["wallets.score#0 done"] >= SHOWS_MS) return { at: null, why: "wallets.score ended without publishing scores" };
    if (neverSpawned(tr)) return { at: null, why: whyNot(tr) };
    if (Date.now() > end) return { at: null, why: `not within ${Math.round(ms / 60_000)} min` };
    await sleep(1000);
  }
}

/** Waits on a page for the score tab until `from + ms` (real ms): the time it showed, or null. */
async function tabBy(page, label, from, ms) {
  for (;;) {
    if (await scoreTab(page, label).count().catch(() => 0)) return Date.now();
    if (Date.now() > from + ms) return null;
    await sleep(1000);
  }
}

// ---------- one add, the page left open ----------

/**
 * Click "+ Add to main page" on `pick`'s card and leave the page open until the card reads "On the main page →",
 * WAIT_MS passes or the add clearly went wrong (lib/outcome.mjs).
 */
async function addAndWait(stack, page, pick, tag, { before } = {}) {
  const at = Date.now();
  const w = watch(at, { card: () => faceOf(page, pick) }, tag, say);
  try {
    let posted = null;
    page.on("response", (res) => {
      if (res.request().method() === "POST" && res.url().endsWith("/api/request") && !posted) posted = { at: Date.now(), status: res.status() };
    });
    await (before ? before() : addButton(page, pick).click());
    const got = await showsUnless(onMainLink(page, pick), WAIT_MS, addGoesWrong(stack, cardOf(page, pick), at, pick.condition_id));
    const t = { click: at, landed: got.shown ? Date.now() : null, posted };
    await sleep(1000);
    return { pick, t, w, landed: got.shown, why: got.why };
  } finally {
    await w.stop();
  }
}

/** Follow the card's "On the main page →" to the market's page (same tab); returns when its title shows. */
async function followLink(page, pick) {
  await onMainLink(page, pick).click();
  await page.locator("h1.market-title").waitFor({ timeout: 30_000 });
  return Date.now();
}

/** Add `p`, follow its link and leave its page open: {res, openedAt, ...}, or null after a FAIL row. */
async function addAndOpen(g, stack, browser, p, tr, tag) {
  const { context, page } = await openWatchlist(browser, stack, p);
  const res = await addAndWait(stack, page, p, tag);
  if (!res.landed) {
    report.row(`${g}: the add`, '"On the main page →" by itself', `never: ${res.why}; ${await runLine(stack, tr.t.run?.id, res.t.click)}`, false);
    await report.shot(page, `${g}-add`);
    await context.close();
    return null;
  }
  const openedAt = await followLink(page, p);
  return { context, page, res, openedAt, errorsFrom: page.errors.length };
}

/** The stages of an add and its scoring, in seconds after the click, as one line (and into TIMES). */
function stages(g, tag, res, tr, extra = {}) {
  const f = tr.t.first;
  const click = res.t.click;
  const s = (x) => (x ? secs(click, x) : null);
  const dec = (tr.decisions ?? []).filter((d) => d.status === 200);
  const rows = [
    ["run made", s(tr.t.created)],
    ["the run's first claim", s(tr.t.started)],
    ["the request marked added (Worker)", s(tr.t.addedAt)],
    ["add.export done (page published, request added)", s(f["add.export#0 done"])],
    ['card reads "On the main page →"', s(res.t.landed)],
    ["wallets.score first claimed", s(f["wallets.score#0 leased"])],
    ["first Decisions answer", dec.length ? s(dec[0].at * 1000) : null],
    ["last Decisions answer", dec.length ? s(dec[dec.length - 1].at * 1000) : null],
    ["scores published (index entry)", s(f["scores published"])],
    ["wallets.score done", s(f["wallets.score#0 done"])],
    ...Object.entries(extra).map(([k, v]) => [k, s(v)]),
  ];
  for (const [what, v] of rows) time(g, `${tag}: ${what}`, v);
  const claim = f["wallets.score#0 leased"];
  const doneAt = f["wallets.score#0 done"];
  if (claim && doneAt) time(g, `${tag}: wallets.score, claim to done`, secs(claim, doneAt));
  if (claim && dec.length) time(g, `${tag}: fills, histories and facts (claim to first answer)`, secs(claim, dec[0].at * 1000));
  return rows.map(([what, v]) => `${what} ${v === null ? "-" : `+${v} s`}`).join("; ");
}

/**
 * Whether a run made while `other`'s wallets.score was still asking was first claimed only once it had asked its last
 * question: `other`'s last Decisions answer (exact) against this run's first claim (the Worker's whole second, so its
 * real start is within the second after it).
 */
function waitedBehind(stack, tr, other) {
  const from = other.t.first["wallets.score#0 leased"] ?? 0;
  const to = other.t.first["wallets.score#0 done"] ?? Infinity;
  // `other`'s own requests: those naming the market (the input's first line) most of its scoring's window names
  const inWindow = decisionsSince(stack, from).filter((c) => c.at * 1000 <= to);
  const counts = new Map();
  for (const c of inWindow) counts.set(c.market, (counts.get(c.market) ?? 0) + 1);
  const line = [...counts].sort((a, b) => b[1] - a[1])[0]?.[0];
  const asked = line ? decisionsSince(stack, from).filter((c) => c.market === line) : inWindow;
  const end = asked.length ? asked[asked.length - 1].at * 1000 : null;
  if (!tr.t.created || !end || tr.t.created >= end) return { overlap: false, waited: false, end };
  const waited = !tr.t.started || tr.t.started + 1000 >= end;
  return { overlap: true, waited, end, by: tr.t.started ? secs(tr.t.created, tr.t.started) : null };
}

// ---------- the groups ----------

/** The largest pick (the big one when the seed found one). */
const largest = (picks) => [...picks].sort((a, b) => Number(b.big) - Number(a.big) || b.volume - a.volume)[0];

async function group(name, offset, seed, mode, body) {
  if (ONLY && !ONLY.includes(name)) return;
  const opts = stackOptions({ "port-base": BASE + offset, picks: seed.picks ?? 1, cards: seed.cards ?? 1, big: seed.big ?? 0, min: seed.min, days: seed.days, quiet: !args.verbose, keep: args.keep });
  opts.scores = mode;
  opts.state = join(STATE, name);
  say(`${name}: starting a stack on ports ${opts.portBase}-${opts.portBase + 3} (Decisions stand-in: ${mode})`);
  let stack;
  let browser;
  const from = report.rows.length;
  try {
    // seed.floors: the seed's least dollars traded, highest first; the first one the seed can fill is used
    for (const [i, min] of (seed.floors ?? [seed.min]).entries()) {
      opts.seed.min = min;
      try {
        stack = await startStack(opts);
        break;
      } catch (e) {
        if (!seed.floors || i === seed.floors.length - 1 || !/the seed failed/.test(e.message)) throw e;
        say(`${name}: no market of $${min.toLocaleString("en-US")} or more for the seed; trying a lower floor`);
      }
    }
    say(`${name}: stack ready (${stack.seed.picks.map((p) => `${p.slug} $${p.volume.toLocaleString("en-US")}${p.big ? " big" : ""} ${p.day}`).join(", ")}; state ${stack.state})`);
    browser = await launch({ headed: !!args.headed });
    await body(stack, browser, stack.seed.picks);
  } catch (e) {
    report.row(`${name} (group)`, "the group runs", `stopped: ${e.stack ?? e.message}`.slice(0, 700), false);
  } finally {
    for (const t of [...TRACKERS]) await t.stop();
    await browser?.close().catch(() => {});
    await stack?.stop({ keepState: report.failedSince(from) });
  }
}

// main: a large market, its page left open, the tab and everything on it
await group("main", 0, { picks: 1, big: 1, min: MID }, "answer", async (stack, browser, picks) => {
  const g = "main";
  const p = largest(picks);
  const label = rubricName(stack);
  const tr = track(stack, p.condition_id);
  const { context, page } = await openWatchlist(browser, stack, p);
  try {
    const res = await addAndWait(stack, page, p, "main");
    const name = tr.t.name ?? (await request(stack, p)).request?.name;
    const whales = tr.t.run?.meta?.whales;
    const leasedSeen = tr.t.first["wallets.score#0 leased"] ?? tr.t.first["wallets.score#0 done"] ?? null;
    const added = tr.t.addedAt;
    const first = added && leasedSeen ? added <= leasedSeen + 2000 : false;
    const quick = added && res.landed ? res.t.landed - added <= 35_000 : false;
    report.row(
      "add with scores on",
      `"Adding to the main page", then "On the main page →" by itself within ${WITHIN_MS / 60_000} min; the market counts as added before its scoring starts (no later than 2 s after wallets.score's first claim) and the card reads it within 35 s: the scoring never holds up its own add`,
      `${p.slug} ($${p.volume.toLocaleString("en-US")}, ${whales ?? "?"} whales): ${res.w.seen("card")}; ${res.landed ? `landed +${secs(res.t.click, res.t.landed)} s` : `never: ${res.why}`}; ` +
        `added (the Worker's time) ${added ? `+${secs(res.t.click, added)} s` : "never"}, wallets.score first claimed ${leasedSeen ? `+${secs(res.t.click, leasedSeen)} s` : "never"}; the card ${added && res.landed ? `${secs(added, res.t.landed)} s after the add was marked` : "-"}`,
      res.landed && res.t.landed - res.t.click <= WITHIN_MS && !hedges(res.w).length && first && quick,
    );
    if (!res.landed) return;
    if (whales !== undefined && whales < LEAST.main) report.skip("a large market", `the seed's big pick has ${whales} whale(s); main needs at least ${LEAST.main}`);

    // its page, from the card's link; Flagged traders and the home card as they read before scoring
    const openedAt = await followLink(page, p);
    const errorsFrom = page.errors.length;
    const before = { pub: tr.t.first["scores published"] ?? null, tabs: await tabsOf(page) };
    await tradersTab(page).click();
    const flaggedBefore = await flaggedView(page);
    const cardBefore = await homeCard(context, stack, name);
    const entryBefore = tr.t.entry;
    const capturedLate = !!tr.t.first["scores published"];

    // the page left open (never reloaded): the tab appears by itself once wallets.score publishes
    const loads = page.loads;
    const w = watch(openedAt, { tabs: () => tabsOf(page) }, "main/open", say);
    const tab = await tabAppears(page, label, tr);
    await w.stop();
    const job = scoreJob(tr.t.jobs);
    const pub = tr.t.first["scores published"];
    tr.decisions = decisionsSince(stack, res.t.click);
    report.row(
      "page left open: the tab appears by itself",
      `its page, opened from "On the main page →" before the scores exist and never reloaded, shows "${label}" after Timeline and Flagged traders within ${SHOWS_MS / 60_000} min of wallets.score publishing`,
      `opened +${secs(res.t.click, openedAt)} s with "${before.tabs}"${before.pub ? " (the scores were already published then)" : ""}; ${w.seen("tabs")}; ` +
        `scores published ${pub ? `+${secs(res.t.click, pub)} s` : "never"}; tab ${tab.at ? `shown +${secs(res.t.click, tab.at)} s (${secs(pub ?? tab.at, tab.at)} s after the publish)` : `not shown: ${tab.why}`}; ` +
        `reloads while open: ${page.loads - loads}; wallets.score ${job?.state ?? "-"} (attempt ${job?.attempt ?? "-"}, laters ${job?.laters ?? "-"}, partials ${job?.partials ?? "-"})`,
      !before.pub && !!tab.at && page.loads === loads && /Timeline \| Flagged traders.* \| /.test(norm(await tabsOf(page))),
    );
    if (!tab.at) await report.shot(page, "main-open");

    // a reload
    const reloadAt = Date.now();
    await page.reload();
    await page.locator("h1.market-title").waitFor({ timeout: 30_000 });
    const reloaded = await shows(scoreTab(page, label), 30_000);
    const reloadedAt = Date.now();
    report.row(
      "after a reload",
      `the "${label}" tab shows after Timeline and Flagged traders`,
      `${reloaded ? `shown ${secs(reloadAt, reloadedAt)} s after the reload` : `not shown${whyNot(tr) ? ` (${whyNot(tr)})` : ""}`}: "${await tabsOf(page)}"`,
      reloaded,
    );
    time(g, "after a reload: the tab shows (s after the reload)", reloaded ? secs(reloadAt, reloadedAt) : null);
    if (!reloaded) await report.shot(page, "main-reload");

    // the tab itself, against the page the Worker publishes
    const data = await marketData(stack, name);
    const c = reloaded ? await scoreChecks(page, data, label, errorsFrom) : { ok: false, notes: ["the tab is not there"], scored: scoredOf(data).scored.length };
    report.row(
      "the Insider score tab",
      "#/m/<name>/score; band legend and counts as the published page's scores give them; the tab's count = whales in flagged bands; one strip dot per scored whale; the constellation and treemap drawn; a pinned wallet's card with a row per answered question and its score; the rubric's paragraph as the last section, as the page data has it, every placeholder filled, no disclaimer; no hedging words; no page errors",
      c.notes.join("; "),
      c.ok,
    );
    if (!c.ok && reloaded) await report.shot(page, "main-tab");

    // Flagged traders and the home card: as before scoring
    await tradersTab(page).click();
    const flaggedAfter = await flaggedView(page);
    const cardAfter = await homeCard(context, stack, name);
    const plain = (e) => {
      if (!e) return {};
      const { spectrum: _s, v: _v, ...rest } = e;
      return rest;
    };
    const changedKeys = Object.keys({ ...plain(entryBefore), ...plain(tr.t.entry) }).filter((k) => JSON.stringify(plain(entryBefore)[k]) !== JSON.stringify(plain(tr.t.entry)[k]));
    const same = JSON.stringify(flaggedAfter) === JSON.stringify(flaggedBefore) && cardAfter.text === cardBefore.text && JSON.stringify(cardAfter.bar) === JSON.stringify(cardBefore.bar);
    report.row(
      "Flagged traders and the home card keep the cut-offs",
      "the Flagged traders tab's four categories, counts and tiles, and the market's home card, read the same before and after scoring",
      `Flagged traders before: ${flaggedBefore.legend.join(", ")} | ${flaggedBefore.tiles.join(", ")} (${flaggedBefore.tab}); after: ${JSON.stringify(flaggedAfter) === JSON.stringify(flaggedBefore) ? "the same" : `${flaggedAfter.legend.join(", ")} | ${flaggedAfter.tiles.join(", ")} (${flaggedAfter.tab})`}; ` +
        `home card ${cardAfter.text === cardBefore.text && JSON.stringify(cardAfter.bar) === JSON.stringify(cardBefore.bar) ? "the same" : `changed: "${cardBefore.text}" -> "${cardAfter.text}"`} ("${cardBefore.text.slice(0, 160)}"); ` +
        `index entry fields other than the scores that changed: ${changedKeys.join(", ") || "none"}; home page errors: ${[...cardBefore.errors, ...cardAfter.errors].length}${capturedLate ? "; the before copies were read after the scores were published" : ""}`,
      same && !capturedLate && !cardBefore.errors.length && !cardAfter.errors.length && flaggedBefore.legend.length > 0,
    );

    // 390 px wide and dark
    if (reloaded) {
      const phone = await browser.newContext({ viewport: { width: 390, height: 844 }, colorScheme: "dark", isMobile: true, hasTouch: true });
      const pp = await newPage(phone);
      await pp.goto(`${stack.urls.site}#/m/${encodeURIComponent(name)}/score`);
      const legendShown = await shows(pp.locator("ul.tr-legend[aria-label^='Bands'] button.legend-item").first(), 30_000);
      await sleep(1500);
      const m = await pp.evaluate(() => ({
        sw: document.documentElement.scrollWidth,
        w: window.innerWidth,
        dark: matchMedia("(prefers-color-scheme: dark)").matches,
        bg: getComputedStyle(document.body).backgroundColor,
      }));
      const lum = (() => {
        const n = (m.bg.match(/[\d.]+/g) ?? []).map(Number);
        return n.length >= 3 ? (0.2126 * n[0] + 0.7152 * n[1] + 0.0722 * n[2]) / 255 : 1;
      })();
      const tapped = legendShown ? await pinFirst(pp, { tap: true }) : null;
      const phoneOk = legendShown && m.sw <= m.w && m.dark && lum < 0.35 && !!tapped?.addr && !pp.errors.length;
      report.row(
        "phone width and dark mode",
        "at 390 px in dark mode the tab draws with no sideways scroll, on a dark background, and a tapped wallet opens its card; no page errors",
        `legend ${legendShown ? "shown" : "missing"}; scroll width ${m.sw} for ${m.w} px; dark ${m.dark}, background ${m.bg}; tapped card: ${tapped?.addr ? `${tapped.addr.slice(0, 10)}…, ${tapped.rows} question rows` : "none"}; errors: ${pp.errors.slice(0, 2).join(" | ") || "none"}`,
        phoneOk,
      );
      if (!phoneOk) await report.shot(pp, "main-phone");
      await phone.close();
    }

    // the backend: one run, every job done, each whale asked once, no regular-Gemini request
    const end = await scoreEnds(tr, 60_000);
    tr.decisions = decisionsSince(stack, res.t.click);
    const got = tr.t.run ? await stack.run(tr.t.run.id) : null;
    const rub = got?.run.meta?.rubric ?? {};
    const asked = tr.decisions.filter((d) => d.status === 200);
    const twice = askedTwice(tr.decisions);
    const model = callsOf(stack, "model").length;
    const ok = got?.run.state === "done" && got.jobs.every((j) => j.state === "done") && end?.state === "done" && asked.length === c.scored && c.scored > 0 && twice === 0 && rub.scored === c.scored && !model;
    report.row(
      "backend: the add's run",
      "one add run, done, every job done (add.fetch, add.whales, add.export, wallets.score); the Decisions API asked once per whale, nothing asked twice; no regular-Gemini request; the run's meta counts the scored whales",
      `${tr.t.run ? await runLine(stack, tr.t.run.id, res.t.click) : "no run"}${whyNot(tr) ? ` (${whyNot(tr)})` : ""}; meta.rubric ${JSON.stringify(rub)}; Decisions answers ${asked.length} for ${c.scored} scored whales (asked twice: ${twice}); regular-Gemini requests ${model}; ${await callsLine(stack, res.t.click, "add")}`,
      ok,
    );
    say(`main stages: ${stages(g, "main", res, tr, { "the open page shows the tab": tab.at })}`);
  } finally {
    await tr.stop();
    await context.close();
  }
});

/** Pin the first wallet of the strip (a click, or a tap on a touch screen): its card's address and rows. */
async function pinFirst(page, { tap = false } = {}) {
  const dot = page.locator("svg.tr-sp g.sp-dots circle.dot").first();
  if (!(await dot.count())) return null;
  await dot.scrollIntoViewIfNeeded().catch(() => {});
  await (tap ? dot.tap({ force: true }) : dot.click({ force: true }));
  const pin = page.locator(".tr-pin").first();
  if (!(await shows(pin, 10_000))) return { addr: null };
  return {
    addr: norm(await pin.locator(".tr-pin-addr").innerText()),
    rows: await pin.locator(".tr-q").count(),
    score: norm(await pin.locator(".tr-score-value").innerText().catch(() => "")),
  };
}

/** The Insider score tab against the published page `data` (the page is on the market, any view). */
async function scoreChecks(page, data, label, errorsFrom = 0, { unanswered = false } = {}) {
  const notes = [];
  const { rubric, scored, bands, flagged } = scoredOf(data);
  if (!rubric || !scored.length) return { ok: false, notes: [`the published page has ${rubric ? "no scored whale" : "no traders.rubric"}`], scored: scored.length };
  await scoreTab(page, label).click();
  await page.waitForURL(/#\/m\/[^/]+\/score$/, { timeout: 10_000 }).catch(() => {});
  const legendSel = "ul.tr-legend[aria-label^='Bands'] button.legend-item";
  const legendShown = await shows(page.locator(legendSel).first(), 30_000);
  await sleep(1500);
  let ok = legendShown && /#\/m\/[^/]+\/score$/.test(page.url());
  notes.push(`url ${page.url().split("#")[1]}`);

  const legend = legendShown ? await legendOf(page, legendSel) : [];
  const want = bands.filter((b) => b.n > 0).map((b) => `${b.label} ${b.n.toLocaleString("en-US")}`);
  ok &&= JSON.stringify(legend) === JSON.stringify(want);
  notes.push(`legend "${legend.join(", ")}" (page data: "${want.join(", ")}")`);

  const count = norm(await scoreTab(page, label).locator(".tab-count").innerText().catch(() => ""));
  ok &&= count.replace(/[^\d]/g, "") === String(flagged);
  notes.push(`tab count ${count || "-"} (flagged bands: ${flagged})`);

  const dots = await page.locator("svg.tr-sp g.sp-dots circle.dot").count();
  const constellation = await page.locator("section[aria-labelledby='tr-constellation-title'] svg.tr-svg circle.dot").count();
  const treemap = await page.locator("section[aria-labelledby='tr-treemap-title'] svg rect").count();
  ok &&= dots === scored.length && constellation > 0 && treemap > 0;
  notes.push(`strip dots ${dots} for ${scored.length} scored whales; constellation dots ${constellation}; treemap rects ${treemap}`);

  const pin = await pinFirst(page);
  const trader = pin?.addr ? scored.find((t) => t.id.toLowerCase() === pin.addr.toLowerCase()) : null;
  const wantRows = trader ? rubric.questions.filter((q) => trader.spectrum.q?.[q.id]).length : -1;
  const wantScore = trader ? trader.spectrum.score.toFixed(Number.isInteger(trader.spectrum.score) ? 0 : 1) : null;
  ok &&= !!trader && pin.rows === wantRows && wantRows > 0 && pin.score.startsWith(`${wantScore} /`);
  notes.push(`pinned ${pin?.addr ? `${pin.addr.slice(0, 10)}…: "${pin.score}", ${pin.rows} question rows (its page data: ${wantScore}, ${wantRows} answered)` : "nothing"}`);
  await page.keyboard.press("Escape");

  const about = rubric.about;
  const box = page.locator("section.tr-about");
  if (about?.text) {
    const shown = await shows(box, 5000);
    const h = shown && about.heading ? straight(await box.locator("h2").textContent()) : null;
    const para = shown ? straight(await box.locator("p").textContent()) : null;
    const atFoot = shown ? await page.locator(".market-view .traders").evaluate((t) => t.lastElementChild?.matches("section.tr-about") ?? false).catch(() => false) : false;
    const holes = `${about.heading ?? ""} ${about.text}`.match(/[{}]/g)?.length ?? 0;
    const disclaims = [...new Set((`${about.heading ?? ""} ${about.text}`.match(DISCLAIM) ?? []).map((x) => x.toLowerCase()))];
    const good = shown && para === straight(about.text) && (!about.heading || h === straight(about.heading)) && atFoot && !holes && !disclaims.length;
    ok &&= good;
    notes.push(
      `paragraph ${shown ? `"${h ?? ""}: ${para.slice(0, 90)}…" ${para === straight(about.text) ? "as the page data has it" : "differs from the page data"}; ${atFoot ? "the tab's last section" : "not the tab's last section"}; unfilled placeholders ${holes}; disclaimers: ${disclaims.join(", ") || "none"}` : "missing"}`,
    );
  } else {
    const none = (await box.count()) === 0;
    ok &&= none;
    notes.push(`no traders.rubric.about in the page data; paragraph ${none ? "not shown" : "shown"}`);
  }

  const text = await page.locator(".market-page").innerText();
  const words = hedgesIn(text, { ...rubric, own: [data.title, data.correctOutcome] });
  const alerts = await alertsOn(page);
  const errors = errorsOf(page, errorsFrom, { unanswered });
  ok &&= !words.length && !alerts.length && !errors.length;
  notes.push(`hedging words: ${words.join(", ") || "none"}; alerts: ${alerts.join(" | ") || "none"}; page errors since its page opened: ${errors.slice(0, 3).join(" | ") || "none"}`);
  return { ok, notes, scored: scored.length };
}

/** The market's page in a fresh tab of `context` shows the score tab (its page read now). */
async function scoredOnSite(stack, context, name, label) {
  const pg = await newPage(context);
  await pg.goto(`${stack.urls.site}#/m/${encodeURIComponent(name)}`);
  const shown = await shows(scoreTab(pg, label), 30_000);
  const tabs = await tabsOf(pg).catch(() => "-");
  const errors = pg.errors;
  await pg.close();
  return { shown, tabs, errors };
}

// concurrency: two adds a few seconds apart; an add while another market's wallets.score runs
await group("concurrency", 4, { picks: 3, big: 1, min: MID }, "answer", async (stack, browser, all) => {
  const g = "concurrency";
  const mids = all.filter((p) => !p.big);
  const big = all.find((p) => p.big);
  const label = rubricName(stack);
  if (mids.length < 2) {
    report.skip("two adds a few seconds apart", `the seed found only ${mids.length} market(s) of $${MID.toLocaleString("en-US")} or more`);
    return;
  }

  // 1. two adds a few seconds apart, each in its own tab left open
  {
    const [a, b] = mids;
    const A = await openWatchlist(browser, stack, a);
    const B = await openWatchlist(browser, stack, b);
    const ta = track(stack, a.condition_id);
    const tb = track(stack, b.condition_id);
    try {
      const first = addAndWait(stack, A.page, a, "close/1st");
      await sleep(5000);
      const second = addAndWait(stack, B.page, b, "close/2nd");
      const [ra, rb] = await Promise.all([first, second]);
      const [ja, jb] = await Promise.all([scoreEnds(ta), scoreEnds(tb)]);
      await sleep(3000);
      ta.decisions = decisionsSince(stack, ra.t.click).filter((d) => d.at * 1000 <= (ta.t.first["wallets.score#0 done"] ?? Infinity) && d.at * 1000 >= (ta.t.first["wallets.score#0 leased"] ?? 0));
      tb.decisions = decisionsSince(stack, rb.t.click).filter((d) => d.at * 1000 <= (tb.t.first["wallets.score#0 done"] ?? Infinity) && d.at * 1000 >= (tb.t.first["wallets.score#0 leased"] ?? 0));
      const [sa, sb] = [scoredOf(await marketData(stack, ta.t.name)), scoredOf(await marketData(stack, tb.t.name))];
      const [va, vb] = [await scoredOnSite(stack, A.context, ta.t.name, label), await scoredOnSite(stack, B.context, tb.t.name, label)];
      const inTime = (r) => r.landed && r.t.landed - r.t.click <= WITHIN_MS;
      const pass = inTime(ra) && inTime(rb) && ja?.state === "done" && jb?.state === "done" && sa.scored.length > 0 && sb.scored.length > 0 && va.shown && vb.shown && !hedges(ra.w).length && !hedges(rb.w).length;
      report.row(
        "two adds a few seconds apart",
        `both cards read "On the main page →" by themselves within ${WITHIN_MS / 60_000} min; both markets get scored (wallets.score done, the published page carries the scores, a fresh tab of each shows the tab)`,
        `1st ${a.slug} (${ta.t.run?.meta?.whales ?? "?"} whales): ${stages(g, "1st of two", ra, ta)}. 2nd ${b.slug} (${tb.t.run?.meta?.whales ?? "?"} whales, clicked +${secs(ra.t.click, rb.t.click)} s after the 1st): ${stages(g, "2nd of two", rb, tb)}. ` +
          `Scored: ${sa.scored.length} and ${sb.scored.length} whales; fresh tabs: "${va.tabs}" / "${vb.tabs}". ${await callsLine(stack, ra.t.click, "add")}`,
        pass,
      );
      const wb = waitedBehind(stack, tb, ta);
      report.row(
        "the 2nd add does not wait for the 1st's scoring",
        "an add queued while another market's wallets.score is still asking is first claimed before that scoring's last question",
        wb.overlap
          ? `the 2nd's run was made +${secs(rb.t.click, tb.t.created)} s after its click, while the 1st's scoring was asking (its last answer +${secs(rb.t.click, wb.end)} s); its first claim ${tb.t.started ? `+${secs(rb.t.click, tb.t.started)} s (a whole second of the Worker's clock)` : "never"}: ${wb.waited ? "once that scoring had asked its last question" : "before that scoring's last question"}`
          : `the 1st's scoring had asked its last question (+${wb.end ? secs(rb.t.click, wb.end) : "-"} s after the 2nd's click) before the 2nd's run was made: nothing to wait for`,
        !wb.waited,
      );
      time(g, "1st of two: click to On the main page", ra.landed ? secs(ra.t.click, ra.t.landed) : null);
      time(g, "2nd of two: click to On the main page", rb.landed ? secs(rb.t.click, rb.t.landed) : null);
      if (!pass) {
        await report.shot(A.page, "close-1st");
        await report.shot(B.page, "close-2nd");
      }
    } finally {
      await ta.stop();
      await tb.stop();
      await A.context.close();
      await B.context.close();
    }
  }

  // 2. an add made while another market's wallets.score is running
  {
    const c = big ?? mids[2] ?? null;
    const d = big ? mids[2] : null;
    if (!c || !d) {
      report.skip("an add while another market is being scored", `the seed found ${mids.length} market(s) of $${MID.toLocaleString("en-US")} or more and ${big ? "a" : "no"} big one`);
      return;
    }
    const C = await openWatchlist(browser, stack, c);
    const D = await openWatchlist(browser, stack, d);
    const tc = track(stack, c.condition_id);
    const td = track(stack, d.condition_id);
    try {
      const rcP = addAndWait(stack, C.page, c, "scoring/C");
      const leased = await until(() => (scoreJob(tc.t.jobs)?.state === "leased" ? Date.now() : null), { ms: WAIT_MS, every: 250, what: "C's wallets.score" }).catch(() => null);
      if (!leased) {
        await rcP;
        report.skip("an add while another market is being scored", `${c.slug}'s wallets.score never ran (${await runLine(stack, tc.t.run?.id, Date.now())})`);
        return;
      }
      const rd = await addAndWait(stack, D.page, d, "scoring/D");
      const rc = await rcP;
      const [jc, jd] = await Promise.all([scoreEnds(tc), scoreEnds(td)]);
      await sleep(3000);
      tc.decisions = decisionsSince(stack, rc.t.click).filter((x) => x.at * 1000 >= (tc.t.first["wallets.score#0 leased"] ?? 0) && x.at * 1000 <= (tc.t.first["wallets.score#0 done"] ?? Infinity));
      td.decisions = decisionsSince(stack, rd.t.click).filter((x) => x.at * 1000 >= (td.t.first["wallets.score#0 leased"] ?? 0) && x.at * 1000 <= (td.t.first["wallets.score#0 done"] ?? Infinity));
      const [sc, sd] = [scoredOf(await marketData(stack, tc.t.name)), scoredOf(await marketData(stack, td.t.name))];
      const [vc, vd] = [await scoredOnSite(stack, C.context, tc.t.name, label), await scoredOnSite(stack, D.context, td.t.name, label)];
      const whalesC = tc.t.run?.meta?.whales ?? null;
      const inTime = (r) => r.landed && r.t.landed - r.t.click <= WITHIN_MS;
      const pass = inTime(rc) && inTime(rd) && jc?.state === "done" && jd?.state === "done" && sc.scored.length > 0 && sd.scored.length > 0 && vc.shown && vd.shown && !hedges(rd.w).length;
      report.row(
        "an add while another market is being scored",
        `an add clicked while another market's wallets.score holds the add lane lands by itself within ${WITHIN_MS / 60_000} min and both markets get scored`,
        `C ${c.slug} (${whalesC ?? "?"} whales): ${stages(g, "C, being scored", rc, tc)}. D ${d.slug} (${td.t.run?.meta?.whales ?? "?"} whales), clicked +${secs(leased, rd.t.click)} s after C's wallets.score was claimed: ` +
          `${stages(g, "D, added meanwhile", rd, td)}. D's click to "On the main page →": ${rd.landed ? `${secs(rd.t.click, rd.t.landed)} s` : `never (${rd.why})`}; ` +
          `C's wallets.score: attempts ${jc?.attempt ?? "-"}, partials ${jc?.partials ?? "-"}. Scored: ${sc.scored.length} and ${sd.scored.length} whales; fresh tabs: "${vc.tabs}" / "${vd.tabs}". ${await callsLine(stack, rc.t.click, "add")}`,
        pass,
      );
      if (whalesC !== null && whalesC < LEAST.scoring) report.skip("an add does not wait for another market's scoring", `C has ${whalesC} whale(s); a scoring long enough to wait behind needs at least ${LEAST.scoring}`);
      else {
        const wd = waitedBehind(stack, td, tc);
        report.row(
          "an add does not wait for another market's scoring",
          "D, made while C's wallets.score asks its questions, is first claimed before C's last question (an add never waits behind another market's whale questions)",
          `D's run made +${td.t.created ? secs(rd.t.click, td.t.created) : "-"} s after its click; its first claim ${td.t.started ? `+${secs(rd.t.click, td.t.started)} s (a whole second of the Worker's clock)` : "never"}; C's last answer +${wd.end ? secs(rd.t.click, wd.end) : "-"} s: ` +
            `${!wd.overlap ? "C had asked its last question before D's run was made" : wd.waited ? `D waited until C had asked its last question (${wd.by ?? "-"} s queued; an uncontended small add is first claimed within a few seconds)` : "D ran before C's last question"}`,
          wd.overlap && !wd.waited,
        );
      }
      time(g, "D (added while C was scored): click to On the main page", rd.landed ? secs(rd.t.click, rd.t.landed) : null);
      time(g, "D: click to its run's first claim (add.fetch)", td.t.started ? secs(rd.t.click, td.t.started) : null);
      if (!pass) {
        await report.shot(C.page, "scoring-c");
        await report.shot(D.page, "scoring-d");
      }
    } finally {
      await tc.stop();
      await td.stop();
      await C.context.close();
      await D.context.close();
    }
  }
});

// quota: the Decisions API's daily quota spent, and the fallback's; then the quota's reset
await group("quota", 8, { picks: 2, min: MID }, "quota", async (stack, browser, picks) => {
  const g = "quota";
  const p = largest(picks);
  const q = picks.find((x) => x !== p) ?? null;
  const label = rubricName(stack);
  const tr = track(stack, p.condition_id);
  const o = await addAndOpen(g, stack, browser, p, tr, "quota");
  if (!o) return;
  const { context, page, res, openedAt, errorsFrom } = o;
  try {
    const loads = page.loads;
    const w = watch(openedAt, { tabs: () => tabsOf(page) }, "quota/open", say);
    const job = await scoreEnds(tr, WAIT_MS, (j) => j.laters >= 1 || ["done", "failed", "cancelled"].includes(j.state));
    const laterAt = Date.now();
    const reset = nextQuotaReset(await stack.workerNow());
    // the job waits: not claimed again while the cron ticks (2 minutes), nothing published
    const asked0 = decisionsSince(stack, res.t.click).length;
    await sleep(2 * 60_000);
    const stillWaiting = scoreJob(tr.t.jobs);
    const askedMeanwhile = decisionsSince(stack, res.t.click).length - asked0;
    const openTabs = norm(await tabsOf(page));
    const openAlerts = await alertsOn(page);
    const lines = tr.t.run ? stepLines(stack, tr.t.run.id) : [];
    const data = await marketData(stack, tr.t.name);
    const dec = decisionsSince(stack, res.t.click);
    const model = callsOf(stack, "model", res.t.click);
    const waits =
      job?.state === "queued" && job.laters === 1 && lines.some((l) => l === "later (model_quota)") && stillWaiting?.state === "queued" && stillWaiting.laters === 1 && !askedMeanwhile &&
      !tr.t.first["wallets.score#0 leased again"] && !data?.traders?.rubric && dec.length > 0 && dec.every((c) => c.status === 429) && model.length > 0 && model.every((c) => c.status === 429);
    // a reload, and a link to the score view (Flagged traders instead)
    const ownTab = await newPage(context);
    await ownTab.goto(`${stack.urls.site}#/m/${encodeURIComponent(tr.t.name)}`);
    await ownTab.locator("h1.market-title").waitFor({ timeout: 30_000 });
    await sleep(3000);
    const reloadTabs = norm(await tabsOf(ownTab));
    await ownTab.evaluate((n) => (window.location.hash = `#/m/${n}/score`), tr.t.name);
    await sleep(3000);
    const swapped = ownTab.url().split("#")[1];
    const legendShown = await shows(ownTab.locator("ul.tr-legend[aria-label^='Risk categories'] button.legend-item").first(), 15_000);
    const alerts = [...openAlerts, ...(await alertsOn(ownTab))];
    const shownOk = twoTabs(openTabs, label) && twoTabs(reloadTabs, label) && page.loads === loads && /\/traders$/.test(swapped) && legendShown && !alerts.length && !errorsOf(page, errorsFrom).length && !ownTab.errors.length;
    report.row(
      "Decisions quota spent: the scoring waits for the reset",
      `the add lands as usual; once both scorers report today's quota spent, wallets.score waits for the quota's reset (a later, model_quota, at ${reset ? new Date(reset * 1000).toISOString() : "?"}): not claimed again while the cron ticks, nothing published; the page, left open and opened again, keeps Timeline and Flagged traders with no "${label}" tab, and #/m/<name>/score opens Flagged traders; no error on screen`,
      `${res.w.seen("card")}; ${stages(g, "quota", res, tr)}; wallets.score ${job ? `${job.state} +${secs(res.t.click, laterAt)} s (attempt ${job.attempt}, laters ${job.laters})` : "did not end or wait"}, step log: ${lines.join(", ") || "-"}; 2 min on: ${stillWaiting ? `${stillWaiting.state}, laters ${stillWaiting.laters}` : "-"}, ${askedMeanwhile} Decisions call(s) meanwhile; ` +
        `Decisions calls ${dec.length} (${[...new Set(dec.map((c) => c.status))].join(", ") || "-"}), regular-Gemini ${model.length} (${[...new Set(model.map((c) => c.status))].join(", ") || "-"}); published page: ${data?.traders?.rubric ? "has a rubric" : "no rubric"}; ` +
        `open page: "${openTabs}"; opened again: "${reloadTabs}"; #/m/<name>/score → ${swapped}; alerts: ${alerts.join(" | ") || "none"}; page errors: ${[...errorsOf(page, errorsFrom), ...ownTab.errors].slice(0, 2).join(" | ") || "none"}`,
      waits && shownOk,
    );
    await ownTab.close();
    if (!(waits && shownOk)) await report.shot(page, "quota");

    // an add while that scoring waits: the add lane is not paused
    let tq = null;
    let rq = null;
    let Q = null;
    if (q) {
      tq = track(stack, q.condition_id);
      Q = await openWatchlist(browser, stack, q);
      rq = await addAndWait(stack, Q.page, q, "quota/2nd");
      const jq = await scoreEnds(tq, 3 * 60_000, (j) => j.laters >= 1 || ["done", "failed", "cancelled"].includes(j.state));
      report.row(
        "an add while another market waits for the quota",
        `"On the main page →" by itself within ${WITHIN_MS / 60_000} min (the add lane goes on while a scoring waits); its own scoring waits for the reset too`,
        `${q.slug}: ${rq.w.seen("card")}; ${rq.landed ? `landed +${secs(rq.t.click, rq.t.landed)} s` : `never: ${rq.why}`}; its wallets.score ${jq ? `${jq.state} (laters ${jq.laters})` : "-"}`,
        rq.landed && rq.t.landed - rq.t.click <= WITHIN_MS && jq?.state === "queued" && jq.laters === 1 && !hedges(rq.w).length,
      );
    } else report.skip("an add while another market waits for the quota", "the seed found one market only");

    // the quota's reset: the Worker's clock moved past it (the one clock move here), the stand-in answers again
    stack.gemini("answer");
    const movedAt = Date.now();
    await stack.clock(reset + 30);
    const back = await until(() => tr.t.first["wallets.score#0 leased again"] ?? (decisionsSince(stack, movedAt).length ? Date.now() : null), { ms: 4 * 60_000, every: 1000, what: "wallets.score after the reset" }).catch(() => null);
    const end = await scoreEnds(tr, 10 * 60_000);
    const tab = await tabAppears(page, label, tr, 10 * 60_000);
    await w.stop();
    const pub = tr.t.first["scores published"];
    const after = scoredOf(await marketData(stack, tr.t.name));
    const got = await stack.run(tr.t.run.id);
    const rub = got?.run.meta?.rubric ?? {};
    let second = "-";
    let secondOk = true;
    if (tq && rq?.landed) {
      const jq2 = await scoreEnds(tq, 10 * 60_000);
      const sq = scoredOf(await marketData(stack, tq.t.name));
      const vq = await scoredOnSite(stack, Q.context, tq.t.name, label);
      second = `the 2nd: wallets.score ${jq2?.state ?? "-"}, ${sq.scored.length} scored, a fresh tab "${vq.tabs}"`;
      secondOk = jq2?.state === "done" && sq.scored.length > 0 && vq.shown;
    }
    report.row(
      "after the quota's reset: scored, and the open page shows the tab",
      `with the Worker's clock moved 30 s past the reset, the waiting jobs are claimed again by themselves (the cron's wake-ups) within 2 min, ask, publish; the adder's page, open all along and never reloaded, shows "${label}" within ${SHOWS_MS / 60_000} min of the publish; the run ends done`,
      `claimed again ${back ? `${secs(movedAt, back)} s after the clock move` : "never"}; wallets.score ${end?.state ?? "-"} (laters ${end?.laters ?? "-"}); published ${pub ? `${secs(movedAt, pub)} s after the move` : "never"}; tab ${tab.at ? `shown ${secs(pub ?? tab.at, tab.at)} s after the publish` : `not shown: ${tab.why}`}; reloads ${page.loads - loads}; ` +
        `${after.scored.length} of ${after.traders} whales scored, meta.rubric ${JSON.stringify(rub)}; run ${got?.run.state}; ${second}; errors: ${errorsOf(page, errorsFrom).slice(0, 2).join(" | ") || "none"}`,
      !!back && back - movedAt <= 2 * 60_000 && end?.state === "done" && !!pub && !!tab.at && page.loads === loads && after.scored.length > 0 && got?.run.state === "done" && secondOk && !errorsOf(page, errorsFrom).length,
    );
    time(g, "quota: the clock move to the job claimed again (s)", back ? secs(movedAt, back) : null);
    time(g, "quota: the clock move to the scores published (s)", pub ? secs(movedAt, pub) : null);
    if (!tab.at) await report.shot(page, "quota-reset");
    await tq?.stop();
    await Q?.context.close();
  } finally {
    await tr.stop();
    await context.close();
  }
});

// busy: the Decisions API busy; it comes back later by itself, and answers by then
await group("busy", 12, { picks: 1, min: MID }, "busy", async (stack, browser, picks) => {
  const g = "busy";
  const p = largest(picks);
  const label = rubricName(stack);
  const tr = track(stack, p.condition_id);
  const o = await addAndOpen(g, stack, browser, p, tr, "busy");
  if (!o) return;
  const { context, page, res, openedAt, errorsFrom } = o;
  const others = [];
  try {
    const loads = page.loads;
    const w = watch(openedAt, { tabs: () => tabsOf(page) }, "busy/open", say);
    // the step's busy tries, then its later
    const later = await scoreEnds(tr, WAIT_MS, (j) => j.laters >= 1 || ["done", "failed", "cancelled"].includes(j.state));
    const laterAt = tr.t.first["wallets.score#0 later 1"] ?? Date.now();
    const busyCalls = decisionsSince(stack, res.t.click).filter((c) => c.status === 503);
    stack.gemini("answer"); // the API answers again while the step waits
    const switchedAt = Date.now();

    // other pages while it waits: the adder's page opened again (a reload), another visitor's, and a background tab
    const again = await marketPage(context, stack, tr.t.name);
    const visitor = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
    const other = await marketPage(visitor, stack, tr.t.name);
    const away = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
    const hidden = await marketPage(away, stack, tr.t.name);
    await setHidden(hidden, true);
    others.push(visitor, away);
    const opened = { again: [again.loads, await tabsOf(again)], other: [other.loads, await tabsOf(other)], hidden: [hidden.loads, await tabsOf(hidden)] };

    await until(() => decisionsSince(stack, switchedAt).length || scoreJob(tr.t.jobs)?.state !== "queued", { ms: 15 * 60_000, every: 1000, what: "wallets.score to come back" }).catch(() => null);
    const job = await scoreEnds(tr);
    const back = await cameBack(stack, tr, laterAt + 1000);
    const pub = tr.t.first["scores published"];
    const answers = decisionsSince(stack, switchedAt).filter((c) => c.status === 200);
    const wait = back ? Math.round((back - laterAt) / 1000) : null;
    const reuse = back && answers.length ? secs(back, answers[0].at * 1000) : null;
    const reads = answers.length ? polymarketBetween(stack, laterAt, answers[0].at * 1000).length : null;
    const lines = tr.t.run ? stepLines(stack, tr.t.run.id) : [];
    tr.decisions = answers;
    const ok1 = later?.laters >= 1 && lines.includes("later (model_busy)") && busyCalls.length >= 5 && wait !== null && wait >= 280 && wait <= 720 && job?.state === "done" && !!pub && reads === 0;
    report.row(
      "Decisions busy: wallets.score comes back by itself",
      "the scorer's busy tries end in a later (model_busy); the job comes back by itself 5 to 10 minutes on (the cron's wake-ups, nothing run by hand), asks again from its saved facts (no Polymarket request before its first question) and publishes",
      `${busyCalls.length} busy (503) Decisions calls, then the step log: ${lines.join(", ") || "-"}, the later recorded +${secs(res.t.click, laterAt)} s; the stand-in answers from +${secs(res.t.click, switchedAt)} s; the job came back ${wait !== null ? `${wait} s after its later` : "never"}${reuse !== null ? `, its first question ${reuse} s after that, with ${reads} Polymarket request(s) before it` : ""}; ` +
        `wallets.score ${job?.state ?? "-"} (laters ${job?.laters ?? "-"}, attempt ${job?.attempt ?? "-"}); ${answers.length} answers; ${stages(g, "busy", res, tr)}`,
      ok1,
    );
    time(g, "busy: the later to the job back (s)", wait);

    // every page, never reloaded: the adder's (30 s reads) within SHOWS_MS, the others (5 min reads) within REFRESH_MS + 30 s
    const from = pub ?? Date.now();
    const [tabA, atAgain, atOther, away2] = await Promise.all([
      tabAppears(page, label, tr),
      tabBy(again, label, from, REFRESH_MS + 30_000),
      tabBy(other, label, from, REFRESH_MS + 30_000),
      (async () => {
        await sleep(Math.max(0, from + 90_000 - Date.now()));
        const before = await scoreTab(hidden, label).count();
        const t0 = Date.now();
        await setHidden(hidden, false);
        const shown = await shows(scoreTab(hidden, label), 10_000);
        return { before, at: shown ? Date.now() - t0 : null };
      })(),
    ]);
    await w.stop();
    const ok2 = !!tabA.at && page.loads === loads && !errorsOf(page, errorsFrom).length && !(await alertsOn(page)).length;
    report.row(
      "Decisions busy: the adder's open page shows the tab by itself",
      `the market's page, open since before the scores and never reloaded, shows "${label}" within ${SHOWS_MS / 60_000} min of the publish; no error on screen meanwhile`,
      `${w.seen("tabs")}; published ${pub ? `+${secs(res.t.click, pub)} s` : "never"}; tab ${tabA.at ? `shown ${secs(from, tabA.at)} s after the publish` : `not shown: ${tabA.why}`}; reloads ${page.loads - loads}; errors: ${errorsOf(page, errorsFrom).slice(0, 2).join(" | ") || "none"}`,
      ok2,
    );
    if (!ok2) await report.shot(page, "busy-open");
    time(g, "busy: the publish to the tab on the adder's open page (s)", tabA.at && pub ? secs(pub, tabA.at) : null);
    const okOthers = !!atAgain && !!atOther && again.loads === opened.again[0] && other.loads === opened.other[0] && !again.errors.length && !other.errors.length;
    report.row(
      "Decisions busy: the adder's page opened again, and another visitor's, show the tab by themselves",
      `the adder's page opened again (as a reload) and another visitor's page, both opened while the job waited and never reloaded, show "${label}" within ${(REFRESH_MS + 30_000) / 1000} s of the publish (they read the index every 5 minutes)`,
      `opened again with "${opened.again[1]}": ${atAgain ? `the tab ${secs(from, atAgain)} s after the publish` : "no tab"}; another visitor's with "${opened.other[1]}": ${atOther ? `the tab ${secs(from, atOther)} s after the publish` : "no tab"}; reloads ${again.loads - opened.again[0]} and ${other.loads - opened.other[0]}; errors: ${[...again.errors, ...other.errors].slice(0, 2).join(" | ") || "none"}`,
      okOthers,
    );
    if (!okOthers) await report.shot(other, "busy-other");
    time(g, "busy: the publish to the tab on the page opened again (s)", atAgain ? secs(from, atAgain) : null);
    time(g, "busy: the publish to the tab on another visitor's page (s)", atOther ? secs(from, atOther) : null);
    const okAway = away2.before === 0 && away2.at !== null && hidden.loads === opened.hidden[0] && !hidden.errors.length;
    report.row(
      "Decisions busy: a background tab coming back into view",
      `a page in a background tab since before the publish has no "${label}" tab 90 s after it (it asks nothing while hidden) and shows it within 10 s of coming back into view, with no reload`,
      `opened with "${opened.hidden[1]}"; 90 s after the publish: ${away2.before ? "the tab already there" : "no tab"}; back in view: ${away2.at !== null ? `the tab after ${(away2.at / 1000).toFixed(1)} s` : "no tab within 10 s"}; reloads ${hidden.loads - opened.hidden[0]}; errors: ${hidden.errors.slice(0, 2).join(" | ") || "none"}`,
      okAway,
    );
    if (!okAway) await report.shot(hidden, "busy-hidden");

    await page.reload();
    await page.locator("h1.market-title").waitFor({ timeout: 30_000 });
    const reloaded = await shows(scoreTab(page, label), 30_000);
    report.row("Decisions busy: after a reload", `"${label}" shows`, `"${await tabsOf(page)}"`, reloaded);
  } finally {
    await tr.stop();
    await context.close();
    for (const c of others) await c.close().catch(() => {});
  }
});

// killed: every Vercel instance killed while wallets.score is asking its questions
await group("killed", 16, { picks: 1, big: 1, min: MID }, "answer", async (stack, browser, picks) => {
  const g = "killed";
  const p = largest(picks);
  const label = rubricName(stack);
  const tr = track(stack, p.condition_id);
  const o = await addAndOpen(g, stack, browser, p, tr, "killed");
  if (!o) return;
  const { context, page, res, openedAt, errorsFrom } = o;
  try {
    const whales = tr.t.run?.meta?.whales ?? null;
    if (whales !== null && whales < LEAST.killed) {
      report.skip("Vercel killed mid-scoring", `the seed's big pick has ${whales} whale(s); a kill between its questions needs at least ${LEAST.killed}`);
      return;
    }
    const loads = page.loads;
    const w = watch(openedAt, { tabs: () => tabsOf(page) }, "killed/open", say);
    // as soon as the step has an answer in, while it still runs: every instance killed
    const hit = await until(
      () => {
        const j = scoreJob(tr.t.jobs);
        if (j && j.state === "done") return { done: true };
        if (neverSpawned(tr)) return { never: true };
        const n = decisionsSince(stack, res.t.click).filter((c) => c.status === 200).length;
        return j?.state === "leased" && n >= 2 ? { n } : null;
      },
      { ms: WAIT_MS, every: 200, what: "wallets.score's first answers" },
    ).catch(() => null);
    if (!hit || hit.done || hit.never) {
      report.skip("Vercel killed mid-scoring", hit?.done ? "wallets.score finished before it could be killed" : hit?.never ? whyNot(tr) : "wallets.score never reached its questions");
      await w.stop();
      return;
    }
    const firstClaim = tr.t.first["wallets.score#0 leased"];
    const firstAnswer = decisionsSince(stack, res.t.click).find((c) => c.status === 200)?.at * 1000;
    const killed = await stack.vercel.kill();
    const killedAt = Date.now();
    const answeredBefore = decisionsSince(stack, res.t.click).filter((c) => c.status === 200 && c.at * 1000 <= killedAt).length;
    say(`killed: ${killed.killed} instance(s), ${killed.calls} call(s), after ${answeredBefore} answers`);
    await until(() => decisionsSince(stack, killedAt + 2000).length || ["done", "failed", "cancelled"].includes(scoreJob(tr.t.jobs)?.state), { ms: 12 * 60_000, every: 1000, what: "wallets.score to be claimed again" }).catch(() => null);
    const job = await scoreEnds(tr);
    const tab = await tabAppears(page, label, tr);
    await w.stop();
    const back = await cameBack(stack, tr, killedAt + 2000);
    const pub = tr.t.first["scores published"];
    const asked = decisionsSince(stack, res.t.click).filter((c) => c.status === 200);
    const twice = askedTwice(asked);
    const after = asked.filter((c) => c.at * 1000 > killedAt);
    const { scored } = scoredOf(await marketData(stack, tr.t.name));
    const firstAfter = back && after.length ? secs(back, after[0].at * 1000) : null;
    const walk = firstClaim && firstAnswer ? secs(firstClaim, firstAnswer) : null;
    const reads = after.length ? polymarketBetween(stack, killedAt, after[0].at * 1000).length : null;
    tr.decisions = asked;
    const ok1 = killed.killed > 0 && !!back && job?.state === "done" && job.attempt >= 1 && twice <= 1 && scored.length > 0 && asked.length - twice === scored.length && !!pub && reads === 0;
    report.row(
      "Vercel killed mid-scoring",
      "every instance killed while wallets.score asks its questions; once its lease runs out the watchdog queues it again (attempt 1), it resumes from its checkpoints: its saved facts (no Polymarket request before its next question) and answers (no whale asked twice but the one in flight); it publishes",
      `killed ${killed.killed} instance(s) (${killed.calls} call(s)) +${secs(res.t.click, killedAt)} s, after ${answeredBefore} answers; claimed again ${back ? `${secs(killedAt, back)} s after the kill` : "never"}${firstAfter !== null ? `, its next question ${firstAfter} s after that with ${reads} Polymarket request(s) before it (the first walk took ${walk ?? "?"} s)` : ""}; ` +
        `wallets.score ${job?.state ?? "-"} (attempt ${job?.attempt ?? "-"}, partials ${job?.partials ?? "-"}); answers ${asked.length} (${after.length} after the kill), asked twice ${twice}; scored ${scored.length}; ${stages(g, "killed", res, tr)}`,
      ok1,
    );
    time(g, "killed: the kill to the step claimed again (s)", back ? secs(killedAt, back) : null);
    const ok2 = !!tab.at && page.loads === loads;
    report.row(
      "Vercel killed mid-scoring: the open page shows the tab",
      `the market's page, open since before the scores and never reloaded, shows "${label}" within ${SHOWS_MS / 60_000} min of the publish`,
      `${w.seen("tabs")}; tab ${tab.at ? `shown ${secs(pub ?? tab.at, tab.at)} s after the publish` : `not shown: ${tab.why}`}; reloads ${page.loads - loads}`,
      ok2,
    );
    await page.reload();
    await page.locator("h1.market-title").waitFor({ timeout: 30_000 });
    const reloaded = await shows(scoreTab(page, label), 30_000);
    const errors = errorsOf(page, errorsFrom);
    report.row("Vercel killed mid-scoring: after a reload", `"${label}" shows; no page errors`, `"${await tabsOf(page)}"; errors: ${errors.slice(0, 2).join(" | ") || "none"}`, reloaded && !errors.length);
    if (!reloaded) await report.shot(page, "killed-reload");
  } finally {
    await tr.stop();
    await context.close();
  }
});

// fallback: the Decisions API out for the day; the rubric's fallback scorer (regular Gemini) answers
await group("fallback", 20, { picks: 1, min: MID }, "fallback", async (stack, browser, picks) => {
  const g = "fallback";
  const p = largest(picks);
  const label = rubricName(stack);
  const tr = track(stack, p.condition_id);
  const o = await addAndOpen(g, stack, browser, p, tr, "fallback");
  if (!o) return;
  const { context, page, res, openedAt, errorsFrom } = o;
  try {
    const loads = page.loads;
    const w = watch(openedAt, { tabs: () => tabsOf(page) }, "fallback/open", say);
    const job = await scoreEnds(tr);
    const tab = await tabAppears(page, label, tr);
    await w.stop();
    const pub = tr.t.first["scores published"];
    const got = tr.t.run ? await stack.run(tr.t.run.id) : null;
    const rub = got?.run.meta?.rubric ?? {};
    const dec = decisionsSince(stack, res.t.click);
    const answered = callsOf(stack, "model", res.t.click).filter((c) => c.status === 200 && c.rubric);
    const data = await marketData(stack, tr.t.name);
    const { scored, traders } = scoredOf(data);
    report.row(
      "Decisions out for the day: the fallback scores",
      `Decisions answers 429 "per day" and the rubric's fallback (regular Gemini, 8 wallets a request) answers instead; wallets.score publishes every whale's score (meta.rubric.scorer "interactions"); the open page shows "${label}" by itself within ${SHOWS_MS / 60_000} min of the publish`,
      `${res.w.seen("card")}; ${stages(g, "fallback", res, tr)}; Decisions calls ${dec.length} (${[...new Set(dec.map((c) => c.status))].join(", ") || "-"}); fallback requests answered ${answered.length} (${answered.map((c) => c.wallets).join(", ") || "-"} wallets); ` +
        `wallets.score ${job?.state ?? "-"}${whyNot(tr) ? ` (${whyNot(tr)})` : ""}; meta.rubric ${JSON.stringify(rub)}; ${scored.length} of ${traders} whales scored; tab ${tab.at ? `shown ${secs(pub ?? tab.at, tab.at)} s after the publish` : `not shown: ${tab.why}`}; reloads ${page.loads - loads}`,
      job?.state === "done" && dec.length > 0 && dec.every((c) => c.status === 429) && answered.length > 0 && rub.scorer === "interactions" && scored.length > 0 && scored.length === traders && !!tab.at && page.loads === loads,
    );
    if (tab.at) {
      const c = await scoreChecks(page, data, label, errorsFrom);
      report.row("the tab with the fallback's scores", "the same checks as main's tab: legend and counts, strip, constellation, treemap, a pinned wallet's card, the paragraph, no hedging, no errors", c.notes.join("; "), c.ok);
      if (!c.ok) await report.shot(page, "fallback-tab");
    } else await report.shot(page, "fallback-open");
  } finally {
    await tr.stop();
    await context.close();
  }
});

// daily: the daily run with scores on (a watched market it adds, a visitor's add meanwhile, the lists)

// One Open now card for the first pick's market, stored the way the open list stores its cards (today and yesterday),
// as watch.mjs's extra cards are; run in a stack workspace after `watchlist.state pull`; prints one JSON line last.
const CARD_PY = String.raw`
import json, os, sys
sys.path.insert(0, os.getcwd())
from pipeline import polymarket_api
from watchlist import filters, kinds, net, opennow, store, sync
from watchlist.common import DAY, day_shift, local_day, local_iso, now_epoch

net.install()
p = json.loads(sys.argv[1])
now = now_epoch()
today, yesterday = local_day(now), day_shift(local_day(now), -1)
OPEN = os.path.join("web", "public", "data", "open.json")
doc = json.load(open(OPEN))
m = polymarket_api.fetch_market(p["slug"])
ev = filters.event_of(m)
f = kinds.file_event(m["question"], m["question"])
k = f.kind if f is not None and f.kind in kinds.KINDS else "emergency_measure"
card = {"id": opennow.story_key(m["question"]), "label": "new", "listed_since": today, "place": "new step", "kind": k,
        "group": (f.group if f is not None else None) or 1, "question": m["question"],
        "timing": {"kind": "any_day", "until": (m.get("endDate") or "")[:10] or None, "last_step": None, "reveal": None,
                   "reveal_note": None},
        "headline": None, "who_knows": "If it happens, the few people deciding it will know before the public does.",
        "why_watched": kinds.KINDS[k].why, "other_side": None,
        "polymarket": {"question": m["question"], "other_dates": [],
                       "url": f"https://polymarket.com/event/{ev.get('slug') or m['slug']}"},
        "headlines": [], "checked_at": local_iso(now), "event_ids": [], "slugs": [m["slug"]]}
ch = sync.watched_choice([m])
day = doc["days"][0] if doc.get("days") else None
if day is None or day["day"] != today:
    day = {"day": today, "status": "updated", "run_at": local_iso(now), "cards": [], "new_today": 0, "held_back": 0}
    doc["days"] = [day] + list(doc.get("days") or [])
day["cards"] = list(day["cards"]) + [card]
day["new_today"] = len(day["cards"])
with open(OPEN, "w") as fh:
    json.dump(doc, fh, separators=(",", ":"))
conn = store.default_conn()
version = (doc.get("rules") or {}).get("prompt_version")
store.save_open_day(conn, today, "updated", day["run_at"], day["cards"], version=version, info={})
store.save_open_day(conn, yesterday, "updated", local_iso(now - DAY), [dict(card, listed_since=yesterday)],
                    version=version, info={})
print(json.dumps({"story": card["id"], "question": card["question"], "cid": ch and ch["conditionId"].lower()}))
`;

/** The Open now card of `pick`'s market on the stack (stored, saved with site:open.json, in the catalog). */
async function addCard(stack, pick) {
  const py = join(stack.state, "score-card.py");
  writeFileSync(py, CARD_PY);
  const step = async (argv, ws) => {
    const r = await stack.python(argv, ws ? { ws } : {});
    if (r.code !== 0) throw new Error(`${argv.slice(0, 3).join(" ")} failed:\n${r.out.slice(-2000)}`);
    return r;
  };
  const { ws } = await step(["-m", "watchlist.state", "pull"]);
  const made = await step([py, JSON.stringify({ slug: pick.slug })], ws);
  await step(["-m", "watchlist.state", "push", "--by", "local"], ws);
  await step(["-m", "watchlist.sync", "catalog"], ws);
  return JSON.parse(made.out.trim().split("\n").pop());
}

/** Every second, a run's jobs: when each first reached each state (real ms). */
function trackRun(stack, id) {
  const t = { first: {}, run: null, jobs: [] };
  let on = true;
  const loop = (async () => {
    while (on) {
      const got = await stack.run(id).catch(() => null);
      const now = Date.now();
      if (got) {
        t.run = got.run;
        t.jobs = got.jobs;
        for (const j of got.jobs) t.first[`${j.kind}#${j.part} ${j.state}`] ??= now;
      }
      await sleep(1000);
    }
  })();
  return {
    t,
    stop: async () => {
      on = false;
      await loop;
    },
  };
}

await group("daily", 24, { picks: 2, min: MID }, "answer", async (stack, browser, picks) => {
  const g = "daily";
  if (picks.length < 2) {
    report.skip("scores through the daily run", `the seed found ${picks.length} market(s); the group needs 2`);
    return;
  }
  const [wp, vp] = picks;
  const label = rubricName(stack);
  const w1 = await addCard(stack, wp);
  say(`daily: the Open now card for ${wp.slug}: ${JSON.stringify(w1)}`);
  // the person watches it from Open now (its question names both its Open now card and its closed card)
  const A = { context: await browser.newContext({ viewport: { width: 1440, height: 1000 } }) };
  A.page = await newPage(A.context);
  await A.page.goto(`${stack.urls.site}#/watchlist?day=${wp.day}`);
  const openCard = A.page.locator("article.op-card").filter({ has: A.page.locator("h3.cl-card-title", { hasText: w1.question.slice(0, 80) }) }).first();
  await openCard.getByRole("button", { name: "Watch this market" }).waitFor({ timeout: 60_000 });
  await openCard.getByRole("button", { name: "Watch this market" }).click();
  const watching = await shows(openCard.locator(".wl-act", { hasText: "Watching" }).first(), 20_000);
  const tw = track(stack, w1.cid ?? wp.condition_id);
  const tv = track(stack, vp.condition_id);
  // the daily run: the Worker's clock moved to the next 04:00 Toronto (the one clock move here), the cron's tick makes it
  const day = dayShift(stack.today, 1);
  const madeAt = Date.now();
  await stack.clock(torontoAt(day, "04:00"));
  const madeW = madeAt + ((await stack.workerNow()) * 1000 - Date.now()); // the same moment on the Worker's clock
  await stack.tick();
  const dr = trackRun(stack, `daily:${day}`);
  try {
    // a visitor's add once the run's heavy Open now walk holds its lease
    const walk = await until(() => (dr.t.jobs.find((j) => j.kind === "open.walk" && j.state === "leased") ? Date.now() : null), { ms: 10 * 60_000, every: 500, what: "the daily run's Open now walk" }).catch(() => null);
    const V = await openWatchlist(browser, stack, vp);
    const rv = await addAndWait(stack, V.page, vp, "daily/visitor");
    // the run to its end, every add scored
    const end = await stack.waitRun(`daily:${day}`, { ms: 40 * 60_000 }).catch(() => null);
    if (!tw.t.req) await sleep(30_000); // the run's last report; then a watched market with no request was not added
    const [jw, jv] = await Promise.all([tw.t.req ? scoreEnds(tw) : null, scoreEnds(tv)]);
    await sleep(3000);
    const runEnd = Date.now();
    const daily = (await stack.run(`daily:${day}`)) ?? { run: {}, jobs: [] };
    const published = (k) => daily.jobs.filter((j) => j.kind === k).every((j) => j.state === "done") && daily.jobs.some((j) => j.kind === k);
    const lists = await Promise.all(["closed", "open"].map((k) => fetch(`${stack.urls.api}/api/data/${k}.json`, { cache: "no-store" }).then((r) => r.json(), () => null)));

    // the watched market: added by the run, scored, its page shows the tab
    const sw = tw.t.name ? scoredOf(await marketData(stack, tw.t.name)) : { scored: [] };
    const vw = tw.t.name ? await scoredOnSite(stack, A.context, tw.t.name, label) : { shown: false, tabs: "-" };
    report.row(
      "daily run: a watched market is added and scored",
      `a market watched from Open now and closed with a result: the daily run adds it with no click, its wallets.score runs and publishes, and its page shows "${label}"`,
      `watched: ${watching ? "Watching" : "the button never read Watching"}; its request ${tw.t.req?.status ?? "none"} (page ${tw.t.name ?? "-"}), made ${tw.t.created ? `${secs(madeAt, tw.t.created)} s after the run was made` : "never"}; wallets.score ${jw?.state ?? "-"}${whyNot(tw) ? ` (${whyNot(tw)})` : ""}; ` +
        `${sw.scored.length} whales scored; a fresh tab of its page: "${vw.tabs}"; ${tw.t.run ? await runLine(stack, tw.t.run.id, madeW) : "no run"}`,
      watching && tw.t.req?.status === "added" && jw?.state === "done" && sw.scored.length > 0 && vw.shown,
    );

    // the visitor's add during the run
    const wb = waitedBehind(stack, tv, tw);
    report.row(
      "an add made during the daily run (scores on)",
      `"On the main page →" by itself within ${WITHIN_MS / 60_000} min of the click (it waits at most for the walk's heavy lease) and the market is scored; whether it waited behind the watched market's scoring is noted`,
      `clicked ${walk ? `${secs(walk, rv.t.click)} s after the Open now walk was claimed` : "(the walk was never seen leased)"}; ${rv.w.seen("card")}; ${rv.landed ? `landed +${secs(rv.t.click, rv.t.landed)} s` : `never: ${rv.why}`}; its wallets.score ${jv?.state ?? "-"}; ` +
        `${wb.overlap ? (wb.waited ? "it waited until the watched market's scoring had asked its last question" : "it ran before the watched market's last question") : "no overlap with the watched market's scoring"}; ${tv.t.run ? await runLine(stack, tv.t.run.id, rv.t.click + (madeW - madeAt)) : "no run"}`,
      rv.landed && rv.t.landed - rv.t.click <= WITHIN_MS && jv?.state === "done" && !hedges(rv.w).length,
    );
    time(g, "visitor's add during the daily run: click to On the main page", rv.landed ? secs(rv.t.click, rv.t.landed) : null);

    // the lists
    report.row(
      "the day's lists publish with scores on",
      "the daily run ends done, its closed.publish and open.publish done, and the Worker serves both lists",
      `${end ? await runLine(stack, `daily:${day}`, madeW) : `the daily run did not end within 40 min (${await runLine(stack, `daily:${day}`, madeW)})`}; closed.json ${lists[0]?.days ? `${lists[0].days.length} day(s)` : "missing"}, open.json ${lists[1]?.days ? `${lists[1].days.length} day(s)` : "missing"}`,
      daily.run.state === "done" && published("closed.publish") && published("open.publish") && Array.isArray(lists[0]?.days) && Array.isArray(lists[1]?.days),
    );

    // what the scoring adds: the heavy jobs one at a time (open.walk against the watched market's add and scoring)
    const f = dr.t.first;
    const s = (x) => (x ? secs(madeAt, x) : null);
    for (const [what, v] of [
      ["daily run: open.walk first claimed", s(f["open.walk#0 leased"])],
      ["daily run: open.walk done", s(f["open.walk#0 done"])],
      ["watched add: run made", s(tw.t.created)],
      ["watched add: first claim", s(tw.t.started)],
      ["watched add: wallets.score first claimed", s(tw.t.first["wallets.score#0 leased"])],
      ["watched add: wallets.score done", s(tw.t.first["wallets.score#0 done"])],
      ["visitor's add: first claim", s(tv.t.started)],
      ["visitor's add: wallets.score done", s(tv.t.first["wallets.score#0 done"])],
      ["daily run: done", s(f["daily.end#0 done"])],
      ["everything done", s(runEnd)],
    ])
      time(g, `${what} (s after the daily run was made)`, v);
    await V.context.close();
  } finally {
    await dr.stop();
    await tw.stop();
    await tv.stop();
    await A.context.close();
  }
});

// large: the seed's largest market; the pacer's window and a scoring across Vercel calls. The pick comes from the
// last 14 days of closed markets (the default 3 days can have none past $250k: too few whales for either check), at
// the highest floor that has one (the seed takes the smallest big picks above its floor, so a high floor makes it large)
await group("large", 28, { picks: 1, big: 3, days: 14, floors: [600_000, 400_000, 300_000, 240_000, 150_000] }, "answer", async (stack, browser, picks) => {
  const g = "large";
  const p = largest(picks);
  const label = rubricName(stack);
  const tr = track(stack, p.condition_id);
  const o = await addAndOpen(g, stack, browser, p, tr, "large");
  if (!o) return;
  const { context, page, res, openedAt, errorsFrom } = o;
  try {
    const whales = tr.t.run?.meta?.whales ?? null;
    const loads = page.loads;
    const w = watch(openedAt, { tabs: () => tabsOf(page) }, "large/open", say);
    const job = await scoreEnds(tr);
    const tab = await tabAppears(page, label, tr);
    await w.stop();
    const pub = tr.t.first["scores published"];
    const asked = decisionsSince(stack, res.t.click);
    const ok = asked.filter((c) => c.status === 200);
    const twice = askedTwice(asked);
    const { scored } = scoredOf(await marketData(stack, tr.t.name));
    const lines = tr.t.run ? stepLines(stack, tr.t.run.id) : [];
    tr.decisions = asked;
    const claim = tr.t.first["wallets.score#0 leased"];
    const walk = claim && ok.length ? (ok[0].at * 1000 - claim) / 1000 : null;
    const gaps = ok.slice(1).map((c, i) => c.at - ok[i].at);
    // the most answers in any 60 s: the rubric's 45,000 tokens a minute (about 1,600 a request) holds it near 28
    const busiest = ok.reduce((m, c, i) => Math.max(m, ok.slice(i).filter((d) => d.at - c.at < 60).length), 0);
    const qEach = ok.length > 1 ? (ok[ok.length - 1].at - ok[0].at) / (ok.length - 1) : null;
    const histEach = walk !== null && whales ? walk / whales : null;
    const split = (job?.partials ?? 0) >= 1;
    const scoredOk = job?.state === "done" && twice === 0 && ok.length === scored.length && scored.length > 0 && !!tab.at && page.loads === loads;
    report.row(
      "a large market: scored, every whale asked once",
      `the seed's largest market: wallets.score asks every whale exactly once (across its calls when it spans several), publishes, and the open page shows "${label}" by itself within ${SHOWS_MS / 60_000} min of the publish`,
      `${p.slug} ($${p.volume.toLocaleString("en-US")}, ${whales ?? "?"} whales); step log: ${lines.join(", ") || "-"}; wallets.score ${job?.state ?? "-"} (partials ${job?.partials ?? "-"}, attempt ${job?.attempt ?? "-"}); answers ${ok.length}, asked twice ${twice}; scored ${scored.length}; ` +
        `tab ${tab.at ? `shown ${secs(pub ?? tab.at, tab.at)} s after the publish` : `not shown: ${tab.why}`}; reloads ${page.loads - loads}; ${stages(g, "large", res, tr)}`,
      scoredOk,
    );
    const splitWhat = "wallets.score runs over more than one Vercel call (a partial, then the next call goes on from its cursor) and still asks no whale twice";
    if (split) report.row("a large market: scored across Vercel calls", splitWhat, `${job.partials} partial(s); step log: ${lines.join(", ")}; asked twice ${twice}`, scoredOk);
    else notRun("a large market: scored across Vercel calls", splitWhat, `the market's ${whales ?? "?"} whales scored in one call (${lines.join(", ") || "-"}); a split needs a market whose fills, histories and questions outlast what is left of a call`);
    const pacerWhat = "the rubric's 45,000 tokens a minute holds any minute to about 28 requests, the rest waiting for the window (the stand-in answers in 1 s; live a request averaged 2.0 s once the window bound)";
    if (ok.length > 30) {
      report.row(
        "a large market: the pacer's token window",
        pacerWhat,
        `${ok.length} answers over ${(ok[ok.length - 1].at - ok[0].at).toFixed(0)} s; the busiest minute held ${busiest}; the longest wait between answers ${Math.max(...gaps).toFixed(1)} s`,
        busiest <= 30 && Math.max(...gaps) >= 10,
      );
      time(g, "answers in the busiest minute", busiest);
    } else notRun("a large market: the pacer's token window", pacerWhat, `${ok.length} answers do not fill the window (about 28 a minute)`);
    // per-whale rates and what 100 and 600 whales would take
    if (histEach !== null) time(g, "fills, histories and facts per whale (s)", Math.round(histEach * 100) / 100);
    if (qEach !== null) time(g, "a question request per whale, as measured here (s; the stand-in answers in 1 s)", Math.round(qEach * 100) / 100);
    time(g, "a question request per whale, live (s; the token window bound)", LIVE_Q_S);
    if (histEach !== null) {
      for (const n of [100, 600]) {
        const scoring = n * (histEach + Math.max(qEach ?? 0, LIVE_Q_S));
        time(g, `${n} whales: wallets.score with live questions (min; an add made meanwhile goes first)`, Math.round(scoring / 6) / 10);
      }
    }
    await page.reload();
    await page.locator("h1.market-title").waitFor({ timeout: 30_000 });
    const reloaded = await shows(scoreTab(page, label), 30_000);
    const errors = errorsOf(page, errorsFrom);
    report.row("a large market: after a reload", `"${label}" shows; no page errors`, `"${await tabsOf(page)}"; errors: ${errors.slice(0, 2).join(" | ") || "none"}`, reloaded && !errors.length);
  } finally {
    await tr.stop();
    await context.close();
  }
});

// faults: Polymarket down during wallets.score, the Worker down while it asks, one wallet's request refused
await group("faults", 32, { picks: 1, big: 1, min: MID }, "answer refuse", async (stack, browser, picks) => {
  const g = "faults";
  const p = largest(picks);
  const label = rubricName(stack);
  const tr = track(stack, p.condition_id);
  // Polymarket down from the moment the add's last step starts (it reads nothing from Polymarket), so the scoring's
  // first Polymarket request meets the outage
  let downAt = null;
  const arm = (async () => {
    const t0 = Date.now();
    while (!downAt && Date.now() - t0 < WAIT_MS) {
      const id = tr.t.run?.id;
      const got = id ? await stack.run(id).catch(() => null) : null;
      if (got?.jobs.some((j) => (j.kind === "add.export" && j.state !== "queued") || j.kind === "wallets.score")) {
        stack.gemini("answer refuse polymarket");
        downAt = Date.now();
      }
      await sleep(200);
    }
  })();
  const o = await addAndOpen(g, stack, browser, p, tr, "faults");
  if (!o) {
    downAt ??= -1;
    await arm;
    return;
  }
  const { context, page, res, openedAt, errorsFrom } = o;
  try {
    await arm;
    const whales = tr.t.run?.meta?.whales ?? null;
    const loads = page.loads;
    const w = watch(openedAt, { tabs: () => tabsOf(page) }, "faults/open", say);
    // 1. Polymarket down: Polymarket's own retries, then a later (polymarket_down); Polymarket back meanwhile
    const later = await scoreEnds(tr, 10 * 60_000, (j) => j.laters >= 1 || ["done", "failed", "cancelled"].includes(j.state));
    const laterAt = Date.now();
    stack.gemini("answer refuse");
    const pmCalls = polymarketBetween(stack, res.t.click).filter((c) => c.status === 503);
    await until(() => decisionsSince(stack, laterAt).length || polymarketBetween(stack, laterAt + 1000).length || scoreJob(tr.t.jobs)?.state !== "queued", { ms: 8 * 60_000, every: 1000, what: "wallets.score after Polymarket" }).catch(() => null);
    const back = await cameBack(stack, tr, laterAt + 1000);
    const lines1 = tr.t.run ? stepLines(stack, tr.t.run.id) : [];
    report.row(
      "Polymarket down during wallets.score",
      "with Polymarket answering 503, the step gives up after Polymarket's own retries with a later (polymarket_down, 120 s); Polymarket back, the job comes back by itself (the cron's wake-ups)",
      `Polymarket down from +${downAt > 0 ? secs(res.t.click, downAt) : "-"} s (${tr.t.first["wallets.score#0 leased"] && downAt > 0 ? (downAt < tr.t.first["wallets.score#0 leased"] ? "before" : "after") : "-"} wallets.score's first claim was seen); ${pmCalls.length} Polymarket 503s; step log: ${lines1.join(", ") || "-"}; ` +
        `the later recorded +${later ? secs(res.t.click, laterAt) : "-"} s; Polymarket back then; the job came back ${back ? `${secs(laterAt, back)} s later` : "never"}`,
      downAt > 0 && later?.laters >= 1 && lines1.includes("later (polymarket_down)") && pmCalls.length > 0 && !!back && back - laterAt <= 5 * 60_000,
    );
    time(g, "Polymarket back to the job claimed again (s)", back ? secs(laterAt, back) : null);

    // 2. the Worker down while the step asks its questions: 90 s
    const asking = await until(() => (decisionsSince(stack, laterAt).filter((c) => c.status === 200).length >= 2 ? Date.now() : neverSpawned(tr) || scoreJob(tr.t.jobs)?.state === "done" ? -1 : null), { ms: 10 * 60_000, every: 200, what: "the step's answers" }).catch(() => null);
    let stoppedAt = null;
    let startedAt = null;
    if (asking && asking > 0) {
      await stack.stopApi();
      stoppedAt = Date.now();
      await sleep(90_000);
      await stack.startApi();
      startedAt = Date.now();
    }
    const job = await scoreEnds(tr);
    const tab = await tabAppears(page, label, tr);
    await w.stop();
    const pub = tr.t.first["scores published"];
    const calls = decisionsSince(stack, res.t.click);
    const refused = [...new Set(calls.filter((c) => c.status === 400).map((c) => c.input))];
    const twice = askedTwice(calls);
    const lines = tr.t.run ? stepLines(stack, tr.t.run.id) : [];
    const resumed = stoppedAt ? decisionsSince(stack, startedAt).find((c) => c.status === 200) : null;
    report.row(
      "the Worker down while wallets.score asks",
      "the Worker stopped for 90 s after two answers: the step's checkpoint fails, its lease runs out, the watchdog queues it again (attempt 1) and it resumes (no whale asked twice but the one in flight), then publishes",
      stoppedAt
        ? `stopped +${secs(res.t.click, stoppedAt)} s after ${decisionsSince(stack, laterAt).filter((c) => c.status === 200 && c.at * 1000 <= stoppedAt).length} answers, back +${secs(res.t.click, startedAt)} s; the next answer ${resumed ? `${secs(startedAt, resumed.at * 1000)} s after it was back` : "never"}; wallets.score ${job?.state ?? "-"} (attempt ${job?.attempt ?? "-"}, laters ${job?.laters ?? "-"}); step log: ${lines.join(", ") || "-"}; asked twice ${twice}`
        : `the step never had two answers in (${whyNot(tr) || `wallets.score ${scoreJob(tr.t.jobs)?.state ?? "-"}`})`,
      !!stoppedAt && job?.state === "done" && job.attempt >= 1 && twice <= 1 && !!pub,
    );
    time(g, "the Worker back to the step asking again (s)", resumed ? secs(startedAt, resumed.at * 1000) : null);

    // 3. one wallet's request refused: that wallet unscored, the rest on the tab; and the open page after it all
    const data = await marketData(stack, tr.t.name);
    const { scored, traders } = scoredOf(data);
    const rub = (await stack.run(tr.t.run.id))?.run.meta?.rubric ?? {};
    const c = tab.at ? await scoreChecks(page, data, label, errorsFrom, { unanswered: true }) : { ok: false, notes: [`the tab never showed: ${tab.why}`] };
    report.row(
      "one wallet's request refused",
      `the stand-in refuses one wallet's request (a plain 400) every time it is asked: that wallet stays unscored (scored = whales - 1, meta.rubric.unscored 1), the strip has a dot per scored whale, and the open page, never reloaded, shows "${label}" by itself within ${SHOWS_MS / 60_000} min of the publish with no error on screen through all three faults`,
      `${whales ?? "?"} whales, ${traders} on the page, ${scored.length} scored; refused input(s) ${refused.length} (asked ${calls.filter((x) => x.status === 400).length} time(s)); meta.rubric ${JSON.stringify(rub)}; tab ${tab.at ? `shown ${secs(pub ?? tab.at, tab.at)} s after the publish` : `not shown: ${tab.why}`}; reloads ${page.loads - loads}; ${c.notes.join("; ")}`,
      refused.length === 1 && scored.length === traders - 1 && rub.unscored === 1 && !!tab.at && page.loads === loads && c.ok,
    );
    if (!c.ok) await report.shot(page, "faults-tab");
    if (whales !== null && whales < LEAST.faults) report.skip("faults on a market with several whales", `the seed's big pick has ${whales} whale(s); the group needs at least ${LEAST.faults}`);
    say(`faults stages: ${stages(g, "faults", res, tr)}`);
  } finally {
    await tr.stop();
    await context.close();
  }
});

// the stages' durations, then the table
const tag = ONLY ? ONLY.join("-") : "all";
if (TIMES.length) {
  console.log("\nstage durations (seconds; after the click unless the stage says otherwise)");
  for (const [g, what, s] of TIMES) console.log(`  ${g.padEnd(12)} ${String(s).padStart(8)}  ${what}`);
  writeFileSync(join(report.shots, `timings-${tag}.json`), JSON.stringify(TIMES, null, 2));
}
report.finish(tag);
