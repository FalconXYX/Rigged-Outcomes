import { useEffect, useRef, useState, type ReactNode } from "react";
import { calDate, fmtDayAt, fmtInt, torontoDay } from "../../lib/format";
import { openSheet, setQuery } from "../../lib/router";

// What the Watchlist page's two sections share: times in words, the icons,
// the cards' columns, the cards' "Details" button, the details panel that
// opens under a card's row, and the panel's plain list of headlines (folded
// when long). Every day and time is read on Toronto's clock (lib/format.ts).

export const ms = (iso: string) => Date.parse(iso);
export const DAY_MS = 864e5;
/** Calendar days from Toronto's day at `b` to Toronto's day at `a` (ISO stamps in any offset). */
export const daysBetween = (a: string, b: string) =>
  Math.round((calDate(torontoDay(a)).getTime() - calDate(torontoDay(b)).getTime()) / DAY_MS);
export const plural = (n: number, one: string, many = `${one}s`) => `${fmtInt(n)} ${n === 1 ? one : many}`;

/**
 * A report the source dated but did not time: the news pipeline stamps those
 * midnight Pacific exactly (07:00 or 08:00 on the zero offset, 03:00 on
 * Toronto's). Read those as date-only; Toronto's day of the stamp is the
 * report's date. (Until upsets.py saves a "time known" flag, this is the only tell.)
 */
const DATE_ONLY = /(?:T0[78]:00:00(?:\.0+)?(?:Z|\+00:00)|T03:00:00(?:\.0+)?-0[45]:00)$/;
export const dateOnly = (iso: string) => DATE_ONLY.test(iso);

export const Chevron = ({ up }: { up?: boolean }) => (
  <svg width="10" height="10" viewBox="0 0 12 12" aria-hidden className="cl-chev" data-up={up || undefined}>
    <path d="M3 4.5 6 7.5 9 4.5" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
  </svg>
);

export const Cross = () => (
  <svg width="12" height="12" viewBox="0 0 12 12" aria-hidden>
    <path d="M3 3l6 6M9 3 3 9" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
  </svg>
);

/** Columns for a row of cards: its count, at least 3 (one card never stretches across the page), at most 5. */
export const colsFor = (n: number) => Math.max(3, Math.min(n, 5));

/** The id of a card's details panel, from its `?open=` key. */
export const panelId = (key: string) => `wl-panel-${key}`;

/**
 * Open a card's details (`?open=key`) as a history step, so the browser's
 * Back closes them. Going from one open card straight to another swaps that
 * step in place, so Back still closes the details instead of reopening the
 * card before.
 */
export function openPanel(key: string) {
  const at = window.history.state?.roSheet;
  const shown = new URLSearchParams(window.location.hash.split("?")[1] ?? "").get("open");
  if (typeof at === "string" && at !== key && shown === at) {
    window.history.replaceState({ ...window.history.state, roSheet: key }, "");
    setQuery({ open: key });
  } else openSheet(key);
}

/**
 * A card's "Details", which reads "Close" while its panel is open. The card's
 * own click opens it too; this is the one a keyboard reaches.
 */
export function MoreButton({ id, open, panel, onToggle }: { id: string; open: boolean; panel: string; onToggle: () => void }) {
  return (
    <button
      type="button"
      id={id}
      className="cl-card-more"
      aria-expanded={open}
      aria-controls={open ? panel : undefined}
      onClick={(e) => {
        e.stopPropagation();
        onToggle();
      }}
    >
      {open ? (
        <>
          Close <Chevron up />
        </>
      ) : (
        <>
          Details <Chevron />
        </>
      )}
    </button>
  );
}

/** px kept between the site header (or the screen's bottom) and what opening a panel scrolls to */
const EDGE_GAP = 12;

/**
 * How far to scroll the page when a panel opens: as little as shows the
 * whole panel, but never so far that the card's title goes under the site
 * header, so the card and its panel read together (on a laptop both fit,
 * or at least the card's title and the panel's actions under its story do).
 * When that would leave the actions off screen (a phone, the panel in one
 * column and taller than the screen), at least 60% of the screen goes to
 * the panel instead, its top always on screen.
 */
