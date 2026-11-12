import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from case_study.domains import DOMAIN_KEYS, materialize_all, program_generator, spec_of, target_model_path
from case_study.domains.orders import DomainOrderGenerator
from compiler.factory_target_machine import AVAIL_LOGISTICS, AVAIL_WORKSTATION, FactoryState
from compiler.ir import ControlFlowGraph, OpInstr
from compiler.target_lowering import LoweredOperation
from compiler.regional_residual import (
    PrecompiledExceptionHandler,
    RegionalResidualRecompilation,
    UncertaintyCoordinator,
)
from compiler.residual_coloring import WorksetColorer, WorksetColorResult
from compiler.residual_ir import CompiledOrderProgram, ResidualRecord, WorksetComposer
from compiler.uncertainty import (
    GROUP_A,
    GROUP_B,
    TARGET_HOLDING,
    TARGET_WORKSTATION,
    EventAttributes,
    ResponsePolicy,
    UncertaintyEvent,
    UncertaintyKind,
    UncertaintyStream,
)
from core.compiler_proxy import CompilerProxy

class TestUncertaintyTaxonomy(unittest.TestCase):
    def test_six_kinds_with_no_mechanism_division(self):
        kinds = {t.kind for t in GROUP_A + GROUP_B}
        self.assertEqual(len(kinds), 6)
        self.assertEqual(kinds, set(UncertaintyKind))
        # An exception type links a machine failure to its predefined handler.
        machine_failure = next(t for t in GROUP_B if t.kind is UncertaintyKind.MACHINE_FAILURE)
        self.assertTrue(machine_failure.divertible)
        self.assertTrue(machine_failure.exception_type,
                        "资源态事件同样可以由 catch 承载，不能没有异常类型")

    def test_policy_matrix_is_driven_by_attributes_not_by_kind(self):
        no_handler = EventAttributes(anticipated=False)
        handled_contained = EventAttributes(anticipated=True, executable_at_trigger=True,
                                            contained=True)
        handled_leaky = EventAttributes(anticipated=True, executable_at_trigger=True,
                                        contained=False, uncovered_versions=4)
        handled_stuck = EventAttributes(anticipated=True, executable_at_trigger=False,
                                        contained=True)

        expected = {
            # (policy, attributes) -> (use_handler, recompile, scope)
            (ResponsePolicy.REACTIVE, "no_handler"): (False, True, "whole_region"),
            (ResponsePolicy.REACTIVE, "contained"): (False, True, "whole_region"),
            (ResponsePolicy.REACTIVE, "stuck"): (False, True, "whole_region"),
            (ResponsePolicy.PREDEFINITION, "no_handler"): (False, False, ""),
            (ResponsePolicy.PREDEFINITION, "contained"): (True, False, ""),
            (ResponsePolicy.PREDEFINITION, "leaky"): (True, False, ""),
            # A predefined-only policy waits when its handler cannot execute.
            (ResponsePolicy.PREDEFINITION, "stuck"): (True, False, ""),
            (ResponsePolicy.HYBRID, "no_handler"): (False, True, "whole_region"),
            (ResponsePolicy.HYBRID, "contained"): (True, False, ""),
            (ResponsePolicy.HYBRID, "leaky"): (True, True, "uncovered_part"),
            (ResponsePolicy.HYBRID, "stuck"): (False, True, "whole_region"),
        }
        for (policy, key), (use_handler, recompile, scope) in expected.items():
            attributes = {"no_handler": no_handler, "contained": handled_contained,
                          "leaky": handled_leaky, "stuck": handled_stuck}[key]
            plan = policy.plan(attributes)
            self.assertEqual(
                (plan.use_handler, plan.recompile, plan.recompile_scope),
                (use_handler, recompile, scope),
                f"{policy.value}/{key}",
            )

    def test_hybrid_declines_the_handler_only_when_it_cannot_run(self):
        runnable = EventAttributes(anticipated=True, executable_at_trigger=True, contained=False)
        stuck = EventAttributes(anticipated=True, executable_at_trigger=False, contained=True)

        self.assertEqual(ResponsePolicy.PREDEFINITION.plan(runnable).use_handler,
                         ResponsePolicy.HYBRID.plan(runnable).use_handler,
                         "处理器跑得起来时，两者对是否启用它的判断一致")
        self.assertFalse(ResponsePolicy.HYBRID.plan(stuck).use_handler,
                         "处理器跑不起来时 Hybrid 应当放弃它并重编译")
        self.assertTrue(ResponsePolicy.PREDEFINITION.plan(stuck).use_handler,
                        "Predefinition 没有退路，只能仍然启用它")

    def test_mixed_stream_uses_group_composition_ratio(self):
        stream = UncertaintyStream(seed=20260927, group_a_share=0.75)
        counts = stream.group_counts(4000)
        share = counts["A"] / (counts["A"] + counts["B"])
        self.assertAlmostEqual(share, 0.75, delta=0.03,
                               msg=f"Mixed 应为 75% A 组合 + 25% B 组合，实测 {share:.3f}")

