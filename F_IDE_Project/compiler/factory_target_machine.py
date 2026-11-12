"""Combine factory configuration with availability, occupancy, WIP locations, and reservations at time t."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Set, Tuple


WAREHOUSE_STORAGE_TYPE = "memory"
HOLDING_STORAGE_TYPE = "register"


AVAIL_WORKSTATION = "ws"
AVAIL_HOLDING = "holding"
AVAIL_LOGISTICS = "logistics"


@dataclass(frozen=True)
class HoldingSite:
    """A physical material-holding site associated with a workstation."""

    storage_id: int
    workstation_id: int
    capacity: int
    role: str


@dataclass(frozen=True)
class ProductionResource:
    """A PALU resource type and its instance count."""

    palu_id: int
    device_type: str
    quantity: int
    capabilities: Tuple[Tuple[str, str, str], ...] = ()


@dataclass(frozen=True)
class LogisticsResource:


    palu_id: int
    device_type: str
    quantity: int


@dataclass
class FactoryState:
    """Time-varying factory state used by lowering and allocation."""

    time: float = 0.0
    # Existing physical occupancy, excluding reservations made in this allocation round.
    occupied: Dict[int, int] = field(default_factory=dict)

    reserved: Dict[int, int] = field(default_factory=dict)
    # Interval reservations permit sequential reuse and account for simultaneous occupancy.


    holding_intervals: Dict[int, List[Tuple[float, float]]] = field(default_factory=dict)

    unavailable_until: Dict[Tuple[str, object], float] = field(default_factory=dict)

    wip_location: Dict[str, int] = field(default_factory=dict)

    released_materials: Set[str] = field(default_factory=set)

    def clone(self) -> "FactoryState":
        return FactoryState(
            time=self.time,
            occupied=dict(self.occupied),
            reserved=dict(self.reserved),
            holding_intervals={k: list(v) for k, v in self.holding_intervals.items()},
            unavailable_until=dict(self.unavailable_until),
            wip_location=dict(self.wip_location),
            released_materials=set(self.released_materials),
        )


class FactoryTargetMachine:


    def __init__(self, target_model_loader, state: Optional[FactoryState] = None):
        self._tm = target_model_loader
        self.state = state if state is not None else FactoryState()
        data = getattr(target_model_loader, "data", None) or {}

        self._holding_sites: Dict[int, HoldingSite] = {}
        self._workstation_holding: Dict[int, List[int]] = {}
        self._production: Dict[int, ProductionResource] = {}
        self._logistics: Optional[LogisticsResource] = None
        self._logistics_caps: Set[Tuple[str, str, str]] = set()
        self._site_capabilities: Dict[int, Set[Tuple[str, str, str]]] = {}
        self._capability_sites: Dict[Tuple[str, str, str], List[int]] = {}
        self._workspace_capabilities: Dict[int, Set[Tuple[str, str, str]]] = {}
        self._warehouse_id: Optional[int] = None
        self._register_ids: List[int] = []

        self._index(data)


    def _index(self, data: dict) -> None:
        addressing = data.get("addressing_model", {}) or {}
        regions = addressing.get("storage_regions", {}) or {}
        self._register_ids = [int(r) for r in (regions.get("register_ids") or [])]
        memory_ids = [int(m) for m in (regions.get("memory_ids") or [])]
        self._warehouse_id = memory_ids[0] if memory_ids else None

        capacities: Dict[int, Tuple[int, str]] = {}
        storage_model = data.get("storage_model", {}) or {}
        for entry in storage_model.get("capacity_constraints", []) or []:
            sid = entry.get("storage_id")
            if isinstance(sid, int):
                capacities[sid] = (int(entry.get("capacity", 1)), str(entry.get("role", "")))

        ws_to_registers: Dict[int, List[int]] = {}
        for ws in addressing.get("workstation_mapping", []) or []:
            ws_id = ws.get("workstation_id")
            if not isinstance(ws_id, int):
                continue
            ws_to_registers[ws_id] = [int(r) for r in (ws.get("assigned_registers") or [])]

        for storage_id in self._register_ids:
            ws_id = self._workstation_of_register(storage_id, ws_to_registers)
            if ws_id is None:
                continue
            capacity, role = capacities.get(storage_id, (1, ""))
            site = HoldingSite(
                storage_id=storage_id,
                workstation_id=ws_id,
                capacity=capacity,
                role=role,
            )
            self._holding_sites[storage_id] = site
            self._workstation_holding.setdefault(ws_id, []).append(storage_id)

        ics = data.get("instruction_capability_set", {}) or {}
        for palu in ics.get("palu_definitions", []) or []:
            palu_id = palu.get("palu_id")
            if not isinstance(palu_id, int):
                continue
            caps = tuple(
                (str(c.get("opcode", "")), str(c.get("func", "")), str(c.get("rp", "")))
                for c in (palu.get("supported_capabilities") or [])
                if isinstance(c, dict) and c.get("opcode") and c.get("func") and c.get("rp")
            )
            resource = ProductionResource(
                palu_id=palu_id,
                device_type=str(palu.get("device_type", "")),
                quantity=int(palu.get("quantity", 1) or 1),
                capabilities=caps,
            )
            self._production[palu_id] = resource
            if self._is_logistics_device(resource.device_type, caps):
                # The logistics pool is shared across workstations.
                self._logistics_caps.update(caps)
                if self._logistics is None or resource.quantity > self._logistics.quantity:
                    self._logistics = LogisticsResource(palu_id, resource.device_type, resource.quantity)

        for ws in addressing.get("workstation_mapping", []) or []:
            ws_id = ws.get("workstation_id")
            if not isinstance(ws_id, int):
                continue
            caps: Set[Tuple[str, str, str]] = set()
            for palu_id in ws.get("assigned_palus") or []:
                resource = self._production.get(palu_id)
                if resource is None:
                    continue
                if self._logistics is not None and palu_id == self._logistics.palu_id:
                    # Shared logistics capabilities do not imply workstation processing capabilities.
                    continue
                caps.update(resource.capabilities)
            self._workspace_capabilities[ws_id] = caps
            for cap in caps:
                self._capability_sites.setdefault(cap, []).append(ws_id)

        for storage_id, site in self._holding_sites.items():
            self._site_capabilities[storage_id] = set(
                self._workspace_capabilities.get(site.workstation_id, set())
            )

    @staticmethod
    def _is_logistics_device(device_type: str, caps: Iterable[Tuple[str, str, str]]) -> bool:
        if "AGV" in device_type.upper() or "LOGISTIC" in device_type.upper():
            return True

        funcs = {c[1] for c in caps}
        return {"010", "000"} <= funcs and len(funcs) <= 2

    @staticmethod
    def _workstation_of_register(register_id: int, ws_to_registers: Dict[int, List[int]]) -> Optional[int]:
        for ws_id, regs in ws_to_registers.items():
            if register_id in regs:
                return ws_id
        return None


    @property
    def warehouse_id(self) -> Optional[int]:
        return self._warehouse_id

    @property
    def register_ids(self) -> List[int]:
        return list(self._register_ids)

    def holding_sites(self) -> List[HoldingSite]:
        return [self._holding_sites[sid] for sid in sorted(self._holding_sites)]

    def holding_site(self, storage_id: int) -> Optional[HoldingSite]:
        return self._holding_sites.get(storage_id)

    def holding_sites_of_workstation(self, ws_id: int) -> List[int]:
        return list(self._workstation_holding.get(ws_id, []))

    def capacity_of(self, storage_id: int) -> int:
        site = self._holding_sites.get(storage_id)
        return site.capacity if site else 0

    def production_resources(self) -> List[ProductionResource]:
        return [self._production[p] for p in sorted(self._production)]

    def logistics_resource(self) -> Optional[LogisticsResource]:
        return self._logistics

    def workstation_capabilities(self, ws_id: int) -> Set[Tuple[str, str, str]]:
        return set(self._workspace_capabilities.get(ws_id, set()))

    def capabilities_of_site(self, storage_id: int) -> Set[Tuple[str, str, str]]:
        return set(self._site_capabilities.get(storage_id, set()))

    def workstations_supporting(self, capability: Tuple[str, str, str]) -> List[int]:
        """Return capable workstations; shared logistics capabilities apply across workstations."""
        if capability in self._logistics_caps:
            return sorted(self._workspace_capabilities)
        return sorted(self._capability_sites.get(capability, []))

    def any_resource_supports(self, capability: Tuple[str, str, str]) -> bool:
        if capability in self._logistics_caps:
            return self._logistics is not None and self._logistics.quantity > 0
        return bool(self._capability_sites.get(capability))

    def is_logistics_capability(self, capability: Tuple[str, str, str]) -> bool:

        return capability in self._logistics_caps

    def nominal_transport_time(self, src_storage: Optional[int], dst_storage: Optional[int]) -> float:
        """Return nominal transport time from the target model."""
        if src_storage is None or dst_storage is None:
            return 0.0
        if src_storage == dst_storage:
            return 0.0
        getter = getattr(self._tm, "get_logistics_time", None)
        if getter is None:
            return 0.0
        value = getter(src_storage, dst_storage)
        return float(value) if value is not None else 0.0


    def at(self, t: float) -> "FactoryTargetMachine":
        return self.with_state(self.state_at(t))

    def state_at(self, t: float) -> FactoryState:
        snapshot = self.state.clone()
        snapshot.time = float(t)
        return snapshot

    def with_state(self, state: FactoryState) -> "FactoryTargetMachine":
        return FactoryTargetMachine(self._tm, state)

    def is_available(self, kind: str, key: object, t: Optional[float] = None) -> bool:
        now = self.state.time if t is None else t
        until = self.state.unavailable_until.get((kind, key))
        if until is None:
            return True
        return now >= until

    def is_site_available(self, storage_id: int, t: Optional[float] = None,
                          source_site: Optional[int] = None) -> bool:
        if not self.is_available(AVAIL_HOLDING, storage_id, t):
            return False
        site = self._holding_sites.get(storage_id)
        if site is None:
            return False
        if not self.is_available(AVAIL_WORKSTATION, site.workstation_id, t):
            return False
        source = self._holding_sites.get(source_site)
        if source is not None and source.workstation_id == site.workstation_id:
            return True
        if self._logistics is None:
            return True
        return self.is_available(AVAIL_LOGISTICS, self._logistics.palu_id, t)

    def occupancy_of(self, storage_id: int) -> int:
        return int(self.state.occupied.get(storage_id, 0))

    def reservation_of(self, storage_id: int) -> int:
        return int(self.state.reserved.get(storage_id, 0))

    def reservations_at(self, storage_id: int, t: Optional[float] = None) -> int:
        """Count current reservations, including intervals covering time t."""
        now = self.state.time if t is None else t
        total = int(self.state.reserved.get(storage_id, 0))
        for start, end in self.state.holding_intervals.get(storage_id, ()):
            if start <= now < end:
                total += 1
        return total

    def free_capacity(self, storage_id: int) -> int:
        """Return capacity minus occupancy and current reservations."""
        return (self.capacity_of(storage_id) - self.occupancy_of(storage_id)
                - self.reservations_at(storage_id))

    def free_capacity_over(self, storage_id: int, start: float, end: float) -> int:
        """Return the minimum free capacity throughout the half-open interval [start, end)."""
        cap = self.capacity_of(storage_id) - self.occupancy_of(storage_id)
        if cap <= 0:
            return cap
        overlapping = [
            interval for interval in self.state.holding_intervals.get(storage_id, ())
            if interval[0] < end and start < interval[1]
        ]
        if not overlapping:
            return cap
        events = []
        for s, e in overlapping:
            events.append((s, 1))
            events.append((e, -1))
        events.sort(key=lambda item: (item[0], item[1]))
        current = peak = 0
        for _, delta in events:
            current += delta
            peak = max(peak, current)
        return cap - peak

    def reserve(self, storage_id: int, count: int = 1) -> None:
        self.state.reserved[storage_id] = self.reservation_of(storage_id) + count

    def reserve_interval(self, storage_id: int, start: float, end: float) -> None:

        self.state.holding_intervals.setdefault(storage_id, []).append((float(start), float(end)))

    def release_reservations(self) -> None:
        self.state.reserved.clear()
        self.state.holding_intervals.clear()

    def wip_location_of(self, material: str) -> Optional[int]:
        return self.state.wip_location.get(material)

    def set_wip_location(self, material: str, storage_id: int) -> None:
        self.state.wip_location[material] = storage_id

    def mark_unavailable(self, kind: str, key: object, until: float) -> None:
        self.state.unavailable_until[(kind, key)] = float(until)

    def mark_available(self, kind: str, key: object) -> None:
        self.state.unavailable_until.pop((kind, key), None)


    def snapshot(self) -> Dict[str, object]:
        return {
            "time": self.state.time,
            "holding_sites": len(self._holding_sites),
            "workstations": len(self._workspace_capabilities),
            "production_resources": len(self._production),
            "logistics_quantity": self._logistics.quantity if self._logistics else 0,
            "occupied": sum(self.state.occupied.values()),
            "reserved": sum(self.state.reserved.values()),
            "unavailable": {
                f"{kind}:{key}": until
                for (kind, key), until in sorted(self.state.unavailable_until.items(), key=lambda kv: str(kv[0]))
            },
        }
