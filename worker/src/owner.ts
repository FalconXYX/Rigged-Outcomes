// The owner's endpoints, behind OWNER_TOKEN, for the private review page (#/review): one overview (which
// also lists the markets sent to the main page, the site's watchlist and the backend's runs, failures and
// budget), the open list's decisions, deleting markers, the pause switch, and "Apply decisions now" (an apply
// run queued and the main lane woken). All work while paused.

import { CONTRACT } from "./contract";
import { readBudget } from "./ledger";
import { APPLIES_A_DAY, BODY, type Ctx, DAY, DAY_TEXT, counter, fail, json, limitDay, noContent, now, oneOf, readBody, text } from "./lib";
import { forgetMarkers } from "./public";
import { makeRun, runId } from "./queue";
import { wakeReady } from "./wake";

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

/**
 * Everything the review page shows, in one batch of reads. Besides the lists: the newest queue day (settings.queue_day:
 * an empty one reads as empty), when the job's token was last refused (job_refused_at, lib.authorize), and the
 * backend (docs/BACKEND.md): the budget, each lane's pause and wake-ups, the latest 15 runs and the
 * latest 10 failed jobs of the last 14 days. A decision on a whole card carries the card's question (its
 * headline is null).
 */
export async function overview({ env, clock }: Ctx): Promise<Response> {
  const at = await clock();
  const [settings, queue, decisions, requests, markers, watched, runs, lanes, backendRuns, failed] = await env.DB.batch([
    env.DB.prepare("SELECT key, value FROM settings WHERE key IN ('writes', 'queue_day', 'job_refused', 'budget')"),
    env.DB.prepare("SELECT day, run_at, steps_by, item FROM open_queue WHERE day = (SELECT max(day) FROM open_queue) ORDER BY n"),
    env.DB.prepare(
      `SELECT d.*, coalesce(
         (SELECT json_extract(q.item, '$.question') FROM open_queue q WHERE q.story = d.story ORDER BY q.day DESC LIMIT 1),
         (SELECT json_extract(c.data, '$.question') FROM catalog c WHERE c.kind = 'open' AND c.id = d.story)) AS question
       FROM open_decisions d WHERE d.decision != 'withdrawn' AND (d.applied_at IS NULL OR d.applied_at >= ?) ORDER BY d.decided_at DESC`,
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
    env.DB.prepare("SELECT lane, paused_until, unanswered, last_wake_at, last_claim_at, wake_error FROM lanes ORDER BY lane"),
    env.DB.prepare(
      "SELECT id, kind, slot, state, created_at, started_at, finished_at, deadline, meta FROM runs ORDER BY created_at DESC, id DESC LIMIT 15",
    ),
    env.DB.prepare("SELECT id, kind, error, updated_at FROM jobs WHERE state = 'failed' AND updated_at >= ? ORDER BY updated_at DESC LIMIT 10").bind(
      at - 14 * DAY,
    ),
  ]);
  const kept = Object.fromEntries((settings.results as { key: string; value: string }[]).map((r) => [r.key, r.value]));
  const parsed = <T>(v: string | undefined): T | null => {
    try {
      return v ? (JSON.parse(v) as T) : null;
    } catch {
      return null;
    }
  };
  const q = queue.results as { day: string; run_at: string; steps_by: string; item: string }[];
  const latest = parsed<{ day: string; run_at: string; steps_by: string }>(kept.queue_day);
  return json({
    now: at,
    settings: { writes: kept.writes === "off" ? "off" : "on" },
    job_refused_at: kept.job_refused ? Number(kept.job_refused) : null,
    queue:
      q.length && !(latest && latest.day > q[0].day)
        ? { day: q[0].day, run_at: q[0].run_at, steps_by: q[0].steps_by, items: q.map((r) => JSON.parse(r.item)) }
        : latest
          ? { ...latest, items: [] }
          : null,
    decisions: decisions.results.map((r) => ({ ...decision(r), question: (r as { question?: string | null }).question ?? null })),
    requests: requests.results,
    markers: markers.results,
    watched: (watched.results as { story: string; since: number; data: string | null }[]).map((w) => {
      const c = w.data ? (JSON.parse(w.data) as { question?: string; url?: string }) : {};
      return { story: w.story, since: w.since, question: c.question ?? null, url: c.url ?? null };
    }),
    runs: runs.results,
    backend: {
      budget: readBudget(kept.budget),
      lanes: lanes.results,
      runs: (backendRuns.results as { meta: string }[]).map((r) => ({ ...r, meta: parsed<Record<string, unknown>>(r.meta) ?? {} })),
      failed_jobs: failed.results,
    },
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

/**
 * "Apply decisions now": an apply run (apply.decide: decisions into the state, the reviewed day rebuilt, the open
 * list saved) queued and the main lane woken after the answer. A press while an apply run is still queued joins
 * it (nothing more is spent); otherwise it makes one, 30 a Toronto day (429 limit after that).
 */
export async function apply({ req, env, waitUntil, clock }: Ctx): Promise<Response> {
  const at = await clock();
  const waiting = await env.DB.prepare("SELECT id FROM runs WHERE kind = 'apply' AND state = 'queued' ORDER BY created_at DESC LIMIT 1").first<string>("id");
  if (waiting) {
    waitUntil(wakeReady(env, at, { only: ["main"], handler: true }));
    return json({ queued: true, run: waiting }, 202);
  }
  if (!(await counter(env, limitDay(req, env, at), "apply", "*", APPLIES_A_DAY).first())) {
    fail(429, "limit", `Decisions have been applied ${APPLIES_A_DAY} times today. Try again tomorrow.`);
  }
  const slot = String(at);
  await env.DB.batch(makeRun(env, { kind: "apply", slot, at, deadline: at + CONTRACT.schedule.apply_run_s }));
  waitUntil(wakeReady(env, at, { only: ["main"], handler: true }));
  return json({ queued: true, run: runId("apply", slot) }, 202);
}
