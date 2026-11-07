"""C2 Solver and C3 Reference (blackout.controllers.solver), and the solve_plan contract.

First the contract on small worlds (assignment, overrides, determinism, rendering), then the
power planner and the fuel model, then the contestants: the planning traps where greedy Rules
loses, the reference's ground truth, the source filter and confirm policy, logs and replay,
and the M7 done-when checks on a dev-v0 sample (identical runs, solve time, Solver >= Rules).
"""

import time
from itertools import pairwise
from pathlib import Path
from typing import Any

import pytest

import blackout.controllers.solver.controller as solver_controller
from blackout.controllers.rules import RulesController
from blackout.controllers.solver import (
    ReferenceController,
    SolverBeliefs,
    SolverController,
    SolverResult,
    solve_plan,
)
from blackout.controllers.solver.fuel import (
    BUCKET_STARTS,
    HORIZON_MINUTES,
    bucket_of,
    greedy_fuel,
    needy,
    solve_fuel,
    truck_options,
)
from blackout.controllers.solver.policy import Claim, SourceFilter, agree
from blackout.controllers.solver.power import best_shed, plan_crews, power_timeline, settle
from blackout.controllers.solver.state import (
    DEFAULT_REPAIR_MINUTES,
    PlanningState,
    planning_state,
)
from blackout.env import BlackoutEnv, env_for, run_episode
from blackout.info.belief import BeliefState
from blackout.info.observation import Observation
from blackout.info.reports import Channel, Fact, FactType, ReportView, SourceClass
from blackout.logs.reader import read_log
from blackout.logs.replay import replay
from blackout.logs.writer import record_episode, run_dir_for
from blackout.scenarios.schema import Scenario, load_scenario
from blackout.sim.actions import AssignCrew, ConfirmReport, DispatchTruck, ShedLoad
from blackout.sim.city import edge_id
from blackout.sim.config import SimConfig
from blackout.sim.entities import TaskKind
from tests.helpers import DEMO, HANDMADE, ROOT, grid, make_scenario, report, substation

TRAP_ASSIGNMENT = HANDMADE / "trap_assignment.json"
TRAP_DEPOT = HANDMADE / "trap_depot.json"
DEV_V0 = ROOT / "scenarios" / "dev-v0"
SAMPLE = [DEV_V0 / f"dev0_{t}_s0_l0.json" for t in ("t01", "t02", "t04")] + [
    DEV_V0 / "dev0_t01_s1_l1.json"
]


def start(scenario: Scenario | Path) -> tuple[BeliefState, Observation]:
    """The belief and observation at a scenario's first decision point."""
    loaded = load_scenario(scenario) if isinstance(scenario, Path) else scenario
    request = BlackoutEnv(loaded).reset()
    observation = request.observations["all"]
    belief = BeliefState()
    belief.observe(observation)
    return belief, observation


def state_of(scenario: Scenario | Path, overrides: SolverBeliefs | None = None) -> PlanningState:
    belief, _ = start(scenario)
    return planning_state(belief, overrides or SolverBeliefs(), SimConfig())


def deliveries(plan: SolverResult) -> dict[str, tuple[str, ...]]:
    return {t.truck_id: tuple(trip.hospital for trip in t.trips) for t in plan.trucks}


def fail(substation_id: str, tick: int = 0, repair: int = 30) -> dict[str, Any]:
    return {
        "kind": "substation_fail",
        "tick": tick,
        "substation": substation_id,
        "repair_minutes": repair,
    }


# --- the contract ---------------------------------------------------------------------------


def test_solve_plan_sends_each_truck_where_it_saves_a_hospital() -> None:
    belief, _ = start(TRAP_ASSIGNMENT)
    plan = solve_plan(belief)
    assert deliveries(plan) == {"T1": ("H2",), "T2": ("H1",)}
    assert plan.dark_minutes == {"H1": 0, "H2": 0}
    assert plan.stats.status == "optimal"
    assert [(a.truck_id, a.task, a.target_id) for a in plan.actions] == [  # type: ignore[union-attr]
        ("T1", TaskKind.DELIVER, "H2"),
        ("T2", TaskKind.DELIVER, "H1"),
    ]
    trip = plan.truck("T1").trips[0]
    assert (trip.depot, trip.deliver_eta, trip.route) == (None, 11, ("B1", "C1"))
    assert all(t.changed for t in plan.trucks)


