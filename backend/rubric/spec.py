"""
Load and check the rubric file, and cut out the part the site renders.

    rubric = load_rubric()                  # rubric/wallet_rubric.toml, as a plain dict
    problems = check(rubric)                # [] when the file is usable
    page_part = display(rubric)             # goes into traders.rubric of each market page

The rubric is a dict straight from tomllib (no classes), so a new key in
the file is available to every module without a code change. The shape is
documented in the file itself; check() is the one place that enforces it,
including keys it does not know (a typo such as `lable` would otherwise be
ignored without a word).

Hashes (cache keys, see cache.py):
    question_hash(q, scorer_cfg)  sha1 of the question as sent plus the model
    A question whose wording, levels or options change gets a new hash, so
    only that question is asked again; weights and bands never cost a call.
"""
import hashlib
import json
import os
import re
import tomllib

from backend.rubric import DEFAULT_PATH

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TOKENS_CSS = os.path.join(ROOT, "web", "src", "styles", "tokens.css")

QUESTION_TYPES = ("score", "noul", "choice")
FORMATS = ("usd", "percent", "probability", "count", "hours", "days", "x", "flag")   # wallet_facts units
SCORERS = ("decisions", "interactions")
# the "market" part of a facts file (wallet_facts.market_facts), plus {facts} and {side}
PLACEHOLDERS = {"name", "title", "slug", "outcome", "condition_id", "volume_usd", "positions", "whales",
                "whale_threshold_usd", "first_trade", "close", "reveal", "reveal_method", "pre_reveal_prob", "news",
                "news_label", "facts", "side"}
LINE_PLACEHOLDERS = {"label", "value", "description"}
ID = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
OPTION_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
# every key the file may hold, by section (check() names any other)
KNOWN = {
    "": {"version", "name", "scale", "scorer", "input", "factors", "questions", "combine", "bands", "backend"},
    "scorer": {"use", "fallback", "decisions", "interactions"},
    "scorer.decisions": {"model", "wallets_per_request", "min_interval", "rpm", "tpm", "daily_cap", "retries"},
    "scorer.interactions": {"models", "wallets_per_request", "temperature", "daily_cap"},
    "input": {"template", "fact_line", "unknown_value", "sides"},
    "factors": {"id", "label", "description", "format", "send", "card"},
    "questions": {"id", "type", "label", "uses", "instructions", "levels", "options", "true_description",
                  "false_description"},
    "levels": {"label", "criterion"},
    "options": {"id", "label", "criterion", "value"},
    "combine": {"method", "weights"},
    "bands": {"id", "label", "min", "color", "flagged"},
    "backend": {"score_on_add"},
}


def _unknown(section, d, where=None):
    if not isinstance(d, dict):
        return [f"{where or section or 'the file'} must be a table"]
    return [f"{where or (f'[{section}]' if section else 'the file')}: unknown key {k!r}"
            for k in d if k not in KNOWN[section]]


def load_rubric(path=None):
    """The rubric file as a dict (tomllib). path defaults to the repo's rubric/wallet_rubric.toml."""
    with open(path or os.path.join(ROOT, DEFAULT_PATH), "rb") as f:
        return tomllib.load(f)


def site_tokens(css_path=TOKENS_CSS):
    """Every --c-* colour token tokens.css defines (bands may use only these)."""
    try:
        with open(css_path, encoding="utf-8") as f:
            return set(re.findall(r"(--c-[a-z0-9-]+)\s*:", f.read()))
    except OSError:
        return set()


def _fields(text):
    return set(re.findall(r"{([a-z_]+)}", text or ""))


def registry():
    """{factor id: Factor(name, group, unit, doc, fn)}: every factor the code computes (wallet_facts.FACTORS)."""
    from backend.rubric.wallet_facts import FACTORS
    return FACTORS


def factor_format(f, reg=None):
    """A rubric factor's display format: its own `format`, else the registered factor's unit."""
    if f.get("format"):
        return f["format"]
    reg = registry() if reg is None else reg
    got = reg.get(f.get("id"))
    return getattr(got, "unit", None) if got else None


