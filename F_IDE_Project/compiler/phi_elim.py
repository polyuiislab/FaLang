"""Replace Phi nodes with path-specific Assign operations representing physical material transfer."""

from compiler.ir import ControlFlowGraph, BasicBlock, OpInstr, PhiInstr, BranchInstr, JumpInstr


class PhiElimination:
    def eliminate(self, cfg: ControlFlowGraph) -> None:

        for block in list(cfg.blocks):
            phis = [instr for instr in block.instructions if isinstance(instr, PhiInstr)]
            if not phis:
                continue

            for phi in phis:
                for pred_block, version in (phi.choices or {}).items():
                    self._insert_assign(cfg, pred_block, block, phi.target, version)


            block.instructions = [i for i in block.instructions if not isinstance(i, PhiInstr)]

    def _insert_assign(
        self,
        cfg: ControlFlowGraph,
        pred_block: BasicBlock,
        curr_block: BasicBlock,
        target: str,
        src: str,
    ):
        """Insert a physical Assign on the incoming edge, splitting a critical edge when needed."""
        if len(pred_block.successors) > 1:
            new_block = self._split_critical_edge(cfg, pred_block, curr_block)
            new_block.add_instr(OpInstr("Assign", target, [src]))
            new_block.add_instr(JumpInstr(curr_block))
        else:
            insert_idx = self._find_insert_position(pred_block)
            pred_block.instructions.insert(insert_idx, OpInstr("Assign", target, [src]))

    @staticmethod
    def _find_insert_position(block: BasicBlock) -> int:
        """Locate the insertion point before the block terminator."""
        n = len(block.instructions)
        for idx in range(n - 1, -1, -1):
            if isinstance(block.instructions[idx], (BranchInstr, JumpInstr)):
                return idx
        return n

    @staticmethod
    def _split_critical_edge(
        cfg: ControlFlowGraph, pred_block: BasicBlock, curr_block: BasicBlock
    ) -> BasicBlock:

        new_block = cfg.new_block(f"PhiPred_{pred_block.id}_{curr_block.id}")


        if curr_block in pred_block.successors:
            pred_block.successors.remove(curr_block)
        if pred_block in curr_block.predecessors:
            curr_block.predecessors.remove(pred_block)

        pred_block.add_successor(new_block)
        new_block.add_successor(curr_block)


        if pred_block.instructions:
            last_instr = pred_block.instructions[-1]
            if isinstance(last_instr, JumpInstr) and last_instr.target_block is curr_block:
                last_instr.target_block = new_block
            elif isinstance(last_instr, BranchInstr):
                if last_instr.true_block is curr_block:
                    last_instr.true_block = new_block
                if last_instr.false_block is curr_block:
                    last_instr.false_block = new_block

        return new_block
