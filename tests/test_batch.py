"""The M6 batch runner: matrices, parallel runs, resume, and result tables.

Done-when check (docs/PRD.md section 12, M6): a re-run with ``--resume`` skips completed runs.
"""

import json
import shutil
from pathlib import Path

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from blackout.cli import app
from blackout.experiments.matrix import Matrix, load_matrix
from blackout.experiments.results import group_rows, run_rows, write_results
from blackout.experiments.runner import Job, code_digest, plan, run_batch, run_config
from blackout.logs.schema import SUMMARY_FILE
from blackout.scenarios.schema import load_scenario
from blackout.scenarios.suites import MANIFEST
from tests.helpers import ROOT

DEV_V0 = ROOT / "scenarios" / "dev-v0"
PICKED = ("dev0_t01_s0_l0", "dev0_t02_s0_l1")
runner = CliRunner()


@pytest.fixture
def scenario_root(tmp_path: Path) -> Path:
    """A two-scenario suite "mini", copied from dev-v0 with a matching manifest."""
    root = tmp_path / "scenarios"
    folder = root / "mini"
    folder.mkdir(parents=True)
    manifest = json.loads((DEV_V0 / MANIFEST).read_text(encoding="utf-8"))
    manifest["scenarios"] = [e for e in manifest["scenarios"] if e["id"] in PICKED]
    (folder / MANIFEST).write_text(json.dumps(manifest), encoding="utf-8")
    for name in PICKED:
        shutil.copy(DEV_V0 / f"{name}.json", folder / f"{name}.json")
    return root


def mini(**changes: object) -> Matrix:
    spec: dict[str, object] = {
        "experiment": "mini",
        "suite": "mini",
        "contestants": ["do-nothing", "rules"],
    }
    return Matrix.model_validate(spec | changes)


# --- matrices -------------------------------------------------------------------------------


def test_dev_baselines_matrix_covers_dev_v0() -> None:
    matrix = load_matrix(ROOT / "configs" / "matrices" / "dev_baselines.yaml")
    assert (matrix.experiment, matrix.suite, matrix.runs) == ("dev_baselines", "dev-v0", 1)
    assert matrix.contestants == ("do-nothing", "rules")
    assert len(matrix.scenarios(ROOT / "scenarios")) == 18


@pytest.mark.parametrize(
    ("changes", "problem"),
    [
        ({"contestants": ["oracle"]}, "unknown contestants"),
        ({"contestants": ["rules", "rules"]}, "more than once"),
        ({"suite": "test"}, "held out"),
        ({"contestants": []}, "at least 1"),
        ({"runs": 0}, "greater than 0"),
        ({"surprise": 1}, "Extra inputs"),
    ],
)
def test_matrix_rejects_bad_specs(changes: dict[str, object], problem: str) -> None:
    with pytest.raises(ValidationError, match=problem):
        mini(**changes)


def test_matrix_filters_and_checks_the_manifest(scenario_root: Path) -> None:
    assert [s.id for _, s in mini().scenarios(scenario_root)] == list(PICKED)
    assert [s.id for _, s in mini(levels=[1]).scenarios(scenario_root)] == ["dev0_t02_s0_l1"]
    assert [s.id for _, s in mini(templates=["T01"]).scenarios(scenario_root)] == [PICKED[0]]
    with pytest.raises(FileNotFoundError, match="generate the suite first"):
        mini(suite="nowhere").scenarios(scenario_root)
    manifest = scenario_root / "mini" / MANIFEST
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["scenarios"][0]["content_hash"] = "0" * 32
    manifest.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="doesn't match its manifest"):
        mini().scenarios(scenario_root)


# --- running and resuming -------------------------------------------------------------------


def test_plan_has_one_job_per_scenario_contestant_and_run(
    scenario_root: Path, tmp_path: Path
) -> None:
    jobs = plan(mini(runs=2), scenario_root=scenario_root, runs_root=tmp_path / "runs")
    assert len(jobs) == 2 * 2 * 2
    first = jobs[0]
    assert first.run_dir == tmp_path / "runs" / "mini" / PICKED[0] / "do-nothing__none__r0"
    assert jobs[1].run_index == 1
    assert len({job.config_hash for job in jobs}) == len(jobs)
    assert not first.is_done()


