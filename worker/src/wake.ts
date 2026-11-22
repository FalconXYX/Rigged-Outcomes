// Waking the step function (docs/BACKEND.md): the Worker sends a signed POST to Vercel's /api/step
// naming one lane, and the call it starts claims that lane's jobs from /api/job/claim itself. A lane is woken
// only while it has a job ready to claim and no live lease, never while paused or blocked by the budget, and with
// back-off: each wake-up sent adds one to lanes.unanswered and any claim sets it back to 0, so a step function
// that is down or refusing is asked at most every 1, 2, 4, ... 60 minutes rather than every tick.

import { BACKGROUND_KINDS, type BudgetLevel, CONTRACT, type Lane, toronto } from "./contract";
import { addUsage, blockedText, isBlocked, readBudget } from "./ledger";
import { type Env, dev, log, now, randomHex } from "./lib";

const W = CONTRACT.wake;
const enc = new TextEncoder();

/** The wake-up's signature: lowercase hex HMAC-SHA256(secret, "v1|<ts>|<lane>|<nonce>"). */
export async function sign(secret: string, ts: number, lane: string, nonce: string): Promise<string> {
  const key = await crypto.subtle.importKey("raw", enc.encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const mac = await crypto.subtle.sign("HMAC", key, enc.encode(`${W.scheme}|${ts}|${lane}|${nonce}`));
  return [...new Uint8Array(mac)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

/** The x-ro-wake header for a lane: "v1,<ts>,<lane>,<nonce>,<sig>", ts the real epoch seconds (Vercel checks it against its own clock). */
export async function wakeHeader(secret: string, lane: string, ts = now(), nonce = randomHex(8)): Promise<string> {
  return `${W.scheme},${ts},${lane},${nonce},${await sign(secret, ts, lane, nonce)}`;
}

/**
 * Where wake-ups go: STEP_URL, an https:// address in production; with DEV=on only a local one (127.0.0.1 or
 * localhost), so a local Worker never wakes the real step function. Null: no wake-ups (nor without WAKE_SECRET).
 */
export function stepUrl(env: Env): string | null {
  if (!env.STEP_URL || !env.WAKE_SECRET) return null;
  try {
    const u = new URL(env.STEP_URL);
    const local = u.hostname === "127.0.0.1" || u.hostname === "localhost";
    return (dev(env) ? local && u.protocol === "http:" : u.protocol === "https:") ? u.toString() : null;
  } catch {
    return null;
  }
}

/** Seconds a lane waits after its last wake-up before the next, with n of them unanswered: 60 × 2^(n−1), at most an hour. */
export const backoff = (unanswered: number) => (unanswered <= 0 ? 0 : Math.min(W.backoff_base_s * 2 ** Math.min(unanswered - 1, 20), W.backoff_max_s));

/** A contract constant as an SQL string literal, quoted in place. */
const sqlText = (s: string) => `'${s.replace(/'/g, "''")}'`;

/** The background kinds as an SQL list, "('wallets.score')". */
export const BACKGROUND = `(${BACKGROUND_KINDS.map(sqlText).join(", ")})`;

/** A level's WHEN of BLOCKED_NOW: its blocked text, NULL at stop; none for a level that blocks nothing. */
const blockedWhen = (level: BudgetLevel) => {
  const t = blockedText(level);
  return t === "" ? "" : `WHEN ${sqlText(level)} THEN ${t === null ? "NULL" : sqlText(t)}`;
};

/**
 * The claim's blocked text (blockedText, ',kind,kind,') for the level settings.budget holds, worked out inside a
 * statement: '' when nothing is blocked (no row, or a value readBudget reads as ok), NULL at stop (every kind).
 */
export const BLOCKED_NOW = `(CASE (SELECT json_extract(s.value, '$.level') FROM settings s WHERE s.key = 'budget' AND json_valid(s.value))
      ${CONTRACT.budget.levels.map(blockedWhen).filter(Boolean).join(" ")} ELSE '' END)`;

/**
 * "A foreground job is ready" for a background job whose lane and heavy are the SQL expressions `lane` and `heavy`,
 * at `now` (the statement's own parameter for it), `blocked` the budget level's blocked text (the claim's own, else
 * BLOCKED_NOW): a job of a kind not in BACKGROUND and not blocked, queued, due, its run live (a final, or before the
 * run's deadline), its barrier clear (the claim's test) and its lane not paused, in that lane, or heavy when the
 * background job is. need_s and live leases don't count (a call with the time comes, a lease ends); a blocked kind
 * can't run before the level drops, so the scoring goes on while an add waits for the budget (up to its 36 h). The
 * claim, the wake-ups and the checkpoint all use this test, so they agree.
 */
export const foregroundReady = (now: string, lane: string, heavy: string, blocked: string) => `EXISTS (SELECT 1 FROM jobs f JOIN runs fr ON fr.id = f.run_id
      WHERE f.state = 'queued' AND f.run_after <= ${now} AND f.kind NOT IN ${BACKGROUND} AND instr(${blocked}, ',' || f.kind || ',') = 0
        AND fr.state IN ('queued', 'running') AND (f.final = 1 OR fr.deadline > ${now})
        AND NOT EXISTS (SELECT 1 FROM jobs fd WHERE fd.run_id = f.run_id AND fd.state IN ('queued', 'leased')
                        AND instr(',' || f.after || ',', ',' || fd.kind || ',') > 0)
        AND NOT EXISTS (SELECT 1 FROM lanes fl WHERE fl.lane = f.lane AND fl.paused_until > ${now})
        AND (f.lane = ${lane} OR (${heavy} = 1 AND f.heavy = 1)))`;

/**
 * The lanes with a job ready to claim (the claim's own conditions: queued, due, its run live, its barrier clear,
 * no live lease in its lane, the heavy rule, a background job only while no foreground job is ready for it) and the
 * kinds ready in each, for the budget's blocked kinds.
 */
const READY = `SELECT j.lane AS lane, group_concat(DISTINCT j.kind) AS kinds FROM jobs j JOIN runs r ON r.id = j.run_id
  WHERE j.state = 'queued' AND j.run_after <= ?1 AND r.state IN ('queued', 'running') AND (j.final = 1 OR r.deadline > ?1)
    AND NOT EXISTS (SELECT 1 FROM jobs d WHERE d.run_id = j.run_id AND d.state IN ('queued', 'leased')
                    AND instr(',' || j.after || ',', ',' || d.kind || ',') > 0)
    AND NOT EXISTS (SELECT 1 FROM jobs l WHERE l.state = 'leased' AND l.lane = j.lane AND l.lease_until > ?1)
    AND (j.heavy = 0 OR NOT EXISTS (SELECT 1 FROM jobs h WHERE h.state = 'leased' AND h.heavy = 1 AND h.lease_until > ?1))
    AND (j.kind NOT IN ${BACKGROUND} OR NOT ${foregroundReady("?1", "j.lane", "j.heavy", BLOCKED_NOW)})
  GROUP BY j.lane`;

/**
 * Take a lane's turn to be woken, in one statement so two senders never both wake it: not paused, and either
 * nothing unanswered, a handler's wake-up while fewer than handler_free_wakes are unanswered (a visitor's click
 * goes out at once), or the back-off has passed. It adds one to unanswered.
 */
const TURN = `UPDATE lanes SET unanswered = unanswered + 1, last_wake_at = ?1
  WHERE lane = ?2 AND paused_until <= ?1
    AND (unanswered = 0 OR (?3 = 1 AND unanswered < ?4)
         OR ?1 >= last_wake_at + min(?5 * (1 << min(unanswered - 1, 20)), ?6))
  RETURNING lane`;

export interface WakeOpts {
  /** Only these lanes (a handler's own lane). */
  only?: readonly Lane[];
  /** Not this lane (the caller of /complete that claims again itself). */
  except?: Lane;
  /** A visitor's action: goes out at once while fewer than handler_free_wakes are unanswered. */
  handler?: boolean;
}

/** Pick the lanes to wake now and take their turn (two D1 queries). Nothing without a step URL (stepUrl), or at budget level stop. */
export async function pickLanes(env: Env, at: number, opts: WakeOpts = {}): Promise<Lane[]> {
  if (!stepUrl(env)) return [];
  const [b, ready] = await env.DB.batch([
    env.DB.prepare("SELECT value FROM settings WHERE key = 'budget'"),
    env.DB.prepare(READY).bind(at),
  ]);
  const level = readBudget((b.results[0] as { value?: string } | undefined)?.value).level;
  if (level === "stop") return [];
  const lanes = (ready.results as { lane: Lane; kinds: string }[])
    .filter((r) => (!opts.only || opts.only.includes(r.lane)) && r.lane !== opts.except)
    .filter((r) => r.kinds.split(",").some((k) => !isBlocked(level, k as never)))
    .map((r) => r.lane)
    .slice(0, CONTRACT.limits.wakes_per_tick);
  if (!lanes.length) return [];
  const turns = await env.DB.batch(
    lanes.map((lane) =>
      env.DB.prepare(TURN).bind(at, lane, opts.handler ? 1 : 0, W.handler_free_wakes, W.backoff_base_s, W.backoff_max_s),
    ),
  );
  return turns.flatMap((t) => (t.results as { lane: Lane }[]).map((r) => r.lane));
}

/**
 * Send the wake-ups, at once, each aborted after abort_ms, with WAKE_GATE as x-ro-gate when it is set (Vercel's
 * firewall turns away any /api/step request without it, before the function starts). Python keeps running after
 * its caller leaves, so an abort means the call is running and counts as delivered, as a 2xx does. Any other
 * status (401, 403, 404, 503 DEPLOYMENT_PAUSED) or no answer is kept in lanes.wake_error until one goes through.
 * Each delivered one is an invocation in the ledger (usage(today, '_wake').wakes).
 */
export async function sendWakes(env: Env, lanes: Lane[], at: number): Promise<void> {
  const url = stepUrl(env);
  if (!lanes.length || !url) return;
  const outcomes = await Promise.all(
    lanes.map(async (lane): Promise<[Lane, string | null]> => {
      try {
        const res = await fetch(url, {
          method: "POST",
          headers: {
            [W.header]: await wakeHeader(env.WAKE_SECRET!, lane),
            ...(env.WAKE_GATE ? { [W.gate_header]: env.WAKE_GATE } : {}),
            "user-agent": "rigged-outcomes-api",
          },
          signal: AbortSignal.timeout(W.abort_ms),
        });
        await res.body?.cancel();
        return [lane, res.ok ? null : `HTTP ${res.status}`];
      } catch (e) {
        const name = (e as { name?: string })?.name;
        return [lane, name === "TimeoutError" || name === "AbortError" ? null : "no answer"];
      }
    }),
  );
  const delivered = outcomes.filter(([, err]) => err === null).length;
  try {
    await env.DB.batch([
      ...outcomes.map(([lane, err]) => env.DB.prepare("UPDATE lanes SET wake_error = ? WHERE lane = ?").bind(err, lane)),
      ...(delivered ? [addUsage(env, toronto(at).day, "_wake", { wakes: delivered })] : []),
    ]);
  } catch (e) {
    log({ event: "wake_record_failed", error: String(e) });
  }
  for (const [lane, err] of outcomes) if (err) log({ event: "wake_failed", lane, error: err });
}

/** Pick and send in one go (a handler's waitUntil, or /complete's); returns the lanes woken. */
export async function wakeReady(env: Env, at: number, opts: WakeOpts = {}): Promise<Lane[]> {
  try {
    const lanes = await pickLanes(env, at, opts);
    await sendWakes(env, lanes, at);
    return lanes;
  } catch (e) {
    log({ event: "wake_error", error: String(e) });
    return [];
  }
}
