#!/usr/bin/env python3
"""
Offline tests for the daily job's state on the watchlist-data branch
(watchlist.state): no network. Every git step runs in a throwaway repository
with a bare "remote" under a temporary directory, never in this checkout.

    python3 tests/test_watchlist_state.py

Covers the dump and load round trip (identical rows, identical bytes when
dumped twice), the manifest check (a wrong sha256 or row count, a missing
table, an empty state), unknown columns, pull (a missing branch, a local
store with newer data, --force and its backup, --rev, the published lists
copied), push (a seed makes an orphan commit and no local branch, a branch
that moved since the pull, nothing changed, the API backup kept or
replaced, pushing again from the same checkout, --onto-head), and prune (what it removes, and that the next day's cards are the
same with or without it).
"""
import contextlib
import importlib
import io
import json
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from watchlist import state as S, store  # noqa: E402
from watchlist.common import DAY, to_epoch  # noqa: E402

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
    with contextlib.redirect_stdout(io.StringIO()):
        yield


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


# ── git: a throwaway repository and its bare remote ──────────────────────────

def _git(repo, *args):
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, check=True).stdout.strip()


@contextlib.contextmanager
def repos(n=1):
    """n clones of a bare remote that holds one commit on main; yields (tmp, [clone paths])."""
    with tempfile.TemporaryDirectory() as tmp:
        bare = os.path.join(tmp, "origin.git")
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", bare], check=True)
        seed = os.path.join(tmp, "seed")
        subprocess.run(["git", "init", "-q", "-b", "main", seed], check=True)
        _git(seed, "config", "user.email", "test@example.com")
        _git(seed, "config", "user.name", "Test")
        with open(os.path.join(seed, "README.md"), "w") as f:
            f.write("main\n")
        _git(seed, "add", "README.md")
        _git(seed, "commit", "-q", "-m", "main")
        _git(seed, "remote", "add", "origin", bare)
        _git(seed, "push", "-q", "origin", "main")
        out = []
        for i in range(n):
            c = os.path.join(tmp, f"clone{i}")
            subprocess.run(["git", "clone", "-q", bare, c], check=True, capture_output=True)
            _git(c, "config", "user.email", "test@example.com")
            _git(c, "config", "user.name", "Test")
            for d in ("site", "job"):                     # beside the clone, as gitignored folders are in the repo
                os.makedirs(f"{c}-{d}")
            out.append(c)
        yield tmp, out


def _site(clone, generated="2026-11-14T04:20:00+00:00"):
    site = f"{clone}-site"
    with open(os.path.join(site, "closed.json"), "w") as f:
        json.dump({"generated_at": generated, "days": []}, f)
    with open(os.path.join(site, "open.json"), "w") as f:
        json.dump({"days": [{"day": "2026-11-13", "run_at": "2026-11-13T04:00:00Z", "cards": []}]}, f)
    return site


def _kw(clone):
    return {"repo": clone, "site_dir": f"{clone}-site", "job_dir": f"{clone}-job"}


def test_pull_refuses_a_missing_branch_and_seed_makes_an_orphan():
    with repos() as (tmp, [c]):
        try:
            S.pull(store.connect(":memory:"), db_path=":memory:", **_kw(c))
            raise AssertionError("pulled a missing branch")
        except S.StateError as e:
            assert e.code == 2 and "No saved state" in str(e) and "--seed" in str(e)
        _site(c)
        with quiet():
            got = S.push(filled(), seed=True, by="mac", now=NOW, **_kw(c))
        assert got["pushed"] and got["changed"]
        head = S.remote_head(c, "origin", S.BRANCH)
        assert head == got["sha"]
        assert _git(c, "rev-list", "--parents", "-n", "1", head) == head           # no parent: an orphan
        assert _git(c, "branch", "--list", S.BRANCH) == ""                          # no local branch left
        assert _git(c, "status", "--porcelain") == ""
        files = set(_git(c, "ls-tree", "-r", "--name-only", head).splitlines())
        assert files == {"README.md", "site/closed.json", "site/open.json", "state/manifest.json"} | \
            {f"state/{t}.jsonl" for t in S.TABLES}
        pushed = json.load(open(os.path.join(f"{c}-job", "state_pushed.json")))
        assert pushed == {"ok": True, "sha": head, "at": NOW}
        m = json.loads(_git(c, "show", f"{head}:state/manifest.json"))
        assert m["by"] == "mac" and m["open_day"] == "2026-11-13" and m["closed_generated_at"].startswith("2026-11-14")
        # a second seed is refused
        try:
            with quiet():
                S.push(filled(), seed=True, now=NOW, **_kw(c))
            raise AssertionError("seeded twice")
        except S.StateError as e:
            assert e.code == 3 and "already exists" in str(e)


