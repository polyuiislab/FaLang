import os
import sys
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from case_study.order_generator import PCOrderGenerator
from case_study.program_generator import PCOrderProgramGenerator
from compiler.factory_target_machine import (
    AVAIL_LOGISTICS,
    AVAIL_WORKSTATION,
    FactoryState,
    FactoryTargetMachine,
)
from compiler.ir import ControlFlowGraph, OpInstr
from compiler.residual_coloring import ResidualGraphColorer
from compiler.residual_ir import (
    CompiledOrderProgram,
    ResidualRecord,
    WorksetComposer,
)
from compiler.target_lowering import TargetLowering, TargetLoweringError
from compiler.vmrs import (
    VirtualResourceError,
    VirtualResourceKind,
    VirtualResourceSpace,
)
from core.compiler_proxy import CompilerProxy
from core.target_model import TargetModelLoader

PC_ORDER_LIBRARY_SOURCE = """
Resource Assembly_PM: Device<Process, Adjust>;
Buffer IntermediateBuffer: Memory<> <Capacity=8>;
Storage Warehouse: Memory<>;
Material PC[1];

Task Main(Material pc) {
    Load(pc, "Warehouse_To_PM_Buffer");
    Process(Assembly_PM, pc, "Base_Assembly;component=Motherboard;model=MB_1");
    Process(Assembly_PM, pc, "CPU_Install;component=CPU;model=CPU_1");
    Store(pc, "PM_Buffer_To_Warehouse");
    return pc;
}
"""

class TestVMRS(unittest.TestCase):
    def _parse(self, source):
        from compiler.lexer import Lexer
        from compiler.parser import Parser

        return Parser(Lexer(source).tokenize()).parse()

    def test_roles_are_classified_by_declaration_keyword(self):
        ast = self._parse(PC_ORDER_LIBRARY_SOURCE)
        space = VirtualResourceSpace.from_ast(ast)

        self.assertIs(space.kind_of("Assembly_PM"), VirtualResourceKind.PRODUCTION)
        self.assertIs(space.kind_of("IntermediateBuffer"), VirtualResourceKind.HOLDING)
        self.assertIs(space.kind_of("Warehouse"), VirtualResourceKind.STORAGE)

        self.assertEqual([r.name for r in space.holding_roles()], ["IntermediateBuffer"])
        self.assertEqual([a.name for a in space.material_address_space()], ["IntermediateBuffer"])

    def test_physical_capacity_is_recorded_but_not_adopted(self):
        """Keep source capacity metadata separate from target capacity constraints."""
        ast = self._parse(PC_ORDER_LIBRARY_SOURCE)
        space = VirtualResourceSpace.from_ast(ast)

        self.assertEqual(space.physical_attributes, {"IntermediateBuffer": {"capacity": 8}})
        role = space.resources["IntermediateBuffer"]
        self.assertFalse(hasattr(role, "capacity"))
        self.assertEqual(len(space.holding_roles()), 1)

    def test_non_production_capability_on_storage_role_is_rejected(self):
        bad = "Storage Warehouse: Memory<Process>;\nMaterial M[1];\nTask Main(Material m) { return m; }"
        with self.assertRaises(VirtualResourceError):
            VirtualResourceSpace.from_ast(self._parse(bad))

