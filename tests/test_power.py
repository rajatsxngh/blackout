"""The power grid: feeds, backup transfer, overloads, trips, cascades, shedding and repairs."""

from typing import Any

from blackout.controllers.do_nothing import DoNothingController
from blackout.controllers.scripted import ScriptedController, load_script
from blackout.env import BlackoutEnv, DecisionRequest, Done, EpisodeResult, run_episode
from blackout.scenarios.schema import load_scenario
from blackout.sim.actions import AssignCrew, Reroute, RestoreLoad, ShedLoad
from blackout.sim.entities import HospitalStatus, SubstationState, VehicleState
from blackout.sim.triggers import TriggerKind
from tests.helpers import CASCADE, grid, open_env, substation, zone

LATER = {"kind": "road_block", "tick": 59, "edge": "A1-B1"}  # a pending event: no early stop


def fail(substation_id: str, tick: int, repair: int = 30) -> dict[str, Any]:
    return {
        "kind": "substation_fail",
        "tick": tick,
        "substation": substation_id,
        "repair_minutes": repair,
    }


def next_decision(env: BlackoutEnv) -> DecisionRequest:
    request = env.advance()
    assert isinstance(request, DecisionRequest), request
    return request


def kinds(request: DecisionRequest) -> set[TriggerKind]:
    return {trigger.kind for trigger in request.triggers}


def test_zones_are_fed_by_their_primary_substation() -> None:
    obs = open_env(**grid(events=[LATER])).observe()
    assert {z.id: z.fed_by for z in obs.zones} == {"ZA": "S1", "ZB": "S1", "ZC": "S2"}
    assert {s.id: s.load_kw for s in obs.substations} == {"S1": 8000, "S2": 5000}
    hospital = obs.hospital("H1")
    assert (hospital.zone, hospital.powered, hospital.status, hospital.ttf_minutes) == (
        "ZC",
        True,
        HospitalStatus.ON_GRID,
        None,
    )


def test_a_failed_primary_hands_its_zones_to_the_backup() -> None:
    env = open_env(
        **grid(
            substations=[substation("S1", "A2", 20000), substation("S2", "C2", 10000)],
            events=[fail("S2", 0), LATER],
        )
    )
    obs = env.observe()
    assert obs.substation("S2").state is SubstationState.FAILED
    assert obs.zone("ZC").fed_by == "S1"
    assert obs.substation("S1").load_kw == 13000
    assert obs.hospital("H1").powered


def test_powered_hospitals_burn_no_fuel() -> None:
    env = open_env(**grid(events=[LATER]))
    assert next_decision(env).tick == 15  # heartbeat
    assert env.engine.hospitals["H1"].fuel_l == 100
    assert env.engine.metrics.burned_l == {"H1": 0}


def test_losing_power_puts_the_hospital_on_its_generator() -> None:
    no_backup = [
        zone("ZA", "A", 4000, "residential", "S1", "S2"),
        zone("ZB", "B", 4000, "essential", "S1", "S2"),
        zone("ZC", "C", 5000, "critical", "S2", None),
    ]
    env = open_env(**grid(zones=no_backup, events=[fail("S2", 3), LATER]))
    request = next_decision(env)
    assert request.tick == 3
    # Going from grid power to 50 minutes of fuel also falls through the 60-minute threshold.
    assert kinds(request) == {
        TriggerKind.SUBSTATION_STATUS,
        TriggerKind.ZONE_STATUS,
        TriggerKind.HOSPITAL_STATUS,
        TriggerKind.TTF_THRESHOLD,
    }
    assert request.observations["all"].hospital("H1").status is HospitalStatus.ON_GENERATOR
    assert next_decision(env).tick == 18  # heartbeat
    assert env.engine.hospitals["H1"].fuel_l == 100 - 2 * 15


def test_an_overload_trips_the_substation_after_three_minutes() -> None:
    env = open_env(**grid(events=[fail("S1", 2, repair=50)]))
    failed = next_decision(env)
    assert (failed.tick, kinds(failed)) == (2, {TriggerKind.SUBSTATION_STATUS})
    east = failed.observations["all"].substation("S2")
    assert (east.load_kw, east.overloaded_minutes) == (13000, 0)

    overload = next_decision(env)
    assert (overload.tick, kinds(overload)) == (3, {TriggerKind.OVERLOAD})
    assert overload.observations["all"].substation("S2").overloaded_minutes == 1

    tripped = next_decision(env)
    assert tripped.tick == 5  # overloaded during minutes 2, 3 and 4
    obs = tripped.observations["all"]
    assert obs.substation("S2").state is SubstationState.TRIPPED
    assert not any(z.powered for z in obs.zones)
    assert obs.hospital("H1").status is HospitalStatus.ON_GENERATOR
    assert env.engine.metrics.trips == 1


