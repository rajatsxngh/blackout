"""Power planning: which zones to shed, where crews go, and when each hospital has grid power.

The power plan comes first because it decides which hospitals burn fuel (PRD section 8.7):

- **Shedding** is an exact search over every set of non-critical zones (at most 2^9 on the
  default map). A set is acceptable if every online substation then carries at most 95% of
  its capacity; among acceptable sets the one serving the most priority-weighted demand wins,
  then the one closest to what's shed now, then the lowest-numbered. Critical zones (the
  hospitals' zones) are never shed. If no set fits under 95% the search allows 100%; if even
  that fails, the overloaded substations are expected to trip and drop out of the picture.
- **Crews** are planned by brute force over every assignment of down substations to crews and
  every order, minimizing the sum of restore time x the weighted demand the substation
  normally feeds. A crew busy repairing joins the plan once its current repair is done.
- The **timeline** says, for every bucket of the fuel model's horizon, which hospitals are
  powered: a substation counts as online in a bucket only if it's restored by the bucket's
  start, so the fuel plan never relies on power that might come back a few minutes late.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from itertools import permutations, product

from blackout.controllers.solver.state import PlanningState
from blackout.info.telemetry import ZoneView
from blackout.sim.entities import Priority, SubstationState, VehicleState

SHED_LIMIT_PCT = 95
_MAX_CREW_OPTIONS = 50_000  # past this many assignments, plan crews greedily instead


@dataclass(frozen=True, slots=True)
class Grid:
    """One way the grid settles: what's shed, who feeds each zone, what tripped."""

    shed: frozenset[str]
    feeds: dict[str, str | None]
    tripped: frozenset[str]
    served_weighted_kw: int

    def powered(self, zone_id: str) -> bool:
        return self.feeds.get(zone_id) is not None


@dataclass(frozen=True, slots=True)
class CrewOrder:
    """One crew's planned work: the substations in order, and when each is back online."""

    crew_id: str
    substations: tuple[str, ...]
    online_at: tuple[int, ...]  # minutes from now


@dataclass(frozen=True, slots=True)
class PowerTimeline:
    shed_now: frozenset[str]
    crews: tuple[CrewOrder, ...]
    online_at: dict[str, int | None]  # substation -> minutes until online (0: now; None: never)
    powered: dict[str, tuple[bool, ...]]  # hospital -> powered in each bucket
    grid_now: Grid


def feeds_for(
    zones: Sequence[ZoneView], online: frozenset[str], shed: frozenset[str]
) -> dict[str, str | None]:
    """The engine's feed rule: primary if online, else alternate if online, unless shed."""
    feeds: dict[str, str | None] = {}
    for z in zones:
        feeds[z.id] = None
        if z.id in shed:
            continue
        for candidate in (z.primary, z.alternate):
            if candidate is not None and candidate in online:
                feeds[z.id] = candidate
                break
    return feeds


def best_shed(
    state: PlanningState,
    online: frozenset[str],
    current: frozenset[str],
    limit_pct: int = SHED_LIMIT_PCT,
) -> Grid | None:
    """The best shed set with every online substation at or below ``limit_pct`` of capacity,
    or None if no set of non-critical zones gets there."""
    zones = state.zones
    capacity = {s.id: s.capacity_kw for s in state.substations}
    weight = {z.id: state.config.weight(z.priority) for z in zones}
    sheddable = [z.id for z in zones if z.priority is not Priority.CRITICAL]
    best: tuple[tuple[int, int], Grid] | None = None
    for mask in range(1 << len(sheddable)):
        shed = frozenset(z for i, z in enumerate(sheddable) if mask >> i & 1)
        feeds = feeds_for(zones, online, shed)
        loads: dict[str, int] = {}
        for z in zones:
            feeder = feeds[z.id]
            if feeder is not None:
                loads[feeder] = loads.get(feeder, 0) + z.demand_kw
        if any(load * 100 > limit_pct * capacity[s] for s, load in loads.items()):
            continue
        served = sum(z.demand_kw * weight[z.id] for z in zones if feeds[z.id] is not None)
        score = (served, -len(shed ^ current))
        if best is None or score > best[0]:
            best = (score, Grid(shed, feeds, frozenset(), served))
    return None if best is None else best[1]


def settle(state: PlanningState, online: frozenset[str], current: frozenset[str]) -> Grid:
    """How the grid ends up with ``online`` substations and the best shedding we can do."""
    tripped: set[str] = set()
    up = set(online)
    while True:
        live = frozenset(up)
        grid = best_shed(state, live, current) or best_shed(state, live, current, 100)
        if grid is not None:
            return Grid(grid.shed, grid.feeds, frozenset(tripped), grid.served_weighted_kw)
        # Even shedding every non-critical zone leaves something overloaded: it will trip.
        everything = frozenset(z.id for z in state.zones if z.priority is not Priority.CRITICAL)
        feeds = feeds_for(state.zones, live, everything)
        capacity = {s.id: s.capacity_kw for s in state.substations}
        loads: dict[str, int] = {}
        for z in state.zones:
            feeder = feeds[z.id]
            if feeder is not None:
                loads[feeder] = loads.get(feeder, 0) + z.demand_kw
        over = sorted(s for s, load in loads.items() if load > capacity[s])
        tripped.update(over)
        up.difference_update(over)


