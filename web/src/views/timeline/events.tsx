// Timeline markers ("at this time, this happened") beyond the ones export.py
// takes from markets.json. Not saved yet: they last until the page reloads.
// They will live in the database once the backend exists (the user chose to
// wait for it, 2026-11-12), where any visitor can add them and a model may
// add them too (source "ai"). Until then the editor shows on the local dev
// server only.
import { useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import type { AddedEvent, MarketData, MarketEvent } from "../../types";
import { fmtT } from "../../lib/format";

export const EVENTS_EDITABLE = import.meta.env.DEV;
const MAX_LABEL = 40;

/** The market with its added markers merged into timeline.events (by time). */
export function withAddedEvents(market: MarketData, added: AddedEvent[]): MarketData {
  if (!added.length) return market;
  const events: MarketEvent[] = [...market.timeline.events, ...added.map(({ t, label }) => ({ t, label }))].sort(
    (a, b) => a.t - b.t,
  );
  return { ...market, timeline: { ...market.timeline, events } };
}

/** This page's added markers, per market; kept in memory until the database exists. */
const session = new Map<string, AddedEvent[]>();

export function useAddedEvents(name: string) {
  const [events, setEvents] = useState<AddedEvent[]>(() => session.get(name) ?? []);
  const save = async (next: AddedEvent[]) => {
    session.set(name, next);
    setEvents(next);
  };
  return { events, save };
}

/* `t` is the US/Eastern wall clock read as UTC (types.ts), so a
   datetime-local value converts with UTC arithmetic and means Eastern time. */
const toInput = (t: number) => new Date(t * 1000).toISOString().slice(0, 16);
const fromInput = (v: string) => {
  const ms = Date.parse(`${v}:00Z`);
  return Number.isFinite(ms) ? Math.round(ms / 1000) : null;
};

/**
 * The editor: the market's markers (the ones from markets.json can't be
 * removed here) and a form to add one, its time starting at the replay
 * playhead so scrubbing to a moment and pressing Add is enough.
 */
export function EventsEditor({
  fixed,
  added,
  save,
  playhead,
}: {
  fixed: MarketEvent[];
  added: AddedEvent[];
  save: (events: AddedEvent[]) => Promise<void>;
  playhead: () => number;
}) {
  const [open, setOpen] = useState(false);
  const [label, setLabel] = useState("");
  const [when, setWhen] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const root = useRef<HTMLDivElement>(null);
  const labelInput = useRef<HTMLInputElement>(null);
  const toggle = useRef<HTMLButtonElement>(null);

  const rows = useMemo(
    () =>
      [
        ...fixed.map((e) => ({ key: `f${e.t}${e.label}`, t: e.t, label: e.label, from: "markets.json", id: null })),
        ...added.map((e) => ({ key: e.id, t: e.t, label: e.label, from: e.source === "ai" ? "AI" : "added", id: e.id })),
      ].sort((a, b) => a.t - b.t),
    [fixed, added],
  );

  const show = () => {
    setWhen(toInput(playhead()));
    setError(null);
    setOpen(true);
  };

  useEffect(() => {
    if (!open) return;
    labelInput.current?.focus();
    const away = (e: PointerEvent) => {
      if (root.current && !root.current.contains(e.target as Node)) setOpen(false);
    };
    window.addEventListener("pointerdown", away);
    return () => window.removeEventListener("pointerdown", away);
  }, [open]);

  const run = async (events: AddedEvent[]) => {
    setBusy(true);
    setError(null);
    try {
      await save(events);
      return true;
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      return false;
    } finally {
      setBusy(false);
    }
  };

  const add = async (e: FormEvent) => {
    e.preventDefault();
    const t = fromInput(when);
    const text = label.trim();
    if (!text || t == null) return;
    const next = [...added, { id: `ev_${Date.now().toString(36)}`, t, label: text, source: "manual" as const }];
    if (await run(next.sort((a, b) => a.t - b.t))) setLabel("");
  };

  return (
    <div
      className="tl-ev"
      ref={root}
      onKeyDown={(e) => {
        e.stopPropagation(); // typing here is not a replay shortcut
        if (e.key === "Escape" && open) {
          setOpen(false);
          toggle.current?.focus(); // the panel's focus would otherwise drop to the page
        }
      }}
    >
      <button
        type="button"
        ref={toggle}
        className={`btn btn-ghost btn-sm tl-ev-toggle${open ? " on" : ""}`}
        onClick={() => (open ? setOpen(false) : show())}
        aria-expanded={open}
        title="Add or remove markers on this timeline (local dev server only)"
      >
        Events
        {rows.length > 0 && <span className="tl-ev-count tabular">{rows.length}</span>}
      </button>

      {open && (
        <div className="tl-ev-panel" role="dialog" aria-label="Timeline events">
          <p className="label">Events on this timeline</p>
          {rows.length > 0 ? (
            <ul className="tl-ev-list">
              {rows.map((r) => (
                <li key={r.key}>
                  <span className="tl-ev-time tabular">{fmtT(r.t)}</span>
                  <span className="tl-ev-label">{r.label}</span>
                  {r.id ? (
                    <button
                      type="button"
                      className="tl-ev-remove"
                      disabled={busy}
                      onClick={() => run(added.filter((a) => a.id !== r.id))}
                      aria-label={`Remove “${r.label}”`}
                    >
                      Remove
                    </button>
                  ) : (
                    <span className="tl-ev-from">{r.from}</span>
                  )}
                </li>
              ))}
            </ul>
          ) : (
            <p className="tl-ev-empty">None yet.</p>
          )}

          <form className="tl-ev-form" onSubmit={add}>
            <label>
              <span className="label">What happened</span>
              <input
                ref={labelInput}
                value={label}
                maxLength={MAX_LABEL}
                onChange={(e) => setLabel(e.target.value)}
                placeholder="e.g. Strikes reported"
              />
            </label>
            <label>
              <span className="label">When (ET)</span>
              <input type="datetime-local" value={when} onChange={(e) => setWhen(e.target.value)} />
            </label>
            <div className="tl-ev-actions">
              <button type="button" className="tl-ev-link" onClick={() => setWhen(toInput(playhead()))}>
                Use the replay time
              </button>
              <button type="submit" className="btn btn-solid btn-sm" disabled={busy || !label.trim() || !fromInput(when)}>
                Add
              </button>
            </div>
          </form>
          {error && <p className="tl-ev-error">Could not save: {error}</p>}
          <p className="tl-ev-note">Not saved yet: markers go away when the page reloads.</p>
        </div>
      )}
    </div>
  );
}
