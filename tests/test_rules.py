"""C1 Rules (blackout.controllers.rules): each rule on small worlds, then the M6 done-when
checks on dev-v0: deterministic, and better than do-nothing on at least 90% of L0 scenarios."""

from pathlib import Path
from typing import Any

import pytest

from blackout.controllers.do_nothing import DoNothingController
from blackout.controllers.registry import CONTESTANTS, make_controller
from blackout.controllers.rules import RulesController
from blackout.env import BlackoutEnv, DecisionRequest, EpisodeResult, run_episode
from blackout.scenarios.schema import Scenario, load_scenario
from blackout.sim.actions import ActionResult, AssignCrew, Reroute, RestoreLoad, ShedLoad
from blackout.sim.city import edge_id
from blackout.sim.routing import shortest_path
from tests.helpers import ROOT, grid, make_scenario, report, substation

DEV_V0 = sorted((ROOT / "scenarios" / "dev-v0").glob("dev0_*.json"))


def fail(substation_id: str, tick: int, repair: int = 20) -> dict[str, Any]:
    return {
        "kind": "substation_fail",
        "tick": tick,
        "substation": substation_id,
        "repair_minutes": repair,
    }


def play(scenario: Scenario) -> tuple[BlackoutEnv, EpisodeResult, list[ActionResult]]:
    env = BlackoutEnv(scenario)
    results: list[ActionResult] = []

    def keep(request: DecisionRequest, done: tuple[ActionResult, ...]) -> None:
        results.extend(done)

    return env, run_episode(env, RulesController(), on_decision=keep), results


def of_kind[T](results: list[ActionResult], kind: type[T]) -> list[T]:
    return [r.action for r in results if isinstance(r.action, kind)]  # type: ignore[misc]


# --- grid -----------------------------------------------------------------------------------


def test_sheds_residential_first_then_restores_after_the_repair() -> None:
    env, _, results = play(make_scenario(**grid(events=[fail("S1", 1)])))
    assert all(r.accepted for r in results), [r.summary() for r in results]
    assert [a.zone_id for a in of_kind(results, ShedLoad)] == ["ZA"]  # 13 MW on S2: ZA is enough
    assert [a.zone_id for a in of_kind(results, RestoreLoad)] == ["ZA"]
    assert [a.substation_id for a in of_kind(results, AssignCrew)] == ["S1"]
    m = env.engine.metrics
    assert (m.trips, m.repairs_completed, m.critical_downtime) == (0, 1, 0)


def test_sheds_essential_when_residential_is_not_enough() -> None:
    subs = [substation("S1", "A2", 10000), substation("S2", "C2", 6000)]
    _, _, results = play(make_scenario(**grid(substations=subs, events=[fail("S1", 1)])))
    shed = [a.zone_id for a in of_kind(results, ShedLoad)]
    assert shed == ["ZA", "ZB"]


def test_never_sheds_a_critical_zone() -> None:
    subs = [substation("S1", "A2", 10000), substation("S2", "C2", 4000)]
    env, _, results = play(make_scenario(**grid(substations=subs, events=[fail("S1", 1)])))
    assert "ZC" not in {a.zone_id for a in of_kind(results, ShedLoad)}
    assert env.engine.metrics.trips >= 1  # it can't be saved without shedding critical load


def test_crews_go_where_the_most_weighted_load_is() -> None:
    _, _, results = play(make_scenario(**grid(events=[fail("S1", 0), fail("S2", 0)])))
    # S2 carries critical ZC (5 MW x 100); S1 carries ZA and ZB (4 MW x 1 + 4 MW x 10).
    assert [a.substation_id for a in of_kind(results, AssignCrew)][:2] == ["S2", "S1"]


# --- fuel and roads -------------------------------------------------------------------------


def test_loads_at_the_depot_then_delivers_by_earliest_deadline() -> None:
    env, _, results = play(make_scenario())
    kinds = [(r.action.kind, getattr(r.action, "task", None)) for r in results[:2]]
    assert [k[1] for k in kinds] == ["load", "deliver"]
    m = env.engine.metrics
    assert m.fuel_delivered_l > 0
    assert m.critical_downtime == 0


LOADED = [{"id": "T1", "node": "A1", "load_l": 300}]


