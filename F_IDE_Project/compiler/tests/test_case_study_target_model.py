import os
import sys
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from compiler.ir import ControlFlowGraph, OpInstr
from compiler.mdg_builder import MDGBuilder
from core.target_model import TargetModelLoader

class TestCaseStudyTargetModel(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tm = TargetModelLoader()
        ok, msg = cls.tm.load()
        if not ok:
            raise RuntimeError(f"Failed to load target model: {msg}")

    def test_pc_mms_model_loads_with_32_physical_registers(self):
        regions = self.tm.get_storage_regions()
        self.assertEqual(len(regions["register_ids"]), 32)
        self.assertEqual(regions["memory_ids"], [100])

    def test_logistics_matrix_generated_from_mms_layout(self):
        self.assertEqual(self.tm.get_logistics_time(0, 1), 1.0)
        self.assertEqual(self.tm.get_logistics_time(100, 0), 12.0)
        self.assertEqual(self.tm.get_logistics_time(0, 4), 8.0)

    def test_legacy_aliases_still_resolve(self):
        self.assertEqual(
            self.tm.resolve_rp_code("000001", "001", "High_Precision_CNC_Milling"),
            "00001",
        )
        self.assertEqual(
            self.tm.resolve_rp_code("000001", "011", "Visual_Inspection"),
            "00001",
        )

    def test_setup_time_is_added_to_mdg_data_edge_weight(self):
        cfg = ControlFlowGraph()
        cfg.resource_names = {"Assembler_1"}
        cfg.material_names = {"pc"}
        block = cfg.entry_block
        first = OpInstr(
            "Process",
            "pc_1",
            ["Assembler_1", "pc_0"],
            params="CPU_Install;component=CPU;model=CPU_1",
            opcode="000001",
            func="001",
            rp="00010",
        )
        second = OpInstr(
            "Process",
            "pc_2",
            ["Assembler_1", "pc_1"],
            params="CPU_Install;component=CPU;model=CPU_3",
            opcode="000001",
            func="001",
            rp="00010",
        )
        block.add_instr(first)
        block.add_instr(second)

        mdg = MDGBuilder(self.tm).build(cfg)
        src = mdg.get_node_for_instr(first)
        dst = mdg.get_node_for_instr(second)
        data_edges = [edge for edge in src.out_edges if edge.dst is dst and edge.edge_type == "data"]

        self.assertEqual(len(data_edges), 1)

        # Edge weight: processing time 8 plus CPU_1 -> CPU_3 setup 6 * 2.
        processing = self.tm.get_expected_time("000001", "001", "00010")
        setup = self.tm.get_setup_time(first.params, second.params)
        self.assertEqual(processing, 8.0)
        self.assertEqual(setup, 12.0)
        self.assertEqual(data_edges[0].weight, processing + setup)

if __name__ == "__main__":
    unittest.main()
