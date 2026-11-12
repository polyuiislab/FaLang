"""Compile pending operations using the current factory state."""
from __future__ import annotations

from dataclasses import dataclass, field
import time
from typing import Dict, List, Optional, Tuple, TYPE_CHECKING

from case_study.order_generator import PCOrder, QUALITY_GRADES
from compiler.ir import OpInstr, BasicBlock, ControlFlowGraph

if TYPE_CHECKING:
    from case_study.simulation import OperationTemplate, OperationInstance, PCFactory


@dataclass
class RuntimeState:
    """Factory-state snapshot; times are absolute simulation minutes."""
    current_time: float = 0.0

    device_busy_until: Dict[str, float] = field(default_factory=dict)

    # None denotes warehouse storage; an integer identifies the current workstation.
    order_location: Dict[int, Optional[int]] = field(default_factory=dict)

    completed_seqs: Dict[int, int] = field(default_factory=dict)

    # Order ID to (executing operation, expected finish time).
    executing_ops: Dict[int, Tuple[OperationInstance, float]] = field(default_factory=dict)

    buffer_occupancy: Dict[int, int] = field(default_factory=dict)

    # Device name to absolute repair-completion time.
    failed_devices: Dict[str, float] = field(default_factory=dict)

    last_model_by_component: Dict[int, Dict[str, str]] = field(default_factory=dict)

    device_last_params: Dict[str, str] = field(default_factory=dict)

    # PALU ID to absolute availability times for its instances.
    palu_busy_until: Dict[int, List[float]] = field(default_factory=dict)


@dataclass
class JITCompileResult:
    # Operation ID to (workstation, planned start, planned finish).
    op_plan: Dict[int, Tuple[Optional[int], float, float]] = field(default_factory=dict)

    artifacts: Dict[str, any] = field(default_factory=dict)

    # Compilation time in milliseconds.
    compile_time_ms: float = 0.0

    makespan: float = 0.0

    # (Trigger operation, exception type) to recovery-step dictionaries.
    catch_plans: Dict[Tuple[int, str], List[Dict[str, any]]] = field(default_factory=dict)


ASSEMBLY_STEPS = [
    ("Base_Assembly", "Motherboard"),
    ("CPU_Install", "CPU"),
    ("Memory_Install", "RAM"),
    ("Storage_Install", "Storage"),
    ("GPU_Install", "GPU"),
    ("Cooling_Install", "Cooling"),
    ("Power_Install", None),
    ("Case_Assembly", None),
]


