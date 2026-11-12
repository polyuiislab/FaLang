# Factory target model

The target model describes the resources and physical constraints used by the compiler and simulation. The IDE loads `core/factory_target_model.json`; case models are stored in `domain_models/`.

## Model sections

| Section | Main fields and purpose |
|---|---|
| `instruction_capability_set` | `supported_instructions` defines operation codes and `supported_rp` values. `palu_definitions` lists processing units, quantities, and supported capabilities. |
| `addressing_model` | `storage_regions` identifies memory and register locations. `workstation_mapping` associates registers and PALUs with workstations. |
| `storage_model` | `capacity_constraints` gives each storage location's ID, type, role, and capacity. |
| `runtime_environment` | `catch_triggers` associates exceptions with operations. Exception codes and catch timing parameters support exception handling. |
| `scheduling_model` | Processing times, transport times or layout, and setup costs provide scheduling weights. |
| `case_study_model` | The default simulation's product variants, arrivals, replanning triggers, and dispatch weights. |

## Operations and capabilities

An operation is identified by `opcode`, `func`, and `rp`. For example, the default model maps `CPU_Install` to PROCESS (`opcode=000001`, `func=001`, `rp=00010`). The same key appears in compatible PALU capabilities, processing times, and any exception triggers.

`core/target_model.py` resolves operation descriptions and aliases to these keys. The main operations are:

| FaLang operation | Opcode | Function |
|---|---|---|
| `Process` | `000001` | `001` |
| `Move`, `Assign` | `000001` | `010` |
| `Inspect` | `000001` | `011` |
| `Adjust` | `000001` | `100` |
| `Load` | `100101` | `000` |
| `Store` | `101101` | `000` |

In FISA output, `RP` encodes the operation parameter and the three-bit `operator` field selects the assigned PALU. `compiler/codegen.py` emits these 32-bit formats:

```text
R-type: OP(6) | RS(5) | RT(5) | RP(5) | RD(5)        | func(3) | operator(3)
I-type: OP(6) | RS(5) | RT(5) | RP(5) | immediate(8)           | operator(3)
```

## Locations and timing

Registers represent physical material-holding locations associated with workstations; memory represents warehouse storage. Capacity entries use `type: "register"` or `type: "memory"`. The model connects location IDs, workstation assignments, and available processing capabilities.

The default model derives transport times from `logistics_layout` and `standard_logistics_times`. The loader also accepts an explicit `logistics_time_matrix` with a `storage_index_map`. Processing and setup times use a consistent time unit throughout a model.

## Selecting a model

Run from `F_IDE_Project/`:

```python
from core.compiler_proxy import CompilerProxy

compiler = CompilerProxy(target_model_path="domain_models/pc_assembly.json")
```

Domain definitions and generators are in `case_study/domains/`. The reproduction scripts select the archived inputs for each formal study; their commands and data provenance are described in [Reproduction](../docs/reproduction.md).
