import { useId, useLayoutEffect, useMemo, useRef, useState, type PointerEvent as RPointerEvent } from "react";
import * as d3 from "d3";
import type { TimelineData } from "../../types";
import { timeTickFormat, toDate } from "../../lib/format";
import { FONT_SANS } from "../../components/ui";
import { HOUR, areaPath, clampDomain, linePath, timeTicks, type Domain } from "./model";

export const OVERVIEW_H = 44;
const AXIS_H = 20;
/** Smallest move target for the window, px (a pointer/touch hit target). */
const MIN_GRAB = 24;
/** Movement before a press counts as a drag rather than a click, px. */
const SLOP = 3;

type Mode = "move" | "w" | "e" | "new";

/**
 * Context strip under the main chart: the full span (crowd avg + odds, faint)
 * with a window that mirrors and drives the main chart's x-domain. Drag the
 * window to pan, its edges to resize, elsewhere to draw a new window; a click
 * recentres the window there.
 *
 * Pointer events (not d3-brush) so the strip keeps `touch-action: pan-y`:
 * vertical swipes scroll the page, horizontal drags brush.
 */
export function Overview({
  tl,
  full,
  domain,
  left,
  width,
  showOdds,
  onDomain,
}: {
  tl: TimelineData;
  full: Domain;
  domain: Domain;
  left: number;
  width: number;
  showOdds: boolean;
  onDomain: (d: Domain) => void;
}) {
  const H = OVERVIEW_H;
  const clipId = "tl-ov-clip-" + useId().replace(/[^a-zA-Z0-9_-]/g, "");
  const fw = full[1] - full[0];
  const ox = useMemo(() => (t: number) => ((t - full[0]) / (full[1] - full[0])) * width, [full, width]);

  const paths = useMemo(() => {
    const h = tl.crowdHourly;
    const maxA = d3.max(h.avg) || 1;
    const cy = (v: number) => H - 3 - (v / maxA) * (H - 10);
    const oy = (v: number) => H - 3 - (v / 100) * (H - 10);
    return {
      crowdArea: areaPath(h.t, h.avg, full[0], full[1], ox, cy, width, H - 2, true),
      crowdLine: linePath(h.t, h.avg, full[0], full[1], ox, cy, width),
      odds: linePath(tl.odds.t, tl.odds.pct, full[0], full[1], ox, oy, width),
    };
  }, [tl, full, ox, width, H]);

  const axisRef = useRef<SVGGElement>(null);
  useLayoutEffect(() => {
    if (!axisRef.current) return;
    const s = d3.scaleUtc().domain([toDate(full[0]), toDate(full[1])]).range([0, width]);
    d3.select(axisRef.current)
      .call(
        d3
          .axisBottom(s)
          .tickValues(timeTicks(s, width, `500 10.5px ${FONT_SANS}`, 120))
          .tickSize(0)
          .tickPadding(6)
          .tickFormat((d) => timeTickFormat(d as Date)),
      )
      .call((g) => g.select(".domain").remove());
  }, [full, width]);

  /* ── window geometry ─────────────────────────────────────────────── */
  const x0 = Math.max(0, ox(domain[0]));
  const x1 = Math.min(width, ox(domain[1]));
  // Keep a very narrow window visible: draw it at least 8px wide, one grip.
  const narrow = x1 - x0 < 14;
  const wx0 = narrow ? Math.max(0, Math.min(width - 8, (x0 + x1) / 2 - 4)) : x0;
  const wx1 = narrow ? wx0 + 8 : x1;
  // Hit targets: resize handles straddle the edges; whatever is left between
  // them moves the window. A small window gets a move target of at least
  // MIN_GRAB px and its handles move outside it, so it can always be dragged.
  const cx = (wx0 + wx1) / 2;
  const roomy = wx1 - wx0 >= MIN_GRAB + 28;
  const hw = roomy ? 14 : 10;
  const move: [number, number] = roomy
    ? [wx0 + hw / 2, wx1 - hw / 2]
    : [cx - Math.max(MIN_GRAB, wx1 - wx0) / 2, cx + Math.max(MIN_GRAB, wx1 - wx0) / 2];
  const handleW: [number, number] = roomy ? [wx0 - hw / 2, wx0 + hw / 2] : [move[0] - hw, move[0]];
  const handleE: [number, number] = roomy ? [wx1 - hw / 2, wx1 + hw / 2] : [move[1], move[1] + hw];

  /* ── dragging ────────────────────────────────────────────────────── */
  const svgRef = useRef<SVGSVGElement>(null);
  const drag = useRef<{ mode: Mode; id: number; x0: number; dom: Domain; moved: boolean } | null>(null);
  const [active, setActive] = useState<Mode | null>(null);
  const localX = (e: RPointerEvent) => e.clientX - (svgRef.current?.getBoundingClientRect().left ?? 0);

  const down = (mode: Mode) => (e: RPointerEvent<SVGRectElement>) => {
    if (e.button !== 0 || drag.current) return;
    if (e.pointerType === "mouse") e.preventDefault(); // no text selection while dragging
    e.currentTarget.setPointerCapture(e.pointerId);
    drag.current = { mode, id: e.pointerId, x0: localX(e), dom: domain, moved: false };
    setActive(mode);
  };

  const moveTo = (e: RPointerEvent<SVGRectElement>) => {
    const d = drag.current;
    if (!d || e.pointerId !== d.id) return;
    const px = Math.min(Math.max(localX(e), 0), width);
    const dx = px - d.x0;
    if (!d.moved && Math.abs(dx) < SLOP) return;
    d.moved = true;
    const dt = (dx / width) * fw;
    const minSpan = Math.min(HOUR, fw);
    const toT = (x: number) => full[0] + (x / width) * fw;
    let next: Domain;
    if (d.mode === "move") next = [d.dom[0] + dt, d.dom[1] + dt];
    else if (d.mode === "w") next = [Math.max(full[0], Math.min(d.dom[0] + dt, d.dom[1] - minSpan)), d.dom[1]];
    else if (d.mode === "e") next = [d.dom[0], Math.min(full[1], Math.max(d.dom[1] + dt, d.dom[0] + minSpan))];
    else next = [toT(Math.min(d.x0, px)), toT(Math.max(d.x0, px))];
    onDomain(clampDomain(next, full));
  };

  const up = (e: RPointerEvent<SVGRectElement>) => {
    const d = drag.current;
    if (!d || e.pointerId !== d.id) return;
    drag.current = null;
    setActive(null);
    if (!d.moved && d.mode === "new") {
      // A click without a drag recentres the current window there.
      const c = full[0] + (d.x0 / width) * fw;
      const w = d.dom[1] - d.dom[0];
      onDomain(clampDomain([c - w / 2, c + w / 2], full));
    }
  };

  // The browser took the gesture over (a vertical scroll): drop it.
  const cancel = (e: RPointerEvent<SVGRectElement>) => {
    if (drag.current?.id !== e.pointerId) return;
    drag.current = null;
    setActive(null);
  };

  const hit = (mode: Mode, a: number, b: number, cls: string) => (
    <rect
      className={cls}
      x={a}
      y={0}
      width={Math.max(0, b - a)}
      height={H}
      onPointerDown={down(mode)}
      onPointerMove={moveTo}
      onPointerUp={up}
      onPointerCancel={cancel}
      onLostPointerCapture={cancel}
    />
  );

  const events = tl.events.filter((e) => e.t >= full[0] && e.t <= full[1]);

  return (
    <div className={`tl-ov${active ? ` is-${active === "new" ? "drawing" : active === "move" ? "moving" : "resizing"}` : ""}`} style={{ paddingLeft: left }}>
      <svg
        ref={svgRef}
        width={width}
        height={H + AXIS_H}
        role="group"
        aria-label="Overview of the full time span. Drag the window to change the visible range."
      >
        <defs>
          <clipPath id={clipId}>
            <rect x={0} y={0} width={Math.max(0, width)} height={H} />
          </clipPath>
        </defs>
        <rect className="tl-ov-bg" x={0.5} y={0.5} width={Math.max(0, width - 1)} height={H - 1} />
        {/* under the data, so the window reads as a pane in the selection wash, not a film over it */}
        <rect className="tl-ov-window-fill" x={wx0} y={0.75} width={Math.max(0, wx1 - wx0)} height={H - 1.5} />
        <g className="tl-ov-data" clipPath={`url(#${clipId})`}>
          <path className="tl-ov-crowd-area" d={paths.crowdArea} />
          <path className="tl-ov-crowd" d={paths.crowdLine} />
          {showOdds && <path className="tl-ov-odds" d={paths.odds} />}
          {events.map((e) => (
            <line key={e.t} className="tl-ov-event" x1={ox(e.t)} x2={ox(e.t)} y1={3} y2={H - 3} />
          ))}
        </g>
        <path
          className="tl-ov-dim"
          d={`M0,0H${wx0}V${H}H0Z M${wx1},0H${width}V${H}H${wx1}Z`}
          clipPath={`url(#${clipId})`}
        />
        <rect className="tl-ov-window" x={wx0} y={0.75} width={Math.max(0, wx1 - wx0)} height={H - 1.5} />
        <g className="tl-ov-grips" aria-hidden>
          {(narrow ? [cx] : [x0, x1]).map((gx, i) => (
            <rect key={i} x={gx - 3} y={H / 2 - 8} width={6} height={16} rx={1} />
          ))}
        </g>
        <g className="tl-ov-hit">
          {hit("new", 0, width, "tl-ov-overlay")}
          {hit("move", move[0], move[1], "tl-ov-move")}
          {hit("w", handleW[0], handleW[1], "tl-ov-handle")}
          {hit("e", handleE[0], handleE[1], "tl-ov-handle")}
        </g>
        <g ref={axisRef} className="axis tl-ov-axis" transform={`translate(0,${H})`} />
      </svg>
      <span className="visually-hidden">
        Visible window covers {Math.round(((domain[1] - domain[0]) / fw) * 100)}% of the full span.
      </span>
    </div>
  );
}
