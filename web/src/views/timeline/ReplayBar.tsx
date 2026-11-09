import { useRef, type CSSProperties, type FormEvent } from "react";
import { Segmented } from "../../components/ui";
import { fmtT } from "../../lib/format";
import { SCRUB_STEPS, SPEEDS, useReplayFrames, useReplaySnapshot, type ReplayEngine, type Speed } from "./replay";

/**
 * Transport for the market replay: play / pause, speed, a scrubber across the
 * visible domain, the playhead clock and a way out. The scrubber and clock
 * follow the playhead through DOM writes (no React render per frame).
 */
export function ReplayBar({ engine }: { engine: ReplayEngine }) {
  const r = useReplaySnapshot(engine);
  const input = useRef<HTMLInputElement>(null);
  const time = useRef<HTMLSpanElement>(null);

  useReplayFrames(
    engine,
    (t) => {
      const p = engine.progress;
      const el = input.current;
      if (el) {
        el.value = String(Math.round(p * SCRUB_STEPS));
        el.style.setProperty("--p", `${(p * 100).toFixed(2)}%`);
        el.setAttribute("aria-valuetext", fmtT(t));
      }
      if (time.current) time.current.textContent = fmtT(t);
    },
    [engine],
  );

  const onScrub = (e: FormEvent<HTMLInputElement>) => {
    const [d0, d1] = engine.domain;
    engine.seek(d0 + (Number(e.currentTarget.value) / SCRUB_STEPS) * (d1 - d0), true);
  };

  return (
    <div className={`tl-transport${r.active ? " on" : ""}${r.playing ? " playing" : ""}`} role="group" aria-label="Market replay">
      <button
        type="button"
        className={`btn btn-sm tl-play ${r.playing ? "btn-solid" : "btn-ghost"}`}
        onClick={() => engine.toggle()}
        aria-label={r.playing ? "Pause replay" : "Play replay"}
        title="Play / pause the replay (Space)"
      >
        <svg width="10" height="10" viewBox="0 0 12 12" aria-hidden>
          {r.playing ? (
            <path d="M2.3 1.5h2.6a.6.6 0 0 1 .6.6v7.8a.6.6 0 0 1-.6.6H2.3a.6.6 0 0 1-.6-.6V2.1a.6.6 0 0 1 .6-.6Zm4.8 0h2.6a.6.6 0 0 1 .6.6v7.8a.6.6 0 0 1-.6.6H7.1a.6.6 0 0 1-.6-.6V2.1a.6.6 0 0 1 .6-.6Z" />
          ) : (
            <path d="M3.2 1.9a.7.7 0 0 1 1.06-.6l6.2 4.1a.7.7 0 0 1 0 1.2l-6.2 4.1a.7.7 0 0 1-1.06-.6V1.9Z" />
          )}
        </svg>
        {r.playing ? "Pause" : "Play"}
      </button>
      <div className="tl-speed">
        <Segmented<Speed> value={r.speed} options={SPEEDS} onChange={(s) => engine.setSpeed(s)} label="Replay speed" />
      </div>
      <input
        ref={input}
        type="range"
        className="tl-scrub"
        min={0}
        max={SCRUB_STEPS}
        step={1}
        defaultValue={Math.round(engine.progress * SCRUB_STEPS)}
        onInput={onScrub}
        aria-label="Replay position"
        aria-valuetext={fmtT(engine.t)}
        style={{ "--p": `${(engine.progress * 100).toFixed(2)}%` } as CSSProperties}
      />
      <span ref={time} className="tl-transport-time tabular" aria-live={r.playing ? "off" : "polite"}>
        {fmtT(engine.t)}
      </span>
      <button
        type="button"
        className="btn btn-ghost btn-sm tl-transport-reset"
        onClick={() => engine.reset()}
        disabled={!r.active}
        title="Leave the replay and show everything (Esc)"
      >
        Reset
      </button>
    </div>
  );
}
