// The owner's endpoints, behind OWNER_TOKEN, for the private review page (#/review): one overview (which
// also lists the markets sent to the main page and the site's watchlist), the open list's decisions,
// deleting markers, the pause switch, and "Apply decisions now" (a GitHub workflow dispatch, when a token for
// it is set). All work while paused.

import {
  BODY,
  type Ctx,
  DAY,
  DAY_TEXT,
  counter,
  fail,
  json,
  log,
  noContent,
  now,
  oneOf,
  readBody,
  text,
  utcDay,
} from "./lib";
import { forgetMarkers } from "./public";

const DECISIONS = ["step", "not a step", "card approved"] as const;

/** A decision row with its headline JSON parsed (the contract's Decision). */
export function decision(row: unknown) {
  const r = row as Record<string, unknown>;
  return {
    story: r.story,
    headline_key: r.headline_key,
    decision: r.decision,
    headline: r.headline ? JSON.parse(r.headline as string) : null,
    day: r.day,
    decided_at: r.decided_at,
    applied_at: r.applied_at,
  };
}

export const MARKER_COLUMNS = "id, market, t, label, source, created_at, deleted_at, deleted_by";

/** Everything the review page shows, in one batch of reads. */
export async function overview({ env }: Ctx): Promise<Response> {
  const at = now();
  const [settings, queue, decisions, requests, markers, watched, runs] = await env.DB.batch([
    env.DB.prepare("SELECT value FROM settings WHERE key = 'writes'"),
    env.DB.prepare("SELECT day, run_at, steps_by, item FROM open_queue WHERE day = (SELECT max(day) FROM open_queue) ORDER BY n"),
    env.DB.prepare(
      "SELECT * FROM open_decisions WHERE decision != 'withdrawn' AND (applied_at IS NULL OR applied_at >= ?) ORDER BY decided_at DESC",
    ).bind(at - 14 * DAY),
    env.DB.prepare("SELECT * FROM requests WHERE status != 'added' OR done_at >= ? ORDER BY requested_at DESC LIMIT 200").bind(
      at - 30 * DAY,
    ),
    env.DB.prepare(`SELECT ${MARKER_COLUMNS} FROM markers ORDER BY created_at DESC LIMIT 50`),
    env.DB.prepare(
      `SELECT w.story, w.created_at AS since, c.data FROM watches w
       LEFT JOIN catalog c ON c.kind = 'open' AND c.id = w.story ORDER BY w.created_at DESC LIMIT 200`,
    ),
    env.DB.prepare("SELECT * FROM job_runs ORDER BY id DESC LIMIT 10"),
  ]);
  const q = queue.results as { day: string; run_at: string; steps_by: string; item: string }[];
  return json({
    now: at,
    settings: { writes: (settings.results[0] as { value?: string } | undefined)?.value === "off" ? "off" : "on" },
    dispatch: !!env.GITHUB_DISPATCH_TOKEN,
    queue: q.length ? { day: q[0].day, run_at: q[0].run_at, steps_by: q[0].steps_by, items: q.map((r) => JSON.parse(r.item)) } : null,
    decisions: decisions.results.map(decision),
    requests: requests.results,
    markers: markers.results,
    watched: (watched.results as { story: string; since: number; data: string | null }[]).map((w) => {
      const c = w.data ? (JSON.parse(w.data) as { question?: string; url?: string }) : {};
      return { story: w.story, since: w.since, question: c.question ?? null, url: c.url ?? null };
    }),
    runs: runs.results,
  });
}

/**
 * Decide one headline of the open list's queue ("step" or "not a step"), or a dated card as a whole
 * (headline_key "card": "card approved" or "not a step"). The headline is copied from the pushed queue, never
 * from the browser. A decision already made can be changed even once its headline has left the queue (a run
 * that applies a decision rebuilds the queue without it): it keeps its stored headline and day. Deciding again
 * replaces the decision and makes it pending; decided_at only moves forward, so the job can tell a decision
 * changed after it read it.
 */
