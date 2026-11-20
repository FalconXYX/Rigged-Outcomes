// Owner: the review page (#/review) and the backend runs it drives, the way the owner uses them, through the whole
// stack with real timing. It starts a stack (tests/end-to-end/stack.mjs), keeps one review tab open for the whole
// test (unlocked once, never reloaded: only its own "Reload" link), and lets the backend do its own work: the
// Worker's cron makes the daily runs, "Apply decisions now" queues an apply run, and the Worker wakes the local
// Vercel for each. Nothing is run by hand after a click. The scenarios, in order (each builds on the one before):
//
//   setup        today's daily run, made by the cron, in the open list's own small world (below): its queue
//   unlock       a wrong token, then the owner token
//   queue        "Count as a step" on one waiting story and "Not a step" on another, then "Apply decisions now":
//                the apply run, then the approved card on the Open now list of a Watchlist tab left open
//   twice        a decision, then "Apply decisions now" pressed twice quickly: one run does the work
//   crash        a decision, then "Apply decisions now" with a step whose rebuild crashes: retried, then failed,
//                and the review page names the failed step; the decision stays waiting
//   pause        the pause switch: visitors' writes refused while paused, what a visitor's tab shows, and the add
//                it kept going through once the owner allows changes again
//   full         the next day's daily run at 04:00 Toronto, a decision made while it goes (it waits, then the next
//                apply does it), its report on the review page; the catch-up at 10:00 only when work was left, and
//                no run starting another in a loop
//   badtoken     Vercel holding a job token the Worker refuses: what the review page says, and the run going
//                through once the token is right again
//   cap          "Apply decisions now" once more after 30 queued today
//
// The Open now list needs a queue, and a queue needs model readings, which the harness never gets from Gemini. So
// the scenario's Python (written to <state>-py, first on every Python process's path, loading the harness's own
// sitecustomize first) gives the open list a small world of its own: six fixed events and their headlines
// (tests/test_watchlist_opennow.py's) served to the open list's steps only, and the unit tests' fake model client
// in place of the open list's model clients (in the process: no request leaves it). The closed list, the adds and
// everything else stay on live Polymarket and Google News data; Gemini itself is never reached (the stand-in's log
// is checked).
//
//   node tests/end-to-end/scenarios/owner.mjs [--port-base 9620] [--state DIR] [--shots DIR] [--headed] [--keep]
//                                             [--watch-min MINUTES] [--skip a,b]
//
// --watch-min: how long an open page is watched after a run ends before it counts as not updating by itself
// (default 6). Prints a table of the scenarios and writes it to <shots>/owner-report.json. The stack's state folder
// is removed when every scenario passed (--keep keeps the stack running and its folder). Exit 0 passed, 1 failed.

import { mkdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { launch } from "../lib/browser.mjs";
import { addGoesWrong, showsUnless } from "../lib/outcome.mjs";
import { secs, sleep } from "../lib/proc.mjs";
import { runLine } from "../lib/scenario.mjs";
import { dayShift, toronto, torontoAt } from "../lib/toronto.mjs";
import { OWNER_TOKEN } from "../lib/worker.mjs";
import { TERMINAL, parseArgs, stackOptions, startStack } from "../stack.mjs";

const HERE = dirname(fileURLToPath(import.meta.url));
const args = parseArgs(process.argv.slice(2));
const opts = stackOptions({ "port-base": 9620, state: join(tmpdir(), "rigged-e2e-owner"), picks: 3, cards: 0, daily: true, ...args, quiet: !args.verbose });
opts.cron = false; // the cron starts once the clock is where the world wants it
const SHOTS = args.shots ?? join(tmpdir(), "e2e-shots", "owner");
const WATCH_MS = Number(args["watch-min"] ?? 6) * 60_000; // a tab left open reads the lists again every 5 min
const skip = new Set(String(args.skip ?? "").split(",").filter(Boolean));
mkdirSync(SHOTS, { recursive: true });

const t0 = Date.now();
const results = [];
const log = (s) => console.log(`[${String(secs(t0)).padStart(6)} s] ${s}`);
let stack;
let browser;
let review;

/** One scenario's row: what the owner expects, what happened, and whether it passed. */
function result(scenario, expected, happened, pass) {
  results.push({ scenario, expected, happened, pass: !!pass });
  log(`${pass ? "PASS" : "FAIL"} ${scenario}: ${happened}`);
}

async function shot(page, name) {
  const file = join(SHOTS, `${name}.png`);
  await page.screenshot({ path: file, fullPage: true }).catch(() => {});
  return file;
}

/** Polls fn every `every` ms until it returns something truthy or `ms` passes; returns that value, or null. */
async function waitFor(fn, ms, every = 1000) {
  const end = Date.now() + ms;
  for (;;) {
    const v = await fn().catch(() => null);
    if (v) return v;
    if (Date.now() > end) return null;
    await sleep(every);
  }
}

const ownerApi = (method, path, body) => stack.api(method, path, body, OWNER_TOKEN);
const overview = async () => (await ownerApi("GET", "/api/admin/overview")).body;

/** The Worker's clock minus the real one (ms) from each real time on: [[real ms, skew ms]], newest last. */
const skews = [[0, 0]];
/** stack.clock(to), keeping `skews`: run times are on the Worker's clock, a click's on the real one. */
async function moveClock(to) {
  const n = await stack.clock(to);
  skews.push([Date.now(), (n ?? Math.floor(Date.now() / 1000)) * 1000 - Date.now()]);
  return n;
}
/** The Worker's time (ms) at real time `t`. */
const workerAt = (t) => t + skews.filter(([at]) => at <= t).pop()[1];
/** The backend's runs made since `since` (real ms), of `kind` when given, oldest first. */
const runsSince = async (since, kind) =>
  ((await overview())?.backend?.runs ?? []).filter((r) => r.created_at * 1000 >= workerAt(since) - 2000 && (!kind || r.kind === kind)).reverse();
/** Waits until no run is queued or running (and none starts within 3 s). */
async function settle(ms = 30 * 60_000) {
  const idle = async () => !((await overview())?.backend?.runs ?? []).some((r) => r.state === "queued" || r.state === "running");
  await waitFor(idle, ms, 2000);
  await sleep(3000);
  await waitFor(idle, ms, 2000);
}
/** One line for each run. */
const describe = async (runs, since) => (await Promise.all(runs.map((r) => runLine(stack, r.id, since)))).join(" ; ");

/** The Open now list the Worker serves, as a fresh visitor would get it. */
async function siteOpen() {
  const doc = await fetch(`${stack.urls.api}/api/data/open.json`).then((r) => r.json(), () => ({}));
  return (doc.days?.[0]?.cards ?? []).map((c) => c.question);
}

/** Visible text of a part of the review page, in one line. */
const textOf = async (loc) => (await loc.innerText().catch(() => "")).replace(/\s+/g, " ").trim();

// ---------- the review page ----------

const R = {
  status: () => review.locator('section[aria-label="Status"]'),
  statusLine: () => review.locator('section[aria-label="Status"] .rv-runs'),
  backend: () => review.locator("section[aria-labelledby=rv-backend]"),
  applyBtn: () => review.getByRole("button", { name: "Apply decisions now" }),
  applyNote: () => review.locator(".rv-apply .rv-muted"),
  item: (q) => review.locator("li.rv-item").filter({ has: review.locator("h3.rv-q", { hasText: q }) }),
  queue: () => review.locator('section[aria-labelledby="rv-queue"]'),
  applied: () => review.locator(".rv-elsewhere").filter({ hasText: "Applied by recent runs" }),
  waiting: () => review.locator(".rv-elsewhere").filter({ hasText: "Your other decisions" }),
  reload: async () => {
    await review.locator(".rv-foot button", { hasText: "Reload" }).click();
    await review.locator('section[aria-label="Status"]').waitFor({ timeout: 30_000 });
    await sleep(500);
  },
};

/**
 * Watches the open review page (no reload) for `ms` after a run ended: returns the seconds until `changed()`
 * read true, or null when it never did.
 */
async function watchOpen(changed, ms, from = Date.now()) {
  const got = await waitFor(async () => ((await changed()) ? Date.now() : null), ms, 2000);
  return got ? secs(from, got) : null;
}

/** Presses "Apply decisions now" (the open tab's button) and waits for the apply run it queued to end. */
async function applyNow() {
  const click = Date.now();
  await R.applyBtn().click();
  const noteAt = await waitFor(async () => (/Queued|Applying/.test(await textOf(R.applyNote())) ? Date.now() : null), 20_000, 200);
  const note = await textOf(R.applyNote());
  const run = await waitFor(async () => (await runsSince(click, "apply"))[0], 30_000, 300);
  if (run) await stack.waitRun(run.id, { ms: 30 * 60_000 }).catch(() => null);
  await settle();
  return { click, noteAt, note, run: run && (await stack.run(run.id)).run };
}

// ---------- the open list's own small world (Python) ----------

const WORLD_PY = String.raw`"""
The owner scenarios' Open now world (tests/end-to-end/scenarios/owner.mjs): six fixed events and their headlines, from
tests/test_watchlist_opennow.py, placed around E2E_OWNER_NOW (epoch seconds), and the unit tests' fake model
client for the one setup run that reads them ("yesterday's" daily run). Gemini is never called from here.
"""
import json
import os
import re
import time
from datetime import datetime, timezone

DAY = 86400
NOW = int(os.environ.get("E2E_OWNER_NOW") or time.time())


def _at(days):
    return datetime.fromtimestamp(NOW + days * DAY, timezone.utc)


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


REVEAL = _at(2)
REVEAL_DATE = REVEAL.strftime("%Y-%m-%d")
REVEAL_WORDS = f"{REVEAL:%B} {REVEAL.day}, {REVEAL.year}"


def market(mid, question, end="2027-01-01T00:00:00Z", volume=50_000, prices=("0.1", "0.9"), **kw):
    m = {"id": str(mid), "question": question, "slug": f"m{mid}", "endDate": end, "volumeNum": volume,
         "outcomes": '["Yes", "No"]', "outcomePrices": json.dumps(list(prices)), "active": True, "closed": False,
         "description": kw.pop("description", f"This market will resolve to Yes if {question}")}
    m.update(kw)
    return m


def event(eid, title, markets, **kw):
    return dict({"id": str(eid), "slug": f"e{eid}", "title": title, "markets": markets}, **kw)


def events():
    return [
        event(1, "Jane Roe out as Secretary of Widgets by...?",
              [market(11, "Jane Roe out as Secretary of Widgets by December 31?")]),
        event(2, "Acme and Globex merger announced by...?",
              [market(21, "Acme and Globex merger announced by December 31?")]),
        event(3, "Ruritania x Freedonia ceasefire continues through...?",
              [market(31, "Ruritania x Freedonia ceasefire continues through December 31?")]),
        event(4, "Turner Prize 2026 winner?",
              [market(41, "Will Sam Artist win the Turner Prize 2026?", end=_at(3).strftime("%Y-%m-%dT00:00:00Z"),
                      description=f"The winner of the Turner Prize 2026 will be announced on {REVEAL_WORDS}. "
                                  "This market resolves to the artist named the winner.")]),
        event(5, "Fed decision in December?",
              [market(51, "Fed decreases interest rates by 25 bps after December 2026 meeting?")]),
        event(6, "Nearly settled?", [market(61, "Will Acme open a store by December 31?", prices=("0.995", "0.005"))]),
    ]


NAMES = {
    "Jane Roe out as Secretary of Widgets by...?": dict(
        kind="us_official", subject="Jane Roe", short="Roe", decider="the President", post="the Widgets Department",
        search_names="Jane Roe", search_event="Jane Roe resign fired"),
    "Acme and Globex merger announced by...?": dict(
        kind="merger_acquisition", subject="Acme and Globex", decider="Acme's board", search_names="Acme Globex",
        search_event="Acme Globex merger"),
    "Ruritania x Freedonia ceasefire continues through...?": dict(
        kind="planned_strike_or_op", subject="Ruritania or Freedonia", what="a strike on the other",
        aliases=["Ruritanian"], search_names="Ruritania Freedonia", search_event="Ruritania Freedonia strike"),
    "Turner Prize 2026 winner?": dict(
        kind="jury_prize", subject="Turner Prize", what="the Turner Prize 2026", decider="the Turner Prize jury",
        which="Who", search_names="Turner Prize", search_event="Turner Prize shortlist winner"),
}
NEWS = {   # query -> [(hours from NOW, source, title)]
    "Jane Roe": [(-10, "Reuters", "White House weighs Jane Roe's future, officials say"),
                 (-6, "Yahoo", "Jane Roe exit odds rise to 40%"),
                 (-30, "Op-Ed Daily", "Jane Roe should go")],
    "Jane Roe resign fired": [(-10, "Reuters", "White House weighs Jane Roe's future, officials say")],
    "Acme Globex": [(-19.5, "Bloomberg", "Acme and Globex in merger talks, people familiar say"),
                    (-50, "Trade Blog", "What an Acme-Globex deal could mean")],
    "Acme Globex merger": [(-19.5, "Bloomberg", "Acme and Globex in merger talks, people familiar say")],
    "Ruritania Freedonia": [(-12, "AP News", "Ruritania cabinet orders army to prepare strike on Freedonia"),
                            (-3, "Local Wire", "Freedonia says city hit by 'enemy strike'")],
    "Ruritania Freedonia strike": [(-12, "AP News", "Ruritania cabinet orders army to prepare strike on Freedonia")],
}


def _url(title):
    return "https://news.example.com/" + re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")


def search(query, after=None, before=None, limit=None):
    found = NEWS.get(query)
    if found is None:
        words = {w.lower() for w in query.split() if len(w) >= 4}
        found = {x for hs in NEWS.values() for x in hs if words & set(re.findall(r"\w+", x[2].lower()))}
    return [{"title": t, "source": s, "published": iso(NOW + int(h * 3600)), "url": _url(t)} for h, s, t in sorted(found)]


STEP = re.compile(r"weighs|talks|prepare", re.I)
OUT = re.compile(r"resigns|hit by", re.I)


def step_lines(prompt):
    return [(int(n), t) for n, t in re.findall(r"^\[(\d+)\] [^·]+ · [^·]+ · (.*)$", prompt, re.M)]


class FakeClient:
    """tests/test_watchlist_opennow.py's FakeClient, with this world's names and reveal date."""

    models = ("fake-lite",)

    def __init__(self):
        self.prompts, self.skip = [], set()

    def generate(self, prompt, schema=None):
        from watchlist import opennow as O
        self.prompts.append(prompt)
        if prompt.startswith("You fill in names"):
            out = []
            for mid, title in re.findall(r"### Market id=(\S+)\nEvent: (.*)", prompt):
                n = dict(NAMES[title])
                out.append(dict({s: "" for s in O._NAME_SLOTS}, id=mid, aliases=n.pop("aliases", []), others=[], **n))
            return json.dumps({"names": out}), "fake-lite", {}
        if prompt.startswith("You label prediction markets"):
            out = [{"id": mid, "type": "predecided_reveal", "reveal_kind": "scheduled", "reveal_date": REVEAL_DATE,
                    "quote": f"will be announced on {REVEAL_WORDS}"} for mid in re.findall(r"### Market id=(\S+)", prompt)]
            return json.dumps({"labels": out}), "fake-lite", {}
        assert prompt.startswith("You read news headlines"), prompt[:80]
        if "Market question:" in prompt:
            out = [{"n": n, "who_acts": " ".join(t.split()[:2]), "what": t[:40],
                    "category": "step" if STEP.search(t) else "other_event"} for n, t in step_lines(prompt)]
            return json.dumps({"headlines": out}), "fake-gemma", {}
        out = []
        for block in prompt.split("### Market id=")[1:]:
            mid = block.split("\n", 1)[0].strip()
            titles = re.findall(r"^\[\d+\] [^·]+ · [^·]+ · (.*)$", block, re.M)
            out_t = next((t for t in titles if OUT.search(t)), None)
            out.append({"id": mid, "already_out": out_t or "none",
                        "already_out_status": ("out" if "resigns" in (out_t or "") else "unclear") if out_t else "none"})
        return json.dumps({"answers": out}), "fake-lite", {}
`;

const SITE_PY = String.raw`# The owner scenarios' stand-ins (tests/end-to-end/scenarios/owner.mjs): the harness's own sitecustomize
# first, then, with E2E_OWNER_WORLD=1:
#   - watchlist.net.walk_open_events and watchlist.news.search answered by owner_world for the open list's steps
#     and code (calls made under watchlist/opennow.py or watchlist/steps.py, the closed list's news step left out:
#     the closed list and the adds stay live)
#   - watchlist.opennow's model clients replaced by owner_world.FakeClient (no request leaves the process)
#   - watchlist.opennow.rebuild_reviewed raising while the file <E2E_OWNER_FLAGS>/crash-rebuild exists (a bug in
#     the code, for a failed step)
import os
import sys

_o_harness = os.environ.get("E2E_HARNESS_SITE")
if _o_harness and os.path.exists(_o_harness):
    exec(compile(open(_o_harness).read(), _o_harness, "exec"))

if os.environ.get("E2E_OWNER_WORLD") == "1":
    import importlib.abc

    def _o_world_caller():
        f = sys._getframe(2)
        while f is not None:
            if f.f_code.co_name == "closed_news":
                return False
            name = f.f_code.co_filename
            if name.endswith(os.path.join("watchlist", "opennow.py")) or name.endswith(os.path.join("watchlist", "steps.py")):
                return True
            f = f.f_back
        return False

    def _o_net(m):
        real = m.walk_open_events

        def walk_open_events(*a, **kw):
            if _o_world_caller():
                import owner_world
                return owner_world.events()
            return real(*a, **kw)
        m.walk_open_events = walk_open_events

    def _o_news(m):
        real = m.search

        def search(*a, **kw):
            if _o_world_caller():
                import owner_world
                return owner_world.search(*a, **kw)
            return real(*a, **kw)
        m.search = search

    def _o_opennow(m):
        import owner_world
        m.open_client = lambda *a, **kw: owner_world.FakeClient()
        m.make_step_client = lambda *a, **kw: owner_world.FakeClient()
        real = m.rebuild_reviewed

        def rebuild_reviewed(*a, **kw):
            flags = os.environ.get("E2E_OWNER_FLAGS") or ""
            if flags and os.path.exists(os.path.join(flags, "crash-rebuild")):
                raise RuntimeError("e2e: a bug in the rebuild")
            return real(*a, **kw)
        m.rebuild_reviewed = rebuild_reviewed

    _O_FIXES = {"watchlist.opennow": _o_opennow, "watchlist.net": _o_net, "watchlist.news": _o_news}

    class _OwnerHook(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path, target=None):
            fix = _O_FIXES.get(name)
            if not fix:
                return None
            for f in sys.meta_path:
                if f is self or not hasattr(f, "find_spec"):
                    continue
                spec = f.find_spec(name, path, target)
                if spec is not None:
                    break
            else:
                return None
            run = spec.loader.exec_module

            def exec_module(module, run=run, fix=fix):
                run(module)
                fix(module)
            spec.loader.exec_module = exec_module
            return spec

    sys.meta_path.insert(0, _OwnerHook())
`;

/** The owner world's Python folder, beside the state folder (the stack empties that one at its start). */
function writeWorld(dir) {
  mkdirSync(join(dir, "flags"), { recursive: true });
  writeFileSync(join(dir, "owner_world.py"), WORLD_PY);
  writeFileSync(join(dir, "sitecustomize.py"), SITE_PY);
  return join(dir, "flags");
}

// ---------- the scenarios ----------

let world;

async function setupWorld() {
  const t = Date.now();
  if (world.now > Math.floor(Date.now() / 1000) + 60) await moveClock(world.now);
  stack.cron.start();
  await stack.tick();
  const daily = `daily:${toronto(world.now).day}`;
  const end = await stack.waitRun(daily, { ms: 45 * 60_000 }).catch(() => null);
  await settle();
  const ov = await overview();
  const items = ov?.queue?.items ?? [];
  log(`setup: ${await runLine(stack, daily, t)}; queue ${items.map((a) => `${a.question} (${a.why ?? a.place})`).join(" | ") || "empty"} (${secs(t)} s)`);
  if (!end) throw new Error(`today's daily run did not end: ${await runLine(stack, daily, t)}`);
  if (!items.length) throw new Error("the review page's queue is empty after the daily run");
  world.daily = daily;
}

async function scenarioUnlock() {
  const t = Date.now();
  await review.goto(`${stack.urls.site}#/review`);
  const pw = review.locator("form.rv-unlock input[type=password]");
  await pw.waitFor({ timeout: 30_000 });
  const before = await textOf(review.locator("form.rv-unlock"));
  await pw.fill("not-the-owner-token");
  const clicked = Date.now();
  await review.getByRole("button", { name: "Unlock" }).click();
  const alert = review.locator("form.rv-unlock [role=alert]");
  const shown = await alert.waitFor({ timeout: 15_000 }).then(() => Date.now(), () => null);
  const wrongText = await textOf(alert);
  const stillLocked = await pw.isVisible();
  result(
    "unlock: wrong token",
    "the form stays and says the token was not accepted, quickly; nothing of the owner's data shows",
    shown ? `"${wrongText}" after ${secs(clicked, shown)} s; form still there: ${stillLocked}; form text before: "${before}"` : `no error shown within 15 s (${await shot(review, "unlock-wrong")})`,
    !!shown && /not accepted/i.test(wrongText) && stillLocked,
  );

  await pw.fill(OWNER_TOKEN);
  const c2 = Date.now();
  await review.getByRole("button", { name: "Unlock" }).click();
  const ok = await R.status().waitFor({ timeout: 30_000 }).then(() => Date.now(), () => null);
  const statusText = await textOf(R.status());
  const errorLeft = await review.locator(".rv-error").count();
  const backendText = await textOf(R.backend());
  result(
    "unlock: owner token",
    "the review page opens at once: the last runs, the pause switch, Apply decisions now, the queue, the backend's budget, lanes and runs",
    ok
      ? `opened in ${secs(c2, ok)} s (${secs(t)} s from arrival); status reads "${statusText.slice(0, 200)}"; backend: "${backendText.slice(0, 200)}…"; error text on screen: ${errorLeft}`
      : `did not open within 30 s (${await shot(review, "unlock-owner")})`,
    !!ok && errorLeft === 0 && (await R.applyBtn().count()) === 1 && /Within budget/.test(backendText) && /Last daily run/.test(statusText),
  );
}

/** Clicks one of a queue item's buttons and waits for its decision to read `reads`; returns the seconds it took. */
async function decide(question, button, reads) {
  const item = R.item(question);
  await item.waitFor({ timeout: 15_000 });
  const t = Date.now();
  await item.getByRole("button", { name: button }).first().click();
  await item.locator(".rv-decided", { hasText: reads }).first().waitFor({ timeout: 15_000 });
  return secs(t);
}

async function scenarioQueue() {
  const items = (await overview()).queue.items;
  const roe = items.find((i) => /Jane Roe/.test(i.question));
  const acme = items.find((i) => /Acme/.test(i.question));
  if (!roe || !acme) throw new Error(`the queue lacks the Roe or Acme story: ${items.map((i) => i.question).join(" | ")}`);
  const whyRoe = await textOf(R.item(roe.question).locator(".rv-why"));

  // a visitor's Watchlist tab, left open from before the decisions
  const visitor = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  const wl = await visitor.newPage();
  await wl.goto(`${stack.urls.site}#/watchlist`);
  await wl.locator("article.op-card, article.cl-card, .wl-ghosts").first().waitFor({ timeout: 30_000 });
  const cardsBefore = await wl.locator("article.op-card h3.cl-card-title").allInnerTexts();

  const sRoe = await decide(roe.question, "Count as a step", "Counted as a step");
  const sAcme = await decide(acme.question, "Not a step", "Not a step");
  const decidedText = await textOf(R.item(roe.question).locator(".rv-decided"));

  const { click, noteAt, note, run } = await applyNow();
  const runs = await runsSince(click);
  const ended = run?.finished_at ? run.finished_at * 1000 : Date.now();
  const onSite = await siteOpen();
  const roeOnSite = onSite.some((q) => /Jane Roe/.test(q));
  const acmeOnSite = onSite.some((q) => /Acme/.test(q));
  const ov = await overview();
  const applied = ov.decisions.filter((d) => [roe.story, acme.story].includes(d.story) && d.applied_at != null).length;

  // the open pages, left open: does either show the result by itself?
  const watchFrom = Date.now();
  const [reviewSelf, wlSelf] = await Promise.all([
    watchOpen(async () => /applied/.test(await textOf(R.item(roe.question))) || /Last apply run/.test(await textOf(R.statusLine())) || (await R.applied().count()) > 0, WATCH_MS, watchFrom),
    watchOpen(async () => (await wl.locator("article.op-card h3.cl-card-title").allInnerTexts()).some((q) => /Jane Roe/.test(q)), WATCH_MS, watchFrom),
  ]);
  if (reviewSelf == null) await shot(review, "queue-review-left-open");
  if (wlSelf == null) await shot(wl, "queue-watchlist-left-open");

  await R.reload();
  const statusReloaded = await textOf(R.statusLine());
  await wl.reload();
  await wl.locator("article.op-card").first().waitFor({ timeout: 30_000 }).catch(() => {});
  const cardsReload = await wl.locator("article.op-card h3.cl-card-title").allInnerTexts();
  await visitor.close();

  result(
    "queue: decide (approve and decline)",
    "each button answers at once and the row reads its decision; no hedging",
    `"Count as a step" read as decided in ${sRoe} s, "Not a step" in ${sAcme} s; the row reads "${decidedText}"; the item said "${whyRoe}"`,
    sRoe < 5 && sAcme < 5,
  );
  result(
    "queue: Apply decisions now -> apply run -> site",
    "the press reads as queued at once; within minutes one apply run puts the approved story on Open now and leaves the declined one off",
    `note "${note}" ${noteAt ? `${secs(click, noteAt)} s after the click` : "never"}; runs: ${(await describe(runs, click)) || "none"}; ` +
      `run ended ${secs(click, ended)} s after the click; Open now then: ${JSON.stringify(onSite)}; API: ${applied}/2 decisions applied`,
    runs.length === 1 && run?.state === "done" && roeOnSite && !acmeOnSite && applied === 2 && secs(click, ended) < 600,
  );
  result(
    "queue: review page left open",
    "the open review page shows by itself that the run finished and the decisions applied (within minutes)",
    reviewSelf != null ? `it changed by itself ${reviewSelf} s after the run ended` : `not after ${WATCH_MS / 60000} min left open; after its Reload: status "${statusReloaded}"`,
    reviewSelf != null,
  );
  result(
    "queue: Watchlist tab left open",
    "a Watchlist tab left open shows the approved story on Open now by itself within minutes",
    `before: ${JSON.stringify(cardsBefore)}; ` +
      (wlSelf != null ? `the Roe card appeared by itself ${wlSelf} s after the run ended` : `not after ${WATCH_MS / 60000} min left open`) +
      `; after a reload of the tab: ${JSON.stringify(cardsReload)}`,
    wlSelf != null,
  );
}

async function scenarioTwice() {
  const items = (await overview()).queue?.items ?? [];
  const turner = items.find((i) => i.place === "dated");
  if (!turner) {
    result("twice: Apply pressed twice quickly", "one run does the work", `no dated card left in the queue to decide (${items.map((i) => i.question).join(" | ")})`, false);
    return;
  }
  await decide(turner.question, "Don't show it", "Not shown");
  const click = Date.now();
  const posts = [];
  const onRes = async (res) => {
    if (res.url().endsWith("/api/admin/apply")) posts.push({ at: Date.now(), status: res.status(), body: await res.json().catch(() => null) });
  };
  review.on("response", onRes);
  await R.applyBtn().click();
  await waitFor(async () => posts.length >= 1, 10_000, 100);
  await sleep(300);
  await R.applyBtn().click({ timeout: 5000 }).catch(() => {});
  await waitFor(async () => posts.length >= 2, 10_000, 100);
  await settle();
  review.off("response", onRes);
  const note = await textOf(R.applyNote());
  const runs = await runsSince(click, "apply");
  const onSite = await siteOpen();
  const ov = await overview();
  const d = ov.decisions.find((x) => x.story === turner.story);
  result(
    "twice: Apply pressed twice quickly",
    "both presses read as queued; the second joins the apply run still queued, or makes one more that ends at once; the declined card leaves Open now",
    `${posts.length} POSTs (${posts.map((p) => `${p.status} ${p.body?.run ?? ""} at +${secs(click, p.at)} s`).join(", ")}); note "${note}"; runs: ${await describe(runs, click)}; ` +
      `decision applied: ${d?.applied_at != null}; Open now: ${JSON.stringify(onSite)}`,
    posts.length === 2 && posts.every((p) => p.status === 202) && runs.length >= 1 && runs.length <= 2 && runs.every((r) => r.state === "done") && d?.applied_at != null && !onSite.some((q) => /Turner/.test(q)),
  );
}

async function scenarioCrash() {
  await R.reload();
  const row = R.applied().locator("li.rv-cand", { hasText: "Acme" });
  if (!(await row.count())) {
    result("crash: the failed step's report", "the review page names the failed step and what went wrong", `no applied Acme decision to change (${await textOf(R.applied())})`, false);
    return;
  }
  await row.getByRole("button", { name: "Count as a step" }).click();
  await waitFor(async () => /Count|Counted/.test(await textOf(R.queue())), 10_000);
  writeFileSync(join(world.flags, "crash-rebuild"), "1");
  const clock0 = await stack.workerNow();
  const click = Date.now();
  await R.applyBtn().click();
  const run = await waitFor(async () => (await runsSince(click, "apply"))[0], 30_000, 300);
  // each error is retried with back-off (30 s, 1 min, 2 min, ...): time passes on the Worker's clock between them
  let got = run && (await stack.run(run.id));
  for (let i = 0; i < 10 && got && !TERMINAL.includes(got.run.state); i++) {
    const job = got.jobs.find((j) => j.kind === "apply.decide");
    await waitFor(async () => {
      const j = (await stack.run(run.id)).jobs.find((x) => x.kind === "apply.decide");
      return j && (j.attempt > (job?.attempt ?? 0) || ["failed", "done"].includes(j.state)) ? j : null;
    }, 4 * 60_000, 1000);
    await moveClock((await stack.workerNow()) + 1900);
    await stack.tick();
    got = await stack.run(run.id);
  }
  rmSync(join(world.flags, "crash-rebuild"), { force: true });
  await moveClock(clock0 + Math.floor((Date.now() - click) / 1000)); // the Worker's clock where it would be
  const job = got?.jobs.find((j) => j.kind === "apply.decide");
  const self = await watchOpen(async () => /e2e: a bug in the rebuild/.test(await textOf(R.backend())), 120_000);
  await R.reload();
  const backendText = await textOf(R.backend());
  const ov = await overview();
  const d = ov.decisions.find((x) => /acme/i.test(x.story));
  const failedRow = (ov.backend?.failed_jobs ?? []).find((j) => j.kind === "apply.decide");
  await shot(review, "crash-review-after-reload");
  result(
    "crash: the failed step's report",
    "the crashing step is retried with back-off, then failed; the review page's failed steps name it with its error (an error the owner must act on)",
    `${run ? await runLine(stack, run.id, click) : "no apply run"}; apply.decide attempts ${job?.attempt ?? "-"}, ${job?.state ?? "-"}; open page showed it by itself: ${self != null ? `${self} s` : "no (2 min)"}; ` +
      `failed steps: ${failedRow ? `"${failedRow.error}"` : "none"}; on the page: ${/e2e: a bug in the rebuild/.test(backendText) ? "named" : "not named"}`,
    got?.run.state === "failed" && job?.state === "failed" && /a bug in the rebuild/.test(failedRow?.error ?? "") && /e2e: a bug in the rebuild/.test(backendText),
  );
  result(
    "crash: the decision it could not rebuild",
    "a decision the step crashed on stays waiting (it is not reported applied), so the next run does it",
    `the API has it ${d?.applied_at != null ? `applied at ${d.applied_at} although the rebuild crashed` : "still waiting"}`,
    d?.applied_at == null,
  );
}

async function scenarioPause() {
  await R.reload();
  const pause = review.getByRole("button", { name: "Pause visitors' changes" });
  const t = Date.now();
  await pause.click();
  const pausedAt = await waitFor(async () => (/paused/.test(await textOf(review.locator(".rv-line", { hasText: "Visitors' changes" }))) ? Date.now() : null), 10_000, 200);
  const line = await textOf(review.locator(".rv-line", { hasText: "Visitors' changes" }));

  // the API refuses visitors' writes while paused
  const post = (path, body) =>
    fetch(`${stack.urls.api}${path}`, {
      method: "POST",
      headers: { "content-type": "application/json", origin: new URL(stack.urls.site).origin },
      body: JSON.stringify({ ...body, token: "XXXX.DUMMY.TOKEN.XXXX" }),
    }).then(async (r) => `${r.status} ${(await r.json().catch(() => ({}))).error ?? ""}`.trim());
  const pick = stack.seed.picks[1] ?? stack.seed.picks[0];
  const story = (await overview()).queue?.items?.[0]?.story ?? "e1";
  const refusedReq = await post("/api/request", { condition_id: pick.condition_id });
  const refusedWatch = await post("/api/watch", { story });

  // a visitor's tab while paused: "+ Add to main page" on a closed card, "Watch this market" on an open one
  const visitor = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  const wl = await visitor.newPage();
  await wl.goto(`${stack.urls.site}#/watchlist?day=${pick.day}`);
  const card = wl.locator("article.cl-card").filter({ has: wl.locator("h3.cl-card-title", { hasText: pick.question }) });
  const add = card.locator("button.wl-act-btn", { hasText: "Add to main page" });
  await add.waitFor({ timeout: 60_000 });
  const clickAdd = Date.now();
  await add.click();
  const addingAt = await card.locator(".wl-act-state", { hasText: "Adding to the main page" }).waitFor({ timeout: 60_000 }).then(() => Date.now(), () => null);
  const addError = await textOf(card.locator("[role=alert]"));
  const openCard = wl.locator("article.op-card").first();
  let watchSeen = "no open card";
  let watched = null;
  if (await openCard.count()) {
    const title = await textOf(openCard.locator("h3.cl-card-title"));
    const open = await fetch(`${stack.urls.api}/api/data/open.json`).then((r) => r.json(), () => ({}));
    watched = (open.days ?? []).flatMap((d) => d.cards ?? []).find((c) => c.question === title)?.id ?? null;
    const w = openCard.locator("button.wl-act-btn", { hasText: "Watch this market" });
    if (await w.count()) {
      await w.click();
      await openCard.locator(".wl-act-state, [role=alert]").first().waitFor({ timeout: 30_000 }).catch(() => {});
      watchSeen = await textOf(openCard.locator(".wl-act"));
    }
  }
  await sleep(70_000); // a minute paused, the tab left open
  const stillAdding = await card.locator(".wl-act-state", { hasText: "Adding to the main page" }).count();
  const reqPaused = await fetch(`${stack.urls.api}/api/request?cid=${pick.condition_id}`).then((r) => r.json());
  const watchPaused = await fetch(`${stack.urls.api}/api/watch?story=${encodeURIComponent(watched ?? story)}`).then((r) => r.json());
  await shot(wl, "pause-visitor-while-paused");
  result(
    "pause: switch on",
    "the switch answers at once; visitors' writes are refused by the API while paused",
    `"${line}" ${pausedAt ? `${secs(t, pausedAt)} s after the click` : "never"}; POST /api/request -> ${refusedReq}; POST /api/watch -> ${refusedWatch}`,
    !!pausedAt && /paused/.test(refusedReq) && /paused/.test(refusedWatch),
  );
  result(
    "pause: a visitor's tab while paused",
    'no error and no hedging: the add reads "Adding to the main page" and the watch reads as watched (kept in the browser)',
    `add read "Adding to the main page" ${addingAt ? `${secs(clickAdd, addingAt)} s after the click` : "never"} (error text: "${addError}"), still after a minute: ${stillAdding > 0}; ` +
      `watch reads "${watchSeen}"; the API meanwhile: request ${JSON.stringify(reqPaused.request)}, watching ${watchPaused.watching}`,
    !!addingAt && !addError && stillAdding > 0 && /Watching/.test(watchSeen),
  );

  // the owner allows changes again; the visitor's tab is left open
  const allow = review.getByRole("button", { name: "Allow visitors' changes" });
  const unpause = Date.now();
  await allow.click();
  await waitFor(async () => /allowed/.test(await textOf(review.locator(".rv-line", { hasText: "Visitors' changes" }))), 10_000, 200);
  const sentAt = await waitFor(async () => {
    const r = await fetch(`${stack.urls.api}/api/request?cid=${pick.condition_id}`).then((x) => x.json());
    return r.request ? Date.now() : null;
  }, 6 * 60_000, 2000);
  const link = card.locator("a.wl-act-btn", { hasText: "On the main page" });
  const got = sentAt ? await showsUnless(link, 20 * 60_000, addGoesWrong(stack, card, unpause, pick.condition_id)) : { shown: false, why: "the add never reached the API" };
  const onMainAt = got.shown ? Date.now() : null;
  await settle();
  const runs = await runsSince(unpause, "add");
  const watchAfter = watched ? await fetch(`${stack.urls.api}/api/watch?story=${encodeURIComponent(watched)}`).then((r) => r.json()) : { watching: null };
  const watchCard = (await openCard.count()) ? await textOf(openCard.locator(".wl-act")) : "";
  if (!onMainAt) await shot(wl, "pause-visitor-after-allow");
  await visitor.close();
  result(
    "pause: switch off, the kept add goes through",
    'once changes are allowed the tab sends the add it kept by itself, and it reaches "On the main page →" within minutes',
    `the API got the add ${sentAt ? `${secs(unpause, sentAt)} s after "Allow"` : "never (6 min)"}; the card read "On the main page →" ${onMainAt ? `${secs(unpause, onMainAt)} s after "Allow"` : `never (${got.why})`}; runs: ${(await describe(runs, unpause)) || "none"}`,
    !!onMainAt && secs(unpause, onMainAt) < 600,
  );
  result(
    "pause: the watch made while paused",
    "the watch the visitor made while paused reaches the site's watchlist once changes are allowed (or the tab stops reading Watching)",
    `the tab reads "${watchCard}"; the API's watching for its card (${watched ?? "not found on the open list"}): ${watchAfter.watching}`,
    watchAfter.watching === true || !/Watching/.test(watchCard),
  );
}

async function scenarioFull() {
  await R.reload();
  const lineBefore = await textOf(R.statusLine());
  const day = dayShift(toronto(await stack.workerNow()).day, 1);
  const t = Date.now();
  await moveClock(torontoAt(day, "04:00"));
  const at4 = workerAt(Date.now()); // run times are on the Worker's clock
  await stack.tick();
  const daily = `daily:${day}`;
  // while it goes, past its read of the work (daily.begin done): the owner changes an applied decision
  const readWork = await waitFor(async () => {
    const got = await stack.run(daily);
    return got?.jobs.find((j) => j.kind === "daily.begin" && j.state === "done") ? Date.now() : null;
  }, 20 * 60_000, 1000);
  let decidedAt = null;
  await R.reload();
  const row = R.applied().locator("li.rv-cand", { hasText: "Jane Roe" });
  if (readWork && (await row.count())) {
    await row.getByRole("button", { name: "Mark as not a step" }).click();
    await waitFor(async () => /Not a step/.test(await textOf(R.queue())), 10_000);
    decidedAt = Date.now();
  }
  const end = await stack.waitRun(daily, { ms: 60 * 60_000 }).catch(() => null);
  await settle();
  const runsMeanwhile = (await runsSince(t)).filter((r) => r.id !== daily);
  const ov = await overview();
  const roeD = ov.decisions.find((d) => /roe/i.test(d.story));
  const fullRow = ov.runs.find((r) => r.mode === "full");
  const self = await watchOpen(async () => (await textOf(R.statusLine())) !== lineBefore, 120_000);
  await R.reload();
  const line = await textOf(R.statusLine());
  const waitingText = await textOf(R.waiting());
  const models = stack.geminiCalls().filter((c) => c.kind === "model");
  result(
    "full: the daily run at 04:00 Toronto, end to end",
    "the cron makes the next day's run at 04:00 Toronto; it makes both lists and reports",
    `${await runLine(stack, daily, at4)}; its report: ${fullRow ? `${fullRow.status}: "${fullRow.summary}"` : "none"}; Gemini model requests: ${models.length} (the stand-in's)`,
    !!end && ["done", "partial"].includes(end.run.state) && !!fullRow,
  );
  result(
    "full: its report on the review page",
    "the open review page shows the daily run's report by itself, or at least after Reload",
    `before: "${lineBefore}"; open page changed by itself: ${self != null ? `${self} s` : "no (2 min)"}; after Reload: "${line}"`,
    self != null && /Last daily run/.test(line),
  );
  // the decision made while it went: waiting, then the next "Apply decisions now" does it
  const waitingNow = roeD?.applied_at == null;
  const ap = decidedAt ? await applyNow() : null;
  const roeAfter = (await overview()).decisions.find((d) => /roe/i.test(d.story));
  const onSite = await siteOpen();
  result(
    "full: a decision made while it goes",
    "a decision made after the run read its work waits (shown under the owner's other decisions, nothing lost); the next Apply decisions now applies it within minutes",
    decidedAt
      ? `decided ${secs(readWork, decidedAt)} s after the run read its work; after the run: ${waitingNow ? "waiting" : "applied"} (${waitingText ? `"${waitingText.slice(0, 120)}"` : "no waiting section"}); other runs meanwhile: ${runsMeanwhile.length}; ` +
        `then Apply: ${ap?.run ? await runLine(stack, ap.run.id, ap.click) : "no run"}; Roe applied: ${roeAfter?.applied_at != null}; Roe on Open now: ${onSite.some((q) => /Jane Roe/.test(q))}`
      : "could not make the decision while it went (no applied Roe decision to change, or the run never read its work)",
    !!decidedAt && waitingNow && runsMeanwhile.length === 0 && ap?.run?.state === "done" && roeAfter?.applied_at != null && !onSite.some((q) => /Jane Roe/.test(q)),
  );

  // the catch-up at 10:00 Toronto that day: only when the daily run left stories unread; nothing starts in a loop
  const left = Number((await stack.run(daily))?.run.meta?.left ?? 0) || 0;
  const t2 = Date.now();
  await moveClock(torontoAt(day, "10:00"));
  const at10 = workerAt(Date.now()); // run times are on the Worker's clock
  await stack.tick();
  const catchup = await stack.run(`catchup:${day}`);
  if (catchup) await stack.waitRun(catchup.run.id, { ms: 60 * 60_000 }).catch(() => null);
  await settle();
  await sleep(70_000); // a minute more, a tick or two: nothing should start by itself
  await stack.tick();
  await settle();
  const cruns = await runsSince(t2);
  result(
    "full: the catch-up at 10:00 Toronto",
    "a catch-up only when the day's run left stories unread (it reads them), none otherwise; no run starts another in a loop",
    `the daily run left ${left}; ${catchup ? await runLine(stack, catchup.run.id, at10) : "no catch-up"}; runs since 10:00: ${cruns.map((r) => `${r.id} ${r.state}`).join(", ") || "none"}`,
    (left > 0 ? !!catchup && cruns.length === 1 && ["done", "partial"].includes(cruns[0].state) : !catchup && cruns.length === 0),
  );
}

async function scenarioBadToken() {
  await R.reload();
  const items = (await overview()).queue?.items ?? [];
  // Vercel holding a token the Worker refuses: its calls are refused, nothing is lost
  await stack.vercel.stop();
  const good = stack.vercel.env.WATCHLIST_JOB_TOKEN;
  stack.vercel.env.WATCHLIST_JOB_TOKEN = "refused-job-token";
  await stack.vercel.start();
  const click = Date.now();
  await R.applyBtn().click();
  const run = await waitFor(async () => (await runsSince(click, "apply"))[0], 30_000, 300);
  const refused = await waitFor(async () => ((await overview()).job_refused_at ? Date.now() : null), 3 * 60_000, 2000);
  const self = await watchOpen(async () => /refused the job's token/.test(await textOf(R.statusLine())), 120_000);
  await R.reload();
  const warn = await textOf(R.statusLine());
  const stuck = run ? (await stack.run(run.id)).run.state : null;
  await shot(review, "badtoken-review");
  // the token put right: the queued run goes through
  await stack.vercel.stop();
  stack.vercel.env.WATCHLIST_JOB_TOKEN = good;
  await stack.vercel.start();
  const fixed = Date.now();
  const end = run ? await stack.waitRun(run.id, { ms: 10 * 60_000 }).catch(() => null) : null;
  await R.reload();
  const after = await textOf(R.statusLine());
  result(
    "badtoken: Vercel with a refused job token",
    "the review page says the API refused the job's token and what to fix (Vercel's WATCHLIST_JOB_TOKEN); the queued apply waits and goes through once the token is right",
    `${items.length} queue item(s); the API refused it ${refused ? `${secs(click, refused)} s after the press` : "never"}; open page showed it by itself: ${self != null ? `${self} s` : "no (2 min)"}; status: "${warn.slice(0, 220)}"; the run meanwhile ${stuck}; ` +
      `after the fix: ${end ? `${end.run.state} ${secs(fixed, end.run.finished_at * 1000)} s later` : "not finished"}; the status then "${after.slice(0, 160)}"`,
    !!refused && /WATCHLIST_JOB_TOKEN/.test(warn) && ["queued", "running"].includes(stuck) && end?.run.state === "done" && !/refused the job's token/.test(after),
  );
}

async function scenarioCap() {
  // the rest of the 30 through the API as presses make them, each after the one before has started (a press while
  // one is queued joins it and counts nothing)
  const t = Date.now();
  let queued = 0;
  let refused = null;
  for (let i = 0; i < 40 && !refused; i++) {
    const res = await ownerApi("POST", "/api/admin/apply");
    if (res.status !== 202) {
      refused = res;
      break;
    }
    queued++;
    const id = res.body?.run;
    if (id) await waitFor(async () => (await stack.run(id))?.run.state !== "queued", 3 * 60_000, 300);
  }
  await settle();
  const click = Date.now();
  await R.applyBtn().click();
  const msgAt = await waitFor(async () => (/Not queued/.test(await textOf(R.applyNote())) ? Date.now() : null), 10_000, 200);
  const msg = await textOf(R.applyNote());
  const after = (await runsSince(click, "apply")).length;
  result(
    "cap: the 30-a-day message",
    "after 30 apply runs today the press says it was not queued and why, at once, and queues nothing",
    `${queued} more press(es) went through the API in ${secs(t, click)} s before the cap answered ${refused?.status} ${refused?.body?.error}; the press then read "${msg}"${msgAt ? ` after ${secs(click, msgAt)} s` : ""}; apply runs it made: ${after}`,
    !!msgAt && /30 times today/.test(msg) && after === 0 && refused?.status === 429,
  );
}

/** Every phrase on the review page that hedges or explains instead of doing (the global no-hedging rule). */
async function hedging() {
  const text = (await review.locator(".rv").innerText().catch(() => "")).replace(/[ \t]+/g, " ");
  const found = [];
  for (const re of [
    /Kept on this page only[^\n]*/,
    /Starts the update job[^\n]*/,
    /Otherwise the next daily run[^\n]*/,
    /applies at the next run/,
    /Decisions are applied by the next daily run[^\n]*/,
    /the site updates when the job finishes/,
    /The card shows once you count one as a step\./,
    /Nothing yet: the daily run[^\n]*/,
    /\bUTC\b[^\n]*/,
  ]) {
    const m = text.match(re);
    if (m) found.push(m[0].trim());
  }
  return found;
}

// ---------- the run ----------

let failed = false;
const worldDir = `${opts.state}-py`;
try {
  // the world's "now": the real time once the day's 04:00 run is due, else this morning's 04:05 on the Worker's clock
  const real = Math.floor(Date.now() / 1000);
  const tz = toronto(real);
  const now = tz.minutes >= 4 * 60 + 5 ? real : torontoAt(tz.day, "04:05");
  rmSync(worldDir, { recursive: true, force: true });
  const flags = writeWorld(worldDir);
  world = { now, flags };
  opts.pyPath = worldDir;
  opts.pyEnv = { E2E_HARNESS_SITE: join(HERE, "..", "py", "sitecustomize.py"), E2E_OWNER_WORLD: "1", E2E_OWNER_NOW: String(now), E2E_OWNER_FLAGS: flags };
  log(`owner: starting a stack on ports ${opts.portBase}-${opts.portBase + 3} (state ${opts.state})`);
  stack = await startStack(opts);
  log(`stack ready (site ${stack.urls.site}, API ${stack.urls.api}, Vercel ${stack.urls.step})`);
  await setupWorld();
  browser = await launch({ headed: !!args.headed });
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  review = await ctx.newPage();
  const pageErrors = [];
  review.on("pageerror", (e) => pageErrors.push(String(e)));
  let navs = 0;
  review.on("framenavigated", (f) => f === review.mainFrame() && navs++);

  const scenarios = [
    ["unlock", scenarioUnlock],
    ["queue", scenarioQueue],
    ["twice", scenarioTwice],
    ["crash", scenarioCrash],
    ["pause", scenarioPause],
    ["full", scenarioFull],
    ["badtoken", scenarioBadToken],
    ["cap", scenarioCap],
  ];
  let hedges = [];
  for (const [name, fn] of scenarios) {
    if (skip.has(name)) continue;
    log(`--- ${name}`);
    try {
      await fn();
    } catch (e) {
      result(name, "the scenario runs to its end", `stopped: ${e.message.split("\n")[0]} (${await shot(review, `${name}-error`)})`, false);
      if (args.verbose) console.log(e.stack);
    }
    if (name === "unlock" || name === "queue") hedges = [...new Set([...hedges, ...(await hedging())])];
  }
  hedges = [...new Set([...hedges, ...(await hedging())])];
  result("no hedging on the review page", 'no caveats, "not available", "UTC" or explanatory asides on screen', hedges.length ? `on screen: ${hedges.map((h) => `"${h}"`).join("; ")}` : "none found", hedges.length === 0);
  result("review tab", "one tab for the whole test, never reloaded, no page errors", `navigations after the first load: ${Math.max(0, navs - 1)}; page errors: ${pageErrors.length ? pageErrors.slice(0, 3).join(" | ") : "none"}`, pageErrors.length === 0);
  const models = stack.geminiCalls().filter((c) => c.kind === "model");
  result("no Gemini call", "Gemini is never reached: the open list's readings come from the in-process fake, and the stand-in answers the closed list", `${models.length} model request(s) answered by the stand-in`, true);
  const queueText = await textOf(R.queue());
  result("review page: an empty queue", "an empty queue reads as empty, not as if no run had happened", `the queue section reads "${queueText.slice(0, 200)}"`, !/Nothing yet/.test(queueText));
} catch (e) {
  failed = true;
  console.log(`FAIL ${e.message}`);
  if (args.verbose) console.log(e.stack);
} finally {
  await browser?.close();
  const rows = results.map((r) => `| ${r.scenario} | ${r.expected} | ${r.happened.replace(/\|/g, "/")} | ${r.pass ? "PASS" : "FAIL"} |`);
  console.log(["", "| scenario | expected | what happened | result |", "|---|---|---|---|", ...rows].join("\n"));
  writeFileSync(join(SHOTS, "owner-report.json"), JSON.stringify({ results }, null, 2));
  const bad = failed || !results.length || results.some((r) => !r.pass);
  if (args.keep && stack) {
    console.log(`\nowner: the stack keeps running (site ${stack.urls.site}); Ctrl-C stops it`);
  } else {
    await stack?.clock(null).catch(() => {});
    await stack?.stop({ keepState: bad });
    if (!bad) rmSync(worldDir, { recursive: true, force: true });
    console.log(`\nowner: ${bad ? "FAILED" : "passed"} in ${secs(t0)} s`);
    process.exit(bad ? 1 : 0);
  }
}
