import * as d3 from "d3";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type FocusEvent, type KeyboardEvent, type MouseEvent, type PointerEvent } from "react";
import type { Trader, TraderCategory, TradersData } from "../../types";
import { TipRow, Tooltip } from "../../components/ui";
import { fmtInt, fmtPct, fmtUSD, fmtUSDCompact, shortAddr } from "../../lib/format";
import { prefersReducedMotion, useSize } from "../../lib/useSize";
import { CATEGORY_ORDER, type CategoryName, type Focus, type Pin, type TreemapCategory, type TreemapLeaf, type TreemapNode, type TreemapRoot, type TreemapWallet } from "./model";
import { CAT_COLOR, catTint, font, headerMix, inkOn, restMix, subInkOn, textWidth, useFontVersion, usePalette } from "./paint";
import { CategoryKey, PinCard } from "./TraderCard";

const HEADER = 20; // category header band height
const GAP = 2; // surface gap between tiles
const TILE_RX = 2; // tile corners: the system's 2px, not pills
const DUR = 520;
/** Least wallet-area depth a header band may leave its category (below this the band moves aside or goes). */
const MIN_LEAF = 16;
/** Header key dot diameter, and the gap between it and the header text. */
const DOT = 8;
const DOT_GAP = 5;
const HDR_PAD = 7;

interface Rect {
  x0: number;
  y0: number;
  x1: number;
  y1: number;
}

/**
 * Where a category's header band sits. "top" is the usual band over the
 * wallets; a block too short for that (a full-width strip of a small
 * category) gets a "side" band at its left instead; a block too small for
 * either shows only its wallets ("none"), named by the key and the tooltip.
 */
type HeaderMode = { mode: "top" } | { mode: "side"; width: number } | { mode: "none" };

interface Placed {
  key: string;
  kind: "header" | "wallet" | "rest";
  category: TraderCategory;
  node: TreemapCategory | TreemapLeaf;
  rect: Rect;
  opacity: number;
  /** Header placement (header items only). */
  band?: "top" | "side";
}

type Layout = Map<string, Placed>;

interface Props {
  data: TradersData;
  tree: TreemapRoot;
  names: Record<TraderCategory, CategoryName>;
  focus: Focus;
  onFocus: (f: Focus) => void;
  onLeave: () => void;
  pin: Pin | null;
  onPin: (p: Pin | null) => void;
  zoomCat: TraderCategory | null;
  onZoom: (c: TraderCategory | null) => void;
  walletRows: Map<string, number[]>;
  /** Tallest the chart may be (px); it is never shorter than 360. */
  maxHeight?: number;
}

const childrenOf = (n: TreemapNode) => (n.kind === "root" || n.kind === "category" ? (n.children as TreemapNode[]) : undefined);

/**
 * Largest-first, except each category's "N more wallets" remainder, which goes
 * last: named wallets lead the reading (and Tab) order, and walletTile lays
 * the remainder out after them, at its full, honest area.
 */
function hierarchy(node: TreemapNode) {
  return d3
    .hierarchy<TreemapNode>(node, childrenOf)
    .sum((n) => (n.kind === "wallet" || n.kind === "rest" ? Math.max(0, n.value) : 0))
    .sort((a, b) => Number(a.data.kind === "rest") - Number(b.data.kind === "rest") || (b.value ?? 0) - (a.value ?? 0));
}

type RNode = d3.HierarchyRectangularNode<TreemapNode>;
const sq = d3.treemapSquarify.ratio(1.25);

/**
 * Squarify, except a block holding a remainder tile: there the named wallets
 * get one band (top or left, squarified inside) and the remainder the other,
 * cut whichever way gives both the squarer shapes. Plain squarify with the
 * remainder sorted last would close a row on the 15th wallet alone, leaving
 * it a full-width sliver. A remainder too small for a slab of its own is
 * squarified among the wallets by value.
 */
function walletTile(node: RNode, x0: number, y0: number, x1: number, y1: number) {
  const kids = node.children ?? [];
  const rest = kids.find((k) => k.data.kind === "rest");
  const total = node.value ?? 0;
  if (!rest || kids.length < 2 || !(total > 0)) return sq(node, x0, y0, x1, y1);
  const f = 1 - (rest.value ?? 0) / total;
  const w = x1 - x0;
  const h = y1 - y0;
  const asp = (a: number, b: number) => (a > 0 && b > 0 ? Math.max(a / b, b / a) : Infinity);
  const across = Math.max(asp(w * f, h), asp(w * (1 - f), h)); // named left, remainder right
  const down = Math.max(asp(w, h * f), asp(w, h * (1 - f))); // named top, remainder below
  if (Math.min(across, down) > 3 && f > 0.5) {
    // A remainder this small can't dominate: squarify it in plain value order (no sliver row).
    // (A large remainder keeps its own slab below, or the value order would put it first.)
    const byValue = [...kids].sort((a, b) => (b.value ?? 0) - (a.value ?? 0));
    return sq({ children: byValue, value: total } as unknown as RNode, x0, y0, x1, y1);
  }
  const named = { children: kids.filter((k) => k !== rest), value: total - (rest.value ?? 0) } as unknown as RNode;
  // Prefer named wallets on top: a full-width band keeps their tiles wide enough to label.
  if (down <= across * 1.35) {
    const ym = y0 + h * f;
    sq(named, x0, y0, x1, ym);
    Object.assign(rest, { x0, y0: ym, x1, y1 });
  } else {
    const xm = x0 + w * f;
    sq(named, x0, y0, xm, y1);
    Object.assign(rest, { x0: xm, y0, x1, y1 });
  }
}

