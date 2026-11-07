"""The ASCII debugging view."""

from blackout.controllers.scripted import ScriptedController, load_script
from blackout.env import BlackoutEnv, DecisionRequest
from blackout.scenarios.schema import load_scenario
from blackout.sim.render_ascii import render_ascii
from tests.helpers import DEMO


def test_map_shows_places_hidden_blocks_and_dark_zones() -> None:
    env = BlackoutEnv(load_scenario(DEMO))
    env.reset()
    picture = render_ascii(env.engine)
    lines = picture.splitlines()
    assert lines[0].split() == ["Alder", "Birch", "Cedar", "Elm", "Maple", "Oak"]
    assert lines[1].split()[1].startswith("D1+-")  # the depot and the truck share Alder & 1st
    assert lines[1].split()[-1] == "C1"  # the crew waits at Oak & 1st
    assert lines[6].split() == ["|", "|", "|", "|", "x", "|"]  # undiscovered block on Maple
    assert lines[9].split()[-1] == ":"  # Oak & 5th: St. Mary's zone has no power
    assert lines[11].split()[:4] == ["6th", ".", "-----", "H2"]  # Riverside Clinic, Birch & 6th
    assert "  S2 East Substation at O4: failed, 0/30000 kW" in lines
    assert "  C1 at O1: idle" in lines
    assert "Z9 dark" in picture
    assert "  D1 Northside Fuel Depot at A1: 3000 L in stock" in lines


def test_discovered_block_stuck_truck_and_repair_in_progress() -> None:
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
    assert "  S2 East Substation at O4: repairing by C1, 111 min left, 0/30000 kW" in lines
