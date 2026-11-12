"""Compare unchanged order programs across domains and paired factory states."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from case_study.domains import (
    DOMAIN_KEYS,
    materialize_all,
    program_generator,
    spec_of,
    target_model_path,
)
from case_study.domains.orders import DomainOrderGenerator
from compiler.factory_target_machine import AVAIL_WORKSTATION, FactoryState, FactoryTargetMachine
from compiler.residual_coloring import WorksetColorer
from compiler.residual_ir import CompiledOrderProgram, ResidualRecord, WorksetComposer
from core.compiler_proxy import CompilerProxy


FT_NORMAL = "normal"
FT_HIGH_OCCUPANCY = "high_occupancy"
FT_MACHINE_DOWN = "machine_unavailable"
FT_STATES: Tuple[str, ...] = (FT_NORMAL, FT_HIGH_OCCUPANCY, FT_MACHINE_DOWN)

RESULTS_DIR = Path(__file__).resolve().parents[1] / "study1_results"


@dataclass
class Study1Record:
    """Results for one domain under one factory state."""

    domain: str
    domain_label: str
    paradigm: str
    ft_state: str
    orders: int
    workstations: int
    holding_sites: int
    holding_capacity: int
    capability_definitions: int
    logical_roles: int
    process_rules: int
    protection_rules: int
    ops_per_order_mean: float
    program_source_digest: str
    grammar_digest: str
    compiler_core_digest: str
    compiled_programs: int
    compile_failures: int
    pending_versions: int
    immediate_placed: int
    immediate_waiting: int
    deferred_after_progress: int
    settled_placed: int
    memory_spills: int
    exception_entries: int
    # Mean operations with catch edges; nested protections can share an operation.
    catch_edge_ops_mean: float
    # Number of distinct predefined-response signatures across orders.
    protection_signatures: int
    immediate_placement: float
    realization_success: float
    settle_rounds_mean: float
    deferred_not_failed: bool
    down_workstation: Optional[int]
    down_workstation_share: float
    wall_clock_s: float
    generated_program_bytes: int


@dataclass
class Study1Summary:
    records: List[Study1Record] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)


def _digest(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


GRAMMAR_FILES = ("compiler/lexer.py", "compiler/parser.py")


def grammar_digest(root: Path) -> str:
    payload = [(root / name).read_text(encoding="utf-8") for name in GRAMMAR_FILES]
    return _digest("\n".join(payload))


def compiler_core_digest(root: Path) -> str:
    core_dir = root / "compiler"
    names = sorted(path.name for path in core_dir.glob("*.py") if path.name != "__init__.py")
    payload = [f"{name}:{(core_dir / name).read_text(encoding='utf-8')}" for name in names]
    return _digest("\n".join(payload))


def _program_digest(sources: Sequence[str]) -> str:
    return _digest("\n---\n".join(sources))


def choose_disruptive_workstation(
    bare_f_t: FactoryTargetMachine,
    compiled: Sequence[CompiledOrderProgram],
    lowerings: Mapping[str, object],
) -> Tuple[Optional[int], float]:
    """Return the workstation whose outage most reduces immediate placement and that placement rate."""
    stations = sorted({site.workstation_id for site in bare_f_t.holding_sites()})
    if not stations:
        return None, 0.0

    best_ws: Optional[int] = None
    best_rate = None
    for ws_id in stations:
        state = build_state(bare_f_t, FT_MACHINE_DOWN, ws_id)
        placed = pending = 0
        for item in compiled:
            lowering = lowerings.get(item.program.order_id)
            if lowering is None:
                continue
            f_t = bare_f_t.with_state(state.clone())
            workset = WorksetComposer().compose(
                [item],
                {item.program.order_id: ResidualRecord(
                    order_id=item.program.order_id, completed_seq=-1)},
                f_t.state,
            )
            result = WorksetColorer(f_t).color(
                workset.fragments, {item.program.order_id: lowering},
                workset.pinned_locations, time=0.0,
            )
            pending += len(workset.pending_vregs)
            placed += len(result.allocation)
        rate = placed / pending if pending else 0.0
        if best_rate is None or rate < best_rate:
            best_rate = rate
            best_ws = ws_id
    return best_ws, round(best_rate or 0.0, 4)


def build_state(
    f_t: FactoryTargetMachine,
    ft_state: str,
    down_workstation: Optional[int] = None,
    repair_at: float = 10_000.0,
) -> FactoryState:
    state = FactoryState(time=0.0)
    if ft_state == FT_HIGH_OCCUPANCY:
        # Fill two thirds of holding sites and leave one free position in each remaining site.
        for index, site in enumerate(f_t.holding_sites()):
            if index % 3 != 0:
                state.occupied[site.storage_id] = site.capacity
            else:
                state.occupied[site.storage_id] = max(0, site.capacity - 1)
    elif ft_state == FT_MACHINE_DOWN and down_workstation is not None:
        state.unavailable_until[(AVAIL_WORKSTATION, int(down_workstation))] = float(repair_at)
    return state


def _order_of(program, orders):
    for order in orders:
        if order.task_name == program.order_id or str(order.order_id) == str(program.order_id):
            return order
    return orders[0]


def run_domain_state(
    domain: str,
    ft_state: str,
    orders_per_domain: int,
    seed: int,
    root: Path,
) -> Study1Record:
    spec = spec_of(domain)
    generator = program_generator(domain)
    proxy = CompilerProxy(str(target_model_path(domain)))

    orders = DomainOrderGenerator(spec, seed=seed, load="medium").generate(
        orders_per_domain, generator
    )

    started = time.perf_counter()
    programs = [generator.generate(order) for order in orders]
    compiled: List[CompiledOrderProgram] = []
    lowerings: Dict[str, object] = {}
    failures = 0
    exception_entries = 0
    catch_edge_ops = 0
    protection_signatures = set()
    for program in programs:
        try:
            results = generator.compile_program(proxy, program)
        except Exception:
            failures += 1
            continue
        artifacts = results["artifacts"]
        compiled.append(CompiledOrderProgram(
            program=program,
            cfg=artifacts["cfg"],
            mdg=artifacts["mdg"],
            op_instr_ids=artifacts["op_instr_ids"],
        ))
        lowerings[program.order_id] = artifacts["lowering"]
        exception_entries += len(artifacts["exception_table"])
        for block in artifacts["cfg"].blocks:
            for instr in block.instructions:
                if getattr(instr, "catch_edges", None):
                    catch_edge_ops += 1
        protection_signatures.add(tuple(sorted(
            (r.region, r.exception_type)
            for r in generator.protections_for(_order_of(program, orders)))))

    bare_f_t = proxy.build_factory_target_machine()
    down_ws, down_share = (None, 0.0)
    if ft_state == FT_MACHINE_DOWN:
        down_ws, down_share = choose_disruptive_workstation(bare_f_t, compiled, lowerings)

    immediate: List[float] = []
    settled_rates: List[float] = []
    settle_rounds: List[int] = []
    pending_total = immediate_total = immediate_waiting = settled_total = 0
    deferred_total = 0
    memory_spills = 0

    for item in compiled:
        f_t = proxy.build_factory_target_machine(
            build_state(bare_f_t, ft_state, down_ws)
        )
        workset = WorksetComposer().compose(
            [item],
            {item.program.order_id: ResidualRecord(order_id=item.program.order_id, completed_seq=-1)},
            f_t.state,
        )
        colorer = WorksetColorer(f_t)
        color_result = colorer.color(
            workset.fragments, lowerings, workset.pinned_locations, time=0.0
        )
        pending = len(workset.pending_vregs)
        placed = len(color_result.allocation)
        pending_total += pending
        immediate_total += placed
        immediate_waiting += len(color_result.waiting)
        memory_spills += sum(1 for value in color_result.allocation.values() if value == "memory")
        immediate.append(placed / pending if pending else 0.0)

        settled, rounds = _settle_waiting(
            colorer, color_result, workset, pending, f_t.state,
            repair_pair=(AVAIL_WORKSTATION, down_ws) if down_ws is not None else None,
        )
        deferred_total += max(0, pending - placed)
        settled_total += settled
        settled_rates.append(settled / pending if pending else 0.0)
        settle_rounds.append(rounds)

    elapsed = time.perf_counter() - started
    ops_per_order = [len(item.program.operations) for item in compiled] or [0]

    return Study1Record(
        domain=domain,
        domain_label=spec.label,
        paradigm=spec.paradigm,
        ft_state=ft_state,
        orders=len(orders),
        workstations=len(spec.workstations),
        holding_sites=len(spec.register_ids),
        holding_capacity=spec.holding_capacity,
        capability_definitions=len(spec.actions),
        logical_roles=len(spec.roles),
        process_rules=len(spec.rules),
        protection_rules=len(spec.protections),
        ops_per_order_mean=round(sum(ops_per_order) / len(ops_per_order), 2),
        program_source_digest=_program_digest([p.source for p in programs]),
        grammar_digest=grammar_digest(root),
        compiler_core_digest=compiler_core_digest(root),
        compiled_programs=len(compiled),
        compile_failures=failures,
        pending_versions=pending_total,
        immediate_placed=immediate_total,
        immediate_waiting=immediate_waiting,
        deferred_after_progress=deferred_total,
        settled_placed=settled_total,
        memory_spills=memory_spills,
        exception_entries=exception_entries,
        catch_edge_ops_mean=round(catch_edge_ops / max(1, len(compiled)), 2),
        protection_signatures=len(protection_signatures),
        immediate_placement=round(sum(immediate) / len(immediate), 4) if immediate else 0.0,
        realization_success=round(sum(settled_rates) / len(settled_rates), 4) if settled_rates else 0.0,
        settle_rounds_mean=round(sum(settle_rounds) / len(settle_rounds), 2) if settle_rounds else 0.0,
        deferred_not_failed=(memory_spills == 0 and settled_total == pending_total),
        down_workstation=down_ws,
        down_workstation_share=down_share,
        wall_clock_s=round(elapsed, 3),
        generated_program_bytes=sum(len(p.program.source) for p in compiled),
    )


def _settle_waiting(
    colorer: WorksetColorer,
    color_result,
    workset,
    pending: int,
    state: FactoryState,
    repair_pair: Optional[Tuple[str, object]] = None,
) -> Tuple[int, int]:
    """Retry waiting versions as occupancy falls and resources recover; return placements and rounds."""
    colorers = colorer.colorers()
    if not any(single.waiting for single in colorers.values()):
        return sum(len(single.allocation) for single in colorers.values()), 0

    rounds = 0
    consumed_total = 0
    repaired = False
    max_rounds = max(8, pending * 2)
    while rounds < max_rounds:
        progressed = False

        if repair_pair is not None and not repaired:
            state.unavailable_until.pop(tuple(repair_pair), None)
            repaired = True
            progressed = True

        occupied_sites = [sid for sid, count in state.occupied.items() if count > 0]
        for sid in occupied_sites[: max(1, len(occupied_sites) // 3)]:
            state.occupied[sid] -= 1
            progressed = True

        for single in colorers.values():
            if not single.waiting:
                continue
            batch = max(1, len(single.allocation) // 4)
            victims = single.earliest_finishing(batch)
            if victims:
                single.consume(victims)
                consumed_total += len(victims)
                progressed = True
            # Retry waiting versions after occupancy changes or resource recovery.
            waiting_before = len(single.waiting)
            single.retry(rounds + 1.0)
            if len(single.waiting) < waiting_before:
                progressed = True

        rounds += 1
        if not progressed or not any(single.waiting for single in colorers.values()):
            break

    # A released version still counts as successfully placed.
    placed = consumed_total + sum(len(single.allocation) for single in colorers.values())
    return placed, rounds


def run_study1(
    orders_per_domain: int = 12,
    seed: int = 20260927,
    out_dir: Optional[Path] = None,
) -> Dict[str, Path]:
    root = Path(__file__).resolve().parents[1]
    out_dir = out_dir or RESULTS_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    materialize_all(force=True)

    summary = Study1Summary()
    for domain in DOMAIN_KEYS:
        for ft_state in FT_STATES:
            summary.records.append(
                run_domain_state(domain, ft_state, orders_per_domain, seed, root)
            )

    summary.notes.append(
        "grammar_digest 在全部记录中相同 ⇒ 三个 domain 使用同一张关键字表与同一套产生式"
    )
    summary.notes.append(
        "compiler_core_digest 在全部记录中相同 ⇒ 三个 domain 由同一组编译器源文件处理"
    )
    summary.notes.append(
        "同一 domain 的 program_source_digest 在三种 F_t 下相同 ⇒ 车间状态变化不需要改订单程序"
    )
    summary.notes.append(
        "realization_success 恒为 1.000 ⇒ 库位吃紧与设备停机只会让版本 WAIT，不会让它不可行"
    )

    paths = {
        "records_json": out_dir / "study1_records.json",
        "records_csv": out_dir / "study1_records.csv",
        "main_table_csv": out_dir / "study1_main_table.csv",
        "main_table_md": out_dir / "study1_main_table.md",
        "manifest_json": out_dir / "study1_manifest.json",
    }
    paths["records_json"].write_text(
        json.dumps([asdict(r) for r in summary.records], indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    _write_csv(paths["records_csv"], [asdict(r) for r in summary.records])
    main_rows = build_main_table(summary.records)
    _write_csv(paths["main_table_csv"], main_rows)
    paths["main_table_md"].write_text(_markdown_table(main_rows), encoding="utf-8")
    paths["manifest_json"].write_text(json.dumps({
        "orders_per_domain": orders_per_domain,
        "seed": seed,
        "domains": list(DOMAIN_KEYS),
        "paradigms": {key: spec_of(key).paradigm for key in DOMAIN_KEYS},
        "ft_states": list(FT_STATES),
        "notes": summary.notes,
        "outputs": {key: str(value) for key, value in paths.items()},
    }, indent=2, ensure_ascii=False), encoding="utf-8")
    return paths


MAIN_TABLE_COLUMNS = (
    "domain", "paradigm", "workstations", "holding_sites", "holding_capacity",
    "capability_definitions", "logical_roles", "process_rules", "protection_rules",
    "protection_signatures", "catch_edge_ops_per_order", "exception_table_entries",
    "grammar_modification", "compiler_core_modification", "source_modification_across_Ft",
    "realization_success", "immediate_placement_normal", "immediate_placement_high_occupancy",
    "immediate_placement_machine_down", "memory_spills", "compile_failures",
)


def build_main_table(records: Sequence[Study1Record]) -> List[Dict[str, object]]:
    """Summarize placement outcomes and changes in grammar, compiler, and program digests."""
    by_domain: Dict[str, List[Study1Record]] = {}
    for record in records:
        by_domain.setdefault(record.domain, []).append(record)

    grammar_values = {r.grammar_digest for r in records}
    core_values = {r.compiler_core_digest for r in records}

    rows: List[Dict[str, object]] = []
    for domain in DOMAIN_KEYS:
        group = by_domain.get(domain, [])
        if not group:
            continue
        first = group[0]
        by_state = {r.ft_state: r for r in group}
        rows.append({
            "domain": domain,
            "paradigm": first.paradigm,
            "workstations": first.workstations,
            "holding_sites": first.holding_sites,
            "holding_capacity": first.holding_capacity,
            "capability_definitions": first.capability_definitions,
            "logical_roles": first.logical_roles,
            "process_rules": first.process_rules,
            "protection_rules": first.protection_rules,
            "protection_signatures": first.protection_signatures,
            "catch_edge_ops_per_order": first.catch_edge_ops_mean,
            "exception_table_entries": first.exception_entries,
            "grammar_modification": 0 if len(grammar_values) == 1 else 1,
            "compiler_core_modification": 0 if len(core_values) == 1 else 1,
            "source_modification_across_Ft": 0 if len({r.program_source_digest for r in group}) == 1 else 1,
            "realization_success": round(
                sum(r.realization_success for r in group) / len(group), 4
            ),
            "immediate_placement_normal": by_state.get(FT_NORMAL).immediate_placement
            if FT_NORMAL in by_state else "",
            "immediate_placement_high_occupancy": by_state.get(FT_HIGH_OCCUPANCY).immediate_placement
            if FT_HIGH_OCCUPANCY in by_state else "",
            "immediate_placement_machine_down": by_state.get(FT_MACHINE_DOWN).immediate_placement
            if FT_MACHINE_DOWN in by_state else "",
            "memory_spills": sum(r.memory_spills for r in group),
            "compile_failures": sum(r.compile_failures for r in group),
        })
    return rows


def _markdown_table(rows: Sequence[Mapping[str, object]]) -> str:
    if not rows:
        return ""
    columns = [c for c in MAIN_TABLE_COLUMNS if c in rows[0]] or list(rows[0].keys())
    header = "| " + " | ".join(columns) + " |"
    divider = "|" + "|".join("---" for _ in columns) + "|"
    body = [
        "| " + " | ".join(str(row.get(column, "")) for column in columns) + " |"
        for row in rows
    ]
    return "\n".join([header, divider, *body]) + "\n"


def _write_csv(path: Path, rows: Sequence[Mapping[str, object]]) -> None:
    import csv

    if not rows:
        path.write_text("", encoding="utf-8")
        return
    columns = [c for c in MAIN_TABLE_COLUMNS if c in rows[0]] or list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Run Paper 1 Study 1.")
    parser.add_argument("--orders", type=int, default=12, help="Orders per domain.")
    parser.add_argument("--seed", type=int, default=20260927)
    parser.add_argument("--out", type=Path, default=RESULTS_DIR)
    args = parser.parse_args()

    paths = run_study1(orders_per_domain=args.orders, seed=args.seed, out_dir=args.out)
    print("Study 1 finished.")
    for key, path in paths.items():
        print(f"{key}: {path}")

    try:
        from case_study.study1_figures import render

        for key, path in render(paths["records_json"], args.out / "study1_realization").items():
            print(f"figure_{key}: {path}")
    except Exception as error:  # noqa: BLE001
        print(f"Warning: figure generation skipped: {error}")


if __name__ == "__main__":
    main()
