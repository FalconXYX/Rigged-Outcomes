// The Watchlist's two lists after a real daily run, through the whole stack (tests/end-to-end/stack.mjs): the Worker's
// cron makes today's daily run, the local Vercel runs its steps (live Polymarket and Google News; Gemini is the
// harness's stand-in, which answers no model request), and the site reads what it publishes from the Worker.
//
// Before the run the stack holds a closed day an older count published with 5 picks (seed.py --crowd). Then:
//
//   three       Recently closed: every day the run published has at most 3 picks, the crowded day exactly 3, and
//               the page shows exactly 3 cards for it
//   toronto     with the browser in Tokyo and in UTC, every page (home, a market, the Watchlist)
//               shows Toronto days and times ("ET") and never "UTC"; Open now's "Updated" time is the run's in
//               Toronto; the daily run's day is Toronto's
//
//   node tests/end-to-end/scenarios/lists.mjs [--port-base 9560] [--state DIR] [--shots DIR] [--wait MIN]
//                                             [--headed] [--verbose] [--keep]
//
// The daily run takes a while (its Open now walk and news searches are real). Prints a table and exits 0 when
// every scenario passed.

import { tmpdir } from "node:os";
import { join } from "node:path";
import { launch } from "../lib/browser.mjs";
import { secs, sleep } from "../lib/proc.mjs";
import { Report, norm, runLine } from "../lib/scenario.mjs";
import { toronto } from "../lib/toronto.mjs";
import { parseArgs, stackOptions, startStack } from "../stack.mjs";

const args = parseArgs(process.argv.slice(2));
const opts = stackOptions({ "port-base": 9560, picks: 3, cards: 2, crowd: 5, daily: true, ...args, quiet: !args.verbose });
opts.cron = false; // the scenario starts the cron itself, just before the daily run
const WAIT_MS = Number(args.wait ?? 75) * 60_000;
const report = new Report("lists", args.shots ?? join(tmpdir(), "e2e-shots", "lists"));
const say = (s) => report.say(s);

/** The page's text has no "UTC"; returns the lines that have it. */
async function utcLines(page) {
  const t = await page.locator("body").innerText().catch(() => "");
  return t.split("\n").filter((l) => /\bUTC\b/.test(l)).map(norm);
}

