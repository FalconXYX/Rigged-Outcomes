import { useCallback, useEffect, useRef, useSyncExternalStore, type RefObject } from "react";
import type { ClosedPick, OpenCard } from "../../types";
import { href } from "../../lib/router";
import { addedHere, onSite } from "../../lib/data";
import {
  OFFLINE,
  WORDS,
  apiConfigured,
  apiGet,
  apiSend,
  requestedHere,
  setRequestedHere,
  setWatchToSend,
  setWatchedHere,
  watchToSend,
  watchedHere,
  type PublicRequest,
} from "../../lib/api";

// The Watchlist's two actions: "Add to main page" (closed card: the click
// queues an add run on the backend, which puts the market on the main page
// within minutes) and "Watch this market" (open card: the site has one
// watchlist, the same for everyone; a watched market that closes with a result
// is added to the main page by the daily run). Each shows in two places: on
// the card's face, beside "Details" (CardAdd, CardWatch), and in the card's
// details panel (AddToMain, WatchControl). Both places share one state per
// market, so they always agree. Until the API first answers, a market shows
// what this browser last saw of it (so a reload never offers "Watch this
// market" for a watched one, and a click then is kept, not lost). A market's
// state is asked of the API when its card shows, and again while the page is
// in view: a watch every minute (someone else's watch shows by itself), a
// market on its way to the main page every 15 seconds at first (less often
// after ten minutes, and after an hour), and either one after asks the API
// didn't answer (less often for each); once the add run has added a market,
// the index is read past every cache instead, until it lists the market and
// its page is served. With no API in
// the build, or the API not answering, the controls work the same and keep
// what this browser did, and a click kept that way is sent once the API
// answers; only a refusal a visitor should
// read (a daily limit, a failed bot check, an add that couldn't be made) is
// shown.

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
 * A market's control state: what the API said (or, `local`, what this browser kept: before the API's first
 * answer, or while it doesn't answer), a write on its way, the last error with the place it was sent from (only
 * that place shows it; null: both do), when the API was last asked, and the asks in a row it didn't answer.
 */
type Shared<S> = { got: S; busy: boolean; error: string | null; by: Where | null; at: number; misses: number };

/** a state asked for less than this long ago is not asked for again */
const FRESH_MS = 60_000;
/** each ask in a row the API doesn't answer doubles that wait, up to this */
const BACKOFF_MS = 16 * 60_000;

/** A state that may be the one kept in this browser (`local`). */
type Kept = { local?: boolean };

/**
 * `local(k)`: the state this browser keeps for `k`, shown until the API answers. `changed(before, after)`: a
 * change to the state's other fields when an answer moves `k` from one state to another.
 */
