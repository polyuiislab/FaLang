from __future__ import annotations

from dataclasses import dataclass, field
import math
import random
from typing import Dict, List, Optional, Sequence, Tuple

from case_study.order_generator import PCOrder, QUALITY_GRADES
from case_study.source_generator import generate_order_source
from case_study.state_aware_jit import StateAwareJITCompiler, RuntimeState, JITCompileResult
from core.compiler_proxy import CompilerProxy
from core.target_model import TargetModelLoader
from compiler.ir import OpInstr


METHODS = ("falang", "fcfs", "edd", "atc", "ga", "jmedd", "jit")


@dataclass(frozen=True)
class OperationTemplate:
    name: str
    stage: str
    opcode: str
    func: str
    rp: str
    component: Optional[str] = None
    model: Optional[str] = None


@dataclass
class OperationInstance:
    op_id: int
    order: PCOrder
    seq: int
    template: OperationTemplate
    ready_time: float = 0.0
    preferred_ws: Optional[int] = None
    compiler_rank: int = 0
    compiler_start: float = 0.0


@dataclass
class RecoveryOperation:
    """A recovery step triggered by an exception."""
    recovery_id: int
    order_id: int
    exc_type: str
    trigger_time: float              # Absolute trigger time.
    trigger_op_id: int
    trigger_ws_id: int
    step_index: int
    op_type: str
    params: Optional[str] = None
    assigned_ws_id: Optional[int] = None  # Taken from the compiler plan or assigned at dispatch.
    assigned_device: Optional[str] = None
    processing_time: float = 0.0
    planned_start: float = 0.0       # Absolute contingency start time.
    planned_finish: float = 0.0
    state: str = "scheduled"         # 'scheduled', 'deferred', or 'late'.
    order_due_time: float = float("inf")


@dataclass
class Workstation:
    workstation_id: int
    pm_type: str
    coord: Tuple[int, int]
    registers: List[int]
    palus: List[int]
    capabilities: set[Tuple[str, str, str]]
    slots: int
    redundant: bool = False
    slot_available: List[float] = field(default_factory=list)
    last_model_by_component: Dict[str, str] = field(default_factory=dict)
    busy_time: float = 0.0

    def reset(self) -> None:
        self.slot_available = [0.0 for _ in range(max(1, self.slots))]
        self.last_model_by_component = {}
        self.busy_time = 0.0


@dataclass
class ScheduledOperation:
    method: str
    seed: int
    load: str
    order_id: int
    op_id: int
    operation: str
    workstation_id: int
    ready_time: float
    logistics_start: float
    logistics_finish: float
    start: float
    finish: float
    processing_time: float
    setup_time: float
    logistics_time: float
    waiting_time: float
    blocking_time: float
    is_recovery: bool = False
    exc_type: Optional[str] = None


@dataclass
class SimulationMetrics:
    method: str
    seed: int
    load: str
    orders: int
    makespan: float
    mean_tardiness: float
    on_time_delivery_rate: float
    resource_utilization: float
    average_wip: float
    blocking_time: float
    waiting_time: float
    replanning_events: int
    triggered_exceptions: int
    recovery_operations: int = 0
    recovery_time: float = 0.0
    mof: float = 0.0
    tst: float = 0.0

    def as_dict(self) -> Dict[str, object]:
        return {
            "method": self.method,
            "seed": self.seed,
            "load": self.load,
            "orders": self.orders,
            "makespan": self.makespan,
            "mean_tardiness": self.mean_tardiness,
            "on_time_delivery_rate": self.on_time_delivery_rate,
            "resource_utilization": self.resource_utilization,
            "average_wip": self.average_wip,
            "blocking_time": self.blocking_time,
            "waiting_time": self.waiting_time,
            "replanning_events": self.replanning_events,
            "triggered_exceptions": self.triggered_exceptions,
            "recovery_operations": self.recovery_operations,
            "recovery_time": self.recovery_time,
            "mof": self.mof,
            "tst": self.tst,
        }


@dataclass
class _Candidate:
    score: float
    finish: float
    op: OperationInstance
    ws_id: int
    slot_idx: int
    agv_idx: int
    logistics_start: float
    logistics_finish: float
    start: float
    processing: float
    setup: float
    logistics: float
    waiting: float
    blocking: float


class PCFactory:
    def __init__(self, target_model: Optional[TargetModelLoader] = None):
        self.tm = target_model or TargetModelLoader()
        if not getattr(self.tm, "data", None):
            ok, msg = self.tm.load()
            if not ok:
                raise RuntimeError(msg)
        self.palu_caps = self._palu_capabilities()
        self.palu_quantities = self._palu_quantities()
        self.palu_normal_capacity_ratios = self._palu_normal_capacity_ratios()
        self.workstations = self._load_workstations()
        self.ws_by_id = {ws.workstation_id: ws for ws in self.workstations}
        self.warehouse_coord = self._warehouse_coord()
        self.buffer_capacity = self._buffer_capacity()
        self.logistics_count = int(self.palu_quantities.get(2, 6))
        configured_weights = (
            (self.tm.data.get("case_study_model") or {}).get("falang_dispatch_weights") or {}
        )
        self.falang_dispatch_weights = {
            "finish": float(configured_weights.get("finish", 1.0)),
            "start": float(configured_weights.get("start", 0.0)),
            "planned_start": float(configured_weights.get("planned_start", 0.0)),
            "setup": float(configured_weights.get("setup", -1.5)),
            "tardiness": float(configured_weights.get("tardiness", 0.0)),
            "waiting": float(configured_weights.get("waiting", 0.0)),
            "remaining_processing": float(configured_weights.get("remaining_processing", 0.0)),
            "arrival_time": float(configured_weights.get("arrival_time", 0.0)),
        }
    def _palu_capabilities(self) -> Dict[int, set[Tuple[str, str, str]]]:
        out: Dict[int, set[Tuple[str, str, str]]] = {}
        for palu in (self.tm.data.get("instruction_capability_set") or {}).get("palu_definitions", []):
            pid = palu.get("palu_id")
            if isinstance(pid, int):
                out[pid] = {
                    (str(cap.get("opcode")), str(cap.get("func")), str(cap.get("rp")))
                    for cap in palu.get("supported_capabilities", [])
                }
        return out

    def _palu_quantities(self) -> Dict[int, int]:
        out: Dict[int, int] = {}
        for palu in (self.tm.data.get("instruction_capability_set") or {}).get("palu_definitions", []):
            pid = palu.get("palu_id")
            if isinstance(pid, int):
                out[pid] = int(palu.get("quantity", 1))
        return out

    def _palu_normal_capacity_ratios(self) -> Dict[int, float]:
        out: Dict[int, float] = {}
        for palu in (self.tm.data.get("instruction_capability_set") or {}).get("palu_definitions", []):
            pid = palu.get("palu_id")
            if isinstance(pid, int) and "normal_capacity_ratio" in palu:
                out[pid] = float(palu["normal_capacity_ratio"])
        return out

    def _warehouse_coord(self) -> Tuple[int, int]:
        layout = ((self.tm.data.get("scheduling_model") or {}).get("logistics_layout") or {})
        coord = (layout.get("warehouse") or {}).get("coord") or [-1, -1]
        return int(coord[0]), int(coord[1])

    def _load_workstations(self) -> List[Workstation]:
        layout = ((self.tm.data.get("scheduling_model") or {}).get("logistics_layout") or {})
        coord_by_ws = {
            int(pm["workstation_id"]): tuple(pm.get("coord") or [0, 0])
            for pm in layout.get("process_modules", [])
        }
        out: List[Workstation] = []
        for ws in (self.tm.data.get("addressing_model") or {}).get("workstation_mapping", []):
            wid = int(ws.get("workstation_id"))
            palus = [int(p) for p in ws.get("assigned_palus") or []]
            caps = set()
            for pid in palus:
                caps.update(self.palu_caps.get(pid, set()))
            pm_type = str(ws.get("pm_type") or "")
            redundant = "redundant" in pm_type
            slots = max(1, sum(1 for pid in palus if pid != 2))
            if redundant:
                normal_ratio = min(
                    (self.palu_normal_capacity_ratios[pid] for pid in palus if pid in self.palu_normal_capacity_ratios),
                    default=0.4,
                )
                slots = max(1, math.ceil(slots * normal_ratio))
            coord = coord_by_ws.get(wid, (0, 0))
            out.append(
                Workstation(
                    workstation_id=wid,
                    pm_type=pm_type,
                    coord=(int(coord[0]), int(coord[1])),
                    registers=[int(r) for r in ws.get("assigned_registers") or []],
                    palus=palus,
                    capabilities=caps,
                    slots=slots,
                    redundant=redundant,
                )
            )
        return out

    def _buffer_capacity(self) -> int:
        caps = [
            int(entry.get("capacity", 2))
            for entry in (self.tm.data.get("storage_model") or {}).get("capacity_constraints", [])
            if entry.get("type") == "register"
        ]
        return min(caps) if caps else 2

    def clone_workstations(self) -> Dict[int, Workstation]:
        clones = {
            ws.workstation_id: Workstation(
                workstation_id=ws.workstation_id,
                pm_type=ws.pm_type,
                coord=ws.coord,
                registers=list(ws.registers),
                palus=list(ws.palus),
                capabilities=set(ws.capabilities),
                slots=ws.slots,
                redundant=ws.redundant,
            )
            for ws in self.workstations
        }
        for ws in clones.values():
            ws.reset()
        return clones

    def candidates(self, op: OperationTemplate) -> List[Workstation]:
        cap = (op.opcode, op.func, op.rp)
        return [ws for ws in self.workstations if cap in ws.capabilities]

    def find_palu_for_op(self, ws_id: int, opcode: str, func: str, rp: str) -> Optional[int]:
        ws = self.ws_by_id.get(ws_id)
        if not ws:
            return None
        cap_key = (opcode, func, rp)
        for palu_id in ws.palus:
            if cap_key in self.palu_caps.get(palu_id, set()):
                return palu_id
        return None

    def processing_time(self, op: OperationTemplate) -> float:
        return float(self.tm.get_expected_time(op.opcode, op.func, op.rp) or 0.0)

    def setup_time(self, ws: Workstation, op: OperationTemplate) -> float:
        if not op.component or not op.model:
            return 0.0
        prev = ws.last_model_by_component.get(op.component)
        if not prev:
            return 0.0
        return self.tm.get_setup_time(
            f"component={op.component};model={prev}",
            f"component={op.component};model={op.model}",
        )

    def logistics_time(self, source_ws: Optional[int], target_ws: int) -> float:
        src_coord = self.warehouse_coord if source_ws is None else self.ws_by_id[source_ws].coord
        dst_coord = self.ws_by_id[target_ws].coord
        distance = abs(src_coord[0] - dst_coord[0]) + abs(src_coord[1] - dst_coord[1])
        if source_ws is None:
            return 8.0 + 2.0 * distance
        if source_ws == target_ws:
            return 1.0
        return 4.0 + 2.0 * distance