function scrollFor(panel: HTMLElement, card: Element | null): number {
  const header = document.querySelector(".site-header")?.getBoundingClientRect().bottom ?? 0;
  const box = panel.getBoundingClientRect();
  const foot = panel.querySelector(".wl-panel-foot")?.getBoundingClientRect().bottom ?? box.bottom;
  const title = card?.querySelector(".cl-card-title") ?? card;
  // the most it may scroll: the panel's own top just under the header, and while the card's title is on screen, that title
  const toTop = box.top - header - EDGE_GAP;
  if (toTop < 0) return toTop;
  const toTitle = title ? title.getBoundingClientRect().top - header - EDGE_GAP : -1;
  const cap = toTitle >= 0 ? Math.min(toTitle, toTop) : toTop;
  const by = Math.max(Math.min(box.bottom + EDGE_GAP - window.innerHeight, cap), 0);
  if (foot - by <= window.innerHeight - EDGE_GAP) return by;
  const room = window.innerHeight - header - 2 * EDGE_GAP;
  const least = box.top + Math.min(box.height, 0.6 * room) + EDGE_GAP - window.innerHeight;
  return Math.min(Math.max(by, least), toTop);
}

/**
 * A card's details, in either section: a box of its own in the page, under
 * the row that holds the card (under the card on a phone), a clear gap
 * above it and nothing joining it to the card. It names its market in its
 * first line, and the card's "Details" reads "Close" meanwhile. The story on
 * the left, the evidence on the right (one column on a phone), the actions
 * under the story.
 *
 * Opening it moves focus to that first line and scrolls the page as little
 * as shows it (scrollFor). ×, Escape, the card's "Close" or the browser's
 * Back closes it (it opens as a history step: openPanel), and focus goes
 * back to the card's button. `?open=` names the one open card, so only one
 * panel is ever open.
 */
export function Panel({
  id,
  question,
  returnTo,
  onClose,
  verdict,
  main,
  side,
  foot,
}: {
  id: string;
  /** the card's question: the panel's first line, and its name for screen readers */
  question: string;
  /** the id of the card's Details / Close button */
  returnTo: string;
  onClose: () => void;
  /** a closed card's verdict: its colour marks the panel's label, checks and first report */
  verdict?: string;
  main: ReactNode;
  side: ReactNode;
  foot: ReactNode;
}) {
  const ref = useRef<HTMLElement>(null);
  const head = useRef<HTMLHeadingElement>(null);
  const closeRef = useRef(onClose);
  closeRef.current = onClose;

  // Escape closes it wherever focus is (a click on a card's face or on the
  // panel's text leaves focus on the page); a key something inside already used is left alone
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape" || e.defaultPrevented) return;
      e.preventDefault();
      closeRef.current();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, []);

  useEffect(() => {
    const panel = ref.current;
    if (!panel) return;
    head.current?.focus({ preventScroll: true });
    const bring = (smooth: boolean) => {
      const by = scrollFor(panel, document.getElementById(returnTo)?.closest(".cl-card") ?? null);
      if (by !== 0) window.scrollBy({ top: by, behavior: smooth ? "smooth" : "auto" });
    };
    bring(!window.matchMedia("(prefers-reduced-motion: reduce)").matches);
    // Opened from a link, the page above it may still be loading (the open
    // list) and push it down: for a moment, while the panel moves, bring it
    // back into view, until the reader scrolls or types
    const docTop = () => panel.getBoundingClientRect().top + window.scrollY;
    let top = docTop();
    const watch = new ResizeObserver(() => {
      const now = docTop();
      if (Math.abs(now - top) < 1) return;
      top = now;
      bring(false);
    });
    watch.observe(panel.closest(".wl-page") ?? document.body);
    const stop = () => watch.disconnect();
    const timer = window.setTimeout(stop, 2000);
    const READER = ["wheel", "touchstart", "keydown", "pointerdown"] as const;
    for (const e of READER) window.addEventListener(e, stop, { passive: true, once: true });
    return () => {
      stop();
      window.clearTimeout(timer);
      for (const e of READER) window.removeEventListener(e, stop);
      // once it is gone: focus back to the card's button, unless something
      // else has it (another card's panel, opened in its place)
      requestAnimationFrame(() => {
        const at = document.activeElement;
        if (at && at !== document.body) return;
        const btn = document.getElementById(returnTo);
        if (!btn) return;
        btn.focus({ preventScroll: true });
        const r = btn.getBoundingClientRect();
        const h = document.querySelector(".site-header")?.getBoundingClientRect().bottom ?? 0;
        if (r.top < h || r.bottom > window.innerHeight) btn.scrollIntoView({ block: "center" });
      });
    };
  }, [returnTo]);

  return (
    <section ref={ref} id={id} className="wl-panel" aria-labelledby={`${id}-name`} data-verdict={verdict}>
      <div className="wl-panel-top">
        <h3 ref={head} id={`${id}-name`} className="wl-panel-name" tabIndex={-1}>
          <span className="wl-panel-k">Details</span>
          <span className="visually-hidden">: </span> <span className="wl-panel-q">{question}</span>
        </h3>
        <button type="button" className="wl-panel-x" onClick={() => closeRef.current()} aria-label="Close details" title="Close details">
          <Cross />
        </button>
      </div>
      <div className="wl-panel-main">{main}</div>
      <div className="wl-panel-side">{side}</div>
      <div className="wl-panel-foot">{foot}</div>
    </section>
  );
}

