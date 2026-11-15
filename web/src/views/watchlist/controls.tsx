import { useCallback, useEffect, useRef, useSyncExternalStore, type RefObject } from "react";
import type { ClosedPick, OpenCard } from "../../types";
import { href } from "../../lib/router";
import {
  OFFLINE,
  apiConfigured,
  apiGet,
  apiSend,
  requestedHere,
  setRequestedHere,
  setWatchedHere,
  watchedHere,
  type PublicRequest,
} from "../../lib/api";

// The Watchlist's two actions: "Add to main page" (closed card: the next run
// of the job adds it) and "Watch this market" (open card: the site has one
// watchlist, the same for everyone). Each shows in two places: on the card's
// face, beside "Details" (CardAdd, CardWatch), and in the card's details panel
// (AddToMain, WatchControl). Both places share one state per market, so they
// always agree. A market's state is asked of the API once when its card shows
// (again after a minute). With no API in the build, or the API not answering,
// the controls work the same and keep what this browser did; only a refusal a
// visitor should read (a daily limit, a failed bot check) is shown.

export const Plus = () => (
  <svg width="10" height="10" viewBox="0 0 12 12" aria-hidden>
    <path d="M6 2v8M2 6h8" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
  </svg>
);

/** Where the bot check shows if Cloudflare wants a click: a footer row of its own, empty (and hidden) otherwise. */
const TsSlot = ({ slot }: { slot: RefObject<HTMLSpanElement | null> }) => <span className="ts-slot" ref={slot} />;

/** Moves focus to `ref` once `when` turns true, if the control's button had it (the button is gone). */
function useFocusAfter(ref: RefObject<HTMLElement | null>, when: boolean, had: RefObject<boolean>) {
  useEffect(() => {
    if (!when || !had.current) return;
    had.current = false;
    ref.current?.focus({ preventScroll: true });
  }, [when, ref, had]);
}

/* ── One state per market, shared by the card and its panel ──────────── */

type Where = "card" | "panel";

/**
 * A market's control state: what the API said (undefined while asking),
 * a write on its way, and the last write's error with the place it was sent
 * from (only that place shows it).
 */
type Shared<S> = { got: S | undefined; busy: boolean; error: string | null; by: Where | null; at: number };

/** a state asked for less than this long ago is not asked for again */
const FRESH_MS = 60_000;

/** `local(k)`: the state kept in this browser, used when the API can't answer for `k`. */
function makeStore<S>(local: (k: string) => S) {
  const all = new Map<string, Shared<S>>();
  const subs = new Map<string, Set<() => void>>();
  const asking = new Set<string>();
  const get = (k: string) => {
    let s = all.get(k);
    if (!s) {
      s = { got: apiConfigured ? undefined : local(k), busy: false, error: null, by: null, at: 0 };
      all.set(k, s);
    }
    return s;
  };
  const set = (k: string, patch: Partial<Shared<S>>) => {
    all.set(k, { ...get(k), ...patch });
    subs.get(k)?.forEach((f) => f());
  };
  const subscribe = (k: string, f: () => void) => {
    const s = subs.get(k) ?? new Set();
    subs.set(k, s);
    s.add(f);
    return () => {
      s.delete(f);
    };
  };
  /** Ask the API for `k`'s state, unless it is being asked or was answered within FRESH_MS. */
  const ask = (k: string, path: string, accept: (r: unknown) => S | null) => {
    const s = get(k);
    if (!apiConfigured || asking.has(k) || (s.got !== undefined && Date.now() - s.at < FRESH_MS)) return;
    asking.add(k);
    apiGet<unknown>(path).then((r) => {
      asking.delete(k);
      // a write on its way has the newer answer
      if (!get(k).busy) set(k, { got: accept(r) ?? local(k), at: Date.now() });
    });
  };
  return { get, set, subscribe, ask };
}

type Store<S> = ReturnType<typeof makeStore<S>>;

function useShared<S>(store: Store<S>, k: string): Shared<S> {
  const subscribe = useCallback((f: () => void) => store.subscribe(k, f), [store, k]);
  return useSyncExternalStore(subscribe, () => store.get(k));
}

/** A click on the card's action never opens the card's details. */
const stay = (e: { stopPropagation(): void }) => e.stopPropagation();

/* ── Add to main page (closed card) ─────────────────────────────────── */

/** `local`: kept in this browser (no API, or the API couldn't answer). */
type RequestState = { request: PublicRequest | null; local?: boolean };

const ADDED_HERE: PublicRequest = { status: "approved", name: null, decided_at: null, done_at: null };

const requests = makeStore<RequestState>((k) => ({ request: requestedHere(k) ? ADDED_HERE : null, local: true }));

/** What a market sent to the main page reads until it is there (then "On the main page →"), whatever its state. */
const ADDING = "Adding to the main page";

