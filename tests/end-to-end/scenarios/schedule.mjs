// The backend's clock and its budget, through the whole stack (tests/end-to-end/stack.mjs): the Worker's one
// every-minute Cron Trigger deciding by Toronto time, and the rolling 30-day ledger that keeps Vercel Hobby from
// ever pausing the project. Time is moved on the Worker's own clock (POST /api/dev/clock) and the trigger fired
// through wrangler's scheduled path, as Cloudflare fires it; ledger rows are written into the stack's D1 with the
// Worker stopped for it. In order, on one stack:
//
//   budget low     30 days of use past 65% of the allowance: a visitor's add waits, its card reading "Adding to
//                  the main page" with nothing hedging and no error; Vercel is never woken for it; the review page
//                  says the budget is low
//   budget stop    today's use past its cap: nothing is claimed and nothing woken; the next day's 04:00 daily run
//                  is recorded "skipped"
//   back to ok     the use gone: the waiting add is woken and lands by itself on the page left open
//   missed day     the cron silent for a whole day: that day's run is recorded "missed" (the review page says so),
//                  and the next day's starts at 04:00
//   04:00 Toronto  on the days the clocks change (2027-03-14 and 2027-11-07): nothing at 03:59, the daily run at
//                  04:00 Toronto with its 09:30 deadline; the newer run cancels the older one still queued
//   catch-up       10:00 Toronto: a catch-up only after a daily run that left work, none otherwise
//
//   node tests/end-to-end/scenarios/schedule.mjs [--port-base 9580] [--state DIR] [--shots DIR] [--headed]
//                                                [--verbose] [--keep]
//
// Vercel is set to answer 503 once the clock jumps ahead, so the daily runs made here never do real work.

import { tmpdir } from "node:os";
import { join } from "node:path";
import { launch } from "../lib/browser.mjs";
import { secs, sleep, until } from "../lib/proc.mjs";
import { Report, addButton, addRuns, callsLine, faceOf, hedges, onMainLink, openWatchlist, ownerSees, runLine, watch } from "../lib/scenario.mjs";
import { dayShift, toronto, torontoAt } from "../lib/toronto.mjs";
import { parseArgs, stackOptions, startStack } from "../stack.mjs";

const args = parseArgs(process.argv.slice(2));
const opts = stackOptions({ "port-base": 9580, picks: 1, cards: 0, ...args, quiet: !args.verbose });
const report = new Report("schedule", args.shots ?? join(tmpdir(), "e2e-shots", "schedule"));
const say = (s) => report.say(s);
const MIN = 60_000;

/** usage rows: [day, kind, cpu_ms, gb_ms] as SQL, and the budget made to be counted again on the next tick. */
const usageSql = (rows) =>
  [
    ...rows.map(([day, kind, cpu, gb]) => `INSERT OR REPLACE INTO usage (day, kind, steps, cpu_ms, gb_ms, wakes, units) VALUES ('${day}', '${kind}', 10, ${cpu}, ${gb}, 0, 0)`),
    "DELETE FROM settings WHERE key = 'budget'",
  ].join("; ");

