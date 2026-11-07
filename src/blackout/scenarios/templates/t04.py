"""T04, breakdown mid-delivery: a loaded truck heading for a hospital breaks down for good.
Good play reassigns early: another truck delivers, loaded or via a depot (PRD 8.6).

A hospital is cut off from the grid. A truck already carrying a full load starts a short
drive away from it and breaks down part of the way there (if sent at once). The rest of the
fleet is spread over the city. A side block elsewhere sometimes adds radio traffic.
Calibration sets the hospital's fuel.
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
    scatter_fleet,
)
from blackout.scenarios.templates.parts import breakdown
from blackout.scenarios.world import Draft, fastest, path_edges
from blackout.sim.events import RoadBlock, RoadClear


class BreakdownMidDelivery:
    id = "T04"
    title = "Breakdown mid-delivery"

    def apply(self, draft: Draft, rng: Rng) -> None:
        cfg = draft.config.templates.T04
        target = rng.pick(draft.hospitals)
        draft.target = target.id
        repair = repair_minutes(draft, rng, cfg.repair_minutes)
        substation_id = isolate_hospital(draft, target.id, repair)
        broken = breakdown(
            draft, rng, drive_minutes=cfg.loaded_drive_minutes, breakdown_pct=cfg.breakdown_pct
        )
        scatter_fleet(draft, rng, keep=frozenset({broken}))
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
