import { useCallback, useEffect, useMemo, useState, type CSSProperties } from "react";
import type { MarketData, RubricDisplay, TradersData } from "../../types";
import { Segmented, StatTile } from "../../components/ui";
import { fmtInt, fmtPct, fmtUSD, smartQuotes } from "../../lib/format";
import {
  TOP_N,
  buildTreemapTree,
  categoryGrouping,
  computeTraderSummary,
  countByGroup,
  cutoffGuide,
  fmtThreshold,
  restRowsByCategory,
  rowsByWallet,
  sameFocus,
  type Focus,
  type FocusSource,
  type Grouping,
  type Pin,
} from "./model";
import { andList, bandGrouping, countedQuestions, flaggedBands, hasSpectrum, orList, rangeText, spectrumData, spectrumGuide } from "./spectrum";
import { useViewportHeight } from "../../lib/useSize";
import { ZOOM_MOD, useEscape } from "./paint";
import { GroupKey } from "./TraderCard";
import { Constellation } from "./Constellation";
import { SpectrumStrip } from "./SpectrumStrip";
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
  // Fresh view state (hidden groups, pins, zoom) per market.
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

/** Which view a scored market opens in: the insider score, or today's cut-offs. Remembered in this browser. */
type Mode = "spectrum" | "cutoffs";
const MODE_KEY = "ro-traders-view";

function readMode(): Mode | null {
  try {
    const v = window.localStorage.getItem(MODE_KEY);
    return v === "spectrum" || v === "cutoffs" ? v : null;
  } catch {
    return null;
  }
}

function writeMode(m: Mode) {
  try {
    window.localStorage.setItem(MODE_KEY, m);
  } catch {
    /* storage off: the choice lasts this page view */
  }
}

function TradersBody({ title, data }: { title: string; data: TradersData }) {
  const scored = hasSpectrum(data) ? data : null;
  const rubric = scored?.rubric;
  const [mode, setMode] = useState<Mode>(() => (rubric && readMode() !== "cutoffs" ? "spectrum" : "cutoffs"));
  const spectrum = !!rubric && mode === "spectrum";
  const choose = useCallback((m: Mode) => {
    setMode(m);
    writeMode(m);
  }, []);

  const view = useMemo(() => (spectrum && scored ? spectrumData(scored) : data), [data, scored, spectrum]);
  const grouping = useMemo(() => (spectrum && rubric ? bandGrouping(rubric, view.traders) : categoryGrouping(data)), [spectrum, rubric, view, data]);

  // A switch starts the charts afresh: hidden groups, pins and zoom name the other view's groups and rows.
  return (
    <TradersPanel
      key={spectrum ? "spectrum" : "cutoffs"}
      title={title}
      data={view}
      source={data}
      grouping={grouping}
      rubric={spectrum ? rubric : undefined}
      modeSwitch={rubric ? { rubric, mode: spectrum ? "spectrum" : "cutoffs", onChange: choose } : undefined}
    />
  );
}

