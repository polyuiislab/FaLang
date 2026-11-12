import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import unittest
from compiler.ir import ControlFlowGraph, OpInstr, BranchInstr, JumpInstr
from compiler.codegen import CodeGenerator
from core.target_model import TargetModelLoader

class TestCodeGenerator(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tm = TargetModelLoader()
        ok, msg = cls.tm.load()
        if not ok:
            raise RuntimeError(f"Failed to load target model: {msg}")
        cls.gen = CodeGenerator(cls.tm)

    def _hex_to_bits(self, hex_str: str) -> str:
        return f"{int(hex_str, 16):032b}"

    def test_process_r_type(self):
        cfg = ControlFlowGraph()
        b0 = cfg.entry_block
        b0.add_instr(OpInstr("Process", "part_1", ["CNC_1", "part_0"], operator="High_Precision_CNC_Milling"))

        alloc = {"part_0": 0, "part_1": 1}
        codes = self.gen.generate(cfg, alloc)
        self.assertEqual(len(codes), 1)

        bits = self._hex_to_bits(codes[0])
        self.assertEqual(bits[0:6], "000001")  # OP
        self.assertEqual(bits[6:11], "00000")  # RS = part_0
        self.assertEqual(bits[11:16], "00000")  # RT: no second material
        self.assertEqual(bits[16:21], "00000")  # RP
        self.assertEqual(bits[21:26], "00001")  # RD = part_1
        self.assertEqual(bits[26:29], "001")  # func = Process
        self.assertEqual(bits[29:32], "000")  # operator = PALU 0

    def test_move_r_type(self):
        cfg = ControlFlowGraph()
        b0 = cfg.entry_block
        b0.add_instr(OpInstr("Move", None, ["part_1", "2"]))

        alloc = {"part_1": 1}
        codes = self.gen.generate(cfg, alloc)
        self.assertEqual(len(codes), 1)

        bits = self._hex_to_bits(codes[0])
        self.assertEqual(bits[0:6], "000001")  # OP
        self.assertEqual(bits[6:11], "00001")  # RS = part_1
        self.assertEqual(bits[11:16], "00010")  # RT = destination register
        self.assertEqual(bits[16:21], "00000")  # RP
        self.assertEqual(bits[21:26], "00000")  # RD: Move has no target variable
        self.assertEqual(bits[26:29], "010")  # func = Move
        self.assertEqual(bits[29:32], "010")  # operator = PALU 2 (AGV)

    def test_inspect_palu_id(self):
        cfg = ControlFlowGraph()
        b0 = cfg.entry_block
        b0.add_instr(OpInstr("Inspect", "part_2", ["Camera_1", "part_1"], operator="Visual_Inspection"))

        # WS1 contains registers 2 and 21.
        alloc = {"part_1": 2, "part_2": 2}
        codes = self.gen.generate(cfg, alloc)
        self.assertEqual(len(codes), 1)

        bits = self._hex_to_bits(codes[0])
        self.assertEqual(bits[0:6], "000001")
        self.assertEqual(bits[6:11], "00010")  # RS
        self.assertEqual(bits[21:26], "00010")  # RD
        self.assertEqual(bits[26:29], "011")  # func = Inspect
        self.assertEqual(bits[29:32], "001")  # operator = PALU 1

    def test_adjust_uses_palu0(self):
        cfg = ControlFlowGraph()
        b0 = cfg.entry_block
        b0.add_instr(OpInstr("Adjust", "part_1", ["CNC_1", "part_0"], operator="Emergency_Unlock_Fixture"))

        # WS0 contains registers 0, 1, and 20.
        alloc = {"part_0": 20, "part_1": 20}
        codes = self.gen.generate(cfg, alloc)
        self.assertEqual(len(codes), 1)

        bits = self._hex_to_bits(codes[0])
        self.assertEqual(bits[26:29], "100")  # func = Adjust
        self.assertEqual(bits[29:32], "000")  # operator = PALU 0

    def test_assign_same_encoding_as_move(self):
        cfg = ControlFlowGraph()
        b0 = cfg.entry_block
        b0.add_instr(OpInstr("Assign", "part_2", ["part_1"]))

        # Register 21 belongs to WS1.
        alloc = {"part_1": 21, "part_2": 21}
        codes = self.gen.generate(cfg, alloc)
        self.assertEqual(len(codes), 1)

        bits = self._hex_to_bits(codes[0])
        self.assertEqual(bits[26:29], "010")  # func = Move/Assign
        self.assertEqual(bits[29:32], "010")  # operator = PALU 2

    def test_load_i_type(self):
        cfg = ControlFlowGraph()
        b0 = cfg.entry_block
        b0.add_instr(OpInstr("Load", None, ["part_0", "20"]))

        alloc = {"part_0": "memory"}
        codes = self.gen.generate(cfg, alloc)
        self.assertEqual(len(codes), 1)

        bits = self._hex_to_bits(codes[0])
        self.assertEqual(bits[0:6], "100101")  # OP = Load
        self.assertEqual(bits[6:11], "00000")  # RS: base address
        self.assertEqual(bits[11:16], "10100")  # RT
        self.assertEqual(bits[16:21], "00000")  # RP
        self.assertEqual(bits[21:29], "00000000")  # immediate
        self.assertEqual(bits[29:32], "010")  # operator = PALU 2

    def test_store_i_type(self):
        cfg = ControlFlowGraph()
        b0 = cfg.entry_block
        b0.add_instr(OpInstr("Store", None, ["part_0", "21"]))

        alloc = {"part_0": "memory"}
        codes = self.gen.generate(cfg, alloc)
        self.assertEqual(len(codes), 1)

        bits = self._hex_to_bits(codes[0])
        self.assertEqual(bits[0:6], "101101")  # OP = Store
        self.assertEqual(bits[6:11], "10101")  # RS
        self.assertEqual(bits[11:16], "00000")  # RT
        self.assertEqual(bits[16:21], "00000")  # RP
        self.assertEqual(bits[21:29], "00000000")  # immediate
        self.assertEqual(bits[29:32], "010")  # operator = PALU 2

    def test_jump_mips_format(self):
        cfg = ControlFlowGraph()
        b0 = cfg.entry_block
        b1 = cfg.new_block("Target")
        b0.add_instr(JumpInstr(b1))

        codes = self.gen.generate(cfg, allocation={})
        self.assertEqual(len(codes), 1)

        bits = self._hex_to_bits(codes[0])
        self.assertEqual(bits[0:6], "000010")  # OP = J
        # The low 26 bits contain the target block ID.
        target_id = b1.id & 0x3FFFFFF
        self.assertEqual(int(bits[6:], 2), target_id)

    def test_branch_mips_format(self):
        cfg = ControlFlowGraph()
        b0 = cfg.entry_block
        b1 = cfg.new_block("True")
        b2 = cfg.new_block("False")
        b0.add_instr(BranchInstr("cond", b1, b2))

        codes = self.gen.generate(cfg, allocation={})
        self.assertEqual(len(codes), 1)

        bits = self._hex_to_bits(codes[0])
        self.assertEqual(bits[0:6], "000100")  # OP = beq
        # RT = zero register
        self.assertEqual(bits[11:16], "00000")

    def test_uses_assigned_palu_id_from_allocator(self):
        cfg = ControlFlowGraph()
        b0 = cfg.entry_block
        instr = OpInstr("Process", "part_1", ["CNC_1", "part_0"], operator="High_Precision_CNC_Milling")
        instr.assigned_palu_id = 7
        b0.add_instr(instr)

        alloc = {"part_0": 0, "part_1": 1}
        codes = self.gen.generate(cfg, alloc)
        self.assertEqual(len(codes), 1)

        bits = self._hex_to_bits(codes[0])
        self.assertEqual(bits[29:32], "111")

    def test_multiple_blocks(self):
        cfg = ControlFlowGraph()
        b0 = cfg.entry_block
        b1 = cfg.new_block("B1")

        b0.add_instr(OpInstr("Process", "part_1", ["CNC_1", "part_0"], operator="High_Precision_CNC_Milling"))
        b0.add_instr(JumpInstr(b1))
        b1.add_instr(OpInstr("Inspect", "part_2", ["Camera_1", "part_1"], operator="Visual_Inspection"))

        alloc = {"part_0": 0, "part_1": 1, "part_2": 2}
        codes = self.gen.generate(cfg, alloc)
        self.assertEqual(len(codes), 3)

        bits0 = self._hex_to_bits(codes[0])
        self.assertEqual(bits0[0:6], "000001")
        self.assertEqual(bits0[26:29], "001")

        bits1 = self._hex_to_bits(codes[1])
        self.assertEqual(bits1[0:6], "000010")

        bits2 = self._hex_to_bits(codes[2])
        self.assertEqual(bits2[0:6], "000001")
        self.assertEqual(bits2[26:29], "011")

    def test_exception_table_records_catch_handler_address(self):
        cfg = ControlFlowGraph()
        trigger = OpInstr("Process", "part_1", ["CNC_1", "part_0"], rp="00001")
        catch_block = cfg.new_block("Catch_ResourceFault")
        catch_instr = OpInstr("Adjust", "part_2", ["CNC_1", "part_0"], rp="00001")
        catch_block.add_instr(catch_instr)
        trigger.catch_edges.append(catch_block)
        cfg.entry_block.add_instr(trigger)

        codes = self.gen.generate(cfg, {"part_0": 0, "part_1": 1, "part_2": 1})

        self.assertEqual(len(codes), 2)
        self.assertEqual(trigger.exception_handler_address, 1)
        self.assertEqual(self.gen.exception_table[0]["handler_address"], 1)

    def test_segmented_coe_image_contains_text_exctab_meta(self):
        cfg = ControlFlowGraph()
        trigger = OpInstr("Process", "part_1", ["CNC_1", "part_0"], rp="00001")
        catch_block = cfg.new_block("Catch_ResourceFault")
        catch_block.add_instr(OpInstr("Adjust", "part_2", ["CNC_1", "part_0"], rp="00001"))
        trigger.catch_edges.append(catch_block)
        cfg.entry_block.add_instr(trigger)

        text = self.gen.generate(cfg, {"part_0": 0, "part_1": 1, "part_2": 1})
        image = self.gen.coe_image

        self.assertEqual(image["format"], "FISA_SEGMENTED_COE_V1")
        self.assertEqual(image["flat"][0], "F15AC0E1")
        self.assertEqual(image["text"], text)
        self.assertEqual(image["exctab"][0], "F15AEC7B")
        self.assertEqual(image["exctab"][1], "00000001")
        self.assertEqual(image["meta"][0], "F15A4E7A")
        self.assertEqual(image["manifest"]["entries"][0]["handler_address"], 1)

if __name__ == "__main__":
    unittest.main()
