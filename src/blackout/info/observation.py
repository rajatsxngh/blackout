"""Observations: the only window contestants have onto the world (invariant I2).

An observation is one role's view at a decision point (docs/PRD.md section 8.4): the header
(tick and the triggers that woke it), the telemetry in its scope, reports routed to it since
its last decision point, confirm replies that arrived, its confirm requests still waiting for a
reply, and how many confirmations the run has left.
Sections outside a role's scope are empty. Nothing in an observation is hidden truth.
"""

from dataclasses import dataclass, field, fields
from enum import Enum

from pydantic import BaseModel

from blackout.info.reports import ConfirmReply, ReportView
from blackout.info.roles import Section, sees
from blackout.info.telemetry import (
    CrewView,
    DepotView,
    HospitalView,
    SubstationView,
    Telemetry,
    TruckView,
    ZoneView,
)
from blackout.sim.city import City, EdgeId
from blackout.sim.hashing import digest
from blackout.sim.triggers import Trigger

__all__ = [
    "CrewView",
    "DepotView",
    "HospitalView",
    "Observation",
    "SubstationView",
    "TruckView",
    "ZoneView",
    "build_observation",
    "plain",
]


@dataclass(frozen=True, slots=True)
class Observation:
    role: str
    tick: int
    horizon: int
    triggers: tuple[Trigger, ...]
    trucks: tuple[TruckView, ...]
    crews: tuple[CrewView, ...]
    hospitals: tuple[HospitalView, ...]
    depots: tuple[DepotView, ...]
    substations: tuple[SubstationView, ...]
    zones: tuple[ZoneView, ...]
    known_blocks: tuple[EdgeId, ...]
    city: City = field(repr=False)
    reports: tuple[ReportView, ...] = ()
    confirmations: tuple[ConfirmReply, ...] = ()
    confirms_left: int = 0
    confirms_pending: tuple[str, ...] = ()

    def truck(self, truck_id: str) -> TruckView:
        return next(t for t in self.trucks if t.id == truck_id)

    def crew(self, crew_id: str) -> CrewView:
        return next(c for c in self.crews if c.id == crew_id)

    def hospital(self, hospital_id: str) -> HospitalView:
        return next(h for h in self.hospitals if h.id == hospital_id)

    def depot(self, depot_id: str) -> DepotView:
        return next(d for d in self.depots if d.id == depot_id)

    def substation(self, substation_id: str) -> SubstationView:
        return next(s for s in self.substations if s.id == substation_id)

    def zone(self, zone_id: str) -> ZoneView:
        return next(z for z in self.zones if z.id == zone_id)

    def to_dict(self) -> dict[str, object]:
        """Everything in the observation except the static map, in JSON-ready form."""
        return {f.name: plain(getattr(self, f.name)) for f in fields(self) if f.name != "city"}

    def digest(self) -> str:
        """A content hash, so run logs can record exactly what a contestant was shown."""
        return digest(self.to_dict())


def plain(value: object) -> object:
    """``value`` as JSON-ready data: dataclasses and models become dicts, tuples lists."""
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, tuple | list):
        return [plain(item) for item in value]
    if isinstance(value, dict):
        return {key: plain(item) for key, item in value.items()}
    if hasattr(value, "__dataclass_fields__"):
        return {f.name: plain(getattr(value, f.name)) for f in fields(value)}  # type: ignore[arg-type]
    return value


def build_observation(
    role: str,
    tick: int,
    horizon: int,
    city: City,
    seen: Telemetry,
    *,
    triggers: tuple[Trigger, ...] = (),
    reports: tuple[ReportView, ...] = (),
    confirmations: tuple[ConfirmReply, ...] = (),
    confirms_left: int = 0,
    confirms_pending: tuple[str, ...] = (),
) -> Observation:
    """``role``'s observation: the telemetry sections in its scope, and its own inbox."""

    def part[T](section: Section, views: tuple[T, ...]) -> tuple[T, ...]:
        return views if sees(role, section) else ()

    return Observation(
        role=role,
        tick=tick,
        horizon=horizon,
        triggers=triggers,
        trucks=part(Section.TRUCKS, seen.trucks),
        crews=part(Section.CREWS, seen.crews),
        hospitals=part(Section.HOSPITALS, seen.hospitals),
        depots=part(Section.DEPOTS, seen.depots),
        substations=part(Section.SUBSTATIONS, seen.substations),
        zones=part(Section.ZONES, seen.zones),
        known_blocks=part(Section.ROADS, seen.known_blocks),
        city=city,
        reports=reports,
        confirmations=confirmations,
        confirms_left=confirms_left,
        confirms_pending=confirms_pending,
    )
