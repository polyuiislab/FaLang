# PSE support: compiling and simulation

## F-IDE

`F_IDE_Project/main.py` starts the Tkinter interface. `gui/app_window.py` calls `core/compiler_proxy.py`.

`CompilerProxy.compile_source()` performs parsing, semantic checks, CFG/SSA construction, manufacturing dependency analysis, scheduling, PBQP allocation, and post-allocation scheduling. It generates FISA/COE output and Function Block configurations. Graph export requires Graphviz; use `emit_outputs=False` to disable file export.

## Representation support across PSE

`CompilerProxy.compile_order_program()` represents order requirements independently of equipment selection. Planning produces FaLang programs from order requirements and process rules; scheduling derives resource allocations through compiling; execution supplies progress and factory state for residual representations and uncertainty responses.

| Module | Purpose |
|---|---|
| `compiler/residual_ir.py` | Order programs, residual worksets, and material versions |
| `compiler/factory_target_machine.py` | Factory configuration and operational states |
| `compiler/residual_coloring.py` | RGC and resource selection rules |
| `compiler/regional_residual.py` | Regional resource reallocation and HUR/CBC uncertainty responses |
| `compiler/uncertainty.py` | Uncertainty events, response policies, and boundary criteria |
| `compiler/target_lowering.py` | Binding order operations to target capabilities before resource allocation |

`benchmarks/study1_runtime/` evaluates representation reuse across three domains and three factory states. `benchmarks/pse_runtime/` supplies the shared discrete-event simulator for Studies 2 and 3. Study 2 compares four allocation methods; Study 3 compares six response-policy/boundary combinations. Both read `configs/studies23.json` and use the same factory model. See [the reproduction commands](reproduction.md).

`scripts/demo_representations.py` uses the case-study PC program generator, `compile_order_program()`, and `WorksetComposer` to export a readable order/residual snapshot. It does not change compiler behavior or run the simulator. [The walkthrough](ide_walkthrough.md) distinguishes these outputs from the GUI's FISA/COE pipeline.

## Models and physical semantics

The IDE uses `core/factory_target_model.json`. Case models are stored in `domain_models/`; `case_study/domains/` defines capabilities, process rules, and seeded order generation.

Material handling follows linear ownership semantics. Compiling and execution-time resource allocation account for capability matching, exclusive equipment use, holding capacity, transfer times, and exception control flow. `scripts/reproduce.py` runs the designated study implementation in a new output directory and compares full-run results with `data/study1/`, `data/study2/`, or `data/study3/`.
