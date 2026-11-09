// Radio reports as the contestant received them, newest first. Whether each was true is
// hidden until you turn on the spoiler toggle.

import { memo, useState } from "react";
import type { Fact, Run, Scenario } from "../types";

interface Props {
  scenario: Scenario;
  run: Run; // reports reach every run of a scenario at the same ticks; we list run A's
  tick: number;
}

const factText = (f: Fact) => `${f.type.replace(/_/g, " ")} ${f.subject}${f.value === null ? "" : `: ${f.value}`}`;

export const ReportsPanel = memo(function ReportsPanel({ scenario, run, tick }: Props) {
  const [spoilers, setSpoilers] = useState(false);
  const byId = new Map(scenario.reports.map((r) => [r.id, r]));
  const received = run.reports
    .filter((r) => r.t <= tick)
    .map((r) => ({ t: r.t, report: byId.get(r.id) }))
    .reverse();

  return (
    <section className="panel" aria-label="Reports">
      <header>
        <h2>Reports</h2>
        <span className="muted small">
          L{scenario.level} · {received.length} of {run.reports.length} received
        </span>
        <label>
          <input type="checkbox" checked={spoilers} onChange={(e) => setSpoilers(e.target.checked)} /> show
          true / false
        </label>
      </header>
      <ol className="list">
        {received.length === 0 && <li className="muted">No reports yet.</li>}
        {received.map(({ t, report }) =>
          report ? (
            <li key={report.id} className={`report ${t === tick ? "current" : ""}`}>
              <div className="row-head">
                <span className="tick">t={t}</span>
                <span className="mono">{report.id}</span>
                <span className={`badge src-${report.source_class}`}>{report.source_class}</span>
                <span className="muted small">
                  {report.channel.replace(/_/g, " ")} · {report.source}
                </span>
              </div>
              {report.text !== null ? (
                <div className="quote">“{report.text}”</div>
              ) : (
                <div className="facts">{report.facts.map(factText).join("; ") || "(no facts)"}</div>
              )}
              {spoilers && (
                <div className="truth">
                  {report.injection ? (
                    <span className="badge bad">
                      injection: wants {report.injection.kind} {report.injection.target} (t
                      {report.injection.start}–{report.injection.end}), posing as {report.injection.authority}
                    </span>
                  ) : report.is_true ? (
                    <span className="badge good">true</span>
                  ) : (
                    <span className="badge bad">{report.stale_of ? "stale" : "false"}</span>
                  )}
                  {report.corrects && <span className="badge">corrects {report.corrects}</span>}
                  {report.cues.map((c, k) => (
                    <span key={k} className="muted small">
                      {" "}
                      cue: {c.note || c.kind}
                      {c.report ? ` (${c.report})` : ""}
                    </span>
                  ))}
                  {report.text !== null && report.claimed_facts.length > 0 && (
                    <div className="muted small">claims: {report.claimed_facts.map(factText).join("; ")}</div>
                  )}
                </div>
              )}
            </li>
          ) : null,
        )}
      </ol>
    </section>
  );
});
