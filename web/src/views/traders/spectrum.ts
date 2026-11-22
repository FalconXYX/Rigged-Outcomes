// Pure math for the insider-score views. Everything a chart needs about the
// rubric (bands, labels, colours, question levels, factor names) comes from the
// market's `traders.rubric`, so a rubric change never touches this file.
//
// Same rules as model.ts: runnable under Node's type stripping (type-only
// imports, no enums / namespaces / parameter properties, no runtime imports).
//
// Views this feeds (docs/WALLET_RUBRIC.md, "On the site"):
//   Spectrum strip   layoutSpectrum(): a 0–100 axis split into bands, one dot per
//                    whale (beeswarm), area ∝ position, fill = band colour. The
//                    top band's dots get the surface-coloured core the
//                    "critical" key has today (second channel beside colour).
//   Constellation    spectrumData(): radius = 110 − score; spectrumGuide(): the
//                    guide rings at the band cut-offs, labelled with band names.
//   Treemap          bandGrouping(): top-level groups are bands.
//   Wallet card      levelSegments(): one stacked bar per score question, its
//                    segments coloured by where each level sits on the scale;
//                    likeliestLevel() for the label beside it; formatFact().
//   Stat tiles       bandGrouping() flags = bands with flagged = true.
//   Market tab       flaggedCount(): the scored whales in flagged bands.

import type {
  RubricBand,
  RubricDisplay,
  RubricFormat,
  RubricQuestion,
  SpectrumAnswer,
  Trader,
  TraderSpectrum,
  TradersData,
  IndexSpectrum,
} from "../../types";
import type { Group, Grouping, RingGuide } from "./groups";

/** A band's colour as CSS: the rubric names a token, the browser resolves it. */
export const bandColor = (b: { color: string }) => (b.color.startsWith("--") ? `var(${b.color})` : b.color);

/** The band a score falls in (min inclusive; the top band includes the scale's max). Every view bands
 *  by score through this, so the page's own rubric decides, whatever band id was exported. */
export function bandOf(rubric: RubricDisplay, score: number): RubricBand {
  let out = rubric.bands[0];
  for (const b of rubric.bands) if (score >= b.min) out = b;
  return out;
}

/** True when the page carries a usable rubric and at least one scored whale. */
export function hasSpectrum(t: TradersData | null | undefined): t is TradersData & { rubric: RubricDisplay } {
  return !!t?.rubric && t.rubric.bands.length > 0 && t.traders.some((tr) => tr.spectrum && Number.isFinite(tr.spectrum.score));
}

/* ── The spectrum's own data set ───────────────────────────────────── */

/** A score on the rubric's scale as 0–100 (the constellation's units, like today's riskScore). */
export const toHundred = (rubric: RubricDisplay, score: number) => {
  const [lo, hi] = rubric.scale;
  return (100 * (score - lo)) / (hi - lo || 1);
};

/** Radius in data units for a score: the same 110 − score rule as today's riskScore (score on 0–100). */
export const spectrumRadius = (rubric: RubricDisplay, score: number) => 110 - toHundred(rubric, score);

/**
 * The scored whales only, each moved to radius 110 − score on the angle (and
 * with the ±4 jitter) export.py gave it, so a wallet keeps its bearing when the
 * visitor moves between the Flagged traders and Insider score tabs.
 */
export function spectrumData(t: TradersData & { rubric: RubricDisplay }): TradersData {
  const traders: Trader[] = [];
  for (const tr of t.traders) {
    const s = tr.spectrum;
    if (!s || !Number.isFinite(s.score)) continue;
    const dist = Math.hypot(tr.x, tr.y);
    const theta = dist > 0 ? Math.atan2(tr.y, tr.x) : ((parseInt(tr.id.slice(2, 8), 16) % 360) * Math.PI) / 180;
    const jitter = Math.max(-4, Math.min(4, dist - (110 - tr.riskScore)));
    const r = Math.max(0, spectrumRadius(t.rubric, s.score) + (Number.isFinite(jitter) ? jitter : 0));
    traders.push({ ...tr, x: r * Math.cos(theta), y: r * Math.sin(theta) });
  }
  return { ...t, traders };
}

