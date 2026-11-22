// The job queue in D1 (docs/BACKEND.md, migrations/0004_backend.sql), behind JOB_TOKEN: the
// step function claims one job of a lane under a lease, checkpoints what it finds as it goes, completes the step
// with one of four outcomes (done, partial, later, error), and reads back its run's results and the news
// searches. This file also makes runs and their jobs (makeRun), for the schedule (cron.ts), a visitor's add
// (public.ts) and a watched market (job.ts).

import { CONTRACT, JOB_KINDS, type JobKind, type Lane, RUN_GRAPHS, type RunKind, isJobKind, toronto } from "./contract";
import { addUsage, blockedText, capsOf, readBudget } from "./ledger";
import { type Ctx, type Env, fail, int, json, list, noContent, obj, oneOf, randomHex, readBody, text } from "./lib";
import { BACKGROUND, BLOCKED_NOW, foregroundReady, wakeReady } from "./wake";

const C = CONTRACT;
const L = C.limits;
const MAX_LATER_S = 2 * 86_400;
const JOB_ID = /^[\w:.|-]{1,400}$/;
const TOKEN = /^[0-9a-f]{32}$/;
const PART = /^[\w.:-]{1,64}$/;

// ---------- runs ----------

export const runId = (kind: RunKind, slot: string) => `${kind}:${slot}`;
const runKindOf = (id: string) => id.slice(0, id.indexOf(":")) as RunKind;

/** A run kind's graph as job rows: lane, heavy, need_s and final from the kind, after as 'kind,kind'. */
const graphRows = (kind: RunKind) =>
  RUN_GRAPHS[kind].map((g) => {
    const k = JOB_KINDS[g.kind as JobKind];
    return { kind: g.kind, lane: k.lane, heavy: k.heavy ? 1 : 0, need_s: k.need_s, final: k.final ? 1 : 0, after: g.after.join(",") };
  });

/**
 * The graph's jobs (part '0') for runs of one kind, by id: only runs made in this same batch (queued, created at
 * `at`), so making a run again never adds jobs to one that already ran.
 */
export const graphJobs = (env: Env, kind: RunKind, ids: string[], at: number, input: Record<string, unknown> = {}) =>
  env.DB.prepare(
    `INSERT OR IGNORE INTO jobs (id, run_id, kind, part, lane, heavy, need_s, final, priority, after, state, run_after, input, created_at, updated_at)
     SELECT r.id || '|' || json_extract(g.value, '$.kind') || '|0', r.id, json_extract(g.value, '$.kind'), '0',
            json_extract(g.value, '$.lane'), json_extract(g.value, '$.heavy'), json_extract(g.value, '$.need_s'),
            json_extract(g.value, '$.final'), ?1, json_extract(g.value, '$.after'), 'queued', ?2, ?3, ?2, ?2
     FROM runs r, json_each(?4) g
     WHERE r.id IN (SELECT value FROM json_each(?5)) AND r.state = 'queued' AND r.created_at = ?2`,
  ).bind(C.run_priority[kind], at, JSON.stringify(input), JSON.stringify(graphRows(kind)), JSON.stringify(ids));

export interface NewRun {
  kind: RunKind;
  slot: string;
  at: number;
  deadline: number;
  /** 'skipped': due while the budget is stop (no jobs, finished at once). */
  state?: "queued" | "skipped";
  /** Every graph job's input (a catch-up's {catch_up, from_run}). */
  input?: Record<string, unknown>;
  /** The run's meta to start with (an add's {condition_id}). */
  meta?: Record<string, unknown>;
}

/** The statements that make a run and its graph's jobs; a run already made (its kind and slot) is left as it is. */
export function makeRun(env: Env, r: NewRun): D1PreparedStatement[] {
  const id = runId(r.kind, r.slot);
  const state = r.state ?? "queued";
  return [
    env.DB.prepare(
      `INSERT INTO runs (id, kind, slot, state, meta, created_at, deadline, finished_at) VALUES (?1, ?2, ?3, ?4, ?8, ?5, ?6, ?7)
       ON CONFLICT DO NOTHING`,
    ).bind(id, r.kind, r.slot, state, r.at, r.deadline, state === "queued" ? null : r.at, JSON.stringify(r.meta ?? {})),
    ...(state === "queued" ? [graphJobs(env, r.kind, [id], r.at, r.input)] : []),
  ];
}