const squarify = (W: number, H: number) =>
  d3
    .treemap<TreemapNode>()
    .tile(walletTile)
    .size([W, H])
    .paddingInner((n) => (n.depth === 0 ? GAP + 2 : GAP));

/* ── Header labels ─────────────────────────────────────────────────── */

const hdrSize = (narrow: boolean) => (narrow ? 11 : 11.5);

interface HeaderLabel {
  /** Category name (bold), or null for the percentage alone. */
  name: string | null;
  /** Share of the total stake, or null for the key dot alone. */
  pct: string | null;
  /** Category volume, right-aligned. */
  money: string | null;
  /** Width of key dot + name + pct (without money). */
  width: number;
}

/**
 * The richest header that fits `maxW`, keeping the $ for as long as possible:
 * full name + % + $ → short name + % + $ → short name + % → % → key dot.
 */
/** Width of key dot + name + percentage as drawn in a header. */
function labelWidth(name: string | null, pct: string | null, narrow: boolean) {
  const fs = hdrSize(narrow);
  return (
    DOT +
    (name || pct ? DOT_GAP : 0) +
    (name ? textWidth(name, font(650, fs)) : 0) +
    (name && pct ? 5 : 0) +
    (pct ? textWidth(pct, font(500, fs)) : 0)
  );
}

const pctText = (cat: TreemapCategory) => `${cat.pctOfTotal.toFixed(1)}%`;

function headerLabel(cat: TreemapCategory, maxW: number, narrow: boolean, withMoney = true): HeaderLabel | null {
  const pct = pctText(cat);
  const money = fmtUSDCompact(cat.value);
  const moneyW = 14 + textWidth(money, font(500, hdrSize(narrow)));
  const ladder: [string | null, string | null, boolean][] = [
    [cat.name, pct, true],
    [cat.short, pct, true],
    [cat.short, pct, false],
    [null, pct, false],
    [null, null, false],
  ];
  for (const [name, p, m] of ladder) {
    if (m && !withMoney) continue;
    const width = labelWidth(name, p, narrow);
    if (width + (m ? moneyW : 0) + 1 <= maxW) return { name, pct: p, money: m ? money : null, width };
  }
  return null;
}

/** Pick each category's header band from its (header-less) block. */
function headerMode(cat: TreemapCategory, w: number, h: number, narrow: boolean): HeaderMode {
  const topLeaf = h - HEADER - GAP;
  const topOK = topLeaf >= MIN_LEAF && w >= DOT + HDR_PAD * 2;
  const topShare = topOK ? topLeaf / h : 0;
  if (topShare >= 0.6) return { mode: "top" };
  // Short block: a band at the left keeps the wallets the block's full height.
  let side: number | null = null;
  if (h >= 14) {
    const pct = pctText(cat);
    const options: [string | null, string | null][] = [
      [cat.short, pct],
      [null, pct],
      [null, null],
    ];
    for (const [name, p] of options) {
      const bandW = Math.ceil(labelWidth(name, p, narrow) + HDR_PAD * 2 + 1);
      if (w - bandW - GAP >= Math.max(32, w * 0.4)) {
        side = bandW;
        break;
      }
    }
  }
  const sideShare = side != null ? (w - side - GAP) / w : 0;
  if (side != null && sideShare > topShare) return { mode: "side", width: side };
  return topOK ? { mode: "top" } : { mode: "none" };
}

