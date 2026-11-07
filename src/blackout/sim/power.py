"""Power flow on the simplest model that still cascades (docs/PRD.md section 8.2).

A zone is fed by its primary substation if that's online, otherwise by its alternate if that's
online, unless the zone is shed. A substation's load is the plain sum of the demand it feeds:
no voltages, no line limits, no reactive power. Overload and tripping live in the engine.
"""

from collections.abc import Mapping

from blackout.sim.entities import Substation, SubstationState, Zone


def resolve_feeds(
    zones: Mapping[str, Zone], substations: Mapping[str, Substation]
) -> dict[str, str | None]:
    """Which substation feeds each zone right now (None: the zone has no power)."""
    feeds: dict[str, str | None] = {}
    for zone_id in sorted(zones):
        zone = zones[zone_id]
        feeds[zone_id] = None
        if zone.shed:
            continue
        for candidate in (zone.primary, zone.alternate):
            if candidate is not None and substations[candidate].state is SubstationState.ONLINE:
                feeds[zone_id] = candidate
                break
    return feeds


def substation_loads(zones: Mapping[str, Zone], feeds: Mapping[str, str | None]) -> dict[str, int]:
    """Total demand, in kW, that each feeding substation carries."""
    loads: dict[str, int] = {}
    for zone_id in sorted(zones):
        feeder = feeds[zone_id]
        if feeder is not None:
            loads[feeder] = loads.get(feeder, 0) + zones[zone_id].demand_kw
    return loads
