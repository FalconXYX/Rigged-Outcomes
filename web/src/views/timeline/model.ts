// Geometry + data helpers for the Timeline view (no React).
import * as d3 from "d3";
import type { TimelineData, Whale } from "../../types";
import { timeTickFormat } from "../../lib/format";

export type View = "whales" | "crowd" | "cmp";
/** Visible x-domain in exported seconds (Eastern wall clock read as UTC). */
export type Domain = readonly [number, number];
export type SeriesKey = "whales" | "highlighted" | "crowd" | "crowdRaw" | "odds" | "events" | "threshold";
export type Visibility = Record<SeriesKey, boolean>;

export const HOUR = 3600;

/**
 * Union of every plotted timestamp and preset range. No time padding: the
 * chart insets the domain by a few pixels instead (see ChartBody), so the
 * overview window fills its strip at the default view.
 */
export function fullDomainOf(tl: TimelineData): Domain {
  let lo = tl.span.start;
  let hi = tl.span.end;
  const take = (t: number | undefined) => {
    if (t == null || !isFinite(t)) return;
    if (t < lo) lo = t;
    if (t > hi) hi = t;
  };
  const h = tl.crowdHourly.t;
  const o = tl.odds.t;
  take(h[0]);
  take(h[h.length - 1]);
  take(o[0]);
  take(o[o.length - 1]);
  for (const w of tl.whales) take(w.t);
  for (const e of tl.events) take(e.t);
  for (const r of tl.timeRanges) {
    take(r.start);
    take(r.end);
  }
  if (hi - lo < HOUR) {
    const c = (lo + hi) / 2;
    return [c - HOUR / 2, c + HOUR / 2];
  }
  return [lo, hi];
}

/** Clamp a domain to [MIN_SPAN, full width] and shift it inside `full`. */
export function clampDomain(d: Domain, full: Domain, minSpan = HOUR): Domain {
  const fw = full[1] - full[0];
  let w = d[1] - d[0];
  if (!isFinite(w) || w <= 0) w = fw;
  w = Math.min(Math.max(w, Math.min(minSpan, fw)), fw);
  const c = (d[0] + d[1]) / 2;
  let a = c - w / 2;
  let b = c + w / 2;
  if (a < full[0]) {
    a = full[0];
    b = a + w;
  }
  if (b > full[1]) {
    b = full[1];
    a = b - w;
  }
  return [a, b];
}

export const sameDomain = (a: Domain, b: Domain, eps = 1) =>
  Math.abs(a[0] - b[0]) <= eps && Math.abs(a[1] - b[1]) <= eps;

/** Index window [i0, i1) covering [d0, d1] plus one point on each side. */
export function visibleSlice(ts: number[], d0: number, d1: number): [number, number] {
  const i0 = Math.max(0, d3.bisectLeft(ts, d0) - 1);
  const i1 = Math.min(ts.length, d3.bisectRight(ts, d1) + 1);
  return [i0, i1];
}

/** Strictly-inside window [i0, i1). */
function insideSlice(ts: number[], d0: number, d1: number): [number, number] {
  return [d3.bisectLeft(ts, d0), d3.bisectRight(ts, d1)];
}

export function nearestIndex(ts: number[], t: number): number {
  if (!ts.length) return -1;
  const i = d3.bisectLeft(ts, t);
  if (i <= 0) return 0;
  if (i >= ts.length) return ts.length - 1;
  return t - ts[i - 1] <= ts[i] - t ? i - 1 : i;
}

type Lin = (v: number) => number;
const lineMono = d3.line().curve(d3.curveMonotoneX);
const lineLin = d3.line();

/**
 * Screen-space path for a time series, drawing only the visible slice. When
 * there are many more points than pixels, each pixel column keeps its first,
 * min, max and last value — visually identical, far fewer vertices.
 */
