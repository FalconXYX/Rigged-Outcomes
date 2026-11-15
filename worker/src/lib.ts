// What every endpoint shares: responses and errors, bodies, the visitor hash, token checks, the daily
// limits, the bot check (Cloudflare Turnstile) and the label and slug rules. The shapes, codes and limits
// are the contract in docs/DEPLOY.md, which the site (web/src/lib/api.ts) and the job (watchlist/sync.py)
// share with this Worker.

export interface Env {
  DB: D1Database;
  // secrets (wrangler secret put; .dev.vars locally)
  TURNSTILE_SECRET: string;
  OWNER_TOKEN: string;
  JOB_TOKEN: string;
  VISITOR_SALT: string;
  GITHUB_DISPATCH_TOKEN?: string;
  // vars (wrangler.toml)
  ALLOWED_ORIGINS: string;
  TURNSTILE_HOSTNAMES: string;
  TURNSTILE_STRICT: string;
  GITHUB_REPO: string;
  GITHUB_WORKFLOW: string;
  DEV: string;
  /** DEV=on only: where "Apply decisions now" sends its dispatch (the test's stand-in for api.github.com). */
  GITHUB_API?: string;
}

/** One request as a handler sees it: params are the route's captured path segments. */
export interface Ctx {
  req: Request;
  env: Env;
  url: URL;
  params: string[];
}

export const DAY = 86_400;
export const now = () => Math.floor(Date.now() / 1000);

/** Body caps: a visitor's write, the owner's review page, the daily job. */
export const BODY = { visitor: 4 * 1024, owner: 64 * 1024, job: 1024 * 1024 } as const;

export const MARKET_NAME = /^[a-z0-9_-]{1,40}$/;
export const PAGE_NAME = /^[a-z0-9][a-z0-9-]{1,31}$/;
export const CONDITION_ID = /^0x[0-9a-f]{64}$/;
export const DAY_TEXT = /^\d{4}-\d{2}-\d{2}$/;

// ---------- responses and errors ----------

/** A failed request: `code` is what the site keys its own words on; `message` is for the owner and logs only. */
export class HttpError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
  ) {
    super(message);
  }
}

export function fail(status: number, code: string, message: string): never {
  throw new HttpError(status, code, message);
}

export function json(data: unknown, status = 200, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "content-type": "application/json; charset=utf-8", "cache-control": "no-store", ...headers },
  });
}

export const noContent = () => new Response(null, { status: 204, headers: { "cache-control": "no-store" } });

// ---------- bodies and fields ----------

/**
 * The JSON object in a request body, read up to `limit` bytes: larger is 413 too_large (checked on the
 * declared length first, then while reading), anything but a JSON object is 400 bad_request. The body must be
 * sent as application/json (415 otherwise), so a page on another site can't post one without the CORS
 * preflight this Worker refuses it.
 */
export async function readBody(req: Request, limit: number): Promise<Record<string, unknown>> {
  if (!/^application\/json\s*(;|$)/i.test(req.headers.get("content-type") ?? "")) {
    fail(415, "bad_request", "The body must be sent as application/json.");
  }
  const declared = Number(req.headers.get("content-length") ?? 0);
  if (declared > limit) fail(413, "too_large", `The body is over ${limit} bytes.`);
  if (!req.body) fail(400, "bad_request", "A JSON body is required.");
  const reader = req.body.getReader();
  const chunks: Uint8Array[] = [];
  let size = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    size += value.byteLength;
    if (size > limit) {
      await reader.cancel();
      fail(413, "too_large", `The body is over ${limit} bytes.`);
    }
    chunks.push(value);
  }
  const bytes = new Uint8Array(size);
  let at = 0;
  for (const c of chunks) {
    bytes.set(c, at);
    at += c.byteLength;
  }
  let data: unknown;
  try {
    data = JSON.parse(new TextDecoder("utf-8", { fatal: true, ignoreBOM: false }).decode(bytes));
  } catch {
    fail(400, "bad_request", "The body is not JSON.");
  }
  if (!data || typeof data !== "object" || Array.isArray(data)) fail(400, "bad_request", "The body must be a JSON object.");
  return data as Record<string, unknown>;
}

/** A string field of 1 to `max` characters, or 400 bad_request naming it. */
export function text(v: unknown, field: string, max: number): string {
  if (typeof v !== "string" || v.length < 1 || v.length > max) {
    fail(400, "bad_request", `${field} must be a string of 1 to ${max} characters.`);
  }
  return v;
}

