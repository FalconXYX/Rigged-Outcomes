// The one Cron Trigger (* * * * *, docs/BACKEND.md). Cloudflare runs it in UTC; each tick
// converts the Worker's clock to Toronto time and does what is due, so a late or missed tick is picked up by the
// next one: the daily run from 04:00 (and a 'missed' row for a day no tick made one), a catch-up from 10:00 to
// 12:00 when the daily run left work, the budget every 10 minutes, the prune once a day from 03:30, and on every
// tick the watchdog (expired leases, run deadlines, finished runs, stuck adds) and the wake-ups. A tick stays a
// few D1 batches and at most wakes_per_tick fetches, inside the free plan's 10 ms of CPU.

import { CONTRACT, dayBefore, hhmm, toronto, torontoAt } from "./contract";
import { pruneParts } from "./blobs";
import { pruneQueue } from "./job";
import { type Budget, capsOf, computeBudget, readBudget } from "./ledger";
import { type Ctx, DAY, type Env, clockOf, daysBefore, int, json, now, readBody } from "./lib";
import { finishRuns, makeRun, runId } from "./queue";
import { pickLanes, sendWakes } from "./wake";

const S = CONTRACT.schedule;
const R = CONTRACT.retry;
const B = CONTRACT.budget;
const P = CONTRACT.prune;
const TERMINAL = `('${CONTRACT.run_terminal.join("','")}')`;

export interface TickReport {
  created: string[];
  woke: string[];
  expired: number;
}

/** The ids of the rows a statement's RETURNING gave. */
const ids = (r: D1Result | undefined) => ((r?.results ?? []) as { id: string }[]).map((x) => x.id);

/**
 * A new daily run takes over from older daily and catch-up runs still queued or running: they are cancelled with
 * their unfinished jobs, so backed-up days never run all at once. Only when `keep` was made in this batch.
 */
function supersede(env: Env, keep: string, at: number): D1PreparedStatement[] {
  const older = "SELECT id FROM runs WHERE kind IN ('daily', 'catchup') AND state IN ('queued', 'running') AND id != ?1";
  const fresh = "EXISTS (SELECT 1 FROM runs WHERE id = ?1 AND created_at = ?2)";
  return [
    env.DB.prepare(
      `UPDATE jobs SET state = 'cancelled', error = 'replaced by a newer run', updated_at = ?2
       WHERE state IN ('queued', 'leased') AND run_id IN (${older}) AND ${fresh}`,
    ).bind(keep, at),
    env.DB.prepare(`UPDATE runs SET state = 'cancelled', finished_at = ?2 WHERE id IN (${older}) AND ${fresh}`).bind(keep, at),
  ];
}

/** An approved add with no live run: its run ended (the add is failed, "stopped part way"), or it never had one (it gets one). */
interface Stuck {
  condition_id: string;
  decided_at: number | null;
  requested_at: number;
  run_id: string | null;
  run_state: string | null;
}

