# F_IDE_Project/case_study/program_generator.py
"""
Paper 1 Method 1 —— Automatic Order Program Generator。

制造语境：定制订单的差别，最终都落在"这张单要装哪些件、装完要检几道、检完怎么包装"上。
如果每来一张定制单都要工艺员重新写一遍程序，定制化带来的工作量就没有被消掉。
本模块把这件事变成**规则驱动的程序生成**：

    订单属性 + BOM + 工艺规则库  →  不可变 FaLang 程序

可复用的是语义核心、能力目录、工艺规则与编译流水线；真正随订单变化的是
process combination 与 process sequence，它们由规则库按订单的组件配置选出。

生成的程序一经产生就**不再改写**：订单中途出现不确定性时，后续处理发生在它的残余 IR 上，
而不是回头重写这段源码。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from case_study.order_generator import PCOrder, QUALITY_GRADES
from compiler.residual_ir import OperationKey, OrderProgram, index_op_instructions


# ----------------------------------------------------------------------
# 能力目录：一个制造 domain 需要哪些逻辑资源角色
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class ResourceRoleSpec:
    """一个逻辑资源角色的声明。只写角色与能力，不写任何物理编号。"""

    name: str
    decl_keyword: str          # 'Resource' / 'Buffer' / 'Storage'
    category: str              # 'Device' / 'Memory'
    capabilities: Tuple[str, ...] = ()


@dataclass(frozen=True)
class CapabilityCatalogue:
    """领域能力目录：换了 domain 只需要换这一份目录。"""

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


# ----------------------------------------------------------------------
# 工艺规则
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class ProcessRule:
    """一条工艺规则：什么时候需要做哪道工序、由哪类角色做。"""

    name: str
    stage: str                      # assembly / testing / packaging
    op_type: str                    # Process / Inspect / Load / Store
    role: Optional[str]             # 逻辑资源角色名；None 表示只作用于物料（Load/Store）
    rp: str                         # 动作描述（rp description）
    component: Optional[str] = None  # 依赖的组件族；None 表示与组件无关
    skip_values: Tuple[str, ...] = ()  # 组件取这些值时跳过该工序
    # 受保护的工序区段名。同一区段内的工序共享一段 try/catch：
    # 区段是"会出同类意外的一段活儿"的边界，不是一个语法概念。
    region: str = ""
    # 是否由订单的质量等级决定做不做（例如检测工序）
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
    """
    一条已经渲染好的预定义保护。

    `body` 是 `catch` 里要执行的语句；`region` 指明它保护哪一段工序。
    同一区段上可以叠多条保护——嵌套 try 即可，被保护的操作会对每一层都挂异常边。
    """

    region: str
    exception_type: str
    body: Tuple[str, ...]
    compensate: Tuple[str, ...] = ()


@dataclass(frozen=True)
class ExceptionRule:
    """
    一段受保护工序区段的预编译异常处理规则。

    `region` 与 `ProcessRule.region`（缺省时取 `stage`）对应；`exception_type` 必须是
    Factory Target Machine Model 的 `runtime_environment.catch_triggers` 中声明过的类型，
    否则该区段内的动作不会产生异常边。
    """

    region: str
    exception_type: str
    # U1 类别键，用于把运行时事件映射回这套预编译步骤（见 compiler/uncertainty.py）
    uncertainty_key: str
    recovery_ops: Tuple[Tuple[str, str, str], ...]  # (op_type, role, rp)
    compensate_ops: Tuple[Tuple[str, str, str], ...] = ()


@dataclass(frozen=True)
class RuleLibrary:
    """一个 domain 的完整工艺规则库。"""

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
    """
    渲染一条处置语句。

    参数个数按 FaLang 自己的约定：Process / Inspect 是"资源 + 物料"，
    Load / Store 只作用于物料，Adjust / Move 是"资源 + 动作描述"。
    """
    if op_type in ("Process", "Inspect"):
        return f'{op_type}({role}, {material}, "{rp}");' if role else f'{op_type}({material}, "{rp}");'
    if op_type in ("Load", "Store"):
        return f'{op_type}({material}, "{rp}");'
    return f'{op_type}({role}, "{rp}");' if role else f'{op_type}({material}, "{rp}");'


def ordered_regions(rules: Sequence[ProcessRule]) -> List[str]:
    """按工序在流程中首次出现的顺序列出区段。"""
    seen: List[str] = []
    for rule in rules:
        region = rule.region or rule.stage
        if region not in seen:
            seen.append(region)
    return seen


# ----------------------------------------------------------------------
# 生成器
# ----------------------------------------------------------------------
class OrderProgramGenerator:
    """把订单变成不可变 FaLang 程序。"""

    def __init__(self, library: RuleLibrary):
        self.library = library

    def ordered_rules_for(self, order: PCOrder) -> List[ProcessRule]:
        """选定该订单要做哪些工序，以及它们的先后顺序。子类可覆盖。"""
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
        """
        该订单需要哪些预定义保护。

        默认实现取规则库里按区段声明的异常规则；按订单属性决定保护的生成器会覆盖它。
        """
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

    # ------------------------------------------------------------------
    def _render(self, order: PCOrder, rules: Sequence[ProcessRule]) -> str:
        catalogue = self.library.catalogue
        # 物料形参名带上订单号：多张订单同时在工作集里时，物料版本必须可区分，
        # 否则不同订单的同名版本会互相冒充。
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

        # 按区段顺序展开：有预定义保护的区段用 try/catch 包起来，
        # 没有保护的区段照常顺序执行。同一区段上可能叠着多条保护，
        # 用嵌套 try 表达——被保护的操作会对每一层活动作用域都挂异常边（见 ir_builder）。
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

    # ------------------------------------------------------------------
    def compile_program(self, proxy, program: OrderProgram):
        """把一个不可变订单程序编译成 IR（每个程序只编译一次）。"""
        results = proxy.compile_order_program(program.source)
        if not results.get("success"):
            raise RuntimeError(
                f"Order program {program.order_id} failed to compile: {results.get('messages')}"
            )
        cfg = results["artifacts"]["cfg"]
        results["artifacts"]["op_instr_ids"] = index_op_instructions(program, cfg)
        return results


# ----------------------------------------------------------------------
# PC assembly domain 的规则库
# ----------------------------------------------------------------------
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
    # 原料从仓储进入车间，是订单语义的一部分：写的是"从仓库取"，不是"取到 3 号库位"
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
    # 上夹具、装外壳这一段依赖工装定位，单独作为一个受保护区段
    ProcessRule("Case_Assembly", "assembly", "Process", "Assembly_PM", "Case_Assembly",
                region="fixture"),
)

# 检测工序由质量等级决定，这里用"每道检测单独一条规则 + 组件置于等级"的方式表达。
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
    # 成品回到仓储，同样是订单语义
    ProcessRule("Outbound_Store", "packaging", "Store", None, "PM_Buffer_To_Warehouse",
                region="verification"),
)

# U1 的三类不确定性各对应一段受保护区段的预编译处理步骤。
# 注意这里没有 MachineFailure 的 catch：设备故障改变可行资源集合，属于 U2，
# 由 Regional Residual Recompilation 处理，不由订单程序里的预定义步骤处理。
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
    """PC 装配 domain 的规则库（当前 case study 使用的默认规则集）。"""
    return RuleLibrary(
        catalogue=PC_ASSEMBLY_CATALOGUE,
        rules=_PC_STAGE_RULES + _PC_PACKAGING_RULES,
        exceptions=_PC_EXCEPTIONS,
    )


def _grade_rules(grade: str) -> Tuple[ProcessRule, ...]:
    return _PC_TEST_RULES.get(grade, _PC_TEST_RULES["basic"])


class PCOrderProgramGenerator(OrderProgramGenerator):
    """按质量等级展开检测工序的 PC 订单程序生成器。"""

    def __init__(self, library: Optional[RuleLibrary] = None):
        super().__init__(library or pc_assembly_library())

    def ordered_rules_for(self, order: PCOrder) -> List[ProcessRule]:
        rules = self.library.rules_of_stage(order, "assembly")
        rules += list(_grade_rules(order.quality_grade))
        rules += self.library.rules_of_stage(order, "packaging")
        return rules
