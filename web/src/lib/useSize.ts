import { useLayoutEffect, useRef, useState, useSyncExternalStore } from "react";

/** Tracks an element's content-box size with ResizeObserver. */
export function useSize<T extends HTMLElement = HTMLDivElement>() {
  const ref = useRef<T>(null);
  const [size, setSize] = useState({ width: 0, height: 0 });

  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const measure = () => {
      const r = el.getBoundingClientRect();
      setSize((s) =>
        Math.round(s.width) === Math.round(r.width) && Math.round(s.height) === Math.round(r.height)
          ? s
          : { width: Math.round(r.width), height: Math.round(r.height) },
      );
    };
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  return [ref, size] as const;
}

function subscribeResize(cb: () => void) {
  window.addEventListener("resize", cb);
  return () => window.removeEventListener("resize", cb);
}

/** The window's inner height, re-read on resize. */
export const useViewportHeight = () => useSyncExternalStore(subscribeResize, () => window.innerHeight, () => 900);

/** True when the user asked for reduced motion. */
export const prefersReducedMotion = () =>
  typeof window !== "undefined" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
