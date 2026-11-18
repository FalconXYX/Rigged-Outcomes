"""
The site's deploy step for what the daily job saved in the database
(.github/workflows/deploy.yml, after export.py): the Watchlist's two lists
and the markets added with "Add to main page", written into the data folder
the site is built from, so the site keeps reading static files.

    python3 -m watchlist.publish [--out web/public/data]

  list:closed, list:open   -> <out>/closed.json, <out>/open.json, byte for
                              byte (one not saved yet leaves the file as it is)
  market:<name>            -> <out>/markets/<name>.json, and its entry in
                              <out>/index.json after export.py's, oldest added
                              first. Its page is built again from its saved
                              data files with this export.py, so it follows
                              the committed markets' format (the page saved
                              when it was added stands in if that fails). A
                              name or slug export.py already built is left
                              out: committed markets win.

Where the database can't give something, the live site's copy (what the last
deploy built: WATCHLIST_SITE_URL, default LIVE_SITE) stands in, with a
::warning::: everything when the API is down (a network error, a 429 or a 5xx
after the client's retries), and a list or a market whose saved document is
damaged (a market the live site lacks is left out). With no API configured
(neither WATCHLIST_API_URL nor WATCHLIST_JOB_TOKEN) the site is built without
the lists and the added markets (exit 0). When the live site is needed and
does not answer either, nothing is written and skip=true goes to
$GITHUB_OUTPUT (exit 0): the deploy is left out, so Pages keeps the last site
(with its lists) and the next deploy publishes. Only a setup or code problem
exits 1 and fails the deploy: one of the two settings set without the other,
the API refusing (a wrong token), or no index.json to add to.
"""
import argparse
import json
import os
import re
import sys
import tempfile
import time

import requests

from watchlist import blobs, sync
from watchlist.common import SITE_DATA_DIR, log

LIVE_SITE = "https://parthjain.ca/Rigged-Outcomes/"
NAME = re.compile(r"^[a-z0-9][a-z0-9-]{1,31}$")
DATA_FILE = re.compile(r"^[A-Za-z0-9]{1,64}_(clean|insiders)\.csv$")   # an added market's data files
LISTS = ("closed", "open")
FETCH_TIMEOUT = 30
FETCH_WAITS = (5, 20)             # seconds before each retry of a live-site read that failed


class PublishError(Exception):
    pass


class LiveSiteDown(PublishError):
    """The live site did not answer (or served something that is not the site's data): an outage."""


def _bare(slug):
    import add_market
    return add_market.bare_slug(slug)


def _write(path, data):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path + ".tmp", "wb") as f:
        f.write(data)
    os.replace(path + ".tmp", path)


def _dumps(doc):
    """JSON the way export.py writes index.json and a market's page."""
    return json.dumps(doc, separators=(",", ":"), allow_nan=False).encode("utf-8")


class LiveSite:
    """The published site's data files, what the last deploy built: get("index.json") -> bytes, or None when the
    site has no such file. Raises PublishError when the site does not answer."""

    def __init__(self, base=None, fetch=None, sleep=time.sleep):
        self.base = (base or os.environ.get("WATCHLIST_SITE_URL") or LIVE_SITE).rstrip("/") + "/data/"
        self._fetch = fetch or self._http
        self.sleep = sleep
        self._index = None

    def _http(self, url):
        why = None
        for attempt in range(len(FETCH_WAITS) + 1):
            if attempt:
                self.sleep(FETCH_WAITS[attempt - 1])
            try:
                r = requests.get(url, timeout=FETCH_TIMEOUT, headers={"User-Agent": "rigged-outcomes-deploy"})
            except requests.RequestException as e:
                why = type(e).__name__
                continue
            if r.status_code == 404:
                return None
            if r.status_code == 200:
                return r.content
            why = f"HTTP {r.status_code}"
        raise LiveSiteDown(f"the live site did not answer for {url} ({why})")

    def get(self, rel):
        return self._fetch(self.base + rel)

    def index(self):
        """The live index.json's markets."""
        if self._index is None:
            data = self.get("index.json")
            try:
                self._index = json.loads(data)["markets"] if data is not None else []
            except (ValueError, KeyError, TypeError) as e:
                raise LiveSiteDown(f"the live site's index.json is not the site's index ({type(e).__name__})")
        return self._index

    def market(self, name):
        """(index entry, page bytes) of the live site's market `name`, or None when it has none."""
        entry = next((m for m in self.index() if isinstance(m, dict) and m.get("name") == name), None)
        page = self.get(f"markets/{name}.json") if entry is not None else None
        if page is None:
            return None
        try:
            ok = json.loads(page).get("name") == name
        except (ValueError, AttributeError):
            ok = False
        return (entry, page) if ok else None