type Story = { title: string; source: string; published: string | null; url: string };

/** a source name longer than this may wrap (on a phone it would push the page wider) */
const NOWRAP = 24;

/** headlines shown while a list is folded; a list at most one longer shows whole */
const PREVIEW = 3;

/**
 * Headlines as a plain list, newest last, each on two lines: the headline,
 * then "Source · Nov 11" (a date-only report by its date alike, never a
 * clock time) and its mark ("First report", "On the card", "Step") in the
 * panel's colour. A long list starts folded to 3: the `pin`ned row (the
 * first report, the card's headline) always, then other marked rows, then
 * the newest; "Show all 12 headlines" opens the rest in place.
 */
export function NewsList<T extends Story>({
  id,
  items,
  mark,
  pin,
}: {
  id: string;
  items: T[];
  mark?: (h: T) => string | null;
  pin?: (h: T) => boolean;
}) {
  const [all, setAll] = useState(false);
  const toggle = useRef<HTMLButtonElement>(null);
  const folded = useRef(false);
  const list = [...items].sort((a, b) => (a.published ?? "").localeCompare(b.published ?? ""));
  const fold = list.length > PREVIEW + 1;
  const shown = new Set<number>();
  if (fold && !all) {
    const newest = [...list.keys()].reverse();
    for (const keep of [(h: T) => !!pin?.(h), (h: T) => !!mark?.(h), () => true])
      for (const i of newest) if (shown.size < PREVIEW && keep(list[i])) shown.add(i);
  }
  // "Show fewer" can leave its button above the screen: bring it back
  useEffect(() => {
    if (!folded.current) return;
    folded.current = false;
    const btn = toggle.current;
    const header = document.querySelector(".site-header")?.getBoundingClientRect().bottom ?? 0;
    if (btn && btn.getBoundingClientRect().top < header) btn.scrollIntoView({ block: "center" });
  }, [all]);
  return (
    <>
      <ol className="wl-news" id={id}>
        {list.map((h, i) => {
          if (shown.size && !shown.has(i)) return null;
          const m = mark?.(h);
          return (
            <li key={`${i}-${h.url || h.title}`} data-mark={m || undefined}>
              <a href={h.url} target="_blank" rel="noopener noreferrer">
                {h.title}
              </a>
              <span className="wl-news-src">
                {/* "Reuters · Nov 12" stays on one line; a long name ("ABC News - Breaking News, …") may wrap, its date kept with its last word */}
                <span className={h.source.length > NOWRAP ? undefined : "wl-nowrap"}>
                  {h.source}
                  {h.published && `\u00a0·\u00a0${fmtDayAt(h.published).replace(/ /g, "\u00a0")}`}
                </span>
                {m && (
                  <>
                    {" "}
                    · <b className="wl-news-mark wl-nowrap">{m}</b>
                  </>
                )}
              </span>
            </li>
          );
        })}
      </ol>
      {fold && (
        <button
          type="button"
          ref={toggle}
          className="cl-link wl-news-all"
          aria-expanded={all}
          aria-controls={id}
          onClick={() => {
            folded.current = all;
            setAll(!all);
          }}
        >
          {all ? "Show fewer" : `Show all ${fmtInt(list.length)} headlines`} <Chevron up={all} />
        </button>
      )}
    </>
  );
}
