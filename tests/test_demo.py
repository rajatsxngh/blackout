"""The hand-made scenarios, end to end, and their golden run hashes.

hello_blackout: a hidden block, a reroute, and a delivery that saves (or doesn't) a hospital
while a crew repairs its substation. cascade: an overload that spreads unless load is shed.

The golden run hashes pin the whole trajectory. They must match on Windows and Linux (CI runs
both). If physics changes on purpose, update them and say why in docs/DECISIONS.md.
"""

from pathlib import Path

import pytest

from blackout.controllers.do_nothing import DoNothingController
from blackout.controllers.scripted import Script, ScriptedController, load_script
from blackout.env import BlackoutEnv, EpisodeResult, run_episode
from blackout.scenarios.schema import load_scenario
from blackout.sim.actions import Reroute
from blackout.sim.triggers import TriggerKind
from tests.helpers import CASCADE, DEMO

GOLDEN = {
    ("hello_blackout", "scripted"): "3e8b19b831efc1c9a41e55e0f9f77e5c",
    ("hello_blackout", "do-nothing"): "66ab4e7e9d616c04a19bae14ee836c7f",
    ("cascade", "scripted"): "e36d31986c4cfe5b4dfa54c9cca68a77",
    ("cascade", "do-nothing"): "bb6147ad4be4988cf3e14cede6263145",
}


def script_for(scenario: Path) -> Script:
    return load_script(scenario.with_suffix(".script.json"))


def run(scenario: Path, controller: ScriptedController | DoNothingController) -> EpisodeResult:
    return run_episode(BlackoutEnv(load_scenario(scenario)), controller)


def test_scripted_demo_tells_the_story() -> None:
    result = run(DEMO, ScriptedController(script_for(DEMO)))
    by_tick = {decision.tick: decision for decision in result.decisions}

    load, send_crew = by_tick[0].results
    assert load.accepted
    assert (send_crew.route, send_crew.eta_minutes) == (("O1", "O2", "O3", "O4"), 9)
    deliver = by_tick[5].results[0]
    assert deliver.route == ("A1", "B1", "C1", "E1", "M1", "M2", "M3", "M4", "M5")
    assert deliver.eta_minutes == 16
    assert {t.kind for t in by_tick[9].triggers} == {
        TriggerKind.VEHICLE_ARRIVED,
        TriggerKind.SUBSTATION_STATUS,
    }

    stuck = by_tick[18]
    assert {t.kind for t in stuck.triggers} == {TriggerKind.VEHICLE_STUCK}
    reroute = stuck.results[0]
    assert reroute.accepted
    assert reroute.route == ("M3", "E3", "E4", "M4", "M5")

    delivered = [t.detail for t in by_tick[34].triggers if t.kind is TriggerKind.TASK_COMPLETED]
    assert delivered == ["delivered 500 L to H1"]

    # The crew finishes at the end of minute 128; with every zone powered, the run stops at 129.
    assert (result.done.tick, result.done.reason) == (129, "stable")
    metrics = result.done.metrics
    assert metrics["critical_downtime_min"] == 0
    assert metrics["stuck_events"] == 1
    assert metrics["repairs_completed"] == 1
    assert metrics["power_restored_at"] == 129
    assert metrics["unmet_kw_min"] == 129 * 8000  # St. Mary's zone, until the repair


def test_doing_nothing_lets_the_hospital_go_dark() -> None:
    result = run(DEMO, DoNothingController())
    went_dark = [
        decision.tick
        for decision in result.decisions
        if any(t.kind is TriggerKind.HOSPITAL_STATUS for t in decision.triggers)
    ]
    assert went_dark == [40]
    assert result.done.metrics["dark_minutes"] == {"H1": 200, "H2": 0}
    assert result.done.metrics["power_restored_at"] is None


def test_rerouting_late_saves_the_hospital_too_late() -> None:
    steps = list(script_for(DEMO).steps)
    assert isinstance(steps[3].action, Reroute)
    steps[3] = steps[3].model_copy(update={"at_tick": 40})
    result = run(DEMO, ScriptedController(Script(steps=steps)))
    assert result.done.metrics["dark_minutes"] == {"H1": 16, "H2": 0}  # dark from 40 to 55


def test_runs_are_deterministic() -> None:
    first = run(DEMO, ScriptedController(script_for(DEMO)))
    second = run(DEMO, ScriptedController(script_for(DEMO)))
    assert first.tick_hashes == second.tick_hashes
    assert len(first.tick_hashes) == 130  # ticks 0 through 129


@pytest.mark.parametrize(("name", "contestant"), sorted(GOLDEN))
def test_golden_run_hashes(name: str, contestant: str) -> None:
    scenario = DEMO if name == "hello_blackout" else CASCADE
    controller = (
        ScriptedController(script_for(scenario))
        if contestant == "scripted"
        else DoNothingController()
    )
    assert run(scenario, controller).run_hash == GOLDEN[(name, contestant)]