def test_solve_plan_loads_where_the_truck_already_is() -> None:
    belief, _ = start(TRAP_DEPOT)
    plan = solve_plan(belief)
    (trip,) = plan.truck("T1").trips[:1]
    assert (trip.depot, trip.load_eta, trip.deliver_eta) == ("D2", 5, 16)
    (action,) = plan.actions
    assert isinstance(action, DispatchTruck)
    assert (action.task, action.target_id, action.amount_l) == (TaskKind.LOAD, "D2", None)


def test_solve_plan_is_deterministic_and_hints_keep_the_plan() -> None:
    belief, _ = start(DEV_V0 / "dev0_t01_s0_l0.json")
    first, again = solve_plan(belief), solve_plan(belief)
    assert first == again  # wall time is left out of the comparison
    assert first.plan_id == again.plan_id
    hinted = solve_plan(belief, previous=first)
    assert (hinted.plan_id, hinted.trucks) == (first.plan_id, first.trucks)


def test_solve_plan_needs_a_belief_that_has_observed() -> None:
    with pytest.raises(ValueError, match="no map"):
        solve_plan(BeliefState())


def test_render_fits_an_llm_tool_result() -> None:
    belief, _ = start(DEV_V0 / "dev0_t02_s0_l0.json")
    plan = solve_plan(belief)
    text = plan.render()
    assert text.startswith(f"plan {plan.plan_id} at t=0 (optimal)")
    assert all(t.truck_id in text for t in plan.trucks)
    assert len(text) <= 1600  # about 400 tokens


def test_unavailable_trucks_and_pins() -> None:
    belief, _ = start(TRAP_ASSIGNMENT)
    plan = solve_plan(belief, SolverBeliefs(unavailable_trucks=("T2",)))
    assert deliveries(plan)["T2"] == ()
    assert deliveries(plan)["T1"] == ("H1", "H2")  # H1 first: H2 is 8 minutes dark, not 12
    assert plan.dark_minutes["H2"] > 0
    pinned = solve_plan(belief, SolverBeliefs(pin={"T1": "H1"}))
    assert deliveries(pinned)["T1"][:1] == ("H1",)


def test_priority_override_decides_who_waits() -> None:
    belief, _ = start(TRAP_ASSIGNMENT)
    one_truck = SolverBeliefs(unavailable_trucks=("T2",), priority={"H1": 1, "H2": 1000})
    plan = solve_plan(belief, one_truck)
    assert deliveries(plan)["T1"] == ("H2", "H1")
    assert plan.dark_minutes["H2"] == 0


def test_fuel_and_burn_overrides() -> None:
    belief, _ = start(TRAP_ASSIGNMENT)
    full = solve_plan(belief, SolverBeliefs(fuel_l={"H1": 600, "H2": 600}))
    assert all(not t.trips for t in full.trucks)  # 300 minutes of fuel each: nothing to do
    assert full.actions == ()
    hungry = solve_plan(belief, SolverBeliefs(burn_lpm={"H1": 20}))
    assert hungry.dark_minutes["H1"] > 0  # 28 L at 20 L/min lasts a minute


def test_road_overrides_change_the_routes() -> None:
    belief, _ = start(TRAP_ASSIGNMENT)
    blocked = solve_plan(belief, SolverBeliefs(blocked_roads=("B1-C1",)))
    for t in blocked.trucks:
        for trip in t.trips:
            assert "B1-C1" not in {edge_id(a, b) for a, b in pairwise(trip.route)}
    for action in blocked.actions:
        assert isinstance(action, DispatchTruck)
        assert action.avoid_edges == ("B1-C1",)
    belief.apply_facts(
        [Fact(type=FactType.ROAD_BLOCKED, subject="B1-C1")],
        lambda f, r: True,
        report=_view("R9"),
    )
    assert "B1-C1" in planning_state(belief, SolverBeliefs(), SimConfig()).blocked
    cleared = solve_plan(belief, SolverBeliefs(cleared_roads=("B1-C1",)))
    assert deliveries(cleared) == {"T1": ("H2",), "T2": ("H1",)}


