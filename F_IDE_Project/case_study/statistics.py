from __future__ import annotations

from collections import Counter, defaultdict
from decimal import Decimal
from math import erf, exp, lgamma, sqrt
import random
from typing import Dict, Iterable, List, Sequence

from case_study.simulation import SimulationMetrics


METRIC_NAMES = [
    "makespan",
    "mean_tardiness",
    "on_time_delivery_rate",
    "resource_utilization",
    "average_wip",
    "blocking_time",
    "waiting_time",
    "mof",
    "tst",
]


def mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def stdev(values: Sequence[float]) -> float:
    if len(values) < 2:
        return 0.0
    m = mean(values)
    return sqrt(sum((x - m) ** 2 for x in values) / (len(values) - 1))


def percentile(values: Sequence[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = (len(ordered) - 1) * p
    lo = int(idx)
    hi = min(lo + 1, len(ordered) - 1)
    frac = idx - lo
    return ordered[lo] * (1 - frac) + ordered[hi] * frac


def bootstrap_ci(values: Sequence[float], seed: int = 20260616, reps: int = 1000) -> tuple[float, float]:
    if not values:
        return 0.0, 0.0
    rng = random.Random(seed)
    boot = []
    n = len(values)
    for _ in range(reps):
        sample = [values[rng.randrange(n)] for _ in range(n)]
        boot.append(mean(sample))
    return percentile(boot, 0.025), percentile(boot, 0.975)


def grouped_values(rows: Iterable[SimulationMetrics], metric: str) -> Dict[str, List[float]]:
    groups: Dict[str, List[float]] = defaultdict(list)
    for row in rows:
        groups[row.method].append(float(getattr(row, metric)))
    return dict(groups)


def summary_table(rows: Sequence[SimulationMetrics]) -> List[Dict[str, object]]:
    out: List[Dict[str, object]] = []
    for metric in METRIC_NAMES:
        for method, values in grouped_values(rows, metric).items():
            lo, hi = bootstrap_ci(values)
            out.append(
                {
                    "metric": metric,
                    "method": method,
                    "mean": mean(values),
                    "std": stdev(values),
                    "median": percentile(values, 0.5),
                    "iqr": percentile(values, 0.75) - percentile(values, 0.25),
                    "ci95_low": lo,
                    "ci95_high": hi,
                }
            )
    return out


def normal_cdf(z: float) -> float:
    return 0.5 * (1.0 + erf(z / sqrt(2.0)))


def rank(values: Sequence[float]) -> List[float]:
    indexed = sorted(enumerate(values), key=lambda item: item[1])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(indexed):
        j = i
        while j + 1 < len(indexed) and indexed[j + 1][1] == indexed[i][1]:
            j += 1
        avg_rank = (i + j + 2) / 2.0
        for k in range(i, j + 1):
            ranks[indexed[k][0]] = avg_rank
        i = j + 1
    return ranks


def friedman_test(rows: Sequence[SimulationMetrics], metric: str) -> Dict[str, float]:
    by_seed: Dict[int, Dict[str, float]] = defaultdict(dict)
    methods = sorted({row.method for row in rows})
    for row in rows:
        by_seed[row.seed][row.method] = float(getattr(row, metric))
    complete = [vals for vals in by_seed.values() if all(method in vals for method in methods)]
    n = len(complete)
    k = len(methods)
    if n == 0 or k < 2:
        return {"statistic": 0.0, "p_value": 1.0}
    rank_sums = {method: 0.0 for method in methods}
    ties = 0
    reverse = metric in ("on_time_delivery_rate", "resource_utilization")
    for vals in complete:
        raw = [(-vals[m] if reverse else vals[m]) for m in methods]
        ties += sum(count ** 3 - count for count in Counter(raw).values())
        for method, r in zip(methods, rank(raw)):
            rank_sums[method] += r
    chi2 = (12.0 / (n * k * (k + 1))) * sum(v * v for v in rank_sums.values()) - 3 * n * (k + 1)
    correction = 1.0 - ties / (n * (k ** 3 - k))
    if correction == 0.0:
        return {"statistic": 0.0, "p_value": 1.0}
    chi2 = max(0.0, chi2) / correction
    p_value = chi_square_sf(max(0.0, chi2), k - 1)
    return {"statistic": chi2, "p_value": p_value}


def chi_square_sf(x: float, df: int) -> float:
    if df <= 0:
        return 1.0
    return gammaincc(df / 2.0, x / 2.0)


def gammaincc(a: float, x: float) -> float:
    if x <= 0:
        return 1.0
    if x < a + 1:
        return 1.0 - _gammainc_series(a, x)
    return _gammainc_cf(a, x)


def _gammainc_series(a: float, x: float) -> float:
    ap = a
    total = 1.0 / a
    delta = total
    for _ in range(200):
        ap += 1
        delta *= x / ap
        total += delta
        if abs(delta) < abs(total) * 1e-12:
            break
    return total * exp(-x + a * _safe_log(x) - lgamma(a))


def _gammainc_cf(a: float, x: float) -> float:
    b = x + 1 - a
    c = 1e30
    d = 1 / max(b, 1e-30)
    h = d
    for i in range(1, 200):
        an = -i * (i - a)
        b += 2
        d = an * d + b
        if abs(d) < 1e-30:
            d = 1e-30
        c = b + an / c
        if abs(c) < 1e-30:
            c = 1e-30
        d = 1 / d
        delta = d * c
        h *= delta
        if abs(delta - 1) < 1e-12:
            break
    return h * exp(-x + a * _safe_log(x) - lgamma(a))


def _safe_log(x: float) -> float:
    import math

    return math.log(max(x, 1e-300))


def wilcoxon_signed_rank(x: Sequence[float], y: Sequence[float]) -> Dict[str, float]:
    """Compute a two-sided signed-rank test using exact sign permutations with tied ranks."""
    # Preserve decimal precision so equal observed differences retain tied ranks.
    diffs = [Decimal(str(a)) - Decimal(str(b)) for a, b in zip(x, y) if a != b]
    n = len(diffs)
    if n == 0:
        return {"statistic": 0.0, "p_value": 1.0}
    abs_ranks = rank([abs(d) for d in diffs])
    w_plus = sum(r for r, d in zip(abs_ranks, diffs) if d > 0)
    w_minus = sum(r for r, d in zip(abs_ranks, diffs) if d < 0)
    w = min(w_plus, w_minus)
    if n <= 100:
        # Double the average ranks to count sign permutations with integer subset sums.
        weights = [int(2 * value) for value in abs_ranks]
        counts = [0] * (sum(weights) + 1)
        counts[0] = 1
        total = 0
        for weight in weights:
            for value in range(total, -1, -1):
                counts[value + weight] += counts[value]
            total += weight
        p = min(1.0, 2 * sum(counts[:int(2 * w) + 1]) / (2 ** n))
        return {"statistic": w, "p_value": p}
    mean_w = n * (n + 1) / 4
    var_w = sum(value * value for value in abs_ranks) / 4
    z = (w - mean_w) / sqrt(var_w) if var_w > 0 else 0.0
    p = 2 * min(normal_cdf(z), 1 - normal_cdf(z))
    return {"statistic": w, "p_value": max(0.0, min(1.0, p))}


def holm_bonferroni(p_values: Dict[str, float]) -> Dict[str, float]:
    items = sorted(p_values.items(), key=lambda item: item[1])
    m = len(items)
    adjusted = {}
    running = 0.0
    for i, (key, p) in enumerate(items):
        adj = min(1.0, (m - i) * p)
        running = max(running, adj)
        adjusted[key] = running
    return adjusted


def cliffs_delta(x: Sequence[float], y: Sequence[float]) -> float:
    if not x or not y:
        return 0.0
    gt = lt = 0
    for a in x:
        for b in y:
            if a > b:
                gt += 1
            elif a < b:
                lt += 1
    return (gt - lt) / (len(x) * len(y))


def paired_tests(rows: Sequence[SimulationMetrics], reference: str = "falang") -> List[Dict[str, object]]:
    methods = sorted({row.method for row in rows if row.method != reference})
    by_seed_method = {(row.seed, row.method): row for row in rows}
    results: List[Dict[str, object]] = []
    for metric in METRIC_NAMES:
        raw_p = {}
        pending = {}
        for method in methods:
            ref_vals = []
            cmp_vals = []
            for seed in sorted({row.seed for row in rows}):
                ref = by_seed_method.get((seed, reference))
                cmp = by_seed_method.get((seed, method))
                if ref and cmp:
                    ref_vals.append(float(getattr(ref, metric)))
                    cmp_vals.append(float(getattr(cmp, metric)))
            test = wilcoxon_signed_rank(ref_vals, cmp_vals)
            key = f"{metric}:{method}"
            raw_p[key] = test["p_value"]
            pending[key] = (method, ref_vals, cmp_vals, test)
        adjusted = holm_bonferroni(raw_p)
        for key, (method, ref_vals, cmp_vals, test) in pending.items():
            lower_is_better = metric not in ("on_time_delivery_rate", "resource_utilization")
            if lower_is_better:
                improvement = (mean(cmp_vals) - mean(ref_vals)) / max(abs(mean(cmp_vals)), 1e-9)
            else:
                improvement = (mean(ref_vals) - mean(cmp_vals)) / max(abs(mean(cmp_vals)), 1e-9)
            results.append(
                {
                    "metric": metric,
                    "reference": reference,
                    "method": method,
                    "wilcoxon_statistic": test["statistic"],
                    "p_value": test["p_value"],
                    "holm_p_value": adjusted[key],
                    "cliffs_delta": cliffs_delta(ref_vals, cmp_vals),
                    "paired_improvement_ratio": improvement,
                }
            )
    return results
