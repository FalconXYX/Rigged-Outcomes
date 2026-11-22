import { useLayoutEffect, useRef, useState, type CSSProperties, type ReactNode } from "react";
import type { RubricDisplay, Trader, TraderSpectrum } from "../../types";
import { TipRow } from "../../components/ui";
import { fmtInt, fmtPct, fmtUSD, polymarketProfile, shortAddr } from "../../lib/format";
import type { Group } from "./groups";
import { answerOf, bandColor, bandOf, formatFact, levelSegments, likeliestLevel } from "./spectrum";
import { prefersReducedMotion } from "../../lib/useSize";

/* ── Group key (dot; the top tier gets a surface-colored core) ──────── */

export function GroupKey({ group, size = 12 }: { group: Pick<Group, "color" | "core">; size?: number }) {
  const r = size * 0.38;
  return (
    <svg className="tr-key" width={size + 4} height={size} viewBox={`0 0 ${size + 4} ${size}`} aria-hidden>
      <circle cx={(size + 4) / 2} cy={size / 2} r={r} style={{ fill: group.color }} />
      {group.core && <circle cx={(size + 4) / 2} cy={size / 2} r={r * 0.36} style={{ fill: "var(--surface)" }} />}
    </svg>
  );
}

const pctText = (p: number) => `${Math.round(Math.max(0, Math.min(1, p)) * 100)}%`;
const scoreText = (s: number) => s.toFixed(Number.isInteger(s) ? 0 : 1);

/* ── Hover tooltip body for a wallet ───────────────────────────────── */

export function WalletTipBody({
  trader,
  group,
  rubric,
  footer,
  extra,
  siblings = 0,
}: {
  trader: Trader;
  group: Group;
  /** The page's rubric: the tooltip reads the trader's answers. */
  rubric: RubricDisplay;
  footer?: ReactNode;
  extra?: ReactNode;
  /** Other whale positions held by the same wallet in this market. */
  siblings?: number;
}) {
  const s = trader.spectrum;
  return (
    <>
      <div className="tip-title tr-tip-title">
        <span className="mono">{shortAddr(trader.id)}</span>
      </div>
      <div className="tr-tip-cat">
        <GroupKey group={group} />
        {group.label}
      </div>
      <TipRow value={fmtUSD(trader.position)} label="Position" />
      {extra}
      {s ? (
        <SpectrumTipRows rubric={rubric} s={s} />
      ) : (
        <>
          <TipRow value={fmtInt(trader.priorBets)} label="Prior bets" />
          <TipRow value={fmtPct(trader.concentration)} label="Portfolio concentration" />
          <TipRow value={trader.riskScore.toFixed(1)} label="Risk score / 100" />
        </>
      )}
      {siblings > 0 && (
        <div className="tip-sub">
          + {siblings} other whale position{siblings === 1 ? "" : "s"} from this wallet
        </div>
      )}
      {footer && <div className="tip-sub">{footer}</div>}
    </>
  );
}

/** Score, each counted question's likeliest level, and the choice questions' picks. */
export function SpectrumTipRows({ rubric, s }: { rubric: RubricDisplay; s: TraderSpectrum }) {
  return (
    <>
      <TipRow value={`${scoreText(s.score)} / ${rubric.scale[1]}`} label={rubric.name} />
      {rubric.questions.map((q) => {
        const a = answerOf(s, q);
        if (!a) return null;
        if (a.type === "score") {
          if (!(q.weight > 0)) return null;
          const best = likeliestLevel(q, a.p);
          return best ? <TipRow key={q.id} value={best.label} label={q.label} /> : null;
        }
        if (a.type === "choice") return <TipRow key={q.id} value={a.label} label={q.label} />;
        return null;
      })}
    </>
  );
}

/* ── Pinned details card ───────────────────────────────────────────── */

function Figure({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="tr-fig">
      <div className="tr-fig-label">{label}</div>
      <div className="tr-fig-value">{value}</div>
    </div>
  );
}