class UncertaintyFixture(unittest.TestCase):
    """Build and initially allocate a Study 2 workset."""

    domain = "garment"

    @classmethod
    def setUpClass(cls):
        materialize_all()
        cls.proxy = CompilerProxy(str(target_model_path(cls.domain)))

    def setUp(self):
        spec = spec_of(self.domain)
        generator = program_generator(self.domain)
        orders = DomainOrderGenerator(spec, seed=211, load="low").generate(3, generator)

        self.lowerings = {}
        self.handlers = {}
        compiled = []
        for order in orders:
            program = generator.generate(order)
            artifacts = generator.compile_program(self.proxy, program)["artifacts"]
            compiled.append(CompiledOrderProgram(
                program=program, cfg=artifacts["cfg"], mdg=artifacts["mdg"],
                op_instr_ids=artifacts["op_instr_ids"],
            ))
            self.lowerings[program.order_id] = artifacts["lowering"]
            self.handlers[program.order_id] = PrecompiledExceptionHandler(
                artifacts["cfg"], artifacts["exception_table"], artifacts["catch_contingency"]
            )
            self.orders = orders
            self.f_t = artifacts["f_t"]

        records = {
            item.program.order_id: ResidualRecord(order_id=item.program.order_id, completed_seq=-1)
            for item in compiled
        }
        self.workset = WorksetComposer().compose(compiled, records, self.f_t.state)
        self.colorer = WorksetColorer(self.f_t)
        initial = self.colorer.color(self.workset.fragments, self.lowerings, time=0.0)
        self.allocation = dict(initial.allocation)

    def coordinator(self, policy):
        coordinator = UncertaintyCoordinator(
            self.f_t, self.lowerings, self.workset, self.colorer,
            policy=policy, handlers=self.handlers,
        )
        coordinator.set_allocation(self.allocation)
        return coordinator

    def station_of_kind(self, ws_type):
        for site in self.f_t.holding_sites():
            mapping = (self.f_t._tm.data.get("addressing_model") or {}).get("workstation_mapping", [])
            entry = next((w for w in mapping if w.get("workstation_id") == site.workstation_id), None)
            if entry and entry.get("pm_type") == ws_type:
                return site.workstation_id
        return None

