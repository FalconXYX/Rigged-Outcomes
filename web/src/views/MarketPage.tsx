import { useEffect, useLayoutEffect, useRef, useState, type CSSProperties, type ReactNode } from "react";
import type { MarketData, MarketIndex, MarketIndexEntry } from "../types";
import { loadIndex, loadMarket, useAsync, type Async } from "../lib/data";
import { fmtInt, polymarketMarket, smartQuotes } from "../lib/format";
import { href, navigate, useQuery, type Query } from "../lib/router";
import { ErrorNote, Loading, Select } from "../components/ui";
import { TimelineView } from "./timeline/TimelineView";
import { TradersView } from "./traders/TradersView";
import { NotFound } from "./NotFound";
import { ResolvedChip, fmtSpan, shortTitle, spanTitle } from "./Home";
import "../components/shell.css";
import "./market.css";

type View = "timeline" | "traders";

class MarketNotFound extends Error {}

/** Resolved market payloads, so revisiting a market renders without a loading frame. */
const resolved = new Map<string, MarketData>();

async function fetchMarket(name: string): Promise<MarketData> {
  // Both requests start together (no index → market round-trip), but the
  // index decides: a missing JSON file can come back as an HTML fallback page.
  const [idx, data] = await Promise.all([
    loadIndex().catch(() => null),
    loadMarket(name).then(
      (d) => ({ ok: true as const, d }),
      (e: unknown) => ({ ok: false as const, e }),
    ),
  ]);
  if (idx && !idx.markets.some((m) => m.name === name)) throw new MarketNotFound(name);
  if (!data.ok) throw data.e;
  return data.d;
}

interface HeaderInfo {
  name: string;
  title: string;
  slug: string;
  correctOutcome: string | null;
  start: number;
  end: number;
  positions: number;
  flagged: number | null;
}

const fromEntry = (e: MarketIndexEntry): HeaderInfo => ({
  name: e.name,
  title: e.title,
  slug: e.slug,
  correctOutcome: e.correctOutcome,
  start: e.start,
  end: e.end,
  positions: e.positions,
  flagged: e.flaggedAccounts,
});

const fromData = (d: MarketData): HeaderInfo => ({
  name: d.name,
  title: d.title,
  slug: d.slug,
  correctOutcome: d.correctOutcome,
  start: d.timeline.span.start,
  end: d.timeline.span.end,
  positions: d.timeline.positions,
  flagged: d.traders?.flaggedAccounts ?? null,
});

/** The flagged count's key: critical red, as a dot beside the (ink) number. */
const CRITICAL_KEY = { "--key": "var(--c-critical)" } as CSSProperties;

/** Timeline preferences worth carrying to another market (the rest — zoom, pin, replay — is market-specific). */
const CARRY_KEYS = ["view", "odds", "filter"] as const;

const pick = (q: Query, keys: readonly string[]): Query | undefined => {
  let out: Query | undefined;
  for (const k of keys) if (k in q) (out ??= {})[k] = q[k];
  return out;
};

/* ── View tabs (real links, text + sliding underline) ───────────────────── */

function ViewTabs({
  name,
  view,
  flagged,
  timelineQuery,
}: {
  name: string;
  view: View;
  flagged: number | null | undefined;
  /** The timeline's last URL state, so the Timeline tab brings back its zoom, pin and paused replay. */
  timelineQuery: Query | undefined;
}) {
  const ref = useRef<HTMLElement>(null);
  const [ind, setInd] = useState<{ x: number; w: number } | null>(null);

  useLayoutEffect(() => {
    const nav = ref.current;
    if (!nav) return;
    const measure = () => {
      const a = nav.querySelector<HTMLElement>('[aria-current="page"]');
      if (!a) return;
      const next = { x: a.offsetLeft, w: a.offsetWidth };
      setInd((p) => (p && p.x === next.x && p.w === next.w ? p : next));
    };
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(nav);
    nav.querySelectorAll("a").forEach((a) => ro.observe(a));
    document.fonts?.ready.then(measure).catch(() => {});
    return () => ro.disconnect();
  }, [view, flagged]);

  const tabs: { key: View; label: string; count?: number | null }[] = [
    { key: "timeline", label: "Timeline" },
    { key: "traders", label: "Flagged traders", count: flagged },
  ];

  return (
    <nav ref={ref} className={`view-tabs${ind ? " measured" : ""}`} aria-label="Market views">
      {ind && <span className="view-tabs-ind" style={{ width: ind.w, transform: `translateX(${ind.x}px)` }} aria-hidden />}
      {tabs.map((t) => (
        <a
          key={t.key}
          className="view-tab"
          href={href.market(name, t.key, t.key === "timeline" ? timelineQuery : undefined)}
          aria-current={view === t.key ? "page" : undefined}
        >
          {t.label}
          {t.count != null && (
            <span className="tab-count tabular" title={`${fmtInt(t.count)} flagged accounts`}>
              {t.count > 0 && <span className="key-dot" style={CRITICAL_KEY} aria-hidden />}
              {fmtInt(t.count)}
            </span>
          )}
        </a>
      ))}
    </nav>
  );
}

