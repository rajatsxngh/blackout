"""T01, blocked fuel route: the fastest depot-to-hospital road is blocked early, and the first
word comes by radio. Good play routes around it before a truck gets stuck (PRD 8.6).

A hospital is cut off from the grid for longer than its fuel lasts. Its fuel covers loading,
the detour and unloading with a few minutes to spare, but not the cost of driving into the
block, getting stuck and rerouting from there.
"""

from blackout.scenarios.schema import Validity
from blackout.scenarios.seeding import Rng
from blackout.scenarios.templates.common import (
    do_nothing_problem,
    isolate_hospital,
    repair_minutes,
    report_block,
    report_clear,
    report_depot,
    report_failure,
    report_fuel,
)
from blackout.scenarios.world import Draft, Infeasible, fastest, path_edges
from blackout.sim.config import SimConfig
from blackout.sim.events import RoadBlock, RoadClear

SIM = SimConfig()


class BlockedFuelRoute:
    id = "T01"
    title = "Blocked fuel route"

    def apply(self, draft: Draft, rng: Rng) -> None:
        cfg = draft.config.templates.T01
        city = draft.built_city()
        target = rng.pick(draft.hospitals)
        repair = repair_minutes(draft, rng, cfg.repair_minutes)
        substation_id = isolate_hospital(draft, target.id, repair)

        routes = []
        for depot in draft.depots:
            found = fastest(city, depot.node, target.node)
            if found is not None:
                routes.append((found[1], depot.id, depot, found[0]))
        _, _, depot, path = min(routes, key=lambda r: (r[0], r[1]))

        # For each segment of the fast route: the planned detour, and the cost of finding the
        # block on arrival (drive to it, get stuck, reroute from there).
        options: list[tuple[str, int, int]] = []
        for index, segment in enumerate(path_edges(path)):
            detour = fastest(city, depot.node, target.node, frozenset({segment}))
            onward = fastest(city, path[index], target.node, frozenset({segment}))
            so_far = fastest(city, depot.node, path[index])
            if detour is None or onward is None or so_far is None:
                continue
            naive_loss = so_far[1] + onward[1] - detour[1]
            if naive_loss >= cfg.min_naive_loss_minutes:
                options.append((segment, detour[1], naive_loss))
        if not options:
            raise Infeasible("no segment of the fast route makes getting stuck costly")
        segment, detour_minutes, naive_loss = rng.pick(sorted(options))

        low, high = cfg.slack_minutes[0], min(cfg.slack_minutes[1], naive_loss - 1)
        if high < low:
            raise Infeasible(f"getting stuck costs only {naive_loss} min")
        slack = rng.between(low, high)
        handling = SIM.load_minutes + SIM.unload_minutes
        fuel = (handling + detour_minutes + slack) * target.burn_lpm
        if fuel > target.tank_l:
            raise Infeasible(f"{target.id} would need {fuel} L")
        draft.update_hospital(target.id, fuel_l=fuel)

        blocked_at = rng.between(*cfg.block_tick)
        draft.events.append(RoadBlock(tick=blocked_at, edge=segment))
        cleared_at = rng.between(*cfg.clear_tick)
        draft.events.append(RoadClear(tick=cleared_at, edge=segment))
        draft.story.append(
            f"The fast route from {depot.name} is blocked on {city.edges[segment].name}"
            f" ({segment}) from t={blocked_at} to t={cleared_at}. {target.id} has {fuel} L:"
            f" enough for the {detour_minutes}-minute detour with {slack} min to spare, not for"
            f" getting stuck first ({naive_loss} min lost)."
        )

        report_block(draft, rng, segment, blocked_at, rng.between(*cfg.report_delay))
        report_fuel(draft, rng, target.id, unpowered_from=0)
        report_failure(draft, rng, substation_id, 0, repair)
        report_depot(draft, rng)
        report_clear(draft, rng, segment, cleared_at)

    def problem(self, validity: Validity) -> str | None:
        return do_nothing_problem(validity)