function makeStore<S extends Kept>(local: (k: string) => S, changed?: (before: S, after: S) => Partial<Shared<S>> | undefined) {
  const all = new Map<string, Shared<S>>();
  const subs = new Map<string, Set<() => void>>();
  const asking = new Set<string>();
  const get = (k: string) => {
    let s = all.get(k);
    if (!s) {
      s = { got: local(k), busy: false, error: null, by: null, at: 0, misses: 0 };
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
  /**
   * Ask the API for `k`'s state, unless it is being asked or was asked within `every` (longer after asks
   * it didn't answer: twice FRESH_MS for each, up to BACKOFF_MS). `accept` reads the answer (null: the
   * browser's state). With no answer the state shown stays.
   */
  const ask = (k: string, path: string, accept: (r: unknown) => S | null, every = FRESH_MS) => {
    const s = get(k);
    const wait = s.misses ? Math.max(every, Math.min(BACKOFF_MS, FRESH_MS * 2 ** s.misses)) : every;
    if (!apiConfigured || asking.has(k) || Date.now() - s.at < wait) return;
    asking.add(k);
    apiGet<unknown>(path).then((r) => {
      asking.delete(k);
      const now = get(k);
      // a write on its way has the newer answer
      if (now.busy) return;
      const got = r == null ? now.got : (accept(r) ?? local(k));
      set(k, { got, at: Date.now(), misses: r == null ? now.misses + 1 : 0, ...(changed?.(now.got, got) ?? {}) });
    });
  };
  return { get, set, subscribe, ask };
}

type Store<S extends Kept> = ReturnType<typeof makeStore<S>>;

function useShared<S extends Kept>(store: Store<S>, k: string): Shared<S> {
  const subscribe = useCallback((f: () => void) => store.subscribe(k, f), [store, k]);
  return useSyncExternalStore(subscribe, () => store.get(k));
}

/** A click on the card's action never opens the card's details. */
const stay = (e: { stopPropagation(): void }) => e.stopPropagation();

/** while a market waits (for its add run, or for the API to answer), how often its card checks whether it is time to ask again */
const TICK_MS = 15_000;

/** Runs `f` every TICK_MS while the page is in view, and when it comes back into view or online; returns the stop. */
function whileShown(f: () => void): () => void {
  const tick = () => {
    if (!document.hidden) f();
  };
  const timer = window.setInterval(tick, TICK_MS);
  document.addEventListener("visibilitychange", tick);
  window.addEventListener("online", tick);
  return () => {
    window.clearInterval(timer);
    document.removeEventListener("visibilitychange", tick);
    window.removeEventListener("online", tick);
  };
}

/* ── Add to main page (closed card) ─────────────────────────────────── */

/** `local`: kept in this browser (before the API answers, no API, or the API couldn't answer). */
type RequestState = { request: PublicRequest | null; local?: boolean };

const ADDED_HERE: PublicRequest = { status: "approved", name: null, decided_at: null, done_at: null };

/** A market on its way to the main page (or there); one its add run couldn't add (failed) offers the button again. */
const onItsWay = (r: PublicRequest | null) => r?.status === "approved" || r?.status === "added";

/** What a market taken off the main page reads, in place of its button. */
const REMOVED = "Taken off the main page";

const requests = makeStore<RequestState>(
  (k) => ({ request: requestedHere(k) ? ADDED_HERE : null, local: true }),
  // a market this page saw on its way that its add run then couldn't add: both places say so, until the next click
  (before, after) =>
    onItsWay(before.request) && after.request?.status === "failed" ? { error: WORDS.add_failed, by: null } : undefined,
);

/** What a market on its way to the main page reads until the site serves it (then "On the main page →"). */
const ADDING = "Adding to the main page";

/** a market on its way is asked about every FAST_MS for its first FAST_FOR seconds, then every minute */
const FAST_MS = 15_000;
const FAST_FOR = 600;
/** once a market has been on its way this long (seconds), it is asked about every SLOW_MS */
const SLOW_AFTER = 3600;
const SLOW_MS = 10 * 60_000;

/** How often to ask about a market on its way since `since` (epoch seconds). */
function pace(since: number | null | undefined): number {
  const age = since ? Date.now() / 1000 - since : 0;
  return age > SLOW_AFTER ? SLOW_MS : age < FAST_FOR ? FAST_MS : FRESH_MS;
}

/** How often to look for an added market in the index since its add run added it (`since`): each tick at first. */
const sitePace = (since: number | null | undefined) => (since && Date.now() / 1000 - since < FAST_FOR ? TICK_MS : pace(since));

/** when each added market was last looked for on the site, so its card and its panel look once between them */
const looked = new Map<string, number>();

/** Look for an added market in the index, at most once every `every` ms (onSite: once it is there, its card gets `onMain`). */
function lookFor(slug: string, every: number) {
  if (Date.now() - (looked.get(slug) ?? 0) < every - 1000) return;
  looked.set(slug, Date.now());
  void onSite(slug);
}

/**
 * Reads the API's answer for closed market `k`, keeping what it says in this browser, and sends a click this
 * browser kept for it (it still reads "Adding") once the API answers.
 */
const readRequest = (k: string) => (r: unknown): RequestState | null => {
  const v = r as { listed?: boolean; request?: PublicRequest | null } | null;
  if (!v) return null;
  // a market on its way keeps its state even once its card has left the closed list
  if (v.request) {
    setRequestedHere(k, onItsWay(v.request));
    return { request: v.request };
  }
  if (v.listed !== true) return null;
  if (!requestedHere(k)) return { request: null };
  queueMicrotask(() => void sendRequest(k, "card", null));
  return { request: ADDED_HERE };
};

/**
 * A closed market's state, while its card or panel shows (not once it is on the main page). The API is asked
 * when it shows, and again while the page is in view and the market waits for its add run (or the API didn't
 * answer). Once the run has added it, the index is read instead (past every cache), and its newer copy then
 * gives the card `onMain`.
 */
function useRequest(p: ClosedPick, onMain: string | null): Shared<RequestState> {
  const k = p.condition_id;
  const s = useShared(requests, k);
  const known = s.got.local ? null : s.got.request;
  const status = known?.status;
  const since = status === "approved" ? known?.decided_at : known?.done_at;
  const again = apiConfigured && (status === "approved" || s.misses > 0);
  const slug = p.slug;
  useEffect(() => {
    if (onMain || status === "added") return;
    const ask = () => requests.ask(k, `/api/request?cid=${encodeURIComponent(k)}`, readRequest(k), pace(since));
    ask();
    return again ? whileShown(ask) : undefined;
  }, [k, onMain, status, since, again]);
  useEffect(() => {
    if (onMain || status !== "added") return;
    const look = () => lookFor(slug, sitePace(since));
    look();
    return whileShown(look);
  }, [slug, onMain, status, since]);
  return s;
}

/**
 * Add closed market `k` to the main page (a bot check first, shown in `slot` if Cloudflare asks for a click,
 * else in a corner box). With no answer the click is kept here and sent again once the API answers.
 */
async function sendRequest(k: string, by: Where, slot: HTMLElement | null) {
  const { got, busy } = requests.get(k);
  if (busy) return;
  const keepHere = () => {
    setRequestedHere(k);
    // asked again until the API answers, from a 2-minute wait
    requests.set(k, { busy: false, error: null, got: { request: ADDED_HERE, local: true }, misses: apiConfigured ? 1 : 0 });
  };
  if (got.local) return keepHere();
  requests.set(k, { busy: true, error: null, by });
  const r = await apiSend<{ request: PublicRequest }>("POST", "/api/request", { condition_id: k }, { bot: "request", slot });
  if (r.ok && r.data?.request) {
    setRequestedHere(k, onItsWay(r.data.request));
    if (onItsWay(r.data.request)) addedHere();
    requests.set(k, { busy: false, got: { request: r.data.request }, at: Date.now(), misses: 0 });
  } else if (r.ok || OFFLINE.has(r.code)) keepHere();
  else {
    setRequestedHere(k, false);
    requests.set(k, { busy: false, error: r.words, got: { request: null } });
  }
}

const addLabel = (
  <>
    <Plus /> Add to main page
  </>
);

/**
 * "+ Add to main page" in the details panel: the closed market gets its own
 * page, which the add run the click queues builds; "Adding to the main page"
 * until then, and once it is there the link to it.
 */
export function AddToMain({ p, onMain }: { p: ClosedPick; onMain: string | null }) {
  const id = `cl-note-${p.condition_id.slice(0, 12)}`;
  const { got, busy, error, by } = useRequest(p, onMain);
  const slot = useRef<HTMLSpanElement>(null);
  const status = useRef<HTMLSpanElement>(null);
  const hadFocus = useRef(false);
  const adding = onItsWay(got.request);
  useFocusAfter(status, adding, hadFocus);

  if (onMain)
    return (
      <a className="cl-link cl-add-btn" href={href.market(onMain)}>
        On the main page →
      </a>
    );

  if (adding || got.request?.status === "removed")
    return (
      <span className="cl-add" ref={status} tabIndex={-1}>
        <span className="cl-add-state">{adding ? ADDING : REMOVED}</span>
      </span>
    );

  const failed = error && by !== "card" ? error : null;
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
            sendRequest(p.condition_id, "panel", slot.current);
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
  const adding = onItsWay(got.request);
  useFocusAfter(status, adding, hadFocus);

  if (onMain)
    return (
      <span className="wl-act" onClick={stay}>
        <a className="cl-link wl-act-btn" href={href.market(onMain)}>
          On the main page →
        </a>
      </span>
    );
  if (adding || got.request?.status === "removed")
    return (
      <span className="wl-act" ref={status} tabIndex={-1} onClick={stay}>
        <span className="wl-act-state">{adding ? ADDING : REMOVED}</span>
      </span>
    );
  const failed = error && by !== "panel" ? error : null;
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
          sendRequest(p.condition_id, "card", null);
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

/** `watching`: on the site's watchlist, for everyone. `local`: kept in this browser (before the API answers, no API, or the API couldn't answer). */
type WatchState = { watching: boolean; local?: boolean };

/** What this browser keeps: a change it couldn't send yet, else what it last saw. */
const watches = makeStore<WatchState>((k) => ({ watching: watchToSend(k) ?? watchedHere(k), local: true }));

const watchPath = (k: string) => `/api/watch?story=${encodeURIComponent(k)}`;

/**
 * Reads the API's answer for open market `k`, keeping what it says in this browser, and sends a change this
 * browser kept for it (shown as made) once the API answers.
 */
const readWatch = (k: string) => (r: unknown): WatchState | null => {
  const w = r as { listed?: boolean; watching?: boolean } | null;
  if (!w || w.listed !== true || typeof w.watching !== "boolean") return null;
  const mine = watchToSend(k);
  if (mine === null || mine === w.watching) {
    setWatchToSend(k, null);
    setWatchedHere(k, w.watching);
    return { watching: w.watching };
  }
  queueMicrotask(() => void saveWatch(k, mine, "card", null));
  return { watching: mine };
};

/** Asks the API whether an open market is on the site's watchlist while its card or panel shows, every minute while the page is in view. */
function useWatch(card: OpenCard): Shared<WatchState> {
  const k = card.id;
  const s = useShared(watches, k);
  useEffect(() => {
    const ask = () => watches.ask(k, watchPath(k), readWatch(k));
    ask();
    return apiConfigured ? whileShown(ask) : undefined;
  }, [k]);
  return s;
}

/** Put it on the site's watchlist (a bot check first) or take it off, for everyone; kept here and sent later when the API can't take it now. */
async function saveWatch(k: string, on: boolean, by: Where, slot: HTMLElement | null) {
  const { got, busy } = watches.get(k);
  if (busy) return;
  const keepHere = () => {
    setWatchedHere(k, on);
    if (apiConfigured) setWatchToSend(k, on);
    watches.set(k, { busy: false, error: null, got: { watching: on, local: true } });
  };
  if (got.local) return keepHere();
  watches.set(k, { busy: true, error: null, by });
  const r = on
    ? await apiSend<{ watching: boolean }>("POST", "/api/watch", { story: k }, { bot: "watch", slot })
    : await apiSend<{ watching: boolean }>("DELETE", watchPath(k));
  if (r.ok && typeof r.data?.watching === "boolean") {
    setWatchToSend(k, null);
    setWatchedHere(k, r.data.watching);
    watches.set(k, { busy: false, got: { watching: r.data.watching } });
  } else if (r.ok || OFFLINE.has(r.code)) keepHere();
  else {
    setWatchToSend(k, null);
    watches.set(k, { busy: false, error: r.words });
  }
}

const WATCH = "Watch this market";

/** "Watch this market" in the details panel; "Watching" and "Stop watching" once watched. */
export function WatchControl({ card }: { card: OpenCard }) {
  const { got, busy: saving, error: err, by } = useWatch(card);
  const error = by !== "card" ? err : null;
  const slot = useRef<HTMLSpanElement>(null);
  const focusTo = useRef<HTMLButtonElement>(null);
  const hadFocus = useRef(false);
  useFocusAfter(focusTo, !saving, hadFocus);

  const save = (on: boolean) => {
    if (saving) return;
    hadFocus.current = document.activeElement instanceof HTMLButtonElement;
    saveWatch(card.id, on, "panel", slot.current);
  };
  const failed = error && (
    <span className="cl-note cl-note-wrap" role="alert">
      {error}
    </span>
  );

  if (got.watching)
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
  const error = by !== "panel" ? err : null;
  const focusTo = useRef<HTMLButtonElement>(null);
  const hadFocus = useRef(false);
  useFocusAfter(focusTo, !saving, hadFocus);

  const save = (on: boolean) => {
    if (saving) return;
    hadFocus.current = document.activeElement instanceof HTMLButtonElement;
    saveWatch(card.id, on, "card", null);
  };
  const failed = error && (
    <span className="cl-note wl-act-error" role="alert">
      {error}
    </span>
  );

  if (got.watching)
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
