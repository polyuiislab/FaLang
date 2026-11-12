# F_IDE_Project/compiler/factory_target_machine.py
"""
Paper 1 Method 2 —— Factory Target Machine Model F_t。

制造语境：同一张订单程序，在甲厂能实现、在乙厂未必能实现；在同一个厂的正常班次能实现、
在高负荷或某台机器停机时也未必能实现。编译器因此不能只面对"一个固定的目标机"，而必须面对
"编译时刻 t 所看到的那台工厂"：

    F_t = (工厂的静态描述, 时刻 t 的工厂状态)

- 静态描述：production resources、logistics resources、physical holding sites、capabilities、
  capacities、nominal transport time。这些来自目标机模型 JSON。
- 时刻 t 的状态：availability、occupancy、WIP 实际位置、本轮分配已做的 reservation。

本模块把两者合成一个对象，供 Target Lowering 与 Residual Graph Coloring 使用。
它**不**承担 Paper 2 的调度职责：这里没有 list scheduling，也没有全局 cost model。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Set, Tuple


WAREHOUSE_STORAGE_TYPE = "memory"
HOLDING_STORAGE_TYPE = "register"

# 资源不可用性用统一的键表示，便于 uncertainty 事件写入。
AVAIL_WORKSTATION = "ws"
AVAIL_HOLDING = "holding"
AVAIL_LOGISTICS = "logistics"


@dataclass(frozen=True)
class HoldingSite:
    """一个物理持有位置（target model 中的 register / 线边库位）。"""

    storage_id: int
    workstation_id: int
    capacity: int
    role: str


@dataclass(frozen=True)
class ProductionResource:
    """一类生产资源（PALU 类型）。quantity 为该类型在厂内的实例数。"""

    palu_id: int
    device_type: str
    quantity: int
    capabilities: Tuple[Tuple[str, str, str], ...] = ()


@dataclass(frozen=True)
class LogisticsResource:
    """物流资源池（例如 AGV 车队）。"""

    palu_id: int
    device_type: str
    quantity: int


@dataclass
class FactoryState:
    """F_t 的时变部分：availability / occupancy / WIP 位置 / 本轮 reservation。"""

    time: float = 0.0
    # storage_id -> 已被车间执行占用的持有数（不由本轮分配产生）
    occupied: Dict[int, int] = field(default_factory=dict)
    # storage_id -> 本轮分配已经预留的持有数 R_p(t)（计数口径）
    reserved: Dict[int, int] = field(default_factory=dict)
    # storage_id -> 本轮分配已预订的**占位区间** [(start, end), ...]
    #
    # 容量约束本质上是时间相关的：C^free_p(t) = C_p - O_p(t) - R_p(t) 必须在整个占位区间内
    # 都不小于 1。只记一个计数会把"两个版本先后使用同一库位"和"两个版本同时占用"混为一谈，
    # 于是容量为 2 的库位被当成只能放一件。
    holding_intervals: Dict[int, List[Tuple[float, float]]] = field(default_factory=dict)
    # (kind, key) -> 恢复可用时刻
    unavailable_until: Dict[Tuple[str, object], float] = field(default_factory=dict)
    # 物料版本 -> 当前所在 storage_id（仓储用 100）
    wip_location: Dict[str, int] = field(default_factory=dict)
    # 已完成的物料版本（不再需要持有位置）
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
    """F_t：编译器在时刻 t 看到的工厂目标机。"""

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

    # ------------------------------------------------------------------
    # 建索引
    # ------------------------------------------------------------------
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
                # 物流资源池：全厂共享，不隶属于某个工位
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
                    # 物流资源是共享池，不代表工位具备该加工能力
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
        # 同时具备 Move / Pload / Pstore 的设备即物流资源
        funcs = {c[1] for c in caps}
        return {"010", "000"} <= funcs and len(funcs) <= 2

    @staticmethod
    def _workstation_of_register(register_id: int, ws_to_registers: Dict[int, List[int]]) -> Optional[int]:
        for ws_id, regs in ws_to_registers.items():
            if register_id in regs:
                return ws_id
        return None

    # ------------------------------------------------------------------
    # 静态查询
    # ------------------------------------------------------------------
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
        """能执行该能力的工位。物流能力由共享 AGV 池承担，因此返回全体工位。"""
        if capability in self._logistics_caps:
            return sorted(self._workspace_capabilities)
        return sorted(self._capability_sites.get(capability, []))

    def any_resource_supports(self, capability: Tuple[str, str, str]) -> bool:
        if capability in self._logistics_caps:
            return self._logistics is not None and self._logistics.quantity > 0
        return bool(self._capability_sites.get(capability))

    def is_logistics_capability(self, capability: Tuple[str, str, str]) -> bool:
        """物流能力（Move / Pload / Pstore）由共享物流资源池承担，不约束持有位置。"""
        return capability in self._logistics_caps

    def nominal_transport_time(self, src_storage: Optional[int], dst_storage: Optional[int]) -> float:
        """名义运输时间。仅作为可解释的量记录，不参与任何代价竞争。"""
        if src_storage is None or dst_storage is None:
            return 0.0
        if src_storage == dst_storage:
            return 0.0
        getter = getattr(self._tm, "get_logistics_time", None)
        if getter is None:
            return 0.0
        value = getter(src_storage, dst_storage)
        return float(value) if value is not None else 0.0

    # ------------------------------------------------------------------
    # 时刻 t 的状态查询
    # ------------------------------------------------------------------
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
        """R_p(t)：此刻已预订的占位份数（计数预留 + 覆盖该时刻的占位区间）。"""
        now = self.state.time if t is None else t
        total = int(self.state.reserved.get(storage_id, 0))
        for start, end in self.state.holding_intervals.get(storage_id, ()):
            if start <= now < end:
                total += 1
        return total

    def free_capacity(self, storage_id: int) -> int:
        """C^free_p(t) = C_p - O_p(t) - R_p(t)，取当前时刻。"""
        return (self.capacity_of(storage_id) - self.occupancy_of(storage_id)
                - self.reservations_at(storage_id))

    def free_capacity_over(self, storage_id: int, start: float, end: float) -> int:
        """
        占位区间 [start, end) 内任一时刻的最小剩余容量。

        这才是容量约束的正确形式：一个物料版本从产生到被取走之间，位置必须一直放得下它。
        """
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
        """登记一段占位区间。"""
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

    # ------------------------------------------------------------------
    # 旁证
    # ------------------------------------------------------------------
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
