import { useId, useLayoutEffect, useRef, useState, type CSSProperties, type KeyboardEvent, type ReactNode } from "react";
import "./ui.css";

/**
 * The UI face for canvas / SVG measurement strings (`${weight} ${px}px ${FONT_SANS}`).
 * Mirrors --font-sans in tokens.css; CSS should use the token, never this.
 */
export const FONT_SANS = `"Montserrat Variable", Montserrat, ui-sans-serif, system-ui, sans-serif`;

/** Inline style carrying a key color as `--key` (keyed marks read it: rules, dots, icons, meter fills). */
export const keyStyle = (color: string | undefined): CSSProperties | undefined =>
  color ? ({ "--key": color } as CSSProperties) : undefined;

/* ── Stat tile ─────────────────────────────────────────────────────────── */

/**
 * One cell of a `.stat-strip` (a ruled row; `.stat-grid` is the old name).
 * With `color` a 6px dot in that series hue sits before the label (the key);
 * surface, border and text never take the hue. Without `color` it's plain.
 */
export function StatTile({
  label,
  value,
  color,
  hint,
  icon,
  emphasis = false,
  className,
}: {
  label: string;
  value: ReactNode;
  /** CSS color (usually a var(--c-*) token) for the identity key; omit for neutral. */
  color?: string;
  hint?: string;
  /** Optional 12px line icon before the label, drawn in the key color (replaces the dot). */
  icon?: ReactNode;
  /** The one figure a set leads with: value at 700. */
  emphasis?: boolean;
  className?: string;
}) {
  const keyed = Boolean(color) && !icon;
  const cls = ["stat", keyed && "keyed", emphasis && "emph", className].filter(Boolean).join(" ");
  return (
    <div className={cls} title={hint} style={keyStyle(color)}>
      <div className="stat-label">
        {icon ? (
          <span className="stat-icon" aria-hidden>
            {icon}
          </span>
        ) : (
          keyed && <span className="key-dot" aria-hidden />
        )}
        <span className="stat-label-text">{label}</span>
      </div>
      <div className="stat-value">{value}</div>
    </div>
  );
}

/* ── Segmented control ─────────────────────────────────────────────────── */