def last_road() -> str:
    """The last road of the loaded truck's fastest route from A1 to the hospital at C3."""
    env = BlackoutEnv(make_scenario(trucks=LOADED))
    env.reset()
    path = shortest_path(env.engine.city, "A1", "C3")
    assert path is not None
    return edge_id(path[-2], path[-1])


def loaded_truck_world(*events: dict[str, Any]) -> tuple[Scenario, str]:
    """A loaded truck at A1 and a hidden block on the last road of its fastest route to the
    hospital at C3 (found only on arrival there), plus ``events``."""
    last = last_road()
    block = {"kind": "road_block", "tick": 0, "edge": last}
    return make_scenario(trucks=LOADED, events=[block, *events]), last


def test_reroutes_when_a_report_says_the_route_is_blocked() -> None:
    last = last_road()
    scenario, _ = loaded_truck_world(report("R1", 1, {"type": "road_blocked", "subject": last}))
    env, _, results = play(scenario)
    reroutes = of_kind(results, Reroute)
    assert reroutes
    assert last in reroutes[0].avoid_edges
    assert env.engine.metrics.stuck_events == 0


def test_reroutes_a_stuck_truck() -> None:
    scenario, _ = loaded_truck_world()
    env, _, results = play(scenario)
    m = env.engine.metrics
    assert m.stuck_events == 1
    assert of_kind(results, Reroute)
    assert m.fuel_delivered_l > 0


def test_reads_l1_text_with_the_keyword_parser() -> None:
    scenario, last = loaded_truck_world()
    a, b = last.split("-")
    streets = {"A": "Alder", "B": "Birch", "C": "Cedar"}
    avenues = {"1": "1st", "2": "2nd", "3": "3rd"}
    if a[0] == b[0]:  # a street segment
        name = f"{streets[a[0]]} St between {avenues[a[1]]} and {avenues[b[1]]}"
    else:
        name = f"{avenues[a[1]]} Ave between {streets[a[0]]} and {streets[b[0]]}"
    text = f"Dispatch, Unit 4. {name} is blokced, a tree down across both lanes. Over."
    event = report(
        "R1", 1, {"type": "road_blocked", "subject": last}, channel="police_radio", text=text
    )
    l1 = Scenario.model_validate(
        {
            **scenario.model_dump(mode="json"),
            "level": 1,
            "events": [*scenario.model_dump(mode="json")["events"], event],
        }
    )
    controller = RulesController()
    env = BlackoutEnv(l1)
    run_episode(env, controller)
    assert last in controller.belief.blocked_roads()
    assert env.engine.metrics.stuck_events == 0


# --- registry and dev-v0 --------------------------------------------------------------------


def test_registry_builds_contestants_by_name() -> None:
    assert set(CONTESTANTS) == {"do-nothing", "rules"}
    assert isinstance(make_controller("rules"), RulesController)
    with pytest.raises(KeyError, match="available: do-nothing, rules"):
        make_controller("oracle")


def outcome(path: Path, controller: RulesController | DoNothingController) -> tuple[int, str]:
    env = BlackoutEnv(load_scenario(path))
    result = run_episode(env, controller)
    return env.engine.metrics.critical_downtime, result.run_hash


@pytest.fixture(scope="module")
def dev_v0_runs() -> dict[str, tuple[int, int, str]]:
    """Per dev-v0 scenario: do-nothing's downtime, Rules' downtime, Rules' run hash."""
    runs = {}
    for path in DEV_V0:
        nothing, _ = outcome(path, DoNothingController())
        rules, run_hash = outcome(path, RulesController())
        runs[path.stem] = (nothing, rules, run_hash)
    return runs


def test_rules_beats_do_nothing_on_90_percent_of_dev_v0_at_l0(
    dev_v0_runs: dict[str, tuple[int, int, str]],
) -> None:
    l0 = {name: run for name, run in dev_v0_runs.items() if name.endswith("_l0")}
    assert len(l0) == 9
    wins = [name for name, (nothing, rules, _) in l0.items() if rules < nothing]
    assert len(wins) * 10 >= 9 * len(l0), {n: r[:2] for n, r in l0.items()}


def test_rules_is_deterministic_over_dev_v0(dev_v0_runs: dict[str, tuple[int, int, str]]) -> None:
    controller = RulesController()  # one instance, reused: reset() must forget everything
    for path in DEV_V0:
        _, run_hash = outcome(path, controller)
        assert run_hash == dev_v0_runs[path.stem][2], path.stem