def check(rubric, reg=None, tokens=None):
    """Every problem with the rubric, as short sentences; [] when it is usable.
    reg: the factor registry (default: wallet_facts.FACTORS); tokens: colour
    tokens (default: tokens.css; none found: colours are not checked)."""
    reg = registry() if reg is None else reg
    tokens = site_tokens() if tokens is None else tokens
    out = _unknown("", rubric)
    if not str(rubric.get("version") or "").strip():
        out.append("version is missing")
    scale = rubric.get("scale") or [0, 100]
    if not (isinstance(scale, list) and len(scale) == 2 and all(isinstance(x, (int, float)) for x in scale)
            and scale[0] < scale[1]):
        out.append("scale must be two numbers, low then high")
        scale = [0, 100]
    lo, hi = scale

    sc = rubric.get("scorer") or {}
    out += _unknown("scorer", sc)
    if sc.get("use") not in SCORERS:
        out.append(f"scorer.use must be one of {SCORERS}")
    if sc.get("fallback") not in (None, "", *SCORERS):
        out.append(f"scorer.fallback must be one of {SCORERS} or empty")
    for name in SCORERS:
        cfg = sc.get(name)
        if cfg is None:
            if name in (sc.get("use"), sc.get("fallback")):
                out.append(f"[scorer.{name}] is missing")
            continue
        out += _unknown(f"scorer.{name}", cfg, f"[scorer.{name}]")
        for k in ("wallets_per_request", "daily_cap", "rpm", "tpm", "retries"):
            if k in cfg and not (isinstance(cfg[k], int) and cfg[k] > 0):
                out.append(f"scorer.{name}.{k} must be a whole number above 0")
        for k in ("min_interval", "temperature"):
            if k in cfg and not (isinstance(cfg[k], (int, float)) and cfg[k] >= 0):
                out.append(f"scorer.{name}.{k} must be a number >= 0")
    if (sc.get("decisions") or {}).get("wallets_per_request", 1) != 1:
        out.append("scorer.decisions.wallets_per_request must be 1 (the API reads one input per request)")
    models = (sc.get("interactions") or {}).get("models")
    if models is not None and not (isinstance(models, list) and models and all(isinstance(m, str) for m in models)):
        out.append("scorer.interactions.models must be a list of model names")

    inp = rubric.get("input") or {}
    out += _unknown("input", inp, "[input]")
    for bad in sorted(_fields(inp.get("template")) - PLACEHOLDERS):
        out.append(f"input.template: unknown placeholder {{{bad}}}")
    if "facts" not in _fields(inp.get("template")):
        out.append("input.template must contain {facts}")
    if not str(inp.get("fact_line") or "").strip():
        out.append("input.fact_line is missing")
    for bad in sorted(_fields(inp.get("fact_line")) - LINE_PLACEHOLDERS):
        out.append(f"input.fact_line: unknown placeholder {{{bad}}}")
    sides = inp.get("sides")
    if sides is not None and not (isinstance(sides, dict) and all(isinstance(v, str) for v in sides.values())):
        out.append("input.sides must map BUY and SELL to text")
    elif "side" in _fields(inp.get("template")) and not sides:
        out.append("input.template uses {side}: input.sides must say what BUY and SELL read as")

    factor_ids = set()
    for f in rubric.get("factors") or []:
        fid = f.get("id", "?") if isinstance(f, dict) else "?"
        out += _unknown("factors", f, f"factor {fid}")
        if not isinstance(f, dict):
            continue
        if not ID.match(str(fid)):
            out.append(f"factor {fid!r}: id must be lower_snake_case")
        if fid in factor_ids:
            out.append(f"factor {fid}: id used twice")
        factor_ids.add(fid)
        if fid not in reg:
            out.append(f"factor {fid}: no factor of that name in backend/rubric/wallet_facts.py")
        if factor_format(f, reg) not in FORMATS:
            out.append(f"factor {fid}: format must be one of {FORMATS}")
        for k in ("label", "description"):
            if not str(f.get(k) or "").strip():
                out.append(f"factor {fid}: {k} is missing")
        for k in ("send", "card"):
            if k in f and not isinstance(f[k], bool):
                out.append(f"factor {fid}: {k} must be true or false")
    if not factor_ids:
        out.append("no factors")
    if factor_ids and not any(f.get("send", True) for f in rubric.get("factors") or [] if isinstance(f, dict)):
        out.append("no factor has send = true: the model would read nothing about the wallet")

    qids = set()
    for q in rubric.get("questions") or []:
        qid = q.get("id", "?") if isinstance(q, dict) else "?"
        out += _unknown("questions", q, f"question {qid}")
        if not isinstance(q, dict):
            continue
        qt = q.get("type")
        if not ID.match(str(qid)):
            out.append(f"question {qid!r}: id must be lower_snake_case")
        if qid in qids:
            out.append(f"question {qid}: id used twice")
        qids.add(qid)
        if qt not in QUESTION_TYPES:
            out.append(f"question {qid}: type must be one of {QUESTION_TYPES}")
        if not str(q.get("instructions") or "").strip():
            out.append(f"question {qid}: instructions are missing")
        for u in q.get("uses") or []:
            if u not in factor_ids:
                out.append(f"question {qid}: uses unknown factor {u!r}")
        if qt == "score":
            levels = q.get("levels") or []
            if not 2 <= len(levels) <= 10:
                out.append(f"question {qid}: a score question needs 2-10 levels (has {len(levels)})")
            for i, lv in enumerate(levels):
                out += _unknown("levels", lv, f"question {qid} level {i + 1}")
                if not (isinstance(lv, dict) and lv.get("label") and lv.get("criterion")):
                    out.append(f"question {qid}: level {i + 1} needs a label and a criterion")
        elif q.get("levels"):
            out.append(f"question {qid}: only a score question has levels")
        if qt == "noul":
            for k in ("true_description", "false_description"):
                if not str(q.get(k) or "").strip():
                    out.append(f"question {qid}: a noul question needs {k}")
        if qt == "choice":
            opts = q.get("options") or []
            if not 2 <= len(opts) <= 255:
                out.append(f"question {qid}: a choice question needs 2-255 options")
            oids = [o.get("id") for o in opts if isinstance(o, dict)]
            if len(set(oids)) != len(oids) or len(oids) != len(opts) or not all(oids):
                out.append(f"question {qid}: every option needs its own id")
            for o in opts:
                out += _unknown("options", o, f"question {qid} option {o.get('id') if isinstance(o, dict) else '?'}")
                if not isinstance(o, dict):
                    continue
                if o.get("id") and not OPTION_ID.match(str(o["id"])):
                    out.append(f"question {qid}: option id {o['id']!r} may hold only letters, digits, _ and -")
                if not str(o.get("label") or "").strip():
                    out.append(f"question {qid}: option {o.get('id')} needs a label")
                if "value" in o and not (isinstance(o["value"], (int, float)) and 0 <= o["value"] <= 1):
                    out.append(f"question {qid}: option {o.get('id')} value must be from 0 to 1")
        elif q.get("options"):
            out.append(f"question {qid}: only a choice question has options")
    if not qids:
        out.append("no questions")

    comb = rubric.get("combine") or {}
    out += _unknown("combine", comb, "[combine]")
    if comb.get("method", "weighted_mean") != "weighted_mean":
        out.append("combine.method must be weighted_mean")
    weights = comb.get("weights") or {}
    qmap = {q.get("id"): q for q in rubric.get("questions") or [] if isinstance(q, dict)}
    for k, w in weights.items():
        if k not in qids:
            out.append(f"combine.weights: unknown question {k!r}")
        elif not isinstance(w, (int, float)) or isinstance(w, bool) or w < 0:
            out.append(f"combine.weights.{k} must be a number >= 0")

    def counted(k, w):
        q = qmap.get(k) or {}
        if not isinstance(w, (int, float)) or w <= 0:
            return False
        if q.get("type") == "choice":
            return all(isinstance(o.get("value"), (int, float)) for o in q.get("options") or [] if isinstance(o, dict))
        return q.get("type") in ("score", "noul")
    if not any(counted(k, w) for k, w in weights.items()):
        out.append("combine.weights: at least one question needs a weight above 0")
    for k, w in weights.items():
        q = qmap.get(k) or {}
        if q.get("type") == "choice" and isinstance(w, (int, float)) and w > 0 and not counted(k, w):
            out.append(f"combine.weights.{k}: a choice question counts only when every option has a value (0-1)")

    bands = rubric.get("bands") or []
    if not bands:
        out.append("no bands")
    for b in bands:
        out += _unknown("bands", b, f"band {b.get('id') if isinstance(b, dict) else '?'}")
    bands = [b for b in bands if isinstance(b, dict)]
    mins = [b.get("min") for b in bands]
    if bands and mins[0] != lo:
        out.append(f"the first band must start at {lo}")
    if any(not isinstance(m, (int, float)) for m in mins) or mins != sorted(set(mins)):
        out.append("band mins must be numbers, rising, each different")
    elif mins and mins[-1] >= hi:
        out.append(f"every band must start below {hi}")
    bids = [b.get("id") for b in bands]
    if len(set(bids)) != len(bids) or not all(bids):
        out.append("every band needs its own id")
    for b in bands:
        if b.get("id") and not ID.match(str(b["id"])):
            out.append(f"band {b['id']!r}: id must be lower_snake_case")
        if not str(b.get("label") or "").strip():
            out.append(f"band {b.get('id')}: label is missing")
        if not re.match(r"^--c-[a-z0-9-]+$", str(b.get("color") or "")):
            out.append(f"band {b.get('id')}: colour must be a --c-* token")
        elif tokens and b.get("color") not in tokens:
            out.append(f"band {b.get('id')}: colour {b.get('color')!r} is not a --c-* token in tokens.css")
        if "flagged" in b and not isinstance(b["flagged"], bool):
            out.append(f"band {b.get('id')}: flagged must be true or false")
    be = rubric.get("backend") or {}
    out += _unknown("backend", be, "[backend]")
    if "score_on_add" in be and not isinstance(be["score_on_add"], bool):
        out.append("backend.score_on_add must be true or false")
    return out


