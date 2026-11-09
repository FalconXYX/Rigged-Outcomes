// Shareable view state for the timeline: what is on screen, mirrored into the
// route's query string (`#/m/usiran?view=cmp&range=Leadup&pin=0x…`).
//
//   view    whales | crowd | cmp            (omitted: whales)
//   range   preset label                    (omitted: the market's default)
//   from,to visible domain, seconds         (omitted: the preset's own span)
//   odds    0 when the odds axis is hidden
//   filter  1 when stats follow the range
//   pin     pinned wallet id (+ ".n" when that wallet holds several positions)
//   series  hidden legend keys, comma-separated
//   t       replay playhead, seconds, only while paused mid-replay
import { useCallback, useEffect, useRef } from "react";
import { setQuery, type Query } from "../../lib/router";
import type { TimelineData } from "../../types";
import { HOUR, clampDomain, sameDomain, type Domain, type SeriesKey, type View } from "./model";

export type ToggleKey = Exclude<SeriesKey, "odds">;
export const TOGGLE_KEYS: ToggleKey[] = ["whales", "highlighted", "crowd", "crowdRaw", "events", "threshold"];
const VIEWS = new Set<string>(["whales", "crowd", "cmp"]);
export const TL_QUERY_KEYS = ["view", "range", "from", "to", "odds", "filter", "pin", "series", "t"] as const;

export interface TimelinePrefs {
  view: View;
  odds: boolean;
  filter: boolean;
}

export interface TimelineInit extends TimelinePrefs {
  range: string | null;
  /** Visible domain when the link carried one (already clamped); else null = the preset. */
  domain: Domain | null;
  pin: number | null;
  hidden: ToggleKey[];
  /** Paused replay position, when the link carried one inside the domain. */
  t: number | null;
}

export interface TimelineState extends TimelinePrefs {
  range: string | null;
  defaultRange: string | null;
  /** Where the visible domain is (or is heading), and the preset's own domain. */
  domain: Domain;
  base: Domain;
  pin: number | null;
  hidden: ToggleKey[];
  t: number | null;
}

/** Whether the query carries any timeline key (a deliberate link, not a bare market URL). */
export function hasTimelineQuery(q: Query): boolean {
  return TL_QUERY_KEYS.some((k) => k in q);
}

const int = (s: string | undefined): number | null => {
  if (s == null || !/^-?\d+$/.test(s)) return null;
  const n = Number(s);
  return isFinite(n) ? n : null;
};

/** Serialize a pinned whale: its wallet id, plus ".n" for its n-th position under that id. */
export function pinParam(tl: TimelineData, i: number): string | null {
  const w = tl.whales[i];
  if (!w) return null;
  let n = 0;
  for (let k = 0; k < i; k++) if (tl.whales[k].id === w.id) n++;
  return n ? `${w.id}.${n}` : w.id;
}

function parsePin(tl: TimelineData, s: string | undefined): number | null {
  if (!s) return null;
  const m = /^(0x[0-9a-f]{40})(?:\.(\d+))?$/i.exec(s.trim());
  if (!m) return null;
  const id = m[1].toLowerCase();
  let n = m[2] ? Number(m[2]) : 0;
  for (let i = 0; i < tl.whales.length; i++) {
    if (tl.whales[i].id.toLowerCase() !== id) continue;
    if (n === 0) return i;
    n--;
  }
  return null;
}

/**
 * Initial view state from a query. Everything is validated against the
 * market; anything unknown falls back to the default (or to `prefs`, the
 * session's last view / odds / filter, when the URL carries no timeline keys
 * at all — switching markets keeps those).
 */
export function readTimelineQuery(
  q: Query,
  tl: TimelineData,
  full: Domain,
  presetDomain: (label: string | null) => Domain,
  defaultRange: string | null,
  prefs: TimelinePrefs | null,
): TimelineInit {
  const explicit = hasTimelineQuery(q);
  const base: TimelinePrefs = !explicit && prefs ? prefs : { view: "whales", odds: true, filter: false };

  const view = q.view && VIEWS.has(q.view) ? (q.view as View) : base.view;
  const odds = q.odds === "0" ? false : q.odds === "1" ? true : base.odds;
  const filter = q.filter === "1" ? true : q.filter === "0" ? false : base.filter;

  const range = q.range && tl.timeRanges.some((r) => r.label === q.range) ? q.range : defaultRange;

  let domain: Domain | null = null;
  const from = int(q.from);
  const to = int(q.to);
  if (from != null && to != null && to > from && from < full[1] && to > full[0]) {
    const d = clampDomain([from, to], full, Math.min(HOUR, full[1] - full[0]));
    if (!sameDomain(d, presetDomain(range), 1)) domain = d;
  }

  const hidden: ToggleKey[] = [];
  if (q.series) {
    for (const k of q.series.split(",")) {
      const key = k.trim() as ToggleKey;
      if (TOGGLE_KEYS.includes(key) && !hidden.includes(key)) hidden.push(key);
    }
  }

  const pin = parsePin(tl, q.pin);

  let t: number | null = null;
  const tq = int(q.t);
  const d = domain ?? presetDomain(range);
  if (tq != null && tq >= d[0] && tq < d[1]) t = tq;

  return { view, odds, filter, range, domain, pin, hidden, t };
}

/** The query patch for a view state: every timeline key, null where it is at its default. */
export function timelineQueryPatch(s: TimelineState, tl: TimelineData): Record<string, string | null> {
  const zoomed = !sameDomain(s.domain, s.base, 1);
  return {
    view: s.view === "whales" ? null : s.view,
    range: s.range && s.range !== s.defaultRange ? s.range : null,
    from: zoomed ? String(Math.round(s.domain[0])) : null,
    to: zoomed ? String(Math.round(s.domain[1])) : null,
    odds: s.odds ? null : "0",
    filter: s.filter ? "1" : null,
    pin: s.pin != null ? pinParam(tl, s.pin) : null,
    series: s.hidden.length ? s.hidden.join(",") : null,
    t: s.t != null ? String(Math.round(s.t)) : null,
  };
}

function routePath(): string {
  const h = window.location.hash.replace(/^#/, "");
  const i = h.indexOf("?");
  return i === -1 ? h : h.slice(0, i);
}

/**
 * Writes query patches with `setQuery` (replaceState), at most once every
 * `ms` (leading + trailing), so zooming does not thrash the address bar.
 * Writes are tied to the route they were scheduled on: a pending write never
 * lands on the next market after navigation. `flush` writes what is pending
 * now (before copying the link).
 */
export function useQueryWriter(ms = 150) {
  const pending = useRef<Record<string, string | null> | null>(null);
  const last = useRef(0);
  const timer = useRef(0);
  const path = useRef<string | null>(null);

  const flush = useCallback(() => {
    window.clearTimeout(timer.current);
    timer.current = 0;
    const patch = pending.current;
    pending.current = null;
    if (!patch) return;
    if (path.current != null && routePath() !== path.current) return;
    last.current = performance.now();
    setQuery(patch);
  }, []);

  const write = useCallback(
    (patch: Record<string, string | null>) => {
      if (path.current == null) path.current = routePath();
      pending.current = patch;
      const wait = last.current + ms - performance.now();
      if (wait <= 0) flush();
      else if (!timer.current) timer.current = window.setTimeout(flush, wait);
    },
    [flush, ms],
  );

  useEffect(
    () => () => {
      window.clearTimeout(timer.current);
      pending.current = null;
    },
    [],
  );

  return { write, flush };
}