class TestAttributeDrivenProgramGeneration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        materialize_all(force=True)

    def test_protections_differ_across_orders_of_the_same_domain(self):
        spec = spec_of("garment")
        generator = program_generator("garment")
        orders = DomainOrderGenerator(spec, seed=31, load="low").generate(24, generator)
        signatures = {
            tuple(sorted((p.region, p.exception_type) for p in generator.protections_for(order)))
            for order in orders
        }
        self.assertGreater(len(signatures), 1,
                           "同一 domain 的不同订单应当得到不同的预定义保护")

    def test_long_lead_component_gets_material_shortage_protection(self):
        spec = spec_of("garment")
        generator = program_generator("garment")
        orders = DomainOrderGenerator(spec, seed=31, load="low").generate(60, generator)
        wool = next(o for o in orders if o.components["Fabric"] == "WOOL")
        blend = next(o for o in orders if o.components["Fabric"] == "BLEND")

        wool_types = {p.exception_type for p in generator.protections_for(wool)}
        blend_types = {p.exception_type for p in generator.protections_for(blend)}
        self.assertIn("MaterialMissing", wool_types)
        self.assertNotIn("MaterialMissing", blend_types)

    def test_material_shortage_handler_loads_from_warehouse(self):
        spec = spec_of("garment")
        generator = program_generator("garment")
        orders = DomainOrderGenerator(spec, seed=31, load="low").generate(60, generator)
        order = next(o for o in orders if o.components["Fabric"] == "WOOL")
        source = generator.generate(order).source
        self.assertIn("catch(MaterialMissing)", source)
        self.assertIn("Load(", source)

    def test_nested_try_gives_one_operation_two_handlers(self):
        proxy = CompilerProxy(str(target_model_path("garment")))
        spec = spec_of("garment")
        generator = program_generator("garment")
        orders = DomainOrderGenerator(spec, seed=31, load="low").generate(60, generator)

        found = False
        for order in orders:
            program = generator.generate(order)
            if program.source.count("try {") < 2:
                continue
            artifacts = generator.compile_program(proxy, program)["artifacts"]
            multi = [instr for block in artifacts["cfg"].blocks
                     for instr in block.instructions
                     if len(getattr(instr, "catch_edges", []) or []) >= 2]
            if multi:
                types = {artifacts["exception_table"][i].exception_type
                         for i, entry in enumerate(artifacts["exception_table"])}
                self.assertIn("ToolingDeviation", types)
                self.assertIn("MachineFailure", types)
                found = True
                break
        self.assertTrue(found, "应当存在同一条工序携带两条异常边的订单程序")

    def test_every_domain_produces_exception_tables_for_all_orders(self):
        for key in DOMAIN_KEYS:
            spec = spec_of(key)
            generator = program_generator(key)
            proxy = CompilerProxy(str(target_model_path(key)))
            for order in DomainOrderGenerator(spec, seed=41, load="low").generate(20, generator):
                artifacts = generator.compile_program(proxy, generator.generate(order))["artifacts"]
                self.assertTrue(artifacts["exception_table"], f"{key}/{order.task_name}")
                self.assertTrue(all(v != "memory" for v in artifacts["alloc_map"].values()))

