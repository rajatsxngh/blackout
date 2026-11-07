"""Smoke tests for the package and its command-line interface."""

import platform
import subprocess
import sys
import tomllib
from pathlib import Path

from typer.testing import CliRunner

from blackout import __version__
from blackout.cli import app

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"
runner = CliRunner()


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
