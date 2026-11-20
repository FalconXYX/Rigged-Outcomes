"""
The text a scorer reads for one wallet, rendered from the rubric's [input]
template and the wallet's facts. Both scorers read the same text, and its
sha1 is the input half of the cache key (cache.py): a wallet whose facts or
template change is asked again, any other is not.

    render_input(rubric, market, facts, side="BUY") -> str
        market: a facts file's "market" part; facts: one whale's {factor id: value}; side: its position's
        side, worded by [input] sides as {side}. ISO times in the market part read "2026-02-28 06:45 UTC".
    input_hash(text) -> sha1 hex
    fmt(value, format) -> "$12,400", "8%", "0.08", "9.5 hours", ...
"""
import hashlib
import re
from datetime import datetime, timezone

from backend.rubric.spec import factor_format

_ISO = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d")


def when(v):
    """An ISO time as the model reads it: "2026-02-28 06:45 UTC"; anything else unchanged."""
    if isinstance(v, str) and _ISO.match(v):
        try:
            t = datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            return v
        if t.tzinfo is not None:
            t = t.astimezone(timezone.utc)
        return t.strftime("%Y-%m-%d %H:%M UTC")
    return v


def fmt(v, form):
    """A fact value as the model reads it. None is handled by the caller (the rubric's unknown_value)."""
    if isinstance(v, bool) or form == "flag":
        return "yes" if v else "no"
    v = float(v)
    if form == "usd":
        return f"${v:,.0f}"
    if form == "percent":
        return f"{v:.2f}%" if abs(v) < 1 else f"{v:.1f}%"
    if form == "probability":
        return f"{v:.3f} ({100 * v:.1f}% implied chance)"
    if form == "hours":
        return f"{v:,.1f} hours" if abs(v) < 72 else f"{v:,.0f} hours ({v / 24:,.1f} days)"
    if form == "days":
        return f"{v:,.1f} days"
    if form == "x":
        return f"{v:,.2f}x"
    return f"{v:,.0f}"


def short(v, form):
    """A fact value for a table cell: "$12.4k", "8%", "0.15", "482h", "20.1d", "1,078"."""
    if v is None:
        return "-"
    if isinstance(v, bool) or form == "flag":
        return "yes" if v else "no"
    v = float(v)
    if form == "usd":
        return f"${v / 1e6:.1f}M" if abs(v) >= 1e6 else f"${v / 1e3:.1f}k" if abs(v) >= 1e3 else f"${v:.0f}"
    if form == "percent":
        return f"{v:.2g}%" if abs(v) < 10 else f"{v:.0f}%"
    if form == "probability":
        return f"{v:.2f}"
    if form == "hours":
        return f"{v:.1f}h" if abs(v) < 72 else f"{v / 24:.0f}d"
    if form == "days":
        return f"{v:.1f}d"
    if form == "x":
        return f"{v:.1f}x"
    return f"{v:,.0f}"


def render_input(rubric, market, facts, reg=None, side="BUY"):
    """The rubric's template filled with the market's fields and one fact line per `send` factor, in the
    rubric's order."""
    inp = rubric["input"]
    unknown = inp.get("unknown_value", "not known")
    lines = []
    for f in rubric["factors"]:
        if not f.get("send", True):
            continue
        v = facts.get(f["id"])
        value = unknown if v is None else fmt(v, factor_format(f, reg))
        lines.append(inp["fact_line"].format(label=f["label"], value=value, description=f["description"]))
    fields = {k: ("" if v is None else when(v)) for k, v in market.items()}
    for k in ("volume_usd", "whale_threshold_usd"):
        if isinstance(fields.get(k), (int, float)):
            fields[k] = fmt(fields[k], "usd")
    fields["facts"] = "\n".join(lines)
    sides = inp.get("sides") or {}
    fields["side"] = sides.get(str(side or "BUY").upper(), str(side or "BUY").lower())
    return inp["template"].strip().format_map(_Missing(fields))


class _Missing(dict):
    def __missing__(self, key):
        return ""


def input_hash(text):
    return hashlib.sha1(text.encode("utf-8")).hexdigest()
