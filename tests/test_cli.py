"""Smoke tests for the package and its command-line interface."""

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
from tests.helpers import DEMO, ROOT

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
    result = runner.invoke(app, ["run", "--scenario", "hello_blackout", "--controller", "scripted"])
    assert result.exit_code == 0, result.output
    assert "vehicle_stuck(T1)" in result.output
    assert "T1 reroute: ok, route M3>E3>E4>M4>M5, arriving in 11 min" in result.output
    assert "critical downtime  0 min" in result.output


def test_run_do_nothing_by_path_with_ascii_map() -> None:
    result = runner.invoke(app, ["run", "-s", str(DEMO), "--render", "ascii", "-q"])
    assert result.exit_code == 0, result.output
    assert "Alder    Birch" in result.output
    assert "critical downtime  200 min (hospitals that lost power: 1)" in result.output


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
