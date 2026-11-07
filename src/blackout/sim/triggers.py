"""Why a contestant is woken up (docs/PRD.md section 8.2).

Every trigger comes from something the contestant can observe: its own fleet's status, hospital
telemetry, or the heartbeat. Hidden events, such as a road being blocked, never wake anyone.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum


class TriggerKind(StrEnum):
    START = "start"
    HEARTBEAT = "heartbeat"
    VEHICLE_ARRIVED = "vehicle_arrived"
    VEHICLE_STUCK = "vehicle_stuck"
    VEHICLE_BROKEN = "vehicle_broken"
    VEHICLE_REPAIRED = "vehicle_repaired"
    TASK_COMPLETED = "task_completed"
    HOSPITAL_STATUS = "hospital_status"
    TTF_THRESHOLD = "ttf_threshold"


@dataclass(frozen=True, slots=True, order=True)
class Trigger:
    kind: TriggerKind
    subject: str = ""
    detail: str = ""

    def __str__(self) -> str:
        return f"{self.kind}({self.subject})" if self.subject else str(self.kind)


class TtfWatch:
    """Reports when a hospital's time-to-failure falls through one of the thresholds.

    Time-to-failure is computed from telemetry (fuel and burn rate). If it drops through several
    thresholds in one tick, one trigger is reported, for the lowest threshold crossed.
    """

    def __init__(self, thresholds: tuple[int, ...], current: Mapping[str, int | None]) -> None:
        self._thresholds = tuple(sorted(thresholds))
        self._last = dict(current)

    def update(self, current: Mapping[str, int | None]) -> list[Trigger]:
        triggers: list[Trigger] = []
        for hospital in sorted(current):
            now, before = current[hospital], self._last.get(hospital)
            self._last[hospital] = now
            if now is None:
                continue
            crossed = [k for k in self._thresholds if now <= k and (before is None or before > k)]
            if crossed:
                detail = f"{now} min of fuel left (crossed {crossed[0]})"
                triggers.append(Trigger(TriggerKind.TTF_THRESHOLD, hospital, detail))
        return triggers
