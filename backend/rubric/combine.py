"""
Answers -> one 0-100 score and its band. No model call: changing weights or
bands in the rubric re-combines cached answers for free.

An answer set is {question id: answer}, the same shape from every scorer:

    score   {"type": "score", "score": 2.78, "p": [0.003, 0.022, 0.164, 0.810], "confidence": 0.81}
            score = the expected level, levels counted from 0 (the Decisions API's own score);
            p = one probability per level, in the rubric's order
    noul    {"type": "noul", "p": 0.73}                      probability of yes
    choice  {"type": "choice", "choice": "analyst", "p": {"analyst": 0.61, "regular": 0.2, ...}}

    value(q, answer)          -> 0-1 (None for a missing answer, or a choice question whose options carry no
                                 `value`)
    spectrum(rubric, answers, facts=None, flags=None)
                              -> (score rounded to 0.1, band id); (None, None) when no counted
                                 question was answered
    detail(rubric, answers, facts=None, flags=None)
                              -> {"score", "band", "tier": id | None, "base", "evidence"}

Methods ([combine] method):
    weighted_mean  the weighted mean of the counted questions' values (`weights`), on the rubric's scale
    tiered         the same weighted mean is every wallet's base. A [[combine.tiers]] block lifts the wallets that
                   match all its conditions (today's `flags`, the whale's `facts`; a value must be equal, a
                   {min, max} table holds both ends) into [floor, ceiling]: where in that range is the tier's own
                   weighted mean (`weights`, else [combine] weights) stretched from `span` [low, high], and the
                   wallet keeps its base if that is higher. The first matching tier, in file order, applies.
"""
from backend.rubric.spec import band_of


def value(q, answer):
    """A question's answer on 0-1: a score question's expected level over its top level, a noul's yes
    probability, a choice question's expected option `value` (when every option has one; else None: shown,
    not counted)."""
    if not answer:
        return None
    if q["type"] == "score":
        top = len(q["levels"]) - 1
        return None if top <= 0 or answer.get("score") is None else min(1.0, max(0.0, answer["score"] / top))
    if q["type"] == "noul":
        p = answer.get("p")
        return None if p is None else min(1.0, max(0.0, float(p)))
    opts = q.get("options") or []
    if not opts or any(not isinstance(o.get("value"), (int, float)) for o in opts):
        return None
    probs = answer.get("p") or {}
    total = sum(float(probs.get(o["id"], 0) or 0) for o in opts)
    if total <= 0:
        return None
    return min(1.0, max(0.0, sum(float(probs.get(o["id"], 0) or 0) * o["value"] for o in opts) / total))


def _mean(rubric, answers, weights):
    """The weighted mean (0-1) of the counted questions' values; None when none was answered. A question left
    unanswered drops out with its weight, so a partly answered wallet still gets a fair score."""
    num = den = 0.0
    for q in rubric["questions"]:
        w = float(weights.get(q["id"], 0) or 0)
        v = value(q, answers.get(q["id"])) if w > 0 else None
        if v is not None:
            num += w * v
            den += w
    return num / den if den > 0 else None


def _holds(cond, got):
    if got is None:
        return False
    if isinstance(cond, dict):
        if isinstance(got, bool) or not isinstance(got, (int, float)):
            return False
        return cond.get("min", got) <= got <= cond.get("max", got)
    if isinstance(cond, bool) or isinstance(got, bool):
        return isinstance(cond, bool) and isinstance(got, bool) and cond == got
    return cond == got


def tier_of(rubric, facts=None, flags=None):
    """The first [[combine.tiers]] block whose every condition holds for this whale, or None."""
    comb = rubric.get("combine") or {}
    if comb.get("method") != "tiered":
        return None
    facts, flags = facts or {}, flags or {}
    for t in comb.get("tiers") or []:
        if all(_holds(c, flags.get(k)) for k, c in (t.get("flags") or {}).items()) and \
                all(_holds(c, facts.get(k)) for k, c in (t.get("facts") or {}).items()):
            return t
    return None


def detail(rubric, answers, facts=None, flags=None):
    """{"score", "band", "tier", "base", "evidence"}: the score with how it was made (see the module doc)."""
    comb = rubric.get("combine") or {}
    lo, hi = (rubric.get("scale") or [0, 100])[:2]
    base = _mean(rubric, answers, comb.get("weights") or {})
    out = {"score": None, "band": None, "tier": None, "base": None, "evidence": None}
    if base is None:
        return out
    score = lo + (hi - lo) * base
    out["base"] = round(score, 1)
    t = tier_of(rubric, facts, flags)
    if t is not None:
        ev = _mean(rubric, answers, t.get("weights") or comb.get("weights") or {})
        a, b = (t.get("span") or [0, 1])[:2]
        x = 0.0 if ev is None else min(1.0, max(0.0, (ev - a) / (b - a)))
        lifted = t["floor"] + (t.get("ceiling", hi) - t["floor"]) * x
        out["tier"], out["evidence"] = t["id"], None if ev is None else round(ev, 3)
        score = max(score, lifted)
    out["score"] = round(score, 1)
    out["band"] = band_of(rubric, out["score"])
    return out


def spectrum(rubric, answers, facts=None, flags=None):
    """(score, band id) on the rubric's scale; (None, None) when no counted question was answered. facts and
    flags are the whale's (they decide a "tiered" rubric's tier; a weighted mean ignores them)."""
    d = detail(rubric, answers, facts, flags)
    return d["score"], d["band"]