/** One tick: the schedule, the watchdog and the wake-ups (the scheduled handler, and POST /api/dev/tick). */
export async function tick(env: Env, waitUntil: (p: Promise<unknown>) => void): Promise<TickReport> {
  const at = await clockOf(env);
  const { day, minutes } = toronto(at);
  const yesterday = dayBefore(day);
  const [settingsRows, todays, stuckRows] = await env.DB.batch([
    env.DB.prepare("SELECT key, value FROM settings WHERE key IN ('budget', 'pruned_day')"),
    env.DB.prepare("SELECT id, kind, state, meta FROM runs WHERE id IN (?1, ?2)").bind(runId("daily", day), runId("catchup", day)),
    env.DB.prepare(
      `SELECT q.condition_id, q.decided_at, q.requested_at, q.run_id, r.state AS run_state
       FROM requests q LEFT JOIN runs r ON r.id = q.run_id
       WHERE q.status = 'approved' AND (r.id IS NULL OR r.state IN ${TERMINAL}) ORDER BY q.decided_at LIMIT 5`,
    ),
  ]);
  const kept = Object.fromEntries((settingsRows.results as { key: string; value: string }[]).map((r) => [r.key, r.value]));
  let budget: Budget = readBudget(kept.budget);
  if (at - budget.computed_at >= S.budget_every_min * 60) budget = await computeBudget(env, at);
  const level = budget.level;
  const blockedNow = (B.blocked_kinds[level] as readonly string[]).filter((k) => k !== "*" && !k.startsWith("add."));
  const runs = Object.fromEntries((todays.results as { id: string; kind: string; state: string; meta: string }[]).map((r) => [r.kind, r]));

  const statements: D1PreparedStatement[] = [];
  const made: [number, string][] = []; // [statement index, run id] of each run this tick may make
  const make = (s: D1PreparedStatement[], id: string) => {
    made.push([statements.length, id]);
    statements.push(...s);
  };

  // a day no tick made a daily run for leaves a record (once the backend has run at all: not the day before the first tick)
  make(
    [
      env.DB.prepare(
        `INSERT INTO runs (id, kind, slot, state, meta, created_at, deadline, finished_at)
         SELECT ?1, 'daily', ?2, 'missed', '{}', ?3, ?3, ?3 WHERE EXISTS (SELECT 1 FROM runs)
         ON CONFLICT DO NOTHING`,
      ).bind(runId("daily", yesterday), yesterday, at),
    ],
    runId("daily", yesterday),
  );

  // the daily run, from 04:00 Toronto
  if (minutes >= hhmm(S.daily_at) && !runs.daily) {
    const id = runId("daily", day);
    const deadline = Math.max(torontoAt(day, S.daily_deadline), at + S.daily_min_run_s);
    make(makeRun(env, { kind: "daily", slot: day, at, deadline, state: level === "stop" ? "skipped" : "queued" }), id);
    statements.push(...supersede(env, id, at));
  }

  // the catch-up, from 10:00 to 12:00, once, when the day's run finished with stories left and the budget allows it
  const daily = runs.daily;
  const left = Number((JSON.parse(daily?.meta || "{}") as { left?: unknown }).left) || 0;
  if (
    minutes >= hhmm(S.catchup_from) &&
    minutes < hhmm(S.catchup_until) &&
    daily &&
    ["done", "partial", "failed"].includes(daily.state) &&
    left > 0 &&
    capsOf(level).catchup &&
    !runs.catchup
  ) {
    const input = { catch_up: true, from_run: daily.id };
    make(makeRun(env, { kind: "catchup", slot: day, at, deadline: at + S.catchup_run_s, input }), runId("catchup", day));
  }

  // the watchdog: a lease past its time is an error ("the step stopped"), retried with back-off or failed. The
  // step sent no usage (killed at the platform's limit, out of memory, or its complete lost), so the ledger is
  // charged what such a step can use: the function's whole 300 s on the 2 GB instance, and half of it as CPU
  const T = CONTRACT.step;
  statements.push(
    env.DB.prepare(
      `INSERT INTO usage (day, kind, steps, cpu_ms, gb_ms, wakes, units)
       SELECT ?2, kind, count(*), count(*) * ?3, count(*) * ?4, 0, 0 FROM jobs WHERE state = 'leased' AND lease_until < ?1 GROUP BY kind
       ON CONFLICT (day, kind) DO UPDATE SET steps = steps + excluded.steps, cpu_ms = cpu_ms + excluded.cpu_ms, gb_ms = gb_ms + excluded.gb_ms`,
    ).bind(at, day, T.killed_cpu_s * 1000, T.killed_wall_s * 1000 * T.instance_gb),
  );
  const expireAt = statements.length;
  statements.push(
    env.DB.prepare(
      `UPDATE jobs SET attempt = attempt + 1, error = 'the step stopped (lease expired)', updated_at = ?1,
         state = CASE WHEN attempt + 1 >= ?2 THEN 'failed' ELSE 'queued' END,
         run_after = ?1 + min(?3 * (1 << min(attempt, 20)), ?4) + abs(random() % (?5 + 1))
       WHERE state = 'leased' AND lease_until < ?1 RETURNING id`,
    ).bind(at, R.max_attempts, R.base_s, R.cap_s, R.jitter_s),
    // a daily or catch-up job of a kind the budget's level blocks is cancelled at once, so the jobs after it (the
    // "already out" check, the lists) go ahead on what is there; an add's wait for the budget instead (its 36 h)
    ...(blockedNow.length
      ? [
          env.DB.prepare(
            `UPDATE jobs SET state = 'cancelled', error = 'budget', updated_at = ?1
             WHERE state = 'queued' AND kind IN (SELECT value FROM json_each(?2))
               AND run_id IN (SELECT id FROM runs WHERE kind IN ('daily', 'catchup') AND state IN ('queued', 'running'))`,
          ).bind(at, JSON.stringify(blockedNow)),
        ]
      : []),
    // past a run's deadline its queued jobs that are not final are cancelled; the finals then publish what is there
    env.DB.prepare(
      `UPDATE jobs SET state = 'cancelled', error = 'run deadline', updated_at = ?1
       WHERE state = 'queued' AND final = 0 AND run_id IN (SELECT id FROM runs WHERE state IN ('queued', 'running') AND deadline < ?1)`,
    ).bind(at),
    // ...and a final still unfinished an hour after it has failed
    env.DB.prepare(
      `UPDATE jobs SET state = 'failed', error = 'unfinished an hour past the run''s deadline', updated_at = ?1
       WHERE state IN ('queued', 'leased') AND final = 1
         AND run_id IN (SELECT id FROM runs WHERE state IN ('queued', 'running') AND deadline + ?2 < ?1)`,
    ).bind(at, S.final_grace_s),
    finishRuns(env, at),
  );

  // adds left approved with no live run: mended, at most 5 a tick
  for (const q of stuckRows.results as Stuck[]) {
    if (q.run_id && q.run_state) {
      statements.push(
        env.DB.prepare(
          "UPDATE requests SET status = 'failed', note = 'stopped part way', done_at = ?1 WHERE condition_id = ?2 AND status = 'approved' AND run_id = ?3",
        ).bind(at, q.condition_id, q.run_id),
      );
    } else {
      const slot = `${q.condition_id}:${q.decided_at ?? q.requested_at}`;
      const id = runId("add", slot);
      make(makeRun(env, { kind: "add", slot, at, deadline: at + S.add_run_s, meta: { condition_id: q.condition_id } }), id);
      statements.push(
        env.DB.prepare("UPDATE requests SET run_id = ?1 WHERE condition_id = ?2 AND status = 'approved'").bind(id, q.condition_id),
      );
    }
  }

  const out = await env.DB.batch(statements);
  const created = made.filter(([i]) => out[i]?.meta.changes).map(([, id]) => id);
  const expired = ids(out[expireAt]).length;

  // the wake-ups (none at level stop)
  const woke = await pickLanes(env, at);
  if (woke.length) waitUntil(sendWakes(env, woke, at));

  if (minutes >= hhmm(S.prune_at) && kept.pruned_day !== day) await prune(env, at, day);
  return { created, woke, expired };
}

