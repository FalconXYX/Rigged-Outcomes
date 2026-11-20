"""
The rubric's results in the site's database (watchlist.blobs), next to each market's page and record:

  rubric:<name>   {"market", "condition_id", "version", "scored_at", "scorer", "stats",
                   "answers": {cache key: answer},          the answer cache (cache.py), so a rubric change on the
                                                            backend asks only what it touched
                   "facts": the market's facts document,    so a rubric change needs no Polymarket walk again
                   "page": pipeline.page_part}              what the market's page carries (export.py attaches it)

  publish_page(c, name, part)    the market's page and index entry built again from its record (add_market.build,
                                 with the page part), saved with backend.site.put_market: page, record, index
  page_part(c, name)             the saved page part, or None (backend.site passes it whenever it rebuilds a page,
                                 so a rebuild keeps the scores)
  merge_page(page, result, rubric)  a page from the API with a scored result attached, matched by wallet and
                                 order (the local `pages` command; the backend rebuilds from the record instead)
"""
from backend.rubric import pipeline

PREFIX = "rubric:"


def blob_name(name):
    return f"{PREFIX}{name}"


def read(c, name):
    """(head, doc) of rubric:<name>; (None, None) when the market has none."""
    from watchlist import blobs
    h, doc = blobs.get_json(c, blob_name(name))
    return (h, doc) if isinstance(doc, dict) else (None, None)


def delete(c, name):
    """Remove rubric:<name>; False when there was none (or the Worker does not know rubric:* blobs yet)."""
    from watchlist import blobs, sync
    try:
        return blobs.delete(c, blob_name(name))
    except sync.ApiError as e:
        if e.status == 400:
            return False
        raise


def write(c, name, doc, expect=None):
    from watchlist import blobs
    from watchlist.blobs import ANY
    meta = {"market": name, "version": doc.get("version"), "scored_at": doc.get("scored_at"),
            "scored": len((doc.get("page") or {}).get("whales") or {})}
    return blobs.put_json(c, blob_name(name), doc, meta, expect=ANY if expect is None else expect)


def page_part(c, name):
    """The page part saved for a market, or None (also from a Worker older than the rubric:* blobs, which
    refuses the name: 400)."""
    from watchlist import sync
    try:
        _, doc = read(c, name)
    except sync.ApiError as e:
        if e.status == 400:
            return None
        raise
    part = (doc or {}).get("page")
    return part if isinstance(part, dict) and part.get("whales") else None


def record_of(c, name):
    """(entry, clean CSV text, insiders CSV text, condition id, added_at) of a market on the site, from its
    market:<name> record; None when there is none."""
    from watchlist import blobs
    h, record = blobs.get_json(c, f"market:{name}")
    if h is None or not isinstance(record, dict):
        return None
    entry, data = record.get("entry") or {}, record.get("data") or {}
    clean, insiders = data.get(f"{entry.get('output')}_clean.csv"), data.get(f"{entry.get('output')}_insiders.csv")
    if not (isinstance(clean, str) and isinstance(insiders, str)):
        return None
    meta = h.get("meta") or {}
    return entry, clean, insiders, str(meta.get("condition_id") or "").lower(), meta.get("added_at")


def publish_page(c, name, part, now=None, by="vercel"):
    """Build the market's page again from its record with the page part attached and save it (page, record,
    index entry). Returns {"name", "v"}, or None when the market has no record."""
    from pipeline import add_market
    from backend import site
    got = record_of(c, name)
    if got is None:
        return None
    entry, clean, insiders, cid, added_at = got
    index, page, data = add_market.build(entry, clean, insiders, spectrum=part)
    return site.put_market(c, entry, index, page, data, cid, now=now, by=by, added_at=added_at)


def merge_page(page, result, rubric):
    """A copy of a market page (export.py's shape) with traders.rubric and each scored row's spectrum: rows are
    matched to the result's whales by wallet, in order (a page lists its rows in the insiders file's order, as
    the facts files do). Returns (page, matched rows)."""
    import copy
    page = copy.deepcopy(page)
    traders = (page.get("traders") or {}).get("traders") or []
    by_wallet = {}
    for w in result["whales"]:
        by_wallet.setdefault(str(w["user_id"]).lower(), []).append(w)
    matched = 0
    for t in traders:
        t.pop("spectrum", None)
        queue = by_wallet.get(str(t["id"]).lower())
        if not queue:
            continue
        w = queue.pop(0)
        fields = pipeline.trader_fields(w, rubric)
        if fields is not None:
            t["spectrum"] = fields
            matched += 1
    if traders:
        from backend.rubric import spec
        page["traders"]["rubric"] = spec.display(rubric)
    return page, matched
