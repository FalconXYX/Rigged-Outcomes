import * as d3 from "d3";
import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties, type KeyboardEvent, type MouseEvent, type PointerEvent } from "react";
import type { Trader, TraderCategory, TradersData } from "../../types";
import { Tooltip } from "../../components/ui";
import { prefersReducedMotion, useSize } from "../../lib/useSize";
import { CATEGORY_ORDER, markerDiameter, ringRadius, type CategoryName, type Focus, type Pin } from "./model";
import { CAT_COLOR, font, textWidth, useFontVersion } from "./paint";
import { PinCard, WalletTipBody } from "./TraderCard";

/** Data domain on both axes, like the Dash figure's range=[-130, 130]. */
const MAX_SIDE = 640;
const HIT = 24;
const MAX_ZOOM = 8;
const RINGS = [0, 25, 50, 75, 100];

interface Dot {
  /** Row index into data.traders (the identity — one wallet can have several rows). */
  row: number;
  tr: Trader;
  /** Base (unzoomed) screen position. */
  bx: number;
  by: number;
  /** Base marker radius. */
  r0: number;
}

interface Props {
  data: TradersData;
  names: Record<TraderCategory, CategoryName>;
  hidden: ReadonlySet<TraderCategory>;
  focus: Focus;
  onFocus: (f: Focus) => void;
  onLeave: () => void;
  pin: Pin | null;
  onPin: (p: Pin | null) => void;
  restRows: Map<TraderCategory, Set<number>>;
  /** Rows per wallet address, for the "other positions" note. */
  walletRows: Map<string, number[]>;
  totalVolume: number;
  /** Tallest the chart may be (px); it stays square up to this. */
  maxHeight?: number;
}

const clamp = (v: number, lo: number, hi: number) => Math.max(lo, Math.min(hi, v));

/** Pointy-top hexagon (Plotly "hexagon") of circumradius r. */
function hexPath(r: number) {
  const h = (r * Math.sqrt(3)) / 2;
  return `M${h},${-r / 2}V${r / 2}L0,${r}L${-h},${r / 2}V${-r / 2}L0,${-r}Z`;
}

interface Annotation {
  key: string;
  head: [number, number];
  /**
   * Tail offsets from the head in screen px (Plotly ax / ay). The first is the
   * Dash figure's; the others are fallbacks tried when it would cover dots.
   */
  offs: [number, number][];
  title: string;
  sub: string;
  color: string;
  width: number;
}

const ANNOTATIONS: Annotation[] = [
  {
    key: "core",
    head: [15, 15],
    offs: [
      [40, 40],
      [64, 14],
      [58, -30],
      [22, 64],
      [76, 52],
    ],
    title: "Core threat cluster",
    sub: "High concentration, zero history",
    color: "var(--c-critical)",
    width: 2,
  },
  {
    key: "orbit",
    head: [-80, -80],
    offs: [
      [-40, -40],
      [-60, -10],
      [-14, -60],
      [-62, 22],
      [34, 40],
    ],
    title: "Normal orbit",
    sub: "Established traders",
    color: "var(--c-normal)",
    width: 1.5,
  },
];

/** Ring-label angles tried (degrees clockwise from 12 o'clock); all four labels share one, like a radial axis. */
const RING_ANGLES = [0, 15, -15, 30, -30, 45, -45, 60, -60, 75, -75, 90, -90];

interface Box {
  x0: number;
  y0: number;
  x1: number;
  y1: number;
}

const overlapArea = (a: Box, b: Box) =>
  Math.max(0, Math.min(a.x1, b.x1) - Math.max(a.x0, b.x0)) * Math.max(0, Math.min(a.y1, b.y1) - Math.max(a.y0, b.y0));

/** How much of the dots a box would cover (≈ px², full weight when the center is inside). */
function dotCover(b: Box, dots: readonly { x: number; y: number; r: number }[]) {
  let s = 0;
  for (const d of dots) {
    const r = d.r + 1.5;
    if (d.x + r < b.x0 || d.x - r > b.x1 || d.y + r < b.y0 || d.y - r > b.y1) continue;
    const dist = Math.hypot(d.x - clamp(d.x, b.x0, b.x1), d.y - clamp(d.y, b.y0, b.y1));
    if (dist < r) s += Math.PI * r * r * (dist === 0 ? 1 : 1 - dist / r);
  }
  return s;
}

