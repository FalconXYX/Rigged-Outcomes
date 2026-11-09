import {
  useCallback,
  useEffect,
  useId,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type FocusEvent,
  type KeyboardEvent,
  type MouseEvent as RMouseEvent,
  type PointerEvent as RPointerEvent,
} from "react";
import * as d3 from "d3";
import type { TimelineData } from "../../types";
import { useSize } from "../../lib/useSize";
import { fmtPct, fmtT, fmtUSD, timeTickFormat, toDate } from "../../lib/format";
import { FONT_SANS, TipRow, Tooltip } from "../../components/ui";
import {
  HOUR,
  areaPath,
  clampDomain,
  layoutLabels,
  linePath,
  maxInside,
  nearestIndex,
  pinCorner,
  placeThresholdLabel,
  sameDomain,
  textWidth,
  timeTicks,
  usdTickFormat,
  usdTicks,
  whaleAutorange,
  whaleRadius,
  type Corner,
  type Domain,
  type View,
  type Visibility,
} from "./model";
import { useFontsReady, useSettled, useTweened } from "./hooks";
import { Overview } from "./Overview";
import { PinnedWhale, WhaleTip } from "./WhaleCard";
import { useReplayFrames, useReplaySnapshot, type ReplayEngine } from "./replay";

export interface TimelineChartProps {
  tl: TimelineData;
  view: View;
  vis: Visibility;
  domain: Domain;
  /** Where the x-domain is heading (animation target) — y axes fit to this. */
  fitDomain: Domain;
  full: Domain;
  pinned: number | null;
  onPin: (i: number | null) => void;
  onDomain: (d: Domain) => void;
  onReset: () => void;
  /** Market replay: while it is active the chart reveals marks up to its playhead. */
  replay: ReplayEngine;
}

/** Measurement strings mirror the CSS: 11px/600 labels (.tl-label, .tl-event-label, .tl-threshold-label), 11px/500 ticks. */
const LABEL_FONT = `600 11px ${FONT_SANS}`;
const LABEL_FONT_SM = `600 10.5px ${FONT_SANS}`;
const EVENT_FONT = `600 11px ${FONT_SANS}`;
const TITLE_FONT = `600 11px ${FONT_SANS}`;
const TITLE_FONT_SM = `600 10.5px ${FONT_SANS}`;
const TICK_FONT = `500 11px ${FONT_SANS}`;
const TICK_FONT_SM = `500 10.5px ${FONT_SANS}`;
const THRESH_FONT = `600 11px ${FONT_SANS}`;
const TIME_FONT = `600 11px ${FONT_SANS}`;
/** Clearance kept either side of the threshold label's text, px. */
const THRESH_PAD_X = 4;

/** Margin widths, desktop / mobile. */
const DESKTOP = { rNone: 18, bottom: 30 };
const MOBILE = { rNone: 10, bottom: 28 };
/** Below this plot width, names that would need a long leader are left to their tooltip. */
const NARROW_PLOT = 500;

interface Dot {
  i: number;
  x: number;
  y: number;
  r: number;
  hi: boolean;
}

interface Hover {
  px: number;
  py: number;
  t: number;
  dot: Dot | null;
}

export function TimelineChart(props: TimelineChartProps) {
  const [ref, { width }] = useSize<HTMLDivElement>();
  return (
    <div ref={ref} className="chart tl-chart">
      {width > 0 && <ChartBody {...props} width={width} />}
    </div>
  );
}

