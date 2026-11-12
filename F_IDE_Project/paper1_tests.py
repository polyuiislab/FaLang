"""Run the representation and uncertainty-handling test suite.

Use unittest discovery to include the full compilation pipeline."""
from __future__ import annotations

import sys
import unittest

PAPER1_TEST_MODULES = (
    "compiler.tests.test_paper1_representation",
    "compiler.tests.test_paper1_uncertainty",
    "compiler.tests.test_study1_generality",
    "compiler.tests.test_study2_mechanism",
    "compiler.tests.test_paper1_statistics",
    "compiler.tests.test_ir_optimizations",
    "compiler.tests.test_mdg_builder",
    "compiler.tests.test_catch_contingency",
    "compiler.tests.test_fb_exception_runtime",
    "compiler.tests.test_coe_output",
    "compiler.tests.test_codegen",
)

# Full-pipeline tests are run by unittest discovery.
PAPER2_TEST_MODULES = (
    "compiler.tests.test_case_study_simulation",
    "compiler.tests.test_case_study_order_generation",
    "compiler.tests.test_case_study_target_model",
    "compiler.tests.test_register_alloc",
    "compiler.tests.test_post_ra_scheduler",
    "compiler.tests.test_ablation_study",
)

def build_suite(modules=PAPER1_TEST_MODULES) -> unittest.TestSuite:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for module in modules:
        suite.addTests(loader.loadTestsFromName(module))
    return suite

def main() -> int:
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(build_suite())
    print()
    print(f"Paper 1 test modules: {len(PAPER1_TEST_MODULES)}")
    print(f"Paper 2 modules skipped: {len(PAPER2_TEST_MODULES)}")
    return 0 if result.wasSuccessful() else 1

if __name__ == "__main__":
    sys.exit(main())
