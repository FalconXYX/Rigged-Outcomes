import { useCallback, useEffect, useId, useMemo, useRef, useState, type CSSProperties, type KeyboardEvent } from "react";
import * as d3 from "d3";
import type { MarketIndexEntry } from "../../types";
import { fmtInt, fmtUSDCompact, toDate } from "../../lib/format";
import { prefersReducedMotion } from "../../lib/useSize";
import "./pipeline.css";

/*
 * The hero's right-hand panel: the four pipeline stages as a stepper. Each
 * step carries a live figure from index.json and a small looping SVG drawn
 * from the data. Only the active step's loop runs; the others hold a finished
 * frame (paused at --t0, pipeline.css). Steps auto-advance every 4 s until
 * the visitor hovers, focuses or taps one, then follow the pointer. Reduced
 * motion: no auto-advance and every visual static.
 */

/** Visual box in user units; CSS scales it (aspect-ratio 3 / 1). */
const W = 168;
const H = 56;
/** Seconds per loop of every visual (pipeline.css --loop). */
const LOOP = 3.6;
const ADVANCE_MS = 4000;

type StepKey = "fetch" | "clean" | "identify" | "visualize";

const STEPS: { key: StepKey; verb: string; text: string }[] = [
  { key: "fetch", verb: "Fetch", text: "Every trade in a market, from Polymarket’s Data API." },
  { key: "clean", verb: "Clean", text: "Trades become one position per wallet and outcome." },
  { key: "identify", verb: "Identify", text: "Flag traders who concentrated bets in the final hours." },
  { key: "visualize", verb: "Visualize", text: "Interactive timelines and trader maps per market." },
];

const pad = (n: number) => String(n).padStart(2, "0");

/** Deterministic jitter in [0, 1) so the visuals never change between renders. */
const noise = (i: number) => (((i + 1) * 9301 + 49297) % 233280) / 233280;

interface Figure {
  value: string;
  unit: string;
}

function figures(markets?: MarketIndexEntry[]): Record<StepKey, Figure> {
  if (!markets) {
    return {
      fetch: { value: "–", unit: "traded" },
      clean: { value: "–", unit: "positions" },
      identify: { value: "–", unit: "flagged accounts" },
      visualize: { value: "–", unit: "markets" },
    };
  }
  const volume = markets.reduce((s, m) => s + m.totalVolume, 0);
  const positions = markets.reduce((s, m) => s + m.positions, 0);
  const flagged = markets.reduce((s, m) => s + (m.flaggedAccounts ?? 0), 0);
  return {
    fetch: { value: fmtUSDCompact(volume), unit: "traded" },
    clean: { value: fmtInt(positions), unit: "positions" },
    identify: { value: fmtInt(flagged), unit: "flagged accounts" },
    visualize: { value: String(markets.length), unit: "markets" },
  };
}

const svgProps = { viewBox: `0 0 ${W} ${H}`, focusable: "false" as const, "aria-hidden": true };

/* ── 01 Fetch: trades streaming in, three lanes, continuous ─────────────── */

function Stream() {
  const dots = useMemo(() => {
    const out: { i: number; y: number; r: number; p: number }[] = [];
    for (let lane = 0; lane < 3; lane++) {
      for (let k = 0; k < 8; k++) {
        const i = lane * 8 + k;
        out.push({
          i,
          y: 12 + lane * 16 + (noise(i) - 0.5) * 6,
          r: 1.4 + noise(i + 40) * 1.8,
          p: (k + noise(i + 80) * 0.8) / 8,
        });
      }
    }
    return out;
  }, []);
  return (
    <svg {...svgProps}>
      {dots.map((d) => (
        <circle key={d.i} className="pv-flow" cx={-4} cy={d.y} r={d.r} style={{ "--p": d.p } as CSSProperties} />
      ))}
    </svg>
  );
}

/* ── 02 Clean: trades drop into one bar per wallet (usiran whales, sorted) ── */

const BARS = 18;
const BAR_W = 6;

