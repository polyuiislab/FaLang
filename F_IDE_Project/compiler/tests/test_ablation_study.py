import os
import sys
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from ablation_study.config import ABLATION_METHODS, method_config
from ablation_study.run_ablation import run_ablation
from case_study.order_generator import PCOrderGenerator
from case_study.simulation import OperationInstance, operation_templates
from case_study.state_aware_jit import RuntimeState, StateAwareJITCompiler
from compiler.ir import OpInstr
from compiler.register_alloc import RegisterAllocator

class TestAblationStudy(unittest.TestCase):
    def test_ablation_method_set_is_fixed(self):
        self.assertEqual(
            ABLATION_METHODS,
            ("falang", "no_pre_ra", "no_pbqp"),
        )
        self.assertEqual(method_config("no_pbqp")["initial_compile_options"]["allocation_mode"], "graph_coloring")
        self.assertTrue(method_config("no_pre_ra")["initial_compile_options"]["skip_pre_ra"])

    def test_no_try_catch_source_has_no_exception_syntax(self):
        orders = PCOrderGenerator(seed=41, load="medium").generate(1)
        template = operation_templates(orders[0])[0]
        pending = {
            orders[0].order_id: [
                OperationInstance(0, orders[0], 0, template, ready_time=orders[0].arrival_time)
            ]
        }
        compiler = object.__new__(StateAwareJITCompiler)
        source = compiler._build_unified_source(
            RuntimeState(), orders, {}, pending, enable_try_catch=False
        )
        self.assertNotIn("try", source)
        self.assertNotIn("catch", source)
        self.assertNotIn("compensate", source)

    def test_graph_coloring_filters_unsupported_workstations(self):
        alloc = object.__new__(RegisterAllocator)
        alloc.vregs = ["pc_0"]
        alloc.pregs = [1, 2, "memory"]
        alloc.allocation = {}
        alloc.vreg_liveness = {"pc_0": (0.0, 10.0)}
        alloc._preg_to_ws = {1: 10, 2: 20}
        alloc._ws_to_pregs = {10: [1], 20: [2]}
        alloc._ws_capabilities = {
            10: {("000001", "011", "00001")},
            20: {("000001", "001", "00001")},
        }
        instr = OpInstr("Process", "pc_0", ["Assembly_PM", "pc_0"], rp="00001")
        alloc.def_instrs = {"pc_0": (0, 0, instr)}
        alloc.use_instrs = {}
        alloc.tm = None
        alloc._get_instr_capability = lambda _instr: ("000001", "001", "00001")
        alloc._is_logistics_cap = lambda _cap: False
        alloc._get_preg_capacity = lambda _preg: 1
        alloc._same_instr_usage = lambda _a, _b: False
        alloc._has_mdg_data_dependency = lambda _a, _b: False
        alloc._has_mdg_setup_dependency = lambda _a, _b: 0.0
        alloc._fix_capacity_violations = lambda: None
        alloc._insert_cross_ws_moves = lambda: None
        alloc._insert_spill_load_store = lambda: None
        alloc._assign_palus = lambda: None

        result = alloc.allocate_graph_coloring()
        self.assertEqual(result["pc_0"], 2)

    def test_ablation_smoke_outputs_four_methods(self):
        with tempfile.TemporaryDirectory() as tmp:
            outputs = run_ablation(
                load="medium",
                seeds=1,
                orders_per_seed=2,
                out_dir=tmp_path(tmp),
                make_plots=False,
            )
            text = outputs["metrics_csv"].read_text(encoding="utf-8")
            for method in ABLATION_METHODS:
                self.assertIn(method, text)

def tmp_path(value):
    from pathlib import Path

    return Path(value)

if __name__ == "__main__":
    unittest.main()