def operation_templates(order: PCOrder) -> List[OperationTemplate]:
    templates = [
        OperationTemplate("Base_Assembly", "assembly", "000001", "001", "00001", "Motherboard", order.components["Motherboard"]),
        OperationTemplate("CPU_Install", "assembly", "000001", "001", "00010", "CPU", order.components["CPU"]),
        OperationTemplate("Memory_Install", "assembly", "000001", "001", "00011", "RAM", order.components["RAM"]),
        OperationTemplate("Storage_Install", "assembly", "000001", "001", "00100", "Storage", order.components["Storage"]),
    ]
    if order.components["GPU"] != "GPU_NONE":
        templates.append(OperationTemplate("GPU_Install", "assembly", "000001", "001", "00101", "GPU", order.components["GPU"]))
    if order.components["Cooling"] != "COOLING_NONE":
        templates.append(OperationTemplate("Cooling_Install", "assembly", "000001", "001", "00110", "Cooling", order.components["Cooling"]))
    templates.extend(
        [
            OperationTemplate("Power_Install", "assembly", "000001", "001", "00111"),
            OperationTemplate("Case_Assembly", "assembly", "000001", "001", "01000"),
        ]
    )
    for test in QUALITY_GRADES[order.quality_grade]["tests"]:
        rp = {"Power_On_Test": "00001", "Benchmark_Test": "00010", "Thermal_Test": "00011"}[test]
        templates.append(OperationTemplate(test, "testing", "000001", "011", rp))
    templates.extend(
        [
            OperationTemplate("Accessory_Check", "packaging", "000001", "011", "00100"),
            OperationTemplate("Packaging", "packaging", "000001", "001", "01001"),
        ]
    )
    return templates


