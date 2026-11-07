"""T08, road reopening: a closed road on the fast route reopens, and the reopening is
reported. Good play takes advantage of the faster route once it's open (PRD 8.6).

A hospital is cut off from the grid and the fleet is spread over the city. A segment of
the fastest route from the nearest depot is closed from the start (police report it at
once) and reopens some minutes later; police report the reopening, or a caller does first
(true, unverified) with the police confirming later. Calibration sets the hospital's fuel.
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
from blackout.scenarios.templates.parts import reopening
from blackout.scenarios.world import Draft


class RoadReopening:
    id = "T08"
    title = "Road reopening"

    def apply(self, draft: Draft, rng: Rng) -> None:
        cfg = draft.config.templates.T08
        target = rng.pick(draft.hospitals)
        draft.target = target.id
        repair = repair_minutes(draft, rng, cfg.repair_minutes)
        substation_id = isolate_hospital(draft, target.id, repair)
        scatter_fleet(draft, rng)
        reopening(
            draft,
            rng,
            reopen_tick=cfg.reopen_tick,
            caller_first_pct=cfg.caller_first_pct,
            min_gain=cfg.min_gain_minutes,
        )
        report_fuel(draft, rng, target.id, unpowered_from=0)
        report_failure(draft, rng, substation_id, 0, repair)
        report_depot(draft, rng)

    def problem(self, validity: Validity) -> str | None:
        return do_nothing_problem(validity)
