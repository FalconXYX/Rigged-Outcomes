// Pure stats math for the Timeline view — a line-by-line port of
// `update_stats` in VisualizeBetTiming.py.
//
// Keep this module free of runtime imports and TS-only runtime syntax: it is
// executed directly by Node's type stripping in verification.
import type { TimelineData } from "../../types";

export interface TimelineStats {
  totalVolume: number;
  threshold: number;
  whaleVolume: number;
  crowdVolume: number;
  whaleCount: number;
  whaleSharePct: number;
  whaleWinnings: number;
  whaleWinPct: number;
  crowdCount: number;
  crowdAvgBet: number;
}

/** pandas .sum() skips NaN; JSON nulls stand in for NaN here. */
function num(v: number | null | undefined): number {
  return v == null || !isFinite(v) ? 0 : v;
}

/**
 * range == null → the Dash app's unfiltered numbers.
 * range given  → the Dash app's "Filter stats to time range" numbers, with the
 * same inclusive `start <= first_trade <= end` test (epoch seconds).
 * Every value is returned unrounded.
 */
export function computeTimelineStats(
  tl: TimelineData,
  range: { start: number; end: number } | null,
): TimelineStats {
  const lo = range ? range.start : -Infinity;
  const hi = range ? range.end : Infinity;

  let whaleVolume = 0;
  let whaleWinnings = 0;
  let whaleCount = 0;
  for (const w of tl.whales) {
    if (w.t >= lo && w.t <= hi) {
      whaleVolume += num(w.dollar);
      whaleWinnings += num(w.profit);
      whaleCount += 1;
    }
  }

  let crowdVolume = 0;
  let crowdCount = 0;
  let crowdValued = 0; // non-NaN rows, the denominator pandas .mean() uses
  const ct = tl.crowdRows.t;
  const cd = tl.crowdRows.d;
  for (let i = 0; i < ct.length; i++) {
    const t = ct[i];
    if (t >= lo && t <= hi) {
      crowdCount += 1;
      const d = cd[i];
      if (d != null && isFinite(d)) {
        crowdVolume += d;
        crowdValued += 1;
      }
    }
  }

  let totalVolume: number;
  let marketWinnings: number;
  if (range) {
    totalVolume = whaleVolume + crowdVolume;
    marketWinnings = 0;
    const wt = tl.winnerRows.t;
    const wp = tl.winnerRows.p;
    for (let i = 0; i < wt.length; i++) {
      const t = wt[i];
      if (t >= lo && t <= hi) marketWinnings += num(wp[i]);
    }
  } else {
    totalVolume = num(tl.totalVolume);
    marketWinnings = num(tl.totalMarketWinnings);
  }

  return {
    totalVolume,
    threshold: tl.threshold,
    whaleVolume,
    crowdVolume,
    whaleCount,
    whaleSharePct: totalVolume ? (whaleVolume / totalVolume) * 100 : 0,
    whaleWinnings,
    whaleWinPct: marketWinnings ? (whaleWinnings / marketWinnings) * 100 : 0,
    crowdCount,
    crowdAvgBet: crowdValued ? crowdVolume / crowdValued : 0,
  };
}
