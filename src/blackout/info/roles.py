"""Roles: which slice of the world each contestant role sees (docs/PRD.md section 8.4).

Single agents and baselines play the ``all`` role, the union of the four specialist roles.
Telemetry is filtered by section; reports are routed by the channel they came in on, never by
what they say (routing by content would leak which reports matter).
"""

from enum import StrEnum

from blackout.info.reports import Channel

ROLE_ALL = "all"
POWER = "power"
HOSPITALS = "hospitals"
FUEL = "fuel"
TRANSPORT = "transport"

ROLES = (ROLE_ALL, POWER, HOSPITALS, FUEL, TRANSPORT)
TEAM_ROLES = (HOSPITALS, POWER, FUEL, TRANSPORT)
"""The specialist roles, in their fixed turn order (PRD section 8.9)."""


class Section(StrEnum):
    """A part of the telemetry."""

    TRUCKS = "trucks"
    CREWS = "crews"
    HOSPITALS = "hospitals"
    DEPOTS = "depots"
    SUBSTATIONS = "substations"
    ZONES = "zones"
    ROADS = "roads"


SCOPES: dict[str, frozenset[Section]] = {
    ROLE_ALL: frozenset(Section),
    POWER: frozenset({Section.SUBSTATIONS, Section.ZONES, Section.CREWS}),
    HOSPITALS: frozenset({Section.HOSPITALS}),
    FUEL: frozenset({Section.DEPOTS, Section.TRUCKS}),  # truck loads, read-only (no ownership)
    TRANSPORT: frozenset({Section.TRUCKS, Section.ROADS}),
}

CHANNEL_ROLE: dict[Channel, str | None] = {
    Channel.CREW_RADIO: POWER,
    Channel.HOSPITAL_STAFF: HOSPITALS,
    Channel.POLICE_RADIO: TRANSPORT,
    Channel.DEPOT_STAFF: FUEL,
    Channel.PUBLIC: None,  # everyone
}


def check_role(role: str) -> None:
    if role not in SCOPES:
        raise ValueError(f"unknown role {role!r}; roles are {', '.join(ROLES)}")


def sees(role: str, section: Section) -> bool:
    """True if ``role``'s telemetry includes ``section``."""
    return section in SCOPES[role]


def receives(role: str, channel: Channel) -> bool:
    """True if reports on ``channel`` are routed to ``role``."""
    target = CHANNEL_ROLE[channel]
    return role == ROLE_ALL or target is None or target == role
