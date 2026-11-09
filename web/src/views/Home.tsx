import { useCallback, useEffect, useMemo, useState, type CSSProperties, type ReactNode } from "react";
import type { MarketIndex, MarketIndexEntry } from "../types";
import type { Async } from "../lib/data";
import { fmtDate, fmtDay, fmtDayYY, fmtInt, fmtPct, fmtT, fmtUSDCompact, smartQuotes, toDate } from "../lib/format";
import { href, setQuery, useQuery } from "../lib/router";
import { prefersReducedMotion } from "../lib/useSize";
import { Button, ErrorNote, Legend, Segmented, Switch, Toolbar, ToolbarGroup, type LegendItem } from "../components/ui";
import { MiniTimeline } from "./home/MiniTimeline";
import { FlagMix } from "./home/FlagMix";
import { Pipeline } from "./home/Pipeline";
import { useReveal } from "./home/useReveal";
import { useFlip } from "./home/useFlip";
import {
  DEFAULT_SORT,
  RESOLVED_OPTIONS,
  SORT_OPTIONS,
  filterMarkets,
  isDefaultView,
  parseFlagged,
  parseResolved,
  parseSort,
  sortMarkets,
  type MarketFilters,
  type ResolvedFilter,
  type SortKey,
} from "./home/ranking";
import "../components/shell.css";
import "./home.css";
import "./home/preview.css";

/* ── Shared market helpers (also used by MarketPage and the wallet pages) ── */

const SHORT_TITLES: Record<string, string> = {
  usiran: "US strikes Iran",
  superbowl: "Super Bowl halftime opener",
  tarrif: "Supreme Court tariff ruling",
  fired: "Kristi Noem out",
  maduro: "Maduro out",
  openai: "OpenAI browser",
  oscars: "Oscars Best Actor",
};

/** Compact market name (loading labels); falls back to the full title. */
/** "Will Saudi Arabia take military action against Yemen on October 18?" → "Saudi Arabia take military action…" */
function compactTitle(title: string): string {
  let t = title.trim().replace(/^will\s+/i, "").replace(/\?$/, "");
  t = t.replace(/\s+(on|by|before|in|at)\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2}(,\s*\d{4})?$/i, "");
  t = t.replace(/\s+(by|before|in)\s+(the\s+end\s+of\s+)?\d{4}$/i, "");
  if (t.length <= 34) return t;
  const cut = t.slice(0, 34);
  return `${cut.slice(0, Math.max(cut.lastIndexOf(" "), 20)).replace(/[\s,;:–-]+$/, "")}…`;
}

export const shortTitle = (m: { name: string; title: string }) => SHORT_TITLES[m.name] ?? compactTitle(m.title);

/**
 * "Jan 19 – Feb 28, 2026" (year once when both ends share it). Across years:
 * "Sep 27, 2025 – Mar 16, 2026", or "Sep 27 ’25 – Mar 16 ’26" when `compact`
 * (cards, where the range shares a row with the Resolved mark).
 */
export function fmtSpan(start: number, end: number, { compact = false }: { compact?: boolean } = {}) {
  const a = toDate(start);
  const b = toDate(end);
  if (a.getUTCFullYear() === b.getUTCFullYear()) return `${fmtDay(a)} – ${fmtDate(b)}`;
  return compact ? `${fmtDayYY(a)} – ${fmtDayYY(b)}` : `${fmtDate(a)} – ${fmtDate(b)}`;
}

export const spanTitle = (start: number, end: number) => `First trade ${fmtT(start)} · last trade ${fmtT(end)}`;

/** "● Resolved · Yes": a key dot (teal Yes / red No, shell.css) and label text. No box. */
export function ResolvedChip({ outcome }: { outcome: string | null }) {
  if (!outcome) return null;
  return (
    <span className="resolved" data-outcome={outcome.toLowerCase()} title={`This market resolved ${outcome}`}>
      <span className="key-dot" aria-hidden />
      Resolved · <b>{outcome}</b>
    </span>
  );
}

/* ── Hero: copy on the left, the pipeline panel on the right ───────────── */

/** Smooth-scrolls to the Markets section and parks focus on its title. */
function exploreMarkets() {
  const section = document.getElementById("markets");
  if (!section) return;
  section.scrollIntoView({ behavior: prefersReducedMotion() ? "auto" : "smooth", block: "start" });
  document.getElementById("markets-title")?.focus({ preventScroll: true });
}

