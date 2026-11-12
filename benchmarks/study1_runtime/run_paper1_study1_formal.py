"""Run paired factory states for independently generated orders in three domains."""
import argparse
import csv
import hashlib
import json
import shutil
import time
import zipfile
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from statistics import mean

from case_study.domains import DOMAIN_KEYS, materialize_all
from case_study.study1 import FT_STATES, run_study1


def run_seed(seed, orders, out):
    paths = run_study1(orders, seed, out / f"seed_{seed}")
    records = json.loads(paths["records_json"].read_text(encoding="utf-8"))
    return [{"seed": seed, **record} for record in records]


def write_csv(path, rows):
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def audit(records, seeds, orders):
    expected = {(seed, domain, state) for seed in seeds
                for domain in DOMAIN_KEYS for state in FT_STATES}
    assert len(records) == len(expected)
    assert {(r["seed"], r["domain"], r["ft_state"]) for r in records} == expected
    assert all(r["orders"] == r["compiled_programs"] == orders for r in records)
    assert all(r["realization_success"] == 1 for r in records)
    assert all(r["compile_failures"] == r["memory_spills"] == 0 for r in records)
    assert len({r["grammar_digest"] for r in records}) == 1
    assert len({r["compiler_core_digest"] for r in records}) == 1
    for seed in seeds:
        for domain in DOMAIN_KEYS:
            group = [r for r in records if r["seed"] == seed and r["domain"] == domain]
            assert len({r["program_source_digest"] for r in group}) == 1


def run(args):
    root = Path(__file__).resolve().parent
    out = args.out
    out.mkdir(parents=True, exist_ok=True)
    reference = json.loads((args.reference / "manifest.json").read_text(encoding="utf-8"))
    hashes = reference["source_sha256"]

    def check_sources():
        assert all(hashlib.sha256((root / name).read_bytes()).hexdigest() == digest
                   for name, digest in hashes.items()), "正式实验源码已变化"

    check_sources()
    seeds = list(range(args.seed_start, args.seed_start + args.seeds))
    models = materialize_all()
    shutil.copyfile(args.reference / "source_snapshot.zip", out / "source_snapshot.zip")
    with zipfile.ZipFile(out / "source_snapshot.zip", "a", zipfile.ZIP_DEFLATED) as archive:
        archive.write(__file__, Path(__file__).name)
        archive.write(root / "core/factory_target_model.json", "core/factory_target_model.json")
        for path in models:
            archive.write(path, "domain_models/" + path.name)
    (out / "manifest.json").write_text(json.dumps({
        "seeds": seeds, "orders_per_domain_per_seed": args.orders,
        "domains": list(DOMAIN_KEYS), "states": list(FT_STATES),
        "source_sha256": hashes,
        "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "models": {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in models},
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    records = []
    started = time.monotonic()
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(run_seed, seed, args.orders, out): seed for seed in seeds}
        for count, future in enumerate(as_completed(futures), 1):
            records.extend(future.result())
            write_csv(out / "study1_records.csv", sorted(
                records, key=lambda r: (r["seed"], r["domain"], r["ft_state"])))
            print(f"[{count}/{len(seeds)}] seed={futures[future]} "
                  f"elapsed={time.monotonic()-started:.0f}s", flush=True)
    check_sources()
    audit(records, seeds, args.orders)
    summary = []
    for domain in DOMAIN_KEYS:
        for state in FT_STATES:
            group = [r for r in records if r["domain"] == domain and r["ft_state"] == state]
            summary.append({
                "domain": domain, "ft_state": state, "seeds": len(seeds),
                "orders_per_seed": args.orders,
                "compiled_programs": sum(r["compiled_programs"] for r in group),
                "realization_success": mean(r["realization_success"] for r in group),
                "immediate_placement": mean(r["immediate_placement"] for r in group),
                "compile_failures": sum(r["compile_failures"] for r in group),
                "memory_spills": sum(r["memory_spills"] for r in group),
                "grammar_modification": 0, "compiler_core_modification": 0,
                "source_modification_across_Ft": 0,
            })
    write_csv(out / "study1_summary.csv", summary)
    (out / "audit.json").write_text(json.dumps({
        "records": len(records), "checks_passed": True,
        "distinct_domain_seed_orders": len(seeds) * len(DOMAIN_KEYS) * args.orders,
        "compiled_programs_across_states": sum(r["compiled_programs"] for r in records),
        "elapsed_s": time.monotonic() - started,
    }, indent=2), encoding="utf-8")
    print("Study 1 formal complete", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--seeds", type=int, default=100)
    parser.add_argument("--orders", type=int, default=100)
    parser.add_argument("--seed-start", type=int, default=70000)
    parser.add_argument("--workers", type=int, default=8)
    run(parser.parse_args())