function ChartBody({ tl, view, vis, domain, fitDomain, full, pinned, onPin, onDomain, onReset, replay, width }: TimelineChartProps & { width: number }) {
  const fontsReady = useFontsReady();
  const clipId = "tl-clip-" + useId().replace(/[^a-zA-Z0-9_-]/g, "");
  // Discrete replay changes (start / pause / checkpoint / end) re-render; the
  // playhead itself moves through DOM writes (see applyReplay below).
  const rp = useReplaySnapshot(replay);
  const replayOn = rp.active;
  const mobile = width < 640;
  const height = mobile ? 420 : 560;
  const M = mobile ? MOBILE : DESKTOP;
  const hasOdds = tl.odds.t.length > 1;
  const showOdds = vis.odds && hasOdds;
  const hasEvents = tl.events.length > 0;
  const sizeScale = mobile ? 0.74 : 1;
  const topTarget = hasEvents ? 34 : 16;
  const plotHTarget = Math.max(80, height - topTarget - M.bottom);

  /* ── x padding: the domain is inset so edge dots are never cut in half ─ */
  const maxDisplay = useMemo(() => d3.max(tl.whales, (w) => w.display) || 1, [tl]);
  const maxR = useMemo(() => d3.max(tl.whales, (w) => whaleRadius(w, maxDisplay, sizeScale)) ?? 0, [tl, maxDisplay, sizeScale]);
  const padTarget = view === "crowd" || !tl.whales.length ? 0 : Math.ceil(maxR + 3);

  /* ── y fitting (to the settled / target x-domain) ─────────────────── */
  const maxAvg = useMemo(() => d3.max(tl.crowdHourly.avg) || 1, [tl]);
  const settled = useSettled(fitDomain, 110, sameDomain);
  const lastFit = useRef<{ w: [number, number]; c: number } | null>(null);
  const fit = useMemo(() => {
    const [s0, s1] = settled;
    // Whales drawn in the side padding count too (plot width estimated, to avoid a layout loop).
    const ext = padTarget > 0 ? ((s1 - s0) * padTarget) / Math.max(120, width - 140) : 0;
    const w =
      whaleAutorange(tl.whales, s0 - ext, s1 + ext, vis, maxDisplay, sizeScale, plotHTarget) ??
      lastFit.current?.w ??
      ([0, maxDisplay * 1.1] as [number, number]);
    const c = maxInside(tl.crowdHourly.t, tl.crowdHourly.avg, s0, s1) ?? lastFit.current?.c ?? maxAvg;
    return { w, c: c > 0 ? c : maxAvg };
  }, [tl, settled, vis, maxDisplay, maxAvg, sizeScale, plotHTarget, padTarget, width]);
  useEffect(() => {
    lastFit.current = fit;
  }, [fit]);
  // Dash: whale y autoranged by Plotly (markers + 5%); crowd y = [0, 1.1 × max(avg)]
  // in the crowd view and autoranged to the filled line (top = max / 0.95) in the comparison.
  const cTopTarget = view === "crowd" ? fit.c * 1.1 : fit.c / 0.95;
  const [wLo, wHi, cTop] = useTweened([fit.w[0], fit.w[1], cTopTarget], 420);

  /* ── margins sized from the tick labels they must hold ────────────── */
  const nY = Math.max(3, Math.round(plotHTarget / (mobile ? 70 : 82)));
  const tickFont = mobile ? TICK_FONT_SM : TICK_FONT;
  const gutter = mobile ? 28 : 32; // tick padding + rotated title + gaps
  const tickW = useMemo(() => {
    void fontsReady;
    const widest = (ticks: number[]) => {
      const f = usdTickFormat(ticks);
      return d3.max(ticks, (v) => textWidth(f(v), tickFont)) ?? 30;
    };
    return {
      w: widest(usdTicks(fit.w[0], fit.w[1], nY)),
      c: widest(usdTicks(0, cTopTarget, nY)),
      odds: textWidth("100%", tickFont),
    };
  }, [fit, cTopTarget, nY, tickFont, fontsReady]);
  const leftTarget = Math.ceil((view === "whales" ? tickW.w : tickW.c) + gutter);
  const rWhale = Math.ceil(tickW.w + gutter);
  const rOdds = Math.ceil(tickW.odds + gutter);
  const rightTarget = view === "cmp" ? rWhale + (showOdds ? rOdds : 0) : showOdds ? rOdds : M.rNone;
  const [mL, mR, mT, rW, padX] = useTweened([leftTarget, rightTarget, topTarget, rWhale, padTarget], 340);
  const mB = M.bottom;
  const plotW = Math.max(40, width - mL - mR);
  const plotH = Math.max(80, height - mT - mB);
  const pad = Math.max(0, Math.min(padX, (plotW - 20) / 2));
  const innerW = plotW - 2 * pad;

  /* ── scales ───────────────────────────────────────────────────────── */
  const plot = useMemo(() => {
    const [d0, d1] = domain;
    // The domain maps to [pad, plotW - pad]; [v0, v1] is what the plot shows.
    const kx = innerW / (d1 - d0);
    const v0 = d0 - pad / kx;
    const v1 = d1 + pad / kx;
    const sx = (t: number) => (t - v0) * kx;
    const xUtc = d3.scaleUtc().domain([toDate(v0), toDate(v1)]).range([0, plotW]);
    const wy = d3.scaleLinear().domain([wLo, wHi]).range([plotH, 0]);
    const cy = d3.scaleLinear().domain([0, cTop]).range([plotH, 0]);
    const oy = d3.scaleLinear().domain([0, 100]).range([plotH, 0]);
    return { d0, d1, v0, v1, kx, pad, innerW, sx, xUtc, wy, cy, oy, plotW, plotH };
  }, [domain, plotW, plotH, innerW, pad, wLo, wHi, cTop]);

  const wTicks = useMemo(() => usdTicks(wLo, wHi, nY), [wLo, wHi, nY]);
  const cTicks = useMemo(() => usdTicks(0, cTop, nY), [cTop, nY]);
  const leftScale = view === "whales" ? plot.wy : plot.cy;
  const leftTicks = view === "whales" ? wTicks : cTicks;

  /* ── marks ────────────────────────────────────────────────────────── */
  const paths = useMemo(() => {
    const { v0, v1, sx } = plot;
    const h = tl.crowdHourly;
    return {
      odds: showOdds ? linePath(tl.odds.t, tl.odds.pct, v0, v1, sx, plot.oy, plotW) : "",
      crowdAvg: view !== "whales" ? linePath(h.t, h.avg, v0, v1, sx, plot.cy, plotW) : "",
      crowdWash: view === "cmp" ? areaPath(h.t, h.avg, v0, v1, sx, plot.cy, plotW, plotH, true) : "",
      crowdRaw: view === "crowd" ? areaPath(h.t, h.vol, v0, v1, sx, plot.cy, plotW, plotH) : "",
    };
  }, [plot, view, showOdds, tl, plotW, plotH]);

  const dots = useMemo(() => {
    const out: Dot[] = [];
    if (view === "crowd") return out;
    tl.whales.forEach((w, i) => {
      const hi = !!w.label;
      if (hi ? !vis.highlighted : !vis.whales) return;
      const r = whaleRadius(w, maxDisplay, sizeScale);
      const x = plot.sx(w.t);
      if (x < -r || x > plot.plotW + r) return;
      out.push({ i, x, y: plot.wy(w.display), r, hi });
    });
    // Big dots underneath, small on top; highlighted users above everything.
    out.sort((a, b) => (a.hi === b.hi ? b.r - a.r : a.hi ? 1 : -1));
    return out;
  }, [tl, plot, vis.whales, vis.highlighted, view, maxDisplay, sizeScale]);

  /** Visible whales in time order, for keyboard stepping. */
  const timeOrder = useMemo(
    () => [...dots].sort((a, b) => tl.whales[a.i].t - tl.whales[b.i].t || a.i - b.i),
    [dots, tl],
  );

  const labelFont = mobile ? LABEL_FONT_SM : LABEL_FONT;
  const labels = useMemo(() => {
    void fontsReady;
    const items = dots
      .filter((d) => d.hi && d.x >= 0 && d.x <= plot.plotW)
      .map((d) => {
        const text = tl.whales[d.i].label ?? "";
        return { key: d.i, x: d.x, y: d.y, r: d.r, text, w: textWidth(text, labelFont) };
      });
    const others = dots.filter((d) => !d.hi);
    return layoutLabels(items, plot.plotW, plot.plotH, others, {
      lineH: mobile ? 14 : 15,
      maxLeader: plot.plotW < NARROW_PLOT ? 40 : Infinity,
    });
  }, [dots, plot, tl, fontsReady, labelFont, mobile]);

  const chips = useMemo(() => {
    void fontsReady;
    if (!vis.events) return [];
    const list = tl.events
      .map((e) => ({ e, x: plot.sx(e.t), w: textWidth(e.label, EVENT_FONT) + 8 }))
      .filter((c) => c.x >= -0.5 && c.x <= plot.plotW + 0.5)
      .sort((a, b) => a.x - b.x);
    const rowsEnd: number[] = [];
    return list.map((c) => {
      const cx = Math.min(Math.max(c.x, c.w / 2), plot.plotW - c.w / 2);
      let row = rowsEnd.findIndex((end) => end + 6 <= cx - c.w / 2);
      if (row < 0) {
        row = rowsEnd.length;
        rowsEnd.push(0);
      }
      rowsEnd[row] = cx + c.w / 2;
      return { ...c, cx, row };
    });
  }, [tl, plot, vis.events, fontsReady]);

  /* ── axes (d3 renders into refs) ──────────────────────────────────── */
  const xAxisRef = useRef<SVGGElement>(null);
  const leftAxisRef = useRef<SVGGElement>(null);
  const whaleRightRef = useRef<SVGGElement>(null);
  const oddsAxisRef = useRef<SVGGElement>(null);

  useLayoutEffect(() => {
    if (xAxisRef.current) {
      d3.select(xAxisRef.current).call(
        d3
          .axisBottom(plot.xUtc)
          .tickValues(timeTicks(plot.xUtc, plot.plotW, mobile ? TICK_FONT_SM : TICK_FONT, mobile ? 85 : 140))
          .tickSizeOuter(0)
          .tickSize(5)
          .tickPadding(7)
          .tickFormat((d) => timeTickFormat(d as Date)),
      );
    }
    const yAxis = (node: SVGGElement | null, axis: d3.Axis<d3.NumberValue>) => {
      if (!node) return;
      d3.select(node)
        .call(axis.tickSize(0).tickPadding(8))
        .call((g) => g.select(".domain").remove());
    };
    const leftFmt = usdTickFormat(leftTicks);
    yAxis(
      leftAxisRef.current,
      d3
        .axisLeft(leftScale)
        .tickValues(leftTicks)
        .tickFormat((v) => leftFmt(+v)),
    );
    if (view === "cmp") {
      const wFmt = usdTickFormat(wTicks);
      yAxis(
        whaleRightRef.current,
        d3
          .axisRight(plot.wy)
          .tickValues(wTicks)
          .tickFormat((v) => wFmt(+v)),
      );
    }
    if (showOdds) {
      yAxis(
        oddsAxisRef.current,
        d3
          .axisRight(plot.oy)
          .tickValues([0, 25, 50, 75, 100])
          .tickFormat((v) => `${+v}%`),
      );
    }
  }, [plot, leftScale, leftTicks, wTicks, view, showOdds, mobile]);

  /* ── interaction ──────────────────────────────────────────────────── */
  const overlayRef = useRef<SVGRectElement>(null);
  const zoomRef = useRef<d3.ZoomBehavior<SVGRectElement, unknown> | null>(null);
  const syncing = useRef(false);
  const dragRef = useRef(false);
  const lastPtr = useRef<{ px: number; py: number } | null>(null);
  const [hover, setHover] = useState<Hover | null>(null);
  const [dragging, setDragging] = useState(false);
  /** Whale stepped to with the keyboard (index into tl.whales). */
  const [kbFocus, setKbFocus] = useState<number | null>(null);

  const live = useRef({ plot, dots, domain, full, onDomain, replay });
  live.current = { plot, dots, domain, full, onDomain, replay };

  const hitTest = useCallback(
    (px: number, py: number): Dot | null => {
      const { dots, replay } = live.current;
      // Dots the replay has not reached yet are not there to hit.
      const until = replay.active ? replay.t : Infinity;
      let best: Dot | null = null;
      let bd = Infinity;
      for (let k = 0; k < dots.length; k++) {
        const d = dots[k];
        if (tl.whales[d.i].t > until) continue;
        const dist = Math.hypot(d.x - px, d.y - py);
        if (dist <= Math.max(d.r + 8, 20) && dist <= bd) {
          bd = dist;
          best = d;
        }
      }
      return best;
    },
    [tl],
  );

  const computeHover = useCallback(
    (px: number, py: number): Hover => {
      const { plot } = live.current;
      const dot = hitTest(px, py);
      const t = dot ? tl.whales[dot.i].t : Math.round((plot.v0 + px / plot.kx) / 60) * 60;
      return { px, py, t, dot };
    },
    [hitTest, tl],
  );

  // Keep the readout live while the domain moves under a still pointer.
  useEffect(() => {
    if (lastPtr.current && !dragRef.current) setHover(computeHover(lastPtr.current.px, lastPtr.current.py));
  }, [plot, dots, computeHover]);

  // Layout effect (declared before the sync below) so d3.zoom exists before
  // its transform is first mirrored from the domain. The overlay sits in a
  // group shifted by `pad`, so d3.zoom works in domain pixels [0, innerW].
  useLayoutEffect(() => {
    const el = overlayRef.current;
    if (!el) return;
    const z = d3
      .zoom<SVGRectElement, unknown>()
      .filter((ev: Event) => {
        const type = ev.type;
        if (type === "wheel" || type === "dblclick") return false; // handled below
        if (type.startsWith("touch")) return ((ev as TouchEvent).touches?.length ?? 0) >= 2; // pinch only
        const me = ev as MouseEvent;
        return !me.ctrlKey && !me.button;
      })
      .on("zoom", (ev: d3.D3ZoomEvent<SVGRectElement, unknown>) => {
        if (syncing.current) return;
        const src = ev.sourceEvent as Event | null;
        if (src && (src.type === "mousemove" || src.type === "touchmove") && !dragRef.current) {
          dragRef.current = true;
          setDragging(true);
          setHover(null);
        }
        const { full, plot, onDomain } = live.current;
        const t = ev.transform;
        const fw = full[1] - full[0];
        const d0 = full[0] + (t.invertX(0) / plot.innerW) * fw;
        const d1 = full[0] + (t.invertX(plot.innerW) / plot.innerW) * fw;
        onDomain(clampDomain([d0, d1], full));
      })
      .on("end", () => {
        if (syncing.current) return;
        if (dragRef.current) {
          dragRef.current = false;
          setDragging(false);
        }
      });
    zoomRef.current = z;
    d3.select(el).call(z).on("dblclick.zoom", null);

    // Same wheel contract as the traders constellation: a plain vertical wheel
    // scrolls the page; ⌘/Ctrl + wheel (and trackpad pinch, which arrives as
    // ctrl + wheel) zooms; a horizontal wheel or Shift + wheel pans.
    const onWheel = (ev: WheelEvent) => {
      const { domain, full, plot, onDomain } = live.current;
      const unit = ev.deltaMode === 1 ? 16 : ev.deltaMode === 2 ? plot.plotH : 1;
      const dx = ev.deltaX * unit;
      const dy = ev.deltaY * unit;
      const span = domain[1] - domain[0];
      const fw = full[1] - full[0];
      const rect = el.getBoundingClientRect();
      const px = Math.min(Math.max(ev.clientX - rect.left, 0), plot.plotW);
      const zoom = ev.ctrlKey || ev.metaKey;
      // Shift + wheel: Chromium/WebKit already deliver it as deltaX; Firefox keeps deltaY.
      const pan = !zoom && (Math.abs(dx) > Math.abs(dy) || (ev.shiftKey && dy !== 0));
      let next: Domain;
      if (pan) {
        // Always claimed: a stray horizontal swipe would otherwise navigate back.
        const shift = (Math.abs(dx) > Math.abs(dy) ? dx : dy) / plot.kx;
        next = [domain[0] + shift, domain[1] + shift];
      } else if (zoom) {
        // Pinch sends small ctrl + wheel deltas; a ⌘/Ctrl mouse notch is ~100px.
        const pinch = ev.ctrlKey && ev.deltaMode === 0 && Math.abs(ev.deltaY) < 40;
        const factor = Math.pow(2, Math.max(-0.8, Math.min(0.8, dy * (pinch ? 0.012 : 0.0022))));
        const minSpan = Math.min(HOUR, fw);
        const ns = Math.min(fw, Math.max(minSpan, span * factor));
        const tp = plot.v0 + px / plot.kx; // time under the pointer stays put
        const f = (px - plot.pad) / plot.innerW;
        next = [tp - f * ns, tp - f * ns + ns];
      } else {
        return; // the page scrolls; the heading hint says how to zoom
      }
      // Claimed even at a zoom limit: ctrl + wheel would otherwise zoom the browser.
      ev.preventDefault();
      const c = clampDomain(next, full);
      if (!sameDomain(c, domain, 0.5)) onDomain(c);
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => {
      d3.select(el).on(".zoom", null);
      el.removeEventListener("wheel", onWheel);
    };
  }, []);

  // Mirror the domain into d3.zoom's stored transform (full → [0, innerW]).
  useLayoutEffect(() => {
    const z = zoomRef.current;
    const el = overlayRef.current;
    if (!z || !el) return;
    const fw = full[1] - full[0];
    const k = fw / (domain[1] - domain[0]);
    const tx = -k * ((domain[0] - full[0]) / fw) * innerW;
    z.extent([
      [0, 0],
      [innerW, plotH],
    ])
      .translateExtent([
        [0, -1e9],
        [innerW, 1e9],
      ])
      .scaleExtent([1, Math.max(1, fw / HOUR)]);
    syncing.current = true;
    d3.select(el).call(z.transform, d3.zoomIdentity.translate(tx, 0).scale(k));
    syncing.current = false;
  }, [domain, full, innerW, plotH]);

  const onPointerMove = (e: RPointerEvent<SVGRectElement>) => {
    const r = e.currentTarget.getBoundingClientRect();
    const px = e.clientX - r.left;
    const py = e.clientY - r.top;
    lastPtr.current = { px, py };
    if (kbFocus != null) setKbFocus(null);
    if (dragRef.current) return;
    setHover(computeHover(px, py));
  };
  const onPointerLeave = () => {
    lastPtr.current = null;
    setHover(null);
  };
  const onClick = (e: RMouseEvent<SVGRectElement>) => {
    const r = e.currentTarget.getBoundingClientRect();
    const dot = hitTest(e.clientX - r.left, e.clientY - r.top);
    onPin(dot ? dot.i : null);
  };

  /** Step the keyboard focus through the visible whales in time order. */
  const step = (dir: 1 | -1) => {
    // During a replay only the whales the playhead has reached are there to step through.
    const order = replayOn ? timeOrder.filter((d) => tl.whales[d.i].t <= replay.t) : timeOrder;
    if (!order.length) return;
    const from = kbFocus ?? pinned;
    let k = from != null ? order.findIndex((d) => d.i === from) : -1;
    if (k >= 0) k = Math.min(order.length - 1, Math.max(0, k + dir));
    else if (from != null) {
      // The focused whale scrolled out of view: continue from its time.
      const t = tl.whales[from].t;
      k = dir > 0 ? order.findIndex((d) => tl.whales[d.i].t >= t) : order.findLastIndex((d) => tl.whales[d.i].t <= t);
      if (k < 0) k = dir > 0 ? order.length - 1 : 0;
    } else k = dir > 0 ? 0 : order.length - 1;
    setHover(null);
    setKbFocus(order[k].i);
  };

  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.target !== e.currentTarget) return; // not while focus is inside the pinned card
    // During a replay ← → nudge the playhead instead (handled by the card).
    if (replayOn && (e.key === "ArrowLeft" || e.key === "ArrowRight")) return;
    const span = domain[1] - domain[0];
    const c = (domain[0] + domain[1]) / 2;
    let next: Domain | null = null;
    if (e.key === "ArrowLeft") next = [domain[0] - span * 0.1, domain[1] - span * 0.1];
    else if (e.key === "ArrowRight") next = [domain[0] + span * 0.1, domain[1] + span * 0.1];
    else if (e.key === "+" || e.key === "=") next = [c - span / 3, c + span / 3];
    else if (e.key === "-" || e.key === "_") next = [c - span * 0.75, c + span * 0.75];
    else if (e.key === "0" || e.key === "Home") {
      e.preventDefault();
      onReset();
      return;
    } else if (e.key === "ArrowDown" || e.key === "]") {
      if (!timeOrder.length) return;
      e.preventDefault();
      step(1);
      return;
    } else if (e.key === "ArrowUp" || e.key === "[") {
      if (!timeOrder.length) return;
      e.preventDefault();
      step(-1);
      return;
    } else if ((e.key === "Enter" || e.key === " ") && kbFocus != null) {
      e.preventDefault();
      onPin(kbFocus === pinned ? null : kbFocus);
      return;
    } else if (e.key === "Escape" && kbFocus != null && pinned == null) {
      setKbFocus(null);
      return;
    }
    if (next) {
      e.preventDefault();
      onDomain(clampDomain(next, full));
    }
  };
  const onBlur = (e: FocusEvent<HTMLDivElement>) => {
    if (!e.currentTarget.contains(e.relatedTarget as Node | null)) setKbFocus(null);
  };

  /* ── hover readouts ───────────────────────────────────────────────── */
  // A keyboard focus the playhead has since been scrubbed back past is dropped (nothing is drawn there).
  const kbDot =
    kbFocus != null && !(replayOn && tl.whales[kbFocus].t > replay.t) ? (dots.find((d) => d.i === kbFocus) ?? null) : null;
  const active: Hover | null =
    hover ?? (kbDot ? { px: kbDot.x, py: kbDot.y, t: tl.whales[kbDot.i].t, dot: kbDot } : null);
  // The tooltip fades out where it last was (with what it last said).
  const lastTip = useRef<Hover | null>(null);
  if (active) lastTip.current = active;
  const tip = active ?? lastTip.current;

  const focusIdx = active?.dot?.i ?? null;
  const replayVersion = rp.version;
  const readout = useMemo(() => {
    if (!tip || tip.dot) return null;
    const t = tip.t;
    // Past the playhead there is nothing drawn to read out yet.
    void replayVersion;
    if (replay.active && t > replay.t) return null;
    const out: { odds?: { t: number; v: number }; avg?: { t: number; v: number }; raw?: { hour: number; v: number } } = {};
    if (showOdds) {
      const i = nearestIndex(tl.odds.t, t);
      if (i >= 0) out.odds = { t: tl.odds.t[i], v: tl.odds.pct[i] };
    }
    if (view !== "whales" && vis.crowd) {
      const h = tl.crowdHourly;
      const i = nearestIndex(h.t, t);
      if (i >= 0) out.avg = { t: h.t[i], v: h.avg[i] };
    }
    if (view === "crowd" && vis.crowdRaw) {
      const hour = Math.floor(t / HOUR) * HOUR;
      const h = tl.crowdHourly;
      const i = d3.bisectLeft(h.t, hour);
      out.raw = { hour, v: h.t[i] === hour ? h.vol[i] : 0 };
    }
    return out;
  }, [tip, showOdds, view, vis.crowd, vis.crowdRaw, tl, replay, replayVersion]);

  const hasRows = !!(readout && (readout.odds || readout.avg || readout.raw));

  /* ── memoised layers (unchanged by pointer moves) ─────────────────── */
  const gridEl = useMemo(
    () => (
      <g className="grid" aria-hidden>
        {leftTicks.map((v) => (
          <line key={v} x1={0} x2={plot.plotW} y1={Math.round(leftScale(v)) + 0.5} y2={Math.round(leftScale(v)) + 0.5} />
        ))}
      </g>
    ),
    [leftTicks, leftScale, plot.plotW],
  );

  // `data-i` keys each mark to its whale; the replay reveals marks by setting
  // `data-on` on them directly (React never writes that attribute).
  const dotsEl = useMemo(
    () => (
      <g key="whales" className={`tl-layer tl-dots${focusIdx != null ? " dim" : ""}`}>
        {dots.map((d) => (
          <circle
            key={d.i}
            data-i={d.i}
            className={`tl-dot${d.hi ? " hi" : ""}${d.i === focusIdx || d.i === pinned ? " on" : ""}`}
            cx={d.x}
            cy={d.y}
            r={d.r}
          />
        ))}
      </g>
    ),
    [dots, focusIdx, pinned],
  );

  // Highlighted users get a pulse ring when the replay reaches them.
  const pulsesEl = useMemo(
    () =>
      replayOn ? (
        <g className="tl-pulses" aria-hidden>
          {dots
            .filter((d) => d.hi)
            .map((d) => (
              <circle key={d.i} data-i={d.i} className="tl-pulse" cx={d.x} cy={d.y} r={d.r + 2} />
            ))}
        </g>
      ) : null,
    [dots, replayOn],
  );

  /* ── replay: marks reveal up to the playhead, through DOM writes per frame ── */
  const svgRef = useRef<SVGSVGElement>(null);
  const revealRef = useRef<SVGRectElement>(null);
  const headRef = useRef<SVGGElement>(null);
  const chipRef = useRef<SVGGElement>(null);
  const chipRectRef = useRef<SVGRectElement>(null);
  const chipTextRef = useRef<SVGTextElement>(null);
  const marks = useRef<{ els: Map<number, Element[]>; state: Uint8Array; evs: { el: Element; t: number }[] } | null>(null);

  const applyReplay = useCallback(
    (t: number, bulk: boolean) => {
      const { plot, replay } = live.current;
      const on = replay.active;
      const x = plot.sx(t);
      // the reveal clip: every line / area is drawn only up to the playhead
      revealRef.current?.setAttribute("width", String(Math.max(0, Math.min(plot.plotW, x))));
      // the playhead and its time chip (kept inside the plot)
      const head = headRef.current;
      if (head) {
        const inside = x >= -0.5 && x <= plot.plotW + 0.5;
        head.style.display = inside ? "" : "none";
        head.setAttribute("transform", `translate(${x},0)`);
        const label = fmtT(t);
        const w = textWidth(label, TIME_FONT) + 14;
        const cx = Math.min(Math.max(x - w / 2, 0), Math.max(0, plot.plotW - w)) - x;
        chipRef.current?.setAttribute("transform", `translate(${cx},6)`);
        chipRectRef.current?.setAttribute("width", String(w));
        const text = chipTextRef.current;
        if (text) {
          text.setAttribute("x", String(w / 2));
          if (text.textContent !== label) text.textContent = label;
        }
      }
      const m = marks.current;
      if (!m) return;
      if (on) {
        for (const [i, els] of m.els) {
          const v = tl.whales[i].t <= t ? 1 : 0;
          if (m.state[i] === v) continue;
          m.state[i] = v;
          for (const el of els) {
            if (!v) el.removeAttribute("data-on");
            else {
              el.setAttribute("data-on", "");
              // a bulk pass (zoom, seek) places marks; only playback pulses them
              if (bulk) el.setAttribute("data-quiet", "");
              else el.removeAttribute("data-quiet");
            }
          }
        }
      }
      for (const ev of m.evs) {
        ev.el.setAttribute("data-state", !on ? "" : replay.checkpoint === ev.t ? "now" : ev.t > t ? "future" : "past");
      }
    },
    [tl],
  );

  // Marks are re-collected whenever React may have created new ones.
  useLayoutEffect(() => {
    const svg = svgRef.current;
    if (!svg) return;
    const els = new Map<number, Element[]>();
    svg.querySelectorAll<SVGElement>("[data-i]").forEach((el) => {
      const i = Number(el.dataset.i);
      const list = els.get(i);
      if (list) list.push(el);
      else els.set(i, [el]);
    });
    const evs: { el: Element; t: number }[] = [];
    svg.querySelectorAll<SVGElement>("[data-t]").forEach((el) => evs.push({ el, t: Number(el.dataset.t) }));
    marks.current = { els, state: new Uint8Array(tl.whales.length).fill(2), evs };
    applyReplay(replay.t, true);
  }, [dots, labels, chips, pinned, replayOn, rp.checkpoint, tl, replay, applyReplay]);

  useReplayFrames(replay, (t) => applyReplay(t, !replay.playing), [applyReplay, replay, plot]);

  const thresholdY = plot.wy(tl.threshold);
  const showThreshold = view === "whales" && vis.threshold && thresholdY >= 0 && thresholdY <= plot.plotH;
  const thresholdLabel = `Whale threshold ${fmtUSD(tl.threshold)}`;
  // Put the threshold label wherever along its line it hides the least; moved
  // off the line (with a leader) when the line is buried in dots, dropped when
  // even that would hide data — the legend still names the line.
  const thresholdPos = useMemo(() => {
    if (!showThreshold) return null;
    void fontsReady;
    const ts = tl.odds.t;
    const ps = tl.odds.pct;
    const oddsSpan = showOdds
      ? (x0: number, x1: number): [number, number] | null => {
          const i0 = Math.max(0, d3.bisectLeft(ts, plot.v0 + x0 / plot.kx) - 1);
          const i1 = Math.min(ts.length, d3.bisectRight(ts, plot.v0 + x1 / plot.kx) + 1);
          if (i1 <= i0) return null;
          let lo = Infinity;
          let hi = -Infinity;
          for (let i = i0; i < i1; i++) {
            const y = plot.oy(ps[i]);
            if (y < lo) lo = y;
            if (y > hi) hi = y;
          }
          return [lo, hi];
        }
      : undefined;
    return placeThresholdLabel({
      lineY: thresholdY,
      w: textWidth(thresholdLabel, THRESH_FONT) + 2 * THRESH_PAD_X,
      W: plot.plotW,
      H: plot.plotH,
      dots,
      labels: labels.map((l) => l.box),
      oddsSpan,
    });
  }, [showThreshold, thresholdLabel, thresholdY, dots, labels, plot, showOdds, tl, fontsReady]);

  // Which right axes exist, and where.
  const whaleAxisX = plot.plotW;
  const oddsAxisX = view === "cmp" ? plot.plotW + rW : plot.plotW;
  const titleFont = mobile ? TITLE_FONT_SM : TITLE_FONT;

  const leftTitle =
    view === "whales"
      ? { text: "Position size (USD)", shape: "dot" as const, color: "var(--c-whale)" }
      : view === "crowd"
        ? { text: "Volume per hour (USD)", shape: "line" as const, color: "var(--c-crowd)" }
        : { text: "Crowd volume / hr", shape: "line" as const, color: "var(--c-crowd)" };

  const activeDot = active?.dot ?? null;
  const tipDot = tip?.dot ?? null;
  const tipWhale = tipDot ? tl.whales[tipDot.i] : null;
  const crossX = active ? plot.sx(active.t) : null;

  /* ── pinned card: floats in the corner away from its dot; docks below on phones ── */
  const pinnedWhale = pinned != null ? tl.whales[pinned] : null;
  const pinnedDot = pinned != null ? (dots.find((d) => d.i === pinned) ?? null) : null;
  const docked = mobile;
  const pinRef = useRef<HTMLDivElement>(null);
  const [pinBox, setPinBox] = useState({ w: 300, h: 300 });
  useLayoutEffect(() => {
    const el = pinRef.current;
    if (!el || docked) return;
    const w = el.offsetWidth;
    const h = el.offsetHeight;
    if (Math.abs(w - pinBox.w) > 0.5 || Math.abs(h - pinBox.h) > 0.5) setPinBox({ w, h });
  });
  const cornerRef = useRef<{ corner: Corner; pin: number | null }>({ corner: "tr", pin: null });
  const pinPlace = useMemo(
    () =>
      pinCorner(cornerRef.current.corner, cornerRef.current.pin === pinned, pinnedDot, pinBox, plot.plotW, plot.plotH),
    [pinned, pinnedDot, pinBox, plot.plotW, plot.plotH],
  );
  useEffect(() => {
    cornerRef.current = { corner: pinPlace.corner, pin: pinned };
  }, [pinPlace.corner, pinned]);

  const ariaLabel =
    view === "whales"
      ? `Scatter of ${dots.length} whale positions over time${showOdds ? ", with market odds" : ""}.`
      : view === "crowd"
        ? `Crowd volume per hour over time${showOdds ? ", with market odds" : ""}.`
        : `Comparison of crowd volume, whale positions${showOdds ? " and market odds" : ""} over time.`;
  const keysHelp =
    (view === "crowd"
      ? "Arrow keys pan, plus and minus zoom, 0 resets."
      : "Left and right arrows pan, plus and minus zoom, 0 resets; up and down arrows step through whales, Enter pins one.") +
    " Space plays or pauses the market replay.";
  const reveal = replayOn ? `url(#${clipId}-r)` : undefined;

  return (
    <div
      className={`tl-chart-body${dragging ? " is-dragging" : ""}${activeDot ? " is-over-dot" : ""}${replayOn ? " is-replay" : ""}${rp.playing ? " is-playing" : ""}`}
      tabIndex={0}
      role="group"
      aria-label={`${ariaLabel} ${keysHelp}`}
      onKeyDown={onKeyDown}
      onBlur={onBlur}
    >
      <svg ref={svgRef} width={width} height={height} className="tl-svg" role="img" aria-label={ariaLabel}>
        <defs>
          <clipPath id={clipId}>
            <rect x={0} y={-4} width={plot.plotW} height={plot.plotH + 6} />
          </clipPath>
          <clipPath id={clipId + "-x"}>
            <rect x={0} y={0} width={plot.plotW} height={plot.plotH} />
          </clipPath>
          {/* replay reveal: its width follows the playhead (set by applyReplay, never by React) */}
          <clipPath id={clipId + "-r"}>
            <rect ref={revealRef} x={0} y={-4} height={plot.plotH + 6} />
          </clipPath>
        </defs>
        <g transform={`translate(${mL},${mT})`}>
          {gridEl}

          {/* events: dashed hairline from the label to the baseline */}
          <g className="tl-events" aria-hidden>
            {chips.map((c) => (
              <line
                key={c.e.t}
                data-t={c.e.t}
                className="tl-event-line"
                x1={Math.round(c.x) + 0.5}
                x2={Math.round(c.x) + 0.5}
                y1={c.row === 0 ? -mT + 26 : 4 + c.row * 20 + 14}
                y2={plot.plotH}
              />
            ))}
          </g>

          <g clipPath={`url(#${clipId})`}>
            <g clipPath={reveal}>
              {view === "crowd" && vis.crowdRaw && (
                <path key="crowdRaw" className="tl-layer tl-crowd-raw" d={paths.crowdRaw} clipPath={`url(#${clipId}-x)`} />
              )}
              {view === "cmp" && vis.crowd && <path key="crowdWash" className="tl-layer tl-crowd-wash" d={paths.crowdWash} />}
              {view !== "whales" && vis.crowd && <path key="crowdAvg" className="tl-layer tl-crowd-line" d={paths.crowdAvg} />}
            </g>
            {showThreshold && (
              <line key="threshold" className="tl-layer tl-threshold" x1={0} x2={plot.plotW} y1={thresholdY} y2={thresholdY} />
            )}
            {view !== "crowd" && dotsEl}
            {view !== "crowd" && pulsesEl}
            <g clipPath={reveal}>{showOdds && <path key="odds" className="tl-layer tl-odds" d={paths.odds} />}</g>
          </g>

          {thresholdPos && (
            <g className="tl-threshold-tag" transform={`translate(${thresholdPos.x},${thresholdPos.y})`} aria-hidden>
              {/* the placement box is the text box widened by THRESH_PAD_X each side */}
              {thresholdPos.leader && (
                <line
                  className="tl-threshold-leader"
                  x1={textWidth(thresholdLabel, THRESH_FONT) / 2 + THRESH_PAD_X}
                  x2={textWidth(thresholdLabel, THRESH_FONT) / 2 + THRESH_PAD_X}
                  y1={thresholdY > thresholdPos.y ? 4.5 : -12.5}
                  y2={thresholdY - thresholdPos.y}
                />
              )}
              <text className="tl-threshold-label" x={THRESH_PAD_X} y={0}>
                {thresholdLabel}
              </text>
            </g>
          )}

          {/* direct labels for highlighted users */}
          {view !== "crowd" && (
            <g className="tl-labels" aria-hidden>
              {labels.map((l) => (
                <g key={l.key} data-i={l.key} className={focusIdx != null && focusIdx !== l.key ? "dim" : undefined}>
                  {l.leader && <polyline className="tl-leader" points={l.leader.map((p) => p.join(",")).join(" ")} />}
                  <text className="tl-label" x={l.x} y={l.y} textAnchor={l.anchor}>
                    {l.text}
                  </text>
                </g>
              ))}
            </g>
          )}

          {/* replay playhead: an ink hairline with a time chip, moved by applyReplay */}
          {replayOn && (
            <g ref={headRef} className="tl-playhead" aria-hidden>
              <line className="tl-playhead-line" x1={0} x2={0} y1={-2} y2={plot.plotH} />
              <g ref={chipRef} className="tl-playhead-chip">
                <rect ref={chipRectRef} height={18} rx={2} />
                <text ref={chipTextRef} y={9} textAnchor="middle" dominantBaseline="central" />
              </g>
            </g>
          )}

          {/* hover layer */}
          {crossX != null && crossX >= 0 && crossX <= plot.plotW && (
            <g className="tl-hover" aria-hidden>
              <line className="crosshair" x1={Math.round(crossX) + 0.5} x2={Math.round(crossX) + 0.5} y1={0} y2={plot.plotH} />
              {readout?.avg && (
                <circle className="tl-focus-pt" cx={plot.sx(readout.avg.t)} cy={plot.cy(readout.avg.v)} r={4.5} style={{ fill: "var(--c-crowd)" }} />
              )}
              {readout?.odds && (
                <circle className="tl-focus-pt" cx={plot.sx(readout.odds.t)} cy={plot.oy(readout.odds.v)} r={4.5} style={{ fill: "var(--c-odds)" }} />
              )}
            </g>
          )}
          {/* keyed like its dot, so a replay reveals the ring with the bet (data-on, see applyReplay) */}
          {pinnedDot && <circle className="tl-ring pinned" data-i={pinnedDot.i} cx={pinnedDot.x} cy={pinnedDot.y} r={pinnedDot.r + 4} />}
          {activeDot && activeDot.i !== pinned && <circle className="tl-ring" cx={activeDot.x} cy={activeDot.y} r={activeDot.r + 3.5} />}

          {/* axes */}
          <g ref={xAxisRef} className="axis tl-x-axis" transform={`translate(0,${plot.plotH})`} />
          <g ref={leftAxisRef} className="axis" />
          {view === "cmp" && <g ref={whaleRightRef} className="axis" transform={`translate(${whaleAxisX},0)`} />}
          {showOdds && <g ref={oddsAxisRef} className="axis" transform={`translate(${oddsAxisX},0)`} />}

          <AxisTitle x={-mL + (mobile ? 10 : 12)} y={plot.plotH / 2} font={titleFont} {...leftTitle} />
          {view === "cmp" && (
            <AxisTitle x={whaleAxisX + rW - (mobile ? 10 : 12)} y={plot.plotH / 2} font={titleFont} text="Whale position" shape="dot" color="var(--c-whale)" />
          )}
          {showOdds && (
            <AxisTitle x={oddsAxisX + rOdds - (mobile ? 10 : 12)} y={plot.plotH / 2} font={titleFont} text="Odds (%)" shape="line" color="var(--c-odds)" />
          )}

          {/* event labels: plain small text on a surface knockout */}
          <g className="tl-events-labels" aria-hidden>
            {chips.map((c) => {
              const y = c.row === 0 ? -mT + 16 : 4 + c.row * 20 + 8;
              return (
                <text key={c.e.t} data-t={c.e.t} className="tl-event-label" x={c.cx} y={y} textAnchor="middle" dominantBaseline="central">
                  {c.e.label}
                </text>
              );
            })}
          </g>

          {/* time pill on the x axis */}
          {active && crossX != null && crossX >= 0 && crossX <= plot.plotW && <TimePill x={crossX} y={plot.plotH} t={active.t} plotW={plot.plotW} />}

          <g transform={`translate(${plot.pad},0)`}>
            <rect
              ref={overlayRef}
              className="tl-overlay"
              x={-plot.pad}
              width={plot.plotW}
              height={plot.plotH}
              onPointerMove={onPointerMove}
              onPointerLeave={onPointerLeave}
              onPointerCancel={onPointerLeave}
              onClick={onClick}
              onDoubleClick={() => onReset()}
            />
          </g>
        </g>
      </svg>

      <Overview tl={tl} full={full} domain={domain} left={mL} width={plot.plotW} showOdds={showOdds} onDomain={onDomain} />

      <Tooltip
        x={tipDot ? mL + tipDot.x : mL + (tip?.px ?? 0)}
        y={tipDot ? mT + tipDot.y - tipDot.r : mT + (tip?.py ?? 0)}
        visible={!!active && !dragging && (activeDot ? activeDot.i !== pinned : hasRows)}
        containerWidth={width}
        containerHeight={height}
      >
        {tipWhale ? (
          <WhaleTip w={tipWhale} hint={kbDot && !hover ? "Enter to pin" : "click to pin"} />
        ) : tip ? (
          <>
            <div className="tip-title">{fmtT(tip.t)}</div>
            {readout?.odds && <TipRow color="var(--c-odds)" value={fmtPct(readout.odds.v)} label="Odds" />}
            {readout?.avg && <TipRow color="var(--c-crowd)" value={`${fmtUSD(readout.avg.v)}/hr`} label="Crowd, 6-hr avg" />}
            {readout?.raw && (
              <TipRow
                color="color-mix(in oklab, var(--c-crowd) 35%, transparent)"
                shape="rect"
                value={fmtUSD(readout.raw.v)}
                label={`Crowd, ${d3.utcFormat("%H:%M")(toDate(readout.raw.hour))} hour`}
              />
            )}
          </>
        ) : null}
      </Tooltip>

      {pinnedWhale && (
        <PinnedWhale
          ref={pinRef}
          w={pinnedWhale}
          docked={docked}
          onClose={() => onPin(null)}
          style={docked ? undefined : { left: mL + pinPlace.rect.x0, top: mT + pinPlace.rect.y0 }}
        />
      )}
    </div>
  );
}

