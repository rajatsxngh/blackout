"""The fuel plan: a CP-SAT model over enumerated truck plans (PRD section 8.7, D002).

**Options.** Each truck gets at most 91: idle, or one or two trips, each trip being
{direct, via each depot} -> a hospital. Times come from believed shortest paths, so a trip's
pickup and delivery minutes are constants. A trip that can't finish delivering within the
120-minute horizon is dropped. A truck already driving a trip may not pick "idle" (it will
keep driving whatever we say); a loading truck must deliver straight from its depot; a broken
or unavailable truck can only be idle.

**Time.** 120 minutes in buckets: 1-minute buckets for the first 30 minutes, then 5-minute
buckets (D063: plain 5-minute buckets can't tell a delivery at minute 16 from one at minute
20, which is the difference between a hospital staying lit and going dark). Fuel delivered
at minute t counts from the first bucket starting at or after t, so the model never assumes
fuel sooner than it can burn it.

**Variables.** ``x[v, p]`` (one plan per truck), pickup and delivery liters per trip,
``fuel[h, b]`` in [0, tank] at the end of each bucket, and ``dark[h, b]`` in [0, len_b]
minutes. Fuel balance: ``fuel[h,b] = fuel[h,b-1] + delivered - burn*len_b*unpowered +
burn*dark``, with ``dark <= len_b*unpowered``.

**Objective** (all integers; ``t_b`` is the bucket's start minute, H = 120):
``1000*w_h*(2H - t_b)`` per dark minute (darkness earlier in the horizon costs more, so the
model can't "save" fuel by going dark), plus ``1200*w_h`` per liter short of a 60-minute
reserve at the end of the horizon (for hospitals still off the grid then), plus ``H`` per
minute of driving and per minute until each delivery (earlier is better), plus a small
anti-thrash penalty for changing the trip a truck is on, minus 1 per liter delivered (the
unloading takes 5 minutes whatever the amount, so a delivery should be as big as fits).

**Determinism.** One worker, ``random_seed=0``, a deterministic time limit, never a wall-clock
limit. Variables are created in sorted-ID order. The hint is complete: the previous plan where
it still applies (else the greedy plan), filling up at depots and delivering all aboard.
If the model finds no solution in time, a greedy earliest-deadline plan is used instead.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from ortools.sat.python import cp_model

from blackout.controllers.solver.state import PlanningState, TruckStart
from blackout.sim.city import NodeId
from blackout.sim.routing import path_minutes

HORIZON_MINUTES = 120
FINE_MINUTES = 30  # 1-minute buckets up to here, then 5-minute buckets
BUCKET_MINUTES = 5
BUCKET_STARTS: tuple[int, ...] = (
    *range(FINE_MINUTES),
    *range(FINE_MINUTES, HORIZON_MINUTES, BUCKET_MINUTES),
)
"""Start minute of every bucket (48 of them)."""
BUCKET_LENGTHS: tuple[int, ...] = tuple(
    end - start
    for start, end in zip(BUCKET_STARTS, (*BUCKET_STARTS[1:], HORIZON_MINUTES), strict=True)
)
RESERVE_MINUTES = 60
DARK_UNIT = 1000
RESERVE_UNIT = 10 * HORIZON_MINUTES  # per liter short, times the hospital's weight
MINUTE_UNIT = HORIZON_MINUTES  # per minute of driving or of waiting for a delivery
THRASH_MINUTES = 30  # changing a truck's current trip costs as much as 30 minutes of driving
DETERMINISTIC_TIME_S = 1.0

type TripKey = tuple[str | None, str]  # (depot or None for direct, hospital)


@dataclass(frozen=True, slots=True)
class Leg:
    """One trip: optionally load at ``depot``, then deliver to ``hospital``.

    Minutes count from now. ``route`` is the whole drive: start -> depot -> hospital.
    """

    depot: str | None
    hospital: str
    loaded_at: int | None
    delivered_at: int
    drive_minutes: int
    route: tuple[NodeId, ...]

    @property
    def key(self) -> TripKey:
        return (self.depot, self.hospital)


@dataclass(frozen=True, slots=True)
class TruckOption:
    truck_id: str
    legs: tuple[Leg, ...]  # empty: idle

    @property
    def key(self) -> tuple[TripKey, ...]:
        return tuple(leg.key for leg in self.legs)

    @property
    def cost(self) -> int:
        """Driving minutes plus minutes until each delivery (the tie-break toward early)."""
        return sum(leg.drive_minutes + leg.delivered_at for leg in self.legs)


@dataclass(frozen=True, slots=True)
class ChosenTrip:
    leg: Leg
    pickup_l: int
    deliver_l: int


@dataclass(frozen=True, slots=True)
class FuelPlan:
    status: str  # "optimal", "feasible" or "greedy"
    trips: dict[str, tuple[ChosenTrip, ...]]
    dark: dict[str, int]  # hospital -> predicted dark minutes within the horizon
    fuel_end: dict[str, int]
    objective: int
    deterministic_time: float
    wall_time: float  # seconds; for solve-time reporting only, never for decisions
    options: int


def bucket_of(minute: int) -> int:
    """The first bucket whose burn a delivery at ``minute`` can cover (len(BUCKET_STARTS) if
    it lands after the last bucket starts: it then only counts toward the end reserve)."""
    later = (b for b, start in enumerate(BUCKET_STARTS) if start >= minute)
    return next(later, len(BUCKET_STARTS))


def needy(state: PlanningState, powered: dict[str, tuple[bool, ...]]) -> frozenset[str]:
    """Hospitals that may need fuel: off the grid in at least one bucket of the horizon.
    Fuel for a hospital on the grid throughout can't change the objective, so trips there
    aren't enumerated (it keeps the model small and the solve fast)."""
    return frozenset(h.id for h in state.hospitals if not all(powered.get(h.id, (False,))))


