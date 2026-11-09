import { useCallback, useLayoutEffect, useRef } from "react";
import { prefersReducedMotion } from "../../lib/useSize";

/*
 * FLIP reorder for a keyed list. Call `snapshot()` right before the state
 * change that reorders the items (it records where every item is now); after
 * React commits the new order, the layout effect measures again and slides
 * each item from its old spot to its new one with the Web Animations API, so
 * nothing lingers in inline styles. Under reduced motion the snapshot is a
 * no-op and items simply appear in place.
 */

const EASE = "cubic-bezier(0.2, 0.7, 0.2, 1)"; // = --ease (WAAPI cannot read CSS variables)
const DURATION = 520;

export function useFlip(orderKey: string) {
  const nodes = useRef(new Map<string, HTMLElement>());
  const callbacks = useRef(new Map<string, (el: HTMLElement | null) => void>());
  const before = useRef<Map<string, DOMRect> | null>(null);

  /** Stable ref callback per key (so React doesn't re-attach every render). */
  const register = useCallback((key: string) => {
    let cb = callbacks.current.get(key);
    if (!cb) {
      cb = (el) => {
        if (el) nodes.current.set(key, el);
        else nodes.current.delete(key);
      };
      callbacks.current.set(key, cb);
    }
    return cb;
  }, []);

  const snapshot = useCallback(() => {
    if (prefersReducedMotion()) return;
    const m = new Map<string, DOMRect>();
    nodes.current.forEach((el, k) => m.set(k, el.getBoundingClientRect()));
    before.current = m;
  }, []);

  useLayoutEffect(() => {
    const prev = before.current;
    before.current = null;
    if (!prev || typeof HTMLElement === "undefined" || !("animate" in HTMLElement.prototype)) return;
    nodes.current.forEach((el, k) => {
      const o = prev.get(k);
      if (!o) return;
      const n = el.getBoundingClientRect();
      const dx = o.left - n.left;
      const dy = o.top - n.top;
      if (Math.abs(dx) < 0.5 && Math.abs(dy) < 0.5) return;
      el.animate([{ transform: `translate(${dx}px, ${dy}px)` }, { transform: "none" }], {
        duration: DURATION,
        easing: EASE,
      });
    });
  }, [orderKey]);

  return { register, snapshot };
}
