import { useEffect, useRef, useState } from "react";
import { prefersReducedMotion } from "../../lib/useSize";

/**
 * "static": drawn in its final state (reduced motion, a return visit, or no
 *   IntersectionObserver). "armed": waiting off-screen, marks hidden.
 * "play": entered the viewport; the CSS entrance runs once.
 */
export type RevealPhase = "static" | "armed" | "play";

/** Plays a one-shot entrance the first time the element scrolls into view. */
export function useReveal<T extends Element>(enabled: boolean) {
  const ref = useRef<T>(null);
  const [phase, setPhase] = useState<RevealPhase>(() =>
    enabled && typeof IntersectionObserver !== "undefined" && !prefersReducedMotion() ? "armed" : "static",
  );

  useEffect(() => {
    if (phase !== "armed") return;
    const el = ref.current;
    if (!el) return;
    const io = new IntersectionObserver(
      (entries) => {
        if (entries.some((e) => e.isIntersecting)) {
          setPhase("play");
          io.disconnect();
        }
      },
      { threshold: 0.2, rootMargin: "0px 0px -6% 0px" },
    );
    io.observe(el);
    return () => io.disconnect();
  }, [phase]);

  return [ref, phase] as const;
}
