import { useCallback, useEffect, useState, useSyncExternalStore } from "react";
import type { ClosedData, MarketData, MarketIndex, MarketIndexEntry, OpenData } from "../types";
import { API_BASE } from "./api";

// Where the site's data comes from (docs/BACKEND.md). Every market, the 7 original ones and those added with
// "Add to main page" (the index and each market's page), and the Watchlist's two lists, the Worker serves from
// its database (/api/data/...). The last good copy of the index and of each list is kept in this browser, and
// shown while the API is down or over its free daily limit.

// Relative to index.html, which works from any GitHub Pages sub-path.
const BASE = import.meta.env.BASE_URL;
/** The Worker's published files ("" in a build with no API: no markets, and the lists from the site's own data/). */
const LIVE = API_BASE ? `${API_BASE}/api/data/` : "";

/** a published file the Worker doesn't answer within this long reads as unanswered (its kept copy stands in) */
const LIVE_MS = 8000;

/** The Worker has no such file yet (404): nothing published, as opposed to no answer. */
class Missing extends Error {}

/**
 * One fetch of a data file. "no-cache" revalidates with the file's ETag, so an unchanged file is a cheap 304
 * (Pages serves max-age=600, and a plain fetch could keep a copy ten minutes old); "reload" skips the
 * browser's cache altogether. Never an If-None-Match set by hand: on the API it would force a CORS preflight.
 */
async function fetchText(url: string, init: RequestInit = {}): Promise<string> {
  const res = await fetch(url, { cache: "no-cache", ...init });
  if (res.status === 404) throw new Missing(`no file at ${url}`);
  if (!res.ok) throw new Error(`${res.status} loading ${url}`);
  return res.text();
}

/** The same from the API, given up on after LIVE_MS (or `signal`). */
async function liveFetch(url: string, cache: RequestCache, signal?: AbortSignal): Promise<string> {
  const ctrl = new AbortController();
  const timer = window.setTimeout(() => ctrl.abort(), LIVE_MS);
  const stop = () => ctrl.abort();
  signal?.addEventListener("abort", stop);
  try {
    return await fetchText(url, { cache, credentials: "omit", signal: ctrl.signal });
  } finally {
    window.clearTimeout(timer);
    signal?.removeEventListener("abort", stop);
  }
}

/* ── The last good copies (localStorage; storage that is off keeps none) ── */

type Key = "index" | "closed" | "open";
const KEPT: Record<Key, string> = { index: "ro-live-index", closed: "ro-list-closed", open: "ro-list-open" };

function kept(key: Key): string | null {
  try {
    return window.localStorage.getItem(KEPT[key]);
  } catch {
    return null;
  }
}

function keep(key: Key, text: string | null) {
  try {
    if (text == null) window.localStorage.removeItem(KEPT[key]);
    else window.localStorage.setItem(KEPT[key], text);
  } catch {
    /* storage off or full: the next visit asks the API again */
  }
}

/**
 * A file the Worker publishes, as text: fetched and kept as the last good copy; with no answer (the API down,
 * over its daily limit, slow, not this file) the kept copy (no copy of the index: it fails); null when nothing is
 * published yet.
 */
async function liveText(key: Key, ok: (d: unknown) => boolean, cache: RequestCache, signal?: AbortSignal): Promise<string | null> {
  try {
    const text = await liveFetch(`${LIVE}${key}.json`, cache, signal);
    if (!ok(JSON.parse(text))) throw new Error(`not the ${key} file`);
    keep(key, text);
    return text;
  } catch (e) {
    if (e instanceof Missing) {
      keep(key, null);
      return null;
    }
    const copy = kept(key);
    if (copy === null && key === "index") throw e;
    return copy;
  }
}

/* ── Reading the files ────────────────────────────────────────────────── */

/** A file as read: its data, and `sig` (its text) to tell a newer copy from the one shown. */
type Loaded<T> = { sig: string; data: T };

/** A market's index entry: `v`, its page's version on the Worker. */
type LiveEntry = MarketIndexEntry & { v?: string };

const hasMarkets = (d: unknown): d is MarketIndex => !!d && Array.isArray((d as MarketIndex).markets);
const hasDays = (d: unknown) => !!d && Array.isArray((d as { days?: unknown }).days);
const listOrNull = <T extends { days?: unknown }>(d: T | null): T | null => (d && hasDays(d) ? d : null);

/** A slug's market part, as the index and the closed list are matched ("event/market#id" → "market"). */
export const bareSlug = (slug: string) => slug.split("/").pop()!.split("#")[0];

