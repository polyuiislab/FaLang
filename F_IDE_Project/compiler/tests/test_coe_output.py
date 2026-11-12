import os
import sys
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core.compiler_proxy import CompilerProxy

class TestSegmentedCOEOutput(unittest.TestCase):
    def test_generate_coe_file_accepts_segmented_image(self):
        proxy = CompilerProxy()
        image = {
            "format": "FISA_SEGMENTED_COE_V1",
            "flat": ["F15AC0E1", "00000001", "00000008"],
        }
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "segmented.coe")
            ok, msg = proxy.generate_coe_file(path, image)
            self.assertTrue(ok, msg)
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()

        self.assertIn("memory_initialization_radix = 16;", text)
        self.assertIn("F15AC0E1,", text)
        self.assertTrue(text.strip().endswith("00000008;"))

if __name__ == "__main__":
    unittest.main()
