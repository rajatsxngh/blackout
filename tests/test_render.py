"""The ASCII debugging view."""

from blackout.controllers.scripted import ScriptedController, load_script
from blackout.env import BlackoutEnv, DecisionRequest
from blackout.scenarios.schema import load_scenario
from blackout.sim.render_ascii import render_ascii
from tests.helpers import DEMO


def test_undiscovered_block_is_drawn_as_x() -> None:
    env = BlackoutEnv(load_scenario(DEMO))
    env.reset()
    picture = render_ascii(env.engine)
    assert picture.splitlines()[0].split() == ["Alder", "Birch", "Cedar", "Elm", "Maple", "Oak"]
    between_3rd_and_4th = picture.splitlines()[6]
    assert between_3rd_and_4th.split() == ["|", "|", "|", "|", "x", "|"]
    assert "D1 Northside Fuel Depot at A1: 3000 L in stock" in picture


def test_discovered_block_and_stuck_truck() -> None:
    env = BlackoutEnv(load_scenario(DEMO))
    controller = ScriptedController(load_script(DEMO.with_suffix(".script.json")))
    request = env.reset()
    while isinstance(request, DecisionRequest) and request.tick < 18:
        for action in controller.decide(request.observations["all"]):
            env.act("all", action)
        request = env.advance()
    lines = render_ascii(env.engine).splitlines()
    assert "T1" in lines[5]  # 3rd Ave row: the truck waits at Maple & 3rd
    assert lines[6].split()[4] == "#"  # the block is now known
    assert "  T1 at M3: 500/500 L, stuck, task deliver H1 via M4>M5" in lines
