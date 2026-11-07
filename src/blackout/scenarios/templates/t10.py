"""T10, compound storm: three of the other situations at once, at moderate intensity. Good
play handles everything together (PRD 8.6).

A hospital is cut off from the grid and the fleet is spread over the city. Three parts are
drawn from: a blocked fast route (T01), a loaded truck breaking down on its way (T04), a
burn spike (T05), the nearest depot running dry (T06), a closed road that reopens (T08) and
a gridlocked district (T09, at its lightest). "Moderate" means each part's mildest settings:
reports come promptly from police, the spike is 4 L/min, gridlock has its minimum of blocks.
Calibration sets the hospital's fuel.
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
from blackout.scenarios.templates.parts import (
    block_fast_route,
    breakdown,
    gridlock,
    reopening,
    shortage,
    spike,
)
from blackout.scenarios.world import Draft

PARTS = ("block", "breakdown", "spike", "shortage", "reopening", "gridlock")
_ROADS = frozenset({"block", "reopening", "gridlock"})  # at most one: they'd fight over a route


class CompoundStorm:
    id = "T10"
    title = "Compound storm"

    def apply(self, draft: Draft, rng: Rng) -> None:
        cfg = draft.config.templates.T10
        target = rng.pick(draft.hospitals)
        draft.target = target.id
        repair = repair_minutes(draft, rng, cfg.repair_minutes)
        substation_id = isolate_hospital(draft, target.id, repair)
        chosen: list[str] = []
        for part in rng.shuffled(PARTS):
            if part in _ROADS and _ROADS & set(chosen):
                continue
            chosen.append(part)
            if len(chosen) == cfg.ingredients:
                break
        chosen.sort(key=PARTS.index)  # apply in a fixed order: the breakdown places a truck
        draft.story.append("Compound storm: " + ", ".join(chosen) + ".")
        templates = draft.config.templates
        kept: frozenset[str] = frozenset()
        if "breakdown" in chosen:
            t04 = templates.T04
            kept = frozenset(
                {
                    breakdown(
                        draft,
                        rng,
                        drive_minutes=t04.loaded_drive_minutes,
                        breakdown_pct=t04.breakdown_pct,
                    )
                }
            )
        scatter_fleet(draft, rng, keep=kept)
        if "shortage" in chosen:
            t06 = templates.T06
            shortage(draft, rng, low_l=t06.low_stock_l, far_l=t06.far_stock_l)
        if "spike" in chosen:
            low = templates.T05.spike_lpm[0]
            spike(draft, rng, target.id, tick=templates.T05.spike_tick, lpm=(low, low))
        if "block" in chosen:
            t01 = templates.T01
            block_fast_route(
                draft,
                rng,
                block_tick=t01.block_tick,
                report_delay=(t01.report_delay[0], t01.report_delay[0] + 2),
                caller_first_pct=0,
                clear_tick=t01.clear_tick,
                min_loss=t01.min_naive_loss_minutes,
            )
        if "reopening" in chosen:
            t08 = templates.T08
            reopening(
                draft,
                rng,
                reopen_tick=t08.reopen_tick,
                caller_first_pct=0,
                min_gain=t08.min_gain_minutes,
            )
        if "gridlock" in chosen:
            t09 = templates.T09
            gridlock(
                draft,
                rng,
                blocks=(t09.blocks[0], t09.blocks[0]),
                block_tick=t09.block_tick,
                report_delay=t09.report_delay,
                caller_only_pct=0,
                unreported_pct=0,
            )
        report_fuel(draft, rng, target.id, unpowered_from=0)
        report_failure(draft, rng, substation_id, 0, repair)
        if "shortage" not in chosen:
            report_depot(draft, rng)

    def problem(self, validity: Validity) -> str | None:
        return do_nothing_problem(validity)