export function Segmented<T extends string>({
  value,
  options,
  onChange,
  label,
}: {
  value: T;
  options: { value: T; label: string }[];
  onChange: (v: T) => void;
  label: string;
}) {
  const group = useRef<HTMLDivElement>(null);
  const current = options.findIndex((o) => o.value === value);
  // ARIA radio-group pattern: one Tab stop (the checked option), arrow keys
  // move the selection and focus together, wrapping at the ends.
  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    const n = options.length;
    if (!n) return;
    const from = current < 0 ? 0 : current;
    let to: number;
    if (e.key === "ArrowRight" || e.key === "ArrowDown") to = (from + 1) % n;
    else if (e.key === "ArrowLeft" || e.key === "ArrowUp") to = (from - 1 + n) % n;
    else if (e.key === "Home") to = 0;
    else if (e.key === "End") to = n - 1;
    else return;
    e.preventDefault();
    group.current?.querySelectorAll<HTMLButtonElement>('[role="radio"]')[to]?.focus();
    if (to !== current) onChange(options[to].value);
  };
  return (
    <div ref={group} className="segmented" role="radiogroup" aria-label={label} onKeyDown={onKeyDown}>
      {options.map((o, i) => (
        <button
          key={o.value}
          type="button"
          role="radio"
          aria-checked={o.value === value}
          tabIndex={i === (current < 0 ? 0 : current) ? 0 : -1}
          className={o.value === value ? "on" : undefined}
          onClick={() => onChange(o.value)}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

/* ── Switch ────────────────────────────────────────────────────────────── */

export function Switch({
  checked,
  onChange,
  label,
}: {
  checked: boolean;
  onChange: (v: boolean) => void;
  label: string;
}) {
  return (
    <label className="switch">
      <input type="checkbox" role="switch" checked={checked} onChange={(e) => onChange(e.target.checked)} />
      <span className="switch-track" aria-hidden>
        <span className="switch-thumb" />
      </span>
      <span className="switch-label">{label}</span>
    </label>
  );
}

/* ── Select (native, styled) ───────────────────────────────────────────── */

export function Select<T extends string>({
  value,
  options,
  onChange,
  label,
  placeholder,
}: {
  value: T | "";
  options: { value: T; label: string }[];
  onChange: (v: T) => void;
  label: string;
  placeholder?: string;
}) {
  return (
    <label className="select">
      <span className="visually-hidden">{label}</span>
      <select value={value} onChange={(e) => onChange(e.target.value as T)} aria-label={label}>
        {placeholder && (
          <option value="" disabled>
            {placeholder}
          </option>
        )}
        {options.map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
      </select>
      <svg className="select-chevron" width="12" height="12" viewBox="0 0 12 12" aria-hidden>
        <path d="M3 4.5 6 7.5 9 4.5" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
    </label>
  );
}

/* ── Button ────────────────────────────────────────────────────────────── */

export function Button({
  children,
  onClick,
  variant = "ghost",
  size,
  title,
  disabled,
  className,
}: {
  children: ReactNode;
  onClick?: () => void;
  /** ghost = hairline surface; solid = ink fill (one per view). "brand" is kept as an alias of solid. */
  variant?: "ghost" | "solid" | "brand";
  /** sm = 12px text, 3px 8px padding (popover rows, card corners). */
  size?: "sm";
  title?: string;
  disabled?: boolean;
  className?: string;
}) {
  const cls = ["btn", `btn-${variant}`, size === "sm" && "btn-sm", className].filter(Boolean).join(" ");
  return (
    <button type="button" className={cls} onClick={onClick} title={title} disabled={disabled}>
      {children}
    </button>
  );
}

/* ── Toolbar: every page's sort / filter controls, collapsed behind one button ── */

const FilterGlyph = () => (
  <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" aria-hidden>
    <path d="M2 4h12M4.5 8h7M7 12h2" />
  </svg>
);

/**
 * One flat "Filter" button; the bar of controls pushes out underneath it and
 * is otherwise not on the page at all. Nothing is applied by default: `active`
 * is true only when the visitor has changed something (a dot on the button and
 * a Clear link in the bar say so). Starts open only when `active` on mount
 * (a shared link that already carries a sort or filter), or `defaultOpen`.
 *
 *   <Toolbar label="Filter" active={active} onClear={clear} aria-label="Filter wallets">
 *     <ToolbarGroup label="Sort"><Segmented … /></ToolbarGroup>
 *     <ToolbarGroup><Switch … /></ToolbarGroup>
 *   </Toolbar>
 */
export function Toolbar({
  label = "Filter",
  active = false,
  defaultOpen,
  onClear,
  children,
  "aria-label": ariaLabel,
}: {
  label?: string;
  /** A sort or filter is applied (not the default view). */
  active?: boolean;
  defaultOpen?: boolean;
  /** Resets to the default view; the Clear link shows while `active`. */
  onClear?: () => void;
  children: ReactNode;
  "aria-label"?: string;
}) {
  const [open, setOpen] = useState(() => defaultOpen ?? active);
  const id = `tb${useId().replace(/[^a-zA-Z0-9_-]/g, "")}`;
  return (
    <div className="toolbar">
      <button
        type="button"
        className={`toolbar-btn${open ? " is-open" : ""}${active ? " is-active" : ""}`}
        aria-expanded={open}
        aria-controls={id}
        onClick={() => setOpen((o) => !o)}
      >
        <FilterGlyph />
        {label}
        {active && <span className="toolbar-dot" aria-hidden />}
      </button>
      {open && (
        <div id={id} className="toolbar-bar" role="group" aria-label={ariaLabel ?? label}>
          {children}
          {active && onClear && (
            <button type="button" className="toolbar-clear" onClick={onClear}>
              Clear
            </button>
          )}
        </div>
      )}
    </div>
  );
}

/** One labelled control inside the bar ("Sort" + a Segmented); omit `label` for a Switch. */
export function ToolbarGroup({ label, children }: { label?: string; children: ReactNode }) {
  return (
    <div className="toolbar-group">
      {label && <span className="toolbar-label">{label}</span>}
      {children}
    </div>
  );
}

/* ── Legend ────────────────────────────────────────────────────────────── */

export type LegendShape = "dot" | "line" | "rect" | "dash";

export interface LegendItem {
  key: string;
  label: string;
  color: string;
  shape: LegendShape;
  /** false = series hidden (rendered struck/dimmed). Omit for non-toggleable. */
  active?: boolean;
}

export function Legend({ items, onToggle }: { items: LegendItem[]; onToggle?: (key: string) => void }) {
  return (
    <ul className="legend">
      {items.map((it) => {
        const key = <LegendKey shape={it.shape} color={it.color} />;
        const off = it.active === false;
        return (
          <li key={it.key}>
            {onToggle && it.active !== undefined ? (
              <button
                type="button"
                className={`legend-item keyed${off ? " off" : ""}`}
                style={keyStyle(it.color)}
                aria-pressed={!off}
                onClick={() => onToggle(it.key)}
                title={off ? `Show ${it.label}` : `Hide ${it.label}`}
              >
                {key}
                {it.label}
              </button>
            ) : (
              <span className="legend-item">
                {key}
                {it.label}
              </span>
            )}
          </li>
        );
      })}
    </ul>
  );
}

export function LegendKey({ shape, color }: { shape: LegendShape; color: string }) {
  return (
    <svg className="legend-key" width="16" height="12" viewBox="0 0 16 12" aria-hidden>
      {shape === "dot" && <circle cx="8" cy="6" r="4.5" style={{ fill: color }} />}
      {shape === "line" && <line x1="1" x2="15" y1="6" y2="6" strokeWidth="2.5" strokeLinecap="round" style={{ stroke: color }} />}
      {shape === "dash" && (
        <line x1="1" x2="15" y1="6" y2="6" strokeWidth="2" strokeDasharray="2 2.5" style={{ stroke: color }} />
      )}
      {shape === "rect" && <rect x="2" y="1.5" width="12" height="9" rx="2.5" style={{ fill: color }} />}
    </svg>
  );
}

/* ── Tooltip ───────────────────────────────────────────────────────────── */

/**
 * Floating tooltip positioned inside a `position: relative` chart container.
 * (x, y) is the anchor in container pixels; the tooltip flips to stay inside
 * the container bounds. Render children as plain JSX (React escapes text).
 */
export function Tooltip({
  x,
  y,
  visible,
  children,
  containerWidth,
  containerHeight,
  accent,
}: {
  x: number;
  y: number;
  visible: boolean;
  children: ReactNode;
  containerWidth: number;
  containerHeight?: number;
  /** Optional 2px top rule keyed to the hovered series: a CSS color (var(--c-*)); "brand" maps to --accent. */
  accent?: string;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const [box, setBox] = useState({ w: 0, h: 0 });

  useLayoutEffect(() => {
    if (!ref.current) return;
    const r = ref.current.getBoundingClientRect();
    if (Math.abs(r.width - box.w) > 0.5 || Math.abs(r.height - box.h) > 0.5) setBox({ w: r.width, h: r.height });
  });

  const gap = 14;
  let left = x + gap;
  if (left + box.w > containerWidth - 4) left = x - gap - box.w;
  left = Math.max(4, left);
  let top = y - box.h - gap;
  if (top < 4) top = y + gap;
  if (containerHeight && top + box.h > containerHeight - 4) top = Math.max(4, containerHeight - box.h - 4);

  return (
    <div
      ref={ref}
      className={`tooltip${visible ? " show" : ""}${accent ? " has-accent" : ""}`}
      style={{
        transform: `translate(${Math.round(left)}px, ${Math.round(top)}px)`,
        ...(accent
          ? ({ "--tip-accent": `linear-gradient(${accent === "brand" ? "var(--accent)" : accent} 0 0)` } as CSSProperties)
          : null),
      }}
      role="status"
      aria-live="polite"
    >
      {children}
    </div>
  );
}

/** One readout row: value leads, label follows, keyed by a short stroke. */
export function TipRow({ color, value, label, shape = "line" }: { color?: string; value: ReactNode; label: ReactNode; shape?: LegendShape }) {
  return (
    <div className="tip-row">
      {color ? <LegendKey shape={shape} color={color} /> : <span className="tip-nokey" />}
      <span className="tip-value">{value}</span>
      <span className="tip-label">{label}</span>
    </div>
  );
}

/* ── Loading / error ───────────────────────────────────────────────────── */

export function Loading({ label = "Loading…" }: { label?: string }) {
  return (
    <div className="loading" role="status">
      <span className="spinner" aria-hidden />
      {label}
    </div>
  );
}

export function ErrorNote({
  error,
  title = "Couldn’t load data.",
  children,
}: {
  error: Error;
  title?: string;
  /** Optional actions shown under the message (e.g. a way back home). */
  children?: ReactNode;
}) {
  return (
    <div className="error-note" role="alert">
      <strong>{title}</strong> {error.message}
      {children && <div className="error-actions">{children}</div>}
    </div>
  );
}
