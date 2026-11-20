// The job's own data, behind JOB_TOKEN (watchlist/blobs.py, migrations/0003_job_blobs.sql, the names in
// CONTRACT.blob_names): its memory between runs (state), the files the site reads (site:index.json, the two lists
// and each added market's page, served by data.ts), each added market's record (market:<name>) and a job's
// scratch data. Each is one blob, a gzip-compressed JSON document the job sends as base64 parts: it puts the new
// version's parts beside the live ones, then commits, which moves job_blobs to the new version and drops the
// old parts in one transaction (the state's and the lists' last KEPT versions stay instead, in
// job_blob_versions, and committing one of them again restores it). A reader sees the old version or the new
// one, never half of either. The Worker never decodes, unzips or hashes a part (a free-plan request has 10 ms
// of CPU): it stores and returns text, and the job checks the sha256 end to end.

import { CONTRACT } from "./contract";
import { BODY, type Ctx, DAY, type Env, fail, int, json, noContent, now, obj, readBody } from "./lib";

const BLOB_NAME = new RegExp(Object.values(CONTRACT.blob_names).join("|"));
const VERSION = /^[0-9a-f]{32}$/;
/** One part's base64 text at most (the job sends 500,000); D1 holds up to 2 MB a row. */
const PART_CHARS = 1_000_000;
/** Parts a version may have: the news cache is about 25 at the job's size, 134 at 90,000 characters. */
const MAX_PARTS = 400;
const META_CHARS = 16_384;
const BASE64 = /^[A-Za-z0-9+/]+={0,2}$/;
const PREFIX = /^[a-z0-9:./_-]{0,60}$/;
/** An upload never committed (a run that stopped halfway) is pruned once it is this old. */
const ORPHAN_AGE = DAY;
/** The blobs whose replaced versions are kept, KEPT of each: a bad save of the job's memory, the index or a list can be undone. */
const HISTORY: readonly string[] = CONTRACT.history_blobs;
const KEPT = 3;
/** Added markets at most, records and pages counted apart (visitors may send 5 to 50 a day): the database's 500 MB must keep room for the rest. */
const MAX_MARKETS = 200;
/** Parts the added markets' records and pages may hold together: 300 MB at the job's 500,000 characters a part. */
const MARKET_PARTS = 600;
/**
 * Every blob's parts together (live and kept, a part counted at the job's full 500,000 characters) a new added
 * market may bring to at most: 350 MB, which keeps D1's 500 MB with room for the queue, the news and the rest.
 */
const STORE_CHARS = 350_000_000;
/** The two kinds of blob an added market has, as primary-key ranges (";" sorts right after ":", "0" right after "/"). */
const MARKET_RANGES = { record: ["market:", "market;"], page: ["site:markets/", "site:markets0"] } as const;
/** The page name inside market:<name> or site:markets/<name>.json, or null. */
const pageOf = (name: string) => /^market:(.+)$/.exec(name)?.[1] ?? /^site:markets\/(.+)\.json$/.exec(name)?.[1] ?? null;

interface BlobRow {
  name: string;
  version: string;
  parts: number;
  bytes: number;
  sha256: string;
  meta: string;
  updated_at: number;
}

/** A job_blobs row as the job reads it (a Head): its meta parsed. */
const head = (r: BlobRow) => ({
  name: r.name,
  version: r.version,
  parts: r.parts,
  bytes: r.bytes,
  sha256: r.sha256,
  meta: JSON.parse(r.meta) as Record<string, unknown>,
  updated_at: r.updated_at,
});

function blobName(v: string): string {
  if (!BLOB_NAME.test(v)) {
    fail(400, "bad_request", "The blob name must be state, site:index.json, site:closed.json, site:open.json, site:markets/<page name>.json, market:<page name> or scratch:<run>:<name>.");
  }
  return v;
}

function version(v: unknown, field = "version"): string {
  if (typeof v !== "string" || !VERSION.test(v)) fail(400, "bad_request", `${field} must be 32 lowercase hex characters.`);
  return v;
}

function partNo(v: string): number {
  const n = /^(0|[1-9]\d{0,2})$/.test(v) ? Number(v) : -1;
  if (n < 0 || n >= MAX_PARTS) fail(400, "bad_request", `The part must be a whole number from 0 to ${MAX_PARTS - 1}.`);
  return n;
}

