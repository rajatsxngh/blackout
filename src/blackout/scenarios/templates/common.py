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
    crew_source,
    depot_source,
    fact,
    hospital_source,
    police_source,
    public_source,
)
from blackout.sim.city import EdgeId

MIN_DO_NOTHING_LOSS = 30
"""PRD 8.6: do-nothing must lose at least 30 hospital-minutes (the reference half is M8)."""

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
    backup feeder is out too, so the hospital runs on its generator. Returns the substation."""
    hospital = draft.hospital(hospital_id)
    zone = draft.zone_of(hospital.node)
    substation_id = zone.primary
    draft.update_substation(substation_id, state="failed", repair_minutes=repair)
    draft.update_zone(zone.id, alternate=None)
    draft.fit_capacity(frozenset({substation_id}))
    substation = draft.substation(substation_id)
    draft.story.append(
        f"{substation.name} ({substation_id}) is down from the start ({repair} min of crew work)"
        f" and {zone.name}'s backup feeder is out, so {hospital.name} ({hospital_id}) is on its"
        " generator."
    )
    return substation_id


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
    burned = 0 if unpowered_from is None else hospital.burn_lpm * max(0, tick - unpowered_from)
    level = max(0, hospital.fuel_l - burned)
    draft.plan(
        tick,
        Channel.HOSPITAL_STAFF,
        SourceClass.FIELD,
        hospital_source(rng, hospital),
        TimedFact(fact(FactType.HOSPITAL_FUEL, hospital_id, level), tick),
    )


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


def report_block(draft: Draft, rng: Rng, segment: EdgeId, blocked_at: int, delay: int) -> None:
    """Police report a block ``delay`` minutes after it happens; a caller may echo it."""
    claim = TimedFact(fact(FactType.ROAD_BLOCKED, segment), blocked_at)
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
