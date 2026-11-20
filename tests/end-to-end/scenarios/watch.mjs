// "Watch this market" on the Open now cards, and the daily run adding a watched market once it closes, the way a
// person uses them, through the whole stack with real timing (tests/end-to-end/stack.mjs). Pages are clicked, left
// open and reloaded; nothing is run by hand after a click: a visitor's add queues its own add run, which the Worker
// wakes the local Vercel for, and the watched markets wait for the next daily run (watch.check, once a day), made
// by the Worker's cron at 04:00 Toronto (the Worker's clock moved to the next 04:00).
//
// The stack's seed has live open cards only, and no card in the backend's state (open_cards), so on top of it this
// adds four Open now cards whose markets have already closed, the way the open list stores them (the state
// pulled, the cards stored for today and yesterday, the state and site:open.json pushed, the catalog sent):
//
//   W1      the first closed pick's market as its own story: watched, never clicked. The daily run should add
//           it, and its Recently closed card read "On the main page →"
//   W2      the second closed pick's market: watched, and a visitor also presses "Add to main page" on its
//           closed card. It must be added once
//   ladder  a real date ladder (or day ladder) event that closed with a result: the run adds the one market
//           watched_choice picks
//   unclear a real market that closed 50-50: it waits
//
// Then, on the same stack: watch / stop watching (card face and details panel), reload, a second browser
// context, quick toggles, and the Worker down (the browser's requests to the API refused); and on a second
// stack with Turnstile's always-failing keys, the bot check failing.
//
//   node tests/end-to-end/scenarios/watch.mjs [--port-base 9600] [--second-run] [--fast] [--only main,bot,slow]
//                                             [--headed] [--verbose] [--shots DIR] [--state DIR] [--keep]
//
// --fast leaves out the scenarios that wait 150 s each (a second context left open, the Worker down). Each part's
// stack (its state folder watch-<part> under --state) removes the folder when the part passed (--keep keeps it); a
// wait for an add to land stops as soon as the outcome is clearly wrong (lib/outcome.mjs).
// Prints one line per stage while it goes and a table at the end. Exit 0 all passed, 1 any failed.

import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { launch } from "../lib/browser.mjs";
import { addGoesWrong, showsUnless } from "../lib/outcome.mjs";
import { secs, sleep, until } from "../lib/proc.mjs";
import { dayShift, torontoAt } from "../lib/toronto.mjs";
import { parseArgs, stackOptions, startStack } from "../stack.mjs";

const args = parseArgs(process.argv.slice(2));
const BASE = Number(args["port-base"] ?? 9600);
const SCRATCH = "/private/tmp/claude-501/-Users-parthjain-Documents-Code-CCT416-Rigged-Outcomes/be509dc4-4b5f-41b9-a722-8a1126673a44/scratchpad";
const SHOTS = args.shots ?? join(SCRATCH, "e2e-shots", "watch");
const ONLY = args.only ? new Set(String(args.only).split(",")) : new Set(["main", "bot", "slow"]);
const STATE = (part) => join(args.state ?? join(SCRATCH, "e2e-state"), `watch-${part}`);
/** Whether a row from `from` on failed (the part's stack then keeps its state folder). */
const failedSince = (from) => rows.slice(from).some((r) => !r.ok);
mkdirSync(SHOTS, { recursive: true });

const rows = [];
const T0 = Date.now();
const log = (s) => console.log(`[watch +${secs(T0)} s] ${s}`);
let shotN = 0;

/** One scenario's row; a failing one gets a screenshot of `page`. */
async function row(scenario, expected, happened, ok, page) {
  let shot = "";
  if (!ok && page) {
    shot = join(SHOTS, `${String(++shotN).padStart(2, "0")}-${scenario.replace(/[^a-z0-9]+/gi, "-").slice(0, 60)}.png`);
    await page.screenshot({ path: shot, fullPage: true }).catch(() => (shot = ""));
  }
  rows.push({ scenario, expected, happened, ok, shot });
  log(`${ok ? "PASS" : "FAIL"} ${scenario}: ${happened}${shot ? ` (screenshot ${shot})` : ""}`);
}

// ---------- what a person sees ----------

const byTitle = (page, sel, q) => page.locator(sel).filter({ has: page.locator("h3.cl-card-title", { hasText: q.slice(0, 80) }) });
const openCard = (page, q) => byTitle(page, "article.op-card", q).first();
const closedCard = (page, q) => byTitle(page, "article.cl-card:not(.op-card)", q).first();

/** The action on a card's face ("Watch this market", "Watching Stop watching", an error after it). */
async function face(card) {
  const t = await card
    .locator(".cl-card-foot .wl-act, .wl-act")
    .first()
    .innerText({ timeout: 2000 })
    .catch(() => "(no action shown)");
  return t.replace(/\s+/g, " ").trim();
}

/** Waits until the card's face matches `re`; the ms it took, or null after `ms`. */
async function waitFace(card, re, ms = 20_000) {
  const t = Date.now();
  while (Date.now() - t < ms) {
    if (re.test(await face(card))) return Date.now() - t;
    await sleep(200);
  }
  return null;
}

/** Phrases the person must never read unless something they must act on failed (the global no-hedging rule). */
const HEDGE = /not available|unavailable|coming soon|\bbeta\b|\btrial\b|paused|not updated|may take|up to a day|within a day|next (?:daily )?run|kept in this browser|saved locally|offline|couldn't send|try again|pending|queued|waiting for|will be added/gi;
let baseline = new Set();
async function hedges(page) {
  const t = await page.locator("body").innerText().catch(() => "");
  return [...new Set((t.match(HEDGE) ?? []).map((s) => s.toLowerCase()))].filter((s) => !baseline.has(s));
}

const ms = (x) => (x == null ? "never" : `${(x / 1000).toFixed(1)} s`);

