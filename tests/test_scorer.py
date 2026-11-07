"""The scorer: recomputes everything from the log and cross-checks it against the end record."""

import json
from pathlib import Path

import pytest

from blackout.controllers.scripted import Script, ScriptedController, load_script
from blackout.logs.reader import read_log
from blackout.logs.replay import ReplayMismatchError
from blackout.scoring.scorer import score
from blackout.sim.actions import DispatchTruck
from blackout.sim.entities import TaskKind
from tests.helpers import DEMO, HANDMADE_RUNS, handmade_controller, read_lines, record, write_lines


@pytest.mark.parametrize(("scenario", "contestant"), HANDMADE_RUNS)
def test_the_scorer_matches_the_engines_own_counters(
    scenario: Path, contestant: str, tmp_path: Path
) -> None:
    recorded = record(scenario, handmade_controller(scenario, contestant), tmp_path)
    card = score(recorded.log_path)
    engine = recorded.episode.done.metrics
    assert card.ok, card.discrepancies
    assert card.critical_downtime_min == engine["critical_downtime_min"]
    assert card.dark_minutes == engine["dark_minutes"]
    assert card.unmet_kw_min == engine["unmet_kw_min"]
    assert card.unmet_weighted_kw_min == engine["unmet_weighted_kw_min"]
    assert card.zone_outage_minutes == engine["zone_outage_minutes"]
    assert card.power_restored_at == engine["power_restored_at"]
    assert card.fuel_delivered_l == engine["fuel_delivered_l"]
    assert card.trips == engine["trips"]
    assert card.repairs_completed == engine["repairs_completed"]
    assert card.decision_points == engine["decision_points"]
    assert card.actions_accepted == engine["actions_accepted"]
    assert card.run_hash == recorded.episode.run_hash
    json.dumps(card.to_dict())  # score cards are JSON-ready


def test_rejected_actions_are_counted_from_the_decision_records(tmp_path: Path) -> None:
    steps = list(load_script(DEMO.with_suffix(".script.json")).steps)
    empty = DispatchTruck(truck_id="T1", task=TaskKind.DELIVER, target_id="H1", reason="too soon")
    steps.insert(0, steps[0].model_copy(update={"action": empty}))  # T1 has no fuel yet
    recorded = record(DEMO, ScriptedController(Script(steps=steps)), tmp_path)
    card = score(recorded.log_path)
    assert card.ok, card.discrepancies
    assert (card.actions, card.actions_accepted) == (5, 4)
    assert card.actions_rejected == {"empty_truck": 1}
    assert card.invalid_action_rate == pytest.approx(1 / 5)
    assert card.actions_by_kind == {"assign_crew": 1, "dispatch_truck": 3, "reroute": 1}
    assert card.llm_calls is None  # no model was involved


def test_a_tampered_end_record_is_a_discrepancy(tmp_path: Path) -> None:
    records = read_lines(record(DEMO, handmade_controller(DEMO, "scripted"), tmp_path).log_path)
    records[-1]["metrics"]["critical_downtime_min"] = 999
    records[-1]["metrics"]["actions_accepted"] = 1
    card = score(read_log(write_lines(records, tmp_path / "doctored.jsonl")))
    assert not card.ok
    assert any(
        d.startswith("critical_downtime_min: the end record says 999") for d in card.discrepancies
    )
    assert any(
        d.startswith("actions_accepted: the decision records give 4") for d in card.discrepancies
    )
    assert card.critical_downtime_min == 0  # the scorer reports what really happened


def test_a_run_without_an_end_record_is_flagged(tmp_path: Path) -> None:
    records = read_lines(record(DEMO, handmade_controller(DEMO, "do-nothing"), tmp_path).log_path)
    card = score(read_log(write_lines(records[:-1], tmp_path / "unfinished.jsonl")))
    assert card.discrepancies == ("the log has no end record, so the run didn't finish",)


def test_a_log_that_does_not_replay_cannot_be_scored(tmp_path: Path) -> None:
    records = read_lines(record(DEMO, handmade_controller(DEMO, "scripted"), tmp_path).log_path)
    records[3]["h"] = "1" * 32
    with pytest.raises(ReplayMismatchError):
        score(read_log(write_lines(records, tmp_path / "corrupt.jsonl")))


def test_model_usage_is_summed_from_the_reserved_fields(tmp_path: Path) -> None:
    records = read_lines(record(DEMO, handmade_controller(DEMO, "scripted"), tmp_path).log_path)
    decisions = [r for r in records if r["type"] == "dp"]
    decisions[0]["llm"] = {"calls": 2, "input_tokens": 900, "output_tokens": 80, "cost_usd": 0.01}
    decisions[1]["llm"] = {"calls": 1, "input_tokens": 400, "latency_ms": 1200}
    card = score(read_log(write_lines(records, tmp_path / "with_usage.jsonl")))
    assert (card.llm_calls, card.input_tokens, card.output_tokens) == (3, 1300, 80)
    assert card.cost_usd == pytest.approx(0.01)
    assert card.thinking_time_ms == 1200
