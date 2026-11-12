from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from compiler.ir import BasicBlock, BranchInstr, ControlFlowGraph, IRInstr, JumpInstr, OpInstr, PhiInstr


@dataclass(frozen=True)
class OptimizationStats:
    removed_redundant_loads: int = 0
    removed_redundant_moves: int = 0


def optimize_cfg(cfg: ControlFlowGraph) -> OptimizationStats:
    """
    Machine-independent IR optimizations on CFG, before SSA weighting / target-model mapping.
    """
    stats = OptimizationStats()
    stats = _eliminate_redundant_loads(cfg, stats)
    stats = _eliminate_redundant_moves(cfg, stats)
    return stats


def _eliminate_redundant_loads(cfg: ControlFlowGraph, stats: OptimizationStats) -> OptimizationStats:
    removed = 0
    for block in cfg.blocks:
        new_instrs: List[IRInstr] = []
        prev_load_sig: Optional[Tuple[Tuple[str, ...], Optional[str]]] = None
        for instr in block.instructions:
            if isinstance(instr, OpInstr) and instr.op_type == "Load":
                sig = (tuple(instr.args or []), instr.params)
                if prev_load_sig == sig:
                    removed += 1
                    continue
                prev_load_sig = sig
                new_instrs.append(instr)
                continue

            if isinstance(instr, (OpInstr, BranchInstr, JumpInstr, PhiInstr)):
                prev_load_sig = None
            new_instrs.append(instr)
        block.instructions = new_instrs

    return OptimizationStats(
        removed_redundant_loads=stats.removed_redundant_loads + removed,
        removed_redundant_moves=stats.removed_redundant_moves,
    )


def _is_barrier_for_move_elim(instr: IRInstr, part: str, intermediate_loc: str) -> bool:
    if isinstance(instr, PhiInstr):
        return True
    if isinstance(instr, (BranchInstr, JumpInstr)):
        return True
    if isinstance(instr, OpInstr):
        if instr.target in (part, intermediate_loc):
            return True
        args = instr.args or []
        if part in args or intermediate_loc in args:
            return True
    return False


def _move_signature(instr: OpInstr) -> Optional[Tuple[str, str]]:
    if instr.op_type == "Move":
        args = instr.args or []
        if len(args) < 2:
            return None
        return args[0], args[1]
    if instr.op_type == "Assign":
        args = instr.args or []
        if len(args) < 1 or not instr.target:
            return None
        return args[0], instr.target
    return None


def _eliminate_redundant_moves(cfg: ControlFlowGraph, stats: OptimizationStats) -> OptimizationStats:
    removed = 0

    for block in cfg.blocks:
        # Map: part -> (index_in_list, intermediate_loc)
        last_move: Dict[str, Tuple[int, str]] = {}
        instrs = list(block.instructions)

        i = 0
        while i < len(instrs):
            instr = instrs[i]

            if isinstance(instr, OpInstr):
                sig = _move_signature(instr)
                if sig is not None:
                    part, dest = sig
                    if part in last_move:
                        prev_idx, prev_dest = last_move[part]
                        # Only eliminate if no barrier between prev move and this move.
                        blocked = False
                        for mid in instrs[prev_idx + 1 : i]:
                            if _is_barrier_for_move_elim(mid, part, prev_dest):
                                blocked = True
                                break
                        if not blocked:
                            # Drop previous Move(part, prev_dest); keep only the later Move(part, dest).
                            instrs.pop(prev_idx)
                            removed += 1
                            # Adjust indices after pop.
                            if prev_idx < i:
                                i -= 1
                            # Rebuild last_move conservatively within this block.
                            last_move = {}
                            j = 0
                            while j < i:
                                j_instr = instrs[j]
                                if isinstance(j_instr, OpInstr):
                                    j_sig = _move_signature(j_instr)
                                    if j_sig is not None:
                                        last_move[j_sig[0]] = (j, j_sig[1])
                                    else:
                                        # Any instruction that touches a part or location could invalidate tracking;
                                        # keep it simple: clear tracking when we see other ops.
                                        pass
                                else:
                                    last_move = {}
                                j += 1

                            # Update current move mapping.
                            last_move[part] = (i, dest)
                            i += 1
                            continue

                    last_move[part] = (i, dest)
                    i += 1
                    continue

            # Non-move (or move without explicit destination): conservatively clear tracking when control-flow-ish.
            if isinstance(instr, (BranchInstr, JumpInstr, PhiInstr)):
                last_move = {}
            i += 1

        block.instructions = instrs

    return OptimizationStats(
        removed_redundant_loads=stats.removed_redundant_loads,
        removed_redundant_moves=stats.removed_redundant_moves + removed,
    )

