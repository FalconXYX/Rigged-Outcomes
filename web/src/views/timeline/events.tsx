// Timeline markers ("at this time, this happened") beyond the ones export.py
// takes from markets.json. Any visitor can add one: the site's API keeps it
// (lib/api.ts) and everyone sees it at once. Whoever added a marker can remove
// it for 24 hours (this browser keeps its undo token); the site's owner can
// remove any on the review page. A model may add them later (source "ai").
// With no API in the build, or the API down, there is no editor and the chart
// keeps its markets.json markers; on the local dev server without an API the
// editor keeps markers in memory until the page reloads, as before.
import { useEffect, useLayoutEffect, useMemo, useRef, useState, type FormEvent } from "react";
import type { AddedEvent, MarketData, MarketEvent } from "../../types";
import { fmtT } from "../../lib/format";
import { OFFLINE, WORDS, apiConfigured, apiGet, apiSend, dropUndo, localMarkers, saveUndo, setLocalMarkers, undoOf, type Marker } from "../../lib/api";

const MAX_LABEL = 40;

/**
 * The market with its added markers merged into timeline.events (by time).
 * Markers outside `bounds` (the market's own span) are left out, so none can
 * stretch the chart.
 */
export function withAddedEvents(market: MarketData, added: AddedEvent[], bounds: readonly [number, number]): MarketData {
  const inside = added.filter((e) => e.t >= bounds[0] && e.t <= bounds[1]);
  if (!inside.length) return market;
  const events: MarketEvent[] = [...market.timeline.events, ...inside.map(({ t, label }) => ({ t, label }))].sort(
    (a, b) => a.t - b.t,
  );
  return { ...market, timeline: { ...market.timeline, events } };
}

/**
 * "off": the markers are kept in this browser (no API in the build, or the
 * API doesn't answer for this market); "loading"; "ready": the API's markers.
 */
export type MarkerStatus = "off" | "loading" | "ready";

type MarkersReply = { listed: boolean; writes: boolean; markers: Marker[] };
type Kept = { events: AddedEvent[]; writes: boolean };

/** This page's markers per market, so switching views doesn't ask again (the dev server's only copy without an API). */
const session = new Map<string, Kept>();

const byTime = (a: AddedEvent, b: AddedEvent) => a.t - b.t;
const fromMarker = (m: Marker): AddedEvent => ({ id: m.id, t: m.t, label: m.label, source: m.source, created_at: m.created_at });

export type AddedEvents = {
  events: AddedEvent[];
  status: MarkerStatus;
  /** false while the owner has paused visitors' changes (adds are then kept in this browser) */
  writes: boolean;
  /** add a marker; an error in words, or null once it is added */
  add: (t: number, label: string, slot?: HTMLElement | null) => Promise<string | null>;
  /** remove one of this browser's markers; an error in words, or null once it is gone */
  remove: (id: string) => Promise<string | null>;
  /** this browser added it, less than 24 hours ago */
  canRemove: (id: string) => boolean;
};