/** Overview: categories with a header band (top, side or none), wallets inside. */
function layoutAll(tree: TreemapRoot, W: number, H: number, narrow: boolean): { placed: Layout; leafAreas: Map<TraderCategory, Rect> } {
  // Pass 1: category blocks. A node's own padding never moves the node, so
  // these are the blocks pass 2 lays out too.
  const blocks = squarify(W, H)(hierarchy(tree));
  const modes = new Map<string, HeaderMode>();
  for (const c of blocks.children ?? []) {
    const cat = c.data as TreemapCategory;
    modes.set(cat.key, headerMode(cat, c.x1 - c.x0, c.y1 - c.y0, narrow));
  }
  // Pass 2: wallets inside each block, under (or beside) its band.
  const root = squarify(W, H)
    .paddingTop((n) => (n.depth === 1 && modes.get(n.data.key)?.mode === "top" ? HEADER + GAP : 0))
    .paddingLeft((n) => {
      const m = n.depth === 1 ? modes.get(n.data.key) : undefined;
      return m?.mode === "side" ? m.width + GAP : 0;
    })(hierarchy(tree));

  const placed: Layout = new Map();
  const leafAreas = new Map<TraderCategory, Rect>();
  for (const c of root.children ?? []) {
    const cat = c.data as TreemapCategory;
    const m = modes.get(cat.key) ?? { mode: "none" };
    // The wallets' area (block minus header band) is what a zoom maps onto the full chart.
    leafAreas.set(cat.category, {
      x0: m.mode === "side" ? Math.min(c.x1, c.x0 + m.width + GAP) : c.x0,
      y0: m.mode === "top" ? Math.min(c.y1, c.y0 + HEADER + GAP) : c.y0,
      x1: c.x1,
      y1: c.y1,
    });
    if (m.mode !== "none") {
      placed.set(cat.key, {
        key: cat.key,
        kind: "header",
        category: cat.category,
        node: cat,
        rect:
          m.mode === "top"
            ? { x0: c.x0, y0: c.y0, x1: c.x1, y1: Math.min(c.y1, c.y0 + HEADER) }
            : { x0: c.x0, y0: c.y0, x1: Math.min(c.x1, c.x0 + m.width), y1: c.y1 },
        opacity: 1,
        band: m.mode,
      });
    }
    for (const l of c.children ?? []) {
      const leaf = l.data as TreemapLeaf;
      placed.set(leaf.key, {
        key: leaf.key,
        kind: leaf.kind,
        category: cat.category,
        node: leaf,
        rect: { x0: l.x0, y0: l.y0, x1: l.x1, y1: l.y1 },
        opacity: 1,
      });
    }
  }
  return { placed, leafAreas };
}

type AllLayout = ReturnType<typeof layoutAll>;

/** A category's wallets re-squarified to fill the chart. */
function layoutLeaves(tree: TreemapRoot, W: number, H: number, cat: TraderCategory): Layout {
  const out: Layout = new Map();
  const node = tree.children.find((c) => c.category === cat);
  if (!node) return out;
  const root = d3.treemap<TreemapNode>().tile(walletTile).size([W, H]).paddingInner(GAP)(hierarchy(node));
  for (const l of root.children ?? []) {
    const leaf = l.data as TreemapLeaf;
    out.set(leaf.key, { key: leaf.key, kind: leaf.kind, category: cat, node: leaf, rect: { x0: l.x0, y0: l.y0, x1: l.x1, y1: l.y1 }, opacity: 1 });
  }
  return out;
}

const lerp = (a: number, b: number, e: number) => a + (b - a) * e;

/** Remainder-tile hatch: one pattern per category (pattern content can't inherit the tile's color). */
const HATCH = 7;
const hatchId = (c: TraderCategory) => `tr-hatch-${c}`;

/**
 * The overview under a partial zoom (e = 0 → overview, 1 → the category's
 * wallet area fills the chart). One affine map for every tile, so nothing
 * overlaps; tiles outside the category fade as they slide off.
 */
function zoomedOverview(all: AllLayout, cat: TraderCategory, W: number, H: number, e: number): Layout {
  const a = all.leafAreas.get(cat);
  if (!a) return all.placed;
  const kx = W / Math.max(1e-6, a.x1 - a.x0);
  const ky = H / Math.max(1e-6, a.y1 - a.y0);
  const fx = (x: number) => lerp(x, (x - a.x0) * kx, e);
  const fy = (y: number) => lerp(y, (y - a.y0) * ky, e);
  const out: Layout = new Map();
  for (const [k, p] of all.placed) {
    const inside = p.category === cat && p.kind !== "header";
    out.set(k, {
      ...p,
      rect: { x0: fx(p.rect.x0), y0: fy(p.rect.y0), x1: fx(p.rect.x1), y1: fy(p.rect.y1) },
      opacity: inside ? 1 : 1 - e,
    });
  }
  return out;
}

/** Static layout for a zoom state (every key present, so hit-testing and highlights can look anything up). */
function staticLayout(tree: TreemapRoot, all: AllLayout, W: number, H: number, cat: TraderCategory | null): Layout {
  if (!cat || !all.leafAreas.has(cat)) return all.placed;
  const out = zoomedOverview(all, cat, W, H, 1);
  for (const [k, p] of out) if (p.category === cat && p.kind !== "header") out.set(k, { ...p, opacity: 0 });
  for (const [k, p] of layoutLeaves(tree, W, H, cat)) out.set(k, p);
  return out;
}

interface Frame {
  base: Layout;
  overlay: Layout | null;
  overlayOpacity: number;
}

/**
 * Category zoom transition. In: affine zoom of the overview, then the
 * re-squarified wallets dissolve in on top. Out: the reverse. Any other
 * change (resize, data, legend) snaps.
 */
