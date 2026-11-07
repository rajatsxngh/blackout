"""The information layer: role filters, report routing, confirm_report, telemetry delay, and
wake triggers that come from each role's own observations."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from blackout.controllers.do_nothing import DoNothingController
from blackout.controllers.scripted import ScriptedController, load_script
from blackout.env import BlackoutEnv, DecisionRequest, Done, EpisodeResult, run_episode
from blackout.info.reports import Channel, Fact, FactType, close_enough
from blackout.info.roles import (
    CHANNEL_ROLE,
    FUEL,
    HOSPITALS,
    POWER,
    ROLE_ALL,
    ROLES,
    SCOPES,
    TEAM_ROLES,
    TRANSPORT,
    Section,
    receives,
)
from blackout.scenarios.schema import load_scenario
from blackout.sim.actions import ConfirmReport, DispatchTruck, RejectReason
from blackout.sim.config import SimConfig
from blackout.sim.entities import TaskKind
from blackout.sim.triggers import TriggerKind
from tests.helpers import HANDMADE_RUNS, grid, handmade_controller, make_scenario, report

FUEL_FACT = {"type": "hospital_fuel", "subject": "H1", "value": 100}


def advance(env: BlackoutEnv) -> DecisionRequest:
    request = env.advance()
    assert isinstance(request, DecisionRequest)
    return request


def kinds(request: DecisionRequest, role: str = ROLE_ALL) -> set[TriggerKind]:
    return {t.kind for t in request.observations[role].triggers}


# --- roles ------------------------------------------------------------------------------


def test_each_role_sees_only_its_sections() -> None:
    env = BlackoutEnv(make_scenario(**grid()), roles=ROLES)
    env.reset()
    sections = {
        Section.TRUCKS: lambda o: o.trucks,
        Section.CREWS: lambda o: o.crews,
        Section.HOSPITALS: lambda o: o.hospitals,
        Section.DEPOTS: lambda o: o.depots,
        Section.SUBSTATIONS: lambda o: o.substations,
        Section.ZONES: lambda o: o.zones,
    }
    for role in ROLES:
        observation = env.observe(role)
        for section, get in sections.items():
            assert bool(get(observation)) == (section in SCOPES[role]), (role, section)


def test_the_team_roles_together_see_what_the_single_agent_sees() -> None:
    union = frozenset().union(*(SCOPES[role] for role in TEAM_ROLES))
    assert union == SCOPES[ROLE_ALL] == frozenset(Section)
    assert TEAM_ROLES == (HOSPITALS, POWER, FUEL, TRANSPORT)


def test_known_blocks_go_to_roles_that_see_roads() -> None:
    env = BlackoutEnv(
        make_scenario(events=[{"kind": "road_block", "tick": 0, "edge": "A1-B1"}]),
        roles=(ROLE_ALL, TRANSPORT, FUEL),
    )
    env.reset()
    env.act(TRANSPORT, DispatchTruck(truck_id="T1", task=TaskKind.MOVE, target_id="C1"))
    stuck = advance(env)
    assert stuck.roles == (ROLE_ALL, TRANSPORT, FUEL)  # all three see the truck
    assert env.observe(TRANSPORT).known_blocks == ("A1-B1",)
    assert env.observe(FUEL).known_blocks == ()
    assert TriggerKind.ROAD_STATUS in kinds(stuck, TRANSPORT)
    assert TriggerKind.ROAD_STATUS not in kinds(stuck, FUEL)


def test_unknown_or_duplicate_roles_are_refused() -> None:
    with pytest.raises(ValueError, match="unknown role"):
        BlackoutEnv(make_scenario(), roles=("mayor",))
    with pytest.raises(ValueError, match="distinct"):
        BlackoutEnv(make_scenario(), roles=(POWER, POWER))
    with pytest.raises(ValueError, match="one role"):
        run_episode(BlackoutEnv(make_scenario(), roles=TEAM_ROLES), DoNothingController())


# --- reports ----------------------------------------------------------------------------


@pytest.mark.parametrize("channel", list(Channel))
def test_reports_are_routed_by_channel(channel: Channel) -> None:
    env = BlackoutEnv(
        make_scenario(horizon=30, events=[report("R1", 4, channel=channel.value)]), roles=ROLES
    )
    env.reset()
    request = advance(env)
    assert request.tick == 4
    expected = {role for role in ROLES if receives(role, channel)}
    assert set(request.roles) == expected
    target = CHANNEL_ROLE[channel]
    assert expected == (set(ROLES) if target is None else {ROLE_ALL, target})
    for role in request.roles:
        assert kinds(request, role) == {TriggerKind.REPORT}
        (seen,) = request.observations[role].reports
        assert (seen.id, seen.tick, seen.channel) == ("R1", 4, channel)


def test_clean_reports_show_facts_and_hide_their_truth() -> None:
    env = BlackoutEnv(
        make_scenario(
            events=[report("R1", 0, FUEL_FACT, truth={"is_true": False, "cue": "telemetry"})]
        )
    )
    request = env.reset()
    (seen,) = request.observations[ROLE_ALL].reports
    assert seen.facts == (Fact(type=FactType.HOSPITAL_FUEL, subject="H1", value=100),)
    assert seen.text is None
    assert "is_true" not in str(request.observations[ROLE_ALL].to_dict())


def test_noisy_levels_show_only_the_text() -> None:
    text = "st marys says they got about a hundred liters left"
    env = BlackoutEnv(make_scenario(level=1, events=[report("R1", 0, FUEL_FACT, text=text)]))
    (seen,) = env.reset().observations[ROLE_ALL].reports
    assert (seen.facts, seen.text) == (None, text)


def test_reports_are_news_only_once() -> None:
    env = BlackoutEnv(make_scenario(horizon=40, events=[report("R1", 0)]))
    assert len(env.reset().observations[ROLE_ALL].reports) == 1
    assert env.observe().reports  # still unread while the decision point is open
    later = advance(env)
    assert later.observations[ROLE_ALL].reports == ()


def test_report_schema_checks_subjects_ids_and_text() -> None:
    with pytest.raises(ValidationError, match="unknown hospital 'H9'"):
        make_scenario(events=[report("R1", 0, {**FUEL_FACT, "subject": "H9"})])
    with pytest.raises(ValidationError, match="duplicate report id"):
        make_scenario(events=[report("R1", 0), report("R1", 2)])
    with pytest.raises(ValidationError, match="needs text at level L2"):
        make_scenario(level=2, events=[report("R1", 0)])
    with pytest.raises(ValidationError, match="takes no value"):
        Fact(type=FactType.ROAD_BLOCKED, subject="A1-B1", value=3)
    with pytest.raises(ValidationError, match="needs a state"):
        Fact(type=FactType.SUBSTATION_STATUS, subject="S1", value="melted")
    with pytest.raises(ValidationError, match="whole number of L"):
        Fact(type=FactType.HOSPITAL_FUEL, subject="H1", value="lots")


def test_numeric_claims_hold_within_ten_percent() -> None:
    assert close_enough(110, 100)
    assert close_enough(90, 100)
    assert not close_enough(111, 100)
    assert close_enough(0, 0)
    assert not close_enough(1, 0)


# --- confirm_report ---------------------------------------------------------------------


def test_a_confirm_reply_arrives_eight_ticks_later_with_the_truth_then() -> None:
    claims = [
        {"type": "road_blocked", "subject": "B2-C2"},
        {"type": "road_cleared", "subject": "A1-B1"},
        FUEL_FACT,
        {"type": "burn_spike", "subject": "H1", "value": 5},
        {"type": "depot_stock", "subject": "D1", "value": 1000},
    ]
    env = BlackoutEnv(
        make_scenario(
            events=[
                report("R1", 0, *claims),
                {"kind": "road_block", "tick": 3, "edge": "B2-C2"},  # after the request
            ]
        )
    )
    env.reset()
    accepted = env.act(ROLE_ALL, ConfirmReport(report_id="R1"))
    assert accepted.accepted
    assert "t=8" in accepted.message
    assert env.observe().confirms_pending == ("R1",)
    assert env.observe().confirms_left == 3
    request = env.advance()
    while isinstance(request, DecisionRequest) and request.tick < 8:
        request = env.advance()
    assert isinstance(request, DecisionRequest)
    assert request.tick == 8
    assert TriggerKind.CONFIRM_REPLY in kinds(request)
    observation = request.observations[ROLE_ALL]
    (reply,) = observation.confirmations
    assert (reply.report_id, reply.requested_at, reply.tick) == ("R1", 0, 8)
    assert [(c.holds, c.actual) for c in reply.checks] == [
        (True, "blocked"),  # blocked at tick 3, so true by the time dispatch looks
        (True, "open"),
        (False, 84),  # 100 L at tick 0, burning 2 L/min
        (False, 2),
        (True, 1000),
    ]
    assert not reply.holds
    assert reply.summary() == "3 of 5 claims hold"
    assert observation.confirms_pending == ()


def test_substation_claims_are_checked_against_its_state() -> None:
    env = BlackoutEnv(
        make_scenario(
            **grid(
                events=[
                    {
                        "kind": "substation_fail",
                        "tick": 0,
                        "substation": "S1",
                        "repair_minutes": 40,
                    },
                    report(
                        "R1",
                        0,
                        {"type": "substation_status", "subject": "S1", "value": "failed"},
                        {"type": "repair_eta", "subject": "S1", "value": 60},
                        {"type": "repair_eta", "subject": "S2", "value": 0},
                    ),
                ]
            )
        )
    )
    env.reset()
    env.act(ROLE_ALL, ConfirmReport(report_id="R1"))
    while (request := env.advance()) and isinstance(request, DecisionRequest):
        if request.observations[ROLE_ALL].confirmations:
            break
    assert isinstance(request, DecisionRequest)
    (reply,) = request.observations[ROLE_ALL].confirmations
    # S1's zones fell back to S2, which tripped: resetting it takes 10 crew minutes.
    assert [(c.holds, c.actual) for c in reply.checks] == [
        (True, "failed"),
        (False, 40),
        (False, 10),
    ]


def test_confirmations_go_only_to_the_role_that_asked_and_are_limited_per_run() -> None:
    env = BlackoutEnv(
        make_scenario(events=[report("R1", 0)]),
        roles=TEAM_ROLES,
        config=SimConfig(confirm_limit=2),
    )
    env.reset()
    assert env.act(TRANSPORT, ConfirmReport(report_id="R1")).accepted
    assert env.act(POWER, ConfirmReport(report_id="R1")).accepted
    third = env.act(HOSPITALS, ConfirmReport(report_id="R1"))
    assert third.reason is RejectReason.CONFIRM_LIMIT
    request = env.advance()
    while isinstance(request, DecisionRequest) and request.tick < 8:
        request = env.advance()
    assert isinstance(request, DecisionRequest)
    assert set(request.roles) == {POWER, TRANSPORT}
    for role in request.roles:
        assert kinds(request, role) == {TriggerKind.CONFIRM_REPLY}
        assert request.observations[role].confirms_left == 0


# --- telemetry delay --------------------------------------------------------------------


def test_delayed_telemetry_shows_old_hospital_and_depot_readings() -> None:
    env = BlackoutEnv(
        make_scenario(horizon=40), config=SimConfig(telemetry_delay_ticks=3, heartbeat_ticks=5)
    )
    env.reset()
    env.act(ROLE_ALL, DispatchTruck(truck_id="T1", task=TaskKind.LOAD, target_id="D1"))
    request = advance(env)  # loading takes 5 minutes
    assert request.tick == 5
    observation = request.observations[ROLE_ALL]
    hospital, depot = observation.hospital("H1"), observation.depot("D1")
    assert (hospital.as_of, hospital.fuel_l) == (2, 96)
    assert (depot.as_of, depot.stock_l) == (2, 1000)  # the load at tick 5 isn't shown yet
    assert observation.truck("T1").load_l == 500  # fleet status is real-time
    later = advance(env)
    assert later.tick == 10
    assert later.observations[ROLE_ALL].depot("D1").stock_l == 500


def test_time_to_failure_triggers_follow_the_delayed_readings() -> None:
    def threshold_tick(delay: int) -> int:
        env = BlackoutEnv(make_scenario(horizon=60), config=SimConfig(telemetry_delay_ticks=delay))
        request: DecisionRequest | Done = env.reset()
        while isinstance(request, DecisionRequest):
            if TriggerKind.TTF_THRESHOLD in kinds(request):
                return request.tick
            request = env.advance()
        raise AssertionError("no threshold crossed")

    assert threshold_tick(0) == 20  # 100 L at 2 L/min: 30 minutes left at tick 20
    assert threshold_tick(4) == 24


def test_a_negative_delay_is_refused() -> None:
    with pytest.raises(ValueError, match="negative"):
        BlackoutEnv(make_scenario(), config=SimConfig(telemetry_delay_ticks=-1))


# --- wake triggers ----------------------------------------------------------------------


@pytest.mark.parametrize(("path", "contestant"), HANDMADE_RUNS)
def test_observed_triggers_match_the_truth_signals_on_the_handmade_runs(
    path: Path, contestant: str
) -> None:
    """With no delay and the full view, observation-driven wake-ups match the engine's own."""
    scenario = load_scenario(path)
    observed = run_episode(BlackoutEnv(scenario), handmade_controller(path, contestant))
    truth = run_episode(
        BlackoutEnv(scenario, wake_on_truth=True), handmade_controller(path, contestant)
    )
    assert observed.run_hash == truth.run_hash

    def plain(result: EpisodeResult) -> list[tuple[int, set[TriggerKind]]]:
        return [
            (d.tick, {t.kind for t in d.triggers} - {TriggerKind.ROAD_STATUS})
            for d in result.decisions
        ]

    assert plain(observed) == plain(truth)


def test_roles_are_woken_separately_with_their_own_heartbeat() -> None:
    env = BlackoutEnv(
        make_scenario(
            **grid(
                horizon=40,
                events=[{"kind": "burn_rate_change", "tick": 39, "hospital": "H1", "burn_lpm": 3}],
            )
        ),
        roles=(POWER, TRANSPORT),
        config=SimConfig(heartbeat_ticks=10),
    )
    env.reset()
    env.act(TRANSPORT, DispatchTruck(truck_id="T1", task=TaskKind.MOVE, target_id="B1"))
    arrived = advance(env)
    assert (arrived.tick, arrived.roles) == (2, (TRANSPORT,))
    beat = advance(env)
    assert (beat.tick, beat.roles) == (10, (POWER,))
    assert kinds(beat, POWER) == {TriggerKind.HEARTBEAT}
    assert advance(env).roles == (TRANSPORT,)  # 10 ticks after its own last wake-up, at 12


def test_scripted_demo_still_runs_with_beliefs() -> None:
    path = HANDMADE_RUNS[0][0]
    controller = ScriptedController(load_script(path.with_suffix(".script.json")))
    run_episode(BlackoutEnv(load_scenario(path)), controller)
    assert controller.belief.blocked_roads() == ("M3-M4",)
