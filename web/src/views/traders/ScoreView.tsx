import { useCallback, useEffect, useMemo, useState, type CSSProperties } from "react";
import type { MarketData, RubricDisplay, TradersData } from "../../types";
import { StatTile } from "../../components/ui";
import { fmtInt, fmtPct, fmtUSD, smartQuotes } from "../../lib/format";
import { TOP_N, rowsByWallet } from "./model";
import {
  buildGroupTreemap,
  computeGroupSummary,
  countByGroup,
  restRowsByGroup,
  sameFocus,
  type Focus,
  type FocusSource,
  type Grouping,
  type Pin,
} from "./groups";
import { andList, bandGrouping, countedQuestions, flaggedBands, hasSpectrum, orList, rangeText, spectrumData, spectrumGuide } from "./spectrum";
import { useViewportHeight } from "../../lib/useSize";
import { ZOOM_MOD, useEscape } from "./paint";
import { GroupKey } from "./ScoreCard";
import { ScoreConstellation } from "./ScoreConstellation";
import { SpectrumStrip } from "./SpectrumStrip";
import { ScoreTreemap } from "./ScoreTreemap";
import "./traders.css";
import "./score.css";

/**
 * The Insider score tab: every scored whale placed by the wallet rubric's
 * score. The spectrum strip, the score constellation (rings at the band
 * cut-offs), the band treemap, the score card and, at the foot, the rubric's
 * own paragraph on how the score is made; every label, band, colour, question
 * and word of that paragraph comes from the page's `traders.rubric`.
 */
export function ScoreView({ market }: { market: MarketData }) {
  const t = market.traders;
  if (!hasSpectrum(t)) return null;
  // Fresh view state (hidden bands, pins, zoom) per market.
  return <ScoreBody key={market.name} title={market.title} data={t} />;
}

/** The one chrome hue on this view: flagged keys and the pull-quote rule. */
const CRITICAL = "var(--c-critical)";
const keyed = (color: string) => ({ "--key": color }) as CSSProperties;

/** Same chart sizing as the Flagged traders tab. */
const CHROME_H = 412;
const CHART_MIN = 360;
const CHART_MAX = 560;