/** Every blob whose name starts with `prefix` (none: all), by name. */
export async function listBlobs({ env, url }: Ctx): Promise<Response> {
  const prefix = url.searchParams.get("prefix") ?? "";
  if (!PREFIX.test(prefix)) fail(400, "bad_request", "prefix must be up to 60 of a-z, 0-9 and : . / _ -.");
  const rows = await env.DB.prepare("SELECT * FROM job_blobs WHERE substr(name, 1, length(?1)) = ?1 ORDER BY name LIMIT 1000")
    .bind(prefix)
    .all<BlobRow>();
  return json({ blobs: rows.results.map(head) });
}

export async function getBlob({ env, params }: Ctx): Promise<Response> {
  const row = await env.DB.prepare("SELECT * FROM job_blobs WHERE name = ?").bind(blobName(params[0])).first<BlobRow>();
  if (!row) fail(404, "no_blob", "No such blob.");
  return json(head(row));
}

/**
 * One part of a version not yet live, in one statement: a live or kept version's parts are never rewritten (409
 * live). Sending a part again replaces it, so a retry is safe.
 */
export async function putPart({ req, env, params }: Ctx): Promise<Response> {
  const name = blobName(params[0]);
  const ver = version(params[1]);
  const part = partNo(params[2]);
  const body = await readBody(req, BODY.job);
  const data = body.data;
  if (typeof data !== "string" || data.length < 1 || data.length > PART_CHARS || !BASE64.test(data)) {
    fail(400, "bad_request", `data must be base64 text of 1 to ${PART_CHARS} characters.`);
  }
  // the site's files are served part by part (data.ts), so each part must decode on its own
  if (name.startsWith("site:") && data.length % 4 !== 0) fail(400, "bad_request", "A site file's part must be a multiple of 4 base64 characters.");
  const r = await env.DB.prepare(
    `INSERT INTO job_blob_parts (name, version, part, data, created_at)
     SELECT ?1, ?2, ?3, ?4, ?5
     WHERE NOT EXISTS (SELECT 1 FROM job_blobs WHERE name = ?1 AND version = ?2)
       AND NOT EXISTS (SELECT 1 FROM job_blob_versions WHERE name = ?1 AND version = ?2)
     ON CONFLICT (name, version, part) DO UPDATE SET data = excluded.data, created_at = excluded.created_at`,
  )
    .bind(name, ver, part, data, now())
    .run();
  if (!r.meta.changes) fail(409, "live", "That version is live or kept; its parts can't be changed.");
  return noContent();
}

export async function getPart({ env, params }: Ctx): Promise<Response> {
  const data = await env.DB.prepare("SELECT data FROM job_blob_parts WHERE name = ? AND version = ? AND part = ?")
    .bind(blobName(params[0]), version(params[1]), partNo(params[2]))
    .first<string>("data");
  if (data === null) fail(404, "no_part", "No such part (the blob may have been replaced since its head was read).");
  return json({ data });
}

/**
 * Make an uploaded version the live one. `expect`, when sent, is the version the job last read (null: the blob
 * must not exist yet), so two runs can't both save over the same state: the later one is 409 moved. Committing
 * the live version again (a retry whose answer was lost) is 200 when its sha256 matches. Every part from 0 to
 * parts-1 must be there (409 incomplete). A new added market (its market:<name> record or its site:markets/<name>.json
 * page) is refused past MAX_MARKETS of its kind, MARKET_PARTS for both together, or STORE_CHARS for every blob
 * (409 full). In one
 * transaction the pointer moves and the old version's parts go, or, for a HISTORY name, the old
 * version is kept, the committed one leaves the kept ones (a restore) and the oldest beyond KEPT go.
 */
