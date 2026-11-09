import { Component, Suspense, lazy, useEffect, useLayoutEffect, useRef, useState, type ComponentType, type ErrorInfo, type ReactNode } from "react";
import { href, useRoute } from "./lib/router";
import { loadIndex, useAsync } from "./lib/data";
import { SiteHeader } from "./components/SiteHeader";
import { SiteFooter } from "./components/SiteFooter";
import { ErrorNote, Loading } from "./components/ui";
import { Home } from "./views/Home";
import { NotFound } from "./views/NotFound";

const SITE = "Rigged Outcomes";

// Every section after the home page is its own chunk: the landing page ships
// only what it draws, and the rest is fetched during idle time right after
// first paint (see `preloadRoutes`), so navigating later needs no round-trip.
//
// `React.lazy` alone would still flash the Suspense fallback for a frame the
// first time a route renders, even with its chunk in cache. `routeChunk`
// renders the component directly once its module has resolved and only
// suspends on a genuinely cold load; the choice is fixed per mount so a page
// never remounts (and loses its state) while in use.
function routeChunk<P extends object>(load: () => Promise<ComponentType<P>>) {
  let ready: ComponentType<P> | null = null;
  let promise: Promise<ComponentType<P>> | null = null;
  const preload = () =>
    (promise ??= load().then(
      (c) => (ready = c),
      (e: unknown) => {
        promise = null; // a failed fetch (offline, mid-deploy) is retried on the next render
        throw e;
      },
    ));
  const Lazy = lazy(() => preload().then((c) => ({ default: c }))) as unknown as ComponentType<P>;
  const Route = (props: P) => {
    const [C] = useState(() => ready ?? Lazy);
    return <C {...props} />;
  };
  return { Route, preload };
}

const market = routeChunk(() => import("./views/MarketPage").then((m) => m.MarketPage));
const { Route: MarketPage } = market;

function preloadRoutes() {
  const run = () => {
    market.preload().catch(() => {});
  };
  if (typeof window.requestIdleCallback === "function") {
    const id = window.requestIdleCallback(run, { timeout: 4000 });
    return () => window.cancelIdleCallback(id);
  }
  const id = window.setTimeout(run, 1500);
  return () => window.clearTimeout(id);
}

/**
 * A deploy while a tab is open changes the chunk hashes, so the next route's
 * chunk 404s. Vite reports that as `vite:preloadError`; reload once to pick up
 * the new index. A second failure on the same URL falls through to the
 * ViewBoundary's error note instead of looping.
 */
function reloadOnStaleChunk() {
  const onError = (e: Event) => {
    const key = "ro-chunk-reload";
    try {
      if (sessionStorage.getItem(key) === window.location.href) return;
      sessionStorage.setItem(key, window.location.href);
    } catch {
      return;
    }
    e.preventDefault();
    window.location.reload();
  };
  window.addEventListener("vite:preloadError", onError);
  return () => window.removeEventListener("vite:preloadError", onError);
}

/**
 * Keeps a crash in one view from unmounting the whole app (header, footer and
 * routing included). Clears itself when `resetKey` (the route) changes, so
 * navigating away — or browser Back — recovers without a reload. A route
 * chunk that fails to download lands here too (the Reload button fetches it).
 */
class ViewBoundary extends Component<{ resetKey: string; children: ReactNode }, { error: Error | null; key: string }> {
  state = { error: null as Error | null, key: this.props.resetKey };

  static getDerivedStateFromError(error: unknown) {
    return { error: error instanceof Error ? error : new Error(String(error)) };
  }

  static getDerivedStateFromProps(props: { resetKey: string }, state: { error: Error | null; key: string }) {
    return props.resetKey !== state.key ? { error: null, key: props.resetKey } : null;
  }

  componentDidCatch(error: unknown, info: ErrorInfo) {
    console.error("View crashed:", error, info.componentStack);
  }