function useZoomTransition(tree: TreemapRoot, all: AllLayout | null, W: number, H: number, zoomCat: TraderCategory | null) {
  const [frame, setFrame] = useState<Frame | null>(null);
  const latest = useRef({ tree, all, W, H });
  const prevCat = useRef(zoomCat);

  useLayoutEffect(() => {
    latest.current = { tree, all, W, H };
  });

  useLayoutEffect(() => {
    const from = prevCat.current;
    prevCat.current = zoomCat;
    if (from === zoomCat) return;
    const cat = zoomCat ?? from;
    if (!cat || (from && zoomCat) || prefersReducedMotion() || !latest.current.all) {
      setFrame(null);
      return;
    }
    const zoomIn = zoomCat !== null;
    const SPLIT = 0.64; // share of the time spent on the affine zoom
    const at = (t: number): Frame | null => {
      const L = latest.current;
      if (!L.all) return null;
      const leaves = () => layoutLeaves(L.tree, L.W, L.H, cat);
      if (zoomIn) {
        if (t < SPLIT) return { base: zoomedOverview(L.all, cat, L.W, L.H, d3.easeCubicInOut(t / SPLIT)), overlay: null, overlayOpacity: 0 };
        return {
          base: zoomedOverview(L.all, cat, L.W, L.H, 1),
          overlay: leaves(),
          overlayOpacity: d3.easeQuadOut((t - SPLIT) / (1 - SPLIT)),
        };
      }
      const fade = 1 - SPLIT;
      if (t < fade) return { base: zoomedOverview(L.all, cat, L.W, L.H, 1), overlay: leaves(), overlayOpacity: 1 - d3.easeQuadIn(t / fade) };
      return { base: zoomedOverview(L.all, cat, L.W, L.H, 1 - d3.easeCubicInOut((t - fade) / SPLIT)), overlay: null, overlayOpacity: 0 };
    };
    setFrame(at(0));
    const timer = d3.timer((elapsed) => {
      const t = Math.min(1, elapsed / DUR);
      if (t >= 1) {
        timer.stop();
        setFrame(null);
      } else setFrame(at(t));
    });
    return () => {
      timer.stop();
      setFrame(null);
    };
  }, [zoomCat]);

  return frame;
}

/* ── Label fitting ─────────────────────────────────────────────────── */

const PAD = 6;

function fit(text: string, f: string, maxW: number) {
  return textWidth(text, f) + 1 <= maxW;
}

interface LeafLabel {
  lines: { text: string; cls: string; size: number; f: string }[];
}

function leafLabel(p: Placed, narrow: boolean): LeafLabel | null {
  const w = p.rect.x1 - p.rect.x0;
  const h = p.rect.y1 - p.rect.y0;
  const s1 = narrow ? 11 : 12;
  const s2 = narrow ? 10.5 : 11.5;
  const f1 = font(650, s1);
  const f2 = font(500, s2);
  const maxW = w - PAD * 2;
  const leaf = p.node as TreemapLeaf;
  const L1 = (text: string) => ({ text, cls: "l1", size: s1, f: f1 });
  const L2 = (text: string) => ({ text, cls: "l2", size: s2, f: f2 });

  // Candidate layouts, most complete first. The same text may wrap over more
  // lines; when nothing fits, the $ line goes first, then the whole label.
  let candidates: ReturnType<typeof L1>[][];
  const money = fmtUSD(leaf.value);
  if (leaf.kind === "wallet") {
    const addr = `${leaf.id.slice(0, 6)}…`;
    const share = `(${leaf.pctOfTotal.toFixed(1)}%)`;
    candidates = [
      [L1(leaf.label), L2(money)],
      [L1(addr), L1(share), L2(money)],
      [L1(leaf.label)],
      [L1(addr), L1(share)],
    ];
  } else {
    const n = `${leaf.count} more wallet${leaf.count === 1 ? "" : "s"}`;
    candidates = [
      [L1(leaf.label)],
      [L1(n), L2(money)],
      [L1(`${leaf.count} more`), L1(leaf.count === 1 ? "wallet" : "wallets"), L2(money)],
      [L1(`+${fmtInt(leaf.count)}`), L2(money)],
      [L1(`+${fmtInt(leaf.count)}`)],
    ];
  }
  for (const lines of candidates) {
    const need = PAD * 2 - 3 + lines.reduce((a, l) => a + l.size * (l.cls === "l1" ? 1.25 : 1.3), 0);
    if (h >= need && lines.every((l) => fit(l.text, l.f, maxW))) return { lines };
  }
  return null;
}

/* ── Component ─────────────────────────────────────────────────────── */

