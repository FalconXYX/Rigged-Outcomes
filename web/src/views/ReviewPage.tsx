import { useCallback, useEffect, useRef, useState, type FormEvent, type ReactNode } from "react";
import {
  WORDS,
  apiConfigured,
  forgetOwner,
  ownerGet,
  ownerSend,
  ownerToken,
  setOwner,
  type AddRequest,
  type AdminMarker,
  type Backend,
  type BackendRun,
  type Budget,
  type BudgetLevel,
  type Candidate,
  type Decision,
  type DecisionValue,
  type JobRun,
  type LaneState,
  type Overview,
  type QueueItem,
  type RunKind,
  type RunState,
  type Sent,
} from "../lib/api";
import { loadIndex, useAsync } from "../lib/data";
import { fmtCalDay, fmtDayAt, fmtInt, fmtStamp, fmtT, torontoClock, torontoDay } from "../lib/format";
import "./review.css";

// The owner's review page (#/review, linked from nowhere). Unlocked with the
// owner token (kept in this page's memory only, so a reload asks again), it
// shows how the last daily run and the last apply run went, what the owner
// must fix (a job token the API refused, a wake-up Vercel didn't take), the
// pause switch for visitors' changes and "Apply decisions now"; then the
// backend (docs/BACKEND.md): Vercel's use over the last 30 days as the Worker
// last counted it, the lanes, the latest runs and the failed steps; then the
// open list's step headlines that wait for a decision, the closed markets sent
// to the main page (a read-only list), the markers visitors added and the
// site's watchlist. A decision is applied by the next daily run, or within
// minutes by "Apply decisions now" (an apply run). While it is in view the
// page asks for the overview again (every 15 s while something waits for a
// run, else every minute), so a run's result shows by itself. Every day and
// time is Toronto's. Plain on purpose, in the site's type and tokens.

/** a daily run older than this has missed one (they start once a day, at 04:00 Toronto time) */
const LATE_HOURS = 40;
/** daily runs in a row with a problem before the page says so (a problem that keeps coming back is worth a look) */
const TROUBLED_RUNS = 3;

/** the first problem a run's summary lists after "; problems: " (watchlist/problems.py summary_with) */
const firstProblem = (summary: string | null) => summary?.split("; problems: ")[1]?.split("; ")[0] ?? null;

/** "Nov 14": a calendar day ("2026-11-14"), or Toronto's day of an ISO stamp */
const day = (iso: string) => (iso.length === 10 ? fmtCalDay(iso) : fmtDayAt(iso));
/** "Nov 14, 04:31 ET" from epoch seconds */
const stampOf = (s: number) => fmtStamp(s * 1000);
const dayOf = (s: number) => fmtDayAt(s * 1000);
/** "04:31", or "Nov 14, 04:31 ET" when it isn't on Toronto's day `ref` (epoch seconds) */
const clockOn = (s: number, ref: number) => (torontoDay(s * 1000) === torontoDay(ref * 1000) ? torontoClock(s * 1000) : stampOf(s));
const dkey = (story: string, key: string) => `${story}\u0000${key}`;

/** how often the page in view asks for the overview again: while a decision, an add or a run waits (or a run was just queued), else */
const POLL_MS = 15_000;
const IDLE_POLL_MS = 60_000;
/** a run queued from this page counts as waited for this long (ms) */
const STARTED_FOR = 30 * 60_000;

/** The overview as compared between asks (its `now` changes every time). */
const sameAs = (a: Overview | null, b: Overview) => !!a && JSON.stringify({ ...a, now: 0 }) === JSON.stringify({ ...b, now: 0 });

const RUN_TERMINAL = new Set<RunState>(["done", "partial", "failed", "cancelled", "missed", "skipped"]);

/** What "Apply decisions now" said, kept across the page's refreshes: `at` (ms) the press; `started`: an apply run was queued (`run`). */
type ApplyNote = { at: number; text: string; started: boolean; run?: string | null };

/** An error from the API in words: the code's words, with the API's own message where it says more. */
const said = (r: Extract<Sent<unknown>, { ok: false }>) =>
  r.code === "unauthorized" || r.code === "locked" ? r.words : r.message || r.words;

export function ReviewPage() {
  return (
    <div className="container rv">
      <h1 className="rv-title">Review</h1>
      {apiConfigured ? <Review /> : <p className="rv-muted">No API in this build.</p>}
    </div>
  );
}

