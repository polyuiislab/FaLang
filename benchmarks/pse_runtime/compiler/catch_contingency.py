from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from compiler.exception_table import ExceptionTableBuilder, ExceptionTableEntry
from compiler.ir import BasicBlock, ControlFlowGraph, OpInstr
from compiler.scheduler import ScheduleResult


@dataclass
class CatchScenarioResult:
    scenario_id: str
    trigger_block_id: int
    catch_block_id: int
    trigger_instr: str
    t_exc: float
    deadline: float
    state: str
    message: str


class CatchContingencyPlanner:
    """
    补偿延迟模式：
    - catch 不预留整个异常风险窗口；
    - 异常发生点采用触发指令 scheduled_finish；
    - 已启动的正常操作保持不可抢占，catch 在 deadline 窗口内见缝插针；
    - 若窗口不足，生成 deferred / late 计划，不让编译结果出现未定义执行。
    """

    def __init__(self, target_model):
        self.tm = target_model
        self._preg_to_ws: Dict[int, int] = {}
        self._ws_to_pregs: Dict[int, List[int]] = {}
        self._ws_to_palus: Dict[int, List[int]] = {}
        self._palu_caps: Dict[int, List[Tuple[str, str, str]]] = {}
        self._build_target_maps()

    def plan(
        self,
        cfg: ControlFlowGraph,
        schedule_result: ScheduleResult,
        allocation: Dict[str, Any],
    ) -> List[CatchScenarioResult]:
        scenarios: List[CatchScenarioResult] = []
        # Paper 1 不做指令调度，调用方显式传 schedule_result=None，此时没有设备时间线可插入。
        # catch 计划退化为预编译语义：异常一发生，恢复动作按各自耗时依次执行。
        # 只要调用方给出了 ScheduleResult（哪怕是空的），就仍按设备空档插入的旧语义处理。
        self.precompiled = schedule_result is None
        device_timeline = self._copy_device_timeline(schedule_result)
        fault_release = self.tm.get_fault_release_latency() if self.tm else 0.0
        exception_table = list(getattr(cfg, "exception_table", []) or [])
        if not exception_table:
            exception_table = ExceptionTableBuilder().build(cfg)

        for entry in exception_table:
            instr = entry.trigger_instr
            t_exc = self._trigger_time(instr)
            trigger_dev = entry.landing_pad.trigger_device or self._explicit_device(cfg, instr)
            if trigger_dev and not self.precompiled:
                device_timeline.setdefault(trigger_dev, []).append(
                    (t_exc, t_exc + fault_release, f"fault_release:{instr}")
                )
            sid = entry.entry_id
            deadline = self._deadline_for(cfg, t_exc)
            state = self._plan_catch_block(
                cfg=cfg,
                entry=entry,
                scenario_id=sid,
                t_exc=t_exc,
                deadline=deadline,
                device_timeline=device_timeline,
                allocation=allocation,
            )
            scenarios.append(
                CatchScenarioResult(
                    scenario_id=sid,
                    trigger_block_id=entry.trigger_block_id,
                    catch_block_id=entry.catch_block.id,
                    trigger_instr=str(instr),
                    t_exc=t_exc,
                    deadline=deadline,
                    state=state,
                    message=self._message(sid, state, t_exc, deadline),
                )
            )
        cfg.catch_contingency = scenarios
        return scenarios

    def _plan_catch_block(
        self,
        cfg: ControlFlowGraph,
        entry: ExceptionTableEntry,
        scenario_id: str,
        t_exc: float,
        deadline: float,
        device_timeline: Dict[str, List[Tuple[float, float, str]]],
        allocation: Dict[str, Any],
    ) -> str:
        cursor = t_exc
        state = "scheduled"
        current_faulted = list(entry.landing_pad.faulted_materials or [])
        for instr in entry.catch_block.instructions:
            if not isinstance(instr, OpInstr):
                continue
            self._bind_faulted_material(instr, current_faulted)
            latency = self._latency(instr)
            bind = self._select_binding(cfg, instr, allocation)
            device = bind.get("device")
            earliest = cursor
            if self.precompiled:
                # 预编译语义：恢复动作紧跟异常发生时刻依次执行，不去找设备空档
                start = cursor
                finish = cursor + latency
                instr_state = "scheduled" if finish <= deadline else (
                    "deferred" if cursor <= deadline else "late"
                )
            else:
                start, finish, instr_state = self._place_in_gap(
                    device_timeline=device_timeline,
                    device=device,
                    earliest=earliest,
                    latest=deadline,
                    latency=latency,
                    label=f"{scenario_id}:{instr}",
                )
            if instr_state != "scheduled":
                state = "late" if instr_state == "late" else "deferred"

            instr.assigned_device_id = device
            instr.assigned_workstation_id = bind.get("workstation")
            instr.assigned_preg = bind.get("preg")
            instr.assigned_palu_id = bind.get("palu")
            instr.contingency_scenario_id = scenario_id
            instr.contingency_start = start
            instr.contingency_finish = finish
            instr.contingency_deadline = deadline
            instr.contingency_deadline_source = self._deadline_source(cfg)
            instr.contingency_state = instr_state
            next_materials = self._output_materials(instr)
            if next_materials:
                current_faulted = next_materials
            cursor = finish
        return state

    def _bind_faulted_material(self, instr: OpInstr, current_faulted: List[str]) -> None:
        if not current_faulted:
            return
        idxs = list(self._material_arg_indices(instr.op_type, len(instr.args)))
        if not idxs:
            return
        first_idx = idxs[0]
        if first_idx < len(instr.args):
            instr.args[first_idx] = current_faulted[0]

    def _output_materials(self, instr: OpInstr) -> List[str]:
        outs = []
        if instr.target:
            outs.append(instr.target)
        outs.extend(instr.implicit_targets or [])
        return [m for m in outs if isinstance(m, str) and m]

    def _place_in_gap(
        self,
        device_timeline: Dict[str, List[Tuple[float, float, str]]],
        device: Optional[str],
        earliest: float,
        latest: float,
        latency: float,
        label: str,
    ) -> Tuple[float, float, str]:
        if not device:
            finish = earliest + latency
            return earliest, finish, "scheduled" if finish <= latest else "deferred"

        busy = sorted(device_timeline.get(device, []), key=lambda x: (x[0], x[1]))
        cursor = earliest
        for st, fin, _label in busy:
            if fin <= cursor:
                continue
            if cursor + latency <= min(st, latest):
                finish = cursor + latency
                device_timeline.setdefault(device, []).append((cursor, finish, label))
                return cursor, finish, "scheduled"
            cursor = max(cursor, fin)

        finish = cursor + latency
        device_timeline.setdefault(device, []).append((cursor, finish, label))
        if cursor <= latest and finish <= latest:
            return cursor, finish, "scheduled"
        if cursor <= latest:
            return cursor, finish, "deferred"
        return cursor, finish, "late"

    def _select_binding(
        self,
        cfg: ControlFlowGraph,
        instr: OpInstr,
        allocation: Dict[str, Any],
    ) -> Dict[str, Any]:
        key = self.tm.optype_to_instruction(instr.op_type) if self.tm else None
        cap = (key.opcode, key.func, instr.rp) if key and instr.rp else None

        preg = self._dominant_preg(instr, allocation)
        ws = self._preg_to_ws.get(preg) if isinstance(preg, int) else None

        if cap:
            cap_ws = self._workstations_for_cap(cap)
            if ws not in cap_ws:
                ws = cap_ws[0] if cap_ws else ws
                preg = self._first_preg_for_ws(ws)

        palu = self._first_palu_for_cap(ws, cap)
        return {
            "device": self._explicit_device(cfg, instr) or self._synthetic_device(ws, palu),
            "workstation": ws,
            "preg": preg,
            "palu": palu,
        }

    def _deadline_for(self, cfg: ControlFlowGraph, t_exc: float) -> float:
        due_time = getattr(cfg, "order_due_time_value", None)
        if due_time is not None:
            try:
                return float(due_time)
            except Exception:
                pass
        if getattr(cfg, "order_due_time_param", None):
            return float("inf")
        default_deadline = self.tm.get_default_catch_deadline() if self.tm else 20.0
        return t_exc + default_deadline

    def _deadline_source(self, cfg: ControlFlowGraph) -> str:
        if getattr(cfg, "order_due_time_value", None) is not None:
            return "program:int order_due_time"
        param = getattr(cfg, "order_due_time_param", None)
        if param:
            return f"param:{param}"
        return "target_model:default_catch_deadline"

    def _trigger_time(self, instr: OpInstr) -> float:
        if instr.scheduled_finish is not None:
            return float(instr.scheduled_finish)
        if instr.scheduled_start is not None:
            return float(instr.scheduled_start)
        return 0.0

    def _latency(self, instr: OpInstr) -> float:
        if instr.expected_time is not None:
            return float(instr.expected_time)
        if not self.tm:
            return 0.0
        if instr.op_type == "Load":
            return self.tm.get_default_load_time()
        if instr.op_type == "Store":
            return self.tm.get_default_store_time()
        if instr.op_type in ("Move", "Assign"):
            return self.tm.get_default_move_time()
        key = self.tm.optype_to_instruction(instr.op_type)
        if key and instr.rp:
            return self.tm.get_expected_time(key.opcode, key.func, instr.rp) or 0.0
        return 0.0

    def _dominant_preg(self, instr: OpInstr, allocation: Dict[str, Any]):
        candidates = []
        if instr.target:
            candidates.append(instr.target)
        for idx in self._material_arg_indices(instr.op_type, len(instr.args)):
            if idx < len(instr.args):
                candidates.append(instr.args[idx])
        for vreg in candidates:
            preg = allocation.get(vreg)
            if preg is not None and preg != "memory":
                return preg
        return None

    def _explicit_device(self, cfg: ControlFlowGraph, instr: OpInstr) -> Optional[str]:
        if instr.op_type not in ("Process", "Inspect", "Adjust"):
            return None
        if not instr.args:
            return None
        resource_names = getattr(cfg, "resource_names", set()) or set()
        dev = instr.args[0]
        return dev if dev in resource_names else None

    def _build_target_maps(self) -> None:
        if not self.tm or not getattr(self.tm, "data", None):
            return
        addr = self.tm.data.get("addressing_model", {})
        for ws in addr.get("workstation_mapping", []):
            ws_id = ws.get("workstation_id")
            if not isinstance(ws_id, int):
                continue
            self._ws_to_palus[ws_id] = list(ws.get("assigned_palus", []) or [])
            self._ws_to_pregs[ws_id] = list(ws.get("assigned_registers", []) or [])
            for preg in self._ws_to_pregs[ws_id]:
                self._preg_to_ws[preg] = ws_id
        ics = self.tm.data.get("instruction_capability_set", {})
        for palu in ics.get("palu_definitions", []) or []:
            pid = palu.get("palu_id")
            if not isinstance(pid, int):
                continue
            caps = []
            for cap in palu.get("supported_capabilities", []) or []:
                caps.append((str(cap.get("opcode", "")), str(cap.get("func", "")), str(cap.get("rp", ""))))
            self._palu_caps[pid] = caps

    def _workstations_for_cap(self, cap: Tuple[str, str, str]) -> List[int]:
        result = []
        for ws, palus in self._ws_to_palus.items():
            if any(cap in self._palu_caps.get(pid, []) for pid in palus):
                result.append(ws)
        return result

    def _first_preg_for_ws(self, ws: Optional[int]):
        if ws is None:
            return None
        regs = self._ws_to_pregs.get(ws, [])
        return regs[0] if regs else None

    def _first_palu_for_cap(self, ws: Optional[int], cap: Optional[Tuple[str, str, str]]) -> Optional[int]:
        if ws is None or cap is None:
            return None
        for pid in self._ws_to_palus.get(ws, []):
            if cap in self._palu_caps.get(pid, []):
                return pid
        return None

    def _synthetic_device(self, ws: Optional[int], palu: Optional[int]) -> Optional[str]:
        if ws is None and palu is None:
            return None
        return f"WS{ws}_PALU{palu}"

    def _copy_device_timeline(self, schedule_result: Optional[ScheduleResult]) -> Dict[str, List[Tuple[float, float, str]]]:
        copied: Dict[str, List[Tuple[float, float, str]]] = {}
        if schedule_result is None:
            return copied
        for dev, entries in (schedule_result.device_timeline or {}).items():
            copied[dev] = [(float(st), float(fin), str(label)) for st, fin, label in entries]
        return copied

    def _material_arg_indices(self, op_type: str, argc: int):
        if op_type == "Process":
            return range(1, argc)
        if op_type == "Inspect":
            return [1] if argc >= 2 else []
        if op_type == "Adjust":
            return [1] if argc >= 2 else []
        if op_type in ("Move", "Load", "Store"):
            return [0] if argc >= 1 else []
        if op_type == "Assign":
            return [0] if argc >= 1 else []
        return []

    def _message(self, sid: str, state: str, t_exc: float, deadline: float) -> str:
        if deadline == float("inf"):
            return f"{sid}: catch deadline is runtime order_due_time; no default-deadline warning emitted."
        if state == "scheduled":
            return f"{sid}: catch scheduled before deadline {deadline:.1f}."
        if state == "deferred":
            return f"{sid}: catch deferred; execution remains defined but may exceed deadline {deadline:.1f}."
        return f"{sid}: catch late recovery path required after deadline {deadline:.1f}."
