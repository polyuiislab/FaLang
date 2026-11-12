"""Flexible garment line with cutting, sewing, finishing, and packing stages."""
from __future__ import annotations

from typing import Dict

from case_study.domains.spec import (
    ActionSpec,
    DomainSpec,
    ProtectionRuleSpec,
    OptionFamilySpec,
    ProcessRuleSpec,
    ResourceModuleSpec,
    ResourceRoleSpec,
    build_target_model,
    lay_out_workstations,
)

KEY = "garment"

ROLES = (
    ResourceRoleSpec("Cutting_Stage", "Resource", "Device", ("Process",)),
    ResourceRoleSpec("Sewing_Stage", "Resource", "Device", ("Process",)),
    ResourceRoleSpec("Quality_Stage", "Resource", "Device", ("Process", "Inspect")),
        ResourceRoleSpec("Recovery_PM", "Resource", "Device", ("Adjust",)),
    ResourceRoleSpec("Warehouse", "Storage", "Memory", ()),
)

ACTIONS = (
    ActionSpec("Load", "00000", "Fabric_Store_To_Table", 5.0),
    ActionSpec("Store", "00000", "Finished_Goods_To_Store", 5.0),
    ActionSpec("Process", "00001", "Cut_Fabric", 9.0),
    ActionSpec("Process", "00010", "Cut_Lining", 9.0),
    ActionSpec("Process", "00011", "Sew_Body", 4.5),
    ActionSpec("Process", "00100", "Sew_Sleeve", 4.5),
    ActionSpec("Process", "00101", "Attach_Collar", 4.5),
    ActionSpec("Process", "00110", "Hem_Finish", 5.0),
    ActionSpec("Process", "00111", "Press_Finish", 4.5),
    ActionSpec("Process", "01001", "Fold_Pack", 6.0),
    ActionSpec("Inspect", "00001", "Stitch_Check", 6.0),
    ActionSpec("Inspect", "00010", "Measurement_Check", 6.0),
    ActionSpec("Adjust", "00001", "Fabric_Replenish", 7.0),
    ActionSpec("Adjust", "00010", "Thread_Tension_Adjust", 5.0),
    ActionSpec("Adjust", "00011", "Garment_Rework", 9.0),
    ActionSpec("Adjust", "00100", "Operator_Reassign", 4.0),
)

RESOURCE_MODULES = (
    ResourceModuleSpec(
        palu_id=0,
        device_type="Cutting_Machine",
        quantity=2,
        actions=(("Process", "00001"), ("Process", "00010")),
    ),
    ResourceModuleSpec(
        palu_id=1,
        device_type="Sewing_Machine",
        quantity=4,
        actions=(
            ("Process", "00011"),
            ("Process", "00100"),
            ("Process", "00101"),
            ("Process", "00110"),
        ),
    ),
    ResourceModuleSpec(
        palu_id=2,
        device_type="Press_Station",
        quantity=3,
        actions=(("Process", "00111"),),
    ),
    ResourceModuleSpec(
        palu_id=6,
        device_type="Packing_Station",
        quantity=1,
        actions=(("Process", "01001"),),
    ),
    ResourceModuleSpec(
        palu_id=3,
        device_type="Conveyor_AGV",
        quantity=5,
        actions=(("Move", "00000"), ("Load", "00000"), ("Store", "00000")),
    ),
    ResourceModuleSpec(
        palu_id=4,
        device_type="Quality_Bench",
        quantity=2,
        actions=(("Inspect", "00001"), ("Inspect", "00010")),
    ),
    ResourceModuleSpec(
        palu_id=5,
        device_type="Recovery_Operator",
        quantity=2,
        actions=(
            ("Adjust", "00001"),
            ("Adjust", "00010"),
            ("Adjust", "00011"),
            ("Adjust", "00100"),
        ),
    ),
)

PARADIGM = "flexible_assembly_line"

# Each stage has parallel workstations; orders follow the cutting-to-packing route.
WORKSTATIONS = lay_out_workstations(
    rows=3,
    cols=3,
    slots=(
        ("cutting", (0, 3)),
        ("cutting", (0, 3)),
        ("cutting", (0, 3)),
        ("sewing", (1, 2, 3)),
        ("sewing", (1, 2, 3)),
        ("sewing", (1, 2, 3)),
        # Packing has one eligible workstation.
        ("inspection", (4, 3, 5)),
        ("inspection", (4, 3, 5)),
        ("packing", (6, 3, 5)),
    ),
    slots_per_workstation=2,
    coords=((0, 0), (1, 0), (2, 0), (3, 0), (4, 0), (5, 0), (6, 0), (7, 0), (8, 0)),
)

OPTION_FAMILIES = (
    OptionFamilySpec("Fabric", ("COTTON", "LINEN", "WOOL", "BLEND"), setup_step=4.0),
    OptionFamilySpec("Style", ("SLIM", "REGULAR", "LOOSE"), setup_step=2.0),
    OptionFamilySpec("Sleeve", ("SLEEVELESS", "SHORT", "LONG"), skip_model="SLEEVELESS",
                     setup_step=2.0),
    OptionFamilySpec("Collar", ("COLLARLESS", "ROUND", "POINTED"), skip_model="COLLARLESS",
                     setup_step=2.0),
)