export function useAddedEvents(name: string): AddedEvents {
  const [state, setState] = useState<{ name: string; status: MarkerStatus } & Kept>(() => {
    const kept = session.get(name);
    if (!apiConfigured) return { name, status: "off", events: kept?.events ?? localMarkers<AddedEvent>(name), writes: true };
    return kept ? { name, status: "ready", ...kept } : { name, status: "loading", events: [], writes: true };
  });
  // a new market's first render, before the effect below has caught up
  const fresh = { name, status: (apiConfigured ? "loading" : "off") as MarkerStatus, events: [] as AddedEvent[], writes: true };
  const current = state.name === name ? state : fresh;

  useEffect(() => {
    const kept = session.get(name);
    if (!apiConfigured) {
      setState({ name, status: "off", events: kept?.events ?? localMarkers<AddedEvent>(name), writes: true });
      return;
    }
    if (kept) {
      setState({ name, status: "ready", ...kept });
      return;
    }
    let live = true;
    apiGet<MarkersReply>(`/api/markers?market=${encodeURIComponent(name)}`).then((r) => {
      if (!live) return;
      if (!r || r.listed !== true || !Array.isArray(r.markers)) {
        setState({ name, status: "off", events: localMarkers<AddedEvent>(name), writes: true });
        return;
      }
      const next: Kept = { events: r.markers.map(fromMarker).sort(byTime), writes: r.writes !== false };
      session.set(name, next);
      setState({ name, status: "ready", ...next });
    });
    return () => {
      live = false;
    };
  }, [name]);

  const keep = (update: (k: Kept) => Kept, here = false) =>
    setState((s) => {
      if (s.name !== name) return s;
      const next = update({ events: s.events, writes: s.writes });
      session.set(name, next);
      if (here || s.status === "off") setLocalMarkers(name, next.events.filter((e) => e.id.startsWith("ev_")));
      return { ...s, ...next, status: here ? "off" : s.status };
    });

  const add = async (t: number, label: string, slot?: HTMLElement | null): Promise<string | null> => {
    const here = () => {
      const e: AddedEvent = { id: `ev_${Date.now().toString(36)}`, t, label, source: "manual" };
      keep((k) => ({ ...k, events: [...k.events, e].sort(byTime) }), true);
      return null;
    };
    if (current.status === "off") return here();
    const r = await apiSend<{ marker: Marker; undo: string }>("POST", "/api/markers", { market: name, t, label }, { bot: "marker", slot });
    if (!r.ok) return OFFLINE.has(r.code) ? here() : r.words;
    if (!r.data?.marker) return WORDS.other;
    if (typeof r.data.undo === "string") saveUndo(r.data.marker.id, r.data.undo);
    const m = fromMarker(r.data.marker);
    keep((k) => ({ ...k, events: [...k.events.filter((e) => e.id !== m.id), m].sort(byTime) }));
    return null;
  };

  const remove = async (id: string): Promise<string | null> => {
    if (current.status === "off" || id.startsWith("ev_")) {
      keep((k) => ({ ...k, events: k.events.filter((e) => e.id !== id) }), true);
      return null;
    }
    const undo = undoOf(id);
    if (!undo) return WORDS.not_yours;
    const r = await apiSend<null>("DELETE", `/api/markers/${encodeURIComponent(id)}`, undefined, { headers: { "x-undo": undo } });
    if (!r.ok && r.code !== "not_found") return r.words;
    dropUndo(id);
    keep((k) => ({ ...k, events: k.events.filter((e) => e.id !== id) }));
    return null;
  };

  const canRemove = (id: string) => current.status === "off" || id.startsWith("ev_") || undoOf(id) != null;

  return { events: current.events, status: current.status, writes: current.writes, add, remove, canRemove };
}

/* `t` is the US/Eastern wall clock read as UTC (types.ts), so a
   datetime-local value converts with UTC arithmetic and means Eastern time. */
const toInput = (t: number) => new Date(t * 1000).toISOString().slice(0, 16);
const fromInput = (v: string) => {
  const ms = Date.parse(`${v}:00Z`);
  return Number.isFinite(ms) ? Math.round(ms / 1000) : null;
};

/** The tag on a row: where the marker came from. */
const FROM: Record<AddedEvent["source"], string> = { manual: "added", ai: "AI" };

/**
 * The editor: the market's markers (the ones from markets.json, "original",
 * can't be removed here; a visitor's own, for 24 hours, can) and a form to
 * add one, its time starting at the replay playhead so scrubbing to a moment
 * and pressing Add marker is enough. Times stay inside `bounds`, the
 * market's span.
 */