/** Does segment p→q cross box b? (Liang–Barsky clip.) */
function segmentHits(px: number, py: number, qx: number, qy: number, b: Box) {
  let t0 = 0;
  let t1 = 1;
  const dx = qx - px;
  const dy = qy - py;
  const edges: [number, number][] = [
    [-dx, px - b.x0],
    [dx, b.x1 - px],
    [-dy, py - b.y0],
    [dy, b.y1 - py],
  ];
  for (const [p, q] of edges) {
    if (p === 0) {
      if (q < 0) return false;
    } else {
      const r = q / p;
      if (p < 0) t0 = Math.max(t0, r);
      else t1 = Math.min(t1, r);
      if (t0 > t1) return false;
    }
  }
  return true;
}

/** Annotation box hung off the tail on the side away from the head, clamped inside the chart. */
function annotBox(hx: number, hy: number, off: [number, number], w: number, h: number, W: number, H: number): Box {
  const [ox, oy] = off;
  const tx = hx + ox;
  const ty = hy + oy;
  let left: number;
  let top: number;
  if (Math.abs(ox) < 0.45 * Math.abs(oy)) {
    left = tx - w / 2;
    top = oy >= 0 ? ty - 4 : ty + 4 - h;
  } else if (Math.abs(oy) < 0.45 * Math.abs(ox)) {
    left = ox >= 0 ? tx - 6 : tx + 6 - w;
    top = ty - h / 2;
  } else {
    left = ox >= 0 ? tx - 10 : tx + 10 - w;
    top = oy >= 0 ? ty - 8 : ty + 8 - h;
  }
  left = clamp(left, 4, W - w - 4);
  top = clamp(top, 4, H - h - 4);
  return { x0: left, y0: top, x1: left + w, y1: top + h };
}

interface Arrow {
  x1: number;
  y1: number;
  x2: number;
  y2: number;
  head: string;
}

/** Arrow from the box edge (toward the head) to the head; null when the box sits on the head. */
function annotArrow(b: Box, hx: number, hy: number, width: number): Arrow | null {
  const w = b.x1 - b.x0;
  const h = b.y1 - b.y0;
  const bcx = b.x0 + w / 2;
  const bcy = b.y0 + h / 2;
  const dx = hx - bcx;
  const dy = hy - bcy;
  const s = Math.min(Math.abs(dx) > 1e-6 ? (w / 2 + 3) / Math.abs(dx) : Infinity, Math.abs(dy) > 1e-6 ? (h / 2 + 3) / Math.abs(dy) : Infinity);
  if (!(s < 1)) return null;
  const x1 = bcx + dx * s;
  const y1 = bcy + dy * s;
  const len = Math.hypot(hx - x1, hy - y1);
  if (len <= 10) return null;
  const ux = (hx - x1) / len;
  const uy = (hy - y1) / len;
  const al = 4 + width * 2.2;
  const aw = 2 + width * 1.2;
  const bx = hx - ux * al;
  const by = hy - uy * al;
  return { x1, y1, x2: bx + ux, y2: by + uy, head: `M${hx},${hy}L${bx - uy * aw},${by + ux * aw}L${bx + uy * aw},${by - ux * aw}Z` };
}

const MARKET_FS = 11.5;
const RING_FS = 10;

/** Box of the "THE MARKET" label under a hexagon centred at (x, y). */
const marketBox = (x: number, y: number, hexR: number, labelW: number): Box => ({
  x0: x - labelW / 2 - 3,
  y0: y + hexR + 4,
  x1: x + labelW / 2 + 3,
  y1: y + hexR + 8 + MARKET_FS,
});

