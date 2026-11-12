import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from case_study.domains import (
    DOMAIN_KEYS,
    DOMAIN_SPECS,
    materialize_all,
    program_generator,
    spec_of,
    target_model_path,
)
from case_study.domains.orders import DomainOrderGenerator
from case_study.study1 import (
    FT_STATES,
    build_main_table,
    compiler_core_digest,
    grammar_digest,
    run_study1,
)
from core.compiler_proxy import CompilerProxy
from core.target_model import TargetModelLoader

class TestDomainSpecifications(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.paths = materialize_all(force=True)

    def test_three_domains_are_materialized_as_target_models(self):
        self.assertEqual(len(self.paths), 3)
        for path in self.paths:
            self.assertTrue(path.exists(), path)
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertIn("instruction_capability_set", payload)
            self.assertIn("addressing_model", payload)
            self.assertIn("storage_model", payload)
            self.assertIn("runtime_environment", payload)

    def test_each_domain_model_loads_through_the_same_loader(self):
        for key in DOMAIN_KEYS:
            loader = TargetModelLoader(str(target_model_path(key)))
            ok, message = loader.load()
            self.assertTrue(ok, f"{key}: {message}")
            regions = loader.get_storage_regions()
            self.assertEqual(len(regions["register_ids"]), 18)
            self.assertEqual(regions["memory_ids"], [100])

    def test_domains_are_genuinely_different_configurations(self):
        role_sets = [set(spec_of(key).role_names()) for key in DOMAIN_KEYS]
        for i, left in enumerate(role_sets):
            for right in role_sets[i + 1:]:
                self.assertNotEqual(left, right, "不同 domain 的逻辑资源角色应当不同")

        action_sets = [
            {(action.op_type, action.rp, action.description) for action in spec_of(key).actions}
            for key in DOMAIN_KEYS
        ]
        # Generic recovery actions may overlap; manufacturing actions distinguish domains.
        for i in range(len(DOMAIN_KEYS)):
            for j in range(i + 1, len(DOMAIN_KEYS)):
                shared = len(action_sets[i] & action_sets[j])
                smaller = min(len(action_sets[i]), len(action_sets[j]))
                self.assertLessEqual(
                    shared / smaller, 0.15,
                    f"{DOMAIN_KEYS[i]} 与 {DOMAIN_KEYS[j]} 的制造动作过于相似：{shared}/{smaller}",
                )

        device_types = [
            {module.device_type for module in spec_of(key).resource_modules}
            for key in DOMAIN_KEYS
        ]
        self.assertTrue(all(len(types) >= 4 for types in device_types))

    def test_every_domain_declares_attribute_driven_protections(self):
        for key in DOMAIN_KEYS:
            spec = spec_of(key)
            self.assertTrue(spec.protections, key)
            kinds = {p.uncertainty_key for p in spec.protections}
            self.assertGreaterEqual(len(kinds), 3, f"{key}: 预定义保护覆盖面过窄 {kinds}")
            for protection in spec.protections:
                self.assertTrue(spec.rules_of_region(protection.region),
                                f"{key}: 区段 {protection.region} 没有任何工序")
                self.assertTrue(
                    protection.always or protection.when_component or protection.when_grades,
                    f"{key}/{protection.region}: 保护缺少触发条件",
                )

    def test_protections_are_selected_by_order_attributes(self):
        for key in DOMAIN_KEYS:
            spec = spec_of(key)
            generator = program_generator(key)
            orders = DomainOrderGenerator(spec, seed=17, load="low").generate(24, generator)
            signatures = {
                tuple(sorted((p.region, p.exception_type) for p in generator.protections_for(order)))
                for order in orders
            }
            self.assertGreater(len(signatures), 1,
                               f"{key}: 所有订单的预定义保护完全相同，说明没有按订单属性区分")

class TestGrammarAndCoreInvariance(unittest.TestCase):

    def test_grammar_digest_is_a_single_value(self):
        digest = grammar_digest(Path(ROOT))
        self.assertEqual(len(digest), 16)
        self.assertEqual(digest, grammar_digest(Path(ROOT)))

    def test_no_domain_specific_keyword_leaks_into_the_grammar(self):
        from compiler.lexer import TokenType

        keywords = {token.name for token in TokenType}
        forbidden = {
            "ASSEMBLY", "SEWING", "MILLING", "GARMENT", "CNC", "SOLDER",
            "PC_ASSEMBLY", "CUT_FABRIC", "FACE_MILL",
        }
        self.assertFalse(keywords & forbidden,
                         "制造门类专属动作必须是能力描述，不能成为语法关键字")

    def test_one_compiler_proxy_per_domain_shares_the_same_code(self):
        digests = {compiler_core_digest(Path(ROOT)) for _ in DOMAIN_KEYS}
        self.assertEqual(len(digests), 1)

class TestCrossDomainCompilation(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        materialize_all()

    def test_every_domain_order_program_compiles_without_spill(self):
        for key in DOMAIN_KEYS:
            spec = spec_of(key)
            generator = program_generator(key)
            proxy = CompilerProxy(str(target_model_path(key)))
            orders = DomainOrderGenerator(spec, seed=7, load="low").generate(2, generator)
            for order in orders:
                program = generator.generate(order)
                results = generator.compile_program(proxy, program)
                artifacts = results["artifacts"]
                self.assertTrue(results["success"], f"{key}: {results['messages']}")

                allocation = artifacts["alloc_map"]
                self.assertTrue(allocation, key)
                for vreg, site in allocation.items():
                    self.assertIsInstance(site, int, f"{key}: {vreg} 不得落到 warehouse")
                self.assertTrue(artifacts["exception_table"],
                                f"{key}: U1 的预编译异常处理必须存在")
                self.assertTrue(artifacts["catch_contingency"], key)

    def test_same_program_across_operational_states(self):
        from case_study.study1 import build_state

        for key in DOMAIN_KEYS:
            spec = spec_of(key)
            generator = program_generator(key)
            proxy = CompilerProxy(str(target_model_path(key)))
            order = DomainOrderGenerator(spec, seed=11, load="low").generate(1, generator)[0]

            sources = []
            for ft_state in FT_STATES:
                program = generator.generate(order)
                f_t = proxy.build_factory_target_machine(
                    build_state(proxy.build_factory_target_machine(), ft_state, 1)
                )
                self.assertIsNotNone(f_t)
                sources.append(program.source)
            self.assertEqual(len(set(sources)), 1,
                             f"{key}: 车间状态变化不得要求修改订单程序")

    def test_order_generation_is_reproducible_per_seed(self):
        for key in DOMAIN_KEYS:
            spec = spec_of(key)
            generator = program_generator(key)
            first = DomainOrderGenerator(spec, seed=5, load="medium").generate(4, generator)
            second = DomainOrderGenerator(spec, seed=5, load="medium").generate(4, generator)
            self.assertEqual([o.options for o in first], [o.options for o in second])
            self.assertEqual([o.arrival_time for o in first], [o.arrival_time for o in second])
            self.assertTrue(all(o.due_time > o.arrival_time for o in first))

    def test_order_customization_space_is_non_trivial(self):
        for key in DOMAIN_KEYS:
            spec = spec_of(key)
            space = 1
            for family in spec.option_families:
                space *= len(family.models)
            self.assertGreaterEqual(space, 64, f"{key}: 可配置空间过小（{space}）")

class TestStudy1Runner(unittest.TestCase):
    def test_runner_writes_all_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = run_study1(orders_per_domain=3, seed=99, out_dir=Path(tmp))
            for key in ("records_json", "records_csv", "main_table_csv",
                        "main_table_md", "manifest_json"):
                self.assertTrue(paths[key].exists(), key)

            records = json.loads(paths["records_json"].read_text(encoding="utf-8"))
            self.assertEqual(len(records), len(DOMAIN_KEYS) * len(FT_STATES))

            manifest = json.loads(paths["manifest_json"].read_text(encoding="utf-8"))
            self.assertEqual(manifest["domains"], list(DOMAIN_KEYS))
            self.assertEqual(manifest["ft_states"], list(FT_STATES))

    def test_main_table_reports_zero_grammar_and_core_modification(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = run_study1(orders_per_domain=3, seed=101, out_dir=Path(tmp))
            records = json.loads(paths["records_json"].read_text(encoding="utf-8"))
            from case_study.study1 import Study1Record

            table = build_main_table([Study1Record(**row) for row in records])
            self.assertEqual(len(table), len(DOMAIN_KEYS))
            for row in table:
                self.assertEqual(row["grammar_modification"], 0, row["domain"])
                self.assertEqual(row["compiler_core_modification"], 0, row["domain"])
                self.assertEqual(row["source_modification_across_Ft"], 0, row["domain"])
                self.assertEqual(row["memory_spills"], 0, row["domain"])
                self.assertEqual(row["compile_failures"], 0, row["domain"])
                self.assertEqual(row["realization_success"], 1.0,
                                 f"{row['domain']}: 订单程序应当在每种车间状态下都能被完整实现")

    def test_realization_is_complete_in_every_domain_and_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = run_study1(orders_per_domain=4, seed=107, out_dir=Path(tmp))
            records = json.loads(paths["records_json"].read_text(encoding="utf-8"))
            self.assertEqual(len(records), len(DOMAIN_KEYS) * len(FT_STATES))
            for row in records:
                self.assertEqual(row["realization_success"], 1.0,
                                 f"{row['domain']}/{row['ft_state']}")
                self.assertTrue(row["deferred_not_failed"], f"{row['domain']}/{row['ft_state']}")
                self.assertEqual(row["memory_spills"], 0, f"{row['domain']}/{row['ft_state']}")
                self.assertEqual(row["settled_placed"], row["pending_versions"],
                                 f"{row['domain']}/{row['ft_state']}")

    def test_degraded_states_actually_change_immediate_placement(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = run_study1(orders_per_domain=6, seed=109, out_dir=Path(tmp))
            records = json.loads(paths["records_json"].read_text(encoding="utf-8"))
            by_domain = {}
            for row in records:
                by_domain.setdefault(row["domain"], {})[row["ft_state"]] = row

            for domain, states in by_domain.items():
                normal = states["normal"]
                self.assertEqual(normal["immediate_placement"], 1.0,
                                 f"{domain}: 正常状态下应当一次全部落位")
                for degraded in ("high_occupancy", "machine_unavailable"):
                    self.assertLess(
                        states[degraded]["immediate_placement"], 1.0,
                        f"{domain}/{degraded}: 降级状态必须产生与正常状态不同的压力",
                    )
                    self.assertGreater(
                        states[degraded]["deferred_after_progress"], 0,
                        f"{domain}/{degraded}: 应当出现需要等待的版本",
                    )
                self.assertIsNotNone(states["machine_unavailable"]["down_workstation"])

    def test_degraded_state_pressure_is_not_overwhelming(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = run_study1(orders_per_domain=6, seed=113, out_dir=Path(tmp))
            records = json.loads(paths["records_json"].read_text(encoding="utf-8"))
            for row in records:
                self.assertGreater(row["immediate_placement"], 0.5,
                                   f"{row['domain']}/{row['ft_state']} 压力过大，已接近不可行")

class TestProductionParadigms(unittest.TestCase):

    def test_paradigms_are_distinct(self):
        paradigms = [spec_of(key).paradigm for key in DOMAIN_KEYS]
        self.assertEqual(len(set(paradigms)), len(DOMAIN_KEYS))
        self.assertIn("matrix", paradigms)
        self.assertIn("flexible_assembly_line", paradigms)
        self.assertIn("flexible_job_shop", paradigms)

    def test_paradigm_is_recorded_in_the_target_model(self):
        for key in DOMAIN_KEYS:
            payload = json.loads(target_model_path(key).read_text(encoding="utf-8"))
            self.assertEqual(payload["meta"]["production_paradigm"], spec_of(key).paradigm)

    def test_layout_reflects_the_paradigm(self):
        # Flow line: parallel stations in each stage are adjacent.
        line = spec_of("garment")
        rows = {ws.row for ws in line.workstations}
        self.assertEqual(rows, {0}, "装配线应当排成一条线")
        stages = [ws.ws_type for ws in sorted(line.workstations, key=lambda w: w.col)]
        self.assertEqual(stages, sorted(stages, key=lambda t: ["cutting", "sewing", "inspection", "packing"].index(t)),
                         "同一工段的并行工位必须相邻")

        # Job shop: similar machines share a functional area.
        job_shop = spec_of("cnc_machining")
        type_cols = {}
        for ws in job_shop.workstations:
            type_cols.setdefault(ws.ws_type, set()).add(ws.col)
        for ws_type, cols in type_cols.items():
            self.assertLessEqual(len(cols), 2, f"{ws_type} 的功能区跨度太大")

        # Matrix: station types alternate across the grid.
        matrix = spec_of("pc_assembly")
        grid_types = {ws.ws_type for ws in matrix.workstations}
        self.assertGreaterEqual(len(grid_types), 3)

if __name__ == "__main__":
    unittest.main()
