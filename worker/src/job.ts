// The daily lists' endpoints, behind JOB_TOKEN (watchlist/sync.py, called from the step function's jobs): the
// run's work, the catalog of what visitors may write to, the watched markets sent to the main page (each gets its
// add run at once), and the run's report. The job's own data (its memory, the lists,
// added markets) is in blobs.ts; the queue itself in queue.ts; old rows go in the cron's daily prune (cron.ts).

import { CONTRACT } from "./contract";
import {
  BODY,
  CONDITION_ID,
  type Ctx,
  DAY,
  HttpError,
  MARKET_NAME,
  PAGE_NAME,
  fail,
  int,
  json,
  jsonChunks,
  list,
  noContent,
  normSlug,
  now,
  obj,
  oneOf,
  readBody,
  text,
} from "./lib";
import { readBudget } from "./ledger";
import { forgetMarkers } from "./public";
import { graphJobs } from "./queue";
import { wakeReady } from "./wake";

/**
 * The run's work: the markets on their way to the main page (status approved, each with its run), the stories
 * on the site's watchlist, and the markets added to the main page (`added`: condition id and day; the closed
 * list keeps their picks). A read writes nothing.
 */
export async function work({ env, clock }: Ctx): Promise<Response> {
  const at = await clock();
  const [requests, watched, added] = await env.DB.batch([
    env.DB.prepare("SELECT * FROM requests WHERE status = 'approved' ORDER BY decided_at"),
    env.DB.prepare("SELECT story FROM watches ORDER BY story"),
    env.DB.prepare("SELECT condition_id, day FROM requests WHERE status = 'added' ORDER BY day, condition_id"),
  ]);
  return json({ now: at, requests: requests.results, watched: watched.results, added: added.results });
}

/**
 * The backend at a glance, read only (python3 -m backend.status): the budget, each lane's pause and wake-ups,
 * the latest 15 runs, the failed jobs of the last 14 days (latest 10), the markets sent to the main page (not
 * yet added, or added in the last 30 days) and the latest 10 run reports.
 */
export async function status({ env, clock }: Ctx): Promise<Response> {
  const at = await clock();
  const [budget, lanes, runs, failed, requests, reports] = await env.DB.batch([
    env.DB.prepare("SELECT value FROM settings WHERE key = 'budget'"),
    env.DB.prepare("SELECT lane, paused_until, unanswered, last_wake_at, last_claim_at, wake_error FROM lanes ORDER BY lane"),
    env.DB.prepare(
      "SELECT id, kind, slot, state, created_at, started_at, finished_at, deadline, meta FROM runs ORDER BY created_at DESC, id DESC LIMIT 15",
    ),
    env.DB.prepare("SELECT id, kind, error, updated_at FROM jobs WHERE state = 'failed' AND updated_at >= ? ORDER BY updated_at DESC LIMIT 10").bind(
      at - 14 * DAY,
    ),
    env.DB.prepare("SELECT * FROM requests WHERE status != 'added' OR done_at >= ? ORDER BY requested_at DESC LIMIT 200").bind(at - 30 * DAY),
    env.DB.prepare("SELECT * FROM job_runs ORDER BY id DESC LIMIT 10"),
  ]);
  const meta = (v: string) => {
    try {
      return JSON.parse(v) as Record<string, unknown>;
    } catch {
      return {};
    }
  };
  return json({
    now: at,
    budget: readBudget((budget.results[0] as { value?: string } | undefined)?.value),
    lanes: lanes.results,
    runs: (runs.results as { meta: string }[]).map((r) => ({ ...r, meta: meta(r.meta) })),
    failed_jobs: failed.results,
    requests: requests.results,
    reports: reports.results,
  });
}

/** Text cut to max characters, or null when it isn't text. */
const clip = (v: unknown, max: number) => (typeof v === "string" && v ? v.slice(0, max) : null);

