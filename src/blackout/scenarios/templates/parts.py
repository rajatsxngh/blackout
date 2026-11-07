"""Situations that templates are built from: a blocked route, a breakdown, a burn spike, a
depot running dry, a road reopening, a gridlocked district.

Each part breaks one thing in a draft, around the hospital calibration will make tight
(``draft.target``), and plans the reports that tell contestants about it. T10 (compound
storm) combines three of them. Every part raises ``Infeasible`` if the world can't host it.
"""

import math

from blackout.info.reports import Channel, FactType, SourceClass
from blackout.scenarios.seeding import Rng
from blackout.scenarios.templates.common import (
    EARLY_TICKS,
    report_block,
    report_clear,
    report_spike,
)
from blackout.scenarios.text.render import TimedFact
from blackout.scenarios.world import (
    Draft,
    Infeasible,
    depot_source,
    fact,
    fastest,
    path_edges,
    police_source,
    public_source,
)
from blackout.sim.city import EdgeId, node_ids
from blackout.sim.events import BurnRateChange, RoadBlock, RoadClear, TruckBreakdown


def nearest_depot(draft: Draft, hospital_id: str) -> tuple[str, list[str], int]:
    """The depot with the fastest drive to the hospital: its ID, the path and the minutes."""
    city = draft.built_city()
    node = draft.hospital(hospital_id).node
    routes = []
    for depot in draft.depots:
        found = fastest(city, depot.node, node)
        if found is not None:
            routes.append((found[1], depot.id, found[0]))
    if not routes:
        raise Infeasible(f"no depot can reach {hospital_id}")
    minutes, depot_id, path = min(routes)
    return depot_id, path, minutes


def block_fast_route(
    draft: Draft,
    rng: Rng,
    *,
    block_tick: tuple[int, int],
    report_delay: tuple[int, int],
    caller_first_pct: int,
    clear_tick: tuple[int, int],
    min_loss: int,
) -> EdgeId:
    """Block a segment of the fastest depot-to-hospital route, where driving into the block
    and rerouting from there costs at least ``min_loss`` minutes more than the detour."""
    assert draft.target is not None
    city = draft.built_city()
    target = draft.hospital(draft.target)
    depot_id, path, _ = nearest_depot(draft, target.id)
    depot = next(d for d in draft.depots if d.id == depot_id)
    options: list[tuple[str, int, int]] = []
    for index, segment in enumerate(path_edges(path)):
        detour = fastest(city, depot.node, target.node, frozenset({segment}))
        onward = fastest(city, path[index], target.node, frozenset({segment}))
        so_far = fastest(city, depot.node, path[index])
        if detour is None or onward is None or so_far is None:
            continue
        naive_loss = so_far[1] + onward[1] - detour[1]
        if naive_loss >= min_loss:
            options.append((segment, detour[1], naive_loss))
    if not options:
        raise Infeasible("no segment of the fast route makes getting stuck costly")
    segment, detour_minutes, naive_loss = rng.pick(sorted(options))
    blocked_at = rng.between(*block_tick)
    cleared_at = rng.between(*clear_tick)
    draft.events.append(RoadBlock(tick=blocked_at, edge=segment))
    draft.events.append(RoadClear(tick=cleared_at, edge=segment))
    caller_first = rng.chance(caller_first_pct)
    delay = rng.between(*report_delay)
    report_block(draft, rng, segment, blocked_at, delay, caller_first=caller_first)
    report_clear(draft, rng, segment, cleared_at)
    first = "a caller" if caller_first else "police"
    draft.story.append(
        f"The fast route from {depot.name} to {target.id} is blocked on"
        f" {city.edges[segment].name} ({segment}) from t={blocked_at} to t={cleared_at};"
        f" {first} report it at t={blocked_at + delay}. The detour takes {detour_minutes} min;"
        f" driving into the block costs {naive_loss} min more."
    )
    return segment


def breakdown(
    draft: Draft, rng: Rng, *, drive_minutes: tuple[int, int], breakdown_pct: tuple[int, int]
) -> str:
    """A truck with a full load starts a short drive from the target hospital and breaks
    down for good part of the way there (if it's sent at once). Returns the truck."""
    assert draft.target is not None
    city = draft.built_city()
    target = draft.hospital(draft.target)
    truck = rng.pick(draft.trucks)
    low, high = drive_minutes
    starts: list[tuple[str, int]] = []
    for node in node_ids(city.width, city.height):
        found = fastest(city, node, target.node)
        if found is not None and low <= found[1] <= high:
            starts.append((node, found[1]))
    if not starts:
        raise Infeasible(f"no intersection is {low}-{high} min from {target.id}")
    start, drive = rng.pick(starts)
    draft.update_truck(truck.id, node=start, load_l=truck.capacity_l)
    broken_at = max(1, math.ceil(drive * rng.between(*breakdown_pct) / 100))
    draft.events.append(TruckBreakdown(tick=broken_at, truck=truck.id))
    draft.story.append(
        f"{truck.id} starts at {start} with {truck.capacity_l} L, {drive} min from"
        f" {target.id}, and breaks down for good at t={broken_at}."
    )
    return truck.id


def spike(
    draft: Draft, rng: Rng, hospital_id: str, *, tick: tuple[int, int], lpm: tuple[int, int]
) -> None:
    """The hospital's generator starts burning faster; staff call it in as it happens."""
    at, burn = rng.between(*tick), rng.between(*lpm)
    draft.events.append(BurnRateChange(tick=at, hospital=hospital_id, burn_lpm=burn))
    report_spike(draft, rng, hospital_id, at, burn)
    draft.story.append(f"{hospital_id}'s generator jumps to {burn} L/min at t={at}.")


