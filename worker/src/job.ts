// The daily lists' endpoints, behind JOB_TOKEN (watchlist/sync.py, called from the step function's jobs): what
// to apply, the open list's review queue, the catalog of what visitors may write to, the watched markets sent to
// the main page (each gets its add run at once), and the run's report. The job's own data (its memory, the lists,
// added markets) is in blobs.ts; the queue itself in queue.ts; old rows go in the cron's daily prune (cron.ts).

import { CONTRACT } from "./contract";
import {
  BODY,
  CONDITION_ID,
  type Ctx,
  DAY_TEXT,
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
import { decision } from "./owner";
import { forgetMarkers } from "./public";
import { graphJobs } from "./queue";
import { wakeReady } from "./wake";

/** opennow offers at most QUEUE_MAX (15) stories a day; twice that leaves room. */
const QUEUE_ITEMS = 30;
const QUEUE_DAYS = 7;
/** One queue item is one row, well under D1's 100 KB statement cap even escaped inside json_each's text (a real item is about 3 KB). */
const ITEM_BYTES = 60_000;

/**
 * What the job applies: pending decisions (a "withdrawn" one included: the job removes it from its store), the
 * markets on their way to the main page (status approved, each with its run), the stories on the site's
 * watchlist, and the markets added to the main page (`added`: condition id and day; the closed list keeps their
 * picks). A read writes nothing.
 */
export async function work({ env, clock }: Ctx): Promise<Response> {
  const at = await clock();
  const [decisions, requests, watched, added] = await env.DB.batch([
    env.DB.prepare("SELECT * FROM open_decisions WHERE applied_at IS NULL ORDER BY decided_at"),
    env.DB.prepare("SELECT * FROM requests WHERE status = 'approved' ORDER BY decided_at"),
    env.DB.prepare("SELECT story FROM watches ORDER BY story"),
    env.DB.prepare("SELECT condition_id, day FROM requests WHERE status = 'added' ORDER BY day, condition_id"),
  ]);
  return json({ now: at, decisions: decisions.results.map(decision), requests: requests.results, watched: watched.results, added: added.results });
}

/**
 * Replace one day's review queue (opennow's `awaiting`, each candidate with its headline key), keeping 7 days. The
 * newest day sent is kept as settings.queue_day ({day, run_at, steps_by}), so a day with nothing to check still
 * reads as that day's queue, empty.
 */
export async function putQueue({ req, env }: Ctx): Promise<Response> {
  const body = await readBody(req, BODY.job);
  const day = text(body.day, "day", 10);
  if (!DAY_TEXT.test(day)) fail(400, "bad_request", "day must be YYYY-MM-DD.");
  const runAt = text(body.run_at, "run_at", 40);
  const stepsBy = oneOf(body.steps_by, "steps_by", ["person", "model"] as const);
  const items = list(body.items, "items", QUEUE_ITEMS).map((v, i) => {
    const item = obj(v, `items[${i}]`);
    const n = int(item.n, `items[${i}].n`);
    const story = text(item.story, `items[${i}].story`, 200);
    list(item.candidates, `items[${i}].candidates`, 100).forEach((c, j) => {
      const at = `items[${i}].candidates[${j}]`;
      const cand = obj(c, at);
      text(cand.key, `${at}.key`, 500);
      if (cand.url != null) cand.url = link(cand.url, 4000); // the review page renders these as links (Google News's run long)
    });
    if (item.url != null) item.url = link(item.url, 1000);
    const data = JSON.stringify(item);
    if (data.length > ITEM_BYTES) fail(413, "too_large", `The queue item for "${story}" is over ${ITEM_BYTES} bytes.`);
    return { n, story, data };
  });
  if (new Set(items.map((i) => i.story)).size !== items.length) fail(400, "bad_request", "A story is in the queue twice.");

  const at = now();
  await env.DB.batch([
    env.DB.prepare("DELETE FROM open_queue WHERE day = ?").bind(day),
    ...jsonChunks(items).map((chunk) =>
      env.DB.prepare(
        `INSERT INTO open_queue (day, story, n, item, run_at, steps_by, pushed_at)
         SELECT ?1, json_extract(value, '$.story'), json_extract(value, '$.n'), json_extract(value, '$.data'), ?2, ?3, ?4 FROM json_each(?5)`,
      ).bind(day, runAt, stepsBy, at, chunk),
    ),
    env.DB.prepare(
      `INSERT INTO settings (key, value, updated_at) VALUES ('queue_day', ?1, ?2)
       ON CONFLICT (key) DO UPDATE SET value = ?1, updated_at = ?2 WHERE json_extract(?1, '$.day') >= json_extract(settings.value, '$.day')`,
    ).bind(JSON.stringify({ day, run_at: runAt, steps_by: stepsBy }), at),
    pruneQueue(env),
  ]);
  return noContent();
}

export const pruneQueue = (env: Ctx["env"]) =>
  env.DB.prepare(`DELETE FROM open_queue WHERE day NOT IN (SELECT DISTINCT day FROM open_queue ORDER BY day DESC LIMIT ${QUEUE_DAYS})`);

/** Text cut to max characters, or null when it isn't text. */
const clip = (v: unknown, max: number) => (typeof v === "string" && v ? v.slice(0, max) : null);

/** A link the review page may render (https only: never javascript: or data:), up to max characters, or null. */
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
 * The run's report. `applied`: decisions the job saved and pushed, marked applied only while their decided_at is
 * unchanged (one decided again meanwhile stays pending); a withdrawn one the job has removed from its store goes.
 * `requests`: what adding the markets did (the page name the job gave one is kept), recorded only while it is
 * still approved. `watched`: the watchlist's stories the job is done with (their market sent to the main page,
 * or on it already), each taken off the watchlist once the catalog's open cards no longer list it. `run`: one
 * job_runs row. Old rows go in the cron's daily prune (cron.ts).
 */
export async function done({ req, env }: Ctx): Promise<Response> {
  // Only what identifies a row is strict here; a long note or summary is cut and a link that isn't https:// is
  // dropped, so a cosmetic slip can't cost the report its "applied" marks.
  const body = await readBody(req, BODY.job);
  const at = now();
  const applied = list(body.applied ?? [], "applied", 2000).map((v, i) => {
    const a = obj(v, `applied[${i}]`);
    return {
      story: text(a.story, `applied[${i}].story`, 200),
      key: text(a.headline_key, `applied[${i}].headline_key`, 500),
      at: int(a.decided_at, `applied[${i}].decided_at`),
    };
  });
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
            mode: oneOf(r.mode, "run.mode", ["full", "apply", "local"] as const),
            status: oneOf(r.status, "run.status", ["success", "partial", "failure"] as const),
            started_at: int(r.started_at, "run.started_at"),
            finished_at: int(r.finished_at, "run.finished_at"),
            summary: clip(r.summary, 500),
            url: link(r.url),
          };
        })();

  const statements: D1PreparedStatement[] = [];
  for (const chunk of jsonChunks(applied)) {
    const reported = `(story, headline_key, decided_at) IN
      (SELECT json_extract(value, '$.story'), json_extract(value, '$.key'), json_extract(value, '$.at') FROM json_each(?1))`;
    statements.push(
      env.DB.prepare(`DELETE FROM open_decisions WHERE decision = 'withdrawn' AND ${reported}`).bind(chunk),
      env.DB.prepare(`UPDATE open_decisions SET applied_at = ?2 WHERE applied_at IS NULL AND decision != 'withdrawn' AND ${reported}`).bind(
        chunk,
        at,
      ),
    );
  }
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
