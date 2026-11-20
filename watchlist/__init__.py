"""
watchlist — the two lists on the site's Watchlist page (#/watchlist).

The site's lists, published to the site's data (site:closed.json and
site:open.json in the database, served by the Worker at /api/data/):

  upsets   the Recently closed list (closed.json): each Toronto day's
           closed markets worth a look as possible insider cases, 3 a day,
           from word rules (kinds), AI labels (ai) and a check against the
           news (judge).
  judge    that check: a news query per market, Google News headlines
           (news) and a model's verdict. No prices, trades or wallets.
  opennow  the Open now list (open.json): open markets where a few people
           will know the answer first and the news shows them moving
           toward it.

The daily run that makes them (docs/BACKEND.md): queued jobs in the
Worker's D1 database, run in steps on Vercel from 04:00 Toronto time:

  steps    one handler per job kind: the work, the watched markets, the
           closed list's scan, labels, queries, news, checks and publish,
           the open list's walk, names, news, readings and publish, and the
           report. Each step works to its own
           deadline and resumes where it stopped.
  state    the job's memory between runs: the store tables the lists read
           and write, saved in the site's database. Nothing of the job's is
           kept in git.
  blobs    how the job saves a document there: gzip-compressed, in parts,
           through the API's job endpoints (worker/src/blobs.ts).
  sync     the job's side of the site's API (worker/): the markets to add,
           the watched markets that closed (sent to the main page), the
           catalog, and the run's report.
  publish  a list published to the site's data (site:<name>.json).

Run modules from the repo root as `python3 -m watchlist.<module>`. Other
generated data lives only under data/watchlist/ (gitignored): the sqlite
store (data/watchlist/watchlist.db), the news cache (data/watchlist/cache/)
and logs.

Layers, bottom up (each imports only the ones above it):

  common   paths, time parsing, logging. No network.
  filters  pure functions on Gamma objects: exclusions (games, prices,
           weather), resolution, event grouping, main-market choice, the
           open pool, series keys. No network.
  store    sqlite: AI labels, the closed list's checks, the open list's
           pool, filings, readings, days, cards and reviews, meta.
  net      every Polymarket call the watchlist makes, behind one token
           bucket per endpoint family (<= 40 % of the documented limits):
           the open-event walk and the closed-market window.
  kinds    the one vocabulary of the closed and the open list: embargoed
           releases, the kinds and their three groups, the dated place, the
           card sentences, step / leak / odds words, source ranks. Pure; it
           imports nothing from the package, so any module can use it.

The legacy pipeline modules (polymarket_api, and add_market for a market
added to the main page) are reused, never copied.

Public API (what the lists build on):

  common.to_epoch(v) / iso(epoch) / utc_day(epoch) / day_str(day)
  common.local_day(epoch) / local_bounds(day) / local_iso(epoch)   Toronto days and stamps
  common.Budget(seconds)               .expired(reserve) / .left()
  filters.exclusion(market, event=None) -> (category, detail) | None
  filters.winning_outcome(market)      -> (index, name) | None
  filters.group_by_event(markets)      -> [{event_id, event, markets}]
  filters.choose_main_market(markets)  -> (main, others, rule)
  filters.pool_reason(m, event, now)   -> None | reason;  open_pool(events, now) -> (groups, counts)
  filters.series_key(event) / title_key(title)
  net.walk_open_events(min_volume=100_000)          -> [event]
  net.closed_markets(since, until=None, min_volume=100_000) -> [market] (closedTime in window)
  net.fetch_event(id) / markets_by_condition(ids) / get_json(url, params) / install()
  store.connect(path) / default_conn() and the get_/save_/set_ helpers per table.
"""

from watchlist import common as _common  # noqa: F401,E402  (puts the repo root on sys.path)
