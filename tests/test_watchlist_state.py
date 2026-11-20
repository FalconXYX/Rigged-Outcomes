#!/usr/bin/env python3
"""
Offline tests for the daily job's memory in the site's database
(watchlist.state) against the fake API (tests/fake_api.py). No network, and
no git: no repository is made, not even in a temporary directory.

    python3 tests/test_watchlist_state.py

Covers the state's files round trip (identical rows, identical bytes when
made twice), the manifest check (a wrong sha256 or row count, a missing
table, an empty state), unknown columns, pull and push (no API, no saved
state, a push then a pull into a fresh store, a local store with newer data
and --force with its backup, a state saved by another run since the pull,
nothing changed and nothing sent, the lists sent only when they changed,
--seed and --onto-head, the news cache never failing a run, the head's
open_left and the open_list output the catch-up run reads), and prune (what
it removes, and that the next day's cards are the same with or without it).
"""
import contextlib
import importlib
import io
import json
import re
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fake_api  # noqa: E402
from fake_api import FakeApi  # noqa: E402
from backend import contract as C  # noqa: E402

# the fake API takes the backend's blob names (site:*, scratch:*) as the Worker does (CONTRACT.blob_names)
fake_api.BLOB_NAME = re.compile("|".join(f"(?:{p})" for p in C.CONTRACT["blob_names"].values()))
fake_api.HISTORY = re.compile(r"^(state|site:(index|closed|open)\.json)$")
fake_api.PREFIX = re.compile(r"^[a-z0-9:._/-]{0,40}$")
from watchlist import ai, blobs, state as S, store  # noqa: E402
from watchlist.common import DAY, iso, to_epoch  # noqa: E402

NOW = to_epoch("2026-11-14T04:17:00Z")


def filled(conn=None):
    """A store with rows in every state table (and one the state leaves alone)."""
    conn = conn or store.connect(":memory:")
    store.save_ai_label(conn, "m1", "r1", {"type": "other", "reveal_kind": "none"}, "fake", True, {"a": 1})
    store.save_closed_check(conn, "0xabc", "closed-check-5", {"verdict": "weak", "headlines": [{"title": "é ✓"}]})
    store.record_pool_entry(conn, "e1", "ev-1", "Jane Roe out?", True, NOW - 3 * DAY, capped=["9"])
    store.save_filing(conn, "e1", "us_official", {"subject": "Jane Roe"}, "rule", story="jane roe")
    store.save_open_answer(conn, "jane roe", "sha1", "open-step-1", 1, {"steps": [], "given": []}, "gemma",
                           created_at=NOW - DAY)
    card = {"id": "jane roe", "place": "new step", "label": "new", "listed_since": "2026-11-13",
            "event_ids": ["e1"], "slugs": ["ev-1"], "question": "Will Jane Roe leave?"}
    store.save_open_day(conn, "2026-11-13", "updated", NOW - DAY, [card], version="open-step-1",
                        info={"seen_step_headlines": ["a"], "new_today": 1})
    store.set_meta(conn, "not state", {"x": 1})
    return conn


def change(conn, key):
    """One more row in the state (an open event filed)."""
    store.save_filing(conn, key, None, None, "rule")