function Review() {
  // a token left in browser storage by an earlier build is removed (lib/api.ts keeps it in memory only)
  useEffect(() => {
    if (!ownerToken()) forgetOwner();
  }, []);
  const [unlocked, setUnlocked] = useState(() => !!ownerToken());
  const [data, setData] = useState<Overview | null>(null);
  // each new overview remounts the sections, whose state starts from it (so Reload shows what changed)
  const [loads, setLoads] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [note, setNote] = useState<ApplyNote | null>(null);
  const shown = useRef<{ data: Overview | null; note: ApplyNote | null }>({ data: null, note: null });
  shown.current = { data, note };

  /** Ask for the overview; `quiet` (the page's own refresh): only a refused token is shown, and an unchanged one changes nothing. */
  const load = useCallback(async (quiet = false) => {
    if (!quiet) setLoading(true);
    const r = await ownerGet<Overview>("/api/admin/overview");
    if (!quiet) setLoading(false);
    if (r.ok && r.data) {
      if (quiet && sameAs(shown.current.data, r.data)) return;
      setData(r.data);
      setLoads((n) => n + 1);
      setError(null);
      setUnlocked(true);
      return;
    }
    if (!r.ok && (r.code === "unauthorized" || r.code === "locked")) {
      forgetOwner();
      setUnlocked(false);
      setData(null);
    } else if (quiet) return;
    setError(r.ok ? WORDS.other : said(r));
  }, []);

  useEffect(() => {
    if (unlocked) load();
    // only on arrival: later loads follow an unlock, a decision or the refresh below
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // the page's own refresh while it is in view
  useEffect(() => {
    if (!unlocked) return;
    let asked = Date.now();
    const waits = () => {
      const { data: d, note: n } = shown.current;
      if (n?.started && Date.now() - n.at < STARTED_FOR) return true;
      return (
        !!d &&
        (d.decisions.some((x) => x.applied_at == null) ||
          d.requests.some((x) => x.status === "approved") ||
          (d.backend?.runs ?? []).some((x) => x.state === "queued" || x.state === "running"))
      );
    };
    const ask = (every: number) => {
      if (document.hidden || Date.now() - asked < every) return;
      asked = Date.now();
      void load(true);
    };
    const timer = window.setInterval(() => ask((waits() ? POLL_MS : IDLE_POLL_MS) - 1000), 5000);
    const back = () => ask(5000);
    document.addEventListener("visibilitychange", back);
    window.addEventListener("focus", back);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", back);
      window.removeEventListener("focus", back);
    };
  }, [unlocked, load]);

  if (!unlocked)
    return (
      <Unlock
        error={error}
        busy={loading}
        onUnlock={(token) => {
          setOwner(token);
          load();
        }}
      />
    );
  if (!data)
    return error ? (
      <p className="rv-line">
        <span className="rv-error">{error}</span> ·{" "}
        <button type="button" className="cl-link" disabled={loading} onClick={() => load()}>
          Try again
        </button>
      </p>
    ) : (
      <p className="rv-muted">Loading…</p>
    );
  return (
    <Signed
      key={loads}
      data={data}
      note={note}
      onNote={setNote}
      reload={() => load()}
      onForget={() => {
        forgetOwner();
        setUnlocked(false);
        setData(null);
        setError(null);
      }}
    />
  );
}

function Unlock({ error, busy, onUnlock }: { error: string | null; busy: boolean; onUnlock: (token: string) => void }) {
  const [token, setToken] = useState("");
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (token.trim()) onUnlock(token.trim());
  };
  return (
    <form className="rv-unlock" onSubmit={submit}>
      <label className="rv-field">
        <span className="label">Owner token</span>
        <input type="password" autoComplete="current-password" value={token} onChange={(e) => setToken(e.target.value)} />
      </label>
      <div className="rv-actions">
        <button type="submit" className="btn btn-solid btn-sm" disabled={busy || !token.trim()}>
          {busy ? "Checking…" : "Unlock"}
        </button>
      </div>
      {error && (
        <p className="rv-error" role="alert">
          {error}
        </p>
      )}
    </form>
  );
}

function Signed({
  data,
  note,
  onNote,
  reload,
  onForget,
}: {
  data: Overview;
  note: ApplyNote | null;
  onNote: (n: ApplyNote | null) => void;
  reload: () => void;
  onForget: () => void;
}) {
  return (
    <>
      <Status data={data} note={note} onNote={onNote} onForget={onForget} />
      {data.backend && <BackendSection backend={data.backend} data={data} />}
      <QueueSection data={data} />
      <RequestsSection data={data} />
      <MarkersSection markers={data.markers} />
      <WatchedSection data={data} />
      <p className="rv-foot">
        <button type="button" className="cl-link" onClick={reload}>
          Reload
        </button>
      </p>
    </>
  );
}

/* ── Status: the last runs, the pause switch, "Apply decisions now" ───── */

const RUN_NAME: Record<JobRun["mode"], string> = { full: "Last daily run", apply: "Last apply run", local: "Last run from the Mac" };

/** One run's report in a line: when, its status, its summary, and its log when it had a problem. */
function RunLine({ run }: { run: JobRun }) {
  return (
    <ul className="meta-line rv-line">
      <li>
        {RUN_NAME[run.mode]}: {stampOf(run.finished_at)}
      </li>
      <li>{run.status}</li>
      {run.summary && <li className="rv-wrap">{run.summary}</li>}
      {run.status !== "success" && run.url && (
        <li>
          <a href={run.url} target="_blank" rel="noopener noreferrer">
            log ↗
          </a>
        </li>
      )}
    </ul>
  );
}