export function seriesPoints(
  ts: number[],
  vs: number[],
  d0: number,
  d1: number,
  sx: Lin,
  sy: Lin,
  width: number,
): { pts: [number, number][]; dense: boolean } {
  const [i0, i1] = visibleSlice(ts, d0, d1);
  const n = i1 - i0;
  const pts: [number, number][] = [];
  if (n <= 0) return { pts, dense: false };
  if (n <= width * 2) {
    for (let i = i0; i < i1; i++) pts.push([sx(ts[i]), sy(vs[i])]);
    return { pts, dense: false };
  }
  let col = NaN;
  let fy = 0;
  let ly = 0;
  let mn = 0;
  let mx = 0;
  let iMn = 0;
  let iMx = 0;
  const flush = () => {
    if (isNaN(col)) return;
    pts.push([col, fy]);
    if (iMn <= iMx) pts.push([col, mn], [col, mx]);
    else pts.push([col, mx], [col, mn]);
    pts.push([col, ly]);
  };
  for (let i = i0; i < i1; i++) {
    const px = Math.round(sx(ts[i]));
    const py = sy(vs[i]);
    if (px !== col) {
      flush();
      col = px;
      fy = ly = mn = mx = py;
      iMn = iMx = i;
    } else {
      if (py < mn) {
        mn = py;
        iMn = i;
      }
      if (py > mx) {
        mx = py;
        iMx = i;
      }
      ly = py;
    }
  }
  flush();
  return { pts, dense: true };
}

export function linePath(ts: number[], vs: number[], d0: number, d1: number, sx: Lin, sy: Lin, width: number) {
  const { pts, dense } = seriesPoints(ts, vs, d0, d1, sx, sy, width);
  if (pts.length < 2) return "";
  return (dense ? lineLin(pts) : lineMono(pts)) ?? "";
}

export function areaPath(
  ts: number[],
  vs: number[],
  d0: number,
  d1: number,
  sx: Lin,
  sy: Lin,
  width: number,
  baseline: number,
  smooth = false,
) {
  const { pts, dense } = seriesPoints(ts, vs, d0, d1, sx, sy, width);
  if (pts.length < 2) return "";
  return (
    d3
      .area<[number, number]>()
      .curve(smooth && !dense ? d3.curveMonotoneX : d3.curveLinear)
      .x((p) => p[0])
      .y0(baseline)
      .y1((p) => p[1])(pts) ?? ""
  );
}

/** Max of `vs` over points strictly inside [d0, d1]; null when none. */
export function maxInside(ts: number[], vs: number[], d0: number, d1: number): number | null {
  const [i0, i1] = insideSlice(ts, d0, d1);
  let m = -Infinity;
  for (let i = i0; i < i1; i++) if (vs[i] > m) m = vs[i];
  return m === -Infinity ? null : m;
}

/* ── y autorange (plotly.js getAutoRange) ─────────────────────────── */

export interface PadPoint {
  v: number;
  /** px the mark reaches past its value (marker pad + 5% of the axis) */
  pad: number;
}

/**
 * Plotly's autorange: the tightest [lo, hi] for which every point, reaching
 * `pad` px beyond its value, still fits on an axis `len` px long. Same
 * pairwise search over undominated extremes as plotly.js. Null when the
 * values do not span a range.
 */
export function autorange(pts: PadPoint[], len: number): [number, number] | null {
  if (!pts.length || len <= 0) return null;
  const byV = [...pts].sort((a, b) => a.v - b.v);
  const mins: PadPoint[] = [];
  const maxs: PadPoint[] = [];
  // A point can only set the low end if nothing lower reaches further down
  // (and vice versa), which leaves a handful of candidates.
  let reach = -Infinity;
  for (const p of byV) {
    if (p.pad <= reach) continue;
    mins.push(p);
    reach = p.pad;
  }
  reach = -Infinity;
  for (let i = byV.length - 1; i >= 0; i--) {
    if (byV[i].pad <= reach) continue;
    maxs.push(byV[i]);
    reach = byV[i].pad;
  }
  let m = 0;
  let out: [number, number] | null = null;
  for (const a of mins) {
    for (const b of maxs) {
      const dv = b.v - a.v;
      if (dv <= 0) continue;
      const dp = len - a.pad - b.pad;
      if (dp > len / 4) {
        if (dv / dp > m) {
          m = dv / dp;
          out = [a.v - m * a.pad, b.v + m * b.pad];
        }
      } else if (dv / len > m) {
        // padding longer than the axis: fall back to the bare values
        m = dv / len;
        out = [a.v, b.v];
      }
    }
  }
  return out;
}

/**
 * The whale axis as Plotly autoranged it in the Dash app: markers (pad =
 * diameter / 1.6, min 3px) plus 5% of the axis at each end, over the
 * whales shown in [d0, d1].
 */