function Bars({ m }: { m?: MarketIndexEntry }) {
  const heights = useMemo(() => {
    const vs = m?.preview.whales.map((w) => w.v).filter((v) => v > 0).sort((a, b) => b - a) ?? [];
    const raw =
      vs.length >= 4
        ? d3.range(BARS).map((i) => vs[Math.min(vs.length - 1, Math.round((i * (vs.length - 1)) / (BARS - 1)))])
        : d3.range(BARS).map((i) => 1 / (1 + i * 0.6));
    const s = d3.scaleSqrt().domain([0, d3.max(raw) ?? 1]).range([0, 38]);
    return raw.map((v) => Math.max(3, s(v)));
  }, [m]);
  const base = H - 8;
  const gap = (W - BARS * BAR_W) / (BARS - 1);
  return (
    <svg {...svgProps}>
      <line className="pv-base" x1={0} x2={W} y1={base + 0.5} y2={base + 0.5} />
      {heights.map((h, i) => {
        const x = i * (BAR_W + gap);
        const style = { "--i": i } as CSSProperties;
        return (
          <g key={i}>
            <rect className="pv-bar" x={x} y={base - h} width={BAR_W} height={h} style={style} />
            <circle className="pv-drop" cx={x + BAR_W / 2} cy={base - h - 3} r={1.8} style={style} />
          </g>
        );
      })}
    </svg>
  );
}

/* ── 03 Identify: a scan sweeps the whales; the flagged ones light up ───── */

const COLS = 7;
const ROWS = 3;
/** "col-row" cells that flag (5 of 21 ≈ the flagged share of whales). */
const FLAGGED = new Set(["1-2", "2-0", "4-1", "5-2", "6-0"]);
/** The sweep takes this share of the loop; the rest is the hold. */
const SWEEP = 0.7;

function Scan() {
  const cells = useMemo(() => {
    const out: { key: string; x: number; y: number; flagged: boolean; arrive: number }[] = [];
    for (let c = 0; c < COLS; c++) {
      for (let r = 0; r < ROWS; r++) {
        const x = 14 + (c * (W - 28)) / (COLS - 1);
        out.push({ key: `${c}-${r}`, x, y: 12 + r * 16, flagged: FLAGGED.has(`${c}-${r}`), arrive: (x / W) * SWEEP * LOOP });
      }
    }
    return out;
  }, []);
  return (
    <svg {...svgProps}>
      {cells.map((d) => (
        <circle key={d.key} className="pv-whale" cx={d.x} cy={d.y} r={3.2} />
      ))}
      {cells
        .filter((d) => d.flagged)
        .map((d) => {
          const style = { "--a": `${d.arrive.toFixed(2)}s` } as CSSProperties;
          return (
            <g key={d.key}>
              <circle className="pv-pulse" cx={d.x} cy={d.y} r={3.2} style={style} />
              <circle className="pv-flag" cx={d.x} cy={d.y} r={3.2} style={style} />
            </g>
          );
        })}
      <line className="pv-scan" x1={0} x2={0} y1={3} y2={H - 3} />
    </svg>
  );
}

/* ── 04 Visualize: the usiran timeline in miniature ─────────────────────── */

type Pt = [number, number];

interface MiniGeometry {
  odds: string;
  crowd: string;
  dots: { x: number; y: number; r: number; hi: boolean; d: number }[];
  events: number[];
}

function buildMini(m: MarketIndexEntry): MiniGeometry {
  const { preview: p, start, end } = m;
  const padX = 4;
  const top = 5;
  const bottom = H - 8;
  const x = d3
    .scaleUtc()
    .domain([toDate(start), toDate(Math.max(end, start + 1))])
    .range([padX, W - padX]);
  const sx = (t: number) => x(toDate(t));

  const oy = d3.scaleLinear().domain([0, 100]).range([bottom, top]);
  const odds =
    d3
      .line<Pt>()
      .x((d) => sx(d[0]))
      .y((d) => oy(d[1]))
      .curve(d3.curveMonotoneX)(p.odds) ?? "";

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
    .range([bottom, bottom - (bottom - top) * 0.36]);
  const crowd =
    d3
      .area<Pt>()
      .x((d) => sx(d[0]))
      .y0(bottom)
      .y1((d) => cy(d[1]))
      .curve(d3.curveMonotoneX)(centers) ?? "";

  // Biggest whales plus every highlighted user; stake → area, log height.
  const plain = p.whales.filter((w) => !w.hi).slice(-26);
  const whales = [...plain, ...p.whales.filter((w) => w.hi)];
  const vs = whales.map((w) => w.v).filter((v) => v > 0);
  const vMin = Math.max(1, d3.min(vs) ?? 1);
  const vMax = Math.max(vMin * 1.5, d3.max(vs) ?? 1);
  const rs = d3.scaleSqrt().domain([0, vMax]).range([0, 3.4]);
  const wy = d3
    .scaleLog()
    .domain([vMin, vMax])
    .range([bottom - 5, top + 4])
    .clamp(true);
  const dots = whales.map((w) => {
    const dx = sx(w.t);
    return {
      x: dx,
      y: wy(Math.max(vMin, w.v)),
      r: Math.max(w.hi ? 2 : 1.2, rs(w.v)),
      hi: w.hi,
      d: (dx - padX) / (W - 2 * padX),
    };
  });

  const events = m.events.filter((e) => e.t >= start && e.t <= end).map((e) => sx(e.t));
  return { odds, crowd, dots, events };
}

