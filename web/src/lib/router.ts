import { useSyncExternalStore } from "react";

// Hash routing: GitHub Pages has no SPA fallback, so routes live after `#`.
//   #/                     home
//   #/m/:name              market (timeline view)
//   #/m/:name/:view        market, view = "timeline" | "traders"
// Every route may carry a query string (`#/m/usiran?range=Leadup&pin=0x…`)
// that views use for shareable state; see useQuery / setQuery.

export type MarketView = "timeline" | "traders";
export type Query = Record<string, string>;

export type Route =
  | { page: "home"; query: Query }
  | { page: "market"; name: string; view: MarketView; query: Query }
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
  if (parts[0] === "m" && parts[1]) {
    const view = parts[2] ?? "timeline";
    if ((view === "timeline" || view === "traders") && parts.length <= 3) {
      return { page: "market", name: parts[1], view, query };
    }
  }
  return { page: "notfound", path, query };
}

// `history.replaceState` does not fire hashchange, so query updates notify
// subscribers through a custom event on window.
const ROUTE_EVENT = "rigged:routechange";

function subscribe(cb: () => void) {
  window.addEventListener("hashchange", cb);
  window.addEventListener(ROUTE_EVENT, cb);
  return () => {
    window.removeEventListener("hashchange", cb);
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

/**
 * Merge `patch` into the current query (a `null`/"" value removes the key).
 * Replaces the history entry by default so zooming/pinning doesn't pile up
 * Back-button stops; pass `{ push: true }` for navigation-like changes.
 */
export function setQuery(patch: Record<string, string | null | undefined>, { push = false } = {}) {
  const path = currentPath();
  const qIndex = path.indexOf("?");
  const pathname = qIndex === -1 ? path : path.slice(0, qIndex);
  const q = parseQuery(qIndex === -1 ? "" : path.slice(qIndex + 1));
  for (const [k, v] of Object.entries(patch)) {
    if (v == null || v === "") delete q[k];
    else q[k] = v;
  }
  const next = `#${pathname}${buildQuery(q)}`;
  if (next === window.location.hash) return;
  if (push) {
    window.location.hash = next.slice(1);
  } else {
    window.history.replaceState(window.history.state, "", next);
    window.dispatchEvent(new Event(ROUTE_EVENT));
  }
}

export const href = {
  home: () => "#/",
  market: (name: string, view: MarketView = "timeline", query?: Query) =>
    `#/m/${encodeURIComponent(name)}${view === "timeline" ? "" : `/${view}`}${query ? buildQuery(query) : ""}`,
};

export function navigate(hash: string) {
  window.location.hash = hash.replace(/^#/, "");
}