export function int(v: unknown, field: string): number {
  if (typeof v !== "number" || !Number.isSafeInteger(v)) fail(400, "bad_request", `${field} must be an integer.`);
  return v;
}

export function oneOf<T extends string>(v: unknown, field: string, options: readonly T[]): T {
  if (typeof v !== "string" || !options.includes(v as T)) {
    fail(400, "bad_request", `${field} must be one of: ${options.join(", ")}.`);
  }
  return v as T;
}

export function list(v: unknown, field: string, max: number): unknown[] {
  if (!Array.isArray(v) || v.length > max) fail(400, "bad_request", `${field} must be a list of at most ${max}.`);
  return v;
}

export function obj(v: unknown, field: string): Record<string, unknown> {
  if (!v || typeof v !== "object" || Array.isArray(v)) fail(400, "bad_request", `${field} must be an object.`);
  return v as Record<string, unknown>;
}

/** A comma-separated var ("a, b") as a list. */
export const names = (v: string | undefined) =>
  (v ?? "")
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);

/**
 * Rows as JSON text for `json_each(?)`, split so no one bound value passes `max` bytes (D1 caps a
 * statement at 100 KB); one statement per chunk keeps a big list within D1's 50 queries a request.
 */
export function jsonChunks(rows: unknown[], max = 90_000): string[] {
  const out: string[] = [];
  let part: string[] = [];
  let size = 2;
  for (const r of rows) {
    const s = JSON.stringify(r);
    if (part.length && size + s.length + 1 > max) {
      out.push(`[${part.join(",")}]`);
      part = [];
      size = 2;
    }
    part.push(s);
    size += s.length + 1;
  }
  if (part.length) out.push(`[${part.join(",")}]`);
  return out;
}

// ---------- hashes, visitors and tokens ----------

const enc = new TextEncoder();

