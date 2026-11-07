"""The stepper API every contestant drives (docs/PRD.md section 8.2).

    env = BlackoutEnv(scenario)
    request = env.reset()                    # first decision point, at tick 0
    result = env.act("all", action)          # applies immediately; the clock is paused
    request = env.advance()                  # runs minutes until the next decision point
    results, request = env.step({"all": [...]})   # act on everything, then advance

The clock only moves inside ``advance()``, so thinking time never costs simulated minutes.
Every contestant is woken by the same rules (invariant I6), evaluated on its own observation
stream (blackout.info). A run can have several roles (``BlackoutEnv(scenario, roles=...)``);
each decision point wakes only the roles with triggers of their own.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from blackout.controllers.base import Controller
from blackout.info.layer import InfoLayer, merge
from blackout.info.observation import Observation
from blackout.info.roles import ROLE_ALL
from blackout.scenarios.schema import Scenario
from blackout.sim.actions import (
    ActionResult,
    AnyAction,
    ConfirmReport,
    RestoreLoad,
    ShedLoad,
    vehicle_of,
)
from blackout.sim.city import edge_id
from blackout.sim.config import SimConfig
from blackout.sim.engine import Engine
from blackout.sim.hashing import chain_digest
from blackout.sim.routing import path_minutes
from blackout.sim.triggers import Trigger, TriggerKind, TtfWatch
from blackout.sim.validate import check

__all__ = ["ROLE_ALL", "BlackoutEnv", "DecisionRequest", "Done", "run_episode"]


@dataclass(frozen=True, slots=True)
class DecisionRequest:
    """A decision point: the simulation is paused and the woken roles may act.

    ``observations`` has one entry per woken role, each carrying that role's own triggers;
    ``triggers`` is all of them together.
    """

    index: int
    tick: int
    triggers: tuple[Trigger, ...]
    observations: Mapping[str, Observation]

    @property
    def roles(self) -> tuple[str, ...]:
        return tuple(self.observations)


@dataclass(frozen=True, slots=True)
class Done:
    """The run is over, at the horizon or early because nothing more can happen."""

    tick: int
    reason: str
    metrics: Mapping[str, object]


class BlackoutEnv:
    def __init__(
        self,
        scenario: Scenario,
        *,
        roles: Sequence[str] = (ROLE_ALL,),
        config: SimConfig | None = None,
        on_tick: Callable[[Engine], None] | None = None,
        wake_on_truth: bool = False,
    ) -> None:
        """``on_tick`` is called with the engine at every recorded tick (for logs and tests).

        ``roles`` are the contestant roles (blackout.info.roles), woken in this order. With
        ``wake_on_truth`` every role is woken by ground-truth signals instead of its own
        observations: that's only for the labeled reference contestant (PRD section 8.7).
        """
        if not roles or len(set(roles)) != len(roles):
            raise ValueError(f"roles must be distinct and not empty, got {tuple(roles)}")
        self.scenario = scenario
        self.config = config or SimConfig()
        self.roles: tuple[str, ...] = tuple(roles)
        self.wake_on_truth = wake_on_truth
        self._info = InfoLayer(scenario, self.config, self.roles)
        self._tick_listeners: list[Callable[[Engine], None]] = [] if on_tick is None else [on_tick]
        self._engine: Engine | None = None
        self._truth_ttf: TtfWatch | None = None
        self._pending: DecisionRequest | None = None
        self._done: Done | None = None
        self._moved: set[str] = set()
        self._tick_hashes: list[str] = []
        self._decisions = 0
        self._last_wake: dict[str, int] = dict.fromkeys(self.roles, 0)

    def add_tick_listener(self, listener: Callable[[Engine], None]) -> None:
        """Call ``listener`` with the engine at every recorded tick, after its hash is taken."""
        self._tick_listeners.append(listener)

    def remove_tick_listener(self, listener: Callable[[Engine], None]) -> None:
        self._tick_listeners.remove(listener)

    @property
    def engine(self) -> Engine:
        if self._engine is None:
            raise RuntimeError("call reset() first")
        return self._engine

    @property
    def info(self) -> InfoLayer:
        """The information layer: every role's observation stream."""
        return self._info

    @property
    def tick_hashes(self) -> tuple[str, ...]:
        """State hash at every recorded tick: after that tick's events, before decisions."""
        return tuple(self._tick_hashes)

    @property
    def done(self) -> Done | None:
        return self._done

    def reset(self) -> DecisionRequest:
        """Start (or restart) the run and return the first decision point, at tick 0."""
        engine = Engine(self.scenario, self.config)
        self._engine = engine
        self._info = InfoLayer(self.scenario, self.config, self.roles)
        self._done = None
        self._tick_hashes = []
        self._decisions = 0
        self._last_wake = dict.fromkeys(self.roles, 0)
        self._info.settle(engine)  # the starting state, before tick 0's events
        engine.apply_due_events()
        self._record_tick()
        found = self._triggers()
        self._info.start_watches(engine)
        self._truth_ttf = TtfWatch(self.config.ttf_thresholds, engine.observed_ttf())
        start = {role: [Trigger(TriggerKind.START)] for role in self.roles}
        return self._open_decision(merge(start, found))

    def observe(self, role: str = ROLE_ALL) -> Observation:
        """What ``role`` sees right now, including the effects of actions taken this turn."""
        self._check_role(role)
        pending = self._pending
        own = pending.observations.get(role) if pending is not None else None
        return self._info.observe(role, self.engine, () if own is None else own.triggers)

    def act(self, role: str, action: AnyAction) -> ActionResult:
        """Validate ``action`` and, if it's accepted, apply it immediately."""
        self._check_role(role)
        if self._pending is None:
            raise RuntimeError("no decision point is open: call reset() or advance()")
        engine = self.engine
        if isinstance(action, ConfirmReport):
            confirmed = self._info.confirm(role, action, engine)
            reason, message, route = confirmed.reason, confirmed.message, ()
        else:
            checked = check(engine, action, self._moved)
            reason, message, route = checked.reason, checked.message, checked.route
        if reason is not None:
            rejected = engine.metrics.actions_rejected
            rejected[reason.value] = rejected.get(reason.value, 0) + 1
            return ActionResult(action, False, message, reason=reason)

        engine.metrics.actions_accepted += 1
        if isinstance(action, ConfirmReport):
            return ActionResult(action, True, message)
        if isinstance(action, ShedLoad | RestoreLoad):
            message = self._switch(action)
            engine.drain_signals()  # the contestant just did this: it isn't news to wake it with
            return ActionResult(action, True, message)

        engine.apply(action, route)
        engine.drain_signals()
        vehicle_id = vehicle_of(action)
        self._moved.add(vehicle_id)
        vehicle = engine.vehicle(vehicle_id)
        assert vehicle is not None
        eta = path_minutes(engine.city, route)
        if vehicle.next_node is not None:
            road = engine.city.edges[edge_id(vehicle.at, vehicle.next_node)]
            eta += road.minutes - vehicle.elapsed
        if vehicle.next_node is None and len(route) == 1:
            message = f"already at {route[0]}; now {vehicle.state.value}"
        else:
            message = f"route {'>'.join(route)}, arriving in {eta} min"
        return ActionResult(action, True, message, route=route, eta_minutes=eta)

    def advance(self) -> DecisionRequest | Done:
        """Run minute by minute until the next decision point, or until the run is over."""
        if self._done is not None:
            return self._done
        if self._pending is None:
            raise RuntimeError("call reset() first")
        engine = self.engine
        self._info.mark_read(self._pending.roles)
        self._pending = None
        self._info.settle(engine)  # changes are measured from here, after this turn's actions
        while True:
            engine.run_minute()
            if engine.tick >= engine.horizon:
                self._record_tick()
                return self._finish("horizon")
            engine.apply_due_events()
            self._record_tick()
            found = self._triggers()
            if engine.is_stable():
                return self._finish("stable")
            for role in self.roles:
                if engine.tick - self._last_wake[role] >= self.config.heartbeat_ticks:
                    found.setdefault(role, []).append(Trigger(TriggerKind.HEARTBEAT))
            if any(found.values()):
                return self._open_decision(found)

    def step(
        self, actions_by_role: Mapping[str, Sequence[AnyAction]]
    ) -> tuple[list[ActionResult], DecisionRequest | Done]:
        """Act on every action (roles in their fixed order), then advance."""
        unknown = sorted(set(actions_by_role) - set(self.roles))
        if unknown:
            raise ValueError(f"unknown roles: {unknown}")
        results = [
            self.act(role, action)
            for role in self.roles
            for action in actions_by_role.get(role, ())
        ]
        return results, self.advance()

    # --- internals --------------------------------------------------------------------------

    def _check_role(self, role: str) -> None:
        if role not in self.roles:
            raise ValueError(f"unknown role {role!r}; this run has roles {self.roles}")

    def _triggers(self) -> dict[str, list[Trigger]]:
        """This tick's triggers for every role: reports and replies, plus what changed."""
        engine = self.engine
        released = self._info.release(engine)
        signals = engine.drain_signals()
        if not self.wake_on_truth:
            return merge(released, self._info.scan(engine))
        if self._truth_ttf is not None:
            signals += self._truth_ttf.update(engine.observed_ttf())
        return merge(released, {role: list(signals) for role in self.roles})

    def _switch(self, action: ShedLoad | RestoreLoad) -> str:
        """Shed or restore a zone, and describe what that did to the substation loads."""
        engine = self.engine
        zone = engine.zones[action.zone_id]
        before = zone.fed_by
        engine.switch(action)

        def load(substation_id: str) -> str:
            substation = engine.substations[substation_id]
            return f"{substation_id} at {substation.load_kw}/{substation.capacity_kw} kW"

        if isinstance(action, ShedLoad):
            relief = f"; {load(before)}" if before is not None else ""
            return f"{zone.id} shed ({zone.demand_kw} kW){relief}"
        if zone.fed_by is None:
            return f"{zone.id} reconnected, but no substation can feed it right now"
        return f"{zone.id} restored, fed by {load(zone.fed_by)}"

    def _record_tick(self) -> None:
        engine = self.engine
        self._tick_hashes.append(engine.state_hash())
        self._info.record(engine)
        for listener in self._tick_listeners:
            listener(engine)

    def _open_decision(self, found: Mapping[str, list[Trigger]]) -> DecisionRequest:
        engine = self.engine
        self._moved = set()
        engine.metrics.decision_points += 1
        observations: dict[str, Observation] = {}
        for role in self.roles:
            triggers = tuple(sorted(dict.fromkeys(found.get(role, ()))))
            if triggers:
                self._last_wake[role] = engine.tick
                observations[role] = self._info.observe(role, engine, triggers)
        merged = tuple(sorted({t for o in observations.values() for t in o.triggers}))
        self._pending = DecisionRequest(self._decisions, engine.tick, merged, observations)
        self._decisions += 1
        return self._pending

    def _finish(self, reason: str) -> Done:
        engine = self.engine
        engine.drain_signals()
        self._pending = None
        self._done = Done(engine.tick, reason, engine.report())
        return self._done


