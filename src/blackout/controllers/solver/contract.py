"""The solver's library contract: ``solve_plan(belief, overrides) -> SolverResult``.

Frozen in M7 (docs/PRD.md section 8.7). C2 Solver, C3 Reference, C4 Parser + Solver and the
LLM agents' ``call_solver`` tool all plan through it, so they differ only in the belief they
hand in. One call:

1. snapshots the belief with the overrides laid over it (``planning_state``);
2. plans power first: the best shed set now, the crew orders, and which hospitals have grid
   power in each bucket of the next 120 minutes (``power_timeline``);
3. plans fuel with CP-SAT over every truck's enumerated options (``solve_fuel``);
4. turns the plan into actions, committing only what changes now: shed and restore switches,
   crews sent to their first substation, and each truck's *first* trip (rolling horizon). A
   vehicle already doing what the plan wants, on a route clear of believed blocks, gets no
   action, so re-planning never makes it thrash.

The same belief and overrides always give the same result (one CP-SAT worker, a fixed seed,
a deterministic time limit, hints from ``previous``); only ``stats.wall_time_s`` differs, and
it's excluded from comparisons and from ``plan_id``.
"""

from dataclasses import dataclass, field

from blackout.controllers.solver.fuel import (
    BUCKET_STARTS,
    DETERMINISTIC_TIME_S,
    ChosenTrip,
    FuelPlan,
    solve_fuel,
)
from blackout.controllers.solver.power import PowerTimeline, power_timeline
from blackout.controllers.solver.state import (
    PlanningState,
    SolverBeliefs,
    TruckStart,
    planning_state,
)
from blackout.info.belief import BeliefState
from blackout.info.observation import plain
from blackout.sim.actions import (
    REASON_MAX_CHARS,
    AnyAction,
    AssignCrew,
    DispatchTruck,
    RestoreLoad,
    ShedLoad,
)
from blackout.sim.city import NodeId
from blackout.sim.config import SimConfig
from blackout.sim.entities import TaskKind, VehicleState
from blackout.sim.hashing import digest

__all__ = [
    "CrewPlan",
    "PlannedTrip",
    "PowerPlan",
    "SolveStats",
    "SolverBeliefs",
    "SolverResult",
    "TruckPlan",
    "solve_plan",
]


@dataclass(frozen=True, slots=True)
class PlannedTrip:
    """One trip of a truck's plan. Ticks are absolute; ``depot`` None means a direct delivery
    of fuel already aboard."""

    depot: str | None
    hospital: str
    pickup_l: int
    deliver_l: int
    load_eta: int | None
    deliver_eta: int
    route: tuple[NodeId, ...]


@dataclass(frozen=True, slots=True)
class TruckPlan:
    """A truck's planned trips (none: idle, or carrying on). ``changed`` says whether the
    proposed actions dispatch it now."""

    truck_id: str
    trips: tuple[PlannedTrip, ...]
    changed: bool


@dataclass(frozen=True, slots=True)
class CrewPlan:
    crew_id: str
    substations: tuple[str, ...]
    online_eta: tuple[int, ...]  # absolute tick each comes back online


@dataclass(frozen=True, slots=True)
class PowerPlan:
    """``shed`` is the set of zones that should be shed now. ``restore_eta`` is each
    substation's planned return (absolute tick; None if no crew can get to it), and
    ``powered`` each hospital's grid power in each bucket of the horizon; ``bucket_starts``
    gives every bucket's first tick."""

    shed: tuple[str, ...]
    crews: tuple[CrewPlan, ...]
    restore_eta: dict[str, int | None]
    powered: dict[str, tuple[bool, ...]]
    bucket_starts: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class SolveStats:
    """How the fuel model went. ``wall_time_s`` is for solve-time reporting only: it differs
    from run to run, so it's left out of equality and of the plan ID."""

    status: str  # optimal, feasible or greedy (the time-boxed fallback)
    objective: int
    deterministic_time: float
    options: int  # truck options enumerated, over all trucks
    wall_time_s: float = field(default=0.0, compare=False)


