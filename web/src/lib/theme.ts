import { useSyncExternalStore } from "react";
import { flushSync } from "react-dom";
import { prefersReducedMotion } from "./useSize";

// Theme preference: "system" follows prefers-color-scheme; "light"/"dark" pin
// it by setting <html data-theme>, which tokens.css keys off. The same logic
// runs inline in index.html before first paint so there is no flash; keep the
// storage key and attribute in sync with that script.

export type ThemePref = "system" | "light" | "dark";
export type ResolvedTheme = "light" | "dark";

export const THEME_STORAGE_KEY = "ro-theme";

/** Fired on window whenever the resolved theme may have changed (for canvas charts). */
export const THEME_EVENT = "themechange";

function readStored(): ThemePref {
  try {
    const v = window.localStorage.getItem(THEME_STORAGE_KEY);
    if (v === "light" || v === "dark") return v;
  } catch {
    /* storage blocked: fall back to system */
  }
  return "system";
}

function writeStored(pref: ThemePref) {
  try {
    if (pref === "system") window.localStorage.removeItem(THEME_STORAGE_KEY);
    else window.localStorage.setItem(THEME_STORAGE_KEY, pref);
  } catch {
    /* storage blocked: preference lasts for this page view only */
  }
}

const darkQuery = () => window.matchMedia("(prefers-color-scheme: dark)");

let pref: ThemePref = typeof window === "undefined" ? "system" : readStored();
const listeners = new Set<() => void>();

export function resolveTheme(p: ThemePref = pref): ResolvedTheme {
  if (p !== "system") return p;
  return darkQuery().matches ? "dark" : "light";
}

/** Point <meta name="theme-color"> at the live --page token. */
function syncThemeColor() {
  const meta = document.querySelector<HTMLMetaElement>('meta[name="theme-color"]');
  if (!meta) return;
  const page = getComputedStyle(document.documentElement).getPropertyValue("--page").trim();
  if (page) meta.content = page;
}

function apply(p: ThemePref) {
  const root = document.documentElement;
  if (p === "system") delete root.dataset.theme;
  else root.dataset.theme = p;
  syncThemeColor();
}

function notify() {
  listeners.forEach((l) => l());
  window.dispatchEvent(new CustomEvent(THEME_EVENT, { detail: resolveTheme() }));
}

export function setTheme(next: ThemePref) {
  if (next === pref) return;
  const commit = () => {
    pref = next;
    writeStored(next);
    apply(next);
    // Re-render subscribers synchronously so a view transition captures the new icon too.
    flushSync(notify);
  };
  const doc = document as Document & { startViewTransition?: (cb: () => void) => unknown };
  if (doc.startViewTransition && !prefersReducedMotion()) doc.startViewTransition(commit);
  else commit();
}

/**
 * Every click flips what the visitor sees: go to the opposite of the theme on
 * screen, and when that opposite is what the OS asks for, follow the OS again
 * ("system") instead of pinning it. So a light-OS visitor toggles
 * system(light) ⇄ dark, and a dark-OS visitor system(dark) ⇄ light.
 */
export function nextTheme(p: ThemePref): ThemePref {
  const opposite: ResolvedTheme = resolveTheme(p) === "dark" ? "light" : "dark";
  return opposite === resolveTheme("system") ? "system" : opposite;
}

let started = false;

/** Call once before the first render: applies the stored theme and wires OS/tab changes. */
export function initTheme() {
  if (started || typeof window === "undefined") return;
  started = true;
  apply(pref);
  darkQuery().addEventListener("change", () => {
    if (pref !== "system") return;
    syncThemeColor();
    notify();
  });
  // Another tab changed the preference.
  window.addEventListener("storage", (e) => {
    if (e.key !== THEME_STORAGE_KEY && e.key !== null) return;
    pref = readStored();
    apply(pref);
    notify();
  });
}

function subscribe(cb: () => void) {
  listeners.add(cb);
  return () => listeners.delete(cb);
}

// Snapshot is a primitive so an OS flip in "system" mode still re-renders.
const snapshot = () => `${pref}:${resolveTheme()}`;

export function useTheme() {
  const snap = useSyncExternalStore(subscribe, snapshot, () => "system:light");
  const [p, resolved] = snap.split(":") as [ThemePref, ResolvedTheme];
  return { pref: p, resolved, next: nextTheme(p), setTheme, cycle: () => setTheme(nextTheme(p)) };
}
