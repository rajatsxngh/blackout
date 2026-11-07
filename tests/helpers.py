"""Builders and checkers shared by the tests."""

import gzip
import json
import random
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from blackout.controllers.base import Controller
from blackout.controllers.do_nothing import DoNothingController
from blackout.controllers.scripted import ScriptedController, load_script
from blackout.env import BlackoutEnv
from blackout.info.observation import Observation
from blackout.logs.writer import RecordedRun, record_episode, run_dir_for
from blackout.scenarios.schema import Scenario, load_scenario
from blackout.sim.actions import (
    AnyAction,
    AssignCrew,
    ConfirmReport,
    DispatchTruck,
    Reroute,
    RestoreLoad,
    ShedLoad,
)
from blackout.sim.city import City, edge_id
from blackout.sim.engine import Engine
from blackout.sim.entities import SubstationState, TaskKind, VehicleState
from blackout.sim.hashing import canonical_json
from blackout.sim.power import resolve_feeds, substation_loads
from blackout.sim.routing import shortest_path

ROOT = Path(__file__).resolve().parents[1]
HANDMADE = ROOT / "scenarios" / "handmade"
DEMO = HANDMADE / "hello_blackout.json"
CASCADE = HANDMADE / "cascade.json"
HANDMADE_RUNS = [
    (DEMO, "scripted"),
    (DEMO, "do-nothing"),
    (CASCADE, "scripted"),
    (CASCADE, "do-nothing"),
]


def handmade_controller(scenario_path: Path, contestant: str) -> Controller:
    """The scripted or do-nothing contestant for a hand-made scenario."""
    if contestant == "scripted":
        return ScriptedController(load_script(scenario_path.with_suffix(".script.json")))
    return DoNothingController()


def record(
    scenario: Scenario | Path, controller: Controller, out: Path, *, run_index: int = 0
) -> RecordedRun:
    """Run ``controller`` on ``scenario`` and log it under ``out``."""
    loaded = load_scenario(scenario) if isinstance(scenario, Path) else scenario
    env = BlackoutEnv(loaded)
    run_dir = run_dir_for(out, loaded.id, controller.name, run_index=run_index)
    return record_episode(env, controller, run_dir, run_index=run_index)


def read_lines(log_path: Path) -> list[dict[str, Any]]:
    """A run log's records as plain dicts (for tampering with in tests)."""
    with gzip.open(log_path, "rt", encoding="utf-8") as stream:
        return [json.loads(line) for line in stream]


def write_lines(records: list[dict[str, Any]], path: Path) -> Path:
    """Write records as an uncompressed JSONL run log (the reader accepts both)."""
    text = "".join(json.dumps(r, sort_keys=True, separators=(",", ":")) + "\n" for r in records)
    path.write_text(text, encoding="ascii", newline="\n")
    return path


def make_scenario(**overrides: Any) -> Scenario:
    """A small valid scenario: a 3x3 grid of 2-minute roads, one hospital, depot and truck.

    It has no power grid, so the hospital is off the grid and always on its generator.
    """
    spec: dict[str, Any] = {
        "id": "test",
        "horizon": 60,
        "city": {"width": 3, "height": 3, "default_travel_minutes": 2},
        "hospitals": [
            {
                "id": "H1",
                "name": "Test Hospital",
                "node": "C3",
                "tank_l": 600,
                "fuel_l": 100,
                "burn_lpm": 2,
            }
        ],
        "depots": [{"id": "D1", "name": "Test Depot", "node": "A1", "stock_l": 1000}],
        "trucks": [{"id": "T1", "node": "A1"}],
        "events": [],
    }
    spec.update(overrides)
    return Scenario.model_validate(spec)


def grid(**overrides: Any) -> dict[str, Any]:
    """``make_scenario`` overrides that add a power grid to the 3x3 test city.

    Zones are the three columns. ZA (4 MW, residential) and ZB (4 MW, essential) are fed by S1
    at A2, with S2 as backup. ZC (5 MW, critical, with H1 at C3) is fed by S2 at C2, with S1 as
    backup. Both substations carry 10 MW, so normal loads are S1 8 MW and S2 5 MW. Crew C1
    starts at B2, two minutes from either substation.
    """
    spec: dict[str, Any] = {
        "crews": [{"id": "C1", "node": "B2"}],
        "substations": [
            {"id": "S1", "name": "West Sub", "node": "A2", "capacity_kw": 10000},
            {"id": "S2", "name": "East Sub", "node": "C2", "capacity_kw": 10000},
        ],
        "zones": [
            zone("ZA", "A", 4000, "residential", "S1", "S2"),
            zone("ZB", "B", 4000, "essential", "S1", "S2"),
            zone("ZC", "C", 5000, "critical", "S2", "S1"),
        ],
    }
    spec.update(overrides)
    return spec