/** Address, group, the figures (or the rubric's breakdown), one action row. */
export function PinCard({
  trader,
  group,
  rubric,
  totalVolume,
  anchor,
  container,
  docked,
  onClose,
}: {
  trader: Trader;
  group: Group;
  /** The page's rubric: the card shows the score, every answer and the card factors. */
  rubric: RubricDisplay;
  totalVolume: number;
  /** Mark position in container px (floating mode). */
  anchor?: { x: number; y: number };
  container?: { width: number; height: number };
  /** Render in flow below the chart instead of floating over it. */
  docked?: boolean;
  onClose: () => void;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const [box, setBox] = useState({ w: 280, h: 240 });
  const s = trader.spectrum;

  useLayoutEffect(() => {
    if (!ref.current || docked) return;
    const r = ref.current.getBoundingClientRect();
    if (Math.abs(r.width - box.w) > 0.5 || Math.abs(r.height - box.h) > 0.5) setBox({ w: r.width, h: r.height });
  });

  // Move focus into the card so keyboard users land on its actions, and bring
  // it into view: a docked card (narrow screens) sits below the chart, and a
  // card pinned from the spectrum strip opens on the chart under it.
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const prev = document.activeElement instanceof HTMLElement || document.activeElement instanceof SVGElement ? document.activeElement : null;
    el.focus({ preventScroll: true });
    el.scrollIntoView({ block: "nearest", behavior: prefersReducedMotion() ? "auto" : "smooth" });
    return () => {
      // Closing returns focus to whatever opened the card (chart or tile), not <body>.
      const active = document.activeElement;
      if (prev && prev !== el && prev.isConnected && (!active || active === document.body || el.contains(active))) {
        prev.focus({ preventScroll: true });
      }
    };
  }, [trader, docked]);

  const style: CSSProperties = {};
  if (!docked && anchor && container) {
    const gap = 18;
    let left = anchor.x + gap;
    if (left + box.w > container.width - 6) left = anchor.x - gap - box.w;
    left = Math.max(6, Math.min(left, container.width - box.w - 6));
    let top = anchor.y - box.h / 2;
    top = Math.max(6, Math.min(top, container.height - box.h - 6));
    style.transform = `translate(${Math.round(left)}px, ${Math.round(top)}px)`;
    // A tall breakdown scrolls inside the chart rather than spilling past it.
    style.maxHeight = Math.max(200, container.height - 12);
  }

  const share = totalVolume > 0 ? (trader.position / totalVolume) * 100 : 0;
  const pctLabel = (v: number) => (v > 0 && v < 0.1 ? "<0.1%" : fmtPct(v));

  return (
    <div
      ref={ref}
      className={`tr-pin${docked ? " docked" : ""}${s ? " wide" : ""}`}
      style={style}
      role="dialog"
      aria-label={`Wallet ${trader.id}`}
      tabIndex={-1}
      onClick={(e) => e.stopPropagation()}
      onPointerDown={(e) => e.stopPropagation()}
    >
      <div className="tr-pin-head">
        <span className="tr-pin-cat">
          <GroupKey group={group} />
          {group.label}
        </span>
        <button type="button" className="icon-btn sm" onClick={onClose} aria-label="Close details" title="Close (Esc)">
          <svg width="14" height="14" viewBox="0 0 16 16" aria-hidden>
            <path d="M4 4l8 8M12 4l-8 8" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" />
          </svg>
        </button>
      </div>

      {/* One click selects the whole address. */}
      <code className="mono tr-pin-addr">{trader.id}</code>

      {s ? (
        <SpectrumBreakdown rubric={rubric} s={s} trader={trader} share={pctLabel(share)} />
      ) : (
        <div className="tr-figs">
          <Figure label="Position" value={fmtUSD(trader.position)} />
          <Figure label="Share of whale stake" value={pctLabel(share)} />
          <Figure label="Prior bets" value={fmtInt(trader.priorBets)} />
          <Figure label="Portfolio concentration" value={fmtPct(trader.concentration)} />
          <Figure
            label="Risk score"
            value={
              <>
                {trader.riskScore.toFixed(1)}
                <span className="tr-fig-unit"> / 100</span>
              </>
            }
          />
        </div>
      )}

      <div className="tr-pin-actions">
        <a className="btn btn-ghost btn-sm" href={polymarketProfile(trader.id)} target="_blank" rel="noopener noreferrer">
          Polymarket <span aria-hidden>↗</span>
        </a>
      </div>
    </div>
  );
}

