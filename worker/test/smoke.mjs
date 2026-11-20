// Smoke test for the API against a running local Worker (wrangler dev with DEV=on, the always-passing
// Turnstile test secret and the dev tokens; test/run.mjs passes them all by --var). It replaces the catalog
// wholesale and moves the Worker's clock, so run it on a throwaway state (npm test does: a fresh --persist-to
// directory each time). Each run counts its daily limits under its own X-Test-Day and visitors under
// X-Test-Visitor, so it can run again on the same state.
//
//   node test/smoke.mjs http://localhost:8788 --step=URL       the whole suite; URL is test/run.mjs's stand-in for
//                                                              the step function (the Worker's STEP_URL, WAKE_SECRET
//                                                              test-wake-secret), which records every wake-up
//   node test/smoke.mjs http://localhost:8788 --bot-fail       only: a failing bot check is 403 bot_check (a Worker
//                                                              started with --var TURNSTILE_SECRET:2x0000000000000000000000000000000AA)
//   node test/smoke.mjs http://localhost:8788 --production     only: production settings (--var DEV:off --var TURNSTILE_STRICT:on):
//                                                              the dummy token fails the hostname check, test headers and
//                                                              the development endpoints are refused, no wake-up goes out
//   node test/smoke.mjs http://localhost:8788 --prune --step=URL   only: the cron's daily prune and its mending of an add
//                                                              with no run, once test/run.mjs has back-dated the state
// Exits 1 on any failed check, 2 when Cloudflare's siteverify could not be reached ("network": run again).

import { createHmac } from "node:crypto";
import { gzipSync } from "node:zlib";

const BASE = (process.argv[2] ?? "http://localhost:8788").replace(/\/$/, "");
const BOT_FAIL = process.argv.includes("--bot-fail");
const PRODUCTION = process.argv.includes("--production");
const PRUNE = process.argv.includes("--prune");
const STEP = process.argv.find((a) => a.startsWith("--step="))?.slice("--step=".length);
const WAKE_SECRET = "test-wake-secret";
const MODE = BOT_FAIL ? " (bot-check failure)" : PRODUCTION ? " (production settings)" : PRUNE ? " (prune)" : "";
const RUN = Date.now().toString(36) + Math.random().toString(36).slice(2, 6);
const DAY_KEY = `t-${RUN}`;
const JOB = "dev-job-token";
const TOKEN = "XXXX.DUMMY.TOKEN.XXXX";
const ALLOWED = "http://localhost:5190";

let passed = 0;
const failed = [];

function check(name, ok, detail) {
  if (ok) passed++;
  else {
    failed.push(name);
    console.log(`FAIL ${name}${detail === undefined ? "" : `\n     ${typeof detail === "string" ? detail : JSON.stringify(detail)}`}`);
  }
}

/** One request: { status, body (parsed JSON or null), headers }. With `ip`, the visitor is that address (X-Test-IP) instead of a named one. */
async function call(method, path, { body, visitor = "a", ip, auth, headers = {}, raw } = {}) {
  const h = { "x-test-day": DAY_KEY, ...(ip ? { "x-test-ip": ip } : { "x-test-visitor": `v-${RUN}-${visitor}` }), ...headers };
  if (auth) h.authorization = `Bearer ${auth}`;
  let payload;
  if (raw !== undefined) payload = raw;
  else if (body !== undefined) {
    payload = JSON.stringify(body);
    h["content-type"] = "application/json";
  }
  const res = await fetch(`${BASE}${path}`, { method, headers: h, body: payload });
  const textBody = await res.text();
  let parsed = null;
  try {
    parsed = textBody ? JSON.parse(textBody) : null;
  } catch {
    parsed = null;
  }
  if (parsed?.error === "bot_check_down") {
    console.log("network: Cloudflare's siteverify could not be reached; run the smoke test again.");
    process.exit(2);
  }
  return { status: res.status, body: parsed, headers: res.headers };
}

const expect = (name, r, status, code) =>
  check(name, r.status === status && (code === undefined || r.body?.error === code), { status: r.status, body: r.body });

const hex = (n, c) => c.repeat(n);
/** A market the suite saves and --prune checks after test/run.mjs back-dates its parts: one live version, one orphan. */
const PRUNE_MARKET = "market:prune-check";
/** Watched stories the suite leaves off Open now, which --prune finds 40 and 400 days old (test/run.mjs back-dates them). */
const PRUNE_WATCH = "prune watch";
const PRUNE_WATCH_YEAR = "prune watch year";
const PRUNE_LIVE_VERSION = "0".repeat(31) + "1";
const PRUNE_ORPHAN = "0".repeat(31) + "2";
const CID_PREFIX = crypto.randomUUID().replace(/-/g, "");
/** A condition id unique to this run. */
const cid = (i) => `0x${CID_PREFIX}${i.toString(16).padStart(32, "0")}`;
const now = () => Math.floor(Date.now() / 1000);

// ---------- names this run writes to ----------

const mA = `s${RUN}a`;
const mFull = `s${RUN}f`;
const mOnMain = `s${RUN}m`;
const mV6 = `s${RUN}v`;
const stories = { a: `story ${RUN} a`, b: `story ${RUN} b`, limit: `story ${RUN} limit` };
const CLOSED = Array.from({ length: 70 }, (_, i) => ({
  condition_id: cid(0xc0ffee00 + i),
  slug: i === 1 ? `onmain-${RUN}` : `closed-${RUN}-${i}`,
  question: `Will closed market ${i} happen?`,
  url: `https://polymarket.com/event/closed-${RUN}-${i}`,
  winner: "Yes",
  closed: "2026-11-12T10:00:00Z",
  day: "2026-11-12",
}));

async function botFailOnly() {
  const cat = await call("PUT", "/api/job/catalog", { auth: JOB, body: { markets: [{ name: mA, slug: `slug-${RUN}` }] } });
  expect("bot-fail: seed catalog", cat, 200);
  const r = await call("POST", "/api/markers", { body: { market: mA, t: 1.77e9, label: "Should fail", token: TOKEN } });
  expect("bot-fail: a token the secret rejects is 403 bot_check", r, 403, "bot_check");
}

