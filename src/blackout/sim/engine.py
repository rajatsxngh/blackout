"""Ground truth and physics. Only this module changes the world (invariant I1).

Order of work (docs/PRD.md section 8.2): events due at a tick are applied before contestants
decide at that tick. Each minute after that runs four steps: power (overload counting, trips
and unmet demand), generator burn, trucks, then crews. Zone feeds, substation loads and
hospital statuses are refreshed after anything that can change them, so an observation always
matches the state the next minute starts from.
"""

from collections.abc import Sequence

from blackout.scenarios.schema import Scenario
from blackout.sim.actions import (
    AssignCrew,
    DispatchTruck,
    MovementAction,
    ShedLoad,
    SwitchAction,
    vehicle_of,
)
from blackout.sim.city import EdgeId, NodeId, build_grid_city, edge_id
from blackout.sim.config import SimConfig
from blackout.sim.entities import (
    Crew,
    Depot,
    Hospital,
    Priority,
    Substation,
    SubstationState,
    Task,
    TaskKind,
    Truck,
    Vehicle,
    VehicleState,
    Zone,
)
from blackout.sim.events import (
    BurnRateChange,
    EventQueue,
    QueueItem,
    RoadBlock,
    RoadClear,
    SubstationFail,
    TruckBreakdown,
    TruckRepair,
)
from blackout.sim.hashing import digest
from blackout.sim.metrics import Metrics
from blackout.sim.power import resolve_feeds, substation_loads
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
            t.id: Truck(id=t.id, at=t.node, capacity_l=t.capacity_l, load_l=t.load_l)
            for t in sorted(scenario.trucks, key=lambda t: t.id)
        }
        self.crews = {
            c.id: Crew(id=c.id, at=c.node) for c in sorted(scenario.crews, key=lambda c: c.id)
        }
        self.substations = {
            s.id: Substation(
                id=s.id,
                name=s.name,
                node=s.node,
                capacity_kw=s.capacity_kw,
                state=SubstationState(s.state),
                repair_minutes=s.repair_minutes,
            )
            for s in sorted(scenario.substations, key=lambda s: s.id)
        }
        self.zones = {
            z.id: Zone(
                id=z.id,
                name=z.name,
                nodes=tuple(sorted(z.nodes)),
                demand_kw=z.demand_kw,
                priority=Priority(z.priority),
                primary=z.primary,
                alternate=z.alternate,
            )
            for z in sorted(scenario.zones, key=lambda z: z.id)
        }
        zone_of = {node: zone.id for zone in self.zones.values() for node in zone.nodes}
        self.hospitals = {
            h.id: Hospital(
                id=h.id,
                name=h.name,
                node=h.node,
                tank_l=h.tank_l,
                fuel_l=h.fuel_l,
                burn_lpm=h.burn_lpm,
                zone=zone_of.get(h.node),
            )
            for h in sorted(scenario.hospitals, key=lambda h: h.id)
        }
        self.depots = {
            d.id: Depot(id=d.id, name=d.name, node=d.node, stock_l=d.stock_l)
            for d in sorted(scenario.depots, key=lambda d: d.id)
        }
        self.blocked: set[EdgeId] = set()
        self.known_blocks: set[EdgeId] = set()
        self.metrics = Metrics(
            dark_minutes=dict.fromkeys(self.hospitals, 0),
            burned_l=dict.fromkeys(self.hospitals, 0),
            zone_outage_minutes=dict.fromkeys(self.zones, 0),
        )
        self.queue = EventQueue()
        for event in scenario.events:
            self.queue.push(event.tick, event)
        self._signals: list[Trigger] = []
        self._refresh_power()
        self._signals.clear()  # the starting state isn't news
        self.fuel_total = self.fuel_in_world()

    # --- queries ---------------------------------------------------------------------------

    def vehicle(self, vehicle_id: str) -> Vehicle | None:
        """A truck or crew by ID."""
        return self.trucks.get(vehicle_id) or self.crews.get(vehicle_id)

    def vehicle_target(self, vehicle: Vehicle) -> NodeId | None:
        """Where a vehicle's task takes it, or None if it has no task."""
        if isinstance(vehicle, Truck):
            return None if vehicle.task is None else vehicle.task.target_node
        if isinstance(vehicle, Crew) and vehicle.substation is not None:
            return self.substations[vehicle.substation].node
        return None

    def fuel_in_world(self) -> int:
        """Fuel in depots, trucks and tanks, plus fuel already burned. Conserved by physics."""
        return (
            sum(d.stock_l for d in self.depots.values())
            + sum(t.load_l for t in self.trucks.values())
            + sum(h.fuel_l for h in self.hospitals.values())
            + self.metrics.fuel_burned_l
        )

    def observed_ttf(self) -> dict[str, int | None]:
        """Each hospital's time-to-failure as telemetry shows it (None while powered)."""
        return {hospital_id: h.ttf_minutes for hospital_id, h in self.hospitals.items()}

    def is_stable(self) -> bool:
        """Nothing more can happen: no events are pending, every zone and hospital has power,
        and no substation is overloaded."""
        return (
            len(self.queue) == 0
            and all(zone.powered for zone in self.zones.values())
            and all(hospital.powered for hospital in self.hospitals.values())
            and not any(_overloaded(s) for s in self.substations.values())
        )

    def report(self) -> dict[str, object]:
        """Final metrics, plus when power was fully restored (None if it never was)."""
        restored = (
            self.metrics.outage_until if all(z.powered for z in self.zones.values()) else None
        )
        return {**self.metrics.to_dict(), "power_restored_at": restored}

    def snapshot(self) -> dict[str, object]:
        """The complete ground-truth state in canonical form, for hashing and replay checks."""
        return {
            "tick": self.tick,
            "trucks": [self.trucks[k].to_dict() for k in sorted(self.trucks)],
            "crews": [self.crews[k].to_dict() for k in sorted(self.crews)],
            "hospitals": [self.hospitals[k].to_dict() for k in sorted(self.hospitals)],
            "depots": [self.depots[k].to_dict() for k in sorted(self.depots)],
            "substations": [self.substations[k].to_dict() for k in sorted(self.substations)],
            "zones": [self.zones[k].to_dict() for k in sorted(self.zones)],
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
        due = self.queue.pop_due(self.tick)
        for item in due:
            self._apply_event(item)
        if due:
            self._refresh_power()

    def _apply_event(self, item: QueueItem) -> None:
        match item:
            case RoadBlock(edge=segment):
                self.blocked.add(segment)
            case RoadClear(edge=segment):
                self.blocked.discard(segment)
            case BurnRateChange(hospital=hospital_id, burn_lpm=burn):
                self.hospitals[hospital_id].burn_lpm = burn
            case SubstationFail(substation=substation_id, repair_minutes=repair_minutes):
                self._fail(self.substations[substation_id], repair_minutes)
            case TruckBreakdown(truck=truck_id, repair_minutes=repair_minutes):
                self._break_down(self.trucks[truck_id], repair_minutes)
            case TruckRepair(truck=truck_id):
                self._repair_truck(self.trucks[truck_id])

    def _fail(self, substation: Substation, repair_minutes: int) -> None:
        lost = ""
        if substation.crew is not None:
            crew = self.crews[substation.crew]
            crew.state = VehicleState.IDLE
            crew.substation = None
            lost = f"; {crew.id}'s work on it was lost"
        substation.state = SubstationState.FAILED
        substation.repair_minutes = repair_minutes
        substation.work_left = 0
        substation.crew = None
        substation.over_minutes = 0
        self._emit(TriggerKind.SUBSTATION_STATUS, substation.id, f"failed{lost}")

    def _break_down(self, truck: Truck, repair_minutes: int | None) -> None:
        if truck.state is not VehicleState.BROKEN:
            truck.resume_state = truck.state
            truck.state = VehicleState.BROKEN
            self._emit(TriggerKind.VEHICLE_BROKEN, truck.id, f"broke down {_where(truck)}")
        if repair_minutes is not None:
            self.queue.push(self.tick + repair_minutes, TruckRepair(truck.id))

    def _repair_truck(self, truck: Truck) -> None:
        if truck.state is VehicleState.BROKEN:
            truck.state = truck.resume_state or VehicleState.IDLE
            truck.resume_state = None
            self._emit(TriggerKind.VEHICLE_REPAIRED, truck.id, f"running again {_where(truck)}")

    # --- actions (already validated by blackout.sim.validate) -------------------------------

    def apply(self, action: MovementAction, route: Sequence[NodeId]) -> None:
        """Apply a validated movement now. ``route`` starts at the vehicle's anchor node."""
        vehicle = self.vehicle(vehicle_of(action))
        assert vehicle is not None
        if isinstance(action, DispatchTruck):
            assert isinstance(vehicle, Truck)
            vehicle.task = Task(action.task, action.target_id, route[-1], action.amount_l)
        elif isinstance(action, AssignCrew):
            assert isinstance(vehicle, Crew)
            vehicle.substation = action.substation_id
        vehicle.route = list(route) if vehicle.next_node is not None else list(route[1:])
        if vehicle.route:
            vehicle.state = VehicleState.MOVING
        else:
            self._start_work(vehicle)

    def switch(self, action: SwitchAction) -> None:
        """Apply a validated shed or restore now."""
        self.zones[action.zone_id].shed = isinstance(action, ShedLoad)
        self._refresh_power()

    # --- physics ---------------------------------------------------------------------------

    def run_minute(self) -> None:
        """Advance the world by one minute, from ``tick`` to ``tick + 1``."""
        self._power_step()
        self._burn_fuel()
        for truck_id in sorted(self.trucks):
            truck = self.trucks[truck_id]
            if truck.state is VehicleState.MOVING:
                self._drive(truck)
            elif truck.state in (VehicleState.LOADING, VehicleState.UNLOADING):
                self._work(truck)
        for crew_id in sorted(self.crews):
            crew = self.crews[crew_id]
            if crew.state is VehicleState.MOVING:
                self._drive(crew)
            elif crew.state is VehicleState.REPAIRING:
                self._repair_step(crew)
        self._refresh_power()
        self.tick += 1

    def _power_step(self) -> None:
        """Count overloaded minutes, trip substations that hit the limit, account outages."""
        grace = self.config.trip_grace_minutes
        tripped = False
        for substation_id in sorted(self.substations):
            substation = self.substations[substation_id]
            if not _overloaded(substation):
                substation.over_minutes = 0
                continue
            substation.over_minutes += 1
            if substation.over_minutes == 1:
                load = f"{substation.load_kw} kW on a {substation.capacity_kw} kW substation"
                detail = f"{load}; it trips after {grace} min overloaded"
                self._emit(TriggerKind.OVERLOAD, substation_id, detail)
            if substation.over_minutes >= grace:
                substation.state = SubstationState.TRIPPED
                substation.over_minutes = 0
                self.metrics.trips += 1
                tripped = True
                detail = f"tripped after {grace} min overloaded"
                self._emit(TriggerKind.SUBSTATION_STATUS, substation_id, detail)
        if tripped:
            self._refresh_power()
        for zone_id in sorted(self.zones):
            zone = self.zones[zone_id]
            if zone.powered:
                continue
            self.metrics.unmet_kw_min += zone.demand_kw
            self.metrics.unmet_weighted_kw_min += zone.demand_kw * self.config.weight(zone.priority)
            self.metrics.zone_outage_minutes[zone_id] += 1
            self.metrics.outage_until = self.tick + 1

    def _burn_fuel(self) -> None:
        for hospital_id in sorted(self.hospitals):
            hospital = self.hospitals[hospital_id]
            if hospital.powered:
                continue
            if hospital.fuel_l > 0:
                used = min(hospital.burn_lpm, hospital.fuel_l)
                hospital.fuel_l -= used
                self.metrics.burned_l[hospital_id] += used
            else:
                self.metrics.dark_minutes[hospital_id] += 1

    def _drive(self, vehicle: Vehicle) -> None:
        heading = vehicle.next_node
        if heading is None:
            if not vehicle.route:
                self._arrive(vehicle)
                return
            heading = vehicle.route[0]
            segment = edge_id(vehicle.at, heading)
            if segment in self.blocked:
                vehicle.state = VehicleState.STUCK
                self.known_blocks.add(segment)
                self.metrics.stuck_events += 1
                ahead = f"road ahead blocked: {segment} ({self.city.edges[segment].name})"
                self._emit(TriggerKind.VEHICLE_STUCK, vehicle.id, ahead)
                return
            vehicle.next_node = heading
            vehicle.elapsed = 0
        vehicle.elapsed += 1
        if isinstance(vehicle, Truck):
            self.metrics.drive_minutes += 1
        else:
            self.metrics.crew_drive_minutes += 1
        if vehicle.elapsed >= self.city.edges[edge_id(vehicle.at, heading)].minutes:
            vehicle.at = heading
            vehicle.next_node = None
            vehicle.elapsed = 0
            vehicle.route.pop(0)
            if isinstance(vehicle, Truck):
                self.metrics.edges_traversed += 1
            if not vehicle.route:
                self._arrive(vehicle)

    def _arrive(self, vehicle: Vehicle) -> None:
        self._emit(TriggerKind.VEHICLE_ARRIVED, vehicle.id, f"reached {vehicle.at}")
        self._start_work(vehicle)

    def _start_work(self, vehicle: Vehicle) -> None:
        if isinstance(vehicle, Truck):
            self._start_task(vehicle)
        elif isinstance(vehicle, Crew):
            self._start_repair(vehicle)

    def _start_task(self, truck: Truck) -> None:
        task = truck.task
        if task is None or task.kind is TaskKind.MOVE:
            truck.state = VehicleState.IDLE
            truck.task = None
        elif task.kind is TaskKind.LOAD:
            truck.state = VehicleState.LOADING
            truck.work_left = self.config.load_minutes
        else:
            truck.state = VehicleState.UNLOADING
            truck.work_left = self.config.unload_minutes

    def _work(self, truck: Truck) -> None:
        truck.work_left -= 1
        if truck.work_left > 0:
            return
        task = truck.task
        assert task is not None, "a loading or unloading truck always has a task"
        if truck.state is VehicleState.LOADING:
            detail = self._load(truck, task)
        else:
            detail = self._unload(truck, task)
        truck.state = VehicleState.IDLE
        truck.task = None
        self._emit(TriggerKind.TASK_COMPLETED, truck.id, detail)

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

    def _start_repair(self, crew: Crew) -> None:
        substation = None if crew.substation is None else self.substations[crew.substation]
        if (
            substation is not None
            and substation.crew is None
            and substation.state in (SubstationState.FAILED, SubstationState.TRIPPED)
        ):
            failed = substation.state is SubstationState.FAILED
            minutes = substation.repair_minutes if failed else self.config.reset_minutes
            assert minutes is not None, "a failed substation always knows its repair time"
            substation.state = SubstationState.REPAIRING
            substation.work_left = minutes
            substation.crew = crew.id
            crew.state = VehicleState.REPAIRING
            job = "repair" if failed else "reset"
            detail = f"{crew.id} started the {job}: {minutes} min"
            self._emit(TriggerKind.SUBSTATION_STATUS, substation.id, detail)
            return
        crew.state = VehicleState.IDLE  # nothing to fix here, or another crew is on it
        crew.substation = None

    def _repair_step(self, crew: Crew) -> None:
        assert crew.substation is not None, "a repairing crew always has a substation"
        substation = self.substations[crew.substation]
        substation.work_left -= 1
        if substation.work_left > 0:
            return
        substation.state = SubstationState.ONLINE
        substation.crew = None
        substation.repair_minutes = None
        substation.over_minutes = 0
        crew.state = VehicleState.IDLE
        crew.substation = None
        self.metrics.repairs_completed += 1
        self._emit(TriggerKind.TASK_COMPLETED, crew.id, f"{substation.id} back online")
        self._emit(TriggerKind.SUBSTATION_STATUS, substation.id, "online")

    def _refresh_power(self) -> None:
        """Re-resolve zone feeds and substation loads, then hospital power and status."""
        feeds = resolve_feeds(self.zones, self.substations)
        for zone_id in sorted(self.zones):
            zone = self.zones[zone_id]
            was_powered = zone.powered
            zone.fed_by = feeds[zone_id]
            if zone.powered != was_powered:
                if zone.powered:
                    now = f"powered by {zone.fed_by}"
                else:
                    now = "shed" if zone.shed else "no power"
                self._emit(TriggerKind.ZONE_STATUS, zone_id, now)
        loads = substation_loads(self.zones, feeds)
        for substation_id in sorted(self.substations):
            self.substations[substation_id].load_kw = loads.get(substation_id, 0)
        for hospital_id in sorted(self.hospitals):
            hospital = self.hospitals[hospital_id]
            hospital.powered = hospital.zone is not None and self.zones[hospital.zone].powered
        self._update_statuses()

    def _update_statuses(self) -> None:
        for hospital_id in sorted(self.hospitals):
            hospital = self.hospitals[hospital_id]
            status = hospital.current_status()
            if status is not hospital.status:
                hospital.status = status
                self._emit(TriggerKind.HOSPITAL_STATUS, hospital_id, f"now {status.value}")

    def _emit(self, kind: TriggerKind, subject: str, detail: str) -> None:
        self._signals.append(Trigger(kind, subject, detail))


def _overloaded(substation: Substation) -> bool:
    return (
        substation.state is SubstationState.ONLINE and substation.load_kw > substation.capacity_kw
    )


def _where(vehicle: Vehicle) -> str:
    if vehicle.next_node is None:
        return f"at {vehicle.at}"
    return f"between {vehicle.at} and {vehicle.next_node}"
