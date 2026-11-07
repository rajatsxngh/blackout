"""C1 Rules: the naive "50-line script" baseline (docs/PRD.md section 8.7).

Kept naive on purpose (C2 Solver, M7, is the strong baseline). At every decision point it:

1. Updates its belief from telemetry and **every** report, trusted blindly; at L1+ the keyword
   parser turns report text into facts first.
2. Reroutes vehicles that are stuck, or whose route crosses a road believed blocked.
3. Serves hospitals on backup power by earliest time to failure: the nearest idle loaded truck
   delivers, or else the idle empty truck nearest the nearest depot with stock goes to load.
4. Sends idle crews to the damaged substation with the most priority-weighted load.
5. On an observed overload, sheds residential then essential zones (never critical) until the
   substation fits; restores shed zones, most important first, once a feeder has room.
"""

from collections.abc import Sequence
from itertools import pairwise

from blackout.controllers.keyword_parser import KeywordParser
from blackout.graph.dependency import time_to_failure
from blackout.info.belief import BeliefState, trust_all
from blackout.info.observation import CrewView, Observation, TruckView
from blackout.sim.actions import (
    AnyAction,
    AssignCrew,
    DispatchTruck,
    Reroute,
    RestoreLoad,
    ShedLoad,
)
from blackout.sim.city import EdgeId, NodeId, edge_id
from blackout.sim.config import SimConfig
from blackout.sim.entities import Priority, SubstationState, TaskKind, VehicleState
from blackout.sim.routing import path_minutes, shortest_path

_SHED_ORDER = (Priority.RESIDENTIAL, Priority.ESSENTIAL)  # never critical
_DOWN = (SubstationState.FAILED.value, SubstationState.TRIPPED.value)
_TOP_UP_PCT = 10  # don't send fuel to a tank that's less than this much short of full


