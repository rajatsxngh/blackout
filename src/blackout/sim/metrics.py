"""Counters the engine keeps while a run is in progress (docs/PRD.md section 8.10).

The M3 scorer recomputes outcome metrics from the run log alone; these counters are the
engine's own record, which that scorer is cross-checked against.
"""

from dataclasses import dataclass, field


@dataclass(slots=True)
class Metrics:
    dark_minutes: dict[str, int]
    fuel_burned_l: int = 0
    fuel_loaded_l: int = 0
    fuel_delivered_l: int = 0
    drive_minutes: int = 0
    edges_traversed: int = 0
    stuck_events: int = 0
    decision_points: int = 0
    actions_accepted: int = 0
    actions_rejected: dict[str, int] = field(default_factory=dict)

    @property
    def critical_downtime(self) -> int:
        """Total hospital-minutes spent dark: the primary metric."""
        return sum(self.dark_minutes.values())

    @property
    def hospitals_lost_power(self) -> int:
        return sum(1 for minutes in self.dark_minutes.values() if minutes > 0)

    def physics(self) -> dict[str, object]:
        """The counters driven by physics alone (part of the hashed state)."""
        return {
            "dark_minutes": dict(sorted(self.dark_minutes.items())),
            "fuel_burned_l": self.fuel_burned_l,
            "fuel_loaded_l": self.fuel_loaded_l,
            "fuel_delivered_l": self.fuel_delivered_l,
            "drive_minutes": self.drive_minutes,
            "edges_traversed": self.edges_traversed,
            "stuck_events": self.stuck_events,
        }

    def to_dict(self) -> dict[str, object]:
        return {
            "critical_downtime_min": self.critical_downtime,
            "hospitals_lost_power": self.hospitals_lost_power,
            **self.physics(),
            "decision_points": self.decision_points,
            "actions_accepted": self.actions_accepted,
            "actions_rejected": dict(sorted(self.actions_rejected.items())),
        }
