import type { CSSProperties } from "react";
import type { IndexSpectrum, MarketPreview, TraderCategory } from "../../types";

/** Same four categories, order and colors as the Flagged-traders tab's cut-offs view. */
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

/** A rubric colour token ("--c-new") as CSS. */
const tokenColor = (c: string) => (c.startsWith("--") ? `var(${c})` : c);

interface Part {
  key: string;
  label: string;
  color: string;
  value: number;
}

/**
 * Preview of the Flagged-traders tab: whale capital split as one stacked bar
 * with a compact key (non-zero parts only). A scored market splits by the
 * insider score's bands (labels and colours from the rubric, via the index
 * entry), any other by today's flag categories. Renders an empty slot
 * (keeping the card's row alignment) when the market has no trader data.
 */
export function FlagMix({ mix, spectrum }: { mix: MarketPreview["flagMix"]; spectrum?: IndexSpectrum | null }) {
  const scored = !!spectrum && spectrum.bands.some((b) => b.stake > 0);
  const all: Part[] = scored
    ? spectrum!.bands.map((b) => ({ key: b.id, label: b.label, color: tokenColor(b.color), value: Math.max(0, b.stake) }))
    : mix
      ? FLAG_CATS.map((c) => ({ key: c.key, label: c.label, color: c.color, value: Math.max(0, mix[c.key] ?? 0) }))
      : [];
  const total = all.reduce((s, p) => s + p.value, 0);
  if (!(total > 0)) return <div className="fm is-empty" aria-hidden />;

  const title = scored ? `Whale capital by ${spectrum!.name}` : "Whale capital by flag";
  const parts = all.map((p) => ({ ...p, share: p.value / total })).filter((p) => p.share > 0);
  return (
    <div className="fm">
      <p className="label" aria-hidden>
        {title}
      </p>
      <div className="fm-bar" aria-hidden>
        {parts.map((p) => (
          <span key={p.key} className="fm-seg" style={{ flexGrow: p.share * 1000, background: p.color }} />
        ))}
      </div>
      <ul className="fm-legend" aria-label={title}>
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