def band_of(rubric, score):
    """The id of the band a 0-100 score falls in (min inclusive)."""
    best = rubric["bands"][0]["id"]
    for b in rubric["bands"]:
        if score >= b["min"]:
            best = b["id"]
    return best


def question_hash(q, scorer_cfg=None, scorer=None):
    """sha1 of what the scorer is asked for this question, plus its model: a change re-asks only this
    question. For "decisions" that is the question exactly as sent (scorers.decisions_question), so a level's
    site label or an option's `value` costs nothing; for "interactions" the labels are in the prompt too."""
    cfg = scorer_cfg or {}
    if scorer == "decisions" or (scorer is None and "model" in cfg):
        from backend.rubric.scorers import decisions_question
        doc = {"id": q.get("id"), "sent": decisions_question(q), "model": cfg.get("model")}
    else:
        opts = [{k: o.get(k) for k in ("id", "label", "criterion")} for o in q.get("options") or []]
        doc = {k: q.get(k) for k in ("id", "type", "instructions", "levels", "true_description", "false_description")}
        doc["options"] = opts or None          # an option's `value` is the combiner's, not the model's
        doc["model"] = cfg.get("models") or cfg.get("model")
    return hashlib.sha1(json.dumps(doc, sort_keys=True).encode()).hexdigest()


