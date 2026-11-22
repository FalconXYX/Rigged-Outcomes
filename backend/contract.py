"""
The backend's shared constants (docs/BACKEND.md): job kinds and their lanes,
the kinds that run in the background, run graphs, states, outcomes, deadlines,
caps, the time zone, the schedule and the budget thresholds.
worker/src/contract.ts holds the same CONTRACT object
(tests/test_backend_contract.py compares the two), so the Vercel step
function and the Worker always agree. Builders read this file.

Standard library only, and nothing heavy at import: api/step.py imports it
before it has checked a wake-up's signature.
"""
from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

CONTRACT = {
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
    "final_grace_s": 3600
  },
  "run_kinds": ["daily", "catchup", "add"],
  "run_priority": { "add": 1, "daily": 2, "catchup": 3 },
  "run_states": ["queued", "running", "done", "partial", "failed", "cancelled", "missed", "skipped"],
  "run_terminal": ["done", "partial", "failed", "cancelled", "missed", "skipped"],
  "lanes": ["main", "news", "model", "add"],
  "job_states": ["queued", "leased", "done", "failed", "cancelled"],
  "job_terminal": ["done", "failed", "cancelled"],
  "outcomes": ["done", "partial", "later", "error"],
  "later_reasons": ["model_busy", "model_quota", "polymarket_down", "news_limited", "budget", "state_moved", "waiting"],
  "job_kinds": {
    "daily.begin":    { "lane": "main",  "heavy": False, "need_s": 60,  "final": False, "handler": "watchlist.steps:daily_begin" },
    "watch.check":    { "lane": "main",  "heavy": False, "need_s": 60,  "final": False, "handler": "watchlist.steps:watch_check" },
    "closed.scan":    { "lane": "main",  "heavy": False, "need_s": 90,  "final": False, "handler": "watchlist.steps:closed_scan" },
    "closed.label":   { "lane": "model", "heavy": False, "need_s": 130, "final": False, "handler": "watchlist.steps:closed_label" },
    "closed.query":   { "lane": "model", "heavy": False, "need_s": 130, "final": False, "handler": "watchlist.steps:closed_query" },
    "closed.news":    { "lane": "news",  "heavy": False, "need_s": 45,  "final": False, "handler": "watchlist.steps:closed_news" },
    "closed.judge":   { "lane": "model", "heavy": False, "need_s": 130, "final": False, "handler": "watchlist.steps:closed_judge" },
    "closed.publish": { "lane": "main",  "heavy": False, "need_s": 90,  "final": True,  "handler": "watchlist.steps:closed_publish" },
    "open.walk":      { "lane": "main",  "heavy": True,  "need_s": 150, "final": False, "handler": "watchlist.steps:open_walk" },
    "open.name":      { "lane": "model", "heavy": False, "need_s": 130, "final": False, "handler": "watchlist.steps:open_name" },
    "open.news":      { "lane": "news",  "heavy": False, "need_s": 45,  "final": False, "handler": "watchlist.steps:open_news" },
    "open.read":      { "lane": "model", "heavy": False, "need_s": 130, "final": False, "handler": "watchlist.steps:open_read" },
    "open.out":       { "lane": "model", "heavy": False, "need_s": 130, "final": False, "handler": "watchlist.steps:open_out" },
    "open.publish":   { "lane": "main",  "heavy": False, "need_s": 120, "final": True,  "handler": "watchlist.steps:open_publish" },
    "daily.end":      { "lane": "main",  "heavy": False, "need_s": 60,  "final": True,  "handler": "watchlist.steps:daily_end" },
    "add.fetch":      { "lane": "add",   "heavy": True,  "need_s": 120, "final": False, "handler": "backend.add_steps:fetch" },
    "add.whales":     { "lane": "add",   "heavy": False, "need_s": 60,  "final": False, "handler": "backend.add_steps:whales" },
    "add.export":     { "lane": "add",   "heavy": True,  "need_s": 60,  "final": True,  "handler": "backend.add_steps:export" },
    "wallets.score":  { "lane": "add",   "heavy": True,  "need_s": 120, "final": False, "handler": "backend.rubric.steps:score" }
  },
  "background_kinds": ["wallets.score"],
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
      "ok":    { "add_whales_day": 800, "open_searches": 300, "open_reads": 30, "open_out": 60, "catchup": True },
      "tight": { "add_whales_day": 200, "open_searches": 150, "open_reads": 15, "open_out": 40, "catchup": False },
      "low":   { "add_whales_day": 0,   "open_searches": 15,  "open_reads": 0,  "open_out": 15, "catchup": False },
      "stop":  { "add_whales_day": 0,   "open_searches": 0,   "open_reads": 0,  "open_out": 0,  "catchup": False }
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
}

