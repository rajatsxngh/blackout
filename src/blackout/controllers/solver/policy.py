"""C2's source filter and confirm policy (docs/PRD.md section 8.7, step 1).

- Facts from ``verified`` and ``field`` sources are believed.
- A fact from an ``unverified`` source is believed only once it's corroborated: by an
  independent second source (a different reporter making the same claim, numbers within
  10%), by telemetry, or by a confirm reply. Until then it waits in ``parked``.
- ``confirm_report`` is spent on a parked report only if believing it would change the plan's
  actions now, and only while the run has confirmations left.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field

from blackout.info.belief import BLOCKED, TELEMETRY, BeliefState, Estimate
from blackout.info.reports import (
    RELIABILITY_PCT,
    Fact,
    FactType,
    ReportView,
    SourceClass,
    close_enough,
)

TRUSTED_PCT = RELIABILITY_PCT[SourceClass.FIELD]


@dataclass(frozen=True, slots=True)
class Claim:
    fact: Fact
    report: ReportView


def agree(a: Fact, b: Fact) -> bool:
    """Whether two facts make the same claim (numbers within 10% of each other)."""
    if (a.type, a.subject) != (b.type, b.subject):
        return False
    if isinstance(a.value, int) and isinstance(b.value, int):
        return close_enough(a.value, b.value) or close_enough(b.value, a.value)
    return a.value == b.value


@dataclass(slots=True)
class SourceFilter:
    """Remembers every claim it has heard, and which unverified ones still wait."""

    heard: list[Claim] = field(default_factory=list)
    parked: list[Claim] = field(default_factory=list)

    def take(self, belief: BeliefState, facts: Sequence[Fact], report: ReportView) -> None:
        """Believe what the filter accepts from ``report``; park the rest."""
        claims = [Claim(fact, report) for fact in facts]
        for claim in claims:
            if self.trusted(belief, claim):
                belief.apply_facts([claim.fact], _yes, report=report)
            else:
                self.parked.append(claim)
        self.heard += claims

    def trusted(self, belief: BeliefState, claim: Claim) -> bool:
        if RELIABILITY_PCT[claim.report.source_class] >= TRUSTED_PCT:
            return True
        return self._corroborated(belief, claim)

    def recheck(self, belief: BeliefState, confirmed: Sequence[str]) -> None:
        """Drop parked claims a confirm reply has settled; believe those now corroborated."""
        waiting: list[Claim] = []
        for claim in self.parked:
            if claim.report.id in confirmed:
                continue
            if self._corroborated(belief, claim):
                belief.apply_facts([claim.fact], _yes, report=claim.report)
            else:
                waiting.append(claim)
        self.parked = waiting

    def _corroborated(self, belief: BeliefState, claim: Claim) -> bool:
        fact, report = claim.fact, claim.report
        for other in self.heard:
            if other.report.source != report.source and agree(other.fact, fact):
                return True
        return _telemetry_agrees(belief, fact)


def _yes(fact: Fact, report: ReportView) -> bool:
    return True


def _telemetry_agrees(belief: BeliefState, fact: Fact) -> bool:
    subject = fact.subject
    match fact.type:
        case FactType.ROAD_BLOCKED:
            return _from_telemetry(belief.roads.get(subject), BLOCKED)
        case FactType.SUBSTATION_STATUS:
            known = belief.substations.get(subject)
            return known is not None and _from_telemetry(known.state, fact.value)
        case FactType.HOSPITAL_FUEL:
            fuel = belief.fuel_now(subject)
            return (
                fuel is not None
                and isinstance(fact.value, int)
                and close_enough(fact.value, max(fuel, 1))
            )
        case FactType.BURN_SPIKE:
            known = belief.hospitals.get(subject)
            return known is not None and _from_telemetry(known.burn_lpm, fact.value)
        case FactType.DEPOT_STOCK:
            known = belief.depots.get(subject)
            value = None if known is None or known.stock_l is None else known.stock_l.value
            return (
                isinstance(value, int)
                and isinstance(fact.value, int)
                and close_enough(fact.value, max(value, 1))
            )
        case _:
            return False


def _from_telemetry(estimate: Estimate | None, value: object) -> bool:
    return estimate is not None and estimate.source == TELEMETRY and estimate.value == value
