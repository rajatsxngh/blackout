"""M8: the test guard (no batch on the held-out suite before a committed pre-registration) and
the test-bank tooling (check, lock). None of these tests reads data/phrasing/test: they build
throwaway banks from the train bank in a temporary folder."""

import shutil
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from blackout.cli import app
from blackout.experiments.guard import (
    PREREGISTRATION,
    guard_problems,
    hashes_block,
    parse_block,
)
from blackout.scenarios.text.bank_check import (
    check_bank,
    lock_path,
    lock_problems,
    write_lock,
)
from blackout.scenarios.text.banks import TRAIN_BANK

ROOT = Path(__file__).resolve().parents[1]
HASHES = {"code": "c0de", "test_bank": "ba4c"}
runner = CliRunner()


def fake_hashes(root: Path) -> dict[str, str]:
    return dict(HASHES)


def git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=root,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-q")
    (tmp_path / "docs").mkdir()
    return tmp_path


def test_other_suites_pass_the_guard(repo: Path) -> None:
    assert guard_problems("dev", repo, hashes=fake_hashes) == []


def test_test_suite_needs_a_preregistration(repo: Path) -> None:
    (problem,) = guard_problems("test", repo, hashes=fake_hashes)
    assert "held out" in problem


def test_preregistration_must_be_committed_with_matching_hashes(repo: Path) -> None:
    prereg = repo / PREREGISTRATION
    prereg.write_text("# Pre-registration\n\n" + hashes_block(HASHES), encoding="utf-8")
    assert any("isn't committed" in p for p in guard_problems("test", repo, hashes=fake_hashes))
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "prereg")
    assert guard_problems("test", repo, hashes=fake_hashes) == []
    prereg.write_text(prereg.read_text(encoding="utf-8") + "edit\n", encoding="utf-8")
    assert any("uncommitted" in p for p in guard_problems("test", repo, hashes=fake_hashes))
    git(repo, "commit", "-q", "-am", "edit")
    drifted = {**HASHES, "code": "beef"}
    problems = guard_problems("test", repo, hashes=lambda root: drifted)
    assert problems == ["frozen code hash 'c0de' doesn't match the current 'beef'"]


def test_preregistration_without_a_block_is_refused(repo: Path) -> None:
    (repo / PREREGISTRATION).write_text("# no hashes\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "prereg")
    assert guard_problems("test", repo, hashes=fake_hashes) == [
        "docs/PREREGISTRATION.md has no ```frozen-hashes block"
    ]
    assert parse_block(hashes_block(HASHES)) == HASHES


def test_batch_refuses_the_test_suite(tmp_path: Path) -> None:
    matrix = tmp_path / "m.yaml"
    matrix.write_text("experiment: x\nsuite: test\ncontestants: [rules]\n", encoding="utf-8")
    result = runner.invoke(app, ["batch", "--matrix", str(matrix)])
    assert result.exit_code != 0
    assert "held out" in result.output


def test_prereg_hashes_prints_a_block() -> None:
    result = runner.invoke(app, ["prereg-hashes"])
    assert result.exit_code == 0, result.output
    block = parse_block(result.output)
    assert block is not None
    assert set(block) == {"code", "scorer", "train_bank", "test_bank", "test_manifest", "prompts"}


# --- the bank tooling ------------------------------------------------------------------------


@pytest.fixture
def bank(tmp_path: Path) -> Path:
    """A stand-in bank: a copy of the train bank (never the real test bank)."""
    target = tmp_path / "phrasing" / "test"
    shutil.copytree(ROOT / TRAIN_BANK, target)
    return target


def test_check_flags_copies_of_the_train_bank_by_id(bank: Path) -> None:
    report = check_bank(bank, ROOT / TRAIN_BANK)
    assert report.ok
    assert report.counts["road_blocked"] >= 8
    assert report.warnings  # a copy reads exactly like the train bank
    assert all("reads like train" in w and "{" not in w for w in report.warnings)


def test_check_reports_errors_without_quoting_text(bank: Path) -> None:
    (bank / "_injection.yaml").unlink()
    assert any("_injection.yaml" in e for e in check_bank(bank, ROOT / TRAIN_BANK).errors)
    (bank / "road_blocked.yaml").write_text(
        "fact_type: road_blocked\nphrasings:\n  - id: tb01\n    text: 'secret wording {road}'\n",
        encoding="utf-8",
    )
    report = check_bank(bank, ROOT / TRAIN_BANK)
    assert not report.ok
    assert "secret wording" not in " ".join(report.errors)
    assert check_bank(bank.parent / "missing").errors


def test_lock_catches_any_change(bank: Path) -> None:
    assert lock_problems(bank) == [
        f"{lock_path(bank)} is missing: run `blackout bank lock {bank.as_posix()}`"
    ]
    write_lock(bank)
    assert lock_problems(bank) == []
    (bank / "_noise.yaml").write_text(
        (bank / "_noise.yaml").read_text(encoding="utf-8") + "\n", encoding="utf-8"
    )
    assert lock_problems(bank) == [f"{bank.as_posix()}/_noise.yaml doesn't match test.lock.json"]
    assert lock_problems(bank.parent / "missing")


def test_cli_bank_check_and_lock(bank: Path) -> None:
    checked = runner.invoke(app, ["bank", "check", str(bank)])
    assert checked.exit_code == 0, checked.output
    assert "warning" in checked.output
    locked = runner.invoke(app, ["bank", "lock", str(bank)])
    assert locked.exit_code == 0, locked.output
    assert lock_path(bank).is_file()


def test_gen_test_suite_needs_a_locked_bank(tmp_path: Path) -> None:
    missing = runner.invoke(app, ["gen", "--suite", "test", "--bank", str(tmp_path / "none")])
    assert missing.exit_code == 1
    assert "TEST_BANK_GUIDE" in missing.output
    sample = runner.invoke(app, ["gen", "--suite", "test", "--sample", "3"])
    assert sample.exit_code != 0
    assert "never printed" in sample.output