// ---------- the stack's side ----------

const apiGet = (stack, path) => fetch(`${stack.urls.api}${path}`).then((r) => r.json(), () => null);
const watching = async (stack, story) => (await apiGet(stack, `/api/watch?story=${encodeURIComponent(story)}`))?.watching;
const request = async (stack, cid) => (await apiGet(stack, `/api/request?cid=${cid}`))?.request ?? null;
const status = (stack) => stack.status();
/** The stories on the site's watchlist, as the job reads them. */
const watchedNow = async (stack) => ((await stack.api("GET", "/api/job/work")).body?.watched ?? []).map((w) => w.story);
const addedBlobs = async (stack) => (await stack.api("GET", "/api/job/blobs?prefix=market:")).body?.blobs ?? [];
/** The markets' index the Worker serves (the 7 original markets, then the added ones). */
const siteIndex = (stack) => fetch(`${stack.urls.api}/api/data/index.json`).then((r) => r.json(), () => ({ markets: [] }));
const bare = (s) => String(s ?? "").split("/").pop().split("#")[0];
const onIndex = (idx, slug) => (idx.markets ?? []).filter((m) => bare(m.slug) === bare(slug));

/** The backend's add runs (newest first), from the backend's status. */
const addRunsAll = async (stack) => ((await status(stack)).runs ?? []).filter((r) => r.kind === "add");
/** Waits until no add run is queued or running. */
const addsIdle = (stack) =>
  until(async () => !(await addRunsAll(stack)).some((r) => ["queued", "running"].includes(r.state)), { ms: 20 * 60_000, every: 3000, what: "the add runs to end" });

/** The daily run of the next 04:00 Toronto on the Worker's clock: moved there, and the cron's tick makes it. */
async function nextDaily(stack, after) {
  const day = dayShift(after, 1);
  await stack.clock(torontoAt(day, "04:00"));
  await stack.tick();
  return { id: `daily:${day}`, day };
}

// The four extra Open now cards (see the top), stored the way the open list stores its cards. Run in a stack
// workspace after `watchlist.state pull`; prints one JSON line last.
const CARDS_PY = String.raw`
import json, os, sys
sys.path.insert(0, os.getcwd())
import polymarket_api
from watchlist import filters, kinds, net, news, opennow, store, sync
from watchlist.common import DAY, day_shift, local_bounds, local_day, local_iso, now_epoch

net.install()
arg = json.loads(sys.argv[1])
now = now_epoch()
today = local_day(now)
yesterday = day_shift(today, -1)
since = local_bounds(yesterday)[0]
OPEN = os.path.join("web", "public", "data", "open.json")
doc = json.load(open(OPEN))

def heads(q):
    try:
        got = news.search(q, after=now - 21 * DAY, before=now + 2 * DAY, limit=6)
    except news.NewsError:
        return []
    got = [h for h in got if h.get("title") and h.get("url")]
    return [{"title": h["title"], "source": h.get("source") or "", "published": h.get("published"), "url": h["url"],
             "step": i == 0, "on_card": i == 0} for i, h in enumerate(got)]

def card(title, question, event_slug, end, event_ids, slugs):
    f = kinds.file_event(title, question)
    k = f.kind if f is not None and f.kind in kinds.KINDS else "emergency_measure"
    hs = heads(title)
    return {"id": opennow.story_key(title), "label": "new", "listed_since": today, "place": "new step", "kind": k,
            "group": (f.group if f is not None else None) or 1, "question": question,
            "timing": {"kind": "any_day", "until": (end or "")[:10] or None, "last_step": None, "reveal": None,
                       "reveal_note": None},
            "headline": hs[0] if hs else None,
            "who_knows": "If it happens, the few people deciding it will know before the public does.",
            "why_watched": kinds.KINDS[k].why, "other_side": None,
            "polymarket": {"question": question, "other_dates": [], "url": f"https://polymarket.com/event/{event_slug}"},
            "headlines": hs, "checked_at": local_iso(now), "event_ids": event_ids, "slugs": slugs}

def with_head(ev):
    head = {k: ev.get(k) for k in ("id", "slug", "title")}
    return [m if filters.event_of(m) else dict(m, events=[head]) for m in ev.get("markets") or []]

out, cards = {}, []
for name, p in (("w1", arg["picks"][0]), ("w2", arg["picks"][1])):
    m = polymarket_api.fetch_market(p["slug"])
    ev = filters.event_of(m)
    c = card(m["question"], m["question"], ev.get("slug") or m["slug"], m.get("endDate"), [], [m["slug"]])
    ch = sync.watched_choice([m])
    cards.append(c)
    out[name] = {"story": c["id"], "question": c["question"], "pick": p["condition_id"],
                 "expect": ch and {"cid": ch["conditionId"].lower(), "slug": ch["slug"], "question": ch["question"]}}

def ladder_events():
    for eid in arg.get("ladders") or []:
        yield eid
    seen = set()
    for m in net.closed_markets(since, now, min_volume=5000):
        eid = str(filters.event_of(m).get("id") or "")
        if eid and eid not in seen:
            seen.add(eid)
            yield eid

for eid in ladder_events():
    try:
        ev = net.fetch_event(eid)
    except Exception:
        continue
    ms = with_head(ev)
    ch = sync.watched_choice(ms)
    if len(ms) < 3 or ch is None or filters.volume(ch) > 250_000 or kinds.closed_exclusion(ch.get("question"), ev.get("title")):
        continue
    first = sorted(ms, key=lambda x: x.get("endDate") or "")[0]
    c = card(ev["title"], first["question"], ev["slug"], first.get("endDate"), [str(ev["id"])],
             [ev["slug"]] + [x["slug"] for x in ms if x.get("slug")][:20])
    cards.append(c)
    out["ladder"] = {"story": c["id"], "question": c["question"], "event": str(ev["id"]), "title": ev["title"],
                     "markets": len(ms), "open": sum(1 for x in ms if not x.get("closed")),
                     "cids": [str(x.get("conditionId")).lower() for x in ms],
                     "expect": {"cid": ch["conditionId"].lower(), "slug": ch["slug"], "question": ch["question"]}}
    break

def unclear_events():
    for eid in arg.get("unclear") or []:
        yield eid
    for m in net.closed_markets(since, now, min_volume=5000):
        if not filters.winning_outcome(m):
            yield str(filters.event_of(m).get("id") or "")

for eid in unclear_events():
    try:
        ev = net.fetch_event(eid)
    except Exception:
        continue
    ms = with_head(ev)
    if not ms or any(not x.get("closed") for x in ms) or sync.watched_choice(ms) is not None:
        continue
    c = card(ev["title"], ms[0]["question"], ev["slug"], ms[0].get("endDate"), [str(ev["id"])],
             [ev["slug"]] + [x["slug"] for x in ms if x.get("slug")][:20])
    cards.append(c)
    out["unclear"] = {"story": c["id"], "question": c["question"], "event": str(ev["id"]), "title": ev["title"],
                      "cids": [str(x.get("conditionId")).lower() for x in ms],
                      "prices": [x.get("outcomePrices") for x in ms][:3]}
    break

day = doc["days"][0] if doc.get("days") else None
if day is None or day["day"] != today:
    day = {"day": today, "status": "updated", "run_at": local_iso(now), "cards": [],
           "new_today": 0, "held_back": 0}
    doc["days"] = [day] + list(doc.get("days") or [])
day["cards"] = list(day["cards"]) + cards
day["new_today"] = len(day["cards"])
with open(OPEN, "w") as f:
    json.dump(doc, f, separators=(",", ":"))
conn = store.default_conn()
version = (doc.get("rules") or {}).get("prompt_version")
store.save_open_day(conn, today, "updated", day["run_at"], day["cards"], version=version, info={})
store.save_open_day(conn, yesterday, "updated", local_iso(now - DAY), [dict(c, listed_since=yesterday) for c in cards],
                    version=version, info={})
print(json.dumps(out))
`;

