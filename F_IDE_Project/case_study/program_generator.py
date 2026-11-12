"""Generate immutable order programs from component choices and process rules."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from case_study.order_generator import PCOrder, QUALITY_GRADES
from compiler.residual_ir import OperationKey, OrderProgram, index_op_instructions


@dataclass(frozen=True)
class ResourceRoleSpec:
    """Declare a resource role and capabilities without a physical resource identifier."""

    name: str
    decl_keyword: str          # 'Resource', 'Buffer', or 'Storage'.
    category: str              # 'Device' or 'Memory'.
    capabilities: Tuple[str, ...] = ()


@dataclass(frozen=True)
class CapabilityCatalogue:

    domain: str
    roles: Tuple[ResourceRoleSpec, ...]
    material_decl: str = "Material"

    def declaration_lines(self) -> List[str]:
        lines: List[str] = []
        for role in self.roles:
            caps = ", ".join(role.capabilities)
            lines.append(f"{role.decl_keyword} {role.name}: {role.category}<{caps}>;")
        return lines

    def role_names(self) -> Tuple[str, ...]:
        return tuple(role.name for role in self.roles)


@dataclass(frozen=True)
class ProcessRule:

    name: str
    stage: str
    op_type: str
    role: Optional[str]             # None for material-only operations such as Load and Store.
    rp: str
    component: Optional[str] = None  # None for operations independent of component choice.
    skip_values: Tuple[str, ...] = ()
    # Operations in the same region share a protected try/catch scope.
    region: str = ""
    grade_gated: bool = False

    def applies_to(self, order: PCOrder) -> bool:
        if self.component is None:
            return True
        value = order.components.get(self.component)
        if value is None:
            return False
        return value not in self.skip_values

    def params_for(self, order: PCOrder) -> str:
        if self.component is None:
            return self.rp
        return f"{self.rp};component={self.component};model={order.components[self.component]}"


@dataclass(frozen=True)
class RenderedProtection:
    """A catch body for a process region; multiple protections form nested try blocks."""

    region: str
    exception_type: str
    body: Tuple[str, ...]
    compensate: Tuple[str, ...] = ()


@dataclass(frozen=True)
class ExceptionRule:
    """A protected region and response for a target-model exception type."""

    region: str
    exception_type: str
    # Runtime uncertainty category associated with these compiled response steps.
    uncertainty_key: str
    recovery_ops: Tuple[Tuple[str, str, str], ...]  # (op_type, role, rp)
    compensate_ops: Tuple[Tuple[str, str, str], ...] = ()


@dataclass(frozen=True)
class RuleLibrary:

    catalogue: CapabilityCatalogue
    rules: Tuple[ProcessRule, ...]
    exceptions: Tuple[ExceptionRule, ...] = ()
    material_name: str = "PC_Order"

    def ordered_rules(self, order: PCOrder) -> List[ProcessRule]:
        return [rule for rule in self.rules if rule.applies_to(order)]

    def rules_of_stage(self, order: PCOrder, stage: str) -> List[ProcessRule]:
        return [rule for rule in self.ordered_rules(order) if rule.stage == stage]

    def rules_of_region(self, rules: Sequence[ProcessRule], region: str) -> List[ProcessRule]:
        return [rule for rule in rules if self.region_of(rule) == region]

    @staticmethod
    def region_of(rule: ProcessRule) -> str:
        return rule.region or rule.stage

    def exception_for_uncertainty(self, uncertainty_key: str) -> Optional[ExceptionRule]:
        for rule in self.exceptions:
            if rule.uncertainty_key == uncertainty_key:
                return rule
        return None


def protection_statement(op_type: str, role: Optional[str], rp: str, material: str) -> str:
    """Render a response statement using the argument convention of its FaLang operation."""
    if op_type in ("Process", "Inspect"):
        return f'{op_type}({role}, {material}, "{rp}");' if role else f'{op_type}({material}, "{rp}");'
    if op_type in ("Load", "Store"):
        return f'{op_type}({material}, "{rp}");'
    return f'{op_type}({role}, "{rp}");' if role else f'{op_type}({material}, "{rp}");'


def ordered_regions(rules: Sequence[ProcessRule]) -> List[str]:
    seen: List[str] = []
    for rule in rules:
        region = rule.region or rule.stage
        if region not in seen:
            seen.append(region)
    return seen


class OrderProgramGenerator:
    """Generate order programs from a capability catalogue and process rules."""

    def __init__(self, library: RuleLibrary):
        self.library = library

    def ordered_rules_for(self, order: PCOrder) -> List[ProcessRule]:
        """Select and order operations; subclasses can customize selection."""
        return self.library.ordered_rules(order)

    def generate(self, order: PCOrder) -> OrderProgram:
        rules = self.ordered_rules_for(order)
        source = self._render(order, rules)
        operations = tuple(
            OperationKey(order_id=order.task_name, seq=seq, name=rule.name)
            for seq, rule in enumerate(rules)
        )
        return OrderProgram(
            order_id=order.task_name,
            source=source,
            operations=operations,
            meta={
                "domain": self.library.catalogue.domain,
                "quality_grade": order.quality_grade,
                "components": dict(order.components),
                "arrival_time": order.arrival_time,
                "due_time": order.due_time,
            },
        )

    def rendered_protections(self, order) -> List[RenderedProtection]:
        """Return region-based responses; subclasses can select them from order attributes."""
        out: List[RenderedProtection] = []
        material = f"raw_{order.task_name}"
        for exception in self.library.exceptions:
            body = tuple(
                protection_statement(op_type, role, rp, material)
                for op_type, role, rp in exception.recovery_ops
            )
            compensate = tuple(
                protection_statement(op_type, role, rp, material)
                for op_type, role, rp in exception.compensate_ops
            )
            out.append(RenderedProtection(
                region=exception.region,
                exception_type=exception.exception_type,
                body=body,
                compensate=compensate,
            ))
        return out

    def _render(self, order: PCOrder, rules: Sequence[ProcessRule]) -> str:
        catalogue = self.library.catalogue
        # Order-specific material names keep SSA versions distinct in the shared workset.
        material_param = f"raw_{order.task_name}"
        lines: List[str] = [
            f"// Auto-generated order program for {order.task_name} "
            f"({self.library.catalogue.domain}, grade={order.quality_grade})",
        ]
        lines.extend(catalogue.declaration_lines())
        lines.append("")
        lines.append(f"{catalogue.material_decl} {self.library.material_name}[1];")
        lines.append("")
        lines.append(f"Task {order.task_name}(Material {material_param}) {{")

        # Nested try blocks attach each response to the same protected operations.
        protections = self.rendered_protections(order)
        by_region: Dict[str, List[RenderedProtection]] = {}
        for protection in protections:
            by_region.setdefault(protection.region, []).append(protection)

        for region in ordered_regions(rules):
            stage_rules = self.library.rules_of_region(rules, region)
            if not stage_rules:
                continue
            region_protections = by_region.get(region, [])
            depth = len(region_protections)
            for level in range(depth):
                lines.append("    " + "    " * level + "try {")
            body_indent = "    " * (depth + 1)
            for rule in stage_rules:
                if rule.role:
                    lines.append(f'{body_indent}{rule.op_type}({rule.role}, {material_param}, '
                                 f'"{rule.params_for(order)}");')
                else:
                    lines.append(f'{body_indent}{rule.op_type}({material_param}, '
                                 f'"{rule.params_for(order)}");')
            for level in range(depth - 1, -1, -1):
                protection = region_protections[level]
                indent = "    " + "    " * level
                lines.append(f"{indent}}}")
                lines.append(f"{indent}catch({protection.exception_type}) {{")
                for statement in protection.body:
                    lines.append(f"{indent}    {statement}")
                lines.append(f"{indent}}}")
                if protection.compensate:
                    lines.append(f"{indent}compensate {{")
                    for statement in protection.compensate:
                        lines.append(f"{indent}    {statement}")
                    lines.append(f"{indent}}}")
            lines.append("")

        lines.append(f"    return {material_param};")
        lines.append("}")
        return "\n".join(lines) + "\n"

    def compile_program(self, proxy, program: OrderProgram):
        results = proxy.compile_order_program(program.source)
        if not results.get("success"):
            raise RuntimeError(
                f"Order program {program.order_id} failed to compile: {results.get('messages')}"
            )
        cfg = results["artifacts"]["cfg"]
        results["artifacts"]["op_instr_ids"] = index_op_instructions(program, cfg)
        return results


PC_ASSEMBLY_CATALOGUE = CapabilityCatalogue(
    domain="pc_assembly",
    roles=(
        ResourceRoleSpec("Assembly_PM", "Resource", "Device", ("Process", "Adjust")),
        ResourceRoleSpec("Test_PM", "Resource", "Device", ("Inspect", "Adjust")),
        ResourceRoleSpec("Packaging_PM", "Resource", "Device", ("Process", "Inspect")),
        ResourceRoleSpec("Recovery_PM", "Resource", "Device", ("Adjust",)),
        ResourceRoleSpec("Warehouse", "Storage", "Memory", ()),
    ),
)

_PC_STAGE_RULES = (
    ProcessRule("Inbound_Load", "assembly", "Load", None, "Warehouse_To_PM_Buffer"),
    ProcessRule("Base_Assembly", "assembly", "Process", "Assembly_PM", "Base_Assembly",
                component="Motherboard"),
    ProcessRule("CPU_Install", "assembly", "Process", "Assembly_PM", "CPU_Install", component="CPU"),
    ProcessRule("Memory_Install", "assembly", "Process", "Assembly_PM", "Memory_Install", component="RAM"),
    ProcessRule("Storage_Install", "assembly", "Process", "Assembly_PM", "Storage_Install",
                component="Storage"),
    ProcessRule("GPU_Install", "assembly", "Process", "Assembly_PM", "GPU_Install", component="GPU",
                skip_values=("GPU_NONE",)),
    ProcessRule("Cooling_Install", "assembly", "Process", "Assembly_PM", "Cooling_Install",
                component="Cooling", skip_values=("COOLING_NONE",)),
    ProcessRule("Power_Install", "assembly", "Process", "Assembly_PM", "Power_Install"),
    ProcessRule("Case_Assembly", "assembly", "Process", "Assembly_PM", "Case_Assembly",
                region="fixture"),
)

_PC_TEST_RULES = {
    "basic": (
        ProcessRule("Power_On_Test", "testing", "Inspect", "Test_PM", "Power_On_Test",
                    region="verification"),
    ),
    "standard": (
        ProcessRule("Power_On_Test", "testing", "Inspect", "Test_PM", "Power_On_Test",
                    region="verification"),
        ProcessRule("Benchmark_Test", "testing", "Inspect", "Test_PM", "Benchmark_Test",
                    region="verification"),
    ),
    "premium": (
        ProcessRule("Power_On_Test", "testing", "Inspect", "Test_PM", "Power_On_Test",
                    region="verification"),
        ProcessRule("Benchmark_Test", "testing", "Inspect", "Test_PM", "Benchmark_Test",
                    region="verification"),
        ProcessRule("Thermal_Test", "testing", "Inspect", "Test_PM", "Thermal_Test",
                    region="verification"),
    ),
}

_PC_PACKAGING_RULES = (
    ProcessRule("Accessory_Check", "packaging", "Inspect", "Packaging_PM", "Accessory_Check",
                region="verification"),
    ProcessRule("Packaging", "packaging", "Process", "Packaging_PM", "Packaging",
                region="verification"),
    ProcessRule("Outbound_Store", "packaging", "Store", None, "PM_Buffer_To_Warehouse",
                region="verification"),
)

# This rule library handles material and quality events; resource failures use regional recompilation.
_PC_EXCEPTIONS = (
    ExceptionRule(
        region="assembly",
        exception_type="MaterialMissing",
        uncertainty_key="material_shortage",
        recovery_ops=(("Adjust", "Recovery_PM", "MaterialMissing_Replenish"),),
        compensate_ops=(("Adjust", "Recovery_PM", "OperatorUnavailable_Reassign"),),
    ),
    ExceptionRule(
        region="fixture",
        exception_type="ToolingDeviation",
        uncertainty_key="tooling_deviation",
        recovery_ops=(("Adjust", "Recovery_PM", "ToolingDeviation_Adjust"),),
        compensate_ops=(("Adjust", "Recovery_PM", "ToolingDeviation_Recheck"),),
    ),
    ExceptionRule(
        region="verification",
        exception_type="QualityFault",
        uncertainty_key="quality_fault",
        recovery_ops=(("Adjust", "Recovery_PM", "QualityFault_Rework"),),
        compensate_ops=(("Adjust", "Recovery_PM", "QualityFault_Reinspect"),),
    ),
)


def pc_assembly_library() -> RuleLibrary:
    return RuleLibrary(
        catalogue=PC_ASSEMBLY_CATALOGUE,
        rules=_PC_STAGE_RULES + _PC_PACKAGING_RULES,
        exceptions=_PC_EXCEPTIONS,
    )


def _grade_rules(grade: str) -> Tuple[ProcessRule, ...]:
    return _PC_TEST_RULES.get(grade, _PC_TEST_RULES["basic"])


class PCOrderProgramGenerator(OrderProgramGenerator):

    def __init__(self, library: Optional[RuleLibrary] = None):
        super().__init__(library or pc_assembly_library())

    def ordered_rules_for(self, order: PCOrder) -> List[ProcessRule]:
        rules = self.library.rules_of_stage(order, "assembly")
        rules += list(_grade_rules(order.quality_grade))
        rules += self.library.rules_of_stage(order, "packaging")
        return rules
