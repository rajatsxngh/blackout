// Live metrics at the current tick, and (side by side) a comparison of the final metrics.

import { memo } from "react";
import type { Run, Scenario } from "../types";

interface Props {
  scenario: Scenario;
  runs: { label: string; run: Run }[];
  tick: number;
}

/** The numbers we show for a run at a tick (or at its end). */
function liveNumbers(run: Run, tick: number) {
  const frame = run.frames[Math.min(tick, run.frames.length - 1)];
  const done = run.decisions.filter((d) => d.t <= tick);
  const llm = done.reduce(
    (sum, d) => ({
      tokens: sum.tokens + (d.llm ? d.llm.input_tokens + d.llm.output_tokens : 0),
      cost: sum.cost + (d.llm?.cost_usd ?? 0),
      any: sum.any || d.llm !== null,
    }),
    { tokens: 0, cost: 0, any: false },
  );
  return {
    frame,
    downtime: frame.hospitals.reduce((s, h) => s + h.dark_min, 0),
    unmet: frame.unmet_kw_min,
    unmetWeighted: frame.unmet_weighted_kw_min,
    decisions: done.length,
    actions: done.reduce((s, d) => s + d.actions.length, 0),
    llm,
  };
}

const kwh = (kwMin: number) => `${Math.round(kwMin / 60).toLocaleString()} kWh`;

export const MetricsPanel = memo(function MetricsPanel({ scenario, runs, tick }: Props) {
  const live = runs.map(({ label, run }) => ({ label, run, ...liveNumbers(run, tick) }));

  return (
    <section className="panel" aria-label="Metrics">
      <header>
        <h2>Metrics</h2>
        <span className="muted small">at t={tick}</span>
      </header>
      <table className="metrics">
        <thead>
          <tr>
            <th></th>
            {live.map((l) => (
              <th key={l.label}>
                {runs.length > 1 && <span className={`run-tag run-${l.label.toLowerCase()}`}>{l.label}</span>}
                {l.run.contestant}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {scenario.hospitals.map((h, i) => (
            <tr key={h.id}>
              <td>
                {h.id} dark <span className="muted small">{h.name}</span>
              </td>
              {live.map((l) => (
                <td key={l.label} className={l.frame.hospitals[i].dark_min > 0 ? "bad-text" : ""}>
                  {l.frame.hospitals[i].dark_min} min
                </td>
              ))}
            </tr>
          ))}
          <tr className="total">
            <td>critical downtime</td>
            {live.map((l) => (
              <td key={l.label}>{l.downtime} min</td>
            ))}
          </tr>
          <tr>
            <td>unmet demand</td>
            {live.map((l) => (
              <td key={l.label}>{kwh(l.unmet)}</td>
            ))}
          </tr>
          <tr>
            <td>decisions · actions</td>
            {live.map((l) => (
              <td key={l.label}>
                {l.decisions} · {l.actions}
              </td>
            ))}
          </tr>
          <tr>
            <td>tokens · cost</td>
            {live.map((l) => (
              <td key={l.label}>
                {l.llm.any ? `${l.llm.tokens.toLocaleString()} · $${l.llm.cost.toFixed(3)}` : "— (no LLM)"}
              </td>
            ))}
          </tr>
        </tbody>
      </table>
      {runs.length > 1 && <FinalComparison runs={runs} />}
    </section>
  );
});

/** Bars comparing the two runs' final metrics (shorter is better for all of them). */
function FinalComparison({ runs }: { runs: { label: string; run: Run }[] }) {
  const finals = runs.map(({ label, run }) => ({ label, ...liveNumbers(run, run.frames.length - 1) }));
  const rows: { name: string; value: (f: (typeof finals)[number]) => number; show: (n: number) => string }[] = [
    { name: "critical downtime", value: (f) => f.downtime, show: (n) => `${n} min` },
    { name: "weighted unmet demand", value: (f) => f.unmetWeighted, show: (n) => kwh(n) },
    { name: "actions", value: (f) => f.actions, show: String },
    { name: "decisions", value: (f) => f.decisions, show: String },
  ];
  return (
    <div className="final">
      <h3>Final metrics</h3>
      {rows.map((row) => {
        const max = Math.max(1, ...finals.map(row.value));
        return (
          <div key={row.name} className="final-row">
            <div className="small">{row.name}</div>
            {finals.map((f) => (
              <div key={f.label} className="bar-line">
                <span className={`run-tag run-${f.label.toLowerCase()}`}>{f.label}</span>
                <span className="bar-track">
                  <span
                    className={`bar bar-${f.label.toLowerCase()}`}
                    style={{ width: `${(100 * row.value(f)) / max}%` }}
                  />
                </span>
                <span className="bar-value">{row.show(row.value(f))}</span>
              </div>
            ))}
          </div>
        );
      })}
    </div>
  );
}
