#!/usr/bin/env python3
"""
Offline tests for the daily job's memory in the site's database
(watchlist.state) against the fake API (tests/fake_api.py). No network, and
no git: no repository is made, not even in a temporary directory.

    python3 tests/test_watchlist_state.py

Covers the dump and load round trip (identical rows, identical bytes when
dumped twice), the manifest check (a wrong sha256 or row count, a missing
table, an empty state), unknown columns, pull and push (no API, no saved
state, a push then a pull into a fresh store, a local store with newer data
and --force with its backup, a state saved by another run since the pull,
nothing changed and nothing sent, the lists sent only when they changed,
--seed and --onto-head, the news cache never failing a run, the head's
open_left and the open_list output the catch-up run reads), migrate (the
branch's bytes kept, a saved state refused or replaced with --force, files
that do not match their manifest), and prune (what it removes, and that the
next day's cards are the same with or without it).
"""
import contextlib
import importlib
import io
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fake_api import FakeApi  # noqa: E402
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
                        info={"awaiting": [{"n": 1}], "new_today": 1})
    store.save_review(conn, "jane roe", "white house weighs", "step", {"title": "White House weighs"}, at=NOW - 3600)
    store.set_meta(conn, "not state", {"x": 1})
    return conn


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
    assert counts["open_cards"] == 1 and counts["open_listed"] == 2 and sum(counts.values()) == 10
    for t in S.TABLES:
        assert rows(a, t) == rows(b, t), t
    assert S.dump_files(b, made_at=NOW) == files                         # and back to the same bytes
    assert store.get_meta(b, "not state") is None                        # the state leaves other tables alone
    m = json.loads(files["manifest.json"])
    assert m["latest"] == {"open_runs.run_at": NOW - DAY, "open_reviews.decided_at": NOW - 3600,
                           "closed_checks.created_at": rows(a, "closed_checks")[0]["created_at"]}
    assert m["format"] == 1 and m["tables"]["open_reviews"]["rows"] == 1
    # rows are in primary key order, keys sorted
    first = files["open_listed.jsonl"].decode().splitlines()
    assert first == sorted(first) and list(json.loads(first[0])) == sorted(json.loads(first[0]))
    with tempfile.TemporaryDirectory() as tmp:
        S.dump(a, tmp, made_at=NOW)
        c = store.connect(":memory:")
        S.load(c, tmp)
        assert all(rows(a, t) == rows(c, t) for t in S.TABLES)
        # a load replaces what the store had
        store.save_review(c, "other", "k", "not a step", {"title": "k"})
        S.load(c, tmp)
        assert rows(c, "open_reviews") == rows(a, "open_reviews")


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
    line = json.loads(files["open_reviews.jsonl"])
    line["new_column"] = "x"
    data = (json.dumps(line, sort_keys=True) + "\n").encode()
    m = json.loads(files["manifest.json"])
    m["tables"]["open_reviews"]["sha256"] = S._sha256(data)
    conn = store.connect(":memory:")
    with quiet():
        S.load_files(conn, dict(files, **{"open_reviews.jsonl": data, "manifest.json": json.dumps(m).encode()}))
    assert rows(conn, "open_reviews")[0]["decision"] == "step"


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
        refused(lambda: S.migrate(None, reader=lambda p: None), 3, "No API configured")
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
                assert S.main(["pull", "--db", db, "--site-dir", kw["site_dir"], "--news-cache",
                               os.path.join(tmp, "n.json")]) == 2
                assert S.main(["push", "--db", db, "--site-dir", kw["site_dir"], "--news-cache",
                               os.path.join(tmp, "n.json")]) == 3
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
        store.save_review(conn, "acme", "acme talks", "step", {"title": "Acme talks"}, at=NOW + 10)
        with quiet():
            first = S.push(conn, api, now=NOW + 20, **kw)
        assert first["changed"] and blobs.head(api, S.STATE)["version"] == first["version"]
        assert rows(conn, "open_reviews")[0]["story"] == "acme"
        # another place started fresh too, but the first state was saved meanwhile: it is refused, not overwritten
        conn2 = store.connect(":memory:")
        with quiet():
            S.pull(conn2, FakeApi(), db_path=":memory:", now=NOW, **other)          # its own empty database
        store.save_review(conn2, "other", "k", "step", {"title": "k"}, at=NOW + 30)
        e = refused(lambda: S.push(conn2, api, now=NOW + 40, **other), 3, "changed since this run's pull")
        assert e.kind == "outside" and blobs.head(api, S.STATE)["version"] == first["version"]


