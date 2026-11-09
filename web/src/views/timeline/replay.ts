// Market replay: a playhead that runs the visible x-domain forward in time.
//
// The engine is a tiny external store. Per-frame work (the playhead, the
// reveal clip, which dots have appeared) goes to `onFrame` listeners that
// write to the DOM directly, so playback never re-renders React. Discrete
// changes (play / pause / speed / checkpoint / the end) bump `version` and
// notify `subscribe` listeners, which is what React components watch.
import { useCallback, useLayoutEffect, useSyncExternalStore } from "react";
import type { Domain } from "./model";

export type Speed = "1" | "4" | "16";
export const SPEEDS: { value: Speed; label: string }[] = [
  { value: "1", label: "1×" },
  { value: "4", label: "4×" },
  { value: "16", label: "16×" },
];
/** At 1× the visible domain plays in this many seconds. */
export const BASE_SECONDS = 40;
/** How long the playhead rests on an event (a checkpoint), ms. */
export const CHECKPOINT_MS = 700;
/** Scrubber resolution (steps across the visible domain). */
export const SCRUB_STEPS = 1000;

const EPS = 1e-6;

type Listener = () => void;
type FrameListener = (t: number) => void;

export class ReplayEngine {
  private d: Domain;
  private evs: number[];
  /** Playhead, exported seconds. Equal to the domain end while no replay is in progress. */
  t: number;
  playing = false;
  speed: Speed = "4";
  /** Event time the playhead is resting on (a checkpoint), while it rests. */
  checkpoint: number | null = null;
  /** Bumped on every discrete change; React components subscribe to it. */
  version = 0;

  private raf = 0;
  private last = 0;
  private holdUntil = 0;
  private frameFns = new Set<FrameListener>();
  private stateFns = new Set<Listener>();
  private quietTimer = 0;

  constructor(domain: Domain, events: number[], t: number | null = null) {
    this.d = domain;
    this.evs = [...events].sort((a, b) => a - b);
    this.t = t != null && t >= domain[0] && t < domain[1] ? t : domain[1];
  }

  get domain(): Domain {
    return this.d;
  }

  /** A replay is in progress (playing or paused before the end): the chart reveals up to `t`. */
  get active(): boolean {
    return this.t < this.d[1] - EPS;
  }

  /** 0 at the domain start, 1 at its end. */
  get progress(): number {
    const span = this.d[1] - this.d[0];
    return span > 0 ? Math.min(1, Math.max(0, (this.t - this.d[0]) / span)) : 1;
  }

  /* ── inputs from the view ─────────────────────────────────────────── */

  /** The visible domain moved (zoom / brush / preset). Playback pauses; a paused playhead stays inside. */
  setDomain(d: Domain) {
    if (d[0] === this.d[0] && d[1] === this.d[1]) return;
    const wasActive = this.active;
    this.d = d;
    if (!wasActive) {
      this.t = d[1];
      this.emitFrame();
      return;
    }
    if (this.playing) this.pause();
    this.seek(this.t, true);
  }

  setEvents(events: number[]) {
    this.evs = [...events].sort((a, b) => a - b);
  }

  /* ── transport ────────────────────────────────────────────────────── */

  play() {
    if (this.playing) return;
    if (!this.active) this.t = this.d[0];
    this.playing = true;
    this.checkpoint = null;
    this.holdUntil = 0;
    this.last = 0;
    this.emitFrame();
    this.emitState();
    this.raf = requestAnimationFrame(this.tick);
  }

  pause() {
    if (!this.playing) return;
    this.playing = false;
    this.checkpoint = null;
    cancelAnimationFrame(this.raf);
    this.raf = 0;
    this.emitState();
  }

  toggle() {
    if (this.playing) this.pause();
    else this.play();
  }