class RulesController:
    name = "rules"
    _obs: Observation  # the observation being decided on

    def __init__(self, config: SimConfig | None = None) -> None:
        self._rules = config or SimConfig()
        self.reset()

    def reset(self) -> None:
        self.belief = BeliefState()
        self._parser: KeywordParser | None = None
        self._loading_for: dict[str, str] = {}  # truck -> hospital it's fetching fuel for
        self._blocked: frozenset[EdgeId] = frozenset()

    def decide(self, observation: Observation) -> Sequence[AnyAction]:
        self.belief.observe(observation, trust_all)  # telemetry, and the facts at L0
        for report in observation.reports:
            if report.facts is None and report.text:
                self._parser = self._parser or KeywordParser.for_observation(observation)
                facts = self._parser.parse(report.text)
                self.belief.apply_facts(facts, trust_all, report=report)
        self._obs, self._blocked = observation, frozenset(self.belief.blocked_roads())
        rerouted = self._reroute()
        moved = {action.vehicle_id for action in rerouted}
        return [*rerouted, *self._fuel(moved), *self._crews(), *self._grid()]

    # --- vehicles ------------------------------------------------------------------------

    def _reroute(self) -> list[Reroute]:
        actions: list[Reroute] = []
        for v in (*self._obs.trucks, *self._obs.crews):
            path = v.route if v.route[:1] == (v.anchor,) else (v.anchor, *v.route)
            crosses = any(edge_id(a, b) in self._blocked for a, b in pairwise(path))
            stuck = v.state is VehicleState.STUCK
            if not (stuck or (v.state is VehicleState.MOVING and crosses)):
                continue
            if self._minutes(v.anchor, path[-1]) is None:
                continue  # no way around what's known; wait for news
            why = "is stuck" if stuck else "would cross a reported block"
            reason = f"{v.id} {why}: route around known blocks"
            actions.append(Reroute(vehicle_id=v.id, avoid_edges=self._avoid, reason=reason))
        return actions

    def _fuel(self, moved: set[str]) -> list[DispatchTruck]:
        trucks = {t.id: t for t in self._obs.trucks}
        self._loading_for = {  # forget trips that ended, or broke down
            t: h
            for t, h in self._loading_for.items()
            if (task := trucks[t].task) is not None
            and task.kind is TaskKind.LOAD
            and trucks[t].state is not VehicleState.BROKEN
        }
        served = set(self._loading_for.values()) | {
            t.task.target_id
            for t in trucks.values()
            if t.task is not None
            and t.task.kind is TaskKind.DELIVER
            and t.state is not VehicleState.BROKEN
        }
        idle = [t for t in trucks.values() if t.state is VehicleState.IDLE and t.id not in moved]
        needy = []
        for h in self._obs.hospitals:
            ttf, fuel = time_to_failure(self.belief, h.id), self.belief.fuel_now(h.id)
            short = fuel is None or (h.tank_l - fuel) * 100 >= _TOP_UP_PCT * h.tank_l
            if ttf is not None and h.id not in served and short:
                needy.append((ttf, h.id, h.node))
        actions: list[DispatchTruck] = []
        for ttf, hospital, node in sorted(needy):
            truck, minutes = self._nearest([t for t in idle if t.load_l > 0], node)
            task, target, plan = TaskKind.DELIVER, hospital, "nearest loaded truck"
            if truck is None:
                depot = self._nearest_depot(node)
                if depot is None:
                    continue
                truck, minutes = self._nearest([t for t in idle if t.load_l == 0], depot[1])
                task, target, plan = TaskKind.LOAD, depot[0], f"loads at {depot[0]}"
            if truck is None:
                continue
            reason = f"EDF: {hospital} dark in {ttf} min; {truck.id} {plan} ({minutes} min away)"
            actions.append(
                DispatchTruck(
                    truck_id=truck.id,
                    task=task,
                    target_id=target,
                    avoid_edges=self._avoid,
                    reason=reason,
                )
            )
            idle.remove(truck)
            if task is TaskKind.LOAD:
                self._loading_for[truck.id] = hospital
        return actions

    def _nearest_depot(self, node: NodeId) -> tuple[str, NodeId] | None:
        """The depot believed to have stock that's quickest to drive from to ``node``."""
        best: tuple[int, str, NodeId] | None = None
        for depot in self._obs.depots:
            known = self.belief.depots.get(depot.id)
            if known is None or known.stock_l is None or known.stock_l.value == 0:
                continue
            minutes = self._minutes(depot.node, node)
            if minutes is not None and (best is None or (minutes, depot.id) < best[:2]):
                best = (minutes, depot.id, depot.node)
        return None if best is None else best[1:]

    def _crews(self) -> list[AssignCrew]:
        obs = self._obs
        busy = {c.substation for c in obs.crews} | {s.id for s in obs.substations if s.crew}
        damaged = []
        for s in obs.substations:
            known = self.belief.substations.get(s.id)
            if known and known.state and known.state.value in _DOWN and s.id not in busy:
                weight = sum(
                    z.demand_kw * self._rules.weight(z.priority)
                    for z in obs.zones
                    if z.primary == s.id
                )
                damaged.append((-weight, s.id, s.node))
        free = [
            c
            for c in obs.crews
            if c.state is VehicleState.IDLE
            and (c.substation is None or obs.substation(c.substation).state.value == "online")
        ]
        actions: list[AssignCrew] = []
        for weight, substation, node in sorted(damaged):
            crew, minutes = self._nearest(free, node)
            if crew is None:
                break
            reason = f"{substation} down, most weighted load ({-weight}); {crew.id} nearest"
            reason += f" ({minutes} min)"
            actions.append(
                AssignCrew(
                    crew_id=crew.id,
                    substation_id=substation,
                    avoid_edges=self._avoid,
                    reason=reason,
                )
            )
            free.remove(crew)
        return actions

    # --- grid ----------------------------------------------------------------------------

    def _grid(self) -> list[ShedLoad | RestoreLoad]:
        obs = self._obs
        load = {s.id: s.load_kw for s in obs.substations}
        actions: list[ShedLoad | RestoreLoad] = []
        for s in obs.substations:
            if s.state is not SubstationState.ONLINE or load[s.id] <= s.capacity_kw:
                continue
            sheddable = [
                z
                for z in obs.zones
                if z.fed_by == s.id and not z.shed and z.priority in _SHED_ORDER
            ]
            sheddable.sort(key=lambda z: (_SHED_ORDER.index(z.priority), -z.demand_kw, z.id))
            for z in sheddable:
                if load[s.id] <= s.capacity_kw:
                    break
                load[s.id] -= z.demand_kw
                reason = f"{s.id} overloaded ({s.load_kw}/{s.capacity_kw} kW): shed {z.priority}"
                actions.append(ShedLoad(zone_id=z.id, reason=reason))
        online = {s.id: s for s in obs.substations if s.state is SubstationState.ONLINE}
        shed = sorted((z for z in obs.zones if z.shed), key=lambda z: (_rank(z.priority), z.id))
        for z in shed:
            feeder = online.get(z.primary) or online.get(z.alternate or "")
            if feeder is not None and load[feeder.id] + z.demand_kw <= feeder.capacity_kw:
                load[feeder.id] += z.demand_kw
                reason = f"{feeder.id} has room for {z.id} ({z.demand_kw} kW)"
                actions.append(RestoreLoad(zone_id=z.id, reason=reason))
        return actions

    # --- helpers -------------------------------------------------------------------------

    @property
    def _avoid(self) -> tuple[EdgeId, ...]:
        return tuple(sorted(self._blocked))

    def _minutes(self, start: NodeId, end: NodeId) -> int | None:
        path = shortest_path(self._obs.city, start, end, self._blocked)
        return None if path is None else path_minutes(self._obs.city, path)

    def _nearest[V: (TruckView, CrewView)](
        self, vehicles: Sequence[V], node: NodeId
    ) -> tuple[V | None, int | None]:
        """The vehicle that can drive to ``node`` soonest on roads believed open."""
        best: tuple[int, str, V] | None = None
        for v in vehicles:
            minutes = self._minutes(v.anchor, node)
            if minutes is not None and (best is None or (minutes, v.id) < best[:2]):
                best = (minutes, v.id, v)
        return (None, None) if best is None else (best[2], best[0])


def _rank(priority: Priority) -> int:
    return (Priority.CRITICAL, Priority.ESSENTIAL, Priority.RESIDENTIAL).index(priority)
