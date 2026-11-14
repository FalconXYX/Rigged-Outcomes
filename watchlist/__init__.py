"""
watchlist — the two lists on the site's Watchlist page (#/watchlist), and
the earlier statistical scans they replaced.

The site's lists, each written to web/public/data/ (gitignored):

  upsets   the Recently closed list (closed.json): each UTC day's closed
           markets worth a look as possible insider cases, from word rules
           (kinds), AI labels (ai) and a check against the news (judge).
  judge    that check: a news query per market, Google News headlines
           (news) and a model's verdict. No prices, trades or wallets.
  opennow  the Open now list (open.json): open markets where a few people
           will know the answer first and the news shows them moving
           toward it.
  deep     the trade-data look at one closed market, kept for later; not
           used by either list yet.

The earlier statistical scans (closed_scan, open_scan, track_record,
backtest, parity; built on method) are not used by the site. Their demo
JSON goes to data/watchlist/demo/.

Run modules from the repo root as `python3 -m watchlist.<module>`. Other
generated data lives only under data/watchlist/ (gitignored): the sqlite
store (data/watchlist/watchlist.db), caches and raw/clean trade CSVs
(data/watchlist/cache/) and logs.

Layers, bottom up (each imports only the ones above it):

  common   paths, time parsing, logging. No network.
  filters  pure functions on Gamma objects: exclusions (games, prices,
           weather), resolution, event grouping, main-market choice, the
           open pool, series keys. No network.
  store    sqlite: wallet look-up cache, scanned markets/events, method
           results, whale rows, AI labels, meta (cursors, run notes).
  net      every Polymarket call the watchlist makes, behind one token
           bucket per endpoint family (<= 40 % of the documented limits):
           open-event walk, closed-market window, recent trades,
           user_volume (cached in the store).
  method   THE USER'S METHOD (scrape_insiders) on one resolved market, with
           prior history from one /v2/user-volume call per whale.
  kinds    the one vocabulary of the closed and the open list: embargoed
           releases, the kinds and their three groups, the dated place, the
           card sentences, step / leak / odds words, source ranks. Pure; it
           imports nothing from the package, so any module can use it.

The legacy pipeline modules (polymarket_api, polymarket_trades, cleandata,
scrape_insiders) are reused, never copied.

Public API (what the scan/rank/AI scripts build on):

  common.to_epoch(v) / iso(epoch) / utc_day(epoch) / day_str(day)
  common.Budget(seconds)               .expired(reserve) / .left()
  filters.exclusion(market, event=None) -> (category, detail) | None
  filters.winning_outcome(market)      -> (index, name) | None
  filters.group_by_event(markets)      -> [{event_id, event, markets}]
  filters.choose_main_market(markets)  -> (main, others, rule)
  filters.pool_reason(m, event, now)   -> None | reason;  open_pool(events, now) -> (groups, counts)
  filters.series_key(event) / title_key(title)
  net.walk_open_events(min_volume=100_000)          -> [event]
  net.closed_markets(since, until=None, min_volume=100_000) -> [market] (closedTime in window)
  net.recent_trades(condition_id, since, until=None) -> (v2 rows, capped)
  net.user_volume(wallet, end_epoch, conn=None)     -> {volume, volume_usdc, trade_count, day, cached}
  net.user_volumes([(wallet, end)], conn=None)      -> [same], in order
  net.exact_prior(wallet, first_ts, conn=None)      -> prior at the exact cutoff (user-volume + first-day fills)
  net.fetch_event(id) / markets_by_condition(ids) / get_json(url, params) / install()
  method.analyze_market(market, cutoff_variant="exact"|"same_day"|"day_before", ...) -> result dict
      (VARIANTS; DEFAULT_VARIANT "exact"; CUTOFF_VARIANTS = the two one-call variants)
  method.analyze_slug(slug, ...) / brief(result) / flag_whale / price_jump / RESULT_FIELDS
  store.connect(path) / default_conn() and the get_/save_/set_ helpers per table;
  store.closed_results(conn, variant, since_ts, until_ts) for the track record.
"""

from watchlist import common as _common  # noqa: F401,E402  (puts the repo root on sys.path)