/**
 * A run whose jobs are all terminal gets its final state: failed when a final job failed, partial when any job
 * failed or was cancelled, else done (`id` null: every live run, the watchdog's sweep).
 */
export const finishRuns = (env: Env, at: number, id: string | null = null) =>
  env.DB.prepare(
    `UPDATE runs SET finished_at = ?1, state = CASE
       WHEN EXISTS (SELECT 1 FROM jobs f WHERE f.run_id = runs.id AND f.final = 1 AND f.state = 'failed') THEN 'failed'
       WHEN EXISTS (SELECT 1 FROM jobs f WHERE f.run_id = runs.id AND f.state IN ('failed', 'cancelled')) THEN 'partial'
       ELSE 'done' END
     WHERE state IN ('queued', 'running') AND (?2 IS NULL OR id = ?2)
       AND NOT EXISTS (SELECT 1 FROM jobs u WHERE u.run_id = runs.id AND u.state IN ('queued', 'leased'))
     RETURNING id, state`,
  ).bind(at, id);

// ---------- fields ----------

const jobId = (v: unknown) => {
  const s = text(v, "job_id", 400);
  if (!JOB_ID.test(s)) fail(400, "bad_request", "job_id is not a job's id.");
  return s;
};

const leaseToken = (v: unknown) => {
  if (typeof v !== "string" || !TOKEN.test(v)) fail(400, "bad_request", "lease_token must be 32 lowercase hex characters.");
  return v;
};

const whole = (v: unknown, field: string) => {
  if (v === undefined || v === null) return 0;
  const n = int(v, field);
  if (n < 0) fail(400, "bad_request", `${field} must be 0 or more.`);
  return n;
};

/** A value's JSON text, at most `max` characters, or 413 too_large naming the field. */
function jsonText(v: unknown, field: string, max: number): string {
  const s = JSON.stringify(v ?? null);
  if (s.length > max) fail(413, "too_large", `${field} is over ${max} bytes.`);
  return s;
}

/** Results as [key, JSON text] pairs (the text goes into job_results as it is). */
function resultRows(v: unknown): [string, string][] {
  return list(v ?? [], "results", 2000).map((r, i) => {
    const o = obj(r, `results[${i}]`);
    if (o.value === undefined) fail(400, "bad_request", `results[${i}].value is required.`);
    return [text(o.key, `results[${i}].key`, L.result_key_chars), jsonText(o.value, `results[${i}].value`, L.result_value_bytes)];
  });
}

/** News searches as [key, JSON text of the headlines]. */
function newsRows(v: unknown): [string, string][] {
  return list(v ?? [], "news", 2000).map((r, i) => {
    const o = obj(r, `news[${i}]`);
    if (!Array.isArray(o.body)) fail(400, "bad_request", `news[${i}].body must be a list of headlines.`);
    return [text(o.key, `news[${i}].key`, L.news_key_chars), jsonText(o.body, `news[${i}].body`, L.news_body_bytes)];
  });
}

/** One line, at most error_chars characters (the step function has already taken secrets out). */
const oneLine = (s: string) => s.split(/\r?\n/)[0].slice(0, L.error_chars);

const parsed = (s: string | null | undefined, fallback: unknown = null) => {
  if (s === null || s === undefined) return fallback;
  try {
    return JSON.parse(s);
  } catch {
    return fallback;
  }
};

// ---------- claim ----------

/**
 * The claim (docs/BACKEND.md), plus: a non-final job of a run past its deadline is not handed out. In
 * the add lane the run with the fewest jobs left goes first, so a small add never waits behind a large one's
 * whale batches; a background job (wallets.score) goes only while no foreground job is ready for its lane or the
 * heavy slot (wake.ts foregroundReady), so no add and no daily walk waits behind a market's scoring.
 */
