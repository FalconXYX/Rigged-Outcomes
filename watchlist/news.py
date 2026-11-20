"""
Headlines from Google News's public RSS search: free, no key. The closed
list's model has no web search (this Gemini key gets 0 search requests and
the older models that had free search are gone), so it reads the headlines
found here instead: what happened, when it was first reported, and whether
it was called a surprise.

    search(query, after=None, before=None, limit=LIMIT) -> [headline]

A headline is {"title", "source", "published" (ISO UTC), "url"}, in Google's
relevance order. after / before (epoch seconds or ISO) become Google's own
after:YYYY-MM-DD / before:YYYY-MM-DD operators, on Toronto days. Requests
are spaced MIN_INTERVAL apart by the job's Spacer (SPACING, set per job: no
state is kept at module level); errors raise NewsError (transient for a
network error, a 429 or a 5xx: the caller may ask again), and so does an
answer that is not a news feed at all (a consent or captcha page, an empty
body: what a refusing Google sends with status 200), so it is never read as
"no news". Inside a step the request's timeout is clamped to the step's
deadline, and one that cannot end in time raises OutOfTime instead.
"""
import contextvars
import html
import re
import threading
import time
from datetime import timezone
from email.utils import parsedate_to_datetime

import requests

from watchlist.common import clamp_timeout, local_day, to_epoch

URL = "https://news.google.com/rss/search"
LIMIT = 15
MIN_INTERVAL = 1.0      # seconds between requests: a public feed, used gently
TIMEOUT = 30

_ITEM = re.compile(r"<item>(.*?)</item>", re.S)


class Spacer:
    """MIN_INTERVAL between one job's requests, across its threads."""

    def __init__(self, interval=MIN_INTERVAL):
        self.interval, self._last, self._lock = interval, 0.0, threading.Lock()

    def wait(self):
        with self._lock:
            wait = self._last + self.interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()


SPACING = contextvars.ContextVar("news_spacing", default=None)    # the job's Spacer (None: requests unspaced)


class NewsError(Exception):
    """A failed search. transient: a network error, a 429 or a 5xx, which a moment later may not repeat (a
    refusal page or another status is not)."""

    def __init__(self, message, transient=False):
        super().__init__(message)
        self.transient = transient


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
    return local_day(to_epoch(ts))


def build_query(query, after=None, before=None):
    q = " ".join(str(query or "").split())
    if after is not None:
        q += f" after:{_day(after)}"
    if before is not None:
        q += f" before:{_day(before)}"
    return q


def search(query, after=None, before=None, limit=LIMIT, session=requests):
    q = build_query(query, after, before)
    timeout = clamp_timeout(TIMEOUT, "a news search")
    spacer = SPACING.get()
    if spacer is not None:
        spacer.wait()
    try:
        resp = session.get(URL, params={"q": q, "hl": "en-US", "gl": "US", "ceid": "US:en"}, timeout=timeout)
    except requests.RequestException as e:
        raise NewsError(f"news search failed: {type(e).__name__}", transient=True) from e
    if resp.status_code != 200:
        raise NewsError(f"news search: HTTP {resp.status_code}",
                        transient=resp.status_code == 429 or resp.status_code >= 500)
    head = (resp.text or "")[:2048].lower()
    if "<rss" not in head and "<channel" not in head:
        raise NewsError("news search: the answer was not a news feed")
    return parse(resp.text)[:limit]
