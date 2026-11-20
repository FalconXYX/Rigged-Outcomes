// The compute ledger (docs/BACKEND.md): what the Vercel calls used, per Toronto day and job kind
// (usage), and the budget level the cron works out from it every 10 minutes (settings.budget), so the step
// function never comes near Vercel Hobby's monthly allowance and its 30-day pause. The level decides what is
// claimed (blocked kinds), how much a day may do (caps), whether wake-ups go out and whether due runs run.

import { type BudgetLevel, CONTRACT, type JobKind, toronto } from "./contract";
import { type Env, daysBefore } from "./lib";

const B = CONTRACT.budget;

/** settings.budget, as GET /api/job/status gives it: the 30 days' and today's figures (safety and wake-ups counted in). */
export interface Budget {
  level: BudgetLevel;
  cpu_ms_30d: number;
  gb_ms_30d: number;
  invocations_30d: number;
  cpu_ms_today: number;
  gb_ms_today: number;
  computed_at: number;
}

const EMPTY: Budget = { level: "ok", cpu_ms_30d: 0, gb_ms_30d: 0, invocations_30d: 0, cpu_ms_today: 0, gb_ms_today: 0, computed_at: 0 };

/** settings.budget's value read back; none yet (or unreadable) is "ok" with nothing used, computed at 0 (so the next tick computes it). */
export function readBudget(value: string | null | undefined): Budget {
  if (!value) return { ...EMPTY };
  try {
    const b = JSON.parse(value) as Partial<Budget>;
    return { ...EMPTY, ...b, level: B.levels.includes(b.level as BudgetLevel) ? (b.level as BudgetLevel) : "ok" };
  } catch {
    return { ...EMPTY };
  }
}

/** The level for the ledger's sums (already with cpu_safety and the wake-ups' share). */
export function levelOf(cpu30: number, gb30: number, inv30: number, cpuToday: number, gbToday: number): BudgetLevel {
  const f = Math.max(cpu30 / B.cpu_ms_month, gb30 / B.gb_ms_month, inv30 / B.invocations_month);
  if (f >= B.level_at.stop || cpuToday >= B.day_cpu_ms_max || gbToday >= B.day_gb_ms_max) return "stop";
  if (f >= B.level_at.low) return "low";
  if (f >= B.level_at.tight) return "tight";
  return "ok";
}

/** A level's daily caps (add whales, Open now's searches, readings, "already out") and whether a catch-up may run. */
export const capsOf = (level: BudgetLevel) => B.caps[level];

/** The kinds a level never lets be claimed, as the claim's ',kind,kind,' text ('' for none); null: nothing at all. */
export function blockedText(level: BudgetLevel): string | null {
  const kinds = B.blocked_kinds[level] as readonly string[];
  if (kinds.includes("*")) return null;
  return kinds.length ? `,${kinds.join(",")},` : "";
}

export const isBlocked = (level: BudgetLevel, kind: JobKind) => {
  const kinds = B.blocked_kinds[level] as readonly string[];
  return kinds.includes("*") || kinds.includes(kind);
};

/** One step's (or a call's, or a wake-up's) use added to usage(day, kind). */
export const addUsage = (env: Env, day: string, kind: string, u: { steps?: number; cpu_ms?: number; gb_ms?: number; wakes?: number; units?: number }) =>
  env.DB.prepare(
    `INSERT INTO usage (day, kind, steps, cpu_ms, gb_ms, wakes, units) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)
     ON CONFLICT (day, kind) DO UPDATE SET steps = steps + excluded.steps, cpu_ms = cpu_ms + excluded.cpu_ms,
       gb_ms = gb_ms + excluded.gb_ms, wakes = wakes + excluded.wakes, units = units + excluded.units`,
  ).bind(day, kind, u.steps ?? 0, u.cpu_ms ?? 0, u.gb_ms ?? 0, u.wakes ?? 0, u.units ?? 0);

/**
 * Work the budget out again from the last 30 Toronto days of usage (today included) and keep it as
 * settings.budget: cpu = Σ cpu_ms × cpu_safety + wakes × invocation_cpu_ms, gb = Σ gb_ms + wakes ×
 * invocation_gb_ms, invocations = Σ wakes.
 */
export async function computeBudget(env: Env, at: number): Promise<Budget> {
  const today = toronto(at).day;
  const from = daysBefore(today, B.window_days - 1);
  const s = await env.DB.prepare(
    `SELECT coalesce(sum(cpu_ms), 0) AS cpu, coalesce(sum(gb_ms), 0) AS gb, coalesce(sum(wakes), 0) AS wakes,
            coalesce(sum(CASE WHEN day = ?2 THEN cpu_ms END), 0) AS cpu_t, coalesce(sum(CASE WHEN day = ?2 THEN gb_ms END), 0) AS gb_t,
            coalesce(sum(CASE WHEN day = ?2 THEN wakes END), 0) AS wakes_t
     FROM usage WHERE day >= ?1 AND day <= ?2`,
  )
    .bind(from, today)
    .first<{ cpu: number; gb: number; wakes: number; cpu_t: number; gb_t: number; wakes_t: number }>();
  const u = s ?? { cpu: 0, gb: 0, wakes: 0, cpu_t: 0, gb_t: 0, wakes_t: 0 };
  const cpu = Math.round(u.cpu * B.cpu_safety + u.wakes * B.invocation_cpu_ms);
  const gb = Math.round(u.gb + u.wakes * B.invocation_gb_ms);
  const cpuToday = Math.round(u.cpu_t * B.cpu_safety + u.wakes_t * B.invocation_cpu_ms);
  const gbToday = Math.round(u.gb_t + u.wakes_t * B.invocation_gb_ms);
  const budget: Budget = {
    level: levelOf(cpu, gb, u.wakes, cpuToday, gbToday),
    cpu_ms_30d: cpu,
    gb_ms_30d: gb,
    invocations_30d: u.wakes,
    cpu_ms_today: cpuToday,
    gb_ms_today: gbToday,
    computed_at: at,
  };
  await env.DB.prepare(
    "INSERT INTO settings (key, value, updated_at) VALUES ('budget', ?1, ?2) ON CONFLICT (key) DO UPDATE SET value = ?1, updated_at = ?2",
  )
    .bind(JSON.stringify(budget), at)
    .run();
  return budget;
}
