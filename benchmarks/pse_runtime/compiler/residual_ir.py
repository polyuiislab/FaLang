# F_IDE_Project/compiler/residual_ir.py
"""
Paper 1 —— 不可变订单程序、残余 IR 与 Workset Composer。

制造语境：定制订单排进车间之后，物料会一张一张地往前走。已经做完的工序不可能再改，
正在做的工序不能被打断，只有还没开始的工序才允许重新决定"放在哪里、在哪台机器上做"。

因此：

- 每张订单对应一个**不可变程序** `P_i`。订单一旦生成，源码与 IR 都不再被改写。
  新订单到来只会新增 `P_new`，不会回头修改已有订单的程序。
- 一个订单在时刻 t 剩下没做完的部分叫它的**残余 IR** `Residual(P_i)`。
- 车间当前的**工作集**是

      W_t = ⋃ Residual(P_i) ∪ IR(P_new)

  本模块负责算出这个工作集，并把其中的物料版本分成三类：

  - 冻结：生产工序已经完成或用料已经全部消耗完，不再参与决策；
  - 携带：物料已经存在于某个实际位置（由运行时状态给出），它的位置是既成事实，
    只能沿用、不能重新挑；
  - 待定：生产工序还没开始（或还没做完），这些才是需要重新落位的版本。

只有第三类进入 Residual Graph Coloring。这就是"局部重调度"在表示层的样子：
它发生在残余 IR 上，而不是把 FaLang 源码重新写一遍。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from compiler.ir import BasicBlock, OpInstr, PhiInstr, ControlFlowGraph
from compiler.mdg import ManufacturingDependencyGraph


# 物料版本的三种残余状态
RESIDUAL_PENDING = "pending"      # 需要重新落位
RESIDUAL_CARRIED = "carried"      # 位置由运行时状态给定，只能沿用
RESIDUAL_FROZEN = "frozen"        # 已完成，不参与决策
RESIDUAL_HANDLER = "handler"      # 预编译处理器里的动作：只在处理器被激活时才存在


@dataclass(frozen=True)
class OperationKey:
    """订单内一道工序的稳定标识。"""

    order_id: str
    seq: int
    name: str

    @property
    def key(self) -> str:
        return f"{self.order_id}:{self.seq}"


@dataclass(frozen=True)
class OrderProgram:
    """不可变订单程序：生成一次，此后任何阶段都不再改写。"""

    order_id: str
    source: str
    operations: Tuple[OperationKey, ...]
    meta: Mapping[str, object] = field(default_factory=dict)

    def op_at(self, seq: int) -> Optional[OperationKey]:
        for op in self.operations:
            if op.seq == seq:
                return op
        return None


@dataclass
class CompiledOrderProgram:
    """一个不可变订单程序编译后的 IR。"""

    program: OrderProgram
    cfg: ControlFlowGraph
    mdg: Optional[ManufacturingDependencyGraph] = None
    # 工序 key -> 该工序对应的指令对象 id 集合
    op_instr_ids: Dict[str, List[int]] = field(default_factory=dict)

    def residual_ops(self, completed_seq: int) -> List[OperationKey]:
        return [op for op in self.program.operations if op.seq > completed_seq]


@dataclass
class ResidualRecord:
    """某订单在时刻 t 的完成情况（由运行时状态提供）。"""

    order_id: str
    completed_seq: int = -1
    executing_seqs: Tuple[int, ...] = ()


@dataclass
class ResidualView:
    """一个订单的残余视图。"""

    order_id: str
    frozen_ops: Tuple[OperationKey, ...] = ()
    residual_ops: Tuple[OperationKey, ...] = ()

    def residual_op_keys(self) -> Set[str]:
        return {op.key for op in self.residual_ops}


@dataclass
class ResidualFragment:
    """残余 IR 片段：原订单 CFG 的浅层副本 + 逐指令的残余标注。"""

    order_id: str
    cfg: ControlFlowGraph
    view: ResidualView
    mdg: Optional[ManufacturingDependencyGraph] = None
    # id(instr) -> 残余状态
    instr_state: Dict[int, str] = field(default_factory=dict)
    # id(instr) -> 所属工序 key
    instr_op: Dict[int, str] = field(default_factory=dict)
    # 本片段内需要重新落位 / 只能沿用既有位置 / 已完成 的物料版本
    pending_vregs: Set[str] = field(default_factory=set)
    carried_vregs: Set[str] = field(default_factory=set)
    frozen_vregs: Set[str] = field(default_factory=set)

    def pending_instr_ids(self) -> Set[int]:
        return {iid for iid, st in self.instr_state.items() if st == RESIDUAL_PENDING}

    def residual_instr_ids(self) -> Set[int]:
        return {iid for iid, st in self.instr_state.items() if st != RESIDUAL_FROZEN}


@dataclass
class ResidualWorkset:
    """W_t = ⋃ Residual(P_i) ∪ IR(P_new)。"""

    fragments: List[ResidualFragment] = field(default_factory=list)
    # 物料版本 -> 运行时已知位置（携带类版本）
    pinned_locations: Dict[str, int] = field(default_factory=dict)
    # 需要重新落位的物料版本
    pending_vregs: Set[str] = field(default_factory=set)
    # 只做记录，不参与决策
    frozen_vregs: Set[str] = field(default_factory=set)
    carried_vregs: Set[str] = field(default_factory=set)

    # 运行时给出的最早开工下界；前序实体动作完成前不能使用该版本的生产资源。
    earliest_use: Dict[str, float] = field(default_factory=dict)

    def orders(self) -> List[str]:
        return [fragment.order_id for fragment in self.fragments]

    def residual_instruction_count(self) -> int:
        return sum(len(fragment.residual_instr_ids()) for fragment in self.fragments)

    def snapshot(self) -> Dict[str, object]:
        return {
            "orders": len(self.fragments),
            "residual_instructions": self.residual_instruction_count(),
            "pending_vregs": len(self.pending_vregs),
            "carried_vregs": len(self.carried_vregs),
            "frozen_vregs": len(self.frozen_vregs),
        }


class WorksetComposer:
    """把若干不可变订单程序按完成情况组合成当前工作集。"""

    def __init__(self, vreg_predicate=None):
        # vreg_predicate 与 RegisterAllocator._is_vreg 一致，用于区分物料版本与资源名
        self._is_vreg = vreg_predicate or _default_is_vreg

    def compose(
        self,
        compiled: Sequence[CompiledOrderProgram],
        records: Mapping[str, ResidualRecord],
        factory_state=None,
    ) -> ResidualWorkset:
        workset = ResidualWorkset()
        runtime_locations = getattr(factory_state, "wip_location", None) or {}

        for item in compiled:
            record = records.get(item.program.order_id)
            if record is None:
                record = ResidualRecord(order_id=item.program.order_id)
            fragment = self._compose_fragment(item, record)
            workset.fragments.append(fragment)

            pending_defined: Set[str] = set()
            carried_defined: Set[str] = set()
            frozen_defined: Set[str] = set()
            for block in fragment.cfg.blocks:
                for instr in block.instructions:
                    if not isinstance(instr, OpInstr):
                        continue
                    state = fragment.instr_state.get(id(instr), RESIDUAL_PENDING)
                    if state == RESIDUAL_HANDLER:
                        continue
                    target = pending_defined if state == RESIDUAL_PENDING else (
                        carried_defined if state == RESIDUAL_CARRIED else frozen_defined
                    )
                    for name in self._defined_materials(instr):
                        target.add(name)
                    if state == RESIDUAL_CARRIED:
                        for name in self._defined_materials(instr):
                            location = runtime_locations.get(name)
                            if isinstance(location, int):
                                workset.pinned_locations[name] = location

            fragment.pending_vregs = pending_defined - carried_defined - frozen_defined
            fragment.carried_vregs = carried_defined
            fragment.frozen_vregs = frozen_defined - pending_defined - carried_defined
            workset.pending_vregs |= fragment.pending_vregs
            workset.carried_vregs |= carried_defined
            workset.frozen_vregs |= fragment.frozen_vregs

        return workset

    # ------------------------------------------------------------------
    def _compose_fragment(self, item: CompiledOrderProgram, record: ResidualRecord) -> ResidualFragment:
        view = ResidualView(
            order_id=item.program.order_id,
            frozen_ops=tuple(op for op in item.program.operations if op.seq <= record.completed_seq),
            residual_ops=tuple(item.residual_ops(record.completed_seq)),
        )
        frozen_keys = {op.key for op in view.frozen_ops}
        residual_keys = view.residual_op_keys()
        executing = set(record.executing_seqs)

        fragment = ResidualFragment(
            order_id=item.program.order_id, cfg=item.cfg, view=view, mdg=item.mdg
        )
        for block in item.cfg.blocks:
            for instr in block.instructions:
                if _is_handler_block(block):
                    # catch / compensate 里的指令只在处理器被激活时才存在，
                    # 平时不进入工作集；激活时由 UncertaintyCoordinator 单独着色。
                    fragment.instr_state[id(instr)] = RESIDUAL_HANDLER
                    fragment.instr_op[id(instr)] = ""
                    continue
                op_key = self._op_key_of(item, instr, fragment)
                if op_key is None:
                    # 不属于任何工序的指令（Phi、跳转等）跟随其所在块的处理状态；
                    # 默认视为残余，避免把尚未完成的工作误判成已完成。
                    fragment.instr_state[id(instr)] = RESIDUAL_PENDING
                    fragment.instr_op[id(instr)] = ""
                    continue
                seq = self._seq_of(op_key)
                fragment.instr_op[id(instr)] = op_key
                if op_key in frozen_keys:
                    fragment.instr_state[id(instr)] = RESIDUAL_FROZEN
                elif seq in executing:
                    # 正在执行的工序不可抢占，其物料位置视为既成事实
                    fragment.instr_state[id(instr)] = RESIDUAL_CARRIED
                elif op_key in residual_keys:
                    fragment.instr_state[id(instr)] = RESIDUAL_PENDING
                else:
                    fragment.instr_state[id(instr)] = RESIDUAL_FROZEN
        return fragment

    @staticmethod
    def _op_key_of(item: CompiledOrderProgram, instr, fragment: ResidualFragment) -> Optional[str]:
        if not isinstance(instr, OpInstr):
            return None
        instr_id = id(instr)
        for op_key, ids in item.op_instr_ids.items():
            if instr_id in ids:
                return op_key
        return None

    @staticmethod
    def _seq_of(op_key: str) -> int:
        _, _, seq = op_key.rpartition(":")
        return int(seq) if seq.isdigit() else -1

    # ------------------------------------------------------------------
    @staticmethod
    def _defined_materials(instr: OpInstr) -> List[str]:
        names = []
        if instr.target:
            names.append(instr.target)
        for extra in instr.implicit_targets or []:
            names.append(extra)
        return names


def _is_handler_block(block) -> bool:
    """catch / compensate 块：只承载预编译处置，不属于常规残余工作。"""
    name = getattr(block, "name", "") or ""
    return name.startswith("Catch_") or name.startswith("Compensate")


def _default_is_vreg(name) -> bool:
    if not isinstance(name, str) or not name:
        return False
    if name.isdigit() or name.startswith('"'):
        return False
    head, _, tail = name.rpartition("_")
    return bool(head) and tail.isdigit()


def index_op_instructions(
    program: OrderProgram,
    cfg: ControlFlowGraph,
) -> Dict[str, List[int]]:
    """
    把指令归到各自的工序上。

    依据是生成器为每条工序写下的 `params` 前缀（`<op_name>;component=...`），
    若匹配不上则退化为按代码块内出现顺序对齐工序序列。
    """
    index: Dict[str, List[int]] = {}
    names = {op.name: op for op in program.operations}
    seen: List[str] = []
    for block in cfg.blocks:
        for instr in block.instructions:
            if not isinstance(instr, OpInstr):
                continue
            head = ""
            params = getattr(instr, "params", None)
            if isinstance(params, str) and params:
                head = params.split(";", 1)[0].strip()
            op = names.get(head)
            if op is None:
                continue
            index.setdefault(op.key, []).append(id(instr))
            seen.append(op.key)

    if len(index) < len(program.operations):
        # 退化路径：按出现顺序补齐尚未归属的工序
        remaining = [op for op in program.operations if op.key not in index]
        unmatched = [
            id(instr)
            for block in cfg.blocks
            for instr in block.instructions
            if isinstance(instr, OpInstr) and not any(id(instr) in ids for ids in index.values())
        ]
        for op, instr_id in zip(remaining, unmatched):
            index.setdefault(op.key, []).append(instr_id)
    return index