/** The index every view shows: the Worker's (none published yet, or a build with no API: no markets). */
async function readIndex(cache: RequestCache, signal?: AbortSignal): Promise<Loaded<MarketIndex>> {
  const text = LIVE ? await liveText("index", hasMarkets, cache, signal) : null;
  let index: MarketIndex = { markets: [] };
  try {
    if (text) index = JSON.parse(text) as MarketIndex;
  } catch {
    /* a kept copy that no longer reads: none */
  }
  return { sig: text ?? "", data: index };
}

/** A list: from the Worker (null while none is published), or in a build with no API the site's own copy. */
async function readList<T extends { days?: unknown }>(key: "closed" | "open", cache: RequestCache): Promise<Loaded<T | null>> {
  let text: string | null = null;
  if (LIVE) text = await liveText(key, hasDays, cache);
  else
    try {
      text = await fetchText(`${BASE}data/${key}.json`, { cache });
    } catch {
      /* none (or the dev server's HTML fallback, below) */
    }
  let data: T | null = null;
  try {
    data = text ? listOrNull(JSON.parse(text) as T) : null;
  } catch {
    /* the dev server's HTML fallback for a missing file */
  }
  return { sig: text ?? "", data };
}

const READ: Record<Key, (cache: RequestCache) => Promise<Loaded<unknown>>> = {
  index: (cache) => readIndex(cache),
  closed: (cache) => readList<ClosedData>("closed", cache),
  open: (cache) => readList<OpenData>("open", cache),
};

/* ── Right after this visitor's add ───────────────────────────────────── */

/** for this long after this visitor's add, the index is read past every cache */
const FRESH_FOR_MS = 30 * 60_000;
let freshUntil = 0;

/** This visitor just added a market to the main page: the index is read past every cache for a while. */
export function addedHere() {
  freshUntil = Date.now() + FRESH_FOR_MS;
}

const modeFor = (key: Key): RequestCache => (key === "index" && Date.now() < freshUntil ? "reload" : "no-cache");

/* ── The loaders ──────────────────────────────────────────────────────── */

const cache = new Map<string, Promise<unknown>>();
/** each file's `sig` as last read, so a read that brings the same file changes nothing */
const sigs = new Map<Key, string>();

function first<T>(key: Key): Promise<T> {
  let p = cache.get(key) as Promise<T> | undefined;
  if (!p) {
    p = READ[key](modeFor(key)).then(({ sig, data }) => {
      sigs.set(key, sig);
      return data as T;
    });
    p.catch(() => cache.delete(key));
    cache.set(key, p);
  }
  return p;
}

/** A market page, by its address: fetched once a session (a versioned page is cached for good). */
function getJSON<T>(url: string, init: RequestInit = {}): Promise<T> {
  let p = cache.get(url) as Promise<T> | undefined;
  if (!p) {
    p = fetchText(url, init).then((text) => JSON.parse(text) as T);
    p.catch(() => cache.delete(url));
    cache.set(url, p);
  }
  return p;
}

/** Where a market's page is on the Worker (by its version, when the index has it). */
const marketUrl = (name: string, e?: LiveEntry) =>
  `${LIVE}markets/${encodeURIComponent(name)}.json${e?.v ? `?v=${encodeURIComponent(e.v)}` : ""}`;

export const loadIndex = () => first<MarketIndex>("index");

export async function loadMarket(name: string): Promise<MarketData> {
  const index = await loadIndex().catch(() => null);
  const e = index?.markets.find((m) => m.name === name) as LiveEntry | undefined;
  if (!LIVE) throw new Error(`no market ${name} in a build with no API`);
  // a versioned page never changes: the browser's copy is used as it is
  return getJSON<MarketData>(marketUrl(name, e), e?.v ? { cache: "default", credentials: "omit" } : { credentials: "omit" });
}

/** The closed list, or null while there is none (nothing published yet, or no copy to show). */
export const loadClosed = (): Promise<ClosedData | null> => first<ClosedData | null>("closed").catch(() => null);
/** The open list, or null while there is none. */
export const loadOpen = (): Promise<OpenData | null> => first<OpenData | null>("open").catch(() => null);

export type Async<T> =
  | { status: "loading"; data?: undefined; error?: undefined }
  | { status: "ready"; data: T; error?: undefined }
  | { status: "error"; data?: undefined; error: Error };