async function productionOnly() {
  const cat = await call("PUT", "/api/job/catalog", { auth: JOB, body: { markets: [{ name: mA, slug: `slug-${RUN}` }] } });
  expect("production: seed catalog", cat, 200);
  const r = await call("POST", "/api/markers", { body: { market: mA, t: 1.77e9, label: "Wrong host", token: TOKEN } });
  expect("production: a token from another hostname is 403 bot_check", r, 403, "bot_check");
  for (let i = 1; i <= 10; i++) await call("GET", "/api/job/status", { visitor: `p${i}`, auth: "wrong" });
  expect("production: X-Test-Visitor is ignored (ten wrong tokens from one address lock it)", await call("GET", "/api/job/status", { visitor: "other", auth: "wrong" }), 429, "locked");
  expect("production: the right token still gets in from that address", await call("GET", "/api/job/status", { visitor: "other", auth: JOB }), 200);
  expect("production: X-Test-IP is ignored too", await call("GET", "/api/job/status", { ip: "2001:db8:ffff::1", auth: "wrong" }), 429, "locked");
  expect("production: no clock to move", await call("POST", "/api/dev/clock", { auth: JOB, body: { now: 1 } }), 404, "not_found");
  expect("production: no tick on demand", await call("POST", "/api/dev/tick", { auth: JOB }), 404, "not_found");
  if (STEP) {
    const before = (await (await fetch(`${STEP}/__wakes`)).json()).length;
    const sent = await call("POST", "/api/job/requests", { visitor: "pa", auth: JOB, body: { requests: [CLOSED[60]] } });
    await sleep(1500);
    check("production: a local STEP_URL is never woken (https only)", sent.status === 200 && sent.body?.requests?.length === 1 && (await (await fetch(`${STEP}/__wakes`)).json()).length === before, sent);
  }
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function suite() {
  // ---------- CORS, health, routing ----------
  {
    const pre = await fetch(`${BASE}/api/markers`, {
      method: "OPTIONS",
      headers: { origin: ALLOWED, "access-control-request-method": "POST", "access-control-request-headers": "content-type" },
    });
    check("cors: preflight from an allowed origin is 204", pre.status === 204, pre.status);
    check("cors: allowed origin is echoed", pre.headers.get("access-control-allow-origin") === ALLOWED);
    check("cors: methods listed", (pre.headers.get("access-control-allow-methods") ?? "").includes("DELETE"));
    check("cors: headers listed", (pre.headers.get("access-control-allow-headers") ?? "").includes("x-undo"));
    check("cors: max-age 86400", pre.headers.get("access-control-max-age") === "86400");
    const bad = await fetch(`${BASE}/api/markers`, {
      method: "OPTIONS",
      headers: { origin: "https://evil.example", "access-control-request-method": "POST" },
    });
    check("cors: preflight from a refused origin is 204", bad.status === 204);
    check("cors: refused origin is not echoed", bad.headers.get("access-control-allow-origin") === null);
    const get = await fetch(`${BASE}/api/health`, { headers: { origin: ALLOWED } });
    check("cors: a GET from an allowed origin is echoed", get.headers.get("access-control-allow-origin") === ALLOWED);
  }
  {
    const h = await call("GET", "/api/health");
    check("health", h.status === 200 && h.body?.ok === true, h);
    expect("unknown endpoint is 404", await call("GET", "/api/nothing"), 404, "not_found");
    expect("wrong method is 405", await call("PATCH", "/api/markers"), 405, "bad_method");
    for (const [method, path] of [["GET", "/api/admin/overview"], ["PUT", "/api/admin/settings"], ["POST", "/api/admin/apply"], ["PUT", "/api/job/queue"]]) {
      expect(`gone: ${method} ${path} is 404`, await call(method, path, { auth: JOB, body: method === "GET" ? undefined : {} }), 404, "not_found");
    }
  }

  // ---------- the catalog ----------
  const MARKETS = [
    { name: mA, slug: `slug-${RUN}-a` },
    { name: mFull, slug: `slug-${RUN}-f` },
    { name: mOnMain, slug: `event-${RUN}/onmain-${RUN}#2` },
    { name: mV6, slug: `slug-${RUN}-v` },
  ];
  {
    const r = await call("PUT", "/api/job/catalog", {
      auth: JOB,
      body: {
        markets: MARKETS,
        closed: CLOSED,
        open: [
          { story: stories.a, question: "Will A happen?", url: "https://polymarket.com/event/a" },
          { story: stories.b, question: "Will B happen?", url: "https://polymarket.com/event/b" },
          { story: stories.limit, question: "Will L happen?", url: "https://polymarket.com/event/l" },
        ],
      },
    });
    check("catalog: seeded", r.status === 200 && r.body?.market === 4 && r.body?.closed === 70 && r.body?.open === 3, r.body);
    // the markets section always keeps the added markets (their market:<name> records), whoever writes it last
    const rec = `market:cat-${RUN}`;
    const vc = crypto.randomUUID().replace(/-/g, "");
    await call("PUT", `/api/job/blobs/${rec}/${vc}/0`, { auth: JOB, body: { data: "UEFHRQ==" } });
    const meta = { condition_id: cid(0xca7), entry: { name: `cat-${RUN}`, slug: `cat-slug-${RUN}` } };
    expect("catalog: an added market's record", await call("POST", `/api/job/blobs/${rec}/${vc}`, { auth: JOB, body: { parts: 1, bytes: 4, sha256: hex(64, "a"), meta, expect: null } }), 200);
    const kept = await call("PUT", "/api/job/catalog", { auth: JOB, body: { markets: MARKETS } });
    check("catalog: a write that leaves out an added market keeps it", kept.body?.market === 5, kept.body);
    expect("catalog: the added market's record removed", await call("DELETE", `/api/job/blobs/${rec}`, { auth: JOB }), 204);
    check("catalog: ...and the next write drops it", (await call("PUT", "/api/job/catalog", { auth: JOB, body: { markets: MARKETS } })).body?.market === 4);
    const partial = await call("PUT", "/api/job/catalog", { auth: JOB, body: { open: [{ story: stories.a, question: "Will A happen?", url: "https://polymarket.com/event/a" }, { story: stories.b, question: "Will B happen?", url: "https://polymarket.com/event/b" }, { story: stories.limit, question: "Will L happen?", url: "https://polymarket.com/event/l" }] } });
    check("catalog: a section left out is kept", partial.body?.market === 4 && partial.body?.closed === 70, partial.body);
    const odd = await call("PUT", "/api/job/catalog", { auth: JOB, body: { markets: [...MARKETS, { name: "Bad Name", slug: "x" }] } });
    check("catalog: an entry that doesn't fit is skipped, the rest kept", odd.status === 200 && odd.body?.market === 4 && odd.body?.skipped?.length === 1, odd.body);
    expect("catalog: a section that isn't a list is 400", await call("PUT", "/api/job/catalog", { auth: JOB, body: { markets: "usiran" } }), 400, "bad_request");
  }

  // ---------- markers ----------
  const t0 = 1_772_240_000;
  let first;
  {
    const empty = await call("GET", `/api/markers?market=${mA}`);
    check("markers: listed market starts empty", empty.body?.listed === true && empty.body?.markers?.length === 0, empty.body);
    check("markers: list is cacheable for 15 s", (empty.headers.get("cache-control") ?? "").includes("max-age=15"));
    const unlisted = await call("GET", `/api/markers?market=nope-${RUN}`);
    check("markers: unlisted market", unlisted.body?.listed === false && unlisted.body?.markers?.length === 0, unlisted.body);
    expect("markers: bad market name on GET is 400", await call("GET", "/api/markers?market=Bad%20Name"), 400, "bad_request");

    const add = await call("POST", "/api/markers", { body: { market: mA, t: t0, label: "  Strikes   hit ", token: TOKEN, source: "ai" } });
    first = add.body;
    check("markers: add is 201", add.status === 201, add);
    check("markers: label cleaned", add.body?.marker?.label === "Strikes hit", add.body);
    check("markers: source is always manual", add.body?.marker?.source === "manual");
    check("markers: undo token is 64 hex", /^[0-9a-f]{64}$/.test(add.body?.undo ?? ""));
    const listed = await call("GET", `/api/markers?market=${mA}`);
    check("markers: a write clears the cached list", listed.body?.markers?.length === 1 && listed.body.markers[0].id === add.body?.marker?.id, listed.body);
    check("markers: list rows have the Marker shape", Object.keys(listed.body?.markers?.[0] ?? {}).sort().join() === "created_at,id,label,source,t");

    expect("markers: empty token is 403 bot_check", await call("POST", "/api/markers", { body: { market: mA, t: t0 + 999, label: "No token", token: "" } }), 403, "bot_check");
    expect("markers: missing token is 403 bot_check", await call("POST", "/api/markers", { body: { market: mA, t: t0 + 999, label: "No token" } }), 403, "bot_check");
    expect("markers: overlong token is 403 bot_check", await call("POST", "/api/markers", { body: { market: mA, t: t0 + 999, label: "Long", token: "x".repeat(2049) } }), 403, "bot_check");

    const badLabels = {
      empty: "",
      spaces: "    ",
      "41 characters": "a".repeat(41),
      http: "see http now",
      HTTPS: "HTTPS thing",
      "www.": "go to www.example",
      "<": "a <b> tag",
      ">": "a > b",
      "x.ab/": "bit.ly/abc",
      "control character": "bell\u0007here",
      "bidi override": "evil‮txt",
      "zero-width space": "zero​width",
    };
    for (const [why, label] of Object.entries(badLabels)) {
      expect(`markers: bad_label (${why})`, await call("POST", "/api/markers", { body: { market: mA, t: t0 + 5000, label, token: TOKEN } }), 400, "bad_label");
    }
    expect("markers: a label that isn't a string is 400 bad_request", await call("POST", "/api/markers", { body: { market: mA, t: t0, label: 5, token: TOKEN } }), 400, "bad_request");
    const forty = await call("POST", "/api/markers", { body: { market: mA, t: t0 + 7000, label: "b".repeat(40), token: TOKEN } });
    check("markers: 40 characters is fine", forty.status === 201, forty);
    const emoji = await call("POST", "/api/markers", { body: { market: mA, t: t0 + 8000, label: "👩‍🔬 lab result", token: TOKEN } });
    check("markers: an emoji with a zero-width joiner is fine", emoji.status === 201, emoji);
    const dotted = await call("POST", "/api/markers", { body: { market: mA, t: t0 + 9000, label: "U.S. strikes", token: TOKEN } });
    check("markers: U.S. is not a link", dotted.status === 201, dotted);

    const badTimes = { "before 1.4e9": 1_000_000_000, "not an integer": 1_772_240_000.5, "over 400 days ahead": now() + 401 * 86_400 };
    for (const [why, t] of Object.entries(badTimes)) {
      expect(`markers: bad_time (${why})`, await call("POST", "/api/markers", { body: { market: mA, t, label: "Time", token: TOKEN } }), 400, "bad_time");
    }
    expect("markers: t that isn't a number is 400 bad_request", await call("POST", "/api/markers", { body: { market: mA, t: "soon", label: "Time", token: TOKEN } }), 400, "bad_request");
    expect("markers: a bad market name is 400 bad_request", await call("POST", "/api/markers", { body: { market: "Bad Name", t: t0, label: "x", token: TOKEN } }), 400, "bad_request");
    expect("markers: unknown_market", await call("POST", "/api/markers", { body: { market: `nope-${RUN}`, t: t0, label: "x", token: TOKEN } }), 404, "unknown_market");

    expect("markers: duplicate (same label, other case, 30 s away)", await call("POST", "/api/markers", { body: { market: mA, t: t0 + 30, label: "STRIKES HIT", token: TOKEN } }), 409, "duplicate");
    const later = await call("POST", "/api/markers", { body: { market: mA, t: t0 + 120, label: "Strikes hit", token: TOKEN } });
    check("markers: the same label 2 minutes away is fine", later.status === 201, later);

    // visitor "a" has 5 now; 5 more reach the cap of 10, the 11th is refused
    for (let i = 0; i < 5; i++) {
      const r = await call("POST", "/api/markers", { body: { market: mA, t: t0 + 20_000 + i * 1000, label: `Limit ${i}`, token: TOKEN } });
      check(`markers: within the visitor's limit (${6 + i} of 10)`, r.status === 201, r);
    }
    expect("markers: the 11th today is 429 limit", await call("POST", "/api/markers", { body: { market: mA, t: t0 + 40_000, label: "One too many", token: TOKEN } }), 429, "limit");
  }
  {
    const id = first.marker.id;
    expect("markers: undo with a wrong token is 403 not_yours", await call("DELETE", `/api/markers/${id}`, { headers: { "x-undo": hex(64, "0") } }), 403, "not_yours");
    expect("markers: undo with no token is 403 not_yours", await call("DELETE", `/api/markers/${id}`), 403, "not_yours");
    expect("markers: undo with the right token is 204", await call("DELETE", `/api/markers/${id}`, { headers: { "x-undo": first.undo } }), 204);
    expect("markers: undoing again is 404", await call("DELETE", `/api/markers/${id}`, { headers: { "x-undo": first.undo } }), 404, "not_found");
    expect("markers: undo of an unknown id is 404", await call("DELETE", `/api/markers/${crypto.randomUUID()}`, { headers: { "x-undo": first.undo } }), 404, "not_found");
    const list = await call("GET", `/api/markers?market=${mA}`);
    check("markers: the undone marker is gone", !list.body?.markers?.some((m) => m.id === id));

  }

  {
    // 60 live markers on one market (6 visitors x 10, over three test days: a timeline takes 20 new ones a
    // day), then the 61st is refused
    let ok = 0;
    for (let v = 0; v < 6; v++) {
      for (let i = 0; i < 10; i++) {
        const headers = { "x-test-day": `${DAY_KEY}-f${Math.floor(v / 2)}` };
        const r = await call("POST", "/api/markers", { visitor: `full${v}`, headers, body: { market: mFull, t: t0 + (v * 10 + i) * 1000, label: `Full ${v} ${i}`, token: TOKEN } });
        if (r.status === 201) ok++;
        else check(`markers: fill ${v}/${i}`, false, r);
      }
    }
    check("markers: 60 live markers fit", ok === 60, ok);
    expect("markers: the 61st is 409 full", await call("POST", "/api/markers", { visitor: "full6", headers: { "x-test-day": `${DAY_KEY}-f9` }, body: { market: mFull, t: t0 + 999_000, label: "Sixty one", token: TOKEN } }), 409, "full");
  }
  {
    // IPv6: every address in one /64 is one visitor; another /64 is another. And a timeline takes 20 new markers a day.
    let ok = 0;
    for (let i = 0; i < 10; i++) {
      const r = await call("POST", "/api/markers", { ip: `2001:db8:${RUN.length}:1::${(i + 1).toString(16)}`, body: { market: mV6, t: t0 + i * 1000, label: `Six ${i}`, token: TOKEN } });
      if (r.status === 201) ok++;
      else check(`markers: v6 ${i}`, false, r);
    }
    check("markers: 10 from addresses in one /64", ok === 10, ok);
    expect("markers: the 11th from the same /64 (another address) is 429 limit", await call("POST", "/api/markers", { ip: `2001:0db8:${RUN.length}:0001:ffff:ffff:ffff:ffff`, body: { market: mV6, t: t0 + 50_000, label: "Same net", token: TOKEN } }), 429, "limit");
    for (let i = 0; i < 10; i++) {
      const r = await call("POST", "/api/markers", { ip: `2001:db8:${RUN.length}:2::${(i + 1).toString(16)}`, body: { market: mV6, t: t0 + 60_000 + i * 1000, label: `Other ${i}`, token: TOKEN } });
      if (r.status === 201) ok++;
      else check(`markers: v6 other /64 ${i}`, false, r);
    }
    check("markers: another /64 has its own 10", ok === 20, ok);
    expect("markers: the timeline's 21st new marker today is 429 timeline_busy", await call("POST", "/api/markers", { ip: "192.0.2.77", body: { market: mV6, t: t0 + 90_000, label: "One more", token: TOKEN } }), 429, "timeline_busy");
    const other = await call("POST", "/api/markers", { ip: "192.0.2.77", body: { market: mA, t: t0 + 91_000, label: "Elsewhere", token: TOKEN } });
    check("markers: ...while another timeline still takes one from that visitor", other.status === 201, other);
    const list = await call("GET", `/api/markers?market=${mFull}`);
    check("markers: the list holds 60, ordered by t", list.body?.markers?.length === 60 && list.body.markers.every((m, i, a) => i === 0 || a[i - 1].t <= m.t));
  }
  // ---------- watch: one watchlist for the whole site ----------
  {
    const s = encodeURIComponent(stories.a);
    const g = await call("GET", `/api/watch?story=${s}`);
    check("watch: starts off the watchlist, with no count", g.body?.listed === true && g.body?.watching === false && !("count" in (g.body ?? {})), g.body);
    check("watch: GET is no-store", g.headers.get("cache-control") === "no-store");
    const w = await call("POST", "/api/watch", { body: { story: stories.a, token: TOKEN } });
    check("watch: on the watchlist", w.status === 200 && Object.keys(w.body ?? {}).join() === "watching" && w.body.watching === true, w);
    const w2 = await call("POST", "/api/watch", { body: { story: stories.a, token: TOKEN } });
    check("watch: watching twice changes nothing", w2.body?.watching === true, w2.body);
    const seen = await call("GET", `/api/watch?story=${s}`, { visitor: "b" });
    check("watch: every visitor sees it on the watchlist", seen.body?.watching === true && !("count" in (seen.body ?? {})), seen.body);
    const other = await call("POST", "/api/watch", { visitor: "b", body: { story: stories.a, token: TOKEN } });
    check("watch: another visitor's watch changes nothing", other.status === 200 && other.body?.watching === true, other);
    const un = await call("DELETE", `/api/watch?story=${s}`, { visitor: "b" });
    check("watch: anyone takes it off, for everyone", un.status === 200 && Object.keys(un.body ?? {}).join() === "watching" && un.body.watching === false, un);
    check("watch: ...so the first visitor sees it off too", (await call("GET", `/api/watch?story=${s}`)).body?.watching === false);
    const un2 = await call("DELETE", `/api/watch?story=${s}`);
    check("watch: stopping twice changes nothing", un2.status === 200 && un2.body?.watching === false, un2.body);
    const back = await call("POST", "/api/watch", { body: { story: stories.a, token: TOKEN } });
    check("watch: back on (job/work and the overview read it below)", back.body?.watching === true, back.body);
    expect("watch: unknown_story", await call("POST", "/api/watch", { body: { story: `gone ${RUN}`, token: TOKEN } }), 404, "unknown_story");
    expect("watch: an empty token is 403 bot_check", await call("POST", "/api/watch", { body: { story: stories.b, token: "" } }), 403, "bot_check");
    expect("watch: a 201-character story is 400", await call("GET", `/api/watch?story=${"s".repeat(201)}`), 400, "bad_request");
    const unlisted = await call("GET", `/api/watch?story=${encodeURIComponent(`gone ${RUN}`)}`);
    check("watch: an unlisted story", unlisted.body?.listed === false && unlisted.body?.watching === false, unlisted.body);

    // 30 watch changes a day per visitor; repeated watches and unwatches in between spend nothing
    const L = encodeURIComponent(stories.limit);
    let changes = 0;
    for (let i = 0; i < 15; i++) {
      const a = await call("POST", "/api/watch", { visitor: "wl", body: { story: stories.limit, token: TOKEN } });
      const a2 = await call("POST", "/api/watch", { visitor: "wl", body: { story: stories.limit, token: TOKEN } });
      const b = await call("DELETE", `/api/watch?story=${L}`, { visitor: "wl" });
      const b2 = await call("DELETE", `/api/watch?story=${L}`, { visitor: "wl" });
      if ([a, a2, b, b2].every((r) => r.status === 200)) changes += 2;
      else check(`watch: cycle ${i}`, false, [a, a2, b, b2].map((r) => r.status));
    }
    check("watch: 30 changes fit (no-ops spend nothing)", changes === 30, changes);
    expect("watch: the 31st change is 429 limit", await call("POST", "/api/watch", { visitor: "wl", body: { story: stories.limit, token: TOKEN } }), 429, "limit");
  }

  // ---------- "Add to main page": added at once, no review ----------
  const c1 = CLOSED[0].condition_id;
  {
    const g = await call("GET", `/api/request?cid=${c1}`);
    check("add: none yet", g.body?.listed === true && g.body?.request === null, g.body);
    const r = await call("POST", "/api/request", { body: { condition_id: c1.toUpperCase().replace("0X", "0x"), token: TOKEN, question: "Forged?" } });
    check("add: recorded at once as approved, the status the job adds (201)", r.status === 201 && r.body?.request?.status === "approved" && r.body.request.decided_at > 0 && r.body.request.name === null, r);
    check("add: the public shape only", Object.keys(r.body?.request ?? {}).sort().join() === "decided_at,done_at,name,status", r.body);
    const again = await call("POST", "/api/request", { visitor: "b", body: { condition_id: c1, token: TOKEN } });
    check("add: adding again is 200 with the same row", again.status === 200 && again.body?.request?.status === "approved", again);
    expect("add: not_listed", await call("POST", "/api/request", { body: { condition_id: cid(0xdead), token: TOKEN } }), 404, "not_listed");
    expect("add: on_main (slugs compared normalised)", await call("POST", "/api/request", { body: { condition_id: CLOSED[1].condition_id, token: TOKEN } }), 409, "on_main");
    expect("add: a bad condition id is 400", await call("POST", "/api/request", { body: { condition_id: "0x12", token: TOKEN } }), 400, "bad_request");
    expect("add: a bad cid on GET is 400", await call("GET", "/api/request?cid=nope"), 400, "bad_request");
    expect("add: an empty token is 403 bot_check", await call("POST", "/api/request", { body: { condition_id: CLOSED[2].condition_id, token: "" } }), 403, "bot_check");

    const ov = await call("GET", "/api/job/status", { auth: JOB });
    const row = ov.body?.requests?.find((x) => x.condition_id === c1);
    check("add: copied from the catalog, never the browser", row?.question === CLOSED[0].question && row?.slug === CLOSED[0].slug && row?.winner === "Yes" && row?.day === "2026-11-12" && row?.status === "approved", row);
    const work = await call("GET", "/api/job/work", { auth: JOB });
    check("add: job/work lists it at once, with no page name (the job makes one from the slug)", work.body?.requests?.some((x) => x.condition_id === c1 && x.status === "approved" && x.name === null), work.body?.requests);
    expect("add: there is no approve or decline step", await call("POST", `/api/admin/requests/${c1}`, { auth: JOB, body: { action: "approve", name: "page-name" } }), 404, "not_found");

    const fail = await call("POST", "/api/job/done", { auth: JOB, body: { requests: [{ condition_id: c1, status: "failed", name: null, note: "too many trades to add automatically" }] } });
    expect("add: the job reports a failure (204)", fail, 204);
    const pub = await call("GET", `/api/request?cid=${c1}`);
    check("add: failed is recorded", pub.body?.request?.status === "failed" && pub.body.request.done_at > 0, pub.body);
    check("add: ...with its reason", (await call("GET", "/api/job/status", { auth: JOB })).body?.requests?.find((x) => x.condition_id === c1)?.note === "too many trades to add automatically");
    check("add: a failed one is not handed to the job again", !(await call("GET", "/api/job/work", { auth: JOB })).body?.requests?.some((x) => x.condition_id === c1));
    const retry = await call("POST", "/api/request", { visitor: "retry", body: { condition_id: c1, token: TOKEN } });
    check("add: a visitor can add a failed one again: approved once more (201)", retry.status === 201 && retry.body?.request?.status === "approved" && retry.body.request.done_at === null && retry.body.request.decided_at >= pub.body?.request?.done_at, retry.body);
    check("add: ...its old reason gone, and the job has it again", (await call("GET", "/api/job/status", { auth: JOB })).body?.requests?.find((x) => x.condition_id === c1)?.note === null && (await call("GET", "/api/job/work", { auth: JOB })).body?.requests?.some((x) => x.condition_id === c1));
    const usedRetry = await call("GET", "/api/request?cid=" + c1);
    check("add: ...and it reads as on its way", usedRetry.body?.request?.status === "approved", usedRetry.body);
    await call("POST", "/api/job/done", { auth: JOB, body: { requests: [{ condition_id: c1, status: "failed", name: null, note: "too many trades to add automatically" }] } });

    const c3 = CLOSED[3].condition_id;
    const name = `page-${RUN}`;
    check("add: another market", (await call("POST", "/api/request", { visitor: "c", body: { condition_id: c3, token: TOKEN } })).status === 201);
    await call("POST", "/api/job/done", { auth: JOB, body: { requests: [{ condition_id: c3, status: "added", name, note: null }] } });
    let added = await call("GET", `/api/request?cid=${c3}`);
    check("add: added, under the page name the job gave it", added.body?.request?.status === "added" && added.body.request.name === name, added.body);
    const listed = await call("GET", "/api/job/work", { auth: JOB });
    check(
      "add: job/work lists it as added (condition id and day only: the closed list keeps its pick), not to add",
      listed.body?.added?.some((x) => x.condition_id === c3 && x.day === CLOSED[3].day && Object.keys(x).sort().join() === "condition_id,day") &&
        !listed.body?.requests?.some((x) => x.condition_id === c3) &&
        !listed.body?.added?.some((x) => x.condition_id === c1),
      listed.body?.added,
    );
    await call("POST", "/api/job/done", { auth: JOB, body: { requests: [{ condition_id: c3, status: "failed", name: "other-name", note: "late" }] } });
    added = await call("GET", `/api/request?cid=${c3}`);
    check("add: the job can't change one that is no longer approved", added.body?.request?.status === "added" && added.body.request.name === name, added.body);
    const visitorAgain = await call("POST", "/api/request", { visitor: "d", body: { condition_id: c3, token: TOKEN } });
    check("add: adding again never changes it", visitorAgain.status === 200 && visitorAgain.body?.request?.status === "added", visitorAgain.body);
  }
  {
    // the visitor limit (5 a day), then the site's (50 a day): picks 10.. are fresh
    let used = 3; // c1 (twice) and c3 above
    let next = 10;
    for (let i = 0; i < 5; i++) {
      const r = await call("POST", "/api/request", { visitor: "rq", body: { condition_id: CLOSED[next++].condition_id, token: TOKEN } });
      check(`add: within the visitor's limit (${i + 1} of 5)`, r.status === 201, r);
      used++;
    }
    expect("add: the 6th today is 429 limit", await call("POST", "/api/request", { visitor: "rq", body: { condition_id: CLOSED[next].condition_id, token: TOKEN } }), 429, "limit");
    let v = 0;
    while (used < 50) {
      const r = await call("POST", "/api/request", { visitor: `rs${Math.floor(v++ / 5)}`, body: { condition_id: CLOSED[next++].condition_id, token: TOKEN } });
      if (r.status !== 201) {
        check(`add: fill ${used}`, false, r);
        break;
      }
      used++;
    }
    expect("add: the site's 51st today is 429 busy", await call("POST", "/api/request", { visitor: "fresh", body: { condition_id: CLOSED[next].condition_id, token: TOKEN } }), 429, "busy");
  }

  // ---------- the site's watchlist sends its closed markets to the main page (the daily run) ----------
  {
    const send = (requests) => call("POST", "/api/job/requests", { auth: JOB, body: { requests } });
    const pick = (i, extra = {}) => ({
      condition_id: cid(0xbeef00 + i),
      slug: `watched-${RUN}-${i}`,
      question: `Will watched market ${i} happen?`,
      url: `https://polymarket.com/event/watched-${RUN}-${i}`,
      winner: "Yes",
      closed: "2026-11-14T17:58:37+00:00",
      day: "2026-11-14",
      ...extra,
    });
    const w1 = cid(0xbeef01);
    expect("watched: a wrong token is 401", await call("POST", "/api/job/requests", { visitor: "wj", auth: "wrong", body: { requests: [pick(0)] } }), 401, "unauthorized");
    expect("watched: no token is 401", await call("POST", "/api/job/requests", { visitor: "wj", body: { requests: [pick(0)] } }), 401, "unauthorized");
    const day = { "x-test-day": `${DAY_KEY}-w` }; // the visitor's add below counts apart from the limits filled above
    const mine = await call("POST", "/api/request", { visitor: "w", headers: day, body: { condition_id: CLOSED[4].condition_id, token: TOKEN } });
    check("watched: a visitor's add first", mine.status === 201, mine);
    const sent = await send([pick(1), pick(2, { url: "http://polymarket.com/event/x" }), pick(3, { condition_id: "0x12" }), { ...CLOSED[4], question: "Changed?" }, CLOSED[0], CLOSED[3], pick(1)]);
    const by = Object.fromEntries((sent.body?.requests ?? []).map((r) => [r.condition_id, r]));
    check("watched: 200 with the rows of the markets sent, each once", sent.status === 200 && sent.body?.requests?.length === 4, sent);
    const r1 = by[w1];
    check(
      "watched: a new one is recorded approved, as a visitor's add is, from the job's copy of the pick",
      r1?.status === "approved" && r1.decided_at > 0 && r1.requested_at === r1.decided_at && r1.name === null && r1.done_at === null &&
        r1.question === "Will watched market 1 happen?" && r1.slug === `watched-${RUN}-1` && r1.winner === "Yes" && r1.day === "2026-11-14" &&
        r1.url === `https://polymarket.com/event/watched-${RUN}-1`,
      r1,
    );
    check("watched: entries that don't fit are left out and named", sent.body?.skipped?.length === 2 && !by[cid(0xbeef02)], sent.body?.skipped);
    const v = by[CLOSED[4].condition_id];
    check("watched: a visitor's add there already is left as it is", v?.status === "approved" && v.question === CLOSED[4].question && v.decided_at === mine.body?.request?.decided_at, v);
    check("watched: a failed one stays failed (never added twice)", by[c1]?.status === "failed" && by[c1].note === "too many trades to add automatically", by[c1]);
    check("watched: an added one stays added", by[CLOSED[3].condition_id]?.status === "added" && by[CLOSED[3].condition_id].name === `page-${RUN}`, by[CLOSED[3].condition_id]);
    const work = await call("GET", "/api/job/work", { auth: JOB });
    check("watched: job/work lists it with the markets to add", work.body?.requests?.some((r) => r.condition_id === w1 && r.status === "approved"), work.body?.requests);
    const pub = await call("GET", `/api/request?cid=${w1}`);
    check("watched: its Recently closed card reads it as a visitor's add", pub.body?.request?.status === "approved" && pub.body.request.decided_at === r1?.decided_at, pub.body);
    const again = await send([pick(1, { question: "Changed?" })]);
    check("watched: sent again, it is left as it is", again.body?.requests?.length === 1 && again.body.requests[0].question === r1?.question && again.body.requests[0].decided_at === r1?.decided_at, again.body);
    const none = await send([]);
    check("watched: an empty list is 200 with no rows", none.status === 200 && none.body?.requests?.length === 0, none);
    expect("watched: no list is 400", await call("POST", "/api/job/requests", { auth: JOB, body: {} }), 400, "bad_request");
    expect("watched: over 200 is 400", await send(Array.from({ length: 201 }, (_, i) => pick(100 + i))), 400, "bad_request");
    await call("POST", "/api/job/done", { auth: JOB, body: { requests: [{ condition_id: w1, status: "added", name: `watched-${RUN}`, note: null }] } });
    const after = await call("GET", `/api/request?cid=${w1}`);
    check("watched: the report adds it as any other", after.body?.request?.status === "added" && after.body.request.name === `watched-${RUN}`, after.body);

    // the report takes the stories the job is done with off the watchlist, once Open now no longer lists them
    const open = [
      { story: stories.a, question: "Will A happen?", url: "https://polymarket.com/event/a" },
      { story: stories.b, question: "Will B happen?", url: "https://polymarket.com/event/b" },
      { story: stories.limit, question: "Will L happen?", url: "https://polymarket.com/event/l" },
    ];
    const gone = `watched ${RUN} done`;
    const extra = [gone, PRUNE_WATCH, PRUNE_WATCH_YEAR].map((story) => ({ story, question: `Will ${story} happen?`, url: "https://polymarket.com/event/d" }));
    await call("PUT", "/api/job/catalog", { auth: JOB, body: { open: [...open, ...extra] } });
    for (const x of extra) await call("POST", "/api/watch", { visitor: "w2", headers: day, body: { story: x.story, token: TOKEN } });
    const watching = async (story) => (await call("GET", `/api/watch?story=${encodeURIComponent(story)}`)).body?.watching;
    expect("watched: done with watched stories is 204", await call("POST", "/api/job/done", { auth: JOB, body: { watched: [gone, stories.a] } }), 204);
    check("watched: one Open now still lists stays on the watchlist", (await watching(gone)) === true && (await watching(stories.a)) === true);
    await call("PUT", "/api/job/catalog", { auth: JOB, body: { open } });
    await call("POST", "/api/job/done", { auth: JOB, body: { watched: [gone] } });
    check("watched: ...and leaves it once Open now no longer lists it", (await watching(gone)) === false);
    check("watched: one the report doesn't name stays, listed or not", (await watching(stories.a)) === true && (await watching(PRUNE_WATCH)) === true);
    expect("done: a watched entry that isn't a story is 400", await call("POST", "/api/job/done", { auth: JOB, body: { watched: [5] } }), 400, "bad_request");
  }

  // ---------- sign-in ----------
  {
    for (let i = 1; i <= 10; i++) {
      expect(`auth: wrong job token ${i} is 401`, await call("GET", "/api/job/work", { visitor: "lock", auth: "wrong" }), 401, "unauthorized");
    }
    expect("auth: the 11th try is 429 locked", await call("GET", "/api/job/work", { visitor: "lock", auth: "wrong" }), 429, "locked");
    expect("auth: the right token still gets in from a locked address", await call("GET", "/api/job/work", { visitor: "lock", auth: JOB }), 200);
    expect("auth: ...and a wrong one stays locked", await call("GET", "/api/job/work", { visitor: "lock", auth: "wrong" }), 429, "locked");
    expect("auth: no token is 401", await call("GET", "/api/job/blobs", { visitor: "auth2" }), 401, "unauthorized");
    check("auth: the right token still works for others", (await call("GET", "/api/job/status", { visitor: "auth3", auth: JOB })).status === 200);
  }

  // ---------- the run's report ----------
  {
    expect("done: a javascript: run url is accepted", await call("POST", "/api/job/done", { auth: JOB, body: { run: { mode: "full", status: "success", started_at: 1, finished_at: 2, summary: "x".repeat(900), url: "javascript:alert(1)" } } }), 204);
    const latest = (await call("GET", "/api/job/status", { auth: JOB })).body?.reports?.[0];
    check("done: ...but stored with no link, and the summary cut to 500", latest?.url === null && latest?.summary?.length === 500, latest);
    expect("done: a bad run mode is 400", await call("POST", "/api/job/done", { auth: JOB, body: { run: { mode: "sometimes", status: "success", started_at: 1, finished_at: 2 } } }), 400, "bad_request");
    expect("done: an empty report is 204", await call("POST", "/api/job/done", { auth: JOB, body: {} }), 204);
    const work = await call("GET", "/api/job/work", { auth: JOB });
    check("job/work: requests, the watchlist and the added markets only", Object.keys(work.body ?? {}).sort().join() === "added,now,requests,watched", work.body && Object.keys(work.body));
  }

  if (STEP) await queue();
  else check("queue: the suite needs --step=URL (npm test passes it)", false);
  await blobs();
  await dataFiles();

  // ---------- status, sizes ----------
  {
    const st = await call("GET", "/api/job/status", { auth: JOB });
    check("status: shape", Object.keys(st.body ?? {}).sort().join() === "budget,failed_jobs,lanes,now,reports,requests,runs", st.body && Object.keys(st.body));
    check("status: the lanes, runs and failed jobs", st.body?.lanes?.length === 4 && st.body.runs.length <= 15 && st.body.failed_jobs.length <= 10, st.body?.lanes);
    check("status: budget fields", Object.keys(st.body?.budget ?? {}).sort().join() === "computed_at,cpu_ms_30d,cpu_ms_today,gb_ms_30d,gb_ms_today,invocations_30d,level", st.body?.budget);
    check("status: a run's meta is an object", st.body?.runs?.every((r) => r.meta && typeof r.meta === "object"), st.body?.runs?.[0]);
    expect("status: no token is 401", await call("GET", "/api/job/status", { visitor: "st1" }), 401, "unauthorized");
    expect("job token: a wrong one from anyone is 401", await call("GET", "/api/job/work", { visitor: "jt1", auth: "wrong-job-token" }), 401, "unauthorized");
    expect("job token: a wrong one from the job is 401", await call("GET", "/api/job/work", { visitor: "jt2", auth: "wrong-job-token", headers: { "user-agent": "rigged-outcomes-job" } }), 401, "unauthorized");

    expect("size: a visitor body over 4 KB is 413", await call("POST", "/api/markers", { raw: JSON.stringify({ market: mA, t: t0, label: "x", token: "y".repeat(5000) }), headers: { "content-type": "application/json" } }), 413, "too_large");
    expect("size: a job body over 1 MB is 413", await call("PUT", "/api/job/catalog", { auth: JOB, raw: JSON.stringify({ open: [], pad: "z".repeat(1_100_000) }), headers: { "content-type": "application/json" } }), 413, "too_large");
    expect("size: a body that isn't JSON is 400", await call("POST", "/api/markers", { raw: "{not json", headers: { "content-type": "application/json" } }), 400, "bad_request");
    expect("body: one sent as text/plain (a CORS simple request) is 415", await call("POST", "/api/markers", { raw: JSON.stringify({ market: mA, t: t0, label: "Plain", token: TOKEN }), headers: { "content-type": "text/plain" } }), 415, "bad_request");
    expect("body: one with no content type is 415", await call("POST", "/api/watch", { raw: JSON.stringify({ story: stories.a, token: TOKEN }) }), 415, "bad_request");
  }
}

// ---------- the queue, the cron and the wake-ups (docs/BACKEND.md) ----------

const iso = (s) => Date.parse(s) / 1000;
const setClock = async (t) => (await call("POST", "/api/dev/clock", { auth: JOB, body: { now: t } })).body?.now;
const tick = async () => (await call("POST", "/api/dev/tick", { auth: JOB })).body;
const claim = (lane, left = 270) => call("POST", "/api/job/claim", { auth: JOB, body: { lane, left_s: left, call: "0123456789abcdef", spent: { cpu_ms: 5, wall_ms: 50 } } });
const complete = (job, outcome, extra = {}) =>
  call("POST", "/api/job/complete", { auth: JOB, body: { job_id: job?.id, lease_token: job?.lease_token, outcome, usage: { cpu_ms: 100, wall_ms: 1000, units: 1 }, ...extra } });
const runOf = async (id) => (await call("GET", `/api/job/run?id=${encodeURIComponent(id)}`, { auth: JOB })).body;
const backend = async () => (await call("GET", "/api/job/status", { auth: JOB })).body;
const wakes = async () => (await fetch(`${STEP}/__wakes`)).json();
const wakesFor = async (lane) => (await wakes()).filter((w) => w.lane === lane);
/** The wake-ups of a lane once there are n (they go out after the answer), or what there are after 5 s. */
async function wokeAtLeast(lane, n) {
  for (let i = 0; i < 50 && (await wakesFor(lane)).length < n; i++) await sleep(100);
  return wakesFor(lane);
}
/** A wake-up's header checked as the step function does: v1, the lane, a fresh ts, and the HMAC of "v1|ts|lane|nonce". */
function signed(w, lane) {
  const [scheme, ts, l, nonce, sig] = (w?.header ?? "").split(",");
  const want = createHmac("sha256", WAKE_SECRET).update(`${scheme}|${ts}|${l}|${nonce}`).digest("hex");
  return scheme === "v1" && l === lane && /^[0-9a-f]{16}$/.test(nonce) && Math.abs(now() - Number(ts)) <= 60 && sig === want;
}

async function queue() {
  expect("dev: the clock takes the job token", await call("POST", "/api/dev/clock", { visitor: "dv", body: { now: 1 } }), 401, "unauthorized");

  // the suite's adds woke the add lane at once, twice, then backed off (no claim answered them)
  const adds = await wakesFor("add");
  check("wake: a visitor's adds wake the add lane at once, then back off", adds.length >= 2 && adds.length <= 3, adds.length);
  check("wake: signed v1,<ts>,<lane>,<nonce>,<sig> with WAKE_SECRET", signed(adds[0], "add"), adds[0]);
  check("wake: a POST to /api/step with the Worker's user agent and no body", adds[0]?.method === "POST" && adds[0].path === "/api/step" && adds[0].agent === "rigged-outcomes-api" && adds[0].body === "", adds[0]);
  check("lanes: the add lane's wake-ups are unanswered", (await backend())?.lanes?.find((l) => l.lane === "add")?.unanswered >= 2);

  // 03:59 Toronto (08:59 UTC in winter): no daily run yet; the day before, which no tick made a run for, is missed
  await setClock(iso("2026-12-01T08:59:00Z"));
  let r = await tick();
  check("tick: 03:59 Toronto makes no daily run", Array.isArray(r?.created) && !r.created.includes("daily:2026-12-01"), r);
  check("tick: ...and records the day before as missed", r?.created?.includes("daily:2026-11-30"), r);
  check("tick: a missed day never runs", (await runOf("daily:2026-11-30"))?.run?.state === "missed");
  const stopped = async () => ((await call("GET", "/api/job/status", { auth: JOB })).body?.requests ?? []).filter((x) => x.note === "stopped part way").length;
  check("watchdog: the suite's adds, a day past their deadline, ended with their runs", (await stopped()) === 0);

  // 04:00 Toronto: the daily run, its 15 jobs, its deadline at 09:30, and the main lane woken
  await setClock(iso("2026-12-01T09:00:00Z"));
  r = await tick();
  check("tick: 04:00 Toronto makes the daily run", r?.created?.includes("daily:2026-12-01"), r);
  check("watchdog: ...and the next tick fails their requests, 5 a tick", (await stopped()) === 5);
  const d1 = await runOf("daily:2026-12-01");
  check("daily: 15 jobs, all queued", d1?.jobs?.length === 15 && d1.jobs.every((j) => j.state === "queued"), d1?.jobs?.map((j) => j.state));
  check("daily: its deadline is 09:30 Toronto", d1?.run?.deadline === iso("2026-12-01T14:30:00Z") && d1.run.state === "queued", d1?.run);
  check("tick: wakes the main lane only", JSON.stringify(r?.woke) === '["main"]', r?.woke);
  check("wake: the main lane's wake-up reaches the step function, signed", signed((await wokeAtLeast("main", 1))[0], "main"));
  check("tick: again at once wakes nothing (back-off)", (await tick())?.woke?.length === 0);
  check("lanes: main has one wake-up unanswered", (await backend())?.lanes?.find((l) => l.lane === "main")?.unanswered === 1);

  // claiming
  expect("claim: a lane that isn't one is 400", await claim("gpu"), 400, "bad_request");
  expect("claim: a call with less time than any job needs gets nothing (204)", await claim("main", 40), 204);
  expect("claim: a call's last report (left_s 0) gets nothing (204)", await claim("main", 0), 204);
  expect("claim: left_s below 0 is 400", await claim("main", -1), 400, "bad_request");
  let c = await claim("main");
  const begin = c.body?.job;
  check(
    "claim: daily.begin first, under a lease",
    c.status === 200 && begin?.kind === "daily.begin" && begin.id === "daily:2026-12-01|daily.begin|0" && begin.run_kind === "daily" && begin.run_slot === "2026-12-01" && /^[0-9a-f]{32}$/.test(begin.lease_token) && begin.attempt === 0 && Array.isArray(begin.missing),
    c.body,
  );
  check("claim: the lease is left_s plus 30 s", begin?.lease_until === c.body?.now + 300 && begin.run_deadline === iso("2026-12-01T14:30:00Z"), [begin?.lease_until, c.body?.now]);
  check("claim: the budget comes with it", JSON.stringify(c.body?.budget) === JSON.stringify({ level: "ok", add_whales_left: 800, open_searches: 300, open_reads: 30, open_out: 60 }), c.body?.budget);
  check("claim: the run is running", (await runOf("daily:2026-12-01"))?.run?.state === "running");
  check("lanes: a claim answers the lane's wake-ups", (await backend())?.lanes?.find((l) => l.lane === "main")?.unanswered === 0);
  expect("claim: one lease a lane (204)", await claim("main"), 204);
  expect("claim: a job behind its barrier isn't handed out (204)", await claim("model"), 204);

  // checkpoint
  const cp = (job, body) => call("POST", "/api/job/checkpoint", { auth: JOB, body: { job_id: job?.id, lease_token: job?.lease_token, ...body } });
  const search = `smoke q ${RUN}|7d`; // (test/run.mjs's --prune phase back-dates a search named "smoke q ...")
  expect("checkpoint: results, a search and the cursor (204)", await cp(begin, { cursor: { done: 1 }, results: [{ key: "daily:work", value: { watched: 2 } }], news: [{ key: search, body: [{ title: "Acme agrees deal" }] }] }), 204);
  expect("checkpoint: another token is 409 lost", await cp({ ...begin, lease_token: hex(32, "0") }, { results: [] }), 409, "lost");
  expect("checkpoint: a value over 200 KB is 413", await cp(begin, { results: [{ key: "big", value: "x".repeat(205_000) }] }), 413, "too_large");
  expect("checkpoint: a search that isn't a list is 400", await cp(begin, { news: [{ key: "k", body: "x" }] }), 400, "bad_request");

  // complete
  const done1 = await complete(begin, "done", { results: [{ key: "daily:note", value: "plain text" }], run_meta: { left: 4, problems: ["one"] }, continuing: true });
  check("complete: done is 200", done1.status === 200 && done1.body?.state === "done" && done1.body.repeated === false, done1);
  const again = await complete(begin, "done");
  check("complete: again is 200 repeated, nothing changed", again.status === 200 && again.body?.repeated === true && again.body.state === "done", again);
  expect("complete: another token is 409 lost", await complete({ ...begin, lease_token: hex(32, "1") }, "done"), 409, "lost");
  expect("complete: an unknown outcome is 400", await complete(begin, "maybe"), 400, "bad_request");
  const res = await call("GET", "/api/job/results?run=daily:2026-12-01&prefix=daily:", { auth: JOB });
  check(
    "results: the run's results by key, each value as it was sent",
    JSON.stringify(res.body) === JSON.stringify({ results: [{ key: "daily:note", job_id: begin?.id, value: "plain text" }, { key: "daily:work", job_id: begin?.id, value: { watched: 2 } }], next: null }),
    res.body,
  );
  const page = await call("GET", "/api/job/results?run=daily:2026-12-01&prefix=daily:&after=daily:note", { auth: JOB });
  check("results: after a key", page.body?.results?.length === 1 && page.body.results[0].key === "daily:work", page.body);
  const news = await call("POST", "/api/job/news/get", { auth: JOB, body: { keys: [search, "none"], max_age_s: 10800 } });
  const hit = news.body?.news?.[search];
  check("news: a stored search, with its Toronto day", hit?.body?.[0]?.title === "Acme agrees deal" && hit.day === "2026-12-01" && hit.fetched_at > 0 && !("none" in news.body.news) && news.body.more?.length === 0, news.body);
  const meta = (await runOf("daily:2026-12-01"))?.run?.meta;
  check("complete: run_meta is merged into the run", meta?.left === 4 && meta.problems?.[0] === "one", meta);
  check("ledger: the step is in the backend's runs", (await backend())?.runs?.some((x) => x.id === "daily:2026-12-01" && x.state === "running" && x.meta.left === 4));

  // partial, error, later (each queued again; the oldest due is claimed first)
  c = await claim("main");
  const scan = c.body?.job;
  const at = c.body?.now;
  check("claim: then closed.scan (its barrier, daily.begin, is done)", scan?.kind === "closed.scan" && scan.cursor === null, scan);
  expect("complete: partial with no cursor is 400", await complete(scan, "partial"), 400, "bad_request");
  await setClock(at + 5); // so the partial one is due after the others (the order is by when each is due)
  check("complete: partial queues it again at once", (await complete(scan, "partial", { cursor: { page: 2 } })).body?.state === "queued");
  const walk = (await claim("main")).body?.job;
  check("claim: then open.walk, due before the partial one", walk?.kind === "open.walk", walk);
  check("complete: error queues a retry", (await complete(walk, "error", { error: "boom\nsecond line" })).body?.state === "queued");
  const wc = (await claim("main")).body?.job;
  check("claim: then watch.check", wc?.kind === "watch.check", wc);
  const l1 = await complete(wc, "later", { reason: "polymarket_down", run_after: at + 120 });
  check("complete: later queues it for then", l1.body?.state === "queued", l1);
  const scan2 = (await claim("main", 100)).body?.job;
  check("claim: the partial one again, with its cursor", scan2?.id === scan?.id && scan2.cursor?.page === 2 && scan2.partials === 1, scan2);
  const jobs1 = (await runOf("daily:2026-12-01"))?.jobs ?? [];
  const w = jobs1.find((j) => j.kind === "open.walk");
  const wl = jobs1.find((j) => j.kind === "watch.check");
  check("error: its first line kept, attempt 1", w?.error === "boom" && w.attempt === 1 && w.cpu_ms === 100 && w.wall_ms === 1000, w);
  check("later: counted, not an attempt", wl?.laters === 1 && wl.attempt === 0, wl);

  // a step that stops: its lease expires and the watchdog retries it
  await setClock(at + 600);
  r = await tick();
  check("watchdog: an expired lease goes back to the queue", r?.expired === 1, r);
  const sRow = (await runOf("daily:2026-12-01"))?.jobs?.find((j) => j.kind === "closed.scan");
  check("watchdog: ...as an attempt, with why", sRow?.state === "queued" && sRow.attempt === 1 && sRow.partials === 1 && /lease expired/.test(sRow.error ?? ""), sRow);
  expect("checkpoint: after its lease expired is 409 lost", await cp(scan2, { results: [] }), 409, "lost");
  check("complete: after it is 200 repeated, nothing changed", (await complete(scan2, "done")).body?.repeated === true);

  // the next day's run takes over: the day before's is cancelled with its unfinished jobs
  await setClock(iso("2026-12-02T09:00:00Z"));
  r = await tick();
  check("supersede: the next day's run is made", r?.created?.includes("daily:2026-12-02"), r);
  check("ledger: the expired lease is charged a killed step's use (150 s of CPU, with the safety margin)", (await backend())?.budget?.cpu_ms_30d >= 187_500, (await backend())?.budget);
  const old = await runOf("daily:2026-12-01");
  check("supersede: the day before's run is cancelled, with its unfinished jobs", old?.run?.state === "cancelled" && old.jobs.every((j) => ["done", "cancelled"].includes(j.state)), old?.run);
  check("news: older than max_age_s is left out", !((await call("POST", "/api/job/news/get", { auth: JOB, body: { keys: [search], max_age_s: 3600 } })).body?.news ?? {})[search]);

  // past the deadline the jobs that aren't final go, and the finals publish what is there
  const b2 = (await claim("main")).body?.job;
  await complete(b2, "done", { run_meta: { left: 3 } });
  await setClock(iso("2026-12-02T14:31:00Z"));
  await tick();
  const d2 = await runOf("daily:2026-12-02");
  const kinds = Object.fromEntries((d2?.jobs ?? []).map((j) => [j.kind, j]));
  check("deadline: queued jobs that aren't final are cancelled", kinds["open.read"]?.state === "cancelled" && kinds["open.read"].error === "run deadline" && kinds["watch.check"]?.state === "cancelled", kinds["open.read"]);
  check("deadline: the finals stay queued", ["closed.publish", "open.publish", "daily.end"].every((k) => kinds[k]?.state === "queued"));
  const pub = (await claim("main")).body?.job;
  check("deadline: a final passes its barrier, told what is missing", pub?.kind === "closed.publish" && pub.missing?.some((x) => x.kind === "closed.scan" && x.state === "cancelled" && x.error === "run deadline"), pub);
  await complete(pub, "done");
  await complete((await claim("main")).body?.job, "done");
  const end = (await claim("main")).body?.job;
  check("deadline: daily.end last", end?.kind === "daily.end", end);
  await complete(end, "done");
  check("run: finished partial (some jobs cancelled)", (await runOf("daily:2026-12-02"))?.run?.state === "partial");

  // the catch-up, from 10:00, once, as the day's run left stories unread
  await setClock(iso("2026-12-02T15:01:00Z"));
  r = await tick();
  check("catch-up: made from 10:00, the day's run finished with stories left", r?.created?.includes("catchup:2026-12-02"), r);
  const cu = await runOf("catchup:2026-12-02");
  check("catch-up: its four jobs", cu?.jobs?.map((j) => j.kind).sort().join() === "daily.end,open.out,open.publish,open.read", cu?.jobs);
  const rd = (await claim("model")).body?.job;
  check("catch-up: its jobs say where to read from", rd?.kind === "open.read" && rd.input?.catch_up === true && rd.input.from_run === "daily:2026-12-02", rd);
  check("catch-up: made once", !(await tick())?.created?.includes("catchup:2026-12-02"));
  // an hour past the catch-up's deadline its finals still waiting have failed, and so has the run
  await setClock(iso("2026-12-02T19:02:00Z"));
  await tick();
  const cu2 = await runOf("catchup:2026-12-02");
  const cuBy = Object.fromEntries((cu2?.jobs ?? []).map((j) => [j.kind, j.state]));
  check(
    "grace: finals unfinished an hour past the deadline fail, and so does the run",
    cu2?.run?.state === "failed" && cuBy["open.publish"] === "failed" && cuBy["daily.end"] === "failed" && cuBy["open.out"] === "cancelled" && cuBy["open.read"] === "cancelled",
    cu2,
  );

  // Add to main page: the click makes the add run and wakes the add lane at once
  let t = iso("2026-12-02T19:10:00Z");
  await setClock(t);
  const pick = CLOSED[65];
  const nAdd = (await wakesFor("add")).length;
  const a = await call("POST", "/api/request", { visitor: "q", headers: { "x-test-day": `${DAY_KEY}-q` }, body: { condition_id: pick.condition_id, token: TOKEN } });
  check("add: approved at once (201)", a.status === 201 && a.body?.request?.status === "approved", a);
  const row = (await call("GET", "/api/job/status", { auth: JOB })).body?.requests?.find((x) => x.condition_id === pick.condition_id);
  check("add: its run is on the request", row?.run_id === `add:${pick.condition_id}:${a.body?.request?.decided_at}`, row);
  const ar = await runOf(row?.run_id);
  check("add: the run's graph", ar?.run?.state === "queued" && ar.jobs.map((j) => j.kind).sort().join() === "add.export,add.fetch" && ar.run.deadline === ar.run.created_at + 129600, ar);
  check("add: the add lane is woken at once", (await wokeAtLeast("add", nAdd + 1)).length === nAdd + 1);
  const fetchJob = (await claim("add")).body?.job;
  check("claim: add.fetch", fetchJob?.kind === "add.fetch" && fetchJob.run_kind === "add" && fetchJob.run_id === row?.run_id, fetchJob);
  expect("complete: a child of a kind not in the contract is 400", await complete(fetchJob, "done", { children: [{ kind: "add.nothing", part: "0" }] }), 400, "bad_request");
  const spawned = await complete(fetchJob, "done", { children: [0, 1].map((i) => ({ kind: "add.whales", part: String(i), input: { wallets: [`0x${i}`] } })) });
  check("complete: done, with two whale batches", spawned.body?.state === "done", spawned);
  // a smaller add, clicked after, goes first: its fetch (2 jobs left) before the first one's whale batches (3 left)
  t += 1;
  await setClock(t);
  const pick2 = CLOSED[67];
  await call("POST", "/api/request", { visitor: "q2", headers: { "x-test-day": `${DAY_KEY}-q2` }, body: { condition_id: pick2.condition_id, token: TOKEN } });
  const small = (await claim("add")).body?.job;
  check("claim: a smaller add goes before a larger one's whale batches", small?.kind === "add.fetch" && small.run_id?.startsWith(`add:${pick2.condition_id}:`), small);
  await call("POST", "/api/job/done", { auth: JOB, body: { requests: [{ condition_id: pick2.condition_id, status: "failed", name: null, note: "smoke" }] } });
  await complete(small, "done", { stop_run: true });
  let wh = (await claim("add")).body?.job;
  check("barrier: a whale batch before the export", wh?.kind === "add.whales" && wh.part === "0" && wh.input?.wallets?.[0] === "0x0", wh);
  await complete(wh, "error", { error: "whale 0" });
  const wh1 = (await claim("add")).body?.job;
  check("claim: the other batch while the first waits to retry", wh1?.kind === "add.whales" && wh1.part === "1", wh1);
  await complete(wh1, "done", { results: [{ key: "whale:0x1", value: { score: 1 } }], usage: { cpu_ms: 10, wall_ms: 10, units: 15 } });
  for (let i = 1; i < 6; i++) {
    t += 2000;
    await setClock(t);
    wh = (await claim("add")).body?.job;
    if (wh?.part !== "0") check(`retry: batch 0 again (${i})`, false, wh);
    await complete(wh, "error", { error: `whale ${i}` });
  }
  const exp = (await claim("add")).body?.job;
  check("barrier: the export once every batch is done or failed, told which failed", exp?.kind === "add.export" && exp.missing?.length === 1 && exp.missing[0].kind === "add.whales" && exp.missing[0].part === "0" && exp.missing[0].state === "failed" && exp.missing[0].error === "whale 5", exp);
  await call("POST", "/api/job/done", { auth: JOB, body: { requests: [{ condition_id: pick.condition_id, status: "added", name: `q-${RUN}`, note: null }] } });
  await complete(exp, "done");
  check("add: its run ends partial (a batch failed)", (await runOf(row?.run_id))?.run?.state === "partial");
  check("add: ...and the market is on the main page", (await call("GET", `/api/request?cid=${pick.condition_id}`)).body?.request?.status === "added");
  check("failed jobs: in the status", (await backend())?.failed_jobs?.some((j) => j.kind === "add.whales" && j.error === "whale 5"));

  // a watched market sent to the main page gets its own add run; one too large reports itself and stops its run
  const wp = { ...CLOSED[66], condition_id: cid(0xabc001), slug: `watched-q-${RUN}` };
  const sent = await call("POST", "/api/job/requests", { auth: JOB, body: { requests: [wp] } });
  const wr = sent.body?.requests?.[0];
  check("watched: approved with its own add run", wr?.status === "approved" && wr.run_id?.startsWith(`add:${wp.condition_id}:`), wr);
  check("watched: its run has the add graph", (await runOf(wr?.run_id))?.jobs?.length === 2);
  const wf = (await claim("add")).body?.job;
  check("claim: the watched market's add.fetch", wf?.run_id === wr?.run_id && wf.kind === "add.fetch", wf);
  await call("POST", "/api/job/done", { auth: JOB, body: { requests: [{ condition_id: wp.condition_id, status: "failed", name: null, note: "too large to add" }] } });
  const st = await complete(wf, "done", { stop_run: true, usage: { cpu_ms: 12_000_000, wall_ms: 1000, units: 0 } });
  const wrun = await runOf(wr?.run_id);
  check("stop_run: the run is cancelled, with its export", st.body?.state === "done" && wrun?.run?.state === "cancelled" && wrun.jobs.find((j) => j.kind === "add.export")?.state === "cancelled", wrun);

  // the budget: the step above used 12,000,000 ms of CPU, over 80% of the month's 14,400,000 with the safety
  // margin and 20 minutes in a day, so the level is stop: nothing is claimed or woken, and a due run is skipped
  t += 11 * 60;
  await setClock(t);
  r = await tick();
  const bud = (await backend())?.budget;
  check("budget: a heavy day is stop", bud?.level === "stop" && bud.cpu_ms_today >= 1_200_000 && bud.cpu_ms_30d >= 15_000_000 && bud.invocations_30d > 0, bud);
  check("budget: at stop nothing is woken", r?.woke?.length === 0, r);
  expect("budget: at stop nothing is claimed (204)", await claim("main"), 204);
  await setClock(iso("2026-12-03T09:00:00Z"));
  await tick();
  check("budget: a run due at stop is skipped", (await runOf("daily:2026-12-03"))?.run?.state === "skipped");

  // daylight saving: 04:00 is 08:00 UTC in summer time (and a month on, the budget is ok again)
  await setClock(iso("2027-03-15T07:59:00Z"));
  r = await tick();
  check("dst: 03:59 summer time makes no run", !r?.created?.includes("daily:2027-03-15"), r);
  await setClock(iso("2027-03-15T08:00:00Z"));
  r = await tick();
  check("dst: 04:00 summer time is 08:00 UTC", r?.created?.includes("daily:2027-03-15"), r);
  const dst = await runOf("daily:2027-03-15");
  check("dst: queued, its deadline 09:30 summer time", dst?.run?.state === "queued" && dst.run.deadline === iso("2027-03-15T13:30:00Z"), dst?.run);
  check("budget: a month on it is ok again", (await backend())?.budget?.level === "ok");

  // at low (65% of the month used) the blocked steps of the daily run are cancelled at once, so the open list's
  // searches (capped to the current cards) and its "already out" check still run, and the lists publish
  const heavy = (await claim("main")).body?.job;
  await complete(heavy, "done", { usage: { cpu_ms: 7_700_000, wall_ms: 1000, units: 0 } });
  await setClock(iso("2027-03-16T08:00:00Z"));
  r = await tick();
  check("low: the budget is low the next day", (await backend())?.budget?.level === "low" && r?.created?.includes("daily:2027-03-16"), [(await backend())?.budget, r]);
  const lowRun = Object.fromEntries(((await runOf("daily:2027-03-16"))?.jobs ?? []).map((j) => [j.kind, j]));
  check("low: open.name and open.read are cancelled at once, for the budget", ["open.name", "open.read"].every((k) => lowRun[k]?.state === "cancelled" && lowRun[k].error === "budget"), lowRun);
  check("low: open.news and open.out wait to run", ["open.news", "open.out"].every((k) => lowRun[k]?.state === "queued"));
  for (let i = 0; i < 4; i++) {
    const m = (await claim("main")).body?.job;
    if (!m) break;
    await complete(m, "done");
  }
  const lowNews = await claim("news");
  check("low: open.news is claimed, capped to the current cards' searches", lowNews.body?.job?.kind === "open.news" && lowNews.body?.budget?.level === "low" && lowNews.body.budget.open_searches === 15, lowNews.body);
  await complete(lowNews.body?.job, "done");
  let lowOut = null;
  for (let i = 0; i < 4 && !lowOut; i++) {
    const m = (await claim("model")).body?.job;
    if (!m) break;
    if (m.kind === "open.out") lowOut = m;
    await complete(m, "done");
  }
  check("low: open.out is claimed (the current cards re-checked)", lowOut?.kind === "open.out" && lowOut.missing?.some((x) => x.kind === "open.read" && x.error === "budget"), lowOut);

  expect("dev: the clock put back", await call("POST", "/api/dev/clock", { auth: JOB, body: { now: null } }), 200);
}

/** The files the backend publishes (/api/data/...), from site:* blobs saved as the step function saves them. */
async function dataFiles() {
  const ver = () => crypto.randomUUID().replace(/-/g, "");
  const name = (n) => encodeURIComponent(n).replace(/%3A/g, ":"); // as watchlist/blobs.py quotes it: the "/" as %2F
  async function save(blob, text, partChars = 500_000) {
    const b64 = gzipSync(Buffer.from(text)).toString("base64");
    const parts = [];
    for (let i = 0; i < b64.length; i += partChars) parts.push(b64.slice(i, i + partChars));
    const v = ver();
    for (const [i, p] of parts.entries()) await call("PUT", `/api/job/blobs/${name(blob)}/${v}/${i}`, { auth: JOB, body: { data: p } });
    const c = await call("POST", `/api/job/blobs/${name(blob)}/${v}`, { auth: JOB, body: { parts: parts.length, bytes: text.length, sha256: hex(64, "a"), meta: {} } });
    check(`data: ${blob} saved in ${parts.length} part(s)`, c.status === 200, c);
    return v;
  }
  const get = (path, headers = {}, method = "GET") => fetch(`${BASE}${path}`, { method, headers });

  const none = await get("/api/data/index.json");
  check("data: no index yet is 404 no_file, not cached", none.status === 404 && (await none.json()).error === "no_file" && none.headers.get("cache-control") === "no-store");
  const closed = JSON.stringify({ days: [{ day: "2026-11-19", picks: [{ q: `smoke ${RUN}` }] }] });
  const vc = await save("site:closed.json", closed);
  const g = await get("/api/data/closed.json", { origin: ALLOWED });
  check("data: the list, byte for byte", g.status === 200 && (await g.text()) === closed, g.status);
  check("data: sent gzipped, as JSON", g.headers.get("content-encoding") === "gzip" && g.headers.get("content-type") === "application/json; charset=utf-8", [...g.headers]);
  check("data: its version is the ETag, revalidated every time", g.headers.get("etag") === `"${vc}"` && g.headers.get("cache-control") === "public, max-age=0, must-revalidate");
  check("data: CORS for the site's origin", g.headers.get("access-control-allow-origin") === ALLOWED);
  const n304 = await get("/api/data/closed.json", { "if-none-match": `"${vc}"` });
  check("data: If-None-Match with its version is 304, no body", n304.status === 304 && (await n304.text()) === "" && n304.headers.get("etag") === `"${vc}"`);
  check("data: a weak tag in a list matches too", (await get("/api/data/closed.json", { "if-none-match": `"x", W/"${vc}"` })).status === 304);
  check("data: another version is 200", (await get("/api/data/closed.json", { "if-none-match": `"${ver()}"` })).status === 200);
  const head = await get("/api/data/closed.json", {}, "HEAD");
  check("data: HEAD has the headers and no body", head.status === 200 && head.headers.get("etag") === `"${vc}"` && (await head.text()) === "");

  // a market's page in several parts, streamed as they are; versioned links are immutable
  const page = `m${RUN}`.slice(0, 30);
  let seed = 7;
  const noise = Array.from({ length: 6000 }, () => ((seed = (seed * 48271) % 2147483647) % 1e9).toString(36)).join(",");
  const body = JSON.stringify({ name: page, rows: noise });
  const vp = await save(`site:markets/${page}.json`, body, 20_000);
  const mp = await get(`/api/data/markets/${page}.json?v=${vp}`);
  check("data: a page saved in parts reads back whole", mp.status === 200 && (await mp.text()) === body, mp.status);
  check("data: ?v= the live version is immutable", mp.headers.get("cache-control") === "public, max-age=31536000, immutable", mp.headers.get("cache-control"));
  const stale = await get(`/api/data/markets/${page}.json?v=${ver()}`);
  check("data: an old ?v= is revalidated instead", stale.headers.get("cache-control") === "public, max-age=0, must-revalidate" && (await stale.text()) === body);
  check("data: no ?v= too", (await get(`/api/data/markets/${page}.json`)).headers.get("cache-control") === "public, max-age=0, must-revalidate");
  expect("data: an unknown page is 404 no_file", await call("GET", `/api/data/markets/none-${RUN}.json`), 404, "no_file");
  expect("data: a page name that isn't one is 404 no_file", await call("GET", "/api/data/markets/Bad_Name.json"), 404, "no_file");
  expect("data: a file that isn't published is 404", await call("GET", "/api/data/state.json"), 404, "not_found");
  expect("data: only GET and HEAD", await call("POST", "/api/data/closed.json", { body: {} }), 405, "bad_method");
  const index = JSON.stringify({ markets: [{ name: page, v: vp }], updated_at: "2026-11-19T04:12:00-05:00" });
  await save("site:index.json", index);
  check("data: the added markets' index", (await (await get("/api/data/index.json")).text()) === index);
  expect("data: the page removed", await call("DELETE", `/api/job/blobs/${name(`site:markets/${page}.json`)}`, { auth: JOB }), 204);
  expect("data: ...is 404 after", await call("GET", `/api/data/markets/${page}.json`), 404, "no_file");
}

/** The job's data (watchlist/blobs.py): parts, commits, the pointer, and every refusal. Only market:<name> blobs this run names. */
async function blobs() {
  const ver = () => crypto.randomUUID().replace(/-/g, "");
  const b64 = (n, seed) => Buffer.from(Array.from({ length: n }, (_, i) => (i * 31 + seed) % 256)).toString("base64");
  const name = `market:t${RUN}`;
  const path = (...p) => `/api/job/blobs/${[encodeURIComponent(name).replace("%3A", ":"), ...p].join("/")}`;
  const put = (v, part, data, n = name) => call("PUT", `/api/job/blobs/${n}/${v}/${part}`, { auth: JOB, body: { data } });
  const commit = (v, body, n = name) => call("POST", `/api/job/blobs/${n}/${v}`, { auth: JOB, body: { bytes: 1234, sha256: hex(64, "a"), meta: { by: "smoke" }, ...body } });
  const get = (p) => call("GET", p, { auth: JOB });

  // sign-in: every route is the job's
  const v0 = ver();
  const routes = [
    ["GET", "/api/job/blobs"],
    ["GET", path()],
    ["GET", path("versions")],
    ["DELETE", path()],
    ["PUT", path(v0, 0)],
    ["GET", path(v0, 0)],
    ["POST", path(v0)],
  ];
  for (const [i, [method, p]] of routes.entries()) {
    const body = method === "PUT" ? { data: "AAAA" } : method === "POST" ? { parts: 1, bytes: 0, sha256: hex(64, "a"), meta: {} } : undefined;
    expect(`blobs: ${method} ${p.replace(name, "<name>")} with no token is 401`, await call(method, p, { visitor: `blob${i}`, body }), 401, "unauthorized");
    expect(`blobs: ${method} ${p.replace(name, "<name>")} with a wrong token is 401`, await call(method, p, { visitor: `blob${i}`, auth: "wrong", body }), 401, "unauthorized");
  }

  // three parts, then the first commit (expect: null, the blob must not exist yet)
  const v1 = ver();
  const p1 = [b64(300, 1), b64(300, 2), `${b64(100, 3)}`];
  for (const [i, data] of p1.entries()) expect(`blobs: part ${i} is 204`, await put(v1, i, data), 204);
  expect("blobs: no head before the commit is 404 no_blob", await get(path()), 404, "no_blob");
  const c1 = await commit(v1, { parts: 3, expect: null, meta: { entry: { name: `t${RUN}` }, added_at: 1 } });
  const headKeys = "bytes,meta,name,parts,sha256,updated_at,version";
  check(
    "blobs: the first commit is 200 with its head",
    c1.status === 200 && Object.keys(c1.body ?? {}).sort().join() === headKeys && c1.body.name === name && c1.body.version === v1 && c1.body.parts === 3 && c1.body.bytes === 1234 && c1.body.sha256 === hex(64, "a") && c1.body.meta?.entry?.name === `t${RUN}` && c1.body.updated_at > 0,
    c1,
  );
  const h1 = await get(path());
  check("blobs: GET head", h1.status === 200 && JSON.stringify(h1.body) === JSON.stringify(c1.body), h1);
  for (const [i, data] of p1.entries()) {
    const g = await get(path(v1, i));
    check(`blobs: part ${i} reads back as sent`, g.status === 200 && g.body?.data === data, { status: g.status, length: g.body?.data?.length });
  }
  expect("blobs: a part that isn't there is 404 no_part", await get(path(v1, 3)), 404, "no_part");
  const listed = await get("/api/job/blobs?prefix=market:");
  check("blobs: listed under prefix market:", listed.status === 200 && listed.body?.blobs?.some((b) => b.name === name && b.version === v1 && b.meta?.added_at === 1), listed.body);
  check("blobs: the list holds markets only, by name", listed.body?.blobs?.every((b, i, a) => b.name.startsWith("market:") && (i === 0 || a[i - 1].name < b.name)), listed.body?.blobs?.map((b) => b.name));
  const all = await get("/api/job/blobs?prefix=");
  check("blobs: an empty prefix lists them all", all.status === 200 && all.body?.blobs?.some((b) => b.name === name), all.body);
  check("blobs: no prefix lists them all too", (await get("/api/job/blobs")).body?.blobs?.length === all.body?.blobs?.length);
  expect("blobs: a prefix with capitals is 400", await get("/api/job/blobs?prefix=Market"), 400, "bad_request");
  expect("blobs: a prefix with a space is 400", await get("/api/job/blobs?prefix=market%20"), 400, "bad_request");
  expect("blobs: a prefix over 60 characters is 400", await get(`/api/job/blobs?prefix=${"a".repeat(61)}`), 400, "bad_request");
  check("blobs: a prefix with a slash lists the pages", (await get("/api/job/blobs?prefix=site:markets/")).status === 200);
  expect("blobs: an unknown blob is 404 no_blob", await get(`/api/job/blobs/market:none-${RUN}`), 404, "no_blob");
  const known = await get("/api/job/blobs/state");
  check("blobs: state, the site's files and scratch are names", [200, 404].includes(known.status) && (await Promise.all(["site:index.json", "site:open.json", `site:markets%2Ft${RUN}.json`, `scratch:${"0".repeat(16)}:stories.json`].map((n) => get(`/api/job/blobs/${n}`)))).every((r) => r.status === 404 || r.status === 200), known);
  for (const gone of ["cache:news", "list:open", "list:closed"]) expect(`blobs: ${gone} is no longer a name (400)`, await get(`/api/job/blobs/${gone}`), 400, "bad_request");

  // commit refusals
  const v2 = ver();
  expect("blobs: v2 part 0", await put(v2, 0, b64(30, 4)), 204);
  expect("blobs: v2 part 2", await put(v2, 2, b64(30, 6)), 204);
  expect("blobs: a missing part is 409 incomplete", await commit(v2, { parts: 3, expect: v1 }), 409, "incomplete");
  expect("blobs: the wrong parts count is 409 incomplete", await commit(v2, { parts: 2, expect: v1 }), 409, "incomplete");
  expect("blobs: v2 part 1", await put(v2, 1, b64(30, 5)), 204);
  expect("blobs: v2 part 1 again (a retry) is 204", await put(v2, 1, b64(30, 5)), 204);
  expect("blobs: expect null when it exists is 409 moved", await commit(v2, { parts: 3, expect: null }), 409, "moved");
  expect("blobs: the wrong expect is 409 moved", await commit(v2, { parts: 3, expect: ver() }), 409, "moved");
  const again = await commit(v1, { parts: 3, expect: null });
  check("blobs: committing the live version again (a lost answer) is 200, before the expect check", again.status === 200 && again.body?.version === v1 && again.body.updated_at === c1.body.updated_at, again);
  expect("blobs: ...with another sha256 is 409 live", await commit(v1, { parts: 3, sha256: hex(64, "b") }), 409, "live");
  expect("blobs: a part of the live version is 409 live", await put(v1, 0, b64(30, 9)), 409, "live");
  expect("blobs: ...even a new part number", await put(v1, 5, b64(30, 9)), 409, "live");
  check("blobs: the live version is unchanged", (await get(path(v1, 0))).body?.data === p1[0] && (await get(path())).body?.version === v1);

  // replacing it: the pointer moves and the old parts go
  const c2 = await commit(v2, { parts: 3, expect: v1, sha256: hex(64, "c"), bytes: 90, meta: { by: "smoke", n: 2 } });
  check("blobs: replacing with expect = the live version is 200", c2.status === 200 && c2.body?.version === v2 && c2.body.parts === 3 && c2.body.sha256 === hex(64, "c") && c2.body.bytes === 90 && c2.body.meta?.n === 2, c2);
  check("blobs: the head is the new version", (await get(path())).body?.version === v2);
  for (let i = 0; i < 3; i++) expect(`blobs: the old version's part ${i} is gone (404 no_part)`, await get(path(v1, i)), 404, "no_part");
  check("blobs: the new parts read back", (await get(path(v2, 1))).body?.data === b64(30, 5));
  expect("blobs: committing the old version again is 409 incomplete (its parts are gone)", await commit(v1, { parts: 3 }), 409, "incomplete");
  const v3 = ver();
  await put(v3, 0, b64(10, 7));
  const c3 = await commit(v3, { parts: 1 });
  check("blobs: a commit with no expect replaces whatever is live", c3.status === 200 && c3.body?.version === v3, c3);
  expect("blobs: ...and the one before's parts are gone", await get(path(v2, 0)), 404, "no_part");

  // validation
  for (const [why, bad] of [["capitals", "market:A"], ["unknown kind", "foo"], ["a path", encodeURIComponent("market:../x")], ["one character", "market:a"], ["list:other", "list:other"], ["a site file that isn't one", "site:other.json"], ["a page with a path", encodeURIComponent("site:markets/a/b.json")], ["scratch with a short run", "scratch:abc:x"]]) {
    expect(`blobs: a bad name (${why}) on GET is 400`, await get(`/api/job/blobs/${bad}`), 400, "bad_request");
    expect(`blobs: a bad name (${why}) on PUT is 400`, await put(ver(), 0, "AAAA", bad), 400, "bad_request");
  }
  expect("blobs: a version that isn't 32 hex is 400", await put("abc", 0, "AAAA"), 400, "bad_request");
  expect("blobs: an uppercase version is 400", await put(ver().toUpperCase(), 0, "AAAA"), 400, "bad_request");
  expect("blobs: part -1 is 400", await put(ver(), -1, "AAAA"), 400, "bad_request");
  expect("blobs: part 400 is 400", await put(ver(), 400, "AAAA"), 400, "bad_request");
  expect("blobs: part 01 is 400", await put(ver(), "01", "AAAA"), 400, "bad_request");
  expect("blobs: part 399 is fine", await put(ver(), 399, "AAAA"), 204);
  expect("blobs: data that isn't base64 is 400", await put(ver(), 0, "not base64!"), 400, "bad_request");
  expect("blobs: empty data is 400", await put(ver(), 0, ""), 400, "bad_request");
  expect("blobs: data that isn't text is 400", await put(ver(), 0, 5), 400, "bad_request");
  expect("blobs: a GET part with a bad version is 400", await get(path("xyz", 0)), 400, "bad_request");
  const vc = ver();
  await put(vc, 0, "AAAA");
  for (const [why, body] of [
    ["parts 0", { parts: 0 }],
    ["parts 401", { parts: 401 }],
    ["parts not a number", { parts: "1" }],
    ["bytes -1", { parts: 1, bytes: -1 }],
    ["a short sha256", { parts: 1, sha256: "abc" }],
    ["an uppercase sha256", { parts: 1, sha256: hex(64, "A") }],
    ["meta a list", { parts: 1, meta: [] }],
    ["meta over 16,384 characters", { parts: 1, meta: { x: "m".repeat(16_400) } }],
    ["expect not a version", { parts: 1, expect: "v1" }],
  ]) {
    expect(`blobs: commit with ${why} is 400`, await commit(vc, body), 400, "bad_request");
  }

  // a big part: 1,000,000 characters fit (a body just under 1 MiB), one more doesn't, and over 1 MiB is 413
  const big = `market:b${RUN}`;
  const vb = ver();
  const million = "A".repeat(1_000_000);
  expect("blobs: a part of 1,000,000 characters is 204", await put(vb, 0, million, big), 204);
  const cb = await commit(vb, { parts: 1, expect: null }, big);
  check("blobs: ...commits", cb.status === 200 && cb.body?.version === vb, cb);
  const gb = await get(`/api/job/blobs/${big}/${vb}/0`);
  check("blobs: ...and reads back whole", gb.status === 200 && gb.body?.data === million, { status: gb.status, length: gb.body?.data?.length });
  expect("blobs: 1,000,001 characters is 400", await put(ver(), 0, `${million}A`, big), 400, "bad_request");
  expect("blobs: a body over 1 MiB is 413", await put(ver(), 0, "A".repeat(1_100_000), big), 413, "too_large");

  // deleting: the pointer and its parts go
  expect("blobs: DELETE is 204", await call("DELETE", `/api/job/blobs/${big}`, { auth: JOB }), 204);
  expect("blobs: ...then its head is 404 no_blob", await get(`/api/job/blobs/${big}`), 404, "no_blob");
  expect("blobs: ...and its part is 404 no_part", await get(`/api/job/blobs/${big}/${vb}/0`), 404, "no_part");
  expect("blobs: DELETE again is 404 no_blob", await call("DELETE", `/api/job/blobs/${big}`, { auth: JOB }), 404, "no_blob");
  check("blobs: the other blob is untouched", (await get(path())).body?.version === v3 && (await get(path(v3, 0))).body?.data === b64(10, 7));
  const fresh = await commit(ver(), { parts: 1, expect: null }, big);
  expect("blobs: a deleted name has no parts to commit (409 incomplete)", fresh, 409, "incomplete");

  // the run's report prunes uploads never committed, but only once a day old
  const vo = ver();
  await put(vo, 0, "QUJD");
  expect("blobs: done is 204", await call("POST", "/api/job/done", { auth: JOB, body: {} }), 204);
  check("blobs: an upload from today survives the prune", (await get(path(vo, 0))).body?.data === "QUJD");
  check("blobs: the live version survives it", (await get(path(v3, 0))).body?.data === b64(10, 7));
  // ...and what --prune checks once test/run.mjs has back-dated every part: a live market and an orphan beside it
  await put(PRUNE_LIVE_VERSION, 0, "TElWRQ==", PRUNE_MARKET);
  await commit(PRUNE_LIVE_VERSION, { parts: 1 }, PRUNE_MARKET);
  expect("blobs: the orphan --prune looks for", await put(PRUNE_ORPHAN, 0, "T1JQSEFO", PRUNE_MARKET), 204);

  // six saves at once over the same version: one goes live, the others are 409 moved and delete nothing
  const race = `market:r${RUN}`;
  const vr = ver();
  await put(vr, 0, b64(10, 1), race);
  await commit(vr, { parts: 1, expect: null }, race);
  const rivals = Array.from({ length: 6 }, () => ver());
  for (const [i, v] of rivals.entries()) await put(v, 0, b64(10, 20 + i), race);
  const answers = await Promise.all(rivals.map((v) => commit(v, { parts: 1, expect: vr }, race)));
  const won = answers.filter((a) => a.status === 200);
  check("race: of six saves at once, exactly one goes live", won.length === 1 && answers.every((a) => a.status === 200 || (a.status === 409 && a.body?.error === "moved")), answers.map((a) => [a.status, a.body?.error]));
  const winner = won[0]?.body?.version;
  check("race: the head is the winner", (await get(`/api/job/blobs/${race}`)).body?.version === winner);
  for (const [i, v] of rivals.entries()) {
    check(`race: rival ${i}'s part is still there (the winner's, or one left for the prune)`, (await get(`/api/job/blobs/${race}/${v}/0`)).body?.data === b64(10, 20 + i));
  }
  expect("race: the replaced version's part is gone", await get(`/api/job/blobs/${race}/${vr}/0`), 404, "no_part");

  // the state's and the lists' replaced versions: the last 3 kept, restorable, never rewritten or deleted. Only on a
  // fresh database (npm test), so a run against the dev Worker never touches its lists
  const L = "site:open.json";
  if ((await get(`/api/job/blobs/${L}`)).status === 404) {
    const lp = (...p) => `/api/job/blobs/${L}/${p.join("/")}`;
    const vs = [];
    for (let i = 0; i < 5; i++) {
      const v = ver();
      await put(v, 0, b64(20, 40 + i), L);
      expect(`history: save ${i}`, await commit(v, { parts: 1, sha256: hex(64, String(i)), meta: { n: i } }, L), 200);
      vs.push(v);
    }
    const kept = await get(lp("versions"));
    check("history: the last 3 replaced are kept, newest first", kept.status === 200 && JSON.stringify(kept.body?.versions?.map((x) => x.version)) === JSON.stringify([vs[3], vs[2], vs[1]]), kept.body);
    const k0 = kept.body?.versions?.[0];
    check("history: each a head with when it was replaced", k0?.meta?.n === 3 && k0.sha256 === hex(64, "3") && k0.parts === 1 && k0.replaced_at >= k0.updated_at && k0.updated_at > 0, k0);
    check("history: a kept version's part reads back", (await get(lp(vs[2], 0))).body?.data === b64(20, 42));
    expect("history: the 4th one back is gone with its parts", await get(lp(vs[0], 0)), 404, "no_part");
    expect("history: a kept version's part can't be rewritten", await put(vs[1], 0, b64(20, 9), L), 409, "live");
    expect("history: restoring over a version that isn't live is 409 moved", await commit(vs[2], { parts: 1, sha256: hex(64, "2"), expect: vs[3] }, L), 409, "moved");
    const r = await commit(vs[2], { parts: 1, sha256: hex(64, "2"), meta: { n: 2 }, expect: vs[4] }, L);
    check("history: committing a kept version again restores it", r.status === 200 && r.body?.version === vs[2] && r.body.meta?.n === 2, r);
    const after = await get(lp("versions"));
    check("history: ...and the one it replaced is kept in its place", JSON.stringify(after.body?.versions?.map((x) => x.version)) === JSON.stringify([vs[4], vs[3], vs[1]]), after.body);
    check("history: the restored version reads back", (await get(lp(vs[2], 0))).body?.data === b64(20, 42) && (await get(lp(vs[4], 0))).body?.data === b64(20, 44));
    expect("history: the lists can't be deleted", await call("DELETE", `/api/job/blobs/${L}`, { auth: JOB }), 400, "bad_request");
    expect("history: nor the index", await call("DELETE", "/api/job/blobs/site:index.json", { auth: JOB }), 400, "bad_request");
    expect("history: nor the state", await call("DELETE", "/api/job/blobs/state", { auth: JOB }), 400, "bad_request");
    check("history: the list is still there", (await get(`/api/job/blobs/${L}`)).body?.version === vs[2]);
  }
  check("history: a market keeps none", (await get(path("versions"))).body?.versions?.length === 0);

  // taking a market off the main page marks the request that added it failed, so the site stops linking to it
  const cOff = CLOSED[3].condition_id;
  const page = `market:page-${RUN}`;
  const vp = ver();
  await put(vp, 0, "UEFHRQ==", page);
  expect("off: the added market's blob", await commit(vp, { parts: 1, expect: null, meta: { condition_id: cOff } }, page), 200);
  check("off: its request is added", (await call("GET", `/api/request?cid=${cOff}`)).body?.request?.status === "added");
  expect("off: DELETE is 204", await call("DELETE", `/api/job/blobs/${page}`, { auth: JOB }), 204);
  const off = await call("GET", `/api/request?cid=${cOff}`);
  check("off: visitors see it as removed now, under the same name", off.body?.request?.status === "removed" && off.body.request.name === `page-${RUN}`, off.body);
  check("off: ...with why", (await call("GET", "/api/job/status", { auth: JOB })).body?.requests?.find((x) => x.condition_id === cOff)?.note === "taken off the main page");
  const offAgain = await call("POST", "/api/request", { visitor: "off", body: { condition_id: cOff, token: TOKEN } });
  check("off: sending it again changes nothing (200, still removed)", offAgain.status === 200 && offAgain.body?.request?.status === "removed", offAgain);
  check("off: ...and the job doesn't get it again", !(await call("GET", "/api/job/work", { auth: JOB })).body?.requests?.some((x) => x.condition_id === cOff));

  // an added market's page, under site:markets/ (its "/" sent as %2F): taking it off marks its request too
  const cPage = CLOSED[6].condition_id;
  await call("POST", "/api/request", { visitor: "pg", headers: { "x-test-day": `${DAY_KEY}-pg` }, body: { condition_id: cPage, token: TOKEN } });
  await call("POST", "/api/job/done", { auth: JOB, body: { requests: [{ condition_id: cPage, status: "added", name: `pg-${RUN}`, note: null }] } });
  const sitePage = `site:markets%2Fpg-${RUN}.json`;
  const vg = ver();
  expect("page: a part of an added market's page", await put(vg, 0, "UEFHRQ==", sitePage), 204);
  expect("page: a site file's part that isn't a multiple of 4 is 400", await put(ver(), 0, "UEFHRQ", sitePage), 400, "bad_request");
  const pc = await commit(vg, { parts: 1, expect: null }, sitePage);
  check("page: committed under its full name", pc.status === 200 && pc.body?.name === `site:markets/pg-${RUN}.json`, pc);
  check("page: listed under site:markets/", (await get("/api/job/blobs?prefix=site:markets/")).body?.blobs?.some((b) => b.name === `site:markets/pg-${RUN}.json`));
  expect("page: DELETE is 204", await call("DELETE", `/api/job/blobs/${sitePage}`, { auth: JOB }), 204);
  check("page: ...and its request reads as removed", (await call("GET", `/api/request?cid=${cPage}`)).body?.request?.status === "removed");

  // added markets have a cap, so the database keeps room for the rest: 600 parts in all, records and pages
  const huge = `market:h${RUN}`;
  const wide = `market:w${RUN}`;
  const heads = async (prefix) => (await get(`/api/job/blobs?prefix=${prefix}`)).body?.blobs ?? [];
  const used = [...(await heads("market:")), ...(await heads("site:markets/"))].reduce((n, b) => n + b.parts, 0);
  const vw = ver();
  await Promise.all(Array.from({ length: 400 }, (_, i) => put(vw, i, "AAAA", wide)));
  expect("full: a market of 400 parts fits", await commit(vw, { parts: 400, expect: null }, wide), 200);
  const vh = ver();
  await Promise.all(Array.from({ length: 201 - used }, (_, i) => put(vh, i, "AAAA", huge)));
  expect("full: a new market past 600 parts in all is 409 full", await commit(vh, { parts: 201 - used, expect: null }, huge), 409, "full");
  expect("full: ...and nothing is saved", await get(`/api/job/blobs/${huge}`), 404, "no_blob");
  const vs1 = ver();
  await put(vs1, 0, "AAAA", huge);
  expect("full: a small one still fits", await commit(vs1, { parts: 1, expect: null }, huge), 200);

  // this run's markets go, so the cap never fills on a database the suite runs on again
  for (const n of [name, race, huge, wide]) expect(`blobs: ${n.replace(RUN, "<run>")} removed`, await call("DELETE", `/api/job/blobs/${n}`, { auth: JOB }), 204);
}

/**
 * The cron's daily prune, once test/run.mjs has back-dated the state: a tick after 03:30 Toronto on a day not yet
 * pruned removes a part left uncommitted for over a day (never a live or a kept version's), a search over three
 * days old and a watched story a year old that Open now no longer lists; and the watchdog gives an approved add
 * with no run (from before the queue) its run.
 */
async function pruneOnly() {
  const get = (p) => call("GET", p, { auth: JOB });
  const at = `/api/job/blobs/${PRUNE_MARKET}`;
  check("prune: the orphan is there before the prune", (await get(`${at}/${PRUNE_ORPHAN}/0`)).body?.data === "T1JQSEFO");
  const kept = (await get("/api/job/blobs/site:open.json/versions")).body?.versions ?? [];
  const searched = async () => (await call("POST", "/api/job/news/get", { auth: JOB, body: { keys: ["smoke q old|7d"], max_age_s: 1e10 } })).body?.news?.["smoke q old|7d"];
  check("prune: the old search is there before the prune", !!(await searched()));
  // tomorrow 05:00 in Toronto, a day the prune hasn't done
  const tomorrow = new Date(Date.now() + 86_400_000).toLocaleDateString("en-CA", { timeZone: "America/Toronto" });
  await call("POST", "/api/dev/clock", { auth: JOB, body: { now: Date.parse(`${tomorrow}T10:00:00Z`) / 1000 } }); // 05:00 EST, 06:00 EDT
  const r = (await call("POST", "/api/dev/tick", { auth: JOB })).body;
  check("prune: the tick ran", Array.isArray(r?.created), r);
  check("prune: a search over 3 days old is gone", !(await searched()));
  expect("prune: a two-day-old part of no version is gone", await get(`${at}/${PRUNE_ORPHAN}/0`), 404, "no_part");
  check("prune: a two-day-old live part stays", (await get(`${at}/${PRUNE_LIVE_VERSION}/0`)).body?.data === "TElWRQ==");
  check("prune: the kept versions' parts stay", kept.length === 3 && (await Promise.all(kept.map((k) => get(`/api/job/blobs/site:open.json/${k.version}/0`)))).every((x) => x.status === 200), kept.map((k) => k.version));
  const any = (await call("GET", "/api/job/status", { auth: JOB })).body;
  const left = any?.requests?.find((x) => x.condition_id === `0x${"e".repeat(64)}`);
  check("watchdog: an approved add with no run gets one", left?.status === "approved" && left.run_id === `add:0x${"e".repeat(64)}:1`, left);
  const lr = (await call("GET", `/api/job/run?id=${encodeURIComponent(left?.run_id ?? "")}`, { auth: JOB })).body;
  check("watchdog: ...with the add graph, queued", lr?.run?.state === "queued" && lr.jobs.length === 2, lr);
  // a watched market waits for its result however long: a story off Open now stays on the watchlist for a year
  const watching = async (story) => (await call("GET", `/api/watch?story=${encodeURIComponent(story)}`)).body?.watching;
  check("prune: a watched story off Open now for 40 days stays", (await watching(PRUNE_WATCH)) === true);
  check("prune: ...and goes after a year", (await watching(PRUNE_WATCH_YEAR)) === false);
  await call("POST", "/api/dev/clock", { auth: JOB, body: { now: null } });
}

try {
  const h = await fetch(`${BASE}/api/health`).catch(() => null);
  if (!h) {
    console.log(`No Worker answers at ${BASE} (start it with npm run dev).`);
    process.exit(1);
  }
  if (BOT_FAIL) await botFailOnly();
  else if (PRODUCTION) await productionOnly();
  else if (PRUNE) await pruneOnly();
  else await suite();
} catch (e) {
  failed.push(`threw: ${e?.stack ?? e}`);
  console.log(`FAIL the test threw: ${e?.stack ?? e}`);
}
console.log(`${passed}/${passed + failed.length} checks passed${MODE}`);
process.exit(failed.length ? 1 : 0);
