// Timeline view — port of VisualizeBetTiming.py (Dash) to React + D3.
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type KeyboardEvent as RKeyboardEvent } from "react";
import * as d3 from "d3";
import type { MarketData } from "../../types";
import { Legend, Segmented, Select, StatTile, Switch, type LegendItem } from "../../components/ui";
import { fmtInt, fmtPct, fmtT, fmtUSD } from "../../lib/format";
import { prefersReducedMotion } from "../../lib/useSize";
import { useQuery } from "../../lib/router";
import { computeTimelineStats } from "./stats";
import { TimelineChart } from "./TimelineChart";
import { HOUR, clampDomain, fullDomainOf, sameDomain, type Domain, type SeriesKey, type View, type Visibility } from "./model";
import { ZOOM_MOD, useMedia, useThrottled } from "./hooks";
import { ReplayEngine, useReplaySnapshot } from "./replay";
import { ReplayBar } from "./ReplayBar";
import { TOGGLE_KEYS, readTimelineQuery, timelineQueryPatch, useQueryWriter, type TimelinePrefs } from "./urlState";
import "./timeline.css";

const VIEWS: { value: View; label: string }[] = [
  { value: "whales", label: "Whale timing" },
  { value: "crowd", label: "Crowd volume" },
  { value: "cmp", label: "Comparison" },
];

type Toggles = Omit<Visibility, "odds">;

/**
 * The session's last view / odds / filter choice. A bare market URL (no
 * timeline keys — a home card, the Timeline tab) opens with these, so
 * moving between markets keeps the way you were looking at them; a link that
 * spells out any state starts from the defaults for the rest.
 */
let sessionPrefs: TimelinePrefs | null = null;

