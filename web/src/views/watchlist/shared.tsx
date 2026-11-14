import { fmtDay, fmtInt } from "../../lib/format";

// What the Watchlist page's two sections share: times in words, the icons,
// the cards' columns, the cards' "Details" button and the sheets' plain
// list of headlines. All times are UTC.

export const ms = (iso: string) => Date.parse(iso);
export const DAY_MS = 864e5;
export const dayOf = (t: number) => Math.floor(t / DAY_MS) * DAY_MS;
export const plural = (n: number, one: string, many = `${one}s`) => `${fmtInt(n)} ${n === 1 ? one : many}`;

/**
 * A report the source dated but did not time: the news pipeline stamps those
 * midnight Pacific, 07:00 or 08:00 UTC exactly. Read those as date-only.
 * (Until upsets.py saves a "time known" flag, this is the only tell.)
 */
const DATE_ONLY = /T0[78]:00:00(?:\.0+)?(?:Z|\+00:00)$/;
export const dateOnly = (iso: string) => DATE_ONLY.test(iso);

/** "23:12", the clock time in UTC */
export const hhmm = (iso: string) => new Date(iso).toISOString().slice(11, 16);

export const Chevron = () => (
  <svg width="10" height="10" viewBox="0 0 12 12" aria-hidden className="cl-chev">
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

/** A card's "Details": opens its sheet. The card's own click does the same; this is the one a keyboard reaches. */
export function MoreButton({ id, open, onOpen }: { id: string; open: boolean; onOpen: () => void }) {
  return (
    <button
      type="button"
      id={id}
      className="cl-card-more"
      aria-haspopup="dialog"
      aria-expanded={open}
      onClick={(e) => {
        e.stopPropagation();
        onOpen();
      }}
    >
      Details <Chevron />
    </button>
  );
}

type Story = { title: string; source: string; published: string | null; url: string };

/**
 * Headlines as a plain list, newest last: "Headline — Source · Nov 11", a
 * date-only report by its date alike, never a clock time. `mark` names the
 * ones to mark ("First report", "On the card"), in the sheet's colour.
 */
export function NewsList<T extends Story>({ items, mark }: { items: T[]; mark?: (h: T) => string | null }) {
  const list = [...items].sort((a, b) => (a.published ?? "").localeCompare(b.published ?? ""));
  return (
    <ol className="wl-news">
      {list.map((h, i) => {
        const m = mark?.(h);
        return (
          <li key={`${i}-${h.url || h.title}`} data-mark={m || undefined}>
            <a href={h.url} target="_blank" rel="noopener noreferrer">
              {h.title}
            </a>{" "}
            <span className="wl-news-src">
              — <span className="wl-nowrap">
                {h.source}
                {h.published && ` · ${fmtDay(new Date(h.published))}`}
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
  );
}
