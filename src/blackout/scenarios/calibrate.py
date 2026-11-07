"""Calibration and tiers: set a world's tight hospital fuel by simulation (PRD 8.6).

A template builds a situation around one hospital (``draft.target``); calibration decides
how much fuel that hospital starts with. It runs the world at L0 and searches:

- ``F_ref``: about the least fuel with which the **reference** (C3, ground truth, no
  foresight) loses nothing. Less than that and the world fails validity.
- ``F_rules``: about the least fuel with which **Rules** (C1, the naive script) loses
  nothing.

Each search starts low and adds the fuel the dark minutes say is missing (``dark minutes x
burn``), so it usually settles in two or three runs. Then the fuel is placed for the tier the
suite asks for, and the world is measured again at exactly that fuel:

- **hard**: just above ``F_ref`` (``hard_extra_minutes``) while Rules still loses minutes.
  Only possible if ``F_rules > F_ref``, i.e. if greedy dispatch needs more fuel than good
  planning does in this world.
- **medium**: a few minutes above ``F_rules`` (``medium_margin_minutes``): Rules survives,
  tightly.
- **easy**: well above both (``easy_margin_minutes``), so the reference keeps at least
  ``easy_slack_minutes`` of fuel at its tightest moment.

The tier is then *measured*, never assumed (``tier_of``): hard if Rules loses at L0, easy if
Rules doesn't and the reference's minimum slack is at least ``easy_slack_minutes``, medium
otherwise. If the measured tier isn't the one asked for, the world is re-rolled.

Only C0, C1 and C3 take part. The Solver (C2) is never run here, so nothing about the
benchmark is tuned to it; Rules is, by the PRD's own definition of the tiers.
"""

from collections.abc import Callable
from dataclasses import dataclass

from blackout.controllers.base import Controller
from blackout.controllers.do_nothing import DoNothingController
from blackout.controllers.rules import RulesController
from blackout.controllers.solver.controller import ReferenceController
from blackout.env import env_for, run_episode
from blackout.scenarios.config import CalibrationCfg
from blackout.scenarios.schema import Scenario, Tier
from blackout.sim.engine import Engine
from blackout.sim.entities import HospitalStatus

TIERS: tuple[Tier, ...] = ("easy", "medium", "hard")


@dataclass(frozen=True, slots=True)
class RunOutcome:
    critical_downtime: int
    trips: int
    min_slack: int | None  # fewest minutes of fuel left on any running generator


def play(scenario: Scenario, controller: Controller) -> RunOutcome:
    """Run one contestant on a scenario and measure it (no log is written)."""
    env = env_for(scenario, controller)
    tightest: list[int] = []

    def watch(engine: Engine) -> None:
        for hospital in engine.hospitals.values():
            if hospital.status is HospitalStatus.ON_GENERATOR:
                minutes = hospital.fuel_l // hospital.burn_lpm
                if not tightest or minutes < tightest[0]:
                    tightest[:] = [minutes]

    env.add_tick_listener(watch)
    run_episode(env, controller)
    metrics = env.engine.metrics
    return RunOutcome(
        critical_downtime=metrics.critical_downtime,
        trips=metrics.trips,
        min_slack=tightest[0] if tightest else None,
    )


def do_nothing(scenario: Scenario) -> RunOutcome:
    return play(scenario, DoNothingController())


def rules(scenario: Scenario) -> RunOutcome:
    return play(scenario, RulesController())


def reference(scenario: Scenario) -> RunOutcome:
    return play(scenario, ReferenceController())


def tier_of(rules_l0: RunOutcome, reference_run: RunOutcome, cfg: CalibrationCfg) -> Tier:
    """PRD 8.6: the tier comes from whether Rules survives at L0 and the reference's slack."""
    if rules_l0.critical_downtime > 0:
        return "hard"
    slack = reference_run.min_slack
    if slack is None or slack >= cfg.easy_slack_minutes:
        return "easy"
    return "medium"


class Calibrator:
    """Searches one world's target fuel. ``build(fuel)`` gives the world at L0 with that
    starting fuel; results are cached per fuel and contestant."""

    def __init__(
        self,
        build: Callable[[int], Scenario],
        *,
        burn: int,
        low: int,
        high: int,
        cfg: CalibrationCfg,
    ) -> None:
        self.build, self.burn, self.low, self.high, self.cfg = build, burn, low, high, cfg
        self._cache: dict[tuple[str, int], RunOutcome] = {}

    def run(self, who: str, fuel: int) -> RunOutcome:
        key = (who, fuel)
        if key not in self._cache:
            scenario = self.build(fuel)
            self._cache[key] = {"do-nothing": do_nothing, "rules": rules, "reference": reference}[
                who
            ](scenario)
        return self._cache[key]

    def least_fuel(self, who: str, start: int) -> int | None:
        """About the least fuel (from ``start`` up) with which ``who`` loses nothing."""
        fuel = max(self.low, start)
        for _ in range(self.cfg.probes):
            if fuel > self.high:
                return None
            dark = self.run(who, fuel).critical_downtime
            if dark == 0:
                return fuel
            fuel = min(self.high + 1, fuel + dark * self.burn)
        return None

    def place(self, tier: Tier, minutes: int) -> int | None:
        """The starting fuel to try for ``tier``, ``minutes`` of burn above its anchor, or
        None if the world can't host the tier."""
        f_ref = self.least_fuel("reference", self.low)
        if f_ref is None:
            return None
        if tier == "hard":
            if self.run("rules", f_ref).critical_downtime == 0:
                return None
            return f_ref + minutes * self.burn
        f_rules = self.least_fuel("rules", f_ref)
        if f_rules is None:
            return None
        return max(f_ref, f_rules) + minutes * self.burn
