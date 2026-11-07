"""Engine rules that are the same for every scenario."""

from collections.abc import Mapping
from dataclasses import asdict, dataclass, fields
from typing import Self

from blackout.sim.entities import Priority


@dataclass(frozen=True, slots=True)
class SimConfig:
    """Timing, weighting and information rules shared by every scenario (PRD sections 8.1-8.4).

    It's logged in every run header, so a replay uses exactly the rules the run had.
    """

    load_minutes: int = 5
    unload_minutes: int = 5
    heartbeat_ticks: int = 15
    ttf_thresholds: tuple[int, ...] = (60, 30, 15)
    trip_grace_minutes: int = 3
    reset_minutes: int = 10
    weight_critical: int = 100
    weight_essential: int = 10
    weight_residential: int = 1
    # Information layer (PRD section 8.4). The delay makes hospital and depot readings D ticks
    # old (a stress modifier); fleet, grid and road telemetry are always real-time.
    telemetry_delay_ticks: int = 0
    confirm_delay_ticks: int = 8
    confirm_limit: int = 4

    def weight(self, priority: Priority) -> int:
        """How much one kW-minute of unmet demand counts, by zone priority."""
        if priority is Priority.CRITICAL:
            return self.weight_critical
        if priority is Priority.ESSENTIAL:
            return self.weight_essential
        return self.weight_residential

    def to_dict(self) -> dict[str, object]:
        return {
            key: list(value) if isinstance(value, tuple) else value
            for key, value in asdict(self).items()
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> Self:
        """Inverse of ``to_dict`` (JSON turns tuples into lists, so they're turned back)."""
        values = dict(data)
        for field in fields(cls):
            value = values.get(field.name)
            if isinstance(field.default, tuple) and isinstance(value, list):
                values[field.name] = tuple(value)
        return cls(**values)  # type: ignore[arg-type]
