import os
import sys
import tempfile
import unittest
from dataclasses import replace

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from case_study.order_generator import PCOrderGenerator
from case_study.program_generator import PCOrderProgramGenerator
from case_study.run_experiment import run_experiment
from case_study.simulation import (
    DynamicSimulator,
    METHODS,
    OperationInstance,
    PCFactory,
    operation_templates,
)
from case_study.state_aware_jit import RuntimeState, StateAwareJITCompiler
from compiler.ir import ControlFlowGraph, OpInstr
from core.target_model import TargetModelLoader

class TestCaseStudySimulation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tm = TargetModelLoader()
        ok, msg = tm.load()
        if not ok:
            raise RuntimeError(msg)
        cls.simulator = DynamicSimulator(PCFactory(tm))

    def test_all_methods_complete_small_order_set(self):
        orders = PCOrderGenerator(seed=11, load="low").generate(8)
        for method in METHODS:
            metrics, schedule = self.simulator.run(orders, method=method, seed=11, load="low")
            self.assertEqual(metrics.orders, 8)
            self.assertGreater(metrics.makespan, 0)
            self.assertGreater(len(schedule), 0)

    def test_future_orders_are_not_released_before_arrival(self):
        orders = PCOrderGenerator(seed=21, load="low").generate(2)
        late = replace(orders[1], arrival_time=500.0, due_time=800.0)
        orders = [orders[0], late]
        for method in METHODS:
            _, schedule = self.simulator.run(orders, method=method, seed=21, load="low")
            late_ops = [item for item in schedule if item.order_id == late.order_id]
            self.assertTrue(late_ops, method)
            self.assertGreaterEqual(min(item.logistics_start for item in late_ops), late.arrival_time)

    def test_experiment_runner_writes_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            outputs = run_experiment("low", seeds=1, orders_per_seed=5, seed_start=0, out_dir=tmp_path(tmp), make_plots=False)
            for key in ("metrics_csv", "summary_csv", "friedman_csv", "paired_tests_csv", "manifest_json"):
                self.assertTrue(outputs[key].exists(), key)

    def test_order_program_is_generated_once_and_per_order(self):
        orders = PCOrderGenerator(seed=31, load="high").generate(2)
        generator = PCOrderProgramGenerator()

        programs = [generator.generate(order) for order in orders]
        self.assertEqual([p.order_id for p in programs], ["Order_0000", "Order_0001"])

        for program, order in zip(programs, orders):
            # Order-specific material names keep simultaneous worksets distinct.
            material = f"raw_{program.order_id}"
            self.assertIn(f"Task {program.order_id}(Material {material})", program.source)
            self.assertIn(f'Load({material}, "Warehouse_To_PM_Buffer")', program.source)
            self.assertTrue(program.operations)

        self.assertNotEqual(programs[0].source, programs[1].source)

        self.assertEqual(generator.generate(orders[0]).source, programs[0].source)
        self.assertEqual(generator.generate(orders[0]).operations, programs[0].operations)

    def test_extract_plan_keeps_order_specific_instruction_bindings(self):
        orders = PCOrderGenerator(seed=32, load="high").generate(2)
        pending = {}
        cfg = ControlFlowGraph()

        for op_id, order in enumerate(orders):
            template = operation_templates(order)[0]
            op = OperationInstance(op_id, order, 0, template, ready_time=order.arrival_time)
            pending[order.order_id] = [op]
            instr = OpInstr(
                "Process",
                None,
                ["Assembly_PM", f"raw_{order.order_id:04d}_0"],
                params=(
                    f"{template.name};component={template.component};model={template.model}"
                ),
                opcode=template.opcode,
                func=template.func,
                rp=template.rp,
            )
            instr.assigned_workstation_id = 10 + order.order_id
            instr.scheduled_start = 20.0 + order.order_id
            instr.scheduled_finish = 30.0 + order.order_id
            cfg.entry_block.add_instr(instr)

        compiler = object.__new__(StateAwareJITCompiler)
        plan = compiler._extract_plan(cfg, None, {}, pending, RuntimeState())

        self.assertEqual(plan[pending[0][0].op_id], (10, 20.0, 30.0))
        self.assertEqual(plan[pending[1][0].op_id], (11, 21.0, 31.0))

def tmp_path(value):
    from pathlib import Path

    return Path(value)

if __name__ == "__main__":
    unittest.main()