/** The extra cards on the stack: stored, saved with site:open.json (the Worker serves it to the site), in the catalog. */
async function addCards(stack) {
  const t = Date.now();
  const py = join(stack.state, "watch-cards.py");
  writeFileSync(py, CARDS_PY);
  const step = async (argv, ws) => {
    const res = await stack.python(argv, ws ? { ws } : {});
    if (res.code !== 0) throw new Error(`${argv.join(" ")} failed:\n${res.out.slice(-2500)}`);
    return res;
  };
  const { ws } = await step(["-m", "watchlist.state", "pull"]);
  const made = await step([py, JSON.stringify({ picks: stack.seed.picks, ladders: ["1124933", "1320204"], unclear: ["1336483"] })], ws);
  const cards = JSON.parse(made.out.trim().split("\n").pop());
  await step(["-m", "watchlist.state", "push", "--by", "local"], ws);
  await step(["-m", "watchlist.sync", "catalog"], ws);
  log(`added the closed-market cards in ${secs(t)} s: ${Object.entries(cards).map(([k, v]) => `${k} "${v.question}"`).join("; ")}`);
  return cards;
}

/** A self-contained scenario: an error in it is its FAIL row, and the part goes on. */
async function own(scenario, fn) {
  try {
    await fn();
  } catch (e) {
    log(`${scenario} stopped: ${e.stack ?? e.message}`);
    rows.push({ scenario, expected: "-", happened: `could not finish: ${String(e.message).slice(0, 300)}`, ok: false, shot: "" });
  }
}

/** A fresh browser context's page on the Watchlist (day: the closed list's day shown). */
async function openPage(browser, stack, day, { route } = {}) {
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  const page = await ctx.newPage();
  page.errors = [];
  page.on("pageerror", (e) => page.errors.push(String(e)));
  if (route) await route(page);
  await page.goto(`${stack.urls.site}#/watchlist${day ? `?day=${day}` : ""}`);
  // the cards (after a daily run whose model calls were all refused, Open now may have none: the closed ones)
  await page.locator("article.cl-card").first().waitFor({ timeout: 60_000 });
  await sleep(500);
  return page;
}

/** The browser's requests to the API refused, as when the Worker is down; returns the undo. */
async function apiDown(page, stack) {
  const pattern = `${stack.urls.api}/**`;
  const handler = (r) => r.abort("connectionrefused");
  await page.context().route(pattern, handler);
  return () => page.context().unroute(pattern, handler);
}

// ---------- the main stack ----------