const CLAIM = `UPDATE jobs SET state = 'leased', lease_token = ?1, lease_until = ?2 + ?3 + ?4, updated_at = ?2, step_nonce = NULL
WHERE id = (
  SELECT j.id FROM jobs j JOIN runs r ON r.id = j.run_id
  WHERE j.state = 'queued' AND j.lane = ?5 AND j.run_after <= ?2 AND j.need_s <= ?3
    AND r.state IN ('queued', 'running') AND (j.final = 1 OR r.deadline > ?2)
    AND instr(?6, ',' || j.kind || ',') = 0
    AND NOT EXISTS (SELECT 1 FROM jobs d WHERE d.run_id = j.run_id AND d.state IN ('queued', 'leased')
                    AND instr(',' || j.after || ',', ',' || d.kind || ',') > 0)
    AND NOT EXISTS (SELECT 1 FROM jobs l WHERE l.state = 'leased' AND l.lane = j.lane AND l.lease_until > ?2)
    AND (j.heavy = 0 OR NOT EXISTS (SELECT 1 FROM jobs h WHERE h.state = 'leased' AND h.heavy = 1 AND h.lease_until > ?2))
    AND (j.kind NOT IN ${BACKGROUND} OR NOT ${foregroundReady("?2", "j.lane", "j.heavy", "?6")})
  ORDER BY j.priority,
    CASE WHEN j.lane = 'add' THEN (SELECT count(*) FROM jobs w WHERE w.run_id = j.run_id AND w.state IN ('queued', 'leased')) ELSE 0 END,
    j.run_after, j.created_at, j.id LIMIT 1)
AND state = 'queued'
RETURNING id, run_id, kind, part, input, cursor, attempt, laters, partials, lease_token, lease_until, after`;

interface Claimed {
  id: string;
  run_id: string;
  kind: string;
  part: string;
  input: string;
  cursor: string | null;
  attempt: number;
  laters: number;
  partials: number;
  lease_token: string;
  lease_until: number;
  after: string;
}

/**
 * POST /api/job/claim {lane, left_s, call, spent}: the next job of the lane under a lease of left_s + slack
 * seconds, or 204 (always for left_s 0, a call's last report of its own time). Either way the lane's unanswered goes to 0 and `spent` (the call's own time) goes to the
 * ledger as '_overhead'. Nothing is handed out at budget level stop, while the lane is paused, or of a kind the
 * level blocks. The run turns running on its first claim.
 */
export async function claim(ctx: Ctx): Promise<Response> {
  const { req, env } = ctx;
  const body = await readBody(req, L.claim_body_bytes);
  const lane = oneOf(body.lane, "lane", C.lanes) as Lane;
  const left = int(body.left_s, "left_s");
  if (left < 0 || left > C.step.function_max_s) fail(400, "bad_request", `left_s must be from 0 to ${C.step.function_max_s}.`);
  if (body.call !== undefined) text(body.call, "call", 64);
  const spent = body.spent === undefined || body.spent === null ? {} : obj(body.spent, "spent");
  const cpu = whole(spent.cpu_ms, "spent.cpu_ms");
  const wall = whole(spent.wall_ms, "spent.wall_ms");
  const at = await ctx.clock();
  const today = toronto(at).day;

  const [b, laneRow, whales] = await env.DB.batch([
    env.DB.prepare("SELECT value FROM settings WHERE key = 'budget'"),
    env.DB.prepare("SELECT paused_until FROM lanes WHERE lane = ?").bind(lane),
    env.DB.prepare("SELECT units FROM usage WHERE day = ? AND kind = 'add.whales'").bind(today),
    env.DB.prepare("UPDATE lanes SET unanswered = 0, last_claim_at = ? WHERE lane = ?").bind(at, lane),
    ...(cpu || wall ? [addUsage(env, today, "_overhead", { cpu_ms: cpu, gb_ms: wall * C.step.instance_gb })] : []),
  ]);
  const level = readBudget((b.results[0] as { value?: string } | undefined)?.value).level;
  const blocked = blockedText(level);
  const paused = ((laneRow.results[0] as { paused_until?: number } | undefined)?.paused_until ?? 0) > at;
  // left_s 0 is a call's last report: its own time goes to the ledger, and nothing is handed out
  if (left === 0 || blocked === null || paused) return noContent();

  const token = randomHex(16);
  const job = await env.DB.prepare(CLAIM).bind(token, at, left, C.step.lease_slack_s, lane, blocked).first<Claimed>();
  if (!job) return noContent();

  const [, runRows, missing] = await env.DB.batch([
    env.DB.prepare("UPDATE runs SET state = 'running', started_at = coalesce(started_at, ?1) WHERE id = ?2 AND state = 'queued'").bind(
      at,
      job.run_id,
    ),
    env.DB.prepare("SELECT kind, slot, deadline, meta FROM runs WHERE id = ?").bind(job.run_id),
    env.DB.prepare(
      `SELECT kind, part, state, error FROM jobs
       WHERE run_id = ?1 AND state IN ('failed', 'cancelled') AND instr(',' || ?2 || ',', ',' || kind || ',') > 0
       ORDER BY kind, part LIMIT 100`,
    ).bind(job.run_id, job.after),
  ]);
  const run = runRows.results[0] as { kind: RunKind; slot: string; deadline: number; meta: string };
  const caps = capsOf(level);
  const usedWhales = (whales.results[0] as { units?: number } | undefined)?.units ?? 0;
  return json({
    now: at,
    job: {
      id: job.id,
      run_id: job.run_id,
      run_kind: run.kind,
      run_slot: run.slot,
      kind: job.kind,
      part: job.part,
      input: parsed(job.input, {}),
      cursor: parsed(job.cursor),
      attempt: job.attempt,
      laters: job.laters,
      partials: job.partials,
      lease_token: job.lease_token,
      lease_until: job.lease_until,
      run_deadline: run.deadline,
      run_meta: parsed(run.meta, {}),
      missing: missing.results,
    },
    budget: {
      level,
      add_whales_left: Math.max(0, caps.add_whales_day - usedWhales),
      open_searches: caps.open_searches,
      open_reads: caps.open_reads,
      open_out: caps.open_out,
    },
  });
}