def _local_index(out):
    try:
        with open(os.path.join(out, "index.json"), encoding="utf-8") as f:
            return json.load(f)["markets"]
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise PublishError(f"no index.json to add the markets to in {out} (run export.py first): {e}")


def rebuild(entry, data):
    """(index entry, page) of an added market built again from its data files ({"<Output>_clean.csv": text, ...})
    with this export.py, in a scratch folder. Raises on anything export.py can't build."""
    import export
    out = str(entry.get("output") or "")
    with tempfile.TemporaryDirectory(prefix="publish-") as tmp:
        for fname, text in data.items():
            if not (DATA_FILE.match(str(fname)) and str(fname).startswith(f"{out}_") and isinstance(text, str)):
                raise ValueError(f"{fname!r} is not one of {out}'s data files")
            with open(os.path.join(tmp, fname), "w", encoding="utf-8") as f:
                f.write(text)
        index, page = export.export_one(entry, data_dir=tmp)
    if not (index.get("name") == entry.get("name") == page.get("name")):
        raise ValueError("export.py named it differently")
    return index, page


def _saved_page(doc, name):
    """(index entry, page bytes) of a saved market document, built again from its data files when it has them;
    None when it is not this market's page."""
    entry, index, page = (doc.get(k) for k in ("entry", "index", "market")) if isinstance(doc, dict) else (None,) * 3
    if not (isinstance(index, dict) and isinstance(page, dict) and index.get("name") == name == page.get("name")):
        return None
    data = doc.get("data")
    if isinstance(entry, dict) and entry.get("name") == name and isinstance(data, dict) and data:
        try:
            built, rebuilt = rebuild(entry, data)
            return built, _dumps(rebuilt)
        except Exception as e:                               # the page saved when it was added stands in
            sync._warn(f"market:{name} could not be built again ({type(e).__name__}: {str(e)[:160]}); the page saved "
                       f"when it was added is used")
    return index, _dumps(page)


def _from_live(live, name, why):
    got = live.market(name) if live is not None else None
    if got is None:
        sync._warn(f"market:{name} {why}, and the live site has no copy of it: left out")
    else:
        sync._warn(f"market:{name} {why}: the live site's copy is used")
    return got


def _finish(out, index, files, lists, added, skipped):
    files[os.path.join(out, "index.json")] = _dumps({"markets": index})
    for path, data in files.items():
        _write(path, data)
    return {"lists": lists, "markets": added, "skipped": skipped}


def _list(c, name, live):
    """A list's bytes: the saved one, else (damaged) the live site's; None when there is neither."""
    try:
        h, data = blobs.get(c, f"list:{name}")
    except blobs.BlobError as e:
        data = live.get(f"{name}.json") if live is not None else None
        sync._warn(f"the saved {name} list is damaged ({e}): " + ("the live site's copy is used" if data is not None
                                                                  else f"{name}.json is left as it is"))
        return data
    if h is None:
        print(f"::notice::No {name} list saved yet: {name}.json is left as it is", flush=True)
    return data


def publish(c, out=SITE_DATA_DIR, live=None):
    """Write the lists and the added markets into out from the database; returns {"lists": [...], "markets":
    [...], "skipped": [...]}. live (a LiveSite): its copy stands in for a damaged document (none: nothing does).
    Raises PublishError (no index.json, the live site not answering when it is needed) or sync.ApiError, and
    writes nothing then."""
    index = _local_index(out)
    files, lists, added, skipped = {}, [], [], []
    for name in LISTS:
        data = _list(c, name, live)
        if data is not None:
            files[os.path.join(out, f"{name}.json")] = data
            lists.append(name)
    names = {m.get("name") for m in index}
    slugs = {_bare(m.get("slug")) for m in index}
    heads = sorted(blobs.heads(c, "market:"), key=lambda h: (int((h.get("meta") or {}).get("added_at") or 0),
                                                              h["name"]))
    for h in heads:
        name = h["name"].split(":", 1)[1]
        slug = ((h.get("meta") or {}).get("entry") or {}).get("slug")
        if name in names or (slug and _bare(slug) in slugs):
            sync._warn(f"{h['name']}: a committed market has its name or slug; the committed one is kept")
            skipped.append(name)
            continue
        try:
            got, doc = blobs.get_json(c, h["name"], head=h)
        except blobs.BlobError as e:
            page = _from_live(live, name, f"is damaged ({e})")
        else:
            if got is None:                                   # taken off while this ran
                continue
            page = _saved_page(doc, name) or _from_live(live, name, "is not a market's page (python3 -m "
                                                                     f"watchlist.blobs rm {h['name']})")
        if page is None or not NAME.match(name):
            skipped.append(name)
            continue
        entry, data = page
        if _bare(entry.get("slug")) in slugs:
            sync._warn(f"{h['name']}: a committed market has its name or slug; the committed one is kept")
            skipped.append(name)
            continue
        names.add(name)
        slugs.add(_bare(entry.get("slug")))
        files[os.path.join(out, "markets", f"{name}.json")] = data
        index.append(entry)
        added.append(name)
    return _finish(out, index, files, lists, added, skipped)