def _view(report_id: str, source_class: str = "field", source: str = "Officer A") -> ReportView:
    return ReportView(
        id=report_id,
        tick=0,
        channel=Channel.POLICE_RADIO,
        source_class=SourceClass(source_class),
        source=source,
        facts=None,
        text=None,
    )


# --- power ----------------------------------------------------------------------------------


def test_shedding_keeps_feeders_under_95_percent_and_spares_essential_zones() -> None:
    state = state_of(make_scenario(**grid(events=[fail("S1")])))
    timeline = power_timeline(state, BUCKET_STARTS)
    assert timeline.shed_now == {"ZA"}  # S2 would carry 13 MW of 10: residential ZA goes
    assert timeline.grid_now.feeds == {"ZA": None, "ZB": "S2", "ZC": "S2"}
    belief, _ = start(make_scenario(**grid(events=[fail("S1")])))
    plan = solve_plan(belief)
    assert [a.zone_id for a in plan.actions if isinstance(a, ShedLoad)] == ["ZA"]
    assert plan.power.shed == ("ZA",)


def test_the_95_percent_limit_sheds_before_the_feeder_is_overloaded() -> None:
    subs = [substation("S1", "A2", 10000), substation("S2", "C2", 9200)]
    state = state_of(make_scenario(**grid(substations=subs, events=[fail("S1")])))
    online = frozenset({"S2"})
    assert best_shed(state, online, frozenset()) is not None
    # ZB + ZC = 9 MW fits under 100% of 9.2 MW but not under 95% (8.74 MW): ZB must go too.
    assert best_shed(state, online, frozenset()).shed == {"ZA", "ZB"}  # type: ignore[union-attr]
    assert best_shed(state, online, frozenset(), 100).shed == {"ZA"}  # type: ignore[union-attr]


def test_critical_zones_are_never_shed_and_hopeless_feeders_trip() -> None:
    subs = [substation("S1", "A2", 10000), substation("S2", "C2", 4000)]
    state = state_of(make_scenario(**grid(substations=subs, events=[fail("S1")])))
    grid_now = settle(state, frozenset({"S2"}), frozenset())
    assert grid_now.tripped == {"S2"}  # ZC alone (5 MW, critical) overloads S2
    assert "ZC" not in grid_now.shed
    assert not grid_now.powered("ZC")


def test_crews_go_to_the_most_weighted_substation_first() -> None:
    spec = grid(events=[fail("S1"), fail("S2")])
    state = state_of(make_scenario(**spec))
    crews, online_at = plan_crews(state)
    (order,) = crews
    assert order.substations == ("S2", "S1")  # S2 feeds critical ZC
    # Repair times are hidden until a crew starts, so the longest PRD repair is assumed.
    first = 2 + DEFAULT_REPAIR_MINUTES
    assert online_at == {"S1": first + 4 + DEFAULT_REPAIR_MINUTES, "S2": first}
    belief, _ = start(make_scenario(**spec))
    (action,) = [a for a in solve_plan(belief).actions if isinstance(a, AssignCrew)]
    assert (action.crew_id, action.substation_id) == ("C1", "S2")


def test_a_reported_repair_time_is_used_and_powers_the_hospital_back() -> None:
    eta = {"type": "repair_eta", "subject": "S2", "value": 20}
    spec = grid(events=[fail("S1"), fail("S2"), report("R1", 0, eta, channel="crew_radio")])
    state = state_of(make_scenario(**spec))
    assert state.repair_minutes == {"S1": DEFAULT_REPAIR_MINUTES, "S2": 20}
    timeline = power_timeline(state, BUCKET_STARTS)
    assert timeline.online_at["S2"] == 22
    powered = timeline.powered["H1"]
    assert not powered[bucket_of(22) - 1]
    assert powered[bucket_of(22)]


# --- the fuel model -------------------------------------------------------------------------