def test_config_hash_covers_scenario_contestant_rules_and_code() -> None:
    scenario = load_scenario(DEV_V0 / f"{PICKED[0]}.json")
    config = run_config(scenario, "rules", 0)
    assert set(config) == {"scenario", "contestant", "engine", "code"}
    assert config["code"] == {"version": "0.1.0", "digest": code_digest()}
    assert run_config(scenario, "rules", 1) != config


def test_resume_skips_completed_runs_and_reruns_changed_ones(
    scenario_root: Path, tmp_path: Path
) -> None:
    jobs = plan(mini(), scenario_root=scenario_root, runs_root=tmp_path / "runs")
    first = run_batch(jobs)
    assert (first.ran, first.skipped) == (4, 0)
    assert all(o.card.ok for o in first.outcomes)
    summary = json.loads(jobs[0].summary_path.read_text(encoding="utf-8"))
    assert summary["config_hash"] == jobs[0].config_hash
    assert summary["experiment"] == "mini"

    again = run_batch(jobs, resume=True)
    assert (again.ran, again.skipped) == (0, 4)
    assert [o.card.run_hash for o in again.outcomes] == [o.card.run_hash for o in first.outcomes]

    # A run whose config changed (or whose summary is unreadable) runs again.
    stale = jobs[1].summary_path
    stale.write_text(json.dumps({"config_hash": "old"}), encoding="utf-8")
    jobs[2].summary_path.write_text("{not json", encoding="utf-8")
    third = run_batch(jobs, resume=True)
    assert [o.ran for o in third.outcomes] == [False, True, True, False]

    registry = tmp_path / "runs" / "registry.jsonl"
    lines = [json.loads(line) for line in registry.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 4 + 2
    assert lines[0]["config_hash"] == jobs[0].config_hash


def test_without_resume_everything_runs_again(scenario_root: Path, tmp_path: Path) -> None:
    jobs = plan(mini(contestants=["rules"]), scenario_root=scenario_root, runs_root=tmp_path)
    run_batch(jobs)
    assert run_batch(jobs).ran == len(jobs)


def test_parallel_workers_give_the_same_runs(scenario_root: Path, tmp_path: Path) -> None:
    serial = plan(mini(), scenario_root=scenario_root, runs_root=tmp_path / "serial")
    parallel = plan(mini(), scenario_root=scenario_root, runs_root=tmp_path / "parallel")
    seen: list[Job] = []
    one = run_batch(serial, workers=1)
    two = run_batch(parallel, workers=2, on_done=lambda o: seen.append(o.job))
    assert seen == parallel  # in job order, whatever order they finished in
    assert [o.card.run_hash for o in two.outcomes] == [o.card.run_hash for o in one.outcomes]
    for a, b in zip(serial, parallel, strict=True):
        assert (a.run_dir / "run.jsonl.gz").read_bytes() == (
            b.run_dir / "run.jsonl.gz"
        ).read_bytes()


# --- results --------------------------------------------------------------------------------


def test_result_tables(scenario_root: Path, tmp_path: Path) -> None:
    jobs = plan(mini(), scenario_root=scenario_root, runs_root=tmp_path / "runs")
    outcomes = run_batch(jobs).outcomes
    scenarios = {o.job.scenario_id: load_scenario(o.job.scenario_path) for o in outcomes}
    rows = run_rows(outcomes, scenarios)
    by = {(r.scenario, r.contestant): r for r in rows}
    nothing, rules = by[(PICKED[0], "do-nothing")], by[(PICKED[0], "rules")]
    assert nothing.da == 0.0
    assert nothing.beats_do_nothing is False
    assert rules.critical_downtime_min < nothing.critical_downtime_min
    assert rules.beats_do_nothing is True
    assert rules.do_nothing_downtime_min == nothing.critical_downtime_min

    groups = {(g.template, g.level, g.contestant): g for g in group_rows(rows)}
    assert groups[("T01", 0, "rules")].beats_do_nothing == 1
    assert groups[("all", 1, "rules")].scenarios == 1
    assert groups[("all", 0, "do-nothing")].mean_invalid_action_rate is None  # no actions

    paths = write_results("mini", outcomes, tmp_path / "results")
    assert [p.name for p in paths] == ["runs.csv", "summary.csv", "results.md"]
    table = (tmp_path / "results" / "mini" / "results.md").read_text(encoding="utf-8")
    assert "| T01 | L0 | rules | 1 | 1 | 0.0 | 1.000 | 1/1 |" in table
    assert "| L1 | 3 | 3 | 3 | 1.000 | 1.000 | 1.000 |" in table
    assert "Scorer discrepancies: 0." in table
    csv_lines = (tmp_path / "results" / "mini" / "runs.csv").read_text(encoding="utf-8")
    assert csv_lines.splitlines()[0].startswith("scenario,template,level,contestant,")
    assert len(csv_lines.splitlines()) == 1 + 4


def test_without_a_do_nothing_run_the_baseline_comes_from_the_scenario(
    scenario_root: Path, tmp_path: Path
) -> None:
    jobs = plan(mini(contestants=["rules"]), scenario_root=scenario_root, runs_root=tmp_path)
    outcomes = run_batch(jobs).outcomes
    scenarios = {o.job.scenario_id: load_scenario(o.job.scenario_path) for o in outcomes}
    (row, *_) = run_rows(outcomes, scenarios)
    validity = scenarios[row.scenario].validity
    assert validity is not None
    assert row.do_nothing_downtime_min == validity.do_nothing_critical_downtime


# --- CLI ------------------------------------------------------------------------------------


def test_cli_batch_and_resume(scenario_root: Path, tmp_path: Path) -> None:
    matrix = tmp_path / "mini.yaml"
    matrix.write_text("experiment: mini\nsuite: mini\ncontestants: [rules]\n", encoding="utf-8")
    args = [
        "batch",
        "--matrix",
        str(matrix),
        "--workers",
        "1",
        "--scenarios",
        str(scenario_root),
        "--out",
        str(tmp_path / "runs"),
        "--results",
        str(tmp_path / "results"),
    ]
    first = runner.invoke(app, args)
    assert first.exit_code == 0, first.output
    assert "Ran 2, skipped 0" in first.output
    second = runner.invoke(app, [*args, "--resume"])
    assert second.exit_code == 0, second.output
    assert "Ran 0, skipped 2" in second.output
    assert "kept dev0_t01_s0_l0/rules__none__r0" in second.output
    assert (tmp_path / "results" / "mini" / "results.md").is_file()


def test_cli_batch_reports_problems(scenario_root: Path, tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("experiment: x\nsuite: test\ncontestants: [rules]\n", encoding="utf-8")
    result = runner.invoke(app, ["batch", "--matrix", str(bad)])
    assert result.exit_code != 0
    assert "held out" in result.output
    missing = tmp_path / "missing.yaml"
    missing.write_text("experiment: x\nsuite: nowhere\ncontestants: [rules]\n", encoding="utf-8")
    result = runner.invoke(
        app, ["batch", "--matrix", str(missing), "--scenarios", str(scenario_root)]
    )
    assert result.exit_code == 1
    assert "generate the suite first" in result.output


def test_cli_batch_fails_on_scorer_discrepancies(
    scenario_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace

    import blackout.experiments.runner as batch_runner

    real = batch_runner.score

    def tampered(path: Path) -> object:
        return replace(real(path), discrepancies=("made up",))

    monkeypatch.setattr(batch_runner, "score", tampered)
    matrix = tmp_path / "mini.yaml"
    matrix.write_text("experiment: mini\nsuite: mini\ncontestants: [rules]\n", encoding="utf-8")
    result = runner.invoke(
        app,
        [
            "batch",
            "--matrix",
            str(matrix),
            "--workers",
            "1",
            "--scenarios",
            str(scenario_root),
            "--out",
            str(tmp_path / "runs"),
            "--results",
            str(tmp_path / "results"),
        ],
    )
    assert result.exit_code == 1
    assert "1 DISCREPANCIES" in result.output


def test_cli_run_accepts_rules(tmp_path: Path) -> None:
    path = DEV_V0 / f"{PICKED[0]}.json"
    result = runner.invoke(
        app, ["run", "-s", str(path), "-c", "rules", "-q", "--out", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output
    assert "critical downtime  0 min" in result.output
    assert (tmp_path / PICKED[0] / "rules__none__r0" / SUMMARY_FILE).is_file()
