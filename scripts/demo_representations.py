"""Export a PC order, its CFG, and residual worksets for a supplied progress snapshot."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
from html import escape
import json
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "F_IDE_Project"))

from case_study.order_generator import PCOrderGenerator
from case_study.program_generator import PCOrderProgramGenerator
from compiler.factory_target_machine import FactoryState
from compiler.ir import OpInstr
from compiler.residual_ir import CompiledOrderProgram, ResidualRecord, WorksetComposer
from compiler.visualizer import CFGVisualizer
from core.compiler_proxy import CompilerProxy


def snapshot(compiled, record, state):
    workset = WorksetComposer().compose([compiled], {record.order_id: record}, state)
    fragment = workset.fragments[0]
    instructions = [
        {"block": block.name, "operation": fragment.instr_op[id(instr)],
         "state": fragment.instr_state[id(instr)], "instruction": str(instr)}
        for block in fragment.cfg.blocks for instr in block.instructions
    ]
    return {
        "completed_seq": record.completed_seq,
        "executing_seqs": list(record.executing_seqs),
        "summary": workset.snapshot(),
        "frozen_operations": [op.key for op in fragment.view.frozen_ops],
        "residual_operations": [op.key for op in fragment.view.residual_ops],
        "pending_vregs": sorted(workset.pending_vregs),
        "carried_vregs": sorted(workset.carried_vregs),
        "frozen_vregs": sorted(workset.frozen_vregs),
        "pinned_locations": workset.pinned_locations,
        "instructions": instructions,
    }


def build_demo(seed, completed):
    proxy = CompilerProxy(str(ROOT / "F_IDE_Project/core/factory_target_model.json"))
    generator = PCOrderProgramGenerator()
    order = PCOrderGenerator(seed=seed, load="low").generate(1)[0]
    program = generator.generate(order)
    if not 0 <= completed <= len(program.operations):
        raise ValueError(f"--completed must be between 0 and {len(program.operations)}")
    result = generator.compile_program(proxy, program)
    artifacts = result["artifacts"]
    # 按规则的操作类型和完整参数关联，避免旧索引的顺序回退误认入库/出库指令。
    instruction_index = {}
    for op, rule in zip(program.operations, generator.ordered_rules_for(order), strict=True):
        matches = [id(instr) for block in artifacts["cfg"].blocks for instr in block.instructions
                   if isinstance(instr, OpInstr) and instr.op_type == rule.op_type
                   and instr.params == rule.params_for(order)]
        if len(matches) != 1:
            raise RuntimeError(f"Expected one instruction for {op.name}, found {len(matches)}")
        instruction_index[op.key] = matches
    compiled = CompiledOrderProgram(program, artifacts["cfg"], artifacts["mdg"], instruction_index)
    initial = snapshot(compiled, ResidualRecord(program.order_id), FactoryState())
    running = (completed,) if completed < len(program.operations) else ()
    record = ResidualRecord(program.order_id, completed - 1, running)
    probe = snapshot(compiled, record, FactoryState())
    # 演示进度由用户指定；运行中物料的位置取自初次 RGC 分配，而非仿真测量。
    locations = {vreg: artifacts["alloc_map"][vreg] for vreg in probe["carried_vregs"]
                 if vreg in artifacts["alloc_map"]}
    progress = snapshot(compiled, record, FactoryState(wip_location=locations))
    payload = {
        "kind": "illustrative_progress_snapshot",
        "seed": seed,
        "completed_operations": completed,
        "order_id": program.order_id,
        "order_metadata": dict(program.meta),
        "source_sha256": hashlib.sha256(program.source.encode("utf-8")).hexdigest(),
        "operations": [{"key": op.key, "seq": op.seq, "name": op.name}
                       for op in program.operations],
        "initial_rgc_allocation": artifacts["alloc_map"],
        "initial": initial,
        "progress": progress,
    }
    return program.source, compiled.cfg, payload, result["messages"]


def table(headers, rows):
    head = "".join(f"<th>{escape(str(value))}</th>" for value in headers)
    body = "".join("<tr>" + "".join(f"<td>{escape(str(value))}</td>" for value in row)
                   + "</tr>" for row in rows)
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def render_html(payload, graph_status):
    sections = []
    for key, title in (("initial", "Before execution"), ("progress", "Supplied execution progress")):
        current = payload[key]
        rows = []
        for op in payload["operations"]:
            states = sorted({instr["state"] for instr in current["instructions"]
                             if instr["operation"] == op["key"]})
            rows.append((op["seq"], op["name"], ", ".join(states)))
        instruction_rows = [(row["block"], row["operation"], row["state"], row["instruction"])
                            for row in current["instructions"]]
        sections.append(
            f'<section id="{key}"><h2>{title}</h2>'
            + table(("Sequence (zero-based)", "Operation", "State"), rows)
            + "<h3>Material versions and fixed locations</h3>"
            + table(("Category", "Values"),
                    [(name, json.dumps(current[name], ensure_ascii=False))
                     for name in ("pending_vregs", "carried_vregs", "frozen_vregs", "pinned_locations")])
            + "<details><summary>Inspect annotated CFG instructions</summary>"
            + table(("Block", "Operation key", "State", "Instruction"), instruction_rows)
            + "</details></section>"
        )
    graph = ('<object data="order_cfg.svg" type="image/svg+xml" style="width:100%;height:75vh">'
             '<a href="order_cfg.svg">Open CFG SVG</a></object>' if graph_status == "generated"
             else f"<p>{escape(graph_status)}. The CFG DOT and residual tables are available.</p>")
    return """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>FaLang: order and residual representation</title>