export async function putDecision({ req, env }: Ctx): Promise<Response> {
  const body = await readBody(req, BODY.owner);
  let day = text(body.day, "day", 10);
  if (!DAY_TEXT.test(day)) fail(400, "bad_request", "day must be YYYY-MM-DD.");
  const story = text(body.story, "story", 200);
  const key = text(body.headline_key, "headline_key", 500);
  const choice = oneOf(body.decision, "decision", DECISIONS);
  const card = key === "card";
  if (card && choice === "step") fail(400, "bad_request", "A whole card is either \"card approved\" or \"not a step\".");
  if (!card && choice === "card approved") fail(400, "bad_request", "A headline is either \"step\" or \"not a step\".");

  const [queued, earlier] = await env.DB.batch([
    env.DB.prepare("SELECT item FROM open_queue WHERE day = ? AND story = ?").bind(day, story),
    env.DB.prepare("SELECT headline, day FROM open_decisions WHERE story = ? AND headline_key = ? AND decision != 'withdrawn'").bind(
      story,
      key,
    ),
  ]);
  const row = queued.results[0] as { item: string } | undefined;
  const prev = earlier.results[0] as { headline: string | null; day: string } | undefined;
  const item = row ? (JSON.parse(row.item) as { place?: string; candidates?: Record<string, unknown>[] }) : null;
  const c = item && !card ? (item.candidates ?? []).find((h) => h.key === key) : undefined;
  let headline: string | null = null;
  if (card && item) {
    if (item.place !== "dated") fail(400, "bad_request", "Only a dated item is decided as a whole card.");
  } else if (c) {
    headline = JSON.stringify({ title: c.title ?? null, source: c.source ?? null, published: c.published ?? null, url: c.url ?? null });
  } else if (prev) {
    headline = prev.headline;
    day = prev.day;
  } else {
    fail(404, "not_found", row ? "That headline is not among the item's candidates." : "That story is not in that day's queue.");
  }
  const saved = await env.DB.prepare(
    `INSERT INTO open_decisions (story, headline_key, decision, headline, day, decided_at, applied_at)
     VALUES (?1, ?2, ?3, ?4, ?5, ?6, NULL)
     ON CONFLICT (story, headline_key) DO UPDATE SET decision = excluded.decision, headline = excluded.headline,
       day = excluded.day, decided_at = max(excluded.decided_at, open_decisions.decided_at + 1), applied_at = NULL
     RETURNING *`,
  )
    .bind(story, key, choice, headline, day, now())
    .first();
  return json({ decision: decision(saved!) });
}

/**
 * Take back a decision the job has not reported applied (an applied one is changed by deciding the opposite).
 * The row becomes "withdrawn" rather than going at once: a run that already fetched the decision may have
 * saved it to its store, so the next run reads the withdrawal, removes the decision there too, and reports it,
 * and only then does the row go. The review page no longer lists it either way.
 */
export async function deleteDecision({ env, url }: Ctx): Promise<Response> {
  const story = text(url.searchParams.get("story"), "story", 200);
  const key = text(url.searchParams.get("key"), "key", 500);
  const gone = await env.DB.prepare(
    `UPDATE open_decisions SET decision = 'withdrawn', decided_at = max(?3, decided_at + 1)
     WHERE story = ?1 AND headline_key = ?2 AND applied_at IS NULL AND decision != 'withdrawn' RETURNING story`,
  )
    .bind(story, key, now())
    .first();
  if (gone) return noContent();
  const left = await env.DB.prepare("SELECT 1 FROM open_decisions WHERE story = ? AND headline_key = ? AND decision != 'withdrawn'")
    .bind(story, key)
    .first();
  if (left) fail(409, "applied", "That decision is already applied; make the opposite decision instead.");
  fail(404, "not_found", "No such decision.");
}

