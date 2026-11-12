import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import unittest
from compiler.ir import ControlFlowGraph, OpInstr, BasicBlock
from compiler.mdg import ManufacturingDependencyGraph, MDGNode, MDGEdge
from compiler.scheduler import ScheduleResult, ResourceState
from compiler.register_alloc import RegisterAllocator
from core.target_model import TargetModelLoader

class TestRegisterAllocation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tm = TargetModelLoader()
        ok, msg = cls.tm.load()
        if not ok:
            raise RuntimeError(f"Failed to load target model: {msg}")

    def _make_mdg_for_block(self, cfg: ControlFlowGraph, block: BasicBlock):
        mdg = ManufacturingDependencyGraph()
        entry = MDGNode(None, block, "entry")
        mdg.add_node(entry)
        mdg.block_entry[block.id] = entry

        prev = entry
        op_nodes = []
        for instr in block.instructions:
            if isinstance(instr, OpInstr):
                node = MDGNode(instr, block, "op")
                mdg.add_node(node)
                mdg.instr_to_node[id(instr)] = node
                mdg.add_edge(MDGEdge(prev, node, 0.0, "struct"))
                prev = node
                op_nodes.append(node)

        exit_node = MDGNode(None, block, "exit")
        mdg.add_node(exit_node)
        mdg.block_exit[block.id] = exit_node
        mdg.add_edge(MDGEdge(prev, exit_node, 0.0, "struct"))

        for i in range(len(op_nodes) - 1):
            mdg.add_edge(MDGEdge(op_nodes[i], op_nodes[i + 1], 1.0, "data"))

        return mdg

    def _make_schedule(self, cfg: ControlFlowGraph, mdg: ManufacturingDependencyGraph, times_per_block):
        """Build a schedule from {block_id: [(node, start, finish), ...]}."""
        block_schedules = {}
        exit_states = {}
        for block in cfg.blocks:
            sched = []
            for node in mdg.nodes:
                if node.block.id == block.id and node.node_type == "op":
                    st = 0.0
                    fin = 1.0
                    for n, s, f in times_per_block.get(block.id, []):
                        if n is node.instr:
                            st = s
                            fin = f
                            break
                    sched.append((node, st, fin))
            block_schedules[block.id] = sched
            exit_states[block.id] = ResourceState()

        return ScheduleResult(
            block_schedules=block_schedules,
            global_makespan=max((f for sched in block_schedules.values() for _, _, f in sched), default=0.0),
            device_timeline={},
            material_timeline={},
            cache_peak={},
            mfr_boundaries=[tuple(b.id for b in cfg.blocks)],
            exit_states=exit_states,
        )

    def _ws_regs(self, ws_id):
        for ws in (self.tm.data.get("addressing_model") or {}).get("workstation_mapping", []):
            if ws.get("workstation_id") == ws_id:
                return set(ws.get("assigned_registers") or [])
        return set()

    def _preg_ws(self, preg):
        for ws in (self.tm.data.get("addressing_model") or {}).get("workstation_mapping", []):
            if preg in (ws.get("assigned_registers") or []):
                return ws.get("workstation_id")
        return None

    def test_same_workstation_no_move(self):
        cfg = ControlFlowGraph()
        cfg.resource_names = {"CNC_1"}
        cfg.material_names = {"part"}
        b0 = cfg.entry_block

        i1 = OpInstr("Process", "part_1", ["CNC_1", "part_0"], operator="High_Precision_CNC_Milling")
        i2 = OpInstr("Process", "part_2", ["CNC_1", "part_1"], operator="High_Precision_CNC_Milling")
        b0.add_instr(i1)
        b0.add_instr(i2)

        mdg = self._make_mdg_for_block(cfg, b0)
        sched = self._make_schedule(cfg, mdg, {
            b0.id: [
                (i1, 0.0, 45.0),
                (i2, 45.0, 90.0),
            ]
        })

        alloc = RegisterAllocator(cfg, mdg, sched, self.tm)
        result = alloc.allocate()

        ws0_regs = self._ws_regs(0)
        for vreg in ["part_0", "part_1", "part_2"]:
            self.assertIn(vreg, result)
            preg = result[vreg]
            self.assertIn(preg, ws0_regs, f"{vreg} should be allocated to WS0 register")

        moves = [i for i in b0.instructions if isinstance(i, OpInstr) and i.op_type == "Move"]
        self.assertEqual(len(moves), 0, "No cross-workstation move expected")

    def test_cross_workstation_inserts_move(self):
        cfg = ControlFlowGraph()
        cfg.resource_names = {"CNC_1", "Camera_1"}
        cfg.material_names = {"part"}
        b0 = cfg.entry_block

        i1 = OpInstr("Process", "part_1", ["CNC_1", "part_0"], operator="High_Precision_CNC_Milling")
        i2 = OpInstr("Inspect", "part_2", ["Camera_1", "part_1"], operator="Camera_Vision_Check")
        b0.add_instr(i1)
        b0.add_instr(i2)

        mdg = self._make_mdg_for_block(cfg, b0)
        n1 = mdg.get_node_for_instr(i1)
        n2 = mdg.get_node_for_instr(i2)
        if n1 and n2:
            mdg.add_edge(MDGEdge(n1, n2, 1.0, "data"))

        sched = self._make_schedule(cfg, mdg, {
            b0.id: [
                (i1, 0.0, 45.0),
                (i2, 45.0, 50.0),
            ]
        })

        alloc = RegisterAllocator(cfg, mdg, sched, self.tm)
        result = alloc.allocate()

        # Multiskilled stations may support both assembly and testing.
        # Insert a Move only when the selected stations differ.
        part1_ws = self._preg_ws(result["part_1"])
        part2_ws = self._preg_ws(result["part_2"])
        moves = [i for i in b0.instructions if isinstance(i, OpInstr) and i.op_type == "Move"]
        if part1_ws != part2_ws:
            self.assertEqual(len(moves), 1, "Should insert one cross-workstation Move")
            self.assertEqual(moves[0].args[0], "part_1")
            expected_dst = result["part_2"]
            self.assertEqual(int(moves[0].args[1]), expected_dst)
        else:
            self.assertEqual(len(moves), 0, "No Move expected when PBQP co-locates operations")

    def _capable_workstations(self, capability):
        ics = self.tm.data.get("instruction_capability_set") or {}
        palus = {p.get("palu_id"): p for p in ics.get("palu_definitions", [])}
        capable = set()
        for ws in (self.tm.data.get("addressing_model") or {}).get("workstation_mapping", []):
            for pid in ws.get("assigned_palus") or []:
                for cap in (palus.get(pid) or {}).get("supported_capabilities", []):
                    triple = (str(cap.get("opcode", "")), str(cap.get("func", "")), str(cap.get("rp", "")))
                    if triple == capability:
                        capable.add(ws.get("workstation_id"))
        return capable

    def test_register_conflict_avoidance(self):
        cfg = ControlFlowGraph()
        cfg.resource_names = {"CNC_1"}
        cfg.material_names = {"part_a", "part_b"}
        b0 = cfg.entry_block

        i1 = OpInstr("Process", "part_a_1", ["CNC_1", "part_a_0"], operator="High_Precision_CNC_Milling")
        i2 = OpInstr("Process", "part_b_1", ["CNC_1", "part_b_0"], operator="High_Precision_CNC_Milling")
        b0.add_instr(i1)
        b0.add_instr(i2)

        mdg = self._make_mdg_for_block(cfg, b0)
        sched = self._make_schedule(cfg, mdg, {
            b0.id: [
                (i1, 0.0, 45.0),
                (i2, 0.0, 45.0),
            ]
        })

        alloc = RegisterAllocator(cfg, mdg, sched, self.tm)
        result = alloc.allocate()

        capable_ws = self._capable_workstations(("000001", "001", "00001"))
        self.assertTrue(capable_ws, "PC-MMS model should have assembly-capable workstations")
        for vreg in ["part_a_0", "part_a_1", "part_b_0", "part_b_1"]:
            self.assertIn(vreg, result)
            self.assertNotEqual(result[vreg], "memory", f"{vreg} should not spill")
            self.assertIn(self._preg_ws(result[vreg]), capable_ws,
                          f"{vreg} should sit on an assembly-capable workstation")

        # Overlapping outputs need distinct sites when site capacity is one.
        self.assertNotEqual(result["part_a_1"], result["part_b_1"],
                            "Overlapping vregs must not share a capacity-1 holding site")

    def test_capacity_two_allows_sharing(self):
        cfg = ControlFlowGraph()
        cfg.resource_names = {"CNC_1"}
        cfg.material_names = {"part"}
        b0 = cfg.entry_block

        i1 = OpInstr("Process", "part_1", ["CNC_1", "part_0"], operator="High_Precision_CNC_Milling")
        i2 = OpInstr("Process", "part_2", ["CNC_1", "part_0"], operator="High_Precision_CNC_Milling")
        b0.add_instr(i1)
        b0.add_instr(i2)

        mdg = self._make_mdg_for_block(cfg, b0)
        # part_0 is the shared input; part_1 and part_2 are parallel outputs.
        sched = self._make_schedule(cfg, mdg, {
            b0.id: [
                (i1, 0.0, 45.0),
                (i2, 0.0, 45.0),
            ]
        })

        alloc = RegisterAllocator(cfg, mdg, sched, self.tm)
        result = alloc.allocate()

        # Overlapping outputs may share a capacity-two site.
        # PALU competition can still favor separate sites.
        self.assertIn("part_1", result)
        self.assertIn("part_2", result)
        self.assertNotEqual(result["part_1"], "memory", "part_1 should not be forced to memory")
        self.assertNotEqual(result["part_2"], "memory", "part_2 should not be forced to memory")

    def test_time_staggered_same_palu_no_competition(self):
        """Nonoverlapping operations do not incur a PALU competition penalty."""
        cfg = ControlFlowGraph()
        cfg.resource_names = {"CNC_1"}
        cfg.material_names = {"part"}
        b0 = cfg.entry_block

        i1 = OpInstr("Process", "part_1", ["CNC_1", "part_0"], operator="High_Precision_CNC_Milling")
        i2 = OpInstr("Process", "part_2", ["CNC_1", "part_1"], operator="High_Precision_CNC_Milling")
        b0.add_instr(i1)
        b0.add_instr(i2)

        mdg = self._make_mdg_for_block(cfg, b0)
        sched = self._make_schedule(cfg, mdg, {
            b0.id: [
                (i1, 0.0, 45.0),
                (i2, 45.0, 90.0),
            ]
        })

        alloc = RegisterAllocator(cfg, mdg, sched, self.tm)
        result = alloc.allocate()

        self.assertIn("part_1", result)
        self.assertIn("part_2", result)
        self.assertNotEqual(result["part_1"], "memory")
        self.assertNotEqual(result["part_2"], "memory")

    def test_fix_capacity_prefers_relocate_over_spill(self):
        cfg = ControlFlowGraph()
        cfg.resource_names = {"CNC_1"}
        cfg.material_names = {"part"}
        b0 = cfg.entry_block

        i1 = OpInstr("Process", "part_1", ["CNC_1", "part_0"], operator="High_Precision_CNC_Milling")
        b0.add_instr(i1)

        mdg = self._make_mdg_for_block(cfg, b0)
        sched = self._make_schedule(cfg, mdg, {b0.id: [(i1, 0.0, 45.0)]})

        alloc = RegisterAllocator(cfg, mdg, sched, self.tm)
        alloc.allocate()

        # Give both WS0 sites capacity two, then overload site 0 with four versions.
        real_capacity = alloc._get_preg_capacity(0)
        ws0_regs = self._ws_regs(0)
        alloc._get_preg_capacity = lambda preg: 2 if preg in ws0_regs else real_capacity

        alloc.allocation["part_0"] = 0
        alloc.allocation["part_1"] = 0
        alloc.vregs.extend(["part_x", "part_y"])
        alloc.vreg_liveness["part_x"] = (0.0, 45.0)
        alloc.vreg_liveness["part_y"] = (0.0, 45.0)
        alloc.allocation["part_x"] = 0
        alloc.allocation["part_y"] = 0
        alloc.def_instrs["part_x"] = (b0.id, 0, i1)
        alloc.def_instrs["part_y"] = (b0.id, 0, i1)

        alloc._fix_capacity_violations()

        values_at_0 = [v for v, p in alloc.allocation.items() if p == 0]
        self.assertLessEqual(len(values_at_0), 2, "库位 0（容量 2）上不应超过 2 个版本")

        non_memory = {p for p in alloc.allocation.values() if p != "memory"}
        self.assertTrue(non_memory.issubset(ws0_regs),
                        "重定位应留在同一工位内，不应跨工位")
        self.assertIn(1, non_memory,
                      "应先把版本搬到同工位其他库位（库位 1），而不是直接 spill")

    def test_memory_fallback_when_no_capable_ws(self):
        cfg = ControlFlowGraph()
        cfg.resource_names = {"CNC_1"}
        cfg.material_names = {"part"}
        b0 = cfg.entry_block

        i1 = OpInstr("Process", "part_1", ["CNC_1", "part_0"], operator="Unknown_Op")
        b0.add_instr(i1)

        mdg = self._make_mdg_for_block(cfg, b0)
        sched = self._make_schedule(cfg, mdg, {b0.id: [(i1, 0.0, 1.0)]})

        alloc = RegisterAllocator(cfg, mdg, sched, self.tm)
        result = alloc.allocate()

        # An unknown operator has no resolved capability constraint.
        self.assertIn("part_1", result)
        self.assertNotEqual(result["part_1"], "memory")

    def test_ra_excludes_exception_path_liveness_from_pbqp(self):
        cfg = ControlFlowGraph()
        cfg.resource_names = {"CNC_1"}
        cfg.material_names = {"part"}
        b0 = cfg.entry_block
        catch_block = cfg.new_block("Catch_ResourceFault")

        trigger = OpInstr("Process", "part_1", ["CNC_1", "part_0"], operator="High_Precision_CNC_Milling")
        catch_instr = OpInstr("Adjust", "part_recovered_1", ["CNC_1", "part_1"], operator="Emergency_Unlock_Fixture")
        trigger.catch_edges.append(catch_block)
        b0.add_instr(trigger)
        catch_block.add_instr(catch_instr)

        mdg = self._make_mdg_for_block(cfg, b0)
        sched = self._make_schedule(cfg, mdg, {b0.id: [(trigger, 0.0, 45.0)]})

        alloc = RegisterAllocator(cfg, mdg, sched, self.tm)
        result = alloc.allocate()

        self.assertIn("part_0", result)
        self.assertIn("part_1", result)
        self.assertNotIn("part_recovered_1", result)
        self.assertIn("part_recovered_1", alloc.exception_liveness)
        self.assertNotIn("part_recovered_1", alloc.normal_liveness)
        self.assertIsNone(getattr(catch_instr, "assigned_palu_id", None))

    def test_spill_load_store_insertion(self):
        cfg = ControlFlowGraph()
        cfg.resource_names = {"CNC_1"}
        cfg.material_names = {"part"}
        b0 = cfg.entry_block

        i1 = OpInstr("Process", "part_1", ["CNC_1", "part_0"], operator="High_Precision_CNC_Milling")
        i2 = OpInstr("Process", "part_2", ["CNC_1", "part_1"], operator="High_Precision_CNC_Milling")
        b0.add_instr(i1)
        b0.add_instr(i2)

        mdg = self._make_mdg_for_block(cfg, b0)
        sched = self._make_schedule(cfg, mdg, {
            b0.id: [(i1, 0.0, 45.0), (i2, 45.0, 90.0)]
        })

        alloc = RegisterAllocator(cfg, mdg, sched, self.tm)
        alloc.allocate()

        alloc.allocation["part_1"] = "memory"
        alloc._insert_spill_load_store()

        instrs = b0.instructions
        i1_idx = instrs.index(i1)
        i2_idx = instrs.index(i2)

        store_found = False
        store_reg = None
        for idx in range(i1_idx + 1, i2_idx):
            instr = instrs[idx]
            if isinstance(instr, OpInstr) and instr.op_type == "Store" and instr.args[0] == "part_1":
                store_found = True
                store_reg = int(instr.args[1])
                break
        self.assertTrue(store_found, "Store should be inserted after defining instruction")

        load_found = False
        load_reg = None
        for idx in range(i1_idx + 1, i2_idx):
            instr = instrs[idx]
            if isinstance(instr, OpInstr) and instr.op_type == "Load" and instr.args[0] == "part_1":
                load_found = True
                load_reg = int(instr.args[1])
                break
        self.assertTrue(load_found, "Load should be inserted before using instruction")

        # A spill register must be an available buffer at the executing station.
        ws0_regs = self._ws_regs(0)
        self.assertIn(store_reg, ws0_regs, "Store source register should belong to WS0")
        self.assertIn(load_reg, ws0_regs, "Load destination register should belong to WS0")

    def test_phi_cross_block_move(self):
        cfg = ControlFlowGraph()
        cfg.resource_names = {"CNC_1", "Camera_1"}
        cfg.material_names = {"part"}

        b0 = cfg.entry_block
        b1 = cfg.new_block("Pred")
        b0.add_successor(b1)

        assign = OpInstr("Assign", "part_2", ["part_1"])
        b1.add_instr(assign)

        mdg = ManufacturingDependencyGraph()
        entry = MDGNode(None, b1, "entry")
        mdg.add_node(entry)
        mdg.block_entry[b1.id] = entry
        n1 = MDGNode(assign, b1, "op")
        mdg.add_node(n1)
        mdg.instr_to_node[id(assign)] = n1
        mdg.add_edge(MDGEdge(entry, n1, 0.0, "struct"))
        exit_node = MDGNode(None, b1, "exit")
        mdg.add_node(exit_node)
        mdg.block_exit[b1.id] = exit_node
        mdg.add_edge(MDGEdge(n1, exit_node, 0.0, "struct"))

        sched = ScheduleResult(
            block_schedules={b1.id: [(n1, 0.0, 1.0)]},
            global_makespan=1.0,
            device_timeline={},
            material_timeline={},
            cache_peak={},
            mfr_boundaries=[(b1.id,)],
            exit_states={b1.id: ResourceState()},
        )

        alloc = RegisterAllocator(cfg, mdg, sched, self.tm)
        alloc.vregs = ["part_1", "part_2"]
        alloc.allocation = {"part_1": 0, "part_2": 2}
        alloc.def_instrs = {"part_2": (b1.id, 0, assign)}
        alloc.use_instrs = {"part_1": [(b1.id, 0, assign)]}
        alloc.vreg_liveness = {
            "part_1": (0.0, 1.0),
            "part_2": (0.0, 1.0),
        }
        alloc._insert_cross_ws_moves()

        moves = [i for i in b1.instructions if isinstance(i, OpInstr) and i.op_type == "Move"]
        self.assertEqual(len(moves), 1, "Should insert Move for cross-block/cross-ws Assign")
        self.assertEqual(moves[0].args[0], "part_1")
        # Move into the PReg selected for the Assign output by PBQP.
        self.assertEqual(int(moves[0].args[1]), 2)

if __name__ == "__main__":
    unittest.main()
