// The backend's shared constants (docs/BACKEND.md): job kinds and their lanes, run graphs, states, outcomes,
// deadlines, caps, the time zone, the schedule and the budget thresholds. backend/contract.py holds the same
// CONTRACT object (tests/test_backend_contract.py compares the two), so the Worker
// and the Vercel step function always agree. Builders read this file; changes go through the contract's owner.
//
// The block between the two markers is strict JSON (no comments, no trailing commas): the Python test parses it.

/* CONTRACT-JSON-BEGIN */
export const CONTRACT = {
  "version": 1,
  "time_zone": "America/Toronto",
  "quota_reset_time_zone": "America/Los_Angeles",
  "quota_reset_delay_s": 300,
  "schedule": {
    "cron": "* * * * *",
    "daily_at": "04:00",
    "daily_deadline": "09:30",
    "daily_min_run_s": 7200,
    "catchup_from": "10:00",
    "catchup_until": "12:00",
    "catchup_run_s": 10800,
    "prune_at": "03:30",
    "budget_every_min": 10,
    "add_run_s": 129600,
    "apply_run_s": 3600,
    "final_grace_s": 3600
  },
  "run_kinds": ["daily", "catchup", "add", "apply"],
  "run_priority": { "apply": 0, "add": 1, "daily": 2, "catchup": 3 },
  "run_states": ["queued", "running", "done", "partial", "failed", "cancelled", "missed", "skipped"],
  "run_terminal": ["done", "partial", "failed", "cancelled", "missed", "skipped"],
  "lanes": ["main", "news", "model", "add"],
  "job_states": ["queued", "leased", "done", "failed", "cancelled"],
  "job_terminal": ["done", "failed", "cancelled"],
  "outcomes": ["done", "partial", "later", "error"],
  "later_reasons": ["model_busy", "model_quota", "polymarket_down", "news_limited", "budget", "state_moved", "waiting"],
  "job_kinds": {
    "daily.begin":    { "lane": "main",  "heavy": false, "need_s": 60,  "final": false, "handler": "watchlist.steps:daily_begin" },
    "watch.check":    { "lane": "main",  "heavy": false, "need_s": 60,  "final": false, "handler": "watchlist.steps:watch_check" },
    "closed.scan":    { "lane": "main",  "heavy": false, "need_s": 90,  "final": false, "handler": "watchlist.steps:closed_scan" },
    "closed.label":   { "lane": "model", "heavy": false, "need_s": 130, "final": false, "handler": "watchlist.steps:closed_label" },
    "closed.query":   { "lane": "model", "heavy": false, "need_s": 130, "final": false, "handler": "watchlist.steps:closed_query" },
    "closed.news":    { "lane": "news",  "heavy": false, "need_s": 45,  "final": false, "handler": "watchlist.steps:closed_news" },
    "closed.judge":   { "lane": "model", "heavy": false, "need_s": 130, "final": false, "handler": "watchlist.steps:closed_judge" },
    "closed.publish": { "lane": "main",  "heavy": false, "need_s": 90,  "final": true,  "handler": "watchlist.steps:closed_publish" },
    "open.walk":      { "lane": "main",  "heavy": true,  "need_s": 150, "final": false, "handler": "watchlist.steps:open_walk" },
    "open.name":      { "lane": "model", "heavy": false, "need_s": 130, "final": false, "handler": "watchlist.steps:open_name" },
    "open.news":      { "lane": "news",  "heavy": false, "need_s": 45,  "final": false, "handler": "watchlist.steps:open_news" },
    "open.read":      { "lane": "model", "heavy": false, "need_s": 130, "final": false, "handler": "watchlist.steps:open_read" },
    "open.out":       { "lane": "model", "heavy": false, "need_s": 130, "final": false, "handler": "watchlist.steps:open_out" },
    "open.publish":   { "lane": "main",  "heavy": false, "need_s": 120, "final": true,  "handler": "watchlist.steps:open_publish" },
    "daily.end":      { "lane": "main",  "heavy": false, "need_s": 60,  "final": true,  "handler": "watchlist.steps:daily_end" },
    "apply.decide":   { "lane": "main",  "heavy": false, "need_s": 120, "final": true,  "handler": "watchlist.steps:apply_decide" },
    "add.fetch":      { "lane": "add",   "heavy": true,  "need_s": 120, "final": false, "handler": "backend.add_steps:fetch" },
    "add.whales":     { "lane": "add",   "heavy": false, "need_s": 60,  "final": false, "handler": "backend.add_steps:whales" },
    "add.export":     { "lane": "add",   "heavy": true,  "need_s": 60,  "final": true,  "handler": "backend.add_steps:export" },
    "wallets.score":  { "lane": "add",   "heavy": true,  "need_s": 120, "final": false, "handler": "backend.rubric.steps:score" }
  },
  "run_graphs": {
    "daily": [
      { "kind": "daily.begin",    "after": [] },
      { "kind": "watch.check",    "after": ["daily.begin"] },
      { "kind": "closed.scan",    "after": ["daily.begin"] },
      { "kind": "closed.label",   "after": ["closed.scan"] },
      { "kind": "closed.query",   "after": ["closed.scan", "closed.label"] },
      { "kind": "closed.news",    "after": ["closed.scan", "closed.label", "closed.query"] },
      { "kind": "closed.judge",   "after": ["closed.scan", "closed.label", "closed.query", "closed.news"] },
      { "kind": "closed.publish", "after": ["closed.scan", "closed.label", "closed.query", "closed.news", "closed.judge"] },
      { "kind": "open.walk",      "after": ["daily.begin"] },
      { "kind": "open.name",      "after": ["open.walk"] },
      { "kind": "open.news",      "after": ["open.walk", "open.name"] },
      { "kind": "open.read",      "after": ["open.walk", "open.name", "open.news"] },
      { "kind": "open.out",       "after": ["open.walk", "open.name", "open.news", "open.read"] },
      { "kind": "open.publish",   "after": ["open.walk", "open.name", "open.news", "open.read", "open.out"] },
      { "kind": "daily.end",      "after": ["daily.begin", "watch.check", "closed.publish", "open.publish"] }
    ],
    "catchup": [
      { "kind": "open.read",      "after": [] },
      { "kind": "open.out",       "after": ["open.read"] },
      { "kind": "open.publish",   "after": ["open.read", "open.out"] },
      { "kind": "daily.end",      "after": ["open.publish"] }
    ],
    "add": [
      { "kind": "add.fetch",      "after": [] },
      { "kind": "add.export",     "after": ["add.fetch", "add.whales"] }
    ],
    "apply": [
      { "kind": "apply.decide",   "after": [] }
    ]
  },
  "retry": {
    "max_attempts": 6,
    "base_s": 30,
    "cap_s": 1800,
    "jitter_s": 15,
    "laters_max": 12,
    "partials_max": 60,
    "model_busy_s": [300, 600],
    "polymarket_down_s": 120,
    "news_limited_s": 120,
    "state_moved_s": 60
  },
  "step": {
    "function_max_s": 300,
    "cold_start_s": 10,
    "reserve_s": 20,
    "min_claim_left_s": 45,
    "lease_slack_s": 30,
    "killed_wall_s": 300,
    "killed_cpu_s": 150,
    "worker_call_timeout_s": 15,
    "tmp_root": "/tmp/ro",
    "tmp_max_mb": 150,
    "instance_gb": 2,
    "state_save_tries": 3
  },
  "limits": {
    "claim_body_bytes": 4096,
    "checkpoint_body_bytes": 262144,
    "complete_body_bytes": 262144,
    "result_value_bytes": 204800,
    "result_key_chars": 300,
    "input_bytes": 16384,
    "cursor_bytes": 65536,
    "run_meta_bytes": 8192,
    "error_chars": 500,
    "children_max": 100,
    "results_page_rows": 500,
    "results_page_bytes": 524288,
    "news_get_keys": 50,
    "news_get_bytes": 524288,
    "news_key_chars": 600,
    "news_body_bytes": 204800,
    "wakes_per_tick": 4
  },
  "wake": {
    "path": "/api/step",
    "header": "x-ro-wake",
    "gate_header": "x-ro-gate",
    "scheme": "v1",
    "max_skew_s": 60,
    "abort_ms": 8000,
    "backoff_base_s": 60,
    "backoff_max_s": 3600,
    "handler_free_wakes": 2
  },
  "budget": {
    "cpu_ms_month": 14400000,
    "gb_ms_month": 1296000000,
    "invocations_month": 1000000,
    "window_days": 30,
    "cpu_safety": 1.25,
    "invocation_cpu_ms": 300,
    "invocation_gb_ms": 6000,
    "levels": ["ok", "tight", "low", "stop"],
    "level_at": { "tight": 0.5, "low": 0.65, "stop": 0.8 },
    "day_cpu_ms_max": 1200000,
    "day_gb_ms_max": 108000000,
    "blocked_kinds": {
      "ok": [],
      "tight": [],
      "low": ["add.fetch", "add.whales", "open.name", "open.read"],
      "stop": ["*"]
    },
    "caps": {
      "ok":    { "add_whales_day": 800, "open_searches": 300, "open_reads": 30, "open_out": 60, "catchup": true },
      "tight": { "add_whales_day": 200, "open_searches": 150, "open_reads": 15, "open_out": 40, "catchup": false },
      "low":   { "add_whales_day": 0,   "open_searches": 15,  "open_reads": 0,  "open_out": 15, "catchup": false },
      "stop":  { "add_whales_day": 0,   "open_searches": 0,   "open_reads": 0,  "open_out": 0,  "catchup": false }
    }
  },
  "lists": {
    "closed_per_day": 3,
    "closed_at_least": 3,
    "closed_days": 2,
    "open_searches_per_story": 1,
    "open_step_readings": 1,
    "open_out_readings": 1,
    "open_read_workers": 3,
    "add_whale_batch": 15,
    "add_max_whales": 700,
    "add_whales_reserve": 100,
    "add_threads": 10
  },
  "data": {
    "prefix": "/api/data/",
    "files": ["index.json", "closed.json", "open.json"],
    "market_path": "markets/",
    "blob_prefix": "site:",
    "list_cache": "public, max-age=0, must-revalidate",
    "market_cache_versioned": "public, max-age=31536000, immutable",
    "market_cache": "public, max-age=0, must-revalidate",
    "part_chars": 500000
  },
  "blob_names": {
    "state": "^state$",
    "site": "^site:(index|closed|open)\\.json$",
    "site_market": "^site:markets/[a-z0-9][a-z0-9-]{1,31}\\.json$",
    "market": "^market:[a-z0-9][a-z0-9-]{1,31}$",
    "scratch": "^scratch:[0-9a-f]{16}:[a-z0-9_.-]{1,40}$",
    "rubric": "^rubric:[a-z0-9][a-z0-9-]{1,31}$"
  },
  "history_blobs": ["state", "site:index.json", "site:closed.json", "site:open.json"],
  "prune": {
    "runs_days": 30,
    "jobs_days": 14,
    "results_days": 3,
    "news_days": 3,
    "usage_days": 60,
    "scratch_days": 3,
    "orphan_parts_days": 1,
    "quota_days": 3
  }
} as const;
/* CONTRACT-JSON-END */

