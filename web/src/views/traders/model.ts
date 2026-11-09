// Pure math for the Traders view (port of VisualizeInsiders.py).
//
// This module must stay runnable under Node's type stripping: type-only
// imports, no enums / namespaces / parameter properties, no runtime imports.

import type { Trader, TraderCategory, TradersData } from "../../types";

/** Draw / legend / treemap order — the Dash app's `cat_colors` dict order. */
export const CATEGORY_ORDER: readonly TraderCategory[] = ["normal", "newAccount", "concentration", "critical"];

/** Wallets shown per category in the treemap (`cat_df.nlargest(15, ...)`). */
export const TOP_N = 15;

/* ── Summary (the five stat tiles) ──────────────────────────────────── */

export interface TraderSummary {
  totalVolume: number;
  suspiciousVolume: number;
  /** Percent 0–100. */
  pctSuspicious: number;
  totalWhales: number;
  flaggedAccounts: number;
}

/** Python's round(x, nd) for display-precision parity with export.py. */
function roundTo(x: number, nd: number): number {
  const f = 10 ** nd;
  return Math.round(x * f) / f;
}

/**
 * Prefer the exported figure when the recomputed one agrees with it within
 * `tol`. The export rounds every position to cents, so a sum over the array
 * drifts from Python's unrounded sum by up to n × ½¢; inside that bound the
 * exported value is the more precise one (it is what the Dash app showed).
 */
function reconcile(computed: number, exported: number | undefined, tol: number): number {
  return exported != null && Number.isFinite(exported) && Math.abs(computed - exported) <= tol ? exported : computed;
}

/**
 * run_app() totals, recomputed from the traders array:
 * total = Σ market_position, suspicious = Σ over is_suspicious rows,
 * ratio = suspicious / total × 100 (0 when there is no volume),
 * whales = row count, flagged = is_suspicious row count.
 * Volumes are cents, the ratio 4 dp — identical to the exported totals.
 */
export function computeTraderSummary(t: TradersData): TraderSummary {
  let total = 0;
  let suspicious = 0;
  let flagged = 0;
  for (const tr of t.traders) {
    total += tr.position;
    if (tr.suspicious) {
      suspicious += tr.position;
      flagged += 1;
    }
  }
  const n = t.traders.length;
  const totalVolume = reconcile(roundTo(total, 2), t.totalVolume, n * 0.005 + 1e-6);
  const suspiciousVolume = reconcile(roundTo(suspicious, 2), t.suspiciousVolume, flagged * 0.005 + 1e-6);
  const pct = totalVolume > 0 ? (suspiciousVolume / totalVolume) * 100 : 0;
  return {
    totalVolume,
    suspiciousVolume,
    pctSuspicious: reconcile(roundTo(pct, 4), t.pctSuspicious, 1e-3),
    totalWhales: n,
    flaggedAccounts: flagged,
  };
}

/* ── Category names ─────────────────────────────────────────────────── */

export interface CategoryName {
  /** Legend / pinned-card label, e.g. "New account (≤10 prior trades)". */
  label: string;
  /** Compact label for tight treemap headers, e.g. "New account". */
  short: string;
}

/** 80 → "80", 80.5 → "80.5" */
export function fmtThreshold(v: number): string {
  return Number.isInteger(v) ? String(v) : String(Number(v.toFixed(2)));
}

/** Labels derived from the exported thresholds (is_first_x: ≤ firstX; Is_percent: ≥ percentVolume). */
export function categoryNames(th: TradersData["thresholds"]): Record<TraderCategory, CategoryName> {
  return {
    normal: { label: "Normal whale", short: "Normal" },
    newAccount: { label: `New account (≤${fmtThreshold(th.firstX)} prior trades)`, short: "New account" },
    concentration: {
      label: `≥ ${fmtThreshold(th.percentVolume)}% portfolio concentration`,
      short: "Concentration",
    },
    critical: { label: "Both flags (critical)", short: "Critical" },
  };
}

