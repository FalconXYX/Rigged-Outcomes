// The Watchlist's two lists after a real daily run, through the whole stack (tests/end-to-end/stack.mjs): the Worker's
// cron makes today's daily run, the local Vercel runs its steps (live Polymarket and Google News; Gemini is the
// harness's stand-in, which answers no model request), and the site reads what it publishes from the Worker.
//
// Before the run the stack holds a closed day an older count published with 5 picks (seed.py --crowd), and the
// "Will Jason or Lucia die in GTA VI?" market (live on Polymarket; GTA VI is out) as a current Open now card,
// stored the way the open list stores one, with a person's filing. Then:
//
//   three       Recently closed: every day the run published has at most 3 picks, the crowded day exactly 3, and
//               the page shows exactly 3 cards for it
//   released    the GTA VI card is gone from the published Open now list and from the page (its game is out, so
//               its answer is public), and its story is recorded as answered
//   toronto     with the browser in Tokyo and in UTC, every page (home, a market, the Watchlist, the review page)
//               shows Toronto days and times ("ET") and never "UTC"; Open now's "Updated" time is the run's in
//               Toronto; the daily run's day is Toronto's
//
//   node tests/end-to-end/scenarios/lists.mjs [--port-base 9560] [--state DIR] [--shots DIR] [--wait MIN]
//                                             [--headed] [--verbose] [--keep]
//
// The daily run takes a while (its Open now walk and news searches are real). Prints a table and exits 0 when
// every scenario passed.

import { writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { launch } from "../lib/browser.mjs";
import { secs, sleep } from "../lib/proc.mjs";
import { Report, norm, runLine } from "../lib/scenario.mjs";
import { toronto } from "../lib/toronto.mjs";
import { OWNER_TOKEN } from "../lib/worker.mjs";
import { parseArgs, stackOptions, startStack } from "../stack.mjs";

const args = parseArgs(process.argv.slice(2));
const opts = stackOptions({ "port-base": 9560, picks: 3, cards: 2, crowd: 5, daily: true, ...args, quiet: !args.verbose });
opts.cron = false; // the run starts once the GTA VI card is in place
const WAIT_MS = Number(args.wait ?? 75) * 60_000;
const report = new Report("lists", args.shots ?? join(tmpdir(), "e2e-shots", "lists"));
const say = (s) => report.say(s);

// The GTA VI market as a current Open now card: run in a stack workspace after `watchlist.state pull`; prints one
// JSON line last ({found, story, question, event}).
const GTA_PY = String.raw`
import json, os, sys
sys.path.insert(0, os.getcwd())
from watchlist import kinds as K, net, news, opennow as O, store
from watchlist.common import DAY, day_shift, local_day, local_iso, now_epoch
net.install()
now = now_epoch()
today = local_day(now)
ev = next((e for e in net.walk_open_events(min_volume=K.MIN_VOLUME)
           if "jason or lucia" in (e.get("title") or "").lower() and "gta" in (e.get("title") or "").lower()), None)
if ev is None:
    print(json.dumps({"found": False}))
    sys.exit(0)
m = sorted([x for x in ev.get("markets") or [] if not x.get("closed")] or ev["markets"], key=lambda x: x.get("endDate") or "")[0]
f = K.file_event(ev["title"], m["question"])
kind = f.kind if f is not None else "finished_content"
key = O.story_key(ev["title"])
conn = store.default_conn()
store.save_filing(conn, ev["id"], kind, {"question": m["question"]}, "person", story=key)
try:
    got = news.search(ev["title"], after=now - 21 * DAY, before=now + 2 * DAY, limit=6)
except news.NewsError:
    got = []
hs = [{"title": h["title"], "source": h.get("source") or "", "published": h.get("published"), "url": h["url"],
       "step": i == 0, "on_card": i == 0} for i, h in enumerate(h for h in got if h.get("title") and h.get("url"))]
card = {"id": key, "label": "listed", "listed_since": day_shift(today, -3), "place": "stays", "kind": kind,
        "group": K.group_of(kind), "question": m["question"],
        "timing": {"kind": "any_day", "until": (m.get("endDate") or "")[:10] or None, "last_step": None, "reveal": None,
                   "reveal_note": None},
        "headline": hs[0] if hs else None, "who_knows": K.KINDS[kind].who_knows, "why_watched": K.KINDS[kind].why,
        "other_side": None,
        "polymarket": {"question": m["question"], "other_dates": [], "url": f"https://polymarket.com/event/{ev['slug']}"},
        "headlines": hs, "checked_at": local_iso(now), "event_ids": [str(ev["id"])], "slugs": [ev["slug"], m["slug"]]}
OPEN = os.path.join("web", "public", "data", "open.json")
doc = json.load(open(OPEN))
day = doc["days"][0] if doc.get("days") else None
if day is None or day["day"] != today:
    day = {"day": today, "status": "updated", "run_at": local_iso(now), "cards": [], "new_today": 0, "held_back": 0,
           "awaiting": 0}
    doc["days"] = [day] + list(doc.get("days") or [])
day["cards"] = list(day["cards"]) + [card]
with open(OPEN, "w") as fh:
    json.dump(doc, fh, separators=(",", ":"))
version = (doc.get("rules") or {}).get("prompt_version")
for d in (day_shift(today, -1), today):
    cards = [c for c in day["cards"]] if d == today else [card]
    store.save_open_day(conn, d, "updated", local_iso(now - (DAY if d != today else 0)), cards, version=version, info={})
print(json.dumps({"found": True, "story": key, "question": m["question"], "event": str(ev["id"]), "kind": kind,
                  "headlines": len(hs)}))
`;

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

  // the GTA VI card, stored, saved and published
  const pulled = await stack.python(["-m", "watchlist.state", "pull"]);
  if (pulled.code !== 0) throw new Error(`state pull failed:\n${pulled.out.slice(-1500)}`);
  const py = join(stack.state, "gta-card.py");
  writeFileSync(py, GTA_PY);
  const made = await stack.python([py], { ws: pulled.ws });
  if (made.code !== 0) throw new Error(`the GTA VI card was not stored:\n${made.out.slice(-2000)}`);
  const gta = JSON.parse(made.out.trim().split("\n").pop());
  for (const cmd of [
    ["-m", "watchlist.state", "push", "--by", "local"],
    ["-m", "watchlist.sync", "catalog"],
  ]) {
    const res = await stack.python(cmd, { ws: pulled.ws });
    if (res.code !== 0) throw new Error(`${cmd.slice(1).join(" ")} failed:\n${res.out.slice(-1500)}`);
  }
  const before = await fetch(`${stack.urls.api}/api/data/open.json`).then((r) => r.json());
  const listedBefore = (before.days?.[0]?.cards ?? []).some((c) => c.id === gta.story);
  say(`GTA VI: ${gta.found ? `"${gta.question}" (event ${gta.event}, kind ${gta.kind}, ${gta.headlines} headline(s)) ${listedBefore ? "on" : "NOT on"} the published list` : "not found on Polymarket"}`);

  browser = await launch({ headed: !!args.headed });
  const tokyo = await browser.newContext({ viewport: { width: 1440, height: 1000 }, timezoneId: "Asia/Tokyo" });
  const page = await tokyo.newPage();
  page.errors = [];
  page.on("pageerror", (e) => page.errors.push(String(e)));
  let gtaShownBefore = false;
  if (gta.found) {
    await page.goto(`${stack.urls.site}#/watchlist`);
    await page.locator("article.cl-card").first().waitFor({ timeout: 60_000 });
    gtaShownBefore = (await page.locator("article.op-card h3.cl-card-title", { hasText: gta.question }).count()) > 0;
  }

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
      await page.reload(); // a new # address alone keeps the list the page read before the run
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

  // released: the GTA VI card is gone
  {
    if (!gta.found) report.skip("a released-game market is dropped from Open now", "the GTA VI market was not found among Polymarket's open events");
    else {
      const open = await fetch(`${stack.urls.api}/api/data/open.json`).then((r) => r.json());
      // today's list, the one the page shows (older days are kept as they were published)
      const cardsNow = (open.days?.[0]?.cards ?? []).map((c) => c.id);
      const onList = cardsNow.includes(gta.story);
      await page.goto(`${stack.urls.site}#/watchlist`);
      await page.reload();
      await page.locator("article.cl-card").first().waitFor({ timeout: 60_000 });
      await sleep(1500);
      const onPage = (await page.locator("article.op-card h3.cl-card-title", { hasText: gta.question }).count()) > 0;
      const check = await stack.python(["-c", `from watchlist import store; print("${gta.story}" in store.open_answered(store.default_conn()))`], {
        ws: (await stack.python(["-m", "watchlist.state", "pull"])).ws,
      });
      const answered = /True/.test(check.out.trim().split("\n").pop() ?? "");
      report.row(
        "a released-game market is dropped from Open now",
        `"${gta.question}" (GTA VI is out, so its answer is public) is not on the published Open now list nor on the page after the daily run, and its story is recorded as answered`,
        `before the run: ${listedBefore ? "published" : "not published"}, ${gtaShownBefore ? "on the page" : "not on the page"}; after: ${onList ? "still published" : "not published"} (${cardsNow.length} card id(s) in the list), ${onPage ? "still on the page" : "not on the page"}; answered in the state: ${answered}`,
        listedBefore && !onList && !onPage && answered,
      );
      if (onPage) await report.shot(page, "released");
    }
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
      await p.goto(`${stack.urls.site}#/review`);
      await p.locator("form.rv-unlock input[type=password]").fill(OWNER_TOKEN);
      await p.locator(".rv-unlock button[type=submit]").click();
      await p.locator("section[aria-labelledby=rv-backend]").waitFor({ timeout: 30_000 });
      await sleep(1000);
      const bad = await utcLines(p);
      const backend = norm(await p.locator("section[aria-labelledby=rv-backend]").innerText());
      const ranET = /\d{2}:\d{2}(–\d{2}:\d{2})? ET/.test(backend);
      notes.push(`${tz} review page: ${bad.length ? `UTC in "${bad.slice(0, 2).join(" | ")}"` : "no UTC"}, the runs' times ${ranET ? "in ET" : "not in ET"}`);
      ok &&= !bad.length && ranET;
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
