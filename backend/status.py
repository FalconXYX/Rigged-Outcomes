"""
The backend at a glance, read only (docs/DEPLOY.md): the 30-day budget, the latest runs, the failed steps of the
last 14 days and the latest run reports, from the Worker's GET /api/job/status. It reads WATCHLIST_API_URL and
WATCHLIST_JOB_TOKEN as the job does (the environment, then ../.env). Times are Toronto's.

    python3 -m backend.status [--json]
"""
import argparse
import json
import sys

from backend import contract as C
from watchlist import sync


def _at(epoch):
    return C.toronto(epoch).strftime("%b %d %H:%M") if epoch else "-"


def text(doc):
    """The status as lines of plain text."""
    b, B = doc.get("budget") or {}, C.BUDGET
    used = max((b.get("cpu_ms_30d") or 0) / B["cpu_ms_month"], (b.get("gb_ms_30d") or 0) / B["gb_ms_month"],
               (b.get("invocations_30d") or 0) / B["invocations_month"])
    lines = [f"Budget: {b.get('level', 'ok')}, {used:.0%} of Vercel's 30-day allowance used "
             f"(work slows at {B['level_at']['tight']:.0%}, adds wait at {B['level_at']['low']:.0%}, "
             f"everything stops at {B['level_at']['stop']:.0%})", "", "Runs:"]
    lines += [f"  {_at(r.get('created_at'))}  {r.get('state', ''):<9} {r.get('id')}" for r in doc.get("runs") or []] \
        or ["  none"]
    lines += ["", "Failed steps (last 14 days):"]
    first = lambda e: ((e or "").splitlines() or [""])[0][:160]  # noqa: E731
    lines += [f"  {_at(j.get('updated_at'))}  {j.get('kind')}: {first(j.get('error'))}"
              for j in doc.get("failed_jobs") or []] or ["  none"]
    lines += ["", "Reports:"]
    lines += [f"  {_at(r.get('finished_at'))}  {r.get('status')}: {r.get('summary') or ''}"
              for r in doc.get("reports") or []] or ["  none"]
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description="The backend's runs, failed steps and budget.")
    ap.add_argument("--json", action="store_true", help="print the Worker's answer as it is")
    args = ap.parse_args(argv)
    c = sync.client()
    if c is None:
        print(sync.NO_API, file=sys.stderr)
        return 1
    doc = c.request("GET", "/api/job/status")
    print(json.dumps(doc, indent=1) if args.json else text(doc))
    return 0


if __name__ == "__main__":
    sys.exit(main())
