# FaLang / F-IDE

This project implements the Factory Language Manufacturing Representation Framework (FMRF) and its language, Factory Language (FaLang), to support **planning, scheduling, and execution (PSE)** in customized production. Planning expresses order requirements and uncertainty responses as FaLang programs. Scheduling uses compiling and Residual Graph Coloring (RGC) to allocate resources under factory constraints. Execution maintains residual representations and uses Hybrid Uncertainty Response (HUR) with the Containment Boundary Criterion (CBC) to coordinate responses and delimit scheduling revision.

This repository includes the Tkinter F-IDE, compiler, factory models, examples, and case-study simulator. Start with the IDE walkthrough, inspect an order and its residual representation, then run the case studies.

## 1. Open F-IDE and compile a production task

Use Python 3.14 with Tkinter (validated on Windows). Core compiling and simulation use the Python standard library. For CFG/MDG graphics, also install the **Graphviz application** and put its `bin` directory on `PATH`; installing a Python package named `graphviz` alone is insufficient. Check the installation with `dot -V`. Optional plotting and packaging dependencies are in `F_IDE_Project/requirements-optional.txt`.

Clone the repository and enter its root directory, which contains this README:

```sh
git clone https://github.com/polyuiislab/FaLang.git
cd FaLang
```

Open F-IDE from the repository root:

```sh
cd F_IDE_Project
python main.py
```

1. Click **Open**, then select `examples/pc_mms_order_demo.falang`.
2. This example assembles a customized PC, tests it, checks accessories, packages it, and returns it to storage. Its two reusable tasks also declare responses to material shortages and machine failures.
3. Click **Compile & Build**. Read the compiler messages in the bottom panel. On success, choose where to save `pc_mms_order_demo.coe` in the save dialog.
4. Open **`F_IDE_Project/factory_ssa_cfg.html`** in a browser to inspect the CFG. Keep `factory_ssa_cfg.svg` beside it. The SVG can also be opened directly.
5. Open `factory_mdg.svg` and `factory_mdg_post_ra.svg` for manufacturing dependencies before and after allocation. Function Block configurations are in `factory_fb_config.json` and `factory_fb_config.xml`.

These generated files are written to the IDE's working directory and replaced by the next build. The `.coe` file goes to the location selected in the save dialog. The default model is `core/factory_target_model.json`.

See the [F-IDE and example walkthrough](docs/ide_walkthrough.md) for the production steps, output meanings, other examples, and troubleshooting.

## 2. Open the residual representation

The IDE's **Compile & Build** button runs the FISA/COE backend. The case studies use `compile_order_program()` and RGC. To inspect the latter's order representation and residual states, return to the repository root and run:

```sh
python scripts/demo_representations.py
```

Open the printed **`results/demo_<timestamp>/index.html`** in a browser. It shows a generated PC order, its CFG, and residual states before execution and after three completed operations with the next operation running. Expand **Inspect annotated CFG instructions** to see which instructions are frozen, carried, pending, or inactive handlers. The same directory contains `order.falang`, `representations.json`, and the CFG DOT/SVG files.

This is an illustrative progress snapshot using the actual compiler and workset composer; progress is supplied, not measured in a simulation. See [Residual representation walkthrough](docs/ide_walkthrough.md#inspect-the-residual-representation) for parameters and interpretation. The HTML tables and JSON work without Graphviz.

## 3. Run the case studies

From the repository root, run either study directly:

```sh
python scripts/reproduce.py --study study2
python scripts/reproduce.py --study study3
```

Both commands use the paper's **50 seeds × 50 orders**, the shared AHU factory configuration, and seeds **182000–182049**. **Study 2** compares four resource-allocation methods. **Study 3** compares six uncertainty-response methods. Settings are in [configs/studies23.json](configs/studies23.json); the factory model is in [configs/studies23_target_model.json](configs/studies23_target_model.json).

For a quick check using two seeds and ten orders, add `--smoke`. To run all three studies, use `python scripts/reproduce.py --study all`. Set parallel workers with `--workers 4`.

Each run prints a new **`results/full_<timestamp>/`** directory (`smoke_<timestamp>` with `--smoke`). Open:

- `study2/results.csv` or `study3/results.csv`: the six metrics reported in the paper, by seed and method.
- `study2/summary.csv` or `study3/summary.csv`: mean metrics for each method.
- `reproduction.json`: completion status and comparison against the supplied reference data.

Full runs check the non-timing results against `data/study2/all_metrics.csv` or `data/study3/all_metrics.csv`. Timing measurements depend on the computer. See [Case-study reproduction](docs/reproduction.md) for the complete settings, method names, event records, and output files.

## Project layout

| Directory | Contents |
|---|---|
| `F_IDE_Project/` | IDE, compiler, models, examples, and tests |
| `benchmarks/study1_runtime/` | Study 1 representation experiment |
| `benchmarks/pse_runtime/` | Shared Study 2/3 simulator and compiler |
| `configs/` | Shared Study 2/3 configuration and factory model |
| `data/` | Reference study results and integrity checksums |
| `scripts/` | Representation demo, data verification, and reproduction commands |
| `docs/` | IDE walkthrough, architecture, and study reproduction |
| `results/` | Newly generated demonstrations and simulation runs (ignored by Git) |

## Tests and packaging

From `F_IDE_Project/`:

```sh
python -B -m unittest discover -s compiler/tests
```

For executable packaging, from the same directory:

```sh
python -m pip install -r requirements-optional.txt
python -m PyInstaller f_ide.spec
```

The executable is written to `dist/`. Tests for the demonstration exporter run from the repository root with `python -B -m unittest discover -s scripts/tests`.

## Documentation

- [F-IDE, examples, CFG, and residual representation](docs/ide_walkthrough.md)
- [Case-study reproduction](docs/reproduction.md)
- [Architecture and entry points](docs/architecture.md)
- [Target models](F_IDE_Project/target_model.md)
- [Scheduling](F_IDE_Project/compiler/schedule_design.md)

## License

Code, models, simulation data, and documentation use the [MIT License](LICENSE). Asset authorization is recorded in [NOTICE](NOTICE).
