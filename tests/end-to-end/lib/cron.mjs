// The stack's Cron Trigger: Cloudflare runs the Worker's `* * * * *` at the start of every minute; wrangler dev runs
// none by itself, so this fires it the same way through wrangler's local scheduled path (`wrangler dev
// --test-scheduled`, GET /cdn-cgi/handler/scheduled). The Worker decides what is due in Toronto time on its own
// clock (moved with POST /api/dev/clock when a scenario jumps ahead), as in production. tick() fires one now.

import { sleep } from "./proc.mjs";

export const SCHEDULED = "/cdn-cgi/handler/scheduled?cron=*+*+*+*+*";

export class Cron {
  constructor({ api, onError = () => {} }) {
    this.api = api;
    this.onError = onError;
    this.ticks = 0;
    this.running = false;
  }

  /** Fire the trigger once; resolves when the scheduled handler has finished (its wake-ups go on in waitUntil). */
  async tick() {
    const res = await fetch(`${this.api}${SCHEDULED}`);
    await res.text();
    if (!res.ok) throw new Error(`the scheduled handler answered ${res.status}`);
    this.ticks++;
  }

  /** Every minute on the minute, as Cloudflare does, until stop(). */
  start() {
    if (this.running) return this;
    this.running = true;
    this.loop = (async () => {
      while (this.running) {
        await sleep(60_000 - (Date.now() % 60_000));
        if (!this.running) break;
        await this.tick().catch((e) => this.onError(e));
      }
    })();
    return this;
  }

  /** Stop firing (a scenario's "the cron misses its ticks"); start() resumes. */
  async stop() {
    this.running = false;
  }
}
