import { useEffect, useState } from "react";
import type { MarketData, MarketIndex } from "../types";

// Relative to index.html, which works from any GitHub Pages sub-path.
const BASE = import.meta.env.BASE_URL;

const cache = new Map<string, Promise<unknown>>();

function getJSON<T>(path: string): Promise<T> {
  let p = cache.get(path) as Promise<T> | undefined;
  if (!p) {
    p = fetch(`${BASE}${path}`).then((res) => {
      if (!res.ok) throw new Error(`${res.status} loading ${path}`);
      return res.json() as Promise<T>;
    });
    p.catch(() => cache.delete(path));
    cache.set(path, p);
  }
  return p;
}

export const loadIndex = () => getJSON<MarketIndex>("data/index.json");
export const loadMarket = (name: string) =>
  getJSON<MarketData>(`data/markets/${encodeURIComponent(name)}.json`);

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
