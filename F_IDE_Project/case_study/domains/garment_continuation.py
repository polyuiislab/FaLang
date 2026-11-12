"""Nonpreemptive line model that reroutes pending operations after a failure."""
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

# Transferring a job requires eight minutes of operator time for reclamping and setup.
RETOOLING_SPEC = replace(
    SPEC, key="garment_retooling",
    actions=tuple(replace(action, processing_time=8.0)
                  if action.description == "Operator_Reassign" else action
                  for action in SPEC.actions),
)
