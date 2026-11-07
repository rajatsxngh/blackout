"""Pieces shared by templates: the template protocol, breaking the grid, and planning reports.

Every planned fact must be true at its report's tick *whatever the contestant does*, because
the text is written before the run. So reports about things contestants can change go out
early: fuel and stock readings before tick 5 (no load or delivery can finish sooner, D015),
a damage report within a few minutes of the failure (no repair can finish that fast), and a
repair estimate before a crew can have done 10% of the work (the claim tolerance).
"""

from typing import Protocol

from blackout.info.reports import Channel, FactType, SourceClass
from blackout.scenarios.schema import Validity
from blackout.scenarios.seeding import Rng
from blackout.scenarios.text.render import TimedFact
from blackout.scenarios.world import (
    Draft,
    FuelReading,
    crew_source,
    depot_source,
    fact,
    hospital_source,
    police_source,
    public_source,
)
from blackout.sim.city import EdgeId

MIN_DO_NOTHING_LOSS = 30
"""PRD 8.6: do-nothing must lose at least 30 hospital-minutes (and the reference none)."""

EARLY_TICKS = (1, 4)
"""Fuel and stock readings go out in this window: nothing a contestant does lands sooner."""


class Template(Protocol):
    id: str
    title: str

    def apply(self, draft: Draft, rng: Rng) -> None:
        """Turn a calm world into this template's situation. Raises Infeasible if it can't."""
        ...

    def problem(self, validity: Validity) -> str | None:
        """Why a finished world doesn't show the situation, or None if it does."""
        ...


def do_nothing_problem(validity: Validity) -> str | None:
    if validity.do_nothing_critical_downtime < MIN_DO_NOTHING_LOSS:
        return (
            f"do-nothing loses only {validity.do_nothing_critical_downtime} hospital-minutes"
            f" (at least {MIN_DO_NOTHING_LOSS} needed)"
        )
    return None


def repair_minutes(draft: Draft, rng: Rng, span: tuple[int, int]) -> int:
    return rng.between(*span, draft.config.repair_step_minutes)


def isolate_hospital(draft: Draft, hospital_id: str, repair: int) -> str:
    """The substation feeding ``hospital_id``'s zone is down from the start, and the zone's
    backup feeder is out too, so the hospital runs on its generator. Returns the substation.
    If that substation is already down, it stays down with its existing repair time."""
    hospital = draft.hospital(hospital_id)
    zone = draft.zone_of(hospital.node)
    substation_id = zone.primary
    if draft.substation(substation_id).state == "online":
        draft.update_substation(substation_id, state="failed", repair_minutes=repair)
    draft.update_zone(zone.id, alternate=None)
    down = frozenset(s.id for s in draft.substations if s.state != "online")
    draft.fit_capacity(down)
    substation = draft.substation(substation_id)
    draft.story.append(
        f"{substation.name} ({substation_id}) is down from the start"
        f" ({substation.repair_minutes} min of crew work) and {zone.name}'s backup feeder is"
        f" out, so {hospital.name} ({hospital_id}) is on its generator."
    )
    return substation_id


def scatter_fleet(draft: Draft, rng: Rng, keep: frozenset[str] = frozenset()) -> None:
    """Spread the trucks (except ``keep``) over the city, some carrying part of a load.

    Trucks parked at the depots with nothing aboard make every plan look alike. Scattered
    trucks, some loaded and far away, some empty next to a depot, are where choosing which
    truck goes where starts to matter (PRD 8.7: "nearest loaded truck, or via the nearest
    depot" is the greedy rule).
    """
    cfg = draft.config.fleet
    nodes = sorted(n for z in draft.zones for n in z.nodes)
    moved = []
    for truck in draft.trucks:
        if truck.id in keep or not rng.chance(cfg.scatter_pct):
            continue
        node = rng.pick(nodes)
        load = 0
        if rng.chance(cfg.loaded_pct):
            load = rng.between(cfg.load_l[0], min(cfg.load_l[1], truck.capacity_l), 50)
        draft.update_truck(truck.id, node=node, load_l=load)
        moved.append(f"{truck.id} at {node} with {load} L")
    if moved:
        draft.story.append("Trucks out on the road: " + ", ".join(moved) + ".")


def report_spike(draft: Draft, rng: Rng, hospital_id: str, tick: int, burn: int) -> None:
    """Hospital staff call in a generator burning faster, as it happens."""
    hospital = draft.hospital(hospital_id)
    draft.plan(
        tick + rng.between(0, 1),
        Channel.HOSPITAL_STAFF,
        SourceClass.FIELD,
        hospital_source(rng, hospital),
        TimedFact(fact(FactType.BURN_SPIKE, hospital_id, burn), tick),
    )


