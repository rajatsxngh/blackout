"""The stepper API every contestant drives (docs/PRD.md section 8.2).

    env = BlackoutEnv(scenario)
    request = env.reset()                    # first decision point, at tick 0
    result = env.act("all", action)          # applies immediately; the clock is paused
    request = env.advance()                  # runs minutes until the next decision point
    results, request = env.step({"all": [...]})   # act on everything, then advance

The clock only moves inside ``advance()``, so thinking time never costs simulated minutes.
Every contestant is woken by the same rules (invariant I6), evaluated on what it can observe.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from blackout.controllers.base import Controller
from blackout.info.observation import Observation, full_view
from blackout.scenarios.schema import Scenario
from blackout.sim.actions import ActionResult, AnyAction, RestoreLoad, ShedLoad, vehicle_of
from blackout.sim.city import edge_id
from blackout.sim.config import SimConfig
from blackout.sim.engine import Engine
from blackout.sim.hashing import chain_digest
from blackout.sim.routing import path_minutes
from blackout.sim.triggers import Trigger, TriggerKind, TtfWatch
from blackout.sim.validate import check

ROLE_ALL = "all"


@dataclass(frozen=True, slots=True)
class DecisionRequest:
    """A decision point: the simulation is paused and these roles may act."""

    index: int
    tick: int
    triggers: tuple[Trigger, ...]
    observations: Mapping[str, Observation]


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
        config: SimConfig | None = None,
        on_tick: Callable[[Engine], None] | None = None,
    ) -> None:
        """``on_tick`` is called with the engine at every recorded tick (for logs and tests)."""
        self.scenario = scenario
        self.config = config or SimConfig()
        self.roles: tuple[str, ...] = (ROLE_ALL,)
        self._on_tick = on_tick
        self._engine: Engine | None = None
        self._ttf: TtfWatch | None = None
        self._pending: DecisionRequest | None = None
        self._done: Done | None = None
        self._moved: set[str] = set()
        self._tick_hashes: list[str] = []
        self._decisions = 0
        self._last_decision_tick = 0

    @property
    def engine(self) -> Engine:
        if self._engine is None:
            raise RuntimeError("call reset() first")
        return self._engine

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
        self._done = None
        self._tick_hashes = []
        self._decisions = 0
        engine.apply_due_events()
        self._record_tick()
        self._ttf = TtfWatch(self.config.ttf_thresholds, engine.observed_ttf())
        return self._open_decision([Trigger(TriggerKind.START), *engine.drain_signals()])

    def observe(self, role: str = ROLE_ALL) -> Observation:
        """What ``role`` sees right now, including the effects of actions taken this turn."""
        self._check_role(role)
        triggers = self._pending.triggers if self._pending is not None else ()
        return full_view(self.engine, role, triggers)

    def act(self, role: str, action: AnyAction) -> ActionResult:
        """Validate ``action`` and, if it's accepted, apply it immediately."""
        self._check_role(role)
        if self._pending is None:
            raise RuntimeError("no decision point is open: call reset() or advance()")
        engine = self.engine
        verdict = check(engine, action, self._moved)
        if verdict.reason is not None:
            rejected = engine.metrics.actions_rejected
            rejected[verdict.reason.value] = rejected.get(verdict.reason.value, 0) + 1
            return ActionResult(action, False, verdict.message, reason=verdict.reason)

        engine.metrics.actions_accepted += 1
        if isinstance(action, ShedLoad | RestoreLoad):
            message = self._switch(action)
            engine.drain_signals()  # the contestant just did this: it isn't news to wake it with
            return ActionResult(action, True, message)

        engine.apply(action, verdict.route)
        engine.drain_signals()
        vehicle_id = vehicle_of(action)
        self._moved.add(vehicle_id)
        vehicle = engine.vehicle(vehicle_id)
        assert vehicle is not None
        eta = path_minutes(engine.city, verdict.route)
        if vehicle.next_node is not None:
            road = engine.city.edges[edge_id(vehicle.at, vehicle.next_node)]
            eta += road.minutes - vehicle.elapsed
        if vehicle.next_node is None and len(verdict.route) == 1:
            message = f"already at {verdict.route[0]}; now {vehicle.state.value}"
        else:
            message = f"route {'>'.join(verdict.route)}, arriving in {eta} min"
        return ActionResult(action, True, message, route=verdict.route, eta_minutes=eta)

    def advance(self) -> DecisionRequest | Done:
        """Run minute by minute until the next decision point, or until the run is over."""
        if self._done is not None:
            return self._done
        if self._pending is None:
            raise RuntimeError("call reset() first")
        assert self._ttf is not None
        engine = self.engine
        self._pending = None
        while True:
            engine.run_minute()
            if engine.tick >= engine.horizon:
                self._record_tick()
                return self._finish("horizon")
            engine.apply_due_events()
            self._record_tick()
            triggers = [*engine.drain_signals(), *self._ttf.update(engine.observed_ttf())]
            if engine.is_stable():
                return self._finish("stable")
            if engine.tick - self._last_decision_tick >= self.config.heartbeat_ticks:
                triggers.append(Trigger(TriggerKind.HEARTBEAT))
            if triggers:
                return self._open_decision(triggers)

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
        if self._on_tick is not None:
            self._on_tick(engine)

    def _open_decision(self, triggers: list[Trigger]) -> DecisionRequest:
        engine = self.engine
        merged = tuple(sorted(dict.fromkeys(triggers)))
        self._moved = set()
        self._last_decision_tick = engine.tick
        engine.metrics.decision_points += 1
        observations = {role: full_view(engine, role, merged) for role in self.roles}
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
    """Drive ``env`` with ``controller`` from reset to the end of the run."""
    controller.reset()
    request = env.reset()
    records: list[DecisionRecord] = []
    while isinstance(request, DecisionRequest):
        actions = controller.decide(request.observations[ROLE_ALL])
        results = tuple(env.act(ROLE_ALL, action) for action in actions)
        records.append(DecisionRecord(request.index, request.tick, request.triggers, results))
        if on_decision is not None:
            on_decision(request, results)
        request = env.advance()
    return EpisodeResult(request, tuple(records), env.tick_hashes)