// ---------- checkpoint ----------

/** Results upserted on (run_id, key) for the job row the guard picks. */
const putResults = (env: Env, guard: string, binds: unknown[], rows: [string, string][], at: number) =>
  env.DB.prepare(
    `INSERT INTO job_results (run_id, key, job_id, value, created_at)
     SELECT j.run_id, json_extract(e.value, '$[0]'), j.id, json_extract(e.value, '$[1]'), ?9 FROM jobs j, json_each(?8) e
     WHERE ${guard}
     ON CONFLICT (run_id, key) DO UPDATE SET job_id = excluded.job_id, value = excluded.value, created_at = excluded.created_at`,
  ).bind(...binds, ...Array(7 - binds.length).fill(null), JSON.stringify(rows), at);

/**
 * 1 when the job j is a background job a foreground job is ready to go before at `now` (the statement's parameter
 * for it), the budget's level read in the statement; the CASE runs the subquery for a background job only.
 */
const giveWay = (now: string) => `CASE WHEN j.kind IN ${BACKGROUND} THEN ${foregroundReady(now, "j.lane", "j.heavy", BLOCKED_NOW)} ELSE 0 END`;

/**
 * POST /api/job/checkpoint {job_id, lease_token, cursor?, results?, news?}: what a step found so far, saved while
 * its lease holds (409 lost otherwise): results upserted on (run_id, key), news searches on their key (the day
 * and time set here), and the cursor, when sent. A step killed after this loses only the item in flight. The
 * answer is 204, or 200 {"give_way": true} for a background job a foreground job is ready to go before (its lane,
 * or the heavy slot): the step then stops at its next safe point, as partial.
 */
