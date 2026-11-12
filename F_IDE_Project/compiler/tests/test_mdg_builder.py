import unittest

from compiler.ir import ControlFlowGraph, OpInstr
from compiler.mdg_builder import MDGBuilder

class TestMDGBuilder(unittest.TestCase):
    def test_empty_block_connects_entry_to_exit(self):
        cfg = ControlFlowGraph()
        empty_join = cfg.new_block("If_End")
        cfg.entry_block.add_successor(empty_join)

        mdg = MDGBuilder().build(cfg)
        entry = mdg.block_entry[empty_join.id]
        exit_node = mdg.block_exit[empty_join.id]

        matching = [
            edge
            for edge in entry.out_edges
            if edge.dst is exit_node
            and edge.edge_type == "struct"
            and edge.weight == 0.0
        ]
        self.assertEqual(len(matching), 1)
        self.assertIn(matching[0], exit_node.in_edges)

    def test_path_specific_phi_copies_all_reach_merge_consumer(self):
        cfg = ControlFlowGraph()
        true_block = cfg.new_block("If_True")
        false_block = cfg.new_block("If_False")
        merge_block = cfg.new_block("If_End")

        cfg.entry_block.add_successor(true_block)
        cfg.entry_block.add_successor(false_block)
        true_block.add_successor(merge_block)
        false_block.add_successor(merge_block)

        true_copy = OpInstr("Assign", "board_3", ["board_1"])
        false_copy = OpInstr("Assign", "board_3", ["board_2"])
        consumer = OpInstr("Store", None, ["board_3"])
        true_block.add_instr(true_copy)
        false_block.add_instr(false_copy)
        merge_block.add_instr(consumer)

        mdg = MDGBuilder().build(cfg)
        true_node = mdg.get_node_for_instr(true_copy)
        false_node = mdg.get_node_for_instr(false_copy)
        consumer_node = mdg.get_node_for_instr(consumer)

        incoming_sources = {
            edge.src
            for edge in consumer_node.in_edges
            if edge.edge_type == "data"
        }
        self.assertEqual(incoming_sources, {true_node, false_node})
        self.assertTrue(any(edge.dst is consumer_node for edge in true_node.out_edges))
        self.assertTrue(any(edge.dst is consumer_node for edge in false_node.out_edges))

if __name__ == "__main__":
    unittest.main()
