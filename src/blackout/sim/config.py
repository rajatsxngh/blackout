"""Engine rules that are the same for every scenario."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SimConfig:
    """Timing rules shared by every scenario (docs/PRD.md sections 8.1 and 8.2)."""

    load_minutes: int = 5
    unload_minutes: int = 5
    heartbeat_ticks: int = 15
    ttf_thresholds: tuple[int, ...] = (60, 30, 15)
