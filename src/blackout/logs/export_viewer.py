"""Build the replay viewer's data files from logged runs (docs/PRD.md sections 8.12 and 8.13).

Run logs hold no frames, so this module re-simulates each selected run with the verified
replay (blackout.logs.replay) and takes one frame per tick: the world after that tick's events
and after any decisions made at it, which is the state the next minute starts from. A run that
doesn't replay exactly is refused, so the viewer only ever shows what really happened.

    blackout export-viewer <experiment> --select configs/viewer/preview.yaml

reads ``runs/<experiment>/<scenario>/<run>/run.jsonl.gz`` for every run the selection names and
writes, under ``--out`` (by default ``viewer/public/data``):

    manifest.json                   what's there: scenarios, their runs and final metrics
    scenarios/<scenario>.json       the static world: map, facilities, zones, reports and truth
    runs/<scenario>/<run>.json      one run: frames, decisions, reports released, events, end

The files are deterministic (sorted keys, no clock), and the TypeScript types that read them
are in ``viewer/src/types.ts``: change both together and bump ``VIEWER_DATA_VERSION``.

The test suite is never exported: a run whose embedded scenario has ``split: test`` is refused.
"""

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from blackout.env import DecisionRequest
from blackout.logs.reader import RunLog, read_log
from blackout.logs.replay import replay
from blackout.logs.schema import LOG_FILE, LogError
from blackout.scenarios.schema import Scenario
from blackout.sim.city import build_grid_city
from blackout.sim.engine import Engine

VIEWER_DATA_VERSION = 1
MANIFEST_FILE = "manifest.json"


class ExportError(Exception):
    """The selection can't be exported: a missing run, a test-suite scenario, or a bad replay."""


# --- the selection file -------------------------------------------------------------------


