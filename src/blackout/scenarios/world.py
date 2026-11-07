"""Seed -> world: a random city drawn from the city config (docs/PRD.md section 8.1).

``build_world`` gives a calm city: every substation online with headroom, every zone powered.
A template then breaks it (blackout.scenarios.templates). Both work on a ``Draft``, a mutable
collection of the scenario's spec objects plus the reports planned so far.
"""

from dataclasses import dataclass, field
from itertools import pairwise

from blackout.info.reports import Channel, CueKind, Fact, InjectionKind, SourceClass
from blackout.scenarios import names
from blackout.scenarios.config import CityConfig
from blackout.scenarios.schema import (
    CitySpec,
    CrewSpec,
    DepotSpec,
    HospitalSpec,
    SubstationSpec,
    TruckSpec,
    ZoneSpec,
)
from blackout.scenarios.seeding import Rng
from blackout.scenarios.text.render import TimedFact
from blackout.scenarios.text.slots import Gazette, Place
from blackout.sim.city import (
    STREETS,
    City,
    EdgeId,
    NodeId,
    build_grid_city,
    edge_id,
    edge_ids,
    node_id,
)
from blackout.sim.events import BurnRateChange, RoadBlock, RoadClear, SubstationFail, TruckBreakdown
from blackout.sim.routing import path_minutes, shortest_path

type PhysicalEvent = RoadBlock | RoadClear | TruckBreakdown | BurnRateChange | SubstationFail


class Infeasible(Exception):  # noqa: N818 (it's an outcome, not a bug)
    """This world can't host the template's situation; the generator re-rolls."""


@dataclass(slots=True, eq=False)
class PlannedCue:
    """A cue that betrays a false planned report; ``ref`` is the contradicting report."""

    kind: CueKind
    ref: "PlannedReport | None" = None
    note: str = ""


@dataclass(slots=True, eq=False)
class PlannedInjection:
    """An L3 injection before it gets text: what it asks for, of what, for how long."""

    kind: InjectionKind
    target: str
    start: int
    end: int
    authority: str


@dataclass(slots=True, eq=False)
class PlannedReport:
    """A report before it gets an ID and (at L1) text.

    ``level`` is the lowest level that carries it: 0 for the reports every level of a seed
    shares, 2 for false, stale and correcting reports, 3 for injections. Planned reports are
    compared by identity (``eq=False``), so cues and corrections can point at them.
    """

    tick: int
    channel: Channel
    source_class: SourceClass
    source: str
    facts: list[TimedFact]
    level: int = 0
    is_true: bool = True
    cues: list[PlannedCue] = field(default_factory=list)
    stale_of: Fact | None = None
    corrects: "PlannedReport | None" = None
    injection: PlannedInjection | None = None


@dataclass(frozen=True, slots=True)
class FuelReading:
    """A planned report that reads a hospital's tank. Its value is filled in from the
    hospital's starting fuel when the world is final (calibration can change that fuel)."""

    report: PlannedReport
    hospital_id: str
    unpowered_from: int | None  # tick the generator started; None: on the grid until then


