"""Result tables for a batch: one row per run, and a summary by template x level (PRD 8.10).

Downtime Avoided is DA = 1 - D / D0, where D0 is do-nothing's critical downtime on the same
scenario: from the batch's own do-nothing run if it has one, else from the scenario file's
validity record (written by the generator, which runs do-nothing). "Beats do-nothing" means
strictly less critical downtime.

Files (in ``results/<experiment>/``): ``runs.csv``, ``summary.csv`` and ``results.md``, which
also reports the keyword parser's F1 per level over the batch's scenarios.
"""

import csv
import io
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from statistics import fmean

from blackout.controllers.do_nothing import DoNothingController
from blackout.experiments.runner import Outcome
from blackout.scenarios.schema import Scenario, load_scenario
from blackout.scoring.parse_f1 import ParseCounts, parse_f1

RESULTS_ROOT = Path("results")


@dataclass(frozen=True, slots=True)
class RunRow:
    scenario: str
    template: str
    level: int
    contestant: str
    run_index: int
    critical_downtime_min: int
    do_nothing_downtime_min: int | None
    da: float | None
    beats_do_nothing: bool | None
    hospitals_lost_power: int
    unmet_weighted_kw_min: int
    trips: int
    stuck_events: int
    decision_points: int
    actions: int
    invalid_action_rate: float | None
    run_hash: str
    discrepancies: int


def run_rows(outcomes: Sequence[Outcome], scenarios: Mapping[str, Scenario]) -> list[RunRow]:
    baseline = {
        o.job.scenario_id: o.card.critical_downtime_min
        for o in outcomes
        if o.job.contestant == DoNothingController.name and o.job.run_index == 0
    }
    rows = []
    for o in outcomes:
        card, scenario = o.card, scenarios[o.job.scenario_id]
        d0 = baseline.get(scenario.id)
        if d0 is None and scenario.validity is not None:
            d0 = scenario.validity.do_nothing_critical_downtime
        d = card.critical_downtime_min
        rows.append(
            RunRow(
                scenario=scenario.id,
                template=scenario.template or "-",
                level=scenario.level,
                contestant=card.contestant,
                run_index=card.run_index,
                critical_downtime_min=d,
                do_nothing_downtime_min=d0,
                da=None if not d0 else 1 - d / d0,
                beats_do_nothing=None if d0 is None else d < d0,
                hospitals_lost_power=card.hospitals_lost_power,
                unmet_weighted_kw_min=card.unmet_weighted_kw_min,
                trips=card.trips,
                stuck_events=card.stuck_events,
                decision_points=card.decision_points,
                actions=card.actions,
                invalid_action_rate=card.invalid_action_rate,
                run_hash=card.run_hash,
                discrepancies=len(card.discrepancies),
            )
        )
    return rows


@dataclass(frozen=True, slots=True)
class GroupRow:
    template: str
    level: int
    contestant: str
    runs: int
    scenarios: int
    mean_downtime_min: float
    mean_da: float | None
    beats_do_nothing: int  # scenarios (judged by their mean downtime)
    compared: int  # scenarios with a do-nothing downtime to compare with
    mean_invalid_action_rate: float | None
    stuck_events: int


def group_rows(rows: Iterable[RunRow]) -> list[GroupRow]:
    """One row per (template, level, contestant), plus an "all" template row per level."""
    rows = list(rows)
    keys = sorted({(r.template, r.level, r.contestant) for r in rows})
    keys += sorted({("all", r.level, r.contestant) for r in rows})
    groups = []
    for template, level, contestant in keys:
        chosen = [
            r
            for r in rows
            if r.level == level and r.contestant == contestant and template in ("all", r.template)
        ]
        per_scenario: dict[str, list[RunRow]] = {}
        for r in chosen:
            per_scenario.setdefault(r.scenario, []).append(r)
        beats = compared = 0
        for runs in per_scenario.values():
            d0 = runs[0].do_nothing_downtime_min
            if d0 is not None:
                compared += 1
                beats += fmean(r.critical_downtime_min for r in runs) < d0
        das = [r.da for r in chosen if r.da is not None]
        rates = [r.invalid_action_rate for r in chosen if r.invalid_action_rate is not None]
        groups.append(
            GroupRow(
                template=template,
                level=level,
                contestant=contestant,
                runs=len(chosen),
                scenarios=len(per_scenario),
                mean_downtime_min=fmean(r.critical_downtime_min for r in chosen),
                mean_da=fmean(das) if das else None,
                beats_do_nothing=beats,
                compared=compared,
                mean_invalid_action_rate=fmean(rates) if rates else None,
                stuck_events=sum(r.stuck_events for r in chosen),
            )
        )
    return groups