async function main(browser) {
  const opts = stackOptions({ "port-base": BASE, state: STATE("main"), quiet: !args.verbose, keep: args.keep, picks: 3, cards: 2 });
  log(`starting a stack on ports ${BASE}-${BASE + 3}`);
  const stack = await startStack(opts);
  const from = rows.length;
  let finished = false;
  try {
    const picks = stack.seed.picks;
    const live = stack.seed.cards;
    log(`stack ready (state ${stack.state}); ${picks.length} closed pick(s), ${live.length} live open card(s)`);
    if (picks.length < 2 || live.length < 2) throw new Error(`the seed needs 2 picks and 2 open cards (it has ${picks.length} and ${live.length})`);
    const extra = await addCards(stack);
    const { w1, w2, ladder, unclear } = extra;

    // A: the person's page, left open on the first pick's day; B: someone else's, open from the start
    const A = await openPage(browser, stack, picks[0].day);
    baseline = new Set((await A.locator("body").innerText()).match(HEDGE)?.map((s) => s.toLowerCase()) ?? []);
    log(`hedging words already on the page before any click (left out of the checks): ${[...baseline].join(", ") || "none"}`);
    const B = await openPage(browser, stack, picks[0].day);
    const shownA = {};
    for (const [k, c] of Object.entries({ live0: live[0], live1: live[1], w1, w2, ladder, unclear })) {
      if (!c) continue;
      shownA[k] = await openCard(A, c.question).isVisible();
    }
    await row(
      "setup: the cards show with Watch this market",
      'every Open now card shows "Watch this market"',
      Object.entries(shownA).map(([k, v]) => `${k} ${v ? "shown" : "missing"}`).join(", ") + `; live0 face "${await face(openCard(A, live[0].question))}"`,
      Object.values(shownA).every(Boolean) && /^Watch this market$/.test(await face(openCard(A, live[0].question))),
      A,
    );

    // 1. watch then stop watching, on the card's face
    {
      const card = openCard(A, live[0].question);
      const t = Date.now();
      await card.getByRole("button", { name: "Watch this market" }).click();
      const busyFace = await face(card);
      const on = await waitFace(card, /^Watching Stop watching$/);
      const api1 = await watching(stack, live[0].story);
      const h1 = await hedges(A);
      await card.getByRole("button", { name: "Stop watching" }).click();
      const off = await waitFace(card, /^Watch this market$/);
      const api2 = await watching(stack, live[0].story);
      await row(
        "watch / stop watching (card face)",
        'click: "Watching · Stop watching" within seconds, the site\'s watchlist has it; stop: back to "Watch this market", off the watchlist',
        `right after the click "${busyFace}"; "Watching" after ${ms(on)}, API watching=${api1}; stop: "Watch this market" after ${ms(off)}, API watching=${api2}; hedging: ${h1.join(", ") || "none"}; ${secs(t)} s in all`,
        on != null && off != null && api1 === true && api2 === false && !h1.length,
        A,
      );
    }

    // 2. the details panel and the face agree
    {
      const card = openCard(A, live[0].question);
      await card.locator(".cl-card-foot button").first().click();
      const panel = A.locator(".wl-panel, [role=dialog]").filter({ hasText: "Why this kind is watched" }).last();
      await panel.waitFor({ timeout: 10_000 }).catch(() => {});
      const btn = A.locator(".op-watch button", { hasText: "Watch this market" }).last();
      const t = Date.now();
      await btn.click();
      const panelOn = await A.locator(".op-watch .cl-add-state", { hasText: "Watching" })
        .last()
        .waitFor({ timeout: 20_000 })
        .then(() => Date.now() - t, () => null);
      const faceNow = await face(card);
      const api = await watching(stack, live[0].story);
      await A.keyboard.press("Escape");
      await sleep(500);
      const faceAfter = await face(card);
      await row(
        "watch from the details panel",
        'the panel reads "Watching · Stop watching" and the card\'s face says the same',
        `panel "Watching" after ${ms(panelOn)}; face then "${faceNow}", after closing the panel "${faceAfter}"; API watching=${api}`,
        panelOn != null && /^Watching/.test(faceAfter) && api === true,
        A,
      );
    }

    // 3. a second browser context: left open, then reloaded
    if (!args.fast) {
      const cardB = openCard(B, live[0].question);
      const before = await face(cardB);
      const t = Date.now();
      const seen = await waitFace(cardB, /^Watching/, 150_000);
      const leftOpen = await face(cardB);
      const tr = Date.now();
      await B.reload();
      const reloaded = await waitFace(openCard(B, live[0].question), /^Watching/, 20_000);
      await row(
        "second browser context sees the watch (page left open)",
        "someone else's open page shows the market as watched within a few minutes, by itself",
        `their page read "${before}" when the watch was made; ${seen == null ? `still "${leftOpen}" after ${secs(t)} s left open` : `"Watching" after ${ms(seen)}`}`,
        seen != null,
        B,
      );
      await row(
        "second browser context sees the watch (reload)",
        'a reload of someone else\'s page reads "Watching"',
        `"Watching" ${ms(reloaded)} after the reload (${secs(tr)} s)`,
        reloaded != null,
        B,
      );
    }

    // 4. reload of the person's own page: what shows first, and how long until "Watching"
    {
      const t = Date.now();
      await A.reload();
      await A.locator("article.op-card").first().waitFor({ timeout: 30_000 });
      const first = await face(openCard(A, live[0].question));
      const on = await waitFace(openCard(A, live[0].question), /^Watching/, 20_000);
      await row(
        "reload keeps the watch",
        'after a reload the card reads "Watching" (no flash of "Watch this market" that a click would act on)',
        `first render "${first}"; "Watching" ${ms(on)} after the cards showed (${secs(t)} s after the reload)`,
        on != null && !/^Watch this market$/.test(first),
        A,
      );
    }

    // 5. two quick toggles: a double click on "Watch", then stop and watch again as fast as the buttons allow
    {
      const card = openCard(A, live[1].question);
      await card.getByRole("button", { name: "Watch this market" }).dblclick();
      const on = await waitFace(card, /^Watching Stop watching$/);
      await sleep(1500);
      const settled1 = await face(card);
      const api1 = await watching(stack, live[1].story);
      await card.getByRole("button", { name: "Stop watching" }).click();
      await card.getByRole("button", { name: "Watch this market" }).click({ timeout: 20_000 });
      await waitFace(card, /^Watching Stop watching$/);
      await card.getByRole("button", { name: "Stop watching" }).click({ timeout: 20_000 });
      await waitFace(card, /^Watch this market$/);
      await sleep(2000);
      const settled2 = await face(card);
      const api2 = await watching(stack, live[1].story);
      await A.reload();
      await A.locator("article.op-card").first().waitFor({ timeout: 30_000 });
      await sleep(3000);
      const reloaded = await face(openCard(A, live[1].question));
      await row(
        "two quick toggles",
        "a double click watches once; stop, watch, stop in quick succession ends unwatched; the page and the API agree, also after a reload",
        `double click: "${settled1}" (after ${ms(on)}), API watching=${api1}; stop/watch/stop: "${settled2}", API watching=${api2}; after reload "${reloaded}"`,
        /^Watching/.test(settled1) && api1 === true && /^Watch this market$/.test(settled2) && api2 === false && /^Watch this market$/.test(reloaded),
        A,
      );
    }

    // 6. the Worker down when the person clicks (the page loaded while it was up)
    if (!args.fast) await own("Worker down at the click (page loaded while it was up)", async () => {
      const C = await openPage(browser, stack, picks[0].day);
      const card = openCard(C, live[1].question);
      await waitFace(card, /^Watch this market$/);
      const up = await apiDown(C, stack);
      const t = Date.now();
      await card.getByRole("button", { name: "Watch this market" }).click();
      await sleep(5000);
      const during = await face(card);
      const h = await hedges(C);
      await up();
      log("the API answers again; the page stays open for 150 s");
      await sleep(150_000);
      const later = await face(card);
      const api = await watching(stack, live[1].story);
      await C.reload();
      await C.locator("article.op-card").first().waitFor({ timeout: 30_000 });
      await sleep(4000);
      const reloaded = await face(openCard(C, live[1].question));
      await row(
        "Worker down at the click (page loaded while it was up)",
        'the click reads "Watching" with no warning, and the watch reaches the site\'s watchlist once the API answers (as "Add to main page" sends a kept click), so it is still watched after a reload',
        `5 s after the click "${during}" (hedging: ${h.join(", ") || "none"}); API back, 150 s left open: "${later}", API watching=${api}; after a reload "${reloaded}" (${secs(t)} s in all)`,
        /^Watching/.test(during) && !h.length && api === true && /^Watching/.test(reloaded),
        C,
      );
      await C.context().close();
    });

    // 7. the Worker down from the page's load: a returning visitor (the page read the lists once before), so the
    // cards come from the browser's saved copy
    if (!args.fast) await own("Worker down from the page load", async () => {
      const D = await openPage(browser, stack, picks[0].day);
      const up = await apiDown(D, stack);
      await D.reload();
      await D.locator("article.op-card").first().waitFor({ timeout: 30_000 });
      const card = openCard(D, live[1].question);
      await sleep(5000);
      const loaded = await face(card);
      const apiBefore = await watching(stack, live[1].story);
      await card.locator("button").filter({ hasText: /Watch this market|Stop watching/ }).first().click();
      await sleep(3000);
      const during = await face(card);
      const h = await hedges(D);
      await up();
      log("the API answers again; the page stays open for 150 s");
      await sleep(150_000);
      const later = await face(card);
      const api = await watching(stack, live[1].story);
      await D.reload();
      await D.locator("article.op-card").first().waitFor({ timeout: 30_000 });
      await sleep(4000);
      const reloaded = await face(openCard(D, live[1].question));
      const wanted = /^Watch this market$/.test(loaded) ? true : !apiBefore;
      await row(
        "Worker down from the page load",
        "the card shows its button with no warning; a click reads as done and reaches the site's watchlist once the API answers, so a reload keeps it",
        `loaded with the API down: "${loaded}" (the API had watching=${apiBefore}); after the click "${during}" (hedging: ${h.join(", ") || "none"}); API back, 150 s left open: "${later}", API watching=${api}; after a reload "${reloaded}"`,
        !h.length && api === wanted && (wanted ? /^Watching/.test(reloaded) : /^Watch this market$/.test(reloaded)),
        D,
      );
      await D.context().close();
    });

    // 8. watch the four closed-market cards; W2's closed card also gets "Add to main page"
    for (const c of [w1, w2, ladder, unclear]) {
      if (!c) continue;
      const card = openCard(A, c.question);
      await card.getByRole("button", { name: "Watch this market" }).click();
      const on = await waitFace(card, /^Watching/);
      log(`watched "${c.question}" (${ms(on)}; API watching=${await watching(stack, c.story)})`);
    }
    log(`the site's watchlist now: ${(await watchedNow(stack)).join(" | ")}`);

    let w2AddedAt = null;
    {
      const P = await openPage(browser, stack, picks[1].day);
      const card = closedCard(P, picks[1].question);
      const t = Date.now();
      await card.getByRole("button", { name: /Add to main page/ }).click();
      const adding = await waitFace(card, /Adding to the main page/, 60_000);
      const got = await showsUnless(card.locator("a.wl-act-btn", { hasText: "On the main page" }), 12 * 60_000, addGoesWrong(stack, card, t, picks[1].condition_id));
      const onMain = got.shown ? Date.now() - t : null;
      w2AddedAt = Date.now();
      const r = await request(stack, picks[1].condition_id);
      await row(
        "W2: watched, and a visitor presses Add on its closed card",
        '"Adding to the main page" at once, then "On the main page →" by itself within minutes',
        `"Adding" after ${ms(adding)}, "On the main page →" after ${ms(onMain)}${got.shown ? "" : ` (stopped waiting: ${got.why})`} (no reload); API status ${r?.status}, page name ${r?.name}`,
        onMain != null && onMain < 10 * 60_000 && r?.status === "added",
        P,
      );
      await P.context().close();
    }
    await addsIdle(stack);

    // 9. the daily run, with the person's page left open on the first pick's day
    await A.reload();
    await A.locator("article.op-card").first().waitFor({ timeout: 30_000 });
    const w1Closed = closedCard(A, picks[0].question);
    const beforeRun = { w1Face: await face(w1Closed), w1Open: await face(openCard(A, w1.question)) };
    const idx0 = await siteIndex(stack);
    const blobs0 = await addedBlobs(stack);
    log(`before the daily run: W1's closed card "${beforeRun.w1Face}", its open card "${beforeRun.w1Open}"; ${blobs0.length} added market(s)`);
    const addsBefore = new Set((await addRunsAll(stack)).map((r) => r.id));
    const tRun = Date.now();
    const next = await nextDaily(stack, stack.today);
    const skew = (await stack.workerNow()) * 1000 - Date.now(); // the Worker's clock minus the real one
    log(`the cron made the daily run ${next.id} at 04:00 Toronto (the Worker's clock)`);
    let w1Seen = null;
    let ended = null;
    const watcher = (async () => {
      while (!ended || Date.now() - ended < 180_000) {
        if (w1Seen == null && /On the main page/.test(await face(w1Closed))) w1Seen = Date.now();
        await sleep(3000);
      }
    })();
    const done = await stack.waitRun(next.id, { ms: 75 * 60_000 }).catch(() => null);
    await addsIdle(stack).catch(() => null);
    ended = Date.now();
    const daily = done?.run ?? (await stack.run(next.id))?.run;
    log(`the daily run ended ${daily?.state} after ${daily?.finished_at ? daily.finished_at - daily.started_at : "?"} s; the page stays open 180 s more`);
    await watcher;
    const after = (await addRunsAll(stack)).filter((r) => !addsBefore.has(r.id));
    const w1Run = after.find((r) => r.slot.startsWith(`${w1.expect?.cid}:`));
    const published = w1Run?.finished_at ? w1Run.finished_at * 1000 - skew : null; // in real time
    const openLeft = { w1Face: await face(w1Closed), w1Open: await face(openCard(A, w1.question)) };
    const hRun = await hedges(A);

    const idx1 = await siteIndex(stack);
    const blobs1 = await addedBlobs(stack);
    const reqW1 = w1.expect ? await request(stack, w1.expect.cid) : null;
    const reqW2 = await request(stack, picks[1].condition_id);
    const reqLadder = ladder ? await request(stack, ladder.expect.cid) : null;
    const ladderAll = ladder ? (await Promise.all(ladder.cids.map((c) => request(stack, c)))).filter(Boolean) : [];
    const unclearAll = unclear ? (await Promise.all(unclear.cids.map((c) => request(stack, c)))).filter(Boolean) : [];
    const ov1 = await status(stack);
    const stillWatched = new Set(await watchedNow(stack));

    await row(
      "daily run: a watched market that closed with a result is added with no click",
      "the next daily run adds W1 to the main page by itself and the site publishes it",
      `run ${daily?.id} ${daily?.state}; W1 request ${reqW1?.status ?? "none"} (page ${reqW1?.name ?? "-"}); its add run ${w1Run ? `${w1Run.id.slice(0, 20)}… ${w1Run.state}, finished ${published ? `${secs(tRun, published)} s after the daily run was made` : "never"}` : "never made"}; on the Worker's index: ${onIndex(idx1, w1.expect?.slug).length}`,
      reqW1?.status === "added" && onIndex(idx1, w1.expect?.slug).length === 1,
      A,
    );
    await row(
      "W1 page left open through the daily run",
      'the open Watchlist page\'s W1 Recently closed card reads "On the main page →" by itself within minutes of the publish',
      `before the run "${beforeRun.w1Face}"; ${w1Seen ? `"On the main page →" ${secs(published ?? ended, w1Seen)} s after the publish` : `still "${openLeft.w1Face}" 180 s after the runs ended`}; its Open now card "${openLeft.w1Open}"; hedging: ${hRun.join(", ") || "none"}`,
      w1Seen != null && published != null && w1Seen - published < 5 * 60_000 && !hRun.length,
      A,
    );

    // a fresh look, as the person coming back, at the list the run published (the harness's model answers none of
    // its checks: the picks it could not check again stay, and W1's check is saved)
    const closedAfter = await fetch(`${stack.urls.api}/api/data/closed.json`).then((r) => r.json(), () => null);
    log(`the published Recently closed list after the run: ${JSON.stringify((closedAfter?.days ?? []).map?.((d) => [d.day, d.picks?.length]) ?? closedAfter?.days)}`);
    const F = await openPage(browser, stack, picks[0].day);
    const fresh = closedCard(F, picks[0].question);
    const freshOn = await fresh
      .locator("a.wl-act-btn", { hasText: "On the main page" })
      .waitFor({ timeout: 30_000 })
      .then(() => true, () => false);
    const freshHref = freshOn ? await fresh.locator("a.wl-act-btn").getAttribute("href") : null;
    const freshOpen = (await openCard(F, w1.question).isVisible()) ? await face(openCard(F, w1.question)) : "(the card has left Open now)";
    await row(
      'W1 Recently closed card after a reload reads "On the main page →"',
      'a freshly opened Watchlist shows W1\'s closed card as "On the main page →", linking to its page',
      `"${await face(fresh)}" (link ${freshHref}); W1's Open now card: "${freshOpen}"`,
      freshOn && freshHref === `#/m/${reqW1?.name}`,
      F,
    );

    // not added twice: W1 (one add), W2 (the visitor's add, then the daily run)
    const countBlobs = (slug) => blobs1.filter((b) => bare(b.meta?.entry?.slug) === bare(slug)).length;
    await row(
      "W2 not added twice (visitor's add, then the daily run)",
      "W2's market is on the main page once, from the visitor's add; the daily run leaves it as it is",
      `added markets with its slug: ${countBlobs(picks[1].slug)} (site index ${onIndex(idx1, picks[1].slug).length}); request ${reqW2?.status} page ${reqW2?.name} (the API had it added ${reqW2?.done_at ? `${Math.round(w2AddedAt / 1000 - reqW2.done_at)} s before its card changed` : "-"}); W2 still on the watchlist: ${stillWatched.has(w2.story)}`,
      countBlobs(picks[1].slug) === 1 && onIndex(idx1, picks[1].slug).length === 1 && reqW2?.status === "added",
      F,
    );
    await row(
      "W1 not added twice",
      "W1's market is on the main page once; the daily run made one add run for it and nothing more",
      `added markets with its slug: ${countBlobs(w1.expect?.slug)} (index ${onIndex(idx1, w1.expect?.slug).length}); add runs the daily run made: ${after.map((r) => `${r.slot.slice(0, 12)}… ${r.state}`).join(", ") || "none"}`,
      countBlobs(w1.expect?.slug) === 1 && onIndex(idx1, w1.expect?.slug).length === 1,
      F,
    );

    if (ladder) {
      const added = ladderAll.filter((r) => r.status === "added" || r.status === "approved");
      await row(
        "watched ladder (multi-market event)",
        `the run adds the one market watched_choice picks ("${ladder.expect.question}") and none of the event's other ${ladder.markets - 1}`,
        `"${ladder.title}" (${ladder.markets} markets, ${ladder.open} still open): ${added.length} of its markets sent (${added.map((r) => `${r.status} ${r.name ?? ""}`).join(", ") || "none"}); the picked one: ${reqLadder?.status ?? "none"}; on the index: ${onIndex(idx1, ladder.expect.slug).length}`,
        added.length === 1 && reqLadder?.status === "added" && onIndex(idx1, ladder.expect.slug).length === 1,
        F,
      );
    }
    if (unclear) {
      const freshUnclear = (await openCard(F, unclear.question).isVisible()) ? await face(openCard(F, unclear.question)) : "(left Open now)";
      await row(
        "watched market closed without a clear result waits",
        "nothing is added and it stays on the site's watchlist; its card reads plainly (no hedging)",
        `"${unclear.title}" (${unclear.prices.join(" ")}): ${unclearAll.length} request(s); still on the watchlist: ${stillWatched.has(unclear.story)}; card "${freshUnclear}"; hedging: ${(await hedges(F)).join(", ") || "none"}`,
        unclearAll.length === 0 && stillWatched.has(unclear.story) && !(await hedges(F)).length,
        F,
      );
    }
    {
      const expected = new Set([w1.expect?.cid, picks[1].condition_id, ladder?.expect.cid].filter(Boolean));
      const others = ov1.requests.filter((r) => !expected.has(r.condition_id));
      await row(
        "watched live (still open) markets wait",
        "the daily run adds nothing for an open market; it stays watched",
        `live0 still watched: ${stillWatched.has(live[0].story)}; requests other than W1, W2 and the ladder's: ${others.map((r) => `${r.slug} ${r.status}`).join("; ") || "none"}`,
        stillWatched.has(live[0].story) && !others.length,
        F,
      );
    }

    // 10. a visitor presses Add on W1's closed card on a page that still offers it (left open from before the run)
    if (/Add to main page/.test(await face(w1Closed))) {
      const runs0 = (await addRunsAll(stack)).length;
      const t = Date.now();
      await w1Closed.getByRole("button", { name: /Add to main page/ }).click();
      const got = await showsUnless(w1Closed.locator("a.wl-act-btn", { hasText: "On the main page" }), 5 * 60_000, addGoesWrong(stack, w1Closed, t, w1.expect?.cid));
      const seen = got.shown ? Date.now() - t : null;
      const shown = await face(w1Closed);
      await addsIdle(stack);
      const blobs2 = await addedBlobs(stack);
      await row(
        "visitor presses Add on W1 after the daily run added it (stale page)",
        '"On the main page →" within seconds; nothing is added again',
        `${seen == null ? `"${shown}" (stopped waiting: ${got.why})` : `"On the main page →" after ${ms(seen)}`}; add runs made: ${(await addRunsAll(stack)).length - runs0}; added markets with its slug: ${blobs2.filter((b) => bare(b.meta?.entry?.slug) === bare(w1.expect?.slug)).length}`,
        seen != null && seen < 90_000 && blobs2.filter((b) => bare(b.meta?.entry?.slug) === bare(w1.expect?.slug)).length === 1,
        A,
      );
    }

    // 11. the next daily run: still once each
    if (args["second-run"]) {
      const n2 = await nextDaily(stack, next.day);
      log(`the second daily run ${n2.id} is made`);
      const d2 = (await stack.waitRun(n2.id, { ms: 75 * 60_000 }).catch(() => null))?.run ?? { id: n2.id, state: "unfinished" };
      await addsIdle(stack);
      const blobs3 = await addedBlobs(stack);
      const counts = [w1.expect?.slug, picks[1].slug, ladder?.expect.slug].filter(Boolean).map((s) => blobs3.filter((b) => bare(b.meta?.entry?.slug) === bare(s)).length);
      await row(
        "the next daily run adds nothing again",
        "W1, W2 and the ladder's market are each on the main page once",
        `run ${d2.id} ${d2.state}; counts ${counts.join(", ")}`,
        counts.every((n) => n === 1),
        F,
      );
    }
    const errs = [A, B, F].flatMap((p) => p.errors);
    await row("no page errors", "no uncaught errors in any page", errs.length ? errs.slice(0, 3).join(" | ") : "none", !errs.length, null);
    for (const p of [A, B, F]) await p.context().close().catch(() => {});
    await stack.clock(null);
    finished = true;
  } finally {
    await stack.stop({ keepState: !finished || failedSince(from) });
  }
}

