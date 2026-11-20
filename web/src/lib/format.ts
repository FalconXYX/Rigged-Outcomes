import * as d3 from "d3";

/** Exported times are Eastern wall-clock seconds; see types.ts. */
export const toDate = (t: number) => new Date(t * 1000);

const usd0 = d3.format("$,.0f");
const num0 = d3.format(",.0f");

/** $12,345 */
export const fmtUSD = (v: number | null | undefined) => (v == null || !isFinite(v) ? "—" : usd0(v));

/** $12.3K / $4.2M / $950 */
export function fmtUSDCompact(v: number | null | undefined): string {
  if (v == null || !isFinite(v)) return "—";
  const a = Math.abs(v);
  if (a >= 1e9) return `$${trim(v / 1e9)}B`;
  if (a >= 1e6) return `$${trim(v / 1e6)}M`;
  if (a >= 1e4) return `$${trim(v / 1e3)}K`;
  return usd0(v);
}

function trim(v: number) {
  return v.toFixed(Math.abs(v) >= 100 ? 0 : 1).replace(/\.0$/, "");
}

/** 1,284 */
export const fmtInt = (v: number | null | undefined) => (v == null || !isFinite(v) ? "—" : num0(v));

/** 30.9% (one decimal, like the Dash app) */
export const fmtPct = (v: number | null | undefined, digits = 1) =>
  v == null || !isFinite(v) ? "—" : `${v.toFixed(digits)}%`;

/** Feb 28 · 01:12 (day unpadded, like the axes: "Feb 7") */
export const fmtDateTime = d3.utcFormat("%b %-d · %H:%M");
/** Feb 28, 2026 */
export const fmtDate = d3.utcFormat("%b %-d, %Y");
/** Feb 28 */
export const fmtDay = d3.utcFormat("%b %-d");
/** Sep 27 ’25 */
export const fmtDayYY = d3.utcFormat("%b %-d ’%y");
/** Thu, Feb 28 */
export const fmtWeekDay = d3.utcFormat("%a, %b %-d");
/** February 28, 2026 */
export const fmtLongDate = d3.utcFormat("%B %-d, %Y");

/** Feb 28 · 01:12 ET from exported seconds */
export const fmtT = (t: number) => `${fmtDateTime(toDate(t))} ET`;

/* ── Toronto time ─────────────────────────────────────────────────────── */
// The backend keeps every day as a Toronto calendar day (docs/BACKEND.md), and every moment the Watchlist and
// the review page show (a list's run, a check, a headline's date, a run on the review page) is read on
// Toronto's clock, daylight saving included, whatever the visitor's own zone. Exported market seconds above
// are already Eastern wall-clock time and keep their own helpers.

const TORONTO = new Intl.DateTimeFormat("en-CA", {
  timeZone: "America/Toronto",
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  hourCycle: "h23",
});

/** A moment (ms, or an ISO stamp in any offset) as Toronto's day ("2026-11-19") and clock ("04:12"). */
function toronto(at: number | string): { day: string; clock: string } {
  const ms = typeof at === "string" ? Date.parse(at) : at;
  const p = Object.fromEntries(TORONTO.formatToParts(new Date(ms)).map((x) => [x.type, x.value]));
  return { day: `${p.year}-${p.month}-${p.day}`, clock: `${p.hour}:${p.minute}` };
}

/** Toronto's calendar day ("YYYY-MM-DD") at a moment (now by default). */
export const torontoDay = (at: number | string = Date.now()) => toronto(at).day;
/** "04:12", Toronto's clock at a moment. */
export const torontoClock = (at: number | string) => toronto(at).clock;
/** A calendar day ("YYYY-MM-DD") as a Date at its noon, for the day formats above (no zone can move it). */
export const calDate = (day: string) => new Date(`${day.slice(0, 10)}T12:00:00Z`);
/** "Nov 14": a calendar day. */
export const fmtCalDay = (day: string) => fmtDay(calDate(day));
/** "Nov 14": the Toronto day of a moment (ms, or an ISO stamp). */
export const fmtDayAt = (at: number | string) => fmtCalDay(torontoDay(at));
/** "Nov 14, 04:31 ET": a moment on Toronto's clock. */
export const fmtStamp = (at: number | string) => `${fmtDayAt(at)}, ${torontoClock(at)} ET`;
/** The calendar day after a "YYYY-MM-DD" day. */
export const dayAfter = (day: string) => {
  const d = calDate(day);
  d.setUTCDate(d.getUTCDate() + 1);
  return d.toISOString().slice(0, 10);
};

/** 0x6b44…be1e */
export const shortAddr = (a: string) => (a.length > 12 ? `${a.slice(0, 6)}…${a.slice(-4)}` : a);

export const polymarketProfile = (addr: string) => `https://polymarket.com/profile/${addr}`;
/**
 * markets.json holds market slugs, which only sometimes equal the event slug.
 * polymarket.com/market/<slug> redirects to the right /event/<event>/<market>
 * page; an "event/market" pair can link to the event page directly.
 */
export function polymarketMarket(slug: string) {
  const s = slug.split("#")[0];
  return s.includes("/") ? `https://polymarket.com/event/${s}` : `https://polymarket.com/market/${s}`;
}

/**
 * Typographic quotes for display text: apostrophes become ’, double quotes
 * become “…”, or ‘…’ when the text is itself shown inside “…” (`nested`).
 * For on-screen titles only; leave data and document.title as they are.
 */
export function smartQuotes(text: string, { nested = false }: { nested?: boolean } = {}): string {
  const [open, close] = nested ? ["‘", "’"] : ["“", "”"];
  return text
    .replace(/(^|[\s([{\u2014\u2013-])"/g, `$1${open}`)
    .replace(/"/g, close)
    .replace(/(^|[\s([{\u2014\u2013-])'/g, "$1‘")
    .replace(/'/g, "’");
}

const tickMinute = d3.utcFormat("%H:%M");
const tickDay = d3.utcFormat("%b %-d");
const tickMonth = d3.utcFormat("%b");
const tickYear = d3.utcFormat("%Y");

/** Multi-scale tick format used on every time axis (exported seconds: Eastern wall clock read with UTC APIs). */
export function timeTickFormat(d: Date): string {
  if (d3.utcDay(d) < d) return tickMinute(d);
  if (d3.utcMonth(d) < d) return tickDay(d);
  if (d3.utcYear(d) < d) return tickMonth(d);
  return tickYear(d);
}
