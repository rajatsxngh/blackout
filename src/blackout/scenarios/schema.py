"""The scenario file: the engine's complete, self-contained input (docs/PRD.md section 8.6).

Version 2 (M2) added the power grid: zones, substations and repair crews. A hospital gets grid
power from the zone that contains its intersection; a hospital outside every zone is off the
grid and always runs on its generator. Version 3 (M4) adds radio reports (``kind: "report"``
events) and the messiness ``level`` that decides whether contestants see a report's facts (L0)
or only its text (L1 and up). M5 adds generator metadata, bumping ``schema_version`` again.
"""

from collections import Counter
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, NonNegativeInt, PositiveInt, model_validator

from blackout.info.reports import SUBJECT_KIND, Report
from blackout.sim.city import MAX_HEIGHT, MAX_WIDTH, edge_ids, node_ids
from blackout.sim.events import (
    BurnRateChange,
    RoadBlock,
    RoadClear,
    ScenarioEvent,
    SubstationFail,
    TruckBreakdown,
)
from blackout.sim.hashing import digest

SCHEMA_VERSION = 3
_ID = r"^[A-Za-z][A-Za-z0-9_]*$"


class _Spec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CitySpec(_Spec):
    width: int = Field(default=6, ge=2, le=MAX_WIDTH)
    height: int = Field(default=6, ge=2, le=MAX_HEIGHT)
    default_travel_minutes: PositiveInt = 3
    travel_minutes: dict[str, PositiveInt] = Field(default_factory=dict)
    landmarks: dict[str, str] = Field(default_factory=dict)


class HospitalSpec(_Spec):
    id: str = Field(pattern=_ID)
    name: str
    node: str
    tank_l: PositiveInt
    fuel_l: NonNegativeInt
    burn_lpm: PositiveInt

    @model_validator(mode="after")
    def _fuel_fits(self) -> Self:
        if self.fuel_l > self.tank_l:
            raise ValueError(f"{self.id}: fuel_l {self.fuel_l} exceeds tank_l {self.tank_l}")
        return self


class DepotSpec(_Spec):
    id: str = Field(pattern=_ID)
    name: str
    node: str
    stock_l: NonNegativeInt


class TruckSpec(_Spec):
    id: str = Field(pattern=_ID)
    node: str
    capacity_l: PositiveInt = 500
    load_l: NonNegativeInt = 0

    @model_validator(mode="after")
    def _load_fits(self) -> Self:
        if self.load_l > self.capacity_l:
            raise ValueError(
                f"{self.id}: load_l {self.load_l} exceeds capacity_l {self.capacity_l}"
            )
        return self


class CrewSpec(_Spec):
    id: str = Field(pattern=_ID)
    node: str


class SubstationSpec(_Spec):
    id: str = Field(pattern=_ID)
    name: str
    node: str
    capacity_kw: PositiveInt
    state: Literal["online", "failed", "tripped"] = "online"
    repair_minutes: PositiveInt | None = Field(
        default=None, description="Crew minutes needed to fix it; required when it starts failed."
    )

    @model_validator(mode="after")
    def _repair_time_matches_state(self) -> Self:
        if (self.state == "failed") != (self.repair_minutes is not None):
            raise ValueError(f"{self.id}: repair_minutes is required exactly when state is failed")
        return self


class ZoneSpec(_Spec):
    id: str = Field(pattern=_ID)
    name: str
    nodes: list[str] = Field(min_length=1)
    demand_kw: PositiveInt
    priority: Literal["critical", "essential", "residential"]
    primary: str
    alternate: str | None = None