export function whaleAutorange(
  whales: Whale[],
  d0: number,
  d1: number,
  vis: Visibility,
  maxDisplay: number,
  scale: number,
  len: number,
): [number, number] | null {
  const extra = 0.05 * len;
  const pts: PadPoint[] = [];
  for (const w of whales) {
    if (w.t < d0 || w.t > d1) continue;
    if (w.label ? !vis.highlighted : !vis.whales) continue;
    if (!isFinite(w.display)) continue;
    pts.push({ v: w.display, pad: Math.max(1.25 * whaleRadius(w, maxDisplay, scale), 3) + extra });
  }
  if (!pts.length) return null;
  // One value (or all equal): anchor the range at $0 instead of Plotly's ±1.
  return autorange(pts, len) ?? autorange([...pts, { v: 0, pad: 0 }], len);
}

/* ── Tick labels ───────────────────────────────────────────────────── */

const usdFull = d3.format("$,.0f");

/**
 * One number format per axis: once the top tick reaches $10K every tick is
 * written in K/M/B with the same precision ($2K … $10K); below that, $1,234.
 */
export function usdTickFormat(ticks: number[]): (v: number) => string {
  const top = d3.max(ticks, (v) => Math.abs(v)) ?? 0;
  if (top < 1e4) return (v) => usdFull(v);
  const [unit, suffix] = top >= 1e9 ? [1e9, "B"] : top >= 1e6 ? [1e6, "M"] : [1e3, "K"];
  const step = ticks.length > 1 ? Math.abs(ticks[1] - ticks[0]) : top;
  const f = d3.format(`,.${d3.precisionFixed(step / unit)}f`);
  return (v) => (v === 0 ? "$0" : `${v < 0 ? "\u2212" : ""}$${f(Math.abs(v) / unit)}${suffix}`);
}

/** Linear ticks for a USD axis, never below $0 (the autorange can dip under it). */
export function usdTicks(lo: number, hi: number, n: number): number[] {
  return d3
    .scaleLinear()
    .domain([lo, hi])
    .ticks(n)
    .filter((v) => v >= 0);
}

/** Plotly marker diameters from VisualizeBetTiming.py, as radii. */
export function whaleRadius(w: Whale, maxDisplay: number, scale = 1): number {
  const f = Math.sqrt(Math.max(0, w.display) / (maxDisplay || 1));
  const d = w.label ? 8 + 36 * f : 9 + 38 * f;
  return (d / 2) * scale;
}

/* ── Text measurement ──────────────────────────────────────────────── */

let ctx: CanvasRenderingContext2D | null | undefined;
const widthCache = new Map<string, number>();

export function textWidth(s: string, font: string): number {
  const key = font + "|" + s;
  const hit = widthCache.get(key);
  if (hit != null) return hit;
  if (ctx === undefined) ctx = typeof document !== "undefined" ? document.createElement("canvas").getContext("2d") : null;
  let w = s.length * 6.6;
  if (ctx) {
    ctx.font = font;
    w = ctx.measureText(s).width;
  }
  widthCache.set(key, w);
  return w;
}

export const clearTextCache = () => widthCache.clear();

/**
 * Time ticks for a UTC scale, thinned so their (multi-scale) labels never
 * collide: ask d3 for ~one tick per `target` px, then keep every k-th.
 */
export function timeTicks(scale: d3.ScaleTime<number, number>, width: number, font: string, target = 110): Date[] {
  let ticks = scale.ticks(Math.max(3, Math.round(width / target)));
  if (ticks.length < 2) return ticks;
  let maxW = 0;
  for (const t of ticks) maxW = Math.max(maxW, textWidth(timeTickFormat(t), font));
  let minDx = Infinity;
  for (let i = 1; i < ticks.length; i++) minDx = Math.min(minDx, scale(ticks[i]) - scale(ticks[i - 1]));
  const k = Math.ceil((maxW + 16) / Math.max(1, minDx));
  if (k > 1) ticks = ticks.filter((_, i) => i % k === 0);
  return ticks;
}

/* ── Direct-label placement ────────────────────────────────────────── */

export interface LabelIn {
  key: number;
  x: number;
  y: number;
  r: number;
  text: string;
  w: number;
}

export interface Rect {
  x0: number;
  x1: number;
  y0: number;
  y1: number;
}