/** Asks the API whether a closed market was sent to the main page while its card or panel shows (not once it is there). */
function useRequest(p: ClosedPick, onMain: string | null): Shared<RequestState> {
  const k = p.condition_id;
  const s = useShared(requests, k);
  useEffect(() => {
    if (onMain) return;
    requests.ask(k, `/api/request?cid=${encodeURIComponent(k)}`, (r) => {
      const v = r as { listed?: boolean; request?: PublicRequest | null } | null;
      return v && v.listed === true ? { request: v.request ?? null } : null;
    });
  }, [k, onMain]);
  return s;
}

/** Add it to the main page (a bot check first, shown in `slot` if Cloudflare asks for a click, else in a corner box). */
async function sendRequest(p: ClosedPick, by: Where, slot: HTMLElement | null) {
  const k = p.condition_id;
  const got = requests.get(k).got;
  if (!got || requests.get(k).busy) return;
  const keepHere = () => {
    setRequestedHere(k);
    requests.set(k, { busy: false, error: null, got: { request: ADDED_HERE, local: true } });
  };
  if (got.local) return keepHere();
  requests.set(k, { busy: true, error: null, by });
  const r = await apiSend<{ request: PublicRequest }>(
    "POST",
    "/api/request",
    { condition_id: p.condition_id },
    { bot: "request", slot },
  );
  if (r.ok && r.data?.request) requests.set(k, { busy: false, got: { request: r.data.request } });
  else if (r.ok || OFFLINE.has(r.code)) keepHere();
  else requests.set(k, { busy: false, error: r.words });
}

const addLabel = (
  <>
    <Plus /> Add to main page
  </>
);

/**
 * "+ Add to main page" in the details panel: the closed market gets its own
 * page, which the next run of the job adds; "Adding to the main page" until
 * then, and once it is there the link to it.
 */
export function AddToMain({ p, onMain }: { p: ClosedPick; onMain: string | null }) {
  const id = `cl-note-${p.condition_id.slice(0, 12)}`;
  const { got, busy, error, by } = useRequest(p, onMain);
  const slot = useRef<HTMLSpanElement>(null);
  const status = useRef<HTMLSpanElement>(null);
  const hadFocus = useRef(false);
  useFocusAfter(status, !!got?.request, hadFocus);

  if (onMain)
    return (
      <a className="cl-link cl-add-btn" href={href.market(onMain)}>
        On the main page →
      </a>
    );

  if (got?.request)
    return (
      <span className="cl-add" ref={status} tabIndex={-1}>
        <span className="cl-add-state">{ADDING}</span>
      </span>
    );

  const failed = error && by === "panel" ? error : null;
  // the bot-check slot stays mounted while sending: Cloudflare may ask for its click there
  return (
    <>
      <span className="cl-add">
        <button
          type="button"
          className="cl-link cl-add-btn"
          aria-describedby={failed ? id : undefined}
          aria-busy={busy || undefined}
          onClick={() => {
            if (busy) return;
            hadFocus.current = document.activeElement instanceof HTMLButtonElement;
            sendRequest(p, "panel", slot.current);
          }}
        >
          {addLabel}
        </button>
        {failed && (
          <span className="cl-note cl-note-wrap" id={id} role="alert">
            {failed}
          </span>
        )}
      </span>
      <TsSlot slot={slot} />
    </>
  );
}

/** "+ Add to main page" on a closed card's face; "Adding to the main page" once clicked; "On the main page →" once there. */
export function CardAdd({ p, onMain }: { p: ClosedPick; onMain: string | null }) {
  const id = `cl-act-${p.condition_id.slice(0, 12)}`;
  const { got, busy, error, by } = useRequest(p, onMain);
  const status = useRef<HTMLSpanElement>(null);
  const hadFocus = useRef(false);
  useFocusAfter(status, !!got?.request, hadFocus);

  if (onMain)
    return (
      <span className="wl-act" onClick={stay}>
        <a className="cl-link wl-act-btn" href={href.market(onMain)}>
          On the main page →
        </a>
      </span>
    );
  if (got?.request)
    return (
      <span className="wl-act" ref={status} tabIndex={-1} onClick={stay}>
        <span className="wl-act-state">{ADDING}</span>
      </span>
    );
  const failed = error && by === "card" ? error : null;
  return (
    <span className="wl-act" onClick={stay}>
      <button
        type="button"
        className="cl-link wl-act-btn"
        aria-describedby={failed ? id : undefined}
        aria-busy={busy || undefined}
        onClick={() => {
          if (busy) return;
          hadFocus.current = document.activeElement instanceof HTMLButtonElement;
          sendRequest(p, "card", null);
        }}
      >
        {addLabel}
      </button>
      {failed && (
        <span className="cl-note wl-act-error" id={id} role="alert">
          {failed}
        </span>
      )}
    </span>
  );
}

/* ── Watch this market (open card) ──────────────────────────────────── */