def test_pull_push_and_their_guards():
    with repos(2) as (tmp, [a, b]):
        _site(a)
        src = filled()
        with quiet():
            first = S.push(src, seed=True, now=NOW, **_kw(a))["sha"]
        # b: a fresh runner pulls the state and the published lists
        conn = store.connect(":memory:")
        with quiet():
            S.pull(conn, db_path=":memory:", now=NOW + 60, **_kw(b))
        assert all(rows(conn, t) == rows(src, t) for t in S.TABLES)
        assert json.load(open(os.path.join(f"{b}-site", "closed.json")))["generated_at"].startswith("2026-11-14")
        assert json.load(open(os.path.join(f"{b}-job", "state_pulled.json")))["sha"] == first
        assert _git(b, "rev-parse", "--is-shallow-repository") == "false"     # a full repository stays full
        # nothing changed: no commit, but the run counts as pushed
        with quiet():
            got = S.push(conn, now=NOW + 120, **_kw(b))
        assert got == {"sha": first, "changed": False, "pushed": False}
        assert json.load(open(os.path.join(f"{b}-job", "state_pushed.json")))["at"] == NOW + 120
        assert S.remote_head(b, "origin", S.BRANCH) == first
        # a decision, and the API's export: a new commit on top, the backup written
        store.save_review(conn, "acme", "acme talks", "not a step", {"title": "Acme talks"}, at=NOW + 100)
        with quiet():
            second = S.push(conn, now=NOW + 180, export={"markers": [], "exported_at": 1}, **_kw(b))["sha"]
        assert _git(b, "rev-parse", f"{second}^") == first
        assert json.loads(_git(b, "show", f"{second}:backup/d1.json")) == {"markers": [], "exported_at": 1}
        # a in the meantime still holds its pull of the first commit: the branch moved, nothing is pushed
        with open(os.path.join(f"{a}-job", "state_pulled.json"), "w") as f:
            json.dump({"sha": first, "at": NOW}, f)
        try:
            with quiet():
                S.push(filled(), now=NOW + 200, **_kw(a))
            raise AssertionError("pushed over a moved branch")
        except S.StateError as e:
            assert e.code == 3 and "moved since this run's pull" in str(e)
        assert S.remote_head(a, "origin", S.BRANCH) == second
        # a new day without the API: the backup stays as it was
        store.save_review(conn, "acme", "acme bid", "step", {"title": "Acme bid"}, at=NOW + 300)
        with quiet():
            S.pull(conn, db_path=":memory:", force=True, now=NOW + 310, **_kw(b))
            store.save_review(conn, "acme", "acme bid", "step", {"title": "Acme bid"}, at=NOW + 300)
            third = S.push(conn, now=NOW + 320, **_kw(b))["sha"]
        assert _git(b, "show", f"{third}:backup/d1.json") == _git(b, "show", f"{second}:backup/d1.json")

        # the guard: a local store with something newer than the branch is not replaced without --force
        with open(os.path.join(f"{a}-job", "state_pulled.json"), "w") as f:
            f.write("{}")
        db = os.path.join(tmp, "local.db")
        local = filled(store.connect(db))
        store.save_review(local, "late", "late headline", "step", {"title": "late headline"}, at=NOW + 10_000)
        try:
            with quiet():
                S.pull(local, db_path=db, now=NOW + 400, **_kw(a))
            raise AssertionError("replaced newer local data")
        except S.StateError as e:
            assert e.code == 2 and "open_reviews.decided_at" in str(e) and "--force" in str(e)
        assert any(r["story"] == "late" for r in rows(local, "open_reviews"))
        for i in range(4):                                       # --force backs the store up, keeping 3 copies
            with quiet():
                S.pull(local, db_path=db, force=True, now=NOW + 500 + i, **_kw(a))
        assert not any(r["story"] == "late" for r in rows(local, "open_reviews"))
        backups = sorted(x for x in os.listdir(tmp) if x.startswith("local.db.bak-"))
        assert len(backups) == S.BACKUPS_KEPT, backups
        # --rev pulls an older commit
        with quiet():
            S.pull(local, db_path=db, force=True, rev=first, now=NOW + 600, **_kw(a))
        assert rows(local, "open_reviews") == rows(src, "open_reviews")
        assert json.load(open(os.path.join(f"{a}-job", "state_pulled.json")))["sha"] == first