export interface LabelOut {
  key: number;
  text: string;
  /** anchor x (see `anchor`) and text baseline y */
  x: number;
  y: number;
  anchor: "start" | "middle" | "end";
  /** leader polyline from the dot edge to the label, when the label had to move */
  leader: [number, number][] | null;
  /** collision box of the text */
  box: Rect;
}

export interface Circle {
  x: number;
  y: number;
  r: number;
}

const overlaps = (a: Rect, b: Rect) => a.x0 < b.x1 && b.x0 < a.x1 && a.y0 < b.y1 && b.y0 < a.y1;

/** Does a circle touch a rect? */
export function circleHitsRect(c: Circle, b: Rect): boolean {
  const nx = Math.max(b.x0, Math.min(c.x, b.x1));
  const ny = Math.max(b.y0, Math.min(c.y, b.y1));
  return (c.x - nx) ** 2 + (c.y - ny) ** 2 < c.r * c.r;
}

/** Leaders closer than this read as one line. */
const LEADER_GAP = 6;

/**
 * Greedy collision-free placement. Each name tries, clear of every dot:
 * centred above its dot, left of it, right of it, below it. Failing those it
 * is stacked further up in a "comb" with a leader, first in slots that stay
 * clear of every dot (up to `reach` px up), then in slots that only avoid
 * other labels. Leaders keep LEADER_GAP px apart (an elbow nudges them
 * sideways) and never cut through a placed label. Dots go left to right so
 * stacked leaders never cross other labels.
 *
 * `maxLeader` caps how long a leader may get; a name that cannot be placed
 * within it is left out (its dot keeps its colour and tooltip).
 */
