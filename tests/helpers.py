"""Builders and checkers shared by the tests."""

import random
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from blackout.env import BlackoutEnv
from blackout.info.observation import Observation
from blackout.scenarios.schema import Scenario
from blackout.sim.actions import DispatchTruck, Reroute
from blackout.sim.city import City, edge_id
from blackout.sim.engine import Engine
from blackout.sim.entities import TaskKind
from blackout.sim.hashing import canonical_json
from blackout.sim.routing import shortest_path

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "scenarios" / "handmade" / "hello_blackout.json"


def make_scenario(**overrides: Any) -> Scenario:
    """A small valid scenario: a 3x3 grid of 2-minute roads, one hospital, depot and truck."""
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

    def decide(self, observation: Observation) -> Sequence[DispatchTruck | Reroute]:
        rng = self._rng
        city = observation.city
        nodes = sorted(city.nodes)
        targets = {
            TaskKind.LOAD: [d.id for d in observation.depots] + ["X9"],
            TaskKind.DELIVER: [h.id for h in observation.hospitals] + ["X9"],
            TaskKind.MOVE: nodes,
        }
        actions: list[DispatchTruck | Reroute] = []
        for truck in observation.trucks:
            roll = rng.random()
            if roll < 0.4:
                continue
            avoid = tuple(rng.sample(sorted(city.edges), k=rng.randint(0, 2)))
            if roll < 0.8:
                task = rng.choice(list(TaskKind))
                target = rng.choice(targets[task])
                amount = rng.choice([None, None, 50, 200, 900])
                route = None
                if rng.random() < 0.2:  # an explicit path, possibly through hidden blocks
                    route = _route_to(city, truck.anchor, target, observation)
                    avoid = ()
                actions.append(
                    DispatchTruck(
                        truck_id=truck.id,
                        task=task,
                        target_id=target,
                        amount_l=amount,
                        route=route,
                        avoid_edges=avoid,
                    )
                )
            else:
                actions.append(Reroute(vehicle_id=truck.id, avoid_edges=avoid))
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

    def __call__(self, engine: Engine) -> None:
        assert engine.fuel_in_world() == engine.fuel_total, "fuel was created or destroyed"
        for truck in engine.trucks.values():
            assert 0 <= truck.load_l <= truck.capacity_l, truck
            assert truck.elapsed >= 0
            assert truck.work_left >= 0
            now = (truck.at, truck.next_node, truck.elapsed)
            before = self._positions.get(truck.id)
            if before is not None:
                assert moved_at_most_one_minute(engine.city, before, now, self._blocked), (
                    truck.id,
                    before,
                    now,
                )
            self._positions[truck.id] = now
        for hospital in engine.hospitals.values():
            assert 0 <= hospital.fuel_l <= hospital.tank_l, hospital
        for depot in engine.depots.values():
            assert depot.stock_l >= 0, depot
        canonical_json(engine.snapshot())  # raises if a float crept into the state
        self._blocked = frozenset(engine.blocked)
        self.ticks += 1


def moved_at_most_one_minute(
    city: City,
    before: tuple[str, str | None, int],
    after: tuple[str, str | None, int],
    blocked_during: frozenset[str],
) -> bool:
    """True if a truck went from ``before`` to ``after`` by driving at most one minute.

    A truck may only enter a road that wasn't blocked during that minute.
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
