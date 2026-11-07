"""Radio reports: what they claim, who sent them, and what contestants are shown.

A report is a scenario event (docs/PRD.md section 8.4). It carries structured ``facts`` (its
claims) and, from level L1 up, free ``text``. What a contestant sees depends on the scenario
level: the facts at L0, the text alone at L1 and above. ``truth`` is ground truth about the
report itself (is it true, what cue betrays it if not) and is never shown to contestants.

Reliabilities are whole percentages, so beliefs stay in integer units.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, NonNegativeInt, model_validator


class Channel(StrEnum):
    """Where a report came in. Reports are routed to roles by channel, never by content."""

    CREW_RADIO = "crew_radio"
    HOSPITAL_STAFF = "hospital_staff"
    POLICE_RADIO = "police_radio"
    DEPOT_STAFF = "depot_staff"
    PUBLIC = "public"


class SourceClass(StrEnum):
    VERIFIED = "verified"
    FIELD = "field"
    UNVERIFIED = "unverified"


RELIABILITY_PCT: dict[SourceClass, int] = {
    SourceClass.VERIFIED: 98,
    SourceClass.FIELD: 85,
    SourceClass.UNVERIFIED: 50,
}
"""Published to every contestant (PRD section 8.4): how often each class of source is right."""


class FactType(StrEnum):
    ROAD_BLOCKED = "road_blocked"
    ROAD_CLEARED = "road_cleared"
    SUBSTATION_STATUS = "substation_status"
    REPAIR_ETA = "repair_eta"
    HOSPITAL_FUEL = "hospital_fuel"
    BURN_SPIKE = "burn_spike"
    DEPOT_STOCK = "depot_stock"


SUBJECT_KIND: dict[FactType, str] = {
    FactType.ROAD_BLOCKED: "road",
    FactType.ROAD_CLEARED: "road",
    FactType.SUBSTATION_STATUS: "substation",
    FactType.REPAIR_ETA: "substation",
    FactType.HOSPITAL_FUEL: "hospital",
    FactType.BURN_SPIKE: "hospital",
    FactType.DEPOT_STOCK: "depot",
}
"""What a fact's subject must be: a road segment, substation, hospital or depot ID."""

SUBSTATION_STATES = ("online", "failed", "tripped", "repairing")

_UNITS: dict[FactType, str] = {
    FactType.REPAIR_ETA: "min",
    FactType.HOSPITAL_FUEL: "L",
    FactType.BURN_SPIKE: "L/min",
    FactType.DEPOT_STOCK: "L",
}


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Fact(_Model):
    """One claim. ``value`` is a whole number (minutes, liters, L/min), a substation state, or
    nothing (road facts)."""

    type: FactType
    subject: str
    value: int | str | None = None

    @model_validator(mode="after")
    def _value_fits_type(self) -> Self:
        kind = self.type
        if kind in (FactType.ROAD_BLOCKED, FactType.ROAD_CLEARED):
            if self.value is not None:
                raise ValueError(f"{kind} takes no value")
        elif kind is FactType.SUBSTATION_STATUS:
            if self.value not in SUBSTATION_STATES:
                raise ValueError(f"{kind} needs a state, one of {', '.join(SUBSTATION_STATES)}")
        elif not isinstance(self.value, int) or isinstance(self.value, bool) or self.value < 0:
            raise ValueError(f"{kind} needs a whole number of {_UNITS[kind]}, at least 0")
        return self

    def describe(self) -> str:
        """A short plain description, e.g. ``hospital_fuel H1 = 120 L``."""
        if self.value is None:
            return f"{self.type} {self.subject}"
        unit = _UNITS.get(self.type)
        return f"{self.type} {self.subject} = {self.value}{'' if unit is None else ' ' + unit}"


class ReportTruth(_Model):
    """Ground truth about a report, for scoring only. Contestants never see it."""

    is_true: bool = True
    cue: str | None = None
    stale_of: str | None = None


class Report(_Model):
    """A radio report, as stored in the scenario's event list (``kind: "report"``)."""

    kind: Literal["report"] = "report"
    tick: NonNegativeInt
    id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]*$")
    channel: Channel
    source_class: SourceClass
    source: str = Field(description="Who is talking, e.g. 'Officer Diaz, Unit 12'.")
    facts: list[Fact] = Field(min_length=1, description="What the report claims (ground truth).")
    text: str | None = Field(default=None, description="Free text, shown instead of facts at L1+.")
    truth: ReportTruth = Field(default_factory=ReportTruth)


@dataclass(frozen=True, slots=True)
class ReportView:
    """A report as a contestant receives it: ``facts`` at L0, ``text`` alone at L1 and up."""

    id: str
    tick: int
    channel: Channel
    source_class: SourceClass
    source: str
    facts: tuple[Fact, ...] | None
    text: str | None


def view_of(report: Report, level: int) -> ReportView:
    """What a contestant is shown of ``report`` in a scenario of the given level."""
    clean = level == 0
    return ReportView(
        id=report.id,
        tick=report.tick,
        channel=report.channel,
        source_class=report.source_class,
        source=report.source,
        facts=tuple(report.facts) if clean else None,
        text=None if clean else report.text,
    )


@dataclass(frozen=True, slots=True)
class ClaimCheck:
    """How one claim of a confirmed report compares with the truth at reply time."""

    fact: Fact
    holds: bool
    actual: int | str | None


@dataclass(frozen=True, slots=True)
class ConfirmReply:
    """Verified dispatch's answer to ``confirm_report``: the true status of every claim."""

    report_id: str
    requested_at: int
    tick: int
    checks: tuple[ClaimCheck, ...]

    @property
    def holds(self) -> bool:
        return all(check.holds for check in self.checks)

    def summary(self) -> str:
        held = sum(1 for check in self.checks if check.holds)
        return f"{held} of {len(self.checks)} claims hold"


@dataclass(frozen=True, slots=True)
class ConfirmDue:
    """Runtime queue item: a confirm reply for ``role`` falls due."""

    report_id: str
    role: str
    requested_at: int


NUMERIC_TOLERANCE_PCT = 10
"""A numeric claim holds if it's within 10% of the truth (the parse-F1 tolerance, PRD 8.4)."""


def close_enough(claimed: int, actual: int) -> bool:
    return abs(claimed - actual) * 100 <= NUMERIC_TOLERANCE_PCT * actual
