from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
import time
from typing import Dict, List

from case_study.order_generator import LOAD_MTBA, PCOrderGenerator
from case_study.plots import write_all_plots
from case_study.simulation import DynamicSimulator, METHODS, PCFactory, ScheduledOperation, SimulationMetrics
from case_study.statistics import METRIC_NAMES, friedman_test, paired_tests, summary_table
from core.target_model import TargetModelLoader


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the FaLang PC-MMS case-study simulation.")
    parser.add_argument("--load", choices=sorted(LOAD_MTBA), required=True, help="Load scenario to run.")
    parser.add_argument("--seeds", type=int, default=100, help="Number of paired random seeds.")
    parser.add_argument("--orders", type=int, default=200, help="Orders per seed.")
    parser.add_argument("--seed-start", type=int, default=0, help="First seed value.")
    parser.add_argument("--out", type=Path, default=Path("case_study_results"), help="Output directory.")
    parser.add_argument("--no-plots", action="store_true", help="Skip SVG generation.")
    parser.add_argument("--methods", nargs="+", default=None, help="Methods to run (space-separated). Default: all built-in methods.")
    return parser.parse_args()


def run_experiment(load: str, seeds: int, orders_per_seed: int, seed_start: int, out_dir: Path, make_plots: bool = True, methods: list = None) -> Dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    tm = TargetModelLoader()
    ok, msg = tm.load()
    if not ok:
        raise RuntimeError(msg)
    factory = PCFactory(tm)
    simulator = DynamicSimulator(factory)

    if methods is None:
        methods = list(METHODS)

    metrics_rows: List[SimulationMetrics] = []
    schedules_by_key: Dict[tuple[str, int], List[ScheduledOperation]] = {}
    started = time.time()

    for idx, seed in enumerate(range(seed_start, seed_start + seeds), start=1):
        generated_orders = PCOrderGenerator(seed=seed, load=load).generate(orders_per_seed)
        for method in methods:
            metrics, schedule = simulator.run(generated_orders, method=method, seed=seed, load=load)
            metrics_rows.append(metrics)
            if idx == 1:
                schedules_by_key[(method, seed)] = schedule
        elapsed = time.time() - started
        print(f"[{idx:03d}/{seeds}] load={load} seed={seed} done, elapsed={elapsed:.1f}s")

    paths = {
        "metrics_csv": out_dir / f"{load}_metrics.csv",
        "summary_csv": out_dir / f"{load}_summary.csv",
        "friedman_csv": out_dir / f"{load}_friedman.csv",
        "paired_tests_csv": out_dir / f"{load}_paired_tests.csv",
        "manifest_json": out_dir / f"{load}_manifest.json",
    }
    write_metrics_csv(paths["metrics_csv"], metrics_rows)
    write_dict_csv(paths["summary_csv"], summary_table(metrics_rows))
    write_dict_csv(paths["friedman_csv"], friedman_rows(metrics_rows))
    write_dict_csv(paths["paired_tests_csv"], paired_tests(metrics_rows, reference="falang"))
    write_manifest(paths["manifest_json"], load, seeds, orders_per_seed, seed_start, methods, paths)
    if make_plots:
        plot_dir = out_dir / f"{load}_figures"
        write_all_plots(metrics_rows, schedules_by_key, plot_dir)
        paths["figures"] = plot_dir
    return paths


def write_metrics_csv(path: Path, rows: List[SimulationMetrics]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].as_dict().keys()) if rows else [])
        writer.writeheader()
        for row in rows:
            writer.writerow(row.as_dict())


def write_dict_csv(path: Path, rows: List[Dict[str, object]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def friedman_rows(rows: List[SimulationMetrics]) -> List[Dict[str, object]]:
    return [
        {
            "metric": metric,
            **friedman_test(rows, metric),
        }
        for metric in METRIC_NAMES
    ]


def write_manifest(path: Path, load: str, seeds: int, orders: int, seed_start: int, methods: list, outputs: Dict[str, Path]) -> None:
    manifest = {
        "load": load,
        "seeds": seeds,
        "orders_per_seed": orders,
        "seed_start": seed_start,
        "methods": list(methods),
        "outputs": {key: str(value) for key, value in outputs.items()},
    }
    path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")


def main() -> None:
    args = parse_args()
    outputs = run_experiment(
        load=args.load,
        seeds=args.seeds,
        orders_per_seed=args.orders,
        seed_start=args.seed_start,
        out_dir=args.out,
        make_plots=not args.no_plots,
        methods=args.methods,
    )
    print("Experiment finished.")
    for name, path in outputs.items():
        print(f"{name}: {path}")


if __name__ == "__main__":
    main()
