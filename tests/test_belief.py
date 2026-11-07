"""BeliefState: telemetry, trusted facts and confirm replies, each with its provenance."""

from blackout.env import BlackoutEnv
from blackout.info.belief import (
    BLOCKED,
    OPEN,
    BeliefState,
    Estimate,
    trust_all,
    trust_at_least,
)
from blackout.info.reports import (
    Channel,
    ClaimCheck,
    ConfirmReply,
    Fact,
    FactType,
    ReportView,
    SourceClass,
)
from blackout.sim.actions import DispatchTruck
from blackout.sim.config import SimConfig
from blackout.sim.entities import TaskKind
from blackout.sim.hashing import canonical_json
from tests.helpers import grid, make_scenario, open_env, report


def view(
    report_id: str, tick: int, *facts: Fact, source_class: SourceClass = SourceClass.FIELD
) -> ReportView:
    return ReportView(report_id, tick, Channel.PUBLIC, source_class, "someone", facts, None)


def fact(kind: FactType, subject: str, value: int | str | None = None) -> Fact:
    return Fact(type=kind, subject=subject, value=value)


def believe_now(env: BlackoutEnv) -> BeliefState:
    belief = BeliefState()
    belief.observe(env.observe())
    return belief


def test_telemetry_fills_the_belief_with_certain_estimates() -> None:
    belief = believe_now(open_env(**grid()))
    assert belief.hospitals["H1"].fuel_l == Estimate(100, 0, "telemetry", 100)
    assert belief.depots["D1"].stock_l == Estimate(1000, 0, "telemetry", 100)
    assert belief.substations["S1"].state == Estimate("online", 0, "telemetry", 100)
    assert belief.substations["S1"].eta_minutes == Estimate(0, 0, "telemetry", 100)
    assert set(belief.trucks) == {"T1"}
    assert set(belief.crews) == {"C1"}
    assert belief.powered("H1")
    assert belief.blocked_roads() == ()
    assert belief.confirms_left == 4


def test_discovered_blocks_are_believed_from_when_they_were_seen() -> None:
    env = open_env(events=[{"kind": "road_block", "tick": 0, "edge": "A1-B1"}])
    belief = BeliefState()
    env.act("all", DispatchTruck(truck_id="T1", task=TaskKind.MOVE, target_id="C1"))
    env.advance()
    belief.observe(env.observe())
    env.advance()
    belief.observe(env.observe())
    assert belief.roads["A1-B1"] == Estimate(BLOCKED, 1, "telemetry", 100)
    # A later report that it cleared wins over the old discovery.
    belief.apply_facts([fact(FactType.ROAD_CLEARED, "A1-B1")], trust_all, report=view("R1", 5))
    assert belief.roads["A1-B1"] == Estimate(OPEN, 5, "R1", 85)
    belief.observe(env.observe())  # telemetry still lists the block, but it's old news
    assert belief.blocked_roads() == ()


def test_a_trust_policy_decides_which_facts_go_in() -> None:
    belief = BeliefState()
    rumor = view("R1", 2, fact(FactType.ROAD_BLOCKED, "B1-C1"), source_class=SourceClass.UNVERIFIED)
    radio = view("R2", 2, fact(FactType.ROAD_BLOCKED, "A1-A2"))
    strict = trust_at_least(85)
    assert belief.apply_facts(rumor.facts or (), strict, report=rumor) == []
    assert len(belief.apply_facts(radio.facts or (), strict, report=radio)) == 1
    assert belief.blocked_roads() == ("A1-A2",)
    belief.apply_facts(rumor.facts or (), trust_all, report=rumor)
    assert belief.roads["B1-C1"] == Estimate(BLOCKED, 2, "R1", 50)


def test_newer_information_wins_and_ties_go_to_the_more_reliable_source() -> None:
    belief = BeliefState()
    staff = view("R1", 4, fact(FactType.HOSPITAL_FUEL, "H1", 200))
    rumor = view(
        "R2", 4, fact(FactType.HOSPITAL_FUEL, "H1", 50), source_class=SourceClass.UNVERIFIED
    )
    older = view("R3", 3, fact(FactType.HOSPITAL_FUEL, "H1", 10), source_class=SourceClass.VERIFIED)
    for item in (staff, rumor, older):
        belief.apply_facts(item.facts or (), trust_all, report=item)
    assert belief.hospitals["H1"].fuel_l == Estimate(200, 4, "R1", 85)


def test_every_fact_type_lands_in_the_belief() -> None:
    belief = BeliefState()
    item = view(
        "R1",
        6,
        fact(FactType.SUBSTATION_STATUS, "S1", "failed"),
        fact(FactType.REPAIR_ETA, "S1", 45),
        fact(FactType.BURN_SPIKE, "H1", 4),
        fact(FactType.DEPOT_STOCK, "D1", 300),
        source_class=SourceClass.VERIFIED,
    )
    belief.apply_facts(item.facts or (), trust_all, report=item)
    assert belief.substations["S1"].state == Estimate("failed", 6, "R1", 98)
    assert belief.substations["S1"].eta_minutes == Estimate(45, 6, "R1", 98)
    assert belief.burn("H1") == 4
    assert belief.depots["D1"].stock_l == Estimate(300, 6, "R1", 98)
    assert belief.fuel_now("H1") is None  # nothing known about the tank yet
    assert not belief.powered("H1")


def test_a_confirm_reply_overrides_with_the_truth() -> None:
    belief = BeliefState()
    claim = fact(FactType.ROAD_BLOCKED, "A1-B1")
    fuel = fact(FactType.HOSPITAL_FUEL, "H1", 300)
    item = view("R1", 2, claim, fuel, source_class=SourceClass.UNVERIFIED)
    belief.apply_facts(item.facts or (), trust_all, report=item)
    belief.pending_confirms = ("R1",)
    reply = ConfirmReply(
        "R1", 2, 10, (ClaimCheck(claim, False, "open"), ClaimCheck(fuel, False, 80))
    )
    belief.apply_confirm(reply)
    assert belief.roads["A1-B1"] == Estimate(OPEN, 10, "confirm:R1", 100)
    assert belief.hospitals["H1"].fuel_l == Estimate(80, 10, "confirm:R1", 100)
    assert belief.pending_confirms == ()


def test_fuel_is_projected_forward_from_delayed_readings() -> None:
    env = BlackoutEnv(make_scenario(), config=SimConfig(telemetry_delay_ticks=3))
    env.reset()
    for _ in range(3):
        env.advance()
    belief = believe_now(env)
    now = belief.tick
    assert now > 3
    reading = belief.hospitals["H1"].fuel_l
    assert reading == Estimate(100 - 2 * (now - 3), now - 3, "telemetry", 100)
    assert belief.fuel_now("H1") == 100 - 2 * now


def test_a_belief_follows_reports_and_replies_through_observe() -> None:
    env = open_env(events=[report("R1", 0, {"type": "road_blocked", "subject": "B2-C2"})])
    belief = believe_now(env)
    assert belief.blocked_roads() == ("B2-C2",)
    assert set(belief.reports) == {"R1"}


def test_the_digest_is_plain_canonical_data() -> None:
    belief = believe_now(open_env(**grid(events=[report("R1", 0)])))
    digest = belief.digest()
    canonical_json(digest)  # no floats, nothing exotic
    assert digest["roads"] == {
        "A1-B1": {"as_of": 0, "confidence": 85, "source": "R1", "value": "blocked"}
    }
    assert set(digest) == {
        "tick",
        "role",
        "roads",
        "hospitals",
        "substations",
        "depots",
        "pending_confirms",
        "confirms_left",
    }