/* ── Header ─────────────────────────────────────────────────────────────── */

function MarketHeader({
  name,
  view,
  info,
  markets,
}: {
  name: string;
  view: View;
  info: HeaderInfo | null;
  markets: MarketIndexEntry[] | undefined;
}) {
  // The timeline keeps its state (zoom, pin, toggles, paused replay) in the
  // query string and unmounts on the traders view. Remember its last query
  // while it is on screen so the Timeline tab links straight back to it; a
  // different market starts clean.
  const query = useQuery();
  const lastTimeline = useRef<{ name: string; query: Query } | null>(null);
  useEffect(() => {
    if (view === "timeline") lastTimeline.current = { name, query };
  }, [name, view, query]);
  const timelineQuery = view === "timeline" ? query : lastTimeline.current?.name === name ? lastTimeline.current.query : undefined;

  return (
    <header className="market-head">
      <div className="container">
        {info ? (
          <>
            <h1 className="market-title">{smartQuotes(info.title)}</h1>
            <div className="market-meta meta-line label">
              <ResolvedChip outcome={info.correctOutcome} />
              <span className="tabular" title={spanTitle(info.start, info.end)}>
                {fmtSpan(info.start, info.end)} ET
              </span>
              <span title="Every position in the market, whales and crowd">
                <b className="tabular">{fmtInt(info.positions)}</b> positions
              </span>
              <a href={polymarketMarket(info.slug)} target="_blank" rel="noopener noreferrer">
                Polymarket ↗<span className="visually-hidden"> (opens in a new tab)</span>
              </a>
            </div>
          </>
        ) : (
          <div className="market-head-sk" aria-hidden>
            <span className="msk msk-title" />
            <span className="msk msk-meta" />
          </div>
        )}

        <div className="market-toolbar">
          <ViewTabs name={name} view={view} flagged={info?.flagged} timelineQuery={timelineQuery} />
          {markets && markets.length > 1 && (
            <div className="switcher">
              <Select
                value={name}
                label="Switch market"
                options={markets.map((m) => ({ value: m.name, label: shortTitle(m) }))}
                // Carry the view / odds / filter choice in the URL; zoom, pin and replay belong to the market left behind.
                onChange={(v) => v !== name && navigate(href.market(v, view, view === "timeline" ? pick(query, CARRY_KEYS) : undefined))}
              />
            </div>
          )}
        </div>
      </div>
    </header>
  );
}

/* ── Page ───────────────────────────────────────────────────────────────── */

export function MarketPage({ name, view, index }: { name: string; view: View; index: Async<MarketIndex> }) {
  const entry = index.data?.markets.find((m) => m.name === name);
  const market = useAsync(() => fetchMarket(name), name);
  if (market.status === "ready") resolved.set(name, market.data);
  const data = resolved.get(name);

  // Keep the last market on screen (dimmed) while the next one loads: no layout jump.
  const [shown, setShown] = useState<MarketData | null>(data ?? null);
  if (data && data !== shown) setShown(data);
  const display = data ?? shown;
  const stale = !data && display != null;

  const unknown =
    (index.status === "ready" && !entry) || (market.status === "error" && market.error instanceof MarketNotFound);
  if (unknown) {
    return (
      <NotFound title="Market not found">
        There’s no market called <code>{name}</code> here. Pick one of the analyzed markets from the home page.
      </NotFound>
    );
  }

  const info = entry ? fromEntry(entry) : data ? fromData(data) : null;
  const loadingLabel = `Loading ${info ? shortTitle(info) : "market"}…`;

  let body: ReactNode;
  if (market.status === "error" && !data) body = <ErrorNote error={market.error} />;
  else if (display)
    body = (
      <div className={`market-view${stale ? " is-stale" : ""}`} aria-busy={stale || undefined} inert={stale || undefined}>
        {view === "traders" ? (
          <TradersView key={display.name} market={display} />
        ) : (
          <TimelineView key={display.name} market={display} />
        )}
      </div>
    );
  else body = <Loading label={loadingLabel} />;

  return (
    <div className="market-page">
      <MarketHeader name={name} view={view} info={info} markets={index.data?.markets} />
      <div className="container market-body">
        {stale && (
          <div className="stale-note" role="status">
            <span className="stale-pill">
              <span className="spinner" aria-hidden />
              {loadingLabel}
            </span>
          </div>
        )}
        {body}
      </div>
    </div>
  );
}