@dataclass(frozen=True, slots=True)
class SolverResult:
    """A complete plan, and the actions that start carrying it out now.

    ``dark_minutes`` is the predicted hospital-minutes dark per hospital over the horizon
    (exact to the minute for the first 30 minutes, then rounded toward caution).
    """

    plan_id: str
    tick: int
    trucks: tuple[TruckPlan, ...]
    dark_minutes: dict[str, int]
    power: PowerPlan
    actions: tuple[AnyAction, ...]
    stats: SolveStats

    def truck(self, truck_id: str) -> TruckPlan:
        return next(t for t in self.trucks if t.truck_id == truck_id)

    def render(self) -> str:
        """A compact text version for LLM tools: about 400 tokens at most on the default map."""
        lines = [f"plan {self.plan_id} at t={self.tick} ({self.stats.status}), next 120 min"]
        for t in self.trucks:
            if not t.trips:
                lines.append(f"{t.truck_id}: idle / carries on")
                continue
            legs = []
            for trip in t.trips:
                via = f"load {trip.depot} t{trip.load_eta} > " if trip.depot else ""
                legs.append(f"{via}{trip.hospital} {trip.deliver_l}L t{trip.deliver_eta}")
            lines.append(f"{t.truck_id}: {'; then '.join(legs)}")
        dark = ", ".join(f"{h} {m}" for h, m in sorted(self.dark_minutes.items()))
        lines.append(f"predicted dark min: {dark or 'n/a'}")
        shed = ", ".join(self.power.shed) or "none"
        etas = ", ".join(
            f"{s} {'now' if eta == self.tick else ('never' if eta is None else f't{eta}')}"
            for s, eta in sorted(self.power.restore_eta.items())
        )
        lines.append(f"shed: {shed}; substations online: {etas}")
        for c in self.power.crews:
            if c.substations:
                lines.append(f"{c.crew_id}: " + " > ".join(c.substations))
        lines.append("actions: " + ("; ".join(_describe(a) for a in self.actions) or "none"))
        return "\n".join(lines)


def solve_plan(
    belief: BeliefState,
    overrides: SolverBeliefs | None = None,
    *,
    config: SimConfig | None = None,
    previous: SolverResult | None = None,
    deterministic_time: float = DETERMINISTIC_TIME_S,
) -> SolverResult:
    """Plan power and fuel from ``belief`` and propose the actions that start the plan.

    ``belief`` is the caller's own BeliefState, updated from its observations (the reference
    contestant alone passes one built from ground truth). ``overrides`` lays the caller's
    corrections over it. ``previous`` is the caller's last result: its truck plans hint the
    model, which speeds it up and keeps plans stable. ``config`` must be the run's SimConfig
    (loading and unloading times, priority weights).

    Raises ValueError if the belief has never observed anything (it has no map).
    """
    rules = config or SimConfig()
    state = planning_state(belief, overrides or SolverBeliefs(), rules)
    power = power_timeline(state, BUCKET_STARTS)
    hint = None
    if previous is not None:
        hint = {t.truck_id: tuple((p.depot, p.hospital) for p in t.trips) for t in previous.trucks}
    fuel = solve_fuel(state, power.powered, hint, deterministic_time)
    return _result(state, power, fuel)


