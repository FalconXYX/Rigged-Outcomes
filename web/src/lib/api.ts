// The site's API (worker/: a Cloudflare Worker and its D1 database; the
// shapes below are the contract it shares with watchlist/sync.py, and
// docs/DEPLOY.md sets it up). Visitors write three things through it:
// timeline markers, the site's one watchlist of open markets, and closed
// markets added to the main page; the owner's review page (#/review) reads
// them and decides the open list's queue. A build without VITE_API_URL and VITE_TURNSTILE_SITEKEY
// makes no call at all, and an API that is down, over its free daily limit
// or slow reads the same: every control still works and remembers what this browser did.
// Nothing here throws.
import { BotError, botToken, type BotAction } from "./turnstile";

const API = (import.meta.env.VITE_API_URL ?? "").trim().replace(/\/+$/, "");
const SITEKEY = (import.meta.env.VITE_TURNSTILE_SITEKEY ?? "").trim();
/** false: no API in this build (no network calls; the controls keep what this browser did) */
export const apiConfigured = !!API && !!SITEKEY;

const GET_MS = 4000;
const SEND_MS = 15000;

/* ── The contract's shapes (worker/src; watchlist/sync.py mirrors them) ─── */

export type Headline = { title: string; source: string | null; published: string | null; url: string | null };
/** key = opennow.headline_key(headline) */
export type Candidate = Headline & { key: string };
/** opennow Run.awaiting() item, plus candidates[].key */
export type QueueItem = {
  n: number;
  story: string;
  place: "new step" | "stays" | "gone quiet" | "dated";
  why: "on the list" | "awaiting approval" | "one reading" | "dated, on the list";
  kind: string | null;
  group: number | null;
  question: string;
  who_knows: string;
  polymarket: string;
  /** null when it wasn't an https:// link (the API drops it) */
  url: string | null;
  reveal: string | null;
  candidates: Candidate[];
  model_copy_unmatched: string[];
};
export type Queue = { day: string; run_at: string; steps_by: "person" | "model"; items: QueueItem[] };
export type DecisionValue = "step" | "not a step" | "card approved";
export type Decision = {
  story: string;
  headline_key: string;
  decision: DecisionValue;
  headline: Headline | null;
  day: string;
  decided_at: number;
  applied_at: number | null;
};
/** approved: the next run of the job adds it; added: it is on the main page; failed: the job couldn't add it (the review page says why) */
export type RequestStatus = "approved" | "added" | "failed";
export type PublicRequest = { status: RequestStatus; name: string | null; decided_at: number | null; done_at: number | null };
export type AddRequest = PublicRequest & {
  condition_id: string;
  slug: string;
  question: string;
  url: string;
  winner: string;
  closed: string;
  day: string;
  note: string | null;
  requested_at: number;
};
export type Marker = { id: string; t: number; label: string; source: "manual" | "ai"; created_at: number };
export type AdminMarker = Marker & { market: string; deleted_at: number | null; deleted_by: "owner" | "visitor" | null };
/** a story on the site's watchlist, since it was put on */
export type Watched = { story: string; since: number; question: string | null; url: string | null };
export type JobRun = {
  id: number;
  mode: "full" | "apply" | "local";
  status: "success" | "partial" | "failure";
  started_at: number;
  finished_at: number;
  summary: string | null;
  url: string | null;
};
export type Overview = {
  now: number;
  settings: { writes: "on" | "off" };
  dispatch: boolean;
  queue: Queue | null;
  decisions: Decision[];
  requests: AddRequest[];
  markers: AdminMarker[];
  watched: Watched[];
  runs: JobRun[];
};

/* ── Words ────────────────────────────────────────────────────────────── */

/**
 * What a visitor reads for each error code (the API's own `message` is never
 * shown to visitors). Anything else, or no answer at all, reads `other`.
 */
