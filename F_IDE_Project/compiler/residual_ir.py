"""Compose residual worksets from immutable order programs and runtime progress."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from compiler.ir import BasicBlock, OpInstr, PhiInstr, ControlFlowGraph
from compiler.mdg import ManufacturingDependencyGraph


RESIDUAL_PENDING = "pending"      # Requires a new allocation.
RESIDUAL_CARRIED = "carried"      # Retains a known runtime location.
RESIDUAL_FROZEN = "frozen"        # Completed and excluded from allocation.
RESIDUAL_HANDLER = "handler"      # Present only while its precompiled handler is active.


@dataclass(frozen=True)
class OperationKey:
    """Stable operation identifier within an order."""

    order_id: str
    seq: int
    name: str

    @property
    def key(self) -> str:
        return f"{self.order_id}:{self.seq}"


@dataclass(frozen=True)
class OrderProgram:
    """An order program retained unchanged after generation."""

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


    program: OrderProgram
    cfg: ControlFlowGraph
    mdg: Optional[ManufacturingDependencyGraph] = None

    op_instr_ids: Dict[str, List[int]] = field(default_factory=dict)

    def residual_ops(self, completed_seq: int) -> List[OperationKey]:
        return [op for op in self.program.operations if op.seq > completed_seq]


@dataclass
class ResidualRecord:
    """Runtime completion and material-location information for one order."""

    order_id: str
    completed_seq: int = -1
    executing_seqs: Tuple[int, ...] = ()


@dataclass
class ResidualView:


    order_id: str
    frozen_ops: Tuple[OperationKey, ...] = ()
    residual_ops: Tuple[OperationKey, ...] = ()

    def residual_op_keys(self) -> Set[str]:
        return {op.key for op in self.residual_ops}


@dataclass
class ResidualFragment:
    """A shallow CFG view with residual-state annotations on its instructions."""

    order_id: str
    cfg: ControlFlowGraph
    view: ResidualView
    mdg: Optional[ManufacturingDependencyGraph] = None

    instr_state: Dict[int, str] = field(default_factory=dict)

    instr_op: Dict[int, str] = field(default_factory=dict)

    pending_vregs: Set[str] = field(default_factory=set)
    carried_vregs: Set[str] = field(default_factory=set)
    frozen_vregs: Set[str] = field(default_factory=set)

    def pending_instr_ids(self) -> Set[int]:
        return {iid for iid, st in self.instr_state.items() if st == RESIDUAL_PENDING}

    def residual_instr_ids(self) -> Set[int]:
        return {iid for iid, st in self.instr_state.items() if st != RESIDUAL_FROZEN}


@dataclass
class ResidualWorkset:
    """Current residual order fragments plus newly arrived order programs."""

    fragments: List[ResidualFragment] = field(default_factory=list)

    pinned_locations: Dict[str, int] = field(default_factory=dict)

    pending_vregs: Set[str] = field(default_factory=set)

    frozen_vregs: Set[str] = field(default_factory=set)
    carried_vregs: Set[str] = field(default_factory=set)

    # Earliest start imposed by completion of preceding physical operations.
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


    def __init__(self, vreg_predicate=None):

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
                    # Inactive catch and compensate instructions stay outside the normal workset.

                    fragment.instr_state[id(instr)] = RESIDUAL_HANDLER
                    fragment.instr_op[id(instr)] = ""
                    continue
                op_key = self._op_key_of(item, instr, fragment)
                if op_key is None:


                    fragment.instr_state[id(instr)] = RESIDUAL_PENDING
                    fragment.instr_op[id(instr)] = ""
                    continue
                seq = self._seq_of(op_key)
                fragment.instr_op[id(instr)] = op_key
                if op_key in frozen_keys:
                    fragment.instr_state[id(instr)] = RESIDUAL_FROZEN
                elif seq in executing:
                    # Running operations are nonpreemptive; their material locations are fixed.
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


    @staticmethod
    def _defined_materials(instr: OpInstr) -> List[str]:
        names = []
        if instr.target:
            names.append(instr.target)
        for extra in instr.implicit_targets or []:
            names.append(extra)
        return names


def _is_handler_block(block) -> bool:
    """Identify handler blocks activated separately from normal residual work."""
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
    """Index instructions by generated operation metadata, then match unassigned operations by source order."""
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