def shortage(draft: Draft, rng: Rng, *, low_l: tuple[int, int], far_l: tuple[int, int]) -> str:
    """The depot nearest the target hospital is nearly empty; the other depots are full. Its
    staff report the stock early (true). Returns the nearly empty depot."""
    assert draft.target is not None
    depot_id, _, _ = nearest_depot(draft, draft.target)
    step = draft.config.depots.stock_step_l
    low = rng.between(*low_l, step)
    draft.depots = [
        d.model_copy(update={"stock_l": low if d.id == depot_id else rng.between(*far_l, step)})
        for d in draft.depots
    ]
    depot = next(d for d in draft.depots if d.id == depot_id)
    tick = rng.between(*EARLY_TICKS)
    draft.plan(
        tick,
        Channel.DEPOT_STAFF,
        SourceClass.FIELD,
        depot_source(rng, depot),
        TimedFact(fact(FactType.DEPOT_STOCK, depot_id, low), tick),
    )
    draft.story.append(f"{depot.name} ({depot_id}), the nearest depot, has only {low} L.")
    return depot_id


def reopening(
    draft: Draft,
    rng: Rng,
    *,
    reopen_tick: tuple[int, int],
    caller_first_pct: int,
    min_gain: int,
) -> EdgeId:
    """A segment of the fastest depot-to-hospital route is closed from the start and reopens
    later; police report the closure at once and the reopening soon after it happens."""
    assert draft.target is not None
    city = draft.built_city()
    target = draft.hospital(draft.target)
    depot_id, path, minutes = nearest_depot(draft, target.id)
    depot = next(d for d in draft.depots if d.id == depot_id)
    options = []
    for segment in path_edges(path):
        detour = fastest(city, depot.node, target.node, frozenset({segment}))
        if detour is not None and detour[1] - minutes >= min_gain:
            options.append((segment, detour[1] - minutes))
    if not options:
        raise Infeasible("closing no segment of the fast route costs enough")
    segment, gain = rng.pick(sorted(options))
    opened = rng.between(*reopen_tick)
    draft.events.append(RoadBlock(tick=0, edge=segment))
    draft.events.append(RoadClear(tick=opened, edge=segment))
    closed = TimedFact(fact(FactType.ROAD_BLOCKED, segment), 0)
    draft.plan(
        rng.between(1, 2), Channel.POLICE_RADIO, SourceClass.FIELD, police_source(rng), closed
    )
    cleared = TimedFact(fact(FactType.ROAD_CLEARED, segment), opened)
    if rng.chance(caller_first_pct):
        draft.plan(
            opened + rng.between(1, 3),
            Channel.PUBLIC,
            SourceClass.UNVERIFIED,
            public_source(rng),
            cleared,
        )
        draft.plan(
            opened + rng.between(*draft.config.reports.police_late_minutes),
            Channel.POLICE_RADIO,
            SourceClass.FIELD,
            police_source(rng),
            cleared,
        )
    else:
        report_clear(draft, rng, segment, opened)
    draft.story.append(
        f"{city.edges[segment].name} ({segment}), on the fast route from {depot.name} to"
        f" {target.id}, is closed until t={opened}; the detour costs {gain} min more."
    )
    return segment


def gridlock(
    draft: Draft,
    rng: Rng,
    *,
    blocks: tuple[int, int],
    block_tick: tuple[int, int],
    report_delay: tuple[int, int],
    caller_only_pct: int,
    unreported_pct: int,
) -> list[EdgeId]:
    """Several blocks cut the fastest ways into the target hospital, one after another, so
    the routes left wind around them. Most are reported by police; some only by a caller
    (true, unverified), some by nobody (found by driving into them). The first one is always
    reported by police."""
    assert draft.target is not None
    city = draft.built_city()
    target = draft.hospital(draft.target)
    chosen: list[EdgeId] = []
    for _ in range(rng.between(*blocks)):
        avoid = frozenset(chosen)
        routes = []
        for origin in sorted({d.node for d in draft.depots} | {t.node for t in draft.trucks}):
            found = fastest(city, origin, target.node, avoid)
            if found is not None and len(found[0]) > 1:
                routes.append((found[1], origin, found[0]))
        if not routes:
            break
        _, _, path = min(routes)
        candidates = [
            e
            for e in path_edges(path)
            if fastest(city, path[0], target.node, frozenset({*chosen, e})) is not None
        ]
        if not candidates:
            break
        chosen.append(rng.pick(candidates))
    if len(chosen) < blocks[0]:
        raise Infeasible(f"only {len(chosen)} blocks keep {target.id} reachable")
    how = []
    for index, segment in enumerate(chosen):
        at = rng.between(*block_tick)
        draft.events.append(RoadBlock(tick=at, edge=segment))
        delay = rng.between(*report_delay)
        if index > 0 and rng.chance(unreported_pct):
            how.append(f"{segment} (unreported)")
        elif index > 0 and rng.chance(caller_only_pct):
            report_block(draft, rng, segment, at, delay, caller_first=True, police=False)
            how.append(f"{segment} (a caller only)")
        else:
            report_block(draft, rng, segment, at, delay)
            how.append(f"{segment} (police)")
    draft.story.append(f"Blocks around {target.id}: " + ", ".join(how) + ".")
    return chosen