export const WORDS: Record<string, string> = {
  limit: "You've reached today's limit. Try again tomorrow (UTC).",
  busy: "The site has taken all it can today. Try again tomorrow.",
  bot_check: "The bot check didn't pass. Try again.",
  bot_script: "Couldn't load the bot check. Allow challenges.cloudflare.com and try again.",
  paused: "Visitors' changes are paused right now.",
  bad_label: "That label can't be used: plain words, no links, up to 40 characters.",
  bad_time: "Pick a time within this market's timeline.",
  full: "This timeline has as many markers as it can hold.",
  timeline_busy: "This timeline can't take more new markers today. Try again tomorrow (UTC).",
  duplicate: "That marker is already on this timeline.",
  not_yours: "Markers can only be removed by whoever added them, within 24 hours.",
  unknown_story: "This market has left the list, so it can't be watched.",
  not_listed: "This market is no longer on the list.",
  on_main: "It's already on the main page.",
  unauthorized: "That token was not accepted.",
  locked: "Too many wrong tokens today. Try again tomorrow (UTC).",
  other: "Couldn't send right now. Try again later.",
};

const wordsFor = (code: string) => WORDS[code] ?? WORDS.other;

/* ── Calls ────────────────────────────────────────────────────────────── */

export type Sent<T> = { ok: true; data: T } | { ok: false; code: string; words: string; message?: string };

type Raw = { status: number; body: unknown } | null;

/** One request: its status and parsed JSON body (null for none), or null when it never got an answer. */
async function call(method: string, path: string, body: unknown, headers: Record<string, string>, ms: number): Promise<Raw> {
  if (!apiConfigured) return null;
  const ctrl = new AbortController();
  const timer = window.setTimeout(() => ctrl.abort(), ms);
  try {
    const res = await fetch(`${API}${path}`, {
      method,
      headers: body === undefined ? headers : { "content-type": "application/json", ...headers },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: ctrl.signal,
      credentials: "omit",
      cache: method === "GET" ? "no-cache" : "no-store",
    });
    const text = await res.text();
    let parsed: unknown = null;
    if (text) {
      try {
        parsed = JSON.parse(text);
      } catch {
        // Cloudflare's own pages (error 1027 when the free daily limit is spent) are HTML
        return { status: res.ok ? 502 : res.status, body: null };
      }
    }
    return { status: res.status, body: parsed };
  } catch {
    return null;
  } finally {
    window.clearTimeout(timer);
  }
}

function result<T>(raw: Raw): Sent<T> {
  if (!raw) return { ok: false, code: "network", words: WORDS.other };
  if (raw.status >= 200 && raw.status < 300) return { ok: true, data: raw.body as T };
  const b = (raw.body ?? {}) as { error?: unknown; message?: unknown };
  const code = typeof b.error === "string" ? b.error : "server";
  return { ok: false, code, words: wordsFor(code), message: typeof b.message === "string" ? b.message : undefined };
}

/** GET a visitor endpoint: its JSON, or null on any failure (no API, down, slow, an error status, not JSON). */
export async function apiGet<T>(path: string): Promise<T | null> {
  const raw = await call("GET", path, undefined, {}, GET_MS);
  return raw && raw.status >= 200 && raw.status < 300 && raw.body != null ? (raw.body as T) : null;
}

/**
 * A visitor write. With `bot`, it first gets a bot-check token (shown in
 * `slot` if Cloudflare asks for a click) and sends it as `token`.
 */
export async function apiSend<T>(
  method: "POST" | "PUT" | "DELETE",
  path: string,
  body?: Record<string, unknown>,
  opts: { bot?: BotAction; slot?: HTMLElement | null; headers?: Record<string, string> } = {},
): Promise<Sent<T>> {
  if (!apiConfigured) return { ok: false, code: "off", words: WORDS.other };
  let payload = body;
  if (opts.bot) {
    try {
      payload = { ...body, token: await botToken(SITEKEY, opts.bot, opts.slot) };
    } catch (e) {
      const code = e instanceof BotError ? e.code : "bot_check";
      return { ok: false, code, words: wordsFor(code) };
    }
  }
  return result<T>(await call(method, path, payload, opts.headers ?? {}, SEND_MS));
}

/* ── The owner's token (#/review) ─────────────────────────────────────── */
// Kept in this page's memory only, never in localStorage or sessionStorage:
// every GitHub Pages site of the account shares the parthjain.ca origin, so
// any script on any of them could read browser storage. Reloading the page
// asks for the token again (a password manager fills it).

let owner: string | null = null;

export function ownerToken(): string | null {
  return owner;
}