// ---------- named views of CONTRACT ----------

export type RunKind = (typeof CONTRACT.run_kinds)[number];
export type RunState = (typeof CONTRACT.run_states)[number];
export type Lane = (typeof CONTRACT.lanes)[number];
export type JobState = (typeof CONTRACT.job_states)[number];
export type Outcome = (typeof CONTRACT.outcomes)[number];
export type LaterReason = (typeof CONTRACT.later_reasons)[number];
export type JobKind = keyof typeof CONTRACT.job_kinds;
export type BudgetLevel = (typeof CONTRACT.budget.levels)[number];

export const JOB_KINDS = CONTRACT.job_kinds;
export const RUN_GRAPHS = CONTRACT.run_graphs;

export const isJobKind = (k: string): k is JobKind => Object.prototype.hasOwnProperty.call(CONTRACT.job_kinds, k);

// ---------- Toronto time (Cron Triggers run in UTC; everything the backend decides is in Toronto time) ----------

const PARTS = new Intl.DateTimeFormat("en-CA", {
  timeZone: CONTRACT.time_zone,
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hourCycle: "h23",
});

/** Toronto's calendar day ("YYYY-MM-DD") and minutes since its local midnight, for epoch seconds. */
export function toronto(epoch: number): { day: string; minutes: number } {
  const p = Object.fromEntries(PARTS.formatToParts(new Date(epoch * 1000)).map((x) => [x.type, x.value]));
  return { day: `${p.year}-${p.month}-${p.day}`, minutes: Number(p.hour) * 60 + Number(p.minute) };
}

/** "HH:MM" as minutes since midnight. */
export const hhmm = (s: string): number => Number(s.slice(0, 2)) * 60 + Number(s.slice(3, 5));

/** The day before a "YYYY-MM-DD" day (calendar arithmetic, no time zone involved). */
export function dayBefore(day: string): string {
  const d = new Date(`${day}T12:00:00Z`);
  d.setUTCDate(d.getUTCDate() - 1);
  return d.toISOString().slice(0, 10);
}

/**
 * Epoch seconds of a Toronto wall-clock time ("HH:MM") on a Toronto day, daylight saving included: the offset is
 * read from the zone itself, so 04:00 is 09:00 UTC in winter and 08:00 UTC in summer.
 */
export function torontoAt(day: string, time: string): number {
  const guess = Date.parse(`${day}T${time}:00Z`) / 1000;
  // two passes settle the offset on either side of a change
  let t = guess;
  for (let i = 0; i < 2; i++) {
    const p = toronto(t);
    const shownAsUtc = Date.parse(`${p.day}T${String(Math.floor(p.minutes / 60)).padStart(2, "0")}:${String(p.minutes % 60).padStart(2, "0")}:00Z`) / 1000;
    t += guess - shownAsUtc;
  }
  return t;
}