export function Constellation({
  data,
  names,
  hidden,
  focus,
  onFocus,
  onLeave,
  pin,
  onPin,
  restRows,
  walletRows,
  totalVolume,
  maxHeight = MAX_SIDE,
}: Props) {
  const [wrapRef, { width: W }] = useSize<HTMLDivElement>();
  const svgRef = useRef<SVGSVGElement>(null);
  const zoomRef = useRef<d3.ZoomBehavior<SVGSVGElement, unknown> | null>(null);
  const prevSize = useRef<{ W: number; H: number } | null>(null);
  const lastIdx = useRef(0);
  const kbIdx = useRef(-1);
  const [t, setT] = useState<d3.ZoomTransform>(d3.zoomIdentity);
  const [dragging, setDragging] = useState(false);
  const [svgFocused, setSvgFocused] = useState(false);
  const fontVersion = useFontVersion();

  const side = Math.min(W, maxHeight);
  const H = side;
  const narrow = W < 480;
  const margin = narrow ? 6 : 10;
  const cx = W / 2;
  const cy = H / 2;
  // Dash's 8 + 40·√(pos/max) on a chart of ~580px; smaller charts shrink the 40 (down to 24), larger ones keep it.
  const spread = 40 * clamp(side / 580, 0.6, 1);
  const hexR = narrow ? 14 : 17.5;

  const maxPos = useMemo(() => d3.max(data.traders, (d) => d.position) ?? 1, [data]);

  // Pixels per data unit: the largest scale that keeps the risk-0 ring and every
  // dot (centre plus marker radius and stroke) inside the square, so the
  // constellation fills the box instead of a fixed ±130 domain.
  const ppu = useMemo(() => {
    const room = side / 2 - margin;
    let k = room / ringRadius(0);
    for (const tr of data.traders) {
      const dist = Math.hypot(tr.x, tr.y);
      if (dist > 0) k = Math.min(k, (room - markerDiameter(tr.position, maxPos, spread) / 2 - 1) / dist);
    }
    return Math.max(0, k);
  }, [data, side, margin, maxPos, spread]);

  /** All dots, in Dash draw order (normal → new → concentration → critical); big under small within a category. */
  const dots: Dot[] = useMemo(() => {
    const rank = new Map(CATEGORY_ORDER.map((c, i) => [c, i]));
    return data.traders
      .map((tr, row) => ({ row, tr, bx: cx + tr.x * ppu, by: cy - tr.y * ppu, r0: markerDiameter(tr.position, maxPos, spread) / 2 }))
      .sort((a, b) => rank.get(a.tr.category)! - rank.get(b.tr.category)! || b.tr.position - a.tr.position);
  }, [data, cx, cy, ppu, maxPos, spread]);

  const byCat = useMemo(() => {
    const m = new Map<TraderCategory, Dot[]>();
    for (const c of CATEGORY_ORDER) m.set(c, []);
    for (const d of dots) m.get(d.tr.category)!.push(d);
    return m;
  }, [dots]);

  const byRow = useMemo(() => new Map(dots.map((d) => [d.row, d])), [dots]);

  const visible = useMemo(() => dots.filter((d) => !hidden.has(d.tr.category)), [dots, hidden]);
  /** Keyboard order: largest positions first. */
  const kbOrder = useMemo(() => [...visible].sort((a, b) => b.tr.position - a.tr.position), [visible]);

  const rScale = t.k ** 0.3;
  const sx = useCallback((d: Dot) => t.x + t.k * d.bx, [t]);
  const sy = useCallback((d: Dot) => t.y + t.k * d.by, [t]);

  const delaunay = useMemo(
    () => d3.Delaunay.from(visible, (d) => t.x + t.k * d.bx, (d) => t.y + t.k * d.by),
    [visible, t],
  );

  /* ── d3.zoom ─────────────────────────────────────────────────────── */

  useEffect(() => {
    const svg = svgRef.current;
    if (!svg || W <= 0) return;
    const sel = d3.select(svg);
    const zoom = d3
      .zoom<SVGSVGElement, unknown>()
      .scaleExtent([1, MAX_ZOOM])
      .extent([
        [0, 0],
        [W, H],
      ])
      .translateExtent([
        [0, 0],
        [W, H],
      ])
      .filter((event: Event) => {
        // A plain wheel scrolls the page; ⌘/Ctrl + wheel (and trackpad pinch, which arrives as ctrl + wheel) zooms.
        if (event.type === "wheel") {
          const we = event as globalThis.WheelEvent;
          return we.ctrlKey || we.metaKey;
        }
        // One finger always scrolls the page (touch-action: pan-y); two fingers pinch and pan.
        if (event.type === "touchstart") return (event as TouchEvent).touches.length >= 2;
        const me = event as globalThis.MouseEvent;
        return !me.ctrlKey && !me.button;
      })
      .wheelDelta((event: globalThis.WheelEvent) => {
        const unit = event.deltaMode === 1 ? 0.05 : event.deltaMode ? 1 : 0.002;
        // Pinch sends small ctrl + wheel deltas and needs d3's 10× boost; a ⌘/Ctrl mouse notch (~100px) does not.
        const pinch = event.ctrlKey && event.deltaMode === 0 && Math.abs(event.deltaY) < 40;
        return clamp(-event.deltaY * unit * (pinch ? 10 : 1), -0.6, 0.6);
      })
      .on("start", (e: d3.D3ZoomEvent<SVGSVGElement, unknown>) => {
        const type = (e.sourceEvent as Event | null)?.type;
        if (type === "mousedown" || type === "touchstart") setDragging(true);
      })
      .on("zoom", (e: d3.D3ZoomEvent<SVGSVGElement, unknown>) => {
        setT(e.transform);
      })
      .on("end", () => setDragging(false));
    sel.call(zoom).on("dblclick.zoom", null);
    // A resize keeps the current zoom: rescale the pan with the chart, then re-constrain.
    const prev = prevSize.current;
    const cur = d3.zoomTransform(svg);
    prevSize.current = { W, H };
    if (prev && prev.W > 0 && prev.H > 0 && cur.k > 1.001) {
      zoom.transform(sel, d3.zoomIdentity.translate((cur.x * W) / prev.W, (cur.y * H) / prev.H).scale(cur.k));
      zoom.translateBy(sel, 0, 0);
    } else {
      zoom.transform(sel, d3.zoomIdentity);
    }
    zoomRef.current = zoom;
    return () => {
      sel.on(".zoom", null);
    };
  }, [W, H]);

  const zoomBy = useCallback((factor: number | null) => {
    const svg = svgRef.current;
    const zoom = zoomRef.current;
    if (!svg || !zoom) return;
    const sel = d3.select(svg);
    const tr = sel.transition().duration(prefersReducedMotion() ? 0 : 420).ease(d3.easeCubicOut);
    if (factor == null) tr.call(zoom.transform, d3.zoomIdentity);
    else tr.call(zoom.scaleBy, factor);
  }, []);

  /* ── Hit testing ─────────────────────────────────────────────────── */

  const pick = useCallback(
    (clientX: number, clientY: number): Dot | null => {
      const svg = svgRef.current;
      if (!svg || visible.length === 0) return null;
      const rect = svg.getBoundingClientRect();
      const px = clientX - rect.left;
      const py = clientY - rect.top;
      const i = delaunay.find(px, py, Math.min(lastIdx.current, visible.length - 1));
      lastIdx.current = i;
      const d = visible[i];
      if (!d) return null;
      const dist = Math.hypot(sx(d) - px, sy(d) - py);
      return dist <= Math.max(HIT, d.r0 * rScale + 4) ? d : null;
    },
    [delaunay, visible, sx, sy, rScale],
  );

  const onPointerMove = (e: PointerEvent<SVGSVGElement>) => {
    if (e.pointerType === "touch" || dragging) return;
    const d = pick(e.clientX, e.clientY);
    if (d) onFocus({ kind: "wallet", row: d.row, source: "constellation" });
    else onLeave();
  };

  const onClick = (e: MouseEvent<SVGSVGElement>) => {
    const d = pick(e.clientX, e.clientY);
    if (d) onPin(pin?.row === d.row ? null : { row: d.row, source: "constellation" });
    else onPin(null);
  };

  const onKeyDown = (e: KeyboardEvent<SVGSVGElement>) => {
    if (e.key === "+" || e.key === "=") return void zoomBy(1.6);
    if (e.key === "-" || e.key === "_") return void zoomBy(1 / 1.6);
    if (e.key === "0") return void zoomBy(null);
    if (!kbOrder.length) return;
    if (e.key === "ArrowRight" || e.key === "ArrowDown" || e.key === "ArrowLeft" || e.key === "ArrowUp") {
      e.preventDefault();
      const step = e.key === "ArrowRight" || e.key === "ArrowDown" ? 1 : -1;
      kbIdx.current = (kbIdx.current + step + kbOrder.length) % kbOrder.length;
      onFocus({ kind: "wallet", row: kbOrder[kbIdx.current].row, source: "constellation" });
    } else if ((e.key === "Enter" || e.key === " ") && focus?.kind === "wallet" && focus.source === "constellation") {
      e.preventDefault();
      onPin({ row: focus.row, source: "constellation" });
    }
  };

  /* ── Highlight state ─────────────────────────────────────────────── */

  const isOn = useMemo<((d: Dot) => boolean) | null>(() => {
    if (!focus) return null;
    if (focus.kind === "wallet") {
      const row = focus.row;
      return (d) => d.row === row;
    }
    const cat = focus.category;
    if (focus.kind === "category") return (d) => d.tr.category === cat;
    const rows = restRows.get(cat);
    return (d) => d.tr.category === cat && !!rows?.has(d.row);
  }, [focus, restRows]);

  const focusDot = focus?.kind === "wallet" ? byRow.get(focus.row) : undefined;
  const pinDot = pin ? byRow.get(pin.row) : undefined;
  const showTip = !!focusDot && focus?.source === "constellation" && !dragging && !hidden.has(focusDot.tr.category);

  /* ── Static layers in base coordinates (zoomed with one transform) ─ */

  // Each category's spokes come in two groups — its top-15 wallets and the
  // wallets folded into the treemap's "N more" tile — so hovering that tile
  // lights exactly the spokes of the dots it lights.
  const spokes = useMemo(
    () =>
      CATEGORY_ORDER.map((c) => {
        const rest = restRows.get(c);
        const line = (d: Dot) => <line key={d.row} x1={cx} y1={cy} x2={d.bx} y2={d.by} />;
        const all = byCat.get(c)!;
        return {
          c,
          top: all.filter((d) => !rest?.has(d.row)).map(line),
          rest: rest ? all.filter((d) => rest.has(d.row)).map(line) : [],
        };
      }),
    [byCat, restRows, cx, cy],
  );

  const spokeClass = (c: TraderCategory, part: "top" | "rest") => {
    if (hidden.has(c)) return "off";
    if (!focus) return "";
    if (focus.kind === "wallet") return "dim";
    if (focus.kind === "rest") return focus.category === c && part === "rest" ? "on" : "dim";
    return focus.category === c ? "on" : "dim";
  };

  const zoomed = t.k > 1.001;
  const gT = `translate(${t.x},${t.y}) scale(${t.k})`;
  const csx = t.applyX(cx);
  const csy = t.applyY(cy);

  /* ── Label placement (base geometry, so nothing jumps while zooming) ── */

  const annotSize = useMemo(() => {
    void fontVersion;
    // Narrow charts get a one-line callout (title only) so the box covers less data.
    const compact = W < 420;
    const fs1 = narrow ? 11 : 12.5;
    const fs2 = narrow ? 10.5 : 11.5;
    const padX = compact ? 6 : 8;
    const padY = compact ? 4 : 6;
    const lh1 = fs1 * 1.3;
    const lh2 = compact ? 0 : fs2 * 1.35;
    const h = Math.ceil(padY * 2 + lh1 + lh2);
    const w = ANNOTATIONS.map(
      (a) => Math.ceil(Math.max(textWidth(a.title, font(700, fs1)), compact ? 0 : textWidth(a.sub, font(500, fs2)))) + padX * 2 + 2,
    );
    return { compact, fs1, fs2, padX, padY, lh1, h, w };
  }, [W, narrow, fontVersion]);

  /** "THE MARKET" sits under the hexagon: above it, the core-cluster arrowhead (15, 15) lands on the text. */
  const marketLabelW = useMemo(() => {
    void fontVersion;
    return textWidth("THE MARKET", font(700, MARKET_FS)) + 10 * 0.08 * MARKET_FS;
  }, [fontVersion]);

  const placement = useMemo(() => {
    if (W <= 0) return null;
    const pts = visible.map((d) => ({ x: d.bx, y: d.by, r: d.r0 }));
    const label = marketBox(cx, cy, hexR, marketLabelW);
    const hex: Box = { x0: cx - hexR, y0: cy - hexR, x1: cx + hexR, y1: cy + hexR };
    // Where the Reset button sits once zoomed.
    const controls: Box = { x0: W - 104, y0: 0, x1: W, y1: 40 };
    const placed: Box[] = [];
    const offIdx: number[] = [];
    ANNOTATIONS.forEach((a, ai) => {
      const hx = cx + a.head[0] * ppu;
      const hy = cy - a.head[1] * ppu;
      let best = 0;
      let bestScore = Infinity;
      a.offs.forEach((off, i) => {
        const b = annotBox(hx, hy, off, annotSize.w[ai], annotSize.h, W, H);
        const ar = annotArrow(b, hx, hy, a.width);
        let score = i * 30 + dotCover(b, pts);
        for (const o of [label, hex, controls, ...placed]) score += 40 * overlapArea(b, o);
        if (!ar) score += 1e5;
        else if (segmentHits(ar.x1, ar.y1, hx, hy, label)) score += 1e4;
        if (score < bestScore) {
          bestScore = score;
          best = i;
        }
      });
      offIdx.push(best);
      placed.push(annotBox(hx, hy, a.offs[best], annotSize.w[ai], annotSize.h, W, H));
    });
    // One angle for all ring labels, clear of dots and callouts where possible.
    const labels = RINGS.slice(0, 4).map((risk) => ({
      R: ringRadius(risk) * ppu,
      w: textWidth(risk === 0 ? "Risk 0" : String(risk), font(600, RING_FS)) + 8,
    }));
    let angle = 0;
    let bestScore = Infinity;
    for (const deg of RING_ANGLES) {
      const th = (deg * Math.PI) / 180;
      let score = Math.abs(deg) * 0.6;
      for (const l of labels) {
        const x = cx + l.R * Math.sin(th);
        const y = cy - l.R * Math.cos(th);
        const b: Box = { x0: x - l.w / 2, y0: y - 7, x1: x + l.w / 2, y1: y + 7 };
        if (b.x0 < 2 || b.x1 > W - 2 || b.y0 < 2 || b.y1 > H - 2) score += 1e4;
        score += dotCover(b, pts);
        for (const o of [label, hex, controls, ...placed]) score += 40 * overlapArea(b, o);
      }
      if (score < bestScore) {
        bestScore = score;
        angle = th;
      }
    }
    return { offIdx, angle };
  }, [W, H, cx, cy, ppu, hexR, visible, annotSize, marketLabelW]);

  /* ── Annotations (screen space, clamped inside the chart) ───────── */

  const annots = useMemo(() => {
    if (W <= 0 || !placement) return [];
    return ANNOTATIONS.flatMap((a, ai) => {
      const hx = t.applyX(cx + a.head[0] * ppu);
      const hy = t.applyY(cy - a.head[1] * ppu);
      if (hx < 4 || hx > W - 4 || hy < 4 || hy > H - 4) return [];
      const w = annotSize.w[ai];
      const b = annotBox(hx, hy, a.offs[placement.offIdx[ai]], w, annotSize.h, W, H);
      return [{ ...a, ...annotSize, left: b.x0, top: b.y0, w, arrow: annotArrow(b, hx, hy, a.width) }];
    });
  }, [W, H, t, cx, cy, ppu, placement, annotSize]);

  /* ── Render ─────────────────────────────────────────────────────── */

  const docked = W > 0 && W < 520;
  const pinTrader = pinDot?.tr;
  const anyHover = !!focusDot && !dragging;
  const cursor = anyHover ? "pointer" : zoomed ? (dragging ? "grabbing" : "grab") : "default";

  const dotEl = (d: Dot, cls: string) => {
    const r = d.r0 * rScale;
    const x = sx(d);
    const y = sy(d);
    return (
      <g key={d.row} className={cls}>
        <circle className="dot" cx={x} cy={y} r={r} />
        {d.tr.category === "critical" && <circle className="dot-core" cx={x} cy={y} r={Math.max(1.5, r * 0.32)} />}
      </g>
    );
  };

  const ringAngle = placement?.angle ?? 0;
  const ringLabels = RINGS.slice(0, 4).map((risk) => {
    const R = ringRadius(risk) * ppu * t.k;
    const x = csx + R * Math.sin(ringAngle);
    const y = csy - R * Math.cos(ringAngle);
    if (y < 8 || y > H - 8 || x < 20 || x > W - 20) return null;
    return (
      <text key={risk} className="ring-label" x={x} y={y} dy="0.35em" textAnchor="middle">
        {risk === 0 ? "Risk 0" : risk}
      </text>
    );
  });

  return (
    <div className="tr-constellation">
      <div ref={wrapRef} className="chart tr-chart" style={{ height: H || undefined }}>
        {W > 0 && (
          <>
            <svg
              ref={svgRef}
              width={W}
              height={H}
              className="tr-svg"
              role="img"
              aria-label={`Threat constellation of ${data.traders.length} whale wallets. Arrow keys step through wallets, Enter pins one, plus and minus zoom.`}
              tabIndex={0}
              style={{ cursor, touchAction: "pan-y" }}
              onPointerMove={onPointerMove}
              onPointerLeave={() => !dragging && onLeave()}
              onClick={onClick}
              onKeyDown={onKeyDown}
              onFocus={(e) => setSvgFocused(e.currentTarget.matches(":focus-visible"))}
              onBlur={() => {
                setSvgFocused(false);
                if (focus?.source === "constellation") onLeave();
              }}
            >
              <rect width={W} height={H} className="tr-bg" />

              <g transform={gT}>
                <g className="rings">
                  {RINGS.map((risk) => (
                    <circle key={risk} cx={cx} cy={cy} r={ringRadius(risk) * ppu} />
                  ))}
                </g>
                <g className={`spoke-layer${focus ? " has-focus" : ""}`}>
                  {spokes.map(({ c, top, rest }) => (
                    <g key={c} data-cat={c} className="spokes" style={{ stroke: CAT_COLOR[c] }}>
                      <g className={`spoke-cat ${spokeClass(c, "top")}`}>{top}</g>
                      {rest.length > 0 && <g className={`spoke-cat ${spokeClass(c, "rest")}`}>{rest}</g>}
                    </g>
                  ))}
                </g>
              </g>

              {/* Highlighted spokes for the hovered / pinned wallet */}
              {[focusDot, pinDot !== focusDot ? pinDot : undefined].map(
                (d) =>
                  d &&
                  !hidden.has(d.tr.category) && (
                    <line
                      key={`hl-${d.row}`}
                      className="spoke-hl"
                      x1={csx}
                      y1={csy}
                      x2={sx(d)}
                      y2={sy(d)}
                      style={{ stroke: CAT_COLOR[d.tr.category] }}
                    />
                  ),
              )}

              <g className="dots-layer">
                {CATEGORY_ORDER.map((c) => (
                  <g key={c} className={`dot-cat${hidden.has(c) ? " off" : ""}`} style={{ fill: CAT_COLOR[c] }}>
                    {byCat.get(c)!.map((d) => dotEl(d, isOn && !isOn(d) ? "dim" : ""))}
                  </g>
                ))}
              </g>

              <g className="ring-labels">{ringLabels}</g>

              {/* THE MARKET */}
              <g className="market" transform={`translate(${csx},${csy})`}>
                <path d={hexPath(hexR)} className="market-hex" />
                <text className="market-label" y={hexR + 6} dy="0.86em" textAnchor="middle" style={{ fontSize: MARKET_FS }}>
                  THE MARKET
                </text>
              </g>

              {/* Annotations */}
              <g className="annots" aria-hidden>
                {annots.map((a) => (
                  <g key={a.key} style={{ "--key": a.color } as CSSProperties}>
                    {a.arrow && (
                      <>
                        <line
                          x1={a.arrow.x1}
                          y1={a.arrow.y1}
                          x2={a.arrow.x2}
                          y2={a.arrow.y2}
                          className="annot-line"
                          style={{ stroke: a.color, strokeWidth: a.width }}
                        />
                        <path d={a.arrow.head} style={{ fill: a.color }} />
                      </>
                    )}
                    <rect x={a.left} y={a.top} width={a.w} height={a.h} rx={3} className="annot-box" />
                    <text x={a.left + a.padX} y={a.top + a.padY + a.fs1 * 0.98} className="annot-title" style={{ fontSize: a.fs1 }}>
                      {a.title}
                    </text>
                    {!a.compact && (
                      <text
                        x={a.left + a.padX}
                        y={a.top + a.padY + a.lh1 + a.fs2 * 0.98}
                        className="annot-sub"
                        style={{ fontSize: a.fs2 }}
                      >
                        {a.sub}
                      </text>
                    )}
                  </g>
                ))}
              </g>

              {/* Hover + pin overlays (on top) */}
              {[pinDot, focusDot !== pinDot ? focusDot : undefined].map((d) => {
                if (!d || hidden.has(d.tr.category)) return null;
                const r = d.r0 * rScale;
                const big = r * 1.18 + 2;
                const x = sx(d);
                const y = sy(d);
                const pinned = d === pinDot;
                return (
                  <g key={`ov-${d.row}`} className="dot-overlay" style={{ fill: CAT_COLOR[d.tr.category] }}>
                    <circle className={`focus-ring${pinned ? " pinned" : ""}`} cx={x} cy={y} r={big + 3.5} />
                    <circle className="dot" cx={x} cy={y} r={big} />
                    {d.tr.category === "critical" && <circle className="dot-core" cx={x} cy={y} r={Math.max(1.8, big * 0.32)} />}
                  </g>
                );
              })}
            </svg>

            {/* Wheel / pinch / keyboard do the zooming; the one control is the way back. */}
            {zoomed && (
              <div className="tr-zoom">
                <button type="button" className="btn btn-ghost btn-sm tr-reset" onClick={() => zoomBy(null)} aria-label={`Reset zoom (${t.k.toFixed(1)}×)`}>
                  Reset <span className="tabular">{t.k.toFixed(1)}×</span>
                </button>
              </div>
            )}

            {showTip && focusDot && (
              <Tooltip x={sx(focusDot)} y={sy(focusDot)} visible containerWidth={W} containerHeight={H}>
                <WalletTipBody
                  trader={focusDot.tr}
                  names={names}
                  siblings={(walletRows.get(focusDot.tr.id)?.length ?? 1) - 1}
                  footer={pin?.row === focusDot.row ? "Pinned · click again to unpin" : svgFocused ? "Enter to pin details" : "Click to pin details"}
                />
              </Tooltip>
            )}

            {pinTrader && pin?.source === "constellation" && !docked && !hidden.has(pinTrader.category) && (
              <PinCard
                trader={pinTrader}
                names={names}
                totalVolume={totalVolume}
                anchor={{ x: sx(pinDot!), y: sy(pinDot!) }}
                container={{ width: W, height: H }}
                onClose={() => onPin(null)}
              />
            )}
          </>
        )}
      </div>
      {pinTrader && pin?.source === "constellation" && docked && (
        <PinCard
          trader={pinTrader}
          names={names}
          totalVolume={totalVolume}
          docked
          onClose={() => onPin(null)}
        />
      )}
    </div>
  );
}
