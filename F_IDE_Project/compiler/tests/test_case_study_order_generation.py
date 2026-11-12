import os
import sys
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from case_study.order_generator import PCOrderGenerator
from case_study.source_generator import generate_order_source
from core.compiler_proxy import CompilerProxy

class TestCaseStudyOrderGeneration(unittest.TestCase):
    def test_order_generation_is_reproducible(self):
        first = PCOrderGenerator(seed=42, load="medium").generate(5)
        second = PCOrderGenerator(seed=42, load="medium").generate(5)

        self.assertEqual(first, second)
        self.assertTrue(all(order.due_time > order.arrival_time for order in first))

    def test_generated_order_source_compiles(self):
        order = PCOrderGenerator(seed=7, load="low").generate(1)[0]
        source = generate_order_source(order)
        result = CompilerProxy().compile_source(source)

        self.assertTrue(result["success"], "\n".join(result["messages"]))
        self.assertGreater(len(result.get("text_data", []) or []), 0)

if __name__ == "__main__":
    unittest.main()