// ---------- the bot check failing ----------

async function bot(browser) {
  const opts = stackOptions({ "port-base": BASE, state: STATE("bot"), quiet: !args.verbose, keep: args.keep, "bot-fail": true, picks: 1, cards: 1 });
  log(`starting a stack with the always-failing Turnstile keys on ports ${BASE}-${BASE + 3}`);
  const stack = await startStack(opts);
  const from = rows.length;
  let ended = false;
  try {
    const card0 = stack.seed.cards[0];
    if (!card0) throw new Error("the seed found no open card");
    const P = await openPage(browser, stack, null);
    const card = openCard(P, card0.question);
    await waitFace(card, /^Watch this market$/);
    const t = Date.now();
    await card.getByRole("button", { name: "Watch this market" }).click();
    let shown = null;
    while (Date.now() - t < 90_000) {
      const f = await face(card);
      if (/bot check|Try again|Couldn't/i.test(f)) {
        shown = Date.now() - t;
        break;
      }
      await sleep(250);
    }
    const f1 = await face(card);
    const api = await watching(stack, card0.story);
    const widget = await P.locator(".ts-slot iframe, body > div iframe").count();
    await row(
      "bot check failing",
      'the person reads why it didn\'t work ("The bot check didn\'t pass. Try again.") next to the button, which still reads "Watch this market"; nothing is saved',
      `${shown == null ? "no message within 90 s" : `message after ${ms(shown)}`}: "${f1}"; API watching=${api}; Turnstile frames on the page: ${widget}`,
      shown != null && /bot check didn't pass/i.test(f1) && /Watch this market/.test(f1) && api === false,
      P,
    );
    // the second try reads the same, and the message goes once the person tries again
    const t2 = Date.now();
    await card.getByRole("button", { name: "Watch this market" }).click();
    await sleep(1000);
    const during = await face(card);
    let again = null;
    while (Date.now() - t2 < 90_000) {
      if (/bot check didn't pass/i.test(await face(card))) {
        again = Date.now() - t2;
        break;
      }
      await sleep(250);
    }
    await row(
      "bot check failing, tried again",
      "the old message clears while it tries, and the same message shows again",
      `1 s after the second click "${during}"; message again after ${ms(again)}`,
      again != null && !/bot check didn't pass/i.test(during),
      P,
    );
    await P.context().close();
    ended = true;
  } finally {
    await stack.stop({ keepState: !ended || failedSince(from) });
  }
}