def truck_options(
    state: PlanningState, truck: TruckStart, targets: frozenset[str] | None = None
) -> list[TruckOption]:
    """Every plan the model may choose for ``truck``, idle first, in a fixed order.
    ``targets`` limits the hospitals trips may go to (default: all of them)."""
    idle = TruckOption(truck.id, ())
    if not truck.available:
        return [idle]
    config = state.config
    hospitals = [h for h in state.hospitals if targets is None or h.id in targets]
    depots = [d for d in state.depots if d.stock_l > 0]

    def legs_from(
        node: NodeId, minute: int, first: bool, has_fuel: bool, room: bool, after: str | None
    ) -> list[Leg]:
        legs: list[Leg] = []
        targets = hospitals
        if first and truck.pin is not None:
            targets = [h for h in hospitals if h.id == truck.pin]
        sources: list[str | None] = []
        if has_fuel:
            sources.append(None)
        if room and not (first and truck.loading_at is not None):
            sources += [d.id for d in depots]
        for source in sources:
            for h in targets:
                if source is None and h.id == after:
                    continue  # leftovers to the hospital just served: nothing to gain
                if source is None:
                    path = state.route(node, h.node)
                    if path is None:
                        continue
                    drive = _minutes(state, path)
                    done = minute + drive + config.unload_minutes
                    leg = Leg(None, h.id, None, done, drive, path)
                else:
                    depot = state.depot(source)
                    there, onward = state.route(node, depot.node), state.route(depot.node, h.node)
                    if there is None or onward is None:
                        continue
                    loaded = minute + _minutes(state, there) + config.load_minutes
                    drive = _minutes(state, there) + _minutes(state, onward)
                    done = loaded + _minutes(state, onward) + config.unload_minutes
                    leg = Leg(source, h.id, loaded, done, drive, (*there, *onward[1:]))
                if leg.delivered_at <= HORIZON_MINUTES:
                    legs.append(leg)
        return legs

    full = truck.load_l >= truck.capacity_l
    firsts = legs_from(truck.node, truck.lead, True, truck.load_l > 0, not full, None)
    options = [] if truck.current is not None else [idle]
    for first in firsts:
        options.append(TruckOption(truck.id, (first,)))
    for first in firsts:
        h = state.hospital(first.hospital)
        for second in legs_from(h.node, first.delivered_at, False, True, True, h.id):
            options.append(TruckOption(truck.id, (first, second)))
    return options or [idle]


def _minutes(state: PlanningState, path: Sequence[NodeId]) -> int:
    return path_minutes(state.city, path)