export async function commitBlob({ req, env, params }: Ctx): Promise<Response> {
  const name = blobName(params[0]);
  const ver = version(params[1]);
  const body = await readBody(req, BODY.job);
  const parts = int(body.parts, "parts");
  if (parts < 1 || parts > MAX_PARTS) fail(400, "bad_request", `parts must be from 1 to ${MAX_PARTS}.`);
  const bytes = int(body.bytes, "bytes");
  if (bytes < 0) fail(400, "bad_request", "bytes must be 0 or more.");
  const sha256 = body.sha256;
  if (typeof sha256 !== "string" || !/^[0-9a-f]{64}$/.test(sha256)) fail(400, "bad_request", "sha256 must be 64 lowercase hex characters.");
  const meta = JSON.stringify(obj(body.meta, "meta"));
  if (meta.length > META_CHARS) fail(400, "bad_request", `meta must be at most ${META_CHARS} characters as JSON.`);
  const checked = "expect" in body;
  const expected = checked && body.expect !== null ? version(body.expect, "expect") : null;

  const market = pageOf(name) !== null;
  const [lo, hi] = name.startsWith("market:") ? MARKET_RANGES.record : MARKET_RANGES.page;
  const [curRows, haveRows, marketRows] = await env.DB.batch([
    env.DB.prepare("SELECT * FROM job_blobs WHERE name = ?").bind(name),
    env.DB.prepare("SELECT count(*) AS n, min(part) AS lo, max(part) AS hi FROM job_blob_parts WHERE name = ? AND version = ?").bind(
      name,
      ver,
    ),
    // the added markets' blobs of this kind, and the parts of both kinds, through the primary key; only for a market's commit
    ...(market
      ? [
          env.DB.prepare(
            `SELECT count(CASE WHEN name > ?1 AND name < ?2 THEN 1 END) AS n, coalesce(sum(parts), 0) AS parts,
                    (SELECT coalesce(sum(parts), 0) FROM job_blobs) + (SELECT coalesce(sum(parts), 0) FROM job_blob_versions) AS stored
             FROM job_blobs
             WHERE (name > 'market:' AND name < 'market;') OR (name > 'site:markets/' AND name < 'site:markets0')`,
          ).bind(lo, hi),
        ]
      : []),
  ]);
  const cur = (curRows.results[0] as BlobRow | undefined) ?? null;
  const have = haveRows.results[0] as { n: number; lo: number | null; hi: number | null };
  const markets = (marketRows?.results[0] as { n: number; parts: number; stored: number } | undefined) ?? { n: 0, parts: 0, stored: 0 };
  if (cur?.version === ver) {
    if (cur.sha256 === sha256) return json(head(cur));
    fail(409, "live", "That version is already live with another sha256.");
  }
  if (checked && expected !== (cur?.version ?? null)) fail(409, "moved", "The blob changed since the version you expected.");
  if (have.n !== parts || have.lo !== 0 || have.hi !== parts - 1) {
    fail(409, "incomplete", `The version has ${have.n} of its ${parts} parts.`);
  }
  const storeFull = (markets.stored + parts) * CONTRACT.data.part_chars > STORE_CHARS;
  if (market && !cur && (markets.n >= MAX_MARKETS || markets.parts + parts > MARKET_PARTS || storeFull)) {
    fail(409, "full", `The main page holds ${markets.n} added markets in ${markets.parts} parts, its limit.`);
  }

  const at = now();
  let row: BlobRow | undefined;
  if (cur) {
    // every statement after the first runs only once the pointer is on the new version: a commit that lost the
    // race changes nothing
    const live = "EXISTS (SELECT 1 FROM job_blobs WHERE name = ?1 AND version = ?2)";
    const beyond = `SELECT version FROM job_blob_versions WHERE name = ?1 ORDER BY seq DESC LIMIT -1 OFFSET ${KEPT}`;
    const [moved] = await env.DB.batch([
      env.DB.prepare(
        `UPDATE job_blobs SET version = ?1, parts = ?2, bytes = ?3, sha256 = ?4, meta = ?5, updated_at = ?6
         WHERE name = ?7 AND version = ?8 RETURNING *`,
      ).bind(ver, parts, bytes, sha256, meta, at, name, cur.version),
      ...(HISTORY.includes(name)
        ? [
            env.DB.prepare(
              `INSERT INTO job_blob_versions (name, version, seq, parts, bytes, sha256, meta, updated_at, replaced_at)
               SELECT ?1, ?3, (SELECT coalesce(max(seq), 0) + 1 FROM job_blob_versions WHERE name = ?1), ?4, ?5, ?6, ?7, ?8, ?9
               WHERE ${live}
               ON CONFLICT (name, version) DO NOTHING`,
            ).bind(name, ver, cur.version, cur.parts, cur.bytes, cur.sha256, cur.meta, cur.updated_at, at),
            env.DB.prepare(`DELETE FROM job_blob_versions WHERE name = ?1 AND version = ?2 AND ${live}`).bind(name, ver),
            env.DB.prepare(`DELETE FROM job_blob_parts WHERE name = ?1 AND version IN (${beyond}) AND ${live}`).bind(name, ver),
            env.DB.prepare(`DELETE FROM job_blob_versions WHERE name = ?1 AND version IN (${beyond}) AND ${live}`).bind(name, ver),
          ]
        : [
            env.DB.prepare(`DELETE FROM job_blob_parts WHERE name = ?1 AND version = ?3 AND ${live}`).bind(name, ver, cur.version),
          ]),
    ]);
    row = moved.results[0] as BlobRow | undefined;
  } else {
    row =
      (await env.DB.prepare(
        `INSERT INTO job_blobs (name, version, parts, bytes, sha256, meta, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)
         ON CONFLICT (name) DO NOTHING RETURNING *`,
      )
        .bind(name, ver, parts, bytes, sha256, meta, at)
        .first<BlobRow>()) ?? undefined;
  }
  if (!row) fail(409, "moved", "Another save of this blob got there first.");
  return json(head(row));
}

