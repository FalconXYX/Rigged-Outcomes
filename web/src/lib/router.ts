import { useSyncExternalStore } from "react";

// Hash routing: GitHub Pages has no SPA fallback, so routes live after `#`.
//   #/                     home
//   #/m/:name              market (timeline view)
//   #/m/:name/:view        market, view = "timeline" | "traders" | "score"
//   #/watchlist            open markets to watch, and closed ones that look
//                          like someone knew (#/closed, its old name, redirects here)
// Every route may carry a query string (`#/m/usiran?range=Leadup&pin=0x…`)
// that views use for shareable state; see useQuery / setQuery.

export type MarketView = "timeline" | "traders" | "score";
export type Query = Record<string, string>;

export type Route =
  | { page: "home"; query: Query }
  | { page: "market"; name: string; view: MarketView; query: Query }
  | { page: "watchlist"; query: Query }
  | { page: "notfound"; path: string; query: Query };

function currentPath() {
  const h = window.location.hash.replace(/^#/, "");
  return h.startsWith("/") ? h : `/${h}`;
}

/** decodeURIComponent throws on malformed escapes (`#/m/100%`); keep those segments raw. */
function decodeSegment(s: string) {
  try {
    return decodeURIComponent(s);
  } catch {
    return s;
  }
}

export function parseQuery(search: string): Query {
  const q: Query = {};
  if (!search) return q;
  for (const [k, v] of new URLSearchParams(search)) q[k] = v;
  return q;
}

export function parseRoute(path: string): Route {
  const qIndex = path.indexOf("?");
  const pathname = qIndex === -1 ? path : path.slice(0, qIndex);
  const query = parseQuery(qIndex === -1 ? "" : path.slice(qIndex + 1));
  const parts = pathname.split("/").filter(Boolean).map(decodeSegment);
  if (parts.length === 0) return { page: "home", query };
  if ((parts[0] === "watchlist" || parts[0] === "closed") && parts.length === 1) return { page: "watchlist", query };
  if (parts[0] === "m" && parts[1]) {
    const view = parts[2] ?? "timeline";
    if ((view === "timeline" || view === "traders" || view === "score") && parts.length <= 3) {
      return { page: "market", name: parts[1], view, query };
    }
  }
  return { page: "notfound", path, query };
}

// `history.replaceState` / `pushState` do not fire hashchange, so query
// updates notify subscribers through a custom event on window; Back and
// Forward over a pushed entry arrive as popstate.
const ROUTE_EVENT = "rigged:routechange";

function subscribe(cb: () => void) {
  window.addEventListener("hashchange", cb);
  window.addEventListener("popstate", cb);
  window.addEventListener(ROUTE_EVENT, cb);
  return () => {
    window.removeEventListener("hashchange", cb);
    window.removeEventListener("popstate", cb);
    window.removeEventListener(ROUTE_EVENT, cb);
  };
}

export function useRoute(): Route {
  const path = useSyncExternalStore(subscribe, currentPath, () => "/");
  return parseRoute(path);
}

/** The current route's query string as a plain object. */
export function useQuery(): Query {
  return useRoute().query;
}

function buildQuery(q: Query): string {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(q)) if (v !== "" && v != null) p.set(k, v);
  const s = p.toString();
  return s ? `?${s}` : "";
}

type QueryPatch = Record<string, string | null | undefined>;

/** The current hash with `patch` merged into its query (a `null`/"" value removes the key). */
function patchedHash(patch: QueryPatch): string {
  const path = currentPath();
  const qIndex = path.indexOf("?");
  const pathname = qIndex === -1 ? path : path.slice(0, qIndex);
  const q = parseQuery(qIndex === -1 ? "" : path.slice(qIndex + 1));
  for (const [k, v] of Object.entries(patch)) {
    if (v == null || v === "") delete q[k];
    else q[k] = v;
  }
  return `#${pathname}${buildQuery(q)}`;
}

/**
 * Merge `patch` into the current query (a `null`/"" value removes the key).
 * Replaces the history entry by default so zooming/pinning doesn't pile up
 * Back-button stops; pass `{ push: true }` for navigation-like changes.
 */
export function setQuery(patch: QueryPatch, { push = false } = {}) {
  const next = patchedHash(patch);
  if (next === window.location.hash) return;
  if (push) {
    window.location.hash = next.slice(1);
  } else {
    window.history.replaceState(window.history.state, "", next);
    window.dispatchEvent(new Event(ROUTE_EVENT));
  }
}

/**
 * Open a Watchlist sheet (`?open=key`) as a new history entry, so the
 * browser's Back closes it. The entry is tagged with the key, which tells
 * closeSheet it can step back instead of writing a new URL.
 */
export function openSheet(key: string) {
  const next = patchedHash({ open: key });
  if (next === window.location.hash) return;
  window.history.pushState({ ...window.history.state, roSheet: key }, "", next);
  window.dispatchEvent(new Event(ROUTE_EVENT));
}

/**
 * Close the sheet `key`: back to the entry it was opened from when openSheet
 * pushed it; otherwise (it came open from a link) replace the URL with `patch`.
 */
export function closeSheet(key: string, patch: QueryPatch) {
  if (window.history.state?.roSheet === key) window.history.back();
  else setQuery(patch);
}

/**
 * Old links: the watchlist was `#/closed` before it was renamed. Swap the
 * path in place (no new history entry), keeping its query string.
 */
export function redirectOldPaths() {
  const h = window.location.hash;
  const old = /^#\/?closed\/?(?=\?|$)/.exec(h);
  if (!old) return;
  window.history.replaceState(window.history.state, "", `#/watchlist${h.slice(old[0].length)}`);
  window.dispatchEvent(new Event(ROUTE_EVENT));
}

export const href = {
  home: () => "#/",
  watchlist: () => "#/watchlist",
  market: (name: string, view: MarketView = "timeline", query?: Query) =>
    `#/m/${encodeURIComponent(name)}${view === "timeline" ? "" : `/${view}`}${query ? buildQuery(query) : ""}`,
};

/** Swap the current hash in place (no new history entry). */
export function replaceHash(hash: string) {
  window.history.replaceState(window.history.state, "", hash.startsWith("#") ? hash : `#${hash}`);
  window.dispatchEvent(new Event(ROUTE_EVENT));
}

export function navigate(hash: string) {
  window.location.hash = hash.replace(/^#/, "");
}
