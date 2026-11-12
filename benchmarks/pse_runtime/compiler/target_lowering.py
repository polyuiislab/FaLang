# F_IDE_Project/compiler/target_lowering.py
"""
Paper 1 —— Target Lowering：把逻辑订单意图落到 Factory Target Machine Model F_t 上。

制造语境：订单里写的是"这一步要做装配"和"物料中间需要一个暂存位置"，而不是"在哪台
机台上做、放进哪个库位"。因此在验证过订单本身的逻辑语义之后，还需要一次**目标绑定**：

    Virtual Resource  --Target Lowering-->  Physical Resource in F_t

这一步负责三件事：

1. 把逻辑动作描述解析为目标机能识别的动作编码（rp）；
2. 校验 F_t 里确实存在能执行该动作的资源（这是"这台机器能不能干这个活"，属于绑定阶段
   的问题，不是订单本身是否合法的问题）；
3. 为每个物料版本算出**候选物理持有位置**：只有所在工位能执行其生产工序的库位才是
   可行候选。这一步的结果直接决定后面的 Residual Graph Coloring 有哪些颜色可用。

本模块不含调度、不含代价模型，只做绑定与可行性收缩。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Set, Tuple

from compiler.ir import BasicBlock, OpInstr, ControlFlowGraph
from compiler.factory_target_machine import FactoryTargetMachine
from compiler.vmrs import VirtualResourceSpace, VirtualResourceKind


Capability = Tuple[str, str, str]


class TargetLoweringError(Exception):
    """某个逻辑动作在 F_t 中找不到可执行资源。"""


@dataclass
class LoweredOperation:
    """一条指令在 F_t 上的绑定结果。"""

    instr: OpInstr
    op_type: str
    capability: Optional[Capability] = None
    # 源码中引用的逻辑资源角色名（Process/Inspect/Adjust 的第一个参数）
    logical_role: Optional[str] = None
    # 能执行该能力的工位；物流动作由共享资源池承担，此处为空
    candidate_workstations: Tuple[int, ...] = ()
    is_logistics: bool = False
    is_frozen: bool = False

    @property
    def is_physical(self) -> bool:
        return self.capability is not None


@dataclass
class LoweringResult:
    """Target Lowering 的完整产物。"""

    operations: Dict[int, LoweredOperation] = field(default_factory=dict)
    # 物料版本 -> 可行物理持有位置（所在工位能执行其全部生产工序）
    eligible_sites: Dict[str, List[int]] = field(default_factory=dict)
    # 物料版本 -> 还需覆盖的消费工序能力（用于在可行位置中择优，不构成硬约束）
    consuming_capabilities: Dict[str, Set[Capability]] = field(default_factory=dict)
    diagnostics: List[str] = field(default_factory=list)

    def op(self, instr) -> Optional[LoweredOperation]:
        return self.operations.get(id(instr))

    def capability_of(self, instr) -> Optional[Capability]:
        lowered = self.operations.get(id(instr))
        return lowered.capability if lowered else None


# 与 scheduler / ssa / register_alloc 保持一致的物料参数位置约定。
def material_arg_indices(op_type: str, argc: int) -> List[int]:
    if op_type == "Process":
        return list(range(1, argc))
    if op_type == "Inspect":
        return [1] if argc >= 2 else []
    if op_type in ("Move", "Load", "Store", "Assign"):
        return [0] if argc >= 1 else []
    return []


class TargetLowering:
    """把 CFG 中的逻辑动作绑定到 F_t。"""

    def __init__(self, f_t: FactoryTargetMachine, vmrs: Optional[VirtualResourceSpace] = None):
        self.f_t = f_t
        self.vmrs = vmrs
        self._tm = f_t._tm

    # ------------------------------------------------------------------
    # 入口
    # ------------------------------------------------------------------
    def lower(self, cfg: ControlFlowGraph, mdg=None) -> LoweringResult:
        result = LoweringResult()
        self._resolve_operations(cfg, result)
        self._collect_eligible_sites(cfg, result)
        return result

    # ------------------------------------------------------------------
    # 1. 动作编码解析 + 目标机可行性
    # ------------------------------------------------------------------
    def _resolve_operations(self, cfg: ControlFlowGraph, result: LoweringResult) -> None:
        for block in cfg.blocks:
            for instr in block.instructions:
                if not isinstance(instr, OpInstr):
                    continue

                lowered = LoweredOperation(instr=instr, op_type=instr.op_type)
                lowered.logical_role = self._logical_role_of(instr)

                key = self._tm.optype_to_instruction(instr.op_type) if self._tm else None
                if key is not None:
                    rp_code = instr.rp or self._resolve_rp(key.opcode, key.func, instr)
                    if not rp_code:
                        raise TargetLoweringError(
                            f"Cannot bind {instr.op_type} to F_t: no action description in the "
                            f"target model matches {getattr(instr, 'params', None)!r}"
                        )
                    lowered.capability = (key.opcode, key.func, rp_code)
                    # 回填到指令，供 codegen / FB 与后续阶段使用
                    instr.opcode = key.opcode
                    instr.func = key.func
                    instr.rp = rp_code
                    if instr.expected_time is None:
                        instr.expected_time = self._expected_time_for(instr, key.opcode, key.func, rp_code)

                if lowered.capability is not None:
                    lowered.is_logistics = self.f_t.is_logistics_capability(lowered.capability)
                    if lowered.is_logistics:
                        if self.f_t.logistics_resource() is None:
                            result.diagnostics.append(
                                f"No logistics resource in F_t for {instr.op_type} ({lowered.capability})"
                            )
                    else:
                        sites = self.f_t.workstations_supporting(lowered.capability)
                        if not sites:
                            raise TargetLoweringError(
                                f"F_t has no production resource able to execute "
                                f"{instr.op_type} (opcode={lowered.capability[0]} "
                                f"func={lowered.capability[1]} rp={lowered.capability[2]})"
                            )
                        lowered.candidate_workstations = tuple(sites)

                self._check_logical_role(instr, lowered, result)
                result.operations[id(instr)] = lowered

    def _expected_time_for(self, instr: OpInstr, opcode: str, func: str, rp_code: str) -> Optional[float]:
        """
        动作的预期耗时。

        加工/检测/调整类动作取目标机模型的处理时间表；搬运与仓储类动作在处理时间表里
        没有条目，取目标机模型的标准物流时间，否则 MDG 的边权会退化为 0，
        占位区间也就无从估算。
        """
        value = self._tm.get_expected_time(opcode, func, rp_code)
        if value is not None:
            return value
        if instr.op_type == "Load":
            return self._tm.get_default_load_time()
        if instr.op_type == "Store":
            return self._tm.get_default_store_time()
        if instr.op_type in ("Move", "Assign"):
            return self._tm.get_default_move_time()
        return None

    def _resolve_rp(self, opcode: str, func: str, instr: OpInstr) -> Optional[str]:
        resolver = getattr(self._tm, "resolve_rp_code", None)
        if resolver is None:
            return None
        # 源码中的 rp 以描述字符串给出，存在 param_str（语法糖）与 operator（等价写法）两处。
        desc = instr.params if getattr(instr, "params", None) else None
        if not desc:
            desc = getattr(instr, "operator", None)
        return resolver(opcode, func, desc)

    def _logical_role_of(self, instr: OpInstr) -> Optional[str]:
        if instr.op_type in ("Process", "Inspect", "Adjust", "Move", "Load", "Store") and instr.args:
            head = instr.args[0]
            if isinstance(head, str) and self.vmrs is not None and self.vmrs.is_known(head):
                return head
        return None

    def _check_logical_role(self, instr: OpInstr, lowered: LoweredOperation, result: LoweringResult) -> None:
        """逻辑角色必须与动作类别相容：加工动作不能落在仓储角色上。"""
        if self.vmrs is None or lowered.logical_role is None:
            return
        kind = self.vmrs.kind_of(lowered.logical_role)
        if kind is None:
            return
        if instr.op_type in ("Process", "Inspect", "Adjust") and kind is not VirtualResourceKind.PRODUCTION:
            result.diagnostics.append(
                f"{instr.op_type} targets non-production role {lowered.logical_role!r} ({kind.value})"
            )

    # ------------------------------------------------------------------
    # 2. 物料版本的候选物理持有位置
    # ------------------------------------------------------------------
    def _collect_eligible_sites(self, cfg: ControlFlowGraph, result: LoweringResult) -> None:
        producers: Dict[str, List[LoweredOperation]] = {}
        consumers: Dict[str, List[LoweredOperation]] = {}

        for block in cfg.blocks:
            for instr in block.instructions:
                if not isinstance(instr, OpInstr):
                    continue
                lowered = result.operations.get(id(instr))
                if lowered is None:
                    continue
                for name in self._defined_materials(instr):
                    producers.setdefault(name, []).append(lowered)
                for name in self._used_materials(instr):
                    consumers.setdefault(name, []).append(lowered)

        for name in sorted(set(producers) | set(consumers)):
            def_caps = self._production_capabilities(producers.get(name, []))
            use_caps = self._production_capabilities(consumers.get(name, []))
            result.consuming_capabilities[name] = use_caps
            result.eligible_sites[name] = self._sites_for_capabilities(def_caps)

    @staticmethod
    def _defined_materials(instr: OpInstr) -> List[str]:
        names = []
        if instr.target:
            names.append(instr.target)
        for extra in instr.implicit_targets or []:
            names.append(extra)
        return names

    @staticmethod
    def _used_materials(instr: OpInstr) -> List[str]:
        names = []
        for idx in material_arg_indices(instr.op_type, len(instr.args)):
            if idx < len(instr.args):
                names.append(instr.args[idx])
        return names

    def _production_capabilities(self, operations: Iterable[LoweredOperation]) -> Set[Capability]:
        caps: Set[Capability] = set()
        for lowered in operations:
            if lowered.capability is None or lowered.is_logistics:
                continue
            caps.add(lowered.capability)
        return caps

    def _sites_for_capabilities(self, capabilities: Set[Capability]) -> List[int]:
        """取所有满足全部能力约束的持有位置。无约束时返回全部位置。"""
        candidates: Optional[Set[int]] = None
        for cap in capabilities:
            ws_ids = set(self.f_t.workstations_supporting(cap))
            sites = {sid for ws in ws_ids for sid in self.f_t.holding_sites_of_workstation(ws)}
            candidates = sites if candidates is None else (candidates & sites)
        if candidates is None:
            candidates = {site.storage_id for site in self.f_t.holding_sites()}
        return sorted(candidates)