def test_shedding_in_time_prevents_the_trip() -> None:
    env = open_env(**grid(events=[fail("S1", 2, repair=50), LATER]))
    next_decision(env)  # tick 2: S1 fails
    next_decision(env)  # tick 3: S2 is overloaded
    shed = env.act("all", ShedLoad(zone_id="ZA"))
    assert shed.accepted
    assert shed.message == "ZA shed (4000 kW); S2 at 9000/10000 kW"
    quiet = next_decision(env)
    assert (quiet.tick, kinds(quiet)) == (18, {TriggerKind.HEARTBEAT})
    assert env.engine.substations["S2"].state is SubstationState.ONLINE
    assert env.engine.metrics.trips == 0


def test_shedding_and_restoring_take_effect_immediately() -> None:
    env = open_env(**grid(events=[LATER]))
    env.act("all", ShedLoad(zone_id="ZB"))
    obs = env.observe()
    assert obs.zone("ZB").shed
    assert not obs.zone("ZB").powered
    assert obs.substation("S1").load_kw == 4000
    restored = env.act("all", RestoreLoad(zone_id="ZB"))
    assert restored.message == "ZB restored, fed by S1 at 8000/10000 kW"
    assert env.observe().zone("ZB").powered


def test_restoring_a_zone_nothing_can_feed() -> None:
    both_down = [
        substation("S1", "A2", 10000, state="failed", repair_minutes=30),
        substation("S2", "C2", 10000, state="failed", repair_minutes=30),
    ]
    env = open_env(**grid(substations=both_down))
    env.act("all", ShedLoad(zone_id="ZA"))
    result = env.act("all", RestoreLoad(zone_id="ZA"))
    assert result.message == "ZA reconnected, but no substation can feed it right now"


def test_unmet_demand_is_counted_every_minute_with_priority_weights() -> None:
    env = open_env(**grid(horizon=10))
    env.act("all", ShedLoad(zone_id="ZB"))  # essential: weight 10
    done = env.advance()
    assert isinstance(done, Done)
    assert done.tick == 10
    assert done.metrics["unmet_kw_min"] == 4000 * 10
    assert done.metrics["unmet_weighted_kw_min"] == 4000 * 10 * 10
    assert done.metrics["zone_outage_minutes"] == {"ZA": 0, "ZB": 10, "ZC": 0}
    assert done.metrics["power_restored_at"] is None


def test_the_report_says_when_power_was_fully_restored() -> None:
    env = open_env(**grid())
    env.act("all", ShedLoad(zone_id="ZA"))
    assert next_decision(env).tick == 15  # heartbeat
    env.act("all", RestoreLoad(zone_id="ZA"))
    done = env.advance()
    assert isinstance(done, Done)
    assert (done.tick, done.reason) == (16, "stable")
    assert done.metrics["power_restored_at"] == 15


def test_a_crew_repairs_a_failed_substation_in_its_repair_time() -> None:
    env = open_env(
        **grid(
            substations=[substation("S1", "A2", 10000), substation("S2", "C2", 20000)],
            events=[fail("S1", 0, repair=30), LATER],
        )
    )
    assert env.observe().substation("S1").work_left is None  # nobody has assessed it yet
    sent = env.act("all", AssignCrew(crew_id="C1", substation_id="S1"))
    assert (sent.route, sent.eta_minutes) == (("B2", "A2"), 2)

    arrived = next_decision(env)
    assert arrived.tick == 2
    assert kinds(arrived) == {TriggerKind.VEHICLE_ARRIVED, TriggerKind.SUBSTATION_STATUS}
    west = arrived.observations["all"].substation("S1")
    assert (west.state, west.work_left, west.crew) == (SubstationState.REPAIRING, 30, "C1")

    while (request := next_decision(env)).tick < 32:
        pass
    assert request.tick == 32  # 30 minutes of work, starting the minute after arrival
    assert {TriggerKind.TASK_COMPLETED, TriggerKind.SUBSTATION_STATUS} <= kinds(request)
    obs = request.observations["all"]
    assert obs.substation("S1").state is SubstationState.ONLINE
    assert obs.zone("ZA").fed_by == "S1"
    assert obs.crew("C1").state is VehicleState.IDLE
    assert env.engine.metrics.repairs_completed == 1


