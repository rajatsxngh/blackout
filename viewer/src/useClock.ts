// The playback clock. `time` is in ticks (simulated minutes) and may be fractional, so trucks
// move smoothly between ticks. At speed 1x one tick takes one second; at 60x, 1/60 s.

import { useCallback, useEffect, useRef, useState } from "react";

export const SPEEDS = [1, 5, 20, 60] as const;
export type Speed = (typeof SPEEDS)[number];

export interface Clock {
  time: number;
  playing: boolean;
  speed: Speed;
  setTime: (t: number) => void;
  setPlaying: (playing: boolean) => void;
  setSpeed: (speed: Speed) => void;
}

export function useClock(maxTick: number, initial = 0): Clock {
  const [time, setTimeState] = useState(initial);
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState<Speed>(20);

  // Keep the latest values in refs so the animation loop doesn't restart on every frame.
  const maxRef = useRef(maxTick);
  maxRef.current = maxTick;
  const timeRef = useRef(initial);

  const setTime = useCallback((t: number) => {
    timeRef.current = Math.max(0, Math.min(maxRef.current, t));
    setTimeState(timeRef.current);
  }, []);

  useEffect(() => {
    if (!playing) return;
    let last = performance.now();
    let frame = requestAnimationFrame(function step(now) {
      // The first frame's timestamp can be a little earlier than `last`: never step backwards.
      const seconds = Math.max(0, now - last) / 1000;
      last = now;
      const next = Math.min(maxRef.current, timeRef.current + seconds * speed);
      timeRef.current = next;
      setTimeState(next);
      if (next >= maxRef.current) setPlaying(false); // stop at the end
      else frame = requestAnimationFrame(step);
    });
    return () => cancelAnimationFrame(frame);
  }, [playing, speed]);

  return { time, playing, speed, setTime, setPlaying, setSpeed };
}
