"""T09, gridlock: several blocks split the network around a hospital. Good play plans the
detours instead of finding the blocks one by one (PRD 8.6).

A hospital is cut off from the grid and the fleet is spread over the city. Three to five
blocks cut the fastest ways in, one after another. Police report most; some only a caller
reports (true, unverified); some nobody reports, and a truck finds them by driving into
them. Calibration sets the hospital's fuel.
"""

from blackout.scenarios.schema import Validity
from blackout.scenarios.seeding import Rng
from blackout.scenarios.templates.common import (
    do_nothing_problem,
    isolate_hospital,
    repair_minutes,
    report_failure,
    report_fuel,
    scatter_fleet,
)
from blackout.scenarios.templates.parts import gridlock
from blackout.scenarios.world import Draft


class Gridlock:
    id = "T09"
    title = "Gridlock"

    def apply(self, draft: Draft, rng: Rng) -> None:
        cfg = draft.config.templates.T09
        target = rng.pick(draft.hospitals)
        draft.target = target.id
        repair = repair_minutes(draft, rng, cfg.repair_minutes)
        substation_id = isolate_hospital(draft, target.id, repair)
        scatter_fleet(draft, rng)
        gridlock(
            draft,
            rng,
            blocks=cfg.blocks,
            block_tick=cfg.block_tick,
            report_delay=cfg.report_delay,
            caller_only_pct=cfg.caller_only_pct,
            unreported_pct=cfg.unreported_pct,
        )
        report_fuel(draft, rng, target.id, unpowered_from=0)
        report_failure(draft, rng, substation_id, 0, repair)

    def problem(self, validity: Validity) -> str | None:
        return do_nothing_problem(validity)
