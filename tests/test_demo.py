"""The hand-made demo: a hidden block, a reroute, and a delivery that saves (or doesn't) a hospital.

The golden run hashes pin the whole trajectory. They must match on Windows and Linux (CI runs
both). If physics changes on purpose, update them and say why in docs/DECISIONS.md.
"""

from blackout.controllers.do_nothing import DoNothingController
from blackout.controllers.scripted import Script, ScriptedController, load_script
from blackout.env import BlackoutEnv, EpisodeResult, run_episode
from blackout.scenarios.schema import load_scenario
from blackout.sim.actions import Reroute
from blackout.sim.triggers import TriggerKind
from tests.helpers import DEMO

GOLDEN_SCRIPTED = "90ccdd5d578a64b3d1f585c7ac220fe9"
GOLDEN_DO_NOTHING = "1219419e1f5d8ec054127036931ce4c7"


def demo_script() -> Script:
    return load_script(DEMO.with_suffix(".script.json"))


def run_demo(controller: ScriptedController | DoNothingController) -> EpisodeResult:
    return run_episode(BlackoutEnv(load_scenario(DEMO)), controller)


def test_scripted_demo_tells_the_story() -> None:
    result = run_demo(ScriptedController(demo_script()))
    by_tick = {decision.tick: decision for decision in result.decisions}

    assert [r.accepted for r in by_tick[0].results] == [True]
    deliver = by_tick[5].results[0]
    assert deliver.route == ("A1", "B1", "C1", "E1", "M1", "M2", "M3", "M4", "M5")
    assert deliver.eta_minutes == 16

    stuck = by_tick[18]
    assert {t.kind for t in stuck.triggers} == {TriggerKind.VEHICLE_STUCK}
    reroute = stuck.results[0]
    assert reroute.accepted
    assert reroute.route == ("M3", "E3", "E4", "M4", "M5")

    delivered = [t.detail for t in by_tick[34].triggers if t.kind is TriggerKind.TASK_COMPLETED]
    assert delivered == ["delivered 500 L to H1"]
    assert result.done.metrics["critical_downtime_min"] == 0
    assert result.done.metrics["stuck_events"] == 1


def test_doing_nothing_lets_the_hospital_go_dark() -> None:
    result = run_demo(DoNothingController())
    went_dark = [
        decision.tick
        for decision in result.decisions
        if any(t.kind is TriggerKind.HOSPITAL_STATUS for t in decision.triggers)
    ]
    assert went_dark == [40]
    assert result.done.metrics["dark_minutes"] == {"H1": 200, "H2": 0}


def test_rerouting_late_saves_the_hospital_too_late() -> None:
    steps = list(demo_script().steps)
    steps[2] = steps[2].model_copy(update={"at_tick": 40})
    assert isinstance(steps[2].action, Reroute)
    result = run_demo(ScriptedController(Script(steps=steps)))
    assert result.done.metrics["dark_minutes"] == {"H1": 16, "H2": 0}  # dark from 40 to 55


def test_runs_are_deterministic() -> None:
    first = run_demo(ScriptedController(demo_script()))
    second = run_demo(ScriptedController(demo_script()))
    assert first.tick_hashes == second.tick_hashes
    assert len(first.tick_hashes) == 241


def test_golden_run_hashes() -> None:
    assert run_demo(ScriptedController(demo_script())).run_hash == GOLDEN_SCRIPTED
    assert run_demo(DoNothingController()).run_hash == GOLDEN_DO_NOTHING
