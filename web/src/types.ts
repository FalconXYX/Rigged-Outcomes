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

/**
 * A timeline marker added on the page (by hand for now, by a model later).
 * Not saved yet: it lasts until the page reloads, until the database exists.
 * `t` follows the time convention above. Markers from markets.json
 * `importantDates` arrive in timeline.events.
 */
export interface AddedEvent {
  id: string;
  t: number;
  label: string;
  source: "manual" | "ai";
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

/* ── Closed list (watchlist/upsets.py → data/closed.json) ─────────────────── */

export interface ClosedHeadline {
  title: string;
  source: string;
  published: string | null;
  url: string;
}

/** One closed market on the Watchlist page (watchlist/upsets.py): checked by a model from its text and the news, no trading data. */
export interface ClosedPick {
  condition_id: string;
  question: string;
  event_title: string;
  url: string;
  /** the market's own slug, as markets.json takes it */
  slug: string;
  type: string | null;
  winner: string;
  closed: string;
  verdict: "potential case" | "weak" | "not a case";
  what_happened: string;
  public_at: string | null;
  surprise: "yes" | "somewhat" | "unknown" | "no";
  who_knew_first: string;
  public_signals: "none" | "some" | "unknown" | "clear";
  reason: string;
  headlines: ClosedHeadline[];
  same_event: string[];
  /** YYYY-MM-DD the market was first on an open card ("On the open list from Nov 9"); absent when it never was */
  on_open_list_from?: string;
  /** the shared vocabulary (watchlist/kinds.py): the market's kind and its "who decides" group; absent when the open
   * list's rules leave such a market out or cannot place it */
  kind?: string;
  group?: 1 | 2 | 3;
  /** the "who would know first" sentence of the open card the market was on, when it was on one */
  who_knows?: string;
}

export interface ClosedDay {
  day: string;
  closed_markets: number;
  /** left after the filters (games, prices, weather, elections, public counts, nothing happened, ladders, same news) */
  candidates: number;
  /** of those, the ones the model checked */
  checked: number;
  verdicts: Partial<Record<ClosedPick["verdict"], number>>;
  /** why the rest were left out, by reason ("could not be checked" is one): checked + these = closed_markets */
  dropped: Record<string, number>;
  picks: ClosedPick[];
}

export interface ClosedData {
  generated_at: string;
  partial_day: string;
  rules: { min_volume: number; per_day: number; at_least: number; news_days_before: number; prompt_version: string };
  days: ClosedDay[];
}

/* ── Open list ("Open now": watchlist/kinds.py vocabulary → data/open.json) ── */
// Markets still open where a few people will know the answer before the public, and this week's
// news shows them moving toward it. No prices, odds, volumes, trades or wallets anywhere in it.
// Dates are YYYY-MM-DD (UTC); times are ISO-8601 UTC.

/** A headline as Google News gave it. A report its source dated but did not time is stamped 07:00 or 08:00 UTC exactly. */
export interface OpenHeadline {
  title: string;
  source: string;
  published: string | null;
  url: string;
  /** a step: a person approved it (rules.steps_by "person"), or both step readings marked it ("model") */
  step: boolean;
  /** the step on the card face (Details labels it "On the card") */
  on_card: boolean;
}

/** The card's one timing line: "Could happen any day until Dec 31" (· "no new step in the news since Nov 6"), or "Answer comes out Nov 14, at the Grand Final". */
export interface OpenTiming {
  kind: "any_day" | "dated";
  /** any-day: the nearest deadline Polymarket asks about, as its question words it */
  until: string | null;
  /** a gone-quiet card: the day of its latest step; else null */
  last_step: string | null;
  /** dated: the day the answer comes out */
  reveal: string | null;
  /** dated: where or how it comes out ("at the Grand Final"), when known */
  reveal_note: string | null;
}

/** "Polymarket asks" in Details. */
export interface OpenPolymarket {
  /** Polymarket's own question for the nearest date */
  question: string;
  /** the other dates the same question is asked for, soonest first ("It also asks about Jan 31, Feb 28 ...") */
  other_dates: string[];
  url: string;
}

export interface OpenCard {
  /** the story key: the same from day to day */
  id: string;
  /** the small label: "New today", "Listed <listed_since>", "Back on the list" */
  label: "new" | "listed" | "back";
  /** the day the current stay on the list began ("On the list since Nov 13") */
  listed_since: string;
  /** why it holds its place: a new step (places 1-3), the 3-day stay, gone quiet (place 4), dated (place 5) */
  place: "new step" | "stays" | "gone quiet" | "dated";
  /** watchlist/kinds.py KINDS key, e.g. "us_official", "jury_prize" */
  kind: string;
  /** who decides: 1 a few people who choose the moment, 2 a formal process or several sides, 3 nobody the news can show */
  group: 1 | 2 | 3;
  /** the plain question from the kind's template, names from the market */
  question: string;
  timing: OpenTiming;
  /** the step on the card face; null on a dated card with no news worth showing */
  headline: OpenHeadline | null;
  /** one complete sentence: who will know first (never who might bet) */
  who_knows: string;
  /** "Why this kind is watched": the kind's fixed sentence naming a documented case */
  why_watched: string;
  /** "The other side": a headline that denies or plays down the step, if one was copied (null while a person approves
   * the steps: the model is not asked for one) */
  other_side: OpenHeadline | null;
  polymarket: OpenPolymarket;
  /** Details' right column: the last 14 days, newest first, up to 10 plus every step */
  headlines: OpenHeadline[];
  /** when the "already out" check last ran ("Now · Checked Nov 13, 04:00 UTC: no report yet that it has happened");
   * null when there was nothing to read (no headline in the last 7 days) */
  checked_at: string | null;
  /** the Polymarket events in the card's story, and their event and market slugs (the closed list matches on these) */
  event_ids: string[];
  slugs: string[];
}

export interface OpenDay {
  day: string;
  /** "not updated today": the run failed, yesterday's live cards stay and none are added */
  status: "updated" | "not updated today";
  /** when the list was made */
  run_at: string;
  /** in place order; fewer than 3 when fewer qualify (never padded) */
  cards: OpenCard[];
  /** cards that were not on the day before's list ("3 NEW TODAY") */
  new_today: number;
  /** markets with a new step held back because a headline from the last 7 days may mean the answer is out */
  held_back: number;
  /** stories whose step headline is waiting for a person's approval (not shown until approved) */
  awaiting?: number;
}

export interface OpenData {
  generated_at: string;
  /** shown as the "Trial" pill while the trial runs */
  trial: boolean;
  rules: {
    min_volume: number;
    per_day: number;
    at_least: number;
    live_days: number;
    fresh_days: number;
    stay_days: number;
    dated_days: number;
    held_back_days: number;
    prompt_version: string;
    model: string;
    /** who confirms each step: "person" (the step readings suggest steps and a person approves every card's step
     * before it shows) or "model" (a step shows when both step readings mark it) */
    steps_by?: "person" | "model";
  };
  /** newest first; days[0] is the latest list. Empty until a person has approved a first card */
  days: OpenDay[];
}