function ScoreBody({ title, data }: { title: string; data: TradersData & { rubric: RubricDisplay } }) {
  const rubric = data.rubric;
  const view = useMemo(() => spectrumData(data), [data]);
  const grouping = useMemo(() => bandGrouping(rubric, view.traders), [rubric, view]);
  const summary = useMemo(() => computeGroupSummary(view, grouping), [view, grouping]);
  const counts = useMemo(() => countByGroup(grouping), [grouping]);
  const guide = useMemo(() => spectrumGuide(rubric), [rubric]);
  const words = useMemo(() => spectrumWords(rubric), [rubric]);

  const [hidden, setHidden] = useState<ReadonlySet<string>>(() => new Set());
  const [focus, setFocus] = useState<Focus>(null);
  const [pin, setPin] = useState<Pin | null>(null);
  const [zoomCat, setZoomCat] = useState<string | null>(null);
  const viewportH = useViewportHeight();

  const tree = useMemo(() => buildGroupTreemap(view, grouping, hidden), [view, grouping, hidden]);
  const restRows = useMemo(() => restRowsByGroup(tree), [tree]);
  const walletRows = useMemo(() => rowsByWallet(view), [view]);

  const updateFocus = useCallback((f: Focus) => setFocus((prev) => (sameFocus(prev, f) ? prev : f)), []);
  const leaveFrom = useCallback((src: FocusSource) => setFocus((prev) => (prev?.source === src ? null : prev)), []);
  const leaveConstellation = useCallback(() => leaveFrom("constellation"), [leaveFrom]);
  const leaveTreemap = useCallback(() => leaveFrom("treemap"), [leaveFrom]);
  const leaveLegend = useCallback(() => leaveFrom("legend"), [leaveFrom]);
  const leaveSpectrum = useCallback(() => leaveFrom("spectrum"), [leaveFrom]);

  useEscape(useCallback(() => setPin(null), []));

  // A pinned card is a popover: a click anywhere outside it and the chart
  // canvases closes it. "click", not "pointerdown", so a touch that scrolls
  // the page leaves a docked card open.
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

  return (
    <div className="traders">
      <div className="stat-strip">
        <StatTile label="Total whale vol" value={fmtUSD(summary.totalVolume)} hint="Sum of every scored whale's position in this market" />
        <StatTile label="Flagged vol" value={fmtUSD(summary.suspiciousVolume)} color={CRITICAL} hint={`Volume held by wallets rated ${words.flagged}`} />
        <StatTile
          label="Flagged capital"
          value={fmtPct(summary.pctSuspicious)}
          color={CRITICAL}
          emphasis
          hint={`Share of whale capital held by wallets rated ${words.flagged} (flagged volume ÷ total whale volume)`}
        />
        <StatTile label="Total whales" value={fmtInt(summary.totalWhales)} />
        <StatTile label="Flagged accounts" value={fmtInt(summary.flaggedAccounts)} color={CRITICAL} hint={`Wallets rated ${words.flagged}`} />
      </div>

      <section className="tr-narrative" aria-label="Summary">
        {summary.flaggedAccounts === 0 ? (
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
        )}
        {/* The rule in one line, beside the band key the three charts share. */}
        <div className="tr-keyrow">
          <p className="tr-def">
            {words.scale} from {words.questions}. Flagged: {words.range ? <span className="tabular">{words.range}</span> : words.flagged}.
          </p>
          <BandLegend
            grouping={grouping}
            counts={counts}
            hidden={hidden}
            onToggle={toggle}
            onHover={(c) => (c ? updateFocus({ kind: "category", category: c, source: "legend" }) : leaveLegend())}
          />
        </div>
      </section>

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
            Every whale on the <span className="tabular">{words.span}</span> scale, sized by position and coloured by band. The score weighs{" "}
            {words.questions}.
          </p>
        </header>
        <SpectrumStrip
          data={view}
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

      <div className="tr-charts">
        <section className="card card-pad tr-card" aria-labelledby="tr-constellation-title">
          <header className="tr-card-head">
            <div className="tr-card-title">
              <h2 id="tr-constellation-title">Score constellation</h2>
              <p className="tr-hint">
                <span className="tr-hint-fine">{ZOOM_MOD} + scroll to zoom · drag to pan · click to pin</span>
                <span className="tr-hint-coarse">Pinch to zoom · tap to pin</span>
              </p>
            </div>
            <p className="tr-card-desc">
              Wallets orbit the market by {rubric.name.toLowerCase()} and are sized by position: the higher the score, the closer to the core.
              Rings mark the band cut-offs.
            </p>
          </header>
          <ScoreConstellation
            data={view}
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
              <h2 id="tr-treemap-title">Capital by band</h2>
              {/* Zoomed into a band, the hint gives way to the breadcrumb back out. */}
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
              Tile area is position size: each band’s share of the whale stake, its {TOP_N} largest wallets named and the rest pooled.
            </p>
          </header>
          <ScoreTreemap
            data={view}
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

      {rubric.about ? <AboutScore about={rubric.about} /> : null}
    </div>
  );
}

/** One-letter words ("a", "A", "I") held to the next word by a no-break space, so no line ends on one. */
const holdShortWords = (s: string) => s.replace(/(^|\s)([aAI]) (?=\S)/g, "$1$2 ");

/** The rubric's [about] paragraph: a ruled block at the foot of the tab, titled like the chart cards. */
function AboutScore({ about }: { about: NonNullable<RubricDisplay["about"]> }) {
  return (
    <section className="tr-about" aria-labelledby={about.heading ? "tr-about-title" : undefined}>
      {about.heading ? <h2 id="tr-about-title">{smartQuotes(about.heading)}</h2> : null}
      <p>{holdShortWords(smartQuotes(about.text))}</p>
    </section>
  );
}

/** The tab's wording, every word of it from the rubric. */
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

/** Band toggles: hairline tags with the key dot, the band name and a count; hovering lights every chart. */
function BandLegend({
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
    <ul className="legend tr-legend" aria-label="Bands (toggle to hide or show)">
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