/** The rubric's bands as the charts' groups (rows of `traders` must all be scored: see spectrumData). */
export function bandGrouping(rubric: RubricDisplay, traders: readonly Trader[]): Grouping {
  const top = rubric.bands[rubric.bands.length - 1]?.id;
  const flagged = isFlagged(rubric);
  const groups: Group[] = rubric.bands.map((b) => ({
    id: b.id,
    label: b.label,
    short: b.label,
    color: bandColor(b),
    core: b.id === top,
    flagged: flagged(b),
  }));
  return {
    noun: "band",
    groups,
    byId: new Map(groups.map((g) => [g.id, g])),
    rowGroup: traders.map((tr) => bandOf(rubric, tr.spectrum?.score ?? rubric.scale[0]).id),
  };
}

/* ── Words built from the rubric ───────────────────────────────────── */

/** "A", "A or B", "A, B or C". */
export function orList(items: readonly string[]): string {
  if (items.length <= 1) return items[0] ?? "";
  return `${items.slice(0, -1).join(", ")} or ${items[items.length - 1]}`;
}

/** "A", "A and B", "A, B and C". */
export function andList(items: readonly string[]): string {
  if (items.length <= 1) return items[0] ?? "";
  return `${items.slice(0, -1).join(", ")} and ${items[items.length - 1]}`;
}

const num = (v: number) => (Number.isInteger(v) ? String(v) : v.toFixed(1));

/** Whether a band counts as flagged: its own `flagged`, or, in a rubric that flags none, the top band. */
export function isFlagged(rubric: RubricDisplay): (b: RubricBand) => boolean {
  const any = rubric.bands.some((b) => b.flagged);
  const top = rubric.bands[rubric.bands.length - 1]?.id;
  return (b) => (any ? b.flagged : b.id === top);
}

/** The flagged bands: labels, and the score range they cover when they are the top bands without a gap. */
export function flaggedBands(rubric: RubricDisplay): { labels: string[]; from: number | null; to: number } {
  const bands = rubric.bands;
  const flagged = isFlagged(rubric);
  const labels = bands.filter(flagged).map((b) => b.label);
  const first = bands.findIndex(flagged);
  const contiguous = first >= 0 && bands.slice(first).every(flagged);
  return { labels, from: contiguous ? bands[first].min : null, to: rubric.scale[1] };
}

/** "Insider score 60–100" for a score range. */
export const rangeText = (rubric: RubricDisplay, from: number, to: number) => `${rubric.name} ${num(from)}–${num(to)}`;

/** Questions that count toward the score, heaviest first (ties keep the rubric's order). */
export function countedQuestions(rubric: RubricDisplay): RubricQuestion[] {
  return rubric.questions
    .map((q, i) => ({ q, i }))
    .filter(({ q }) => q.weight > 0)
    .sort((a, b) => b.q.weight - a.q.weight || a.i - b.i)
    .map(({ q }) => q);
}

/* ── Constellation guide ───────────────────────────────────────────── */

/**
 * Rings at the scale's ends and every band cut-off; a numeric axis labels the
 * cut-offs ("Score 0", 20, 40, ...) up to the top band, whose ring the core
 * callout already names; the two callouts name the top band (at the core) and
 * the lowest band (the outer orbit) and aim at a real wallet of their band.
 */
export function spectrumGuide(rubric: RubricDisplay): RingGuide {
  const [lo, hi] = rubric.scale;
  const cuts = new Set<number>([lo, hi]);
  for (const b of rubric.bands) if (b.min > lo && b.min < hi) cuts.add(b.min);
  // The guide speaks the constellation's 0–100 units; the callouts quote the rubric's own scale.
  const rings = [...cuts].sort((a, b) => a - b).map((c) => toHundred(rubric, c));
  const top = rubric.bands[rubric.bands.length - 1];
  const bottom = rubric.bands[0];
  const mid = (b: RubricBand) => toHundred(rubric, (b.min + b.max) / 2);
  const at = (score: number, deg: number): [number, number] => {
    const r = 110 - score;
    const th = (deg * Math.PI) / 180;
    return [r * Math.cos(th), r * Math.sin(th)];
  };
  return {
    rings,
    labels: [...cuts]
      .filter((c) => c < top.min)
      .sort((a, b) => a - b)
      .map((c) => ({ score: toHundred(rubric, c), text: c === lo ? `Score ${num(c)}` : num(c) })),
    core: {
      head: at(mid(top), 45),
      group: top.id,
      title: top.label,
      sub: rangeText(rubric, top.min, top.max),
      color: bandColor(top),
    },
    orbit: {
      head: at(mid(bottom), 225),
      group: bottom.id,
      title: bottom.label,
      sub: rangeText(rubric, bottom.min, bottom.max),
      color: bandColor(bottom),
    },
  };
}

