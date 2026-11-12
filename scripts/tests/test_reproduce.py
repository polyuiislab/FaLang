"""Check reference comparison and the public study configuration without running simulations."""
import copy
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from reproduce import compare_rows
from verify_data import ROOT


class ReproductionChecks(unittest.TestCase):
    def setUp(self):
        self.reference = [{"seed": "182000", "method": "graph_coloring",
                           "makespan": "1027.72", "compile_time_s": "0.6"}]

    def test_cpu_timing_can_vary(self):
        rows = copy.deepcopy(self.reference)
        rows[0]["compile_time_s"] = "1.2"
        self.assertEqual(compare_rows("study2", rows, self.reference, ("seed", "method")), 3)

    def test_changed_production_metric_is_rejected(self):
        rows = copy.deepcopy(self.reference)
        rows[0]["makespan"] = "1100"
        with self.assertRaises(ValueError):
            compare_rows("study2", rows, self.reference, ("seed", "method"))

    def test_missing_or_duplicate_runs_are_rejected(self):
        for rows in ([], self.reference * 2):
            with self.subTest(rows=len(rows)), self.assertRaises(ValueError):
                compare_rows("study2", rows, self.reference, ("seed", "method"))

    def test_studies_share_the_paper_configuration(self):
        manifest = json.loads((ROOT / "data/manifest.json").read_text(encoding="utf-8"))
        second, third = (manifest["studies"][name] for name in ("study2", "study3"))
        for key in ("configuration", "target_model", "seeds", "orders"):
            self.assertEqual(second[key], third[key])
        self.assertEqual(second["seeds"], list(range(182000, 182050)))
        self.assertEqual(second["orders"], 50)
        self.assertEqual(len(second["methods"]), 4)
        self.assertEqual(len(third["methods"]), 6)
        self.assertNotIn("reactive@whole_workset", third["methods"])


if __name__ == "__main__":
    unittest.main()
