import { useCallback, useEffect, useMemo, useState, type CSSProperties } from "react";
import type { MarketData, TraderCategory, TradersData } from "../../types";
import { StatTile } from "../../components/ui";
import { fmtInt, fmtPct, fmtUSD, smartQuotes } from "../../lib/format";
import {
  CATEGORY_ORDER,
  TOP_N,
  buildTreemapTree,
  categoryNames,
  computeTraderSummary,
  countByCategory,
  fmtThreshold,
  restRowsByCategory,
  rowsByWallet,
  sameFocus,
  type CategoryName,
  type Focus,
  type FocusSource,
  type Pin,
} from "./model";
import { useViewportHeight } from "../../lib/useSize";
import { ZOOM_MOD, useEscape } from "./paint";
import { CategoryKey } from "./TraderCard";
import { Constellation } from "./Constellation";
import { Treemap } from "./Treemap";
import "./traders.css";

export function TradersView({ market }: { market: MarketData }) {
  if (!market.traders || market.traders.traders.length === 0) {
    return (
      <div className="traders">
        <div className="card card-pad tr-empty">
          <svg width="40" height="40" viewBox="0 0 40 40" aria-hidden>
            <circle cx="20" cy="20" r="17" className="tr-empty-ring" />
            <circle cx="20" cy="20" r="9" className="tr-empty-ring" />
            <circle cx="20" cy="20" r="2.5" className="tr-empty-dot" />
          </svg>
          <h2>No trader analysis for this market yet</h2>
          <p>
            The whale-history scrape hasn’t been run for “{smartQuotes(market.title, { nested: true })}”, so there are no wallets to
            score. The bet timeline for this market is still available.
          </p>
        </div>
      </div>
    );
  }
  // Fresh view state (hidden categories, pins, zoom) per market.
  return <TradersBody key={market.name} title={market.title} data={market.traders} />;
}

/** The one chrome hue on this view: flagged keys and the pull-quote rule. */
const CRITICAL = "var(--c-critical)";
const keyed = (color: string) => ({ "--key": color }) as CSSProperties;

/**
 * Everything on screen besides the chart itself when the view is scrolled to
 * the stat strip: sticky header, stats, summary, card chrome and a little air.
 * The charts take the rest, so the whole view fits one screen.
 */
const CHROME_H = 412;
const CHART_MIN = 360;
const CHART_MAX = 560;

