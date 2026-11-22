// Browser-side helpers for the Traders charts: resolve CSS color tokens to RGB
// (so text inside a colored fill can pick ink vs. inverse by luminance),
// measure label widths before placing them, and re-run both when the theme
// or the web font changes.

import { useEffect, useMemo, useSyncExternalStore } from "react";
import type { TraderCategory } from "../../types";
import { FONT_SANS } from "../../components/ui";

export type RGB = [number, number, number];

/* ── Category tokens ──────────────────────────────────────────────── */

export const CAT_COLOR: Record<TraderCategory, string> = {
  normal: "var(--c-normal)",
  newAccount: "var(--c-new)",
  concentration: "var(--c-concentration)",
  critical: "var(--c-critical)",
};

/** A category color mixed into the surface (`mix` 0–1), as the browser paints it. */
export const catTint = (c: TraderCategory, mix: number) =>
  `color-mix(in oklab, ${CAT_COLOR[c]} ${Math.round(mix * 100)}%, var(--surface))`;

/** Dark surfaces take stronger tints, or the tile / band sinks into the page. */
const isDarkSurface = (surface: RGB) => luminance(surface) < 0.2;

/** Remainder tiles ("N more wallets"): a wash of the category under a hatch in the full color. */
export const restMix = (surface: RGB) => (isDarkSurface(surface) ? 0.34 : 0.24);

/** Category header bands: a clear tint, light enough to read apart from the solid wallet tiles. */
export const headerMix = (surface: RGB) => (isDarkSurface(surface) ? 0.3 : 0.2);

/* ── Theme version (data-theme attribute + OS scheme) ─────────────── */

let themeVersion = 0;
const themeListeners = new Set<() => void>();
let themeWired = false;

function wireTheme() {
  if (themeWired || typeof window === "undefined") return;
  themeWired = true;
  const bump = () => {
    themeVersion += 1;
    themeListeners.forEach((l) => l());
  };
  new MutationObserver(bump).observe(document.documentElement, {
    attributes: true,
    attributeFilter: ["data-theme", "class", "style"],
  });
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", bump);
  window.addEventListener("themechange", bump);
}

function subscribeTheme(cb: () => void) {
  wireTheme();
  themeListeners.add(cb);
  return () => themeListeners.delete(cb);
}

export const useThemeVersion = () => useSyncExternalStore(subscribeTheme, () => themeVersion, () => 0);

/* ── Token → RGB ──────────────────────────────────────────────────── */

let probe: HTMLSpanElement | null = null;
let px: CanvasRenderingContext2D | null = null;

function parseRGB(s: string): RGB | null {
  const m = s.match(/^rgba?\(\s*([\d.]+)[,\s]+([\d.]+)[,\s]+([\d.]+)/);
  if (m) return [+m[1], +m[2], +m[3]];
  const c = s.match(/^color\(srgb\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)/);
  if (c) return [+c[1] * 255, +c[2] * 255, +c[3] * 255];
  return null;
}

/**
 * The sRGB value the browser actually paints for any CSS color — a token,
 * `var(--x)`, or a `color-mix(...)` (whose computed value serializes as
 * oklab()/color() rather than rgb()). Resolved by the style engine on a
 * probe element, then normalized through a 1×1 canvas when the computed
 * string isn't plain rgb().
 */
export function resolveRGB(cssColor: string): RGB | null {
  if (typeof document === "undefined") return null;
  if (!probe) {
    probe = document.createElement("span");
    probe.setAttribute("aria-hidden", "true");
    probe.style.cssText = "position:absolute;width:0;height:0;overflow:hidden;pointer-events:none;";
    document.body.appendChild(probe);
  }
  probe.style.color = "";
  probe.style.color = cssColor;
  const computed = getComputedStyle(probe).color;
  const direct = parseRGB(computed);
  if (direct) return direct;
  if (!px) {
    const cv = document.createElement("canvas");
    cv.width = cv.height = 1;
    px = cv.getContext("2d", { willReadFrequently: true });
  }
  if (!px) return null;
  px.clearRect(0, 0, 1, 1);
  px.fillStyle = "#000";
  px.fillStyle = computed;
  px.fillRect(0, 0, 1, 1);
  const d = px.getImageData(0, 0, 1, 1).data;
  return [d[0], d[1], d[2]];
}

export interface Palette {
  surface: RGB;
  text: RGB;
  text2: RGB;
  inverse: RGB;
  /** Live sRGB of any CSS color string in the current theme (cached until the theme changes). */
  rgb: (cssColor: string) => RGB;
}

// Only used if a token cannot be resolved (no DOM): neutral greys, not palette copies.
const GREY: RGB = [128, 128, 128];
const FALLBACK = {
  surface: [255, 255, 255] as RGB,
  text: [0, 0, 0] as RGB,
  inverse: [255, 255, 255] as RGB,
};

/** Live RGB values of the tokens the treemap needs; recomputed on theme change. */
export function usePalette(): Palette {
  const v = useThemeVersion();
  return useMemo(() => {
    void v;
    const cache = new Map<string, RGB>();
    const rgb = (css: string) => {
      let hit = cache.get(css);
      if (!hit) {
        hit = resolveRGB(css) ?? GREY;
        cache.set(css, hit);
      }
      return hit;
    };
    const get = (token: string, fb: RGB) => resolveRGB(`var(${token})`) ?? fb;
    return {
      surface: get("--surface", FALLBACK.surface),
      text: get("--text", FALLBACK.text),
      text2: get("--text-2", FALLBACK.text),
      inverse: get("--text-inverse", FALLBACK.inverse),
      rgb,
    };
  }, [v]);
}

/* ── Luminance / contrast ─────────────────────────────────────────── */

const lin = (c: number) => {
  const s = c / 255;
  return s <= 0.04045 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
};

export const luminance = ([r, g, b]: RGB) => 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b);

