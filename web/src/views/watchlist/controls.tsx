import { useEffect, useRef, useState, type ReactNode, type RefObject } from "react";
import type { ClosedPick, OpenCard } from "../../types";
import { href } from "../../lib/router";
import { WORDS, apiConfigured, apiGet, apiSend, setWatchedHere, watchedHere, type PublicRequest, type RequestStatus } from "../../lib/api";
import { plural } from "./shared";

// The two controls in the Watchlist sheets' footers, each a quiet text button
// that says what it does and what happens next: "Add to main page" (closed
// sheet: the site's owner reviews each request) and "Watch this market" (open
// sheet: when it closes, Recently closed says how many watched it). Each asks the
// API for its state when the sheet opens. With no API in the build, the API
// down, or the market not one it lists, both read "Not available yet"; while
// the owner has paused visitors' changes, "Not available right now". Nothing
// is added to the cards.

export const Plus = () => (
  <svg width="10" height="10" viewBox="0 0 12 12" aria-hidden>
    <path d="M6 2v8M2 6h8" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
  </svg>
);

/** Where the bot check shows if Cloudflare wants a click: a footer row of its own, empty (and hidden) otherwise. */
const TsSlot = ({ slot }: { slot: RefObject<HTMLSpanElement | null> }) => <span className="ts-slot" ref={slot} />;

/** A control that can't be used: its button greyed out, and why beside it (the note is its description). */
function Unavailable({ id, children, note, row }: { id: string; children: ReactNode; note?: string; row?: boolean }) {
  return (
    <span className={row ? "cl-add op-watch" : "cl-add"}>
      {/* aria-disabled, not disabled: it stays reachable so its note is read out */}
      <button
        type="button"
        className="cl-link cl-add-btn"
        aria-disabled="true"
        aria-describedby={note ? id : undefined}
        onClick={(e) => e.preventDefault()}
      >
        {children}
      </button>
      {note && (
        <>
          <span aria-hidden="true">·</span>
          <span className="cl-note" id={id}>
            {note}
          </span>
        </>
      )}
    </span>
  );
}

/** Moves focus to `ref` once `when` turns true, if the control's button had it (the button is gone). */
function useFocusAfter(ref: RefObject<HTMLElement | null>, when: boolean, had: RefObject<boolean>) {
  useEffect(() => {
    if (!when || !had.current) return;
    had.current = false;
    ref.current?.focus({ preventScroll: true });
  }, [when, ref, had]);
}

/* ── Add to main page (closed sheet) ─────────────────────────────────── */

type RequestState = { listed: boolean; writes: boolean; request: PublicRequest | null };

/** A request's state in words: what it is, then what happens next. */
const STATUS: Record<RequestStatus, [state: string, next: string]> = {
  requested: ["Requested", "waiting for the owner's review"],
  approved: ["Approved", "it joins the main page at the next site update"],
  added: ["Added", "it appears on the main page after the site updates"],
  declined: ["Not added", "the owner decided against it"],
  failed: ["Approved, but adding it failed", "the owner will look into it"],
};

/**
 * "+ Add to main page": a visitor asks for a closed market to get its own
 * page; the owner approves it on the review page and the next site update
 * adds it. Once the market is on the main page, the link to it.
 */
export function AddToMain({ p, onMain }: { p: ClosedPick; onMain: string | null }) {
  const id = `cl-note-${p.condition_id.slice(0, 12)}`;
  // undefined while asking; null when it can't be used (no API, down, the market not listed)
  const [got, setGot] = useState<RequestState | null | undefined>(apiConfigured ? undefined : null);
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const slot = useRef<HTMLSpanElement>(null);
  const status = useRef<HTMLSpanElement>(null);
  const hadFocus = useRef(false);
  useFocusAfter(status, !!got?.request, hadFocus);

  useEffect(() => {
    if (onMain || !apiConfigured) return;
    let live = true;
    apiGet<RequestState>(`/api/request?cid=${encodeURIComponent(p.condition_id)}`).then(
      (r) => live && setGot(r && r.listed === true ? r : null),
    );
    return () => {
      live = false;
    };
  }, [p.condition_id, onMain]);

  if (onMain)
    return (
      <a className="btn btn-solid btn-sm" href={href.market(onMain)}>
        On the main page →
      </a>
    );

  const label = (
    <>
      <Plus /> Add to main page
    </>
  );
  if (got === null)
    return (
      <Unavailable id={id} note="Not available yet">
        {label}
      </Unavailable>
    );
  if (got === undefined) return <Unavailable id={id}>{label}</Unavailable>;
  if (got.request) {
    const [state, next] = STATUS[got.request.status];
    return (
      <span className="cl-add" ref={status} tabIndex={-1}>
        <span>{state}</span>
        <span aria-hidden="true">·</span>
        <span className="cl-note cl-note-wrap">{next}</span>
      </span>
    );
  }
  if (!got.writes)
    return (
      <Unavailable id={id} note="Not available right now">
        {label}
      </Unavailable>
    );
  // the bot-check slot stays mounted while sending: Cloudflare may ask for its click there
  if (sending)
    return (
      <>
        <span className="cl-add">
          <button type="button" className="cl-link cl-add-btn" aria-disabled="true" onClick={(e) => e.preventDefault()}>
            {label}
          </button>
          <span aria-hidden="true">·</span>
          <span className="cl-note" role="status">
            Sending…
          </span>
        </span>
        <TsSlot slot={slot} />
      </>
    );

  const send = async () => {
    hadFocus.current = document.activeElement instanceof HTMLButtonElement;
    setSending(true);
    setError(null);
    const r = await apiSend<{ request: PublicRequest }>(
      "POST",
      "/api/request",
      { condition_id: p.condition_id },
      { bot: "request", slot: slot.current },
    );
    setSending(false);
    if (r.ok && r.data?.request) setGot({ ...got, request: r.data.request });
    else if (r.ok) setError(WORDS.other);
    else if (r.code === "paused") setGot({ ...got, writes: false });
    else setError(r.words);
  };

  return (
    <>
      <span className="cl-add">
        <button type="button" className="cl-link cl-add-btn" aria-describedby={id} onClick={send}>
          {label}
        </button>
        <span aria-hidden="true">·</span>
        <span className="cl-note cl-note-wrap" id={id} role={error ? "alert" : undefined}>
          {error ? `Couldn't send: ${error}` : "The site's owner reviews each request"}
        </span>
      </span>
      <TsSlot slot={slot} />
    </>
  );
}

