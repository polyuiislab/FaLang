"""Convert domain specifications into capability catalogues and program-generation rules."""
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
    """Build process rules; order-specific protection rules are selected separately."""
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
    """Generate order programs using domain rules and quality-grade requirements."""

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
        return [p for p in self.spec.protections if p.applies_to(order)]

    def rendered_protections(self, order) -> List[RenderedProtection]:
        """Render catch bodies; redispatch rules repeat the protected operations on a logical resource."""
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
        total = 0.0
        for rule in self.ordered_rules_for(order):
            value = self.spec.action_time(rule.op_type, rule.rp)
            if value:
                total += value
        return total

    def nominal_lead_time(self, order, logistics_allowance: float = 0.35) -> float:
        """Estimate serial processing time plus transport and setup allowances."""
        processing = self.nominal_processing_time(order)
        return processing * (1.0 + logistics_allowance)