/** What fixes a wake-up Vercel didn't take, by the error the Worker kept (lanes.wake_error). */
const WAKE_FIX: Record<string, string> = {
  "HTTP 401": "WAKE_SECRET must be the same on the Worker and on Vercel",
  "HTTP 403": "vercel.json's /api/step rule must let the signed wake-ups through",
  "HTTP 404": "the Worker's STEP_URL must be the Vercel deployment's /api/step",
  "HTTP 503": "the Vercel project isn't taking calls; its dashboard says why",
  "no answer": "the Worker's STEP_URL must be the Vercel deployment's /api/step",
};

/** What the owner must fix for the backend to run, newest first (none: nothing to fix). */
function setupErrors(data: Overview, newest: number): string[] {
  const out: string[] = [];
  if (data.job_refused_at != null && data.job_refused_at > newest)
    out.push(
      `The API refused the job's token (${stampOf(data.job_refused_at)}): Vercel's WATCHLIST_JOB_TOKEN must be the Worker's JOB_TOKEN.`,
    );
  const byError = new Map<string, string[]>();
  for (const l of data.backend?.lanes ?? []) if (l.wake_error) byError.set(l.wake_error, [...(byError.get(l.wake_error) ?? []), l.lane]);
  for (const [err, lanes] of byError) {
    const fix = WAKE_FIX[err];
    out.push(
      `Vercel didn't take the last wake-up of the ${lanes.join(", ")} ${lanes.length === 1 ? "lane" : "lanes"} (${err})${fix ? `: ${fix}` : ""}.`,
    );
  }
  return out;
}

function Status({
  data,
  note,
  onNote,
  onForget,
}: {
  data: Overview;
  note: ApplyNote | null;
  onNote: (n: ApplyNote | null) => void;
  onForget: () => void;
}) {
  const [writes, setWrites] = useState(data.settings.writes);
  const [busy, setBusy] = useState(false);
  const [switchError, setSwitchError] = useState<string | null>(null);

  const runs = [...data.runs].sort((a, b) => b.finished_at - a.finished_at);
  const dailies = runs.filter((r) => r.mode !== "apply");
  const daily = dailies[0];
  // the last daily run always; the last apply run too when it came after it
  const apply = runs.find((r) => r.mode === "apply" && (!daily || r.finished_at > daily.finished_at));
  const late = daily ? Math.floor((data.now - daily.finished_at) / 3600) : null;
  const troubled = dailies.length >= TROUBLED_RUNS && dailies.slice(0, TROUBLED_RUNS).every((r) => r.status !== "success");
  const problem = troubled ? firstProblem(daily.summary) : null;
  const errors = setupErrors(data, runs[0]?.finished_at ?? 0);
  // a queued apply's note goes once a run has reported since the press, or its run has finished (the lines say how it went)
  const applyRun = note?.run ? data.backend?.runs.find((r) => r.id === note.run) : undefined;
  const reported = note?.started && (runs.some((r) => r.finished_at * 1000 >= note.at) || (!!applyRun && RUN_TERMINAL.has(applyRun.state)));
  const noteText = note && !reported ? (applyRun?.state === "running" ? "Applying your decisions." : note.text) : null;

  const toggle = async () => {
    setBusy(true);
    setSwitchError(null);
    const next = writes === "on" ? "off" : "on";
    const r = await ownerSend<{ settings: { writes: "on" | "off" } }>("PUT", "/api/admin/settings", { writes: next });
    setBusy(false);
    if (r.ok) setWrites(r.data?.settings?.writes ?? next);
    else setSwitchError(said(r));
  };

  const start = async () => {
    setBusy(true);
    const at = Date.now();
    onNote(null);
    const r = await ownerSend<{ queued: boolean; run?: string }>("POST", "/api/admin/apply");
    setBusy(false);
    if (r.ok) onNote({ at, text: "Queued: your decisions are applied in a few minutes.", started: true, run: r.data?.run ?? null });
    else if (r.code === "limit") onNote({ at, text: "Not queued: it has been pressed 30 times today.", started: false });
    else onNote({ at, text: `Not queued: ${said(r)}`, started: false });
  };

  return (
    <section className="rv-sec" aria-label="Status">
      <div className="rv-runs">
        {daily && <RunLine run={daily} />}
        {apply && <RunLine run={apply} />}
        {!runs.length && (
          <ul className="meta-line rv-line">
            <li>No run has reported yet.</li>
          </ul>
        )}
        {errors.map((e) => (
          <p key={e} className="rv-warn" role="alert">
            {e}
          </p>
        ))}
      </div>
      {late != null && late > LATE_HOURS && (
        <p className="rv-warn">No daily run has reported for {fmtInt(late)} hours. The backend's runs below show where it stopped.</p>
      )}
      {troubled && (
        <p className="rv-warn">
          The last {TROUBLED_RUNS} daily runs each had a problem{problem ? ` (the newest: ${problem})` : ""}. The failed steps below
          say more.
        </p>
      )}
      <p className="rv-line">
        Visitors' changes: {writes === "on" ? "allowed" : "paused"} ·{" "}
        <button type="button" className="btn btn-ghost btn-sm" disabled={busy} onClick={toggle}>
          {writes === "on" ? "Pause visitors' changes" : "Allow visitors' changes"}
        </button>
        {switchError && <span className="rv-error"> {switchError}</span>}
      </p>
      <div className="rv-apply">
        <button type="button" className="btn btn-solid btn-sm" disabled={busy} onClick={start}>
          Apply decisions now
        </button>
        {noteText && <p className="rv-muted">{noteText}</p>}
      </div>
      <p className="rv-line">
        <button type="button" className="cl-link" onClick={onForget}>
          Forget token
        </button>
      </p>
    </section>
  );
}

