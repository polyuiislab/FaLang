import os
import sys
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from compiler.catch_contingency import CatchContingencyPlanner
from compiler.exception_table import ExceptionTableBuilder
from compiler.ir import ControlFlowGraph, OpInstr
from compiler.ast_nodes import IntDecl, Operation
from compiler.lexer import Lexer
from compiler.parser import Parser
from compiler.scheduler import ScheduleResult
from core.target_model import TargetModelLoader

class TestCatchContingencyPlanner(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tm = TargetModelLoader()
        ok, msg = cls.tm.load()
        if not ok:
            raise RuntimeError(msg)

    def _schedule_result(self, timeline):
        return ScheduleResult(
            block_schedules={},
            global_makespan=0.0,
            device_timeline=timeline,
            material_timeline={},
            cache_peak={},
            mfr_boundaries=[],
            exit_states={},
        )

    def _cfg_with_catch(self):
        cfg = ControlFlowGraph()
        cfg.resource_names = {"CNC_1"}
        cfg.material_names = {"part"}
        trigger = OpInstr(
            "Process",
            "part_1",
            ["CNC_1", "part_0"],
            rp="00001",
            expected_time=45.0,
        )
        trigger.scheduled_finish = 45.0
        catch_block = cfg.new_block("Catch_ResourceFault")
        catch_instr = OpInstr(
            "Adjust",
            "part_2",
            ["CNC_1", "part_1"],
            rp="00001",
            expected_time=2.0,
        )
        catch_block.add_instr(catch_instr)
        trigger.catch_edges.append(catch_block)
        cfg.entry_block.add_instr(trigger)
        return cfg, catch_instr

    def test_catch_waits_for_busy_device_and_stays_before_deadline(self):
        cfg, catch_instr = self._cfg_with_catch()
        planner = CatchContingencyPlanner(self.tm)
        result = planner.plan(
            cfg,
            self._schedule_result({"CNC_1": [(45.0, 50.0, "normal_op")]}),
            {"part_1": 0, "part_2": 0},
        )

        self.assertEqual(result[0].state, "scheduled")
        self.assertEqual(catch_instr.contingency_start, 50.0)
        self.assertEqual(catch_instr.contingency_finish, 52.0)
        self.assertEqual(catch_instr.assigned_device_id, "CNC_1")
        self.assertIsNotNone(catch_instr.assigned_palu_id)

    def test_catch_late_path_is_explicit_when_deadline_window_is_missed(self):
        cfg, catch_instr = self._cfg_with_catch()
        planner = CatchContingencyPlanner(self.tm)
        result = planner.plan(
            cfg,
            self._schedule_result({"CNC_1": [(45.0, 80.0, "long_normal_op")]}),
            {"part_1": 0, "part_2": 0},
        )

        self.assertEqual(result[0].state, "late")
        self.assertEqual(catch_instr.contingency_state, "late")
        self.assertGreater(catch_instr.contingency_start, catch_instr.contingency_deadline)

    def test_due_time_source_comes_from_falang_task_param(self):
        cfg, catch_instr = self._cfg_with_catch()
        cfg.order_due_time_param = "order_due_time"
        planner = CatchContingencyPlanner(self.tm)
        planner.plan(
            cfg,
            self._schedule_result({}),
            {"part_1": 0, "part_2": 0},
        )

        self.assertEqual(catch_instr.contingency_deadline_source, "param:order_due_time")
        self.assertEqual(catch_instr.contingency_deadline, float("inf"))

    def test_static_order_due_time_drives_warning_window(self):
        cfg, catch_instr = self._cfg_with_catch()
        cfg.order_due_time_value = 51.0
        planner = CatchContingencyPlanner(self.tm)
        result = planner.plan(
            cfg,
            self._schedule_result({"CNC_1": [(45.0, 50.0, "normal_op")]}),
            {"part_1": 0, "part_2": 0},
        )

        self.assertEqual(result[0].state, "deferred")
        self.assertEqual(catch_instr.contingency_deadline, 51.0)
        self.assertEqual(catch_instr.contingency_deadline_source, "program:int order_due_time")

    def test_int_operation_assignment_still_parses(self):
        src = """
        Resource AGV_1: Device<Load>;
        Material part[1];
        Task Main(Material part) {
            int ready = IsAvailable(AGV_1);
            int order_due_time = 120;
        }
        """
        ast = Parser(Lexer(src).tokenize()).parse()
        body = ast.tasks[0].body

        self.assertIsInstance(body[0], Operation)
        self.assertEqual(body[0].target, "ready")
        self.assertEqual(body[0].op_type, "IsAvailable")
        self.assertIsInstance(body[1], IntDecl)
        self.assertEqual(body[1].name, "order_due_time")

    def test_exception_table_uses_trigger_material_as_faulted_material(self):
        cfg, catch_instr = self._cfg_with_catch()
        entries = ExceptionTableBuilder().build(cfg)

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].landing_pad.faulted_materials, ["part_0"])

        CatchContingencyPlanner(self.tm).plan(
            cfg,
            self._schedule_result({}),
            {"part_0": 0, "part_1": 0, "part_2": 0},
        )
        self.assertEqual(catch_instr.args[1], "part_0")

    def test_deferred_catch_runs_after_all_busy_normal_work(self):
        cfg, catch_instr = self._cfg_with_catch()
        planner = CatchContingencyPlanner(self.tm)
        result = planner.plan(
            cfg,
            self._schedule_result({"CNC_1": [(45.0, 80.0, "long_normal_op")]}),
            {"part_0": 0, "part_1": 0, "part_2": 0},
        )

        self.assertEqual(result[0].state, "late")
        self.assertEqual(catch_instr.contingency_start, 80.0)

if __name__ == "__main__":
    unittest.main()
