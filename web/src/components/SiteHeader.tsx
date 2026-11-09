import { useId } from "react";
import { href } from "../lib/router";
import { resolveTheme, useTheme, type ThemePref } from "../lib/theme";
import "./shell.css";

export const REPO_URL = "https://github.com/FalconXYX/Rigged-Outcomes";
export const SITE_URL = "https://parthjain.ca";

/** Gradient tile with a flat line that breaks upward: the bet before the news. 22px, near-square corners. */
export function BrandMark({ size = 22 }: { size?: number }) {
  // useId output contains ":" / "«" — strip to a plain fragment id for url(#…).
  const id = `bm${useId().replace(/[^a-zA-Z0-9_-]/g, "")}`;
  return (
    <svg className="brand-mark" width={size} height={size} viewBox="0 0 32 32" aria-hidden>
      <defs>
        <linearGradient id={id} x1="0" y1="0" x2="32" y2="32" gradientUnits="userSpaceOnUse">
          <stop offset="0" style={{ stopColor: "var(--brand-a)" }} />
          <stop offset="1" style={{ stopColor: "var(--brand-b)" }} />
        </linearGradient>
      </defs>
      <rect width="32" height="32" rx="4" style={{ fill: `url(#${id})` }} />
      <path className="brand-mark-ink" d="M6.5 22.5h7l2.6-2.6 2.4 2.1 5.2-9.5" />
      <circle className="brand-mark-dot" cx="24.5" cy="10" r="2.6" />
    </svg>
  );
}

export function GitHubIcon({ size = 18 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" aria-hidden fill="currentColor">
      <path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.01 8.01 0 0 0 16 8c0-4.42-3.58-8-8-8Z" />
    </svg>
  );
}

const THEME_LABEL: Record<ThemePref, string> = { system: "System", light: "Light", dark: "Dark" };

function ThemeIcon({ pref }: { pref: ThemePref }) {
  const common = {
    width: 18,
    height: 18,
    viewBox: "0 0 24 24",
    fill: "none",
    stroke: "currentColor",
    strokeWidth: 1.9,
    strokeLinecap: "round" as const,
    strokeLinejoin: "round" as const,
    "aria-hidden": true,
  };
  if (pref === "light")
    return (
      <svg {...common} className="theme-icon" key="light">
        <circle cx="12" cy="12" r="4.2" />
        <path d="M12 2.5v2.2M12 19.3v2.2M4.6 4.6l1.6 1.6M17.8 17.8l1.6 1.6M2.5 12h2.2M19.3 12h2.2M4.6 19.4l1.6-1.6M17.8 6.2l1.6-1.6" />
      </svg>
    );
  if (pref === "dark")
    return (
      <svg {...common} className="theme-icon" key="dark">
        <path d="M20.2 14.6A8.4 8.4 0 0 1 9.4 3.8a8.4 8.4 0 1 0 10.8 10.8Z" />
      </svg>
    );
  // system: follow the device
  return (
    <svg {...common} className="theme-icon" key="system">
      <rect x="3" y="4.2" width="18" height="12.4" rx="2.2" />
      <path d="M8.5 20.3h7M12 16.6v3.7" />
    </svg>
  );
}

function ThemeToggle() {
  const { pref, resolved, next, cycle } = useTheme();
  // `next` is always the opposite of what's on screen ("system" when the OS already asks for it).
  const target = next === "system" ? `${resolveTheme("system")} (follow system)` : next;
  const label = `Theme: ${THEME_LABEL[pref]}${pref === "system" ? ` (${resolved})` : ""}. Switch to ${target}.`;
  return (
    <button type="button" className="icon-btn" onClick={cycle} aria-label={label} title={label}>
      <ThemeIcon pref={pref} />
    </button>
  );
}

export function SiteHeader() {
  return (
    <header className="site-header">
      <div className="container site-header-inner">
        <a className="brand" href={href.home()} aria-label="Rigged Outcomes — all markets">
          <BrandMark />
          <span className="brand-name">Rigged Outcomes</span>
        </a>
        <nav className="site-nav" aria-label="Site">
          <a className="icon-btn" href={REPO_URL} target="_blank" rel="noopener noreferrer" aria-label="Source code on GitHub" title="Source code on GitHub">
            <GitHubIcon />
          </a>
          <ThemeToggle />
        </nav>
      </div>
    </header>
  );
}