def publish_live(out=SITE_DATA_DIR, live=None):
    """publish's stand-in while the API is down: the lists and the added markets as the live site has them (its
    markets that export.py did not build here; one taken out of markets.json since the last deploy comes back
    until the API answers again). Raises PublishError; writes nothing then."""
    live = live or LiveSite()
    index = _local_index(out)
    files, lists, added, skipped = {}, [], [], []
    for name in LISTS:
        data = live.get(f"{name}.json")
        if data is None:
            print(f"::notice::The live site has no {name} list: {name}.json is left as it is", flush=True)
            continue
        files[os.path.join(out, f"{name}.json")] = data
        lists.append(name)
    names = {m.get("name") for m in index}
    slugs = {_bare(m.get("slug")) for m in index}
    for entry in live.index():
        name = entry.get("name") if isinstance(entry, dict) else None
        if not isinstance(name, str) or not NAME.match(name) or name in names or _bare(entry.get("slug")) in slugs:
            continue
        got = live.market(name)
        if got is None:
            sync._warn(f"the live site lists {name} without its page: left out")
            skipped.append(name)
            continue
        names.add(name)
        slugs.add(_bare(entry.get("slug")))
        files[os.path.join(out, "markets", f"{name}.json")] = got[1]
        index.append(entry)
        added.append(name)
    return _finish(out, index, files, lists, added, skipped)


def _output(**kv):
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a") as f:
            for k, v in kv.items():
                f.write(f"{k}={v}\n")


def main(argv=None, live=None):
    ap = argparse.ArgumentParser(description="Add the Watchlist's lists and the added markets to the site's data.")
    ap.add_argument("--out", default=SITE_DATA_DIR, help="the site's data folder, export.py's output")
    args = ap.parse_args(argv)
    c = sync.client()
    if c is None:
        half = [k for k in ("WATCHLIST_API_URL", "WATCHLIST_JOB_TOKEN") if sync._env(k)]
        if half:
            print(f"::error::Only {half[0]} is set: the site would be built without the Watchlist lists and the added "
                  f"markets. Set both (the variable API_URL and the secret WATCHLIST_JOB_TOKEN) or neither",
                  flush=True)
            return 1
        print("::notice::No API configured: the site is built without the Watchlist lists and added markets",
              flush=True)
        return 0
    live = live or LiveSite()
    try:
        try:
            got = publish(c, args.out, live)
        except sync.ApiError as e:
            if not e.outage:                     # a refused token (401, or 429 locked), or a request it cannot read
                print(f"::error::The API refused the deploy's reads ({e}): check the WATCHLIST_JOB_TOKEN secret",
                      flush=True)
                return 1
            print(f"::warning::The API did not answer ({e}): the Watchlist lists and the added markets are the live "
                  f"site's ({live.base})", flush=True)
            got = publish_live(args.out, live)
    except LiveSiteDown as e:
        print(f"::warning::Neither the API nor the live site could give the Watchlist's data ({e}): this deploy is "
              f"left out and Pages keeps the last site; the next deploy publishes", flush=True)
        _output(skip="true")
        return 0
    except (PublishError, blobs.BlobError) as e:
        print(f"::error::The Watchlist's data could not be added to the site: {e}", flush=True)
        return 1
    log(f"lists: {', '.join(got['lists']) or 'none'}; added markets: {', '.join(got['markets']) or 'none'}"
        + (f"; left out: {', '.join(got['skipped'])}" if got["skipped"] else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