/* ── Watch this market (open sheet) ──────────────────────────────────── */

type WatchState = { listed: boolean; writes: boolean; count: number; watching: boolean };

/**
 * "Watch this market": counts the visitors watching an open market. A
 * market from an open card is always checked by the closed list when it
 * closes; a watched one is marked there with how many watched it.
 */
export function WatchControl({ card }: { card: OpenCard }) {
  const id = `op-note-${card.id.length}-${card.id.slice(0, 24).replace(/[^a-z0-9]+/gi, "-")}`;
  const [got, setGot] = useState<WatchState | null | undefined>(apiConfigured ? undefined : null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const slot = useRef<HTMLSpanElement>(null);
  const focusTo = useRef<HTMLButtonElement>(null);
  const hadFocus = useRef(false);
  useFocusAfter(focusTo, !saving && got != null, hadFocus);

  const path = `/api/watch?story=${encodeURIComponent(card.id)}`;
  useEffect(() => {
    if (!apiConfigured) return;
    let live = true;
    apiGet<WatchState>(path).then((r) => {
      if (!live) return;
      const ok = r && r.listed === true && typeof r.count === "number";
      setGot(ok ? { ...r, watching: r.watching ?? watchedHere(card.id) } : null);
    });
    return () => {
      live = false;
    };
  }, [path, card.id]);

  const label = "Watch this market";
  if (got === null)
    return (
      <Unavailable id={id} note="Not available yet" row>
        {label}
      </Unavailable>
    );
  if (got === undefined)
    return (
      <Unavailable id={id} row>
        {label}
      </Unavailable>
    );
  // stopping works while visitors' changes are paused; starting doesn't
  if (!got.writes && !got.watching)
    return (
      <Unavailable id={id} note="Not available right now" row>
        {label}
      </Unavailable>
    );

  const save = async (on: boolean) => {
    hadFocus.current = document.activeElement instanceof HTMLButtonElement;
    setSaving(true);
    setError(null);
    const r = on
      ? await apiSend<{ count: number; watching: boolean }>("POST", "/api/watch", { story: card.id }, { bot: "watch", slot: slot.current })
      : await apiSend<{ count: number; watching: boolean }>("DELETE", path);
    setSaving(false);
    if (r.ok && typeof r.data?.count === "number") {
      setWatchedHere(card.id, r.data.watching);
      setGot({ ...got, count: r.data.count, watching: r.data.watching });
    } else if (!r.ok && r.code === "paused") setGot({ ...got, writes: false });
    else setError(r.ok ? WORDS.other : r.words);
  };

  const failed = error && (
    <>
      <span aria-hidden="true">·</span>
      <span className="cl-note cl-note-wrap" role="alert">
        Couldn't save: {error}
      </span>
    </>
  );

  if (got.watching)
    return (
      <>
        <span className="cl-add op-watch">
          <span>Watching</span>
          <span aria-hidden="true">·</span>
          <span>watched by {plural(got.count, "visitor")}</span>
          <span aria-hidden="true">·</span>
          <button
            type="button"
            ref={focusTo}
            className="cl-link cl-add-btn"
            aria-disabled={saving || undefined}
            onClick={() => !saving && save(false)}
          >
            {saving ? "Saving…" : "Stop watching"}
          </button>
          {failed}
        </span>
        <TsSlot slot={slot} />
      </>
    );

  return (
    <>
      <span className="cl-add op-watch">
        <button
          type="button"
          ref={focusTo}
          className="cl-link cl-add-btn"
          aria-describedby={id}
          aria-disabled={saving || undefined}
          onClick={() => !saving && save(true)}
        >
          {saving ? "Saving…" : label}
        </button>
        <span aria-hidden="true">·</span>
        {error ? (
          <span className="cl-note cl-note-wrap" id={id} role="alert">
            Couldn't save: {error}
          </span>
        ) : (
          <span className="cl-note cl-note-wrap" id={id}>
            {got.count > 0 && <>Watched by {plural(got.count, "visitor")} · </>}when it closes, Recently closed says how many watched it
          </span>
        )}
      </span>
      <TsSlot slot={slot} />
    </>
  );
}
