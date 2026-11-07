"""T01, blocked fuel route: the fastest depot-to-hospital road is blocked early, and the first
word comes by radio. Good play routes around it before a truck gets stuck (PRD 8.6).

A hospital is cut off from the grid for longer than its fuel lasts, and the fleet is spread
over the city. A segment of the fastest route from the nearest depot is blocked where
driving into it costs real minutes. Sometimes a public caller (unverified, but right) is the
first to report it and the police confirm it only later. Calibration sets the hospital's fuel.
"""

from blackout.scenarios.schema import Validity
from blackout.scenarios.seeding import Rng
from blackout.scenarios.templates.common import (
    do_nothing_problem,
    isolate_hospital,
    repair_minutes,
    report_depot,
    report_failure,
    report_fuel,
    scatter_fleet,
)
from blackout.scenarios.templates.parts import block_fast_route
from blackout.scenarios.world import Draft


class BlockedFuelRoute:
    id = "T01"
    title = "Blocked fuel route"

    def apply(self, draft: Draft, rng: Rng) -> None:
        cfg = draft.config.templates.T01
        target = rng.pick(draft.hospitals)
        draft.target = target.id
        repair = repair_minutes(draft, rng, cfg.repair_minutes)
        substation_id = isolate_hospital(draft, target.id, repair)
        scatter_fleet(draft, rng)
        block_fast_route(
            draft,
            rng,
            block_tick=cfg.block_tick,
            report_delay=cfg.report_delay,
            caller_first_pct=cfg.caller_first_pct,
            clear_tick=cfg.clear_tick,
            min_loss=cfg.min_naive_loss_minutes,
        )
        report_fuel(draft, rng, target.id, unpowered_from=0)
        report_failure(draft, rng, substation_id, 0, repair)
        report_depot(draft, rng)

    def problem(self, validity: Validity) -> str | None:
        return do_nothing_problem(validity)