export function Treemap({ data, tree, names, focus, onFocus, onLeave, pin, onPin, zoomCat, onZoom, walletRows, maxHeight = 640 }: Props) {
  const [wrapRef, { width: W }] = useSize<HTMLDivElement>();
  const svgRef = useRef<SVGSVGElement>(null);
  const pal = usePalette();
  const mix = restMix(pal.surface);
  const hdrMix = headerMix(pal.surface);
  // Label ink per fill, from the colors the browser actually paints in this
  // theme: light or dark ink, whichever contrasts more (every category clears
  // 4.5:1 that way in both themes). Re-resolved when the theme changes.
  const inks = useMemo(() => {
    const out = {} as Record<TraderCategory, { solid: string; rest: string; hdr: string; hdrSub: string }>;
    for (const c of CATEGORY_ORDER) {
      const band = pal.rgb(catTint(c, hdrMix));
      out[c] = {
        solid: inkOn(pal.rgb(CAT_COLOR[c]), pal),
        rest: inkOn(pal.rgb(catTint(c, mix)), pal),
        hdr: inkOn(band, pal),
        hdrSub: subInkOn(band, pal),
      };
    }
    return out;
  }, [pal, mix, hdrMix]);
  const [tip, setTip] = useState<{ key: string; x: number; y: number } | null>(null);
  const [kbKey, setKbKey] = useState<string | null>(null);
  // Roving tabindex: the treemap is one Tab stop (this tile), arrow keys walk
  // the tiles. Survives blur so Shift+Tab back lands where the visitor left.
  const [rovingKey, setRovingKey] = useState<string | null>(null);

  const H = W > 0 ? Math.round(Math.max(360, Math.min(W, maxHeight))) : 0;
  const narrow = W < 480;

  const traderAt = useCallback((row: number): Trader | undefined => data.traders[row], [data]);
  const siblingsOf = (tr: Trader) => (walletRows.get(tr.id)?.length ?? 1) - 1;

  const fontVersion = useFontVersion();
  const all = useMemo(() => {
    void fontVersion; // side-band widths are measured text
    return W > 0 ? layoutAll(tree, W, H, narrow) : null;
  }, [tree, W, H, narrow, fontVersion]);
  const display = useMemo<Layout>(() => (all ? staticLayout(tree, all, W, H, zoomCat) : new Map()), [all, tree, W, H, zoomCat]);
  const frame = useZoomTransition(tree, all, W, H, zoomCat);
  const animating = frame !== null;

  // Labels fade in only as a zoom settles. Any other remount (resize, font
  // swap) shows them at full ink at once — never a half-faded label.
  const [labelsFade, setLabelsFade] = useState(false);
  const wasAnimating = useRef(false);
  useEffect(() => {
    const ended = wasAnimating.current && !animating;
    wasAnimating.current = animating;
    if (!ended) return;
    setLabelsFade(true);
    const id = window.setTimeout(() => setLabelsFade(false), 320);
    return () => window.clearTimeout(id);
  }, [animating]);

  // Leaving a category that vanished (e.g. new data) falls back to the overview.
  useEffect(() => {
    if (zoomCat && !tree.children.some((c) => c.category === zoomCat)) onZoom(null);
  }, [zoomCat, tree, onZoom]);

  const items = useMemo(() => [...(frame?.base ?? display).values()], [frame, display]);
  const overlayItems = useMemo(() => (frame?.overlay ? [...frame.overlay.values()] : null), [frame]);

  /** Keyboard order: every tile that is drawn and interactive, in layout order (category by category). */
  const kbOrder = useMemo(
    () => items.filter((p) => p.opacity > 0.5 && p.rect.x1 - p.rect.x0 >= 0.5 && p.rect.y1 - p.rect.y0 >= 0.5).map((p) => p.key),
    [items],
  );
  const tabStop = rovingKey && kbOrder.includes(rovingKey) ? rovingKey : (kbOrder[0] ?? null);

  /* ── Highlight ───────────────────────────────────────────────────── */

  const onKey = useMemo<((p: Placed) => boolean) | null>(() => {
    if (!focus) return null;
    if (focus.kind === "category") {
      const c = focus.category;
      return (p) => p.category === c;
    }
    if (focus.kind === "rest") {
      const k = `rest:${focus.category}`;
      return (p) => p.key === k || (p.kind === "header" && p.category === focus.category);
    }
    const tr = traderAt(focus.row);
    if (!tr) return null;
    const wk = `w:${focus.row}`;
    const target = display.has(wk) ? wk : `rest:${tr.category}`;
    return (p) => p.key === target || (p.kind === "header" && p.category === tr.category);
  }, [focus, traderAt, display]);

  const outlineKey = useMemo(() => {
    if (focus?.kind === "wallet") {
      const tr = traderAt(focus.row);
      if (!tr) return null;
      return display.has(`w:${focus.row}`) ? `w:${focus.row}` : `rest:${tr.category}`;
    }
    if (focus?.kind === "rest") return `rest:${focus.category}`;
    return null;
  }, [focus, traderAt, display]);

  /* ── Events (delegated) ──────────────────────────────────────────── */

  const placedAt = (el: EventTarget | null): Placed | null => {
    const g = (el as Element | null)?.closest?.("[data-key]");
    const k = g?.getAttribute("data-key");
    return k ? (display.get(k) ?? null) : null;
  };

  const focusFor = useCallback(
    (p: Placed): Focus => {
      if (p.kind === "header") return { kind: "category", category: p.category, source: "treemap" };
      if (p.kind === "rest") return { kind: "rest", category: p.category, source: "treemap" };
      return { kind: "wallet", row: (p.node as TreemapWallet).row, source: "treemap" };
    },
    [],
  );

  const onPointerMove = (e: PointerEvent<SVGSVGElement>) => {
    if (animating) return;
    const p = placedAt(e.target);
    if (!p || p.opacity < 0.5) {
      setTip(null);
      onLeave();
      return;
    }
    const rect = svgRef.current!.getBoundingClientRect();
    if (e.pointerType !== "touch") setTip({ key: p.key, x: e.clientX - rect.left, y: e.clientY - rect.top });
    onFocus(focusFor(p));
  };

  const activate = (p: Placed) => {
    if (p.kind === "header") onZoom(zoomCat === p.category ? null : p.category);
    else if (p.kind === "rest") {
      if (!zoomCat) onZoom(p.category);
    } else {
      const row = (p.node as TreemapWallet).row;
      onPin(pin?.row === row ? null : { row, source: "treemap" });
    }
  };

  const onClick = (e: MouseEvent<SVGSVGElement>) => {
    if (animating) return;
    const p = placedAt(e.target);
    if (!p || p.opacity < 0.5) {
      onPin(null);
      return;
    }
    if (p.kind === "header" || p.kind === "rest") setTip(null);
    activate(p);
  };

  const focusTile = (key: string) => {
    svgRef.current?.querySelector<SVGGElement>(`[data-key="${CSS.escape(key)}"]`)?.focus();
  };

  const onKeyDown = (e: KeyboardEvent<SVGGElement>, p: Placed) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      activate(p);
      return;
    }
    if (animating || !kbOrder.length) return;
    const i = kbOrder.indexOf(p.key);
    let next: string | undefined;
    if (e.key === "ArrowRight" || e.key === "ArrowDown") next = kbOrder[(i + 1) % kbOrder.length];
    else if (e.key === "ArrowLeft" || e.key === "ArrowUp") next = kbOrder[(i - 1 + kbOrder.length) % kbOrder.length];
    else if (e.key === "Home") next = kbOrder[0];
    else if (e.key === "End") next = kbOrder[kbOrder.length - 1];
    if (next === undefined) return;
    e.preventDefault();
    focusTile(next);
  };

  /* ── Render ──────────────────────────────────────────────────────── */

  const pinnedKey = pin ? `w:${pin.row}` : null;
  const tipPlaced = tip ? display.get(tip.key) : undefined;
  const kbPlaced = kbKey ? display.get(kbKey) : undefined;
  const docked = W > 0 && W < 520;
  const pinTrader = pin ? traderAt(pin.row) : undefined;
  const pinPlaced = pinnedKey ? display.get(pinnedKey) : undefined;
  const showPin = !!pinTrader && pin?.source === "treemap" && (docked || (!!pinPlaced && pinPlaced.opacity > 0.5 && !animating));

  const tipFor = (p: Placed) => {
    if (p.kind === "header") {
      const c = p.node as TreemapCategory;
      return (
        <>
          <div className="tip-title tr-tip-title tr-tip-cat-title">
            <CategoryKey category={c.category} />
            {c.name}
          </div>
          <TipRow value={fmtUSD(c.value)} label="Volume" />
          <TipRow value={fmtPct(c.pctOfTotal)} label="of total whale stake" />
          <TipRow value={fmtInt(c.count)} label={c.count === 1 ? "wallet" : "wallets"} />
          <div className="tip-sub">{zoomCat === c.category ? "Click to zoom out" : "Click to zoom in"}</div>
        </>
      );
    }
    const leaf = p.node as TreemapLeaf;
    if (leaf.kind === "rest") {
      return (
        <>
          <div className="tip-title tr-tip-title">
            {fmtInt(leaf.count)} more wallet{leaf.count === 1 ? "" : "s"}
          </div>
          <div className="tr-tip-cat">
            <CategoryKey category={leaf.category} />
            {names[leaf.category].label}
          </div>
          <TipRow value={fmtUSD(leaf.value)} label="Combined position" />
          <TipRow value={fmtPct(leaf.pctOfTotal)} label="of total whale stake" />
          <TipRow value={fmtPct(leaf.pctOfCategory)} label="of category" />
          <div className="tip-sub">Smaller than the top 15 in this category{zoomCat ? "" : " · click to zoom in"}</div>
        </>
      );
    }
    const tr = traderAt(leaf.row);
    const sib = tr ? siblingsOf(tr) : 0;
    return (
      <>
        <div className="tip-title tr-tip-title">
          <span className="mono">{shortAddr(leaf.id)}</span>
        </div>
        <div className="tr-tip-cat">
          <CategoryKey category={leaf.category} />
          {names[leaf.category].label}
        </div>
        <TipRow value={fmtUSD(leaf.value)} label="Position" />
        <TipRow value={fmtPct(leaf.pctOfTotal)} label="of total whale stake" />
        <TipRow value={fmtPct(leaf.pctOfCategory)} label="of category" />
        <TipRow value={fmtInt(leaf.priorBets)} label="Prior bets" />
        <TipRow value={fmtPct(leaf.concentration)} label="Portfolio concentration" />
        {tr && <TipRow value={tr.riskScore.toFixed(1)} label="Risk score / 100" />}
        {sib > 0 && (
          <div className="tip-sub">
            + {sib} other whale position{sib === 1 ? "" : "s"} from this wallet
          </div>
        )}
        <div className="tip-sub">{pin?.row === leaf.row ? "Pinned · click again to unpin" : "Click to pin details"}</div>
      </>
    );
  };

  const tileEl = (p: Placed, keyPrefix = "") => {
    const { x0, y0, x1, y1 } = p.rect;
    const w = x1 - x0;
    const h = y1 - y0;
    if (w < 0.5 || h < 0.5 || p.opacity <= 0.01) return null;
    const cat = p.category;
    const node = p.node;
    const hiddenCat = node.hidden;
    const dim = onKey && !onKey(p);
    const op = p.opacity * (hiddenCat ? 0.2 : 1);
    const interactive = p.opacity > 0.5;
    // React wants `key` on the element itself, never inside a spread.
    const key = keyPrefix + p.key;
    const common = {
      "data-key": p.key,
      className: `tile tile-${p.kind}${dim ? " dim" : ""}${p.key === outlineKey ? " hl" : ""}${p.key === pinnedKey ? " pinned" : ""}`,
      style: { opacity: op },
      // One tile per treemap is in the Tab order; the base layer only (the zoom overlay repeats keys).
      tabIndex: interactive && !keyPrefix && p.key === tabStop ? 0 : -1,
      role: "button",
      onKeyDown: (e: KeyboardEvent<SVGGElement>) => onKeyDown(e, p),
      onFocus: (e: FocusEvent<SVGGElement>) => {
        setRovingKey(p.key);
        // Keyboard focus only: a mouse click also focuses the tile, but the pointer tooltip covers that.
        if (!e.currentTarget.matches(":focus-visible")) return;
        setKbKey(p.key);
        onFocus(focusFor(p));
      },
      onBlur: () => {
        setKbKey(null);
        onLeave();
      },
    };

    if (p.kind === "header") {
      const c = node as TreemapCategory;
      const lab = !animating && !zoomCat && h >= 14 ? headerLabel(c, w - HDR_PAD * 2, narrow, p.band !== "side") : null;
      const fs = hdrSize(narrow);
      const cy = y0 + h / 2;
      const kx = x0 + HDR_PAD + DOT / 2;
      return (
        <g key={key} {...common} aria-label={`${c.label}, ${fmtUSD(c.value)}. ${zoomCat === cat ? "Zoom out" : "Zoom in"}`}>
          <rect x={x0} y={y0} width={w} height={h} rx={Math.min(TILE_RX, h / 2, w / 2)} style={{ fill: catTint(cat, hdrMix) }} />
          {lab && (
            <g className="hdr-label">
              {/* Category key: identity comes from the dot, the text stays ink. */}
              <circle cx={kx} cy={cy} r={DOT / 2} className="hdr-key" style={{ fill: CAT_COLOR[cat] }} />
              {cat === "critical" && <circle cx={kx} cy={cy} r={DOT * 0.18} className="hdr-key-core" />}
              {(lab.name || lab.pct) && (
                <text x={x0 + HDR_PAD + DOT + DOT_GAP} y={cy} dy="0.35em" style={{ fontSize: fs }}>
                  {lab.name && (
                    <tspan className="hdr-l" style={{ fill: inks[cat].hdr }}>
                      {lab.name}
                    </tspan>
                  )}
                  {lab.pct && (
                    <tspan className="hdr-p" dx={lab.name ? 5 : 0} style={{ fill: inks[cat].hdrSub }}>
                      {lab.pct}
                    </tspan>
                  )}
                </text>
              )}
              {lab.money && (
                <text x={x1 - HDR_PAD} y={cy} dy="0.35em" textAnchor="end" className="hdr-r" style={{ fontSize: fs, fill: inks[cat].hdrSub }}>
                  {lab.money}
                </text>
              )}
            </g>
          )}
        </g>
      );
    }

    const leaf = node as TreemapLeaf;
    const rest = leaf.kind === "rest";
    // The remainder is many wallets, not one: a hatched wash, never a solid wallet fill.
    const fill = rest ? `url(#${hatchId(cat)})` : CAT_COLOR[cat];
    const ink = rest ? inks[cat].rest : inks[cat].solid;
    const lab = !animating ? leafLabel(p, narrow) : null;
    const rx = Math.min(TILE_RX, Math.min(w, h) / 3);
    const s1 = lab?.lines[0]?.size ?? 12;
    return (
      <g
        key={key}
        {...common}
        aria-label={
          leaf.kind === "wallet"
            ? `Wallet ${leaf.id}, ${fmtUSD(leaf.value)}, ${fmtPct(leaf.pctOfTotal)} of total. Pin details`
            : `${leaf.count} more wallets, ${fmtUSD(leaf.value)}`
        }
      >
        <rect x={x0} y={y0} width={w} height={h} rx={rx} style={{ fill }} />
        {lab && (
          <text className={`leaf-text${rest ? " on-hatch" : ""}`} style={rest ? { fill: ink, stroke: catTint(cat, mix) } : { fill: ink }}>
            {lab.lines.map((l, i) => {
              let y = y0 + PAD + s1 * 0.95;
              for (let j = 0; j < i; j++) y += lab.lines[j + 1].size * (lab.lines[j + 1].cls === "l1" ? 1.25 : 1.3);
              return (
                <tspan key={i} className={l.cls} x={x0 + PAD} y={y} style={{ fontSize: l.size }}>
                  {l.text}
                </tspan>
              );
            })}
          </text>
        )}
      </g>
    );
  };

  // Outline overlays (drawn last so neighbours never cover them).
  const outline = (key: string | null, cls: string) => {
    const p = key ? display.get(key) : undefined;
    if (!p || p.opacity < 0.5) return null;
    const { x0, y0, x1, y1 } = p.rect;
    const w = x1 - x0;
    const h = y1 - y0;
    if (w < 2 || h < 2) return null;
    return <rect key={cls} className={cls} x={x0 + 1} y={y0 + 1} width={w - 2} height={h - 2} rx={Math.min(TILE_RX, w / 2, h / 2)} />;
  };

  const tipKey = tip?.key ?? null;
  const tipAnchor = tipPlaced
    ? tip!
    : kbPlaced
      ? { x: (kbPlaced.rect.x0 + kbPlaced.rect.x1) / 2, y: (kbPlaced.rect.y0 + kbPlaced.rect.y1) / 2 }
      : null;
  const tipItem = tipPlaced ?? kbPlaced;

  return (
    <div className="tr-treemap">
      <div ref={wrapRef} className="chart tr-chart" style={{ height: H || undefined }}>
        {W > 0 && (
          <>
            <svg
              ref={svgRef}
              width={W}
              height={H}
              className={`tr-svg tr-tm${animating ? " animating" : ""}${labelsFade ? " labels-fade" : ""}`}
              onPointerMove={onPointerMove}
              onPointerLeave={() => {
                setTip(null);
                onLeave();
              }}
              onClick={onClick}
            >
              <defs>
                {tree.children.map((c) => (
                  <pattern
                    key={c.category}
                    id={hatchId(c.category)}
                    width={HATCH}
                    height={HATCH}
                    patternUnits="userSpaceOnUse"
                    patternTransform="rotate(45)"
                  >
                    <rect width={HATCH} height={HATCH} style={{ fill: catTint(c.category, mix) }} />
                    <line x1={HATCH / 2} y1={0} x2={HATCH / 2} y2={HATCH} className="hatch-line" style={{ stroke: CAT_COLOR[c.category] }} />
                  </pattern>
                ))}
              </defs>
              <rect width={W} height={H} className="tr-bg" />
              <g role="group" aria-label="Whale positions by category. Arrow keys move between tiles; Enter pins a wallet or zooms a category.">
                {items.map((p) => tileEl(p))}
              </g>
              {overlayItems && <g style={{ opacity: frame?.overlayOpacity ?? 0 }}>{overlayItems.map((p) => tileEl(p, "o:"))}</g>}
              {!animating && outline(outlineKey !== pinnedKey ? outlineKey : null, "tile-outline")}
              {/* Keyboard focus on a header or "N more" tile has no hover outline of its own: draw one. */}
              {!animating && kbKey !== outlineKey && kbKey !== pinnedKey && outline(kbKey, "tile-outline kb")}
              {!animating && outline(pinnedKey, "tile-outline pinned")}
            </svg>
            {tipItem && tipAnchor && !animating && (tipKey || kbKey) && (
              <Tooltip x={tipAnchor.x} y={tipAnchor.y} visible containerWidth={W} containerHeight={H}>
                {tipFor(tipItem)}
              </Tooltip>
            )}
            {showPin && !docked && pinPlaced && (
              <PinCard
                trader={pinTrader!}
                names={names}
                totalVolume={tree.value}
                anchor={{ x: (pinPlaced.rect.x0 + pinPlaced.rect.x1) / 2, y: (pinPlaced.rect.y0 + pinPlaced.rect.y1) / 2 }}
                container={{ width: W, height: H }}
                onClose={() => onPin(null)}
              />
            )}
          </>
        )}
      </div>
      {showPin && docked && (
        <PinCard
          trader={pinTrader!}
          names={names}
          totalVolume={tree.value}
          docked
          onClose={() => onPin(null)}
        />
      )}
    </div>
  );
}