class TestHandlerActivationAndContainment(UncertaintyFixture):
    def test_precompiled_actions_share_their_output_location(self):
        for handler in self.handlers.values():
            for entry in handler.exception_table:
                for key, binding in handler.cfg.precompiled_handler_bindings.items():
                    if key[0] != entry.entry_id:
                        continue
                    for instr in handler.recovery_instructions(entry.exception_type, entry.catch_block.id):
                        action = binding.actions.get(id(instr))
                        if action is None or action[0] != 'workstation':
                            continue
                        outputs = list(instr.implicit_targets or []) + ([instr.target] if instr.target else [])
                        for vreg in outputs:
                            if vreg in binding.allocation:
                                self.assertEqual(self.f_t.holding_site(binding.allocation[vreg]).workstation_id, action[1])

    def test_handler_lookup_is_scoped_to_the_trigger_instruction(self):
        handler = next(iter(self.handlers.values()))
        entry = next(e for e in handler.exception_table if e.exception_type == "MachineFailure")
        scoped = handler.for_instruction(entry.trigger_instr)
        self.assertTrue(scoped.handles("MachineFailure"))
        self.assertIsNotNone(scoped.apply("MachineFailure", 10.0))
        foreign = OpInstr("Process", "foreign_1", [], params="Cut_Fabric")
        self.assertFalse(handler.for_instruction(foreign).handles("MachineFailure"))

    def test_anticipated_event_activates_prebound_handler_without_coloring(self):
        coordinator = self.coordinator(ResponsePolicy.PREDEFINITION)
        order = self.orders[0]
        event = UncertaintyEvent(
            event_id="mf-sewing", type=_machine_failure(), time=30.0,
            order_id=order.task_name, target_kind=TARGET_WORKSTATION,
            target=self.station_of_kind("sewing"), resolves_at=200.0,
        )
        entry = next(e for e in coordinator.handlers[order.task_name].exception_table
                     if e.exception_type == event.type.exception_type and event.target in
                     coordinator.lowerings[order.task_name].operations[id(e.trigger_instr)].candidate_workstations)
        coordinator.handlers[order.task_name] = coordinator.handlers[order.task_name].for_instruction(entry.trigger_instr)
        attributes = coordinator.attributes_of(event)
        self.assertTrue(attributes.anticipated, "订单程序里已预定义设备故障处置")
        self.assertIsNotNone(attributes.handler_exception_type)

        from unittest.mock import patch
        with patch.object(coordinator.colorer, 'color', side_effect=AssertionError('运行时不得为 handler 着色')):
            outcome = coordinator.handle(event)
        self.assertIsNotNone(outcome.handler_application)
        # Handler activation uses the binding established during initial compilation.
        handler_vregs = outcome.handler_application.region_vregs
        placed = [v for v in handler_vregs if v in coordinator.allocation]
        self.assertTrue(placed, "处理器产生的物料版本必须被分配物理位置")

    def test_parallel_alternatives_do_not_repair_existing_bindings(self):
        """A spare station does not make a failed existing binding executable."""
        coordinator = self.coordinator(ResponsePolicy.HYBRID)
        order = self.orders[0]
        event = UncertaintyEvent(
            event_id="mf-sewing", type=_machine_failure(), time=30.0,
            order_id=order.task_name, target_kind=TARGET_WORKSTATION,
            target=self.station_of_kind("sewing"), resolves_at=200.0,
        )
        attributes = coordinator.attributes_of(event)
        self.assertFalse(attributes.contained)
        self.assertGreater(attributes.uncovered_versions, 0)
        for fragment in self.workset.fragments:
            lowering = self.lowerings[fragment.order_id]
            for vreg in fragment.pending_vregs:
                site = coordinator.allocation.get(vreg)
                if site is not None and self.f_t.holding_site(site).workstation_id == event.target:
                    coordinator.allocation[vreg] = next(
                        s for s in lowering.eligible_sites[vreg]
                        if self.f_t.holding_site(s).workstation_id != event.target)
        self.assertTrue(coordinator.attributes_of(event).contained)
        outcome = coordinator.handle(event)
        self.assertEqual(outcome.plan.use_handler, outcome.attributes.executable_at_trigger)
        self.assertEqual(outcome.plan.recompile, not outcome.attributes.executable_at_trigger,
                         "外部绑定有效时，是否重编译仍取决于固定处理器资源能否使用")

    def test_not_contained_when_the_bottleneck_station_has_no_alternative(self):
        """A unique packaging station can invalidate allocations outside the handler coverage."""
        coordinator = self.coordinator(ResponsePolicy.HYBRID)
        order = self.orders[0]
        station = self.station_of_kind("packing")
        self.assertIsNotNone(station, "服装线应当有一个包装工位")
        event = UncertaintyEvent(
            event_id="mf-packing", type=_machine_failure(), time=30.0,
            order_id=order.task_name, target_kind=TARGET_WORKSTATION,
            target=station, resolves_at=200.0,
        )
        attributes = coordinator.attributes_of(event)
        self.assertFalse(attributes.contained,
                         "唯一包装工位停机时，处理器兜不住")
        self.assertGreater(attributes.uncovered_versions, 0)

        outcome = coordinator.handle(event)
        self.assertTrue(outcome.plan.use_handler)
        self.assertTrue(outcome.plan.recompile,
                        "兜不住的部分必须交给区域性重编译")
        self.assertEqual(outcome.plan.recompile_scope, "uncovered_part")
        self.assertTrue(outcome.recompile.rebuilt)

    def test_reactive_never_uses_the_handler(self):
        coordinator = self.coordinator(ResponsePolicy.REACTIVE)
        order = self.orders[0]
        event = UncertaintyEvent(
            event_id="mf-sewing", type=_machine_failure(), time=30.0,
            order_id=order.task_name, target_kind=TARGET_WORKSTATION,
            target=self.station_of_kind("sewing"), resolves_at=200.0,
        )
        outcome = coordinator.handle(event)
        self.assertFalse(outcome.plan.use_handler)
        self.assertIsNone(outcome.handler_application)
        self.assertTrue(outcome.plan.recompile)

    def test_predefinition_never_recompiles_even_when_uncontained(self):
        coordinator = self.coordinator(ResponsePolicy.PREDEFINITION)
        order = self.orders[0]
        event = UncertaintyEvent(
            event_id="mf-packing", type=_machine_failure(), time=30.0,
            order_id=order.task_name, target_kind=TARGET_WORKSTATION,
            target=self.station_of_kind("packing"), resolves_at=200.0,
        )
        outcome = coordinator.handle(event)
        self.assertTrue(outcome.plan.use_handler)
        self.assertFalse(outcome.plan.recompile)
        self.assertIsNone(outcome.recompile)

    def test_no_handler_leads_to_regional_recompilation(self):
        coordinator = self.coordinator(ResponsePolicy.HYBRID)
        event = UncertaintyEvent(
            event_id="buffer-down", type=_buffer_unavailable(), time=30.0,
            target_kind=TARGET_HOLDING, target=next(iter(self.allocation.values())),
            resolves_at=200.0,
        )
        attributes = coordinator.attributes_of(event)
        self.assertFalse(attributes.anticipated,
                         "缓存不可用没有预定义处理器")
        outcome = coordinator.handle(event)
        self.assertEqual(outcome.plan.recompile_scope, "whole_region")
        self.assertTrue(outcome.recompile.rebuilt)

    def test_release_restores_availability_and_retries(self):
        coordinator = self.coordinator(ResponsePolicy.HYBRID)
        station = self.station_of_kind("packing")
        event = UncertaintyEvent(
            event_id="mf-packing", type=_machine_failure(), time=30.0,
            target_kind=TARGET_WORKSTATION, target=station, resolves_at=90.0,
        )
        coordinator.handle(event)
        self.assertFalse(self.f_t.is_site_available(
            self.f_t.holding_sites_of_workstation(station)[0], 30.0))
        released, _ = coordinator.release(event)
        self.assertTrue(released)
        self.assertTrue(self.f_t.is_site_available(
            self.f_t.holding_sites_of_workstation(station)[0], 90.0))

    def test_no_policy_ever_spills_material(self):
        for policy in ResponsePolicy:
            coordinator = self.coordinator(policy)
            order = self.orders[0]
            coordinator.handle(UncertaintyEvent(
                event_id=f"e-{policy.value}", type=_machine_failure(), time=20.0,
                order_id=order.task_name, target_kind=TARGET_WORKSTATION,
                target=self.station_of_kind("packing"), resolves_at=120.0,
            ))
            for vreg, site in coordinator.allocation.items():
                self.assertIsInstance(site, int, f"{policy.value}: {vreg} 落到了 warehouse")

