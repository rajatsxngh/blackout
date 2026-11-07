"""Replay: every logged run reproduces exactly, and any tampering is caught where it happened."""

import tempfile
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from blackout.logs.reader import read_log
from blackout.logs.replay import ReplayMismatchError, replay
from blackout.scenarios.schema import Scenario, load_scenario
from blackout.scoring.scorer import score
from tests.helpers import (
    CASCADE,
    DEMO,
    HANDMADE_RUNS,
    RandomController,
    handmade_controller,
    read_lines,
    record,
    write_lines,
)
from tests.strategies import scenarios


@pytest.mark.parametrize(("scenario", "contestant"), HANDMADE_RUNS)
def test_handmade_runs_replay_exactly(scenario: Path, contestant: str, tmp_path: Path) -> None:
    recorded = record(scenario, handmade_controller(scenario, contestant), tmp_path)
    replayed = replay(read_log(recorded.log_path))
    assert replayed.episode.tick_hashes == recorded.episode.tick_hashes
    assert replayed.episode.run_hash == recorded.episode.run_hash
    assert replayed.report == dict(recorded.episode.done.metrics)


@pytest.fixture
def demo_records(tmp_path: Path) -> list[dict[str, Any]]:
    return read_lines(record(DEMO, handmade_controller(DEMO, "scripted"), tmp_path).log_path)


def tampered(records: list[dict[str, Any]], tmp_path: Path) -> Path:
    return write_lines(records, tmp_path / "tampered.jsonl")


def decision_at(records: list[dict[str, Any]], tick: int) -> dict[str, Any]:
    return next(r for r in records if r["type"] == "dp" and r["t"] == tick)


def test_a_changed_state_hash_is_caught_at_its_tick(
    demo_records: list[dict[str, Any]], tmp_path: Path
) -> None:
    tick_50 = next(r for r in demo_records if r["type"] == "tick" and r["t"] == 50)
    tick_50["h"] = "0" * 32
    with pytest.raises(ReplayMismatchError, match="differs from the log at tick 50"):
        replay(read_log(tampered(demo_records, tmp_path)))


def test_a_changed_action_is_caught_the_next_minute(
    demo_records: list[dict[str, Any]], tmp_path: Path
) -> None:
    (deliver,) = decision_at(demo_records, 5)["actions"]
    deliver["raw"]["amount_l"] = 100  # deliver less than the log's outcome implies
    with pytest.raises(ReplayMismatchError, match="differs from the log at tick 6"):
        replay(read_log(tampered(demo_records, tmp_path)))


def test_a_changed_outcome_is_caught(demo_records: list[dict[str, Any]], tmp_path: Path) -> None:
    decision_at(demo_records, 18)["actions"][0]["accepted"] = False
    with pytest.raises(ReplayMismatchError, match="action outcomes differ"):
        replay(read_log(tampered(demo_records, tmp_path)))


def test_a_changed_observation_is_caught(
    demo_records: list[dict[str, Any]], tmp_path: Path
) -> None:
    decision_at(demo_records, 9)["obs"] = "f" * 32
    with pytest.raises(ReplayMismatchError, match="at tick 9: the observation differs"):
        replay(read_log(tampered(demo_records, tmp_path)))


def test_a_missing_decision_point_is_caught(
    demo_records: list[dict[str, Any]], tmp_path: Path
) -> None:
    last = max(i for i, r in enumerate(demo_records) if r["type"] == "dp")
    tick = demo_records[last]["t"]
    del demo_records[last]
    with pytest.raises(ReplayMismatchError, match=f"decision point at tick {tick} that isn't"):
        replay(read_log(tampered(demo_records, tmp_path)))


def test_a_truncated_log_is_caught(demo_records: list[dict[str, Any]], tmp_path: Path) -> None:
    cut = next(i for i, r in enumerate(demo_records) if r["type"] == "tick" and r["t"] == 60)
    with pytest.raises(ReplayMismatchError, match="ran past tick 60, where the log ends"):
        replay(read_log(tampered(demo_records[: cut + 1], tmp_path)))


def test_replaying_against_another_scenario_is_refused(
    demo_records: list[dict[str, Any]], tmp_path: Path
) -> None:
    log = read_log(tampered(demo_records, tmp_path))
    with pytest.raises(ReplayMismatchError, match="isn't the one this run was recorded on"):
        replay(log, load_scenario(CASCADE))


@given(scenario=scenarios(), seed=st.integers(0, 2**32 - 1))
def test_random_runs_replay_and_score_cleanly(scenario: Scenario, seed: int) -> None:
    with tempfile.TemporaryDirectory() as out:
        recorded = record(scenario, RandomController(seed), Path(out))
        log = read_log(recorded.log_path)
        assert replay(log).episode.run_hash == recorded.episode.run_hash
        card = score(log)
        assert card.ok, card.discrepancies
        assert card.critical_downtime_min == recorded.episode.done.metrics["critical_downtime_min"]
