"""Ground truth and physics. Only this module changes the world (invariant I1).

Order of work (docs/PRD.md section 8.2): events due at a tick are applied before contestants
decide at that tick. In each minute after that, hospitals burn fuel first, then trucks drive,
load and unload, and then hospital statuses are updated.
"""

from collections.abc import Sequence

from blackout.scenarios.schema import Scenario
from blackout.sim.actions import DispatchTruck, Reroute, vehicle_of
from blackout.sim.city import EdgeId, NodeId, build_grid_city, edge_id
from blackout.sim.config import SimConfig
from blackout.sim.entities import Depot, Hospital, Task, TaskKind, Truck, TruckState
from blackout.sim.events import (
    BurnRateChange,
    EventQueue,
    QueueItem,
    RoadBlock,
    RoadClear,
    TruckBreakdown,
    TruckRepair,
)
from blackout.sim.hashing import digest
from blackout.sim.metrics import Metrics
from blackout.sim.triggers import Trigger, TriggerKind


class Engine:
    """The world as it really is. Contestants only ever see observations built from it."""

    def __init__(self, scenario: Scenario, config: SimConfig | None = None) -> None:
        spec = scenario.city
        self.scenario_id = scenario.id
        self.horizon = scenario.horizon
        self.config = config or SimConfig()
        self.city = build_grid_city(
            spec.width,
            spec.height,
            default_minutes=spec.default_travel_minutes,
            minutes=spec.travel_minutes,
            landmarks=spec.landmarks,
        )
        self.tick = 0
        self.trucks = {
            t.id: Truck(t.id, t.capacity_l, t.load_l, t.node)
            for t in sorted(scenario.trucks, key=lambda t: t.id)
        }
        self.hospitals = {
            h.id: Hospital(h.id, h.name, h.node, h.tank_l, h.fuel_l, h.burn_lpm, h.grid_powered)
            for h in sorted(scenario.hospitals, key=lambda h: h.id)
        }
        for hospital in self.hospitals.values():
            hospital.status = hospital.current_status()
        self.depots = {
            d.id: Depot(d.id, d.name, d.node, d.stock_l)
            for d in sorted(scenario.depots, key=lambda d: d.id)
        }
        self.blocked: set[EdgeId] = set()
        self.known_blocks: set[EdgeId] = set()
        self.metrics = Metrics(dark_minutes=dict.fromkeys(self.hospitals, 0))
        self.queue = EventQueue()
        for event in scenario.events:
            self.queue.push(event.tick, event)
        self._signals: list[Trigger] = []
        self.fuel_total = self.fuel_in_world()

    # --- queries ---------------------------------------------------------------------------

    def fuel_in_world(self) -> int:
        """Fuel in depots, trucks and tanks, plus fuel already burned. Conserved by physics."""
        return (
            sum(d.stock_l for d in self.depots.values())
            + sum(t.load_l for t in self.trucks.values())
            + sum(h.fuel_l for h in self.hospitals.values())
            + self.metrics.fuel_burned_l
        )

    def observed_ttf(self) -> dict[str, int | None]:
        """Each hospital's time-to-failure as telemetry shows it (None while on grid)."""
        return {hospital_id: h.ttf_minutes for hospital_id, h in self.hospitals.items()}

    def is_stable(self) -> bool:
        """Nothing more can happen: no events are pending and every hospital has grid power."""
        return len(self.queue) == 0 and all(h.grid_powered for h in self.hospitals.values())

    def snapshot(self) -> dict[str, object]:
        """The complete ground-truth state in canonical form, for hashing and replay checks."""
        return {
            "tick": self.tick,
            "trucks": [self.trucks[k].to_dict() for k in sorted(self.trucks)],
            "hospitals": [self.hospitals[k].to_dict() for k in sorted(self.hospitals)],
            "depots": [self.depots[k].to_dict() for k in sorted(self.depots)],
            "blocked": sorted(self.blocked),
            "known_blocks": sorted(self.known_blocks),
            "metrics": self.metrics.physics(),
        }

    def state_hash(self) -> str:
        return digest(self.snapshot())

    def drain_signals(self) -> list[Trigger]:
        """Observable things that happened since the last call (they become wake triggers)."""
        signals, self._signals = self._signals, []
        return signals

    # --- events ----------------------------------------------------------------------------

    def apply_due_events(self) -> None:
        for item in self.queue.pop_due(self.tick):
            self._apply_event(item)

    def _apply_event(self, item: QueueItem) -> None:
        match item:
            case RoadBlock(edge=segment):
                self.blocked.add(segment)
            case RoadClear(edge=segment):
                self.blocked.discard(segment)
            case BurnRateChange(hospital=hospital_id, burn_lpm=burn):
                self.hospitals[hospital_id].burn_lpm = burn
            case TruckBreakdown(truck=truck_id, repair_minutes=repair_minutes):
                self._break_down(self.trucks[truck_id], repair_minutes)
            case TruckRepair(truck=truck_id):
                self._repair(self.trucks[truck_id])

    def _break_down(self, truck: Truck, repair_minutes: int | None) -> None:
        if truck.state is not TruckState.BROKEN:
            truck.resume_state = truck.state
            truck.state = TruckState.BROKEN
            self._emit(TriggerKind.VEHICLE_BROKEN, truck, f"broke down {self._where(truck)}")
        if repair_minutes is not None:
            self.queue.push(self.tick + repair_minutes, TruckRepair(truck.id))

    def _repair(self, truck: Truck) -> None:
        if truck.state is TruckState.BROKEN:
            truck.state = truck.resume_state or TruckState.IDLE
            truck.resume_state = None
            self._emit(TriggerKind.VEHICLE_REPAIRED, truck, f"running again {self._where(truck)}")

    # --- actions (already validated by blackout.sim.validate) -------------------------------

    def apply(self, action: DispatchTruck | Reroute, route: Sequence[NodeId]) -> None:
        """Apply a validated action immediately. ``route`` starts at the truck's anchor node."""
        truck = self.trucks[vehicle_of(action)]
        if isinstance(action, DispatchTruck):
            truck.task = Task(action.task, action.target_id, route[-1], action.amount_l)
        truck.route = list(route) if truck.next_node is not None else list(route[1:])
        if truck.route:
            truck.state = TruckState.MOVING
        else:
            self._start_task(truck)

    # --- physics ---------------------------------------------------------------------------

    def run_minute(self) -> None:
        """Advance the world by one minute, from ``tick`` to ``tick + 1``."""
        self._burn_fuel()
        for truck_id in sorted(self.trucks):
            truck = self.trucks[truck_id]
            if truck.state is TruckState.MOVING:
                self._drive(truck)
            elif truck.state in (TruckState.LOADING, TruckState.UNLOADING):
                self._work(truck)
        self._update_statuses()
        self.tick += 1

    def _burn_fuel(self) -> None:
        for hospital_id in sorted(self.hospitals):
            hospital = self.hospitals[hospital_id]
            if hospital.grid_powered:
                continue
            if hospital.fuel_l > 0:
                used = min(hospital.burn_lpm, hospital.fuel_l)
                hospital.fuel_l -= used
                self.metrics.fuel_burned_l += used
            else:
                self.metrics.dark_minutes[hospital_id] += 1

    def _drive(self, truck: Truck) -> None:
        heading = truck.next_node
        if heading is None:
            if not truck.route:
                self._arrive(truck)
                return
            heading = truck.route[0]
            segment = edge_id(truck.at, heading)
            if segment in self.blocked:
                truck.state = TruckState.STUCK
                self.known_blocks.add(segment)
                self.metrics.stuck_events += 1
                name = self.city.edges[segment].name
                self._emit(
                    TriggerKind.VEHICLE_STUCK, truck, f"road ahead blocked: {segment} ({name})"
                )
                return
            truck.next_node = heading
            truck.elapsed = 0
        truck.elapsed += 1
        self.metrics.drive_minutes += 1
        if truck.elapsed >= self.city.edges[edge_id(truck.at, heading)].minutes:
            truck.at = heading
            truck.next_node = None
            truck.elapsed = 0
            truck.route.pop(0)
            self.metrics.edges_traversed += 1
            if not truck.route:
                self._arrive(truck)

    def _arrive(self, truck: Truck) -> None:
        self._emit(TriggerKind.VEHICLE_ARRIVED, truck, f"reached {truck.at}")
        self._start_task(truck)

    def _start_task(self, truck: Truck) -> None:
        task = truck.task
        if task is None or task.kind is TaskKind.MOVE:
            truck.state = TruckState.IDLE
            truck.task = None
        elif task.kind is TaskKind.LOAD:
            truck.state = TruckState.LOADING
            truck.work_left = self.config.load_minutes
        else:
            truck.state = TruckState.UNLOADING
            truck.work_left = self.config.unload_minutes

    def _work(self, truck: Truck) -> None:
        truck.work_left -= 1
        if truck.work_left > 0:
            return
        task = truck.task
        assert task is not None, "a loading or unloading truck always has a task"
        if truck.state is TruckState.LOADING:
            detail = self._load(truck, task)
        else:
            detail = self._unload(truck, task)
        truck.state = TruckState.IDLE
        truck.task = None
        self._emit(TriggerKind.TASK_COMPLETED, truck, detail)

    def _load(self, truck: Truck, task: Task) -> str:
        depot = self.depots[task.target_id]
        room = truck.capacity_l - truck.load_l
        wanted = room if task.amount_l is None else min(room, task.amount_l)
        loaded = min(wanted, depot.stock_l)
        depot.stock_l -= loaded
        truck.load_l += loaded
        self.metrics.fuel_loaded_l += loaded
        return f"loaded {loaded} L at {depot.id}"

    def _unload(self, truck: Truck, task: Task) -> str:
        hospital = self.hospitals[task.target_id]
        wanted = truck.load_l if task.amount_l is None else min(truck.load_l, task.amount_l)
        delivered = min(wanted, hospital.tank_l - hospital.fuel_l)
        hospital.fuel_l += delivered
        truck.load_l -= delivered
        self.metrics.fuel_delivered_l += delivered
        return f"delivered {delivered} L to {hospital.id}"

    def _update_statuses(self) -> None:
        for hospital_id in sorted(self.hospitals):
            hospital = self.hospitals[hospital_id]
            status = hospital.current_status()
            if status is not hospital.status:
                hospital.status = status
                self._signals.append(
                    Trigger(TriggerKind.HOSPITAL_STATUS, hospital_id, f"now {status.value}")
                )

    # --- helpers ---------------------------------------------------------------------------

    def _emit(self, kind: TriggerKind, truck: Truck, detail: str) -> None:
        self._signals.append(Trigger(kind, truck.id, detail))

    @staticmethod
    def _where(truck: Truck) -> str:
        if truck.next_node is None:
            return f"at {truck.at}"
        return f"between {truck.at} and {truck.next_node}"
