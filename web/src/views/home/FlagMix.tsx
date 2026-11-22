import type { CSSProperties } from "react";
import type { MarketPreview, TraderCategory } from "../../types";

/** Same four categories, order and colors as the Flagged-traders tab. */
export const FLAG_CATS: { key: TraderCategory; label: string; color: string }[] = [
  { key: "normal", label: "Normal", color: "var(--c-normal)" },
  { key: "newAccount", label: "New account", color: "var(--c-new)" },
  { key: "concentration", label: "Concentration", color: "var(--c-concentration)" },
  { key: "critical", label: "Both flags", color: "var(--c-critical)" },
];

function shareLabel(s: number) {
  if (s < 0.005) return "<1%";
  return `${Math.round(s * 100)}%`;
}

/**
 * Preview of the Flagged-traders tab: whale capital split by flag category as
 * one stacked bar with a compact key (non-zero categories only). Renders an
 * empty slot (keeping the card's row alignment) when the market has no trader data.
 */
export function FlagMix({ mix }: { mix: MarketPreview["flagMix"] }) {
  const total = mix ? FLAG_CATS.reduce((s, c) => s + Math.max(0, mix[c.key] ?? 0), 0) : 0;
  if (!mix || !(total > 0)) return <div className="fm is-empty" aria-hidden />;

  const parts = FLAG_CATS.map((c) => ({ ...c, share: Math.max(0, mix[c.key] ?? 0) / total })).filter((p) => p.share > 0);
  return (
    <div className="fm">
      <p className="label" aria-hidden>
        Whale capital by flag
      </p>
      <div className="fm-bar" aria-hidden>
        {parts.map((p) => (
          <span key={p.key} className="fm-seg" style={{ flexGrow: p.share * 1000, background: p.color }} />
        ))}
      </div>
      <ul className="fm-legend" aria-label="Whale capital by flag">
        {parts.map((p) => (
          <li key={p.key}>
            <span className="key-dot" style={{ "--key": p.color } as CSSProperties} aria-hidden />
            {p.label}
            <b className="tabular">{shareLabel(p.share)}</b>
          </li>
        ))}
      </ul>
    </div>
  );
}