class TestFactoryTargetMachine(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.loader = TargetModelLoader()
        ok, msg = cls.loader.load()
        if not ok:
            raise RuntimeError(msg)

    def test_free_capacity_subtracts_occupancy_and_reservation(self):
        f_t = FactoryTargetMachine(self.loader, FactoryState(occupied={0: 1}))
        self.assertEqual(f_t.capacity_of(0), 1)
        self.assertEqual(f_t.free_capacity(0), 0)

        f_t.reserve(0)
        self.assertEqual(f_t.free_capacity(0), -1, "预留之后再预留应体现为超额")
        f_t.release_reservations()
        self.assertEqual(f_t.free_capacity(0), 0)

    def test_availability_is_scoped_to_time(self):
        f_t = FactoryTargetMachine(self.loader, FactoryState(time=10.0))
        self.assertTrue(f_t.is_site_available(0))

        f_t.mark_unavailable(AVAIL_WORKSTATION, 0, until=25.0)
        self.assertFalse(f_t.is_site_available(0, t=10.0))
        self.assertTrue(f_t.is_site_available(0, t=25.0))

    def test_logistics_resource_pool_is_shared(self):
        f_t = FactoryTargetMachine(self.loader)
        logistics = f_t.logistics_resource()
        self.assertIsNotNone(logistics)
        self.assertEqual(logistics.quantity, 18)

        f_t.mark_unavailable(AVAIL_LOGISTICS, logistics.palu_id, until=40.0)
        self.assertFalse(f_t.is_site_available(0, t=10.0),
                         "物流资源不可用时任何位置都取不到料")
        self.assertTrue(f_t.is_site_available(0, t=40.0))

    def test_logistics_outage_allows_processing_at_the_current_station(self):
        f_t = FactoryTargetMachine(self.loader)
        f_t.mark_unavailable(AVAIL_LOGISTICS, f_t.logistics_resource().palu_id, until=40.0)
        self.assertTrue(f_t.is_site_available(0, t=10.0, source_site=0))
        other = next(s.storage_id for s in f_t.holding_sites()
                     if s.workstation_id != f_t.holding_site(0).workstation_id)
        self.assertFalse(f_t.is_site_available(other, t=10.0, source_site=0))
        self.assertFalse(f_t.is_site_available(0, t=10.0))

    def test_holding_sites_follow_workstation_mapping(self):
        f_t = FactoryTargetMachine(self.loader)
        self.assertEqual(len(f_t.holding_sites()), 32)
        for site in f_t.holding_sites():
            self.assertIn(site.storage_id, f_t.holding_sites_of_workstation(site.workstation_id))

class TestTargetLowering(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.proxy = CompilerProxy()
        cls.f_t = cls.proxy.build_factory_target_machine()

    def test_action_codes_are_filled_by_lowering_not_by_the_frontend(self):
        from compiler.lexer import Lexer
        from compiler.parser import Parser
        from compiler.ir_builder import IRBuilder
        from compiler.semantic import SemanticAnalyzer
        from compiler.vmrs import VirtualResourceSpace

        ast = Parser(Lexer(PC_ORDER_LIBRARY_SOURCE).tokenize()).parse()
        SemanticAnalyzer(target_model=None, logical_only=True).analyze(ast)
        cfg = IRBuilder(target_model=self.proxy.target_model, defer_target_codes=True).build(ast, "Main")

        physical = [i for b in cfg.blocks for i in b.instructions
                    if isinstance(i, OpInstr) and i.op_type in ("Process", "Load", "Store")]
        self.assertTrue(physical)
        for instr in physical:
            self.assertIsNone(instr.rp, "IR 必须是目标机无关的")
            self.assertIsNone(instr.opcode)

        lowering = TargetLowering(self.f_t, VirtualResourceSpace.from_ast(ast)).lower(cfg)
        for instr in physical:
            self.assertIsNotNone(instr.rp, f"{instr.op_type} 的动作编码应由 Target Lowering 填入")
            self.assertIsNotNone(instr.opcode)
            self.assertIsNotNone(instr.expected_time)

    def test_unknown_action_description_is_rejected_during_lowering(self):
        cfg = ControlFlowGraph()
        cfg.resource_names = {"Assembly_PM"}
        cfg.material_names = {"pc"}
        cfg.entry_block.add_instr(
            OpInstr("Process", "pc_1", ["Assembly_PM", "pc_0"], params="No_Such_Operation")
        )
        with self.assertRaises(TargetLoweringError):
            TargetLowering(self.f_t).lower(cfg)

    def test_eligible_sites_respect_producing_capability(self):
        from compiler.lexer import Lexer
        from compiler.parser import Parser
        from compiler.ir_builder import IRBuilder
        from compiler.semantic import SemanticAnalyzer
        from compiler.vmrs import VirtualResourceSpace

        ast = Parser(Lexer(PC_ORDER_LIBRARY_SOURCE).tokenize()).parse()
        SemanticAnalyzer(target_model=None, logical_only=True).analyze(ast)
        cfg = IRBuilder(target_model=self.proxy.target_model, defer_target_codes=True).build(ast, "Main")
        vmrs = VirtualResourceSpace.from_ast(ast)
        lowering = TargetLowering(self.f_t, vmrs).lower(cfg)

        # Packaging outputs can occupy only sites with packaging capability.
        packaging_ws = set(self.f_t.workstations_supporting(("000001", "001", "01001")))
        self.assertTrue(packaging_ws)
        for vreg, sites in lowering.eligible_sites.items():
            producers = [op for op in lowering.operations.values()
                         if vreg in (op.instr.target, *(op.instr.implicit_targets or []))]
            caps = [op.capability for op in producers if op.capability and not op.is_logistics]
            if ("000001", "001", "01001") not in caps:
                continue
            for site_id in sites:
                self.assertIn(self.f_t.holding_site(site_id).workstation_id, packaging_ws)

class TestResidualGraphColoring(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.proxy = CompilerProxy()
        cls.loader = cls.proxy.target_model

    def _compile(self, source):
        results = self.proxy.compile_order_program(source)
        self.assertTrue(results["success"], results["messages"])
        return results["artifacts"]

    def test_coloring_never_uses_warehouse_or_spill(self):
        artifacts = self._compile(PC_ORDER_LIBRARY_SOURCE)
        allocation = artifacts["alloc_map"]
        self.assertTrue(allocation)
        for vreg, site in allocation.items():
            self.assertIsInstance(site, int, f"{vreg} 必须落在物理持有位置上")
            self.assertIn(site, self.loader.get_storage_regions()["register_ids"])

        moves = [i for b in artifacts["cfg"].blocks for i in b.instructions
                 if isinstance(i, OpInstr) and i.op_type == "Store" and len(i.args) >= 2]
        self.assertEqual(moves, [], "不应出现分配器插入的 spill Store")

    def test_capacity_and_availability_bound_the_candidate_set(self):
        artifacts = self._compile(PC_ORDER_LIBRARY_SOURCE)
        f_t = artifacts["f_t"]
        lowering = artifacts["lowering"]
        cfg = artifacts["cfg"]

        state = FactoryState()
        for site in f_t.holding_sites():
            state.occupied[site.storage_id] = site.capacity
        colorer = ResidualGraphColorer(cfg, artifacts["mdg"], f_t, lowering, state=state)
        outcome = colorer.run(0.0)
        self.assertEqual(outcome.allocation, {}, "没有空闲容量时不应强行落位")
        self.assertTrue(outcome.waiting)

    def test_wait_then_retry_after_release(self):
        artifacts = self._compile(PC_ORDER_LIBRARY_SOURCE)
        f_t = artifacts["f_t"]
        lowering = artifacts["lowering"]
        cfg = artifacts["cfg"]

        state = FactoryState()
        for site in f_t.holding_sites():
            state.occupied[site.storage_id] = site.capacity
        colorer = ResidualGraphColorer(cfg, artifacts["mdg"], f_t, lowering, state=state)
        first = colorer.color(0.0)
        self.assertTrue(first.waiting)
        waiting_before = set(first.waiting)

        state.occupied.clear()
        second = colorer.retry(5.0)
        self.assertTrue(second.colored)
        self.assertLess(len(colorer.waiting), len(waiting_before),
                        "释放之后应当有着色成功的版本")

    def test_coloring_is_deterministic(self):
        first = self._compile(PC_ORDER_LIBRARY_SOURCE)["alloc_map"]
        second = self._compile(PC_ORDER_LIBRARY_SOURCE)["alloc_map"]
        self.assertEqual(first, second)

    def test_next_version_stays_with_its_material_when_capabilities_match(self):
        artifacts = self._compile(PC_ORDER_LIBRARY_SOURCE)
        lowering = artifacts["lowering"]
        vreg = next(v for v, sites in lowering.eligible_sites.items()
                    if len({artifacts["f_t"].holding_site(s).workstation_id for s in sites}) > 1)
        sites = lowering.eligible_sites[vreg]
        source = sites[-1]
        state = FactoryState(occupied={source: 1},
                             wip_location={vreg.rsplit("_", 1)[0] + "_held": source})
        colorer = ResidualGraphColorer(artifacts["cfg"], artifacts["mdg"],
                                      artifacts["f_t"], lowering, state=state, emit_moves=False)
        colorer._covering_cache[vreg] = set(sites)
        chosen = colorer._pick_site(vreg, sites)
        self.assertEqual(colorer.f_t.holding_site(chosen).workstation_id,
                         colorer.f_t.holding_site(source).workstation_id)

    def test_load_balance_sees_another_orders_interval_reservation(self):
        from compiler.residual_coloring import LoadBalanceSelector
        artifacts = self._compile(PC_ORDER_LIBRARY_SOURCE)
        colorer = ResidualGraphColorer(artifacts["cfg"], artifacts["mdg"],
                                      artifacts["f_t"], artifacts["lowering"],
                                      state=FactoryState(), selector=LoadBalanceSelector(),
                                      emit_moves=False)
        vreg = next(v for v, sites in artifacts["lowering"].eligible_sites.items() if len(sites) > 1)
        first, second = artifacts["lowering"].eligible_sites[vreg][:2]
        start, end = colorer._holding_span(vreg)
        colorer.f_t.reserve_interval(first, start, end)
        self.assertEqual(colorer._pick_site(vreg, (first, second)), second)
        colorer.f_t.release_reservations()
        colorer.f_t.reserve_interval(first, end, end + 10)
        self.assertEqual(colorer._pick_site(vreg, (first, second)), first)

    def test_same_station_versions_reuse_the_held_buffer_for_all_selectors(self):
        from compiler.residual_coloring import SITE_SELECTORS, make_site_selector
        artifacts = self._compile(PC_ORDER_LIBRARY_SOURCE)
        vreg = next(iter(artifacts["lowering"].eligible_sites))
        sites = artifacts["lowering"].eligible_sites[vreg]
        pair = next((a, b) for a in sites for b in sites if a < b and
                    artifacts["f_t"].holding_site(a).workstation_id ==
                    artifacts["f_t"].holding_site(b).workstation_id)
        state = FactoryState(occupied={pair[1]: 1},
                             wip_location={vreg.rsplit("_", 1)[0] + "_held": pair[1]})
        for selector in SITE_SELECTORS:
            colorer = ResidualGraphColorer(artifacts["cfg"], artifacts["mdg"],
                                          artifacts["f_t"], artifacts["lowering"],
                                          state=state.clone(), selector=make_site_selector(selector),
                                          emit_moves=False)
            self.assertEqual(colorer._pick_site(vreg, pair), pair[1], selector)

    def test_paper1_pipeline_does_not_run_pre_ra_pbqp_or_post_ra(self):
        artifacts = self._compile(PC_ORDER_LIBRARY_SOURCE)
        with open(os.path.join(ROOT, "core", "compiler_proxy.py"), encoding="utf-8") as handle:
            source = handle.read()

        cfg = artifacts["cfg"]
        bound = [i for b in cfg.blocks for i in b.instructions
                 if isinstance(i, OpInstr) and i.assigned_workstation_id is not None]
        self.assertTrue(bound)
        self.assertNotIn("schedule_result", artifacts)
        self.assertNotIn("mdg_post_ra", artifacts)

        paper1_body = source.split("def compile_order_program", 1)[1].split("def build_factory_target_machine")[0]
        for forbidden in ("RegisterAllocator", "RegionScheduler", "PostRAScheduler", "PBQPSolver"):
            self.assertNotIn(forbidden, paper1_body,
                             f"Paper 1 流水线不应使用 {forbidden}")

class TestResidualIRAndWorksetComposer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.proxy = CompilerProxy()
        cls.generator = PCOrderProgramGenerator()

    def _compile_orders(self, orders):
        compiled = []
        for order in orders:
            program = self.generator.generate(order)
            results = self.generator.compile_program(self.proxy, program)
            artifacts = results["artifacts"]
            compiled.append(CompiledOrderProgram(
                program=program,
                cfg=artifacts["cfg"],
                mdg=artifacts["mdg"],
                op_instr_ids=artifacts["op_instr_ids"],
            ))
        return compiled

    def test_program_generation_is_rule_driven(self):
        orders = PCOrderGenerator(seed=5, load="low").generate(2)
        program = self.generator.generate(orders[0])

        names = [op.name for op in program.operations]
        self.assertEqual(names[0], "Inbound_Load")
        self.assertIn("Base_Assembly", names)
        self.assertIn("Outbound_Store", names)
        if orders[0].components["GPU"] == "GPU_NONE":
            self.assertNotIn("GPU_Install", names)
        else:
            self.assertIn("GPU_Install", names)

    def test_workset_marks_completed_and_pending_parts(self):
        orders = PCOrderGenerator(seed=9, load="low").generate(2)
        compiled = self._compile_orders(orders)
        records = {
            item.program.order_id: ResidualRecord(order_id=item.program.order_id, completed_seq=3)
            for item in compiled
        }
        workset = WorksetComposer().compose(compiled, records, FactoryState())

        self.assertEqual(workset.orders(), [item.program.order_id for item in compiled])
        self.assertTrue(workset.pending_vregs)
        self.assertTrue(workset.frozen_vregs)
        self.assertFalse(workset.pending_vregs & workset.frozen_vregs)

        for fragment in workset.fragments:
            self.assertEqual(len(fragment.view.frozen_ops), 4)
            self.assertEqual(len(fragment.view.residual_ops), len(fragment.view.frozen_ops)
                             + len(fragment.view.residual_ops) - 4)

    def test_carried_materials_keep_their_runtime_location(self):
        orders = PCOrderGenerator(seed=13, load="low").generate(1)
        compiled = self._compile_orders(orders)
        # Sequence 2 is complete; sequence 3 is running and its location is fixed.
        record = ResidualRecord(order_id=orders[0].task_name, completed_seq=2, executing_seqs=(3,))

        probe = WorksetComposer().compose(compiled, {record.order_id: record}, FactoryState())
        self.assertTrue(probe.carried_vregs, "正在执行的工序所产出的物料应被标为携带类")
        self.assertFalse(probe.carried_vregs & probe.pending_vregs,
                         "携带类版本不应再参与重新落位")

        vreg = sorted(probe.carried_vregs)[0]
        state = FactoryState(time=10.0)
        state.wip_location[vreg] = 5
        workset = WorksetComposer().compose(compiled, {record.order_id: record}, state)

        self.assertIn(vreg, workset.carried_vregs)
        self.assertEqual(workset.pinned_locations.get(vreg), 5)

    def test_workset_composition_does_not_rewrite_the_order_program(self):
        orders = PCOrderGenerator(seed=17, load="low").generate(1)
        compiled = self._compile_orders(orders)
        program = compiled[0].program
        before = program.source

        WorksetComposer().compose(
            compiled,
            {program.order_id: ResidualRecord(order_id=program.order_id, completed_seq=5)},
            FactoryState(),
        )
        self.assertEqual(program.source, before, "工作集组合不得改写订单源码")

class TestCompilerProxyReentrancy(unittest.TestCase):
    """Check independent frontend state across consecutive compilations."""

    SOURCE_A = (
        "Resource Assembly_PM: Device<Process>;\n"
        "Material A[1];\n"
        "Task Main(Material a) {\n"
        '    Process(Assembly_PM, a, "Base_Assembly;component=Motherboard;model=MB_1");\n'
        "    return a;\n"
        "}\n"
    )
    SOURCE_B = (
        "Resource Assembly_PM: Device<Process>;\n"
        "Material B[1];\n"
        "Task Main(Material b) {\n"
        '    Process(Assembly_PM, b, "CPU_Install;component=CPU;model=CPU_1");\n'
        "    return b;\n"
        "}\n"
    )

    def test_lightweight_pipeline_can_be_called_repeatedly(self):
        proxy = CompilerProxy()
        for _ in range(3):
            results = proxy.compile_source_lightweight(self.SOURCE_A, enable_try_catch=True)
            self.assertTrue(results["success"], results["messages"])
            self.assertTrue(results["artifacts"]["alloc_map"])

    def test_full_pipeline_can_be_called_repeatedly(self):
        proxy = CompilerProxy()
        for source in (self.SOURCE_A, self.SOURCE_B, self.SOURCE_A):
            results = proxy.compile_source(source, emit_outputs=False)
            self.assertTrue(results["success"], results["messages"])

    def test_paper1_pipeline_can_be_called_repeatedly(self):
        proxy = CompilerProxy()
        for source in (self.SOURCE_A, self.SOURCE_B, self.SOURCE_A):
            results = proxy.compile_order_program(source)
            self.assertTrue(results["success"], results["messages"])

class TestEndToEndPCOrderCompilation(unittest.TestCase):

    def test_full_pc_order_program_compiles_and_binds(self):
        proxy = CompilerProxy()
        generator = PCOrderProgramGenerator()

        for order in PCOrderGenerator(seed=3, load="medium").generate(3):
            program = generator.generate(order)
            results = generator.compile_program(proxy, program)

            self.assertTrue(results["success"], results["messages"])
            artifacts = results["artifacts"]
            allocation = artifacts["alloc_map"]

            self.assertTrue(allocation)
            self.assertTrue(all(v != "memory" for v in allocation.values()),
                            "Paper 1 不得把物料版本落到 warehouse")
            self.assertEqual(len(allocation), len(artifacts["coloring"].allocation))
            self.assertFalse(artifacts["coloring"].waiting)

            self.assertTrue(artifacts["exception_table"],
                            "订单程序带 try/catch，必须产出异常表")
            self.assertTrue(artifacts["catch_contingency"])

            for vreg, site_id in allocation.items():
                self.assertIn(site_id, artifacts["lowering"].eligible_sites[vreg],
                              f"{vreg} 被分配到不具备其生产能力的工位")

    def test_unknown_operation_fails_logically_only_frontend_then_lowering(self):
        """Resolve target capabilities during lowering after logical frontend checks."""
        proxy = CompilerProxy()
        source = (
            "Resource Assembly_PM: Device<Process>;\n"
            "Material PC[1];\n"
            "Task Main(Material pc) {\n"
            '    Process(Assembly_PM, pc, "Weld_Frame;component=X;model=Y");\n'
            "    return pc;\n"
            "}\n"
        )
        results = proxy.compile_order_program(source)
        self.assertFalse(results["success"])
        self.assertIn("Cannot bind", " ".join(results["messages"]))

if __name__ == "__main__":
    unittest.main()
