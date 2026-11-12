from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

from compiler.ir import BasicBlock, ControlFlowGraph, OpInstr


def _material_arg_indices(op_type: str, argc: int):
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


def _is_vreg(name: str, resource_names: Set[str]) -> bool:
    if not isinstance(name, str) or not name:
        return False
    if name.isdigit() or name.startswith('"') or name in resource_names:
        return False
    parts = name.rsplit("_", 1)
    return len(parts) == 2 and parts[1].isdigit()


@dataclass
class LandingPadInfo:
    landing_pad_id: str
    exception_type: str
    trigger_device: Optional[str]
    trigger_workstation: Optional[int]
    trigger_preg: Optional[int]
    faulted_materials: List[str]
    t_exc_policy: str = "scheduled_finish"


@dataclass
class ExceptionTableEntry:
    entry_id: str
    trigger_block_id: int
    trigger_instr_index: int
    trigger_instr: OpInstr
    exception_type: str
    catch_block: BasicBlock
    landing_pad: LandingPadInfo
    handler_address: Optional[int] = None
    normal_live_vregs: Set[str] = field(default_factory=set)
    exception_live_vregs: Set[str] = field(default_factory=set)


class ExceptionTableBuilder:
    """Build exception entries and landing-pad physical context from CFG exception edges."""

    def build(self, cfg: ControlFlowGraph) -> List[ExceptionTableEntry]:
        resource_names = set(getattr(cfg, "resource_names", set()) or set())
        catch_ids = self._catch_block_ids(cfg)
        normal_vregs = self._collect_vregs(
            [b for b in cfg.blocks if b.id not in catch_ids],
            resource_names,
        )

        entries: List[ExceptionTableEntry] = []
        for block in cfg.blocks:
            for idx, instr in enumerate(block.instructions):
                if not isinstance(instr, OpInstr) or not instr.catch_edges:
                    continue
                for catch_block in instr.catch_edges:
                    exc_type = self._exception_type_from_block(catch_block)
                    entry_id = f"EXC_B{block.id}_I{idx}_B{catch_block.id}"
                    faulted = self._faulted_materials(instr, resource_names)
                    landing = LandingPadInfo(
                        landing_pad_id=f"LP_{entry_id}",
                        exception_type=exc_type,
                        trigger_device=getattr(instr, "assigned_device_id", None) or self._explicit_device(cfg, instr),
                        trigger_workstation=getattr(instr, "assigned_workstation_id", None),
                        trigger_preg=getattr(instr, "assigned_preg", None),
                        faulted_materials=faulted,
                    )
                    entries.append(
                        ExceptionTableEntry(
                            entry_id=entry_id,
                            trigger_block_id=block.id,
                            trigger_instr_index=idx,
                            trigger_instr=instr,
                            exception_type=exc_type,
                            catch_block=catch_block,
                            landing_pad=landing,
                            normal_live_vregs=set(normal_vregs),
                            exception_live_vregs=self._collect_vregs([catch_block], resource_names),
                        )
                    )

        cfg.exception_table = entries
        cfg.path_sensitive_liveness = {
            "normal": set(normal_vregs),
            "exception": {e.entry_id: set(e.exception_live_vregs) for e in entries},
        }
        return entries

    def _catch_block_ids(self, cfg: ControlFlowGraph) -> Set[int]:
        ids = set()
        for block in cfg.blocks:
            for instr in block.instructions:
                if isinstance(instr, OpInstr):
                    ids.update(b.id for b in instr.catch_edges or [])
        return ids

    def _collect_vregs(self, blocks: List[BasicBlock], resource_names: Set[str]) -> Set[str]:
        vregs: Set[str] = set()
        for block in blocks:
            for instr in block.instructions:
                if not isinstance(instr, OpInstr):
                    continue
                if _is_vreg(instr.target, resource_names):
                    vregs.add(instr.target)
                for it in instr.implicit_targets or []:
                    if _is_vreg(it, resource_names):
                        vregs.add(it)
                for idx in _material_arg_indices(instr.op_type, len(instr.args)):
                    if idx < len(instr.args) and _is_vreg(instr.args[idx], resource_names):
                        vregs.add(instr.args[idx])
        return vregs

    def _faulted_materials(self, instr: OpInstr, resource_names: Set[str]) -> List[str]:
        mats = []
        for idx in _material_arg_indices(instr.op_type, len(instr.args)):
            if idx < len(instr.args):
                arg = instr.args[idx]
                if _is_vreg(arg, resource_names):
                    mats.append(arg)
        return mats

    def _exception_type_from_block(self, block: BasicBlock) -> str:
        name = getattr(block, "name", "") or ""
        if name.startswith("Catch_"):
            return name.replace("Catch_", "", 1) or "Exception"
        return "Exception"

    def _explicit_device(self, cfg: ControlFlowGraph, instr: OpInstr) -> Optional[str]:
        if instr.op_type not in ("Process", "Inspect", "Adjust"):
            return None
        if not instr.args:
            return None
        resource_names = set(getattr(cfg, "resource_names", set()) or set())
        dev = instr.args[0]
        return dev if dev in resource_names else None