def test_a_used_database_without_its_state_is_refused():
    """A list, the news cache or a replaced state version is saved only after a state: holding one without a
    state means the state was lost or API_URL points at another database. Starting fresh there would record every
    first-entry 99c check again, so the pull refuses, as the setup's problem, and loads nothing."""
    for what, fill in (("list:closed", lambda api: blobs.put(api, "list:closed", b'{"days": []}', {"by": "mac"})),
                       ("cache:news", lambda api: blobs.put(api, "cache:news", b"{}", {"by": "mac"})),
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
        assert got["changed"] and got["lists"] == ["closed.json", "open.json"] and not got["news"]
        h = blobs.head(api, S.STATE)
        assert h["version"] == got["version"] and h["meta"]["by"] == "mac" and h["meta"]["open_day"] == "2026-11-13"
        assert h["meta"]["tables"]["open_reviews"]["rows"] == 1
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
        del api.blobs["list:open"]
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
        store.save_review(local, "late", "late headline", "step", {"title": "late headline"}, at=NOW + 10_000)
        refused(lambda: S.pull(local, api, db_path=db, now=NOW + 400, **a), 2, "open_reviews.decided_at")
        assert any(r["story"] == "late" for r in rows(local, "open_reviews"))
        for i in range(4):                                       # --force backs the store up, keeping 3 copies
            with quiet():
                S.pull(local, api, db_path=db, force=True, now=NOW + 500 + i, **a)
        assert rows(local, "open_reviews") == rows(src, "open_reviews")
        backups = sorted(x for x in os.listdir(tmp) if x.startswith("local.db.bak-"))
        assert len(backups) == S.BACKUPS_KEPT, backups
        # a state that does not match its manifest is refused before anything changes
        api.parts[("state", api.blobs["state"]["version"], 0)] = "AAAA"
        before = rows(local, "open_reviews")
        refused(lambda: S.pull(local, api, db_path=db, force=True, now=NOW + 600, **a), 2, "could not be downloaded")
        assert rows(local, "open_reviews") == before


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
        assert got == {"version": first, "changed": False, "lists": [], "news": False}
        assert api.calls["PUT"] == calls.get("PUT", 0) and api.calls["POST"] == calls.get("POST", 0)
        assert json.loads(read(os.path.join(b["job_dir"], "state_pushed.json"))) == \
            {"ok": True, "version": first, "changed": False, "at": NOW + 20}
        # b saves a decision; a, still on its pull of the first version, is refused and the pointer stays
        store.save_review(conn_b, "acme", "acme talks", "not a step", {"title": "Acme talks"}, at=NOW + 25)
        with quiet():
            second = S.push(conn_b, api, now=NOW + 30, **b)
        assert second["changed"] and second["version"] != first and second["lists"] == []
        store.save_review(conn_a, "other", "other talks", "step", {"title": "Other talks"}, at=NOW + 26)
        refused(lambda: S.push(conn_a, api, now=NOW + 40, **a), 3, "changed since this run's pull")
        assert blobs.head(api, S.STATE)["version"] == second["version"]
        # pushing again from b works (its push counts as its new pull)
        store.save_review(conn_b, "acme", "acme bid", "step", {"title": "Acme bid"}, at=NOW + 50)
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
        assert rows(fresh, "open_reviews") == rows(conn_a, "open_reviews")
        # a race: the pointer moves between the check and the commit; nothing changes
        store.save_review(fresh, "late", "k", "step", {"title": "k"}, at=NOW + 90)
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
        assert blobs.head(api, "list:closed")["meta"] == {"by": "github", "made_at": iso(NOW + 2),
                                                         "run_url": "https://run/1"}
        assert json.loads(blobs.get(api, "list:closed")[1])["generated_at"].startswith("2026-11-15")
        # a list the database refuses fails the push after the state was saved
        store.save_review(conn, "acme", "acme talks", "step", {"title": "Acme talks"}, at=NOW + 3)
        _site(a, generated="2026-11-16T04:20:00+00:00")
        api.fail_next(400, "bad_request", method="POST", path="list:closed")
        refused(lambda: S.push(conn, api, now=NOW + 4, **a), 3, "the state was saved but the lists were not")
        assert json.loads(read(os.path.join(a["job_dir"], "state_pushed.json")))["ok"]
        fresh = store.connect(":memory:")
        with quiet():
            S.pull(fresh, api, db_path=":memory:", now=NOW + 5, **a)
        assert rows(fresh, "open_reviews") == rows(conn, "open_reviews")


def test_the_catch_up_run_reads_the_head_and_deploys_only_a_changed_open_list():
    # the saved state's head carries what the latest open-list day left unread (the catch-up run's check reads
    # .meta.open_left with one request), and the push tells the job whether it sent open.json (the deploy waits on it)
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
        out = os.path.join(tmp, "github_output")
        old = (S.sync.client, S.push, os.environ.get("GITHUB_OUTPUT"))
        push = S.push
        S.sync.client = lambda *x, **k: api
        S.push = lambda *x, **k: push(*x, **dict(k, job_dir=a["job_dir"]))
        os.environ["GITHUB_OUTPUT"] = out
        args = ["push", "--db", db, "--site-dir", a["site_dir"], "--news-cache", os.path.join(tmp, "news.json")]
        try:
            # the catch-up read what was left, the list did not change: the state is saved, open.json is not sent
            store.save_open_day(conn, "2026-11-14", "updated", NOW, [], version="open-step-1", info={})
            with quiet():
                assert S.main(args) == 0
            assert read(out, "r").splitlines() == ["open_list=false"]
            assert blobs.head(api, S.STATE)["meta"]["open_left"] == {"day": "2026-11-14", "unread": 0}
            # it changed: sent, and the job deploys
            with open(os.path.join(a["site_dir"], "open.json"), "w") as f:
                json.dump({"days": [{"day": "2026-11-14", "run_at": "2026-11-14T04:17:00Z", "cards": [{}]}]}, f)
            with quiet():
                assert S.main(args) == 0
            assert read(out, "r").splitlines() == ["open_list=false", "open_list=true"]
        finally:
            S.sync.client, S.push = old[0], old[1]
            if old[2] is None:
                os.environ.pop("GITHUB_OUTPUT", None)
            else:
                os.environ["GITHUB_OUTPUT"] = old[2]


def test_the_news_cache_rides_along_and_never_fails_a_run():
    api = FakeApi(now=NOW + 100)
    with place() as (a, tmp), place() as (b, _):
        news = os.path.join(tmp, "news.json")
        with open(news, "w") as f:
            json.dump({"q": ["headline é"]}, f)
        _site(a)
        conn = filled()
        with quiet():
            got = S.push(conn, api, seed=True, news_cache=news, now=NOW, **a)
        assert got["news"] and blobs.get(api, S.NEWS)[1] == read(news)
        mine = os.path.join(tmp, "mine.json")
        with quiet():
            S.pull(store.connect(":memory:"), api, db_path=":memory:", news_cache=mine, now=NOW + 200, **b)
        assert read(mine) == read(news)
        # a local cache newer than the saved one is kept
        with open(mine, "w") as f:
            f.write("{}")
        os.utime(mine, (NOW + 500, NOW + 500))
        with quiet():
            S.pull(store.connect(":memory:"), api, db_path=":memory:", news_cache=mine, now=NOW + 600, **b)
        assert read(mine) == b"{}"
        # unchanged: not sent again; an API error on it only warns
        with quiet():
            S.pull(conn, api, db_path=":memory:", now=NOW + 700, **a)
            puts = api.calls["PUT"]
            assert not S.push(conn, api, news_cache=news, now=NOW + 710, **a)["news"] and api.calls["PUT"] == puts
        with open(news, "w") as f:
            f.write('{"q": []}')
        api.fail_next(503, "server", method="PUT", path="cache:news")
        with quiet() as out:
            got = S.push(conn, api, news_cache=news, now=NOW + 720, **a)
        assert not got["news"] and "news cache was not saved" in out.getvalue()
        api.fail_next(None, "network", method="GET", path="cache:news")
        os.remove(mine)
        with quiet() as out:
            S.pull(conn, api, db_path=":memory:", news_cache=mine, now=NOW + 730, **a)
        assert "news cache was not pulled" in out.getvalue() and not os.path.exists(mine)


def branch(conn=None, site=True):
    """The watchlist-data branch's files, as migrate's reader serves them."""
    files = {f"state/{k}": v for k, v in S.dump_files(conn or filled(), by="github", made_at=NOW,
                                                       run_url="https://run/9").items()}
    if site:
        files["site/closed.json"] = b'{"generated_at": "2026-11-14T04:20:00+00:00", "days": []}\n'
        files["site/open.json"] = b'{"days": []}\n'
    return files


def test_migrate_moves_the_branch_as_it_is():
    real_run = S.subprocess.run
    S.subprocess.run = lambda *a, **k: (_ for _ in ()).throw(AssertionError("git ran"))    # no git at all
    try:
        api = FakeApi()
        files = branch()
        with quiet():
            got = S.migrate(api, ref="origin/watchlist-data", reader=files.get)
        manifest = json.loads(files["state/manifest.json"])
        saved = saved_files(api)
        assert saved == {k[len("state/"):]: v for k, v in files.items() if k.startswith("state/")}
        for t in S.TABLES:
            assert S._sha256(saved[f"{t}.jsonl"]) == manifest["tables"][t]["sha256"]
        assert got["meta"] == dict(manifest, migrated_from="origin/watchlist-data")
        assert blobs.get(api, "list:closed")[1] == files["site/closed.json"]
        assert blobs.get(api, "list:open")[1] == files["site/open.json"]
        with place() as (kw, _):                              # and a run pulls it like any saved state
            conn = store.connect(":memory:")
            with quiet():
                S.pull(conn, api, db_path=":memory:", now=NOW, **kw)
            assert all(rows(conn, t) == rows(filled(), t) for t in S.TABLES)
            assert read(os.path.join(kw["site_dir"], "open.json")) == files["site/open.json"]
        # a database that holds a state is refused, then replaced with --force
        newer = filled()
        store.save_review(newer, "acme", "acme talks", "step", {"title": "Acme talks"}, at=NOW + 5)
        refused(lambda: S.migrate(api, reader=branch(newer).get), 3, "already holds a state")
        assert saved_files(api) == saved
        with quiet():
            S.migrate(api, reader=branch(newer, site=False).get, force=True)
        assert saved_files(api)["open_reviews.jsonl"] == branch(newer)["state/open_reviews.jsonl"]
        assert blobs.get(api, "list:open")[1] == files["site/open.json"]          # no lists: the old ones stay
        # files that do not match their manifest are refused, and nothing is sent
        bad = dict(files, **{"state/open_runs.jsonl": files["state/open_runs.jsonl"] + b"{}\n"})
        puts = api.calls["PUT"]
        refused(lambda: S.migrate(FakeApi(), reader=bad.get), 2, "sha256")
        refused(lambda: S.migrate(api, reader=bad.get, force=True), 2, "sha256")
        assert api.calls["PUT"] == puts
        # a state a run saved (not a migrate) may hold newer work than the branch: --force alone is refused
        with place() as (kw, _):
            conn = store.connect(":memory:")
            with quiet():
                S.pull(conn, api, db_path=":memory:", now=NOW, **kw)
                store.save_review(conn, "acme", "acme later", "step", {"title": "Later"}, at=NOW + 9)
                S.push(conn, api, by="mac", now=NOW + 10, **kw)
            run_saved, puts = saved_files(api), api.calls["PUT"]
            refused(lambda: S.migrate(api, reader=files.get, force=True), 3, "--replace-run-state")
            assert saved_files(api) == run_saved and api.calls["PUT"] == puts
            with quiet() as out:
                S.migrate(api, reader=files.get, force=True, replace_run_state=True)
            assert saved_files(api) == saved
            assert f"made {json.loads(files['state/manifest.json'])['made_at']} by github" in out.getvalue()
            assert blobs.versions(api, S.STATE)[0]["meta"]["by"] == "mac"           # the run's is kept, restorable
    finally:
        S.subprocess.run = real_run
    # --dir reads a folder holding state/ and site/
    with tempfile.TemporaryDirectory() as tmp:
        for path, data in files.items():
            os.makedirs(os.path.dirname(os.path.join(tmp, path)), exist_ok=True)
            with open(os.path.join(tmp, path), "wb") as f:
                f.write(data)
        api = FakeApi()
        with quiet():
            S.migrate(api, from_dir=tmp)
        assert saved_files(api) == saved and blobs.head(api, S.STATE)["meta"]["migrated_from"].startswith("dir ")


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
    assert infos["2026-11-13"]["awaiting"] == [{"n": 1}]                # a recent day keeps its notes
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
            outs.append((doc["days"][0]["cards"], s["awaiting"], s["picks"]))
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
