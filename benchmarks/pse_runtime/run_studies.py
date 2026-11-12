"""Run Study 2 allocation or Study 3 uncertainty response on the shared AHU configuration."""
from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
import csv
from dataclasses import replace
import gzip
import json
from pathlib import Path
import time

from case_study.partial_coverage import run_candidate
from case_study.scenario import register_physical_scenario
from case_study.domains.spec import build_target_model
from case_study.study2 import Study2Config
from compiler.uncertainty import ResponsePolicy

METHODS = {
    "study2": (
        ("graph_coloring", "RGC", "hybrid", "containment", "graph_coloring"),
        ("first_fit", "First-fit", "hybrid", "containment", "first_fit"),
        ("machine_transport_joint", "Transfer priority", "hybrid", "containment", "machine_transport_joint"),
        ("load_balance", "Load priority", "hybrid", "containment", "load_balance"),
    ),
    "study3": (
        ("hybrid", "HUR (CBC)", "hybrid", "containment", "graph_coloring"),
        ("predefinition", "Predefined", "predefinition", "containment", "graph_coloring"),
        ("reactive", "Reactive", "reactive", "containment", "graph_coloring"),
        ("hybrid@event_order", "HUR (Order)", "hybrid", "event_order", "graph_coloring"),
        ("hybrid@machine_group", "HUR (Group)", "hybrid", "machine_group", "graph_coloring"),
        ("hybrid@whole_workset", "HUR (All)", "hybrid", "whole_workset", "graph_coloring"),
    ),
}
METRICS = ("makespan", "on_time_delivery_rate", "mean_tardiness", "resource_utilization",
           "waiting_time", "transport_time")


def dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_csv(path, rows):
    if not rows:
        return
    fields = list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def make_jobs(study, configuration, spec_key, seeds, orders):
    values = dict(configuration["scenario"]["config"])
    values.update(domain=spec_key, orders=orders, seeds=tuple(seeds))
    values["policies"] = tuple(ResponsePolicy(policy) for policy in values["policies"])
    base = Study2Config(**values)
    for seed in seeds:
        for name, _, policy, region, selector in METHODS[study]:
            yield (study, name, seed, replace(base, policies=(ResponsePolicy(policy),),
                                             region_rule=region, site_selector=selector))


def paper_rows(study, rows):
    labels = {method[0]: method[1] for method in METHODS[study]}
    return [dict(study=study, seed=row["seed"], orders=row["orders"], method=row["method"],
                 method_label=labels[row["method"]], **{key: row[key] for key in METRICS}) for row in rows]


def run(args):
    configuration = json.loads(args.configuration.read_text(encoding="utf-8"))
    seeds = configuration["seeds"][:2] if args.smoke else configuration["seeds"]
    orders = 10 if args.smoke else configuration["orders"]
    candidate = configuration["scenario"]
    spec = register_physical_scenario(candidate)
    model = build_target_model(spec)
    expected_model = json.loads(args.target_model.read_text(encoding="utf-8"))
    if model != expected_model:
        raise ValueError("Generated factory model differs from configs/studies23_target_model.json")
    args.out.mkdir(parents=True, exist_ok=False)
    model_path = args.out / "target_model.json"
    dump(model_path, model)
    jobs = list(make_jobs(args.study, configuration, spec.key, seeds, orders))
    dump(args.out / "configuration.json", configuration)
    dump(args.out / "run.json", dict(study=args.study, seeds=seeds, orders=orders,
                                     methods=[m[0] for m in METHODS[args.study]], smoke=args.smoke))
    rows, failures = [], []
    details = args.out / "details"
    details.mkdir()
    started = time.monotonic()
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(run_candidate, job, model_path, candidate): job for job in jobs}
        for index, future in enumerate(as_completed(futures), 1):
            job = futures[future]
            try:
                metrics, events, calendar = future.result()
                rows.append(dict(candidate=candidate["name"], **metrics))
                with gzip.open(details / f"{job[1]}_{job[2]}.json.gz", "wt", encoding="utf-8") as stream:
                    json.dump(dict(metrics=metrics, events=events, calendar=calendar), stream,
                              ensure_ascii=False, separators=(",", ":"))
            except Exception as error:
                failures.append(dict(method=job[1], seed=job[2], error=f"{type(error).__name__}: {error}"))
            if index % 10 == 0 or index == len(jobs):
                rows.sort(key=lambda row: (row["seed"], row["method"]))
                write_csv(args.out / "all_metrics.csv", rows)
                write_csv(args.out / "results.csv", paper_rows(args.study, rows))
                dump(args.out / "failures.json", failures)
                print(f"{index}/{len(jobs)} completed; failures={len(failures)}", flush=True)
    dump(args.out / "completion.json", dict(expected=len(jobs), completed=len(rows),
         failed=len(failures), elapsed_s=time.monotonic() - started))
    if failures:
        raise RuntimeError(f"{len(failures)} simulations failed; see failures.json")
    summary = []
    for name, label, *_ in METHODS[args.study]:
        selected = [row for row in rows if row["method"] == name]
        summary.append(dict(method=name, method_label=label, seeds=len(selected),
                       **{key: sum(row[key] for row in selected) / len(selected) for key in METRICS}))
    write_csv(args.out / "summary.csv", summary)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--study", choices=tuple(METHODS), required=True)
    parser.add_argument("--configuration", type=Path, required=True)
    parser.add_argument("--target-model", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--workers", type=int, default=2)
    options = parser.parse_args()
    if options.workers < 1:
        parser.error("--workers must be positive")
    run(options)