/**
 * The daily prune, one batch (docs/BACKEND.md): finished runs with their jobs and results after 30
 * days, terminal jobs after 14, results, news searches and scratch blobs after 3, the ledger after 60 days, the
 * daily limits after 3, orphan blob parts after one; and what the run's report used to prune: job_runs beyond
 * 200, deleted markers and visitor hashes after 30 days, watched stories a year old that Open now no longer
 * lists, withdrawals no run reported within 30 days, queue days beyond 7.
 */
export async function prune(env: Env, at: number, day: string): Promise<void> {
  const oldRuns = `SELECT id FROM runs WHERE state IN ${TERMINAL} AND coalesce(finished_at, created_at) < ?1`;
  const scratch = "name > 'scratch:' AND name < 'scratch;'";
  await env.DB.batch([
    env.DB.prepare(`DELETE FROM job_results WHERE run_id IN (${oldRuns})`).bind(at - P.runs_days * DAY),
    env.DB.prepare(`DELETE FROM jobs WHERE run_id IN (${oldRuns})`).bind(at - P.runs_days * DAY),
    env.DB.prepare(`DELETE FROM runs WHERE id IN (${oldRuns})`).bind(at - P.runs_days * DAY),
    env.DB.prepare("DELETE FROM jobs WHERE state IN ('done', 'failed', 'cancelled') AND updated_at < ?").bind(at - P.jobs_days * DAY),
    env.DB.prepare("DELETE FROM job_results WHERE created_at < ?").bind(at - P.results_days * DAY),
    env.DB.prepare("DELETE FROM news_cache WHERE fetched_at < ?").bind(at - P.news_days * DAY),
    env.DB.prepare("DELETE FROM usage WHERE day < ?").bind(daysBefore(day, P.usage_days)),
    env.DB.prepare("DELETE FROM quota WHERE day < ?").bind(daysBefore(day, P.quota_days)),
    env.DB.prepare(`DELETE FROM job_blob_parts WHERE name IN (SELECT name FROM job_blobs WHERE ${scratch} AND updated_at < ?)`).bind(
      at - P.scratch_days * DAY,
    ),
    env.DB.prepare(`DELETE FROM job_blobs WHERE ${scratch} AND updated_at < ?`).bind(at - P.scratch_days * DAY),
    pruneParts(env, at),
    env.DB.prepare("DELETE FROM job_runs WHERE id NOT IN (SELECT id FROM job_runs ORDER BY id DESC LIMIT 200)"),
    env.DB.prepare("DELETE FROM markers WHERE deleted_at IS NOT NULL AND deleted_at < ?").bind(at - 30 * DAY),
    env.DB.prepare("UPDATE markers SET visitor = NULL WHERE visitor IS NOT NULL AND created_at < ?").bind(at - 30 * DAY),
    env.DB.prepare("DELETE FROM watches WHERE created_at < ? AND story NOT IN (SELECT id FROM catalog WHERE kind = 'open')").bind(
      at - 365 * DAY,
    ),
    env.DB.prepare("DELETE FROM open_decisions WHERE decision = 'withdrawn' AND decided_at < ?").bind(at - 30 * DAY),
    pruneQueue(env),
    env.DB.prepare(
      "INSERT INTO settings (key, value, updated_at) VALUES ('pruned_day', ?1, ?2) ON CONFLICT (key) DO UPDATE SET value = ?1, updated_at = ?2",
    ).bind(day, at),
  ]);
}

// ---------- development only (DEV=on) ----------

/** POST /api/dev/clock {"now": epoch | null}: move the Worker's clock (settings.dev_offset); null puts it back. */
export async function devClock({ req, env }: Ctx): Promise<Response> {
  const body = await readBody(req, 1024);
  const to = body.now === null || body.now === undefined ? null : int(body.now, "now");
  if (to === null) await env.DB.prepare("DELETE FROM settings WHERE key = 'dev_offset'").run();
  else {
    await env.DB.prepare(
      "INSERT INTO settings (key, value, updated_at) VALUES ('dev_offset', ?1, ?2) ON CONFLICT (key) DO UPDATE SET value = ?1, updated_at = ?2",
    )
      .bind(String(to - now()), now())
      .run();
  }
  return json({ now: await clockOf(env) });
}

/** POST /api/dev/tick: run the scheduled handler now; answers what it made, woke and expired. */
export async function devTick({ env, waitUntil }: Ctx): Promise<Response> {
  return json(await tick(env, waitUntil));
}
