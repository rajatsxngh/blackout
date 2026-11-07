"""Smoke tests for the package and its command-line interface."""

import json
import platform
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from blackout import __version__
from blackout.cli import app
from blackout.scenarios.suites import SUITES, Suite
from tests.helpers import DEMO, ROOT, read_lines, write_lines

PYPROJECT = ROOT / "pyproject.toml"
runner = CliRunner()


@pytest.fixture
def in_repo_root(monkeypatch: pytest.MonkeyPatch) -> None:
    """Scenario IDs are looked up under ./scenarios, so run from the repository root."""
    monkeypatch.chdir(ROOT)


def test_installed_version_matches_pyproject() -> None:
    project = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]
    assert __version__ == project["version"]


def test_help_lists_commands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "info" in result.output


def test_version_flag_prints_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.output.strip() == f"blackout {__version__}"


def test_info_reports_python_version() -> None:
    result = runner.invoke(app, ["info"])
    assert result.exit_code == 0
    assert platform.python_version() in result.output


def test_module_entry_point() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "blackout", "--version"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == f"blackout {__version__}"


@pytest.mark.usefixtures("in_repo_root")
def test_run_scripted_demo_by_id() -> None:
    args = ["run", "--scenario", "hello_blackout", "--controller", "scripted", "--no-log"]
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    assert "vehicle_stuck(T1)" in result.output
    assert "T1 reroute: ok, route M3>E3>E4>M4>M5, arriving in 11 min" in result.output
    assert "critical downtime  0 min" in result.output
    assert "log " not in result.output


def test_run_do_nothing_by_path_with_ascii_map() -> None:
    result = runner.invoke(app, ["run", "-s", str(DEMO), "--render", "ascii", "-q", "--no-log"])
    assert result.exit_code == 0, result.output
    assert "Alder    Birch" in result.output
    assert "critical downtime  200 min (hospitals that lost power: 1)" in result.output
    assert "fully restored at never" in result.output


@pytest.mark.usefixtures("in_repo_root")
def test_run_cascade_scenarios() -> None:
    saved = runner.invoke(app, ["run", "-s", "cascade", "-c", "scripted", "--no-log"])
    assert saved.exit_code == 0, saved.output
    assert "shed Z3: ok, Z3 shed (6000 kW); S2 at 30000/30000 kW" in saved.output
    assert "0 trip(s), 1 repair(s), fully restored at t=103" in saved.output
    collapsed = runner.invoke(app, ["run", "-s", "cascade", "-q", "--no-log"])
    assert "2 trip(s)" in collapsed.output


def test_run_logs_then_replay_and_score(tmp_path: Path) -> None:
    ran = runner.invoke(
        app, ["run", "-s", str(DEMO), "-c", "scripted", "-q", "--out", str(tmp_path)]
    )
    assert ran.exit_code == 0, ran.output
    run_dir = tmp_path / "hello_blackout" / "scripted__none__r0"
    assert (run_dir / "run.jsonl.gz").is_file()
    assert (run_dir / "summary.json").is_file()
    assert "log                " in ran.output

    replayed = runner.invoke(app, ["replay", str(run_dir)])
    assert replayed.exit_code == 0, replayed.output
    assert "hello_blackout/scripted__none__r0: verified 130 ticks and 14 decision points" in (
        replayed.output
    )

    scored = runner.invoke(app, ["score", str(tmp_path)])
    assert scored.exit_code == 0, scored.output
    assert "downtime 0 min, unmet 1032000 kW-min, restored t=129, 4 actions (0 rejected) - ok" in (
        scored.output
    )
    as_json = runner.invoke(app, ["score", str(run_dir / "run.jsonl.gz"), "--json"])
    (card,) = json.loads(as_json.output)
    assert card["run_hash"] == "3e8b19b831efc1c9a41e55e0f9f77e5c"


def test_replay_and_score_fail_loudly_on_a_doctored_log(tmp_path: Path) -> None:
    runner.invoke(app, ["run", "-s", str(DEMO), "-q", "--out", str(tmp_path)])
    log_path = tmp_path / "hello_blackout" / "do-nothing__none__r0" / "run.jsonl.gz"
    records = read_lines(log_path)
    records[-1]["metrics"]["critical_downtime_min"] = 0  # claim nobody went dark
    doctored = write_lines(records, tmp_path / "doctored.jsonl")
    scored = runner.invoke(app, ["score", str(doctored)])
    assert scored.exit_code == 1
    assert "1 DISCREPANCIES" in scored.output
    assert "discrepancy: critical_downtime_min: the end record says 0, the replay gives 200" in (
        scored.output
    )

    records[5]["h"] = "2" * 32
    corrupt = write_lines(records, tmp_path / "corrupt.jsonl")
    replayed = runner.invoke(app, ["replay", str(corrupt)])
    assert replayed.exit_code == 1
    assert "replay failed" in replayed.output
    assert runner.invoke(app, ["score", str(corrupt)]).exit_code == 1


def test_score_needs_some_logs(tmp_path: Path) -> None:
    result = runner.invoke(app, ["score", str(tmp_path)])
    assert result.exit_code == 2
    assert "no run logs found" in result.output


@pytest.mark.usefixtures("in_repo_root")
def test_run_unknown_scenario_fails() -> None:
    result = runner.invoke(app, ["run", "--scenario", "no_such_scenario"])
    assert result.exit_code == 2
    assert "no_such_scenario" in result.output


def test_run_scripted_without_a_script_fails(tmp_path: Path) -> None:
    lonely = tmp_path / "lonely.json"
    shutil.copy(DEMO, lonely)
    result = runner.invoke(app, ["run", "-s", str(lonely), "-c", "scripted"])
    assert result.exit_code == 2
    assert "script not found" in result.output


TINY = Suite("dev", "tiny", ("T04",), seeds=1, levels=(0, 1, 2, 3), tiers=("medium",))


@pytest.fixture
def tiny_suite(monkeypatch: pytest.MonkeyPatch) -> Suite:
    """A one-world suite, so gen runs in seconds."""
    monkeypatch.setitem(SUITES, "tiny", TINY)
    return TINY


@pytest.mark.usefixtures("in_repo_root", "tiny_suite")
def test_gen_writes_checks_and_samples_a_suite(tmp_path: Path) -> None:
    out = tmp_path / "suite"
    result = runner.invoke(app, ["gen", "--suite", "tiny", "--out", str(out), "--sample", "40"])
    assert result.exit_code == 0, result.output
    assert len(list(out.glob("tiny_*.json"))) == 4
    assert "Tier mix (worlds): easy 0, medium 1, hard 0" in result.output
    assert "[FALSE]" in result.output
    assert "[INJECTION" in result.output
    again = runner.invoke(app, ["gen", "--suite", "tiny", "--check", "--out", str(out)])
    assert again.exit_code == 0, again.output
    assert "4 scenarios in" in again.output
    missing = runner.invoke(app, ["gen", "--suite", "tiny", "--check", "--out", str(tmp_path)])
    assert missing.exit_code == 1
    assert "missing" in missing.output


def test_gen_refuses_unknown_and_frozen_suites() -> None:
    result = runner.invoke(app, ["gen", "--suite", "nowhere"])
    assert result.exit_code != 0
    assert "unknown suite" in result.output
    frozen = runner.invoke(app, ["gen", "--suite", "dev-v0"])
    assert frozen.exit_code != 0
    assert "frozen" in frozen.output