export async function checkpoint(ctx: Ctx): Promise<Response> {
  const { req, env } = ctx;
  const body = await readBody(req, L.checkpoint_body_bytes);
  const id = jobId(body.job_id);
  const token = leaseToken(body.lease_token);
  const results = resultRows(body.results);
  const news = newsRows(body.news);
  const hasCursor = "cursor" in body;
  const cursor = hasCursor && body.cursor !== null ? jsonText(body.cursor, "cursor", L.cursor_bytes) : null;
  const at = await ctx.clock();
  const live = "j.id = ?1 AND j.lease_token = ?2 AND j.state = 'leased' AND j.lease_until > ?3";
  const [check] = await env.DB.batch([
    env.DB.prepare(`SELECT 1 AS ok, ${giveWay("?3")} AS give_way FROM jobs j WHERE ${live}`).bind(id, token, at),
    ...(hasCursor
      ? [env.DB.prepare(`UPDATE jobs SET cursor = ?4, updated_at = ?3 WHERE id = (SELECT j.id FROM jobs j WHERE ${live})`).bind(id, token, at, cursor)]
      : []),
    ...(results.length ? [putResults(env, live, [id, token, at], results, at)] : []),
    ...(news.length
      ? [
          env.DB.prepare(
            `INSERT INTO news_cache (key, day, fetched_at, body)
             SELECT json_extract(e.value, '$[0]'), ?4, ?3, json_extract(e.value, '$[1]') FROM jobs j, json_each(?5) e
             WHERE ${live}
             ON CONFLICT (key) DO UPDATE SET day = excluded.day, fetched_at = excluded.fetched_at, body = excluded.body`,
          ).bind(id, token, at, toronto(at).day, JSON.stringify(news)),
        ]
      : []),
  ]);
  if (!check.results.length) fail(409, "lost", "The job's lease is gone.");
  return (check.results[0] as { give_way: number }).give_way ? json({ give_way: true }) : noContent();
}

// ---------- complete ----------

interface JobRow {
  id: string;
  run_id: string;
  kind: JobKind;
  lane: Lane;
  state: string;
  lease_token: string | null;
  attempt: number;
  laters: number;
  partials: number;
  error: string | null;
  /** 1 when it is a background job a foreground job is ready to go before (giveWay). */
  give_way: number;
}

/** A spawned job, checked against the contract: its kind's lane, heavy, need_s and final, and its run's priority. */
function childRows(v: unknown, runKind: RunKind, at: number) {
  return list(v ?? [], "children", L.children_max).map((c, i) => {
    const o = obj(c, `children[${i}]`);
    const kind = text(o.kind, `children[${i}].kind`, 40);
    if (!isJobKind(kind)) fail(400, "bad_request", `children[${i}].kind is not a job kind.`);
    const part = text(o.part, `children[${i}].part`, 64);
    if (!PART.test(part)) fail(400, "bad_request", `children[${i}].part must be letters, digits and . : _ - only.`);
    const after = list(o.after ?? [], `children[${i}].after`, 20).map((a, j) => {
      if (typeof a !== "string" || !isJobKind(a)) fail(400, "bad_request", `children[${i}].after[${j}] is not a job kind.`);
      return a;
    });
    const k = JOB_KINDS[kind];
    const runAfter = o.run_after === undefined || o.run_after === null ? at : Math.min(Math.max(int(o.run_after, `children[${i}].run_after`), at), at + MAX_LATER_S);
    return {
      kind,
      part,
      lane: k.lane,
      heavy: k.heavy ? 1 : 0,
      need_s: k.need_s,
      final: k.final ? 1 : 0,
      priority: C.run_priority[runKind],
      after: after.join(","),
      run_after: runAfter,
      input: jsonText(o.input === undefined || o.input === null ? {} : obj(o.input, `children[${i}].input`), `children[${i}].input`, L.input_bytes),
    };
  });
}

/**
 * POST /api/job/complete: the end of a step. done (with its results, spawned children, run_meta, or stop_run);
 * partial (queued again at once with its cursor; past partials_max: failed; a background job's partial sent with
 * gave_way while a foreground job is ready is not counted: it stopped for that job, not for want of time); later
 * (queued at run_after, at most two days on, the lane paused when asked; past laters_max: failed); error (retried
 * with back-off; at max_attempts: failed). One D1 batch: the job's update guarded by its lease token, then
 * everything else guarded by the nonce that update wrote (so two copies of one complete count once), the ledger,
 * and the run's finish. A repeat (the job no longer leased, the same token) is 200 repeated and changes nothing;
 * another token is 409 lost. Lanes left with a ready job are woken after the answer.
 */