@dataclass(slots=True)
class Draft:
    config: CityConfig
    city: CitySpec
    hospitals: list[HospitalSpec]
    depots: list[DepotSpec]
    trucks: list[TruckSpec]
    crews: list[CrewSpec]
    substations: list[SubstationSpec]
    zones: list[ZoneSpec]
    events: list[PhysicalEvent] = field(default_factory=list)
    reports: list[PlannedReport] = field(default_factory=list)
    story: list[str] = field(default_factory=list)
    target: str | None = None  # the hospital whose starting fuel calibration sets
    fuel_readings: list[FuelReading] = field(default_factory=list)

    # --- lookups ---------------------------------------------------------------------------

    def built_city(self) -> City:
        spec = self.city
        return build_grid_city(
            spec.width,
            spec.height,
            default_minutes=spec.default_travel_minutes,
            minutes=spec.travel_minutes,
            landmarks=spec.landmarks,
        )

    def gazette(self) -> Gazette:
        return Gazette(
            city=self.built_city(),
            hospitals={h.id: Place(h.name, h.node) for h in self.hospitals},
            depots={d.id: Place(d.name, d.node) for d in self.depots},
            substations={s.id: Place(s.name, s.node) for s in self.substations},
        )

    def hospital(self, hospital_id: str) -> HospitalSpec:
        return next(h for h in self.hospitals if h.id == hospital_id)

    def substation(self, substation_id: str) -> SubstationSpec:
        return next(s for s in self.substations if s.id == substation_id)

    def zone_of(self, node: NodeId) -> ZoneSpec:
        return next(z for z in self.zones if node in z.nodes)

    # --- edits -----------------------------------------------------------------------------

    def update_hospital(self, hospital_id: str, **changes: object) -> None:
        self.hospitals = [
            h.model_copy(update=changes) if h.id == hospital_id else h for h in self.hospitals
        ]

    def update_substation(self, substation_id: str, **changes: object) -> None:
        self.substations = [
            s.model_copy(update=changes) if s.id == substation_id else s for s in self.substations
        ]

    def update_zone(self, zone_id: str, **changes: object) -> None:
        self.zones = [z.model_copy(update=changes) if z.id == zone_id else z for z in self.zones]

    def update_truck(self, truck_id: str, **changes: object) -> None:
        self.trucks = [t.model_copy(update=changes) if t.id == truck_id else t for t in self.trucks]

    # --- power -----------------------------------------------------------------------------

    def loads(self, down: frozenset[str]) -> dict[str, int]:
        """Each working substation's load if the substations in ``down`` are out (no shedding)."""
        loads = {s.id: 0 for s in self.substations if s.id not in down}
        for zone in self.zones:
            for feeder in (zone.primary, zone.alternate):
                if feeder is not None and feeder not in down:
                    loads[feeder] += zone.demand_kw
                    break
        return loads

    def fit_capacity(self, down: frozenset[str]) -> None:
        """Raise capacities so no working substation is overloaded with ``down`` out."""
        cfg = self.config.substations
        for substation_id, load in sorted(self.loads(down).items()):
            needed = _round_up(load + cfg.headroom_kw, cfg.capacity_step_kw)
            if self.substation(substation_id).capacity_kw < needed:
                self.update_substation(substation_id, capacity_kw=needed)

    # --- reports ---------------------------------------------------------------------------

    def plan(
        self,
        tick: int,
        channel: Channel,
        source_class: SourceClass,
        source: str,
        *facts: TimedFact,
        level: int = 0,
    ) -> PlannedReport | None:
        """Plan a report; None if it would come at or after the horizon."""
        if tick >= self.config.horizon:
            return None
        report = PlannedReport(tick, channel, source_class, source, list(facts), level=level)
        self.reports.append(report)
        return report

    def set_fuel(self, hospital_id: str, fuel_l: int) -> None:
        """Set a hospital's starting fuel and re-read every planned reading of its tank."""
        self.update_hospital(hospital_id, fuel_l=fuel_l)
        for reading in self.fuel_readings:
            if reading.hospital_id != hospital_id:
                continue
            hospital = self.hospital(hospital_id)
            timed = reading.report.facts[0]
            since = reading.unpowered_from
            burned = 0 if since is None else hospital.burn_lpm * max(0, timed.since - since)
            level = max(0, hospital.fuel_l - burned)
            reading.report.facts[0] = TimedFact(
                fact(timed.fact.type.value, hospital_id, level), timed.since
            )

    def hospitals_at_risk(self) -> list[str]:
        """Hospitals whose zone has no working feeder at the start (on their generators)."""
        down = {s.id for s in self.substations if s.state != "online"}
        risky = []
        for hospital in self.hospitals:
            zone = self.zone_of(hospital.node)
            feeders = [f for f in (zone.primary, zone.alternate) if f is not None]
            if all(f in down for f in feeders):
                risky.append(hospital.id)
        return risky


def fastest(
    city: City, a: NodeId, b: NodeId, avoid: frozenset[EdgeId] = frozenset()
) -> tuple[list[NodeId], int] | None:
    """The fastest path and its minutes, or None if ``b`` can't be reached."""
    path = shortest_path(city, a, b, avoid)
    return None if path is None else (path, path_minutes(city, path))