export function contrast(a: RGB, b: RGB) {
  const la = luminance(a);
  const lb = luminance(b);
  return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05);
}

/**
 * Ink for text set inside a fill: whichever of the light / dark ink tokens
 * contrasts more with the painted fill. (--text and --text-inverse swap
 * between themes, so this always compares a near-white with a near-black.)
 */
export function inkOn(fill: RGB, pal: Palette): string {
  return contrast(fill, pal.text) >= contrast(fill, pal.inverse) ? "var(--text)" : "var(--text-inverse)";
}

/** Secondary ink inside a light tint: --text-2 while it clears 4.5:1, else the full ink. */
export function subInkOn(fill: RGB, pal: Palette): string {
  const ink = inkOn(fill, pal);
  return ink === "var(--text)" && contrast(fill, pal.text2) >= 4.5 ? "var(--text-2)" : ink;
}

/* ── Text measurement ─────────────────────────────────────────────── */

/** Canvas font string in the UI face (the same stack as --font-sans), so measured widths match what the SVG draws. */
export const font = (weight: number, size: number) => `${weight} ${size}px ${FONT_SANS}`;

let ctx: CanvasRenderingContext2D | null = null;
const widthCache = new Map<string, number>();

export function textWidth(text: string, cssFont: string): number {
  const k = `${cssFont}|${text}`;
  const hit = widthCache.get(k);
  if (hit !== undefined) return hit;
  if (!ctx && typeof document !== "undefined") ctx = document.createElement("canvas").getContext("2d");
  if (!ctx) return text.length * 7;
  ctx.font = cssFont;
  const w = ctx.measureText(text).width;
  widthCache.set(k, w);
  return w;
}

let fontVersion = 0;
const fontListeners = new Set<() => void>();
let fontsWired = false;

function subscribeFonts(cb: () => void) {
  fontListeners.add(cb);
  if (!fontsWired && typeof document !== "undefined" && document.fonts) {
    fontsWired = true;
    const bump = () => {
      widthCache.clear();
      fontVersion += 1;
      fontListeners.forEach((l) => l());
    };
    document.fonts.ready.then(bump);
    document.fonts.addEventListener("loadingdone", bump);
  }
  return () => fontListeners.delete(cb);
}

/** Re-render when web fonts finish loading (label widths change). */
export const useFontVersion = () => useSyncExternalStore(subscribeFonts, () => fontVersion, () => 0);

/* ── Misc ─────────────────────────────────────────────────────────── */

/** Modifier key that turns a wheel over a chart into a zoom (⌘ on Apple platforms; Ctrl works everywhere). */
export const ZOOM_MOD = typeof navigator !== "undefined" && /Mac|iPhone|iPad|iPod/.test(navigator.userAgent) ? "⌘" : "Ctrl";

/** Calls `onEscape` on Escape anywhere on the page. */
export function useEscape(onEscape: () => void) {
  useEffect(() => {
    const h = (e: KeyboardEvent) => {
      if (e.key === "Escape") onEscape();
    };
    window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  }, [onEscape]);
}
