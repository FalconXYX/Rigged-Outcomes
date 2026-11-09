import { useMemo, type CSSProperties } from "react";
import * as d3 from "d3";
import type { MarketIndexEntry } from "../../types";
import { fmtDay, timeTickFormat, toDate } from "../../lib/format";
import { useSize } from "../../lib/useSize";

/*
 * Thumbnail of the Timeline view's whale chart for a home-page card: crowd
 * volume as a wash along the bottom, the odds line over the full height, whale
 * positions as dots, events as dashed hairlines. Display only — no axes, no
 * hover; the card itself links to the full chart. Entrance motion lives in
 * preview.css, driven by the card's data-phase (useReveal).
 */

const PAD_TOP = 12;
/** Markets with events keep a band clear above the plot for the event chips. */
const PAD_TOP_EVENTS = 30;
const PAD_BOTTOM = 22; // time labels
const PAD_X = 14;
/** Crowd volume keeps to the bottom share of the plot. */
const CROWD_SHARE = 0.36;

type Pt = [number, number];

interface Geometry {
  bottom: number;
  guideY: number;
  crowdArea: string;
  crowdEdge: string;
  odds: string;
  dots: { x: number; y: number; r: number; hi: boolean; delay: number }[];
  ticks: { x: number; text: string; anchor: "start" | "middle" | "end" }[];
  events: {
    key: string;
    x: number;
    label: string;
    align: "start" | "center" | "end";
  }[];
}

function build(m: MarketIndexEntry, w: number, h: number): Geometry {
  const { preview: p, start, end } = m;
  const x = d3
    .scaleUtc()
    .domain([toDate(start), toDate(Math.max(end, start + 1))])
    .range([PAD_X, w - PAD_X]);
  const sx = (t: number) => x(toDate(t));
  const hasEvents = m.events.some((e) => e.t >= start && e.t <= end);
  const top = hasEvents ? PAD_TOP_EVENTS : PAD_TOP;
  const bottom = h - PAD_BOTTOM;
  const plotH = bottom - top;
  const plotW = w - 2 * PAD_X;

  // Odds: 0–100 % over the full plot height.
  const oy = d3.scaleLinear().domain([0, 100]).range([bottom, top]);
  const odds =
    d3
      .line<Pt>()
      .x((d) => sx(d[0]))
      .y((d) => oy(d[1]))
      .curve(d3.curveMonotoneX)(p.odds) ?? "";

  // Crowd: sqrt so the quiet stretches keep some texture next to the spikes.
  const binW = p.crowd.length ? (end - start) / p.crowd.length : 0;
  const centers: Pt[] = p.crowd.map(([t, v]) => [t + binW / 2, v]);
  if (centers.length) {
    centers.unshift([start, centers[0][1]]);
    centers.push([end, centers[centers.length - 1][1]]);
  }
  const cMax = d3.max(centers, (d) => d[1]) ?? 0;
  const cy = d3
    .scaleSqrt()
    .domain([0, cMax > 0 ? cMax : 1])
    .range([bottom, bottom - plotH * CROWD_SHARE]);
  const crowdArea =
    d3
      .area<Pt>()
      .x((d) => sx(d[0]))
      .y0(bottom)
      .y1((d) => cy(d[1]))
      .curve(d3.curveMonotoneX)(centers) ?? "";
  const crowdEdge =
    d3
      .line<Pt>()
      .x((d) => sx(d[0]))
      .y((d) => cy(d[1]))
      .curve(d3.curveMonotoneX)(centers) ?? "";

  // Whales: area ∝ stake; height on a log scale so small bets don't flatten.
  const vs = p.whales.map((d) => d.v).filter((v) => v > 0);
  const vMin = Math.max(1, d3.min(vs) ?? 1);
  const vMax = Math.max(vMin * 1.5, d3.max(vs) ?? 1);
  const rMax = w < 420 ? 7.5 : 9;
  const rs = d3.scaleSqrt().domain([0, vMax]).range([0, rMax]);
  const rOf = (v: number) => Math.max(2, rs(v));
  /** Highlighted users stay findable even when their stake is small. */
  const HI_MIN_R = 3.5;
  const wy = d3
    .scaleLog()
    .domain([vMin, vMax])
    .range([bottom - rOf(vMin) - 2, top + rMax])
    .clamp(true);
  const dot = (d: { t: number; v: number; hi: boolean }) => {
    const dx = sx(d.t);
    const r = d.hi ? Math.max(HI_MIN_R, rOf(d.v)) : rOf(d.v);
    return {
      x: dx,
      y: wy(Math.max(vMin, d.v)),
      r,
      hi: d.hi,
      delay: Math.round(140 + ((dx - PAD_X) / plotW) * 900),
    };
  };
  // Biggest on top (input is small → big); highlighted users above every whale.
  const dots = [...p.whales.filter((d) => !d.hi).map(dot), ...p.whales.filter((d) => d.hi).map(dot)];

  // Time labels: both ends, plus up to two round ticks that clear them.
  const edge = 56;
  const inner = x
    .ticks(w < 420 ? 2 : 4)
    .map((d) => ({ x: x(d), d }))
    .filter((t) => t.x > PAD_X + edge && t.x < w - PAD_X - edge);
  const n = inner.length;
  const keep =
    n <= 2 ? inner : n === 3 ? [inner[1]] : [inner[Math.round((n - 1) / 3)], inner[Math.round(((n - 1) * 2) / 3)]];
  const ticks: Geometry["ticks"] = [
    { x: PAD_X, text: fmtDay(toDate(start)), anchor: "start" },
    ...keep.map((t) => ({
      x: t.x,
      text: timeTickFormat(t.d),
      anchor: "middle" as const,
    })),
    { x: w - PAD_X, text: fmtDay(toDate(end)), anchor: "end" },
  ];

  const events = m.events
    .filter((e) => e.t >= start && e.t <= end)
    .map((e) => {
      const ex = sx(e.t);
      const align: "start" | "center" | "end" = ex > w - 80 ? "end" : ex < 80 ? "start" : "center";
      // keyed by time, not pixels: a resize must not remount (and replay) the chip
      return { key: `${e.t}-${e.label}`, x: ex, label: e.label, align };
    });

  return {
    bottom,
    guideY: oy(50),
    crowdArea,
    crowdEdge,
    odds,
    dots,
    ticks,
    events,
  };
}

