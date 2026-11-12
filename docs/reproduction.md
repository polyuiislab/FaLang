# Reproduce the case studies

Run commands from the repository root (the directory containing `README.md`) with Python 3.14. The simulator uses the standard library. F-IDE and Graphviz are not required for simulation.

## Start Study 2 or Study 3

```sh
python scripts/reproduce.py --study study2
python scripts/reproduce.py --study study3
```

The default is the paper's full design: **50 seeds (182000–182049), 50 orders per seed**. Both studies use the same AHU factory, order generator, and uncertainty generator in [studies23.json](../configs/studies23.json), with the model in [studies23_target_model.json](../configs/studies23_target_model.json).

Study 2 changes the resource-allocation method while retaining HUR with CBC. Study 3 changes the response policy or revision boundary while retaining RGC.

| Study | Method identifier in CSV | Paper label |
|---|---|---|
| 2 | `graph_coloring` | RGC |
| 2 | `first_fit` | First-fit |
| 2 | `machine_transport_joint` | Transfer priority |
| 2 | `load_balance` | Load priority |
| 3 | `hybrid` | HUR (CBC) |
| 3 | `predefinition` | Predefined |
| 3 | `reactive` | Reactive |
| 3 | `hybrid@event_order` | HUR (Order) |
| 3 | `hybrid@machine_group` | HUR (Group) |
| 3 | `hybrid@whole_workset` | HUR (All) |

The full runs contain **200 Study 2 records** and **300 Study 3 records**. Each record describes one method and seed, simulating 50 orders.

For a quick command check, use two seeds and ten orders:

```sh
python scripts/reproduce.py --study study2 --smoke
python scripts/reproduce.py --study study3 --smoke
```

These smaller runs check execution and output coverage; their metrics are not compared with the paper's full-design values.

To run Study 1 or all studies:

```sh
python scripts/reproduce.py --study study1
python scripts/reproduce.py --study all
```

Study 1 uses seeds 130000–130049, 50 orders per domain, three domains, and three paired factory states. It produces 450 records. Add `--smoke` to check all three studies quickly.

## Shared Study 2/3 settings

| Setting | Value |
|---|---|
| Workstations | 2 preparation, 3 assembly/sealing, 2 inspection, 1 packaging, 1 adjustment bay |
| Concurrent operations | 2 at assembly/sealing stations; 1 elsewhere |
| Holding sites | 9 sites, capacity 12 per site |
| AGVs | 5 |
| Nominal operation times | Preparation 4.5 min; assembly/sealing 9 min; inspection/packaging 6 min; loading/unloading 5 min |
| Transfer time | 4 min plus 0.75 min per layout-distance unit |
| Production-time variation | Uniform multiplier over [0.85, 1.15] |
| Order release | 50 orders at time zero; at most 8 admitted concurrently |
| Due dates | 2.5 times nominal bottleneck workload, weighted by relative order delivery allowance |
| Material shortage | Probability 0.60 at initial loading |
| Machine failure | Probability 0.40 per eligible preparation or inspection startup |
| Holding-site unavailability | Probability 0.24 per eligible transfer to another workstation for processing or inspection |
| Resource-event eligibility | Requires a backup channel; at most one simultaneous startup outage per stage |
| Resource restoration | 80 min |
| Response operations | Replenishment loading 5 min; adjustment 7 min; preparation after workstation change 12 min |

The configuration includes the order options and exact event-trigger lists. Resource events depend on operation starts and transfers, so their realized times and targets can differ between methods even with the same seed. The AHU scenario's internal model identifier is `garment_continuation_compact`; configuration and output files use this identifier.

## Output directory and files

Each command prints the directory created for the run:

- Full design: `results/full_<timestamp>/`
- Quick check: `results/smoke_<timestamp>/`
- Custom name: append `--out results/my_study2_run`

An output directory must be new. Existing directories are rejected. Set worker count with `--workers 4` (default: 2).

For a Study 2 run, inspect:

| Path within the run directory | Contents |
|---|---|
| `reproduction.json` | Overall status and reference-comparison result |
| `study2.log` | Simulation progress and error messages |
| `study2/results.csv` | Six paper metrics for each seed and method |
| `study2/summary.csv` | Mean of each metric by method |
| `study2/all_metrics.csv` | Complete simulation metrics, including physical audits and response measurements |
| `study2/configuration.json` | Shared experiment settings |
| `study2/run.json` | Actual seeds, order count, methods, and run mode |
| `study2/target_model.json` | Factory model used for this run |
| `study2/details/<method>_<seed>.json.gz` | Compressed metrics, event records, and event calendar |
| `study2/completion.json`, `study2/failures.json` | Completed/failed run counts and failure information |

Study 3 uses the same filenames under `study3/`. Study 1 writes `study1/study1_records.csv`, `study1/study1_summary.csv`, and per-seed tables under `study1/seed_<seed>/`. Directories beginning with `_` contain isolated source copies used by the run.

Open CSV files in a spreadsheet application. Time metrics use minutes; delivery and utilization rates are fractions (multiply by 100 for percentages). The six paper columns are `makespan`, `on_time_delivery_rate`, `mean_tardiness`, `resource_utilization`, `waiting_time`, and `transport_time`.

To inspect a compressed event file using Python, replace the path with an actual output file:

```python
import gzip
import json

with gzip.open("results/my_study2_run/study2/details/graph_coloring_182000.json.gz",
               "rt", encoding="utf-8") as stream:
    detail = json.load(stream)
print(detail.keys())  # metrics, events, calendar
```

The simulation runs without a graphical animation. To inspect CFGs and residual states, use the [representation walkthrough](ide_walkthrough.md#inspect-the-residual-representation).

## Check completion and reference results

A successful command ends with `PASS: results/...` and writes `"status": "PASS"` to `reproduction.json`. Full runs compare the non-timing results against the supplied references, with numerical tolerance for floating-point values. CPU timing fields ending in `_s` or `_ms` are excluded. Study 1 source-text digests are checked against its supplied compiler source.

Reference data are in:

- `data/study1/study1_records.csv`
- `data/study2/results.csv` and `data/study2/all_metrics.csv`
- `data/study3/results.csv` and `data/study3/all_metrics.csv`

Check data, configuration, and runtime integrity independently with:

```sh
python scripts/verify_data.py
```

On failure, inspect the study log and `failures.json`. To follow a running log from another PowerShell terminal:

```powershell
Get-Content 'results/my_study2_run/study2.log' -Tail 20 -Wait
```

An interrupted run retains its partial outputs. Start a new run in another directory; the command does not resume an existing directory.
