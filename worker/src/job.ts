// The daily job's endpoints, behind JOB_TOKEN (watchlist/sync.py): what to apply, the open list's review
// queue, the catalog of what visitors may write to, the run's report (which also prunes old rows), and an
// export for the backup on the public watchlist-data branch: no visitor or undo hashes, and only markers that
// are live and at least 2 days old, so the owner has two days to delete an abusive one before it reaches git.

/** A marker reaches the public backup only once it has been up this long without being deleted. */
const BACKUP_MARKER_AGE = 2 * DAY;

import {
  BODY,
  CONDITION_ID,
  type Ctx,
  DAY,
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
import { MARKER_COLUMNS, decision } from "./owner";
import { forgetMarkers } from "./public";

/** opennow offers at most QUEUE_MAX (15) stories a day; twice that leaves room. */
const QUEUE_ITEMS = 30;
const QUEUE_DAYS = 7;
/** One queue item is one row, well under D1's 100 KB statement cap even escaped inside json_each's text (a real item is about 3 KB). */
const ITEM_BYTES = 60_000;

/** What the job applies: pending decisions (a "withdrawn" one included: the job removes it from its store), approved requests, watch counts. */
export async function work({ env }: Ctx): Promise<Response> {
  const [decisions, requests, watched] = await env.DB.batch([
    env.DB.prepare("SELECT * FROM open_decisions WHERE applied_at IS NULL ORDER BY decided_at"),
    env.DB.prepare("SELECT * FROM requests WHERE status = 'approved' ORDER BY decided_at"),
    env.DB.prepare("SELECT story, count(*) AS count FROM watches GROUP BY story ORDER BY story"),
  ]);
  return json({ now: now(), decisions: decisions.results.map(decision), requests: requests.results, watched: watched.results });
}

/** Replace one day's review queue (opennow's `awaiting`, each candidate with its headline key), keeping 7 days. */
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
    pruneQueue(env),
  ]);
  return noContent();
}

const pruneQueue = (env: Ctx["env"]) =>
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
 * closed picks (requests) and open cards (watches). Each section sent replaces its kind in one transaction; a
 * section left out is kept. A repeated id keeps its last entry. An entry that doesn't fit (a markets.json name
 * with capitals, a pick with no link) is left out and named in `skipped`, so one odd entry can't stop the rest.
 */
export async function putCatalog({ req, env }: Ctx): Promise<Response> {
  const body = await readBody(req, BODY.job);
  const at = now();
  const statements: D1PreparedStatement[] = [];
  const skipped: string[] = [];
  for (const [section, spec] of Object.entries(CATALOG_KINDS)) {
    if (body[section] === undefined) continue;
    const rows = new Map<string, Entry>();
    list(body[section], section, 5000).forEach((v, i) => {
      try {
        const e = spec.entry(v, `${section}[${i}]`);
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

/**
 * The run's report. `applied`: decisions the job saved and pushed, marked applied only while their decided_at is
 * unchanged (one decided again meanwhile stays pending); a withdrawn one the job has removed from its store goes.
 * `requests`: what adding approved markets did, recorded only while a request is still approved. `run`: one
 * job_runs row. Then the old rows go: limits after 3 days, runs beyond 200, deleted markers after 30 days, visitor
 * hashes on markers after 30 days, watches over 30 days old on a story no longer listed, withdrawals no run
 * reported within 30 days, queue days beyond 7.
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
  const old = new Date((at - 3 * DAY) * 1000).toISOString().slice(0, 10);
  statements.push(
    env.DB.prepare("DELETE FROM quota WHERE day < ?").bind(old),
    env.DB.prepare("DELETE FROM job_runs WHERE id NOT IN (SELECT id FROM job_runs ORDER BY id DESC LIMIT 200)"),
    env.DB.prepare("DELETE FROM markers WHERE deleted_at IS NOT NULL AND deleted_at < ?").bind(at - 30 * DAY),
    env.DB.prepare("UPDATE markers SET visitor = NULL WHERE visitor IS NOT NULL AND created_at < ?").bind(at - 30 * DAY),
    env.DB.prepare("DELETE FROM watches WHERE created_at < ? AND story NOT IN (SELECT id FROM catalog WHERE kind = 'open')").bind(
      at - 30 * DAY,
    ),
    env.DB.prepare("DELETE FROM open_decisions WHERE decision = 'withdrawn' AND decided_at < ?").bind(at - 30 * DAY),
    pruneQueue(env),
  );
  await env.DB.batch(statements);
  return noContent();
}

/**
 * The backup the job commits to the public watchlist-data branch: everything but visitor and undo hashes, and of
 * the markers only the live ones at least BACKUP_MARKER_AGE old (a deleted or brand-new label never reaches git;
 * D1's Time Travel covers the rest for 7 days).
 */
export async function exportAll({ env }: Ctx): Promise<Response> {
  const [settings, markers, watches, requests, decisions, runs] = await env.DB.batch([
    env.DB.prepare("SELECT key, value FROM settings ORDER BY key"),
    env.DB.prepare(`SELECT ${MARKER_COLUMNS} FROM markers WHERE deleted_at IS NULL AND created_at < ? ORDER BY created_at, id`).bind(
      now() - BACKUP_MARKER_AGE,
    ),
    env.DB.prepare("SELECT story, count(*) AS count, min(created_at) AS since FROM watches GROUP BY story ORDER BY story"),
    env.DB.prepare("SELECT * FROM requests ORDER BY requested_at, condition_id"),
    env.DB.prepare("SELECT * FROM open_decisions ORDER BY story, headline_key"),
    env.DB.prepare("SELECT * FROM job_runs ORDER BY id"),
  ]);
  return json({
    exported_at: now(),
    settings: Object.fromEntries((settings.results as { key: string; value: string }[]).map((r) => [r.key, r.value])),
    markers: markers.results,
    watch_counts: watches.results,
    requests: requests.results,
    decisions: decisions.results.map(decision),
    runs: runs.results,
  });
}
