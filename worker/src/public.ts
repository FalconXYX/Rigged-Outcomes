// What any visitor can do, with no sign-in: read and add timeline markers (and undo their own for 24
// hours), put an open market on the site's one watchlist or take it off, and add a closed one to the main
// page (which queues its add run and wakes the step function at once, queue.ts and wake.ts). Every write
// carries a Turnstile token (taking a market off the watchlist needs none), checked before a daily limit is
// spent, and may only touch what the job listed in the catalog; the browser never supplies a market's text.

import { CONTRACT } from "./contract";

import {
  BODY,
  CONDITION_ID,
  type Ctx,
  DAY,
  MARKERS_A_DAY_PER_MARKET,
  MARKET_NAME,
  cleanLabel,
  fail,
  json,
  noContent,
  normSlug,
  now,
  quota,
  randomHex,
  readBody,
  safeEqual,
  sha256hex,
  text,
  turnstile,
  visitorOf,
} from "./lib";
import { graphJobs, runId } from "./queue";
import { wakeReady } from "./wake";

/** Live markers a market's timeline holds at most. */
const MARKERS_MAX = 60;
const UNDO_TOKEN = /^[0-9a-f]{64}$/;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

const story = (v: unknown) => text(v, "story", 200);

function conditionId(v: unknown): string {
  const cid = typeof v === "string" ? v.toLowerCase() : "";
  if (!CONDITION_ID.test(cid)) fail(400, "bad_request", "condition_id must be 0x and 64 hex characters.");
  return cid;
}

// ---------- timeline markers ----------

/** Each market page's marker list, kept 15 s in this isolate; a write through this isolate clears it. */
const MARKER_CACHE = new Map<string, { at: number; body: string }>();
const CACHE_MS = 15_000;

/** Forget cached marker lists: one market's, or all (the catalog changed). */
export function forgetMarkers(market?: string): void {
  if (market === undefined) MARKER_CACHE.clear();
  else MARKER_CACHE.delete(market);
}

export async function getMarkers({ env, url }: Ctx): Promise<Response> {
  const market = url.searchParams.get("market") ?? "";
  if (!MARKET_NAME.test(market)) fail(400, "bad_request", "market must be a markets.json name.");
  const headers = { "cache-control": "public, max-age=15" };
  const hit = MARKER_CACHE.get(market);
  if (hit && Date.now() - hit.at < CACHE_MS) {
    return new Response(hit.body, { headers: { "content-type": "application/json; charset=utf-8", ...headers } });
  }
  const [listed, rows] = await env.DB.batch([
    env.DB.prepare("SELECT 1 FROM catalog WHERE kind = 'market' AND id = ?").bind(market),
    env.DB.prepare(
      "SELECT id, t, label, source, created_at FROM markers WHERE market = ? AND deleted_at IS NULL ORDER BY t, created_at LIMIT ?",
    ).bind(market, MARKERS_MAX),
  ]);
  const isListed = listed.results.length > 0;
  const body = JSON.stringify({ listed: isListed, markers: isListed ? rows.results : [] });
  if (MARKER_CACHE.size > 500) MARKER_CACHE.clear();
  MARKER_CACHE.set(market, { at: Date.now(), body });
  return new Response(body, { headers: { "content-type": "application/json; charset=utf-8", ...headers } });
}

/**
 * Add a marker, in the order the contract fixes: body, shape (t: an integer from 1.4e9 to 400 days
 * ahead, in the site's US/Eastern-read-as-UTC seconds), label, bot check, catalog, room (60 live), duplicate
 * (the same label, any case, within 60 s of the same time), the daily limits (the visitor's, this market's 20
 * new markers a day from everyone, which is 429 timeline_busy, then the site's), then the insert. The undo
 * token comes back once; only its sha-256 is kept.
 */
