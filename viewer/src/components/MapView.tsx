// One run's map: a canvas that redraws whenever the clock or the view changes.

import { useEffect, useRef, useState } from "react";
import { drawMap, type View } from "../draw";
import { beliefAt, type MapIndex } from "../logic";
import type { Palette } from "../palette";
import type { Run, Scenario } from "../types";

interface Props {
  label: string; // "A" or "B"
  scenario: Scenario;
  index: MapIndex;
  run: Run;
  time: number;
  view: View;
  highlight: Set<string>;
  palette: Palette;
}

export function MapView({ label, scenario, index, run, time, view, highlight, palette }: Props) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const boxRef = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState({ width: 600, height: 480 });
  // Redraw a few times a second even while paused, so low-fuel bars keep flashing.
  const [, setPulse] = useState(0);
  useEffect(() => {
    const timer = setInterval(() => setPulse((n) => n + 1), 400);
    return () => clearInterval(timer);
  }, []);

  // Follow the size of the box the canvas sits in.
  useEffect(() => {
    const box = boxRef.current;
    if (!box) return;
    const observer = new ResizeObserver(([entry]) => {
      const { width, height } = entry.contentRect;
      setSize({ width: Math.max(200, Math.floor(width)), height: Math.max(200, Math.floor(height)) });
    });
    observer.observe(box);
    return () => observer.disconnect();
  }, []);

  // A run that ended early (nothing more could happen) just stays on its last frame.
  const last = run.frames.length - 1;
  const tick = Math.max(0, Math.min(Math.floor(time), last));
  const fraction = tick < last ? time - tick : 0;
  const frame = run.frames[tick];
  const next = run.frames[Math.min(tick + 1, last)];
  const belief = beliefAt(run, tick);

  useEffect(() => {
    const canvas = canvasRef.current;
    const ctx = canvas?.getContext("2d");
    if (!canvas || !ctx) return;
    // Draw at the screen's real pixel density so lines stay sharp.
    const dpr = window.devicePixelRatio || 1;
    if (canvas.width !== size.width * dpr || canvas.height !== size.height * dpr) {
      canvas.width = size.width * dpr;
      canvas.height = size.height * dpr;
    }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    drawMap(ctx, {
      scenario,
      index,
      frame,
      next,
      fraction,
      view,
      belief,
      highlight,
      palette,
      width: size.width,
      height: size.height,
      now: performance.now(),
    });
  });

  const downtime = frame.hospitals.reduce((sum, h) => sum + h.dark_min, 0);
  const ended = time >= last && last < scenario.horizon;
  return (
    <figure className="map">
      <figcaption>
        <span className={`run-tag run-${label.toLowerCase()}`}>{label}</span>
        <strong>{run.contestant}</strong>
        <span className="muted"> {run.run_id.split("/").pop()}</span>
        <span className="map-stat">
          downtime so far <strong>{downtime} min</strong>
          {ended ? ` · ended at t=${run.end.tick} (${run.end.reason})` : ""}
        </span>
      </figcaption>
      {view === "belief" && (
        <p className="view-note">
          {belief
            ? `What ${run.contestant} believed after its decision at t=${belief.tick}`
            : `${run.contestant} keeps no belief: showing nothing believed`}
        </p>
      )}
      <div className="canvas-box" ref={boxRef}>
        <canvas
          ref={canvasRef}
          style={{ width: size.width, height: size.height }}
          aria-label={`Map of ${scenario.id} at minute ${tick}, run ${label}`}
        />
      </div>
    </figure>
  );
}
