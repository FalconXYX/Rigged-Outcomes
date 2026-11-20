// Smoke: "+ Add to main page" the way a person uses it, through the whole stack with real timing. It starts a
// stack (tests/end-to-end/stack.mjs), opens the Watchlist on the seeded day, presses "+ Add to main page" on a
// closed card, and then only waits: the page stays open, is never reloaded, and nothing is run by hand (the
// Worker wakes the local Vercel, which runs the add's steps). It passes when
//
//   - the card reads "Adding to the main page" after the click, then "On the main page →" by itself;
//   - the click made exactly one add run, which ended done with every job done, and every Vercel call answered;
//   - that link opens the market's page in the same tab (its page read from the Worker's /api/data/markets/),
//     the home page there lists it, and a fresh tab of the site lists it too, with no site build since the start;
//   - the API records the add as added, under the page name the run gave it;
//   - no model request was made, and the page threw no errors.
//
// It prints how long each stage took from the click. The wait stops as soon as the outcome is clearly wrong
// (lib/outcome.mjs: the card gone or saying the add failed, the add's run ending other than done).
//
//   node tests/end-to-end/scenarios/smoke.mjs [--port-base 9500] [--state DIR] [--timeout MINUTES] [--headed]
//                                             [--verbose] [--keep]
//
// --keep leaves the stack running afterwards (Ctrl-C stops it) and keeps its state folder, which a pass removes
// otherwise. Exit 0 passed, 1 failed.

import { launch } from "../lib/browser.mjs";
import { addGoesWrong, failedJobs, showsUnless } from "../lib/outcome.mjs";
import { secs, sleep } from "../lib/proc.mjs";
import { parseArgs, stackOptions, startStack } from "../stack.mjs";

const args = parseArgs(process.argv.slice(2));
const opts = stackOptions({ "port-base": 9500, ...args, quiet: !args.verbose });
const timeoutMs = Number(args.timeout ?? 15) * 60_000;
const failures = [];
const check = (ok, what) => {
  console.log(`${ok ? "ok  " : "FAIL"} ${what}`);
  if (!ok) failures.push(what);
};