function TradersBody({ title, data }: { title: string; data: TradersData }) {
  const summary = useMemo(() => computeTraderSummary(data), [data]);
  const names = useMemo(() => categoryNames(data.thresholds), [data]);
  const counts = useMemo(() => countByCategory(data), [data]);

  const [hidden, setHidden] = useState<ReadonlySet<TraderCategory>>(() => new Set());
  const [focus, setFocus] = useState<Focus>(null);
  const [pin, setPin] = useState<Pin | null>(null);
  const [zoomCat, setZoomCat] = useState<TraderCategory | null>(null);
  const viewportH = useViewportHeight();

  const tree = useMemo(() => buildTreemapTree(data, hidden), [data, hidden]);
  const restRows = useMemo(() => restRowsByCategory(tree), [tree]);
  const walletRows = useMemo(() => rowsByWallet(data), [data]);

  const updateFocus = useCallback((f: Focus) => setFocus((prev) => (sameFocus(prev, f) ? prev : f)), []);
  const leaveFrom = useCallback((src: FocusSource) => setFocus((prev) => (prev?.source === src ? null : prev)), []);
  const leaveConstellation = useCallback(() => leaveFrom("constellation"), [leaveFrom]);
  const leaveTreemap = useCallback(() => leaveFrom("treemap"), [leaveFrom]);
  const leaveLegend = useCallback(() => leaveFrom("legend"), [leaveFrom]);

  useEscape(useCallback(() => setPin(null), []));

  // A pinned card is a popover: a click anywhere outside it and the chart
  // canvases closes it. (Clicks on a chart are handled there — a mark
  // toggles its pin, empty space clears it.) "click", not "pointerdown", so
  // a touch that scrolls the page leaves a docked card open.
  useEffect(() => {
    if (!pin) return;
    const onClick = (e: MouseEvent) => {
      const el = e.target instanceof Element ? e.target : null;
      if (el?.closest(".tr-pin, .tr-svg, .tr-zoom")) return;
      setPin(null);
    };
    document.addEventListener("click", onClick);
    return () => document.removeEventListener("click", onClick);
  }, [pin]);

  const toggle = (c: TraderCategory) =>
    setHidden((prev) => {
      const next = new Set(prev);
      if (next.has(c)) next.delete(c);
      else next.add(c);
      return next;
    });

  const firstX = fmtThreshold(data.thresholds.firstX);
  const pctVol = fmtThreshold(data.thresholds.percentVolume);
  const zoomed = zoomCat ? tree.children.find((c) => c.category === zoomCat) : undefined;
  const chartH = Math.round(Math.min(CHART_MAX, Math.max(CHART_MIN, viewportH - CHROME_H)));

  return (
    <div className="traders">
      <div className="stat-strip">
        <StatTile label="Total whale vol" value={fmtUSD(summary.totalVolume)} hint="Sum of every whale's position in this market" />
        <StatTile label="Suspicious vol" value={fmtUSD(summary.suspiciousVolume)} color={CRITICAL} hint="Volume held by flagged accounts" />
        <StatTile
          label="Flagged capital"
          value={fmtPct(summary.pctSuspicious)}
          color={CRITICAL}
          emphasis
          hint="Share of whale capital held by flagged accounts (suspicious volume ÷ total whale volume)"
        />
        <StatTile label="Total whales" value={fmtInt(summary.totalWhales)} />
        <StatTile label="Flagged accounts" value={fmtInt(summary.flaggedAccounts)} color={CRITICAL} hint="Wallets matching one or both flags" />
      </div>

      <section className="tr-narrative" aria-label="Summary">
        {summary.flaggedAccounts === 0 ? (
          <p className="tr-lead">
            None of the <span className="tabular">{fmtInt(summary.totalWhales)}</span> whales in “
            {smartQuotes(title, { nested: true })}” match either flag.
          </p>
        ) : summary.pctSuspicious < 10 ? (
          <p className="tr-lead">
            <span className="tabular">{fmtPct(summary.pctSuspicious)}</span> of whale capital (
            <span className="tabular">{fmtUSD(summary.suspiciousVolume)}</span>) in “{smartQuotes(title, { nested: true })}” sits
            with flagged accounts.
          </p>
        ) : (
          <>
            <p className="tr-lead">
              Analysis of “{smartQuotes(title, { nested: true })}” reveals a critical structural imbalance in capital deployment.
            </p>
            <p className="tr-callout rule-key" style={keyed(CRITICAL)}>
              <strong className="tabular">{fmtPct(summary.pctSuspicious)}</strong> of the capital (
              <strong className="tabular">{fmtUSD(summary.suspiciousVolume)}</strong>) is artificially concentrated among
              flagged accounts. These accounts exhibit suspicious signatures: they possess low prior trading history on the
              platform, yet they executed large, targeted positions exclusively on this outcome.
            </p>
          </>
        )}
        {/* The rule in one line, beside the category key both charts share; the thresholds come from the export. */}
        <div className="tr-keyrow">
          <p className="tr-def">
            Flagged: <span className="tabular">≤{firstX}</span> prior trades, or <span className="tabular">≥{pctVol}%</span> of
            lifetime volume in this market.
          </p>
          <CategoryLegend
            names={names}
            counts={counts}
            hidden={hidden}
            onToggle={toggle}
            onHover={(c) => (c ? updateFocus({ kind: "category", category: c, source: "legend" }) : leaveLegend())}
          />
        </div>
      </section>

      <div className="tr-charts">
        <section className="card card-pad tr-card" aria-labelledby="tr-constellation-title">
          <header className="tr-card-head">
            <div className="tr-card-title">
              <h2 id="tr-constellation-title">Threat constellation</h2>
              <p className="tr-hint">
                <span className="tr-hint-fine">{ZOOM_MOD} + scroll to zoom · drag to pan · click to pin</span>
                <span className="tr-hint-coarse">Pinch to zoom · tap to pin</span>
              </p>
            </div>
            <p className="tr-card-desc">
              Wallets orbit the market by risk score and are sized by position; flagged accounts with no history and an outsized
              stake are pulled into the core.
            </p>
          </header>
          <Constellation
            data={data}
            names={names}
            hidden={hidden}
            focus={focus}
            onFocus={updateFocus}
            onLeave={leaveConstellation}
            pin={pin}
            onPin={setPin}
            restRows={restRows}
            walletRows={walletRows}
            totalVolume={summary.totalVolume}
            maxHeight={chartH}
          />
        </section>

        <section className="card card-pad tr-card" aria-labelledby="tr-treemap-title">
          <header className="tr-card-head">
            <div className="tr-card-title">
              <h2 id="tr-treemap-title">Capital concentration</h2>
              {/* Zoomed into a category, the hint gives way to the breadcrumb back out. */}
              {zoomCat && zoomed ? (
                <div className="tr-crumbs-row">
                  <nav className="tr-crumbs" aria-label="Treemap level">
                    <button type="button" className="tr-crumb link" onClick={() => setZoomCat(null)}>
                      All whales
                    </button>
                    <span className="tr-crumb-sep" aria-hidden>
                      ›
                    </span>
                    <span className="tr-crumb current" aria-current="page">
                      <CategoryKey category={zoomCat} />
                      {names[zoomCat].label}
                    </span>
                  </nav>
                  <span className="tr-crumbs-meta tabular">
                    {fmtUSD(zoomed.value)} · {fmtPct(zoomed.pctOfTotal)} of stake
                  </span>
                </div>
              ) : (
                <p className="tr-hint">
                  <span className="tr-hint-fine">Click a category to zoom · click a wallet to pin</span>
                  <span className="tr-hint-coarse">Tap a category to zoom · tap a wallet to pin</span>
                </p>
              )}
            </div>
            <p className="tr-card-desc">
              Tile area is position size: each risk category’s share of the whale stake, its {TOP_N} largest wallets named and
              the rest pooled.
            </p>
          </header>
          <Treemap
            data={data}
            tree={tree}
            names={names}
            focus={focus}
            onFocus={updateFocus}
            onLeave={leaveTreemap}
            pin={pin}
            onPin={setPin}
            zoomCat={zoomCat}
            onZoom={setZoomCat}
            walletRows={walletRows}
            maxHeight={chartH}
          />
        </section>
      </div>
    </div>
  );
}

