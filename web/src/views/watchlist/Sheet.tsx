import { useEffect, useRef, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { Cross } from "./shared";

// A card's details, in either section: a side sheet that slides in from the
// right over the page, full height under the site header (the whole screen
// on a phone), over a light dim. ×, Escape or a click on the dim closes it.
// Tab stays inside while it is open, and focus goes back to the card's
// Details button when it closes. `?open=` names the one open card, so only
// one sheet is ever open.

const FOCUSABLE = 'a[href], button:not([disabled]), summary, input, select, textarea, [tabindex]:not([tabindex="-1"])';

export function Sheet({
  label,
  returnTo,
  onClose,
  verdict,
  children,
}: {
  /** the dialog's name for screen readers: the card's question */
  label: string;
  /** the id of the card's Details button */
  returnTo: string;
  onClose: () => void;
  /** a closed card's verdict: its colour marks the sheet's checks and first report */
  verdict?: string;
  children: ReactNode;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const closeRef = useRef(onClose);
  closeRef.current = onClose;

  useEffect(() => {
    const sheet = ref.current;
    if (!sheet) return;
    sheet.focus({ preventScroll: true });

    // the page under it stays put (its scrollbar's width kept, so nothing shifts)
    const root = document.documentElement;
    const bar = window.innerWidth - root.clientWidth;
    root.style.overflow = "hidden";
    if (bar > 0) root.style.paddingRight = `${bar}px`;

    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !e.defaultPrevented) {
        e.preventDefault();
        closeRef.current();
        return;
      }
      if (e.key !== "Tab") return;
      const items = [...sheet.querySelectorAll<HTMLElement>(FOCUSABLE)].filter((el) => el.getClientRects().length > 0);
      if (!items.length) {
        e.preventDefault();
        return;
      }
      const first = items[0];
      const last = items[items.length - 1];
      const at = document.activeElement;
      const inside = at instanceof Node && sheet.contains(at) && at !== sheet;
      if (e.shiftKey && (!inside || at === first)) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && (!inside || at === last)) {
        e.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      root.style.overflow = "";
      root.style.paddingRight = "";
      document.getElementById(returnTo)?.focus();
    };
  }, [returnTo]);

  return createPortal(
    <>
      <div className="wl-sheet-dim" onClick={() => closeRef.current()} aria-hidden="true" />
      <div ref={ref} className="wl-sheet" role="dialog" aria-modal="true" aria-label={label} tabIndex={-1} data-verdict={verdict}>
        <button type="button" className="wl-sheet-x" onClick={() => closeRef.current()} aria-label="Close details" title="Close details">
          <Cross />
        </button>
        <div className="wl-sheet-body">{children}</div>
      </div>
    </>,
    document.body,
  );
}