const t0 = Date.now();
let clickAt = 0;
console.log(`smoke: starting a stack on ports ${opts.portBase}-${opts.portBase + 3}`);
let stack;
let browser;
let pick;
try {
  stack = await startStack(opts);
  console.log(`smoke: stack ready in ${secs(t0)} s (state ${stack.state})`);
  pick = stack.seed.picks[0];
  console.log(`smoke: adding "${pick.question}" (${pick.slug}, $${pick.volume.toLocaleString("en-US")} traded, closed ${pick.day})`);

  browser = await launch({ headed: !!args.headed });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  const page = await context.newPage();
  const consoleErrors = [];
  page.on("pageerror", (e) => consoleErrors.push(String(e)));
  let posted = null;
  const pageReads = [];
  page.on("response", (res) => {
    if (res.request().method() === "POST" && res.url().endsWith("/api/request") && !posted) posted = { at: Date.now(), status: res.status() };
    if (res.url().includes("/api/data/markets/")) pageReads.push({ url: res.url(), status: res.status() });
  });
  let navigations = 0;
  page.on("framenavigated", (f) => {
    if (f === page.mainFrame()) navigations++;
  });

  await page.goto(`${stack.urls.site}#/watchlist?day=${pick.day}`);
  const card = page.locator("article.cl-card").filter({ has: page.locator("h3.cl-card-title", { hasText: pick.question }) });
  const add = card.locator("button.wl-act-btn", { hasText: "Add to main page" });
  await add.waitFor({ timeout: 60_000 });
  const loadedNavigations = navigations;

  // what the backend does meanwhile: the add's run and its jobs, sampled every 2 s
  const seen = {};
  let watching = true;
  const watcher = (async () => {
    while (watching) {
      const id = await stack.runOf(pick.condition_id).catch(() => null);
      const got = id ? await stack.run(id).catch(() => null) : null;
      if (got) {
        seen.run ??= got.run.id;
        for (const j of got.jobs) seen[`${j.kind}#${j.part} ${j.state}`] ??= Date.now();
        seen[`run ${got.run.state}`] ??= Date.now();
      }
      await sleep(2000);
    }
  })();

  clickAt = Date.now();
  await add.click();
  await card.locator(".wl-act-state", { hasText: "Adding to the main page" }).waitFor({ timeout: 60_000 });
  const addingAt = Date.now();
  console.log(`  +${secs(clickAt, addingAt)} s  the card reads "Adding to the main page" (POST /api/request ${posted?.status ?? "?"})`);

  const link = card.locator("a.wl-act-btn", { hasText: "On the main page" });
  const got = await showsUnless(link, timeoutMs, addGoesWrong(stack, card, clickAt, pick.condition_id));
  if (!got.shown) {
    const req = await fetch(`${stack.urls.api}/api/request?cid=${pick.condition_id}`).then((r) => r.json(), () => ({}));
    throw new Error(`the card does not read "On the main page →": ${got.why} (the API has the add as ${req.request?.status ?? "nothing"})`);
  }
  const onMainAt = Date.now();
  console.log(`  +${secs(clickAt, onMainAt)} s  the card reads "On the main page →" (no reload, nothing run by hand)`);
  check(navigations === loadedNavigations, "the page was never reloaded or navigated away while it waited");

  // the API's record of the add
  const req = await fetch(`${stack.urls.api}/api/request?cid=${pick.condition_id}`).then((r) => r.json());
  const name = req.request?.name;
  check(req.request?.status === "added" && !!name, `the API records the add as added (${req.request?.status}, page name ${name})`);
  const href = await link.getAttribute("href");
  check(href === `#/m/${name}`, `"On the main page →" links to its page (${href})`);

  // the link, then the home page, in the same tab
  await link.click();
  await page.locator("h1.market-title").waitFor({ timeout: 30_000 });
  const marketAt = Date.now();
  check(page.url().endsWith(`#/m/${name}`), `the link opens the market's page in the same tab (${await page.locator("h1.market-title").innerText()})`);
  check(
    pageReads.some((r) => r.url.includes(`/api/data/markets/${name}.json?v=`) && r.status === 200),
    `its page came from the Worker (${pageReads.map((r) => `${r.status} ${r.url.replace(stack.urls.api, "")}`).join(", ") || "no read"})`,
  );
  await page.evaluate(() => (window.location.hash = "#/"));
  await page.locator(`a.mc-link[href="#/m/${name}"]`).waitFor({ timeout: 30_000 });
  const homeAt = Date.now();
  check(true, "the home page in the same tab lists the market");

  // a fresh tab, as someone opening the site now
  const fresh = await context.newPage();
  await fresh.goto(stack.urls.site);
  const listed = await fresh
    .locator(`a.mc-link[href="#/m/${name}"]`)
    .waitFor({ timeout: 30_000 })
    .then(() => true, () => false);
  check(listed, "a fresh tab of the site lists the market on the home page");
  check(stack.site.deploys.length === 1, `no site build since the start (${stack.site.deploys.length} build(s))`);

  // the backend: one add run, done, every job done, every Vercel call answered
  watching = false;
  await watcher;
  const o = await stack.overview();
  const addRuns = (o.backend?.runs ?? []).filter((r) => r.kind === "add" && String(r.slot).startsWith(`${pick.condition_id}:`));
  check(addRuns.length === 1, `the click made exactly one add run (${addRuns.map((r) => `${r.id} ${r.state}`).join(", ")})`);
  const r = addRuns[0] ? await stack.run(addRuns[0].id) : null;
  check(r?.run.state === "done", `that run ended ${r?.run.state}${r ? ` (${failedJobs(r.jobs) || "no job failed"})` : ""}`);
  const kinds = (r?.jobs ?? []).map((j) => `${j.kind}#${j.part}:${j.state}`);
  check(
    !!r && r.jobs.every((j) => j.state === "done") && ["add.fetch", "add.export"].every((k) => r.jobs.some((j) => j.kind === k)),
    `its jobs are all done (${kinds.join(", ")})`,
  );
  const calls = (await stack.vercel.calls()).filter((c) => c.at * 1000 >= clickAt);
  check(
    calls.length > 0 && calls.every((c) => c.status === 200),
    `every Vercel call answered 200 (${calls.map((c) => `${c.lane} ${c.status} ${c.s} s${c.cold ? " cold" : ""}`).join(", ")})`,
  );
  const models = stack.geminiCalls().filter((c) => c.kind === "model");
  check(models.length === 0, `no model request was made (${models.length})`);
  check(consoleErrors.length === 0, `no page errors${consoleErrors.length ? `: ${consoleErrors.slice(0, 3).join(" | ")}` : ""}`);

  // the stages, from the click
  const at = (k) => seen[k];
  const firstWake = calls.find((c) => c.lane === "add");
  const rows = [
    ["POST /api/request answered", posted?.at],
    ['card reads "Adding to the main page"', addingAt],
    ["the add run made", r?.run.created_at ? r.run.created_at * 1000 : null],
    ["the first wake-up reached Vercel", firstWake ? firstWake.at * 1000 : null],
    ["the run's first claim", r?.run.started_at ? r.run.started_at * 1000 : null],
    ["add.fetch done (fills, cleaned, whales listed)", at("add.fetch#0 done")],
    ["add.export done (page, index, request added)", at("add.export#0 done")],
    ["the run finished", r?.run.finished_at ? r.run.finished_at * 1000 : null],
    ['card reads "On the main page →" by itself', onMainAt],
    ["its link opened the market page", marketAt],
    ["home page lists it (same tab)", homeAt],
  ];
  console.log("\nstage (seconds after the click)");
  for (const [what, when] of rows) console.log(`  ${when ? String(secs(clickAt, when)).padStart(7) : "      -"}  ${what}`);
  const whales = (r?.jobs ?? []).filter((j) => j.kind === "add.whales").length;
  const cpu = (r?.jobs ?? []).reduce((s, j) => s + (j.cpu_ms ?? 0), 0);
  console.log(`\n  ${calls.length} Vercel call(s), ${whales} whale batch(es), ${(cpu / 1000).toFixed(1)} s of step CPU; on the main page ${secs(clickAt, onMainAt)} s after the click`);
} catch (e) {
  failures.push(e.message);
  console.log(`FAIL ${e.message}`);
  if (stack && pick) console.log(await diagnose(stack, pick.condition_id).catch((x) => `(no diagnosis: ${x.message})`));
} finally {
  await browser?.close();
  if (args.keep && stack) {
    console.log(`\nsmoke: the stack keeps running (site ${stack.urls.site}); Ctrl-C stops it`);
  } else {
    await stack?.stop({ keepState: failures.length > 0 });
    console.log(`\nsmoke: ${failures.length ? `FAILED (${failures.length})` : "passed"} in ${secs(t0)} s`);
    process.exit(failures.length ? 1 : 0);
  }
}

/** The add's run and jobs, the lanes and the Vercel calls, for a failure. */
async function diagnose(stack, cid) {
  const lines = ["", "backend:"];
  const id = await stack.runOf(cid);
  const got = id ? await stack.run(id) : null;
  lines.push(`  run ${id ?? "none"}: ${got?.run.state ?? "-"}`);
  for (const j of got?.jobs ?? []) lines.push(`    ${j.kind}#${j.part}: ${j.state} (attempt ${j.attempt}, laters ${j.laters}, partials ${j.partials})${j.error ? ` ${j.error}` : ""}`);
  const o = await stack.overview();
  for (const l of o.backend?.lanes ?? []) lines.push(`  lane ${l.lane}: unanswered ${l.unanswered}, wake_error ${l.wake_error ?? "-"}`);
  for (const c of (await stack.vercel.calls()).slice(-8)) lines.push(`  vercel ${c.lane}: ${c.status} in ${c.s} s, ${c.jobs ?? "-"} job(s)`);
  lines.push(`  logs: ${stack.dirs.logs}`);
  return lines.join("\n");
}