let stack;
let browser;
try {
  say(`starting a stack on ports ${opts.portBase}-${opts.portBase + 3}`);
  stack = await startStack(opts);
  const D = stack.today;
  const p = stack.seed.picks[0];
  browser = await launch({ headed: !!args.headed });
  const level = async () => (await stack.overview()).backend?.budget?.level;
  const runState = async (id) => (await stack.run(id))?.run?.state ?? null;

  // ── budget low: 10 earlier days at 12.7 CPU-min each (×1.25 safety: 66% of 4 h), today nothing ──
  const past = Array.from({ length: 10 }, (_, i) => [dayShift(D, -(i + 1)), "open.read", 760_000, 2_000_000]);
  await stack.sql(usageSql(past));
  await stack.tick();
  const low = await level();
  const { context, page } = await openWatchlist(browser, stack, p);
  const clickAt = Date.now();
  const w = watch(clickAt, { card: () => faceOf(page, p) }, "budget", say);
  await addButton(page, p).click();
  await sleep(2.5 * MIN); // two or three ticks of the cron
  const waiting = await faceOf(page, p);
  const run = (await addRuns(stack, p.condition_id))[0];
  const addCalls = (await stack.vercel.calls()).filter((c) => c.lane === "add" && c.at * 1000 >= clickAt);
  const owner = await ownerSees(browser, stack);
  report.row(
    "budget low: an add waits",
    'the level is "low"; the card keeps reading "Adding to the main page" (no error, nothing hedging); its run waits queued and Vercel is never woken for it; the review page says the budget is low',
    `level ${low}; the card: ${w.seen("card")}; ${run ? await runLine(stack, run.id, clickAt) : "no add run"}; add-lane Vercel calls: ${addCalls.length}; review page: ${owner.includes("Budget low") ? '"Budget low: …"' : `no "Budget low" (${owner.slice(0, 120)})`}`,
    low === "low" && waiting === "Adding to the main page" && !hedges(w).length && run?.state === "queued" && !addCalls.length && owner.includes("Budget low"),
  );

  // ── budget stop: today's use past its 20 CPU-min cap, and the 30 days past 80% (so tomorrow is still over) ──
  await stack.sql(usageSql([[D, "open.read", 2_000_000, 2_000_000]]));
  await stack.tick();
  const stop = await level();
  const callsBefore = (await stack.vercel.calls()).length;
  const claims = await Promise.all(["main", "add", "news", "model"].map((lane) => stack.api("POST", "/api/job/claim", { lane, left_s: 270 })));
  await sleep(70_000);
  await stack.tick();
  const callsAfter = (await stack.vercel.calls()).length;
  const tomorrow = dayShift(D, 1);
  await stack.cron.stop();
  await stack.clock(torontoAt(tomorrow, "04:00"));
  await stack.tick();
  const skipped = await runState(`daily:${tomorrow}`);
  report.row(
    "budget stop: nothing runs",
    'the level is "stop": every claim gets nothing (204), no wake-up goes to Vercel, and the next 04:00 daily run is recorded "skipped"',
    `level ${stop}; claims ${claims.map((c) => c.status).join(", ")}; Vercel calls over a minute and a tick: ${callsAfter - callsBefore}; daily:${tomorrow} ${skipped}`,
    stop === "stop" && claims.every((c) => c.status === 204) && callsAfter === callsBefore && skipped === "skipped",
  );

  // ── back to ok: the waiting add lands on the page left open ──
  await stack.sql(`DELETE FROM usage; DELETE FROM settings WHERE key = 'budget'`);
  const backAt = Date.now();
  stack.cron.start();
  await stack.tick();
  const ok = await level();
  const landed = await onMainLink(page, p)
    .waitFor({ timeout: 8 * MIN })
    .then(() => Date.now(), () => null);
  await w.stop();
  const addRun = (await addRuns(stack, p.condition_id))[0];
  report.row(
    "back under budget: the waiting add lands",
    'the level is "ok" again; the add that waited is woken and the card reads "On the main page →" by itself (adds wait, they are never lost)',
    `level ${ok}; ${landed ? `landed ${secs(backAt, landed)} s after the use was gone` : "never landed"}; ${await runLine(stack, addRun?.id, clickAt)}; ${await callsLine(stack, backAt, "add")}; the card: ${w.seen("card")}`,
    ok === "ok" && !!landed && !hedges(w).length,
  );
  if (!landed) await report.shot(page, "budget-back");
  await context.close();

  // from here the daily runs made are never woken into real work: Vercel answers 503
  await stack.cron.stop();
  await stack.vercel.fault({ status: 503, error: "DEPLOYMENT_PAUSED" });

  // ── a missed cron day: no tick at all on D+2, the next tick on D+3 at 04:05 ──
  const d2 = dayShift(D, 2);
  const d3 = dayShift(D, 3);
  await stack.clock(torontoAt(d3, "04:05"));
  await stack.tick();
  const missed = await runState(`daily:${d2}`);
  const made3 = await stack.run(`daily:${d3}`);
  const ownerMissed = await ownerSees(browser, stack);
  report.row(
    "a missed cron day",
    `a day with no tick at all (${d2}) is recorded "missed" and the review page says so; the next day's run (${d3}) is made by its first tick after 04:00`,
    `daily:${d2} ${missed}; daily:${d3} ${made3?.run.state ?? "not made"} (deadline ${made3 ? new Date(made3.run.deadline * 1000).toISOString() : "-"}); review page ${/missed: no run that day/.test(ownerMissed) ? 'says "missed: no run that day"' : "does not say missed"}`,
    missed === "missed" && ["queued", "running"].includes(made3?.run.state) && /missed: no run that day/.test(ownerMissed),
  );

  // ── 04:00 Toronto on the days the clocks change ──
  const notes = [];
  let ok4 = true;
  let older = `daily:${d3}`;
  for (const day of ["2027-03-14", "2027-11-07"]) {
    const at4 = torontoAt(day, "04:00");
    // the older run still within its deadline (the jump is months ahead, and a run an hour past its deadline is
    // ended by the tick's cleanup before the next day's run could replace it)
    await stack.sql(`UPDATE runs SET deadline = ${at4 + 7200} WHERE id = '${older}'`);
    await stack.clock(at4 - 60);
    await stack.tick();
    const early = await runState(`daily:${day}`);
    await stack.clock(at4);
    await stack.tick();
    const got = await stack.run(`daily:${day}`);
    const deadline = got?.run.deadline;
    const want = torontoAt(day, "09:30");
    const prev = await runState(older);
    notes.push(
      `${day}: 03:59 Toronto (${new Date((at4 - 60) * 1000).toISOString().slice(11, 16)} by the trigger's UTC) ${early ?? "no run"}; 04:00 Toronto (${new Date(at4 * 1000).toISOString().slice(11, 16)}) ${got?.run.state ?? "no run"}, deadline ${deadline === want ? "09:30 Toronto" : deadline}; the older ${older} ${prev}`,
    );
    ok4 &&= early === null && ["queued", "running"].includes(got?.run.state) && deadline === want && prev === "cancelled" && toronto(at4).minutes === 240;
    older = `daily:${day}`;
  }
  report.row("the daily run starts at 04:00 Toronto, daylight saving included", "nothing at 03:59 Toronto, the day's run at 04:00 Toronto (08:00 UTC in summer time, 09:00 in winter) with a 09:30 deadline; the newer run cancels the older one still queued", notes.join("; "), ok4);

  // ── the catch-up: only after a run that left work ──
  const withWork = "2027-11-07";
  const noWork = "2027-11-09";
  await stack.sql(
    [
      `UPDATE jobs SET state = 'cancelled' WHERE run_id = 'daily:${withWork}' AND state IN ('queued', 'leased')`,
      `UPDATE runs SET state = 'done', meta = '{"left": 4}', finished_at = ${torontoAt(withWork, "06:00")} WHERE id = 'daily:${withWork}'`,
      `INSERT OR REPLACE INTO runs (id, kind, slot, state, meta, created_at, started_at, deadline, finished_at) VALUES ('daily:${noWork}', 'daily', '${noWork}', 'done', '{"left": 0}', ${torontoAt(noWork, "04:00")}, ${torontoAt(noWork, "04:00")}, ${torontoAt(noWork, "09:30")}, ${torontoAt(noWork, "06:00")})`,
    ].join("; "),
  );
  const cnotes = [];
  let okc = true;
  for (const [day, want] of [
    [withWork, "made"],
    [noWork, "none"],
  ]) {
    await stack.clock(torontoAt(day, "09:59"));
    await stack.tick();
    const before = await runState(`catchup:${day}`);
    await stack.clock(torontoAt(day, "10:00"));
    await stack.tick();
    const got = await stack.run(`catchup:${day}`);
    cnotes.push(`${day} (${want === "made" ? "4 stories left" : "nothing left"}): 09:59 ${before ?? "no catch-up"}, 10:00 ${got ? `catch-up ${got.run.state} (${got.jobs.map((j) => j.kind).join(", ")})` : "no catch-up"}`);
    okc &&= before === null && (want === "made" ? !!got && got.jobs.some((j) => j.kind === "open.read") : !got);
  }
  report.row("a catch-up at 10:00 Toronto only when work was left", "a catch-up run at 10:00 Toronto after a daily run that left stories unread, and none after one that left nothing", cnotes.join("; "), okc);
  await stack.clock(null);
} catch (e) {
  report.row("schedule (setup)", "the stack and its clock", `stopped: ${e.stack ?? e.message}`.slice(0, 800), false);
} finally {
  await browser?.close().catch(() => {});
  if (args.keep && stack) say(`the stack keeps running (site ${stack.urls.site}); Ctrl-C stops it`);
  else await stack?.stop({ keepState: report.rows.some((r) => !r.pass) });
}
if (!args.keep) report.finish();
