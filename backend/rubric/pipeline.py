"""
Score one market's whales: facts file -> rendered inputs -> cached or new answers -> 0-100 score and band per
whale.

    score_market(rubric, facts_doc, cache, scorer=None, dry_run=False, ...) -> result
        facts_doc     a facts file (wallet_facts.market_facts / fact_sources CLI)
        cache         cache.AnswerCache for this market
        scorer        scorers.get_scorer(rubric) by default; on ScorerUnavailable the run switches to the
                      rubric's fallback (make_scorer(name) builds it) and goes on
        dry_run       nothing is sent: result.stats.would_ask counts the requests it would make
        max_requests  stop after this many requests (stats.stopped = "max_requests")
        stop_when     stop_when() before each request; True stops the run (stats.stopped = "another job is
                      waiting", stats.stop = "give_way": a step's ctx.give_way)
        on_answers    on_answers({cache key: answer}) after each request (a step checkpoints them)
        refresh       ask again even what is cached (a new model, a suspicion about old answers)
        only          ask only the whales whose wallet or user id is in this set (stats.skipped counts the others
                      left unanswered); None: every whale

    result = {
      "market": name, "version": the rubric's, "rubric": spec.display(rubric), "scorer": "decisions",
      "stats": {"whales", "cached", "asked", "requests", "would_ask", "unscored", "refused", "skipped", "tokens",
                "stopped": None | why, "stop": None | "busy" | "unavailable" | "max_requests" | "give_way"},
      "whales": [{"key", "item", "wallet", "user_id", "first_buy", "side", "current_rules", "score", "band",
                  "tier", "base", "evidence" (combine.detail), "answers", "facts", "flags"}],
    }

    trader_fields(whale, rubric) -> what a market page's traders[i] carries:
      {"score": 73.4, "band": "suspicious",
       "q": {"timing": {"p": [...], "level": 3}, "single_purpose": {"p": 0.71},
             "profile": {"choice": "early_informed", "p": 0.62}},
       "facts": {factor id: value, card factors only}}
    page_part(result, rubric) -> {"version", "rubric": display, "whales": {key: trader_fields}}: what export.py
      attaches to a page (traders.rubric, and traders[i].spectrum by key "<user_id>@<first trade epoch>").
"""
from datetime import datetime, timezone

from backend.rubric import combine, prompt, spec
from backend.rubric.cache import key as cache_key
from backend.rubric.scorers import ScorerBusy, ScorerRefused, ScorerUnavailable, get_scorer


def item_id(whale):
    return f"{whale['user_id']}@{whale['first_buy']}"


def epoch(iso):
    """Epoch seconds of an ISO time (a time with no offset is UTC, as pandas reads the CSVs)."""
    t = datetime.fromisoformat(str(iso).strip().replace("Z", "+00:00").replace(" ", "T", 1))
    return int((t if t.tzinfo else t.replace(tzinfo=timezone.utc)).timestamp())


def page_key(user_id, first_trade):
    """A whale's key on a market page: "<user_id lower>@<epoch of its first trade>"."""
    t = first_trade if isinstance(first_trade, (int, float)) else epoch(first_trade)
    return f"{str(user_id).lower()}@{int(t)}"