class DynamicSimulator:
    """Discrete-event simulator with state-aware compilation."""

    def __init__(self, factory: PCFactory):
        self.factory = factory
        self.falang_jit = StateAwareJITCompiler(factory)
        self._scenario_seed = 0

    def run(
        self,
        orders: Sequence[PCOrder],
        method: str,
        seed: int,
        load: str,
        method_config: Optional[Dict[str, object]] = None,
    ) -> Tuple[SimulationMetrics, List[ScheduledOperation]]:
        method_config = method_config or {}
        self._active_method_config = method_config
        base_method = str(method_config.get("base_method", method))
        is_falang_method = base_method == "falang"
        if method not in METHODS and not is_falang_method:
            raise ValueError(f"Unknown method: {method}")

        if "method_seed_index" in method_config:
            method_seed_index = int(method_config["method_seed_index"])
        elif base_method in METHODS:
            method_seed_index = METHODS.index(base_method)
        else:
            method_seed_index = 0
        rng = random.Random((seed + 1) * 1009 + method_seed_index * 9176)
        self._scenario_seed = seed
        orders = sorted(orders, key=lambda item: item.arrival_time)
        workstations = self.factory.clone_workstations()
        self._workstations = workstations
        self._workstations = workstations
        agv_available = [0.0 for _ in range(max(1, self.factory.logistics_count))]
        buffer_intervals: Dict[int, List[Tuple[float, float]]] = {ws_id: [] for ws_id in workstations}
        failed_until: Dict[int, float] = {}
        repair_events: List[Tuple[float, int]] = []
        order_location: Dict[int, Optional[int]] = {}
        order_completion: Dict[int, float] = {}
        next_seq: Dict[int, int] = {}
        op_chains: Dict[int, List[OperationInstance]] = {}
        scheduled: List[ScheduledOperation] = []
        arrived: set[int] = set()
        completed: set[int] = set()
        next_arrival_idx = 0
        current_time = orders[0].arrival_time if orders else 0.0
        self._current_time = current_time
        replanning_events = 0
        triggered_exceptions = 0

        self._pending_recoveries: List[RecoveryOperation] = []
        self._recovery_id_counter = 0
        in_recovery: Dict[int, List[RecoveryOperation]] = {}  # Order ID to pending recovery steps.
        recovery_next_seq: Dict[int, int] = {}                 # Order ID to the operation sequence resumed after recovery.

        compiler_plan: Dict[int, Tuple[Optional[int], float, float]] = {}
        jit_result: Optional[JITCompileResult] = None
        needs_recompile = is_falang_method
        pending_completions_since_recompile = 0
        REOPT_COMPLETION_BATCH = 4

        initial_compile_options = dict(method_config.get("initial_compile_options", {}))
        recompile_options = dict(method_config.get("recompile_options", {}))
        defer_initial_compile = bool(method_config.get("defer_initial_compile", False))
        use_catch_plans = bool(method_config.get("use_catch_plans", True))
        compile_count = 0

        def next_compile_options() -> Dict[str, object]:
            nonlocal compile_count
            options = initial_compile_options if compile_count == 0 else recompile_options
            compile_count += 1
            return dict(options)

        op_id = 0
        for order in orders:
            chain: List[OperationInstance] = []
            for seq, template in enumerate(operation_templates(order)):
                chain.append(
                    OperationInstance(
                        op_id=op_id,
                        order=order,
                        seq=seq,
                        template=template,
                        ready_time=order.arrival_time if seq == 0 else float("inf"),
                        preferred_ws=None,
                        compiler_rank=seq,
                        compiler_start=0.0,
                    )
                )
                op_id += 1
            op_chains[order.order_id] = chain
            next_seq[order.order_id] = 0
            order_location[order.order_id] = None

        if is_falang_method and not defer_initial_compile:
            runtime_state = self._build_runtime_state(
                current_time, workstations, agv_available, buffer_intervals,
                order_location, completed, next_seq, op_chains, failed_until,
                arrived, [],
            )
            runtime_state.palu_busy_until = self._build_palu_busy_for_jit(
                scheduled, workstations, op_chains, agv_available, current_time, failed_until,
            )
            jit_result = self.falang_jit.compile_with_state(
                runtime_state, list(orders), op_chains, options=next_compile_options()
            )
            compiler_plan = jit_result.op_plan
            replanning_events += 1

        while len(completed) < len(orders):
            repair_triggered = False
            while repair_events and min(repair_events)[0] <= current_time + 1e-9:
                repair_time, repaired_ws = min(repair_events)
                repair_events.remove((repair_time, repaired_ws))
                if failed_until.get(repaired_ws, 0.0) <= repair_time + 1e-9:
                    failed_until.pop(repaired_ws, None)
                repair_triggered = True
                replanning_events += 1

            arrival_triggered = False
            while next_arrival_idx < len(orders) and orders[next_arrival_idx].arrival_time <= current_time + 1e-9:
                order = orders[next_arrival_idx]
                arrived.add(order.order_id)
                arrival_triggered = True
                replanning_events += 1
                next_arrival_idx += 1

            if is_falang_method and (arrival_triggered or repair_triggered):
                needs_recompile = True
                pending_completions_since_recompile = 0

            if is_falang_method and needs_recompile:
                runtime_state = self._build_runtime_state(
                    current_time, workstations, agv_available, buffer_intervals,
                    order_location, completed, next_seq, op_chains, failed_until,
                    arrived, compiler_plan,
                )
                runtime_state.palu_busy_until = self._build_palu_busy_for_jit(
                    scheduled, workstations, op_chains, agv_available, current_time, failed_until,
                )
                jit_result = self.falang_jit.compile_with_state(
                    runtime_state, list(orders), op_chains, options=next_compile_options()
                )
                compiler_plan = jit_result.op_plan
                replanning_events += 1
                needs_recompile = False

                for oid, chain in op_chains.items():
                    for op in chain:
                        plan = compiler_plan.get(op.op_id)
                        if plan:
                            op.preferred_ws, op.compiler_start, _ = plan

            self._current_time = current_time
            recovery_ready = [
                r for r in self._pending_recoveries
                if r.trigger_time <= current_time + 1e-9
            ]
            if recovery_ready:
                selected = self._select_recovery_op(
                    recovery_ready, base_method, workstations, agv_available,
                    buffer_intervals, order_location, failed_until, rng,
                )
                if selected is not None:
                    self._pending_recoveries.remove(selected)
                    rec_schedule = self._commit_recovery(
                        selected, method, seed, load, workstations,
                        agv_available, buffer_intervals, order_location,
                    )
                    scheduled.append(rec_schedule)
                    current_time = max(current_time, rec_schedule.finish)

                    rec_oid = selected.order_id
                    remaining = [r for r in self._pending_recoveries if r.order_id == rec_oid]
                    if not remaining and rec_oid in in_recovery:
                        del in_recovery[rec_oid]
                        advance_seq = recovery_next_seq.pop(rec_oid, 0)
                        chain = op_chains.get(rec_oid, [])
                        if advance_seq < len(chain):
                            next_seq[rec_oid] = advance_seq
                            chain[advance_seq].ready_time = rec_schedule.finish
                        else:
                            completed.add(rec_oid)
                            order_completion[rec_oid] = rec_schedule.finish
                    continue

            ready = self._ready_operations(arrived, completed, next_seq, op_chains, current_time, in_recovery)
            if not ready:
                current_time = self._next_event_time(
                    current_time, next_arrival_idx, orders, arrived, completed,
                    next_seq, op_chains, repair_events,
                )
                self._current_time = current_time
                continue

            # Dependencies and resource availability can permit an earlier start than the plan.
            if is_falang_method:
                ready = self._apply_dynamic_early_start(ready, compiler_plan, current_time, workstations)

            external_event = min(
                orders[next_arrival_idx].arrival_time if next_arrival_idx < len(orders) else float("inf"),
                min((t for t, _ in repair_events), default=float("inf")),
            )
            if is_falang_method:
                candidate = self._select_candidate_falang(ready, workstations, agv_available, buffer_intervals, order_location, failed_until, rng, compiler_plan, current_time)
            elif method == "jmedd":
                candidate = self._choose_candidate_jmedd(ready, workstations, agv_available, buffer_intervals, order_location, failed_until, rng)
            else:
                candidate = self._choose_candidate(ready, method, workstations, agv_available, buffer_intervals, order_location, failed_until, rng)
            candidate = self._apply_runtime_penalty(candidate)
            if candidate.start > external_event + 1e-9:
                current_time = external_event
                self._current_time = current_time
                continue

            op_schedule = self._commit(candidate, method, seed, load, workstations, agv_available, buffer_intervals, order_location)
            scheduled.append(op_schedule)

            completion_triggered = False

            exc_type = self._exception_type(candidate.op, rng)
            if exc_type is not None:
                triggered_exceptions += 1
                replanning_events += 1

                if exc_type == "MachineFailure":
                    # Resource-topology changes trigger recompilation.
                    if is_falang_method:
                        needs_recompile = True
                        pending_completions_since_recompile = 0
                    repair_rng = self._scenario_rng(candidate.op.op_id, candidate.ws_id, 3)
                    repair_time = op_schedule.finish + repair_rng.uniform(15.0, 30.0)
                    failed_until[candidate.ws_id] = max(failed_until.get(candidate.ws_id, 0.0), repair_time)
                    repair_events.append((repair_time, candidate.ws_id))
                else:
                    # Material and quality events activate compiled catch paths.
                    recovery_ops = self._generate_recovery_ops(
                        exc_type, candidate.op, op_schedule.finish,
                        "falang" if (is_falang_method and use_catch_plans) else method, jit_result, rng,
                    )
                    self._pending_recoveries.extend(recovery_ops)
                    oid = candidate.op.order.order_id
                    if oid not in in_recovery:
                        in_recovery[oid] = []
                    in_recovery[oid].extend(recovery_ops)
                    recovery_next_seq[oid] = candidate.op.seq + 1

            oid = candidate.op.order.order_id
            next_idx = candidate.op.seq + 1

            if oid in in_recovery:
                completion_triggered = True
            elif next_idx < len(op_chains[oid]):
                next_seq[oid] = next_idx
                op_chains[oid][next_idx].ready_time = op_schedule.finish
                completion_triggered = True
            else:
                completed.add(oid)
                order_completion[oid] = op_schedule.finish
                completion_triggered = True

            if is_falang_method and completion_triggered and oid not in in_recovery:
                pending_completions_since_recompile += 1
                if pending_completions_since_recompile >= REOPT_COMPLETION_BATCH:
                    needs_recompile = True
                    pending_completions_since_recompile = 0

            current_time = max(current_time, min(op_schedule.start, op_schedule.finish))
            self._current_time = current_time

        metrics = self._metrics(method, seed, load, orders, order_completion, scheduled, workstations, replanning_events, triggered_exceptions)
        return metrics, scheduled

    def _ready_operations(self, arrived, completed, next_seq, op_chains, current_time, in_recovery=None) -> List[OperationInstance]:
        ready = []
        if in_recovery is None:
            in_recovery = {}
        for oid in arrived:
            if oid in completed:
                continue
            if oid in in_recovery:
                continue
            idx = next_seq[oid]
            op = op_chains[oid][idx]
            if op.ready_time <= current_time + 1e-9:
                ready.append(op)
        return ready

    def _next_event_time(self, current_time, next_arrival_idx, orders, arrived, completed, next_seq, op_chains, repair_events) -> float:
        candidates: List[float] = []
        if next_arrival_idx < len(orders):
            candidates.append(orders[next_arrival_idx].arrival_time)
        candidates.extend(t for t, _ in repair_events)
        for oid in arrived:
            if oid not in completed:
                candidates.append(op_chains[oid][next_seq[oid]].ready_time)
        for r in getattr(self, '_pending_recoveries', []):
            if r.trigger_time > current_time + 1e-9:
                candidates.append(r.trigger_time)
        # Include slot releases that can unblock recovery steps.
        pending = getattr(self, '_pending_recoveries', [])
        wss = getattr(self, '_workstations', None)
        if pending and wss is not None:
            for ws in wss.values():
                for slot_time in ws.slot_available:
                    if slot_time > current_time + 1e-9:
                        candidates.append(slot_time)
        if not candidates:
            future_events: List[float] = []
        else:
            future_events = [t for t in candidates if t > current_time + 1e-9]

        if not future_events:
            pending = getattr(self, '_pending_recoveries', [])
            wss = getattr(self, '_workstations', None)
            if pending and wss is not None:
                for r in pending:
                    if r.trigger_time <= current_time + 1e-9:
                        for ws in wss.values():
                            if any(cap[1] == "100" for cap in ws.capabilities):
                                slot_ready = min(ws.slot_available) if ws.slot_available else 0.0
                                if slot_ready > current_time + 1e-9:
                                    future_events.append(slot_ready)
            if not future_events and wss is not None:
                for ws in wss.values():
                    slot_ready = min(ws.slot_available) if ws.slot_available else 0.0
                    if slot_ready > current_time + 1e-9:
                        future_events.append(slot_ready)
            if not future_events:
                return current_time + 0.1

        return min(future_events)

    def _build_palu_busy_for_jit(
        self,
        scheduled: List[ScheduledOperation],
        workstations: Dict[int, Workstation],
        op_chains: Dict[int, List[OperationInstance]],
        agv_available: List[float],
        current_time: float,
        failed_until: Dict[int, float],
    ) -> Dict[int, List[float]]:
        """Return absolute PALU availability times from operations currently in flight."""
        palu_busy: Dict[int, List[float]] = {}

        for s in scheduled:
            if s.start <= current_time + 1e-9 and s.finish > current_time + 1e-9:
                chain = op_chains.get(s.order_id, [])
                palu_id = None
                for op in chain:
                    if op.op_id == s.op_id:
                        t = op.template
                        palu_id = self.factory.find_palu_for_op(
                            s.workstation_id, t.opcode, t.func, t.rp,
                        )
                        break
                if palu_id is not None:
                    palu_busy.setdefault(palu_id, []).append(s.finish)

        # Logistics uses PALU 2.
        for agv_finish in agv_available:
            if agv_finish > current_time + 1e-9:
                palu_busy.setdefault(2, []).append(agv_finish)

        # A failed device keeps its processing PALUs busy until repair.
        for ws_id, repair_time in failed_until.items():
            if repair_time > current_time + 1e-9:
                ws = workstations.get(ws_id)
                if ws:
                    for palu_id in ws.palus:
                        if palu_id != 2:  # The device outage does not block the logistics PALU.
                            palu_busy.setdefault(palu_id, []).append(repair_time)

        return palu_busy

    def _build_runtime_state(
        self,
        current_time: float,
        workstations: Dict[int, Workstation],
        agv_available: List[float],
        buffer_intervals: Dict[int, List[Tuple[float, float]]],
        order_location: Dict[int, Optional[int]],
        completed: set,
        next_seq: Dict[int, int],
        op_chains: Dict[int, List[OperationInstance]],
        failed_until: Dict[int, float],
        arrived: set,
        compiler_plan: Dict[int, Tuple[Optional[int], float, float]],
    ) -> RuntimeState:
        device_busy_until: Dict[str, float] = {}
        for ws_id, ws in workstations.items():
            for slot_time in ws.slot_available:
                if slot_time > current_time + 1e-9:
                    for cap in ws.capabilities:
                        opcode, func, rp = cap
                        if func == "001":
                            dev_name = "Assembly_PM"
                        elif func == "011":
                            dev_name = "Test_PM"
                        elif func == "100":
                            dev_name = "Recovery_PM"
                        else:
                            continue
                        existing = device_busy_until.get(dev_name, 0.0)
                        device_busy_until[dev_name] = max(existing, slot_time)
                    break

        failed_devices: Dict[str, float] = {}
        for ws_id, repair_time in failed_until.items():
            ws = workstations.get(ws_id)
            if ws and repair_time > current_time + 1e-9:
                for cap in ws.capabilities:
                    if cap[1] == "001":
                        failed_devices["Assembly_PM"] = max(failed_devices.get("Assembly_PM", 0.0), repair_time)
                    elif cap[1] == "011":
                        failed_devices["Test_PM"] = max(failed_devices.get("Test_PM", 0.0), repair_time)

        order_loc = dict(order_location)
        completed_seqs: Dict[int, int] = {}
        executing_ops: Dict[int, Tuple[OperationInstance, float]] = {}
        for oid in arrived:
            if oid in completed:
                chain = op_chains.get(oid, [])
                completed_seqs[oid] = len(chain) - 1 if chain else -1
            else:
                idx = next_seq.get(oid, 0)
                completed_seqs[oid] = idx - 1
                if idx > 0:
                    chain = op_chains.get(oid, [])
                    if idx - 1 < len(chain):
                        op = chain[idx - 1]
                        plan = compiler_plan.get(op.op_id)
                        if plan:
                            _, _, planned_finish = plan
                            executing_ops[oid] = (op, planned_finish)

        buffer_occupancy: Dict[int, int] = {}
        for ws_id, intervals in buffer_intervals.items():
            occupied = sum(1 for s, e in intervals if s <= current_time < e)
            if occupied > 0:
                buffer_occupancy[ws_id] = occupied

        device_last_params: Dict[str, str] = {}
        for ws_id, ws in workstations.items():
            for comp, model in ws.last_model_by_component.items():
                params = f"component={comp};model={model}"
                for cap in ws.capabilities:
                    if cap[1] == "001":
                        device_last_params["Assembly_PM"] = params
                    elif cap[1] == "011":
                        device_last_params["Test_PM"] = params
                    break

        return RuntimeState(
            current_time=current_time,
            device_busy_until=device_busy_until,
            order_location=order_loc,
            completed_seqs=completed_seqs,
            executing_ops=executing_ops,
            buffer_occupancy=buffer_occupancy,
            failed_devices=failed_devices,
            device_last_params=device_last_params,
        )

    def _apply_dynamic_early_start(
        self,
        ready: List[OperationInstance],
        compiler_plan: Dict[int, Tuple[Optional[int], float, float]],
        current_time: float,
        workstations: Dict[int, Workstation],
    ) -> List[OperationInstance]:
        """Allow an operation to start before its planned time when dependencies and resources permit."""
        for op in ready:
            plan = compiler_plan.get(op.op_id)
            if plan:
                ws_id, planned_start, planned_finish = plan
                op.preferred_ws = ws_id
                op.compiler_start = planned_start

                if ws_id is not None and current_time < planned_start - 1e-9:
                    ws = workstations.get(ws_id)
                    if ws:
                        earliest_slot = min(ws.slot_available) if ws.slot_available else 0.0
                        if earliest_slot <= current_time + 1e-9:
                            op.compiler_start = current_time
        return ready

    def _select_candidate_falang(self, ready, workstations, agv_available, buffer_intervals, order_location, failed_until, rng, compiler_plan, current_time) -> _Candidate:
        """Select ready operations by compiler-plan order and preferred workstation."""
        def _plan_order(op: OperationInstance) -> float:
            plan = compiler_plan.get(op.op_id)
            return plan[1] if (plan and plan[1] is not None) else float("inf")
        ordered = sorted(ready, key=_plan_order)

        best = None
        for op in ordered:
            plan = compiler_plan.get(op.op_id)
            compiler_ws_id = plan[0] if (plan and plan[0] is not None) else None

            # Try the planned workstation first, then other compatible workstations.
            if compiler_ws_id is not None:
                compiler_ws = workstations.get(compiler_ws_id)
                if compiler_ws is not None and (op.template.opcode, op.template.func, op.template.rp) in compiler_ws.capabilities:
                    ws_list = [compiler_ws]
                else:
                    ws_list = [w for w in workstations.values()
                              if (op.template.opcode, op.template.func, op.template.rp) in w.capabilities]
            else:
                ws_list = [w for w in workstations.values()
                          if (op.template.opcode, op.template.func, op.template.rp) in w.capabilities]

            for ws in ws_list:
                slot_idx, pm_ready = min(enumerate(ws.slot_available), key=lambda item: item[1])
                pm_ready = max(pm_ready, failed_until.get(ws.workstation_id, 0.0))
                src_ws = order_location.get(op.order.order_id)
                log_time = self._uncertain_time(
                    self.factory.logistics_time(src_ws, ws.workstation_id), 0.30,
                    self._scenario_rng(op.op_id, ws.workstation_id, 1),
                )
                agv_idx, agv_ready = min(enumerate(agv_available), key=lambda item: item[1])
                logistics_start = max(op.ready_time, agv_ready)
                logistics_finish = self._first_buffer_slot(logistics_start + log_time, buffer_intervals[ws.workstation_id], self.factory.buffer_capacity)
                blocking = max(0.0, logistics_finish - (logistics_start + log_time))
                setup = self.factory.setup_time(ws, op.template)
                processing = self._uncertain_time(
                    self.factory.processing_time(op.template), 0.25,
                    self._scenario_rng(op.op_id, ws.workstation_id, 2),
                )
                start = max(logistics_finish, pm_ready)
                finish = start + processing + setup
                waiting = max(0.0, start - logistics_finish)

                # Retain the planned workstation when it is available.
                weights = self.factory.falang_dispatch_weights
                planned_start = float(plan[1] or 0.0) if plan else 0.0
                remaining_processing = 0.0
                order_templates = None
                if weights["remaining_processing"]:
                    order_templates = operation_templates(op.order)
                if weights["remaining_processing"]:
                    remaining_processing = sum(
                        self.factory.processing_time(template)
                        for template in order_templates[op.seq:]
                    )
                score = (
                    weights["finish"] * finish
                    + weights["start"] * start
                    + weights["planned_start"] * planned_start
                    + weights["tardiness"] * max(0.0, finish - op.order.due_time)
                    + weights["waiting"] * waiting
                    + weights["remaining_processing"] * remaining_processing
                    + weights["arrival_time"] * op.order.arrival_time
                )
                if compiler_ws_id is not None and ws.workstation_id == compiler_ws_id:
                    score += weights["setup"] * setup

                candidate = _Candidate(score, finish, op, ws.workstation_id, slot_idx, agv_idx, logistics_start, logistics_finish, start, processing, setup, log_time, waiting, blocking)
                if best is None or (candidate.score, candidate.finish) < (best.score, best.finish):
                    best = candidate

        if best is None:
            return self._choose_candidate(ready, "fcfs", workstations, agv_available, buffer_intervals, order_location, failed_until, rng)
        return best

    def _choose_candidate(self, ready, method, workstations, agv_available, buffer_intervals, order_location, failed_until, rng) -> _Candidate:
        ordered = sorted(ready, key=lambda op: self._priority(method, op, rng))
        if method == "ga":
            ordered = self._ga_reorder(ordered, workstations, rng)
        best = None
        for op in ordered:
            candidate = self._best_candidate_for_op(op, method, workstations, agv_available, buffer_intervals, order_location, failed_until, rng)
            if best is None or (candidate.score, candidate.finish, candidate.op.order.order_id) < (best.score, best.finish, best.op.order.order_id):
                best = candidate
        assert best is not None
        return best

    def _choose_candidate_jmedd(self, ready, workstations, agv_available, buffer_intervals, order_location, failed_until, rng) -> _Candidate:
        """Prefer zero-setup candidates, then apply modified earliest-due-date priority."""
        zero_setup_candidates: List[_Candidate] = []
        for op in ready:
            ws_list = [
                w for w in workstations.values()
                if (op.template.opcode, op.template.func, op.template.rp) in w.capabilities
            ]
            non_redundant = [w for w in ws_list if not w.redundant]
            if non_redundant:
                ws_list = non_redundant

            for ws in ws_list:
                if self.factory.setup_time(ws, op.template) <= 1e-9:
                    candidate = self._candidate_for_op_ws(
                        op, ws, workstations, agv_available, buffer_intervals,
                        order_location, failed_until, rng,
                    )
                    if candidate is not None:
                        zero_setup_candidates.append(candidate)

        if zero_setup_candidates:
            zero_setup_candidates.sort(
                key=lambda c: (
                    self._priority("jmedd", c.op, rng),
                    c.finish,
                    c.op.order.order_id,
                )
            )
            return zero_setup_candidates[0]

        ordered = sorted(ready, key=lambda op: self._priority("jmedd", op, rng))
        best = None
        for op in ordered:
            candidate = self._best_candidate_for_op(
                op, "jmedd", workstations, agv_available, buffer_intervals,
                order_location, failed_until, rng,
            )
            if best is None or (candidate.finish, candidate.op.order.order_id) < (best.finish, best.op.order.order_id):
                best = candidate
        assert best is not None
        return best

    def _candidate_for_op_ws(
        self, op, ws, workstations, agv_available, buffer_intervals,
        order_location, failed_until, rng,
    ) -> Optional[_Candidate]:
        """Evaluate one operation/workstation pair and return a feasible candidate or None."""
        if failed_until.get(ws.workstation_id, 0.0) > op.ready_time + 1e-9:
            return None
        slot_idx, pm_ready = min(enumerate(ws.slot_available), key=lambda item: item[1])
        pm_ready = max(pm_ready, failed_until.get(ws.workstation_id, 0.0))
        src_ws = order_location.get(op.order.order_id)
        log_time = self._uncertain_time(
            self.factory.logistics_time(src_ws, ws.workstation_id), 0.30,
            self._scenario_rng(op.op_id, ws.workstation_id, 1),
        )
        agv_idx, agv_ready = min(enumerate(agv_available), key=lambda item: item[1])
        logistics_start = max(op.ready_time, agv_ready)
        logistics_finish = self._first_buffer_slot(
            logistics_start + log_time,
            buffer_intervals.get(ws.workstation_id, []),
            self.factory.buffer_capacity,
        )
        blocking = max(0.0, logistics_finish - (logistics_start + log_time))
        setup = self.factory.setup_time(ws, op.template)
        processing = self._uncertain_time(
            self.factory.processing_time(op.template), 0.25,
            self._scenario_rng(op.op_id, ws.workstation_id, 2),
        )
        start = max(logistics_finish, pm_ready)
        finish = start + processing + setup
        waiting = max(0.0, start - logistics_finish)
        score = finish
        return _Candidate(
            score, finish, op, ws.workstation_id, slot_idx, agv_idx,
            logistics_start, logistics_finish, start, processing, setup,
            log_time, waiting, blocking,
        )

    def _apply_runtime_penalty(self, candidate: _Candidate) -> _Candidate:
        penalties = (getattr(self, "_active_method_config", {}) or {}).get("runtime_penalties", {}) or {}
        logistics_factor = float(penalties.get("logistics_factor", 0.0) or 0.0)
        waiting_factor = float(penalties.get("waiting_factor", 0.0) or 0.0)
        if logistics_factor <= 0.0 and waiting_factor <= 0.0:
            return candidate

        logistics_delay = max(0.0, candidate.logistics * logistics_factor)
        waiting_delay = max(0.0, candidate.processing * waiting_factor)
        delay = logistics_delay + waiting_delay
        if delay <= 0.0:
            return candidate

        return _Candidate(
            candidate.score + delay,
            candidate.finish + delay,
            candidate.op,
            candidate.ws_id,
            candidate.slot_idx,
            candidate.agv_idx,
            candidate.logistics_start,
            candidate.logistics_finish + logistics_delay,
            candidate.start + delay,
            candidate.processing,
            candidate.setup,
            candidate.logistics + logistics_delay,
            candidate.waiting + waiting_delay,
            candidate.blocking + logistics_delay,
        )

    def _remaining_processing_time(self, op: OperationInstance) -> float:
        templates = operation_templates(op.order)
        total = 0.0
        for i in range(op.seq, len(templates)):
            total += self.factory.processing_time(templates[i])
        return total

    def _priority(self, method: str, op: OperationInstance, rng: random.Random) -> Tuple[float, ...]:
        order = op.order
        remaining = max(1.0, order.nominal_lead_time - (op.ready_time - order.arrival_time))
        slack = order.due_time - op.ready_time - remaining
        if method == "fcfs":
            return order.arrival_time, order.order_id, op.seq
        if method == "edd":
            return order.due_time, order.order_id, op.seq
        if method == "atc":
            p = max(1.0, remaining / 10.0)
            score = math.exp(-max(slack, 0.0) / (2.0 * p)) / p
            return -score, order.due_time, op.seq
        if method == "falang":
            return op.compiler_start, op.compiler_rank, order.due_time, order.order_id
        if method == "jmedd":
            # MEDD priority is max(due_time, current_time + remaining_processing_time).
            rem = self._remaining_processing_time(op)
            medd = max(order.due_time, op.ready_time + rem)
            return medd, order.order_id, op.seq
        if method == "jit":
            # JIT priority is squared deviation from the processing-adjusted due date.
            rem = self._remaining_processing_time(op)
            jit_slack = order.due_time - rem - op.ready_time
            return jit_slack * jit_slack, order.order_id, op.seq
        return rng.random(), order.due_time, op.seq

    def _ga_reorder(self, ready: List[OperationInstance], workstations: Dict[int, Workstation], rng: random.Random) -> List[OperationInstance]:
        if len(ready) <= 2:
            return ready
        pool = ready[: min(len(ready), 10)]
        population = []
        for _ in range(6):
            chrom = list(pool)
            rng.shuffle(chrom)
            population.append(chrom)
        for _ in range(6):
            population.sort(key=lambda chrom: self._sequence_score(chrom, workstations))
            parent_a, parent_b = population[0], population[1]
            cut = rng.randrange(1, len(pool))
            child_prefix = parent_a[:cut]
            child = child_prefix + [op for op in parent_b if op not in child_prefix]
            if rng.random() < 0.25 and len(child) > 1:
                i, j = rng.sample(range(len(child)), 2)
                child[i], child[j] = child[j], child[i]
            population[-1] = child
        best = min(population, key=lambda chrom: self._sequence_score(chrom, workstations))
        return best + [op for op in ready if op not in best]

    def _sequence_score(self, seq: List[OperationInstance], workstations: Dict[int, Workstation]) -> float:
        score = 0.0
        for rank, op in enumerate(seq):
            cap = (op.template.opcode, op.template.func, op.template.rp)
            earliest = min(min(ws.slot_available) for ws in workstations.values() if cap in ws.capabilities)
            score += 2.0 * max(0.0, earliest + rank - op.order.due_time) + 0.1 * rank * op.order.nominal_lead_time
        return score

    def _best_candidate_for_op(self, op, method, workstations, agv_available, buffer_intervals, order_location, failed_until, rng) -> _Candidate:
        candidates = [workstations[ws.workstation_id] for ws in self.factory.candidates(op.template)]
        if method in ("fcfs", "edd", "atc", "jmedd", "jit"):
            candidates = [ws for ws in candidates if not ws.redundant] or candidates
        if method == "falang" and op.preferred_ws is not None and op.preferred_ws in workstations:
            preferred = workstations[op.preferred_ws]
            if (op.template.opcode, op.template.func, op.template.rp) in preferred.capabilities:
                candidates = [preferred]
        elif method == "falang":
            pass

        best = None
        for ws in candidates:
            slot_idx, pm_ready = min(enumerate(ws.slot_available), key=lambda item: item[1])
            pm_ready = max(pm_ready, failed_until.get(ws.workstation_id, 0.0))
            src_ws = order_location.get(op.order.order_id)
            log_time = self._uncertain_time(
                self.factory.logistics_time(src_ws, ws.workstation_id), 0.30,
                self._scenario_rng(op.op_id, ws.workstation_id, 1),
            )
            agv_idx, agv_ready = min(enumerate(agv_available), key=lambda item: item[1])
            logistics_start = max(op.ready_time, agv_ready)
            logistics_finish = self._first_buffer_slot(logistics_start + log_time, buffer_intervals[ws.workstation_id], self.factory.buffer_capacity)
            blocking = max(0.0, logistics_finish - (logistics_start + log_time))
            setup = self.factory.setup_time(ws, op.template)
            processing = self._uncertain_time(
                self.factory.processing_time(op.template), 0.25,
                self._scenario_rng(op.op_id, ws.workstation_id, 2),
            )
            start = max(logistics_finish, pm_ready)
            finish = start + processing + setup
            waiting = max(0.0, start - logistics_finish)
            score = finish
            if method == "atc":
                score += max(0.0, finish - op.order.due_time)
            if method == "falang":
                score += 0.3 * setup + 0.15 * blocking + 0.06 * max(0.0, finish - op.order.due_time)
            candidate = _Candidate(score, finish, op, ws.workstation_id, slot_idx, agv_idx, logistics_start, logistics_finish, start, processing, setup, log_time, waiting, blocking)
            if best is None or (candidate.score, candidate.finish) < (best.score, best.finish):
                best = candidate
        assert best is not None
        return best

    def _commit(self, candidate, method, seed, load, workstations, agv_available, buffer_intervals, order_location) -> ScheduledOperation:
        ws = workstations[candidate.ws_id]
        agv_available[candidate.agv_idx] = candidate.logistics_finish
        ws.slot_available[candidate.slot_idx] = candidate.finish
        ws.busy_time += candidate.processing + candidate.setup
        if candidate.op.template.component and candidate.op.template.model:
            ws.last_model_by_component[candidate.op.template.component] = candidate.op.template.model
        if candidate.start > candidate.logistics_finish:
            buffer_intervals[candidate.ws_id].append((candidate.logistics_finish, candidate.start))
        order_location[candidate.op.order.order_id] = candidate.ws_id
        return ScheduledOperation(
            method=method,
            seed=seed,
            load=load,
            order_id=candidate.op.order.order_id,
            op_id=candidate.op.op_id,
            operation=candidate.op.template.name,
            workstation_id=candidate.ws_id,
            ready_time=candidate.op.ready_time,
            logistics_start=candidate.logistics_start,
            logistics_finish=candidate.logistics_finish,
            start=candidate.start,
            finish=candidate.finish,
            processing_time=candidate.processing,
            setup_time=candidate.setup,
            logistics_time=candidate.logistics,
            waiting_time=candidate.waiting,
            blocking_time=candidate.blocking,
        )

    def _select_recovery_op(
        self,
        recovery_ops: List[RecoveryOperation],
        method: str,
        workstations: Dict[int, Workstation],
        agv_available: List[float],
        buffer_intervals: Dict[int, List[Tuple[float, float]]],
        order_location: Dict[int, Optional[int]],
        failed_until: Dict[int, float],
        rng: random.Random,
    ) -> Optional[RecoveryOperation]:
        if not recovery_ops:
            return None

        recovery_wss = [
            ws for ws in workstations.values()
            if any(cap[1] == "100" for cap in ws.capabilities)
        ]
        all_wss = list(workstations.values())

        if method == "falang":
            return self._select_recovery_falang(
                recovery_ops, recovery_wss, all_wss,
                workstations, agv_available, buffer_intervals,
                order_location, failed_until,
            )
        elif method == "fcfs":
            return self._select_recovery_fcfs(
                recovery_ops, recovery_wss, all_wss,
                workstations, agv_available, buffer_intervals,
                order_location, failed_until,
            )
        elif method == "edd":
            return self._select_recovery_edd(
                recovery_ops, recovery_wss, all_wss,
                workstations, agv_available, buffer_intervals,
                order_location, failed_until,
            )
        elif method == "atc":
            return self._select_recovery_atc(
                recovery_ops, recovery_wss, all_wss,
                workstations, agv_available, buffer_intervals,
                order_location, failed_until,
            )
        elif method == "ga":
            return self._select_recovery_ga(
                recovery_ops, recovery_wss, all_wss,
                workstations, agv_available, buffer_intervals,
                order_location, failed_until, rng,
            )
        elif method == "jmedd":
            return self._select_recovery_jmedd(
                recovery_ops, recovery_wss, all_wss,
                workstations, agv_available, buffer_intervals,
                order_location, failed_until,
            )
        elif method == "jit":
            return self._select_recovery_jit(
                recovery_ops, recovery_wss, all_wss,
                workstations, agv_available, buffer_intervals,
                order_location, failed_until,
            )
        return None

    def _select_recovery_falang(
        self, recovery_ops, recovery_wss, all_wss, workstations,
        agv_available, buffer_intervals, order_location, failed_until,
    ) -> Optional[RecoveryOperation]:
        """Order recovery steps by their compiled contingency start times."""
        ordered = sorted(recovery_ops, key=lambda r: r.planned_start)
        return self._select_recovery_from_ordered(
            ordered, recovery_wss, all_wss, workstations,
            agv_available, buffer_intervals, order_location, failed_until,
        )

    def _select_recovery_fcfs(
        self, recovery_ops, recovery_wss, all_wss, workstations,
        agv_available, buffer_intervals, order_location, failed_until,
    ) -> Optional[RecoveryOperation]:
        ordered = sorted(recovery_ops, key=lambda r: (r.trigger_time, r.order_id, r.step_index))
        return self._select_recovery_from_ordered(
            ordered, recovery_wss, all_wss, workstations,
            agv_available, buffer_intervals, order_location, failed_until,
        )

    def _select_recovery_edd(
        self, recovery_ops, recovery_wss, all_wss, workstations,
        agv_available, buffer_intervals, order_location, failed_until,
    ) -> Optional[RecoveryOperation]:
        ordered = sorted(recovery_ops, key=lambda r: (r.order_due_time, r.trigger_time, r.step_index))
        return self._select_recovery_from_ordered(
            ordered, recovery_wss, all_wss, workstations,
            agv_available, buffer_intervals, order_location, failed_until,
        )

    def _select_recovery_atc(
        self, recovery_ops, recovery_wss, all_wss, workstations,
        agv_available, buffer_intervals, order_location, failed_until,
    ) -> Optional[RecoveryOperation]:
        def _atc_score(r: RecoveryOperation) -> float:
            p = max(1.0, r.processing_time)
            slack = r.order_due_time - r.trigger_time - r.processing_time
            return -math.exp(-max(slack, 0.0) / (2.0 * p)) / p
        ordered = sorted(recovery_ops, key=_atc_score)
        return self._select_recovery_from_ordered(
            ordered, recovery_wss, all_wss, workstations,
            agv_available, buffer_intervals, order_location, failed_until,
        )

    def _select_recovery_ga(
        self, recovery_ops, recovery_wss, all_wss, workstations,
        agv_available, buffer_intervals, order_location, failed_until, rng,
    ) -> Optional[RecoveryOperation]:
        if len(recovery_ops) <= 2:
            ordered = list(recovery_ops)
        else:
            pool = recovery_ops[:min(len(recovery_ops), 10)]
            population = []
            for _ in range(6):
                chrom = list(pool)
                rng.shuffle(chrom)
                population.append(chrom)
            for _ in range(6):
                population.sort(key=lambda chrom: self._recovery_sequence_score(chrom, recovery_wss))
                parent_a, parent_b = population[0], population[1]
                cut = rng.randrange(1, len(pool))
                child_prefix = parent_a[:cut]
                child = child_prefix + [r for r in parent_b if r not in child_prefix]
                if rng.random() < 0.25 and len(child) > 1:
                    i, j = rng.sample(range(len(child)), 2)
                    child[i], child[j] = child[j], child[i]
                population[-1] = child
            best = min(population, key=lambda chrom: self._recovery_sequence_score(chrom, recovery_wss))
            ordered = best + [r for r in recovery_ops if r not in best]
        return self._select_recovery_from_ordered(
            ordered, recovery_wss, all_wss, workstations,
            agv_available, buffer_intervals, order_location, failed_until,
        )

    def _select_recovery_jmedd(
        self, recovery_ops, recovery_wss, all_wss, workstations,
        agv_available, buffer_intervals, order_location, failed_until,
    ) -> Optional[RecoveryOperation]:
        """Prefer zero-setup recovery steps, then apply modified earliest-due-date priority."""
        zero_setup = []
        for r in recovery_ops:
            target_wss = recovery_wss if r.op_type == "Adjust" else all_wss
            for ws in target_wss:
                # Recovery steps have no component/model setup change.
                zero_setup.append(r)
                break

        # MEDD priority is max(due_time, trigger_time + processing_time).
        def _medd_key(r: RecoveryOperation):
            return max(r.order_due_time, r.trigger_time + r.processing_time), r.trigger_time, r.step_index

        if zero_setup:
            ordered = sorted(zero_setup, key=_medd_key)
        else:
            ordered = sorted(recovery_ops, key=_medd_key)

        return self._select_recovery_from_ordered(
            ordered, recovery_wss, all_wss, workstations,
            agv_available, buffer_intervals, order_location, failed_until,
        )

    def _select_recovery_jit(
        self, recovery_ops, recovery_wss, all_wss, workstations,
        agv_available, buffer_intervals, order_location, failed_until,
    ) -> Optional[RecoveryOperation]:
        """Rank recovery steps by squared deviation from their due-date target."""
        def _jit_key(r: RecoveryOperation):
            slack = r.order_due_time - r.processing_time - r.trigger_time
            return slack * slack, r.trigger_time, r.step_index
        ordered = sorted(recovery_ops, key=_jit_key)
        return self._select_recovery_from_ordered(
            ordered, recovery_wss, all_wss, workstations,
            agv_available, buffer_intervals, order_location, failed_until,
        )

    def _recovery_sequence_score(
        self, seq: List[RecoveryOperation], recovery_wss: List[Workstation],
    ) -> float:
        score = 0.0
        busy_until = 0.0
        for rank, r in enumerate(seq):
            earliest = min(min(ws.slot_available) for ws in recovery_wss) if recovery_wss else 0.0
            start = max(r.trigger_time, busy_until, earliest)
            score += 2.0 * max(0.0, start + rank - r.order_due_time) + 0.1 * rank * r.processing_time
            busy_until = start + r.processing_time
        return score

    def _select_recovery_from_ordered(
        self, ordered: List[RecoveryOperation],
        recovery_wss: List[Workstation], all_wss: List[Workstation],
        workstations, agv_available, buffer_intervals,
        order_location, failed_until,
    ) -> Optional[RecoveryOperation]:
        for r in ordered:
            if r.assigned_ws_id is not None and r.assigned_ws_id in workstations:
                target_ws = workstations[r.assigned_ws_id]
                if self._can_execute_recovery(r, target_ws, failed_until):
                    return r

            target_wss = recovery_wss if r.op_type == "Adjust" else all_wss
            for ws in target_wss:
                if self._can_execute_recovery(r, ws, failed_until):
                    r.assigned_ws_id = ws.workstation_id
                    return r
        return None

    def _can_execute_recovery(
        self, r: RecoveryOperation, ws: Workstation, failed_until: Dict[int, float],
    ) -> bool:
        now = getattr(self, '_current_time', r.trigger_time)
        if failed_until.get(ws.workstation_id, 0.0) > now + 1e-9:
            return False
        slot_ready = min(ws.slot_available) if ws.slot_available else 0.0
        if slot_ready > now + 1e-9:
            return False
        return True

    def _commit_recovery(
        self,
        recovery: RecoveryOperation,
        method: str,
        seed: int,
        load: str,
        workstations: Dict[int, Workstation],
        agv_available: List[float],
        buffer_intervals: Dict[int, List[Tuple[float, float]]],
        order_location: Dict[int, Optional[int]],
    ) -> ScheduledOperation:
        ws = workstations[recovery.assigned_ws_id] if recovery.assigned_ws_id is not None else list(workstations.values())[0]

        slot_idx, slot_ready = min(enumerate(ws.slot_available), key=lambda x: x[1])
        start_time = max(recovery.trigger_time, slot_ready, recovery.planned_start)
        finish_time = start_time + recovery.processing_time

        logistics_start = start_time
        logistics_finish = start_time
        logistics_time = 0.0
        blocking_time = 0.0
        waiting_time = max(0.0, start_time - recovery.trigger_time)
        setup_time = 0.0

        if recovery.op_type == "Move":
            source_ws = order_location.get(recovery.order_id)
            logistics_time = self._uncertain_time(
                self.factory.logistics_time(source_ws, ws.workstation_id), 0.30,
                self._scenario_rng(recovery.recovery_id, ws.workstation_id, 1),
            )
            agv_idx, agv_ready = min(enumerate(agv_available), key=lambda x: x[1])
            logistics_start = max(recovery.trigger_time, agv_ready)
            logistics_finish = logistics_start + logistics_time
            logistics_finish = self._first_buffer_slot(
                logistics_finish, buffer_intervals.get(ws.workstation_id, []),
                self.factory.buffer_capacity,
            )
            blocking_time = max(0.0, logistics_finish - (logistics_start + logistics_time))
            agv_available[agv_idx] = logistics_finish
            start_time = max(logistics_finish, slot_ready)
            finish_time = start_time + recovery.processing_time
            waiting_time = max(0.0, start_time - logistics_finish)

        ws.slot_available[slot_idx] = finish_time
        ws.busy_time += recovery.processing_time

        if recovery.op_type == "Move":
            order_location[recovery.order_id] = ws.workstation_id

        if start_time > logistics_finish and recovery.op_type == "Move":
            buffer_intervals.setdefault(ws.workstation_id, []).append(
                (logistics_finish, start_time)
            )

        return ScheduledOperation(
            method=method,
            seed=seed,
            load=load,
            order_id=recovery.order_id,
            op_id=-(recovery.recovery_id + 1),  # Negative operation IDs identify recovery steps.
            operation=f"Recovery_{recovery.exc_type}_{recovery.op_type}",
            workstation_id=ws.workstation_id,
            ready_time=recovery.trigger_time,
            logistics_start=logistics_start,
            logistics_finish=logistics_finish,
            start=start_time,
            finish=finish_time,
            processing_time=recovery.processing_time,
            setup_time=setup_time,
            logistics_time=logistics_time,
            waiting_time=waiting_time,
            blocking_time=blocking_time,
            is_recovery=True,
            exc_type=recovery.exc_type,
        )

    def _first_buffer_slot(self, t: float, intervals: List[Tuple[float, float]], capacity: int) -> float:
        current = t
        while True:
            overlap_ends = [end for start, end in intervals if start <= current < end]
            if len(overlap_ends) < capacity:
                return current
            current = min(overlap_ends)

    def _uncertain_time(self, base: float, cv: float, rng: random.Random) -> float:
        if base <= 0:
            return 0.0
        return rng.uniform((1.0 - cv) * base, (1.0 + cv) * base)

    def _scenario_rng(self, op_id: int, workstation_id: int, stream: int) -> random.Random:
        """Use the same physical-scenario random stream across methods."""
        seed = (
            (self._scenario_seed + 1) * 1_000_003
            + (op_id + 1) * 10_007
            + (workstation_id + 2) * 101
            + stream * 17
        )
        return random.Random(seed)

    def _recovery_step_time(self, op_type: str, params: Optional[str]) -> float:
        if op_type == "Adjust":
            rp = "00001"
            if params:
                rp_map = {
                    "MaterialMissing_Recovery": "00001",
                    "MachineFailure_Reset": "00010",
                    "OperatorUnavailable_Reassign": "00011",
                    "QualityFault_Rework": "00100",
                }
                rp = rp_map.get(params, "00001")
            t = self.factory.tm.get_expected_time("000001", "100", rp)
            return t if t else 10.0
        elif op_type == "Move":
            return self.factory.tm.get_default_move_time()
        elif op_type == "Load":
            return self.factory.tm.get_default_load_time()
        elif op_type == "Store":
            return self.factory.tm.get_default_store_time()
        return 5.0

    def _generate_recovery_ops(
        self,
        exc_type: str,
        trigger_op: OperationInstance,
        trigger_finish: float,
        method: str,
        jit_result: Optional[JITCompileResult],
        rng: random.Random,
    ) -> List[RecoveryOperation]:
        recovery_ops: List[RecoveryOperation] = []

        if method == "falang" and jit_result is not None:
            catch_steps = jit_result.catch_plans.get((trigger_op.op_id, exc_type), [])
            if catch_steps:
                for i, step in enumerate(catch_steps):
                    op_type = step.get("op_type", "Adjust")
                    params = step.get("params")
                    processing_time = step.get("contingency_finish", 0.0) - step.get("contingency_start", 0.0)
                    if processing_time <= 0:
                        processing_time = self._recovery_step_time(op_type, params)

                    recovery_ops.append(RecoveryOperation(
                        recovery_id=self._recovery_id_counter,
                        order_id=trigger_op.order.order_id,
                        exc_type=exc_type,
                        trigger_time=trigger_finish,
                        trigger_op_id=trigger_op.op_id,
                        trigger_ws_id=trigger_op.preferred_ws or -1,
                        step_index=i,
                        op_type=op_type,
                        params=params,
                        assigned_ws_id=step.get("assigned_ws"),
                        assigned_device=step.get("assigned_device"),
                        processing_time=processing_time,
                        planned_start=float(step.get("contingency_start", trigger_finish)),
                        planned_finish=float(step.get("contingency_finish", trigger_finish + processing_time)),
                        state=step.get("contingency_state", "scheduled"),
                        order_due_time=trigger_op.order.due_time,
                    ))
                    self._recovery_id_counter += 1
                return recovery_ops

        adjust_params = {
            "MaterialMissing": "MaterialMissing_Recovery",
            "OperatorUnavailable": "OperatorUnavailable_Reassign",
            "QualityFault": "QualityFault_Rework",
        }.get(exc_type, f"{exc_type}_Recovery")

        adjust_time = self._recovery_step_time("Adjust", adjust_params)
        move_time = self._recovery_step_time("Move", "Matrix_Buffer_Move")
        penalties = (getattr(self, "_active_method_config", {}) or {}).get("runtime_penalties", {}) or {}
        recovery_delay = max(0.0, float(penalties.get("recovery_delay", 0.0) or 0.0))

        recovery_ops.append(RecoveryOperation(
            recovery_id=self._recovery_id_counter,
            order_id=trigger_op.order.order_id,
            exc_type=exc_type,
            trigger_time=trigger_finish,
            trigger_op_id=trigger_op.op_id,
            trigger_ws_id=-1,
            step_index=0,
            op_type="Adjust",
            params=adjust_params,
            assigned_ws_id=None,
            assigned_device=None,
            processing_time=adjust_time,
            planned_start=trigger_finish + recovery_delay,
            planned_finish=trigger_finish + recovery_delay + adjust_time,
            state="scheduled",
            order_due_time=trigger_op.order.due_time,
        ))
        self._recovery_id_counter += 1

        recovery_ops.append(RecoveryOperation(
            recovery_id=self._recovery_id_counter,
            order_id=trigger_op.order.order_id,
            exc_type=exc_type,
            trigger_time=trigger_finish,
            trigger_op_id=trigger_op.op_id,
            trigger_ws_id=-1,
            step_index=1,
            op_type="Move",
            params="Matrix_Buffer_Move",
            assigned_ws_id=None,
            assigned_device=None,
            processing_time=move_time,
            planned_start=trigger_finish + recovery_delay + adjust_time,
            planned_finish=trigger_finish + recovery_delay + adjust_time + move_time,
            state="scheduled",
            order_due_time=trigger_op.order.due_time,
        ))
        self._recovery_id_counter += 1

        return recovery_ops

    def _exception_type(self, op: OperationInstance, rng: random.Random) -> Optional[str]:
        rng = self._scenario_rng(op.op_id, -1, 4)
        if op.template.stage == "testing":
            prob = QUALITY_GRADES[op.order.quality_grade]["quality_fault_probability"]
            if rng.random() < prob:
                return "QualityFault"
        elif op.template.name in ("CPU_Install", "Memory_Install", "Storage_Install", "GPU_Install"):
            roll = rng.random()
            if roll < 0.008:
                return "MachineFailure"
            if roll < 0.018:
                return "MaterialMissing"
            if roll < 0.025:
                return "OperatorUnavailable"
        elif rng.random() < 0.01:
            return "OperatorUnavailable"
        return None

    def _metrics(self, method, seed, load, orders, completion, scheduled, workstations, replanning_events, triggered_exceptions) -> SimulationMetrics:
        start = min(order.arrival_time for order in orders) if orders else 0.0
        finish = max(completion.values()) if completion else start
        horizon = max(1.0, finish - start)
        tardiness = [max(0.0, completion[order.order_id] - order.due_time) for order in orders]
        on_time = [completion[order.order_id] <= order.due_time for order in orders]
        wip_area = sum(max(0.0, completion[order.order_id] - order.arrival_time) for order in orders)
        resource_capacity_time = sum(max(1, ws.slots) * horizon for ws in workstations.values())
        resource_busy = sum(ws.busy_time for ws in workstations.values())
        recovery_ops = [s for s in scheduled if s.is_recovery]
        recovery_count = len(recovery_ops)
        recovery_total_time = sum(s.processing_time for s in recovery_ops)
        flow_times = [
            max(0.0, completion[order.order_id] - order.arrival_time)
            for order in orders
        ]
        mof = sum(flow_times) / max(1, len(flow_times))
        tst = sum(item.setup_time for item in scheduled)
        return SimulationMetrics(
            method=method,
            seed=seed,
            load=load,
            orders=len(orders),
            makespan=horizon,
            mean_tardiness=sum(tardiness) / max(1, len(tardiness)),
            on_time_delivery_rate=sum(1 for ok in on_time if ok) / max(1, len(on_time)),
            resource_utilization=resource_busy / max(1.0, resource_capacity_time),
            average_wip=wip_area / horizon,
            blocking_time=sum(item.blocking_time for item in scheduled),
            waiting_time=sum(item.waiting_time for item in scheduled),
            replanning_events=replanning_events,
            triggered_exceptions=triggered_exceptions,
            recovery_operations=recovery_count,
            recovery_time=recovery_total_time,
            mof=mof,
            tst=tst,
        )
