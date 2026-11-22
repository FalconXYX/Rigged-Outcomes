// Pure math for the Insider score tab: the rubric's bands as chart groups,
// the band-grouped treemap tree, the constellation's ring guide and the
// linked-view state of the tab's three charts. The Flagged traders tab keeps
// its own (model.ts); this file only borrows model.ts's category-free helpers.
//
// No enums, namespaces or parameter properties, so it runs under Node's type stripping.

import type { TradersData } from "../../types";
import { TOP_N, formatUSD, topByPosition, type TraderSummary } from "./model";

/* ── Groups: what the charts colour, toggle and pool by ─────────────── */

export interface Group {
  id: string;
  /** Legend / card label, e.g. "Suspicious". */
  label: string;
  /** Compact label for tight treemap headers and legend tags. */
  short: string;
  /** CSS colour of the group's marks. */
  color: string;
  /** The top tier: its marks carry a surface-coloured core (a second channel beside colour). */
  core: boolean;
  /** Counted in the "flagged" totals. */
  flagged: boolean;
}

export interface Grouping {
  /** What one group is called in running text ("band"). */
  noun: string;
  /** Lowest tier first: the draw order and the legend order. */
  groups: readonly Group[];
  byId: ReadonlyMap<string, Group>;
  /** The group of each row of the TradersData the charts are given. */
  rowGroup: readonly string[];
}

/** Group of a row (falls back to the lowest tier, so a stray id never breaks a chart). */
export const groupOf = (g: Grouping, row: number): Group => g.byId.get(g.rowGroup[row]) ?? g.groups[0];

/** Rows per group id (every group present, zero when empty). */
export function countByGroup(g: Grouping): Map<string, number> {
  const out = new Map<string, number>(g.groups.map((x) => [x.id, 0]));
  g.rowGroup.forEach((_, row) => {
    const id = groupOf(g, row).id;
    out.set(id, (out.get(id) ?? 0) + 1);
  });
  return out;
}

/** A group's colour (any CSS colour) mixed into the surface (`mix` 0–1), as the browser paints it. */
export const tint = (color: string, mix: number) => `color-mix(in oklab, ${color} ${Math.round(mix * 100)}%, var(--surface))`;

/* ── Summary (the five stat tiles) ──────────────────────────────────── */

const roundTo = (x: number, nd: number) => Math.round(x * 10 ** nd) / 10 ** nd;

/** Totals over the given rows; "flagged" is the rows in the grouping's flagged groups. */
export function computeGroupSummary(t: TradersData, g: Grouping): TraderSummary {
  let total = 0;
  let flaggedVol = 0;
  let flagged = 0;
  t.traders.forEach((tr, row) => {
    total += tr.position;
    if (groupOf(g, row).flagged) {
      flaggedVol += tr.position;
      flagged += 1;
    }
  });
  const totalVolume = roundTo(total, 2);
  const suspiciousVolume = roundTo(flaggedVol, 2);
  return {
    totalVolume,
    suspiciousVolume,
    pctSuspicious: roundTo(totalVolume > 0 ? (suspiciousVolume / totalVolume) * 100 : 0, 4),
    totalWhales: t.traders.length,
    flaggedAccounts: flagged,
  };
}

/* ── Treemap hierarchy, grouped by band ─────────────────────────────── */

export interface TreemapWallet {
  kind: "wallet";
  /** `w:<row>`: wallets can hold several whale positions, so rows (not addresses) are the identity. */
  key: string;
  /** Index into TradersData.traders. */
  row: number;
  id: string;
  /** Group id. */
  category: string;
  value: number;
  /** Percent of the total whale stake. */
  pctOfTotal: number;
  /** Percent of this group's volume. */
  pctOfCategory: number;
  priorBets: number;
  concentration: number;
  /** "0x1234… (x.x%)": share of the total. */
  label: string;
  hidden: boolean;
}

/** Everything beyond the top 15, so each group's area stays equal to its full volume. */
export interface TreemapRest {
  kind: "rest";
  key: string;
  category: string;
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
  category: string;
  /** Group name without the share, e.g. "Suspicious". */
  name: string;
  short: string;
  /** "<Group> (x.x%)" of the total. */
  label: string;
  value: number;
  count: number;
  pctOfTotal: number;
  /** Group toggled off in the legend: keep its (honest) area, render dimmed. */
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

const pct1 = (v: number) => `${v.toFixed(1)}%`;

/**
 * Root "Total whale stake" → one node per group with volume > 0 (the
 * grouping's order) → its top 15 wallets by position, plus one remainder leaf
 * holding the rest of the group (a group's area is its full volume). Leaf
 * values sum exactly to their group value. `hidden` only flags nodes (for
 * dimming); it never changes values.
 */
export function buildGroupTreemap(t: TradersData, g: Grouping, hidden?: ReadonlySet<string>): TreemapRoot {
  let total = 0;
  for (const tr of t.traders) total += tr.position;
  const share = (v: number) => (total > 0 ? (v / total) * 100 : 0);

  const children: TreemapCategory[] = [];
  for (const group of g.groups) {
    const cat = group.id;
    const members: number[] = [];
    t.traders.forEach((_, i) => {
      if (groupOf(g, i).id === cat) members.push(i);
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
      name: group.label,
      short: group.short,
      label: `${group.label} (${pct1(share(catVol))})`,
      value: catVol,
      count: members.length,
      pctOfTotal: share(catVol),
      hidden: isHidden,
      children: leaves,
    });
  }

  return { kind: "root", key: "root", label: "Total whale stake", value: total, count: t.traders.length, children };
}

/** Row indices folded into each group's remainder tile. */
export function restRowsByGroup(tree: TreemapRoot): Map<string, Set<number>> {
  const out = new Map<string, Set<number>>();
  for (const c of tree.children) {
    for (const leaf of c.children) if (leaf.kind === "rest") out.set(c.category, new Set(leaf.rows));
  }
  return out;
}

/* ── Constellation guide ───────────────────────────────────────────── */

/** A constellation callout: arrow head in data units, a title and a sub-line, the arrow's colour. */
export interface RingCallout {
  /** Where the arrow points when no `group` is given (or the view does not resolve groups). */
  head: [number, number];
  /** Aim at this group's largest visible wallet instead; the callout hides when the group shows none. */
  group?: string;
  title: string;
  sub: string;
  color: string;
}

/** What the constellation's rings and callouts say (spectrumGuide in spectrum.ts). */
export interface RingGuide {
  /** Ring scores (radius 110 − score); the lowest score is the outer, dashed ring. */
  rings: number[];
  /** Labels on one shared bearing, each at its score's radius, outermost (lowest score) first. */
  labels: { score: number; text: string }[];
  /** Callout at the core (the top tier) and on the outer orbit (the lowest tier). */
  core: RingCallout;
  orbit: RingCallout;
}

/* ── Linked-view state (shared by the tab's three charts) ──────────── */

export type FocusSource = "constellation" | "treemap" | "legend" | "spectrum";

export type Focus =
  | { kind: "wallet"; row: number; source: FocusSource }
  | { kind: "category"; category: string; source: FocusSource }
  | { kind: "rest"; category: string; source: FocusSource }
  | null;

export function sameFocus(a: Focus, b: Focus): boolean {
  if (a === b) return true;
  if (!a || !b || a.kind !== b.kind || a.source !== b.source) return false;
  if (a.kind === "wallet" && b.kind === "wallet") return a.row === b.row;
  if (a.kind !== "wallet" && b.kind !== "wallet") return a.category === b.category;
  return false;
}

export type { Pin } from "./model";