def report(
    report_id: str,
    tick: int,
    *facts: dict[str, Any],
    channel: str = "public",
    source_class: str = "field",
    **extra: Any,
) -> dict[str, Any]:
    """A report event. Without facts, it claims road A1-B1 is blocked."""
    claims = list(facts) or [{"type": "road_blocked", "subject": "A1-B1"}]
    return {
        "kind": "report",
        "tick": tick,
        "id": report_id,
        "channel": channel,
        "source_class": source_class,
        "source": f"Source of {report_id}",
        "facts": claims,
    } | extra


def zone(
    zone_id: str, column: str, demand_kw: int, priority: str, primary: str, alternate: str | None
) -> dict[str, Any]:
    """A zone spec covering one column of the 3x3 test city."""
    return {
        "id": zone_id,
        "name": f"Column {column}",
        "nodes": [f"{column}{row}" for row in (1, 2, 3)],
        "demand_kw": demand_kw,
        "priority": priority,
        "primary": primary,
        "alternate": alternate,
    }


def substation(substation_id: str, node: str, capacity_kw: int, **extra: Any) -> dict[str, Any]:
    return {
        "id": substation_id,
        "name": substation_id,
        "node": node,
        "capacity_kw": capacity_kw,
    } | extra


def open_env(**overrides: Any) -> BlackoutEnv:
    """An environment for ``make_scenario(**overrides)``, reset and waiting at tick 0."""
    env = BlackoutEnv(make_scenario(**overrides))
    env.reset()
    return env


class RandomController:
    """Issues random actions, valid or not, from a seeded generator. Tests only."""

    name = "random"

    def __init__(self, seed: int) -> None:
        self._seed = seed
        self._rng = random.Random(seed)

    def reset(self) -> None:
        self._rng = random.Random(self._seed)

    def decide(self, observation: Observation) -> Sequence[AnyAction]:
        rng = self._rng
        city = observation.city
        edges = sorted(city.edges)
        targets = {
            TaskKind.LOAD: [d.id for d in observation.depots] + ["X9"],
            TaskKind.DELIVER: [h.id for h in observation.hospitals] + ["X9"],
            TaskKind.MOVE: sorted(city.nodes),
        }
        actions: list[AnyAction] = []
        for truck in observation.trucks:
            roll = rng.random()
            if roll < 0.4:
                continue
            avoid = tuple(rng.sample(edges, k=rng.randint(0, 2)))
            if roll < 0.8:
                task = rng.choice(list(TaskKind))
                target = rng.choice(targets[task])
                route = None
                if rng.random() < 0.2:  # an explicit path, possibly through hidden blocks
                    route = _route_to(city, truck.anchor, target, observation)
                    avoid = ()
                actions.append(
                    DispatchTruck(
                        truck_id=truck.id,
                        task=task,
                        target_id=target,
                        amount_l=rng.choice([None, None, 50, 200, 900]),
                        route=route,
                        avoid_edges=avoid,
                    )
                )
            else:
                actions.append(Reroute(vehicle_id=truck.id, avoid_edges=avoid))
        substations = [s.id for s in observation.substations] + ["S9"]
        for crew in observation.crews:
            roll = rng.random()
            if roll < 0.5:
                continue
            avoid = tuple(rng.sample(edges, k=rng.randint(0, 1)))
            if roll < 0.85:
                target = rng.choice(substations)
                actions.append(AssignCrew(crew_id=crew.id, substation_id=target, avoid_edges=avoid))
            else:
                actions.append(Reroute(vehicle_id=crew.id, avoid_edges=avoid))
        zones = [z.id for z in observation.zones] + ["Z9"]
        for _ in range(rng.randint(0, 2) if observation.zones else 0):
            switch = rng.choice([ShedLoad, RestoreLoad])
            actions.append(switch(zone_id=rng.choice(zones)))
        reports = [r.id for r in observation.reports] + ["R99"]
        if rng.random() < 0.3:
            actions.append(ConfirmReport(report_id=rng.choice(reports)))
        return actions


def _route_to(
    city: City, start: str, target: str, observation: Observation
) -> tuple[str, ...] | None:
    """The fastest path ignoring all blocks (so it may run into hidden ones), if any."""
    places = {d.id: d.node for d in observation.depots}
    places |= {h.id: h.node for h in observation.hospitals}
    node = places.get(target, target)
    if node not in city.nodes:
        return None
    path = shortest_path(city, start, node)
    return None if path is None else tuple(path)