/** The dataset figures live in the pipeline panel beside the copy, not here too. */
function Hero({ markets }: { markets?: MarketIndexEntry[] }) {
  return (
    <section className="hero" aria-labelledby="hero-title">
      <div className="container hero-grid">
        <div className="hero-copy">
          <p className="eyebrow">Insider-trading signals · Polymarket</p>
          <h1 id="hero-title" className="hero-title">
            <span className="gradient-text">Rigged Outcomes</span>
          </h1>
          <p className="hero-lede lede">
            Detects potential insider trading on Polymarket prediction markets by analyzing who traded profitably before
            major outcomes occurred.
          </p>
          <Button variant="solid" className="hero-cta" onClick={exploreMarkets}>
            Explore markets ↓
          </Button>
        </div>
        <Pipeline markets={markets} />
      </div>
    </section>
  );
}

/* ── Market cards ───────────────────────────────────────────────────────── */

function Figure({ label, value, keyColor }: { label: string; value: ReactNode; keyColor?: string }) {
  return (
    <div className="mc-fig">
      <dt className="label">
        {keyColor && <span className="key-dot" style={{ "--key": keyColor } as CSSProperties} aria-hidden />}
        {label}
      </dt>
      <dd className="tabular">{value}</dd>
    </div>
  );
}

/**
 * The whole card links to the timeline through the title link's stretched
 * ::after; the Flagged-traders action sits above that overlay as its own link
 * (never an <a> inside an <a>). "Timeline →" is the same destination as the
 * title, so it is a visual affordance only.
 */
function MarketCard({
  m,
  i,
  animate,
  itemRef,
}: {
  m: MarketIndexEntry;
  i: number;
  animate: boolean;
  itemRef: (el: HTMLLIElement | null) => void;
}) {
  const [ref, phase] = useReveal<HTMLElement>(animate);
  return (
    <li ref={itemRef} className="mc-item" style={{ "--i": i } as CSSProperties}>
      <article ref={ref} className="card market-card" data-phase={phase} aria-labelledby={`mc-${m.name}-title`}>
        <div className="mc-top label">
          <ResolvedChip outcome={m.correctOutcome} />
          <span className="mc-dates tabular" title={spanTitle(m.start, m.end)}>
            {fmtSpan(m.start, m.end, { compact: true })}
          </span>
        </div>
        <h3 className="mc-title" id={`mc-${m.name}-title`}>
          <a className="mc-link" href={href.market(m.name)}>
            {smartQuotes(m.title)}
          </a>
        </h3>
        <MiniTimeline m={m} />
        <FlagMix mix={m.preview.flagMix} />
        <dl className="mc-figs">
          <Figure label="Volume" value={fmtUSDCompact(m.totalVolume)} />
          {/* The Traders tab's whale set: Flagged accounts and Flagged capital are computed over it. */}
          <Figure label="Whales" value={fmtInt(m.traderWhales ?? m.whaleCount)} keyColor="var(--c-whale)" />
          <Figure label="Flagged accounts" value={fmtInt(m.flaggedAccounts)} keyColor="var(--c-critical)" />
          {/* Same metric, label and key as the Watch page's "Flagged capital" (export: flaggedWhaleCapitalPct). */}
          <Figure label="Flagged capital" value={fmtPct(m.pctSuspicious)} keyColor="var(--c-critical)" />
        </dl>
        <div className="mc-foot">
          <span className="mc-act" aria-hidden>
            Timeline →
          </span>
          <a className="mc-act mc-act-alt" href={href.market(m.name, "traders")}>
            Flagged traders<span className="visually-hidden">: {shortTitle(m)}</span> →
          </a>
        </div>
      </article>
    </li>
  );
}

/** The one key for every card preview (marks only; the series palette). */
const PREVIEW_KEY: LegendItem[] = [
  { key: "whale", label: "Whale bet", color: "var(--c-whale)", shape: "dot" },
  { key: "hi", label: "Highlighted user", color: "var(--c-highlight)", shape: "dot" },
  { key: "odds", label: "Odds", color: "var(--c-odds)", shape: "line" },
  { key: "crowd", label: "Crowd volume", color: "var(--c-crowd)", shape: "rect" },
  { key: "event", label: "Event", color: "var(--c-event)", shape: "dash" },
];

