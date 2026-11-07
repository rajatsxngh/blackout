"""Counters the engine keeps while a run is in progress (docs/PRD.md section 8.10).

The M3 scorer recomputes outcome metrics from the run log alone; these counters are the
engine's own record, which that scorer is cross-checked against.
"""

from dataclasses import dataclass, field


@dataclass(slots=True)
class Metrics:
    dark_minutes: dict[str, int]
    burned_l: dict[str, int]
    zone_outage_minutes: dict[str, int]
    fuel_loaded_l: int = 0
    fuel_delivered_l: int = 0
    drive_minutes: int = 0
    edges_traversed: int = 0
    crew_drive_minutes: int = 0
    stuck_events: int = 0
    unmet_kw_min: int = 0
    unmet_weighted_kw_min: int = 0
    trips: int = 0
    repairs_completed: int = 0
    outage_until: int = 0  # 1 + the last minute in which some zone had no power
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

    @property
    def fuel_burned_l(self) -> int:
        return sum(self.burned_l.values())

    def physics(self) -> dict[str, object]:
        """The counters driven by physics alone (part of the hashed state)."""
        return {
            "dark_minutes": dict(sorted(self.dark_minutes.items())),
            "burned_l": dict(sorted(self.burned_l.items())),
            "zone_outage_minutes": dict(sorted(self.zone_outage_minutes.items())),
            "fuel_loaded_l": self.fuel_loaded_l,
            "fuel_delivered_l": self.fuel_delivered_l,
            "drive_minutes": self.drive_minutes,
            "edges_traversed": self.edges_traversed,
            "crew_drive_minutes": self.crew_drive_minutes,
            "stuck_events": self.stuck_events,
            "unmet_kw_min": self.unmet_kw_min,
            "unmet_weighted_kw_min": self.unmet_weighted_kw_min,
            "trips": self.trips,
            "repairs_completed": self.repairs_completed,
            "outage_until": self.outage_until,
        }

    def to_dict(self) -> dict[str, object]:
        return {
            "critical_downtime_min": self.critical_downtime,
            "hospitals_lost_power": self.hospitals_lost_power,
            "fuel_burned_l": self.fuel_burned_l,
            **self.physics(),
            "decision_points": self.decision_points,
            "actions_accepted": self.actions_accepted,
            "actions_rejected": dict(sorted(self.actions_rejected.items())),
        }
