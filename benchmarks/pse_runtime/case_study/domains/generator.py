# F_IDE_Project/case_study/domains/generator.py
"""
把一个 domain 描述翻译成订单程序生成器所需的规则库与能力目录。

Paper 1 的主张之一是"换了制造门类，只需要换 capability catalogue、工艺规则库与目标机模型"。
本模块就是这句话的实现：它把 `DomainSpec` 机械地展开成生成器认识的结构，
生成器本身（`OrderProgramGenerator`）与编译器核心都不需要任何 domain 知识。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Mapping, Sequence, Tuple

from case_study.domains.spec import DomainSpec, ProtectionRuleSpec
from case_study.program_generator import (
    CapabilityCatalogue,
    OrderProgramGenerator,
    ProcessRule,
    RenderedProtection,
    protection_statement,
    ResourceRoleSpec as CatalogueRoleSpec,
    RuleLibrary,
)


def capability_catalogue_from_spec(spec: DomainSpec) -> CapabilityCatalogue:
    return CapabilityCatalogue(
        domain=spec.key,
        roles=tuple(
            CatalogueRoleSpec(role.name, role.decl_keyword, role.category, role.capabilities)
            for role in spec.roles
        ),
        material_decl=spec.material_decl,
    )


def rule_library_from_spec(spec: DomainSpec) -> RuleLibrary:
    """
    规则库只需要工艺规则；预定义保护由 `protections_for()` 单独给出，
    因为它依赖订单属性，不能笼统地挂在一个区段上。
    """
    rules = tuple(
        ProcessRule(
            name=rule.name,
            stage=rule.region,
            op_type=rule.op_type,
            role=rule.role,
            rp=rule.rp,
            component=rule.component,
            skip_values=tuple(rule.skip_values),
            region=rule.region,
            grade_gated=rule.grade_gated,
        )
        for rule in spec.rules
    )
    return RuleLibrary(
        catalogue=capability_catalogue_from_spec(spec),
        rules=rules,
        exceptions=(),
        material_name=spec.material_name,
    )


class DomainProgramGenerator(OrderProgramGenerator):
    """
    按 domain 的工艺规则库生成不可变订单程序。

    与 PC 专用生成器的区别只有一处：哪些检测工序要做由 `quality_grades` 声明，
    而不是写死在代码里。规则在规则库中的位置即工序在流程中的位置。
    """

    def __init__(self, spec: DomainSpec, library: RuleLibrary = None):
        self.spec = spec
        super().__init__(library or rule_library_from_spec(spec))

    def ordered_rules_for(self, order) -> List[ProcessRule]:
        grade_rules = set(self.spec.quality_grades.get(order.quality_grade, ()))
        selected: List[ProcessRule] = []
        for rule in self.library.rules:
            if not rule.applies_to(order):
                continue
            if rule.grade_gated and rule.name not in grade_rules:
                continue
            selected.append(rule)
        return selected

    def protections_for(self, order) -> List[ProtectionRuleSpec]:
        """该订单需要预定义哪些保护——由订单属性决定。"""
        return [p for p in self.spec.protections if p.applies_to(order)]

    def rendered_protections(self, order) -> List[RenderedProtection]:
        """
        把保护规则渲染成 try/catch 的语句体。

        `redispatch_region=True` 的保护是"在同类逻辑资源上把本区段的工序重做一遍"，
        用于设备故障的转移处置——因为角色是逻辑的，重发同样的语句就会落到另一台工位上。
        """
        rules = self.ordered_rules_for(order)
        material = f"raw_{order.task_name}"
        out: List[RenderedProtection] = []
        for protection in self.protections_for(order):
            if protection.redispatch_region:
                body = []
                for rule in self.library.rules_of_region(rules, protection.region):
                    if rule.role:
                        body.append(
                            f'{rule.op_type}({rule.role}, {material}, "{rule.params_for(order)}");'
                        )
                    else:
                        body.append(f'{rule.op_type}({material}, "{rule.params_for(order)}");')
            else:
                body = [
                    protection_statement(op_type, role, rp, material)
                    for op_type, role, rp in protection.recovery
                ]
            compensate = [
                protection_statement(op_type, role, rp, material)
                for op_type, role, rp in protection.compensate
            ]
            out.append(RenderedProtection(
                region=protection.region,
                exception_type=protection.exception_type,
                body=tuple(body),
                compensate=tuple(compensate),
            ))
        return out

    def nominal_processing_time(self, order) -> float:
        """一张订单的名义加工时间：所有工序预期时间之和。"""
        total = 0.0
        for rule in self.ordered_rules_for(order):
            value = self.spec.action_time(rule.op_type, rule.rp)
            if value:
                total += value
        return total

    def nominal_lead_time(self, order, logistics_allowance: float = 0.35) -> float:
        """
        名义交期基数（单件流经全线的占用时长）。

        订单沿工艺路线**逐站串行**推进，所以在制品同时只占用一台工位；车间的工位数
        决定的是产出速率，不是单件的通过时间。因此这里只按加工时间叠加物流与换型余量，
        不再按工位数折减——按工位数折减会把交期压到短于订单自身的加工时间。
        """
        processing = self.nominal_processing_time(order)
        return processing * (1.0 + logistics_allowance)
