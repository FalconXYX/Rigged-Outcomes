// Data contract produced by ../export.py. Keep in sync with that script.
//
// TIME CONVENTION: every `t` / `start` / `end` is integer epoch SECONDS of the
// US/Eastern wall clock read as if it were UTC. Build Dates with
// `new Date(t * 1000)` and format/scale them with UTC APIs (d3.scaleUtc,
// d3.utcFormat) so every viewer sees Eastern time regardless of their locale.

export interface MarketEvent {
  t: number;
  label: string;
}

export interface MarketIndexEntry {
  name: string; // route key, e.g. "usiran"
  title: string;
  slug: string;
  correctOutcome: string | null;
  start: number; // first trade
  end: number; // last trade
  totalVolume: number;
  positions: number; // aggregated user/outcome/side rows in the clean CSV
  /** Timeline whale count (after the IQR time-outlier filter), as in "Whale bettors". */
  whaleCount: number;
  /** Whales scored on the Traders tab (= traders.totalWhales); flaggedAccounts and pctSuspicious use this set. */
  traderWhales: number | null;
  flaggedAccounts: number | null;
  pctSuspicious: number | null;
  events: MarketEvent[];
  preview: MarketPreview;

  // ── Ranking fields ("how suspicious does this market look") ──────────────
  /**
   * Σ net profit (USD) of the timeline whales (the dots on the chart, after the
   * IQR filter) whose wallet is flagged (is_suspicious on ANY of its rows) in
   * this market's insiders set. Null without insider data.
   */
  flaggedWinnings: number | null;
  /**
   * flaggedWinnings / timeline.totalMarketWinnings × 100 — "flagged wallets took
   * X% of the winnings". Null without insider data or when winnings are 0.
   */
  flaggedWinningsPct: number | null;
  /** Share (%) of whale capital (market_position) held by flagged accounts. Same number as pctSuspicious. */
  flaggedWhaleCapitalPct: number | null;
  /** Number of named wallets (markets.json highlightedUsers), 0 when none. */
  namedWallets: number;
}

/** Downsampled series for the home-page card preview (display only, never for stats). */
export interface MarketPreview {
  /** [t, odds %] — median per equal-width time bin over [start, end]. */
  odds: [number, number][];
  /** [binStart, crowd USD] — crowd hourly volume summed per bin. */
  crowd: [number, number][];
  /** Biggest whale positions + every highlighted user; sorted small → big. */
  whales: { t: number; v: number; hi: boolean }[];
  /** Whale capital (USD) per trader category on the Traders tab; null without insider data. */
  flagMix: Record<TraderCategory, number> | null;
}

export interface MarketIndex {
  markets: MarketIndexEntry[];
}

export interface Whale {
  id: string; // wallet address
  label: string | null; // name from markets.json highlightedUsers
  t: number; // first_trade
  dollar: number; // dollar_amount (net cost)
  display: number; // y position + size basis (contracts*odds for fully hedged highlighted users)
  odds: number; // avg_odds (0–1)
  profit: number; // net profit if this position won, else 0
  contracts: number;
  outcome: string;
  hedged: boolean;
}

export interface TimeRange {
  label: string;
  start: number;
  end: number;
}

export interface TimelineData {
  threshold: number; // whale threshold in USD
  totalVolume: number; // sum of every position's dollar_amount (unfiltered stat)
  totalMarketWinnings: number; // sum of net profit over every winning position
  whales: Whale[];
  /** Crowd (non-whale) volume per hour; avg = 6-row centered rolling mean. */
  crowdHourly: { t: number[]; vol: number[]; avg: number[] };
  /** Event probability in percent (0–100), per minute, rolling median. */
  odds: { t: number[]; pct: number[] };
  events: MarketEvent[];
  timeRanges: TimeRange[];
  defaultRange: string | null;
  /** Every crowd position (first_trade, dollar_amount) — for range-filtered stats. */
  crowdRows: { t: number[]; d: number[] };
  /** Every winning position (first_trade, net profit) — for range-filtered stats. */
  winnerRows: { t: number[]; p: number[] };
  span: { start: number; end: number };
  positions: number;
}

export type TraderCategory = "normal" | "newAccount" | "concentration" | "critical";

export interface Trader {
  id: string;
  position: number; // market_position (USD)
  lifetime: number; // lifetime_portfolio (USD)
  priorBets: number;
  concentration: number; // portfolio_concentration, percent 0–100
  isFirstX: boolean; // priorBets <= thresholds.firstX
  isPercent: boolean; // concentration >= thresholds.percentVolume
  suspicious: boolean;
  category: TraderCategory;
  riskScore: number; // 0–100
  /** Constellation coordinates in data units, both within [-130, 130]. */
  x: number;
  y: number;
}

export interface TradersData {
  totalVolume: number;
  suspiciousVolume: number;
  pctSuspicious: number;
  totalWhales: number;
  flaggedAccounts: number;
  thresholds: { firstX: number; percentVolume: number };
  traders: Trader[];
}

export interface MarketData {
  name: string;
  title: string;
  slug: string;
  correctOutcome: string | null;
  timeline: TimelineData;
  traders: TradersData | null;
}
