// Smoke test for the API against a running local Worker (wrangler dev with .dev.vars: DEV=on, the
// always-passing Turnstile test secret, the dev tokens). It replaces the catalog wholesale, so run it on a
// throwaway state (npm test does: a fresh --persist-to directory each time). Each run counts its daily
// limits under its own X-Test-Day and visitors under X-Test-Visitor, so it can run again on the same state.
//
//   node test/smoke.mjs http://localhost:8788                  the whole suite
//   node test/smoke.mjs http://localhost:8788 --bot-fail       only: a failing bot check is 403 bot_check (a Worker
//                                                              started with --var TURNSTILE_SECRET:2x0000000000000000000000000000000AA)
//   node test/smoke.mjs http://localhost:8788 --production     only: production settings (--var DEV:off --var TURNSTILE_STRICT:on):
//                                                              the dummy token fails the hostname check, test headers are ignored
//   node test/smoke.mjs http://localhost:8788 --dispatch=URL   only: "Apply decisions now" against test/run.mjs's stand-in for
//                                                              GitHub at URL (--var GITHUB_DISPATCH_TOKEN:… --var GITHUB_API:URL)
//   node test/smoke.mjs http://localhost:8788 --prune          only: the run's report prunes the upload the whole suite left
//                                                              unfinished, once test/run.mjs has back-dated every part 2 days
// Exits 1 on any failed check, 2 when Cloudflare's siteverify could not be reached ("network": run again).

const BASE = (process.argv[2] ?? "http://localhost:8788").replace(/\/$/, "");
const BOT_FAIL = process.argv.includes("--bot-fail");
const PRODUCTION = process.argv.includes("--production");
const DISPATCH = process.argv.find((a) => a.startsWith("--dispatch="))?.slice("--dispatch=".length);
const PRUNE = process.argv.includes("--prune");
const MODE = BOT_FAIL ? " (bot-check failure)" : PRODUCTION ? " (production settings)" : DISPATCH ? " (dispatch)" : PRUNE ? " (prune)" : "";
const RUN = Date.now().toString(36) + Math.random().toString(36).slice(2, 6);
const DAY_KEY = `t-${RUN}`;
const OWNER = "dev-owner-token";
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
  for (let i = 1; i <= 10; i++) await call("GET", "/api/admin/overview", { visitor: `p${i}`, auth: "wrong" });
  expect("production: X-Test-Visitor is ignored (ten wrong tokens from one address lock it)", await call("GET", "/api/admin/overview", { visitor: "other", auth: "wrong" }), 429, "locked");
  expect("production: the right token still gets in from that address", await call("GET", "/api/admin/overview", { visitor: "other", auth: OWNER }), 200);
  expect("production: X-Test-IP is ignored too", await call("GET", "/api/admin/overview", { ip: "2001:db8:ffff::1", auth: "wrong" }), 429, "locked");
}

