#!/usr/bin/env python3
"""
The contract both sides share, and the wake-up's signature (docs/BACKEND.md). No network.

    python3 tests/test_backend_contract.py

Covers: worker/src/contract.ts's CONTRACT (the JSON between `export const CONTRACT = ` and ` as const;`) equal
to backend.contract.CONTRACT; backend.wake's copies of the lanes and the skew; the signature's test vector; a
header refused for each way it can be wrong; api/step.py answering a bad header 401 {} without importing the
runtime or pandas; the outcomes' deadline helpers; and the next 04:00 in Toronto across a change of clocks.
"""
import json
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from backend import contract, outcomes, wake  # noqa: E402

SECRET = "test-wake-secret"
VECTOR = (1795078800, "main", "0123456789abcdef", "20ef9719fc110b70bbb7dd2f327e5b4e0c3d07117b936549707287c366a7304a")


def ts_contract():
    with open(os.path.join(ROOT, "worker", "src", "contract.ts"), encoding="utf-8") as f:
        text = f.read()
    start = text.index("export const CONTRACT = ") + len("export const CONTRACT = ")
    return json.loads(text[start:text.index(" as const;", start)])


def test_the_two_contracts_are_the_same():
    assert ts_contract() == contract.CONTRACT
    assert contract.BACKGROUND_KINDS and contract.BACKGROUND_KINDS <= set(contract.JOB_KINDS)
    assert wake.LANES == tuple(contract.CONTRACT["lanes"])
    assert wake.MAX_SKEW_S == contract.WAKE["max_skew_s"] and wake.SCHEME == contract.WAKE["scheme"]
    for kind, spec in contract.JOB_KINDS.items():
        if spec["handler"].startswith("backend.add_steps:"):
            from backend import add_steps
            assert callable(getattr(add_steps, contract.handler_path(kind)[1])), kind


def test_signature_vector():
    ts, lane, nonce, sig = VECTOR
    assert wake.signature(SECRET, ts, lane, nonce) == sig
    assert wake.sign(SECRET, lane, ts=ts, nonce=nonce) == f"v1,{ts},{lane},{nonce},{sig}"
    assert wake.verify(f"v1,{ts},{lane},{nonce},{sig}", SECRET, now=ts + 59) == "main"


def test_bad_headers_are_refused():
    ts, lane, nonce, sig = VECTOR
    good = f"v1,{ts},{lane},{nonce},{sig}"
    assert wake.verify(good, SECRET, now=ts) == "main"
    bad = [good.replace("v1,", "v2,"), good.replace(",main,", ",other,"), good[:-1] + ("0" if sig[-1] != "0" else "1"),
           good + ",x", "", None, good.upper(), f"v1,{ts},{lane},{nonce[:-1]},{sig}"]
    for h in bad:
        assert wake.verify(h, SECRET, now=ts) is None, h
    assert wake.verify(good, SECRET, now=ts + 61) is None and wake.verify(good, SECRET, now=ts - 61) is None
    assert wake.verify(good, "", now=ts) is None and wake.verify(good, "another", now=ts) is None
    signed = wake.sign(SECRET, "add")
    assert wake.verify(signed, SECRET) == "add"


def test_step_refuses_before_importing_anything_heavy():
    code = ("import sys, time; sys.path.insert(0, %r); import api.step as s; "
            "st, body = s.run('v1,1,main,0123456789abcdef,' + '0'*64, time.monotonic(), {'WAKE_SECRET': 'x'}); "
            "assert (st, body) == (401, {}), (st, body); "
            "heavy = [m for m in ('backend.runtime', 'pandas', 'numpy', 'requests', 'watchlist') if m in sys.modules]; "
            "assert not heavy, heavy; print('ok')") % ROOT
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0 and out.stdout.strip() == "ok", out.stderr


def test_outcomes_follow_the_deadline():
    assert outcomes.left() is None and outcomes.clamp(30) == 30
    token = outcomes.DEADLINE.set(time.monotonic() + 10)
    try:
        assert 9 < outcomes.left() <= 10 and outcomes.clamp(30) <= 10
        outcomes.check(5)
        for fn in (lambda: outcomes.check(20), lambda: outcomes.sleep(9)):
            try:
                fn()
                raise AssertionError("no OutOfTime")
            except outcomes.OutOfTime:
                pass
    finally:
        outcomes.DEADLINE.reset(token)
    t = outcomes.CLOCK.set(lambda: 1_000)
    try:
        e = outcomes.Later.after(120, "polymarket_down", pause=True)
        assert (e.run_after, e.reason, e.pause_lane_until) == (1_120, "polymarket_down", 1_120)
    finally:
        outcomes.CLOCK.reset(t)
    assert outcomes.per_job("x", dict, default="module") == "module"
    j = outcomes.JOB.set(outcomes.new_job_objects())
    try:
        a = outcomes.per_job("x", dict)
        assert a is outcomes.per_job("x", dict) and a == {}
    finally:
        outcomes.JOB.reset(j)


def test_next_daily_in_toronto():
    from backend.add_steps import next_daily
    # 2026-11-01: Toronto falls back from EDT to EST at 02:00
    before = contract.toronto_at("2026-10-31", "12:00")
    assert next_daily(before) == contract.toronto_at("2026-11-01", "04:00")
    assert contract.toronto(next_daily(before)).strftime("%H:%M %z") == "04:00 -0500"
    early = contract.toronto_at("2026-11-19", "03:59")
    assert next_daily(early) == contract.toronto_at("2026-11-19", "04:00")
    at = contract.toronto_at("2026-11-19", "04:00")
    assert next_daily(at) == contract.toronto_at("2026-11-20", "04:00")
    spring = contract.toronto_at("2027-03-13", "22:00")                  # springs forward 2027-03-14
    assert contract.toronto(next_daily(spring)).strftime("%Y-%m-%d %H:%M %z") == "2027-03-14 04:00 -0400"


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