/* ── The backend: Vercel's use, the lanes, the runs, the failed steps ─── */

/** Vercel Hobby's monthly allowances (docs/BACKEND.md; the Worker's budget counts against them). */
const CPU_MS_MONTH = 14_400_000;
const GB_MS_MONTH = 1_296_000_000;
const CALLS_MONTH = 1_000_000;

const LEVEL: Record<BudgetLevel, string> = {
  ok: "Within budget: everything runs.",
  tight: "Budget tight: no catch-up runs, and adds and Open now are capped lower.",
  low: "Budget low: adds and new Open now readings wait; the lists still publish.",
  stop: "Budget at its limit: nothing runs on Vercel until the last 30 days' use falls under 80%.",
};

/** "42 min", "3.1 h", "4 h" of CPU */
const cpuWords = (ms: number) =>
  ms < 90 * 60_000 ? `${fmtInt(Math.round(ms / 60_000))} min` : `${(ms / 36e5).toFixed(1).replace(/\.0$/, "")} h`;
/** "12.4 GB-h" */
const gbWords = (gbMs: number) => `${(gbMs / 36e5).toFixed(gbMs < 36e6 ? 1 : 0)} GB-h`;
const pct = (v: number, of: number) => `${fmtInt(Math.round((100 * v) / of))}%`;

function BudgetLine({ b }: { b: Budget }) {
  if (!b.computed_at) return <p className="rv-line">{LEVEL[b.level]} No use counted yet.</p>;
  return (
    <>
      <p className={b.level === "ok" ? "rv-line" : "rv-warn"}>{LEVEL[b.level]}</p>
      <ul className="meta-line rv-line tabular">
        <li>
          Last 30 days: CPU {cpuWords(b.cpu_ms_30d)} of {cpuWords(CPU_MS_MONTH)} ({pct(b.cpu_ms_30d, CPU_MS_MONTH)})
        </li>
        <li>
          memory {gbWords(b.gb_ms_30d)} of {gbWords(GB_MS_MONTH)} ({pct(b.gb_ms_30d, GB_MS_MONTH)})
        </li>
        <li>
          {fmtInt(b.invocations_30d)} of {fmtInt(CALLS_MONTH)} calls
        </li>
        <li>
          today CPU {cpuWords(b.cpu_ms_today)}, memory {gbWords(b.gb_ms_today)}
        </li>
        <li>counted {stampOf(b.computed_at)}</li>
      </ul>
    </>
  );
}

/** A lane in a row: its last claim, a pause, wake-ups no claim followed. */
function LaneRow({ l, now }: { l: LaneState; now: number }) {
  const facts = [
    l.last_claim_at ? `last step ${clockOn(l.last_claim_at, now)}` : "no step yet",
    l.paused_until > now ? `paused until ${clockOn(l.paused_until, now)}` : null,
    l.unanswered > 0 ? `${fmtInt(l.unanswered)} ${l.unanswered === 1 ? "wake-up" : "wake-ups"} since` : null,
  ].filter(Boolean);
  return (
    <li className="rv-row rv-row-lane">
      <b>{l.lane}</b>
      <span className="rv-label rv-muted">{facts.join(" · ")}</span>
    </li>
  );
}

const RUN_KIND: Record<RunKind, string> = { daily: "Daily", catchup: "Catch-up", add: "Add", apply: "Apply" };
const RUN_STATE: Record<RunState, string> = {
  queued: "queued",
  running: "running",
  done: "done",
  partial: "partly done",
  failed: "failed",
  cancelled: "cancelled",
  missed: "missed: no run that day",
  skipped: "skipped: over budget",
};

/** A run's meta as an object (some answers give it as JSON text). */
function metaOf(r: BackendRun): Record<string, unknown> {
  if (r.meta && typeof r.meta === "object") return r.meta;
  if (typeof r.meta === "string")
    try {
      const v = JSON.parse(r.meta) as unknown;
      return v && typeof v === "object" ? (v as Record<string, unknown>) : {};
    } catch {
      return {};
    }
  return {};
}

