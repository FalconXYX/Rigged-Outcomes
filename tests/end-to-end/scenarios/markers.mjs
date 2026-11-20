// Timeline markers on a market page (#/m/<name>), the way people use them, through the whole stack with real
// timing. It starts a stack (tests/end-to-end/stack.mjs), opens one market's timeline in several browser contexts
// (each one a different person: its own storage, so its own undo tokens) and drives the Events panel with real
// clicks:
//
//   add          a marker through the bot check: it shows on the adder's chart and list at once, with Remove
//   second       a second person opening the page afterwards sees it, tagged "added", with no Remove
//   left open    a page opened before someone else's add, left open (never reloaded): does it show it by itself?
//   reload       after a reload both people see every marker; the adder still has Remove
//   remove own   the adder removes theirs (within 24 h): gone for them, the API and a fresh visitor
//   not theirs   the other person has no Remove, and the API refuses a forged undo (403 not_yours)
//   owner        the owner deletes one on #/review: gone for a fresh visitor; the adder's open page?
//   worker down  the API stops while a page is open and the person adds: what they see, and whether the
//                marker ever reaches the API once it is back (page left open, then a reload)
//   pause        the owner's pause switch: a new marker doesn't reach the API; what the person sees; after
//                "Allow", the same tab's next add
//   limit        one person's daily limit (10 a Toronto day): the add after it reads the limit's words, with no
//                "UTC" in them
//
// With --bot-fail the stack uses Turnstile's always-failing keys and only the bot-check refusal runs.
//
//   node tests/end-to-end/scenarios/markers.mjs [--port-base 9640] [--market fired] [--state DIR] [--bot-fail]
//                                               [--headed] [--wait-open SECONDS] [--verbose] [--keep]
//
// Screenshots of failures go to --shots (default <scratchpad>/e2e-shots/markers). The stack's state folder is
// removed when every scenario passed (--keep keeps the stack running and its folder). Exit 0 passed, 1 failed.

import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { launch } from "../lib/browser.mjs";
import { secs, sleep } from "../lib/proc.mjs";
import { OWNER_TOKEN } from "../lib/worker.mjs";
import { parseArgs, stackOptions, startStack } from "../stack.mjs";

const args = parseArgs(process.argv.slice(2));
const opts = stackOptions({ "port-base": 9640, picks: 1, cards: 1, ...args, quiet: !args.verbose });
const MARKET = String(args.market ?? "fired");
const WAIT_OPEN_MS = Number(args["wait-open"] ?? 180) * 1000;
const SHOTS = String(
  args.shots ?? "/private/tmp/claude-501/-Users-parthjain-Documents-Code-CCT416-Rigged-Outcomes/be509dc4-4b5f-41b9-a722-8a1126673a44/scratchpad/e2e-shots/markers",
);
mkdirSync(SHOTS, { recursive: true });

/** Words that hedge or say something doesn't work; none of them may show on the market page. */
const HEDGE = /paused|not available|unavailable|offline|not (yet )?(saved|sent|updated)|only (in|on) this (browser|device)|kept (here|locally|in this browser)|\bbeta\b|\btrial\b|try again later|couldn't send/i;

const results = [];
const t0 = Date.now();
let stack;
let browser;
const pages = new Set();
const consoleErrors = [];

// ---------- the person's actions ----------

async function person(name) {
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  const page = await context.newPage();
  page.on("pageerror", (e) => consoleErrors.push(`${name}: ${e}`));
  page.__who = name;
  pages.add(page);
  return page;
}

const marketUrl = () => `${stack.urls.site}#/m/${MARKET}`;

/** Open the market's timeline; resolves once the Events control is there (the API has answered or failed). */
async function openMarket(page) {
  const at = Date.now();
  await page.goto(marketUrl());
  await page.locator("button.tl-ev-toggle").waitFor({ timeout: 60_000 });
  return Date.now() - at;
}

