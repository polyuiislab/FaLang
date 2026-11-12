"""Check residual progress semantics and safe, offline demonstration export."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import demo_representations as demo


class RepresentationDemoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source, cls.cfg, cls.data, cls.messages = demo.build_demo(seed=9, completed=3)

    def test_progress_separates_completed_running_and_remaining_operations(self):
        data = self.data
        self.assertFalse(data["initial"]["frozen_operations"])
        self.assertFalse(data["initial"]["carried_vregs"])
        for op in data["operations"]:
            states = {row["state"] for row in data["progress"]["instructions"]
                      if row["operation"] == op["key"]}
            expected = "frozen" if op["seq"] < 3 else "carried" if op["seq"] == 3 else "pending"
            self.assertEqual(states, {expected}, op["name"])
        progress = data["progress"]
        self.assertTrue(progress["pinned_locations"])
        self.assertEqual(set(progress["carried_vregs"]), set(progress["pinned_locations"]))
        self.assertFalse(set(progress["carried_vregs"]) & set(progress["pending_vregs"]))
        for vreg, location in progress["pinned_locations"].items():
            self.assertEqual(location, data["initial_rgc_allocation"][vreg])

    def test_finished_order_has_no_remaining_production_operations(self):
        source, _, data, _ = demo.build_demo(seed=9, completed=len(self.data["operations"]))
        self.assertEqual(source, self.source)
        self.assertEqual(data["source_sha256"], hashlib.sha256(source.encode("utf-8")).hexdigest())
        self.assertFalse(data["progress"]["residual_operations"])
        self.assertFalse(data["progress"]["executing_seqs"])
        self.assertFalse(data["progress"]["carried_vregs"])

    def test_impossible_progress_is_rejected(self):
        for count in (-1, len(self.data["operations"]) + 1):
            with self.subTest(count=count), self.assertRaises(ValueError):
                demo.build_demo(seed=9, completed=count)

    def test_export_without_graphviz_and_no_overwrite(self):
        # 所有测试文件仅写入新建临时目录，退出时由 TemporaryDirectory 清理。
        results_root = (demo.ROOT / "results").resolve()
        results_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="falang_demo_test_", dir=results_root) as temp:
            root = Path(temp).resolve()
            self.assertTrue(root.is_relative_to(results_root))
            built = (self.source, self.cfg, copy.deepcopy(self.data), list(self.messages))
            with patch.object(demo, "ROOT", root), patch.object(demo, "build_demo", return_value=built):
                output = root / "results/demo"
                page = demo.export_demo(output, graph=False)
                data = json.loads((output / "representations.json").read_text(encoding="utf-8"))
                self.assertEqual(data["cfg_svg"], "SVG export disabled")
                self.assertIn("Supplied execution progress", page.read_text(encoding="utf-8"))
                self.assertTrue((output / "order_cfg.dot").is_file())
                self.assertFalse((output / "order_cfg.svg").exists())
                before = {p.name: p.read_bytes() for p in output.iterdir()}
                with self.assertRaises(ValueError):
                    demo.export_demo(output, completed=6)
                self.assertEqual(before, {p.name: p.read_bytes() for p in output.iterdir()})
                for invalid in (root / "data", root / "results", root / "results/../data"):
                    with self.subTest(path=invalid), self.assertRaises(ValueError):
                        demo.export_demo(invalid)


if __name__ == "__main__":
    unittest.main()