/** What a run is about: its day (daily, catch-up), its market (add), the press (apply). */
function runWhat(r: BackendRun, requests: AddRequest[]): string {
  if (r.kind === "daily" || r.kind === "catchup") return fmtCalDay(r.slot);
  if (r.kind === "apply") return stampOf(Number(r.slot) || r.created_at);
  const cid = String(metaOf(r).condition_id ?? r.slot.split(":")[0]).toLowerCase();
  return requests.find((x) => x.condition_id.toLowerCase() === cid)?.question ?? `${cid.slice(0, 10)}…`;
}

/** When a run ran: "Nov 19, 04:00–05:12 ET", queued since, or nothing for a run that never ran. */
function runWhen(r: BackendRun): string {
  if (r.state === "missed" || r.state === "skipped") return "";
  if (!r.started_at) return `queued ${stampOf(r.created_at)}`;
  if (!r.finished_at) return `since ${stampOf(r.started_at)}`;
  const sameDay = torontoDay(r.started_at * 1000) === torontoDay(r.finished_at * 1000);
  return sameDay
    ? `${dayOf(r.started_at)}, ${torontoClock(r.started_at * 1000)}–${torontoClock(r.finished_at * 1000)} ET`
    : `${stampOf(r.started_at)} – ${stampOf(r.finished_at)}`;
}

/** What a run's jobs reported: the stories left for the catch-up, the first problem. */
function runNotes(r: BackendRun): string {
  const m = metaOf(r);
  const out: string[] = [];
  if (typeof m.left === "number" && m.left > 0) out.push(`${fmtInt(m.left)} ${m.left === 1 ? "story" : "stories"} left`);
  const problems = Array.isArray(m.problems) ? m.problems.filter((p): p is string => typeof p === "string") : [];
  if (problems.length) out.push(problems.length > 1 ? `${problems[0]} (and ${fmtInt(problems.length - 1)} more)` : problems[0]);
  return out.join(" · ");
}