export function countByCategory(t: TradersData): Record<TraderCategory, number> {
  const out: Record<TraderCategory, number> = { normal: 0, newAccount: 0, concentration: 0, critical: 0 };
  for (const tr of t.traders) out[tr.category] += 1;
  return out;
}

/* ── Treemap hierarchy (fig_treemap_concentration) ─────────────────── */

export interface TreemapWallet {
  kind: "wallet";
  /** `w:<row>` — wallets can hold several whale positions, so rows (not addresses) are the identity. */
  key: string;
  /** Index into TradersData.traders. */
  row: number;
  id: string;
  category: TraderCategory;
  value: number;
  /** Percent of the total whale stake. */
  pctOfTotal: number;
  /** Percent of this category's volume. */
  pctOfCategory: number;
  priorBets: number;
  concentration: number;
  /** "0x1234… (x.x%)" — share of the total, like the Dash tile label. */
  label: string;
  hidden: boolean;
}

/** Everything beyond the top 15, so each category's area stays equal to its full volume. */
export interface TreemapRest {
  kind: "rest";
  key: string;
  category: TraderCategory;
  value: number;
  count: number;
  /** Row indices folded into this tile. */
  rows: number[];
  pctOfTotal: number;
  pctOfCategory: number;
  /** "N more wallets · $X" */
  label: string;
  hidden: boolean;
}

export type TreemapLeaf = TreemapWallet | TreemapRest;

export interface TreemapCategory {
  kind: "category";
  key: string;
  category: TraderCategory;
  /** Category name without the share, e.g. "Normal whale". */
  name: string;
  short: string;
  /** "<Category> (x.x%)" of the total, like the Dash category label. */
  label: string;
  value: number;
  count: number;
  pctOfTotal: number;
  /** Category toggled off in the legend: keep its (honest) area, render dimmed. */
  hidden: boolean;
  children: TreemapLeaf[];
}

export interface TreemapRoot {
  kind: "root";
  key: "root";
  label: string;
  value: number;
  count: number;
  children: TreemapCategory[];
}

export type TreemapNode = TreemapRoot | TreemapCategory | TreemapLeaf;

const usd = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  minimumFractionDigits: 0,
  maximumFractionDigits: 0,
});

/** $12,345 */
export function formatUSD(v: number): string {
  return usd.format(v);
}

const pct1 = (v: number) => `${v.toFixed(1)}%`;

/** pandas nlargest(n, col) with keep="first": stable sort by value desc. Returns row indices. */
export function topByPosition(traders: readonly Trader[], rows: readonly number[], n: number): number[] {
  return [...rows].sort((a, b) => traders[b].position - traders[a].position || a - b).slice(0, n);
}

/**
 * Root "Total whale stake" → one node per category with volume > 0 (Dash
 * order) → its top 15 wallets by position, plus one remainder leaf holding
 * the rest of the category (branchvalues="total" semantics: a category's
 * area is its full volume). Leaf values sum exactly to their category value.
 * `hidden` only flags nodes (for dimming); it never changes values.
 */
