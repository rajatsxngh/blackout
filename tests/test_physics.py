"""Engine physics: timing, clamping, blocks, breakdowns, fuel burn and when runs end."""

from blackout.env import BlackoutEnv, DecisionRequest, Done
from blackout.sim.actions import DispatchTruck
from blackout.sim.entities import HospitalStatus, TaskKind, VehicleState
from blackout.sim.triggers import TriggerKind
from tests.helpers import grid, make_scenario, open_env


def dispatch(task: TaskKind, target: str, truck: str = "T1", **kwargs: object) -> DispatchTruck:
    return DispatchTruck(truck_id=truck, task=task, target_id=target, **kwargs)  # type: ignore[arg-type]


def next_decision(env: BlackoutEnv) -> DecisionRequest:
    request = env.advance()
    assert isinstance(request, DecisionRequest), request
    return request


def kinds(request: DecisionRequest) -> set[TriggerKind]:
    return {trigger.kind for trigger in request.triggers}


def test_loading_takes_five_minutes_and_fills_the_truck() -> None:
    env = open_env()
    assert env.act("all", dispatch(TaskKind.LOAD, "D1")).accepted
    request = next_decision(env)
    assert request.tick == 5
    assert TriggerKind.TASK_COMPLETED in kinds(request)
    assert env.engine.trucks["T1"].load_l == 500
    assert env.engine.depots["D1"].stock_l == 500


def test_loading_is_limited_by_stock_and_requested_amount() -> None:
    env = open_env(depots=[{"id": "D1", "name": "Low", "node": "A1", "stock_l": 120}])
    env.act("all", dispatch(TaskKind.LOAD, "D1"))
    next_decision(env)
    assert env.engine.trucks["T1"].load_l == 120

    env = open_env()
    env.act("all", dispatch(TaskKind.LOAD, "D1", amount_l=200))
    next_decision(env)
    assert env.engine.trucks["T1"].load_l == 200


def test_delivery_is_limited_by_tank_room() -> None:
    env = open_env(
        trucks=[{"id": "T1", "node": "C3", "load_l": 500}],
        hospitals=[
            {"id": "H1", "name": "X", "node": "C3", "tank_l": 600, "fuel_l": 550, "burn_lpm": 2}
        ],
    )
    # 50 L of room at the start, plus the 10 L burned during the five minutes of unloading.
    env.act("all", dispatch(TaskKind.DELIVER, "H1"))
    request = next_decision(env)
    assert request.tick == 5
    assert env.engine.hospitals["H1"].fuel_l == 600
    assert env.engine.trucks["T1"].load_l == 440


def test_driving_a_road_takes_its_travel_time() -> None:
    env = open_env()
    env.act("all", dispatch(TaskKind.MOVE, "B1"))
    request = next_decision(env)
    assert request.tick == 2
    assert TriggerKind.VEHICLE_ARRIVED in kinds(request)
    truck = env.engine.trucks["T1"]
    assert (truck.at, truck.state) == ("B1", VehicleState.IDLE)


def test_hidden_block_stops_the_truck_and_becomes_known() -> None:
    env = open_env(events=[{"kind": "road_block", "tick": 0, "edge": "A1-B1"}])
    assert env.observe().known_blocks == ()
    assert env.act("all", dispatch(TaskKind.MOVE, "B1")).accepted
    request = next_decision(env)
    assert request.tick == 1
    assert TriggerKind.VEHICLE_STUCK in kinds(request)
    assert env.engine.trucks["T1"].state is VehicleState.STUCK
    assert request.observations["all"].known_blocks == ("A1-B1",)


def test_block_ahead_does_not_stop_a_truck_already_on_that_road() -> None:
    env = open_env(
        city={"width": 3, "height": 3, "default_travel_minutes": 2, "travel_minutes": {"A1-B1": 3}},
        events=[{"kind": "road_block", "tick": 1, "edge": "A1-B1"}],
    )
    env.act("all", dispatch(TaskKind.MOVE, "B1"))
    request = next_decision(env)
    assert request.tick == 3
    assert TriggerKind.VEHICLE_ARRIVED in kinds(request)
    assert env.engine.metrics.stuck_events == 0


