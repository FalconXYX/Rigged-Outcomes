"""
The backend's Python side (docs/BACKEND.md): the Vercel step function's runtime and the steps that add a market.

  contract   the constants the Worker shares (worker/src/contract.ts holds the same)
  wake       signing and checking a wake-up (standard library only: api/step.py checks one before anything else)
  outcomes   Later, OutOfTime, Lost and the step's deadline (standard library only)
  runtime    one Vercel call: claim, run the job's handler before the deadline, checkpoint, complete, claim again
  add_steps  add.fetch, add.whales and add.export: a market added to the main page in resumable steps
  site       what the Worker serves from D1: the lists, the added markets' pages and their index
  dev_server a local /api/step for the end-to-end harness, which stops any call at 300 s

Nothing here imports pandas at import time.
"""
