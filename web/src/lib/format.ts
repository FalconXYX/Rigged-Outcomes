import * as d3 from "d3";

/** Exported times are Eastern wall-clock seconds; see types.ts. */
export const toDate = (t: number) => new Date(t * 1000);
export const toSecs = (d: Date) => Math.round(d.getTime() / 1000);

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

/** Feb 28 · 01:12 ET from exported seconds */
export const fmtT = (t: number) => `${fmtDateTime(toDate(t))} ET`;

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

/** Multi-scale UTC tick format used on every time axis. */
export function timeTickFormat(d: Date): string {
  if (d3.utcDay(d) < d) return tickMinute(d);
  if (d3.utcMonth(d) < d) return tickDay(d);
  if (d3.utcYear(d) < d) return tickMonth(d);
  return tickYear(d);
}
