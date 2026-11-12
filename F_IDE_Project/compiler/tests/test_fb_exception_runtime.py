import os
import sys
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from compiler.ir import ControlFlowGraph, OpInstr
from compiler.fb_config.fb_builder import FBConfigBuilder
from core.target_model import TargetModelLoader

class TestFBExceptionRuntimeNetwork(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tm = TargetModelLoader()
        ok, msg = cls.tm.load()
        if not ok:
            raise RuntimeError(f"Failed to load target model: {msg}")

    def test_exception_edge_uses_runtime_network(self):
        cfg = ControlFlowGraph()
        cfg.resource_names = {"CNC_1"}
        cfg.material_names = {"part"}
        catch_block = cfg.new_block("Catch_ResourceFault")

        trigger = OpInstr("Process", "part_1", ["CNC_1", "part_0"], rp="00001")
        catch_instr = OpInstr("Adjust", "part_recovered_1", ["CNC_1", "part_1"], rp="00001")
        trigger.catch_edges.append(catch_block)
        cfg.entry_block.add_instr(trigger)
        catch_block.add_instr(catch_instr)

        config = FBConfigBuilder(self.tm).build(cfg, {"part_0": 0, "part_1": 1})
        fb_types = {inst.fb_type for inst in config.top_level.instances}

        for fb_type in {
            "CatchDispatcher_FB",
            "LandingPad_FB",
            "CatchResourceRequest_FB",
            "DeferredCatchQueue_FB",
            "LateRecovery_FB",
            "SafeDegrade_FB",
        }:
            self.assertIn(fb_type, fb_types)

        conns = {(c.source, c.destination, c.conn_type) for c in config.top_level.connections}
        block_id = cfg.entry_block.id
        catch_id = catch_block.id
        base = f"EXC_B{block_id}_I0_C{catch_id}"

        self.assertNotIn((f"Block_{block_id}.EO_EXC", f"Block_{catch_id}.EI_CATCH", "EVENT"), conns)
        self.assertIn((f"Block_{block_id}.EO_EXC_I0", f"{base}_Dispatcher.EI_EXE", "EVENT"), conns)
        self.assertIn((f"{base}_Dispatcher.EO_DONE", f"{base}_LandingPad.EI_EXE", "EVENT"), conns)
        self.assertIn((f"{base}_LandingPad.EO_DONE", f"{base}_ResourceRequest.EI_EXE", "EVENT"), conns)
        self.assertIn((f"{base}_ResourceRequest.EO_DONE", f"Block_{catch_id}.EI_CATCH", "EVENT"), conns)
        self.assertIn((f"{base}_ResourceRequest.EO_DEFER", f"{base}_DeferredQueue.EI_EXE", "EVENT"), conns)
        self.assertIn((f"{base}_DeferredQueue.EO_DONE", f"Block_{catch_id}.EI_CATCH", "EVENT"), conns)
        self.assertIn((f"{base}_DeferredQueue.EO_LATE", f"{base}_LateRecovery.EI_EXE", "EVENT"), conns)
        self.assertIn((f"{base}_LateRecovery.EO_DONE", f"{base}_SafeDegrade.EI_EXE", "EVENT"), conns)

if __name__ == "__main__":
    unittest.main()
