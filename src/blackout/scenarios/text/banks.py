"""Phrasing banks: YAML files of report phrasings, one per fact type (docs/PRD.md section 8.4).

A bank is a directory (``data/phrasing/train`` or, from M8, the human-written ``test``) with
one ``<fact_type>.yaml`` per fact type, a ``_noise.yaml`` lexicon for the noise operators and,
for L3 scenarios, an ``_injection.yaml`` of injection texts.
Loading checks every phrasing: known slots only, the subject slot always, the value slot for
facts that carry one, and a state for each ``substation_status`` phrasing.
"""

import string
from pathlib import Path
from typing import Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from blackout.info.reports import SUBSTATION_STATES, Channel, FactType, InjectionKind

TRAIN_BANK = Path("data") / "phrasing" / "train"
MIN_PHRASINGS = 8
"""Every bank has at least 8 phrasings per fact type: the test bank's minimum (PRD 8.4)."""
TRAIN_MIN_PHRASINGS = 10
"""The train bank has at least 10 (PRD 8.4; tested)."""

SUBJECT_SLOT: dict[FactType, str] = {
    FactType.ROAD_BLOCKED: "road",
    FactType.ROAD_CLEARED: "road",
    FactType.SUBSTATION_STATUS: "substation",
    FactType.REPAIR_ETA: "substation",
    FactType.HOSPITAL_FUEL: "hospital",
    FactType.BURN_SPIKE: "hospital",
    FactType.DEPOT_STOCK: "depot",
}
VALUE_SLOT: dict[FactType, str] = {
    FactType.REPAIR_ETA: "duration",
    FactType.HOSPITAL_FUEL: "liters",
    FactType.BURN_SPIKE: "rate",
    FactType.DEPOT_STOCK: "liters",
}
EXTRA_SLOTS = frozenset({"when"})
"""Slots the renderer fills for every fact type: ``when`` is a relative time."""


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Phrasing(_Model):
    id: str = Field(pattern=r"^[a-z]{2}\d{2}$")
    text: str
    value: str | None = Field(default=None, description="The state it's for (substation_status).")
    channels: tuple[Channel, ...] = ()

    @property
    def slots(self) -> frozenset[str]:
        return frozenset(
            name for _, name, _, _ in string.Formatter().parse(self.text) if name is not None
        )

    def fits(self, channel: Channel) -> bool:
        return not self.channels or channel in self.channels


class FactBank(_Model):
    fact_type: FactType
    vocab: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    phrasings: tuple[Phrasing, ...]

    @model_validator(mode="after")
    def _check(self) -> Self:
        kind = self.fact_type
        allowed = {SUBJECT_SLOT[kind], *EXTRA_SLOTS, *self.vocab}
        if kind in VALUE_SLOT:
            allowed.add(VALUE_SLOT[kind])
        problems: list[str] = []
        ids = [p.id for p in self.phrasings]
        problems += [f"duplicate phrasing id {i}" for i in sorted(set(ids)) if ids.count(i) > 1]
        for phrasing in self.phrasings:
            slots = phrasing.slots
            if unknown := sorted(slots - allowed):
                problems.append(f"{phrasing.id}: unknown slots {unknown}")
            if SUBJECT_SLOT[kind] not in slots:
                problems.append(f"{phrasing.id}: no {{{SUBJECT_SLOT[kind]}}} slot")
            if kind in VALUE_SLOT and VALUE_SLOT[kind] not in slots:
                problems.append(f"{phrasing.id}: no {{{VALUE_SLOT[kind]}}} slot")
            if (kind is FactType.SUBSTATION_STATUS) != (phrasing.value is not None):
                problems.append(f"{phrasing.id}: a state value is needed exactly for {kind}")
            if phrasing.value is not None and phrasing.value not in SUBSTATION_STATES:
                problems.append(f"{phrasing.id}: unknown state {phrasing.value!r}")
        if len(self.phrasings) < MIN_PHRASINGS:
            problems.append(f"{len(self.phrasings)} phrasings, at least {MIN_PHRASINGS} needed")
        if problems:
            raise ValueError(f"{kind}: " + "; ".join(problems))
        return self

    def candidates(self, channel: Channel, value: object) -> tuple[Phrasing, ...]:
        """Phrasings for this channel (and state); all of the state's if none fit the channel."""
        matching = tuple(
            p
            for p in self.phrasings
            if self.fact_type is not FactType.SUBSTATION_STATUS or p.value == value
        )
        fitting = tuple(p for p in matching if p.fits(channel))
        return fitting or matching


class NoiseRates(_Model):
    typo_pct: tuple[int, int]
    abbreviation_pct: int = Field(ge=0, le=100)
    slang_pct: int = Field(ge=0, le=100)
    hedge_pct: int = Field(ge=0, le=100)
    open_pct: int = Field(ge=0, le=100)
    filler_pct: int = Field(ge=0, le=100)
    close_pct: int = Field(ge=0, le=100)