export function TimelineView({ market }: { market: MarketData }) {
  // The view reads its state from the URL once, on mount. Its own query
  // writes use replaceState (no hashchange); a real hash navigation that
  // stays on this market (a pasted link, a wallet's "see this bet" link,
  // Back) remounts it so the new query is read.
  const [gen, setGen] = useState(0);
  useEffect(() => {
    const onHash = () => {
      const m = /^#\/m\/([^/?#]+)(?:\/timeline)?(?:\?|$)/.exec(window.location.hash);
      if (!m) return;
      let name = m[1];
      try {
        name = decodeURIComponent(name);
      } catch {
        /* keep raw */
      }
      if (name === market.name) setGen((g) => g + 1);
    };
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, [market.name]);
  return <TimelineInner key={`${market.name}:${gen}`} market={market} />;
}

function TimelineInner({ market }: { market: MarketData }) {
  const tl = market.timeline;
  const ranges = tl.timeRanges;
  const full = useMemo(() => fullDomainOf(tl), [tl]);
  const defaultRange =
    tl.defaultRange && ranges.some((r) => r.label === tl.defaultRange) ? tl.defaultRange : (ranges[0]?.label ?? null);

  const presetDomain = useCallback(
    (label: string | null): Domain => {
      const r = ranges.find((x) => x.label === label);
      if (!r) return full;
      // A preset covering (nearly) everything is the full view, so there is no
      // sliver of zoom-out left over (a scroll there would only show "Reset zoom").
      // Stats still use the preset's exact range.
      if (r.end - r.start >= 0.995 * (full[1] - full[0])) return full;
      const d: Domain = [r.start, r.end];
      return r.end - r.start >= HOUR && r.start >= full[0] && r.end <= full[1] ? d : clampDomain(d, full);
    },
    [ranges, full],
  );

  /* ── initial state: the URL's query, validated (see urlState.ts) ──── */
  const query = useQuery();
  const [init] = useState(() => readTimelineQuery(query, tl, full, presetDomain, defaultRange, sessionPrefs));

  const [view, setView] = useState<View>(init.view);
  const [rangeLabel, setRangeLabel] = useState<string | null>(init.range);
  const [showOdds, setShowOdds] = useState(init.odds);
  const [filterStats, setFilterStats] = useState(init.filter);
  const [toggles, setToggles] = useState<Toggles>(() => {
    const t: Toggles = { whales: true, highlighted: true, crowd: true, crowdRaw: true, events: true, threshold: true };
    for (const k of init.hidden) t[k] = false;
    return t;
  });
  const vis: Visibility = useMemo(() => ({ ...toggles, odds: showOdds }), [toggles, showOdds]);
  const [pinned, setPinned] = useState<number | null>(init.pin);

  useEffect(() => {
    sessionPrefs = { view, odds: showOdds, filter: filterStats };
  }, [view, showOdds, filterStats]);

  const preset = ranges.find((r) => r.label === rangeLabel) ?? null;
  const base = useMemo(() => presetDomain(rangeLabel), [presetDomain, rangeLabel]);
  const baseRef = useRef(base);
  baseRef.current = base;

  /* ── x-domain: single source of truth, animated between presets ─── */
  const [domain, setDomain] = useState<Domain>(init.domain ?? base);
  const [target, setTarget] = useState<Domain | null>(null);
  const domainRef = useRef(domain);
  domainRef.current = domain;
  const anim = useRef<d3.Timer | null>(null);
  useEffect(() => () => anim.current?.stop(), []);

  const userDomain = useCallback((d: Domain) => {
    anim.current?.stop();
    anim.current = null;
    setTarget(null);
    setDomain(d);
  }, []);

  const animateTo = useCallback((to: Domain) => {
    anim.current?.stop();
    anim.current = null;
    const from = domainRef.current;
    if (sameDomain(from, to, 0) || prefersReducedMotion()) {
      setTarget(null);
      setDomain(to);
      return;
    }
    setTarget(to);
    const c0 = (from[0] + from[1]) / 2;
    const c1 = (to[0] + to[1]) / 2;
    const lw0 = Math.log(from[1] - from[0]);
    const lw1 = Math.log(to[1] - to[0]);
    const dur = 460;
    const timer = d3.timer((elapsed) => {
      const p = Math.min(1, elapsed / dur);
      if (p >= 1) {
        timer.stop();
        anim.current = null;
        setTarget(null);
        setDomain(to);
        return;
      }
      const e = d3.easeCubicInOut(p);
      const w = Math.exp(lw0 + (lw1 - lw0) * e);
      const c = c0 + (c1 - c0) * e;
      setDomain([c - w / 2, c + w / 2]);
    });
    anim.current = timer;
  }, []);

  const resetZoom = useCallback(() => animateTo(baseRef.current), [animateTo]);
  const onRange = (label: string) => {
    setRangeLabel(label);
    animateTo(presetDomain(label));
  };

  const heading = target ?? domain;
  const isZoomed = !sameDomain(heading, base, 1);

  /* ── replay engine (one per market; the chart and the bar subscribe) ── */
  const [replay] = useState(() => new ReplayEngine(init.domain ?? presetDomain(init.range), eventTimes(tl, !init.hidden.includes("events")), init.t));
  useEffect(() => () => replay.destroy(), [replay]);
  // Zooming / brushing / a new preset while playing pauses (setDomain does).
  useLayoutEffect(() => replay.setDomain(domain), [replay, domain]);
  useEffect(() => replay.setEvents(eventTimes(tl, toggles.events)), [replay, tl, toggles.events]);
  const rp = useReplaySnapshot(replay);
  const replayT = rp.active && !rp.playing ? rp.t : null;

  /* ── URL: the query mirrors what is on screen (throttled, replaceState) ── */
  const { write: writeQuery, flush: flushQuery } = useQueryWriter(150);
  const hiddenKeys = TOGGLE_KEYS.filter((k) => !toggles[k]);
  const queryPatch = JSON.stringify(
    timelineQueryPatch(
      { view, odds: showOdds, filter: filterStats, range: rangeLabel, defaultRange, domain: heading, base, pin: pinned, hidden: hiddenKeys, t: replayT },
      tl,
    ),
  );
  useEffect(() => {
    writeQuery(JSON.parse(queryPatch) as Record<string, string | null>);
  }, [queryPatch, writeQuery]);

  /* ── keyboard, anywhere in the card: Space plays, ← → nudge, Esc stops ── */
  const onCardKey = (e: RKeyboardEvent<HTMLDivElement>) => {
    if (e.defaultPrevented || e.altKey || e.metaKey || e.ctrlKey) return;
    const el = e.target as HTMLElement;
    const tag = el.tagName;
    const scrubber = el instanceof HTMLInputElement && el.type === "range";
    const typing = !scrubber && (tag === "INPUT" || tag === "SELECT" || tag === "TEXTAREA" || el.isContentEditable);
    if (e.key === " ") {
      if (typing || tag === "BUTTON" || tag === "A") return;
      e.preventDefault();
      replay.toggle();
    } else if (e.key === "Escape") {
      // A pinned card closes first (the window listener below); the next Esc stops the replay.
      if (pinned != null || !replay.active) return;
      e.preventDefault();
      replay.reset();
    } else if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
      if (typing || scrubber || !replay.active) return;
      e.preventDefault();
      replay.nudge(e.key === "ArrowRight" ? 1 : -1, e.shiftKey);
    }
  };

  /* ── pinning ─────────────────────────────────────────────────────── */
  useEffect(() => {
    if (pinned == null) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setPinned(null);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [pinned]);

  useEffect(() => {
    if (pinned == null) return;
    const w = tl.whales[pinned];
    if (!w || view === "crowd" || (w.label ? !toggles.highlighted : !toggles.whales)) setPinned(null);
  }, [pinned, view, toggles, tl]);

  /* ── stats (follow the visible x-domain when filtering) ──────────── */
  // While a preset animates, stats jump straight to where it is heading.
  const statsDomain = useThrottled(target ?? domain, 140, sameDomain);
  const statsZoomed = !sameDomain(statsDomain, base, 1);
  const statsRange = useMemo(() => {
    if (!filterStats) return null;
    if (!statsZoomed) return preset ? { start: preset.start, end: preset.end } : null;
    return { start: Math.round(statsDomain[0]), end: Math.round(statsDomain[1]) };
  }, [filterStats, statsZoomed, preset, statsDomain]);
  const stats = useMemo(() => computeTimelineStats(tl, statsRange), [tl, statsRange]);

  /* ── legend ──────────────────────────────────────────────────────── */
  const hasHi = useMemo(() => tl.whales.some((w) => w.label), [tl]);
  const hasOdds = tl.odds.t.length > 1;
  const hasEvents = tl.events.length > 0;
  const legend: LegendItem[] = [];
  if (view !== "crowd") {
    legend.push({ key: "whales", label: "Whales", color: "var(--c-whale)", shape: "dot", active: toggles.whales });
    if (hasHi)
      legend.push({ key: "highlighted", label: "Highlighted users", color: "var(--c-highlight)", shape: "dot", active: toggles.highlighted });
  }
  if (view !== "whales")
    legend.push({
      key: "crowd",
      label: view === "crowd" ? "Crowd volume, 6-hr avg" : "Crowd volume / hr",
      color: "var(--c-crowd)",
      shape: "line",
      active: toggles.crowd,
    });
  if (view === "crowd")
    legend.push({
      key: "crowdRaw",
      label: "Hourly volume",
      color: "color-mix(in oklab, var(--c-crowd) 35%, transparent)",
      shape: "rect",
      active: toggles.crowdRaw,
    });
  if (hasOdds) legend.push({ key: "odds", label: "Odds (% chance)", color: "var(--c-odds)", shape: "line", active: showOdds });
  if (hasEvents) legend.push({ key: "events", label: "Events", color: "var(--c-event)", shape: "dash", active: toggles.events });
  if (view === "whales")
    legend.push({ key: "threshold", label: "Whale threshold", color: "var(--text-3)", shape: "dash", active: toggles.threshold });

  const onToggle = (key: string) => {
    if (key === "odds") setShowOdds((v) => !v);
    else setToggles((t) => ({ ...t, [key as Exclude<SeriesKey, "odds">]: !t[key as Exclude<SeriesKey, "odds">] }));
  };

  const coarse = useMedia("(hover: none)");
  // The one hint for the chart. Times say ET where they are read (tooltips,
  // the replay clock, the caption), so there is no "All times Eastern" line.
  const hint = coarse
    ? "Drag the strip below to zoom · tap a dot for details"
    : `${ZOOM_MOD} + scroll to zoom · drag to pan · double-click to reset`;

  // Shown only while "Filter stats" is on: what the strips below cover.
  const caption = !filterStats
    ? null
    : statsRange
      ? `Stats for ${fmtT(statsRange.start).replace(/ ET$/, "")} – ${fmtT(statsRange.end)}`
      : "Stats for the whole market · zoom in to filter";

  return (
    <section className="tl" aria-labelledby="tl-heading">
      <div className="tl-head">
        <h2 id="tl-heading" className="section-title">
          Timeline of contract activity
        </h2>
        <p className="tl-hint">{hint}</p>
      </div>

      <div className="card tl-card" tabIndex={-1} onKeyDown={onCardKey}>
        <div className="tl-controls" role="group" aria-label="Chart controls">
          <div className="tl-ctrl tl-ctrl-view">
            <Segmented value={view} options={VIEWS} onChange={setView} label="Chart view" />
          </div>
          {ranges.length > 0 && (
            <div className="tl-ctrl tl-ctrl-range">
              <Select
                value={rangeLabel ?? ""}
                options={ranges.map((r) => ({ value: r.label, label: r.label }))}
                onChange={onRange}
                label="Time range"
              />
            </div>
          )}
          <div className="tl-switches">
            {hasOdds && <Switch checked={showOdds} onChange={setShowOdds} label="Odds axis" />}
            <Switch checked={filterStats} onChange={setFilterStats} label="Filter stats" />
          </div>
          <div className="tl-ctrl-end">
            <div className={`tl-reset${isZoomed ? " show" : ""}`} aria-hidden={!isZoomed}>
              <button type="button" className="btn btn-ghost btn-sm" onClick={resetZoom} title="Reset zoom (or double-click the chart)" disabled={!isZoomed}>
                Reset zoom
              </button>
            </div>
            <CopyLink beforeCopy={flushQuery} />
          </div>
        </div>

        <div className="tl-legend-row">
          <Legend items={legend} onToggle={onToggle} />
        </div>

        <ReplayBar engine={replay} />

        <TimelineChart
          tl={tl}
          view={view}
          vis={vis}
          domain={domain}
          fitDomain={heading}
          full={full}
          pinned={pinned}
          onPin={setPinned}
          onDomain={userDomain}
          onReset={resetZoom}
          replay={replay}
        />
      </div>

      <div className="tl-stats">
        {caption && (
          <p className="tl-caption" aria-live="polite">
            {caption}
          </p>
        )}
        <div className="tl-stat-groups">
          <section className="tl-group tl-group-all" aria-label="Overall">
            <h3 className="label tl-group-head">Overall</h3>
            <div className="stat-strip accent-rule">
              <StatTile label="Total volume" value={<span className="tabular">{fmtUSD(stats.totalVolume)}</span>} hint="Sum of every position's cost" />
            </div>
          </section>
          <section className="tl-group tl-group-whale" aria-label="Whales">
            <h3 className="label tl-group-head">Whales</h3>
            <div className="stat-strip accent-rule">
              <StatTile
                label="Whale threshold"
                value={<span className="tabular">≥ {fmtUSD(stats.threshold)}</span>}
                hint="Winning positions at or above this size count as whales"
              />
              <StatTile label="Whale volume" value={<span className="tabular">{fmtUSD(stats.whaleVolume)}</span>} />
              <StatTile label="Whale bettors" value={<span className="tabular">{fmtInt(stats.whaleCount)}</span>} />
              <StatTile label="Whale share" value={<span className="tabular">{fmtPct(stats.whaleSharePct)}</span>} hint="Whale volume ÷ total volume" />
              <StatTile label="Whale winnings" value={<span className="tabular">{fmtUSD(stats.whaleWinnings)}</span>} hint="Net profit taken home by whales" />
              <StatTile
                label="Whale win / vol"
                value={<span className="tabular">{fmtPct(stats.whaleWinPct)}</span>}
                hint="Whale winnings ÷ net profit of every winning position"
              />
            </div>
          </section>
          <section className="tl-group tl-group-crowd" aria-label="Crowd">
            <h3 className="label tl-group-head">Crowd</h3>
            <div className="stat-strip accent-rule">
              <StatTile label="Crowd volume" value={<span className="tabular">{fmtUSD(stats.crowdVolume)}</span>} />
              <StatTile label="Crowd bettors" value={<span className="tabular">{fmtInt(stats.crowdCount)}</span>} />
              <StatTile label="Crowd avg bet" value={<span className="tabular">{fmtUSD(stats.crowdAvgBet)}</span>} />
            </div>
          </section>
        </div>
      </div>
    </section>
  );
}

/** Event times the replay should rest on (none while the Events series is hidden). */
function eventTimes(tl: MarketData["timeline"], shown: boolean): number[] {
  return shown ? tl.events.map((e) => e.t) : [];
}

/** Copies the page URL — which carries the whole view state — with a check-mark confirmation. */
function CopyLink({ beforeCopy }: { beforeCopy: () => void }) {
  const [copied, setCopied] = useState(false);
  const timer = useRef(0);
  useEffect(() => () => window.clearTimeout(timer.current), []);

  const copy = async () => {
    beforeCopy();
    const url = window.location.href;
    try {
      if (navigator.clipboard?.writeText) await navigator.clipboard.writeText(url);
      else {
        const ta = document.createElement("textarea");
        ta.value = url;
        ta.setAttribute("readonly", "");
        ta.style.position = "fixed";
        ta.style.opacity = "0";
        document.body.appendChild(ta);
        ta.select();
        document.execCommand("copy");
        ta.remove();
      }
      setCopied(true);
      window.clearTimeout(timer.current);
      timer.current = window.setTimeout(() => setCopied(false), 1800);
    } catch {
      setCopied(false);
    }
  };

  return (
    <button
      type="button"
      className={`icon-btn sm tl-copy${copied ? " on" : ""}`}
      onClick={copy}
      aria-label="Copy a link to this view"
      title="Copy a link to this exact view (range, zoom, pinned wallet, toggles)"
    >
      {copied ? (
        <svg width="14" height="14" viewBox="0 0 14 14" aria-hidden>
          <path d="M3 7.4l2.6 2.6L11 4.6" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      ) : (
        <svg width="14" height="14" viewBox="0 0 14 14" aria-hidden>
          <path
            d="M6 8.2a2.6 2.6 0 0 0 3.7 0l1.9-1.9a2.6 2.6 0 0 0-3.7-3.7l-.9.9M8 5.8a2.6 2.6 0 0 0-3.7 0L2.4 7.7a2.6 2.6 0 0 0 3.7 3.7l.9-.9"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.5"
            strokeLinecap="round"
          />
        </svg>
      )}
      <span className="visually-hidden" aria-live="polite">
        {copied ? "Link copied" : ""}
      </span>
    </button>
  );
}
