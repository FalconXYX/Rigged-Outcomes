import { useLayoutEffect, useRef, type CSSProperties, type Ref } from "react";
import type { Whale } from "../../types";
import { TipRow } from "../../components/ui";
import { fmtInt, fmtT, fmtUSD, polymarketProfile, shortAddr } from "../../lib/format";
import { prefersReducedMotion } from "../../lib/useSize";

export const whaleColor = (w: Whale) => (w.label ? "var(--c-highlight)" : "var(--c-whale)");
export const fmtOdds = (v: number) => (v == null || !isFinite(v) ? "—" : v.toFixed(2));

/** Hover tooltip body for one whale position (same fields as the Dash hover). */
export function WhaleTip({ w, hint = "click to pin" }: { w: Whale; hint?: string }) {
  return (
    <>
      <div className="tip-title tl-tip-title">
        <span className="key-dot" style={{ "--key": whaleColor(w) } as CSSProperties} aria-hidden />
        {/* mono: the UI font's contextual alternates would draw "0x" as "0×" */}
        {w.label ? <span>{w.label}</span> : <span className="mono tl-tip-addr">{shortAddr(w.id)}</span>}
      </div>
      <TipRow value={fmtUSD(w.dollar)} label="Bet" />
      <TipRow value={fmtOdds(w.odds)} label="Odds" />
      <TipRow value={fmtUSD(w.profit)} label="Net profit" />
      <TipRow value={w.outcome} label="Outcome" />
      {w.hedged && <TipRow value={`${fmtInt(w.contracts)} contracts`} label="fully hedged" />}
      <div className="tip-sub">
        {fmtT(w.t)} · <span className="tl-tip-hint">{hint}</span>
      </div>
    </>
  );
}

/**
 * Details card for a pinned whale dot: floats over the chart (`style` places
 * it) or, `docked`, sits in flow below it on narrow screens. Label (or the
 * short address), the full address, four figures, two links.
 */
export function PinnedWhale({
  w,
  onClose,
  style,
  docked = false,
  ref,
}: {
  w: Whale;
  onClose: () => void;
  style?: CSSProperties;
  docked?: boolean;
  ref?: Ref<HTMLDivElement>;
}) {
  const own = useRef<HTMLDivElement>(null);

  // Focus moves into the card so keyboard users land on its actions (as in the
  // traders view); a docked card is brought into view. Closing returns focus
  // to whatever opened it (the chart), not <body>.
  useLayoutEffect(() => {
    const el = own.current;
    if (!el) return;
    const prev = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    el.focus({ preventScroll: true });
    if (docked) el.scrollIntoView({ block: "nearest", behavior: prefersReducedMotion() ? "auto" : "smooth" });
    return () => {
      const activeEl = document.activeElement;
      if (prev && prev !== el && prev.isConnected && (!activeEl || activeEl === document.body || el.contains(activeEl))) {
        prev.focus({ preventScroll: true });
      }
    };
  }, [w, docked]);

  const setRefs = (el: HTMLDivElement | null) => {
    own.current = el;
    if (typeof ref === "function") ref(el);
    else if (ref) ref.current = el;
  };

  return (
    <div
      ref={setRefs}
      className={`tl-pin${docked ? " docked" : ""}`}
      style={{ "--key": whaleColor(w), ...style } as CSSProperties}
      tabIndex={-1}
      role="dialog"
      aria-label={`Position details for ${w.label ?? w.id}`}
      onPointerDown={(e) => e.stopPropagation()}
      onClick={(e) => e.stopPropagation()}
    >
      <div className="tl-pin-head">
        {/* mono for the short address: the UI font's alternates would draw "0x" as "0×" */}
        <div className="tl-pin-name">{w.label ?? <span className="mono">{shortAddr(w.id)}</span>}</div>
        <button type="button" className="icon-btn sm tl-pin-close" onClick={onClose} aria-label="Close details" title="Close (Esc)">
          <svg width="14" height="14" viewBox="0 0 14 14" aria-hidden>
            <path d="M3.5 3.5l7 7M10.5 3.5l-7 7" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
          </svg>
        </button>
      </div>
      <code className="mono tl-pin-addr">{w.id}</code>

      <dl className="tl-pin-grid">
        <div>
          <dt>Bet</dt>
          <dd className="tabular">{fmtUSD(w.dollar)}</dd>
        </div>
        <div>
          <dt>Odds</dt>
          <dd className="tabular">{fmtOdds(w.odds)}</dd>
        </div>
        <div>
          <dt>Net profit</dt>
          <dd className="tabular">{fmtUSD(w.profit)}</dd>
        </div>
        <div>
          <dt>Outcome</dt>
          <dd>
            {w.outcome}
            {w.hedged && <span className="tl-pin-note"> · hedged</span>}
          </dd>
        </div>
      </dl>

      <a className="tl-pin-link" href={polymarketProfile(w.id)} target="_blank" rel="noopener noreferrer">
        Polymarket <span aria-hidden>↗</span>
      </a>
    </div>
  );
}