function Mini({ m }: { m?: MarketIndexEntry }) {
  const geo = useMemo(() => (m ? buildMini(m) : null), [m]);
  const bottom = H - 8;
  return (
    <svg {...svgProps}>
      <line className="pv-base" x1={0} x2={W} y1={bottom + 0.5} y2={bottom + 0.5} />
      {geo && (
        <>
          <path className="pv-crowd" d={geo.crowd} />
          {geo.events.map((ex, k) => (
            <line key={k} className="pv-event" x1={ex} x2={ex} y1={4} y2={bottom} />
          ))}
          <path className="pv-odds" d={geo.odds} pathLength={1} />
          {geo.dots.map((d, k) => (
            <circle
              key={k}
              className={d.hi ? "pv-dot is-hi" : "pv-dot"}
              cx={d.x}
              cy={d.y}
              r={d.r}
              style={{ "--d": d.d.toFixed(3) } as CSSProperties}
            />
          ))}
        </>
      )}
    </svg>
  );
}

/* ── The panel ──────────────────────────────────────────────────────────── */

export function Pipeline({ markets }: { markets?: MarketIndexEntry[] }) {
  const [active, setActive] = useState(0);
  const [auto, setAuto] = useState(() => !prefersReducedMotion());
  const buttons = useRef<(HTMLButtonElement | null)[]>([]);
  const headId = useId();
  const figs = useMemo(() => figures(markets), [markets]);
  const sample = useMemo(() => markets?.find((m) => m.name === "usiran") ?? markets?.[0], [markets]);

  useEffect(() => {
    if (!auto) return;
    const id = window.setInterval(() => setActive((a) => (a + 1) % STEPS.length), ADVANCE_MS);
    return () => window.clearInterval(id);
  }, [auto]);

  /** Any hover, focus or tap hands control to the visitor for good. */
  const pick = useCallback((i: number) => {
    setAuto(false);
    setActive(i);
  }, []);

  const onKeyDown = (e: KeyboardEvent<HTMLButtonElement>, i: number) => {
    const n = STEPS.length;
    let to: number | null = null;
    if (e.key === "ArrowDown" || e.key === "ArrowRight") to = (i + 1) % n;
    else if (e.key === "ArrowUp" || e.key === "ArrowLeft") to = (i - 1 + n) % n;
    else if (e.key === "Home") to = 0;
    else if (e.key === "End") to = n - 1;
    if (to == null) return;
    e.preventDefault();
    buttons.current[to]?.focus(); // onFocus → pick(to)
  };

  const visual = (key: StepKey) => {
    switch (key) {
      case "fetch":
        return <Stream />;
      case "clean":
        return <Bars m={sample} />;
      case "identify":
        return <Scan />;
      case "visualize":
        return <Mini m={sample} />;
    }
  };

  return (
    <section className="card pipe" aria-labelledby={headId}>
      <div className="pipe-head">
        <h2 id={headId} className="label">
          How it works
        </h2>
        <span className="label tabular" aria-hidden>
          {pad(active + 1)} / {pad(STEPS.length)}
        </span>
      </div>
      <ol className="pipe-steps">
        {STEPS.map((s, i) => {
          const on = i === active;
          const fig = figs[s.key];
          return (
            <li key={s.key} className="pipe-step" data-active={on || undefined}>
              <button
                ref={(el) => {
                  buttons.current[i] = el;
                }}
                type="button"
                className="pipe-btn"
                aria-current={on ? "step" : undefined}
                onPointerEnter={() => pick(i)}
                onFocus={() => pick(i)}
                onClick={() => pick(i)}
                onKeyDown={(e) => onKeyDown(e, i)}
              >
                <span className="pipe-copy">
                  <span className="pipe-label label">
                    <span className="tabular">{pad(i + 1)}</span>
                    {s.verb}
                  </span>
                  <span className="pipe-text">{s.text}</span>
                  <span className="pipe-stat">
                    <b className="tabular">{fig.value}</b>
                    <span className="label">{fig.unit}</span>
                  </span>
                </span>
                {/* Remounts on every active toggle so the loop restarts: from 0 when
                    active, at the held frame (--t0) when not. */}
                <span key={on ? "on" : "off"} className="pipe-vis" aria-hidden>
                  {visual(s.key)}
                </span>
              </button>
            </li>
          );
        })}
      </ol>
    </section>
  );
}