async function dispatchOnly() {
  const fake = (path) => fetch(`${DISPATCH}${path}`).then((r) => r.json());
  check("dispatch: overview says it can dispatch", (await call("GET", "/api/admin/overview", { auth: OWNER })).body?.dispatch === true);
  await fake("/__answer?status=500");
  expect("dispatch: GitHub refusing is 502 github", await call("POST", "/api/admin/apply", { auth: OWNER }), 502, "github");
  await fake("/__answer?status=204");
  const ok = await call("POST", "/api/admin/apply", { auth: OWNER });
  check("dispatch: a refused try doesn't start the 2-minute wait; this one is 202", ok.status === 202 && ok.body?.dispatched === true, ok);
  const last = await fake("/__last");
  check("dispatch: the workflow's dispatch URL", last.path === "/repos/FalconXYX/Rigged-Outcomes/actions/workflows/watchlist.yml/dispatches" && last.method === "POST", last);
  check("dispatch: body", JSON.stringify(last.body) === JSON.stringify({ ref: "main", inputs: { mode: "apply" } }), last.body);
  check(
    "dispatch: headers",
    last.headers.authorization === "Bearer fake-pat" &&
      last.headers.accept === "application/vnd.github+json" &&
      last.headers["x-github-api-version"] === "2022-11-28" &&
      last.headers["user-agent"] === "rigged-outcomes-api",
    last.headers,
  );
  expect("dispatch: again within 2 minutes is 429 limit", await call("POST", "/api/admin/apply", { auth: OWNER }), 429, "limit");
  check("dispatch: GitHub was asked twice in all", (await fake("/__count")).count === 2);
}

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
    check("health", h.status === 200 && h.body?.ok === true && h.body?.writes === true, h);
    expect("unknown endpoint is 404", await call("GET", "/api/nothing"), 404, "not_found");
    expect("wrong method is 405", await call("PATCH", "/api/markers"), 405, "bad_method");
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
    check("markers: listed market starts empty", empty.body?.listed === true && empty.body?.writes === true && empty.body?.markers?.length === 0, empty.body);
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

    const victim = list.body.markers[0].id;
    expect("markers: owner delete is 204", await call("DELETE", `/api/admin/markers/${victim}`, { auth: OWNER }), 204);
    expect("markers: owner delete of an unknown id is 404", await call("DELETE", `/api/admin/markers/${crypto.randomUUID()}`, { auth: OWNER }), 404, "not_found");
    const after = await call("GET", `/api/markers?market=${mA}`);
    check("markers: the owner-deleted marker is gone", !after.body?.markers?.some((m) => m.id === victim));
    const ov = await call("GET", "/api/admin/overview", { auth: OWNER });
    const rows = ov.body?.markers ?? [];
    check("markers: overview shows who deleted", rows.find((m) => m.id === victim)?.deleted_by === "owner" && rows.find((m) => m.id === id)?.deleted_by === "visitor", rows.slice(0, 3));
    check("markers: overview rows carry no visitor or undo hash", rows.every((m) => !("visitor" in m) && !("undo_sha" in m)));
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
    check("watch: starts off the watchlist, with no count", g.body?.listed === true && g.body?.writes === true && g.body?.watching === false && !("count" in (g.body ?? {})), g.body);
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
    check("add: none yet", g.body?.listed === true && g.body?.writes === true && g.body?.request === null, g.body);
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

    const ov = await call("GET", "/api/admin/overview", { auth: OWNER });
    const row = ov.body?.requests?.find((x) => x.condition_id === c1);
    check("add: copied from the catalog, never the browser", row?.question === CLOSED[0].question && row?.slug === CLOSED[0].slug && row?.winner === "Yes" && row?.day === "2026-11-12" && row?.status === "approved", row);
    const work = await call("GET", "/api/job/work", { auth: JOB });
    check("add: job/work lists it at once, with no page name (the job makes one from the slug)", work.body?.requests?.some((x) => x.condition_id === c1 && x.status === "approved" && x.name === null), work.body?.requests);
    expect("add: there is no approve or decline step", await call("POST", `/api/admin/requests/${c1}`, { auth: OWNER, body: { action: "approve", name: "page-name" } }), 404, "not_found");

    const fail = await call("POST", "/api/job/done", { auth: JOB, body: { requests: [{ condition_id: c1, status: "failed", name: null, note: "too many trades to add automatically" }] } });
    expect("add: the job reports a failure (204)", fail, 204);
    const pub = await call("GET", `/api/request?cid=${c1}`);
    check("add: failed is recorded", pub.body?.request?.status === "failed" && pub.body.request.done_at > 0, pub.body);
    check("add: ...with its reason for the review page", (await call("GET", "/api/admin/overview", { auth: OWNER })).body?.requests?.find((x) => x.condition_id === c1)?.note === "too many trades to add automatically");
    check("add: a failed one is not handed to the job again", !(await call("GET", "/api/job/work", { auth: JOB })).body?.requests?.some((x) => x.condition_id === c1));

    const c3 = CLOSED[3].condition_id;
    const name = `page-${RUN}`;
    check("add: another market", (await call("POST", "/api/request", { visitor: "c", body: { condition_id: c3, token: TOKEN } })).status === 201);
    await call("POST", "/api/job/done", { auth: JOB, body: { requests: [{ condition_id: c3, status: "added", name, note: null }] } });
    let added = await call("GET", `/api/request?cid=${c3}`);
    check("add: added, under the page name the job gave it", added.body?.request?.status === "added" && added.body.request.name === name, added.body);
    await call("POST", "/api/job/done", { auth: JOB, body: { requests: [{ condition_id: c3, status: "failed", name: "other-name", note: "late" }] } });
    added = await call("GET", `/api/request?cid=${c3}`);
    check("add: the job can't change one that is no longer approved", added.body?.request?.status === "added" && added.body.request.name === name, added.body);
    const visitorAgain = await call("POST", "/api/request", { visitor: "d", body: { condition_id: c3, token: TOKEN } });
    check("add: adding again never changes it", visitorAgain.status === 200 && visitorAgain.body?.request?.status === "added", visitorAgain.body);
  }
  {
    // the visitor limit (5 a day), then the site's (50 a day): picks 10.. are fresh
    let used = 2; // c1 and c3 above
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

  // ---------- pause ----------
  {
    const keep = await call("POST", "/api/markers", { visitor: "p", body: { market: mA, t: t0 + 77_000, label: "Before the pause", token: TOKEN } });
    await call("POST", "/api/watch", { visitor: "p", body: { story: stories.b, token: TOKEN } });
    const off = await call("PUT", "/api/admin/settings", { auth: OWNER, body: { writes: "off" } });
    check("pause: switched off", off.body?.settings?.writes === "off", off.body);
    expect("pause: adding a marker is 503 paused", await call("POST", "/api/markers", { visitor: "p", body: { market: mA, t: t0 + 78_000, label: "During", token: TOKEN } }), 503, "paused");
    expect("pause: watching is 503 paused", await call("POST", "/api/watch", { visitor: "p", body: { story: stories.a, token: TOKEN } }), 503, "paused");
    expect("pause: adding to the main page is 503 paused", await call("POST", "/api/request", { visitor: "p", body: { condition_id: CLOSED[5].condition_id, token: TOKEN } }), 503, "paused");
    const m = await call("GET", `/api/markers?market=${mA}`);
    check("pause: marker list reads writes:false", m.body?.writes === false && m.body?.markers?.length > 0, m.body?.writes);
    check("pause: watch reads writes:false", (await call("GET", `/api/watch?story=${encodeURIComponent(stories.a)}`)).body?.writes === false);
    check("pause: request reads writes:false", (await call("GET", `/api/request?cid=${c1}`)).body?.writes === false);
    check("pause: health reads writes:false", (await call("GET", "/api/health")).body?.writes === false);
    expect("pause: undo still works", await call("DELETE", `/api/markers/${keep.body?.marker?.id}`, { visitor: "p", headers: { "x-undo": keep.body?.undo } }), 204);
    const un = await call("DELETE", `/api/watch?story=${encodeURIComponent(stories.b)}`, { visitor: "p" });
    check("pause: unwatch still works", un.status === 200 && un.body?.watching === false, un);
    check("pause: overview still works", (await call("GET", "/api/admin/overview", { auth: OWNER })).body?.settings?.writes === "off");
    expect("pause: a bad value is 400", await call("PUT", "/api/admin/settings", { auth: OWNER, body: { writes: "maybe" } }), 400, "bad_request");
    const on = await call("PUT", "/api/admin/settings", { auth: OWNER, body: { writes: "on" } });
    check("pause: switched back on", on.body?.settings?.writes === "on" && (await call("GET", "/api/health")).body?.writes === true);
  }

  // ---------- sign-in ----------
  {
    for (let i = 1; i <= 10; i++) {
      expect(`auth: wrong owner token ${i} is 401`, await call("GET", "/api/admin/overview", { visitor: "lock", auth: "wrong" }), 401, "unauthorized");
    }
    expect("auth: the 11th try is 429 locked", await call("GET", "/api/admin/overview", { visitor: "lock", auth: "wrong" }), 429, "locked");
    expect("auth: the right token still gets in from a locked address", await call("GET", "/api/admin/overview", { visitor: "lock", auth: OWNER }), 200);
    expect("auth: ...and a wrong one stays locked", await call("GET", "/api/admin/overview", { visitor: "lock", auth: "wrong" }), 429, "locked");
    expect("auth: the job token on an owner endpoint is 401", await call("GET", "/api/admin/overview", { visitor: "auth2", auth: JOB }), 401, "unauthorized");
    expect("auth: the owner token on a job endpoint is 401", await call("GET", "/api/job/work", { visitor: "auth2", auth: OWNER }), 401, "unauthorized");
    expect("auth: no token is 401", await call("GET", "/api/job/blobs", { visitor: "auth2" }), 401, "unauthorized");
    check("auth: the right token still works for others", (await call("GET", "/api/admin/overview", { visitor: "auth3", auth: OWNER })).status === 200);
  }

  // ---------- the open list's queue and decisions ----------
  const day = "2026-11-13";
  const qStory = `queue ${RUN} one`;
  const qDated = `queue ${RUN} dated`;
  const item = (n, story, place, why, candidates) => ({
    n, story, place, why, kind: "merger_acquisition", group: 1, question: `Will ${story}?`, who_knows: "Both boards.",
    polymarket: `${story}?`, url: "https://polymarket.com/event/q", reveal: null, candidates, model_copy_unmatched: [],
  });
  const k1 = { title: "Tesla and SpaceX agree to merge", source: "Reuters", published: "2026-11-13T08:00:00Z", url: "https://news.example/1", key: "tesla and spacex agree to merge" };
  const k2 = { title: "Talks stall", source: "AP", published: "2026-11-12T08:00:00Z", url: null, key: "talks stall" };
  {
    const put = await call("PUT", "/api/job/queue", {
      auth: JOB,
      body: { day, run_at: "2026-11-13T22:30:20Z", steps_by: "person", items: [item(1, qStory, "new step", "awaiting approval", [k1, k2]), item(2, qDated, "dated", "dated, on the list", [])] },
    });
    expect("queue: put is 204", put, 204);
    const ov = await call("GET", "/api/admin/overview", { auth: OWNER });
    check("queue: overview shows the day", ov.body?.queue?.day === day && ov.body.queue.steps_by === "person" && ov.body.queue.items.length === 2 && ov.body.queue.items[0].candidates[0].key === k1.key, ov.body?.queue);
    expect("queue: over 30 items is 400", await call("PUT", "/api/job/queue", { auth: JOB, body: { day: "2026-11-01", run_at: "x", steps_by: "person", items: Array.from({ length: 31 }, (_, i) => item(i, `s${i}`, "stays", "on the list", [])) } }), 400, "bad_request");
    expect("queue: a candidate with no key is 400", await call("PUT", "/api/job/queue", { auth: JOB, body: { day: "2026-11-01", run_at: "x", steps_by: "person", items: [item(1, "s", "stays", "on the list", [{ title: "t" }])] } }), 400, "bad_request");
    const older = await call("PUT", "/api/job/queue", { auth: JOB, body: { day: "2026-11-12", run_at: "2026-11-12T22:00:00Z", steps_by: "person", items: [item(1, "old story", "stays", "on the list", [])] } });
    expect("queue: an older day is accepted", older, 204);
    check("queue: overview keeps the newest day", (await call("GET", "/api/admin/overview", { auth: OWNER })).body?.queue?.day === day);

    const d = (story, key, decision, extra = {}) => call("PUT", "/api/admin/decisions", { auth: OWNER, body: { day, story, headline_key: key, decision, ...extra } });
    const s1 = await d(qStory, k1.key, "step", { headline: { title: "FORGED" } });
    check("decide: saved", s1.status === 200 && s1.body?.decision?.decision === "step" && s1.body.decision.applied_at === null, s1);
    check("decide: the headline comes from the queue", s1.body?.decision?.headline?.title === k1.title && s1.body.decision.headline.source === "Reuters" && !("key" in s1.body.decision.headline), s1.body?.decision?.headline);
    expect("decide: an unknown key is 404", await d(qStory, "no such headline", "step"), 404, "not_found");
    expect("decide: an unknown story is 404", await d(`nothing ${RUN}`, k1.key, "step"), 404, "not_found");
    expect("decide: card approved on a headline is 400", await d(qStory, k2.key, "card approved"), 400, "bad_request");
    expect("decide: a card on an item that isn't dated is 400", await d(qStory, "card", "card approved"), 400, "bad_request");
    expect("decide: a bad decision is 400", await d(qStory, k1.key, "maybe"), 400, "bad_request");
    const card = await d(qDated, "card", "card approved");
    check("decide: a dated card as a whole", card.status === 200 && card.body?.decision?.headline === null, card);
    const del = (story, key) => call("DELETE", `/api/admin/decisions?story=${encodeURIComponent(story)}&key=${encodeURIComponent(key)}`, { auth: OWNER });
    expect("decide: taking back a pending decision is 204", await del(qDated, "card"), 204);
    expect("decide: taking it back again is 404", await del(qDated, "card"), 404, "not_found");
    const wd = (await call("GET", "/api/job/work", { auth: JOB })).body?.decisions?.find((x) => x.story === qDated && x.headline_key === "card");
    check("decide: a taken-back decision reaches the job as withdrawn (a run may already have saved it)", wd?.decision === "withdrawn" && wd.applied_at === null && wd.decided_at > card.body?.decision?.decided_at, wd);
    check("decide: ...and the review page no longer lists it", !(await call("GET", "/api/admin/overview", { auth: OWNER })).body?.decisions?.some((x) => x.story === qDated));
    const reCard = await d(qDated, "card", "card approved");
    check("decide: deciding a withdrawn one again makes it a decision again", reCard.status === 200 && reCard.body?.decision?.decision === "card approved", reCard.body);
    expect("decide: ...and taking that back is 204 again", await del(qDated, "card"), 204);
    const wd2 = (await call("GET", "/api/job/work", { auth: JOB })).body?.decisions?.find((x) => x.story === qDated && x.headline_key === "card");
    const s2 = await d(qStory, k2.key, "not a step");
    const again = await d(qStory, k1.key, "step");
    check("decide: deciding again moves decided_at forward", again.body?.decision?.decided_at > s1.body?.decision?.decided_at, [s1.body?.decision?.decided_at, again.body?.decision?.decided_at]);

    const work = await call("GET", "/api/job/work", { auth: JOB });
    const pending = (work.body?.decisions ?? []).filter((x) => x.story === qStory);
    check("job/work: pending decisions", pending.length === 2 && pending.every((x) => x.applied_at === null && x.day === day), pending);
    check("job/work: the watchlist's stories, no counts", work.body?.watched?.some((w) => w.story === stories.a) && work.body.watched.every((w) => Object.keys(w).join() === "story"), work.body?.watched);
    check("job/work: shape", typeof work.body?.now === "number" && Array.isArray(work.body?.requests));

    const done = await call("POST", "/api/job/done", {
      auth: JOB,
      body: {
        applied: [
          { story: qStory, headline_key: k1.key, decided_at: again.body.decision.decided_at },
          { story: qStory, headline_key: k2.key, decided_at: s2.body.decision.decided_at - 1 },
          { story: qDated, headline_key: "card", decided_at: wd2?.decided_at },
        ],
        run: { mode: "local", status: "success", started_at: now() - 60, finished_at: now(), summary: `smoke ${RUN}`, url: "https://github.com/FalconXYX/Rigged-Outcomes/actions/runs/1" },
      },
    });
    expect("done: 204", done, 204);
    const ov2 = await call("GET", "/api/admin/overview", { auth: OWNER });
    const byKey = Object.fromEntries((ov2.body?.decisions ?? []).filter((x) => x.story === qStory).map((x) => [x.headline_key, x]));
    check("done: a decision with its decided_at is applied", byKey[k1.key]?.applied_at > 0, byKey[k1.key]);
    check("done: a decided_at mismatch stays pending", byKey[k2.key]?.applied_at === null, byKey[k2.key]);
    check("done: the run is recorded", ov2.body?.runs?.[0]?.summary === `smoke ${RUN}` && ov2.body.runs[0].mode === "local", ov2.body?.runs?.[0]);
    const after = (await call("GET", "/api/job/work", { auth: JOB })).body?.decisions ?? [];
    check("done: a withdrawal the job reported is gone", !after.some((x) => x.story === qDated), after.filter((x) => x.story === qDated));
    expect("decide: taking back an applied decision is 409 applied", await del(qStory, k1.key), 409, "applied");
    const redo = await d(qStory, k1.key, "not a step");
    check("decide: deciding an applied one again makes it pending", redo.body?.decision?.applied_at === null && redo.body.decision.decision === "not a step", redo.body);

    // the run that applies a decision rebuilds the queue without its headline; the decision can still be changed
    const bad = { ...k2, url: "javascript:alert(1)" };
    await call("PUT", "/api/job/queue", { auth: JOB, body: { day, run_at: "2026-11-13T23:00:00Z", steps_by: "person", items: [{ ...item(1, qStory, "new step", "awaiting approval", [bad]), url: "data:text/html,x" }] } });
    const rebuilt = (await call("GET", "/api/admin/overview", { auth: OWNER })).body?.queue?.items?.[0];
    check("queue: links that aren't https:// are stored as null", rebuilt?.url === null && rebuilt?.candidates?.[0]?.url === null && rebuilt?.candidates?.[0]?.title === k2.title, rebuilt);
    await call("POST", "/api/job/done", { auth: JOB, body: { applied: [{ story: qStory, headline_key: k1.key, decided_at: redo.body.decision.decided_at }] } });
    const flip = await d(qStory, k1.key, "step");
    check("decide: an applied decision whose headline left the queue can still be changed", flip.status === 200 && flip.body?.decision?.decision === "step" && flip.body.decision.applied_at === null && flip.body.decision.headline?.title === k1.title, flip);
    const flipOld = await call("PUT", "/api/admin/decisions", { auth: OWNER, body: { day: "2026-11-01", story: qStory, headline_key: k1.key, decision: "not a step" } });
    check("decide: ...it keeps its own day and headline", flipOld.status === 200 && flipOld.body?.decision?.day === day && flipOld.body.decision.headline?.source === "Reuters", flipOld.body);
    expect("decide: a headline never decided and not in the queue is still 404", await d(qStory, "never seen", "step"), 404, "not_found");
    expect("done: a javascript: run url is accepted", await call("POST", "/api/job/done", { auth: JOB, body: { run: { mode: "full", status: "success", started_at: 1, finished_at: 2, summary: "x".repeat(900), url: "javascript:alert(1)" } } }), 204);
    const latest = (await call("GET", "/api/admin/overview", { auth: OWNER })).body?.runs?.[0];
    check("done: ...but stored with no link, and the summary cut to 500", latest?.url === null && latest?.summary?.length === 500, latest);
    expect("done: a bad run mode is 400", await call("POST", "/api/job/done", { auth: JOB, body: { run: { mode: "sometimes", status: "success", started_at: 1, finished_at: 2 } } }), 400, "bad_request");
    expect("done: an empty report is 204", await call("POST", "/api/job/done", { auth: JOB, body: {} }), 204);
  }

  await blobs();

  // ---------- apply, sizes ----------
  {
    expect("apply: no dispatch token is 501", await call("POST", "/api/admin/apply", { auth: OWNER }), 501, "no_dispatch");
    const ov = await call("GET", "/api/admin/overview", { auth: OWNER });
    check("overview: dispatch is false", ov.body?.dispatch === false);
    check("overview: the watchlist's rows", ov.body?.watched?.some((w) => w.story === stories.a && w.since > 0 && w.question === "Will A happen?" && !("count" in w)), ov.body?.watched);
    const ovKeys = Object.keys(ov.body ?? {}).sort().join();
    check("overview: shape", ovKeys === "decisions,dispatch,markers,now,queue,requests,runs,settings,watched", ovKeys);

    expect("size: a visitor body over 4 KB is 413", await call("POST", "/api/markers", { raw: JSON.stringify({ market: mA, t: t0, label: "x", token: "y".repeat(5000) }), headers: { "content-type": "application/json" } }), 413, "too_large");
    expect("size: a job body over 1 MB is 413", await call("PUT", "/api/job/catalog", { auth: JOB, raw: JSON.stringify({ open: [], pad: "z".repeat(1_100_000) }), headers: { "content-type": "application/json" } }), 413, "too_large");
    expect("size: a body that isn't JSON is 400", await call("POST", "/api/markers", { raw: "{not json", headers: { "content-type": "application/json" } }), 400, "bad_request");
    expect("body: one sent as text/plain (a CORS simple request) is 415", await call("POST", "/api/markers", { raw: JSON.stringify({ market: mA, t: t0, label: "Plain", token: TOKEN }), headers: { "content-type": "text/plain" } }), 415, "bad_request");
    expect("body: one with no content type is 415", await call("POST", "/api/watch", { raw: JSON.stringify({ story: stories.a, token: TOKEN }) }), 415, "bad_request");
  }
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
    expect(`blobs: ${method} ${p.replace(name, "<name>")} with the owner token is 401`, await call(method, p, { visitor: `blob${i}`, auth: OWNER, body }), 401, "unauthorized");
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
  expect("blobs: a prefix with a slash is 400", await get("/api/job/blobs?prefix=market%2F"), 400, "bad_request");
  expect("blobs: a prefix over 40 characters is 400", await get(`/api/job/blobs?prefix=${"a".repeat(41)}`), 400, "bad_request");
  expect("blobs: an unknown blob is 404 no_blob", await get(`/api/job/blobs/market:none-${RUN}`), 404, "no_blob");
  const known = await get("/api/job/blobs/cache:news");
  check("blobs: state, cache:news and the lists are names", [200, 404].includes(known.status) && [200, 404].includes((await get("/api/job/blobs/list:open")).status), known);

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
  for (const [why, bad] of [["capitals", "market:A"], ["unknown kind", "foo"], ["a path", encodeURIComponent("market:../x")], ["one character", "market:a"], ["list:other", "list:other"]]) {
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
  const L = "list:open";
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
  check("off: the request is failed now, under the same name", off.body?.request?.status === "failed" && off.body.request.name === `page-${RUN}`, off.body);
  check("off: ...with why, for the review page", (await call("GET", "/api/admin/overview", { auth: OWNER })).body?.requests?.find((x) => x.condition_id === cOff)?.note === "taken off the main page");
  check("off: sending it again changes nothing", (await call("POST", "/api/request", { visitor: "off", body: { condition_id: cOff, token: TOKEN } })).body?.request?.status === "failed");

  // added markets have a cap, so the database keeps room for the rest: 300 parts in all
  const huge = `market:h${RUN}`;
  const vh = ver();
  for (let i = 0; i < 301; i++) await put(vh, i, "AAAA", huge);
  expect("full: a new market past 300 parts in all is 409 full", await commit(vh, { parts: 301, expect: null }, huge), 409, "full");
  expect("full: ...and nothing is saved", await get(`/api/job/blobs/${huge}`), 404, "no_blob");
  const vs1 = ver();
  await put(vs1, 0, "AAAA", huge);
  expect("full: a small one still fits", await commit(vs1, { parts: 1, expect: null }, huge), 200);

  // this run's markets go, so the cap never fills on a database the suite runs on again
  for (const n of [name, race, huge]) expect(`blobs: ${n.replace(RUN, "<run>")} removed`, await call("DELETE", `/api/job/blobs/${n}`, { auth: JOB }), 204);
}

/** The run's report prunes a part left uncommitted for over a day, and never a live or a kept version's. */
async function pruneOnly() {
  const get = (p) => call("GET", p, { auth: JOB });
  const at = `/api/job/blobs/${PRUNE_MARKET}`;
  check("prune: the orphan is there before the report", (await get(`${at}/${PRUNE_ORPHAN}/0`)).body?.data === "T1JQSEFO");
  const kept = (await get("/api/job/blobs/list:open/versions")).body?.versions ?? [];
  expect("prune: done is 204", await call("POST", "/api/job/done", { auth: JOB, body: {} }), 204);
  expect("prune: a two-day-old part of no version is gone", await get(`${at}/${PRUNE_ORPHAN}/0`), 404, "no_part");
  check("prune: a two-day-old live part stays", (await get(`${at}/${PRUNE_LIVE_VERSION}/0`)).body?.data === "TElWRQ==");
  check("prune: the kept versions' parts stay", kept.length === 3 && (await Promise.all(kept.map((k) => get(`/api/job/blobs/list:open/${k.version}/0`)))).every((r) => r.status === 200), kept.map((k) => k.version));
}

try {
  const h = await fetch(`${BASE}/api/health`).catch(() => null);
  if (!h) {
    console.log(`No Worker answers at ${BASE} (start it with npm run dev).`);
    process.exit(1);
  }
  if (BOT_FAIL) await botFailOnly();
  else if (PRODUCTION) await productionOnly();
  else if (DISPATCH) await dispatchOnly();
  else if (PRUNE) await pruneOnly();
  else await suite();
} catch (e) {
  failed.push(`threw: ${e?.stack ?? e}`);
  console.log(`FAIL the test threw: ${e?.stack ?? e}`);
}
console.log(`${passed}/${passed + failed.length} checks passed${MODE}`);
process.exit(failed.length ? 1 : 0);
