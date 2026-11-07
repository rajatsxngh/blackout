"""Turn a report's facts into L1 free text: pick phrasings, fill slots, add noise.

Each report draws from its own random stream, ``Rng.derive(scenario_seed, "report",
report_id)`` (PRD 8.4), so changing one report's wording never shifts another's.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from blackout.info.reports import Channel, Fact, FactType
from blackout.scenarios.seeding import Rng
from blackout.scenarios.text.banks import SUBJECT_SLOT, VALUE_SLOT, PhrasingBank
from blackout.scenarios.text.noise import apply_noise
from blackout.scenarios.text.slots import (
    Gazette,
    duration_phrases,
    liters_phrases,
    rate_phrases,
    when_phrases,
)


@dataclass(frozen=True, slots=True)
class TimedFact:
    """A claim, and the tick from which it has been true (for relative times like "just now")."""

    fact: Fact
    since: int


@dataclass(frozen=True, slots=True)
class Rendered:
    text: str
    phrasings: list[str]
    noise: list[str]


def report_rng(scenario_seed: int, report_id: str) -> Rng:
    return Rng.derive(scenario_seed, "report", report_id)


def callsign(channel: Channel, source: str) -> str:
    """How the speaker names themself on the radio: "Unit 12", "Charge nurse Okafor"."""
    head, _, tail = source.partition(", ")
    if tail.startswith("Unit "):
        return tail
    return head.removesuffix(" center")


def render(
    facts: Sequence[TimedFact],
    *,
    tick: int,
    channel: Channel,
    source: str,
    gazette: Gazette,
    bank: PhrasingBank,
    rng: Rng,
    noise: bool = True,
) -> Rendered:
    """Text for a report sent at ``tick`` that claims ``facts`` (one sentence or more each)."""
    parts: list[str] = []
    used: list[str] = []
    for timed in facts:
        fact = timed.fact
        fact_bank = bank.facts[fact.type]
        phrasing = rng.pick(fact_bank.candidates(channel, fact.value))
        slots = {SUBJECT_SLOT[fact.type]: rng.pick(_subject_phrases(gazette, fact))}
        if fact.type in VALUE_SLOT:
            slots[VALUE_SLOT[fact.type]] = rng.pick(_value_phrases(fact))
        if "when" in phrasing.slots:
            slots["when"] = rng.pick(when_phrases(tick - timed.since))
        for name in sorted(phrasing.slots & set(fact_bank.vocab)):
            slots[name] = rng.pick(fact_bank.vocab[name])
        parts.append(phrasing.text.format_map(slots))
        used.append(f"{fact.type.value}/{phrasing.id}")
    if not noise:
        return Rendered(" ".join(p[:1].upper() + p[1:] for p in parts), used, [])
    text, applied = apply_noise(parts, channel, callsign(channel, source), bank.noise, rng)
    return Rendered(text, used, applied)


def _subject_phrases(gazette: Gazette, fact: Fact) -> list[str]:
    match fact.type:
        case FactType.ROAD_BLOCKED | FactType.ROAD_CLEARED:
            return gazette.road_phrases(fact.subject)
        case FactType.SUBSTATION_STATUS | FactType.REPAIR_ETA:
            return gazette.substation_phrases(fact.subject)
        case FactType.HOSPITAL_FUEL | FactType.BURN_SPIKE:
            return gazette.hospital_phrases(fact.subject)
        case FactType.DEPOT_STOCK:
            return gazette.depot_phrases(fact.subject)


def _value_phrases(fact: Fact) -> list[str]:
    assert isinstance(fact.value, int)
    match fact.type:
        case FactType.REPAIR_ETA:
            return duration_phrases(fact.value)
        case FactType.BURN_SPIKE:
            return rate_phrases(fact.value)
        case _:
            return liters_phrases(fact.value)
