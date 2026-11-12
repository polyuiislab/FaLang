"""Verify reference data, shared study settings, and executable source checksums."""
from __future__ import annotations

import csv
import hashlib
import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
METRICS = ("makespan", "on_time_delivery_rate", "mean_tardiness", "resource_utilization",
           "waiting_time", "transport_time")


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def verify(root: Path = ROOT) -> dict:
    root = root.resolve()
    manifest = json.loads((root / "data/manifest.json").read_text(encoding="utf-8"))
    for collection in ("files", "runtime_files"):
        require(bool(manifest[collection]), f"Missing checksum inventory: {collection}")
        for name, item in manifest[collection].items():
            path = (root / name).resolve()
            require(path.is_relative_to(root), f"Invalid manifest path: {name}")
            require(path.is_file(), f"Missing file: {name}")
            require(hashlib.sha256(path.read_bytes()).hexdigest() == item["sha256"],
                    f"Hash mismatch: {name}")
    actual_data = {path.relative_to(root).as_posix() for directory in ("data", "configs")
                   for path in (root / directory).rglob("*") if path.is_file()
                   and path != root / "data/manifest.json"}
    require(actual_data == set(manifest["files"]), "Unlisted or missing study data/configuration")
    configuration = json.loads((root / "configs/studies23.json").read_text(encoding="utf-8"))
    counts = {}
    for study, expected in manifest["studies"].items():
        rows = read_rows(root / expected["main_csv"])
        require(len(rows) == expected["rows"], f"Unexpected record count: {study}")
        require({int(row["seed"]) for row in rows} == set(expected["seeds"]), f"Unexpected seeds: {study}")
        require(all(int(row["orders"]) == expected["orders"] for row in rows), f"Order count: {study}")
        if study == "study1":
            keys = {(int(r["seed"]), r["domain"], r["ft_state"]) for r in rows}
            wanted = {(seed, domain, state) for seed in expected["seeds"]
                      for domain in expected["domains"] for state in expected["states"]}
            require(keys == wanted and len(keys) == len(rows), "Study 1 coverage differs")
            require(sum(int(r["compiled_programs"]) for r in rows) == 22500, "Study 1 compiling count")
        else:
            require(expected["seeds"] == configuration["seeds"] == list(range(182000, 182050)),
                    f"Study seeds differ from shared settings: {study}")
            require(expected["orders"] == configuration["orders"] == 50, f"Shared order count: {study}")
            keys = {(int(r["seed"]), r["method"]) for r in rows}
            wanted = {(seed, method) for seed in expected["seeds"] for method in expected["methods"]}
            require(keys == wanted and len(keys) == len(rows), f"Method/seed coverage: {study}")
            raw = read_rows(root / expected["all_metrics"])
            indexed = {(int(r["seed"]), r["method"]): r for r in raw}
            require(len(raw) == len(indexed) == len(wanted) and set(indexed) == wanted,
                    f"Detailed results coverage: {study}")
            for row in rows:
                original = indexed[int(row["seed"]), row["method"]]
                require(row["study"] == study, f"Wrong study label: {study}")
                for metric in METRICS:
                    value = float(row[metric])
                    require(math.isfinite(value) and value == float(original[metric]),
                            f"Paper metric differs: {study}/{row['seed']}/{row['method']}/{metric}")
                require(int(original["capacity_violations"]) == 0
                        and int(original["ordinary_reassignments"]) == 0, f"Physical audit: {study}")
        counts[study] = len(rows)
    return {"status": "PASS", "records": counts, "total_records": sum(counts.values()),
            "data_files": len(manifest["files"]), "runtime_files": len(manifest["runtime_files"])}


if __name__ == "__main__":
    print(json.dumps(verify(), indent=2))