def test_every_truck_has_at_most_91_options_idle_first() -> None:
    raw = load_scenario(DEV_V0 / "dev0_t01_s0_l0.json").model_dump()
    for s in raw["substations"]:
        s["state"], s["repair_minutes"] = "failed", 200
    scenario = Scenario.model_validate(
        {k: v for k, v in raw.items() if k not in ("content_hash", "validity", "split")}
    )
    state = state_of(scenario)
    timeline = power_timeline(state, BUCKET_STARTS)
    assert needy(state, timeline.powered) == {"H1", "H2", "H3"}
    for truck in state.trucks:
        options = truck_options(state, truck)
        assert 1 < len(options) <= 91
        assert options[0].legs == ()
        assert all(leg.delivered_at <= HORIZON_MINUTES for o in options for leg in o.legs)


def test_a_broken_or_unavailable_truck_can_only_idle() -> None:
    state = state_of(TRAP_ASSIGNMENT, SolverBeliefs(unavailable_trucks=("T1",)))
    t1 = next(t for t in state.trucks if t.id == "T1")
    assert [o.legs for o in truck_options(state, t1)] == [()]


def test_greedy_fallback_serves_the_earliest_deadline_first() -> None:
    state = state_of(TRAP_ASSIGNMENT)
    timeline = power_timeline(state, BUCKET_STARTS)
    plan = greedy_fuel(state, timeline.powered)
    assert plan.status == "greedy"
    assert {v: tuple(t.leg.hospital for t in trips) for v, trips in plan.trips.items()} == {
        "T1": ("H1",),  # the trap greedy dispatch falls into
        "T2": ("H2",),
    }


def test_the_model_is_used_when_it_solves_and_greedy_when_it_cannot() -> None:
    state = state_of(TRAP_ASSIGNMENT)
    timeline = power_timeline(state, BUCKET_STARTS)
    assert solve_fuel(state, timeline.powered).status == "optimal"
    assert solve_fuel(state, timeline.powered, deterministic_time=0.0).status == "greedy"


# --- the contestants ------------------------------------------------------------------------


def downtime(scenario: Path, controller: Any) -> tuple[int, int]:
    loaded = load_scenario(scenario)
    env = env_for(loaded, controller)
    run_episode(env, controller)
    metrics = env.engine.metrics
    return metrics.critical_downtime, metrics.stuck_events


@pytest.mark.parametrize(("trap", "rules_loses"), [(TRAP_ASSIGNMENT, 7), (TRAP_DEPOT, 3)])
def test_planning_beats_greedy_rules_on_the_traps(trap: Path, rules_loses: int) -> None:
    assert downtime(trap, RulesController())[0] == rules_loses
    assert downtime(trap, SolverController())[0] == 0
    assert downtime(trap, ReferenceController())[0] == 0


def test_the_reference_knows_hidden_blocks_and_the_solver_does_not() -> None:
    solver_dark, solver_stuck = downtime(DEMO, SolverController())
    reference_dark, reference_stuck = downtime(DEMO, ReferenceController())
    assert solver_stuck >= 1  # nobody reported the block: it finds it the hard way
    assert reference_stuck == 0
    assert reference_dark <= solver_dark


def test_the_reference_must_be_woken_by_truth_and_attached() -> None:
    scenario = load_scenario(TRAP_DEPOT)
    with pytest.raises(ValueError, match="wake_on_truth"):
        run_episode(BlackoutEnv(scenario), ReferenceController())
    _, observation = start(TRAP_DEPOT)
    with pytest.raises(RuntimeError, match="attach"):
        ReferenceController().decide(observation)
    assert env_for(scenario, ReferenceController()).wake_on_truth
    assert not env_for(scenario, SolverController()).wake_on_truth


def route_edges(scenario: Scenario) -> list[str]:
    belief, _ = start(scenario)
    trip = solve_plan(belief).truck("T1").trips[0]
    return [edge_id(a, b) for a, b in pairwise(trip.route)]


def test_the_filter_parks_an_unverified_claim_until_a_second_source_backs_it() -> None:
    claim = Fact(type=FactType.ROAD_BLOCKED, subject="B1-C1")
    belief, _ = start(TRAP_ASSIGNMENT)
    source_filter = SourceFilter()
    source_filter.take(belief, [claim], _view("R1", "unverified", "Resident A"))
    assert belief.blocked_roads() == ()
    assert len(source_filter.parked) == 1
    source_filter.take(belief, [claim], _view("R2", "unverified", "Resident A"))
    assert belief.blocked_roads() == ()  # the same person twice isn't corroboration
    source_filter.take(belief, [claim], _view("R3", "unverified", "Resident B"))
    source_filter.recheck(belief, [])
    assert belief.blocked_roads() == ("B1-C1",)
    assert source_filter.parked == []
    field = SourceFilter()
    field.take(belief, [Fact(type=FactType.ROAD_BLOCKED, subject="A1-B1")], _view("R4"))
    assert "A1-B1" in belief.blocked_roads()