QUALITY_GRADES: Dict[str, tuple] = {
    "basic": ("Stitch_Check",),
    "standard": ("Stitch_Check", "Measurement_Check"),
}

RULES = (
    ProcessRuleSpec("Inbound_Load", "cutting", "Load", None, "Fabric_Store_To_Table"),
    ProcessRuleSpec("Cut_Fabric", "cutting", "Process", "Cutting_Stage", "Cut_Fabric",
                    component="Fabric"),
    ProcessRuleSpec("Cut_Lining", "cutting", "Process", "Cutting_Stage", "Cut_Lining"),
    ProcessRuleSpec("Sew_Body", "sewing", "Process", "Sewing_Stage", "Sew_Body",
                    component="Style"),
    ProcessRuleSpec("Sew_Sleeve", "sewing", "Process", "Sewing_Stage", "Sew_Sleeve",
                    component="Sleeve", skip_values=("SLEEVELESS",)),
    ProcessRuleSpec("Attach_Collar", "sewing", "Process", "Sewing_Stage", "Attach_Collar",
                    component="Collar", skip_values=("COLLARLESS",)),
    ProcessRuleSpec("Press_Finish", "sewing", "Process", "Sewing_Stage", "Press_Finish",
                    component="Fabric"),
    ProcessRuleSpec("Stitch_Check", "inspection", "Inspect", "Quality_Stage", "Stitch_Check",
                    grade_gated=True),
    ProcessRuleSpec("Measurement_Check", "inspection", "Inspect", "Quality_Stage",
                    "Measurement_Check", grade_gated=True),
    ProcessRuleSpec("Fold_Pack", "inspection", "Process", "Quality_Stage", "Fold_Pack"),
    ProcessRuleSpec("Outbound_Store", "inspection", "Store", None, "Finished_Goods_To_Store"),
)

PROTECTIONS = (
    # Predefine replenishment for the long-lead-time fabrics.
    ProtectionRuleSpec(
        region="cutting",
        exception_type="MaterialMissing",
        uncertainty_key="material_shortage",
        when_component="Fabric", when_values=("WOOL", "LINEN"),
        recovery=(("Load", None, "Fabric_Store_To_Table"),
                  ("Adjust", "Recovery_PM", "Fabric_Replenish")),
        compensate=(("Adjust", "Recovery_PM", "Operator_Reassign"),),
    ),
    ProtectionRuleSpec(
        region="sewing",
        exception_type="ToolingDeviation",
        uncertainty_key="tooling_deviation",
        always=True,
        recovery=(("Adjust", "Recovery_PM", "Thread_Tension_Adjust"),),
        compensate=(("Adjust", "Recovery_PM", "Thread_Tension_Adjust"),),
    ),
    # Repeat the sewing region on a compatible workstation after a failure.
    ProtectionRuleSpec(
        region="sewing",
        exception_type="MachineFailure",
        uncertainty_key="machine_failure",
        always=True,
        redispatch_region=True,
        compensate=(("Adjust", "Recovery_PM", "Operator_Reassign"),),
    ),
    ProtectionRuleSpec(
        region="inspection",
        exception_type="QualityFault",
        uncertainty_key="quality_fault",
        when_grades=("standard",),
        recovery=(("Adjust", "Recovery_PM", "Garment_Rework"),
                  ("Inspect", "Quality_Stage", "Stitch_Check")),
        compensate=(("Adjust", "Recovery_PM", "Garment_Rework"),),
    ),
)
CATCH_TRIGGERS = {
    "MaterialMissing": (("Load", "00000"), ("Process", "00001")),
    "ToolingDeviation": (("Process", "00011"), ("Process", "00101")),
    "QualityFault": (("Inspect", "00001"), ("Inspect", "00010")),
    "MachineFailure": (("Process", "00001"), ("Process", "00011")),
}

SPEC = DomainSpec(
    key=KEY,
    label="Customized_Garment_Assembly_Line",
    scenario="flexible assembly line for customized garment sewing",
    material_decl="Material",
    material_name="Garment_Order",
    paradigm=PARADIGM,
    roles=ROLES,
    actions=ACTIONS,
    resource_modules=RESOURCE_MODULES,
    workstations=WORKSTATIONS,
    option_families=OPTION_FAMILIES,
    quality_grades=QUALITY_GRADES,
    rules=RULES,
    protections=PROTECTIONS,
    catch_triggers=CATCH_TRIGGERS,
    rows=3,
    cols=3,
    holding_slots_per_workstation=2,
    holding_capacity=2,
    warehouse_capacity=5000,
    arrival_mtba={"low": 20.0, "medium": 15.0, "high": 10.0},
)


def target_model() -> Dict[str, object]:
    return build_target_model(SPEC)