class TestRegionalResidualRecompilation(UncertaintyFixture):
    def test_resource_boundary_keeps_valid_neighbour_bindings(self):
        coordinator = self.coordinator(ResponsePolicy.HYBRID)
        site = next(iter(self.allocation.values()))
        event = UncertaintyEvent("site-down", _buffer_unavailable(), 25.0,
                                 target_kind=TARGET_HOLDING, target=site, resolves_at=100.0)
        region = coordinator.detector.detect_region(event, self.workset, self.allocation, 25.0)
        expected = {vreg for vreg in self.workset.pending_vregs
                    if self.allocation.get(vreg) == site}
        self.assertTrue(expected)
        self.assertEqual(region.expansion, expected)

    def test_handler_checks_every_action_including_continuations(self):
        cfg = ControlFlowGraph()
        first = cfg.new_block("Catch_MaterialMissing")
        second = cfg.new_block("Catch_MaterialMissing_Fallthrough")
        first.add_successor(second)
        first_op = OpInstr("Adjust", "p_1", [])
        second_op = OpInstr("Adjust", "p_2", [])
        first.add_instr(first_op)
        second.add_instr(second_op)
        entry = SimpleNamespace(entry_id='test', exception_type="MaterialMissing", catch_block=first)
        from compiler.precompiled_binding import HandlerBinding
        cfg.precompiled_handler_bindings = {('test', None, None): HandlerBinding(
            actions={id(first_op):('workstation',0,0), id(second_op):('workstation',1,0)})}
        handler = PrecompiledExceptionHandler(cfg, [entry, entry])
        self.assertEqual(handler.recovery_instructions("MaterialMissing"), [first_op, second_op])
        order_id = self.orders[0].task_name
        coordinator = self.coordinator(ResponsePolicy.HYBRID)
        coordinator.handlers[order_id] = handler
        lowering = coordinator.lowerings[order_id]
        for instr, station in ((first_op, 0), (second_op, 1)):
            lowering.operations[id(instr)] = LoweredOperation(
                instr, "Adjust", capability=("1", "1", "1"), candidate_workstations=(station,))
        event = UncertaintyEvent("missing", GROUP_A[0], 10.0, order_id=order_id)
        self.assertTrue(coordinator._handler_executable(event))
        self.f_t.mark_unavailable(AVAIL_WORKSTATION, 1, 50.0)
        self.assertFalse(coordinator._handler_executable(event))

    def test_logistics_failure_does_not_invalidate_in_place_processing(self):
        coordinator = self.coordinator(ResponsePolicy.HYBRID)
        fragment = self.workset.fragments[0]
        vreg = sorted(fragment.pending_vregs)[0]
        site = self.lowerings[fragment.order_id].eligible_sites[vreg][0]
        self.f_t.state.wip_location[vreg.rsplit("_", 1)[0] + "_held"] = site
        event = UncertaintyEvent("agv-down", GROUP_B[2], 10.0,
                                 target_kind="logistics", target=self.f_t.logistics_resource().palu_id,
                                 resolves_at=50.0)
        coordinator.detector.apply_state_change(event)
        invalid = coordinator.detector.versions_with_invalidated_candidates(event, self.workset, 10.0)
        self.assertNotIn(vreg, invalid)
        self.assertFalse(coordinator.detector._is_directly_invalidated(
            vreg, site, self.lowerings[fragment.order_id], set(), set(), True))

    def test_other_orders_keep_their_capacity_reservations(self):
        first, second, third = self.workset.fragments
        versions = [sorted(f.pending_vregs)[0] for f in (first, second, third)]
        site = self.lowerings[first.order_id].eligible_sites[versions[0]][0]
        self.assertEqual(self.f_t.capacity_of(site), 2)
        self.lowerings[third.order_id].eligible_sites[versions[2]] = [site]
        result = self.colorer.color(
            self.workset.fragments, self.lowerings,
            pinned_locations={versions[0]: site, versions[1]: site},
            affected_vregs={third.order_id: {versions[2]}})
        self.assertIn(versions[2], result.waiting)
        self.assertNotIn(versions[2], result.allocation)
        self.assertEqual(result.allocation[versions[0]], site)
        self.assertEqual(result.allocation[versions[1]], site)

    def test_runtime_coloring_keeps_the_instruction_sequences(self):
        blocks = [block for fragment in self.workset.fragments for block in fragment.cfg.blocks]
        before = {id(block): tuple(block.instructions) for block in blocks}
        for t in (10.0, 20.0):
            self.colorer.color(self.workset.fragments, self.lowerings, time=t)
        self.assertEqual({id(block): tuple(block.instructions) for block in blocks}, before)

    def test_wait_result_removes_the_previous_allocation(self):
        coordinator = self.coordinator(ResponsePolicy.HYBRID)
        vreg = next(iter(coordinator.allocation))
        coordinator._absorb(WorksetColorResult(waiting=[vreg]))
        self.assertNotIn(vreg, coordinator.allocation)
        self.assertIn(vreg, coordinator.waiting)

    def test_explicit_seeds_keep_their_own_orders(self):
        coordinator = self.coordinator(ResponsePolicy.HYBRID)
        event = UncertaintyEvent(
            event_id="shared-failure", type=_machine_failure(), time=25.0,
            order_id=self.orders[0].task_name, target_kind=TARGET_WORKSTATION,
            target=self.station_of_kind("packing"), resolves_at=200.0,
        )
        seeds = set()
        for fragment in self.workset.fragments:
            seeds.add(sorted(fragment.pending_vregs)[0])
        region = coordinator.detector.detect_region(
            event, self.workset, self.allocation, 25.0, seeds=seeds)
        self.assertEqual(set(region.by_order), {f.order_id for f in self.workset.fragments})
        self.assertTrue(seeds <= region.expansion)
        for fragment in self.workset.fragments:
            self.assertTrue(region.by_order[fragment.order_id] <=
                            (set(fragment.pending_vregs) | set(fragment.carried_vregs)))

    def test_event_order_boundary_on_unanticipated_resource_event(self):
        coordinator = self.coordinator(ResponsePolicy.HYBRID)
        coordinator.region_rule = "event_order"
        order_id = self.orders[0].task_name
        event = UncertaintyEvent(
            event_id="buffer-local", type=_buffer_unavailable(), time=25.0,
            order_id=order_id, target_kind=TARGET_HOLDING,
            target=next(iter(self.allocation.values())), resolves_at=200.0,
        )
        outcome = coordinator.handle(event)
        self.assertEqual(outcome.plan.recompile_scope, "whole_region")
        self.assertEqual(set(outcome.region.by_order), {order_id})
        self.assertEqual(outcome.region.expansion,
                         coordinator.detector.order_residual_versions(self.workset, order_id))

    def test_whole_workset_includes_other_orders_after_handler(self):
        coordinator = self.coordinator(ResponsePolicy.HYBRID)
        coordinator.region_rule = "whole_workset"
        event = UncertaintyEvent(
            event_id="packing-all", type=_machine_failure(), time=25.0,
            order_id=self.orders[0].task_name, target_kind=TARGET_WORKSTATION,
            target=self.station_of_kind("packing"), resolves_at=200.0,
        )
        outcome = coordinator.handle(event)
        self.assertEqual(outcome.plan.recompile_scope, "uncovered_part")
        self.assertEqual(outcome.region.expansion,
                         set(self.workset.pending_vregs) | set(self.workset.carried_vregs))

    def test_empty_machine_group_stays_empty(self):
        coordinator = self.coordinator(ResponsePolicy.HYBRID)
        coordinator.region_rule = "machine_group"
        event = UncertaintyEvent(
            event_id="unused-machine", type=_machine_failure(), time=25.0,
            target_kind=TARGET_WORKSTATION, target=-1, resolves_at=200.0,
        )
        self.assertEqual(coordinator._baseline_boundary(event, {"unrelated"}), set())

    def test_machine_group_maps_a_buffer_to_its_workstation(self):
        coordinator = self.coordinator(ResponsePolicy.HYBRID)
        coordinator.region_rule = "machine_group"
        station = self.station_of_kind("packing")
        site = self.f_t.holding_sites_of_workstation(station)[0]
        event = UncertaintyEvent(
            event_id="packing-buffer", type=_buffer_unavailable(), time=25.0,
            target_kind=TARGET_HOLDING, target=site, resolves_at=200.0,
        )
        picked = coordinator._baseline_boundary(event, None)
        expected = set()
        sites = set(self.f_t.holding_sites_of_workstation(station))
        for fragment in self.workset.fragments:
            lowering = self.lowerings[fragment.order_id]
            for vreg in set(fragment.pending_vregs) | set(fragment.carried_vregs):
                if sites & set(lowering.eligible_sites.get(vreg, ())):
                    expected.add(vreg)
        self.assertTrue(expected)
        self.assertEqual(picked, expected)

    def test_buffer_failure_does_not_invalidate_its_sibling(self):
        coordinator = self.coordinator(ResponsePolicy.HYBRID)
        station = self.station_of_kind("packing")
        first, second = self.f_t.holding_sites_of_workstation(station)
        fragment = self.workset.fragments[0]
        vreg = sorted(fragment.pending_vregs)[0]
        lowering = self.lowerings[fragment.order_id]
        lowering.eligible_sites[vreg] = [first, second]
        event = UncertaintyEvent(
            event_id="one-buffer", type=_buffer_unavailable(), time=25.0,
            target_kind=TARGET_HOLDING, target=first, resolves_at=200.0,
        )
        coordinator.detector.apply_state_change(event)
        invalid = coordinator.detector.versions_with_invalidated_candidates(
            event, self.workset, 25.0)
        self.assertNotIn(vreg, invalid)
        self.assertFalse(coordinator.detector._is_directly_invalidated(
            vreg, second, lowering, *coordinator.detector._invalidated_resources(event, 25.0)))

    def test_only_the_invalidated_versions_are_rebound(self):
        detector = RegionalResidualRecompilation(self.f_t, self.lowerings)
        station = self.station_of_kind("packing")
        event = UncertaintyEvent(
            event_id="mf", type=_machine_failure(), time=25.0,
            target_kind=TARGET_WORKSTATION, target=station, resolves_at=200.0,
        )
        detector.apply_state_change(event)
        region = detector.detect_region(event, self.workset, self.allocation, 25.0)
        self.assertFalse(region.is_empty())
        self.assertTrue(region.frozen)

        result = detector.recompile(region, self.workset, self.allocation, self.colorer, 25.0)
        self.assertTrue(result.rebuilt)
        for vreg, site in self.allocation.items():
            if vreg in region.expansion:
                continue
            self.assertEqual(result.color_result.allocation.get(vreg, site), site,
                             f"未受影响的版本 {vreg} 不应被搬动")

    def test_short_outage_preserves_bindings_used_after_recovery(self):
        detector = RegionalResidualRecompilation(self.f_t, self.lowerings)
        station = self.station_of_kind("packing")
        event = UncertaintyEvent(
            event_id="brief", type=_machine_failure(), time=25.0,
            target_kind=TARGET_WORKSTATION, target=station, resolves_at=30.0,
        )
        detector.apply_state_change(event)
        original = detector.detect_region(event, self.workset, self.allocation, 25.0)
        self.assertTrue(original.directly_affected)
        later = next(iter(original.directly_affected))
        self.workset.earliest_use[later] = 30.0
        region = detector.detect_region(event, self.workset, self.allocation, 25.0)
        self.assertNotIn(later, region.expansion)
        self.assertIn(later, region.frozen)
        self.assertNotIn(later, detector.versions_with_invalidated_candidates(
            event, self.workset, 25.0))
        # Repair the binding if its operation could start before resource recovery.
        self.workset.earliest_use[later] = 29.0
        region = detector.detect_region(event, self.workset, self.allocation, 25.0)
        self.assertIn(later, region.expansion)