class SelectedScenario(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    runs: tuple[str, ...] | None = Field(
        default=None, description="Run folder names, e.g. rules__none__r0; default all."
    )
    note: str = Field(default="", description="One line shown in the viewer's run picker.")


class Selection(BaseModel):
    """A curated list of runs to export (``configs/viewer/*.yaml``)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    title: str = "Blackout replays"
    scenarios: tuple[SelectedScenario, ...] = Field(min_length=1)


def load_selection(path: Path) -> Selection:
    with path.open(encoding="utf-8") as handle:
        return Selection.model_validate(yaml.safe_load(handle))


# --- frames -------------------------------------------------------------------------------


def frame(engine: Engine) -> dict[str, object]:
    """Everything the viewer draws at one tick. Static facts live in the scenario file, so a
    frame holds only what can change, in the scenario file's entity order (sorted by ID)."""
    metrics = engine.metrics
    return {
        "t": engine.tick,
        "trucks": [
            {
                "at": t.at,
                "next": t.next_node,
                "elapsed": t.elapsed,
                "route": list(t.route),
                "state": t.state.value,
                "load_l": t.load_l,
                "task": None if t.task is None else t.task.target_id,
            }
            for _, t in sorted(engine.trucks.items())
        ],
        "crews": [
            {
                "at": c.at,
                "next": c.next_node,
                "elapsed": c.elapsed,
                "route": list(c.route),
                "state": c.state.value,
                "substation": c.substation,
            }
            for _, c in sorted(engine.crews.items())
        ],
        "hospitals": [
            {
                "fuel_l": h.fuel_l,
                "burn_lpm": h.burn_lpm,
                "status": h.status.value,
                "dark_min": metrics.dark_minutes[h.id],
            }
            for _, h in sorted(engine.hospitals.items())
        ],
        "depots": [{"stock_l": d.stock_l} for _, d in sorted(engine.depots.items())],
        "substations": [
            {"state": s.state.value, "load_kw": s.load_kw, "work_left": s.work_left}
            for _, s in sorted(engine.substations.items())
        ],
        "zones": [{"shed": z.shed, "fed_by": z.fed_by} for _, z in sorted(engine.zones.items())],
        "blocked": sorted(engine.blocked),
        "known_blocks": sorted(engine.known_blocks),
        "unmet_kw_min": metrics.unmet_kw_min,
        "unmet_weighted_kw_min": metrics.unmet_weighted_kw_min,
    }


@dataclass(slots=True)
class _FrameRecorder:
    """Collects one frame per tick; a decision at a tick replaces that tick's frame with the
    state after its actions (trucks dispatched, zones shed)."""

    frames: list[dict[str, object]]

    def on_tick(self, engine: Engine) -> None:
        self.frames.append(frame(engine))

    def on_decision(self, _request: DecisionRequest, engine: Engine) -> None:
        self.frames[-1] = frame(engine)


# --- files --------------------------------------------------------------------------------


def scenario_file(scenario: Scenario) -> dict[str, object]:
    """The static world, plus every report with its ground truth (shown behind a spoiler)."""
    spec = scenario.city
    city = build_grid_city(
        spec.width,
        spec.height,
        default_minutes=spec.default_travel_minutes,
        minutes=spec.travel_minutes,
        landmarks=spec.landmarks,
    )
    zone_of = {node: zone.id for zone in scenario.zones for node in zone.nodes}
    reports: list[dict[str, object]] = []
    events: list[dict[str, object]] = []
    for event in scenario.events:
        data = event.model_dump(mode="json")
        if data["kind"] != "report":
            events.append(data)
            continue
        truth = data.pop("truth")
        reports.append(
            {
                **{k: data[k] for k in ("id", "tick", "channel", "source", "source_class")},
                # What the contestant was given: structured facts at L0, only the text above.
                "text": data.get("text") if scenario.level >= 1 else None,
                "facts": data["facts"] if scenario.level == 0 else [],
                "claimed_facts": data["facts"],
                "is_true": truth["is_true"],
                "stale_of": truth.get("stale_of"),
                "corrects": truth.get("corrects"),
                "cues": truth.get("cues", []),
                "injection": truth.get("injection"),
            }
        )
    return {
        "v": VIEWER_DATA_VERSION,
        "id": scenario.id,
        "description": scenario.description,
        "template": scenario.template,
        "level": scenario.level,
        "tier": scenario.tier,
        "split": scenario.split,
        "horizon": scenario.horizon,
        "city": {
            "width": city.width,
            "height": city.height,
            "nodes": [
                {"id": n.id, "x": n.x, "y": n.y, "name": n.name}
                for _, n in sorted(city.nodes.items())
            ],
            "edges": [
                {"id": e.id, "a": e.a, "b": e.b, "minutes": e.minutes, "name": e.name}
                for _, e in sorted(city.edges.items())
            ],
            "landmarks": dict(sorted(city.landmarks.items())),
        },
        "hospitals": [
            {
                "id": h.id,
                "name": h.name,
                "node": h.node,
                "tank_l": h.tank_l,
                "zone": zone_of.get(h.node),
            }
            for h in sorted(scenario.hospitals, key=lambda h: h.id)
        ],
        "depots": [
            {"id": d.id, "name": d.name, "node": d.node, "stock_l": d.stock_l}
            for d in sorted(scenario.depots, key=lambda d: d.id)
        ],
        "substations": [
            {"id": s.id, "name": s.name, "node": s.node, "capacity_kw": s.capacity_kw}
            for s in sorted(scenario.substations, key=lambda s: s.id)
        ],
        "zones": [
            {
                "id": z.id,
                "name": z.name,
                "nodes": sorted(z.nodes),
                "priority": z.priority,
                "demand_kw": z.demand_kw,
                "primary": z.primary,
                "alternate": z.alternate,
            }
            for z in sorted(scenario.zones, key=lambda z: z.id)
        ],
        "trucks": [
            {"id": t.id, "capacity_l": t.capacity_l}
            for t in sorted(scenario.trucks, key=lambda t: t.id)
        ],
        "crews": [{"id": c.id} for c in sorted(scenario.crews, key=lambda c: c.id)],
        "reports": reports,
        "events": events,
    }


def run_file(log: RunLog) -> dict[str, object]:
    """Re-simulate ``log`` (checking every hash on the way) and return the viewer's run file."""
    recorder = _FrameRecorder([])
    result = replay(log, on_tick=recorder.on_tick, on_decision=recorder.on_decision)
    released: list[dict[str, object]] = []
    events: list[dict[str, object]] = []
    for tick in log.ticks:
        for event in tick.ev:
            if event.get("kind") == "report":
                released.append({"t": tick.t, "id": event["id"]})
            else:
                events.append({"t": tick.t, **event})
    decisions = [
        {
            "i": dp.i,
            "t": dp.t,
            "role": dp.role,
            "triggers": [trigger.model_dump(mode="json") for trigger in dp.triggers],
            "actions": [
                {
                    "kind": action.raw.get("kind"),
                    "raw": action.raw,
                    "accepted": action.accepted,
                    "reason": action.reason,
                    "message": action.message,
                    "route": action.route,
                }
                for action in dp.actions
            ],
            "belief": dp.belief,
            # Tokens and cost (LLM contestants, M10 on); None for the baselines.
            "llm": None if dp.llm is None else dp.llm.model_dump(mode="json"),
        }
        for dp in log.decisions
    ]
    header = log.header
    return {
        "v": VIEWER_DATA_VERSION,
        "run_id": header.run_id,
        "scenario": header.scenario.id,
        "contestant": header.contestant.name,
        "model": header.contestant.model,
        "run_index": header.contestant.run_index,
        "run_hash": result.episode.run_hash,
        "end": {
            "tick": result.episode.done.tick,
            "reason": result.episode.done.reason,
            "metrics": result.report,
        },
        "frames": recorder.frames,
        "decisions": decisions,
        "reports": released,
        "events": events,
    }


# --- the export ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Exported:
    out: Path
    scenarios: int
    runs: int
    bytes_written: int


def _write_json(path: Path, data: object) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
    path.write_text(text, encoding="utf-8", newline="\n")
    return len(text.encode("utf-8"))


def _run_dirs(scenario_dir: Path, wanted: Sequence[str] | None) -> list[Path]:
    if wanted is None:
        return sorted(p.parent for p in scenario_dir.glob(f"*/{LOG_FILE}"))
    dirs = [scenario_dir / name for name in wanted]
    missing = [str(d) for d in dirs if not (d / LOG_FILE).is_file()]
    if missing:
        raise ExportError(f"no run log in {', '.join(missing)}")
    return dirs


def export_viewer(
    experiment_dir: Path,
    selection: Selection,
    out: Path,
    *,
    on_run: Callable[[str], None] | None = None,
) -> Exported:
    """Export every selected run of ``experiment_dir`` to ``out`` (see the module docstring)."""
    manifest_scenarios: list[dict[str, object]] = []
    written = 0
    run_count = 0
    # Read every selected log first, so a missing run or a test-suite scenario stops the export
    # before anything is re-simulated or written.
    chosen_logs: list[tuple[SelectedScenario, list[tuple[Path, RunLog]]]] = []
    for chosen in selection.scenarios:
        scenario_dir = experiment_dir / chosen.id
        if not scenario_dir.is_dir():
            raise ExportError(f"no runs for scenario {chosen.id!r} in {experiment_dir}")
        run_dirs = _run_dirs(scenario_dir, chosen.runs)
        if not run_dirs:
            raise ExportError(f"no run logs under {scenario_dir}")
        logs: list[tuple[Path, RunLog]] = []
        for run_dir in run_dirs:
            try:
                log = read_log(run_dir)
            except LogError as error:
                raise ExportError(f"{run_dir}: {error}") from error
            if log.header.scenario.spec.get("split") == "test":
                raise ExportError(f"{run_dir}: the test suite is never exported")
            if log.header.scenario.hash != (logs[0][1] if logs else log).header.scenario.hash:
                raise ExportError(f"{run_dir} was recorded on another version of {chosen.id}")
            logs.append((run_dir, log))
        chosen_logs.append((chosen, logs))

    for chosen, logs in chosen_logs:
        runs: list[dict[str, object]] = []
        for run_dir, log in logs:
            try:
                data = run_file(log)
            except LogError as error:
                raise ExportError(f"{run_dir}: {error}") from error
            name = run_dir.name
            written += _write_json(out / "runs" / chosen.id / f"{name}.json", data)
            end = log.end
            metrics: Mapping[str, Any] = {} if end is None else end.metrics
            runs.append(
                {
                    "id": name,
                    "contestant": log.header.contestant.name,
                    "file": f"runs/{chosen.id}/{name}.json",
                    "run_hash": data["run_hash"],
                    "ticks": len(log.ticks),
                    "decisions": len(log.decisions),
                    "critical_downtime_min": metrics.get("critical_downtime_min"),
                    "unmet_weighted_kw_min": metrics.get("unmet_weighted_kw_min"),
                }
            )
            run_count += 1
            if on_run is not None:
                on_run(log.header.run_id)
        scenario = logs[0][1].scenario()
        written += _write_json(out / "scenarios" / f"{chosen.id}.json", scenario_file(scenario))
        manifest_scenarios.append(
            {
                "id": chosen.id,
                "note": chosen.note,
                "template": scenario.template,
                "level": scenario.level,
                "tier": scenario.tier,
                "file": f"scenarios/{chosen.id}.json",
                "runs": runs,
            }
        )
    manifest = {
        "v": VIEWER_DATA_VERSION,
        "title": selection.title,
        "experiment": experiment_dir.name,
        "scenarios": manifest_scenarios,
    }
    written += _write_json(out / MANIFEST_FILE, manifest)
    return Exported(out, len(manifest_scenarios), run_count, written)