@dataclass(frozen=True, slots=True)
class DecisionRecord:
    index: int
    tick: int
    triggers: tuple[Trigger, ...]
    results: tuple[ActionResult, ...]


@dataclass(frozen=True, slots=True)
class EpisodeResult:
    done: Done
    decisions: tuple[DecisionRecord, ...]
    tick_hashes: tuple[str, ...]

    @property
    def run_hash(self) -> str:
        """One digest of every tick's state: equal runs have equal run hashes."""
        return chain_digest(self.tick_hashes)


def run_episode(
    env: BlackoutEnv,
    controller: Controller,
    *,
    on_decision: Callable[[DecisionRequest, tuple[ActionResult, ...]], None] | None = None,
) -> EpisodeResult:
    """Drive a one-role ``env`` with ``controller`` from reset to the end of the run."""
    if len(env.roles) != 1:
        raise ValueError(f"run_episode drives one role, but the env has roles {env.roles}")
    (role,) = env.roles
    controller.reset()
    request = env.reset()
    records: list[DecisionRecord] = []
    while isinstance(request, DecisionRequest):
        actions = controller.decide(request.observations[role])
        results = tuple(env.act(role, action) for action in actions)
        records.append(DecisionRecord(request.index, request.tick, request.triggers, results))
        if on_decision is not None:
            on_decision(request, results)
        request = env.advance()
    return EpisodeResult(request, tuple(records), env.tick_hashes)
