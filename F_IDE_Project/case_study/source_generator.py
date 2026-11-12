from __future__ import annotations

from typing import Dict, List, Optional, Set, Tuple
from case_study.order_generator import PCOrder, QUALITY_GRADES


ASSEMBLY_STEPS = [
    ("Base_Assembly", "Motherboard"),
    ("CPU_Install", "CPU"),
    ("Memory_Install", "RAM"),
    ("Storage_Install", "Storage"),
    ("GPU_Install", "GPU"),
    ("Cooling_Install", "Cooling"),
    ("Power_Install", None),
    ("Case_Assembly", None),
]


def generate_order_source(order: PCOrder, material_name: str = "PC_Order") -> str:
    lines = [
        "Resource Assembly_PM: Device<Process, Adjust>;",
        "Resource Test_PM: Device<Inspect, Adjust>;",
        "Resource Packaging_PM: Device<Process, Inspect>;",
        "Resource Recovery_PM: Device<Adjust>;",
        "",
        "Storage Warehouse: Memory<>;",
        f"Material {material_name}[1];",
        "",
        f"Task {order.task_name}(Material pc) {{",
        "    try {",
        "        Load(pc, \"Warehouse_To_PM_Buffer\");",
    ]

    for op_name, component_family in ASSEMBLY_STEPS:
        if component_family == "GPU" and order.components["GPU"] == "GPU_NONE":
            continue
        if component_family == "Cooling" and order.components["Cooling"] == "COOLING_NONE":
            continue
        if component_family is None:
            params = op_name
        else:
            params = f"{op_name};component={component_family};model={order.components[component_family]}"
        lines.append(f"        Process(Assembly_PM, pc, \"{params}\");")

    lines.extend(
        [
            "    }",
            "    catch(MaterialMissing) {",
            "        Adjust(Recovery_PM, \"MaterialMissing_Recovery\");",
            "        Move(pc, \"Matrix_Buffer_Move\");",
            "    }",
            "    compensate {",
            "        Adjust(Recovery_PM, \"OperatorUnavailable_Reassign\");",
            "    }",
            "",
            "    try {",
        ]
    )

    for test in QUALITY_GRADES[order.quality_grade]["tests"]:
        lines.append(f"        Inspect(Test_PM, pc, \"{test}\");")

    lines.extend(
        [
            "        Inspect(Packaging_PM, pc, \"Accessory_Check\");",
            "        Process(Packaging_PM, pc, \"Packaging\");",
            "        Store(pc, \"PM_Buffer_To_Warehouse\");",
            "    }",
            "    catch(MachineFailure) {",
            "        Adjust(Recovery_PM, \"MachineFailure_Reset\");",
            "        Move(pc, \"Matrix_Buffer_Move\");",
            "    }",
            "    compensate {",
            "        Adjust(Recovery_PM, \"QualityFault_Rework\");",
            "    }",
            "",
            "    return pc;",
            "}",
            "",
            f"Task Main(Material {material_name}) {{",
            f"    finished = {order.task_name}({material_name});",
            "    return finished;",
            "}",
        ]
    )
    return "\n".join(lines) + "\n"


def generate_unified_source(
    orders: List[PCOrder],
    pending_ops_by_order: Dict[int, List[Dict]],
    runtime_state_device_busy: Optional[Dict[str, float]] = None,
    runtime_state_failed: Optional[Dict[str, float]] = None,
    runtime_state_completed: Optional[Dict[int, int]] = None,
    runtime_state_current_time: float = 0.0,
) -> str:
    """Generate a Main task that invokes each order's pending operations."""
    lines: List[str] = []
    device_busy = runtime_state_device_busy or {}
    device_failed = runtime_state_failed or {}
    completed_seqs = runtime_state_completed or {}

    lines.append("Resource Assembly_PM: Device<Process, Adjust>;")
    lines.append("Resource Test_PM: Device<Inspect, Adjust>;")
    lines.append("Resource Packaging_PM: Device<Process, Inspect>;")
    lines.append("Resource Recovery_PM: Device<Adjust>;")
    lines.append("")
    lines.append("Storage Warehouse: Memory<>;")
    pending_orders = [order for order in orders if order.order_id in pending_ops_by_order]
    lines.append(f"Material RawPC[{len(pending_orders)}];")
    lines.append("")

    for order in orders:
        ops = pending_ops_by_order.get(order.order_id, [])
        if not ops:
            continue
        lines.extend(_generate_pending_order_task(order, ops, completed_seqs.get(order.order_id, -1)))
        lines.append("")

    lines.append("// Entry point for all orders.")
    main_params = ", ".join(
        f"Material raw_{order.order_id:04d}" for order in pending_orders
    )
    lines.append(f"Task Main({main_params}) {{")
    for order in pending_orders:
        material = f"raw_{order.order_id:04d}"
        lines.append(f"    o{order.order_id:04d} = {order.task_name}({material});")
    lines.append(f"    return raw_{pending_orders[0].order_id:04d};")
    lines.append("}")

    return "\n".join(lines) + "\n"


def _generate_pending_order_task(
    order: PCOrder,
    pending_ops: List[Dict],
    completed_seq: int,
) -> List[str]:
    lines: List[str] = []
    lines.append(f"// Order {order.order_id:04d}: {order.quality_grade} grade")
    lines.append(f"Task {order.task_name}(Material pc) {{")

    assembly_ops = [op for op in pending_ops if op.get("stage") == "assembly"]
    testing_ops = [op for op in pending_ops if op.get("stage") == "testing"]
    packaging_ops = [op for op in pending_ops if op.get("stage") == "packaging"]

    if assembly_ops:
        lines.append("    try {")
        if assembly_ops[0]["seq"] == 0 or completed_seq < 0:
            lines.append('        Load(pc, "Warehouse_To_PM_Buffer");')
        for op in assembly_ops:
            name = op["name"]
            comp = op.get("component")
            model = op.get("model")
            if comp and model:
                lines.append(f'        Process(Assembly_PM, pc, "{name};component={comp};model={model}");')
            else:
                lines.append(f'        Process(Assembly_PM, pc, "{name}");')
        lines.append("    }")
        lines.append("    catch(MaterialMissing) {")
        lines.append('        Adjust(Recovery_PM, "MaterialMissing_Recovery");')
        lines.append('        Move(pc, "Matrix_Buffer_Move");')
        lines.append("    }")
        lines.append("    compensate {")
        lines.append('        Adjust(Recovery_PM, "OperatorUnavailable_Reassign");')
        lines.append("    }")
        lines.append("")

    rest_ops = testing_ops + packaging_ops
    if rest_ops:
        lines.append("    try {")
        for op in rest_ops:
            name = op["name"]
            if op.get("stage") == "testing":
                lines.append(f'        Inspect(Test_PM, pc, "{name}");')
            elif name == "Accessory_Check":
                lines.append(f'        Inspect(Packaging_PM, pc, "{name}");')
            elif name == "Packaging":
                lines.append(f'        Process(Packaging_PM, pc, "{name}");')
        lines.append('        Store(pc, "PM_Buffer_To_Warehouse");')
        lines.append("    }")
        lines.append("    catch(MachineFailure) {")
        lines.append('        Adjust(Recovery_PM, "MachineFailure_Reset");')
        lines.append('        Move(pc, "Matrix_Buffer_Move");')
        lines.append("    }")
        lines.append("    compensate {")
        lines.append('        Adjust(Recovery_PM, "QualityFault_Rework");')
        lines.append("    }")

    lines.append("    return pc;")
    lines.append("}")
    return lines