class Hedges(_Model):
    prefix: tuple[str, ...]
    suffix: tuple[str, ...]


class Chatter(_Model):
    open: dict[Channel, tuple[str, ...]]
    filler: tuple[str, ...]
    close: dict[Channel, tuple[str, ...]]
    connectors: tuple[str, ...]

    @model_validator(mode="after")
    def _every_channel(self) -> Self:
        for part in (self.open, self.close):
            missing = sorted(set(Channel) - set(part))
            if missing:
                raise ValueError(f"chatter has no lines for channels {missing}")
        return self


class NoiseLexicon(_Model):
    rates: NoiseRates
    abbreviations: dict[str, str]
    slang: dict[str, tuple[str, ...]]
    hedges: Hedges
    chatter: Chatter


INJECTION_SLOTS: dict[InjectionKind, frozenset[str]] = {
    InjectionKind.SHED_ZONE: frozenset({"zone"}),
    InjectionKind.DISPATCH: frozenset({"hospital", "depot"}),
    InjectionKind.ASSIGN_CREW: frozenset({"substation"}),
}
"""The target slot(s) an injection phrasing of each kind may use (exactly one of them)."""
MIN_INJECTION_PHRASINGS = 4
"""At least this many injection phrasings per kind (needed for L3)."""


class InjectionPhrasing(_Model):
    id: str = Field(pattern=r"^in\d{2}$")
    kind: InjectionKind
    text: str

    @property
    def slots(self) -> frozenset[str]:
        return frozenset(
            name for _, name, _, _ in string.Formatter().parse(self.text) if name is not None
        )

    @property
    def target_slot(self) -> str:
        (slot,) = self.slots & INJECTION_SLOTS[self.kind]
        return slot


class InjectionBank(_Model):
    """L3 injection texts (``_injection.yaml``): each asks for one action on one target,
    claiming the authority in ``{authority}``."""

    phrasings: tuple[InjectionPhrasing, ...]

    @model_validator(mode="after")
    def _check(self) -> Self:
        problems: list[str] = []
        ids = [p.id for p in self.phrasings]
        problems += [f"duplicate phrasing id {i}" for i in sorted(set(ids)) if ids.count(i) > 1]
        for p in self.phrasings:
            allowed = {"authority", *INJECTION_SLOTS[p.kind]}
            if unknown := sorted(p.slots - allowed):
                problems.append(f"{p.id}: unknown slots {unknown}")
            if len(p.slots & INJECTION_SLOTS[p.kind]) != 1:
                options = " or ".join(f"{{{s}}}" for s in sorted(INJECTION_SLOTS[p.kind]))
                problems.append(f"{p.id}: needs exactly one target slot, {options}")
        for kind in InjectionKind:
            count = sum(1 for p in self.phrasings if p.kind is kind)
            if count < MIN_INJECTION_PHRASINGS:
                problems.append(
                    f"{count} {kind} phrasings, at least {MIN_INJECTION_PHRASINGS} needed"
                )
        if problems:
            raise ValueError("injections: " + "; ".join(problems))
        return self

    def candidates(self, kind: InjectionKind, slot: str) -> tuple[InjectionPhrasing, ...]:
        return tuple(p for p in self.phrasings if p.kind is kind and p.target_slot == slot)


class PhrasingBank(_Model):
    """A loaded bank: one ``FactBank`` per fact type, the noise lexicon, and (for L3) the
    injection texts."""

    name: str
    facts: dict[FactType, FactBank]
    noise: NoiseLexicon
    injection: InjectionBank | None = None

    @model_validator(mode="after")
    def _complete(self) -> Self:
        missing = sorted(set(FactType) - set(self.facts))
        if missing:
            raise ValueError(f"bank {self.name!r} has no phrasings for {missing}")
        return self


def load_bank(path: Path = TRAIN_BANK) -> PhrasingBank:
    """Load and check a phrasing bank directory. Its name is the directory's name."""
    facts: dict[FactType, FactBank] = {}
    for kind in FactType:
        file = path / f"{kind.value}.yaml"
        if file.is_file():
            facts[kind] = FactBank.model_validate(_read(file))
            if facts[kind].fact_type is not kind:
                raise ValueError(f"{file} declares fact_type {facts[kind].fact_type}")
    noise = NoiseLexicon.model_validate(_read(path / "_noise.yaml"))
    injection_file = path / "_injection.yaml"
    injection = (
        InjectionBank.model_validate(_read(injection_file)) if injection_file.is_file() else None
    )
    return PhrasingBank(name=path.name, facts=facts, noise=noise, injection=injection)


def _read(path: Path) -> object:
    with path.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)
