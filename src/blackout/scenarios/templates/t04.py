"""T04, breakdown mid-delivery: a loaded truck heading for a hospital breaks down for good.
Good play reassigns early: another truck loads at a depot and delivers (PRD 8.6).

A hospital is cut off from the grid. A truck already carrying a full load starts a short
drive away from it and breaks down part of the way there (if sent at once). The hospital's
fuel covers the time until the breakdown, plus a reserve truck's load, drive and unload, plus
a little slack. A side block elsewhere in the city sometimes adds radio traffic.
"""

import math

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
from blackout.sim.city import node_ids
from blackout.sim.config import SimConfig
from blackout.sim.events import RoadBlock, RoadClear, TruckBreakdown

SIM = SimConfig()


class BreakdownMidDelivery:
    id = "T04"
    title = "Breakdown mid-delivery"

    def apply(self, draft: Draft, rng: Rng) -> None:
        cfg = draft.config.templates.T04
        city = draft.built_city()
        target = rng.pick(draft.hospitals)
        repair = repair_minutes(draft, rng, cfg.repair_minutes)
        substation_id = isolate_hospital(draft, target.id, repair)

        truck = rng.pick(draft.trucks)
        low, high = cfg.loaded_drive_minutes
        starts: list[tuple[str, int]] = []
        for node in node_ids(city.width, city.height):
            found = fastest(city, node, target.node)
            if found is not None and low <= found[1] <= high:
                starts.append((node, found[1]))
        if not starts:
            raise Infeasible(f"no intersection is {low}-{high} min from {target.id}")
        start, drive = rng.pick(starts)
        draft.update_truck(truck.id, node=start, load_l=truck.capacity_l)
        broken_at = max(1, math.ceil(drive * rng.between(*cfg.breakdown_pct) / 100))
        draft.events.append(TruckBreakdown(tick=broken_at, truck=truck.id))

        reserve = []
        for other in draft.trucks:
            if other.id == truck.id:
                continue
            found = fastest(city, other.node, target.node)
            if found is not None:
                reserve.append((found[1], other.id))
        if not reserve:
            raise Infeasible("no reserve truck can reach the hospital")
        reserve_drive, reserve_id = min(reserve)
        slack = rng.between(*cfg.slack_minutes)
        handling = SIM.load_minutes + SIM.unload_minutes
        fuel = (broken_at + handling + reserve_drive + slack) * target.burn_lpm
        if fuel > target.tank_l:
            raise Infeasible(f"{target.id} would need {fuel} L")
        draft.update_hospital(target.id, fuel_l=fuel)
        draft.story.append(
            f"{truck.id} starts at {start} with {truck.capacity_l} L, {drive} min from"
            f" {target.id}, and breaks down for good at t={broken_at}. {target.id} has {fuel} L:"
            f" enough for {reserve_id} to load and deliver after the breakdown with {slack} min"
            " to spare."
        )

        report_fuel(draft, rng, target.id, unpowered_from=0)
        report_failure(draft, rng, substation_id, 0, repair)
        report_depot(draft, rng)
        if rng.chance(cfg.side_block_pct):
            self._side_block(draft, rng)

    @staticmethod
    def _side_block(draft: Draft, rng: Rng) -> None:
        """Block a segment that no fastest truck-to-hospital route uses, and report it."""
        city = draft.built_city()
        used: set[str] = set()
        for hospital in draft.hospitals:
            for origin in [t.node for t in draft.trucks] + [d.node for d in draft.depots]:
                found = fastest(city, origin, hospital.node)
                if found is not None:
                    used.update(path_edges(found[0]))
        free = sorted(set(city.edges) - used)
        if not free:
            return
        segment = rng.pick(free)
        blocked_at = rng.between(10, 60)
        cleared_at = blocked_at + rng.between(40, 120)
        draft.events.append(RoadBlock(tick=blocked_at, edge=segment))
        draft.events.append(RoadClear(tick=cleared_at, edge=segment))
        draft.story.append(
            f"A side block on {city.edges[segment].name} ({segment}) from t={blocked_at} to"
            f" t={cleared_at} touches no fast route."
        )
        report_block(draft, rng, segment, blocked_at, rng.between(1, 4))
        report_clear(draft, rng, segment, cleared_at)

    def problem(self, validity: Validity) -> str | None:
        return do_nothing_problem(validity)