export function layoutLabels(
  items: LabelIn[],
  W: number,
  H: number,
  others: Circle[] = [],
  { lineH = 15, maxLeader = Infinity, reach = 170 }: { lineH?: number; maxLeader?: number; reach?: number } = {},
): LabelOut[] {
  const placed: Rect[] = [];
  const runs: { x: number; y0: number; y1: number }[] = [];
  const labelled: Circle[] = items.map((it) => ({ x: it.x, y: it.y, r: it.r }));
  const out: LabelOut[] = [];
  const order = items.map((_, i) => i).sort((a, b) => items[a].x - items[b].x || items[b].r - items[a].r);

  for (const idx of order) {
    const it = items[idx];
    const w = it.w;
    const box = (anchor: LabelOut["anchor"], x: number, y: number): Rect => {
      const x0 = anchor === "start" ? x : anchor === "end" ? x - w : x - w / 2;
      return { x0: x0 - 3, x1: x0 + w + 3, y0: y - lineH + 3, y1: y + 4 };
    };
    const free = (rect: Rect, avoidDots: boolean) => {
      if (rect.x0 < 0 || rect.x1 > W || rect.y0 < 0 || rect.y1 > H) return false;
      for (const p of placed) if (overlaps(p, rect)) return false;
      for (const l of runs) if (l.x + 1 > rect.x0 && l.x - 1 < rect.x1 && l.y0 < rect.y1 && rect.y0 < l.y1) return false;
      for (let j = 0; j < labelled.length; j++) if (j !== idx && circleHitsRect(labelled[j], rect)) return false;
      if (avoidDots) for (const o of others) if (circleHitsRect(o, rect)) return false;
      return true;
    };
    const place = (o: Omit<LabelOut, "key" | "text">) => {
      placed.push(o.box);
      out.push({ key: it.key, text: it.text, ...o });
    };

    // 1. natural slots next to the dot
    const aboveY = it.y - it.r - 6;
    const midY = it.y + 4;
    const natural: { anchor: LabelOut["anchor"]; x: number; y: number }[] = [
      { anchor: "middle", x: it.x, y: aboveY },
      { anchor: "end", x: it.x - it.r - 5, y: midY },
      { anchor: "start", x: it.x + it.r + 5, y: midY },
      { anchor: "middle", x: it.x, y: it.y + it.r + lineH - 2 },
    ];
    const nat = natural.find((c) => free(box(c.anchor, c.x, c.y), true));
    if (nat) {
      place({ ...nat, leader: null, box: box(nat.anchor, nat.x, nat.y) });
      continue;
    }

    // 2. stacked slots with a leader
    const top = it.y - it.r - 1;
    const spaced = (y0: number, y1: number) => {
      // The tolerance matters: (x + GAP) - x can round to just under GAP,
      // which would re-find the same leader forever. Each step moves past a
      // distinct leader, so runs.length steps always suffice.
      const clash = (lx: number) => runs.find((l) => Math.abs(l.x - lx) < LEADER_GAP - 0.01 && l.y0 < y1 && y0 < l.y1);
      let lx = it.x;
      for (let n = 0, c = clash(lx); c && lx <= W && n <= runs.length; n++, c = clash(lx)) lx = c.x + LEADER_GAP;
      if (lx <= W - 2 && !clash(lx)) return lx;
      lx = it.x;
      for (let n = 0, c = clash(lx); c && lx >= 0 && n <= runs.length; n++, c = clash(lx)) lx = c.x - LEADER_GAP;
      return Math.max(2, lx);
    };
    type Slot = { anchor: "start" | "end"; ax: number; y: number; lx: number; len: number; rect: Rect };
    const slots = (avoidDots: boolean): Slot[] => {
      const found: Slot[] = [];
      for (let k = 0; k <= 18; k++) {
        const y = aboveY - k * (lineH + 1);
        const len = top - (y + 4);
        if (len > maxLeader || (avoidDots && len > reach) || y - lineH + 3 < 0) break;
        const lx = spaced(y + 4, top);
        for (const anchor of ["end", "start"] as const) {
          const ax = anchor === "end" ? Math.min(lx + 2, W - 3) : Math.max(lx - 2, 3);
          const rect = box(anchor, ax, y);
          if (!free(rect, avoidDots)) continue;
          const run: Rect = { x0: lx - 1, x1: lx + 1, y0: y + 4, y1: top };
          if (len > 3 && placed.some((p) => overlaps(p, run))) continue;
          found.push({ anchor, ax, y, lx, len, rect });
          if (avoidDots) return found; // the first clear slot is the closest one
        }
      }
      return found;
    };
    // Clear of every dot if possible; otherwise the free slot covering the
    // fewest dots, where each extra 60px of leader costs as much as a dot.
    let slot: Slot | undefined = slots(true)[0];
    if (!slot) {
      let best = Infinity;
      for (const c of slots(false)) {
        let score = c.len / 60;
        for (const o of others) if (circleHitsRect(o, c.rect)) score++;
        if (score < best) {
          best = score;
          slot = c;
        }
      }
    }
    let done = false;
    if (slot) {
      const { anchor, ax, y, lx, len, rect } = slot;
      let leader: [number, number][] | null = null;
      if (len > 3) {
        const dx = lx - it.x;
        leader =
          Math.abs(dx) < 0.5
            ? [
                [it.x, top],
                [it.x, y + 4],
              ]
            : [
                [it.x, top],
                [lx, Math.max(y + 4, top - Math.abs(dx))],
                [lx, y + 4],
              ];
        runs.push({ x: lx, y0: y + 4, y1: top });
      }
      place({ anchor, x: ax, y, leader, box: rect });
      done = true;
    }
    if (done || isFinite(maxLeader)) continue;

    // 3. nowhere free: keep it legible above the dot, clamped into the plot
    const x = Math.min(Math.max(it.x, w / 2 + 2), W - w / 2 - 2);
    const y = Math.max(lineH, aboveY);
    place({ anchor: "middle", x, y, leader: null, box: box("middle", x, y) });
  }
  return out;
}

/**
 * Where the "Whale threshold $X" label goes along its line: just above or
 * just below it, at either end or in between, wherever it covers the fewest
 * dots, names and odds-line crossings (ends and "above" win ties). When the
 * line is buried in dots the label moves a row or two away from it (`leader`:
 * the chart ties it back with a hairline), and when even that would hide
 * three or more dots there is no label (null) — the legend still names the line.
 * `oddsSpan(x0, x1)` returns the odds line's [minY, maxY] over a px span.
 */
