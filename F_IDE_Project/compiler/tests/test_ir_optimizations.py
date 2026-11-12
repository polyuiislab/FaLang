import os
import sys
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from compiler.ir import ControlFlowGraph, OpInstr, PhiInstr  # noqa: E402
from compiler.ir_opt import optimize_cfg  # noqa: E402
from compiler.ssa import SSAPass  # noqa: E402

class TestIROptimizations(unittest.TestCase):
    def test_redundant_consecutive_load_elim(self):
        cfg = ControlFlowGraph()
        b0 = cfg.entry_block
        b0.add_instr(OpInstr("Load", None, ["ToolA"]))
        b0.add_instr(OpInstr("Load", None, ["ToolA"]))
        b0.add_instr(OpInstr("Load", None, ["ToolA"], params="P=1"))
        b0.add_instr(OpInstr("Load", None, ["ToolA"], params="P=1"))

        stats = optimize_cfg(cfg)
        self.assertEqual(stats.removed_redundant_loads, 2)
        loads = [i for i in b0.instructions if isinstance(i, OpInstr) and i.op_type == "Load"]
        self.assertEqual(len(loads), 2)

    def test_redundant_move_elim_no_barrier(self):
        cfg = ControlFlowGraph()
        b0 = cfg.entry_block
        b0.add_instr(OpInstr("Move", None, ["Part1", "Buf_A"]))
        b0.add_instr(OpInstr("Adjust", None, ["CNC_1"], params="Nop"))
        b0.add_instr(OpInstr("Move", None, ["Part1", "Buf_B"]))

        stats = optimize_cfg(cfg)
        self.assertEqual(stats.removed_redundant_moves, 1)
        moves = [i for i in b0.instructions if isinstance(i, OpInstr) and i.op_type == "Move"]
        self.assertEqual(len(moves), 1)
        self.assertEqual(moves[0].args, ["Part1", "Buf_B"])

    def test_redundant_move_not_elim_with_barrier(self):
        cfg = ControlFlowGraph()
        b0 = cfg.entry_block
        b0.add_instr(OpInstr("Move", None, ["Part1", "Buf_A"]))
        b0.add_instr(OpInstr("Process", None, ["CNC_1", "Part1"], params="Recipe=R1"))
        b0.add_instr(OpInstr("Move", None, ["Part1", "Buf_B"]))

        stats = optimize_cfg(cfg)
        self.assertEqual(stats.removed_redundant_moves, 0)
        moves = [i for i in b0.instructions if isinstance(i, OpInstr) and i.op_type == "Move"]
        self.assertEqual(len(moves), 2)

    def test_phi_simplification_all_same(self):
        cfg = ControlFlowGraph()
        b0 = cfg.entry_block
        b1 = cfg.new_block("B1")
        b2 = cfg.new_block("B2")
        b3 = cfg.new_block("Merge")

        b0.add_successor(b1)
        b0.add_successor(b2)
        b1.add_successor(b3)
        b2.add_successor(b3)

        phi = PhiInstr("CNC_Main_2", {b1: "CNC_Main_1", b2: "CNC_Main_1"})
        b3.instructions.insert(0, phi)
        b3.add_instr(OpInstr("Adjust", None, ["CNC_Main_2"], params="HomeAxes"))

        ssa = SSAPass()
        ssa._simplify_phis(cfg)

        self.assertFalse(any(isinstance(i, PhiInstr) for i in b3.instructions))
        uses = [i for i in b3.instructions if isinstance(i, OpInstr)]
        self.assertEqual(uses[0].args, ["CNC_Main_1"])

if __name__ == "__main__":
    unittest.main()
