"""Regression checks for ties and small-sample statistics."""
import itertools
from types import SimpleNamespace
import unittest

from case_study.statistics import friedman_test, rank, wilcoxon_signed_rank

class TestPaper1Statistics(unittest.TestCase):
    def test_friedman_corrects_tied_methods(self):
        rows = [SimpleNamespace(seed=seed, method=method, score=value)
                for seed in range(4) for method, value in zip("abc", (1, 1, 2))]
        self.assertAlmostEqual(friedman_test(rows, "score")["statistic"], 8.0)

    def test_friedman_identical_methods_have_no_difference(self):
        rows = [SimpleNamespace(seed=seed, method=method, score=seed)
                for seed in range(4) for method in "abc"]
        self.assertEqual(friedman_test(rows, "score"), {"statistic": 0.0, "p_value": 1.0})

    def test_one_pair_cannot_be_significant(self):
        self.assertEqual(wilcoxon_signed_rank([2], [1])["p_value"], 1.0)

    def test_exact_wilcoxon_matches_all_sign_assignments_with_ties_and_zeros(self):
        diffs = [0, 1, -1, 2, 2, -3]
        active = [value for value in diffs if value]
        ranks = rank([abs(value) for value in active])
        total = sum(ranks)
        observed_plus = sum(r for r, d in zip(ranks, active) if d > 0)
        observed = min(observed_plus, total - observed_plus)
        extreme = 0
        for signs in itertools.product((0, 1), repeat=len(active)):
            plus = sum(r * sign for r, sign in zip(ranks, signs))
            extreme += min(plus, total - plus) <= observed
        result = wilcoxon_signed_rank(diffs, [0] * len(diffs))
        self.assertEqual(result["statistic"], observed)
        self.assertEqual(result["p_value"], extreme / 2 ** len(active))

    def test_twenty_equal_direction_pairs(self):
        result = wilcoxon_signed_rank([1] * 20, [0] * 20)
        self.assertEqual(result["p_value"], 2 / 2 ** 20)

    def test_decimal_observations_keep_equal_difference_ranks(self):
        result = wilcoxon_signed_rank([0.4, 0.6, 0.2], [0.3, 0.5, 0.3])
        self.assertEqual(result["statistic"], 2.0)
        self.assertEqual(result["p_value"], 1.0)

if __name__ == "__main__":
    unittest.main()