def solve_fuel(
    state: PlanningState,
    powered: dict[str, tuple[bool, ...]],
    hint: dict[str, tuple[TripKey, ...]] | None = None,
    deterministic_time: float = DETERMINISTIC_TIME_S,
) -> FuelPlan:
    """Choose every truck's plan. ``powered`` is the power plan's bucket timeline."""
    targets = needy(state, powered)
    options = {t.id: truck_options(state, t, targets) for t in state.trucks}
    trucks = {t.id: t for t in state.trucks}
    model = cp_model.CpModel()
    x: dict[tuple[str, int], cp_model.IntVar] = {}
    pick: dict[tuple[str, int, int], cp_model.IntVar] = {}
    dlv: dict[tuple[str, int, int], cp_model.IntVar] = {}
    arrivals: dict[tuple[str, int], list[cp_model.IntVar]] = {}
    from_depot: dict[str, list[cp_model.IntVar]] = {d.id: [] for d in state.depots}
    cost: list[cp_model.LinearExprT] = []

    for v in sorted(options):
        truck = trucks[v]
        cap = truck.capacity_l
        chosen = []
        for p, option in enumerate(options[v]):
            on = model.new_bool_var(f"x_{v}_{p}")
            x[v, p] = on
            chosen.append(on)
            plan_cost = MINUTE_UNIT * option.cost
            if truck.current is not None and (not option.legs or _changes(truck, option)):
                plan_cost += MINUTE_UNIT * THRASH_MINUTES
            if plan_cost:
                cost.append(plan_cost * on)
            aboard = cp_model.LinearExpr.term(on, truck.load_l)  # liters aboard, as we go
            for k, leg in enumerate(option.legs):
                if leg.depot is not None:
                    got = model.new_int_var(0, cap, f"pick_{v}_{p}_{k}")
                    model.add(got <= cap * on)
                    model.add(aboard + got <= cap)
                    pick[v, p, k] = got
                    from_depot[leg.depot].append(got)
                    aboard = aboard + got
                given = model.new_int_var(0, cap, f"dlv_{v}_{p}_{k}")
                model.add(given <= cap * on)
                model.add(given <= aboard)
                dlv[v, p, k] = given
                cost.append(-1 * given)  # unloading takes 5 minutes anyway: deliver what fits
                aboard = aboard - given
                arrivals.setdefault((leg.hospital, bucket_of(leg.delivered_at)), []).append(given)
        model.add_exactly_one(chosen)
        if truck.fixed is not None:
            fixed = truck.fixed
            landed = model.new_int_var(0, fixed.liters, f"fixed_{v}")
            arrivals.setdefault((fixed.hospital, bucket_of(fixed.minute)), []).append(landed)
            cost.append(-1 * landed)  # take all of it unless the tank can't

    for depot in state.depots:
        if from_depot[depot.id]:
            model.add(sum(from_depot[depot.id]) <= depot.stock_l)

    dark: dict[tuple[str, int], cp_model.IntVar] = {}
    fuel_end: dict[str, cp_model.IntVar] = {}
    for h in state.hospitals:
        level = cp_model.LinearExpr.constant(h.fuel_l)  # liters at the end of the last bucket
        timeline = powered.get(h.id, (False,) * len(BUCKET_STARTS))
        for b in range(len(BUCKET_STARTS) + 1):
            landed = arrivals.get((h.id, b), [])
            if landed:
                level = level + cp_model.LinearExpr.sum(landed)
                model.add(level <= h.tank_l)
            if b == len(BUCKET_STARTS):
                break
            minutes = BUCKET_LENGTHS[b] * (0 if timeline[b] else 1)  # minutes off the grid
            gone = model.new_int_var(0, minutes, f"dark_{h.id}_{b}")
            dark[h.id, b] = gone
            after = model.new_int_var(0, h.tank_l, f"fuel_{h.id}_{b}")
            model.add(after == level - h.burn_lpm * minutes + h.burn_lpm * gone)
            level = cp_model.LinearExpr.term(after, 1)
            weight = DARK_UNIT * h.weight * (2 * HORIZON_MINUTES - BUCKET_STARTS[b])
            cost.append(weight * gone)
        end = model.new_int_var(0, h.tank_l, f"fuel_{h.id}_end")
        model.add(end == level)
        fuel_end[h.id] = end
        if not timeline[-1]:
            reserve = min(h.tank_l, h.burn_lpm * RESERVE_MINUTES)
            short = model.new_int_var(0, reserve, f"short_{h.id}")
            model.add(short >= reserve - end)
            cost.append(RESERVE_UNIT * h.weight * short)
    model.minimize(sum(cost))

    # A complete hint: the previous plan where it still applies, else the greedy plan; the
    # hinted option fills up at depots and delivers everything aboard, the others are zero.
    greedy = greedy_fuel(state, powered, options).trips
    for v in sorted(options):
        previous = (hint or {}).get(v)
        if previous is None:
            previous = tuple(trip.leg.key for trip in greedy.get(v, ()))
        wanted = _hinted(options[v], previous)
        for p, option in enumerate(options[v]):
            model.add_hint(x[v, p], 1 if p == wanted else 0)
            aboard = trucks[v].load_l
            for k in range(len(option.legs)):
                got = trucks[v].capacity_l - aboard if p == wanted else 0
                if (v, p, k) in pick:
                    model.add_hint(pick[v, p, k], got)
                    aboard += got
                given = aboard if p == wanted else 0
                model.add_hint(dlv[v, p, k], given)
                aboard -= given

    solver = cp_model.CpSolver()
    solver.parameters.num_workers = 1
    solver.parameters.random_seed = 0
    solver.parameters.max_deterministic_time = deterministic_time
    # Measured on the slowest dev-v0 models (D063): presolve costs more than it saves at this
    # size, and the stronger LP relaxation pays for itself. Both are deterministic.
    solver.parameters.cp_model_presolve = False
    solver.parameters.linearization_level = 2
    status = solver.solve(model)
    count = sum(len(o) for o in options.values())
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return greedy_fuel(state, powered, options, solver.wall_time)

    trips: dict[str, tuple[ChosenTrip, ...]] = {}
    for v in sorted(options):
        p = next(p for p in range(len(options[v])) if solver.value(x[v, p]))
        trips[v] = tuple(
            ChosenTrip(
                leg,
                solver.value(pick[v, p, k]) if (v, p, k) in pick else 0,
                solver.value(dlv[v, p, k]),
            )
            for k, leg in enumerate(options[v][p].legs)
        )
    return FuelPlan(
        status="optimal" if status == cp_model.OPTIMAL else "feasible",
        trips=trips,
        dark={
            h.id: sum(solver.value(dark[h.id, b]) for b in range(len(BUCKET_STARTS)))
            for h in state.hospitals
        },
        fuel_end={h.id: solver.value(fuel_end[h.id]) for h in state.hospitals},
        objective=round(solver.objective_value),
        deterministic_time=solver.response_proto.deterministic_time,
        wall_time=solver.wall_time,
        options=count,
    )