/** Load a promise-returning resource keyed by `key`; re-runs when key changes. */
export function useAsync<T>(fn: () => Promise<T>, key: string): Async<T> {
  const [state, setState] = useState<{ key: string; value: Async<T> }>({
    key,
    value: { status: "loading" },
  });

  useEffect(() => {
    let live = true;
    fn().then(
      (data) => live && setState({ key, value: { status: "ready", data } }),
      (error: Error) => live && setState({ key, value: { status: "error", error } }),
    );
    return () => {
      live = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);

  return state.key === key ? state.value : { status: "loading" };
}

/* ── The files the backend republishes ────────────────────────────────── */
// The index (a market added to the main page joins it), the closed list and the open list change as the backend
// publishes: an add, the day's lists. While a view that shows one is open and in
// view, it is read again every REFRESH_MS and when the page comes back into view (a cheap 304 while it is
// unchanged); a newer copy takes the old one's place in every view. Every read is a request to the Worker, whose
// free plan allows 100,000 a day for the site and the backend together, so a tab left open asks rarely: the
// index every FAST_MS only for a while after this visitor's own add.

const REFRESH_MS = 5 * 60_000;
const FAST_MS = 30_000;
/** a page coming back into view asks again only when the last ask is older than this */
const AGAIN_MS = 60_000;

type Newer = { value: unknown; subs: Set<() => void>; at: number; asking: boolean };
const newer = new Map<Key, Newer>();

const newerOf = (key: Key): Newer => {
  let n = newer.get(key);
  if (!n) newer.set(key, (n = { value: null, subs: new Set(), at: Date.now(), asking: false }));
  return n;
};

/** The file's newer copy: every view showing it gets it, and the next load of it too. */
function publish(key: Key, got: Loaded<unknown>) {
  sigs.set(key, got.sig);
  cache.set(key, Promise.resolve(got.data));
  const n = newerOf(key);
  n.value = { data: got.data };
  n.subs.forEach((f) => f());
}

/** Read `key` again; a copy that differs from the last one is published. */
async function refresh(key: Key) {
  const n = newerOf(key);
  if (n.asking) return;
  n.asking = true;
  n.at = Date.now();
  try {
    const got = await READ[key](modeFor(key));
    if (got.sig !== sigs.get(key)) publish(key, got);
  } catch {
    /* unreachable or unreadable: the copy shown stays */
  } finally {
    n.asking = false;
  }
}

let timer: number | null = null;
const watched = () => [...newer.entries()].filter(([, n]) => n.subs.size).map(([key]) => key);
const tick = (every: number) => {
  if (document.hidden) return;
  for (const key of watched()) {
    const wait = key === "index" && Date.now() < freshUntil ? Math.min(every, FAST_MS - 1000) : every;
    if (Date.now() - newerOf(key).at >= wait) void refresh(key);
  }
};
const onTick = () => tick(REFRESH_MS - 1000);
const onShow = () => tick(AGAIN_MS);

function follow(key: Key, f: () => void): () => void {
  newerOf(key).subs.add(f);
  if (timer === null) {
    timer = window.setInterval(onTick, 5000);
    document.addEventListener("visibilitychange", onShow);
    window.addEventListener("focus", onShow);
  }
  return () => {
    newerOf(key).subs.delete(f);
    if (!watched().length && timer !== null) {
      window.clearInterval(timer);
      timer = null;
      document.removeEventListener("visibilitychange", onShow);
      window.removeEventListener("focus", onShow);
    }
  };
}

/** `load` (the first read of `key`), then each newer copy of it while the view is open. */
function useNewer<T>(key: Key, load: () => Promise<T>): Async<T> {
  const loaded = useAsync(load, key);
  const subscribe = useCallback((f: () => void) => follow(key, f), [key]);
  const value = useSyncExternalStore(subscribe, () => newer.get(key)?.value ?? null) as { data: T } | null;
  return value === null ? loaded : { status: "ready", data: value.data };
}

/** The index (the original markets and the added ones), kept up to date while it shows. */
export const useIndex = (): Async<MarketIndex> => useNewer("index", loadIndex);
/** The closed list, kept up to date while it shows (null while there is none). */
export const useClosed = (): Async<ClosedData | null> => useNewer("closed", loadClosed);
/** The open list, kept up to date while it shows (null while there is none). */
export const useOpen = (): Async<OpenData | null> => useNewer("open", loadOpen);

const SITE_MS = 8000;

/**
 * Whether the site serves the market with slug `slug` yet: it is in the index, read past every cache, and its
 * page is there. Once it is, that index is the one every view uses. False on any failure.
 */
export async function onSite(slug: string): Promise<boolean> {
  const ctrl = new AbortController();
  const timer = window.setTimeout(() => ctrl.abort(), SITE_MS);
  try {
    const got = await readIndex("reload", ctrl.signal);
    const m = got.data.markets.find((e) => bareSlug(e.slug) === slug) as LiveEntry | undefined;
    if (!m) return false;
    // a missing file can come back as the dev server's HTML page
    const file = await fetch(marketUrl(m.name, m), { method: "HEAD", cache: "reload", signal: ctrl.signal, credentials: "omit" });
    if (!file.ok || !file.headers.get("content-type")?.includes("json")) return false;
    if (got.sig !== sigs.get("index")) publish("index", got);
    return true;
  } catch {
    return false;
  } finally {
    window.clearTimeout(timer);
  }
}
