// The whole viewer. `App` loads the manifest and handles the run picker and deep links;
// `Player` gets the loaded scenario and runs and owns the clock, the maps and the panels.

import { useCallback, useEffect, useMemo, useState } from "react";
import { DecisionsPanel } from "./components/DecisionsPanel";
import { Legend } from "./components/Legend";
import { MapView } from "./components/MapView";
import { MetricsPanel } from "./components/MetricsPanel";
import { ReportsPanel } from "./components/ReportsPanel";
import { Timeline } from "./components/Timeline";
import { loadManifest, loadRun, loadScenario } from "./data";
import type { View } from "./draw";
import { buildIndex, buildLink, decisionTicks, eventTicks, firstDivergence, parseLink, type Link } from "./logic";
import { palettes, type Theme } from "./palette";
import type { Manifest, Run, Scenario } from "./types";
import { useClock } from "./useClock";

interface Choice {
  s: string;
  a: string;
  b: string | null;
}

interface Loaded {
  choice: Choice;
  scenario: Scenario;
  runA: Run;
  runB: Run | null;
}

export function App() {
  const [manifest, setManifest] = useState<Manifest | null>(null);
  const [choice, setChoice] = useState<Choice | null>(null);
  const [loaded, setLoaded] = useState<Loaded | null>(null);
  const [error, setError] = useState<string | null>(null);
  // The tick from the page's link, used once for the first run shown.
  const [linkTick, setLinkTick] = useState<number | null>(null);
  const theme = useTheme();

  // 1. Load the manifest and pick what the link asks for (or the first scenario, side by side).
  useEffect(() => {
    loadManifest()
      .then((m) => {
        setManifest(m);
        const link = parseLink(window.location.search);
        setChoice(chooseFromLink(m, link));
        setLinkTick(link.t);
      })
      .catch((e: Error) =>
        setError(`${e.message}. Export some runs first: uv run blackout export-viewer <experiment> --select <file>`),
      );
  }, []);

  // 2. Load the chosen scenario and runs.
  useEffect(() => {
    if (!manifest || !choice) return;
    const entry = manifest.scenarios.find((s) => s.id === choice.s);
    const runA = entry?.runs.find((r) => r.id === choice.a);
    const runB = entry?.runs.find((r) => r.id === choice.b);
    if (!entry || !runA) return;
    let cancelled = false;
    Promise.all([loadScenario(entry.file), loadRun(runA.file), runB ? loadRun(runB.file) : Promise.resolve(null)])
      .then(([scenario, a, b]) => {
        if (!cancelled) setLoaded({ choice, scenario, runA: a, runB: b });
      })
      .catch((e: Error) => setError(e.message));
    return () => {
      cancelled = true;
    };
  }, [manifest, choice]);

  if (error) return <p className="error">Couldn't load the replays: {error}</p>;
  if (!manifest || !choice) return <p className="loading">Loading replays…</p>;

  const entry = manifest.scenarios.find((s) => s.id === choice.s) ?? manifest.scenarios[0];
  const pick = (next: Partial<Choice>) => {
    setLinkTick(null);
    setChoice({ ...choice, ...next });
  };

  return (
    <div className="app">
      <header className="top">
        <h1>
          Blackout <span className="muted">replays</span>
        </h1>
        <div className="picker">
          <label>
            scenario{" "}
            <select
              value={choice.s}
              onChange={(e) => {
                const s = manifest.scenarios.find((x) => x.id === e.target.value)!;
                pick({ s: s.id, a: s.runs[0].id, b: s.runs[1]?.id ?? null });
              }}
            >
              {manifest.scenarios.map((s) => (
                <option key={s.id} value={s.id}>
                  {s.id}
                  {s.tier ? ` (${s.tier})` : ""}
                </option>
              ))}
            </select>
          </label>
          <label>
            <span className="run-tag run-a">A</span>{" "}
            <select value={choice.a} onChange={(e) => pick({ a: e.target.value })}>
              {entry.runs.map((r) => (
                <option key={r.id} value={r.id}>
                  {r.contestant} · {r.critical_downtime_min ?? "?"} min dark
                </option>
              ))}
            </select>
          </label>
          <label>
            <span className="run-tag run-b">B</span>{" "}
            <select value={choice.b ?? ""} onChange={(e) => pick({ b: e.target.value || null })}>
              <option value="">none (one map)</option>
              {entry.runs.map((r) => (
                <option key={r.id} value={r.id}>
                  {r.contestant} · {r.critical_downtime_min ?? "?"} min dark
                </option>
              ))}
            </select>
          </label>
        </div>
        <button className="theme" onClick={theme.toggle} title="Light / dark theme">
          {theme.resolved === "dark" ? "☀ light" : "☾ dark"}
        </button>
      </header>
      {entry.note && <p className="note">{entry.note}</p>}
      {loaded && sameChoice(loaded.choice, choice) ? (
        <Player
          key={`${choice.s}|${choice.a}|${choice.b}`}
          loaded={loaded}
          initialTick={linkTick ?? 0}
          theme={theme.resolved}
        />
      ) : (
        <p className="loading">Loading {choice.s}…</p>
      )}
      <footer className="muted small">
        {manifest.title} · experiment <span className="mono">{manifest.experiment}</span> · every run was re-simulated
        and hash-checked when exported · a research sandbox, not emergency software
      </footer>
    </div>
  );
}