function TradersPanel({
  title,
  data,
  source,
  grouping,
  rubric,
  modeSwitch,
}: {
  title: string;
  /** The rows the charts draw: every whale, or the scored whales placed by score. */
  data: TradersData;
  /** The page's own traders block (thresholds for the cut-off wording). */
  source: TradersData;
  grouping: Grouping;
  /** Present in the insider-score view. */
  rubric?: RubricDisplay;
  modeSwitch?: { rubric: RubricDisplay; mode: Mode; onChange: (m: Mode) => void };
}) {
  const summary = useMemo(() => computeTraderSummary(data, grouping), [data, grouping]);
  const counts = useMemo(() => countByGroup(grouping), [grouping]);
  const guide = useMemo(() => (rubric ? spectrumGuide(rubric) : cutoffGuide()), [rubric]);

  const [hidden, setHidden] = useState<ReadonlySet<string>>(() => new Set());
  const [focus, setFocus] = useState<Focus>(null);
  const [pin, setPin] = useState<Pin | null>(null);
  const [zoomCat, setZoomCat] = useState<string | null>(null);
  const viewportH = useViewportHeight();

  const tree = useMemo(() => buildTreemapTree(data, grouping, hidden), [data, grouping, hidden]);
  const restRows = useMemo(() => restRowsByCategory(tree), [tree]);
  const walletRows = useMemo(() => rowsByWallet(data), [data]);

  const updateFocus = useCallback((f: Focus) => setFocus((prev) => (sameFocus(prev, f) ? prev : f)), []);
  const leaveFrom = useCallback((src: FocusSource) => setFocus((prev) => (prev?.source === src ? null : prev)), []);
  const leaveConstellation = useCallback(() => leaveFrom("constellation"), [leaveFrom]);
  const leaveTreemap = useCallback(() => leaveFrom("treemap"), [leaveFrom]);
  const leaveLegend = useCallback(() => leaveFrom("legend"), [leaveFrom]);
  const leaveSpectrum = useCallback(() => leaveFrom("spectrum"), [leaveFrom]);

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

  const toggle = (c: string) =>
    setHidden((prev) => {
      const next = new Set(prev);
      if (next.has(c)) next.delete(c);
      else next.add(c);
      return next;
    });

  const zoomed = zoomCat ? tree.children.find((c) => c.category === zoomCat) : undefined;
  const zoomGroup = zoomCat ? grouping.byId.get(zoomCat) : undefined;
  const chartH = Math.round(Math.min(CHART_MAX, Math.max(CHART_MIN, viewportH - CHROME_H)));
  const quoted = smartQuotes(title, { nested: true });
  const words = rubric ? spectrumWords(rubric) : null;
  const firstX = fmtThreshold(source.thresholds.firstX);
  const pctVol = fmtThreshold(source.thresholds.percentVolume);

  return (
    <div className="traders">
      <div className="stat-strip">
        <StatTile label="Total whale vol" value={fmtUSD(summary.totalVolume)} hint="Sum of every whale's position in this market" />
        <StatTile
          label={rubric ? "Flagged vol" : "Suspicious vol"}
          value={fmtUSD(summary.suspiciousVolume)}
          color={CRITICAL}
          hint={words ? `Volume held by wallets rated ${words.flagged}` : "Volume held by flagged accounts"}
        />
        <StatTile
          label="Flagged capital"
          value={fmtPct(summary.pctSuspicious)}
          color={CRITICAL}
          emphasis
          hint={
            words
              ? `Share of whale capital held by wallets rated ${words.flagged} (flagged volume ÷ total whale volume)`
              : "Share of whale capital held by flagged accounts (suspicious volume ÷ total whale volume)"
          }
        />
        <StatTile label="Total whales" value={fmtInt(summary.totalWhales)} />
        <StatTile
          label="Flagged accounts"
          value={fmtInt(summary.flaggedAccounts)}
          color={CRITICAL}
          hint={words ? `Wallets rated ${words.flagged}` : "Wallets matching one or both flags"}
        />
      </div>

      <section className="tr-narrative" aria-label="Summary">
        {words ? (
          summary.flaggedAccounts === 0 ? (
            <p className="tr-lead">
              None of the <span className="tabular">{fmtInt(summary.totalWhales)}</span> whales in “{quoted}” is rated {words.flagged}.
            </p>
          ) : summary.pctSuspicious < 10 ? (
            <p className="tr-lead">
              <span className="tabular">{fmtPct(summary.pctSuspicious)}</span> of whale capital (
              <span className="tabular">{fmtUSD(summary.suspiciousVolume)}</span>) in “{quoted}” sits with wallets rated {words.flagged}.
            </p>
          ) : (
            <>
              <p className="tr-lead">In “{quoted}”, a large share of the whale capital sits with flagged wallets.</p>
              <p className="tr-callout rule-key" style={keyed(CRITICAL)}>
                <strong className="tabular">{fmtPct(summary.pctSuspicious)}</strong> of it (
                <strong className="tabular">{fmtUSD(summary.suspiciousVolume)}</strong>) is held by{" "}
                <span className="tabular">{fmtInt(summary.flaggedAccounts)}</span> wallet{summary.flaggedAccounts === 1 ? "" : "s"} rated{" "}
                {words.flagged}
                {words.range ? <span className="tabular"> ({words.range})</span> : null}.
              </p>
            </>
          )
        ) : summary.flaggedAccounts === 0 ? (
          <p className="tr-lead">
            None of the <span className="tabular">{fmtInt(summary.totalWhales)}</span> whales in “{quoted}” match either flag.
          </p>
        ) : summary.pctSuspicious < 10 ? (
          <p className="tr-lead">
            <span className="tabular">{fmtPct(summary.pctSuspicious)}</span> of whale capital (
            <span className="tabular">{fmtUSD(summary.suspiciousVolume)}</span>) in “{quoted}” sits with flagged accounts.
          </p>
        ) : (
          <>
            <p className="tr-lead">In “{quoted}”, a large share of the whale capital sits with flagged accounts.</p>
            {/* the flag rule in words (scrape_insiders.analyze_whale): at most firstX trades before the account's first
                trade here, or this position at least pctVol% of everything it had staked by then */}
            <p className="tr-callout rule-key" style={keyed(CRITICAL)}>
              <strong className="tabular">{fmtPct(summary.pctSuspicious)}</strong> of it (
              <strong className="tabular">{fmtUSD(summary.suspiciousVolume)}</strong>) is held by accounts that had made{" "}
              <span className="tabular">{firstX}</span> or fewer trades before their first bet here, or that put{" "}
              <span className="tabular">{pctVol}%</span> or more of their money into this market.
            </p>
          </>
        )}
        {/* The rule in one line, beside the key all three charts share and the view switch. */}
        <div className="tr-keyrow">
          {words ? (
            <p className="tr-def">
              {words.scale} from {words.questions}. Flagged: {words.range ? <span className="tabular">{words.range}</span> : words.flagged}.
            </p>
          ) : (
            <p className="tr-def">
              Flagged: <span className="tabular">≤{firstX}</span> prior trades, or <span className="tabular">≥{pctVol}%</span> of
              lifetime volume in this market.
            </p>
          )}
          <div className="tr-keyctl">
            <GroupLegend
              grouping={grouping}
              counts={counts}
              hidden={hidden}
              onToggle={toggle}
              onHover={(c) => (c ? updateFocus({ kind: "category", category: c, source: "legend" }) : leaveLegend())}
            />
            {modeSwitch && (
              <Segmented
                label="Wallet view"
                value={modeSwitch.mode}
                options={[
                  { value: "spectrum", label: modeSwitch.rubric.name },
                  { value: "cutoffs", label: "Cut-offs" },
                ]}
                onChange={modeSwitch.onChange}
              />
            )}
          </div>
        </div>
      </section>

      {rubric && words && (
        <section className="card card-pad tr-card" aria-labelledby="tr-spectrum-title">
          <header className="tr-card-head">
            <div className="tr-card-title">
              <h2 id="tr-spectrum-title">{rubric.name}</h2>
              <p className="tr-hint">
                <span className="tr-hint-fine">Hover a wallet to find it in both charts · click to pin</span>
                <span className="tr-hint-coarse">Tap a wallet to pin</span>
              </p>
            </div>
            <p className="tr-card-desc">
              Every whale on the <span className="tabular">{words.span}</span> scale, sized by position and coloured by band. The score
              weighs {words.questions}.
            </p>
          </header>
          <SpectrumStrip
            data={data}
            rubric={rubric}
            grouping={grouping}
            hidden={hidden}
            focus={focus}
            onFocus={updateFocus}
            onLeave={leaveSpectrum}
            pin={pin}
            onPin={setPin}
            restRows={restRows}
            walletRows={walletRows}
          />
        </section>
      )}

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
              {rubric
                ? `Wallets orbit the market by ${rubric.name.toLowerCase()} and are sized by position: the higher the score, the closer to the core. Rings mark the band cut-offs.`
                : "Wallets orbit the market by risk score and are sized by position; flagged accounts with no history and an outsized stake are pulled into the core."}
            </p>
          </header>
          <Constellation
            data={data}
            grouping={grouping}
            guide={guide}
            rubric={rubric}
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
              {/* Zoomed into a group, the hint gives way to the breadcrumb back out. */}
              {zoomCat && zoomed && zoomGroup ? (
                <div className="tr-crumbs-row">
                  <nav className="tr-crumbs" aria-label="Treemap level">
                    <button type="button" className="tr-crumb link" onClick={() => setZoomCat(null)}>
                      All whales
                    </button>
                    <span className="tr-crumb-sep" aria-hidden>
                      ›
                    </span>
                    <span className="tr-crumb current" aria-current="page">
                      <GroupKey group={zoomGroup} />
                      {zoomGroup.label}
                    </span>
                  </nav>
                  <span className="tr-crumbs-meta tabular">
                    {fmtUSD(zoomed.value)} · {fmtPct(zoomed.pctOfTotal)} of stake
                  </span>
                </div>
              ) : (
                <p className="tr-hint">
                  <span className="tr-hint-fine">Click a {grouping.noun} to zoom · click a wallet to pin</span>
                  <span className="tr-hint-coarse">Tap a {grouping.noun} to zoom · tap a wallet to pin</span>
                </p>
              )}
            </div>
            <p className="tr-card-desc">
              {rubric
                ? `Tile area is position size: each band’s share of the whale stake, its ${TOP_N} largest wallets named and the rest pooled.`
                : `Tile area is position size: each risk category’s share of the whale stake, its ${TOP_N} largest wallets named and the rest pooled.`}
            </p>
          </header>
          <Treemap
            data={data}
            tree={tree}
            grouping={grouping}
            rubric={rubric}
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

/** The insider-score view's wording, every word of it from the rubric. */
function spectrumWords(rubric: RubricDisplay) {
  const [lo, hi] = rubric.scale;
  const f = flaggedBands(rubric);
  const counted = countedQuestions(rubric).map((q) => q.label);
  return {
    /** "Suspicious or Highly suspicious" */
    flagged: orList(f.labels),
    /** "Insider score 60–100", when the flagged bands are the top of the scale */
    range: f.from != null ? rangeText(rubric, f.from, f.to) : null,
    /** "Insider score 0–100" */
    scale: rangeText(rubric, lo, hi),
    span: `${lo}–${hi}`,
    /** "Overall, Timing, Account, Conviction and Pattern" (heaviest first) */
    questions: andList(counted),
  };
}

/** Group toggles: hairline tags with the key dot, the short name and a count; hovering lights every chart. */
function GroupLegend({
  grouping,
  counts,
  hidden,
  onToggle,
  onHover,
}: {
  grouping: Grouping;
  counts: Map<string, number>;
  hidden: ReadonlySet<string>;
  onToggle: (c: string) => void;
  onHover: (c: string | null) => void;
}) {
  const present = grouping.groups.filter((g) => (counts.get(g.id) ?? 0) > 0);
  return (
    <ul className="legend tr-legend" aria-label={`${grouping.mode === "spectrum" ? "Bands" : "Risk categories"} (toggle to hide or show)`}>
      {present.map((g) => {
        const off = hidden.has(g.id);
        return (
          <li key={g.id}>
            <button
              type="button"
              className={`legend-item keyed${off ? " off" : ""}`}
              aria-pressed={!off}
              onClick={() => {
                onToggle(g.id);
                onHover(off ? g.id : null);
              }}
              onPointerEnter={() => !off && onHover(g.id)}
              onPointerLeave={() => onHover(null)}
              onFocus={() => !off && onHover(g.id)}
              onBlur={() => onHover(null)}
              title={off ? `Show ${g.label}` : `Hide ${g.label}`}
            >
              <GroupKey group={g} />
              {g.short}
              <span className="tr-legend-count tabular">{fmtInt(counts.get(g.id) ?? 0)}</span>
            </button>
          </li>
        );
      })}
    </ul>
  );
}