def plan_crews(state: PlanningState) -> tuple[tuple[CrewOrder, ...], dict[str, int | None]]:
    """Brute-force crew plan. Returns each crew's order and every substation's restore time."""
    online_at: dict[str, int | None] = {}
    down: list[str] = []
    free_at: dict[str, tuple[str, int]] = {}  # crew -> (node, minute it can start driving)
    nodes = {s.id: s.node for s in state.substations}
    for s in state.substations:
        if s.state is SubstationState.ONLINE:
            online_at[s.id] = 0
        elif s.state is SubstationState.REPAIRING:
            online_at[s.id] = state.repair_minutes[s.id]
        else:
            down.append(s.id)
            online_at[s.id] = None
    for c in state.crews:
        if c.view.state is VehicleState.REPAIRING and c.view.substation is not None:
            site = c.view.substation
            free_at[c.id] = (nodes[site], online_at.get(site) or 0)
        else:
            free_at[c.id] = (c.node, c.lead)
    crews = sorted(free_at)
    if not down or not crews:
        return tuple(CrewOrder(c, (), ()) for c in crews), online_at

    weight = dict.fromkeys(down, 1)
    for z in state.zones:
        if z.primary in weight:
            weight[z.primary] += z.demand_kw * state.config.weight(z.priority)
    travel: dict[tuple[str, str], int | None] = {}

    def drive(start: str, site: str) -> int | None:
        key = (start, site)
        if key not in travel:
            travel[key] = state.minutes(start, nodes[site])
        return travel[key]

    def evaluate(order: Sequence[str], who: Sequence[str]) -> tuple[int, dict[str, list[int]]]:
        position = dict(free_at)
        done: dict[str, list[int]] = {c: [] for c in crews}
        cost = 0
        for site, crew in zip(order, who, strict=True):
            node, minute = position[crew]
            minutes = drive(node, site) if node != nodes[site] else 0
            if minutes is None:
                return _UNREACHABLE, done
            finish = minute + minutes + state.repair_minutes[site]
            position[crew] = (nodes[site], finish)
            done[crew].append(finish)
            cost += finish * weight[site]
        return cost, done

    options = _count(len(down), len(crews))
    best: tuple[int, tuple[str, ...], tuple[str, ...]] | None = None
    if options <= _MAX_CREW_OPTIONS:
        for order in permutations(down):
            for who in product(crews, repeat=len(down)):
                cost, _ = evaluate(order, who)
                if cost < _UNREACHABLE and (best is None or cost < best[0]):
                    best = (cost, order, who)
    else:  # too many to enumerate: most weighted substation first, to the first crew free
        order = tuple(sorted(down, key=lambda s: (-weight[s], s)))
        who = tuple(crews[i % len(crews)] for i in range(len(order)))
        cost, _ = evaluate(order, who)
        best = (cost, order, who) if cost < _UNREACHABLE else None
    if best is None:
        return tuple(CrewOrder(c, (), ()) for c in crews), online_at
    _, order, who = best
    _, done = evaluate(order, who)
    plans = []
    for crew in crews:
        sites = tuple(site for site, w in zip(order, who, strict=True) if w == crew)
        plans.append(CrewOrder(crew, sites, tuple(done[crew])))
        for site, finish in zip(sites, done[crew], strict=True):
            online_at[site] = finish
    return tuple(plans), online_at


_UNREACHABLE = 1 << 62


def _count(sites: int, crews: int) -> int:
    total = crews**sites
    for k in range(2, sites + 1):
        total *= k
    return total


def power_timeline(state: PlanningState, starts: Sequence[int]) -> PowerTimeline:
    """The power plan: shedding now, crew orders, and each hospital's power in each bucket
    (``starts`` are the buckets' start minutes)."""
    current = frozenset(z.id for z in state.zones if z.shed)
    crews, online_at = plan_crews(state)
    now_online = frozenset(s for s, at in online_at.items() if at == 0)
    grid_now = settle(state, now_online, current)
    by_online: dict[frozenset[str], Grid] = {now_online: grid_now}
    powered: dict[str, list[bool]] = {h.id: [] for h in state.hospitals}
    for start in starts:
        online = frozenset(s for s, at in online_at.items() if at is not None and at <= start)
        if online not in by_online:
            by_online[online] = settle(state, online, grid_now.shed)
        grid = by_online[online]
        for h in state.hospitals:
            powered[h.id].append(h.zone is not None and grid.powered(h.zone))
    return PowerTimeline(
        shed_now=grid_now.shed,
        crews=crews,
        online_at=online_at,
        powered={h: tuple(p) for h, p in powered.items()},
        grid_now=grid_now,
    )