/* ── Spectrum strip (beeswarm) ─────────────────────────────────────── */

export interface SpectrumDot {
  row: number;
  /** Centre in px from the plot's left edge / its baseline (y grows upward). */
  x: number;
  y: number;
  r: number;
  band: RubricBand;
}

/** x in px of a score on a strip `width` px wide, inset by `pad` on both ends. */
export function scoreX(rubric: RubricDisplay, score: number, width: number, pad: number): number {
  const [lo, hi] = rubric.scale;
  return pad + ((score - lo) / (hi - lo || 1)) * Math.max(0, width - 2 * pad);
}

/**
 * One dot per scored whale on a horizontal axis, area ∝ position
 * (radius rMin + (rMax − rMin)·√(position / max)), stacked upward from the
 * baseline without overlap: biggest first, each at the lowest free height
 * above its x. Deterministic, so the strip never reshuffles on a resize of
 * the same width. When the stack would pass `maxHeight`, every radius shrinks
 * until it fits (the dots keep their sizes relative to each other).
 */
export function layoutSpectrum(
  rubric: RubricDisplay,
  traders: readonly Trader[],
  width: number,
  opts: { rMin?: number; rMax?: number; gap?: number; pad?: number; maxHeight?: number } = {},
): { dots: SpectrumDot[]; height: number; scale: number } {
  const gap = opts.gap ?? 1;
  const pad = opts.pad ?? (opts.rMax ?? 11);
  const maxHeight = opts.maxHeight ?? Infinity;
  let scale = 1;
  let out = beeswarm(rubric, traders, width, (opts.rMin ?? 2.5) * scale, (opts.rMax ?? 11) * scale, gap, pad);
  for (let i = 0; i < 6 && out.height > maxHeight; i++) {
    scale *= Math.max(0.35, Math.sqrt(maxHeight / out.height) * 0.97);
    out = beeswarm(rubric, traders, width, Math.max(1.2, (opts.rMin ?? 2.5) * scale), Math.max(2, (opts.rMax ?? 11) * scale), gap, pad);
  }
  return { ...out, scale };
}

function beeswarm(rubric: RubricDisplay, traders: readonly Trader[], width: number, rMin: number, rMax: number, gap: number, pad: number) {
  const rows: number[] = [];
  let maxPos = 0;
  traders.forEach((t, i) => {
    if (!t.spectrum) return;
    rows.push(i);
    maxPos = Math.max(maxPos, t.position);
  });
  const order = [...rows].sort((a, b) => traders[b].position - traders[a].position || a - b);
  const placed: SpectrumDot[] = [];
  let height = 0;
  for (const row of order) {
    const t = traders[row];
    const s = t.spectrum as TraderSpectrum;
    const r = rMin + (rMax - rMin) * (maxPos > 0 ? Math.sqrt(Math.max(0, t.position) / maxPos) : 0);
    const x = scoreX(rubric, s.score, width, pad);
    // Candidate heights: the baseline, and resting on top of each neighbour that overlaps in x.
    const near = placed.filter((d) => Math.abs(d.x - x) < d.r + r + gap);
    const candidates = [r, ...near.map((d) => d.y + Math.sqrt(Math.max(0, (d.r + r + gap) ** 2 - (d.x - x) ** 2)))];
    candidates.sort((a, b) => a - b);
    let y = r;
    for (const c of candidates) {
      if (c < r) continue;
      if (near.every((d) => (d.x - x) ** 2 + (d.y - c) ** 2 >= (d.r + r + gap) ** 2 - 1e-6)) {
        y = c;
        break;
      }
    }
    placed.push({ row, x, y, r, band: bandOf(rubric, s.score) });
    height = Math.max(height, y + r);
  }
  placed.sort((a, b) => b.r - a.r || a.row - b.row); // draw big first, so small dots stay visible on top
  return { dots: placed, height };
}

/* ── Wallet card: per-question answers ─────────────────────────────── */

export interface LevelSegment {
  level: number;
  label: string;
  /** Start and width, as fractions of the bar. */
  from: number;
  width: number;
  /** The band colour of the level's place on the scale (lowest level = lowest band). */
  color: string;
}