def display(rubric, reg=None):
    """What the site renders, embedded as traders.rubric in each market page:
    {version, name, scale, bands: [{id, label, min, max, color, flagged}],
     questions: [{id, type, label, weight, levels: [label] | options: [{id, label}]}],
     factors: [{id, label, format, description}]  (card = true only)}
    Level / option order is the rubric's; a whale's level probabilities use the same order."""
    reg = registry() if reg is None else reg
    lo, hi = (rubric.get("scale") or [0, 100])[:2]
    bands = rubric["bands"]
    weights = (rubric.get("combine") or {}).get("weights") or {}
    qs = []
    for q in rubric["questions"]:
        item = {"id": q["id"], "type": q["type"], "label": q.get("label") or q["id"],
                "weight": weights.get(q["id"], 0)}
        if q["type"] == "score":
            item["levels"] = [lv["label"] for lv in q["levels"]]
        elif q["type"] == "choice":
            item["options"] = [{"id": o["id"], "label": o.get("label") or o["id"]} for o in q["options"]]
        qs.append(item)
    return {
        "version": rubric["version"],
        "name": rubric.get("name") or "Insider score",
        "scale": [lo, hi],
        "bands": [{"id": b["id"], "label": b["label"], "min": b["min"],
                   "max": bands[i + 1]["min"] if i + 1 < len(bands) else hi,
                   "color": b["color"], "flagged": bool(b.get("flagged"))}
                  for i, b in enumerate(bands)],
        "questions": qs,
        "factors": [{"id": f["id"], "label": f["label"], "format": factor_format(f, reg), "description": f["description"]}
                    for f in rubric["factors"] if f.get("card")],
    }