def score_market(rubric, facts_doc, cache, scorer=None, dry_run=False, on_answers=None, make_scorer=None,
                 max_requests=None, refresh=False, log=None, only=None, stop_when=None):
    reg = spec.registry()
    sc_cfg = rubric.get("scorer") or {}
    make = make_scorer or (lambda name: get_scorer(rubric, name))
    scorer = scorer or make(sc_cfg.get("use", "decisions"))
    fallback = sc_cfg.get("fallback")
    names = [n for n in dict.fromkeys((scorer.name, sc_cfg.get("use"), fallback)) if n]
    hashes = {(n, q["id"]): spec.question_hash(q, sc_cfg.get(n), n) for n in names for q in rubric["questions"]}
    questions = rubric["questions"]
    stats = {"whales": 0, "cached": 0, "asked": 0, "requests": 0, "would_ask": 0, "unscored": 0, "refused": 0,
             "skipped": 0, "tokens": 0, "stopped": None, "stop": None}

    rows = []
    for w in facts_doc["whales"]:
        text = prompt.render_input(rubric, facts_doc["market"], w["facts"], reg, side=w.get("position_side", "BUY"))
        rows.append({"whale": w, "item": item_id(w), "input": text, "ihash": prompt.input_hash(text)})
    fresh = {}                                   # answers asked in this run (refresh: only these count)

    def cached(row, q):
        """This question's answer for this wallet from any scorer (the one in use first), or None."""
        for name in names:
            k = cache_key(row["item"], q["id"], hashes[(name, q["id"])], row["ihash"])
            a = fresh.get(k) if refresh else cache.doc["answers"].get(k)
            if a is not None:
                return a
        return None

    def missing(row):
        return [q for q in questions if cached(row, q) is None]

    todo = [r for r in rows if missing(r)]
    stats["cached"] = len(rows) - len(todo)
    if only is not None:                         # ask only these wallets; the rest keep what is cached
        only = {str(x).lower() for x in only}
        kept = [r for r in todo if r["whale"]["user_id"].lower() in only or r["whale"]["wallet"].lower() in only]
        stats["skipped"], todo = len(todo) - len(kept), kept
    refused = set()
    i = 0
    while i < len(todo) and not dry_run:
        if max_requests is not None and stats["requests"] >= max_requests:
            stats["stopped"], stats["stop"] = f"stopped after {max_requests} requests", "max_requests"
            break
        if stop_when is not None and stop_when():
            stats["stopped"], stats["stop"] = "another job is waiting", "give_way"
            break
        batch = todo[i:i + scorer.batch]
        need = {q["id"] for r in batch for q in missing(r)}
        qs = [q for q in questions if q["id"] in need]
        try:
            got = scorer.score([{"id": r["item"], "input": r["input"]} for r in batch], qs)
        except ScorerUnavailable as e:
            if log:
                log(f"{scorer.name}: {e}")
            if not fallback or fallback == scorer.name:
                stats["stopped"], stats["stop"] = str(e), "unavailable"
                break
            scorer, fallback = make(fallback), None
            if scorer.name not in names:
                names.append(scorer.name)
                hashes.update({(scorer.name, q["id"]): spec.question_hash(q, sc_cfg.get(scorer.name), scorer.name)
                               for q in questions})
            continue
        except ScorerBusy as e:
            stats["stopped"], stats["stop"] = str(e), "busy"
            break
        except ScorerRefused as e:
            if log:
                log(f"refused: {e}")
            refused.update(r["item"] for r in batch)
            stats["requests"] += 1
            i += len(batch)
            continue
        stats["requests"] += 1
        new = {}
        for r in batch:
            for q in qs:
                a = got.get(r["item"], {}).get(q["id"])
                if a is not None:
                    k = cache_key(r["item"], q["id"], hashes[(scorer.name, q["id"])], r["ihash"])
                    new[k] = a
        fresh.update(new)
        cache.update(new)
        stats["asked"] += len(batch)
        if on_answers:
            on_answers(new)
        if stats["requests"] % 20 == 0:
            cache.save()
        i += len(batch)
    cache.save()
    if dry_run:
        stats["would_ask"] = -(-len(todo) // max(1, scorer.batch))
    stats["tokens"] = int(getattr(scorer, "tokens", 0) or 0)
    stats["refused"] = len(refused)

    whales = []
    for r in rows:
        answers = {q["id"]: a for q in questions if (a := cached(r, q)) is not None}
        w = r["whale"]
        how = combine.detail(rubric, answers, w["facts"], w.get("flags"))
        score, band = how["score"], how["band"]
        if score is None:
            stats["unscored"] += 1
        whales.append({"key": page_key(w["user_id"], w["first_buy"]), "item": r["item"], "wallet": w["wallet"],
                       "user_id": w["user_id"], "first_buy": w["first_buy"], "side": w.get("position_side", "BUY"),
                       "current_rules": w.get("current_rules"), "score": score, "band": band, "tier": how["tier"],
                       "base": how["base"], "evidence": how["evidence"], "answers": answers,
                       "facts": w["facts"], "flags": w.get("flags") or {}})
    stats["whales"] = len(whales)
    return {"market": facts_doc["market"]["name"], "version": rubric["version"], "rubric": spec.display(rubric, reg),
            "scorer": scorer.name, "stats": stats, "whales": whales}


def used_keys(rubric, facts_doc):
    """Every cache key the rubric's questions could use for these whales (any scorer): what a cache may keep."""
    reg = spec.registry()
    sc_cfg = rubric.get("scorer") or {}
    out = set()
    for w in facts_doc["whales"]:
        text = prompt.render_input(rubric, facts_doc["market"], w["facts"], reg, side=w.get("position_side", "BUY"))
        ih = prompt.input_hash(text)
        for q in rubric["questions"]:
            for name in spec.SCORERS:
                out.add(cache_key(item_id(w), q["id"], spec.question_hash(q, sc_cfg.get(name), name), ih))
    return out


def _r(x, nd):
    return None if x is None else round(float(x), nd)


def trader_fields(whale, rubric):
    """The compact per-trader block a market page carries (see the module doc); None when it has no score."""
    if whale.get("score") is None:
        return None
    q = {}
    for qq in rubric["questions"]:
        a = whale["answers"].get(qq["id"])
        if not a:
            continue
        if qq["type"] == "score":
            q[qq["id"]] = {"p": [round(x, 3) for x in a["p"]], "level": max(range(len(a["p"])), key=a["p"].__getitem__)}
        elif qq["type"] == "noul":
            q[qq["id"]] = {"p": round(a["p"], 3)}
        else:
            q[qq["id"]] = {"choice": a["choice"], "p": round(a["p"].get(a["choice"], 0.0), 3)}
    card = [f["id"] for f in rubric["factors"] if f.get("card")]
    facts = {k: whale["facts"].get(k) for k in card}
    facts = {k: (_r(v, 4) if isinstance(v, float) else v) for k, v in facts.items()}
    return {"score": whale["score"], "band": whale["band"], "q": q, "facts": facts}


def page_part(result, rubric):
    """What export.py attaches to a market's page: the rubric's display part and each scored whale's fields by
    page key."""
    whales = {}
    for w in result["whales"]:
        f = trader_fields(w, rubric)
        if f is not None:
            whales[w["key"]] = f
    return {"version": rubric["version"], "rubric": spec.display(rubric), "whales": whales}