export function placeThresholdLabel({
  lineY,
  w,
  W,
  H,
  dots,
  labels,
  oddsSpan,
}: {
  lineY: number;
  w: number;
  W: number;
  H: number;
  dots: Circle[];
  labels: Rect[];
  oddsSpan?: (x0: number, x1: number) => [number, number] | null;
}): { x: number; y: number; leader: boolean } | null {
  const steps = 6;
  // Rows hugging the line come first; the rows away from it (drawn with a
  // leader back to the line) cost about two dots, so they are used only when
  // every slot on the line would hide three or more. Below the line there is
  // often an empty strip (the y-range pads under $0) that the lowered row uses.
  // The lowered row sits as far down as the strip allows (chip bottom 2.5px
  // above the baseline), and only when that is well clear of the near row.
  const lowered = Math.min(30, H - lineY - 7);
  const rows = [
    { dy: -6, pen: 0, leader: false },
    { dy: 16, pen: 0.05, leader: false },
    { dy: -24, pen: 2.2, leader: true },
    ...(lowered >= 24 ? [{ dy: lowered, pen: 2.3, leader: true }] : []),
    { dy: -42, pen: 2.4, leader: true },
  ];
  let best: { x: number; y: number; score: number; hits: number; leader: boolean } | null = null;
  for (const row of rows) {
    const y = lineY + row.dy;
    // the box is the drawn chip: text baseline at y, 12.5px above it, 4.5px below
    const b0 = y - 12.5;
    const b1 = y + 4.5;
    if (b0 < 0 || b1 > H) continue;
    for (let i = steps; i >= 0; i--) {
      const x = 6 + ((W - 12 - w) * i) / steps;
      if (x < 0) continue;
      const rect: Rect = { x0: x - 2, x1: x + w + 2, y0: b0, y1: b1 };
      // A dot grazing the chip's edge by a pixel hides nothing.
      const core: Rect = { x0: rect.x0 + 1.5, x1: rect.x1 - 1.5, y0: rect.y0 + 1.5, y1: rect.y1 - 1.5 };
      let hits = 0;
      for (const d of dots) if (circleHitsRect(d, core)) hits += 1;
      let score = hits + row.pen;
      for (const l of labels) if (overlaps(l, rect)) score += 3;
      const span = oddsSpan?.(rect.x0, rect.x1);
      if (span && span[0] <= rect.y1 + 2 && span[1] >= rect.y0 - 2) score += 2;
      score += (i === 0 || i === steps ? 0 : 0.3) + (i === 0 ? 0.01 : 0);
      if (!best || score < best.score) best = { x, y, score, hits, leader: row.leader };
    }
  }
  // Nowhere it would hide fewer than three dots: the legend names the line instead.
  if (!best || best.hits >= 3) return null;
  return { x: best.x, y: best.y, leader: best.leader };
}

export type Corner = "tr" | "tl" | "br" | "bl";

/**
 * Which plot corner the pinned card sits in. A new pin starts on the side
 * away from its dot (top first), so the dot and its neighbours stay in view;
 * while the same pin pans or zooms (`keep`) the card stays put until it would
 * cover the dot. Falls back to the corner covering the dot least.
 */
export function pinCorner(
  prev: Corner,
  keep: boolean,
  dot: Circle | null,
  card: { w: number; h: number },
  W: number,
  H: number,
  inset = { x: 10, y: 8 },
): { corner: Corner; rect: Rect } {
  const rectOf = (c: Corner): Rect => {
    const x0 = c[1] === "l" ? inset.x : W - inset.x - card.w;
    const y0 = c[0] === "t" ? inset.y : H - inset.y - card.h;
    return { x0, x1: x0 + card.w, y0, y1: y0 + card.h };
  };
  const at = (c: Corner) => ({ corner: c, rect: rectOf(c) });
  if (!dot) return at(prev);
  const ring: Circle = { x: dot.x, y: dot.y, r: dot.r + 10 };
  const clearOf = (c: Corner) => !circleHitsRect(ring, rectOf(c));
  if (keep && clearOf(prev)) return at(prev);
  const away = dot.x > W / 2 ? "l" : "r";
  const near = away === "l" ? "r" : "l";
  const order = [`t${away}`, `b${away}`, `t${near}`, `b${near}`] as Corner[];
  const clear = order.find(clearOf);
  if (clear) return at(clear);
  const cover = (c: Corner) => {
    const r = rectOf(c);
    const ox = Math.max(0, Math.min(r.x1, dot.x + dot.r) - Math.max(r.x0, dot.x - dot.r));
    const oy = Math.max(0, Math.min(r.y1, dot.y + dot.r) - Math.max(r.y0, dot.y - dot.r));
    return ox * oy;
  };
  return at([...order].sort((a, b) => cover(a) - cover(b))[0]);
}
