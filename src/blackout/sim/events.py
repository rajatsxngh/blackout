"""Scenario events and the deterministic event queue (docs/PRD.md section 8.2).

Every scenario event is fixed in the scenario file, so the engine itself has no randomness.
Items due on the same tick come out in a fixed order: by kind, then in the order they were
added.
"""

import heapq
from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, NonNegativeInt, PositiveInt


class _Event(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tick: NonNegativeInt


class RoadBlock(_Event):
    """A road segment becomes impassable. Nobody is told; a truck finds out when it gets there."""

    kind: Literal["road_block"] = "road_block"
    edge: str


class RoadClear(_Event):
    """A blocked road segment opens again. Nobody is told about this either."""

    kind: Literal["road_clear"] = "road_clear"
    edge: str


class TruckBreakdown(_Event):
    """A truck stops where it is, keeping its load, until it's repaired (if ever)."""

    kind: Literal["truck_breakdown"] = "truck_breakdown"
    truck: str
    repair_minutes: PositiveInt | None = None


class BurnRateChange(_Event):
    """A hospital's generator starts burning fuel at a new rate (liters per minute)."""

    kind: Literal["burn_rate_change"] = "burn_rate_change"
    hospital: str
    burn_lpm: PositiveInt


class SubstationFail(_Event):
    """A substation is damaged. Fixing it takes a crew ``repair_minutes`` on site (hidden)."""

    kind: Literal["substation_fail"] = "substation_fail"
    substation: str
    repair_minutes: PositiveInt


ScenarioEvent = Annotated[
    RoadBlock | RoadClear | TruckBreakdown | BurnRateChange | SubstationFail,
    Field(discriminator="kind"),
]


@dataclass(frozen=True, slots=True)
class TruckRepair:
    """Runtime item: a broken truck is fixed. Scheduled by ``TruckBreakdown.repair_minutes``."""

    truck: str


type QueueItem = (
    RoadBlock | RoadClear | TruckBreakdown | BurnRateChange | SubstationFail | TruckRepair
)

# Order within one tick: clears before blocks, and repairs before new breakdowns.
_PRIORITY: dict[type, int] = {
    RoadClear: 0,
    RoadBlock: 1,
    BurnRateChange: 2,
    SubstationFail: 3,
    TruckRepair: 4,
    TruckBreakdown: 5,
}


class EventQueue:
    """A min-heap of (tick, priority, insertion order, item)."""

    def __init__(self) -> None:
        self._heap: list[tuple[int, int, int, QueueItem]] = []
        self._added = 0

    def push(self, tick: int, item: QueueItem) -> None:
        heapq.heappush(self._heap, (tick, _PRIORITY[type(item)], self._added, item))
        self._added += 1

    def pop_due(self, tick: int) -> list[QueueItem]:
        """Remove and return every item due at or before ``tick``, in order."""
        due: list[QueueItem] = []
        while self._heap and self._heap[0][0] <= tick:
            due.append(heapq.heappop(self._heap)[3])
        return due

    def __len__(self) -> int:
        return len(self._heap)