function AxisTitle({
  x,
  y,
  text,
  shape,
  color,
  font,
}: {
  x: number;
  y: number;
  text: string;
  shape: "dot" | "line";
  color: string;
  font: string;
}) {
  const tw = textWidth(text, font);
  const keyW = 14;
  const gap = 6;
  const start = -(keyW + gap + tw) / 2;
  return (
    <g className="tl-axis-title" transform={`translate(${x},${y}) rotate(-90)`} aria-hidden>
      {shape === "dot" ? (
        <circle cx={start + keyW / 2} cy={0} r={4.5} style={{ fill: color }} />
      ) : (
        <line x1={start + 1} x2={start + keyW - 1} y1={0} y2={0} strokeWidth={2.5} strokeLinecap="round" style={{ stroke: color }} />
      )}
      <text className="axis-title" x={start + keyW + gap} y={0} dominantBaseline="central" style={{ font }}>
        {text}
      </text>
    </g>
  );
}

function TimePill({ x, y, t, plotW }: { x: number; y: number; t: number; plotW: number }) {
  const label = fmtT(t);
  const w = textWidth(label, TIME_FONT) + 14;
  const cx = Math.min(Math.max(x, w / 2), plotW - w / 2);
  return (
    <g className="tl-pill" transform={`translate(${cx - w / 2},${y + 4})`} aria-hidden>
      <rect width={w} height={18} rx={2} />
      <text x={w / 2} y={9} textAnchor="middle" dominantBaseline="central">
        {label}
      </text>
    </g>
  );
}