/** The kept versions of a HISTORY blob, newest first: each a head with the time it was replaced. */
export async function listVersions({ env, params }: Ctx): Promise<Response> {
  const name = blobName(params[0]);
  const rows = await env.DB.prepare("SELECT * FROM job_blob_versions WHERE name = ? ORDER BY seq DESC")
    .bind(name)
    .all<BlobRow & { replaced_at: number }>();
  return json({ versions: rows.results.map((r) => ({ ...head(r), replaced_at: r.replaced_at })) });
}

/**
 * Take a market off the main page (backend.site's remove: its market:<name> record and its
 * site:markets/<name>.json page, then the index saved again) or drop a scratch blob, with the live parts. The
 * request that added the market is marked failed, "taken off the main page", so the site stops linking to it
 * (visitors see it as removed) and no one can send it again. The state, the index and the lists are never
 * deleted (400): a save replaces them, and a kept version restores one.
 */
export async function deleteBlob({ env, params }: Ctx): Promise<Response> {
  const name = blobName(params[0]);
  if (HISTORY.includes(name)) fail(400, "bad_request", "The state, the index and the lists can't be deleted; save over them or restore a kept version.");
  const page = pageOf(name);
  const [, , gone] = await env.DB.batch([
    env.DB.prepare(
      `UPDATE requests SET status = 'failed', note = 'taken off the main page', done_at = ?3
       WHERE status = 'added' AND ?2 IS NOT NULL
         AND (name = ?2 OR condition_id = (SELECT lower(json_extract(meta, '$.condition_id')) FROM job_blobs WHERE name = ?1))`,
    ).bind(name, page, now()),
    env.DB.prepare("DELETE FROM job_blob_parts WHERE name = ?1 AND version = (SELECT version FROM job_blobs WHERE name = ?1)").bind(name),
    env.DB.prepare("DELETE FROM job_blobs WHERE name = ? RETURNING name").bind(name),
  ]);
  if (!gone.results.length) fail(404, "no_blob", "No such blob.");
  return noContent();
}

/** For the cron's daily prune (cron.ts): parts of a version neither live nor kept, once ORPHAN_AGE old. */
export const pruneParts = (env: Env, at: number) =>
  env.DB.prepare(
    `DELETE FROM job_blob_parts
     WHERE created_at < ?
       AND NOT EXISTS (SELECT 1 FROM job_blobs b WHERE b.name = job_blob_parts.name AND b.version = job_blob_parts.version)
       AND NOT EXISTS (SELECT 1 FROM job_blob_versions v WHERE v.name = job_blob_parts.name AND v.version = job_blob_parts.version)`,
  ).bind(at - ORPHAN_AGE);