export function setOwner(token: string) {
  forgetOwner();
  owner = token;
}

export function forgetOwner() {
  owner = null;
  // an earlier build of the review page could keep it in browser storage: never leave a copy there
  writeKey("ro-owner", null, "session");
  writeKey("ro-owner", null, "local");
}

const bearer = (): Record<string, string> => {
  const t = ownerToken();
  return t ? { authorization: `Bearer ${t}` } : {};
};

export async function ownerGet<T>(path: string): Promise<Sent<T>> {
  if (!apiConfigured) return { ok: false, code: "off", words: WORDS.other };
  return result<T>(await call("GET", path, undefined, bearer(), SEND_MS));
}

export async function ownerSend<T>(method: "POST" | "PUT" | "DELETE", path: string, body?: Record<string, unknown>): Promise<Sent<T>> {
  if (!apiConfigured) return { ok: false, code: "off", words: WORDS.other };
  return result<T>(await call(method, path, body, bearer(), SEND_MS));
}

/* ── What this browser remembers ──────────────────────────────────────── */
// The undo token for each marker this browser added (`ro-undo:<id>`, 24 hours)
// and the watchlist as this browser last saw or set it (`ro-watch:<story>`).
// Storage can be off (a private window, blocked site data): then this tab's
// memory holds them.

const memory = new Map<string, string>();

function storage(kind: "local" | "session"): Storage | null {
  try {
    return kind === "local" ? window.localStorage : window.sessionStorage;
  } catch {
    return null;
  }
}

function readKey(key: string, kind: "local" | "session" = "local"): string | null {
  try {
    const v = storage(kind)?.getItem(key);
    if (v != null) return v;
  } catch {
    /* fall through to memory */
  }
  return memory.get(`${kind}:${key}`) ?? null;
}

function writeKey(key: string, value: string | null, kind: "local" | "session" = "local") {
  const k = `${kind}:${key}`;
  if (value == null) memory.delete(k);
  else memory.set(k, value);
  try {
    const s = storage(kind);
    if (value == null) s?.removeItem(key);
    else s?.setItem(key, value);
  } catch {
    /* memory has it */
  }
}

/** A visitor's own marker can be removed for this long after it was added (the API checks the same). */
export const UNDO_SECONDS = 24 * 3600;

export function saveUndo(id: string, token: string) {
  writeKey(`ro-undo:${id}`, JSON.stringify({ token, at: Math.floor(Date.now() / 1000) }));
}

/** The undo token for a marker this browser added, while it still works; null otherwise. */
export function undoOf(id: string): string | null {
  const raw = readKey(`ro-undo:${id}`);
  if (!raw) return null;
  try {
    const { token, at } = JSON.parse(raw) as { token?: unknown; at?: unknown };
    if (typeof token === "string" && typeof at === "number" && Date.now() / 1000 - at < UNDO_SECONDS) return token;
  } catch {
    /* unreadable: dropped below */
  }
  writeKey(`ro-undo:${id}`, null);
  return null;
}

export const dropUndo = (id: string) => writeKey(`ro-undo:${id}`, null);

export const watchedHere = (story: string) => readKey(`ro-watch:${story}`) === "1";
export const setWatchedHere = (story: string, on: boolean) => writeKey(`ro-watch:${story}`, on ? "1" : null);

/** A closed market this browser added to the main page (kept when the API can't take it). */
export const requestedHere = (cid: string) => readKey(`ro-req:${cid}`) === "1";
export const setRequestedHere = (cid: string) => writeKey(`ro-req:${cid}`, "1");

/** A market's markers added in this browser while the API can't take them. */
export function localMarkers<T>(market: string): T[] {
  try {
    const v = JSON.parse(readKey(`ro-markers:${market}`) ?? "[]");
    return Array.isArray(v) ? (v as T[]) : [];
  } catch {
    return [];
  }
}
export const setLocalMarkers = (market: string, events: unknown[]) =>
  writeKey(`ro-markers:${market}`, events.length ? JSON.stringify(events) : null);

/** Answers that mean "the API can't take it right now", as opposed to a refusal a visitor should read. */
export const OFFLINE = new Set(["network", "off", "paused", "server", "other"]);
