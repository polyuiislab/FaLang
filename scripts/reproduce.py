"""Run the paper's study settings; --smoke runs a smaller command check."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

from verify_data import ROOT, read_rows, require, verify
from source_fingerprints import study1_source_digests


def compare_rows(study, rows, reference, keys, source_digests=None):
    source_digests = source_digests or {}
    actual = {tuple(row[key] for key in keys): row for row in rows}
    original = {tuple(row[key] for key in keys): row for row in reference}
    require(len(actual) == len(rows) and actual.keys() == original.keys(), f"Result coverage differs: {study}")
    compared = 0
    for identity, expected in original.items():
        for field, value in expected.items():
            if field.endswith(("_s", "_ms")):
                continue
            got = actual[identity].get(field)
            if field in source_digests:
                require(got == source_digests[field], f"Source digest differs: {study}/{field}")
            elif got != value:
                try:
                    equal = math.isclose(float(got), float(value), rel_tol=1e-8, abs_tol=1e-8)
                except (ValueError, TypeError):
                    equal = False
                require(equal, f"Result differs: {study}/{identity}/{field}: {got} != {value}")
            compared += 1
    return compared


def check_results(study, directory, expected, smoke):
    path = directory / ("study1_records.csv" if study == "study1" else "results.csv")
    rows = read_rows(path)
    seeds = expected["seeds"][:2] if smoke else expected["seeds"]
    orders = 10 if smoke else expected["orders"]
    keys = ("seed", "domain", "ft_state") if study == "study1" else ("seed", "method")
    found = {tuple(row[key] for key in keys) for row in rows}
    wanted = ({(str(seed), domain, state) for seed in seeds
               for domain in expected["domains"] for state in expected["states"]} if study == "study1"
              else {(str(seed), method) for seed in seeds for method in expected["methods"]})
    require(found == wanted and len(rows) == len(wanted), f"Missing or duplicate records: {study}")
    require(all(int(row["orders"]) == orders for row in rows), f"Order count differs: {study}")
    checked = 0
    if study != "study1":
        raw = read_rows(directory / "all_metrics.csv")
        require({(r["seed"], r["method"]) for r in raw} == wanted and len(raw) == len(wanted),
                f"Detailed result coverage differs: {study}")
        require(all(int(r["capacity_violations"]) == 0 and int(r["ordinary_reassignments"]) == 0
                    for r in raw), f"Physical audit failed: {study}")
    if not smoke:
        if study == "study1":
            checked = compare_rows(study, rows, read_rows(ROOT / expected["main_csv"]), keys,
                                   study1_source_digests(ROOT / expected["runtime"]))
        else:
            checked = compare_rows(study, raw, read_rows(ROOT / expected["all_metrics"]), keys)
    return {"study": study, "records": len(rows), "orders": orders, "seeds": seeds,
            "reference_match": not smoke, "compared_fields": checked}


def prepare_runtime(output, relative):
    runtime = output / ("_" + Path(relative).name)
    if not runtime.exists():
        shutil.copytree(ROOT / relative, runtime, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    return runtime


def prepare_study1_reference(output, runtime):
    reference = output / "_study1_reference"
    reference.mkdir()
    source = json.loads((ROOT / "data/study1/source_manifest.json").read_text(encoding="utf-8"))
    import hashlib
    source["source_sha256"] = {
        name: hashlib.sha256((runtime / name).read_bytes()).hexdigest()
        for name in source["source_sha256"]}
    (reference / "manifest.json").write_text(json.dumps(source, indent=2), encoding="utf-8")
    with zipfile.ZipFile(reference / "source_snapshot.zip", "w", zipfile.ZIP_DEFLATED) as archive:
        for name in source["source_sha256"]:
            archive.write(runtime / name, name)
    return reference


def run(args):
    verify()
    require(args.workers >= 1, "--workers must be positive")
    manifest = json.loads((ROOT / "data/manifest.json").read_text(encoding="utf-8"))
    selected = list(manifest["studies"]) if args.study == "all" else [args.study]
    mode = "smoke" if args.smoke else "full"
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    output = args.out or Path("results") / f"{mode}_{stamp}"
    output = (ROOT / output).resolve() if not output.is_absolute() else output.resolve()
    results_root = (ROOT / "results").resolve()
    require(output != results_root and output.is_relative_to(results_root),
            "--out must be a new subdirectory of this project's results/")
    require(not output.exists(), f"Refusing to overwrite existing output: {output}")
    output.mkdir(parents=True)
    report = {"mode": mode, "status": "running", "studies": selected, "results": []}
    report_path = output / "reproduction.json"

    def save_report():
        report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    save_report()
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1")
    try:
        for study in selected:
            expected = manifest["studies"][study]
            runtime = prepare_runtime(output, expected["runtime"])
            destination = output / study
            if study == "study1":
                reference = prepare_study1_reference(output, runtime)
                command = [sys.executable, "-B", "-X", "utf8", "run_paper1_study1_formal.py",
                           "--reference", str(reference), "--out", str(destination),
                           "--seeds", str(2 if args.smoke else len(expected["seeds"])),
                           "--orders", str(10 if args.smoke else expected["orders"]),
                           "--seed-start", str(expected["seeds"][0]), "--workers", str(args.workers)]
            else:
                command = [sys.executable, "-B", "-X", "utf8", "run_studies.py",
                           "--study", study, "--configuration", str(ROOT / expected["configuration"]),
                           "--target-model", str(ROOT / expected["target_model"]),
                           "--out", str(destination), "--workers", str(args.workers)]
                if args.smoke:
                    command.append("--smoke")
            print(f"{study}: {2 if args.smoke else len(expected['seeds'])} seeds x "
                  f"{10 if args.smoke else expected['orders']} orders", flush=True)
            log = output / f"{study}.log"
            with log.open("w", encoding="utf-8") as stream:
                process = subprocess.run(command, cwd=runtime, env=env, stdout=stream, stderr=subprocess.STDOUT)
            if process.returncode:
                print("\n".join(log.read_text(encoding="utf-8").splitlines()[-12:]), file=sys.stderr)
                raise RuntimeError(f"{study} failed; see {log}")
            report["results"].append(check_results(study, destination, expected, args.smoke))
            save_report()
        verify()
    except Exception as error:
        report.update(status="failed", error=str(error))
        save_report()
        raise
    report["status"] = "PASS"
    save_report()
    print(f"PASS: {output.relative_to(ROOT).as_posix()}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--study", choices=("all", "study1", "study2", "study3"), default="all")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--smoke", action="store_true", help="Use 2 seeds and 10 orders for a quick check")
    mode.add_argument("--full", dest="smoke", action="store_false", help=argparse.SUPPRESS)
    parser.set_defaults(smoke=False)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--out", type=Path, help="New output directory below results/")
    run(parser.parse_args())
