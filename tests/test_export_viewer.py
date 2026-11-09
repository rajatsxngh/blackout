"""The viewer export: frames from a verified replay, deterministic files, and no test suite."""

import gzip
import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from blackout.cli import app
from blackout.controllers.registry import make_controller
from blackout.env import env_for
from blackout.logs.export_viewer import (
    MANIFEST_FILE,
    VIEWER_DATA_VERSION,
    ExportError,
    Selection,
    export_viewer,
    load_selection,
)
from blackout.logs.writer import record_episode, run_dir_for
from blackout.scenarios.schema import Scenario, load_scenario
from tests.helpers import HANDMADE, ROOT, make_scenario, read_lines

TRAP = HANDMADE / "trap_assignment.json"


def record_run(scenario: Scenario, contestant: str, experiment: Path) -> Path:
    controller = make_controller(contestant)
    run_dir = run_dir_for(experiment, scenario.id, controller.name)
    record_episode(env_for(scenario, controller), controller, run_dir)
    return run_dir


def selection(scenario_id: str, *runs: str) -> Selection:
    return Selection.model_validate(
        {"scenarios": [{"id": scenario_id, "runs": list(runs) or None, "note": "a trap"}]}
    )


@pytest.fixture
def trap_experiment(tmp_path: Path) -> Path:
    experiment = tmp_path / "runs" / "viewer_test"
    scenario = load_scenario(TRAP)
    for contestant in ("rules", "solver"):
        record_run(scenario, contestant, experiment)
    return experiment


def load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def test_export_writes_manifest_scenario_and_one_frame_per_tick(
    trap_experiment: Path, tmp_path: Path
) -> None:
    out = tmp_path / "data"
    exported = export_viewer(trap_experiment, selection("trap_assignment"), out)
    assert (exported.scenarios, exported.runs) == (1, 2)

    manifest = load(out / MANIFEST_FILE)
    assert manifest["v"] == VIEWER_DATA_VERSION
    (entry,) = manifest["scenarios"]
    assert [r["id"] for r in entry["runs"]] == ["rules__none__r0", "solver__none__r0"]
    assert entry["note"] == "a trap"

    scenario = load(out / entry["file"])
    assert scenario["id"] == "trap_assignment"
    assert [h["id"] for h in scenario["hospitals"]] == ["H1", "H2"]

    for listed in entry["runs"]:
        run = load(out / listed["file"])
        frames = run["frames"]
        assert isinstance(frames, list)
        assert [f["t"] for f in frames] == list(range(listed["ticks"]))
        # The last frame's dark minutes add up to the run's critical downtime.
        downtime = sum(h["dark_min"] for h in frames[-1]["hospitals"])
        assert (
            downtime
            == listed["critical_downtime_min"]
            == run["end"]["metrics"]["critical_downtime_min"]
        )
        assert len(run["decisions"]) == listed["decisions"]


def test_a_decision_tick_shows_the_state_after_its_actions(
    trap_experiment: Path, tmp_path: Path
) -> None:
    out = tmp_path / "data"
    export_viewer(trap_experiment, selection("trap_assignment", "rules__none__r0"), out)
    run = load(out / "runs" / "trap_assignment" / "rules__none__r0.json")
    first = run["decisions"][0]
    assert first["t"] == 0
    assert first["actions"]
    dispatched = {a["raw"]["truck_id"] for a in first["actions"] if a["accepted"]}
    trucks = run["frames"][0]["trucks"]
    scenario = load(out / "scenarios" / "trap_assignment.json")
    ids = [t["id"] for t in scenario["trucks"]]
    for truck_id in dispatched:
        assert trucks[ids.index(truck_id)]["route"], f"{truck_id} should be on its way at t=0"


def test_export_is_deterministic(trap_experiment: Path, tmp_path: Path) -> None:
    first, second = tmp_path / "one", tmp_path / "two"
    export_viewer(trap_experiment, selection("trap_assignment"), first)
    export_viewer(trap_experiment, selection("trap_assignment"), second)
    files = sorted(p.relative_to(first) for p in first.rglob("*.json"))
    assert files == sorted(p.relative_to(second) for p in second.rglob("*.json"))
    for name in files:
        assert (first / name).read_bytes() == (second / name).read_bytes()


def test_the_test_suite_is_never_exported(tmp_path: Path) -> None:
    experiment = tmp_path / "runs" / "sneaky"
    record_run(make_scenario(id="held_out", split="test"), "do-nothing", experiment)
    out = tmp_path / "data"
    with pytest.raises(ExportError, match="test suite is never exported"):
        export_viewer(experiment, selection("held_out"), out)
    assert not out.exists()  # refused before anything was written


def test_missing_runs_and_scenarios_are_reported(trap_experiment: Path, tmp_path: Path) -> None:
    with pytest.raises(ExportError, match="no runs for scenario"):
        export_viewer(trap_experiment, selection("nowhere"), tmp_path / "data")
    with pytest.raises(ExportError, match="no run log in"):
        export_viewer(trap_experiment, selection("trap_assignment", "oracle__none__r0"), tmp_path)


def test_a_tampered_log_is_refused(trap_experiment: Path, tmp_path: Path) -> None:
    log_path = trap_experiment / "trap_assignment" / "rules__none__r0" / "run.jsonl.gz"
    records = read_lines(log_path)
    tick = next(r for r in records if r["type"] == "tick" and r["t"] == 5)
    tick["h"] = "0" * 32
    lines = "".join(json.dumps(r, sort_keys=True) + "\n" for r in records)
    with gzip.open(log_path, "wt", encoding="utf-8", newline="\n") as handle:
        handle.write(lines)
    with pytest.raises(ExportError, match="differs from the log at tick 5"):
        export_viewer(trap_experiment, selection("trap_assignment"), tmp_path / "data")


def test_the_preview_selection_names_no_test_scenarios() -> None:
    chosen = load_selection(ROOT / "configs" / "viewer" / "preview.yaml")
    assert {"trap_assignment", "trap_depot"} <= {s.id for s in chosen.scenarios}
    for entry in chosen.scenarios:
        assert not entry.id.startswith("test")
        assert entry.runs == ("rules__none__r0", "solver__none__r0")


def test_cli_exports_an_experiment(trap_experiment: Path, tmp_path: Path) -> None:
    select = tmp_path / "select.yaml"
    select.write_text("scenarios:\n  - id: trap_assignment\n", encoding="utf-8")
    out = tmp_path / "data"
    result = CliRunner().invoke(
        app, ["export-viewer", str(trap_experiment), "--select", str(select), "--out", str(out)]
    )
    assert result.exit_code == 0, result.output
    assert "Wrote 2 runs of 1 scenarios" in result.output
    assert (out / MANIFEST_FILE).is_file()

    bad = CliRunner().invoke(
        app,
        [
            "export-viewer",
            "no_such_experiment",
            "--select",
            str(select),
            "--runs-root",
            str(tmp_path),
        ],
    )
    assert bad.exit_code != 0