class StateAwareJITCompiler:
    """Compile pending orders and extract workstation assignments and planned times."""

    def __init__(self, factory: PCFactory):
        self.factory = factory
        self._cache: Dict[int, JITCompileResult] = {}
        from core.compiler_proxy import CompilerProxy
        self._proxy = CompilerProxy()

    def compile_with_state(
        self,
        runtime_state: RuntimeState,
        orders: List[PCOrder],
        op_chains: Dict[int, List[OperationInstance]],
        options: Optional[Dict[str, any]] = None,
    ) -> JITCompileResult:
        """Compile pending operations, reusing cached results for an equivalent runtime state."""
        t0 = time.perf_counter()
        options = options or {}

        pending_ops = self._collect_pending_ops(runtime_state, orders, op_chains)

        if not pending_ops:
            compile_time_ms = (time.perf_counter() - t0) * 1000.0
            return JITCompileResult(op_plan={}, compile_time_ms=compile_time_ms)

        state_hash = self._compute_state_hash(runtime_state, pending_ops, options)
        if state_hash in self._cache:
            cached = self._cache[state_hash]
            return cached

        source_code = self._build_unified_source(
            runtime_state, orders, op_chains, pending_ops,
        )

        schedule_state_override = self._build_schedule_state_override(runtime_state)
        device_last_model_override = dict(runtime_state.device_last_params)

        # Include exception paths so runtime handling can activate compiled recovery plans.
        result = self._proxy.compile_source_lightweight(
            source_code,
            schedule_state_override=schedule_state_override,
            device_last_model_override=device_last_model_override,
            allocation_mode=str(options.get("allocation_mode", "pbqp")),
            enable_try_catch=bool(options.get("enable_try_catch", True)),
            skip_pre_ra=bool(options.get("skip_pre_ra", False)),
        )

        compile_time_ms = (time.perf_counter() - t0) * 1000.0

        if not result.get("success"):
            return JITCompileResult(op_plan={}, compile_time_ms=compile_time_ms)

        artifacts = result.get("artifacts", {})
        cfg = artifacts.get("cfg")
        schedule_result = artifacts.get("schedule_result")
        alloc_map = artifacts.get("alloc_map", {})

        op_plan = self._extract_plan(cfg, schedule_result, alloc_map, pending_ops, runtime_state)

        makespan = getattr(schedule_result, "global_makespan", 0.0) if schedule_result else 0.0

        catch_plans: Dict[Tuple[int, str], List[Dict[str, any]]] = {}
        exception_table = artifacts.get("exception_table")
        catch_scenarios = artifacts.get("catch_contingency")
        if exception_table and catch_scenarios and cfg is not None:
            catch_plans = self._build_catch_plans(
                exception_table, catch_scenarios, cfg, pending_ops,
            )

        jit_result = JITCompileResult(
            op_plan=op_plan,
            artifacts=artifacts,
            compile_time_ms=compile_time_ms,
            makespan=makespan,
            catch_plans=catch_plans,
        )
        self._cache[state_hash] = jit_result
        return jit_result

    def _compute_state_hash(
        self,
        runtime_state: RuntimeState,
        pending_ops: Dict[int, List],
        options: Optional[Dict[str, any]] = None,
    ) -> int:
        key_parts = [
            int(runtime_state.current_time * 10),  # Quantize time to tenths of a minute for cache keys.
            tuple(sorted((d, int(t * 10)) for d, t in runtime_state.device_busy_until.items() if t > runtime_state.current_time + 0.5)),
            tuple(sorted(runtime_state.completed_seqs.items())),
            tuple(sorted(runtime_state.order_location.items())),
            tuple(sorted(runtime_state.failed_devices.items())),
            tuple(
                sorted(
                    (
                        oid,
                        tuple(
                            (
                                op.op_id,
                                op.seq,
                                op.template.name,
                                op.template.component,
                                op.template.model,
                            )
                            for op in ops
                        ),
                    )
                    for oid, ops in pending_ops.items()
                )
            ),
            tuple(sorted((options or {}).items())),
        ]
        try:
            return hash(key_parts)
        except TypeError:
            h = 0
            for part in key_parts:
                try:
                    h ^= hash(part)
                except TypeError:
                    h ^= hash(str(part))
            return h

    def _collect_pending_ops(
        self,
        runtime_state: RuntimeState,
        orders: List[PCOrder],
        op_chains: Dict[int, List[OperationInstance]],
    ) -> Dict[int, List[OperationInstance]]:
        """Collect unfinished operations while retaining nonpreemptive execution constraints."""
        pending: Dict[int, List[OperationInstance]] = {}

        for order in orders:
            oid = order.order_id
            if oid not in runtime_state.completed_seqs and order.arrival_time > runtime_state.current_time + 1e-9:
                continue
            chain = op_chains.get(oid, [])
            if not chain:
                continue

            completed_seq = runtime_state.completed_seqs.get(oid, -1)

            ops = []
            for op in chain:
                if op.seq > completed_seq:
                    ops.append(op)

            if ops:
                pending[oid] = ops

        return pending

    def _build_schedule_state_override(self, runtime_state: RuntimeState) -> Dict:
        """Convert absolute resource availability times to delays from the current time."""
        override: Dict[str, any] = {}

        # The scheduler uses remaining delays, not absolute simulation times.
        palu_busy_remaining: Dict[int, List[float]] = {}
        for pid, busy_list in runtime_state.palu_busy_until.items():
            remaining = [
                max(0.0, float(t) - runtime_state.current_time)
                for t in busy_list
            ]
            palu_busy_remaining[int(pid)] = remaining

        for ws_id, repair_time in runtime_state.failed_devices.items():
            pass

        override["palu_busy_until"] = palu_busy_remaining

        material_locations: Dict[str, Tuple[Optional[str], float]] = {}
        for oid, ws_id in runtime_state.order_location.items():
            mat_name = f"pc_{oid}"
            if ws_id is not None:
                material_locations[mat_name] = ("register", runtime_state.current_time)
            else:
                material_locations[mat_name] = ("memory", runtime_state.current_time)
        override["material_locations"] = material_locations

        buffer_occupancy: Dict[str, set] = {}
        for oid, ws_id in runtime_state.order_location.items():
            if ws_id is not None:
                key = "register"
                buffer_occupancy.setdefault(key, set()).add(f"pc_{oid}")
        override["buffer_occupancy"] = buffer_occupancy

        override["palu_last_model"] = {}
        override["device_last_model"] = dict(runtime_state.device_last_params)

        return override

    def _build_unified_source(
        self,
        runtime_state: RuntimeState,
        orders: List[PCOrder],
        op_chains: Dict[int, List[OperationInstance]],
        pending_ops: Dict[int, List[OperationInstance]],
        enable_try_catch: bool = True,
    ) -> str:
        """Inline pending operations into one Main task with distinct material names per order."""
        lines: List[str] = []

        lines.append("Resource Assembly_PM: Device<Process, Adjust>;")
        lines.append("Resource Test_PM: Device<Inspect, Adjust>;")
        lines.append("Resource Packaging_PM: Device<Process, Inspect>;")
        lines.append("Resource Recovery_PM: Device<Adjust>;")
        lines.append("")
        lines.append("Storage Warehouse: Memory<>;")
        pending_orders = [order for order in orders if order.order_id in pending_ops]
        lines.append(f"Material RawPC[{len(pending_orders)}];")
        lines.append("")

        lines.append("// Entry point for pending orders.")
        main_params = ", ".join(
            f"Material raw_{order.order_id:04d}" for order in pending_orders
        )
        lines.append(f"Task Main({main_params}) {{")

        for order in pending_orders:
            ops = pending_ops.get(order.order_id, [])
            if not ops:
                continue
            material = f"raw_{order.order_id:04d}"
            lines.append(f"    // Order {order.order_id:04d}: {order.quality_grade} grade")

            assembly_ops = [op for op in ops if op.template.stage == "assembly"]
            testing_ops = [op for op in ops if op.template.stage == "testing"]
            packaging_ops = [op for op in ops if op.template.stage == "packaging"]
            rest_ops = testing_ops + packaging_ops

            if assembly_ops:
                if assembly_ops[0].seq == 0 or runtime_state.completed_seqs.get(order.order_id, -1) < 0:
                    lines.append(f'    Load({material}, "Warehouse_To_PM_Buffer");')
                elif runtime_state.order_location.get(order.order_id) is not None:
                    lines.append(f'    Move({material}, "Matrix_Buffer_Move");')
                for op in assembly_ops:
                    params = self._build_op_params(op)
                    if params:
                        lines.append(f'    Process(Assembly_PM, {material}, "{params}");')
                    else:
                        lines.append(f'    Process(Assembly_PM, {material}, "{op.template.name}");')

            for op in rest_ops:
                if op.template.stage == "testing":
                    lines.append(f'    Inspect(Test_PM, {material}, "{op.template.name}");')
                elif op.template.name == "Accessory_Check":
                    lines.append(f'    Inspect(Packaging_PM, {material}, "{op.template.name}");')
                elif op.template.name == "Packaging":
                    lines.append(f'    Process(Packaging_PM, {material}, "{op.template.name}");')

            if rest_ops:
                lines.append(f'    Store({material}, "PM_Buffer_To_Warehouse");')

            lines.append("")

        lines.append(f"    return raw_{pending_orders[0].order_id:04d};")
        lines.append("}")

        return "\n".join(lines) + "\n"

    def _generate_order_task(
        self,
        order: PCOrder,
        pending_ops: List[OperationInstance],
        runtime_state: RuntimeState,
        enable_try_catch: bool = True,
    ) -> List[str]:
        """Deprecated: pending operations are inlined by _build_unified_source."""
        return [f"    // Order {order.order_id:04d} (inlined in Main)"]

    def _build_op_params(self, op: OperationInstance) -> Optional[str]:
        template = op.template
        if template.component and template.model:
            return f"{template.name};component={template.component};model={template.model}"
        return template.name if template.name else None

    def _extract_plan(
        self,
        cfg: Optional[ControlFlowGraph],
        schedule_result,
        alloc_map: Dict[str, any],
        pending_ops: Dict[int, List[OperationInstance]],
        runtime_state: RuntimeState,
    ) -> Dict[int, Tuple[Optional[int], float, float]]:
        """Map normal-path compiled instructions to operation assignments and scheduled times."""
        op_plan: Dict[int, Tuple[Optional[int], float, float]] = {}

        if cfg is None:
            return op_plan

        normal_instrs: List[OpInstr] = []
        for block in cfg.blocks:
            name = getattr(block, "name", "") or ""
            if name.startswith("Catch_") or name.startswith("Compensate"):
                continue
            for instr in block.instructions:
                if isinstance(instr, OpInstr):
                    if instr.op_type in ("Process", "Inspect", "Adjust"):
                        if not getattr(instr, "initial_busy_reserve", False):
                            normal_instrs.append(instr)

        for order_id, ops in pending_ops.items():
            order = None
            for op in ops:
                order = op.order
                break
            if order is None:
                continue

            cursor = 0
            for op in ops:
                matched = self._match_instr(op, normal_instrs, cursor)
                if matched is not None:
                    instr, new_cursor = matched
                    cursor = new_cursor
                    ws_id = getattr(instr, "assigned_workstation_id", None)
                    start = float(getattr(instr, "scheduled_start", 0.0) or 0.0)
                    finish = float(getattr(instr, "scheduled_finish", 0.0) or 0.0)
                    op_plan[op.op_id] = (ws_id, start, finish)
                else:
                    op_plan[op.op_id] = (None, 0.0, 0.0)

        return op_plan

    def _match_instr(
        self,
        op: OperationInstance,
        instrs: List[OpInstr],
        cursor: int,
    ) -> Optional[Tuple[OpInstr, int]]:
        template = op.template
        for idx in range(cursor, len(instrs)):
            instr = instrs[idx]
            if self._instr_matches_template(instr, template, op.order):
                return instr, idx + 1
        return None

    def _instr_matches_template(
        self,
        instr: OpInstr,
        template: OperationTemplate,
        order: PCOrder,
    ) -> bool:
        if instr.op_type not in ("Process", "Inspect"):
            return False

        # Match the order-specific material identity before matching operation parameters.
        order_material = f"raw_{order.order_id:04d}"
        if not any(
            arg == order_material or arg.startswith(f"{order_material}_")
            for arg in (instr.args or [])
            if isinstance(arg, str)
        ):
            return False

        instr_rp = instr.rp
        if instr_rp is None:
            return False

        params = getattr(instr, "params", None) or ""
        template_name = template.name

        if template_name in params:
            if template.component:
                comp_str = f"component={template.component}"
                if comp_str in params:
                    model_str = f"model={template.model}"
                    if template.model and model_str in params:
                        return True
                    elif not template.model:
                        return True
                else:
                    return False
            return True

        return False

    def _build_catch_plans(
        self,
        exception_table: List,
        catch_scenarios: List,
        cfg,
        pending_ops: Dict[int, List],
    ) -> Dict[Tuple[int, str], List[Dict[str, any]]]:
        """Map each triggering operation and exception type to its compiled recovery steps."""
        from compiler.ir import OpInstr

        catch_plans: Dict[Tuple[int, str], List[Dict[str, any]]] = {}

        normal_instrs: List[OpInstr] = []
        for block in cfg.blocks:
            name = getattr(block, "name", "") or ""
            if name.startswith("Catch_") or name.startswith("Compensate"):
                continue
            for instr in block.instructions:
                if isinstance(instr, OpInstr):
                    if instr.op_type in ("Process", "Inspect", "Adjust"):
                        if not getattr(instr, "initial_busy_reserve", False):
                            normal_instrs.append(instr)

        all_ops: List = []
        for order_id, ops in pending_ops.items():
            for op in ops:
                all_ops.append(op)
        all_ops.sort(key=lambda o: (o.order.order_id, o.seq))

        instr_to_opid: Dict[int, int] = {}
        cursor = 0
        for op in all_ops:
            matched = self._match_instr(op, normal_instrs, cursor)
            if matched is not None:
                instr, new_cursor = matched
                cursor = new_cursor
                instr_to_opid[id(instr)] = op.op_id

        for entry in exception_table:
            trigger = entry.trigger_instr
            op_id = instr_to_opid.get(id(trigger))
            if op_id is None:
                continue
            exc_type = entry.exception_type

            steps: List[Dict[str, any]] = []
            catch_block = entry.catch_block
            for instr in catch_block.instructions:
                if not isinstance(instr, OpInstr):
                    continue
                step = {
                    "op_type": instr.op_type,
                    "params": instr.params,
                    "assigned_device": getattr(instr, "assigned_device_id", None),
                    "assigned_ws": getattr(instr, "assigned_workstation_id", None),
                    "contingency_start": float(getattr(instr, "contingency_start", 0.0) or 0.0),
                    "contingency_finish": float(getattr(instr, "contingency_finish", 0.0) or 0.0),
                    "contingency_state": getattr(instr, "contingency_state", "scheduled") or "scheduled",
                }
                steps.append(step)

            if steps:
                catch_plans[(op_id, exc_type)] = steps

        return catch_plans