def test_pushing_again_from_one_checkout_and_the_way_back():
    """A push that went through (a seed included) or found nothing to change counts as this checkout's new pull,
    so a later push from it works; an older commit pulled with --rev is put back on top with --onto-head."""
    with repos() as (tmp, [c]):
        _site(c)
        conn = filled()
        with quiet():
            first = S.push(conn, seed=True, now=NOW, **_kw(c))["sha"]
            store.save_review(conn, "acme", "acme talks", "step", {"title": "Acme talks"}, at=NOW + 10)
            second = S.push(conn, now=NOW + 20, **_kw(c))["sha"]
            store.save_review(conn, "acme", "acme bid", "step", {"title": "Acme bid"}, at=NOW + 30)
            third = S.push(conn, now=NOW + 40, **_kw(c))["sha"]
            unchanged = S.push(conn, now=NOW + 50, **_kw(c))
        assert _git(c, "rev-parse", f"{second}^") == first and _git(c, "rev-parse", f"{third}^") == second
        assert unchanged == {"sha": third, "changed": False, "pushed": False}
        pulled = json.load(open(os.path.join(f"{c}-job", "state_pulled.json")))
        made = json.loads(_git(c, "show", f"{third}:state/manifest.json"))["made_at"]
        assert pulled == {"sha": third, "made_at": made, "at": NOW + 50}
        with quiet():                                            # the store matches the branch: a pull is fine
            S.pull(conn, db_path=":memory:", now=NOW + 60, **_kw(c))

        # the way back from a bad head: pull an older commit, then put it on top of the current head
        older = store.connect(":memory:")
        with quiet():
            S.pull(older, db_path=":memory:", rev=first, force=True, now=NOW + 70, **_kw(c))
        try:
            with quiet():
                S.push(older, now=NOW + 80, **_kw(c))
            raise AssertionError("pushed an older pull without --onto-head")
        except S.StateError as e:
            assert e.code == 3 and "moved" in str(e)
        with quiet():
            back = S.push(older, onto_head=True, now=NOW + 90, **_kw(c))
        assert back["pushed"] and _git(c, "rev-parse", f"{back['sha']}^") == third
        assert S.remote_head(c, "origin", S.BRANCH) == back["sha"]
        fresh = store.connect(":memory:")
        with quiet():
            S.pull(fresh, db_path=":memory:", now=NOW + 100, **_kw(c))
        assert rows(fresh, "open_reviews") == rows(filled(), "open_reviews")


def test_push_never_touches_the_working_tree():
    with repos() as (tmp, [c]):
        _site(c)
        before = (_git(c, "rev-parse", "HEAD"), _git(c, "status", "--porcelain"),
                  _git(c, "branch", "--list"), _git(c, "worktree", "list"))
        with quiet():
            S.push(filled(), seed=True, now=NOW, **_kw(c))
        after = (_git(c, "rev-parse", "HEAD"), _git(c, "status", "--porcelain"),
                 _git(c, "branch", "--list"), _git(c, "worktree", "list"))
        assert before == after
        # --no-push makes the commit and leaves the branch as it was
        conn = store.connect(":memory:")
        with quiet():
            S.pull(conn, db_path=":memory:", now=NOW + 1, **_kw(c))
            store.save_review(conn, "x", "y", "step", {"title": "y"}, at=NOW + 2)
            got = S.push(conn, no_push=True, now=NOW + 3, **_kw(c))
        assert got["changed"] and not got["pushed"] and S.remote_head(c, "origin", S.BRANCH) != got["sha"]
        assert _git(c, "rev-parse", f"{got['sha']}^") == S.remote_head(c, "origin", S.BRANCH)
        assert (_git(c, "rev-parse", "HEAD"), _git(c, "status", "--porcelain")) == after[:2]


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