export function buildTreemapTree(t: TradersData, hidden?: ReadonlySet<TraderCategory>): TreemapRoot {
  const names = categoryNames(t.thresholds);
  let total = 0;
  for (const tr of t.traders) total += tr.position;
  const share = (v: number) => (total > 0 ? (v / total) * 100 : 0);

  const children: TreemapCategory[] = [];
  for (const cat of CATEGORY_ORDER) {
    const members: number[] = [];
    t.traders.forEach((tr, i) => {
      if (tr.category === cat) members.push(i);
    });
    let catVol = 0;
    for (const i of members) catVol += t.traders[i].position;
    if (!(catVol > 0)) continue;

    const isHidden = hidden?.has(cat) ?? false;
    const catShare = (v: number) => (catVol > 0 ? (v / catVol) * 100 : 0);
    const top = topByPosition(t.traders, members, TOP_N);
    const topRows = new Set(top);

    const leaves: TreemapLeaf[] = [];
    let topVol = 0;
    for (const row of top) {
      const tr = t.traders[row];
      topVol += tr.position;
      leaves.push({
        kind: "wallet",
        key: `w:${row}`,
        row,
        id: tr.id,
        category: cat,
        value: tr.position,
        pctOfTotal: share(tr.position),
        pctOfCategory: catShare(tr.position),
        priorBets: tr.priorBets,
        concentration: tr.concentration,
        label: `${tr.id.slice(0, 6)}… (${pct1(share(tr.position))})`,
        hidden: isHidden,
      });
    }

    const rest = members.filter((i) => !topRows.has(i));
    if (rest.length > 0) {
      // Remainder = category volume − top-15 volume, so leaves sum to the category exactly.
      const restVol = Math.max(0, catVol - topVol);
      if (restVol > 0) {
        leaves.push({
          kind: "rest",
          key: `rest:${cat}`,
          category: cat,
          value: restVol,
          count: rest.length,
          rows: rest,
          pctOfTotal: share(restVol),
          pctOfCategory: catShare(restVol),
          label: `${rest.length} more wallet${rest.length === 1 ? "" : "s"} · ${formatUSD(restVol)}`,
          hidden: isHidden,
        });
      }
    }

    children.push({
      kind: "category",
      key: `cat:${cat}`,
      category: cat,
      name: names[cat].label,
      short: names[cat].short,
      label: `${names[cat].label} (${pct1(share(catVol))})`,
      value: catVol,
      count: members.length,
      pctOfTotal: share(catVol),
      hidden: isHidden,
      children: leaves,
    });
  }

  return {
    kind: "root",
    key: "root",
    label: "Total whale stake",
    value: total,
    count: t.traders.length,
    children,
  };
}

/** Row indices folded into each category's remainder tile. */
export function restRowsByCategory(tree: TreemapRoot): Map<TraderCategory, Set<number>> {
  const out = new Map<TraderCategory, Set<number>>();
  for (const c of tree.children) {
    for (const leaf of c.children) if (leaf.kind === "rest") out.set(c.category, new Set(leaf.rows));
  }
  return out;
}

/** Every row index per wallet address (a wallet can hold several whale positions). */
export function rowsByWallet(t: TradersData): Map<string, number[]> {
  const out = new Map<string, number[]>();
  t.traders.forEach((tr, i) => {
    const list = out.get(tr.id);
    if (list) list.push(i);
    else out.set(tr.id, [i]);
  });
  return out;
}

/* ── Constellation helpers (fig_threat_constellation) ──────────────── */

/** Marker diameter in px: 8 + 40·√(position / max), with the 40 optionally scaled to the chart. */
export function markerDiameter(position: number, maxPosition: number, spread = 40): number {
  const f = maxPosition > 0 ? Math.sqrt(Math.max(0, position) / maxPosition) : 0;
  return 8 + spread * f;
}

/** Guide-ring radius (data units) for a risk score: base_radius = 110 − risk_score. */
export const ringRadius = (risk: number) => 110 - risk;

/* ── Linked-view state (shared by both charts) ─────────────────────── */

export type FocusSource = "constellation" | "treemap" | "legend";

export type Focus =
  | { kind: "wallet"; row: number; source: FocusSource }
  | { kind: "category"; category: TraderCategory; source: FocusSource }
  | { kind: "rest"; category: TraderCategory; source: FocusSource }
  | null;

export interface Pin {
  /** Row index into TradersData.traders. */
  row: number;
  source: "constellation" | "treemap";
}

export function sameFocus(a: Focus, b: Focus): boolean {
  if (a === b) return true;
  if (!a || !b || a.kind !== b.kind || a.source !== b.source) return false;
  if (a.kind === "wallet" && b.kind === "wallet") return a.row === b.row;
  if (a.kind !== "wallet" && b.kind !== "wallet") return a.category === b.category;
  return false;
}
