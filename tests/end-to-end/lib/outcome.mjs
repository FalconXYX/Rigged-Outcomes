// Waiting on an open page for what a person waits for (an add's "On the main page →"), and stopping as soon as the
// outcome is clearly wrong instead of at the timeout. It reads the page, and the add's run in the backend through
// the owner's overview and the job's run endpoint (neither is a visitor call, so it changes nothing the page asks
// about).

import { sleep } from "./proc.mjs";

/** How long a card may be off the page, or offer its button again, before that counts (a reload takes seconds). */
const GONE_MS = 30_000;
/**
 * The add's run finished this long ago and the page still has not changed: nothing more happens (an open page
 * asks the API every 15 s while an add is on its way, and reads the index past the cache once it is added).
 */
export const STUCK_MS = 150_000;

/**
 * Waits until `locator` shows, at most `ms`, asking `wrong()` every `every` ms meanwhile: when it names a clearly
 * wrong outcome (a sentence) the wait stops there. Resolves {shown: true} or {shown: false, why}.
 */
export async function showsUnless(locator, ms, wrong, every = 3000) {
  let over = false;
  const shown = locator.waitFor({ timeout: ms }).then(
    () => ({ shown: true }),
    () => ({ shown: false, why: `not within ${Math.round(ms / 1000)} s` }),
  );
  const watched = (async () => {
    while (!over) {
      await sleep(every);
      const why = over ? null : await wrong().catch(() => null);
      if (why) return { shown: false, why };
    }
    return shown;
  })();
  const got = await Promise.race([shown, watched]);
  over = true;
  return got;
}

/** One line on a run's failed or cancelled jobs, for a report. */
export function failedJobs(jobs = []) {
  return jobs
    .filter((j) => ["failed", "cancelled"].includes(j.state))
    .map((j) => `${j.kind}#${j.part} ${j.state}${j.error ? `: ${j.error}` : ""}`)
    .join("; ");
}

/**
 * The clearly wrong outcomes of an add from a Recently closed card (`card`, its locator) of condition id `cid`
 * clicked at `since` (ms), for showsUnless: the card off the page, or offering "Add to main page" again with
 * nothing on its way, for GONE_MS straight; the card saying the add failed, the market is no longer on the list or
 * it was taken off the main page; no add run STUCK_MS after the click; the add's run ending other than done; or
 * the run done STUCK_MS ago with the card unchanged.
 */
export function addGoesWrong(stack, card, since, cid) {
  let gone = null;
  let button = null;
  return async () => {
    const now = Date.now();
    const there = (await card.count()) > 0;
    gone = there ? null : (gone ?? now);
    if (gone !== null && now - gone >= GONE_MS) return `the card left the page (gone for ${Math.round((now - gone) / 1000)} s)`;
    if (there) {
      const face = (await card.locator(".cl-card-foot .wl-act").innerText({ timeout: 1500 })).replace(/\s+/g, " ").trim();
      if (/Couldn't add|no longer on the list|Taken off the main page/.test(face)) return `the card reads "${face}"`;
      button = /Add to main page/.test(face) && !/Adding/.test(face) ? (button ?? now) : null;
      if (button !== null && now - button >= GONE_MS) return `the card offers its button again ("${face}")`;
    }
    if (!cid) return null;
    const id = await stack.runOf(cid);
    if (!id) return now - since >= STUCK_MS ? `no add run was made in ${Math.round((now - since) / 1000)} s` : null;
    const got = await stack.run(id);
    if (!got) return null;
    const { run, jobs } = got;
    if (["failed", "partial", "cancelled", "skipped"].includes(run.state)) return `its run ${id} ended ${run.state} (${failedJobs(jobs)})`;
    if (run.state === "done" && run.finished_at && now - run.finished_at * 1000 >= STUCK_MS) {
      return `its run ${id} finished ${Math.round(now / 1000 - run.finished_at)} s ago and the card has not changed`;
    }
    return null;
  };
}