/** Category toggles: hairline tags with the key dot, the short name and a count; hovering lights both charts. */
function CategoryLegend({
  names,
  counts,
  hidden,
  onToggle,
  onHover,
}: {
  names: Record<TraderCategory, CategoryName>;
  counts: Record<TraderCategory, number>;
  hidden: ReadonlySet<TraderCategory>;
  onToggle: (c: TraderCategory) => void;
  onHover: (c: TraderCategory | null) => void;
}) {
  const present = CATEGORY_ORDER.filter((c) => counts[c] > 0);
  return (
    <ul className="legend tr-legend" aria-label="Risk categories (toggle to hide or show)">
      {present.map((c) => {
        const off = hidden.has(c);
        return (
          <li key={c}>
            <button
              type="button"
              className={`legend-item keyed${off ? " off" : ""}`}
              aria-pressed={!off}
              onClick={() => {
                onToggle(c);
                onHover(off ? c : null);
              }}
              onPointerEnter={() => !off && onHover(c)}
              onPointerLeave={() => onHover(null)}
              onFocus={() => !off && onHover(c)}
              onBlur={() => onHover(null)}
              title={off ? `Show ${names[c].label}` : `Hide ${names[c].label}`}
            >
              <CategoryKey category={c} />
              {names[c].short}
              <span className="tr-legend-count tabular">{fmtInt(counts[c])}</span>
            </button>
          </li>
        );
      })}
    </ul>
  );
}