export async function addMarker({ req, env }: Ctx): Promise<Response> {
  const body = await readBody(req, BODY.visitor);
  const market = body.market;
  if (typeof market !== "string" || !MARKET_NAME.test(market)) fail(400, "bad_request", "market must be a markets.json name.");
  const t = body.t;
  if (typeof t !== "number") fail(400, "bad_request", "t must be a number (seconds).");
  if (!Number.isInteger(t) || t < 1.4e9 || t > now() + 400 * DAY) fail(400, "bad_time", "t is outside the times a marker can have.");
  const label = cleanLabel(body.label);
  await turnstile(req, env, body.token, "marker");
  const visitor = await visitorOf(req, env);

  const [listed, live, near] = await env.DB.batch([
    env.DB.prepare("SELECT 1 FROM catalog WHERE kind = 'market' AND id = ?").bind(market),
    env.DB.prepare("SELECT count(*) AS n FROM markers WHERE market = ? AND deleted_at IS NULL").bind(market),
    env.DB.prepare("SELECT label FROM markers WHERE market = ? AND deleted_at IS NULL AND t BETWEEN ? AND ?").bind(
      market,
      t - 60,
      t + 60,
    ),
  ]);
  if (!listed.results.length) fail(404, "unknown_market", "That market is not on the main page.");
  if (((live.results[0] as { n: number }).n ?? 0) >= MARKERS_MAX) fail(409, "full", `This timeline holds ${MARKERS_MAX} markers.`);
  const lower = label.toLowerCase();
  if ((near.results as { label: string }[]).some((r) => r.label.toLowerCase() === lower)) {
    fail(409, "duplicate", "The same marker is already within a minute of that time.");
  }
  await quota(req, env, "marker", visitor, [
    {
      kind: `marker:${market}`,
      cap: MARKERS_A_DAY_PER_MARKET,
      code: "timeline_busy",
      message: `This timeline takes ${MARKERS_A_DAY_PER_MARKET} new markers a day.`,
    },
  ]);

  const id = crypto.randomUUID();
  const undo = randomHex(32);
  const at = now();
  // The room check again, inside the insert, so two adds at 59 can't both land.
  const added = await env.DB.prepare(
    `INSERT INTO markers (id, market, t, label, source, visitor, undo_sha, created_at)
     SELECT ?1, ?2, ?3, ?4, 'manual', ?5, ?6, ?7
     WHERE (SELECT count(*) FROM markers WHERE market = ?2 AND deleted_at IS NULL) < ?8`,
  )
    .bind(id, market, t, label, visitor, await sha256hex(undo), at, MARKERS_MAX)
    .run();
  if (!added.meta.changes) fail(409, "full", `This timeline holds ${MARKERS_MAX} markers.`);
  forgetMarkers(market);
  return json({ marker: { id, t, label, source: "manual", created_at: at }, undo }, 201);
}

/** Undo one's own marker (X-Undo: the token its add returned), within 24 hours. */
export async function undoMarker({ req, env, params }: Ctx): Promise<Response> {
  const id = params[0];
  const row = UUID.test(id)
    ? await env.DB.prepare("SELECT market, undo_sha, created_at, deleted_at FROM markers WHERE id = ?")
        .bind(id)
        .first<{ market: string; undo_sha: string | null; created_at: number; deleted_at: number | null }>()
    : null;
  if (!row || row.deleted_at !== null) fail(404, "not_found", "No such marker.");
  const token = req.headers.get("x-undo") ?? "";
  const mine =
    UNDO_TOKEN.test(token) && !!row.undo_sha && now() - row.created_at < DAY && (await safeEqual(await sha256hex(token), row.undo_sha));
  if (!mine) fail(403, "not_yours", "Only whoever added a marker can remove it, within 24 hours.");
  await env.DB.prepare("UPDATE markers SET deleted_at = ?, deleted_by = 'visitor' WHERE id = ? AND deleted_at IS NULL")
    .bind(now(), id)
    .run();
  forgetMarkers(row.market);
  return noContent();
}

// ---------- watch: one watchlist for the whole site ----------

const onWatchlist = (env: Ctx["env"], s: string) => env.DB.prepare("SELECT 1 FROM watches WHERE story = ?").bind(s);

/** Whether an open card's market is on the site's watchlist (the same answer for every visitor). */
export async function getWatch({ env, url }: Ctx): Promise<Response> {
  const s = story(url.searchParams.get("story") ?? "");
  const [listed, on] = await env.DB.batch([
    env.DB.prepare("SELECT 1 FROM catalog WHERE kind = 'open' AND id = ?").bind(s),
    onWatchlist(env, s),
  ]);
  return json({ listed: listed.results.length > 0, watching: on.results.length > 0 });
}

/** Put an open card's market on the site's watchlist, for everyone. Already on it: nothing changes and no limit is spent. */
export async function watch({ req, env }: Ctx): Promise<Response> {
  const body = await readBody(req, BODY.visitor);
  const s = story(body.story);
  await turnstile(req, env, body.token, "watch");
  const visitor = await visitorOf(req, env);
  const listed = await env.DB.prepare("SELECT 1 FROM catalog WHERE kind = 'open' AND id = ?").bind(s).first();
  if (!listed) fail(404, "unknown_story", "That market has left the open list.");
  if (await onWatchlist(env, s).first()) return json({ watching: true });
  await quota(req, env, "watch", visitor);
  await env.DB.prepare("INSERT OR IGNORE INTO watches (story, created_at) VALUES (?, ?)").bind(s, now()).run();
  return json({ watching: true });
}

/** Take a market off the site's watchlist, for everyone (no bot check). Spends a watch change only when it was on. */
export async function unwatch({ req, env, url }: Ctx): Promise<Response> {
  const s = story(url.searchParams.get("story") ?? "");
  if (!(await onWatchlist(env, s).first())) return json({ watching: false });
  await quota(req, env, "watch", await visitorOf(req, env));
  await env.DB.prepare("DELETE FROM watches WHERE story = ?").bind(s).run();
  return json({ watching: false });
}

// ---------- "Add to main page" ----------

type RequestRow = { status: string; name: string | null; decided_at: number | null; done_at: number | null; note: string | null };