class TestContinuationHandlerBoundary(UncertaintyFixture):
    domain = "garment_continuation"

    def test_handler_includes_the_faulting_instruction_material(self):
        coordinator = self.coordinator(ResponsePolicy.HYBRID)
        fragment = self.workset.fragments[0]
        handler = self.handlers[fragment.order_id]
        entry = next(e for e in handler.exception_table if e.exception_type == "MachineFailure")
        instr = entry.trigger_instr
        event = UncertaintyEvent(
            "fault", _machine_failure(), 10.0, order_id=fragment.order_id,
            op_key=fragment.instr_op[id(instr)], target_kind=TARGET_WORKSTATION,
            target=0, resolves_at=50.0)
        app = coordinator._activate_handler(event)
        defined = set(instr.implicit_targets or ()) | ({instr.target} if instr.target else set())
        pending = defined & fragment.pending_vregs
        self.assertTrue(pending)
        self.assertLessEqual(pending, app.region_vregs)

    def test_uncovered_recompile_keeps_the_handlers_new_binding(self):
        coordinator = self.coordinator(ResponsePolicy.HYBRID)
        fragment = self.workset.fragments[0]
        vreg = next(v for v in sorted(fragment.pending_vregs)
                    if v in self.allocation and
                    len({self.f_t.holding_site(s).workstation_id
                         for s in self.lowerings[fragment.order_id].eligible_sites[v]}) > 1)
        old = self.allocation[vreg]
        station = self.f_t.holding_site(old).workstation_id
        new = next(s for s in self.lowerings[fragment.order_id].eligible_sites[vreg]
                   if self.f_t.holding_site(s).workstation_id != station)
        event = UncertaintyEvent("fault", _machine_failure(), 10.0,
            order_id=fragment.order_id, target_kind=TARGET_WORKSTATION,
            target=station, resolves_at=50.0)
        from compiler.precompiled_binding import HandlerBinding
        application = SimpleNamespace(region_vregs={vreg}, catch_block_id=0,
                                      binding=HandlerBinding(allocation={vreg:new}))

        def place_handler(*args):
            coordinator.allocation[vreg] = new

        with patch.object(coordinator, "_activate_handler", return_value=application), \
             patch.object(coordinator, "_handler_executable", return_value=True), \
             patch.object(coordinator, "_bind_handler_material", side_effect=place_handler), \
             patch.object(coordinator.detector, "recompile", wraps=coordinator.detector.recompile) as rebuild:
            outcome = coordinator.handle(event)
        self.assertTrue(outcome.plan.recompile)
        self.assertNotIn(vreg, outcome.region.expansion)
        self.assertEqual(rebuild.call_args.args[2][vreg], new)
        self.assertEqual(coordinator.allocation[vreg], new)

def _machine_failure():
    from compiler.uncertainty import ALL_TYPES
    return next(t for t in ALL_TYPES if t.kind is UncertaintyKind.MACHINE_FAILURE)

def _buffer_unavailable():
    from compiler.uncertainty import ALL_TYPES
    return next(t for t in ALL_TYPES if t.kind is UncertaintyKind.BUFFER_UNAVAILABLE)

if __name__ == "__main__":
    unittest.main()