def path_edges(path: list[NodeId]) -> list[EdgeId]:
    return [edge_id(a, b) for a, b in pairwise(path)]


def _round_up(value: int, step: int) -> int:
    return -(-value // step) * step


# --- the calm world -----------------------------------------------------------------------


def build_world(config: CityConfig, rng: Rng) -> Draft:
    """Draw a calm city from ``config``: all power on, every substation with headroom."""
    width, height = config.grid.width, config.grid.height
    city = _draw_city(config, rng)
    zones = _draw_zones(config, rng)
    substations, zones = _draw_substations(config, rng, zones)
    hospitals = _draw_hospitals(config, rng, zones)
    taken = {h.node for h in hospitals} | {s.node for s in substations}
    depots = _draw_depots(config, rng, width, height, taken)
    trucks = [
        TruckSpec(
            id=f"T{i + 1}", node=depots[i % len(depots)].node, capacity_l=config.trucks.capacity_l
        )
        for i in range(config.trucks.count)
    ]
    crew_nodes = rng.sample(
        sorted(node_id(x, y) for x in range(width) for y in range(height)), config.crews.count
    )
    crews = [CrewSpec(id=f"C{i + 1}", node=node) for i, node in enumerate(crew_nodes)]
    return Draft(config, city, hospitals, depots, trucks, crews, substations, zones)


def _draw_city(config: CityConfig, rng: Rng) -> CitySpec:
    width, height = config.grid.width, config.grid.height
    street = rng.between(1, width - 2) if width > 2 else 0
    avenue = rng.between(1, height - 2) if height > 2 else 0
    minutes: dict[str, int] = {}
    for segment in edge_ids(width, height):
        a, b = segment.split("-")
        arterial = (a[0] == b[0] and a[0] == node_id(street, 0)[0]) or (
            a[1:] == b[1:] and int(a[1:]) == avenue + 1
        )
        span = config.arterials.travel_minutes if arterial else config.travel_minutes
        minutes[segment] = rng.between(*span)
    nodes = sorted(node_id(x, y) for x in range(width) for y in range(height))
    places = rng.sample(names.LANDMARKS, config.landmarks)
    spots = rng.sample(nodes, config.landmarks)
    return CitySpec(
        width=width,
        height=height,
        default_travel_minutes=config.travel_minutes[0],
        travel_minutes=minutes,
        landmarks=dict(sorted(zip(places, spots, strict=True))),
    )


def _draw_zones(config: CityConfig, rng: Rng) -> list[ZoneSpec]:
    columns, rows = config.zones.blocks
    block_w, block_h = config.grid.width // columns, config.grid.height // rows
    count = columns * rows
    order = rng.shuffled(range(count))
    critical = set(order[: config.zones.critical])
    essential = set(order[config.zones.critical : config.zones.critical + config.zones.essential])
    low, high = config.zones.demand_kw
    zones: list[ZoneSpec] = []
    for index in range(count):
        row, column = divmod(index, columns)
        nodes = sorted(
            node_id(x, y)
            for x in range(column * block_w, (column + 1) * block_w)
            for y in range(row * block_h, (row + 1) * block_h)
        )
        name = names.ZONES_3X3[row][column] if (columns, rows) == (3, 3) else f"Zone {index + 1}"
        priority = (
            "critical"
            if index in critical
            else "essential"
            if index in essential
            else "residential"
        )
        zones.append(
            ZoneSpec(
                id=f"Z{index + 1}",
                name=name,
                nodes=nodes,
                demand_kw=rng.between(low, high, config.zones.demand_step_kw),
                priority=priority,
                primary=f"S{column + 1}",  # placeholder until substations exist
            )
        )
    return zones


def _draw_substations(
    config: CityConfig, rng: Rng, zones: list[ZoneSpec]
) -> tuple[list[SubstationSpec], list[ZoneSpec]]:
    """One substation per column of zones, sited in one of that column's zones. Each zone's
    primary is its column's substation; its alternate is a neighboring column's."""
    columns = config.substation_count
    cfg = config.substations
    station_names = rng.sample(names.SUBSTATIONS, columns)
    by_column: dict[int, list[ZoneSpec]] = {c: [] for c in range(columns)}
    for index, zone in enumerate(zones):
        by_column[index % columns].append(zone)
    wired: list[ZoneSpec] = []
    for index, zone in enumerate(zones):
        column = index % columns
        neighbors = [c for c in (column - 1, column + 1) if 0 <= c < columns]
        wired.append(zone.model_copy(update={"alternate": f"S{rng.pick(neighbors) + 1}"}))
    substations: list[SubstationSpec] = []
    for column in range(columns):
        home = rng.pick(by_column[column])
        load = sum(z.demand_kw for z in by_column[column])
        capacity = max(
            rng.between(*cfg.capacity_kw, cfg.capacity_step_kw),
            _round_up(load + cfg.headroom_kw, cfg.capacity_step_kw),
        )
        substations.append(
            SubstationSpec(
                id=f"S{column + 1}",
                name=station_names[column],
                node=rng.pick(home.nodes),
                capacity_kw=capacity,
            )
        )
    return substations, wired


def _draw_hospitals(config: CityConfig, rng: Rng, zones: list[ZoneSpec]) -> list[HospitalSpec]:
    cfg = config.hospitals
    critical = [z for z in zones if z.priority == "critical"]
    hospital_names = rng.sample(names.HOSPITALS, len(critical))
    return [
        HospitalSpec(
            id=f"H{i + 1}",
            name=hospital_names[i],
            node=rng.pick(zone.nodes),
            tank_l=cfg.tank_l,
            fuel_l=rng.between(*cfg.fuel_l, cfg.fuel_step_l),
            burn_lpm=cfg.burn_lpm,
        )
        for i, zone in enumerate(critical)
    ]


def _draw_depots(
    config: CityConfig, rng: Rng, width: int, height: int, taken: set[NodeId]
) -> list[DepotSpec]:
    cfg = config.depots
    free = [
        node_id(x, y) for x in range(width) for y in range(height) if node_id(x, y) not in taken
    ]
    chosen: list[tuple[int, int]] = []
    for node in rng.shuffled(sorted(free)):
        x, y = _xy(node)
        if all(abs(x - cx) + abs(y - cy) >= cfg.min_separation for cx, cy in chosen):
            chosen.append((x, y))
        if len(chosen) == cfg.count:
            break
    if len(chosen) < cfg.count:
        raise Infeasible(f"can't place {cfg.count} depots {cfg.min_separation} apart")
    depot_names = rng.sample(names.DEPOTS, cfg.count)
    return [
        DepotSpec(
            id=f"D{i + 1}",
            name=depot_names[i],
            node=node_id(x, y),
            stock_l=rng.between(*cfg.stock_l, cfg.stock_step_l),
        )
        for i, (x, y) in enumerate(chosen)
    ]


def _xy(node: NodeId) -> tuple[int, int]:
    return [s[0] for s in STREETS].index(node[0]), int(node[1:]) - 1


# --- report sources -----------------------------------------------------------------------


def police_source(rng: Rng) -> str:
    return f"{rng.pick(names.POLICE_RANKS)} {rng.pick(names.SURNAMES)}, Unit {rng.between(3, 41)}"


def hospital_source(rng: Rng, hospital: HospitalSpec) -> str:
    return f"{rng.pick(names.HOSPITAL_ROLES)} {rng.pick(names.SURNAMES)}, {hospital.name}"


def depot_source(rng: Rng, depot: DepotSpec) -> str:
    return f"{rng.pick(names.DEPOT_ROLES)} {rng.pick(names.SURNAMES)}, {depot.name}"


def crew_source(crew: CrewSpec) -> str:
    return f"Line crew {crew.id.removeprefix('C')}"


def public_source(rng: Rng) -> str:
    return rng.pick(names.PUBLIC_SOURCES)


def fact(kind: str, subject: str, value: int | str | None = None) -> Fact:
    return Fact.model_validate({"type": kind, "subject": subject, "value": value})