  /** Move the playhead. `quiet` batches the React notification (scrubber drags). */
  seek(t: number, quiet = false) {
    const [d0, d1] = this.d;
    const next = Math.min(d1, Math.max(d0, t));
    const wasActive = this.active;
    this.t = next;
    this.checkpoint = null;
    this.holdUntil = 0;
    if (!this.active && this.playing) {
      this.playing = false;
      cancelAnimationFrame(this.raf);
      this.raf = 0;
      quiet = false;
    }
    this.emitFrame();
    if (!quiet || wasActive !== this.active) this.emitState();
    else this.emitStateQuiet();
  }

  /** Step the playhead by 1% of the domain (10% with `big`). */
  nudge(dir: 1 | -1, big = false) {
    const span = this.d[1] - this.d[0];
    this.seek(this.t + dir * span * (big ? 0.1 : 0.01));
  }

  /** Leave the replay: everything visible again. */
  reset() {
    this.playing = false;
    this.checkpoint = null;
    cancelAnimationFrame(this.raf);
    this.raf = 0;
    this.t = this.d[1];
    this.emitFrame();
    this.emitState();
  }

  setSpeed(s: Speed) {
    if (s === this.speed) return;
    this.speed = s;
    this.emitState();
  }

  destroy() {
    cancelAnimationFrame(this.raf);
    window.clearTimeout(this.quietTimer);
    this.frameFns.clear();
    this.stateFns.clear();
  }

  /* ── listeners ────────────────────────────────────────────────────── */

  onFrame(fn: FrameListener): () => void {
    this.frameFns.add(fn);
    return () => {
      this.frameFns.delete(fn);
    };
  }

  subscribe = (fn: Listener): (() => void) => {
    this.stateFns.add(fn);
    return () => {
      this.stateFns.delete(fn);
    };
  };

  getVersion = () => this.version;

  private emitFrame() {
    for (const fn of this.frameFns) fn(this.t);
  }

  private emitState() {
    window.clearTimeout(this.quietTimer);
    this.quietTimer = 0;
    this.version++;
    for (const fn of this.stateFns) fn();
  }

  private emitStateQuiet() {
    if (this.quietTimer) return;
    this.quietTimer = window.setTimeout(() => {
      this.quietTimer = 0;
      this.emitState();
    }, 120);
  }

  /* ── the loop ─────────────────────────────────────────────────────── */

  private tick = (now: number) => {
    if (!this.playing) return;
    this.raf = requestAnimationFrame(this.tick);
    if (now < this.holdUntil) return;
    if (this.checkpoint != null) {
      // the rest is over: carry on past the event
      this.checkpoint = null;
      this.emitState();
    }
    const dt = this.last ? Math.min(now - this.last, 100) / 1000 : 0;
    this.last = now;
    const [d0, d1] = this.d;
    const rate = ((d1 - d0) / BASE_SECONDS) * Number(this.speed);
    const next = this.t + dt * rate;
    // Events are checkpoints: the playhead stops on one for a moment.
    for (const e of this.evs) {
      if (e > this.t && e <= next && e >= d0 && e < d1) {
        this.t = e;
        this.checkpoint = e;
        this.holdUntil = now + CHECKPOINT_MS;
        this.emitFrame();
        this.emitState();
        return;
      }
    }
    if (next >= d1 - EPS) {
      this.t = d1;
      this.playing = false;
      cancelAnimationFrame(this.raf);
      this.raf = 0;
      this.emitFrame();
      this.emitState();
      return;
    }
    this.t = next;
    this.emitFrame();
  };
}

/** Re-render on the engine's discrete changes; returns the engine for reading. */
export function useReplaySnapshot(engine: ReplayEngine): ReplayEngine {
  useSyncExternalStore(engine.subscribe, engine.getVersion, engine.getVersion);
  return engine;
}

/**
 * Run `fn` on every playhead move, and once (before paint) whenever `deps`
 * change. `fn` writes to the DOM directly; keep it cheap.
 */
export function useReplayFrames(engine: ReplayEngine, fn: FrameListener, deps: readonly unknown[]) {
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const cb = useCallback(fn, deps);
  useLayoutEffect(() => {
    cb(engine.t);
    return engine.onFrame(cb);
  }, [engine, cb]);
}