def rows(conn, table):
    order = ", ".join(S.TABLES[table])
    return [dict(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY {order}").fetchall()]


@contextlib.contextmanager
def quiet():
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        yield out


def test_round_trip_and_same_bytes():
    a = filled()
    files = S.dump_files(a, made_at=NOW)
    assert files == S.dump_files(a, made_at=NOW)                       # dumping twice: the same bytes
    assert set(files) == {"manifest.json"} | {f"{t}.jsonl" for t in S.TABLES}
    b = store.connect(":memory:")
    counts = S.load_files(b, files)
    assert counts["open_cards"] == 1 and counts["open_listed"] == 2 and sum(counts.values()) == 9
    for t in S.TABLES:
        assert rows(a, t) == rows(b, t), t
    assert S.dump_files(b, made_at=NOW) == files                         # and back to the same bytes
    assert store.get_meta(b, "not state") is None                        # the state leaves other tables alone
    m = json.loads(files["manifest.json"])
    assert m["latest"] == {"open_runs.run_at": NOW - DAY,
                           "closed_checks.created_at": rows(a, "closed_checks")[0]["created_at"]}
    assert m["format"] == 1 and m["tables"]["open_filings"]["rows"] == 1
    # rows are in primary key order, keys sorted
    first = files["open_listed.jsonl"].decode().splitlines()
    assert first == sorted(first) and list(json.loads(first[0])) == sorted(json.loads(first[0]))


def test_bad_state_is_refused():
    files = S.dump_files(filled(), made_at=NOW)

    def refused(fs, words):
        try:
            S.load_files(store.connect(":memory:"), fs)
        except S.StateError as e:
            assert e.code == 2 and words in str(e), str(e)
            return
        raise AssertionError(f"not refused: {words}")

    bad = dict(files, **{"open_runs.jsonl": files["open_runs.jsonl"] + b"{}\n"})
    refused(bad, "sha256")
    m = json.loads(files["manifest.json"])
    m["tables"]["open_cards"]["rows"] = 5
    refused(dict(files, **{"manifest.json": json.dumps(m).encode()}), "rows")
    refused({k: v for k, v in files.items() if k != "open_pool.jsonl"}, "no open_pool table")
    refused({k: v for k, v in files.items() if k != "manifest.json"}, "manifest")
    m = json.loads(files["manifest.json"])
    m["format"] = 2
    refused(dict(files, **{"manifest.json": json.dumps(m).encode()}), "format")
    empty = S.dump_files(store.connect(":memory:"), made_at=NOW)
    refused(empty, "empty")
    assert sum(S.load_files(store.connect(":memory:"), empty, allow_empty=True).values()) == 0


def test_unknown_columns_are_left_out():
    files = S.dump_files(filled(), made_at=NOW)
    line = json.loads(files["open_filings.jsonl"])
    line["new_column"] = "x"
    data = (json.dumps(line, sort_keys=True) + "\n").encode()
    m = json.loads(files["manifest.json"])
    m["tables"]["open_filings"]["sha256"] = S._sha256(data)
    conn = store.connect(":memory:")
    with quiet():
        S.load_files(conn, dict(files, **{"open_filings.jsonl": data, "manifest.json": json.dumps(m).encode()}))
    assert rows(conn, "open_filings")[0]["kind"] == "us_official"
    # a state saved before a table left the format still loads (the extra table is left alone)
    m = json.loads(files["manifest.json"])
    m["tables"]["open_reviews"] = {"rows": 1, "sha256": S._sha256(b"{}\n")}
    with quiet():
        S.load_files(store.connect(":memory:"), dict(files, **{"open_reviews.jsonl": b"{}\n",
                                                               "manifest.json": json.dumps(m).encode()}))


# ── the database: the fake API (tests/fake_api.py) ───────────────────────────

@contextlib.contextmanager
def place():
    """A run's folders (the site's lists, the job's files) and its store file, under a temporary directory."""
    with tempfile.TemporaryDirectory() as tmp:
        for d in ("site", "job"):
            os.makedirs(os.path.join(tmp, d))
        yield {"site_dir": os.path.join(tmp, "site"), "job_dir": os.path.join(tmp, "job")}, tmp


def _site(kw, generated="2026-11-14T04:20:00+00:00"):
    with open(os.path.join(kw["site_dir"], "closed.json"), "w") as f:
        json.dump({"generated_at": generated, "days": []}, f)
    with open(os.path.join(kw["site_dir"], "open.json"), "w") as f:
        json.dump({"days": [{"day": "2026-11-13", "run_at": "2026-11-13T04:00:00Z", "cards": []}]}, f)


def read(path, mode="rb"):
    with open(path, mode) as f:
        return f.read()


def refused(fn, code, words):
    try:
        with quiet():
            fn()
    except S.StateError as e:
        assert e.code == code and words in str(e), (code, str(e))
        return e
    raise AssertionError(f"not refused: {words}")


def saved_files(api):
    """The saved state's files, as bytes."""
    _, doc = blobs.get_json(api, S.STATE)
    return {k: v.encode("utf-8") for k, v in doc["files"].items()}


def test_no_api():
    with place() as (kw, tmp):
        refused(lambda: S.pull(store.connect(":memory:"), None, db_path=":memory:", **kw), 2, "No API configured")
        refused(lambda: S.push(filled(), None, seed=True, now=NOW, **kw), 3, "No API configured")
        api = FakeApi()
        refused(lambda: S.push(filled(), api, now=NOW, **kw), 3, "has not pulled")
        refused(lambda: S.push(filled(), api, onto_head=True, now=NOW, **kw), 3, "seed it")
        assert api.calls["PUT"] == 0 and not os.listdir(kw["job_dir"])
        # the CLI: no API configured is exit 2 for a pull, 3 for a push
        old = (os.environ.pop("WATCHLIST_API_URL", None), os.environ.pop("WATCHLIST_JOB_TOKEN", None), ai.ENV_FILE)
        ai.ENV_FILE = os.path.join(tmp, "no.env")
        try:
            db = os.path.join(tmp, "cli.db")
            with contextlib.redirect_stderr(io.StringIO()) as err:
                assert S.main(["pull", "--db", db, "--site-dir", kw["site_dir"]]) == 2
                assert S.main(["push", "--db", db, "--site-dir", kw["site_dir"]]) == 3
            assert "No API configured" in err.getvalue()
        finally:
            ai.ENV_FILE = old[2]
            for k, v in zip(("WATCHLIST_API_URL", "WATCHLIST_JOB_TOKEN"), old[:2]):
                if v is not None:
                    os.environ[k] = v


def test_no_saved_state_starts_fresh_and_saves_the_first():
    """A fresh database (no state at all) is no refusal: the run starts from the store as it is, with a warning,
    and its push saves the first state; one saved by someone else meanwhile is never overwritten."""
    api = FakeApi()
    with place() as (kw, _), place() as (other, _):
        # an added market alone does not make it a used database (a first run whose save failed may add one)
        blobs.put_json(api, "market:gemini-carbon", {}, {"entry": {"name": "gemini-carbon", "slug": "g-c"}})
        conn = store.connect(":memory:")
        with quiet() as out:
            got = S.pull(conn, api, db_path=":memory:", now=NOW, **kw)
        assert got == {"fresh": True} and "No saved state in the database" in out.getvalue()
        assert not os.path.exists(os.path.join(kw["site_dir"], "closed.json"))
        pulled = json.loads(read(os.path.join(kw["job_dir"], "state_pulled.json")))
        assert pulled["fresh"] and pulled["version"] is None
        # nothing changed: nothing to save, and still a fresh start
        with quiet():
            same = S.push(conn, api, now=NOW + 5, **kw)
        assert same["changed"] is False and same["version"] is None and blobs.head(api, S.STATE) is None
        # the run's work: its push saves the first state
        change(conn, "acme")
        with quiet():
            first = S.push(conn, api, now=NOW + 20, **kw)
        assert first["changed"] and blobs.head(api, S.STATE)["version"] == first["version"]
        assert rows(conn, "open_filings")[0]["event_id"] == "acme"
        # another place started fresh too, but the first state was saved meanwhile: it is refused, not overwritten
        conn2 = store.connect(":memory:")
        with quiet():
            S.pull(conn2, FakeApi(), db_path=":memory:", now=NOW, **other)          # its own empty database
        change(conn2, "other")
        e = refused(lambda: S.push(conn2, api, now=NOW + 40, **other), 3, "changed since this run's pull")
        assert e.kind == "outside" and blobs.head(api, S.STATE)["version"] == first["version"]


def test_a_used_database_without_its_state_is_refused():
    """A list, the news cache or a replaced state version is saved only after a state: holding one without a
    state means the state was lost or API_URL points at another database. Starting fresh there would record every
    first-entry 99c check again, so the pull refuses, as the setup's problem, and loads nothing."""
    for what, fill in (("site:closed.json", lambda api: blobs.put(api, "site:closed.json", b'{"days": []}',
                                                                  {"by": "mac"})),
                       ("replaced state version", lambda api: api.kept.setdefault("state", []).append(
                           {"name": "state", "version": "a" * 32, "parts": 1, "bytes": 1, "sha256": "0" * 64,
                            "meta": "{}", "updated_at": NOW, "replaced_at": NOW}))):
        api = FakeApi()
        fill(api)
        with place() as (kw, _):
            e = refused(lambda: S.pull(store.connect(":memory:"), api, db_path=":memory:", now=NOW, **kw), 2,
                        "but no saved state")
            assert e.kind == "config" and what in str(e) and "restore state VERSION" in str(e), (what, str(e))
            assert not os.listdir(kw["job_dir"]) and not os.listdir(kw["site_dir"])
    # the check itself waits out an API that does not answer like every pull does: an outside refusal then
    api = FakeApi()
    api.fail_next(503, "server", method="GET", path="prefix=")
    with place() as (kw, _):
        e = refused(lambda: S.pull(store.connect(":memory:"), api, db_path=":memory:", now=NOW, **kw), 2,
                    "could not be downloaded")
        assert e.kind == "outside"


def test_refusals_say_whose_they_are():
    """An API that does not answer is an outside refusal; a refused token or a damaged state is the setup's."""
    with place() as (kw, _):
        api = FakeApi()
        with quiet():
            S.push(filled(), api, seed=True, now=NOW, **kw)
        for status, code, kind in ((503, "server", "outside"), (None, "network", "outside"),
                                   (401, "unauthorized", "config"), (429, "locked", "config")):
            api.fail_next(status, code, method="GET")
            e = refused(lambda: S.pull(store.connect(":memory:"), api, db_path=":memory:", **kw), 2,
                        "could not be downloaded")
            assert e.kind == kind, (status, e.kind)
            api.fail_next(status, code, method="GET")
            e = refused(lambda: S.push(filled(), api, onto_head=True, now=NOW, **kw), 3, "nothing was saved")
            assert e.kind == kind, (status, e.kind)
        api.parts[("state", api.blobs["state"]["version"], 0)] = "AAAA"           # damaged
        e = refused(lambda: S.pull(store.connect(":memory:"), api, db_path=":memory:", **kw), 2, "versions state")
        assert e.kind == "config"


def test_push_then_pull_into_a_fresh_store():
    api = FakeApi()
    with place() as (a, _), place() as (b, _):
        _site(a)
        src = filled()
        with quiet():
            got = S.push(src, api, seed=True, by="mac", now=NOW, **a)
        assert got["changed"] and got["lists"] == ["closed.json", "open.json"]
        h = blobs.head(api, S.STATE)
        assert h["version"] == got["version"] and h["meta"]["by"] == "mac" and h["meta"]["open_day"] == "2026-11-13"
        assert h["meta"]["tables"]["open_filings"]["rows"] == 1
        assert json.loads(read(os.path.join(a["job_dir"], "state_pushed.json"))) == \
            {"ok": True, "version": got["version"], "changed": True, "at": NOW}
        for name in S.SITE_FILES:
            assert blobs.get(api, S._list_name(name))[1] == read(os.path.join(a["site_dir"], name))
            assert blobs.head(api, S._list_name(name))["meta"] == {"by": "mac", "made_at": iso(NOW), "run_url": None}
        # b: a fresh runner pulls the state and the published lists
        conn = store.connect(":memory:")
        with quiet():
            m = S.pull(conn, api, db_path=":memory:", now=NOW + 60, **b)
        assert m["by"] == "mac" and all(rows(conn, t) == rows(src, t) for t in S.TABLES)
        for name in S.SITE_FILES:
            assert read(os.path.join(b["site_dir"], name)) == read(os.path.join(a["site_dir"], name))
        pulled = json.loads(read(os.path.join(b["job_dir"], "state_pulled.json")))
        assert pulled == {"version": got["version"], "made_at": iso(NOW), "at": NOW + 60,
                          "tables": {t: v["sha256"] for t, v in m["tables"].items()}}
        assert saved_files(api) == S.dump_files(src, by="mac", made_at=NOW, site_dir=a["site_dir"])
        # a list missing from the database leaves the local file as it is
        del api.blobs["site:open.json"]
        with open(os.path.join(b["site_dir"], "open.json"), "w") as f:
            f.write("mine")
        with quiet():
            S.pull(conn, api, db_path=":memory:", now=NOW + 70, **b)
        assert read(os.path.join(b["site_dir"], "open.json"), "r") == "mine"


def test_a_newer_local_store_is_kept_unless_forced():
    api = FakeApi()
    with place() as (a, tmp):
        _site(a)
        src = filled()
        with quiet():
            S.push(src, api, seed=True, now=NOW, **a)
        db = os.path.join(tmp, "local.db")
        local = filled(store.connect(db))
        store.save_open_day(local, "2026-11-20", "updated", NOW + 10_000, [])
        refused(lambda: S.pull(local, api, db_path=db, now=NOW + 400, **a), 2, "open_runs.run_at")
        assert any(r["day"] == "2026-11-20" for r in rows(local, "open_runs"))
        for i in range(4):                                       # --force backs the store up, keeping 3 copies
            with quiet():
                S.pull(local, api, db_path=db, force=True, now=NOW + 500 + i, **a)
        assert rows(local, "open_runs") == rows(src, "open_runs")
        backups = sorted(x for x in os.listdir(tmp) if x.startswith("local.db.bak-"))
        assert len(backups) == S.BACKUPS_KEPT, backups
        # a state that does not match its manifest is refused before anything changes
        api.parts[("state", api.blobs["state"]["version"], 0)] = "AAAA"
        before = rows(local, "open_runs")
        refused(lambda: S.pull(local, api, db_path=db, force=True, now=NOW + 600, **a), 2, "could not be downloaded")
        assert rows(local, "open_runs") == before


def test_two_writers_and_the_guards():
    api = FakeApi()
    with place() as (a, _), place() as (b, _):
        _site(a)
        with quiet():
            first = S.push(filled(), api, seed=True, now=NOW, **a)["version"]
        refused(lambda: S.push(filled(), api, seed=True, now=NOW, **a), 3, "already holds a state")
        conn_a, conn_b = store.connect(":memory:"), store.connect(":memory:")
        with quiet():
            S.pull(conn_a, api, db_path=":memory:", now=NOW + 10, **a)
            S.pull(conn_b, api, db_path=":memory:", now=NOW + 10, **b)
        # nothing changed: nothing is sent, and the run counts as saved
        calls = dict(api.calls)
        with quiet():
            got = S.push(conn_b, api, now=NOW + 20, **b)
        assert got == {"version": first, "changed": False, "lists": []}
        assert api.calls["PUT"] == calls.get("PUT", 0) and api.calls["POST"] == calls.get("POST", 0)
        assert json.loads(read(os.path.join(b["job_dir"], "state_pushed.json"))) == \
            {"ok": True, "version": first, "changed": False, "at": NOW + 20}
        # b saves a change; a, still on its pull of the first version, is refused and the pointer stays
        change(conn_b, "acme")
        with quiet():
            second = S.push(conn_b, api, now=NOW + 30, **b)
        assert second["changed"] and second["version"] != first and second["lists"] == []
        change(conn_a, "other")
        refused(lambda: S.push(conn_a, api, now=NOW + 40, **a), 3, "changed since this run's pull")
        assert blobs.head(api, S.STATE)["version"] == second["version"]
        # pushing again from b works (its push counts as its new pull)
        change(conn_b, "acme bid")
        with quiet():
            third = S.push(conn_b, api, now=NOW + 60, **b)
        assert third["changed"] and blobs.head(api, S.STATE)["version"] == third["version"]
        # a's way back: --onto-head saves its store over whatever is saved now
        with quiet():
            back = S.push(conn_a, api, onto_head=True, now=NOW + 70, **a)
        assert back["changed"] and blobs.head(api, S.STATE)["version"] == back["version"]
        fresh = store.connect(":memory:")
        with quiet():
            S.pull(fresh, api, db_path=":memory:", now=NOW + 80, **b)
        assert rows(fresh, "open_filings") == rows(conn_a, "open_filings")
        # a race: the pointer moves between the check and the commit; nothing changes
        change(fresh, "late")
        api.fail_next(409, "moved", method="POST")
        refused(lambda: S.push(fresh, api, now=NOW + 95, **b), 3, "changed since this run's pull")
        assert blobs.head(api, S.STATE)["version"] == back["version"]


def test_lists_are_sent_only_when_they_changed():
    api = FakeApi()
    with place() as (a, _):
        _site(a)
        conn = filled()
        with quiet():
            S.push(conn, api, seed=True, now=NOW, **a)
            S.pull(conn, api, db_path=":memory:", now=NOW + 1, **a)
        puts = api.calls["PUT"]
        _site(a, generated="2026-11-15T04:20:00+00:00")                 # closed.json changed, open.json did not
        with quiet():
            got = S.push(conn, api, by="github", run_url="https://run/1", now=NOW + 2, **a)
        assert not got["changed"] and got["lists"] == ["closed.json"] and api.calls["PUT"] == puts + 1
        assert blobs.head(api, "site:closed.json")["meta"] == {"by": "github", "made_at": iso(NOW + 2),
                                                         "run_url": "https://run/1"}
        assert json.loads(blobs.get(api, "site:closed.json")[1])["generated_at"].startswith("2026-11-15")
        # a list the database refuses fails the push after the state was saved
        change(conn, "acme")
        _site(a, generated="2026-11-16T04:20:00+00:00")
        api.fail_next(400, "bad_request", method="POST", path="site:closed.json")
        refused(lambda: S.push(conn, api, now=NOW + 4, **a), 3, "the state was saved but the lists were not")
        assert json.loads(read(os.path.join(a["job_dir"], "state_pushed.json")))["ok"]
        fresh = store.connect(":memory:")
        with quiet():
            S.pull(fresh, api, db_path=":memory:", now=NOW + 5, **a)
        assert rows(fresh, "open_filings") == rows(conn, "open_filings")


def test_the_head_says_what_the_latest_open_day_left():
    # the saved state's head carries what the latest open-list day left unread (.meta.open_left: the catch-up's
    # check reads it with one request)
    api = FakeApi()
    with place() as (a, tmp):
        _site(a)
        db = os.path.join(tmp, "w.db")
        conn = filled(store.connect(db))
        store.save_open_day(conn, "2026-11-14", "updated", NOW, [], version="open-step-1",
                            info={"left_for_next_run": {"steps 1": 2, "names": 4}, "step_failed": ["acme", "x"],
                                  "step_failed_busy": ["acme"]})
        assert S.open_left(conn) == {"day": "2026-11-14", "unread": 3}
        assert S.open_left(store.connect(":memory:")) is None
        with quiet():
            S.push(conn, api, seed=True, now=NOW, **a)
            S.pull(conn, api, db_path=db, now=NOW + 1, **a)
        assert blobs.head(api, S.STATE)["meta"]["open_left"] == {"day": "2026-11-14", "unread": 3}


def test_prune():
    conn = filled()
    old = NOW - 70 * DAY
    store.save_open_answer(conn, "jane roe", "old", "open-out-1", 1, {"x": 1}, created_at=old)
    store.save_open_answer(conn, "jane roe", "recent", "open-out-1", 1, {"x": 1}, created_at=NOW - 30 * DAY)
    with store.transaction(conn):
        conn.execute("INSERT INTO closed_checks VALUES ('0xold', 'v', '{}', ?)", (NOW - 50 * DAY,))
    for day, status in (("2026-10-20", "updated"), ("2026-10-21", "not updated today")):
        store.save_open_day(conn, day, status, to_epoch(f"{day}T04:00:00Z"), [],
                            info={"awaiting": [1], "seen_step_headlines": ["a"], "held_back_stories": ["b"],
                                  "new_today": 2})
    assert S.prune(conn, NOW, dry_run=True) == {"open_answers": 1, "closed_checks": 1, "open_runs notes": 2}
    assert len(rows(conn, "open_answers")) == 3                          # a dry run removes nothing
    assert S.prune(conn, NOW) == {"open_answers": 1, "closed_checks": 1, "open_runs notes": 2}
    assert {r["headlines_sha1"] for r in rows(conn, "open_answers")} == {"sha1", "recent"}
    assert {r["condition_id"] for r in rows(conn, "closed_checks")} == {"0xabc"}
    infos = {r["day"]: json.loads(r["info"]) for r in rows(conn, "open_runs")}
    assert infos["2026-10-20"] == {"new_today": 2} and infos["2026-10-21"] == {"new_today": 2}
    assert infos["2026-11-13"]["seen_step_headlines"] == ["a"]          # a recent day keeps its notes
    assert S.prune(conn, NOW) == {"open_answers": 0, "closed_checks": 0, "open_runs notes": 0}
    # the latest updated day keeps its notes however old it is (the next run reads its seen headlines)
    lone = store.connect(":memory:")
    store.save_open_day(lone, "2026-10-01", "updated", to_epoch("2026-10-01T04:00:00Z"), [],
                        info={"seen_step_headlines": ["a"]})
    assert S.prune(lone, NOW)["open_runs notes"] == 0
    # a file that would grow too big loses readings sooner, but never inside the open list's 22-day look back
    big = store.connect(":memory:")
    for d in (40, 30, 24, 22, 1):
        store.save_open_answer(big, "s", f"d{d}", "open-step-1", 1, {"pad": "x" * 1000}, created_at=NOW - d * DAY)
    S.prune(big, NOW, max_answer_bytes=10)
    assert {r["headlines_sha1"] for r in rows(big, "open_answers")} == {"d22", "d1"}


def test_prune_leaves_the_next_days_cards_as_they_were():
    # two days of the open list's whole-run fixture, plus rows prune removes (old readings, an old closed
    # check, an old day's notes); the third day's list is the same from the pruned store as from the full one
    T = importlib.import_module("test_watchlist_opennow")
    O = T.O
    outs = []
    with tempfile.TemporaryDirectory() as tmp:
        for pruned in (False, True):
            conn = store.connect(":memory:")
            out = os.path.join(tmp, f"{pruned}.json")
            with quiet():
                T._run(conn, T.NOW, T.FakeClient(), out)
                T._run(conn, T.NOW + DAY, T.FakeClient(), out)
            for k in (T.ROE_K, T.ACME_K):
                store.save_open_answer(conn, k, "ancient", O.PROMPT_VERSION, 1,
                                       {"steps": [], "given": [], "ident": [], "read_at": T.NOW - 70 * DAY},
                                       created_at=T.NOW - 70 * DAY)
            with store.transaction(conn):
                conn.execute("INSERT INTO closed_checks VALUES ('0xold', 'v', '{}', ?)", (T.NOW - 50 * DAY,))
            store.save_open_day(conn, "2026-10-01", "updated", to_epoch("2026-10-01T04:00:00Z"), [],
                                info={"seen_step_headlines": ["x"], "awaiting": [{"n": 1}]})
            if pruned:
                got = S.prune(conn, T.NOW + 2 * DAY)
                assert got == {"open_answers": 2, "closed_checks": 1, "open_runs notes": 1}, got
                S.prune(conn, T.NOW + 2 * DAY, answers_days=S.ANSWERS_FLOOR_DAYS)    # the shortest it may go
            with quiet():
                s = T._run(conn, T.NOW + 2 * DAY, T.FakeClient(), out)
            doc = json.load(open(out))
            outs.append((doc["days"][0]["cards"], s["held_back"], s["picks"]))
    assert outs[0] == outs[1]
    assert outs[0][0], "the fixture's third day has cards"


if __name__ == "__main__":
    checks = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = []
    for fn in checks:
        try:
            fn()
            print(f"PASS {fn.__name__}", flush=True)
        except Exception as e:
            failed.append(fn.__name__)
            import traceback
            traceback.print_exc()
            print(f"FAIL {fn.__name__}: {e}", flush=True)
    print(f"\n{len(checks) - len(failed)}/{len(checks)} checks passed")
    sys.exit(1 if failed else 0)
