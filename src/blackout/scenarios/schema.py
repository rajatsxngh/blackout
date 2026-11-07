"""The scenario file: the engine's complete, self-contained input (docs/PRD.md section 8.6).

Version 2 (M2) added the power grid: zones, substations and repair crews. A hospital gets grid
power from the zone that contains its intersection; a hospital outside every zone is off the
grid and always runs on its generator. Version 3 (M4) adds radio reports (``kind: "report"``
events) and the messiness ``level`` that decides whether contestants see a report's facts (L0)
or only its text (L1 and up). Version 4 (M5) adds what the generator records: template, seed,
split and tier, generator details, validity results, and a content hash checked on load.
Version 5 (M8) adds levels L2 and L3: report ground truth gains cues, stale and correction
links, structured injections and the level a report first appears at; validity gains the
reference and Rules results that decide a world's tier.

``description`` and everything outside the world itself are metadata for people and the
scorer; contestants only ever see observations (blackout.info.observation).
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

SCHEMA_VERSION = 5
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


class GeneratorInfo(_Spec):
    """How a generated scenario was made (blackout.scenarios.generator)."""

    version: PositiveInt
    config_digest: str = Field(description="Digest of the city config the world was drawn from.")
    attempt: NonNegativeInt = Field(description="Re-roll sub-seed that gave a valid world.")
    world_hash: str = Field(description="world_digest(): equal for every level of one seed.")


class Validity(_Spec):
    """Validity and tier results recorded at generation (docs/PRD.md section 8.6).

    Do-nothing, the reference and Rules are measured on the scenario's own level, except
    ``rules_l0_critical_downtime`` (Rules at L0 decides the tier, so every level of a seed
    records the same value). ``reference_min_slack`` is the tightest moment of the reference
    run: the fewest minutes of fuel any hospital running on its generator had left.
    """

    do_nothing_critical_downtime: NonNegativeInt
    do_nothing_trips: NonNegativeInt
    reference_critical_downtime: NonNegativeInt | None = None
    reference_min_slack: NonNegativeInt | None = None
    rules_l0_critical_downtime: NonNegativeInt | None = None


Tier = Literal["easy", "medium", "hard"]


Split = Literal["handmade", "dev-v0", "dev", "test"]


class Scenario(_Spec):
    schema_version: Literal[5] = SCHEMA_VERSION
    id: str = Field(pattern=_ID)
    description: str = ""
    template: str | None = Field(default=None, pattern=r"^T\d\d$")
    seed: NonNegativeInt | None = None
    level: int = Field(default=0, ge=0, le=3, description="Messiness: L0 clean to L3 adversarial.")
    split: Split = "handmade"
    tier: Tier | None = None
    horizon: PositiveInt = 240
    city: CitySpec = Field(default_factory=CitySpec)
    hospitals: list[HospitalSpec]
    depots: list[DepotSpec] = Field(default_factory=list)
    trucks: list[TruckSpec] = Field(default_factory=list)
    crews: list[CrewSpec] = Field(default_factory=list)
    substations: list[SubstationSpec] = Field(default_factory=list)
    zones: list[ZoneSpec] = Field(default_factory=list)
    events: list[ScenarioEvent] = Field(default_factory=list)
    generator: GeneratorInfo | None = None
    validity: Validity | None = None
    content_hash: str | None = Field(
        default=None, description="content_digest() of everything else; checked on load."
    )

    @model_validator(mode="after")
    def _check_references(self) -> Self:
        problems = [*self._city_problems(), *self._grid_problems(), *self._event_problems()]
        if problems:
            raise ValueError("; ".join(problems))
        return self

    @model_validator(mode="after")
    def _check_content_hash(self) -> Self:
        if self.content_hash is not None and self.content_hash != content_digest(self):
            raise ValueError(
                f"content_hash {self.content_hash} doesn't match the content: the file was"
                " edited after it was generated"
            )
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


def content_digest(scenario: Scenario) -> str:
    """Digest of everything in the scenario except its own ``content_hash`` field."""
    return digest(scenario.model_dump(mode="json", exclude={"content_hash"}))


_RENDERING = ("text", "bank", "phrasings", "noise")


def world_digest(scenario: Scenario) -> str:
    """Digest of the physical world and the facts reported about it, at what ticks.

    It leaves out the scenario's ID and level, its content hash, the world hash it records,
    the validity results of its own level, and how each report was worded (text, bank,
    phrasings, noise). Only the reports every level shares (``from_level`` 0) count, without
    their IDs: an L2 or L3 report slots in by tick, which renumbers the ones after it. Every
    level made from one seed must give the same value: same world, same facts at the same
    ticks (PRD 8.4).
    """
    data = scenario.model_dump(mode="json", exclude={"id", "level", "content_hash", "validity"})
    if data["generator"] is not None:
        del data["generator"]["world_hash"]
    events = []
    for event in data["events"]:
        if event["kind"] == "report":
            if event["truth"]["from_level"] > 0:
                continue
            for key in ("id", "text"):
                event.pop(key)
            for key in _RENDERING[1:]:
                event["truth"].pop(key)
        events.append(event)
    data["events"] = events
    return digest(data)