export function EventsEditor({
  fixed,
  api,
  bounds,
  playhead,
}: {
  fixed: MarketEvent[];
  api: AddedEvents;
  bounds: readonly [number, number];
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
  const slot = useRef<HTMLSpanElement>(null);
  const panel = useRef<HTMLDivElement>(null);

  const rows = useMemo(
    () =>
      [
        ...fixed.map((e) => ({ key: `f${e.t}${e.label}`, t: e.t, label: e.label, from: "original", id: null as string | null })),
        ...api.events.map((e) => ({ key: e.id, t: e.t, label: e.label, from: FROM[e.source], id: e.id })),
      ].sort((a, b) => a.t - b.t),
    [fixed, api.events],
  );

  const clamp = (t: number) => Math.min(bounds[1], Math.max(bounds[0], t));
  const show = () => {
    setWhen(toInput(clamp(playhead())));
    setError(null);
    setOpen(true);
  };

  // the panel hangs from the button's right edge; on a phone the button sits left of the screen's edge,
  // so the panel is nudged back inside the page's 16px margins
  useLayoutEffect(() => {
    const el = panel.current;
    if (!open || !el) return;
    el.style.transform = "";
    const r = el.getBoundingClientRect();
    const shift = r.left < 16 ? 16 - r.left : r.right > window.innerWidth - 16 ? window.innerWidth - 16 - r.right : 0;
    if (shift) el.style.transform = `translateX(${Math.round(shift)}px)`;
  }, [open]);

  useEffect(() => {
    if (!open) return;
    labelInput.current?.focus();
    const away = (e: PointerEvent) => {
      if (root.current && !root.current.contains(e.target as Node)) setOpen(false);
    };
    window.addEventListener("pointerdown", away);
    return () => window.removeEventListener("pointerdown", away);
  }, [open]);

  const run = async (job: () => Promise<string | null>) => {
    setBusy(true);
    setError(null);
    const err = await job();
    setBusy(false);
    setError(err);
    return !err;
  };

  const add = async (e: FormEvent) => {
    e.preventDefault();
    const t = fromInput(when);
    const text = label.trim().replace(/\s+/g, " ");
    if (!text || t == null || busy) return;
    if (t < bounds[0] || t > bounds[1]) {
      setError(WORDS.bad_time);
      return;
    }
    if (await run(() => api.add(t, text, slot.current))) setLabel("");
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
        title="Add a marker to this timeline: what happened, and when (ET)"
      >
        Events
        {rows.length > 0 && <span className="tl-ev-count tabular">{rows.length}</span>}
      </button>

      {open && (
        <div className="tl-ev-panel" ref={panel} role="dialog" aria-label="Timeline events">
          <p className="label">Events on this timeline</p>
          {rows.length > 0 ? (
            <ul className="tl-ev-list">
              {rows.map((r) => (
                <li key={r.key}>
                  <span className="tl-ev-time tabular">{fmtT(r.t)}</span>
                  <span className="tl-ev-label" title={r.label}>
                    {r.label}
                  </span>
                  {r.id && api.canRemove(r.id) ? (
                    <button
                      type="button"
                      className="tl-ev-remove"
                      disabled={busy}
                      onClick={() => run(() => api.remove(r.id!))}
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

          {/* noValidate: a time outside the market's span gets this page's words, not the browser's bubble */}
          <form className="tl-ev-form" onSubmit={add} noValidate>
            <fieldset className="tl-ev-fields">
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
                <input
                  type="datetime-local"
                  value={when}
                  min={toInput(bounds[0])}
                  max={toInput(bounds[1])}
                  onChange={(e) => setWhen(e.target.value)}
                />
              </label>
              <div className="tl-ev-actions">
                <button type="button" className="tl-ev-link" onClick={() => setWhen(toInput(clamp(playhead())))}>
                  Use the replay time
                </button>
                <button type="submit" className="btn btn-solid btn-sm" disabled={busy || !label.trim() || !fromInput(when)}>
                  {busy ? "Adding…" : "Add marker"}
                </button>
              </div>
            </fieldset>
          </form>
          <span className="ts-slot" ref={slot} />
          {error && (
            <p className="tl-ev-error" role="alert">
              {error}
            </p>
          )}
          <p className="tl-ev-note">Markers you add are shown to everyone. You can remove yours for 24 hours.</p>
        </div>
      )}
    </div>
  );
}