/** A link the site may render (https only: never javascript: or data:), up to max characters, or null. */
const link = (v: unknown, max = 500) =>
  typeof v === "string" && v.length <= max && /^https:\/\/[^\s"<>]+$/.test(v) ? v : null;

type Entry = { id: string; slug: string | null; data: string };

const CATALOG_KINDS = {
  markets: {
    kind: "market",
    entry(v: unknown, at: string): Entry {
      const e = obj(v, at);
      const name = text(e.name, `${at}.name`, 40);
      if (!MARKET_NAME.test(name)) fail(400, "bad_request", `${at}.name must match ${MARKET_NAME}.`);
      const slug = text(e.slug, `${at}.slug`, 1000);
      return { id: name, slug: normSlug(slug), data: JSON.stringify({ name, slug }) };
    },
  },
  closed: {
    kind: "closed",
    entry(v: unknown, at: string): Entry {
      const e = obj(v, at);
      const cid = text(e.condition_id, `${at}.condition_id`, 66).toLowerCase();
      if (!CONDITION_ID.test(cid)) fail(400, "bad_request", `${at}.condition_id must be 0x and 64 hex characters.`);
      const url = link(text(e.url, `${at}.url`, 1000), 1000);
      if (!url) fail(400, "bad_request", `${at}.url must be an https:// link.`);
      const d = {
        condition_id: cid,
        slug: text(e.slug, `${at}.slug`, 1000),
        question: text(e.question, `${at}.question`, 1000),
        url,
        winner: text(e.winner, `${at}.winner`, 200),
        closed: text(e.closed, `${at}.closed`, 40),
        day: text(e.day, `${at}.day`, 10),
      };
      return { id: cid, slug: normSlug(d.slug), data: JSON.stringify(d) };
    },
  },
  open: {
    kind: "open",
    entry(v: unknown, at: string): Entry {
      const e = obj(v, at);
      const d = {
        story: text(e.story, `${at}.story`, 200),
        question: text(e.question, `${at}.question`, 1000),
        url: link(text(e.url, `${at}.url`, 1000), 1000),
      };
      return { id: d.story, slug: null, data: JSON.stringify(d) };
    },
  },
} as const;

/**
 * What visitors may write to, as the job lists it: main-page markets (markers, and "already on the main page"),
 * closed picks (Add to main page) and open cards (the watchlist). Each section sent replaces its kind in one
 * transaction; a section left out is kept. The markets section always holds every added market too (from their
 * market:<name> records), so a writer that read the added markets before another add finished never drops it.
 * A repeated id keeps its last entry. An entry that doesn't fit (a markets.json name with capitals, a pick with
 * no link) is left out and named in `skipped`, so one odd entry can't stop the rest.
 */
export async function putCatalog({ req, env }: Ctx): Promise<Response> {
  const body = await readBody(req, BODY.job);
  const at = now();
  const statements: D1PreparedStatement[] = [];
  const skipped: string[] = [];
  const added =
    body.markets === undefined
      ? []
      : (
          await env.DB.prepare(
            `SELECT json_extract(meta, '$.entry.name') AS name, json_extract(meta, '$.entry.slug') AS slug FROM job_blobs
             WHERE name > 'market:' AND name < 'market;' AND json_extract(meta, '$.entry.name') IS NOT NULL ORDER BY name`,
          ).all<{ name: unknown; slug: unknown }>()
        ).results;
  for (const [section, spec] of Object.entries(CATALOG_KINDS)) {
    if (body[section] === undefined) continue;
    const rows = new Map<string, Entry>();
    const sent = list(body[section], section, 5000).map((v, i): [string, unknown] => [`${section}[${i}]`, v]);
    const kept = section === "markets" ? added.map((v): [string, unknown] => [`the added market ${String(v.name)}`, v]) : [];
    [...kept, ...sent].forEach(([where, v]) => {
      try {
        const e = spec.entry(v, where);
        rows.set(e.id, e);
      } catch (err) {
        if (!(err instanceof HttpError)) throw err;
        skipped.push(err.message);
      }
    });
    statements.push(env.DB.prepare("DELETE FROM catalog WHERE kind = ?").bind(spec.kind));
    for (const chunk of jsonChunks([...rows.values()])) {
      statements.push(
        env.DB.prepare(
          `INSERT INTO catalog (kind, id, slug, data, synced_at)
           SELECT ?1, json_extract(value, '$.id'), json_extract(value, '$.slug'), json_extract(value, '$.data'), ?2 FROM json_each(?3)`,
        ).bind(spec.kind, at, chunk),
      );
    }
  }
  if (statements.length) await env.DB.batch(statements);
  const total = await env.DB.prepare("SELECT kind, count(*) AS n FROM catalog GROUP BY kind").all<{ kind: string; n: number }>();
  const n = (k: string) => total.results.find((r) => r.kind === k)?.n ?? 0;
  forgetMarkers();
  return json({ market: n("market"), closed: n("closed"), open: n("open"), skipped: skipped.slice(0, 50) });
}

/** Rows one call of the watched markets takes at most. */
const WATCHED_SENT = 200;

/**
 * The markets the site's watchlist sends to the main page (the daily run's watch.check: a watched open card's
 * market that closed with a result). Each is recorded as approved, as a visitor's "Add to main page" is, from
 * the job's copy of the closed pick (the fields the catalog's closed entries hold, checked the same way), and
 * gets its add run in the same batch, so it is added at once and its Recently closed card follows it; the add
 * lane is woken after the answer. One row per market: one there already (a visitor's add, or one added or
 * failed) is left as it is. An entry that doesn't fit is left out and named in `skipped`. Answers with the rows
 * of the markets sent, as they are now.
 */
export async function addRequests({ req, env, clock, waitUntil }: Ctx): Promise<Response> {
  const body = await readBody(req, BODY.job);
  const rows = new Map<string, string>();
  const skipped: string[] = [];
  list(body.requests, "requests", WATCHED_SENT).forEach((v, i) => {
    try {
      const e = CATALOG_KINDS.closed.entry(v, `requests[${i}]`);
      rows.set(e.id, e.data);
    } catch (err) {
      if (!(err instanceof HttpError)) throw err;
      skipped.push(err.message);
    }
  });
  if (!rows.size) return json({ requests: [], skipped });
  const at = await clock();
  const picks = [...rows.values()].map((d) => JSON.parse(d) as Record<string, string>);
  const runs = [...rows.keys()].map((cid) => `add:${cid}:${at}`);
  const results = await env.DB.batch([
    ...jsonChunks(picks).map((chunk) =>
      env.DB.prepare(
        `INSERT INTO requests (condition_id, slug, question, url, winner, closed, day, status, requested_at, decided_at, run_id)
         SELECT json_extract(value, '$.condition_id'), json_extract(value, '$.slug'), json_extract(value, '$.question'),
                json_extract(value, '$.url'), json_extract(value, '$.winner'), json_extract(value, '$.closed'),
                json_extract(value, '$.day'), 'approved', ?1, ?1, 'add:' || json_extract(value, '$.condition_id') || ':' || ?3
         FROM json_each(?2) WHERE true
         ON CONFLICT (condition_id) DO NOTHING`,
      ).bind(at, chunk, String(at)), // the slot's second as text: a bound number reaches SQLite as a REAL ("1796248600.0")
    ),
    // an add run for each request just made (its run_id is this call's)
    env.DB.prepare(
      `INSERT INTO runs (id, kind, slot, state, meta, created_at, deadline)
       SELECT run_id, 'add', substr(run_id, 5), 'queued', json_object('condition_id', condition_id), ?1, ?2
       FROM requests WHERE run_id IN (SELECT value FROM json_each(?3)) AND status = 'approved'
       ON CONFLICT DO NOTHING`,
    ).bind(at, at + CONTRACT.schedule.add_run_s, JSON.stringify(runs)),
    graphJobs(env, "add", runs, at),
    env.DB.prepare("SELECT * FROM requests WHERE condition_id IN (SELECT value FROM json_each(?)) ORDER BY decided_at").bind(
      JSON.stringify([...rows.keys()]),
    ),
  ]);
  if (results[results.length - 3].meta.changes) waitUntil(wakeReady(env, at, { only: ["add"], handler: true }));
  return json({ requests: results[results.length - 1].results, skipped });
}

/**
 * The run's report. `requests`: what adding the markets did (the page name the job gave one is kept), recorded only while it is
 * still approved. `watched`: the watchlist's stories the job is done with (their market sent to the main page,
 * or on it already), each taken off the watchlist once the catalog's open cards no longer list it. `run`: one
 * job_runs row. Old rows go in the cron's daily prune (cron.ts).
 */
export async function done({ req, env }: Ctx): Promise<Response> {
  // Only what identifies a row is strict here; a long note or summary is cut and a link that isn't https:// is
  // dropped, so a cosmetic slip can't cost the report its request marks.
  const body = await readBody(req, BODY.job);
  const at = now();
  const requests = list(body.requests ?? [], "requests", 200).map((v, i) => {
    const r = obj(v, `requests[${i}]`);
    const cid = text(r.condition_id, `requests[${i}].condition_id`, 66).toLowerCase();
    if (!CONDITION_ID.test(cid)) fail(400, "bad_request", `requests[${i}].condition_id must be 0x and 64 hex characters.`);
    return {
      cid,
      status: oneOf(r.status, `requests[${i}].status`, ["added", "failed"] as const),
      name: typeof r.name === "string" && PAGE_NAME.test(r.name) ? r.name : null,
      note: clip(r.note, 500),
    };
  });
  const watched = list(body.watched ?? [], "watched", 1000).map((v, i) => text(v, `watched[${i}]`, 200));
  const run =
    body.run === undefined || body.run === null
      ? null
      : (() => {
          const r = obj(body.run, "run");
          return {
            mode: oneOf(r.mode, "run.mode", ["full", "local"] as const),
            status: oneOf(r.status, "run.status", ["success", "partial", "failure"] as const),
            started_at: int(r.started_at, "run.started_at"),
            finished_at: int(r.finished_at, "run.finished_at"),
            summary: clip(r.summary, 500),
            url: link(r.url),
          };
        })();

  const statements: D1PreparedStatement[] = [];
  for (const chunk of jsonChunks(requests)) {
    statements.push(
      env.DB.prepare(
        `UPDATE requests SET status = j.status, note = j.note, name = coalesce(requests.name, j.name), done_at = ?1
         FROM (SELECT json_extract(value, '$.cid') AS cid, json_extract(value, '$.status') AS status,
                      json_extract(value, '$.note') AS note, json_extract(value, '$.name') AS name FROM json_each(?2)) AS j
         WHERE requests.condition_id = j.cid AND requests.status = 'approved'`,
      ).bind(at, chunk),
    );
  }
  for (const chunk of jsonChunks(watched)) {
    statements.push(
      env.DB.prepare(
        "DELETE FROM watches WHERE story IN (SELECT value FROM json_each(?)) AND story NOT IN (SELECT id FROM catalog WHERE kind = 'open')",
      ).bind(chunk),
    );
  }
  if (run) {
    statements.push(
      env.DB.prepare("INSERT INTO job_runs (mode, status, started_at, finished_at, summary, url) VALUES (?, ?, ?, ?, ?, ?)").bind(
        run.mode,
        run.status,
        run.started_at,
        run.finished_at,
        run.summary,
        run.url,
      ),
    );
  }
  if (statements.length) await env.DB.batch(statements);
  return noContent();
}
