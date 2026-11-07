"""The information layer: separates truth from what each role knows (docs/PRD.md section 8.4).

It sits between the engine and the contestants. For every role it keeps an observation stream:
telemetry in the role's scope (hospital and depot readings delayed under the delay modifier),
reports routed to it by channel, and replies to its ``confirm_report`` requests. Wake triggers
are computed from each role's own stream (blackout.info.wake), never from ground truth.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from blackout.info.confirm import check_claims
from blackout.info.observation import Observation, build_observation
from blackout.info.reports import ConfirmDue, ConfirmReply, Report, ReportView, view_of
from blackout.info.roles import check_role, receives
from blackout.info.telemetry import ReadingsHistory, Telemetry, telemetry
from blackout.info.wake import changes
from blackout.scenarios.schema import Scenario
from blackout.sim.actions import ConfirmReport, RejectReason
from blackout.sim.config import SimConfig
from blackout.sim.engine import Engine
from blackout.sim.triggers import Trigger, TriggerKind, TtfWatch


@dataclass(slots=True)
class _Stream:
    """What one role has been told, and what it saw last (the baseline for wake triggers)."""

    role: str
    inbox: list[ReportView] = field(default_factory=list)
    replies: list[ConfirmReply] = field(default_factory=list)
    delivered: set[str] = field(default_factory=set)
    pending: dict[str, int] = field(default_factory=dict)  # report ID -> requested at
    baseline: Observation | None = None
    ttf: TtfWatch | None = None


@dataclass(frozen=True, slots=True)
class ConfirmVerdict:
    reason: RejectReason | None
    message: str


class InfoLayer:
    def __init__(self, scenario: Scenario, config: SimConfig, roles: Sequence[str]) -> None:
        for role in roles:
            check_role(role)
        self.level = scenario.level
        self.config = config
        self.reports = {event.id: event for event in scenario.events if isinstance(event, Report)}
        self.history = ReadingsHistory(config.telemetry_delay_ticks)
        self.streams = {role: _Stream(role) for role in roles}
        self.confirms_used = 0

    @property
    def confirms_left(self) -> int:
        return max(0, self.config.confirm_limit - self.confirms_used)

    # --- observations ----------------------------------------------------------------------

    def telemetry(self, engine: Engine) -> Telemetry:
        return telemetry(engine, self.history.released(engine))

    def observe(self, role: str, engine: Engine, triggers: tuple[Trigger, ...] = ()) -> Observation:
        """``role``'s observation right now, with its unread reports and replies."""
        return self._observe(role, engine, self.telemetry(engine), triggers)

    def _observe(
        self, role: str, engine: Engine, seen: Telemetry, triggers: tuple[Trigger, ...]
    ) -> Observation:
        stream = self.streams[role]
        return build_observation(
            role,
            engine.tick,
            engine.horizon,
            engine.city,
            seen,
            triggers=triggers,
            reports=tuple(stream.inbox),
            confirmations=tuple(stream.replies),
            confirms_left=self.confirms_left,
            confirms_pending=tuple(sorted(stream.pending)),
        )

    def record(self, engine: Engine) -> None:
        """Take this tick's hospital and depot readings (called at every recorded tick)."""
        self.history.record(engine)

    def settle(self, engine: Engine) -> None:
        """Make what every role sees now the baseline that later changes are measured from."""
        seen = self.telemetry(engine)
        for role, stream in self.streams.items():
            stream.baseline = self._observe(role, engine, seen, ())

    def start_watches(self, engine: Engine) -> None:
        seen = self.telemetry(engine)
        for role, stream in self.streams.items():
            hospitals = self._observe(role, engine, seen, ()).hospitals
            stream.ttf = TtfWatch(
                self.config.ttf_thresholds, {h.id: h.ttf_minutes for h in hospitals}
            )

    def mark_read(self, roles: Sequence[str]) -> None:
        """``roles`` were shown their inbox at a decision point; it's no longer news."""
        for role in roles:
            stream = self.streams[role]
            stream.inbox.clear()
            stream.replies.clear()

    # --- each tick -------------------------------------------------------------------------

    def release(self, engine: Engine) -> dict[str, list[Trigger]]:
        """Deliver reports and confirm replies that fell due at this tick."""
        triggers: dict[str, list[Trigger]] = {role: [] for role in self.streams}
        for item in engine.released:
            if isinstance(item, Report):
                view = view_of(item, self.level)
                detail = f"{item.source_class} source on {item.channel}"
                for role, stream in self.streams.items():
                    if receives(role, item.channel):
                        stream.inbox.append(view)
                        stream.delivered.add(item.id)
                        triggers[role].append(Trigger(TriggerKind.REPORT, item.id, detail))
            elif isinstance(item, ConfirmDue):
                stream = self.streams[item.role]
                reply = check_claims(engine, self.reports[item.report_id], item.requested_at)
                stream.pending.pop(item.report_id, None)
                stream.replies.append(reply)
                trigger = Trigger(TriggerKind.CONFIRM_REPLY, item.report_id, reply.summary())
                triggers[item.role].append(trigger)
        return triggers

    def scan(self, engine: Engine) -> dict[str, list[Trigger]]:
        """What changed in each role's telemetry since its baseline; moves the baseline on."""
        seen = self.telemetry(engine)
        grace = self.config.trip_grace_minutes
        triggers: dict[str, list[Trigger]] = {}
        for role, stream in self.streams.items():
            now = self._observe(role, engine, seen, ())
            found = (
                [] if stream.baseline is None else changes(stream.baseline, now, trip_grace=grace)
            )
            if stream.ttf is not None:
                found += stream.ttf.update({h.id: h.ttf_minutes for h in now.hospitals})
            stream.baseline = now
            triggers[role] = found
        return triggers

    # --- confirm_report --------------------------------------------------------------------

    def confirm(self, role: str, action: ConfirmReport, engine: Engine) -> ConfirmVerdict:
        """Check a confirm request and, if it's accepted, schedule the reply."""
        stream = self.streams[role]
        if action.report_id not in stream.delivered:
            message = f"{role} hasn't received a report {action.report_id!r}"
            return ConfirmVerdict(RejectReason.UNKNOWN_REPORT, message)
        if self.confirms_left == 0:
            limit = self.config.confirm_limit
            return ConfirmVerdict(RejectReason.CONFIRM_LIMIT, f"all {limit} confirmations are used")
        self.confirms_used += 1
        due = engine.tick + self.config.confirm_delay_ticks
        engine.queue.push(due, ConfirmDue(action.report_id, role, engine.tick))
        stream.pending[action.report_id] = engine.tick
        left = self.confirms_left
        return ConfirmVerdict(
            None, f"verified dispatch replies at t={due}; {left} confirmations left"
        )


def merge(*parts: Mapping[str, list[Trigger]]) -> dict[str, list[Trigger]]:
    """Combine per-role trigger lists."""
    merged: dict[str, list[Trigger]] = {}
    for part in parts:
        for role, triggers in part.items():
            merged.setdefault(role, []).extend(triggers)
    return merged