/* ── The rubric's breakdown of one wallet ──────────────────────────── */

/** The score on the scale: the bands as one rail, an ink tick at the wallet's score. */
function ScoreRail({ rubric, score }: { rubric: RubricDisplay; score: number }) {
  const [lo, hi] = rubric.scale;
  const at = Math.max(0, Math.min(1, (score - lo) / (hi - lo || 1)));
  return (
    <div className="tr-rail" aria-hidden>
      <div className="tr-rail-bands">
        {rubric.bands.map((b) => (
          <span key={b.id} style={{ flexGrow: Math.max(0, b.max - b.min), background: bandColor(b) }} />
        ))}
      </div>
      <span className="tr-rail-tick" style={{ left: `${at * 100}%` }} />
    </div>
  );
}

function SpectrumBreakdown({ rubric, s, trader, share }: { rubric: RubricDisplay; s: TraderSpectrum; trader: Trader; share: string }) {
  const band = bandOf(rubric, s.score);
  const facts = rubric.factors.filter((f) => f.id in s.facts);
  return (
    <>
      <div className="tr-score">
        <div className="tr-score-main">
          <div className="tr-fig-label">{rubric.name}</div>
          <div className="tr-score-value tabular">
            {scoreText(s.score)}
            <span className="tr-fig-unit"> / {rubric.scale[1]}</span>
          </div>
        </div>
        <div className="tr-score-side">
          <div className="tr-fig-label">Position</div>
          <div className="tr-score-pos tabular">
            {fmtUSD(trader.position)}
            <span className="tr-fig-unit"> · {share}</span>
          </div>
        </div>
      </div>
      <ScoreRail rubric={rubric} score={s.score} />
      <span className="visually-hidden">{band.label}</span>

      <div className="tr-qs">
        {rubric.questions.map((q) => {
          const a = answerOf(s, q);
          if (!a) return null;
          if (a.type === "score") {
            const segs = levelSegments(rubric, q, a.p);
            const best = likeliestLevel(q, a.p);
            const spoken = segs.map((g) => `${g.label} ${pctText(g.width)}`).join(", ");
            return (
              <div key={q.id} className="tr-q">
                <span className="tr-q-label">{q.label}</span>
                <div className="tr-q-bar" role="img" aria-label={`${q.label}: ${spoken}`}>
                  {segs.map((g) =>
                    g.width > 0 ? (
                      <span
                        key={g.level}
                        className={best && g.level === best.level ? "top" : undefined}
                        style={{ flexGrow: g.width * 1000, background: g.color }}
                        title={`${g.label} · ${pctText(g.width)}`}
                      />
                    ) : null,
                  )}
                </div>
                <span className="tr-q-value">
                  {best ? (
                    <>
                      {best.label} <b className="tabular">{pctText(best.p)}</b>
                    </>
                  ) : null}
                </span>
              </div>
            );
          }
          if (a.type === "noul") {
            const yes = a.p >= 0.5;
            return (
              <div key={q.id} className="tr-q tr-q-line">
                <span className="tr-q-label">{q.label}</span>
                <span className="tr-q-value">
                  {yes ? "Yes" : "No"} <b className="tabular">{pctText(yes ? a.p : 1 - a.p)}</b>
                </span>
              </div>
            );
          }
          return (
            <div key={q.id} className="tr-q tr-q-line">
              <span className="tr-q-label">{q.label}</span>
              <span className="tr-q-value">
                {a.label} <b className="tabular">{pctText(a.p)}</b>
              </span>
            </div>
          );
        })}
      </div>

      {facts.length > 0 && (
        <dl className="tr-facts">
          {facts.map((f) => (
            <div key={f.id} className="tr-fact" title={`${f.label}: ${f.description}`}>
              <dt>{f.label}</dt>
              <dd className="tabular">{formatFact(s.facts[f.id], f.format)}</dd>
            </div>
          ))}
        </dl>
      )}
    </>
  );
}