def test_telemetry_corroborates_and_numbers_agree_within_10_percent() -> None:
    belief, _ = start(TRAP_ASSIGNMENT)
    fuel = Fact(type=FactType.HOSPITAL_FUEL, subject="H1", value=30)
    source_filter = SourceFilter()
    source_filter.take(belief, [fuel], _view("R1", "unverified"))
    assert source_filter.parked == []  # telemetry says 28 L
    assert agree(fuel, Fact(type=FactType.HOSPITAL_FUEL, subject="H1", value=28))
    assert not agree(fuel, Fact(type=FactType.HOSPITAL_FUEL, subject="H1", value=20))
    assert not agree(fuel, Fact(type=FactType.HOSPITAL_FUEL, subject="H2", value=30))
    assert source_filter.heard == [Claim(fuel, _view("R1", "unverified"))]


def test_the_solver_confirms_an_unverified_report_that_would_change_its_plan() -> None:
    on_route = route_edges(make_scenario())[0]
    rumor = report(
        "R1", 0, {"type": "road_blocked", "subject": on_route}, source_class="unverified"
    )
    scenario = make_scenario(events=[rumor])
    controller = SolverController()
    env = BlackoutEnv(scenario)
    request = env.reset()
    actions = controller.decide(request.observations["all"])
    asks = [a for a in actions if isinstance(a, ConfirmReport)]
    assert [a.report_id for a in asks] == ["R1"]
    assert on_route not in controller.belief.blocked_roads()  # not believed yet
    results = [env.act("all", a) for a in actions]
    assert all(r.accepted for r in results), [r.summary() for r in results]
    # The reply (8 ticks later) is the truth: the road is open, and the claim is dropped.
    while True:
        request = env.advance()
        assert not hasattr(request, "reason"), "the run ended before the reply came"
        observation = request.observations["all"]  # type: ignore[union-attr]
        for action in controller.decide(observation):
            env.act("all", action)
        if observation.confirmations:
            break
    assert controller._filter.parked == []
    assert on_route not in controller.belief.blocked_roads()
    assert not [a for a in controller.decide(env.observe()) if isinstance(a, ConfirmReport)]


def test_unverified_reports_that_change_nothing_are_not_confirmed() -> None:
    far = report("R1", 0, {"type": "road_blocked", "subject": "C1-C2"}, source_class="unverified")
    scenario = make_scenario(events=[far])
    assert "C1-C2" not in route_edges(make_scenario())
    actions = SolverController().decide(BlackoutEnv(scenario).reset().observations["all"])
    assert not [a for a in actions if isinstance(a, ConfirmReport)]


def test_solver_and_reference_runs_log_and_replay(tmp_path: Path) -> None:
    scenario = load_scenario(TRAP_ASSIGNMENT)
    for controller in (SolverController(), ReferenceController()):
        run_dir = run_dir_for(tmp_path, scenario.id, controller.name)
        recorded = record_episode(env_for(scenario, controller), controller, run_dir)
        stats = recorded.summary["contestant_stats"]
        assert isinstance(stats, dict)
        assert stats["solves"] == len(recorded.episode.decisions)
        log = read_log(recorded.log_path)
        assert log.header.engine.wake_on_truth is (controller.name == "reference")
        assert replay(log).episode.run_hash == recorded.episode.run_hash


# --- M7 done-when on a dev-v0 sample --------------------------------------------------------


