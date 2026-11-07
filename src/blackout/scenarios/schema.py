"""The scenario file: the engine's complete, self-contained input (docs/PRD.md section 8.6).

M1 covers what the engine needs today. Later milestones add zones and substations (M2),
reports (M4) and generator metadata (M5), bumping ``schema_version`` when the shape changes.
"""

from collections import Counter
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, NonNegativeInt, PositiveInt, model_validator

from blackout.sim.city import MAX_HEIGHT, MAX_WIDTH, edge_ids, node_ids
from blackout.sim.events import BurnRateChange, RoadBlock, RoadClear, ScenarioEvent, TruckBreakdown

SCHEMA_VERSION = 1
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
    grid_powered: bool = False

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


class Scenario(_Spec):
    schema_version: Literal[1] = SCHEMA_VERSION
    id: str = Field(pattern=_ID)
    description: str = ""
    horizon: PositiveInt = 240
    city: CitySpec = Field(default_factory=CitySpec)
    hospitals: list[HospitalSpec]
    depots: list[DepotSpec]
    trucks: list[TruckSpec]
    events: list[ScenarioEvent] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_references(self) -> Self:
        nodes = set(node_ids(self.city.width, self.city.height))
        edges = set(edge_ids(self.city.width, self.city.height))
        problems: list[str] = []

        problems += [
            f"unknown road segment {e!r}" for e in self.city.travel_minutes if e not in edges
        ]
        problems += [
            f"landmark {name!r} at unknown intersection {node!r}"
            for name, node in self.city.landmarks.items()
            if node not in nodes
        ]
        entities = [*self.hospitals, *self.depots, *self.trucks]
        problems += [
            f"{entity.id} is at unknown intersection {entity.node!r}"
            for entity in entities
            if entity.node not in nodes
        ]
        counts = Counter(entity.id for entity in entities)
        problems += [f"duplicate id {name!r}" for name, n in sorted(counts.items()) if n > 1]

        hospitals = {h.id for h in self.hospitals}
        trucks = {t.id for t in self.trucks}
        for event in self.events:
            if event.tick >= self.horizon:
                problems.append(f"{event.kind} at tick {event.tick} is not before the horizon")
            match event:
                case RoadBlock(edge=edge) | RoadClear(edge=edge) if edge not in edges:
                    problems.append(f"{event.kind} on unknown road segment {edge!r}")
                case TruckBreakdown(truck=truck) if truck not in trucks:
                    problems.append(f"truck_breakdown for unknown truck {truck!r}")
                case BurnRateChange(hospital=hospital) if hospital not in hospitals:
                    problems.append(f"burn_rate_change for unknown hospital {hospital!r}")
                case _:
                    pass

        if problems:
            raise ValueError("; ".join(problems))
        return self


def load_scenario(path: Path) -> Scenario:
    """Read and validate a scenario file."""
    return Scenario.model_validate_json(path.read_text(encoding="utf-8"))