/** "Nov 19, 04:31 ET" for epoch seconds, as the site writes Toronto stamps. */
function stampET(epoch) {
  const f = new Intl.DateTimeFormat("en-US", { timeZone: "America/Toronto", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
  const p = Object.fromEntries(f.formatToParts(new Date(epoch * 1000)).map((x) => [x.type, x.value]));
  return `${p.month} ${p.day}, ${p.hour}:${p.minute} ET`;
}

let stack;
let browser;
const t0 = Date.now();
try {
  say(`starting a stack on ports ${opts.portBase}-${opts.portBase + 3}`);
  stack = await startStack(opts);
  const crowd = stack.seed.picks.filter((p) => p.crowd);
  const crowdDay = crowd[0]?.day;
  say(`stack ready in ${secs(t0)} s; crowded day ${crowdDay ?? "none"} (${crowd.length} picks)`);

  browser = await launch({ headed: !!args.headed });
  const tokyo = await browser.newContext({ viewport: { width: 1440, height: 1000 }, timezoneId: "Asia/Tokyo" });
  const page = await tokyo.newPage();
  page.errors = [];
  page.on("pageerror", (e) => page.errors.push(String(e)));

  // the daily run: the cron from now on, as Cloudflare fires it
  const runAt = Date.now();
  stack.cron.start();
  await stack.tick();
  const daily = `daily:${stack.today}`;
  say(`the daily run ${daily} is made; waiting for it to end (up to ${WAIT_MS / 60_000} min)`);
  let last = "";
  const progress = setInterval(async () => {
    const got = await stack.run(daily).catch(() => null);
    const line = got ? got.jobs.map((j) => `${j.kind}:${j.state}`).join(" ") : "";
    if (line !== last) say(`  ${line}`);
    last = line;
  }, 15_000);
  const end = await stack.waitRun(daily, { ms: WAIT_MS }).catch(() => null);
  clearInterval(progress);
  const runText = await runLine(stack, daily, runAt);
  say(runText);
  if (!end) throw new Error(`the daily run did not end within ${WAIT_MS / 60_000} min: ${runText}`);

  // three: at most 3 a day, the crowded day exactly 3, on screen too
  {
    const closed = await fetch(`${stack.urls.api}/api/data/closed.json`).then((r) => r.json());
    const counts = (closed.days ?? []).map((d) => [d.day, (d.picks ?? []).length]);
    const crowded = counts.find(([d]) => d === crowdDay)?.[1] ?? null;
    let shown = null;
    let rule = "";
    if (crowdDay) {
      await page.goto(`${stack.urls.site}#/watchlist?day=${crowdDay}`);
      await page.reload(); // a fresh load, so the page reads the lists the run published
      await page.locator("article.cl-card:not(.op-card)").first().waitFor({ timeout: 60_000 });
      await sleep(1500);
      shown = await page.locator("article.cl-card:not(.op-card)").count();
      rule = norm(await page.locator("body").innerText()).match(/[^.]*3 a day[^.]*/)?.[0] ?? "";
    }
    report.row(
      "Recently closed shows exactly 3",
      "every day the run published has at most 3 picks; the day an older count listed with 5 now has exactly 3, and the page shows exactly 3 cards for it",
      `published days: ${counts.map(([d, n]) => `${d} ${n}`).join(", ")}; crowded day ${crowdDay}: ${crowded} published, ${shown} on the page${rule ? `; the page says "${rule.trim()}"` : ""}`,
      counts.length > 0 && counts.every(([, n]) => n <= 3) && crowded === 3 && shown === 3,
    );
    if (shown !== 3) await report.shot(page, "three");
  }

  // toronto: no UTC anywhere, times in ET, in Tokyo and in UTC
  {
    const notes = [];
    let ok = true;
    const open = await fetch(`${stack.urls.api}/api/data/open.json`).then((r) => r.json());
    // the list's day was updated at its run_at (or, when the day is older, the last run's time): Toronto's clock
    const stamps = [open.days?.[0]?.run_at, open.generated_at].filter(Boolean).map((x) => stampET(Date.parse(x) / 1000));
    for (const tz of ["Asia/Tokyo", "UTC"]) {
      const ctx = await browser.newContext({ viewport: { width: 1440, height: 1000 }, timezoneId: tz });
      const p = await ctx.newPage();
      const pages = [
        ["home", "#/", "a.mc-link"],
        ["market", "#/m/fired", "h1.market-title"],
        ["watchlist", "#/watchlist", "article.cl-card"],
      ];
      for (const [what, hash, sel] of pages) {
        await p.goto(`${stack.urls.site}${hash}`);
        await p.locator(sel).first().waitFor({ timeout: 60_000 });
        await sleep(1500);
        const bad = await utcLines(p);
        notes.push(`${tz} ${what}: ${bad.length ? `UTC in "${bad.slice(0, 2).join(" | ")}"` : "no UTC"}`);
        ok &&= !bad.length;
      }
      const body = norm(await p.locator("body").innerText());
      // the label's style writes it in capitals: compared case-blind
      const updated = body.match(/Updated ([A-Z][a-z]{2} \d{1,2}, \d{2}:\d{2} ET)/i)?.[1] ?? null;
      notes.push(`${tz} Open now "Updated ${updated}" (the run's Toronto time: ${stamps.join(" or ")})`);
      ok &&= !!updated && stamps.map((x) => x.toUpperCase()).includes(updated.toUpperCase());
      await ctx.close();
    }
    const runDay = (await stack.run(daily))?.run;
    const torontoDay = runDay ? toronto(runDay.created_at).day : null;
    notes.push(`the daily run's day ${stack.today}, the Toronto day it was made on ${torontoDay}`);
    ok &&= torontoDay === stack.today;
    report.row("days and times on screen are Toronto time with no UTC", 'a browser in Tokyo or in UTC sees Toronto days and times ("ET") on every page and never "UTC"; the run\'s day is Toronto\'s', notes.join("; "), ok);
  }
  if (page.errors.length) report.row("no page errors", "none", page.errors.slice(0, 3).join(" | "), false);
} catch (e) {
  report.row("lists (setup)", "the stack and the daily run", `stopped: ${e.stack ?? e.message}`.slice(0, 800), false);
} finally {
  await browser?.close().catch(() => {});
  if (args.keep && stack) say(`the stack keeps running (site ${stack.urls.site}); Ctrl-C stops it`);
  else await stack?.stop({ keepState: report.rows.some((r) => !r.pass) });
}
if (!args.keep) report.finish();