async function openPanel(page) {
  const toggle = page.locator("button.tl-ev-toggle");
  if ((await toggle.getAttribute("aria-expanded")) !== "true") await toggle.click();
  await page.locator(".tl-ev-panel").waitFor({ timeout: 10_000 });
}

/** The panel's rows: label, and whether it has Remove (else its tag). */
async function rows(page) {
  await openPanel(page);
  return page.locator(".tl-ev-list li").evaluateAll((lis) =>
    lis.map((li) => ({
      label: li.querySelector(".tl-ev-label")?.textContent ?? "",
      remove: !!li.querySelector("button.tl-ev-remove"),
      tag: li.querySelector(".tl-ev-from")?.textContent ?? null,
    })),
  );
}
const rowOf = async (page, label) => (await rows(page)).find((r) => r.label === label) ?? null;

/** Lines on the chart (markets.json markers plus added ones inside the span). */
const chartLines = (page) => page.locator(".tl-events .tl-event-line").count();
const chartHas = (page, label) =>
  page.locator("text.tl-event-label").evaluateAll((ts, l) => ts.some((t) => (t.textContent ?? "").replace(/…$/, "").length > 0 && l.startsWith((t.textContent ?? "").replace(/…$/, ""))), label);

/** The panel's own words (the markers' labels left out: they are what people typed). */
const panelText = async (page) =>
  (await page.locator(".tl-ev-panel").count())
    ? page.locator(".tl-ev-panel").evaluate((el) => {
        const copy = el.cloneNode(true);
        copy.querySelectorAll(".tl-ev-label, input").forEach((n) => n.remove());
        return copy.textContent ?? "";
      })
    : "";

/**
 * Type a label and press "Add marker". Resolves when the row shows or an error does: { ok, ms, error, status }
 * (status: the POST /api/markers answer, or "none" when it never got one).
 */
async function addMarker(page, label) {
  await openPanel(page);
  const input = page.locator('.tl-ev-form input:not([type="datetime-local"])');
  await input.click();
  await input.fill(label);
  let status = "none";
  const onRes = (res) => {
    if (res.request().method() === "POST" && res.url().endsWith("/api/markers")) status = res.status();
  };
  const onFail = (req) => {
    if (req.method() === "POST" && req.url().endsWith("/api/markers")) status = `failed (${req.failure()?.errorText})`;
  };
  page.on("response", onRes);
  page.on("requestfailed", onFail);
  const at = Date.now();
  await page.locator('.tl-ev-form button[type="submit"]').click();
  const row = page.locator(".tl-ev-list li .tl-ev-label", { hasText: label });
  const err = page.locator(".tl-ev-error");
  const which = await Promise.race([
    row.first().waitFor({ timeout: 60_000 }).then(() => "row"),
    err.waitFor({ timeout: 60_000 }).then(() => "error"),
  ]).catch(() => "timeout");
  const ms = Date.now() - at;
  await sleep(300); // let the response listener settle
  page.off("response", onRes);
  page.off("requestfailed", onFail);
  const error = which === "error" ? await err.innerText() : null;
  return { ok: which === "row", ms, error, status, which };
}

/** The API's live markers for the market (asked from Node, no cache). */
async function apiMarkers() {
  const r = await fetch(`${stack.urls.api}/api/markers?market=${MARKET}`, { cache: "no-store" }).then((x) => x.json());
  return r.markers ?? [];
}
const apiHas = async (label) => (await apiMarkers()).some((m) => m.label === label);

/** Poll `probe` every `every` ms until it is truthy or `ms` passes: the ms it took, or null. */
async function within(ms, probe, every = 5000) {
  const at = Date.now();
  while (Date.now() - at <= ms) {
    if (await probe()) return Date.now() - at;
    await sleep(every);
  }
  return null;
}

async function shoot(tag) {
  for (const p of pages) {
    if (p.isClosed()) continue;
    const file = join(SHOTS, `${tag}-${p.__who}.png`);
    await p.screenshot({ path: file, fullPage: false }).catch(() => {});
  }
}