export async function complete(ctx: Ctx): Promise<Response> {
  const { req, env, waitUntil } = ctx;
  const body = await readBody(req, L.complete_body_bytes);
  const id = jobId(body.job_id);
  const token = leaseToken(body.lease_token);
  const outcome = oneOf(body.outcome, "outcome", C.outcomes);
  const results = resultRows(body.results);
  const usage = body.usage === undefined || body.usage === null ? {} : obj(body.usage, "usage");
  const cpu = whole(usage.cpu_ms, "usage.cpu_ms");
  const wall = whole(usage.wall_ms, "usage.wall_ms");
  const units = whole(usage.units, "usage.units");
  whole(usage.mem_mb, "usage.mem_mb");
  const runMeta = body.run_meta === undefined || body.run_meta === null ? null : jsonText(obj(body.run_meta, "run_meta"), "run_meta", L.run_meta_bytes);
  const stopRun = body.stop_run === true;
  const continuing = body.continuing === true;
  const gaveWay = body.gave_way === true;
  const at = await ctx.clock();

  const job = await env.DB.prepare(
    `SELECT id, run_id, kind, lane, state, lease_token, attempt, laters, partials, error, ${giveWay("?2")} AS give_way FROM jobs j WHERE id = ?1`,
  )
    .bind(id, at)
    .first<JobRow>();
  if (!job || job.lease_token !== token) fail(409, "lost", "The job's lease is gone.");
  if (job.state !== "leased") return json({ ok: true, state: job.state, repeated: true });
  const children = outcome === "done" ? childRows(body.children, runKindOf(job.run_id), at) : [];

  let state = "queued";
  let runAfter = at;
  let { attempt, laters, partials } = job;
  let error = job.error;
  let cursor: string | null = null;
  let setCursor = false;
  let pause: number | null = null;
  if (outcome === "done") {
    state = "done";
  } else if (outcome === "partial") {
    if (body.cursor === undefined || body.cursor === null) fail(400, "bad_request", "A partial step must send its cursor.");
    cursor = jsonText(body.cursor, "cursor", L.cursor_bytes);
    setCursor = true;
    if (!(gaveWay && job.give_way)) partials++;
    if (partials > C.retry.partials_max) {
      state = "failed";
      error = "no end in sight";
    }
  } else if (outcome === "later") {
    const reason = oneOf(body.reason, "reason", C.later_reasons);
    runAfter = Math.min(Math.max(int(body.run_after, "run_after"), at), at + MAX_LATER_S);
    if (body.pause_lane_until !== undefined && body.pause_lane_until !== null) {
      pause = Math.min(Math.max(int(body.pause_lane_until, "pause_lane_until"), at), at + MAX_LATER_S);
    }
    laters++;
    if (laters > C.retry.laters_max) {
      state = "failed";
      error = `waited ${C.retry.laters_max} times: ${reason}`;
    }
  } else {
    error = oneLine(text(body.error, "error", 20_000));
    attempt++;
    if (attempt >= C.retry.max_attempts) state = "failed";
    else runAfter = at + retryDelay(attempt);
  }

  const nonce = randomHex(16);
  const mine = "j.id = ?1 AND j.step_nonce = ?2";
  const ofMine = "EXISTS (SELECT 1 FROM jobs j WHERE j.id = ?1 AND j.step_nonce = ?2)";
  const day = toronto(at).day;
  const [moved] = await env.DB.batch([
    env.DB.prepare(
      `UPDATE jobs SET state = ?3, run_after = ?4, attempt = ?5, laters = ?6, partials = ?7,
         cursor = CASE WHEN ?8 = 1 THEN ?9 ELSE cursor END, error = ?10,
         cpu_ms = cpu_ms + ?11, wall_ms = wall_ms + ?12, updated_at = ?13, step_nonce = ?14
       WHERE id = ?1 AND lease_token = ?2 AND state = 'leased'`,
    ).bind(id, token, state, runAfter, attempt, laters, partials, setCursor ? 1 : 0, cursor, error, cpu, wall, at, nonce),
    ...(results.length ? [putResults(env, mine, [id, nonce], results, at)] : []),
    ...(children.length
      ? [
          env.DB.prepare(
            `INSERT OR IGNORE INTO jobs (id, run_id, kind, part, lane, heavy, need_s, final, priority, after, state, run_after, input, created_at, updated_at)
             SELECT j.run_id || '|' || json_extract(e.value, '$.kind') || '|' || json_extract(e.value, '$.part'), j.run_id,
                    json_extract(e.value, '$.kind'), json_extract(e.value, '$.part'), json_extract(e.value, '$.lane'),
                    json_extract(e.value, '$.heavy'), json_extract(e.value, '$.need_s'), json_extract(e.value, '$.final'),
                    json_extract(e.value, '$.priority'), json_extract(e.value, '$.after'), 'queued',
                    json_extract(e.value, '$.run_after'), json_extract(e.value, '$.input'), ?3, ?3
             FROM jobs j, json_each(?4) e WHERE ${mine}`,
          ).bind(id, nonce, at, JSON.stringify(children)),
        ]
      : []),
    ...(runMeta
      ? [
          env.DB.prepare(
            `UPDATE runs SET meta = json_patch(meta, ?3) WHERE id = ?4 AND ${ofMine} AND length(json_patch(meta, ?3)) <= ?5`,
          ).bind(id, nonce, runMeta, job.run_id, L.run_meta_bytes),
        ]
      : []),
    env.DB.prepare(
      `INSERT INTO usage (day, kind, steps, cpu_ms, gb_ms, wakes, units) SELECT ?3, ?4, 1, ?5, ?6, 0, ?7 WHERE ${ofMine}
       ON CONFLICT (day, kind) DO UPDATE SET steps = steps + 1, cpu_ms = cpu_ms + excluded.cpu_ms, gb_ms = gb_ms + excluded.gb_ms,
         units = units + excluded.units`,
    ).bind(id, nonce, day, job.kind, cpu, wall * C.step.instance_gb, units),
    ...(stopRun && outcome === "done"
      ? [
          env.DB.prepare(
            `UPDATE jobs SET state = 'cancelled', error = 'the run was stopped', updated_at = ?3
             WHERE run_id = ?4 AND id != ?1 AND state IN ('queued', 'leased') AND ${ofMine}`,
          ).bind(id, nonce, at, job.run_id),
          env.DB.prepare(`UPDATE runs SET state = 'cancelled', finished_at = ?3 WHERE id = ?4 AND state IN ('queued', 'running') AND ${ofMine}`).bind(
            id,
            nonce,
            at,
            job.run_id,
          ),
        ]
      : []),
    ...(pause !== null
      ? [env.DB.prepare(`UPDATE lanes SET paused_until = max(paused_until, ?3) WHERE lane = ?4 AND ${ofMine}`).bind(id, nonce, pause, job.lane)]
      : []),
    finishRuns(env, at, job.run_id),
  ]);
  if (!moved.meta.changes) {
    // another copy of this complete got there first
    const now = await env.DB.prepare("SELECT state, lease_token FROM jobs WHERE id = ?").bind(id).first<{ state: string; lease_token: string }>();
    if (now?.lease_token === token && now.state !== "leased") return json({ ok: true, state: now.state, repeated: true });
    fail(409, "lost", "The job's lease is gone.");
  }
  waitUntil(wakeReady(env, at, { except: continuing ? job.lane : undefined }));
  return json({ ok: true, state, repeated: false });
}

