// Decisions with their reasons. Click one to jump to its tick and ring the entities it names.

import { memo, useMemo, useState } from "react";
import { describeAction, entitiesOf, type Divergence } from "../logic";
import type { Decision, Run } from "../types";

interface Props {
  runs: { label: string; run: Run }[]; // A, and B in side-by-side
  tick: number;
  divergence: Divergence | null;
  onPick: (tick: number, entities: string[]) => void;
}

interface Row {
  label: string;
  decision: Decision;
  diverges: boolean;
}

export const DecisionsPanel = memo(function DecisionsPanel({ runs, tick, divergence, onPick }: Props) {
  const [role, setRole] = useState("any");
  const [withActions, setWithActions] = useState(true);
  const [whose, setWhose] = useState("both");

  const roles = useMemo(
    () => [...new Set(runs.flatMap(({ run }) => run.decisions.map((d) => d.role)))].sort(),
    [runs],
  );

  // Every decision of the shown runs, in tick order (A before B at the same tick).
  const rows = useMemo<Row[]>(() => {
    const all: Row[] = [];
    for (const { label, run } of runs) {
      const index = label === "A" ? divergence?.indexA : divergence?.indexB;
      for (const d of run.decisions) all.push({ label, decision: d, diverges: d.i === index });
    }
    return all.sort((x, y) => x.decision.t - y.decision.t || x.label.localeCompare(y.label));
  }, [runs, divergence]);

  const shown = rows.filter(
    (r) =>
      (role === "any" || r.decision.role === role) &&
      (!withActions || r.decision.actions.length > 0 || r.diverges) &&
      (whose === "both" || r.label === whose),
  );

  return (
    <section className="panel" aria-label="Decisions">
      <header>
        <h2>Decisions</h2>
        <label>
          role{" "}
          <select value={role} onChange={(e) => setRole(e.target.value)}>
            <option value="any">any</option>
            {roles.map((r) => (
              <option key={r}>{r}</option>
            ))}
          </select>
        </label>
        {runs.length > 1 && (
          <label>
            run{" "}
            <select value={whose} onChange={(e) => setWhose(e.target.value)}>
              <option value="both">A and B</option>
              <option value="A">A</option>
              <option value="B">B</option>
            </select>
          </label>
        )}
        <label>
          <input type="checkbox" checked={withActions} onChange={(e) => setWithActions(e.target.checked)} />{" "}
          only with actions
        </label>
      </header>
      <ol className="list">
        {shown.length === 0 && <li className="muted">No decisions match.</li>}
        {shown.map(({ label, decision: d, diverges }) => (
          <li
            key={`${label}${d.i}`}
            className={[
              "decision",
              d.t > tick ? "future" : "",
              d.t === tick ? "current" : "",
              diverges ? "diverges" : "",
            ].join(" ")}
            onClick={() => onPick(d.t, d.actions.flatMap((a) => entitiesOf(a.raw)))}
          >
            <div className="row-head">
              {runs.length > 1 && <span className={`run-tag run-${label.toLowerCase()}`}>{label}</span>}
              <span className="tick">t={d.t}</span>
              {diverges && <span className="badge diverge-badge">◆ first difference</span>}
              <span className="muted small">
                {d.role} · woken by {d.triggers.map((t) => t.kind).join(", ") || "nothing"}
              </span>
            </div>
            {d.actions.length === 0 && <div className="muted small">no actions</div>}
            {d.actions.map((a, k) => (
              <div key={k} className={a.accepted ? "action" : "action rejected"}>
                <span className="action-what">{describeAction(a.raw)}</span>
                {!a.accepted && <span className="badge bad">rejected: {a.reason}</span>}
                {typeof a.raw.reason === "string" && a.raw.reason && (
                  <div className="why">“{a.raw.reason}”</div>
                )}
              </div>
            ))}
          </li>
        ))}
      </ol>
    </section>
  );
});