function MethodCard({ i, itemRef }: { i: number; itemRef: (el: HTMLLIElement | null) => void }) {
  return (
    <li ref={itemRef} className="mc-item" style={{ "--i": i } as CSSProperties}>
      <aside className="card market-card method-card" aria-labelledby="method-title">
        <h3 id="method-title" className="mc-title">
          How an account gets flagged
        </h3>
        <ul className="method-list">
          <li>
            <b>Whales</b> are winning positions worth at least 0.1% of the market’s volume (clamped to $1K–$5K).
          </li>
          <li>
            A whale is <b>flagged</b> if its wallet had 10 or fewer prior trades, or put 80%+ of its lifetime volume
            here.
          </li>
          <li>
            <b>Flagged capital</b> is the flagged accounts’ share of whale capital.
          </li>
        </ul>
        <p className="method-caveat">Flags are statistical signals, not proof of wrongdoing.</p>
      </aside>
    </li>
  );
}

function SkeletonCard({ i }: { i: number }) {
  return (
    <li className="mc-item" style={{ "--i": i } as CSSProperties} aria-hidden>
      <div className="card market-card is-skeleton">
        <span className="sk sk-top" />
        <span className="sk sk-line" />
        <span className="sk sk-preview" />
        <span className="sk sk-bar" />
        <span className="sk sk-figs" />
        <span className="sk sk-foot" />
      </div>
    </li>
  );
}

/** The card entrance plays once per visit, not on every return to home. */
let introPlayed = false;

export function Home({ index }: { index: Async<MarketIndex> }) {
  const markets = index.data?.markets;
  const [intro] = useState(() => !introPlayed);
  useEffect(() => {
    if (markets) introPlayed = true;
  }, [markets]);

  const query = useQuery();
  const sort = parseSort(query.sort);
  const filters = useMemo<MarketFilters>(
    () => ({ resolved: parseResolved(query.resolved), flagged: parseFlagged(query.flagged) }),
    [query.resolved, query.flagged],
  );
  const active = !isDefaultView(sort, filters);
  const ordered = useMemo(
    () => (markets ? sortMarkets(filterMarkets(markets, filters), sort) : null),
    [markets, sort, filters],
  );
  const orderKey = ordered ? ordered.map((m) => m.name).join("|") : "";
  const { register, snapshot } = useFlip(orderKey);

  const changeSort = useCallback(
    (s: SortKey) => {
      if (s === sort) return;
      snapshot();
      setQuery({ sort: s === DEFAULT_SORT ? null : s });
    },
    [sort, snapshot],
  );
  const changeResolved = useCallback(
    (r: ResolvedFilter) => {
      snapshot();
      setQuery({ resolved: r === "any" ? null : r });
    },
    [snapshot],
  );
  const changeFlagged = useCallback(
    (v: boolean) => {
      snapshot();
      setQuery({ flagged: v ? "1" : null });
    },
    [snapshot],
  );
  const clearTools = useCallback(() => {
    snapshot();
    setQuery({ sort: null, resolved: null, flagged: null });
  }, [snapshot]);

  return (
    <div className={`home${intro ? "" : " no-intro"}`}>
      <Hero markets={markets} />
      <section
        id="markets"
        className="container markets"
        aria-labelledby="markets-title"
        aria-busy={index.status === "loading"}
      >
        <div className="section-head markets-head">
          <h2 id="markets-title" className="section-title" tabIndex={-1}>
            Markets
          </h2>
          <p className="section-sub">Pick a market to see when the big winning bets landed and which accounts were flagged.</p>
          <div className="preview-key" aria-label="How to read a preview">
            <Legend items={PREVIEW_KEY} />
          </div>
        </div>
        <Toolbar label="Sort & filter" active={active} onClear={clearTools} aria-label="Sort and filter markets">
          <ToolbarGroup label="Sort">
            <Segmented value={sort} options={SORT_OPTIONS} onChange={changeSort} label="Sort markets by" />
          </ToolbarGroup>
          <ToolbarGroup label="Resolved">
            <Segmented value={filters.resolved} options={RESOLVED_OPTIONS} onChange={changeResolved} label="Resolved outcome" />
          </ToolbarGroup>
          <ToolbarGroup>
            <Switch checked={filters.flagged} onChange={changeFlagged} label="Only markets with flagged wallets" />
          </ToolbarGroup>
        </Toolbar>
        {index.status === "error" ? (
          <ErrorNote error={index.error} />
        ) : (
          <ul className="market-grid">
            {ordered
              ? ordered.map((m, i) => <MarketCard key={m.name} m={m} i={i} animate={intro} itemRef={register(m.name)} />)
              : Array.from({ length: 7 }, (_, i) => <SkeletonCard key={i} i={i} />)}
            <MethodCard i={ordered ? ordered.length : 7} itemRef={register("__method")} />
          </ul>
        )}
      </section>
    </div>
  );
}
