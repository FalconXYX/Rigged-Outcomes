import type { MarketIndexEntry } from "../../types";

/*
 * Home ordering and filters. Both live in the route query so a sorted or
 * filtered home is a shareable link:
 *   #/?sort=volume        rank by a metric (absent = markets.json order)
 *   #/?resolved=yes|no    only markets that resolved that way
 *   #/?flagged=1          only markets with at least one flagged wallet
 * The default view writes no keys at all, keeping `#/` clean: nothing is
 * sorted or filtered until the visitor opens the toolbar and asks for it.
 */

export type SortKey = "default" | "suspicious" | "winnings" | "accounts" | "volume" | "newest";

export const DEFAULT_SORT: SortKey = "default";

export const SORT_OPTIONS: { value: SortKey; label: string }[] = [
  { value: "default", label: "Default order" },
  { value: "suspicious", label: "Most suspicious" },
  { value: "winnings", label: "Flagged winnings" },
  { value: "accounts", label: "Flagged accounts" },
  { value: "volume", label: "Volume" },
  { value: "newest", label: "Newest" },
];

const SORT_KEYS = new Set<string>(SORT_OPTIONS.map((o) => o.value));

const isSortKey = (s: unknown): s is SortKey => typeof s === "string" && SORT_KEYS.has(s);

/** `?sort=` → a sort key; absent or unknown is the default (markets.json order). */
export const parseSort = (s: string | undefined): SortKey => (isSortKey(s) ? s : DEFAULT_SORT);

/* ── Filters ─────────────────────────────────────────────────────────────── */

export type ResolvedFilter = "any" | "yes" | "no";

export interface MarketFilters {
  resolved: ResolvedFilter;
  /** Only markets with at least one flagged wallet. */
  flagged: boolean;
}

export const RESOLVED_OPTIONS: { value: ResolvedFilter; label: string }[] = [
  { value: "any", label: "Any" },
  { value: "yes", label: "Yes" },
  { value: "no", label: "No" },
];

export const parseResolved = (s: string | undefined): ResolvedFilter => (s === "yes" || s === "no" ? s : "any");

export const parseFlagged = (s: string | undefined): boolean => s === "1";

const hasFlagged = (m: MarketIndexEntry) => (m.flaggedAccounts ?? 0) > 0;

export function filterMarkets(markets: MarketIndexEntry[], f: MarketFilters): MarketIndexEntry[] {
  return markets.filter((m) => {
    if (f.resolved !== "any" && (m.correctOutcome ?? "").toLowerCase() !== f.resolved) return false;
    if (f.flagged && !hasFlagged(m)) return false;
    return true;
  });
}

/** True when neither a sort nor a filter is active (the plain home). */
export const isDefaultView = (sort: SortKey, f: MarketFilters) => sort === DEFAULT_SORT && f.resolved === "any" && !f.flagged;

/* ── Sort ────────────────────────────────────────────────────────────────── */

/** "How suspicious": flagged wallets' share of the winnings, else their share of whale capital. */
const suspicion = (m: MarketIndexEntry): number | null => m.flaggedWinningsPct ?? m.pctSuspicious;

/** The number a market is ranked by under `key`; null sorts last (and never under the default). */
function sortValue(m: MarketIndexEntry, key: SortKey): number | null {
  switch (key) {
    case "default":
      return null;
    case "suspicious":
      return suspicion(m);
    case "winnings":
      return m.flaggedWinnings;
    case "accounts":
      return m.flaggedAccounts;
    case "volume":
      return m.totalVolume;
    case "newest":
      return m.end;
  }
}

/** Descending by the sort value; nulls last; ties keep markets.json order (Array.sort is stable). */
export function sortMarkets(markets: MarketIndexEntry[], key: SortKey): MarketIndexEntry[] {
  if (key === "default") return [...markets];
  return [...markets].sort((a, b) => {
    const va = sortValue(a, key);
    const vb = sortValue(b, key);
    if (va == null && vb == null) return 0;
    if (va == null) return 1;
    if (vb == null) return -1;
    return vb - va;
  });
}
