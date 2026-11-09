// Play / pause, speed, step, jumps, and a scrubber with decision ticks and the divergence
// marker drawn on it.

import { nextAfter, previousBefore, type Divergence } from "../logic";
import { SPEEDS, type Clock } from "../useClock";

interface Props {
  clock: Clock;
  maxTick: number;
  decisionTicks: number[]; // run A's decision points (and B's, in side-by-side)
  eventTicks: number[];
  divergence: Divergence | null;
}

export function Timeline({ clock, maxTick, decisionTicks, eventTicks, divergence }: Props) {
  const { time, playing, speed, setTime, setPlaying, setSpeed } = clock;
  const tick = Math.floor(time);
  const jump = (t: number | null) => {
    if (t === null) return;
    setPlaying(false);
    setTime(t);
  };
  const percent = (t: number) => `${(100 * t) / Math.max(1, maxTick)}%`;

  return (
    <section className="timeline" aria-label="Timeline">
      <div className="controls">
        <button onClick={() => jump(previousBefore(decisionTicks, tick))} title="Previous decision">
          ⏮
        </button>
        <button onClick={() => jump(tick - 1)} title="Step back one minute (←)">
          ◀
        </button>
        <button
          className="play"
          onClick={() => {
            if (!playing && time >= maxTick) setTime(0); // play again from the start
            setPlaying(!playing);
          }}
          title="Play / pause (space)"
        >
          {playing ? "❚❚ Pause" : "▶ Play"}
        </button>
        <button onClick={() => jump(tick + 1)} title="Step forward one minute (→)">
          ▶
        </button>
        <button onClick={() => jump(nextAfter(decisionTicks, tick))} title="Next decision">
          ⏭ decision
        </button>
        <button onClick={() => jump(nextAfter(eventTicks, tick))} title="Next event, report or action">
          ⏭ event
        </button>
        {divergence && (
          <button className="diverge-btn" onClick={() => jump(divergence.tick)} title="First different action">
            ◆ divergence t={divergence.tick}
          </button>
        )}
        <span className="speeds" role="group" aria-label="Speed">
          {SPEEDS.map((s) => (
            <button key={s} className={s === speed ? "active" : ""} onClick={() => setSpeed(s)}>
              {s}×
            </button>
          ))}
        </span>
        <span className="clock">
          t = <strong>{tick}</strong> / {maxTick} min
        </span>
      </div>
      <div className="scrubber">
        <div className="marks" aria-hidden="true">
          {decisionTicks.map((t) => (
            <span key={t} className="mark" style={{ left: percent(t) }} />
          ))}
          {divergence && (
            <span className="diverge" style={{ left: percent(divergence.tick) }} title="First different action">
              ◆
            </span>
          )}
        </div>
        <input
          type="range"
          min={0}
          max={maxTick}
          step="any"
          value={time}
          aria-label="Minute"
          onChange={(e) => {
            setPlaying(false);
            setTime(Number(e.target.value));
          }}
        />
      </div>
    </section>
  );
}
