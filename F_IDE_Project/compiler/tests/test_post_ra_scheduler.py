import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import unittest
from compiler.ir import ControlFlowGraph, OpInstr, BranchInstr, JumpInstr
from compiler.post_ra_scheduler import PostRAScheduler
from core.target_model import TargetModelLoader

class TestPostRAScheduler(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tm = TargetModelLoader()
        ok, msg = cls.tm.load()
        if not ok:
            raise RuntimeError(f"Failed to load target model: {msg}")

    def test_coalesce_same_preg(self):
        cfg = ControlFlowGraph()
        cfg.resource_names = {"CNC_1"}
        b0 = cfg.entry_block

        b0.add_instr(OpInstr("Process", "part_1", ["CNC_1", "part_0"], operator="High_Precision_CNC_Milling"))
        b0.add_instr(OpInstr("Assign", "part_2", ["part_1"]))
        b0.add_instr(OpInstr("Process", "part_3", ["CNC_1", "part_2"], operator="High_Precision_CNC_Milling"))

        alloc = {"part_0": 0, "part_1": 1, "part_2": 1, "part_3": 0}
        sched = PostRAScheduler(self.tm)
        sched.schedule(cfg, alloc)

        assigns = [i for i in b0.instructions if isinstance(i, OpInstr) and i.op_type == "Assign"]
        self.assertEqual(len(assigns), 0, "Redundant Assign should be coalesced away")

    def test_coalesce_different_preg_kept(self):
        cfg = ControlFlowGraph()
        cfg.resource_names = {"CNC_1"}
        b0 = cfg.entry_block

        b0.add_instr(OpInstr("Process", "part_1", ["CNC_1", "part_0"], operator="High_Precision_CNC_Milling"))
        b0.add_instr(OpInstr("Assign", "part_2", ["part_1"]))

        alloc = {"part_0": 0, "part_1": 0, "part_2": 2}
        sched = PostRAScheduler(self.tm)
        sched.schedule(cfg, alloc)

        assigns = [i for i in b0.instructions if isinstance(i, OpInstr) and i.op_type == "Assign"]
        self.assertEqual(len(assigns), 1, "Cross-PReg Assign should be kept")

    def test_raw_dependency_respected(self):
        cfg = ControlFlowGraph()
        cfg.resource_names = {"CNC_1"}
        b0 = cfg.entry_block

        i1 = OpInstr("Process", "part_1", ["CNC_1", "part_0"], operator="High_Precision_CNC_Milling")
        i2 = OpInstr("Process", "part_2", ["CNC_1", "part_1"], operator="High_Precision_CNC_Milling")
        b0.add_instr(i1)
        b0.add_instr(i2)

        alloc = {"part_0": 0, "part_1": 1, "part_2": 0}
        sched = PostRAScheduler(self.tm)
        sched.schedule(cfg, alloc)

        idx1 = b0.instructions.index(i1)
        idx2 = b0.instructions.index(i2)
        self.assertLess(idx1, idx2, "RAW dependency must be respected")

    def test_load_before_store_priority(self):
        cfg = ControlFlowGraph()
        cfg.resource_names = {"CNC_1"}
        b0 = cfg.entry_block

        store = OpInstr("Store", None, ["part_0", "0"])
        load = OpInstr("Load", None, ["part_1", "0"])
        proc = OpInstr("Process", "part_2", ["CNC_1", "part_1"], operator="High_Precision_CNC_Milling")
        b0.add_instr(store)
        b0.add_instr(load)
        b0.add_instr(proc)

        # part_0 and part_1 use distinct registers; part_2 depends on part_1.
        alloc = {"part_0": 20, "part_1": 21, "part_2": 0}
        sched = PostRAScheduler(self.tm)
        sched.schedule(cfg, alloc)

        idx_store = b0.instructions.index(store)
        idx_load = b0.instructions.index(load)
        idx_proc = b0.instructions.index(proc)

        self.assertLess(idx_load, idx_store, "Load should be scheduled before Store")
        self.assertLess(idx_load, idx_proc, "Proc should be after Load")

    def test_control_instr_kept_at_end(self):
        cfg = ControlFlowGraph()
        b0 = cfg.entry_block
        b1 = cfg.new_block("Target")

        i1 = OpInstr("Process", "part_1", ["CNC_1", "part_0"], operator="High_Precision_CNC_Milling")
        jump = JumpInstr(b1)
        b0.add_instr(i1)
        b0.add_instr(jump)

        alloc = {"part_0": 0, "part_1": 1}
        sched = PostRAScheduler(self.tm)
        sched.schedule(cfg, alloc)

        self.assertIsInstance(b0.instructions[-1], JumpInstr, "Control instruction must stay at block end")

    def test_waw_dependency_respected(self):
        cfg = ControlFlowGraph()
        cfg.resource_names = {"CNC_1"}
        b0 = cfg.entry_block

        i1 = OpInstr("Process", "part_1", ["CNC_1", "part_0"], operator="High_Precision_CNC_Milling")
        i2 = OpInstr("Process", "part_1", ["CNC_1", "part_2"], operator="High_Precision_CNC_Milling")
        b0.add_instr(i1)
        b0.add_instr(i2)

        alloc = {"part_0": 0, "part_1": 1, "part_2": 0}
        sched = PostRAScheduler(self.tm)
        sched.schedule(cfg, alloc)

        idx1 = b0.instructions.index(i1)
        idx2 = b0.instructions.index(i2)
        self.assertLess(idx1, idx2, "WAW dependency must be respected")

if __name__ == "__main__":
    unittest.main()
