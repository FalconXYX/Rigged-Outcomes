// The stack's Vercel: api/step.py served by backend/dev_server.py from the stack's own workspace, Fluid-like (calls
// share instance processes, a new instance when the live ones are full), every call stopped at 300 s, and the
// harness's switches (--control): a fault answered to the Worker's wake-ups instead of invoking anything (503
// DEPLOYMENT_PAUSED, say), lost wake-ups, every instance killed at once (a step killed part way), the per-call
// limit changed, and the log of every call. The calls get the stack's settings only: the Worker's local address
// and job token, the wake-up secret, a stand-in Gemini key (py/sitecustomize.py answers Gemini), their /tmp
// folder in the state folder.

import { join } from "node:path";
import { cleanEnv, startProcess, until } from "./proc.mjs";

export const WAKE_SECRET = "e2e-wake-secret";

export class LocalVercel {
  /** snap: the workspace it runs from; env: what the calls see on top of a clean environment. */
  constructor({ snap, port, env, logPath, procs, maxS = 300, concurrency = 4, idleS = 120 }) {
    Object.assign(this, { snap, port, env, logPath, procs, maxS, concurrency, idleS });
    this.url = `http://127.0.0.1:${port}`;
    this.stepUrl = `${this.url}/api/step`;
  }

  async start() {
    const args = ["-m", "backend.dev_server", "--port", String(this.port), "--max-s", String(this.maxS), "--concurrency", String(this.concurrency), "--idle-s", String(this.idleS), "--control"];
    this.proc = startProcess("python3", args, { cwd: this.snap, env: cleanEnv(this.env), logPath: this.logPath, procs: this.procs });
    await until(
      async () => {
        if (this.proc.child.exitCode !== null) throw new Error(`the step server exited (log: ${this.logPath})`);
        return fetch(`${this.url}/__dev/calls`).then((r) => r.ok, () => false);
      },
      { ms: 30_000, every: 200, what: `the step server on ${this.url}` },
    );
    return this;
  }

  async control(method, path, body) {
    const res = await fetch(`${this.url}/__dev/${path}`, { method, body: body ? JSON.stringify(body) : undefined, headers: body ? { "content-type": "application/json" } : {} });
    return res.json();
  }

  /** Answer the wake-ups so instead of invoking: {status: 503, error: "DEPLOYMENT_PAUSED", count}, {drop: n} (lost), or {} (none). */
  fault(spec = {}) {
    return this.control("POST", "fault", spec);
  }

  /** Kill every instance now, with the calls in them ({killed, calls}). */
  kill() {
    return this.control("POST", "kill");
  }

  /** The per-call limit, in seconds. */
  maxSeconds(s) {
    return this.control("POST", "max", { max_s: s });
  }

  /** {calls: [{at, lane, pid, cold, s, status, jobs}], instances, fault, max_s}. */
  state() {
    return this.control("GET", "calls");
  }

  async calls() {
    return (await this.state()).calls;
  }

  stop() {
    return this.proc?.stop();
  }
}