const s = (ms) => `${(ms / 1000).toFixed(1)} s`;

/** One scenario: fn(note, check) records what was seen; any failed check (or a throw) fails it. */
async function scenario(key, name, expected, fn) {
  const notes = [];
  let pass = true;
  const note = (x) => notes.push(x);
  const check = (ok, what) => {
    if (!ok) pass = false;
    notes.push(`${ok ? "" : "FAIL: "}${what}`);
    return ok;
  };
  const at = Date.now();
  console.log(`\n[${key}] ${name}`);
  try {
    await fn(note, check);
  } catch (e) {
    pass = false;
    notes.push(`FAIL: threw ${String(e.message ?? e).split("\n")[0]}`);
  }
  if (!pass) await shoot(key);
  for (const n of notes) console.log(`  ${n}`);
  console.log(`  -> ${pass ? "PASS" : "FAIL"} (${secs(at)} s)`);
  results.push({ key, name, expected, notes, pass, secs: secs(at) });
}

// ---------- what the API counted ----------

/**
 * The markers this run's adds put in the API today, deleted ones included (the owner's overview lists the newest
 * 50): each counted against this address's daily limit (every person here is 127.0.0.1), whether its add was a
 * click or a marker kept in a browser and sent once the API took it.
 */
async function countedToday() {
  const ov = await stack.api("GET", "/api/admin/overview", undefined, OWNER_TOKEN);
  return (ov.body?.markers ?? []).filter((m) => m.market === MARKET && m.label.startsWith("E2E")).length;
}

// ---------- the owner's review page ----------

async function ownerPage() {
  const page = await person("owner");
  await page.goto(`${stack.urls.site}#/review`);
  await page.locator('input[type="password"]').fill(OWNER_TOKEN);
  await page.locator(".rv-unlock button[type=submit]").click();
  await page.locator("#rv-markers").waitFor({ timeout: 30_000 });
  return page;
}

// ---------- the run ----------

