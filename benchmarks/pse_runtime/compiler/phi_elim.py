# F_IDE_Project/compiler/phi_elim.py
"""
FaLang Phi 消除（Phi Elimination）。

核心原则：Phi 节点在物理上等价于"根据实际走哪条路径，把该路径上的物理对象移动到统一出口位置"。
因此用 Assign（物理移动语义）代替传统编译器中的复制插入。

处理步骤：
1. 遍历所有基本块，收集 Phi 指令。
2. 对每个 Phi 的每个前驱选择，在前驱块中插入 Assign(target, version)。
3. 若前驱块有多个后继（临界边），则拆分临界边：插入空块并在其中放置 Assign。
4. 删除原 Phi 节点。
"""

from compiler.ir import ControlFlowGraph, BasicBlock, OpInstr, PhiInstr, BranchInstr, JumpInstr


class PhiElimination:
    def eliminate(self, cfg: ControlFlowGraph) -> None:
        """消除 CFG 中所有 Phi 节点。"""
        for block in list(cfg.blocks):
            phis = [instr for instr in block.instructions if isinstance(instr, PhiInstr)]
            if not phis:
                continue

            for phi in phis:
                for pred_block, version in (phi.choices or {}).items():
                    self._insert_assign(cfg, pred_block, block, phi.target, version)

            # 删除 phi 节点
            block.instructions = [i for i in block.instructions if not isinstance(i, PhiInstr)]

    def _insert_assign(
        self,
        cfg: ControlFlowGraph,
        pred_block: BasicBlock,
        curr_block: BasicBlock,
        target: str,
        src: str,
    ):
        """在前驱块（或拆分后的新块）中插入 target = Assign(src)。"""
        if len(pred_block.successors) > 1:
            new_block = self._split_critical_edge(cfg, pred_block, curr_block)
            new_block.add_instr(OpInstr("Assign", target, [src]))
            new_block.add_instr(JumpInstr(curr_block))
        else:
            insert_idx = self._find_insert_position(pred_block)
            pred_block.instructions.insert(insert_idx, OpInstr("Assign", target, [src]))

    @staticmethod
    def _find_insert_position(block: BasicBlock) -> int:
        """找到块末尾、但在 Branch/Jump 之前的插入位置。"""
        n = len(block.instructions)
        for idx in range(n - 1, -1, -1):
            if isinstance(block.instructions[idx], (BranchInstr, JumpInstr)):
                return idx
        return n

    @staticmethod
    def _split_critical_edge(
        cfg: ControlFlowGraph, pred_block: BasicBlock, curr_block: BasicBlock
    ) -> BasicBlock:
        """拆分 pred_block -> curr_block 的临界边，返回中间插入的新块。"""
        new_block = cfg.new_block(f"PhiPred_{pred_block.id}_{curr_block.id}")

        # 修改图结构
        if curr_block in pred_block.successors:
            pred_block.successors.remove(curr_block)
        if pred_block in curr_block.predecessors:
            curr_block.predecessors.remove(pred_block)

        pred_block.add_successor(new_block)
        new_block.add_successor(curr_block)

        # 若 pred 末尾有显式跳转/分支指向 curr，修改其目标为新块
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