# ── named views of CONTRACT ─────────────────────────────────────────────────

TIME_ZONE = CONTRACT["time_zone"]
TZ = ZoneInfo(TIME_ZONE)
QUOTA_TZ = ZoneInfo(CONTRACT["quota_reset_time_zone"])
RUN_KINDS = tuple(CONTRACT["run_kinds"])
RUN_STATES = tuple(CONTRACT["run_states"])
LANES = tuple(CONTRACT["lanes"])
JOB_STATES = tuple(CONTRACT["job_states"])
JOB_TERMINAL = frozenset(CONTRACT["job_terminal"])
OUTCOMES = tuple(CONTRACT["outcomes"])
LATER_REASONS = tuple(CONTRACT["later_reasons"])
JOB_KINDS = CONTRACT["job_kinds"]
BACKGROUND_KINDS = frozenset(CONTRACT["background_kinds"])
RUN_GRAPHS = CONTRACT["run_graphs"]
RETRY = CONTRACT["retry"]
STEP = CONTRACT["step"]
LIMITS = CONTRACT["limits"]
WAKE = CONTRACT["wake"]
BUDGET = CONTRACT["budget"]
LISTS = CONTRACT["lists"]
DATA = CONTRACT["data"]


def handler_path(kind):
    """("module", "function") of a job kind's step handler."""
    module, _, func = JOB_KINDS[kind]["handler"].partition(":")
    return module, func


# ── Toronto time (every day, schedule and limit the backend keeps is in Toronto time) ──

def toronto(epoch):
    """An aware datetime in Toronto for epoch seconds."""
    return datetime.fromtimestamp(int(epoch), TZ)


def toronto_day(epoch):
    """Toronto's calendar day, "YYYY-MM-DD", for epoch seconds."""
    return toronto(epoch).strftime("%Y-%m-%d")


def toronto_iso(epoch):
    """ISO 8601 with Toronto's offset ("2026-11-19T04:12:00-05:00"): the stamp every published file carries."""
    return toronto(epoch).isoformat(timespec="seconds")


def toronto_bounds(day):
    """(start, end) epoch seconds of a Toronto day: [00:00, next 00:00), 23 or 25 hours on a change of clocks."""
    d = datetime.strptime(day, "%Y-%m-%d").date()
    start = datetime.combine(d, dtime(0, 0), TZ)
    end = datetime.combine(d + timedelta(days=1), dtime(0, 0), TZ)
    return int(start.timestamp()), int(end.timestamp())


def toronto_at(day, hhmm):
    """Epoch seconds of a Toronto wall-clock time ("HH:MM") on a Toronto day."""
    d = datetime.strptime(day, "%Y-%m-%d").date()
    h, m = (int(x) for x in hhmm.split(":"))
    return int(datetime.combine(d, dtime(h, m), TZ).timestamp())


def next_quota_reset(epoch):
    """When Gemini's daily quota is next fresh: the next midnight in Los Angeles, plus quota_reset_delay_s."""
    now = datetime.fromtimestamp(int(epoch), QUOTA_TZ)
    midnight = datetime.combine(now.date() + timedelta(days=1), dtime(0, 0), QUOTA_TZ)
    return int(midnight.timestamp()) + CONTRACT["quota_reset_delay_s"]