console.log(`markers: starting a stack on ports ${opts.portBase}-${opts.portBase + 3}${opts.botFail ? " (Turnstile always fails)" : ""}`);
try {
  stack = await startStack(opts);
  console.log(`markers: stack ready in ${secs(t0)} s (state ${stack.state}); market ${MARKET}`);
  const listed = await fetch(`${stack.urls.api}/api/markers?market=${MARKET}`).then((r) => r.json());
  if (!listed.listed) throw new Error(`the API doesn't list ${MARKET} in its catalog`);
  browser = await launch({ headed: !!args.headed });

  if (opts.botFail) {
    await scenario("bot-fail", "Add a marker, the bot check fails", "the marker isn't added; the bot check's words show (the person must act); the API has nothing", async (note, check) => {
      const a = await person("A");
      note(`page ready in ${s(await openMarket(a))}`);
      const r = await addMarker(a, "E2E bot fails");
      note(`after the click: ${r.which} in ${s(r.ms)} (POST ${r.status})${r.error ? `, it reads "${r.error}"` : ""}`);
      check(!r.ok, "the marker does not show as added");
      check(r.error === "The bot check didn't pass. Try again.", `the bot check's words show (${r.error})`);
      check(!(await apiHas("E2E bot fails")), "the API has no such marker");
      const reloadAt = Date.now();
      await a.reload();
      await a.locator("button.tl-ev-toggle").waitFor({ timeout: 60_000 });
      check(!(await rowOf(a, "E2E bot fails")), `after a reload (${secs(reloadAt)} s) it isn't there either`);
    });
  } else {
    const A = await person("A"); // the adder
    const B = await person("B"); // someone else, page left open from the start
    let serverAdds = 0; // this file's clicks that reached the API (the limit scenario reads the API's own count: countedToday)
    let fixedLines = 0;

    await scenario("add", "Add a marker (bot check)", "after Add marker, within seconds and with no reload: the row with Remove and a line on the chart; the API has it; no hedging text", async (note, check) => {
      note(`A's page ready in ${s(await openMarket(A))}`);
      note(`B's page ready in ${s(await openMarket(B))} (left open from here on)`);
      fixedLines = await chartLines(A);
      const r = await addMarker(A, "E2E first marker");
      if (r.ok) serverAdds++;
      check(r.ok, `the row shows ${s(r.ms)} after the click (POST ${r.status})${r.error ? `; error "${r.error}"` : ""}`);
      const row = await rowOf(A, "E2E first marker");
      check(row?.remove === true, `the row has Remove (${JSON.stringify(row)})`);
      const lines = await chartLines(A);
      check(lines === fixedLines + 1, `the chart has one more marker line (${fixedLines} -> ${lines})`);
      note(`chart label drawn: ${await chartHas(A, "E2E first marker")}`);
      check(await apiHas("E2E first marker"), "the API lists it");
      const text = await panelText(A);
      check(!HEDGE.test(text), `no hedging text in the panel${HEDGE.test(text) ? `: "${text.match(HEDGE)[0]}"` : ""}`);
      check(!(await A.locator(".tl-ev-error").count()), "no error shows");
    });

    await scenario("second", "It shows for a second browser context", 'a person opening the page after the add sees it at once, tagged "added", with no Remove', async (note, check) => {
      const C = await person("C");
      const ms = await openMarket(C);
      const row = await rowOf(C, "E2E first marker");
      check(!!row, `C (opened after the add) sees it on load (${s(ms)})`);
      check(row && !row.remove && row.tag === "added", `tagged "added", no Remove (${JSON.stringify(row)})`);
      check((await chartLines(C)) === fixedLines + 1, "C's chart has its line");
      await C.context().close();
      pages.delete(C);
    });

    await scenario("left-open", "Page left open while someone else adds", "B's page, open since before A's adds and never reloaded, shows A's markers by itself within minutes", async (note, check) => {
      const r = await addMarker(A, "E2E second marker");
      if (r.ok) serverAdds++;
      note(`A added "E2E second marker" (${r.ok ? `shown in ${s(r.ms)}` : `not added: ${r.error}`})`);
      const linesBefore = await chartLines(B);
      note(`B's page was opened before both adds; it now shows ${(await rows(B)).filter((x) => x.label.startsWith("E2E")).length} E2E marker(s) and ${linesBefore} chart line(s)`);
      const took = await within(WAIT_OPEN_MS, async () => !!(await rowOf(B, "E2E second marker")) && !!(await rowOf(B, "E2E first marker")), 10_000);
      check(took !== null, took !== null ? `B shows both by itself ${s(took)} after A's add` : `B still doesn't show A's markers after ${s(WAIT_OPEN_MS)} open (B's rows: ${JSON.stringify((await rows(B)).map((x) => x.label))})`);
      // moving around the site in the same tab, no reload
      await B.evaluate(() => (window.location.hash = "#/"));
      await B.locator("a.mc-link").first().waitFor({ timeout: 30_000 });
      await B.evaluate((m) => (window.location.hash = `#/m/${m}`), MARKET);
      await B.locator("button.tl-ev-toggle").waitFor({ timeout: 30_000 });
      const nav = await rowOf(B, "E2E second marker");
      check(!!nav, `after going to the home page and back in the same tab (no reload), B ${nav ? "shows" : "still doesn't show"} it`);
    });

    await scenario("reload", "Reload", "after a reload B sees both markers (tagged added); A sees both, still with Remove", async (note, check) => {
      let at = Date.now();
      await B.reload();
      await B.locator("button.tl-ev-toggle").waitFor({ timeout: 60_000 });
      const b = await rows(B);
      const bE = b.filter((x) => x.label.startsWith("E2E"));
      check(bE.length === 2 && bE.every((x) => !x.remove && x.tag === "added"), `B after a reload (${secs(at)} s): ${JSON.stringify(bE)}`);
      at = Date.now();
      await A.reload();
      await A.locator("button.tl-ev-toggle").waitFor({ timeout: 60_000 });
      const aE = (await rows(A)).filter((x) => x.label.startsWith("E2E"));
      check(aE.length === 2 && aE.every((x) => x.remove), `A after a reload (${secs(at)} s), both with Remove: ${JSON.stringify(aE)}`);
    });

    await scenario("remove-own", "Remove your own within 24 h", "A presses Remove: gone from A's list and chart at once, from the API, and for a fresh visitor; no error", async (note, check) => {
      await openPanel(A);
      const linesBefore = await chartLines(A);
      const at = Date.now();
      await A.locator(".tl-ev-list li", { hasText: "E2E second marker" }).locator("button.tl-ev-remove").click();
      const gone = await A.locator(".tl-ev-list li", { hasText: "E2E second marker" }).waitFor({ state: "detached", timeout: 30_000 }).then(() => Date.now() - at, () => null);
      check(gone !== null, `gone from A's list ${gone !== null ? `${s(gone)} after the click` : "never (30 s)"}`);
      check((await chartLines(A)) === linesBefore - 1, `A's chart lost its line (${linesBefore} -> ${await chartLines(A)})`);
      check(!(await A.locator(".tl-ev-error").count()), "no error shows");
      check(!(await apiHas("E2E second marker")), "the API no longer lists it");
      const D = await person("D");
      await openMarket(D);
      check(!(await rowOf(D, "E2E second marker")), "a fresh visitor doesn't see it");
      await D.context().close();
      pages.delete(D);
      const stale = await rowOf(B, "E2E second marker");
      note(`B (open since its reload) ${stale ? "still shows it (stale until B reloads)" : "no longer shows it"}`);
    });

    await scenario("not-theirs", "Can't remove someone else's", "B sees A's marker with no Remove; the API refuses a removal without A's undo token (403 not_yours)", async (note, check) => {
      const row = await rowOf(B, "E2E first marker");
      check(row && !row.remove, `B's row has no Remove (${JSON.stringify(row)})`);
      const id = (await apiMarkers()).find((m) => m.label === "E2E first marker")?.id;
      const forged = await B.evaluate(
        async ({ api, id }) => {
          const r = await fetch(`${api}/api/markers/${id}`, { method: "DELETE", headers: { "x-undo": "0".repeat(64) } });
          return { status: r.status, body: await r.json().catch(() => null) };
        },
        { api: stack.urls.api, id },
      );
      check(forged.status === 403 && forged.body?.error === "not_yours", `a forged undo from B's page: ${forged.status} ${forged.body?.error}`);
      check(await apiHas("E2E first marker"), "the marker is still there");
    });

    await scenario("owner-delete", "Owner deletes a marker from #/review", 'the owner presses Delete: the row reads "deleted by the owner"; a fresh visitor no longer sees it; the adder\'s open page drops it within minutes', async (note, check) => {
      const r = await addMarker(A, "E2E owner deletes me");
      if (r.ok) serverAdds++;
      check(r.ok, `A added "E2E owner deletes me" (${s(r.ms)})`);
      const O = await ownerPage();
      const row = O.locator(".rv-row", { hasText: "E2E owner deletes me" });
      check((await row.count()) === 1, "the review page lists it");
      const at = Date.now();
      await row.locator("button", { hasText: "Delete" }).click();
      const done = await row.locator(".rv-muted", { hasText: "deleted by the owner" }).waitFor({ timeout: 30_000 }).then(() => Date.now() - at, () => null);
      check(done !== null, `the row reads "deleted by the owner" ${done !== null ? `${s(done)} after the click` : "never"}`);
      check(!(await apiHas("E2E owner deletes me")), "the API no longer lists it");
      const E = await person("E");
      await openMarket(E);
      check(!(await rowOf(E, "E2E owner deletes me")), "a fresh visitor doesn't see it");
      await E.context().close();
      pages.delete(E);
      const took = await within(120_000, async () => !(await rowOf(A, "E2E owner deletes me")), 10_000);
      check(took !== null, took !== null ? `A's open page dropped it by itself ${s(took)} after the delete` : "A's open page still shows it 2 minutes after the owner deleted it (and still offers Remove)");
      await O.context().close();
      pages.delete(O);
      await A.reload();
      await A.locator("button.tl-ev-toggle").waitFor({ timeout: 60_000 });
      note(`after a reload A ${(await rowOf(A, "E2E owner deletes me")) ? "still shows it" : "no longer shows it"}`);
    });

    await scenario("worker-down", "The Worker down when adding", "A adds while the API is down: no hedging text; once the API is back the marker reaches it by itself (page left open) and survives a reload; a fresh visitor sees it", async (note, check) => {
      await openPanel(A);
      await stack.stopApi();
      note("the API stopped");
      const r = await addMarker(A, "E2E while down");
      note(`after the click: ${r.which} in ${s(r.ms)} (POST ${r.status})${r.error ? `, it reads "${r.error}"` : ""}`);
      const row = await rowOf(A, "E2E while down");
      note(`A's list: ${row ? `shows it (${row.remove ? "with Remove" : row.tag})` : "doesn't show it"}; panel note: "${(await A.locator(".tl-ev-note").innerText().catch(() => "")).trim()}"`);
      const text = await panelText(A);
      check(!HEDGE.test(text), `no hedging text${HEDGE.test(text) ? `: "${text.match(HEDGE)[0]}"` : ""}`);
      const at = Date.now();
      await stack.startApi();
      note(`the API is back (${secs(at)} s to start)`);
      const reached = await within(120_000, () => apiHas("E2E while down"), 10_000);
      check(reached !== null, reached !== null ? `the marker reached the API by itself ${s(reached)} after the API came back` : "the marker never reached the API in 2 minutes with A's page open");
      const r2 = await addMarker(A, "E2E after back");
      if (r2.ok && (await apiHas("E2E after back"))) serverAdds++;
      check(await apiHas("E2E after back"), `A's next add in the same tab, the API back: ${r2.ok ? "shows for A" : `not shown (${r2.error})`}, POST ${r2.status}, the API ${(await apiHas("E2E after back")) ? "has it" : "doesn't have it"}`);
      await A.reload();
      await A.locator("button.tl-ev-toggle").waitFor({ timeout: 60_000 });
      const afterReload = await rowOf(A, "E2E while down");
      check(!!afterReload, `after a reload A ${afterReload ? "still shows" : "no longer shows"} "E2E while down"`);
      const F = await person("F");
      await openMarket(F);
      check(!!(await rowOf(F, "E2E while down")), `a fresh visitor ${(await rowOf(F, "E2E while down")) ? "sees" : "doesn't see"} it`);
      await F.context().close();
      pages.delete(F);
    });

    await scenario("pause", "The pause switch blocking new markers", 'the owner pauses: a new marker doesn\'t reach the API or anyone else; no "paused" or hedging text on the market page; after "Allow", adds work again in the same tab', async (note, check) => {
      const O = await ownerPage();
      let at = Date.now();
      await O.locator("button", { hasText: "Pause visitors' changes" }).click();
      await O.locator(".rv-line", { hasText: "Visitors' changes: paused" }).waitFor({ timeout: 30_000 });
      note(`the owner paused visitors' changes (${secs(at)} s)`);
      const G = await person("G");
      await openMarket(G);
      const r = await addMarker(G, "E2E while paused");
      note(`G (page opened after the pause) adds: ${r.which} in ${s(r.ms)} (POST ${r.status})${r.error ? `, it reads "${r.error}"` : ""}`);
      const row = await rowOf(G, "E2E while paused");
      note(`G's list ${row ? `shows it (${row.remove ? "with Remove" : row.tag})` : "doesn't show it"}`);
      check(!(await apiHas("E2E while paused")), "the API doesn't have it (blocked)");
      const text = await panelText(G);
      check(!HEDGE.test(text), `no hedging text on the market page${HEDGE.test(text) ? `: "${text.match(HEDGE)[0]}"` : ""}`);
      const H = await person("H");
      await openMarket(H);
      check(!(await rowOf(H, "E2E while paused")), "another visitor doesn't see it");
      await H.context().close();
      pages.delete(H);
      at = Date.now();
      await O.locator("button", { hasText: "Allow visitors' changes" }).click();
      await O.locator(".rv-line", { hasText: "Visitors' changes: allowed" }).waitFor({ timeout: 30_000 });
      note(`the owner allowed visitors' changes again (${secs(at)} s)`);
      const reached = await within(60_000, () => apiHas("E2E while paused"), 10_000);
      note(`the paused marker ${reached !== null ? `reached the API ${s(reached)} after "Allow"` : "never reached the API (G's page left open a minute)"}`);
      const r2 = await addMarker(G, "E2E after allow");
      const inApi = await apiHas("E2E after allow");
      if (inApi) serverAdds++;
      check(inApi, `G's next add in the same tab after "Allow": ${r2.ok ? "shows for G" : `not shown (${r2.error})`}, POST ${r2.status}, the API ${inApi ? "has it" : "doesn't have it"}`);
      await G.reload();
      await G.locator("button.tl-ev-toggle").waitFor({ timeout: 60_000 });
      note(`after a reload G ${(await rowOf(G, "E2E while paused")) ? "still shows" : "no longer shows"} "E2E while paused" and ${(await rowOf(G, "E2E after allow")) ? "shows" : "doesn't show"} "E2E after allow"`);
      await O.context().close();
      pages.delete(O);
    });

    await scenario("limit", "The daily limit", "a person's adds work up to 10 a Toronto day; the next one shows the limit's words (\"Try again tomorrow.\", no UTC) and isn't added; nothing is kept as if added", async (note, check) => {
      const L = await person("L"); // a fresh tab of the same person (same address, so the same daily count)
      await openMarket(L);
      const counted = await countedToday();
      note(`adds the API already counted for this address today: ${counted} (this file's own count of its clicks that reached it: ${serverAdds})`);
      let n = counted;
      let refused = null;
      for (let i = 1; i <= 12 && !refused; i++) {
        const label = `E2E limit ${i}`;
        const r = await addMarker(L, label);
        if (r.ok && (await apiHas(label))) n++;
        else if (r.error) refused = { i, n, r, label };
        else refused = { i, n, r, label, silent: true };
      }
      check(!!refused, refused ? `refused after ${refused.n} adds today (POST ${refused.r.status}, ${s(refused.r.ms)})` : "never refused");
      check(refused?.n === 10, `the limit is 10 a day (${refused?.n})`);
      check(refused?.r.error === "You've reached today's limit. Try again tomorrow.", `it reads "${refused?.r.error}"`);
      if (refused) check(!(await rowOf(L, refused.label)), "the refused marker isn't shown as added");
    });
  }
} catch (e) {
  console.log(`FAIL ${e.message}`);
  results.push({ key: "setup", name: "the stack", expected: "starts", notes: [String(e.message)], pass: false });
  await shoot("setup");
} finally {
  if (consoleErrors.length) console.log(`\npage errors: ${consoleErrors.slice(0, 5).join(" | ")}`);
  await browser?.close();
  writeFileSync(join(SHOTS, `results${opts.botFail ? "-botfail" : ""}.json`), JSON.stringify({ results, consoleErrors }, null, 2));
  console.log("\nscenario | result | seconds");
  for (const r of results) console.log(`  ${r.pass ? "PASS" : "FAIL"}  ${r.name} (${r.secs ?? "-"} s)`);
  if (args.keep && stack) {
    console.log(`\nmarkers: the stack keeps running (site ${stack.urls.site}); Ctrl-C stops it`);
  } else {
    const failed = results.filter((r) => !r.pass).length;
    await stack?.stop({ keepState: failed > 0 });
    console.log(`\nmarkers: ${failed ? `FAILED (${failed})` : "passed"} in ${secs(t0)} s`);
    process.exit(failed ? 1 : 0);
  }
}
