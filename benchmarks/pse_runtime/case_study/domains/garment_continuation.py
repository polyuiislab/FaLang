"""Study 2 非抢占加工场景：故障后改派尚未执行工序，已完成工序不重做。"""
from dataclasses import replace

from case_study.domains.garment import SPEC as GARMENT_SPEC
from case_study.domains.spec import ProtectionRuleSpec

SPEC = replace(
    GARMENT_SPEC, key="garment_continuation",
    protections=tuple(
        replace(rule, redispatch_region=False,
                recovery=(("Adjust", "Recovery_PM", "Operator_Reassign"),), compensate=())
        if rule.exception_type == "MachineFailure" else rule
        for rule in GARMENT_SPEC.protections) + (
        ProtectionRuleSpec(
            region="cutting", exception_type="MachineFailure",
            uncertainty_key="machine_failure", always=True,
            recovery=(("Adjust", "Recovery_PM", "Operator_Reassign"),)),
    ),
)

COMPACT_SPEC = replace(
    SPEC, key="garment_continuation_compact", holding_slots_per_workstation=1,
    workstations=tuple(replace(ws, registers=ws.registers[:1]) for ws in SPEC.workstations),
)

# 工装随工单转移的生产线：换机后重新装夹、确认设置，共占用操作员 8 分钟。
RETOOLING_SPEC = replace(
    SPEC, key="garment_retooling",
    actions=tuple(replace(action, processing_time=8.0)
                  if action.description == "Operator_Reassign" else action
                  for action in SPEC.actions),
)