function Player({ loaded, initialTick, theme }: { loaded: Loaded; initialTick: number; theme: Theme }) {
  const { scenario, runA, runB, choice } = loaded;
  const maxTick = Math.max(runA.frames.length, runB?.frames.length ?? 0) - 1;
  const clock = useClock(maxTick, Math.min(initialTick, maxTick));
  const [view, setView] = useState<View>("truth");
  const [highlight, setHighlight] = useState<Set<string>>(new Set());
  const tick = Math.floor(clock.time);
  const palette = palettes[theme];

  const index = useMemo(() => buildIndex(scenario), [scenario]);
  const runs = useMemo(
    () => [{ label: "A", run: runA }, ...(runB ? [{ label: "B", run: runB }] : [])],
    [runA, runB],
  );
  const divergence = useMemo(() => (runB ? firstDivergence(runA, runB) : null), [runA, runB]);
  const decisions = useMemo(
    () => [...new Set(runs.flatMap(({ run }) => decisionTicks(run)))].sort((x, y) => x - y),
    [runs],
  );
  const events = useMemo(
    () => [...new Set(runs.flatMap(({ run }) => eventTicks(run)))].sort((x, y) => x - y),
    [runs],
  );

  // Keep the address bar's deep link in step (only while paused, so playing doesn't spam history).
  useEffect(() => {
    if (clock.playing) return;
    const link = buildLink({ s: choice.s, a: choice.a, b: choice.b, t: tick });
    window.history.replaceState(null, "", link || window.location.pathname);
  }, [choice, tick, clock.playing]);

  // Keyboard: space plays / pauses, arrows step a minute.
  const { setPlaying, setTime, playing } = clock;
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement;
      if (target.tagName === "SELECT" || target.tagName === "INPUT") return;
      if (e.key === " ") {
        e.preventDefault();
        setPlaying(!playing);
      } else if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
        e.preventDefault();
        setPlaying(false);
        setTime(Math.floor(clock.time) + (e.key === "ArrowRight" ? 1 : -1));
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [playing, setPlaying, setTime, clock.time]);

  const onPick = useCallback(
    (t: number, entities: string[]) => {
      setPlaying(false);
      setTime(t);
      setHighlight(new Set(entities));
    },
    [setPlaying, setTime],
  );

  return (
    <main>
      <div className="toolbar">
        <span role="group" aria-label="Map view" className="segmented">
          <button className={view === "truth" ? "active" : ""} onClick={() => setView("truth")}>
            truth
          </button>
          <button className={view === "belief" ? "active" : ""} onClick={() => setView("belief")}>
            what it believed
          </button>
        </span>
        {highlight.size > 0 && (
          <button onClick={() => setHighlight(new Set())}>clear highlight ({[...highlight].join(", ")})</button>
        )}
        <span className="muted small">
          {scenario.template ?? "hand-made"} · L{scenario.level}
          {scenario.tier ? ` · ${scenario.tier}` : ""} · horizon {scenario.horizon} min
        </span>
        <Legend palette={palette} />
      </div>
      <div className={`maps ${runB ? "two" : "one"}`}>
        {runs.map(({ label, run }) => (
          <MapView
            key={label}
            label={label}
            scenario={scenario}
            index={index}
            run={run}
            time={clock.time}
            view={view}
            highlight={highlight}
            palette={palette}
          />
        ))}
      </div>
      <Timeline clock={clock} maxTick={maxTick} decisionTicks={decisions} eventTicks={events} divergence={divergence} />
      <div className="panels">
        <DecisionsPanel runs={runs} tick={tick} divergence={divergence} onPick={onPick} />
        <ReportsPanel scenario={scenario} run={runA} tick={tick} />
        <MetricsPanel scenario={scenario} runs={runs} tick={tick} />
      </div>
    </main>
  );
}

// ---- helpers --------------------------------------------------------------------------------

function sameChoice(x: Choice, y: Choice): boolean {
  return x.s === y.s && x.a === y.a && x.b === y.b;
}

/** What to show first: the link's scenario and runs if they exist, else the first scenario
 * with its first two runs side by side. */
function chooseFromLink(manifest: Manifest, link: Link): Choice {
  const entry = manifest.scenarios.find((s) => s.id === link.s) ?? manifest.scenarios[0];
  const has = (id: string | null) => id !== null && entry.runs.some((r) => r.id === id);
  const a = has(link.a) ? link.a! : entry.runs[0].id;
  if (link.s === entry.id) return { s: entry.id, a, b: has(link.b) ? link.b : null };
  return { s: entry.id, a, b: entry.runs[1]?.id ?? null };
}

/** Light or dark: follows the system until you press the button. */
function useTheme() {
  const media = useMemo(() => window.matchMedia("(prefers-color-scheme: dark)"), []);
  const [system, setSystem] = useState<Theme>(media.matches ? "dark" : "light");
  const [chosen, setChosen] = useState<Theme | null>(null);
  useEffect(() => {
    const onChange = (e: MediaQueryListEvent) => setSystem(e.matches ? "dark" : "light");
    media.addEventListener("change", onChange);
    return () => media.removeEventListener("change", onChange);
  }, [media]);
  const resolved = chosen ?? system;
  useEffect(() => {
    document.documentElement.dataset.theme = resolved;
  }, [resolved]);
  return { resolved, toggle: () => setChosen(resolved === "dark" ? "light" : "dark") };
}