/** `watching`: on the site's watchlist, for everyone. `local`: kept in this browser (no API, or the API couldn't answer). */
type WatchState = { watching: boolean; local?: boolean };

const watches = makeStore<WatchState>((k) => ({ watching: watchedHere(k), local: true }));

const watchPath = (card: OpenCard) => `/api/watch?story=${encodeURIComponent(card.id)}`;

/** Asks the API whether an open market is on the site's watchlist while its card or panel shows. */
function useWatch(card: OpenCard): Shared<WatchState> {
  const s = useShared(watches, card.id);
  useEffect(() => {
    watches.ask(card.id, watchPath(card), (r) => {
      const w = r as { listed?: boolean; watching?: boolean } | null;
      return w && w.listed === true && typeof w.watching === "boolean" ? { watching: w.watching } : null;
    });
  }, [card]);
  return s;
}

/** Put it on the site's watchlist (a bot check first) or take it off, for everyone. */
async function saveWatch(card: OpenCard, on: boolean, by: Where, slot: HTMLElement | null) {
  const k = card.id;
  const got = watches.get(k).got;
  if (!got || watches.get(k).busy) return;
  const keepHere = () => {
    setWatchedHere(k, on);
    watches.set(k, { busy: false, error: null, got: { watching: on, local: true } });
  };
  if (got.local) return keepHere();
  watches.set(k, { busy: true, error: null, by });
  const r = on
    ? await apiSend<{ watching: boolean }>("POST", "/api/watch", { story: card.id }, { bot: "watch", slot })
    : await apiSend<{ watching: boolean }>("DELETE", watchPath(card));
  if (r.ok && typeof r.data?.watching === "boolean") {
    setWatchedHere(k, r.data.watching);
    watches.set(k, { busy: false, got: { watching: r.data.watching } });
  } else if (r.ok || OFFLINE.has(r.code)) keepHere();
  else watches.set(k, { busy: false, error: r.words });
}

const WATCH = "Watch this market";

/** "Watch this market" in the details panel; "Watching" and "Stop watching" once watched. */
export function WatchControl({ card }: { card: OpenCard }) {
  const { got, busy: saving, error: err, by } = useWatch(card);
  const error = by === "panel" ? err : null;
  const slot = useRef<HTMLSpanElement>(null);
  const focusTo = useRef<HTMLButtonElement>(null);
  const hadFocus = useRef(false);
  useFocusAfter(focusTo, !saving && got !== undefined, hadFocus);

  const save = (on: boolean) => {
    if (saving) return;
    hadFocus.current = document.activeElement instanceof HTMLButtonElement;
    saveWatch(card, on, "panel", slot.current);
  };
  const failed = error && (
    <span className="cl-note cl-note-wrap" role="alert">
      {error}
    </span>
  );

  if (got?.watching)
    return (
      <>
        <span className="cl-add op-watch">
          <span className="cl-add-state">Watching</span>
          <button type="button" ref={focusTo} className="cl-link cl-add-btn wl-act-stop" aria-busy={saving || undefined} onClick={() => save(false)}>
            Stop watching
          </button>
          {failed}
        </span>
        <TsSlot slot={slot} />
      </>
    );

  return (
    <>
      <span className="cl-add op-watch">
        <button type="button" ref={focusTo} className="cl-link cl-add-btn" aria-busy={saving || undefined} onClick={() => save(true)}>
          {WATCH}
        </button>
        {failed}
      </span>
      <TsSlot slot={slot} />
    </>
  );
}

/** "Watch this market" on an open card's face; "Watching" and "Stop watching" once watched. */
export function CardWatch({ card }: { card: OpenCard }) {
  const { got, busy: saving, error: err, by } = useWatch(card);
  const error = by === "card" ? err : null;
  const focusTo = useRef<HTMLButtonElement>(null);
  const hadFocus = useRef(false);
  useFocusAfter(focusTo, !saving && got !== undefined, hadFocus);

  const save = (on: boolean) => {
    if (saving) return;
    hadFocus.current = document.activeElement instanceof HTMLButtonElement;
    saveWatch(card, on, "card", null);
  };
  const failed = error && (
    <span className="cl-note wl-act-error" role="alert">
      {error}
    </span>
  );

  if (got?.watching)
    return (
      <span className="wl-act" onClick={stay}>
        <span className="wl-act-state">Watching</span>
        <button type="button" ref={focusTo} className="cl-link wl-act-stop" aria-busy={saving || undefined} onClick={() => save(false)}>
          Stop watching
        </button>
        {failed}
      </span>
    );

  return (
    <span className="wl-act" onClick={stay}>
      <button type="button" ref={focusTo} className="cl-link wl-act-btn" aria-busy={saving || undefined} onClick={() => save(true)}>
        {WATCH}
      </button>
      {failed}
    </span>
  );
}
