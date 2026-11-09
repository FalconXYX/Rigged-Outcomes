import { useEffect, useRef, useState } from "react";
import * as d3 from "d3";
import { prefersReducedMotion } from "../../lib/useSize";

/**
 * Smoothly animates a small vector of numbers toward `target` (cubic-out).
 * Instant under prefers-reduced-motion. The first value is the first target.
 */
export function useTweened(target: readonly number[], duration = 320): number[] {
  const [value, setValue] = useState<number[]>(() => [...target]);
  const cur = useRef<number[]>([...target]);
  const key = target.map((v) => (isFinite(v) ? v.toFixed(4) : String(v))).join(",");

  useEffect(() => {
    const from = cur.current;
    const to = [...target];
    if (from.length === to.length && from.every((v, i) => Math.abs(v - to[i]) < 1e-9)) return;
    if (prefersReducedMotion() || duration <= 0 || from.length !== to.length) {
      cur.current = to;
      setValue(to);
      return;
    }
    const timer = d3.timer((elapsed) => {
      const p = Math.min(1, elapsed / duration);
      const e = d3.easeCubicOut(p);
      const v = to.map((t, i) => from[i] + (t - from[i]) * e);
      cur.current = v;
      setValue(v);
      if (p >= 1) timer.stop();
    });
    return () => timer.stop();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, duration]);

  return value;
}

/** Returns `value` once it has stopped changing for `delay` ms. */
export function useSettled<T>(value: T, delay: number, equal: (a: T, b: T) => boolean = Object.is): T {
  const [settled, setSettled] = useState(value);
  const latest = useRef(value);
  latest.current = value;
  useEffect(() => {
    if (equal(settled, value)) return;
    const id = window.setTimeout(() => setSettled(latest.current), delay);
    return () => window.clearTimeout(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value, delay]);
  return settled;
}

/** Re-render once web fonts are ready (text measurement depends on them). */
export function useFontsReady(): boolean {
  const [ready, setReady] = useState(() => typeof document === "undefined" || document.fonts?.status === "loaded");
  useEffect(() => {
    if (ready || typeof document === "undefined" || !document.fonts) return;
    let live = true;
    document.fonts.ready.then(() => live && setReady(true));
    return () => {
      live = false;
    };
  }, [ready]);
  return ready;
}

/** matchMedia as state. */
export function useMedia(query: string): boolean {
  const [match, setMatch] = useState(() => typeof window !== "undefined" && window.matchMedia(query).matches);
  useEffect(() => {
    const mq = window.matchMedia(query);
    const on = () => setMatch(mq.matches);
    on();
    mq.addEventListener("change", on);
    return () => mq.removeEventListener("change", on);
  }, [query]);
  return match;
}

/** Follows `value`, but updates at most once every `ms` (leading + trailing). */
export function useThrottled<T>(value: T, ms: number, equal: (a: T, b: T) => boolean = Object.is): T {
  const [out, setOut] = useState(value);
  const last = useRef(0);
  const latest = useRef(value);
  latest.current = value;
  useEffect(() => {
    if (equal(out, value)) return;
    const now = performance.now();
    const wait = Math.max(0, last.current + ms - now);
    const id = window.setTimeout(() => {
      last.current = performance.now();
      setOut(latest.current);
    }, wait);
    return () => window.clearTimeout(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value, ms]);
  return out;
}

/** The zoom modifier key to name in hints (⌘ on Apple devices, Ctrl elsewhere). */
export const ZOOM_MOD = typeof navigator !== "undefined" && /Mac|iPhone|iPad|iPod/.test(navigator.userAgent) ? "⌘" : "Ctrl";
