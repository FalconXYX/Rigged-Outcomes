"""
Answers already paid for, one document per market, and the day's request count.

Key: (wallet item, question id, question hash, input hash), where
  item           "<user_id>@<first buy ISO>" (a wallet can hold several whale positions)
  question hash  spec.question_hash: the question's wording, levels, options and the scorer's model
  input hash     prompt.input_hash: the text the model read for this wallet

So a rubric edit re-asks only what it touched: new wording on one question re-asks that question for every
wallet; a new or changed `send` factor or a new template re-asks every question (the input changed); weights,
bands, labels, `card` and the version cost nothing (combine.py runs again on the cached answers, and every
result carries the version it was combined under).

Document: {"market": name, "answers": {"<item>|<qid>|<qhash>|<ihash>": answer}}
Stores:   AnswerCache(path)   a local file (the CLI; default data/rubric/cache/<market>.json)
          AnswerCache(doc=)   a document read from elsewhere (the backend's rubric:<market> blob, store.py)
Ledger(path): requests sent per scorer and Pacific day (Gemini's quota day), so a rubric's daily_cap holds
across CLI runs of one day.
"""
import json
import os
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

QUOTA_TZ = ZoneInfo("America/Los_Angeles")


def key(item, qid, qhash, ihash):
    return f"{item}|{qid}|{qhash}|{ihash}"


class AnswerCache:
    def __init__(self, path=None, market=None, doc=None):
        self.path = path
        self.doc = {"market": market, "answers": {}}
        if doc is not None:
            self.doc = {"market": doc.get("market", market), "answers": dict(doc.get("answers") or {})}
        elif path and os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                self.doc = json.load(f)
            self.doc.setdefault("answers", {})
        self.dirty = False
        self._lock = threading.Lock()

    def get(self, item, qid, qhash, ihash):
        return self.doc["answers"].get(key(item, qid, qhash, ihash))

    def put(self, item, qid, qhash, ihash, answer):
        with self._lock:
            self.doc["answers"][key(item, qid, qhash, ihash)] = answer
            self.dirty = True

    def update(self, answers):
        """Merge {key: answer} (a step's checkpointed answers)."""
        if answers:
            with self._lock:
                self.doc["answers"].update(answers)
                self.dirty = True

    def keep_only(self, keys):
        """Drop every answer whose key is not in keys (answers no current question or input can use)."""
        keys = set(keys)
        with self._lock:
            before = len(self.doc["answers"])
            self.doc["answers"] = {k: v for k, v in self.doc["answers"].items() if k in keys}
            self.dirty |= len(self.doc["answers"]) != before
        return before - len(self.doc["answers"])

    def save(self):
        if not (self.path and self.dirty):
            return
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with self._lock:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.doc, f, separators=(",", ":"))
            os.replace(tmp, self.path)
            self.dirty = False


def quota_day(epoch=None):
    """Gemini's quota day (it resets at midnight in Los Angeles), "YYYY-MM-DD"."""
    import time
    return datetime.fromtimestamp(time.time() if epoch is None else epoch, QUOTA_TZ).strftime("%Y-%m-%d")


class Ledger:
    """{day: {scorer: requests}} in a small file (path None: in memory). Days other than today are dropped."""

    def __init__(self, path=None, day=quota_day):
        self.path, self.day = path, day
        self._lock = threading.Lock()
        self.doc = {}
        if path and os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    self.doc = json.load(f)
            except (OSError, ValueError):
                self.doc = {}

    def used(self, scorer):
        return int((self.doc.get(self.day()) or {}).get(scorer, 0))

    def add(self, scorer, n=1):
        with self._lock:
            today = self.day()
            self.doc = {today: dict(self.doc.get(today) or {})}
            self.doc[today][scorer] = self.doc[today].get(scorer, 0) + n
            if self.path:
                os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
                tmp = self.path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(self.doc, f)
                os.replace(tmp, self.path)