def test_a_crew_resets_a_tripped_substation_in_ten_minutes() -> None:
    env = open_env(
        **grid(
            substations=[
                substation("S1", "A2", 20000),
                substation("S2", "C2", 10000, state="tripped"),
            ],
            events=[LATER],
        )
    )
    env.act("all", AssignCrew(crew_id="C1", substation_id="S2"))
    arrived = next_decision(env)
    assert arrived.tick == 2
    assert arrived.observations["all"].substation("S2").work_left == 10
    assert next_decision(env).tick == 12
    assert env.engine.substations["S2"].state is SubstationState.ONLINE


def test_crews_get_stuck_on_hidden_blocks_too() -> None:
    env = open_env(
        **grid(
            substations=[
                substation("S1", "A2", 10000, state="failed", repair_minutes=30),
                substation("S2", "C2", 20000),
            ],
            events=[{"kind": "road_block", "tick": 0, "edge": "A2-B2"}, LATER],
        )
    )
    env.act("all", AssignCrew(crew_id="C1", substation_id="S1"))
    stuck = next_decision(env)
    assert (stuck.tick, kinds(stuck)) == (1, {TriggerKind.VEHICLE_STUCK})
    detour = env.act("all", Reroute(vehicle_id="C1"))
    assert detour.accepted
    assert detour.route == ("B2", "B1", "A1", "A2")


def test_a_crew_sent_to_a_working_substation_just_waits_there() -> None:
    env = open_env(**grid(events=[LATER]))
    env.act("all", AssignCrew(crew_id="C1", substation_id="S2"))
    arrived = next_decision(env)
    assert (arrived.tick, kinds(arrived)) == (2, {TriggerKind.VEHICLE_ARRIVED})
    crew = arrived.observations["all"].crew("C1")
    assert (crew.at, crew.state, crew.substation) == ("C2", VehicleState.IDLE, None)


def test_a_second_failure_during_a_repair_loses_the_work() -> None:
    env = open_env(
        **grid(
            substations=[substation("S1", "A2", 10000), substation("S2", "C2", 20000)],
            events=[fail("S1", 0, repair=30), fail("S1", 10, repair=40), LATER],
        )
    )
    env.act("all", AssignCrew(crew_id="C1", substation_id="S1"))
    assert next_decision(env).tick == 2  # the crew starts the repair
    again = next_decision(env)
    assert again.tick == 10
    (status,) = [t for t in again.triggers if t.kind is TriggerKind.SUBSTATION_STATUS]
    assert status.detail == "failed; C1's work on it was lost"
    assert env.engine.crews["C1"].state is VehicleState.IDLE
    assert env.engine.substations["S1"].repair_minutes == 40


# --- the hand-made cascade scenario (M2's acceptance checks) ------------------------------


def run_cascade(controller: ScriptedController | DoNothingController) -> EpisodeResult:
    return run_episode(BlackoutEnv(load_scenario(CASCADE)), controller)


def first_tick(result: EpisodeResult, kind: TriggerKind, subject: str) -> int:
    return next(
        decision.tick
        for decision in result.decisions
        for trigger in decision.triggers
        if trigger.kind is kind and trigger.subject == subject
    )


def test_cascade_trips_east_then_west_when_nobody_acts() -> None:
    result = run_cascade(DoNothingController())
    assert first_tick(result, TriggerKind.OVERLOAD, "S2") == 11
    assert first_tick(result, TriggerKind.SUBSTATION_STATUS, "S2") == 13  # tripped in minute 12
    assert first_tick(result, TriggerKind.OVERLOAD, "S3") == 14
    assert first_tick(result, TriggerKind.SUBSTATION_STATUS, "S3") == 16  # tripped in minute 15
    metrics = result.done.metrics
    assert metrics["trips"] == 2
    assert metrics["dark_minutes"] == {"H1": 75, "H2": 125}


def test_cascade_is_prevented_by_shedding_in_time() -> None:
    result = run_cascade(ScriptedController(load_script(CASCADE.with_suffix(".script.json"))))
    metrics = result.done.metrics
    assert (result.done.tick, result.done.reason) == (104, "stable")
    assert metrics["trips"] == 0
    assert metrics["critical_downtime_min"] == 0
    # Only Z3 was ever without power: shed from minute 10 to minute 102.
    assert metrics["zone_outage_minutes"] == {f"Z{i}": 93 if i == 3 else 0 for i in range(1, 10)}
    assert metrics["unmet_kw_min"] == 93 * 6000
    assert metrics["power_restored_at"] == 103