/** A score question's level probabilities as bar segments, lowest level on the left. */
export function levelSegments(rubric: RubricDisplay, q: RubricQuestion, p: readonly number[]): LevelSegment[] {
  const levels = q.levels ?? [];
  const n = levels.length;
  const [lo, hi] = rubric.scale;
  const sum = levels.reduce((a, _, i) => a + Math.max(0, p[i] ?? 0), 0) || 1;
  let from = 0;
  return levels.map((label, level) => {
    const width = Math.max(0, p[level] ?? 0) / sum;
    // The level's place on the scale: the lowest level at the bottom band, the top level at the top band.
    const at = n > 1 ? lo + ((hi - lo) * level) / (n - 1) : lo;
    const seg = { level, label, from, width, color: bandColor(bandOf(rubric, at)) };
    from += width;
    return seg;
  });
}

/** The likeliest level of a score answer and its probability ("Hours before · 42%"). */
export function likeliestLevel(q: RubricQuestion, p: readonly number[]): { level: number; label: string; p: number } | null {
  if (!q.levels || p.length === 0) return null;
  let best = 0;
  for (let i = 1; i < Math.min(p.length, q.levels.length); i++) if (p[i] > p[best]) best = i;
  return { level: best, label: q.levels[best] ?? "", p: p[best] };
}

/** One answer of a trader, typed by its question (null when it is missing or the wrong shape). */
export function answerOf(s: TraderSpectrum, q: RubricQuestion) {
  const a: SpectrumAnswer | undefined = s.q[q.id];
  if (!a) return null;
  if (q.type === "score") return "p" in a && Array.isArray(a.p) ? { type: "score" as const, p: a.p } : null;
  if (q.type === "noul") return "p" in a && typeof a.p === "number" && !("choice" in a) ? { type: "noul" as const, p: a.p } : null;
  if ("choice" in a) {
    const opt = q.options?.find((o) => o.id === a.choice);
    return { type: "choice" as const, id: a.choice, label: opt?.label ?? a.choice, p: a.p };
  }
  return null;
}

/* ── Factor values ─────────────────────────────────────────────────── */

const int = (v: number) => Math.round(v).toLocaleString("en-US");

/** A factor value as the card shows it, by the rubric's format. */
export function formatFact(v: number | boolean | null | undefined, format: RubricFormat | string): string {
  if (v == null || (typeof v === "number" && !Number.isFinite(v))) return "—";
  if (typeof v === "boolean" || format === "flag") return v ? "Yes" : "No";
  const neg = v < 0 ? "−" : "";
  const a = Math.abs(v);
  switch (format) {
    case "usd":
      return `${neg}$${int(a)}`;
    case "percent":
      return `${neg}${a > 0 && a < 0.1 ? "<0.1" : a < 10 ? a.toFixed(1) : a.toFixed(0)}%`;
    case "probability":
      return `${neg}${(a * 100).toFixed(a * 100 < 10 ? 1 : 0)}%`;
    case "count":
      return `${neg}${int(a)}`;
    case "hours":
      if (a < 1) return `${neg}${Math.max(1, Math.round(a * 60))} min`;
      if (a < 48) return `${neg}${a.toFixed(a < 10 ? 1 : 0)} h`;
      return `${neg}${int(a / 24)} days`;
    case "days":
      if (a < 1 / 24) return `${neg}<1 h`;
      if (a < 1) return `${neg}${Math.round(a * 24)} h`;
      return `${neg}${int(a)} day${Math.round(a) === 1 ? "" : "s"}`;
    case "x":
      return `${neg}${a < 10 ? a.toFixed(1) : int(a)}×`;
    default:
      return `${neg}${a < 10 ? a.toFixed(2) : int(a)}`;
  }
}

/* ── Counts outside the Traders view ───────────────────────────────── */

/** Whales in flagged bands on a scored page (the market tab's count); null when the page is not scored. */
export function flaggedCount(t: TradersData | null | undefined): number | null {
  if (!hasSpectrum(t)) return null;
  const flagged = isFlagged(t.rubric);
  let n = 0;
  for (const tr of t.traders) if (tr.spectrum && Number.isFinite(tr.spectrum.score) && flagged(bandOf(t.rubric, tr.spectrum.score))) n += 1;
  return n;
}

/** Whales in flagged bands from a market's index entry (the tab's count before the page loads). */
export function indexFlaggedCount(s: IndexSpectrum | null | undefined): number | null {
  if (!s || s.bands.length === 0) return null;
  const any = s.bands.some((b) => b.flagged);
  const top = s.bands[s.bands.length - 1].id;
  return s.bands.reduce((n, b) => n + ((any ? b.flagged : b.id === top) ? b.count : 0), 0);
}