/** An error's retry wait (attempt is the new count, 1 and up): min(30 × 2^(attempt−1), 1800) plus up to 15 s. */
export const retryDelay = (attempt: number) =>
  Math.min(C.retry.base_s * 2 ** Math.max(attempt - 1, 0), C.retry.cap_s) + Math.floor(Math.random() * (C.retry.jitter_s + 1));

// ---------- reads ----------

/**
 * GET /api/job/results?run=&prefix=&after=: a run's results by key, at most 500 rows or about 1 MB a page (the
 * stored values are spliced into the answer as they are, never parsed here). next: the key to ask after, or null.
 */
export async function results({ env, url }: Ctx): Promise<Response> {
  const run = text(url.searchParams.get("run"), "run", 300);
  const prefix = url.searchParams.get("prefix") ?? "";
  if (prefix.length > L.result_key_chars) fail(400, "bad_request", "prefix is too long.");
  const after = url.searchParams.get("after") ?? "";
  const rows = await env.DB.prepare(
    `SELECT key, job_id, value, n FROM (
       SELECT key, job_id, value, count(*) OVER () AS n,
              sum(length(value)) OVER (ORDER BY key ROWS UNBOUNDED PRECEDING) - length(value) AS before
       FROM (SELECT key, job_id, value FROM job_results
             WHERE run_id = ?1 AND key > ?2 AND key >= ?3 AND substr(key, 1, length(?3)) = ?3 ORDER BY key LIMIT ?4))
     WHERE before < ?5 ORDER BY key`,
  )
    .bind(run, after, prefix, L.results_page_rows + 1, L.results_page_bytes)
    .all<{ key: string; job_id: string; value: string; n: number }>();
  let got = rows.results;
  const total = got[0]?.n ?? 0;
  let next: string | null = null;
  if (got.length > L.results_page_rows) got = got.slice(0, L.results_page_rows);
  if (got.length < total) next = got[got.length - 1].key;
  const items = got.map((r) => `{"key":${JSON.stringify(r.key)},"job_id":${JSON.stringify(r.job_id)},"value":${r.value}}`);
  return new Response(`{"results":[${items.join(",")}],"next":${JSON.stringify(next)}}`, {
    headers: { "content-type": "application/json; charset=utf-8", "cache-control": "no-store" },
  });
}

