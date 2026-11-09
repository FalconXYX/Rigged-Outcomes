import { useLayoutEffect, useRef, useState, type CSSProperties, type ReactNode } from "react";
import type { Trader, TraderCategory } from "../../types";
import { TipRow } from "../../components/ui";
import { fmtInt, fmtPct, fmtUSD, polymarketProfile, shortAddr } from "../../lib/format";
import type { CategoryName } from "./model";
import { CAT_COLOR } from "./paint";
import { prefersReducedMotion } from "../../lib/useSize";

/* ── Category key (dot; critical gets a surface-colored core) ───────── */

export function CategoryKey({ category, size = 12 }: { category: TraderCategory; size?: number }) {
  const r = size * 0.38;
  return (
    <svg className="tr-key" width={size + 4} height={size} viewBox={`0 0 ${size + 4} ${size}`} aria-hidden>
      <circle cx={(size + 4) / 2} cy={size / 2} r={r} style={{ fill: CAT_COLOR[category] }} />
      {category === "critical" && <circle cx={(size + 4) / 2} cy={size / 2} r={r * 0.36} style={{ fill: "var(--surface)" }} />}
    </svg>
  );
}

/* ── Hover tooltip body for a wallet ───────────────────────────────── */

export function WalletTipBody({
  trader,
  names,
  footer,
  extra,
  siblings = 0,
}: {
  trader: Trader;
  names: Record<TraderCategory, CategoryName>;
  footer?: ReactNode;
  extra?: ReactNode;
  /** Other whale positions held by the same wallet in this market. */
  siblings?: number;
}) {
  return (
    <>
      <div className="tip-title tr-tip-title">
        <span className="mono">{shortAddr(trader.id)}</span>
      </div>
      <div className="tr-tip-cat">
        <CategoryKey category={trader.category} />
        {names[trader.category].label}
      </div>
      <TipRow value={fmtUSD(trader.position)} label="Position" />
      {extra}
      <TipRow value={fmtInt(trader.priorBets)} label="Prior bets" />
      <TipRow value={fmtPct(trader.concentration)} label="Portfolio concentration" />
      <TipRow value={trader.riskScore.toFixed(1)} label="Risk score / 100" />
      {siblings > 0 && (
        <div className="tip-sub">
          + {siblings} other whale position{siblings === 1 ? "" : "s"} from this wallet
        </div>
      )}
      {footer && <div className="tip-sub">{footer}</div>}
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

/** Address, category, five figures, one action row. */
export function PinCard({
  trader,
  names,
  totalVolume,
  anchor,
  container,
  docked,
  onClose,
}: {
  trader: Trader;
  names: Record<TraderCategory, CategoryName>;
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

  useLayoutEffect(() => {
    if (!ref.current || docked) return;
    const r = ref.current.getBoundingClientRect();
    if (Math.abs(r.width - box.w) > 0.5 || Math.abs(r.height - box.h) > 0.5) setBox({ w: r.width, h: r.height });
  });

  // Move focus into the card so keyboard users land on its actions; a docked
  // card (narrow screens) sits below the chart, so bring it into view.
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const prev = document.activeElement instanceof HTMLElement || document.activeElement instanceof SVGElement ? document.activeElement : null;
    el.focus({ preventScroll: true });
    if (docked) el.scrollIntoView({ block: "nearest", behavior: prefersReducedMotion() ? "auto" : "smooth" });
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
  }

  const share = totalVolume > 0 ? (trader.position / totalVolume) * 100 : 0;
  const pctLabel = (v: number) => (v > 0 && v < 0.1 ? "<0.1%" : fmtPct(v));

  return (
    <div
      ref={ref}
      className={`tr-pin${docked ? " docked" : ""}`}
      style={style}
      role="dialog"
      aria-label={`Wallet ${trader.id}`}
      tabIndex={-1}
      onClick={(e) => e.stopPropagation()}
      onPointerDown={(e) => e.stopPropagation()}
    >
      <div className="tr-pin-head">
        <span className="tr-pin-cat">
          <CategoryKey category={trader.category} />
          {names[trader.category].label}
        </span>
        <button type="button" className="icon-btn sm" onClick={onClose} aria-label="Close details" title="Close (Esc)">
          <svg width="14" height="14" viewBox="0 0 16 16" aria-hidden>
            <path d="M4 4l8 8M12 4l-8 8" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" />
          </svg>
        </button>
      </div>

      {/* One click selects the whole address. */}
      <code className="mono tr-pin-addr">{trader.id}</code>

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

      <div className="tr-pin-actions">
        <a className="btn btn-ghost btn-sm" href={polymarketProfile(trader.id)} target="_blank" rel="noopener noreferrer">
          Polymarket <span aria-hidden>↗</span>
        </a>
      </div>
    </div>
  );
}