class InvariantChecker:
    """Checks physics invariants at every recorded tick. Pass it as ``on_tick``."""

    def __init__(self) -> None:
        self.ticks = 0
        self._positions: dict[str, tuple[str, str | None, int]] = {}
        self._blocked: frozenset[str] = frozenset()
        self._burned: dict[str, int] = {}
        self._dark: dict[str, int] = {}
        self._outages: dict[str, int] = {}

    def __call__(self, engine: Engine) -> None:
        assert engine.fuel_in_world() == engine.fuel_total, "fuel was created or destroyed"
        for truck in engine.trucks.values():
            assert 0 <= truck.load_l <= truck.capacity_l, truck
            assert truck.work_left >= 0
        for vehicle in [*engine.trucks.values(), *engine.crews.values()]:
            assert vehicle.elapsed >= 0
            now = (vehicle.at, vehicle.next_node, vehicle.elapsed)
            before = self._positions.get(vehicle.id)
            if before is not None:
                assert moved_at_most_one_minute(engine.city, before, now, self._blocked), (
                    vehicle.id,
                    before,
                    now,
                )
            self._positions[vehicle.id] = now
        for hospital in engine.hospitals.values():
            assert 0 <= hospital.fuel_l <= hospital.tank_l, hospital
        for depot in engine.depots.values():
            assert depot.stock_l >= 0, depot
        self._check_power(engine)
        canonical_json(engine.snapshot())  # raises if a float crept into the state
        self._blocked = frozenset(engine.blocked)
        self.ticks += 1

    def _check_power(self, engine: Engine) -> None:
        feeds = resolve_feeds(engine.zones, engine.substations)
        assert {z.id: z.fed_by for z in engine.zones.values()} == feeds, "stale zone feeds"
        loads = substation_loads(engine.zones, feeds)
        grace = engine.config.trip_grace_minutes
        for s in engine.substations.values():
            assert s.load_kw == loads.get(s.id, 0), s
            assert s.over_minutes < grace, s
            if s.state is SubstationState.REPAIRING:
                assert s.crew is not None, s
                assert s.work_left > 0, s
                crew = engine.crews[s.crew]
                assert crew.state is VehicleState.REPAIRING, crew
                assert (crew.substation, crew.at, crew.next_node) == (s.id, s.node, None), crew
            else:
                assert s.crew is None, s
        for crew in engine.crews.values():
            if crew.state is VehicleState.REPAIRING:
                assert crew.substation is not None
                assert engine.substations[crew.substation].crew == crew.id, crew
        metrics = engine.metrics
        for hospital in engine.hospitals.values():
            zone = None if hospital.zone is None else engine.zones[hospital.zone]
            assert hospital.powered == (zone is not None and zone.powered), hospital
            burned = metrics.burned_l[hospital.id] - self._burned.get(hospital.id, 0)
            dark = metrics.dark_minutes[hospital.id] - self._dark.get(hospital.id, 0)
            if (burned > 0 or dark > 0) and zone is not None and self.ticks > 0:
                # A generator only runs in a minute when its zone was counted as unpowered.
                outage = metrics.zone_outage_minutes[zone.id] - self._outages.get(zone.id, 0)
                assert outage == 1, ("burned fuel while powered", hospital, zone)
            self._burned[hospital.id] = metrics.burned_l[hospital.id]
            self._dark[hospital.id] = metrics.dark_minutes[hospital.id]
        self._outages = dict(metrics.zone_outage_minutes)


def moved_at_most_one_minute(
    city: City,
    before: tuple[str, str | None, int],
    after: tuple[str, str | None, int],
    blocked_during: frozenset[str],
) -> bool:
    """True if a vehicle went from ``before`` to ``after`` by driving at most one minute.

    A vehicle may only enter a road that wasn't blocked during that minute.
    """
    if before == after:
        return True
    at, heading, elapsed = before
    new_at, new_heading, new_elapsed = after
    if heading is None:  # it was at an intersection
        if new_heading is not None:  # it set off along a road
            entered = city.edge_between(at, new_heading)
            return (
                new_at == at
                and new_elapsed == 1
                and entered is not None
                and entered.id not in blocked_during
            )
        road = city.edge_between(at, new_at)  # it crossed a one-minute road
        return road is not None and road.minutes == 1 and road.id not in blocked_during
    if (new_at, new_heading) == (at, heading):
        return new_elapsed == elapsed + 1
    road_minutes = city.edges[edge_id(at, heading)].minutes
    return new_heading is None and new_at == heading and elapsed + 1 == road_minutes