/** One-line description of what the thumbnail shows, for screen readers. */
function describe(m: MarketIndexEntry) {
  const o = m.preview.odds;
  const odds = o.length
    ? `odds moved from ${Math.round(o[0][1])}% to ${Math.round(o[o.length - 1][1])}%`
    : "no odds data";
  const whales = m.preview.whales.filter((d) => !d.hi).length;
  const hi = m.preview.whales.length - whales;
  return `Timeline preview: ${odds}; dots mark the ${whales} biggest whale positions${hi ? ` and ${hi} highlighted users` : ""}.`;
}

export function MiniTimeline({ m }: { m: MarketIndexEntry }) {
  const [ref, { width, height }] = useSize<HTMLDivElement>();
  const geo = useMemo(() => (width > 40 && height > 60 ? build(m, width, height) : null), [m, width, height]);
  const gid = `mt-crowd-${m.name}`;

  return (
    <div className="mt" role="img" aria-label={describe(m)}>
      <div ref={ref} className="mt-plot">
        {geo && (
          <>
            <svg width={width} height={height} aria-hidden focusable="false">
              <defs>
                <linearGradient id={gid} x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0" className="mt-stop-a" />
                  <stop offset="1" className="mt-stop-b" />
                </linearGradient>
              </defs>
              <line className="mt-guide" x1={PAD_X} x2={width - PAD_X} y1={geo.guideY} y2={geo.guideY} />
              <g className="mt-crowd">
                <path d={geo.crowdArea} fill={`url(#${gid})`} />
                <path className="mt-crowd-edge" d={geo.crowdEdge} />
              </g>
              <line className="mt-base" x1={PAD_X} x2={width - PAD_X} y1={geo.bottom + 0.5} y2={geo.bottom + 0.5} />
              {geo.events.map((e) => (
                <line key={e.key} className="mt-event-line" x1={e.x} x2={e.x} y1={22} y2={geo.bottom} />
              ))}
              <path className="mt-odds" d={geo.odds} pathLength={1} />
              <g className="mt-dots">
                {geo.dots.map((d, k) => (
                  <circle
                    key={k}
                    className={d.hi ? "mt-dot is-hi" : "mt-dot"}
                    cx={d.x}
                    cy={d.y}
                    r={d.r}
                    style={{ "--d": `${d.delay}ms` } as CSSProperties}
                  />
                ))}
              </g>
              <g className="mt-ticks">
                {geo.ticks.map((t, k) => (
                  <text key={k} x={t.x} y={height - 7} textAnchor={t.anchor}>
                    {t.text}
                  </text>
                ))}
              </g>
            </svg>
            {geo.events.map((e) => (
              <span key={e.key} className="mt-event" data-align={e.align} style={{ left: e.x }}>
                {e.label}
              </span>
            ))}
          </>
        )}
      </div>
    </div>
  );
}