function BackendSection({ backend, data }: { backend: Backend; data: Overview }) {
  return (
    <section className="rv-sec" aria-labelledby="rv-backend">
      <h2 className="rv-h" id="rv-backend">
        Backend
      </h2>
      <BudgetLine b={backend.budget} />
      {backend.lanes.length > 0 && (
        <ul className="rv-rows">
          {backend.lanes.map((l) => (
            <LaneRow key={l.lane} l={l} now={data.now} />
          ))}
        </ul>
      )}
      <h3 className="label rv-sub-h">Latest runs</h3>
      {backend.runs.length === 0 ? (
        <p className="rv-muted">None yet.</p>
      ) : (
        <ul className="rv-rows">
          {backend.runs.map((r) => (
            <li key={r.id} className="rv-row rv-row-run" data-state={r.state}>
              <b>{RUN_KIND[r.kind] ?? r.kind}</b>
              <span className="rv-label">{runWhat(r, data.requests)}</span>
              <span className={r.state === "failed" ? "rv-error" : undefined}>{RUN_STATE[r.state] ?? r.state}</span>
              <span className="rv-muted tabular">{runWhen(r)}</span>
              {runNotes(r) && <span className="rv-muted rv-row-note">{runNotes(r)}</span>}
            </li>
          ))}
        </ul>
      )}
      <h3 className="label rv-sub-h">Failed steps, last 14 days</h3>
      {backend.failed_jobs.length === 0 ? (
        <p className="rv-muted">None.</p>
      ) : (
        <ul className="rv-rows">
          {backend.failed_jobs.map((j) => (
            <li key={j.id} className="rv-row rv-row-fail">
              <b>{j.kind}</b>
              <span className="rv-muted tabular">{stampOf(j.updated_at)}</span>
              <span className="rv-label">{j.error ?? "no error kept"}</span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

/* ── The open list's queue ────────────────────────────────────────────── */

const WHY: Record<QueueItem["why"], string> = {
  "awaiting approval": "Both readings marked a step.",
  "on the list": "On today's list, with newer steps the readings marked.",
  "one reading": "Only one reading marked a step.",
  "dated, on the list": "Decided in advance, on today's list.",
};

const PLACE: Record<QueueItem["place"], string> = {
  "new step": "New step",
  stays: "Stays on the list",
  "gone quiet": "Gone quiet",
  dated: "Decided in advance",
};

/** What a decision was about: its headline, or a whole card's question. */
const titleOf = (d: Decision) => d.headline?.title ?? d.question ?? d.story;

/** A decision in words, by what it was about: a headline, or a dated card as a whole. */
function decisionWords(d: Decision): string {
  if (d.headline_key === "card") return d.decision === "card approved" ? "Shown" : "Not shown";
  return d.decision === "step" ? "Counted as a step" : "Not a step";
}

/** The other decision on the same headline or card, and its button's words. */
function oppositeOf(d: Decision): DecisionValue {
  if (d.headline_key === "card") return d.decision === "card approved" ? "not a step" : "card approved";
  return d.decision === "step" ? "not a step" : "step";
}

function oppositeWords(d: Decision): string {
  if (d.headline_key === "card") return d.decision === "card approved" ? "Don't show it" : "Show this card";
  return d.decision === "step" ? "Mark as not a step" : "Count as a step";
}

function QueueSection({ data }: { data: Overview }) {
  const q = data.queue;
  const [decided, setDecided] = useState(() => new Map(data.decisions.map((d) => [dkey(d.story, d.headline_key), d] as const)));
  const [errors, setErrors] = useState(() => new Map<string, string>());

  const setError = (k: string, msg: string | null) =>
    setErrors((m) => {
      const next = new Map(m);
      if (msg) next.set(k, msg);
      else next.delete(k);
      return next;
    });

  const decide = async (item: QueueItem, key: string, decision: DecisionValue) => {
    const k = dkey(item.story, key);
    setError(k, null);
    const r = await ownerSend<{ decision: Decision }>("PUT", "/api/admin/decisions", {
      day: q!.day,
      story: item.story,
      headline_key: key,
      decision,
    });
    if (r.ok && r.data?.decision) setDecided((m) => new Map(m).set(k, r.data.decision));
    else setError(k, r.ok ? WORDS.other : said(r));
  };

  // a decision whose headline has left the queue (applied by a run, or from an earlier list) is changed under
  // its own day; the API keeps its stored headline
  const redecide = async (d: Decision, decision: DecisionValue) => {
    const k = dkey(d.story, d.headline_key);
    setError(k, null);
    const r = await ownerSend<{ decision: Decision }>("PUT", "/api/admin/decisions", {
      day: d.day,
      story: d.story,
      headline_key: d.headline_key,
      decision,
    });
    if (r.ok && r.data?.decision) setDecided((m) => new Map(m).set(k, r.data.decision));
    else setError(k, r.ok ? WORDS.other : said(r));
  };

  const undo = async (story: string, key: string) => {
    const k = dkey(story, key);
    setError(k, null);
    const r = await ownerSend<null>("DELETE", `/api/admin/decisions?story=${encodeURIComponent(story)}&key=${encodeURIComponent(key)}`);
    if (r.ok || r.code === "not_found")
      setDecided((m) => {
        const next = new Map(m);
        next.delete(k);
        return next;
      });
    else if (r.code === "applied") setError(k, "Already applied: make the opposite decision instead.");
    else setError(k, said(r));
  };

  // A run that applies a decision rebuilds the queue without that headline (it is decided), so a decision is
  // listed below the queue whenever its headline isn't in it: still waiting, or applied by a recent run.
  const shown = new Set(
    (q?.items ?? []).flatMap((i) =>
      i.candidates.length ? i.candidates.map((c) => dkey(i.story, c.key)) : i.place === "dated" ? [dkey(i.story, "card")] : [],
    ),
  );
  const notShown = [...decided.values()].filter((d) => !shown.has(dkey(d.story, d.headline_key)));
  const elsewhere = notShown.filter((d) => d.applied_at == null);
  const applied = notShown.filter((d) => d.applied_at != null).sort((a, b) => b.applied_at! - a.applied_at!);

  return (
    <section className="rv-sec" aria-labelledby="rv-queue">
      <h2 className="rv-h" id="rv-queue">
        Open list · waiting for your check{q && ` (${day(q.day)})`}
      </h2>
      {!q || q.items.length === 0 ? (
        <p className="rv-muted">Nothing is waiting for a check.</p>
      ) : (
        <ol className="rv-list">
          {q.items.map((item) => (
            <li key={item.story} className="rv-item">
              <p className="label">
                {PLACE[item.place]}
                {item.kind && ` · ${item.kind.replace(/_/g, " ")}`}
              </p>
              <h3 className="rv-q">{item.question}</h3>
              <p className="rv-sub">
                Polymarket asks:{" "}
                {item.url ? (
                  <a className="rv-a" href={item.url} target="_blank" rel="noopener noreferrer">
                    {item.polymarket} <span aria-hidden>↗</span>
                  </a>
                ) : (
                  item.polymarket
                )}
              </p>
              <p className="rv-sub">Who would know first: {item.who_knows}</p>
              <p className="rv-why">{WHY[item.why]}</p>
              {item.candidates.length > 0 ? (
                <ul className="rv-cands">
                  {item.candidates.map((c) => (
                    <CandidateRow
                      key={c.key}
                      c={c}
                      d={decided.get(dkey(item.story, c.key))}
                      error={errors.get(dkey(item.story, c.key))}
                      onDecide={(v) => decide(item, c.key, v)}
                      onUndo={() => undo(item.story, c.key)}
                    />
                  ))}
                </ul>
              ) : (
                item.place === "dated" && (
                  <ul className="rv-cands">
                    <CardRow
                      d={decided.get(dkey(item.story, "card"))}
                      error={errors.get(dkey(item.story, "card"))}
                      onDecide={(v) => decide(item, "card", v)}
                      onUndo={() => undo(item.story, "card")}
                    />
                  </ul>
                )
              )}
              {item.model_copy_unmatched.length > 0 && (
                <p className="rv-muted">
                  The readings also named {item.model_copy_unmatched.length === 1 ? "a headline" : "headlines"} not found in the
                  news: {item.model_copy_unmatched.map((t) => `“${t}”`).join(", ")}
                </p>
              )}
            </li>
          ))}
        </ol>
      )}
      {elsewhere.length > 0 && (
        <div className="rv-elsewhere">
          <p className="label">Your other decisions</p>
          <ul className="rv-cands">
            {elsewhere.map((d) => (
              <li key={dkey(d.story, d.headline_key)} className="rv-cand">
                <span className="rv-cand-text">
                  {titleOf(d)} · {decisionWords(d)}
                </span>
                <span className="rv-cand-do">
                  <button type="button" className="cl-link" onClick={() => undo(d.story, d.headline_key)}>
                    Undo
                  </button>
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}
      {applied.length > 0 && (
        <div className="rv-elsewhere">
          <p className="label">Applied by recent runs</p>
          <ul className="rv-cands">
            {applied.map((d) => (
              <li key={dkey(d.story, d.headline_key)} className="rv-cand">
                <span className="rv-cand-text">
                  {titleOf(d)} · {decisionWords(d)} · applied {dayOf(d.applied_at!)}
                </span>
                <span className="rv-cand-do">
                  <button type="button" className="cl-link" onClick={() => redecide(d, oppositeOf(d))}>
                    {oppositeWords(d)}
                  </button>
                </span>
                {errors.get(dkey(d.story, d.headline_key)) && (
                  <span className="rv-error">{errors.get(dkey(d.story, d.headline_key))}</span>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}

/** What a decision reads once made: "Counted as a step · Undo", once a run applied it "· applied Nov 14". */
function Decided({ d, onUndo, opposite }: { d: Decision; onUndo: () => void; opposite: ReactNode }) {
  return (
    <span className="rv-decided">
      <b>{decisionWords(d)}</b>
      {d.applied_at == null ? (
        <>
          {" "}
          ·{" "}
          <button type="button" className="cl-link" onClick={onUndo}>
            Undo
          </button>
        </>
      ) : (
        <>
          {" "}
          · applied {dayOf(d.applied_at)} · {opposite}
        </>
      )}
    </span>
  );
}

function CandidateRow({
  c,
  d,
  error,
  onDecide,
  onUndo,
}: {
  c: Candidate;
  d: Decision | undefined;
  error: string | undefined;
  onDecide: (v: DecisionValue) => Promise<void>;
  onUndo: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const run = async (v: DecisionValue) => {
    setBusy(true);
    await onDecide(v);
    setBusy(false);
  };
  return (
    <li className="rv-cand">
      <span className="rv-cand-text">
        {c.url ? (
          <a className="rv-a" href={c.url} target="_blank" rel="noopener noreferrer">
            {c.title}
          </a>
        ) : (
          c.title
        )}{" "}
        <span className="rv-src">
          — {c.source ?? "unknown source"}
          {c.published && ` · ${day(c.published)}`}
        </span>
      </span>
      <span className="rv-cand-do">
        {d ? (
          <Decided
            d={d}
            onUndo={onUndo}
            opposite={
              <button type="button" className="cl-link" disabled={busy} onClick={() => run(d.decision === "step" ? "not a step" : "step")}>
                {d.decision === "step" ? "Mark as not a step" : "Count as a step"}
              </button>
            }
          />
        ) : (
          <>
            <button type="button" className="btn btn-ghost btn-sm" disabled={busy} onClick={() => run("step")}>
              Count as a step
            </button>
            <button type="button" className="btn btn-ghost btn-sm" disabled={busy} onClick={() => run("not a step")}>
              Not a step
            </button>
          </>
        )}
      </span>
      {error && <span className="rv-error">{error}</span>}
    </li>
  );
}

/** A dated card with no step headline: show it, or not, as a whole (key "card"). */
function CardRow({
  d,
  error,
  onDecide,
  onUndo,
}: {
  d: Decision | undefined;
  error: string | undefined;
  onDecide: (v: DecisionValue) => Promise<void>;
  onUndo: () => void;
}) {
  return (
    <li className="rv-cand">
      <span className="rv-cand-text rv-muted">No step headline: the card itself</span>
      <span className="rv-cand-do">
        {d ? (
          <Decided
            d={d}
            onUndo={onUndo}
            opposite={
              <button
                type="button"
                className="cl-link"
                onClick={() => onDecide(d.decision === "card approved" ? "not a step" : "card approved")}
              >
                {d.decision === "card approved" ? "Don't show it" : "Show this card"}
              </button>
            }
          />
        ) : (
          <>
            <button type="button" className="btn btn-ghost btn-sm" onClick={() => onDecide("card approved")}>
              Show this card
            </button>
            <button type="button" className="btn btn-ghost btn-sm" onClick={() => onDecide("not a step")}>
              Don't show it
            </button>
          </>
        )}
      </span>
      {error && <span className="rv-error">{error}</span>}
    </li>
  );
}

/* ── Closed markets sent to the main page ─────────────────────────────── */

/** A market's state in words: "Adding to the main page", "Added as gemini-hydrogen on Nov 14", or why its add run couldn't add it. */
function requestWords(r: AddRequest): [head: string, tail: string] {
  switch (r.status) {
    case "approved":
      return ["Adding to the main page", ""];
    case "added":
      return [`Added${r.name ? ` as ${r.name}` : ""}`, r.done_at ? ` on ${dayOf(r.done_at)}` : ""];
    case "failed":
      return ["Couldn't add it", r.note ? `: ${r.note}` : ""];
    case "removed":
      return ["Taken off the main page", ""];
  }
}

function RequestsSection({ data }: { data: Overview }) {
  const order = [...data.requests].sort((a, b) => b.requested_at - a.requested_at);
  return (
    <section className="rv-sec" aria-labelledby="rv-requests">
      <h2 className="rv-h" id="rv-requests">
        Add to main page
      </h2>
      {order.length === 0 ? (
        <p className="rv-muted">None yet.</p>
      ) : (
        <ol className="rv-list">
          {order.map((r) => {
            const [head, tail] = requestWords(r);
            return (
              <li key={r.condition_id} className="rv-item">
                <h3 className="rv-q">{r.question}</h3>
                <ul className="meta-line rv-sub">
                  <li>closed {day(r.closed)}</li>
                  <li>resolved {r.winner}</li>
                  <li>
                    <a href={r.url} target="_blank" rel="noopener noreferrer">
                      Polymarket ↗
                    </a>
                  </li>
                </ul>
                <p className="rv-decided">
                  <b>{head}</b>
                  {tail}
                </p>
              </li>
            );
          })}
        </ol>
      )}
    </section>
  );
}

/* ── Visitors' markers and watched markets ────────────────────────────── */

function MarkersSection({ markers: initial }: { markers: AdminMarker[] }) {
  const index = useAsync(loadIndex, "index");
  const [markers, setMarkers] = useState(initial);
  const [errors, setErrors] = useState(() => new Map<string, string>());
  const titles = new Map((index.data?.markets ?? []).map((m) => [m.name, m.title] as const));
  const del = async (m: AdminMarker) => {
    const r = await ownerSend<null>("DELETE", `/api/admin/markers/${encodeURIComponent(m.id)}`);
    if (r.ok || r.code === "not_found")
      setMarkers((list) => list.map((x) => (x.id === m.id ? { ...x, deleted_at: Math.floor(Date.now() / 1000), deleted_by: "owner" } : x)));
    else setErrors((e) => new Map(e).set(m.id, said(r)));
  };
  return (
    <section className="rv-sec" aria-labelledby="rv-markers">
      <h2 className="rv-h" id="rv-markers">
        Timeline markers added by visitors (newest 50)
      </h2>
      {markers.length === 0 ? (
        <p className="rv-muted">None yet.</p>
      ) : (
        <ul className="rv-rows">
          {markers.map((m) => (
            <li key={m.id} className="rv-row" data-gone={m.deleted_at != null || undefined}>
              <a className="rv-a" href={`#/m/${encodeURIComponent(m.market)}`} title={titles.get(m.market)}>
                {m.market}
              </a>
              <span className="tabular">{fmtT(m.t)}</span>
              <span className="rv-label">
                {m.label}
                {m.source === "ai" && <span className="rv-muted"> · AI</span>}
              </span>
              <span className="rv-row-do">
                {m.deleted_at != null ? (
                  <span className="rv-muted">{m.deleted_by === "visitor" ? "removed by its author" : "deleted by the owner"}</span>
                ) : (
                  <button type="button" className="btn btn-ghost btn-sm" onClick={() => del(m)}>
                    Delete
                  </button>
                )}
                {errors.get(m.id) && <span className="rv-error"> {errors.get(m.id)}</span>}
              </span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function WatchedSection({ data }: { data: Overview }) {
  return (
    <section className="rv-sec" aria-labelledby="rv-watched">
      <h2 className="rv-h" id="rv-watched">
        Watched markets
      </h2>
      {data.watched.length === 0 ? (
        <p className="rv-muted">None yet.</p>
      ) : (
        <ul className="rv-rows">
          {data.watched.map((w) => (
            <li key={w.story} className="rv-row rv-row-watch">
              <span className="rv-label">
                {w.url ? (
                  <a className="rv-a" href={w.url} target="_blank" rel="noopener noreferrer">
                    {w.question ?? w.story}
                  </a>
                ) : (
                  (w.question ?? w.story)
                )}
              </span>
              <span className="rv-muted">since {dayOf(w.since)}</span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