@pytest.fixture(scope="module")
def sample_runs() -> dict[str, Any]:
    """Solver and Rules runs over the sample, timing every solve_plan call."""
    times: list[float] = []
    original = solver_controller.solve_plan

    def timed(*args: Any, **kwargs: Any) -> SolverResult:
        began = time.perf_counter()
        result = original(*args, **kwargs)
        times.append(time.perf_counter() - began)
        return result

    solver_controller.solve_plan = timed  # type: ignore[assignment]
    try:
        runs = {}
        for path in SAMPLE:
            env = BlackoutEnv(load_scenario(path))
            result = run_episode(env, SolverController())
            rules = BlackoutEnv(load_scenario(path))
            run_episode(rules, RulesController())
            runs[path.stem] = (
                result.run_hash,
                env.engine.metrics.critical_downtime,
                rules.engine.metrics.critical_downtime,
            )
    finally:
        solver_controller.solve_plan = original  # type: ignore[assignment]
    return {"runs": runs, "times": sorted(times)}


def test_solver_runs_are_identical(sample_runs: dict[str, Any]) -> None:
    controller = SolverController()  # one instance, reused: reset() must forget everything
    for path in SAMPLE:
        result = run_episode(BlackoutEnv(load_scenario(path)), controller)
        assert result.run_hash == sample_runs["runs"][path.stem][0], path.stem


def test_solver_is_at_least_as_good_as_rules(sample_runs: dict[str, Any]) -> None:
    for name, (_, solver, rules) in sample_runs["runs"].items():
        assert solver <= rules, name


def test_solve_time_p95_is_under_a_second(sample_runs: dict[str, Any]) -> None:
    times = sample_runs["times"]
    p95 = times[max(0, -(-95 * len(times) // 100) - 1)]
    assert len(times) > 50
    assert p95 < 1.0, f"p95 {p95:.3f} s over {len(times)} solves"


# --- rendering, telemetry corroboration, crews in a run ---------------------------------------


def test_render_lists_trips_shedding_crews_and_actions() -> None:
    belief, _ = start(TRAP_DEPOT)
    text = solve_plan(belief).render()
    assert "T1: load D2 t5 > H1 500L t16" in text
    assert "actions: T1 load D2" in text
    belief, _ = start(make_scenario(**grid(events=[fail("S1"), fail("S2")])))
    plan = solve_plan(belief)
    text = plan.render()
    assert "C1: S2 > S1" in text
    assert f"S1 t{plan.power.restore_eta['S1']}" in text
    assert "C1 to S2" in text
    belief, _ = start(make_scenario(**grid(events=[fail("S1")])))
    text = solve_plan(belief).render()
    assert "shed: ZA" in text
    assert "shed ZA" in text


@pytest.mark.parametrize(
    ("fact", "agrees"),
    [
        (Fact(type=FactType.SUBSTATION_STATUS, subject="S1", value="failed"), True),
        (Fact(type=FactType.SUBSTATION_STATUS, subject="S1", value="online"), False),
        (Fact(type=FactType.BURN_SPIKE, subject="H1", value=2), True),
        (Fact(type=FactType.BURN_SPIKE, subject="H1", value=4), False),
        (Fact(type=FactType.DEPOT_STOCK, subject="D1", value=990), True),
        (Fact(type=FactType.DEPOT_STOCK, subject="D1", value=500), False),
        (Fact(type=FactType.REPAIR_ETA, subject="S1", value=30), False),  # telemetry can't say
        (Fact(type=FactType.ROAD_BLOCKED, subject="A1-B1"), False),
    ],
)
def test_telemetry_backs_an_unverified_claim_only_when_it_says_the_same(
    fact: Fact, agrees: bool
) -> None:
    belief, _ = start(make_scenario(**grid(events=[fail("S1")])))
    source_filter = SourceFilter()
    source_filter.take(belief, [fact], _view("R1", "unverified"))
    assert (source_filter.parked == []) is agrees


def test_crews_drive_to_the_plan_and_are_left_alone_on_the_way() -> None:
    spec = grid(events=[fail("S1", repair=20), fail("S2", tick=3, repair=20)])
    env = BlackoutEnv(make_scenario(**spec))
    assigned: list[tuple[int, str]] = []

    def keep(request: Any, results: tuple[Any, ...]) -> None:
        for r in results:
            if isinstance(r.action, AssignCrew):
                assigned.append((request.tick, r.action.substation_id))

    run_episode(env, SolverController(), on_decision=keep)
    assert assigned[0] == (0, "S1")
    assert env.engine.metrics.repairs_completed == 2
    assert env.engine.metrics.critical_downtime == 0
