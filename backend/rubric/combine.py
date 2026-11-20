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
    spectrum(rubric, answers) -> (score rounded to 0.1, band id); (None, None) when no counted
                                 question was answered
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


def spectrum(rubric, answers):
    """The weighted mean of the counted questions' values, on the rubric's scale, and its band. A question
    left unanswered drops out with its weight, so a partly answered wallet still gets a fair score."""
    weights = (rubric.get("combine") or {}).get("weights") or {}
    lo, hi = (rubric.get("scale") or [0, 100])[:2]
    num = den = 0.0
    for q in rubric["questions"]:
        w = float(weights.get(q["id"], 0) or 0)
        v = value(q, answers.get(q["id"])) if w > 0 else None
        if v is not None:
            num += w * v
            den += w
    if den <= 0:
        return None, None
    score = round(lo + (hi - lo) * num / den, 1)
    return score, band_of(rubric, score)