/** Delete any marker (kept 30 days as deleted, then removed for good). */
export async function deleteMarker({ env, params }: Ctx): Promise<Response> {
  const id = params[0];
  const hit = await env.DB.prepare("UPDATE markers SET deleted_at = ?, deleted_by = 'owner' WHERE id = ? AND deleted_at IS NULL RETURNING market")
    .bind(now(), id)
    .first<{ market: string }>();
  if (hit) {
    forgetMarkers(hit.market);
    return noContent();
  }
  if (await env.DB.prepare("SELECT 1 FROM markers WHERE id = ?").bind(id).first()) return noContent();
  fail(404, "not_found", "No such marker.");
}

/** The pause switch: writes "off" stops visitors' markers, watches and adds to the main page (undo and unwatch still work). */
export async function putSettings({ req, env }: Ctx): Promise<Response> {
  const body = await readBody(req, BODY.owner);
  const writes = oneOf(body.writes, "writes", ["on", "off"] as const);
  await env.DB.prepare(
    "INSERT INTO settings (key, value, updated_at) VALUES ('writes', ?1, ?2) ON CONFLICT (key) DO UPDATE SET value = ?1, updated_at = ?2",
  )
    .bind(writes, now())
    .run();
  forgetMarkers();
  return json({ settings: { writes } });
}

const DISPATCH_GAP = 120;
const DISPATCHES_A_DAY = 30;

/**
 * "Apply decisions now": start the Watchlist workflow in apply mode (GITHUB_DISPATCH_TOKEN, a fine-grained
 * token for this repository with Actions: read and write). At most one every 2 minutes and 30 a day.
 */
export async function apply({ req, env }: Ctx): Promise<Response> {
  if (!env.GITHUB_DISPATCH_TOKEN) fail(501, "no_dispatch", "No GITHUB_DISPATCH_TOKEN is set, so the next hourly run applies decisions.");
  const at = now();
  const prev = (await env.DB.prepare("SELECT value FROM settings WHERE key = 'last_dispatch'").first<string>("value")) ?? "0";
  const claimed = await env.DB.prepare(
    `INSERT INTO settings (key, value, updated_at) VALUES ('last_dispatch', ?1, ?2)
     ON CONFLICT (key) DO UPDATE SET value = ?1, updated_at = ?2 WHERE CAST(settings.value AS INTEGER) <= ?3
     RETURNING value`,
  )
    .bind(String(at), at, at - DISPATCH_GAP)
    .first();
  if (!claimed) fail(429, "limit", "The update job was started less than 2 minutes ago.");
  const giveBack = () =>
    env.DB.prepare("UPDATE settings SET value = ? WHERE key = 'last_dispatch' AND value = ?").bind(prev, String(at)).run();
  if (!(await counter(env, utcDay(req, env), "dispatch", "*", DISPATCHES_A_DAY).first())) {
    await giveBack();
    fail(429, "limit", `The update job has been started ${DISPATCHES_A_DAY} times today.`);
  }

  let status = 0;
  let detail = "";
  try {
    const api = (env.DEV === "on" && env.GITHUB_API) || "https://api.github.com";
    const res = await fetch(
      `${api}/repos/${env.GITHUB_REPO}/actions/workflows/${encodeURIComponent(env.GITHUB_WORKFLOW)}/dispatches`,
      {
        method: "POST",
        headers: {
          Authorization: `Bearer ${env.GITHUB_DISPATCH_TOKEN}`,
          Accept: "application/vnd.github+json",
          "X-GitHub-Api-Version": "2022-11-28",
          "User-Agent": "rigged-outcomes-api",
          "content-type": "application/json",
        },
        body: JSON.stringify({ ref: "main", inputs: { mode: "apply" } }),
        signal: AbortSignal.timeout(10_000),
      },
    );
    status = res.status;
    if (status !== 204) detail = ((await res.json<{ message?: string }>().catch(() => ({}))) as { message?: string }).message ?? "";
  } catch (e) {
    detail = String(e);
  }
  if (status !== 204) {
    await giveBack();
    log({ event: "dispatch_failed", status, detail: detail.slice(0, 200) });
    fail(502, "github", `GitHub didn't start the job (${status || "no answer"}${detail ? `: ${detail.slice(0, 200)}` : ""}).`);
  }
  return json({ dispatched: true }, 202);
}