class Scenario(_Spec):
    schema_version: Literal[3] = SCHEMA_VERSION
    id: str = Field(pattern=_ID)
    description: str = ""
    level: int = Field(default=0, ge=0, le=3, description="Messiness: L0 clean to L3 adversarial.")
    horizon: PositiveInt = 240
    city: CitySpec = Field(default_factory=CitySpec)
    hospitals: list[HospitalSpec]
    depots: list[DepotSpec] = Field(default_factory=list)
    trucks: list[TruckSpec] = Field(default_factory=list)
    crews: list[CrewSpec] = Field(default_factory=list)
    substations: list[SubstationSpec] = Field(default_factory=list)
    zones: list[ZoneSpec] = Field(default_factory=list)
    events: list[ScenarioEvent] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_references(self) -> Self:
        problems = [*self._city_problems(), *self._grid_problems(), *self._event_problems()]
        if problems:
            raise ValueError("; ".join(problems))
        return self

    def _city_problems(self) -> list[str]:
        nodes = set(node_ids(self.city.width, self.city.height))
        edges = set(edge_ids(self.city.width, self.city.height))
        problems = [
            f"unknown road segment {e!r}" for e in self.city.travel_minutes if e not in edges
        ]
        problems += [
            f"landmark {name!r} at unknown intersection {node!r}"
            for name, node in self.city.landmarks.items()
            if node not in nodes
        ]
        placed = [*self.hospitals, *self.depots, *self.trucks, *self.crews, *self.substations]
        problems += [
            f"{thing.id} is at unknown intersection {thing.node!r}"
            for thing in placed
            if thing.node not in nodes
        ]
        counts = Counter(thing.id for thing in [*placed, *self.zones])
        problems += [f"duplicate id {name!r}" for name, n in sorted(counts.items()) if n > 1]
        return problems

    def _grid_problems(self) -> list[str]:
        nodes = set(node_ids(self.city.width, self.city.height))
        substations = {s.id for s in self.substations}
        problems: list[str] = []
        zone_of: dict[str, str] = {}
        for zone in self.zones:
            for node in zone.nodes:
                if node not in nodes:
                    problems.append(f"zone {zone.id} includes unknown intersection {node!r}")
                elif node in zone_of:
                    problems.append(f"{node} is in both {zone_of[node]} and {zone.id}")
                else:
                    zone_of[node] = zone.id
            for feeder in (zone.primary, zone.alternate):
                if feeder is not None and feeder not in substations:
                    problems.append(f"zone {zone.id} is fed by unknown substation {feeder!r}")
            if zone.alternate == zone.primary:
                problems.append(f"zone {zone.id} has the same primary and alternate")
        priorities = {zone.id: zone.priority for zone in self.zones}
        for hospital in self.hospitals:
            zone_id = zone_of.get(hospital.node)
            if zone_id is not None and priorities[zone_id] != "critical":
                problems.append(f"zone {zone_id} contains {hospital.id}, so it must be critical")
        return problems

    def _event_problems(self) -> list[str]:
        edges = set(edge_ids(self.city.width, self.city.height))
        hospitals = {h.id for h in self.hospitals}
        trucks = {t.id for t in self.trucks}
        substations = {s.id for s in self.substations}
        subjects = {
            "road": edges,
            "substation": substations,
            "hospital": hospitals,
            "depot": {d.id for d in self.depots},
        }
        problems: list[str] = []
        report_ids: set[str] = set()
        for event in self.events:
            if isinstance(event, Report):
                problems += self._report_problems(event, subjects, report_ids)
            if event.tick >= self.horizon:
                problems.append(f"{event.kind} at tick {event.tick} is not before the horizon")
            match event:
                case RoadBlock(edge=edge) | RoadClear(edge=edge) if edge not in edges:
                    problems.append(f"{event.kind} on unknown road segment {edge!r}")
                case TruckBreakdown(truck=truck) if truck not in trucks:
                    problems.append(f"truck_breakdown for unknown truck {truck!r}")
                case BurnRateChange(hospital=hospital) if hospital not in hospitals:
                    problems.append(f"burn_rate_change for unknown hospital {hospital!r}")
                case SubstationFail(substation=substation) if substation not in substations:
                    problems.append(f"substation_fail for unknown substation {substation!r}")
                case _:
                    pass
        return problems

    def _report_problems(
        self, report: Report, subjects: dict[str, set[str]], seen: set[str]
    ) -> list[str]:
        problems: list[str] = []
        if report.id in seen:
            problems.append(f"duplicate report id {report.id!r}")
        seen.add(report.id)
        if self.level > 0 and not report.text:
            problems.append(f"report {report.id} needs text at level L{self.level}")
        for fact in report.facts:
            kind = SUBJECT_KIND[fact.type]
            if fact.subject not in subjects[kind]:
                problems.append(
                    f"report {report.id}: {fact.type} about unknown {kind} {fact.subject!r}"
                )
        return problems


def load_scenario(path: Path) -> Scenario:
    """Read and validate a scenario file."""
    return Scenario.model_validate_json(path.read_text(encoding="utf-8"))


def scenario_digest(scenario: Scenario) -> str:
    """Content hash of a validated scenario (defaults included), independent of file layout."""
    return digest(scenario.model_dump(mode="json"))