/**
 * POST /api/job/news/get {keys, max_age_s}: the stored searches for up to 50 keys, fetched within max_age_s.
 * Keys not found or older are absent; ones left out to keep the answer under 2 MB are named in `more`.
 */
export async function newsGet(ctx: Ctx): Promise<Response> {
  const { req, env } = ctx;
  const body = await readBody(req, 64 * 1024);
  const keys = list(body.keys, "keys", L.news_get_keys).map((k, i) => text(k, `keys[${i}]`, L.news_key_chars));
  const maxAge = whole(body.max_age_s, "max_age_s");
  const at = await ctx.clock();
  const sizes = await env.DB.prepare(
    "SELECT key, length(body) AS n FROM news_cache WHERE key IN (SELECT value FROM json_each(?1)) AND fetched_at >= ?2 ORDER BY key",
  )
    .bind(JSON.stringify(keys), at - maxAge)
    .all<{ key: string; n: number }>();
  const take: string[] = [];
  const more: string[] = [];
  let size = 0;
  for (const r of sizes.results) {
    if (take.length && size + r.n > L.news_get_bytes) more.push(r.key);
    else {
      take.push(r.key);
      size += r.n;
    }
  }
  const rows = take.length
    ? (
        await env.DB.prepare("SELECT key, day, fetched_at, body FROM news_cache WHERE key IN (SELECT value FROM json_each(?1))")
          .bind(JSON.stringify(take))
          .all<{ key: string; day: string; fetched_at: number; body: string }>()
      ).results
    : [];
  const items = rows.map(
    (r) => `${JSON.stringify(r.key)}:{"day":${JSON.stringify(r.day)},"fetched_at":${r.fetched_at},"body":${r.body}}`,
  );
  return new Response(`{"news":{${items.join(",")}},"more":${JSON.stringify(more)}}`, {
    headers: { "content-type": "application/json; charset=utf-8", "cache-control": "no-store" },
  });
}

/** GET /api/job/run?id=: a run and up to 500 of its jobs (no inputs or cursors). */
export async function getRun({ env, url }: Ctx): Promise<Response> {
  const id = text(url.searchParams.get("id"), "id", 300);
  const [runRows, jobs] = await env.DB.batch([
    env.DB.prepare("SELECT id, kind, slot, state, meta, created_at, started_at, deadline, finished_at FROM runs WHERE id = ?").bind(id),
    env.DB.prepare(
      "SELECT id, kind, part, state, attempt, laters, partials, error, cpu_ms, wall_ms FROM jobs WHERE run_id = ? ORDER BY created_at, id LIMIT 500",
    ).bind(id),
  ]);
  const run = runRows.results[0] as Record<string, unknown> | undefined;
  if (!run) fail(404, "no_run", "No such run.");
  return json({ run: { ...run, meta: parsed(run.meta as string, {}) }, jobs: jobs.results });
}