def report_failure(draft: Draft, rng: Rng, substation_id: str, failed_at: int, repair: int) -> None:
    """Grid control confirms the failure; a line crew estimates the repair, sometimes in the
    same message; sometimes a member of the public calls it in first."""
    cfg = draft.config.reports
    crew = rng.pick(draft.crews)
    status = TimedFact(fact(FactType.SUBSTATION_STATUS, substation_id, "failed"), failed_at)
    eta = TimedFact(fact(FactType.REPAIR_ETA, substation_id, repair), failed_at)
    if rng.chance(cfg.public_echo_pct):
        draft.plan(
            failed_at + rng.between(0, 2),
            Channel.PUBLIC,
            SourceClass.UNVERIFIED,
            public_source(rng),
            status,
        )
    if rng.chance(cfg.bundle_pct):
        draft.plan(
            failed_at + rng.between(2, 4),
            Channel.CREW_RADIO,
            SourceClass.FIELD,
            crew_source(crew),
            status,
            eta,
        )
        return
    draft.plan(
        failed_at + rng.between(1, 3),
        Channel.CREW_RADIO,
        SourceClass.VERIFIED,
        "Grid control center",
        status,
    )
    draft.plan(
        failed_at + rng.between(2, 5), Channel.CREW_RADIO, SourceClass.FIELD, crew_source(crew), eta
    )


def report_fuel(draft: Draft, rng: Rng, hospital_id: str, unpowered_from: int | None) -> None:
    """Hospital staff read their tank early on. ``unpowered_from``: tick its generator started
    (None: on the grid until after the reading)."""
    hospital = draft.hospital(hospital_id)
    tick = rng.between(*EARLY_TICKS)
    planned = draft.plan(
        tick,
        Channel.HOSPITAL_STAFF,
        SourceClass.FIELD,
        hospital_source(rng, hospital),
        TimedFact(fact(FactType.HOSPITAL_FUEL, hospital_id, hospital.fuel_l), tick),
    )
    assert planned is not None
    draft.fuel_readings.append(FuelReading(planned, hospital_id, unpowered_from))
    draft.set_fuel(hospital_id, hospital.fuel_l)  # fills in the reading


def report_depot(draft: Draft, rng: Rng) -> None:
    """Sometimes a depot calls in its stock early on."""
    if not rng.chance(draft.config.reports.depot_report_pct):
        return
    depot = rng.pick(draft.depots)
    tick = rng.between(*EARLY_TICKS)
    draft.plan(
        tick,
        Channel.DEPOT_STAFF,
        SourceClass.FIELD,
        depot_source(rng, depot),
        TimedFact(fact(FactType.DEPOT_STOCK, depot.id, depot.stock_l), tick),
    )


def report_block(
    draft: Draft,
    rng: Rng,
    segment: EdgeId,
    blocked_at: int,
    delay: int,
    *,
    caller_first: bool = False,
    police: bool = True,
) -> None:
    """Police report a block ``delay`` minutes after it happens; a caller may echo it.

    With ``caller_first``, a member of the public calls it in ``delay`` minutes after it
    happens and the police only later, if at all (``police``). That caller is an unverified
    source telling the truth, and often the only early warning: PRD 8.4's "critical true
    unverified report", which a contestant that ignores unverified sources pays for.
    """
    claim = TimedFact(fact(FactType.ROAD_BLOCKED, segment), blocked_at)
    if caller_first:
        draft.plan(
            blocked_at + delay, Channel.PUBLIC, SourceClass.UNVERIFIED, public_source(rng), claim
        )
        if police:
            later = blocked_at + delay + rng.between(*draft.config.reports.police_late_minutes)
            draft.plan(later, Channel.POLICE_RADIO, SourceClass.FIELD, police_source(rng), claim)
        return
    draft.plan(
        blocked_at + delay, Channel.POLICE_RADIO, SourceClass.FIELD, police_source(rng), claim
    )
    if rng.chance(draft.config.reports.public_echo_pct):
        draft.plan(
            blocked_at + delay + rng.between(1, 6),
            Channel.PUBLIC,
            SourceClass.UNVERIFIED,
            public_source(rng),
            claim,
        )


def report_clear(draft: Draft, rng: Rng, segment: EdgeId, cleared_at: int) -> None:
    claim = TimedFact(fact(FactType.ROAD_CLEARED, segment), cleared_at)
    draft.plan(
        cleared_at + rng.between(1, 4),
        Channel.POLICE_RADIO,
        SourceClass.FIELD,
        police_source(rng),
        claim,
    )