/** The note of an added market's request once it was taken off the main page (backend.site rm: blobs.deleteBlob). */
const TAKEN_OFF = "taken off the main page";
const takenOff = (r: RequestRow) => r.status === "failed" && r.note === TAKEN_OFF;

/**
 * What any visitor may see of a market sent to the main page: its state (approved, added, failed: the job
 * couldn't add it, or removed: taken off the main page) and when it changed.
 */
const publicRequest = (r: RequestRow) => ({ status: takenOff(r) ? "removed" : r.status, name: r.name, decided_at: r.decided_at, done_at: r.done_at });

const REQUEST_ROW = "SELECT status, name, decided_at, done_at, note FROM requests WHERE condition_id = ?";

/**
 * A closed market's state for a visitor's card, which open pages ask every 15 seconds while it is on its way.
 * Nothing is restarted from here: the queue retries a step, and the cron's watchdog mends an add left with no run.
 */
export async function getRequest({ env, url }: Ctx): Promise<Response> {
  const cid = conditionId(url.searchParams.get("cid"));
  const [listed, row] = await env.DB.batch([
    env.DB.prepare("SELECT 1 FROM catalog WHERE kind = 'closed' AND id = ?").bind(cid),
    env.DB.prepare(REQUEST_ROW).bind(cid),
  ]);
  const r = row.results[0] as RequestRow | undefined;
  return json({ listed: listed.results.length > 0, request: r ? publicRequest(r) : null });
}

/**
 * Add a closed pick to the main page: it is recorded as approved at once and its add run is made in the same
 * batch (add.fetch, the whale batches, add.export: docs/BACKEND.md), then the add lane is woken after
 * the answer, so the market is on the main page within minutes with no site rebuild (the page name comes from
 * the slug there, add_market.page_name). One row per market: an existing one comes back as it is (200, spending
 * nothing and making nothing), except one the job could not add (failed, not one taken off the main
 * page), which is approved again with a run of its own, like a new add. The question, links and dates are
 * copied from the catalog's pick.
 */
export async function addRequest({ req, env, waitUntil, clock }: Ctx): Promise<Response> {
  const body = await readBody(req, BODY.visitor);
  const cid = conditionId(body.condition_id);
  await turnstile(req, env, body.token, "request");
  const visitor = await visitorOf(req, env);

  const [existing, pick] = await env.DB.batch([
    env.DB.prepare(REQUEST_ROW).bind(cid),
    env.DB.prepare("SELECT slug, data FROM catalog WHERE kind = 'closed' AND id = ?").bind(cid),
  ]);
  const old = existing.results[0] as RequestRow | undefined;
  if (old && (old.status !== "failed" || takenOff(old))) return json({ request: publicRequest(old) });
  const p = pick.results[0] as { slug: string; data: string } | undefined;
  if (!p) fail(404, "not_listed", "That market is no longer on the closed list.");
  const onMain = await env.DB.prepare("SELECT 1 FROM catalog WHERE kind = 'market' AND slug = ?").bind(normSlug(p.slug)).first();
  if (onMain) fail(409, "on_main", "That market is already on the main page.");
  const at = await clock();
  await quota(req, env, "request", visitor, [], at);

  const d = JSON.parse(p.data) as Record<string, string>;
  const slot = `${cid}:${at}`;
  const run = runId("add", slot);
  const [added] = await env.DB.batch([
    old
      ? env.DB.prepare(
          `UPDATE requests SET status = 'approved', note = NULL, done_at = NULL, decided_at = ?2, run_id = ?4
           WHERE condition_id = ?1 AND status = 'failed' AND coalesce(note, '') != ?3`,
        ).bind(cid, at, TAKEN_OFF, run)
      : env.DB.prepare(
          `INSERT INTO requests (condition_id, slug, question, url, winner, closed, day, status, requested_at, decided_at, run_id)
           VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, 'approved', ?8, ?8, ?9) ON CONFLICT (condition_id) DO NOTHING`,
        ).bind(cid, d.slug, d.question, d.url, d.winner, d.closed, d.day, at, run),
    // the run only when this click made the request approved with it (a click racing this one makes none)
    env.DB.prepare(
      `INSERT INTO runs (id, kind, slot, state, meta, created_at, deadline)
       SELECT ?1, 'add', ?2, 'queued', json_object('condition_id', ?3), ?4, ?5
       WHERE EXISTS (SELECT 1 FROM requests WHERE condition_id = ?3 AND status = 'approved' AND run_id = ?1)
       ON CONFLICT DO NOTHING`,
    ).bind(run, slot, cid, at, at + CONTRACT.schedule.add_run_s),
    graphJobs(env, "add", [run], at),
  ]);
  if (added.meta.changes) waitUntil(wakeReady(env, at, { only: ["add"], handler: true }));
  const row = await env.DB.prepare(REQUEST_ROW).bind(cid).first<RequestRow>();
  return json({ request: publicRequest(row!) }, 201);
}