  render() {
    if (!this.state.error) return this.props.children;
    return (
      <div className="container">
        <ErrorNote error={this.state.error} title="This view hit an error.">
          <a className="btn btn-solid" href={href.home()}>
            All markets
          </a>
          <button type="button" className="btn btn-ghost" onClick={() => window.location.reload()}>
            Reload
          </button>
        </ErrorNote>
      </div>
    );
  }
}

export default function App() {
  const route = useRoute();
  // Loaded once for the whole session: home grid, market header and switcher, titles.
  const index = useAsync(loadIndex, "index");

  const page = route.page;
  const name = route.page === "market" ? route.name : null;
  const marketTitle = name ? index.data?.markets.find((m) => m.name === name)?.title : undefined;

  useEffect(() => {
    if (page === "home") document.title = `${SITE} · Polymarket insider-trading signals`;
    else if (page === "market")
      document.title = marketTitle
        ? `${marketTitle} · ${SITE}`
        : index.status === "ready"
          ? `Market not found · ${SITE}`
          : SITE;
    else document.title = `Page not found · ${SITE}`;
  }, [page, marketTitle, index.status]);

  useEffect(preloadRoutes, []);
  useEffect(reloadOnStaleChunk, []);

  // Scroll to top when the page or market changes (not when switching views
  // within a market); coming back home restores where the visitor left off.
  const scrollKey =
    route.page === "market"
      ? `m/${route.name}`
      : route.page === "notfound"
        ? `404${route.path}`
        : route.page;
  const routeKey = route.page === "market" ? `${scrollKey}/${route.view}` : scrollKey;
  const prevKey = useRef(scrollKey);
  const homeY = useRef(0);

  useEffect(() => {
    if (page !== "home") return;
    const onScroll = () => {
      homeY.current = window.scrollY;
    };
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => window.removeEventListener("scroll", onScroll);
  }, [page]);

  useLayoutEffect(() => {
    if (prevKey.current === scrollKey) return;
    prevKey.current = scrollKey;
    window.scrollTo({ top: scrollKey === "home" ? homeY.current : 0, behavior: "instant" });
    // The link that was activated (a card, the breadcrumb) is gone, so focus
    // fell to <body>. Move it to the new page's heading so keyboard and
    // screen-reader users land on, and hear, the new page. Controls that stay
    // mounted (the market switcher) keep their focus.
    const active = document.activeElement;
    if (active && active !== document.body && active.isConnected) return;
    const main = document.getElementById("main");
    if (!main) return;
    const focusOn = (target: HTMLElement) => {
      if (!target.hasAttribute("tabindex")) target.setAttribute("tabindex", "-1");
      target.focus({ preventScroll: true });
    };
    const h1 = main.querySelector<HTMLElement>("h1");
    if (h1) {
      focusOn(h1);
      return;
    }
    // No heading yet (a route chunk or the page's data is still loading):
    // park focus on <main> and move it to the heading once one appears,
    // unless the visitor has moved on to something else meanwhile.
    focusOn(main);
    const mo = new MutationObserver(() => {
      const heading = main.querySelector<HTMLElement>("h1");
      if (!heading) return;
      mo.disconnect();
      const now = document.activeElement;
      if (now === main || now === document.body || !now) focusOn(heading);
    });
    mo.observe(main, { childList: true, subtree: true });
    return () => mo.disconnect();
  }, [scrollKey]);

  const focusMain = () => {
    const main = document.getElementById("main");
    main?.focus();
    main?.scrollIntoView();
  };

  return (
    <>
      <button type="button" className="skip-link" onClick={focusMain}>
        Skip to content
      </button>
      <SiteHeader />
      <main id="main" className="site-main" tabIndex={-1}>
        <div key={page} className="page-enter">
          <ViewBoundary resetKey={routeKey}>
            <Suspense
              fallback={
                <div className="container">
                  <Loading />
                </div>
              }
            >
              {route.page === "home" ? (
                <Home index={index} />
              ) : route.page === "market" ? (
                <MarketPage name={route.name} view={route.view} index={index} />
              ) : (
                <NotFound />
              )}
            </Suspense>
          </ViewBoundary>
        </div>
      </main>
      <SiteFooter />
    </>
  );
}