def _result(state: PlanningState, power: PowerTimeline, fuel: FuelPlan) -> SolverResult:
    now = state.tick
    avoid = tuple(sorted(e for e in state.blocked if e in state.city.edges))
    actions: list[AnyAction] = []
    current = frozenset(z.id for z in state.zones if z.shed)
    for z in sorted(power.shed_now - current):
        actions.append(ShedLoad(zone_id=z, reason=_reason(f"plan: shed {z} to keep feeders <=95%")))
    for z in sorted(current - power.shed_now):
        actions.append(RestoreLoad(zone_id=z, reason=_reason(f"plan: {z} fits again, restore")))

    crews = {c.id: c for c in state.crews}
    crew_plans = []
    for order in power.crews:
        crew_plans.append(
            CrewPlan(order.crew_id, order.substations, tuple(now + m for m in order.online_at))
        )
        if not order.substations:
            continue
        crew, site = crews[order.crew_id], order.substations[0]
        view = crew.view
        if view.state is VehicleState.REPAIRING:
            continue
        if view.substation == site and view.state is VehicleState.MOVING and crew.route_ok:
            continue
        why = f"plan: {site} back at t{now + order.online_at[0]}"
        actions.append(
            AssignCrew(crew_id=crew.id, substation_id=site, avoid_edges=avoid, reason=_reason(why))
        )

    trucks: list[TruckPlan] = []
    for t in state.trucks:
        chosen = fuel.trips.get(t.id, ())
        action = _truck_action(t, chosen, avoid, now)
        if action is not None:
            actions.append(action)
        trips = tuple(
            PlannedTrip(
                depot=c.leg.depot,
                hospital=c.leg.hospital,
                pickup_l=c.pickup_l,
                deliver_l=c.deliver_l,
                load_eta=None if c.leg.loaded_at is None else now + c.leg.loaded_at,
                deliver_eta=now + c.leg.delivered_at,
                route=c.leg.route,
            )
            for c in chosen
        )
        trucks.append(TruckPlan(t.id, trips, action is not None))

    plan = PowerPlan(
        shed=tuple(sorted(power.shed_now)),
        crews=tuple(crew_plans),
        restore_eta={s: None if m is None else now + m for s, m in sorted(power.online_at.items())},
        powered=dict(sorted(power.powered.items())),
        bucket_starts=tuple(now + start for start in BUCKET_STARTS),
    )
    stats = SolveStats(
        fuel.status, fuel.objective, fuel.deterministic_time, fuel.options, fuel.wall_time
    )
    body = {
        "tick": now,
        "trucks": plain(trucks),
        "dark": dict(sorted(fuel.dark.items())),
        "power": plain(plan),
        "actions": [a.model_dump(mode="json") for a in actions],
    }
    return SolverResult(
        plan_id=digest(body)[:12],
        tick=now,
        trucks=tuple(trucks),
        dark_minutes=dict(sorted(fuel.dark.items())),
        power=plan,
        actions=tuple(actions),
        stats=stats,
    )


def _truck_action(
    truck: TruckStart, chosen: tuple[ChosenTrip, ...], avoid: tuple[str, ...], now: int
) -> DispatchTruck | None:
    """The dispatch that starts the truck's first planned trip, if it isn't on it already."""
    view = truck.view
    if not chosen or chosen[0].deliver_l == 0 or not truck.available:
        return None
    if view.state in (VehicleState.LOADING, VehicleState.UNLOADING):
        return None  # busy; it's re-planned when it's done
    first = chosen[0]
    leg, task = first.leg, view.task
    keeping = view.state is VehicleState.MOVING and truck.route_ok and task is not None
    doing = (task.kind, task.target_id) if keeping and task is not None else None
    if leg.depot is not None:
        if doing == (TaskKind.LOAD, leg.depot):
            return None
        why = f"plan: load {leg.depot} for {leg.hospital}, lands t{now + leg.delivered_at}"
        return DispatchTruck(
            truck_id=view.id,
            task=TaskKind.LOAD,
            target_id=leg.depot,  # fill up: the engine takes what the depot has
            avoid_edges=avoid,
            reason=_reason(why),
        )
    if doing == (TaskKind.DELIVER, leg.hospital):
        return None
    # Deliver all that fits, unless the plan keeps the rest for a second hospital.
    split = len(chosen) > 1 and chosen[1].leg.depot is None
    amount = first.deliver_l if split and 0 < first.deliver_l < view.load_l else None
    why = f"plan: {first.deliver_l} L to {leg.hospital}, lands t{now + leg.delivered_at}"
    return DispatchTruck(
        truck_id=view.id,
        task=TaskKind.DELIVER,
        target_id=leg.hospital,
        amount_l=amount,
        avoid_edges=avoid,
        reason=_reason(why),
    )


def _reason(text: str) -> str:
    return text[:REASON_MAX_CHARS]


def _describe(action: AnyAction) -> str:
    if isinstance(action, DispatchTruck):
        return f"{action.truck_id} {action.task.value} {action.target_id}"
    if isinstance(action, AssignCrew):
        return f"{action.crew_id} to {action.substation_id}"
    if isinstance(action, ShedLoad):
        return f"shed {action.zone_id}"
    if isinstance(action, RestoreLoad):
        return f"restore {action.zone_id}"
    return action.kind
