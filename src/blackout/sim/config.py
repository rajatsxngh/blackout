"""Engine rules that are the same for every scenario."""

from dataclasses import dataclass

from blackout.sim.entities import Priority


@dataclass(frozen=True, slots=True)
class SimConfig:
    """Timing and weighting rules shared by every scenario (docs/PRD.md sections 8.1-8.2)."""

    load_minutes: int = 5
    unload_minutes: int = 5
    heartbeat_ticks: int = 15
    ttf_thresholds: tuple[int, ...] = (60, 30, 15)
    trip_grace_minutes: int = 3
    reset_minutes: int = 10
    weight_critical: int = 100
    weight_essential: int = 10
    weight_residential: int = 1

    def weight(self, priority: Priority) -> int:
        """How much one kW-minute of unmet demand counts, by zone priority."""
        if priority is Priority.CRITICAL:
            return self.weight_critical
        if priority is Priority.ESSENTIAL:
            return self.weight_essential
        return self.weight_residential