<style>
body{font:16px/1.5 system-ui,sans-serif;max-width:1200px;margin:32px auto;padding:0 20px;color:#172536}
table{border-collapse:collapse;width:100%;margin:16px 0}th,td{border:1px solid #ccd5df;padding:7px;text-align:left;overflow-wrap:anywhere}
th{background:#eef3f8}section{margin:32px 0}summary{cursor:pointer;font-weight:bold}a{color:#075aa0}
code{background:#eef3f8;padding:2px 5px}
</style></head><body><h1>FaLang: order and residual representation</h1>
<p><a href="order.falang">FaLang source</a> · <a href="representations.json">All representation data (JSON)</a>
 · <a href="order_cfg.dot">CFG DOT</a> · <a href="compiler.log">Compiler log</a></p>
<p>This PC assembly example uses the case-study order generator and compiler. The progress below
is supplied for demonstration; it is not a measured simulation trace. No uncertainty event is injected
and residual resource allocation is not rerun. Fixed locations come from the initial RGC allocation.</p>
<p><b>frozen</b>: completed; <b>carried</b>: running, retaining its known material location;
<b>pending</b>: awaiting allocation; <b>handler</b>: inactive exception-response instructions,
excluded from the normal allocation workset. Compiler-inserted transfers and unmapped control
instructions remain pending as returned by the workset composer.</p>
""" + f"<p>Seed: {payload['seed']}; completed operations: {payload['completed_operations']}. " \
        "The next operation is marked running if one remains.</p>" \
        + "<h2>Order choices</h2>" \
        + table(("Attribute", "Value"), [(key, json.dumps(value, ensure_ascii=False))
                                         for key, value in payload["order_metadata"].items()]) \
        + "".join(sections) + "<h2>Full order CFG after lowering</h2>" + graph \
        + "<p>The graph shows the full compiled order; the tables above annotate its residual states.</p></body></html>"


def export_demo(output, seed=9, completed=3, graph=True):
    output = output.resolve()
    results_root = (ROOT / "results").resolve()
    if output == results_root or not output.is_relative_to(results_root):
        raise ValueError("--out must be a new subdirectory of this project's results/ directory")
    if output.exists():
        raise ValueError(f"Refusing to overwrite existing output: {output}")
    source, cfg, payload, messages = build_demo(seed, completed)
    output.mkdir(parents=True)
    (output / "order.falang").write_text(source, encoding="utf-8")
    (output / "order_cfg.dot").write_text(CFGVisualizer.to_dot_cfg_only(cfg), encoding="utf-8")
    graph_status = "Graphviz dot was not found on PATH" if graph else "SVG export disabled"
    dot = shutil.which("dot") if graph else None
    if dot:
        rendered = subprocess.run([dot, "-Tsvg", str(output / "order_cfg.dot"),
                                   "-o", str(output / "order_cfg.svg")],
                                  capture_output=True, text=True, encoding="utf-8", errors="replace")
        graph_status = "generated" if rendered.returncode == 0 else "Graphviz SVG export failed"
        if rendered.stderr:
            messages.append(rendered.stderr)
    payload["cfg_svg"] = graph_status
    (output / "representations.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    (output / "index.html").write_text(render_html(payload, graph_status), encoding="utf-8")
    (output / "compiler.log").write_text("\n".join(messages) + "\n", encoding="utf-8")
    return output / "index.html"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=9)
    parser.add_argument("--completed", type=int, default=3, help="Number of completed operations")
    parser.add_argument("--out", type=Path, help="New output directory below results/")
    parser.add_argument("--no-graph", action="store_true", help="Skip optional Graphviz SVG rendering")
    args = parser.parse_args()
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    output = args.out or Path("results") / f"demo_{stamp}"
    output = ROOT / output if not output.is_absolute() else output
    try:
        page = export_demo(output, args.seed, args.completed, not args.no_graph)
    except (ValueError, RuntimeError) as error:
        parser.exit(1, f"Error: {error}\n")
    print(f"Open in a browser: {page}")


if __name__ == "__main__":
    main()