def write_results(
    experiment: str,
    outcomes: Sequence[Outcome],
    out: Path = RESULTS_ROOT,
) -> list[Path]:
    """Write runs.csv, summary.csv and results.md for a batch. Returns the paths written."""
    scenarios = {o.job.scenario_id: load_scenario(o.job.scenario_path) for o in outcomes}
    rows = run_rows(outcomes, scenarios)
    groups = group_rows(rows)
    parse = parse_f1(scenarios[s] for s in sorted(scenarios))
    folder = out / experiment
    folder.mkdir(parents=True, exist_ok=True)
    files = {
        "runs.csv": _csv(rows),
        "summary.csv": _csv(groups),
        "results.md": markdown(experiment, groups, parse, rows),
    }
    paths = []
    for name, text in files.items():
        path = folder / name
        path.write_text(text, encoding="utf-8", newline="\n")
        paths.append(path)
    return paths


def markdown(
    experiment: str,
    groups: Sequence[GroupRow],
    parse: Mapping[int, ParseCounts],
    rows: Sequence[RunRow],
) -> str:
    lines = [
        f"# Results: {experiment}",
        "",
        "Generated by `blackout batch`. DA = 1 - D / D(do-nothing) per scenario, averaged;"
        " *beats* counts scenarios where the contestant's critical downtime is strictly below"
        " do-nothing's.",
        "",
        "| Template | Level | Contestant | Scenarios | Runs | Mean downtime (min) | Mean DA"
        " | Beats do-nothing | Invalid-action rate | Stuck events |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for g in groups:
        lines.append(
            f"| {g.template} | L{g.level} | {g.contestant} | {g.scenarios} | {g.runs}"
            f" | {g.mean_downtime_min:.1f} | {_num(g.mean_da)}"
            f" | {g.beats_do_nothing}/{g.compared} | {_num(g.mean_invalid_action_rate)}"
            f" | {g.stuck_events} |"
        )
    lines += [
        "",
        "## Keyword parser F1 by level",
        "",
        "Scored on (kind, subject, value within 10%) against each report's true facts. At L0"
        " contestants receive the facts themselves, so F1 is 1 by construction.",
        "",
        "| Level | Facts | Parsed | Correct | Precision | Recall | F1 |",
        "|---|---|---|---|---|---|---|",
    ]
    for level, c in parse.items():
        lines.append(
            f"| L{level} | {c.tp + c.fn} | {c.tp + c.fp} | {c.tp} | {_num(c.precision)}"
            f" | {_num(c.recall)} | {_num(c.f1)} |"
        )
    problems = sum(r.discrepancies for r in rows)
    lines += ["", f"Runs: {len(rows)}. Scorer discrepancies: {problems}.", ""]
    return "\n".join(lines)


def _num(value: float | None) -> str:
    return "-" if value is None else f"{value:.3f}"


def _csv(items: Sequence[object]) -> str:
    buffer = io.StringIO()
    if not items:
        return ""
    names = list(items[0].__dataclass_fields__)  # type: ignore[attr-defined]
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(names)
    for item in items:
        writer.writerow([_cell(getattr(item, name)) for name in names])
    return buffer.getvalue()


def _cell(value: object) -> object:
    if isinstance(value, float):
        return f"{value:.4f}"
    if value is None:
        return ""
    return value