def test_breakdown_freezes_the_truck_until_it_is_repaired() -> None:
    env = open_env(
        events=[{"kind": "truck_breakdown", "tick": 1, "truck": "T1", "repair_minutes": 4}]
    )
    env.act("all", dispatch(TaskKind.MOVE, "C1"))
    broken = next_decision(env)
    assert (broken.tick, kinds(broken)) == (1, {TriggerKind.VEHICLE_BROKEN})
    truck = env.engine.trucks["T1"]
    assert (truck.state, truck.at, truck.next_node, truck.elapsed) == (
        VehicleState.BROKEN,
        "A1",
        "B1",
        1,
    )
    repaired = next_decision(env)
    assert (repaired.tick, kinds(repaired)) == (5, {TriggerKind.VEHICLE_REPAIRED})
    assert truck.elapsed == 1  # it didn't move while broken
    arrived = next_decision(env)
    assert arrived.tick == 8  # 1 more minute to B1, then 2 to C1
    assert truck.at == "C1"


def test_generator_burns_down_then_the_hospital_goes_dark() -> None:
    env = BlackoutEnv(
        make_scenario(
            horizon=10,
            hospitals=[
                {"id": "H1", "name": "X", "node": "C3", "tank_l": 600, "fuel_l": 5, "burn_lpm": 2}
            ],
        )
    )
    env.reset()
    request = next_decision(env)
    assert request.tick == 3
    assert TriggerKind.HOSPITAL_STATUS in kinds(request)
    assert env.engine.hospitals["H1"].status is HospitalStatus.DARK
    done = env.advance()
    assert isinstance(done, Done)
    assert done.metrics["dark_minutes"] == {"H1": 7}  # minutes 3 through 9
    assert env.engine.metrics.fuel_burned_l == 5


def test_burn_rate_change_shows_up_as_a_falling_time_to_failure() -> None:
    env = open_env(
        events=[{"kind": "burn_rate_change", "tick": 3, "hospital": "H1", "burn_lpm": 5}]
    )
    request = next_decision(env)
    # 94 L left after 3 minutes at 2 L/min; at 5 L/min that's 18 minutes, through the 30 mark.
    assert request.tick == 3
    assert [str(t) for t in request.triggers] == ["ttf_threshold(H1)"]
    hospital = request.observations["all"].hospital("H1")
    assert (hospital.fuel_l, hospital.burn_lpm, hospital.ttf_minutes) == (94, 5, 18)


def test_falling_through_a_fuel_threshold_wakes_the_contestant() -> None:
    env = open_env(
        hospitals=[
            {"id": "H1", "name": "X", "node": "C3", "tank_l": 600, "fuel_l": 70, "burn_lpm": 2}
        ]
    )
    request = next_decision(env)
    assert request.tick == 5  # 60 L left at 2 L/min is 30 minutes
    (trigger,) = request.triggers
    assert (trigger.kind, trigger.subject) == (TriggerKind.TTF_THRESHOLD, "H1")
    assert trigger.detail == "30 min of fuel left (crossed 30)"


def test_heartbeat_every_15_minutes_when_nothing_else_happens() -> None:
    env = open_env(
        hospitals=[
            {"id": "H1", "name": "X", "node": "C3", "tank_l": 600, "fuel_l": 600, "burn_lpm": 1}
        ]
    )
    ticks = []
    while isinstance(request := env.advance(), DecisionRequest):
        assert kinds(request) == {TriggerKind.HEARTBEAT}
        ticks.append(request.tick)
    assert ticks == [15, 30, 45]


def test_run_ends_early_once_nothing_more_can_happen() -> None:
    env = open_env(**grid())  # every zone and the hospital have power, and no events are due
    done = env.advance()
    assert isinstance(done, Done)
    assert (done.tick, done.reason) == (1, "stable")
    assert done.metrics["power_restored_at"] == 0


def test_run_ends_at_the_horizon() -> None:
    env = open_env(horizon=10)
    while isinstance(request := env.advance(), DecisionRequest):
        pass
    assert isinstance(request, Done)
    assert (request.tick, request.reason) == (10, "horizon")
    assert len(env.tick_hashes) == 11  # ticks 0 through 10