def _changes(truck: TruckStart, option: TruckOption) -> bool:
    """Whether ``option`` takes the truck off the trip it's on now."""
    assert truck.current is not None
    depot, hospital = truck.current
    first = option.legs[0]
    if first.depot != depot:
        return True
    return hospital is not None and first.hospital != hospital


def _hinted(options: Sequence[TruckOption], previous: tuple[TripKey, ...] | None) -> int:
    """Which option continues the previous plan, if any (0, idle, otherwise)."""
    if not previous:
        return 0
    keys = [o.key for o in options]
    hospital, rest = previous[0][1], previous[1:]
    for wanted in (previous, ((None, hospital), *rest), rest):
        if wanted and wanted in keys:
            return keys.index(wanted)
    return 0


def greedy_fuel(
    state: PlanningState,
    powered: dict[str, tuple[bool, ...]],
    options: dict[str, list[TruckOption]] | None = None,
    wall_time: float = 0.0,
) -> FuelPlan:
    """The time-boxed fallback (PRD M7): only each truck's next trip, assigned by earliest
    deadline. Hospitals off the grid now, most urgent first, each get the free truck whose
    single trip lands soonest; every other truck stays idle (or keeps its current trip)."""
    if options is None:
        targets = needy(state, powered)
        options = {t.id: truck_options(state, t, targets) for t in state.trucks}
    deadline = []
    for h in state.hospitals:
        if powered.get(h.id, (False,))[0] or h.burn_lpm == 0:
            continue
        deadline.append((h.fuel_l // h.burn_lpm, h.id))
    trips: dict[str, tuple[ChosenTrip, ...]] = {}
    free = sorted(options)
    for _, hospital in sorted(deadline):
        best: tuple[int, str, Leg] | None = None
        for v in free:
            for option in options[v]:
                if len(option.legs) == 1 and option.legs[0].hospital == hospital:
                    leg = option.legs[0]
                    if best is None or (leg.delivered_at, v) < best[:2]:
                        best = (leg.delivered_at, v, leg)
        if best is None:
            continue
        _, v, leg = best
        cap = next(t.capacity_l for t in state.trucks if t.id == v)
        trips[v] = (ChosenTrip(leg, cap if leg.depot else 0, cap),)
        free.remove(v)
    for v in free:  # no new trip: it keeps whatever it's doing
        trips[v] = ()
    return FuelPlan(
        status="greedy",
        trips={v: trips[v] for v in sorted(trips)},
        dark={},
        fuel_end={},
        objective=0,
        deterministic_time=0.0,
        wall_time=wall_time,
        options=sum(len(o) for o in options.values()),
    )