const hex = (buf: ArrayBuffer) => [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");

export async function sha256hex(s: string): Promise<string> {
  return hex(await crypto.subtle.digest("SHA-256", enc.encode(s)));
}

/** n random bytes as hex (an undo token is 32 bytes: 64 hex characters). */
export function randomHex(n: number): string {
  return hex(crypto.getRandomValues(new Uint8Array(n)).buffer);
}

/** Equal strings, compared as SHA-256 digests in constant time (so neither length nor content leaks). */
export async function safeEqual(a: string, b: string): Promise<boolean> {
  const [x, y] = await Promise.all([
    crypto.subtle.digest("SHA-256", enc.encode(a)),
    crypto.subtle.digest("SHA-256", enc.encode(b)),
  ]);
  return crypto.subtle.timingSafeEqual(x, y);
}

const dev = (env: Env) => env.DEV === "on";

/**
 * The address a visitor counts under: an IPv4 address as it is, and an IPv6 one as its /64 network
 * ("v6:2001:db8:1:2::/64"), because one subscriber is usually given a whole /64 and could otherwise take a
 * fresh daily allowance from each of its addresses. An IPv4-mapped IPv6 address counts as its IPv4 address;
 * anything unreadable counts as it is.
 */
export function addressKey(ip: string): string {
  const a = ip.trim().toLowerCase().replace(/%.*$/, "");
  if (!a.includes(":")) return a;
  const mapped = /^[0:]*:ffff:(\d{1,3}(?:\.\d{1,3}){3})$/.exec(a);
  if (mapped) return mapped[1];
  const halves = a.split("::");
  if (halves.length > 2) return a;
  const part = (h: string) => (h ? h.split(":") : []);
  // an embedded IPv4 tail (::ffff:1.2.3.4 is handled above) fills two groups
  const groups = (h: string) => part(h).flatMap((g) => (g.includes(".") ? ["0", "0"] : [g]));
  const head = groups(halves[0]);
  const tail = halves.length === 2 ? groups(halves[1]) : [];
  const fill = 8 - head.length - tail.length;
  if (fill < 0 || (halves.length === 1 && fill !== 0)) return a;
  const all = [...head, ...Array(fill).fill("0"), ...tail];
  if (!all.every((g) => /^[0-9a-f]{1,4}$/.test(g))) return a;
  return `v6:${all
    .slice(0, 4)
    .map((g) => g.replace(/^0+(?=.)/, ""))
    .join(":")}::/64`;
}

/**
 * Who is asking, without keeping who they are: the first 32 hex characters of
 * HMAC-SHA256(VISITOR_SALT, addressKey(CF-Connecting-IP)). Visitors on one address (or one IPv6 /64) share
 * a hash, and so their watch state and daily limits; that is accepted. With DEV=on, an X-Test-Visitor header
 * stands in for the hash, and an X-Test-IP header for CF-Connecting-IP.
 */
export async function visitorOf(req: Request, env: Env): Promise<string> {
  let ip = req.headers.get("cf-connecting-ip") || "0.0.0.0";
  if (dev(env)) {
    const test = req.headers.get("x-test-visitor");
    if (test && /^[\w-]{1,64}$/.test(test)) return test;
    const testIp = req.headers.get("x-test-ip");
    if (testIp && /^[0-9a-fA-F:.]{2,45}$/.test(testIp)) ip = testIp;
  }
  if (!env.VISITOR_SALT) throw new Error("VISITOR_SALT is not set");
  const key = await crypto.subtle.importKey("raw", enc.encode(env.VISITOR_SALT), { name: "HMAC", hash: "SHA-256" }, false, [
    "sign",
  ]);
  return hex(await crypto.subtle.sign("HMAC", key, enc.encode(addressKey(ip)))).slice(0, 32);
}

/** Today's UTC date, which the daily limits count under. With DEV=on, X-Test-Day stands in (fresh counters per test run). */
export function utcDay(req?: Request, env?: Env): string {
  if (req && env && dev(env)) {
    const test = req.headers.get("x-test-day");
    if (test && /^[\w-]{1,24}$/.test(test)) return test;
  }
  return new Date().toISOString().slice(0, 10);
}

const AUTH_FAILS = 10;

/**
 * Owner and job sign-in: one bearer token each. The token is compared first, so the right one always gets
 * in (one shared network's wrong guesses can't lock the owner out). Wrong ones are counted per visitor; after
 * 10 in a UTC day that visitor's wrong tokens are answered 429 locked and no longer counted (which keeps the
 * writes down; guessing a 256-bit token is hopeless anyway).
 */
export async function authorize(req: Request, env: Env, who: "owner" | "job"): Promise<void> {
  const m = /^Bearer (\S{1,512})$/.exec(req.headers.get("authorization") ?? "");
  const secret = who === "owner" ? env.OWNER_TOKEN : env.JOB_TOKEN;
  if (m && secret && (await safeEqual(m[1], secret))) return;
  const visitor = await visitorOf(req, env);
  const day = utcDay(req, env);
  if (!(await counter(env, day, "auth_fail", visitor, AUTH_FAILS).first())) {
    fail(429, "locked", "Too many wrong tokens today. Try again tomorrow (UTC).");
  }
  fail(401, "unauthorized", "That token was not accepted.");
}

// ---------- daily limits ----------

/** [per visitor, whole site] per UTC day. */
export const LIMITS = {
  marker: [10, 300],
  watch: [30, 1000],
  request: [5, 50],
} as const;

/** New markers one market's timeline takes a UTC day, from everyone together (so no one fills it in a day). */
export const MARKERS_A_DAY_PER_MARKET = 20;

/** One counter, one statement: it counts the attempt and returns the new n, or returns nothing (and writes nothing) at the cap. */
const COUNT = `INSERT INTO quota (day, kind, visitor, n) VALUES (?1, ?2, ?3, 1)
ON CONFLICT (day, kind, visitor) DO UPDATE SET n = n + 1 WHERE n < ?4
RETURNING n`;

export const counter = (env: Env, day: string, kind: string, visitor: string, cap: number) =>
  env.DB.prepare(COUNT).bind(day, kind, visitor, cap);

/** A shared counter a write also spends, after the visitor's (e.g. one market's markers): 429 `code` at its cap. */
export type SharedLimit = { kind: string; cap: number; code: string; message: string };

/**
 * Spend one of today's writes of this kind: the visitor's count first (429 limit at its cap), then any
 * `also` counter (its own code), then the site's (429 busy). A later count is only touched once the earlier
 * ones allowed it, so one visitor at their limit can't use up everyone's; a visitor turned away by a later
 * cap loses that one count.
 */
export async function quota(req: Request, env: Env, kind: keyof typeof LIMITS, visitor: string, also: SharedLimit[] = []): Promise<void> {
  const day = utcDay(req, env);
  const [mine, site] = LIMITS[kind];
  if (!(await counter(env, day, kind, visitor, mine).first())) fail(429, "limit", `Today's limit of ${mine} ${kind} writes is reached.`);
  for (const a of also) if (!(await counter(env, day, a.kind, "*", a.cap).first())) fail(429, a.code, a.message);
  if (!(await counter(env, day, kind, "*", site).first())) fail(429, "busy", `The site's limit of ${site} ${kind} writes today is reached.`);
}

// ---------- settings ----------

/** Whether visitors may write (the review page's pause switch, settings.writes). */
export async function writesOn(env: Env): Promise<boolean> {
  return ((await env.DB.prepare("SELECT value FROM settings WHERE key = 'writes'").first<string>("value")) ?? "on") !== "off";
}

export async function requireWrites(env: Env): Promise<void> {
  if (!(await writesOn(env))) fail(503, "paused", "Visitors' changes are paused.");
}

// ---------- the bot check ----------

const SITEVERIFY = "https://challenges.cloudflare.com/turnstile/v0/siteverify";

interface Siteverify {
  success: boolean;
  hostname?: string;
  action?: string;
  "error-codes"?: string[];
}

/**
 * Check a visitor's Turnstile token for this action, before any limit is spent. An empty or overlong token
 * is refused without asking Cloudflare. With TURNSTILE_STRICT on (anything but "off"), the token must also
 * come from one of TURNSTILE_HOSTNAMES and name this action. Cloudflare unreachable: 502 bot_check_down.
 */
export async function turnstile(req: Request, env: Env, token: unknown, action: "marker" | "watch" | "request"): Promise<void> {
  if (typeof token !== "string" || !token || token.length > 2048) fail(403, "bot_check", "No bot-check token was sent.");
  const ip = req.headers.get("cf-connecting-ip");
  let out: Siteverify;
  try {
    const res = await fetch(SITEVERIFY, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ secret: env.TURNSTILE_SECRET, response: token, ...(ip ? { remoteip: ip } : {}) }),
      signal: AbortSignal.timeout(8000),
    });
    if (!res.ok) throw new Error(`siteverify answered ${res.status}`);
    out = await res.json<Siteverify>();
  } catch (e) {
    log({ event: "siteverify_unreachable", error: String(e) });
    fail(502, "bot_check_down", "The bot check could not be reached.");
  }
  if (!out.success) {
    log({ event: "bot_check_failed", codes: out["error-codes"] ?? [] });
    fail(403, "bot_check", `The bot check failed (${(out["error-codes"] ?? []).join(", ") || "no reason given"}).`);
  }
  if (env.TURNSTILE_STRICT !== "off" && (!names(env.TURNSTILE_HOSTNAMES).includes(out.hostname ?? "") || out.action !== action)) {
    log({ event: "bot_check_mismatch", hostname: out.hostname ?? null, action: out.action ?? null, expected: action });
    fail(403, "bot_check", "The bot check was for another site or action.");
  }
}