// ---------- a slow API: what a reload shows while it asks ----------

async function slow(browser) {
  const opts = stackOptions({ "port-base": BASE, state: STATE("slow"), quiet: !args.verbose, keep: args.keep, picks: 1, cards: 1 });
  log(`starting a stack for the slow API on ports ${BASE}-${BASE + 3}`);
  const stack = await startStack(opts);
  const from = rows.length;
  let ended = false;
  try {
    const card0 = stack.seed.cards[0];
    if (!card0) throw new Error("the seed found no open card");
    const P = await openPage(browser, stack, null);
    await openCard(P, card0.question).getByRole("button", { name: "Watch this market" }).click();
    await waitFace(openCard(P, card0.question), /^Watching/);
    // the API's answers to "is it watched?" take 3 s, as a slow mobile connection or a cold Worker can
    await P.context().route(`${stack.urls.api}/api/watch?*`, async (r) => {
      if (r.request().method() === "GET") await sleep(3000);
      await r.continue();
    });
    await P.reload();
    await P.locator("article.op-card").first().waitFor({ timeout: 30_000 });
    const card = openCard(P, card0.question);
    const seen = [];
    const t = Date.now();
    let clicked = null;
    while (Date.now() - t < 6000) {
      const f = await face(card);
      if (seen.at(-1)?.f !== f) seen.push({ at: Date.now() - t, f });
      if (clicked == null && /^Watch this market$/.test(f)) {
        clicked = Date.now() - t;
        await card.getByRole("button", { name: "Watch this market" }).click({ timeout: 1000 }).catch(() => {});
      }
      await sleep(150);
    }
    await sleep(3000);
    const end = await face(card);
    const api = await watching(stack, card0.story);
    await row(
      "reload with a slow API (3 s) on a watched market",
      'the card never offers "Watch this market" for a market already watched; a click while it asks is not lost',
      `faces after the cards showed: ${seen.map((s) => `${(s.at / 1000).toFixed(1)} s "${s.f}"`).join(" -> ")}; ${clicked == null ? "no click needed" : `clicked "Watch this market" at ${(clicked / 1000).toFixed(1)} s`}; at the end "${end}", API watching=${api}`,
      clicked == null && /^Watching/.test(end) && api === true,
      P,
    );
    await P.context().close();
    ended = true;
  } finally {
    await stack.stop({ keepState: !ended || failedSince(from) });
  }
}

// ---------- run ----------

let browser;
let failed = false;
try {
  browser = await launch({ headed: !!args.headed });
  for (const [part, f] of [
    ["main", main],
    ["bot", bot],
    ["slow", slow],
  ]) {
    if (!ONLY.has(part)) continue;
    try {
      await f(browser);
    } catch (e) {
      failed = true;
      log(`the ${part} part stopped: ${e.stack ?? e.message}`);
      rows.push({ scenario: `${part}: could not finish`, expected: "-", happened: String(e.message).slice(0, 400), ok: false, shot: "" });
    }
  }
} finally {
  await browser?.close();
}

console.log("\nscenario | expected | what happened | result");
for (const r of rows) console.log(`${r.scenario} | ${r.expected} | ${r.happened}${r.shot ? ` [${r.shot}]` : ""} | ${r.ok ? "PASS" : "FAIL"}`);
writeFileSync(join(SHOTS, "results.json"), JSON.stringify(rows, null, 2));
console.log(`\nwatch: ${rows.filter((r) => !r.ok).length} failed of ${rows.length} in ${secs(T0)} s`);
process.exit(failed || rows.some((r) => !r.ok) ? 1 : 0);
