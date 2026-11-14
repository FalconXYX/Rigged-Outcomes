"""
Headlines from Google News's public RSS search: free, no key. The closed
list's model has no web search (this Gemini key gets 0 search requests and
the older models that had free search are gone), so it reads the headlines
found here instead: what happened, when it was first reported, and whether
it was called a surprise.

    search(query, after=None, before=None, limit=LIMIT) -> [headline]

A headline is {"title", "source", "published" (ISO UTC), "url"}, in Google's
relevance order. after / before (epoch seconds or ISO) become Google's own
after:YYYY-MM-DD / before:YYYY-MM-DD operators. Requests are spaced
MIN_INTERVAL apart across threads; errors raise NewsError.
"""
import html
import re
import threading
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import requests

from watchlist.common import to_epoch

URL = "https://news.google.com/rss/search"
LIMIT = 15
MIN_INTERVAL = 1.0      # seconds between requests: a public feed, used gently
TIMEOUT = 30

_ITEM = re.compile(r"<item>(.*?)</item>", re.S)
_lock = threading.Lock()
_last = [0.0]


class NewsError(Exception):
    pass


def _tag(block, name):
    m = re.search(rf"<{name}\b[^>]*>(.*?)</{name}>", block, re.S)
    if not m:
        return ""
    text = m.group(1).strip()
    if text.startswith("<![CDATA[") and text.endswith("]]>"):
        text = text[9:-3]
    return html.unescape(text).strip()


def parse(xml):
    """[headline] from a Google News RSS document. The title's trailing
    " - Source" is cut when it repeats the item's source."""
    out = []
    for block in _ITEM.findall(xml or ""):
        title, source = _tag(block, "title"), _tag(block, "source")
        if source and title.endswith(f" - {source}"):
            title = title[: -len(source) - 3].rstrip()
        try:
            published = parsedate_to_datetime(_tag(block, "pubDate")).astimezone(timezone.utc)
            published = published.isoformat().replace("+00:00", "Z")
        except (TypeError, ValueError):
            published = None
        if title:
            out.append({"title": title, "source": source, "published": published, "url": _tag(block, "link")})
    return out


def _day(ts):
    return datetime.fromtimestamp(to_epoch(ts), timezone.utc).strftime("%Y-%m-%d")


def build_query(query, after=None, before=None):
    q = " ".join(str(query or "").split())
    if after is not None:
        q += f" after:{_day(after)}"
    if before is not None:
        q += f" before:{_day(before)}"
    return q


def search(query, after=None, before=None, limit=LIMIT, session=requests):
    q = build_query(query, after, before)
    with _lock:
        wait = _last[0] + MIN_INTERVAL - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last[0] = time.monotonic()
    try:
        resp = session.get(URL, params={"q": q, "hl": "en-US", "gl": "US", "ceid": "US:en"}, timeout=TIMEOUT)
    except requests.RequestException as e:
        raise NewsError(f"news search failed: {type(e).__name__}") from e
    if resp.status_code != 200:
        raise NewsError(f"news search: HTTP {resp.status_code}")
    return parse(resp.text)[:limit]