// ---------- labels and slugs ----------

/**
 * A marker label as stored: NFC, whitespace collapsed and trimmed, 1 to 40 code points, and plain words:
 * no control or invisible formatting characters (an emoji's zero-width joiner is fine), nothing that reads
 * as a link (http, www., <, >, or "x.ab/"). Anything else is 400 bad_label.
 */
export function cleanLabel(raw: unknown): string {
  if (typeof raw !== "string") fail(400, "bad_request", "label must be a string.");
  const s = raw.normalize("NFC").replace(/\s+/gu, " ").trim();
  const n = [...s].length;
  const bad =
    n < 1 ||
    n > 40 ||
    /[\p{Cc}\p{Cs}\p{Co}]/u.test(s) ||
    /\p{Cf}/u.test(s.replace(/‍/g, "")) ||
    /http|www\.|[<>]/i.test(s) ||
    /\w\.[a-z]{2,}\//i.test(s);
  if (bad) fail(400, "bad_label", "Labels are plain words, no links, 1 to 40 characters.");
  return s;
}

/** A market slug as the site compares them (WatchlistPage): the last "/" segment, cut before any "#". */
export const normSlug = (slug: string) => slug.split("/").pop()!.split("#")[0];

// ---------- logs ----------

/** One structured line per event, for Workers Logs. Never tokens, hashes or addresses. */
export function log(fields: Record<string, unknown>): void {
  console.log(JSON.stringify({ at: new Date().toISOString(), ...fields }));
}
