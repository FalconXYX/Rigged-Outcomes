import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent, type ReactNode } from "react";
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
  type Candidate,
  type Decision,
  type DecisionValue,
  type JobRun,
  type Overview,
  type QueueItem,
  type Sent,
} from "../lib/api";
import { loadIndex, useAsync } from "../lib/data";
import { fmtDay, fmtInt, fmtT } from "../lib/format";
import "./review.css";

// The owner's review page (#/review, linked from nowhere). Unlocked with the
// owner token (kept in this page's memory only, so a reload asks again), it
// shows how the daily run went and the pause switch for visitors' changes,
// then what waits for a decision: the open list's step headlines, requests to
// add a closed market to the main page, the markers visitors added and the
// markets they watch. A decision is applied by the next run: the daily one
// (about 04:17 UTC), or at once with "Apply decisions now" when the Worker
// holds a GitHub token. Plain on purpose, in the site's type and tokens.

/** a daily run older than this has missed one (they run once a day) */
const LATE_HOURS = 30;

const day = (iso: string) => fmtDay(new Date(iso.length === 10 ? `${iso}T00:00:00Z` : iso));
const hhmm = (s: number) => new Date(s * 1000).toISOString().slice(11, 16);
/** "Nov 14, 04:31 UTC" from epoch seconds */
const stampOf = (s: number) => `${fmtDay(new Date(s * 1000))}, ${hhmm(s)} UTC`;
const dayOf = (s: number) => fmtDay(new Date(s * 1000));
const dkey = (story: string, key: string) => `${story}\u0000${key}`;

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
  // each load remounts the sections, whose state starts from the overview (so Reload shows what changed)
  const [loads, setLoads] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    const r = await ownerGet<Overview>("/api/admin/overview");
    setLoading(false);
    if (r.ok && r.data) {
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
    }
    setError(r.ok ? WORDS.other : said(r));
  }, []);

  useEffect(() => {
    if (unlocked) load();
    // only on arrival: later loads follow an unlock or a decision
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

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
        <button type="button" className="cl-link" disabled={loading} onClick={load}>
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
      reload={load}
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
      <p className="rv-muted">Kept on this page only: reloading the page asks for it again.</p>
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

function Signed({ data, reload, onForget }: { data: Overview; reload: () => void; onForget: () => void }) {
  return (
    <>
      <Status data={data} onForget={onForget} />
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

/* ── Status: the last run, the pause switch, "Apply decisions now" ───── */

const APPLY_NOTE =
  "Starts the update job (about 10–20 minutes): it applies your decisions and adds approved markets; the site updates " +
  "when it finishes. Otherwise the next daily run (starts about 04:17 UTC) does it.";

const RUN_NAME: Record<JobRun["mode"], string> = { full: "Last daily run", apply: "Last apply run", local: "Last run from the Mac" };

function Status({ data, onForget }: { data: Overview; onForget: () => void }) {
  const [writes, setWrites] = useState(data.settings.writes);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const [applied, setApplied] = useState<string | null>(null);

  const runs = [...data.runs].sort((a, b) => b.finished_at - a.finished_at);
  const last = runs[0];
  const daily = runs.find((r) => r.mode !== "apply");
  const late = daily ? Math.floor((data.now - daily.finished_at) / 3600) : null;

  const toggle = async () => {
    setBusy(true);
    setNote(null);
    const next = writes === "on" ? "off" : "on";
    const r = await ownerSend<{ settings: { writes: "on" | "off" } }>("PUT", "/api/admin/settings", { writes: next });
    setBusy(false);
    if (r.ok) setWrites(r.data?.settings?.writes ?? next);
    else setNote(said(r));
  };

  const apply = async () => {
    setBusy(true);
    setApplied(null);
    const r = await ownerSend<{ dispatched: boolean }>("POST", "/api/admin/apply");
    setBusy(false);
    if (r.ok) setApplied("Started · the site updates when the job finishes.");
    else if (r.code === "limit") setApplied("Not started: it was started less than 2 minutes ago, or 30 times today.");
    else if (r.code === "no_dispatch") setApplied("Not started: the Worker has no GitHub token.");
    else setApplied(`Not started: ${said(r)}`);
  };

  return (
    <section className="rv-sec" aria-label="Status">
      <ul className="meta-line rv-line">
        {last ? (
          <>
            <li>
              {RUN_NAME[last.mode]}: {stampOf(last.finished_at)}
            </li>
            <li>{last.status}</li>
            {last.summary && <li className="rv-wrap">{last.summary}</li>}
            {last.status !== "success" && last.url && (
              <li>
                <a href={last.url} target="_blank" rel="noopener noreferrer">
                  log ↗
                </a>
              </li>
            )}
          </>
        ) : (
          <li>No run has reported yet.</li>
        )}
      </ul>
      {late != null && late > LATE_HOURS && (
        <p className="rv-warn">
          No daily run has reported for {fmtInt(late)} hours. Check the workflow in GitHub Actions.
        </p>
      )}
      <p className="rv-line">
        Visitors' changes: {writes === "on" ? "allowed" : "paused"} ·{" "}
        <button type="button" className="btn btn-ghost btn-sm" disabled={busy} onClick={toggle}>
          {writes === "on" ? "Pause visitors' changes" : "Allow visitors' changes"}
        </button>
        {note && <span className="rv-error"> {note}</span>}
      </p>
      {data.dispatch ? (
        <div className="rv-apply">
          <button type="button" className="btn btn-solid btn-sm" disabled={busy} onClick={apply}>
            Apply decisions now
          </button>
          <p className="rv-muted">{applied ?? APPLY_NOTE}</p>
        </div>
      ) : (
        <p className="rv-muted">Decisions are applied by the next daily run (starts about 04:17 UTC).</p>
      )}
      <p className="rv-line">
        <button type="button" className="cl-link" onClick={onForget}>
          Forget token
        </button>
      </p>
    </section>
  );
}

/* ── The open list's queue ────────────────────────────────────────────── */

const WHY: Record<QueueItem["why"], string> = {
  "awaiting approval": "Both readings marked a step. The card shows once you count one as a step.",
  "on the list":
    "On today's list, with newer steps the readings marked. Count one as a step to keep the card fresh, or mark one as not a step to hide it.",
  "one reading": "Only one reading marked a step. Count it as a step to list it.",
  "dated, on the list": "Decided in advance: it shows on its own. Choose Don't show it to take it off.",
};

const PLACE: Record<QueueItem["place"], string> = {
  "new step": "New step",
  stays: "Stays on the list",
  "gone quiet": "Gone quiet",
  dated: "Decided in advance",
};

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
      {!q ? (
        <p className="rv-muted">Nothing yet: the daily run sends the open list's queue after it runs.</p>
      ) : q.items.length === 0 ? (
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
          <p className="label">Also waiting for the next run (not in the list above)</p>
          <ul className="rv-cands">
            {elsewhere.map((d) => (
              <li key={dkey(d.story, d.headline_key)} className="rv-cand">
                <span className="rv-cand-text">
                  {d.headline?.title ?? d.story} · {decisionWords(d)}
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
                  {d.headline?.title ?? d.story} · {decisionWords(d)} · applied {dayOf(d.applied_at!)}
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

/** What a decision reads once made: "Counted as a step · applies at the next run · Undo", later "· applied Nov 14". */
function Decided({ d, onUndo, opposite }: { d: Decision; onUndo: () => void; opposite: ReactNode }) {
  return (
    <span className="rv-decided">
      <b>{decisionWords(d)}</b>
      {d.applied_at == null ? (
        <>
          {" "}
          · applies at the next run ·{" "}
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

/* ── Requests to add a closed market to the main page ─────────────────── */

const NAME_RULE = /^[a-z0-9][a-z0-9-]{1,31}$/;
const STOP = new Set(
  (
    "a an the of in on at by to for and or be is are was were will would before after with from as its it this that than " +
    "over under into out up down more end any who what which how does do did has have new next first last"
  ).split(" "),
);

/**
 * A page name from the question: its first 3 telling words, lower case
 * ("Will Gemini 3 ship by May?" → "gemini-ship-may"), "-2" on a clash.
 */
function suggestName(question: string, taken: Set<string>): string {
  const words = question
    .toLowerCase()
    .replace(/[^a-z\s]+/g, " ")
    .split(/\s+/)
    .filter((w) => w.length > 1 && !STOP.has(w));
  let base = words.slice(0, 3).join("-").slice(0, 28).replace(/-+$/, "") || "market";
  if (base.length < 2) base = `${base}-market`;
  if (!taken.has(base)) return base;
  for (let i = 2; ; i++) if (!taken.has(`${base}-${i}`)) return `${base}-${i}`;
}

/** A request's state in words: the state, then what follows ("Approved as gemini-hydrogen · added at the next run"). */
function requestWords(r: AddRequest): [head: string, tail: string] {
  const as = r.name ? ` as ${r.name}` : "";
  switch (r.status) {
    case "requested":
      return ["Requested", ""];
    case "approved":
      return [`Approved${as}`, " · added at the next run"];
    case "added":
      return [`Added${as}`, r.done_at ? ` on ${dayOf(r.done_at)}` : ""];
    case "declined":
      return ["Declined", ""];
    case "failed":
      return ["Couldn't add it", r.note ? `: ${r.note}` : ""];
  }
}

function RequestsSection({ data }: { data: Overview }) {
  const index = useAsync(loadIndex, "index");
  const [reqs, setReqs] = useState(data.requests);
  const taken = useMemo(() => {
    const s = new Set((index.data?.markets ?? []).map((m) => m.name));
    for (const r of reqs) if (r.name && (r.status === "approved" || r.status === "added")) s.add(r.name);
    return s;
  }, [index.data, reqs]);
  const update = (r: AddRequest) => setReqs((list) => list.map((x) => (x.condition_id === r.condition_id ? r : x)));
  const order = [...reqs].sort((a, b) => b.requested_at - a.requested_at);
  return (
    <section className="rv-sec" aria-labelledby="rv-requests">
      <h2 className="rv-h" id="rv-requests">
        Requests to add to the main page
      </h2>
      {order.length === 0 ? (
        <p className="rv-muted">No requests.</p>
      ) : (
        <ol className="rv-list">
          {order.map((r) => (
            <RequestRow key={r.condition_id} r={r} taken={taken} onChange={update} />
          ))}
        </ol>
      )}
    </section>
  );
}

function RequestRow({ r, taken, onChange }: { r: AddRequest; taken: Set<string>; onChange: (r: AddRequest) => void }) {
  const [name, setName] = useState(() => r.name ?? suggestName(r.question, taken));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // the button pressed may be gone after it worked: focus goes to the line that says what happened
  const said_ = useRef<HTMLParagraphElement>(null);
  const act = async (action: "approve" | "decline" | "retry") => {
    if (action === "approve" && !NAME_RULE.test(name)) {
      setError("A page name is 2 to 32 lower-case letters, digits or dashes, starting with a letter or digit.");
      return;
    }
    setBusy(true);
    setError(null);
    const r2 = await ownerSend<{ request: AddRequest }>(
      "POST",
      `/api/admin/requests/${encodeURIComponent(r.condition_id)}`,
      action === "approve" ? { action, name } : { action },
    );
    setBusy(false);
    if (r2.ok && r2.data?.request) {
      onChange(r2.data.request);
      window.setTimeout(() => said_.current?.focus({ preventScroll: true }));
    } else setError(r2.ok ? WORDS.other : said(r2));
  };
  const canName = r.status === "requested" || r.status === "declined";
  const [head, tail] = requestWords(r);
  return (
    <li className="rv-item">
      <h3 className="rv-q">{r.question}</h3>
      <ul className="meta-line rv-sub">
        <li>closed {day(r.closed)}</li>
        <li>resolved {r.winner}</li>
        <li>asked {dayOf(r.requested_at)}</li>
        <li>
          <a href={r.url} target="_blank" rel="noopener noreferrer">
            Polymarket ↗
          </a>
        </li>
      </ul>
      <p className="rv-decided" ref={said_} tabIndex={-1}>
        <b>{head}</b>
        {tail}
      </p>
      {canName && (
        <label className="rv-field rv-name">
          <span className="label">Page name</span>
          <input value={name} maxLength={32} spellCheck={false} onChange={(e) => setName(e.target.value.toLowerCase())} />
        </label>
      )}
      <div className="rv-actions">
        {canName && (
          <button type="button" className="btn btn-solid btn-sm" disabled={busy} onClick={() => act("approve")}>
            Approve and add
          </button>
        )}
        {r.status === "failed" && (
          <button type="button" className="btn btn-solid btn-sm" disabled={busy} onClick={() => act("retry")}>
            Try again
          </button>
        )}
        {(r.status === "requested" || r.status === "approved" || r.status === "failed") && (
          <button type="button" className="btn btn-ghost btn-sm" disabled={busy} onClick={() => act("decline")}>
            Decline
          </button>
        )}
      </div>
      {error && (
        <p className="rv-error" role="alert">
          {error}
        </p>
      )}
    </li>
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
              <span className="tabular">{fmtInt(w.count)} watching</span>
              <span className="rv-muted">since {dayOf(w.since)}</span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
